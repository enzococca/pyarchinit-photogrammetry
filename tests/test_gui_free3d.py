"""Free drawings in the 3D view: lines, polygons and points clicked on the model with the view live between the
clicks (a drag still rotates), the rubber band following the cursor, the snap to the vertices shown, Backspace,
Enter and Esc; the features given shown by the view and kept through a vertical exaggeration; a feature selected,
its vertices dragged on the frozen view and picked again on the surface, deleted, draped on the model."""
from __future__ import annotations

import os

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
    """The 3D view alone on the trench's model, seen from above; ``view.drawn`` records ``free_drawn``."""
    monkeypatch.setenv("PPG_RENDERER", "software")
    from pyarchinit_photogrammetry.gui.model_view import ModelView

    _doc, chunk, _ = processed
    mv = ModelView()
    mv.resize(640, 480)
    mv.show()
    app.processEvents()
    mv.set_chunk(chunk, "model")
    mv.canvas.top_view()
    app.processEvents()
    mv.drawn = []
    mv.free_drawn.connect(lambda kind, xyz: mv.drawn.append((kind, np.asarray(xyz, float))))
    yield mv
    mv.stop_drawing()
    mv.stop_free_select()
    mv.close()


def _targets(view, n: int) -> np.ndarray:
    """``n`` vertices of the model (internal) spread over the middle of the view, a few tens of pixels apart."""
    chunk, sd = view.chunk, view.sd
    v = np.asarray(chunk.model.vertices, np.float64)
    _img, project = view.canvas.snapshot()
    x, y, _d, ok = project(sd.to_display(chunk.to_world(v) - sd.offset))
    w, h = view.canvas.view_size()
    inner = ok & (x > 0.25 * w) & (x < 0.75 * w) & (y > 0.25 * h) & (y < 0.75 * h)
    idx = np.nonzero(inner)[0]
    assert len(idx) > 50
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False)
    cx, cy, r = w / 2, h / 2, 0.18 * min(w, h)
    out = []
    for a in angles:
        tx, ty = cx + r * np.cos(a), cy + r * np.sin(a)
        out.append(v[idx[np.argmin((x[idx] - tx) ** 2 + (y[idx] - ty) ** 2)]])
    return np.array(out)


def _key(view, key):
    from PySide6.QtTest import QTest

    QTest.keyClick(view.canvas, key)


def test_clicked_vertices_lie_on_the_model(view, app):
    from free3d_helpers import click_vertex, distance_to_model, double_click_at, median_edge, screen_of
    from PySide6.QtCore import Qt

    chunk = view.chunk
    tol = median_edge(chunk)
    view.start_drawing("line")
    assert view.drawing_kind == "line"
    for p in _targets(view, 4):
        click_vertex(view, p)
    assert len(view.drawing_vertices()) == 4
    assert view.drawn == []
    _key(view, Qt.Key.Key_Return)
    assert len(view.drawn) == 1
    kind, xyz = view.drawn[0]
    assert kind == "line" and xyz.shape == (4, 3)
    assert distance_to_model(chunk, xyz).max() < tol

    view.start_drawing("polygon")
    targets = _targets(view, 6)
    for p in targets[:5]:
        click_vertex(view, p)
    x, y = screen_of(view, targets[5])
    double_click_at(view, x, y)  # its first click adds the sixth vertex, the second ends
    assert len(view.drawn) == 2
    kind, xyz = view.drawn[1]
    assert kind == "polygon" and len(xyz) == 6
    assert len(np.unique(np.round(xyz, 9), axis=0)) == len(xyz)  # no vertex twice from the double click
    assert distance_to_model(chunk, xyz).max() < tol

    view.start_drawing("point")  # a point ends at its click
    click_vertex(view, targets[0])
    assert len(view.drawn) == 3 and view.drawn[2][0] == "point" and view.drawn[2][1].shape == (1, 3)
    assert distance_to_model(chunk, view.drawn[2][1]).max() < tol


def test_backspace_esc_and_minimum(view, app):
    from free3d_helpers import click_vertex
    from PySide6.QtCore import Qt

    targets = _targets(view, 3)
    view.start_drawing("polygon")
    for p in targets:
        click_vertex(view, p)
    _key(view, Qt.Key.Key_Backspace)
    assert len(view.drawing_vertices()) == 2
    _key(view, Qt.Key.Key_Return)  # a polygon of two vertices: nothing, the drawing goes on
    assert view.drawn == [] and view.drawing_kind == "polygon"
    assert len(view.drawing_vertices()) == 2
    _key(view, Qt.Key.Key_Escape)
    assert view.drawn == [] and view.drawing_kind is None
    assert view.sd.drawing is None
    # starting a pick ends the drawing; starting a drawing ends a pick
    view.start_drawing("line")
    view.start_pick("marker")
    assert view.drawing_kind is None
    view.start_drawing("line")
    assert view.stack.currentWidget() is view.canvas and view.drawing_kind == "line"


def test_drag_rotates_without_a_vertex(view, app):
    from free3d_helpers import click_vertex, screen_of
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    targets = _targets(view, 2)
    view.start_drawing("line")
    click_vertex(view, targets[0])
    R0 = view.canvas.cam.basis().copy()
    x, y = screen_of(view, targets[1])
    c = view.canvas
    QTest.mousePress(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(x), int(y)))
    for k in range(1, 7):
        QTest.mouseMove(c, QPoint(int(x) + 5 * k, int(y)))
    QTest.mouseRelease(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(x) + 30, int(y)))
    app.processEvents()
    assert not np.allclose(c.cam.basis(), R0)  # the view turned
    assert len(view.drawing_vertices()) == 1  # and no vertex was added
    click_vertex(view, targets[1])  # the next click after the rotation still lands on the model
    assert len(view.drawing_vertices()) == 2


def test_snap_to_an_existing_vertex(view, app):
    from free3d_helpers import click_at, screen_of

    chunk = view.chunk
    a, b = _targets(view, 2)
    world = chunk.to_world(np.array([a, b]))
    view.set_free([{"id": "f1", "kind": "line", "xyz": world.tolist()}], {"f1": "#ff0000"})
    assert view.sd.free and view.sd.free[0][0] == "f1"
    view.start_drawing("point")
    x, y = screen_of(view, a)
    click_at(view, x + 3, y + 4)  # 5 px off the vertex
    assert len(view.drawn) == 1
    # the feature's own vertex, as the view converts it (not a point of the surface 5 px away)
    assert np.allclose(view.drawn[0][1][0], chunk.to_internal(world)[0], atol=1e-9, rtol=0)


def test_snap_never_takes_a_vertex_behind_the_surface(view, app):
    from free3d_helpers import click_at, distance_to_model, median_edge, screen_of

    chunk = view.chunk
    a, b = _targets(view, 2)
    x, y = screen_of(view, a)
    o, d, _metre = view._sight(x, y)
    depth = 6 * median_edge(chunk)
    hidden = a + depth * d  # on the line of sight through a, under the surface
    world = chunk.to_world(np.array([hidden, b]))
    view.set_free([{"id": "under", "kind": "point", "xyz": world[:1].tolist()},
                   {"id": "seen", "kind": "point", "xyz": world[1:].tolist()}], {})
    view.start_drawing("point")
    click_at(view, x + 3, y + 4)  # 5 px from where the hidden point projects: the surface, not the point
    assert len(view.drawn) == 1
    got = view.drawn[0][1][0]
    assert np.linalg.norm(got - hidden) > 3 * median_edge(chunk)
    assert distance_to_model(chunk, got[None])[0] < median_edge(chunk)
    x, y = screen_of(view, b)
    click_at(view, x + 3, y + 4)  # a visible vertex: snapped exactly
    assert np.allclose(view.drawn[1][1][0], chunk.to_internal(world[1:])[0], atol=1e-9, rtol=0)


def test_rubber_band_follows_the_cursor(view, app):
    from free3d_helpers import click_vertex, screen_of
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest

    targets = _targets(view, 3)
    view.start_drawing("line")
    click_vertex(view, targets[0])
    x, y = screen_of(view, targets[1])
    QTest.qWait(40)
    QTest.mouseMove(view.canvas, QPoint(int(x), int(y)))
    QTest.qWait(60)
    first = view.sd.drawing
    assert first is not None and len(first) == 2
    x, y = screen_of(view, targets[2])
    QTest.mouseMove(view.canvas, QPoint(int(x), int(y)))
    QTest.qWait(60)
    second = view.sd.drawing
    assert second is not None and len(second) == 2
    assert np.allclose(second[0], first[0])
    assert not np.allclose(second[-1], first[-1])


def test_free_items_survive_exaggeration(view, app):
    from free3d_helpers import click_vertex

    from pyarchinit_photogrammetry.gui.scene import exaggerated

    chunk = view.chunk
    a, b, c = _targets(view, 3)
    world = chunk.to_world(np.array([a, b, c]))
    view.set_free([{"id": "p", "kind": "polygon", "xyz": world.tolist()}], {"p": "#00ff00"}, selected={"p"})
    view.start_drawing("line")
    click_vertex(view, a)
    sd = view.base_sd
    assert sd.free and sd.drawing is not None
    ex = exaggerated(sd, 2.0)
    assert [f[0] for f in ex.free] == ["p"] and ex.free[0][1] == "polygon" and ex.free[0][4]
    assert np.allclose(ex.free[0][2], ex.to_display(sd.free[0][2]))
    assert not np.allclose(ex.free[0][2], sd.free[0][2])
    assert np.allclose(ex.drawing, ex.to_display(sd.drawing))
    view.set_exaggeration(2.0)
    assert view.sd.free and np.allclose(view.sd.free[0][2], view.sd.to_display(world - view.sd.offset))
    assert view.drawing_kind == "line" and view.sd.drawing is not None
    view.set_exaggeration(1.0)
    view.set_free([], {})
    assert view.sd.free == []


def _line(view, a, b, fid="f1"):
    world = view.chunk.to_world(np.array([a, b]))
    view.set_free([{"id": fid, "kind": "line", "xyz": world.tolist()}], {})
    return world


def _record(view):
    got = {"selected": [], "moved": [], "deleted": [], "draped": []}
    view.free_selected.connect(lambda fid: got["selected"].append(fid))
    view.free_vertex_moved.connect(lambda fid, i, xyz: got["moved"].append((fid, i, np.asarray(xyz, float))))
    view.free_delete_requested.connect(lambda fid, i: got["deleted"].append((fid, i)))
    view.free_drape_requested.connect(lambda fid, xyz: got["draped"].append((fid, np.asarray(xyz, float))))
    return got


def _select_middle(view, a, b):
    from free3d_helpers import click_at, screen_of

    (xa, ya), (xb, yb) = screen_of(view, a), screen_of(view, b)
    view.start_free_select()
    click_at(view, (xa + xb) / 2 + 2, (ya + yb) / 2 + 3)  # beside the segment, within 6 px


def test_click_selects_a_feature(view, app):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    a, b = _targets(view, 2)
    _line(view, a, b)
    got = _record(view)
    view.start_drawing("line")
    _select_middle(view, a, b)
    assert view.drawing_kind is None  # selecting ended the drawing
    assert got["selected"] == ["f1"]
    assert view.sd.free[0][4] and view.base_sd.free[0][4]  # drawn thicker
    assert view.stack.currentWidget() is view.editor and len(view.editor.handles) == 2
    assert view.a_free_drape.isEnabled()
    QTest.mouseClick(view.editor, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(3, 3))
    assert got["selected"] == ["f1", ""] and not view.sd.free[0][4]
    assert view.stack.currentWidget() is view.canvas
    view.start_drawing("line")  # drawing ends the select mode
    assert not view.free_select_on


def _drag(widget, start, end):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    p0 = QPoint(int(round(start[0])), int(round(start[1])))
    p1 = QPoint(int(round(end[0])), int(round(end[1])))
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
    for k in range(1, 6):
        QTest.mouseMove(widget, p0 + (p1 - p0) * k / 5)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)


def test_moved_vertex_is_picked_again_on_the_surface(view, app):
    from free3d_helpers import distance_to_model, median_edge, screen_of

    a, b, c = _targets(view, 3)
    _line(view, a, b)
    got = _record(view)
    _select_middle(view, a, b)
    ed = view.editor
    _drag(ed, screen_of(view, a), screen_of(view, c))
    assert len(got["moved"]) == 1
    fid, i, xyz = got["moved"][0]
    assert fid == "f1" and i == 0
    assert distance_to_model(view.chunk, xyz[None])[0] < median_edge(view.chunk)
    # the empty sky: no surface there, no signal, the handle back where it was
    w, h = view.canvas.view_size()
    project = view.canvas.projector()
    sky = next(((x, y) for x, y in ((2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3))
                if view._surface_at(x, y, project, refine=True) is None), None)
    assert sky is not None
    before = (ed.handles[1].x(), ed.handles[1].y())
    _drag(ed, before, sky)
    assert len(got["moved"]) == 1
    assert (ed.handles[1].x(), ed.handles[1].y()) == before


def test_delete_a_vertex_or_the_feature(view, app):
    from free3d_helpers import screen_of
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    a, b = _targets(view, 2)
    _line(view, a, b)
    got = _record(view)
    _select_middle(view, a, b)
    ed = view.editor
    QTest.keyClick(ed, Qt.Key.Key_Delete)  # no handle chosen: the feature
    assert got["deleted"] == [("f1", -1)]
    x, y = screen_of(view, b)
    QTest.mouseClick(ed, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(x), int(y)))
    QTest.keyClick(ed, Qt.Key.Key_Delete)
    assert got["deleted"] == [("f1", -1), ("f1", 1)]
    QTest.keyClick(ed, Qt.Key.Key_Escape)
    assert view.stack.currentWidget() is view.canvas


def test_drape_adds_surface_points(view, app):
    from free3d_helpers import distance_to_model, median_edge

    chunk, sd = view.chunk, view.sd
    v = np.asarray(chunk.model.vertices, np.float64)
    project = view.canvas.projector()
    x, y, _d, ok = project(sd.to_display(chunk.to_world(v) - sd.offset))
    w, h = view.canvas.view_size()
    ia = int(np.argmin(np.where(ok, (x - (w / 2 - 100)) ** 2 + (y - h / 2) ** 2, np.inf)))
    ib = int(np.argmin(np.where(ok, (x - (x[ia] + 200)) ** 2 + (y - y[ia]) ** 2, np.inf)))
    a, b = v[ia], v[ib]
    assert abs(np.hypot(x[ib] - x[ia], y[ib] - y[ia]) - 200) < 5
    out = view.drape(np.array([a, b]), project, view.canvas.view_ray)
    assert 9 <= len(out) - 2 <= 11
    assert np.allclose(out[0], a) and np.allclose(out[-1], b)
    assert distance_to_model(chunk, out).max() < median_edge(chunk)
    # from the menu, on the selected line
    _line(view, a, b)
    got = _record(view)
    _select_middle(view, a, b)
    view.a_free_drape.trigger()
    assert len(got["draped"]) == 1 and got["draped"][0][0] == "f1"
    assert 9 <= len(got["draped"][0][1]) - 2 <= 11


def test_snap_off_the_model_checks_the_candidate_s_own_line_of_sight(view, app, monkeypatch):
    from free3d_helpers import click_at, median_edge, screen_of

    chunk = view.chunk
    a, b = _targets(view, 2)
    x, y = screen_of(view, a)
    o, d, _metre = view._sight(x, y)
    hidden = a + 6 * median_edge(chunk) * d  # behind the model at its own pixel
    world = chunk.to_world(np.array([hidden, b]))
    view.set_free([{"id": "under", "kind": "point", "xyz": world[:1].tolist()},
                   {"id": "seen", "kind": "point", "xyz": world[1:].tolist()}], {})
    monkeypatch.setattr(view, "_surface_at", lambda *a, **k: None)  # nothing under the cursor
    view.start_drawing("point")
    click_at(view, x + 3, y + 4)
    assert view.drawn == []  # not the hidden point, and no surface: nothing
    x, y = screen_of(view, b)
    click_at(view, x + 3, y + 4)  # a vertex seen at its own pixel is still taken
    assert len(view.drawn) == 1
    assert np.allclose(view.drawn[0][1][0], chunk.to_internal(world[1:])[0], atol=1e-9, rtol=0)


def test_a_polygon_s_first_vertex_again_before_three(view, app):
    from free3d_helpers import click_at, click_vertex, screen_of

    a, b, c = _targets(view, 3)
    view.start_drawing("polygon")
    click_vertex(view, a)
    click_vertex(view, b)
    x, y = screen_of(view, a)
    click_at(view, x + 3, y + 4)  # snapped on the first vertex: nothing with two vertices
    assert len(view.drawing_vertices()) == 2 and view.drawn == []
    click_vertex(view, c)
    click_at(view, x + 3, y + 4)  # with three: closes the polygon
    assert len(view.drawn) == 1 and len(view.drawn[0][1]) == 3


def test_the_editor_follows_the_selected_drawing(view, app):
    from free3d_helpers import screen_of
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    chunk = view.chunk
    a, b, c = _targets(view, 3)
    world = chunk.to_world(np.array([a, b, c]))
    view.set_free([{"id": "f1", "kind": "line", "xyz": world.tolist()}], {})
    got = _record(view)
    _select_middle(view, a, b)
    ed = view.editor
    assert len(ed.handles) == 3
    x, y = screen_of(view, b)
    QTest.mouseClick(ed, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(x), int(y)))
    QTest.keyClick(ed, Qt.Key.Key_Delete)
    assert got["deleted"] == [("f1", 1)]
    # the window answers with the drawing without that vertex: new handles, none chosen
    view.set_free([{"id": "f1", "kind": "line", "xyz": world[[0, 2]].tolist()}], {})
    assert view.stack.currentWidget() is ed and len(ed.handles) == 2 and ed.current is None
    assert np.allclose([ed.handles[1].x(), ed.handles[1].y()], screen_of(view, c), atol=1e-6)
    QTest.keyClick(ed, Qt.Key.Key_Delete)
    assert got["deleted"][-1] == ("f1", -1)  # not a stale index
    # a drape answered: the new vertices get handles too
    view.a_free_drape.trigger()
    new = got["draped"][-1][1]
    view.set_free([{"id": "f1", "kind": "line", "xyz": chunk.to_world(new).tolist()}], {})
    assert len(ed.handles) == len(new) > 2
    # the same drawing given again: the editor stays as it is
    ed.current = 1
    view.set_free([{"id": "f1", "kind": "line", "xyz": chunk.to_world(new).tolist()}], {})
    assert ed.current == 1
    # gone (an undo of its creation): the editor closes; "" clears a selection
    view.set_free([], {})
    assert view.stack.currentWidget() is view.canvas and view.selected_free is None
    view.set_free([{"id": "f1", "kind": "line", "xyz": world.tolist()}], {}, selected="f1")
    assert view.selected_free == "f1"
    view.set_free([{"id": "f1", "kind": "line", "xyz": world.tolist()}], {}, selected="")
    assert view.selected_free is None and not view.a_free_drape.isEnabled()


def test_select_skips_hidden_drawings_and_takes_backspace(view, app):
    from free3d_helpers import click_at, median_edge, screen_of
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    chunk = view.chunk
    a, b = _targets(view, 2)
    x, y = screen_of(view, a)
    o, d, _metre = view._sight(x, y)
    hidden = a + 6 * median_edge(chunk) * d
    world = chunk.to_world(np.array([hidden, b]))
    view.set_free([{"id": "under", "kind": "point", "xyz": world[:1].tolist()},
                   {"id": "seen", "kind": "point", "xyz": world[1:].tolist()}], {})
    got = _record(view)
    view.start_free_select()
    click_at(view, x + 2, y + 2)
    assert got["selected"] == [] and view.selected_free is None  # behind the model: not taken
    x, y = screen_of(view, b)
    click_at(view, x + 2, y + 2)
    assert got["selected"] == ["seen"]
    QTest.keyClick(view.editor, Qt.Key.Key_Escape)  # back to the live view, still selecting
    assert view.stack.currentWidget() is view.canvas and view.free_select_on
    QTest.keyClick(view.canvas, Qt.Key.Key_Backspace)  # the Mac "delete" key
    assert got["deleted"] == [("seen", -1)]
    view.start_free_select()
    click_at(view, x + 2, y + 2)
    assert view.stack.currentWidget() is view.editor
    view.resize(700, 520)  # a resize: the frozen frame is of the old size, back to the live view
    app.processEvents()
    assert view.stack.currentWidget() is view.canvas
