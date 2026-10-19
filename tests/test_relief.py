"""Relief visualisations of a DEM: sky-view factor, openness, local relief, MSRM, multi-hillshade."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.archaeo import relief

RES = 0.05


def _dem(n: int = 200):
    """A gentle slope (the trend) with a wall 30 cm high and a ditch 25 cm deep on it."""
    y, x = np.mgrid[0:n, 0:n] * RES
    z = 100.0 + 0.05 * x + 0.02 * y
    z += 0.30 * np.exp(-((x - 2.5) ** 2) / 0.02) * (np.abs(y - 5.0) < 3.5)
    z -= 0.25 * np.exp(-((y - 7.5) ** 2) / 0.01) * ((x > 4.0) & (x < 8.5))
    return z


WALL, DITCH, FLAT = (100, 50), (150, 125), (40, 150)  # (row, column) of a wall top, a ditch bottom, open ground


def test_default_radius():
    assert relief.default_radius(0.05) == 1.0
    assert relief.default_radius(0.5) == 10.0
    assert relief.default_radius(0.023) == 0.5


def test_features_stand_out():
    z = _dem()
    v = {m: relief.compute(z, RES, m, radius=1.0) for m in relief.METHODS}
    svf = v["svf"]
    assert svf[DITCH] < svf[FLAT] - 0.05 and svf[WALL] > 0.97 and svf[FLAT] > 0.97
    assert v["openness_pos"][WALL] > v["openness_pos"][FLAT] + 2
    assert v["openness_neg"][DITCH] > v["openness_neg"][FLAT] + 2
    for m, least in (("slrm", 0.1), ("msrm", 0.02)):  # the trend removed: the wall up, the ditch down
        assert v[m][WALL] > least and v[m][DITCH] < -least and abs(v[m][FLAT]) < 0.01, m
    hs = v["multi_hillshade"]
    assert np.nanmin(hs) >= 0 and np.nanmax(hs) <= 1


def test_no_data_stays_no_data():
    z = _dem(120)
    z[:20, :] = np.nan
    for m in relief.METHODS:
        out = relief.compute(z, RES, m, radius=0.5)
        assert np.isnan(out[:20]).all() and np.isfinite(out[30:, 30:]).all(), m


@pytest.mark.parametrize("method", relief.METHODS)
def test_tiles_give_the_whole_dem(tmp_path, method):
    import rasterio

    from pyarchinit_photogrammetry.engine.dem import write_geotiff

    z = _dem(150)
    dem = write_geotiff(tmp_path / "dem.tif", z.astype(np.float32), (1000.0, 2000.0), RES, "EPSG:32633",
                        nodata=relief.NODATA)
    info = relief.build_relief(dem, tmp_path / f"{method}.tif", method, radius=0.6, tile=64, workers=3)
    with rasterio.open(info["path"]) as src:
        out = src.read(1)
        assert src.crs.to_epsg() == 32633 and src.transform == rasterio.open(dem).transform
    whole = relief.compute(z.astype(np.float32).astype(np.float64), RES, method, radius=0.6)
    np.testing.assert_allclose(out, whole.astype(np.float32), rtol=1e-5, atol=1e-5)
    assert info["p2"] <= info["p98"]


def test_chunk_cli_and_view(tmp_path):
    import os

    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.engine.dem import write_geotiff

    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(tmp_path / "p.ppg")
    ch.data_dir.mkdir(parents=True, exist_ok=True)
    dem = write_geotiff(ch.data_dir / "dem.tif", _dem(120).astype(np.float32), (0.0, 6.0), RES, None,
                        nodata=relief.NODATA)
    ch._set_product("dem", dem)
    doc.save()
    path = ch.build_relief("svf", radius=0.5)
    assert ch.product_path("svf") == path and ch.meta["relief"]["svf"]["radius_m"] == 0.5
    r = CliRunner().invoke(app, ["relief", str(tmp_path / "p.ppg"), "--method", "slrm", "--radius", "0.5"])
    assert r.exit_code == 0, r.output
    assert ppg.Document.open(tmp_path / "p.ppg").chunk.product_path("slrm").exists()
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from pyarchinit_photogrammetry.gui.image_views import RasterView, relief_colors

    view = RasterView()
    view.load(path, "svf")
    lo, hi = view._dem_range
    assert 0.5 < lo < hi <= 1.0 and not view.pixmap_item.pixmap().isNull()
    rgba = relief_colors(np.array([[lo, hi, np.nan]]), "svf", (lo, hi))
    assert rgba[0, 0, 0] == 0 and rgba[0, 1, 0] == 255 and rgba[0, 2, 3] == 0
    rgba = relief_colors(np.array([[-0.1, 0.1]]), "slrm", (-0.1, 0.1))  # diverging: blue below, red above
    assert rgba[0, 0, 2] > rgba[0, 0, 0] and rgba[0, 1, 0] > rgba[0, 1, 2]
