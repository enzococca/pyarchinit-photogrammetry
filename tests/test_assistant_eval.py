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


class _OpenAI:
    """A local OpenAI-compatible server on 127.0.0.1: two models, and every chat answered with ``answer``."""

    def __init__(self, answer: str):
        self.answer = answer
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
                if not body.get("stream"):
                    self._json({"choices": [{"message": {"role": "assistant", "content": fake.answer},
                                             "finish_reason": "stop"}]})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in ({"choices": [{"delta": {"content": fake.answer}, "finish_reason": None}]},
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
