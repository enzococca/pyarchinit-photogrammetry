"""Looking through a photo in the 3D view (Metashape's *Look Through*) on the synthetic trench: the view takes the
photo's pose and field of view, the photo is drawn undistorted over the model - the targets on the photo shown lie
within 2 px of the targets of the model - with an opacity of its own; the photo that best matches the view, the
next/previous photo, Esc and any movement of the view leave it; the window's entries (Photos pane, photo view)."""
from __future__ import annotations

import os
import shutil
import time

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")


@pytest.fixture
def app(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    return app


@pytest.fixture
def view(processed, app, monkeypatch):
    monkeypatch.setenv("PPG_RENDERER", "software")
    from pyarchinit_photogrammetry.gui.model_view import ModelView

    _doc, chunk, _ = processed
    mv = ModelView()
    mv.resize(760, 520)
    mv.show()
    app.processEvents()
    mv.set_chunk(chunk, "model_textured" if chunk.product_path("model_textured") else "model")
    app.processEvents()
    mv.looks = []
    mv.look_changed.connect(lambda cam: mv.looks.append(cam))
    yield mv
    mv.close()


def wait_photo(mv, app, seconds=30.0):
    end = time.monotonic() + seconds
    while not mv.look_ready() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert mv.look_ready(), "the photo was not read"


def _photo_to_widget(mv, cam, pixels):
    """Pixels of the photo (COLMAP) -> widget pixels of the photo drawn over the view."""
    from pyarchinit_photogrammetry.engine import look_through as lt

    sensor = mv.chunk.sensor(cam)
    ideal = mv._look["ideal"]
    x, y, w, _h = mv.canvas.photo_rect()
    s = w / ideal.width
    return np.array([x, y]) + s * lt.photo_to_ideal(sensor, ideal, pixels)


def _scene_to_widget(mv, internal):
    sd = mv.sd
    x, y, _z, ok = mv.canvas.projector()(sd.to_display(mv.chunk.to_world(np.atleast_2d(internal)) - sd.offset))
    assert ok.all()
    return np.column_stack([x, y])


def _camera_with_markers(chunk, n=3):
    best = None
    for cam in chunk.aligned_cameras:
        seen = [m for m in chunk.markers if cam.key in m.projections and m.position is not None
                and m.projections[cam.key].measured]
        if best is None or len(seen) > len(best[1]):
            best = (cam, seen)
    assert len(best[1]) >= n
    return best


def test_the_photo_lies_on_the_model(view, app):
    """Marker centres: measured on the photo and shown through the photo's frame, detected on the undistorted
    photo drawn, projected from their 3D position by the view: all within 2 px."""
    import cv2

    from pyarchinit_photogrammetry.engine import look_through as lt
    from pyarchinit_photogrammetry.markers.targets import detect

    mv = view
    cam, markers = _camera_with_markers(mv.chunk)
    assert mv.look_through(cam)
    assert mv.look_camera is cam and mv.looks == [cam] and mv.look_row.isVisibleTo(mv)
    wait_photo(mv, app)
    # the view is the photo's: centre and direction
    centre, basis = lt.photo_pose(mv.chunk, cam)
    vc = mv.canvas.get_cam()
    assert np.allclose(vc.eye + mv.sd.offset, centre, atol=1e-6)
    assert np.allclose(vc.basis(), basis, atol=1e-9)
    # the photo fills the view in one direction, centred in the other
    w, h = mv.canvas.view_size()
    x, y, pw, ph = mv.canvas.photo_rect()
    assert (abs(pw - w) < 0.5 and abs(x) < 0.5) or (abs(ph - h) < 0.5 and abs(y) < 0.5)
    assert abs(x + pw / 2 - w / 2) < 0.5 and abs(y + ph / 2 - h / 2) < 0.5
    projected = _scene_to_widget(mv, np.array([m.position for m in markers]))
    measured = _photo_to_widget(mv, cam, np.array([[m.projections[cam.key].x, m.projections[cam.key].y]
                                                   for m in markers]))
    assert np.abs(projected - measured).max() < 2.0, np.abs(projected - measured).max()
    # the targets found on the photo drawn (undistorted) are where the model has them
    image = mv._look["image"]
    arr = np.frombuffer(image.constBits(), np.uint8, image.sizeInBytes()).reshape(image.height(), -1)[
        :, : image.width() * 4].reshape(image.height(), image.width(), 4)
    found = {d["code"]: d["center"] + 0.5 for d in detect(cv2.cvtColor(arr[:, :, :3], cv2.COLOR_RGB2GRAY))}
    s = pw / mv._look["ideal"].width
    checked = 0
    for m, p in zip(markers, projected):
        if m.code in found:
            assert np.linalg.norm(np.array([x, y]) + s * found[m.code] - p) < 2.0
            checked += 1
    assert checked >= 2


def test_opacity_and_the_frame_drawn(view, app, tmp_path):
    """0 %: the model as without the photo; 100 %: the photo; 50 %: between; the slider remembered."""
    from pyarchinit_photogrammetry.gui import app_settings

    mv = view
    cam, _markers = _camera_with_markers(mv.chunk)
    mv.look_through(cam)
    wait_photo(mv, app)
    grabs = {}
    for value in (0, 50, 100):
        mv.look_slider.setValue(value)
        app.processEvents()
        grabs[value] = mv.canvas.grab_array().astype(np.float64)
    image = mv._look["image"].convertToFormat(mv._look["image"].Format.Format_RGB888).scaled(
        *[int(round(v)) for v in mv.canvas.photo_rect()[2:]])
    x, y, w, h = [int(round(v)) for v in mv.canvas.photo_rect()]
    inner = (slice(y + h // 4, y + 3 * h // 4), slice(x + w // 4, x + 3 * w // 4))
    a0, a50, a100 = (grabs[v][inner] for v in (0, 50, 100))
    assert np.abs(a50 - (a0 + a100) / 2).mean() < 6
    assert np.abs(a100 - a0).mean() > 10  # the photo is not the model shaded
    assert int(float(app_settings().value("view3d/look_opacity"))) == 100
    assert image.width() > 0


def test_next_previous_best_and_leaving(view, app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from pyarchinit_photogrammetry.engine import look_through as lt

    mv = view
    order = lt.ordered_cameras(mv.chunk)
    mv.look_through(order[3])
    QTest.keyClick(mv.canvas, Qt.Key.Key_PageDown)
    assert mv.look_camera is order[4]
    QTest.keyClick(mv.canvas, Qt.Key.Key_PageUp)
    QTest.keyClick(mv.canvas, Qt.Key.Key_Left)
    assert mv.look_camera is order[2]
    mv.look_step(-1)
    mv.look_step(-1)
    mv.look_step(-1)
    assert mv.look_camera is order[-1]  # round the list
    # Esc leaves: the free view where the photo was
    eye = mv.canvas.get_cam().eye.copy()
    QTest.keyClick(mv.canvas, Qt.Key.Key_Escape)
    assert mv.look_camera is None and mv.looks[-1] is None and not mv.look_row.isVisibleTo(mv)
    assert mv.canvas.photo is None and np.allclose(mv.canvas.get_cam().eye, eye)
    # the best photo for the view: the view of a photo, moved a little, gives that photo back
    target = order[len(order) // 2]
    mv.look_through(target)
    tb = mv.canvas.trackball
    tb.press(300, 260, "rotate")
    tb.move(303, 261)  # moving the view leaves the photo
    tb.release()
    assert mv.look_camera is None
    assert mv.look_best() is target and mv.look_camera is target
    # Home leaves and gives the field of view back
    fov = mv.canvas.cam.fov
    mv.canvas.reset_view()
    assert mv.look_camera is None and mv.canvas.cam.fov != fov
    # the vertical exaggeration goes back to 1 for a photo, and an exaggeration leaves it
    mv.set_exaggeration(3.0)
    mv.look_through(target)
    assert mv.exaggeration == 1.0 and mv.look_camera is target
    mv.set_exaggeration(2.0)
    assert mv.look_camera is None
    # an unaligned photo or one of another chunk cannot be looked through
    import types

    assert not mv.look_through(types.SimpleNamespace(aligned=False))


def test_the_photo_stays_through_another_kind_and_a_resize(view, app):
    mv = view
    cam, markers = _camera_with_markers(mv.chunk)
    mv.look_through(cam)
    wait_photo(mv, app)
    mv.show_kind("point_cloud")
    app.processEvents()
    assert mv.look_camera is cam and mv.canvas.photo is not None
    mv.resize(600, 600)
    for _ in range(5):
        app.processEvents()
    w, h = mv.canvas.view_size()
    x, y, pw, ph = mv.canvas.photo_rect()
    assert abs(pw - w) < 0.5 and abs(y + ph / 2 - h / 2) < 0.5  # wider than tall: bands above and below
    projected = _scene_to_widget(mv, np.array([m.position for m in markers]))
    measured = _photo_to_widget(mv, cam, np.array([[m.projections[cam.key].x, m.projections[cam.key].y]
                                                   for m in markers]))
    assert np.abs(projected - measured).max() < 2.0


@pytest.fixture
def win(app, processed, tmp_path):
    import pyarchinit_photogrammetry as ppg
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
    app.processEvents()
    yield w
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


def _menu_entry(monkeypatch, module, open_menu, text):
    """Trigger the entry ``text`` of the context menu ``open_menu()`` opens (the module's QMenu stood in)."""
    from PySide6.QtWidgets import QMenu

    found = []

    class Menu(QMenu):
        def exec(self, *_a, **_k):
            for a in self.actions():
                if a.text() == text:
                    found.append(a)
                    a.trigger()
                    return a
            return None

    monkeypatch.setattr(module, "QMenu", Menu)
    open_menu()
    assert found, f"no entry {text!r}"


def test_window_entries(win, app, monkeypatch):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QContextMenuEvent

    from pyarchinit_photogrammetry.gui import image_views
    from pyarchinit_photogrammetry.gui import main_window as mw

    cam = win.chunk.aligned_cameras[5]
    # the Photos pane: right click on one aligned photo
    win.photos.clearSelection()
    for i in range(win.photos.count()):
        it = win.photos.item(i)
        if it.data(Qt.ItemDataRole.UserRole).key == cam.key:
            it.setSelected(True)
    _menu_entry(monkeypatch, mw, lambda: win._photos_menu(QPoint(0, 0)), mw.tr("Guarda dalla foto nella vista 3D"))
    assert win.tabs.currentWidget() is win.model_view and win.model_view.look_camera is cam
    win.model_view.leave_look()
    # the photo view: its right-click menu
    other = win.chunk.aligned_cameras[7]
    win.open_photo(other)
    pv = win.photo_view
    ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(20, 20), pv.mapToGlobal(QPoint(20, 20)))
    _menu_entry(monkeypatch, image_views, lambda: pv.contextMenuEvent(ev), mw.tr("Guarda dalla foto nella vista 3D"))
    assert win.tabs.currentWidget() is win.model_view and win.model_view.look_camera is other
    # the photo looked through is the one selected in the Photos pane
    assert [c.key for c in win.photos.selected_cameras()] == [other.key]


def test_vtk_photo_lies_on_the_model(processed, app, monkeypatch):
    """The same through the VTK canvas: the photo on a quad of its own layer, the targets within 2 px."""
    from pyarchinit_photogrammetry.gui import model_view as mvm

    monkeypatch.setenv("PPG_RENDERER", "vtk")
    if os.environ.get("PPG_TEST_VTK") != "1":
        pytest.skip("VTK canvas: opt-in with PPG_TEST_VTK=1 (a QtInteractor without an OpenGL context may "
                    "crash on some platforms)")
    if not mvm._vtk_usable():
        pytest.skip("VTK is not usable here")
    from pyarchinit_photogrammetry.gui.gc_guard import install

    collector = install(app)  # as the application: no garbage collection in the thread reading the photo
    _doc, chunk, _ = processed
    mv = mvm.ModelView()
    try:
        assert mv.backend == "vtk"
        mv.resize(760, 520)
        mv.show()
        app.processEvents()
        mv.set_chunk(chunk, "model")
        app.processEvents()
        cam, markers = _camera_with_markers(chunk)
        assert mv.look_through(cam)
        wait_photo(mv, app)
        w, h = mv.canvas.view_size()
        x, y, pw, ph = mv.canvas.photo_rect()
        assert abs(x + pw / 2 - w / 2) < 0.5 and abs(y + ph / 2 - h / 2) < 0.5
        projected = _scene_to_widget(mv, np.array([m.position for m in markers]))
        measured = _photo_to_widget(mv, cam, np.array([[m.projections[cam.key].x, m.projections[cam.key].y]
                                                       for m in markers]))
        assert np.abs(projected - measured).max() < 2.0
        mv.look_slider.setValue(100)
        app.processEvents()
        assert mv.canvas._photo_actor.GetProperty().GetOpacity() == pytest.approx(1.0)
        mv.leave_look()
        assert mv.canvas._photo_actor is None and mv.look_camera is None
    finally:
        mv.close()
        collector.stop()
