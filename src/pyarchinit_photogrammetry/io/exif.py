"""EXIF reading: image size, camera body, focal length and GPS position."""
from __future__ import annotations

from pathlib import Path

from PIL import ExifTags, Image

from ..gnss.dji import rtk_from_xmp

_TAGS = {v: k for k, v in ExifTags.TAGS.items()}
_GPS_IFD = 0x8825
_EXIF_IFD = 0x8769


def _ratio(v) -> float:
    try:
        return float(v)
    except TypeError:
        num, den = v
        return float(num) / float(den) if den else 0.0


def _dms(values, ref) -> float:
    d, m, s = (_ratio(x) for x in values)
    val = d + m / 60.0 + s / 3600.0
    return -val if ref in ("S", "W") else val


def read_exif(path: str | Path) -> dict:
    """Return ``width``, ``height``, ``make``, ``model``, ``lens``, ``focal_length_mm``,
    ``focal_length_35mm``, ``datetime``, ``gps`` = ``(lon, lat, alt)`` and ``rtk`` (the RTK state a DJI
    drone writes in the XMP, see :func:`~pyarchinit_photogrammetry.gnss.dji.rtk_from_xmp`) when present.
    A camera raw file is read by :func:`..raw.read_raw_exif` (PIL would open its thumbnail): its size is the
    one of the developed photo, known when rawpy is installed."""
    from . import raw

    if raw.is_raw(path):
        info = raw.read_raw_exif(path)
        if raw.available():
            try:
                import rawpy

                with rawpy.imread(str(path)) as r:
                    info["width"], info["height"] = int(r.sizes.iwidth), int(r.sizes.iheight)
            except Exception:
                pass
        return info
    with Image.open(path) as im:
        info: dict = {"width": im.width, "height": im.height}
        xmp = im.info.get("xmp")
        rtk = rtk_from_xmp(xmp.decode("utf-8", "ignore") if isinstance(xmp, bytes) else xmp or "")
        if rtk:
            info["rtk"] = rtk
        try:
            exif = im.getexif()
        except Exception:
            return info
        for key, value in exif_fields(exif).items():
            if key != "orientation":  # kept for the raws only: their pixels are developed unrotated
                info.setdefault(key, value)
    return info


def exif_fields(exif) -> dict:
    """The fields of :func:`read_exif` an EXIF block (``PIL.Image.Exif``) holds, with ``orientation`` (the EXIF
    value, when it is not 1) and ``rtk`` from an XMP packet kept as a TIFF tag (DNG)."""
    info: dict = {}
    if not exif:
        return info
    info["make"] = _text(exif.get(_TAGS["Make"]))
    info["model"] = _text(exif.get(_TAGS["Model"]))
    info["datetime"] = exif.get(_TAGS["DateTime"])
    if exif.get(_TAGS["Orientation"]) not in (None, 1):
        try:
            info["orientation"] = int(exif.get(_TAGS["Orientation"]))
        except (TypeError, ValueError):
            pass
    xmp = exif.get(700)
    if isinstance(xmp, (bytes, str)) and xmp:
        rtk = rtk_from_xmp(xmp.decode("utf-8", "ignore") if isinstance(xmp, bytes) else xmp)
        if rtk:
            info["rtk"] = rtk
    sub = exif.get_ifd(_EXIF_IFD) if hasattr(exif, "get_ifd") else {}
    if sub.get(_TAGS["FocalLength"]):
        info["focal_length_mm"] = round(_ratio(sub[_TAGS["FocalLength"]]), 3)
    if sub.get(_TAGS["FocalLengthIn35mmFilm"]):
        info["focal_length_35mm"] = float(sub[_TAGS["FocalLengthIn35mmFilm"]])
    res, unit = sub.get(0xA20E), sub.get(0xA210, 2)  # FocalPlaneXResolution, FocalPlaneResolutionUnit
    unit_mm = {2: 25.4, 3: 10.0, 4: 1.0}.get(int(unit) if isinstance(unit, (int, float)) else 2)
    if res and unit_mm and _ratio(res) > 0:
        info["focal_plane_mm_per_px"] = unit_mm / _ratio(res)
        if sub.get(0xA002):  # PixelXDimension: the width the resolution above refers to
            info["exif_width"] = int(sub[0xA002])
    if sub.get(_TAGS["DateTimeOriginal"]):
        info["datetime"] = sub[_TAGS["DateTimeOriginal"]]
    lens = sub.get(_TAGS["LensModel"]) if "LensModel" in _TAGS else None
    if lens:
        info["lens"] = str(lens).strip() or None
    gps = exif.get_ifd(_GPS_IFD) if hasattr(exif, "get_ifd") else {}
    if gps and 2 in gps and 4 in gps:
        try:
            lat = _dms(gps[2], gps.get(1, "N"))
            lon = _dms(gps[4], gps.get(3, "E"))
            alt = _ratio(gps[6]) if 6 in gps else 0.0
            if gps.get(5) in (1, b"\x01"):
                alt = -alt
            info["gps"] = (lon, lat, alt)
        except Exception:
            pass
    return info


def focal_px(info: dict) -> tuple[float | None, str | None]:
    """The focal length in pixels of a photo from its EXIF (:func:`read_exif`) and where it comes from:
    the 35 mm equivalent over 36 mm across the longer side (``exif``), else the focal length in mm
    over the size of a pixel on the sensor (FocalPlaneXResolution, ``exif_mm``); ``(None, None)``
    when the photo says neither, or a value no camera has (an equivalent under 4 mm or over 2000 mm,
    a sensor under 1 mm or over 100 mm across)."""
    w, h = info.get("width"), info.get("height")
    if not w or not h:
        return None, None
    side = max(w, h)
    f35 = info.get("focal_length_35mm")
    if f35 and 4.0 <= f35 <= 2000.0:
        return f35 / 36.0 * side, "exif"
    fmm, pitch = info.get("focal_length_mm"), info.get("focal_plane_mm_per_px")
    if fmm and pitch:
        width = info.get("exif_width") or w
        pitch *= width / w  # a photo made smaller after the camera keeps the EXIF of the full size
        across = pitch * side
        if 1.0 <= across <= 100.0 and 4.0 <= 36.0 * fmm / across <= 2000.0:
            return fmm / pitch, "exif_mm"
    return None, None


def _text(value) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    return (str(value).strip("\x00 ").strip() if value else "") or None
