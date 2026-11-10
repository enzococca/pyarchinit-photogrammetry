"""A minimal writer of ArduPilot DataFlash logs for the tests, binary (``.bin``) and text (``.log`` of Mission Planner),
written from the published description of the format (the table of format characters in the comments of
``libraries/AP_Logger/LogStructure.h`` and ``AP_Logger/README.md``): every message starts with the bytes 0xA3 0x95 and
its type; the FMT messages (type 128) describe the others; FMTU, UNIT and MULT give their units and multipliers. A text
log writes one message per line, its name and its values separated by commas, the fields of the characters ``c C e E
L`` already scaled."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

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
GPS_LAG_US = 80_000  # a GPS message is logged this long after its fix


def flight(shots: int = 50, *, message: str = "CAM", instances=(0,), no_fix: int = 0, interval_ms: int = 2000,
           gps_hz: int = 5, fix_until: int | None = None, units: bool = True, img0: int = 1) -> tuple[LogWriter, list]:
    """A survey flight: GPS (instance 0, in use) at ``gps_hz`` with status 6 (RTK fix) for the shots before
    ``fix_until`` (default: half of them) then 5 (float), GPA with HAcc/VAcc, and ``shots`` camera messages
    (``message``) every ``interval_ms`` for each camera of ``instances``, the first ``no_fix`` without a GPS time
    (week 0). Returns the writer and the expected shots: ``(instance, img, week, tow_ms, time_us, lat, lng, alt_cm,
    roll_cd, pitch_cd, yaw_cd, status, hacc_cm, vacc_cm)``."""
    log = LogWriter()
    if units:
        log.units({"s": "s", "d": "deg", "m": "m", "-": ""}, {"F": 1e-6, "C": 1e-3, "G": 1e-7, "B": 1e-2, "-": 0.0})
    log.fmt("GPS", *GPS_FORMAT)
    log.fmt("GPA", *GPA_FORMAT)
    log.fmt(message, *CAM_FORMAT)
    if units:
        log.fmtu("GPS", "s#-----ddm", "F------GGB")
        log.fmtu(message, "s#-s-ddmmmddd", "F--C-GGBBB---")
    fix_until = shots // 2 if fix_until is None else fix_until
    period_us = 1_000_000 // gps_hz
    span_us = (shots + 2) * interval_ms * 1000
    expected = []
    t_us = 0
    k_shot = 0
    first_shot_us = 1_234_567
    while t_us <= span_us:
        fix_us = BOOT0_US + t_us
        tow = TOW0_MS + t_us // 1000
        k_now = round((t_us - first_shot_us) / (interval_ms * 1000))
        status = 6 if k_now < fix_until else 5
        hacc, vacc = (2, 3) if status == 6 else (35, 60)
        log.add("GPS", fix_us + GPS_LAG_US, 0, status, tow, WEEK, 21, 70, int(LAT0 * 1e7), int(LON0 * 1e7), 15000,
                9.5, 90.0, 0.0, 0.0, 1)
        log.add("GPA", fix_us + GPS_LAG_US + 300, 0, 120, hacc, vacc, 20, 0.0, 1, tow, 200, 0.0)
        while k_shot < shots and first_shot_us + k_shot * interval_ms * 1000 < t_us + period_us:
            s_us = first_shot_us + k_shot * interval_ms * 1000
            s_status = 6 if k_shot < fix_until else 5
            for inst in instances:
                img = img0 + k_shot
                lat = int(LAT0 * 1e7) + 1234 * k_shot + 7 * inst
                lng = int(LON0 * 1e7) - 987 * k_shot
                alt, rel, galt = 15123 + k_shot, 6012, 15140
                roll, pitch, yaw = -312 + k_shot, 147, (35950 + 10 * k_shot) % 36000
                if k_shot < no_fix:
                    week, tow_ms = 0, 0
                else:
                    week, tow_ms = WEEK, TOW0_MS + s_us // 1000
                log.add(message, BOOT0_US + s_us, inst, img, tow_ms, week, lat, lng, alt, rel, galt, roll, pitch, yaw)
                ha, va = (2, 3) if s_status == 6 else (35, 60)
                expected.append((inst, img, week, tow_ms, BOOT0_US + s_us, lat, lng, alt, roll, pitch, yaw, s_status,
                                 ha, va))
            k_shot += 1
        t_us += period_us
    return log, expected
