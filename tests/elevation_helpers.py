"""Helpers shared by the tests of patches on elevations (test_elevation_patches, test_gui_patches): the origin of the
synthetic trench, every photo read by the engines replaced by a plain colour of its own (so that the colour of a pixel
tells which photo it comes from), and a cell of an elevation with relief. Plain functions, no fixtures: they live in a
module of their own, as tests/gnss_helpers.py, so that a test file can be changed without breaking the others."""
from __future__ import annotations

import numpy as np
import rasterio

from pyarchinit_photogrammetry.engine import elevation as E

ORIGIN = np.array([2480000.0, 4500000.0, 100.0])  # the synthetic trench (testing.synthetic.make_scene)


def colour_of(key: int) -> tuple[int, int, int]:
    """A colour (BGR) of its own for every photo (the rule of tests/test_patches.py)."""
    rng = np.random.default_rng(1000 + int(key))
    return tuple(int(v) for v in rng.integers(30, 226, 3))


def fake_photo(cam, flags=None):
    s = cam._chunk_sensor
    return np.full((s.height, s.width, 3), colour_of(cam.key), np.uint8)


def plain(monkeypatch):
    """Every photo read by the ortho and visibility engines is a plain colour of its own."""
    from pyarchinit_photogrammetry.engine import ortho, visibility

    monkeypatch.setattr(ortho, "read_photo", fake_photo)
    monkeypatch.setattr(visibility, "read_photo", fake_photo)


def prepare(ch):
    """The sensor of every camera where :func:`fake_photo` finds it."""
    for cam in ch.cameras:
        cam._chunk_sensor = ch.sensor(cam)


def surface_cell(ch, entry):
    """(u, v) of a cell of the elevation where its relief has a value, near the middle."""
    with rasterio.open(E.elevation_paths(ch, entry)[1]) as src:
        d = src.read(1)
        t = src.transform
    from scipy.ndimage import distance_transform_edt

    rows, cols = np.nonzero(np.isfinite(d))
    k = len(rows) // 2
    # among the cells farthest from the edge of the relief (an area drawn around the cell stays on the surface, whatever
    # the mesh of the run: the middle cell in row order could lie at the top or the bottom of the wall) the nearest
    # to the middle one
    depth = distance_transform_edt(np.pad(np.isfinite(d), 1))[1:-1, 1:-1]
    deep = np.argwhere(depth >= 0.9 * depth.max())
    r, c = deep[np.argmin(np.hypot(deep[:, 0] - rows[k], deep[:, 1] - cols[k]))]
    return t.c + (c + 0.5) * t.a, t.f + (r + 0.5) * t.e
