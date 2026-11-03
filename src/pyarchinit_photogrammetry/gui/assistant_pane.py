"""The Assistente pane: on the left the conversation with the model chosen (local, Claude or OpenAI), what left the
computer one click away; on the right a script editor whose scripts run in the worker thread
(:meth:`MainWindow.run_script_source`) and are undone in one step with *Annulla lo script*.

The model answers in a thread of its own (:class:`AssistantWorker`): the pane, and the window, stay usable while it
thinks, a question can be stopped, and a failed one is said in the conversation. Widgets are only touched in the
interface thread: the worker talks through signals.

Actions: the session is given :meth:`AssistantPane.act`. When the model calls an action tool, ``act`` runs in the
assistant's thread: it hands the call to the interface thread (a queued signal carrying a
:class:`concurrent.futures.Future`) and waits on the future. In the interface thread :meth:`AssistantPane._on_action`
decides by the setting ``assistant_actions`` (*Vista > Assistente > Azioni*: "ask" every action waits for *Esegui*,
"quick" - the default - showing and choosing photos to filter or select them run at once, "all" everything runs at
once) whether an :class:`ActionCard` waits for *Esegui* / *Modifica* / *No*, and runs the action through
:meth:`MainWindow.run_action` (one entry of Modifica > Annulla, *Annulla* on the card). The future gets
``{"ran": True, ...}``, ``{"declined": True}``, ``{"stopped": True}`` (*Ferma*, which also stops a running action) or
``{"error": ...}``. The arguments arrive with the real names already restored by the session."""
from __future__ import annotations

import html
import logging
import textwrap
import threading
import time
import weakref
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QFont, QKeySequence, QShortcut, QTextCursor, QTextDocument, QTextDocumentFragment
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView,
                               QLabel, QPlainTextEdit, QPushButton, QSizePolicy, QSplitter, QTableWidget,
                               QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)

from ..assistant import actions as _actions
from ..assistant import inventory, privacy
from ..assistant.providers import Provider
from ..assistant.session import _SCRIPT, Assistant, Turn
from .assistant_actions import (ACTION_TITLES, MAX_CARDS, ActionCard, STOP_REASONS, action_mode, edit_args,
                                resolve_future, result_line)
from .assistant_settings import (KIND_NAMES, KINDS, context_chars_of, keep_thread, park, release_thread,
                                 same_provider, saved_kind, saved_model)
from .i18n import current, tr
from .icons import icon

log = logging.getLogger(__name__)

COLOURS = {"green": "#2e7d32", "orange": "#ef6c00", "grey": "#757575"}
SHUTDOWN_MS = 5000


def scripts_of(text: str) -> list[str]:
    """Every ```python block of an answer, dedented, in order (the editor button of each)."""
    found = []
    for m in _SCRIPT.finditer((text or "").replace("\r\n", "\n")):
        body = textwrap.dedent(m.group("body")).strip("\n").rstrip()
        if body:
            found.append(body)
    return found


class AssistantWorker(QObject):
    """Asks in its own thread. ``assistant`` is set by the pane while the worker is idle; ``cancel`` (from any
    thread) stops the question in progress within a tenth of a second."""

    text = Signal(str)
    finished = Signal(object)  # the Turn
    failed = Signal(str)
    resolved = Signal(int, object, str)  # the request number, the provider (None), why there is none

    def __init__(self):
        super().__init__()
        self.assistant: Assistant | None = None
        self.act = None  # the pane's way to carry out an action (AssistantPane.act), given to every question
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def arm(self) -> None:
        """Before a question is sent (from the interface thread): a Ferma pressed after this stops it, even before
        the worker has started it."""
        self._cancel.clear()

    def _index(self, assistant: Assistant) -> None:
        if assistant.index is None:
            try:
                from ..assistant.knowledge import default_index

                assistant.index = default_index(assistant.language)
            except Exception:  # noqa: BLE001 - the guide is a help, never a reason not to answer
                log.debug("guide index", exc_info=True)

    def _run(self, fn) -> None:
        assistant = self.assistant
        if assistant is None:
            self.failed.emit(tr("Nessun modello scelto"))
            return
        try:
            self._index(assistant)
            turn = fn(assistant)
        except Exception as exc:  # noqa: BLE001 - said in the conversation, the pane stays usable
            log.warning("assistente: %s", exc, exc_info=True)
            self._emit(self.failed, f"{type(exc).__name__}: {exc}")
            return
        self._emit(self.finished, turn)

    @staticmethod
    def _emit(signal, value) -> None:
        try:
            signal.emit(value)
        except RuntimeError:  # parked (the window went away while the model hung): nobody listens any more
            log.debug("assistente: risposta di un thread parcheggiato", exc_info=True)

    @Slot(str, object)
    def ask(self, question: str, inventory=None) -> None:
        self._run(lambda a: a.ask(question, on_text=self.text.emit, cancel=self._cancel.is_set, inventory=inventory,
                                  act=self.act))

    @Slot(str, str, object)
    def ask_error(self, script: str, error: str, inventory=None) -> None:
        self._run(lambda a: a.ask_about_error(script, error, on_text=self.text.emit, cancel=self._cancel.is_set,
                                              inventory=inventory, act=self.act))

    @Slot(int)
    def resolve(self, request: int) -> None:
        """The provider of the saved settings (finding a local server takes a moment: never in the interface)."""
        from . import app_settings
        from .assistant_settings import provider_from_settings

        why: list[str] = []
        try:
            provider = provider_from_settings(app_settings(), why)
        except Exception as exc:  # noqa: BLE001
            log.warning("assistente: provider", exc_info=True)
            provider, why = None, [str(exc)]
        self.resolved.emit(request, provider, why[0] if why else "")


class _Editor(QPlainTextEdit):
    """The script editor: monospace, Tab = 4 spaces."""

    def __init__(self):
        super().__init__()
        mono = QFont("Menlo")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(mono)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)

    def keyPressEvent(self, e) -> None:  # noqa: N802 - Qt name
        if e.key() == Qt.Key.Key_Tab and not e.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            self.textCursor().insertText("    ")
            return
        super().keyPressEvent(e)


class AssistantPane(QWidget):
    """The pane of the window (bottom area, beside the Python console)."""

    _ask = Signal(str, object)  # the question and the inventory of the project (taken here, in the interface)
    _ask_error = Signal(str, str, object)
    _resolve = Signal(int)
    _act_request = Signal(str, object, object)  # an action from the assistant's thread: name, arguments, Future

    def __init__(self, window):
        super().__init__()
        self._main = window
        self.assistant: Assistant | None = None
        self.last_result = None
        self.scripts: list[str] = []
        self._thread: QThread | None = None
        self._worker: AssistantWorker | None = None
        self._busy = False
        self._request = 0  # the provider asked of the settings: an older answer is dropped
        self._waiting: tuple | None = None  # the question asked before the provider was known
        self._stream_at: int | None = None
        self._script_source = ""
        self._script_running = False
        self._started = False  # a question was asked in the conversation on screen
        self._warned = False  # the warning line written in it (external models)
        self._skip_note = False
        self._doc_ref = None  # the project the conversation on screen is about
        self._asking = None  # the assistant the question in progress was asked of
        self.cards: list[ActionCard] = []
        self._action: dict | None = None  # the action of the question in progress: future, card, running
        self._action_lines: list[str] = []  # what the actions of the question in progress did, for the chat
        self._asked_on: tuple | None = None  # (project, chunk key, chunk label) of the question in progress
        self._build()
        self._show_kind(saved_kind(self._settings()), None)
        self._act_request.connect(self._on_action, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------------ building
    def _build(self) -> None:
        split = QSplitter(Qt.Orientation.Horizontal)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(split)
        # the conversation
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.model_box = QComboBox()
        self.model_box.setToolTip(tr("Il modello che risponde"))
        self._fill_models()
        self.model_box.activated.connect(self._model_chosen)
        self.dot = QLabel()
        self.b_sent = QPushButton(icon("export"), tr("Cosa viene inviato"))
        self.b_sent.setToolTip(tr("Il testo esatto uscito dal computer in questa conversazione"))
        self.b_sent.clicked.connect(self.show_sent)
        self.b_new = QPushButton(icon("new"), tr("Nuova conversazione"))
        self.b_new.clicked.connect(self.new_conversation)
        self.b_settings = QPushButton(icon("settings"), tr("Impostazioni..."))
        self.b_settings.clicked.connect(self.open_settings)
        top.addWidget(self.model_box, 1)
        top.addWidget(self.dot)
        top.addWidget(self.b_sent)
        top.addWidget(self.b_new)
        top.addWidget(self.b_settings)
        ll.addLayout(top)
        self.chat = QTextBrowser()
        self.chat.setOpenLinks(False)
        self.chat.anchorClicked.connect(self._link)
        ll.addWidget(self.chat, 1)
        self.cards_area = QWidget()  # the cards of the actions, under the conversation
        self.cards_layout = QVBoxLayout(self.cards_area)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_area.hide()
        ll.addWidget(self.cards_area)
        self.warning = QLabel(tr(privacy.WARNING))
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(f"color: {COLOURS['orange']};")
        self.warning.hide()
        ll.addWidget(self.warning)
        self.question = QPlainTextEdit()
        self.question.setPlaceholderText(tr("Scrivi una domanda (Ctrl+Invio per inviare)"))
        self.question.setMaximumHeight(64)
        self.question.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        ll.addWidget(self.question)
        for keys in ("Ctrl+Return", "Ctrl+Enter"):
            sc = QShortcut(QKeySequence(keys), self.question)
            sc.setContext(Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(self._send)
        row = QHBoxLayout()
        self.b_send = QPushButton(icon("run"), tr("Chiedi"))
        self.b_send.clicked.connect(self._send)
        self.b_stop = QPushButton(icon("cancel"), tr("Ferma"))
        self.b_stop.setEnabled(False)
        self.b_stop.clicked.connect(self.stop)
        row.addStretch(1)
        row.addWidget(self.b_send)
        row.addWidget(self.b_stop)
        ll.addLayout(row)
        split.addWidget(left)
        # the editor
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.editor = _Editor()
        self.editor.setPlaceholderText(tr("Script Python: doc, chunk, window, np... Esegui lo fa girare sul progetto"))
        rl.addWidget(self.editor, 3)
        row = QHBoxLayout()
        self.b_run = QPushButton(icon("run"), tr("Esegui"))
        self.b_run.clicked.connect(self.run_editor)
        self.b_stop_script = QPushButton(icon("cancel"), tr("Ferma"))
        self.b_stop_script.setEnabled(False)
        self.b_stop_script.clicked.connect(lambda: self._main.runner.cancel())
        self.b_undo = QPushButton(icon("undo"), tr("Annulla lo script"))
        self.b_undo.setToolTip(tr("Riporta il progetto a prima dello script, in un passo (come Modifica > Annulla)"))
        self.b_undo.setEnabled(False)
        self.b_undo.clicked.connect(self.undo_script)
        self.b_save = QPushButton(icon("save"), tr("Salva come .py"))
        self.b_save.clicked.connect(self.save_script)
        for b in (self.b_run, self.b_stop_script, self.b_undo, self.b_save):
            row.addWidget(b)
        row.addStretch(1)
        rl.addLayout(row)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(self.editor.font())
        rl.addWidget(self.output, 2)
        self.b_ask_error = QPushButton(icon("assistant"), tr("Errore? Chiedi all'assistente"))
        self.b_ask_error.clicked.connect(self.ask_about_error)
        self.b_ask_error.hide()
        rl.addWidget(self.b_ask_error)
        split.addWidget(right)
        split.setSizes([600, 500])

    @staticmethod
    def _settings():
        from . import app_settings

        return app_settings()

    def _fill_models(self) -> None:
        s = self._settings()
        self.model_box.blockSignals(True)
        self.model_box.clear()
        for kind in KINDS:
            model = saved_model(s, kind)
            name = tr(KIND_NAMES[kind])
            self.model_box.addItem(tr("{0} · {1}", name, model) if model else name, kind)
        self.model_box.addItem(tr("Impostazioni..."), "settings")
        k = self.model_box.findData(saved_kind(s))
        self.model_box.setCurrentIndex(max(k, 0))
        self.model_box.blockSignals(False)

    # ------------------------------------------------------------------ the provider
    def _show_kind(self, kind: str | None, provider: Provider | None) -> None:
        """The dot (green: nothing leaves; orange: external) and the warning line above the question."""
        if kind is None:
            colour, label = "grey", tr("nessun modello")
        elif kind == "local":
            colour, label = "green", tr("locale")
        else:
            colour, label = "orange", tr("esterno")
        self.dot.setText(f"● {label}")
        self.dot.setStyleSheet(f"color: {COLOURS[colour]};")
        self.dot.setProperty("colour", colour)
        self.dot.setToolTip(provider.label if provider is not None else "")
        self.warning.setVisible(kind not in (None, "local"))

    def set_provider(self, provider: Provider | None) -> None:
        """The model that answers from now on: a new conversation (each has its own privacy filter). A conversation
        already on screen is cleared, with a line naming the new model: the chat and *Cosa viene inviato* always
        tell the same conversation."""
        self._request += 1  # a provider still being found is no longer wanted
        if self._busy and self._worker is not None:
            self._worker.cancel()  # its answer belongs to the old conversation: dropped when it arrives
        if self._started:
            label = provider.label if provider is not None else tr("nessun modello")
            self._clear_chat(tr("Modello cambiato: nuova conversazione con {0}", label))
        if provider is None:
            self.assistant = None
        else:
            self.assistant = Assistant(provider, lambda: self._main.doc, None, language=current(),
                                       context_chars=context_chars_of(self._settings()))
        self._show_kind(getattr(provider, "kind", "local") if provider is not None else None, provider)
        if provider is not None:  # the model that answers, as the provider names it
            k = self.model_box.findData(provider.kind)
            if k >= 0:
                self.model_box.setItemText(k, provider.label)
                self.model_box.setCurrentIndex(k)

    def _model_chosen(self, index: int) -> None:
        data = self.model_box.itemData(index)
        if data == "settings":
            self.open_settings()
            return
        s = self._settings()
        if data != saved_kind(s):
            s.setValue("assistant/kind", data)
        self.reload()

    def reload(self) -> None:
        """After the settings or the combo: the same kind, model and address keep the conversation; another one
        starts a new conversation (the chat cleared, saying so) and its provider is found in the assistant's
        thread."""
        settings = self._settings()
        self._fill_models()
        if self.assistant is not None and same_provider(settings, self.assistant.provider):
            k = self.model_box.findData(self.assistant.provider.kind)
            if k >= 0:
                self.model_box.setItemText(k, self.assistant.provider.label)
            return
        if self._busy and self._worker is not None:
            self._worker.cancel()
        if self._started:
            self._clear_chat(tr("Modello cambiato: nuova conversazione"))
        self.assistant = None
        self._show_kind(saved_kind(settings), None)
        self._find_provider()

    def _find_provider(self) -> None:
        self._request += 1
        self._start()
        self._resolve.emit(self._request)

    @Slot(int, object, str)
    def _resolved(self, request: int, provider, why: str) -> None:
        if request != self._request:
            return
        self.set_provider(provider)
        waiting, self._waiting = self._waiting, None
        if waiting is None:
            return
        if provider is None:
            self._set_busy(False)
            self._error_line(why or tr("Nessun modello disponibile: avvia un server locale (LM Studio, llama.cpp, "
                                       "Ollama, Unsloth) o scegli Claude o OpenAI in Impostazioni..."))
            return
        self._busy = False
        self._dispatch(*waiting)

    def open_settings(self) -> None:
        from .assistant_settings import AssistantSettingsDialog

        dialog = AssistantSettingsDialog(self._main, self._settings())
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted:
            self.reload()
        else:
            self._fill_models()

    # ------------------------------------------------------------------ the thread
    def _start(self) -> None:
        if self._thread is not None:
            return
        self._thread = QThread()
        self._worker = AssistantWorker()
        self._worker.act = self.act
        self._worker.moveToThread(self._thread)
        self._ask.connect(self._worker.ask)
        self._ask_error.connect(self._worker.ask_error)
        self._resolve.connect(self._worker.resolve)
        self._worker.text.connect(self._on_text)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.resolved.connect(self._resolved)
        keep_thread(self._thread, self._worker)  # never destroyed while it runs, also if the window is dropped
        self._thread.start()

    def shutdown(self) -> None:
        """Before the window closes: the question in progress stopped, the thread ended."""
        if self._thread is None:
            return
        self._worker.cancel()
        self._thread.quit()
        if self._thread.wait(SHUTDOWN_MS):
            release_thread(self._thread)
        else:  # a provider that ignores cancel: the thread is parked, the program ends all the same
            log.warning("assistente: il thread non si è fermato in tempo")
            park(self._thread, self._worker)
        self._thread = self._worker = None

    @property
    def busy(self) -> bool:
        """A question is on its way (its answer not yet arrived)."""
        return self._busy

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.b_send.setEnabled(not busy)
        self.b_stop.setEnabled(busy)
        self.b_ask_error.setEnabled(not busy)
        self.b_new.setEnabled(not busy)

    # ------------------------------------------------------------------ asking
    def _send(self) -> None:
        text = self.question.toPlainText().strip()
        if text and not self._busy:
            self.question.clear()
            self.ask(text)

    def ask(self, text: str) -> None:
        """Asks ``text``: shown at once, answered in the assistant's thread."""
        if self._busy:
            return
        self._request_answer("ask", text)

    def ask_about_error(self) -> None:
        """*Errore? Chiedi all'assistente*: the script and its traceback, asking for the corrected script."""
        r = self.last_result
        if self._busy or r is None or not r.error:
            return
        self._request_answer("error", self._script_source, r.traceback or r.error)

    def _request_answer(self, mode: str, *args: str) -> None:
        self._set_busy(True)
        if self.assistant is None:
            self._waiting = (mode, *args)
            self._find_provider()
            return
        self._dispatch(mode, *args)

    def _dispatch(self, mode: str, *args: str) -> None:
        self._start()
        self._set_busy(True)
        doc = self._main.doc
        if self._started and self._doc_ref is not None and self._doc_ref() is not doc:
            # another project: the conversation ends here as the assistant ends it (its note is not repeated)
            self.assistant.new_conversation()
            self._clear_chat(tr("Il progetto è cambiato: nuova conversazione"))
            self._skip_note = True
        self._doc_ref = _weak(doc)
        self._asked_on = self._where()  # the project and the active chunk the actions of this question are for
        self._started = True
        self._asking = self.assistant
        if self.assistant.external and not self._warned:
            self._warned = True
            self._paragraph(f'<p style="color:{COLOURS["orange"]}"><i>{html.escape(tr(privacy.WARNING))}</i></p>')
        shown = args[0] if mode == "ask" else tr("Lo script si è fermato con un errore: perché?")
        self._paragraph(f"<p><b>{html.escape(tr('Domanda'))}:</b> {html.escape(shown).replace(chr(10), '<br>')}</p>")
        self._paragraph(f"<p><b>{html.escape(self.assistant.provider.label)}:</b></p>")
        self._stream_at = self._end().position()
        self._worker.assistant = self.assistant
        self._worker.arm()
        snap = self._inventory(doc)  # the window is read here, never from the assistant's thread
        if mode == "ask":
            self._ask.emit(args[0], snap)
        else:
            self._ask_error.emit(args[0], args[1], snap)

    def _inventory(self, doc):
        """What the project holds and the window shows now (:func:`..assistant.inventory.snapshot`); None when it
        cannot be read: the question goes without it."""
        try:
            return inventory.snapshot(doc, self._main)
        except Exception:  # noqa: BLE001 - the question is never stopped by its inventory
            log.warning("assistente: inventario del progetto", exc_info=True)
            return None

    def stop(self) -> None:
        """*Ferma*: the question stopped; an action waiting for *Esegui* answers ``stopped``, a running one is
        stopped too (the task runner's cancel) and its answer is ``stopped`` at once."""
        self._stop_action()
        if self._worker is not None:
            self._worker.cancel()
        if self._waiting is not None:  # still looking for the model: nothing asked
            self._waiting = None
            self._request += 1
            self._set_busy(False)

    # ------------------------------------------------------------------ actions
    def action_mode(self) -> str:
        """*Vista > Assistente > Azioni*: "ask", "quick" (default) or "all"."""
        return action_mode(self._settings())

    def act(self, name: str, args: dict) -> dict:
        """The action ``name`` with ``args`` (real names), asked by the model: called in the assistant's thread, it
        hands the call to the interface thread and waits for the answer (``{"ran": True, ...}``,
        ``{"declined": True}``, ``{"stopped": True}`` or ``{"error": ...}``). The question stopped while it waits
        (the window closing) answers ``stopped``."""
        if threading.current_thread() is threading.main_thread():  # never wait on the thread that answers
            return {"error": "actions are asked from the assistant's thread"}
        worker = self._worker
        if worker is not None and worker.cancelled():  # Ferma already pressed: nothing more is asked
            return {"stopped": True}
        future: Future = Future()
        self._act_request.emit(name, dict(args or {}), future)
        while True:
            try:
                return future.result(timeout=0.1)
            except FutureTimeout:
                if worker is not None and worker.cancelled():
                    try:
                        return future.result(timeout=0.5)  # Ferma answers in the interface thread
                    except FutureTimeout:
                        future.cancel()
                        return {"stopped": True}

    def _where(self) -> tuple:
        """The project and the key of the active chunk the question was asked on (what its inventory said)."""
        doc = self._main.doc
        chunk = getattr(doc, "chunk", None) if doc is not None else None
        return _weak(doc), (chunk.key if chunk is not None else None), (chunk.label if chunk is not None else None)

    @Slot(str, object, object)
    def _on_action(self, name: str, args: dict, future: Future) -> None:
        """In the interface thread: the card, or the action at once, as the setting says."""
        if future.done():
            return
        doc_ref, key, label = self._asked_on or self._where()
        mode = self.action_mode()
        quick = _actions.is_quick(name, args, label)
        # under "all" too the model's own code and files written outside the project wait for Esegui
        at_once = (mode == "all" and name not in _actions.ASK_ALWAYS) or (mode == "quick" and quick)
        action = {"future": future, "card": None, "running": False, "name": name, "args": args,
                  "doc": doc_ref, "chunk": key}
        if not (at_once and quick):  # a quick action that runs at once leaves only a line in the chat
            action["card"] = self._add_card(name, args, future, label)
        self._action = action
        if at_once:
            self._execute(action)

    def _add_card(self, name: str, args: dict, future: Future, chunk_label: str | None) -> ActionCard:
        card = ActionCard(name, args, self.cards_area, chunk_label=chunk_label)
        card.b_run.clicked.connect(lambda: self._card_run(card, future))
        card.b_no.clicked.connect(lambda: self._card_no(card, future))
        card.b_edit.clicked.connect(lambda: self._card_edit(card))
        card.b_undo.clicked.connect(lambda: self._card_undo(card))
        self.cards.append(card)
        self.cards_layout.addWidget(card)
        while len(self.cards) > MAX_CARDS:
            old = self.cards.pop(0)
            old.setParent(None)
            old.deleteLater()
        self.cards_area.show()
        return card

    def _card_run(self, card: ActionCard, future: Future) -> None:
        action = self._action
        if card.state != "waiting" or future.done() or action is None or action["future"] is not future:
            return
        action["args"] = dict(card.args)
        self._execute(action)

    def _card_no(self, card: ActionCard, future: Future) -> None:
        if card.state != "waiting":
            return
        card.set_state("declined")
        self._action_lines.append(f"{card.heading()}: {tr('non eseguita')}")
        if self._action is not None and self._action["future"] is future:
            self._action = None
        resolve_future(future, {"declined": True})

    def _card_edit(self, card: ActionCard) -> None:
        """*Modifica*: the parameters of the action in a form; the card waits again for *Esegui*."""
        if card.state != "waiting":
            return
        args = edit_args(self, card.name, card.args, card.heading())
        if args is not None:
            card.args = args
            card.refresh()

    def _card_undo(self, card: ActionCard) -> None:
        """*Annulla* on a card: the window's undo, while the action's entry is the last one."""
        h = getattr(self._main, "history", None)
        if card.entry is None or _top(h) is not card.entry:
            self._main.statusBar().showMessage(tr("L'azione non è più l'ultimo passo: usa Modifica > Annulla"), 8000)
            card.b_undo.setEnabled(False)
            return
        self._main.undo()
        if _top(h) is not card.entry:  # undone (an undo that failed leaves it the last one)
            card.b_undo.setEnabled(False)
            card.result_label.setText(card.result_label.text() + " - " + tr("annullata"))

    def _moved(self, action: dict) -> str | None:
        """Why ``action`` must not run any more (a code of :data:`STOP_REASONS`): the project or the active chunk
        is not the one it was asked on - or the one an action of the same question left active."""
        doc = self._main.doc
        if action["doc"]() is not doc:
            return "project_changed"
        chunk = getattr(doc, "chunk", None)
        if (chunk.key if chunk is not None else None) != action["chunk"]:
            return "chunk_changed"
        return None

    def _execute(self, action: dict) -> None:
        """The action through :meth:`MainWindow.run_action`; its answer resolves the future. Refused - answered
        ``stopped`` with the reason - when the project or the active chunk changed since the question."""
        from .undo import ScriptAction

        future, card, name = action["future"], action["card"], action["name"]
        why = self._moved(action)
        if why is not None:
            self._stop_action(why)
            return
        action["running"] = True
        if card is not None:
            card.set_state("running")
        h = getattr(self._main, "history", None)
        top0 = _top(h)

        def done(result: dict) -> None:
            if self._action is action:
                self._action = None
            entry = _top(h)
            made = entry if entry is not top0 and entry is not None and isinstance(entry.action, ScriptAction) \
                else None
            if card is not None and card in self.cards:
                card.finish(result, made)
            if result.get("ran") and not future.done() and name == "show" and action["args"].get("chunk_label"):
                # the assistant's own step (a chunk it made active): the next action of this question starts from
                # there; any other change of chunk - the user's, also during a long build - still stops it
                self._asked_on = self._where()
            if not future.done():  # stopped meanwhile: the question already ended saying so
                title = tr(ACTION_TITLES.get(name, name))
                self._action_lines.append(f"{tr('Azione: {0}', title)}: {result_line(name, result)}")
            resolve_future(future, result)

        self._main.run_action(name, action["args"], done)

    def _stop_action(self, why: str | None = None) -> None:
        """The pending or running action stopped (``why``: a code of :data:`STOP_REASONS`, said on the card and in
        the chat in the interface's language; the model gets the code, the session says it in its own)."""
        action, self._action = self._action, None
        if action is None:
            return
        card = action["card"]
        if action["running"]:
            self._main.runner.cancel()
        if card is not None and card in self.cards and card.state in ("waiting", "running"):
            card.set_state("stopped", why)
        title = tr(ACTION_TITLES.get(action["name"], action["name"]))
        if not action["future"].done():
            stopped = tr("fermata") if why is None else tr("fermata: {0}", tr(STOP_REASONS.get(why, why)))
            self._action_lines.append(f"{tr('Azione: {0}', title)}: {stopped}")
        resolve_future(action["future"], {"stopped": True, "reason": why} if why else {"stopped": True})

    def project_changed(self) -> None:
        """The window shows another project: an action of the old one waiting or running is stopped (the model is
        told the project changed); the conversation itself ends at the next question."""
        self._stop_action("project_changed")

    def _end(self) -> QTextCursor:
        cursor = self.chat.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        return cursor

    def _paragraph(self, markup: str) -> None:
        cursor = self._end()
        if not self.chat.document().isEmpty():
            cursor.insertBlock()
        cursor.insertHtml(markup)
        self.chat.setTextCursor(cursor)
        self.chat.ensureCursorVisible()

    def _error_line(self, text: str) -> None:
        self._paragraph(f'<p style="color:#c62828"><b>{html.escape(tr("Errore"))}:</b> {html.escape(text)}</p>')

    @Slot(str)
    def _on_text(self, delta: str) -> None:
        if self._stream_at is None:
            return
        cursor = self._end()
        cursor.insertText(delta)
        self.chat.ensureCursorVisible()

    def _drop_stream(self) -> None:
        if self._stream_at is None:
            return
        cursor = self._end()
        cursor.setPosition(self._stream_at, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self._stream_at = None

    @Slot(object)
    def _on_finished(self, turn: Turn) -> None:
        if self._asking is not self.assistant:  # asked of a model since replaced: not this conversation
            self._asking = None
            self._set_busy(False)
            return
        self._asking = None
        self._drop_stream()
        skip_note, self._skip_note = self._skip_note, False
        if turn.note and not skip_note:
            self._paragraph(f"<p><i>{html.escape(turn.note)}</i></p>")
        self._write_action_lines()
        if turn.text:
            markdown = QTextDocument()
            # no HTML: a model writing <US12> or <percorso> in its prose is shown as written
            markdown.setMarkdown(turn.text, QTextDocument.MarkdownFeature.MarkdownDialectGitHub
                                 | QTextDocument.MarkdownFeature.MarkdownNoHTML)
            cursor = self._end()
            cursor.insertBlock()
            cursor.insertFragment(QTextDocumentFragment(markdown))
            links = []
            label = html.escape(tr("Apri nell'editor"))
            for script in scripts_of(turn.text):
                self.scripts.append(script)
                links.append(f'<a href="script:{len(self.scripts) - 1}">{label}</a>')
            if links:
                self._paragraph("<p>" + " · ".join(links) + "</p>")
        if turn.cancelled:
            self._paragraph(f"<p><i>{html.escape(tr('(risposta fermata)'))}</i></p>")
        if turn.error:
            self._error_line(turn.error)
        self._set_busy(False)

    @Slot(str)
    def _on_failed(self, text: str) -> None:
        stale, self._asking = self._asking is not self.assistant, None
        if stale:
            self._set_busy(False)
            return
        self._drop_stream()
        self._write_action_lines()
        self._error_line(text)
        self._set_busy(False)

    def _write_action_lines(self) -> None:
        """What the actions of the question did, in the chat (written once the streamed text is gone)."""
        lines, self._action_lines = self._action_lines, []
        for line in lines:
            self._paragraph(f"<p><i>{html.escape(line)}</i></p>")

    def _link(self, url: QUrl) -> None:
        if url.scheme() == "script":
            try:
                self.open_in_editor(self.scripts[int(url.path())])
            except (ValueError, IndexError):
                pass

    def new_conversation(self) -> None:
        if self._busy:
            return
        if self.assistant is not None:
            self.assistant.new_conversation()
        self._clear_chat()

    def _clear_chat(self, line: str = "") -> None:
        """The chat emptied (and what belongs to it), with ``line`` saying why."""
        self._stop_action()  # an action of the old conversation waiting for Esegui: stopped
        for card in self.cards:
            card.setParent(None)
            card.deleteLater()
        self.cards = []
        self._action_lines = []
        self.cards_area.hide()
        self.chat.clear()
        self.scripts = []
        self._stream_at = None
        self._started = False
        self._warned = False
        self._skip_note = False
        if line:
            self._paragraph(f"<p><i>{html.escape(line)}</i></p>")

    def show_sent(self) -> None:
        SentDialog(self, self.assistant).exec()

    # ------------------------------------------------------------------ scripts
    def open_in_editor(self, script: str) -> None:
        self.editor.setPlainText(script)
        self.editor.setFocus()

    def run_editor(self) -> None:
        """*Esegui*: the editor's script in the worker; one entry of Modifica > Annulla."""
        source = self.editor.toPlainText()
        if not source.strip():
            return
        self._script_source = source
        self.output.setPlainText(tr("# esecuzione {0}\n", time.strftime("%H:%M:%S")))
        self.b_ask_error.hide()
        if not self._main.run_script_source(source, tr("Script dell'assistente"), on_done=self._script_done):
            self.output.appendPlainText(tr("Un'elaborazione è in corso: lo script non parte"))
            return
        self._script_running = True
        self.b_run.setEnabled(False)
        self.b_stop_script.setEnabled(True)

    def _script_done(self, result) -> None:
        self._script_running = False
        self.last_result = result
        self.b_run.setEnabled(True)
        self.b_stop_script.setEnabled(False)
        if result.output:
            self.output.appendPlainText(result.output.rstrip("\n"))
        if result.error:
            self.output.appendPlainText((result.traceback or result.error).rstrip("\n"))
            self.b_ask_error.show()
        self.update_undo()

    def update_undo(self) -> None:
        """*Annulla lo script* is on when the last entry of Modifica > Annulla is a script."""
        from .undo import ScriptAction

        h = getattr(self._main, "history", None)
        ok = bool(h is not None and h.can_undo() and isinstance(h.entries[h.index - 1].action, ScriptAction))
        self.b_undo.setEnabled(ok and not self._script_running)

    def undo_script(self) -> None:
        self.update_undo()
        if self.b_undo.isEnabled():
            self._main.undo()
        self.update_undo()

    def save_script(self) -> None:
        start = str(Path(self._main._last_dir()) / "script.py")
        path, _ = QFileDialog.getSaveFileName(self, tr("Salva come .py"), start, "Python (*.py)")
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() != ".py":
            target = target.with_name(target.name + ".py")
        target.write_text(self.editor.toPlainText(), encoding="utf-8")
        self.output.appendPlainText(tr("Salvato in {0}", target))


def _top(history):
    """The last entry of Modifica > Annulla that can be undone, None when there is none."""
    if history is None or not history.can_undo():
        return None
    return history.entries[history.index - 1]


def _weak(obj):
    try:
        return weakref.ref(obj)
    except TypeError:
        return lambda: obj


class SentDialog(QDialog):
    """*Cosa viene inviato*: every text that left the computer in this conversation, turn by turn, and the names
    the filter replaced (real -> placeholder)."""

    def __init__(self, parent, assistant: Assistant | None):
        super().__init__(parent)
        self.setWindowTitle(tr("Cosa viene inviato"))
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        external = assistant is not None and assistant.external
        lay.addWidget(QLabel(tr("Testo uscito dal computer, esattamente come è partito:") if external else
                             tr("Modello locale: niente lascia il computer. Qui il testo dato al modello.")))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(_Editor().font())
        lines: list[str] = []
        n = 0
        for turn in (assistant.history if assistant is not None else []):
            if turn.role == "user":
                n += 1
                lines.append(tr("=== Domanda {0}: {1}", n, turn.text))
                continue
            for entry in turn.sent:
                lines.append(entry)
                lines.append("---")
        self.log.setPlainText("\n".join(lines) if lines else tr("Niente ancora."))
        lay.addWidget(self.log, 3)
        lay.addWidget(QLabel(tr("Nomi sostituiti dal filtro")))
        mapping = assistant.filter.mapping if assistant is not None else {}
        self.table = QTableWidget(len(mapping), 2)
        self.table.setHorizontalHeaderLabels([tr("Nome vero"), tr("Inviato come")])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, (real, placeholder) in enumerate(sorted(mapping.items())):
            self.table.setItem(row, 0, QTableWidgetItem(real))
            self.table.setItem(row, 1, QTableWidgetItem(placeholder))
        lay.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


__all__ = ["AssistantPane", "AssistantWorker", "SentDialog", "scripts_of"]
