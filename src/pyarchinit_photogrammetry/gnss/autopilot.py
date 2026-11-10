"""The camera shots of an autopilot log: the CAM and TRIG messages of an ArduPilot DataFlash log, binary (``.bin``) or
text (``.log`` of Mission Planner), with the GPS time, the position above the sea level and the attitude of each shot
and the status and accuracy of the GPS at that time.

The reader is written from the published description of the format - the table of format characters in the comments
of ``libraries/AP_Logger/LogStructure.h`` and ``libraries/AP_Logger/README.md`` of ArduPilot, and the list of log
messages at https://ardupilot.org/copter/docs/logmessages.html - with no code of ArduPilot or pymavlink. A binary log
is a sequence of messages, each the bytes 0xA3 0x95, its type (one byte) and its fields, packed little-endian; the FMT
messages (type 128: ``Type B, Length B, Name n, Format N, Columns Z``, 89 bytes with the header) describe every other
type - its name, its length with the header, one format character per field and the names of the fields - and can
describe a type again later in the log, which then holds for the messages after it. FMTU messages give each field of a
type a unit and a multiplier by one-character ids that UNIT and MULT messages define. A text log writes one message per
line, its name and its values separated by commas, the fields of the characters ``c C e E L`` already scaled."""
from __future__ import annotations

import math
import mmap
import re
from array import array
from pathlib import Path

import numpy as np

from .shots import Shot, ShotSet

HEAD = b"\xa3\x95"
FMT_TYPE = 128
FMT_LENGTH = 89
_FMT_FORMAT = "BBnNZ"
_FMT_COLUMNS = ["Type", "Length", "Name", "Format", "Columns"]
# the format characters: how each field is stored (little-endian) and what its value is multiplied by
_CHARS = {"a": ("<i2", (32,)), "b": "i1", "B": "u1", "h": "<i2", "H": "<u2", "i": "<i4", "I": "<u4", "f": "<f4",
          "d": "<f8", "n": "S4", "N": "S16", "Z": "S64", "c": "<i2", "C": "<u2", "e": "<i4", "E": "<u4", "L": "<i4",
          "M": "u1", "q": "<i8", "Q": "<u8"}
SCALES = {"c": 0.01, "C": 0.01, "e": 0.01, "E": 0.01, "L": 1e-7}  # centi-units, and degrees of latitude/longitude
_STRINGS = "nNZ"
_NAME = re.compile(r"^[A-Za-z0-9_]{1,4}$")
_SNIFF = 1 << 16  # where the first message must be found for a binary log that does not start with one

GPS_STATUS = {0: "none", 1: "none", 2: "single", 3: "single", 4: "single", 5: "float", 6: "fix"}  # Status of GPS
MESSAGES = {"cam": "CAM", "trig": "TRIG"}
NEAREST_GPS_S = 1.0  # status and accuracy of a shot from the GPS and GPA messages this close to it
CLOCK_CHECK_S = 0.5  # a shot whose GPS time is this far from the boot clock of the autopilot (put in GPS time by the
# GPS messages) has a wrong GPS time: not valid, never paired
_WEEK = 604800
_ANTENNA_OFFSET = re.compile(r"^GPS(_POS\d_|\d_POS_)[XYZ]$")  # where the antenna is in the body of the drone


class _Format:
    """One definition of a message type (an FMT message)."""

    def __init__(self, type_id: int, length: int, name: str, chars: str, columns: list[str]):
        self.type, self.length, self.name, self.chars = type_id, length, name, chars
        self.columns = _unique(columns)
        self.reason = _check(length, name, chars, columns)
        self.dtype = None
        if self.reason is None:
            self.dtype = np.dtype({"names": ["_head"] + self.columns,
                                   "formats": ["V3"] + [_CHARS[c] for c in chars]}, align=False)
            if self.dtype.itemsize != length:
                self.reason = f"its fields take {self.dtype.itemsize - 3} bytes, not {length - 3}"
                self.dtype = None


def _unique(columns: list[str]) -> list[str]:
    out = []
    for i, c in enumerate(columns):
        c = c.strip() or f"field{i}"
        while c in out or c == "_head":
            c += "_2"
        out.append(c)
    return out


def _check(length: int, name: str, chars: str, columns: list[str]) -> str | None:
    if not _NAME.match(name):
        return "a name that is not one"
    bad = sorted(set(chars) - set(_CHARS))
    if bad or not chars:
        return f"unknown format characters {''.join(bad)!r}" if bad else "no fields"
    if len(columns) != len(chars):
        return f"{len(chars)} format characters for {len(columns)} fields"
    if length < 3:
        return f"a length of {length} bytes"
    return None


def _text(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


class DataFlash:
    """An ArduPilot DataFlash log as read by :func:`read_dataflash`.

    ``kind`` is ``"binary"`` or ``"text"``; ``formats`` the last definition of each message by name (``type``,
    ``length``, ``format``, ``columns``); ``units`` for each message with an FMTU the unit (the label of its UNIT id)
    and the multiplier (the value of its MULT id) of each field, None for none (id ``-``, an unknown id, a multiplier
    that is not a positive number). ``truncated`` counts the messages cut at the end of the file (one at most: the
    logger stopped while writing it), ``skipped_bytes`` the bytes that were no message (damage, a type without a
    format) and ``damaged`` the places where they were; ``bad_formats`` the FMT messages that cannot be read, by name,
    with the reason, ``undecoded`` the messages of those types (skipped by their length), ``bad_lines`` the lines of a
    text log that are no message of a known format. Close it (or use it in a ``with``) to release the file."""

    def __init__(self, path: Path, kind: str):
        self.path, self.kind = path, kind
        self.formats: dict[str, dict] = {}
        self.units: dict[str, dict[str, dict]] = {}
        self.truncated = self.skipped_bytes = self.damaged = self.undecoded = self.bad_lines = 0
        self.bad_formats: dict[str, str] = {}
        self._segments: dict[str, list[tuple[_Format, object]]] = {}  # by name: (format, offsets or rows)
        self._history: dict[int, list[tuple[int, str]]] = {}  # type -> (position, name) of each definition
        self._buffer = None
        self._mmap = None

    # --- reading ---------------------------------------------------------------------------------------------

    def _define(self, fmt: _Format, position: int) -> object:
        if fmt.reason is not None:
            self.bad_formats[fmt.name if _NAME.match(fmt.name) else repr(fmt.name)] = fmt.reason
        else:
            self.formats[fmt.name] = {"type": fmt.type, "length": fmt.length, "format": fmt.chars,
                                      "columns": list(fmt.columns)}
            self._history.setdefault(fmt.type, []).append((position, fmt.name))
        store = array("q") if self.kind == "binary" else []
        self._segments.setdefault(fmt.name, []).append((fmt, store))
        return store

    def _read_binary(self, mm) -> None:
        n = len(mm)
        lengths = [0] * 256
        lengths[FMT_TYPE] = FMT_LENGTH
        stores: list = [None] * 256
        formats: list = [None] * 256
        self._define(_Format(FMT_TYPE, FMT_LENGTH, "FMT", _FMT_FORMAT, list(_FMT_COLUMNS)), 0)
        fmt_store = self._segments["FMT"][0][1]
        pos = 0
        while pos + 3 <= n:
            if mm[pos] == 0xA3 and mm[pos + 1] == 0x95:
                kind = mm[pos + 2]
                length = lengths[kind]
                if length:
                    end = pos + length
                    if end > n:
                        self.truncated += 1
                        return
                    if kind == FMT_TYPE:
                        fmt_store.append(pos)
                        self._binary_format(mm[pos + 3:end], pos, lengths, stores, formats)
                    else:
                        stores[kind].append(pos)
                        if formats[kind].reason is not None:
                            self.undecoded += 1
                    pos = end
                    continue
            nxt = mm.find(HEAD, pos + 1)
            while nxt >= 0 and not self._plausible(mm, nxt, lengths):
                nxt = mm.find(HEAD, nxt + 1)
            if nxt < 0:
                self.skipped_bytes += n - pos
                self.damaged += 1
                return
            self.skipped_bytes += nxt - pos
            self.damaged += 1
            pos = nxt
        if pos < n:  # a header cut
            self.truncated += 1

    @staticmethod
    def _plausible(mm, pos: int, lengths: list[int]) -> bool:
        """After damage, a header is taken as one when its type is known and another header (or the end) follows."""
        if pos + 3 > len(mm):
            return False
        length = lengths[mm[pos + 2]]
        if not length:
            return False
        end = pos + length
        return end >= len(mm) or (end + 2 <= len(mm) and mm[end:end + 2] == HEAD)

    def _binary_format(self, payload: bytes, pos: int, lengths, stores, formats) -> None:
        type_id, length = payload[0], payload[1]
        name, chars, columns = _text(payload[2:6]), _text(payload[6:22]), _text(payload[22:86])
        if type_id == FMT_TYPE:
            if (length, name, chars) != (FMT_LENGTH, "FMT", _FMT_FORMAT):
                self.bad_formats["FMT"] = "the format of FMT itself cannot change: kept as it is"
            return
        fmt = _Format(type_id, length, name, chars, columns.split(",") if columns else [])
        if fmt.reason is not None and length < 3:
            lengths[type_id], stores[type_id], formats[type_id] = 0, None, None
            self.bad_formats[name if _NAME.match(name) else repr(name)] = fmt.reason
            return
        lengths[type_id] = length
        stores[type_id] = self._define(fmt, pos)
        formats[type_id] = fmt

    def _read_text(self, lines: list[str], ends_with_newline: bool) -> None:
        current: dict[str, tuple[_Format, list]] = {}
        last = len(lines) - 1
        for number, line in enumerate(lines):
            parts = [p.strip() for p in line.split(",")]
            name = parts[0]
            if not name:
                continue
            cut = number == last and not ends_with_newline
            if name == "FMT":
                try:
                    type_id, msg, chars = int(parts[1]), parts[3], parts[4]
                    int(parts[2])  # the length: the fields of a text log are not counted in bytes
                except (IndexError, ValueError):
                    self.truncated += cut
                    self.bad_lines += not cut
                    continue
                if type_id == FMT_TYPE:
                    continue
                fmt = _Format(type_id, 3 + _size(chars), msg, chars, parts[5:])
                current[msg] = (fmt, self._define(fmt, number))
                continue
            entry = current.get(name)
            if entry is None:
                self.bad_lines += 1
                continue
            fmt, rows = entry
            values = parts[1:]
            if name in ("UNIT", "MULT") and len(values) > 1 and len(values[1]) == 1:
                values[1] = str(ord(values[1]))  # an id written as its character (a digit too: no code is below 32)
            if len(values) > len(fmt.chars) and fmt.chars[-1] in _STRINGS:
                values = values[:len(fmt.chars) - 1] + [",".join(values[len(fmt.chars) - 1:])]
            if len(values) != len(fmt.chars) or not _parses(fmt.chars, values):
                self.truncated += cut
                self.bad_lines += not cut
                continue
            if fmt.reason is not None:
                self.undecoded += 1
                continue
            rows.append((number, values))

    def _resolve_units(self) -> None:
        if not (self.count("FMTU") and self.count("UNIT") and self.count("MULT")):
            return
        labels = {chr(int(i) & 0xFF): str(lab) for i, lab in zip(*_cols(self.messages("UNIT", False), "Id", "Label"))}
        mults = {}
        for i, m in zip(*_cols(self.messages("MULT", False), "Id", "Mult")):
            m = float(m)
            mults[chr(int(i) & 0xFF)] = m if math.isfinite(m) and m > 0 else None
        fmtu = self.messages("FMTU", False)
        positions = self._positions("FMTU")
        for (type_id, unit_ids, mult_ids), position in zip(zip(*_cols(fmtu, "FmtType", "UnitIds", "MultIds")),
                                                           positions):
            name = self._name_at(int(type_id), position)
            if name is None or name not in self.formats:
                continue
            columns = self.formats[name]["columns"]
            self.units[name] = {c: {"unit": labels.get(unit_ids[k]) if k < len(unit_ids) and unit_ids[k] != "-"
                                    else None,
                                    "mult": mults.get(mult_ids[k]) if k < len(mult_ids) and mult_ids[k] != "-"
                                    else None}
                                for k, c in enumerate(columns)}

    def _positions(self, name: str) -> list[int]:
        out: list[int] = []
        for fmt, store in self._segments.get(name, []):
            if fmt.reason is None:
                out += list(store) if self.kind == "binary" else [n for n, _v in store]
        return out

    def _name_at(self, type_id: int, position: int) -> str | None:
        name = None
        for at, defined in self._history.get(type_id, []):
            if at <= position:
                name = defined
        if name is None and self.kind == "text":  # a text log names its messages: the type of the last definition
            defs = self._history.get(type_id, [])
            name = defs[-1][1] if defs else None
        return name

    # --- the messages ----------------------------------------------------------------------------------------

    def count(self, name: str) -> int:
        """How many messages ``name`` the log has that can be read (0 for a name it does not define)."""
        return sum(len(store) for fmt, store in self._segments.get(name, []) if fmt.reason is None)

    def messages(self, name: str, multipliers: bool = True) -> np.ndarray:
        """The messages ``name`` as a numpy structured array, one field per column, in the order of the log: the
        fields of the characters ``c C e E`` in units (x 0.01), ``L`` in degrees (x 1e-7), with ``multipliers`` also
        the other numeric fields multiplied by the MULT their FMTU gives (``TimeUS`` in seconds, a field in mm in
        metres); never both - the multiplier ArduPilot gives a field of ``L`` or ``e`` is the one its character
        applies. Strings as text, ``a`` as 32 integers. A message defined again with other fields gives the columns of
        all its definitions, NaN (or ``""``) where a definition has not the field. ``KeyError`` for a message the log
        does not define."""
        segments = [(fmt, store) for fmt, store in self._segments.get(name, []) if fmt.reason is None]
        if name not in self.formats or not segments:
            raise KeyError(f"{self.path.name}: the log defines no message {name}")
        with np.errstate(invalid="ignore", over="ignore"):  # damaged floats (a signalling NaN) are read as they are
            parts = [self._decode(fmt, store, multipliers) for fmt, store in segments]
        if len(parts) == 1:
            return parts[0]
        names: list[str] = []
        for p in parts:
            names += [c for c in p.dtype.names if c not in names]
        formats = []
        for c in names:
            types = [p.dtype[c] for p in parts if c in p.dtype.names]
            missing = any(c not in p.dtype.names for p in parts)
            if any(t.kind == "U" for t in types):
                formats.append(f"U{max(t.itemsize // 4 for t in types if t.kind == 'U')}")
            elif types[0].shape:
                formats.append(types[0])
            else:
                t = np.result_type(*types)
                formats.append(np.float64 if missing or t.kind not in "iufb" else t)
        out = np.zeros(sum(len(p) for p in parts), dtype=list(zip(names, formats)))
        start = 0
        for p in parts:
            block = out[start:start + len(p)]
            for c, f in zip(names, formats):
                if c in p.dtype.names:
                    block[c] = p[c]
                elif np.dtype(f).kind == "f":
                    block[c] = np.nan
            start += len(p)
        return out

    def _decode(self, fmt: _Format, store, multipliers: bool) -> np.ndarray:
        units = self.units.get(fmt.name, {}) if multipliers else {}
        out_types = []
        for c, column in zip(fmt.chars, fmt.columns):
            mult = (units.get(column) or {}).get("mult")
            if c in _STRINGS:
                out_types.append(f"U{np.dtype(_CHARS[c]).itemsize}")
            elif c == "a":
                out_types.append(_CHARS[c])
            elif c in SCALES or c in "fd" or (mult is not None and mult != 1.0):
                out_types.append(np.float64)
            else:
                out_types.append(_CHARS[c].replace("<", "") if self.kind == "binary" else np.int64)
        out = np.zeros(len(store), dtype=list(zip(fmt.columns, out_types)))
        if not len(store):
            return out
        raw = self._raw_binary(fmt, store) if self.kind == "binary" else None
        rows = None if raw is not None else [v for _n, v in store]
        for k, (c, column) in enumerate(zip(fmt.chars, fmt.columns)):
            if raw is not None:
                values = raw[column]
                if c in _STRINGS:
                    values = np.array([_text(bytes(v)) for v in values], dtype=out.dtype[column])
            else:
                values = _text_column(c, [r[k] for r in rows])
            if c in SCALES and raw is not None:
                values = values * SCALES[c]
            mult = (units.get(column) or {}).get("mult")
            if c not in SCALES and c not in _STRINGS and c != "a" and mult is not None and mult != 1.0:
                values = np.asarray(values, dtype=np.float64) * mult
            out[column] = values
        return out

    def _raw_binary(self, fmt: _Format, store) -> np.ndarray:
        offsets = np.frombuffer(store, dtype=np.int64) if len(store) else np.zeros(0, np.int64)
        if self._buffer is None:
            raise ValueError(f"{self.path.name}: the log is closed")
        buf = np.frombuffer(self._buffer, dtype=np.uint8)
        block = np.empty((len(offsets), fmt.length), dtype=np.uint8)
        for k in range(fmt.length):
            block[:, k] = buf[offsets + k]
        return block.view(fmt.dtype).reshape(len(offsets))

    @property
    def warnings(self) -> list[str]:
        """What a reader of the log should know of it, as sentences."""
        out = []
        if self.truncated:
            out.append("the last message of the log is cut (the logger stopped while writing it): left out")
        if self.skipped_bytes:
            out.append(f"{self.skipped_bytes} bytes in {self.damaged} place{'s' if self.damaged > 1 else ''} that are "
                       "no message (a damaged log) skipped")
        for name, reason in self.bad_formats.items():
            out.append(f"the format of {name} cannot be read ({reason}): its messages skipped")
        if self.bad_lines:
            out.append(f"{self.bad_lines} line{'s' if self.bad_lines > 1 else ''} of the log that are no message of a "
                       "known format skipped")
        return out

    def close(self) -> None:
        self._buffer = None
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None

    def __enter__(self) -> DataFlash:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _size(chars: str) -> int:
    try:
        return sum(np.dtype(_CHARS[c]).itemsize for c in chars)
    except KeyError:
        return 0


def _cols(arr: np.ndarray, *names: str) -> list:
    return [arr[n] if n in arr.dtype.names else [] for n in names]


def _parses(chars: str, values: list[str]) -> bool:
    try:
        for c, v in zip(chars, values):
            if c == "a":
                [int(x) for x in v.split()]
            elif c not in _STRINGS and not (math.isfinite(float(v)) or c in "fd"):
                return False
    except ValueError:
        return False
    return True


def _text_column(c: str, values: list[str]) -> np.ndarray:
    """A column of a text log: its values as written (the characters of :data:`SCALES` already scaled)."""
    if c in _STRINGS:
        return np.array(values, dtype=f"U{np.dtype(_CHARS[c]).itemsize}")
    if c == "a":
        out = np.zeros((len(values), 32), dtype=np.int16)
        for i, v in enumerate(values):
            try:
                items = [int(x) for x in v.split()][:32]
            except ValueError:
                continue
            out[i, :len(items)] = items
        return out
    floats = np.array([_number(v) for v in values], dtype=np.float64)
    if c in SCALES or c in "fd":
        return floats
    try:
        return np.array(values, dtype=np.int64)
    except (ValueError, OverflowError):
        return np.clip(np.nan_to_num(floats), -9.2e18, 9.2e18).astype(np.int64)


def _number(v: str) -> float:
    try:
        return float(v)
    except ValueError:
        return math.nan


def read_dataflash(path: str | Path) -> DataFlash:
    """An ArduPilot DataFlash log, binary (it starts with 0xA3 0x95, or has its first message within the first 64 KB)
    or text (lines ``FMT, ...``). A ``ValueError`` for an empty file or one that is neither, or that defines no
    message. The binary file is mapped, not read in memory: close the result."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"{path}: no such file")
    if path.stat().st_size == 0:
        raise ValueError(f"{path.name}: the file is empty")
    with path.open("rb") as f:
        head = f.read(_SNIFF)
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if head[:2] != HEAD and stripped[:4] == b"FMT,":
        df = DataFlash(path, "text")
        raw = path.read_bytes().decode("utf-8", "replace")
        df._read_text(raw.splitlines(), raw.endswith(("\n", "\r")))
    elif head[:2] == HEAD or HEAD + bytes([FMT_TYPE]) in head:
        df = DataFlash(path, "binary")
        with path.open("rb") as f:
            df._mmap = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        df._buffer = df._mmap
        try:
            df._read_binary(df._mmap)
        except Exception:
            df.close()
            raise
    else:
        raise ValueError(f"{path.name}: not an ArduPilot DataFlash log (binary or text)")
    if not any(name != "FMT" for name in df.formats):
        df.close()
        raise ValueError(f"{path.name}: not an ArduPilot DataFlash log - no message formats in it")
    df._resolve_units()
    return df


# --- the camera shots ------------------------------------------------------------------------------------------


def _column(arr: np.ndarray, *names: str) -> np.ndarray | None:
    for n in names:
        if n in arr.dtype.names:
            return arr[n]
    return None


def _nearest(times: np.ndarray, at: float, within: float) -> int | None:
    """The index in the sorted ``times`` nearest to ``at``, if within ``within``."""
    if not len(times):
        return None
    k = int(np.searchsorted(times, at))
    best = min((i for i in (k - 1, k) if 0 <= i < len(times)), key=lambda i: abs(times[i] - at))
    return best if abs(times[best] - at) <= within else None


def _gps_rows(df: DataFlash) -> dict | None:
    """The GPS messages of the receiver in use (``U``; else instance 0), with their boot and GPS times."""
    if not df.count("GPS"):
        return None
    gps = df.messages("GPS", multipliers=False)
    keep = np.ones(len(gps), bool)
    use, inst = _column(gps, "U"), _column(gps, "I")
    if use is not None and np.any(use != 0):
        keep = use != 0
    elif inst is not None:
        keep = inst == 0
    gps = gps[keep]
    boot = _column(gps, "TimeUS")
    week, ms = _column(gps, "GWk", "Week"), _column(gps, "GMS", "TimeMS")
    status = _column(gps, "Status")
    if boot is None or status is None:
        return None
    order = np.argsort(boot, kind="stable")
    gps = gps[order]
    rows = {"boot": gps["TimeUS"].astype(np.float64) * 1e-6, "status": status[order].astype(int),
            "instance": (_column(gps, "I") if "I" in gps.dtype.names else np.zeros(len(gps))).astype(int)}
    if week is not None and ms is not None:
        w, m = week[order].astype(np.float64), ms[order].astype(np.float64)
        good = w > 0
        if good.any():
            rows["offset"] = float(np.median(w[good] * _WEEK + m[good] / 1000.0 - rows["boot"][good]))
    return rows


def _gpa_rows(df: DataFlash) -> dict | None:
    if not df.count("GPA"):
        return None
    raw, gpa = df.messages("GPA", multipliers=False), df.messages("GPA")
    boot, hacc, vacc = _column(raw, "TimeUS"), _column(gpa, "HAcc"), _column(gpa, "VAcc")
    if boot is None or hacc is None or vacc is None:
        return None
    inst = _column(raw, "I")
    order = np.argsort(boot, kind="stable")
    return {"boot": boot[order].astype(np.float64) * 1e-6, "hacc": hacc[order].astype(np.float64),
            "vacc": vacc[order].astype(np.float64),
            "instance": (inst[order] if inst is not None else np.full(len(raw), -1)).astype(int)}


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
    """The camera shots of an ArduPilot log (:func:`read_dataflash`): its CAM messages - the shots the camera
    confirmed on its feedback pin, or the triggers when no pin is set - or, with ``message="trig"`` or ``"auto"`` in a
    log without CAM, its TRIG messages (the commands to shoot, not the shots: a warning). ``camera`` chooses the
    camera (``I``) of a log with several; without it they are a ``ValueError`` that lists them.

    Each shot: ``index`` the image number ``Img`` (None in logs without it); ``gps_time`` = ``GPSWeek`` x 604800 +
    ``GPSTime`` / 1000 - the GPS week and the milliseconds of the week the autopilot gives the moment it logged the
    shot from its own clock, which it keeps on GPS time with the GPS messages: to the millisecond (1 cm at 10 m/s),
    no better than that synchronisation (a few ms; older firmware may give the time of the last GPS fix, up to its
    period). That time is checked against the boot clock (``TimeUS``) put in GPS time by the GPS messages in use:
    shots more than :data:`CLOCK_CHECK_S` from it are not valid (a wrong week, a stale time), with a warning. Rows
    without a GPS time (``GPSWeek`` 0: no fix yet) or without a position are left out and counted. ``antenna`` is
    ``(Lat, Lng, Alt)``, ``height="msl"``: ``Alt`` is the altitude above the sea level the autopilot estimates
    (never ellipsoidal); it is the position of the autopilot's origin, which is the antenna unless ``GPS_POS1_X/Y/Z``
    place it elsewhere in the body (a warning when the log's parameters do). ``attitude`` is ``(R, P, Y)`` in degrees
    (yaw 0-360 from north). ``status`` comes from ``Status`` of the GPS message of the receiver in use (``U``) nearest
    in time (``TimeUS``) within :data:`NEAREST_GPS_S` (6 fix, 5 float, 2-4 single, 0-1 none), ``std`` (north, east,
    up) from ``HAcc``, ``HAcc``, ``VAcc`` of the GPA of the same receiver nearest within that time; None without them.
    ``source`` keeps the message, ``instance``, ``img``, ``gps_week``, ``tow`` (s), ``tow_ms``, ``time_us``,
    ``rel_alt``, ``gps_alt``, ``gps_status`` and ``gps_instance``."""
    path = Path(path)
    with read_dataflash(path) as df:
        name = _choose(df, message)
        rows = df.messages(name, multipliers=False)
        gps, gpa = _gps_rows(df), _gpa_rows(df)
        params = df.messages("PARM", multipliers=False) if df.count("PARM") else None
        warnings = list(df.warnings)
    kind = "ardupilot_cam" if name == "CAM" else "ardupilot_trig"
    week, tow_ms = _column(rows, "GPSWeek"), _column(rows, "GPSTime")
    lat, lon, alt = _column(rows, "Lat"), _column(rows, "Lng"), _column(rows, "Alt")
    missing = [c for c, v in (("GPSWeek", week), ("GPSTime", tow_ms), ("Lat", lat), ("Lng", lon), ("Alt", alt))
               if v is None]
    if missing:
        raise ValueError(f"{path.name}: the {name} messages have no {', '.join(missing)}: not a format this reader "
                         "knows")
    instances = _column(rows, "I")
    instances = np.zeros(len(rows), int) if instances is None else instances.astype(int)
    found = sorted(set(instances.tolist()))
    if camera is None and len(found) > 1:
        raise ValueError(f"{path.name}: the log has {len(found)} cameras ({name} instances "
                         f"{', '.join(map(str, found))}): choose one")
    if camera is not None and camera not in found:
        raise ValueError(f"{path.name}: no camera {camera} in the log (cameras {', '.join(map(str, found))})")
    chosen = found[0] if camera is None else camera
    img = _column(rows, "Img")
    roll, pitch, yaw = _column(rows, "R", "Roll"), _column(rows, "P", "Pitch"), _column(rows, "Y", "Yaw")
    boot = _column(rows, "TimeUS")
    rel, galt = _column(rows, "RelAlt"), _column(rows, "GPSAlt")
    shots, no_fix, no_position, no_gps, off_clock = [], 0, 0, 0, 0
    for k in np.flatnonzero(instances == chosen):
        w, ms = int(week[k]), int(tow_ms[k])
        if w <= 0 or not 0 <= ms < _WEEK * 1000:
            no_fix += 1
            continue
        la, lo, h = float(lat[k]), float(lon[k]), float(alt[k])
        if not (abs(la) <= 90 and abs(lo) <= 180 and math.isfinite(h)) or (la == 0 and lo == 0):
            no_position += 1
            continue
        t = w * float(_WEEK) + ms / 1000.0
        source = {"file": str(path), "kind": kind, "message": name, "row": int(k), "instance": int(instances[k]),
                  "img": int(img[k]) if img is not None else None, "gps_week": w, "tow": ms / 1000.0, "tow_ms": ms}
        if rel is not None:
            source["rel_alt"] = float(rel[k])
        if galt is not None:
            source["gps_alt"] = float(galt[k])
        attitude = (float(roll[k]), float(pitch[k]), float(yaw[k])) if all(
            a is not None for a in (roll, pitch, yaw)) else None
        if attitude is not None and not all(map(math.isfinite, attitude)):
            attitude = None
        status = std = None
        valid = True
        if boot is not None:
            at = int(boot[k])
            source["time_us"] = at
            status, gps_instance = _gps_at(gps, at * 1e-6, source)
            if "gps_status" not in source:
                no_gps += 1
            std = _std_at(gpa, at * 1e-6, gps_instance)
            if gps is not None and "offset" in gps:
                apart = t - (at * 1e-6 + gps["offset"])
                if abs(apart) > CLOCK_CHECK_S:
                    source["clock_disagrees_s"] = round(apart, 3)
                    valid = False
                    off_clock += 1
        shots.append(Shot(index=int(img[k]) if img is not None else None, gps_time=t, antenna=(la, lo, h),
                          height="msl", std=std, status=status, attitude=attitude, valid=valid, source=source))
    shots.sort(key=lambda s: s.gps_time)
    if name == "TRIG":
        warnings.append("TRIG messages are the commands to shoot, not the shots: the camera may expose later or not "
                        "at all (a feedback pin gives CAM messages, the confirmed shots)")
    if no_fix:
        warnings.append(f"{no_fix} {name} message{_plural(no_fix)} without a GPS time (GPSWeek 0: no GPS fix yet) "
                        "left out")
    if no_position:
        warnings.append(f"{no_position} {name} message{_plural(no_position)} without a position left out")
    if no_gps:
        warnings.append(f"{no_gps} {name} shot{_plural(no_gps)} with no GPS message of the receiver in use within "
                        f"{NEAREST_GPS_S:g} s: no status")
    if off_clock:
        warnings.append(f"{off_clock} {name} shot{_plural(off_clock)} whose GPS time disagrees with the boot clock of "
                        f"the autopilot by more than {CLOCK_CHECK_S:g} s (a wrong week or a stale time): not valid, "
                        "never paired")
    warnings += _antenna_offset(params)
    return ShotSet(shots=shots, kind=kind, name=path.name, warnings=warnings, path=str(path))


def _plural(n: int) -> str:
    return "s" if n > 1 else ""


def _gps_at(gps: dict | None, at: float, source: dict) -> tuple[str | None, int | None]:
    if gps is None:
        return None, None
    k = _nearest(gps["boot"], at, NEAREST_GPS_S)
    if k is None:
        return None, None
    raw = int(gps["status"][k])
    source["gps_status"] = raw
    source["gps_instance"] = int(gps["instance"][k])
    return GPS_STATUS.get(raw), int(gps["instance"][k])


def _std_at(gpa: dict | None, at: float, instance: int | None) -> tuple[float, float, float] | None:
    if gpa is None or instance is None:
        return None
    mine = (gpa["instance"] == instance) | (gpa["instance"] == -1)
    k = _nearest(gpa["boot"][mine], at, NEAREST_GPS_S)
    if k is None:
        return None
    h, v = float(gpa["hacc"][mine][k]), float(gpa["vacc"][mine][k])
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


def cameras(path: str | Path) -> list[dict]:
    """What the cameras of an ArduPilot log recorded, one dict per instance ``I``: ``cam`` and ``trig`` (how many
    CAM and TRIG messages), ``no_fix`` (messages without a GPS time, of CAM when there are some, else TRIG),
    ``first`` and ``last`` (their GPS times), ``first_img`` and ``last_img``."""
    path = Path(path)
    with read_dataflash(path) as df:
        tables = {name: df.messages(name, multipliers=False) for name in ("CAM", "TRIG") if df.count(name)}
    found: set[int] = set()
    for rows in tables.values():
        inst = _column(rows, "I")
        found |= set((inst if inst is not None else np.zeros(len(rows), int)).astype(int).tolist())
    out = []
    for i in sorted(found):
        entry = {"instance": i, "cam": 0, "trig": 0, "no_fix": 0, "first": None, "last": None, "first_img": None,
                 "last_img": None}
        for name in ("TRIG", "CAM"):  # CAM last: its times win
            rows = tables.get(name)
            if rows is None:
                continue
            inst = _column(rows, "I")
            mine = rows[(inst.astype(int) == i) if inst is not None else np.ones(len(rows), bool)]
            entry[name.lower()] = len(mine)
            if not len(mine):
                continue
            week, ms, img = _column(mine, "GPSWeek"), _column(mine, "GPSTime"), _column(mine, "Img")
            if week is None or ms is None:
                continue
            good = week.astype(np.int64) > 0
            times = week[good].astype(np.float64) * _WEEK + ms[good].astype(np.float64) / 1000.0
            entry["no_fix"] = int((~good).sum())
            entry["first"] = float(times.min()) if len(times) else None
            entry["last"] = float(times.max()) if len(times) else None
            if img is not None:
                entry["first_img"], entry["last_img"] = int(img.min()), int(img.max())
        out.append(entry)
    return out
