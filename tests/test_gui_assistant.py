"""The Assistente pane: a conversation with the model chosen (in a thread of its own, the pane always usable), what
leaves the computer shown, a script editor whose scripts run in the worker and are undone as one step, and the
settings of the assistant. The providers are fakes in this process: the tests never reach the network."""
from __future__ import annotations

import http.server
import os
import shutil
import threading
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from pyarchinit_photogrammetry.assistant import keys, privacy  # noqa: E402
from pyarchinit_photogrammetry.assistant.providers import Message, ProviderError, Reply  # noqa: E402


class FakeProvider:
    """Scripted replies, in order (the last one repeated); ``calls`` keeps the messages of every ``complete``."""

    def __init__(self, replies, kind="local"):
        self.kind, self.model, self.label = kind, "finto", f"Finto · {kind}"
        self.replies = list(replies)
        self.calls: list[list[Message]] = []

    def complete(self, messages, tools=None, on_text=None, cancel=None):
        self.calls.append(list(messages))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        if on_text is not None and reply.text:
            on_text(reply.text[:4])
            on_text(reply.text[4:])
        return reply

    def list_models(self):
        return ["finto"]

    def check(self):
        return "ok"


def text(t):
    return Reply(t, [], "end", {})


@pytest.fixture(autouse=True)
def _memory_keyring(monkeypatch):
    """Never the user's keychain."""
    store: dict = {}
    monkeypatch.setattr(keys, "_keyring_get", lambda service, user: store.get((service, user)))
    monkeypatch.setattr(keys, "_keyring_set", lambda service, user, key: store.__setitem__((service, user), key))
    monkeypatch.setattr(keys, "_keyring_delete", lambda service, user: store.pop((service, user), None))
    monkeypatch.setattr(keys, "keyring_available", lambda: True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return store


@pytest.fixture
def window(processed, tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import pyarchinit_photogrammetry as ppg

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    w = mw.MainWindow(ppg.Document.open(d / doc.path.name), autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w._script_message = lambda text: None
    yield w, app
    w.runner.wait()
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def _wait(app, pane, timeout=20.0):
    t0 = time.monotonic()
    while pane.busy and time.monotonic() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert not pane.busy, "the question did not end"


def _run(w, app, pane, source):
    pane.editor.setPlainText(source)
    pane.b_run.click()
    w.runner.wait()
    app.processEvents()


def test_pane_is_in_the_bottom_area_and_asks(window):
    from PySide6.QtCore import Qt, QUrl

    w, app = window
    pane = w.assistant
    assert w.dockWidgetArea(w.panes.dock(pane)) == Qt.DockWidgetArea.BottomDockWidgetArea
    assert w.panes.dock(pane).objectName() == "pane_assistant"
    answer = "Il DEM ha **100 x 80** celle.\n\n```python\nprint(chunk.label)\n```\n"
    fake = FakeProvider([text(answer)])
    pane.set_provider(fake)
    assert "locale" in pane.dot.text() and pane.dot.property("colour") == "green"
    assert pane.warning.isHidden()  # nothing leaves the computer
    pane.ask("quanto è grande il DEM?")
    assert pane.busy and pane.b_stop.isEnabled() and not pane.b_send.isEnabled()
    _wait(app, pane)
    shown = pane.chat.toPlainText()
    assert "quanto è grande il DEM?" in shown and "100 x 80" in shown and "**" not in shown  # markdown rendered
    assert "Apri nell'editor" in shown
    assert pane.b_send.isEnabled() and not pane.b_stop.isEnabled()
    assert fake.calls and fake.calls[0][-1].content.startswith("quanto è grande il DEM?")
    pane.chat.anchorClicked.emit(QUrl("script:0"))  # the button under the code block
    assert pane.editor.toPlainText() == "print(chunk.label)"
    pane.b_new.click()
    assert pane.chat.toPlainText().strip() == "" and pane.assistant.history == []


def test_run_from_editor_goes_through_the_worker_and_undo(window):
    w, app = window
    pane = w.assistant
    assert not pane.b_undo.isEnabled()
    _run(w, app, pane, "chunk.meta['x'] = 1\nprint('fatto')")
    assert "fatto" in pane.output.toPlainText()
    assert w.chunk.meta.get("x") == 1 and pane.last_result is not None and pane.last_result.error is None
    assert pane.b_ask_error.isHidden()
    assert pane.b_undo.isEnabled()
    pane.b_undo.click()
    app.processEvents()
    assert "x" not in w.chunk.meta
    assert not pane.b_undo.isEnabled()


def test_a_failed_question_is_shown_and_the_pane_stays_usable(window):
    w, app = window
    pane = w.assistant
    fake = FakeProvider([ProviderError("127.0.0.1:9 non risponde"), text("Eccomi.")])
    pane.set_provider(fake)
    pane.ask("ci sei?")
    _wait(app, pane)
    assert "127.0.0.1:9 non risponde" in pane.chat.toPlainText()
    assert pane.b_send.isEnabled() and not pane.b_stop.isEnabled() and pane.question.isEnabled()
    pane.ask("e adesso?")
    _wait(app, pane)
    assert "Eccomi." in pane.chat.toPlainText()


def test_stop_ends_a_question(window):
    w, app = window
    pane = w.assistant
    release = threading.Event()

    class Slow(FakeProvider):
        def complete(self, messages, tools=None, on_text=None, cancel=None):
            while not (cancel and cancel()):
                time.sleep(0.02)
            release.set()
            return text("")

    pane.set_provider(Slow([text("")]))
    pane.ask("lenta")
    pane.b_stop.click()
    _wait(app, pane)
    assert release.is_set() and pane.b_send.isEnabled()


def test_external_shows_warning_and_sent_log(window):
    from pyarchinit_photogrammetry.gui.assistant_pane import SentDialog

    w, app = window
    pane = w.assistant
    pane.set_provider(FakeProvider([text("Forse un muro.")], kind="claude"))
    assert "esterno" in pane.dot.text() and pane.dot.property("colour") == "orange"
    assert not pane.warning.isHidden() and pane.warning.text() == privacy.WARNING
    question = "perché a 4512345, 2314567 il DEM è alto?"
    pane.ask(question)
    _wait(app, pane)
    shown = pane.chat.toPlainText()
    assert privacy.WARNING in shown and shown.index(privacy.WARNING) < shown.index(question)
    dialog = SentDialog(pane, pane.assistant)
    log = dialog.log.toPlainText()
    assert privacy.WARNING in log and question in log  # the question as typed: what left
    assert dialog.table.rowCount() == len(pane.assistant.filter.mapping)
    dialog.close()
    pane.ask("e il muro?")
    _wait(app, pane)
    assert pane.chat.toPlainText().count(privacy.WARNING) == 1  # once per conversation
    pane.set_provider(FakeProvider([text("ok")], kind="local"))
    assert pane.warning.isHidden() and "locale" in pane.dot.text()


def test_settings_dialog_round_trip(window, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from pyarchinit_photogrammetry.gui import app_settings
    from pyarchinit_photogrammetry.gui import main_window as mwmod
    from pyarchinit_photogrammetry.gui.assistant_settings import SETTINGS, AssistantSettingsDialog

    w, app = window
    s = app_settings()
    dialog = AssistantSettingsDialog(w, s)
    dialog.r_claude.setChecked(True)
    dialog.key_claude.setText("sk-ant-segretissima")  # typed, not saved in the keychain: never in the settings
    dialog.model_claude.setCurrentText("claude-opus-5")
    dialog.r_local.setChecked(True)
    dialog.base_url.setText("http://127.0.0.1:1234/v1")
    dialog.model_local.setCurrentText("qwen3-coder")
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    s.sync()
    assert s.value(SETTINGS["kind"]) == "local"
    assert s.value(SETTINGS["base_url"]) == "http://127.0.0.1:1234/v1"
    assert s.value(SETTINGS["model_local"]) == "qwen3-coder"
    assert s.value(SETTINGS["model_claude"]) == "claude-opus-5"
    assert "segretissima" not in open(s.fileName(), encoding="utf-8").read()
    shown = []
    monkeypatch.setattr(mwmod, "message", lambda *a, **k: shown.append(a))
    again = AssistantSettingsDialog(w, s)
    assert again.r_local.isChecked() and again.base_url.text() == "http://127.0.0.1:1234/v1"
    assert again.model_claude.currentText() == "claude-opus-5"
    again.base_url.setText("http://192.168.1.20:1234/v1")
    again.accept()
    assert shown and "192.168.1.20" in shown[0][3]
    assert again.result() != QDialog.DialogCode.Accepted
    assert s.value(SETTINGS["base_url"]) == "http://127.0.0.1:1234/v1"
    again.reject()
    # the score of the last evaluation: under 2 the kind is "limited for scripts"
    s.setValue(SETTINGS["score"].format(kind="local"), 1.5)
    third = AssistantSettingsDialog(w, s)
    assert "limitato per gli script" in third.scores.text()
    third.reject()


def test_key_saved_in_the_keychain_and_forgotten(window, _memory_keyring):
    from pyarchinit_photogrammetry.gui import app_settings
    from pyarchinit_photogrammetry.gui.assistant_settings import AssistantSettingsDialog

    w, app = window
    dialog = AssistantSettingsDialog(w, app_settings())
    dialog.key_claude.setText("sk-ant-prova")
    dialog.b_save_claude.click()
    assert keys.get_key("claude") == "sk-ant-prova" and dialog.key_claude.text() == ""
    dialog.b_forget_claude.click()
    assert keys.get_key("claude") is None
    dialog.reject()


class _Models(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b'{"data": [{"id": "qwen3-coder"}, {"id": "gemma"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_provider_from_settings(window, _memory_keyring):
    from pyarchinit_photogrammetry.assistant import providers
    from pyarchinit_photogrammetry.gui import app_settings
    from pyarchinit_photogrammetry.gui.assistant_settings import SETTINGS, provider_from_settings

    w, app = window
    s = app_settings()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Models)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        s.setValue(SETTINGS["kind"], "local")
        s.setValue(SETTINGS["base_url"], url)
        s.setValue(SETTINGS["model_local"], "")
        p = provider_from_settings(s)
        assert p is not None and p.kind == "local" and p.base_url == url and p.model == "qwen3-coder"
        s.setValue(SETTINGS["model_local"], "gemma")
        assert provider_from_settings(s).model == "gemma"
    finally:
        server.shutdown()
        server.server_close()
    s.setValue(SETTINGS["base_url"], "http://192.168.1.20:1234/v1")  # never off this computer
    original = providers.LOCAL_SERVERS
    providers.LOCAL_SERVERS = {}
    try:
        assert provider_from_settings(s) is None
    finally:
        providers.LOCAL_SERVERS = original
    s.setValue(SETTINGS["kind"], "claude")
    assert provider_from_settings(s) is None  # no key
    s.setValue(SETTINGS["kind"], "openai")
    assert provider_from_settings(s) is None


def test_error_button_asks_about_the_traceback(window):
    w, app = window
    pane = w.assistant
    _run(w, app, pane, "x = 1 / 0")
    assert pane.last_result.error and "ZeroDivisionError" in pane.output.toPlainText()
    assert not pane.b_ask_error.isHidden()
    fake = FakeProvider([text("Si divide per zero.\n\n```python\nx = 1 / 1\n```")])
    pane.set_provider(fake)
    pane.b_ask_error.click()
    _wait(app, pane)
    sent = fake.calls[0][-1].content
    assert "ZeroDivisionError" in sent and "x = 1 / 0" in sent
    assert "Si divide per zero." in pane.chat.toPlainText()


def test_save_as_py(window, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.gui import assistant_pane

    w, app = window
    pane = w.assistant
    target = tmp_path / "mio_script"
    monkeypatch.setattr(assistant_pane.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "Python (*.py)")))
    pane.editor.setPlainText("print('ciao')\n")
    pane.b_save.click()
    assert (tmp_path / "mio_script.py").read_text(encoding="utf-8") == "print('ciao')\n"


def test_editor_tab_is_four_spaces(window):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    w, app = window
    pane = w.assistant
    pane.editor.setPlainText("")
    QApplication.sendEvent(pane.editor, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Tab,
                                                  Qt.KeyboardModifier.NoModifier, "\t"))
    assert pane.editor.toPlainText() == "    "


# ---------------------------------------------------------------- fix round 1


def test_angle_brackets_in_an_answer_stay_whole(window):
    w, app = window
    pane = w.assistant
    pane.set_provider(FakeProvider([text("La quota <US12> e il muro <img src=x onerror=alert(1)> fine riga")]))
    pane.ask("dove?")
    _wait(app, pane)
    shown = pane.chat.toPlainText()
    assert "La quota <US12> e il muro <img src=x onerror=alert(1)> fine riga" in shown


def test_a_new_model_starts_a_new_conversation(window):
    from pyarchinit_photogrammetry.gui import app_settings
    from pyarchinit_photogrammetry.gui.assistant_pane import SentDialog
    from pyarchinit_photogrammetry.gui.assistant_settings import SETTINGS

    w, app = window
    pane = w.assistant
    s = app_settings()
    s.setValue(SETTINGS["kind"], "claude")
    s.setValue(SETTINGS["model_claude"], "finto")
    first = FakeProvider([text("Primo.")], kind="claude")
    pane.set_provider(first)
    pane.ask("prima domanda")
    _wait(app, pane)
    kept = pane.assistant
    pane.reload()  # the settings did not change (OK without changes): everything stays
    assert pane.assistant is kept and "prima domanda" in pane.chat.toPlainText()
    assert "prima domanda" in SentDialog(pane, pane.assistant).log.toPlainText()
    s.setValue(SETTINGS["model_claude"], "claude-opus-5")  # another model: a new conversation
    pane._find_provider = lambda: None  # no resolution in this test: the provider is given below
    pane.reload()
    shown = pane.chat.toPlainText()
    assert "prima domanda" not in shown and "nuova conversazione" in shown.lower()
    pane.set_provider(FakeProvider([text("Secondo.")], kind="claude"))
    assert "Niente ancora." in SentDialog(pane, pane.assistant).log.toPlainText()  # consistent with the chat
    pane.ask("seconda")
    _wait(app, pane)
    assert pane.chat.toPlainText().count(privacy.WARNING) == 1


def test_provider_given_while_talking_clears_the_chat(window):
    w, app = window
    pane = w.assistant
    pane.set_provider(FakeProvider([text("Uno.")], kind="local"))
    pane.ask("uno?")
    _wait(app, pane)
    other = FakeProvider([text("Due.")], kind="local")
    other.label = "Altro modello"
    pane.set_provider(other)
    shown = pane.chat.toPlainText()
    assert "uno?" not in shown and "Altro modello" in shown and pane.scripts == []


def test_new_conversation_is_off_while_a_question_runs(window):
    w, app = window
    pane = w.assistant

    class Slow(FakeProvider):
        def complete(self, messages, tools=None, on_text=None, cancel=None):
            while not (cancel and cancel()):
                time.sleep(0.02)
            return text("")

    pane.set_provider(Slow([text("")]))
    pane.ask("lenta")
    assert not pane.b_new.isEnabled()
    pane.stop()
    _wait(app, pane)
    assert pane.b_new.isEnabled()


def test_project_switch_clears_the_chat_and_warns_again(window, monkeypatch):
    import pyarchinit_photogrammetry as ppg

    w, app = window
    pane = w.assistant
    pane.set_provider(FakeProvider([text("Risposta.")], kind="claude"))
    pane.ask("prima")
    _wait(app, pane)
    from pyarchinit_photogrammetry.gui import main_window as mwmod

    monkeypatch.setattr(mwmod.application, "document", ppg.Document())  # another project in the window
    pane.ask("dopo")
    _wait(app, pane)
    shown = pane.chat.toPlainText()
    assert "prima" not in shown.split(privacy.WARNING)[-1] and "dopo" in shown
    assert shown.count(privacy.WARNING) == 1 and shown.index(privacy.WARNING) < shown.index("dopo")
    assert "progetto è cambiato" in shown.lower()
    assert shown.lower().count("progetto è cambiato") == 1  # said once, not again by the answer's note
    monkeypatch.undo()


@pytest.mark.parametrize("kind, setup, words", [
    ("claude", {}, "chiave di Claude"),
    ("openai", {"key": "sk-x"}, "modello OpenAI"),
    ("local", {}, "server locale"),
])
def test_the_missing_model_says_why(window, _memory_keyring, monkeypatch, kind, setup, words):
    from pyarchinit_photogrammetry.assistant import providers
    from pyarchinit_photogrammetry.gui import app_settings
    from pyarchinit_photogrammetry.gui.assistant_settings import SETTINGS

    w, app = window
    monkeypatch.setattr(providers, "LOCAL_SERVERS", {})
    s = app_settings()
    s.setValue(SETTINGS["kind"], kind)
    s.setValue(SETTINGS["base_url"], "")
    s.setValue(SETTINGS["model_openai"], "")
    if "key" in setup:
        _memory_keyring[(keys.SERVICE, kind)] = setup["key"]
    pane = w.assistant
    pane.reload()
    pane.ask("ci sei?")
    _wait(app, pane)
    assert words in pane.chat.toPlainText() and pane.b_send.isEnabled()


def test_keychain_errors_are_translated(window, monkeypatch):
    from pyarchinit_photogrammetry.gui import app_settings, i18n
    from pyarchinit_photogrammetry.gui import main_window as mwmod
    from pyarchinit_photogrammetry.gui.assistant_settings import AssistantSettingsDialog

    w, app = window
    monkeypatch.setattr(keys, "keyring_available", lambda: False)
    shown = []
    monkeypatch.setattr(mwmod, "message", lambda *a, **k: shown.append(a[3]))
    i18n.set_current("en")
    try:
        dialog = AssistantSettingsDialog(w, app_settings())
        dialog.key_claude.setText("sk-ant-x")
        dialog.b_save_claude.click()
        dialog.reject()
    finally:
        i18n.set_current("it")
    assert shown and "keychain" in shown[0] and "portachiavi" not in shown[0]


def test_settings_dialog_closes_at_once_with_a_slow_call(window):
    from pyarchinit_photogrammetry.gui import app_settings
    from pyarchinit_photogrammetry.gui.assistant_settings import AssistantSettingsDialog

    w, app = window
    dialog = AssistantSettingsDialog(w, app_settings())
    gate = threading.Event()
    dialog.background.start(lambda: gate.wait(10), lambda r: None, lambda e: None)
    t0 = time.monotonic()
    dialog.reject()
    assert time.monotonic() - t0 < 1.0
    gate.set()


_EXIT_SCRIPT = r"""
import os, sys, time
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
app = QApplication([])
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, sys.argv[1])
from pyarchinit_photogrammetry.gui import assistant_pane as ap
from pyarchinit_photogrammetry.gui import assistant_settings as st
from pyarchinit_photogrammetry.assistant.providers import Reply

class Hung:  # never looks at cancel
    kind, model, label = "local", "m", "hung"
    def complete(self, messages, tools=None, on_text=None, cancel=None):
        time.sleep(30)
        return Reply("", [], "end", {})
    def list_models(self): return ["m"]
    def check(self): return "ok"

class Window:
    doc = None
    history = None

st.EXIT_WAIT_MS = 200
ap.SHUTDOWN_MS = 200
pane = ap.AssistantPane(Window())
pane.set_provider(Hung())
pane.ask("x")
t0 = time.monotonic()
while time.monotonic() - t0 < 1.0:
    app.processEvents()
    time.sleep(0.01)
if sys.argv[2] == "shutdown":
    pane.shutdown()
del pane
print("exiting", flush=True)
sys.exit(0)
"""


@pytest.mark.parametrize("how", ["shutdown", "atexit"])
def test_the_program_exits_with_a_hung_model(tmp_path, how):
    import subprocess
    import sys

    script = tmp_path / "hung.py"
    script.write_text(_EXIT_SCRIPT, encoding="utf-8")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PPG_RENDERER="software")
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, str(script), str(tmp_path / "settings"), how], env=env,
                       capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "exiting" in r.stdout and "Destroyed while thread" not in r.stderr
    assert time.monotonic() - t0 < 20
