"""Only the layers ticked in the Layers pane go to QGIS: the rows of a layer hidden leave the live GeoPackage without
being read back as deleted there, its drawings and the edits made on it by hand stay in the program, and ticking it
again puts its rows back as they were - with the identities they had. What QGIS saved on a layer a moment before it
was hidden is still read."""
from __future__ import annotations

from PySide6.QtCore import Qt
from shapely.geometry import LineString, box

from pyarchinit_photogrammetry.core import free_drawings as fd
from pyarchinit_photogrammetry.io.qgis_link import ID_FIELD, LIVE_DB, LiveLink, plan_identities
from test_free_two_way import LINES, POLYGONS, POINTS, _by_id, _chunk, _three
from test_plan_two_way import LAYER, STONES, Plugin, _by_n, _line, _live
from test_qgis_two_way import Qgis, X0, Y0, _from_qgis, _rows, _square

CRS = "EPSG:3004"
OTHER = "PhotoCAD: Ortofoto 2"
UNITS = "Unita' stratigrafiche"


def _layers(sent=True, other=True, units=True):
    unit = {"us_s": "6", "area_s": "1", "ppg_id": "u6", "_geom": _square(X0, Y0)}
    return {LAYER: {"kind": "line", "sent": sent,
                    "features": [{"_geom": ("LineString", _line(i)), "n": i} for i in range(4)]},
            OTHER: {"kind": "line", "sent": other,
                    "features": [{"_geom": ("LineString", _line(i + 10)), "n": i + 10} for i in range(2)]},
            UNITS: {"kind": "polygon", "sent": units, "features": [unit]}}


def _of(path, table, layer):
    return [r for r in _rows(path, table) if r["layer"] == layer]


# ---------------------------------------------------------------- the GeoPackage
def test_only_the_layers_sent_are_written(tmp_path):
    link = LiveLink(tmp_path, CRS)
    link.write(_layers(sent=False))
    assert _of(link.path, "linee", LAYER) == [] and len(_of(link.path, "linee", OTHER)) == 2
    assert len(_rows(link.path, "unita")) == 1
    assert link.read_changes() == {}
    link.write(_layers(units=False))  # the same features, another choice: written again
    assert len(_of(link.path, "linee", LAYER)) == 4 and _rows(link.path, "unita") == []
    assert link.read_changes() == {}
    link.close()


def test_a_layer_no_longer_sent_leaves_the_file_and_is_not_read_as_deleted(tmp_path):
    link = LiveLink(tmp_path, CRS)
    link.write(_layers())
    first = _by_n(_of(link.path, "linee", LAYER))
    other = _by_n(_of(link.path, "linee", OTHER))
    assert len(first) == 4 and link.read_changes() == {}

    link.write(_layers(sent=False, units=False))
    assert _of(link.path, "linee", LAYER) == [] and _rows(link.path, "unita") == []
    assert {n: r["fid"] for n, r in _by_n(_of(link.path, "linee", OTHER)).items()} == \
        {n: r["fid"] for n, r in other.items()}  # the others in place
    assert link.read_changes() == {}  # nothing deleted there
    link.write(_layers(sent=False, units=False))
    assert link.read_changes() == {}

    again = LiveLink(tmp_path, CRS)  # the program started again: the record of the file says the same
    again.write(_layers(sent=False, units=False))
    assert again.read_changes() == {}
    again.close()

    link.write(_layers())  # sent again: the same identities
    back = _by_n(_of(link.path, "linee", LAYER))
    assert {n: r[ID_FIELD] for n, r in back.items()} == {n: r[ID_FIELD] for n, r in first.items()}
    assert [r[ID_FIELD] for r in _rows(link.path, "unita")] == ["u6"]
    assert link.read_changes() == {}
    link.close()


def test_the_identities_do_not_depend_on_what_is_sent(tmp_path):
    assert set(plan_identities(_layers(sent=False))) == set(plan_identities(_layers()))
    twice = {LAYER: {"kind": "line", "sent": False, "features": [{"_geom": ("LineString", _line(0))}]},
             OTHER: {"kind": "line", "features": [{"_geom": ("LineString", _line(0))}] * 2}}
    link = LiveLink(tmp_path, CRS)
    link.write(twice)
    assert {r[ID_FIELD] for r in _rows(link.path, "linee")} == \
        {pid for pid, (_t, name, _a) in plan_identities(twice).items() if name == OTHER}
    link.close()


def test_what_qgis_saved_on_a_layer_before_it_was_held_is_still_read(tmp_path):
    link = LiveLink(tmp_path, CRS)
    link.write(_layers())
    rows = _by_n(_of(link.path, "linee", LAYER))
    unit = _rows(link.path, "unita")[0]
    qgis = Qgis(link.path)
    moved = LineString([(X0 + 1, Y0 + 1.02), (X0 + 2, Y0 + 2), (X0 + 3, Y0 + 1.02)])
    grown = box(X0, Y0, X0 + 0.75, Y0 + 0.5)
    qgis.save(qgis.move("linee", rows[2]["fid"], moved), qgis.delete("linee", rows[0]["fid"]),
              qgis.move("unita", unit["fid"], grown))

    link.write(_layers(sent=False, units=False))  # hidden a moment later: never over what nobody read
    left = _of(link.path, "linee", LAYER)
    assert [r["fid"] for r in left] == [rows[2]["fid"]] and left[0]["geom"].equals(moved)
    assert _rows(link.path, "unita")[0]["geom"].equals(grown)
    got = link.read_changes()
    assert set(got) == {"linee", "unita"}
    assert [f.id for f in got["linee"].changed] == [rows[2][ID_FIELD]]
    assert got["linee"].deleted == [(rows[0][ID_FIELD], 1)] and got["linee"].added == []
    assert [f.id for f in got["unita"].changed] == ["u6"] and got["unita"].deleted == []
    assert link.read_changes() == {}

    link.write(_layers(sent=False, units=False))  # read: the rows of the layers held leave
    assert _of(link.path, "linee", LAYER) == [] and _rows(link.path, "unita") == []
    assert link.read_changes() == {}
    qgis.close()
    link.close()


def test_the_free_drawings_of_a_layer_hidden_are_not_written(tmp_path):
    _doc, ch = _chunk(tmp_path)
    line, poly, point = _three(ch)  # the polygon on the layer "Muri"
    link = LiveLink(tmp_path / "qgis", ch.crs)
    link.write({}, chunk=ch.key, free=ch)
    first = _by_id(link.path, POLYGONS)
    assert set(first) == {poly["id"]}

    fd.set_layer(ch, "Muri", visible=False)
    assert link.write_free(ch) != []
    assert _by_id(link.path, POLYGONS) == {} and set(_by_id(link.path, LINES)) == {line["id"]}
    assert set(_by_id(link.path, POINTS)) == {point["id"]}
    assert link.read_changes() == {} and [r["id"] for r in fd.features(ch, "Muri")] == [poly["id"]]
    assert link.write_free(ch) == []  # nothing changed: nothing written

    fd.set_layer(ch, "Muri", visible=True)
    link.write_free(ch)
    assert set(_by_id(link.path, POLYGONS)) == {poly["id"]} and link.read_changes() == {}

    qgis = Qgis(link.path)  # edited there, hidden here before the look: read, then it leaves
    fid = _by_id(link.path, POLYGONS)[poly["id"]]["fid"]
    qgis.save((f'UPDATE "{POLYGONS}" SET name = ? WHERE fid = ?', ("Muro rifatto", fid)))
    fd.set_layer(ch, "Muri", visible=False)
    link.write_free(ch)
    assert _by_id(link.path, POLYGONS)[poly["id"]]["name"] == "Muro rifatto"
    got = link.read_changes()
    assert [f.id for f in got[POLYGONS].changed] == [poly["id"]] and got[POLYGONS].deleted == []
    link._signatures.clear()
    link.write_free(ch)
    assert _by_id(link.path, POLYGONS) == {} and link.read_changes() == {}
    qgis.close()
    link.close()


# ---------------------------------------------------------------- the window
def _tick(w, name, shown):
    tree = w.layers_pane.tree
    item = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)[0] == name)
    item.setCheckState(0, Qt.CheckState.Checked if shown else Qt.CheckState.Unchecked)


def _write(w):
    """The write the window asked for by itself, made now."""
    assert w._qgis_timer.isActive()
    w._qgis_timer.stop()
    w._export_qgis_layers()
    w._poll_qgis()


def test_a_layer_unticked_leaves_qgis_and_comes_back_with_its_edits(trench):
    w, app, mw = trench
    Plugin(w)
    v = w.ortho_view
    path = _live(w)
    rows = _by_n(_of(path, "linee", LAYER))
    assert len(rows) == 5 and len(_of(path, "poligoni", STONES)) == 2

    qgis = Qgis(path)  # an edit made by hand, kept with the chunk
    moved = LineString([(X0 + 1, Y0 + 1.02), (X0 + 2, Y0 + 2), (X0 + 3, Y0 + 1.02)])
    qgis.save(qgis.move("linee", rows[2]["fid"], moved), qgis.delete("linee", rows[0]["fid"]))
    w._poll_qgis()
    w._export_qgis_layers()
    edits = repr(w.chunk.meta[mw.PLAN_EDITS])
    said = _from_qgis(w)
    w._qgis_timer.stop()

    _tick(w, LAYER, False)
    assert not v.layers[LAYER]["visible"]
    _write(w)  # asked by the tick alone
    assert _of(path, "linee", LAYER) == [] and len(_of(path, "poligoni", STONES)) == 2
    assert _from_qgis(w) == said  # nothing read as deleted there
    assert len(v.features(LAYER)) == 4 and repr(w.chunk.meta[mw.PLAN_EDITS]) == edits
    w._poll_qgis()
    assert _from_qgis(w) == said and len(v.features(LAYER)) == 4

    _tick(w, LAYER, True)
    _write(w)
    back = _by_n(_of(path, "linee", LAYER))
    assert set(back) == {1, 2, 3, 4} and back[2]["geom"].equals(moved)
    assert {n: r[ID_FIELD] for n, r in back.items()} == {n: rows[n][ID_FIELD] for n in back}
    assert _from_qgis(w) == said and repr(w.chunk.meta[mw.PLAN_EDITS]) == edits

    qgis.save(qgis.move("linee", back[3]["fid"], LineString([(X0 + 1, Y0 + 2), (X0 + 3, Y0 + 2)])))
    w._poll_qgis()  # still both ways
    assert [a["_geom"][1] for a in v.features(LAYER) if a.get("n") == 3] == [[[X0 + 1, Y0 + 2], [X0 + 3, Y0 + 2]]]
    qgis.close()


def test_none_and_all_of_the_pane_and_their_undo_reach_qgis(trench):
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    w, app, mw = trench
    Plugin(w)
    w._add_unit(UnitPolygon(box(X0 + 1, Y0 + 4, X0 + 2, Y0 + 5), us="6", area="1", sito="Saggio"))
    path = _live(w)
    assert len(_rows(path, "linee")) == 5 and len(_rows(path, "poligoni")) == 2 and len(_rows(path, "unita")) == 1
    w._qgis_timer.stop()

    w.layers_pane.b_none.click()
    _write(w)
    assert _rows(path, "linee") == [] and _rows(path, "poligoni") == [] and _rows(path, "unita") == []
    assert _from_qgis(w) == [] and [u.us for u in w.units] == ["6"] and len(w.ortho_view.features(LAYER)) == 5

    w.history.undo()
    _write(w)
    assert len(_rows(path, "linee")) == 5 and len(_rows(path, "poligoni")) == 2 and len(_rows(path, "unita")) == 1
    assert _from_qgis(w) == [] and [u.us for u in w.units] == ["6"]


def test_a_free_layer_unticked_leaves_qgis(trench):
    w, app, mw = trench
    line, poly, point = _three(w.chunk)
    w._dirty(True)
    w._draw_free()
    path = _live(w)
    assert set(_by_id(path, POLYGONS)) == {poly["id"]}
    w._qgis_timer.stop()

    _tick(w, mw.LAYER_PREFIX + "Muri", False)
    _write(w)
    assert _by_id(path, POLYGONS) == {} and set(_by_id(path, LINES)) == {line["id"]}
    assert _from_qgis(w) == [] and [r["id"] for r in fd.features(w.chunk, "Muri")] == [poly["id"]]
    _tick(w, mw.LAYER_PREFIX + "Muri", True)
    _write(w)
    assert set(_by_id(path, POLYGONS)) == {poly["id"]} and _from_qgis(w) == []
