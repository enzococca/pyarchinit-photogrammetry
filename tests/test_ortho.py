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
    worst = _targets_error(many, scene)
    assert worst < 0.005, f"targets in the orthomosaic off by up to {worst * 1000:.2f} mm"
    # a true orthophoto on the mesh (vertical rays), and from the dense cloud alone
    worst = _targets_error(ch.build_orthomosaic(resolution=0.004, surface="model"), scene)
    assert worst < 0.006, f"targets in the true orthophoto off by up to {worst * 1000:.2f} mm"
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


def test_cancelled_orthomosaic_leaves_no_file(processed, tmp_path):
    import pytest

    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

    doc, ch = _copy(processed, tmp_path)
    before = ch.orthomosaic_path.read_bytes()
    prog = Progress()

    def stop(fraction, _text=""):
        if fraction > 0.1:
            prog.cancel_requested = True

    prog.callback = stop
    with pytest.raises(Cancelled):
        ch.build_orthomosaic(resolution=0.002, progress=prog)
    assert not (ch.data_dir / "orthomosaic.tmp.tif").exists()
    assert ch.orthomosaic_path.read_bytes() == before  # the orthomosaic of before is still there


def test_grazing_views_leave_the_cells_transparent(processed, tmp_path):
    """Cells seen only from far from the vertical (an orbit of oblique photos, a DEM interpolated
    beyond the reconstruction) smeared the photo over the surface: they stay transparent."""
    doc, ch = _copy(processed, tmp_path)

    def coverage(path):
        with rasterio.open(path) as src:
            return float((src.read(4) > 0).mean())

    normal = coverage(ch.build_orthomosaic(resolution=0.008))
    strict = coverage(ch.build_orthomosaic(resolution=0.008, max_angle=12.0))
    assert ch.meta["orthomosaic"]["max_angle"] == 12.0
    # how much falls out depends on the margins of the DEM seen only obliquely: a few points
    # (5 with the two-view depth maps, 3.4 with PatchMatch, which leaves less ground to interpolate)
    assert normal > 0.7 and strict < normal - 0.02


def test_a_float_source_takes_the_best_photo_that_has_a_value(processed, tmp_path):
    """A float source (a thermal frame, a registered band) has no value over parts of its photo: a
    cell takes the best photo that has one there, however many better photos have none - as the
    low strips of a flight whose narrower thermal frames miss the cell that their visible photos
    frame at the edge (a hole of 12 % in the paired thermal orthomosaic of a real flight)."""
    from pyarchinit_photogrammetry.engine.ortho import build_orthomosaic
    from pyarchinit_photogrammetry.engine.visibility import read_photo

    doc, ch = _copy(processed, tmp_path)
    up = np.linalg.inv(ch.transform)[:3, :3] @ np.array([0.0, 0.0, 1.0])
    up /= np.linalg.norm(up)

    def looks_down(cam) -> bool:
        return float(-(cam.transform[:3, :3] @ np.array([0.0, 0.0, 1.0])) @ up) > np.cos(np.radians(15))

    nadir = {c.key for c in ch.aligned_cameras if looks_down(c)}
    assert 10 <= len(nadir) < len(ch.aligned_cameras) - 4  # the obliques: the photos with values

    def source(cam):
        img = read_photo(cam).astype(np.float32)[::2, ::2]  # a band on a reduced grid, like the thermal ones
        return np.full_like(img, np.nan) if cam.key in nadir else img

    def read(path):
        with rasterio.open(path) as src:
            return src.read(1)

    mixed = read(build_orthomosaic(ch, resolution=0.008, source=source, out_path=tmp_path / "mixed.tif"))
    for cam in ch.aligned_cameras:  # the same without the photos that have no value
        if cam.key in nadir:
            cam.transform = None
    alone = read(build_orthomosaic(ch, resolution=0.008, source=source, out_path=tmp_path / "alone.tif"))
    assert np.isfinite(alone).mean() > 0.5
    both = np.isfinite(mixed) & np.isfinite(alone)
    assert both.sum() >= 0.995 * np.isfinite(alone).sum()  # every cell the obliques see has its value
    assert np.median(np.abs(mixed[both] - alone[both])) < 0.5
