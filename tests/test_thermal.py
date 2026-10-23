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


def _exiftool_like(path: Path) -> tuple[np.ndarray, dict]:
    """The raw thermal image and the FLIR tags of a JPEG as ExifTool reads and prints them
    (Image::ExifTool::FLIR): the FFF from the APP1 segments ("FLIR\\0", 1, number, last number) in
    the order of their numbers; the byte order of the header from its version (100-199), then
    toggled by a record whose first number reads 0x100 or more; the raw data a TIFF only when it is
    little-endian and width x height x 2 bytes; the values as ExifTool prints them, which is what
    the tools built on it (Thermal-Image-Analysis) compute with."""
    data = path.read_bytes()
    assert data[:2] == b"\xff\xd8"
    i, chunks, total = 2, {}, None
    while data[i] == 0xFF and data[i + 1] != 0xDA:
        n = struct.unpack(">H", data[i + 2:i + 4])[0]
        body = data[i + 4:i + 2 + n]
        if data[i + 1] == 0xE1 and body[:5] == b"FLIR\x00":
            assert body[5] == 1 and n <= 65535
            chunks[body[6]] = body[8:]
            total = body[7] + 1
        i += 2 + n
    assert total and sorted(chunks) == list(range(total))
    fff = b"".join(chunks[k] for k in range(total))
    assert fff[:4] == b"FFF\x00"
    order = ">" if 100 <= struct.unpack(">I", fff[0x14:0x18])[0] < 200 else "<"
    pos, num = struct.unpack(order + "II", fff[0x18:0x20])
    recs = {}
    for k in range(num):
        rtype, _sub, _ver, _idx, off, length = struct.unpack(order + "HHIIII", fff[pos + 32 * k:pos + 32 * k + 20])
        recs[rtype] = fff[off:off + length]

    def toggled(o, rec):
        return ("<" if o == ">" else ">") if struct.unpack(o + "H", rec[:2])[0] >= 0x100 else o

    order = toggled(order, recs[1])
    w, h = struct.unpack(order + "HH", recs[1][2:6])
    body = recs[1][32:]
    assert order == "<" and len(body) == w * h * 2  # RawThermalImageType: TIFF
    image = np.frombuffer(body, "<u2").reshape(h, w)
    cam = recs[0x20]
    order = toggled(order, cam)

    def f(at):
        return struct.unpack(order + "f", cam[at:at + 4])[0]

    rh = f(0x3C)
    rh = rh / 100 if rh > 2 else rh
    tags = {"Emissivity": "%.2f" % f(0x20), "ObjectDistance": "%.2f m" % f(0x24),
            "ReflectedApparentTemperature": "%.1f C" % (f(0x28) - 273.15),
            "AtmosphericTemperature": "%.1f C" % (f(0x2C) - 273.15),
            "IRWindowTemperature": "%.1f C" % (f(0x30) - 273.15), "IRWindowTransmission": "%.2f" % f(0x34),
            "RelativeHumidity": "%.1f %%" % (rh * 100), "PlanckR1": "%.8g" % f(0x58), "PlanckB": "%.8g" % f(0x5C),
            "PlanckF": "%.8g" % f(0x60), "PlanckO": "%d" % struct.unpack(order + "i", cam[0x308:0x30C])[0],
            "PlanckR2": "%.8g" % f(0x30C)}
    return image, tags


def _flir_reference_temperature(raw: np.ndarray, tags: dict) -> np.ndarray:
    """Degrees from FLIR counts the way the tools for FLIR files do it (Thermimage's raw2temp, as
    thermal_base of Thermal-Image-Analysis applies it to ExifTool's printed tags), written out term by
    term: the object's counts are what the camera counted, less the air and the window in front of
    it and the surroundings it reflects, then FLIR's Planck curve backwards."""
    v = {k: float(str(x).split()[0]) for k, x in tags.items()}
    e, d, t_refl, t_atm = v["Emissivity"], v["ObjectDistance"], v["ReflectedApparentTemperature"], v["AtmosphericTemperature"]
    t_win, irt, rh = v["IRWindowTemperature"], v["IRWindowTransmission"], v["RelativeHumidity"]
    r1, b, f, o, r2 = v["PlanckR1"], v["PlanckB"], v["PlanckF"], v["PlanckO"], v["PlanckR2"]
    water = rh / 100 * np.exp(1.5587 + 0.06939 * t_atm - 0.00027816 * t_atm ** 2 + 0.00000068455 * t_atm ** 3)
    half = np.sqrt(d / 2)
    tau = 1.9 * np.exp(-half * (0.006569 - 0.002276 * np.sqrt(water))) + (1 - 1.9) * np.exp(
        -half * (0.01262 - 0.00667 * np.sqrt(water)))

    def counts(t_c):
        return r1 / (r2 * (np.exp(b / (t_c + 273.15)) - f)) - o

    obj = (raw.astype(np.float64) / (e * tau * irt * tau)
           - (1 - tau) / e / tau * counts(t_atm)                 # the air between the object and the window
           - (1 - tau) / e / tau / irt / tau * counts(t_atm)     # the air between the window and the camera
           - (1 - irt) / e / tau / irt * counts(t_win)           # the window itself
           - (1 - e) / e * counts(t_refl))                       # the surroundings, reflected
    inside = r1 / (r2 * (obj + o)) + f
    assert np.all(inside > 0)  # Thermal-Image-Analysis gives up otherwise ("Image seems to be corrupted")
    return b / np.log(inside) - 273.15


def test_flir_radiometric_jpeg_round_trip(tmp_path):
    """A FLIR radiometric JPEG written by the program gives its temperatures back to ExifTool's way
    of reading (and to the tools on it), to the program's own reader, and holds a picture."""
    yy, xx = np.mgrid[0:300, 0:420]
    t = (14.0 + 0.03 * xx + 6.0 * np.sin(yy / 23.0)).astype(np.float32)
    t[200:240, 300:360] = 57.3  # a hot roof
    t[:30, :50] = np.nan  # outside the survey
    params = dict(thermal.DEFAULTS, distance=19.837, emissivity=0.95, humidity=70.0)
    path = thermal.write_flir_jpeg(tmp_path / "t.jpg", t, params)
    ok = np.isfinite(t)
    raw, tags = _exiftool_like(path)
    assert raw.shape == t.shape
    assert tags["Emissivity"] == "0.95" and tags["ObjectDistance"] == "19.84 m"
    assert tags["ReflectedApparentTemperature"] == "22.0 C" and tags["RelativeHumidity"] == "70.0 %"
    got = _flir_reference_temperature(raw, tags)
    assert np.max(np.abs(got[ok] - t[ok])) < 0.01
    assert np.allclose(got[~ok], np.nanmin(t), atol=0.01)  # no value: the coldest of the image
    # the program's own reader, with the parameters written in the file
    assert thermal.kind_of(path) == "flir"
    counts, info = thermal.read_flir(path)
    assert info["emissivity"] == pytest.approx(0.95) and info["humidity"] == pytest.approx(70.0)
    back = thermal.decode_file(path, dict(params, distance=19.84))
    assert np.max(np.abs(back[ok] - t[ok])) < 0.01
    # the picture is the palette rendering, black where nothing
    img = cv2.imread(str(path))
    assert img.shape[:2] == t.shape and img[5, 5].max() < 20 and img[220, 330].max() > 150
    # big frames do not fit in 256 segments
    with pytest.raises(ValueError, match="tiles"):
        thermal.write_flir_jpeg(tmp_path / "big.jpg", np.zeros((3000, 3000), np.float32), params)
    # the FLIR files of the cameras: counts in a PNG with the bytes swapped (most of them do)
    png_jpeg = tmp_path / "png.jpg"
    data = path.read_bytes()
    fff = thermal._flir_fff(data)
    recs = thermal._flir_records(fff)
    swapped = counts.astype(np.uint16).byteswap()
    okp, png = cv2.imencode(".png", swapped)
    raw_rec = recs[1][:32] + png.tobytes()
    cam_rec = recs[0x20]
    head = bytearray(fff[:64])
    struct.pack_into(">IIII", head, 0x14, 100, 64, 2, 3)
    entries = (struct.pack(">HHIIIIIII", 1, 3, 0x64, 1, 128, len(raw_rec), 0, 0, 0)
               + struct.pack(">HHIIIIIII", 0x20, 1, 0x64, 2, 128 + len(raw_rec), len(cam_rec), 0, 0, 0))
    new = bytes(head) + entries + raw_rec + cam_rec
    seg = b"\xff\xe1" + struct.pack(">H", 2 + 8 + len(new)) + b"FLIR\x00\x01\x00\x00" + new
    png_jpeg.write_bytes(data[:2] + seg + _without_flir(data)[2:])
    back = thermal.decode_file(png_jpeg, dict(params, distance=19.84))
    assert np.max(np.abs(back[ok] - t[ok])) < 0.01


def _without_flir(data: bytes) -> bytes:
    """A JPEG without its FLIR segments."""
    out, i = bytearray(data[:2]), 2
    while data[i] == 0xFF and data[i + 1] != 0xDA:
        n = struct.unpack(">H", data[i + 2:i + 4])[0]
        if not (data[i + 1] == 0xE1 and data[i + 4:i + 9] == b"FLIR\x00"):
            out += data[i:i + 2 + n]
        i += 2 + n
    return bytes(out + data[i:])


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool is not installed")
def test_flir_jpeg_read_by_exiftool(tmp_path):
    import subprocess

    t = np.linspace(10, 40, 64 * 48, dtype=np.float32).reshape(48, 64)
    path = thermal.write_flir_jpeg(tmp_path / "t.jpg", t, dict(thermal.DEFAULTS, distance=20.0))
    out = subprocess.run(["exiftool", "-j", "-Emissivity", "-ObjectDistance", "-ReflectedApparentTemperature",
                          "-AtmosphericTemperature", "-IRWindowTemperature", "-IRWindowTransmission", "-RelativeHumidity",
                          "-PlanckR1", "-PlanckB", "-PlanckF", "-PlanckO", "-PlanckR2", "-RawThermalImageType", str(path)],
                         capture_output=True, text=True, check=True)
    tags = json.loads(out.stdout)[0]
    assert tags["RawThermalImageType"] == "TIFF" and tags["Emissivity"] == 0.95
    tiff = subprocess.run(["exiftool", "-b", "-RawThermalImage", str(path)], capture_output=True, check=True).stdout
    from io import BytesIO

    from PIL import Image

    raw = np.asarray(Image.open(BytesIO(tiff)))
    got = _flir_reference_temperature(raw, {k: v for k, v in tags.items() if k not in ("SourceFile", "RawThermalImageType")})
    assert np.max(np.abs(got - t)) < 0.01


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
    # FLIR radiometric JPEGs in tiles: every tile's thermogram is the orthomosaic, placed by its world file
    index = thermal.export(ch, tmp_path / "t_flir.jpg", "flir", tile=64)
    rows = index.read_text().splitlines()
    assert rows[1].startswith("file,xmin") and len(rows) > 3
    with rasterio.open(out) as src:
        tr0 = src.transform
    for row in rows[2:]:
        name = row.split(",")[0]
        jpg = tmp_path / name
        raw, tags = _exiftool_like(jpg)
        got = _flir_reference_temperature(raw, tags)
        r0, c0 = (int(v[1:]) * 64 for v in jpg.stem.split("_")[-2:])
        want = t[r0:r0 + raw.shape[0], c0:c0 + raw.shape[1]]
        ok = np.isfinite(want)
        assert np.max(np.abs(got[ok] - want[ok])) < 0.01
        assert np.all(got[~ok] >= np.min(want[ok]) - 0.01)  # no value: the coldest of the tile
        a, d, b, e, cx, cy = (float(v) for v in jpg.with_suffix(".jgw").read_text().split())
        assert (cx, cy) == pytest.approx(tr0 * (c0 + 0.5, r0 + 0.5)) and a == pytest.approx(tr0.a) and e == pytest.approx(tr0.e)
        assert "<SRS>" in Path(str(jpg) + ".aux.xml").read_text()
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
    """The window with the captures of a thermal camera: the Termico tab shows the steps of the
    temperatures (the Multispettrale tab those of the spectral bands), the thermal orthomosaic opens
    from the Termico node of the tree on its iron ramp with the range of the pane, another range
    recolours it, the temperature under the mouse is read at full resolution, a measuring point
    keeps its elevation and temperature through saving, deleting and exporting, the export keeps
    degrees."""
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
        pane = w_.thermal_pane
        assert w_.left_tabs.tabText(w_.left_tabs.indexOf(pane)) == "Termico"
        assert not w_.ms_pane.rows["thermal"][0].isVisibleTo(w_.ms_pane) and w_.ms_pane.rows["reflectance"][0].isVisibleTo(w_.ms_pane)
        assert pane.rows["thermal"][0].isVisibleTo(pane) and not pane.rows["reflectance"][0].isVisibleTo(pane)
        assert pane.rows["thermal"][0].text().startswith("<b>5.")  # captures, alignment, surface, parameters, thermal
        assert "0.95" in pane.states["thermal_params"].text() and "0.42" in pane.states["thermal"].text()
        assert pane.b_tshow.isEnabled() and (pane.s_tlow.value(), pane.s_thigh.value()) == (18.0, 26.0)
        tree = w_.workspace
        chunk_item = tree.topLevelItem(0).child(0)
        kinds = [chunk_item.child(i).data(0, mwmod.Qt.ItemDataRole.UserRole)[0] for i in range(chunk_item.childCount())]
        assert "multispectral" not in kinds  # no spectral band: no Multispettrale node
        node = next(chunk_item.child(i) for i in range(chunk_item.childCount())
                    if chunk_item.child(i).data(0, mwmod.Qt.ItemDataRole.UserRole)[0] == "thermal")
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
        # under the mouse: the temperature at full resolution, with its name and unit
        w_._show_coords(400000.0 + 40 * 0.05, 4500000.0 - 5 * 0.05, None)
        assert "T 26.00 °C" in w_.coord_label.text()
        # a measuring point: its temperature, in the layer, through saving and reopening, deleted, exported
        w_.set_draw_mode("probe")
        assert v.mode == "probe"
        v.probe_clicked.emit(400000.0 + 10 * 0.05 + 0.01, 4500000.0 - 20 * 0.05 - 0.01)
        assert len(w_.probe_points) == 1 and w_.probe_points[0]["temp_c"] == pytest.approx(18.0)
        assert len(v.features(mwmod.PROBE_LAYER)) == 1 and "1 punti di misura" in pane.states["points"].text()
        w_._store_drawings()
        doc.save()
        assert ch.meta["drawings"]["points"][0]["label"] == "P1"
        w_._reload_drawings()
        assert len(w_.probe_points) == 1 and len(v.features(mwmod.PROBE_LAYER)) == 1
        gpkg_out = tmp_path / "punti.gpkg"
        monkeypatch.setattr(mwmod.QFileDialog, "getSaveFileName", lambda *a, **k: (str(gpkg_out), ""))
        monkeypatch.setattr(w_, "_info", lambda *a, **k: None)
        w_.export_probe_points()
        import sqlite3

        con = sqlite3.connect(gpkg_out)
        assert con.execute("SELECT temp_c, label FROM punti_misura").fetchall() == [(18.0, "P1")]
        con.close()
        v.select_items([v.layers[mwmod.PROBE_LAYER]["features"][0][0]])
        assert w_.delete_selected(confirm=False) == 1 and w_.probe_points == []
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


def test_frames_of_other_thermal_cameras(tmp_path):
    """Frames that are not DJI's: FLIR radiometric JPEGs and TIFFs of temperatures are thermal
    frames by themselves; TIFFs of integers (WIRIS and other cameras) only when the frames are added
    as thermal ones (nothing else tells them from the bands of a multispectral camera), and become
    degrees with the scale and offset of the measure's parameters, as Metashape's raster transform."""
    from PIL import Image

    yy, xx = np.mgrid[0:48, 0:64]
    t = (18.0 + 0.05 * xx + 0.02 * yy).astype(np.float32)
    ints = tmp_path / "wiris"
    ints.mkdir()
    for i in range(3):
        Image.fromarray(np.round((t + i + 100.0) * 100.0).astype(np.uint16)).save(ints / f"IMG_{i:04d}.tiff")
    flir = tmp_path / "flir"
    flir.mkdir()
    for i in range(2):
        thermal.write_flir_jpeg(flir / f"FLIR{i:04d}.jpg", t + i, dict(thermal.DEFAULTS, distance=10.0))
    deg = tmp_path / "gradi"
    deg.mkdir()
    _write_float_tiff(deg / "frame_a.tif", t)
    # recognised by themselves: the FLIR JPEGs and the TIFFs of degrees, not the integers
    assert set(ms.detect_bands([flir])) == {"FLIR0000", "FLIR0001"}
    assert all(set(c) == {"LWIR"} for c in ms.detect_bands([flir]).values())
    assert set(ms.detect_bands([deg])) == {"frame_a"}
    assert ms.detect_bands([ints]) == {}
    caps = ms.detect_bands([ints], thermal=True)
    assert len(caps) == 3 and thermal.kind_of(caps["IMG_0001"]["LWIR"]) == "counts"
    # integers: degrees only once the conversion is set
    f = caps["IMG_0001"]["LWIR"]
    with pytest.raises(thermal.ThermalDecoderMissing, match="scale"):
        thermal.decode_file(f, thermal.DEFAULTS)
    scale, offset = thermal.RAW_PRESETS["centidegrees_100"]
    got = thermal.decode_file(f, dict(thermal.DEFAULTS, scale=scale, offset=offset))
    assert np.max(np.abs(got - (t + 1))) < 0.006
    assert thermal.intensity(f).max() > 10000  # the geometry reads the integers as they are
    # into a chunk as the frames of a thermal camera
    doc = ppg.Document()
    ch = doc.add_chunk("Altre camere")
    doc.save(tmp_path / "altre.ppg")
    cams = ms.add_captures(ch, [ints], thermal=True)
    assert len(cams) == 3 and ch.meta["multispectral"]["reference"] == "LWIR"
    thermal.set_params(ch, scale=scale, offset=offset)
    assert thermal.params_of(ch)["scale"] == 0.01
    assert np.max(np.abs(thermal.decode_file(thermal.thermal_file(cams[0]), thermal.params_of(ch)) - t)) < 0.006
