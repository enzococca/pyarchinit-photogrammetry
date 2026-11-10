"""Masks from a colour range of the orthophoto (engine/colour_mask): a synthetic orthophoto of brown ground with a
blue-green rectangle of "water" (noise in it, specks of the same colour outside, an island inside) and a red patch
whose hues sit on both sides of 0; the mask written as a layer of free drawings with the heights of the DEM. The
coordinates are a synthetic origin, no real place."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core import free_drawings
from pyarchinit_photogrammetry.engine import colour_mask
from pyarchinit_photogrammetry.engine.colour_mask import HsvRange
from pyarchinit_photogrammetry.engine.dem import NODATA, write_geotiff

CRS = "EPSG:3004"
RES = 0.01
W, H = 4.0, 3.0
X0, Y1 = 2480000.0, 4500003.0
WATER = (X0 + 0.80, Y1 - 2.20, X0 + 2.60, Y1 - 0.60)  # xmin, ymin, xmax, ymax
ISLAND = (X0 + 1.50, Y1 - 1.60, X0 + 1.70, Y1 - 1.40)
RED = (X0 + 3.00, Y1 - 2.70, X0 + 3.60, Y1 - 2.10)
WATER_HSV = (190.0, 70.0, 60.0)  # degrees, %, %
GROUND_HSV = (30.0, 45.0, 55.0)
SPECKS = [(X0 + 0.3 + 0.25 * i, Y1 - 2.6) for i in range(3)] + [(X0 + 3.2, Y1 - 0.4)]


def _px(x, y):
    return int(round((x - X0) / RES)), int(round((Y1 - y) / RES))


def _box(img, box, hsv, rng, hue_spread=4.0):
    c0, r1 = _px(box[0], box[1])
    c1, r0 = _px(box[2], box[3])
    shape = (r1 - r0, c1 - c0)
    h = (hsv[0] + rng.normal(0, hue_spread, shape)) % 360.0
    s = np.clip(hsv[1] / 100 + rng.normal(0, 0.03, shape), 0, 1)
    v = np.clip(hsv[2] / 100 + rng.normal(0, 0.03, shape), 0, 1)
    img[r0:r1, c0:c1] = np.dstack([h, s, v]).astype(np.float32)


def orthophoto(specks=True, island=True):
    """RGBA uint8 of the synthetic ground."""
    rng = np.random.default_rng(3)
    rows, cols = int(round(H / RES)), int(round(W / RES))
    hsv = np.zeros((rows, cols, 3), np.float32)
    _box(hsv, (X0, Y1 - H, X0 + W, Y1), GROUND_HSV, rng, 6.0)
    _box(hsv, WATER, WATER_HSV, rng)
    if island:
        _box(hsv, ISLAND, GROUND_HSV, rng, 6.0)
    _box(hsv, RED, (0.0, 75.0, 60.0), rng, 5.0)  # hues on both sides of 0
    if specks:
        for x, y in SPECKS:  # 3 x 3 cells of the colour of the water
            _box(hsv, (x, y, x + 0.03, y + 0.03), WATER_HSV, rng)
    rgb = (cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB) * 255.0 + 0.5).astype(np.uint8)
    alpha = np.full((rows, cols, 1), 255, np.uint8)
    alpha[:, :5] = 0  # a strip with no value
    return np.concatenate([rgb, alpha], axis=2)


def dem_z(x, y):
    return 50.0 + 0.1 * (x - X0) - 0.05 * (Y1 - y)


def project(tmp_path, **kw):
    import rasterio
    from rasterio.transform import from_origin

    doc = ppg.Document()
    a = doc.add_chunk("A")
    a.crs = CRS
    doc.save(tmp_path / "p.ppg")
    img = orthophoto(**kw)
    path = a.data_dir / "orthomosaic.tif"
    with rasterio.open(path, "w", driver="GTiff", width=img.shape[1], height=img.shape[0], count=4, dtype="uint8",
                       crs=CRS, transform=from_origin(X0, Y1, RES, RES), photometric="RGB", BIGTIFF="IF_SAFER") as dst:
        dst.colorinterp = [rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
                           rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha]  # as engine/ortho
        dst.write(np.moveaxis(img, -1, 0))
    a._set_product("orthomosaic", path)
    n = 2.0 * RES
    rows, cols = int(round(H / n)), int(round(W / n))
    rr, cc = np.mgrid[0:rows, 0:cols]
    z = dem_z(X0 + (cc + 0.5) * n, Y1 - (rr + 0.5) * n).astype(np.float32)
    a._set_product("dem", write_geotiff(a.data_dir / "dem.tif", z, (X0, Y1), n, CRS, nodata=NODATA))
    doc.save()
    return doc, a


WATER_RANGE = HsvRange((170.0, 210.0), (40.0, 100.0), (30.0, 100.0))


def test_rectangle_found_within_two_cells(tmp_path):
    _doc, a = project(tmp_path)
    r = colour_mask.mask(a, WATER_RANGE)
    assert len(r.polygons) == 1
    b = r.polygons[0].bounds
    assert np.allclose(b, WATER, atol=2 * RES), (b, WATER)
    truth = (WATER[2] - WATER[0]) * (WATER[3] - WATER[1])
    assert r.area == pytest.approx(truth, rel=0.02)  # the island filled
    assert r.cell == pytest.approx(RES)
    assert 0.2 < r.share < 0.3
    # a smaller copy: the same rectangle within two of its cells
    r2 = colour_mask.mask(a, WATER_RANGE, side=200)
    assert r2.cell == pytest.approx(W / 200)
    assert np.allclose(r2.polygons[0].bounds, WATER, atol=2 * r2.cell)


def test_hue_wraps_for_reds(tmp_path):
    _doc, a = project(tmp_path)
    reds = HsvRange((345.0, 15.0), (50.0, 100.0), (30.0, 100.0))
    assert reds.wraps
    r = colour_mask.mask(a, reds)
    assert len(r.polygons) == 1
    assert np.allclose(r.polygons[0].bounds, RED, atol=2 * RES)
    half = colour_mask.mask(a, HsvRange((0.0, 15.0), (50.0, 100.0), (30.0, 100.0)), keep="all", opening=0.0,
                            fill_holes=False, min_area=0.0)
    truth = (RED[2] - RED[0]) * (RED[3] - RED[1])
    assert 0.3 * truth < half.area < 0.7 * truth  # one side of 0 alone: about half of the cells


def test_opening_drops_specks_and_fill_closes_holes(tmp_path):
    _doc, a = project(tmp_path)
    raw = colour_mask.mask(a, WATER_RANGE, opening=0.0, fill_holes=False, keep="all", min_area=0.0)
    assert len(raw.polygons) == 1 + len(SPECKS)
    assert raw.holes == 1  # the island
    opened = colour_mask.mask(a, WATER_RANGE, opening=0.05, fill_holes=False, keep="all", min_area=0.0)
    assert len(opened.polygons) == 1 and opened.holes == 1
    filled = colour_mask.mask(a, WATER_RANGE, opening=0.05, fill_holes=True, keep="all", min_area=0.0)
    assert len(filled.polygons) == 1 and filled.holes == 0
    isl = (ISLAND[2] - ISLAND[0]) * (ISLAND[3] - ISLAND[1])
    assert filled.area - opened.area == pytest.approx(isl, rel=0.15)
    # min_area alone drops the specks too
    big = colour_mask.mask(a, WATER_RANGE, opening=0.0, fill_holes=True, keep="all", min_area=0.01)
    assert len(big.polygons) == 1


def test_margin_grows_the_polygon(tmp_path):
    _doc, a = project(tmp_path)
    r0 = colour_mask.mask(a, WATER_RANGE)
    r1 = colour_mask.mask(a, WATER_RANGE, margin=0.1)
    w, h = WATER[2] - WATER[0], WATER[3] - WATER[1]
    grown = (w + 0.2) * (h + 0.2) - (4 - np.pi) * 0.1 ** 2  # rounded corners
    assert r1.area == pytest.approx(grown, rel=0.02)
    assert np.allclose(r1.polygons[0].bounds, np.add(r0.polygons[0].bounds, [-0.1, -0.1, 0.1, 0.1]), atol=0.005)
    shrunk = colour_mask.mask(a, WATER_RANGE, margin=-0.1)
    assert shrunk.area == pytest.approx((w - 0.2) * (h - 0.2), rel=0.03)


def test_layer_written_with_heights_from_the_dem(tmp_path):
    doc, a = project(tmp_path)
    r = colour_mask.mask(a, WATER_RANGE)
    ids = colour_mask.to_layer(a, r)
    feats = free_drawings.features(a, "Maschera colore 1")
    assert [f["id"] for f in feats] == ids and len(ids) == 1
    xyz = np.asarray(feats[0]["xyz"], float)
    assert feats[0]["kind"] == "polygon" and feats[0]["attributes"]["maschera"] == "colore"
    assert np.abs(xyz[:, 2] - dem_z(xyz[:, 0], xyz[:, 1])).max() < 0.003  # the DEM, interpolated at the vertices
    u = colour_mask.union(a, "Maschera colore 1")
    assert u.area == pytest.approx(r.area, rel=1e-6)
    # the same layer again: the earlier mask replaced, a polygon drawn by hand kept
    free_drawings.add(a, "polygon", [[X0 + 3, Y1 - 1, 50], [X0 + 3.5, Y1 - 1, 50], [X0 + 3.5, Y1 - 0.5, 50]],
                      layer="Maschera colore 1")
    again = colour_mask.to_layer(a, colour_mask.mask(a, WATER_RANGE, margin=0.05), layer="Maschera colore 1")
    assert len(free_drawings.features(a, "Maschera colore 1")) == 2 and again[0] not in ids
    assert colour_mask.next_layer_name(a) == "Maschera colore 2"
    doc.save()
    reopened = ppg.Document.open(tmp_path / "p.ppg")
    assert len(free_drawings.features(reopened.chunks[0], "Maschera colore 1")) == 2
    # without a DEM nothing is written
    a.products.pop("dem")
    n = len(free_drawings.features(a))
    with pytest.raises(ValueError, match="DEM"):
        colour_mask.to_layer(a, r, layer="altro")
    assert len(free_drawings.features(a)) == n
    with pytest.raises(ValueError, match="empty"):
        colour_mask.to_layer(a, colour_mask.mask(a, HsvRange((90.0, 120.0))))


def test_sample_gives_a_range_that_finds_it(tmp_path):
    _doc, a = project(tmp_path)
    cx, cy = (WATER[0] + WATER[2]) / 2 + 0.4, (WATER[1] + WATER[3]) / 2
    rng = colour_mask.sample(a, cx, cy)
    assert abs(((rng.hue[0] + rng.hue[1]) / 2) - WATER_HSV[0]) < 3
    r = colour_mask.mask(a, rng)
    assert np.allclose(r.polygons[0].bounds, WATER, atol=2 * RES)
    red = colour_mask.sample(a, (RED[0] + RED[2]) / 2, (RED[1] + RED[3]) / 2)
    assert red.wraps  # the median hue of the red patch is near 0: the range goes through it
    assert np.allclose(colour_mask.mask(a, red).polygons[0].bounds, RED, atol=2 * RES)
    with pytest.raises(ValueError, match="outside"):
        colour_mask.sample(a, X0 - 10, Y1)
    with pytest.raises(ValueError, match="no value"):
        colour_mask.sample(a, X0 + 0.02, Y1 - 1.0, radius=0.01)  # the strip with no value


def test_cli_mask_colour(tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    project(tmp_path)
    p = str(tmp_path / "p.ppg")
    cx, cy = (WATER[0] + WATER[2]) / 2, (WATER[1] + WATER[3]) / 2 + 0.3
    run = CliRunner().invoke(app, ["mask", "colour", p, "A", "--sample", f"{cx},{cy}", "--layer", "Acqua"])
    assert run.exit_code == 0, run.output
    assert "Acqua" in run.output
    doc = ppg.Document.open(tmp_path / "p.ppg")
    feats = free_drawings.features(doc.chunks[0], "Acqua")
    assert len(feats) == 1
    run = CliRunner().invoke(app, ["mask", "colour", p, "A", "--hue", "170,210", "--sat", "40,100", "--val",
                                   "30,100", "--all", "--opening", "0", "--min-area", "0", "--margin", "0.02"])
    assert run.exit_code == 0, run.output
    doc = ppg.Document.open(tmp_path / "p.ppg")
    assert len(free_drawings.features(doc.chunks[0], "Maschera colore 1")) == 1 + len(SPECKS)
    both = CliRunner().invoke(app, ["mask", "colour", p, "A", "--hue", "170,210", "--sample", f"{cx},{cy}"])
    assert both.exit_code != 0  # a range or a sample, not both
    none = CliRunner().invoke(app, ["mask", "colour", p, "A"])
    assert none.exit_code != 0


def test_a_hue_from_360_is_from_0(tmp_path):
    _doc, a = project(tmp_path)
    opts = dict(keep="all", opening=0.0, fill_holes=False, min_area=0.0)
    from_360 = colour_mask.mask(a, HsvRange((360.0, 15.0), (50.0, 100.0), (30.0, 100.0)), **opts)
    from_0 = colour_mask.mask(a, HsvRange((0.0, 15.0), (50.0, 100.0), (30.0, 100.0)), **opts)
    assert from_360.cells == from_0.cells  # not every hue
    assert colour_mask._circular_median(np.array([359.99, 0.01])) % 360.0 < 0.1


def test_mask_side_bounds_share_and_16_bits(tmp_path, monkeypatch):
    import rasterio

    _doc, a = project(tmp_path)
    monkeypatch.setattr(colour_mask, "MASK_SIDE", 100)  # the cap read at the call
    small = colour_mask.mask(a, WATER_RANGE)
    assert small.cell == pytest.approx(W / 100)
    monkeypatch.undo()
    whole = colour_mask.mask(a, WATER_RANGE)
    part = colour_mask.mask(a, WATER_RANGE, bounds=(WATER[0] - 0.2, WATER[1] - 0.2, WATER[2] + 0.2, WATER[3] + 0.2))
    assert np.allclose(part.polygons[0].bounds, whole.polygons[0].bounds, atol=RES)
    assert part.share > 2 * whole.share  # of the area read
    with pytest.raises(colour_mask.MaskError, match="outside") as err:
        colour_mask.mask(a, WATER_RANGE, bounds=(X0 - 10, Y1 - 10, X0 - 9, Y1 - 9))
    assert "fuori" in err.value.italian
    grown = colour_mask.mask(a, WATER_RANGE, margin=5.0)
    assert grown.share <= 1.0 and grown.share == pytest.approx(whole.share)  # the cells, before the margin
    # a 16 bit orthophoto: the same rectangle
    path = a.orthomosaic_path
    with rasterio.open(path) as src:
        data, profile = src.read(), src.profile
    profile.update(dtype="uint16")
    p16 = path.with_name("ortho16.tif")
    with rasterio.open(p16, "w", **profile) as dst:
        dst.colorinterp = [rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
                           rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha]
        dst.write(data.astype(np.uint16) * 257)
    a._set_product("orthomosaic", p16)
    r16 = colour_mask.mask(a, WATER_RANGE)
    assert np.allclose(r16.polygons[0].bounds, whole.polygons[0].bounds, atol=RES)


def test_stale_orthophoto_and_dem_refused(tmp_path):
    _doc, a = project(tmp_path)
    r = colour_mask.mask(a, WATER_RANGE)
    built = np.asarray(a.transform, float).copy()
    built[:3, 3] += (0.5, 0.0, 0.0)  # built in another place of the chunk
    a.meta.setdefault("built_in", {})["dem"] = built.tolist()
    with pytest.raises(colour_mask.MaskError, match="DEM") as err:
        colour_mask.to_layer(a, r)
    assert "rifallo" in err.value.italian and free_drawings.features(a) == []
    a.meta["built_in"]["orthomosaic"] = built.tolist()
    with pytest.raises(colour_mask.MaskError, match="orthophoto"):
        colour_mask.mask(a, WATER_RANGE)


def test_crop_with_the_union_of_a_layer(tmp_path):
    import rasterio

    from pyarchinit_photogrammetry.engine import crop

    _doc, a = project(tmp_path)
    reds = colour_mask.mask(a, HsvRange((345.0, 15.0), (50.0, 100.0), (30.0, 100.0)))
    water = colour_mask.mask(a, WATER_RANGE)
    colour_mask.to_layer(a, water, layer="Due")
    colour_mask.to_layer(a, reds, layer="Due", replace=False)
    u = colour_mask.union(a, "Due")
    assert u.geom_type == "MultiPolygon" and u.area == pytest.approx(water.area + reds.area, rel=1e-6)
    out = crop.crop_raster(a.orthomosaic_path, tmp_path / "ritaglio.tif", u)
    with rasterio.open(out) as src:
        alpha = src.read(4)
        tr = src.transform
    rows, cols = np.nonzero(alpha)
    xs, ys = tr.c + (cols + 0.5) * tr.a, tr.f + (rows + 0.5) * tr.e
    from shapely.geometry import Point

    sample = np.random.default_rng(0).choice(len(xs), 200, replace=False)
    assert all(u.buffer(RES).contains(Point(xs[i], ys[i])) for i in sample)  # nothing outside the two polygons
    gap = _px((WATER[2] + RED[0]) / 2, (WATER[1] + WATER[3]) / 2)  # between them: transparent
    c, r = (int((X0 + gap[0] * RES - tr.c) / tr.a), int((Y1 - gap[1] * RES - tr.f) / tr.e))
    assert alpha[r, c] == 0
    with pytest.raises(colour_mask.MaskError):
        colour_mask.union(a, "nessuno")
