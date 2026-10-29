"""The live link with QGIS both ways: rows written in place by identity (their fid kept), the edits
QGIS saves in the GeoPackage read back into the window, undone with Modifica > Annulla, and the 3D
proxy of a unit built again when its polygon changes there. QGIS is simulated by a second sqlite3
connection editing the file, as QGIS's GeoPackage provider does when an edit session is saved."""
from __future__ import annotations

import json
import sqlite3

import pytest
import shapely
from shapely.geometry import LineString, Point, Polygon, box

from pyarchinit_photogrammetry.io import gpkg
from pyarchinit_photogrammetry.io.qgis_link import (ID_FIELD, INFO_TABLE, LIVE_DB, PART_FIELD, SIG_FIELD, LiveLink,
                                                    new_id, row_signature)

CRS = "EPSG:3004"
X0, Y0 = 2480000.0, 4500000.0  # metre coordinates as in Gauss-Boaga: float32 would round them


def _square(x, y, side=0.5):
    return ("Polygon", [[x, y], [x + side, y], [x + side, y + side], [x, y + side]])


def _layers(units, quote=(), sections=(), curves=()):
    return {"Unita' stratigrafiche": {"kind": "polygon", "features": list(units)},
            "Quote": {"kind": "point", "features": list(quote)},
            "Sezioni": {"kind": "line", "features": list(sections)},
            "Curve di livello (0.1 m)": {"kind": "line", "features": list(curves)}}


def _unit(us, x, y, pid=None, side=0.5):
    return {"etichetta": f"US {us}", "us_s": str(us), "unita_tipo_s": "US", "ppg_id": pid or new_id(),
            "_geom": _square(x, y, side)}


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


class Qgis:
    """Another program editing the GeoPackage: a connection of its own, one transaction per save."""

    def __init__(self, path):
        self.con = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.srs = self.con.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name='unita'").fetchone()[0]

    def save(self, *statements):
        self.con.execute("BEGIN IMMEDIATE")
        for sql, args in statements:
            self.con.execute(sql, args)
        self.con.execute("COMMIT")

    def move(self, table, fid, geom, **values):
        sets = "".join(f', "{k}" = ?' for k in values)
        return (f'UPDATE "{table}" SET geom = ?{sets} WHERE fid = ?', (gpkg.blob(geom, self.srs), *values.values(), fid))

    def insert(self, table, geom, **values):
        names = "".join(f', "{k}"' for k in values)
        marks = ", ?" * len(values)
        return f'INSERT INTO "{table}" (geom{names}) VALUES (?{marks})', (gpkg.blob(geom, self.srs), *values.values())

    @staticmethod
    def delete(table, fid):
        return f'DELETE FROM "{table}" WHERE fid = ?', (fid,)

    def close(self):
        self.con.close()


# ---------------------------------------------------------------- the GeoPackage
def test_rows_are_written_in_place_by_identity(tmp_path):
    """A row keeps its fid while its feature lives; only the rows that changed are written."""
    a, b, c = _unit(1, X0, Y0), _unit(2, X0 + 1, Y0), _unit(3, X0 + 2, Y0)
    curve = {"quota": 99.5, "_geom": ("LineString", [[X0, Y0], [X0 + 2, Y0 + 2]])}
    link = LiveLink(tmp_path, CRS)
    link.write(_layers([a, b, c], curves=[curve]))
    first = {r[ID_FIELD]: r for r in _rows(link.path, "unita")}
    assert set(first) == {a["ppg_id"], b["ppg_id"], c["ppg_id"]}
    assert all(r[SIG_FIELD] and r[PART_FIELD] == 1 for r in first.values())
    assert first[a["ppg_id"]]["geom"].bounds[0] == X0  # double precision, the metre coordinates kept
    curve_fid = _rows(link.path, "curve")[0]["fid"]

    b["us_s"] = "20"  # an attribute edited, a unit deleted, one drawn
    d = _unit(4, X0 + 3, Y0)
    link.write(_layers([a, b, d], curves=[curve]))
    now = {r[ID_FIELD]: r for r in _rows(link.path, "unita")}
    assert set(now) == {a["ppg_id"], b["ppg_id"], d["ppg_id"]}
    kept, before = now[a["ppg_id"]], first[a["ppg_id"]]
    assert kept["fid"] == before["fid"] and kept[SIG_FIELD] == before[SIG_FIELD]  # not written again
    assert now[b["ppg_id"]]["fid"] == first[b["ppg_id"]]["fid"] and now[b["ppg_id"]]["us_s"] == "20"
    assert now[d["ppg_id"]]["fid"] > max(r["fid"] for r in first.values())
    assert _rows(link.path, "curve")[0]["fid"] == curve_fid  # an unchanged one-way table is not touched

    # a unit of two polygons: one row per part, the same identity
    b["_geom"] = _square(X0 + 1, Y0)
    second = dict(b, _geom=_square(X0 + 1, Y0 + 1))
    link.write(_layers([a, b, second, d], curves=[curve]))
    parts = [r for r in _rows(link.path, "unita") if r[ID_FIELD] == b["ppg_id"]]
    assert [r[PART_FIELD] for r in parts] == [1, 2] and parts[0]["fid"] == first[b["ppg_id"]]["fid"]


def test_a_geopackage_of_an_earlier_version_gets_identities(tmp_path):
    """Tables written by an earlier version (no identity column) are replaced once."""
    old = tmp_path / LIVE_DB
    con = gpkg.connect(old)
    con.execute("BEGIN IMMEDIATE")
    srs = gpkg.srs_of(CRS)
    gpkg.create(con, srs)
    cols = gpkg.ensure_table(con, "unita", "POLYGON", {"layer": "TEXT", "etichetta": "TEXT", "us_s": "TEXT"}, srs[0])
    gpkg.replace_rows(con, "unita", cols, [(Polygon(_square(X0, Y0)[1]), {"layer": "x", "us_s": "1"})] * 2, srs[0])
    con.execute("COMMIT")
    con.close()
    a = _unit(1, X0, Y0)
    LiveLink(tmp_path, CRS).write(_layers([a]))
    rows = _rows(old, "unita")
    assert len(rows) == 1 and rows[0][ID_FIELD] == a["ppg_id"]


def test_what_qgis_saves_is_read_back_once(tmp_path):
    a, b, c = _unit(1, X0, Y0), _unit(2, X0 + 1, Y0), _unit(3, X0 + 2, Y0)
    q = {"quota_q": 99.25, "us_q": "1", "ppg_id": new_id(), "_geom": ("Point", [X0 + 0.2, Y0 + 0.2])}
    s = {"etichetta": "Sezione 1", "ppg_id": new_id(), "_geom": ("LineString", [[X0, Y0], [X0 + 2, Y0]])}
    link = LiveLink(tmp_path, CRS)
    link.write(_layers([a, b, c], quote=[q], sections=[s]))
    assert link.external_change()  # the first look reads everything once
    assert link.read_changes() == {}
    assert not link.external_change()
    fids = {r[ID_FIELD]: r["fid"] for r in _rows(link.path, "unita")}

    link.write(_layers([a, b, c, _unit(4, X0 + 3, Y0)], quote=[q], sections=[s]))  # the program's own write
    assert not link.external_change() and link.read_changes() == {}

    qgis = Qgis(link.path)
    moved = box(X0 + 1, Y0, X0 + 1.8, Y0 + 0.6)
    qgis.save(qgis.move("unita", fids[b["ppg_id"]], moved, us_s="6"),
              qgis.delete("unita", fids[c["ppg_id"]]),
              qgis.insert("unita", box(X0 + 5, Y0, X0 + 5.5, Y0 + 0.5), us_s="7", unita_tipo_s="USM"),
              qgis.move("quote", _rows(link.path, "quote")[0]["fid"], Point(X0 + 0.3, Y0 + 0.4), quota_q=99.4),
              (f'UPDATE "sezioni" SET etichetta = ? WHERE {ID_FIELD} = ?', ("Sezione A", s["ppg_id"])))
    assert link.external_change()
    got = link.read_changes()
    assert set(got) == {"unita", "quote", "sezioni"}
    u = got["unita"]
    assert [f.id for f in u.changed] == [b["ppg_id"]] and u.changed[0].values["us_s"] == "6"
    assert u.changed[0].geometry.equals(moved) and u.changed[0].fid == fids[b["ppg_id"]]
    assert u.deleted == [(c["ppg_id"], 1)]
    (new,) = u.added
    assert new.values["us_s"] == "7" and new.id and new.part == 1
    stored = {r["fid"]: r for r in _rows(link.path, "unita")}
    assert stored[new.fid][ID_FIELD] == new.id  # the row drawn in QGIS has its identity now
    assert got["quote"].changed[0].geometry.equals(Point(X0 + 0.3, Y0 + 0.4))
    assert got["sezioni"].changed[0].values["etichetta"] == "Sezione A"
    assert not link.external_change() and link.read_changes() == {}  # given once

    # the program brings the rows to its own version: they keep their fid and are not read back
    b2 = dict(b, us_s="6", _geom=("Polygon", [list(p) for p in moved.exterior.coords[:-1]]))
    n = dict(_unit(7, X0 + 5, Y0, pid=new.id), unita_tipo_s="USM")
    link.write(_layers([a, b2, _unit(4, X0 + 3, Y0), n], quote=[q], sections=[s]))
    after = {r[ID_FIELD]: r["fid"] for r in _rows(link.path, "unita")}
    assert after[b["ppg_id"]] == fids[b["ppg_id"]] and after[new.id] == new.fid
    assert not link.external_change()
    qgis.close()


def test_the_program_never_writes_over_an_edit_not_read_yet(tmp_path):
    a, b = _unit(1, X0, Y0), _unit(2, X0 + 1, Y0)
    link = LiveLink(tmp_path, CRS)
    link.write(_layers([a, b]))
    link.read_changes()
    fid = next(r["fid"] for r in _rows(link.path, "unita") if r[ID_FIELD] == a["ppg_id"])
    qgis = Qgis(link.path)
    qgis.save(qgis.move("unita", fid, box(X0, Y0, X0 + 3, Y0 + 3)),
              qgis.insert("unita", box(X0 + 9, Y0, X0 + 9.5, Y0 + 0.5), us_s="9"))
    a["us_s"] = "10"  # the program changes the same unit before it has looked at the file
    link.write(_layers([a]), force=True)  # and drops b
    rows = {r["fid"]: r for r in _rows(link.path, "unita")}
    assert rows[fid]["geom"].equals(box(X0, Y0, X0 + 3, Y0 + 3)) and rows[fid]["us_s"] == "1"  # QGIS's edit stays
    assert any(r["us_s"] == "9" for r in rows.values())  # and the unit drawn there
    assert b["ppg_id"] not in {r[ID_FIELD] for r in rows.values()}
    got = link.read_changes()["unita"]
    assert [f.fid for f in got.changed] == [fid] and len(got.added) == 1
    qgis.close()


def test_a_deletion_in_qgis_is_never_undone_by_a_write(tmp_path):
    """A unit deleted in QGIS while the program draws (its write comes before its next look at the
    file): the row is not written again, and the reader still gives the deletion."""
    a, b = _unit(1, X0, Y0), _unit(2, X0 + 1, Y0)
    link = LiveLink(tmp_path, CRS)
    link.write(_layers([a, b]))
    fid = next(r["fid"] for r in _rows(link.path, "unita") if r[ID_FIELD] == b["ppg_id"])
    qgis = Qgis(link.path)
    qgis.save(qgis.delete("unita", fid))
    c = _unit(3, X0 + 2, Y0)
    link.write(_layers([a, b, c]))  # a unit drawn here, b still in the window
    link.write(_layers([a, b, c]), force=True)
    assert {r[ID_FIELD] for r in _rows(link.path, "unita")} == {a["ppg_id"], c["ppg_id"]}
    got = link.read_changes()["unita"]
    assert got.deleted == [(b["ppg_id"], 1)] and not got.changed and not got.added
    link.write(_layers([a, c]))  # the window without it: nothing more
    assert link.read_changes() == {} and len(_rows(link.path, "unita")) == 2
    qgis.close()


def test_a_copy_made_in_qgis_is_a_feature_of_its_own(tmp_path):
    a = _unit(1, X0, Y0)
    link = LiveLink(tmp_path, CRS)
    link.write(_layers([a]))
    link.read_changes()
    (row,) = _rows(link.path, "unita")
    qgis = Qgis(link.path)  # copy and paste: every attribute copied, the identity and its signature too
    qgis.save(qgis.insert("unita", box(X0 + 4, Y0, X0 + 4.5, Y0 + 0.5), us_s="1", **{
        ID_FIELD: row[ID_FIELD], SIG_FIELD: row[SIG_FIELD], PART_FIELD: 1}))
    got = link.read_changes()["unita"]
    assert not got.changed and len(got.added) == 1 and got.added[0].id != a["ppg_id"]
    assert got.added[0].fid != row["fid"]
    qgis.close()


def test_a_row_under_an_identity_never_known_here_is_added_as_it_is(tmp_path):
    """Features pasted in QGIS from another project with their identity, never signed by this program - one unit of
    one polygon, one of two (as a unit drawn in RustGIS as two polygons comes): added under that identity, every row of
    it, kept and signed; a new part of a unit known here is a change of that unit, and so is a unit known here whose
    row was replaced by an unsigned one under its identity."""
    a, b = _unit(1, X0, Y0), _unit(2, X0 + 1, Y0)
    link = LiveLink(tmp_path, CRS)
    link.write(_layers([a, b]))
    link.read_changes()
    qgis = Qgis(link.path)
    other, pair = new_id(), new_id()
    qgis.save(qgis.insert("unita", box(X0 + 4, Y0, X0 + 4.5, Y0 + 0.5), us_s="5", **{ID_FIELD: other, PART_FIELD: 1}),
              qgis.insert("unita", box(X0 + 6, Y0, X0 + 6.5, Y0 + 0.5), us_s="6", **{ID_FIELD: pair, PART_FIELD: 1}),
              qgis.insert("unita", box(X0 + 7, Y0, X0 + 7.5, Y0 + 0.5), us_s="6", **{ID_FIELD: pair, PART_FIELD: 2}),
              qgis.insert("unita", box(X0, Y0 + 2, X0 + 0.5, Y0 + 2.5), us_s="1",
                          **{ID_FIELD: a["ppg_id"], PART_FIELD: 2}))
    got = link.read_changes()["unita"]
    assert sorted((f.id, f.part) for f in got.added) == sorted([(other, 1), (pair, 1), (pair, 2)])
    assert [(f.id, f.part) for f in got.changed] == [(a["ppg_id"], 2)] and not got.deleted
    stored = {r["fid"]: r for r in _rows(link.path, "unita")}
    assert all(stored[f.fid][ID_FIELD] == f.id and stored[f.fid][SIG_FIELD] for f in got.added)
    assert link.read_changes() == {}  # given once
    qgis.save(qgis.move("unita", next(f.fid for f in got.added if f.id == other), box(X0 + 4, Y0, X0 + 5, Y0 + 1)))
    assert [(f.id, f.part) for f in link.read_changes()["unita"].changed] == [(other, 1)]  # known now: a change
    old = next(r["fid"] for r in _rows(link.path, "unita") if r[ID_FIELD] == b["ppg_id"])
    qgis.save(qgis.delete("unita", old),
              qgis.insert("unita", box(X0 + 1, Y0, X0 + 1.8, Y0 + 0.8), us_s="2",
                          **{ID_FIELD: b["ppg_id"], PART_FIELD: 3}))
    got = link.read_changes()["unita"]
    assert [(f.id, f.part) for f in got.changed] == [(b["ppg_id"], 3)] and not got.added
    assert got.deleted == [(b["ppg_id"], 1)]
    qgis.close()


def test_a_unit_pasted_in_qgis_with_its_identity_comes_into_the_window(trench):
    """Through the window: a unit pasted with its identity as two polygons, and a spot height pasted with its own,
    come in as one unit and one spot height, under those identities."""
    w, app, mw = trench
    w.a_qgis_live.setChecked(True)
    w._export_qgis_layers()
    path = w._qgis_dir() / LIVE_DB
    w._poll_qgis()
    unit, spot = new_id(), new_id()
    qgis = Qgis(path)
    qgis.save(qgis.insert("unita", box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2), us_s="21", area_s="1",
                          **{ID_FIELD: unit, PART_FIELD: 1}),
              qgis.insert("unita", box(X0 + 3, Y0 + 1, X0 + 3.5, Y0 + 1.5), us_s="21", area_s="1",
                          **{ID_FIELD: unit, PART_FIELD: 2}),
              qgis.insert("quote", Point(X0 + 0.5, Y0 + 0.5), quota_q=100.5, us_q="21", **{ID_FIELD: spot}))
    w._poll_qgis()
    (u,) = [u for u in w.units if u.us == "21"]
    assert u.attributes[ID_FIELD] == unit and u.geometry.geom_type == "MultiPolygon" and len(u.geometry.geoms) == 2
    assert [(q[ID_FIELD], q["quota_q"]) for q in w.quote] == [(spot, 100.5)]
    assert sorted(_from_qgis(w)) == ["Da QGIS: US 21 aggiunta", "Da QGIS: quota 100.50 aggiunta"]
    qgis.close()


def test_a_locked_file_is_read_again_later(tmp_path):
    link = LiveLink(tmp_path, CRS)
    a = _unit(1, X0, Y0)
    link.write(_layers([a]))
    link.read_changes()
    fid = _rows(link.path, "unita")[0]["fid"]
    qgis = Qgis(link.path)
    qgis.save(qgis.move("unita", fid, box(X0, Y0, X0 + 2, Y0 + 2)))
    qgis.con.execute("BEGIN IMMEDIATE")  # QGIS is saving again: the file is held
    assert link.external_change()
    with pytest.raises(sqlite3.OperationalError):
        link.read_changes(busy_ms=50)
    qgis.con.execute("COMMIT")
    assert link.external_change()  # nothing was lost: read at the next look
    assert len(link.read_changes()["unita"].changed) == 1
    qgis.close()


def test_a_table_in_another_crs_is_refused(tmp_path):
    link = LiveLink(tmp_path, CRS)
    link.write(_layers([_unit(1, X0, Y0)]))
    link.read_changes()
    qgis = Qgis(link.path)
    qgis.save(("UPDATE gpkg_geometry_columns SET srs_id = 4326 WHERE table_name = 'unita'", ()))
    got = link.read_changes()
    assert got["unita"].refused == (4326, 3004) and not got["unita"].changed
    qgis.close()


def test_after_a_change_of_system_the_tables_are_written_in_the_new_one(tmp_path):
    """The chunk changed its system (EPSG:32633 into EPSG:32632, another zone of UTM): the GeoPackage QGIS reads is
    written in the new one - every table made again in it, its rows in it, the system listed - and what QGIS saves
    after it is read back. Before, the tables kept the old system: QGIS drew the converted drawings in it, about 500 km
    off, and its edits were refused."""
    from pyproj import Transformer

    from pyarchinit_photogrammetry.io.qgis_link import LIVE_TABLES

    to = Transformer.from_crs("EPSG:32633", "EPSG:32632", always_xy=True).transform
    x, y = 438000.0, 4553000.0
    unit = _unit(1, x, y)
    spot = {"quota_q": 120.5, "us_q": "1", "ppg_id": new_id(), "_geom": ("Point", [x, y])}
    old = LiveLink(tmp_path, "EPSG:32633")
    old.write(_layers([unit], quote=[spot]))
    assert old.read_changes() == {}
    old.close()
    # the drawings converted by the window, written by the link of the new system (the window makes a new one)
    moved = dict(unit, _geom=("Polygon", [list(to(px, py)) for px, py in unit["_geom"][1]]))
    moved_spot = dict(spot, _geom=("Point", list(to(x, y))))
    link = LiveLink(tmp_path, "EPSG:32632")
    link.write(_layers([moved], quote=[moved_spot]))
    con = sqlite3.connect(link.path)
    try:
        for table in LIVE_TABLES:
            assert gpkg.table_srs(con, table) == 32632, table
            assert con.execute("SELECT srs_id FROM gpkg_contents WHERE table_name = ?", (table,)).fetchone() == (32632,)
        assert con.execute("SELECT count(*) FROM gpkg_spatial_ref_sys WHERE srs_id = 32632").fetchone() == (1,)
        assert [gpkg.blob_srs(r[0]) for r in con.execute('SELECT geom FROM "unita"')] == [32632]
        assert [gpkg.blob_srs(r[0]) for r in con.execute('SELECT geom FROM "quote"')] == [32632]
    finally:
        con.close()
    (row,) = _rows(link.path, "unita")
    assert row[ID_FIELD] == unit["ppg_id"]
    assert row["geom"].equals_exact(Polygon(moved["_geom"][1]), 1e-6)
    (point,) = _rows(link.path, "quote")
    assert (point["geom"].x, point["geom"].y) == pytest.approx(to(x, y)) and point["quota_q"] == 120.5
    assert link.read_changes() == {}  # nothing refused, nothing taken for a change of QGIS
    qgis = Qgis(link.path)
    assert qgis.srs == 32632
    nx, ny = to(x, y)
    qgis.save(qgis.move("unita", row["fid"], box(nx, ny, nx + 1.0, ny + 1.0)))
    got = link.read_changes()
    assert got["unita"].refused is None and [f.id for f in got["unita"].changed] == [unit["ppg_id"]]
    qgis.close()


def test_a_table_written_whole_in_another_system_is_made_again(tmp_path):
    """A table that :func:`gpkg.write_table` writes whole, in a GeoPackage written before in another system (QGIS had
    given it a spatial index): made again in the new system, the index and its metadata gone with it; other tables
    kept. A table added to (``ensure_table`` without ``recreate``, as an archive does) keeps its rows and its system."""
    path = tmp_path / "zone.gpkg"
    gpkg.write_table(path, "zone", "POLYGON", {"zona": "INTEGER"}, [(box(0, 0, 1, 1), {"zona": 1})], "EPSG:32633")
    gpkg.write_table(path, "altro", "POINT", {"n": "INTEGER"}, [(Point(5, 5), {"n": 1})], "EPSG:32633")
    con = sqlite3.connect(path, isolation_level=None)
    con.execute('CREATE TABLE "rtree_zone_geom" (id INTEGER PRIMARY KEY, minx, maxx, miny, maxy)')  # as GDAL's
    con.execute("CREATE TABLE gpkg_extensions (table_name TEXT, column_name TEXT, extension_name TEXT NOT NULL, "
                "definition TEXT NOT NULL, scope TEXT NOT NULL)")
    con.execute("INSERT INTO gpkg_extensions VALUES ('zone', 'geom', 'gpkg_rtree_index', 'GeoPackage 1.0', "
                "'write-only')")
    con.close()
    gpkg.write_table(path, "zone", "POLYGON", {"zona": "INTEGER"}, [(box(2, 2, 3, 3), {"zona": 2})], "EPSG:32632")
    con = sqlite3.connect(path, isolation_level=None)
    try:
        assert gpkg.table_srs(con, "zone") == 32632 and gpkg.table_srs(con, "altro") == 32633
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "rtree_zone_geom" not in names and "altro" in names
        assert con.execute("SELECT count(*) FROM gpkg_extensions").fetchone() == (0,)
        assert con.execute("SELECT zona FROM zone").fetchall() == [(2,)]
        con.execute("BEGIN IMMEDIATE")  # adding to a table: its rows and its system stay
        gpkg.create(con, gpkg.srs_of("EPSG:32632"))
        gpkg.ensure_table(con, "altro", "POINT", {"n": "INTEGER"}, 32632)
        con.execute("COMMIT")
        assert gpkg.table_srs(con, "altro") == 32633 and con.execute("SELECT n FROM altro").fetchall() == [(1,)]
    finally:
        con.close()


def test_signatures_do_not_depend_on_empty_columns():
    raw = shapely.to_wkb(LineString([(0, 0), (1, 1)]), byte_order=1, output_dimension=2)
    assert row_signature(raw, {"a": 1.5, "b": None}) == row_signature(raw, {"a": 1.5, "nuova": None})
    assert row_signature(raw, {"a": 1.5}) != row_signature(raw, {"a": 1.25})
    assert row_signature(raw, {"a": 1.5, ID_FIELD: "x"}) == row_signature(raw, {"a": 1.5, ID_FIELD: "y"})


# ---------------------------------------------------------------- the window
def _log(w):
    t = w.log_pane.table
    return [t.item(r, 2).text() for r in range(t.rowCount())]


def _from_qgis(w):
    return [line for line in _log(w) if line.startswith("Da QGIS")]


def _units(w):
    return {u.us: u for u in w.units}


def test_units_saved_in_qgis_come_back_and_can_be_undone(trench):
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    w, app, mw = trench
    w._add_unit(UnitPolygon(box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2), us="6", area="1", sito="Saggio"))
    w._add_unit(UnitPolygon(box(X0 + 3, Y0 + 1, X0 + 4, Y0 + 2), us="8", area="1", sito="Saggio"))
    six = w.units[0].geometry
    w.a_qgis_live.setChecked(True)
    w._export_qgis_layers()
    path = w._qgis_dir() / LIVE_DB
    w._poll_qgis()  # the first look: everything there was written here
    assert _from_qgis(w) == []
    rows = {r["us_s"]: r for r in _rows(path, "unita")}
    assert rows["6"][ID_FIELD] == w.units[0].attributes[ID_FIELD]

    qgis = Qgis(path)
    grown = box(X0 + 1, Y0 + 1, X0 + 2.5, Y0 + 2)
    qgis.save(qgis.move("unita", rows["6"]["fid"], grown, disegnatore="E. C."),
              qgis.delete("unita", rows["8"]["fid"]),
              qgis.insert("unita", box(X0 + 4.5, Y0 + 4, X0 + 5, Y0 + 4.5), us_s="9", area_s="1", unita_tipo_s="USM"))
    w._poll_qgis()
    units = _units(w)
    assert set(units) == {"6", "9"}
    assert units["6"].geometry.equals(grown) and units["6"].attributes["disegnatore"] == "E. C."
    assert units["9"].tipo == "USM" and units["9"].sito == "Saggio"
    new = next(r for r in _rows(path, "unita") if r["us_s"] == "9")
    assert units["9"].attributes[ID_FIELD] == new[ID_FIELD]  # the row drawn in QGIS has its identity
    assert sorted(_from_qgis(w)) == ["Da QGIS: US 6 modificata", "Da QGIS: US 8 eliminata", "Da QGIS: USM 9 aggiunta"]
    assert w.history.undo_text() == "Da QGIS: 3 modifiche" and w.isWindowModified()
    stored = {d["us"] for d in w.chunk.meta["drawings"]["units"]}
    assert stored == {"6", "9"}  # the drawings of the chunk
    assert len(w.ortho_view.features("Unita' stratigrafiche")) == 2  # drawn again

    # the program writes its version of the rows: they keep their fid, and nothing comes back
    w._export_qgis_layers()
    after = {r["us_s"]: r for r in _rows(path, "unita")}
    assert after["6"]["fid"] == rows["6"]["fid"] and after["9"]["fid"] == new["fid"]
    assert after["9"]["layer"] == "Unita' stratigrafiche" and after["9"]["etichetta"] == "USM 9"
    w._poll_qgis()
    assert len(_from_qgis(w)) == 3

    w.undo()  # Modifica > Annulla takes the whole batch back
    units = _units(w)
    assert set(units) == {"6", "8"} and units["6"].geometry.equals(six)
    w._export_qgis_layers()
    back = {r["us_s"]: r for r in _rows(path, "unita")}
    assert set(back) == {"6", "8"} and back["6"]["fid"] == rows["6"]["fid"] and back["6"]["geom"].equals(six)
    w._poll_qgis()
    assert len(_from_qgis(w)) == 3
    qgis.close()


def test_spot_heights_sections_and_bad_geometries(trench):
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    w, app, mw = trench
    w._add_unit(UnitPolygon(box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2), us="6", area="1", sito="Saggio"))
    w._add_unit(UnitPolygon(box(X0 + 3, Y0 + 1, X0 + 4, Y0 + 2), us="8", area="1", sito="Saggio"))
    q = {"x": X0 + 0.5, "y": Y0 + 0.5, "quota_q": 100.12, "us_q": "6", "sito_q": "Saggio"}
    w.quote.append(q)
    w._draw_quota(q, 0)
    w.sections = [{"label": "Sezione 1", "p1": [X0 + 0.5, Y0 + 3.0], "p2": [X0 + 5.5, Y0 + 3.0], "length": 5.0,
                   "svg": "", "dxf": "", "dxf3d": "", "kind": "vertical"},
                  {"label": "Sezione 2", "p1": [X0 + 0.5, Y0 + 5.0], "p2": [X0 + 5.5, Y0 + 5.0], "length": 5.0,
                   "svg": "", "dxf": "", "dxf3d": "", "kind": "vertical"}]
    w._ensure_ppg_ids()
    w._draw_sections()
    w.a_qgis_live.setChecked(True)
    w._export_qgis_layers()
    path = w._qgis_dir() / LIVE_DB
    w._poll_qgis()
    units = {r["us_s"]: r for r in _rows(path, "unita")}
    quota = _rows(path, "quote")[0]
    secs = {r["etichetta"]: r for r in _rows(path, "sezioni")}
    assert quota[ID_FIELD] == q[ID_FIELD] and set(secs) == {"Sezione 1", "Sezione 2"}

    qgis = Qgis(path)
    bowtie = Polygon([(X0 + 1, Y0 + 1), (X0 + 2, Y0 + 2), (X0 + 2, Y0 + 1), (X0 + 1, Y0 + 2)])  # self-crossing
    assert not bowtie.is_valid
    qgis.save(qgis.move("unita", units["6"]["fid"], bowtie),
              ('UPDATE "unita" SET geom = NULL WHERE fid = ?', (units["8"]["fid"],)),  # a feature left without geometry
              qgis.move("quote", quota["fid"], Point(X0 + 0.7, Y0 + 0.6), quota_q=100.2),
              qgis.move("sezioni", secs["Sezione 1"]["fid"], LineString([(X0 + 0.5, Y0 + 3.0), (X0 + 5.5, Y0 + 3.5)]),
                        etichetta="Sezione A"),
              qgis.delete("sezioni", secs["Sezione 2"]["fid"]))
    w._poll_qgis()
    units = _units(w)
    assert units["6"].geometry.is_valid and units["6"].geometry.geom_type == "MultiPolygon"  # made valid
    assert units["8"].geometry.equals(box(X0 + 3, Y0 + 1, X0 + 4, Y0 + 2))  # kept as it was
    assert (w.quote[0]["x"], w.quote[0]["y"], w.quote[0]["quota_q"]) == (round(X0 + 0.7, 3), round(Y0 + 0.6, 3), 100.2)
    assert [s["label"] for s in w.sections] == ["Sezione A"] and w.sections[0]["p2"] == [X0 + 5.5, Y0 + 3.5]
    assert [s["label"] for s in w.sections_pane.sections] == ["Sezione A"]
    lines = _log(w)
    assert "Da QGIS: la geometria di US 6 non era valida ed è stata corretta" in lines
    assert "Da QGIS: US 8 senza geometria leggibile, non ripresa" in lines
    assert "Da QGIS: quota 100.20 modificata" in lines and "Da QGIS: Sezione 2 eliminata" in lines
    # no model in this project: the trace is kept, its drawing not made again
    assert any("Sezione A: la traccia è cambiata ma il disegno non è stato rifatto" in t for t in lines)
    assert json_sections(w) == ["Sezione A"]
    qgis.close()


def json_sections(w):
    import json

    return [s["label"] for s in json.loads(w._sections_file().read_text(encoding="utf-8"))]


def test_a_unit_drawn_in_qgis_reaches_the_window_by_itself(trench):
    import time

    w, app, mw = trench
    w.a_qgis_live.setChecked(True)
    w._export_qgis_layers()
    path = w._qgis_dir() / LIVE_DB
    qgis = Qgis(path)
    qgis.save(qgis.insert("unita", box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2), us_s="12", area_s="2"))
    end = time.monotonic() + 5
    while time.monotonic() < end and not w.units:  # the window looks once a second
        app.processEvents()
        time.sleep(0.05)
    assert [u.us for u in w.units] == ["12"] and "Da QGIS: US 12 aggiunta" in _log(w)
    fid = _rows(path, "unita")[0]["fid"]
    qgis.save(qgis.move("unita", fid, box(X0 + 1, Y0 + 1, X0 + 2.5, Y0 + 2)))
    qgis.con.execute("BEGIN IMMEDIATE")  # and holds the file again at once: the window waits, without errors
    w._poll_qgis()
    assert w.units[0].geometry.equals(box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2))
    qgis.con.execute("COMMIT")
    w._poll_qgis()
    assert w.units[0].geometry.equals(box(X0 + 1, Y0 + 1, X0 + 2.5, Y0 + 2))
    qgis.close()


def test_the_proxy_follows_its_unit(trench, monkeypatch):
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    w, app, mw = trench
    w._add_unit(UnitPolygon(box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2), us="6", area="1", sito="Saggio"))
    answers = {"units": "drawn", "base": "plane", "z": 99.0, "thickness": 0.1, "dem": ""}
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, parent, title, fields, intro="": dict(answers)))
    w.create_proxies()
    w.runner.wait()
    app.processEvents()
    (e,) = w.chunk.meta["proxies"]
    assert e["base"] == "plane" and e["base_z"] == 99.0 and e["ppg_id"] == w.units[0].attributes[ID_FIELD]
    volume, area = e["volume_m3"], e["area_m2"]
    w.a_qgis_live.setChecked(True)
    w._export_qgis_layers()
    path = w._qgis_dir() / LIVE_DB
    w._poll_qgis()
    qgis = Qgis(path)
    fid = _rows(path, "unita")[0]["fid"]

    qgis.save(qgis.move("unita", fid, box(X0 + 1, Y0 + 1, X0 + 3, Y0 + 2)))  # twice as large
    w._poll_qgis()
    w.runner.wait()
    app.processEvents()
    (e,) = w.chunk.meta["proxies"]
    assert e["area_m2"] == pytest.approx(2 * area, rel=0.05) and e["volume_m3"] > 1.8 * volume
    assert any(t.startswith("Proxy US6 ricostruito sul nuovo poligono") for t in _log(w))

    qgis.save(qgis.move("unita", fid, box(X0 + 1, Y0 + 1, X0 + 3, Y0 + 2), us_s="7"))  # renumbered only
    w._poll_qgis()
    (e,) = w.chunk.meta["proxies"]
    assert e["name"] == "US7" and e["us"] == "7" and (w.chunk.data_dir / e["file"]).name == "US7.ply"
    assert (w.chunk.data_dir / e["file"]).exists() and not (w.chunk.data_dir / "proxies" / "US6.ply").exists()

    # a proxy of an earlier version, its plane not kept: asked once, then kept
    del e["base_z"]
    asked = []
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(
        lambda cls, parent, title, fields, intro="": asked.append(title) or {"z": 98.5}))
    qgis.save(qgis.move("unita", fid, box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2)))
    w._poll_qgis()
    w.runner.wait()
    app.processEvents()
    (e,) = w.chunk.meta["proxies"]
    assert asked == ["Proxy di US7"] and e["base_z"] == 98.5
    assert e["z_bottom"][0] == pytest.approx(98.5) and e["area_m2"] == pytest.approx(area, rel=0.05)
    qgis.close()


def test_a_section_moved_in_qgis_is_cut_again(processed, tmp_path):
    """On the processed trench (a model to cut): the trace moved in QGIS, the section drawn again."""
    import os
    import time
    from pathlib import Path

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    import numpy as np
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import pyarchinit_photogrammetry as ppg

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc = ppg.Document.open(processed[0].path)
    doc.save(tmp_path / "copia.ppg")  # a copy: the processed project is shared by the tests
    doc.chunk.meta.pop("drawings", None)
    doc.chunk.meta.pop("section_options", None)  # cut from the model, whatever another test chose
    doc.meta["sito"] = "Saggio"
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.show()
    app.processEvents()
    try:
        w._section_drawn(np.array([X0 + 1.0, Y0 + 1.5]), np.array([X0 + 3.0, Y0 + 1.5]))
        w.runner.wait()
        app.processEvents()
        (s,) = w.sections
        svg, before = Path(s["svg"]), Path(s["svg"]).stat().st_mtime_ns
        assert 1.9 < s["length"] < 2.1
        w.a_qgis_live.setChecked(True)
        w._export_qgis_layers()
        path = w._qgis_dir() / LIVE_DB
        w._poll_qgis()
        qgis = Qgis(path)
        (row,) = _rows(path, "sezioni")
        time.sleep(0.01)
        qgis.save(qgis.move("sezioni", row["fid"], LineString([(X0 + 1.0, Y0 + 1.0), (X0 + 3.0, Y0 + 2.5)])))
        w._poll_qgis()
        w.runner.wait()
        app.processEvents()
        (s,) = w.sections
        assert s["p1"] == [X0 + 1.0, Y0 + 1.0] and s["p2"] == [X0 + 3.0, Y0 + 2.5]
        assert s["length"] == pytest.approx(2.5, abs=0.05)  # cut again along the new trace
        assert Path(s["svg"]) == svg and svg.stat().st_mtime_ns > before
        assert "Sezione 1: sezione ricalcolata sulla nuova traccia" in _log(w)
        assert "Da QGIS: Sezione 1 modificata" in _log(w)
        qgis.close()

        # Annulla puts the trace back, and its drawing is cut again along it; Ripeti the other way
        w.a_qgis_live.setChecked(False)  # the queue of the window does it, not the look at QGIS
        moved = svg.stat().st_mtime_ns
        time.sleep(0.01)
        w.undo()
        w.runner.wait()
        app.processEvents()
        (s,) = w.sections
        assert s["p1"] == [X0 + 1.0, Y0 + 1.5] and s["p2"] == [X0 + 3.0, Y0 + 1.5]
        assert 1.9 < s["length"] < 2.1 and svg.stat().st_mtime_ns > moved
        w.redo()
        w.runner.wait()
        app.processEvents()
        (s,) = w.sections
        assert s["p2"] == [X0 + 3.0, Y0 + 2.5] and s["length"] == pytest.approx(2.5, abs=0.05)
        assert _log(w).count("Sezione 1: sezione ricalcolata sulla nuova traccia") == 3
    finally:
        w.setWindowModified(False)
        w.close()


def test_the_file_keeps_the_key_of_its_chunk(tmp_path):
    """The live GeoPackage keeps the key of the chunk whose drawings it holds, written with them (only when it changes:
    the same chunk written again changes nothing for QGIS); what QGIS saved is read with the key the file had then."""
    link = LiveLink(tmp_path, CRS)
    assert link.stored_chunk() is None
    six = _unit(6, X0, Y0)
    link.write(_layers([six]), chunk=1)
    assert link.stored_chunk() == "1"
    other = sqlite3.connect(link.path)  # another program reading the file
    try:
        assert other.execute(f"SELECT value FROM {INFO_TABLE} WHERE name = 'chunk'").fetchone() == ("1",)
        before = other.execute("PRAGMA data_version").fetchone()[0]
        link.write(_layers([six]), chunk=1)  # the same chunk, the same drawings: nothing written
        assert other.execute("PRAGMA data_version").fetchone()[0] == before
        link.write(_layers([six]), chunk=2)  # only the key changes: written
        assert other.execute("PRAGMA data_version").fetchone()[0] != before and link.stored_chunk() == "2"
    finally:
        other.close()
    qgis = Qgis(link.path)
    qgis.save(qgis.insert("unita", box(X0 + 4, Y0 + 4, X0 + 5, Y0 + 5), us_s="12"))
    qgis.close()
    assert link.read_changes()["unita"].added and link.read_chunk == "2"
    link.close()


def test_qgis_edits_of_a_chunk_left_go_to_it(trench):
    """QGIS alone: a unit drawn in QGIS and saved right before the active chunk changes - not read yet - goes into the
    chunk it was drawn on, never into the active one, and only then the new chunk's drawings go into the file. For a
    chunk removed before its edits were read they go nowhere: kept in a file beside the project, said in the log."""
    w, app, mw = trench
    first = w.chunk
    second = w.doc.add_chunk("Saggio 2")
    second.crs = first.crs
    w.a_qgis_live.setChecked(True)
    w._qgis_poll.stop()  # the looks are the test's
    w._export_qgis_layers()
    w._poll_qgis()
    path = w._qgis_dir() / LIVE_DB
    qgis = Qgis(path)
    qgis.save(qgis.insert("unita", box(X0 + 1, Y0 + 1, X0 + 2, Y0 + 2), us_s="12", area_s="1"))
    w._activate_chunk(second)
    assert w._export_qgis_layers()  # the edits read first, into their chunk
    assert [d["us"] for d in first.meta["drawings"]["units"]] == ["12"] and w.units == []
    assert "Da QGIS: US 12 aggiunta, nel chunk Saggio 1" in _log(w)
    assert w._live_link.stored_chunk() == str(second.key) and _rows(path, "unita") == []
    w._poll_qgis()
    assert w.units == []
    # a unit saved in QGIS for the second chunk, which is removed before the window reads it
    qgis.save(qgis.insert("unita", box(X0 + 3, Y0 + 1, X0 + 4, Y0 + 2), us_s="13", area_s="1"))
    assert w.remove_workspace_items([("chunk", second, None)], confirm=False)
    w.runner.wait()
    assert w.chunk is first and w._export_qgis_layers()
    assert sorted(u.us for u in w.units) == ["12"]
    kept = sorted(w.doc.path.parent.glob(f"{w.doc.path.stem}_modifiche_chunk_{second.key}_*.geojson"))
    assert len(kept) == 1
    features = json.loads(kept[0].read_text(encoding="utf-8"))["features"]
    assert [(f["properties"]["us_s"], f["properties"]["modifica"]) for f in features] == [("13", "aggiunta")]
    assert any("non è più nel progetto" in line and kept[0].name in line for line in _log(w))
    assert w._live_link.stored_chunk() == str(first.key)
    qgis.close()
