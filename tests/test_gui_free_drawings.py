"""Free drawings in the window: drawn in the 3D view or with *Disegno libero* on the 2D view (z from the DEM on the
plan, from the relief on an elevation), shown in the layers "Disegni 3D: <layer>" on the plan and on the elevations
they lie on, edited in a form, hidden with their layer in 2D and 3D, one step of Modifica > Annulla per change, saved
with the project and refused on a raster to rebuild."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

PREFIX = "Disegni 3D: "


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(qapp, processed, tmp_path, monkeypatch):
    from orient_helpers import close_window, window

    w = window(processed, tmp_path, monkeypatch)
    qapp.processEvents()
    yield w
    close_window(w)


def _model_vertices(ch, n=3):
    """``n`` vertices of the model (internal) spread around its middle."""
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    v = np.asarray(read_mesh(ch.product_path("model")).xyz, float)
    c = v.mean(axis=0)
    targets = [c + 0.3 * np.array([np.cos(a), np.sin(a), 0.0]) for a in np.linspace(0, 2 * np.pi, n, endpoint=False)]
    return np.array([v[np.argmin(np.linalg.norm(v - t, axis=1))] for t in targets])


def _new(ch, before):
    from pyarchinit_photogrammetry.core import free_drawings

    out = [f for f in free_drawings.features(ch) if f["id"] not in before]
    assert len(out) == 1, out
    return out[0]


def _ids(ch):
    from pyarchinit_photogrammetry.core import free_drawings

    return {f["id"] for f in free_drawings.features(ch)}


def _layer_ids(view, name):
    return [a.get("ppg_id") for a in view.features(name)]


def test_3d_line_lands_in_the_chunk_and_on_the_plan(win, qapp):
    ch = win.chunk
    before = _ids(ch)
    internal = _model_vertices(ch)
    win._show_raster("dem")
    win.model_view.free_drawn.emit("line", internal)
    qapp.processEvents()
    rec = _new(ch, before)
    assert rec["kind"] == "line" and rec["layer"] == "Disegni 3D"
    assert np.allclose(rec["xyz"], ch.to_world(internal), atol=1e-9)
    assert win.isWindowModified()
    name = PREFIX + "Disegni 3D"
    assert name in win.ortho_view.layers
    ids = _layer_ids(win.ortho_view, name)
    assert rec["id"] in ids and len(ids) == len(before) + 1
    attrs = next(a for a in win.ortho_view.features(name) if a.get("ppg_id") == rec["id"])
    assert attrs["_free"] is True and attrs["name"] == rec["name"]
    assert np.allclose(np.asarray(attrs["_geom"][1], float), np.asarray(rec["xyz"])[:, :2], atol=1e-6)
    assert rec["id"] in {f[0] for f in win.model_view._free}  # the 3D view shows it too
    # a point and a polygon in another layer: the current layer of the tool
    win.set_free_layer("Muri")
    win.model_view.free_drawn.emit("point", internal[:1])
    qapp.processEvents()
    assert PREFIX + "Muri" in win.ortho_view.layers
    assert win.settings.value("draw/free_layer") == "Muri"


def test_2d_polygon_takes_z_from_the_dem(win, qapp):
    from orient_helpers import centre

    from pyarchinit_photogrammetry.archaeo.measures import ground_dem_at

    ch = win.chunk
    win._show_raster("dem")
    x, y, _z = centre(ch)
    xy = np.array([[x - 0.3, y - 0.2], [x + 0.4, y - 0.25], [x + 0.35, y + 0.3], [x - 0.2, y + 0.25]])
    before = _ids(ch)
    win.set_draw_mode("free_polygon")
    assert win.ortho_view.mode == "free_polygon"
    assert win.draw_actions["free"].isChecked()
    win.ortho_view.free_finished.emit("polygon", xy)
    qapp.processEvents()
    rec = _new(ch, before)
    assert rec["kind"] == "polygon"
    xyz = np.asarray(rec["xyz"])
    assert np.allclose(xyz[:, :2], xy, atol=1e-12)
    for (px, py, pz) in xyz:
        assert abs(pz - ground_dem_at(ch.dem_path, px, py)) < 1e-6
    # a vertex outside the DEM takes the height interpolated along the drawing from its neighbours
    line = np.array([[x - 0.3, y], [x + 1e4, y], [x + 0.3, y]])
    before = _ids(ch)
    win.ortho_view.free_finished.emit("line", line)
    rec = _new(ch, before)
    za, zb = ground_dem_at(ch.dem_path, *line[0]), ground_dem_at(ch.dem_path, *line[2])
    d1, d2 = np.hypot(*(line[1] - line[0])), np.hypot(*(line[2] - line[1]))
    assert abs(rec["xyz"][1][2] - (za + (zb - za) * d1 / (d1 + d2))) < 1e-6
    # no vertex on the DEM: refused, said
    win.said.clear()
    before = _ids(ch)
    win.ortho_view.free_finished.emit("point", np.array([[x + 1e4, y + 1e4]]))
    assert _ids(ch) == before
    assert any(k == "warn" and "DEM" in t for k, t in win.said)


def test_projection_on_an_elevation(win, qapp):
    from pyarchinit_photogrammetry.core import free_drawings
    from pyarchinit_photogrammetry.engine import elevation as E

    ch = win.chunk
    entry = next(e for e in ch.meta["elevations"] if e["id"] == "el000001")
    u0, v0, u1, v1 = entry["bounds"]
    uv = np.array([[u0 + 0.3, v0 + 0.5], [u0 + 1.0, v0 + 0.8], [u0 + 1.6, v0 + 0.6]])
    on = np.array([E.world_of(entry, u, v, 0.01) for u, v in uv])
    off = np.array([E.world_of(entry, u, v, 1.5) for u, v in uv])
    a = free_drawings.add(ch, "line", on.tolist())
    b = free_drawings.add(ch, "line", off.tolist())
    win._draw_free()
    win.show_elevation(entry)
    qapp.processEvents()
    name = PREFIX + "Disegni 3D"
    ids = _layer_ids(win.ortho_view, name)
    assert a["id"] in ids and b["id"] not in ids
    attrs = next(x for x in win.ortho_view.features(name) if x.get("ppg_id") == a["id"])
    assert np.allclose(np.asarray(attrs["_geom"][1], float), uv, atol=1e-6)
    # drawn on the elevation: on its relief, in the chunk's coordinates
    before = _ids(ch)
    win.ortho_view.free_finished.emit("line", uv)
    rec = _new(ch, before)
    want = np.array([E.world_of(entry, u, v, None, chunk=ch) for u, v in uv])
    assert np.allclose(rec["xyz"], want, atol=1e-9)
    assert rec["id"] in _layer_ids(win.ortho_view, name)
    # the plan again: both drawn there
    win._show_raster("dem")
    ids = _layer_ids(win.ortho_view, name)
    assert a["id"] in ids and b["id"] in ids


def test_form_edits_name_layer_colour_z(win, qapp, monkeypatch):
    from pyarchinit_photogrammetry.core import free_drawings
    from pyarchinit_photogrammetry.gui import free_drawing_dialog as fdd

    ch = win.chunk
    rec = next(f for f in free_drawings.features(ch) if f["id"] == "f1")
    dlg = fdd.FreeDrawingDialog(win, ch, rec)
    assert dlg.name_edit.text() == rec["name"]
    assert dlg.layer_combo.currentText() == "Disegni 3D"
    assert dlg.vertices.rowCount() == 3
    assert "-" not in dlg.measures.text().split()[0]  # a line: its 3D length shown
    dlg.name_edit.setText("Muro 1")
    dlg.layer_combo.setEditText("Muri")
    dlg.colour_edit.setText("#00ff00")
    dlg.vertices.item(0, 2).setText("123.25")
    dlg.add_attribute("materiale", "pietra")
    values = dlg.values()
    dlg.close()
    assert values["name"] == "Muro 1" and values["layer"] == "Muri" and values["colour"] == "#00ff00"
    assert values["attributes"] == {"materiale": "pietra"}
    assert values["xyz"][0][2] == 123.25 and values["xyz"][1] == rec["xyz"][1]
    asked = []
    monkeypatch.setattr(fdd.FreeDrawingDialog, "ask", classmethod(lambda cls, w, c, r: asked.append(r["id"]) or values))
    win._show_raster("dem")
    win.free_properties("f1")
    new = next(f for f in free_drawings.features(ch) if f["id"] == "f1")
    assert new["name"] == "Muro 1" and new["layer"] == "Muri" and new["colour"] == "#00ff00"
    assert new["xyz"][0][2] == 123.25 and new["attributes"] == {"materiale": "pietra"}
    assert "f1" in _layer_ids(win.ortho_view, PREFIX + "Muri")
    assert "f1" not in _layer_ids(win.ortho_view, PREFIX + "Disegni 3D")
    # a double click on the feature in 2D opens the form too, the 3D's Properties as well
    attrs = next(a for a in win.ortho_view.features(PREFIX + "Muri") if a.get("ppg_id") == "f1")
    win.ortho_view.feature_double_clicked.emit(PREFIX + "Muri", attrs)
    win.model_view.free_selected.emit("f1")
    win.a_free_props.trigger()
    assert asked == ["f1", "f1", "f1"]


def test_measure_area_of_a_selected_free_polygon(win, qapp):
    from orient_helpers import centre

    ch = win.chunk
    win._show_raster("dem")
    x, y, _z = centre(ch)
    xy = np.array([[x - 0.3, y - 0.2], [x + 0.4, y - 0.25], [x + 0.35, y + 0.3]])
    win.ortho_view.free_finished.emit("polygon", xy)
    name = PREFIX + "Disegni 3D"
    item = next(it for it, _l, a in win.ortho_view.layers[name]["features"] if a.get("_geom", ("",))[0] == "Polygon")
    win.ortho_view.select_items([item])
    win.said.clear()
    win._area_selected()
    assert any(k == "info" and "m²" in t for k, t in win.said), win.said


def test_layer_visibility_hides_2d_and_3d(win, qapp):
    from PySide6.QtCore import Qt

    from pyarchinit_photogrammetry.core import free_drawings

    ch = win.chunk
    win._show_raster("dem")
    win._draw_free()
    name = PREFIX + "Disegni 3D"
    assert "f1" in {f[0] for f in win.model_view._free}
    tree = win.layers_pane.tree
    top = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
               if tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)[0] == name)
    top.setCheckState(0, Qt.CheckState.Unchecked)
    qapp.processEvents()
    assert not next(x for x in free_drawings.layers(ch) if x["name"] == "Disegni 3D")["visible"]
    assert not win.ortho_view.layers[name]["visible"]
    assert "f1" not in {f[0] for f in win.model_view._free}
    win._show_raster("ortho")  # another raster: still hidden
    assert not win.ortho_view.layers[name]["visible"]
    tree = win.layers_pane.tree
    top = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
               if tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)[0] == name)
    top.setCheckState(0, Qt.CheckState.Checked)
    qapp.processEvents()
    assert next(x for x in free_drawings.layers(ch) if x["name"] == "Disegni 3D")["visible"]
    assert win.ortho_view.layers[name]["visible"]
    assert "f1" in {f[0] for f in win.model_view._free}


def test_undo_redo_of_add_move_delete(win, qapp):
    from pyarchinit_photogrammetry.core import free_drawings

    ch = win.chunk
    win._show_raster("dem")
    win.history.rebase()
    internal = _model_vertices(ch, 3)
    before = _ids(ch)
    win.model_view.free_drawn.emit("line", internal)
    rec = _new(ch, before)
    fid = rec["id"]
    moved = internal[1] + np.array([0.01, 0.0, 0.0])
    win.model_view.free_vertex_moved.emit(fid, 1, moved)
    after_move = next(f for f in free_drawings.features(ch) if f["id"] == fid)
    assert np.allclose(after_move["xyz"][1], ch.to_world(moved[None])[0], atol=1e-9)
    win.model_view.free_delete_requested.emit(fid, 0)  # a vertex: two left
    assert len(next(f for f in free_drawings.features(ch) if f["id"] == fid)["xyz"]) == 2
    win.model_view.free_delete_requested.emit(fid, 0)  # a line keeps two: refused
    assert len(next(f for f in free_drawings.features(ch) if f["id"] == fid)["xyz"]) == 2
    assert "elimina il disegno intero" in win.statusBar().currentMessage()
    win.model_view.free_delete_requested.emit(fid, -1)
    assert fid not in _ids(ch)
    assert fid not in _layer_ids(win.ortho_view, PREFIX + "Disegni 3D")

    def xyz():
        f = next((f for f in free_drawings.features(ch) if f["id"] == fid), None)
        return None if f is None else np.asarray(f["xyz"])

    assert win.history.undo() is not None  # the delete
    assert xyz() is not None and len(xyz()) == 2
    assert fid in _layer_ids(win.ortho_view, PREFIX + "Disegni 3D")
    assert fid in {f[0] for f in win.model_view._free}
    win.history.undo()  # the vertex deleted
    assert len(xyz()) == 3 and np.allclose(xyz()[1], after_move["xyz"][1], atol=1e-9)
    win.history.undo()  # the move
    assert np.allclose(xyz(), rec["xyz"], atol=1e-9)
    win.history.undo()  # the add
    assert xyz() is None
    assert fid not in _layer_ids(win.ortho_view, PREFIX + "Disegni 3D")
    for _ in range(3):
        win.history.redo()
    assert len(xyz()) == 2
    win.history.redo()
    assert xyz() is None


def test_free_drawings_survive_save_and_reopen(win, qapp, tmp_path):
    from orient_helpers import centre

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core import free_drawings
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    ch = win.chunk
    win._show_raster("dem")
    win.set_free_layer("Muri")
    free_drawings.set_layer(ch, "Muri", colour="#123456")
    x, y, _z = centre(ch)
    win.ortho_view.free_finished.emit("polygon", np.array([[x, y], [x + 0.3, y], [x + 0.3, y + 0.3]]))
    win.model_view.free_drawn.emit("line", _model_vertices(ch, 2))
    feats, layers = free_drawings.features(ch), free_drawings.layers(ch)
    assert win.save_document()
    doc2 = ppg.Document.open(win.doc.path)
    w2 = MainWindow(doc2, autoconnect=False)
    w2.runner.show_dialog = False
    try:
        qapp.processEvents()
        w2._show_raster("dem")
        ch2 = w2.chunk
        assert free_drawings.features(ch2) == feats
        assert free_drawings.layers(ch2) == layers
        for lay in layers:
            name = PREFIX + lay["name"]
            mine, theirs = win.ortho_view.layers.get(name), w2.ortho_view.layers.get(name)
            assert (mine is None) == (theirs is None)
            if mine is None:
                continue
            assert [a["ppg_id"] for _i, _l, a in mine["features"]] == [a["ppg_id"] for _i, _l, a in theirs["features"]]
            colours = [it.pen().color().name() for it, _l, _a in theirs["features"] if hasattr(it, "pen")]
            assert colours == [it.pen().color().name() for it, _l, _a in mine["features"] if hasattr(it, "pen")]
        muri = [it for it, _l, _a in w2.ortho_view.layers[PREFIX + "Muri"]["features"]]
        assert all(it.pen().color().name() == "#123456" for it in muri if hasattr(it, "pen"))
        assert {f[0] for f in w2.model_view._free} == {f["id"] for f in feats}
    finally:
        w2.photos.stop()
        w2.setWindowModified(False)
        w2.close()


def test_free_tools_refused_on_a_stale_raster(win, qapp):
    ch = win.chunk
    xyz = np.asarray(ch.point_cloud.xyz, float)
    c = np.median(xyz, axis=0)
    _, _, vt = np.linalg.svd(xyz[::50] - c, full_matrices=False)
    span = np.ptp((xyz - c) @ vt[:2].T, axis=0)
    targets = [c - 0.3 * span[0] * vt[0], c + 0.3 * span[0] * vt[0], c + 0.3 * span[1] * vt[1]]
    points = ch.to_world(np.array([xyz[np.argmin(np.linalg.norm(xyz - t, axis=1))] for t in targets]))
    win.orient_chunk(ch, points=points)
    qapp.processEvents()
    win._show_raster("dem")
    assert win._shown_raster_stale()
    for mode in ("free_line", "free_polygon", "free_point"):
        win.said.clear()
        win.set_draw_mode(mode)
        assert win.ortho_view.mode == "pan"
        assert win.draw_actions["pan"].isChecked()
        assert any(k == "warn" and "va rifatto prima di disegnarci sopra" in t for k, t in win.said)
    win.said.clear()
    win.draw_actions["free"].trigger()
    assert win.ortho_view.mode == "pan"
    assert win.said


def _real_double_click(view, x: float, y: float) -> None:
    """A double click as the mouse sends it: Press, Release, Press, DblClick, Release, each to the widget of the 3D
    view under the mouse at that moment (the first click may open the vertex editor in place of the live view)."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    pos = QPointF(float(round(x)), float(round(y)))
    for kind, buttons in ((QEvent.Type.MouseButtonPress, left), (QEvent.Type.MouseButtonRelease, none),
                          (QEvent.Type.MouseButtonPress, left), (QEvent.Type.MouseButtonDblClick, left),
                          (QEvent.Type.MouseButtonRelease, none)):
        target = view.editor if view.stack.currentWidget() is view.editor else view._draw_target()
        ev = QMouseEvent(kind, pos, target.mapToGlobal(pos), left, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(target, ev)


def test_3d_double_click_opens_the_form_only_on_the_selected_drawing(win, qapp, monkeypatch):
    from free3d_helpers import screen_of

    from pyarchinit_photogrammetry.gui import free_drawing_dialog as fdd

    ch = win.chunk
    mv = win.model_view
    win.show_view(mv)
    mv.resize(640, 480)
    mv.set_chunk(ch, "model")
    win._draw_free_3d()
    mv.canvas.top_view()
    qapp.processEvents()
    asked = []
    monkeypatch.setattr(fdd.FreeDrawingDialog, "ask", classmethod(lambda cls, w, c, r: asked.append(r["id"])))
    internal = _model_vertices(ch, 2)
    before = _ids(ch)
    mv.free_drawn.emit("line", internal)
    fid = _new(ch, before)["id"]
    qapp.processEvents()
    a, b = (screen_of(mv, p) for p in internal)
    middle = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    mv.start_free_select()
    qapp.processEvents()
    _real_double_click(mv, *middle)
    qapp.processEvents()
    assert mv.selected_free == fid
    assert asked == [fid]
    # a double click on empty space: the selection goes, no form
    w, h = mv.canvas.width(), mv.canvas.height()
    _real_double_click(mv, 3, 3)
    qapp.processEvents()
    assert asked == [fid]
    assert mv.selected_free is None
    # the double click that ends a new 3D line: no form, though a drawing was selected before
    _real_double_click(mv, *middle)
    qapp.processEvents()
    assert mv.selected_free == fid and asked == [fid, fid]
    mv.start_drawing("line")
    before = _ids(ch)
    from free3d_helpers import click_at

    p, q = (0.4 * w, 0.45 * h), (0.6 * w, 0.55 * h)  # the middle of the trench seen from above: on the model
    click_at(mv, *p)
    assert len(mv.drawing_vertices()) == 1
    _real_double_click(mv, *q)
    qapp.processEvents()
    assert len(_ids(ch)) == len(before) + 1  # the line ended
    assert asked == [fid, fid]
    mv.stop_drawing()


def test_name_in_the_layer_pane_and_colour_back_to_the_layer(win, qapp):
    from pyarchinit_photogrammetry.core import free_drawings
    from pyarchinit_photogrammetry.gui import free_drawing_dialog as fdd

    ch = win.chunk
    win._show_raster("dem")
    name = PREFIX + "Disegni 3D"
    idx = _layer_ids(win.ortho_view, name).index("f1")
    win.layers_pane.attribute_edited.emit(name, idx, "name", "Soglia")
    qapp.processEvents()
    assert next(f for f in free_drawings.features(ch) if f["id"] == "f1")["name"] == "Soglia"
    free_drawings.update(ch, "f1", colour="#ff0000")
    rec = next(f for f in free_drawings.features(ch) if f["id"] == "f1")
    dlg = fdd.FreeDrawingDialog(win, ch, rec)
    dlg.colour_edit.setText("")
    values = dlg.values()
    dlg.close()
    assert values["colour"] == ""
    import pytest as _pt

    mp = _pt.MonkeyPatch()
    try:
        mp.setattr(fdd.FreeDrawingDialog, "ask", classmethod(lambda cls, w, c, r: values))
        assert win.free_properties("f1")
    finally:
        mp.undo()
    rec = next(f for f in free_drawings.features(ch) if f["id"] == "f1")
    assert "colour" not in rec and free_drawings.colour_of(ch, rec) == free_drawings.DEFAULT_COLOUR
