"""Measuring points and the readout under the mouse (engine.probe): the values of every product of a
chunk at a point, read at full resolution - elevation, temperature and its anomaly, reflectance of
each band, indices - rounded to what they mean, and written to a GeoPackage, a CSV or a GeoJSON
that QGIS opens with their reference system."""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine.probe import Probe, write_points

X0, Y0, CELL = 400000.0, 4500000.0, 0.05


def _raster(path, bands, crs="EPSG:32633", descriptions=None):
    import rasterio
    from rasterio.transform import from_origin

    arr = np.asarray(bands, np.float32)
    if arr.ndim == 2:
        arr = arr[None]
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[1], width=arr.shape[2], count=arr.shape[0],
                       dtype="float32", crs=crs, transform=from_origin(X0, Y0, CELL, CELL), nodata=float("nan")) as dst:
        dst.write(arr)
        for i, d in enumerate(descriptions or []):
            dst.set_band_description(i + 1, d)


@pytest.fixture
def chunk(tmp_path):
    doc = ppg.Document()
    ch = doc.add_chunk("Campo")
    doc.save(tmp_path / "campo.ppg")
    ch.crs = "EPSG:32633"
    yy, xx = np.mgrid[0:40, 0:60].astype(np.float32)
    dem = 150.0 + 0.01 * xx + 0.02 * yy
    _raster(ch.data_dir / "dem.tif", dem)
    ch._set_product("dem", ch.data_dir / "dem.tif")
    t = 15.0 + 0.1 * xx
    t[0, 0] = np.nan
    _raster(ch.data_dir / "multispectral" / "thermal.tif", t)
    _raster(ch.data_dir / "multispectral" / "thermal_anomaly.tif", t - np.nanmean(t))
    refl = np.stack([0.05 + 0 * xx, 0.08 + 0.001 * yy, 0.30 + 0.002 * xx])
    _raster(ch.data_dir / "multispectral" / "reflectance.tif", refl, descriptions=["Green", "Red", "NIR"])
    ndvi = (refl[2] - refl[1]) / (refl[2] + refl[1])
    _raster(ch.data_dir / "multispectral" / "index_NDVI.tif", ndvi)
    ch.meta["thermal"] = {"file": "multispectral/thermal.tif", "anomaly": {"file": "multispectral/thermal_anomaly.tif"}}
    ch.meta["multispectral"] = {"bands": ["Green", "Red", "NIR", "LWIR"], "reflectance": "multispectral/reflectance.tif",
                                "reflectance_bands": ["Green", "Red", "NIR"],
                                "indices": {"NDVI": {"file": "multispectral/index_NDVI.tif"}}}
    return ch, dem, t, refl, ndvi


def test_values_at_a_point(chunk):
    ch, dem, t, refl, ndvi = chunk
    with Probe(ch) as probe:
        assert probe.fields == ["z", "temp_c", "anomaly_c", "refl_green", "refl_red", "refl_nir", "ndvi"]
        r, c = 12, 33
        x, y = X0 + (c + 0.5) * CELL, Y0 - (r + 0.5) * CELL
        v = probe.at(x, y)
        assert v["z"] == pytest.approx(dem[r, c]) and v["temp_c"] == pytest.approx(t[r, c])
        assert v["refl_nir"] == pytest.approx(refl[2, r, c]) and v["ndvi"] == pytest.approx(ndvi[r, c])
        assert set(probe.at(x, y, ["temp_c"])) == {"temp_c"}  # only the files asked for are read
        assert "temp_c" not in probe.at(X0 + 0.5 * CELL, Y0 - 0.5 * CELL)  # an empty cell: no temperature
        assert probe.at(X0 - 10, Y0) == {}  # outside every product
        rec = probe.record(x, y, label="P1")
        assert rec["x"] == round(x, 3) and rec["temp_c"] == round(float(t[r, c]), 2) and rec["label"] == "P1"
        assert rec["refl_red"] == round(float(refl[1, r, c]), 4)
        text = probe.text(v, ["z", "temp_c", "anomaly_c", "ndvi"])
        assert text.startswith(f"Z {dem[r, c]:.3f}  T {t[r, c]:.2f} °C  ΔT ") and "NDVI " in text
        assert probe.fields_of(ch.data_dir / "multispectral" / "index_NDVI.tif") == ["ndvi"]
        # while a task may write the products: the same values, no file left open (Windows does not
        # replace or delete a file held open)
        assert probe._open
        probe.keep_open = False
        assert probe.at(x, y) == v and not probe._open


def test_points_for_qgis(chunk, tmp_path):
    ch, *_ = chunk
    with Probe(ch) as probe:
        pts = [probe.record(X0 + 1.0, Y0 - 0.5, label="P1"), probe.record(X0 + 2.0, Y0 - 1.0, label="P2")]
    out = write_points(pts, tmp_path / "punti.gpkg", ch.crs)
    con = sqlite3.connect(out)
    try:
        rows = con.execute("SELECT label, z, temp_c, ndvi FROM punti_misura ORDER BY label").fetchall()
        srs = con.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name = 'punti_misura'").fetchone()[0]
    finally:
        con.close()
    assert [r[0] for r in rows] == ["P1", "P2"] and rows[0][2] == pts[0]["temp_c"] and srs == 32633
    csv_out = write_points(pts, tmp_path / "punti.csv", ch.crs)
    lines = csv_out.read_text().splitlines()
    assert lines[0] == "# EPSG:32633" and lines[1].startswith("x,y,") and len(lines) == 4
    gj = json.loads(write_points(pts, tmp_path / "punti.geojson", ch.crs).read_text())
    assert gj["crs"]["properties"]["name"] == "EPSG:32633" and len(gj["features"]) == 2
    assert gj["features"][0]["geometry"]["coordinates"][2] == pts[0]["z"]
