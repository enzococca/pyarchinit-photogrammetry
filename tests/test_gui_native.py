"""The window actions of the native functions (offscreen): duplicate a chunk with the photos chosen,
enable or disable photos, the region on three markers and the normal map."""
from __future__ import annotations

import os
import shutil
import time

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(qapp, processed, tmp_path):
    from PySide6.QtCore import QSettings

    import pyarchinit_photogrammetry as ppg

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    src, _ch, _ = processed
    d = tmp_path / "progetto"
    d.mkdir()
    shutil.copy2(src.path, d / src.path.name)
    shutil.copytree(src.files_dir, d / src.files_dir.name)
    shutil.copytree(src.path.parent / "photos", d / "photos")
    doc = ppg.Document.open(d / src.path.name)
    w = MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qapp.processEvents()
    yield w
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


def _wait(w, qapp, seconds=300):
    end = time.monotonic() + seconds
    while w.runner.busy and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.02)
    qapp.processEvents()
    assert not w.runner.busy


def _select(w, keys):
    from PySide6.QtCore import Qt

    w.photos.clearSelection()
    for i in range(w.photos.count()):
        it = w.photos.item(i)
        if it.data(Qt.ItemDataRole.UserRole).key in keys:
            it.setSelected(True)


def _six(ch):
    """Six nadir photos of the trench in a block of two rows by three (inside the grid of 4 x 5): each has
    partners among the others for its depth map."""
    return [ch.camera(f"IMG_{i:04d}") for i in (6, 7, 8, 11, 12, 13)]


def _menu_action(w, monkeypatch, open_menu, text):
    """Trigger the entry ``text`` of the context menu that ``open_menu()`` opens: the window's QMenu
    stood in by one whose exec triggers that entry instead of showing itself."""
    from PySide6.QtWidgets import QMenu

    from pyarchinit_photogrammetry.gui import main_window as mw

    found = []

    class Menu(QMenu):
        def exec(self, *_a, **_k):
            for a in self.actions():
                if a.text() == text:
                    found.append(a.text())
                    a.trigger()
                    return a
            return None

    monkeypatch.setattr(mw, "QMenu", Menu)
    open_menu()
    assert found, f"no menu entry {text!r}"


def test_window_duplicate_and_disable(win, qapp, monkeypatch):
    from PySide6.QtCore import QPoint, Qt

    from pyarchinit_photogrammetry.gui import main_window as mw

    w = win
    ch = w.chunk
    assert ch.label == "Saggio 1"
    six = _six(ch)
    keys = {c.key for c in six}
    _select(w, keys)
    assert {c.key for c in w.selected_cameras()} == keys
    asked = []

    def choose(_parent, _kind, _title, text, options, *a, **k):
        asked.append(options)
        return next(i for i, o in enumerate(options) if "selezionate" in o)

    monkeypatch.setattr(mw, "choice", choose)
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, parent, title, fields, intro="": {
        f.name: f.default for f in fields}))
    _menu_action(w, monkeypatch, lambda: w._workspace_menu("chunk", ch, None, QPoint(0, 0)), mw.tr("Duplica chunk..."))
    _wait(w, qapp)
    assert asked and len(asked[0]) == 3
    assert [c.label for c in w.doc.chunks] == ["Saggio 1", "Saggio 1 (copia)"]
    dup = w.doc.chunks[1]
    assert {c.key for c in dup.cameras} == keys and len(ch.cameras) == 28
    assert w.chunk is dup  # the copy becomes the active chunk
    # disable two photos of the copy from the Photos pane
    w._activate_chunk(dup)
    qapp.processEvents()
    two = sorted(keys)[:2]
    _select(w, set(two))
    before = {}
    for i in range(w.photos.count()):
        it = w.photos.item(i)
        before[it.data(Qt.ItemDataRole.UserRole).key] = it.icon().pixmap(64, 64).toImage()
    _menu_action(w, monkeypatch, lambda: w._photos_menu(QPoint(0, 0)), mw.tr("Disattiva le {0} foto selezionate", 2))
    assert sorted(c.key for c in dup.cameras if not c.enabled) == two
    assert len(dup.aligned_cameras) == 4
    for i in range(w.photos.count()):
        it = w.photos.item(i)
        cam = it.data(Qt.ItemDataRole.UserRole)
        if cam.key in two:  # the badge of the disabled photos (the others may have got their thumbnail meanwhile)
            assert it.icon().pixmap(64, 64).toImage() != before[cam.key]
        assert (mw.tr("disattivata") in it.toolTip()) == (cam.key in two)
    assert w.isWindowModified()
    _menu_action(w, monkeypatch, lambda: w._photos_menu(QPoint(0, 0)), mw.tr("Attiva le {0} foto selezionate", 2))
    assert all(c.enabled for c in dup.cameras)


def test_window_duplicate_with_the_photos_of_the_selected_area(win, qapp, monkeypatch):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.gui import main_window as mw

    w = win
    ch = w.chunk
    with __import__("rasterio").open(ch.orthomosaic_path) as o:
        b = o.bounds
    cx, cy = (b.left + b.right) / 2, (b.bottom + b.top) / 2
    area = box(cx - 0.2, cy - 0.2, cx + 0.2, cy + 0.2)
    monkeypatch.setattr(w, "selected_polygon", lambda: area)
    monkeypatch.setattr(mw, "choice", lambda _p, _k, _t, _x, options, *a, **k:
                        next(i for i, o in enumerate(options) if "area" in o))
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, parent, title, fields, intro="": {
        "label": "Area"}))
    w.duplicate_chunk(ch)
    _wait(w, qapp)
    dup = w.doc.chunks[-1]
    assert dup.label == "Area" and 0 < len(dup.cameras) <= len(ch.cameras)


def test_window_region_on_markers_and_normal_map(win, qapp, monkeypatch):
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = win
    ch = w.chunk
    placed = [m for m in ch.markers if m.position is not None]
    labels = None
    for i in range(len(placed) - 2):
        ms = placed[i:i + 3]
        pts = ch.to_world(np.array([m.position for m in ms], float))
        if np.linalg.norm(np.cross(pts[1] - pts[0], pts[2] - pts[0])) > 1e-3:
            labels = [m.key for m in ms]
            break
    assert labels is not None
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, parent, title, fields, intro="": {
        "a": labels[0], "b": labels[1], "c": labels[2], "thickness": 0.15}))
    w.region_from_markers()
    assert ch.region is not None and np.isclose(ch.region.size[2], 0.15)
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, parent, title, fields, intro="": {
        "source": "dem"}))
    w.build_normal_map()
    _wait(w, qapp)
    assert ch.product_path("normal_map").exists() and ch.meta["normal_map"]["source"] == "dem"
    assert w.ortho_view.path is not None and str(w.ortho_view.path).endswith("normal_map.tif")


def test_selected_polygon_is_public(win):
    from shapely.geometry import Polygon

    w = win
    assert w.selected_polygon() is None
    sq = [(0, 0), (1, 0), (1, 1), (0, 1)]
    w.ortho_view.selected_features = lambda: [("Disegni", 0, {"_geom": ("Polygon", sq)})]
    w.ortho_view.layers["Disegni"] = {"kind": "polygon"}
    assert w.selected_polygon().equals(Polygon(sq))
    assert w._selected_polygon().equals(w.selected_polygon())  # the old name stays
