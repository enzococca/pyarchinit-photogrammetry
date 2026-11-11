"""The free drawings in 3D both ways through the live GeoPackage: the tables ``disegni_3d_*`` written row by row by
identity, what QGIS saves there (a drawing edited, deleted, drawn) read back into the chunk, never written over before
it is read, found also when it was saved while the program was closed, undone with Modifica > Annulla. QGIS is
simulated by a second sqlite3 connection editing the file, as its GeoPackage provider does when an edit session is
saved."""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest
from shapely.geometry import LineString, MultiLineString, Point, Polygon

from pyarchinit_photogrammetry.core import free_drawings as fd
from pyarchinit_photogrammetry.io import free_drawings as fio
from pyarchinit_photogrammetry.io import gpkg
from pyarchinit_photogrammetry.io.qgis_link import ID_FIELD, LIVE_DB, SIG_FIELD, LiveLink, UnreadEdits

CRS = "EPSG:3004"
X0, Y0 = 2480000.0, 4500000.0
LINES, POLYGONS, POINTS = "disegni_3d_linee", "disegni_3d_poligoni", "disegni_3d_punti"


def _chunk(tmp_path, label="A"):
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    ch = doc.add_chunk(label)
    ch.crs = CRS
    doc.save(tmp_path / "progetto.ppg")
    return doc, ch


def _three(ch):
    line = fd.add(ch, "line", [[X0 + 1.0, Y0 + 1.0, 101.25], [X0 + 2.5, Y0 + 1.75, 100.875],
                               [X0 + 3.0, Y0 + 0.5, 99.5]], name="Linea A")
    poly = fd.add(ch, "polygon", [[X0 + 4.0, Y0 + 4.0, 100.0], [X0 + 5.0, Y0 + 4.0, 100.5],
                                  [X0 + 5.0, Y0 + 5.0, 101.0], [X0 + 4.0, Y0 + 5.0, 100.25]], layer="Muri",
                  name="Muro 1", attributes={"materiale": "pietra", "fase 2": "II", "n": 3})
    point = fd.add(ch, "point", [[X0 + 5.0, Y0 + 1.0, 102.125]], name="P")
    return line, poly, point


def _rows(path, table):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        out = []
        for r in con.execute(f'SELECT * FROM "{table}" ORDER BY fid'):
            r = dict(r)
            r["geom"] = gpkg.geometry(r.pop("geom"))
            out.append(r)
        return out
    finally:
        con.close()


def _by_id(path, table):
    return {r[ID_FIELD]: r for r in _rows(path, table)}


class Qgis:
    """Another program editing the GeoPackage: a connection of its own, one transaction per save."""

    def __init__(self, path):
        self.con = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.srs = self.con.execute("SELECT srs_id FROM gpkg_contents WHERE data_type = 'features'").fetchone()[0]

    def save(self, *statements):
        self.con.execute("BEGIN IMMEDIATE")
        for sql, args in statements:
            self.con.execute(sql, args)
        self.con.execute("COMMIT")

    def move(self, table, fid, geom, **values):
        sets = "".join(f', "{k}" = ?' for k in values)
        return (f'UPDATE "{table}" SET geom = ?{sets} WHERE fid = ?',
                (gpkg.blob(geom, self.srs, z=geom.has_z), *values.values(), fid))

    @staticmethod
    def edit(table, fid, **values):
        sets = ", ".join(f'"{k}" = ?' for k in values)
        return f'UPDATE "{table}" SET {sets} WHERE fid = ?', (*values.values(), fid)

    def insert(self, table, geom, **values):
        names = "".join(f', "{k}"' for k in values)
        marks = ", ?" * len(values)
        return (f'INSERT INTO "{table}" (geom{names}) VALUES (?{marks})',
                (gpkg.blob(geom, self.srs, z=geom.has_z), *values.values()))

    @staticmethod
    def delete(table, fid):
        return f'DELETE FROM "{table}" WHERE fid = ?', (fid,)

    def close(self):
        self.con.close()


def _link(tmp_path, ch):
    link = LiveLink(tmp_path / "qgis", ch.crs)
    link.write({}, chunk=ch.key, free=ch)
    return link


# ---------------------------------------------------------------- the GeoPackage
def test_free_rows_are_written_in_place_by_identity(tmp_path):
    """A row keeps its fid while its drawing lives, only the rows that changed are written, each one signed."""
    _doc, ch = _chunk(tmp_path)
    line, poly, point = _three(ch)
    link = _link(tmp_path, ch)
    first = {t: _by_id(link.path, t) for t in (LINES, POLYGONS, POINTS)}
    assert set(first[LINES]) == {line["id"]} and set(first[POLYGONS]) == {poly["id"]}
    assert all(r[SIG_FIELD] for rows in first.values() for r in rows.values())
    assert first[POLYGONS][poly["id"]]["materiale"] == "pietra"
    assert np.allclose(np.asarray(first[LINES][line["id"]]["geom"].coords), line["xyz"], atol=1e-9, rtol=0)
    assert link.write_free(ch) == []  # nothing changed: nothing written

    other = fd.add(ch, "line", [[X0, Y0, 100.0], [X0 + 1, Y0, 100.1]], name="Linea B")
    fd.update(ch, poly["id"], name="Muro grande")
    fd.remove(ch, point["id"])
    assert sorted(link.write_free(ch)) == [LINES, POLYGONS, POINTS]
    now = {t: _by_id(link.path, t) for t in (LINES, POLYGONS, POINTS)}
    kept, before = now[LINES][line["id"]], first[LINES][line["id"]]
    assert kept["fid"] == before["fid"] and kept[SIG_FIELD] == before[SIG_FIELD]  # not written again
    assert now[LINES][other["id"]]["fid"] > kept["fid"]
    assert now[POLYGONS][poly["id"]]["fid"] == first[POLYGONS][poly["id"]]["fid"]
    assert now[POLYGONS][poly["id"]]["name"] == "Muro grande"
    assert now[POLYGONS][poly["id"]][SIG_FIELD] != first[POLYGONS][poly["id"]][SIG_FIELD]
    assert now[POINTS] == {}
    assert link.read_changes() == {}  # the program's own writes are no change of another program

    for rec in fd.features(ch):  # no drawing left: no tables, as before
        fd.remove(ch, rec["id"])
    link.write_free(ch)
    assert link.free_tables() == []
    link.close()


def test_what_qgis_saves_in_the_free_tables_is_read_back_once(tmp_path):
    _doc, ch = _chunk(tmp_path)
    line, poly, point = _three(ch)
    link = _link(tmp_path, ch)
    rows = {t: _by_id(link.path, t) for t in (LINES, POLYGONS, POINTS)}
    qgis = Qgis(link.path)
    moved = LineString([[X0 + 1.0, Y0 + 1.0, 101.25], [X0 + 2.5, Y0 + 2.0, 100.9], [X0 + 3.0, Y0 + 0.5, 99.5]])
    drawn = Polygon([[X0, Y0, 100.0], [X0 + 1, Y0, 100.0], [X0 + 1, Y0 + 1, 100.0]])
    qgis.save(qgis.move(LINES, rows[LINES][line["id"]]["fid"], moved, name="Linea mossa"),
              qgis.edit(POLYGONS, rows[POLYGONS][poly["id"]]["fid"], materiale="mattone"),
              qgis.delete(POINTS, rows[POINTS][point["id"]]["fid"]),
              qgis.insert(POLYGONS, drawn, name="Fossa", layer="Fosse"))
    assert link.external_change()
    got = link.read_changes()
    assert set(got) == {LINES, POLYGONS, POINTS}
    assert [f.id for f in got[LINES].changed] == [line["id"]] and not got[LINES].added and not got[LINES].deleted
    assert got[LINES].changed[0].geometry.equals(moved) and got[LINES].changed[0].values["name"] == "Linea mossa"
    assert [f.id for f in got[POLYGONS].changed] == [poly["id"]]
    assert got[POLYGONS].changed[0].values["materiale"] == "mattone"
    new = got[POLYGONS].added[0]
    assert len(got[POLYGONS].added) == 1 and new.id and new.id != poly["id"] and new.values["name"] == "Fossa"
    assert got[POINTS].deleted == [(point["id"], 1)] and not got[POINTS].changed
    stored = [r for r in _rows(link.path, POLYGONS) if r["name"] == "Fossa"][0]
    assert stored[ID_FIELD] == new.id and stored[SIG_FIELD]  # the row drawn there has its identity
    assert link.read_changes() == {}  # given once
    qgis.close()
    link.close()


def test_the_program_never_writes_over_a_free_edit_not_read_yet(tmp_path):
    """Edited or deleted in QGIS and not read: a write of the program (its own version of the drawing changed too)
    leaves those rows as QGIS saved them, and the reader still finds them."""
    _doc, ch = _chunk(tmp_path)
    line, poly, point = _three(ch)
    link = _link(tmp_path, ch)
    rows = {t: _by_id(link.path, t) for t in (LINES, POLYGONS, POINTS)}
    qgis = Qgis(link.path)
    qgis.save(qgis.edit(LINES, rows[LINES][line["id"]]["fid"], name="Da QGIS"),
              qgis.delete(POINTS, rows[POINTS][point["id"]]["fid"]))
    fd.update(ch, line["id"], name="Dal programma")
    fd.update(ch, point["id"], name="P2")
    fd.update(ch, poly["id"], name="Muro 2")
    link.write_free(ch)
    assert _by_id(link.path, LINES)[line["id"]]["name"] == "Da QGIS"
    assert _rows(link.path, POINTS) == []  # the point deleted there is not written back
    assert _by_id(link.path, POLYGONS)[poly["id"]]["name"] == "Muro 2"  # the others are
    link.write({}, chunk=ch.key, free=ch, force=True)  # nor by a write of everything
    assert _by_id(link.path, LINES)[line["id"]]["name"] == "Da QGIS" and _rows(link.path, POINTS) == []
    got = link.read_changes()
    assert [f.values["name"] for f in got[LINES].changed] == ["Da QGIS"]
    assert got[POINTS].deleted == [(point["id"], 1)]
    assert POLYGONS not in got
    # read: the program's version goes in at the next write
    link.write_free(ch)
    assert _by_id(link.path, LINES)[line["id"]]["name"] == "Dal programma"
    assert _by_id(link.path, LINES)[line["id"]]["fid"] == rows[LINES][line["id"]]["fid"]
    assert [r["name"] for r in _rows(link.path, POINTS)] == ["P2"]
    qgis.close()
    link.close()


def test_free_edits_saved_while_the_program_was_closed_are_found(tmp_path):
    """Another link on the same file (the program started again): the signature and the record of the rows are in the
    file, so the edit, the deletion and the new drawing are found, and the first write leaves them alone."""
    _doc, ch = _chunk(tmp_path)
    line, poly, point = _three(ch)
    link = _link(tmp_path, ch)
    rows = {t: _by_id(link.path, t) for t in (LINES, POLYGONS, POINTS)}
    link.close()
    qgis = Qgis(link.path)
    qgis.save(qgis.edit(LINES, rows[LINES][line["id"]]["fid"], name="Chiuso"),
              qgis.delete(POLYGONS, rows[POLYGONS][poly["id"]]["fid"]),
              qgis.insert(POINTS, Point(X0 + 2, Y0 + 2, 100.4), name="Nuovo"))
    qgis.close()
    again = LiveLink(tmp_path / "qgis", ch.crs)
    again.write({}, chunk=ch.key, free=ch)  # the first export of the new session
    assert _by_id(again.path, LINES)[line["id"]]["name"] == "Chiuso"
    assert _rows(again.path, POLYGONS) == []
    assert len(_rows(again.path, POINTS)) == 2
    got = again.read_changes()
    assert [f.values["name"] for f in got[LINES].changed] == ["Chiuso"]
    assert got[POLYGONS].deleted == [(poly["id"], 1)]
    assert [f.values["name"] for f in got[POINTS].added] == ["Nuovo"]
    again.close()


def test_free_tables_of_an_earlier_version_are_written_again(tmp_path):
    """The tables as 1.7.7 wrote them (one-way: no signature): written again with it, nothing read from them."""
    _doc, ch = _chunk(tmp_path)
    line, _poly, _point = _three(ch)
    link = LiveLink(tmp_path / "qgis", ch.crs)
    link.write({}, chunk=ch.key)
    con = sqlite3.connect(link.path, isolation_level=None)
    con.execute("BEGIN IMMEDIATE")
    fio.write_tables(con, ch, gpkg.srs_of(CRS)[0])
    con.execute(f'UPDATE "{LINES}" SET name = ?', ("Vecchio",))
    con.execute("COMMIT")
    con.close()
    assert link.read_changes() == {}  # no signature: not two-way yet
    assert sorted(link.write_free(ch)) == [LINES, POLYGONS, POINTS]
    row = _by_id(link.path, LINES)[line["id"]]
    assert row["name"] == "Linea A" and row[SIG_FIELD]
    assert link.read_changes() == {}
    link.close()


def test_another_chunks_drawings_never_go_over_a_free_edit_of_qgis(tmp_path):
    doc, ch = _chunk(tmp_path)
    line, _poly, _point = _three(ch)
    second = doc.add_chunk("B")
    second.crs = CRS
    fd.add(second, "point", [[X0, Y0, 100.0]], name="Altro")
    link = _link(tmp_path, ch)
    fid = _by_id(link.path, LINES)[line["id"]]["fid"]
    qgis = Qgis(link.path)
    qgis.save(qgis.edit(LINES, fid, name="Da QGIS"))
    with pytest.raises(UnreadEdits):
        link.write({}, chunk=second.key, free=second)
    assert _by_id(link.path, LINES)[line["id"]]["name"] == "Da QGIS" and link.stored_chunk() == str(ch.key)
    got = link.read_changes()
    assert link.read_chunk == str(ch.key) and [f.id for f in got[LINES].changed] == [line["id"]]
    link.write({}, chunk=second.key, free=second)
    assert _rows(link.path, LINES) == [] and [r["name"] for r in _rows(link.path, POINTS)] == ["Altro"]
    qgis.close()
    link.close()


# ---------------------------------------------------------------- a row back into a drawing
def test_a_row_back_into_a_drawing_keeps_what_was_not_edited():
    old = {"id": "a", "kind": "polygon", "layer": "Muri", "name": "Muro 1", "colour": "#112233",
           "xyz": [[0.0, 0.0, 10.0], [1.0, 0.0, 10.5], [1.0, 1.0, 11.0]],
           "attributes": {"materiale": "pietra", "fase 2": "II", "n": 3, "note": ["a", "b"], "vuoto": None}}
    written = fio._row(old, fio.attribute_columns([old]))
    geom = Polygon(old["xyz"])
    same = fio.from_row("polygon", geom, dict(written), old)
    assert same["attributes"] == old["attributes"] and same["name"] == "Muro 1" and same["layer"] == "Muri"
    assert same["colour"] == "#112233" and same["xyz"] == old["xyz"] and same["dem"] == 0
    # columns edited in QGIS win; a number stays a number, a list a list, an emptied column takes the attribute away
    edited = dict(written, materiale="mattone", n="4", note='["c"]', vuoto="pieno", name="", layer=None,
                  colour="rosso", quota_min=12.5)
    got = fio.from_row("polygon", geom, edited, old)
    assert got["attributes"] == {"materiale": "mattone", "fase 2": "II", "n": 4, "note": ["c"], "vuoto": "pieno",
                                 "quota_min": 12.5}
    assert got["name"] == "Muro 1" and got["layer"] == "Muri" and got["colour"] == "#112233"
    gone = fio.from_row("polygon", geom, dict(written, materiale=None), old)
    assert "materiale" not in gone["attributes"] and gone["attributes"]["n"] == 3
    # the JSON of the attributes edited: taken, a column edited too wins over it
    text = json.dumps({"materiale": "pietra", "fase 2": "III", "n": 3})
    got = fio.from_row("polygon", geom, dict(written, attributes=text, n="7"), old)
    assert got["attributes"] == {"materiale": "pietra", "fase 2": "III", "n": 7}
    broken = fio.from_row("polygon", geom, dict(written, attributes="{non json"), old)
    assert broken["attributes"] == old["attributes"]
    # a new row: its JSON, then its own columns
    new = fio.from_row("line", LineString([[0, 0, 1.0], [1, 1, 2.0]]),
                       {"name": None, "layer": "", "attributes": None, "tipo": "canaletta", "ppg_id": "x",
                        "ppg_sig": "y", "colour": "#00ff00"})
    assert new["attributes"] == {"tipo": "canaletta"} and new["name"] == "" and new["layer"] == fd.DEFAULT_LAYER
    assert new["colour"] == "#00ff00" and new["xyz"] == [[0.0, 0.0, 1.0], [1.0, 1.0, 2.0]]


def test_a_row_back_into_a_drawing_takes_the_missing_heights_from_the_dem():
    dem = lambda xy: [50.0 + x for x, _y in xy]  # noqa: E731
    old = {"id": "a", "kind": "line", "layer": "L", "name": "Linea", "attributes": {},
           "xyz": [[0.0, 0.0, 10.0], [4.0, 0.0, 0.0]]}  # a vertex of the drawing really at 0
    values = fio._row(old, [])
    # a vertex added in QGIS (its default Z, 0) takes the DEM; those the drawing had keep theirs, moved or not
    got = fio.from_row("line", LineString([[0, 0, 10.0], [2, 0, 0.0], [4, 0, 0.0], [5, 1, 7.0]]), values, old, dem)
    assert got["xyz"] == [[0.0, 0.0, 10.0], [2.0, 0.0, 52.0], [4.0, 0.0, 0.0], [5.0, 1.0, 7.0]] and got["dem"] == 1
    # no DEM under it: interpolated along the drawing
    off = fio.from_row("line", LineString([[0, 0, 10.0], [2, 0, 0.0], [4, 0, 0.0]]), values, old, lambda xy: [None])
    assert off["xyz"][1] == [2.0, 0.0, 5.0] and off["dem"] == 0
    # drawn in QGIS without Z, or with its default: the DEM, the vertices off it interpolated
    flat = fio.from_row("line", LineString([[0, 0], [1, 0], [2, 0]]), {}, None,
                        lambda xy: [None if x == 1 else 50.0 + x for x, _y in xy])
    assert flat["xyz"] == [[0.0, 0.0, 50.0], [1.0, 0.0, 51.0], [2.0, 0.0, 52.0]] and flat["dem"] == 2
    zero = fio.from_row("point", Point(3, 0, 0.0), {}, None, dem)
    assert zero["xyz"] == [[3.0, 0.0, 53.0]] and zero["dem"] == 1
    # no heights anywhere: the zeros of the file kept, a geometry without Z refused
    assert fio.from_row("point", Point(3, 0, 0.0), {}, None, None)["xyz"] == [[3.0, 0.0, 0.0]]
    with pytest.raises(ValueError):
        fio.from_row("line", LineString([[0, 0], [1, 0]]), {}, None, lambda xy: [None] * len(xy))
    with pytest.raises(ValueError):  # not the geometry of the table
        fio.from_row("polygon", Point(3, 0, 1.0), {}, None, dem)
    # several parts: the first one, the others counted
    multi = fio.from_row("line", MultiLineString([[[0, 0, 1.0], [1, 0, 1.0]], [[5, 5, 2.0], [6, 5, 2.0]]]), {}, None)
    assert multi["xyz"] == [[0.0, 0.0, 1.0], [1.0, 0.0, 1.0]] and multi["dropped"] == 1


# ---------------------------------------------------------------- the window
def _log(w):
    t = w.log_pane.table
    return [t.item(r, 2).text() for r in range(t.rowCount())]


def _from_qgis(w):
    return [line for line in _log(w) if line.startswith("Da QGIS")]


def _free(ch):
    return {r["name"]: r for r in fd.features(ch)}


def _dem(x, y):
    return 100.0 + 0.01 * (x - X0) - 0.02 * (Y0 + 6.0 - y)  # the DEM of the trench (tests/conftest.py)


def _drawn(w):
    """A line, a polygon with attributes and a point drawn in the window, the live link on and the file written."""
    recs = _three(w.chunk)
    w._dirty(True)
    w._draw_free()
    w.a_qgis_live.setChecked(True)
    w._export_qgis_layers()
    path = w._qgis_dir() / LIVE_DB
    w._poll_qgis()  # the first look: everything there was written here
    assert _from_qgis(w) == []
    return recs, path


def test_free_drawings_saved_in_qgis_come_back_and_can_be_undone(trench):
    w, app, mw = trench
    (line, poly, point), path = _drawn(w)
    rows = {t: _by_id(path, t) for t in (LINES, POLYGONS, POINTS)}
    qgis = Qgis(path)
    moved = LineString([[X0 + 1.0, Y0 + 1.0, 101.25], [X0 + 2.5, Y0 + 2.0, 100.9], [X0 + 3.0, Y0 + 0.5, 99.5]])
    drawn = LineString([[X0 + 1, Y0 + 3, 100.2], [X0 + 2, Y0 + 3, 100.3]])
    qgis.save(qgis.move(LINES, rows[LINES][line["id"]]["fid"], moved),
              qgis.edit(POLYGONS, rows[POLYGONS][poly["id"]]["fid"], materiale="mattone", name="Muro rifatto",
                        layer="Strutture"),
              qgis.delete(POINTS, rows[POINTS][point["id"]]["fid"]),
              (f'ALTER TABLE "{LINES}" ADD COLUMN tipo TEXT', ()),  # a field added to the layer in QGIS
              qgis.insert(LINES, drawn, name="Canaletta", layer="Canalette", tipo="scolo"))
    w._poll_qgis()
    got = _free(w.chunk)
    assert set(got) == {"Linea A", "Muro rifatto", "Canaletta"}
    assert got["Linea A"]["id"] == line["id"]
    assert np.allclose(got["Linea A"]["xyz"], np.asarray(moved.coords), atol=1e-9, rtol=0)
    wall = got["Muro rifatto"]
    assert wall["id"] == poly["id"] and wall["layer"] == "Strutture"
    assert wall["attributes"] == {"materiale": "mattone", "fase 2": "II", "n": 3}
    new = got["Canaletta"]
    assert new["kind"] == "line" and new["layer"] == "Canalette" and new["attributes"] == {"tipo": "scolo"}
    assert np.allclose(new["xyz"], np.asarray(drawn.coords), atol=1e-9, rtol=0)
    stored = next(r for r in _rows(path, LINES) if r["name"] == "Canaletta")
    assert stored[ID_FIELD] == new["id"]  # the row drawn in QGIS has its identity
    assert {x["name"] for x in fd.layers(w.chunk)} >= {"Strutture", "Canalette"}
    assert sorted(_from_qgis(w)) == ["Da QGIS: disegno 3D Canaletta aggiunto al layer Canalette",
                                     "Da QGIS: disegno 3D Linea A modificato",
                                     "Da QGIS: disegno 3D Muro rifatto modificato",
                                     "Da QGIS: disegno 3D P eliminato"]
    assert w.history.undo_text() == "Da QGIS: 4 modifiche" and w.isWindowModified()
    assert len(w.ortho_view.features(mw.LAYER_PREFIX + "Canalette")) == 1  # drawn on the plan

    # the program writes its version of the rows: they keep their fid, and nothing comes back
    w._export_qgis_layers()
    after = {t: _by_id(path, t) for t in (LINES, POLYGONS, POINTS)}
    assert after[LINES][line["id"]]["fid"] == rows[LINES][line["id"]]["fid"]
    assert after[LINES][new["id"]]["fid"] == stored["fid"]
    assert after[POLYGONS][poly["id"]]["materiale"] == "mattone"
    assert json.loads(after[POLYGONS][poly["id"]]["attributes"])["materiale"] == "mattone"
    assert json.loads(after[LINES][new["id"]]["attributes"]) == {"tipo": "scolo"}
    w._poll_qgis()
    assert len(_from_qgis(w)) == 4

    w.undo()  # Modifica > Annulla takes the whole batch back
    back = _free(w.chunk)
    assert set(back) == {"Linea A", "Muro 1", "P"}
    assert back["Linea A"]["xyz"] == line["xyz"] and back["Muro 1"]["attributes"]["materiale"] == "pietra"
    w._export_qgis_layers()
    again = {t: _by_id(path, t) for t in (LINES, POLYGONS, POINTS)}
    assert set(again[LINES]) == {line["id"]} and again[LINES][line["id"]]["fid"] == rows[LINES][line["id"]]["fid"]
    assert again[POLYGONS][poly["id"]]["name"] == "Muro 1" and set(again[POINTS]) == {point["id"]}
    w._poll_qgis()
    assert len(_from_qgis(w)) == 4
    qgis.close()


def test_a_free_drawing_made_in_qgis_without_heights_takes_the_dem(trench):
    w, app, mw = trench
    (line, _poly, _point), path = _drawn(w)
    rows = _by_id(path, LINES)
    qgis = Qgis(path)
    longer = LineString([*line["xyz"][:2], [X0 + 2.75, Y0 + 1.0, 0.0], line["xyz"][2]])  # a vertex added at QGIS's 0
    qgis.save(qgis.move(LINES, rows[line["id"]]["fid"], longer),
              qgis.insert(POINTS, Point(X0 + 2.0, Y0 + 2.0), name="Senza Z"),
              qgis.insert(POINTS, Point(X0 + 3.0, Y0 + 3.0, 0.0), name="A zero"),
              qgis.insert(POINTS, Point(X0 + 50.0, Y0 + 50.0), name="Fuori"))
    w._poll_qgis()
    got = _free(w.chunk)
    assert got["Senza Z"]["xyz"][0][2] == pytest.approx(_dem(X0 + 2.0, Y0 + 2.0), abs=0.002)
    assert got["A zero"]["xyz"][0][2] == pytest.approx(_dem(X0 + 3.0, Y0 + 3.0), abs=0.002)
    xyz = got["Linea A"]["xyz"]
    assert len(xyz) == 4 and xyz[2][2] == pytest.approx(_dem(X0 + 2.75, Y0 + 1.0), abs=0.002)
    assert [p[2] for i, p in enumerate(xyz) if i != 2] == [p[2] for p in line["xyz"]]
    assert "Fuori" not in got
    assert any("Fuori" in s and "non ripreso" in s for s in _from_qgis(w))
    assert any("Senza Z" in s and "DEM" in s for s in _from_qgis(w))
    # the heights taken go back into the file, the row that could not be taken leaves it
    w._export_qgis_layers()
    points = {r["name"]: r for r in _rows(path, POINTS)}
    assert "Fuori" not in points and points["Senza Z"]["geom"].z == pytest.approx(got["Senza Z"]["xyz"][0][2])
    w._poll_qgis()
    assert not any("Fuori" in name for name in _free(w.chunk))
    qgis.close()


def test_free_edits_made_in_qgis_while_the_program_was_closed_come_back(trench):
    """The program started again (no link remembers the file): what QGIS saved meanwhile is not written over by the
    first export and comes into the window."""
    w, app, mw = trench
    (line, poly, point), path = _drawn(w)
    rows = {t: _by_id(path, t) for t in (LINES, POLYGONS, POINTS)}
    w._live_link.close()
    w._live_link = None
    qgis = Qgis(path)
    qgis.save(qgis.edit(LINES, rows[LINES][line["id"]]["fid"], name="Rinominata"),
              qgis.delete(POLYGONS, rows[POLYGONS][poly["id"]]["fid"]),
              qgis.insert(POINTS, Point(X0 + 2.0, Y0 + 2.0, 100.6), name="Chiodo"))
    qgis.close()
    assert w._export_qgis_layers()
    assert _by_id(path, LINES)[line["id"]]["name"] == "Rinominata" and _rows(path, POLYGONS) == []
    w._poll_qgis()
    got = _free(w.chunk)
    assert set(got) == {"Rinominata", "P", "Chiodo"} and got["Chiodo"]["xyz"] == [[X0 + 2.0, Y0 + 2.0, 100.6]]
    assert "Da QGIS: disegno 3D Muro 1 eliminato" in _log(w)


def test_free_edits_of_a_chunk_left_go_to_it(trench):
    """Saved in QGIS right before the active chunk changed: the edits go to the chunk the file held the drawings of,
    then the file takes the active chunk's."""
    w, app, mw = trench
    (line, _poly, point), path = _drawn(w)
    first = w.chunk
    second = w.doc.add_chunk("Saggio 2")
    second.crs = CRS
    fd.add(second, "point", [[X0 + 1, Y0 + 1, 100.0]], name="Del secondo")
    rows = {t: _by_id(path, t) for t in (LINES, POINTS)}
    qgis = Qgis(path)
    qgis.save(qgis.edit(LINES, rows[LINES][line["id"]]["fid"], name="Del primo"),
              qgis.delete(POINTS, rows[POINTS][point["id"]]["fid"]))
    qgis.close()
    w._activate_chunk(second)
    w._export_qgis_layers()
    w._poll_qgis()
    w._export_qgis_layers()
    assert set(_free(first)) == {"Del primo", "Muro 1"} and set(_free(second)) == {"Del secondo"}
    assert any("Del primo" in s and "Saggio 1" in s for s in _log(w))
    assert [r["name"] for r in _rows(path, POINTS)] == ["Del secondo"] and _rows(path, LINES) == []
