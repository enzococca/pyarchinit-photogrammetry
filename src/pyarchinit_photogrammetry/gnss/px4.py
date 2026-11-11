"""The camera shots of a PX4 log (ULog, ``.ulg``, read by :mod:`.ulog`): the ``camera_capture`` topic, or
``camera_trigger`` when there is none, with the GPS time, position, attitude and GPS status of each shot.

What PX4 writes (PX4-Autopilot read for this, main of 2026: ``src/drivers/camera_capture``,
``src/drivers/camera_trigger``, ``src/modules/camera_feedback``; the uORB messages ``CameraCapture``,
``CameraTrigger``, ``SensorGps``). ``camera_trigger`` is published at each command to shoot (``feedback`` false) or,
with a camera feedback pin (the camera_capture driver), at each confirmed exposure (``feedback`` true: ``timestamp``
the middle of the exposure when the strobe gives its two edges). ``camera_capture`` is ``camera_feedback``'s copy of
each trigger with a position: ``timestamp`` and ``timestamp_utc`` of the trigger, ``seq``, ``lat``/``lon``/``alt`` of
``vehicle_global_position`` - the estimate of the autopilot (EKF) at that time, its altitude above the sea level
(AMSL) -, ``q`` the attitude of the vehicle, or of the gimbal when one reports it, and ``result`` 1. With
``CAM_CAP_FBACK`` set it copies only feedback triggers; otherwise the captures are the commands, not the exposures (a
warning).

The time of a shot. ``timestamp`` is the boot clock of the autopilot (us). ``timestamp_utc`` is UTC from the last PPS
of the receiver (``pps_capture``, when the receiver's PPS is wired) or from the real-time clock of the autopilot,
which PX4 sets from GPS time now and then: its accuracy is not known from the log. So the shot time is ``timestamp``
put in GPS time with a clock fitted to the GPS messages (:func:`.autopilot.fit_clock`, the clock of the ArduPilot
reader): each message of ``vehicle_gps_position`` (or, without it, ``sensor_gps``) has ``timestamp`` (the boot time it
was published) and ``time_utc_usec`` (UTC, us), put in GPS time with the leap seconds of its day. Two kinds were found
in real logs: receivers whose ``time_utc_usec`` is the epoch of the fix, on the grid of the fixes (u-blox: the boot
times are processing times, 0 to tens of ms after the arrival, one-sided - the clock is their envelope, time base
``"gps_fix"``; the envelope of the ArduPilot reader, whose rise to a branch one 20 ms loop above models ArduPilot's
50 Hz loop: whether PX4's GPS driver delays its messages in such branches no PX4 log here shows, the model is applied
as it is, not proven), and receivers that stamp each message with its own UTC off any grid (a DroneCAN receiver: the clock is
the median, ``"gps_stamp"``); they are told by whether the UTC times fall on the grid of the fixes. ``timestamp_utc``
is the check: its median difference from the fitted clock is kept per shot (``source["utc_minus_clock_ms"]``); over
:data:`UTC_CHECK_S` a warning, and when the difference is the leap seconds ``timestamp_utc`` was GPS time already
(said).
Without GPS messages with a time the shot time is ``timestamp_utc`` plus the leap seconds (``"timestamp_utc"``, with a
warning). ``timestamp_sample`` of the GPS messages is not used: on the real logs read (PX4 1.15, 1.16) it stays at
one value for the whole log.

As with ArduPilot, a boot clock within :data:`.autopilot.SLOW_PPM` of GPS time drifts too slowly through the
processing delays for every window of the envelope to hold a message processed at once: with ``"gps_fix"`` such shots
are warned of (``source["clock_ppm"]``); how early their times can be is measured on ArduPilot's loop only.

What is not removed: the latency of the receiver from the epoch of a fix to its message (``"gps_fix"``: every time
early by it, tens of ms on a u-blox; ``shots.pair`` and ``apply_shots`` take it as ``latency_s``) and, for the
commands, the delay of the camera to the exposure; nor, with ``"gps_stamp"``, whatever the receiver's own time of a
message is from the epoch of its fix (the log does not say). On logs the tests write: ``"gps_stamp"`` within 1.5 ms;
``"gps_fix"`` at 5 Hz with processing 0-30 ms late within 1 ms on logs of 10-30 minutes, up to 1.8 ms early on one of
80 s (the rate of the clock is a line through one-sided delays). Real logs (PX4 1.15.4 with a serial receiver, fix
epochs; 1.16.0 with a DroneCAN one, stamps; captures without feedback): ``timestamp_utc`` 9.8 ms before and 17.8 ms
after the fitted clock - which of the two is nearer the exposure no log here can tell. The DroneCAN receiver gave the
same ellipsoidal and AMSL heights (no geoid): such heights stay ``"msl"``.

The position: ``lat``/``lon`` of the capture and its AMSL altitude put on the ellipsoid with the separation the
receiver gives (``altitude_ellipsoid_m`` - ``altitude_msl_m`` of the GPS message nearest in time, its geoid; EGM96 on
u-blox) - ``height`` ``"ellipsoidal"``; without it ``"msl"``, never taken for an ellipsoidal height. It is the EKF's
estimate, not the antenna's fix: it matters only without PPK, which replaces it. A trigger without capture takes the
position of ``vehicle_global_position`` interpolated at its time. ``attitude`` (roll, pitch, yaw in degrees) from
``q``; ``status`` and ``std`` (``eph``, ``eph``, ``epv``) from the GPS message nearest in time within
:data:`.autopilot.NEAREST_GPS_S`."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import rinex
from .autopilot import NEAREST_GPS_S, SLOW_PPM, _plural, _to_gps, fit_clock
from .lever import quaternion_to_euler
from .shots import Shot, ShotSet
from .ulog import read_ulog

__all__ = ["px4", "TOPICS", "UTC_CHECK_S"]

TOPICS = {"capture": "camera_capture", "trigger": "camera_trigger"}
UTC_CHECK_S = 0.050  # timestamp_utc further than this from the fitted clock: a warning
GRID_S = 0.002  # UTC times of the GPS messages within this of the grid of the fixes: fix epochs
_UNIX_TO_GPS = 315964800  # seconds from 1970-01-01 to 1980-01-06
_WEEK = 604800
# fix_type of PX4's GPS messages: 0-1 none, 2 2D, 3 3D, 4 code differential, 5 RTK float, 6 RTK fixed, 8 extrapolated
GPS_STATUS = {0: "none", 1: "none", 2: "single", 3: "single", 4: "single", 5: "float", 6: "fix", 8: "single"}
_BUSES = {1: "I2C", 2: "SPI", 3: "DroneCAN", 4: "simulation", 5: "serial", 6: "MAVLink"}  # device_id & 7
SAME_HEIGHT_M = 0.001  # ellipsoidal and AMSL heights this close on every message: the receiver gives no geoid
_READ = ("camera_capture", "camera_trigger", "vehicle_gps_position", "sensor_gps", "vehicle_global_position",
         "pps_capture", "gimbal_device_attitude_status")


def _leap(utc_us: float) -> int:
    return rinex.gps_minus_utc(datetime(1970, 1, 1) + timedelta(microseconds=int(utc_us)))


def _firmware(log) -> str | None:
    release = log.info.get("ver_sw_release")
    if isinstance(release, int) and release:
        return f"PX4 {release >> 24}.{(release >> 16) & 0xFF}.{(release >> 8) & 0xFF}"
    return None


def _col(arr: np.ndarray, *names: str) -> np.ndarray | None:
    for name in names:
        if name in arr.dtype.names:
            return arr[name]
    return None


def _gps(log) -> dict | None:
    """The GPS messages with a time: of ``vehicle_gps_position`` (the receiver PX4 uses; ``sensor_gps`` without it),
    ``boot`` (s), ``utc_us``, ``gps`` (GPS time, s), ``lat``, ``lon``, ``msl``, ``ell`` (m, ell NaN when not given),
    ``fix``, ``eph``, ``epv``, ``topic``. Both layouts: ``latitude_deg``/``altitude_msl_m`` (1.14 on) and ``lat``
    (1e-7 deg)/``alt`` (mm)."""
    for topic in ("vehicle_gps_position", "sensor_gps"):
        for multi in log.instances(topic):
            arr = log.get(topic, multi)
            if arr is None or not len(arr):
                continue
            utc = _col(arr, "time_utc_usec")
            if utc is None:
                continue
            if "latitude_deg" in arr.dtype.names:
                lat, lon = arr["latitude_deg"].astype(float), arr["longitude_deg"].astype(float)
                msl = arr["altitude_msl_m"].astype(float)
                ell = _col(arr, "altitude_ellipsoid_m")
                ell = ell.astype(float) if ell is not None else np.full(len(arr), np.nan)
            else:
                lat, lon = arr["lat"] * 1e-7, arr["lon"] * 1e-7
                msl = arr["alt"] * 1e-3
                ell = _col(arr, "alt_ellipsoid")
                ell = ell * 1e-3 if ell is not None else np.full(len(arr), np.nan)
            fix = arr["fix_type"].astype(int)
            utc = utc.astype(np.float64)
            ok = utc > 0
            gps = np.full(len(arr), np.nan)
            if ok.any():
                leap = _leap(float(utc[ok][0]))
                if _leap(float(utc[ok][-1])) != leap:
                    leap = np.array([_leap(float(u)) if u > 0 else 0 for u in utc])
                gps[ok] = utc[ok] / 1e6 - _UNIX_TO_GPS + (leap[ok] if isinstance(leap, np.ndarray) else leap)
            device = _col(arr, "device_id")
            bus = _BUSES.get(int(device[0]) & 0x7) if device is not None and len(device) else None
            return {"topic": topic, "instance": multi, "bus": bus, "boot": arr["timestamp"].astype(np.float64) / 1e6,
                    "utc_us": utc, "gps": gps, "lat": lat, "lon": lon, "msl": msl, "ell": ell, "fix": fix,
                    "eph": arr["eph"].astype(float), "epv": arr["epv"].astype(float)}
    return None


def _clock(gps: dict) -> dict | None:
    """The clock fitted to the GPS messages with a time and a fix (2D or better; any with a time when fewer than 2):
    envelope when their UTC times are the epochs of the fixes (9 in 10 on the grid of their period within
    :data:`GRID_S`, the grid placed by all of them: :func:`_grid_phase`), median
    when the receiver stamps its messages off any grid."""
    timed = np.isfinite(gps["gps"])
    sel = timed & (gps["fix"] >= 2)
    if np.count_nonzero(sel) < 2:
        sel = timed
    if np.count_nonzero(sel) < 2:
        return None
    g, a = gps["gps"][sel], gps["boot"][sel]
    period = float(np.median(np.diff(np.unique(np.round(g, 6))))) if len(np.unique(g)) > 1 else 0.0
    grid = False
    if period > 0:
        grid = bool(np.mean(np.abs(_grid_phase(g, period)) <= GRID_S) >= 0.9)
    clock = fit_clock(a, g, "gps_fix" if grid else "gps_stamp", grid)
    clock.update(instance=gps["instance"], topic=gps["topic"], period_ms=round(period * 1000.0, 1), bus=gps["bus"])
    return clock


def _grid_phase(g: np.ndarray, period: float) -> np.ndarray:
    """How far each time of ``g`` is from a grid of ``period`` (s, within half a period), the grid placed by all the
    times - the direction of their mean on the circle of the period, then the median of the phases around it -, so
    that a few times off it (the first among them) do not move it."""
    angle = 2.0 * np.pi * np.mod(g - g[0], period) / period
    centre = float(np.angle(np.mean(np.exp(1j * angle)))) * period / (2.0 * np.pi)
    phase = np.mod(g - g[0] - centre + period / 2.0, period) - period / 2.0
    centre += float(np.median(phase))
    return np.mod(g - g[0] - centre + period / 2.0, period) - period / 2.0


def _instances(log, topic: str) -> list[tuple[int, np.ndarray]]:
    """The instances of ``topic`` with data: ``(multi_id, rows)``."""
    out = []
    for multi in log.instances(topic):
        arr = log.get(topic, multi)
        if arr is not None and len(arr):
            out.append((multi, arr))
    return out


def _pick(instances: list[tuple[int, np.ndarray]], name: str, confirmed) -> tuple[int, np.ndarray, str | None]:
    """The instance of a camera topic the shots are taken from, and what to say when there were several: the one
    with the most confirmed shots (``confirmed(rows)``: messages of the camera's feedback), then the most messages,
    then the lowest ``multi_id``. Only one is used - several instances are copies of the same shots (commands and
    their feedback), never added up, so no shot is counted twice."""
    counts = [(multi, arr, confirmed(arr)) for multi, arr in instances]
    multi, arr, n = max(counts, key=lambda c: (c[2], len(c[1]), -c[0]))
    if len(counts) == 1:
        return multi, arr, None
    listed = ", ".join(f"{m}: {len(a)} message{_plural(len(a))}, {c} with feedback" for m, a, c in counts)
    why = "the most with the camera's feedback" if n else "the most messages, none with feedback"
    return multi, arr, (f"{name} has {len(counts)} instances ({listed}): instance {multi} used ({why}); the others "
                        "left out, a shot is never taken twice (source['instance'])")


def _nearest(times: np.ndarray, at: float, within: float) -> int | None:
    if not len(times):
        return None
    k = int(np.searchsorted(times, at))
    best = min((j for j in (k - 1, k) if 0 <= j < len(times)), key=lambda j: abs(times[j] - at))
    return best if abs(times[best] - at) <= within else None


def _interp_position(gpos: np.ndarray | None, at_us: float) -> tuple[float, float, float] | None:
    """The position of ``vehicle_global_position`` at the boot time ``at_us`` (us), interpolated (AMSL altitude)."""
    if gpos is None or len(gpos) < 2:
        return None
    t = gpos["timestamp"].astype(np.float64)
    if not t[0] <= at_us <= t[-1]:
        return None
    lat, lon = gpos["lat"].astype(np.float64), gpos["lon"].astype(np.float64)
    alt = gpos["alt"].astype(np.float64)
    return float(np.interp(at_us, t, lat)), float(np.interp(at_us, t, lon)), float(np.interp(at_us, t, alt))


def px4(path: str | Path, topic: str = "auto") -> ShotSet:
    """The camera shots of a PX4 log: with ``topic`` "auto" the ``camera_capture`` messages with ``result`` 1 (the
    others left out and counted), or the ``camera_trigger`` messages when there are no captures (the commands to
    shoot unless ``feedback``: a warning); ``"capture"`` or ``"trigger"`` choose one. Of a topic with several instances (``multi_id``) one is used, the one
    with the most messages of the camera's feedback (:func:`_pick`; said, ``source["instance"]``) - a capture's
    feedback is that of the trigger at its time. A log with neither is a
    ``ValueError`` that says so (and names the topics the log's info says it left out).

    Each shot: ``index`` the ``seq`` of the message, ``gps_time`` (see the module; ``source["time_base"]`` one of
    ``"gps_fix"``, ``"gps_stamp"``, ``"timestamp_utc"``), ``antenna`` ``(lat, lon, h)`` with ``height``
    ``"ellipsoidal"`` or ``"msl"``, ``attitude`` from ``q``, ``status`` and ``std`` from the nearest GPS message.
    ``valid`` is False for a shot with no time (no GPS clock around it and no ``timestamp_utc``). ``source`` keeps
    ``file``, ``kind``, ``topic``, ``instance``, ``row``, ``seq``, ``time_us``, ``timestamp_utc``, ``time_base``, ``clock_topic``,
    ``fixes_above``, ``clock_ppm``, ``utc_minus_clock_ms``, ``feedback`` (None when unknown), ``firmware``,
    ``msl_alt``, ``geoid_sep`` (the separation used), ``ground_distance``, ``gps_status``, ``gps_alt``."""
    path = Path(path)
    if topic not in ("auto", *TOPICS):
        raise ValueError(f"topic must be auto, capture or trigger, not {topic!r}")
    log = read_ulog(path, topics=_READ)
    warnings = list(log.warnings)
    all_captures = _instances(log, "camera_capture")
    all_triggers = _instances(log, "camera_trigger")
    feedback_at = {}  # boot time of a trigger -> feedback (camera_feedback gives a capture the time of its trigger)
    for _multi, arr in all_triggers:
        for stamp, fed in zip(arr["timestamp"].tolist(), arr["feedback"].tolist()):
            feedback_at[int(stamp)] = feedback_at.get(int(stamp), False) or bool(fed)
    seq_feedback = {}  # with one instance of triggers: seq -> feedback, for captures not at a trigger's time
    if len(all_triggers) == 1:
        for row in all_triggers[0][1]:
            seq_feedback.setdefault(int(row["seq"]), bool(row["feedback"]))

    def fed(row) -> bool | None:
        stamp = int(row["timestamp"])
        return feedback_at[stamp] if stamp in feedback_at else seq_feedback.get(int(row["seq"]))

    if topic == "auto":
        topic = "capture" if all_captures else "trigger"
    candidates = all_captures if topic == "capture" else all_triggers
    rows = None
    if candidates:
        instance, rows, instance_note = _pick(candidates, TOPICS[topic],
                                              (lambda arr: sum(fed(r) is True for r in arr)) if topic == "capture"
                                              else (lambda arr: int(np.count_nonzero(arr["feedback"]))))
    if rows is None or not len(rows):
        left_out = str(log.info.get("excluded_optional_topics", "")) + "".join(
            map(str, log.multi_info.get("excluded_optional_topics", [])))
        why = " (the log's info lists them among the topics it left out)" if "camera_" in left_out else ""
        raise ValueError(f"{path.name}: the log has no {TOPICS[topic]} messages{why}: no camera was set up to log "
                         "its shots")
    firmware = _firmware(log)
    gps = _gps(log)
    clock = _clock(gps) if gps is not None else None
    gpos = log.get("vehicle_global_position")
    kind = "px4_capture" if topic == "capture" else "px4_trigger"
    separated = False  # the receiver gives a geoid: its ellipsoidal and AMSL heights differ
    if gps is not None:
        both = np.isfinite(gps["ell"]) & (gps["fix"] >= 2) & (gps["ell"] != 0)
        separated = bool(both.any() and np.any(np.abs(gps["ell"][both] - gps["msl"][both]) > SAME_HEIGHT_M))
    shots: list[Shot] = []
    failed = 0
    no_time = 0
    diffs = []
    for k, row in enumerate(rows):
        if topic == "capture" and "result" in rows.dtype.names and int(row["result"]) != 1:
            failed += 1
            continue
        seq = int(row["seq"])
        boot_us = int(row["timestamp"])
        utc_us = int(row["timestamp_utc"])
        feedback = bool(row["feedback"]) if topic == "trigger" else fed(row)
        source = {"file": str(path), "kind": kind, "topic": TOPICS[topic], "instance": instance, "row": k, "seq": seq,
                  "time_us": boot_us,
                  "timestamp_utc": utc_us, "feedback": feedback, "firmware": firmware}
        utc_gps = utc_us / 1e6 - _UNIX_TO_GPS + _leap(utc_us) if utc_us > 0 else None
        t, base, valid = None, None, True
        if clock is not None:
            mapped, why, _age, _period, above, rate = _to_gps(clock, boot_us / 1e6)
            if mapped is not None:
                t, base = mapped, clock["base"]
                source.update(clock_topic=clock["topic"], clock_instance=clock["instance"], clock_bus=clock["bus"])
                if above is not None:
                    source["fixes_above"] = above
                if rate is not None:
                    source["clock_ppm"] = round(rate, 1)
                if utc_gps is not None:
                    source["utc_minus_clock_ms"] = round((utc_gps - t) * 1000.0, 3)
                    diffs.append(utc_gps - t)
            else:
                source["no_gps_time"] = why
        if t is None and utc_gps is not None:
            t, base = utc_gps, "timestamp_utc"
        if t is None:
            no_time += 1
            valid, t, base = False, 0.0, "none"
        source["time_base"] = base
        # position
        if topic == "capture":
            lat, lon, msl = float(row["lat"]), float(row["lon"]), float(row["alt"])
            position = (lat, lon, msl) if all(map(math.isfinite, (lat, lon, msl))) and (lat or lon) else None
            source["ground_distance"] = float(row["ground_distance"])
        else:
            position = _interp_position(gpos, boot_us)
        attitude = None
        if topic == "capture":
            q = [float(v) for v in row["q"]]
            if all(map(math.isfinite, q)) and any(q):
                attitude = quaternion_to_euler(*q)
        status = std = None
        height = None
        antenna = None
        if gps is not None:
            j = _nearest(gps["boot"], boot_us / 1e6, NEAREST_GPS_S)
            if j is not None:
                status = GPS_STATUS.get(int(gps["fix"][j]), "single")
                source["gps_status"] = int(gps["fix"][j])
                source["gps_alt"] = float(gps["msl"][j])
                std = (float(gps["eph"][j]), float(gps["eph"][j]), float(gps["epv"][j]))
                if not all(map(math.isfinite, std)):
                    std = None
        if position is not None:
            lat, lon, msl = position
            source["msl_alt"] = msl
            sep = None
            if gps is not None and separated:
                ok = np.flatnonzero(np.isfinite(gps["ell"]) & (gps["fix"] >= 2) & (gps["ell"] != 0))
                if len(ok):
                    j = ok[np.argmin(np.abs(gps["boot"][ok] - boot_us / 1e6))]
                    sep = float(gps["ell"][j] - gps["msl"][j])
            if sep is not None:
                antenna, height = (lat, lon, msl + sep), "ellipsoidal"
                source["geoid_sep"] = round(sep, 4)
            else:
                antenna, height = (lat, lon, msl), "msl"
        week = int(t // _WEEK)
        source["gps_week"], source["tow"] = week, round(t - week * _WEEK, 6)
        shots.append(Shot(index=seq, gps_time=t, antenna=antenna, height=height, std=std, status=status,
                          attitude=attitude, valid=valid, source=source))
    shots.sort(key=lambda s: s.gps_time)
    name = TOPICS[topic]
    if instance_note:
        warnings.append(instance_note)
    if topic == "trigger":
        if not any(s.source["feedback"] for s in shots):
            warnings.append("camera_trigger messages without feedback are the commands to shoot, not the shots: the "
                            "camera may expose later or not at all (a feedback pin gives the confirmed shots)")
        if not all_captures:
            warnings.append("the shots have the position of the autopilot's estimate (vehicle_global_position) "
                            "interpolated at their times" if gpos is not None else "the log has no positions for the "
                            "triggers (no vehicle_global_position)")
    elif shots and all(s.source["feedback"] is False for s in shots):
        warnings.append("the camera_capture messages copy triggers without feedback: their times are the commands to "
                        "shoot, not the exposures (a camera feedback pin and CAM_CAP_FBACK give the confirmed shots)")
    if failed:
        warnings.append(f"{failed} camera_capture message{_plural(failed)} with a result other than 1 (a failed "
                        "capture) left out")
    if no_time:
        warnings.append(f"{no_time} {name} shot{_plural(no_time)} with no time (no GPS clock around and no "
                        "timestamp_utc): not valid, never paired")
    bases = {s.source["time_base"] for s in shots if s.valid}
    if clock is None:
        warnings.append("the log has no GPS messages with a time: the shot times are timestamp_utc, the clock of the "
                        "autopilot (set from GPS time now and then, or from the PPS of the receiver), of an accuracy "
                        "the log does not give")
    elif "timestamp_utc" in bases:
        outside = [s for s in shots if s.source["time_base"] == "timestamp_utc"]
        warnings.append(f"{len(outside)} shot{_plural(len(outside))} outside the GPS messages "
                        f"({outside[0].source.get('no_gps_time', '')}): their times are timestamp_utc, of an accuracy "
                        "the log does not give")
    if clock is not None and clock["base"] == "gps_fix":
        warnings.append(f"the GPS messages ({clock['topic']}) carry the time of their fix and the boot time they were "
                        "processed: the clock is fitted to the least delayed of them; every shot time is early by the "
                        "latency of the receiver (tens of ms on a u-blox), not removed")
    if clock is not None and clock["envelope"]:
        slow = [s.source["clock_ppm"] for s in shots if s.valid and s.source["time_base"] == clock["base"]
                and s.source.get("clock_ppm") is not None and abs(s.source["clock_ppm"]) < SLOW_PPM]
        if slow:
            nearest = min(slow, key=abs)
            warnings.append(f"{len(slow)} shot{_plural(len(slow))} timed on a clock that runs {abs(nearest):.1f} ppm "
                            f"from GPS time (under {SLOW_PPM:g} ppm, GPS messages every {clock['step_ms']} ms): the "
                            "delays of processing drift too slowly to be all sampled, the clock may not reach the "
                            "least delayed messages and those shot times can be early - by up to 20 ms on "
                            "ArduPilot's 50 Hz loop, not measured on PX4 (source['clock_ppm'])")
    if clock is not None and clock["base"] == "gps_stamp":
        bus = f", {clock['bus']}" if clock.get("bus") else ""
        warnings.append(f"the GPS messages ({clock['topic']}{bus}) carry a UTC time of the receiver's own, off the "
                        "grid of the fixes: the clock is fitted to those times as given; how they stand to the epochs "
                        "of the fixes depends on the receiver and is not in the log")
    if clock is not None and clock["glitches"]:
        warnings.append(f"{clock['glitches']} GPS message{_plural(clock['glitches'])} with a time off from the others "
                        "left out of the clock")
    if diffs:
        median = float(np.median(diffs))
        leap = rinex.gps_minus_utc(datetime(1980, 1, 6) + timedelta(seconds=float(np.nanmedian(gps["gps"]))))
        if abs(abs(median) - leap) <= 1.0:
            warnings.append(f"timestamp_utc of the {name} messages is {median:+.3f} s from the GPS clock: it was GPS "
                            "time already, not UTC (the times come from the GPS clock)")
        elif abs(median) > UTC_CHECK_S:
            warnings.append(f"timestamp_utc of the {name} messages is {median * 1000:+.0f} ms from the clock fitted to "
                            "the GPS messages: the clock of the autopilot was off (the times come from the GPS clock; "
                            "source['utc_minus_clock_ms'])")
    gimbal = log.get("gimbal_device_attitude_status")
    if topic == "capture" and gimbal is not None and len(gimbal):
        warnings.append("the log has a gimbal: the attitude of a capture is the gimbal's when it reported one, not the "
                        "vehicle's - a lever arm in the body of the drone turned with it is wrong")
    heights = {s.height for s in shots if s.antenna is not None}
    if "msl" in heights:
        why = ("give the same ellipsoidal and AMSL heights (the receiver reports no geoid)" if gps is not None and
               np.isfinite(gps["ell"]).any() and not separated else "give no ellipsoidal height")
        warnings.append(f"the GPS messages {why}: the heights of the shots are above the sea level as the autopilot "
                        "estimates it, marked 'msl'")
    return ShotSet(shots=shots, kind=kind, name=path.name, warnings=warnings, path=str(path))
