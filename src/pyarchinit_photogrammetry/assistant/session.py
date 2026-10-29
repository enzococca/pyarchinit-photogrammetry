"""The session of the assistant: one conversation with a model about the open project.

:class:`Assistant` composes every question with the passages of the guide that answer it, lets the model call the
read-only fact tools of :mod:`facts` over the project, and keeps the history. When the provider is not local
(Claude, OpenAI) what leaves passes through the privacy :class:`Filter`: the question, the tool results and the
history go out with placeholders, the answer comes back with the real names. The passages of the guide are public
text and leave as they are.

One Filter per conversation: its placeholders stay stable from the first question to the last, and the history
is re-sent through it. When the project changes (another document opened, or the project closed) the conversation
ends: the history is cleared, a fresh Filter is built from the new project, and the first turn after the change
carries a ``note`` saying so.

Streaming: every round of the model is streamed live through ``on_text`` (the text of a round that ends in tool
calls - "let me check..." - and then the answer); a round that ended in tool calls is followed by one ``"\\n"``.
The deltas are restored best-effort (a placeholder split over two deltas stays as it is): ``Turn.text`` is the
authoritative answer.

The texts this module composes for the user (notes, marks, the question about an error) are engine texts in the
language of the conversation: they are not translated by the interface.
"""
from __future__ import annotations

import json
import logging
import re
import textwrap
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable

from .facts import TOOLS, run_tool
from .knowledge import Index, guide_folder
from .privacy import WARNING, Filter
from .providers import Message, Provider, ProviderError, Reply, ToolCall

log = logging.getLogger(__name__)

PASSAGES = 3  # guide passages retrieved for a question
OVERVIEW_CHARS = 1500  # of the scripting page in the system prompt
CONSOLE_NAMES = ("doc", "chunk", "app", "ppg", "np", "window", "polygon", "cameras", "progress")

REFUSED = {"it": "Il modello ha rifiutato la risposta", "en": "The model refused to answer"}
LENGTH_NOTE = {"it": "[risposta interrotta: il modello ha raggiunto il limite di lunghezza]",
               "en": "[answer cut short: the model reached its length limit]"}
NOT_SENT = {"it": "[non inviato: modello locale] ", "en": "[not sent: local model] "}
ANSWER_NOW = {"it": "Rispondi ora con quello che sai.", "en": "Answer now with what you know."}
FROM_GUIDE = {"it": "Dalla guida:", "en": "From the guide:"}
ERROR_QUESTION = {
    "it": "Questo script ha dato un errore. Spiega la causa e dai lo script corretto.\n\n"
          "Script:\n```python\n{script}\n```\n\nErrore:\n```\n{error}\n```",
    "en": "This script raised an error. Explain the cause and give the corrected script.\n\n"
          "Script:\n```python\n{script}\n```\n\nError:\n```\n{error}\n```"}
PROJECT_CHANGED = {"it": "Il progetto è cambiato: nuova conversazione", "en": "The project changed: new conversation"}
GUIDE_LABEL = {"it": "guida", "en": "guide"}
TOOL_LABEL = {"it": "strumento", "en": "tool"}
ERROR_LABEL = {"it": "errore", "en": "error"}

_INSTRUCTIONS = {
    "it": (
        "Sei l'assistente di PyArchInit Photogrammetry, un programma di fotogrammetria per l'archeologia. Rispondi in "
        "italiano, in modo breve e preciso.\n"
        "- Non inventare mai numeri sul progetto: usa gli strumenti e riferisci solo quello che restituiscono. Se uno "
        "strumento risponde con \"none\" o \"error\", dillo.\n"
        "- Per come si fa una cosa nel programma usa i passaggi della guida allegati alla domanda o lo strumento "
        "guide_search; per le funzioni Python usa scripting_api.\n"
        "- Quando ti viene chiesto uno script, dai UN solo blocco ```python che usa i nomi della console ({names}); "
        "il programma lo mostra all'utente e non esegue mai nulla da solo: non dire mai di averlo eseguito.\n"
        "- Alla fine della risposta, in una sola riga che inizia con \"Ho letto:\", elenca gli strumenti usati e le "
        "pagine della guida lette (o \"Ho letto: niente\")."),
    "en": (
        "You are the assistant of PyArchInit Photogrammetry, a photogrammetry program for archaeology. Answer in "
        "English, briefly and precisely.\n"
        "- Never invent numbers about the project: use the tools and report only what they return. When a tool "
        "answers with \"none\" or \"error\", say so.\n"
        "- For how a thing is done in the program use the guide passages attached to the question or the "
        "guide_search tool; for the Python functions use scripting_api.\n"
        "- When asked for a script, give ONE ```python block that uses the names of the console ({names}); the "
        "program shows it to the user and never runs anything by itself: never say you ran it.\n"
        "- At the end of the answer, in one line starting with \"I read:\", list the tools used and the guide pages "
        "read (or \"I read: nothing\")."),
}
_PLACEHOLDERS = {
    "it": ("- I nomi che vedi (Sito A, Luogo 1, Persona 1, chunk 1, progetto) sono segnaposto messi dal programma al "
           "posto dei nomi veri: tienili come sono nella risposta, il programma rimette quelli veri. Non chiedere i "
           "nomi veri né le coordinate."),
    "en": ("- The names you see (Sito A, Luogo 1, Persona 1, chunk 1, progetto) are placeholders the program put in "
           "place of the real names: keep them as they are in the answer, the program restores the real ones. Never "
           "ask for the real names nor for coordinates."),
}
_HEADINGS = {"it": ("## Script", "## Strumenti"), "en": ("## Scripts", "## Tools")}

# a fenced block ```python, ```py or ~~~python (any case, an info string allowed), closed by the same fence
_SCRIPT = re.compile(r"^[ \t]*(?P<fence>```|~~~)[ \t]*(?i:python|py)\b[^\n]*\n(?P<body>.*?)^[ \t]*(?P=fence)[ \t]*$",
                     re.MULTILINE | re.DOTALL)


@dataclass
class Turn:
    """One side of the conversation. ``sent``: the exact texts that left in this turn (the composed question, each
    tool result, the final "answer now" line), marked ``NOT_SENT`` with a local model; ``read``: what the answer
    was built on - "guida: <page>" for each guide page retrieved for the question (consulted, whether the model used
    it or not) and "strumento: <name> (<what it said>)" for each tool called; ``script``: the last ```python block
    of the answer; ``error``: why there is no answer (a refusal, the provider); ``note``: a word for the user
    (the project changed and a new conversation began); ``cancelled``: the answer was stopped by the user and
    ``text`` is what had arrived."""
    role: str
    text: str
    sent: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    script: str | None = None
    error: str | None = None
    note: str | None = None
    cancelled: bool = False


def extract_script(text: str) -> str | None:
    """The last ```python block of ``text`` (also ```py and ~~~python), dedented and stripped: a block indented
    under a list item runs as it is. None when there is none or it is empty."""
    found = [m.group("body") for m in _SCRIPT.finditer((text or "").replace("\r\n", "\n"))]
    if not found:
        return None
    script = textwrap.dedent(found[-1]).strip("\n").rstrip()
    return script or None


def _language(language: str) -> str:
    return "it" if (language or "").lower().startswith("it") else "en"


def _number(x: float, lang: str, digits: int = 1) -> str:
    s = f"{x:,.{digits}f}".rstrip("0").rstrip(".")
    if lang == "it":
        s = s.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return s


def _integer(n: int, lang: str) -> str:
    s = f"{int(n):,}"
    return s.replace(",", ".") if lang == "it" else s


def _length(metres: float, lang: str) -> str:
    if metres < 0.01:
        return f"{_number(metres * 1000, lang)} mm"
    if metres < 1:
        return f"{_number(metres * 100, lang)} cm"
    return f"{_number(metres, lang, 2)} m"


def read_label(name: str, result: Any, lang: str = "it") -> str:
    """What a tool said in a few words, after its name: "dem_statistics (DEM 7 cm, copertura 98 %)"; the bare name
    when there is nothing short to say."""
    if not isinstance(result, dict):
        return name
    if "error" in result:
        return f"{name} ({ERROR_LABEL[lang]})"
    if "none" in result:
        return f"{name} ({result['none']})"
    try:
        if name == "dem_statistics" and result.get("resolution"):
            cov = result.get("coverage_percent")
            word = "copertura" if lang == "it" else "coverage"
            detail = f"DEM {_length(float(result['resolution']), lang)}"
            if cov is not None:
                detail += f", {word} {_number(float(cov), lang)} %"
            return f"{name} ({detail})"
        if name == "orthomosaic_info" and result.get("resolution"):
            word = "ortofoto" if lang == "it" else "orthomosaic"
            detail = f"{word} {_length(float(result['resolution']), lang)}"
            if result.get("width") and result.get("height"):
                detail += f", {int(result['width'])} x {int(result['height'])} px"
            return f"{name} ({detail})"
        if name == "point_cloud_info" and result.get("points") is not None:
            word = "punti" if lang == "it" else "points"
            return f"{name} ({_integer(result['points'], lang)} {word})"
        if name == "model_info" and result.get("faces") is not None:
            word = "facce" if lang == "it" else "faces"
            return f"{name} ({_integer(result['faces'], lang)} {word})"
        if name == "alignment_report" and result.get("photos") is not None:
            word = "foto allineate" if lang == "it" else "photos aligned"
            detail = f"{int(result.get('aligned') or 0)}/{int(result['photos'])} {word}"
            if result.get("reprojection_error") is not None:
                detail += f", {_number(float(result['reprojection_error']), lang, 2)} px"
            return f"{name} ({detail})"
        if name == "markers" and isinstance(result.get("markers"), list):
            return f"{name} ({len(result['markers'])} marker)"
        if name == "project_summary" and isinstance(result.get("chunks"), list):
            n = len(result["chunks"])
            word = ("chunk" if lang == "it" else ("chunk" if n == 1 else "chunks"))
            return f"{name} ({n} {word})"
        if name in ("guide_search", "scripting_api") and isinstance(result.get("passages"), list):
            found = result["passages"]
            pages = sorted({p.get("page", "") for p in found if isinstance(p, dict) and p.get("page")})
            n = len(found)
            word = ("passaggio" if n == 1 else "passaggi") if lang == "it" else ("passage" if n == 1 else "passages")
            detail = f"{n} {word}"
            if pages:
                detail += f" ({', '.join(pages)})"
            return f"{name} ({detail})"
    except (TypeError, ValueError):  # a label never stops the answer
        log.debug("label of %s", name, exc_info=True)
    return name


class Assistant:
    """A conversation with ``provider`` about the project ``document()`` gives (None: no project open).

    One privacy filter per conversation: built from the document when the provider is external (a passthrough when
    it is local), and only taught the names that appeared since before every question. When ``document()`` gives
    another object than the one the conversation was about (or None once the project is closed) the conversation
    ends by itself - history cleared, warning re-armed, fresh filter - and the next answer carries the ``note``
    PROJECT_CHANGED; ``document_changed`` says it until that answer. A ``filter_`` given explicitly is kept.

    ``ask`` never raises for a provider failure: the turn carries the error. The warning that typed names and
    coordinates are not filtered is the first entry of ``sent`` of the first external turn of a conversation."""

    def __init__(self, provider: Provider, document: Callable[[], Any], index: Index | None,
                 language: str = "it", filter_: Filter | None = None, max_tool_rounds: int = 8):
        self.provider = provider
        self._document = document
        self.index = index
        self.language = _language(language)
        self.max_tool_rounds = max(0, int(max_tool_rounds))
        self.history: list[Turn] = []
        self._explicit = filter_ is not None
        self._doc_ref: Any = _NO_DOCUMENT  # a weakref to the document of the conversation, None for "no project"
        self._filter: Filter = filter_ if filter_ is not None else self._new_filter(None)
        self._warned = False
        self._overview: str | None = None
        self.document_changed = False

    # ------------------------------------------------------------------ state
    @property
    def external(self) -> bool:
        """True when the answers come from outside this computer (the filter is active)."""
        return getattr(self.provider, "kind", "local") != "local"

    @property
    def filter(self) -> Filter:
        return self._filter

    def _new_filter(self, document: Any) -> Filter:
        return Filter(document) if self.external else Filter.passthrough()

    def _document_now(self) -> Any:
        """The document of this question; a change of project ends the conversation (one filter per conversation)."""
        try:
            document = self._document()
        except Exception:  # a getter that fails = no project
            log.debug("document()", exc_info=True)
            document = None
        if self._doc_ref is _NO_DOCUMENT:  # the first question: the conversation is about this document
            changed = False
        elif self._doc_ref is None:  # the conversation had no project
            changed = document is not None
        else:
            known = self._doc_ref()  # None once the old document is gone: a change, whatever came after
            changed = known is None or known is not document
        if changed:
            self.new_conversation()
            if not self._explicit:
                self._filter = self._new_filter(document)
            self.document_changed = True
        self._filter.update(document)
        self._doc_ref = None if document is None else _ref(document)
        return document

    def new_conversation(self) -> None:
        """Forgets the history (the filter and its placeholders stay)."""
        self.history = []
        self._warned = False

    @property
    def sent_log(self) -> list[str]:
        """Every text that left, in the order it left, over the whole conversation."""
        return [entry for turn in self.history for entry in turn.sent]

    # ------------------------------------------------------------------ the prompt
    def _scripting_overview(self) -> str:
        if self._overview is None:
            name = "scripting.it.md" if self.language == "it" else "scripting.md"
            try:
                text = (guide_folder() / name).read_text(encoding="utf-8")
            except OSError:
                log.debug("scripting overview %s", name, exc_info=True)
                text = ""
            self._overview = text[:OVERVIEW_CHARS].rstrip()
        return self._overview

    def system_prompt(self) -> str:
        lang = self.language
        names = ", ".join(f"`{n}`" for n in CONSOLE_NAMES)
        parts = [_INSTRUCTIONS[lang].format(names=names)]
        if self.external:
            parts[0] += "\n" + _PLACEHOLDERS[lang]
        scripts, tools = _HEADINGS[lang]
        overview = self._scripting_overview()
        if overview:
            parts.append(f"{scripts}\n\n{overview}")
        parts.append(f"{tools}\n\n" + "\n".join(f"- {t.name}: {t.description}" for t in TOOLS))
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ asking
    def _context(self, question: str) -> tuple[str, list[str]]:
        """The guide passages for ``question`` as a block of text (public: never filtered) and their labels."""
        if self.index is None:
            return "", []
        try:
            found = self.index.search(question, PASSAGES)
        except Exception:
            log.debug("guide search", exc_info=True)
            return "", []
        if not found:
            return "", []
        lang = self.language
        blocks = [f"### {p.title} ({p.page})\n{p.text.strip()}" for p in found]
        labels = list(dict.fromkeys(f"{GUIDE_LABEL[lang]}: {p.page}" for p in found))
        return f"{FROM_GUIDE[lang]}\n\n" + "\n\n".join(blocks), labels

    def _messages(self) -> list[Message]:
        """The system prompt and the history as the model sees it (filtered when external)."""
        messages = [Message("system", self.system_prompt())]
        for turn in self.history:
            if turn.role == "user":
                messages.append(Message("user", self._filter.outgoing(turn.text)))
            elif turn.role == "assistant" and turn.text and turn.error is None:
                messages.append(Message("assistant", self._filter.outgoing(turn.text)))
        return messages

    def _mark(self, text: str) -> str:
        return text if self.external else NOT_SENT[self.language] + text

    def _run_call(self, call: ToolCall, document: Any) -> tuple[Message, str]:
        """The tool message of ``call`` and its label for ``read``."""
        lang = self.language
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        if "_invalid_json" in arguments:
            raw = str(arguments["_invalid_json"])[:200]
            result: dict = {"error": f"arguments were not valid JSON: {raw}"}
        else:
            # a placeholder in an argument (chunk "chunk 1") names the real chunk
            arguments = {k: self._filter.incoming(v) if isinstance(v, str) else v for k, v in arguments.items()}
            result = run_tool(call.name, arguments, document, self.index)
            if not isinstance(result, dict):
                result = {"error": f"the tool {call.name} returned {type(result).__name__}, not an object"}
        label = f"{TOOL_LABEL[lang]}: {read_label(call.name, result, lang)}"
        safe = self._filter.facts(result)
        content = json.dumps(safe, ensure_ascii=False, default=str)
        return Message("tool", content, tool_call_id=call.id, error="error" in result), label

    def ask(self, question: str, on_text: Callable[[str], None] | None = None,
            cancel: Callable[[], bool] | None = None) -> Turn:
        """Asks ``question``: the answer's turn, appended to the history after the question's.

        ``on_text`` receives the deltas of every round as they arrive (restored best-effort: a placeholder split
        across two deltas reaches it as the model wrote it) and ``"\\n"`` after a round that ended in tool calls:
        when the turn ends, replace what was shown with ``Turn.text``, which is the restored answer. ``cancel`` is
        read from a watcher thread too: give a thread-safe callable (an ``Event.is_set``)."""
        lang = self.language
        document = self._document_now()
        context, labels = self._context(question)
        outgoing = self._filter.outgoing(question)
        composed = f"{outgoing}\n\n{context}" if context else outgoing
        if self.max_tool_rounds == 0:  # no tool round: the question and the "answer now" line go together
            composed = f"{composed}\n\n{ANSWER_NOW[lang]}"
        turn = Turn("assistant", "", read=list(labels))
        if self.document_changed:
            turn.note = PROJECT_CHANGED[lang]
            self.document_changed = False
        if self.external and not self._warned:
            turn.sent.append(WARNING)
            self._warned = True
        turn.sent.append(self._mark(composed))
        self.history.append(Turn("user", question))
        self.history.append(turn)
        messages = self._messages()[:-1] + [Message("user", composed)]

        def stream(delta: str) -> None:
            if on_text is not None:
                on_text(self._filter.incoming(delta))

        rounds = 0
        earlier = ""  # the text of the rounds that ended in tool calls
        reply: Reply | None = None
        try:
            while True:
                with_tools = rounds < self.max_tool_rounds
                if not with_tools and rounds:
                    messages.append(Message("user", ANSWER_NOW[lang]))
                    turn.sent.append(self._mark(ANSWER_NOW[lang]))
                reply = self.provider.complete(messages, TOOLS if with_tools else None, on_text=stream,
                                              cancel=cancel)
                if not (reply.tool_calls and with_tools and reply.stop != "refusal") or _cancelled(cancel):
                    break
                messages.append(Message("assistant", reply.text, tool_calls=[_as_written(c) for c in reply.tool_calls]))
                for call in reply.tool_calls:
                    message, label = self._run_call(call, document)
                    messages.append(message)
                    turn.sent.append(self._mark(message.content))
                    turn.read.append(label)
                if reply.text:
                    earlier = (earlier + "\n" + reply.text) if earlier else reply.text
                    if on_text is not None:
                        on_text("\n")
                rounds += 1
        except ProviderError as exc:
            turn.error = str(exc)
            if reply is None:
                return turn
        if reply is None:
            return turn
        turn.cancelled = _cancelled(cancel)
        answer = reply.text or earlier
        if reply.stop == "refusal":
            turn.error = REFUSED[lang]
        elif reply.stop == "length":
            answer = f"{answer}\n\n{LENGTH_NOTE[lang]}" if answer else LENGTH_NOTE[lang]
        turn.text = self._filter.incoming(answer)
        turn.script = extract_script(turn.text)
        return turn

    def ask_about_error(self, script: str, error: str, on_text: Callable[[str], None] | None = None,
                        cancel: Callable[[], bool] | None = None) -> Turn:
        """Asks why ``script`` failed with ``error`` (its traceback) and for the corrected script."""
        question = ERROR_QUESTION[self.language].format(script=(script or "").rstrip(), error=(error or "").rstrip())
        return self.ask(question, on_text=on_text, cancel=cancel)


_NO_DOCUMENT = object()  # before the first question


def _ref(document: Any) -> Any:
    """A weak reference to the document, or the document itself when it cannot be referenced weakly."""
    try:
        return weakref.ref(document)
    except TypeError:
        return lambda: document


def _as_written(call: ToolCall) -> ToolCall:
    """The call as it goes back to the model: broken arguments are not its own writing, they go back empty."""
    if isinstance(call.arguments, dict) and "_invalid_json" not in call.arguments:
        return call
    return ToolCall(call.id, call.name, {})


def _cancelled(cancel: Callable[[], bool] | None) -> bool:
    if cancel is None:
        return False
    try:
        return bool(cancel())
    except Exception:
        return False
