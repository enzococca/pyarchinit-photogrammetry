"""Munsell colour of the units: colour chart calibration, notation, colour of an area."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.archaeo import munsell as mu

colour = pytest.importorskip("colour")

CHECKER = np.array([[115, 81, 67], [199, 147, 129], [91, 122, 156], [90, 108, 64], [130, 128, 176],
                    [92, 190, 172], [224, 124, 47], [68, 91, 170], [198, 82, 97], [94, 58, 106],
                    [159, 189, 63], [230, 162, 39], [35, 63, 147], [67, 149, 74], [180, 49, 57],
                    [238, 198, 20], [193, 84, 151], [0, 136, 170], [245, 245, 243], [200, 202, 202],
                    [161, 163, 163], [121, 121, 122], [82, 84, 86], [49, 49, 51]], float)  # OpenCV's reference, sRGB


def _srgb_of(notation: str) -> np.ndarray:
    """sRGB (D65) of a Munsell notation, through the renotation data (illuminant C)."""
    obs = colour.CCS_ILLUMINANTS["CIE 1931 2 Degree Standard Observer"]
    xyz = colour.xyY_to_XYZ(colour.munsell_colour_to_xyY(notation))
    xyz = colour.chromatic_adaptation(xyz, colour.xy_to_XYZ(obs["C"]), colour.xy_to_XYZ(obs["D65"]),
                                      method="Von Kries", transform="Bradford")
    return np.clip(colour.XYZ_to_sRGB(xyz), 0, 1)


@pytest.mark.parametrize("chip", ["10YR 5/4", "7.5YR 3/2", "2.5Y 6/3", "5YR 4/6", "10R 3/6"])
def test_soil_chips_come_back(chip):
    m = mu.munsell(_srgb_of(chip))
    assert m["chip"] == chip, m


def test_neutral_and_hue_names():
    assert mu.munsell([0.5, 0.5, 0.5])["chip"] == "N 5/"
    assert mu._hue_name(mu._hue_angle(9.1, "YR")) == "10YR"
    assert mu._hue_name(mu._hue_angle(0.6, "Y")) == "10YR"  # 0.6Y is nearer to 10YR than to 2.5Y
    assert mu._hue_name(mu._hue_angle(2.9, "Y")) == "2.5Y"


def _chart(cast):
    import cv2

    img = np.full((900, 1200, 3), 120, np.uint8)
    x0, y0, s, g = 200, 200, 120, 25
    cv2.rectangle(img, (x0 - 30, y0 - 30), (x0 + 6 * (s + g) - g + 30, y0 + 4 * (s + g) - g + 30), (20, 20, 20), -1)
    seen = np.clip((CHECKER / 255) ** 2.2 * cast, 0, 1) ** (1 / 2.2) * 255  # a cast in linear light
    for i, c in enumerate(seen):
        r, k = divmod(i, 6)
        cv2.rectangle(img, (x0 + k * (s + g), y0 + r * (s + g)), (x0 + k * (s + g) + s, y0 + r * (s + g) + s),
                      tuple(int(round(v)) for v in c), -1)
    return img  # RGB


def test_chart_calibration_removes_a_cast():
    cast = np.array([1.15, 1.0, 0.8])
    cal = mu.calibrate_chart(_chart(cast))
    assert cal["loss"] < 3.0
    seen = np.clip((CHECKER / 255) ** 2.2 * cast, 0, 1) ** (1 / 2.2)
    fixed = mu.correct(seen, cal["ccm"]) * 255
    greys = fixed[18:24]  # the neutral row comes back neutral
    assert np.abs(greys - greys.mean(axis=1, keepdims=True)).max() < 4
    assert np.abs(fixed - CHECKER).mean() < 6 < np.abs(seen * 255 - CHECKER).mean()
    with pytest.raises(ValueError):
        mu.calibrate_chart(np.full((400, 400, 3), 128, np.uint8))


def test_area_colour(tmp_path):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.engine.dem import write_geotiff

    soil = np.round(_srgb_of("10YR 5/4") * 255).astype(np.uint8)
    img = np.zeros((200, 200, 3), np.uint8)
    img[:] = soil
    rng = np.random.default_rng(0)
    spots = rng.random((200, 200)) < 0.05
    img[spots] = 0  # shadows and pebbles
    img[rng.random((200, 200)) < 0.03] = 255  # glare
    img[:, 150:] = (200, 30, 30)  # something else beside the unit
    path = write_geotiff(tmp_path / "ortho.tif", np.moveaxis(img, -1, 0), (100.0, 200.0), 0.01, None)
    out = mu.area_colour(path, box(100.1, 198.2, 101.4, 199.9))  # 1.3 x 1.7 m inside the soil
    assert out["chip"] == "10YR 5/4" and out["pixels"] > 10000


@pytest.fixture
def surveyed(tmp_path):
    """A project whose orthomosaic shows a colour chart (with a warm cast) and a patch of soil."""
    import cv2

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.engine.dem import write_geotiff

    cast = np.array([1.15, 1.0, 0.8])
    img = np.zeros((1400, 1600, 3), np.uint8)
    img[:] = 128
    img[:900, :1200] = _chart(cast)
    soil = np.clip((_srgb_of("10YR 5/4") ** 2.2) * cast, 0, 1) ** (1 / 2.2) * 255
    img[1000:1400, 0:600] = soil.round().astype(np.uint8)
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(tmp_path / "p.ppg")
    ch.data_dir.mkdir(parents=True, exist_ok=True)
    rgba = np.dstack([img, np.full(img.shape[:2], 255, np.uint8)])
    ortho = write_geotiff(ch.data_dir / "orthomosaic.tif", np.moveaxis(rgba, -1, 0), (0.0, 14.0), 0.01, None)
    dem = write_geotiff(ch.data_dir / "dem.tif", np.full((140, 160), 100.0, np.float32), (0.0, 14.0), 0.1, None,
                        nodata=-32767.0)
    ch._set_product("orthomosaic", ortho)
    ch._set_product("dem", dem)
    doc.save()
    assert cv2 is not None
    return doc


def test_cli_calibrates_and_measures(surveyed):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    soil = "0.5,0.5,5.5,3.5"  # the soil: rows 1000-1400, columns 0-600 of 1 cm pixels
    r = CliRunner().invoke(app, ["munsell", str(surveyed.path), "--area", soil])
    assert r.exit_code == 0, r.output
    assert "not calibrated" in r.output and "10YR 5/4" not in r.output  # the warm cast shows
    r = CliRunner().invoke(app, ["munsell", str(surveyed.path), "--chart", "0,5,12,14", "--area", soil])
    assert r.exit_code == 0, r.output
    assert "10YR 5/4" in r.output and "not calibrated" not in r.output


def test_window_calibration_and_area(surveyed, tmp_path, monkeypatch):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = mw.MainWindow(surveyed, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.show()
    app.processEvents()
    shown = []
    monkeypatch.setattr(w, "_info", lambda title, text: shown.append(text))
    w.calibrate_colour_chart()
    assert w.ortho_view.mode == "chart"
    w.ortho_view.chart_finished.emit(np.array([[0.0, 5.0], [12.0, 5.0], [12.0, 14.0], [0.0, 14.0]]))
    w.runner.wait()
    app.processEvents()
    assert w.chunk.meta["colour_chart"]["loss"] < 3 and w.ortho_view.mode == "pan"
    w.ortho_view.area_finished.emit(np.array([[0.5, 0.5], [5.5, 0.5], [5.5, 3.5], [0.5, 3.5]]))
    w.runner.wait()
    app.processEvents()
    assert "10YR 5/4" in shown[-1]
    w.setWindowModified(False)
    w.close()
