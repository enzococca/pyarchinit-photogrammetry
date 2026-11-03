"""The session of the assistant: retrieval from the guide, tool calls over the project, the privacy filter both
ways, scripts extracted. A FakeProvider answers with scripted replies and records the messages it received."""
from __future__ import annotations

import json
import threading

import pytest

from pyarchinit_photogrammetry.assistant import facts as F
from pyarchinit_photogrammetry.assistant import privacy, session as S
from pyarchinit_photogrammetry.assistant.knowledge import default_index
from pyarchinit_photogrammetry.assistant.privacy import Filter
from pyarchinit_photogrammetry.assistant.providers import Message, Reply, ToolCall
from pyarchinit_photogrammetry.core.document import Document

DEM = {"resolution": 0.07, "width": 100, "height": 80, "coverage_percent": 98.0, "nodata_percent": 2.0,
       "version": None, "crs": "EPSG:32637", "source": "dense cloud",
       "height_above_lowest": {"min": 0, "max": 3.2, "median": 1.1, "p05": 0.2, "p95": 2.9}}


class FakeProvider:
    """Scripted replies, in order; ``calls`` keeps (messages, tools) of every ``complete``."""

    def __init__(self, replies, kind="claude", stream=True):
        self.kind, self.model, self.label = kind, "fake", f"Fake · {kind}"
        self.replies = list(replies)
        self.calls: list[tuple[list[Message], list | None]] = []
        self.stream = stream

    def complete(self, messages, tools=None, on_text=None, cancel=None):
        self.calls.append(([Message(m.role, m.content, list(m.tool_calls), m.tool_call_id, m.error)
                            for m in messages], tools))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if callable(reply):
            reply = reply(messages)
        if on_text is not None and self.stream and reply.text:
            for piece in (reply.text[:3], reply.text[3:]):
                if piece:
                    on_text(piece)
        return reply

    def list_models(self):
        return ["fake"]

    def check(self):
        return "ok"


def text(t, stop="end"):
    return Reply(t, [], stop, {})


def calls(*names_and_args, t=""):
    return Reply(t, [ToolCall(f"call_{i}", n, a) for i, (n, a) in enumerate(names_and_args)], "tool_calls", {})


def tool_messages(messages):
    return [m for m in messages if m.role == "tool"]


@pytest.fixture
def doc(tmp_path):
    d = Document(tmp_path / "Scavi" / "Rilievo Nord.ppg")
    d.meta["sito"] = "Tell Sultan"
    d.add_chunk("Volo 28/08")
    return d


@pytest.fixture
def fake_tools(monkeypatch):
    """run_tool replaced: dem_statistics gives DEM, project_summary names the site, the rest as facts does."""
    seen = []

    def run_tool(name, arguments, document, index, act=None):
        seen.append((name, dict(arguments or {}), document))
        if name == "dem_statistics" and document is not None:
            return dict(DEM)
        if name == "project_summary" and document is not None:
            return {"project": "Rilievo Nord", "site": "Tell Sultan",
                    "chunks": [{"label": "Volo 28/08", "key": 0, "photos": 0}]}
        return F.run_tool(name, arguments, document, index, act=act)

    monkeypatch.setattr(S, "run_tool", run_tool)
    return seen


def make(doc, replies, kind="claude", language="it", **kw):
    fake = FakeProvider(replies, kind=kind)
    return S.Assistant(fake, lambda: doc, default_index(language), language=language, **kw), fake


def test_question_with_tools_and_answer(doc, fake_tools):
    assistant, fake = make(doc, [calls(("dem_statistics", {})), text("Il DEM ha 7 cm.\n\nHo letto: dem_statistics")])
    pieces = []
    turn = assistant.ask("Che risoluzione ha il DEM?", on_text=pieces.append)
    assert turn.role == "assistant"
    assert turn.text.startswith("Il DEM ha 7 cm.")
    assert turn.error is None and turn.script is None
    assert "".join(pieces) == "Il DEM ha 7 cm.\n\nHo letto: dem_statistics"
    # two calls: the one that asked for the tool, the one that answered with its result
    assert len(fake.calls) == 2
    first, tools = fake.calls[0]
    assert first[0].role == "system" and [t.name for t in tools] == F.tool_names()
    assert first[-1].role == "user" and "Che risoluzione ha il DEM?" in first[-1].content
    assert "Dalla guida:" in first[-1].content
    second, _ = fake.calls[1]
    assert second[-2].role == "assistant" and second[-2].tool_calls[0].name == "dem_statistics"
    (result,) = tool_messages(second)
    assert result.tool_call_id == "call_0" and result.error is False
    assert json.loads(result.content)["coverage_percent"] == 98.0
    # the tool ran over the open project; the turn says what was read
    assert fake_tools[0][0] == "dem_statistics" and fake_tools[0][2] is doc
    assert "strumento: dem_statistics (DEM 7 cm, copertura 98 %)" in turn.read
    assert turn.read[0].startswith("guida: ")
    assert [t.role for t in assistant.history] == ["user", "assistant"]
    assert assistant.history[0].text == "Che risoluzione ha il DEM?"
    # the tool result left as the text of the tool message
    assert result.content in turn.sent
    assert first[-1].content in turn.sent


def test_system_prompt_names_the_language_the_tools_and_the_console(doc):
    assistant, _ = make(doc, [text("ok")])
    prompt = assistant.system_prompt()
    assert "italiano" in prompt and "Ho letto:" in prompt and "```python" in prompt
    for name in ("doc", "chunk", "app", "ppg", "np", "window", "polygon", "cameras", "progress"):
        assert f"`{name}`" in prompt
    for name in F.tool_names():
        assert name in prompt
    assert "Script" in prompt and "console Python" in prompt  # the overview of scripting.it.md
    assert "Sito A" in prompt  # external: the placeholders are explained
    local, _ = make(doc, [text("ok")], kind="local", language="en")
    prompt = local.system_prompt()
    assert "English" in prompt and "I read:" in prompt and "Sito A" not in prompt
    assert "Python console" in prompt


def test_external_filter_in_both_directions(doc, fake_tools):
    assistant, fake = make(doc, [calls(("project_summary", {})),
                                 text("Sito A è il sito del chunk 1.\n\nHo letto: project_summary")])
    assert assistant.external
    turn = assistant.ask("Come si chiama il sito Tell Sultan del chunk Volo 28/08?")
    user = fake.calls[0][0][-1].content
    # the passages after "Dalla guida:" are public text (the tutorial names its tell): the question is filtered
    question, context = user.split("\n\nDalla guida:")
    assert "Tell Sultan" not in question and "Sito A" in question and "chunk 1" in question
    assert "Volo 28/08" not in question
    assert context.strip()
    (result,) = tool_messages(fake.calls[1][0])
    assert "Tell Sultan" not in result.content and "Sito A" in result.content
    assert "Volo 28/08" not in result.content and "chunk 1" in result.content
    assert turn.text.startswith("Tell Sultan è il sito del Volo 28/08.")  # "chunk 1" is the label alone
    assert assistant.sent_log == turn.sent
    for entry in assistant.sent_log:
        assert "Tell Sultan" not in entry.split("\n\nDalla guida:")[0] and "Volo 28/08" not in entry
        assert not entry.startswith("[non inviato")
    assert assistant.filter.mapping["Tell Sultan"] == "Sito A"


def test_local_kind_sends_everything_and_logs_nothing_as_sent(doc, fake_tools):
    assistant, fake = make(doc, [calls(("project_summary", {})), text("Tell Sultan.")], kind="local")
    assert not assistant.external
    turn = assistant.ask("Come si chiama il sito Tell Sultan?")
    assert "Tell Sultan" in fake.calls[0][0][-1].content
    (result,) = tool_messages(fake.calls[1][0])
    assert "Tell Sultan" in result.content
    assert turn.text == "Tell Sultan."
    assert assistant.sent_log and all(e.startswith("[non inviato: modello locale] ") for e in assistant.sent_log)
    assert privacy.WARNING not in "".join(assistant.sent_log)
    assert assistant.filter.mapping == {}


def test_sent_log_warns_that_typed_coordinates_are_not_recognised(doc):
    assistant, fake = make(doc, [text("Capito."), text("Ancora.")])
    first = assistant.ask("Il punto 4556123 N 317456 E è dentro lo scavo?")
    assert first.sent[0] == privacy.WARNING
    assert assistant.sent_log[0] == privacy.WARNING
    second = assistant.ask("E questo?")
    assert privacy.WARNING not in second.sent  # once per conversation
    assert assistant.sent_log.count(privacy.WARNING) == 1
    # the coordinates typed in the question left as they were: that is what the warning says
    assert "4556123" in fake.calls[0][0][-1].content


def test_unknown_tool_and_bad_arguments_are_reported_to_the_model(doc, fake_tools):
    assistant, fake = make(doc, [calls(("no_such_tool", {}), ("dem_statistics", {"_invalid_json": "{oops"})),
                                 text("Fatto.")])
    turn = assistant.ask("Prova")
    results = tool_messages(fake.calls[1][0])
    assert len(results) == 2
    assert all(r.error for r in results)
    assert "unknown tool" in json.loads(results[0].content)["error"]
    assert json.loads(results[1].content)["error"] == "arguments were not valid JSON: {oops"
    assert [n for n, _, _ in fake_tools] == ["no_such_tool"]  # the broken call never ran
    assert turn.text == "Fatto." and turn.error is None
    assert "strumento: no_such_tool (errore)" in turn.read
    assert "strumento: dem_statistics (errore)" in turn.read
    # the broken call goes back to the model as its own call with empty arguments, never as {"_invalid_json": ...}
    resent = [m for m in fake.calls[1][0] if m.role == "assistant"][-1]
    assert [c.arguments for c in resent.tool_calls] == [{}, {}]
    assert "_invalid_json" not in json.dumps([m.content for m in fake.calls[1][0]])


def test_tool_rounds_are_bounded(doc, fake_tools):
    always = calls(("dem_statistics", {}), t="Controllo...")

    def reply(messages):
        if messages[-1].role == "user" and messages[-1].content == S.ANSWER_NOW["it"]:
            return text("Il DEM ha 7 cm.")
        return always

    assistant, fake = make(doc, [reply], max_tool_rounds=3)
    pieces = []
    turn = assistant.ask("Che risoluzione ha il DEM?", on_text=pieces.append)
    assert len(fake.calls) == 4 == assistant.max_tool_rounds + 1
    assert fake.calls[-1][1] is None  # the last call without tools
    last = fake.calls[-1][0]
    assert last[-1].role == "user" and last[-1].content == "Rispondi ora con quello che sai."
    assert turn.text == "Il DEM ha 7 cm."
    assert S.ANSWER_NOW["it"] in turn.sent
    # every round streamed live, separated by a line
    assert "".join(pieces) == "Controllo...\n" * 3 + "Il DEM ha 7 cm."
    assert len([n for n, _, _ in fake_tools]) == 1  # the same call again is answered from its result, not run


def test_a_model_that_ignores_the_last_call_still_answers(doc, fake_tools):
    assistant, fake = make(doc, [calls(("dem_statistics", {}), t="Controllo...")], max_tool_rounds=1)
    turn = assistant.ask("Ancora?")
    assert len(fake.calls) == 2
    assert turn.text == "Controllo..."  # the text of the last round, tool calls ignored
    assert turn.error is None


def test_refusal_and_length(doc):
    assistant, _ = make(doc, [text("", stop="refusal")])
    turn = assistant.ask("Domanda")
    assert turn.error == "Il modello ha rifiutato la risposta"
    assert turn.text == ""
    assistant, _ = make(doc, [text("Una risposta lunga", stop="length")], language="en")
    turn = assistant.ask("Question")
    assert turn.error is None
    assert turn.text.startswith("Una risposta lunga")
    assert S.LENGTH_NOTE["en"] in turn.text
    assistant, _ = make(doc, [text("Una risposta lunga", stop="length")])
    assert S.LENGTH_NOTE["it"] in assistant.ask("Domanda").text


def test_script_is_extracted_from_the_last_python_block(doc):
    answer = ("Prima:\n```python\nprint(1)\n```\nPoi:\n```\nnon python\n```\n"
              "```python\nfor c in doc.chunks:\n    print(c.label)\n```\n\nHo letto: niente")
    assistant, _ = make(doc, [text(answer)])
    turn = assistant.ask("Uno script che stampa i chunk")
    assert turn.script == "for c in doc.chunks:\n    print(c.label)"
    assert S.extract_script("niente") is None
    assert S.extract_script("```python\n```") is None
    assert S.extract_script("```python\nx = 1\n```") == "x = 1"
    assert S.extract_script("```\nx = 1\n```") is None
    assert S.extract_script("```pythonic\nx = 1\n```") is None
    # a block indented under a list item runs as it is; CRLF, ```Python, ```py, ~~~python and an info string are fences
    assert S.extract_script("1. Poi:\n   ```python\n   for c in doc.chunks:\n       print(c)\n   ```\n") == \
        "for c in doc.chunks:\n    print(c)"
    assert S.extract_script("```python\r\nx = 1\r\n```\r\n") == "x = 1"
    assert S.extract_script("```Python\nx = 1\n```") == "x = 1"
    assert S.extract_script("```py\nx = 1\n```") == "x = 1"
    assert S.extract_script("~~~python\nx = 1\n~~~") == "x = 1"
    assert S.extract_script("```python title=\"a.py\"\nx = 1\n```") == "x = 1"
    assert S.extract_script("~~~python\nx = 1\n```") is None  # closed by another fence


def test_question_without_a_document(fake_tools):
    assistant, fake = make(None, [calls(("dem_statistics", {})), text("Apri un progetto.")])
    turn = assistant.ask("Come si fanno le curve di livello?")
    assert turn.text == "Apri un progetto." and turn.error is None
    (result,) = tool_messages(fake.calls[1][0])
    assert result.error is False and "none" in json.loads(result.content)
    assert "Dalla guida:" in fake.calls[0][0][-1].content
    assert turn.read and all(r.startswith("guida: ") and r.endswith(".md") for r in turn.read[:-1])
    assert turn.read[-1] == "strumento: dem_statistics (nessun progetto aperto)"
    assert assistant.filter.active and assistant.filter.mapping == {}
    local, _ = make(None, [text("ok")], kind="local")
    assert not local.filter.active
    assert local.ask("ciao").text == "ok"


def test_ask_about_error_sends_script_and_traceback(doc):
    assistant, fake = make(doc, [text("Manca il DEM.")])
    script = "chunk.build_dem()\nprint(chunk.dem_path)"
    error = "Traceback (most recent call last):\n  File \"<script>\", line 1\nRuntimeError: nessuna nuvola"
    turn = assistant.ask_about_error(script, error)
    user = fake.calls[0][0][-1].content
    assert script in user and "RuntimeError: nessuna nuvola" in user
    assert "errore" in user.lower()
    assert turn.text == "Manca il DEM."
    assert assistant.history[0].text.startswith(S.ERROR_QUESTION["it"].split("{")[0])


def test_new_conversation_clears_history_but_not_the_mapping(doc):
    assistant, fake = make(doc, [text("Sito A."), text("Ancora Sito A.")])
    assistant.ask("Dimmi del sito Tell Sultan")
    mapping = dict(assistant.filter.mapping)
    assert mapping["Tell Sultan"] == "Sito A"
    assistant.new_conversation()
    assert assistant.history == [] and assistant.sent_log == []
    assert assistant.filter.mapping == mapping
    turn = assistant.ask("Di nuovo")
    assert turn.text == "Ancora Tell Sultan."
    assert len(fake.calls[1][0]) == 2  # system + the new question only
    assert fake.calls[1][0][-1].content.startswith("Di nuovo")


def test_history_goes_back_to_the_model_filtered(doc):
    assistant, fake = make(doc, [text("Sito A è a nord."), text("Sì.")])
    assistant.ask("Dove sta Tell Sultan?")
    assistant.ask("Sicuro?")
    messages = fake.calls[1][0]
    assert [m.role for m in messages] == ["system", "user", "assistant", "user"]
    assert "Tell Sultan" not in messages[1].content and "Sito A" in messages[1].content
    assert messages[2].content == "Sito A è a nord."
    assert "Dalla guida:" not in messages[2].content
    assert assistant.history[1].text == "Tell Sultan è a nord."


def test_one_filter_per_conversation_learns_the_names_added_since(doc, fake_tools):
    fake = FakeProvider([text("Sito A.")])
    assistant = S.Assistant(fake, lambda: doc, default_index("it"))
    assistant.ask("Ciao Tell Sultan")
    filter_ = assistant.filter
    assert filter_.mapping["Tell Sultan"] == "Sito A"
    doc.add_chunk("Saggio Sud")  # a name added since: the same filter, the placeholders kept
    turn = assistant.ask("E il chunk Saggio Sud?")
    assert assistant.filter is filter_ and filter_.mapping["Tell Sultan"] == "Sito A"
    assert "chunk 2" in fake.calls[-1][0][-1].content
    assert turn.note is None and not assistant.document_changed
    assert len(assistant.history) == 4


def _names_of_a(messages):
    return [m.content for m in messages if "Tell Sultan" in m.content.split("\n\nDalla guida:")[0]
            or "Volo 28/08" in m.content or "Rilievo Nord" in m.content]


def test_a_project_change_starts_a_new_conversation_and_nothing_of_the_old_one_leaves(doc, fake_tools):
    docs = {"doc": doc}
    fake = FakeProvider([text("Sito A è a nord."), text("Qui è Sito A."), text("Nessun progetto.")])
    assistant = S.Assistant(fake, lambda: docs["doc"], default_index("it"))
    first = assistant.ask("Dove sta Tell Sultan?")
    assert first.text == "Tell Sultan è a nord." and first.note is None
    other = Document()
    other.meta["sito"] = "Città di Castello"
    docs["doc"] = other  # another project opened: the conversation about the first one ends
    turn = assistant.ask("E qui?")
    assert turn.note == "Il progetto è cambiato: nuova conversazione"
    assert turn.text == "Qui è Città di Castello." and turn.error is None
    assert not assistant.document_changed
    assert [t.role for t in assistant.history] == ["user", "assistant"]  # the history of project A is gone
    assert assistant.history[0].text == "E qui?"
    messages = fake.calls[1][0]
    assert [m.role for m in messages] == ["system", "user"]
    assert _names_of_a(messages) == []
    assert "Tell Sultan" not in assistant.filter.mapping and assistant.filter.mapping["Città di Castello"] == "Sito A"
    assert turn.sent[0] == privacy.WARNING  # the warning again: a new conversation
    assert "Tell Sultan" not in "".join(assistant.sent_log).split("\n\nDalla guida:")[0]
    docs["doc"] = None  # the project closed: again a new conversation, about no project
    turn = assistant.ask("E senza progetto?")
    assert turn.note == "Il progetto è cambiato: nuova conversazione"
    assert [m.role for m in fake.calls[2][0]] == ["system", "user"]
    assert "Città di Castello" not in json.dumps([m.content for m in fake.calls[2][0]])
    assert assistant.filter.active and assistant.filter.mapping == {}
    assert len(assistant.history) == 2
    assert assistant.ask("Ancora?").note is None  # still no project: the same conversation
    assert len(assistant.history) == 4


def test_a_project_opened_after_questions_without_one_starts_a_new_conversation(fake_tools, doc):
    docs = {"doc": None}
    fake = FakeProvider([text("ok")])
    assistant = S.Assistant(fake, lambda: docs["doc"], default_index("it"), language="en")
    assistant.ask("How are contour lines made?")
    docs["doc"] = doc
    turn = assistant.ask("And here?")
    assert turn.note == "The project changed: new conversation"
    assert len(assistant.history) == 2 and assistant.filter.mapping["Tell Sultan"] == "Sito A"


def test_a_dead_document_counts_as_a_change_even_when_the_project_is_closed(fake_tools):
    import gc

    docs = {"doc": Document()}
    docs["doc"].meta["sito"] = "Tell Sultan"
    fake = FakeProvider([text("ok")])
    assistant = S.Assistant(fake, lambda: docs["doc"], default_index("it"))
    assistant.ask("Tell Sultan?")
    docs["doc"] = None
    gc.collect()
    turn = assistant.ask("E ora?")
    assert turn.note == "Il progetto è cambiato: nuova conversazione"
    assert [m.role for m in fake.calls[1][0]] == ["system", "user"]
    assert "Tell Sultan" not in fake.calls[1][0][-1].content.split("\n\nDalla guida:")[0]


def test_placeholders_in_tool_arguments_name_the_real_chunk(doc, fake_tools):
    assistant, _ = make(doc, [calls(("dem_statistics", {"chunk": "chunk 1"})), text("ok")])
    assistant.ask("Il DEM del chunk Volo 28/08?")
    assert fake_tools[0][1] == {"chunk": "Volo 28/08"}


def test_cancel_stops_between_rounds(doc, fake_tools):
    stop = threading.Event()

    def reply(messages):
        stop.set()
        return calls(("dem_statistics", {}), t="Controllo...")

    assistant, fake = make(doc, [reply])
    turn = assistant.ask("Che risoluzione?", cancel=stop.is_set)
    assert len(fake.calls) == 1
    assert turn.text == "Controllo..." and turn.error is None
    assert turn.cancelled is True
    assistant, _ = make(doc, [text("Intera.")])
    assert assistant.ask("Ciao").cancelled is False


def test_no_tool_rounds_means_one_message_with_the_answer_now_line(doc, fake_tools):
    assistant, fake = make(doc, [text("Solo quello che so.")], max_tool_rounds=0)
    turn = assistant.ask("Che risoluzione?")
    assert len(fake.calls) == 1 and fake.calls[0][1] is None
    messages = fake.calls[0][0]
    assert [m.role for m in messages] == ["system", "user"]
    assert messages[-1].content.startswith("Che risoluzione?")
    assert messages[-1].content.endswith("\n\nRispondi ora con quello che sai.")
    assert turn.sent == [privacy.WARNING, messages[-1].content]
    assert turn.text == "Solo quello che so."


def test_a_provider_error_is_the_turn_error(doc):
    from pyarchinit_photogrammetry.assistant.providers import ProviderError

    class Broken(FakeProvider):
        def complete(self, messages, tools=None, on_text=None, cancel=None):
            raise ProviderError("127.0.0.1:1234 non risponde")

    assistant = S.Assistant(Broken([]), lambda: doc, default_index("it"))
    turn = assistant.ask("Ciao")
    assert turn.error == "127.0.0.1:1234 non risponde" and turn.text == ""
    assert [t.role for t in assistant.history] == ["user", "assistant"]


def test_read_labels(doc):
    assert S.read_label("dem_statistics", DEM, "it") == "dem_statistics (DEM 7 cm, copertura 98 %)"
    assert S.read_label("dem_statistics", DEM, "en") == "dem_statistics (DEM 7 cm, coverage 98 %)"
    assert S.read_label("dem_statistics", {"none": "il chunk non ha un DEM"}, "it") == \
        "dem_statistics (il chunk non ha un DEM)"
    assert S.read_label("markers", {"error": "boom"}, "it") == "markers (errore)"
    assert S.read_label("markers", {"error": "boom"}, "en") == "markers (error)"
    assert S.read_label("orthomosaic_info", {"resolution": 0.0015, "width": 10, "height": 10}, "it") == \
        "orthomosaic_info (ortofoto 1,5 mm, 10 x 10 px)"
    assert S.read_label("guide_search", {"passages": [{"page": "gui/panes.md", "title": "Layer"}]}, "en") == \
        "guide_search (1 passage (gui/panes.md))"
    assert S.read_label("point_cloud_info", {"points": 1234567}, "it") == "point_cloud_info (1.234.567 punti)"
    assert S.read_label("alignment_report", {"aligned": 57, "photos": 57, "reprojection_error": 0.57}, "it") == \
        "alignment_report (57/57 foto allineate, 0,57 px)"
    assert S.read_label("model_info", {"faces": 2000}, "en") == "model_info (2,000 faces)"
    assert S.read_label("thermal_info", {"frames": 3}, "it") == "thermal_info"


def test_an_explicit_filter_is_kept(doc):
    filter_ = Filter(doc, people=["Mario Rossi"])
    assistant, fake = make(doc, [text("Persona 1 e Sito A.")], filter_=filter_)
    turn = assistant.ask("Mario Rossi scava a Tell Sultan")
    assert assistant.filter is filter_
    assert "Persona 1" in fake.calls[0][0][-1].content
    assert turn.text == "Mario Rossi e Tell Sultan."


# ---------------------------------------------------------------- actions


def test_action_tools_are_offered_only_with_act(doc):
    assistant, fake = make(doc, [text("ok")])
    assistant.ask("Ciao")
    _, tools = fake.calls[0]
    assert [t.name for t in tools] == F.tool_names()
    assert not any(t.name.startswith("act_") for t in tools)
    assert "act_stones" not in fake.calls[0][0][0].content  # the system prompt names only what is offered
    assert {"act_show", "act_select_photos", "act_stones", "act_build", "act_segment", "act_export",
            "act_run_script"} <= set(F.tool_names(acts=True))
    assistant, fake = make(doc, [text("ok")])
    assistant.ask("Ciao", act=lambda name, args: {"ran": True})
    system, tools = fake.calls[0][0][0].content, fake.calls[0][1]
    assert [t.name for t in tools] == F.tool_names(acts=True)
    assert "act_stones" in system and '"ran"' in system and '"declined"' in system and '"stopped"' in system
    stones = next(t for t in tools if t.name == "act_stones")
    assert stones.acts and stones.parameters["type"] == "object"
    assert stones.parameters["properties"]["min_size"]["description"]  # the English description of the schema
    json.dumps([t.parameters for t in tools])
    build = next(t for t in tools if t.name == "act_build")
    assert "resolution" in build.parameters["properties"]["params"]["description"]


def test_an_action_tool_without_the_window_says_so(doc):
    assert F.run_tool("act_show", {"what": "dem"}, doc, None) == {"error": "actions need the window"}
    seen = []
    out = F.run_tool("act_show", {"what": "dem"}, doc, None, act=lambda n, a: seen.append((n, a)) or {"ran": True})
    assert out == {"ran": True} and seen == [("show", {"what": "dem"})]
    assert F.run_tool("act_show", {}, doc, None, act=lambda n, a: 1 / 0)["error"].startswith("ZeroDivisionError")


def test_an_action_gets_the_real_names_and_its_result_goes_back_filtered(doc):
    seen = []

    def act(name, args):
        seen.append((name, args))
        return {"ran": True, "shown": "orthomosaic", "chunk": "Volo 28/08"}

    assistant, fake = make(doc, [calls(("act_show", {"what": "ortho", "chunk_label": "chunk 1",
                                                     "elevation": ["chunk 1"]})),
                                 text("Ecco l'ortofoto di chunk 1.")])
    turn = assistant.ask("Mostra l'ortofoto del chunk Volo 28/08", act=act)
    assert seen == [("show", {"what": "ortho", "chunk_label": "Volo 28/08", "elevation": ["Volo 28/08"]})]
    (result,) = tool_messages(fake.calls[1][0])
    assert "Volo 28/08" not in result.content and json.loads(result.content)["ran"] is True
    assert turn.text == "Ecco l'ortofoto di Volo 28/08."
    assert "strumento: act_show (eseguita)" in turn.read


def test_a_declined_action_is_said_to_the_model(doc):
    assistant, fake = make(doc, [calls(("act_stones", {"min_size": 0.1})), text("Non le ho disegnate.")],
                           kind="local")
    turn = assistant.ask("Disegna le pietre", act=lambda name, args: {"declined": True})
    (result,) = tool_messages(fake.calls[1][0])
    assert json.loads(result.content) == {"declined": True}
    assert turn.text == "Non le ho disegnate." and "strumento: act_stones (rifiutata)" in turn.read


def test_a_stopped_action_ends_the_turn_saying_so(doc):
    asked = []
    assistant, fake = make(doc, [calls(("act_stones", {}), ("act_show", {"what": "dem"}), t="Cerco le pietre."),
                                 text("Fatto!")], language="en")
    turn = assistant.ask("Draw the stones", act=lambda name, args: asked.append(name) or {"stopped": True})
    assert len(fake.calls) == 1  # no other round: the model is not asked to go on
    assert asked == ["stones"]  # after a stop the next action of the same reply is not asked for
    assert turn.cancelled and turn.text.startswith("Cerco le pietre.")
    assert "act_stones" in turn.text and "stopped" in turn.text and "Fatto" not in turn.text
    assert "tool: act_stones (stopped)" in turn.read


def test_the_reason_of_a_stop_is_said(doc):
    assistant, fake = make(doc, [calls(("act_stones", {})), text("Fatto!")], kind="local")
    turn = assistant.ask("Disegna le pietre", act=lambda name, args: {"stopped": True, "reason": "project_changed"})
    assert turn.cancelled and turn.text.endswith(": progetto cambiato]") and len(fake.calls) == 1

    assistant, _ = make(doc, [calls(("act_stones", {})), text("Done!")], kind="local", language="en")
    turn = assistant.ask("Draw the stones", act=lambda name, args: {"stopped": True, "reason": "chunk_changed"})
    assert turn.text.endswith(": the active chunk changed]")  # the code said in the conversation's language
