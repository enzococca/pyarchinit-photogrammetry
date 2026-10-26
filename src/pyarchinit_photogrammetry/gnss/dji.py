"""What DJI drones record about the position of a photo: the RTK state in its XMP and the Timestamp.MRK."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import rinex

STATUS = {50: "fix", 34: "float", 16: "single", 0: "none"}

_STD_KEYS = ("RtkStdLon", "RtkStdLat", "RtkStdHgt")


def _value(text: str, key: str) -> str | None:
    """The value of ``drone-dji:<key>`` written as an attribute or as an element."""
    m = (re.search(rf"drone-dji:{key}\s*=\s*[\"']([^\"']*)[\"']", text)
         or re.search(rf"<drone-dji:{key}[^>]*>\s*([^<]*?)\s*</drone-dji:{key}>", text))
    return m.group(1).strip() if m else None


def _number(text: str, key: str) -> float | None:
    """The value of ``drone-dji:<key>`` as a finite number, None when it is missing or is not one."""
    try:
        value = float(_value(text, key))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


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


# --- Timestamp.MRK ---------------------------------------------------------------------------------------------

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
LEAP_SECONDS = 18  # GPS time is ahead of UTC by this many seconds since 2017; MrkShot.utc takes those of its date

_A = 6378137.0  # WGS84 semi-major axis (m)
_E2 = 0.00669437999014  # WGS84 first eccentricity squared

_NUM = r"[-+]?\d+(?:\.\d+)?"
# index, GPS seconds of the week, [GPS week], lever arm N, E, V (mm), antenna lat, lon, ellipsoidal height,
# standard deviations N, E, V (m), quality flag; tabs and padding spaces are both just white space
_MRK_LINE = re.compile(
    rf"^\s*(\d+)\s+({_NUM})\s+\[(\d+)\]\s+({_NUM}),N\s+({_NUM}),E\s+({_NUM}),V\s+({_NUM}),Lat\s+({_NUM}),Lon\s+"
    rf"({_NUM}),Ellh\s+({_NUM}),\s*({_NUM}),\s*({_NUM})\s+(\d+),Q\s*$")
# DJI_0001.JPG (Phantom 4 RTK) or DJI_20240512103015_0001_V.JPG (Mavic 3, Matrice 30/350): the 4 digits are the shot
_SHOT_NUMBER = re.compile(r"^DJI_(?:\d{14}_)?(\d{4})(?:_\w+)?$")


@dataclass
class MrkShot:
    """One line of a ``Timestamp.MRK``: the antenna position at the shot and the lever arm to the camera."""

    index: int
    gps_week: int
    gps_seconds: float
    offset_ned_mm: tuple[float, float, float]  # antenna phase centre -> camera: north, east, down (mm)
    lat: float  # of the antenna
    lon: float
    ellh: float  # ellipsoidal height of the antenna (m)
    std: tuple[float, float, float]  # north, east, vertical (m)
    flag: int  # 50 fix, 34 float, 16 single

    @property
    def gps_time(self) -> float:
        """The time of the shot in seconds of GPS time since 6 January 1980, as the times of a PPK solution."""
        return self.gps_week * 604800.0 + self.gps_seconds  # seconds in a week

    @property
    def utc(self) -> datetime:
        """The time of the shot in UTC: the GPS week and seconds from 6 January 1980, less the leap seconds GPS time
        was ahead of UTC by then (13 in 2005, 18 since 2017)."""
        gps = GPS_EPOCH + timedelta(weeks=self.gps_week, seconds=self.gps_seconds)
        return gps - timedelta(seconds=_gps_minus_utc(gps.replace(tzinfo=None)))


def _gps_minus_utc(gps: datetime) -> int:
    """The seconds GPS time was ahead of UTC at the GPS time ``gps`` (without a time zone). The table of the leap
    seconds goes by UTC dates, which GPS time reaches that many seconds earlier: looked up at ``gps`` itself, the last
    seconds before a new leap second would already count it, so it is looked up again at the UTC time the first look
    gives."""
    return rinex.gps_minus_utc(gps - timedelta(seconds=rinex.gps_minus_utc(gps)))


def read_mrk(path: str | Path) -> list[MrkShot]:
    """The shots of a ``Timestamp.MRK``, in the order of the file. A line that is not in the DJI format is a
    ``ValueError`` naming it: skipping it would put every later position on the wrong photo."""
    shots = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        m = _MRK_LINE.match(line)
        if not m:
            raise ValueError(f"{Path(path).name}, line {number}: not a DJI Timestamp.MRK line: {line.strip()[:80]!r}")
        v = m.groups()
        shots.append(MrkShot(index=int(v[0]), gps_week=int(v[2]), gps_seconds=float(v[1]),
                             offset_ned_mm=(float(v[3]), float(v[4]), float(v[5])), lat=float(v[6]),
                             lon=float(v[7]), ellh=float(v[8]), std=(float(v[9]), float(v[10]), float(v[11])),
                             flag=int(v[12])))
    return shots


def lever_arm(lat: float, lon: float, h: float, offset_ned_mm) -> tuple[float, float, float]:
    """``(lon, lat, h)`` of the camera hanging from an antenna at ``lat``, ``lon`` (degrees) and ellipsoidal height
    ``h``: the antenna plus the lever arm ``offset_ned_mm``, north, east and DOWN in millimetres (the camera hangs
    below the antenna). The arm is turned into degrees with the radii of curvature of the WGS84 ellipsoid at the
    antenna; for an arm under half a metre this is within a millimetre of the exact position."""
    north, east, down = (v / 1000.0 for v in offset_ned_mm)
    phi = math.radians(lat)
    w = 1.0 - _E2 * math.sin(phi) ** 2
    meridian = _A * (1.0 - _E2) / w ** 1.5
    prime_vertical = _A / math.sqrt(w)
    return (lon + math.degrees(east / ((prime_vertical + h) * math.cos(phi))),
            lat + math.degrees(north / (meridian + h)),
            h - down)


def camera_position(shot: MrkShot) -> tuple[float, float, float]:
    """``(lon, lat, h)`` of the camera at a shot: the antenna of its line plus its lever arm (:func:`lever_arm`)."""
    return lever_arm(shot.lat, shot.lon, shot.ellh, shot.offset_ned_mm)


def shot_number(name: str) -> int | None:
    """The shot number in the name of a DJI photo (``DJI_0001.JPG``, ``DJI_20240512103015_0001_V.JPG``), the
    index of its line in the ``Timestamp.MRK``; None when the name has none."""
    m = _SHOT_NUMBER.match(Path(name).stem)
    return int(m.group(1)) if m else None
