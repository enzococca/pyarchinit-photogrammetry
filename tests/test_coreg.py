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


def test_too_far_apart_for_the_slopes(tmp_path):
    """Further apart than the slopes can tell (1.5 m on waves of 0.4-3 m): without the initial
    search the shift does not settle, no state agrees clearly better, the DEM is not moved and the
    result says so; the search finds it and the fit starts from there."""
    first, second = write_pair(tmp_path, shift=(1.2, -0.9), dug=False)
    r = coreg.coregister(first, second, max_iter=6, search=0)
    assert not r.converged and (r.dx, r.dy) == (0.0, 0.0) and r.initial == "off"
    assert "NOT CONVERGED" in coreg.summary(r)
    r = coreg.coregister(first, second, max_iter=6)
    assert r.converged and r.initial == "used" and r.method_used == "search+nuth_kaab"
    assert abs(r.dx - 1.2) < 0.01 * RES and abs(r.dy + 0.9) < 0.01 * RES
    assert r.initial_shift == pytest.approx((1.2, -0.9), abs=r.search_cell)
    assert "initial search +1.20 -0.90 m" in coreg.summary(r) and "NOT CONVERGED" not in coreg.summary(r)


# ---------------------------------------------------------------- shifts of metres
FAR = (4.3, -2.7)  # two surveys georeferenced with different GPS: the second shows the ground that far


def rough_ground(extent=(-40.0, 40.0), step=0.025, relief=0.15, longest=4.0, seed=5):
    """Ground rough at every scale up to ``longest`` m and level beyond - a site, with pits, banks
    and stones, and no hill whose slopes would lead a fit from far away: a random field with a power
    spectrum k^-3 (smooth below 8 steps) of ``relief`` m, read anywhere by cubic splines; local
    coordinates in ``extent``."""
    from scipy import fft, ndimage

    n = fft.next_fast_len(int(round((extent[1] - extent[0]) / step)))
    k = np.hypot(*np.meshgrid(fft.fftfreq(n, step), fft.rfftfreq(n, step), indexing="ij"))
    k[0, 0] = 1.0
    spec = fft.rfft2(np.random.default_rng(seed).normal(size=(n, n)))
    spec *= k ** -1.5 * np.exp(-(8 * step * k) ** 2) * (1 - np.exp(-(longest * k) ** 2))
    z = fft.irfft2(spec, s=(n, n))
    coef = ndimage.spline_filter(z * relief / z.std(), order=3)

    def surface(x, y):
        rows, cols = (np.asarray(y) - extent[0]) / step, (np.asarray(x) - extent[0]) / step
        return 100.0 + ndimage.map_coordinates(coef, [rows.ravel(), cols.ravel()], order=3, prefilter=False,
                                               mode="mirror").reshape(np.shape(x))
    return surface


def field(x, y):
    """Level ground with a few features: three ditches and five pits, nothing else."""
    z = 100.0 + 0.004 * x - 0.002 * y
    for ax, ay, ang, length in ((-5, 3, 0.3, 18), (6, -4, 1.9, 14), (0, 10, 2.8, 10)):
        u = (x - ax) * np.cos(ang) + (y - ay) * np.sin(ang)
        v = -(x - ax) * np.sin(ang) + (y - ay) * np.cos(ang)
        z = z - 0.3 * np.exp(-(v / 0.25) ** 2) * (np.abs(u) < length / 2)
    for px, py in ((-8, -8), (9, 9), (3, -12), (-12, 6), (12, 1)):
        z = z - 0.4 * np.exp(-((x - px) ** 2 + (y - py) ** 2) / (2 * 0.6 ** 2))
    return z


def dug_far(x, y):
    """A pit 1.5 m across dug between the two surveys (local coordinates of the ground)."""
    t = np.clip((1.5 - np.hypot(x - 2.0, y + 3.0)) / 0.3, 0, 1)
    return 0.5 * t * t * (3 - 2 * t)


def write_far(folder, surface, size=30.0, res=0.05, offset=(8.0, -5.0), shift=FAR, dz=3.0, tilt=(0.002, -0.001),
              dig=None, noise=0.003):
    """The reference on a square of ``size`` m and the second survey on the square moved by
    ``offset`` (a partial overlap), showing the ground ``shift`` further, ``dz`` higher and tilted
    (about the centre of the reference), with ``dig`` taken away and noise; local coordinates of the
    ground from the centre of the reference. Returns the paths and the height of the second survey
    above the first at the centre of its grid, where :func:`coreg.coregister` measures ``dz``."""
    c = size / 2
    x, y = cells(res, (X0, Y1), size)
    first = write_geotiff(folder / "first.tif", surface(x - X0 - c, y - Y1 + c).astype(np.float32), (X0, Y1), res,
                          "EPSG:32633", nodata=NODATA)
    origin = (X0 + offset[0], Y1 + offset[1])
    x, y = cells(res, origin, size)
    gx, gy = x - shift[0] - X0 - c, y - shift[1] - Y1 + c
    z = surface(gx, gy) + dz + tilt[0] * (x - X0 - c) + tilt[1] * (y - Y1 + c)
    if dig is not None:
        z = z - dig(gx, gy)
    z = z + np.random.default_rng(1).normal(0, noise, z.shape)
    second = write_geotiff(folder / "second.tif", z.astype(np.float32), origin, res, "EPSG:32633", nodata=NODATA)
    cx, cy = origin[0] + c, origin[1] - c
    return first, second, dz + tilt[0] * (cx + shift[0] - X0 - c) + tilt[1] * (cy + shift[1] - Y1 + c)


def test_shift_of_metres_partial_overlap_tilt_and_a_pit(tmp_path):
    """Two surveys georeferenced with different GPS: 4.3 m east and 2.7 m south of each other, 3 m
    higher, tilted, overlapping on 80 % of their area, a pit dug in between, on rough ground whose
    relief is all shorter than the shift. The initial search finds the shift, the fit refines it to
    a fraction of a cell and the volume of the pit comes right; the fit alone stays where it starts."""
    first, second, dz = write_far(tmp_path, rough_ground(), dig=dug_far)
    out = tmp_path / "coregistered.tif"
    r = coreg.coregister(first, second, out)
    assert r.initial == "used" and r.method_used == "search+nuth_kaab" and r.converged and r.horizontal
    assert abs(r.dx - FAR[0]) < 0.02 * 0.05 and abs(r.dy - FAR[1]) < 0.02 * 0.05  # 0.02 cell
    assert abs(r.initial_shift[0] - FAR[0]) < r.search_cell and abs(r.initial_shift[1] - FAR[1]) < r.search_cell
    assert r.initial_score > 0.8 and r.search_cell == pytest.approx(0.1)
    assert abs(r.dz - dz) < 0.001 and r.tilt_x == pytest.approx(0.002, abs=1e-5) and \
        r.tilt_y == pytest.approx(-0.001, abs=1e-5)
    assert r.nmad_after < 0.005 < 0.05 < r.nmad_before
    # the pit: where the second survey shows it, moved back onto the first
    c = 15.0
    pit = Point(X0 + c + 2.0, Y1 - c - 3.0).buffer(1.8)
    step = 0.005
    gx, gy = np.meshgrid(np.arange(0.5, 3.5, step), np.arange(-4.5, -1.5, step))
    truth = float(dug_far(gx, gy).sum() * step * step)
    assert volume(out, reference=first, polygon=pit)["cut_m3"] == pytest.approx(truth, rel=0.01)
    d = r.to_dict()
    assert d["initial"] == "used" and d["initial_shift"] == pytest.approx(list(r.initial_shift))
    assert d["method_used"] == "search+nuth_kaab" and d["search_cell"] == pytest.approx(0.1)
    import json

    json.dumps(d)
    # without the search the fit stays near the DEM as it is: no stable ground agrees there
    alone = coreg.coregister(first, second, search=0)
    assert alone.initial == "off" and alone.shift < 1.0 and alone.nmad_after > 10 * r.nmad_after


def test_shift_of_metres_on_level_ground_with_a_few_features(tmp_path):
    """Level ground with three ditches and five pits: the fit from the DEM as it is ends near it with
    the stable ground agreeing as well as it can (the features out of place are taken for ground
    that changed); the shift found by the search leaves far less of the sloped ground in
    disagreement and is kept."""
    first, second, dz = write_far(tmp_path, field, size=40.0, offset=(6.0, -4.0))
    r = coreg.coregister(first, second)
    assert r.initial == "used" and r.converged and r.horizontal
    assert abs(r.dx - FAR[0]) < 0.02 * 0.05 and abs(r.dy - FAR[1]) < 0.02 * 0.05 and abs(r.dz - dz) < 0.001


def test_periodic_ground_is_flagged_not_moved_by_the_search(tmp_path):
    """Ground that repeats itself every 1.5 m both ways: many shifts match as well, the search says
    so and its shift is not used; the fit from the DEM as it is ends on the match next to it."""
    def eggs(x, y):
        return 100.0 + 0.1 * np.sin(2 * np.pi * x / 1.5) * np.sin(2 * np.pi * y / 1.5)

    first, second, _ = write_far(tmp_path, eggs)
    r = coreg.coregister(first, second)
    assert r.initial == "ambiguous" and r.initial_shift is None and r.method_used == "nuth_kaab"
    assert abs(r.dx) <= 0.75 and abs(r.dy) <= 0.75  # within half a period of the DEM as it is
    assert "initial search ambiguous" in coreg.summary(r)
    # furrows: the same along them, every 0.8 m across
    def furrows(x, y):
        return 100.0 + 0.08 * np.sin(2 * np.pi * (x * np.cos(0.4) + y * np.sin(0.4)) / 0.8)

    first, second, _ = write_far(tmp_path, furrows)
    r = coreg.coregister(first, second)
    assert r.initial == "ambiguous" and not r.horizontal and r.shift < 0.8


def test_flat_or_different_ground_finds_nothing(tmp_path):
    """Level ground: nothing for the search to find, the horizontal shift is not measured. Two
    surveys of different ground: the search finds no match and the DEM is not moved."""
    first, second, dz = write_far(tmp_path, lambda x, y: np.full_like(x, 100.0))
    r = coreg.coregister(first, second)
    assert r.initial == "none" and r.initial_shift is None and not r.horizontal and (r.dx, r.dy) == (0.0, 0.0)
    # not moved: the tilt is measured where the DEM is, not where the shift would take it
    assert r.dz == pytest.approx(dz - (0.002 * FAR[0] - 0.001 * FAR[1]), abs=0.001)
    first, _, _ = write_far(tmp_path, rough_ground(), dz=0.0, tilt=(0.0, 0.0))
    (tmp_path / "other").mkdir()
    _, other, _ = write_far(tmp_path / "other", rough_ground(seed=6), dz=0.0, tilt=(0.0, 0.0))
    r = coreg.coregister(first, other, max_iter=6)
    assert r.initial == "none" and (r.dx, r.dy) == (0.0, 0.0) and not r.converged


def test_surveys_that_do_not_overlap_as_they_are(tmp_path):
    """A GPS error larger than the surveys: as they are the two DEMs of 4 m do not overlap, and the
    fit from there cannot start. The search matches them and the fit from its shift is used; without
    the search they cannot be compared."""
    first, second, _ = write_far(tmp_path, rough_ground(), size=4.0, res=0.01, offset=(4.5, 0.2), shift=(4.3, 0.1))
    r = coreg.coregister(first, second)
    assert r.initial == "used" and r.converged and abs(r.dx - 4.3) < 0.02 * 0.01 and abs(r.dy - 0.1) < 0.02 * 0.01
    assert r.nmad_after < 0.005 and r.to_dict()["nmad_before"] is None  # nothing to compare before
    with pytest.raises(ValueError, match="too few"):
        coreg.coregister(first, second, search=0)


def test_search_distance(tmp_path):
    """A search shorter than the shift: the best match within reach only looks like the ground, a
    clearly better one lies further; it is reported, not used. Long enough, it is used."""
    first, second, _ = write_far(tmp_path, rough_ground(), dig=dug_far)
    r = coreg.coregister(first, second, search=2.0)
    assert r.initial == "beyond" and r.initial_shift == pytest.approx(FAR, abs=r.search_cell)
    assert r.shift < 1.0 and r.method_used == "nuth_kaab" and "beyond the search distance" in coreg.summary(r)
    r = coreg.coregister(first, second, search=6.0)
    assert r.initial == "used" and abs(r.dx - FAR[0]) < 0.001


def test_a_few_million_cells(tmp_path):
    """Two DEMs of 4 million cells (100 m at 5 cm) metres apart: found and fitted in seconds."""
    import time

    first, second, dz = write_far(tmp_path, rough_ground((-80.0, 80.0), step=0.05), size=100.0,
                                  offset=(10.0, -7.0), shift=(4.33, -2.71))
    t = time.perf_counter()
    r = coreg.coregister(first, second, tmp_path / "out.tif")
    elapsed = time.perf_counter() - t
    assert r.initial == "used" and r.step == 2 and r.search_cell == pytest.approx(0.2)
    assert abs(r.dx - 4.33) < 0.02 * 0.05 and abs(r.dy + 2.71) < 0.02 * 0.05 and abs(r.dz - dz) < 0.001
    assert elapsed < 60, f"{elapsed:.1f} s"


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


def test_summary_of_dems_apart_as_they_were():
    from pyarchinit_photogrammetry.archaeo import coreg

    r = {"dx": 4.3, "dy": -2.7, "dz": 3.0, "tilt_x": 0.0, "tilt_y": 0.0, "nmad_before": None, "nmad_after": 0.003,
         "cells": 1000, "initial": "used", "initial_shift": (4.31, -2.69), "initial_score": 0.8}
    text = coreg.summary(r)
    assert "NMAD - -> 0.0030 m" in text and "nan" not in text and "initial search +4.31 -2.69 m" in text
