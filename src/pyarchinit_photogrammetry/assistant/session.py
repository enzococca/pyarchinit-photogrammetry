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

from . import inventory as _inventory
from .facts import TOOLS, action_name, run_tool
from .knowledge import Index, guide_folder
from .privacy import WARNING, Filter
from .providers import ContextOverflow, Message, Provider, ProviderError, Reply, ToolCall

log = logging.getLogger(__name__)

PASSAGES = 3  # guide passages retrieved for a question
OVERVIEW_CHARS = 4000  # of the scripting page in the system prompt (the names, `window`, the bounds of a script)
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
FROM_TEXT = {"it": "chiamata letta dal testo", "en": "call read from the text"}
ALREADY = {"it": "già chiamato", "en": "already called"}
HAVE_RESULTS = {"it": "hai già i risultati: rispondi", "en": "you already have the results: answer"}
TRUNCATED = {"it": "… [troncato]", "en": "… [truncated]"}
OMITTED = {"it": "(risultato omesso)", "en": "(result omitted)"}
TOO_LONG = {"it": "La conversazione supera il contesto del modello{tokens}: inizia una nuova conversazione",
            "en": "The conversation exceeds the context of the model{tokens}: start a new conversation"}
TOKENS = {"it": " ({0} token)", "en": " ({0} tokens)"}
ACTION_STOPPED = {"it": "[azione fermata: «{0}» non è andata fino in fondo]",
                  "en": "[action stopped: «{0}» did not run to the end]"}
STOP_REASONS = {"it": {"project_changed": "progetto cambiato", "chunk_changed": "chunk attivo cambiato"},
                "en": {"project_changed": "the project changed", "chunk_changed": "the active chunk changed"}}
ACTION_LABELS = {"it": {"ran": "eseguita", "declined": "rifiutata", "stopped": "fermata"},
                 "en": {"ran": "ran", "declined": "declined", "stopped": "stopped"}}

TOOL_RESULT_CHARS = 4000  # of one tool result sent back to the model
KEEP_TOOL_RESULTS = 12  # a local model gets the last ones in full, the older ones as OMITTED
LOCAL_CONTEXT_CHARS = 80_000  # the default budget of a request to a local model
ACTIONS_ROOM = 8000  # characters the context must leave free beyond the prompt for the actions to be offered
ACTIONS_OFF = {"it": "Azioni spente: il contesto di questo modello ({0} caratteri) non lascia gli {1} caratteri "
                     "liberi che servono oltre alle istruzioni",
               "en": "Actions off: the context of this model ({0} characters) does not leave the {1} free characters "
                     "they need beyond the instructions"}
CHARS_PER_TOKEN = 3  # after a context error naming N tokens the budget becomes 3 N characters
REPEAT_ROUNDS = 2  # consecutive rounds of calls already made: the loop stops

_INSTRUCTIONS = {
    "it": (
        "Sei l'assistente di PyArchInit Photogrammetry, un programma di fotogrammetria per l'archeologia. Rispondi in "
        "italiano, in modo breve e preciso.\n"
        "- Non inventare mai numeri sul progetto: usa gli strumenti e riferisci solo quello che restituiscono. Se uno "
        "strumento risponde con \"none\" o \"error\", dillo.\n"
        "- Per come si fa una cosa nel programma usa i passaggi della guida allegati alla domanda o lo strumento "
        "guide_search; per le funzioni Python usa scripting_api.\n"
        "- Quando ti viene chiesto uno script, dai UN solo blocco ```python che usa i nomi della console ({names}); "
        "il programma lo mostra all'utente e lo esegue solo quando lui preme Esegui: non dire mai di averlo eseguito.\n"
        "- Il blocco \"## Il progetto adesso\" dice cosa contiene il progetto aperto e cosa mostra la finestra. Gli "
        "script sono per QUESTO progetto: `chunk` è il chunk attivo nominato lì, un altro chunk si prende col suo "
        "nome con `doc.chunk_by_label(\"nome\")`; usa i nomi di prodotti, raster e versioni che elenca.\n"
        "- Se la vista 2D mostra un raster del tipo chiesto (un'ortofoto, un DEM, un prospetto...), lavora su quello "
        "senza chiedere, anche se altri chunk ne hanno uno, a meno che la domanda ne nomini un altro. Fai UNA "
        "domanda breve, che nomina i chunk o le versioni, solo quando non è mostrato niente di adatto (nessun "
        "raster, o uno di un altro tipo) e il prodotto c'è in più chunk o versioni.\n"
        "- Quando l'utente chiede di fare una cosa, falla con uno strumento che agisce se ce n'è uno; dai uno script "
        "quando chiede uno script.\n"
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
        "program shows it to the user and runs it only when they press Run: never say you ran it.\n"
        "- The block \"## The project now\" says what the open project holds and what the window shows. Scripts are "
        "for THIS project: `chunk` is the active chunk named there, another chunk is taken by its label with "
        "`doc.chunk_by_label(\"label\")`; use the names of the products, rasters and versions it lists.\n"
        "- When the 2D view shows a raster of the kind asked (an orthophoto, a DEM, an elevation...), work on it "
        "without asking, even if other chunks have one, unless the question names another. Ask ONE short question, "
        "naming the chunks or versions, only when nothing suitable is shown (no raster, or one of another kind) and "
        "the product exists in several chunks or versions.\n"
        "- When the user asks to do something, do it with a tool that acts when there is one; give a script when "
        "they ask for a script.\n"
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
_ACTIONS_RULE = {
    "it": ("- Gli strumenti che iniziano con act_ agiscono sul progetto aperto (mostrare, scegliere foto, pietre, "
           "ricostruire un prodotto, unità con SAM, esportare, eseguire uno script); il programma può chiedere "
           "all'utente prima di eseguirli. Uno per richiesta. La loro risposta dice cosa è successo: \"ran\": "
           "true = fatto (riferisci i fatti che dà); \"declined\": true = l'utente ha detto no: di' che non è stato "
           "fatto; \"stopped\": true = fermato dall'utente; \"error\" = non riuscito: di' perché. Non dire mai di "
           "aver fatto una cosa che uno strumento act_ non ha riportato come \"ran\". Il raster mostrato è il "
           "bersaglio; se non è mostrato niente di adatto e il bersaglio non è chiaro, fai una domanda invece di "
           "agire."),
    "en": ("- The tools whose name starts with act_ act on the open project (showing, choosing photos, stones, "
           "building a product again, units by SAM, exporting, running a script); the program may ask the user "
           "before running them. One per request. Their answer says what happened: \"ran\": true = done (report "
           "the facts it gives); \"declined\": true = the user said no: say it was not done; \"stopped\": true = "
           "stopped by the user; \"error\" = it failed: say why. Never say you did something an act_ tool did not "
           "report as \"ran\". The raster shown is the target; when nothing suitable is shown and the target is not "
           "clear, ask a question instead of acting."),
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
    if action_name(name) is not None:
        for key in ("ran", "declined", "stopped"):
            if result.get(key) is True:
                return f"{name} ({ACTION_LABELS[lang][key]})"
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
        if name == "tin_info" and result.get("triangles") is not None:
            word = "triangoli" if lang == "it" else "triangles"
            return f"{name} ({_integer(result['triangles'], lang)} {word})"
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
                 language: str = "it", filter_: Filter | None = None, max_tool_rounds: int = 8,
                 context_chars: int | None = None):
        self.provider = provider
        # the budget of one request in characters: a local model's default LOCAL_CONTEXT_CHARS, none for the services
        if context_chars is None and getattr(provider, "kind", "local") == "local":
            context_chars = LOCAL_CONTEXT_CHARS
        self.context_chars = int(context_chars) if context_chars else None
        self._context_chars_given = self.context_chars  # a new conversation starts from it again
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
        self.context_chars = getattr(self, "_context_chars_given", self.context_chars)

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
            cut = text[:OVERVIEW_CHARS]
            if len(text) > OVERVIEW_CHARS and "\n" in cut:  # whole lines: never half a sentence
                cut = cut[:cut.rindex("\n")]
            self._overview = cut.rstrip()
        return self._overview

    def system_prompt(self, inventory: str | None = None, actions: bool = False) -> str:
        """The instructions, the scripting overview, the tools and, when given, the block of the open project
        (:func:`.inventory.text`, already filtered for an external model). ``actions``: the action tools are
        offered (the rule on how their answers are reported is added, and they are listed)."""
        lang = self.language
        names = ", ".join(f"`{n}`" for n in CONSOLE_NAMES)
        parts = [_INSTRUCTIONS[lang].format(names=names)]
        if actions:
            parts[0] += "\n" + _ACTIONS_RULE[lang]
        if self.external:
            parts[0] += "\n" + _PLACEHOLDERS[lang]
        scripts, tools = _HEADINGS[lang]
        overview = self._scripting_overview()
        if overview:
            parts.append(f"{scripts}\n\n{overview}")
        parts.append(f"{tools}\n\n" + "\n".join(f"- {t.name}: {t.description}" for t in _tools(actions)))
        if inventory:
            parts.append(inventory)
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

    def _messages(self, inventory: str | None = None, actions: bool = False) -> list[Message]:
        """The system prompt and the history as the model sees it (filtered when external)."""
        messages = [Message("system", self.system_prompt(inventory, actions))]
        for turn in self.history:
            if turn.role == "user":
                messages.append(Message("user", self._filter.outgoing(turn.text)))
            elif turn.role == "assistant" and turn.text and turn.error is None:
                messages.append(Message("assistant", self._filter.outgoing(turn.text)))
        return messages

    def _mark(self, text: str) -> str:
        return text if self.external else NOT_SENT[self.language] + text

    def _incoming(self, value: Any) -> Any:
        """``value`` with every placeholder restored, also inside lists and objects (the ``params`` of a build,
        the names of markers)."""
        if isinstance(value, str):
            return self._filter.incoming(value)
        if isinstance(value, list):
            return [self._incoming(v) for v in value]
        if isinstance(value, dict):
            return {k: self._incoming(v) for k, v in value.items()}
        return value

    def _run_call(self, call: ToolCall, document: Any, act: Callable | None = None) -> tuple[Message, str, dict]:
        """The tool message of ``call``, its label for ``read`` and the result as the tool gave it."""
        lang = self.language
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        if "_invalid_json" in arguments:
            raw = str(arguments["_invalid_json"])[:200]
            result: dict = {"error": f"arguments were not valid JSON: {raw}"}
        else:
            # a placeholder in an argument (chunk "chunk 1") names the real chunk; an action gets the real names
            arguments = {k: self._incoming(v) for k, v in arguments.items()}
            result = run_tool(call.name, arguments, document, self.index, act=act)
            if not isinstance(result, dict):
                result = {"error": f"the tool {call.name} returned {type(result).__name__}, not an object"}
        label = f"{TOOL_LABEL[lang]}: {read_label(call.name, result, lang)}"
        if call.from_text:
            label += f" - {FROM_TEXT[lang]}"
        safe = self._filter.facts(result)
        content = json.dumps(safe, ensure_ascii=False, default=str)
        if len(content) > TOOL_RESULT_CHARS:  # one result never fills the context by itself
            content = content[:TOOL_RESULT_CHARS] + TRUNCATED[lang]
        return Message("tool", content, tool_call_id=call.id, error="error" in result), label, result

    def _fit(self, messages: list[Message]) -> list[Message]:
        """The request as it is sent: a local model gets the last KEEP_TOOL_RESULTS tool results in full and the
        older ones as one OMITTED line; then, while the request is longer than ``context_chars``, the oldest tool
        results still in full are omitted too. ``messages`` is not changed."""
        out = list(messages)
        omitted = OMITTED[self.language]
        tools = [i for i, m in enumerate(out) if m.role == "tool" and m.content != omitted]
        if not self.external:
            for i in tools[:-KEEP_TOOL_RESULTS] if len(tools) > KEEP_TOOL_RESULTS else []:
                out[i] = _omitted(out[i], omitted)
            tools = tools[-KEEP_TOOL_RESULTS:]
        if self.context_chars:
            total = sum(len(m.content) for m in out)
            for i in tools:
                if total <= self.context_chars:
                    break
                total -= len(out[i].content) - len(omitted)
                out[i] = _omitted(out[i], omitted)
        return out

    def _complete(self, messages: list[Message], tools, stream, cancel, state: dict) -> Reply:
        """One round: the request fitted to the budget; a server that says the context is exceeded gets one more
        try with the oldest half of the tool results omitted (``messages`` changed in place)."""
        while True:
            try:
                return self.provider.complete(self._fit(messages), tools, on_text=stream, cancel=cancel)
            except ContextOverflow as exc:
                if exc.tokens:  # the size the server named: the budget of the rest of the conversation
                    budget = CHARS_PER_TOKEN * exc.tokens
                    self.context_chars = min(self.context_chars or budget, budget)
                omitted = OMITTED[self.language]
                full = [i for i, m in enumerate(messages) if m.role == "tool" and m.content != omitted]
                if state.get("retried") or not full:
                    tokens = TOKENS[self.language].format(exc.tokens) if exc.tokens else ""
                    raise ProviderError(TOO_LONG[self.language].format(tokens=tokens)) from exc
                state["retried"] = True
                for i in full[:max(1, len(full) // 2)]:
                    messages[i] = _omitted(messages[i], omitted)

    def ask(self, question: str, on_text: Callable[[str], None] | None = None,
            cancel: Callable[[], bool] | None = None, inventory: dict | None = None,
            act: Callable[[str, dict], dict] | None = None) -> Turn:
        """Asks ``question``: the answer's turn, appended to the history after the question's.

        ``on_text`` receives the deltas of every round as they arrive (restored best-effort: a placeholder split
        across two deltas reaches it as the model wrote it) and ``"\\n"`` after a round that ended in tool calls:
        when the turn ends, replace what was shown with ``Turn.text``, which is the restored answer. ``cancel`` is
        read from a watcher thread too: give a thread-safe callable (an ``Event.is_set``).

        ``inventory``: the snapshot of the open project (:func:`.inventory.snapshot`, taken where the window may be
        read), written as the block ``## Il progetto adesso`` of the system prompt - through the privacy filter for an
        external model - and listed among what was sent. None: the prompt without it.

        ``act(action, arguments) -> dict``: the window's way to carry out an action (:mod:`.actions`), called from
        this thread with the arguments the model gave, placeholders already turned back into the real names; it
        answers ``{"ran": True, ...}``, ``{"declined": True}``, ``{"stopped": True}`` or ``{"error": ...}``, which
        go back to the model through the filter. Only with ``act`` are the action tools offered. An action that
        answers ``stopped`` ends the turn at once (no other round): its text says so and ``cancelled`` is set."""
        lang = self.language
        document = self._document_now()
        block = _inventory.text(_inventory.filtered(inventory, self._filter), lang) if inventory is not None else None
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
        if block:
            turn.sent.append(self._mark(block))
        turn.sent.append(self._mark(composed))
        self.history.append(Turn("user", question))
        self.history.append(turn)
        actions = act is not None
        if actions and self.context_chars:  # the actions' rule and tools need room the model's context may not have
            needed = len(self.system_prompt(block, True)) + len(composed) + \
                sum(len(t.description) + len(json.dumps(t.parameters)) for t in TOOLS)
            if self.context_chars - needed < ACTIONS_ROOM:
                actions, act = False, None
                off = ACTIONS_OFF[lang].format(_integer(self.context_chars, lang), _integer(ACTIONS_ROOM, lang))
                turn.note = f"{turn.note} · {off}" if turn.note else off
        offered = _tools(actions)
        messages = self._messages(block, actions)[:-1] + [Message("user", composed)]

        def stream(delta: str) -> None:
            if on_text is not None:
                on_text(self._filter.incoming(delta))

        rounds = 0
        repeats = 0  # consecutive rounds made only of calls already made in this turn
        done: dict[str, Message] = {}  # the result of every call made in this turn, by name and arguments
        state: dict = {}
        earlier = ""  # the text of the rounds that ended in tool calls
        reply: Reply | None = None
        stopped: str | None = None  # the action the user stopped
        why = None  # the reason the window gave (the project changed...)
        try:
            while True:
                with_tools = rounds < self.max_tool_rounds and repeats < REPEAT_ROUNDS
                if not with_tools and rounds:
                    # the bound of the rounds first; else the model only repeated calls it already made
                    last = ANSWER_NOW[lang] if rounds >= self.max_tool_rounds else HAVE_RESULTS[lang]
                    messages.append(Message("user", last))
                    turn.sent.append(self._mark(last))
                reply = self._complete(messages, offered if with_tools else None, stream, cancel, state)
                if not (reply.tool_calls and with_tools and reply.stop != "refusal") or _cancelled(cancel):
                    break
                messages.append(Message("assistant", reply.text, tool_calls=[_as_written(c) for c in reply.tool_calls]))
                new_call = False
                for call in reply.tool_calls:
                    if call.from_text:  # the model wrote the call in its text: said where what left is said
                        turn.sent.append(self._mark(f"[{FROM_TEXT[lang]}: {call.name}]"))
                    key = _call_key(call)
                    earlier_result = done.get(key)
                    if earlier_result is not None:  # the same call again: the result it had, not run again
                        message = Message("tool", earlier_result.content, tool_call_id=call.id,
                                          error=earlier_result.error)
                        turn.sent.append(self._mark(f"[{ALREADY[lang]}: {call.name}]"))
                        turn.read.append(f"{TOOL_LABEL[lang]}: {call.name} - {ALREADY[lang]}")
                    elif stopped and action_name(call.name) is not None:  # after a stop no other action
                        message = Message("tool", json.dumps({"stopped": True}), tool_call_id=call.id)
                        turn.read.append(f"{TOOL_LABEL[lang]}: {read_label(call.name, {'stopped': True}, lang)}")
                    else:
                        new_call = True
                        message, label, result = self._run_call(call, document, act)
                        done[key] = message
                        turn.read.append(label)
                        if result.get("stopped") is True and action_name(call.name) is not None and not stopped:
                            stopped = call.name
                            why = result.get("reason")
                    messages.append(message)
                    turn.sent.append(self._mark(message.content))
                repeats = 0 if new_call else repeats + 1
                if reply.text:
                    earlier = (earlier + "\n" + reply.text) if earlier else reply.text
                    if on_text is not None:
                        on_text("\n")
                rounds += 1
                if stopped:  # the user stopped an action: the model is not asked to go on
                    break
        except ProviderError as exc:
            turn.error = str(exc)
            if reply is None:
                return turn
        if reply is None:
            return turn
        turn.cancelled = _cancelled(cancel) or stopped is not None
        answer = reply.text or earlier
        if stopped is not None:
            from .actions import TITLES

            line = ACTION_STOPPED[lang].format(TITLES[lang].get(action_name(stopped) or "", stopped))
            if why:
                line = f"{line[:-1]}: {STOP_REASONS[lang].get(str(why), str(why))}]"
            answer = f"{earlier}\n\n{line}" if earlier else line
        if reply.stop == "refusal":
            turn.error = REFUSED[lang]
        elif reply.stop == "length":
            answer = f"{answer}\n\n{LENGTH_NOTE[lang]}" if answer else LENGTH_NOTE[lang]
        turn.text = self._filter.incoming(answer)
        turn.script = extract_script(turn.text)
        return turn

    def ask_about_error(self, script: str, error: str, on_text: Callable[[str], None] | None = None,
                        cancel: Callable[[], bool] | None = None, inventory: dict | None = None,
                        act: Callable[[str, dict], dict] | None = None) -> Turn:
        """Asks why ``script`` failed with ``error`` (its traceback) and for the corrected script."""
        question = ERROR_QUESTION[self.language].format(script=(script or "").rstrip(), error=(error or "").rstrip())
        return self.ask(question, on_text=on_text, cancel=cancel, inventory=inventory, act=act)


_NO_DOCUMENT = object()  # before the first question


def _tools(actions: bool) -> list:
    """The tools offered: those that read, and the actions when there is a window to act in."""
    return [t for t in TOOLS if actions or not t.acts]


def _ref(document: Any) -> Any:
    """A weak reference to the document, or the document itself when it cannot be referenced weakly."""
    try:
        return weakref.ref(document)
    except TypeError:
        return lambda: document


def _call_key(call: ToolCall) -> str:
    """The same tool with the same arguments: the same key."""
    try:
        arguments = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        arguments = repr(call.arguments)
    return f"{call.name}\0{arguments}"


def _omitted(message: Message, text: str) -> Message:
    return Message(message.role, text, tool_calls=message.tool_calls, tool_call_id=message.tool_call_id,
                   error=message.error)


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
