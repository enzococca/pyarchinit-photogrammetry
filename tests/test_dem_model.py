"""DEM from a large model: its surface points are made block by block (engine.dem.ModelSurface)."""
import numpy as np
import pytest
import rasterio

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine import dem as dem_engine
from pyarchinit_photogrammetry.io.mesh_ply import write_mesh_ply

TRANSFORM = np.array([[2.0, 0, 0, 1000.0], [0, 2.0, 0, 2000.0], [0, 0, 2.0, 50.0], [0, 0, 0, 1.0]])


def _height(x, y):
    return 0.5 * np.sin(x / 3.0) * np.cos(y / 4.0) + 0.02 * np.sin(x * 7.0)


def _mesh_chunk(root, side=160, double=False):
    """A chunk whose only product is a model: a height field of 10 x 10 units, scaled and moved by the chunk."""
    doc = ppg.Document()
    ch = doc.add_chunk("mesh")
    doc.save(root / "p.ppg")
    ch.data_dir.mkdir(parents=True, exist_ok=True)
    g = np.linspace(0.0, 10.0, side)
    gx, gy = np.meshgrid(g, g)
    v = np.column_stack([gx.ravel(), gy.ravel(), _height(gx, gy).ravel()])
    idx = np.arange(side * side, dtype=np.int32).reshape(side, side)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()
    model = write_mesh_ply(ch.data_dir / "model.ply", v, np.vstack([np.column_stack([a, b, c]),
                                                                    np.column_stack([a, c, d])]), double=double)
    ch._set_product("model", model)
    ch.meta["levelled"] = True
    ch.transform = TRANSFORM.copy()
    return ch


def _read(path):
    with rasterio.open(path) as src:
        a = src.read(1).astype(np.float64)
        a[a == src.nodata] = np.nan
        return a, src.transform


def _in_blocks(monkeypatch):
    monkeypatch.setattr(dem_engine, "STREAM_FACES", 1)
    monkeypatch.setattr(dem_engine, "FACE_BLOCK", 7000)
    monkeypatch.setattr(dem_engine, "SAMPLE_BLOCK", 20000)


@pytest.mark.parametrize("double", [False, True])
def test_dem_of_a_model_in_blocks_is_the_dem_of_the_model_at_once(tmp_path, monkeypatch, double):
    ch = _mesh_chunk(tmp_path, double=double)
    assert dem_engine.model_surface(ch) is None  # a small model: sampled at once, as before
    whole, frame = _read(ch.build_dem(resolution=0.05, source="model"))
    spacing = dem_engine.point_spacing(dem_engine.surface_points_world(ch, "model")[:, :2])

    _in_blocks(monkeypatch)
    surface = dem_engine.model_surface(ch)
    assert surface is not None and len(surface._areas) > 5  # several blocks of faces
    assert surface.xyz.dtype == (np.float64 if double else np.float32)  # the coordinates as the file holds them
    assert sum(len(p) for p in surface.blocks()) == surface.count
    first, again = np.vstack(list(surface.blocks())), np.vstack(list(surface.blocks()))
    assert np.array_equal(first, again)  # two passes over the model give the same points
    # the spacing of the points (the resolution, when it is not given) as from the points all together
    assert surface.spacing() == pytest.approx(spacing, rel=0.03)
    del surface
    blocks, frame_blocks = _read(ch.build_dem(resolution=0.05, source="model"))
    assert blocks.shape == whole.shape and frame_blocks == frame  # the same grid
    assert np.array_equal(np.isfinite(blocks), np.isfinite(whole))
    assert np.isfinite(blocks).mean() > 0.99
    d = np.abs(blocks - whole)[np.isfinite(whole)]
    # other points of the same surface: the highest in a cell of 5 cm differs by millimetres on a slope
    assert np.median(d) < 0.003 and np.percentile(d, 99) < 0.015, (np.median(d), np.percentile(d, 99))

    # against the surface itself, at the cell centres
    rows, cols = np.nonzero(np.isfinite(blocks))
    x, y = rasterio.transform.xy(frame_blocks, rows, cols)
    truth = 50.0 + 2.0 * _height((np.asarray(x) - 1000.0) / 2.0, (np.asarray(y) - 2000.0) / 2.0)
    error, before = np.median(np.abs(blocks[rows, cols] - truth)), np.median(np.abs(whole[rows, cols] - truth))
    assert error < 0.005 and error < before + 0.0005, (error, before)


def test_dem_of_a_model_too_large_for_the_memory_says_so(tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.engine import satellite

    ch = _mesh_chunk(tmp_path, side=60)
    _in_blocks(monkeypatch)
    monkeypatch.setattr(satellite, "_physical_memory", lambda: 50_000.0)
    with pytest.raises(MemoryError, match="decimate the model"):
        ch.build_dem(resolution=0.05, source="model")
