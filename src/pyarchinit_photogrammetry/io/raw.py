"""Camera raw files (DNG, CR2/CR3, NEF, ARW, RAF, ORF, RW2, PEF...) as photos.

A raw file is no image the libraries of the pipeline read: PIL and COLMAP open its thumbnail or its embedded
preview (another size), OpenCV nothing. So a raw is *developed* once into an ordinary image inside the chunk folder
(``<project>.ppg.files/chunk_N/raw/``) and the camera points to that copy - as the reference copies of the
multispectral captures do - while the raw stays where it is, named in ``camera.meta["raw"]``. Every reader then
works unchanged. This module is the one place that knows about raws: extensions, the optional decoder (rawpy,
LibRaw), their EXIF, the development and its cache.

The development is the same for every frame: white balance of the camera, no automatic brightness, sRGB, 8 bit,
the pixels as the sensor read them (no rotation: one sensor model for frames shot upright and across), the full
size (never the embedded preview), one exposure shift and one tone for all the frames of a camera.

The tone (:data:`TONES`) is there for the alignment, not for the look: a linear development has far less contrast
than the JPEG a camera writes, and the key points are found by their contrast. Measured on raws of four cameras,
the linear copies gave from a third to a sixth of the key points of the camera JPEGs; the curve of "camera" gives
about as many. The raws of phones that are no sensor mosaic (linear DNG: Apple ProRAW) are flatter still - the
phone completes them with a local tone map stored in the file, which LibRaw does not apply: eight such frames
that align from their JPEGs did not align with the curve alone, and do with the local contrast of "local". On
the raws of the other cameras "local" is too much (harsh textures), hence "auto". No tone moves a pixel.

The phone's tone map is in the file, though: Apple ProRAW stores the look of the phone as DNG tags - BaselineExposure,
a local tone map (ProfileGainTableMap: a gain for each place of the frame and each brightness) and a
ProfileToneCurve - and the tone "file" applies them as the DNG specification says (:func:`_file_tone`); measured on
real ProRAW frames it gives the tones of the phone's own JPEG within three grey levels. The phone also sharpens its
JPEG at several scales (two to three times the amplitude of the development between 0.05 and 0.3 cycles per pixel):
thirty frames of a vase that align all from their JPEGs aligned 27 from the faithful tones alone, all 30 with that
detail back (:data:`_DETAIL`). "auto" is "file" with that detail for a linear DNG that has the map.
"""
from __future__ import annotations

import importlib.util
import json
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Iterable

from ..core.progress import ProgressCallback, as_progress

# what LibRaw develops, by the extensions cameras write (".raw" is left out: too many other files carry it)
RAW_EXTENSIONS = frozenset({
    ".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf", ".orf", ".rw2", ".pef",
    ".srw", ".3fr", ".fff", ".iiq", ".erf", ".kdc", ".dcr", ".mrw", ".mos", ".mef", ".rwl", ".x3f"})
FORMATS = ("jpg", "tif")
# the development of a raw: file of the copy, JPEG quality, exposure shift in EV (the same for every frame),
# half the size (one pixel per 2x2 cell of the sensor: four times less disk and time; a linear DNG, which has no
# cells, is developed whole and then averaged 2x2: the same size, less time saved)
# the tone: "camera" the contrast curve of a camera JPEG (_tone_table), "local" the curve and the contrast evened
# out over the frame (_local_contrast), "linear" neither; "auto" is "local" for a raw that is no sensor mosaic
# (the linear DNG of the phones), "camera" for the others
# "file" the look the DNG carries (a phone's ProRAW), "local" for a raw that has none
TONES = ("auto", "camera", "local", "linear", "file")
# detail: the amount of the band boost (_DETAIL), "auto" that of the phones for the tone "file", 0 for the others
DEFAULTS = {"format": "jpg", "quality": 95, "exposure": 1.0, "tone": "auto", "half_size": False, "detail": "auto"}
_CURVE = (6.0, 0.4)  # the curve of "camera": the steepness of a logistic curve and the level it turns around
_LOCAL = (3.0, 8)  # the local contrast: the limit of the equalisation and the tiles per side
# the detail: a band between two Gaussian blurs (sigmas in pixels) added back times an amount; the amount of the phones
# (tone "file") from the spectrum of ProRAW developments against the JPEGs of the same frames
_DETAIL = (0.6, 3.0, 2.3)
_STRIP = 512  # rows at a time for the tone map and the detail: memory bound with several raws developed at once
# the DNG tags of the look: BaselineExposure, ProfileToneCurve, ProfileGainTableMap; SubIFDs
_BASELINE_EXPOSURE, _TONE_CURVE, _GAIN_TABLE, _SUB_IFDS = 50730, 50940, 52525, 330
FOLDER = "raw"  # in the chunk folder
INDEX = "index.json"
PREFER = ("jpeg", "raw")  # which file of a raw + JPEG pair becomes the photo


class RawUnavailable(RuntimeError):
    """A raw file was given and the decoder (rawpy) is not installed."""


def is_raw(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() in RAW_EXTENSIONS


def available() -> bool:
    """rawpy is installed (looked up, not imported)."""
    return importlib.util.find_spec("rawpy") is not None


def unavailable_message() -> str:
    from ..gui.i18n import tr

    return tr("I file raw delle fotocamere (DNG, CR2, NEF, ARW...) richiedono il modulo rawpy, che non è "
              "installato: pip install \"pyarchinit-photogrammetry[raw]\". In alternativa esportare le foto in "
              "JPEG o TIFF.")


def _rawpy():
    if not available():
        raise RawUnavailable(unavailable_message())
    import rawpy

    return rawpy


def version() -> str | None:
    """``"rawpy 0.28.0, LibRaw 0.22.2"`` or ``None`` without rawpy."""
    if not available():
        return None
    try:
        import rawpy

        return f"rawpy {rawpy.__version__}, LibRaw {'.'.join(map(str, rawpy.libraw_version))}"
    except Exception as exc:  # installed and broken
        return f"rawpy: {exc}"


def settings(overrides: dict | None = None) -> dict:
    """:data:`DEFAULTS` with ``overrides``, checked (ValueError on a key or a value that is not one of them)."""
    out = dict(DEFAULTS)
    for key, value in (overrides or {}).items():
        if key not in DEFAULTS:
            raise ValueError(f"raw development: unknown setting {key!r} (one of {', '.join(DEFAULTS)})")
        if value is not None:
            out[key] = value
    out["format"] = str(out["format"]).lower().lstrip(".").replace("jpeg", "jpg").replace("tiff", "tif")
    if out["format"] not in FORMATS:
        raise ValueError(f"raw development: format must be one of {', '.join(FORMATS)}")
    out["quality"] = int(out["quality"])
    if not 50 <= out["quality"] <= 100:
        raise ValueError("raw development: quality between 50 and 100")
    out["exposure"] = round(float(out["exposure"]), 3)
    if not -2.0 <= out["exposure"] <= 3.0:
        raise ValueError("raw development: exposure between -2 and +3 EV")
    out["tone"] = str(out["tone"]).lower()
    if out["tone"] not in TONES:
        raise ValueError(f"raw development: tone must be one of {', '.join(TONES)}")
    out["half_size"] = bool(out["half_size"])
    if str(out["detail"]).lower() == "auto":
        out["detail"] = "auto"
    else:
        try:
            out["detail"] = round(float(out["detail"]), 3)
        except ValueError:
            raise ValueError("raw development: detail \"auto\" or between 0 and 4") from None
        if not 0.0 <= out["detail"] <= 4.0:
            raise ValueError("raw development: detail \"auto\" or between 0 and 4")
    return out


def _tone_table():
    """The curve of the tone "camera" as a table from the 16-bit values of the development to the 8 bits of the
    copy: a logistic curve between black and white, steeper in the middle tones (more contrast there, less in
    the deep shadows and the highlights), the same for every frame."""
    import numpy as np

    k, pivot = _CURVE
    v = np.arange(65536, dtype=np.float64) / 65535.0

    def f(x):
        return 1.0 / (1.0 + np.exp(-k * (x - pivot)))

    return np.clip((f(v) - f(0.0)) / (f(1.0) - f(0.0)) * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _local_contrast(rgb):
    """``rgb`` (8 bit) with its lightness equalised tile by tile under a limit (CLAHE), the colours as they were:
    the detail of the flat areas comes out as the tone map of a phone brings it out. A value is replaced by
    another in its place: nothing moves."""
    import cv2

    clip, tiles = _LOCAL
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    lab[..., 0] = cv2.createCLAHE(clipLimit=clip, tileGridSize=(tiles, tiles)).apply(lab[..., 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


_TIFF_TYPES = {1: ("B", 1), 3: ("H", 2), 4: ("L", 4), 5: ("L", 8), 7: ("B", 1), 8: ("h", 2), 9: ("l", 4),
               10: ("l", 8), 11: ("f", 4), 12: ("d", 8), 13: ("L", 4)}


def _dng_look(path) -> dict | None:
    """The look a DNG carries (Apple ProRAW): ``{"baseline": EV, "curve": (in, out) points or None, "gains": V x H x N
    gains, "spacing": (v, h), "origin": (v, h), "weights": 5 floats}``, from the tags BaselineExposure,
    ProfileToneCurve and ProfileGainTableMap of the main directory and its SubIFDs; ``None`` without a gain table
    map (or for a file that is no TIFF, or one the map does not fit)."""
    import struct

    import numpy as np

    found: dict[int, object] = {}
    try:
        with open(path, "rb") as fp:
            head = fp.read(8)
            if head[:4] not in (b"II*\x00", b"MM\x00*"):
                return None
            bo = "<" if head[:2] == b"II" else ">"

            def walk(offset: int, depth: int) -> None:
                fp.seek(offset)
                (count,) = struct.unpack(bo + "H", fp.read(2))
                entries = [struct.unpack(bo + "HHL4s", fp.read(12)) for _ in range(min(count, 4096))]
                subs: list[int] = []
                for tag, kind, n, value in entries:
                    if tag not in (_BASELINE_EXPOSURE, _TONE_CURVE, _GAIN_TABLE, _SUB_IFDS) or kind not in _TIFF_TYPES:
                        continue
                    code, size = _TIFF_TYPES[kind]
                    total = size * n
                    if total <= 4:
                        data = value[:total]
                    else:
                        fp.seek(struct.unpack(bo + "L", value)[0])
                        data = fp.read(total)
                    if tag == _GAIN_TABLE:
                        found.setdefault(tag, data)
                        continue
                    numbers = struct.unpack(bo + code * (n * (2 if kind in (5, 10) else 1)), data)
                    if kind in (5, 10):
                        numbers = tuple(a / b if b else 0.0 for a, b in zip(numbers[0::2], numbers[1::2]))
                    if tag == _SUB_IFDS:
                        subs = list(numbers)
                    else:
                        found.setdefault(tag, numbers)
                if depth < 2:
                    for sub in subs:
                        walk(int(sub), depth + 1)

            walk(struct.unpack(bo + "L", head[4:8])[0], 0)
    except (OSError, struct.error):
        return None
    blob = found.get(_GAIN_TABLE)
    if not blob:
        return None
    for order in (bo, ">" if bo == "<" else "<"):  # the phones write it big-endian; the file's order is tried first
        try:
            v, h, sv, sh, ov, oh, n = struct.unpack(order + "LL4dL", blob[:44])
            weights = struct.unpack(order + "5f", blob[44:64])
        except struct.error:
            return None
        if v and h and n and 64 + 4 * v * h * n == len(blob):
            gains = np.frombuffer(blob, order + "f4", v * h * n, 64).astype(np.float32).reshape(v, h, n)
            curve = found.get(_TONE_CURVE)
            return {"baseline": float((found.get(_BASELINE_EXPOSURE) or (0.0,))[0]),
                    "curve": np.asarray(curve, np.float64).reshape(-1, 2) if curve and len(curve) >= 4 else None,
                    "gains": gains, "spacing": (sv, sh), "origin": (ov, oh), "weights": weights}
    return None


def _file_tone(rgb16, look: dict, shift: float):
    """The linear development ``rgb16`` (16 bit, linear sRGB) with the look of the DNG ``look`` (:func:`_dng_look`):
    the baseline exposure, the gain of the map for each pixel - interpolated between the nodes around it and
    between the two entries around its input value, the weighted sum of its R, G, B, smallest and largest
    value - then ``shift`` EV, the tone curve and the sRGB curve to 8 bits. A gain per pixel: nothing moves."""
    import numpy as np

    h, w = rgb16.shape[:2]
    gains = look["gains"]
    nv, nh, nn = gains.shape
    (sv, sh), (ov, oh) = look["spacing"], look["origin"]
    wr, wg, wb, wmin, wmax = (np.float32(x) for x in look["weights"])

    def nodes(count: int, size: int, origin: float, spacing: float):
        pos = ((np.arange(size) + 0.5) / size - origin) / (spacing or 1.0)
        first = np.clip(np.floor(pos).astype(np.int64), 0, max(count - 2, 0))
        frac = np.clip(pos - first, 0.0, 1.0).astype(np.float32) if count > 1 else np.zeros(size, np.float32)
        return first, np.minimum(first + 1, count - 1), frac

    x0, x1, fx = nodes(nh, w, oh, sh)
    rows0, rows1, frows = nodes(nv, h, ov, sv)
    v = np.arange(65536) / 65535.0
    if look["curve"] is not None:
        v = np.interp(v, look["curve"][:, 0], look["curve"][:, 1])
    v = np.where(v <= 0.0031308, 12.92 * v, 1.055 * np.clip(v, 0, 1) ** (1 / 2.4) - 0.055)
    table = np.clip(v * 255.0 + 0.5, 0, 255).astype(np.uint8)
    scale = np.float32(2.0 ** look["baseline"] / 65535.0)
    after = np.float32(2.0 ** shift * 65535.0)
    out = np.empty((h, w, 3), np.uint8)
    for top in range(0, h, _STRIP):
        lin = rgb16[top:top + _STRIP].astype(np.float32) * scale
        y0, y1, fy = rows0[top:top + _STRIP, None], rows1[top:top + _STRIP, None], frows[top:top + _STRIP, None]
        value = (lin[..., 0] * wr + lin[..., 1] * wg + lin[..., 2] * wb + lin.min(axis=2) * wmin
                 + lin.max(axis=2) * wmax)
        k = np.clip(value, 0.0, 1.0) * (nn - 1)
        k0 = np.minimum(k.astype(np.int64), max(nn - 2, 0))
        k1 = np.minimum(k0 + 1, nn - 1)
        fk = (k - k0).astype(np.float32)
        gain = np.zeros(value.shape, np.float32)
        for yy, wy in ((y0, 1 - fy), (y1, fy)):
            for xx, wx in ((x0[None, :], 1 - fx[None, :]), (x1[None, :], fx[None, :])):
                gain += (gains[yy, xx, k0] * (1 - fk) + gains[yy, xx, k1] * fk) * (wy * wx)
        lin *= gain[..., None] * after
        out[top:top + _STRIP] = table[np.clip(lin + 0.5, 0, 65535).astype(np.uint16)]
    return out


def _detail(rgb, amount: float):
    """``rgb`` (8 bit) with the band between two blurs (:data:`_DETAIL`) added back ``amount`` times: the detail
    a phone brings out in its JPEG. Symmetric blurs: nothing moves."""
    import cv2
    import numpy as np

    small, large, _phone = _DETAIL
    margin = int(np.ceil(4 * large))
    h = rgb.shape[0]
    out = np.empty_like(rgb)
    for top in range(0, h, _STRIP):
        a, b = max(0, top - margin), min(h, top + _STRIP + margin)
        x = rgb[a:b].astype(np.float32)
        y = x + np.float32(amount) * (cv2.GaussianBlur(x, (0, 0), small) - cv2.GaussianBlur(x, (0, 0), large))
        out[top:top + _STRIP] = np.clip(y[top - a:top - a + _STRIP] + 0.5, 0, 255).astype(np.uint8)
    return out


def _half(rgb):
    """``rgb`` at half the size, each pixel the mean of a 2x2 cell (an odd last row or column dropped): what the
    half size of LibRaw gives a sensor mosaic, one pixel per cell with its centre in the middle of the cell, for a
    raw LibRaw does not halve (a linear DNG has no mosaic)."""
    import numpy as np

    h, w = rgb.shape[0] // 2, rgb.shape[1] // 2
    cells = rgb[: 2 * h, : 2 * w].reshape(h, 2, w, 2, -1).astype(np.uint32).sum(axis=(1, 3))
    return ((cells + 2) // 4).astype(rgb.dtype)


# ------------------------------------------------------------------ pairs
def photo_files(paths: Iterable[str | os.PathLike], extensions, recursive: bool = False,
                prefer: str = "jpeg") -> tuple[list[Path], list[Path]]:
    """The files ``paths`` (files, or folders searched for ``extensions`` and the raw ones) name, one per shot:
    of a raw and an image with the same name in one folder (what a camera set to raw + JPEG writes) only the one
    ``prefer`` says stays. Returns ``(files, left out)``."""
    if prefer not in PREFER:
        raise ValueError(f"prefer must be one of {', '.join(PREFER)}, not {prefer!r}")
    files: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            # hidden files are not photos: macOS leaves an AppleDouble "._name.JPG" beside each photo on exFAT
            files += sorted(f for f in it if (f.suffix.lower() in extensions or is_raw(f)) and not f.name.startswith("."))
        else:
            files.append(p)
    shots: dict[tuple, dict[bool, Path]] = {}
    for f in files:
        shots.setdefault(shot_key(f), {}).setdefault(is_raw(f), f)
    keep_raw = prefer == "raw"
    out, left = [], []
    seen = set()
    for f in files:
        if f in seen:
            continue
        seen.add(f)
        pair = shots[shot_key(f)]
        if len(pair) == 2 and is_raw(f) != keep_raw:
            left.append(f)
        else:
            out.append(f)
    return out, left


def shot_key(path: str | os.PathLike) -> tuple[str, str]:
    """What tells a shot apart: its folder and its name without the extension (case ignored)."""
    p = Path(path)
    return (os.path.normcase(str(p.parent.resolve())), p.stem.lower())


# ------------------------------------------------------------------- EXIF
def read_raw_exif(path: str | os.PathLike) -> dict:
    """The fields of :func:`..exif.read_exif` read from a raw file, with ``orientation`` (the EXIF value) and
    without ``width`` and ``height`` (those are the developed photo's). The raws built on TIFF (DNG, CR2, NEF,
    ARW, PEF...) are read as such; for the others the EXIF of the embedded preview and what LibRaw says."""
    from PIL import Image

    from .exif import exif_fields

    info: dict = {}
    try:
        with open(path, "rb") as fp:
            if fp.read(4) in (b"II*\x00", b"MM\x00*"):
                fp.seek(0)
                exif = Image.Exif()
                exif.load_from_fp(fp)
                info = exif_fields(exif)
    except Exception:
        info = {}
    if (not info.get("make") or not info.get("focal_length_mm")) and available():
        for key, value in _libraw_fields(path).items():
            info.setdefault(key, value)
            if info[key] is None:
                info[key] = value
    return info


def _libraw_fields(path) -> dict:
    """What the embedded preview's EXIF and LibRaw's own reading say of a raw that is no TIFF (RAF, CR3...)."""
    import io

    from PIL import Image

    from .exif import exif_fields

    out: dict = {}
    rawpy = _rawpy()
    try:
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    with Image.open(io.BytesIO(thumb.data)) as im:
                        out = exif_fields(im.getexif())
            except Exception:
                pass
            other, lens = getattr(raw, "other", None), getattr(raw, "lens", None)
            if other is not None:
                if getattr(other, "focal_length", 0):
                    out.setdefault("focal_length_mm", round(float(other.focal_length), 3))
                stamp = getattr(other, "timestamp", None)
                if stamp is not None and not out.get("datetime"):
                    out["datetime"] = stamp.strftime("%Y:%m:%d %H:%M:%S")
            if lens is not None and getattr(lens, "model", "") and not out.get("lens"):
                out["lens"] = lens.model
    except Exception:
        pass
    out.pop("width", None)
    out.pop("height", None)
    return out


def _exif_bytes(info: dict):
    """The EXIF of a developed copy: body, lens, focal lengths, time and GPS of the raw. No orientation: the
    pixels are the sensor's and every reader must see them the same way."""
    from PIL import Image

    exif = Image.Exif()
    if info.get("make"):
        exif[0x010F] = str(info["make"])
    if info.get("model"):
        exif[0x0110] = str(info["model"])
    if info.get("datetime"):
        exif[0x0132] = str(info["datetime"])
    sub = exif.get_ifd(0x8769)
    if info.get("datetime"):
        sub[0x9003] = str(info["datetime"])
    if info.get("focal_length_mm"):
        sub[0x920A] = float(info["focal_length_mm"])
    if info.get("focal_length_35mm"):
        sub[0xA405] = int(round(info["focal_length_35mm"]))
    if info.get("lens"):
        sub[0xA434] = str(info["lens"])
    if info.get("gps"):
        lon, lat, alt = info["gps"]
        gps = exif.get_ifd(0x8825)
        gps[1], gps[2] = ("N" if lat >= 0 else "S"), _dms(lat)
        gps[3], gps[4] = ("E" if lon >= 0 else "W"), _dms(lon)
        gps[5], gps[6] = (b"\x00" if alt >= 0 else b"\x01"), abs(float(alt))
    return exif


def _dms(value: float) -> tuple[float, float, float]:
    seconds = round(abs(value) * 3600, 6)
    d, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return (d, m, s)


# ------------------------------------------------------------ development
def develop_file(source: str | os.PathLike, target: str | os.PathLike, options: dict | None = None,
                 info: dict | None = None) -> tuple[int, int]:
    """Develop the raw ``source`` into the image ``target`` (see the module docstring) and return its
    ``(width, height)``. ``info``: the EXIF fields written into the copy (default: read from the raw). The file
    appears whole or not at all. A ``ValueError`` names a file LibRaw cannot read."""
    from PIL import Image

    rawpy = _rawpy()
    opts = settings(options)
    source, target = Path(source), Path(target)
    gain = 2.0 ** opts["exposure"]
    tone = opts["tone"]
    look = None
    try:
        with rawpy.imread(str(source)) as raw:
            mosaic = raw.raw_type == rawpy.RawType.Flat
            if tone == "file" or (tone == "auto" and not mosaic):  # a phone's raw: its own look, if it has one
                look = _dng_look(source)
                tone = "file" if look is not None else ("camera" if mosaic and tone == "auto" else "local")
            if tone == "auto":  # a mosaic: the curve of a camera JPEG
                tone = "camera"
            if tone == "file" and look is None:  # asked for a raw that carries no look
                tone = "camera" if mosaic else "local"
            curve = tone != "linear"
            if tone == "file":  # linear: the look is applied below, the exposure shift with it
                rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=16, gamma=(1, 1),
                                      user_flip=0, output_color=rawpy.ColorSpace.sRGB, half_size=opts["half_size"])
            else:
                rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=16 if curve else 8,
                                      user_flip=0, output_color=rawpy.ColorSpace.sRGB, half_size=opts["half_size"],
                                      exp_shift=gain, exp_preserve_highlights=1.0 if gain > 1.0 else 0.0)
    except rawpy.LibRawError as exc:
        from ..gui.i18n import tr

        raise ValueError(tr("File raw non leggibile: {0} ({1})", source.name, exc)) from exc
    if opts["half_size"] and not mosaic:  # LibRaw halves a mosaic only: a linear DNG is halved here
        rgb = _half(rgb)
    if tone == "file":  # the shift is relative to the default: the default exposure gives the phone's own brightness
        rgb = _file_tone(rgb, look, opts["exposure"] - DEFAULTS["exposure"])
    elif curve:  # from the 16 bits of the development: no steps in the tones the curve spreads
        rgb = _tone_table()[rgb]
    if tone == "local":
        rgb = _local_contrast(rgb)
    detail = opts["detail"] if opts["detail"] != "auto" else (_DETAIL[2] if tone == "file" else 0.0)
    if detail > 0:
        rgb = _detail(rgb, detail)
    exif = _exif_bytes(read_raw_exif(source) if info is None else info)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    image = Image.fromarray(rgb)
    try:
        if opts["format"] == "jpg":
            image.save(part, "JPEG", quality=opts["quality"], exif=exif)
        else:
            image.save(part, "TIFF", compression="tiff_adobe_deflate", exif=exif)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return int(rgb.shape[1]), int(rgb.shape[0])


def stamp(source: str | os.PathLike) -> dict:
    """What tells whether a raw is still the file that was developed: its size and its time (whole seconds)."""
    st = os.stat(source)
    return {"size": int(st.st_size), "mtime": int(st.st_mtime)}


def _read_index(folder: Path) -> dict:
    try:
        data = json.loads((folder / INDEX).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_index(folder: Path, index: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / (INDEX + ".part")
    part.write_text(json.dumps(index, indent=1), encoding="utf-8")
    os.replace(part, folder / INDEX)


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _name_for(source: Path, suffix: str, index: dict, taken: set[str], folder: Path) -> str:
    """The name of the copy of ``source``: the one it has in the index, else its own name with the extension of
    the copies; with the folder's name, then a number, when another raw has it (or a file the index does not
    know is there: the copies of a duplicated chunk)."""
    for name, entry in index.items():
        if _same(entry.get("source", ""), str(source)) and name.endswith(suffix) and name not in taken:
            return name
    stems = [source.stem, f"{source.parent.name}_{source.stem}"]
    k = 2
    while True:
        for stem in stems:
            name = stem + suffix
            if name not in taken and name not in index and not (folder / name).exists():
                return name
        stems = [f"{source.parent.name}_{source.stem}_{k}"]
        k += 1


def workers_default() -> int:
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def develop(folder: str | os.PathLike, sources: Iterable[str | os.PathLike], options: dict | None = None,
            progress: ProgressCallback | None = None, workers: int | None = None, force: bool = False,
            names: dict | None = None) -> dict[Path, dict]:
    """Develop the raws ``sources`` into ``folder`` (the ``raw`` folder of a chunk), several at a time, and return
    for each ``{"path", "width", "height", "info", "record"}``: the copy, its size, the EXIF fields of the raw and
    what ``camera.meta["raw"]`` keeps (source, size, time, settings). A copy made before from the same file (size
    and time) with the same settings is kept (``force``: made again). ``names``: the file name to give to the
    copy of a source (the one a camera already points to). Cancelling stops between files; the copies finished
    stay and are found again."""
    from PIL import Image

    _rawpy()
    opts = settings(options)
    progress = as_progress(progress)
    folder = Path(folder)
    sources = [Path(s) for s in sources]
    index = {name: entry for name, entry in _read_index(folder).items() if (folder / name).is_file()}
    suffix = "." + opts["format"]
    taken: set[str] = set()
    plan = []
    for src in sources:
        if not src.is_file() or src.stat().st_size == 0:
            raise FileNotFoundError(str(src))
        name = (names or {}).get(src) or _name_for(src, suffix, index, taken, folder)
        taken.add(name)
        plan.append((src, name, {"source": str(src), **stamp(src), "settings": opts}))
    out: dict[Path, dict] = {}

    def one(src: Path, name: str, record: dict) -> dict:
        info = read_raw_exif(src)
        target = folder / name
        # the copy of this name is of this raw (the index or the camera says so): a raw that was only moved, with
        # its size and time, keeps its copy
        entry = {k: v for k, v in (index.get(name) or {}).items() if k != "source"}
        if (not force and entry == {k: v for k, v in record.items() if k != "source"} and target.is_file()
                and target.stat().st_size > 0):
            with Image.open(target) as im:
                width, height = im.size
        else:
            width, height = develop_file(src, target, opts, info)
        return {"path": target, "width": width, "height": height, "info": info,
                "record": {"path": record["source"], "size": record["size"], "mtime": record["mtime"],
                           "settings": dict(opts)}}

    from ..gui.i18n import tr

    done = 0
    pool = ThreadPoolExecutor(max_workers=workers or workers_default(), thread_name_prefix="ppg-raw")
    try:
        progress(0.0, tr("Sviluppo dei file raw: {0}", len(plan)))
        pending = {pool.submit(one, *item): item for item in plan}
        while pending:
            finished, _ = wait(list(pending), timeout=0.2, return_when=FIRST_COMPLETED)
            progress.check()
            for fut in finished:
                src, name, record = pending.pop(fut)
                out[src] = fut.result()
                index[name] = record
                done += 1
                told = done == len(plan) or done % max(1, len(plan) // 10) == 0  # every message goes to the log
                progress(done / len(plan), tr("Sviluppo dei file raw: {0} di {1}", done, len(plan)) if told else "")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        for fut, (_src, name, record) in pending.items():  # cancelled or failed: the files that finished meanwhile
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                index[name] = record
                done += 1
        if done:
            _write_index(folder, index)
    return {src: out[src] for src in sources if src in out}  # in the order asked, not the one they finished in
