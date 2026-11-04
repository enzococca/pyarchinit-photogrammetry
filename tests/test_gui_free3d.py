"""Free drawings in the 3D view: lines, polygons and points clicked on the model with the view live between the
clicks (a drag still rotates), the rubber band following the cursor, the snap to the vertices shown, Backspace,
Enter and Esc; the features given shown by the view and kept through a vertical exaggeration."""
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
    from free3d_helpers import click_vertex, distance_to_model, median_edge, screen_of
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

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
    QTest.mouseDClick(view.canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                      QPoint(int(round(x)), int(round(y))))
    assert len(view.drawn) == 2
    kind, xyz = view.drawn[1]
    assert kind == "polygon" and 5 <= len(xyz) <= 6
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
