"""The units drawn edited in the 2D view - their vertices moved, added and removed, their outline
reshaped with a line as in QGIS - with their 3D proxy built again by itself; and the units drawn with a
number but without their US form brought into the Extended Matrix through PyArchInit."""
from __future__ import annotations

import numpy as np
import pytest
from conftest import TRENCH_ORIGIN
from shapely.geometry import LineString, Polygon, box

from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon, reshape_polygon

X0, Y0 = TRENCH_ORIGIN


# ---------------------------------------------------------------- reshape (QGIS's Reshape Features)
def test_reshape_adds_what_the_line_encloses_outside():
    sq = box(0, 0, 1, 1)
    out = reshape_polygon(sq, [(0.8, 0.3), (1.5, 0.3), (1.5, 0.7), (0.8, 0.7)])
    assert out.area == pytest.approx(1.2) and out.bounds == (0.0, 0.0, 1.5, 1.0)
    corner = reshape_polygon(sq, [(1.0, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5), (0.5, 1.0)])  # from the outline
    assert corner.area == pytest.approx(1.75)


def test_reshape_cuts_away_the_smaller_part():
    sq = box(0, 0, 1, 1)
    cut = reshape_polygon(sq, LineString([(0.7, -0.2), (0.7, 1.2)]))
    assert cut.area == pytest.approx(0.7) and cut.bounds == (0.0, 0.0, 0.7, 1.0)
    notch = reshape_polygon(sq, [(0.4, 1.2), (0.5, 0.6), (0.6, 1.2)])
    assert notch.is_valid and notch.area == pytest.approx(1 - 0.5 * (0.4 / 3) * 0.4)


def test_reshape_keeps_holes_and_needs_two_crossings():
    holed = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)], [[(1, 1), (2, 1), (2, 2), (1, 2)]])
    r = reshape_polygon(holed, [(3.5, -1), (3.5, 5)])
    assert r.area == pytest.approx(13.0) and len(r.interiors) == 1
    assert reshape_polygon(box(0, 0, 1, 1), [(0.2, 0.2), (0.4, 0.4)]) is None  # inside, never crossing
    assert reshape_polygon(box(0, 0, 1, 1), [(0.5, 0.5), (2, 0.5)]) is None  # crossing once


# ---------------------------------------------------------------- the window
def _log(w):
    t = w.log_pane.table
    return [t.item(r, 2).text() for r in range(t.rowCount())]


def _six(w):
    w._add_unit(UnitPolygon(box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2), us="6", area="1", sito="Saggio"))
    return w.units[-1]


def _proxy(w, app, mw, monkeypatch):
    answers = {"units": "drawn", "base": "plane", "z": 99.0, "thickness": 0.1, "dem": ""}
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, parent, title, fields, intro="": dict(answers)))
    w.create_proxies()
    w.runner.wait()
    app.processEvents()
    (e,) = w.chunk.meta["proxies"]
    return e


def _part(w):
    return w.ortho_view.layers["Unita' stratigrafiche"]["features"][0][0]


def test_vertices_moved_the_proxy_follows_and_undo_brings_both_back(trench, monkeypatch):
    w, app, mw = trench
    u = _six(w)
    area = _proxy(w, app, mw, monkeypatch)["area_m2"]
    w.set_draw_mode("vertices")
    v = w.ortho_view
    assert v.begin_vertex_edit(_part(w)) and v.editing_vertices()
    corner = v._vedit["xy"].index([X0 + 2, Y0 + 2])
    v.move_vertex(corner, X0 + 3, Y0 + 2)  # a trapezoid of 1.5 m2
    v.end_vertex_edit(commit=True)
    assert not v.editing_vertices()
    assert u.geometry.area == pytest.approx(1.5) and u.geometry.bounds == (X0 + 1, Y0 + 1, X0 + 3, Y0 + 2)
    assert w.history.undo_text() == "Vertici di US 6" and "Vertici di US 6: area 1,500 m2" in _log(w)
    w.runner.wait()
    app.processEvents()
    (e,) = w.chunk.meta["proxies"]
    assert e["area_m2"] == pytest.approx(1.5 * area, rel=0.05)
    assert any(t.startswith("Proxy US6 ricostruito sul nuovo poligono") for t in _log(w))

    w.undo()  # the outline back, and its proxy with it
    w.runner.wait()
    app.processEvents()
    assert w.units[0].geometry.equals(box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2))
    (e,) = w.chunk.meta["proxies"]
    assert e["area_m2"] == pytest.approx(area, rel=0.01)

    # Esc puts the outline back; an outline crossing itself is refused
    entries = len(w.history.entries)
    assert v.begin_vertex_edit(_part(w))
    v.move_vertex(0, X0 + 5, Y0 + 5)
    v.end_vertex_edit(commit=False)
    assert w.units[0].geometry.equals(box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2)) and len(w.history.entries) == entries
    assert v.begin_vertex_edit(_part(w))
    xy = v._vedit["xy"]
    crossing = [list(p) for p in xy]
    crossing[xy.index([X0 + 2, Y0 + 2])] = [X0 + 1, Y0 + 0.5]
    assert not Polygon(crossing).is_valid
    v.move_vertex(xy.index([X0 + 2, Y0 + 2]), X0 + 1, Y0 + 0.5)
    v.end_vertex_edit(commit=True)
    assert w.units[0].geometry.equals(box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2)) and len(w.history.entries) == entries
    assert "si incrocia" in w.statusBar().currentMessage()


def test_vertices_with_the_mouse(trench):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    w, app, mw = trench
    u = _six(w)
    w.set_draw_mode("vertices")
    v = w.ortho_view
    vp = v.viewport()

    def at(x, y):
        return v.mapFromScene(v.scene_pt(x, y))

    QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(X0 + 1.5, Y0 + 1.5))
    assert v.editing_vertices()
    # the middle of the eastern side dragged out: a new vertex
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(X0 + 2, Y0 + 1.5))
    QTest.mouseMove(vp, at(X0 + 2.5, Y0 + 1.5))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(X0 + 2.5, Y0 + 1.5))
    assert len(v._vedit["xy"]) == 5
    # a corner removed with Alt+click, then Enter keeps the outline
    QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier, at(X0 + 1, Y0 + 1))
    assert len(v._vedit["xy"]) == 4
    QTest.keyClick(v, Qt.Key.Key_Return)
    assert not v.editing_vertices()
    # (2,1) (2.5,1.5) (2,2) (1,2): the square less the triangle of its corner, plus the triangle out east
    assert u.geometry.area == pytest.approx(1.0 - 0.5 + 0.25, abs=0.01)
    assert len(u.geometry.exterior.coords) == 5


def test_reshape_in_the_window(trench):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    w, app, mw = trench
    u = _six(w)
    w.set_draw_mode("reshape")
    v = w.ortho_view
    vp = v.viewport()
    for x, y in ((1.8, 1.3), (2.6, 1.3), (2.6, 1.7), (1.8, 1.7)):  # out of the eastern side and back
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         v.mapFromScene(v.scene_pt(X0 + x, Y0 + y)))
    QTest.keyClick(v, Qt.Key.Key_Return)
    assert u.geometry.area == pytest.approx(1.0 + 0.6 * 0.4, rel=0.02)
    assert w.history.undo_text() == "Rimodellata US 6" and v.mode == "reshape"
    before = u.geometry
    w._unit_reshaped(np.array([[X0 + 4, Y0 + 4], [X0 + 5, Y0 + 5]]))  # far from every unit
    assert u.geometry.equals(before) and "attraversare due volte" in w.statusBar().currentMessage()


# ---------------------------------------------------------------- into the Extended Matrix
def test_a_unit_drawn_with_its_number_reaches_the_matrix_through_pyarchinit(pa_db, trench, monkeypatch):
    """US 2 has its form in PyArchInit; US 7, drawn inside it with its number, has none: the window
    says so, Proponi US e rapporti creates its form (with its node_uuid), the cut it fills, and the
    relations with US 2 too, whose form is otherwise left as it was."""
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit
    from pyarchinit_photogrammetry.pyarchinit.bridge import parse_relationships

    w, app, mw = trench
    w.pa = PyArchInit.connect()
    w._add_unit(UnitPolygon(box(X0 + 1, Y0 + 1, X0 + 4, Y0 + 4), us="2", area="1", sito="Saggio"))
    assert not any("non è ancora in PyArchInit" in t for t in _log(w))
    w._add_unit(UnitPolygon(box(X0 + 2, Y0 + 2, X0 + 3, Y0 + 3), us="7", area="1", sito="Saggio"))
    assert any(t.startswith("US 7: la scheda US non è ancora in PyArchInit") for t in _log(w))
    assert w._without_us_form("Saggio") == [1]

    asked = []
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(
        lambda cls, parent, title, fields, intro="": asked.append(intro) or
        {"first": 8, "area": "1", "step": 2.0, "cuts": True}))
    told = []
    monkeypatch.setattr(w, "_info", lambda title, text: told.append(text))
    w.propose_units()
    w.runner.wait()
    app.processEvents()
    assert "1 unita' disegnate con il loro numero non hanno ancora la scheda" in asked[0]
    d = w._proposal_win
    rows = [(d.units.item(r, 1).text(), d.units.item(r, 2).text()) for r in range(d.units.rowCount())]
    assert rows == [("7", "US"), ("8", "US")]  # US 7 and the cut it fills; US 2 only for the relations
    rels = {(d.relations.item(r, 1).text(), d.relations.item(r, 2).text(), d.relations.item(r, 3).text())
            for r in range(d.relations.rowCount())}
    assert rels == {("US 7", "Riempie", "US 8"), ("US 8", "Taglia", "US 2")}
    assert "1 US con la scheda gia' in PyArchInit" in d.status.text()
    d._apply(True)
    w.runner.wait()
    app.processEvents()
    seven, cut, two = (w.pa.us_record("Saggio", "1", n) for n in ("7", "8", "2"))
    assert seven is not None and seven["node_uuid"] and cut is not None
    assert ["Riempie", "8"] in [r[:2] for r in parse_relationships(seven["rapporti"])]
    assert ["Tagliato da", "8"] in [r[:2] for r in parse_relationships(two["rapporti"])]
    assert two["d_stratigrafica"] == "Strato 2"  # its form otherwise as it was
    assert "Esporta sito in em.json" in told[-1] and "2 schede US create" in told[-1]
    assert w._without_us_form("Saggio") == []
