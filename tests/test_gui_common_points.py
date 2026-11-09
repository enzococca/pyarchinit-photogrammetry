"""Flusso di lavoro > Punti in comune: two chunks side by side, the views linked through the current transforms, a tie
made by a click in each (one step of Modifica > Annulla with both markers), Esc dropping a half tie, an end dragged to
another point, an end measured on a photo. Small synthetic rasters in local coordinates only (no real coordinates)."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

import pyarchinit_photogrammetry as ppg  # noqa: E402
from pyarchinit_photogrammetry.core.camera import Camera, Sensor  # noqa: E402
from pyarchinit_photogrammetry.core.pointcloud import PointCloud  # noqa: E402
from pyarchinit_photogrammetry.engine import ties  # noqa: E402
from pyarchinit_photogrammetry.engine.dem import write_geotiff  # noqa: E402

SHIFT = np.array([2.0, -1.0, 0.0])  # how far B moved (wrongly) after its rasters were built
OLD = np.array([10.0, 5.0, 0.0])  # B's internal frame: the true places minus this


def _surface(x, y):
    return 0.2 * np.sin(x * 1.1) + 0.1 * np.cos(y * 1.6)


def _translation(t) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = t
    return m


def _rasters(ch, frame, cell_dem: float, cell_ortho: float, x0=-1.0, y1=5.0, size=(6.0, 6.0)) -> None:
    """DEM and orthophoto of the surface, stamped as built in ``frame`` (right then: the file's coordinates are the
    true places of the surface)."""
    rng = np.random.default_rng(ch.key + 3)
    for name, cell in (("dem", cell_dem), ("orthomosaic", cell_ortho)):
        cols, rows = int(size[0] / cell), int(size[1] / cell)
        if name == "dem":
            xs = x0 + (np.arange(cols) + 0.5) * cell
            ys = y1 - (np.arange(rows) + 0.5) * cell
            gx, gy = np.meshgrid(xs, ys)
            path = write_geotiff(ch.data_dir / "dem.tif", _surface(gx, gy).astype(np.float32), (x0, y1), cell, None,
                                 nodata=-9999.0)
        else:
            arr = rng.integers(0, 255, (3, rows, cols), dtype=np.uint8)
            path = write_geotiff(ch.data_dir / "orthomosaic.tif", arr, (x0, y1), cell, None)
        ch._set_product(name, path)
        ch.meta.setdefault("built_in", {})[name] = np.asarray(frame, float).tolist()


def _photo(ch, tmp_path):
    """One aligned pinhole photo of ``ch`` 10 m above the middle of the surface, looking down, and tie points on
    the surface."""
    import cv2

    ch.sensors[0] = Sensor(key=0, label="s", width=1000, height=800, model="PINHOLE",
                           params=[1000.0, 1000.0, 500.0, 400.0])
    pose = np.diag([1.0, -1.0, -1.0, 1.0])
    pose[:3, 3] = [2.0, 2.0, 10.0]
    path = tmp_path / "foto.jpg"
    cv2.imwrite(str(path), np.full((800, 1000, 3), 128, np.uint8))
    cam = Camera(key=0, label="foto", path=str(path), sensor_key=0, transform=pose)
    ch.cameras.append(cam)
    g = np.mgrid[0:4:0.05, 0:4:0.05].reshape(2, -1).T
    ch.tie_points = PointCloud(np.c_[g, _surface(g[:, 0], g[:, 1])])
    return cam


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    from pyarchinit_photogrammetry.gui import main_window as mwmod
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    doc = ppg.Document(tmp_path / "punti.ppg")
    a, b = doc.add_chunk("2024"), doc.add_chunk("2025")
    a.crs = b.crs = "LOCAL"
    _rasters(a, np.eye(4), 0.02, 0.01)
    old = _translation(OLD)  # B's frame when its rasters were built (right then)
    b.transform = old.copy()
    _rasters(b, old, 0.05, 0.02)
    b.transform = _translation(SHIFT) @ old  # moved since: the rasters stay in the old frame (to be rebuilt)
    doc.save()
    said: list = []
    monkeypatch.setattr(mwmod, "message", lambda *a, **k: said.append(("message", a[2] if len(a) > 2 else "")))
    w = MainWindow(doc, autoconnect=False, check_updates=False)
    monkeypatch.setattr(w, "_warn", lambda text: said.append(("warn", text)))
    monkeypatch.setattr(w, "_info", lambda title, text: said.append(("info", text)))
    w.said = said
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qapp.processEvents()
    yield w
    w._close_common_points()
    w._close_ties_dialog()
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


def _open(w, qapp):
    cp = w.common_points()
    assert cp is not None and cp.isVisible()
    cp.resize(1400, 800)
    qapp.processEvents()
    a, b = cp.sides
    assert a.chunk.label == "2024" and b.chunk.label == "2025"
    assert a.view.tr is not None and b.view.tr is not None
    return cp


def _centre(cp, side):
    return np.array(cp.centre(side))


def test_views_linked_through_the_current_transforms(win, qapp):
    cp = _open(win, qapp)
    a, b = cp.sides
    # B's raster was built before B moved by SHIFT: the same world point is SHIFT away in its file
    a.view.setTransform(a.view.transform().scale(3.0, 3.0))
    cp.centre_on(a, 1.5, 2.0)
    qapp.processEvents()
    np.testing.assert_allclose(_centre(cp, a), [1.5, 2.0], atol=2 * cp.units_per_pixel(a))
    np.testing.assert_allclose(_centre(cp, b), [1.5 - SHIFT[0], 2.0 - SHIFT[1]], atol=2 * cp.units_per_pixel(b))
    assert cp.units_per_pixel(b) == pytest.approx(cp.units_per_pixel(a), rel=1e-6)
    # panning B moves A: the other way round
    bar = b.view.horizontalScrollBar()
    bar.setValue(bar.value() + 40)
    qapp.processEvents()
    xb, yb = _centre(cp, b)
    np.testing.assert_allclose(_centre(cp, a), [xb + SHIFT[0], yb + SHIFT[1]], atol=2 * cp.units_per_pixel(a))
    # the cursor of A is a cross in B, at the same world point
    cp._echo(a, 1.0, 1.0)
    pos = b.cross.pos()
    np.testing.assert_allclose(b.view.world(pos), [1.0 - SHIFT[0], 1.0 - SHIFT[1]], atol=1e-6)
    # B shown as its DEM: the link holds (another cell, the same place)
    cp.set_raster(1, "dem")
    qapp.processEvents()
    assert b.kind == "dem"
    np.testing.assert_allclose(_centre(cp, b), [xb, yb], atol=2 * cp.units_per_pixel(b))


def test_a_tie_by_two_clicks_is_one_step_with_its_markers(win, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    cp = _open(win, qapp)
    a, b = cp.sides
    a.view.setTransform(a.view.transform().scale(2.0, 2.0))
    cp.centre_on(a, 2.0, 2.0)
    qapp.processEvents()
    # a real click in A: the first end; B jumps to where the transforms put it
    target = a.view.mapFromScene(a.view.scene_pt(1.2, 1.4))
    QTest.mouseClick(a.view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    qapp.processEvents()
    assert cp.pending is not None and cp.pending.chunk is a.chunk and not ties.ties_of(win.doc)
    xa, ya = a.view.world(a.view.mapToScene(target))
    np.testing.assert_allclose(_centre(cp, b), [xa - SHIFT[0], ya - SHIFT[1]], atol=2 * cp.units_per_pixel(b))
    assert "Legame a metà" in a.view.layers
    # Esc drops it
    QTest.keyClick(a.view, Qt.Key.Key_Escape)
    assert cp.pending is None and "Legame a metà" not in a.view.layers
    # again, then the same point in B (where B's file has it): the tie, listed at once in the dialog of the ties
    win.tie_chunks()
    qapp.processEvents()
    assert win._ties_dialog.table.rowCount() == 0
    cp.clicked(a, 1.2, 1.4)
    cp.clicked(b, 1.2, 1.4)
    (tie,) = ties.ties_of(win.doc)
    ma, mb = a.chunk.marker_by_key(tie.a[1]), b.chunk.marker_by_key(tie.b[1])
    assert ma.label == mb.label == "legame 1"
    np.testing.assert_allclose(ma.position, [1.2, 1.4, _surface(1.2, 1.4)], atol=0.03)
    np.testing.assert_allclose(mb.position, [1.2 - OLD[0], 1.4 - OLD[1], _surface(1.2, 1.4)], atol=0.06)
    assert tie.precision == pytest.approx(np.hypot(2 * 0.01, 2 * 0.02))  # two cells of each raster shown
    assert "Legami" in a.view.layers and "Legami" in b.view.layers and cp.selected == tie.id
    assert win._ties_dialog.table.rowCount() == 1
    # one step of Annulla: the tie and both markers go, and come back
    assert win.history.can_undo()
    win.undo()
    qapp.processEvents()
    assert not ties.ties_of(win.doc) and not a.chunk.markers and not b.chunk.markers
    assert "Legami" not in a.view.layers or not a.view.layers["Legami"]["features"]
    assert win._ties_dialog.table.rowCount() == 0
    win.redo()
    qapp.processEvents()
    assert [t.id for t in ties.ties_of(win.doc)] == [tie.id] and len(a.chunk.markers) == len(b.chunk.markers) == 1


def test_dragging_an_end_moves_its_marker(win, qapp):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    cp = _open(win, qapp)
    a, b = cp.sides
    a.view.setTransform(a.view.transform().scale(2.0, 2.0))
    cp.centre_on(a, 2.0, 2.0)
    qapp.processEvents()
    cp.clicked(a, 1.8, 2.2)
    cp.clicked(b, 1.8, 2.2)
    (tie,) = ties.ties_of(win.doc)
    cp._moved.clear()
    before = a.chunk.marker_by_key(tie.a[1]).position.copy()
    start = a.view.mapFromScene(a.view.scene_pt(1.8, 2.2))
    end = start + QPoint(30, -20)
    vp = a.view.viewport()
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(vp, start + QPoint(10, -5))
    QTest.mouseMove(vp, end)
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, pos=end)
    qapp.processEvents()
    x, y = a.view.world(a.view.mapToScene(end))
    after = a.chunk.marker_by_key(tie.a[1]).position
    np.testing.assert_allclose(after[:2], [x, y], atol=1e-6)
    assert after[2] == pytest.approx(_surface(x, y), abs=0.03)  # the height from the DEM
    assert tie.id in cp._moved  # its residual is no longer drawn
    win.undo()
    np.testing.assert_allclose(a.chunk.marker_by_key(tie.a[1]).position, before)
    cp.redraw()
    assert tie.id not in cp._moved  # back where it was checked: its residual is drawn again


def test_residuals_of_the_check_drawn_and_buttons(win, qapp):
    cp = _open(win, qapp)
    a, b = cp.sides
    for x, y in ((0.5, 0.5), (3.0, 0.8), (2.5, 3.0), (0.6, 3.2)):
        cp.clicked(a, x, y)
        cp.clicked(b, x, y + (0.3 if (x, y) == (3.0, 0.8) else 0.0))  # one tie 30 cm off
    assert len(ties.ties_of(win.doc)) == 4
    cp.check()
    qapp.processEvents()
    for _ in range(200):
        dlg = win._ties_dialog
        if dlg.result is not None:
            break
        qapp.processEvents()
        import time

        time.sleep(0.02)
    assert dlg.result is not None
    assert "Residui dei legami" in a.view.layers and "Residui dei legami" in b.view.layers
    cp.apply()
    qapp.processEvents()
    assert win.doc.meta.get("ties_report") is not None  # applied (with redundancy: no question)


def test_an_end_on_a_photo_becomes_a_pinned_projection(win, qapp, tmp_path):
    from PySide6.QtWidgets import QMenu

    from pyarchinit_photogrammetry.core.marker import Projection

    cp = _open(win, qapp)
    a, b = cp.sides
    cam = _photo(a.chunk, tmp_path)
    # another marker of the chunk, measured on the photo but kept elsewhere: an end on a photo must not move it
    other = a.chunk.add_marker("X", [3.0, 3.0, 1.0])
    other.projections[cam.key] = Projection(100.0, 100.0, True)
    world = np.array([[2.3, 1.7, _surface(2.3, 1.7)]])
    (px, py), = cam.project(a.chunk.sensor(cam), world)[0]
    # the first end on B's raster, the second on a photo of A: the photo view's menu while the window is open
    cp.clicked(b, 2.3, 1.7)
    assert cp.pending is not None
    win.photo_view.show_camera(a.chunk, cam)
    menu = QMenu()
    win._photo_tie_menu(menu, px, py)
    (action,) = [x for x in menu.actions() if x.text() == "Estremo del legame su questa foto"]
    action.trigger()
    qapp.processEvents()
    (tie,) = ties.ties_of(win.doc)
    end_a = tie.a if tie.a[0] == a.chunk.key else tie.b
    marker = a.chunk.marker_by_key(end_a[1])
    pr = marker.projections[cam.key]
    assert pr.pinned is True and (pr.x, pr.y) == pytest.approx((px, py))
    np.testing.assert_allclose(marker.position, world[0], atol=0.05)  # one photo: on the surface
    np.testing.assert_allclose(other.position, [3.0, 3.0, 1.0])
    assert tie.precision > 0
    # undo: the tie, the marker and its projection go together
    win.undo()
    assert not ties.ties_of(win.doc) and [m.label for m in a.chunk.markers] == ["X"]
    win.redo()
    assert ties.ties_of(win.doc)

    # the pending end of a chunk measured on its photo first, then tied
    cp.clicked(a, 1.0, 1.0)
    (qx, qy), = cam.project(a.chunk.sensor(cam), np.array([[1.0, 1.0, _surface(1.0, 1.0)]]))[0]
    assert cp.end_on_photo(a.chunk, cam, qx, qy) is None and cp.pending.camera is cam
    cp.clicked(b, 1.0, 1.0)
    second = [t for t in ties.ties_of(win.doc) if t.id != tie.id]
    assert len(second) == 1
    m2 = a.chunk.marker_by_key(second[0].a[1] if second[0].a[0] == a.chunk.key else second[0].b[1])
    assert m2.projections[cam.key].pinned is True

    # the selected tie measured on one more photo
    cp.select(second[0].id)
    menu = QMenu()
    win._photo_tie_menu(menu, qx + 1, qy)
    texts = [x.text() for x in menu.actions()]
    assert any("anche su questa foto" in t for t in texts)


def test_a_local_chunk_beside_one_in_a_system_is_superimposed(win, qapp):
    cp = _open(win, qapp)
    a, b = cp.sides
    b.chunk.crs = "EPSG:3004"
    cp.refresh()
    qapp.processEvents()
    assert cp.superimposed and not cp.note.isHidden()
    cp.centre_on(a, 2.0, 2.0)  # the middle of A's raster: on the middle of B's
    qapp.processEvents()
    np.testing.assert_allclose(_centre(cp, b), [2.0, 2.0], atol=2 * cp.units_per_pixel(b))
    cp.clicked(a, 1.0, 1.0)
    cp.clicked(b, 1.0, 1.0)
    assert len(ties.ties_of(win.doc)) == 1  # a tie between them holds all the same


def test_selected_tie_on_one_more_photo_keeps_a_precision_set_by_hand(win, qapp, tmp_path):
    cp = _open(win, qapp)
    a, b = cp.sides
    cam = _photo(a.chunk, tmp_path)
    cp.clicked(b, 2.0, 2.0)
    cp.clicked(a, 2.0, 2.0)  # a raster end in A
    (tie,) = ties.ties_of(win.doc)
    made = tie.precision
    (px, py), = cam.project(a.chunk.sensor(cam), np.array([[2.0, 2.0, _surface(2.0, 2.0)]]))[0]
    cp.select(tie.id)
    assert cp.add_photo_to_selected(a.chunk, cam, px, py)
    (tie,) = ties.ties_of(win.doc)
    assert tie.precision != pytest.approx(made)  # made here: this end's precision from its photo now
    all_ties = ties.ties_of(win.doc)
    all_ties[0].precision = 0.123  # by hand in the dialog
    ties.save_ties(win.doc, all_ties)
    assert cp.add_photo_to_selected(a.chunk, cam, px + 0.5, py)
    assert ties.ties_of(win.doc)[0].precision == pytest.approx(0.123)
    assert "invariata" in cp.status.text()


def test_half_tie_same_chunk_and_unshown(win, qapp):
    cp = _open(win, qapp)
    a, b = cp.sides
    cp.clicked(a, 1.0, 1.0)
    cp.clicked(a, 1.5, 1.0)
    assert "Stesso chunk" in cp.status.text() and cp.pending.internal[0] == pytest.approx(1.5)
    cp.set_chunks(b.chunk, b.chunk)  # the half tie's chunk in neither view: dropped, said
    qapp.processEvents()
    assert cp.pending is None and "non è più" in cp.status.text()


def test_esc_on_a_combo_forgets_the_window(win, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QShortcut
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMenu

    cp = _open(win, qapp)
    assert len(cp.findChildren(QShortcut)) == 2  # Ctrl+Z / Ctrl+Shift+Z: the main window's Annulla / Ripeti
    combo = cp.sides[1].rasters
    combo.setFocus()
    QTest.keyClick(combo, Qt.Key.Key_Escape)  # no half tie: Esc rejects the dialog (no closeEvent in Qt 6)
    qapp.processEvents()
    assert win._live_common_points() is None and win._common_points is None
    cam = _photo(cp_chunk := win.doc.chunks[0], _folder(win))
    win.photo_view.show_camera(cp_chunk, cam)
    menu = QMenu()
    win._photo_tie_menu(menu, 10.0, 10.0)
    assert not menu.actions()
    win.refresh_all()  # nothing raises: the window no longer follows the project
    again = win.common_points()
    assert again is not None and again is not cp and again.isVisible()
    # the same for the dialog of the ties
    win.tie_chunks()
    dlg = win._ties_dialog
    QTest.keyClick(dlg.table, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert win._live_ties_dialog() is None


def _folder(win):
    return win.doc.path.parent


def test_no_tie_edit_while_a_check_runs_and_a_stale_end_placed_again(win, qapp):
    cp = _open(win, qapp)
    a, b = cp.sides
    win.runner.thread = object()  # a task (the check of the ties) runs in the worker
    try:
        cp.clicked(a, 1.2, 1.4)
        cp.clicked(b, 1.2, 1.4)
        assert not ties.ties_of(win.doc)
        assert "aspetta che finisca" in cp.status.text()
    finally:
        win.runner.thread = None
    cp.cancel()
    cp.clicked(a, 1.2, 1.4)
    cp.clicked(b, 1.2, 1.4)
    (tie,) = ties.ties_of(win.doc)
    b.chunk.meta["alignment_id"] = "nuovo"  # 2025 aligned again: its end, clicked on a raster, is stale
    assert ties.tie_by_id(win.doc, tie.id).stale == ["b"]
    win.runner.thread = object()
    try:
        assert not cp.move_end(b, tie.id, "b", 1.5, 1.5)
    finally:
        win.runner.thread = None
    assert cp.move_end(b, tie.id, "b", 1.3, 1.4)  # placed again in the new frame
    assert not ties.tie_by_id(win.doc, tie.id).stale
    win.undo()  # the old place and the old alignment of the end come back together
    assert ties.tie_by_id(win.doc, tie.id).stale == ["b"]


def test_the_photo_menu_hook_does_not_hold_the_window(win):
    import weakref

    hook = win.photo_view.context_hook
    assert hook is not None and hook.__self__ is win
    assert isinstance(win.photo_view._context_hook, weakref.WeakMethod)
    plain = []
    win.photo_view.context_hook = lambda menu, x, y: plain.append((x, y))
    win.photo_view.context_hook(None, 1, 2)
    assert plain == [(1, 2)]
    win.photo_view.context_hook = hook
