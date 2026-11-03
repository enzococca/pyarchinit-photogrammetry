"""The actions of the assistant in the window: the setting *Vista > Assistente > Azioni* (when they run), the card
that shows an action in the conversation (:class:`ActionCard`: *Esegui*, *Modifica*, *No*, then the result and
*Annulla*) and the words for an action and its result. The pane (:mod:`.assistant_pane`) asks and runs them."""
from __future__ import annotations

import json
from concurrent.futures import Future, InvalidStateError

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..assistant import actions as _actions
from ..assistant.session import STOP_REASONS as _STOP_REASONS
from .dialogs import Field, ParamDialog
from .i18n import NO_TRANSLATE, current, decimal_comma, tr
from .icons import icon

ACTION_SETTING = "assistant_actions"  # "ask" | "quick" | "all"
ACTION_MODES = ("ask", "quick", "all")
ACTION_DEFAULT = "quick"
ACTION_MODE_NAMES = {"ask": "Chiedi sempre", "quick": "Subito le rapide",
                     "all": "Esegui tutto (script ed esportazioni chiedono)"}
ACTION_MODE_TIPS = {
    "ask": "Ogni azione dell'assistente aspetta Esegui",
    "quick": "Mostrare e filtrare o selezionare foto subito; pietre, ricostruzioni, SAM, esportazioni, foto attivate "
             "o disattivate, un altro chunk e script aspettano Esegui",
    "all": "Ogni azione parte subito tranne gli script e le esportazioni, che chiedono Esegui; ciò che uno script fa "
           "fuori dal progetto non si annulla"}
# the name of each action in the cards and in Modifica > Annulla ("Assistente: Pietre (SAM)"): the engine's table
ACTION_TITLES = _actions.TITLES["it"]
MAX_CARDS = 4  # cards kept under the conversation (the oldest go)
# why an action was stopped: the code goes to the session (which says it in the conversation's language), the text
# (translated by the interface) to the card and the chat - the one table of the session
STOP_REASONS = _STOP_REASONS["it"]
# the facts an action answers, as the card and the chat name them
RESULT_LABELS = {"stones": "pietre", "total": "in tutto", "layer": "layer", "raster": "raster", "chunk": "chunk",
                 "units": "unità", "file": "file", "bytes": "byte", "what": "cosa", "format": "formato",
                 "shown": "mostrato", "version": "versione", "elevation": "prospetto", "photos": "foto",
                 "labels": "nomi", "by": "scelte per", "then": "poi", "changed": "cambiate", "product": "prodotto",
                 "version_kept": "versione conservata", "resolution": "risoluzione", "width": "larghezza",
                 "height": "altezza", "bands": "bande", "points": "punti", "vertices": "vertici", "faces": "facce",
                 "texture": "texture", "pages": "pagine", "method": "metodo", "triangles": "triangoli",
                 "source": "da", "lines": "linee", "interval": "equidistanza", "note": "nota", "output": "uscita",
                 "seconds": "secondi"}
# the area of the actions that work on a raster, when no box is given
WHOLE_AREA = {"stones": "tutto il raster", "segment": "il poligono selezionato, altrimenti tutto il raster"}


def action_mode(settings=None) -> str:
    """The setting *Vista > Assistente > Azioni*: "ask", "quick" (the default) or "all"."""
    if settings is None:
        from . import app_settings

        settings = app_settings()
    mode = str(settings.value(ACTION_SETTING, ACTION_DEFAULT) or ACTION_DEFAULT)
    return mode if mode in ACTION_MODES else ACTION_DEFAULT


def _value_text(value) -> str:
    """A parameter's value as the card writes it: numbers with the decimal comma in Italian, yes/no, lists."""
    if isinstance(value, bool):
        return tr("sì") if value else tr("no")
    if isinstance(value, (int, float)):
        return decimal_comma(f"{value:g}")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value_text(v) for v in value) + "]"
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_value_text(v)}" for k, v in value.items())
    return str(value)


def _label(prop: dict, key: str) -> str:
    return str(prop.get("description_it" if current() == "it" else "description_en") or key)


def describe_action(name: str, args: dict) -> list[tuple[str, str]]:
    """The parameters of an action in words: ``(label of the schema, value)`` in the language of the interface;
    ``build``'s ``params`` one by one with the labels of the product."""
    entry = _actions.ACTIONS.get(name)
    props = (entry[4].get("properties") or {}) if entry else {}
    out = []
    for key, value in (args or {}).items():
        if value is None or value == "":
            continue
        prop = props.get(key) or {}
        if key == "params" and isinstance(value, dict) and "per_product" in prop:
            per = prop["per_product"].get(str(args.get("product")), {})
            out += [(_label(per.get(k) or {}, k), _value_text(v)) for k, v in value.items() if v is not None]
            continue
        out.append((_label(prop, key), _value_text(value)))
    return out


def resolve_future(future: Future, value: dict) -> None:
    """The answer of an action, once (a future already answered - stopped meanwhile - keeps its answer)."""
    if not future.done():
        try:
            future.set_result(value)
        except InvalidStateError:
            pass


class ActionCard(QFrame):
    """An action the model asks for, in the conversation: its name and parameters in words; *Esegui*, *Modifica*
    (the parameters in a small form), *No* while it waits; then the result line and *Annulla* (the window's undo of
    that step, while it is the last one). ``state``: "waiting", "running", "done", "declined", "stopped", "failed"."""

    STATES = {"waiting": "in attesa", "running": "in corso...", "done": "eseguita", "declined": "non eseguita",
              "stopped": "fermata", "failed": "non riuscita"}

    def __init__(self, name: str, args: dict, parent=None, chunk_label: str | None = None):
        super().__init__(parent)
        self.name = name
        self.args = dict(args)
        self.chunk_label = chunk_label  # the chunk the action is for (the active one when it was asked)
        self.state = "waiting"
        self.why: str | None = None  # why it stopped (the project changed...)
        self.entry = None  # the entry of Modifica > Annulla the action made
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        self.title = QLabel()
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.title.setProperty(NO_TRANSLATE, True)
        lay.addWidget(self.title)
        self.params = QLabel()
        self.params.setWordWrap(True)
        self.params.setProperty(NO_TRANSLATE, True)  # values and labels already in the interface's language
        lay.addWidget(self.params)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        self.result_label.setProperty(NO_TRANSLATE, True)
        self.result_label.hide()
        lay.addWidget(self.result_label)
        row = QHBoxLayout()
        self.b_run = QPushButton(icon("run"), tr("Esegui"))
        self.b_edit = QPushButton(icon("settings"), tr("Modifica"))
        self.b_no = QPushButton(icon("cancel"), tr("No"))
        self.b_undo = QPushButton(icon("undo"), tr("Annulla", context="undo"))
        self.b_undo.setToolTip(tr("Riporta il progetto a prima di questa azione (come Modifica > Annulla)"))
        self.b_undo.hide()
        for b in (self.b_run, self.b_edit, self.b_no, self.b_undo):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.refresh()

    def heading(self) -> str:
        return tr("Azione: {0}", tr(ACTION_TITLES.get(self.name, self.name)))

    def text(self) -> str:
        """What the card says: its heading and its parameters."""
        return f"{self.title.text()}\n{self.params.text()}"

    def refresh(self) -> None:
        state = tr(self.STATES[self.state])
        if self.why:
            state = f"{state}: {tr(STOP_REASONS.get(self.why, self.why))}"
        self.title.setText(f"{self.heading()} ({state})")
        words = describe_action(self.name, self.args)
        chunk = self.args.get("chunk_label") or self.chunk_label  # the chunk the arguments name, else the active one
        lines = [tr("Chunk: {0}", chunk)] if chunk else []
        lines += [f"{label}: {value}" for label, value in words]
        if self.name in WHOLE_AREA and not self.args.get("bounds"):
            lines.append(tr("Area: {0}", tr(WHOLE_AREA[self.name])))
        if self.name == "show" and self.args.get("chunk_label") and self.args["chunk_label"] != self.chunk_label:
            lines.append(tr("Mostrare un altro chunk lo rende attivo e ne fa ripartire la cronologia di Annulla"))
        if not words and len(lines) <= 1:
            lines.append(tr("con i valori predefiniti"))
        self.params.setText("\n".join(lines))
        waiting = self.state == "waiting"
        for b in (self.b_run, self.b_edit, self.b_no):
            b.setVisible(waiting)
        self.b_undo.setVisible(self.entry is not None)

    def set_state(self, state: str, why: str | None = None) -> None:
        self.state = state
        self.why = why
        self.refresh()

    def finish(self, result: dict, entry=None) -> None:
        """After the run: the state and the result line from ``result``, *Annulla* for ``entry``. A card already
        stopped stays stopped; an action that finished anyway says so."""
        self.entry = entry
        line = result_line(self.name, result)
        if self.state == "stopped":
            if result.get("ran"):
                line = f"{line} {tr('(completata comunque)')}"
            self.result_label.setText(line)
            self.result_label.show()
            self.refresh()
        else:
            self.result_label.setText(line)
            self.result_label.show()
            self.set_state("done" if result.get("ran") else "stopped" if result.get("stopped") else
                           "declined" if result.get("declined") else "failed")
        self.b_undo.setEnabled(entry is not None)


def _json_type(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return {list: "array", dict: "object", str: "string"}.get(type(value), "null")


def parse_value(text: str, prop: dict):
    """A value typed in *Modifica* as the schema of the parameter wants it: a number, true/false, a list or an
    object read as JSON only when the parameter takes that type; a parameter that takes a text keeps the text as it
    is (a label "2024" stays a string). None for an empty field."""
    text = (text or "").strip()
    if not text:
        return None
    kinds = prop.get("type") or "string"
    kinds = set(kinds) if isinstance(kinds, (list, tuple)) else {kinds}
    if kinds == {"string"}:
        return text
    try:
        value = json.loads(text.replace(",", ".") if kinds & {"number", "integer"} and "," in text
                           and not text.startswith(("[", "{")) else text)
    except ValueError:
        return text  # not JSON: as written (the action says what it wants)
    kind = _json_type(value)
    if kind in kinds or (kind == "integer" and "number" in kinds):
        return value
    return text if "string" in kinds else value


def edit_args(parent, name: str, args: dict, title: str) -> dict | None:
    """*Modifica*: the parameters of the action ``name`` in a form (enums as choices, the rest as text read by
    :func:`parse_value`); the new arguments, or None when cancelled."""
    entry = _actions.ACTIONS.get(name)
    if entry is None:
        return None
    props = entry[4].get("properties") or {}
    fields = []
    for key, prop in props.items():
        if prop.get("script_only"):  # a script's parameter (export's path): the window asks for it, as for the model
            continue
        value = args.get(key)
        label = _label(prop, key)
        if prop.get("enum") and not isinstance(value, (list, dict)):
            choices = [("", tr("(predefinito)"))] + [(v, str(v)) for v in prop["enum"]]
            fields.append(Field(key, label, value if value is not None else "", choices=choices))
        else:
            text = "" if value is None else value if isinstance(value, str) else json.dumps(value)
            fields.append(Field(key, label, text, kind="text"))
    values = ParamDialog.ask(parent, title, fields)
    if values is None:
        return None
    out = {}
    for key, value in values.items():
        if isinstance(value, str):
            value = parse_value(value, props.get(key) or {})
        if value is not None and value != "":
            out[key] = value
    return out


def result_line(name: str, result: dict) -> str:
    """The result of an action in one line for the user."""
    if result.get("stopped"):
        return tr("Fermata")
    if result.get("declined"):
        return tr("Non eseguita")
    if "error" in result:
        return tr("Errore: {0}", result["error"])
    facts = {k: v for k, v in result.items() if k not in ("ran", "not_undoable") and v not in (None, [], "")}
    line = "; ".join(f"{tr(RESULT_LABELS.get(k, k))}: {_value_text(v)}" for k, v in facts.items()) or tr("fatto")
    if result.get("not_undoable"):
        line += " - " + tr("non annullabili: {0}", ", ".join(result["not_undoable"]))
    return line

