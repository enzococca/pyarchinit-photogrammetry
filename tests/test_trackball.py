"""The visible trackball of the 3D view: free, ring and roll rotations about the pivot, zoom toward
the cursor, the pivot from a double click, the keys and the setting."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def app(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    return app


def _scene():
    from pyarchinit_photogrammetry.gui.scene import SceneData

    rng = np.random.default_rng(1)
    pts = rng.uniform(-1, 1, (4000, 3)) * [2.0, 1.5, 0.3]
    return SceneData(np.zeros(3), points=pts.astype(np.float32), colors=np.full((4000, 3), 200, np.uint8),
                     kind="point_cloud")


@pytest.fixture
def canvas(app, monkeypatch):
    monkeypatch.setenv("PPG_RENDERER", "software")
    from pyarchinit_photogrammetry.gui.model_view import SoftwareCanvas

    c = SoftwareCanvas()
    c.resize(400, 300)
    c.set_scene(_scene())
    yield c
    c.close()


def _screen(c, p):
    x, y, _z, _ok = c.cam.project(np.asarray(p, float)[None], *c.view_size())
    return np.array([x[0], y[0]])


def _drag(c, path, kind="rotate"):
    tb = c.trackball
    tb.press(*path[0], kind)
    for x, y in path[1:]:
        tb.move(x, y)
    tb.release()


def _angle_to_up(c):
    return float(np.degrees(np.arccos(np.clip(c.cam.basis()[2] @ np.array([0, 0, 1.0]), -1, 1))))


def test_orbit_camera_keeps_yaw_and_pitch():
    from pyarchinit_photogrammetry.gui.scene import OrbitCamera

    cam = OrbitCamera(np.zeros(3), 5.0, yaw=20.0, pitch=-10.0)
    assert cam.yaw == pytest.approx(20.0) and cam.pitch == pytest.approx(-10.0)
    cam.pitch = 50
    assert cam.yaw == pytest.approx(20.0) and cam.pitch == pytest.approx(50.0)
    R = cam.basis()
    assert np.allclose(R @ R.T, np.eye(3)) and np.linalg.det(R) == pytest.approx(1.0)


def test_drag_on_the_horizontal_ring_turns_only_about_up(canvas):
    c = canvas
    tb = c.trackball
    geo = tb.geometry()
    xy, ok = geo["rings"]["up"][2], geo["rings"]["up"][3]
    i = int(np.argmax(np.where(ok, xy[:, 1], -np.inf)))  # the lowest point of the ring on the screen
    start = xy[i]
    assert tb.hit(*start) == ("ring", "up")
    pivot_before = _screen(c, tb.pivot)
    tilt, yaw = _angle_to_up(c), c.cam.yaw
    _drag(c, [start, start + (15, 0), start + (40, 3)])
    assert abs(c.cam.yaw - yaw) > 3  # it turned
    assert _angle_to_up(c) == pytest.approx(tilt, abs=1e-6)  # only about up
    assert np.allclose(_screen(c, tb.pivot), pivot_before, atol=1e-6)
    assert np.allclose(c.cam.basis()[0] @ [0, 0, 1.0], 0, atol=1e-9)  # no roll


def test_free_drag_keeps_the_pivot_on_the_screen(canvas):
    c = canvas
    tb = c.trackball
    tb.set_pivot([0.5, -0.3, 0.1])  # off the middle: the view does not move
    pivot_before = _screen(c, tb.pivot)
    R0 = c.cam.basis()
    cx, cy = tb.geometry()["centre"]
    assert np.allclose((cx, cy), pivot_before)
    start = np.array([cx + 10, cy - 20])
    assert tb.hit(*start) == ("free", None)
    _drag(c, [start, start + (25, 10), start + (50, 30)])
    assert not np.allclose(c.cam.basis(), R0)
    assert np.allclose(_screen(c, tb.pivot), pivot_before, atol=1e-6)


def test_free_drag_follows_the_cursor(canvas):
    """The point of the sphere under the cursor stays under it (Shoemake's arcball)."""
    from pyarchinit_photogrammetry.gui.trackball import arcball_vector

    c = canvas
    tb = c.trackball
    geo = tb.geometry()
    cx, cy = geo["centre"]
    r = geo["radius"]
    a, b = np.array([cx + 0.2 * r, cy + 0.1 * r]), np.array([cx + 0.45 * r, cy - 0.15 * r])
    v = arcball_vector(*a, cx, cy, r)
    world = tb.pivot + geo["world_radius"] * (c.cam.basis().T @ v)  # the point of the sphere under a
    _drag(c, [a, b])
    v1 = c.cam.basis() @ ((world - tb.pivot) / geo["world_radius"])
    assert np.allclose(v1, arcball_vector(*b, cx, cy, r), atol=1e-9)


def test_roll_outside_the_sphere_keeps_the_view_direction(canvas):
    c = canvas
    tb = c.trackball
    cx, cy = tb.geometry()["centre"]
    fwd = c.cam.basis()[2]
    pivot_before = _screen(c, tb.pivot)
    start = np.array([cx + 190, cy + 140])
    assert tb.hit(*start) == ("roll", None)
    _drag(c, [start, (cx + 150, cy + 145), (cx + 100, cy + 145)])
    assert np.allclose(c.cam.basis()[2], fwd, atol=1e-9)
    assert not np.allclose(c.cam.basis()[0], np.cross(fwd, [0, 0, 1.0]) / np.linalg.norm(np.cross(fwd, [0, 0, 1.0])))
    assert np.allclose(_screen(c, tb.pivot), pivot_before, atol=1e-6)


def test_wheel_toward_a_corner_moves_along_its_ray(canvas):
    c = canvas
    tb = c.trackball
    eye0, p0, d0 = c.cam.eye.copy(), c.pivot(), c.cam.distance
    pivot_before = _screen(c, p0)
    o, ray, _ = c.view_ray(30, 25)
    tb.zoom(2, 30, 25)
    assert c.cam.distance == pytest.approx(d0 * 0.88 ** 2)
    move = c.cam.eye - eye0  # the camera goes down the ray of the cursor
    assert np.allclose(np.cross(move / np.linalg.norm(move), ray), 0, atol=1e-9) and move @ ray > 0
    # the pivot slides toward the point of that ray at its depth, by the zoom factor, and keeps its place
    R = c.cam.basis()
    point = o + ray * (((p0 - eye0) @ R[2]) / (ray @ R[2]))
    assert np.allclose(tb.pivot, point + 0.88 ** 2 * (p0 - point))
    assert np.allclose(_screen(c, tb.pivot), pivot_before, atol=1e-6)
    # Shift+wheel: dolly, the pivot stays where it is
    p1, fwd = tb.pivot.copy(), c.cam.basis()[2]
    eye1 = c.cam.eye.copy()
    tb.zoom(1, 30, 25, dolly=True)
    assert np.allclose(tb.pivot, p1)
    assert np.allclose(np.cross(c.cam.eye - eye1, fwd), 0, atol=1e-9)


def test_pan_moves_the_scene_under_the_trackball(canvas):
    """The scene follows the cursor; the sphere keeps its place on the screen (as in Metashape)."""
    c = canvas
    tb = c.trackball
    p0 = c.pivot()
    before = _screen(c, p0)
    _drag(c, [(100, 100), (130, 90)], kind="pan")
    assert np.allclose(_screen(c, p0), before + (30, -10), atol=1e-6)
    assert np.allclose(_screen(c, tb.pivot), before, atol=1e-6)


def test_keys(canvas):
    from PySide6.QtCore import Qt

    c = canvas
    yaw, tilt = c.cam.yaw, _angle_to_up(c)
    assert c.view_key(Qt.Key.Key_Right)
    assert (c.cam.yaw - yaw) % 360 == pytest.approx(345.0) or (c.cam.yaw - yaw) % 360 == pytest.approx(15.0)
    assert _angle_to_up(c) == pytest.approx(tilt)
    assert c.view_key(Qt.Key.Key_Up)
    assert abs(_angle_to_up(c) - tilt) == pytest.approx(15.0)
    p0 = c.pivot()
    before = _screen(c, p0)
    assert c.view_key(Qt.Key.Key_Left, shift=True)
    assert np.allclose(_screen(c, p0), before + (30, 0), atol=1e-6)  # 10 % of 300 px
    d = c.cam.distance
    assert c.view_key(Qt.Key.Key_Plus) and c.cam.distance == pytest.approx(d * 0.88)
    assert c.view_key(Qt.Key.Key_Minus) and c.cam.distance == pytest.approx(d)
    assert c.view_key(Qt.Key.Key_F) and np.allclose(c.cam.basis()[2], [0, 1, 0])
    assert c.view_key(Qt.Key.Key_L) and np.allclose(c.cam.basis()[2], [1, 0, 0])
    assert c.view_key(Qt.Key.Key_T) and c.cam.pitch == pytest.approx(89.5)
    c.trackball.set_pivot([1.0, 1.0, 0.0])
    assert c.view_key(Qt.Key.Key_Home)
    assert np.allclose(c.pivot(), c.sd.center) and c.cam.pitch == pytest.approx(35.0)
    assert not c.view_key(Qt.Key.Key_Q)


def test_visibility_and_the_setting(app, monkeypatch):
    monkeypatch.setenv("PPG_RENDERER", "software")
    from pyarchinit_photogrammetry.gui import app_settings
    from pyarchinit_photogrammetry.gui import trackball as tbm
    from pyarchinit_photogrammetry.gui.model_view import ModelView

    mv = ModelView()
    tb = mv.canvas.trackball
    assert mv.trackball_mode() == "auto" and mv.trackball_actions["auto"].isChecked()
    assert tb.opacity() == 0.0  # the mouse is not over the view
    tb.touched(10, 10)
    assert tb.opacity() == 1.0
    now = tb._last_move
    assert tb.opacity(now + tbm.FADE_AFTER + tbm.FADE_TIME / 2) == pytest.approx(0.5)
    assert tb.opacity(now + tbm.FADE_AFTER + tbm.FADE_TIME + 0.01) == 0.0
    tb.left()
    assert tb.opacity() == 0.0
    mv.trackball_actions["always"].trigger()
    assert tb.opacity() == 1.0 and app_settings().value(tbm.SETTING) == "always"
    mv.close()
    mv2 = ModelView()  # the setting is remembered
    assert mv2.trackball_mode() == "always" and mv2.trackball_actions["always"].isChecked()
    mv2.set_trackball_mode("never")
    assert mv2.canvas.trackball.opacity() == 0.0 and app_settings().value(tbm.SETTING) == "never"
    mv2.close()


def test_trackball_is_drawn_and_never_grabbed(canvas):
    c = canvas
    c.set_trackball_mode("always")
    with_tb = c.grab().toImage()
    frame = c.grab_array()  # the frame for lasso and picks: without the sphere
    c.set_trackball_mode("never")
    without = c.grab_array()
    assert np.array_equal(frame, without)
    from PySide6.QtGui import QImage

    a = with_tb.convertToFormat(QImage.Format.Format_RGB888)
    arr = np.frombuffer(a.constBits(), np.uint8, a.sizeInBytes()).reshape(a.height(), a.bytesPerLine())
    arr = arr[:, : a.width() * 3].reshape(a.height(), a.width(), 3)
    assert (arr != without).any()


@pytest.fixture
def model_view(processed, app, monkeypatch):
    monkeypatch.setenv("PPG_RENDERER", "software")
    from pyarchinit_photogrammetry.gui.model_view import ModelView

    _doc, chunk, _ = processed
    mv = ModelView()
    mv.resize(640, 480)
    mv.show()
    app.processEvents()
    mv.set_chunk(chunk, "point_cloud")
    app.processEvents()
    yield mv, chunk
    mv.close()


def test_double_click_sets_the_pivot_on_the_surface(model_view, app):
    mv, chunk = model_view
    c = mv.canvas
    w, h = c.view_size()
    frame0, pick0 = mv.view_frame(), None
    _img, project = c.snapshot()
    pick0 = mv.pick_point(w / 2 + 20, h / 2 + 10, project, c.view_ray)
    R0, eye0 = c.cam.basis(), c.cam.eye.copy()
    c.pivot_requested.emit(w / 2 + 20, h / 2 + 10)  # what a double click there sends
    pivot = mv.pivot()
    assert pivot is not None
    xyz = chunk.to_world(chunk.point_cloud.xyz)
    assert np.min(np.linalg.norm(xyz - pivot, axis=1)) < 0.005  # on the floor of the trench
    assert np.allclose(c.cam.basis(), R0) and np.allclose(c.cam.eye, eye0)  # the view did not move
    # picks and the frame of the view do not depend on the trackball
    _img, project = c.snapshot()
    assert np.allclose(mv.pick_point(w / 2 + 20, h / 2 + 10, project, c.view_ray), pick0)
    frame1 = mv.view_frame()
    for key in ("centre", "toward", "up", "corners"):
        assert np.allclose(frame0[key], frame1[key])
    # the pivot stays on its point of the scene when the heights are exaggerated
    mv.set_exaggeration(3.0)
    assert np.allclose(mv.pivot(), pivot, atol=1e-6)
    mv.set_exaggeration(1.0)
    # Centra la vista sul punto: the pivot in the middle of the view
    mv.b_center.click()
    assert np.allclose(_screen(c, c.trackball.pivot), (w / 2, h / 2), atol=1e-6)


def test_vtk_trackball(app, monkeypatch):
    from pyarchinit_photogrammetry.gui import model_view as mvm

    monkeypatch.setenv("PPG_RENDERER", "vtk")
    if os.environ.get("PPG_TEST_VTK") != "1":
        pytest.skip("VTK canvas: opt-in with PPG_TEST_VTK=1 (a QtInteractor without an OpenGL context may "
                    "crash on some platforms)")
    if not mvm._vtk_usable():
        pytest.skip("VTK is not usable here")
    c = mvm.VtkCanvas()
    c.resize(400, 300)
    c.show()
    app.processEvents()
    c.set_scene(_scene())
    tb = c.trackball
    tilt = float(np.degrees(np.arccos(c.get_cam().basis()[2] @ [0, 0, 1.0])))
    geo = tb.geometry()
    xy, ok = geo["rings"]["up"][2], geo["rings"]["up"][3]
    start = xy[int(np.argmax(np.where(ok, xy[:, 1], -np.inf)))]
    pivot_before = np.array(c.get_cam().project(tb.pivot[None], *c.view_size())[:2]).ravel()
    tb.press(*start, "rotate")
    tb.move(*(start + (40, 3)))
    tb.release()
    cam = c.get_cam()
    assert float(np.degrees(np.arccos(cam.basis()[2] @ [0, 0, 1.0]))) == pytest.approx(tilt, abs=1e-4)
    assert np.allclose(np.array(cam.project(tb.pivot[None], *c.view_size())[:2]).ravel(), pivot_before, atol=1e-3)
    c.set_trackball_mode("always")
    c._update_trackball_actors()  # what happens before every render
    actor, poly = c._tb_actors["up"]
    assert actor.GetVisibility() and poly.GetNumberOfPoints() == 96
    assert poly.GetNumberOfLines() + c._tb_actors["up_back"][1].GetNumberOfLines() == 96  # front and back halves
    x, y, _z, _ok = cam.project(np.array([poly.GetPoint(i) for i in range(96)]), *c.view_size())
    r = np.hypot(x - pivot_before[0], y - pivot_before[1])
    assert r.max() == pytest.approx(0.4 * min(c.view_size()), rel=0.05)  # 40 % of the short side
    c.set_trackball_mode("never")
    c._update_trackball_actors()
    assert not actor.GetVisibility()
    # the interactor style hands VTK's events to the trackball
    iren = c.plotter.iren.interactor
    assert iren.GetInteractorStyle() is c._style
    got = []
    c.pivot_requested.connect(lambda x, y: got.append((x, y)))

    def event(name, x, y, repeat=0):
        iren.SetEventInformation(x, y, 0, 0, chr(0), repeat)
        iren.InvokeEvent(name)

    R0 = c.get_cam().basis()
    event("LeftButtonPressEvent", 200, 150)
    event("MouseMoveEvent", 230, 160)
    event("LeftButtonReleaseEvent", 230, 160)
    assert not np.allclose(c.get_cam().basis(), R0)
    d = c.get_cam().distance
    event("MouseWheelForwardEvent", 50, 50)
    assert c.get_cam().distance == pytest.approx(d * 0.88)
    event("LeftButtonPressEvent", 100, 100, repeat=1)
    assert len(got) == 1
    c.close()


def test_ring_drag_near_the_axis_does_not_jump(canvas):
    """Seen from above the horizontal ring's axis points at the viewer: around its projection the
    angle is undefined and the camera waits for the cursor to leave it."""
    c = canvas
    tb = c.trackball
    c.top_view()
    geo = tb.geometry()
    cx, cy = geo["centre"]
    xy = geo["rings"]["up"][2]
    start = xy[int(np.argmax(xy[:, 1]))]
    assert tb.hit(*start) == ("ring", "up")
    tb.press(*start, "rotate")
    tb.move(start[0] + 5, start[1])
    R0 = c.cam.basis()
    tb.move(cx + 1, cy + 1)  # onto the axis
    assert np.allclose(c.cam.basis(), R0)
    tb.move(cx - 1, cy + 2)  # around it, still within 5 % of the radius
    assert np.allclose(c.cam.basis(), R0)
    tb.release()


def test_top_view_puts_the_pivot_back_in_the_middle(canvas):
    c = canvas
    c.trackball.set_pivot([1.0, 1.0, 0.0])
    _drag(c, [(100, 100), (160, 120)], kind="pan")
    c.top_view()
    assert np.allclose(c.pivot(), c.sd.center)
    assert np.allclose(_screen(c, c.pivot()), np.array(c.view_size()) / 2)


def test_vtk_keys_and_wheel_are_the_trackball_s_only(app, monkeypatch):
    """pyvista's own keys never act beside the trackball's, Shift+wheel sideways (macOS) dollies, and
    the back half of the rings is a dimmer actor of its own."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    from pyarchinit_photogrammetry.gui import model_view as mvm

    monkeypatch.setenv("PPG_RENDERER", "vtk")
    if os.environ.get("PPG_TEST_VTK") != "1":
        pytest.skip("VTK canvas: opt-in with PPG_TEST_VTK=1 (a QtInteractor without an OpenGL context may "
                    "crash on some platforms)")
    if not mvm._vtk_usable():
        pytest.skip("VTK is not usable here")
    c = mvm.VtkCanvas()
    c.resize(400, 300)
    c.show()
    app.processEvents()
    c.set_scene(_scene())
    iren = c.plotter.iren.interactor
    vcam = c.plotter.renderer.GetActiveCamera()
    for sym in ("Up", "Down", "plus", "minus", "q", "v", "b", "C"):
        angle, size = vcam.GetViewAngle(), c._cloud_actor.GetProperty().GetPointSize()
        iren.SetKeySym(sym)
        iren.InvokeEvent("KeyPressEvent")
        assert vcam.GetViewAngle() == pytest.approx(angle), sym
        assert c._cloud_actor.GetProperty().GetPointSize() == pytest.approx(size), sym
    assert not getattr(c.plotter, "_closed", False)
    d, p = c.get_cam().distance, c.pivot()
    iren.SetKeySym("plus")
    iren.InvokeEvent("KeyPressEvent")
    assert c.get_cam().distance == pytest.approx(d * 0.88)  # the trackball's own zoom
    d = c.get_cam().distance
    ev = QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(0, 0), QPoint(120, 0), Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.ShiftModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(c.plotter.interactor, ev)
    assert c.get_cam().distance == pytest.approx(d * 0.88)
    assert np.allclose(c.pivot(), p)  # a dolly: the pivot stays where it is
    c.set_trackball_mode("always")
    c._update_trackball_actors()
    front, back = c._tb_actors["up"], c._tb_actors["up_back"]
    assert front[1].GetNumberOfLines() + back[1].GetNumberOfLines() == 96
    assert 0 < back[1].GetNumberOfLines() < 96
    assert back[0].GetProperty().GetOpacity() < front[0].GetProperty().GetOpacity()
    c.close()
