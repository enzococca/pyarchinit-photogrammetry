"""The session fails gracefully instead of overflowing the server: calls already made are answered from their result,
tool results are capped and the oldest omitted, and a server that says the context is exceeded gets one shorter
request, then a message without its address."""
from __future__ import annotations

import pytest

from pyarchinit_photogrammetry.assistant import session as S
from pyarchinit_photogrammetry.assistant.providers import (ContextOverflow, Message, ProviderError, Reply, ToolCall,
                                                           context_overflow)
from pyarchinit_photogrammetry.assistant.session import Assistant


class Fake:
    """A local provider: ``turns`` is called with (messages, tools) and gives the reply; every request is kept."""

    kind = "local"
    model = "fake"
    label = "Locale · fake"

    def __init__(self, turns):
        self.turns = turns
        self.requests: list[tuple[list[Message], object]] = []

    def complete(self, messages, tools=None, on_text=None, cancel=None):
        self.requests.append(([Message(m.role, m.content, list(m.tool_calls), m.tool_call_id, m.error)
                               for m in messages], tools))
        return self.turns(messages, tools)

    def list_models(self):
        return [self.model]

    def check(self):
        return "ok"


@pytest.fixture
def runs(monkeypatch):
    """The tools run for real are counted; each answers with a result of ``size`` characters."""
    made: list[tuple[str, dict]] = []
    size = {"n": 50}

    def run_tool(name, arguments, document, index):
        made.append((name, dict(arguments)))
        return {"name": name, "args": arguments, "data": "x" * size["n"]}

    monkeypatch.setattr(S, "run_tool", run_tool)
    return made, size


def _answer(text="Fatto."):
    return Reply(text, [], "end", {})


def test_a_call_repeated_30_times_ends_with_an_answer(runs):
    made, _ = runs

    def turns(messages, tools):
        if tools is None:
            return _answer("Il progetto ha 8 marker.")
        return Reply("", [ToolCall(f"c{i}", "markers", {}) for i in range(30)], "tool_calls", {})

    fake = Fake(turns)
    turn = Assistant(fake, lambda: None, None, max_tool_rounds=8).ask("Quali marker?")
    assert turn.error is None and turn.text == "Il progetto ha 8 marker."
    assert len(made) == 1 < 5  # one run, shared by the 30 calls of each round
    assert len(fake.requests) <= 4  # the first round, two rounds of repeats, the answer
    last, tools = fake.requests[-1]
    assert tools is None and last[-1].role == "user" and last[-1].content == "hai già i risultati: rispondi"
    assert "[non inviato: modello locale] [già chiamato: markers]" in turn.sent
    assert any(r.endswith("già chiamato") for r in turn.read)
    # every call of the model has its tool message, with the result of the one run
    first_round = fake.requests[1][0]
    tool_messages = [m for m in first_round if m.role == "tool"]
    full = [m.content for m in tool_messages if m.content != "(risultato omesso)"]
    assert len(tool_messages) == 30 and len(full) == S.KEEP_TOOL_RESULTS and len(set(full)) == 1


def test_a_new_call_after_a_repeat_keeps_the_loop_going(runs):
    made, _ = runs
    script = [[("markers", {})], [("markers", {})], [("dem_statistics", {})], [("dem_statistics", {})]]

    def turns(messages, tools):
        if tools is None or not script:
            return _answer()
        return Reply("", [ToolCall(f"c{len(script)}", n, a) for n, a in script.pop(0)], "tool_calls", {})

    turn = Assistant(Fake(turns), lambda: None, None).ask("Dimmi tutto")
    assert turn.text == "Fatto." and [n for n, _ in made] == ["markers", "dem_statistics"]


def test_tool_results_are_capped_and_the_old_ones_omitted(runs):
    made, size = runs
    size["n"] = 10_000
    calls = iter(range(20))

    def turns(messages, tools):
        i = next(calls)
        if tools is None or i >= 14:
            return _answer()
        return Reply("", [ToolCall(f"c{i}", "markers", {"chunk": f"c{i}"})], "tool_calls", {})

    fake = Fake(turns)
    turn = Assistant(fake, lambda: None, None, max_tool_rounds=20, context_chars=10**9).ask("Molto")
    assert turn.text == "Fatto." and len(made) == 14
    request = fake.requests[-1][0]
    results = [m.content for m in request if m.role == "tool"]
    assert len(results) == 14
    assert results[:2] == ["(risultato omesso)"] * 2  # a local model gets the last 12 in full
    assert all(len(r) == S.TOOL_RESULT_CHARS + len("… [troncato]") and r.endswith("… [troncato]")
               for r in results[2:])
    # the log keeps what was sent at the time: every result in full (capped)
    assert sum("… [troncato]" in e for e in turn.sent) == 14


def test_the_request_is_kept_under_the_context_budget(runs):
    _, size = runs
    size["n"] = 3000
    calls = iter(range(10))

    def turns(messages, tools):
        i = next(calls)
        if tools is None or i >= 5:
            return _answer()
        return Reply("", [ToolCall(f"c{i}", "markers", {"chunk": f"c{i}"})], "tool_calls", {})

    fake = Fake(turns)
    Assistant(fake, lambda: None, None, max_tool_rounds=10, context_chars=12_000).ask("Molto")
    for request, _ in fake.requests:
        full = [m for m in request if m.role == "tool" and m.content != "(risultato omesso)"]
        if full:  # the oldest go first; the newest result is always there in full
            assert request[-1].role == "tool" and request[-1] in full or request[-1].role != "tool"
        assert sum(len(m.content) for m in request) <= 12_000 or len(full) <= 1
    last = [m.content for m in fake.requests[-1][0] if m.role == "tool"]
    assert last[0] == "(risultato omesso)" and last[-1] != "(risultato omesso)"
    # a service has no default budget
    claude = Fake(lambda m, t: _answer())
    claude.kind = "claude"
    assert Assistant(claude, lambda: None, None).context_chars is None
    assert Assistant(Fake(lambda m, t: _answer()), lambda: None, None).context_chars == S.LOCAL_CONTEXT_CHARS


def _overflowing(times: int):
    """A model that calls three tools, then fails ``times`` times with a context error, then answers."""
    state = {"fails": times, "round": 0}

    def turns(messages, tools):
        state["round"] += 1
        if state["round"] == 1:
            return Reply("", [ToolCall(f"c{i}", "markers", {"chunk": f"c{i}"}) for i in range(4)], "tool_calls", {})
        if state["fails"]:
            state["fails"] -= 1
            raise ContextOverflow("127.0.0.1:8080 ha risposto 400: request (40000 tokens) exceeds the available "
                                  "context size (32768 tokens)", 32768)
        return _answer("Ecco.")

    return Fake(turns)


def test_a_context_error_is_retried_once_with_a_shorter_request(runs):
    fake = _overflowing(1)
    turn = Assistant(fake, lambda: None, None).ask("Quali marker?")
    assert turn.error is None and turn.text == "Ecco."
    assert len(fake.requests) == 3
    failed, retried = fake.requests[1][0], fake.requests[2][0]
    assert sum(len(m.content) for m in retried) < sum(len(m.content) for m in failed)
    assert [m.content for m in retried if m.role == "tool"][:2] == ["(risultato omesso)"] * 2


def test_a_second_context_error_ends_with_a_message_without_the_address(runs):
    fake = _overflowing(2)
    turn = Assistant(fake, lambda: None, None).ask("Quali marker?")
    assert turn.error == ("La conversazione supera il contesto del modello (32768 token): inizia una nuova "
                          "conversazione")
    assert "127.0.0.1" not in turn.error and len(fake.requests) == 3


def test_context_errors_of_the_servers_are_recognised():
    llama = ('{"error":{"code":400,"message":"request (210923 tokens) exceeds the available context size (32768 '
             'tokens), try increasing it","type":"exceed_context_size_error"}}')
    lmstudio = ('{"error":"Trying to keep the first 9000 tokens when context the overflows. However, the model is '
                'loaded with context length of 4096 tokens, which is not enough."}')
    openai = '{"error": {"code": "context_length_exceeded", "message": "maximum context length is 128000 tokens"}}'
    assert context_overflow(llama, "x").tokens == 32768
    assert context_overflow(lmstudio, "x").tokens == 4096
    assert context_overflow(openai, "x") is not None
    assert context_overflow('{"error": "model not found"}', "x") is None
    assert issubclass(ContextOverflow, ProviderError)
