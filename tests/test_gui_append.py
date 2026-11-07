"""*File > Aggiungi progetti...*: other projects appended as new chunks, one step of Modifica > Annulla, a summary."""
from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _settle(qapp) -> None:
    from PySide6.QtCore import QCoreApplication, QEvent

    for _ in range(4):
        qapp.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _wait(w, qapp, seconds: float = 60.0) -> None:
    end = time.monotonic() + seconds
    while w.runner.busy and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    _settle(qapp)
    assert not w.runner.busy


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    doc = ppg.Document()
    doc.add_chunk("Chunk 1")
    doc.save(tmp_path / "dst" / "mio.ppg")
    w = MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.resize(1000, 700)
    w.show()
    _settle(qapp)
    shown = []
    monkeypatch.setattr(w, "_info", lambda title, text: shown.append((title, text)))
    w.shown = shown
    yield w, mw
    w.setWindowModified(False)
    w.close()
    _settle(qapp)


def test_append_from_the_file_menu_one_undo_step_and_the_summary(window, qapp, tmp_path, monkeypatch):
    from test_append import _broken_psx, make_source

    w, mw = window
    src = make_source(tmp_path / "src")
    bad = _broken_psx(tmp_path / "rotto.psx")
    asked = []
    monkeypatch.setattr(mw, "choice", lambda *a, **k: asked.append(a[3]) or 0)  # File...
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(src), str(bad)], ""))
    ticked = []

    def ask(cls, parent, title, fields, intro=""):
        ticked.append([(f.label, f.choices) for f in fields])
        return {f.name: [1] for f in fields}  # Saggio B only

    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(ask))
    menu_file = next(a.menu() for a in w.menuBar().actions() if a.text().replace("&", "") == "File")
    assert w.a_append in menu_file.actions() and w.a_ws_append in w.workspace_toolbar.actions()
    w.a_append.trigger()
    _wait(w, qapp)
    assert ticked == [[("Chunk di scavo.ppg", [(0, "Chunk 1"), (1, "Saggio B")])]]
    doc = w.doc
    assert [c.label for c in doc.chunks] == ["Chunk 1", "Saggio B"]
    new = doc.chunks[1]
    assert w.chunk is new  # the active chunk was empty: the first appended takes its place
    assert w.isWindowModified()
    title, text = w.shown[-1]
    assert "1 non aggiunti" in title and "rotto.psx" in text and "Saggio B" in text and "Allinea chunk" in text
    # one step undoes it all, its folder set aside; redo brings it back
    assert w.history.undo_text() and "Aggiungi progetti" in w.history.undo_text()
    w.undo()
    _settle(qapp)
    assert [c.label for c in doc.chunks] == ["Chunk 1"] and w.chunk is doc.chunks[0]
    w.redo()
    _settle(qapp)
    assert [c.label for c in doc.chunks] == ["Chunk 1", "Saggio B"] and w.chunk is new


def test_append_keeps_an_active_chunk_with_photos_and_the_folders_go_when_never_saved(window, qapp, tmp_path,
                                                                                      monkeypatch):
    from test_append import _photo, make_source

    w, mw = window
    doc = w.doc
    doc.chunks[0].add_photos([_photo(tmp_path / "mie" / "a.jpg", 30)])
    first = doc.chunks[0]
    src = make_source(tmp_path / "src")
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, p, t, fields, intro="": {
        f.name: [0, 1] for f in fields}))
    w.append_projects(sources=[src])
    _wait(w, qapp)
    assert [c.label for c in doc.chunks] == ["Chunk 1", "scavo - Chunk 1", "Saggio B"]
    assert w.chunk is first
    folder = doc.files_dir / f"chunk_{doc.chunks[1].key}"
    assert folder.is_dir()
    w._settle_removals(doc)  # the session ends without a save: the project on disk has no such chunk
    assert not folder.exists()


def test_sections_of_the_source_come_and_go_with_the_undo(window, qapp, tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from test_append import make_linked_source

    w, mw = window
    src = make_linked_source(tmp_path / "src")
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, p, t, fields, intro="": {
        f.name: [0, 1] for f in fields}))
    w.append_projects(sources=[src])  # two chunks: the checklist
    _wait(w, qapp)
    labels = [s["label"] for s in w.sections]
    assert labels == ["Sezione 1"] and w.sections[0].get("ppg_id")
    file = Path(w.doc.path).parent / "sezioni" / "sezioni.json"
    assert [s["label"] for s in json.loads(file.read_text())] == ["Sezione 1"]
    assert "Tell Sorgente" in w.shown[-1][1]  # the site of the source, said
    w.undo()
    _settle(qapp)
    assert w.sections == [] and json.loads(file.read_text()) == []
    w.redo()
    _settle(qapp)
    assert [s["label"] for s in w.sections] == ["Sezione 1"] and len(json.loads(file.read_text())) == 1


def test_no_other_edit_of_the_chunks_while_an_append_runs(window, qapp, tmp_path):
    import threading

    w, mw = window
    doc = w.doc
    other = doc.add_chunk("Altro")
    release = threading.Event()
    w._appending = True
    assert w.run_task("Aggiunta di progetti", lambda p: release.wait(30), refresh=False, save=False)
    _settle(qapp)
    try:
        assert not w.a_ws_add_chunk.isEnabled() and not w.a_ws_append.isEnabled()
        w.add_chunk()  # refused at once: no question, no chunk
        w._workspace_item("chunk", other, None)
        w.rename_chunk(other)
        w.set_chunks_enabled([other], False)
        assert [c.label for c in doc.chunks] == ["Chunk 1", "Altro"]
        assert w.chunk is doc.chunks[0] and other.enabled
    finally:
        release.set()
    _wait(w, qapp)
    assert w._appending is False and w.a_ws_add_chunk.isEnabled()
