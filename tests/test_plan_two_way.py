"""The other drawings of the plan (the tables ``poligoni``, ``curve``, ``linee``, ``punti`` of the live GeoPackage: the
layers of the plugins, what is drawn in QGIS) both ways: what QGIS saves there comes into the 2D view within a look,
feature by feature, as one step of Modifica > Annulla; the edits made by hand (in QGIS, or deleted in the program) are
kept with the chunk and stay on the layer when its maker draws it again (a save, a step, another raster). The layers
the program draws from its own data stay one-way: said once, and put back."""
from __future__ import annotations

import contextlib
import os
import time

import pytest
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box

from pyarchinit_photogrammetry.io.qgis_link import ID_FIELD, LIVE_DB, PLAN_TABLES, LiveLink, plan_identities
from test_qgis_two_way import Qgis, X0, Y0, _from_qgis, _log, _rows

LAYER = "PhotoCAD: Ortofoto 1"
STONES = "PhotoCAD: Ortofoto 1 (pietre)"


def _line(i):
    return [[X0 + 1, Y0 + 1 + i * 0.0005], [X0 + 2, Y0 + 1.2 + i * 0.0005], [X0 + 3, Y0 + 1 + i * 0.0005]]


class Plugin:
    """A plugin that draws its layers the way PhotoCAD does: from its own results, the layers removed and drawn again
    each time the view shows another raster or the window shows the project again."""

    def __init__(self, w, n=5, batch=True):
        self.w, self.n, self.drawn, self.batch = w, n, 0, batch
        w.ortho_view.raster_loaded.connect(self.draw)
        w.refreshed.connect(self.draw)
        self.draw()

    def draw(self):
        v = self.w.ortho_view
        if v.tr is None:
            return
        self.drawn += 1
        for name in (LAYER, STONES):
            v.remove_layer(name)
        with v.batch() if self.batch else contextlib.nullcontext():
            for i in range(self.n):
                v.add_line(_line(i), "#ff4081", "", layer=LAYER, width=1.5,
                           attrs={"photocad": "abc", "n": i, "photocad_layer": "muri", "entity": "LINE", "k": 0.5})
            for i in range(2):
                ring = list(box(X0 + 1 + i, Y0 + 3, X0 + 1.5 + i, Y0 + 3.5).exterior.coords)[:-1]
                v.add_polygon(ring, "", "#8d6e63", 60, layer=STONES, attrs={"photocad": "abc", "n": i})


def _live(w):
    w.a_qgis_live.setChecked(True)
    w._export_qgis_layers()
    w._poll_qgis()
    return w._qgis_dir() / LIVE_DB


def _geoms(w, name):
    return [a["_geom"][1] for a in w.ortho_view.features(name)]


def _by_n(rows):
    return {int(r["n"]): r for r in rows if r.get("n") is not None}


# ---------------------------------------------------------------- the GeoPackage
def test_the_rows_of_the_plan_are_read_back(tmp_path):
    layers = {LAYER: {"kind": "line", "features": [{"_geom": ("LineString", _line(i)), "n": i} for i in range(4)]},
              "Punti notevoli": {"kind": "point", "features": [{"_geom": ("Point", [X0 + 1, Y0 + 1]), "nome": "a"}]}}
    link = LiveLink(tmp_path, "EPSG:3004")
    link.write(layers)
    assert link.read_changes() == {}
    ids = plan_identities(layers)
    rows = _by_n(_rows(link.path, "linee"))
    assert {r[ID_FIELD] for r in rows.values()} | {_rows(link.path, "punti")[0][ID_FIELD]} == set(ids)
    assert ids[rows[2][ID_FIELD]] == ("linee", LAYER, layers[LAYER]["features"][2])

    qgis = Qgis(link.path)
    moved = LineString([(X0 + 1, Y0 + 1.02), (X0 + 2, Y0 + 2), (X0 + 3, Y0 + 1.02)])
    qgis.save(qgis.move("linee", rows[2]["fid"], moved), qgis.delete("linee", rows[0]["fid"]),
              qgis.insert("linee", LineString([(X0, Y0), (X0 + 1, Y0)]), layer=LAYER))
    link.write(layers)  # never over what was saved there and nobody read
    assert _by_n(_rows(link.path, "linee"))[2]["geom"].equals(moved) and len(_rows(link.path, "linee")) == 4
    got = link.read_changes()
    assert set(got) == {"linee"} and set(got) <= set(PLAN_TABLES)
    ch = got["linee"]
    assert [f.id for f in ch.changed] == [rows[2][ID_FIELD]] and ch.changed[0].geometry.equals(moved)
    assert ch.deleted == [(rows[0][ID_FIELD], 1)]
    assert len(ch.added) == 1 and ch.added[0].values["layer"] == LAYER and ch.added[0].id
    assert link.read_changes() == {}  # given once

    link.write(layers)  # the program took none of them: its rows again, in place
    after = _by_n(_rows(link.path, "linee"))
    assert len(_rows(link.path, "linee")) == 4 and set(after) == {0, 1, 2, 3}
    assert after[2]["fid"] == rows[2]["fid"] and after[2]["geom"].equals(LineString(_line(2)))
    assert link.read_changes() == {}
    qgis.close()
    link.close()


# ---------------------------------------------------------------- the window
def test_a_plugin_layer_edited_in_qgis_comes_back_and_stays(trench):
    w, app, mw = trench
    plugin = Plugin(w)
    v = w.ortho_view
    path = _live(w)
    assert _from_qgis(w) == []
    rows = _by_n(_rows(path, "linee"))
    assert len(rows) == 5 and mw.PLAN_EDITS not in w.chunk.meta

    qgis = Qgis(path)
    moved = LineString([(X0 + 1, Y0 + 1.02), (X0 + 2, Y0 + 2), (X0 + 2.5, Y0 + 2.1), (X0 + 3, Y0 + 1.02)])
    two = MultiLineString([[(X0 + 4, Y0 + 4), (X0 + 5, Y0 + 4)], [(X0 + 4, Y0 + 5), (X0 + 5, Y0 + 5)]])
    qgis.save(qgis.move("linee", rows[2]["fid"], moved, photocad_layer="soglie"),
              qgis.delete("linee", rows[0]["fid"]),
              qgis.insert("linee", two, layer=LAYER, photocad_layer="aggiunte"),
              qgis.insert("linee", LineString([(X0 + 1, Y0 + 5), (X0 + 2, Y0 + 5)])),
              qgis.insert("punti", Point(X0 + 2, Y0 + 2), layer="Chiodi", etichetta="c1"))
    w._poll_qgis()
    feats = {a.get("n"): a for a in v.features(LAYER) if a.get("n") is not None}
    assert set(feats) == {1, 2, 3, 4}
    assert feats[2]["_geom"][1] == [list(c) for c in moved.coords] and feats[2]["photocad_layer"] == "soglie"
    assert feats[2]["photocad"] == "abc" and feats[2]["k"] == 0.5  # the plugin's columns as they were
    assert feats[2][ID_FIELD] == rows[2][ID_FIELD]
    added = [a for a in v.features(LAYER) if a.get("n") is None]
    assert len(added) == 1 and added[0]["_geom"][1] == [[X0 + 4, Y0 + 4], [X0 + 5, Y0 + 4]]
    assert added[0]["photocad_layer"] == "aggiunte"
    assert _geoms(w, mw.QGIS_LAYER) == [[[X0 + 1, Y0 + 5], [X0 + 2, Y0 + 5]]]  # no layer named: the standard one
    assert _geoms(w, "Chiodi") == [[X0 + 2, Y0 + 2]] and v.features("Chiodi")[0]["etichetta"] == "c1"
    assert sorted(_from_qgis(w)) == sorted([
        f"Da QGIS: elemento del layer {LAYER} modificato", f"Da QGIS: elemento del layer {LAYER} eliminato",
        f"Da QGIS: elemento del layer {LAYER} aggiunto (di più parti, tenuta la prima)",
        f"Da QGIS: elemento del layer {mw.QGIS_LAYER} aggiunto", "Da QGIS: elemento «c1» del layer Chiodi aggiunto"])
    assert w.history.undo_text() == "Da QGIS: 5 modifiche" and w.isWindowModified()
    edits = w.chunk.meta[mw.PLAN_EDITS]
    assert set(edits) == {LAYER, mw.QGIS_LAYER, "Chiodi"} and edits[LAYER]["deleted"] == [rows[0][ID_FIELD]]

    # the program's version of the rows: in place, nothing comes back
    w._export_qgis_layers()
    after = _rows(path, "linee")
    assert len(after) == 6 and _by_n(after)[2]["fid"] == rows[2]["fid"] and _by_n(after)[2]["geom"].equals(moved)
    assert sorted(r["layer"] for r in after) == sorted([LAYER] * 5 + [mw.QGIS_LAYER])
    first = next(r for r in after if r["photocad_layer"] == "aggiunte")["geom"]
    assert first.equals(LineString(two.geoms[0]))
    assert len(_rows(path, "punti")) == 1
    w._poll_qgis()
    assert len(_from_qgis(w)) == 5

    # the plugin draws its layers again from its results (a step done): the edits made by hand are still on them
    drawn = plugin.drawn
    w.refreshed.emit()
    app.processEvents()
    assert plugin.drawn == drawn + 1
    feats = {a.get("n"): a for a in v.features(LAYER) if a.get("n") is not None}
    assert set(feats) == {1, 2, 3, 4} and feats[2]["_geom"][1] == [list(c) for c in moved.coords]
    assert len(v.features(LAYER)) == 5 and len(v.features(STONES)) == 2
    w._export_qgis_layers()
    again = _rows(path, "linee")
    assert {r["fid"] for r in again} == {r["fid"] for r in after} and _by_n(again)[2]["geom"].equals(moved)

    # saved with the project, and still there after the save
    assert w.save_document()
    app.processEvents()
    assert set(a.get("n") for a in v.features(LAYER)) == {1, 2, 3, 4, None}
    assert _geoms(w, "Chiodi") == [[X0 + 2, Y0 + 2]] and len(_geoms(w, mw.QGIS_LAYER)) == 1
    import pyarchinit_photogrammetry as ppg

    stored = ppg.Document.open(w.doc.path).chunks[0].meta[mw.PLAN_EDITS]
    assert stored == w.chunk.meta[mw.PLAN_EDITS]
    qgis.close()


def test_qgis_edits_of_a_plugin_layer_are_undone_in_one_step(trench):
    w, app, mw = trench
    Plugin(w)
    v = w.ortho_view
    path = _live(w)
    rows = _by_n(_rows(path, "linee"))
    original = _geoms(w, LAYER)
    qgis = Qgis(path)
    moved = LineString([(X0 + 1, Y0 + 1.02), (X0 + 2, Y0 + 2), (X0 + 3, Y0 + 1.02)])
    qgis.save(qgis.move("linee", rows[2]["fid"], moved), qgis.delete("linee", rows[0]["fid"]),
              qgis.insert("linee", LineString([(X0 + 1, Y0 + 5), (X0 + 2, Y0 + 5)])))
    w._poll_qgis()
    assert len(v.features(LAYER)) == 4 and mw.QGIS_LAYER in v.layers
    w.undo()
    app.processEvents()
    assert _geoms(w, LAYER) == original and mw.QGIS_LAYER not in v.layers
    assert mw.PLAN_EDITS not in w.chunk.meta
    w._export_qgis_layers()
    back = _rows(path, "linee")
    assert len(back) == 5 and _by_n(back)[2]["fid"] == rows[2]["fid"]
    assert _by_n(back)[2]["geom"].equals(LineString(_line(2)))
    w._poll_qgis()
    assert len(_from_qgis(w)) == 3
    w.redo()
    app.processEvents()
    assert len(v.features(LAYER)) == 4 and len(_geoms(w, mw.QGIS_LAYER)) == 1
    assert [g for g in _geoms(w, LAYER) if g == [list(c) for c in moved.coords]]
    qgis.close()


def test_the_layers_the_program_draws_stay_one_way(trench):
    w, app, mw = trench
    v = w.ortho_view
    key = w._stones_key()
    w.shown_stones[key] = [Polygon([(X0 + 1, Y0 + 1), (X0 + 1.4, Y0 + 1), (X0 + 1.4, Y0 + 1.3), (X0 + 1, Y0 + 1.3)])]
    contours = "Curve di livello 0.5 m"
    w.line_layers[contours] = [([[X0 + 1, Y0 + 2], [X0 + 3, Y0 + 2]], "#8d6e63", "100", {"quota": 100.0}, 1.0)]
    w._redraw_overlays()
    path = _live(w)
    stone, contour = _rows(path, "poligoni")[0], _rows(path, "curve")[0]
    qgis = Qgis(path)
    qgis.save(qgis.move("poligoni", stone["fid"], box(X0 + 1, Y0 + 1, X0 + 3, Y0 + 3)),
              qgis.delete("curve", contour["fid"]),
              qgis.insert("poligoni", box(X0 + 4, Y0 + 4, X0 + 5, Y0 + 5), layer=mw.STONES_LAYER))
    before = w.history.undo_text()
    w._poll_qgis()
    assert len(v.features(mw.STONES_LAYER)) == 1 and len(v.features(contours)) == 1
    assert mw.PLAN_EDITS not in w.chunk.meta and w.history.undo_text() == before
    said = [line for line in _log(w) if "si modifica nel programma" in line]
    assert sorted(said) == sorted(
        f"Da QGIS: il layer {name} è disegnato dal programma dai suoi dati e si modifica nel programma: le modifiche "
        "fatte lì non sono riprese, il layer torna com'era" for name in (mw.STONES_LAYER, contours))
    w._export_qgis_layers()  # as they were
    stones = _rows(path, "poligoni")
    assert len(stones) == 1 and stones[0]["fid"] == stone["fid"] and stones[0]["geom"].equals(stone["geom"])
    assert len(_rows(path, "curve")) == 1
    w._poll_qgis()
    qgis.save(qgis.move("poligoni", stone["fid"], box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2)))
    w._poll_qgis()
    assert len([line for line in _log(w) if "si modifica nel programma" in line]) == 2  # once per layer
    qgis.close()


def test_what_makes_no_feature_of_its_layer_is_left(trench):
    w, app, mw = trench
    Plugin(w)
    path = _live(w)
    rows = _by_n(_rows(path, "linee"))
    qgis = Qgis(path)
    qgis.save(qgis.insert("linee", LineString([(X0, Y0), (X0 + 1, Y0)]), layer=STONES),  # a line in a layer of polygons
              (f'UPDATE linee SET geom = NULL WHERE fid = {rows[1]["fid"]}', ()))
    w._poll_qgis()
    assert len(w.ortho_view.features(STONES)) == 2 and len(w.ortho_view.features(LAYER)) == 5
    log = "\n".join(_log(w))
    assert "non fa un disegno di questo layer" in log and "senza geometria leggibile" in log
    w._export_qgis_layers()
    assert len(_rows(path, "linee")) == 5 and _by_n(_rows(path, "linee"))[1]["geom"].equals(LineString(_line(1)))
    qgis.close()


# ---------------------------------------------------------------- edits made in the program
@pytest.mark.parametrize("live,batch", [(True, True), (False, True), (True, False)])
def test_a_feature_of_a_plugin_layer_deleted_in_the_program_stays_deleted_after_a_save(trench, live, batch):
    w, app, mw = trench
    plugin = Plugin(w, batch=batch)  # also one that draws its features one by one, a change of the layers for each
    v = w.ortho_view
    path = _live(w) if live else None
    v.select_items([v.layers[LAYER]["features"][2][0], v.layers[STONES]["features"][0][0]])
    assert w.delete_selected(confirm=False) == 2
    app.processEvents()
    assert [a["n"] for a in v.features(LAYER)] == [0, 1, 3, 4] and [a["n"] for a in v.features(STONES)] == [1]
    if live:
        w._export_qgis_layers()
        assert sorted(_by_n(_rows(path, "linee"))) == [0, 1, 3, 4] and len(_rows(path, "poligoni")) == 1
    drawn = plugin.drawn
    assert w.save_document()  # the window shows the project again: the plugin draws its layers from its results
    app.processEvents()
    assert plugin.drawn > drawn
    assert [a["n"] for a in v.features(LAYER)] == [0, 1, 3, 4] and [a["n"] for a in v.features(STONES)] == [1]
    if live:
        w._export_qgis_layers()
        assert sorted(_by_n(_rows(path, "linee"))) == [0, 1, 3, 4] and len(_rows(path, "poligoni")) == 1
        w._poll_qgis()
        assert _from_qgis(w) == []
    w._load_raster()  # and when the raster is shown again
    app.processEvents()
    assert [a["n"] for a in v.features(LAYER)] == [0, 1, 3, 4]
    w.undo()  # the deletion is a step of Modifica > Annulla
    app.processEvents()
    assert [a["n"] for a in v.features(LAYER)] == [0, 1, 2, 3, 4] and [a["n"] for a in v.features(STONES)] == [0, 1]
    assert mw.PLAN_EDITS not in w.chunk.meta
    w.redo()
    app.processEvents()
    assert [a["n"] for a in v.features(LAYER)] == [0, 1, 3, 4] and [a["n"] for a in v.features(STONES)] == [1]


# ---------------------------------------------------------------- large layers
def test_seven_thousand_lines_stay_fast(trench):
    w, app, mw = trench
    plugin = Plugin(w, n=7000)
    v = w.ortho_view
    t0 = time.monotonic()
    path = _live(w)
    first = time.monotonic() - t0
    rows = _by_n(_rows(path, "linee"))
    qgis = Qgis(path)
    moved = LineString([(X0 + 1, Y0 + 1.02), (X0 + 2, Y0 + 2), (X0 + 3, Y0 + 1.02)])
    qgis.save(qgis.move("linee", rows[2]["fid"], moved), qgis.delete("linee", rows[0]["fid"]))
    t0 = time.monotonic()
    w._poll_qgis()
    took = time.monotonic() - t0
    assert len(v.features(LAYER)) == 6999
    t0 = time.monotonic()
    w._export_qgis_layers()
    write = time.monotonic() - t0
    t0 = time.monotonic()
    for _ in range(20):
        w._poll_qgis()  # nothing saved there: a look costs nothing
    idle = (time.monotonic() - t0) / 20
    t0 = time.monotonic()
    plugin.draw()
    app.processEvents()
    redraw = time.monotonic() - t0
    assert len(v.features(LAYER)) == 6999
    print(f"\n7000 lines: first write {first:.2f} s, edit read and applied {took:.2f} s, write after {write:.2f} s, "
          f"idle look {idle * 1000:.2f} ms, plugin redraw with the edits {redraw:.2f} s")
    if not os.environ.get("CI"):
        assert took < 5 and write < 3 and idle < 0.05 and redraw < 5
    qgis.close()
