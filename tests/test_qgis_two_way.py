"""The live GeoPackage of the QGIS link both ways: rows written in place by identity (their fid
kept), and the edits another program saves in it read back. QGIS is simulated by a second sqlite3
connection editing the file, as QGIS's GeoPackage provider does when an edit session is saved."""
from __future__ import annotations

import sqlite3

import pytest
import shapely
from shapely.geometry import LineString, Point, Polygon, box

from pyarchinit_photogrammetry.io import gpkg
from pyarchinit_photogrammetry.io.qgis_link import (ID_FIELD, LIVE_DB, PART_FIELD, SIG_FIELD, LiveLink, new_id,
                                                    row_signature)

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


def test_signatures_do_not_depend_on_empty_columns():
    raw = shapely.to_wkb(LineString([(0, 0), (1, 1)]), byte_order=1, output_dimension=2)
    assert row_signature(raw, {"a": 1.5, "b": None}) == row_signature(raw, {"a": 1.5, "nuova": None})
    assert row_signature(raw, {"a": 1.5}) != row_signature(raw, {"a": 1.25})
    assert row_signature(raw, {"a": 1.5, ID_FIELD: "x"}) == row_signature(raw, {"a": 1.5, ID_FIELD: "y"})

