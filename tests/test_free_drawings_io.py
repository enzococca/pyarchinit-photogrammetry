"""Free drawings in 3D out of the program and back: DXF 3D, GeoPackage with Z, GeoJSON and CSV, imported again (z from
the DEM when a file has none), the one-way tables of the live link with QGIS and RustGIS, the sheet of the tables and
*File > Esporta > Disegni 3D...*."""
from __future__ import annotations

import csv
import json
import os
import sqlite3

import numpy as np
import pytest

from pyarchinit_photogrammetry.core import free_drawings as fd
from pyarchinit_photogrammetry.io import free_drawings as fio

X0, Y0 = 2480000.0, 4500000.0


def _chunk(tmp_path, crs="EPSG:3004"):
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    ch = doc.add_chunk("A")
    ch.crs = crs
    doc.save(tmp_path / "progetto.ppg")
    return doc, ch


def _three(ch):
    """A line and a point in the default layer, a polygon in "Muri" with attributes (one with a name not fit for a
    column)."""
    line = fd.add(ch, "line", [[X0 + 0.123456789, Y0 + 1.0, 101.25], [X0 + 2.5, Y0 + 1.75, 100.875],
                               [X0 + 3.0, Y0 + 0.5, 99.5]], name="Linea A")
    poly = fd.add(ch, "polygon", [[X0, Y0, 100.0], [X0 + 1.0, Y0, 100.5], [X0 + 1.0, Y0 + 1.0, 101.0],
                                  [X0, Y0 + 1.0, 100.25]], layer="Muri", name="Muro 1",
                  attributes={"materiale": "pietra", "fase 2": "II", "n": "3"})
    point = fd.add(ch, "point", [[X0 + 5.0, Y0 + 5.0, 102.125]], name="P")
    return [line, poly, point]


def _same(got, want, tol):
    by_id = {r["id"]: r for r in got}
    assert set(by_id) == {r["id"] for r in want}
    for r in want:
        g = by_id[r["id"]]
        assert g["kind"] == r["kind"] and g["layer"] == r["layer"] and g["name"] == r["name"]
        assert g["attributes"] == r["attributes"]
        assert np.allclose(np.asarray(g["xyz"], float), np.asarray(r["xyz"], float), atol=tol, rtol=0)


@pytest.mark.parametrize("fmt", ["gpkg", "geojson", "dxf"])
def test_round_trip_dxf_gpkg_geojson_keeps_z(tmp_path, fmt):
    _doc, ch = _chunk(tmp_path)
    want = _three(ch)
    out = fio.export(ch, tmp_path / f"disegni.{fmt}")
    assert out.exists()
    got = fio.read(out)
    _same(got, want, 1e-6 if fmt == "dxf" else 1e-9)
    if fmt == "gpkg":
        con = sqlite3.connect(out)
        rows = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT table_name, geometry_type_name, z FROM gpkg_geometry_columns")}
        con.close()
        # the base types with z = 1 (GeoPackage, Annex E), as the elevations' 3D tables
        assert rows == {"disegni_3d_linee": ("LINESTRING", 1), "disegni_3d_poligoni": ("POLYGON", 1),
                        "disegni_3d_punti": ("POINT", 1)}
        con = sqlite3.connect(out)
        cols = [r[1] for r in con.execute('PRAGMA table_info("disegni_3d_poligoni")')]
        con.close()
        assert {"ppg_id", "layer", "name", "attributes", "materiale", "n"} <= set(cols) and "fase 2" not in cols
    # one layer only
    only = fio.export(ch, tmp_path / f"muri.{fmt}", layer="Muri")
    assert [r["id"] for r in fio.read(only)] == [want[1]["id"]]


def test_csv_and_formats(tmp_path):
    _doc, ch = _chunk(tmp_path)
    want = _three(ch)
    out = fio.export(ch, tmp_path / "vertici.csv")
    with out.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["id", "layer", "kind", "name", "vertex", "x", "y", "z"]
    assert len(rows) == 1 + 3 + 4 + 1
    first = next(r for r in rows[1:] if r[0] == want[0]["id"] and r[4] == "1")
    assert float(first[5]) == want[0]["xyz"][0][0] and float(first[7]) == want[0]["xyz"][0][2]
    assert fio.FORMATS == ("dxf", "gpkg", "geojson", "csv")
    with pytest.raises(ValueError):
        fio.export(ch, tmp_path / "x.txt")
    assert fio.export(ch, tmp_path / "y.bin", fmt="geojson").exists()


def test_import_without_z_takes_the_dem(tmp_path):
    from pyarchinit_photogrammetry.engine import dem as dem_mod

    _doc, ch = _chunk(tmp_path)
    n, res = 40, 0.1
    xs = X0 + (np.arange(n) + 0.5) * res
    ys = Y0 + n * res - (np.arange(n) + 0.5) * res
    gx, gy = np.meshgrid(xs, ys)
    ground = (100.0 + 0.2 * (gx - X0) + 0.1 * (gy - Y0)).astype(np.float32)
    dem = dem_mod.write_geotiff(ch.data_dir / "dem.tif", ground, (X0, Y0 + n * res), res, ch.crs,
                                nodata=dem_mod.NODATA)
    ch._set_product("dem", dem)
    feats = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "senza quota", "tipo": "muro"},
         "geometry": {"type": "LineString", "coordinates": [[X0 + 0.55, Y0 + 0.55], [X0 + 1e4, Y0 + 1.0],
                                                            [X0 + 2.05, Y0 + 2.05]]}},
        {"type": "Feature", "properties": {"name": "fuori"},
         "geometry": {"type": "Point", "coordinates": [X0 - 50.0, Y0 - 50.0]}},
        {"type": "Feature", "properties": {"name": "con quota", "layer": "Rilievo"},
         "geometry": {"type": "Polygon", "coordinates": [[[X0, Y0, 1.0], [X0 + 1, Y0, 2.0], [X0 + 1, Y0 + 1, 3.0],
                                                          [X0, Y0, 1.0]]]}}]}
    path = tmp_path / "scavo_2024.geojson"
    path.write_text(json.dumps(feats), encoding="utf-8")
    got = fio.import_features(ch, path)
    assert got == {"added": 2, "dem_z": 1, "skipped": 1, "holes_dropped": 0}
    recs = {r["name"]: r for r in fd.features(ch)}
    line = recs["senza quota"]
    assert line["layer"] == "scavo_2024" and line["attributes"] == {"tipo": "muro"}
    import rasterio

    with rasterio.open(ch.dem_path) as src:
        za, zc = (float(v[0]) for v in src.sample([(X0 + 0.55, Y0 + 0.55), (X0 + 2.05, Y0 + 2.05)]))
    xyz = np.asarray(line["xyz"])
    assert abs(xyz[0, 2] - za) < 1e-6 and abs(xyz[2, 2] - zc) < 1e-6
    d1, d2 = np.hypot(*(xyz[1, :2] - xyz[0, :2])), np.hypot(*(xyz[2, :2] - xyz[1, :2]))
    assert abs(xyz[1, 2] - (za + (zc - za) * d1 / (d1 + d2))) < 1e-6
    assert recs["con quota"]["layer"] == "Rilievo" and len(recs["con quota"]["xyz"]) == 3
    assert [p[2] for p in recs["con quota"]["xyz"]] == [1.0, 2.0, 3.0]
    # the same file again: the ids are new where they exist already, a layer given wins
    out = fio.export(ch, tmp_path / "giro.gpkg")
    before = {r["id"] for r in fd.features(ch)}
    again = fio.import_features(ch, out, layer="Copia")
    assert again["added"] == 2 and again["dem_z"] == 0
    copies = [r for r in fd.features(ch) if r["layer"] == "Copia"]
    assert len(copies) == 2 and not ({r["id"] for r in copies} & before)


def test_import_refused_when_the_heights_would_come_from_a_dem_to_rebuild(tmp_path):
    """A DEM built in another system of the chunk (an orientation since): no heights taken from it, nothing added;
    drawings with their own heights still come in."""
    from pyarchinit_photogrammetry.core import stamps
    from pyarchinit_photogrammetry.engine import dem as dem_mod

    _doc, ch = _chunk(tmp_path)
    ground = np.full((10, 10), 100.0, np.float32)
    ch._set_product("dem", dem_mod.write_geotiff(ch.data_dir / "dem.tif", ground, (X0, Y0 + 1.0), 0.1, ch.crs,
                                                 nodata=dem_mod.NODATA))
    stamps.stamp(ch, "dem")
    t = np.eye(4)
    t[:3, 3] = [2.0, -1.0, 0.5]
    ch.transform = t @ np.asarray(ch.transform, float)
    assert stamps.is_stale(ch, "dem")
    flat = tmp_path / "piatti.geojson"
    flat.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "senza quota"},
         "geometry": {"type": "Point", "coordinates": [X0 + 0.5, Y0 + 0.5]}}]}), encoding="utf-8")
    with pytest.raises(fio.StaleDem):
        fio.import_features(ch, flat)
    assert not fd.features(ch)
    with_z = tmp_path / "quote.geojson"
    with_z.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "con quota"},
         "geometry": {"type": "Point", "coordinates": [X0 + 0.5, Y0 + 0.5, 99.0]}}]}), encoding="utf-8")
    assert fio.import_features(ch, with_z)["added"] == 1


def test_geojson_crs_member(tmp_path):
    _doc, ch = _chunk(tmp_path, crs="EPSG:3004")
    _three(ch)
    data = json.loads(fio.export(ch, tmp_path / "a.geojson").read_text(encoding="utf-8"))
    assert data["crs"] == {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3004"}}
    assert len(data["features"][0]["geometry"]["coordinates"][0]) == 3
    local = tmp_path / "locale"
    local.mkdir()
    _doc2, ch2 = _chunk(local, crs="LOCAL")
    _three(ch2)
    data = json.loads(fio.export(ch2, local / "b.geojson").read_text(encoding="utf-8"))
    assert "crs" not in data


def test_live_link_tables(tmp_path):
    from pyarchinit_photogrammetry.io.qgis_link import LiveLink

    _doc, ch = _chunk(tmp_path)
    want = _three(ch)
    link = LiveLink(tmp_path / "qgis", ch.crs)
    link.write({}, chunk=ch.key)  # the GeoPackage of the drawings as the window makes it
    written = link.write_free(ch)
    assert sorted(written) == sorted(t for t, _g in fio.TABLES.values())
    assert sorted(t for t, _g in link.free_tables()) == sorted(written)
    _same(fio.read(link.path), want, 1e-9)
    assert link.write_free(ch) == []  # unchanged: nothing written
    fd.update(ch, want[0]["id"], name="Nuovo nome")
    assert link.write_free(ch)  # changed: written again
    names = {r["name"] for r in fio.read(link.path)}
    assert "Nuovo nome" in names
    vectors = link.free_vectors()
    assert [v["name"] for v in vectors] == ["Disegni 3D - linee (ppg)", "Disegni 3D - poligoni (ppg)",
                                            "Disegni 3D - punti (ppg)"]
    assert all(v["path"] == str(link.path) for v in vectors)
    link.close()


def test_tables_sheet(tmp_path):
    from pyarchinit_photogrammetry.io.tables import project_tables

    _doc, ch = _chunk(tmp_path)
    want = _three(ch)
    en = project_tables(ch, language="en")["3D drawings"]
    assert en.headers[:4] == ["ID", "Layer", "Kind", "Name"]
    rows = {r[0]: r for r in en.rows}
    assert set(rows) == {r["id"] for r in want}
    line, poly = rows[want[0]["id"]], rows[want[1]["id"]]
    assert line[4] == 3 and line[5] == pytest.approx(
        float(np.sum(np.linalg.norm(np.diff(np.asarray(want[0]["xyz"]), axis=0), axis=1))))
    assert poly[6] is not None and poly[7] == pytest.approx(1.0)
    assert "Disegni 3D" in project_tables(ch, language="it")


def test_export_menu_writes_the_file(qapp_env, processed, tmp_path, monkeypatch):
    from orient_helpers import close_window, window

    from pyarchinit_photogrammetry.gui import main_window as mwmod

    w = window(processed, tmp_path, monkeypatch)
    try:
        out = tmp_path / "esportati.dxf"
        monkeypatch.setattr(mwmod.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
        monkeypatch.setattr(mwmod.QInputDialog, "getItem", staticmethod(lambda *a, **k: (a[3][0], True)))
        w.a_export_free.trigger()
        assert out.exists()
        assert {r["id"] for r in fio.read(out)} == {r["id"] for r in fd.features(w.chunk)}
        # imported back: new ids, one step of Modifica > Annulla
        monkeypatch.setattr(mwmod.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: (str(fio.export(w.chunk, tmp_path / "a.gpkg")), "")))
        n = len(fd.features(w.chunk))
        w.a_import_free.trigger()
        assert len(fd.features(w.chunk)) == 2 * n
        assert w.history.undo() is not None
        assert len(fd.features(w.chunk)) == n
        # the 2D layers of the free drawings never go out through the generic tables of the live link
        written = {}
        from pyarchinit_photogrammetry.io.qgis_link import LiveLink

        monkeypatch.setattr(LiveLink, "write", lambda self, layers, **k: written.update(layers) or {})
        w._show_raster("dem")
        assert any(n.startswith(mwmod.LAYER_PREFIX) for n in w.ortho_view.layers)
        w._export_qgis_layers(force=True)
        assert written and not any(n.startswith(mwmod.LAYER_PREFIX) for n in written)
    finally:
        close_window(w)


@pytest.fixture
def qapp_env():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_import_counts_holes_reads_flat_cad_and_keeps_new_ids(tmp_path, caplog):
    import ezdxf

    _doc, ch = _chunk(tmp_path)
    ring = [[X0, Y0, 1.0], [X0 + 4, Y0, 1.0], [X0 + 4, Y0 + 4, 1.0], [X0, Y0 + 4, 1.0], [X0, Y0, 1.0]]
    hole = [[X0 + 1, Y0 + 1, 1.0], [X0 + 2, Y0 + 1, 1.0], [X0 + 2, Y0 + 2, 1.0], [X0 + 1, Y0 + 1, 1.0]]
    path = tmp_path / "muro.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"ppg_id": "abc", "name": "con buco"},
         "geometry": {"type": "Polygon", "coordinates": [ring, hole]}}]}), encoding="utf-8")
    got = fio.import_features(ch, path)
    assert got["added"] == 1 and got["holes_dropped"] == 1
    assert any("buchi" in r.getMessage() for r in caplog.records)
    assert fd.features(ch)[0]["id"] == "abc"  # new to the chunk: the file's identity kept
    with pytest.raises(ValueError):
        fd.add(ch, "point", [[0, 0, 0]], id="abc")
    # a flat CAD drawing: a 2D polyline and points at 0 m in layer "0" take no height from the file
    dxf = ezdxf.new("R2010")
    msp = dxf.modelspace()
    msp.add_polyline2d([(X0, Y0), (X0 + 1, Y0 + 1)])
    msp.add_point((X0 + 2, Y0 + 2, 0.0))
    cad = tmp_path / "rilievo_cad.dxf"
    dxf.saveas(cad)
    recs = fio.read(cad)
    assert len(recs) == 2 and not any(r["has_z"] for r in recs)
    assert all(r["layer"] is None for r in recs)  # the layer "0" is no user layer: the file's name
    assert fio.import_features(ch, cad)["skipped"] == 2  # no DEM: nothing to take the heights from


def test_gpkg_keeps_the_own_colour_and_json_attributes(tmp_path):
    _doc, ch = _chunk(tmp_path)
    rec = fd.add(ch, "point", [[X0, Y0, 1.0]], colour="#123456", attributes={"lista": [1, 2], "ok": True})
    out = fio.export(ch, tmp_path / "c.gpkg")
    got = fio.read(out)[0]
    assert got["colour"] == "#123456" and got["attributes"] == rec["attributes"]
    con = sqlite3.connect(out)
    (lista, ok), = con.execute('SELECT lista, ok FROM "disegni_3d_punti"').fetchall()
    con.close()
    assert json.loads(lista) == [1, 2] and json.loads(ok) is True
