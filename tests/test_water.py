"""Water level and refraction (engine/water): a synthetic pond - a DEM of a basin whose 0.80 m contour is the edge of
the water, an orthophoto blue inside it and brown outside, a dense cloud of the bottom seen through the water. The
coordinates are a synthetic origin, no real place."""
from __future__ import annotations

import cv2
import numpy as np
import pytest
from shapely.geometry import Point
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.core import versions
from pyarchinit_photogrammetry.core.pointcloud import PointCloud
from pyarchinit_photogrammetry.engine import water
from pyarchinit_photogrammetry.engine.colour_mask import HsvRange
from pyarchinit_photogrammetry.engine.dem import NODATA, write_geotiff

CRS = "EPSG:3004"
X0, Y0 = 2480000.0, 4500000.0  # the centre of the pond
SIDE = 6.0
DEM_CELL = 0.01
ORTHO_CELL = 0.02
LEVEL = 0.80
RADIUS = 2.0  # where the basin is at LEVEL
WATER_RANGE = HsvRange((170.0, 210.0), (40.0, 100.0), (30.0, 100.0))


def basin(x, y):
    return 0.6 + 0.05 * ((x - X0) ** 2 + (y - Y0) ** 2)  # 0.80 at RADIUS, 0.2 m/m of slope there


def _grid(cell):
    n = int(round(SIDE / cell))
    rr, cc = np.mgrid[0:n, 0:n]
    x = X0 - SIDE / 2 + (cc + 0.5) * cell
    y = Y0 + SIDE / 2 - (rr + 0.5) * cell
    return x, y


def project(tmp_path, shift=(X0, Y0, 0.0)):
    """A project with one chunk: the DEM of the basin, the orthophoto and a dense cloud (internal coordinates =
    world - ``shift``)."""
    import rasterio
    from rasterio.transform import from_origin

    doc = ppg.Document()
    a = doc.add_chunk("A")
    a.crs = CRS
    t = np.eye(4)
    t[:3, 3] = shift
    a.transform = t
    doc.save(tmp_path / "p.ppg")
    x, y = _grid(DEM_CELL)
    origin = (X0 - SIDE / 2, Y0 + SIDE / 2)
    a._set_product("dem", write_geotiff(a.data_dir / "dem.tif", basin(x, y).astype(np.float32), origin, DEM_CELL,
                                        CRS, nodata=NODATA))
    x, y = _grid(ORTHO_CELL)
    wet = basin(x, y) < LEVEL
    hsv = np.zeros(x.shape + (3,), np.float32)
    hsv[...] = (30.0, 0.45, 0.55)
    hsv[wet] = (190.0, 0.7, 0.6)
    rgb = (cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB) * 255.0 + 0.5).astype(np.uint8)
    img = np.concatenate([rgb, np.full(x.shape + (1,), 255, np.uint8)], axis=2)
    path = a.data_dir / "orthomosaic.tif"
    with rasterio.open(path, "w", driver="GTiff", width=img.shape[1], height=img.shape[0], count=4, dtype="uint8",
                       crs=CRS, transform=from_origin(*origin, ORTHO_CELL, ORTHO_CELL), photometric="RGB",
                       BIGTIFF="IF_SAFER") as dst:
        dst.colorinterp = [rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
                           rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha]
        dst.write(np.moveaxis(img, -1, 0))
    a._set_product("orthomosaic", path)
    x, y = _grid(0.05)
    world = np.column_stack([x.ravel(), y.ravel(), basin(x, y).ravel()])
    normals = np.tile(np.array([[0.0, 0.0, 1.0]], np.float32), (len(world), 1))
    a.point_cloud = PointCloud(a.to_internal(world), np.full((len(world), 3), 128, np.uint8), normals)
    doc.save()
    return doc, a


def test_corrected_formula():
    assert water.corrected(np.array([9.0, 10.5]), 10.0, 1.34).tolist() == pytest.approx([8.66, 10.5])


def test_level_from_the_edge_of_a_pond(tmp_path):
    _doc, a = project(tmp_path)
    r = water.level_from_edge(a, Point(X0, Y0).buffer(RADIUS, quad_segs=64))
    assert r["level"] == pytest.approx(LEVEL, abs=0.01)
    assert r["edge_cells"] > 1000
    assert r["spread"] < 0.005 and r["p10"] <= r["level"] <= r["p90"]
    assert (r["x"], r["y"]) == pytest.approx((X0, Y0), abs=0.01)
    assert "water" not in a.meta  # only measured
    # the same from the colours of the orthophoto
    r2 = water.level_from_edge(a, WATER_RANGE)
    assert r2["level"] == pytest.approx(LEVEL, abs=0.01)
    rec = water.set_level(a, r2.pop("level"), source="edge", n=water.N_FRESH, **r2)
    assert rec["source"] == "edge" and rec["n"] == water.N_FRESH and rec["built_in"] is not None
    lv = water.level_of(a)
    assert lv == {"level": pytest.approx(rec["level"]), "n": water.N_FRESH, "source": "edge",
                  "spread": pytest.approx(r2["spread"])}
    # an island in the pond: its shore is land too (inside the hole)
    ring = Point(X0, Y0).buffer(RADIUS, quad_segs=64).difference(Point(X0 + 1.0, Y0).buffer(0.3))
    assert water.level_from_edge(a, ring)["level"] == pytest.approx(LEVEL, abs=0.01)


def test_no_water_or_edge_outside_the_dem_is_an_error_and_writes_nothing(tmp_path):
    doc, a = project(tmp_path)
    reds = HsvRange((345.0, 15.0), (50.0, 100.0), (30.0, 100.0))  # no red in the orthophoto
    with pytest.raises(ValueError) as e:
        water.level_from_edge(a, reds)
    assert getattr(e.value, "italian", "")  # said in both languages
    far = Point(X0 + 100.0, Y0).buffer(RADIUS)
    with pytest.raises(water.WaterError, match="outside the DEM") as e:
        water.level_from_edge(a, far)
    assert "fuori dal DEM" in e.value.italian
    with pytest.raises(water.WaterError):
        water.level_from_edge(a, Point(X0, Y0).buffer(0.0))
    assert "water" not in a.meta
    runner = CliRunner()
    for args in (["--hue", "345,15", "--sat", "50,100"], ["--layer", "nessuno"]):
        res = runner.invoke(app, ["water", "level", str(tmp_path / "p.ppg"), "A", *args])
        assert res.exit_code == 1, res.output
        assert "error:" in res.output
    again = ppg.Document.open(tmp_path / "p.ppg")
    assert "water" not in again.chunks[0].meta
    with pytest.raises(water.WaterError, match="no water level"):
        water.correct_cloud(a)
    with pytest.raises(water.WaterError, match="refractive index"):
        water.set_level(a, 0.8, n=2.5)
    assert "water" not in a.meta


def test_level_moves_with_the_chunk(tmp_path):
    _doc, a = project(tmp_path)
    water.set_level(a, LEVEL, source="hand")
    assert water.level_of(a)["level"] == pytest.approx(LEVEL)
    t = np.array(a.transform, float)
    t[2, 3] += 0.3
    a.transform = t
    assert water.level_of(a)["level"] == pytest.approx(LEVEL + 0.3)
    # a small tilt about the x axis: the point of the water (the centre of the DEM) is followed
    c, s = np.cos(0.01), np.sin(0.01)
    rot = np.array([[1, 0, 0, 0], [0, c, -s, 0], [0, s, c, 0], [0, 0, 0, 1.0]])
    a.transform = t @ rot
    inner = np.linalg.solve(t, [X0, Y0, LEVEL + 0.3, 1.0])
    assert water.level_of(a)["level"] == pytest.approx(float((t @ rot @ inner)[2]))


def test_correct_cloud_is_a_version(tmp_path):
    doc, a = project(tmp_path)
    before = a.point_cloud
    water.set_level(a, LEVEL, source="hand", n=water.N_SEA)
    r = water.correct_cloud(a)
    assert r["name"] == "Nuvola corretta per la rifrazione (n 1,34)"
    assert r["note"] == water.NOTE and r["dem"] is None
    world0 = a.to_world(before.xyz)
    after = a.point_cloud
    world1 = a.to_world(after.xyz)
    below = world0[:, 2] < LEVEL
    assert r["below"] == int(below.sum()) > 0 and r["points"] == len(before)
    assert np.allclose(world1[:, 2], water.corrected(world0[:, 2], LEVEL, 1.34), atol=1e-9)
    assert np.allclose(world1[:, :2], world0[:, :2], atol=1e-9)
    deepest = world0[:, 2].argmin()
    assert world1[deepest, 2] == pytest.approx(LEVEL - 1.34 * (LEVEL - world0[deepest, 2]))
    assert np.allclose(np.linalg.norm(after.normals, axis=1), 1.0, atol=1e-5)
    kept = versions.versions(a, "point_cloud")
    assert [e["id"] for e in kept] == [r["version"]]
    assert versions.active_info(a, "point_cloud")["name"] == r["name"]
    assert a.meta["dense"]["refraction"]["n"] == pytest.approx(1.34)
    assert a.meta["water"]["correction"]["below"] == r["below"]
    with pytest.raises(water.WaterError, match="already corrected"):
        water.correct_cloud(a)
    doc.save()
    # undo: the cloud kept made active again
    versions.activate(a, "point_cloud", r["version"])
    assert np.array_equal(a.point_cloud.xyz, before.xyz)
    assert not (a.meta.get("dense") or {}).get("refraction")
    assert water.correct_cloud(a, n=water.N_FRESH)["below"] == r["below"]  # it can be corrected again


def test_points_above_the_level_untouched(tmp_path):
    _doc, a = project(tmp_path)
    before = a.point_cloud
    water.set_level(a, LEVEL, source="hand")
    water.correct_cloud(a)
    after = a.point_cloud
    above = a.to_world(before.xyz)[:, 2] >= LEVEL
    assert above.sum() > 0
    assert np.array_equal(after.xyz[above], before.xyz[above])
    assert np.array_equal(after.normals[above], before.normals[above])
    assert np.array_equal(after.rgb, before.rgb)
    # a level below every point: nothing to correct, nothing written
    water.set_level(a, -5.0, source="hand")
    n_versions = len(versions.versions(a, "point_cloud"))
    a.meta["dense"].pop("refraction")
    with pytest.raises(water.WaterError, match="no point"):
        water.correct_cloud(a)
    assert len(versions.versions(a, "point_cloud")) == n_versions


def test_cli_water_level_and_correct(tmp_path):
    project(tmp_path)
    p = str(tmp_path / "p.ppg")
    runner = CliRunner()
    res = runner.invoke(app, ["water", "show", p, "A"])
    assert res.exit_code == 0 and "no water level" in res.output
    res = runner.invoke(app, ["water", "level", p, "A", "--sample", f"{X0},{Y0}", "--fresh"])
    assert res.exit_code == 0, res.output
    assert "water level 0.80" in res.output and "n 1.33" in res.output and "DEM cells" in res.output
    res = runner.invoke(app, ["water", "level", p, "A", "--set", "0.75", "--sea"])
    assert res.exit_code == 0, res.output
    assert "water level 0.750 m (hand), n 1.34" in res.output
    res = runner.invoke(app, ["water", "level", p, "A", "--set", "0.75", "--sea", "--fresh"])
    assert res.exit_code != 0
    res = runner.invoke(app, ["water", "level", p, "A"])
    assert res.exit_code != 0
    res = runner.invoke(app, ["water", "correct", p, "A", "--dem"])
    assert res.exit_code == 0, res.output
    assert "corrected with n 1.34" in res.output and "paraxial" in res.output and "DEM built again" in res.output
    doc = ppg.Document.open(p)
    a = doc.chunks[0]
    assert len(versions.versions(a, "point_cloud")) == 1 and len(versions.versions(a, "dem")) == 1
    assert versions.active_info(a, "dem")["name"] == "DEM dalla nuvola corretta (n 1,34)"
    res = runner.invoke(app, ["water", "show", p, "A"])
    assert res.exit_code == 0 and "water level 0.750 m (hand)" in res.output and "corrected" in res.output


def test_a_failed_dem_undoes_the_whole_correction(tmp_path, monkeypatch):
    _doc, a = project(tmp_path)
    before = a.point_cloud
    water.set_level(a, LEVEL, source="hand")
    dem_before = a.dem_path.read_bytes()

    def broken(*args, **kw):
        raise RuntimeError("no DEM today")

    monkeypatch.setattr(a, "build_dem", broken)
    with pytest.raises(RuntimeError, match="no DEM today"):
        water.correct_cloud(a, rebuild_dem=True)
    assert np.array_equal(a.point_cloud.xyz, before.xyz)
    assert versions.versions(a, "point_cloud") == [] and versions.versions(a, "dem") == []
    assert a.dem_path.read_bytes() == dem_before
    assert not (a.meta.get("dense") or {}).get("refraction") and "correction" not in a.meta["water"]
