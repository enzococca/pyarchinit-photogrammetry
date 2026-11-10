"""The camera shots of an autopilot log: the CAM and TRIG messages of an ArduPilot DataFlash log (read by
:mod:`.dataflash`), with the GPS time, the position above the sea level and the attitude of each shot and the status
and accuracy of the GPS at that time.

The time of a shot. What ArduPilot writes in a CAM or TRIG message was checked in its source (``AP_Camera_Logging.cpp``,
``AP_Camera_Backend.cpp``, ``AP_GPS.cpp``, ``GPS_Backend.cpp`` of master, November 2026): ``TimeUS`` is the boot clock
of the autopilot (microseconds) at the shot - the time the interrupt of the feedback pin saw the flash of the camera
for a CAM, else the time the message was written (the trigger) - while ``GPSWeek``/``GPSTime`` are
``gps.time_week()``/``gps.time_week_ms()`` of the primary receiver: the GPS time of the LAST FIX, not of the shot (up
to one GPS period early, more in an outage). So the time of a shot is its ``TimeUS`` put in GPS time with the fixes of
the receiver in use: each GPS message (``GWk``, ``GMS``: the GPS time of a fix) has beside it a GPA message of the
same instance and ``TimeUS`` whose ``SMS`` is ``last_message_time_ms`` - the boot time (ms) the fix arrived, corrected
for the jitter of the serial port by ArduPilot (``correct_offboard_timestamp_usec``), the same mapping ArduPilot's own
``time_epoch_usec`` uses. Around each shot the offsets GPS time - arrival of up to :data:`NEIGHBOURS` fixes before and
after it are taken (their medians: one bad fix is outvoted) and interpolated at its ``TimeUS``: a local mapping that
follows the drift of the autopilot's clock (tens of ppm) over a long flight and bridges an outage of the GPS up to
:data:`MAX_GAP_S` (beyond the first or last fix up to :data:`MAX_EXTRAPOLATION_S`). Without GPA (or SMS) the
``TimeUS`` of the GPS messages stands for the arrival, with a warning (it is the time they were written, later by the
processing). ``GPSTime`` remains a check: the shot must come after the fix it names by the time since the last fix
that had arrived before it (the "age"), give or take one GPS period (a fix that arrived just before the shot may not
yet be processed when the message is written, one that arrived just after may already be) and
:data:`CHECK_MARGIN_S`; a shot outside that (a wrong week, a bad fix) is not valid, with a warning.

What is not removed: the latency of the receiver (from the epoch of a fix to its arrival on the serial port, tens of
ms on a u-blox; ArduPilot's ``GPS_DELAY_MS`` is its own estimate of it, not logged with the fixes) makes every time
early by that constant - :func:`.shots.pair` and ``apply_shots`` take it as ``latency_s`` - and the delay of the
camera from the flash shoe to the middle of the exposure likewise. Verified on synthetic logs with the semantics
above (1 to 10 Hz, 50 ppm drift, outages): within 1.5 ms of the true time (see the tests). Not verified: real logs of
every receiver driver (a driver without corrected timestamps gives the time the message was parsed as SMS)."""
from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np

from .dataflash import SCALES, DataFlash, read_dataflash
from .shots import Shot, ShotSet

__all__ = ["DataFlash", "read_dataflash", "ardupilot", "cameras", "SCALES"]

# Status of a GPS message (AP_GPS_FixType): 0 no GPS, 1 no fix, 2 2D, 3 3D, 4 DGPS, 5 RTK float, 6 RTK fixed,
# 7 static (a fixed position, as of a base station: no RTK claim, so single), 8 PPP (decimetres once converged: float,
# and the accuracy of GPA, which is never below its floor, says the rest)
GPS_STATUS = {0: "none", 1: "none", 2: "single", 3: "single", 4: "single", 5: "float", 6: "fix", 7: "single",
              8: "float"}
MESSAGES = {"cam": "CAM", "trig": "TRIG"}
SHOT_MESSAGES = ("CAM", "TRIG", "GPS", "GPA", "PARM")  # what the shots need of a log
TIME_BASES = ("gpa_sms", "gps_timeus", "cam_gpstime")  # how the times of the shots were found, best first
NEAREST_GPS_S = 1.0  # status and accuracy of a shot from the GPS and GPA messages this close to it
NEIGHBOURS = 5  # fixes taken on each side of a shot to put its boot time in GPS time
MAX_GAP_S = 120.0  # a shot between two fixes further apart than this (an outage) has no GPS time
MAX_EXTRAPOLATION_S = 10.0  # nor one further than this before the first fix or after the last
CHECK_MARGIN_S = 0.1  # the GPSTime of a shot and its time disagree by more than this: not valid
_WEEK = 604800
_ANTENNA_OFFSET = re.compile(r"^GPS(_POS\d_|\d_POS_)[XYZ]$")  # where the antenna is in the body of the drone


def _column(arr: np.ndarray, *names: str) -> np.ndarray | None:
    for n in names:
        if n in arr.dtype.names:
            return arr[n]
    return None


def _ints(col: np.ndarray | None, size: int, fill: int) -> np.ndarray:
    """A column as int64, ``fill`` where it is missing or not a number (a message defined without the field)."""
    if col is None:
        return np.full(size, fill, dtype=np.int64)
    col = np.asarray(col)
    if col.dtype.kind == "f":
        col = np.where(np.isfinite(col) & (np.abs(col) < 9e18), col, fill)
    return col.astype(np.int64)


def _floats(col: np.ndarray | None, size: int) -> np.ndarray:
    return np.full(size, np.nan) if col is None else np.asarray(col, dtype=np.float64)


def _nearest(times: np.ndarray, at: float, within: float) -> int | None:
    """The index in the sorted ``times`` nearest to ``at``, if within ``within``."""
    if not len(times):
        return None
    k = int(np.searchsorted(times, at))
    best = min((i for i in (k - 1, k) if 0 <= i < len(times)), key=lambda i: abs(times[i] - at))
    return best if abs(times[best] - at) <= within else None


def _plural(n: int) -> str:
    return "s" if n > 1 else ""


# --- the receiver in use: status, accuracy and the clock -------------------------------------------------------


def _receiver(df: DataFlash) -> dict | None:
    """The GPS messages of the receiver in use (``U``; else instance 0) sorted by ``TimeUS``: ``boot`` (s),
    ``status``, ``instance``; and its fixes for the clock: ``A`` the boot time of each fix (s, sorted), ``O`` its GPS
    time less that, ``base`` (``"gpa_sms"`` or ``"gps_timeus"``)."""
    if not df.count("GPS"):
        return None
    gps = df.messages("GPS", multipliers=False)
    n = len(gps)
    status = _column(gps, "Status")
    if "TimeUS" not in gps.dtype.names or status is None:
        return None
    boot_us = _ints(gps["TimeUS"], n, -1)
    inst = _ints(_column(gps, "I"), n, 0)
    use = _ints(_column(gps, "U"), n, 0)
    keep = boot_us >= 0
    if "U" in gps.dtype.names and np.any(use != 0):
        keep &= use != 0
    else:
        keep &= inst == 0
    order = np.flatnonzero(keep)[np.argsort(boot_us[keep], kind="stable")]
    week = _ints(_column(gps, "GWk", "Week"), n, 0)[order]
    ms = _ints(_column(gps, "GMS", "TimeMS"), n, -1)[order]
    boot_us, inst = boot_us[order], inst[order]
    rec = {"boot": boot_us * 1e-6, "status": _ints(status, n, -1)[order], "instance": inst}
    good = (week > 0) & (ms >= 0) & (ms < _WEEK * 1000)
    gps_s = week * float(_WEEK) + ms / 1000.0
    sms = _sms(df, boot_us, inst)
    if np.count_nonzero(good & np.isfinite(sms)) >= 2:
        rec["base"], arrival, good = "gpa_sms", sms, good & np.isfinite(sms)
    else:
        rec["base"], arrival = "gps_timeus", boot_us * 1e-6
    a, g = arrival[good], gps_s[good]
    order = np.lexsort((a, g))  # by GPS time, then arrival: one fix logged twice is kept once, at its first arrival
    a, g = a[order], g[order]
    first = np.ones(len(g), bool)
    first[1:] = g[1:] != g[:-1]
    a, g = a[first], g[first]
    order = np.argsort(a, kind="stable")
    rec["A"], rec["O"] = a[order], g[order] - a[order]
    return rec


def _sms(df: DataFlash, boot_us: np.ndarray, inst: np.ndarray) -> np.ndarray:
    """For each GPS message, the ``SMS`` (s) of the GPA written with it (same ``TimeUS`` and instance); NaN without."""
    out = np.full(len(boot_us), np.nan)
    if not df.count("GPA"):
        return out
    gpa = df.messages("GPA", multipliers=False)
    if "SMS" not in gpa.dtype.names or "TimeUS" not in gpa.dtype.names:
        return out
    n = len(gpa)
    has_i = "I" in gpa.dtype.names
    found = {(int(i), int(t)): int(s) for t, i, s in
             zip(_ints(gpa["TimeUS"], n, -1), _ints(_column(gpa, "I"), n, 0), _ints(gpa["SMS"], n, 0)) if s > 0}
    for k, (t, i) in enumerate(zip(boot_us.tolist(), inst.tolist())):
        s = found.get((i if has_i else 0, t))
        if s is not None:
            out[k] = s / 1000.0
    return out


def _accuracy(df: DataFlash) -> dict | None:
    """HAcc and VAcc (m) of the GPA messages by ``TimeUS`` (s) and instance (-1 for messages without one)."""
    if not df.count("GPA"):
        return None
    raw, gpa = df.messages("GPA", multipliers=False), df.messages("GPA")
    n = len(raw)
    if "TimeUS" not in raw.dtype.names or "HAcc" not in gpa.dtype.names or "VAcc" not in gpa.dtype.names:
        return None
    boot = _floats(raw["TimeUS"], n)
    order = np.argsort(boot, kind="stable")
    return {"boot": boot[order] * 1e-6, "hacc": _floats(gpa["HAcc"], n)[order],
            "vacc": _floats(gpa["VAcc"], n)[order], "instance": _ints(_column(raw, "I"), n, -1)[order]}


def _to_gps(rec: dict, b: float) -> tuple[float | None, str, float | None, float]:
    """The GPS time of the boot time ``b`` (s): ``(time, why not, age, period)`` - age the time since the last fix
    before ``b`` (None before the first), period the time between that fix and the next."""
    a, o = rec["A"], rec["O"]
    k = int(np.searchsorted(a, b, side="right"))
    before, after = slice(max(0, k - NEIGHBOURS), k), slice(k, k + NEIGHBOURS)
    spacing = float(np.median(np.diff(a))) if len(a) > 1 else 0.0
    if 0 < k < len(a):
        gap = float(a[k] - a[k - 1])
        if gap > MAX_GAP_S:
            return None, f"in a gap of {gap:.0f} s between GPS fixes", None, gap
        bb, ob = float(np.median(a[before])), float(np.median(o[before]))
        ba, oa = float(np.median(a[after])), float(np.median(o[after]))
        offset = ob + (oa - ob) * (b - bb) / (ba - bb) if ba > bb else ob
        return b + offset, "", b - float(a[k - 1]), gap
    far = b - float(a[-1]) if k else float(a[0]) - b
    if far > MAX_EXTRAPOLATION_S:
        return None, f"{far:.0f} s {'after the last' if k else 'before the first'} GPS fix", None, spacing
    side = before if k else after
    return b + float(np.median(o[side])), "", (b - float(a[-1])) if k else None, spacing


def _status_at(rec: dict | None, at: float, source: dict) -> tuple[str | None, int | None]:
    if rec is None:
        return None, None
    k = _nearest(rec["boot"], at, NEAREST_GPS_S)
    if k is None:
        return None, None
    raw = int(rec["status"][k])
    source["gps_status"] = raw
    source["gps_instance"] = int(rec["instance"][k])
    return GPS_STATUS.get(raw), int(rec["instance"][k])


def _std_at(acc: dict | None, at: float, instance: int | None) -> tuple[float, float, float] | None:
    if acc is None or instance is None:
        return None
    mine = (acc["instance"] == instance) | (acc["instance"] == -1)
    k = _nearest(acc["boot"][mine], at, NEAREST_GPS_S)
    if k is None:
        return None
    h, v = float(acc["hacc"][mine][k]), float(acc["vacc"][mine][k])
    if not (math.isfinite(h) and math.isfinite(v) and 0 < h < 1000 and 0 < v < 1000):
        return None
    return h, h, v


def _antenna_offset(params: np.ndarray | None) -> list[str]:
    if params is None or "Name" not in params.dtype.names or "Value" not in params.dtype.names:
        return []
    found = {}
    for n, v in zip(params["Name"], params["Value"]):
        if _ANTENNA_OFFSET.match(str(n)) and math.isfinite(float(v)) and float(v) != 0.0:
            found[str(n)] = float(v)
    if not found:
        return []
    values = ", ".join(f"{n} = {v:g} m" for n, v in sorted(found.items()))
    return [f"the autopilot places the GPS antenna away from its origin ({values}): the positions of the log are of "
            "that origin (the IMU), so measure the lever arm of the camera from it"]


# --- the shots -------------------------------------------------------------------------------------------------


def _choose(df: DataFlash, message: str) -> str:
    if message not in ("auto", *MESSAGES):
        raise ValueError(f"message must be one of auto, {', '.join(MESSAGES)}, not {message!r}")
    if message != "auto":
        name = MESSAGES[message]
        if not df.count(name):
            raise ValueError(f"{df.path.name}: the log has no {name} messages")
        return name
    for name in ("CAM", "TRIG"):
        if df.count(name):
            return name
    raise ValueError(f"{df.path.name}: the log has no camera messages (CAM or TRIG): no camera was set up to log "
                     "its shots")


def ardupilot(path: str | Path, message: str = "auto", camera: int | None = None) -> ShotSet:
    """The camera shots of an ArduPilot log (:func:`.dataflash.read_dataflash`): its CAM messages - the shots the
    camera confirmed on its feedback pin, or the triggers when no pin is set - or, with ``message="trig"`` or
    ``"auto"`` in a log without CAM, its TRIG messages (the commands to shoot, not the shots: a warning). ``camera``
    chooses the camera (``I``) of a log with several; without it they are a ``ValueError`` that lists them. The layouts
    of ArduPilot 3.x to master are read: with or without ``I`` and ``Img``, attitude ``R P Y`` (centidegrees or float
    degrees) or ``Roll Pitch Yaw``.

    Each shot: ``index`` the image number ``Img`` (None in logs without it); ``gps_time`` its ``TimeUS`` in GPS time
    (see the module: to about a millisecond, early by the latency of the receiver), ``source["time_base"]`` saying how
    (:data:`TIME_BASES`: ``"cam_gpstime"``, the time of the last fix, only when the log has no fixes to map with, with
    a warning). Shots outside the fixes (:data:`MAX_GAP_S`, :data:`MAX_EXTRAPOLATION_S`) or whose ``GPSTime`` disagrees
    (:data:`CHECK_MARGIN_S`) are not valid, never paired, with a warning. Rows without a GPS time (``GPSWeek`` 0: no
    fix yet) or without a position are left out and counted.

    ``antenna`` is ``(Lat, Lng, Alt)`` with ``height="msl"``: the position the AHRS/EKF of the autopilot estimates when
    the message is written (a few ms after the pin), ``Alt`` its altitude above the sea level fused with the barometer
    - never ellipsoidal, and not the GPS height (``GPSAlt``, in ``source["gps_alt"]``); it is the position of the
    autopilot's origin, which is the antenna unless ``GPS_POS1_X/Y/Z`` place it elsewhere in the body (a warning when
    the log's parameters do). It matters only without PPK, which replaces it. ``attitude`` is ``(R, P, Y)`` in degrees
    (yaw 0-360 from north). ``status`` comes from ``Status`` of the GPS message of the receiver in use (``U``) nearest
    in time (``TimeUS``) within :data:`NEAREST_GPS_S` (:data:`GPS_STATUS`), ``std`` (north, east, up) from ``HAcc``,
    ``HAcc``, ``VAcc`` of the GPA of the same receiver nearest within that time; None without them. ``source`` keeps
    the message, ``row``, ``instance``, ``img``, ``gps_week`` and ``tow`` (s) of the shot time, ``time_us``,
    ``time_base``, ``cam_gps_week`` and ``cam_tow_ms`` (what the message says: the last fix), ``rel_alt``,
    ``gps_alt``, ``gps_status`` and ``gps_instance``."""
    path = Path(path)
    with read_dataflash(path, names=SHOT_MESSAGES) as df:
        name = _choose(df, message)
        rows = df.messages(name, multipliers=False)
        rec, acc = _receiver(df), _accuracy(df)
        params = df.messages("PARM", multipliers=False) if df.count("PARM") else None
        warnings = list(df.warnings)
    kind = "ardupilot_cam" if name == "CAM" else "ardupilot_trig"
    n = len(rows)
    missing = [c for c in ("GPSWeek", "GPSTime", "Lat", "Lng", "Alt") if c not in rows.dtype.names]
    if missing:
        raise ValueError(f"{path.name}: the {name} messages have no {', '.join(missing)}: not a format this reader "
                         "knows")
    week, tow_ms = _ints(rows["GPSWeek"], n, -1), _ints(rows["GPSTime"], n, -1)
    lat, lon, alt = _floats(rows["Lat"], n), _floats(rows["Lng"], n), _floats(rows["Alt"], n)
    instances = _ints(_column(rows, "I"), n, 0)  # a layout without I has one camera
    found = sorted(set(instances.tolist()))
    if camera is None and len(found) > 1:
        raise ValueError(f"{path.name}: the log has {len(found)} cameras ({name} instances "
                         f"{', '.join(map(str, found))}): choose one")
    if camera is not None and camera not in found:
        raise ValueError(f"{path.name}: no camera {camera} in the log (cameras {', '.join(map(str, found))})")
    chosen = found[0] if camera is None else camera
    img = _floats(_column(rows, "Img"), n)
    att = [_floats(_column(rows, *names), n) for names in (("R", "Roll"), ("P", "Pitch"), ("Y", "Yaw"))]
    boot = _ints(_column(rows, "TimeUS"), n, -1)
    rel, galt = _column(rows, "RelAlt"), _column(rows, "GPSAlt")
    mapped = rec is not None and len(rec["A"]) >= 2
    base = rec["base"] if mapped else "cam_gpstime"
    shots: list[Shot] = []
    no_fix = no_position = no_gps = 0
    unmapped: list[str] = []
    disagree: list[float] = []
    for k in np.flatnonzero(instances == chosen):
        w, ms = int(week[k]), int(tow_ms[k])
        if w <= 0 or not 0 <= ms < _WEEK * 1000:
            no_fix += 1
            continue
        la, lo, h = float(lat[k]), float(lon[k]), float(alt[k])
        if not (abs(la) <= 90 and abs(lo) <= 180 and math.isfinite(h)) or (la == 0 and lo == 0):
            no_position += 1
            continue
        cam_t = w * float(_WEEK) + ms / 1000.0
        source = {"file": str(path), "kind": kind, "message": name, "row": int(k), "instance": int(instances[k]),
                  "img": int(img[k]) if math.isfinite(img[k]) else None, "cam_gps_week": w, "cam_tow_ms": ms}
        if rel is not None:
            source["rel_alt"] = float(rel[k])
        if galt is not None:
            source["gps_alt"] = float(galt[k])
        attitude = tuple(float(a[k]) for a in att)
        attitude = attitude if all(map(math.isfinite, attitude)) else None
        t, valid, status, std = cam_t, True, None, None
        shot_base = base
        if boot[k] >= 0:
            at = int(boot[k]) * 1e-6
            source["time_us"] = int(boot[k])
            status, gps_instance = _status_at(rec, at, source)
            if "gps_status" not in source:
                no_gps += 1
            std = _std_at(acc, at, gps_instance)
            if mapped:
                mapped_t, why, age, period = _to_gps(rec, at)
                if mapped_t is None:
                    valid = False
                    unmapped.append(why)
                    source["no_gps_time"] = why
                else:
                    t = mapped_t
                    apart = t - cam_t
                    low = (age - period if age is not None else -MAX_EXTRAPOLATION_S) - CHECK_MARGIN_S
                    high = (age + period if age is not None else MAX_EXTRAPOLATION_S) + CHECK_MARGIN_S
                    if not low <= apart <= high:
                        source["gps_time_disagrees_s"] = round(apart - (age or 0.0), 3)
                        valid = False
                        disagree.append(apart - (age or 0.0))
        else:
            shot_base = "cam_gpstime"
        source["time_base"] = shot_base
        gps_week = int(t // _WEEK)
        source["gps_week"], source["tow"] = gps_week, round(t - gps_week * _WEEK, 6)
        shots.append(Shot(index=source["img"], gps_time=gps_week * float(_WEEK) + source["tow"],
                          antenna=(la, lo, h), height="msl", std=std, status=status, attitude=attitude,
                          valid=valid, source=source))
    shots.sort(key=lambda s: s.gps_time)
    if name == "TRIG":
        warnings.append("TRIG messages are the commands to shoot, not the shots: the camera may expose later or not "
                        "at all (a feedback pin gives CAM messages, the confirmed shots)")
    if any(s.source["time_base"] == "cam_gpstime" for s in shots):
        warnings.append("the log has no GPS fixes to put the boot times of the shots in GPS time: the times are those "
                        "of the last fix before each shot (GPSTime), early by up to a GPS period")
    elif base == "gps_timeus" and shots:
        warnings.append("the GPS fixes have no arrival time (no GPA with SMS): their times of writing stand for it, so "
                        "the shot times can be early by the processing of the fixes (some ms to tens of ms)")
    if no_fix:
        warnings.append(f"{no_fix} {name} message{_plural(no_fix)} without a GPS time (GPSWeek 0: no GPS fix yet) "
                        "left out")
    if no_position:
        warnings.append(f"{no_position} {name} message{_plural(no_position)} without a position left out")
    if no_gps:
        warnings.append(f"{no_gps} {name} shot{_plural(no_gps)} with no GPS message of the receiver in use within "
                        f"{NEAREST_GPS_S:g} s: no status")
    if unmapped:
        warnings.append(f"{len(unmapped)} {name} shot{_plural(len(unmapped))} without a GPS time ({unmapped[0]}): not "
                        "valid, never paired")
    if disagree:
        worst = max(disagree, key=abs)
        warnings.append(f"{len(disagree)} {name} shot{_plural(len(disagree))} whose GPSTime disagrees with the GPS "
                        f"fixes by up to {worst:.3f} s (a wrong week, a bad fix): not valid, never paired")
    warnings += _antenna_offset(params)
    return ShotSet(shots=shots, kind=kind, name=path.name, warnings=warnings, path=str(path))


def cameras(path: str | Path) -> list[dict]:
    """What the cameras of an ArduPilot log recorded, one dict per instance ``I``: ``cam`` and ``trig`` (how many
    CAM and TRIG messages), ``no_fix`` (messages without a GPS time, of CAM when there are some, else TRIG),
    ``first`` and ``last`` (their GPSTime: the last fix before the first and the last shot, enough to tell flights
    apart), ``first_img`` and ``last_img``."""
    path = Path(path)
    with read_dataflash(path, names=("CAM", "TRIG")) as df:
        tables = {name: df.messages(name, multipliers=False) for name in ("CAM", "TRIG") if df.count(name)}
    found: set[int] = set()
    for rows in tables.values():
        found |= set(_ints(_column(rows, "I"), len(rows), 0).tolist())
    out = []
    for i in sorted(found):
        entry = {"instance": i, "cam": 0, "trig": 0, "no_fix": 0, "first": None, "last": None, "first_img": None,
                 "last_img": None}
        for name in ("TRIG", "CAM"):  # CAM last: its times win
            rows = tables.get(name)
            if rows is None:
                continue
            mine = rows[_ints(_column(rows, "I"), len(rows), 0) == i]
            entry[name.lower()] = len(mine)
            if not len(mine) or "GPSWeek" not in mine.dtype.names or "GPSTime" not in mine.dtype.names:
                continue
            week, ms = _ints(mine["GPSWeek"], len(mine), -1), _ints(mine["GPSTime"], len(mine), -1)
            good = week > 0
            times = week[good] * float(_WEEK) + ms[good] / 1000.0
            entry["no_fix"] = int((~good).sum())
            entry["first"] = float(times.min()) if len(times) else None
            entry["last"] = float(times.max()) if len(times) else None
            img = _floats(_column(mine, "Img"), len(mine))
            if np.isfinite(img).any():
                entry["first_img"], entry["last_img"] = int(np.nanmin(img)), int(np.nanmax(img))
        out.append(entry)
    return out
