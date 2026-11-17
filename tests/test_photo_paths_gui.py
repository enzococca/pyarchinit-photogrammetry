"""The photos missing when a project is opened and *Cambia percorso...* in the window (issue #30), headless."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from test_photo_paths import _photo, _project  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def gui(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    made = []

    def window(doc):
        w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
        w.runner.show_dialog = False
        w.show()
        qapp.processEvents()
        made.append(w)
        return w

    yield mw, window
    for w in made:
        w.setWindowModified(False)
        w.close()
    qapp.processEvents()


def _log(w) -> str:
    t = w.log_pane.table
    return "\n".join(t.item(r, 2).text() for r in range(t.rowCount()) if t.item(r, 2) is not None)


def test_photos_missing_are_said_when_a_project_is_opened(gui, tmp_path, monkeypatch, qapp):
    import pyarchinit_photogrammetry as ppg

    mw, window = gui
    doc, ch = _project(tmp_path, n=3)
    Path(ch.cameras[0].path).unlink()
    Path(ch.cameras[2].path).write_bytes(b"")  # kept online only
    asked = []
    monkeypatch.setattr(mw, "choice", lambda *a, **k: asked.append(a) or 1)  # "Più tardi"
    w = window(ppg.Document())
    w.open_document(str(doc.path))
    qapp.processEvents()
    assert len(asked) == 1
    assert asked[0][4] == ["Ricollega foto...", "Più tardi"]
    assert "2 foto" in asked[0][3]
    assert "2 foto non trovate" in _log(w)
    # all found: nothing said
    asked.clear()
    for cam in ch.cameras:
        _photo(Path(cam.path), 60)
    w.open_document(str(doc.path))
    qapp.processEvents()
    assert asked == [] and w.check_photos_on_open() == 0


def test_relink_offered_on_open_links_the_missing_photos(gui, tmp_path, monkeypatch, qapp):
    import pyarchinit_photogrammetry as ppg

    mw, window = gui
    doc, ch = _project(tmp_path, n=2)
    shutil.move(str(tmp_path / "foto"), str(tmp_path / "spostate"))
    monkeypatch.setattr(mw, "choice", lambda *a, **k: 0)  # "Ricollega foto..."
    monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path / "spostate"))
    w = window(ppg.Document.open(doc.path))
    assert w.check_photos_on_open() == 2
    assert all(Path(c.path).is_file() for c in w.doc.chunk.cameras)


def test_missing_photos_are_marked_in_the_photos_pane(gui, tmp_path):
    import pyarchinit_photogrammetry as ppg
    from PySide6.QtCore import Qt

    mw, window = gui
    doc, ch = _project(tmp_path, n=2)
    gone = ch.cameras[1].path
    Path(gone).unlink()
    w = window(ppg.Document.open(doc.path))
    items = [w.photos.item(i) for i in range(w.photos.count())]
    assert "mancante" not in items[0].toolTip()
    assert "mancante" in items[1].toolTip() and gone in items[1].toolTip()
    assert items[1].foreground().color() != items[0].foreground().color()
    assert items[1].data(Qt.ItemDataRole.UserRole).label == "IMG_1"


def test_change_path_of_one_photo_and_undo(gui, tmp_path, monkeypatch, qapp):
    import pyarchinit_photogrammetry as ppg

    mw, window = gui
    doc, _ch = _project(tmp_path, n=2)
    w = window(ppg.Document.open(doc.path))
    cam = w.chunk.cameras[0]
    before = cam.path
    other = _photo(tmp_path / "altrove" / "nuovo_nome.jpg", 90)
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(other), ""))
    assert w.change_photo_paths([cam])
    assert cam.path == str(other.resolve())
    assert w.isWindowModified()
    w.history.undo()
    qapp.processEvents()
    assert cam.path == before
    w.history.redo()
    assert cam.path == str(other.resolve())


def test_change_path_of_several_photos_takes_a_folder(gui, tmp_path, monkeypatch, qapp):
    import pyarchinit_photogrammetry as ppg

    mw, window = gui
    doc, _ch = _project(tmp_path, n=3)
    copy = tmp_path / "ssd"
    copy.mkdir()
    for p in (tmp_path / "foto").iterdir():
        shutil.copy2(p, copy / p.name)
    w = window(ppg.Document.open(doc.path))
    cams = w.chunk.cameras
    before = [c.path for c in cams]
    monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", lambda *a, **k: str(copy))
    assert w.change_photo_paths([cams[0], cams[2]])
    assert [Path(c.path).parent.name for c in cams] == ["ssd", "foto", "ssd"]
    w.history.undo()
    assert [c.path for c in cams] == before


def test_change_path_is_in_the_context_menu_and_the_workflow_menu(gui, tmp_path):
    import pyarchinit_photogrammetry as ppg

    mw, window = gui
    doc, _ch = _project(tmp_path, n=1)
    w = window(ppg.Document.open(doc.path))
    texts = [a.text() for a in w.findChildren(mw.QAction)]
    assert "Cambia percorso delle foto selezionate..." in texts  # Flusso di lavoro
    menu = w._photos_menu_for(w.chunk.cameras)
    assert "Cambia percorso..." in [a.text() for a in menu.actions()]
    menu.deleteLater()


def test_a_missing_folder_costs_one_look_in_the_photos_pane(gui, tmp_path, monkeypatch):
    import pyarchinit_photogrammetry as ppg
    from test_photo_paths import _count

    mw, window = gui
    doc, ch = _project(tmp_path, n=5)
    folder = tmp_path / "foto"
    shutil.rmtree(folder)
    w = window(ppg.Document.open(doc.path))
    calls = _count(monkeypatch, folder)
    w.photos.set_chunk(w.chunk)
    assert len(calls) == 1, calls  # one listing of the folder, no file looked at
    assert all("mancante" in w.photos.item(i).toolTip() for i in range(5))


def test_photos_of_a_folder_there_cost_one_listing_in_the_photos_pane(gui, tmp_path, monkeypatch):
    import pyarchinit_photogrammetry as ppg
    from test_photo_paths import _count

    mw, window = gui
    doc, ch = _project(tmp_path, n=5)
    Path(ch.cameras[3].path).unlink()
    w = window(ppg.Document.open(doc.path))
    calls = _count(monkeypatch, tmp_path / "foto")
    w.photos.set_chunk(w.chunk)
    assert calls == [("scandir", str(Path(ch.cameras[0].path).parent))], calls
    assert ["mancante" in w.photos.item(i).toolTip() for i in range(5)] == [False, False, False, True, False]


def test_the_tooltip_of_a_photo_without_markers(gui, tmp_path):
    import pyarchinit_photogrammetry as ppg

    mw, window = gui
    doc, ch = _project(tmp_path, n=1)
    Path(ch.cameras[0].path).unlink()
    w = window(ppg.Document.open(doc.path))
    w.photos._flags[w.chunk.cameras[0].key] = (0, 0, [])
    assert "Marker" not in w.photos._tooltip(w.chunk.cameras[0])


def test_change_path_dialog_lists_every_image_kind_and_opens_in_the_raw_folder(gui, tmp_path, monkeypatch):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.chunk import IMAGE_EXTENSIONS

    mw, window = gui
    doc, ch = _project(tmp_path, n=1)
    raws = tmp_path / "raw_originali"
    raws.mkdir()
    ch.cameras[0].meta["raw"] = {"path": str(raws / "IMG_0.dng"), "size": 1}
    doc.save()
    w = window(ppg.Document.open(doc.path))
    asked = []
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: asked.append(a) or ("", ""))
    assert not w.change_photo_paths([w.chunk.cameras[0]])
    _parent, _title, start, filters = asked[0]
    assert Path(start) == raws.resolve()
    assert all(f"*{e}" in filters for e in IMAGE_EXTENSIONS) and "*.dng" in filters
