"""Voids of a DEM interpolated (engine.dem_voids): the holes enclosed by heights filled from their edges up to a
size, the larger ones and the ground outside the data left empty, the cells filled kept in a mask of the DEM."""
from __future__ import annotations

import shutil

import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.engine import dem_voids

RES = 0.05
X0, Y0 = 2480000.0, 4500010.0


def _surface(x, y):
    return 100.0 + 0.05 * x - 0.03 * y + 0.2 * np.sin(x / 2.0) * np.cos(y / 3.0)


def _grid(n=200):
    rows, cols = np.mgrid[0:n, 0:n]
    x, y = (cols + 0.5) * RES, -(rows + 0.5) * RES
    return x, y, _surface(x, y)


def _holes(x, y, z):
    """A small hole (0.4 m across), a big one (3 m across), a bay open to the edge of the data and the ground
    outside it (the right edge of the grid empty)."""
    z = z.copy()
    small = (x - 3.0) ** 2 + (y + 3.0) ** 2 < 0.2 ** 2
    big = (x - 6.0) ** 2 + (y + 6.0) ** 2 < 1.5 ** 2
    outside = x > 9.0
    bay = (np.abs(y + 2.0) < 0.15) & (x > 8.0)  # a notch 0.3 m wide reaching the empty edge
    for m in (small, big, outside, bay):
        z[m] = np.nan
    return z, small, big, outside | bay


def test_small_voids_filled_big_ones_and_outside_left():
    x, y, truth = _grid()
    z, small, big, out = _holes(x, y, truth)
    filled_dem, filled, info = dem_voids.fill_voids_array(z, RES, max_width=1.0)
    assert filled[small].all() and not filled[~small].any()
    assert np.abs(filled_dem[small] - truth[small]).max() < 0.003  # a smooth surface: millimetres
    assert np.isnan(filled_dem[big]).all() and np.isnan(filled_dem[out]).all()
    assert info["holes"] == 1 and info["skipped"] == 1 and info["cells"] == int(small.sum())
    # an area limit alone: the big hole (7 m2) too, never the ground outside
    filled_dem, filled, info = dem_voids.fill_voids_array(z, RES, max_width=None, max_area=10.0)
    assert filled[big].all() and np.isnan(filled_dem[out]).all()
    assert np.abs(filled_dem[big] - truth[big]).max() < 0.05
    assert np.nanmax(filled_dem[big]) <= np.nanmax(z) and np.nanmin(filled_dem[big]) >= np.nanmin(z)
    # a limit the big hole does not fit: left
    _d, filled, _i = dem_voids.fill_voids_array(z, RES, max_width=None, max_area=5.0)
    assert not filled[big].any()


def test_a_plane_stays_a_plane():
    rows, cols = np.mgrid[0:60, 0:60]
    plane = 50.0 + 0.1 * cols - 0.2 * rows
    z = plane.astype(np.float64)
    hole = (rows - 30) ** 2 + (cols - 30) ** 2 < 100
    z[hole] = np.nan
    out, filled, _ = dem_voids.fill_voids_array(z, 0.1, max_width=None)
    assert np.allclose(out[hole], plane[hole], atol=1e-8)
    assert filled[hole].all() and not filled[~hole].any()
    with pytest.raises(ValueError):
        dem_voids.fill_voids_array(z, 0.1, max_width=0)


def _chunk(tmp_path, z):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.engine.dem import NODATA, write_geotiff

    doc = ppg.Document()
    ch = doc.add_chunk("Saggio 1")
    doc.save(tmp_path / "scavo.ppg")
    ch.crs = "EPSG:3004"
    ch.data_dir.mkdir(parents=True, exist_ok=True)
    arr = np.where(np.isfinite(z), z, NODATA).astype(np.float32)
    ch._set_product("dem", write_geotiff(ch.data_dir / "dem.tif", arr, (X0, Y0), RES, "EPSG:3004", nodata=NODATA))
    doc.save()
    return doc, ch


def _read(path):
    with rasterio.open(path) as src:
        a = src.read(1).astype(np.float64)
        a[a == src.nodata] = np.nan
        return a


def test_fill_the_dem_of_a_chunk_with_its_mask_and_versions(tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.core import versions
    from pyarchinit_photogrammetry.engine.probe import Probe

    x, y, truth = _grid(400)  # 400 x 400 cells: several tiles of windows
    z, small, big, out = _holes(x, y, truth)
    second = (x - 2.0) ** 2 + (y + 15.0) ** 2 < 0.3 ** 2  # another small hole, in another tile
    z[second] = np.nan
    monkeypatch.setattr(dem_voids, "TILE", 128)
    monkeypatch.setattr(dem_voids, "STRIP", 64)
    doc, ch = _chunk(tmp_path, z)
    assert dem_voids.mask_path(ch) is None
    z = _read(ch.dem_path)  # as stored (float32)
    info = ch.fill_dem_voids(max_width=1.0)
    assert info["holes"] == 2 and info["cells"] == int(small.sum() + second.sum()) and info["skipped"] == 1
    after = _read(ch.dem_path)
    assert np.abs(after[small] - truth[small]).max() < 0.003
    assert np.isnan(after[big]).all() and np.isnan(after[out & (x < 10)]).all()
    assert np.array_equal(after[~(small | second)], z[~(small | second)], equal_nan=True)
    mask = dem_voids.mask_path(ch)
    assert mask is not None and mask.parent == ch.data_dir
    m = _read(mask)
    assert (m[small | second] == dem_voids.VOID).all() and np.isnan(m[~(small | second)]).all()  # nodata 0
    # the DEM as it was among the versions, without a mask
    kept = versions.versions(ch, "dem")
    assert len(kept) == 1 and "vuoti interpolati" in versions.active_info(ch, "dem")["name"]
    old = versions.version_file(ch, "dem", kept[0]["id"])
    assert dem_voids.mask_path(ch, old) is None
    # the readout and the measuring points say the height is interpolated
    r, c = np.argwhere(small)[0]
    px, py = X0 + (c + 0.5) * RES, Y0 - (r + 0.5) * RES
    assert dem_voids.interpolated_at(ch, px, py) == dem_voids.VOID
    with Probe(ch) as probe:
        values = probe.at(px, py, ["z"])
        assert values.get("z_interp") == dem_voids.VOID
        assert "interpolata" in probe.text(values, ["z"])
        assert probe.record(px, py)["z_interp"] == dem_voids.VOID
        assert "interpolata" not in probe.text(probe.at(X0 + 0.1, Y0 - 0.1, ["z"]), ["z"])
    st = dem_voids.mask_stats(ch)
    assert st["void"] == info["cells"] and st["gap"] == 0
    from pyarchinit_photogrammetry.io import report_content as rc

    rows = rc._interpolated_rows(rc.Words("it"), ch, ch.dem_path)
    assert rows and rows[0][0] == "Quote interpolate"
    # the export of the DEM takes its mask along
    exported = ch.export_dem(tmp_path / "out" / "dem_scavo.tif")
    assert (tmp_path / "out" / "dem_scavo_interpolata.tif").exists() and exported.exists()
    # a second fill (an area limit): the cells filled before stay marked, a new mask replaces the old one
    info2 = ch.fill_dem_voids(max_width=None, max_area=10.0, keep_version=False)
    assert info2["holes"] == 1
    m2 = _read(dem_voids.mask_path(ch))
    assert (m2[small | second | big] == dem_voids.VOID).all()
    assert len(list(ch.data_dir.glob("dem_interpolated_*.tif"))) == 1  # the first mask: no DEM kept wants it
    # undo by the versions: the DEM as it was is active again, without a mask
    versions.activate(ch, "dem", kept[0]["id"])
    assert dem_voids.mask_path(ch) is None
    assert np.isnan(_read(ch.dem_path)[small]).all()
    # and back: the filled DEM, now a version, finds its mask again
    filled_v = versions.versions(ch, "dem")[-1]
    versions.activate(ch, "dem", filled_v["id"])
    assert dem_voids.mask_path(ch) is not None


def test_nothing_to_fill_leaves_the_dem_alone(tmp_path):
    from pyarchinit_photogrammetry.core import versions

    _x, _y, truth = _grid(100)
    doc, ch = _chunk(tmp_path, truth)
    before = ch.dem_path.stat().st_mtime_ns
    info = ch.fill_dem_voids(max_width=1.0)
    assert info["holes"] == 0 and info["cells"] == 0
    assert ch.dem_path.stat().st_mtime_ns == before and not versions.versions(ch, "dem")


def test_trench_holes_cut_in_the_cloud_and_the_dem(processed, scene, tmp_path):
    """On the synthetic trench: holes cut in the dense cloud and in the DEM, filled against the true surface."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.engine.dem import dem_from_points, surface_points_world

    doc0, ch0, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc0.path, d / doc0.path.name)
    shutil.copytree(doc0.files_dir, d / doc0.files_dir.name)
    doc = ppg.Document.open(d / doc0.path.name)
    ch = doc.chunk
    ox, oy = scene.origin[:2]
    # 1. holes cut in the cloud: the DEM built from it without the usual gap filling keeps them
    pts = surface_points_world(ch, "point_cloud")
    centres = [(0.6, 1.0), (3.4, 2.2), (1.4, 1.5), (2.6, 1.6)]  # the ground around, the floor of the trench
    radius = 0.06
    keep = np.ones(len(pts), bool)
    for cx, cy in centres:
        keep &= np.hypot(pts[:, 0] - ox - cx, pts[:, 1] - oy - cy) > radius
    res = 0.01
    dem, origin, _ = dem_from_points(pts[keep], res, interpolate=False)
    rows, cols = np.mgrid[0:dem.shape[0], 0:dem.shape[1]]
    x, y = origin[0] + (cols + 0.5) * res, origin[1] - (rows + 0.5) * res
    cut = np.zeros(dem.shape, bool)
    for cx, cy in centres:
        cut |= np.hypot(x - ox - cx, y - oy - cy) < radius - res
    assert np.isnan(dem[cut]).mean() > 0.95
    out, filled, info = dem_voids.fill_voids_array(dem, res, max_width=0.2)
    truth = scene.world_height(x, y).reshape(x.shape)
    err = np.abs(out[cut & filled] - truth[cut & filled])
    assert (cut & filled).sum() > 0.9 * cut.sum()
    assert np.median(err) < 0.003 and np.percentile(err, 95) < 0.006
    # the empty ground around the survey is never filled
    edge = np.isnan(dem) & ~filled
    assert np.isnan(out[edge]).all()
    # 2. holes cut in the DEM of the project: small ones filled, a big one left, nothing outside the data
    with rasterio.open(ch.dem_path) as src:
        t = src.transform
        h, w = src.height, src.width
    z = _read(ch.dem_path)
    rows, cols = np.mgrid[0:h, 0:w]
    x, y = t.c + (cols + 0.5) * t.a, t.f + (rows + 0.5) * t.e
    small = np.zeros(z.shape, bool)
    for cx, cy in centres:
        small |= np.hypot(x - ox - cx, y - oy - cy) < 0.08
    big = np.hypot(x - ox - 2.0, y - oy - 0.45) < 0.4
    empty_before = np.isnan(z)
    z[small | big] = np.nan
    from pyarchinit_photogrammetry.engine.dem import NODATA

    with rasterio.open(ch.dem_path, "r+") as dst:
        dst.write(np.where(np.isfinite(z), z, NODATA).astype(np.float32), 1)
    info = ch.fill_dem_voids(max_width=0.25)
    after = _read(ch.dem_path)
    assert np.isfinite(after[small]).all()
    err = np.abs(after[small] - scene.world_height(x[small], y[small]))
    assert np.median(err) < 0.003 and np.percentile(err, 95) < 0.008
    assert np.isnan(after[big]).all()
    assert np.isnan(after[empty_before & ~small]).all()
    assert info["skipped"] >= 1
    # 3. the DEM built with the option: the mask of its interpolated cells
    ch.build_dem(fill_voids=0.25)
    assert ch.meta["dem"]["voids_filled"]["max_width"] == 0.25
    st = dem_voids.mask_stats(ch)
    assert st is None or st["cells"] > 0


def test_interface_fills_the_voids_and_undo_brings_the_dem_back(trench, monkeypatch):
    """Strumenti > Interpola i vuoti del DEM: the hole filled, the readout says "interpolata", Modifica > Annulla
    gives the DEM back without its mask, Ripeti the filled one with it."""
    w, app, mw = trench
    ch = w.chunk
    with rasterio.open(ch.dem_path, "r+") as dst:
        z = dst.read(1)
        t = dst.transform
        z[100:110, 120:130] = dst.nodata  # a hole of 20 x 20 cm
        dst.write(z, 1)
    w._probe_reset()
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {
        "max_width": 0.5, "max_area": None, "keep_copy": False, "version_name": ""}))
    w.fill_dem_voids()
    w.runner.wait()
    app.processEvents()
    after = _read(ch.dem_path)
    assert np.isfinite(after[100:110, 120:130]).all()
    assert dem_voids.mask_path(ch) is not None
    px, py = t.c + 125.5 * t.a, t.f + 105.5 * t.e
    assert "interpolata" in w._interpolated_note(px, py)
    assert w._interpolated_note(t.c + 10.5 * t.a, t.f + 10.5 * t.e) == ""
    w.undo()  # as Modifica > Annulla: the readout above left the DEM open, which Windows would not replace
    assert np.isnan(_read(ch.dem_path)[100:110, 120:130]).all()
    assert dem_voids.mask_path(ch) is None and w._interpolated_note(px, py) == ""
    w.redo()
    assert np.isfinite(_read(ch.dem_path)[100:110, 120:130]).all() and dem_voids.mask_path(ch) is not None
