"""Plugin updates in the window: the look at the plugin index that goes with the daily check of the program (in the
background, one at a time), the summary of what it found - each plugin that waits, update that is not installed or
update that failed told once, the windows one at a time - the switch *Aggiorna i plugin da soli*, the lines in the log
and the note of the plugin manager. Nothing reaches the network, no modal window opens: the look is a function of the
test and the summary window a fake."""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from pyarchinit_photogrammetry import updates as program_updates  # noqa: E402
from pyarchinit_photogrammetry.gui import i18n, update_dialog  # noqa: E402
from pyarchinit_photogrammetry.plugins import Manager  # noqa: E402
from pyarchinit_photogrammetry.plugins import index as plugin_index  # noqa: E402
from pyarchinit_photogrammetry.plugins import updates as plugin_updates  # noqa: E402
from pyarchinit_photogrammetry.plugins.updates import PluginUpdates  # noqa: E402
from test_plugin_updates import _installed, _newer  # noqa: E402
from test_plugins import _entry, _index, _install_folder, _wheel  # noqa: E402

REASON = "needs pyarchinit-photogrammetry 99.0 or later"  # what Entry.problem() says of a version for a newer program


@pytest.fixture(autouse=True)
def _application():
    """The tests that only read a summary need the application too (the text of a rich-text document)."""
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture
def windows(tmp_path):
    """A factory of windows as the GUI tests make them (no project, not connected to PyArchInit, no check at start),
    and the application. The preferences are in a folder of the test; the windows are closed at the end."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    stale = QApplication.activeModalWidget()
    if stale is not None:  # a window another test left open would hold back the summaries of these tests
        stale.close()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.core.document import Document
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    made = []

    def make():
        w = MainWindow(Document(), autoconnect=False, check_updates=False)
        w.runner.show_dialog = False
        made.append(w)
        return w

    yield make, app
    running = [w for w in made if w.plugin_checker.running]
    for w in made:
        w.setWindowModified(False)
        w.close()
    # a look left running hands its answer to the next test, where the fakes of this one are gone and the real summary
    # window opens (conftest then fails *that* test): every test of this file ends with no look running
    assert not running, "the test left a look at the plugin index running"


@pytest.fixture
def window(windows):
    make, app = windows
    return make(), app


@pytest.fixture
def english():
    i18n.set_current("en")
    yield
    i18n.set_current("it")


def _wait(app, condition, what: str, timeout: float = 10.0) -> None:
    """Let the interface thread deliver what the thread of the look emitted."""
    start = time.time()
    while not condition() and time.time() - start < timeout:
        app.processEvents()
        time.sleep(0.005)
    assert condition(), f"timed out waiting for {what}"


def _text(html: str) -> str:
    """What the user reads of a summary."""
    from PySide6.QtGui import QTextDocument

    doc = QTextDocument()
    doc.setHtml(html)
    return doc.toPlainText()


def _rows(w) -> list[tuple[int, str]]:
    """The rows of the log pane: (level, text)."""
    return [(level, text) for _time, level, text, _details in w.log_pane.entries]


def _release():
    """A newer version of the program, as the program's own check hands it over."""
    return program_updates.Release(version="9.9.9", tag="v9.9.9", url="https://example.org/release")


def _counting(w) -> list:
    """A list that gains an item each time a look of the window ``w`` is over and the window has dealt with what it
    found: the receiver is connected after the window's own slots, and slots run in the order they were connected."""
    from PySide6.QtCore import QObject, Slot

    ended: list = []

    class Counter(QObject):
        @Slot(object, object, bool)
        def done(self, *_):
            ended.append(True)

    w.plugin_checker.finished.connect(Counter(w).done)  # a child of the window: it lives as long as the window
    return ended


def _looker(w, app):
    """``look(manual=False)``: one look of the window ``w``, to the end - the window has dealt with what it found."""
    ended = _counting(w)

    def look(manual: bool = False) -> None:
        n = len(ended)
        assert w._check_plugins(manual=manual)
        _wait(app, lambda: len(ended) > n, "the look to end")

    return look


def _fake_run(monkeypatch, result=None, error=None) -> list[dict]:
    """The look at the plugin index of these tests: nothing is read or downloaded. The calls are listed. ``result`` is
    what each look finds - or a list of them, one per look, the last one for the looks after it."""
    calls: list[dict] = []

    def run(seen=None, *, folder=None, update=True, url=None, progress=None):
        calls.append({"seen": seen, "folder": folder, "update": update})
        if error is not None:
            raise error
        if isinstance(result, list):
            return result[min(len(calls), len(result)) - 1]
        return result if result is not None else PluginUpdates()

    monkeypatch.setattr(plugin_updates, "run", run)
    return calls


def _fake_dialog(monkeypatch, answer=None, order=None, during=None) -> list[str]:
    """Stands in for the summary window (a modal window never opens in a test): the html it would show is added to the
    list, ``exec()`` returns at once and the ``action`` is ``answer``. ``order`` (a list) gets "the summary opens" and
    "the summary closes"; ``during()`` runs in between, while the window is "open"."""
    from pyarchinit_photogrammetry.gui import main_window as mw

    shown: list[str] = []

    class Summary:
        def __init__(self, result, parent=None):
            self.result, self.action = result, None

        def exec(self):
            if order is not None:
                order.append("the summary opens")
            shown.append(update_dialog.plugin_summary_html(self.result))
            if during is not None:
                during()
            if order is not None:
                order.append("the summary closes")
            self.action = answer
            return 0

    monkeypatch.setattr(mw, "PluginUpdatesDialog", Summary)
    return shown


def _fake_program_dialog(monkeypatch, order, during=None) -> None:
    """The same for the window of the program's own update: ``order`` gets "the window of the program opens" and
    "... closes", ``during()`` runs in between."""
    from pyarchinit_photogrammetry.gui import main_window as mw

    class Program:
        def __init__(self, release, parent=None):
            self.action, self.command = "later", None

        def exec(self):
            order.append("the window of the program opens")
            if during is not None:
                during()
            order.append("the window of the program closes")
            return 0

    monkeypatch.setattr(mw, "UpdateDialog", Program)


def _new_entry(name: str = "nuovo", **extra) -> plugin_index.Entry:
    """A plugin of the index (its wheel is never fetched)."""
    return plugin_index.Entry(name=name, version=extra.pop("version", "0.1.0"), sha256="0" * 64,
                              wheel=f"https://example.org/{name}-0.1.0-py3-none-any.whl", **extra)


# ---------------------------------------------------------------------------------------- the daily look
def test_the_summary_after_the_daily_check(window, monkeypatch):
    w, app = window
    new = _new_entry("nuovo", title="Plugin nuovo", description="Fa una cosa utile")
    result = PluginUpdates(updated=[("a", "0.1.0", "0.2.0")], waiting=[("b", "0.3.0", REASON)], new=[new],
                           seen=["a", "b", "nuovo"])
    calls = _fake_run(monkeypatch, result)
    shown = _fake_dialog(monkeypatch, answer="restart")
    restarted = []  # whether each restart was asked for in the interface thread
    monkeypatch.setattr(w, "restart", lambda: restarted.append(threading.current_thread() is threading.main_thread()))
    assert w._check_plugins(manual=False)
    _wait(app, lambda: restarted, "the restart the summary asked for")
    assert restarted == [True]  # the window deals with the result in the interface thread, not in the one that looked
    assert len(shown) == 1 and len(calls) == 1 and calls[0]["seen"] is None  # the first look: nothing seen before
    text = _text(shown[0])
    assert "a 0.1.0 → 0.2.0" in text and "attivi dal prossimo avvio" in text  # staged: active at the next start
    assert "In attesa" in text and f"b 0.3.0: {REASON}" in text and "richiede" not in text  # the reason as it is
    assert "Plugin nuovo" in text and "Fa una cosa utile" in text  # the new one by its title
    assert json.loads(w.settings.value("plugins_seen")) == ["a", "b", "nuovo"]  # told once: not new at the next look


@pytest.mark.parametrize("answer, called", [("restart", "restart"), ("manager", "show_plugins"), (None, None)])
def test_what_the_summary_asks_for(window, monkeypatch, answer, called):
    w, app = window
    _fake_run(monkeypatch, PluginUpdates(failed=[("a", "no space left")], seen=["a"]))
    shown = _fake_dialog(monkeypatch, answer=answer)
    done = []
    monkeypatch.setattr(w, "restart", lambda: done.append("restart"))
    monkeypatch.setattr(w, "show_plugins", lambda: done.append("show_plugins"))
    assert w._check_plugins(manual=True)
    _wait(app, lambda: shown, "the summary")
    app.processEvents()
    assert done == ([called] if called else [])


def test_nothing_to_say_says_nothing(window, monkeypatch):
    w, app = window
    _fake_run(monkeypatch, PluginUpdates(seen=["a", "b"]))
    shown = _fake_dialog(monkeypatch)
    before = len(w.log_pane.entries)
    assert w._check_plugins(manual=False)
    _wait(app, lambda: w.settings.value("plugins_seen", "") != "", "the names seen to be stored")
    assert json.loads(w.settings.value("plugins_seen")) == ["a", "b"]
    assert not shown and len(w.log_pane.entries) == before  # no window, no line


def test_an_update_staged_by_an_earlier_look_is_not_told_again(window, monkeypatch):
    w, app = window
    pending = PluginUpdates(pending=[("a", "0.1.0", "0.2.0")], seen=["a"])
    assert not pending  # told when it was staged
    _fake_run(monkeypatch, pending)
    shown = _fake_dialog(monkeypatch)
    assert w._check_plugins(manual=False)
    _wait(app, lambda: w.settings.value("plugins_seen", "") != "", "the names seen to be stored")
    assert not shown


def test_manual_check_with_nothing_writes_one_line(window, monkeypatch):
    w, app = window
    _fake_run(monkeypatch, PluginUpdates(seen=["a"]))
    shown = _fake_dialog(monkeypatch)
    before = len(w.log_pane.entries)
    assert w._check_plugins(manual=True)
    _wait(app, lambda: len(w.log_pane.entries) > before, "the line in the log")
    app.processEvents()
    assert _rows(w)[before:] == [(logging.INFO, "Plugin: nessun aggiornamento")] and not shown


def test_offline_is_one_log_line(window, monkeypatch):
    w, app = window
    w.settings.setValue("plugins_seen", json.dumps(["a"]))
    _fake_run(monkeypatch, error=plugin_index.PluginError("the plugin index could not be read: offline"))
    shown = _fake_dialog(monkeypatch)
    before = len(w.log_pane.entries)
    assert w._check_plugins(manual=False)
    _wait(app, lambda: len(w.log_pane.entries) > before, "the line in the log")
    app.processEvents()
    rows = _rows(w)[before:]
    assert len(rows) == 1 and "could not be read: offline" in rows[0][1]
    assert rows[0][0] == logging.INFO  # an info, not a warning
    assert not shown and json.loads(w.settings.value("plugins_seen")) == ["a"]  # nothing recorded as seen


def test_a_manual_check_that_fails_warns_in_the_log(window, monkeypatch):
    w, app = window
    _fake_run(monkeypatch, error=plugin_index.PluginError("the plugin index could not be read: offline"))
    shown = _fake_dialog(monkeypatch)
    before = len(w.log_pane.entries)
    assert w._check_plugins(manual=True)
    _wait(app, lambda: len(w.log_pane.entries) > before, "the warning in the log")
    app.processEvents()
    rows = _rows(w)[before:]
    assert len(rows) == 1  # one row, not a line of the logger and another of the window
    assert rows[0][0] == logging.WARNING and "could not be read: offline" in rows[0][1] and not shown


def test_the_names_seen_are_remembered_between_looks(window, monkeypatch):
    w, app = window
    calls = _fake_run(monkeypatch, PluginUpdates(seen=["a", "b"]))
    _fake_dialog(monkeypatch)
    look = _looker(w, app)
    look()  # nothing stored: the first look
    assert calls[0]["seen"] is None
    look()
    assert calls[1]["seen"] == ["a", "b"]
    for garbage in ("not json", "{}", '"a"', "42"):  # not a list of names: as if there were none
        w.settings.setValue("plugins_seen", garbage)
        look()
        assert calls[-1]["seen"] is None, garbage


def test_two_real_looks_at_an_index(windows, tmp_path, monkeypatch):
    make, app = windows
    folder = tmp_path / "plugins"
    _install_folder(folder, "a")
    newer = _entry(_wheel(tmp_path / "a-0.2.0-py3-none-any.whl", "a", version="0.2.0"), "a", version="0.2.0")
    zeta = _entry(_wheel(tmp_path / "z-0.1.0-py3-none-any.whl", "z"), "z", title="Zeta", description="Una novità")
    eta = _entry(_wheel(tmp_path / "eta-0.1.0-py3-none-any.whl", "eta"), "eta", title="Eta", description="Un'altra")
    monkeypatch.setenv("PPG_PLUGINS_DIR", str(folder))
    monkeypatch.setenv("PPG_PLUGIN_INDEX", _index(tmp_path, [newer, zeta]))  # the index is a file of the test
    w = make()
    shown = _fake_dialog(monkeypatch)
    look = _looker(w, app)
    look()  # the first look: the update is downloaded, the plugin the user does not have is only recorded
    assert len(shown) == 1 and "a 0.1.0 → 0.2.0" in _text(shown[0]) and "Zeta" not in _text(shown[0])
    assert plugin_updates.staged(folder) == {"a": "0.2.0"}
    assert [i.version for i in Manager(folder).discover()] == ["0.1.0"]  # in use at the next start, not before
    assert json.loads(w.settings.value("plugins_seen")) == ["a", "z"]
    monkeypatch.setenv("PPG_PLUGIN_INDEX", _index(tmp_path, [newer, zeta, eta]))  # the index gains a plugin
    look()
    assert len(shown) == 2
    text = _text(shown[1])
    assert "Eta" in text and "Un'altra" in text  # announced as new
    assert "Zeta" not in text  # the first look recorded it
    assert "0.2.0" not in text  # the update waiting for the restart is not told again
    assert not (folder / "z").exists() and not (folder / "eta").exists()  # a new plugin is never installed by itself
    look()  # nothing new now: nothing said
    assert len(shown) == 2 and json.loads(w.settings.value("plugins_seen")) == ["a", "eta", "z"]
    look(manual=True)  # the update staged is ready, and the manual look says so
    rows = _rows(w)
    assert (logging.INFO, "Plugin a: la 0.2.0 è pronta, attiva dal prossimo avvio") in rows
    assert (logging.INFO, "Plugin: nessun aggiornamento") not in rows and len(shown) == 2


def test_the_switch_turns_installing_off(window, monkeypatch):
    w, app = window
    assert w.a_plugins_auto_update.isCheckable() and w.a_plugins_auto_update.isChecked()  # on until the user says no
    assert w.a_plugins_auto_update.text() == "Aggiorna i plugin da soli"
    calls = _fake_run(monkeypatch, PluginUpdates(available=[("a", "0.1.0", "0.2.0")], seen=["a"]))
    shown = _fake_dialog(monkeypatch)
    look = _looker(w, app)  # every look is waited for to its end: a look left running reaches the next test
    w.a_plugins_auto_update.setChecked(False)
    assert w.settings.value("plugins_auto_update") == "false"
    look()
    assert len(shown) == 1 and calls[0]["update"] is False and calls[0]["folder"] == w.plugins.folder
    text = _text(shown[0])
    assert "Aggiornamenti disponibili" in text and "a 0.1.0 → 0.2.0" in text  # told, not installed
    assert "spento" in text and "attivi dal prossimo avvio" not in text
    w.a_plugins_auto_update.setChecked(True)
    assert w.settings.value("plugins_auto_update") == "true"
    look()
    assert len(calls) == 2 and calls[1]["update"] is True  # the look after it downloads


def test_the_switch_is_in_the_help_menu_and_remembered(windows):
    make, app = windows
    w = make()
    actions = w.help_menu.actions()
    assert actions.index(w.a_plugins_auto_update) == actions.index(w.a_update_auto) + 1  # next to the program's own
    w.a_plugins_auto_update.setChecked(False)
    again = make()  # the next start
    assert not again.a_plugins_auto_update.isChecked() and again.a_update_auto.isChecked()


def test_the_daily_check_covers_the_plugins(window, monkeypatch):
    w, app = window
    program, plugins = [], []
    monkeypatch.setattr(w.update_checker, "start", lambda manual=False, skip=None: program.append(manual) or True)
    monkeypatch.setattr(w.plugin_checker, "start", lambda *args: plugins.append(args) or True)
    w._auto_check_updates()
    w._auto_check_updates()  # the same day: no second look
    assert program == [False] and plugins == [(False, None, True, w.plugins.folder)]
    # the program's check at start switched off: the plugins' look goes with it
    w.settings.setValue("updates_last_check", "2000-01-01")
    w.a_update_auto.setChecked(False)
    w._auto_check_updates()
    assert program == [False] and len(plugins) == 1


def test_the_manual_check_looks_at_the_plugins_too(window, monkeypatch):
    w, app = window
    program = []
    monkeypatch.setattr(w.update_checker, "start", lambda manual=False, skip=None: program.append(manual) or True)
    calls = _fake_run(monkeypatch, PluginUpdates(seen=["a"]))
    _fake_dialog(monkeypatch)
    w.check_updates()
    assert program == [True] and w.statusBar().currentMessage() == "Controllo degli aggiornamenti..."
    # a manual look answers, whatever it found
    _wait(app, lambda: (logging.INFO, "Plugin: nessun aggiornamento") in _rows(w), "the answer of the look")
    assert len(calls) == 1


def test_one_check_at_a_time(window, monkeypatch, tmp_path):
    w, app = window
    gate, started = threading.Event(), threading.Event()

    def slow(seen=None, *, folder=None, update=True, url=None, progress=None):
        started.set()
        gate.wait(10)
        return PluginUpdates(seen=["a"])

    monkeypatch.setattr(plugin_updates, "run", slow)
    _fake_dialog(monkeypatch)
    monkeypatch.setattr(w.update_checker, "start", lambda manual=False, skip=None: False)  # the program's runs too
    try:
        assert w._check_plugins(manual=False)  # the daily look
        assert started.wait(5) and w.plugin_checker.running
        assert not w.plugin_checker.start(True, None, True, tmp_path)  # a second one is refused ...
        assert not w._check_plugins(manual=True)  # ... also from the window
        w.check_updates()  # *Controlla aggiornamenti* while both run: it says so
        assert w.statusBar().currentMessage() == "Controllo degli aggiornamenti gia' in corso"
    finally:
        gate.set()
    _wait(app, lambda: not w.plugin_checker.running, "the look to end")
    gate.clear()
    started.clear()
    try:
        assert w._check_plugins(manual=False)  # one look at a time, not once for ever
        assert started.wait(5)
    finally:
        gate.set()
    _wait(app, lambda: not w.plugin_checker.running, "the second look to end")


# ---------------------------------------------------------------------------------------- told once
# kind -> (what a look finds of that kind at a version, the key kept in ``plugins_told`` for it)
KINDS = {
    "waiting": (lambda v: PluginUpdates(waiting=[("b", v, REASON)], seen=["b"]), "waiting:b:{}"),
    "available": (lambda v: PluginUpdates(available=[("b", "0.1.0", v)], seen=["b"]), "available:b:{}"),
    # a failed update has no version of its own in the result: the message stands in its place
    "failed": (lambda v: PluginUpdates(failed=[("b", f"b: the wheel could not be downloaded ({v})")], seen=["b"]),
               "failed:b:b: the wheel could not be downloaded ({})"),
}


@pytest.mark.parametrize("kind", list(KINDS))
def test_the_daily_look_tells_an_item_once_per_version(window, monkeypatch, kind):
    w, app = window
    make, key = KINDS[kind]
    gone = PluginUpdates(seen=["b"])
    _fake_run(monkeypatch, [make("0.3.0"), make("0.3.0"), make("0.4.0"), make("0.4.0"), gone, make("0.4.0")])
    shown = _fake_dialog(monkeypatch)
    look = _looker(w, app)

    def told():
        return json.loads(w.settings.value("plugins_told"))

    look()  # the first day: told, and remembered
    assert len(shown) == 1 and "0.3.0" in _text(shown[0]) and told() == [key.format("0.3.0")]
    look()  # the next day, the same item: nothing opens
    assert len(shown) == 1 and told() == [key.format("0.3.0")]
    look()  # another version of it: the summary opens with that item alone, and the old entry is dropped
    assert len(shown) == 2 and "0.4.0" in _text(shown[1]) and "0.3.0" not in _text(shown[1])
    assert told() == [key.format("0.4.0")]
    look(manual=True)  # the user asks: everything is shown again
    assert len(shown) == 3 and "0.4.0" in _text(shown[2])
    look()  # the item is gone ...
    assert len(shown) == 3 and told() == []
    look()  # ... and back: told again
    assert len(shown) == 4


def test_the_daily_summary_has_only_what_was_not_told(window, monkeypatch):
    w, app = window
    waiting = ("b", "0.3.0", REASON)
    nuovo = _new_entry("nuovo", title="Plugin nuovo")
    news = PluginUpdates(updated=[("a", "0.1.0", "0.2.0")], waiting=[waiting], new=[nuovo], seen=["a", "b", "nuovo"])
    _fake_run(monkeypatch, [PluginUpdates(waiting=[waiting], seen=["b"]), news])
    shown = _fake_dialog(monkeypatch)
    look = _looker(w, app)
    look()
    assert len(shown) == 1 and "b 0.3.0" in _text(shown[0])
    look()  # an update made and a new plugin are always news; the plugin that waits was told
    assert len(shown) == 2
    text = _text(shown[1])
    assert "a 0.1.0 → 0.2.0" in text and "Plugin nuovo" in text
    assert "b 0.3.0" not in text and "In attesa" not in text
    assert json.loads(w.settings.value("plugins_told")) == ["waiting:b:0.3.0"]  # and is still remembered


def test_a_damaged_list_of_what_was_told_is_an_empty_one(window, monkeypatch):
    w, app = window
    _fake_run(monkeypatch, PluginUpdates(waiting=[("b", "0.3.0", REASON)], seen=["b"]))
    shown = _fake_dialog(monkeypatch)
    look = _looker(w, app)
    for garbage in ("not json", "{}", '"a"', "42"):
        w.settings.setValue("plugins_told", garbage)
        look()
        assert len(shown) > 0 and json.loads(w.settings.value("plugins_told")) == ["waiting:b:0.3.0"], garbage
        shown.clear()


def test_a_manual_check_tells_the_updates_waiting_for_the_restart(window, monkeypatch):
    w, app = window
    pending = [("a", "0.1.0", "0.2.0"), ("b", "0.3.0", "0.4.0")]
    _fake_run(monkeypatch, PluginUpdates(pending=pending, seen=["a", "b"]))
    shown = _fake_dialog(monkeypatch)
    look = _looker(w, app)
    before = len(w.log_pane.entries)
    look()  # the daily look says nothing of them: they were told when they were downloaded
    assert len(w.log_pane.entries) == before
    look(manual=True)  # the manual one does: one line for each, not "no updates"
    assert _rows(w)[before:] == [(logging.INFO, "Plugin a: la 0.2.0 è pronta, attiva dal prossimo avvio"),
                                 (logging.INFO, "Plugin b: la 0.4.0 è pronta, attiva dal prossimo avvio")]
    assert not shown


def test_an_index_with_no_plugin_records_nothing(window, monkeypatch):
    w, app = window
    both = PluginUpdates(seen=["a", "b"])
    calls = _fake_run(monkeypatch, [PluginUpdates(seen=[]), both, both])
    _fake_dialog(monkeypatch)
    look = _looker(w, app)
    look()  # the index is valid and holds no plugin yet
    assert w.settings.value("plugins_seen", "") == ""  # nothing is recorded: the first look is still to come
    look()
    assert calls[1]["seen"] is None  # so this is the first look, and it announces no plugin as new
    assert json.loads(w.settings.value("plugins_seen")) == ["a", "b"]
    look()
    assert calls[2]["seen"] == ["a", "b"]


# ---------------------------------------------------------------------------------------- one window at a time
def test_the_summary_waits_for_the_window_of_the_program_update(window, monkeypatch):
    w, app = window
    order = []
    shown = _fake_dialog(monkeypatch, order=order)
    result = PluginUpdates(updated=[("a", "0.1.0", "0.2.0")], seen=["a"])
    # the look of the plugins ends while the window of the program's update is open
    _fake_program_dialog(monkeypatch, order, during=lambda: w.plugin_checker.finished.emit(result, None, False))
    w._updates_checked(_release(), None, False)
    assert order == ["the window of the program opens", "the window of the program closes",
                     "the summary opens", "the summary closes"]
    assert len(shown) == 1


def test_the_window_of_the_program_update_waits_for_the_summary(window, monkeypatch):
    w, app = window
    order = []
    _fake_program_dialog(monkeypatch, order)
    _fake_dialog(monkeypatch, order=order, during=lambda: w.update_checker.finished.emit(_release(), None, False))
    w._plugins_checked(PluginUpdates(updated=[("a", "0.1.0", "0.2.0")], seen=["a"]), None, False)
    assert order == ["the summary opens", "the summary closes",
                     "the window of the program opens", "the window of the program closes"]


def test_the_summary_waits_for_any_modal_window(window, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from pyarchinit_photogrammetry.gui import main_window as mw

    w, app = window
    monkeypatch.setattr(mw.MainWindow, "UPDATE_WINDOW_POLL_MS", 20)  # the wait for a window to close, shortened
    shown = _fake_dialog(monkeypatch)
    other = QDialog(w)  # a window of the program, open when the look ends
    other.setModal(True)
    other.show()
    app.processEvents()
    try:
        w._plugins_checked(PluginUpdates(waiting=[("b", "0.3.0", REASON)], seen=["b"]), None, False)
        for _ in range(10):
            time.sleep(0.02)
            app.processEvents()
        assert not shown and w.settings.value("plugins_told", "") == ""  # held back, and not remembered as told
        w._plugins_checked(PluginUpdates(waiting=[("b", "0.4.0", REASON)], seen=["b"]), None, False)  # a newer answer
    finally:
        other.close()
    _wait(app, lambda: shown, "the summary, once the window was closed")
    app.processEvents()
    assert len(shown) == 1 and "0.4.0" in _text(shown[0])  # the newer answer replaced the one that waited
    assert json.loads(w.settings.value("plugins_told")) == ["waiting:b:0.4.0"]


# ---------------------------------------------------------------------------------------- a manual request
def test_a_manual_request_met_by_the_look_that_runs_is_answered(window, monkeypatch):
    w, app = window
    gate, started = threading.Event(), threading.Event()
    calls = []

    def slow_first(seen=None, *, folder=None, update=True, url=None, progress=None):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            gate.wait(10)
        return PluginUpdates(seen=["a"])

    monkeypatch.setattr(plugin_updates, "run", slow_first)
    _fake_dialog(monkeypatch)
    ended = _counting(w)
    before = len(w.log_pane.entries)
    try:
        assert w._check_plugins(manual=False)  # the daily look
        assert started.wait(5)
        assert not w._check_plugins(manual=True)  # asked for while it runs: refused, but the look answers for it too
    finally:
        gate.set()
    _wait(app, lambda: len(ended) == 1, "the daily look to end")
    assert _rows(w)[before:] == [(logging.INFO, "Plugin: nessun aggiornamento")]
    assert w._check_plugins(manual=False)  # that was one request, not every look after it
    _wait(app, lambda: len(ended) == 2, "the next look to end")
    assert _rows(w)[before:] == [(logging.INFO, "Plugin: nessun aggiornamento")]


@pytest.mark.parametrize("program, plugins, text", [
    (True, True, "Controllo degli aggiornamenti..."),
    (True, False, "Controllo degli aggiornamenti... (il controllo dei plugin è già in corso)"),
    (False, True, "Controllo degli aggiornamenti... (il controllo della versione del programma è già in corso)"),
    (False, False, "Controllo degli aggiornamenti gia' in corso"),
])
def test_the_manual_check_says_which_look_is_already_running(window, monkeypatch, program, plugins, text):
    w, app = window
    monkeypatch.setattr(w.update_checker, "start", lambda manual=False, skip=None: program)
    monkeypatch.setattr(w.plugin_checker, "start", lambda *args: plugins)
    w.check_updates()
    assert w.statusBar().currentMessage() == text


# ---------------------------------------------------------------------------------------- the checker
def test_the_checker_reports_in_the_interface_thread(window, tmp_path):
    import shiboken6
    from PySide6.QtCore import QObject, Slot

    _, app = window
    got = []
    owner = QObject()  # the parent of the checkers and the receiver: deleted at the end, here, in the interface thread

    class Receiver(QObject):
        @Slot(object, object, bool)
        def done(self, result, error, manual):
            got.append((result, error, manual, threading.current_thread() is threading.main_thread()))

    receiver = Receiver(owner)
    result = PluginUpdates(seen=["a"])
    calls = []

    def run(seen=None, *, folder=None, update=True, url=None, progress=None):
        calls.append((seen, folder, update))
        return result

    error = plugin_index.PluginError("the plugin index could not be read: down")

    def fail(seen=None, *, folder=None, update=True, url=None, progress=None):
        raise error

    try:
        ok = update_dialog.PluginUpdateChecker(owner, run=run)
        ok.finished.connect(receiver.done)
        assert ok.start(True, ["a"], False, tmp_path)
        bad = update_dialog.PluginUpdateChecker(owner, run=fail)
        bad.finished.connect(receiver.done)
        assert bad.start(False, None, True, tmp_path)
        _wait(app, lambda: len(got) == 2 and not ok.running and not bad.running, "both looks")
        assert (result, None, True, True) in got and (None, error, False, True) in got
        assert calls == [(["a"], tmp_path, False)]  # what start() was given is what run() gets
    finally:
        shiboken6.delete(owner)  # no QObject is left to be deleted by a collection in some other thread


@pytest.mark.parametrize("which", ["program", "plugins"])
def test_a_check_that_ends_after_the_window_closed_is_silent(tmp_path, windows, monkeypatch, caplog, which):
    import shiboken6
    from PySide6.QtCore import QObject

    _, app = windows
    gate = threading.Event()
    unhandled = []
    monkeypatch.setattr(threading, "excepthook", lambda args: unhandled.append(args.exc_value))
    owner = QObject()

    def slow_program(skip=None):
        gate.wait(10)
        return None

    def slow_plugins(seen=None, *, folder=None, update=True, url=None, progress=None):
        gate.wait(10)
        return PluginUpdates()

    if which == "program":
        checker = update_dialog.UpdateChecker(owner, check=slow_program)
        assert checker.start(False, None)
    else:
        checker = update_dialog.PluginUpdateChecker(owner, run=slow_plugins)
        assert checker.start(False, None, True, tmp_path)
    thread = next(t for t in threading.enumerate() if t.name == checker.thread_name)
    with caplog.at_level(logging.DEBUG, logger=update_dialog.__name__):
        shiboken6.delete(owner)  # the window and its checker are gone
        gate.set()
        thread.join(10)
    assert not thread.is_alive() and unhandled == []
    assert any("closed" in record.getMessage() for record in caplog.records)  # said at the debug level, not lost


def test_both_checkers_are_one_check_in_a_thread():
    assert issubclass(update_dialog.UpdateChecker, update_dialog.BackgroundCheck)
    assert issubclass(update_dialog.PluginUpdateChecker, update_dialog.BackgroundCheck)
    assert update_dialog.UpdateChecker.thread_name != update_dialog.PluginUpdateChecker.thread_name


# ---------------------------------------------------------------------------------------- the summary
def test_the_summary_names_each_kind_under_its_own_heading():
    new = _new_entry("nuovo", title="Plugin nuovo", description="Fa una cosa utile", min_app="99.0")
    result = PluginUpdates(
        updated=[("a", "0.1.0", "0.2.0")], available=[("b", "0.4.0", "0.5.0")],
        failed=[("c", "c: the wheel does not match its SHA-256 in the index")],  # as index.install words it
        waiting=[("d", "0.3.0", REASON), ("e", "0.2.0", "installed in /x/my-e, not in /x/e: update it by hand")],
        new=[new])
    text = _text(update_dialog.plugin_summary_html(result))
    lines = text.splitlines()
    assert lines[:2] == ["Aggiornati: attivi dal prossimo avvio", "a 0.1.0 → 0.2.0"]
    assert "Aggiornamenti disponibili" in lines and "b 0.4.0 → 0.5.0" in lines
    assert "Aggiornamenti non riusciti" in lines and "c: the wheel does not match its SHA-256 in the index" in lines
    assert "c: c:" not in text  # the name stands in front of the line once
    assert "In attesa" in lines  # one heading for all that wait, whatever the reason
    assert f"d 0.3.0: {REASON}" in lines
    assert "e 0.2.0: installed in /x/my-e, not in /x/e: update it by hand" in lines
    assert "richiede" not in text
    assert "Nuovi nel repository dei plugin" in lines
    assert "Plugin nuovo 0.1.0" in lines and "Fa una cosa utile" in lines
    assert REASON in lines  # a new plugin this program cannot install says why
    order = [lines.index(h) for h in ("Aggiornati: attivi dal prossimo avvio", "Aggiornamenti disponibili",
                                       "Aggiornamenti non riusciti", "In attesa", "Nuovi nel repository dei plugin")]
    assert order == sorted(order)
    # a kind with nothing in it has no heading
    only = _text(update_dialog.plugin_summary_html(PluginUpdates(failed=[("c", "no space left")])))
    assert only.splitlines()[:1] == ["Aggiornamenti non riusciti"] and "In attesa" not in only and "Nuovi" not in only
    assert update_dialog.plugin_summary_html(PluginUpdates(seen=["a"])) == ""


def test_the_summary_shows_the_title_in_the_language_of_the_interface(english):
    entry = _new_entry("nuovo", title="New plugin", description="Does a thing",
                       i18n={"it": {"title": "Plugin nuovo", "description": "Fa una cosa"}})
    assert "New plugin" in _text(update_dialog.plugin_summary_html(PluginUpdates(new=[entry])))
    i18n.set_current("it")
    text = _text(update_dialog.plugin_summary_html(PluginUpdates(new=[entry])))
    assert "Plugin nuovo" in text and "Fa una cosa" in text and "New plugin" not in text


def test_the_summary_escapes_what_the_index_says():
    entry = _new_entry("x", title="R&D <b>tools</b>", description="<img src='file:///etc/passwd'>", version="1<2")
    html = update_dialog.plugin_summary_html(PluginUpdates(new=[entry], failed=[("a", "<script>boom()</script>")],
                                                             waiting=[("b<i>", "0.3.0", "needs <u>more</u>")]))
    assert "<script" not in html and "<img" not in html and "<u>" not in html and "<i>" not in html
    text = _text(html)
    assert "R&D <b>tools</b> 1<2" in text and "<img src='file:///etc/passwd'>" in text
    assert "a: <script>boom()</script>" in text and "b<i> 0.3.0: needs <u>more</u>" in text


def test_the_summary_in_english_and_the_reasons_go_through_translate(english, monkeypatch):
    # the reasons are engine messages: the catalog may know them, as it knows other messages of the engine
    monkeypatch.setitem(i18n.catalog(), REASON, "ENGLISH REASON")
    text = _text(update_dialog.plugin_summary_html(PluginUpdates(
        updated=[("a", "0.1.0", "0.2.0")], waiting=[("b", "0.3.0", REASON)], failed=[("c", REASON)],
        available=[("d", "0.1.0", "0.2.0")], new=[_new_entry("nuovo", title="New one", min_app="99.0")])))
    lines = text.splitlines()
    assert "Updated: active from the next start" in lines and "Updates available" in lines
    assert "Updates that failed" in lines and "Waiting" in lines and "New in the plugin repository" in lines
    assert "b 0.3.0: ENGLISH REASON" in lines and "c: ENGLISH REASON" in lines
    assert lines.count("ENGLISH REASON") == 1  # the new plugin that this program cannot install, as well


def test_the_summary_window_offers_what_fits(window):
    new = _new_entry("nuovo", title="Plugin nuovo")
    staged = update_dialog.PluginUpdatesDialog(PluginUpdates(updated=[("a", "0.1.0", "0.2.0")]))
    assert set(staged.buttons) == {"restart", "manager", "close"} and staged.action is None
    assert "a 0.1.0 → 0.2.0" in staged.summary.toPlainText()
    assert staged.buttons["restart"].text() == "Riavvia ora"
    assert staged.buttons["manager"].text() == "Apri il gestore dei plugin"
    assert staged.buttons["close"].text() == "Chiudi"
    staged.buttons["restart"].click()
    assert staged.action == "restart"
    for nothing_staged in (PluginUpdates(new=[new]), PluginUpdates(available=[("a", "0.1.0", "0.2.0")]),
                           PluginUpdates(waiting=[("a", "0.3.0", REASON)]), PluginUpdates(failed=[("a", "no")])):
        d = update_dialog.PluginUpdatesDialog(nothing_staged)  # no restart to offer
        assert set(d.buttons) == {"manager", "close"}
        d.buttons["manager"].click()
        assert d.action == "manager"
        again = update_dialog.PluginUpdatesDialog(nothing_staged)
        again.buttons["close"].click()
        assert again.action is None


# ---------------------------------------------------------------------------------------- the log
def test_applied_updates_are_written_in_the_log(windows, tmp_path, monkeypatch):
    make, app = windows
    folder = tmp_path / "plugins-of-this-test"  # a folder is put in order once per process: not the shared one
    _install_folder(folder, "a")
    staged = plugin_updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)
    assert staged.updated == [("a", "0.1.0", "0.2.0")]  # an earlier session downloaded it
    monkeypatch.setenv("PPG_PLUGINS_DIR", str(folder))
    w = make()
    assert w.plugins.applied == [("a", "0.1.0", "0.2.0")] and w.plugins.get("a").version == "0.2.0"
    assert (logging.INFO, "Plugin a aggiornato dalla 0.1.0 alla 0.2.0") in _rows(w)
    assert not any("updated from" in text for _level, text in _rows(w))  # not once more, in English, from the engine
    monkeypatch.setenv("PPG_PLUGINS_DIR", str(tmp_path / "other-plugins"))  # a start with nothing staged
    assert not any("aggiornato dalla" in text for _level, text in _rows(make()))


def test_applied_updates_are_written_even_when_the_plugins_cannot_be_listed(windows, tmp_path, monkeypatch):
    make, app = windows
    folder = tmp_path / "plugins-that-fail"
    _install_folder(folder, "a")
    plugin_updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)
    monkeypatch.setenv("PPG_PLUGINS_DIR", str(folder))

    def broken(self):
        raise OSError("the plugin folder cannot be read")

    monkeypatch.setattr(Manager, "discover", broken)
    w = make()
    rows = _rows(w)
    assert (logging.INFO, "Plugin a aggiornato dalla 0.1.0 alla 0.2.0") in rows  # put in place before the failure
    assert any("Plugin non caricati" in text for _level, text in rows)


# ---------------------------------------------------------------------------------------- the plugin manager
def test_the_plugin_manager_says_when_an_update_is_ready(windows, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt

    from pyarchinit_photogrammetry.gui.plugins_dialog import PluginsDialog

    make, app = windows
    folder = tmp_path / "plugins"
    for name in "abcde":
        _install_folder(folder, name)
    _install_folder(folder, "g", version="0.2.0")  # the index says "0.2": the same version
    _install_folder(folder, "h", version="0.3.0")  # newer than the index has
    (folder / "e").rename(folder / "my-e")  # by hand: an update would go to <folder>/e
    entries = {}
    for name, version, extra in [("a", "0.2.0", {}), ("b", "0.2.0", {}), ("c", "0.3.0", {"min_app": "99.0"}),
                                 ("d", "0.1.0", {}), ("e", "0.2.0", {}), ("f", "0.1.0", {}), ("g", "0.2", {}),
                                 ("h", "0.2.0", {})]:
        wheel = _wheel(tmp_path / f"{name}-{version}-py3-none-any.whl", name, version=version)
        entries[name] = _entry(wheel, name, version=version, **extra)
    monkeypatch.setenv("PPG_PLUGINS_DIR", str(folder))
    monkeypatch.setenv("PPG_PLUGIN_INDEX", _index(tmp_path, list(entries.values())))
    w = make()
    # the look of the day downloaded the update while the window was open
    staged = plugin_updates.check([plugin_index.Entry.from_dict(entries["a"])], _installed(folder), None, folder)
    assert staged.updated == [("a", "0.1.0", "0.2.0")]
    dialog = PluginsDialog(w)
    dialog.fetch()
    table = dialog.available
    notes = {table.item(r, 0).data(Qt.ItemDataRole.UserRole): table.item(r, 4).text() for r in range(table.rowCount())}
    assert notes["a"] == "pronta la 0.2.0: attiva dal prossimo avvio"  # not "aggiornabile": it is downloaded already
    assert notes["b"] == "installata la 0.1.0: aggiornabile"
    assert notes["c"] == REASON
    assert notes["d"] == "installato (0.1.0)"
    assert notes["g"] == "installato (0.2.0)" and notes["h"] == "installato (0.3.0)"  # 0.2 is 0.2.0, not "aggiornabile"
    assert "update it by hand" in notes["e"] and str(folder / "my-e") in notes["e"]
    assert notes["f"] == ""
    dialog.close()
