"""EXIF reading: image size, camera body, focal length and GPS position."""
from __future__ import annotations

from pathlib import Path

from PIL import ExifTags, Image

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
    ``focal_length_35mm``, ``datetime`` and ``gps`` = ``(lon, lat, alt)`` when present."""
    with Image.open(path) as im:
        info: dict = {"width": im.width, "height": im.height}
        try:
            exif = im.getexif()
        except Exception:
            return info
        if not exif:
            return info
        info["make"] = (exif.get(_TAGS["Make"]) or "").strip() or None
        info["model"] = (exif.get(_TAGS["Model"]) or "").strip() or None
        info["datetime"] = exif.get(_TAGS["DateTime"])
        sub = exif.get_ifd(_EXIF_IFD) if hasattr(exif, "get_ifd") else {}
        if sub.get(_TAGS["FocalLength"]):
            info["focal_length_mm"] = round(_ratio(sub[_TAGS["FocalLength"]]), 3)
        if sub.get(_TAGS["FocalLengthIn35mmFilm"]):
            info["focal_length_35mm"] = float(sub[_TAGS["FocalLengthIn35mmFilm"]])
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
