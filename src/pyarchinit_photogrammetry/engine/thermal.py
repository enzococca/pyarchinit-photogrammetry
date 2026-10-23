"""Thermal cameras: the temperature of every pixel from the radiometric images, the thermal frame
of a capture registered onto its visible photo, the temperature orthomosaic in degrees Celsius
that stays radiometric through every export.

A drone thermal camera (DJI Mavic 2 Enterprise Advanced, M30T, Mavic 3 Thermal, H20T; MicaSense
Altum's LWIR band) shoots a visible photo and a thermal frame at the same moment: ``DJI_0833_W.JPG``
and ``DJI_0833_T.JPG``. The visible photo - 48 megapixels, full of texture - is aligned and
reconstructed as any photo; the thermal frame (640 x 512, smooth) is brought onto it with a
homography found on the images (:func:`register`): the two lenses are a few centimetres apart, at
the distances of a drone the parallax is below a thermal pixel. Their fields of view differ (the
thermal one is narrower): the first guess comes from the focal lengths, ECC on the gradient
magnitudes refines it (the edges of a road, a wall, a tree agree in both), and one homography for
the whole flight (the camera is rigid) is taken from the captures where ECC agrees best, then
refined capture by capture only where that clearly agrees better.

Temperatures (:func:`temperature`): the raw counts of a radiometric JPEG become degrees through the
camera maker's model with the object parameters - emissivity, reflected temperature, ambient
temperature, humidity, distance (:data:`DEFAULTS`: the factory values of the DJI cameras, 0.95 and
22 degrees). DJI R-JPEGs are decoded by DJI's Thermal SDK (``dji_irp -a measure``; Windows and
Linux; on macOS through Docker), FLIR radiometric JPEGs here (:func:`decode_flir`: the FFF records
of their APP1 segments and FLIR's Planck model), MicaSense LWIR TIFFs are centikelvin, float TIFFs
(from the DJI Thermal Analysis Tool, Thermal-Tools, ...) are degrees already. The frames decoded are kept as float32
TIFFs in the chunk (``<data>/thermal/frames/``) with the parameters in their tags.

The temperature orthomosaic (:func:`build_thermal`) is the orthophoto engine run on the visible
photos with the registered temperatures as float values: ``<data>/multispectral/thermal.tif``, one
float32 band in degrees Celsius, NaN where nothing, the parameters in its tags. Uncooled
microbolometers drift between frames (the flat-field corrections jump by tenths of a degree, a
degree): :func:`balance_offsets` finds an offset per frame where the frames overlap and takes it
away, the mean level kept - the strips between flight lines go. :func:`anomalies` is the local
deviation (the temperature minus its surroundings): what a buried wall or ditch does to the soil.
:func:`export` writes the temperatures as float32 GeoTIFF (degrees Celsius), 16-bit GeoTIFF in
centikelvin (the integer format of MicaSense and of most tools), CSV of x, y, temperature, or
FLIR radiometric JPEGs (:func:`write_flir_jpeg`, tiles with world files) that the tools made for
FLIR cameras read as thermograms.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress

# ECC (OpenCV's video module) refines the registrations; a build without it (the Windows ARM64
# wheel up to 1.3.0) keeps the first estimate. The motions by value: OpenCV's numbers.
ECC = hasattr(cv2, "findTransformECC")
MOTION_AFFINE = getattr(cv2, "MOTION_AFFINE", 2)
MOTION_HOMOGRAPHY = getattr(cv2, "MOTION_HOMOGRAPHY", 3)

DEFAULTS = {"emissivity": 0.95, "reflected": 22.0, "ambient": 22.0, "humidity": 70.0, "distance": None,
            "scale": None, "offset": None}  # scale, offset: degrees = value x scale + offset for integer TIFFs
# integer encodings of temperatures that are common enough to pick by name: (scale, offset)
RAW_PRESETS = {"centikelvin": (0.01, -273.15), "centidegrees_100": (0.01, -100.0)}
DISTANCE_RANGE = (1.0, 25.0)  # what DJI's SDK accepts; None = the flight height of the frame, clipped
THERMAL_BANDS = ("LWIR",)
DOCKER_IMAGE = "ppg-dji-tsdk:1"  # ubuntu:22.04 with the OpenMP runtime dji_irp needs, built when missing
# DJI's Thermal SDK as DJI publishes it (linked from dji.com/downloads/softwares/dji-thermal-sdk): downloaded by
# install_sdk for whoever accepts its licence, never shipped with the program
SDK_VERSION = "1.9"
SDK_URL = "https://terra-1-g.djicdn.com/2640963bcd8e45c0a4f0cb9829739d6b/TSDK/v1.9/dji_thermal_sdk_v1.9_20260526.zip"
SDK_SHA256 = "9345dc8d403ecc413c212e545d690b8bc843b3189157f3687bddd1c38e1d76eb"  # of the archive at SDK_URL
SDK_SIZE = 73_701_242  # for the progress when the server does not say
SDK_EULA_URL = "https://developer.dji.com/policies/eula/"
SDK_PAGE_URL = "https://www.dji.com/downloads/softwares/dji-thermal-sdk"
DOCKERFILE = ("FROM ubuntu:22.04\n"
              "RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*\n")
PALETTES = {  # (position, (R, G, B)): the ramps of temperature
    "iron": [(0.0, (0, 0, 0)), (0.18, (40, 0, 110)), (0.38, (140, 0, 140)), (0.55, (220, 40, 40)),
             (0.72, (250, 130, 0)), (0.88, (255, 215, 40)), (1.0, (255, 255, 230))],
    "inferno": [(0.0, (0, 0, 4)), (0.25, (87, 16, 110)), (0.5, (188, 55, 84)), (0.75, (249, 142, 9)),
                (1.0, (252, 255, 164))],
    "rainbow": [(0.0, (0, 0, 130)), (0.2, (0, 60, 255)), (0.4, (0, 220, 220)), (0.6, (80, 230, 40)),
                (0.8, (255, 200, 0)), (1.0, (200, 0, 0))],
    "grey": [(0.0, (0, 0, 0)), (1.0, (255, 255, 255))],
    "grey_inverted": [(0.0, (255, 255, 255)), (1.0, (0, 0, 0))],
}
PALETTE_NAMES = [("iron", "Iron (ferro)"), ("inferno", "Inferno"), ("rainbow", "Arcobaleno"), ("grey", "Scala di grigi"),
                 ("grey_inverted", "Grigi invertiti")]


class ThermalDecoderMissing(RuntimeError):
    """The program that decodes these radiometric images is not available; the message says how
    to get it."""


# ------------------------------------------------------------------------------ parameters
def params_of(chunk: Chunk | None) -> dict:
    """The object parameters of the chunk's thermal frames (:data:`DEFAULTS` where not set)."""
    p = dict(DEFAULTS)
    if chunk is not None:
        p.update({k: v for k, v in ((chunk.meta.get("thermal") or {}).get("params") or {}).items() if k in DEFAULTS})
    return p


def set_params(chunk: Chunk, **params) -> dict:
    """Change the object parameters; the frames decoded with others are decoded again."""
    meta = chunk.meta.setdefault("thermal", {})
    current = params_of(chunk)
    new = dict(current)
    new.update({k: (None if v is None else float(v)) for k, v in params.items() if k in DEFAULTS})
    if new != current:
        meta["params"] = new
        meta.pop("decoded", None)
        frames = Path(chunk.data_dir) / "thermal" / "frames"
        if frames.exists():
            shutil.rmtree(frames, ignore_errors=True)
    return new


# ------------------------------------------------------------------------------ the files
def _jpeg_segments(path, limit: int = 64) -> list[tuple[int, bytes]]:
    """``(marker, first bytes)`` of the first JPEG segments of ``path`` (APPn and the like)."""
    out = []
    with open(path, "rb") as f:
        if f.read(2) != b"\xff\xd8":
            return out
        while len(out) < limit:
            b = f.read(2)
            if len(b) < 2 or b[0] != 0xFF:
                break
            marker = b[1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                continue
            if marker == 0xDA:  # the scan: the image data follows
                break
            n = struct.unpack(">H", f.read(2))[0]
            body = f.read(n - 2)
            out.append((marker, body[:32]))
    return out


def kind_of(path) -> str | None:
    """What decodes ``path``: ``dji`` (a DJI R-JPEG), ``flir`` (a FLIR radiometric JPEG),
    ``micasense`` (a MicaSense LWIR TIFF in centikelvin), ``tiff`` (temperatures as floats),
    ``counts`` (a single-band TIFF of integers: values that become degrees with the scale and
    offset of the measure's parameters, as Metashape's raster transform does - WIRIS and other
    cameras), or None for an image that holds no temperatures."""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        try:
            segs = _jpeg_segments(p)
        except OSError:
            return None
        if any(m == 0xE1 and body.startswith(b"FLIR\x00") for m, body in segs):
            return "flir"
        app3 = sum(1 for m, _b in segs if m == 0xE3)
        dji = any(m == 0xE1 and b"DJI" in body for m, body in segs) or _exif_make(p) == "DJI"
        if app3 and dji:
            return "dji"
        return None
    if suffix in (".tif", ".tiff"):
        try:
            from PIL import Image

            with Image.open(p) as im:
                mode = im.mode
        except Exception:  # noqa: BLE001
            return None
        if mode in ("F", "F;32BF") or mode.startswith("F"):
            return "tiff"
        if mode in ("I;16", "I;16B", "I;16L", "I"):
            from .multispectral import band_name_of

            return "micasense" if band_name_of(p) == "LWIR" else "counts"
    return None


def _exif_make(path) -> str | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return str(im.getexif().get(271) or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def image_size(path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as im:
        return im.size


def flight_height(path) -> float | None:
    """The height of the drone over the take-off point (``drone-dji:RelativeAltitude``), metres."""
    from .multispectral import xmp_of, _number

    try:
        return _number(xmp_of(path).get("RelativeAltitude"), None)
    except Exception:  # noqa: BLE001
        return None


def focal_35mm(path) -> float | None:
    """The 35 mm equivalent focal length of the EXIF, when there is one."""
    try:
        from PIL import ExifTags, Image

        with Image.open(path) as im:
            sub = im.getexif().get_ifd(ExifTags.IFD.Exif)
            v = sub.get(41989)  # FocalLengthIn35mmFilm
            return float(v) if v else None
    except Exception:  # noqa: BLE001
        return None


def raw_counts(path) -> np.ndarray | None:
    """The raw counts of a DJI R-JPEG (the 16-bit frame its APP3 segments hold, one after the
    other), or None when the file has not got them. They grow with the temperature: enough for the
    geometry - an 8-bit copy to align the frames on - not degrees, which only DJI's SDK gives."""
    data = Path(path).read_bytes()
    if data[:2] != b"\xff\xd8":
        return None
    chunks = []
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:
            break
        n = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker == 0xE3:
            chunks.append(data[i + 4:i + 2 + n])
        i += 2 + n
    if not chunks:
        return None
    raw = b"".join(chunks)
    w, h = image_size(path)
    if len(raw) < w * h * 2:
        return None
    return np.frombuffer(raw[:w * h * 2], "<u2").reshape(h, w).astype(np.float32)


def intensity(path) -> np.ndarray:
    """What a thermal frame looks like for the geometry: its raw counts (DJI), else its
    temperatures as the files hold them (TIFFs, MicaSense)."""
    kind = kind_of(path)
    if kind == "dji":
        raw = raw_counts(path)
        if raw is not None:
            return raw
    if kind == "counts":  # the integers as they are: the geometry needs no degrees
        return _tiff_values(path)
    return decode_file(path)


def _tiff_values(path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as im:
        t = np.asarray(im, np.float32)
    return t[..., 0] if t.ndim == 3 else t


def stretch_of(paths, sample: int = 40) -> tuple[float, float]:
    """One stretch for every frame of a flight (the 1st and 99th percentiles of a sample of
    frames): the same counts give the same grey in every 8-bit copy, as the features need."""
    paths = list(paths)
    pick = paths if len(paths) <= sample else [paths[int(i)] for i in np.linspace(0, len(paths) - 1, sample)]
    vals = []
    for p in pick:
        v = intensity(p)
        v = v[np.isfinite(v)]
        if v.size:
            vals.append(np.percentile(v, [1, 99]))
    if not vals:
        return 0.0, 1.0
    lo = float(np.median([v[0] for v in vals]))
    hi = float(np.median([v[1] for v in vals]))
    span = max(hi - lo, 1e-6)
    return lo - 0.5 * span, hi + 0.5 * span  # room for the frames warmer or colder than the median


def reference_copy(src, dst: Path, stretch: tuple[float, float]) -> Path:
    """An 8-bit JPEG of a thermal frame for the alignment, with the EXIF (GPS, camera) of the
    original, stretched by ``stretch`` (:func:`stretch_of`) and sharpened a little (local contrast:
    a thermal frame is smooth, its features faint)."""
    from PIL import Image

    if dst.exists() and dst.stat().st_mtime >= Path(src).stat().st_mtime:
        return dst
    v = intensity(src)
    lo, hi = stretch
    img = np.clip((np.nan_to_num(v, nan=lo) - lo) / max(hi - lo, 1e-6) * 255.0, 0, 255).astype(np.uint8)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    img = clahe.apply(img)
    exif = b""
    try:
        with Image.open(src) as im:
            exif = im.info.get("exif") or b""
    except Exception:  # noqa: BLE001
        exif = b""
    dst.parent.mkdir(parents=True, exist_ok=True)
    pil = Image.fromarray(img)
    if exif:
        pil.save(dst, quality=97, exif=exif)
    else:
        pil.save(dst, quality=97)
    return dst


# ------------------------------------------------------------------------------ decoding
def frames_dir(chunk: Chunk) -> Path:
    return Path(chunk.data_dir) / "thermal" / "frames"


def _frame_path(chunk: Chunk, cam) -> Path:
    name = cam.meta.get("capture") or Path(cam.path).stem
    return frames_dir(chunk) / f"{name}.tif"


def _write_frame(path: Path, temp: np.ndarray, params: dict, source: str, decoder: str) -> None:
    import rasterio

    path.parent.mkdir(parents=True, exist_ok=True)
    h, w = temp.shape
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       nodata=float("nan"), compress="deflate", BIGTIFF="IF_SAFER") as dst:
        dst.write(temp.astype(np.float32), 1)
        dst.set_band_description(1, "temperature (degrees Celsius)")
        dst.update_tags(units="degC", source=str(source), decoder=decoder,
                        **{k: ("" if v is None else str(v)) for k, v in params.items()})


def _read_frame(path: Path) -> np.ndarray:
    import rasterio

    with rasterio.open(path) as src:
        return src.read(1).astype(np.float32)


def decode_file(path, params: dict | None = None, distance: float | None = None) -> np.ndarray:
    """The temperatures (degrees Celsius, float32) of one radiometric image."""
    params = dict(DEFAULTS if params is None else params)
    kind = kind_of(path)
    if kind == "tiff":
        from PIL import Image

        with Image.open(path) as im:
            t = np.asarray(im, np.float32)
        if t.ndim == 3:
            t = t[..., 0]
        finite = t[np.isfinite(t)]
        if finite.size and np.median(finite) > 150.0:  # kelvin
            t = t - 273.15
        return t
    if kind == "micasense":
        from PIL import Image

        with Image.open(path) as im:
            t = np.asarray(im, np.float32)
        return t / 100.0 - 273.15  # centikelvin
    if kind == "counts":
        scale, offset = params.get("scale"), params.get("offset")
        if scale is None or offset is None:
            raise ThermalDecoderMissing(f"{Path(path).name}: a TIFF of integers - set how they become degrees (scale "
                                        "and offset in the measure's parameters, e.g. 0.01 x value - 273.15 for "
                                        "centikelvin, 0.01 x value - 100 for hundredths of a degree above -100)")
        v = _tiff_values(path)
        return np.where(v > 0, v * float(scale) + float(offset), np.nan).astype(np.float32)  # 0: no value
    if kind == "dji":
        out = decode_dji([Path(path)], params, distances=[distance])
        return out[Path(path)]
    if kind == "flir":
        return decode_flir(path, params, distance)
    raise ValueError(f"{Path(path).name} holds no temperatures (not a radiometric JPEG or a temperature TIFF)")


# ------------------------------------------------------------------------------ FLIR radiometric JPEGs
# A FLIR radiometric JPEG carries a FLIR file (FFF) in its APP1 segments, each opened by "FLIR\0",
# 0x01, its number and the number of the last one: a big-endian header of 64 bytes, a directory of
# records of 32 bytes, the records. Two of them matter: the raw data (1: the counts of the sensor,
# 16-bit, or a PNG) and the camera information (0x20: the object parameters, FLIR's Planck
# constants of the camera, the constants of the atmosphere). The layout is the one ExifTool documents
# (Image::ExifTool::FLIR); the conversion from counts to degrees is FLIR's model as Thermimage writes
# it (raw2temp), the one the tools for FLIR files use.
FLIR_PLANCK = {"R1": 21106.77, "B": 1501.0, "F": 1.0, "O": -7340.0, "R2": 0.012545258}  # Thermimage's defaults
FLIR_ATMOSPHERE = {"ATA1": 0.006569, "ATA2": 0.01262, "ATB1": -0.002276, "ATB2": -0.00667, "ATX": 1.9}
FLIR_SEGMENT = 65524  # bytes of the FFF in one APP1 segment, as FLIR's cameras write them
FLIR_TILE = 2048  # side of the exported tiles: 256 segments hold 16.7 MB, a raw frame of 2896 x 2896 at most
_CAMERA_INFO_SIZE = 0x5D0
_PNG = b"\x89PNG\r\n\x1a\n"


def _flir_fff(data: bytes) -> bytes | None:
    """The FFF held by the APP1 segments of a JPEG, in the order of their numbers."""
    if data[:2] != b"\xff\xd8":
        return None
    chunks: dict[int, bytes] = {}
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:
            break
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        n = struct.unpack(">H", data[i + 2:i + 4])[0]
        body = data[i + 4:i + 2 + n]
        if marker == 0xE1 and body[:5] == b"FLIR\x00" and len(body) >= 8:
            chunks[body[6]] = body[8:]
        i += 2 + n
    if not chunks:
        return None
    return b"".join(chunks[k] for k in sorted(chunks))


def _flir_records(fff: bytes) -> dict[int, bytes]:
    """The records of an FFF by type."""
    if fff[:4] not in (b"FFF\x00", b"AFF\x00"):
        raise ValueError("not a FLIR file (FFF)")
    order = ">"
    if not 100 <= struct.unpack(">I", fff[0x14:0x18])[0] < 200:  # the version tells the byte order
        order = "<"
    pos, num = struct.unpack(order + "II", fff[0x18:0x20])
    out = {}
    for k in range(num):
        e = pos + 32 * k
        rtype = struct.unpack(order + "H", fff[e:e + 2])[0]
        if rtype == 0:
            continue
        off, length = struct.unpack(order + "II", fff[e + 12:e + 20])
        out.setdefault(rtype, fff[off:off + length])
    return out


def _record_order(rec: bytes) -> str:
    """A record's own byte order, from its first number (2 when read the right way)."""
    return "<" if struct.unpack("<H", rec[:2])[0] == 2 else ">"


def read_flir(path) -> tuple[np.ndarray, dict]:
    """The raw counts (float32) of a FLIR radiometric JPEG and its camera information: the object
    parameters the camera stored (``emissivity``, ``distance``, ``reflected``, ``ambient``,
    ``humidity`` in percent, ``window``, ``window_transmission``), FLIR's Planck constants (``R1``,
    ``B``, ``F``, ``O``, ``R2``) and those of the atmosphere (``ATA1``...``ATX``)."""
    fff = _flir_fff(Path(path).read_bytes())
    if fff is None:
        raise ValueError(f"{Path(path).name}: no FLIR data in the file")
    recs = _flir_records(fff)
    if 1 not in recs or 0x20 not in recs:
        raise ValueError(f"{Path(path).name}: the FLIR data has no raw data or no camera information")
    raw_rec = recs[1]
    o = _record_order(raw_rec)
    w, h = struct.unpack(o + "HH", raw_rec[2:6])
    body = raw_rec[32:]
    if body[:8] == _PNG:
        img = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"{Path(path).name}: the PNG of the FLIR raw data does not open")
        raw = img.astype(np.uint16)
        if o == "<":  # the counts written little-endian inside a PNG (most FLIR cameras do): swapped back
            raw = raw.byteswap()
    else:
        raw = np.frombuffer(body[:w * h * 2], o + "u2").reshape(h, w)
    cam = recs[0x20]
    o = _record_order(cam)

    def f32(at):
        return float(struct.unpack(o + "f", cam[at:at + 4])[0])

    info = {"emissivity": f32(0x20), "distance": f32(0x24), "reflected": f32(0x28) - 273.15,
            "ambient": f32(0x2C) - 273.15, "window": f32(0x30) - 273.15, "window_transmission": f32(0x34),
            "humidity": f32(0x3C), "R1": f32(0x58), "B": f32(0x5C), "F": f32(0x60),
            "O": float(struct.unpack(o + "i", cam[0x308:0x30C])[0]), "R2": f32(0x30C),
            "ATA1": f32(0x70), "ATA2": f32(0x74), "ATB1": f32(0x78), "ATB2": f32(0x7C), "ATX": f32(0x80)}
    info["humidity"] = info["humidity"] * 100.0 if info["humidity"] <= 2.0 else info["humidity"]  # a fraction, or percent
    if not info["window_transmission"]:
        info["window_transmission"] = 1.0
    if not all(info[k] for k in FLIR_ATMOSPHERE):
        info.update(FLIR_ATMOSPHERE)
    return raw.astype(np.float32), info


def _flir_tau(distance: float, humidity: float, ambient: float, k: dict) -> float:
    """FLIR's transmission of the air over half the distance (Thermimage's raw2temp)."""
    h2o = (humidity / 100.0) * np.exp(1.5587 + 0.06939 * ambient - 0.00027816 * ambient ** 2
                                       + 0.00000068455 * ambient ** 3)
    d = np.sqrt(distance / 2.0)
    return float(k["ATX"] * np.exp(-d * (k["ATA1"] + k["ATB1"] * np.sqrt(h2o)))
                 + (1 - k["ATX"]) * np.exp(-d * (k["ATA2"] + k["ATB2"] * np.sqrt(h2o))))


def _planck(t_c, k: dict):
    """The counts of a black body at ``t_c`` degrees."""
    return k["R1"] / (k["R2"] * (np.exp(k["B"] / (np.asarray(t_c, np.float64) + 273.15)) - k["F"])) - k["O"]


def _flir_terms(p: dict, k: dict) -> tuple[float, float]:
    """``raw = gain * (raw_object + offset)``: FLIR's model of what the camera counts for an object
    of radiance ``raw_object`` seen through the air and the window, the surroundings reflected."""
    e, irt = float(p["emissivity"]), float(p.get("window_transmission", 1.0) or 1.0)
    tau = _flir_tau(float(p["distance"]), float(p["humidity"]), float(p["ambient"]), k)
    window = p.get("window")
    window = float(p["ambient"]) if window is None else float(window)
    offset = ((1 - e) / e * _planck(p["reflected"], k)
              + (1 - tau) / e / tau * _planck(p["ambient"], k)
              + (1 - tau) / e / tau / irt / tau * _planck(p["ambient"], k)
              + (1 - irt) / e / tau / irt * _planck(window, k))
    return e * tau * irt * tau, float(offset)


def flir_temperature(raw: np.ndarray, params: dict, constants: dict | None = None) -> np.ndarray:
    """Degrees Celsius of FLIR raw counts with the object ``params`` (``emissivity``, ``distance``,
    ``reflected``, ``ambient``, ``humidity`` in percent; ``window``, ``window_transmission``) and the
    camera's ``constants`` (Planck and atmosphere; Thermimage's defaults when None)."""
    k = {**FLIR_PLANCK, **FLIR_ATMOSPHERE, **(constants or {})}
    gain, offset = _flir_terms(params, k)
    obj = np.asarray(raw, np.float64) / gain - offset
    val = k["R1"] / (k["R2"] * (obj + k["O"])) + k["F"]
    with np.errstate(invalid="ignore", divide="ignore"):
        t = k["B"] / np.log(val) - 273.15
    return np.where(val > 1.0, t, np.nan).astype(np.float32)


def flir_counts(t: np.ndarray, params: dict, constants: dict | None = None) -> np.ndarray:
    """The FLIR raw counts (float) that :func:`flir_temperature` turns back into ``t`` degrees."""
    k = {**FLIR_PLANCK, **FLIR_ATMOSPHERE, **(constants or {})}
    gain, offset = _flir_terms(params, k)
    return gain * (_planck(t, k) + offset)


def decode_flir(path, params: dict | None = None, distance: float | None = None) -> np.ndarray:
    """The temperatures of a FLIR radiometric JPEG with the chunk's object parameters (the camera's
    own calibration, its Planck constants, from the file); the distance is the flight height when
    not set, else the one the camera stored."""
    raw, info = read_flir(path)
    p = dict(DEFAULTS if params is None else params)
    if distance is None:
        distance = p.get("distance")
    if distance is None:
        distance = flight_height(path) or info["distance"] or 1.0
    p.update(distance=float(distance), window=info["window"], window_transmission=info["window_transmission"])
    constants = {k: info[k] for k in (*FLIR_PLANCK, *FLIR_ATMOSPHERE)}
    return flir_temperature(raw, p, constants)


def _printed(params: dict) -> dict:
    """The object parameters as ExifTool prints them (emissivity to 2 decimals, distance to the
    centimetre, temperatures to a tenth of a degree, humidity to a tenth of a percent): the tools
    that read FLIR files through ExifTool compute with these, so the counts are made with them too.
    A distance not set (the flight height of every frame) is written as 1 m."""
    distance = params.get("distance")
    return {"emissivity": round(float(params["emissivity"]), 2),
            "distance": round(float(1.0 if distance is None else distance), 2),
            "reflected": round(float(params["reflected"]), 1), "ambient": round(float(params["ambient"]), 1),
            "humidity": round(float(params["humidity"]), 1), "window": round(float(params["ambient"]), 1),
            "window_transmission": 1.0}


def _fff_bytes(raw: np.ndarray, p: dict, model: str) -> bytes:
    """An FFF with the raw data (16-bit little-endian, ExifTool turns it into a TIFF) and the camera
    information (the object parameters, Thermimage's Planck and atmosphere constants)."""
    h, w = raw.shape
    raw_head = np.zeros(16, "<u2")
    raw_head[:3] = (2, w, h)
    raw_head[6], raw_head[8] = w - 1, h - 1
    raw_rec = raw_head.tobytes() + np.ascontiguousarray(raw, "<u2").tobytes()
    cam = bytearray(_CAMERA_INFO_SIZE)
    struct.pack_into("<H", cam, 0, 2)
    for at, v in ((0x20, p["emissivity"]), (0x24, p["distance"]), (0x28, p["reflected"] + 273.15),
                  (0x2C, p["ambient"] + 273.15), (0x30, p["window"] + 273.15), (0x34, p["window_transmission"]),
                  (0x3C, p["humidity"] / 100.0), (0x58, FLIR_PLANCK["R1"]), (0x5C, FLIR_PLANCK["B"]),
                  (0x60, FLIR_PLANCK["F"]), (0x70, FLIR_ATMOSPHERE["ATA1"]), (0x74, FLIR_ATMOSPHERE["ATA2"]),
                  (0x78, FLIR_ATMOSPHERE["ATB1"]), (0x7C, FLIR_ATMOSPHERE["ATB2"]), (0x80, FLIR_ATMOSPHERE["ATX"]),
                  (0x90, 150.0 + 273.15), (0x94, -40.0 + 273.15), (0x30C, FLIR_PLANCK["R2"])):
        struct.pack_into("<f", cam, at, float(v))
    struct.pack_into("<i", cam, 0x308, int(FLIR_PLANCK["O"]))
    name = model.encode("ascii", "replace")[:31]
    cam[0xD4:0xD4 + len(name)] = name
    lo, hi = int(raw.min()), int(raw.max())
    struct.pack_into("<HH", cam, 0x310, lo, hi)
    struct.pack_into("<H", cam, 0x338, int(np.median(raw)))
    struct.pack_into("<H", cam, 0x33C, hi - lo)
    raw_at = 64 + 2 * 32
    cam_at = raw_at + len(raw_rec)
    head = bytearray(64)
    head[:4] = b"FFF\x00"
    head[4:20] = b"pyarchinit-ppg\x00\x00"
    struct.pack_into(">IIII", head, 0x14, 100, 64, 2, 3)
    entries = (struct.pack(">HHIIIIIII", 1, 2, 0x64, 1, raw_at, len(raw_rec), 0, 0, 0)
               + struct.pack(">HHIIIIIII", 0x20, 1, 0x64, 2, cam_at, len(cam), 0, 0, 0))
    return bytes(head) + entries + raw_rec + bytes(cam)


def write_flir_jpeg(path, t: np.ndarray, params: dict, lims: tuple[float, float] | None = None,
                    palette: str = "iron", model: str = "pyarchinit-photogrammetry") -> Path:
    """A FLIR radiometric JPEG of the temperatures ``t`` (degrees Celsius, NaN where nothing): the
    image is the palette rendering, the APP1 segments hold the FLIR file with the raw counts made
    with the inverse of FLIR's model for ``params`` (rounded as ExifTool prints them, see
    :func:`_printed`), so ExifTool, FLIR's tools and the programs built on them (Thermal-Image-Analysis,
    flirimageextractor, flyr) read these temperatures back, within 0.01 degrees. The format has no
    empty value: cells without a temperature get the coldest of the image (black in the picture).
    At most 2896 x 2896 cells (:data:`FLIR_TILE`)."""
    t = np.asarray(t, np.float32)
    h, w = t.shape
    if h * w * 2 + 2048 > 256 * FLIR_SEGMENT:
        raise ValueError(f"{w} x {h} cells do not fit in a FLIR file: export in tiles")
    ok = np.isfinite(t)
    if not ok.any():
        raise ValueError("no temperature to write")
    p = _printed(params)
    filled = np.where(ok, t, float(np.min(t[ok])))
    counts = flir_counts(filled, p)
    if counts.max() > 65535 or counts.min() < 1:
        raise ValueError(f"temperatures from {float(t[ok].min()):.1f} to {float(t[ok].max()):.1f} degrees do not fit "
                         "in FLIR's 16-bit counts with these parameters")
    raw = np.round(counts).astype(np.uint16)
    fff = _fff_bytes(raw, p, model)
    rgba = colours(t, lims, palette)
    bgr = cv2.cvtColor(rgba[..., :3], cv2.COLOR_RGB2BGR)
    bgr[~ok] = 0
    okj, jpeg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not okj:
        raise RuntimeError("the JPEG could not be encoded")
    jpeg = jpeg.tobytes()
    n = (len(fff) + FLIR_SEGMENT - 1) // FLIR_SEGMENT
    segments = b"".join(b"\xff\xe1" + struct.pack(">H", 2 + 8 + len(part)) + b"FLIR\x00\x01" + bytes((k, n - 1)) + part
                        for k, part in enumerate(fff[i:i + FLIR_SEGMENT] for i in range(0, len(fff), FLIR_SEGMENT)))
    at = 2
    if jpeg[2:4] == b"\xff\xe0":  # after the JFIF segment, which must come first
        at = 4 + struct.unpack(">H", jpeg[4:6])[0]
    path = Path(path)
    path.write_bytes(jpeg[:at] + segments + jpeg[at:])
    return path


def decode_all(chunk: Chunk, cams=None, progress: Progress | None = None, force: bool = False) -> int:
    """Decode the thermal frame of every capture of ``cams`` (all the multispectral cameras) into
    ``<data>/thermal/frames/``, all the DJI R-JPEGs in one run of the SDK; returns how many were
    decoded now."""
    from .multispectral import multispectral_cameras

    progress = as_progress(progress)
    cams = list(cams) if cams is not None else [c for c in multispectral_cameras(chunk) if thermal_file(c)]
    params = params_of(chunk)
    todo = [c for c in cams if thermal_file(c) and (force or not _frame_path(chunk, c).exists())]
    if not todo:
        return 0
    dji = [c for c in todo if kind_of(thermal_file(c)) == "dji"]
    other = [c for c in todo if c not in dji]
    n_done = 0
    for i, cam in enumerate(other):
        progress(0.9 * i / max(len(todo), 1), f"Temperatures of {cam.label} ({i + 1} of {len(todo)})")
        src = thermal_file(cam)
        _write_frame(_frame_path(chunk, cam), decode_file(src, params, _distance(params, src)), params, src, kind_of(src) or "")
        n_done += 1
    if dji:
        files = [Path(thermal_file(c)) for c in dji]
        temps = decode_dji(files, params, distances=[_distance(params, f) for f in files],
                           progress=progress.sub(0.9 * len(other) / max(len(todo), 1), 0.9 * len(dji) / max(len(todo), 1)))
        for cam, f in zip(dji, files):
            _write_frame(_frame_path(chunk, cam), temps[f], params, f, "dji")
            n_done += 1
    chunk.meta.setdefault("thermal", {})["decoded"] = len(list(frames_dir(chunk).glob("*.tif")))
    progress(1.0, f"Temperatures of {n_done} frames")
    return n_done


def _distance(params: dict, path) -> float:
    d = params.get("distance")
    if d is None:
        d = flight_height(path) or 5.0
    return float(min(max(float(d), DISTANCE_RANGE[0]), DISTANCE_RANGE[1]))


def thermal_file(cam) -> str | None:
    bands = cam.meta.get("bands") or {}
    for b in THERMAL_BANDS:
        if b in bands:
            return bands[b]
    return None


def temperature(chunk: Chunk, cam) -> np.ndarray:
    """The temperatures (degrees Celsius) of the thermal frame of ``cam``'s capture, decoded now
    when they are not in the chunk yet."""
    path = _frame_path(chunk, cam)
    if not path.exists():
        decode_all(chunk, [cam])
    return _read_frame(path)


# ------------------------------------------------------------------------------ DJI's SDK
class SdkInstallError(RuntimeError):
    """DJI's Thermal SDK could not be downloaded or is not what was expected."""


def sdk_folder() -> Path:
    """Where the program keeps DJI's Thermal SDK: ``<app data>/dji_tsdk``."""
    from ..ai.sam_env import data_dir

    return data_dir() / "dji_tsdk"


def _unzip_safely(archive: Path, dest: Path) -> None:
    """The archive unpacked into ``dest``: no member outside it (absolute paths, ``..``), the
    executable bits kept (``dji_irp`` runs as it is, also inside Docker)."""
    import zipfile

    dest = dest.resolve()
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if target != dest and dest not in target.parents:
                raise SdkInstallError(f"the archive has a file outside its folder: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            mode = (info.external_attr >> 16) & 0o777
            if os.name != "nt" and (mode or target.name.startswith("dji_")):
                os.chmod(target, (mode or 0o644) | (0o755 if target.name.startswith("dji_") and "." not in target.name else 0))


def install_sdk(progress: Progress | None = None, url: str = SDK_URL, sha256: str | None = None,
                folder: Path | None = None) -> Path:
    """DJI's Thermal SDK downloaded from DJI and unpacked into the program's data (``folder``, by
    default :func:`sdk_folder`), for whoever accepted DJI's licence first (the samples and the
    utilities such as dji_irp under the MIT licence, the rest under DJI's SDK EULA: the text is in
    ``License.txt`` of the SDK). The download is written aside, checked against ``sha256`` (by
    default the SHA-256 of the archive DJI published), unpacked without letting any file out of its
    folder, and put in place in one move, over an older SDK too; a failure leaves nothing behind.
    Returns the folder."""
    import datetime
    import hashlib
    import urllib.error
    import urllib.request

    from ..updates import _ssl_context

    progress = as_progress(progress)
    expected = SDK_SHA256 if sha256 is None and url == SDK_URL else sha256
    folder = Path(folder) if folder is not None else sdk_folder()
    folder.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="dji_tsdk_", dir=folder.parent))
    try:
        archive = work / (Path(urllib.request.url2pathname(url.rsplit("/", 1)[-1])).name or "sdk.zip")
        digest = hashlib.sha256()
        done = 0
        request = urllib.request.Request(url, headers={"User-Agent": "pyarchinit-photogrammetry"})
        kw = {"context": _ssl_context()} if url.lower().startswith("https:") else {}
        try:
            with urllib.request.urlopen(request, timeout=120, **kw) as r, open(archive, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0) or SDK_SIZE
                while True:
                    progress.check()
                    block = r.read(1 << 20)
                    if not block:
                        break
                    f.write(block)
                    digest.update(block)
                    done += len(block)
                    progress(0.9 * min(done / total, 1.0), f"Downloading DJI's Thermal SDK: {done / 1e6:.0f} MB")
        except (urllib.error.URLError, OSError) as exc:
            raise SdkInstallError(f"could not download DJI's Thermal SDK from {url}: {exc}") from exc
        got = digest.hexdigest()
        if expected and got != expected.lower():
            raise SdkInstallError(f"the file downloaded is not the SDK DJI published (SHA-256 {got[:12]}..., expected "
                                  f"{expected[:12]}...): download it from {SDK_PAGE_URL}")
        progress(0.92, "Unpacking DJI's Thermal SDK")
        unpacked = work / "dji_tsdk"
        unpacked.mkdir()
        try:
            _unzip_safely(archive, unpacked)
        except SdkInstallError:
            raise
        except Exception as exc:  # noqa: BLE001 - not a zip, a broken one
            raise SdkInstallError(f"the file downloaded is not a usable archive: {exc}") from exc
        if not any(p.name in ("dji_irp", "dji_irp.exe") for p in unpacked.rglob("dji_irp*")):
            raise SdkInstallError("the archive has no dji_irp: it is not DJI's Thermal SDK")
        (unpacked / "installed.json").write_text(json.dumps(
            {"version": SDK_VERSION if url == SDK_URL else None, "url": url, "sha256": got, "bytes": done,
             "installed": datetime.datetime.now().isoformat(timespec="seconds"),
             "licence": f"MIT for the samples and utilities, DJI's SDK EULA ({SDK_EULA_URL}) for the rest"}, indent=1))
        old = None
        if folder.exists():
            old = work / "previous"
            folder.rename(old)
        unpacked.rename(folder)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    progress(1.0, f"DJI's Thermal SDK in {folder}")
    return folder


def sdk_dir() -> Path | None:
    """The folder of DJI's Thermal SDK: ``PPG_DJI_TSDK``, else ``<app data>/dji_tsdk``."""
    env = os.environ.get("PPG_DJI_TSDK")
    if env and Path(env).exists():
        return Path(env)
    from ..ai.sam_env import data_dir

    d = data_dir() / "dji_tsdk"
    return d if d.exists() else None


def sdk_tool(linux: bool = False) -> Path | None:
    """``dji_irp`` of the SDK for this computer (``linux``: the Linux x86-64 one, for Docker)."""
    root = sdk_dir()
    if root is None:
        return None
    exe = "dji_irp.exe" if (os.name == "nt" and not linux) else "dji_irp"
    want = "windows" if (os.name == "nt" and not linux) else "linux"
    arch = "x64"
    if not linux and platform.machine().lower() in ("aarch64", "arm64") and sys.platform.startswith("linux"):
        arch = "aarch64"
    found = [p for p in root.rglob(exe) if p.is_file() and want in str(p).lower()]
    found.sort(key=lambda p: (arch not in str(p).lower(), "release" not in str(p).lower(), len(str(p))))
    return found[0] if found else None


def _docker() -> str | None:
    for c in (shutil.which("docker"), "/usr/local/bin/docker", "/Applications/Docker.app/Contents/Resources/bin/docker"):
        if c and Path(c).exists():
            return str(c)
    return None


def _docker_running(docker: str) -> bool:
    try:
        return subprocess.run([docker, "info"], capture_output=True, timeout=20, close_fds=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _ensure_image(docker: str, progress=None) -> str:
    """The Docker image that runs dji_irp (linux/amd64): ``PPG_DJI_DOCKER_IMAGE``, else
    :data:`DOCKER_IMAGE`, built from :data:`DOCKERFILE` the first time."""
    image = os.environ.get("PPG_DJI_DOCKER_IMAGE", DOCKER_IMAGE)
    have = subprocess.run([docker, "image", "inspect", image], capture_output=True, close_fds=False).returncode == 0
    if have or image != DOCKER_IMAGE:
        return image
    if progress:
        progress(0.0, "Preparing the Docker image for DJI's SDK (once)")
    proc = subprocess.run([docker, "build", "--platform", "linux/amd64", "-t", image, "-"], input=DOCKERFILE, text=True,
                          capture_output=True, close_fds=False)
    if proc.returncode != 0:
        raise ThermalDecoderMissing("could not prepare the Docker image for DJI's SDK: "
                                    + ((proc.stderr or proc.stdout).strip().splitlines() or ["?"])[-1])
    return image


def decoder_status() -> dict:
    """How DJI R-JPEGs would be decoded here: ``{"available", "mode", "tool", "message"}``.
    ``PPG_DJI_IRP`` names a ``dji_irp`` to run as it is (a native build, a wrapper script)."""
    own = os.environ.get("PPG_DJI_IRP")
    if own and Path(own).exists():
        return {"available": True, "mode": "native", "tool": own, "message": f"dji_irp ({own})"}
    if sdk_dir() is None:
        return {"available": False, "mode": None, "tool": None,
                "message": "DJI Thermal SDK not found: download it from dji.com (Windows or Linux version), "
                           "unzip it into the folder dji_tsdk of the program's data, or point PPG_DJI_TSDK to it"}
    if sys.platform == "darwin":
        tool = sdk_tool(linux=True)
        docker = _docker()
        if tool is None:
            return {"available": False, "mode": "docker", "tool": None, "message": "the SDK has no Linux x64 dji_irp"}
        if docker is None:
            return {"available": False, "mode": "docker", "tool": str(tool),
                    "message": "on macOS the SDK runs in Docker (linux/amd64): install Docker Desktop"}
        if not _docker_running(docker):
            return {"available": False, "mode": "docker", "tool": str(tool),
                    "message": "on macOS the SDK runs in Docker: start Docker Desktop and try again"}
        return {"available": True, "mode": "docker", "tool": str(tool), "message": f"DJI Thermal SDK through Docker ({docker})"}
    tool = sdk_tool()
    if tool is None:
        return {"available": False, "mode": "native", "tool": None, "message": "the SDK has no dji_irp for this system"}
    return {"available": True, "mode": "native", "tool": str(tool), "message": f"DJI Thermal SDK ({tool})"}


def _failure_detail(errors: str, stderr: str) -> str:
    """What went wrong, from the SDK's errors or Docker's: the line that says it (Docker ends with
    "Run 'docker run --help' for more information", which does not), with what to do when Docker's
    own disk is read-only or full (it happens when the computer's disk fills up while Docker writes)."""
    lines = [ln.strip() for ln in (errors.strip().splitlines() or stderr.strip().splitlines()) if ln.strip()]
    if not lines:
        return "no output"
    detail = next((ln for ln in lines if "error" in ln.lower()), lines[-1])
    text = (errors + stderr).lower()
    if "read-only file system" in text or "no space left" in text:
        detail += (" - Docker's disk is read-only or full (this happens when the computer's disk fills up while "
                   "Docker writes): free some space, then restart Docker Desktop")
    return detail


def _irp_args(src: str, out: str, params: dict, distance: float, ambient: bool = True) -> list[str]:
    args = ["-s", src, "-a", "measure", "-o", out, "--measurefmt", "float32",
            "--distance", f"{distance:g}", "--humidity", f"{float(params.get('humidity') or 70.0):g}",
            "--emissivity", f"{float(params['emissivity']):g}", "--reflection", f"{float(params['reflected']):g}"]
    if ambient and params.get("ambient") is not None:
        args += ["--ambient", f"{float(params['ambient']):g}"]
    return args


def decode_dji(files: list[Path], params: dict, distances: list | None = None, progress=None) -> dict[Path, np.ndarray]:
    """Temperatures of DJI R-JPEGs with the SDK's ``dji_irp -a measure --measurefmt float32``: one
    run of a script for all the files (natively, or in one Docker container on macOS, the files
    copied to a folder of the internal disk that Docker can mount)."""
    progress = as_progress(progress)
    status = decoder_status()
    if not status["available"]:
        raise ThermalDecoderMissing(status["message"])
    distances = list(distances or [None] * len(files))
    sizes = [image_size(f) for f in files]
    from ..ai.sam_env import data_dir

    base = data_dir() / "thermal_work"
    base.mkdir(parents=True, exist_ok=True)
    out: dict[Path, np.ndarray] = {}
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        work = Path(tmp)
        (work / "in").mkdir()
        (work / "out").mkdir()
        docker = status["mode"] == "docker"
        tool = Path(status["tool"])
        lines = ["#!/bin/sh", "set -u"]
        if docker:
            lib = "/sdk/" + str(tool.parent.relative_to(sdk_dir())).replace(os.sep, "/")
            exe = f"{lib}/{tool.name}"
        else:
            lib, exe = str(tool.parent), str(tool)
        lines.append(f'export LD_LIBRARY_PATH="{lib}:${{LD_LIBRARY_PATH:-}}"')  # the SDK's libraries beside dji_irp
        names = []
        for i, f in enumerate(files):
            name = f"f{i:05d}"
            if docker:
                shutil.copy(f, work / "in" / f"{name}.jpg")
                src, dst = f"/work/in/{name}.jpg", f"/work/out/{name}.raw"
            else:
                src, dst = str(f), str(work / "out" / f"{name}.raw")
            d = distances[i] if distances[i] is not None else _distance(params, f)
            args = _irp_args(src, dst, params, d)
            lines.append(" ".join(_q(a) for a in [exe] + args) + f" > /dev/null 2>> {'/work' if docker else str(work)}/errors.txt"
                         + f" || {' '.join(_q(a) for a in [exe] + _irp_args(src, dst, params, d, ambient=False))} > /dev/null 2>> "
                         + f"{'/work' if docker else str(work)}/errors.txt")
            lines.append(f'echo "{i + 1}" >> {"/work" if docker else str(work)}/progress.txt')
            names.append(name)
        script = work / "run.sh"
        script.write_text("\n".join(lines) + "\n")
        script.chmod(0o755)
        if docker:
            image = _ensure_image(_docker(), progress)
            cmd = [_docker(), "run", "--rm", "--platform", "linux/amd64", "-v", f"{sdk_dir()}:/sdk:ro", "-v", f"{work}:/work",
                   image, "sh", "/work/run.sh"]
        elif os.name == "nt":  # pragma: no cover - Windows: the same lines in a batch file
            bat = work / "run.bat"
            bat.write_text("\r\n".join(" ".join(_q(a) for a in [exe] + _irp_args(str(f), str(work / "out" / f"{n}.raw"), params,
                                                                               distances[i] if distances[i] is not None else _distance(params, f)))
                                       for i, (f, n) in enumerate(zip(files, names))) + "\r\n")
            cmd = ["cmd.exe", "/c", str(bat)]
        else:
            cmd = ["/bin/sh", str(script)]
        progress(0.0, f"Decoding {len(files)} thermal frames with DJI's SDK" + (" in Docker" if docker else ""))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, close_fds=False)
        try:
            import time

            while proc.poll() is None:
                time.sleep(0.5)
                done = (work / "progress.txt").read_text().count("\n") if (work / "progress.txt").exists() else 0
                try:
                    progress(min(0.99, done / max(len(files), 1)), f"Decoding thermal frames: {done} of {len(files)}")
                except Exception:
                    proc.terminate()
                    raise
        finally:
            if proc.poll() is None:
                proc.kill()
        _stdout, stderr = proc.communicate()
        errors = (work / "errors.txt").read_text(errors="replace") if (work / "errors.txt").exists() else ""
        for f, name, (w, h) in zip(files, names, sizes):
            raw = work / "out" / f"{name}.raw"
            if not raw.exists() or raw.stat().st_size != w * h * 4:
                raise RuntimeError(f"DJI's SDK did not decode {f.name}: {_failure_detail(errors, stderr)}")
            out[f] = np.fromfile(raw, dtype="<f4").reshape(h, w).astype(np.float32)
    progress(1.0, f"{len(out)} thermal frames decoded")
    return out


def _q(a: str) -> str:
    a = str(a)
    if a and all(c.isalnum() or c in "/._-=:+" for c in a):
        return a
    return "'" + a.replace("'", "'\\''") + "'"


# ------------------------------------------------------------------------------ registration
def _grad(img: np.ndarray, sigma: float = 1.0) -> np.ndarray:
    g = cv2.GaussianBlur(np.nan_to_num(img.astype(np.float32), nan=float(np.nanmedian(img)) if np.isfinite(img).any() else 0.0),
                         (0, 0), sigma)
    gx = cv2.Scharr(g, cv2.CV_32F, 1, 0)
    gy = cv2.Scharr(g, cv2.CV_32F, 0, 1)
    mag = np.sqrt(gx * gx + gy * gy)
    return ((mag - mag.mean()) / (mag.std() + 1e-9)).astype(np.float32)


def visible_grid(path, factor: int) -> np.ndarray:
    """The visible photo in grey, reduced ``factor`` times (2, 4 or 8: decoded reduced by the JPEG
    decoder itself), as the orthophoto engine and the registration see it."""
    flags = {2: cv2.IMREAD_REDUCED_GRAYSCALE_2, 4: cv2.IMREAD_REDUCED_GRAYSCALE_4,
             8: cv2.IMREAD_REDUCED_GRAYSCALE_8}.get(int(factor), cv2.IMREAD_GRAYSCALE)
    img = cv2.imread(str(path), flags | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        raise RuntimeError(f"cannot read {path}")
    if factor not in (2, 4, 8) and factor > 1:
        img = cv2.resize(img, (img.shape[1] // factor, img.shape[0] // factor), interpolation=cv2.INTER_AREA)
    return img.astype(np.float32)


def grid_factor(scale_tw: float) -> int:
    """The reduction of the visible photo that makes a thermal pixel about 1.2 cells of the grid
    (``scale_tw``: thermal pixels per visible pixel)."""
    want = 1.0 / (1.2 * max(scale_tw, 1e-6))
    return int(min((1, 2, 4, 8), key=lambda f: abs(np.log(f / max(want, 1.0)))))


def initial_homography(grid_shape, thermal_shape, cover: float) -> np.ndarray:
    """Grid pixel -> thermal pixel for a thermal frame centred on the visible one whose width spans
    ``cover`` of the grid's width (OpenCV pixel centres)."""
    gh, gw = grid_shape
    th, tw = thermal_shape
    a = tw / max(cover * gw, 1e-9)  # thermal pixels per grid pixel
    cgx, cgy = (gw - 1) / 2.0, (gh - 1) / 2.0
    ctx, cty = (tw - 1) / 2.0, (th - 1) / 2.0
    return np.array([[a, 0.0, ctx - a * cgx], [0.0, a, cty - a * cgy], [0.0, 0.0, 1.0]])


def _footprint_box(h_gt: np.ndarray, grid_shape, thermal_shape, shrink: float = 0.04) -> tuple[int, int, int, int]:
    """The box of the grid the thermal frame covers (its corners mapped back), shrunk a little."""
    th, tw = thermal_shape
    inv = np.linalg.inv(h_gt)
    corners = np.array([[0, 0, 1], [tw - 1, 0, 1], [tw - 1, th - 1, 1], [0, th - 1, 1]], float).T
    g = inv @ corners
    g = g[:2] / g[2]
    x0, x1 = g[0].min(), g[0].max()
    y0, y1 = g[1].min(), g[1].max()
    dx, dy = (x1 - x0) * shrink, (y1 - y0) * shrink
    gh, gw = grid_shape
    return (int(max(0, np.floor(x0 + dx))), int(max(0, np.floor(y0 + dy))),
            int(min(gw, np.ceil(x1 - dx))), int(min(gh, np.ceil(y1 - dy))))


def ecc(grid_grad: np.ndarray, thermal_grad: np.ndarray, h_gt: np.ndarray, motion: int = MOTION_HOMOGRAPHY,
        iterations: int = 80) -> tuple[np.ndarray, float]:
    """ECC of the thermal gradients on the grid's, from ``h_gt`` (grid pixel -> thermal pixel) on
    the part of the grid the frame covers. Returns the homography and the correlation (0 when ECC
    failed or OpenCV has no ECC, see :data:`ECC`: then the homography given)."""
    if not ECC:
        return h_gt, 0.0
    th, tw = thermal_grad.shape
    x0, y0, x1, y1 = _footprint_box(h_gt, grid_grad.shape, (th, tw))
    if x1 - x0 < 16 or y1 - y0 < 16:
        return h_gt, 0.0
    template = np.ascontiguousarray(grid_grad[y0:y1, x0:x1])
    shift = np.array([[1.0, 0.0, x0], [0.0, 1.0, y0], [0.0, 0.0, 1.0]])
    warp = (h_gt @ shift)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, int(iterations), 1e-5)
    try:
        if motion == MOTION_HOMOGRAPHY:
            cc, w = cv2.findTransformECC(template, thermal_grad, (warp / warp[2, 2]).astype(np.float32), motion, criteria, None, 3)
            refined = w.astype(np.float64)
        else:
            cc, w = cv2.findTransformECC(template, thermal_grad, warp[:2].astype(np.float32), motion, criteria, None, 3)
            refined = np.vstack([w.astype(np.float64), [0.0, 0.0, 1.0]])
    except cv2.error:
        return h_gt, 0.0
    return refined @ np.linalg.inv(shift), float(cc)


def correlation(grid_grad: np.ndarray, thermal_grad: np.ndarray, h_gt: np.ndarray) -> float:
    """The correlation of the gradients of the grid and of the thermal frame put on it by ``h_gt``
    (what ECC maximises), over the part the frame covers."""
    gh, gw = grid_grad.shape
    warped = cv2.warpPerspective(thermal_grad, np.asarray(h_gt, np.float64), (gw, gh),
                                 flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_CONSTANT,
                                 borderValue=float("nan"))
    x0, y0, x1, y1 = _footprint_box(h_gt, (gh, gw), thermal_grad.shape)
    a = grid_grad[y0:y1, x0:x1].ravel().astype(np.float64)
    b = warped[y0:y1, x0:x1].ravel().astype(np.float64)
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 100:
        return 0.0
    a, b = a[ok] - a[ok].mean(), b[ok] - b[ok].mean()
    return float((a @ b) / (np.sqrt((a @ a) * (b @ b)) + 1e-12))


def register(grid: np.ndarray, temp: np.ndarray, cover: float | None = None) -> tuple[np.ndarray, float, float]:
    """The homography (grid pixel -> thermal pixel) of a thermal frame on its visible photo
    reduced to ``grid``, its ECC correlation and the cover used. ``cover``: the part of the
    visible width the thermal frame spans (from the focal lengths); None or doubtful: searched,
    every cover from 0.3 to 1.0 tried with an affine ECC and the best kept."""
    gg = _grad(grid)
    tg = _grad(temp)
    covers = [cover] if cover else []
    covers += [c for c in np.round(np.arange(0.3, 1.01, 0.05), 2) if not cover or abs(c - cover) > 0.025]
    best = (None, -1.0, None)
    for c in covers:
        h0 = initial_homography(gg.shape, tg.shape, float(c))
        h, cc = ecc(gg, tg, h0, motion=MOTION_AFFINE, iterations=40)
        if cc > best[1]:
            best = (h, cc, float(c))
        if cover and c == cover and cc > 0.5:
            break  # the focal lengths were right
    h, cc, c = best
    if h is None:
        return initial_homography(gg.shape, tg.shape, cover or 0.75), 0.0, cover or 0.75
    h2, cc2 = ecc(gg, tg, h, motion=MOTION_HOMOGRAPHY, iterations=80)
    if cc2 >= cc:
        h, cc = h2, cc2
    return h, float(cc), float(c)


def cover_from_focals(visible_path, thermal_path, visible_focal_px: float | None, visible_size) -> float | None:
    """The part of the visible width the thermal frame spans, from the focal lengths: the visible
    one of the aligned sensor (pixels), the thermal one from its EXIF (35 mm equivalent)."""
    f35 = focal_35mm(thermal_path)
    if not f35 or not visible_focal_px:
        return None
    tw, th = image_size(thermal_path)
    f_t = f35 / 43.2666 * np.hypot(tw, th)  # 35 mm equivalents are on the diagonal
    vw = visible_size[0]
    # thermal width in visible pixels: tw * f_v / f_t
    return float(min(1.5, (tw * visible_focal_px / f_t) / vw))


def registrations(chunk: Chunk, cams=None, progress: Progress | None = None, samples: int = 24) -> dict:
    """The homography of every capture's thermal frame on its visible photo: one for the flight
    (the median of the captures where ECC agrees best, the camera being rigid), refined capture
    by capture where that agrees clearly better and stays close. Kept in ``meta["thermal"]
    ["registration"]``: ``{"factor", "cover", "fixed", "captures": {key: {"h", "cc", "refined"}}}``."""
    from .multispectral import multispectral_cameras

    progress = as_progress(progress)
    cams = [c for c in (cams if cams is not None else multispectral_cameras(chunk)) if thermal_file(c)]
    if not cams:
        raise RuntimeError("no thermal frames in the chunk (add the captures of a thermal camera)")
    first = cams[0]
    sensor = chunk.sensor(first)
    vis_size = (sensor.width, sensor.height)
    t0 = temperature(chunk, first)
    cover = cover_from_focals(first.path, thermal_file(first), getattr(sensor, "focal_px", None), vis_size)
    scale_tw = t0.shape[1] / ((cover or 0.75) * vis_size[0])
    factor = grid_factor(scale_tw)
    pick = cams if len(cams) <= samples else [cams[int(i)] for i in np.linspace(0, len(cams) - 1, samples)]
    found = []
    for i, cam in enumerate(pick):
        progress(0.5 * i / len(pick), f"Registering the thermal frames on the photos: {i + 1} of {len(pick)}")
        grid = visible_grid(cam.path, factor)
        h, cc, c = register(grid, temperature(chunk, cam), cover)
        found.append((h / h[2, 2], cc, c))
    good = [f for f in found if f[1] >= 0.25]
    if not good:
        good = sorted(found, key=lambda f: -f[1])[:max(1, len(found) // 3)]
    fixed = np.median(np.stack([g[0] for g in good]), axis=0)
    cover_used = float(np.median([g[2] for g in good]))
    out = {"factor": factor, "cover": cover_used, "cover_from_focals": cover, "fixed": fixed.tolist(),
           "fixed_cc": float(np.median([g[1] for g in good])), "captures": {}}
    for i, cam in enumerate(cams):
        progress(0.5 + 0.5 * i / len(cams), f"Thermal frame on its photo: {i + 1} of {len(cams)}")
        grid = visible_grid(cam.path, factor)
        temp = temperature(chunk, cam)
        gg, tg = _grad(grid), _grad(temp)
        cc_fixed = correlation(gg, tg, fixed)
        h, _cc = ecc(gg, tg, fixed, motion=MOTION_HOMOGRAPHY, iterations=60)
        cc = correlation(gg, tg, h)
        refined = cc > cc_fixed + 0.05 and _corner_shift(h, fixed, temp.shape) < 4.0
        out["captures"][str(cam.key)] = {"h": (h if refined else fixed).tolist(), "cc": float(cc if refined else cc_fixed),
                                         "refined": bool(refined)}
    chunk.meta.setdefault("thermal", {})["registration"] = out
    progress(1.0, f"Thermal frames registered: {sum(v['refined'] for v in out['captures'].values())} of {len(cams)} "
                  f"refined on their own, the others with the flight's homography (correlation {out['fixed_cc']:.2f})")
    return out


def _corner_shift(h1: np.ndarray, h2: np.ndarray, thermal_shape) -> float:
    """How far apart (thermal pixels) two homographies put the frame's corners."""
    th, tw = thermal_shape
    inv1, inv2 = np.linalg.inv(h1), np.linalg.inv(h2)
    corners = np.array([[0, 0, 1], [tw - 1, 0, 1], [tw - 1, th - 1, 1], [0, th - 1, 1]], float).T
    a = inv1 @ corners
    b = inv2 @ corners
    a, b = a[:2] / a[2], b[:2] / b[2]
    # grid pixels back to thermal pixels: the scale of h2
    scale = np.sqrt(abs(np.linalg.det(h2[:2, :2])))
    return float(np.max(np.hypot(*(a - b))) * scale)


def thermal_only(chunk: Chunk) -> bool:
    """The thermal frames are the photos (no visible camera): aligned themselves, no registration."""
    return (chunk.meta.get("multispectral") or {}).get("reference") == "LWIR"


def thermal_stack(chunk: Chunk, cam, offsets: dict | None = None) -> np.ndarray:
    """The temperatures of ``cam``'s capture on the reduced grid of its visible photo, NaN where
    the thermal frame does not reach: ``(h, w, 3)`` float32 (the three channels the orthophoto
    engine paints), the frame's offset taken away. When the thermal frames are the photos, the
    frame itself."""
    if thermal_only(chunk):
        temp = temperature(chunk, cam)
        off = (offsets or {}).get(str(cam.key))
        if off:
            temp = temp - np.float32(off)
        return np.repeat(temp[..., None], 3, axis=2).astype(np.float32)
    reg = (chunk.meta.get("thermal") or {}).get("registration")
    if not reg:
        raise RuntimeError("register the thermal frames first")
    factor = int(reg["factor"])
    sensor = chunk.sensor(cam)
    gw, gh = -(-sensor.width // factor), -(-sensor.height // factor)  # what the reduced JPEG decoding gives
    entry = reg["captures"].get(str(cam.key))
    h = np.asarray(entry["h"] if entry else reg["fixed"], np.float64)
    temp = temperature(chunk, cam)
    off = (offsets or {}).get(str(cam.key))
    if off:
        temp = temp - np.float32(off)
    warped = cv2.warpPerspective(temp, h, (gw, gh), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=float("nan"))
    return np.repeat(warped[..., None], 3, axis=2).astype(np.float32)


# ------------------------------------------------------------------------------ offsets between frames
def balance_offsets(chunk: Chunk, cams=None, samples: int = 4000, rounds: int = 40, progress: Progress | None = None) -> dict:
    """The offset of every frame (degrees) that brings the frames to the same temperature where
    they overlap: surface points read in every frame, each reading split into a temperature of the
    point and an offset of the frame (least squares by alternation, offsets of mean zero, readings
    beyond three MADs dropped half way). ``{key: offset}`` plus ``"spread"`` (their standard
    deviation): what the flat-field corrections of the camera did between frames."""
    from .colour import _surface_points
    from .multispectral import multispectral_cameras
    from .visibility import project_valid

    progress = as_progress(progress)
    cams = [c for c in (cams if cams is not None else multispectral_cameras(chunk)) if c.aligned and thermal_file(c)]
    if len(cams) < 2:
        return {}
    pts = _surface_points(chunk, samples)
    if not len(pts):
        return {}
    point, photo, value = [], [], []
    for i, cam in enumerate(cams):
        progress(i / len(cams), f"Balancing the thermal frames: {i + 1} of {len(cams)}")
        px, _depth, ok = project_valid(chunk, cam, pts, margin=4.0)
        idx = np.nonzero(ok)[0]
        if not len(idx):
            continue
        stack = thermal_stack(chunk, cam)[..., 0]
        sensor = chunk.sensor(cam)
        sx, sy = stack.shape[1] / sensor.width, stack.shape[0] / sensor.height
        c = np.clip(np.floor(px[idx, 0] * sx).astype(int), 0, stack.shape[1] - 1)
        r = np.clip(np.floor(px[idx, 1] * sy).astype(int), 0, stack.shape[0] - 1)
        v = stack[r, c]
        fin = np.isfinite(v)
        point.append(idx[fin])
        photo.append(np.full(fin.sum(), i))
        value.append(v[fin].astype(np.float64))
    if not point:
        return {}
    point, photo, value = np.concatenate(point), np.concatenate(photo), np.concatenate(value)
    seen = np.bincount(point, minlength=len(pts))
    keep = seen[point] >= 2
    point, photo, value = point[keep], photo[keep], value[keep]
    if not len(point):
        return {}
    o, n_ph = _fit_offsets(point, photo, value, len(pts), len(cams), rounds)
    out = {str(cam.key): float(o[i]) for i, cam in enumerate(cams) if n_ph[i] > 0}
    out["spread"] = float(o[n_ph > 0].std())
    progress(1.0, f"Thermal frames balanced: offsets of {out['spread']:.2f} degrees")
    return out


def _fit_offsets(point, photo, value, n_points: int, n_frames: int, rounds: int = 40) -> tuple[np.ndarray, np.ndarray]:
    """Readings split into a temperature of the point and an offset of the frame (least squares by
    alternation, offsets of mean zero). Half way the readings beyond three MADs of the others once
    the frames' offsets are taken off are dropped - never the readings of a frame for being far
    from the others before its offset. Returns the offsets and the readings kept per frame."""
    o = np.zeros(n_frames)
    n_ph = np.zeros(n_frames)
    keep = np.ones(len(value), bool)
    for it in range(rounds):
        n_pt = np.bincount(point[keep], minlength=n_points).astype(float)
        temp = np.bincount(point[keep], (value - o[photo])[keep], minlength=n_points) / np.maximum(n_pt, 1)
        res = value - temp[point]
        if it == rounds // 2:
            rest = res - o[photo]
            mid = np.median(rest[keep])
            mad = np.median(np.abs(rest[keep] - mid)) * 1.4826 + 1e-9
            keep &= np.abs(rest - mid) <= 3 * mad + 0.05
        n_ph = np.bincount(photo[keep], minlength=n_frames).astype(float)
        o = np.bincount(photo[keep], res[keep], minlength=n_frames) / np.maximum(n_ph, 1)
        if (n_ph > 0).any():
            o -= o[n_ph > 0].mean()
    return o, n_ph


# ------------------------------------------------------------------------------ the orthomosaic
def thermal_gsd(chunk: Chunk) -> float:
    """The ground size of a thermal pixel: the visible photos' one times the visible pixels a
    thermal pixel spans (rounded to a millimetre)."""
    from .dem import estimate_gsd

    reg = (chunk.meta.get("thermal") or {}).get("registration") or {}
    fixed = reg.get("fixed")
    gsd = estimate_gsd(chunk)
    if thermal_only(chunk):
        return max(0.001, round(gsd, 3))
    if fixed is None:
        return round(gsd * 8, 3)
    h = np.asarray(fixed, float)
    a = np.sqrt(abs(np.linalg.det(h[:2, :2])))  # thermal pixels per grid pixel
    visible_per_thermal = int(reg["factor"]) / max(a, 1e-9)
    return max(0.001, round(gsd * visible_per_thermal, 3))


def build_thermal(chunk: Chunk, resolution: float | None = None, balance: bool = True, progress: Progress | None = None,
                  **ortho_options) -> Path:
    """The temperature orthomosaic: ``<data>/multispectral/thermal.tif``, one float32 band in
    degrees Celsius on the grid of the orthophoto engine (``resolution`` m/px, by default the
    thermal pixel's ground size), NaN where nothing, the object parameters in its tags."""
    import rasterio

    from .multispectral import multispectral_cameras
    from .ortho import build_orthomosaic

    progress = as_progress(progress)
    cams = [c for c in multispectral_cameras(chunk) if thermal_file(c)]
    if not cams:
        raise RuntimeError("no thermal frames in the chunk (add the captures of a thermal camera)")
    if chunk.dem_path is None:
        raise RuntimeError("build the DEM first: the temperature orthomosaic lies on it")
    decode_all(chunk, cams, progress.sub(0.0, 0.25))
    reg = None if thermal_only(chunk) else registrations(chunk, cams, progress.sub(0.25, 0.15))
    meta = chunk.meta.setdefault("thermal", {})
    offsets = None
    if balance:
        offsets = balance_offsets(chunk, cams, progress=progress.sub(0.4, 0.05))
        meta["offsets"] = offsets
    else:
        meta.pop("offsets", None)
    keys = {c.key for c in cams}

    def source(cam):
        if cam.key not in keys:
            s = chunk.sensor(cam)
            f = int(reg["factor"]) if reg else 1
            return np.full((-(-s.height // f), -(-s.width // f), 3), np.nan, np.float32)
        return thermal_stack(chunk, cam, offsets)

    folder = Path(chunk.data_dir) / "multispectral"
    folder.mkdir(parents=True, exist_ok=True)
    resolution = resolution or thermal_gsd(chunk)
    part = folder / "thermal_part.tif"
    build_orthomosaic(chunk, resolution=resolution, source=source, out_path=part, progress=progress.sub(0.45, 0.5),
                      **ortho_options)
    final = folder / "thermal.tif"
    params = params_of(chunk)
    with rasterio.open(part) as src:
        profile = src.profile.copy()
        t = src.read(1)
    profile.update(count=1, dtype="float32", nodata=float("nan"), BIGTIFF="IF_SAFER")
    with rasterio.open(final, "w", **profile) as dst:
        dst.write(t.astype(np.float32), 1)
        dst.set_band_description(1, "temperature (degrees Celsius)")
        dst.update_tags(units="degC", emissivity=str(params["emissivity"]), reflected_temperature=str(params["reflected"]),
                        ambient_temperature=str(params["ambient"]), humidity=str(params["humidity"]),
                        distance="flight height" if params["distance"] is None else str(params["distance"]),
                        offsets_balanced=str(bool(balance)),
                        **({"raw_scale": str(params["scale"]), "raw_offset": str(params["offset"])}
                           if params.get("scale") is not None and params.get("offset") is not None else {}))
    part.unlink(missing_ok=True)
    v = t[np.isfinite(t)]
    meta["file"] = str(final.relative_to(chunk.data_dir))
    meta["resolution"] = float(resolution)
    meta["stats"] = ({"min": float(v.min()), "max": float(v.max()), "median": float(np.median(v)),
                      "p2": float(np.percentile(v, 2)), "p98": float(np.percentile(v, 98))} if v.size else {})
    progress(1.0, f"Temperature orthomosaic: {resolution * 100:.1f} cm/px" + (
        f", {meta['stats']['p2']:.1f} - {meta['stats']['p98']:.1f} degrees" if v.size else ""))
    return final


def thermal_path(chunk: Chunk) -> Path | None:
    f = (chunk.meta.get("thermal") or {}).get("file")
    p = Path(chunk.data_dir) / f if f else None
    return p if p is not None and p.exists() else None


# ------------------------------------------------------------------------------ products from it
def anomalies(chunk: Chunk, radius_m: float = 2.0) -> Path:
    """The thermal anomalies: the temperature minus the mean of its surroundings (a Gaussian of
    ``radius_m``, NaN-aware): ``<data>/multispectral/thermal_anomaly.tif``, degrees, around zero."""
    import rasterio

    src_path = thermal_path(chunk)
    if src_path is None:
        raise RuntimeError("build the temperature orthomosaic first")
    with rasterio.open(src_path) as src:
        t = src.read(1).astype(np.float32)
        profile = src.profile.copy()
        res = abs(src.transform.a)
    ok = np.isfinite(t)
    sigma = max(1.0, radius_m / max(res, 1e-9) / 2.0)
    filled = np.where(ok, t, 0.0).astype(np.float32)
    num = cv2.GaussianBlur(filled, (0, 0), sigma)
    den = cv2.GaussianBlur(ok.astype(np.float32), (0, 0), sigma)
    local = np.where(den > 1e-3, num / np.maximum(den, 1e-6), np.nan)
    out = np.where(ok, t - local, np.nan).astype(np.float32)
    target = src_path.with_name("thermal_anomaly.tif")
    profile.update(count=1, dtype="float32", nodata=float("nan"), BIGTIFF="IF_SAFER")
    with rasterio.open(target, "w", **profile) as dst:
        dst.write(out, 1)
        dst.set_band_description(1, f"temperature minus its surroundings within {radius_m:g} m (degrees)")
        dst.update_tags(units="degC", radius_m=str(radius_m))
    meta = chunk.meta.setdefault("thermal", {})
    meta["anomaly"] = {"file": str(target.relative_to(chunk.data_dir)), "radius_m": float(radius_m)}
    return target


def difference(chunk: Chunk, other: Path, name: str = "thermal_difference") -> Path:
    """This temperature orthomosaic minus another one of the same ground (a night flight against a
    day flight: soils that keep the heat differently), on this one's grid."""
    import rasterio
    from rasterio.warp import Resampling, reproject

    mine = thermal_path(chunk)
    if mine is None:
        raise RuntimeError("build the temperature orthomosaic first")
    with rasterio.open(mine) as a:
        ta = a.read(1).astype(np.float32)
        profile = a.profile.copy()
        tr, crs = a.transform, a.crs
    with rasterio.open(other) as b:
        tb = np.full(ta.shape, np.nan, np.float32)
        reproject(b.read(1).astype(np.float32), tb, src_transform=b.transform, src_crs=b.crs or crs, dst_transform=tr,
                  dst_crs=crs, src_nodata=np.nan, dst_nodata=np.nan, resampling=Resampling.bilinear)
    out = mine.with_name(f"{name}.tif")
    profile.update(BIGTIFF="IF_SAFER")
    with rasterio.open(out, "w", **profile) as dst:
        dst.write((ta - tb).astype(np.float32), 1)
        dst.update_tags(units="degC", minus=str(other))
    return out


def export(chunk: Chunk, path, fmt: str | None = None, source: Path | None = None, tile: int = FLIR_TILE,
           palette: str = "iron") -> Path:
    """The temperatures in a file that keeps them: ``tif`` float32 GeoTIFF in degrees Celsius,
    ``tif16`` 16-bit GeoTIFF in centikelvin (0 = nothing; degrees = value / 100 - 273.15, as
    MicaSense writes its thermal band), ``csv`` one line per cell with x, y and the temperature,
    ``flir`` FLIR radiometric JPEGs (:func:`export_flir`: tiles of ``tile`` cells with world files).
    The parameters of the measure go with it (tags, the CSV's header, the FLIR camera information)."""
    import rasterio

    src_path = Path(source) if source is not None else thermal_path(chunk)
    if src_path is None or not src_path.exists():
        raise RuntimeError("build the temperature orthomosaic first")
    path = Path(path)
    fmt = fmt or {".csv": "csv", ".txt": "csv", ".jpg": "flir", ".jpeg": "flir"}.get(path.suffix.lower(), "tif")
    if fmt == "flir":
        return export_flir(chunk, path, src_path, tile=tile, palette=palette)
    with rasterio.open(src_path) as src:
        t = src.read(1).astype(np.float32)
        profile = src.profile.copy()
        tags = src.tags()
        tr = src.transform
    if fmt == "tif":
        shutil.copy(src_path, path)
        return path
    if fmt == "tif16":
        ck = np.where(np.isfinite(t), np.clip(np.round((t + 273.15) * 100.0), 1, 65535), 0).astype(np.uint16)
        profile.update(dtype="uint16", nodata=0, BIGTIFF="IF_SAFER")
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(ck, 1)
            dst.set_band_description(1, "temperature in centikelvin (degrees = value / 100 - 273.15)")
            dst.update_tags(**{**tags, "units": "centikelvin", "scale": "0.01", "offset": "-273.15"})
            dst.scales, dst.offsets = (0.01,), (-273.15,)
        return path
    if fmt == "csv":
        rows, cols = np.nonzero(np.isfinite(t))
        xs, ys = rasterio.transform.xy(tr, rows, cols, offset="center")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# temperature in degrees Celsius; " + json.dumps({k: tags[k] for k in sorted(tags)}) + "\n")
            f.write("x,y,temperature_c\n")
            step = 200000
            for s in range(0, len(rows), step):
                block = np.column_stack([np.asarray(xs[s:s + step]), np.asarray(ys[s:s + step]), t[rows[s:s + step], cols[s:s + step]]])
                np.savetxt(f, block, fmt=["%.3f", "%.3f", "%.2f"], delimiter=",")
        return path
    raise ValueError(f"unknown format {fmt!r}: tif, tif16, csv or flir")


def _export_distance(chunk: Chunk, params: dict, sample: int = 40) -> float:
    """The distance the temperatures were measured at: the one set, else the median flight height of
    the thermal frames (as :func:`_distance` takes it for every frame)."""
    if params.get("distance") is not None:
        return float(params["distance"])
    files = [f for f in (thermal_file(c) for c in chunk.cameras if c.enabled) if f]
    if not files:
        return 1.0
    step = max(1, len(files) // sample)
    return float(np.median([_distance(params, f) for f in files[::step]]))


def export_flir(chunk: Chunk, path, source: Path, tile: int = FLIR_TILE, palette: str = "iron") -> Path:
    """The temperature orthomosaic as FLIR radiometric JPEGs (:func:`write_flir_jpeg`): one file when
    it fits in ``tile`` cells a side, else tiles ``<name>_r<row>_c<column>.jpg`` with an index
    ``<name>_tiles.csv`` (file, bounds, coldest and hottest degrees, cells with a value). Every JPEG
    has its world file (``.jgw``) and its reference system (``.aux.xml``): QGIS and GDAL place it.
    The colours of all the tiles share the ramp of the whole orthomosaic. The object parameters
    written are those the temperatures were measured with (the distance: the median flight height).
    Returns the file, or the index of the tiles."""
    import rasterio
    from rasterio.windows import Window

    path = Path(path).with_suffix(".jpg")
    tile = int(min(max(tile, 64), 2800))
    params = params_of(chunk)
    params = {**params, "distance": _export_distance(chunk, params)}
    with rasterio.open(source) as src:
        wkt = src.crs.to_wkt() if src.crs else None
        tr = src.transform
        h, w = src.height, src.width
        step = max(1, int(np.ceil(max(h, w) / 2000)))
        lims = limits(src.read(1, out_shape=(max(1, h // step), max(1, w // step))).astype(np.float32))
        tiles = []
        for r0 in range(0, h, tile):
            for c0 in range(0, w, tile):
                win = Window(c0, r0, min(tile, w - c0), min(tile, h - r0))
                t = src.read(1, window=win).astype(np.float32)
                ok = np.isfinite(t)
                if not ok.any():
                    continue
                single = h <= tile and w <= tile
                out = path if single else path.with_name(f"{path.stem}_r{r0 // tile:02d}_c{c0 // tile:02d}.jpg")
                write_flir_jpeg(out, t, params, lims, palette)
                wt = rasterio.windows.transform(win, tr)
                # the world file: the size of a cell and the centre of the first one
                out.with_suffix(".jgw").write_text("\n".join(f"{v:.10f}" for v in (
                    wt.a, wt.d, wt.b, wt.e, wt.c + wt.a / 2 + wt.b / 2, wt.f + wt.d / 2 + wt.e / 2)) + "\n")
                if wkt:
                    from xml.sax.saxutils import escape

                    Path(str(out) + ".aux.xml").write_text(f"<PAMDataset>\n  <SRS>{escape(wkt)}</SRS>\n</PAMDataset>\n")
                b = rasterio.windows.bounds(win, tr)
                tiles.append((out.name, b[0], b[1], b[2], b[3], float(np.min(t[ok])), float(np.max(t[ok])),
                              int(ok.sum())))
    if not tiles:
        raise RuntimeError("the temperature orthomosaic is empty")
    if len(tiles) == 1 and tiles[0][0] == path.name:
        return path
    index = path.with_name(f"{path.stem}_tiles.csv")
    with open(index, "w", encoding="utf-8") as f:
        f.write("# FLIR radiometric JPEGs of " + Path(source).name + "; " + json.dumps(_printed(params)) + "\n")
        f.write("file,xmin,ymin,xmax,ymax,tmin_c,tmax_c,cells\n")
        for row in tiles:
            f.write(f"{row[0]},{row[1]:.3f},{row[2]:.3f},{row[3]:.3f},{row[4]:.3f},{row[5]:.2f},{row[6]:.2f},{row[7]}\n")
    return index


# ------------------------------------------------------------------------------ colours
def limits(t: np.ndarray, low: float | None = None, high: float | None = None) -> tuple[float, float]:
    """The ends of the colour ramp: the given ones, else the 2nd and 98th percentiles."""
    v = t[np.isfinite(t)]
    if v.size == 0:
        return 0.0, 1.0
    lo = float(np.percentile(v, 2)) if low is None else float(low)
    hi = float(np.percentile(v, 98)) if high is None else float(high)
    return (lo, hi) if hi > lo else (lo, lo + 1e-3)


def colours(t: np.ndarray, lims: tuple[float, float] | None = None, palette: str = "iron") -> np.ndarray:
    """RGBA of temperatures on a :data:`PALETTES` ramp between ``lims``, transparent where NaN."""
    ok = np.isfinite(t)
    rgba = np.zeros(t.shape + (4,), np.uint8)
    if not ok.any():
        return rgba
    lo, hi = lims if lims is not None else limits(t)
    x = np.clip((np.where(ok, t, lo) - lo) / max(hi - lo, 1e-9), 0, 1)
    stops = PALETTES.get(palette, PALETTES["iron"])
    pos = np.array([s[0] for s in stops])
    for k in range(3):
        rgba[..., k] = np.interp(x, pos, [s[1][k] for s in stops]).astype(np.uint8)
    rgba[..., 3] = np.where(ok, 255, 0)
    return rgba
