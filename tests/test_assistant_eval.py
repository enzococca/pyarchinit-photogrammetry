"""The evaluation of the assistant: the runner's checks with a provider that answers from the cases themselves (no
model), the results file, the command line with a fake OpenAI-compatible server on loopback, and the score kept in
the settings."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from pyarchinit_photogrammetry.assistant import evaluate
from pyarchinit_photogrammetry.assistant.evaluate import (CASES, CasesProvider, load_cases, run_eval,
                                                          write_results)
from pyarchinit_photogrammetry.assistant.providers import Reply

SMALL = {
    "questions": [
        {"id": "dem", "question_it": "Che risoluzione ha il DEM?", "question_en": "What is the resolution of the DEM?",
         "must_contain": [r"\b5\s*cm\b", r"risoluzion|resolution"], "tools_expected": ["dem_statistics"],
         "example_it": "Il DEM ha una risoluzione di 5 cm.\n\nHo letto: niente",
         "example_en": "The DEM has a resolution of 5 cm.\n\nI read: nothing"}],
    "scripts": [
        {"id": "rename", "task_it": "Rinomina il chunk attivo in Nuovo.", "task_en": "Rename the active chunk Nuovo.",
         "check": "chunk.label == 'Nuovo'", "example": "chunk.label = 'Nuovo'"},
        {"id": "add", "task_it": "Aggiungi un chunk chiamato B.", "task_en": "Add a chunk called B.",
         "check": "len(doc.chunks) == 2 and doc.chunks[1].label == 'B'", "example": "doc.add_chunk('B')"}],
}


@pytest.fixture
def small(tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    doc.add_chunk("Saggio 1")
    project = doc.save(tmp_path / "scavo.ppg")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(SMALL), encoding="utf-8")
    return project, cases


class _Wrong(CasesProvider):
    """Answers the question with nothing useful."""

    def complete(self, messages, tools=None, on_text=None, cancel=None):
        if messages[-1].content.startswith("Che risoluzione"):
            return Reply("Non lo so.", [], "end", {})
        return super().complete(messages, tools, on_text, cancel)


class _Flaky(CasesProvider):
    """The first script of the task "rename" fails; asked about the error, the fixed script of the cases."""

    def complete(self, messages, tools=None, on_text=None, cancel=None):
        if messages[-1].content.startswith("Rinomina"):
            return Reply("```python\nraise RuntimeError('boom')\n```", [], "end", {})
        return super().complete(messages, tools, on_text, cancel)


def test_eval_runner_checks_answers_and_scripts(small):
    project, cases = small
    result = run_eval(CasesProvider(cases), project, cases)
    assert result["questions"]["passed"] == 1 and result["questions"]["of"] == 1
    assert result["scripts"]["passed"] == 2 and result["scripts"]["of"] == 2
    assert all(r["attempts"] == 1 for r in result["scripts"]["results"])
    assert result["score_scripts"] == 2 and result["limited_for_scripts"] is False
    first = result["questions"]["results"][0]
    assert first["kind"] == "question" and first["sent_chars"] > 0 and first["elapsed"] >= 0
    # the scripts ran on copies: the project itself is untouched
    import pyarchinit_photogrammetry as ppg

    again = ppg.Document.open(project)
    assert [c.label for c in again.chunks] == ["Saggio 1"]

    wrong = run_eval(_Wrong(cases), project, cases, kinds="questions")
    assert wrong["questions"]["passed"] == 0 and wrong["scripts"]["of"] == 0 and wrong["score_scripts"] is None
    detail = wrong["questions"]["results"][0]["detail"]
    assert "5" in detail and "cm" in detail  # the pattern that did not match is named

    flaky = run_eval(_Flaky(cases), project, cases, kinds="scripts")
    rename = next(r for r in flaky["scripts"]["results"] if r["id"] == "rename")
    assert rename["passed"] and rename["attempts"] == 2
    assert flaky["scripts"]["first_try"] == 1 and flaky["score_scripts"] == 2


def test_results_file_and_limited_flag(small, tmp_path):
    project, cases = small

    class _NoScripts(CasesProvider):
        def complete(self, messages, tools=None, on_text=None, cancel=None):
            return Reply("Non so scrivere script.", [], "end", {})

    result = run_eval(_NoScripts(cases), project, cases, kinds="scripts")
    assert result["score_scripts"] == 0 and result["limited_for_scripts"] is True
    assert all(r["attempts"] == 1 and "no script" in r["detail"] for r in result["scripts"]["results"])
    out = tmp_path / "deep" / "results.json"
    write_results(result, out)
    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(json.dumps(result))


def test_the_evaluation_set_is_complete_and_its_examples_pass_their_own_checks():
    data = load_cases(CASES)
    assert len(data["questions"]) == 8 and len(data["scripts"]) == 6
    ids = [c["id"] for c in data["questions"] + data["scripts"]]
    assert len(ids) == len(set(ids))
    for case in data["questions"]:
        for lang in ("it", "en"):
            assert case[f"question_{lang}"].strip()
            for pattern in case["must_contain"]:
                assert re.search(pattern, case[f"example_{lang}"], re.IGNORECASE), (case["id"], lang, pattern)
    for case in data["scripts"]:
        assert case["task_it"].strip() and case["task_en"].strip()
        compile(case["check"], case["id"], "eval")
        compile(case["example"], case["id"], "exec")
    from pyarchinit_photogrammetry.assistant import facts

    actions = {c["id"]: c for c in data["actions"]}
    assert len(actions) >= 6 and "ambiguous_orthophoto" in actions
    assert not set(actions) & set(ids)
    for case in actions.values():
        assert case["kind"] == "action"
        for lang in ("it", "en"):
            assert case[f"question_{lang}"].strip()
            for pattern in case.get("must_contain", []):
                assert re.search(pattern, case[f"example_{lang}"], re.IGNORECASE), (case["id"], lang, pattern)
        if case.get("tool"):
            assert case["tool"] in facts.tool_names(acts=True)
            assert evaluate._contains(case["example_args"], case["args_contain"]), case["id"]
    ambiguous = actions["ambiguous_orthophoto"]
    assert ambiguous.get("tool") is None and ambiguous["setup"] == "two_orthophotos"
    assert any("quale" in p and "which" in p for p in ambiguous["must_contain"])


class _OpenAI:
    """A local OpenAI-compatible server on 127.0.0.1: two models, and every chat answered with ``answer``."""

    def __init__(self, answer):
        self.answers = list(answer) if isinstance(answer, (list, tuple)) else [answer]
        self.posts: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, data):
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._json({"data": [{"id": "qwen-test"}, {"id": "altro"}]})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                fake.posts.append(body)
                answer = fake.answers[min(len(fake.posts), len(fake.answers)) - 1]  # in order, the last repeated
                if isinstance(answer, tuple):  # (status, body): an HTTP error
                    raw = answer[1].encode()
                    self.send_response(answer[0])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if not body.get("stream"):
                    self._json({"choices": [{"message": {"role": "assistant", "content": answer},
                                             "finish_reason": "stop"}]})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in ({"choices": [{"delta": {"content": answer}, "finish_reason": None}]},
                              {"choices": [{"delta": {}, "finish_reason": "stop"}]}):
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def openai_server():
    servers = []

    def make(answer):
        servers.append(_OpenAI(answer))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


def test_cli_assistant_models_and_ask(small, openai_server):
    from pyarchinit_photogrammetry.cli import app

    project, _ = small
    server = openai_server("Il DEM ha una risoluzione di 5 cm.")
    runner = CliRunner()
    r = runner.invoke(app, ["assistant", "models", "--kind", "local", "--base-url", server.url])
    assert r.exit_code == 0, r.output
    assert "qwen-test" in r.output and "altro" in r.output
    r = runner.invoke(app, ["assistant", "ask", str(project), "Che risoluzione ha il DEM?", "--kind", "local",
                            "--base-url", server.url, "--model", "qwen-test"])
    assert r.exit_code == 0, r.output
    assert "Il DEM ha una risoluzione di 5 cm." in r.output
    assert server.posts and server.posts[-1]["model"] == "qwen-test"
    r = runner.invoke(app, ["assistant", "sent"])
    assert r.exit_code == 0, r.output
    assert "Che risoluzione ha il DEM?" in r.output and "[non inviato: modello locale]" in r.output
    assert "## Il progetto adesso" in r.output  # the inventory of the project, from the document alone
    # a server that is not on this computer is refused
    r = runner.invoke(app, ["assistant", "models", "--kind", "local", "--base-url", "http://192.0.2.1:8080/v1"])
    assert r.exit_code != 0


def test_record_writes_the_score_setting(small, openai_server, tmp_path):
    from PySide6.QtCore import QSettings

    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.gui import app_settings
    from pyarchinit_photogrammetry.gui.assistant_settings import score_of

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    project, cases = small
    server = openai_server("Ecco:\n\n```python\nchunk.label = 'Nuovo'\n```\n\nHo letto: niente")
    out = tmp_path / "out" / "local.json"
    r = CliRunner().invoke(app, ["assistant", "eval", str(project), "--kind", "local", "--base-url", server.url,
                                 "--model", "qwen-test", "--cases", str(cases), "--out", str(out), "--record"])
    assert r.exit_code == 0, r.output
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["scripts"]["passed"] == 1 and saved["limited_for_scripts"] is True  # "add" fails: a rename instead
    assert score_of(app_settings(), "local") == 1.0
    assert "limitato" in r.output.lower() or "limited" in r.output.lower()
    # the reference answers of the cases are not a model: never recorded
    r = CliRunner().invoke(app, ["assistant", "eval", str(project), "--kind", "reference", "--cases", str(cases),
                                 "--record"])
    assert r.exit_code != 0
    assert evaluate.REFERENCE == "reference"


# ---------------------------------------------------------------- tool calls a local model writes as text

def _local_assistant(server, project, rounds=8):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.assistant.providers import OpenAICompatible
    from pyarchinit_photogrammetry.assistant.session import Assistant

    doc = ppg.Document.open(project)
    return Assistant(OpenAICompatible(server.url, "qwen-test", kind="local"), lambda: doc, None, language="it",
                     max_tool_rounds=rounds)


def test_a_tool_call_written_as_json_text_runs_the_tool(small, openai_server):
    project, _ = small
    server = openai_server(['Controllo.\n\n```json\n{"name": "markers", "arguments": {}}\n```',
                            "Il progetto non ha marker.\n\nHo letto: markers"])
    turn = _local_assistant(server, project).ask("Quali marker ci sono?")
    assert turn.error is None and turn.text == "Il progetto non ha marker.\n\nHo letto: markers"
    assert len(server.posts) == 2
    assert server.posts[0]["max_tokens"] > 0  # a local model that repeats itself is cut, never streams for ever
    second = server.posts[1]["messages"]
    call = next(m for m in second if m["role"] == "assistant" and m.get("tool_calls"))
    assert call["tool_calls"][0]["function"]["name"] == "markers" and "```json" not in (call.get("content") or "")
    tool = next(m for m in second if m["role"] == "tool")
    assert tool["tool_call_id"] == call["tool_calls"][0]["id"] and "error" not in json.loads(tool["content"])
    assert any(r.startswith("strumento: markers") and r.endswith("chiamata letta dal testo") for r in turn.read)
    assert "[non inviato: modello locale] [chiamata letta dal testo: markers]" in turn.sent


def test_an_unknown_tool_written_as_text_is_reported_to_the_model(small, openai_server):
    project, _ = small
    server = openai_server(['{"name": "export_units", "arguments": {"units": "tutte"}}',
                            "Non posso esportare da qui."])
    turn = _local_assistant(server, project).ask("Esporta le unità")
    assert turn.text == "Non posso esportare da qui."
    tool = next(m for m in server.posts[1]["messages"] if m["role"] == "tool")
    assert "unknown tool export_units" in json.loads(tool["content"])["error"]


def test_plain_json_in_an_answer_stays_text(small, openai_server):
    from pyarchinit_photogrammetry.assistant.providers import text_tool_calls

    project, _ = small
    answer = 'Ecco i valori:\n\n```json\n{"risoluzione": 0.01, "unita": "m"}\n```'
    server = openai_server(answer)
    turn = _local_assistant(server, project).ask("Dammi la risoluzione in JSON")
    assert turn.text == answer and len(server.posts) == 1
    for text in ('{"name": "markers", "arguments": {}, "note": "x"}',  # another key: not a call
                 '{"name": "Mario Rossi", "arguments": {}}',  # not a tool name
                 'Usa {"name": "markers", "arguments": {}} nel testo.'):  # inside prose, no block of its own
        assert text_tool_calls(text) == ([], text)
    calls, _ = text_tool_calls('```python\n{"name": "markers", "arguments": {"chunk": None}}\n```')
    assert [(c.name, c.arguments) for c in calls] == [("markers", {"chunk": None})]  # a Python literal
    for text in ('```python\n{[1]: 2}\n```',  # a literal that cannot be built: TypeError, never raised
                 "```python\n{'name': 'markers', 'arguments': {'a': b'x'}}\n```",  # arguments that are not JSON
                 "```python\n{'name': 'markers', 'arguments': {(1, 2): 3}}\n```",
                 '```json\n{"function": {"name": "markers", "arguments": {}}, "note": "x"}\n```'):  # around a function
        assert text_tool_calls(text) == ([], text), text
    calls, _ = text_tool_calls('```json\n{"type": "function", "id": "a", "function": {"name": "markers", '
                               '"arguments": {}}}\n```', prefix="text_3")
    assert [(c.id, c.name) for c in calls] == [("text_3_0", "markers")]
    calls, rest = text_tool_calls('<tool_call>\n{"name": "dem_statistics", "arguments": "{\\"chunk\\": \\"Saggio 1\\"}"}\n</tool_call>')
    assert [(c.name, c.arguments, c.from_text) for c in calls] == [("dem_statistics", {"chunk": "Saggio 1"}, True)]
    assert rest == ""


def test_text_tool_calls_are_bounded_by_the_tool_rounds(small, openai_server):
    project, _ = small
    server = openai_server('```json\n{"name": "markers", "arguments": {}}\n```')  # it never stops calling
    turn = _local_assistant(server, project, rounds=2).ask("Quali marker?")
    assert len(server.posts) == 3 and "tools" not in server.posts[-1]  # the last round without tools
    assert turn.error is None


def test_ids_of_calls_read_from_the_text_are_unique_in_a_conversation(small, openai_server):
    project, _ = small
    server = openai_server(['```json\n{"name": "markers", "arguments": {}}\n```',
                            '```json\n{"name": "project_summary", "arguments": {}}\n```', "Fatto."])
    assert _local_assistant(server, project).ask("Tutto").text == "Fatto."
    ids = [c["id"] for m in server.posts[-1]["messages"] if m["role"] == "assistant" for c in m.get("tool_calls", [])]
    assert len(ids) == 2 and len(set(ids)) == 2


def test_a_script_that_raises_cancelled_itself_is_not_a_timeout(small):
    project, _ = small
    cases = {"questions": [], "scripts": [{"id": "own", "task_it": "Fermati.", "task_en": "Stop.", "check": "True",
                                           "example": "from pyarchinit_photogrammetry.core.progress import "
                                                      "Cancelled\nraise Cancelled('mio')"}]}
    result = run_eval(CasesProvider(cases), project, cases, kinds="scripts", attempts=1, script_timeout=30)
    own = result["scripts"]["results"][0]
    assert not own["passed"] and "Cancelled" in own["detail"] and "tempo scaduto" not in own["detail"]
    assert own["elapsed"] < 20


def test_a_slow_script_is_stopped_and_counted_as_failed(small):
    project, _ = small
    cases = {"questions": [], "scripts": [{"id": "slow", "task_it": "Aspetta.", "task_en": "Wait.", "check": "True",
                                           "example": "import time\nwhile True:\n    cancel()\n    time.sleep(0.02)"}]}
    result = run_eval(CasesProvider(cases), project, cases, kinds="scripts", script_timeout=0.3)
    slow = result["scripts"]["results"][0]
    assert not slow["passed"] and slow["attempts"] == 2 and "tempo scaduto" in slow["detail"]
    assert evaluate.SCRIPT_TIMEOUT == 120.0


def test_a_context_error_of_llama_cpp_is_retried_shorter_then_said_without_the_address(small, openai_server):
    project, _ = small
    overflow = (400, '{"error":{"code":400,"message":"request (40000 tokens) exceeds the available context size '
                     '(32768 tokens), try increasing it","type":"exceed_context_size_error"}}')
    call = '```json\n{"name": "project_summary", "arguments": {}}\n```'
    server = openai_server([call, overflow, "Ecco il riassunto."])
    turn = _local_assistant(server, project).ask("Riassumi il progetto")
    assert turn.text == "Ecco il riassunto." and len(server.posts) == 3
    assert len(json.dumps(server.posts[2]["messages"])) < len(json.dumps(server.posts[1]["messages"]))
    server = openai_server([call, overflow, overflow])
    turn = _local_assistant(server, project).ask("Riassumi il progetto")
    assert turn.error == ("La conversazione supera il contesto del modello (32768 token): inizia una nuova "
                          "conversazione")


def test_the_action_cases_pass_with_the_reference_answers_on_the_demo(processed):
    doc, _ch, _ = processed
    result = run_eval(CasesProvider(), doc.path, kinds="actions")
    actions = result["actions"]
    assert actions["of"] >= 6 and actions["passed"] == actions["of"], evaluate.table(result)
    assert result["questions"]["of"] == 0 and result["scripts"]["of"] == 0 and result["score_scripts"] is None
    ambiguous = next(r for r in actions["results"] if r["id"] == "ambiguous_orthophoto")
    assert ambiguous["kind"] == "action" and ambiguous["passed"]
    import pyarchinit_photogrammetry as ppg

    assert [c.label for c in ppg.Document.open(doc.path).chunks] == ["Saggio 1"]  # the setup ran on a copy

    class _Talks(CasesProvider):
        """Answers every action with words, never with the tool."""

        def complete(self, messages, tools=None, on_text=None, cancel=None):
            return Reply("Lo faccio subito. Quale? Area nord o Area sud.", [], "end", {})

    talks = run_eval(_Talks(), doc.path, kinds="actions")["actions"]
    by_id = {r["id"]: r for r in talks["results"]}
    assert not by_id["action_stones"]["passed"] and "act_stones" in by_id["action_stones"]["detail"]
    assert by_id["ambiguous_orthophoto"]["passed"]  # a question and no action: what the case wants

    class _Rushes(CasesProvider):
        """Draws the stones on the ambiguous question instead of asking."""

        def complete(self, messages, tools=None, on_text=None, cancel=None):
            if messages[-1].role == "user" and "ortofoto" in messages[-1].content and \
                    not any(m.role == "tool" for m in messages):
                from pyarchinit_photogrammetry.assistant.providers import ToolCall

                return Reply("", [ToolCall("c", "act_stones", {})], "tool_calls", {})
            return super().complete(messages, tools, on_text, cancel)

    rushes = run_eval(_Rushes(), doc.path, kinds="actions")["actions"]
    ambiguous = next(r for r in rushes["results"] if r["id"] == "ambiguous_orthophoto")
    assert not ambiguous["passed"] and "act_stones" in ambiguous["detail"]


def test_the_command_line_ask_says_it_does_not_act():
    from pyarchinit_photogrammetry.cli import app

    out = CliRunner().invoke(app, ["assistant", "ask", "--help"], terminal_width=200)
    assert out.exit_code == 0 and "not offered" in " ".join(out.output.split())
