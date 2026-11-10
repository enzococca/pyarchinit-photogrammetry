"""The camera shots of an autopilot log: the CAM and TRIG messages of an ArduPilot DataFlash log (read by
:mod:`.dataflash`), with the GPS time, the position above the sea level and the attitude of each shot and the status
and accuracy of the GPS at that time.

The time of a shot (ArduPilot source of 3.5, 3.6, 4.0, 4.5 and master read for this). ``GPSWeek``/``GPSTime`` of a
CAM or TRIG are ``gps.time_week()``/``gps.time_week_ms()`` of the primary receiver: the GPS time of its LAST FIX, not
of the shot (up to one GPS period early, more in an outage). ``TimeUS`` is the boot clock of the autopilot: from 4.0 on
the time the interrupt of the feedback pin saw the flash (for a CAM; for a TRIG, or without a pin, the time the message
is written); before 4.0 always the time the message is written at the next 50 Hz loop, 0-20 ms after the flash - a
delay that cannot be removed (a warning names the firmware, read from the MSG messages into ``source["firmware"]``).

So the time of a shot is its ``TimeUS`` put in GPS time with a clock fitted to the fixes of one receiver: each GPS
message (``GWk``, ``GMS``: the GPS time of a fix; ``Status``) has a GPA message with the same ``TimeUS`` and instance,
whose ``SMS`` is the boot time (ms) the autopilot gives the fix. What that time is depends on the firmware and on the
driver: in 4.5 and master the arrival of the fix on the serial port corrected for its jitter
(``last_corrected_gps_time_us``, in master the PPS edge when there is one) - time base ``"gpa_sms"``; before 4.0, and
from 4.0 for drivers without corrected timestamps (CAN/DroneCAN/UAVCAN, MAVLink receivers), the time the fix was
processed, 0 to tens of ms after its arrival - time base ``"gpa_sms_uncorrected"``. The two are told apart by what
the data shows, not by the firmware: ``TimeUS`` - ``SMS`` under :data:`UNCORRECTED_US` on 90 % of the fixes means SMS
was taken within 2 ms of writing the message (a processing time); a corrected arrival is tens of ms earlier than the
writing (real logs: 0.1-1.0 ms against 94-97 ms; the median is kept in ``source["sms_lag_ms"]``). A driver that
corrected its times and wrote them at once would be taken for processing times: its shot times would be late by
about 1.6 times the noise of SMS. Without GPA (or SMS) the ``TimeUS`` of the GPS messages stands for it
(``"gps_timeus"``), later still, fitted as processing times.

The clock: GPS time = boot time + offset. A stretch of the fixes of a receiver at one level of time ends at a step
(the offset jumps by more than :data:`STEP_S`, beyond the rate of the clock); a stretch that does not hold for
:data:`STEP_MIN_FIXES` fixes and :data:`STEP_MIN_S` is a glitch of the receiver's time (bad fixes), left out and said;
where the log ends (or starts) inside such a stretch a glitch cannot be told from a step, and the shots it would time
(from halfway between it and the last fix that holds) have no GPS time on that receiver: not valid, said.
A step during an outage smaller than :data:`STEP_S` plus 1 % of the outage is not seen: the clock is assumed to run
on across it. In each stretch the rate of the clock (thousands of ppm off on some boards) is the slope of a robust
line through all its fixes, and the offset is found at knots every :data:`KNOT_S` and interpolated between them:
with corrected times the median of the offsets around each knot (:data:`FIT_WINDOWS_S`); with processing times their
envelope over :data:`ENVELOPE_WINDOWS_S` (a full window, shifted at the ends of a stretch). Processing delays are
one-sided - a fix is never processed before it arrives - and a sawtooth: the fix waits for the next tick of a 50 Hz
loop, a wait that drifts with the rate of the clock, so the offsets lie in branches 20 ms (:data:`LOOP_S`) apart, the
highest being the fixes that caught the tick (no delay). The envelope (:func:`_envelope_level`) leaves out the fixes
more than :data:`ENVELOPE_SPREAD_S` above the others (bad fixes), takes the level under all but 2 % of the rest (at
least 3 fixes above it) and rises to the branch one loop period above only on the evidence a few bad fixes cannot
give: at least :data:`BRANCH_SHARE` of the fixes (and 3), spread over :data:`BRANCH_SPREAD` of the window, above
delays that leave a band of the loop period empty (:data:`BRANCH_GAP_S`; delays that fill it already hold the
fixes that caught the tick). Bad fixes 10-40 ms high are so not taken for a branch in dense logs (synthetic, 1 and
5 Hz, 3 or 6 of them, together or 40 s apart: the shots as without them); in a sparse log (a fix every 10 s) 4 of
them in a window make the level, the shots late by some 10 ms, and the share of fixes above the clock says so.

What it cannot see: when the clock of the autopilot runs within :data:`SLOW_PPM` of GPS time the wait for the loop
changes so slowly that a window may hold no fix that caught the tick: the clock then sits on a lower branch and the
shot times are early by up to 20 ms, by an amount that VARIES along the flight (synthetic, deterministic sawtooth at
1-10 Hz over 30 min: 5 ppm -20 to -11 ms, 20 ppm -10 to +1 ms, 30 ppm -3.6 ms, 35 ppm -2.2 ms, 50 ppm and more within
1.5 ms). The fixes above the clock cannot show it (they are at most 3 % near a shot there), so the rate of each
stretch is kept in ``source["clock_ppm"]`` and a slower clock is a warning, with how early the times can be: up to
20 ms when the fixes come at a multiple of :data:`LOOP_S` (1, 5, 10 Hz), less when their period steps through the
loop (4 Hz: 10 ms, 8 Hz: 5 ms). The rate is the slope
through all the fixes of a stretch: a stretch holding few periods of the sawtooth (20 ms over the rate's offset from
GPS time: 67 s at 300 ppm) gives it a little off, the times by some ms (80 s at 300 ppm: -5 to +0.6 ms).
``source["fixes_above"]`` is the share of the fixes within :data:`ABOVE_WINDOW_S` of a shot more than
:data:`ABOVE_S` above its clock (fixes processed earlier than the clock says possible: a branch the envelope missed,
or bad fixes), warned of over :data:`ABOVE_WARNING`. With too few fixes in a stretch the offsets of up to
:data:`NEIGHBOURS` fixes each side are interpolated (their medians). Fixes before the receiver had a 3D fix
(``Status`` < 3; < 2 when that leaves too few) are not used.

Which receiver: the clock of each physical receiver (``I`` 0, 1) is fitted from ALL its fixes, whether in use or not;
a shot takes the receiver that was in use most (``U``), or another one when that one has no fixes around the shot;
two receivers are never mixed in one clock (their latencies differ). The blended solution ArduPilot 4.5 logs as
instance 2 (``U`` 1, its times weighted averages of the two) is the last choice. ``U`` chooses the status and accuracy
only. Logs of 3.x and 4.0 write a second receiver as GPS2/GPA2, not read: when the GPS messages are not in use near a
shot the status comes from them with a warning.

``GPSTime`` remains a check: the shot must come after the fix it names by the time since the last fix that arrived
before it, give or take one GPS period and :data:`CHECK_MARGIN_S`; outside that (a wrong week, a bad fix) the shot is
not valid, with a warning.

What is not removed: the latency of the receiver from the epoch of a fix to its arrival (tens of ms on a u-blox;
``GPS_DELAY_MS`` is ArduPilot's own estimate of it, not logged with the fixes) makes every time early by that constant
- :func:`.shots.pair` and ``apply_shots`` take it as ``latency_s`` - and the delay of the camera from the flash shoe to
the middle of the exposure likewise. On synthetic logs (see the tests): corrected SMS within 1.5 ms of the true time
at 1-10 Hz, 50 and 3000 ppm clocks and outages; processing times at a 50 Hz loop within 3 ms when the clock is 50 ppm
or more from GPS time, early by up to 20 ms, varying, nearer to it; a glitch of one fix (0.25-1 s) or a few bad fixes
in sparse logs move no shot by more than 3.5 ms (3 fixes 30 ms high: 4 ms); 3.x logs that plus the 0-20 ms of the
CAM writing. A real log with CAM and corrected SMS was not available."""
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
SHOT_MESSAGES = ("CAM", "TRIG", "GPS", "GPA", "PARM", "MSG")  # what the shots need of a log
TIME_BASES = ("gpa_sms", "gpa_sms_uncorrected", "gps_timeus", "cam_gpstime")  # how the shot times were found
NEAREST_GPS_S = 1.0  # status and accuracy of a shot from the GPS and GPA messages this close to it
KNOT_S = 15.0  # the offset of a clock is found at knots this far apart and interpolated between them
FIT_WINDOWS_S = (20.0, 60.0, 180.0)  # corrected times: the fixes this close to a knot, the first window that holds ...
MIN_FIT = 10  # ... this many fixes; with fewer, the medians of the NEIGHBOURS fixes on each side are interpolated
NEIGHBOURS = 5
ENVELOPE_WINDOWS_S = (300.0, 600.0)  # processing times: the envelope of the fixes this close to a knot (a window of
# twice this, shifted, not cut, at the ends of a stretch), the first that holds MIN_FIT fixes ...
MIN_ENVELOPE = 5  # the envelope from this many fixes (sparse logs: one fix a minute)
ENVELOPE_TOP = 0.02  # ... the level under all but this share of them (at least ENVELOPE_LEAST) ...
ENVELOPE_LEAST = 3
BRANCH_SHARE = 0.01  # a branch one loop period above the level: at least this share of the fixes (and
# ENVELOPE_LEAST) ...
BRANCH_SPREAD = 0.25  # ... spread over this share of the window (a few bad fixes are not a branch) ...
BRANCH_GAP_S = 0.005  # ... and the offsets in the loop period under the level leave a band this wide empty
ENVELOPE_SPREAD_S = 0.040  # ... after those more than this above their median are left out (bad fixes: legitimate
# delays of the processing span 20 ms at 50 Hz, up to some 40 ms with CAN receivers)
LOOP_S = 0.020  # the period of the 50 Hz loop that processes the fixes: the branches of the sawtooth of delays
SLOW_PPM = 35.0  # processing times: a clock this near GPS time (ppm) drifts too slowly through the loop period for
# every window to hold a fix that caught the tick (synthetic, 1-10 Hz: 35 ppm within 2.2 ms, 30 ppm 3.6, 20 ppm 10)
ABOVE_S = 0.005  # a fix this far above the clock was processed earlier than the clock says possible ...
ABOVE_WINDOW_S = 30.0  # ... counted among the fixes this close to a shot (source["fixes_above"]; not those beyond
# ENVELOPE_SPREAD_S, bad fixes) ...
ABOVE_WARNING = 0.05  # ... and more than this share of them is a warning
UNCORRECTED_US = 2000  # TimeUS - SMS under this on most fixes: SMS was taken within 2 ms of writing the message
STEP_S = 0.2  # a jump of the offset (beyond the rate of the clock) this big between two fixes: a step of time ...
STEP_RATE = 0.01  # ... (the rate of the clock may differ from GPS time by up to 1 % between fixes) ...
STEP_MIN_FIXES = 3  # ... that holds for at least this many fixes and ...
STEP_MIN_S = 2.0  # ... this long; a shorter stretch at another level is bad fixes (a glitch), left out
MAX_GAP_S = 120.0  # a shot between two fixes further apart than this (an outage) has no GPS time
MAX_EXTRAPOLATION_S = 10.0  # nor one further than this before the first fix or after the last
CHECK_MARGIN_S = 0.1  # the GPSTime of a shot and its time disagree by more than this: not valid
BLENDED = 2  # the instance of the blended solution of two receivers in ArduPilot 4.5
_WEEK = 604800
_ANTENNA_OFFSET = re.compile(r"^GPS(_POS\d_|\d_POS_)[XYZ]$")  # where the antenna is in the body of the drone
_FIRMWARE = re.compile(r"\b((?:Ardu[A-Za-z]+|APM:[A-Za-z]+)\s+V?(\d+)\.(\d+)(?:\.(\d+))?)")


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


def _firmware(df: DataFlash) -> tuple[str | None, tuple[int, int] | None]:
    """The firmware the MSG messages name ("ArduPlane V3.6.0") and its major and minor version."""
    if not df.count("MSG"):
        return None, None
    msg = df.messages("MSG", multipliers=False)
    if "Message" not in msg.dtype.names:
        return None, None
    for text in msg["Message"][:200]:
        m = _FIRMWARE.search(str(text))
        if m:
            return m.group(1), (int(m.group(2)), int(m.group(3)))
    return None, None


# --- the receivers: status, accuracy and clocks ------------------------------------------------------------------


def _gps(df: DataFlash) -> dict | None:
    """The GPS messages sorted by ``TimeUS``: ``boot_us``, ``instance``, ``used`` (``U``; without it instance 0),
    ``status``, ``week``, ``ms``."""
    if not df.count("GPS"):
        return None
    gps = df.messages("GPS", multipliers=False)
    n = len(gps)
    if "TimeUS" not in gps.dtype.names or "Status" not in gps.dtype.names:
        return None
    boot_us = _ints(gps["TimeUS"], n, -1)
    order = np.flatnonzero(boot_us >= 0)
    order = order[np.argsort(boot_us[order], kind="stable")]
    inst = _ints(_column(gps, "I"), n, 0)[order]
    use = _ints(_column(gps, "U"), n, 0)[order]
    if "U" not in gps.dtype.names or not np.any(use != 0):
        use = (inst == 0).astype(np.int64)
    return {"boot_us": boot_us[order], "boot": boot_us[order] * 1e-6, "instance": inst, "used": use != 0,
            "status": _ints(gps["Status"], n, -1)[order], "week": _ints(_column(gps, "GWk", "Week"), n, 0)[order],
            "ms": _ints(_column(gps, "GMS", "TimeMS"), n, -1)[order]}


def _sms(df: DataFlash, gps: dict) -> np.ndarray:
    """For each GPS message, the ``SMS`` (s) of the GPA written with it (same ``TimeUS`` and instance; a GPA without
    ``I`` belongs to instance 0); NaN without."""
    out = np.full(len(gps["boot_us"]), np.nan)
    if not df.count("GPA"):
        return out
    gpa = df.messages("GPA", multipliers=False)
    if "SMS" not in gpa.dtype.names or "TimeUS" not in gpa.dtype.names:
        return out
    n = len(gpa)
    found = {(int(i), int(t)): int(s) for t, i, s in
             zip(_ints(gpa["TimeUS"], n, -1), _ints(_column(gpa, "I"), n, 0), _ints(gpa["SMS"], n, 0)) if s > 0}
    for k, (t, i) in enumerate(zip(gps["boot_us"].tolist(), gps["instance"].tolist())):
        s = found.get((i, t))
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


def _clocks(gps: dict, sms: np.ndarray) -> list[dict]:
    """The clock of each receiver, in the order shots try them: ``instance``, ``A`` the boot times of its fixes (s,
    sorted), ``O`` GPS time less boot time, ``seg`` the stretch without a step of time of each fix, ``base``,
    ``envelope`` (the times are processing times: one-sided delays)."""
    clocks = []
    timed = (gps["week"] > 0) & (gps["ms"] >= 0) & (gps["ms"] < _WEEK * 1000)
    for inst in sorted(set(gps["instance"].tolist())):
        mine = gps["instance"] == inst
        for least in (3, 2, -1):  # fixes with a 3D fix; with fewer than two, 2D; else any with a time
            sel = mine & timed & (gps["status"] >= least)
            if np.count_nonzero(sel) >= 2:
                break
        if np.count_nonzero(sel) < 2:
            continue
        g = gps["week"] * float(_WEEK) + gps["ms"] / 1000.0
        lag_ms = None
        if np.count_nonzero(sel & np.isfinite(sms)) >= 2:
            sel &= np.isfinite(sms)
            lag = gps["boot_us"][sel] - np.round(sms[sel] * 1000.0) * 1000.0
            lag_ms = round(float(np.median(lag)) / 1000.0, 2)
            base = "gpa_sms_uncorrected" if np.mean(lag < UNCORRECTED_US) >= 0.9 else "gpa_sms"
            a = sms[sel]
        else:
            base, a = "gps_timeus", gps["boot"][sel]
        g = g[sel]
        order = np.lexsort((a, g))  # by GPS time, then arrival: a fix logged twice is kept once, at its first time
        a, g = a[order], g[order]
        first = np.ones(len(g), bool)
        first[1:] = g[1:] != g[:-1]
        a, g = a[first], g[first]
        order = np.argsort(a, kind="stable")
        a, o = a[order], g[order] - a[order]
        a, o, glitches, unsure = _without_glitches(a, o)
        seg = _segment_ids(a, o)
        envelope = base != "gpa_sms"
        step_ms = int(round(float(np.median(np.diff(a + o))) * 1000.0)) if len(a) > 1 else 0
        clock = {"instance": inst, "A": a, "O": o, "seg": seg, "base": base, "envelope": envelope, "step_ms": step_ms,
                 "used": int(np.count_nonzero(mine & gps["used"])), "fixes": len(a), "glitches": glitches,
                 "unsure": unsure,
                 "lag_ms": lag_ms, "models": [_segment_model(a[seg == k], o[seg == k], envelope)
                                              for k in range(int(seg[-1]) + 1)]}
        clocks.append(clock)
    physical = [c for c in clocks if c["instance"] != BLENDED] or clocks
    rest = [c for c in clocks if c not in physical]
    return (sorted(physical, key=lambda c: (-c["used"], -c["fixes"], c["instance"]))
            + sorted(rest, key=lambda c: (-c["used"], -c["fixes"])))


def _segment_ids(a: np.ndarray, o: np.ndarray) -> np.ndarray:
    """The stretch of each fix: a new one where the offset jumps by more than :data:`STEP_S` (beyond the rate of the
    clock) from the fix before."""
    if len(a) < 2:
        return np.zeros(len(a), dtype=np.int64)
    jump = np.abs(np.diff(o)) > STEP_S + STEP_RATE * np.diff(a)
    return np.concatenate([[0], np.cumsum(jump)])


def _without_glitches(a: np.ndarray, o: np.ndarray) -> tuple[np.ndarray, np.ndarray, int, list]:
    """The fixes without the bad ones: a stretch at another level (a step of time) that does not hold for
    :data:`STEP_MIN_FIXES` fixes and :data:`STEP_MIN_S` is a glitch of the receiver's time (or of SMS), not a step,
    when a stretch that holds exists; how many were left out; and where the log ends inside such a stretch - after the
    last stretch that holds, or before the first - the boot times (from, to, the jump in s, fixes) that cannot be told
    from a step: from halfway between the last fix that holds and the first left out (to halfway between the last left
    out and the first that holds)."""
    seg = _segment_ids(a, o)
    if not len(seg) or seg[-1] == 0:
        return a, o, 0, []
    starts = np.flatnonzero(np.diff(np.concatenate([[-1], seg])))
    ends = np.concatenate([starts[1:], [len(seg)]]) - 1
    persistent = ((ends - starts + 1) >= STEP_MIN_FIXES) & ((a[ends] - a[starts]) >= STEP_MIN_S)
    if not persistent.any() or persistent.all():
        return a, o, 0, []
    held = np.flatnonzero(persistent)
    unsure = []
    if held[-1] < len(starts) - 1:  # the log ends in a short stretch
        last, first = int(ends[held[-1]]), int(starts[held[-1] + 1])
        unsure.append(((a[last] + a[first]) / 2.0, math.inf, float(o[first] - o[last]), len(a) - first))
    if held[0] > 0:  # the log starts in one
        last, first = int(ends[held[0] - 1]), int(starts[held[0]])
        unsure.append((-math.inf, (a[last] + a[first]) / 2.0, float(o[last] - o[first]), first))
    keep = persistent[seg]
    return a[keep], o[keep], int(np.count_nonzero(~keep)), unsure


def _line(x: np.ndarray, y: np.ndarray) -> tuple[float, float] | None:
    """A straight line fitted to ``(x, y)`` by least squares, points beyond 3 MAD of it left out: ``(value at 0,
    slope)``; None when ``x`` gives no line."""
    if len(x) < 3 or np.ptp(x) <= 0:
        return None
    keep = np.ones(len(x), bool)
    design = np.column_stack([x, np.ones(len(x))])
    for _ in range(4):
        coef = np.linalg.lstsq(design[keep], y[keep], rcond=None)[0]
        r = y - design @ coef
        mid = np.median(r[keep])
        mad = 1.4826 * np.median(np.abs(r[keep] - mid)) + 1e-4
        new = np.abs(r - mid) <= 3.0 * mad
        if np.count_nonzero(new) < 3 or np.array_equal(new, keep):
            break
        keep = new
    return float(coef[1]), float(coef[0])


def _window(a: np.ndarray, knot: float, half: float) -> slice:
    """The fixes (``a`` sorted) within a window of ``2 * half`` around ``knot``, the window shifted - not cut - where
    the stretch ends nearer than ``half``."""
    first, last = float(a[0]), float(a[-1])
    center = (first + last) / 2.0 if last - first <= 2.0 * half else min(max(knot, first + half), last - half)
    return slice(int(np.searchsorted(a, center - half)), int(np.searchsorted(a, center + half, side="right")))


def _envelope_level(r: np.ndarray, t: np.ndarray) -> float:
    """The level of the least delayed of the offsets ``r`` of the fixes at the boot times ``t``: those more than
    :data:`ENVELOPE_SPREAD_S` above their median left out (bad fixes), the level under all but :data:`ENVELOPE_TOP`
    of the others (at least :data:`ENVELOPE_LEAST`), raised to the branch one loop period above it (:data:`LOOP_S`)
    when that branch is a branch of the sawtooth, not a few bad fixes: at least :data:`ENVELOPE_LEAST` fixes and
    :data:`BRANCH_SHARE` of them, spread over :data:`BRANCH_SPREAD` of the window - the fixes that caught the tick
    of the loop, the least delayed."""
    keep = r <= np.median(r) + ENVELOPE_SPREAD_S
    r, t = r[keep], t[keep]
    above = max(ENVELOPE_LEAST, int(math.ceil(ENVELOPE_TOP * len(r))))
    level = float(np.sort(r)[-above - 1]) if len(r) > above else float(np.min(r))
    least = max(ENVELOPE_LEAST, int(math.ceil(BRANCH_SHARE * len(r))))
    span = float(np.ptp(t)) if len(t) else 0.0
    for _ in range(2):
        on = (r > level + LOOP_S / 2.0) & (r <= level + 1.5 * LOOP_S)
        if np.count_nonzero(on) < least or float(np.ptp(t[on])) < BRANCH_SPREAD * span:
            break
        below = np.sort(r[(r >= level - LOOP_S) & (r <= level)])
        if float(np.max(np.diff(np.concatenate([[level - LOOP_S], below, [level]])))) < BRANCH_GAP_S:
            break  # the delays under the level fill the loop period: the least delayed are already there
        level = float(np.median(r[on]))
    return level


def _segment_model(a: np.ndarray, o: np.ndarray, envelope: bool) -> dict:
    """The offset of a stretch of a clock: its slope (the rate of the clock: a robust line through all its fixes) and
    the offset at knots every :data:`KNOT_S` (``K``, ``L``): with corrected times the median of the offsets around
    each knot, with processing times their envelope (:func:`_envelope_level`), both with the slope removed. No knots
    (``K`` empty) when the stretch has too few fixes."""
    model = {"start": float(a[0]), "end": float(a[-1]), "slope": 0.0, "K": np.zeros(0), "L": np.zeros(0)}
    least = MIN_ENVELOPE if envelope else MIN_FIT
    fit = _line(a - a.mean(), o) if len(a) >= least else None
    if fit is None:
        return model
    slope = fit[1]
    knots = np.arange(model["start"], model["end"] + KNOT_S, KNOT_S)
    knots[-1] = min(knots[-1], model["end"])
    kept_k, levels = [], []
    for knot in knots:
        for half in (ENVELOPE_WINDOWS_S if envelope else FIT_WINDOWS_S):
            sel = _window(a, knot, half)
            if sel.stop - sel.start >= least:
                r = o[sel] - slope * (a[sel] - knot)
                levels.append(_envelope_level(r, a[sel]) if envelope else float(np.median(r)))
                kept_k.append(knot)
                break
    model.update(slope=slope, K=np.array(kept_k), L=np.array(levels))
    return model


def _offset(model: dict, b: np.ndarray | float):
    """The offset of the stretch ``model`` at the boot times ``b``: interpolated between its knots, extended with
    its slope beyond them."""
    k, lv = model["K"], model["L"]
    b = np.asarray(b, dtype=np.float64)
    inside = np.interp(b, k, lv)
    return np.where(b < k[0], lv[0] + model["slope"] * (b - k[0]),
                    np.where(b > k[-1], lv[-1] + model["slope"] * (b - k[-1]), inside))


def _to_gps(clock: dict, b: float) -> tuple[float | None, str, float | None, float, float | None, float | None]:
    """The GPS time of the boot time ``b`` (s) on ``clock``: ``(time, why not, age, period, fixes above, rate)`` -
    age the time since the last fix before ``b`` (None before the first), period the time between that fix and the
    next, fixes above the share of the fixes within :data:`ABOVE_WINDOW_S` more than :data:`ABOVE_S` above the
    clock, rate the rate of the clock of that stretch against GPS time (ppm; None with too few fixes to fit one)."""
    a, o, seg = clock["A"], clock["O"], clock["seg"]
    for lo, hi, jump, count in clock.get("unsure", ()):
        if lo <= b <= hi:
            where = "last" if math.isinf(hi) else "first"
            return (None, f"the GPS time jumps by {jump:+.3f} s in the {where} {count} fix{'es' if count > 1 else ''} "
                    "of the log: a glitch or a step of time, the log too short there to tell", None, 0.0, None, None)
    k = int(np.searchsorted(a, b, side="right"))
    spacing = float(np.median(np.diff(a))) if len(a) > 1 else 0.0
    age = b - float(a[k - 1]) if k else None
    if 0 < k < len(a):
        period = float(a[k] - a[k - 1])
        if period > MAX_GAP_S:
            return None, f"in a gap of {period:.0f} s between GPS fixes", None, period, None, None
        near = k - 1 if seg[k - 1] == seg[k] or b - a[k - 1] <= a[k] - b else k
    else:
        period = spacing
        far = b - float(a[-1]) if k else float(a[0]) - b
        if far > MAX_EXTRAPOLATION_S:
            return (None, f"{far:.0f} s {'after the last' if k else 'before the first'} GPS fix", None, period, None,
                    None)
        near = k - 1 if k else 0
    model = clock["models"][int(seg[near])]
    if len(model["K"]):
        offset = float(_offset(model, b))
        lo, hi = np.searchsorted(a, b - ABOVE_WINDOW_S), np.searchsorted(a, b + ABOVE_WINDOW_S, side="right")
        mine = slice(lo, hi)
        same = seg[mine] == seg[near]
        r = o[mine][same] - _offset(model, a[mine][same])
        # bad fixes far above (ENVELOPE_SPREAD_S) are left out of the clock and of this count
        r = r[r <= ENVELOPE_SPREAD_S]
        above = round(float(np.mean(r > ABOVE_S)), 3) if len(r) else None
        return b + offset, "", age, period, above, model["slope"] * 1e6
    idx = np.flatnonzero(seg == seg[near])  # few fixes: the medians of the neighbours each side
    j = int(np.searchsorted(a[idx], b, side="right"))
    before, after = idx[max(0, j - NEIGHBOURS):j], idx[j:j + NEIGHBOURS]
    if len(before) and len(after):
        bb, ob = float(np.median(a[before])), float(np.median(o[before]))
        ba, oa = float(np.median(a[after])), float(np.median(o[after]))
        offset = ob + (oa - ob) * (b - bb) / (ba - bb) if ba > bb else ob
    else:
        offset = float(np.median(o[before if len(before) else after]))
    return b + offset, "", age, period, None, None


def _status_at(gps: dict | None, at: float, source: dict) -> tuple[str | None, int | None, bool]:
    """The status of the GPS message in use nearest to ``at``; without one, of the nearest of any receiver (the
    third value True)."""
    if gps is None:
        return None, None, False
    for rows, other in ((gps["used"], False), (np.ones(len(gps["used"]), bool), True)):
        idx = np.flatnonzero(rows)
        k = _nearest(gps["boot"][idx], at, NEAREST_GPS_S)
        if k is not None:
            k = idx[k]
            raw = int(gps["status"][k])
            source["gps_status"] = raw
            source["gps_instance"] = int(gps["instance"][k])
            return GPS_STATUS.get(raw), int(gps["instance"][k]), other
    return None, None, False


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
    old = [name for name in ("CAM", "TRIG", "GPS")
           if name in df.formats and "TimeUS" not in df.formats[name]["columns"]]
    if old and not (df.count("CAM") or df.count("TRIG")):
        raise ValueError(f"{df.path.name}: the log has no camera messages (CAM or TRIG), and is too old for the times "
                         f"of shots anyway: its formats of {', '.join(old)} have no TimeUS (an ArduPilot of about 2015 "
                         "or older)")
    if old:
        raise ValueError(f"{df.path.name}: a log too old for the times of its shots - its {' and '.join(old)} "
                         "messages have no TimeUS (an ArduPilot of about 2015 or older)")
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
    with ``TimeUS`` are read, 3.x to master: with or without ``I`` and ``Img``, attitude ``R P Y`` (centidegrees or
    float degrees) or ``Roll Pitch Yaw``; a log without ``TimeUS`` is a ``ValueError`` (too old).

    Each shot: ``index`` the image number ``Img`` (None in logs without it); ``gps_time`` its ``TimeUS`` in GPS time
    on the clock of a receiver (see the module), ``source["time_base"]`` saying how (:data:`TIME_BASES`;
    ``"cam_gpstime"``, the time of the last fix, only when the log has no fixes to fit a clock to, with a warning) and
    ``source["clock_instance"]`` on which receiver. Shots outside the fixes (:data:`MAX_GAP_S`,
    :data:`MAX_EXTRAPOLATION_S`) or whose ``GPSTime`` disagrees (:data:`CHECK_MARGIN_S`) are not valid, never paired,
    with a warning. Rows without a GPS time (``GPSWeek`` 0: no fix yet) or without a position are left out and
    counted.

    ``antenna`` is ``(Lat, Lng, Alt)`` with ``height="msl"``: the position the AHRS/EKF of the autopilot estimates when
    the message is written (a few ms after the pin), ``Alt`` its altitude above the sea level fused with the barometer
    - never ellipsoidal, and not the GPS height (``GPSAlt``, in ``source["gps_alt"]``); it is the position of the
    autopilot's origin, which is the antenna unless ``GPS_POS1_X/Y/Z`` place it elsewhere in the body (a warning when
    the log's parameters do). It matters only without PPK, which replaces it. ``attitude`` is ``(R, P, Y)`` in degrees
    (yaw 0-360 from north). ``status`` comes from ``Status`` of the GPS message in use (``U``) nearest in time
    (``TimeUS``) within :data:`NEAREST_GPS_S` (:data:`GPS_STATUS`; without one in use, of any receiver, with a
    warning), ``std`` (north, east, up) from ``HAcc``, ``HAcc``, ``VAcc`` of the GPA of the same receiver nearest
    within that time; None without them. ``source`` keeps the message, ``row``, ``instance``, ``img``, ``gps_week``
    and ``tow`` (s) of the shot time, ``time_us``, ``time_base``, ``clock_instance``, ``cam_gps_week`` and
    ``cam_tow_ms`` (what the message says: the last fix), ``firmware``, ``rel_alt``, ``gps_alt``, ``gps_status``,
    ``gps_instance`` and, from the clock, ``sms_lag_ms``, ``fixes_above`` and ``clock_ppm`` (the rate of the clock of
    the stretch against GPS time)."""
    path = Path(path)
    with read_dataflash(path, names=SHOT_MESSAGES) as df:
        name = _choose(df, message)
        rows = df.messages(name, multipliers=False)
        gps, acc = _gps(df), _accuracy(df)
        clocks = _clocks(gps, _sms(df, gps)) if gps is not None else []
        params = df.messages("PARM", multipliers=False) if df.count("PARM") else None
        firmware, version = _firmware(df)
        warnings = list(df.warnings)
    kind = "ardupilot_cam" if name == "CAM" else "ardupilot_trig"
    n = len(rows)
    missing = [c for c in ("TimeUS", "GPSWeek", "GPSTime", "Lat", "Lng", "Alt") if c not in rows.dtype.names]
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
    boot = _ints(rows["TimeUS"], n, -1)
    rel, galt = _column(rows, "RelAlt"), _column(rows, "GPSAlt")
    shots: list[Shot] = []
    no_fix = no_position = no_gps = other_gps = 0
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
                  "img": int(img[k]) if math.isfinite(img[k]) else None, "cam_gps_week": w, "cam_tow_ms": ms,
                  "firmware": firmware}
        if rel is not None:
            source["rel_alt"] = float(rel[k])
        if galt is not None:
            source["gps_alt"] = float(galt[k])
        attitude = tuple(float(a[k]) for a in att)
        attitude = attitude if all(map(math.isfinite, attitude)) else None
        t, valid, status, std, base = cam_t, True, None, None, "cam_gpstime"
        if boot[k] >= 0:
            at = int(boot[k]) * 1e-6
            source["time_us"] = int(boot[k])
            status, gps_instance, other = _status_at(gps, at, source)
            no_gps += "gps_status" not in source
            other_gps += other
            std = _std_at(acc, at, gps_instance)
            why = ""
            for clock in clocks:
                mapped, reason, age, period, above, rate = _to_gps(clock, at)
                if mapped is not None:
                    break
                why = why or reason
            if clocks and mapped is None:
                valid = False
                unmapped.append(why)
                source["no_gps_time"] = why
            elif clocks:
                t, base = mapped, clock["base"]
                source["clock_instance"] = clock["instance"]
                source["sms_lag_ms"] = clock["lag_ms"]
                if above is not None:
                    source["fixes_above"] = above
                if rate is not None:
                    source["clock_ppm"] = round(rate, 1)
                apart = t - cam_t
                low = (age - period if age is not None else -MAX_EXTRAPOLATION_S) - CHECK_MARGIN_S
                high = (age + period if age is not None else MAX_EXTRAPOLATION_S) + CHECK_MARGIN_S
                if not low <= apart <= high:
                    source["gps_time_disagrees_s"] = round(apart - (age or 0.0), 3)
                    valid = False
                    disagree.append(apart - (age or 0.0))
        source["time_base"] = base
        gps_week = int(t // _WEEK)
        source["gps_week"], source["tow"] = gps_week, round(t - gps_week * _WEEK, 6)
        shots.append(Shot(index=source["img"], gps_time=gps_week * float(_WEEK) + source["tow"],
                          antenna=(la, lo, h), height="msl", std=std, status=status, attitude=attitude,
                          valid=valid, source=source))
    shots.sort(key=lambda s: s.gps_time)
    warnings += _time_warnings(name, shots, firmware, version, clocks)
    if no_fix:
        warnings.append(f"{no_fix} {name} message{_plural(no_fix)} without a GPS time (GPSWeek 0: no GPS fix yet) "
                        "left out")
    if no_position:
        warnings.append(f"{no_position} {name} message{_plural(no_position)} without a position left out")
    if no_gps:
        warnings.append(f"{no_gps} {name} shot{_plural(no_gps)} with no GPS message within {NEAREST_GPS_S:g} s: no "
                        "status")
    if other_gps:
        warnings.append(f"{other_gps} {name} shot{_plural(other_gps)} with the status of a receiver not in use (the "
                        "one in use is logged as GPS2 or has no message near them)")
    if unmapped:
        warnings.append(f"{len(unmapped)} {name} shot{_plural(len(unmapped))} without a GPS time ({unmapped[0]}): not "
                        "valid, never paired")
    if disagree:
        worst = max(disagree, key=abs)
        warnings.append(f"{len(disagree)} {name} shot{_plural(len(disagree))} whose GPSTime disagrees with the GPS "
                        f"fixes by up to {worst:.3f} s (a wrong week, a bad fix): not valid, never paired")
    warnings += _antenna_offset(params)
    return ShotSet(shots=shots, kind=kind, name=path.name, warnings=warnings, path=str(path))


def _time_warnings(name: str, shots: list[Shot], firmware: str | None, version, clocks: list[dict]) -> list[str]:
    """What the times of the shots owe to the firmware and to the receivers."""
    out = []
    if name == "TRIG":
        out.append("TRIG messages are the commands to shoot, not the shots: the camera may expose later or not at all "
                   "(a feedback pin gives CAM messages, the confirmed shots)")
    if version is not None and version < (4, 0):
        out.append(f"{firmware} writes the camera messages at its next 50 Hz loop, 0-20 ms after the shot (the "
                   "feedback pin is timed from ArduPilot 4.0): every shot time can be late by up to 20 ms")
    bases = {s.source["time_base"] for s in shots}
    if any(s.valid and s.source["time_base"] == "cam_gpstime" for s in shots):
        out.append("the log has no GPS fixes to put the boot times of the shots in GPS time: the times are those of "
                   "the last fix before each shot (GPSTime), early by up to a GPS period")
    above = [s.source["fixes_above"] for s in shots if s.source.get("fixes_above") is not None]
    many = sum(a > ABOVE_WARNING for a in above)
    if many:
        out.append(f"{many} shot{_plural(many)} with more than {ABOVE_WARNING:.0%} of the GPS fixes around "
                   f"{'them' if many > 1 else 'it'} above the clock (up to {max(above):.0%}): a branch of the delays "
                   "of the fixes the clock does not follow, or bad fixes - those shot times can be early by up to "
                   "20 ms (source['fixes_above'])")
    if "gpa_sms_uncorrected" in bases:
        out.append("the GPS fixes carry the time the autopilot processed them, not their arrival (firmware before 4.0, "
                   "or a CAN or MAVLink receiver): the clock is fitted to the least delayed of them; when the clock "
                   f"of the autopilot runs within {SLOW_PPM:g} ppm of GPS time the shot times can be early by up to "
                   "20 ms, by an amount that varies along the flight")
    out += _slow_clock_warnings(shots, clocks)
    if "gps_timeus" in bases:
        out.append("the GPS fixes have no arrival time (no GPA with SMS): their times of writing stand for it, fitted "
                   "to the least delayed of them; the shot times can be early by up to 20 ms or more, varying along "
                   "the flight")
    glitches = sum(c["glitches"] for c in clocks)
    if glitches:
        out.append(f"{glitches} GPS fix{'es' if glitches > 1 else ''} with a time off by more than {STEP_S:g} s from "
                   "the fixes around (a glitch of the receiver's time, for less than "
                   f"{STEP_MIN_FIXES} fixes and {STEP_MIN_S:g} s) left out of the clock")
    receivers = sorted({s.source["clock_instance"] for s in shots if "clock_instance" in s.source})
    if len(receivers) > 1:
        out.append(f"the shot times come from the clocks of receivers {', '.join(map(str, receivers))} (one had no "
                   "fixes around some shots): their latencies can differ by tens of ms")
    return out


def _slow_clock_warnings(shots: list[Shot], clocks: list[dict]) -> list[str]:
    """One warning for each receiver whose clock, fitted to processing times, runs within :data:`SLOW_PPM` of GPS
    time where it times shots: the delays of the loop are then not all sampled. How early the times can be depends on
    how the period of the fixes falls on the loop: a period a multiple of :data:`LOOP_S` keeps each fix at one phase
    of the loop (up to 20 ms); others step through it by their greatest common divisor with it (250 ms: 10 ms;
    125 ms: 5 ms; 333 ms: 1 ms, the delays sampled whatever the rate)."""
    out = []
    loop_ms = int(round(LOOP_S * 1000.0))
    for clock in clocks:
        if not clock["envelope"]:
            continue
        rates = [s.source["clock_ppm"] for s in shots
                 if s.source.get("clock_instance") == clock["instance"] and s.source.get("clock_ppm") is not None
                 and s.source["time_base"] == clock["base"]]
        slow = [r for r in rates if abs(r) < SLOW_PPM]
        grain = math.gcd(clock["step_ms"], loop_ms) if clock["step_ms"] > 0 else loop_ms
        if not slow or grain < 2:
            continue
        nearest = min(slow, key=abs)
        out.append(f"{len(slow)} shot{_plural(len(slow))} timed on a clock of receiver {clock['instance']} that runs "
                   f"{abs(nearest):.1f} ppm from GPS time (under {SLOW_PPM:g} ppm, fixes every {clock['step_ms']} "
                   "ms): the delays of processing drift too slowly to be all sampled, those shot times can be early "
                   f"by up to {grain} ms, varying along the flight (source['clock_ppm'])")
    return out


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
