"""Helpers of the tests of the free drawings in the 3D view: a click on the projection of a point, the size of the
model's triangles and the distance of points from the model's surface."""
from __future__ import annotations

import numpy as np


def screen_of(view, internal_xyz) -> tuple[float, float]:
    """The widget pixel where the internal point ``internal_xyz`` is shown now."""
    sd, chunk = view.sd, view.chunk
    shown = sd.to_display(chunk.to_world(np.asarray(internal_xyz, np.float64).reshape(1, 3)) - sd.offset)
    _image, project = view.canvas.snapshot()
    x, y, _depth, ok = project(shown)
    assert ok[0], "the point is not in the view"
    return float(x[0]), float(y[0])


def click_at(view, x: float, y: float) -> None:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    QTest.mouseClick(view.canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(int(round(x)), int(round(y))))


def click_vertex(view, internal_xyz) -> tuple[float, float]:
    """A left click on the pixel where ``internal_xyz`` is shown; returns the pixel."""
    x, y = screen_of(view, internal_xyz)
    click_at(view, x, y)
    return x, y


def double_click_at(view, x: float, y: float) -> None:
    """A double click as a real mouse sends it: Press, Release, Press, DblClick, Release (``QTest.mouseDClick`` sends
    the DblClick alone)."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    pos = QPointF(float(round(x)), float(round(y)))
    glob = view.canvas.mapToGlobal(pos)
    for kind, buttons in ((QEvent.Type.MouseButtonPress, left), (QEvent.Type.MouseButtonRelease, none),
                          (QEvent.Type.MouseButtonPress, left), (QEvent.Type.MouseButtonDblClick, left),
                          (QEvent.Type.MouseButtonRelease, none)):
        ev = QMouseEvent(kind, pos, glob, left, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view.canvas, ev)


def median_edge(chunk) -> float:
    """The median length of the edges of the model (internal units)."""
    m = chunk.model
    v, f = np.asarray(m.vertices, np.float64), np.asarray(m.faces, np.int64)
    e = np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    return float(np.median(np.linalg.norm(v[e[:, 0]] - v[e[:, 1]], axis=1)))


def distance_to_model(chunk, xyz) -> np.ndarray:
    """The distance of each internal point of ``xyz`` from the surface of the model."""
    import trimesh

    m = chunk.model
    _closest, dist, _tri = trimesh.proximity.closest_point(m, np.asarray(xyz, np.float64).reshape(-1, 3))
    return np.asarray(dist)
