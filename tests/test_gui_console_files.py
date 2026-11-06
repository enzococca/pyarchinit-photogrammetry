"""The Python console runs a file (since 1.7.4): *Esegui file .py...*, *Riesegui*, *Recenti*, *Apri nell'editor*,
*Pulisci*, a ``.py`` dropped on the console; ``window.run_python_file(path)`` runs it in the worker as *Esegui script
Python...* does (one step of Modifica > Annulla), its output in the console as it comes, the names it defined copied
into the console as IPython's ``%run`` does."""
from __future__ import annotations

import os
import sys
import threading
import types
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    w = MainWindow(ppg.Document(), autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qapp.processEvents()
    yield w
    w.runner.wait()
    w.setWindowModified(False)
    w.close()


def _run(w, qapp, path) -> bool:
    started = w.run_python_file(path)
    w.runner.wait()
    for _ in range(3):
        qapp.processEvents()
    return started


def _text(w) -> str:
    return w.console.out.toPlainText()


def test_a_file_prints_defines_names_and_sees_its_own_argv(window, qapp, tmp_path):
    w = window
    folder = tmp_path / "scripts"
    folder.mkdir()
    path = folder / "conta.py"
    path.write_text("import sys\nimport json as _j\nimport os\n"
                    "print('ciao dal file')\n"
                    "a = 2\n_hidden = 3\n"
                    "def f():\n    return a + 1\n"
                    "argv = list(sys.argv)\nhere = __file__\nfirst = sys.path[0]\n", encoding="utf-8")
    assert _run(w, qapp, path)
    out = _text(w)
    assert f">>> run {path}" in out and "ciao dal file" in out
    assert out.index(f">>> run {path}") < out.index("ciao dal file")
    last = out.rstrip("\n").splitlines()
    assert any(line.startswith("<<< ok") for line in last[-3:])
    names = w.console.console.locals
    assert names["a"] == 2 and names["f"]() == 3
    assert "_hidden" not in names and "os" not in names and "_j" not in names  # private names and modules stay out
    assert names["argv"] == [str(path)] and names["here"] == str(path) and names["first"] == str(folder)
    assert sys.argv != [str(path)] and str(folder) not in sys.path  # only during the run
    assert "Nuovi nomi: a, argv, f, first, here" in out
    assert w.console.b_rerun.isEnabled() and "conta.py" in w.console.b_rerun.toolTip()
    w.console.run_source("b = f() * 10")  # the console goes on with them
    assert names["b"] == 30


def test_output_comes_while_the_file_runs(window, qapp, tmp_path):
    w = window
    gate = threading.Event()
    sys.modules["_ppg_console_gate"] = types.SimpleNamespace(gate=gate)
    try:
        path = tmp_path / "lento.py"
        path.write_text("import _ppg_console_gate as g\nprint('primo', flush=True)\n"
                        "g.gate.wait(20)\nprint('secondo')\n", encoding="utf-8")
        assert w.run_python_file(path)
        import time

        t0 = time.monotonic()
        while "primo" not in _text(w) and time.monotonic() - t0 < 15:
            qapp.processEvents()
            time.sleep(0.01)
        assert "primo" in _text(w) and "secondo" not in _text(w) and w.runner.busy
    finally:
        gate.set()
        w.runner.wait()
        qapp.processEvents()
        sys.modules.pop("_ppg_console_gate", None)
    assert "secondo" in _text(w)


def test_a_file_that_fails_shows_its_traceback_and_is_one_step(window, qapp, tmp_path):
    w = window
    path = tmp_path / "rotto.py"
    path.write_text("doc.meta['dal_file'] = 1\nx = 5\nraise ValueError('boom')\n", encoding="utf-8")
    entries = len(w.history.entries)
    assert _run(w, qapp, path)
    out = _text(w)
    assert "ValueError: boom" in out and "rotto.py" in out and "Traceback" in out
    assert "scripts.py" not in out  # the frames of the program are left out: only the file's
    assert any(line.startswith("<<< errore") for line in out.splitlines())
    assert w.console.console.locals.get("x") == 5  # what it defined before the error, as %run does
    assert len(w.history.entries) == entries + 1 and w.doc.meta.get("dal_file") == 1
    w.undo()  # the whole file in one step: nothing left half-done
    assert "dal_file" not in w.doc.meta


def test_a_file_while_another_step_runs_is_refused(window, qapp, tmp_path):
    w = window
    path = tmp_path / "attesa.py"
    path.write_text("print('non dovrei girare')\n", encoding="utf-8")
    gate = threading.Event()
    assert w.runner.run("Attesa", lambda progress: gate.wait(20))
    try:
        assert not w.run_python_file(path)
        qapp.processEvents()
        assert "Un'elaborazione è in corso: il file non parte" in _text(w)
        assert f">>> run {path}" not in _text(w)
    finally:
        gate.set()
        w.runner.wait()
        qapp.processEvents()
    assert "non dovrei girare" not in _text(w)


def test_recent_files_rerun_and_clear(window, qapp, tmp_path):
    from pyarchinit_photogrammetry.gui import app_settings

    w = window
    c = w.console
    assert not c.b_rerun.isEnabled() and not c.b_edit.isEnabled()
    paths = []
    for i in range(12):
        p = tmp_path / f"s{i:02d}.py"
        p.write_text(f"n{i} = {i}\nprint('giro {i}')\n", encoding="utf-8")
        paths.append(p)
    for p in paths:
        assert _run(w, qapp, p)
    recent = list(app_settings().value("console/recent", []) or [])
    assert recent == [str(p) for p in reversed(paths)][:10]  # the newest first, ten at most
    paths[-2].unlink()
    c.refresh_recent()
    shown = [a.data() for a in c.recent_menu.actions() if a.data()]
    assert str(paths[-2]) not in shown and shown[0] == str(paths[-1]) and len(shown) == 9  # missing files dropped
    assert str(paths[-2]) not in (app_settings().value("console/recent", []) or [])
    c.b_clear.click()
    assert _text(w) == ""
    c.b_rerun.click()  # the last file again
    w.runner.wait()
    qapp.processEvents()
    assert "giro 11" in _text(w)
    c.recent_menu.actions()[1].trigger()  # one of the list
    w.runner.wait()
    qapp.processEvents()
    assert "giro 9" in _text(w)


def test_a_py_dropped_on_the_console_runs_and_others_are_refused(window, qapp, tmp_path):
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent

    w = window
    c = w.console
    path = tmp_path / "trascinato.py"
    path.write_text("print('trascinato qui')\n", encoding="utf-8")
    other = tmp_path / "note.txt"
    other.write_text("niente", encoding="utf-8")

    def drop(p: Path) -> None:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p))])
        ev = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
        c.dropEvent(ev)

    drop(other)
    assert "note.txt" in _text(w) and not w.runner.busy
    assert c.accepts(mime_with(other)) is False and c.accepts(mime_with(path)) is True
    drop(path)
    w.runner.wait()
    qapp.processEvents()
    assert "trascinato qui" in _text(w)


def mime_with(p: Path):
    from PySide6.QtCore import QMimeData, QUrl

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p))])
    return mime


def test_open_in_the_editor_and_the_menu_entry(window, qapp, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = window
    path = tmp_path / "da_vedere.py"
    source = "valore = 41 + 1\nprint(valore)\n"
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), "")))
    w.run_script()  # *Strumenti > Esegui script Python...*: the same way as the console's button
    w.runner.wait()
    qapp.processEvents()
    assert f">>> run {path}" in _text(w) and "42" in _text(w)
    assert w.console.console.locals["valore"] == 42
    assert w.console.b_edit.isEnabled()
    w.console.b_edit.click()
    assert w.assistant.editor.toPlainText() == source
