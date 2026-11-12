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
                       capture_output=True, text=True, timeout=25,
                       close_fds=False)  # posix_spawn, never a fork of the process of the tests (see test_env)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "exiting" in r.stdout and "Destroyed while thread" not in r.stderr
    assert time.monotonic() - t0 < 20


# ---------------------------------------------------------------- actions


def _calls(name, args, t=""):
    from pyarchinit_photogrammetry.assistant.providers import ToolCall

    return Reply(t, [ToolCall("call_0", name, args)], "tool_calls", {})


def _until(app, condition, timeout=20.0):
    t0 = time.monotonic()
    while not condition() and time.monotonic() - t0 < timeout:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert condition(), "the condition did not come true"


def _tool_result(fake):
    import json

    return json.loads(next(m for m in fake.calls[-1] if m.role == "tool").content)


@pytest.fixture
def fake_actions(window, monkeypatch):
    """``stones`` and ``show`` replaced by fakes that record their calls in the thread they run in; ``stones``
    writes into the chunk (an entry of Modifica > Annulla)."""
    from pyarchinit_photogrammetry.assistant import actions

    w, app = window
    seen = []

    def stones(chunk, window, progress=None, **kw):
        seen.append(("stones", kw, threading.current_thread() is threading.main_thread()))
        chunk.meta["pietre_finte"] = chunk.meta.get("pietre_finte", 0) + 3  # a change every time
        return {"stones": 3, "layer": "Pietre", "chunk": chunk.label}

    def show(chunk, window, what="ortho", **kw):
        seen.append(("show", {"what": what, **kw}, threading.current_thread() is threading.main_thread()))
        return {"shown": what}

    monkeypatch.setitem(actions.ACTIONS, "stones", (stones, *actions.ACTIONS["stones"][1:]))
    monkeypatch.setitem(actions.ACTIONS, "show", (show, *actions.ACTIONS["show"][1:]))
    return seen


def _mode(pane, mode):
    pane._settings().setValue("assistant_actions", mode)


def test_a_card_waits_for_run_and_the_action_is_undone_in_one_step(window, fake_actions):
    w, app = window
    pane = w.assistant
    _mode(pane, "ask")
    fake = FakeProvider([_calls("act_stones", {"min_size": 0.1}), text("Ho trovato 3 pietre.")])
    pane.set_provider(fake)
    pane.ask("disegna le pietre")
    _until(app, lambda: pane.cards and pane.cards[-1].state == "waiting")
    card = pane.cards[-1]
    assert not fake_actions and pane.busy  # nothing ran: the card waits
    assert "Pietra più piccola" in card.text() and "0,1" in card.text() and "Area: tutto il raster" in card.text()
    card.b_run.click()
    _wait(app, pane)
    assert fake_actions == [("stones", {"min_size": 0.1}, False)]  # in the task runner, not in the interface
    assert card.state == "done" and "3" in card.result_label.text()
    result = _tool_result(fake)
    assert result["ran"] is True and result["stones"] == 3
    assert "Ho trovato 3 pietre." in pane.chat.toPlainText()
    assert w.chunk.meta["pietre_finte"] == 3
    assert w.history.undo_text().endswith("Pietre (SAM)")
    assert card.b_undo.isEnabled()
    card.b_undo.click()
    app.processEvents()
    assert "pietre_finte" not in w.chunk.meta and not card.b_undo.isEnabled()


def test_no_declines_and_stop_stops(window, fake_actions):
    w, app = window
    pane = w.assistant
    _mode(pane, "ask")
    fake = FakeProvider([_calls("act_stones", {}), text("Va bene, non le disegno.")])
    pane.set_provider(fake)
    pane.ask("disegna le pietre")
    _until(app, lambda: pane.cards and pane.cards[-1].state == "waiting")
    pane.cards[-1].b_no.click()
    _wait(app, pane)
    assert _tool_result(fake) == {"declined": True} and not fake_actions
    assert pane.cards[-1].state == "declined" and "non le disegno" in pane.chat.toPlainText()
    fake = FakeProvider([_calls("act_stones", {}), text("Fatto!")])
    pane.set_provider(fake)
    pane.ask("disegna le pietre")
    _until(app, lambda: pane.cards and pane.cards[-1].state == "waiting")
    pane.b_stop.click()
    _wait(app, pane)
    assert not fake_actions and pane.cards[-1].state == "stopped" and len(fake.calls) == 1
    shown = pane.chat.toPlainText()
    assert "Fatto!" not in shown and "fermata" in shown
    assert pane.b_send.isEnabled() and not w.isWindowModified()


def test_modify_changes_the_parameters_before_running(window, fake_actions, monkeypatch):
    from pyarchinit_photogrammetry.gui import assistant_actions

    w, app = window
    pane = w.assistant
    _mode(pane, "ask")
    asked = []

    def edit(parent, title, fields, intro=""):
        asked.append({f.name: f.default for f in fields})
        return {**asked[-1], "min_size": "0.2", "thoroughness": "massima"}

    monkeypatch.setattr(assistant_actions.ParamDialog, "ask", staticmethod(edit))
    pane.set_provider(FakeProvider([_calls("act_stones", {"min_size": 0.1}), text("Fatto.")]))
    pane.ask("disegna le pietre")
    _until(app, lambda: pane.cards and pane.cards[-1].state == "waiting")
    pane.cards[-1].b_edit.click()
    assert asked and asked[0]["min_size"] == "0.1"
    assert pane.cards[-1].state == "waiting" and "0,2" in pane.cards[-1].text()
    pane.cards[-1].b_run.click()
    _wait(app, pane)
    assert fake_actions[0][1] == {"min_size": 0.2, "thoroughness": "massima"}
    w.setWindowModified(False)


def test_quick_runs_showing_at_once_and_all_runs_everything(window, fake_actions):
    w, app = window
    pane = w.assistant
    assert pane.action_mode() == "quick"  # the default
    fake = FakeProvider([_calls("act_show", {"what": "dem"}), text("Ecco il DEM."),
                         _calls("act_stones", {}), text("Fatto.")])
    pane.set_provider(fake)
    pane.ask("mostra il DEM")
    _wait(app, pane)
    # no card for a quick one; it only changes the view: run at once in the interface, no step of the history
    assert fake_actions == [("show", {"what": "dem"}, True)] and not pane.cards
    assert not w.history.can_undo() and "Mostra" in pane.chat.toPlainText()
    assert _tool_result(fake) == {"ran": True, "shown": "dem"}
    pane.ask("disegna le pietre")
    _until(app, lambda: pane.cards and pane.cards[-1].state == "waiting")  # not quick: it waits
    pane.cards[-1].b_run.click()
    _wait(app, pane)
    assert len(fake_actions) == 2
    # Vista > Assistente > Azioni > Esegui tutto
    w.assistant_action_modes["all"].trigger()
    assert pane.action_mode() == "all" and pane._settings().value("assistant_actions") == "all"
    fake = FakeProvider([_calls("act_stones", {"max_size": 0.5}), text("Fatto.")])
    pane.set_provider(fake)
    pane.ask("disegna le pietre")
    _wait(app, pane)
    assert len(fake_actions) == 3 and pane.cards[-1].state == "done"  # ran at once, the card says what ran
    assert pane.cards[-1].b_undo.isEnabled()
    w.assistant_action_modes["quick"].trigger()
    w.setWindowModified(False)


def test_a_failing_action_says_its_error_and_the_window_stays_usable(window, monkeypatch):
    from pyarchinit_photogrammetry.assistant import actions

    w, app = window
    pane = w.assistant

    def broken(chunk, window, progress=None, **kw):
        chunk.meta["mezzo"] = 1
        raise RuntimeError("rotto")

    monkeypatch.setitem(actions.ACTIONS, "stones", (broken, *actions.ACTIONS["stones"][1:]))
    _mode(pane, "all")
    fake = FakeProvider([_calls("act_stones", {}), text("Non è andata.")])
    pane.set_provider(fake)
    pane.ask("disegna le pietre")
    _wait(app, pane)
    assert "rotto" in _tool_result(fake)["error"] and pane.cards[-1].state == "failed"
    assert not w.runner.busy and pane.b_send.isEnabled()
    # what it changed before failing is one step back
    assert w.chunk.meta.get("mezzo") == 1
    w.undo()
    assert "mezzo" not in w.chunk.meta
    # refused while another step runs
    got = []
    release = threading.Event()
    assert w.runner.run("lungo", lambda progress: release.wait(10))
    assert w.run_action("stones", {}, got.append) is False
    assert got and "error" in got[0]
    release.set()
    w.runner.wait()
    w.setWindowModified(False)


def test_a_waiting_card_never_runs_on_another_project_or_chunk(window, fake_actions, processed, tmp_path):
    import pyarchinit_photogrammetry as ppg

    w, app = window
    pane = w.assistant
    _mode(pane, "ask")
    fake = FakeProvider([_calls("act_stones", {}), text("Fatto!")])
    pane.set_provider(fake)
    pane.ask("disegna le pietre")
    _until(app, lambda: pane.cards and pane.cards[-1].state == "waiting")
    card = pane.cards[-1]
    assert "Saggio 1" in card.text()  # the chunk it will act on
    ran = []
    original = w.run_action
    w.run_action = lambda *a, **k: ran.append(a) or original(*a, **k)
    other = ppg.Document()
    other.add_chunk("Altro")
    other.save(tmp_path / "altro" / "altro.ppg")
    w.set_document(other)  # another project opened: the card of the old one stops at once
    _wait(app, pane)
    assert card.state == "stopped" and "progetto cambiato" in card.text()
    card.b_run.click()
    app.processEvents()
    assert not ran and not fake_actions and len(fake.calls) == 1
    assert "progetto cambiato" in pane.chat.toPlainText()
    w.setWindowModified(False)


def test_a_card_does_not_run_when_the_active_chunk_changed(window, fake_actions):
    w, app = window
    pane = w.assistant
    _mode(pane, "ask")
    fake = FakeProvider([_calls("act_stones", {}), text("Fatto!")])
    pane.set_provider(fake)
    pane.ask("disegna le pietre")
    _until(app, lambda: pane.cards and pane.cards[-1].state == "waiting")
    w.doc.chunk = w.doc.add_chunk("Saggio 2")  # another chunk active since the question
    pane.cards[-1].b_run.click()
    _wait(app, pane)
    assert not fake_actions and pane.cards[-1].state == "stopped"
    assert "chunk attivo cambiato" in pane.chat.toPlainText()
    w.setWindowModified(False)


def test_stop_while_an_action_runs(window, monkeypatch):
    from pyarchinit_photogrammetry.assistant import actions

    w, app = window
    pane = w.assistant
    started = threading.Event()

    def slow(chunk, window, progress=None, **kw):
        started.set()
        while True:  # a step that honours the progress's cancel
            if progress.cancel_requested:
                from pyarchinit_photogrammetry.core.progress import Cancelled

                raise Cancelled("stop")
            time.sleep(0.02)

    monkeypatch.setitem(actions.ACTIONS, "stones", (slow, *actions.ACTIONS["stones"][1:]))
    _mode(pane, "all")
    fake = FakeProvider([_calls("act_stones", {}), text("Fatto!")])
    pane.set_provider(fake)
    pane.ask("disegna le pietre")
    _until(app, lambda: started.is_set())
    card = pane.cards[-1]
    assert card.state == "running"
    pane.b_stop.click()
    _wait(app, pane)
    w.runner.wait()
    app.processEvents()
    assert card.state == "stopped" and not w.runner.busy and len(fake.calls) == 1
    assert "fermata" in pane.chat.toPlainText()


def test_a_card_that_stopped_says_when_the_action_finished_anyway(window):
    from pyarchinit_photogrammetry.gui.assistant_actions import ActionCard

    w, app = window
    card = ActionCard("stones", {}, None, chunk_label="Saggio 1")
    card.set_state("stopped")
    card.finish({"ran": True, "stones": 3})
    assert card.state == "stopped" and "completata comunque" in card.result_label.text()


def test_modify_reads_the_fields_by_the_type_of_the_parameter():
    from pyarchinit_photogrammetry.gui.assistant_actions import parse_value

    assert parse_value("2024", {"type": "string"}) == "2024"  # a name stays a name
    assert parse_value("0.2", {"type": "number"}) == 0.2 and parse_value("0,2", {"type": "number"}) == 0.2
    assert parse_value("[1, 2, 3, 4]", {"type": ["array", "string"]}) == [1, 2, 3, 4]
    assert parse_value("selection", {"type": ["array", "string"]}) == "selection"
    assert parse_value("12", {"type": ["array", "string", "integer"]}) == 12
    assert parse_value("true", {"type": "boolean"}) is True
    assert parse_value("  ", {"type": "number"}) is None
    assert parse_value('{"resolution": 0.02}', {"type": "object"}) == {"resolution": 0.02}


def test_modify_never_offers_a_scripts_only_parameter(monkeypatch):
    """The export's ``path`` is a script's parameter: the window asks for the file, so the form of *Modifica* does not
    show it either (a path typed there would bypass the save dialog)."""
    from pyarchinit_photogrammetry.gui import assistant_actions as aa

    seen = {}

    def fake_ask(parent, title, fields):
        seen["keys"] = [f.name for f in fields]
        return None

    monkeypatch.setattr(aa.ParamDialog, "ask", staticmethod(fake_ask))
    assert aa.edit_args(None, "export", {"what": "dem", "format": "tif"}, "Esporta") is None
    assert "what" in seen["keys"] and "path" not in seen["keys"]


def test_the_assistants_own_steps_do_not_stop_the_next_action(window, monkeypatch):
    """show makes another chunk active, then stones in the same reply: the chunk changed by the assistant itself,
    not by the user - the stones run on it."""
    from pyarchinit_photogrammetry.assistant import actions
    from pyarchinit_photogrammetry.assistant.providers import ToolCall

    w, app = window
    pane = w.assistant
    w.doc.add_chunk("Saggio 2")
    seen = []

    def show(chunk, window, what="ortho", chunk_label=None, **kw):
        window._activate_chunk(window.doc.chunk_by_label(chunk_label))
        return {"shown": what, "chunk": chunk_label}

    def stones(chunk, window, progress=None, **kw):
        seen.append(chunk.label)
        chunk.meta["pietre_finte"] = 1
        return {"stones": 1}

    monkeypatch.setitem(actions.ACTIONS, "show", (show, *actions.ACTIONS["show"][1:]))
    monkeypatch.setitem(actions.ACTIONS, "stones", (stones, *actions.ACTIONS["stones"][1:]))
    _mode(pane, "quick")  # showing another chunk waits for Esegui, then the stones wait for theirs
    two = Reply("", [ToolCall("c0", "act_show", {"what": "ortho", "chunk_label": "Saggio 2"}),
                     ToolCall("c1", "act_stones", {})], "tool_calls", {})
    fake = FakeProvider([two, text("Fatto su Saggio 2.")])
    pane.set_provider(fake)
    pane.ask("quella di Saggio 2: disegna le pietre")
    _until(app, lambda: pane.cards and pane.cards[-1].state == "waiting")
    assert pane.cards[-1].name == "show"
    pane.cards[-1].b_run.click()
    _until(app, lambda: pane.cards and pane.cards[-1].name == "stones" and pane.cards[-1].state == "waiting")
    assert w.chunk.label == "Saggio 2" and "Saggio 2" in pane.cards[-1].text()
    pane.cards[-1].b_run.click()
    _wait(app, pane)
    assert seen == ["Saggio 2"] and pane.cards[-1].state == "done"
    assert "Fatto su Saggio 2." in pane.chat.toPlainText()
    w.setWindowModified(False)



def test_all_still_asks_for_scripts_and_exports_and_quick_for_another_chunk(window, monkeypatch):
    from pyarchinit_photogrammetry.assistant import actions

    w, app = window
    pane = w.assistant
    seen = []

    def build(chunk, window, product, progress=None, **kw):
        seen.append(product)
        return {"product": product}

    monkeypatch.setitem(actions.ACTIONS, "build", (build, *actions.ACTIONS["build"][1:]))
    _mode(pane, "all")
    pane.set_provider(FakeProvider([_calls("act_build", {"product": "relief"}), text("Fatto."),
                                    _calls("act_run_script", {"source": "print(1)"}), text("Non eseguito.")]))
    pane.ask("il rilievo")
    _wait(app, pane)
    assert seen == ["relief"]  # at once
    pane.ask("esegui questo")
    _until(app, lambda: pane.cards and pane.cards[-1].name == "run_script" and pane.cards[-1].state == "waiting")
    pane.cards[-1].b_no.click()
    _wait(app, pane)
    # quick: showing another chunk makes it active (its undo history starts afresh): a card says so
    w.doc.add_chunk("Saggio 2")
    _mode(pane, "quick")
    pane.set_provider(FakeProvider([_calls("act_show", {"what": "dem", "chunk_label": "Saggio 2"}), text("ok")]))
    pane.ask("il DEM di Saggio 2")
    _until(app, lambda: pane.cards and pane.cards[-1].state == "waiting")
    assert "Chunk: Saggio 2" in pane.cards[-1].text() and "cronologia" in pane.cards[-1].text()
    pane.cards[-1].b_no.click()
    _wait(app, pane)
    w.setWindowModified(False)


def test_an_export_of_the_model_goes_through_the_save_dialog(window, monkeypatch):
    w, app = window
    pane = w.assistant
    asked = []
    monkeypatch.setattr(w, "ask_save_path", lambda title, name, filter_: asked.append(name) or "")
    fake = FakeProvider([_calls("act_export", {"what": "orthomosaic", "format": "tif", "path": "/tmp/fuori.tif"}),
                         text("Annullato.")])
    pane.set_provider(fake)
    pane.ask("esporta l'ortofoto")
    _until(app, lambda: pane.cards and pane.cards[-1].state == "waiting")
    assert "fuori" not in pane.cards[-1].text()
    pane.cards[-1].b_run.click()
    _wait(app, pane)
    assert asked == ["orthomosaic.tif"] and _tool_result(fake) == {"declined": True}
    assert pane.cards[-1].state == "declined"


def test_a_chunk_switched_by_the_user_during_an_action_stops_the_next(window, monkeypatch):
    from pyarchinit_photogrammetry.assistant import actions
    from pyarchinit_photogrammetry.assistant.providers import ToolCall

    w, app = window
    pane = w.assistant
    other = w.doc.add_chunk("Saggio 2")
    shown = []

    def stones(chunk, window, progress=None, **kw):
        w.doc.chunk = other  # the user makes another chunk active while it runs
        return {"stones": 1}

    monkeypatch.setitem(actions.ACTIONS, "stones", (stones, *actions.ACTIONS["stones"][1:]))
    monkeypatch.setitem(actions.ACTIONS, "show",
                        (lambda chunk, window, what="ortho", **kw: shown.append(what) or {"shown": what},
                         *actions.ACTIONS["show"][1:]))
    _mode(pane, "all")
    two = Reply("", [ToolCall("c0", "act_stones", {}), ToolCall("c1", "act_show", {"what": "dem"})], "tool_calls", {})
    pane.set_provider(FakeProvider([two, text("ok")]))
    pane.ask("pietre e poi il DEM")
    _wait(app, pane)
    assert not shown and "chunk attivo cambiato" in pane.chat.toPlainText()
    w.setWindowModified(False)


def test_result_lines_name_the_facts_in_words():
    from pyarchinit_photogrammetry.gui.assistant_actions import result_line

    assert result_line("stones", {"ran": True, "stones": 3, "layer": "Pietre"}) == "pietre: 3; layer: Pietre"
