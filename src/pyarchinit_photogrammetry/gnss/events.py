"""The external events of a receiver as shots: a cable from the flash shoe of the camera to the event pin of the
receiver makes it record the time of each shot. A RINEX observation file writes them as epochs of flag 5 (Emlid Reach
writes its "time marks" so, and convbin of RTKLIB from the TIM-TM2 messages of a u-blox log); their times, written to a
tenth of a microsecond in the time system of the file, become GPS time."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from . import rinex
from .shots import Shot, ShotSet

DOUBLE_EVENT_S = 0.005  # two events closer than this are one shot recorded twice (a bouncing contact)

_GPS_START = date(1980, 1, 6)
_WEEK = 604800
_BDT_BEHIND_GPS = 14  # BeiDou time is GPS time less 14 s
NO_EVENTS = "the receiver recorded no events"


def _ahead(time_system: str, fields: tuple, leap: int | None) -> int:
    """The seconds GPS time is ahead of the time system of the file: GPS, Galileo, QZSS and NavIC time are GPS time;
    UTC - and GLONASS time, which RINEX writes as UTC - is behind by the leap seconds (those of the header when it
    gives them, else those of the day); BeiDou time by 14 s."""
    if time_system in ("GLO", "UTC"):
        return leap or rinex.gps_minus_utc(datetime(*fields[:5]))
    if time_system == "BDT":
        return _BDT_BEHIND_GPS
    return 0


def _week_and_seconds(fields: tuple, ahead: int) -> tuple[int, float]:
    """The GPS week and seconds of the week of a time written as (year, month, day, hour, minute, seconds) in a system
    ``ahead`` seconds behind GPS time: whole seconds counted as integers, the fraction kept as written."""
    year, month, day, hour, minute, seconds = fields
    whole = (date(year, month, day) - _GPS_START).days * 86400 + hour * 3600 + minute * 60 + ahead
    week, rest = divmod(whole, _WEEK)
    tow = rest + seconds
    if tow >= _WEEK:
        week, tow = week + 1, tow - _WEEK
    return week, tow


def _apart(a: tuple[int, float], b: tuple[int, float]) -> float:
    return (b[0] - a[0]) * _WEEK + (b[1] - a[1])


def rinex_events(path: str | Path) -> ShotSet:
    """The external events (epoch flag 5) of a RINEX 2 or 3 observation file as shots, in the order of their times:
    each with its time in GPS time (``gps_time``, and the GPS week and seconds of the week, to the tenth of a
    microsecond the file writes, in ``source["gps_week"]`` and ``source["tow"]``), converted from the time system of the
    file (``TIME OF FIRST OBS``: GPS by default; UTC and GLONASS less the leap seconds, BeiDou less 14 s), ``index``
    the place of the shot (from 1), no position. An event within :data:`DOUBLE_EVENT_S` of the one kept before it is
    left out and counted (``duplicates``, and a warning). A file without events gives no shots and the warning
    :data:`NO_EVENTS`. A file packed in layers is read through :func:`.rinex.plain`; a navigation file, or a file that
    is not RINEX, is a ``ValueError`` naming it."""
    path = Path(path)
    info, leap, found = rinex.external_events(path)
    system = info.time_system
    timed = []
    for fields, line in found:
        week, tow = _week_and_seconds(fields, _ahead(system, fields, leap))
        timed.append(((week, tow), line))
    timed.sort(key=lambda item: item[0])
    shots: list[Shot] = []
    doubles = 0
    kept: tuple[int, float] | None = None
    for (week, tow), line in timed:
        if kept is not None and _apart(kept, (week, tow)) < DOUBLE_EVENT_S:
            doubles += 1
            continue
        kept = (week, tow)
        shots.append(Shot(index=len(shots) + 1, gps_time=week * float(_WEEK) + tow,
                          source={"file": str(path), "kind": "rinex_events", "line": line, "gps_week": week,
                                  "tow": tow, "time_system": system}))
    warnings = []
    if not found:
        warnings.append(NO_EVENTS)
    if doubles:
        warnings.append(f"{doubles} double event{'s' if doubles > 1 else ''} within {DOUBLE_EVENT_S:g} s of the one "
                        "before left out")
    return ShotSet(shots=shots, kind="rinex_events", name=path.name, warnings=warnings, duplicates=doubles)
