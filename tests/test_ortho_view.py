"""The orthographic (parallel) projection of the 3D view: the maths of the camera, the trackball in
it (zoom is a scale), picks, and the toggle of the view with its setting."""
from __future__ import annotations

import os

import numpy as np
import pytest

from pyarchinit_photogrammetry.gui.scene import OrbitCamera, splat

W, H = 400, 300


def _cam(ortho=True, **kw):
    return OrbitCamera(np.array([0.5, -0.2, 0.1]), 6.0, yaw=-50.0, pitch=30.0, ortho=ortho, **kw)


def test_parallel_lines_stay_parallel_and_sizes_do_not_depend_on_depth():
    cam = _cam(back=40.0)
    R = cam.basis()
    rng = np.random.default_rng(0)
    a = rng.uniform(-1, 1, (50, 3))
    direction = np.array([0.3, 0.8, -0.5])
    b = a + direction * rng.uniform(0.2, 2.0, (50, 1))
    xa, ya, _za, oka = cam.project(a, W, H)
    xb, yb, _zb, okb = cam.project(b, W, H)
    assert oka.all() and okb.all()
    v = np.c_[xb - xa, yb - ya]
    v /= np.linalg.norm(v, axis=1)[:, None]
    assert np.allclose(v, v[0], atol=1e-9)  # one direction on the screen for every one of the parallel segments
    # a segment across the view has the same length on the screen wherever it is along the line of sight
    seg = np.array([[0.0, 0, 0], R[0] * 0.5])
    lengths = []
    for depth in (-3.0, 0.0, 2.0, 5.0):
        x, y, _z, _ok = cam.project(seg + R[2] * depth, W, H)
        lengths.append(np.hypot(x[1] - x[0], y[1] - y[0]))
    assert np.allclose(lengths, lengths[0])
    assert lengths[0] == pytest.approx(0.5 / cam.pixel_size(1.0, H))
    # in perspective the same segment shrinks with the distance
    persp = _cam(ortho=False)
    near, far = (np.hypot(*np.diff(np.array(persp.project(seg + R[2] * d, W, H)[:2]), axis=1).ravel())
                 for d in (-3.0, 5.0))
    assert near > far * 2


@pytest.mark.parametrize("ortho", [False, True])
def test_project_and_unproject_are_inverse(ortho):
    cam = _cam(ortho=ortho, back=25.0)
    pts = np.random.default_rng(1).uniform(-1.5, 1.5, (200, 3))
    x, y, z, ok = cam.project(pts, W, H)
    assert ok.all()
    assert np.allclose(cam.unproject(x, y, z, W, H), pts, atol=1e-9)
    # the line of sight through a pixel passes through the points seen there
    for i in (0, 17, 99):
        o, d = cam.ray(x[i], y[i], W, H)
        assert np.linalg.norm(np.cross(pts[i] - o, d)) < 1e-9 and (pts[i] - o) @ d > 0
    if ortho:  # every line of sight has the direction of the view
        assert np.allclose(cam.ray(3, 7, W, H)[1], cam.basis()[2])


def test_changing_the_projection_keeps_what_is_at_the_target():
    persp, ortho = _cam(ortho=False), _cam(ortho=True)
    R = persp.basis()
    pts = persp.target + np.array([R[0] * 0.7, R[1] * -0.4, R[0] * -1.1 + R[1] * 0.3])  # at the target's depth
    assert np.allclose(persp.project(pts, W, H)[:2], ortho.project(pts, W, H)[:2])
    # the picture does not depend on how far back the orthographic camera stands, the depths do
    far = _cam(back=100.0)
    assert np.allclose(far.project(pts, W, H)[:2], ortho.project(pts, W, H)[:2])
    assert np.allclose(far.project(pts, W, H)[2], 100.0)


def test_the_software_renderer_draws_the_orthographic_view():
    """A square grid seen from above covers the same pixels whatever its height."""
    g = np.linspace(-1, 1, 60)
    xx, yy = np.meshgrid(g, g)
    cols = np.full((xx.size, 3), 255, np.uint8)
    cam = OrbitCamera(np.zeros(3), 6.0, ortho=True, back=20.0)
    cam.R = cam._from_angles(-90.0, 90.0)
    boxes = []
    for z in (-2.0, 0.0, 3.0):
        img = splat(np.c_[xx.ravel(), yy.ravel(), np.full(xx.size, z)], cols, cam, W, H, size=1, background=(0, 0, 0))
        rows, columns = np.nonzero(img[:, :, 0])
        boxes.append((rows.min(), rows.max(), columns.min(), columns.max()))
    assert boxes[0] == boxes[1] == boxes[2]
    assert boxes[0][1] - boxes[0][0] == pytest.approx(2.0 / cam.pixel_size(1.0, H), abs=1.5)


# ----------------------------------------------------------------------------- the canvases
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
    c.resize(W, H)
    c.set_scene(_scene())
    yield c
    c.close()


def _screen(c, p):
    x, y, _z, _ok = c.get_cam().project(np.atleast_2d(np.asarray(p, float)), *c.view_size())
    return np.c_[x, y]


def test_toggle_does_not_jump_and_everything_stays_in_front(canvas):
    c = canvas
    R = c.cam.basis()
    at_target = c.cam.target + np.array([R[0] * 0.8, R[1] * 0.5])
    before = _screen(c, at_target)
    c.set_ortho(True)
    assert c.cam.ortho and np.allclose(_screen(c, at_target), before)
    assert c.cam.project(c.sd.points, *c.view_size())[3].all()  # nothing of the scene behind the plane of sight
    c.set_ortho(False)
    assert not c.cam.ortho and np.allclose(_screen(c, at_target), before)


def test_zoom_is_a_scale_and_keeps_the_point_under_the_cursor(canvas):
    c = canvas
    c.set_ortho(True)
    tb = c.trackball
    w, h = c.view_size()
    fwd = c.cam.basis()[2].copy()
    o, d, pixel = c.view_ray(70, 40)
    under = o + d * 9.0  # a point seen at the cursor, wherever it is along the line of sight
    scale0, pivot0 = pixel(1.0), _screen(c, c.pivot())
    tb.zoom(3, 70, 40)
    assert np.allclose(c.cam.basis()[2], fwd)
    _o, _d, pixel = c.view_ray(70, 40)
    assert pixel(1.0) == pytest.approx(scale0 * 0.88 ** 3) and pixel(50.0) == pixel(1.0)
    assert np.allclose(_screen(c, under), (70, 40), atol=1e-6)
    assert np.allclose(_screen(c, tb.pivot), pivot0, atol=1e-6)
    tb.zoom(-1, dolly=True)  # Shift+wheel: the scale again, around the middle of the view
    assert c.view_ray(0, 0)[2](1.0) == pytest.approx(scale0 * 0.88 ** 2)
    # pan: the scene follows the cursor by the pixels dragged, at every depth
    p = np.array([[0.3, 0.2, 0.1], [-1.0, 0.4, -0.2]])
    s0 = _screen(c, p)
    tb.press(100, 100, "pan")
    tb.move(130, 85)
    tb.release()
    assert np.allclose(_screen(c, p) - s0, (30, -15), atol=1e-6)
    # the trackball keeps its size on the screen
    geo = tb.geometry()
    ring = _screen(c, geo["rings"]["up"][1])
    assert np.allclose(np.hypot(*(ring - geo["centre"]).T).max(), 0.4 * min(w, h), rtol=0.02)
    # centring on the pivot does not change the scale
    scale = c.view_ray(0, 0)[2](1.0)
    tb.set_pivot(np.array([0.5, 0.5, 0.0]), recentre=True)
    assert c.view_ray(0, 0)[2](1.0) == pytest.approx(scale)
    assert np.allclose(_screen(c, tb.pivot), (w / 2, h / 2), atol=1e-6)


def test_views_from_above_and_the_side_are_exact(canvas):
    c = canvas
    c.set_ortho(True)
    c.top_view()
    assert c.cam.ortho and np.allclose(c.cam.basis()[2], (0, 0, -1), atol=1e-12)
    # a plan: points one above the other fall on the same pixel
    column = np.array([[0.7, 0.3, -0.3], [0.7, 0.3, 0.3]])
    s = _screen(c, column)
    assert np.allclose(s[0], s[1], atol=1e-9)
    c.trackball.key("left_side")  # along +x: what is aligned along x is one point
    s = _screen(c, np.array([[-2.0, 0.4, 0.1], [2.0, 0.4, 0.1]]))
    assert np.allclose(s[0], s[1], atol=1e-9)
    c.reset_view()
    assert c.cam.ortho  # a new camera keeps the projection chosen


def test_a_photo_looked_through_stays_in_perspective(canvas):
    c = canvas
    c.set_ortho(True)
    cam = OrbitCamera(np.zeros(3), 4.0, fov=55.0)
    c.look(cam)
    assert c.look_on and not c.get_cam().ortho
    c.trackball.turn(np.array([0, 0, 1.0]), 0.2)  # the view moves: out of the photo, back to the projection chosen
    assert not c.look_on and c.get_cam().ortho
    c.look(OrbitCamera(np.zeros(3), 4.0, fov=55.0))
    c.end_look()  # Esc
    assert c.get_cam().ortho


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


def test_toggle_of_the_view_its_setting_and_picks(model_view, app):
    from scipy.spatial import cKDTree

    from pyarchinit_photogrammetry.gui import app_settings
    from pyarchinit_photogrammetry.gui.model_view import ORTHO_SETTING, ModelView

    mv, chunk = model_view
    c = mv.canvas
    w, h = c.view_size()
    assert not mv.ortho() and not mv.a_ortho.isChecked()
    mv.b_ortho.click()  # the button of the rail
    assert mv.ortho() and mv.a_ortho.isChecked() and mv.b_ortho.isChecked() and c.cam.ortho
    assert app_settings().value(ORTHO_SETTING) == "true"
    other = ModelView()  # the choice is found again
    assert other.ortho() and other.a_ortho.isChecked()
    other.close()
    # a pick in the orthographic view: on the surface, on the line of sight of the pixel
    xyz = chunk.to_world(chunk.point_cloud.xyz)
    tree = cKDTree(xyz)
    c.top_view()
    app.processEvents()
    for x, y in ((w / 2 + 20, h / 2 + 10), (w / 2 - 35, h / 2 - 15)):
        _img, project = c.snapshot()
        picked = mv.pick_point(x, y, project, c.view_ray)
        assert picked is not None
        world = chunk.to_world(np.asarray(picked)[None])[0]
        assert tree.query(world)[0] < 0.02
        px, py, _depth, ok = project(mv.sd.to_display(world[None] - mv.sd.offset))
        assert ok[0] and np.hypot(px[0] - x, py[0] - y) < 1.0
        o, d, _pixel = c.view_ray(x, y)
        assert np.allclose(d, c.cam.basis()[2])
    # the double click puts the centre of rotation there, the frame of the view is the plan
    c.pivot_requested.emit(w / 2 + 20, h / 2 + 10)
    assert tree.query(mv.pivot())[0] < 0.02
    frame = mv.view_frame()
    toward = frame["toward"] / np.linalg.norm(frame["toward"])
    assert np.allclose(toward, (0, 0, 1), atol=1e-6)
    width = np.linalg.norm(frame["corners"][1] - frame["corners"][0])
    assert width == pytest.approx(w * c.view_ray(0, 0)[2](1.0), rel=1e-6)
    # a frame grabbed from the view is the orthographic one
    ortho_frame = c.grab_array()
    mv.a_ortho.trigger()  # the action of the Vista menu
    assert not mv.ortho() and not mv.b_ortho.isChecked() and app_settings().value(ORTHO_SETTING) == "false"
    assert (c.grab_array() != ortho_frame).any()
    from PySide6.QtCore import Qt

    assert c.view_key(Qt.Key.Key_O) and mv.ortho()  # the key O on the view


def test_vtk_orthographic(app, monkeypatch):
    from pyarchinit_photogrammetry.gui import model_view as mvm

    monkeypatch.setenv("PPG_RENDERER", "vtk")
    if os.environ.get("PPG_TEST_VTK") != "1":
        pytest.skip("VTK canvas: opt-in with PPG_TEST_VTK=1 (a QtInteractor without an OpenGL context may "
                    "crash on some platforms)")
    if not mvm._vtk_usable():
        pytest.skip("VTK is not usable here")
    c = mvm.VtkCanvas()
    c.resize(W, H)
    c.show()
    app.processEvents()
    c.set_scene(_scene())
    cam0 = c.get_cam()
    R = cam0.basis()
    pts = cam0.target + np.array([R[0] * 0.8, R[1] * 0.5, R[2] * 0.6 - R[0] * 0.3])
    project = c.projector()
    before = np.array(project(pts)[:2]).T
    c.set_ortho(True)
    vtk_cam = c.plotter.renderer.GetActiveCamera()
    assert vtk_cam.GetParallelProjection()
    cam = c.get_cam()
    assert cam.ortho and cam.distance == pytest.approx(cam0.distance)
    project = c.projector()
    x, y, depth, ok = project(pts)
    assert ok.all() and np.allclose(np.c_[x, y][:2], before[:2], atol=1e-3)  # at the target: where they were
    # the heights on the screen are the camera's (the widths follow the render window, which has no size of its own
    # without an OpenGL context)
    assert np.allclose(y, cam.project(pts, *c.view_size())[1], atol=1e-3)
    assert depth[2] - depth[0] == pytest.approx(0.6, abs=1e-6)  # depths along the line of sight
    assert project(np.asarray(c.sd.points, float))[2].min() > 0  # the camera stands behind the whole scene
    o, d, pixel = c.view_ray(*np.c_[x, y][2])
    assert np.allclose(d, R[2], atol=1e-6) and np.linalg.norm(np.cross(pts[2] - o, d)) < 1e-4
    scale = pixel(1.0)
    c.trackball.zoom(2, 60, 50)
    assert c.view_ray(60, 50)[2](1.0) == pytest.approx(scale * 0.88 ** 2, rel=1e-6)
    assert np.allclose(c.get_cam().basis(), R, atol=1e-9)
    c.top_view()
    c.reset_view()
    assert c.plotter.renderer.GetActiveCamera().GetParallelProjection()
    c.set_ortho(False)
    assert not c.plotter.renderer.GetActiveCamera().GetParallelProjection()
    c.close()
