"""The external events of a receiver as shots: a cable from the flash shoe of the camera to the event pin of the
receiver makes it record the time of each shot. A RINEX observation file writes them as epochs of flag 5 (Emlid Reach
writes its "time marks" so, and convbin of RTKLIB from the TIM-TM2 messages of a u-blox log); their times, written to a
tenth of a microsecond in the time system of the file, become GPS time. A u-blox receiver logs them as UBX-TIM-TM2
messages (read here from the u-blox Interface Description, UBX-18010854, to the nanosecond), and RTKLIB writes the
positions it interpolates at them in ``<solution>_events.pos``."""
from __future__ import annotations

import mmap
import re
import struct
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

from . import ppk, rinex
from .shots import Shot, ShotSet

DOUBLE_EVENT_S = 0.005  # two events closer than this are one shot recorded twice (a bouncing contact)

_GPS_START = date(1980, 1, 6)
_WEEK = 604800
NO_EVENTS = "the receiver recorded no events"
NO_TIME_MARKS = "the log has no time marks (UBX-TIM-TM2): the event input of the receiver recorded nothing"
EDGES = ("falling", "rising")


def _ahead(time_system: str, fields: tuple, leap: int | None) -> int:
    """The seconds GPS time is ahead of the time system of the file: GPS, Galileo, QZSS and NavIC time are GPS time;
    UTC - and GLONASS time, which RINEX writes as UTC - is behind by the leap seconds (those of the header when it
    gives them, else those of the day); BeiDou time by 14 s."""
    # rinex._to_gps (header().first/last, time_span) reads a "UTC" file as GPS time - phase 3 behaviour, kept: for
    # such a file those times are the leap seconds (18 s today) away from its events, which are converted here
    return rinex.gps_ahead(time_system, datetime(*fields[:5]), leap)


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
    the place of the shot (from 1), no position; ``valid`` False for a mark RTKLIB wrote with the comment "Time mark is
    not valid" (the receiver had no valid GNSS fix when it took it: :func:`invalid_warning`). An event within
    :data:`DOUBLE_EVENT_S` of the one kept before it is left out and counted (``duplicates``, and a warning). A file
    without events gives no shots and the warning
    :data:`NO_EVENTS`. A file packed in layers is read through :func:`.rinex.plain`; a navigation file, or a file that
    is not RINEX, is a ``ValueError`` naming it."""
    path = Path(path)
    info, leap, found = rinex.external_events(path)
    system = info.time_system
    timed = []
    for fields, line, valid, _epoch in found:
        week, tow = _week_and_seconds(fields, _ahead(system, fields, leap))
        timed.append(((week, tow), line, valid))
    kept, doubles = _without_doubles(timed)
    shots = [Shot(index=i, gps_time=week * float(_WEEK) + tow, valid=valid,
                  source={"file": str(path), "kind": "rinex_events", "line": line, "gps_week": week, "tow": tow,
                          "time_system": system, "time_valid": valid})
             for i, ((week, tow), line, valid) in enumerate(kept, 1)]
    warnings = []
    if not found:
        warnings.append(NO_EVENTS)
    warnings += invalid_warning([valid for _t, _line, valid in kept])
    warnings += _doubles_warning(doubles)
    return ShotSet(shots=shots, kind="rinex_events", name=path.name, warnings=warnings, duplicates=doubles)


def rinex_event_times(path: str | Path) -> list[float]:
    """The GPS times of every external event of a RINEX observation file, in the order of their times: doubles and
    marks without a valid time too, as RTKLIB reads them (:func:`rinex_events` leaves doubles out)."""
    return sorted(t for t, _epoch in rinex_event_epochs(path))


def rinex_event_epochs(path: str | Path) -> list[tuple[float, int]]:
    """``(GPS time, observation epoch)`` of every external event of a RINEX observation file, in the order of the
    file: the number of the observation epoch RTKLIB gives it (:func:`.rinex.external_events`)."""
    info, leap, found = rinex.external_events(Path(path))
    out = []
    for fields, _line, _valid, epoch in found:
        week, tow = _week_and_seconds(fields, _ahead(info.time_system, fields, leap))
        out.append((week * float(_WEEK) + tow, epoch))
    return out


def invalid_warning(valid: list[bool]) -> list[str]:
    """The warning for the time marks without a valid time among marks in time order (``valid`` of each): the receiver
    had no valid GNSS fix when it took them - before its first fix, or after losing it in flight (signals blocked)."""
    invalid = [i for i, ok in enumerate(valid) if not ok]
    if not invalid:
        return []
    first = next((i for i, ok in enumerate(valid) if ok), len(valid))
    before = sum(i < first for i in invalid)
    lost = len(invalid) - before
    where = []
    if before:
        where.append(f"{before} before its first fix")
    if lost:
        where.append(f"{lost} after losing it in flight, e.g. signals blocked")
    n = len(invalid)
    return [f"{n} time mark{'s' if n > 1 else ''} without a valid time: the receiver had no valid GNSS fix when "
            f"{'they were' if n > 1 else 'it was'} taken ({', '.join(where)}). Kept, marked as not valid: check "
            f"{'those photos' if n > 1 else 'that photo'}"]


def _without_doubles(timed: list) -> tuple[list, int]:
    """``timed`` - items whose first element is a time (GPS week, seconds of the week) - in time order without those
    within :data:`DOUBLE_EVENT_S` of the one kept before them, and how many were left out."""
    kept_items: list = []
    doubles = 0
    kept: tuple[int, float] | None = None
    for item in sorted(timed, key=lambda item: item[0]):
        if kept is not None and _apart(kept, item[0]) < DOUBLE_EVENT_S:
            doubles += 1
            continue
        kept = item[0]
        kept_items.append(item)
    return kept_items, doubles


def _doubles_warning(doubles: int) -> list[str]:
    if not doubles:
        return []
    return [f"{doubles} double event{'s' if doubles > 1 else ''} within {DOUBLE_EVENT_S:g} s of the one before left "
            "out"]


# --- u-blox logs: UBX-TIM-TM2 ---------------------------------------------------------------------------------------

_SYNC = b"\xb5\x62"
_TIM_TM2 = (0x0D, 0x03)  # class, id; payload of 28 bytes
_CFG_TP5 = (0x06, 0x31)  # the settings of a time pulse (poll answer or command), 32 bytes in version 1
_TM2 = struct.Struct("<BBHHHIIIII")  # ch, flags, count, wnR, wnF, towMsR, towSubMsR, towMsF, towSubMsF, accEst
_TP5 = struct.Struct("<BB2xhhIIIIiI")  # tpIdx, version, antCableDelay, rfGroupDelay, ..., userConfigDelay, flags
# TIM-TM2 flags: bit 2 newFallingEdge, bits 3-4 timeBase, bit 5 utc available, bit 6 time is valid, bit 7 newRisingEdge
_NEW_EDGE = {"falling": 1 << 2, "rising": 1 << 7}
_TIME_VALID = 1 << 6
_BASES = {0: "receiver", 1: "GNSS", 2: "UTC"}
# CFG-TP5 version 1, flags bits 7-10 (gridUtcGnss): the time grid of the time pulse - and of the time marks in "GNSS"
# time base - 0 UTC, 1 GPS, 2 GLONASS, 3 BeiDou, 4 Galileo. The Interface Description does not say how TIM-TM2 writes
# the week and the time of week on a BeiDou, Galileo or GLONASS grid (whose week numbering, seconds of the week or
# offset from GPS time?): marks on those grids are not converted, only marked as not valid. A CFG-TP5 of version 0
# (no grid field) and the time grid set by CFG-VALSET (CFG-TP-TIMEGRID_TP1, F9 receivers) are not read: their marks in
# "GNSS" time are read as GPS time with a warning.
_GRIDS = {0: "UTC", 1: "GPS", 2: "GLONASS", 3: "BeiDou", 4: "Galileo"}
_OTHER_GRIDS = ("GLONASS", "BeiDou", "Galileo")
# the UBX classes of the Interface Description (NAV, RXM, INF, ACK, CFG, UPD, MON, AID, TIM, ESF, MGA, LOG, SEC, NAV2):
# a frame of another class with a wrong checksum is sync bytes by chance in other data (RTCM 3, a binary of another
# receiver), not a damaged frame; nor is one longer than any UBX message
_CLASSES = frozenset((0x01, 0x02, 0x04, 0x05, 0x06, 0x09, 0x0A, 0x0B, 0x0D, 0x10, 0x13, 0x21, 0x27, 0x29))
_LONGEST = 8192


def ubx_frames(path: str | Path, skipped: list[int] | None = None) -> Iterator[tuple[int, int, bytes, int]]:
    """The UBX frames of a u-blox log as ``(class, id, payload, offset)``, in order. A frame is 0xB5 0x62, class, id,
    the little-endian length of the payload, the payload and the two bytes of the 8-bit Fletcher checksum over class,
    id, length and payload (u-blox Interface Description); the bytes between frames (NMEA, RTCM) are passed over. A
    whole frame whose checksum is wrong is skipped - its offset appended to ``skipped`` when a list is given and it
    has a UBX class and a length a UBX message can have (not sync bytes by chance in other data) - and the next sync
    bytes after its start are looked for, as are those after a frame cut by the end of the file."""
    path = Path(path)
    with open(path, "rb") as f:
        if path.stat().st_size == 0:
            return
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            size = len(data)
            i = data.find(_SYNC)
            while 0 <= i and i + 8 <= size:
                length = data[i + 4] | (data[i + 5] << 8)
                end = i + 8 + length
                if end <= size:
                    a = b = 0
                    for byte in data[i + 2:end - 2]:
                        a = (a + byte) & 0xFF
                        b = (b + a) & 0xFF
                    if data[end - 2] == a and data[end - 1] == b:
                        yield data[i + 2], data[i + 3], bytes(data[i + 6:end - 2]), i
                        i = data.find(_SYNC, end)
                        continue
                    if skipped is not None and data[i + 2] in _CLASSES and length <= _LONGEST:
                        skipped.append(i)
                i = data.find(_SYNC, i + 1)


def _tm2_time(week: int, ms: int, sub_ns: int, utc: bool) -> tuple[int, float, int]:
    """(GPS week, seconds of the week, leap seconds added) of an edge at ``ms`` milliseconds and ``sub_ns``
    nanoseconds of the week ``week``, in UTC when ``utc`` (GPS time is ahead by the leap seconds of that day)."""
    leap = 0
    if utc:
        leap = rinex.gps_minus_utc(datetime(1980, 1, 6) + timedelta(weeks=week, milliseconds=ms))
    whole_ms = ms + leap * 1000
    week, whole_ms = week + whole_ms // (_WEEK * 1000), whole_ms % (_WEEK * 1000)
    return week, whole_ms / 1000.0 + sub_ns * 1e-9, leap


WRAP_NEAR = 1024  # the 16-bit counter of TIM-TM2 going down: a wrap (65535 -> 0) when it lands within this many
# counts of where it would after 65535, else the counter started again (a reset: the receiver restarted)


def _count_gaps(counts: list[tuple[int, int, int]]) -> int:
    """How many values the counter of TIM-TM2 skipped: ``counts`` (channel, run, unwrapped count) in the order of the
    log; each channel and each run of its counter (between resets) apart."""
    runs: dict[tuple[int, int], set[int]] = {}
    for channel, run, count in counts:
        runs.setdefault((channel, run), set()).add(count)
    missing = 0
    for values in runs.values():
        values = sorted(values)
        missing += sum(b - a - 1 for a, b in zip(values, values[1:]) if b - a > 1)
    return missing


def ubx_time_marks(path: str | Path, edge: str = "falling") -> ShotSet:
    """The time marks of a u-blox log (UBX-TIM-TM2 messages) as shots, in the order of their times: one for every
    message that announces a new ``edge`` ("falling", the default, or "rising") of the signal at the event input,
    ``index`` the counter of the message (``count``), the time of that edge (``wn``, ``towMs``, ``towSubMs``: to the
    nanosecond) in GPS time - ``gps_time`` and, exact, ``source["gps_week"]``, ``source["tow"]``, ``source["tow_ms"]``,
    ``source["tow_sub_ns"]``; the 16-bit counter wraps after 65535 and is unwrapped, each event input (``channel``)
    apart, a drop of it that is not a wrap (:data:`WRAP_NEAR`) a reset, said, the indices going on (``source["count"]``
    as sent). A mark in UTC is put in GPS time with the leap seconds of its day (``source["leap_seconds"]``); one in
    GNSS time is GPS time when the last CFG-TP5 before it sets the time grid to GPS, not valid when it sets BeiDou,
    Galileo or GLONASS (not converted: see :data:`_GRIDS`), else read as GPS time with a warning; one in receiver time
    likewise. ``valid`` is False for a mark without a valid time (bit 6: the receiver had no valid GNSS fix at the mark,
    before its first fix or lost in flight), with a warning (:func:`invalid_warning`); a gap of the counter (pulses the
    receiver never reported, counted on each channel between resets) is warned of too; ``source["acc_ns"]`` the accuracy
    the receiver gives the time. A mark within :data:`DOUBLE_EVENT_S` of the one kept before it (a message repeated, a
    bouncing contact) is left out and counted; damaged frames are skipped and counted in a warning. A log without marks
    of ``edge`` gives no shots and a warning: :data:`NO_TIME_MARKS`, or one that names the other edge when the log has
    those."""
    if edge not in EDGES:
        raise ValueError(f"edge must be one of {', '.join(EDGES)}, not {edge!r}")
    other = EDGES[1 - EDGES.index(edge)]
    path = Path(path)
    skipped: list[int] = []
    marks: list[tuple[int, tuple]] = []
    grid = None  # the time grid of the last CFG-TP5 of the time pulse 0, version 1
    for cls, msg_id, payload, offset in ubx_frames(path, skipped):
        if (cls, msg_id) == _TIM_TM2 and len(payload) == _TM2.size:
            marks.append((offset, grid, _TM2.unpack(payload)))
        elif (cls, msg_id) == _CFG_TP5 and len(payload) == _TP5.size:
            tp_index, version, *_, flags = _TP5.unpack(payload)
            if tp_index == 0 and version >= 1:
                grid = _GRIDS.get((flags >> 7) & 0xF, "unknown")
    timed = []
    others = 0
    channels = set()
    counts = []
    state: dict[int, dict] = {}  # of each channel: its last count, what its counts are raised by, its run
    resets: list[tuple[int, int, int]] = []  # (channel, count before, count after)
    for offset, grid, (channel, flags, count, wn_r, wn_f, ms_r, ns_r, ms_f, ns_f, acc) in marks:
        st = state.setdefault(channel, {"last": None, "base": 0, "run": 0, "index": 0})
        if st["last"] is not None and count < st["last"]:
            if count + 65536 - st["last"] <= WRAP_NEAR:  # 65535 -> 0: the counter of 16 bits went round
                st["base"] += 65536
            else:  # the counter started again (the receiver restarted): the indices go on after the last
                st["run"] += 1
                st["base"] = st["index"] + 1 - count
                resets.append((channel, st["last"], count))
        st["last"] = count
        index = st["index"] = st["base"] + count
        counts.append((channel, st["run"], index))
        if not flags & _NEW_EDGE[edge]:
            others += bool(flags & _NEW_EDGE[other])
            continue
        channels.add(channel)
        week, ms, ns = (wn_f, ms_f, ns_f) if edge == "falling" else (wn_r, ms_r, ns_r)
        base = _BASES.get((flags >> 3) & 0x3, "unknown")
        gps_week, tow, leap = _tm2_time(week, ms, ns, base == "UTC")
        valid = bool(flags & _TIME_VALID)
        if base == "GNSS" and (grid == "GPS" or grid in _OTHER_GRIDS):
            base = grid
        source = {"file": str(path), "kind": "ubx_tim_tm2", "offset": offset, "edge": edge, "gps_week": gps_week,
                  "tow": tow, "tow_ms": ms, "tow_sub_ns": ns, "time_base": base, "acc_ns": acc, "channel": channel,
                  "count": count, "time_valid": valid}
        if base == "UTC":
            source["leap_seconds"] = leap
        timed.append(((gps_week, tow), index, valid and base not in _OTHER_GRIDS, source))
    kept, doubles = _without_doubles(timed)
    shots = [Shot(index=index, gps_time=week * float(_WEEK) + tow, valid=valid, source=source)
             for (week, tow), index, valid, source in kept]
    warnings = []
    if skipped:
        warnings.append(f"{len(skipped)} damaged UBX frame{'s' if len(skipped) > 1 else ''} (wrong checksum) skipped"
                        " - in a log mixed with other data (RTCM 3) some can be bytes that look like a frame by chance")
    if not timed:
        warnings.append(f"the log has no time marks of the {edge} edge, but {others} of the {other} edge: read them "
                        f"with edge=\"{other}\"" if others else NO_TIME_MARKS)
    bases = {s.source["time_base"] for s in shots}
    unknown = sorted(bases - {"GPS", "UTC", *_OTHER_GRIDS})
    if unknown:
        warnings.append(f"the time marks are in {' and '.join(unknown)} time and the log does not say of which system: "
                        "read as GPS time, they can be off by seconds if that is not GPS time")
    for system in _OTHER_GRIDS:
        n = sum(s.source["time_base"] == system for s in shots)
        if n:
            warnings.append(f"{n} time mark{'s' if n > 1 else ''} in {system} time (the time grid the log sets): not "
                            "converted to GPS time, marked as not valid - set the time grid of the receiver to GPS")
    warnings += invalid_warning([s.source["time_valid"] for s in shots])
    missing = _count_gaps(counts)
    if missing:
        warnings.append(f"{missing} pulse{'s' if missing > 1 else ''} of the event input never reported (gaps in the "
                        "counter of TIM-TM2): the receiver counted them but sent no message for them - it sends at "
                        "most one time mark per navigation epoch - so their photos have no time mark")
    if resets:
        said = ", ".join(f"{a} to {b}" + (f" (channel {c})" if len(state) > 1 else "") for c, a, b in resets[:3])
        warnings.append(f"the counter of TIM-TM2 started again {len(resets)} time{'s' if len(resets) > 1 else ''} "
                        f"({said}{', ...' if len(resets) > 3 else ''}): the receiver restarted - the marks keep "
                        "their order, and pulses lost while it restarted are not counted")
    if len(channels) > 1:
        warnings.append(f"time marks of {len(channels)} event inputs (channels {', '.join(map(str, sorted(channels)))})"
                        " mixed in one set")
    warnings += _doubles_warning(doubles)
    return ShotSet(shots=shots, kind="ubx_tim_tm2", name=path.name, warnings=warnings, duplicates=doubles)


# --- RTKLIB's _events.pos -------------------------------------------------------------------------------------------

_INVALID_LINE = re.compile(r"^(\S+)\s+(\S+)\s+Q=0,\s*" + re.escape(ppk.INVALID_TIME_MARK))


def _time_scale(lines: list[str]) -> str:
    """The time scale (GPST, UTC, JST) of a .pos file from the header line that names its columns."""
    for line in lines:
        if line.startswith("%"):
            fields = ppk._FIELDS.split(line[1:].strip())
            if "Q" in fields and "ns" in fields:
                return fields[0]
    return "GPST"


def events_pos(path: str | Path) -> ShotSet:
    """The time marks of an ``_events.pos`` file - the positions of the rover that RTKLIB (or Emlid Studio) interpolated
    at the events of the rover - as shots in time order, ``index`` their place (from 1): each with the antenna
    ``(lat, lon, h)`` above the WGS84 ellipsoid, ``std`` (north, east, up), ``status`` from RTKLIB's Q (``source["q"]``,
    ``source["ns"]``) and its time in GPS time. A line "Q=0, Time mark is not valid" (a mark between epochs RTKLIB
    could not solve) is a shot without a position, ``valid`` False and ``status`` "none", with a warning. The file is
    read with :func:`.ppk.read_pos`; what it refuses is refused here."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    scale = _time_scale(lines)
    if scale not in ppk._TIME_SCALES:
        raise ValueError(f"{path.name}: its times are in {scale}, which is not GPS time (GPST), UTC or JST")
    timed: list[tuple[float, Shot]] = []
    for number, line in enumerate(lines, 1):
        match = _INVALID_LINE.match(line.strip())
        if match:
            seconds = ppk._gps_seconds(ppk._file_seconds(match.group(1), match.group(2)), scale)
            timed.append((seconds, Shot(index=None, gps_time=seconds, status="none", valid=False,
                                        source={"file": str(path), "kind": "events_pos", "line": number, "q": 0})))
    warnings: list[str] = []
    try:
        solution = ppk.read_pos(path)
    except ppk.NoPositions:
        if not timed:
            raise
    else:
        warnings += solution.warnings
        for t, (lat, lon, h), q, ns, (east, north, up) in zip(solution.times, solution.llh, solution.q, solution.ns,
                                                               solution.sd):
            timed.append((float(t), Shot(index=None, gps_time=float(t), antenna=(float(lat), float(lon), float(h)),
                                         height="ellipsoidal", std=(float(north), float(east), float(up)),
                                         status=ppk.status(int(q)),
                                         source={"file": str(path), "kind": "events_pos", "q": int(q), "ns": int(ns)})))
    timed.sort(key=lambda item: item[0])
    shots = []
    for i, (t, shot) in enumerate(timed, 1):
        week = int(t // _WEEK)
        shot.index = i
        shot.source.update(gps_week=week, tow=t - week * _WEEK)
        shots.append(shot)
    invalid = sum(not s.valid for s in shots)
    if invalid:
        warnings.append(f"{invalid} time mark{'s' if invalid > 1 else ''} RTKLIB could not place (no solution at the "
                        "epochs around it): not valid, without a position")
    return ShotSet(shots=shots, kind="events_pos", name=path.name, warnings=warnings)
