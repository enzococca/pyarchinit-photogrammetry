"""One layer in QGIS for each layer of the program: the QGIS project gets a layer for every layer with drawings in
the live GeoPackage, filtered on its name (two for a layer of lines and polygons; none for a table without rows), and
what is drawn in it comes back in that layer. A feature goes to the table of its own geometry, whatever the first
feature of its layer was - a file and a project of 1.7.8, which sent them all to the table of the first, are taken
without anything read as changed. The ticks of the Layers pane stay with the project."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, box

from pyarchinit_photogrammetry.core import free_drawings as fd
from pyarchinit_photogrammetry.io import qgis_link
from pyarchinit_photogrammetry.io.qgis_link import (ID_FIELD, LIVE_DB, LIVE_TABLES, LiveLink, QgisInstall,
                                                    build_project, layer_subset, layers_not_in_project,
                                                    legacy_identities, live_vectors, plan_identities, project_layers,
                                                    subset_layer)
from test_free_drawings_io import _chunk, _three
from test_plan_two_way import LAYER, STONES, Plugin, _by_n, _line, _live
from test_qgis_two_way import Qgis, X0, Y0, _from_qgis, _log, _rows, _square

CRS = "EPSG:3004"
MIXED = "PhotoCAD: Rilievo"
UNITS = "Unita' stratigrafiche"


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


# ---------------------------------------------------------------- one layer of QGIS for each layer of the program
def _unit(us, x):
    return {"us_s": str(us), "area_s": "1", "ppg_id": f"u{us}", "_geom": _square(x, Y0)}


def test_every_layer_with_drawings_makes_a_layer_of_qgis(tmp_path):
    layers = {**_mixed(), UNITS: {"kind": "polygon", "features": [_unit(6, X0)]},
              "Muri d'ambito": {"kind": "line", "features": [{"_geom": ("LineString", _line(40))}]},
              "Vuoto": {"kind": "line", "features": []}}
    link = LiveLink(tmp_path, CRS)
    files = link.write(layers)
    vectors = live_vectors(link.path)
    assert [(v["name"], v["table"]) for v in vectors] == [
        (UNITS, "unita"), (f"{MIXED} - poligoni", "poligoni"), (f"{MIXED} - linee", "linee"), (LAYER, "linee"),
        ("Muri d'ambito", "linee"), (f"{MIXED} - punti", "punti")]  # no layer for sezioni, curve, quote: no rows
    by = {v["name"]: v for v in vectors}
    assert by["Muri d'ambito"]["subset"] == "\"layer\" = 'Muri d''ambito'"
    assert by["Muri d'ambito"]["defaults"] == {"layer": "Muri d'ambito"}
    assert by[f"{MIXED} - linee"]["subset"] == by[f"{MIXED} - poligoni"]["subset"] == f"\"layer\" = '{MIXED}'"
    assert all(v["geometry"] == LIVE_TABLES[v["table"]][1] and v["path"] == str(link.path) for v in vectors)
    lines = [v["name"] for v in live_vectors(link.path, ["linee"])]
    assert lines == [f"{MIXED} - linee", LAYER, "Muri d'ambito"]  # the same names, whatever tables are asked
    assert live_vectors(tmp_path / "missing.gpkg") == []
    for name in ("Muri d'ambito", "a 'b' c", 'with "quotes"', "x|y"):
        assert subset_layer(layer_subset(name)) == name
    assert subset_layer(layer_subset("")) == "" and subset_layer("") is None and subset_layer('"us_s" = 6') is None

    seen = {}

    def run(args, **kwargs):  # QGIS's Python: what it is given
        seen.update(json.loads(Path(args[2]).read_text(encoding="utf-8")))
        Path(seen["project"]).write_text("<qgis/>", encoding="utf-8")
        report = {"written": True, "vectors": [], "rasters": []}
        return subprocess.CompletedProcess(args, 0, "PPG_QGIS_REPORT " + json.dumps(report) + "\n", "")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(qgis_link.subprocess, "run", run)
        extra = [{"name": "Zone", "path": str(link.path), "table": "linee", "geometry": "LINESTRINGZ", "color": "#000"}]
        build_project(tmp_path / "p.qgs", CRS, files, [], qgis=QgisInstall(app="qgis", python="python-of-qgis"),
                      extra_vectors=extra)
    assert [v["name"] for v in seen["vectors"]] == [v["name"] for v in vectors] + ["Zone"]
    assert seen["vectors"][0]["subset"] == layer_subset(UNITS) and "subset" not in seen["vectors"][-1]
    link.close()


def test_rows_without_a_layer_have_the_layer_of_their_table(tmp_path):
    link = LiveLink(tmp_path, CRS)
    link.write(_mixed())
    qgis = Qgis(link.path)
    qgis.save(qgis.insert("linee", LineString([(X0, Y0), (X0 + 1, Y0)])),  # drawn in a project of 1.7.8: no layer
              qgis.insert("punti", Point(X0, Y0), layer=""))
    qgis.close()
    by = {v["name"]: v for v in live_vectors(link.path)}
    assert by[LIVE_TABLES["linee"][0]]["layer"] == "" and "defaults" not in by[LIVE_TABLES["linee"][0]]
    assert by[LIVE_TABLES["punti"][0]]["subset"] == by[LIVE_TABLES["linee"][0]]["subset"] == layer_subset(None)
    assert f"{MIXED} - linee" in by
    link.close()


def test_the_layers_of_the_free_drawings_are_one_each(tmp_path):
    _doc, ch = _chunk(tmp_path)
    _three(ch)  # a line and a point in the default layer, a polygon in "Muri"
    link = LiveLink(tmp_path / "qgis", ch.crs)
    link.write({}, chunk=ch.key, free=ch)
    vectors = link.free_vectors()
    assert [(v["name"], v["table"]) for v in vectors] == [
        (f"Disegni 3D: {fd.DEFAULT_LAYER} - linee", "disegni_3d_linee"), ("Disegni 3D: Muri", "disegni_3d_poligoni"),
        (f"Disegni 3D: {fd.DEFAULT_LAYER} - punti", "disegni_3d_punti")]
    assert vectors[1]["subset"] == layer_subset("Muri") and vectors[1]["defaults"] == {"layer": "Muri"}
    assert all(v["geometry"] in ("LINESTRING", "POLYGON", "POINT") for v in vectors)
    assert link.elevation_tables(filled=True) == []
    link.close()


_PROJECT = """<qgis><projectlayers>
<maplayer><datasource>./{db}|layername=linee|subset="layer" = 'Muri d''ambito'</datasource></maplayer>
<maplayer><datasource>./{db}|layername=poligoni|subset="layer" = '{mixed}'</datasource></maplayer>
<maplayer><datasource>./{db}|layername=quote</datasource></maplayer>
<maplayer><datasource>./ortho.tif</datasource></maplayer>
<maplayer><datasource>./altro.gpkg|layername=linee</datasource></maplayer>
<maplayer><datasource></datasource></maplayer>
</projectlayers></qgis>"""


def test_the_layers_of_a_project_are_read_from_it(tmp_path):
    project = tmp_path / "p.qgs"
    project.write_text(_PROJECT.format(db=LIVE_DB, mixed=MIXED), encoding="utf-8")
    have = project_layers(project)
    assert have == {("linee", "Muri d'ambito"), ("poligoni", MIXED), ("quote", None)}
    assert project_layers(tmp_path / "none.qgs") is None
    (tmp_path / "bad.qgs").write_text("not xml", encoding="utf-8")
    assert project_layers(tmp_path / "bad.qgs") is None
    vectors = [{"name": "a", "table": "linee", "layer": "Muri d'ambito"}, {"name": "b", "table": "linee", "layer": "B"},
               {"name": "c", "table": "poligoni", "layer": MIXED}, {"name": "d", "table": "quote", "layer": "Quote"},
               {"name": "e", "table": "disegni_3d_linee", "layer": "Muri"}]
    assert layers_not_in_project(have, vectors) == ["b", "e"]  # "quote" is there whole: every layer of it shown
    assert layers_not_in_project(None, vectors) == []


# ---------------------------------------------------------------- with QGIS itself
_QGIS_EDIT = r"""
import json, sys
from qgis.core import QgsApplication, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayerUtils
app = QgsApplication([], False)
app.initQgis()
proj = QgsProject.instance()
proj.read(sys.argv[1])
layers = {l.name(): l for l in proj.mapLayers().values()}
x, y = float(sys.argv[2]), float(sys.argv[3])
out = {"layers": {n: [sum(1 for _ in l.getFeatures()), l.subsetString(),
                      l.defaultValueDefinition(l.fields().indexOf("layer")).expression()] for n, l in layers.items()}}
lines = layers[sys.argv[4]]
ok = [lines.startEditing()]
ok.append(lines.addFeature(QgsVectorLayerUtils.createFeature(
    lines, QgsGeometry.fromPolylineXY([QgsPointXY(x, y), QgsPointXY(x + 1, y)]))))
ok.append(lines.commitChanges())
other = layers[sys.argv[5]]
old = next(other.getFeatures())
ok += [other.startEditing(), other.changeGeometry(old.id(), QgsGeometry.fromPolylineXY(
    [QgsPointXY(x, y + 1), QgsPointXY(x + 1, y + 1.25)])), other.commitChanges()]
out["ok"] = ok
out["after"] = sum(1 for _ in lines.getFeatures())
print("STATE " + json.dumps(out), flush=True)
app.exitQgis()
"""


def test_qgis_draws_in_the_layer_of_a_layer_and_it_comes_back(tmp_path):
    """With QGIS's own Python (skipped without QGIS): the project has a valid layer for each layer of the program,
    each shows its rows only, and a feature drawn in one - created as QGIS's digitising does, with the defaults of
    the layer - is saved with the name of the layer and read back in it; so is a vertex moved."""
    q = qgis_link.find_qgis()
    if q is None or not q.python or not Path(q.python).exists():
        pytest.skip("QGIS not installed")
    link = LiveLink(tmp_path, CRS)
    files = link.write({**_mixed(), "Muri d'ambito": {"kind": "line",
                                                     "features": [{"_geom": ("LineString", _line(40)), "n": 40}]}})
    project = tmp_path / "p.qgs"
    rep = build_project(project, CRS, files, [], qgis=q)
    assert rep["written"] and all(v["valid"] for v in rep["vectors"])
    names = [f"{MIXED} - poligoni", f"{MIXED} - linee", LAYER, "Muri d'ambito", f"{MIXED} - punti"]
    assert [v["name"] for v in rep["vectors"]] == names
    assert project_layers(project) == {("poligoni", MIXED), ("linee", MIXED), ("linee", LAYER),
                                       ("linee", "Muri d'ambito"), ("punti", MIXED)}
    text = project.read_text(encoding="utf-8")
    assert text.count('autoRefreshEnabled="1"') == len(names) and "layername=sezioni" not in text
    script = tmp_path / "edit.py"
    script.write_text(_QGIS_EDIT, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PROJ_", "GDAL_", "QT_"))}
    env.update(q.env)
    env["QT_QPA_PLATFORM"] = "offscreen"
    r = subprocess.run([q.python, str(script), str(project), str(X0), str(Y0 + 5), f"{MIXED} - linee",
                        "Muri d'ambito"], capture_output=True, text=True, env=env, timeout=300, close_fds=False)
    state = json.loads(next(ln for ln in r.stdout.splitlines() if ln.startswith("STATE ")).split(" ", 1)[1])
    assert {n: v[0] for n, v in state["layers"].items()} == dict(zip(names, [2, 3, 2, 1, 1]))
    assert state["layers"]["Muri d'ambito"][1:] == ["\"layer\" = 'Muri d''ambito'", "'Muri d''ambito'"]
    assert all(state["ok"]) and state["after"] == 4
    changes = link.read_changes()
    assert list(changes) == ["linee"]
    assert [(f.values["layer"], f.geometry.geom_type) for f in changes["linee"].added] == [(MIXED, "LineString")]
    assert [f.values["layer"] for f in changes["linee"].changed] == ["Muri d'ambito"]
    link.close()


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


def test_a_layer_the_qgis_project_does_not_show_is_said_once(trench):
    w, app, mw = trench
    plugin = Plugin(w)
    assert plugin.drawn
    path = _live(w)
    said = lambda: [line for line in _log(w) if "non mostra ancora" in line]  # noqa: E731
    assert said() == []  # no project of QGIS yet
    project = w._qgis_project()
    project.write_text(f"<qgis><datasource>./{LIVE_DB}|layername=linee|subset={layer_subset(LAYER)}</datasource>"
                       "</qgis>", encoding="utf-8")
    w._export_qgis_layers(force=True)
    assert len(said()) == 1 and f"«{STONES}»" in said()[0] and LAYER not in said()[0].replace(STONES, "")
    w._export_qgis_layers(force=True)
    assert len(said()) == 1
    w.ortho_view.add_line(_line(50), "#000000", "", layer="Nuovo", attrs={})
    w._export_qgis_layers()
    assert len(said()) == 2 and "«Nuovo»" in said()[1]
    project.write_text("".join(f"<qgis><datasource>./{LIVE_DB}|layername={t}</datasource></qgis>"
                               for t in ("poligoni",)).replace("</qgis><qgis>", ""), encoding="utf-8")
    os.utime(project, ns=(project.stat().st_atime_ns, project.stat().st_mtime_ns + 5_000_000_000))
    w.ortho_view.set_layer_visible(STONES, False)
    w._export_qgis_layers()
    w.ortho_view.set_layer_visible(STONES, True)
    w._export_qgis_layers()
    assert len(said()) == 3 and STONES not in said()[2]  # a project of 1.7.8 with the table whole shows them all
    assert len(_rows(path, "poligoni")) == 2
