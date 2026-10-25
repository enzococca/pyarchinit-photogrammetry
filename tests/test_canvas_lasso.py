"""Freehand selection changes the selection, never the unit geometry or drawing tools."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(qapp):
    from PySide6.QtCore import QCoreApplication, QEvent
    from pyarchinit_photogrammetry.gui.image_views import RasterView

    view = RasterView()
    view.detail = False
    view.resize(500, 500)
    view.show()
    view.show_frame((0, 0, 10, 10), 0.02)
    qapp.processEvents()
    yield view
    view.close()
    view.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def polygon(view, bounds, number, *, layer="Unità topografiche"):
    x0, y0, x1, y1 = bounds
    return view.add_polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], label=f"UT {number}",
                            layer=layer, attrs={"nr_ut": number})


def scene_polygon(view, points):
    return [view.scene_pt(x, y) for x, y in points]


def numbers(view):
    return {attrs["nr_ut"] for _, _, attrs in view.selected_features()}


def stroke(view, points, *, modifiers=None):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    modifiers = Qt.KeyboardModifier.NoModifier if modifiers is None else modifiers
    positions = [view.mapFromScene(p) for p in scene_polygon(view, points)]
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, modifiers, positions[0])
    for position in positions[1:]:
        QTest.mouseMove(view.viewport(), position)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, modifiers, positions[-1])


def test_polygon_selection_hits_intersections_and_containers_but_not_hidden_features(view):
    polygon(view, (1, 1, 2, 2), 1)
    polygon(view, (3, 1, 5, 3), 2)
    polygon(view, (0.5, 0.5, 6, 4), 3)
    polygon(view, (1, 1, 2, 2), 4, layer="Nascoste")
    polygon(view, (8, 8, 9, 9), 5)
    view.set_layer_visible("Nascoste", False)
    # Lasso encloses 1, intersects 2, and lies entirely inside 3.
    points = scene_polygon(view, [(0.8, 0.8), (3.5, 0.8), (3.5, 2.5), (0.8, 2.5)])
    view.select_in_polygon(points)
    assert numbers(view) == {1, 2, 3}
    assert len(view.selection) == 3  # labels/highlights are not selected a second time


def test_freehand_gesture_is_not_a_box_and_never_creates_a_unit(view):
    polygon(view, (1, 1, 2, 2), 1)
    polygon(view, (4, 4, 5, 5), 2)
    polygon(view, (4, 1, 5, 2), 3)
    finished = []
    changed = []
    view.polygon_finished.connect(finished.append)
    view.selection_changed.connect(changed.append)
    view.selection_shape = "freehand"
    view.set_mode("select")
    # An L-shaped stroke includes 1 and 3, excludes 2 despite its bounding box.
    stroke(view, [(0.5, 0.5), (5.5, 0.5), (5.5, 2.5), (2.5, 2.5), (2.5, 5.5), (0.5, 5.5), (0.5, 0.5)])
    assert numbers(view) == {1, 3}
    assert changed[-1] == 2 and not finished
    assert view._lasso_item is None and not view._lasso_points
    assert len(view.layers["Unità topografiche"]["features"]) == 3


def test_freehand_add_remove_and_click_keep_existing_modifier_behavior(view):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    first = polygon(view, (1, 1, 2, 2), 1)
    polygon(view, (4, 1, 5, 2), 2)
    view.select_items([first])
    view.selection_shape = "freehand"
    view.set_mode("select")
    loop = [(3.5, 0.5), (5.5, 0.5), (5.5, 2.5), (3.5, 2.5), (3.5, 0.5)]
    stroke(view, loop, modifiers=Qt.KeyboardModifier.ShiftModifier)
    assert numbers(view) == {1, 2}
    stroke(view, loop, modifiers=Qt.KeyboardModifier.AltModifier)
    assert numbers(view) == {1}
    position = view.mapFromScene(view.scene_pt(4.5, 1.5))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.MetaModifier, position)
    assert numbers(view) == {1, 2}
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.MetaModifier, position)
    assert numbers(view) == {1}


@pytest.mark.parametrize("cancel", ["escape", "right"])
def test_cancelling_a_stroke_preserves_the_previous_selection(view, cancel):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    first = polygon(view, (1, 1, 2, 2), 1)
    view.select_items([first])
    view.selection_shape = "freehand"
    view.set_mode("select")
    start, middle = [view.mapFromScene(view.scene_pt(x, y)) for x, y in [(3, 3), (6, 6)]]
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(view.viewport(), middle)
    if cancel == "escape":
        QTest.keyClick(view, Qt.Key.Key_Escape)
    else:
        QTest.mousePress(view.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, middle)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, middle)
    assert numbers(view) == {1}
    assert view._lasso_item is None and not view._lasso_points


def test_default_rectangle_and_lasso_reset_on_raster_change(view):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    polygon(view, (1, 1, 2, 2), 1)
    polygon(view, (4, 4, 5, 5), 2)
    view.set_mode("select")
    assert view.selection_shape == "rectangle"
    stroke(view, [(0.5, 0.5), (2.5, 2.5)])
    assert numbers(view) == {1}
    view.selection_shape = "freehand"
    position = view.mapFromScene(view.scene_pt(3, 3))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position)
    assert view._lasso_item is not None
    view.show_frame((0, 0, 5, 5), 0.02)
    assert view._lasso_item is None and not view._lasso_points
    view.set_mode("pan")  # no access to the scene item deleted during the reload
