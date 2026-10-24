"""Raster calculator: formulas written as in QGIS on every raster of a chunk - products, kept
versions, bands by number or name, indices, the GeoTIFFs of the plugins, earlier results - read
safely (never run as Python), on the grid of one of them, block by block, also on rasters of other
grids and in a chunk with no reference system; the plugins name their rasters and use the
calculator; the window and the command line."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine import rastercalc as rc

X0, Y0 = 2480000.0, 4500000.0  # metres as in Gauss-Boaga: float32 would round them
CRS = "EPSG:3004"
RES = 0.05  # cell of the DEM
W, H = 200, 160  # cells of the DEM (overviews from 128 cells)


def _tif(path, data, origin, res, crs=CRS, nodata=None, descriptions=None, tags=None):
    data = np.asarray(data)
    data = data[None] if data.ndim == 2 else data
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", height=data.shape[1], width=data.shape[2], count=data.shape[0],
                       dtype=data.dtype, crs=crs, transform=from_origin(origin[0], origin[1], res, res), nodata=nodata,
                       BIGTIFF="IF_SAFER") as dst:
        dst.write(data)
        for i, d in enumerate(descriptions or [], 1):
            dst.set_band_description(i, d)
        if tags:
            dst.update_tags(**tags)
    return path


def _plane(origin, res, shape, a, b, c):
    """``a x + b y + c`` at the centres of the cells (x, y in metres from the corner of the DEM)."""
    rows, cols = np.mgrid[0:shape[0], 0:shape[1]]
    x = origin[0] - X0 + (cols + 0.5) * res
    y = origin[1] - Y0 + -(rows + 0.5) * res
    return a * x + b * y + c


def _chunk(tmp_path, crs=CRS):
    """A saved project whose chunk has a DEM with a hole, an RGBA orthomosaic at half its cell with a
    transparent corner, a slope raster, a kept version of the DEM, a reflectance of three bands on a
    coarser grid shifted by half a cell, an index, a zoning of integers, a GeoTIFF of a plugin, an
    elevation and an image with no place."""
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    ch.crs = crs if crs else ch.crs
    doc.save(tmp_path / "scavo.ppg")
    data = ch.data_dir
    top = (X0, Y0 + H * RES)
    dem = _plane(top, RES, (H, W), 0.5, 0.25, 100.0).astype(np.float32)
    dem[10:20, 30:40] = -32767.0  # no data
    ch._set_product("dem", _tif(data / "dem.tif", dem, top, RES, crs, nodata=-32767.0))
    rgba = np.zeros((4, 2 * H, 2 * W), np.uint8)
    rgba[0], rgba[1], rgba[2], rgba[3] = 200, 100, 50, 255
    rgba[3, :20, :20] = 0  # a corner no photo saw
    with rasterio.open(data / "orthomosaic.tif", "w", driver="GTiff", height=2 * H, width=2 * W, count=4,
                       dtype="uint8", crs=crs, transform=from_origin(top[0], top[1], RES / 2, RES / 2),
                       photometric="RGB", BIGTIFF="IF_SAFER") as dst:
        dst.colorinterp = [rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
                           rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha]  # before the pixels,
        dst.write(rgba)  # as the program writes it: the alpha band is the mask of the others
    ch._set_product("orthomosaic", data / "orthomosaic.tif")
    ch._set_product("slope", _tif(data / "terrain" / "slope.tif", np.full((H, W), 12.0, np.float32), top, RES, crs,
                                  nodata=-32767.0))
    _tif(data / "versions" / "dem" / "v1" / "dem.tif", dem - np.where(dem > -30000, 0.5, 0).astype(np.float32), top,
         RES, crs, nodata=-32767.0)
    ch.meta["versions"] = {"dem": [{"id": "v1", "name": "Prima dello scavo", "created": "2026-10-01T09:30:00",
                                   "params": {}, "files": {"dem": "versions/dem/v1/dem.tif"}}]}
    ms_top = (X0 - 0.05, Y0 + H * RES + 0.05)  # a coarser grid, shifted
    shape = (H // 2 + 2, W // 2 + 2)
    refl = np.stack([_plane(ms_top, 0.1, shape, 0.01, 0.0, 0.1), _plane(ms_top, 0.1, shape, 0.0, 0.01, 0.2),
                     _plane(ms_top, 0.1, shape, 0.02, 0.02, 0.5)]).astype(np.float32)
    _tif(data / "multispectral" / "reflectance.tif", refl, ms_top, 0.1, crs, nodata=float("nan"),
         descriptions=["Green", "Red", "NIR"])
    _tif(data / "multispectral" / "ndvi.tif", ((refl[2] - refl[1]) / (refl[2] + refl[1])).astype(np.float32), ms_top,
         0.1, crs, nodata=float("nan"))
    zones = np.ones(shape, np.int32)
    zones[:, shape[1] // 2:] = 2
    zones[:10] = 3
    _tif(data / "multispectral" / "ndvi_zone.tif", zones, ms_top, 0.1, crs, nodata=0)
    ch.meta["multispectral"] = {"reflectance": "multispectral/reflectance.tif", "reflectance_bands": ["Green", "Red", "NIR"],
                                "indices": {"NDVI": {"file": "multispectral/ndvi.tif", "formula": "(NIR - Red) / (NIR + Red)"}},
                                "zoning": {"NDVI": {"raster": "multispectral/ndvi_zone.tif", "classes": 3}}}
    _tif(data / "terrain" / "twi.tif", np.full((H, W), 3.0, np.float32), top, RES, crs, nodata=float("nan"),
         tags={"ppg_name": "Indice di umidità"})  # a plugin's, listed nowhere
    _tif(data / "elevations" / "e1" / "relief.tif", np.ones((10, 10), np.float32), (0.0, 10.0), 0.01, None)
    (data / "varie").mkdir()
    with rasterio.open(data / "varie" / "foto.tif", "w", driver="GTiff", height=5, width=5, count=1, dtype="uint8",
                       BIGTIFF="IF_SAFER") as dst:
        dst.write(np.zeros((1, 5, 5), np.uint8))  # no place: not a map
    doc.save()
    return doc, ch, dem


# ---------------------------------------------------------------- the formulas
def _catalog():
    def r(name, bands=("",)):
        return rc.Raster(name, name, Path(f"{name}.tif"), "product", list(bands))

    return [r("dem"), r("slope"), r("orthomosaic", ("", "", "", "")), r("reflectance", ("Green", "Red", "NIR"))]


def test_formulas_written_as_in_qgis():
    cat = _catalog()
    dem = np.array([[100.0, 101.0, np.nan], [99.0, 100.0, 103.0]])
    slope = np.array([[10.0, 40.0, 5.0], [20.0, np.nan, 30.0]])
    nir, red = np.full((2, 3), 0.6), np.full((2, 3), 0.2)
    values = {("dem", 1): dem, ("slope", 1): slope, ("reflectance", 3): nir, ("reflectance", 2): red,
              ("orthomosaic", 1): np.full((2, 3), 255.0)}

    def run(text):
        return rc.parse(text, cat).evaluate(values)

    assert np.allclose(run('"dem@1" * 2 + 1'), dem * 2 + 1, equal_nan=True)
    assert np.allclose(run("2 * dem^2"), 2 * dem ** 2, equal_nan=True)  # ^ is the power, before *
    assert np.allclose(run("(reflectance@NIR - reflectance@Red) / (reflectance@NIR + reflectance@Red)"), 0.5)
    assert np.allclose(run('reflectance@3 - "reflectance@Red"'), 0.4) and np.allclose(run("orthomosaic@1 / 255"), 1.0)
    both = run("dem > 99.5 AND slope < 35")
    assert np.array_equal(both, [[1.0, 0.0, np.nan], [0.0, np.nan, 1.0]], equal_nan=True)  # no data stays no data
    assert np.array_equal(run("dem > 99.5 and not slope < 35"), [[0.0, 1.0, np.nan], [0.0, np.nan, 0.0]],
                          equal_nan=True)
    assert np.array_equal(run("dem = 100 OR dem <> 100"), [[1.0, 1.0, np.nan], [1.0, 1.0, 1.0]], equal_nan=True)
    assert np.array_equal(run("if(slope > 25, 1, 0)"), [[0.0, 1.0, 0.0], [0.0, np.nan, 1.0]], equal_nan=True)
    assert np.array_equal(run("coalesce(dem, 0)"), [[100.0, 101.0, 0.0], [99.0, 100.0, 103.0]])
    assert np.array_equal(run("isnan(slope)"), [[0, 0, 0], [0, 1, 0]])
    assert np.allclose(run("max(dem, slope * 4) - min(dem, 100) + sqrt(abs(-4)) * pi / pi"),
                       np.maximum(dem, slope * 4) - np.minimum(dem, 100) + 2, equal_nan=True)
    assert np.isnan(run("dem / 0")).all()  # infinities are no data
    # also for what follows: a comparison, coalesce, isnan
    assert np.isnan(run("(dem / 0) > 1")).all() and np.isnan(run("exp(dem * 100) > 0")).all()
    assert np.array_equal(run("coalesce(dem / 0, 5)"), np.full((2, 3), 5.0))
    assert np.array_equal(run("isnan(ln(dem - dem))"), np.ones((2, 3)))
    # a number with a raster in AND, OR
    assert np.array_equal(run("dem > 100 AND 1"), run("dem > 100"), equal_nan=True)
    assert np.array_equal(run("1 OR dem > 100"), [[1.0, 1.0, np.nan], [1.0, 1.0, 1.0]], equal_nan=True)
    f = rc.parse("slope + dem@1 - dem", cat)
    assert [(r.name, b) for r, b in f.inputs] == [("slope", 1), ("dem", 1)] and [r.name for r in f.rasters] == ["slope", "dem"]


@pytest.mark.parametrize("text, code, message", [
    ("", "empty", "write a formula"),
    ("dem +", "syntax", "cannot be read"),
    ("demm * 2", "unknown", "no raster named demm (perhaps dem?)"),
    ("orthomosaic + 1", "bands", "orthomosaic has 4 bands: write orthomosaic@1 ... orthomosaic@4"),
    ("dem@2", "band_number", "dem has 1 band(s): dem@2 does not exist"),
    ("reflectance@SWIR", "band_name", "reflectance has no band SWIR (bands: Green, Red, NIR)"),
    ("dem > 1 & slope < 3", "and_or", "write AND, OR"),
    ("~dem", "not", "write NOT"),
    ("dem @ slope", "operator", "the operator MatMult is not in the formulas"),
    ("dem.real", "not_allowed", "not in a formula: Attribute"),
    ("__import__('os').system('x')", "function", "is not a function of the formulas"),
    ("open('x')", "function", "open is not a function of the formulas"),
    ("[dem]", "not_allowed", "List"),
    ("(lambda: 1)()", "function", "this is not a function"),
    ("'abc'", "numbers", "numbers and rasters only"),
    ("sqrt(dem, 2)", "arguments", "sqrt takes 1 value(s)"),
    ("min(dem)", "arguments", "min takes at least 2 value(s)"),
    ("clip(dem, lo=1, hi=2)", "unknown", "no raster named lo"),  # = is the equality of QGIS
    ("sqrt + 1", "is_function", "sqrt is a function"),
    ("dem is slope", "compare", "compare with"),
])
def test_what_a_formula_cannot_have(text, code, message):
    with pytest.raises(rc.FormulaError, match=None) as err:
        rc.parse(text, _catalog())
    assert err.value.code == code and message in str(err.value), str(err.value)


def test_names_of_the_results():
    cat = _catalog()
    assert rc.check_name(None, "scavo_1", cat) == "scavo_1"
    for bad in ("1scavo", "a b", "", "__x", "pi", "sqrt", "and", "AND", "if", "lambda"):
        with pytest.raises(rc.FormulaError):
            rc.check_name(None, bad, cat)
    with pytest.raises(rc.FormulaError, match="there is a raster of the chunk"):
        rc.check_name(None, "dem", cat)
    assert [rc.slug(s) for s in ("Prima dello scavo", "2024 rilievo", "Umidità TWI", "pi", "if", "-")] == \
        ["prima_dello_scavo", "r_2024_rilievo", "umidita_twi", "pi_r", "if_r", "raster"]


# ---------------------------------------------------------------- the rasters of a chunk and the result
def test_every_raster_of_the_chunk_by_its_name(tmp_path):
    doc, ch, _dem = _chunk(tmp_path)
    found = {r.name: r for r in rc.rasters(ch)}
    assert set(found) == {"dem", "orthomosaic", "slope", "dem_prima_dello_scavo", "reflectance", "ndvi", "ndvi_zone",
                          "terrain_twi"}  # not the elevation (a frame of its own), not the image with no place
    assert found["reflectance"].bands == ["Green", "Red", "NIR"] and len(found["orthomosaic"].bands) == 4
    assert found["terrain_twi"].label == "Indice di umidità" and found["terrain_twi"].source == "file"
    assert found["ndvi_zone"].integer and not found["dem"].integer and found["dem"].resolution == pytest.approx(RES)
    # a plugin names its own rasters: the same file, not twice
    entry = rc.register(lambda chunk: [{"name": "Umidità TWI", "label": "TWI", "path": chunk.data_dir / "terrain" / "twi.tif"}],
                        owner="terrain")
    try:
        found = {r.name: r for r in rc.rasters(ch)}
        assert "umidita_twi" in found and "terrain_twi" not in found and found["umidita_twi"].source == "plugin"
    finally:
        rc.unregister(entry)
    assert "terrain_twi" in {r.name for r in rc.rasters(ch)}


def test_a_formula_on_rasters_of_other_grids_block_by_block(tmp_path):
    doc, ch, dem = _chunk(tmp_path)
    seen = []
    info = rc.calculate(ch, "dem - dem_prima_dello_scavo", "scavato", progress=lambda f, m: seen.append(m), block=48)
    path = ch.data_dir / info["file"]
    assert info["grid"] == "dem" and info["size"] == [W, H] and info["inputs"] == ["dem@1", "dem_prima_dello_scavo@1"]
    with rasterio.open(path) as src:
        out = src.read(1)
        assert src.crs.to_epsg() == 3004 and src.transform == from_origin(X0, Y0 + H * RES, RES, RES)
        assert src.overviews(1) == [2] and src.descriptions[0] == "scavato" and src.tags()["ppg_formula"]
    hole = dem < -30000
    assert np.isnan(out[hole]).all() and np.allclose(out[~hole], 0.5)
    assert info["stats"]["cells"] == (~hole).sum() and info["stats"]["min"] == pytest.approx(0.5)
    assert any("block 12/" in m for m in seen)
    # the reflectance on its coarser grid, shifted: resampled onto the grid of the DEM (bilinear: a plane is exact)
    info = rc.calculate(ch, "(reflectance@NIR - reflectance@Red) / (reflectance@NIR + reflectance@Red)", "ndvi_dem",
                        grid="dem", block=64)
    with rasterio.open(ch.data_dir / info["file"]) as src:
        out = src.read(1)
    top = (X0, Y0 + H * RES)
    nir, red = _plane(top, RES, (H, W), 0.02, 0.02, 0.5), _plane(top, RES, (H, W), 0.0, 0.01, 0.2)
    inner = (slice(4, H - 4), slice(4, W - 4))
    assert np.allclose(out[inner], ((nir - red) / (nir + red))[inner], atol=1e-5)
    # the transparent corner of the orthomosaic has no data; the zones stay classes (nearest)
    info = rc.calculate(ch, "orthomosaic@1 / 255 + ndvi_zone * 10", "colore_zone", grid="dem")
    with rasterio.open(ch.data_dir / info["file"]) as src:
        out = src.read(1)
    assert np.isnan(out[:10, :10]).all()
    assert set(np.unique(np.round(out[20:-4, 20:-4] - 200 / 255, 4))) <= {10.0, 20.0, 30.0}
    # at another resolution, a result made again in place, a name taken, the catalog with the results
    info = rc.calculate(ch, "slope * 2", "scavato", resolution=0.1)
    assert info["size"] == [W // 2, H // 2] and info["stats"]["max"] == pytest.approx(24.0)
    assert list(ch.meta["calculator"]) == ["scavato", "ndvi_dem", "colore_zone"]
    found = {r.name: r for r in rc.rasters(ch)}
    assert found["scavato"].source == "calculator" and found["scavato"].width == W // 2
    assert np.allclose(rc.calculate(ch, "scavato + 1", "piu_uno")["stats"]["min"], 25.0)
    with pytest.raises(rc.FormulaError, match="there is a raster of the chunk"):
        rc.calculate(ch, "dem", "slope")
    with pytest.raises(rc.FormulaError, match="no raster named nessuno for the grid"):
        rc.calculate(ch, "dem", "x", grid="nessuno")
    assert not list((ch.data_dir / "calculator").glob("*.tmp.tif"))


def test_a_chunk_with_no_reference_system(tmp_path):
    """The rasters of a local chunk have no CRS: those of other grids are resampled all the same."""
    doc, ch, _dem = _chunk(tmp_path, crs=None)
    info = rc.calculate(ch, "reflectance@NIR * 10 + dem * 0", "locale", grid="dem")
    with rasterio.open(ch.data_dir / info["file"]) as src:
        out = src.read(1)
        assert src.crs is None
    top = (X0, Y0 + H * RES)
    want = _plane(top, RES, (H, W), 0.02, 0.02, 0.5) * 10
    inner = (slice(25, H - 4), slice(45, W - 4))  # away from the hole of the DEM
    assert np.allclose(out[inner], want[inner], atol=1e-4)


def test_a_rotated_grid_for_the_result(tmp_path):
    """A rotated grid (a geophysical survey) as the grid of the result: the DEM resampled onto all of
    it, block by block."""
    from affine import Affine

    doc, ch, _dem = _chunk(tmp_path)
    n, res = 30, 0.1
    turned = Affine.translation(X0 + 5.0, Y0 + 4.5) @ Affine.rotation(30) @ Affine.scale(res, -res)  # in the DEM
    (ch.data_dir / "geophysics").mkdir()
    with rasterio.open(ch.data_dir / "geophysics" / "gpr.tif", "w", driver="GTiff", height=n, width=n, count=1,
                       dtype="float32", crs=CRS, transform=turned, nodata=float("nan"), BIGTIFF="IF_SAFER") as dst:
        dst.write(np.ones((1, n, n), np.float32))
    info = rc.calculate(ch, "geophysics_gpr * 0 + dem", "ruotato", block=8)
    with rasterio.open(ch.data_dir / info["file"]) as src:
        out = src.read(1)
        assert src.transform.almost_equals(turned)
    rows, cols = np.mgrid[0:n, 0:n]
    xs, ys = turned @ (cols + 0.5, rows + 0.5)
    want = 0.5 * (xs - X0) + 0.25 * (ys - Y0) + 100.0  # the plane of the DEM at the centres of the turned cells
    assert np.isfinite(out).all() and np.allclose(out, want, atol=5e-3)  # cells twice the DEM's: averaged


# ---------------------------------------------------------------- plugins, window, command line
def test_a_plugin_names_its_rasters_and_reads_the_catalog(tmp_path):
    from pyarchinit_photogrammetry.plugins.api import Context

    doc, ch, _dem = _chunk(tmp_path)
    context = Context("terrain", tmp_path / "plugin")
    context.add_rasters(lambda chunk: [{"name": "twi", "label": "Indice topografico di umidità",
                                        "path": chunk.data_dir / "terrain" / "twi.tif"}])
    try:
        found = {r["name"]: r for r in context.rasters(ch)}
        assert found["twi"]["label"] == "Indice topografico di umidità" and found["twi"]["source"] == "plugin"
        assert found["reflectance"]["bands"] == ["Green", "Red", "NIR"]
        info = rc.calculate(ch, "twi * slope", "umido_ripido")
        assert info["stats"]["max"] == pytest.approx(36.0)
    finally:
        context.release()
    assert "twi" not in {r["name"] for r in context.rasters(ch)}


def test_the_calculator_in_the_window(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import i18n, main_window as mw
    from pyarchinit_photogrammetry.gui.raster_calculator_dialog import RasterCalculatorDialog

    doc, ch, _dem = _chunk(tmp_path)
    # the dialog: the formula checked while it is written, a double click writes a raster
    i18n.set_current("it")
    d = RasterCalculatorDialog(rc.rasters(ch))
    ok = d.buttons.button(d.buttons.StandardButton.Ok)
    assert d.name.text() == "calcolo_1" and not ok.isEnabled() and d.state.text() == "Scrivi una formula."
    d.formula.setText("orthomosaic + dem")
    assert not ok.isEnabled() and d.state.text() == "orthomosaic ha 4 bande: scrivi orthomosaic@1 ... orthomosaic@4."
    d.formula.setText("demm")
    assert d.state.text() == "Nessun raster si chiama demm (forse dem?)."
    d.formula.clear()
    row = next(i for i, r in enumerate(d.rasters) if r.name == "orthomosaic")
    d._insert(row)
    d.formula.insert(" / 255 - ")
    d._insert(next(i for i, r in enumerate(d.rasters) if r.name == "slope"))
    assert d.formula.text() == "orthomosaic@1 / 255 - slope" and ok.isEnabled()
    assert d.state.text() == "Legge orthomosaic@1, slope; il risultato sulla griglia di orthomosaic."
    d.name.setText("dem")
    assert not ok.isEnabled() and "c'è già un raster" in d.state.text()
    d.close()
    # the window: the result built, shown, listed in the workspace and removed from there
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.show()
    app.processEvents()
    try:
        monkeypatch.setattr(RasterCalculatorDialog, "ask", classmethod(lambda cls, catalog, parent=None, formula="", name=None: {
            "formula": "dem - dem_prima_dello_scavo", "name": "scavato", "grid": None, "resolution": None}))
        w.raster_calculator()
        w.runner.wait()
        app.processEvents()
        v = w.ortho_view
        assert v.kind == "calc" and v.path == ch.data_dir / "calculator" / "scavato.tif"
        assert "scavato = dem - dem_prima_dello_scavo" in w.statusBar().currentMessage()
        items = [it for it in w.workspace._walk() if it.data(0, Qt.ItemDataRole.UserRole)
                 and it.data(0, Qt.ItemDataRole.UserRole)[2:] == ("calc:scavato",)]
        assert len(items) == 1 and items[0].text(0) == "scavato = dem - dem_prima_dello_scavo"
        w._show_coords(X0 + 3.0, Y0 + 3.0, 0.5)
        assert "valore 0,5" in w.coord_label.text()
        w._show_raster("dem")
        w._workspace_item("product", ch, "calc:scavato")
        assert w.ortho_view.kind == "calc"
        assert w._removable(ch, "product", "calc:scavato")
        from pyarchinit_photogrammetry.core import removal

        part = removal.make_part(ch, "product", "calc:scavato")
        assert part.capture(ch)["files"][0] == ch.data_dir / "calculator" / "scavato.tif"
    finally:
        w.setWindowModified(False)
        w.close()


def test_the_command_line(tmp_path):
    doc, ch, _dem = _chunk(tmp_path)
    env = dict(os.environ, PPG_NO_PLUGINS="1")
    run = [sys.executable, "-m", "pyarchinit_photogrammetry.cli", "calc", str(doc.path)]
    out = subprocess.run(run + ["--list"], capture_output=True, text=True, env=env, timeout=300, close_fds=False)
    assert out.returncode == 0 and "reflectance" in out.stdout and "@3 NIR" in out.stdout, out.stdout + out.stderr
    out = subprocess.run(run + ["slope * 2", "--name", "doppio"], capture_output=True, text=True, env=env, timeout=300,
                         close_fds=False)
    assert out.returncode == 0 and "values 24 to 24" in out.stdout, out.stdout + out.stderr
    meta = json.loads(doc.path.read_text())["chunks"][0]["meta"]["calculator"]
    assert meta["doppio"]["formula"] == "slope * 2"
    out = subprocess.run(run + ["slope +", "--name", "rotto"], capture_output=True, text=True, env=env, timeout=300,
                         close_fds=False)
    assert out.returncode != 0 and "cannot be read" in out.stdout + out.stderr
