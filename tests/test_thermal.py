"""Thermal cameras (engine/thermal.py): the radiometric images recognised and decoded (DJI's SDK
through a stand-in dji_irp, temperature TIFFs, MicaSense centikelvin), the visible and thermal files
of a capture paired by name or by the time of the shot, the thermal frame registered on its
visible photo, and the temperature orthomosaic of the synthetic trench in degrees with the offsets
between frames taken away, its anomalies and its exports."""
from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine import multispectral as ms
from pyarchinit_photogrammetry.engine import thermal

COVER = 0.75  # the thermal frame spans three quarters of the visible width
SHIFT = (6, -4)  # the thermal lens a little off the visible one (visible pixels)
T_SIZE = (320, 256)  # 5:4, like the 640 x 512 frames of the DJI cameras


def _thermal_of(photo: np.ndarray, offset: float = 0.0) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """A thermal frame of a visible photo: its middle (``COVER`` of the width, 5:4, moved by
    ``SHIFT``) reduced to ``T_SIZE``, hot where the photo is dark (as soil in the sun), plus
    ``offset`` degrees (the drift of an uncooled camera). Returns it and the crop (x0, y0, w, h)."""
    h, w = photo.shape[:2]
    cw = COVER * w
    ch = cw * T_SIZE[1] / T_SIZE[0]
    x0 = (w - cw) / 2 + SHIFT[0]
    y0 = (h - ch) / 2 + SHIFT[1]
    grey = cv2.cvtColor(photo, cv2.COLOR_BGR2GRAY).astype(np.float32)
    m = np.array([[T_SIZE[0] / cw, 0, (-x0 + 0.5) * T_SIZE[0] / cw - 0.5],
                  [0, T_SIZE[1] / ch, (-y0 + 0.5) * T_SIZE[1] / ch - 0.5]], np.float64)
    crop = cv2.warpAffine(cv2.GaussianBlur(grey, (0, 0), 0.6 * cw / T_SIZE[0]), m, T_SIZE, flags=cv2.INTER_LINEAR)
    return (40.0 - 25.0 * crop / 255.0 + offset).astype(np.float32), (x0, y0, cw, ch)


def _write_float_tiff(path: Path, t: np.ndarray) -> None:
    from PIL import Image

    Image.fromarray(t.astype(np.float32), mode="F").save(path)


def _dji_like_jpeg(path: Path, size=(64, 48)) -> None:
    """A JPEG as DJI's thermal cameras write it, as far as recognising it goes: EXIF make DJI, an
    APP3 segment (where the raw counts are)."""
    from PIL import Image

    im = Image.fromarray(np.full((size[1], size[0]), 128, np.uint8))
    exif = Image.Exif()
    exif[271] = "DJI"
    exif[272] = "MAVIC2-ENTERPRISE-ADVANCED"
    im.save(path, exif=exif.tobytes())
    raw = path.read_bytes()
    app3 = b"\xff\xe3" + struct.pack(">H", 2 + 16) + b"\x00" * 16
    path.write_bytes(raw[:2] + app3 + raw[2:])


def _fake_irp(folder: Path) -> Path:
    """A stand-in for DJI's dji_irp: writes float32 temperatures that tell the parameters it got
    (20 + 10 x emissivity + distance / 100 + reflection / 1000), refuses --ambient as the SDK does
    for the cameras without it."""
    script = folder / "dji_irp"
    script.write_text(f"""#!{sys.executable}
import sys
import numpy as np
from PIL import Image
args = sys.argv[1:]
if "--ambient" in args:
    sys.stderr.write("unknown option --ambient\\n")
    sys.exit(2)
opt = {{args[i]: args[i + 1] for i in range(0, len(args) - 1) if args[i].startswith("-")}}
w, h = Image.open(opt["-s"]).size
v = 20 + 10 * float(opt["--emissivity"]) + float(opt["--distance"]) / 100 + float(opt["--reflection"]) / 1000
np.full((h, w), v, "<f4").tofile(opt["-o"])
""")
    script.chmod(0o755)
    return script


def test_files_recognised_and_decoded(tmp_path, monkeypatch):
    jpg = tmp_path / "DJI_0001_T.JPG"
    _dji_like_jpeg(jpg)
    assert thermal.kind_of(jpg) == "dji"
    plain = tmp_path / "plain.jpg"
    cv2.imwrite(str(plain), np.zeros((20, 20, 3), np.uint8))
    assert thermal.kind_of(plain) is None
    # without the SDK the message says where to get it
    monkeypatch.delenv("PPG_DJI_IRP", raising=False)
    monkeypatch.setenv("PPG_DJI_TSDK", str(tmp_path / "nowhere"))
    monkeypatch.setattr(thermal, "sdk_dir", lambda: None)
    with pytest.raises(thermal.ThermalDecoderMissing, match="DJI Thermal SDK"):
        thermal.decode_file(jpg)
    # with dji_irp: the factory parameters, the distance from the flight height (none here: 5 m)
    monkeypatch.setenv("PPG_DJI_IRP", str(_fake_irp(tmp_path)))
    t = thermal.decode_file(jpg, thermal.DEFAULTS, distance=None)
    assert t.shape == (48, 64) and t.dtype == np.float32
    assert t[0, 0] == pytest.approx(20 + 9.5 + 0.05 + 0.022, abs=1e-4)  # no flight height in the file: 5 m
    p = dict(thermal.DEFAULTS, emissivity=0.98, reflected=30.0)
    t2 = thermal.decode_dji([jpg], p, distances=[20.0])[jpg]
    assert t2[0, 0] == pytest.approx(20 + 9.8 + 0.2 + 0.03, abs=1e-4)
    # temperatures already in a TIFF: degrees, or kelvin turned into degrees; MicaSense centikelvin
    deg = tmp_path / "t.tif"
    _write_float_tiff(deg, np.full((10, 12), 23.5, np.float32))
    assert thermal.kind_of(deg) == "tiff" and thermal.decode_file(deg)[0, 0] == pytest.approx(23.5)
    kel = tmp_path / "k.tif"
    _write_float_tiff(kel, np.full((10, 12), 296.65, np.float32))
    assert thermal.decode_file(kel)[0, 0] == pytest.approx(23.5, abs=1e-3)


def test_visible_and_thermal_paired_by_name_or_by_time(tmp_path):
    from PIL import Image

    rgb, ir = tmp_path / "RGB", tmp_path / "IR" / "100MEDIA"
    rgb.mkdir(parents=True)
    ir.mkdir(parents=True)

    def shot(path, stamp, size=(80, 60)):
        im = Image.fromarray(np.zeros((size[1], size[0], 3), np.uint8))
        exif = Image.Exif()
        exif[271] = "DJI"
        exif.get_ifd(0x8769)[36867] = stamp
        im.save(path, exif=exif.tobytes())

    # by name: the same number on both files
    shot(rgb / "DJI_0010_W.JPG", "2025:10:30 11:43:37")
    shot(ir / "DJI_0010_T.JPG", "2025:10:30 11:43:37", (32, 24))
    # numbered differently: by the time of the shot
    shot(rgb / "DJI_0020_W.JPG", "2025:10:30 11:43:41")
    shot(ir / "DJI_0021_T.JPG", "2025:10:30 11:43:41", (32, 24))
    shot(rgb / "DJI_0030_W.JPG", "2025:10:30 11:43:45")
    shot(ir / "DJI_0031_T.JPG", "2025:10:30 11:43:49", (32, 24))  # four seconds later: not the same shot
    shot(rgb / "DJI_0030_Z.JPG", "2025:10:30 11:43:45")  # zoom: not a band
    caps = ms.detect_bands([tmp_path], recursive=True)
    assert set(caps) == {"DJI_0010", "DJI_0020"}
    assert caps["DJI_0020"]["LWIR"].name == "DJI_0021_T.JPG" and caps["DJI_0010"]["Visible"].parent == rgb
    sniff = ms.looks_multispectral([tmp_path], recursive=True)
    assert sniff["thermal"] and sniff["bands"] == ["LWIR", "Visible"] and sniff["captures"] == 3
    doc = ppg.Document()
    ch = doc.add_chunk("Campo")
    doc.save(tmp_path / "campo.ppg")
    cams = ms.add_captures(ch, [tmp_path], recursive=True)
    assert len(cams) == 2 and ch.meta["multispectral"]["reference"] == "Visible"
    assert {Path(c.path).name for c in cams} == {"DJI_0010_W.JPG", "DJI_0020_W.JPG"}  # the visible photos themselves
    assert all(thermal.thermal_file(c) for c in cams)
    with pytest.raises(RuntimeError, match="temperature orthomosaic"):
        ms.build_reflectance(ch)


def test_registration_finds_the_thermal_frame(scene_dir):
    photo = cv2.imread(str(sorted((scene_dir / "photos").glob("*.jpg"))[3]))
    temp, (x0, y0, cw, ch) = _thermal_of(photo)
    grid = cv2.imread(str(sorted((scene_dir / "photos").glob("*.jpg"))[3]),
                      cv2.IMREAD_REDUCED_GRAYSCALE_2 | cv2.IMREAD_IGNORE_ORIENTATION).astype(np.float32)
    h, cc, cover = thermal.register(grid, temp, cover=None)  # no focal lengths: the cover searched
    assert cc > 0.5 and abs(cover - COVER) < 0.1
    g = np.array([[150, 120], [450, 120], [300, 225], [180, 330], [420, 330]], float)  # grid pixels inside the frame
    w_px = 2 * g + 0.5  # the visible pixel at the centre of a grid pixel (reduced by two)
    true = np.column_stack([(w_px[:, 0] - x0 + 0.5) * T_SIZE[0] / cw - 0.5, (w_px[:, 1] - y0 + 0.5) * T_SIZE[1] / ch - 0.5])
    got = (h @ np.column_stack([g, np.ones(len(g))]).T).T
    got = got[:, :2] / got[:, 2:]
    assert np.max(np.hypot(*(got - true).T)) < 0.6  # thermal pixels
    assert thermal.correlation(thermal._grad(grid), thermal._grad(temp), h) == pytest.approx(cc, abs=0.1)


def _write_thermal_captures(scene_dir: Path, out: Path, odd_offset: float = 1.5) -> Path:
    folder = out / "volo"
    folder.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(sorted((scene_dir / "photos").glob("*.jpg"))):
        photo = cv2.imread(str(p))
        cv2.imwrite(str(folder / f"{p.stem}_W.jpg"), photo, [cv2.IMWRITE_JPEG_QUALITY, 97])
        temp, _box = _thermal_of(photo, odd_offset if i % 2 else 0.0)
        _write_float_tiff(folder / f"{p.stem}_T.tif", temp)
    return folder


def test_temperature_orthomosaic_of_the_trench(scene_dir, tmp_path):
    import rasterio
    from rasterio.warp import Resampling, reproject

    folder = _write_thermal_captures(scene_dir, tmp_path)
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(tmp_path / "saggio.ppg")
    cams = ms.add_captures(ch, [folder])
    n = len(list((scene_dir / "photos").glob("*.jpg")))
    assert len(cams) == n and ch.meta["multispectral"]["bands"] == ["LWIR", "Visible"]
    ch.detect_markers("aruco_4x4_50")
    ch.match_photos(quality="high", gpu="off")
    assert ch.align_cameras(mapper="incremental", use_gps=False) >= 0.9 * n
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    assert ch.update_transform()["control_rmse"] < 0.003
    ch.build_depth_maps(quality="medium")
    ch.build_point_cloud()
    ch.build_dem()
    thermal.set_params(ch, emissivity=0.95)
    out = thermal.build_thermal(ch)
    info = ch.meta["thermal"]
    reg = info["registration"]
    assert reg["factor"] == 2 and abs(reg["cover"] - COVER) < 0.1 and reg["fixed_cc"] > 0.5
    # the offsets: the odd frames 1.5 degrees warmer, the even ones not (mean zero)
    off = info["offsets"]
    odd = [off[str(c.key)] for i, c in enumerate(cams) if i % 2 and str(c.key) in off]
    even = [off[str(c.key)] for i, c in enumerate(cams) if not i % 2 and str(c.key) in off]
    assert np.median(odd) - np.median(even) == pytest.approx(1.5, abs=0.25)
    with rasterio.open(out) as src:
        t = src.read(1)
        tr, crs = src.transform, src.crs
        tags = src.tags()
        assert src.count == 1 and src.dtypes[0] == "float32" and tags["units"] == "degC" and tags["emissivity"] == "0.95"
    assert np.isfinite(t).mean() > 0.3
    # against the visible orthomosaic: the temperature the frames were made from (40 - 25 grey / 255)
    ch.build_orthomosaic(resolution=info["resolution"] / 2)
    with rasterio.open(ch.orthomosaic_path) as src:
        bgr = np.stack([src.read(3), src.read(2), src.read(1)], -1)
        alpha = src.read(4)
        grey = np.where(alpha > 0, cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32), np.nan)
        expected = np.full(t.shape, np.nan, np.float32)
        reproject(grey, expected, src_transform=src.transform, src_crs=src.crs, dst_transform=tr, dst_crs=crs,
                  src_nodata=np.nan, dst_nodata=np.nan, resampling=Resampling.average)
    expected = 40.0 - 25.0 * expected / 255.0
    both = np.isfinite(t) & np.isfinite(expected)
    diff = t[both] - expected[both]
    level = np.median(diff)  # the balance keeps the mean level of the frames: half the offset of the odd ones
    assert abs(level - 0.75) < 0.35
    assert np.median(np.abs(diff - level)) < 0.6
    assert np.corrcoef(t[both], expected[both])[0, 1] > 0.9
    # anomalies, exports
    an = thermal.anomalies(ch, radius_m=0.3)
    with rasterio.open(an) as src:
        a = src.read(1)
    assert abs(np.nanmedian(a)) < 0.3 and np.nanstd(a) > 0.3
    t16 = thermal.export(ch, tmp_path / "t16.tif", "tif16")
    with rasterio.open(t16) as src:
        ck = src.read(1)
        assert src.dtypes[0] == "uint16" and src.nodata == 0
    ok = ck > 0
    assert ok.sum() == np.isfinite(t).sum()
    assert np.max(np.abs(ck[ok] / 100.0 - 273.15 - t[ok])) < 0.006
    csv = thermal.export(ch, tmp_path / "t.csv")
    lines = csv.read_text().splitlines()
    assert lines[1] == "x,y,temperature_c" and len(lines) == 2 + np.isfinite(t).sum()
    assert json.loads(lines[0].split("; ", 1)[1])["emissivity"] == "0.95"
    # without the balance the strips come back: the two halves differ by the offset again
    thermal.build_thermal(ch, balance=False)
    assert "offsets" not in ch.meta["thermal"]


def test_colours_and_limits():
    t = np.array([[10.0, 20.0, np.nan], [30.0, 40.0, 25.0]], np.float32)
    lo, hi = thermal.limits(t, 15.0, 35.0)
    assert (lo, hi) == (15.0, 35.0)
    rgba = thermal.colours(t, (lo, hi), "iron")
    assert rgba[0, 2, 3] == 0 and rgba[0, 0, 3] == 255
    assert tuple(rgba[0, 0, :3]) == (0, 0, 0) and tuple(rgba[1, 1, :3]) == (255, 255, 230)  # the ends of iron
    grey = thermal.colours(t, (lo, hi), "grey")
    assert grey[1, 2, 0] in (127, 128)


def test_thermal_steps_views_and_exports_in_the_window(tmp_path, monkeypatch):
    """The window with the captures of a thermal camera: the pane shows the steps of the
    temperatures (not those of the spectral bands), the thermal orthomosaic opens from the tree on
    its iron ramp with the range of the pane, another range recolours it, the export keeps degrees."""
    pytest.importorskip("PySide6")
    import rasterio
    from rasterio.transform import from_origin

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    QApplication.instance() or QApplication([])
    from pyarchinit_photogrammetry.gui import image_views
    from pyarchinit_photogrammetry.gui import main_window as mwmod

    doc = ppg.Document()
    ch = doc.add_chunk("Campo")
    doc.save(tmp_path / "campo.ppg")
    ch.crs = "EPSG:32633"
    folder = ch.data_dir / "multispectral"
    folder.mkdir(parents=True)
    t = np.full((40, 50), 18.0, np.float32)
    t[:, 25:] = 26.0  # a warm half
    t[0, 0] = np.nan
    with rasterio.open(folder / "thermal.tif", "w", driver="GTiff", height=40, width=50, count=1, dtype="float32",
                       crs="EPSG:32633", transform=from_origin(400000.0, 4500000.0, 0.05, 0.05), nodata=float("nan")) as dst:
        dst.write(t, 1)
        dst.update_tags(units="degC", emissivity="0.95")
    ch.meta["multispectral"] = {"reference": "Visible", "bands": ["LWIR", "Visible"], "captures": 4, "panel": None,
                                "registration": {}}
    ch.meta["thermal"] = {"file": "multispectral/thermal.tif", "resolution": 0.05,
                          "stats": {"p2": 18.0, "p98": 26.0, "min": 18.0, "max": 26.0, "median": 22.0},
                          "offsets": {"spread": 0.42}}
    doc.save()
    w_ = mwmod.MainWindow(doc, autoconnect=False)
    w_.runner.show_dialog = False
    try:
        pane = w_.ms_pane
        assert pane.rows["thermal"][0].isVisibleTo(pane) and not pane.rows["reflectance"][0].isVisibleTo(pane)
        assert pane.rows["thermal"][0].text().startswith("<b>5.")  # captures, alignment, surface, parameters, thermal
        assert "0.95" in pane.states["thermal_params"].text() and "0.42" in pane.states["thermal"].text()
        assert pane.b_tshow.isEnabled() and (pane.s_tlow.value(), pane.s_thigh.value()) == (18.0, 26.0)
        tree = w_.workspace
        chunk_item = tree.topLevelItem(0).child(0)
        node = next(chunk_item.child(i) for i in range(chunk_item.childCount())
                    if chunk_item.child(i).data(0, mwmod.Qt.ItemDataRole.UserRole)[0] == "multispectral")
        payloads = [node.child(i).data(0, mwmod.Qt.ItemDataRole.UserRole)[2] for i in range(node.childCount())]
        assert payloads == ["ms:thermal"]
        w_._workspace_item("product", ch, "ms:thermal")
        v = w_.ortho_view
        assert v.kind == "thermal" and v._dem_range == (18.0, 26.0) and v.thermal_palette == "iron"
        rgba = image_views.read_window_rgba(folder / "thermal.tif", "thermal", (0, 0, 50, 40), (40, 50), v._dem_range,
                                            palette="iron")
        assert rgba[5, 5, :3].sum() < 30 and rgba[5, 40, :3].sum() > 700 and rgba[0, 0, 3] == 0  # black cold, white warm
        # another range from the pane: the warm half no longer at the top of the ramp
        pane.set_thermal_limits(10.0, 40.0)
        pane.c_tpalette.setCurrentIndex(pane.c_tpalette.findData("grey"))
        assert v.kind == "thermal" and v._dem_range == (10.0, 40.0) and v.thermal_palette == "grey"
        assert w_.settings.value("thermal/palette") == "grey"
        pane.b_tauto.click()
        assert v.thermal_range is None and v._dem_range == (18.0, 26.0)
        # the export keeps degrees
        out = tmp_path / "temperatura_C.tif"
        monkeypatch.setattr(mwmod.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
        monkeypatch.setattr(w_, "_info", lambda *a, **k: None)
        w_.ms_thermal_export("tif")
        w_.runner.wait()
        with rasterio.open(out) as src:
            assert src.read(1)[5, 40] == pytest.approx(26.0) and src.tags()["units"] == "degC"
    finally:
        w_.setWindowModified(False)
        w_.close()
