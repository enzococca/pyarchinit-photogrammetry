"""An ArduPilot DataFlash log, binary (``.bin``) or text (``.log`` of Mission Planner), read as numpy arrays.

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
_ALWAYS = ("FMT", "FMTU", "UNIT", "MULT")  # kept whatever the names asked: they describe the others
_SNIFF = 1 << 16
_TEXT_BLOCK = 50_000  # lines of a text log decoded at a time  # where the first message must be found for a binary log that does not start with one



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
    text log that are no message of a known format. ``names`` are the messages kept (None: all): the others are only
    counted (in a text log without checking their values). Close it (or use it in a ``with``) to release the file."""

    def __init__(self, path: Path, kind: str, names=None):
        self.path, self.kind = path, kind
        self.names = None if names is None else set(names) | set(_ALWAYS)
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
        if self.names is not None and fmt.name not in self.names:
            store = _Counter()
        else:
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

    def _read_text(self, f) -> None:
        """The lines of a text log, read one at a time from the binary file ``f`` (split on newlines only: a string
        can hold any other character); only the messages of :attr:`names` are kept."""
        current: dict[str, tuple[_Format, object]] = {}
        for number, raw in enumerate(f):
            cut = not raw.endswith(b"\n")  # the last line, without its end: the logger stopped while writing it
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            name = line.split(",", 1)[0].strip()
            if not name:
                continue
            if name == "FMT":
                parts = [p.strip() for p in line.split(",")]
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
            if isinstance(rows, _Counter):
                rows.append(None)
                continue
            values = _values(name, fmt.chars, line)
            if values is None or not _parses(fmt.chars, values):
                self.truncated += cut
                self.bad_lines += not cut
                continue
            if fmt.reason is not None:
                self.undecoded += 1
                continue
            rows.append((number, line))  # the line, not its values: a fraction of the memory, split again to decode

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
        if self.names is not None and name not in self.names:
            raise KeyError(f"{self.path.name}: the messages {name} were not kept (names)")
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
        if self.kind == "binary":
            self._fill(fmt, out, self._raw_binary(fmt, store), None, units)
        else:  # a block of lines at a time: their values as strings take many times the memory of the line
            for start in range(0, len(store), _TEXT_BLOCK):
                rows = [_values(fmt.name, fmt.chars, line) for _n, line in store[start:start + _TEXT_BLOCK]]
                self._fill(fmt, out[start:start + len(rows)], None, rows, units)
        return out

    @staticmethod
    def _fill(fmt: _Format, out: np.ndarray, raw: np.ndarray | None, rows: list | None, units: dict) -> None:
        for k, (c, column) in enumerate(zip(fmt.chars, fmt.columns)):
            if raw is not None:
                values = raw[column]
                if c in _STRINGS:
                    values = np.array([_text(bytes(v)) for v in values], dtype=out.dtype[column])
                elif c in SCALES:
                    values = values * SCALES[c]
            else:
                values = _text_column(c, [r[k] for r in rows])
            mult = (units.get(column) or {}).get("mult")
            if c not in SCALES and c not in _STRINGS and c != "a" and mult is not None and mult != 1.0:
                values = np.asarray(values, dtype=np.float64) * mult
            out[column] = values

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


class _Counter:
    """In place of the offsets or rows of a message not kept: how many there are."""

    __slots__ = ("n",)

    def __init__(self) -> None:
        self.n = 0

    def append(self, _item) -> None:
        self.n += 1

    def __len__(self) -> int:
        return self.n


def _size(chars: str) -> int:
    try:
        return sum(np.dtype(_CHARS[c]).itemsize for c in chars)
    except KeyError:
        return 0


def _cols(arr: np.ndarray, *names: str) -> list:
    return [arr[n] if n in arr.dtype.names else [] for n in names]


def _values(name: str, chars: str, line: str) -> list[str] | None:
    """The values of a line of a text log, None when there are not as many as ``chars``: a last string may hold
    commas; the id of UNIT and MULT may be written as its character (a digit too: no code is below 32)."""
    values = [p.strip() for p in line.split(",")][1:]
    if name in ("UNIT", "MULT") and len(values) > 1 and len(values[1]) == 1:
        values[1] = str(ord(values[1]))
    if len(values) > len(chars) and chars[-1] in _STRINGS:
        values = values[:len(chars) - 1] + [line.split(",", len(chars))[-1].strip()]
    return values if len(values) == len(chars) else None


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


def read_dataflash(path: str | Path, names=None) -> DataFlash:
    """An ArduPilot DataFlash log, binary (it starts with 0xA3 0x95, or has its first message within the first 64 KB)
    or text (lines ``FMT, ...``), keeping the messages ``names`` (None: all; FMT, FMTU, UNIT and MULT always) and only
    counting the others. A ``ValueError`` for an empty file or one that is neither, or that defines no message. The
    binary file is mapped, not read in memory (the offsets of the messages kept take 8 bytes each), and a text one
    read a line at a time, keeping the lines of the messages kept (a few times their size in memory): close the
    result."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"{path}: no such file")
    if path.stat().st_size == 0:
        raise ValueError(f"{path.name}: the file is empty")
    with path.open("rb") as f:
        head = f.read(_SNIFF)
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if head[:2] != HEAD and stripped[:4] == b"FMT,":
        df = DataFlash(path, "text", names)
        with path.open("rb") as f:
            df._read_text(f)
    elif head[:2] == HEAD or HEAD + bytes([FMT_TYPE]) in head:
        df = DataFlash(path, "binary", names)
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
