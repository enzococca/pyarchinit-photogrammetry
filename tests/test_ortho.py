"""The orthomosaic made tile by tile: any size (wider than 32767 px), every surface, the same
result with one or more workers."""
from __future__ import annotations

import shutil

import numpy as np
import rasterio

from pyarchinit_photogrammetry.markers.targets import detect


def _copy(processed, tmp_path, name="copia"):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / name
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def _targets_error(path, scene) -> float:
    with rasterio.open(path) as src:
        img = np.moveaxis(src.read([3, 2, 1]), 0, -1).copy()
        tr = src.transform
    errors = []
    for d in detect(img):
        x, y = rasterio.transform.xy(tr, d["center"][1], d["center"][0])
        tx, ty, _ = scene.targets[d["code"]]
        errors.append(np.hypot(x - tx, y - ty))
    assert len(errors) >= 6
    return float(np.max(errors))


def test_surfaces_and_workers(processed, scene, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    one = ch.build_orthomosaic(resolution=0.004, workers=1)
    with rasterio.open(one) as src:
        a = src.read()
        assert src.overviews(1)  # pyramids for a quick display
    many = ch.build_orthomosaic(resolution=0.004, workers=4)
    with rasterio.open(many) as src:
        assert np.array_equal(src.read(), a)  # the same pixels, tile by tile in any order
    assert _targets_error(many, scene) < 0.005
    # a true orthophoto on the mesh (vertical rays), and from the dense cloud alone
    assert _targets_error(ch.build_orthomosaic(resolution=0.004, surface="model"), scene) < 0.006
    ch.products.pop("dem")
    ch.products.pop("model")
    ch.products.pop("model_textured", None)
    path = ch.build_orthomosaic(resolution=0.004)
    with rasterio.open(path) as src:
        assert (src.read(4) > 0).mean() > 0.6
    assert _targets_error(path, scene) < 0.006


def test_dem_surface_wider_than_opencv_maps(tmp_path):
    """OpenCV remaps at most 32767 px a side: the whole grid of a 45000 px orthophoto stopped
    with an assertion. The DEM is now read and resampled one window at a time."""
    from rasterio.transform import from_origin

    from pyarchinit_photogrammetry.engine.ortho import _DemSurface

    w, h = 40000, 6
    z = np.tile(np.arange(w, dtype=np.float32) * 0.001, (h, 1))
    z[:, 1000:1010] = -32767.0
    path = tmp_path / "dem.tif"
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32", nodata=-32767.0,
                       transform=from_origin(500000.0, 4000000.0, 0.05, 0.05), tiled=True, blockxsize=256,
                       blockysize=256) as dst:
        dst.write(z, 1)
    s = _DemSurface(path, 0.05)
    assert s.shape == (h, w)
    tile = s.tile(0, 39000, h, 1000)
    assert np.allclose(tile, z[:, 39000:40000], atol=1e-4)
    gap = s.tile(0, 990, h, 30)
    assert np.isnan(gap[:, 11:19]).all() and np.isfinite(gap[:, :9]).all()
    half = _DemSurface(path, 0.025)  # finer than the DEM: interpolated between its cells
    assert half.shape == (2 * h, 2 * w)
    t2 = half.tile(0, 70000, 2, 100)
    assert np.all(np.diff(t2[1][np.isfinite(t2[1])]) > 0)
