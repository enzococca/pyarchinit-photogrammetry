"""The raster view shows a large orthomosaic or DEM from a reduced copy and reads the visible
part again from the file, up to the full resolution, when zooming in."""
from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _orthomosaic(path, size=4096, res=0.01):
    """An RGBA GeoTIFF whose every pixel can be told apart: red = column, green = row (mod 256),
    blue = a fine checkerboard that averaging turns grey."""
    rows, cols = np.mgrid[0:size, 0:size]
    rgba = np.stack([cols % 256, rows % 256, ((rows // 2 + cols // 2) % 2) * 255,
                     np.full((size, size), 255)]).astype(np.uint8)
    profile = {"driver": "GTiff", "height": size, "width": size, "count": 4, "dtype": "uint8",
               "transform": from_origin(1000.0, 2000.0, res, res), "tiled": True, "blockxsize": 256,
               "blockysize": 256, "compress": "deflate", "photometric": "RGB"}
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(rgba)
        dst.build_overviews([2, 4, 8, 16], Resampling.average)
    return rgba


def _view(qapp, path, kind="ortho"):
    from pyarchinit_photogrammetry.gui.image_views import RasterView

    view = RasterView()
    view.resize(400, 300)
    view.show()
    qapp.processEvents()
    view.load(path, kind, max_side=512)
    return view


def test_zooming_in_reads_the_full_resolution(qapp, tmp_path):
    path = tmp_path / "ortho.tif"
    rgba = _orthomosaic(path)
    view = _view(qapp, path)
    assert view.pixmap_item.pixmap().width() == 512  # an overview: the photo detail is lost
    view.update_detail(wait=True)
    assert view.detail_resolution() is None  # the whole raster on screen: no detail needed
    # zoom on 40 x 30 pixels of the reduced copy = 320 x 240 pixels of the file
    view.fitInView(view.scene_pt(1000.0 + 20.0, 2000.0 - 10.0).x(), view.scene_pt(1000.0 + 20.0, 2000.0 - 10.0).y(),
                   40, 30)
    view.update_detail(wait=True)
    assert view.detail_resolution() == pytest.approx(0.01)  # one pixel of the file per pixel
    item = view._detail_item
    img = item.pixmap().toImage()
    # a pixel of the detail is the very pixel of the file under it
    for dx, dy in ((5, 5), (60, 33), (150, 90)):
        scene = item.mapToScene(dx + 0.5, dy + 0.5)
        x, y = view.world(scene)
        col, row = int((x - 1000.0) / 0.01), int((2000.0 - y) / 0.01)
        c = img.pixelColor(dx, dy)
        assert (c.red(), c.green(), c.blue()) == tuple(rgba[:3, row, col])
    # zooming out again leaves the reduced copy alone
    view.fit()
    view.update_detail(wait=True)
    assert view.detail_resolution() is None and not view._detail_item.isVisible()
    view.close()


def test_the_detail_of_a_dem_keeps_the_colours_of_the_whole_dem(qapp, tmp_path):
    from pyarchinit_photogrammetry.gui.image_views import dem_colors

    size, res = 2048, 0.05
    rows, cols = np.mgrid[0:size, 0:size].astype(np.float32)
    z = 100.0 + 0.002 * cols + 3.0 * np.sin(rows / 300.0)
    path = tmp_path / "dem.tif"
    with rasterio.open(path, "w", driver="GTiff", height=size, width=size, count=1, dtype="float32",
                       transform=from_origin(0.0, size * res, res, res), tiled=True, blockxsize=256,
                       blockysize=256) as dst:
        dst.write(z, 1)
        dst.build_overviews([2, 4, 8], Resampling.average)
    view = _view(qapp, path, "dem")
    full = view._dem_range
    view.fitInView(100, 100, 20, 15)
    view.update_detail(wait=True)
    assert view.detail_resolution() == pytest.approx(res)
    img = view._detail_item.pixmap().toImage()
    # the window of the file coloured with the limits of the whole DEM
    (c0, r0, w, h), (oh, ow) = view._detail_key
    expect = dem_colors(z[r0:r0 + h, c0:c0 + w].astype(np.float64), res, full)
    got = np.array([[img.pixelColor(x, y).red() for x in (10, ow // 2)] for y in (10, oh // 2)])
    want = expect[[10, oh // 2]][:, [10, ow // 2], 0]
    assert np.abs(got.astype(int) - want.astype(int)).max() <= 2
    view.close()


def test_the_detail_is_read_in_a_thread(qapp, tmp_path):
    import time

    path = tmp_path / "ortho.tif"
    _orthomosaic(path, size=2048)
    view = _view(qapp, path)
    view.fitInView(50, 50, 40, 30)
    view.update_detail()  # asynchronous, as after a wheel zoom
    deadline = time.monotonic() + 10
    while view.detail_resolution() is None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert view.detail_resolution() == pytest.approx(0.01)
    view.close()
