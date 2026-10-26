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

# the first line of an epoch: RINEX 2 has a 2-digit year and no marker, RINEX 3 a "> " and a 4-digit year; the last
# group is the epoch flag
_EPOCH_V2 = re.compile(r"^ (\d\d) +(\d{1,2}) +(\d{1,2}) +(\d{1,2}) +(\d{1,2}) +(\d{1,2}\.\d*)\s+(\d)")
_EPOCH_V3 = re.compile(r"^> +(\d{4}) +(\d{1,2}) +(\d{1,2}) +(\d{1,2}) +(\d{1,2}) +(\d{1,2}\.\d*)\s+(\d)")
_OBSERVATION_FLAGS = "016"  # 0 ok, 1 power failure before the epoch, 6 cycle slips; 2-5 are events, not data
_SATELLITES_V2 = re.compile(rf"^(?:[{SYSTEMS} ][ \d]\d)+\s*$")  # the satellite list of an epoch, continued
_SATELLITE_V3 = re.compile(rf"^([{SYSTEMS}])\d\d")
_NAV_RECORD_V3 = re.compile(rf"^([{SYSTEMS}])\d\d ")

_TAIL_BYTES = 1 << 20  # the last epoch is looked for in this many bytes from the end first, then in four times more
_TAIL_LIMIT = 64 << 20  # up to this many; a file that has no epoch in them is read through

_NAV_V2 = {"N": "G", "G": "R", "H": "S"}  # the file type of a RINEX 2 navigation file says its system
_NAV_YEAR_EXTENSION = re.compile(r"\.(\d\d)[ngplc]$", re.IGNORECASE)
_OBS_YEAR_EXTENSION = re.compile(r"\.(\d\d)o$", re.IGNORECASE)
_LONG_NAV = re.compile(r"_[A-Z]N\.rnx$", re.IGNORECASE)
_LONG_NAME = re.compile(r"^([A-Z0-9]{9})_[RSU]_\d{11}_", re.IGNORECASE)


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
    first: datetime | None  # of the first and last epoch, as the file writes them (GPS time for a receiver, DJI's
    last: datetime | None  # included), without a time zone
    systems: tuple[str, ...]  # the letters of the constellations, in the order of ``SYSTEMS``
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


def _when(year: int, month: int, day: int, hour: int, minute: int, seconds: float) -> datetime:
    return datetime(_year(year), month, day, hour, minute) + timedelta(seconds=seconds)


def _header_time(lines: list[str]) -> datetime | None:
    """The time in a ``TIME OF FIRST OBS`` / ``TIME OF LAST OBS`` line: year, month, day, hour, minute, seconds."""
    try:
        parts = lines[0].split()
        return _when(*(int(p) for p in parts[:5]), float(parts[5]))
    except (IndexError, ValueError):
        return None


def _numbers(text: str, count: int = 3, width: int = 14) -> tuple[float, ...] | None:
    """``count`` finite numbers written with spaces between them or in columns of ``width``, None when they are not."""
    parts = text.split()
    try:
        if len(parts) == count:
            values = tuple(float(p) for p in parts)
        else:  # run together, like "-3976219.5082-3382372.5671"
            values = tuple(float(text[i * width:(i + 1) * width]) for i in range(count))
    except ValueError:
        return None
    return values if all(math.isfinite(v) for v in values) else None


def _read_header(path: Path) -> tuple[RinexInfo, bool]:
    """The header of the file, and whether the systems are still to be found in the data."""
    name = path.name
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
    info = RinexInfo(
        version=version, kind=kind, marker=field("MARKER NAME"), receiver=field("REC # / TYPE / VERS", 20, 40),
        antenna=field("ANT # / TYPE", 20, 40), approx_xyz=xyz if xyz and any(xyz) else None,
        antenna_delta_hen=delta, interval=interval if interval is not None and interval > 0 else None,
        first=_header_time(labels.get("TIME OF FIRST OBS", [])), last=_header_time(labels.get("TIME OF LAST OBS", [])),
        systems=tuple(s for s in SYSTEMS if s in systems), compressed="gzip" if path.suffix.lower() == ".gz" else None)
    return info, scan


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


def _span(path: Path, version: float) -> tuple[datetime, datetime] | None:
    first = _first_epoch(path, version)
    if first is None:
        return None
    return first, _last_epoch(path, version) or first


def _sat_systems(text: str) -> set[str]:
    """The systems of a RINEX 2 satellite list (``G 3G 7R 5``): the letter of every three characters, blank is GPS."""
    return {"G" if text[i] == " " else text[i] for i in range(0, len(text.rstrip()), 3)}


def _data_systems(path: Path, info: RinexInfo) -> tuple[str, ...]:
    """The systems of a mixed file from its satellite ids: the first ten epochs of observations, every record of
    navigation messages."""
    found: set[str] = set()
    epochs = 0
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
        elif _EPOCH_V2.match(line):
            epochs += 1
            found |= _sat_systems(line[32:])
        elif not line[:32].strip() and _SATELLITES_V2.match(line[32:]):
            found |= _sat_systems(line[32:])
        if epochs > 10:
            break
    return tuple(s for s in SYSTEMS if s in found)


# --- the public interface ---------------------------------------------------------------------------------------


def header(path: str | Path) -> RinexInfo:
    """What the header of a RINEX observation or navigation file says. The first and last epoch of observations
    that the header does not give are read from the epochs, and the systems of a mixed file from its satellite
    ids. A file that is not RINEX, or is Hatanaka-compressed, is a ``ValueError`` naming it."""
    path = Path(path)
    info, scan = _read_header(path)
    if info.kind == "obs" and (info.first is None or info.last is None):
        span = _span(path, info.version)
        if span:
            info.first, info.last = info.first or span[0], info.last or span[1]
    if scan:
        info.systems = _data_systems(path, info)
    return info


def time_span(path: str | Path) -> tuple[datetime, datetime] | None:
    """The first and last epoch of observations as the epochs of the file give them (not as the header declares),
    in the time of the file; None when the file has no epochs."""
    path = Path(path)
    info, _ = _read_header(path)
    if info.kind != "obs":
        raise ValueError(f"{path.name}: a navigation file has no observation epochs")
    return _span(path, info.version)


def find_companions(obs: str | Path) -> list[Path]:
    """The navigation files that go with an observation file, from the folder it is in: the files with its name
    and a navigation extension (``.nav``, ``.24n`` ``.24g`` ``.24p`` ``.24l`` ``.24c`` for the year of the
    observations), the RINEX 3 ``*_MN.rnx`` and ``*_GN.rnx`` of its station, and every ``.nav`` (a DJI drone
    writes ``*_Rinex.nav`` beside ``*_Rinex.obs``), each also as a .gz. Sorted; never the file itself."""
    obs = Path(obs)
    own = _without_gz(obs.name)
    stem = own.rsplit(".", 1)[0]
    obs_year = _OBS_YEAR_EXTENSION.search(own)
    obs_station = _LONG_NAME.match(own)
    found = []
    for entry in obs.parent.iterdir():
        if entry.name == obs.name or not entry.is_file():
            continue
        name = _without_gz(entry.name)
        year_ext = _NAV_YEAR_EXTENSION.search(name)
        if name.lower().endswith(".nav"):
            found.append(entry)
        elif (year_ext and name[:year_ext.start()].lower() == stem.lower()
              and (not obs_year or year_ext.group(1) == obs_year.group(1))):
            found.append(entry)
        elif _LONG_NAV.search(name) and (not obs_station or name.split("_")[0].upper() == obs_station.group(1).upper()):
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
