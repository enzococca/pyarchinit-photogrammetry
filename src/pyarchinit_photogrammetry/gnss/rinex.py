"""What a RINEX file holds: the receiver and antenna in its header, the time it covers, the navigation files that
go with it and the position of its antenna. Plain Python: nothing here needs RTKLIB."""
from __future__ import annotations

import gzip
import math
import re
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

SYSTEMS = "GRECJIS"  # GPS, GLONASS, Galileo, BeiDou, QZSS, NavIC, SBAS: the letters of the satellite ids

_A = 6378137.0  # WGS84 semi-major axis (m)
_E2 = 0.00669437999014  # WGS84 first eccentricity squared

_HATANAKA_NAME = re.compile(r"\.(?:crx|\d\dd)$", re.IGNORECASE)
_HATANAKA_MESSAGE = ("{name}: Hatanaka-compressed RINEX is not supported yet. Decompress it with CRX2RNX first "
                     "(support is planned)")
_NOT_RINEX_MESSAGE = "{name}: not a RINEX file"
# the archives of the stations (BKG, CDDIS...) also give their files compressed with the Unix compress (.Z) or zipped;
# only gzip is read as it is. By the name, or by the first bytes for a file renamed
_ARCHIVES = {".z": "compress (.Z)", ".zip": "zip"}
_ARCHIVE_MAGIC = ((b"\x1f\x9d", "compress (.Z)"), (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"))
_ARCHIVE_MESSAGE = "{name} is compressed with {kind}: decompress it first (only gzip files are read as they are)"

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
_OBS_YEAR_EXTENSION = re.compile(r"\.(\d\d)o$", re.IGNORECASE)
_LONG_NAV = re.compile(r"_[A-Z]N\.rnx$", re.IGNORECASE)
# the daily broadcast file of the IGS in RINEX 2 (brdcDDD0.YYn GPS, .YYg GLONASS), of any station's day
_BRDC = re.compile(r"^brdc(\d{3})\d\.(\d\d)[ng]$", re.IGNORECASE)
# the day of an observation file by its name: RINEX 2 short names ssssDDDf.YYo, RINEX 3 long names ..._YYYYDDDHHMM_...
_OBS_SHORT_DAY = re.compile(r"^[a-z0-9]{4}(\d{3})[a-z0-9]\.(\d\d)o$", re.IGNORECASE)
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
    compressed: str | None  # "gzip" when the file is a .gz, which is read as it is


# --- reading ----------------------------------------------------------------------------------------------------


def _without_gz(name: str) -> str:
    return name[:-3] if name.lower().endswith(".gz") else name


def _open(path: Path):
    """The file as text, a .gz decompressed on the fly. Bytes that are not ASCII are replaced, not refused."""
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="ascii", errors="replace")
    return open(path, "rt", encoding="ascii", errors="replace")


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


def _archive(path: Path) -> str | None:
    """The compression of a file that is not read as it is: Unix compress or zip, by its extension or by its first
    bytes; None for any other file."""
    kind = _ARCHIVES.get(path.suffix.lower())
    if kind:
        return kind
    with open(path, "rb") as f:
        start = f.read(4)
    return next((kind for magic, kind in _ARCHIVE_MAGIC if start.startswith(magic)), None)


def _read_header(path: Path) -> tuple[RinexInfo, bool, int | None]:
    """The header of the file, whether the systems are still to be found in the data, and the leap seconds the
    header gives (None when it gives none)."""
    name = path.name
    kind = _archive(path)
    if kind:
        raise ValueError(_ARCHIVE_MESSAGE.format(name=name, kind=kind))
    if _HATANAKA_NAME.search(_without_gz(name)):
        raise ValueError(_HATANAKA_MESSAGE.format(name=name))
    labels: dict[str, list[str]] = {}
    try:
        with _open(path) as f:
            first = f.readline(1000)
            if first[60:].strip().startswith("CRINEX"):
                raise ValueError(_HATANAKA_MESSAGE.format(name=name))
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
        time_system=time_system, systems=ordered, compressed="gzip" if path.suffix.lower() == ".gz" else None)
    return info, scan, leap


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
    if path.suffix.lower() != ".gz":
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
    A file that is not RINEX, or is Hatanaka-compressed, is a ``ValueError`` naming it."""
    path = Path(path)
    info, scan, leap = _read_header(path)
    if info.kind == "obs" and (info.first is None or info.last is None):
        span = _span(path, info, leap)
        if span:
            info.first, info.last = info.first or span[0], info.last or span[1]
    if scan:
        info.systems = _data_systems(path, info)
    return info


def time_span(path: str | Path) -> tuple[datetime, datetime] | None:
    """The first and last epoch of observations as the epochs of the file give them (not as the header declares),
    in GPS time; None when the file has no epochs."""
    path = Path(path)
    info, _, leap = _read_header(path)
    if info.kind != "obs":
        raise ValueError(f"{path.name}: a navigation file has no observation epochs")
    return _span(path, info, leap)


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
    drone writes ``*_Rinex.nav`` beside ``*_Rinex.obs``), each also as a .gz. Sorted; never the file itself, never a
    hidden file (on an external drive macOS leaves a ``._x.nav`` beside every ``x.nav``)."""
    obs = Path(obs)
    own = _without_gz(obs.name)
    stem = own.rsplit(".", 1)[0]
    obs_year = _OBS_YEAR_EXTENSION.search(own)
    found = []
    for entry in obs.parent.iterdir():
        if entry.name == obs.name or entry.name.startswith(".") or not entry.is_file():
            continue
        name = _without_gz(entry.name)
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
