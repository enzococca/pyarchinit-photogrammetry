"""Raw receiver logs told by their content and converted to RINEX for the PPK: u-blox (UBX), RTCM 3 and the base logs
of DJI D-RTK 2/3 stations (``.DAT``, which DJI documents as "RTCM3.2 raw data"), converted by RTKLIB's convbin inside
the native core (``_native.raw_to_rinex``) or by a convbin program, into a cache of conversions made once.

A D-RTK ``.DAT`` is read as RTCM 3 and nothing else, and only when its bytes are RTCM 3 frames (preamble 0xD3, length,
CRC-24Q): any other content is refused with the way out (RINEX exported by DJI Terra 4.4 or later, or DJI Assistant
2). The layout of closed formats is never guessed.

RTCM 3 observations carry the time of the week only: an approximate time of the log (the day of the flight) places
them in their week, and the GPS ephemerides of the log (message 1019, with its week number) check that week.

The native core is built with NFREQ=3 and keeps at most 6 signals of a satellite in an epoch (the convbin program 7):
on logs of many signals a few observation types may be left out; :class:`Converted` warns when a system reaches 6."""
from __future__ import annotations

import hashlib
import json
import mmap
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import _native
from ..core.progress import Cancelled, as_progress
from . import download, rinex, rinex_files

KINDS = ("rinex", "ubx", "rtcm3")  # what raw_kind tells
HEAD_BYTES = 1 << 16  # the part of a file looked at to tell what it is
FRAMES_NEEDED = 3  # valid frames that make a file a UBX or RTCM 3 log
LONGEST_FRAME = 3 + 1023 + 3  # an RTCM 3 frame: header, the longest message, CRC
DAT_COVERAGE = 0.5  # the share of the bytes, from the first frame on, that RTCM 3 frames must hold in a D-RTK .DAT
WEEK = 604800.0
DAY = 86400.0
WRONG_WEEK_DAYS = 3.0  # a conversion this far from where it should be is in another week
DJI_DAT_REFUSED = "not an RTCM 3 log: export RINEX from DJI Terra (4.4 or later) or DJI Assistant 2"
CORE_SIGNALS = 6  # signals of a satellite the core keeps in an epoch (NFREQ=3 + NEXOBS=3)
_GPS_EPOCH = datetime(1980, 1, 6)


# --------------------------------------------------------------------------------------------- RTCM 3 frames, CRC-24Q

def _crc24q_table(polynomial: int = 0x1864CFB) -> tuple[int, ...]:
    """The table of the CRC-24Q of RTCM 3 (RTCM 10403, Qualcomm CRC-24Q: x^24 + x^23 + x^18 + x^17 + x^14 + x^11 +
    x^10 + x^7 + x^6 + x^5 + x^4 + x^3 + x + 1), computed from the polynomial, one entry per byte value."""
    table = []
    for byte in range(256):
        crc = byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= polynomial
        table.append(crc & 0xFFFFFF)
    return tuple(table)


CRC24Q = _crc24q_table()


def crc24q(data: bytes, crc: int = 0) -> int:
    """The CRC-24Q of ``data`` (initial value 0, most significant bit first, no final xor), continued from ``crc``."""
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ CRC24Q[((crc >> 16) ^ byte) & 0xFF]
    return crc


def rtcm3_frames(data: bytes, limit: int | None = None):
    """The whole RTCM 3 frames of ``data`` as ``(message type, offset, size)``, in order: a frame is the preamble
    0xD3, 6 bits and the 10-bit length of the message, the message (its type in the first 12 bits) and the CRC-24Q
    of all that came before. The bytes between frames are passed over; a 0xD3 whose frame fails its CRC is not one
    (the next 0xD3 after it is looked at). Stops after ``limit`` frames when one is given."""
    found, i, size = 0, data.find(b"\xd3"), len(data)
    while 0 <= i and i + 6 <= size:
        length = ((data[i + 1] & 0x03) << 8) | data[i + 2]
        end = i + 3 + length + 3
        if length >= 2 and end <= size and crc24q(data[i:end - 3]) == int.from_bytes(data[end - 3:end], "big"):
            yield (data[i + 3] << 4) | (data[i + 4] >> 4), i, end - i
            found += 1
            if limit is not None and found >= limit:
                return
            i = data.find(b"\xd3", end)
        else:
            i = data.find(b"\xd3", i + 1)


def _ubx_frames(data: bytes) -> list[tuple[int, int]]:
    """The whole UBX frames of ``data`` as ``(offset, size)``: sync 0xB5 0x62, class, id, the little-endian length,
    the payload and the 8-bit Fletcher checksum over class to payload (u-blox Interface Description)."""
    frames, i, size = [], data.find(b"\xb5\x62"), len(data)
    while 0 <= i and i + 8 <= size:
        end = i + 8 + (data[i + 4] | (data[i + 5] << 8))
        if end <= size:
            a = b = 0
            for byte in data[i + 2:end - 2]:
                a = (a + byte) & 0xFF
                b = (b + a) & 0xFF
            if data[end - 2] == a and data[end - 1] == b:
                frames.append((i, end - i))
                i = data.find(b"\xb5\x62", end)
                continue
        i = data.find(b"\xb5\x62", i + 1)
    return frames


def _inner_head(path: Path) -> tuple[tuple[str, ...], bytes]:
    """The layers ``path`` is packed in and the first bytes of what is inside them (of the file itself when none)."""
    layers = rinex_files.compression(path)
    with open(path, "rb") as f:
        head = f.read(HEAD_BYTES)
    for layer in layers:
        if layer == "zip":
            head = rinex_files._zip_head(path)
        elif layer in ("gzip", "compress"):
            head = rinex_files._inflate_head(layer, head)
    return layers, head


def _is_rinex(head: bytes) -> bool:
    return head.split(b"\n", 1)[0][60:80].strip() == b"RINEX VERSION / TYPE"


def raw_kind(path: str | Path) -> str | None:
    """What ``path`` is, told by its content, never by its name: ``"rinex"`` a RINEX file (also packed: gzip, the Unix
    compress, zip, Hatanaka), ``"ubx"`` a u-blox log (at least 3 whole UBX frames in its first 64 KiB), ``"rtcm3"`` an
    RTCM 3 log (at least 3 frames whose CRC-24Q is right in its first 64 KiB, also after bytes that are not a frame:
    a log may start in the middle of a message), None for anything else. A file with both kinds of frames (a receiver
    that writes RTCM 3 beside UBX) is the kind whose frames hold more of its first bytes. A raw log under gzip or zip
    is not looked into."""
    path = Path(path)
    layers, head = _inner_head(path)
    if "hatanaka" in layers or _is_rinex(head):
        return "rinex"
    if layers:
        return None
    ubx = _ubx_frames(head)
    rtcm = list(rtcm3_frames(head))
    ubx_bytes = sum(size for _, size in ubx) if len(ubx) >= FRAMES_NEEDED else 0
    rtcm_bytes = sum(size for _, _, size in rtcm) if len(rtcm) >= FRAMES_NEEDED else 0
    if not ubx_bytes and not rtcm_bytes:
        return None
    return "ubx" if ubx_bytes >= rtcm_bytes else "rtcm3"


def _is_dat(path: Path) -> bool:
    return path.suffix.lower() == ".dat"


def _check_dat(path: Path) -> None:
    """A D-RTK ``.DAT`` read as RTCM 3 only when it is: in its first 64 KiB at least 3 frames with a right CRC-24Q,
    the first within the length of the longest frame from the start (a log may start in the middle of a message), and
    frames holding at least half of the bytes from the first frame on (less the frame the 64 KiB may cut); otherwise
    a ValueError with :data:`DJI_DAT_REFUSED`."""
    with open(path, "rb") as f:
        head = f.read(HEAD_BYTES)
    frames = list(rtcm3_frames(head))
    if len(frames) >= FRAMES_NEEDED and frames[0][1] < LONGEST_FRAME:
        first = frames[0][1]
        if sum(size for _, _, size in frames) >= DAT_COVERAGE * (len(head) - first - LONGEST_FRAME):
            return
    raise ValueError(f"{path.name}: {DJI_DAT_REFUSED}")


# ----------------------------------------------------------------------------------------------------- the GPS week

_FRAME_1019 = re.compile(rb"\xd3\x00\x3d\x3f[\xb0-\xbf]", re.DOTALL)  # preamble, length 61, type 1019 (0x3FB)


def _gps_ephemerides(path: Path) -> list[tuple[int, float]]:
    """``(week mod 1024, toe in seconds of the week)`` of the GPS ephemerides of an RTCM 3 log: the frames of message
    1019 (61 bytes: DF076 the week, 10 bits after the 12-bit type and the 6-bit satellite id; DF093 toe, 16 bits of
    16 s, 288 bits into the message) whose CRC-24Q is right, looked for through the whole file without checking the
    frames of other messages."""
    found = []
    with open(path, "rb") as f:
        if path.stat().st_size == 0:
            return found
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
            for match in _FRAME_1019.finditer(data):
                i = match.start()
                frame = data[i:i + 67]
                if len(frame) < 67 or crc24q(frame[:64]) != int.from_bytes(frame[64:], "big"):
                    continue
                message = int.from_bytes(frame[3:64], "big")

                def field(start: int, width: int) -> int:
                    return (message >> (61 * 8 - start - width)) & ((1 << width) - 1)

                found.append((field(18, 10), field(288, 16) * 16.0))
    return found


def _week_warning(path: Path, span: tuple[float, float]) -> str | None:
    """None when the GPS ephemerides of the RTCM 3 log agree with the week its observations were put in, a warning
    when it has none (the week is not checked), a ValueError when they do not agree (the approximate time was in
    another week). An ephemeris is in its week ``WN`` or, near the end of a week, in the next one (a receiver gives
    the week it received it): its time is taken as the nearer of the two to the observations; the observations put a
    week before their time are told, a week after are not (an ephemeris received the week before)."""
    ephemerides = _gps_ephemerides(path)
    if not ephemerides:
        return (f"{path.name}: no GPS ephemerides (RTCM 1019) in the log: the week of its observations comes from the "
                "approximate time alone and is not checked")
    start, end = span
    week = int(start // WEEK)
    distances = []
    for week_mod, toe in ephemerides:
        full = week + ((week_mod - week) % 1024 + 512) % 1024 - 512  # the week nearest to the observations
        times = ((full + k) * WEEK + toe for k in (0, 1))
        distances.append(min(0.0 if start <= t <= end else min(abs(t - start), abs(t - end)) for t in times))
    distances.sort()
    if distances[len(distances) // 2] > WRONG_WEEK_DAYS * DAY:
        raise ValueError(f"{path.name}: the observations and the GPS ephemerides of the log are in different weeks: "
                         "the approximate time of the log is in the wrong week - give the day of the flight")
    return None


# ------------------------------------------------------------------------------------------------------- conversion

@dataclass
class Converted:
    """A raw log converted to RINEX (or a RINEX file taken as it is)."""

    source: Path  # the file given
    kind: str  # "ubx", "rtcm3", "novatel", ... or "rinex"
    obs: Path  # the RINEX observations
    nav: Path | None  # the RINEX navigation file the log gave, None when none (and for a RINEX file)
    epochs: int | None  # epochs and events written (None for a RINEX file: not counted)
    events: int | None
    span: tuple[float, float]  # the first and last epoch, GPS seconds since 1980-01-06
    station_position: tuple[float, float, float] | None  # APPROX POSITION XYZ (ECEF, m): RTCM 1005/1006, u-blox fix
    antenna: str = ""  # the antenna type the log names (RTCM 1007/1008/1033), "" when none
    warnings: list[str] = field(default_factory=list)


def _gps_seconds(when: float | datetime) -> float:
    """GPS seconds since 1980-01-06: a number as it is, a naive datetime as GPS time, an aware one converted from
    UTC with the leap seconds."""
    if isinstance(when, datetime):
        if when.tzinfo is not None:
            utc = when.astimezone(timezone.utc).replace(tzinfo=None)
            return (utc - _GPS_EPOCH).total_seconds() + rinex.gps_minus_utc(utc)
        return (when - _GPS_EPOCH).total_seconds()
    return float(when)


def _seconds(when: datetime) -> float:
    return (when - _GPS_EPOCH).total_seconds()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _from_rinex(path: Path) -> Converted:
    info = rinex.header(path)
    if info.kind != "obs" or info.first is None or info.last is None:
        raise ValueError(f"{path.name}: a RINEX file with no observation epochs")
    return Converted(path, "rinex", path, None, None, None, (_seconds(info.first), _seconds(info.last)),
                     info.approx_xyz, info.antenna)


def _signal_warnings(obs: Path) -> list[str]:
    """A warning for each system that reaches the signals the core keeps (some may have been left out)."""
    if not _native.convbin_identity().startswith("ppg_core"):
        return []
    info = rinex.header(obs)
    types = rinex._obs_types(rinex._header_lines(obs), info.version)
    full = [s for s, codes in types.items() if len({c[1:] for c in codes}) >= CORE_SIGNALS]
    return [f"{obs.name}: system {s} has {CORE_SIGNALS} signals, as many as the native core keeps of a satellite "
            "(NFREQ=3): others in the log may have been left out" for s in full]


def _cached(entry: Path) -> dict | None:
    """The counts of the conversion kept in ``entry``, None when it is not there whole (removed in part, damaged)."""
    try:
        meta = json.loads((entry / "convert.json").read_text())
        if (entry / meta["obs"]).is_file() and (not meta.get("nav") or (entry / meta["nav"]).is_file()):
            return meta
    except (OSError, ValueError, KeyError, TypeError):
        pass
    shutil.rmtree(entry, ignore_errors=True)
    return None


def _convert(path: Path, kind: str, entry: Path, approx: float | None, progress) -> dict:
    """The conversion written into the folder ``entry`` (made through ``entry`` + ".part"), its counts returned."""
    part = entry.with_name(entry.name + ".part")
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True)
    try:
        obs, nav = part / f"{path.stem}.obs", part / f"{path.stem}.nav"
        stop = threading.Event()

        def watch():  # the cancel button of a run stops the core's conversion
            while not stop.wait(0.1):
                if progress.cancel_requested:
                    _native.convert_cancel()
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            result = _native.raw_to_rinex(kind, path, obs, nav, approx)
        finally:
            stop.set()
            watcher.join()
        code = result["code"]
        if code == 1:
            raise Cancelled(f"conversion of {path.name} cancelled")
        if code == -2:
            raise ValueError(f"{path.name}: {result['message'] or f'not a {kind} log, or damaged'}")
        if code != 0:
            raise RuntimeError(f"{path.name}: conversion failed ({code}): {result['message']}")
        meta = {k: result.get(k, 0) for k in ("epochs", "events", "navigation", "errors")}
        meta.update(kind=kind, obs=obs.name, nav=nav.name if nav.exists() else None, message=result["message"])
        (part / "convert.json").write_text(json.dumps(meta))
        os.replace(part, entry)
        return meta
    finally:
        shutil.rmtree(part, ignore_errors=True)


def to_rinex(path: str | Path, folder: str | Path | None = None, *, kind: str | None = None,
             approx_time: float | datetime | None = None, progress=None) -> Converted:
    """``path`` as RINEX for the PPK. A RINEX file (also packed) is taken as it is; a raw log - told by
    :func:`raw_kind`, or of the ``kind`` given (one of ``_native.RAW_FORMATS``: NovAtel and the others are not told
    by their content) - is converted into RINEX 3.04 observations and navigation in ``folder`` (by default the
    cache of the program, ``download.cache_dir("converted")``), once: the conversion is kept and used again while the
    file (its path, size and time), the converter (``_native.convbin_identity``) and the options (kind, the GPS day of
    ``approx_time`` for the formats that need it) are the same; the conversions of earlier states of the file are removed.

    ``approx_time`` (GPS seconds since 1980-01-06, or a datetime: naive in GPS time, aware converted from UTC) is
    needed by RTCM 3 (and RT-17), which carry the time of the week only: the day of the flight is enough; given for
    another log it only checks it. The epochs of the conversion must lie within 3 days of it, and the GPS ephemerides of an RTCM 3 log must be in the week of
    its observations (:func:`_week_warning`): otherwise a ValueError that speaks of the wrong week.

    A DJI D-RTK base log (``.DAT``) is read as RTCM 3 only, when its bytes are RTCM 3 frames: otherwise - or with
    another ``kind`` - a ValueError with :data:`DJI_DAT_REFUSED`. A file that is no raw log is a ValueError, and so is
    a log that has no observations (the converter's message). A cancelled ``progress`` stops the conversion of the
    core (Cancelled); a convbin program runs to its end. Nothing is left of a conversion that fails."""
    path = Path(path)
    progress = as_progress(progress)
    if kind is not None and kind != "rinex" and kind not in _native.RAW_FORMATS:
        raise ValueError(f"the kind {kind!r} is not one of rinex, {', '.join(_native.RAW_FORMATS)}")
    if _is_dat(path):
        if kind not in (None, "rtcm3"):
            raise ValueError(f"{path.name}: {DJI_DAT_REFUSED}")
        _check_dat(path)
        kind = "rtcm3"
    found = kind or raw_kind(path)
    if found is None:
        raise ValueError(f"{path.name}: not a raw receiver log (u-blox, RTCM 3) nor a RINEX file")
    if found == "rinex":
        return _from_rinex(path)
    approx = _gps_seconds(approx_time) if approx_time is not None else None
    if found in _native.RAW_NEED_TIME and not approx:
        raise ValueError(f"{path.name}: an {found} log carries the time of the week only: an approximate time of the "
                         "log (the day of the flight) is needed")

    stat = path.stat()
    cache = Path(folder) if folder is not None else download.cache_dir("converted")
    cache.mkdir(parents=True, exist_ok=True)
    whose = _hash(str(path.resolve()))
    timed = found in _native.RAW_NEED_TIME  # the approximate time changes the conversion of these only
    day = int(approx // DAY) if timed else ""
    key = _hash(f"{stat.st_size}|{stat.st_mtime_ns}|{found}|{day}|{_native.convbin_identity()}")
    entry = cache / f"{whose}-{key}"
    progress(0.0, f"Converting {path.name} ({found}) to RINEX")
    meta = _cached(entry)
    if meta is None:
        meta = _convert(path, found, entry, approx if timed else None, progress)
        for old in cache.glob(f"{whose}-*"):
            if old != entry and not old.name.endswith(".part"):
                shutil.rmtree(old, ignore_errors=True)
    try:
        obs = entry / meta["obs"]
        nav = entry / meta["nav"] if meta.get("nav") else None
        info = rinex.header(obs)
        span = rinex.time_span(obs)
        if span is None:
            raise ValueError(f"{path.name}: the conversion has no epochs"
                             + (": is the approximate time of the log in the right week?" if approx else ""))
        span = (_seconds(span[0]), _seconds(span[1]))
        warnings = []
        if approx:
            if min(abs(span[0] - approx), abs(span[1] - approx)) > WRONG_WEEK_DAYS * DAY and not (
                    span[0] <= approx <= span[1]):
                raise ValueError(f"{path.name}: the epochs are more than {WRONG_WEEK_DAYS:g} days from the "
                                 "approximate time of the log: is it the log of that day, in the right week?")
            if found == "rtcm3":
                warning = _week_warning(path, span)
                if warning:
                    warnings.append(warning)
        warnings += _signal_warnings(obs)
    except ValueError:
        shutil.rmtree(entry, ignore_errors=True)  # a conversion refused is not kept
        raise
    progress(1.0, f"{path.name}: {meta['epochs']} epochs, {meta['events']} events")
    return Converted(path, found, obs, nav, int(meta["epochs"]), int(meta["events"]), span, info.approx_xyz,
                     info.antenna, warnings)


def dji_dat(path: str | Path, folder: str | Path | None = None, *, approx_time: float | datetime | None = None,
            progress=None) -> Converted:
    """The base log of a DJI D-RTK 2/3 station (``.DAT``) as RINEX: :func:`to_rinex` with ``kind="rtcm3"`` - read
    as RTCM 3 when its bytes are RTCM 3 frames, refused with :data:`DJI_DAT_REFUSED` otherwise, whatever its name."""
    path = Path(path)
    _check_dat(path)
    return to_rinex(path, folder, kind="rtcm3", approx_time=approx_time, progress=progress)
