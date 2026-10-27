"""What a RINEX file holds: the receiver and antenna in its header, the time it covers, the navigation files that
go with it and the position of its antenna; files as the archives of the stations give them - gzip, the Unix compress,
zip, Hatanaka - read as they are, and several files of one receiver written as one. Plain Python: nothing here needs
RTKLIB (a Hatanaka file is decoded by the native core or a CRX2RNX, see ``_native.crx2rnx``)."""
from __future__ import annotations

import gzip
import hashlib
import math
import os
import re
import shutil
import tempfile
import zipfile
import zlib
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from .. import _native
from . import download, lzw

SYSTEMS = "GRECJIS"  # GPS, GLONASS, Galileo, BeiDou, QZSS, NavIC, SBAS: the letters of the satellite ids

_A = 6378137.0  # WGS84 semi-major axis (m)
_E2 = 0.00669437999014  # WGS84 first eccentricity squared

_NOT_RINEX_MESSAGE = "{name}: not a RINEX file"

# the first line of an epoch: RINEX 2 has a 2-digit year and no marker, RINEX 3 a "> " and a 4-digit year; the last
# group is the epoch flag
_EPOCH_V2 = re.compile(r"^ ([ \d]\d) +(\d{1,2}) +(\d{1,2}) +(\d{1,2}) +(\d{1,2}) +(\d{1,2}\.\d*)\s+(\d)")
_EPOCH_V3 = re.compile(r"^> +(\d{4}) +(\d{1,2}) +(\d{1,2}) +(\d{1,2}) +(\d{1,2}) +(\d{1,2}\.\d*)\s+(\d)")
# 0 ok, 1 power failure before the epoch, 6 cycle slip records, which carry the time of an observation epoch: it is
# an epoch like the others. 2-5 are events (moved antenna, new site...): their time is not an observation time
_OBSERVATION_FLAGS = "016"
_SATELLITE_V3 = re.compile(rf"^([{SYSTEMS}])\d\d")
_NAV_RECORD_V3 = re.compile(rf"^([{SYSTEMS}])\d\d ")

_TAIL_BYTES = 1 << 20  # the last epoch is looked for in this many bytes from the end first, then in four times more
_TAIL_LIMIT = 64 << 20  # up to this many; a file that has no epoch in them is read through

_NAV_V2 = {"N": "G", "G": "R", "H": "S"}  # the file type of a RINEX 2 navigation file says its system
_NAV_YEAR_EXTENSION = re.compile(r"\.(\d\d)[ngplc]$", re.IGNORECASE)
_OBS_YEAR_EXTENSION = re.compile(r"\.(\d\d)[od]$", re.IGNORECASE)  # o: observations, d: Hatanaka
_LONG_NAV = re.compile(r"_[A-Z]N\.rnx$", re.IGNORECASE)
# the daily broadcast file of the IGS in RINEX 2 (brdcDDD0.YYn GPS, .YYg GLONASS), of any station's day
_BRDC = re.compile(r"^brdc(\d{3})\d\.(\d\d)[ng]$", re.IGNORECASE)
# the day of an observation file by its name: RINEX 2 short names ssssDDDf.YYo, RINEX 3 long names ..._YYYYDDDHHMM_...
_OBS_SHORT_DAY = re.compile(r"^[a-z0-9]{4}(\d{3})[a-z0-9]\.(\d\d)[od]$", re.IGNORECASE)
_OBS_LONG_DAY = re.compile(r"_(\d{4})(\d{3})\d{4}_", re.IGNORECASE)

# GPS time minus UTC (s) from each date on: GPS time counts every second, UTC leaves out the leap seconds
_GPS_MINUS_UTC = [(datetime(1981, 7, 1), 1), (datetime(1982, 7, 1), 2), (datetime(1983, 7, 1), 3),
                  (datetime(1985, 7, 1), 4), (datetime(1988, 1, 1), 5), (datetime(1990, 1, 1), 6),
                  (datetime(1991, 1, 1), 7), (datetime(1992, 7, 1), 8), (datetime(1993, 7, 1), 9),
                  (datetime(1994, 7, 1), 10), (datetime(1996, 1, 1), 11), (datetime(1997, 7, 1), 12),
                  (datetime(1999, 1, 1), 13), (datetime(2006, 1, 1), 14), (datetime(2009, 1, 1), 15),
                  (datetime(2012, 7, 1), 16), (datetime(2015, 7, 1), 17), (datetime(2017, 1, 1), 18)]
_BDT_BEHIND_GPS = 14  # BeiDou time is GPS time less 14 s


@dataclass
class RinexInfo:
    """What the header of a RINEX file says. A field the header does not have is None, or an empty string for a
    name."""

    version: float
    kind: str  # "obs" observations, "nav" navigation messages
    marker: str  # the name of the mark or station
    receiver: str
    antenna: str  # the type, with the radome
    approx_xyz: tuple[float, float, float] | None  # ECEF (m); None when absent or all zeros
    antenna_delta_hen: tuple[float, float, float] | None  # the antenna above the mark, then east, north (m)
    interval: float | None  # seconds between epochs
    first: datetime | None  # of the first and last epoch, in GPS time (converted from the time system of the file),
    last: datetime | None  # without a time zone
    time_system: str  # the one the times of the file are written in: GPS, GLO (UTC), GAL, QZS, BDT or IRN
    systems: tuple[str, ...]  # the letters of the constellations, in the order of ``SYSTEMS``; see ``header``
    compressed: str | None  # how it is packed, the layers from the outside in joined with "+": "gzip", "zip+hatanaka"


# --- reading ----------------------------------------------------------------------------------------------------


def _without_packing(name: str) -> str:
    """``name`` without the extension of a gzip file, a file of the Unix compress or a zip archive."""
    for extension in (".gz", ".z", ".zip"):
        if name.lower().endswith(extension):
            return name[:-len(extension)]
    return name


def _is_gzip(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == _GZIP


def _open(path: Path, encoding: str = "ascii", errors: str = "replace"):
    """The file as text, a gzip file decompressed on the fly (by its first bytes, not by its name). Bytes that are not
    ASCII are replaced, not refused."""
    if _is_gzip(path):
        return gzip.open(path, "rt", encoding=encoding, errors=errors)
    return open(path, "rt", encoding=encoding, errors=errors)


def _header_end(line: str) -> bool:
    return line.rstrip().endswith("END OF HEADER")


def _data_lines(path: Path):
    """The lines after the header. A gzip file that is cut short or damaged ends where it breaks: the epochs
    before the break are still good."""
    with _open(path) as f:
        try:
            for line in f:
                if _header_end(line):
                    break
            yield from f
        except (EOFError, gzip.BadGzipFile, zlib.error):
            return


# --- files packed in layers ---------------------------------------------------------------------------------------

_HEAD_BYTES = 1 << 16  # the part of a file looked at to tell what it is packed in
_MAX_LAYERS = 4
_GZIP = b"\x1f\x8b"
_ZIP = (b"PK\x03\x04", b"PK\x05\x06")  # an archive with files, an empty one


def _is_crinex(head: bytes) -> bool:
    """The first line of ``head`` is that of a Hatanaka file: ``CRINEX VERS   / TYPE`` where a header has its labels."""
    return head.split(b"\n", 1)[0][60:].strip().startswith(b"CRINEX")


def _kind(head: bytes, outermost: bool) -> str | None:
    """The layer a file starts with, told by its first bytes ``head``: "gzip", "compress" (the Unix compress),
    "zip" (only for the outermost layer: the directory of an archive is at its end) or "hatanaka"; None for text."""
    if head.startswith(_GZIP):
        return "gzip"
    if head.startswith(lzw.MAGIC):
        return "compress"
    if outermost and head.startswith(_ZIP):
        return "zip"
    return "hatanaka" if _is_crinex(head) else None


def _member(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    """The one file of the zip archive called ``name`` (the folders in it and the resource forks that macOS adds are
    not files); a ``ValueError`` naming the archive and what it holds when that is not one file."""
    members = [m for m in archive.infolist()
               if not m.is_dir() and not (m.filename.startswith("__MACOSX/")
                                          or PurePosixPath(m.filename).name.startswith("._"))]
    if not members:
        raise ValueError(f"{name} holds no file")
    if len(members) > 1:
        raise ValueError(f"{name} holds {len(members)} files ({', '.join(m.filename for m in members)}): unzip it and "
                         "give the one you want")
    return members[0]


def _zip_head(path: Path) -> bytes:
    """The first bytes of the one file in the zip archive ``path``; nothing when it holds none or several, or cannot
    be read."""
    try:
        with zipfile.ZipFile(path) as archive, archive.open(_member(archive, path.name)) as member:
            return member.read(_HEAD_BYTES)
    except (ValueError, zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError):
        return b""


def _inflate_head(kind: str, data: bytes) -> bytes:
    """The first bytes of what ``data``, the beginning of a gzip or compress stream, holds; what is read before a break,
    nothing if it breaks at once."""
    if kind == "compress":
        return lzw.head(data, _HEAD_BYTES)
    try:
        return zlib.decompressobj(31).decompress(data, _HEAD_BYTES)
    except zlib.error:
        return b""


def compression(path: str | Path) -> tuple[str, ...]:
    """What the file is packed in, from the outside in: "gzip", "compress" (the Unix compress, .Z), "zip" (an archive of
    one file) and "hatanaka" (Compact RINEX), told by the first bytes of the file - and by those of what is inside - and
    by the first line of a Hatanaka file. The name is only a hint, and not looked at: a RINEX file called .crx is
    RINEX. A text file has none (``()``). A zip archive is looked into only as the outermost layer. The file is not read
    further, so a damaged file shows its layers here and fails when :func:`plain` opens it. A ``FileNotFoundError`` for
    a file that is not there."""
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(_HEAD_BYTES)
    layers: list[str] = []
    kind = _kind(head, True)
    if kind == "zip":
        layers.append("zip")
        head = _zip_head(path)
        kind = _kind(head, False)
    while kind in ("gzip", "compress") and len(layers) < _MAX_LAYERS:
        layers.append(kind)
        head = _inflate_head(kind, head)
        kind = _kind(head, False)
    if kind == "hatanaka" and len(layers) < _MAX_LAYERS:
        layers.append("hatanaka")
    return tuple(layers)


@contextmanager
def _writing(target: Path):
    """``target`` written through ``target`` + ".part", which becomes ``target`` when the block ends well and is gone
    when it does not: ``target`` is never half a file."""
    part = target.with_name(target.name + ".part")
    try:
        with open(part, "wb") as f:
            yield f
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)


def gunzip(src: str | Path, dst: str | Path, name: str | None = None) -> None:
    """The gzip file ``src`` decompressed into ``dst`` a piece at a time (through ``dst`` + ".part"); a ``ValueError``
    naming the file - ``name``, or ``src``'s - when it is cut short or damaged."""
    src, dst = Path(src), Path(dst)
    try:
        with gzip.open(src, "rb") as packed, _writing(dst) as plain_file:
            shutil.copyfileobj(packed, plain_file, 1 << 20)
    except (EOFError, gzip.BadGzipFile, zlib.error) as error:
        raise ValueError(f"{name or src.name} is cut short or damaged ({error}): download it again") from None


def _renamed(error: Exception, src: Path, name: str) -> str:
    """The message of ``error``, which names ``src``, with ``name`` (the file the user gave) in its place."""
    text = str(error)
    return text.replace(src.name, name, 1) if src.name != name else text


def _undo_gzip(src: Path, dst: Path, name: str) -> None:
    gunzip(src, dst, name)


def _undo_compress(src: Path, dst: Path, name: str) -> None:
    try:
        lzw.decompress(src, dst)
    except ValueError as error:
        raise ValueError(_renamed(error, src, name)) from None


def _undo_zip(src: Path, dst: Path, name: str) -> None:
    try:
        with zipfile.ZipFile(src) as archive:
            with archive.open(_member(archive, name)) as packed, _writing(dst) as plain_file:
                shutil.copyfileobj(packed, plain_file, 1 << 20)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as error:
        raise ValueError(f"{name} is not a zip archive that can be read ({error})") from None


def _undo_hatanaka(src: Path, dst: Path, name: str) -> None:
    try:
        _native.crx2rnx(src, dst)
    except ValueError as error:
        raise ValueError(_renamed(error, src, name)) from None
    except RuntimeError as error:  # no decoder: the core older than 0.6 and no CRX2RNX
        raise ValueError(f"{name}: {error}") from None


_UNDO = {"gzip": _undo_gzip, "compress": _undo_compress, "zip": _undo_zip, "hatanaka": _undo_hatanaka}


def _unpack(path: Path, layers: tuple[str, ...], target: Path) -> None:
    """The text of ``path`` under all its ``layers``, written to ``target``: each layer undone into a file of a private
    folder beside ``target``, the last one renamed to ``target``, which is there only when it is whole (and is never
    written by two runs at once). Errors name ``path``, the file the user gave."""
    scratch = Path(tempfile.mkdtemp(prefix=".unpack-", dir=target.parent))
    try:
        source = path
        for i, layer in enumerate(layers):
            out = scratch / f"layer{i + 1}"
            _UNDO[layer](source, out, path.name)
            source = out
        os.replace(source, target)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _plain(path: Path, layers: tuple[str, ...], folder: str | Path | None) -> Path:
    """:func:`plain` for a file whose ``layers`` are known. The copy is called after a hash of the whole path of the
    file and a hash of its size and modification time: the same file changed has another copy, and the copies of its
    earlier states are removed."""
    if not layers or layers == ("gzip",):
        return path
    stat = path.stat()
    cache = Path(folder) if folder is not None else download.cache_dir("rinex")
    whose = _hash(str(path.resolve()))
    copy = cache / f"{whose}-{_hash(f'{stat.st_size}|{stat.st_mtime_ns}')}.rnx"
    if not copy.exists():
        cache.mkdir(parents=True, exist_ok=True)
        _unpack(path, layers, copy)
        for old in cache.glob(f"{whose}-*.rnx"):
            if old != copy:
                old.unlink(missing_ok=True)
    return copy


def plain(path: str | Path, folder: str | Path | None = None) -> Path:
    """The file to read the text of ``path`` from: ``path`` itself when it is text or only gzip (RINEX is read through
    gzip as it is), otherwise a decompressed copy of it - under all its layers: the Unix compress, zip (an archive of
    one file), Hatanaka - in ``folder`` (by default the cache of the program, :func:`download.cache_dir`), made once and
    used again as long as the size and the time of the file are the same. The copy is written beside itself and renamed
    when it is whole, so a file that is damaged or cut short leaves nothing: it is a ``ValueError`` naming the file (and
    the line, for a Hatanaka file), or the files of an archive that holds several. A Hatanaka file needs a decoder
    (``_native.crx2rnx``): without one it is a ``ValueError`` that says what to install."""
    path = Path(path)
    return _plain(path, compression(path), folder)


def _year(year: int) -> int:
    """A year written with two digits (RINEX 2 epochs): 80-99 are 19xx, 00-79 are 20xx."""
    return year if year >= 100 else year + (1900 if year >= 80 else 2000)


def gps_minus_utc(when: datetime) -> int:
    """The seconds GPS time is ahead of UTC at ``when`` (a UTC time without a time zone): 0 until 1 July 1981,
    18 since 1 January 2017, which holds until a new leap second is announced."""
    seconds = 0
    for start, value in _GPS_MINUS_UTC:
        if when >= start:
            seconds = value
    return seconds


def _to_gps(when: datetime | None, time_system: str, leap: int | None) -> datetime | None:
    """A time of the file in GPS time. GPS, Galileo, QZSS and NavIC time are the same; BeiDou time is 14 s behind;
    GLONASS time is UTC, so GPS time is ahead of it by the leap seconds (those of the header when it has them)."""
    if when is None:
        return None
    if time_system == "GLO":
        return when + timedelta(seconds=leap or gps_minus_utc(when))
    if time_system == "BDT":
        return when + timedelta(seconds=_BDT_BEHIND_GPS)
    return when


def _when(year: int, month: int, day: int, hour: int, minute: int, seconds: float) -> datetime:
    return datetime(_year(year), month, day, hour, minute) + timedelta(seconds=seconds)


def _header_time(lines: list[str]) -> datetime | None:
    """The time in a ``TIME OF FIRST OBS`` / ``TIME OF LAST OBS`` line: year, month, day, hour, minute, seconds,
    as the file writes it."""
    try:
        parts = lines[0].split()
        return _when(*(int(p) for p in parts[:5]), float(parts[5]))
    except (IndexError, ValueError):
        return None


def _time_system(lines: list[str], systems: tuple[str, ...]) -> str:
    """The time system written after the time in ``TIME OF FIRST OBS``; when it is blank, the default of the
    file: GLONASS time for a file of GLONASS only, BeiDou time for BeiDou only, GPS time for any other."""
    parts = lines[0].split() if lines else []
    if len(parts) > 6:
        return parts[6].upper()
    return {("R",): "GLO", ("C",): "BDT"}.get(systems, "GPS")


def _numbers(text: str, count: int = 3, width: int = 14) -> tuple[float, ...] | None:
    """``count`` finite numbers written with spaces between them or, when fewer words than numbers are there
    (they run together), in columns of ``width``; None when the line is not that."""
    parts = text.split()
    try:
        if len(parts) == count:
            values = tuple(float(p) for p in parts)
        elif len(parts) < count:  # run together, like "-10000000.1234-20000000.1234-30000000.1234"
            values = tuple(float(text[i * width:(i + 1) * width]) for i in range(count))
        else:
            return None
    except ValueError:
        return None
    return values if all(math.isfinite(v) for v in values) else None


def _read_header(path: Path) -> tuple[RinexInfo, bool, int | None, Path]:
    """The header of the file, whether the systems are still to be found in the data, the leap seconds the header
    gives (None when it gives none) and the file to read the epochs from (:func:`plain`)."""
    name = path.name
    layers = compression(path)
    source = _plain(path, layers, None)
    labels: dict[str, list[str]] = {}
    try:
        with _open(source) as f:
            first = f.readline(1000)
            if first[60:].strip() != "RINEX VERSION / TYPE":
                raise ValueError(_NOT_RINEX_MESSAGE.format(name=name))
            try:
                version = float(first[:9])
            except ValueError:
                raise ValueError(_NOT_RINEX_MESSAGE.format(name=name)) from None
            for line in f:
                if _header_end(line):
                    break
                labels.setdefault(line[60:].strip(), []).append(line.rstrip("\r\n")[:60])
            else:
                raise ValueError(f"{name}: not a RINEX file, the header has no END OF HEADER line")
    except (EOFError, gzip.BadGzipFile, zlib.error) as error:
        raise ValueError(f"{name}: not a RINEX file ({error})") from None

    file_type = first[20:40].strip()[:1].upper()
    if file_type == "O":
        kind = "obs"
    elif file_type == "N" or (version < 3 and file_type in ("G", "H")):
        kind = "nav"
    else:
        raise ValueError(f"{name}: a RINEX file, but not an observation or navigation one")

    systems = {line[0] for line in labels.get("SYS / # / OBS TYPES", []) if line[:1] and line[:1] in SYSTEMS}
    letter = first[40:41].upper()
    scan = False
    if not systems:
        if kind == "nav" and version < 3:
            systems = {_NAV_V2[file_type]}
        elif letter and letter in SYSTEMS:
            systems = {letter}
        elif version < 3 and kind == "obs" and not letter.strip():
            systems = {"G"}  # RINEX 2: a blank system is GPS
        else:
            scan = True  # mixed: only the satellite ids say which

    def raw(label: str) -> str:
        return labels[label][0] if label in labels else ""

    def field(label: str, start: int = 0, end: int = 60) -> str:
        return raw(label)[start:end].strip()

    xyz = _numbers(raw("APPROX POSITION XYZ"))
    delta = _numbers(raw("ANTENNA: DELTA H/E/N"))
    try:
        interval = float(field("INTERVAL").split()[0])
    except (IndexError, ValueError):
        interval = None
    try:
        leap = int(raw("LEAP SECONDS")[:6])
    except ValueError:
        leap = None
    leap = leap if leap and leap > 0 else None
    ordered = tuple(s for s in SYSTEMS if s in systems)
    time_system = _time_system(labels.get("TIME OF FIRST OBS", []), ordered)
    info = RinexInfo(
        version=version, kind=kind, marker=field("MARKER NAME"), receiver=field("REC # / TYPE / VERS", 20, 40),
        antenna=field("ANT # / TYPE", 20, 40), approx_xyz=xyz if xyz and any(xyz) else None,
        antenna_delta_hen=delta, interval=interval if interval is not None and interval > 0 else None,
        first=_to_gps(_header_time(labels.get("TIME OF FIRST OBS", [])), time_system, leap),
        last=_to_gps(_header_time(labels.get("TIME OF LAST OBS", [])), time_system, leap),
        time_system=time_system, systems=ordered, compressed="+".join(layers) or None)
    return info, scan, leap, source


def _epoch(line: str, version: float) -> datetime | None:
    """The time of an epoch line of observations, None for any other line and for event epochs."""
    m = (_EPOCH_V3 if version >= 3 else _EPOCH_V2).match(line)
    if not m or m.group(7) not in _OBSERVATION_FLAGS:
        return None
    try:
        return _when(*(int(g) for g in m.groups()[:5]), float(m.group(6)))
    except ValueError:
        return None


def _first_epoch(path: Path, version: float) -> datetime | None:
    for line in _data_lines(path):
        when = _epoch(line, version)
        if when:
            return when
    return None


def _last_epoch_from_end(path: Path, version: float) -> datetime | None:
    """The last epoch of a plain file, looked for in a window from its end that grows until it has one."""
    size = path.stat().st_size
    window = min(_TAIL_BYTES, size)
    with open(path, "rb") as f:
        while True:
            f.seek(size - window)
            lines = f.read(window).decode("ascii", errors="replace").splitlines()
            if window < size:
                lines = lines[1:]  # it starts in the middle of a line
            else:
                for i, line in enumerate(lines):  # the whole file: leave out the header
                    if _header_end(line):
                        lines = lines[i + 1:]
                        break
            for line in reversed(lines):
                when = _epoch(line, version)
                if when:
                    return when
            if window >= min(size, _TAIL_LIMIT):
                return None
            window = min(size, _TAIL_LIMIT, window * 4)


def _last_epoch(path: Path, version: float) -> datetime | None:
    """The last epoch. A plain file is read from its end, so that a day of observations at 1 Hz costs the same as
    a minute; a gzip file has to be read through."""
    if not _is_gzip(path):
        last = _last_epoch_from_end(path, version)
        if last:
            return last
    last = None
    for line in _data_lines(path):
        last = _epoch(line, version) or last
    return last


def _span(path: Path, info: RinexInfo, leap: int | None) -> tuple[datetime, datetime] | None:
    """The first and last epoch of the observations, in GPS time."""
    first = _first_epoch(path, info.version)
    if first is None:
        return None
    last = _last_epoch(path, info.version) or first
    return _to_gps(first, info.time_system, leap), _to_gps(last, info.time_system, leap)


def _sat_systems(text: str) -> set[str]:
    """The systems of ``text``, slots of three characters of a RINEX 2 satellite list (``G 3G 7R 5``): the letter
    of each, blank is GPS."""
    return {s for s in ("G" if text[i] == " " else text[i] for i in range(0, len(text), 3)) if s in SYSTEMS}


def _declared(line: str) -> int:
    """The number of satellites a RINEX 2 epoch line declares (columns 30-32), 0 when it does not say."""
    try:
        return int(line[29:32])
    except ValueError:
        return 0


def _data_systems(path: Path, info: RinexInfo) -> tuple[str, ...]:
    """The systems of a mixed file from its satellite ids: those of the first ten epochs of observations (a system
    that only appears later is not found), every record of navigation messages."""
    found: set[str] = set()
    epochs = pending = 0
    for line in _data_lines(path):
        if info.kind == "nav":
            m = _NAV_RECORD_V3.match(line)
            if m:
                found.add(m.group(1))
        elif info.version >= 3:
            if line.startswith(">"):
                epochs += 1
            m = _SATELLITE_V3.match(line)
            if m:
                found.add(m.group(1))
        else:
            m = _EPOCH_V2.match(line)
            if m:
                epochs += 1
                pending = _declared(line) if m.group(7) in _OBSERVATION_FLAGS else 0
            if pending:  # the slots of the satellites declared, 12 to a line; not the clock offset after them
                take = min(pending, 12)
                found |= _sat_systems(line[32:32 + 3 * take])
                pending -= take
        if epochs > 10:
            break
    return tuple(s for s in SYSTEMS if s in found)


# --- the public interface ---------------------------------------------------------------------------------------


def header(path: str | Path) -> RinexInfo:
    """What the header of a RINEX observation or navigation file says. The first and last epoch of observations
    that the header does not give are read from the epochs; both are in GPS time whatever the time system of the
    file (``time_system``). The systems of a mixed file are read from the satellite ids of its first epochs only.
    A file packed in gzip, the Unix compress, zip or Hatanaka is read through :func:`plain` (``compressed`` says how
    it is packed). A file that is not RINEX is a ``ValueError`` naming it, and so is one that cannot be unpacked."""
    path = Path(path)
    info, scan, leap, source = _read_header(path)
    if info.kind == "obs" and (info.first is None or info.last is None):
        span = _span(source, info, leap)
        if span:
            info.first, info.last = info.first or span[0], info.last or span[1]
    if scan:
        info.systems = _data_systems(source, info)
    return info


def time_span(path: str | Path) -> tuple[datetime, datetime] | None:
    """The first and last epoch of observations as the epochs of the file give them (not as the header declares),
    in GPS time; None when the file has no epochs."""
    path = Path(path)
    info, _, leap, source = _read_header(path)
    if info.kind != "obs":
        raise ValueError(f"{path.name}: a navigation file has no observation epochs")
    return _span(source, info, leap)


# --- one file from several --------------------------------------------------------------------------------------

_EVENT_V2 = re.compile(r"^ {28}[2-5]")  # an event record of RINEX 2 without a time: blank up to its flag
# header lines that count the observations of the file they are in: of no use, and wrong, for a file made of several
_STATISTICS = ("# OF SATELLITES", "PRN / # OF OBS")
_GAP_INTERVALS = 2.0  # a step between two epochs of more than this many intervals is a gap
_LAST_OBS = "TIME OF LAST OBS"


def _label(line: str) -> str:
    return line[60:].strip()


def _obs_types(lines: Iterable[str], version: float) -> dict[str, tuple[str, ...]]:
    """The observation types that header lines give, by satellite system (RINEX 3 and later: ``SYS / # / OBS TYPES``,
    with continuation lines; RINEX 2 has one list for all, under "": ``# / TYPES OF OBSERV``), each in the order
    given."""
    types: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        label = _label(line)
        if version < 3 and label == "# / TYPES OF OBSERV":
            types.setdefault("", []).extend(line[6:60].split())
        elif version >= 3 and label == "SYS / # / OBS TYPES":
            if line[:1].strip():  # the letter of a system starts its list, a blank goes on with the one before
                current = line[0]
                types[current] = []
            if current is not None:
                types[current].extend(line[7:60].split())
    return {system: tuple(codes) for system, codes in types.items()}


def _header_lines(path: Path) -> list[str]:
    """The lines of the header of a file, ``END OF HEADER`` included, without their line ends."""
    lines = []
    with _open(path, "latin-1", "strict") as f:
        for line in f:
            lines.append(line.rstrip("\r\n"))
            if _header_end(line):
                break
    return lines


def _observed_interval(path: Path, version: float) -> float | None:
    """The seconds between the first epochs of a file (the middle step of the first 30), None when they show none."""
    times = []
    for line in _data_lines(path):
        when = _epoch(line, version)
        if when:
            times.append(when)
            if len(times) == 30:
                break
    steps = sorted((b - a).total_seconds() for a, b in zip(times, times[1:]) if b > a)
    return steps[len(steps) // 2] if steps else None


@dataclass
class _Piece:
    """A file to join: where its text is read from, what its header says, when it begins."""

    path: Path
    source: Path  # the file the text is read from (:func:`plain`)
    info: RinexInfo
    lines: list[str]  # its header
    first: datetime  # its first epoch in GPS time
    leap: int | None
    interval: float | None  # seconds between its epochs: the header's, else what its first epochs show


def _piece(path: Path) -> _Piece:
    """The file ``path`` as a piece to join; a ``ValueError`` when it is not observations with epochs."""
    info, _scan, leap, source = _read_header(path)
    if info.kind != "obs":
        raise ValueError(f"{path.name} holds navigation messages, not observations: only observation files are joined")
    first = _first_epoch(source, info.version)
    if first is None:
        raise ValueError(f"{path.name} has no observation epochs")
    return _Piece(path, source, info, _header_lines(source), _to_gps(first, info.time_system, leap), leap,
                  info.interval or _observed_interval(source, info.version))


def _alike(first: _Piece, other: _Piece) -> None:
    """A ``ValueError`` unless ``other`` is observations of the receiver and antenna of ``first``: the same major
    version of RINEX, marker, time system, antenna (its type, and its height and offsets over the mark) and
    observation types of each system."""
    a, b, name, ref = first.info, other.info, other.path.name, first.path.name
    if int(a.version) != int(b.version):
        raise ValueError(f"{name} is RINEX {b.version:g} and {ref} is RINEX {a.version:g}: files of another version of "
                         "RINEX are not joined")
    if a.marker != b.marker:
        raise ValueError(f"{name} is of the marker {b.marker!r} and {ref} of the marker {a.marker!r}: files of another "
                         "marker are not joined")
    if a.time_system != b.time_system:
        raise ValueError(f"{name} writes its times in {b.time_system} and {ref} in {a.time_system}: the time system of "
                         "the files has to be the same")
    if a.antenna != b.antenna or a.antenna_delta_hen != b.antenna_delta_hen:
        raise ValueError(f"{name} has the antenna {b.antenna!r} over the mark by {b.antenna_delta_hen} and {ref} the "
                         f"antenna {a.antenna!r} by {a.antenna_delta_hen}: files of another antenna, or of another "
                         "height of it, are not joined")
    types_a, types_b = _obs_types(first.lines, a.version), _obs_types(other.lines, b.version)
    if types_a != types_b:
        system = next(s for s in sorted({*types_a, *types_b}) if types_a.get(s) != types_b.get(s))
        where = f" of system {system}" if system else ""
        raise ValueError(f"{name} has other observation types than {ref}{where} ({' '.join(types_b.get(system, ()))} "
                         f"and {' '.join(types_a.get(system, ()))}): files with other observation types are not joined")


def _record_start(line: str, version: float):
    """What the line that starts a record of observations says, ``(time, flag, count, fields)``: the time of the epoch
    (None when the line has none), its flag, the number of satellites - or, for an event, of the lines of its - that
    follow, and the (year, month, day, hour, minute, seconds) as written; None when the line starts no record."""
    if version >= 3:
        if not line.startswith(">"):
            return None
        match = _EPOCH_V3.match(line)
        flag, count = line[31:32], line[32:35]
    else:
        match = _EPOCH_V2.match(line)
        if match is None and not _EVENT_V2.match(line):
            return None
        flag, count = line[28:29], line[29:32]
    try:
        number = int(count)
    except ValueError:
        number = 0
    if match is None:  # an event without a time (or a line that starts like a record of RINEX 3 and is not)
        return None, int(flag) if flag.isdigit() else -1, number, None
    year = _year(int(match.group(1))) if version < 3 else int(match.group(1))
    fields = (year, *(int(match.group(i)) for i in range(2, 6)), float(match.group(6)))
    try:
        return _when(*fields[:5], fields[5]), int(match.group(7)), number, fields
    except ValueError:  # a date that is not one: not an epoch
        return None


def _records(lines: Iterable[str], version: float) -> Iterator[tuple[datetime | None, int, list[str], tuple | None]]:
    """The records of the lines after a header: ``(time, flag, lines, fields)`` of each epoch of observations and of
    each event, its lines up to the next record; the lines an event announces (flags 2 to 5 count them) are its
    whatever they hold. Lines before the first record are a record of flag -1."""
    record: list | None = None
    special = 0
    for line in lines:
        if special:
            record[2].append(line)
            special -= 1
            continue
        start = _record_start(line, version)
        if start is None:
            if record is None:
                record = [None, -1, [line], None]
            else:
                record[2].append(line)
            continue
        if record is not None:
            yield tuple(record)
        time, flag, count, fields = start
        record = [time, flag, [line], fields]
        special = count if 2 <= flag <= 5 else 0
    if record is not None:
        yield tuple(record)


def _piece_lines(piece: _Piece) -> Iterator[str]:
    """The lines after the header of a piece. A gzip file cut short or damaged is an error here: a base missing its end
    is not used in silence."""
    try:
        with _open(piece.source, "latin-1", "strict") as f:
            for line in f:
                if _header_end(line):
                    break
            for line in f:
                yield line.rstrip("\r\n")
    except (EOFError, gzip.BadGzipFile, zlib.error) as error:
        raise ValueError(f"{piece.path.name} is cut short or damaged ({error}): download it again") from None


def _time_card(label: str, fields: Sequence[float], system: str) -> str:
    """A ``TIME OF FIRST OBS`` or ``TIME OF LAST OBS`` line for the (year, month, day, hour, minute, seconds)
    ``fields``."""
    year, month, day, hour, minute, seconds = fields
    when = f"{int(year):6d}{int(month):6d}{int(day):6d}{int(hour):6d}{int(minute):6d}{seconds:13.7f}"
    return f"{when}     {system:<3}".ljust(60) + label


def _joined_header(first: _Piece, interval: float | None, several: bool) -> tuple[list[str], int]:
    """The header of the joined file and the index of its ``TIME OF LAST OBS`` line, which is to be filled in once the
    last epoch is known: the header of the first piece, without the lines that count the observations of one file when
    there are several, with the smallest ``INTERVAL`` of the pieces. The lines keep their places; one that is not there
    is added, ``INTERVAL`` before ``TIME OF FIRST OBS`` and ``TIME OF LAST OBS`` after it."""
    lines = [line for line in first.lines if not (several and _label(line) in _STATISTICS)]

    def find(label: str) -> int | None:
        return next((i for i, line in enumerate(lines) if _label(line) == label), None)

    if interval is not None and not (first.info.interval is not None and abs(first.info.interval - interval) < 1e-9):
        card = f"{interval:10.3f}".ljust(60) + "INTERVAL"
        i = find("INTERVAL")
        if i is not None:
            lines[i] = card
        else:
            first_obs = find("TIME OF FIRST OBS")
            lines.insert(first_obs if first_obs is not None else len(lines) - 1, card)
    i, j = find(_LAST_OBS), find("TIME OF FIRST OBS")
    system = lines[i if i is not None else j][48:51].strip() if (i is not None or j is not None) else ""
    card = _time_card(_LAST_OBS, (2000, 1, 1, 0, 0, 0.0), system)
    if i is not None:
        lines[i] = card
    else:
        i = j + 1 if j is not None else len(lines) - 1
        lines.insert(i, card)
    return lines, i


def _same_file(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve() or (a.exists() and b.exists() and os.path.samefile(a, b))


def join(paths: str | Path | Iterable[str | Path],
         out: str | Path) -> tuple[RinexInfo, list[tuple[datetime, datetime]]]:
    """The observations of several files of one receiver - pieces of a day, an hour or a quarter of an hour, as the
    archives give them, packed or not - written to ``out`` as one RINEX file; the header of the new file and the
    holes in it, each as the epoch before it and the epoch after it, in GPS time.

    The files have to be observations of one receiver: the same major version of RINEX, marker, time system, antenna
    (its type, and its height and offsets over the mark) and observation types of each system, or it is a
    ``ValueError`` naming the file and what differs - and so is a navigation file, a file without epochs, a gzip
    file cut short or damaged (the base is never missing its end in silence) and an ``out`` that is one of the
    files. They are written in the order of their first epoch (the order given for those that begin together): the
    records of a file whose epoch is not later than the last one written are repeats and are left out, with the event
    records that follow them; the other records, events included, are copied as they are. A header event inside a
    piece that gives observation types other than its header's is refused when there are several pieces, the pieces
    after it would be read with them.

    The header is that of the first file, with ``TIME OF LAST OBS`` of the last epoch written (the line is added when
    it is not there), the smallest ``INTERVAL`` of the pieces, and without the header lines that count the
    observations of one file (``# OF SATELLITES``, ``PRN / # OF OBS``) when there are several. A hole is a step
    between two epochs written of more than twice the interval - the larger of those of the two pieces they are in;
    the interval of a piece is the header's or, without one, what its first epochs show.

    ``out`` is written beside itself and renamed when it is whole: after an error there is none."""
    out = Path(out)
    given = [Path(paths)] if isinstance(paths, (str, os.PathLike)) else [Path(p) for p in paths]
    if not given:
        raise ValueError("no file to join")
    for path in given:
        if _same_file(path, out):
            raise ValueError(f"{path.name} is the file the joined one is written to: it would overwrite it")
    pieces = sorted((_piece(path) for path in given), key=lambda piece: piece.first)
    first = pieces[0]
    for other in pieces[1:]:
        _alike(first, other)
    several = len(pieces) > 1
    declared = [piece.info.interval for piece in pieces if piece.info.interval]
    top, mark = _joined_header(first, min(declared) if declared else None, several)
    version, types = first.info.version, _obs_types(first.lines, first.info.version)
    system = top[mark][48:51].strip()
    out.parent.mkdir(parents=True, exist_ok=True)
    gaps: list[tuple[datetime, datetime]] = []
    last: datetime | None = None
    fields: tuple | None = None
    interval: float | None = None  # of the piece the last epoch written is in
    with _writing(out) as f:
        def emit(lines: list[str]) -> None:
            f.write(("\n".join(lines) + "\n").encode("latin-1"))

        emit(top)
        for piece in pieces:
            skipping = False  # after an epoch left out, until the next one written
            held: list[str] = []  # cycle slip records (flag 6), written before the epoch they come with
            for time, flag, lines, record_fields in _records(_piece_lines(piece), version):
                if flag == 6:
                    held += lines
                elif flag in (0, 1) and time is not None:
                    if last is not None and time <= last:
                        skipping, held = True, []
                        continue
                    steps = [v for v in (interval, piece.interval) if v]
                    if last is not None and steps and (time - last).total_seconds() > _GAP_INTERVALS * max(steps):
                        gaps.append((last, time))
                    skipping = False
                    last, fields, interval = time, record_fields, piece.interval
                    emit(held + lines)
                    held = []
                else:  # an event, or lines before the first epoch
                    if several and 2 <= flag <= 5 and any(
                            types.get(s) != t for s, t in _obs_types(lines[1:], version).items()):
                        raise ValueError(f"{piece.path.name} gives other observation types than its header inside its "
                                         "observations: it cannot be joined with other files")
                    if not skipping:
                        emit(held + lines)
                    held = []
            if held and not skipping:
                emit(held)
        f.seek(sum(len(line) + 1 for line in top[:mark]))
        f.write(_time_card(_LAST_OBS, fields, system).encode("latin-1"))
    time_system, leap = first.info.time_system, first.leap
    return header(out), [(_to_gps(a, time_system, leap), _to_gps(b, time_system, leap)) for a, b in gaps]


def _obs_day(name: str) -> datetime | None:
    """The day of an observation file by its name - the day of the year of a RINEX 2 short name (``siteDDDf.YYo``) or
    of a RINEX 3 long name (``..._YYYYDDDHHMM_...``) - or None when its name does not say."""
    short, long = _OBS_SHORT_DAY.match(name), _OBS_LONG_DAY.search(name)
    try:
        if short:
            return datetime(_year(int(short.group(2))), 1, 1) + timedelta(days=int(short.group(1)) - 1)
        if long:
            return datetime(int(long.group(1)), 1, 1) + timedelta(days=int(long.group(2)) - 1)
    except ValueError:
        return None
    return None


def _broadcast_day(name: str, obs: str) -> bool:
    """``name`` is a daily broadcast file of the IGS (``brdcDDD0.YYn``, ``.YYg``) for the observation file ``obs``:
    of its day or of the day before or after (a flight across midnight) when the name of ``obs`` gives its day, of the
    year of its ``.YYo`` extension when only that is given, of any day otherwise."""
    brdc = _BRDC.match(name)
    if not brdc:
        return False
    day = _obs_day(obs)
    if day is not None:
        try:
            broadcast = datetime(_year(int(brdc.group(2))), 1, 1) + timedelta(days=int(brdc.group(1)) - 1)
        except ValueError:
            return False
        return abs((broadcast - day).days) <= 1
    year = _OBS_YEAR_EXTENSION.search(obs)
    return not year or year.group(1) == brdc.group(2)


def find_companions(obs: str | Path) -> list[Path]:
    """The navigation files that go with an observation file, from the folder it is in: the files with its name
    and a navigation extension (``.nav``, ``.24n`` ``.24g`` ``.24p`` ``.24l`` ``.24c`` for the year of the
    observations), the daily broadcast files of the IGS in RINEX 2 (``brdcDDD0.24n``, ``.24g``, of the day of the
    observations and the days around it when their name says it), every RINEX 3 ``*_MN.rnx`` ``*_GN.rnx`` ... of any
    station (a station is served by the combined broadcast file ``BRDC00IGS_R_..._MN.rnx``) and every ``.nav`` (a DJI
    drone writes ``*_Rinex.nav`` beside ``*_Rinex.obs``), each also as a .gz, .Z or .zip (the observations of a
    Hatanaka file ``.24d`` are those of ``.24o``). Sorted; never the file itself, never a
    hidden file (on an external drive macOS leaves a ``._x.nav`` beside every ``x.nav``)."""
    obs = Path(obs)
    own = _without_packing(obs.name)
    stem = own.rsplit(".", 1)[0]
    obs_year = _OBS_YEAR_EXTENSION.search(own)
    found = []
    for entry in obs.parent.iterdir():
        if entry.name == obs.name or entry.name.startswith(".") or not entry.is_file():
            continue
        name = _without_packing(entry.name)
        year_ext = _NAV_YEAR_EXTENSION.search(name)
        if name.lower().endswith(".nav"):
            found.append(entry)
        elif (year_ext and name[:year_ext.start()].lower() == stem.lower()
              and (not obs_year or year_ext.group(1) == obs_year.group(1))):
            found.append(entry)
        elif _broadcast_day(name, own):
            found.append(entry)
        elif _LONG_NAV.search(name):
            found.append(entry)
    return sorted(found)


def ecef_to_llh(xyz: Sequence[float]) -> tuple[float, float, float]:
    """``(latitude, longitude, height)`` on the WGS84 ellipsoid, in degrees and metres, of a position in the Earth-
    centred Earth-fixed frame (x, y, z in metres), as the header of a RINEX file gives its antenna. The height is
    above the ellipsoid, not above the sea."""
    values = [float(v) for v in xyz]
    if len(values) != 3:
        raise ValueError("ecef_to_llh takes the three coordinates x, y, z")
    x, y, z = values
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1.0 - _E2))
    for _ in range(30):  # every step cuts the error by about e2 (1/150): a handful reach the precision of a double
        sin_lat = math.sin(lat)
        n = _A / math.sqrt(1.0 - _E2 * sin_lat * sin_lat)
        step = math.atan2(z + _E2 * n * sin_lat, p)
        done = abs(step - lat) < 1e-14
        lat = step
        if done:
            break
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    # not p / cos(lat) - n: that one is lost at the poles
    height = p * cos_lat + z * sin_lat - _A * math.sqrt(1.0 - _E2 * sin_lat * sin_lat)
    return math.degrees(lat), math.degrees(math.atan2(y, x)), height
