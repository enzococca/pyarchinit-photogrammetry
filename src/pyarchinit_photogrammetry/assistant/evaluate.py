"""The evaluation of the assistant: a fixed set of questions and script tasks on the demo project, asked to a model.

The set (``assistant/evaluation/cases.json``, package data: :data:`CASES`) has three parts:

* ``questions``: asked one by one, each in a fresh conversation, with the project open; the answer passes when every
  regular expression of ``must_contain`` is found in it (case-insensitive). ``tools_expected`` are the fact tools a
  good answer reads: the ones the model did not call are said in the detail, they do not decide the result.
* ``scripts``: a task for which the model must give a script; the script is taken from the answer, run with
  :func:`.scripts.run_script` on a copy of the project (the original is never touched) and then the ``check`` - a
  Python expression over ``doc``, ``chunk`` (the chunk active before the script), ``result`` (the
  :class:`.scripts.ScriptResult`), ``polygon``, ``Path`` and ``np`` - must be true. When the script fails or the check
  is false, the error goes back to the model once, as *Errore? Chiedi all'assistente* of the pane does, and the
  corrected script runs on a fresh copy (attempt 2). A script may run :data:`SCRIPT_TIMEOUT` seconds: a slower one is
  stopped and counted as failed ("tempo scaduto").
* ``actions`` (kind "action"): a request to do something, asked with the inventory of the project
  (:func:`.inventory.snapshot`, no window) and an ``act`` that only records the calls (:class:`_Window`: nothing is
  run). A case with ``tool`` passes when the model called that action tool with arguments that include
  ``args_contain`` (the ``params`` of a build are looked into too; numbers compared to 1e-6, texts without case); a
  case without ``tool`` passes when no action was called and the answer matches every ``must_contain`` - the
  ambiguous request, whose right answer is ONE question. ``setup`` prepares a copy of the project first:
  "two_orthophotos" = the chunk renamed "Area nord" and a second chunk "Area sud" with a copy of its orthophoto;
  ``shown`` ("ortho", "dem") is what the 2D view shows in the inventory (nothing otherwise): with two orthophotos
  and one shown the model works on the one shown, with none shown it asks which.

Every case also carries a reference answer (``example_it``/``example_en`` for a question and for an action - after
the call ``example_args`` of its ``tool`` -, ``example`` for a script):
:class:`CasesProvider` answers with them, which tests the runner without a model and, on the demo project, shows that
every check can be met (``ppg assistant eval PROJECT --kind reference``).

The score of a model is ``score_scripts``, the script tasks done within the two attempts; under 2 the model is
"limited for scripts" (the settings of the assistant show it, :func:`record_score`).
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.progress import ProgressCallback, as_progress
from .knowledge import PACKAGE, default_index
from .providers import Message, Provider, Reply, ToolSpec
from .session import ERROR_QUESTION, NOT_SENT, TOOL_LABEL, Assistant, extract_script

CASES = PACKAGE / "evaluation" / "cases.json"  # package data: the installed program and the app have it too
REFERENCE = "reference"  # the kind of CasesProvider: the reference answers of the set, not a model
SCRIPT_TIMEOUT = 120.0  # seconds a script of the evaluation may run; then it is stopped and counted as failed
TIMED_OUT = "tempo scaduto"
LIMITED_UNDER = 2  # script tasks done: under this the model is "limited for scripts"
KINDS = ("all", "questions", "scripts", "actions")
AREAS = ("Area nord", "Area sud")  # the labels of the chunks of the setup "two_orthophotos"


@dataclass
class CaseResult:
    """One case: ``kind`` "question", "script" or "action"; ``attempts`` 1 or 2 (a script asked again after its error);
    ``detail`` why it failed (or what it read); ``elapsed`` seconds with the model and the script;
    ``sent_chars`` the length of the texts composed for the model (they leave this computer only with an external
    model)."""
    id: str
    kind: str
    passed: bool
    attempts: int
    detail: str
    elapsed: float
    sent_chars: int


def load_cases(path: str | Path = CASES) -> dict:
    """The set of ``path``: ``{"questions": [...], "scripts": [...], "actions": [...]}``."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"evaluation set not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"questions": list(data.get("questions") or []), "scripts": list(data.get("scripts") or []),
            "actions": list(data.get("actions") or [])}


def _same(value: Any, wanted: Any) -> bool:
    if isinstance(wanted, bool) or isinstance(value, bool):
        return value is wanted
    if isinstance(wanted, (int, float)) and isinstance(value, (int, float)):
        return abs(float(value) - float(wanted)) <= 1e-6
    if isinstance(wanted, str) and isinstance(value, str):
        return value.strip().lower() == wanted.strip().lower()
    if isinstance(wanted, (list, tuple)) and isinstance(value, (list, tuple)):
        return len(value) == len(wanted) and all(_same(v, w) for v, w in zip(value, wanted))
    return value == wanted


def _contains(args: dict, wanted: dict) -> bool:
    """Whether the arguments of a call include every key of ``wanted`` with its value (also inside ``params``)."""
    args = args if isinstance(args, dict) else {}
    params = args.get("params") if isinstance(args.get("params"), dict) else {}
    for key, value in (wanted or {}).items():
        if key in args and _same(args[key], value):
            continue
        if key in params and _same(params[key], value):
            continue
        return False
    return True


def _lang(language: str) -> str:
    return "it" if (language or "it").lower().startswith("it") else "en"


def _asks(text: str, question: str | None) -> bool:
    """Whether the user message ``text`` is ``question``: its whole first line (the guide passages follow after a
    blank line), never only its beginning - "Disegna le pietre sull'ortofoto." is not the case that goes on with
    ", solo quelle più grandi di 10 cm"."""
    return bool(question) and (text or "").split("\n", 1)[0].strip() == question.strip()


class CasesProvider:
    """A provider that answers from the cases themselves: a question with its ``example_<lang>``, a script task with
    its ``example`` in a ```python block (also when asked about an error); anything else with "?". Local: nothing
    leaves."""

    kind = REFERENCE
    model = "cases"
    label = "Risposte di riferimento del set"

    def __init__(self, cases: str | Path | dict = CASES, language: str = "it"):
        self.cases = cases if isinstance(cases, dict) else load_cases(cases)
        self.language = _lang(language)

    def _answer_for(self, text: str) -> str | None:
        for case in self.cases.get("actions") or []:
            if any(_asks(text, case.get(f"question_{lang}")) for lang in ("it", "en")):
                return case.get(f"example_{self.language}") or case.get("example_it", "")
        for case in self.cases["questions"]:
            if any(_asks(text, case.get(f"question_{lang}")) for lang in ("it", "en")):
                return case.get(f"example_{self.language}") or case.get("example_it", "")
        for case in self.cases["scripts"]:
            if any(_asks(text, case.get(f"task_{lang}")) for lang in ("it", "en")):
                return f"```python\n{case.get('example', '')}\n```"
        return None

    def complete(self, messages: list[Message], tools: list[ToolSpec] | None = None, on_text=None,
                 cancel=None) -> Reply:
        # the last user message that is a case (an error question names no case: the task before it does)
        answer = None
        answered = False  # a tool result came after it: the call was made
        for message in reversed(messages):
            if message.role == "tool":
                answered = True
            if message.role == "user":
                call = None if answered else self._call_for(message.content, tools)
                if call is not None:
                    return Reply("", [call], "tool_calls", {})
                answer = self._answer_for(message.content)
                if answer is not None:
                    break
        answer = answer if answer is not None else "?"
        if on_text is not None:
            on_text(answer)
        return Reply(answer, [], "end", {})

    def _call_for(self, text: str, tools: list[ToolSpec] | None):
        """The call ``example_args`` of the action case asked by ``text``, when its tool is offered."""
        from .providers import ToolCall

        offered = {t.name for t in tools or []}
        for case in self.cases.get("actions") or []:
            if case.get("tool") in offered and \
                    any(_asks(text, case.get(f"question_{lang}")) for lang in ("it", "en")):
                return ToolCall(f"reference_{case['id']}", case["tool"], dict(case.get("example_args") or {}))
        return None

    def list_models(self) -> list[str]:
        return [self.model]

    def check(self) -> str:
        return self.label


# ---------------------------------------------------------------- running

def _sent_chars(turn) -> int:
    marks = tuple(NOT_SENT.values())
    total = 0
    for entry in turn.sent:
        for mark in marks:
            if entry.startswith(mark):
                entry = entry[len(mark):]
                break
        total += len(entry)
    return total


def _tools_read(turn) -> list[str]:
    prefixes = tuple(f"{label}: " for label in TOOL_LABEL.values())
    names = []
    for entry in turn.read:
        if entry.startswith(prefixes):
            names.append(entry.split(": ", 1)[1].split(" (", 1)[0])
    return names


def _short(text: str, n: int = 300) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _copy(project: Path, folder: Path, n: int):
    """A copy of ``project`` (the .ppg and its files) in ``folder``, opened: the photos stay where they are."""
    from ..core.document import Document

    doc = Document.open(project)
    doc.save(folder / f"copy_{n}" / project.name)  # "save as" copies the .files folder; photo paths stay valid
    return Document.open(doc.path)


def _polygon(case: dict, chunk) -> Any:
    """``case["polygon"]``: ``[[x0, y0], [x1, y1]]`` as fractions of the extent of the DEM (or of the orthomosaic)
    of ``chunk``: the box between them, in the chunk's coordinates; None without one."""
    spec = case.get("polygon")
    if not spec:
        return None
    import rasterio
    from shapely.geometry import box

    for product in ("dem", "orthomosaic"):
        path = chunk.product_path(product)
        if path and Path(path).exists():
            with rasterio.open(path) as src:
                b = src.bounds
            (fx0, fy0), (fx1, fy1) = spec
            return box(b.left + fx0 * (b.right - b.left), b.bottom + fy0 * (b.top - b.bottom),
                       b.left + fx1 * (b.right - b.left), b.bottom + fy1 * (b.top - b.bottom))
    raise ValueError(f"case {case.get('id')}: a polygon needs a DEM or an orthomosaic")


class _Window:
    """``window`` of an evaluated script or action: what a script asks of the window that makes sense without one
    (``mark_modified``, ``selected_polygon``); for the actions :meth:`inventory` (the project's snapshot, no view)
    and :meth:`act`, which records the call in ``calls`` and runs nothing; anything else is an error naming it."""

    def __init__(self, polygon, document=None, shown: str | None = None):
        self._polygon = polygon
        self._document = document
        self._shown = shown
        self.calls: list[tuple[str, dict]] = []

    def inventory(self) -> dict | None:
        """What the pane would send: the snapshot of the project (no window here: nothing selected), with the 2D view
        showing ``shown`` of the active chunk ("ortho" or "dem") when the case says so, else nothing."""
        from . import inventory

        snap = inventory.snapshot(self._document)
        if snap is not None and self._shown in ("ortho", "dem"):
            snap["view"].update(kind=self._shown, raster="orthomosaic" if self._shown == "ortho" else "dem")
        return snap

    def act(self, name: str, args: dict) -> dict:
        """The action recorded, not run: the model is told it ran (the evaluation looks at the call only)."""
        self.calls.append((name, dict(args)))
        return {"ran": True, "evaluation": "recorded, not run"}

    def mark_modified(self) -> None:
        """Nothing to do: the copy is thrown away."""

    def selected_polygon(self):
        return self._polygon

    def __getattr__(self, name):
        raise AttributeError(f"window.{name} is not available in the evaluation (no window)")


def _run_limited(script: str, namespace: dict, name: str, timeout: float):
    """``run_script`` in a thread of its own for at most ``timeout`` seconds: then the progress is cancelled (the
    steps of the engine and ``cancel()`` stop on it) and, if the script still runs a little later, it is left behind
    (a daemon thread on a copy that is thrown away). Returns the result, or None when the time ran out."""
    import threading

    from ..core.progress import Cancelled, Progress
    from .scripts import run_script

    progress = Progress()
    box: dict = {}

    def target():
        try:
            box["result"] = run_script(script, namespace, progress, name=name)
        except Cancelled as exc:
            if progress.cancel_requested:  # stopped by the limit
                box["result"] = None
            else:  # the script raised Cancelled by itself: its own failure, not the time
                import traceback

                from .scripts import ScriptResult

                box["result"] = ScriptResult("", f"Cancelled: {exc}".rstrip(": "), traceback.format_exc())
        except BaseException as exc:  # noqa: BLE001 - reported as the script's failure
            box["error"] = exc

    thread = threading.Thread(target=target, name=f"eval-{name}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        progress.cancel()
        thread.join(min(10.0, max(0.5, timeout / 4)))
        return None
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _run_on_copy(script: str, case: dict, project: Path, folder: Path, n: int,
                 timeout: float = SCRIPT_TIMEOUT) -> tuple[bool, str, Any]:
    """Runs ``script`` on a fresh copy for at most ``timeout`` seconds; (check passed, detail, the script's result:
    None when the time ran out)."""
    import numpy as np

    import pyarchinit_photogrammetry as ppg

    from ..app import app as application

    doc = _copy(project, folder, n)
    chunk = doc.chunk
    polygon = _polygon(case, chunk)
    namespace = {"doc": doc, "chunk": chunk, "app": application, "ppg": ppg, "np": np, "polygon": polygon,
                 "cameras": [], "window": _Window(polygon), "__name__": "__main__"}
    before = application.document
    application.document = doc
    try:
        result = _run_limited(script, namespace, f"{case['id']}.py", timeout)
    finally:
        application.document = before
    if result is None:
        return False, f"{TIMED_OUT} ({timeout:g} s)", None
    if result.error:
        return False, result.traceback or result.error, result
    try:
        ok = bool(eval(case["check"], {"__builtins__": __builtins__},  # noqa: S307 - the set's own expression
                       {"doc": doc, "chunk": chunk, "result": result, "polygon": polygon, "Path": Path, "np": np}))
    except Exception as exc:  # noqa: BLE001 - a check that cannot be evaluated is a failed task
        return False, f"the check raised {type(exc).__name__}: {exc}", result
    return ok, "" if ok else f"the check is false: {case['check']}", result


def _question(assistant: Assistant, case: dict, lang: str) -> CaseResult:
    t0 = time.monotonic()
    turn = assistant.ask(case[f"question_{lang}"])
    elapsed = time.monotonic() - t0
    if turn.error:
        return CaseResult(case["id"], "question", False, 1, f"error: {turn.error}", elapsed, _sent_chars(turn))
    missing = [p for p in case.get("must_contain", []) if not re.search(p, turn.text or "", re.IGNORECASE)]
    read = _tools_read(turn)
    not_read = [t for t in case.get("tools_expected", []) if t not in read]
    parts = []
    if missing:
        parts.append("missing: " + ", ".join(f"/{p}/" for p in missing))
    parts.append("tools: " + (", ".join(read) or "none"))
    if not_read:
        parts.append("not called: " + ", ".join(not_read))
    if missing:
        parts.append("answer: " + _short(turn.text))
    return CaseResult(case["id"], "question", not missing, 1, "; ".join(parts), elapsed, _sent_chars(turn))


def _script(assistant: Assistant, case: dict, lang: str, project: Path, folder: Path, attempts: int,
            timeout: float = SCRIPT_TIMEOUT) -> CaseResult:
    t0 = time.monotonic()
    sent = 0
    detail = ""
    turn = assistant.ask(case[f"task_{lang}"])
    for attempt in range(1, max(1, attempts) + 1):
        sent += _sent_chars(turn)
        if turn.error:
            detail = f"error: {turn.error}"
            return CaseResult(case["id"], "script", False, attempt, detail, time.monotonic() - t0, sent)
        script = turn.script or extract_script(turn.text)
        if not script:
            detail = "no script in the answer: " + _short(turn.text)
            return CaseResult(case["id"], "script", False, attempt, detail, time.monotonic() - t0, sent)
        try:
            ok, detail, result = _run_on_copy(script, case, project, folder, attempt, timeout)
        except Exception as exc:  # noqa: BLE001 - the copy or the polygon failed: the case cannot run
            return CaseResult(case["id"], "script", False, attempt, f"not run: {exc}", time.monotonic() - t0, sent)
        if ok:
            return CaseResult(case["id"], "script", True, attempt, "", time.monotonic() - t0, sent)
        error = detail  # the whole traceback goes to the model; the result keeps its last line
        if result is not None and result.error:
            detail = result.error
        if attempt >= attempts:
            break
        if result is None:  # too slow: said so
            error = (f"The script was stopped after {timeout:g} seconds ({TIMED_OUT}): "
                     "give a script that does the task faster.")
        elif not result.error:  # it ran but did not do the task: its output is the hint
            error = "The script ran without errors but did not do what was asked."
            if result.output.strip():
                error += "\n" + result.output.strip()[-2000:]
        turn = assistant.ask_about_error(script, error)
    return CaseResult(case["id"], "script", False, attempts, _short(detail, 600), time.monotonic() - t0, sent)


def _setup(case: dict, project: Path, folder: Path):
    """The project of an action case: the project itself, or a copy prepared by ``case["setup"]``."""
    from ..core.document import Document

    setup = case.get("setup")
    if not setup:
        return Document.open(project)  # the actions are only recorded: nothing is written
    if setup != "two_orthophotos":
        raise ValueError(f"case {case.get('id')}: unknown setup {setup!r}")
    doc = _copy(project, folder, 0)
    first = doc.chunk
    ortho = first.product_path("orthomosaic") if first is not None else None
    if ortho is None or not Path(ortho).exists():
        raise ValueError(f"case {case.get('id')}: the project needs an orthophoto")
    first.label = AREAS[0]
    second = doc.add_chunk(AREAS[1])
    second.crs = first.crs
    second.data_dir.mkdir(parents=True, exist_ok=True)
    target = second.data_dir / Path(ortho).name
    shutil.copy2(ortho, target)
    second._set_product("orthomosaic", target)
    doc.chunk = first
    doc.save()
    return doc


def _action(assistant: Assistant, case: dict, lang: str, window: _Window) -> CaseResult:
    t0 = time.monotonic()
    turn = assistant.ask(case[f"question_{lang}"], inventory=window.inventory(), act=window.act)
    elapsed = time.monotonic() - t0
    if turn.error:
        return CaseResult(case["id"], "action", False, 1, f"error: {turn.error}", elapsed, _sent_chars(turn))
    called = [(f"act_{name}", args) for name, args in window.calls]
    names = ", ".join(f"{name} {json.dumps(args, ensure_ascii=False, sort_keys=True)}" for name, args in called) \
        or "none"
    tool = case.get("tool")
    if tool:
        ok = any(name == tool and _contains(args, case.get("args_contain") or {}) for name, args in called)
        detail = f"called: {names}" if ok else \
            f"wanted {tool} with {json.dumps(case.get('args_contain') or {}, ensure_ascii=False)}; called: {names}"
    else:
        missing = [p for p in case.get("must_contain", []) if not re.search(p, turn.text or "", re.IGNORECASE)]
        ok = not called and not missing
        parts = [f"called: {names}"]
        if missing:
            parts.append("missing: " + ", ".join(f"/{p}/" for p in missing))
        if not ok:
            parts.append("answer: " + _short(turn.text))
        detail = "; ".join(parts)
    return CaseResult(case["id"], "action", ok, 1, detail, elapsed, _sent_chars(turn))


def run_eval(provider: Provider, project: Path, cases: Path = CASES, kinds: str = "all", attempts: int = 2,
             progress: ProgressCallback | None = None, language: str = "it",
             script_timeout: float = SCRIPT_TIMEOUT) -> dict:
    """Asks the set ``cases`` to ``provider`` on ``project`` (a .ppg, never changed: the scripts run on copies in a
    temporary folder, each for at most ``script_timeout`` seconds: then it fails with "tempo scaduto") in
    ``language``; ``kinds`` "all", "questions", "scripts" or "actions". The result::

        {"provider": label, "kind", "model", "language", "date", "elapsed",
         "questions": {"passed": n, "of": m, "results": [CaseResult as dict, ...]},
         "scripts": {"passed": n, "of": m, "first_try": k, "results": [...]},
         "actions": {"passed": n, "of": m, "results": [...]},
         "score_scripts": script tasks done within ``attempts`` (None when the scripts were not asked),
         "limited_for_scripts": score_scripts < 2 (None likewise)}
    """
    if kinds not in KINDS:
        raise ValueError(f"kinds: one of {', '.join(KINDS)}")
    project = Path(project).resolve()
    if not project.is_file():
        raise FileNotFoundError(project)
    data = cases if isinstance(cases, dict) else load_cases(cases)
    lang = _lang(language)
    progress = as_progress(progress)
    questions = data["questions"] if kinds in ("all", "questions") else []
    scripts = data["scripts"] if kinds in ("all", "scripts") else []
    action_cases = data.get("actions", []) if kinds in ("all", "actions") else []
    total = max(1, len(questions) + len(scripts) + len(action_cases))
    try:
        index = default_index(lang)
    except Exception:  # noqa: BLE001 - without the guide the questions are still asked
        index = None
    from ..core.document import Document

    t0 = time.monotonic()
    q_results: list[CaseResult] = []
    s_results: list[CaseResult] = []
    a_results: list[CaseResult] = []
    done = 0
    if questions:
        doc = Document.open(project)  # the tools only read
        for case in questions:
            progress(done / total, f"question {case['id']}")
            assistant = Assistant(provider, lambda: doc, index, language=lang)
            q_results.append(_question(assistant, case, lang))
            done += 1
    with tempfile.TemporaryDirectory(prefix="ppg_eval_") as tmp:
        for case in scripts:
            progress(done / total, f"script {case['id']}")
            folder = Path(tmp) / case["id"]
            folder.mkdir()
            holder: dict = {}
            assistant = Assistant(provider, lambda: holder.get("doc"), index, language=lang)
            try:
                holder["doc"] = Document.open(project)  # what the fact tools see while the model writes
                s_results.append(_script(assistant, case, lang, project, folder, attempts, script_timeout))
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            done += 1
        for case in action_cases:
            progress(done / total, f"action {case['id']}")
            folder = Path(tmp) / f"action_{case['id']}"
            folder.mkdir()
            try:
                doc_a = _setup(case, project, folder)
                window = _Window(None, doc_a, shown=case.get("shown"))
                assistant = Assistant(provider, lambda d=doc_a: d, index, language=lang)
                a_results.append(_action(assistant, case, lang, window))
            except Exception as exc:  # noqa: BLE001 - a case that cannot be prepared fails, the others go on
                a_results.append(CaseResult(case["id"], "action", False, 1, f"not run: {exc}", 0.0, 0))
            finally:
                shutil.rmtree(folder, ignore_errors=True)
            done += 1
    progress(1.0, "evaluation done")
    passed = sum(r.passed for r in s_results)
    return {
        "provider": getattr(provider, "label", "") or f"{getattr(provider, 'kind', '?')} {getattr(provider, 'model', '')}",
        "kind": getattr(provider, "kind", ""), "model": getattr(provider, "model", ""), "language": lang,
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed": round(time.monotonic() - t0, 1),
        "questions": {"passed": sum(r.passed for r in q_results), "of": len(q_results),
                      "results": [asdict(r) for r in q_results]},
        "scripts": {"passed": passed, "of": len(s_results), "first_try": sum(r.passed and r.attempts == 1
                                                                                for r in s_results),
                    "results": [asdict(r) for r in s_results]},
        "actions": {"passed": sum(r.passed for r in a_results), "of": len(a_results),
                    "results": [asdict(r) for r in a_results]},
        "score_scripts": passed if scripts else None,
        "limited_for_scripts": (passed < LIMITED_UNDER) if scripts else None,
    }


def write_results(result: dict, out: Path) -> None:
    """``result`` as JSON in ``out`` (its folder made)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def record_score(settings, kind: str, result: dict) -> float:
    """Keeps ``score_scripts`` of ``result`` as the last evaluation of ``kind`` (the settings of the assistant show
    it); returns it. Raises ValueError for the reference answers or a result without scripts."""
    if kind == REFERENCE or kind not in ("local", "claude", "openai"):
        raise ValueError(f"only a model is recorded, not {kind!r}")
    score = result.get("score_scripts")
    if score is None:
        raise ValueError("the result has no script task: nothing to record")
    from ..gui.assistant_settings import SETTINGS

    settings.setValue(SETTINGS["score"].format(kind=kind), float(score))
    settings.sync()
    return float(score)


def table(result: dict) -> str:
    """The result as lines of text: one line per case and the totals."""
    lines = [f"{result['provider']} ({result['language']})"]
    for part in ("questions", "scripts", "actions"):
        for r in (result.get(part) or {}).get("results", []):
            mark = "ok  " if r["passed"] else "FAIL"
            lines.append(f"  {mark} {r['kind']:8} {r['id']:22} {r['attempts']} {r['elapsed']:7.1f} s  "
                         f"{_short(r['detail'], 160)}")
    q, s = result["questions"], result["scripts"]
    a = result.get("actions") or {"passed": 0, "of": 0}
    lines.append(f"questions {q['passed']}/{q['of']}, scripts {s['passed']}/{s['of']} "
                 f"(first try {s['first_try']}), actions {a['passed']}/{a['of']}, {result['elapsed']} s")
    if result["limited_for_scripts"]:
        lines.append(f"limited for scripts (score {result['score_scripts']} < {LIMITED_UNDER})")
    return "\n".join(lines)


__all__ = ["CASES", "REFERENCE", "CaseResult", "CasesProvider", "load_cases", "record_score", "run_eval", "table",
           "write_results"]
