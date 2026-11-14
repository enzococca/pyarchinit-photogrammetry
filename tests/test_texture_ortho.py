"""Orthophotos from the texture of the model (engine/texture_ortho) for chunks without photos, in tiles: the same
pixels whatever the tile, and wider than the 32,767 pixels of OpenCV's remap. Synthetic textured models in a local
frame (no real place)."""
from __future__ import annotations

import numpy as np
import pytest
import rasterio
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.engine import elevation as E
from pyarchinit_photogrammetry.engine import model_merge, texture_ortho

from engine_gaps_helpers import checker, grid_mesh, new_doc, textured_chunk

COLOUR = (200, 150, 100)


def _plane(tmp_path, cells=8, page=256, side=1.0, shift=(1000.0, 2000.0, 50.0), crs="EPSG:32633"):
    doc = new_doc(tmp_path)
    x0, y0 = 500000.0, 4400000.0  # synthetic map coordinates
    xyz, f, uv = grid_mesh(x0, y0, x0 + side, y0 + side, 10, 10, z=lambda x, y: 10.0 + 0.0 * x)
    ch = textured_chunk(doc, "Importato", xyz, f, uv, [checker(page, page, COLOUR, cells=cells)],
                        shift=(x0, y0, shift[2]), crs=crs)
    doc.save()
    return doc, ch, (x0, y0)


def _expected(x, y, x0, y0, side=1.0, cells=8):
    on = ((np.floor((x - x0) / side * cells) + np.floor((y0 + side - y) / side * cells)) % 2 == 0)
    return np.where(on[..., None], np.array(COLOUR), np.array(COLOUR) // 2)


def test_auto_orthophoto_from_the_texture_without_photos(tmp_path):
    doc, ch, (x0, y0) = _plane(tmp_path)
    path = ch.build_orthomosaic(resolution=0.01)
    meta = ch.meta["orthomosaic"]
    assert meta["source"] == "texture" and meta["blending"] == "texture" and meta["resolution"] == 0.01
    with rasterio.open(path) as src:
        img = src.read()
        assert src.crs.to_epsg() == 32633 and src.res == (0.01, 0.01)
        assert src.transform.c == pytest.approx(x0) and src.transform.f == pytest.approx(y0 + 1.0)
        rows, cols = np.mgrid[0:src.height, 0:src.width]
        x = src.transform.c + (cols + 0.5) * 0.01
        y = src.transform.f - (rows + 0.5) * 0.01
    assert img.shape == (4, 100, 100) and (img[3] == 255).all()
    exp = _expected(x, y, x0, y0)
    # away from the edges of the squares (JPEG and the bilinear sample blur them) the colours are the texture's
    fx = ((x - x0) * 8) % 1
    fy = ((y0 + 1 - y) * 8) % 1
    inner = (fx > 0.2) & (fx < 0.8) & (fy > 0.2) & (fy < 0.8)
    diff = np.abs(np.moveaxis(img[:3], 0, -1).astype(int) - exp)[inner]
    assert np.median(diff) <= 2 and diff.max() < 25
    assert ch.orthomosaic_path == path
    doc.save()
    # a texture orthophoto built again with its saved parameters stays one
    assert ch.build_orthomosaic(resolution=0.02, blending="texture") == path
    assert ch.meta["orthomosaic"]["source"] == "texture"


def test_supersampling_averages_fine_texture(tmp_path):
    _doc, ch, _ = _plane(tmp_path, cells=64, page=512)
    coarse = 1.0 / 32  # two squares of the checkerboard per cell: their mean
    ch.build_orthomosaic(resolution=coarse)
    assert ch.meta["orthomosaic"]["supersample"] == 4
    with rasterio.open(ch.orthomosaic_path) as src:
        img = src.read()[:3].reshape(3, -1).astype(float)
    mean = (np.array(COLOUR) + np.array(COLOUR) // 2) / 2
    assert np.abs(np.median(img, axis=1) - mean[::1]).max() < 8


def test_tiles_give_the_same_pixels(tmp_path):
    _doc, ch, _ = _plane(tmp_path)
    a = texture_ortho.build_orthomosaic(ch, 0.007, out_path=tmp_path / "a.tif", tile=37)
    b = texture_ortho.build_orthomosaic(ch, 0.007, out_path=tmp_path / "b.tif", tile=4096)
    with rasterio.open(a) as sa, rasterio.open(b) as sb:
        assert sa.shape == sb.shape and np.array_equal(sa.read(), sb.read())


def test_a_strip_wider_than_32767_pixels(tmp_path):
    """A strip 41 m long and 2 cm wide at 1 mm: 41,000 x 20 px, beyond the 32,767 px of cv2.remap."""
    doc = new_doc(tmp_path)
    length = 41.0
    xyz, f, uv = grid_mesh(0.0, 0.0, length, 0.02, 410, 1)
    page = np.zeros((8, 4096, 3), np.uint8)
    page[..., 2] = np.linspace(0, 255, 4096).astype(np.uint8)[None]  # red rising along the strip (BGR)
    ch = textured_chunk(doc, "Striscia", xyz, f, uv, [page])
    path = ch.build_orthomosaic(resolution=0.001)
    with rasterio.open(path) as src:
        assert src.width == 41000 and src.height == 20 and src.width > 32767
        mid = src.read(window=rasterio.windows.Window(0, 10, src.width, 1))
    red = mid[0, 0].astype(float)
    assert (mid[3, 0] == 255).all()
    x = (np.arange(41000) + 0.5) / 41000
    assert np.abs(red - x * 255)[50:-50].max() < 4  # the gradient all along, no tile seam


def test_elevation_from_the_texture(tmp_path):
    doc = new_doc(tmp_path)
    xyz, f, uv = grid_mesh(0.0, 0.0, 2.0, 1.0, 20, 10)
    wall = np.column_stack([xyz[:, 0], np.zeros(len(xyz)), xyz[:, 1]])  # the plane y = 0, z up
    ch = textured_chunk(doc, "Muro", wall, f, uv, [checker(256, 128, COLOUR)])
    frame = E.plane_from_view(np.array([1.0, 0.0, 0.5]), np.array([0.0, 1.0, 0.0]), np.array([0.0, 0.0, 1.0]))
    entry = E.build_elevation(ch, frame, resolution=0.01, depth=(-0.5, 0.5))
    assert entry["blending"] == "texture" and entry["source"] == "texture"
    ortho, relief = E.elevation_paths(ch, entry)
    with rasterio.open(ortho) as src:
        img = src.read()
    seen = img[3] == 255
    assert seen.mean() > 0.9
    rgb = img[:3][:, seen].T.astype(int)
    near = np.minimum(np.abs(rgb - np.array(COLOUR)).sum(1), np.abs(rgb - np.array(COLOUR) // 2).sum(1))
    assert np.median(near) < 10


def test_merged_model_orthophoto_and_errors(tmp_path):
    doc = new_doc(tmp_path)
    xyz, f, uv = grid_mesh(0.0, 0.0, 1.0, 1.0, 10, 10)
    a = textured_chunk(doc, "A", xyz, f, uv, [checker(128, 128, (250, 0, 0))])
    xyz, f, uv = grid_mesh(1.5, 0.0, 2.5, 1.0, 10, 10)
    b = textured_chunk(doc, "B", xyz, f, uv, [checker(128, 128, (0, 0, 250))])
    merged, _ = model_merge.merge_models(doc, [a, b])
    merged.build_orthomosaic(resolution=0.01)
    with rasterio.open(merged.orthomosaic_path) as src:
        img = src.read()
    assert img.shape[2] == 250 and (img[3, :, 100:150] == 0).all()  # the gap between the two stays transparent
    assert img[0, :, :100][img[3, :, :100] > 0].mean() > img[2, :, :100][img[3, :, :100] > 0].mean()
    bare = doc.add_chunk("vuoto")
    with pytest.raises(RuntimeError, match="neither aligned photos nor a model"):
        bare.build_orthomosaic()


def test_cli_ortho_from_the_texture(tmp_path):
    doc, ch, _ = _plane(tmp_path)
    res = CliRunner().invoke(app, ["ortho", str(doc.path), "--resolution", "0.02", "--chunk", "Importato"])
    assert res.exit_code == 0, res.output
    assert "texture of the model" in res.output
    again = ppg.Document.open(doc.path)
    assert again.chunks[0].meta["orthomosaic"]["source"] == "texture"


def test_dem_surface_of_the_photo_orthophoto_wider_than_32767_cells(tmp_path):
    """The photo orthophoto reads its DEM one window per tile (engine/ortho._DemSurface): a DEM of 40,000 x 3 cells
    gives its heights at the far end, where a whole-raster cv2.remap would stop."""
    from pyarchinit_photogrammetry.engine.dem import NODATA, write_geotiff
    from pyarchinit_photogrammetry.engine.ortho import _DemSurface

    z = np.tile(np.linspace(0.0, 40.0, 40000, dtype=np.float32), (3, 1))
    path = write_geotiff(tmp_path / "dem.tif", z, (0.0, 3.0), 1.0, "LOCAL", nodata=NODATA)
    surf = _DemSurface(path, 0.5)
    assert surf.shape == (6, 80000)
    heights = surf.tile(2, 79488, 2, 512)
    x = (79488 + np.arange(512) + 0.5) * 0.5
    expected = np.interp(x - 0.5, np.arange(40000), np.linspace(0.0, 40.0, 40000))
    assert np.allclose(heights[0], expected, atol=1e-3)


def test_elevation_from_the_texture_keeps_to_its_depth_band(tmp_path):
    """A wall over half the width in the band, another one behind it (out of the band) over the whole width: the half
    without the front wall stays transparent, it does not show the wall behind."""
    doc = new_doc(tmp_path)
    xyz1, f1, uv1 = grid_mesh(0.0, 0.0, 1.0, 1.0, 10, 10)
    xyz2, f2, uv2 = grid_mesh(0.0, 0.0, 2.0, 1.0, 20, 10)
    front = np.column_stack([xyz1[:, 0], np.zeros(len(xyz1)), xyz1[:, 1]])
    back = np.column_stack([xyz2[:, 0], np.zeros(len(xyz2)), xyz2[:, 1]])
    walls = []
    for depth in (0.3, -0.3):
        b = back.copy()
        b[:, 1] = depth
        walls.append(b)
    xyz = np.vstack([front] + walls)
    faces = np.vstack([f1, f2 + len(front), f2 + len(front) + len(back)])
    uv = np.vstack([uv1, uv2, uv2])
    ch = textured_chunk(doc, "Muri", xyz, faces, uv, [checker(64, 64, COLOUR)])
    frame = E.plane_from_view(np.array([1.0, 0.0, 0.5]), np.array([0.0, 1.0, 0.0]), np.array([0.0, 0.0, 1.0]))
    entry = E.build_elevation(ch, frame, resolution=0.02, depth=(-0.1, 0.1), bounds=(-1.0, 0.0, 1.0, 1.0))
    ortho, _relief = E.elevation_paths(ch, entry)
    with rasterio.open(ortho) as src:
        alpha = src.read(4)
    cols = alpha.mean(axis=0) / 255.0
    assert 0.4 < (cols > 0.5).mean() < 0.6  # half the columns seen (the front wall), the other half transparent


def test_elevation_without_photos_and_texture_keeps_its_relief(tmp_path):
    """A model with no texture in a chunk with no aligned photo (an imported mesh with vertex colours): the
    elevation is built as before the texture orthophoto existed - the relief, an empty orthophoto - and only an
    explicit ``texture=True`` is refused."""
    doc = new_doc(tmp_path)
    xyz, f, uv = grid_mesh(0.0, 0.0, 2.0, 1.0, 20, 10)
    wall = np.column_stack([xyz[:, 0], np.zeros(len(xyz)), xyz[:, 1]])
    ch = textured_chunk(doc, "Muro", wall, f, uv, [checker(256, 128, COLOUR)])
    (ch.data_dir / "model_texture.jpg").unlink()
    assert not texture_ortho.has_texture(ch)
    frame = E.plane_from_view(np.array([1.0, 0.0, 0.5]), np.array([0.0, 1.0, 0.0]), np.array([0.0, 0.0, 1.0]))
    entry = E.build_elevation(ch, frame, resolution=0.01, depth=(-0.5, 0.5))
    assert entry.get("source") != "texture"
    ortho, relief = E.elevation_paths(ch, entry)
    with rasterio.open(ortho) as src:
        assert (src.read(4) == 0).all()
    with rasterio.open(relief) as src:
        d = src.read(1, masked=True)
    assert d.count() > 0.9 * d.size and abs(float(d.mean())) < 1e-3
    with pytest.raises(RuntimeError, match="no texture"):
        E.build_elevation(ch, frame, resolution=0.01, depth=(-0.5, 0.5), texture=True)
