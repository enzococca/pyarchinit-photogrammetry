"""The settings of the assistant: which model answers (a local server on this computer, Claude or OpenAI), the
models of each, the keys (only in the system keychain or the environment, never in the settings) and a check of
the connection. :func:`provider_from_settings` turns the saved settings into a provider."""
from __future__ import annotations

import atexit
import logging
import os
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QVBoxLayout)

from ..assistant import keys
from ..assistant.providers import (CLAUDE_CHOICES, DEFAULT_CLAUDE, OPENAI_URL, OpenAICompatible, Provider,
                                   ProviderError, is_loopback, make_provider, probe_local)
from .i18n import current, number, tr

log = logging.getLogger(__name__)

SETTINGS = {"kind": "assistant/kind", "base_url": "assistant/base_url", "model_local": "assistant/model_local",
            "model_claude": "assistant/model_claude", "model_openai": "assistant/model_openai",
            "score": "assistant/score_{kind}", "context_chars": "assistant/context_chars"}
KINDS = ("local", "claude", "openai")
KIND_NAMES = {"local": "Locale", "claude": "Claude", "openai": "OpenAI"}
SCRIPT_SCORE = 2.0  # under this score of the last evaluation a kind is "limited for scripts"
LOCAL_CHECK_TIMEOUT = 1.0  # seconds: does the saved local server answer?
GUIDE_URL = {"it": "https://enzococca.github.io/pyarchinit-photogrammetry/it/gui/assistant/",
             "en": "https://enzococca.github.io/pyarchinit-photogrammetry/gui/assistant/"}


def _text(settings, key: str, default: str = "") -> str:
    value = settings.value(SETTINGS[key], default)
    return str(value).strip() if value is not None else default


def saved_kind(settings) -> str:
    kind = _text(settings, "kind", "local")
    return kind if kind in KINDS else "local"


def saved_model(settings, kind: str) -> str:
    return _text(settings, f"model_{kind}", DEFAULT_CLAUDE if kind == "claude" else "")


def score_of(settings, kind: str) -> float | None:
    value = settings.value(SETTINGS["score"].format(kind=kind))
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def context_chars_of(settings) -> int | None:
    """The budget in characters of one request to a local model (``assistant/context_chars``), None for the default
    of the session (80 000)."""
    try:
        value = int(float(settings.value(SETTINGS["context_chars"]) or 0))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def provider_from_settings(settings, why: list[str] | None = None) -> Provider | None:
    """The provider of the saved settings, or None when the kind needs a key that is missing (Claude, OpenAI), no
    OpenAI model was chosen, or no local server answers (the saved address first, then the usual ports). ``why``,
    when given, receives the reason of a None, in the words of the interface."""

    def none(reason: str) -> None:
        if why is not None:
            why.append(reason)
        return None

    kind = saved_kind(settings)
    model = saved_model(settings, kind)
    try:
        if kind == "local":
            base = _text(settings, "base_url")
            if base and is_loopback(base):
                try:
                    models = OpenAICompatible(base, "", timeout=LOCAL_CHECK_TIMEOUT, kind="local").list_models()
                except ProviderError as exc:
                    log.info("assistente: %s", exc)
                else:
                    return OpenAICompatible(base, model or (models[0] if models else ""), kind="local")
            found = probe_local()
            if not found:
                return none(tr("Nessun server locale risponde (LM Studio, llama.cpp, Ollama, Unsloth): avvialo con un "
                               "modello caricato, o scegli Claude o OpenAI in Impostazioni..."))
            _, url, models = found[0]
            chosen = model if (model and (model in models or not models)) else (models[0] if models else "")
            return OpenAICompatible(url, chosen, kind="local")
        if not keys.get_key(kind):
            if kind == "claude":
                return none(tr("Manca la chiave di Claude: salvala in Impostazioni... o usa la variabile d'ambiente "
                               "ANTHROPIC_API_KEY"))
            return none(tr("Manca la chiave di OpenAI: salvala in Impostazioni... o usa la variabile d'ambiente "
                           "OPENAI_API_KEY"))
        if kind == "openai":
            if not model:
                return none(tr("Nessun modello OpenAI scelto: sceglilo in Impostazioni... (Aggiorna i modelli)"))
            return make_provider("openai", model, OPENAI_URL)
        return make_provider("claude", model or DEFAULT_CLAUDE)
    except ProviderError as exc:  # the library missing, an address refused
        log.info("assistente: %s", exc)
        return none(str(exc))


def same_provider(settings, provider) -> bool:
    """True when ``provider`` is the one the saved settings name (kind, model, address): nothing to change."""
    if provider is None:
        return False
    kind = saved_kind(settings)
    if getattr(provider, "kind", None) != kind:
        return False
    model = saved_model(settings, kind)
    if model and getattr(provider, "model", None) != model:
        return False
    base = _text(settings, "base_url").rstrip("/")
    return not (kind == "local" and base and getattr(provider, "base_url", base) != base)


# ---------------------------------------------------------------- threads that must never be destroyed running

EXIT_WAIT_MS = 2000  # at exit, how long a thread still working is waited for
_LIVE: dict = {}  # QThread -> the objects it needs (worker...): every thread of the assistant until it ends
_PARKED: list = []


def keep_thread(thread: QThread, *objects) -> None:
    """``thread`` and ``objects`` stay referenced until :func:`release_thread` (or the end of the program)."""
    _LIVE[thread] = objects


def release_thread(thread: QThread) -> None:
    _LIVE.pop(thread, None)


def park(thread: QThread, *objects) -> None:
    """A thread that did not stop (a call that ignores cancel): never destroyed while it runs. It becomes a child
    of the main thread's QThread object, which Qt never deletes before the process ends: so neither the garbage
    collector nor PySide's clean-up at exit (which deletes every QObject owned by Python) destroys it - that would
    abort the program ("QThread: Destroyed while thread is still running") - and the process ending stops it."""
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance()
    try:
        if app is not None and thread.thread() is app.thread():
            thread.setParent(app.thread())
    except RuntimeError:  # already gone
        log.debug("park", exc_info=True)
    _PARKED.append((thread, objects))
    _LIVE.pop(thread, None)


@atexit.register
def _finish_threads() -> None:
    """At exit: every thread of the assistant asked to stop and waited for a moment; one still running is parked."""
    for thread, objects in list(_LIVE.items()):
        try:
            for obj in objects:
                cancel = getattr(obj, "cancel", None)
                if callable(cancel):
                    cancel()
            thread.quit()
            if not thread.wait(EXIT_WAIT_MS):
                park(thread, *objects)
        except RuntimeError:  # already deleted by Qt
            pass
    _LIVE.clear()


class _Job(QObject):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn

    @Slot()
    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # noqa: BLE001 - said in the dialog
            self.failed.emit(str(exc) or type(exc).__name__)
            return
        self.done.emit(result)


class _Relay(QObject):
    """Lives in the interface thread: the results of a job reach the dialog through its slots (queued)."""

    def __init__(self, owner: "Background", on_done, on_error):
        super().__init__()
        self.owner, self.on_done, self.on_error = owner, on_done, on_error

    @Slot(object)
    def ok(self, result) -> None:
        if self.on_done is not None:
            self.on_done(result)

    @Slot(str)
    def ko(self, text: str) -> None:
        if self.on_error is not None:
            self.on_error(text)

    @Slot()
    def end(self) -> None:
        owner, self.owner = self.owner, None
        if owner is not None:
            owner._forget(self)


class Background(QObject):
    """Short calls (list the models, try the connection, find the servers) in threads of their own; the callbacks
    run in the interface thread. :meth:`drop` when the owner goes away: the calls still running are not waited for,
    their results are dropped and their threads end by themselves (kept referenced until then)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs: dict[_Relay, QThread] = {}

    def start(self, fn: Callable[[], Any], on_done=None, on_error=None) -> None:
        thread, job = QThread(), _Job(fn)
        relay = _Relay(self, on_done, on_error)
        job.moveToThread(thread)
        job.done.connect(relay.ok)
        job.failed.connect(relay.ko)
        job.done.connect(thread.quit)
        job.failed.connect(thread.quit)
        thread.finished.connect(relay.end)
        thread.started.connect(job.run)
        self.jobs[relay] = thread
        keep_thread(thread, job, relay)
        thread.start()

    def _forget(self, relay: _Relay) -> None:
        thread = self.jobs.pop(relay, None) or next((t for t, o in _LIVE.items() if relay in o), None)
        relay.on_done = relay.on_error = None
        if thread is not None:
            thread.wait()  # finished: returns at once
            release_thread(thread)

    def drop(self) -> None:
        """The results of the calls still running are dropped; nothing is waited for."""
        for relay, thread in list(self.jobs.items()):
            relay.on_done = relay.on_error = None
            thread.quit()
        self.jobs.clear()  # the threads stay in _LIVE until they end (relay.end) or the program does


# ---------------------------------------------------------------- the dialog


class AssistantSettingsDialog(QDialog):
    """*Strumenti > Assistente > Impostazioni dell'assistente...*: the kind of model, its address or key, the models;
    OK writes the settings (the keys go to the keychain, never to the settings)."""

    def __init__(self, parent, settings):
        super().__init__(parent)
        self.settings = settings
        self.background = Background(self)
        self.setWindowTitle(tr("Impostazioni dell'assistente"))
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Chi risponde alle domande:")))
        self.r_local = QRadioButton(tr("Locale (su questo computer: niente lascia il computer)"))
        self.r_claude = QRadioButton(tr("Claude (esterno: passa solo ciò che il filtro lascia passare)"))
        self.r_openai = QRadioButton(tr("OpenAI (esterno: passa solo ciò che il filtro lascia passare)"))
        self.kinds = QButtonGroup(self)
        for k, r in zip(KINDS, (self.r_local, self.r_claude, self.r_openai)):
            self.kinds.addButton(r)
            r.setProperty("kind", k)
            lay.addWidget(r)
        # local
        box = QGroupBox(tr("Modello locale"))
        form = QFormLayout(box)
        self.base_url = QLineEdit(_text(settings, "base_url"))
        self.base_url.setPlaceholderText("http://127.0.0.1:1234/v1")
        self.b_find = QPushButton(tr("Trova i server"))
        self.b_find.clicked.connect(self.find_servers)
        row = QHBoxLayout()
        row.addWidget(self.base_url, 1)
        row.addWidget(self.b_find)
        form.addRow(tr("Indirizzo (solo questo computer)"), row)
        self.model_local = self._combo([], saved_model(settings, "local"))
        self.b_models_local = QPushButton(tr("Aggiorna i modelli"))
        self.b_models_local.clicked.connect(lambda: self.refresh_models("local"))
        form.addRow(tr("Modello"), self._pair(self.model_local, self.b_models_local))
        lay.addWidget(box)
        # Claude
        box = QGroupBox("Claude")
        form = QFormLayout(box)
        self.model_claude = self._combo(list(CLAUDE_CHOICES), saved_model(settings, "claude"))
        self.b_models_claude = QPushButton(tr("Aggiorna i modelli"))
        self.b_models_claude.clicked.connect(lambda: self.refresh_models("claude"))
        form.addRow(tr("Modello"), self._pair(self.model_claude, self.b_models_claude))
        self.key_claude, self.b_save_claude, self.b_forget_claude, self.key_state_claude = self._key_row(form, "claude")
        lay.addWidget(box)
        # OpenAI
        box = QGroupBox("OpenAI")
        form = QFormLayout(box)
        self.model_openai = self._combo([], saved_model(settings, "openai"))
        self.b_models_openai = QPushButton(tr("Aggiorna i modelli"))
        self.b_models_openai.clicked.connect(lambda: self.refresh_models("openai"))
        form.addRow(tr("Modello"), self._pair(self.model_openai, self.b_models_openai))
        self.key_openai, self.b_save_openai, self.b_forget_openai, self.key_state_openai = self._key_row(form, "openai")
        lay.addWidget(box)
        # evaluation, check, guide
        self.scores = QLabel(self._scores_text())
        self.scores.setWordWrap(True)
        lay.addWidget(self.scores)
        self.b_check = QPushButton(tr("Prova la connessione"))
        self.b_check.clicked.connect(self.check)
        self.check_label = QLabel("")
        self.check_label.setWordWrap(True)
        lay.addLayout(self._pair(self.b_check, self.check_label, stretch_second=True))
        title = tr("La guida dell'assistente")
        guide = QLabel(f'<a href="{GUIDE_URL["it" if current() == "it" else "en"]}">{title}</a>')
        guide.setOpenExternalLinks(True)
        lay.addWidget(guide)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        {"local": self.r_local, "claude": self.r_claude, "openai": self.r_openai}[saved_kind(settings)].setChecked(True)
        if keys.get_key("claude"):  # the account's models beside the usual ones
            self.refresh_models("claude")
        if keys.get_key("openai"):
            self.refresh_models("openai")

    # ------------------------------------------------------------------ widgets
    @staticmethod
    def _combo(items: list[str], value: str) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(items)
        if value:
            combo.setCurrentText(value)
        return combo

    @staticmethod
    def _pair(first, second, stretch_second: bool = False) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(first, 0 if stretch_second else 1)
        row.addWidget(second, 1 if stretch_second else 0)
        return row

    def _key_row(self, form: QFormLayout, kind: str):
        field = QLineEdit()
        field.setEchoMode(QLineEdit.EchoMode.Password)
        field.setPlaceholderText(tr("Incolla la chiave"))
        save = QPushButton(tr("Salva nel portachiavi"))
        forget = QPushButton(tr("Dimentica"))
        save.clicked.connect(lambda: self.save_key(kind))
        forget.clicked.connect(lambda: self.forget_key(kind))
        row = QHBoxLayout()
        row.addWidget(field, 1)
        row.addWidget(save)
        row.addWidget(forget)
        form.addRow(tr("Chiave"), row)
        state = QLabel(self._key_state(kind))
        form.addRow("", state)
        return field, save, forget, state

    @staticmethod
    def _key_state(kind: str) -> str:
        if os.environ.get(keys.ENV[kind], "").strip():
            return tr("Chiave dalla variabile d'ambiente {0}", keys.ENV[kind])
        if keys.get_key(kind):
            return tr("Chiave nel portachiavi del sistema")
        return tr("Nessuna chiave")

    def _scores_text(self) -> str:
        parts = []
        for kind in KINDS:
            value = score_of(self.settings, kind)
            if value is None:
                shown = tr("non valutato")
            else:
                shown = number(value, 1)
                if value < SCRIPT_SCORE:
                    shown = tr("{0} (limitato per gli script)", shown)
            parts.append(tr("{0}: {1}", tr(KIND_NAMES[kind]), shown))
        return tr("Ultima valutazione: {0}", " · ".join(parts))

    # ------------------------------------------------------------------ state
    def kind(self) -> str:
        button = self.kinds.checkedButton()
        return button.property("kind") if button is not None else "local"

    def _model_box(self, kind: str) -> QComboBox:
        return {"local": self.model_local, "claude": self.model_claude, "openai": self.model_openai}[kind]

    def _typed_key(self, kind: str) -> str | None:
        field = self.key_claude if kind == "claude" else self.key_openai
        return field.text().strip() or None

    def _provider(self, kind: str) -> Provider:
        """A provider of what the dialog shows now (not yet saved)."""
        model = self._model_box(kind).currentText().strip()
        if kind == "local":
            url = self.base_url.text().strip()
            if url and not is_loopback(url):
                raise ProviderError(tr("{0} non è un indirizzo di questo computer", url))
            return make_provider("local", model, url)
        return make_provider(kind, model, OPENAI_URL if kind == "openai" else "", api_key=self._typed_key(kind))

    # ------------------------------------------------------------------ actions
    def find_servers(self) -> None:
        self.check_label.setText(tr("Cerco i server locali..."))

        def found(servers) -> None:
            if not servers:
                self.check_label.setText(tr("Nessun server locale risponde (LM Studio, llama.cpp, Ollama, Unsloth)"))
                return
            name, url, models = servers[0]
            self.base_url.setText(url)
            self._fill(self.model_local, models)
            self.check_label.setText(", ".join(f"{n}: {u}" for n, u, _m in servers))

        self.background.start(probe_local, found, self.check_label.setText)

    def _fill(self, combo: QComboBox, models: list[str], keep: tuple[str, ...] = ()) -> None:
        current_text = combo.currentText()
        combo.clear()
        combo.addItems(list(dict.fromkeys(list(keep) + list(models))))
        if current_text:
            combo.setCurrentText(current_text)
        elif combo.count():
            combo.setCurrentIndex(0)

    def refresh_models(self, kind: str) -> None:
        try:
            provider = self._provider(kind)
        except ProviderError as exc:
            self.check_label.setText(str(exc))
            return
        keep = tuple(CLAUDE_CHOICES) if kind == "claude" else ()
        self.background.start(provider.list_models, lambda models: self._fill(self._model_box(kind), models, keep),
                              self.check_label.setText)

    def check(self) -> None:
        kind = self.kind()
        try:
            provider = self._provider(kind)
        except ProviderError as exc:
            self.check_label.setText(str(exc))
            return
        self.check_label.setText(tr("Prova in corso..."))
        self.background.start(provider.check, lambda text: self.check_label.setText(f"{provider.label}: {text}"),
                              self.check_label.setText)

    def save_key(self, kind: str) -> bool:
        key = self._typed_key(kind)
        if not key:
            return True
        try:
            keys.set_key(kind, key)
        except ProviderError as exc:
            from . import main_window as mwmod

            if keys.keyring_available() and exc.__cause__ is not None:
                text = tr("Il portachiavi ha rifiutato la chiave: {0}", exc.__cause__)
            else:
                text = tr("Nessun portachiavi disponibile su questo computer: la chiave non può essere conservata "
                          "(usa la variabile d'ambiente {0})", keys.ENV[kind])
            mwmod.message(self, "warning", tr("Assistente"), text)
            return False
        (self.key_claude if kind == "claude" else self.key_openai).clear()
        (self.key_state_claude if kind == "claude" else self.key_state_openai).setText(self._key_state(kind))
        return True

    def forget_key(self, kind: str) -> None:
        keys.forget_key(kind)
        (self.key_state_claude if kind == "claude" else self.key_state_openai).setText(self._key_state(kind))

    def accept(self) -> None:
        url = self.base_url.text().strip()
        if url and not is_loopback(url):
            from . import main_window as mwmod

            mwmod.message(self, "warning", tr("Assistente"),
                          tr("{0} non è un indirizzo di questo computer: un server locale deve essere su 127.0.0.1, "
                             "::1 o localhost.", url))
            return
        if not (self.save_key("claude") and self.save_key("openai")):
            return
        s = self.settings
        s.setValue(SETTINGS["kind"], self.kind())
        s.setValue(SETTINGS["base_url"], url)
        for kind in KINDS:
            s.setValue(SETTINGS[f"model_{kind}"], self._model_box(kind).currentText().strip())
        s.sync()
        super().accept()

    def done(self, result: int) -> None:
        self.background.drop()  # never waits: a slow network does not hold the window
        super().done(result)


__all__ = ["SETTINGS", "KINDS", "KIND_NAMES", "context_chars_of", "provider_from_settings", "same_provider", "saved_kind", "saved_model",
           "score_of", "AssistantSettingsDialog", "Background", "keep_thread", "release_thread", "park"]
