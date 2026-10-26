"""The RTK state DJI drones write into the XMP of every photo."""
from __future__ import annotations

import re

STATUS = {50: "fix", 34: "float", 16: "single", 0: "none"}

_STD_KEYS = ("RtkStdLon", "RtkStdLat", "RtkStdHgt")


def _value(text: str, key: str) -> str | None:
    """The value of ``drone-dji:<key>`` written as an attribute or as an element."""
    m = (re.search(rf"drone-dji:{key}\s*=\s*[\"']([^\"']*)[\"']", text)
         or re.search(rf"<drone-dji:{key}[^>]*>\s*([^<]*?)\s*</drone-dji:{key}>", text))
    return m.group(1).strip() if m else None


def _number(text: str, key: str) -> float | None:
    try:
        return float(_value(text, key))
    except (TypeError, ValueError):
        return None


def rtk_from_xmp(text: str) -> dict | None:
    """``{"flag", "status", "std": [lon, lat, hgt] | None, "diff_age"}`` from the XMP of a DJI photo,
    or None when it has no ``RtkFlag``. An unlisted flag has the status ``"unknown"``."""
    try:
        flag = int(_value(text, "RtkFlag"))
    except (TypeError, ValueError):
        return None
    std = [_number(text, k) for k in _STD_KEYS]
    return {"flag": flag, "status": STATUS.get(flag, "unknown"),
            "std": std if None not in std else None, "diff_age": _number(text, "RtkDiffAge")}
