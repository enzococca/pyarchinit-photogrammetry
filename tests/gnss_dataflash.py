"""A minimal writer of ArduPilot DataFlash logs for the tests, binary (``.bin``) and text (``.log`` of Mission Planner),
written from the published description of the format (the table of format characters in the comments of
``libraries/AP_Logger/LogStructure.h`` and ``AP_Logger/README.md``): every message starts with the bytes 0xA3 0x95 and
its type; the FMT messages (type 128) describe the others; FMTU, UNIT and MULT give their units and multipliers. A text
log writes one message per line, its name and its values separated by commas, the fields of the characters ``c C e E
L`` already scaled."""
from __future__ import annotations

import bisect
import math
import struct
from dataclasses import dataclass, field

import numpy as np

HEAD = b"\xa3\x95"
FMT_TYPE = 128
# the format characters and how they are packed (little-endian)
PACK = {"a": "32h", "b": "b", "B": "B", "h": "h", "H": "H", "i": "i", "I": "I", "f": "f", "d": "d", "n": "4s",
        "N": "16s", "Z": "64s", "c": "h", "C": "H", "e": "i", "E": "I", "L": "i", "M": "B", "q": "q", "Q": "Q"}
SCALE = {"c": 0.01, "C": 0.01, "e": 0.01, "E": 0.01, "L": 1e-7}
STRINGS = "nNZ"

# the messages of a flight, as ArduPilot 4.x writes them
CAM_FORMAT = ("QBHIHLLeeeccC", "TimeUS,I,Img,GPSTime,GPSWeek,Lat,Lng,Alt,RelAlt,GPSAlt,R,P,Y")
GPS_FORMAT = ("QBBIHBcLLeffffB", "TimeUS,I,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,GCrs,VZ,Yaw,U")
GPA_FORMAT = ("QBCCCCfBIHf", "TimeUS,I,VDop,HAcc,VAcc,SAcc,YAcc,VV,SMS,Delta,Und")


def _struct(fmt: str) -> struct.Struct:
    return struct.Struct("<" + "".join(PACK[c] for c in fmt))


@dataclass
class LogWriter:
    """The messages of a log, in order; :meth:`binary` and :meth:`text` write them."""

    formats: dict[str, tuple[int, str, list[str]]] = field(default_factory=dict)
    records: list[tuple[str, tuple]] = field(default_factory=list)
    next_type: int = 129

    def __post_init__(self) -> None:
        self.formats["FMT"] = (FMT_TYPE, "BBnNZ", ["Type", "Length", "Name", "Format", "Columns"])
        self.records.append(("FMT", (FMT_TYPE, 89, "FMT", "BBnNZ", "Type,Length,Name,Format,Columns")))

    def fmt(self, name: str, fmt: str, columns: str, type_id: int | None = None) -> int:
        """Define (or define again) the message ``name``; its type."""
        if type_id is None:
            type_id = self.next_type
            self.next_type += 1
        self.formats[name] = (type_id, fmt, columns.split(","))
        self.records.append(("FMT", (type_id, 3 + _struct(fmt).size, name, fmt, columns)))
        return type_id

    def add(self, name: str, *values) -> None:
        """A message ``name`` with its ``values`` as stored (the integers of ``L``, ``c``...)."""
        type_id, fmt, columns = self.formats[name]
        assert len(values) == len(fmt) == len(columns), name
        self.records.append((name, (type_id, fmt, values)))

    def units(self, units: dict[str, str], mults: dict[str, float]) -> None:
        """UNIT and MULT messages: what the ids of FMTU mean."""
        for name, fmt, columns in (("UNIT", "QbZ", "TimeUS,Id,Label"), ("MULT", "Qbd", "TimeUS,Id,Mult"),
                                   ("FMTU", "QBNN", "TimeUS,FmtType,UnitIds,MultIds")):
            if name not in self.formats:
                self.fmt(name, fmt, columns)
        for key, label in units.items():
            self.add("UNIT", 0, ord(key), label)
        for key, mult in mults.items():
            self.add("MULT", 0, ord(key), mult)

    def fmtu(self, name: str, unit_ids: str, mult_ids: str) -> None:
        self.add("FMTU", 0, self.formats[name][0], unit_ids, mult_ids)

    def binary(self) -> bytes:
        out = bytearray()
        for name, values in self.records:
            if name == "FMT":
                type_id, length, msg, fmt, columns = values
                out += HEAD + bytes([FMT_TYPE]) + _struct("BBnNZ").pack(type_id, length, msg.encode(), fmt.encode(),
                                                                         columns.encode())
                continue
            type_id, fmt, row = values
            packed = []
            for c, v in zip(fmt, row):
                if c in STRINGS:
                    packed.append(v.encode() if isinstance(v, str) else v)
                elif c == "a":
                    packed.extend(v)
                else:
                    packed.append(v)
            out += HEAD + bytes([type_id]) + _struct(fmt).pack(*packed)
        return bytes(out)

    def text(self) -> str:
        lines = []
        for name, values in self.records:
            if name == "FMT":
                type_id, length, msg, fmt, columns = values
                lines.append(f"FMT, {type_id}, {length}, {msg}, {fmt}, {columns}")
                continue
            _type_id, fmt, row = values
            out = []
            for c, v in zip(fmt, row):
                if c in SCALE:
                    out.append(f"{v * SCALE[c]:.7f}" if c == "L" else f"{v * SCALE[c]:.2f}")
                elif c in "fd":
                    out.append(repr(float(v)))
                elif c == "a":
                    out.append(" ".join(map(str, v)))
                else:
                    out.append(str(v))
            lines.append(", ".join([name] + out))
        return "\n".join(lines) + "\n"


# --- a survey flight ---------------------------------------------------------------------------------------------

WEEK = 2315  # GPS week of 2024-05-12
TOW0_MS = 30615400  # milliseconds of the week of the first GPS fix
BOOT0_US = 95_000_000  # the boot clock (TimeUS) at that fix
LAT0, LON0 = 43.7712345, 11.2543210
PROCESS_US = 12_000  # a GPS message is processed and written this long (plus up to 4 ms) after its fix arrives
CAM_WRITE_US = 3_000  # a CAM is written this long after the pin saw the flash

# the layouts of CAM, GPS and GPA in ArduPilot versions
CAM_LAYOUTS = {"4.x": CAM_FORMAT, "master": ("QBHIHLLeeefff", CAM_FORMAT[1]),
               "3.x": ("QIHLLeeeccC", "TimeUS,GPSTime,GPSWeek,Lat,Lng,Alt,RelAlt,GPSAlt,Roll,Pitch,Yaw")}
GPS_LAYOUTS = {"4.x": GPS_FORMAT,
               "3.x": ("QBIHBcLLefffB", "TimeUS,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,GCrs,VZ,U"),
               "no_u": ("QBIHBcLLefff", "TimeUS,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,GCrs,VZ")}
GPA_LAYOUTS = {"4.x": GPA_FORMAT, "3.x": ("QCCCCBIH", "TimeUS,VDop,HAcc,VAcc,SAcc,VV,SMS,Delta")}


def _row(layout: tuple[str, str], **values) -> tuple:
    return tuple(values[c] for c in layout[1].split(","))


def _week_ms(g: float) -> tuple[int, int]:
    total = round(g * 1000.0)
    return total // 604_800_000, total % 604_800_000


def flight(shots: int = 50, *, message: str = "CAM", instances=(0,), no_fix: int = 0, interval_ms: int = 2000,
           gps_hz: float = 5, fix_until: int | None = None, units: bool = True, img0: int = 1,
           drift_ppm: float = 0.0, latency_ms: float = 0.0, outage: tuple[float, float] | None = None,
           gpa: bool = True, cam_layout: str = "4.x", gps_layout: str = "4.x", gpa_layout: str = "4.x",
           tow0_ms: int = TOW0_MS, week: int = WEEK, shot_offsets_s=None, seed: int = 1,
           sms_jitter_ms: float = 0.0, firmware: str = "4.x", sms: str | None = None, processing_ms: float = 20.0,
           receivers: int = 1, latencies_ms=(0.0, 40.0), switch_s: float | None = None, blended: bool = False,
           prelock: int = 0, duplicate_every: int = 0, log_every: int = 1) -> tuple[LogWriter, list]:
    """A survey flight as ArduPilot logs it.

    ``receivers`` receivers (instances 0, 1) have a fix every 1/``gps_hz`` s of GPS time, none in ``outage``
    (seconds from the first fix); a fix of receiver r arrives ``latencies_ms[r]`` (``latency_ms`` for receiver 0)
    after its epoch. With ``sms="corrected"`` (the default of ``firmware="4.x"``) GPA ``SMS`` is that arrival (ms,
    off by up to ``sms_jitter_ms`` either way) and the GPS and GPA messages are written 12-16 ms later; with
    ``"loop"`` (the default of ``firmware="3.x"``) the fix is processed at the next tick of a 50 Hz loop - delays that
    drift slowly as a sawtooth with the clock, as in real logs (``"tick"``: exactly at the tick, 0.3 ms after it, the
    sawtooth without jitter); with ``"processing"`` it is processed 0-
    ``processing_ms`` after its arrival (uniform). In both SMS is the processing time (ms, truncated) and the messages
    are written within the next 0.8 ms. The boot clock runs
    ``drift_ppm`` fast. Only every ``log_every``-th fix is logged (sparse GPS rows); every ``duplicate_every``-th
    is logged twice; the first ``prelock`` fixes have status 1 and a GPS time 1 s wrong. Receiver 0 is in use (``U``)
    until ``switch_s``, receiver 1 after; with ``blended`` a blended solution (instance 2: GPS time of the fix, SMS
    the mean of the two arrivals) is in use. Status 6 (RTK fix) until shot ``fix_until`` (default: half of them),
    then 5.

    A camera of each of ``instances`` shoots ``shots`` times every ``interval_ms`` (or at ``shot_offsets_s`` from
    the first fix); its message has the boot time of the shot as ``TimeUS`` - with ``firmware="3.x"`` the time it is
    written, at the next 50 Hz loop (0-20 ms later) - and, as the firmware writes it, the GPS week and time of the
    LAST FIX processed of the receiver in use (week 0 for the first ``no_fix``). Returns the writer and one dict per
    camera message: ``inst``, ``img``, ``true`` (GPS time of the shot, s), ``late`` (s the TimeUS is after it),
    ``time_us``, ``cam_week``, ``cam_tow_ms``, ``lat``, ``lng`` (1e-7 deg), ``alt`` (cm), ``attitude`` (deg),
    ``status`` (of the in-use GPS message nearest in TimeUS within 1 s, None without), ``hacc``, ``vacc`` (cm, None
    without)."""
    rng = np.random.default_rng(seed)
    sms = sms or ("loop" if firmware == "3.x" else "corrected")
    cam_l, gps_l, gpa_l = CAM_LAYOUTS[cam_layout], GPS_LAYOUTS[gps_layout], GPA_LAYOUTS[gpa_layout]
    log = LogWriter()
    if units:
        log.units({"s": "s", "d": "deg", "m": "m", "-": ""}, {"F": 1e-6, "C": 1e-3, "G": 1e-7, "B": 1e-2, "-": 0.0})
    log.fmt("MSG", "QZ", "TimeUS,Message")
    log.add("MSG", BOOT0_US - 50_000_000, "ArduPlane V3.6.0 (abc)" if firmware == "3.x" else "ArduCopter V4.5.7 (abc)")
    log.fmt("GPS", *gps_l)
    if gpa:
        log.fmt("GPA", *gpa_l)
    log.fmt(message, *cam_l)
    if units and gps_layout == "4.x" and cam_layout == "4.x":
        log.fmtu("GPS", "s#-----ddm", "F------GGB")
        log.fmtu(message, "s#-s-ddmmmddd", "F--C-GGBBB---")
    g0 = week * 604800.0 + tow0_ms / 1000.0
    scale = 1.0 + drift_ppm * 1e-6
    lat_ms = [latency_ms] + list(latencies_ms[1:])

    def boot_us(g: float) -> float:
        return BOOT0_US + (g - g0) * 1e6 * scale

    offsets = (list(shot_offsets_s) if shot_offsets_s is not None
               else [1.234567 + k * interval_ms / 1000.0 for k in range(shots)])
    fix_until = len(offsets) // 2 if fix_until is None else fix_until
    boundary = (offsets[fix_until] - interval_ms / 2000.0) if fix_until < len(offsets) else math.inf
    period_ms = round(1000.0 / gps_hz)
    events = []  # (time written, name, values)
    rows = []  # the GPS messages: (time written, instance, week, ms, status, hacc, vacc, in use)
    k = 0
    while k * period_ms / 1000.0 <= offsets[-1] + 3.0:
        rel = k * period_ms / 1000.0
        k += 1
        if outage is not None and outage[0] <= rel < outage[1]:
            continue
        if (k - 1) % log_every:
            continue
        g = g0 + rel
        wk, ms = _week_ms(g)
        status = 6 if rel < boundary else 5
        if k <= prelock:
            status, (wk, ms) = 1, _week_ms(g + 1.0)
        hacc, vacc = (2, 3) if status == 6 else (35, 60)
        primary = 0 if switch_s is None or rel < switch_s else 1
        arrivals = []
        for inst in range(receivers):
            arrival = boot_us(g + lat_ms[inst] / 1000.0)
            arrivals.append(arrival)
        insts = list(range(receivers)) + ([2] if blended else [])
        for inst in insts:
            arrival = arrivals[inst] if inst < receivers else sum(arrivals) / len(arrivals)
            if sms == "corrected":
                sms_ms = int(round(arrival / 1000.0 + rng.uniform(-sms_jitter_ms, sms_jitter_ms)))
                written = int(round(arrival)) + PROCESS_US + int(rng.integers(0, 4000))
            elif sms == "tick":  # processed exactly at the next tick of a 50 Hz loop: a deterministic sawtooth
                processed = math.ceil(arrival / 20_000) * 20_000 + 300
                sms_ms = int(processed // 1000)
                written = int(processed + 400)
            elif sms == "loop":  # processed at the next tick of a 50 Hz loop (and up to 1.5 ms of reading)
                processed = math.ceil((arrival + rng.uniform(0, 1500)) / 20_000) * 20_000 + rng.uniform(0, 500)
                sms_ms = int(processed // 1000)
                written = int(processed + rng.uniform(0, 800))
            else:
                processed = arrival + rng.uniform(0, processing_ms * 1000.0)
                sms_ms = int(processed // 1000)
                written = int(processed + rng.uniform(0, 800))
            used = int(inst == 2) if blended else int(inst == primary)
            copies = 2 if duplicate_every and k % duplicate_every == 0 else 1
            for copy in range(copies):
                at = written + copy * 20_000
                values = dict(TimeUS=at, I=inst, Status=status, GMS=ms, GWk=wk, NSats=21, HDop=70,
                              Lat=int(LAT0 * 1e7), Lng=int(LON0 * 1e7), Alt=15000, Spd=9.5, GCrs=90.0, VZ=0.0, Yaw=0.0,
                              U=used)
                events.append((at, "GPS", _row(gps_l, **values)))
                if gpa:
                    values = dict(TimeUS=at, I=inst, VDop=120, HAcc=hacc, VAcc=vacc, SAcc=20, YAcc=0.0, VV=1,
                                  SMS=sms_ms, Delta=period_ms, Und=0.0)
                    events.append((at, "GPA", _row(gpa_l, **values)))
                rows.append((at, inst, wk, ms, status, hacc, vacc, used))
    rows.sort()
    used_rows = [r for r in rows if r[7]]
    times = [r[0] for r in used_rows]
    expected = []
    for n, rel in enumerate(offsets):
        g = g0 + rel
        late = rng.uniform(0, 0.020) if firmware == "3.x" else 0.0
        t_us = int(round(boot_us(g + late)))
        written = t_us + CAM_WRITE_US
        j = bisect.bisect_right(times, written)
        cam_week, cam_ms = (used_rows[j - 1][2], used_rows[j - 1][3]) if j and n >= no_fix else (0, 0)
        j = bisect.bisect_left(times, t_us)
        near = min(used_rows[max(0, j - 1):j + 1], key=lambda r: abs(r[0] - t_us))
        close = abs(near[0] - t_us) <= 1_000_000
        status = near[4] if close else None
        hacc, vacc = (near[5], near[6]) if close and gpa else (None, None)
        for inst in instances:
            img = img0 + n
            lat = int(LAT0 * 1e7) + 1234 * n + 7 * inst
            lng = int(LON0 * 1e7) - 987 * n
            alt, rel_alt, galt = 15123 + n, 6012, 15140
            roll_cd, pitch_cd, yaw_cd = -312 + n, 147, (35950 + 10 * n) % 36000
            if cam_layout == "master":
                r, p, y = roll_cd / 100.0, pitch_cd / 100.0, yaw_cd / 100.0
            else:
                r, p, y = roll_cd, pitch_cd, yaw_cd
            values = dict(TimeUS=t_us, I=inst, Img=img, GPSTime=cam_ms, GPSWeek=cam_week, Lat=lat, Lng=lng, Alt=alt,
                          RelAlt=rel_alt, GPSAlt=galt, R=r, P=p, Y=y, Roll=r, Pitch=p, Yaw=y)
            events.append((written, message, _row(cam_l, **values)))
            expected.append({"inst": inst, "img": img if cam_layout != "3.x" else None, "true": g, "late": late,
                             "time_us": t_us, "cam_week": cam_week, "cam_tow_ms": cam_ms, "lat": lat, "lng": lng,
                             "alt": alt, "attitude": (roll_cd / 100.0, pitch_cd / 100.0, yaw_cd / 100.0),
                             "status": status, "hacc": hacc, "vacc": vacc})
    for _written, name, values in sorted(events, key=lambda e: e[0]):
        log.add(name, *values)
    return log, expected
