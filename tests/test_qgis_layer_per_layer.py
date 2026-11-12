"""A feature of the plan goes to the table of its own geometry in the live GeoPackage, whatever the first feature
of its layer was: a layer of lines and polygons (PhotoCAD) fills two tables and comes back from QGIS whole. A file
and a project of 1.7.8, which sent them all to the table of the first, are taken without anything read as changed. The ticks of the Layers pane
stay with the project."""
from __future__ import annotations

import pytest
from shapely.geometry import LineString, box

from pyarchinit_photogrammetry.io.qgis_link import ID_FIELD, LiveLink, legacy_identities, plan_identities
from test_plan_two_way import LAYER, STONES, Plugin, _by_n, _line, _live
from test_qgis_two_way import Qgis, X0, Y0, _from_qgis, _rows

CRS = "EPSG:3004"
MIXED = "PhotoCAD: Rilievo"


def _ring(i):
    return [[X0 + 1 + i, Y0 + 3], [X0 + 1.5 + i, Y0 + 3], [X0 + 1.5 + i, Y0 + 3.5], [X0 + 1 + i, Y0 + 3.5]]


def _mixed(first="polygon"):
    """A layer of two polygons and three lines (and a point), as PhotoCAD draws one: its kind is that of the first."""
    polygons = [{"_geom": ("Polygon", _ring(i)), "n": 10 + i} for i in range(2)]
    lines = [{"_geom": ("LineString", _line(i)), "n": i} for i in range(3)]
    point = [{"_geom": ("Point", [X0 + 4, Y0 + 4]), "n": 20}]
    feats = polygons + lines + point if first == "polygon" else lines + polygons + point
    return {MIXED: {"kind": first, "features": feats},
            LAYER: {"kind": "line",
                    "features": [{"_geom": ("LineString", _line(i + 10)), "n": 30 + i} for i in range(2)]}}


def _old(monkeypatch):
    """The link as 1.7.8 wrote the file: every feature in the table of the kind of its layer."""
    new = LiveLink._group
    monkeypatch.setattr(LiveLink, "_group", staticmethod(
        lambda layers, legacy=None: new(layers, legacy=legacy is not False)))
    return lambda: monkeypatch.setattr(LiveLink, "_group", staticmethod(new))


def _of(path, table, layer):
    return [r for r in _rows(path, table) if r["layer"] == layer]


# ---------------------------------------------------------------- a feature in the table of its geometry
@pytest.mark.parametrize("first", ["polygon", "line"])
def test_a_layer_of_lines_and_polygons_fills_the_table_of_each(tmp_path, first):
    link = LiveLink(tmp_path, CRS)
    link.write(_mixed(first))
    assert sorted(_by_n(_of(link.path, "poligoni", MIXED))) == [10, 11]
    assert sorted(_by_n(_of(link.path, "linee", MIXED))) == [0, 1, 2]
    assert sorted(_by_n(_of(link.path, "punti", MIXED))) == [20]
    assert all(r["geom"].geom_type == "Polygon" for r in _rows(link.path, "poligoni"))
    assert all(r["geom"].geom_type == "LineString" for r in _rows(link.path, "linee"))
    assert link.read_changes() == {}
    ids = plan_identities(_mixed(first))  # the window knows each by the table of its row
    assert {(t, name) for t, name, _a in ids.values()} == {("poligoni", MIXED), ("linee", MIXED), ("punti", MIXED),
                                                            ("linee", LAYER)}
    link.close()


def test_a_line_of_a_layer_that_begins_with_a_polygon_is_read_back(tmp_path):
    link = LiveLink(tmp_path, CRS)
    link.write(_mixed())
    row = _by_n(_of(link.path, "linee", MIXED))[1]
    moved = LineString([(X0 + 1, Y0 + 1), (X0 + 2, Y0 + 1.5)])
    qgis = Qgis(link.path)
    qgis.save(qgis.move("linee", row["fid"], moved))
    changes = link.read_changes()
    assert list(changes) == ["linee"] and [f.id for f in changes["linee"].changed] == [row[ID_FIELD]]
    assert changes["linee"].changed[0].values["layer"] == MIXED and changes["linee"].changed[0].geometry.equals(moved)
    qgis.close()
    link.close()


@pytest.mark.parametrize("first", ["polygon", "line"])
def test_a_file_of_1_7_8_is_taken_without_a_change_read(tmp_path, monkeypatch, first):
    now = _old(monkeypatch)
    old = LiveLink(tmp_path, CRS)
    old.write(_mixed(first), chunk="k")
    wrong, right = ("poligoni", "linee") if first == "polygon" else ("linee", "poligoni")
    assert len(_of(old.path, wrong, MIXED)) == 6 and _of(old.path, right, MIXED) == []  # all in the table of the first
    native = {r["n"]: (r["fid"], r[ID_FIELD]) for r in _of(old.path, wrong, MIXED)
              if r["geom"].geom_type.lower().startswith(first[:4])}
    other = {r["n"]: (r["fid"], r[ID_FIELD]) for r in _of(old.path, "linee", LAYER)}
    old.close()
    now()

    link = LiveLink(tmp_path, CRS)  # the program of now, started on that file
    assert link.read_changes() == {}
    link.write(_mixed(first), chunk="k")
    assert sorted(_by_n(_of(link.path, "poligoni", MIXED))) == [10, 11]
    assert sorted(_by_n(_of(link.path, "linee", MIXED))) == [0, 1, 2]
    assert sorted(_by_n(_of(link.path, "punti", MIXED))) == [20]
    assert {r["n"]: (r["fid"], r[ID_FIELD]) for r in _of(link.path, wrong, MIXED)} == native  # those in place
    assert {r["n"]: (r["fid"], r[ID_FIELD]) for r in _of(link.path, "linee", LAYER)} == other
    assert link.read_changes() == {}  # the rows moved to their table are not read as deleted and drawn
    link.write(_mixed(first), chunk="k")
    assert link.read_changes() == {}
    again = LiveLink(tmp_path, CRS)
    assert again.read_changes() == {}
    again.close()
    link.close()


def test_the_identities_of_1_7_8_are_found_again():
    layers = _mixed()
    renamed = legacy_identities(layers)
    now = plan_identities(layers)
    assert len(renamed) == 4 and set(renamed.values()) <= set(now)  # three lines and a point: their table changed
    assert {now[pid][0] for pid in renamed.values()} == {"linee", "punti"}
    assert not set(renamed) & set(now)
    assert legacy_identities({LAYER: layers[LAYER]}) == {}  # a layer of one kind: nothing changed
    kept = {**layers, MIXED: {"kind": "polygon", "features": [dict(f, ppg_id=f"id{i}")
                                                              for i, f in enumerate(layers[MIXED]["features"])]}}
    assert legacy_identities(kept) == {}  # an identity of its own never changes


# ---------------------------------------------------------------- the window
class Mixed(Plugin):
    """A plugin that draws polygons and then lines in one layer (PhotoCAD: a drawing with closed and open
    polylines)."""

    def draw(self):
        v = self.w.ortho_view
        if v.tr is None:
            return
        self.drawn += 1
        v.remove_layer(MIXED)
        with v.batch():
            for i in range(2):
                v.add_polygon(_ring(i), "", "#8d6e63", 60, layer=MIXED, attrs={"photocad": self.run, "n": 10 + i})
            for i in range(3):
                v.add_line(_line(i), "#ff4081", "", layer=MIXED, width=1.5, attrs={"photocad": self.run, "n": i})


def test_a_layer_of_a_plugin_with_lines_and_polygons_goes_both_ways(trench):
    w, app, mw = trench
    plugin = Mixed(w)
    v = w.ortho_view
    assert v.layers[MIXED]["kind"] == "polygon"
    path = _live(w)
    lines = _by_n(_of(path, "linee", MIXED))
    assert sorted(lines) == [0, 1, 2] and sorted(_by_n(_of(path, "poligoni", MIXED))) == [10, 11]
    assert _from_qgis(w) == []

    moved = LineString([(X0 + 1, Y0 + 1), (X0 + 2, Y0 + 1.5), (X0 + 3, Y0 + 1)])
    qgis = Qgis(path)
    qgis.save(qgis.move("linee", lines[1]["fid"], moved),
              qgis.insert("linee", LineString([(X0 + 1, Y0 + 2), (X0 + 2, Y0 + 2)]), layer=MIXED),  # as its layer of
              qgis.insert("poligoni", box(X0 + 4, Y0 + 3, X0 + 4.5, Y0 + 3.5), layer=MIXED))  # QGIS draws them
    w._poll_qgis()
    app.processEvents()
    feats = v.features(MIXED)
    assert len(feats) == 7 and sorted(a["_geom"][0] for a in feats) == ["LineString"] * 4 + ["Polygon"] * 3
    assert {a.get("n"): a for a in feats}[1]["_geom"][1] == [list(c) for c in moved.coords]
    w._export_qgis_layers()
    w._poll_qgis()
    assert len(_of(path, "linee", MIXED)) == 4 and len(_of(path, "poligoni", MIXED)) == 3
    assert _by_n(_of(path, "linee", MIXED))[1]["geom"].equals(moved)
    assert len(v.features(MIXED)) == 7  # nothing read twice
    drawn = plugin.drawn
    w.refreshed.emit()  # the plugin draws its layer again: the edits made in QGIS stay on it
    app.processEvents()
    assert plugin.drawn > drawn
    feats = v.features(MIXED)
    assert len(feats) == 7 and {a.get("n"): a for a in feats}[1]["_geom"][1] == [list(c) for c in moved.coords]
    qgis.close()


def test_a_project_and_a_file_of_1_7_8_are_taken_as_they_are(trench):
    import pyarchinit_photogrammetry as ppg

    w, app, mw = trench
    v = w.ortho_view
    with pytest.MonkeyPatch.context() as m:  # the program as it was in 1.7.8: one table for a layer, one identity
        _old(m)
        m.setattr(mw.MainWindow, "_plan_rename", lambda self, name: None)
        plugin = Mixed(w)
        assert plugin.drawn
        path = _live(w)  # the file as 1.7.8 left it: the lines of the layer among the polygons
        assert len(_of(path, "poligoni", MIXED)) == 5 and _of(path, "linee", MIXED) == []
        renamed = legacy_identities({MIXED: {"kind": "polygon", "features": v.features(MIXED)}})  # {then: now}
        assert len(renamed) == 3  # the three lines: their table is another now
        v.select_items([next(f[0] for f in v.layers[MIXED]["features"] if f[2].get("n") == 2)])
        assert w.delete_selected(confirm=False) == 1  # an edit made by hand, kept with the project...
        app.processEvents()
        was = list(w.chunk.meta[mw.PLAN_EDITS][MIXED]["deleted"])
        assert len(was) == 1 and was[0] in renamed  # ...under the identity 1.7.8 gave the line
        w._export_qgis_layers()
        assert len(_of(path, "poligoni", MIXED)) == 4
        assert w.save_document()
        app.processEvents()
        assert ppg.Document.open(w.doc.path).chunks[0].meta[mw.PLAN_EDITS][MIXED]["deleted"] == was

    w.set_document(ppg.Document.open(w.doc.path))  # the program of now opens that project on that file
    app.processEvents()
    assert sorted(a["n"] for a in v.features(MIXED)) == [0, 1, 10, 11]  # the line deleted by hand stays deleted
    kept = w.chunk.meta[mw.PLAN_EDITS][MIXED]["deleted"]
    assert kept == [renamed[was[0]]]  # under the identity of now
    w.a_qgis_live.setChecked(True)
    w._export_qgis_layers()
    w._poll_qgis()
    app.processEvents()
    assert sorted(_by_n(_of(path, "linee", MIXED))) == [0, 1]  # each in the table of its geometry
    assert sorted(_by_n(_of(path, "poligoni", MIXED))) == [10, 11]
    assert _from_qgis(w) == [] and sorted(a["n"] for a in v.features(MIXED)) == [0, 1, 10, 11]


def test_the_ticks_of_the_layers_stay_with_the_project(trench):
    import pyarchinit_photogrammetry as ppg

    w, app, mw = trench
    plugin = Plugin(w)
    v = w.ortho_view
    path = _live(w)
    w.setWindowModified(False)
    v.set_layer_visible(STONES, False)
    assert w.chunk.meta[mw.LAYERS_HIDDEN] == [STONES] and w.isWindowModified()
    v.set_layer_visible(LAYER, False)
    v.set_layer_visible(LAYER, True)
    assert w.chunk.meta[mw.LAYERS_HIDDEN] == [STONES]
    drawn = plugin.drawn
    w.refreshed.emit()  # drawn again by its plugin: still unticked
    app.processEvents()
    assert plugin.drawn > drawn
    assert not v.layers[STONES]["visible"] and v.layers[LAYER]["visible"]
    assert all(not f[0].isVisible() for f in v.layers[STONES]["features"])
    w._export_qgis_layers()
    assert _rows(path, "poligoni") == [] and len(_rows(path, "linee")) == 5

    assert w.save_document()
    w.set_document(ppg.Document.open(w.doc.path))  # the project opened again
    app.processEvents()
    assert w.chunk.meta[mw.LAYERS_HIDDEN] == [STONES]
    assert not v.layers[STONES]["visible"] and v.layers[LAYER]["visible"]
    assert w._plan_layers()[STONES]["sent"] is False and w._plan_layers()[LAYER]["sent"] is True
    assert not w.isWindowModified()
    w.a_qgis_live.setChecked(True)
    w._export_qgis_layers()
    assert _rows(path, "poligoni") == [] and len(_rows(path, "linee")) == 5

    v.set_layer_visible(STONES, True)  # ticked again: no key left in the project
    assert mw.LAYERS_HIDDEN not in w.chunk.meta
    w._export_qgis_layers()
    assert len(_rows(path, "poligoni")) == 2
