"""Multispectral cameras: the bands of every capture, their radiometric calibration, reflectance
orthomosaics, vegetation and thermal indices, zoning (the prescription map of agriculture; in
archaeology the crop marks over buried walls and ditches).

A multispectral camera (MicaSense RedEdge / Altum, DJI Mavic 3 Multispectral, Parrot Sequoia,
...) writes one file per band for every capture, side by side: ``IMG_0001_1.tif`` ...
``IMG_0001_5.tif``, ``DJI_0001_MS_G.TIF`` ... The bands of a capture are separate little
cameras a few centimetres apart: at the distances of a drone the parallax between them is far
below a pixel, so one band (the **reference band**, the red one by default) is aligned and
reconstructed as any photo, and the other bands of each capture are brought onto it with a
homography found on the images themselves (:func:`register_bands`, ECC on gradients: the bands
look different, their edges agree).

Radiometry (:func:`reflectance_image`): the digital numbers become radiance with the camera's
XMP tags when there are any (MicaSense: black level, gain and exposure, the radiometric
calibration ``a1 a2 a3``, the vignetting polynomial; DJI: black level, gain, exposure,
vignetting), else they are normalised by exposure and gain; radiance becomes reflectance through
a calibration panel photographed in the flight (:func:`panel_factors`: the panel's known
reflectance per band divided by its radiance) or, failing that, the irradiance of the sun sensor
(DLS) written in the tags. Without any of these the bands stay normalised numbers: the indices,
ratios of bands, still work, their absolute values do not.

The reflectance orthomosaic (:func:`build_reflectance`) is the orthophoto engine run on the
reference band's geometry with the registered bands as float values: one GeoTIFF, a float32
band per spectral band, NaN where nothing. Indices (:func:`index_raster`, :data:`INDICES`) are
rasters of the same grid; :func:`zoning` cuts an index into classes (quantiles, equal intervals,
natural breaks or k-means) and gives polygons with the class and the mean, exported as GeoPackage
(:func:`export_zoning`), the format farm software and QGIS read.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import cv2
import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress

# ECC (OpenCV's video module) refines the registration of the bands; without it (the Windows ARM64
# wheel up to 1.3.0) the shift of the phase correlation is kept.
ECC = hasattr(cv2, "findTransformECC")
MOTION_AFFINE = getattr(cv2, "MOTION_AFFINE", 2)
MOTION_HOMOGRAPHY = getattr(cv2, "MOTION_HOMOGRAPHY", 3)

# the usual names, in the order of the suffixes of the common cameras
MICASENSE_BANDS = {"1": "Blue", "2": "Green", "3": "Red", "4": "NIR", "5": "RedEdge", "6": "LWIR"}
DJI_BANDS = {"G": "Green", "R": "Red", "RE": "RedEdge", "NIR": "NIR"}
SEQUOIA_BANDS = {"GRE": "Green", "RED": "Red", "REG": "RedEdge", "NIR": "NIR"}
DEFAULT_REFERENCE = ("Visible", "Red", "Green", "NIR", "RedEdge", "Blue")  # a thermal camera's visible photo first
INDICES = {  # name: (formula over band names, description)
    "NDVI": ("(NIR - Red) / (NIR + Red)", "vegetation vigour"),
    "NDRE": ("(NIR - RedEdge) / (NIR + RedEdge)", "chlorophyll, late season"),
    "GNDVI": ("(NIR - Green) / (NIR + Green)", "vegetation, nitrogen"),
    "SAVI": ("1.5 * (NIR - Red) / (NIR + Red + 0.5)", "vegetation over bare soil"),
    "NDWI": ("(Green - NIR) / (Green + NIR)", "water, moisture"),
    "OSAVI": ("1.16 * (NIR - Red) / (NIR + Red + 0.16)", "vegetation, less soil effect"),
    "CIRE": ("NIR / RedEdge - 1", "chlorophyll"),
    "Thermal": ("LWIR", "temperature (the thermal band itself)"),
}
_SUFFIX = re.compile(r"^(?P<stem>.+?)_(?P<band>\d{1,2}|MS_[A-Z]+|GRE|RED|REG|NIR|RGB)$", re.IGNORECASE)
# the DJI enterprise cameras (Mavic 2 Enterprise Advanced, M30T, Mavic 3 Thermal, H20T): a visible
# photo (W wide, V visible) and a thermal frame (T) per capture; Z (zoom) and S (screen) are not bands
_DJI_PAIR = re.compile(r"^(?P<stem>.+)_(?P<kind>[WTVZS])$", re.IGNORECASE)
DJI_PAIR_BANDS = {"W": "Visible", "V": "Visible", "T": "LWIR"}
NON_SPECTRAL = ("Visible", "LWIR")  # not reflectance: the visible photo, the temperatures


# ------------------------------------------------------------------------------ the captures
IMAGE_SUFFIXES = (".tif", ".tiff", ".jpg", ".jpeg", ".png")


def image_files(paths, recursive: bool = False) -> list[Path]:
    """The image files among ``paths`` (files, or folders listed - with their subfolders when
    ``recursive``: the rolls of a camera, 100MEDIA and 101MEDIA)."""
    files: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            found = p.rglob("*") if recursive else p.iterdir()
            files += sorted(f for f in found if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES)
        else:
            files.append(p)
    return files


def detect_bands(paths, recursive: bool = False, thermal: bool = False) -> dict[str, dict[str, Path]]:
    """The captures among ``paths`` (files or folders, their subfolders too when ``recursive``):
    ``{capture: {band name: file}}``. A file is a band of a capture by its suffix (MicaSense
    ``_1``..``_6``, DJI ``_MS_G`` ..., Sequoia ``_GRE`` ...) or by the ``BandName`` of its XMP;
    the RGB JPEG of a DJI capture is left out (it is not a band). A FLIR radiometric JPEG or a
    TIFF of temperatures is a thermal frame of its own; with ``thermal`` every radiometric image
    without a band is one (the TIFFs of integers too, which nothing else tells from the bands of
    a multispectral camera)."""
    return _captures_of(image_files(paths, recursive), thermal=thermal)


def looks_multispectral(paths, recursive: bool = False, sample: int = 60) -> dict | None:
    """Whether ``paths`` hold the captures of a multispectral camera, from the first ``sample``
    image files only (a folder of a thousand ordinary photos is not read through): ``{"bands",
    "captures"}`` (the captures estimated for the whole set), or None."""
    files = image_files(paths, recursive)
    if not files:
        return None
    kinds = [DJI_PAIR_BANDS.get(m.group("kind").upper()) for m in (_DJI_PAIR.match(f.stem) for f in files) if m]
    n_vis, n_th = kinds.count("Visible"), kinds.count("LWIR")
    if n_vis and n_th:  # a thermal camera: visible photos and thermal frames, perhaps in different folders
        return {"bands": ["LWIR", "Visible"], "captures": min(n_vis, n_th), "files": len(files), "thermal": True}
    if n_th:  # the thermal frames alone: radiometric ones only
        from .thermal import kind_of

        probe = [f for f in files if _DJI_PAIR.match(f.stem) and DJI_PAIR_BANDS.get(_DJI_PAIR.match(f.stem).group("kind").upper()) == "LWIR"]
        if kind_of(probe[0]) is not None:
            return {"bands": ["LWIR"], "captures": n_th, "files": len(files), "thermal": True}
    caps = _captures_of(files[:sample])
    if not caps:
        return None
    bands = sorted({b for c in caps.values() for b in c})
    estimate = len(caps) if len(files) <= sample else int(round(len(caps) * len(files) / sample))
    return {"bands": bands, "captures": estimate, "files": len(files), "thermal": "LWIR" in bands}


def _captures_of(files: list[Path], thermal: bool = False) -> dict[str, dict[str, Path]]:
    from .thermal import kind_of

    pairs = [f for f in files if _DJI_PAIR.match(f.stem)]
    captures: dict[str, dict[str, Path]] = _wide_thermal_pairs(pairs) if pairs else {}
    thermal_alone = {k for k, v in captures.items() if set(v) == {"LWIR"}}
    # the radiometric frames that are thermal captures by themselves (not bands of a capture)
    alone_kinds = ("flir", "tiff", "counts", "micasense", "dji") if thermal else ("flir", "tiff")
    files = [f for f in files if not _DJI_PAIR.match(f.stem)]
    owner: dict[str, Path] = {}  # the folder a capture name comes from: the numbering of a camera
    # starts again in every folder (100MEDIA, 101MEDIA), so a repeated name takes its folder as a prefix
    first_stem: dict[str, str] = {}  # the capture of a DJI P4 Multispectral: six files of their own
    # numbers (DJI_0010.JPG, DJI_0011.TIF ... DJI_0015.TIF) sharing a CaptureUUID in the XMP

    def unique(stem: str, folder: Path) -> str:
        if owner.setdefault(stem, folder) != folder:
            stem = f"{folder.name}_{stem}"
            owner.setdefault(stem, folder)
        return stem

    for f in files:
        m = _SUFFIX.match(f.stem)
        band = None
        stem = f.stem
        if m:
            suffix = m.group("band").upper()
            stem = m.group("stem")
            if suffix.startswith("MS_"):
                band = DJI_BANDS.get(suffix[3:])
            elif suffix == "RGB":
                continue
            elif suffix.isdigit():
                # a MicaSense band: its XMP names it (the suffix alone is not enough: _6 is the thermal
                # band of an Altum, the panchromatic one of a RedEdge-P or an Altum-PT, a blue of the
                # second camera of a RedEdge-MX Dual; _7 to _10 have no fixed meaning)
                band = _band_alias(xmp_of(f).get("BandName")) or MICASENSE_BANDS.get(suffix)
            else:
                band = SEQUOIA_BANDS.get(suffix)
        if band is None:
            tags = xmp_of(f)
            band = _band_alias(tags.get("BandName"))
            uuid = tags.get("CaptureUUID")
            if band and uuid:
                stem = first_stem.setdefault(str(uuid), unique(f.stem, f.parent))
            elif band:
                stem = unique(stem, f.parent)
            elif kind_of(f) in alone_kinds:  # a FLIR R-JPEG, a TIFF of temperatures: a thermal frame
                band, stem = "LWIR", unique(f.stem, f.parent)
                thermal_alone.add(stem)
        else:
            stem = unique(stem, f.parent)
        if band is None:
            continue
        captures.setdefault(stem, {})[band] = f
    return {k: v for k, v in captures.items() if len(v) >= 2 or k in thermal_alone}


def _shot(path) -> tuple[float | None, tuple[float, float] | None]:
    """When a photo was taken (seconds, from DateTimeOriginal and its sub-seconds) and where
    (degrees), from its EXIF."""
    from datetime import datetime

    from PIL import ExifTags, Image

    try:
        with Image.open(path) as im:
            ex = im.getexif()
            sub = ex.get_ifd(ExifTags.IFD.Exif)
            gps = ex.get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:  # noqa: BLE001
        return None, None
    t = None
    stamp = sub.get(36867) or ex.get(306)
    if stamp:
        try:
            t = datetime.strptime(str(stamp).strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp()
            frac = str(sub.get(37521) or "").strip()
            if frac.isdigit():
                t += float("0." + frac)
        except ValueError:
            t = None
    where = None
    try:
        if gps and 2 in gps and 4 in gps:
            dms = lambda v: float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600  # noqa: E731
            where = (dms(gps[2]) * (-1 if gps.get(1) == "S" else 1), dms(gps[4]) * (-1 if gps.get(3) == "W" else 1))
    except Exception:  # noqa: BLE001
        where = None
    return t, where


def _wide_thermal_pairs(files: list[Path], max_seconds: float = 1.5, max_metres: float = 5.0) -> dict[str, dict[str, Path]]:
    """The captures of a DJI enterprise camera: the visible photo (``_W``/``_V``) and the thermal
    frame (``_T``) of the same moment, paired by their number when both have it once, else by the
    time and the place of the shot (the camera may number the two files differently, and the two
    kinds may sit in different folders: RGB and IR). Named after the visible photo."""
    wide: dict[str, list[Path]] = {}
    thermal: dict[str, list[Path]] = {}
    for f in files:
        m = _DJI_PAIR.match(f.stem)
        band = DJI_PAIR_BANDS.get(m.group("kind").upper())
        if band == "Visible":
            wide.setdefault(m.group("stem"), []).append(f)
        elif band == "LWIR":
            thermal.setdefault(m.group("stem"), []).append(f)
    out: dict[str, dict[str, Path]] = {}
    used_w, used_t = set(), set()
    for stem in sorted(set(wide) & set(thermal)):
        if len(wide[stem]) == 1 and len(thermal[stem]) == 1:
            out[stem] = {"Visible": wide[stem][0], "LWIR": thermal[stem][0]}
            used_w.add(wide[stem][0])
            used_t.add(thermal[stem][0])
    rest_w = [f for v in wide.values() for f in v if f not in used_w]
    rest_t = [f for v in thermal.values() for f in v if f not in used_t]
    if not wide:  # the thermal frames alone (no visible photos): each its own capture
        for f in rest_t:
            name = _DJI_PAIR.match(f.stem).group("stem")
            if name in out:
                name = f"{f.parent.name}_{name}"
            out[name] = {"LWIR": f}
        return out
    if rest_w and rest_t:
        # one visible photo for one thermal frame, the closest pairs first: the times of the EXIF are
        # whole seconds, so the photo of the next shot (a few metres on) can look as close in time as
        # the right one, and taking the closest in time stole it from the frame it belonged to (13 of
        # 407 captures of a Mavic 2 Enterprise Advanced); the place decides, the time breaks the ties
        shots_w = [(f, *_shot(f)) for f in rest_w]
        shots_w = [s for s in shots_w if s[1] is not None]
        times = np.array([s[1] for s in shots_w]) if shots_w else np.zeros(0)
        candidates = []
        for f in rest_t:
            t, where = _shot(f)
            if t is None or not len(times):
                continue
            for k in np.flatnonzero(np.abs(times - t) <= max_seconds):
                wf, wt, wwhere = shots_w[k]
                metres = max_metres
                if where and wwhere:
                    dy = (where[0] - wwhere[0]) * 111320.0
                    dx = (where[1] - wwhere[1]) * 111320.0 * np.cos(np.radians(where[0]))
                    metres = float(np.hypot(dx, dy))
                    if metres > max_metres:
                        continue
                candidates.append((metres + 0.01 * abs(wt - t), str(f), str(wf), f, wf))
        used_t_here = set()
        for _cost, _a, _b, f, wf in sorted(candidates):
            if f in used_t_here or wf in used_w:
                continue
            name = _DJI_PAIR.match(wf.stem).group("stem")
            if name in out:
                name = f"{wf.parent.name}_{name}"
            out[name] = {"Visible": wf, "LWIR": f}
            used_w.add(wf)
            used_t_here.add(f)
    return out


_EXIF_BASICS = {258: "BitsPerSample", 50714: "BlackLevel", 34855: "ISOSpeedRatings", 33434: "ExposureTime",
                306: "DateTime", 272: "Model", 271: "Make", 305: "Software"}
# the camera's settings in the EXIF directory of a TIFF (a MicaSense band keeps its exposure and its
# ISO there, nowhere else: read from the first directory only, the model took exposure 1 s and gain 1)
_EXIF_SETTINGS = {33434: "ExposureTime", 34867: "ISOSpeed", 34855: "ISOSpeedRatings",
                  41486: "FocalPlaneXResolution", 41487: "FocalPlaneYResolution"}


def xmp_of(path) -> dict:
    """The XMP tags of a photo as ``{name: value}`` (the names without their namespaces) plus the
    few EXIF tags the radiometry needs when the XMP has not got them (the black level of a DJI
    P4 Multispectral band is an EXIF tag of the TIFF), {} when there are none."""
    from PIL import Image

    out: dict = {}
    try:
        with Image.open(path) as im:
            raw = im.info.get("xmp") or b""
            if not raw and hasattr(im, "tag_v2"):  # the XMP tag of a TIFF
                raw = im.tag_v2.get(700) or b""
                if isinstance(raw, (tuple, list)):
                    raw = raw[0] if raw else b""
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            if raw:
                out = parse_xmp(raw)
            elif hasattr(im, "getxmp"):
                import warnings

                try:
                    with warnings.catch_warnings():  # PIL warns when it lacks defusedxml
                        warnings.simplefilter("ignore")
                        data = im.getxmp()
                    out = _flatten(data) if data else {}
                except Exception:  # noqa: BLE001 - needs defusedxml
                    out = {}
            try:
                exif = im.getexif()
                for tag, name in _EXIF_BASICS.items():
                    v = exif.get(tag)
                    if v is not None and name not in out:
                        out[name] = list(v) if isinstance(v, (tuple, list)) else v
                from PIL import ExifTags

                sub = exif.get_ifd(ExifTags.IFD.Exif)
                for tag, name in _EXIF_SETTINGS.items():
                    v = sub.get(tag)
                    if v is not None and name not in out:
                        out[name] = float(v) if not isinstance(v, (tuple, list, str, bytes)) else v
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 - not a photo PIL reads: no tags
        return out
    return out


def _flatten(data, out=None, prefix="") -> dict:
    out = {} if out is None else out
    if isinstance(data, dict):
        for k, v in data.items():
            key = str(k).split(":")[-1]
            if isinstance(v, (dict, list)):
                _flatten(v, out, key)
            else:
                out[key] = v
    elif isinstance(data, list):
        values = [v for v in data if not isinstance(v, (dict, list))]
        if values and prefix:
            out[prefix] = values
        for v in data:
            if isinstance(v, (dict, list)):
                _flatten(v, out, prefix)
    return out


def parse_xmp(text: str) -> dict:
    """The tags of an XMP packet (attributes ``ns:Name="value"`` and elements
    ``<ns:Name>value</ns:Name>``, lists ``<rdf:Seq><rdf:li>v</rdf:li>...``) without namespaces."""
    out: dict = {}
    for m in re.finditer(r'([A-Za-z][\w-]*):([A-Za-z][\w-]*)="([^"]*)"', text):
        if m.group(1) not in ("xmlns", "x", "rdf", "xml"):
            out[m.group(2)] = m.group(3)
    for m in re.finditer(r"<([A-Za-z][\w-]*):([A-Za-z][\w-]*)>(.*?)</\1:\2>", text, re.S):
        if m.group(1) in ("rdf", "x"):
            continue
        body = m.group(3)
        items = re.findall(r"<rdf:li>(.*?)</rdf:li>", body, re.S)
        out[m.group(2)] = [i.strip() for i in items] if items else body.strip()
    return out


_BAND_ALIASES = {"red edge": "RedEdge", "rededge": "RedEdge", "red-edge": "RedEdge", "nir": "NIR", "lwir": "LWIR",
                 "thermal": "LWIR", "blue": "Blue", "green": "Green", "red": "Red", "panchro": "Panchro",
                 "panchromatic": "Panchro"}


def _band_alias(name) -> str | None:
    """The band's name as the program writes it: one word a formula can use. The second camera of
    a MicaSense RedEdge-MX Dual names its bands by colour and wavelength (``Blue-444``, ``Red
    edge-705``): they become ``Blue444``, ``RedEdge705`` (with a hyphen a formula would read a
    subtraction), and the bands of the first camera keep the plain names the indices use."""
    if isinstance(name, list):
        name = name[0] if name else None
    if not name:
        return None
    name = str(name).strip()
    if name.lower() in _BAND_ALIASES:
        return _BAND_ALIASES[name.lower()]
    m = re.fullmatch(r"(?P<base>[A-Za-z][A-Za-z ]*?)[\s_-]*(?P<nm>\d{3,5})(?:\s*nm)?", name)
    if m and m.group("base").strip().lower() in _BAND_ALIASES:
        return _BAND_ALIASES[m.group("base").strip().lower()] + m.group("nm")
    return name


def band_name_of(path) -> str | None:
    return _band_alias(xmp_of(path).get("BandName"))


def _number(v, default=None):
    try:
        if isinstance(v, list):
            v = v[0]
        if isinstance(v, str) and "/" in v:
            a, b = v.split("/", 1)
            return float(a) / float(b)
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def _numbers(v) -> list[float]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        v = [v]
    # a list of one string "a,b,c" too (MicaSense's ReflectArea and VignettingPolynomial2D)
    v = [y for x in v for y in (re.split(r"[,\s]+", x.strip()) if isinstance(x, str) else [x]) if y != ""]
    out = []
    for x in v:
        n = _number(x)
        if n is not None:
            out.append(n)
    return out


def read_band(path) -> np.ndarray:
    """The band as float32 digital numbers (16-bit or 8-bit files)."""
    if str(path).lower().endswith((".tif", ".tiff")):  # PIL: no libtiff warnings on DJI's tags
        from PIL import Image

        with Image.open(path) as im:
            img = np.asarray(im)
    else:
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED | cv2.IMREAD_ANYDEPTH)
    if img is None:
        raise RuntimeError(f"cannot read {path}")
    if img.ndim == 3:
        img = img[..., 0]
    return img.astype(np.float32)


# ------------------------------------------------------------------------------ radiometry
def radiance_image(path, tags: dict | None = None) -> tuple[np.ndarray, dict]:
    """The band in radiance (W/m2/sr/nm, MicaSense's model; a MicaSense thermal band in degrees
    Celsius) or, without calibration tags, in digital numbers normalised by exposure and gain
    (black level taken off, vignetting corrected when known). Returns the image and what was used."""
    tags = xmp_of(path) if tags is None else tags
    dn = read_band(path)
    h, w = dn.shape
    used: dict = {}
    if _micasense_lwir(tags):  # centikelvin: the temperature, not a radiance (RadiometricCalibration 0 0 0)
        used["model"] = "micasense_lwir"
        return (dn / 100.0 - 273.15).astype(np.float32), used
    black = _number(tags.get("BlackLevel"), 0.0)
    if tags.get("BlackLevel") and isinstance(tags["BlackLevel"], list) and len(tags["BlackLevel"]) > 1:
        black = float(np.mean(_numbers(tags["BlackLevel"])))
    bits = _number(tags.get("BitsPerSample"), None)
    if bits is None:
        bits = 16.0 if dn.max() > 255 else 8.0
    dji = tags.get("VignettingData") is not None and tags.get("CalibratedOpticalCenterX") is not None
    cal = _numbers(tags.get("RadiometricCalibration"))
    micasense = not dji and len(cal) >= 3
    # MicaSense's model divides by 2^N (N the bits of the file: 16, or 12 for the RedEdge-P and the
    # Altum-PT), the black level too; DJI's guide by 65535
    norm = (dn - black) / (2.0 ** bits if micasense else 2.0 ** bits - 1.0)
    used["black_level"] = black
    gain = _number(tags.get("SensorGain") or tags.get("Gain"), None)
    if gain is None:  # MicaSense: the ISO / 100 (EXIF ISOSpeed)
        iso = _number(tags.get("ISOSpeed") or tags.get("ISOSpeedRatings") or tags.get("ISO"), None)
        gain = iso / 100.0 if iso else 1.0
    exposure = _number(tags.get("ExposureTime"), None) or _number(tags.get("exposure_time"), 1.0) or 1.0
    if dji and exposure > 1.0:  # DJI P4 Multispectral: microseconds
        exposure = exposure / 1e6
    if micasense and str(tags.get("Model") or "") != "Altum" and abs(exposure - 1.0 / 6329.0) < 1e-6:
        exposure = 0.000274  # the exposure some old RedEdge firmware wrote wrong, as MicaSense corrects it
    used.update(gain=gain, exposure=exposure)
    # vignetting: MicaSense's polynomial in the distance from the centre is the fall-off (the
    # image is divided by it; the newer cameras - RedEdge-P, Altum-PT - give a polynomial in x/width
    # and y/height instead), DJI's (VignettingData k1..k6 from the calibrated optical centre) is the
    # correction (the image is multiplied by it): both as in their guides
    centre = _numbers(tags.get("VignettingCenter"))
    poly = _numbers(tags.get("VignettingPolynomial"))
    multiply = False
    if dji:
        centre = [_number(tags.get("CalibratedOpticalCenterX"), w / 2), _number(tags.get("CalibratedOpticalCenterY"), h / 2)]
        poly = _numbers(tags.get("VignettingData"))
        multiply = True
    fall_off = None
    if len(centre) >= 2 and poly:
        yy, xx = np.mgrid[0:h, 0:w]
        r = np.sqrt((xx - centre[0]) ** 2 + (yy - centre[1]) ** 2)
        v = np.ones_like(r)
        for k, c in enumerate(poly, 1):
            v += c * r ** k
        fall_off = v
    else:
        coef = _numbers(tags.get("VignettingPolynomial2D"))
        powers = _numbers(tags.get("VignettingPolynomial2DName"))
        if coef and len(powers) >= 2 * len(coef):
            x = (np.arange(w, dtype=np.float64) / w)[None, :]
            y = (np.arange(h, dtype=np.float64) / h)[:, None]
            fall_off = np.zeros((h, w))
            for i, c in enumerate(coef):
                fall_off += c * x ** powers[2 * i] * y ** powers[2 * i + 1]
    if fall_off is not None:
        v = np.where(fall_off > 1e-6, fall_off, 1.0).astype(np.float32)
        norm = norm * v if multiply else norm / v
        used["vignetting"] = True
    if dji:  # the guide's normalised camera value, with the gain adjustment of the band
        adjust = _number(tags.get("SensorGainAdjustment"), 1.0) or 1.0
        used["model"] = "dji"
        used["gain_adjustment"] = adjust
        return (norm * adjust / (max(gain, 1e-9) * max(exposure, 1e-9))).astype(np.float32), used
    if micasense:  # L = V * a1 / g * (p - pBL) / (te + a2 * y - a3 * te * y), y the row; never below 0
        a1, a2, a3 = cal[:3]
        rows = np.arange(h, dtype=np.float64)[:, None]
        denom = exposure + a2 * rows - a3 * exposure * rows
        rad = np.maximum(norm, 0.0) * (a1 / max(gain, 1e-9)) / np.where(np.abs(denom) > 1e-12, denom, 1e-12)
        used["model"] = "micasense"
        return rad.astype(np.float32), used
    used["model"] = "normalised"
    return (norm / (max(gain, 1e-9) * max(exposure, 1e-9))).astype(np.float32), used


def _micasense_lwir(tags: dict) -> bool:
    return _band_alias(tags.get("BandName")) == "LWIR" and (
        "micasense" in str(tags.get("Make") or "").lower() or tags.get("ThermalCalibrationParameters") is not None)


def _version(text) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", str(text or ""))[:3])


def _horizontal_irradiance_valid(tags: dict) -> bool:
    """Whether the DLS 2's horizontal irradiance can be taken as written: some firmware of the
    Altum (before 1.2.3) and of the RedEdge(-M/-MX) (before 5.1.7) computed it wrong while the direct
    and scattered parts were right (MicaSense's documentation); the firmware is the EXIF Software."""
    model = str(tags.get("Model") or "")
    firmware = _version(tags.get("Software"))
    good = {"Altum": (1, 2, 3), "RedEdge": (5, 1, 7), "RedEdge-M": (5, 1, 7)}.get(model)
    return good is None or not firmware or firmware >= good


def irradiance_of(tags: dict) -> float | None:
    """The irradiance of the sun sensor (DLS) in the tags, W/m2/nm, when it is there: the
    horizontal irradiance of a DLS 2 (MicaSense writes it in hundredths of W/m2/nm unless
    ``IrradianceScaleToSIUnits`` says otherwise; with a firmware that wrote it wrong, direct x
    sin(sun's elevation) + scattered), else the spectral irradiance the sensor measured (a DLS 1,
    not corrected for the sensor's tilt)."""
    horizontal = _number(tags.get("HorizontalIrradiance"), None)
    scale = _number(tags.get("IrradianceScaleToSIUnits"), None)
    if scale is None:
        scale = 0.01 if horizontal is not None else 1.0
    if horizontal and _horizontal_irradiance_valid(tags):
        return horizontal * scale
    direct, scattered = _number(tags.get("DirectIrradiance"), None), _number(tags.get("ScatteredIrradiance"), None)
    elevation = _number(tags.get("SolarElevation"), None)  # radians
    if direct and scattered is not None and elevation is not None:
        return (direct * np.sin(elevation) + scattered) * scale
    for key in ("Irradiance", "SpectralIrradiance"):
        v = _number(tags.get(key), None)
        if v:
            return v * scale
    return None


def panel_in_tags(tags: dict) -> tuple[list[tuple[float, float]], float | None] | None:
    """The calibration panel a MicaSense camera found by itself in a photo (``CalibrationPicture``
    2, the panel's QR code read): the corners of its reflective area in the image (``ReflectArea``,
    x y pairs) and its reflectance in this band (``Albedo``, from the QR code), or None."""
    corners = _numbers(tags.get("ReflectArea"))
    if len(corners) < 6 or len(corners) % 2:
        return None
    return [(corners[k], corners[k + 1]) for k in range(0, len(corners), 2)], _number(tags.get("Albedo"), None)


def _region_mask(region, shape) -> np.ndarray:
    """The pixels of a panel ``region``: a rectangle (x0, y0, x1, y1) or a polygon [(x, y), ...]."""
    mask = np.zeros(shape, bool)
    pts = np.asarray(region, float)
    if pts.ndim == 1 and pts.size == 4:
        x0, y0, x1, y1 = (int(v) for v in pts)
        mask[max(y0, 0):max(y1, 0), max(x0, 0):max(x1, 0)] = True
        return mask
    poly = np.zeros(shape, np.uint8)
    cv2.fillPoly(poly, [np.round(pts.reshape(-1, 2)).astype(np.int32)], 1)
    return poly.astype(bool)


def panel_factors(captures: dict[str, dict[str, Path]], capture: str, region=None,
                  reflectance: dict[str, float] | float | None = None, found: dict | None = None) -> dict:
    """The factor reflectance / radiance of every band from the photo of a calibration panel
    (the capture that shows it, the ``region`` of the panel in its images - x0, y0, x1, y1 or the
    corners of a polygon - and the known reflectance of the panel per band or one for all).
    Without a ``region`` or a ``reflectance``, what the camera wrote (:func:`panel_in_tags`: a
    MicaSense camera finds its panel and reads its reflectances from the QR code); a band whose
    tags have no panel takes the corners of a band that has (moved onto it with the registration
    of the bands) and the mean reflectance of the others. ``found``, when given, collects where
    the region and the reflectance of every band came from."""
    bands = captures[capture]
    tags = {b: xmp_of(p) for b, p in bands.items()}
    in_tags = {b: panel_in_tags(t) for b, t in tags.items() if b not in NON_SPECTRAL and not _micasense_lwir(t)}
    rads = {b: radiance_image(bands[b], tags[b])[0] for b in in_tags}
    regions: dict = {}
    if region is not None:
        regions = {b: region for b in rads}
    else:
        regions = {b: t[0] for b, t in in_tags.items() if t}
        missing = [b for b in rads if b not in regions]
        if missing and regions:  # the corners of a band with a panel, carried onto the others
            source = next(iter(regions))
            images = {source: rads[source], **{b: rads[b] for b in missing}}
            hs = register_bands(images, source, cameras=band_cameras({b: tags[b] for b in images}))
            for b in missing:  # the homography maps a pixel of the source band onto the band's own grid
                pts = np.asarray(regions[source], np.float64)
                xy = cv2.perspectiveTransform(pts[None], np.asarray(hs[b], np.float64))[0]
                regions[b] = [tuple(float(v) for v in p) for p in xy]
    albedo = {b: t[1] for b, t in in_tags.items() if t and t[1]}
    out = {}
    for band, rad in rads.items():
        if band not in regions:
            continue
        if reflectance is None:
            rho = albedo.get(band, float(np.mean(list(albedo.values()))) if albedo else None)
        else:
            rho = reflectance.get(band) if isinstance(reflectance, dict) else reflectance
        if rho is None:
            continue
        mask = _region_mask(regions[band], rad.shape)
        if not mask.any():
            continue
        mean = float(np.mean(rad[mask]))
        if mean > 0:
            out[band] = float(rho) / mean
            if found is not None:
                found[band] = {"region": np.asarray(regions[band], float).round(1).tolist(), "reflectance": float(rho),
                               "radiance": mean,
                               "from": "given" if region is not None else ("tags" if in_tags.get(band) else "carried")}
    return out


def reflectance_image(path, factor: float | None = None, tags: dict | None = None) -> np.ndarray:
    """The band in reflectance: radiance x the panel ``factor`` of its band, or pi x radiance /
    the irradiance of the sun sensor when the tags have it, else the normalised numbers."""
    tags = xmp_of(path) if tags is None else tags
    rad, used = radiance_image(path, tags)
    if factor is not None:
        return (rad * factor).astype(np.float32)
    irr = irradiance_of(tags)
    if irr:
        if used.get("model") == "dji":  # the sun sensor's value is normalised like the camera's: a ratio
            return (rad / irr).astype(np.float32)
        return (np.pi * rad / irr).astype(np.float32)
    return rad


# ------------------------------------------------------------------------------ registration
BAND_ORDER = ("Blue", "Green", "Red", "RedEdge", "NIR", "LWIR")  # by wavelength: the neighbours look alike
# the centre of each band in nanometres (MicaSense's, near enough for the others); a band named with
# its wavelength (Blue444, RedEdge705) has it in the name; the panchromatic band covers the visible
WAVELENGTHS = {"Blue": 475, "Green": 560, "Red": 668, "RedEdge": 717, "NIR": 842, "Panchro": 634, "LWIR": 11000}


def _wavelength_index(band: str) -> float:
    m = re.fullmatch(r"[A-Za-z]+?(\d{3,5})", band)
    if m:
        return float(m.group(1))
    return float(WAVELENGTHS.get(band, 1e5))


def _rig_rotation(degrees) -> np.ndarray:
    """The rotation of a band's camera in the rig from MicaSense's ``RigRelatives`` (degrees about
    x, then y, then z, composed Rx Ry Rz)."""
    ax, ay, az = np.radians([float(v) for v in degrees[:3]])
    rx = np.array([[1, 0, 0], [0, np.cos(ax), -np.sin(ax)], [0, np.sin(ax), np.cos(ax)]])
    ry = np.array([[np.cos(ay), 0, np.sin(ay)], [0, 1, 0], [-np.sin(ay), 0, np.cos(ay)]])
    rz = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
    return rx @ ry @ rz


def band_cameras(tags: dict[str, dict], shapes: dict[str, tuple] | None = None) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """The camera of every band whose tags describe it (MicaSense: focal length and principal point
    in millimetres, ``FocalPlaneXResolution`` pixels per millimetre, the rotation in the rig
    ``RigRelatives``): ``{band: (K, R)}``. The bands of a MicaSense camera are separate cameras
    with their own lens - the panchromatic band of a RedEdge-P or an Altum-PT and the thermal one
    of an Altum see another field at another scale, a few degrees apart - and :func:`register_bands`
    starts from what these say. ``shapes`` (rows, columns) check the principal point."""
    out = {}
    for band, t in tags.items():
        f = _number(t.get("PerspectiveFocalLength"), None)
        res = _number(t.get("FocalPlaneXResolution"), None)
        res_y = _number(t.get("FocalPlaneYResolution"), res)
        pp = _numbers(t.get("PrincipalPoint"))
        rig = _numbers(t.get("RigRelatives"))
        if not f or not res or len(pp) < 2:
            continue
        if str(t.get("PerspectiveFocalLengthUnits") or "mm").lower() not in ("mm", ""):
            fx = fy = f  # already in pixels
        else:
            fx, fy = f * res, f * res_y
        cx, cy = pp[0] * res, pp[1] * res_y
        if shapes and band in shapes:
            h, w = shapes[band][:2]
            if not (0 < cx < w and 0 < cy < h):
                continue
        k = np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]])
        out[band] = (k, _rig_rotation(rig) if len(rig) >= 3 else np.eye(3))
    return out


def _camera_homography(cameras: dict, reference: str, band: str) -> np.ndarray | None:
    """Reference pixel -> band pixel for a scene far away (rotation only, no lens distortion)."""
    if not cameras or reference not in cameras or band not in cameras:
        return None
    (kr, rr), (kb, rb) = cameras[reference], cameras[band]
    h = kb @ rb.T @ rr @ np.linalg.inv(kr)
    return h / h[2, 2]


def register_bands(images: dict[str, np.ndarray], reference: str, scale: int = 2, iterations: int = 100,
                   quality: dict | None = None, cameras: dict | None = None) -> dict[str, np.ndarray]:
    """The homography of every band onto the ``reference`` band (ECC on the gradient magnitudes,
    at a reduced size then scaled back): ``{band: 3 x 3}`` mapping a pixel of the reference to
    the same point in the band (what :func:`warp_band` needs), the identity for the reference
    and for a band ECC cannot register. A band of another size (the panchromatic band of a
    RedEdge-P, the thermal one of an Altum) is matched at the reference's size and its homography
    given onto its own pixels. The bands are registered in a chain by wavelength: a
    band is matched against the two registered bands nearest to it in wavelength, already brought
    into the frame of the reference, and the better match wins - a leaf is bright and textured in
    the NIR, dark and smooth in the red, so NIR on red seldom converges (correlation 0.1-0.3 on a
    DJI P4 Multispectral over scrub), NIR on red-edge always does (0.9). ``quality``, when given,
    collects the ECC correlation of every band and the band it was matched to. ``cameras``
    (:func:`band_cameras`) give where to start: the band brought onto the reference by the
    cameras' geometry first, ECC from there; a band ECC cannot match (a thermal band: its edges are
    not those of the others) keeps the cameras' homography when ECC would move it far from it."""
    ref = images[reference]
    out = {}
    h0, w0 = ref.shape[:2]

    def grad(img, s: int):
        """The gradient magnitude at 1/s, normalised: bands that look different (a leaf bright
        in the NIR, dark in the red) keep the same edges."""
        small = cv2.resize(img, (max(8, img.shape[1] // s), max(8, img.shape[0] // s)), interpolation=cv2.INTER_AREA) if s > 1 else img
        g = cv2.GaussianBlur(np.nan_to_num(small.astype(np.float32)), (0, 0), 1.0)
        gx = cv2.Scharr(g, cv2.CV_32F, 1, 0)
        gy = cv2.Scharr(g, cv2.CV_32F, 0, 1)
        mag = np.sqrt(gx * gx + gy * gy)
        return ((mag - mag.mean()) / (mag.std() + 1e-9)).astype(np.float32)

    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, int(iterations), 1e-4)
    window = cv2.createHanningWindow((w0, h0), cv2.CV_32F)
    # the registered bands in the frame of the reference: their gradient features at full and
    # at the reduced size, the targets of the bands still to register
    targets: dict[str, tuple[np.ndarray, np.ndarray]] = {reference: (grad(ref, 1), grad(ref, scale) if scale > 1 else grad(ref, 1))}
    out[reference] = np.eye(3)

    def register(target: tuple[np.ndarray, np.ndarray], pimg_full: np.ndarray, pimg_half: np.ndarray):
        """ECC of a band on a target: the shift first (phase correlation, whole image), then an
        affine at the reduced size, then the homography at full size, each starting where the
        previous ended. Returns the homography and the correlation reached (0 when ECC failed)."""
        tfull, thalf = target
        (dx, dy), response = cv2.phaseCorrelate(tfull, pimg_full, window)
        h = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy], [0.0, 0.0, 1.0]])
        cc = 0.0
        if not ECC:  # an OpenCV without the video module: the shift alone
            return h, float(response)
        try:
            if scale > 1:
                s = np.diag([1.0 / scale, 1.0 / scale, 1.0])
                warp = (s @ h @ np.linalg.inv(s))[:2].astype(np.float32)
                cc, warp = cv2.findTransformECC(thalf, pimg_half, warp, MOTION_AFFINE, criteria, None, 5)
                h = np.linalg.inv(s) @ np.vstack([warp.astype(np.float64), [0.0, 0.0, 1.0]]) @ s
            cc, warp3 = cv2.findTransformECC(tfull, pimg_full, h.astype(np.float32), MOTION_HOMOGRAPHY, criteria, None, 5)
            h = warp3.astype(np.float64)
        except cv2.error:
            pass  # the shift alone, or what the affine gave
        return h, float(cc)

    pending = sorted((b for b in images if b != reference),
                     key=lambda b: (abs(_wavelength_index(b) - _wavelength_index(reference)), b))
    for band in pending:
        img = images[band]
        start = _camera_homography(cameras, reference, band)
        size = np.eye(3)  # from the reference's frame to the band's own pixels
        if start is not None:
            # the band seen through the cameras' geometry, reduced first when it is much finer (the
            # panchromatic band: the warp alone would alias it); replicated edges, not NaN: an edge of
            # zeros would be the strongest gradient of the image
            src = np.nan_to_num(img.astype(np.float32))
            k = min(img.shape[0] / h0, img.shape[1] / w0)
            if k > 1.3:
                src = cv2.resize(src, (int(round(img.shape[1] / k)), int(round(img.shape[0] / k))), interpolation=cv2.INTER_AREA)
                down = np.diag([src.shape[1] / img.shape[1], src.shape[0] / img.shape[0], 1.0])
            else:
                down = np.eye(3)
            size = start
            img = cv2.warpPerspective(src, down @ start, (w0, h0), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                      borderMode=cv2.BORDER_REPLICATE)
        elif img.shape[:2] != (h0, w0):
            size = np.diag([img.shape[1] / w0, img.shape[0] / h0, 1.0])
            small = img.shape[0] < h0
            img = cv2.resize(np.nan_to_num(img.astype(np.float32)), (w0, h0),
                             interpolation=cv2.INTER_LINEAR if small else cv2.INTER_AREA)
        pimg_full = grad(img, 1)
        pimg_half = grad(img, scale) if scale > 1 else pimg_full
        nearest = sorted(targets, key=lambda t: (abs(_wavelength_index(t) - _wavelength_index(band)), t != reference))[:2]
        best = None
        for t in nearest:
            h, cc = register(targets[t], pimg_full, pimg_half)
            if best is None or cc > best[1]:
                best = (h, cc, t)
            if cc >= 0.6:  # good enough: the second candidate is for the bands that look unlike
                break
        h, cc, matched = best
        if start is not None and cc < 0.3:
            # a weak match: kept only when it stays near the cameras' geometry (2 % of the diagonal)
            corners = np.array([[0, 0], [w0, 0], [0, h0], [w0, h0]], np.float64)[None]
            moved = np.linalg.norm(cv2.perspectiveTransform(corners, h)[0] - corners[0], axis=1).max()
            if moved > 0.02 * np.hypot(w0, h0):
                h, matched = np.eye(3), "cameras"
        out[band] = size @ h
        if quality is not None:
            quality[band] = {"correlation": cc, "on": matched}
        if cc > 0.15:  # registered: a target for the bands further away in wavelength
            warped = warp_band(img, h, (h0, w0))
            targets[band] = (grad(warped, 1), grad(warped, scale) if scale > 1 else grad(warped, 1))
    return out


def warp_band(img: np.ndarray, h: np.ndarray, shape) -> np.ndarray:
    """``img`` (a band) brought onto the reference band's grid of ``shape`` with ``h``, the
    homography of :func:`register_bands` (reference pixel -> band pixel); NaN outside."""
    if np.allclose(h, np.eye(3)) and img.shape[:2] == tuple(shape):
        return img.astype(np.float32)
    out = cv2.warpPerspective(img.astype(np.float32), np.asarray(h, np.float64), (int(shape[1]), int(shape[0])),
                              flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_CONSTANT,
                              borderValue=float("nan"))
    return out


# ------------------------------------------------------------------------------ the chunk
def add_captures(chunk: Chunk, paths, reference: str | None = None, recursive: bool = False,
                 thermal: bool = False) -> list:
    """The captures of a multispectral camera into the chunk (``thermal``: of a thermal camera -
    every radiometric image without a band is a thermal frame, see :func:`detect_bands`): the reference band of every
    capture becomes its photo (aligned and reconstructed as any photo), the other bands are
    remembered in ``camera.meta["bands"]``. Returns the cameras added. Called again with another
    folder of the same flight (the rolls of a camera, whose numbering starts again), it adds
    those captures to the ones there - a name already taken gets its folder as a prefix - keeps
    the reference band chosen the first time, and leaves out files already in the chunk."""
    captures = detect_bands(paths, recursive=recursive, thermal=thermal)
    if not captures:
        raise RuntimeError("no thermal frames found (DJI R-JPEGs, FLIR radiometric JPEGs, TIFFs of temperatures or "
                           "of integers)" if thermal else
                           "no multispectral captures found (files <capture>_<band>.tif side by side)")
    ms = chunk.meta.get("multispectral") or {}
    existing = multispectral_cameras(chunk)
    known = {Path(p).resolve() for c in existing for p in (c.meta.get("bands") or {}).values()}
    captures = {s: f for s, f in captures.items() if not any(Path(p).resolve() in known for p in f.values())}
    bands = sorted({b for c in captures.values() for b in c} | set(ms.get("bands") or []))
    if existing and ms.get("reference"):
        reference = ms["reference"]
    else:
        reference = reference or next((b for b in DEFAULT_REFERENCE + ("LWIR",) if b in bands), bands[0])
    taken = {c.meta.get("capture") for c in existing}
    usable: dict[str, dict[str, Path]] = {}
    for stem, files in captures.items():
        if reference not in files:
            continue
        name = stem
        if name in taken:
            name = f"{files[reference].parent.name}_{stem}"
        k = 2
        while name in taken:
            name = f"{files[reference].parent.name}_{stem}_{k}"
            k += 1
        taken.add(name)
        usable[name] = files
    # the photos of the chunk: 8-bit copies of the reference band, stretched (12 or 16 bits with
    # a dark scene would come out almost black for the feature detector), with the tags of the
    # original (GPS, camera); the values come from the original files
    folder = chunk.data_dir / "multispectral" / "reference"
    folder.mkdir(parents=True, exist_ok=True)
    copies = {}
    stretch = None
    if reference == "LWIR":  # the thermal frames alone: one stretch for all their 8-bit copies
        from .thermal import stretch_of

        stretch = ms.get("thermal_stretch") or list(stretch_of([f["LWIR"] for f in usable.values()]))
        ms["thermal_stretch"] = stretch
    for stem, files in usable.items():
        if reference == "Visible":  # a thermal camera's visible photo: the photo itself, colours and all
            copies[stem] = Path(files[reference])
        elif reference == "LWIR":
            from .thermal import reference_copy as thermal_copy

            copies[stem] = thermal_copy(files[reference], folder / f"{stem}.jpg", tuple(stretch))
        else:
            copies[stem] = reference_copy(files[reference], folder / f"{stem}.jpg")
    cams = chunk.add_photos([copies[s] for s in usable]) if usable else []
    by_path = {Path(c.path).resolve(): c for c in cams}
    for stem, files in usable.items():
        cam = by_path.get(copies[stem].resolve())
        if cam is None:
            continue
        cam.meta["bands"] = {b: str(p) for b, p in files.items()}
        cam.meta["capture"] = stem
    ms.update({"reference": reference, "bands": bands, "captures": len(existing) + len(cams)})
    ms.setdefault("panel", None)
    ms.setdefault("registration", {})
    chunk.meta["multispectral"] = ms
    return cams


def reference_copy(src: Path, dst: Path) -> Path:
    """An 8-bit JPEG of a band for the geometry (1st-99th percentile stretch), with the EXIF
    of the original when it has one."""
    from PIL import Image

    if dst.exists() and dst.stat().st_mtime >= Path(src).stat().st_mtime:
        return dst
    dn = read_band(src)
    lo, hi = np.percentile(dn, (1.0, 99.0))
    img = np.clip((dn - lo) / max(hi - lo, 1e-6) * 255.0, 0, 255).astype(np.uint8)
    exif = b""
    try:
        with Image.open(src) as im:
            exif = im.info.get("exif") or b""
            if not exif:  # a TIFF: its EXIF rebuilt with the camera and GPS directories
                from PIL import ExifTags

                ex = im.getexif()
                ex.get_ifd(ExifTags.IFD.Exif)
                ex.get_ifd(ExifTags.IFD.GPSInfo)
                exif = ex.tobytes() if len(ex) else b""
    except Exception:  # noqa: BLE001
        exif = b""
    pil = Image.fromarray(img)
    if exif:
        pil.save(dst, quality=97, exif=exif)
    else:
        pil.save(dst, quality=97)
    return dst


def multispectral_cameras(chunk: Chunk) -> list:
    return [c for c in chunk.cameras if c.meta.get("bands")]


def calibrate_panel(chunk: Chunk, camera, region=None, reflectance: dict[str, float] | float | None = None) -> dict:
    """The panel factors from the capture of ``camera`` (its bands), kept in the chunk. Without a
    ``region`` or a ``reflectance``, the panel the camera found and the reflectances it read
    (MicaSense: :func:`panel_in_tags`)."""
    captures = {camera.meta.get("capture", "panel"): {b: Path(p) for b, p in camera.meta["bands"].items()}}
    found: dict = {}
    factors = panel_factors(captures, next(iter(captures)), region, reflectance, found=found)
    if not factors:
        raise ValueError("no panel: give its region in the images and its reflectance (the tags of these "
                         "photos do not say where it is)")
    ms = chunk.meta.setdefault("multispectral", {})
    if reflectance is None:
        reflectance = {b: v["reflectance"] for b, v in found.items()}
    ms["panel"] = {"camera": camera.key, "region": None if region is None else np.asarray(region, float).tolist(),
                   "factors": factors, "reflectance": reflectance,
                   "regions": {b: v["region"] for b, v in found.items()},
                   "source": "tags" if region is None else "given"}
    return factors


def panel_captures(chunk: Chunk) -> list:
    """The cameras of the chunk whose bands show a panel the camera found by itself (MicaSense)."""
    out = []
    for cam in multispectral_cameras(chunk):
        if any(panel_in_tags(xmp_of(p)) for b, p in cam.meta["bands"].items() if b not in NON_SPECTRAL):
            out.append(cam)
    return out


def band_stack(chunk: Chunk, camera, bands: list[str], cache: dict | None = None,
               gains: dict[str, dict[str, float]] | None = None) -> np.ndarray:
    """The bands of the capture of ``camera`` in reflectance, registered onto the reference
    band: ``(h, w, len(bands))`` float32, NaN where a band does not cover. ``gains`` (per band,
    per camera key: :func:`balance_gains`) divide the values."""
    ms = chunk.meta.get("multispectral") or {}
    reference = ms.get("reference")
    files = camera.meta.get("bands") or {}
    factors = (ms.get("panel") or {}).get("factors") or {}
    files = {b: p for b, p in files.items() if b != "Visible" or b == reference}
    tags = {b: xmp_of(p) for b, p in files.items()}
    refl = {}
    for b, p in files.items():
        if b == "LWIR":  # a thermal band: degrees Celsius, not reflectance
            from .thermal import decode_file

            refl[b] = decode_file(p)
        else:
            refl[b] = reflectance_image(p, factors.get(b), tags[b])
    hs = ms.get("registration", {}).get(str(camera.key))
    if hs is None:
        quality: dict = {}
        cameras = band_cameras(tags, {b: v.shape for b, v in refl.items()})
        hs = (register_bands(refl, reference, quality=quality, cameras=cameras) if reference in refl
              else {b: np.eye(3) for b in refl})
        ms.setdefault("registration", {})[str(camera.key)] = {b: np.asarray(h).tolist() for b, h in hs.items()}
        ms.setdefault("registration_quality", {})[str(camera.key)] = quality
    shape = refl[reference].shape if reference in refl else next(iter(refl.values())).shape
    stack = np.full((shape[0], shape[1], len(bands)), np.nan, np.float32)
    for k, b in enumerate(bands):
        if b in refl:
            stack[..., k] = warp_band(refl[b], np.asarray(hs[b], float), shape)
            field = _balance_field(gains, b, str(camera.key), shape)
            if field is not None:
                stack[..., k] /= field
    return stack


def _balance_field(gains: dict | None, band: str, key: str, shape) -> np.ndarray | float | None:
    """What :func:`balance_gains` found for the photo ``key`` in ``band``, as what every pixel of the
    frame (``shape``) is divided by: its gain times exp(plane + radial term), or the gain alone (a
    project balanced before the planes); None when nothing is known."""
    if not gains:
        return None
    entry = (gains.get(band) or {}).get(key)
    radial = float((gains.get("radial") or {}).get(band) or 0.0)
    if entry is None and not radial:
        return None
    box = (-0.5, 0.5, -0.5, 0.5)
    if entry is None:
        g, a, b = 1.0, 0.0, 0.0
    elif isinstance(entry, (int, float)):
        g, a, b = float(entry), 0.0, 0.0
    else:
        values = [float(x) for x in entry]
        g, a, b = values[:3]
        if len(values) >= 7:
            box = tuple(values[3:7])
    if not a and not b and not radial:
        return g
    h, w = shape[:2]
    u = ((np.arange(w, dtype=np.float32) + 0.5) / w - 0.5)[None, :]
    v = ((np.arange(h, dtype=np.float32) + 0.5) / h - 0.5)[:, None]
    # the plane holds where the photo's points were, beyond them it stays at its edge: a photo at the
    # edge of the block sees the surface on one side of its frame only
    plane = a * np.clip(u, box[0], box[1]) + b * np.clip(v, box[2], box[3])
    return (g * np.exp(plane + radial * (u * u + v * v - 1.0 / 6.0))).astype(np.float32)


def _photo_ranges(photo, u, v, n_photos: int, low: float = 2.0, high: float = 98.0) -> np.ndarray:
    """Per photo, the part of its frame its readings cover: the ``low`` and ``high`` percentiles of
    ``u`` and of ``v`` (``(n_photos, 4)``: u from, u to, v from, v to; the whole frame without
    readings)."""
    out = np.tile(np.array([-0.5, 0.5, -0.5, 0.5]), (n_photos, 1))
    if not len(photo):
        return out
    order = np.argsort(photo, kind="stable")
    ph, uu, vv = photo[order], u[order], v[order]
    cut = np.flatnonzero(np.diff(ph)) + 1
    for idx, us, vs in zip(np.split(ph, cut), np.split(uu, cut), np.split(vv, cut)):
        out[idx[0]] = [*np.percentile(us, [low, high]), *np.percentile(vs, [low, high])]
    return out


BALANCE_RIDGE = 0.001  # the planes drawn to zero where a photo's points cover a sliver of its frame only
BALANCE_POINTS_PER_PHOTO = 80  # surface points read, per photo of the chunk (at least 4000, at most 100000)
BALANCE_POINTS_MAX = 100_000


def _balance_planes(photo, u, v, r, n_photos: int, ridge: float = BALANCE_RIDGE, weights=None) -> np.ndarray:
    """Weighted least squares ``r ~ g + a u + b v`` for every photo (its normal equations from
    bincounts), the slopes drawn to zero by ``ridge`` times the weight of the readings; a photo
    whose readings weigh less than 10 gets their weighted mean only. ``(n_photos, 3)``."""
    w8 = np.ones_like(u) if weights is None else weights

    def total(x):
        return np.bincount(photo, w8 * x, minlength=n_photos)

    cols = (np.ones_like(u), u, v)
    m = np.empty((n_photos, 3, 3))
    for i in range(3):
        for j in range(i, 3):
            m[:, i, j] = m[:, j, i] = total(cols[i] * cols[j])
    n = m[:, 0, 0].copy()
    m[:, 1, 1] += ridge * n
    m[:, 2, 2] += ridge * n
    rhs = np.stack([total(r), total(r * u), total(r * v)], axis=1)
    out = np.zeros((n_photos, 3))
    full = n >= 10
    if full.any():
        out[full] = np.linalg.solve(m[full], rhs[full][..., None])[..., 0]
    few = (n > 0) & ~full
    out[few, 0] = rhs[few, 0] / n[few]
    return out


TUKEY = 4.685  # the biweight's constant, in MADs of the residuals


def _fit_balance(point, photo, logv, u, v, n_points: int, n_photos: int, rounds: int = 40,
                 planes: bool = True) -> tuple[np.ndarray, float, np.ndarray, float]:
    """The log readings split into a value of the point, and per photo a gain and (``planes``) a
    plane across its frame (``u``, ``v`` from -0.5 to 0.5), plus a radial term common to the photos
    (u2 + v2 - 1/6, of mean zero over the frame): least squares by alternation, the gains of mean
    zero, every reading weighed by Tukey's biweight of its residual once the photos are corrected
    (recomputed every round from the third): readings that no correction of their photo explains
    - the shadow of a tree that moved between two flights, a saturated field, vegetation in the
    wind - weigh little or nothing, instead of tilting the plane of the photo to explain them.
    Returns the (gain, slope u, slope v) of every photo in log, the radial term, the readings that
    count per photo and the MAD of what is left."""
    q = u * u + v * v - 1.0 / 6.0
    coef = np.zeros((n_photos, 3))
    radial = 0.0
    w = np.ones(len(logv))
    for it in range(rounds):
        corr = coef[photo, 0] + coef[photo, 1] * u + coef[photo, 2] * v + radial * q
        sw = np.bincount(point, w, minlength=n_points)
        value = np.bincount(point, w * (logv - corr), minlength=n_points) / np.maximum(sw, 1e-12)
        res = logv - value[point]  # what the photo and the place in its frame add
        if it >= 2:
            rest = res - corr
            mid = np.median(rest[w > 0])
            mad = 1.4826 * np.median(np.abs(rest[w > 0] - mid)) + 1e-9
            t = (rest - mid) / (TUKEY * mad)
            w = np.where(np.abs(t) < 1.0, (1.0 - t * t) ** 2, 0.0)
        if planes:
            coef = _balance_planes(photo, u, v, res - radial * q, n_photos, weights=w)
            rest = res - coef[photo, 0] - coef[photo, 1] * u - coef[photo, 2] * v
            sw_all = max(float(w.sum()), 1e-12)
            qk = q - float(np.sum(w * q)) / sw_all
            rk = rest - float(np.sum(w * rest)) / sw_all
            radial = float(np.sum(w * qk * rk) / max(float(np.sum(w * qk * qk)), 1e-12))
        else:
            weight = np.bincount(photo, w, minlength=n_photos)
            coef[:, 0] = np.bincount(photo, w * res, minlength=n_photos) / np.maximum(weight, 1e-12)
        counted = np.bincount(photo, (w > 0).astype(float), minlength=n_photos)
        if (counted > 0).any():
            coef[:, 0] -= coef[counted > 0, 0].mean()
    corr = coef[photo, 0] + coef[photo, 1] * u + coef[photo, 2] * v + radial * q
    sw = np.bincount(point, w, minlength=n_points)
    value = np.bincount(point, w * (logv - corr), minlength=n_points) / np.maximum(sw, 1e-12)
    rest = (logv - corr - value[point])[w > 0]
    residual = float(1.4826 * np.median(np.abs(rest - np.median(rest)))) if len(rest) else 0.0
    n_ph = np.bincount(photo, (w > 0).astype(float), minlength=n_photos)
    return coef, radial, n_ph, residual


def balance_gains(chunk: Chunk, bands: list[str] | None = None, samples: int | None = None, rounds: int = 40,
                  progress: Progress | None = None, planes: bool = True) -> dict:
    """Per band and photo, what brings the captures to the same reflectance where they overlap:
    points of the surface (``samples``, by default 80 per photo) are projected into every capture
    and read in its registered bands; the log readings are split into a value of the point and,
    per photo, a gain and a plane across the frame, plus one radial term per band
    (:func:`_fit_balance`). The gains take away the strips between flight lines - the photos of
    one heading or one moment brighter than the next (sun and view geometry, a light sensor
    tilted or seeing another cloud than the ground) - and the planes the light changing across a
    frame (a cloud's shadow, the reflectance of the vegetation seen from one side); the radial
    term the edges of every frame brighter or darker than its middle. The mean level is kept.
    ``{band: {camera key: [gain, slope u, slope v, u from, u to, v from, v to]}}`` (the plane holds on the
    part of the frame the photo's points covered) plus ``"spread"`` (the standard deviation of
    the log gains per band, what the balance took away), ``"radial"`` and ``"residual"`` (the MAD of
    the log readings of a point between the photos once balanced). ``planes=False``: a gain per
    photo only."""
    from .colour import _surface_points
    from .visibility import project_valid

    progress = as_progress(progress)
    ms = chunk.meta.get("multispectral") or {}
    bands = [b for b in (bands or ms.get("bands") or []) if b not in NON_SPECTRAL]  # temperatures: offsets, see thermal
    cams = [c for c in multispectral_cameras(chunk) if c.aligned]
    if not bands or len(cams) < 2:
        return {}
    if samples is None:
        samples = int(min(BALANCE_POINTS_MAX, max(4000, BALANCE_POINTS_PER_PHOTO * len(cams))))
    pts = _surface_points(chunk, samples)
    if not len(pts):
        return {}
    obs: dict[str, list] = {b: [] for b in bands}
    for i, cam in enumerate(cams):
        progress(i / len(cams), f"Balancing the photos: {i + 1} of {len(cams)}")
        px, _depth, ok = project_valid(chunk, cam, pts, margin=4.0)
        idx = np.nonzero(ok)[0]
        if not len(idx):
            continue
        stack = band_stack(chunk, cam, bands)
        h, w = stack.shape[:2]
        c = np.clip(np.floor(px[idx, 0]).astype(int), 0, w - 1)
        r = np.clip(np.floor(px[idx, 1]).astype(int), 0, h - 1)
        uu, vv = px[idx, 0] / w - 0.5, px[idx, 1] / h - 0.5  # the place in the frame, -0.5 to 0.5
        for k, b in enumerate(bands):
            v = stack[r, c, k]
            good = np.isfinite(v) & (v > 1e-6)
            obs[b].append((idx[good], np.full(good.sum(), i), np.log(v[good]), uu[good], vv[good]))
    out: dict = {"spread": {}, "radial": {}, "residual": {}, "model": "planes" if planes else "gains"}
    for b in bands:
        if not obs[b]:
            continue
        point, photo, logv, u, v = (np.concatenate([o[j] for o in obs[b]]) for j in range(5))
        seen = np.bincount(point, minlength=len(pts))
        keep = seen[point] >= 2
        point, photo, logv, u, v = point[keep], photo[keep], logv[keep], u[keep], v[keep]
        if not len(point):
            continue
        coef, radial, n_ph, residual = _fit_balance(point, photo, logv, u, v, len(pts), len(cams), rounds, planes)
        box = _photo_ranges(photo, u, v, len(cams))
        out[b] = {str(cam.key): [float(np.exp(coef[i, 0])), float(coef[i, 1]), float(coef[i, 2]), *(float(x) for x in box[i])]
                  for i, cam in enumerate(cams) if n_ph[i] > 0}
        out["spread"][b] = float(coef[n_ph > 0, 0].std())
        out["residual"][b] = residual
        if planes:
            out["radial"][b] = radial
    progress(1.0, "Photos balanced: " + ", ".join(f"{b} {s * 100:.1f} %" for b, s in out["spread"].items()))
    return out


def build_reflectance(chunk: Chunk, bands: list[str] | None = None, resolution: float | None = None,
                      progress: Progress | None = None, balance: bool = True, **ortho_options) -> Path:
    """The reflectance orthomosaic: ``<data>/multispectral/reflectance.tif``, a float32 band per
    spectral band on the grid of the orthophoto engine (``resolution`` as the orthomosaic), NaN
    where nothing. Three bands at a time go through the engine (its channels). ``balance``: the
    photos brought to the same reflectance where they overlap first (:func:`balance_gains`),
    against the strips between flight lines; the gains are kept in ``meta["multispectral"]
    ["balance"]``."""
    import rasterio

    from .ortho import build_orthomosaic

    progress = as_progress(progress)
    ms = chunk.meta.get("multispectral") or {}
    bands = list(bands or [b for b in (ms.get("bands") or []) if b != "Visible"])
    cams = multispectral_cameras(chunk)
    if not bands or not cams:
        raise RuntimeError("no multispectral captures in the chunk (add them with add_captures)")
    if all(b in NON_SPECTRAL for b in bands) and ms.get("reference") == "Visible":
        raise RuntimeError("the captures of a thermal camera have no spectral bands: build the temperature orthomosaic")
    folder = chunk.data_dir / "multispectral"
    folder.mkdir(parents=True, exist_ok=True)
    gains = None
    if balance:
        gains = balance_gains(chunk, bands, progress=progress.sub(0.0, 0.1))
        ms["balance"] = gains
    else:
        ms.pop("balance", None)
    groups = [bands[i:i + 3] for i in range(0, len(bands), 3)]
    parts = []
    for gi, group in enumerate(groups):
        trio = (group + group[:1] * 3)[:3]  # three channels for the engine
        keys = {c.key for c in cams}

        def source(cam, trio=trio, keys=keys):
            if cam.key not in keys:
                return np.full((chunk.sensors[cam.sensor_key].height, chunk.sensors[cam.sensor_key].width, 3), np.nan, np.float32)
            return band_stack(chunk, cam, trio, gains=gains)

        out = folder / f"part_{gi}.tif"
        start = 0.1 if balance else 0.0
        progress(start + (1 - start) * gi / len(groups), f"Reflectance bands {', '.join(group)}")
        build_orthomosaic(chunk, resolution=resolution, source=source, out_path=out,
                          progress=progress.sub(start + (1 - start) * gi / len(groups), (0.9 - start) / len(groups)),
                          **ortho_options)
        parts.append((group, out))
    final = folder / "reflectance.tif"
    with rasterio.open(parts[0][1]) as first:
        profile = first.profile.copy()
    # BigTIFF: a classic TIFF stops at 4 GB, past it the last bands of a large survey read as zeros
    profile.update(count=len(bands), dtype="float32", nodata=float("nan"), BIGTIFF="IF_SAFER")
    with rasterio.open(final, "w", **profile) as dst:
        k = 1
        for group, path in parts:
            with rasterio.open(path) as src:
                for j, b in enumerate(group):
                    dst.write(src.read(j + 1), k)
                    dst.set_band_description(k, b)
                    k += 1
    for _group, path in parts:
        path.unlink(missing_ok=True)
    ms["reflectance"] = str(final.relative_to(chunk.data_dir))
    ms["reflectance_bands"] = bands
    progress(1.0, f"Reflectance orthomosaic: {len(bands)} bands")
    return final


# ------------------------------------------------------------------------------ indices and zoning
def formula_bands(formula: str) -> set[str]:
    """The band names a formula uses (``np`` and the numbers left out)."""
    import ast

    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"formula {formula!r}: {exc.msg}") from None
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id != "np"}


def missing_bands(formula: str, available) -> list[str]:
    """The bands a formula needs that ``available`` has not got (sorted)."""
    return sorted(formula_bands(formula) - set(available))


def available_indices(bands) -> list[str]:
    """The indices of :data:`INDICES` whose bands are all among ``bands``."""
    return [name for name, (formula, _) in INDICES.items() if not missing_bands(formula, bands)]


def index_values(formula: str, bands: dict[str, np.ndarray]) -> np.ndarray:
    """``formula`` over the band arrays (names as in :data:`INDICES`); NaN where undefined. A
    band the captures have not got (the thermal LWIR of a camera without it) is an error that
    names it, not a crash."""
    missing = missing_bands(formula, bands)
    if missing:
        raise ValueError(f"the formula needs the band(s) {', '.join(missing)}, which these captures have not got "
                         f"(bands: {', '.join(bands)})")
    names = {k: np.asarray(v, np.float32) for k, v in bands.items()}
    with np.errstate(divide="ignore", invalid="ignore"):
        out = eval(formula, {"__builtins__": {}, "np": np}, names)  # noqa: S307 - our own formulas
    out = np.asarray(out, np.float32)
    out[~np.isfinite(out)] = np.nan
    return out


def index_raster(chunk: Chunk, name: str, formula: str | None = None) -> Path:
    """The index ``name`` (one of :data:`INDICES`, or any ``formula`` over the band names) from
    the reflectance orthomosaic: ``<data>/multispectral/<name>.tif``."""
    import rasterio

    ms = chunk.meta.get("multispectral") or {}
    path = ms.get("reflectance")
    if not path:
        raise RuntimeError("build the reflectance orthomosaic first")
    formula = formula or INDICES[name][0]
    with rasterio.open(chunk.data_dir / path) as src:
        bands = {src.descriptions[i] or f"b{i + 1}": src.read(i + 1) for i in range(src.count)}
        profile = src.profile.copy()
    out = index_values(formula, bands)
    profile.update(count=1, dtype="float32", nodata=float("nan"), BIGTIFF="IF_SAFER")
    if not profile.get("tiled"):
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)
    target = chunk.data_dir / "multispectral" / f"{name}.tif"
    with rasterio.open(target, "w", **profile) as dst:
        dst.write(out, 1)
        dst.set_band_description(1, name)
    ms.setdefault("indices", {})[name] = {"file": str(target.relative_to(chunk.data_dir)), "formula": formula}
    return target


ZONING_METHODS = ("quantiles", "equal", "jenks", "kmeans")
ZONING_SAMPLE = 200_000  # cells drawn (fixed seed) when an index has more, for k-means and natural breaks
JENKS_SAMPLE = 3000  # values the natural breaks are exact on (a table of n x n sums: 72 MB, 0.13 s for 10 classes)


def _zoning_sample(v: np.ndarray) -> np.ndarray:
    """At most :data:`ZONING_SAMPLE` of the values ``v``, always the same ones (seed 0)."""
    return v if v.size <= ZONING_SAMPLE else np.random.default_rng(0).choice(v, ZONING_SAMPLE, replace=False)


def _fisher_jenks(x: np.ndarray, w: np.ndarray, k: int, block: int = 512) -> list[int]:
    """Fisher's optimal partition of the sorted distinct values ``x`` (weighed by their counts
    ``w``, ``k`` < len(x)) into ``k`` runs of least total sum of squared deviations from their
    means: dynamic programming over the sums of every run ``x[a:b]``, from cumulative sums of the
    values centred on their mean in double precision. Returns the index of the first value of
    every class after the first."""
    u = x.size
    y = x - np.sum(w * x) / np.sum(w)
    n, s, q = (np.concatenate([[0.0], np.cumsum(t)]) for t in (w, w * y, w * y * y))
    cost = np.full((u, u), np.inf)  # cost[a, b - 1]: the run x[a:b], a < b
    for b0 in range(1, u + 1, block):
        b = np.arange(b0, min(u, b0 + block - 1) + 1)
        a = np.arange(b[-1])[:, None]
        with np.errstate(divide="ignore", invalid="ignore"):
            c = (q[b] - q[a]) - (s[b] - s[a]) ** 2 / (n[b] - n[a])
        cost[:b[-1], b0 - 1:b[-1]] = np.where(a < b, np.maximum(c, 0.0), np.inf)
    best = cost[0]  # best[b - 1]: the least sum with one class over the first b values
    first = np.zeros((k + 1, u), np.int64)  # first[j, b - 1]: where the j-th class starts in the best of b values
    for j in range(2, k + 1):
        before = np.concatenate([[np.inf], best])  # j - 1 classes over the first a values
        new = np.empty(u)
        for b0 in range(0, u, block):
            cols = slice(b0, min(u, b0 + block))
            total = before[:cols.stop, None] + cost[:cols.stop, cols]
            a = np.argmin(total, axis=0)
            new[cols] = total[a, np.arange(total.shape[1])]
            first[j, cols] = a
        best = new
    starts, b = [], u
    for j in range(k, 1, -1):
        b = int(first[j, b - 1])
        starts.append(b)
    return starts[::-1]


def _between(low: float, high: float) -> float:
    """A break halfway between two values that keeps ``low`` below and ``high`` above it once
    cast to float32, as :func:`zoning` compares (two neighbouring floats: ``high``)."""
    mid = np.float32((float(low) + float(high)) / 2)
    return float(mid if mid > np.float32(low) else np.float32(high))


def natural_breaks(values: np.ndarray, classes: int) -> list[float]:
    """Fisher's natural breaks (Jenks): the ``classes - 1`` breaks that make the classes the most
    homogeneous - the least sum of squared deviations from the means of the classes, the optimum
    k-means looks for by successive approximations with no guarantee of reaching it (on an index
    with a large population beside small ones it can stop splitting the large one and merging
    the small ones). Exact on a sorted sample of at most :data:`JENKS_SAMPLE` values: all the
    finite values when there are no more, else the values at evenly spaced ranks of the finite
    values (of :data:`ZONING_SAMPLE` of them drawn with a fixed seed when there are more), which
    stand for the distribution better than as many random cells. A break lies halfway between
    the last value of a class and the first of the next. With fewer distinct values than classes
    every value is a class and there are fewer breaks."""
    v = np.asarray(values).ravel()
    v = v[np.isfinite(v)]
    if v.size == 0 or classes < 2:
        return []
    if v.size > JENKS_SAMPLE:
        v = np.sort(_zoning_sample(v))
        v = v[((np.arange(JENKS_SAMPLE) + 0.5) * v.size / JENKS_SAMPLE).astype(np.int64)]
    x, w = np.unique(v.astype(np.float64), return_counts=True)
    starts = list(range(1, x.size)) if x.size <= classes else _fisher_jenks(x, w.astype(np.float64), classes)
    return [_between(x[i - 1], x[i]) for i in starts]


def zoning(values: np.ndarray, classes: int = 3, method: str = "quantiles", min_cells: int = 20) -> tuple[np.ndarray, list[float]]:
    """``values`` cut into ``classes`` (1 = the lowest): by ``quantiles`` (as many cells each),
    ``equal`` intervals, natural breaks (``jenks``: :func:`natural_breaks`, the most homogeneous
    classes) or ``kmeans`` (the same aim by successive approximations from the quantiles);
    patches smaller than ``min_cells`` given to their surroundings. Returns the class raster (0
    where NaN) and the breaks."""
    valid = np.isfinite(values)
    v = values[valid]
    if v.size == 0:
        return np.zeros(values.shape, np.int32), []
    if method == "quantiles":
        breaks = [float(np.quantile(v, k / classes)) for k in range(1, classes)]
    elif method == "equal":
        lo, hi = float(v.min()), float(v.max())
        breaks = [lo + (hi - lo) * k / classes for k in range(1, classes)]
    elif method == "jenks":
        breaks = natural_breaks(v, classes)
    elif method == "kmeans":
        sample = _zoning_sample(v)
        centres = np.quantile(sample, [(k + 0.5) / classes for k in range(classes)]).astype(np.float32)
        for _ in range(30):
            labels = np.argmin(np.abs(sample[:, None] - centres[None, :]), axis=1)
            new = np.array([sample[labels == k].mean() if (labels == k).any() else centres[k] for k in range(classes)], np.float32)
            if np.allclose(new, centres):
                break
            centres = new
        centres = np.sort(centres)
        breaks = [float((centres[k] + centres[k + 1]) / 2) for k in range(classes - 1)]
    else:
        raise ValueError("method: " + ", ".join(ZONING_METHODS))
    cls = np.zeros(values.shape, np.int32)
    cls[valid] = 1 + np.searchsorted(np.asarray(breaks, np.float32), v, side="right")
    if min_cells > 1:
        from scipy import ndimage

        for k in range(1, classes + 1):
            labels, n = ndimage.label(cls == k)
            if n == 0:
                continue
            sizes = ndimage.sum(np.ones_like(cls, np.int32), labels, index=np.arange(1, n + 1))
            small = np.isin(labels, np.flatnonzero(sizes < min_cells) + 1)
            if small.any():
                cls[small] = 0
        if (cls == 0).any() and valid.any():  # the small patches and nothing else take the nearest class
            idx = ndimage.distance_transform_edt(cls == 0, return_distances=False, return_indices=True)
            filled = cls[tuple(idx)]
            cls = np.where(valid & (cls == 0), filled, cls)
    return cls, breaks


def zoning_polygons(cls: np.ndarray, values: np.ndarray, transform, breaks: list[float]) -> list[tuple]:
    """Polygons of the classes (``(shapely geometry, attributes)``): class, its range, the mean
    and the area of the polygon."""
    from rasterio.features import shapes
    from shapely.geometry import shape as to_shape

    out = []
    mask = cls > 0
    for geom, value in shapes(cls.astype(np.int32), mask=mask, transform=transform, connectivity=4):
        k = int(value)
        poly = to_shape(geom)
        if poly.is_empty:
            continue
        from rasterio.features import rasterize

        inside = rasterize([(geom, 1)], out_shape=cls.shape, transform=transform, fill=0, dtype=np.uint8) == 1
        vals = values[inside & np.isfinite(values)]
        low = breaks[k - 2] if k >= 2 else float(np.nanmin(values))
        high = breaks[k - 1] if k - 1 < len(breaks) else float(np.nanmax(values))
        out.append((poly, {"classe": k, "da": round(low, 4), "a": round(high, 4),
                           "media": round(float(vals.mean()), 4) if vals.size else None, "area_m2": round(poly.area, 2)}))
    return out


def export_zoning(chunk: Chunk, name: str, path, classes: int = 3, method: str = "quantiles",
                  min_cells: int = 20) -> dict:
    """The zoning of the index ``name`` as polygons in a GeoPackage (``path``, layer ``zone``
    with class, range, mean and area) and as a class raster beside the index. Returns a
    summary (breaks, polygons, area per class)."""
    import rasterio

    from ..io import gpkg

    ms = chunk.meta.get("multispectral") or {}
    info = (ms.get("indices") or {}).get(name)
    if not info:
        raise RuntimeError(f"build the index {name} first")
    with rasterio.open(chunk.data_dir / info["file"]) as src:
        values = src.read(1)
        transform, crs = src.transform, src.crs
    cls, breaks = zoning(values, classes, method, min_cells)
    polys = zoning_polygons(cls, values, transform, breaks)
    columns = {"classe": "INTEGER", "da": "REAL", "a": "REAL", "media": "REAL", "area_m2": "REAL"}
    gpkg.write_table(path, "zone", "POLYGON", columns, polys, chunk.crs if chunk.crs != "LOCAL" else None, f"zone {name}")
    with rasterio.open(chunk.data_dir / "multispectral" / f"{name}_zone.tif", "w", driver="GTiff", height=cls.shape[0],
                       width=cls.shape[1], count=1, dtype="int32", nodata=0, transform=transform, crs=crs,
                       compress="deflate", BIGTIFF="IF_SAFER") as dst:
        dst.write(cls, 1)
    areas = {k: round(sum(a["area_m2"] for _, a in polys if a["classe"] == k), 2) for k in range(1, classes + 1)}
    summary = {"index": name, "classes": classes, "method": method, "breaks": [round(b, 4) for b in breaks],
               "polygons": len(polys), "area_m2": areas, "file": str(path),
               "raster": str((chunk.data_dir / "multispectral" / f"{name}_zone.tif").relative_to(chunk.data_dir))}
    ms.setdefault("zoning", {})[name] = summary
    json.dumps(summary)  # serialisable
    return summary
