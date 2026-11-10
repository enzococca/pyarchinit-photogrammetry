"""A PX4 ULog file (``.ulg``) read as numpy arrays.

The reader is written from the published specification of the format, "ULog File Format" of the PX4 guide
(https://docs.px4.io/main/en/dev_log/ulog_file_format.html, CC BY 4.0), with no code of PX4 or pyulog. A file is a
header of 16 bytes - the magic ``ULog`` 0x01 0x12 0x35, the version (1) and the boot time (us) the log started at -
and a sequence of messages, each a header of 3 bytes (``msg_size`` uint16, ``msg_type`` a character) and
``msg_size`` bytes, all little-endian:

- ``B`` flag bits (only first after the header, optional; anywhere else ignored): 8 compatible and 8 incompatible
  flag bytes and three file offsets of appended data; bit 0 of the first incompatible byte says data was appended at
  the non-zero offsets, each where the file ended when that data was appended - the log before the first offset and
  each block before the next may stop in the middle of a message, reading goes on at the offset (an offset not
  beyond the one before, or past the end of the file, is not taken); any other incompatible bit: a log this reader
  cannot read.
- ``F`` the format of a topic: ``name:type field;type field;...``, types ``int8_t`` to ``uint64_t``, ``float``,
  ``double``, ``bool``, ``char`` or the name of another format (nested), each with an optional ``[n]`` array; the
  fields are packed with no padding of their own (padding is fields named ``_padding...``, the last of which a data
  message may leave out).
- ``I`` information (``type name`` key, value), ``M`` information over several messages (``is_continued``),
  ``P``/``Q`` parameters (and their defaults), ``L``/``C`` logged strings, ``S`` sync (8 magic bytes), ``O`` a
  dropout (``duration`` ms: data lost while the log could not write).
- ``A`` a subscription: ``multi_id`` (the instance of the topic), ``msg_id`` and the topic name; ``R`` its end.
- ``D`` data: ``msg_id`` and the fields of its topic, ``timestamp`` (uint64, us since boot) first.

Damaged data (a message of an unknown type or id, a size past the end) is skipped by looking for the next sync message,
counted in the warnings."""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

MAGIC = b"ULog\x01\x12\x35"
SYNC = bytes([0x2F, 0x73, 0x13, 0x20, 0x25, 0x0C, 0xBB, 0x12])
APPENDED = 1  # bit 0 of the first incompatible flag byte: data appended at appended_offsets
_TYPES = {"int8_t": "i1", "uint8_t": "u1", "int16_t": "<i2", "uint16_t": "<u2", "int32_t": "<i4", "uint32_t": "<u4",
          "int64_t": "<i8", "uint64_t": "<u8", "float": "<f4", "double": "<f8", "bool": "?", "char": "S1"}
_FIELD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")
_KNOWN = set(b"BFIMPQALCDSOR")


@dataclass
class ULog:
    """What a ULog file holds: ``version``, ``start_us`` (the boot time the log started), ``formats`` (each topic: its
    flat fields ``(name, numpy type)``), ``info`` and ``multi_info`` (the ``I`` and ``M`` messages, values decoded),
    ``params``, ``subscriptions`` (``msg_id`` -> ``(topic, multi_id)``), ``data`` ((topic, multi_id) -> structured
    array, of the topics read), ``counts`` (data messages of every subscription, read or not), ``dropouts`` and
    ``dropout_ms``, ``logged`` (``(level, timestamp, text)`` of the ``L`` and ``C`` messages), ``appended`` (whether
    data was appended), ``offsets_ignored`` (offsets of appended data not taken), ``damaged`` (bytes skipped to find the next sync)."""
    path: str
    version: int = 1
    start_us: int = 0
    formats: dict = field(default_factory=dict)
    info: dict = field(default_factory=dict)
    multi_info: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    subscriptions: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    dropouts: int = 0
    dropout_ms: int = 0
    logged: list = field(default_factory=list)
    appended: bool = False
    offsets_ignored: int = 0
    damaged: int = 0

    def topics(self) -> list[str]:
        """The topics with data messages, by name (each once)."""
        return sorted({name for (name, _m), n in self.counts.items() if n})

    def instances(self, topic: str) -> list[int]:
        """The ``multi_id`` of the instances of ``topic`` with data, in order."""
        return sorted(m for (name, m), n in self.counts.items() if name == topic and n)

    def get(self, topic: str, multi_id: int = 0) -> np.ndarray | None:
        """The data of one instance of ``topic`` read (None when it has none or was not asked for)."""
        return self.data.get((topic, multi_id))

    @property
    def warnings(self) -> list[str]:
        out = []
        if self.damaged:
            out.append(f"{Path(self.path).name}: {self.damaged} damaged bytes skipped (to the next sync message)")
        if self.offsets_ignored:
            out.append(f"{Path(self.path).name}: an offset of appended data before the data it follows or past the end "
                       "of the file: no appended data read from it on")
        if self.dropouts:
            out.append(f"{Path(self.path).name}: {self.dropouts} dropout{'s' if self.dropouts > 1 else ''} of the log "
                       f"({self.dropout_ms} ms in all): data lost while the log could not write")
        return out


def _value(type_: str, raw: bytes, formats: dict):
    """The value of an ``I``/``M``/``P`` message of type ``type_`` (``char[n]`` a string)."""
    m = re.match(r"^([A-Za-z0-9_]+)(?:\[(\d+)\])?$", type_)
    if not m:
        return raw
    base, n = m.group(1), m.group(2)
    if base == "char":
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    if base not in _TYPES:
        return raw
    dt = np.dtype(_TYPES[base])
    count = int(n) if n else 1
    if len(raw) < dt.itemsize * count:
        return raw
    values = np.frombuffer(raw[:dt.itemsize * count], dtype=dt)
    return values.tolist() if n else values[0].item()


def _flatten(name: str, formats: dict, prefix: str = "", depth: int = 0) -> list[tuple[str, object]]:
    """The fields of the format ``name`` as ``(name, numpy type)``: nested formats spelled out (``a.b``,
    ``a[0].b``), arrays of numbers as sub-arrays, ``char[n]`` as bytes."""
    if depth > 8 or name not in formats:
        raise ValueError(f"format {name!r} unknown or nested too deep")
    out = []
    for type_, n, fname in formats[name]:
        full = prefix + fname
        if type_ in _TYPES:
            if type_ == "char":
                out.append((full, f"S{n or 1}"))
            elif n:
                out.append((full, (_TYPES[type_], (n,))))
            else:
                out.append((full, _TYPES[type_]))
        else:
            for k in range(n or 1):
                inner = f"{full}[{k}]." if n else f"{full}."
                out += _flatten(type_, formats, inner, depth + 1)
    return out


def _parse_format(text: str) -> tuple[str, list[tuple[str, int, str]]]:
    name, _, body = text.partition(":")
    fields = []
    for part in body.split(";"):
        if not part.strip():
            continue
        m = _FIELD.match(part)
        if not m:
            raise ValueError(f"field {part!r} of format {name!r}")
        fields.append((m.group(1), int(m.group(2)) if m.group(2) else 0, m.group(3)))
    return name.strip(), fields


def read_ulog(path: str | Path, topics=None) -> ULog:
    """Read the ULog file ``path``: every definition, and the data of the ``topics`` (names; None: all) as structured
    arrays. A file that is not a ULog, of another version or with an incompatible flag this reader does not know, is a
    ``ValueError``."""
    path = Path(path)
    raw = path.read_bytes()
    if len(raw) < 16 or raw[:7] != MAGIC:
        raise ValueError(f"{path.name} is not a ULog file (no ULog header)")
    log = ULog(path=str(path), version=raw[7], start_us=struct.unpack_from("<Q", raw, 8)[0])
    if log.version > 1:
        raise ValueError(f"{path.name}: ULog version {log.version}, this reader knows version 1")
    state = _State(raw=raw, log=log, wanted=None if topics is None else set(topics))
    end = len(raw)
    pos = 16
    blocks = [(16, end)]  # (start, stop) of the log and of each block of appended data, in the file's order
    if end >= 19 and raw[18] == ord("B"):
        pos, blocks = _flag_bits(raw, log)
    for b, (start, block_end) in enumerate(blocks):
        last = b == len(blocks) - 1
        pos = max(pos, start)
        while pos + 3 <= block_end:
            size, kind = struct.unpack_from("<HB", raw, pos)
            stop = pos + 3 + size
            if kind in _KNOWN and stop > block_end and not last:
                break  # the block stops in the middle of a message: reading goes on at the next offset
            if kind not in _KNOWN or stop > block_end:
                found = raw.find(SYNC, pos + 1, block_end)
                if found < 0:
                    log.damaged += block_end - pos
                    break
                log.damaged += max(0, found - 3 - pos)  # up to the header of the sync message
                pos = found + len(SYNC)  # the message after the sync
                continue
            if kind != ord("B"):  # flag bits anywhere but first: ignored
                _message(state, kind, raw[pos + 3:stop])
            pos = stop
    for key, dt, parts in state.done + list(state.reading.values()):
        n = dt.itemsize
        buf = b"".join(p[:n] if len(p) >= n else p + b"\0" * (n - len(p)) for p in parts)
        arr = np.frombuffer(buf, dtype=dt).copy() if buf else np.zeros(0, dtype=dt)
        old = log.data.get(key)
        log.data[key] = arr if old is None else np.concatenate([old, arr])
    formats = state.formats
    log.formats = {name: _flatten(name, formats) for name in formats if _flattens(name, formats)}
    return log


def _flag_bits(raw: bytes, log: ULog) -> tuple[int, list[tuple[int, int]]]:
    """The flag bits message, first after the header: where the messages after it start and the blocks to read - the
    log up to the first offset of appended data, then each block of appended data up to the next offset (or the end
    of the file). An offset is taken only beyond the flag bits and the offset before it and within the file; one that
    is not ends the offsets (counted in ``offsets_ignored``)."""
    end = len(raw)
    size = struct.unpack_from("<H", raw, 16)[0]
    pos = 19 + size
    if pos > end or size < 40:
        return 16, [(16, end)]  # no room for the flags: read as any message (damaged or skipped)
    body = raw[19:pos]
    incompat = body[8:16]
    if incompat[0] & ~APPENDED & 0xFF or any(incompat[1:]):
        raise ValueError(f"{Path(log.path).name}: incompatible ULog flags this reader does not know")
    cuts: list[int] = []
    if incompat[0] & APPENDED:
        for offset in struct.unpack_from("<3Q", body, 16):
            if not offset:
                break
            if offset < (cuts[-1] if cuts else pos) or offset > end:
                log.offsets_ignored += 1
                break
            cuts.append(offset)
    log.appended = bool(cuts)
    edges = [pos, *cuts, end]
    return pos, list(zip(edges[:-1], edges[1:]))


@dataclass
class _State:
    raw: bytes
    log: ULog
    wanted: set | None
    formats: dict = field(default_factory=dict)
    reading: dict = field(default_factory=dict)  # msg_id -> [(topic, multi_id), dtype, payloads] of a subscription read
    done: list = field(default_factory=list)  # those whose msg_id was given to another subscription


def _message(state: _State, kind: int, body: bytes) -> None:
    """One message (not the flag bits) of type ``kind`` read into ``state``."""
    log, formats, reading = state.log, state.formats, state.reading
    size = len(body)
    if kind == ord("D"):
        if size < 2:
            return
        msg_id = struct.unpack_from("<H", body)[0]
        key = log.subscriptions.get(msg_id)
        if key is None:
            log.damaged += size + 3
            return
        log.counts[key] = log.counts.get(key, 0) + 1
        if msg_id in reading:
            reading[msg_id][2].append(body[2:])
    elif kind == ord("A"):
        if size < 3:
            return
        multi_id, msg_id = struct.unpack_from("<BH", body)
        name = body[3:].decode("ascii", "replace").strip("\0")
        log.subscriptions[msg_id] = (name, multi_id)
        log.counts.setdefault((name, multi_id), 0)
        if msg_id in reading:
            state.done.append(reading.pop(msg_id))
        if (state.wanted is None or name in state.wanted) and name in formats:
            reading[msg_id] = [(name, multi_id), np.dtype(_flatten(name, formats)), []]
    elif kind == ord("F"):
        name, fields = _parse_format(body.decode("ascii", "replace").strip("\0"))
        formats[name] = fields
    elif kind in (ord("I"), ord("P"), ord("Q"), ord("M")):
        skip = {ord("I"): 0, ord("P"): 0, ord("Q"): 1, ord("M"): 1}[kind]
        if size < skip + 1:
            return
        klen = body[skip]
        key = body[skip + 1:skip + 1 + klen].decode("ascii", "replace")
        type_, _, name = key.partition(" ")
        value = _value(type_, body[skip + 1 + klen:], formats)
        if kind == ord("I"):
            log.info[name] = value
        elif kind == ord("M"):
            log.multi_info.setdefault(name, []).append(value)
        elif kind == ord("P"):
            log.params[name] = value
    elif kind == ord("O"):
        if size >= 2:
            log.dropouts += 1
            log.dropout_ms += struct.unpack_from("<H", body)[0]
    elif kind == ord("L"):
        if size >= 9:
            level, t = struct.unpack_from("<BQ", body)
            log.logged.append((level, t, body[9:].decode("utf-8", "replace").strip("\0")))
    elif kind == ord("C"):
        if size >= 11:
            level, _tag, t = struct.unpack_from("<BHQ", body)
            log.logged.append((level, t, body[11:].decode("utf-8", "replace").strip("\0")))


def _flattens(name: str, formats: dict) -> bool:
    try:
        _flatten(name, formats)
        return True
    except ValueError:
        return False
