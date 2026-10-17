"""Overviews made again over a window only, the same as building them all."""
from __future__ import annotations

import shutil

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.transform import from_origin
from rasterio.windows import Window

from pyarchinit_photogrammetry.engine import overviews


def _rgba(path, size=(1536, 1280)):
    rng = np.random.default_rng(3)
    h, w = size
    data = rng.integers(0, 256, (4, h, w), dtype=np.uint8)
    data[3] = 255
    data[3, :200, :300] = 0  # a transparent corner, as at the edge of an orthomosaic
    data[:3, :200, :300] = 0
    profile = {"driver": "GTiff", "height": h, "width": w, "count": 4, "dtype": "uint8", "photometric": "RGB",
               "transform": from_origin(0, 0, 1, 1), "tiled": True, "blockxsize": 256, "blockysize": 256,
               "compress": "deflate"}
    with rasterio.open(path, "w", **profile) as dst:
        dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        dst.write(data)
        dst.build_overviews([2, 4, 8, 16], Resampling.average)


def _levels(path):
    out = {}
    with rasterio.open(path) as src:
        for i, f in enumerate(src.overviews(1)):
            with rasterio.open(path, overview_level=i) as ov:
                out[f] = ov.read()
    return out


def test_refresh_matches_a_full_build(tmp_path):
    assert overviews.available()
    a, b = tmp_path / "a.tif", tmp_path / "b.tif"
    _rgba(a)
    win = (430, 610, 300, 260)  # a patch: new pixels in this window
    with rasterio.open(a, "r+") as dst:
        patch = np.full((4, win[3], win[2]), 255, np.uint8)
        patch[0], patch[1], patch[2] = 200, 40, 90
        patch[3, :40] = 0
        dst.write(patch, window=Window(*win))
    shutil.copy2(a, b)
    with rasterio.open(b, "r+") as dst:  # every overview made again by GDAL
        dst.build_overviews([2, 4, 8, 16], Resampling.average)
    assert overviews.refresh(a, [win])
    got, want = _levels(a), _levels(b)
    assert sorted(got) == [2, 4, 8, 16]
    for f in got:
        diff = np.abs(got[f].astype(int) - want[f].astype(int))
        assert diff.max() <= 2, (f, diff.max())  # as GDAL makes it (it rounds every level: +-2)
    # before the refresh the overviews still showed the old pixels there
    with rasterio.open(a) as src:
        assert src.read(window=Window(*win))[0].max() == 200


def test_refresh_without_overviews_says_so(tmp_path):
    path = tmp_path / "c.tif"
    with rasterio.open(path, "w", driver="GTiff", height=64, width=64, count=1, dtype="uint8") as dst:
        dst.write(np.zeros((1, 64, 64), np.uint8))
    assert not overviews.refresh(path, [(0, 0, 10, 10)])
