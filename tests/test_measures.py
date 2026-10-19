"""Measures: surface area in 3D, slope and aspect, height above the ground."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.archaeo import measures
from pyarchinit_photogrammetry.engine.dem import write_geotiff


def _plane_dem(tmp_path, gx: float, gy: float, res: float = 0.05, size: int = 200):
    """A DEM of the plane z = 100 + gx * x + gy * y (x east, y north) from (0, 0) to size * res."""
    x = (np.arange(size) + 0.5) * res
    y = (size - np.arange(size) - 0.5) * res  # row 0 at the top (north)
    X, Y = np.meshgrid(x, y)
    z = (100 + gx * X + gy * Y).astype(np.float32)
    return write_geotiff(tmp_path / "dem.tif", z, (0.0, size * res), res, "EPSG:3004", nodata=-32767.0)


def test_surface_area_slope_and_aspect(tmp_path):
    import trimesh
    from shapely.geometry import box

    slope = np.tan(np.radians(30))
    dem = _plane_dem(tmp_path, slope, 0.0)  # rising to the east: facing west
    poly = box(2.0, 2.0, 7.0, 6.0)  # 5 x 4 m
    out = measures.surface_area(dem, poly)
    assert out["plan"] == pytest.approx(20.0, rel=1e-3)
    assert out["surface"] == pytest.approx(20.0 / np.cos(np.radians(30)), rel=2e-3)
    assert out["mean_slope"] == pytest.approx(30.0, abs=0.1)
    s, a = measures.slope_aspect_at(dem, 5.0, 5.0)
    assert s == pytest.approx(30.0, abs=0.01) and a == pytest.approx(270.0, abs=0.01)
    dem2 = _plane_dem(tmp_path, 0.0, 0.2)  # rising to the north: facing south
    s, a = measures.slope_aspect_at(dem2, 5.0, 5.0)
    assert s == pytest.approx(np.degrees(np.arctan(0.2)), abs=0.01) and a == pytest.approx(180.0, abs=0.01)
    assert measures.slope_aspect_at(dem2, -5.0, 5.0) is None  # outside
    import rasterio

    out_path = measures.aspect_raster(dem2, tmp_path / "aspect.tif", crs="EPSG:3004")
    with rasterio.open(out_path) as src:
        a = src.read(1)
    assert np.median(a[5:-5, 5:-5]) == pytest.approx(180.0, abs=0.01)
    # the faces of a model: the same plane, inclined 30 degrees
    v = np.array([[0, 0, 100], [10, 0, 100 + 10 * slope], [10, 10, 100 + 10 * slope], [0, 10, 100]], float)
    mesh = trimesh.Trimesh(v, [[0, 1, 2], [0, 2, 3]], process=False)
    mesh = mesh.subdivide().subdivide().subdivide().subdivide()
    out = measures.surface_area(dem, poly, model=mesh)
    assert out["model_plan"] == pytest.approx(20.0, rel=0.05)
    assert out["model_surface"] == pytest.approx(out["model_plan"] / np.cos(np.radians(30)), rel=1e-6)


def test_height_above_ground():
    rng = np.random.default_rng(0)
    n = 20000
    xy = rng.uniform(0, 10, (n, 2))
    ground = 100 + 0.1 * xy[:, 0]
    veg = rng.random(n) < 0.2
    z = ground + np.where(veg, 1.5, 0.0) + rng.normal(0, 0.003, n)
    classes = np.where(veg, 3, 2)
    hag = measures.height_above_ground(np.column_stack([xy, z]), classes)
    assert np.median(hag[veg]) == pytest.approx(1.5, abs=0.02)
    assert np.median(np.abs(hag[~veg])) < 0.01
    with pytest.raises(ValueError):
        measures.height_above_ground(np.column_stack([xy, z]), np.full(n, 1))


def test_interface(processed, tmp_path, monkeypatch):
    import os
    import shutil

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import pyarchinit_photogrammetry as ppg

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.ai.classify import classify_chunk
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    doc = ppg.Document.open(d / doc.path.name)
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    shown = []
    monkeypatch.setattr(w, "_info", lambda title, text: shown.append(text))
    x0, y0 = 2480000.0, 4500000.0
    w._area_drawn([(x0 + 1.2, y0 + 1.0), (x0 + 2.8, y0 + 1.0), (x0 + 2.8, y0 + 2.0), (x0 + 1.2, y0 + 2.0)])
    w.runner.wait()
    app.processEvents()
    import re

    plan = float(re.search(r"Area in pianta ([\d,]+) m2", shown[0]).group(1).replace(",", "."))
    assert plan == pytest.approx(1.6, abs=0.02) and "Dal modello" in shown[0]
    assert "Misure" in w.line_layers
    w._show_raster("dem")
    w._show_coords(x0 + 0.5, y0 + 0.5, 100.0)
    assert "pend." in w.coord_label.text()
    for kind, word in (("slope", "pendenza"), ("aspect", "esposizione")):
        w.terrain_raster(kind)
        w.runner.wait()
        app.processEvents()
        assert w.ortho_view.kind == kind and w.chunk.product_path(kind).exists()
        w._show_coords(x0 + 0.5, y0 + 0.5, 20.0)
        assert word in w.coord_label.text()
    # heights above the ground in the 3D view, once the ground is classified
    classify_chunk(w.chunk)
    w._show_model("point_cloud")
    w.model_view.set_color_mode("hag")
    assert not w.model_view.sd.hag_missing and len(w.model_view.sd.colors)
    w.setWindowModified(False)
    w.close()
    app.processEvents()
