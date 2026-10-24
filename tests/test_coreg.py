"""Coregistration of DEMs: a survey shifted, raised and tilted, with a pit dug in between, moved back
onto the first over the ground that did not change."""
from __future__ import annotations

import numpy as np
import pytest
import rasterio
from shapely.geometry import Point

from pyarchinit_photogrammetry.archaeo import coreg
from pyarchinit_photogrammetry.archaeo.terrain import volume
from pyarchinit_photogrammetry.engine.dem import NODATA, write_geotiff

RES = 0.01  # a 5 x 5 m square, cells of 1 cm
SIZE = 5.0
X0, Y1 = 500000.0, 4500005.0
XC, YC = X0 + SIZE / 2, Y1 - SIZE / 2
DX, DY, DZ = 0.07, -0.04, 0.015  # the second survey shows the ground 7 cm east, 4 cm south and 1.5 cm higher
TX, TY = 0.0015, -0.001  # and tilted (m per m)
PIT = (XC + 1.0, YC - 0.6, 0.5, 0.1, 0.3)  # centre, radius, width of the wall, depth (m)


def ground(x, y):
    """Rough ground with slopes in every direction: waves of 0.4-3 m running every way."""
    rng = np.random.default_rng(7)
    z = 100.0 + 0.04 * (x - XC) + 0.02 * (y - YC)
    for lam in np.geomspace(0.4, 3.0, 12):
        ang, phase = rng.uniform(0, 2 * np.pi, 2)
        k = 2 * np.pi / lam
        z = z + 0.12 / k * np.sin(k * (np.cos(ang) * (x - XC) + np.sin(ang) * (y - YC)) + phase)
    return z


def pit(x, y):
    """The depth of the pit dug between the two surveys (sloping walls)."""
    cx, cy, r, wall, depth = PIT
    t = np.clip((r - np.hypot(x - cx, y - cy)) / wall, 0, 1)
    return depth * t * t * (3 - 2 * t)


def pit_volume() -> float:
    step = 0.0025
    gx, gy = np.meshgrid(np.arange(PIT[0] - 0.6, PIT[0] + 0.6, step) + step / 2,
                         np.arange(PIT[1] - 0.6, PIT[1] + 0.6, step) + step / 2)
    return float(pit(gx, gy).sum() * step * step)


def cells(res=RES, origin=(X0, Y1), size=SIZE):
    n = int(round(size / res))
    rows, cols = np.mgrid[0:n, 0:n]
    return origin[0] + (cols + 0.5) * res, origin[1] - (rows + 0.5) * res


def write_pair(folder, surface=ground, res=RES, res2=None, origin2=(X0, Y1), noise=0.001, dug=True, shift=(DX, DY),
               dz=DZ, tilt=(TX, TY)):
    """The reference DEM and the second survey: the same ground (with the pit dug, ``dug``) seen
    ``shift`` further, ``dz`` higher and tilted, with noise; returns their paths."""
    x, y = cells(res)
    first = write_geotiff(folder / "first.tif", surface(x, y).astype(np.float32), (X0, Y1), res, "EPSG:32633",
                          nodata=NODATA)
    x2, y2 = cells(res2 or res, origin2)
    gx, gy = x2 - shift[0], y2 - shift[1]  # where the second survey takes the ground of each of its cells
    z2 = surface(gx, gy) - (pit(gx, gy) if dug else 0.0) + dz + tilt[0] * (x2 - XC) + tilt[1] * (y2 - YC)
    z2 = z2 + np.random.default_rng(1).normal(0, noise, z2.shape) if noise else z2
    second = write_geotiff(folder / "second.tif", z2.astype(np.float32), origin2, res2 or res, "EPSG:32633",
                           nodata=NODATA)
    return first, second


def stable_xy(result, res=RES):
    rows, cols = result.stable_index
    return X0 + (cols + 0.5) * res, Y1 - (rows + 0.5) * res


def test_shift_bias_tilt_and_a_pit(tmp_path):
    first, second = write_pair(tmp_path)
    out = tmp_path / "coregistered.tif"
    r = coreg.coregister(first, second, out)
    assert r.converged and r.horizontal and r.iterations <= 10
    assert abs(r.dx - DX) < 0.1 * RES and abs(r.dy - DY) < 0.1 * RES  # within a tenth of a cell
    assert abs(r.dz - DZ) < 0.002  # at the centre (the tilt adds 0.15 mm along the shift)
    assert r.tilt_x == pytest.approx(TX, abs=1e-4) and r.tilt_y == pytest.approx(TY, abs=1e-4)
    assert r.centre == pytest.approx((XC, YC))
    assert r.nmad_after < 0.0015 < 0.01 < r.nmad_before and r.rmse_after < r.rmse_before / 10
    # the pit changed: none of its cells is stable ground
    x, y = stable_xy(r)
    assert int((pit(x, y) > 0.005).sum()) == 0 and r.cells > 0.8 * r.compared
    # the coregistered DEM: the grid of the second, float32, empty cells NaN
    with rasterio.open(second) as a, rasterio.open(out) as b:
        assert (b.width, b.height, b.transform, b.crs) == (a.width, a.height, a.transform, a.crs)
        assert b.dtypes[0] == "float32" and np.isnan(b.nodata)
    # the volume of the pit: clearly wrong before, right after
    around = Point(PIT[0], PIT[1]).buffer(PIT[2] + 0.15)
    truth = pit_volume()
    before = volume(second, reference=first, polygon=around)["cut_m3"]
    after = volume(out, reference=first, polygon=around)["cut_m3"]
    assert abs(before / truth - 1) > 0.05
    assert after == pytest.approx(truth, rel=0.02)
    d = r.to_dict()
    assert d["dx"] == pytest.approx(r.dx) and d["path"] == str(out) and "stable_index" not in d
    assert "dx +0.07" in coreg.summary(r)


def test_flat_ground_only_the_vertical(tmp_path):
    """On flat ground a horizontal shift cannot be measured (and changes no volume): only the
    vertical bias and the tilt are corrected."""
    first, second = write_pair(tmp_path, surface=lambda x, y: np.full_like(x, 100.0))
    r = coreg.coregister(first, second, tmp_path / "out.tif")
    assert not r.horizontal and (r.dx, r.dy) == (0.0, 0.0)
    assert r.dz == pytest.approx(DZ, abs=0.001) and r.tilt_x == pytest.approx(TX, abs=1e-4)
    x, y = stable_xy(r)
    assert int((pit(x - DX, y - DY) > 0.005).sum()) == 0  # the pit where the second survey shows it


def test_one_slope_is_a_vertical_shift(tmp_path):
    """On a plane a horizontal shift is a vertical one: measured as such, the volumes are the same."""
    first, second = write_pair(tmp_path, surface=lambda x, y: 100.0 + 0.3 * (x - XC) + 0.1 * (y - YC), dug=False,
                               tilt=(0.0, 0.0))
    r = coreg.coregister(first, second, tmp_path / "out.tif", tilt=False)
    assert not r.horizontal and (r.dx, r.dy) == (0.0, 0.0)
    assert r.dz == pytest.approx(DZ - 0.3 * DX - 0.1 * DY, abs=0.001)
    assert r.nmad_after < 0.002 and abs(r.median_after) < 0.0005


def test_too_far_apart_is_not_moved(tmp_path):
    """Further apart than the slopes can tell (1.5 m on waves of 0.4-3 m): the shift does not
    settle, no state agrees clearly better, the DEM is not moved and the result says so."""
    first, second = write_pair(tmp_path, shift=(1.2, -0.9), dug=False)
    r = coreg.coregister(first, second, max_iter=6)
    assert not r.converged and (r.dx, r.dy) == (0.0, 0.0)
    assert "NOT CONVERGED" in coreg.summary(r)


def test_another_grid_holes_and_a_sample(tmp_path):
    """The second DEM on a coarser grid with another origin and holes; the shift measured on a
    sample of the cells of the reference."""
    first, second = write_pair(tmp_path, res2=0.02, origin2=(X0 + 0.0047, Y1 - 0.0123))
    with rasterio.open(second, "r+") as dst:
        z = dst.read(1)
        z[100:140, 60:120] = NODATA
        dst.write(z, 1)
    r = coreg.coregister(first, second, tmp_path / "out.tif", max_samples=30000)
    assert r.step > 1 and r.converged
    assert abs(r.dx - DX) < 0.1 * RES and abs(r.dy - DY) < 0.1 * RES and abs(r.dz - DZ) < 0.002
    with rasterio.open(tmp_path / "out.tif") as b, rasterio.open(second) as a:
        out = b.read(1)
        assert b.transform == a.transform
    # the hole moved back with the DEM: 2 rows up, 3.5 columns left
    assert np.isnan(out[105:130, 65:110]).all() and np.isfinite(out[200:240, 150:200]).all()


def test_stable_and_excluded_areas(tmp_path):
    first, second = write_pair(tmp_path)
    dug = Point(PIT[0], PIT[1]).buffer(PIT[2] + 0.05)
    from shapely import contains_xy

    r = coreg.coregister(first, second, exclude=[dug])
    x, y = stable_xy(r)
    assert not contains_xy(dug, x, y).any() and abs(r.dx - DX) < 0.1 * RES
    box = Point(XC - 1, YC + 1).buffer(1.2)  # only there the ground may be stable
    r = coreg.coregister(first, second, stable=box)
    x, y = stable_xy(r)
    assert contains_xy(box, x, y).all() and abs(r.dy - DY) < 0.1 * RES
    with pytest.raises(ValueError, match="too few"):
        coreg.coregister(first, second, exclude=Point(XC, YC).buffer(10))
    with pytest.raises(ValueError):
        coreg.coregister(first, second, method="nothing")


def test_dems_in_different_systems(tmp_path):
    first, second = write_pair(tmp_path, noise=0.0)
    with rasterio.open(second) as src:
        z, prof = src.read(1), src.profile
    prof.update(crs="EPSG:32634", BIGTIFF="IF_SAFER")
    with rasterio.open(tmp_path / "other.tif", "w", **prof) as dst:
        dst.write(z, 1)
    with pytest.raises(ValueError, match="coordinate systems"):
        coreg.coregister(first, tmp_path / "other.tif")


def _project(tmp_path):
    """Two chunks, one survey each: the second shifted, raised and tilted, with the pit dug."""
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    doc.save(tmp_path / "scavo.ppg")
    first, second = write_pair(tmp_path)
    chunks = []
    for label, path in (("Prima", first), ("Dopo", second)):
        ch = doc.add_chunk(label)
        ch.crs = "EPSG:32633"
        dst = ch.data_dir / "dem.tif"
        dst.write_bytes(path.read_bytes())
        ch._set_product("dem", dst)
        ch.meta["dem"] = {"resolution": RES, "source": "point_cloud"}
        chunks.append(ch)
    doc.save()
    return doc, chunks[0], chunks[1]


def test_chunk_dem_kept_as_a_version(tmp_path):
    from pyarchinit_photogrammetry.core import versions

    doc, a, b = _project(tmp_path)
    original = rasterio.open(b.dem_path).read(1)
    out = b.coregister_dem(a, exclude=Point(PIT[0], PIT[1]).buffer(0.6))
    assert out["dx"] == pytest.approx(DX, abs=0.1 * RES) and out["reference"] == "Prima"
    assert out["reference_chunk"] == a.key and out["reference_version"] is None
    assert versions.active_info(b, "dem")["name"] == out["name"] == "DEM coregistrato su Prima"
    meta = b.meta["dem"]["coregistration"]
    assert meta["dy"] == pytest.approx(DY, abs=0.1 * RES) and b.meta["dem"]["source"] == "point_cloud"
    kept = versions.versions(b, "dem")
    assert len(kept) == 1 and kept[0]["id"] == out["version"]
    assert np.array_equal(rasterio.open(versions.version_file(b, "dem", out["version"])).read(1), original)
    assert b.dem_path == b.data_dir / "dem.tif" and not (b.data_dir / "dem.coreg.tmp.tif").exists()
    assert volume(b.dem_path, reference=a.dem_path, polygon=Point(PIT[0], PIT[1]).buffer(0.65))["cut_m3"] == \
        pytest.approx(pit_volume(), rel=0.02)
    doc.save()
    import pyarchinit_photogrammetry as ppg

    again = ppg.Document.open(doc.path)
    assert again.chunks[1].meta["dem"]["coregistration"]["dx"] == pytest.approx(out["dx"])
    # onto a kept version: the DEM of the chunk as it was, now among the versions of b
    out2 = a.coregister_dem((b, out["version"]), name="Prima sul vecchio Dopo")
    assert out2["reference_version"] == out["version"] and out2["dx"] == pytest.approx(-DX, abs=0.1 * RES)
    assert versions.active_info(a, "dem")["name"] == "Prima sul vecchio Dopo"
    with pytest.raises(ValueError, match="itself"):
        a.coregister_dem(a)


def test_command_line(tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, a, b = _project(tmp_path)
    wkt = Point(PIT[0], PIT[1]).buffer(0.6).wkt
    r = CliRunner().invoke(app, ["coregister", str(doc.path), "--reference", "Prima", "--chunk", "Dopo",
                                 "--exclude-polygon", wkt])
    assert r.exit_code == 0, r.output
    assert "coregistered on Prima" in r.output and "dx +0.07" in r.output
    import pyarchinit_photogrammetry as ppg

    again = ppg.Document.open(doc.path)
    assert again.chunks[1].meta["dem"]["coregistration"]["dx"] == pytest.approx(DX, abs=0.1 * RES)
    # the area dug from a GeoJSON file; once more on the coregistered DEM: nothing left to move
    import json

    from shapely.geometry import mapping

    gj = tmp_path / "scavo.geojson"
    gj.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": mapping(Point(PIT[0], PIT[1]).buffer(0.6))}]}))
    r = CliRunner().invoke(app, ["coregister", str(doc.path), "--reference", "Prima", "--chunk", "Dopo",
                                 "--exclude-polygon", str(gj), "--no-tilt", "--name", "Di nuovo"])
    assert r.exit_code == 0, r.output
    assert "active DEM 'Di nuovo'" in r.output and "dx +0.0000 m, dy +0.0000 m" in r.output
    for bad in (["--reference", "Nessuno"], ["--reference", "Prima", "--exclude-polygon", "POLYGON((0 0, 1 0"]):
        r = CliRunner().invoke(app, ["coregister", str(doc.path), "--chunk", "Dopo"] + bad)
        assert r.exit_code != 0


def test_interface_tool(tmp_path, monkeypatch):
    """Strumenti > Coregistra DEM: the reference chosen in the dialog, the selected polygon left out."""
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.core import versions
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, a, b = _project(tmp_path)
    doc.chunk = b
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    asked = {}

    def ask(cls, parent, title, fields, intro=""):
        asked["fields"] = {f.name: f for f in fields}
        return {"reference": f"{a.key}:", "areas": "exclude", "tilt": True, "name": ""}

    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(ask))
    dug = Point(PIT[0], PIT[1]).buffer(0.6)
    monkeypatch.setattr(w, "_selected_polygon", lambda: dug)
    w.coregister_dem()
    w.runner.wait()
    app.processEvents()
    assert [v for v, _ in asked["fields"]["reference"].choices] == [f"{a.key}:"]
    assert "areas" in asked["fields"]
    assert versions.active_info(b, "dem")["name"] == "DEM coregistrato su Prima"
    assert b.meta["dem"]["coregistration"]["dx"] == pytest.approx(DX, abs=0.1 * RES)
    assert any("DEM coregistrato su Prima" in e[2] for e in w.log_pane.entries)
    # the kept DEM of b is now a reference too
    monkeypatch.setattr(w, "_selected_polygon", lambda: None)
    w.coregister_dem()
    w.runner.wait()
    keys = [v for v, _ in asked["fields"]["reference"].choices]
    assert keys[0] == f"{a.key}:" and any(k.startswith(f"{b.key}:2") for k in keys) and "areas" not in asked["fields"]
    w.setWindowModified(False)
    w.close()
    app.processEvents()


@pytest.mark.skipif(not coreg.xdem_available(), reason="xdem is not installed (pip install "
                                                       "pyarchinit-photogrammetry[coreg])")
def test_xdem_agrees(tmp_path):
    first, second = write_pair(tmp_path)
    ours = coreg.coregister(first, second, tmp_path / "ours.tif")
    theirs = coreg.coregister(first, second, tmp_path / "xdem.tif", method="xdem")
    assert theirs.method == "xdem"
    assert theirs.shift == pytest.approx(ours.shift, abs=0.2 * RES) and abs(theirs.dz - ours.dz) < 0.002
    a = rasterio.open(tmp_path / "ours.tif").read(1)
    b = rasterio.open(tmp_path / "xdem.tif").read(1)
    both = np.isfinite(a) & np.isfinite(b)
    assert np.median(np.abs(a[both] - b[both])) < 0.002
    around = Point(PIT[0], PIT[1]).buffer(PIT[2] + 0.15)
    assert volume(tmp_path / "xdem.tif", reference=first, polygon=around)["cut_m3"] == pytest.approx(pit_volume(),
                                                                                                    rel=0.05)
