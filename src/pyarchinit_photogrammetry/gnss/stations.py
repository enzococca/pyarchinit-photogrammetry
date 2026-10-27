"""Permanent GNSS stations near a survey, and their coordinates at the epoch of a flight.

The list is the union of the stations of the EPN and of RING (M3G, the metadata system of EUREF and the EPN) and of
the network of the IGS, each with an approximate position - enough to sort them by distance. The precise position
comes, in this order, from the multi-year solution of the EPN in IGS20, from IGc20 (the IGS realisation of ITRF2020),
from the daily solutions of RING (a straight line through the last two years) and, when there is nothing else, from
the approximate position, which is only good to metres. IGc20 is used only where its own header allows it: not for a
station that has post-seismic deformation terms (``psd_IGc20.snx``), and only with the solution that
``soln_IGc20.snx`` says is valid at the epoch, which ``IGc20.ssc`` lacks after a break that came after March 2025.
Everything is in ITRF2020 (IGS20) and at the epoch asked for: the stations move with their plate, about 2 cm a year in
Europe, and a base is only good to the centimetre at the epoch of its coordinates.

The addresses, and the layout of what they return, are those of ``tests/data/gnss/README.md``; the files that are
downloaded are kept in ``stations`` in the GNSS folder (:func:`download.gnss_dir`).

A station can also be the base of a survey: :func:`observation_files` says which files of the archives hold its
observations for the hours of a flight (at 1 s or at 30 s, BKG's data centre for the stations of the EPN and of the IGS,
RING's for those of RING), :func:`download_observations` brings them, one at a time, into ``stations/<id>`` and joins
them into one RINEX file, and :func:`download_navigation` brings the broadcast navigation messages of the days of the
flight. The names of the files are never made up: the folder of the archive is listed and the names that are the
station's are taken."""
from __future__ import annotations

import calendar
import dataclasses
import json
import logging
import math
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .. import _native
from ..core.progress import as_progress
from . import download, rinex

log = logging.getLogger(__name__)

# The services. M3G_GEOJSON takes the network (EPN or RING), RING_POS the 9-character id of the station.
M3G_GEOJSON = "https://gnss-metadata.eu/v1/sitelog/geojson?network={network}&pagination=0"
IGS_NETWORK = "https://files.igs.org/pub/station/general/IGSNetwork.json"
EPN_REFERENCE_FRAME = "https://epncb.oma.be/pub/product/referenceframe/"
IGS_SSC = "https://files.igs.org/pub/station/coord/IGc20/IGc20.ssc"
RING_POS = "https://webring.gm.ingv.it:44324/products/POS/IGS2/{id}/{id}.gipsyX.IGS20.pos"

NETWORKS = ("EPN", "IGS", "RING")
FRAME = "ITRF2020"
LIST_AGE = timedelta(days=7)  # the list of stations is fetched again after a week
IGS_SSC_AGE = timedelta(days=30)  # the file of IGc20 and its post-seismic models (made once a year) after a month
IGS_SOLN_AGE = timedelta(days=7)  # the list of its discontinuities, which grows as breaks come, after a week
RING_POS_AGE = timedelta(days=7)  # a series of RING (it grows every few months) after a week
EPN_FOLDERS = 3  # how many of the newest Cwwww folders are looked into for the multi-year solution
MAX_DISTANCE = 1000.0  # m: a precise position farther than this from the position in the list is another station
WINDOW = 1.0  # years: half the span of the series of RING that is fitted
MIN_DAYS = 30  # the fewest daily positions that make a series

_ID = re.compile(r"[A-Z0-9]{9}")  # a station: four characters, the monument and receiver numbers, the country
_WEEK = re.compile(r"C(\d{4})/?")
# The multi-year solution of the EPN: with no frame in the name it is the IGS20 one (the others end in _ETRF2000 ...).
# Between the dates and _SOL there is only the length (00U): a frame there would be read as ITRF2020.
_EPN_NAME = r"EUR0OPSSNX_\d{7}_(?P<end>\d{7})_\d{2}[A-Z]_SOL\.SSC"
_EPN_SSC = re.compile(_EPN_NAME, re.IGNORECASE)
# a copy kept in the stations folder: epn_Cwwww_ and the name, nothing after it (not the .part of a download that was killed)
_EPN_COPY = re.compile(rf"epn_C(?P<week>\d{{4}})_{_EPN_NAME}", re.IGNORECASE)
_SINEX_TYPES = ("STAX", "STAY", "STAZ", "VELX", "VELY", "VELZ")
_PSD_TYPE = re.compile(r"[AT](?:LOG|EXP)_[ENU]")  # the terms of a post-seismic model: amplitude and time, log and exp
# CODE PT SOLN T DATA_START DATA_END EVENT comment: EVENT is P for a break of the position and V for one of the velocity
_DISCONTINUITY = re.compile(r"\s*(\w{4})\s+(\S+)\s+(\d+)\s+\w\s+(\d\d:\d{3}:\d{5})\s+(\d\d:\d{3}:\d{5})\s+([PV])\b")


def _now() -> datetime:
    """The time (UTC); the tests move it."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Station:
    """A permanent station of the list: ``id`` is its 9-character name (``"AQUI00ITA"``), ``name`` the place,
    ``networks`` the ones among ``"EPN"``, ``"IGS"`` and ``"RING"`` that list it, ``lat``, ``lon`` and ``h`` (degrees
    and metres above the ellipsoid) and ``xyz`` (m) its approximate position, from the site log: good to metres."""

    id: str
    name: str
    networks: tuple[str, ...]
    lat: float
    lon: float
    h: float
    xyz: tuple[float, float, float]


@dataclass(frozen=True)
class StationCoordinates:
    """The position of ``station`` at ``epoch`` (decimal year) in ``frame`` (``"ITRF2020"``, the same as IGS20):
    ``xyz`` (m) and ``llh`` (degrees, degrees, metres above the ellipsoid). ``source`` is ``"EPN"``, ``"IGS"``,
    ``"RING"`` or ``"approximate"`` (the position of the list, good to metres). ``sigma`` is the 1-sigma error of
    ``xyz``, three numbers in metres, or None when it is not known: from the EPN and the IGS the formal error of the
    position at the reference epoch and of the velocity brought to ``epoch``; from RING the scatter of the daily
    positions around the line. ``citation`` says what to credit."""

    station: Station
    xyz: tuple[float, float, float]
    llh: tuple[float, float, float]
    frame: str
    epoch: float
    source: str
    sigma: tuple[float, float, float] | None
    citation: str


class _Absent(Exception):
    """The station is not in this source: the next one is tried, with no complaint."""


# --- the list of stations ---------------------------------------------------------------------------------------


def _stations_dir() -> Path:
    folder = download.gnss_dir() / "stations"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _removed(text) -> bool:
    """Whether the date of removal in a site log has come."""
    if not text:
        return False
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return True  # a date nobody can read is still a date of removal
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= _now()


def _parse_m3g(data) -> list[Station]:
    """The stations in the answer of M3G: an array of GeoJSON features (a FeatureCollection is read too), without
    those that were removed. Their id is in ``properties.id``, the approximate position in
    ``properties.location.approximatePositionITRF`` (X, Y, Z) and ``approximatePositionGRS80`` (latitude, longitude,
    elevation), the removal in ``properties.siteForm.dateRemoved``."""
    features = data["features"] if isinstance(data, dict) else data
    if not isinstance(features, list):
        raise ValueError("not a list of features")
    found, skipped = [], 0
    for feature in features:
        try:
            props = feature["properties"]
            sid = str(props["id"]).strip().upper()
            form = props.get("siteForm") or {}
            location = props["location"]
            if not _ID.fullmatch(sid):
                raise ValueError(sid)
            if _removed(form.get("dateRemoved")):
                continue
            itrf, grs80 = location["approximatePositionITRF"], location["approximatePositionGRS80"]
            xyz = tuple(float(itrf[axis]) for axis in "XYZ")
            lat, lon, h = float(grs80["latitude"]), float(grs80["longitude"]), float(grs80["elevation"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        site = str(form.get("siteName") or "").strip()
        name = site if site and site.upper() != sid else str(location.get("location") or "").strip() or sid
        found.append(Station(sid, name, (), lat, lon, h, xyz))
    if skipped:
        log.warning("%d entries of the list of M3G have no usable id or position and are left out", skipped)
    if not found and features:
        raise ValueError("no entry has an id and a position")
    return found


def _parse_igs(data) -> list[Station]:
    """The stations of the network of the IGS: an object keyed by the 9-character id, with ``X``, ``Y`` and ``Z`` in
    metres. Its latitude, longitude and height are strings rounded to 0.001 degrees (100 m): they are computed from
    the XYZ instead. There is no name."""
    if not isinstance(data, dict):
        raise ValueError("not an object of stations")
    found = []
    for key, entry in data.items():
        try:
            sid = str(key).strip().upper()
            xyz = tuple(float(entry[axis]) for axis in "XYZ")
        except (KeyError, TypeError, ValueError):
            continue
        if _ID.fullmatch(sid):
            lat, lon, h = rinex.ecef_to_llh(xyz)
            found.append(Station(sid, sid, (), lat, lon, h, xyz))
    if not found and data:
        raise ValueError("no entry has an id and a position")
    return found


def _merge(found: dict[str, Station], network: str, parsed: list[Station]) -> None:
    """``parsed`` into ``found`` by id: a station listed twice is one, with both networks, and the position and the name
    of the list that came first (EPN, RING, IGS: the site logs before the table of the IGS)."""
    for station in parsed:
        old = found.get(station.id)
        if old is None:
            found[station.id] = dataclasses.replace(station, networks=(network,))
        else:
            networks = tuple(n for n in NETWORKS if n in old.networks or n == network)
            name = old.name if old.name != old.id or station.name == station.id else station.name
            found[station.id] = dataclasses.replace(old, networks=networks, name=name)


def _download_list(progress) -> list[Station]:
    sources = (("EPN", M3G_GEOJSON.format(network="EPN")), ("RING", M3G_GEOJSON.format(network="RING")),
               ("IGS", IGS_NETWORK))
    folder = Path(tempfile.mkdtemp(prefix="list-", dir=_stations_dir()))
    found: dict[str, Station] = {}
    try:
        for n, (network, url) in enumerate(sources):
            path = download.fetch(url, folder / f"{network}.json", progress.sub(n / 3, 1 / 3))
            try:
                data = json.loads(path.read_bytes())
                parsed = _parse_igs(data) if network == "IGS" else _parse_m3g(data)
            except (ValueError, KeyError, TypeError) as exc:  # a page of an error is not a list
                raise download.DownloadError(f"{url}: not the list of stations that was expected ({exc})",
                                             url) from exc
            _merge(found, network, parsed)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return sorted(found.values(), key=lambda station: station.id)


def _saved_list() -> tuple[datetime, list[Station]] | None:
    path = _stations_dir() / "stations.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(payload["fetched"])
        listed = [Station(s["id"], s["name"], tuple(s["networks"]), float(s["lat"]), float(s["lon"]), float(s["h"]),
                          tuple(float(v) for v in s["xyz"])) for s in payload["stations"]]
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("the saved list of stations (%s) cannot be read and is ignored: %s", path, exc)
        return None
    return (fetched if fetched.tzinfo else fetched.replace(tzinfo=timezone.utc)), listed


def station_list(refresh: bool = False, progress=None) -> list[Station]:
    """The permanent stations of the EPN, the IGS and RING, sorted by id, one entry for a station whatever the
    number of networks; stations that were removed are left out. It is saved in ``stations/stations.json`` of the GNSS
    folder with the time it was fetched and used for a week; ``refresh=True`` fetches it anyway. When it cannot be
    fetched the saved one is used, with a warning in the log, however old it is; without one it is a
    :class:`download.DownloadError` that names the address."""
    progress = as_progress(progress)
    now = _now()
    saved = _saved_list()
    if saved is not None and not refresh and now - saved[0] < LIST_AGE:
        return saved[1]
    try:
        found = _download_list(progress)
    except download.DownloadError as exc:
        if saved is None:
            raise
        log.warning("%s; using the list of stations saved on %s", exc, saved[0].date().isoformat())
        return saved[1]
    path = _stations_dir() / "stations.json"
    work = path.with_name(path.name + ".part")
    payload = {"version": 1, "fetched": now.isoformat(timespec="seconds"),
               "stations": [dataclasses.asdict(station) for station in found]}
    work.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(work, path)
    progress(1.0, f"{len(found)} permanent stations")
    return found


def nearby(lat: float, lon: float, limit: int = 10, within_km: float = 200.0,
           stations: list[Station] | None = None) -> list[tuple[Station, float]]:
    """The stations within ``within_km`` of a point, the nearest first, at most ``limit`` of them, each with its
    distance in km along the ellipsoid. ``stations`` is the pool (by default :func:`station_list`)."""
    from pyproj import Geod

    pool = list(station_list() if stations is None else stations)
    if not pool or limit <= 0:
        return []
    n = len(pool)
    _az1, _az2, metres = Geod(ellps="GRS80").inv([lon] * n, [lat] * n, [s.lon for s in pool], [s.lat for s in pool])
    ranked = sorted(zip(metres, pool), key=lambda pair: (pair[0], pair[1].id))
    return [(station, d / 1000.0) for d, station in ranked if d / 1000.0 <= within_km][:limit]


# --- files that are downloaded and kept ---------------------------------------------------------------------------


def _cached(url: str, name: str, max_age: timedelta | None, progress) -> Path:
    """The file of ``url`` in the stations folder as ``name``: the copy there when it is younger than ``max_age`` (any
    age for None), else fetched again - and the old copy, with a warning, when that fails."""
    path = _stations_dir() / name
    saved = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) if path.exists() else None
    if saved is not None and (max_age is None or _now() - saved < max_age):
        return path
    try:
        return download.fetch(url, path, progress)
    except download.DownloadError as exc:
        if saved is None:
            raise
        log.warning("%s; using the copy saved on %s", exc, saved.date().isoformat())
        return path


def _saved_epn_solution() -> tuple[Path, str] | None:
    """The newest complete copy of the EPN solution in the stations folder and the name of its week (``"C2400"``), or None:
    the name has to be exactly ``epn_Cwwww_`` and the name of the file, so a ``.part`` that a download left when it was
    killed is never one."""
    copies = []
    for path in _stations_dir().iterdir():
        match = _EPN_COPY.fullmatch(path.name)
        if match and path.is_file():
            copies.append(((int(match.group("week")), match.group("end")), path))
    if not copies:
        return None
    (week, _end), path = max(copies)
    return path, f"C{week}"


def _fresh_epn_file(progress) -> tuple[Path, str]:
    """The multi-year solution of the EPN in IGS20 of the newest ``Cwwww`` folder that has one, and the name of the
    folder (``"C2400"``): the copy kept when the week is the one that is kept, else downloaded."""
    root = EPN_REFERENCE_FRAME if EPN_REFERENCE_FRAME.endswith("/") else EPN_REFERENCE_FRAME + "/"
    names = download.listing(root)
    weeks = sorted((int(m.group(1)) for m in map(_WEEK.fullmatch, names) if m), reverse=True)
    failure: download.DownloadError | None = None
    for week in weeks[:EPN_FOLDERS]:
        folder = f"C{week}"
        url = f"{root}{folder}/"
        try:
            files = sorted((m for m in map(_EPN_SSC.fullmatch, download.listing(url)) if m),
                           key=lambda m: m.group("end"))
        except download.DownloadError as exc:  # the folder before may be fine
            failure = exc
            continue
        if files:
            return _cached(url + files[-1].group(0), f"epn_{folder}_{files[-1].group(0)}", None, progress), folder
    raise failure or download.NotPublished(f"{root}: no folder of the EPN has the multi-year solution in IGS20", root)


def _epn_file(progress) -> tuple[Path, str]:
    """The multi-year solution of the EPN in IGS20 and the name of its folder (``"C2400"``). Whenever a fresh one cannot
    be had - the archive does not answer, lists no weekly folder, cannot list them or the newest has a file that does not
    come - the newest complete copy that is kept is used, with a warning that names its week; without one the error
    stands (an archive that answers and has no solution is an error too, not a quiet miss)."""
    try:
        return _fresh_epn_file(progress)
    except download.DownloadError as exc:
        kept = _saved_epn_solution()
        if kept is None:
            if isinstance(exc, download.NotPublished):
                raise download.DownloadError(str(exc), exc.url) from exc
            raise
        path, folder = kept
        log.warning("%s; using the solution of the EPN %s saved on %s", exc, folder,
                    datetime.fromtimestamp(path.stat().st_mtime).date().isoformat())
        return path, folder


# --- SINEX: the solutions of a station ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Solution:
    """One piece of the coordinates of a station: a position ``xyz`` at the epoch ``ref`` and a velocity, valid from
    ``start`` to ``end`` (decimal years, None for no limit), with the 1-sigma of each."""

    number: int
    start: float | None
    end: float | None
    ref: float
    xyz: tuple[float, float, float]
    velocity: tuple[float, float, float]
    sx: tuple[float, float, float]
    sv: tuple[float, float, float]


def _sinex_epoch(text: str) -> float | None:
    """The decimal year of a SINEX epoch (``YY:DDD:SSSSS``; two digits of year are 1950-2049), or None for the day 0
    that SINEX writes where there is no date (``00:000:00000``)."""
    match = re.fullmatch(r"(\d{2}|\d{4}):(\d{3}):(\d{5})", text.strip())
    if not match:
        raise ValueError(f"{text.strip()!r} is not a SINEX epoch (YY:DDD:SSSSS)")
    year, day, seconds = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if day == 0:
        return None
    if len(match.group(1)) == 2:
        year += 2000 if year < 50 else 1900
    length = 366 if calendar.isleap(year) else 365
    return year + ((day - 1) * 86400 + seconds) / (length * 86400)


def _float(text: str) -> float:
    return float(text.replace("D", "E").replace("d", "e"))


def _sinex_rows(path: Path, blocks: set[str]):
    """The data lines of a SINEX file as ``(block, number, line)``: those inside a block that are not blank or a comment.
    The names of the blocks met are added to ``blocks``. A ``ValueError`` names the file when its first line does not
    start with ``%=SNX``."""
    with open(path, encoding="utf-8", errors="replace") as f:
        if not f.readline().startswith("%=SNX"):
            raise ValueError(f"{path.name}: not a SINEX file (the first line should start with %=SNX): its layout is "
                             "not one this program reads")
        block = ""
        for number, line in enumerate(f, start=2):
            if line.startswith("+"):
                block = (line[1:].split() or [""])[0]
                blocks.add(block)
            elif line.startswith("-"):
                block = ""
            elif line.strip() and line[0] not in "*%":
                yield block, number, line


def _read_sinex(path: Path, code: str) -> list[_Solution]:
    """The solutions of the station ``code`` (4 characters) in a SINEX file: from ``SOLUTION/EPOCHS`` the span of each
    (``DATA_START``, ``DATA_END``) and from ``SOLUTION/ESTIMATE`` its ``STAX``..``STAZ`` and ``VELX``..``VELZ`` at the
    reference epoch. IGc20.ssc is such a file. A ``ValueError`` names the file when it is not SINEX or cannot be read,
    and ``_Absent`` is raised when the station is not in it."""
    spans: dict[int, tuple[float | None, float | None]] = {}
    values: dict[int, dict[str, tuple[float, float]]] = {}
    refs: dict[int, float | None] = {}
    points: set[str] = set()
    blocks: set[str] = set()
    for block, number, line in _sinex_rows(path, blocks):
        fields = line.split()
        try:
            if block == "SOLUTION/EPOCHS":
                # CODE PT SOLN T DATA_START DATA_END MEAN_EPOCH: counted from the end, the point may be blank
                if len(fields) >= 6 and fields[0].upper() == code:
                    points.add(fields[1] if len(fields) == 7 else "")
                    spans[int(fields[-5])] = (_sinex_epoch(fields[-3]), _sinex_epoch(fields[-2]))
            elif block == "SOLUTION/ESTIMATE":
                # INDEX TYPE CODE PT SOLN REF_EPOCH UNIT S VALUE STD_DEV
                if len(fields) >= 9 and fields[2].upper() == code and fields[1] in _SINEX_TYPES:
                    kind, unit = fields[1], fields[-4]
                    if unit != ("m" if kind.startswith("STA") else "m/y"):
                        raise ValueError(f"{kind} in {unit}")
                    soln = int(fields[-6])
                    values.setdefault(soln, {})[kind] = (_float(fields[-2]), _float(fields[-1]))
                    if kind == "STAX":
                        refs[soln] = _sinex_epoch(fields[-5])
        except ValueError as exc:
            raise ValueError(f"{path.name}, line {number}: {exc}") from None
    if "SOLUTION/ESTIMATE" not in blocks:
        raise ValueError(f"{path.name}: not a SINEX file of coordinates (it has no SOLUTION/ESTIMATE block)")
    if not values:
        raise _Absent(f"{code} is not in {path.name}")
    if len(points) > 1:
        raise ValueError(f"{path.name}: {code} has several points ({', '.join(sorted(points))})")
    found = []
    for soln, got in sorted(values.items()):
        if soln in spans and refs.get(soln) is not None and all(kind in got for kind in _SINEX_TYPES):
            start, end = spans[soln]
            found.append(_Solution(
                soln, start, end, refs[soln], tuple(got[k][0] for k in _SINEX_TYPES[:3]),
                tuple(got[k][0] for k in _SINEX_TYPES[3:]), tuple(got[k][1] for k in _SINEX_TYPES[:3]),
                tuple(got[k][1] for k in _SINEX_TYPES[3:])))
    if not found:
        raise ValueError(f"{path.name}: {code} has no solution with a position, a velocity, an epoch for them and "
                         "the span of its data")
    return found


def _read_psd(path: Path, code: str) -> list[float | None]:
    """The epochs (decimal years; None where SINEX gives none) of the earthquakes for which a SINEX file of
    post-seismic models (``psd_IGc20.snx``) has terms for the station ``code``: ``ALOG_E``, ``TLOG_E``, ``AEXP_N``,
    ``TEXP_N`` ... in ``SOLUTION/ESTIMATE``, whose reference epoch is the earthquake. Empty when it has none."""
    epochs: list[float | None] = []
    blocks: set[str] = set()
    for block, number, line in _sinex_rows(path, blocks):
        fields = line.split()
        if (block == "SOLUTION/ESTIMATE" and len(fields) >= 9 and fields[2].upper() == code
                and _PSD_TYPE.fullmatch(fields[1])):
            try:
                epoch = _sinex_epoch(fields[-5])
            except ValueError as exc:
                raise ValueError(f"{path.name}, line {number}: {exc}") from None
            if epoch not in epochs:
                epochs.append(epoch)
    if "SOLUTION/ESTIMATE" not in blocks:
        raise ValueError(f"{path.name}: not a SINEX file of post-seismic models (it has no SOLUTION/ESTIMATE block)")
    return sorted(epochs, key=lambda e: -math.inf if e is None else e)


def _read_discontinuities(path: Path, code: str) -> list[tuple[int, float | None, float | None]]:
    """The position solutions of the station ``code`` in a SINEX list of discontinuities (``soln_IGc20.snx``): ``(number,
    start, end)`` in decimal years, None for no limit. These are the validity periods of the solutions, which are
    not the spans of their data; the rows of the breaks of the velocity (event ``V``) are left out. A ``ValueError``
    names the file when it is not such a list or has nothing about the station."""
    spans: list[tuple[int, float | None, float | None]] = []
    blocks: set[str] = set()
    for block, number, line in _sinex_rows(path, blocks):
        match = _DISCONTINUITY.match(line) if block == "SOLUTION/DISCONTINUITY" else None
        if match and match.group(1).upper() == code and match.group(6) == "P":
            try:
                spans.append((int(match.group(3)), _sinex_epoch(match.group(4)), _sinex_epoch(match.group(5))))
            except ValueError as exc:
                raise ValueError(f"{path.name}, line {number}: {exc}") from None
    if "SOLUTION/DISCONTINUITY" not in blocks:
        raise ValueError(f"{path.name}: not a SINEX list of discontinuities (it has no SOLUTION/DISCONTINUITY block)")
    if not spans:
        raise ValueError(f"{path.name} has no position solution of {code}")
    return sorted(spans)


def _valid_at(spans: list[tuple[int, float | None, float | None]], epoch: float):
    """The ``(number, start, end)`` among ``spans`` that is valid at ``epoch``, from its start up to but not including its
    end (at a break the later one holds), or None."""
    valid = [s for s in spans if (s[1] is None or s[1] <= epoch) and (s[2] is None or epoch < s[2])]
    return max(valid, key=lambda s: s[0]) if valid else None


def _covering(solutions: list[_Solution], epoch: float) -> _Solution:
    """The solution valid at ``epoch``. Beyond the last one (the data of a frame end before the flight) the last one
    goes on; in a gap between two, or before the first, the nearest in time; where two meet, the later."""
    def key(solution: _Solution):
        lo = -math.inf if solution.start is None else solution.start
        hi = math.inf if solution.end is None else solution.end
        return max(lo - epoch, epoch - hi, 0.0), -lo

    return min(solutions, key=key)


def _same_place(station: Station, xyz, where: str) -> None:
    """A position that is farther than :data:`MAX_DISTANCE` from the one in the list is of another station: the files
    give four characters, and two stations can have the same."""
    away = float(np.linalg.norm(np.array(xyz) - np.array(station.xyz)))
    if away > MAX_DISTANCE:
        raise ValueError(f"{where} has a station with the code {station.id[:4]} {away / 1000:.0f} km from "
                         f"{station.id}: another station")


def _at_epoch(station: Station, epoch: float, solution: _Solution, where: str, source: str,
              citation: str) -> StationCoordinates:
    """The position of ``station`` at ``epoch`` on the line of ``solution``, with its error brought to the epoch."""
    dt = epoch - solution.ref
    xyz = tuple(float(x + v * dt) for x, v in zip(solution.xyz, solution.velocity))
    sigma = tuple(math.hypot(sx, sv * dt) for sx, sv in zip(solution.sx, solution.sv))
    _same_place(station, xyz, where)
    span = f"{solution.start or 0:.2f} to {solution.end or 0:.2f}"
    log.info("%s: solution %d of %s (data %s) at %.3f from %.1f", station.id, solution.number, where, span, epoch,
             solution.ref)
    return StationCoordinates(station, xyz, tuple(rinex.ecef_to_llh(xyz)), FRAME, epoch, source, sigma, citation)


def _from_epn(station: Station, epoch: float, progress) -> StationCoordinates | None:
    """The EPN solution is read as a SINEX file, like IGc20.ssc, whose layout is the only one of its family that has been
    seen: the Central Bureau did not answer when this was written (2026-10-27). A file laid out in another way is not
    guessed at: it is a ``ValueError`` that names it, and the next source answers."""
    if "EPN" not in station.networks:
        return None
    path, folder = _epn_file(progress)
    citation = (f"EUREF Permanent GNSS Network (EPN), multi-year position and velocity solution {folder} in IGS20, "
                f"EPN Central Bureau, Royal Observatory of Belgium, doi:10.24414/ROB-EUREF-{folder}, CC BY 4.0")
    solution = _covering(_read_sinex(path, station.id[:4].upper()), epoch)
    return _at_epoch(station, epoch, solution, path.name, "EPN", citation)


def _igs_companion(name: str, max_age: timedelta, progress) -> Path:
    """A file of the folder of IGc20.ssc that says when its coordinates can be used, kept in the same cache. Without it
    IGc20 is not used, and a plain ``DownloadError`` says so (a ``NotPublished`` would pass for a quiet miss)."""
    try:
        return _cached(IGS_SSC.rsplit("/", 1)[0] + "/" + name, name, max_age, progress)
    except download.DownloadError as exc:
        raise download.DownloadError(f"{exc}; IGc20 is not used without {name}", exc.url) from exc


def _from_igs(station: Station, epoch: float, progress) -> StationCoordinates | None:
    """IGc20 where its own header allows it. That header says that the positions and velocities of a station with
    post-seismic deformation models (``psd_IGc20.snx``) cannot be used without them, and that after a break that came
    later than the data of the frame (15 March 2025) the station has no coordinates: the solution that is valid at the
    epoch is the one ``soln_IGc20.snx`` gives, not the last one of IGc20.ssc. The line of an IGc20 solution is used
    only for a station without post-seismic terms and with the valid solution in the file; otherwise a ``ValueError``
    says why and the next source answers. The two files come from the folder of IGc20.ssc and are fetched only for a
    station that is in it."""
    if "IGS" not in station.networks:
        return None
    code = station.id[:4].upper()
    ssc = _cached(IGS_SSC, "IGc20.ssc", IGS_SSC_AGE, progress)
    solutions = _read_sinex(ssc, code)  # _Absent for a station that IGc20 does not have
    psd = _igs_companion("psd_IGc20.snx", IGS_SSC_AGE, progress)
    quakes = _read_psd(psd, code)
    if quakes:
        known = [q for q in quakes if q is not None]
        when = f" (the earthquake of {known[0]:.2f})" if known else ""
        raise ValueError(f"{code} has post-seismic deformation terms in {psd.name}{when}: IGc20 says that its positions "
                         "and velocities cannot be used without that model, which this program does not apply")
    soln = _igs_companion("soln_IGc20.snx", IGS_SOLN_AGE, progress)
    valid = _valid_at(_read_discontinuities(soln, code), epoch)
    if valid is None:
        raise ValueError(f"{soln.name} has no solution of {code} that is valid at {epoch:.2f}")
    number, start, _end = valid
    chosen = next((s for s in solutions if s.number == number), None)
    if chosen is None:
        held = ", ".join(str(s.number) for s in solutions)
        since = f" (since {start:.2f})" if start is not None else ""
        raise ValueError(f"{soln.name} puts {code} at {epoch:.2f} in solution {number}{since}, which {ssc.name} does not "
                         f"hold: it has solutions {held} of the station, none from a break after March 2025")
    citation = ("IGS reference frame IGc20 (the IGS realisation of ITRF2020-u2024, aligned to ITRF2020), "
                "International GNSS Service, https://files.igs.org/pub/station/coord/IGc20/IGc20.ssc")
    return _at_epoch(station, epoch, chosen, ssc.name, "IGS", citation)


# --- RING: a straight line through the daily positions ------------------------------------------------------------------


def _mjd_years(mjd: np.ndarray) -> np.ndarray:
    """Modified Julian days as decimal years."""
    moment = np.datetime64("1858-11-17T00:00:00") + np.round(mjd * 86400.0).astype("int64").astype("timedelta64[s]")
    year = moment.astype("datetime64[Y]")
    start, end = year.astype("datetime64[s]"), (year + np.timedelta64(1, "Y")).astype("datetime64[s]")
    return year.astype(int) + 1970 + (moment - start) / (end - start)


def _read_pos(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """The epochs (decimal years) and the positions (m, one row each) of a daily series of RING (``*.gipsyX.IGS20.pos``):
    the header names the frame, then 25 columns - date, time, MJD, X, Y, Z, their sigmas ... - for each day."""
    mjd, xyz = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        frame = re.search(r"Reference Frame\s*:\s*(\S+)", f.readline())
        if frame is None:
            raise ValueError(f"{path.name}: not a series of RING (the first line does not name the frame)")
        if frame.group(1) != "IGS20":
            raise ValueError(f"{path.name}: the series is in {frame.group(1)}, not in IGS20")
        for line in f:
            fields = line.split()
            if len(fields) >= 6 and len(fields[0]) == 8 and fields[0].isdigit():
                mjd.append(float(fields[2]))
                xyz.append((float(fields[3]), float(fields[4]), float(fields[5])))
    if len(mjd) < MIN_DAYS:
        raise ValueError(f"{path.name}: only {len(mjd)} daily positions")
    years = _mjd_years(np.array(mjd))
    order = np.argsort(years)
    return years[order], np.array(xyz)[order]


def _scatter(residuals: np.ndarray) -> float:
    """The robust standard deviation of ``residuals`` (the median absolute deviation, scaled to a Gaussian)."""
    return float(1.4826 * np.median(np.abs(residuals - np.median(residuals))))


def _robust_line(t: np.ndarray, y: np.ndarray) -> tuple[float, float, float, float]:
    """``y = a + b (t - tc)`` fitted by least squares with Huber weights, so that days that are far off do not pull the
    line. Returns ``a``, ``b``, ``tc`` and the scatter of the residuals."""
    tc = float(t.mean())
    design = np.column_stack([np.ones_like(t), t - tc])
    coef = np.linalg.lstsq(design, y, rcond=None)[0]
    for _ in range(50):
        residuals = y - design @ coef
        u = np.abs(residuals) / (1.345 * max(_scatter(residuals), 1e-6))
        root = np.sqrt(1.0 / np.maximum(u, 1.0))  # weight 1 up to 1.345 scatters, then falling as 1/u
        new = np.linalg.lstsq(design * root[:, None], y * root, rcond=None)[0]
        converged = float(np.abs(new - coef).max()) < 1e-10
        coef = new
        if converged:
            break
    return float(coef[0]), float(coef[1]), tc, _scatter(y - design @ coef)


def _from_ring(station: Station, epoch: float, progress) -> StationCoordinates | None:
    """The line through the last two years of the series of RING, at ``epoch``. For an epoch that the series covers
    it is the two years around it: it does not matter how the station moved after."""
    name = re.sub(r"[^A-Za-z0-9_-]", "_", station.id)  # the id of a Station made by hand is not trusted in a name
    path = _cached(RING_POS.format(id=station.id), f"ring_{name}.pos", RING_POS_AGE, progress)
    t, xyz = _read_pos(path)
    if t[-1] - t[0] <= 2 * WINDOW:
        keep = np.ones(len(t), dtype=bool)
    else:
        centre = min(max(epoch, t[0] + WINDOW), t[-1] - WINDOW)
        keep = (t >= centre - WINDOW) & (t <= centre + WINDOW)
    if keep.sum() < MIN_DAYS:
        raise ValueError(f"{path.name}: only {int(keep.sum())} daily positions around {epoch:.2f}")
    if epoch - t[-1] > 1.0:
        log.warning("%s: the series of RING ends %.1f years before %.2f, the line is extrapolated", station.id,
                    epoch - t[-1], epoch)
    fits = [_robust_line(t[keep], xyz[keep, k]) for k in range(3)]
    point = tuple(a + b * (epoch - tc) for a, b, tc, _s in fits)
    _same_place(station, point, path.name)
    citation = ("INGV RING, GipsyX daily solutions in IGS20 (https://doi.org/10.13127/gnss/gipos), CC BY 4.0: a "
                "straight line through two years of daily positions")
    return StationCoordinates(station, tuple(float(v) for v in point), tuple(rinex.ecef_to_llh(point)), FRAME, epoch,
                              "RING", tuple(s for _a, _b, _tc, s in fits), citation)


# --- coordinates ----------------------------------------------------------------------------------------------------


def coordinates(station: Station, epoch: float, progress=None) -> StationCoordinates:
    """The position of ``station`` at ``epoch`` (a decimal year such as 2026.5) in ITRF2020, the best that is available:
    the multi-year solution of the EPN in IGS20 (for the stations of the EPN), IGc20 (for those of the IGS, where its
    own conditions hold: no post-seismic terms, and the solution that is valid at ``epoch`` in the file), the daily
    solutions of RING, and in the end the approximate position of the list, with no ``sigma``. Where a station has a
    discontinuity (a new antenna, an earthquake) the solution that covers ``epoch`` is used. A source that cannot
    be reached, or does not make sense, is skipped with a message in the log; the files are kept for the next time and
    used when the network is gone."""
    if not (isinstance(epoch, (int, float)) and 1980.0 <= epoch <= 2200.0):
        raise ValueError(f"the epoch is a decimal year such as 2026.5, not {epoch!r}")
    epoch = float(epoch)
    progress = as_progress(progress)
    routes = (("EPN", _from_epn), ("IGS", _from_igs), ("RING", _from_ring))
    for n, (label, route) in enumerate(routes):
        try:
            found = route(station, epoch, progress.sub(n / 3, 1 / 3))
        except _Absent as exc:
            log.info("%s: %s", station.id, exc)
        except download.NotPublished as exc:
            log.info("%s: no coordinates from %s: %s", station.id, label, exc)
        except (download.DownloadError, ValueError, OSError) as exc:
            log.warning("%s: the coordinates from %s were not used: %s", station.id, label, exc)
        else:
            if found is not None:
                progress(1.0, f"{station.id}: coordinates from {found.source}")
                return found
    log.warning("%s: only the approximate position of the list is known, good to metres", station.id)
    progress(1.0, f"{station.id}: approximate coordinates")
    return StationCoordinates(
        station, station.xyz, (station.lat, station.lon, station.h), FRAME, epoch, "approximate", None,
        "approximate position from the site log (M3G, IGS network table): good to metres only")


# --- the observations and the navigation of a station, for the hours of a flight ---------------------------------

# The archives. BKG's data centre keeps the stations of the EPN (EUREF) and of the IGS (IGS); RING keeps its own, on a
# port that some firewalls close. Both are read with no account.
BKG_ROOT = "https://igs.bkg.bund.de/root_ftp/"
RING_RINEX = "https://webring.gm.ingv.it:44324/rinex/RING/"
RING_PORT = 44324

MARGIN = timedelta(minutes=10)  # the base is wanted this long before the flight and after it
LATE = timedelta(hours=3)  # a file that is not there is "not published yet" until this long after it was due
NAV_SETTLE = timedelta(hours=1)  # the navigation file of a day is final this long after the end of the day
_BKG_DAILY = "BKG daily files are online about 30 minutes after midnight UTC"

# The names of the files of observations (RINEX 3, "long" names): station, source (R receiver, S stream, U unknown),
# the start of the file, its length, the interval of its epochs, MO (observations), Hatanaka inside gzip. Only ASCII
# letters and digits (\d takes other digits too), and a name is taken only whole (``fullmatch``).
_OBS_NAME = re.compile(r"(?P<id>[A-Z0-9]{9})_(?P<source>[RSU])_(?P<year>[0-9]{4})(?P<day>[0-9]{3})(?P<hour>[0-9]{2})"
                       r"(?P<minute>[0-9]{2})_(?P<period>[0-9]{2}[SMHD])_(?P<rate>[0-9]{2}[SMHD])_MO\.crx\.gz")
_NAV_PRODUCT = "BRDC00WRD"  # the broadcast messages of all constellations, merged for the world
_NAV_NAME = re.compile(rf"{_NAV_PRODUCT}_[RSU]_[0-9]{{11}}_01D_MN\.rnx(?:\.gz)?")
_UNITS = {"S": 1, "M": 60, "H": 3600, "D": 86400}
_RATES = {"1s": 1.0, "30s": 30.0}
_RATE_TEXT = {1.0: "1 s", 30.0: "30 s"}
_SHOWN = 4  # how many of the missing files an error message lists for a rate


def _seconds(code: str) -> int:
    """The seconds of a length or an interval as the names write it: ``"15M"`` is 900."""
    return int(code[:-1]) * _UNITS[code[-1]]


def _doy(when: datetime | date) -> str:
    return f"{when.timetuple().tm_yday:03d}"


def _slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def _ring_hint() -> str:
    return f"RING is on port {RING_PORT}; a firewall may block it"


@dataclass(frozen=True)
class RemoteFile:
    """One file of a station in an archive: its address and its name, the time it holds by its name (``start`` to
    ``end``, GPS time without a zone; a file of a day begins at midnight and ends at the next one) and the seconds
    between its epochs."""

    url: str
    name: str
    start: datetime
    end: datetime
    interval: float


@dataclass(frozen=True)
class _Kind:
    """A kind of file that an archive keeps: how long a file is and the interval of its epochs (as the names write
    them, ``01D`` and ``30S``), the address of the folder of the files that begin at a time, and when they come
    online."""

    label: str
    period_code: str
    rate_code: str
    folder: Callable[[datetime], str]
    when: str

    @property
    def period(self) -> timedelta:
        return timedelta(seconds=_seconds(self.period_code))

    @property
    def interval(self) -> float:
        return float(_seconds(self.rate_code))


@dataclass(frozen=True)
class _Archive:
    """A place that keeps observations: its name, the site it is reached at (``"BKG"``, ``"RING"``: if one archive of a
    site cannot be reached the others cannot either), its kinds of file in the order they are tried, and what to add to
    the complaint that it cannot be reached."""

    label: str
    site: str
    kinds: tuple[_Kind, ...]
    hint: str = ""


def _bkg(tree: str) -> _Archive:
    """The tree ``tree`` (``"EUREF"`` or ``"IGS"``) of BKG's data centre: files of a day in ``obs/<yyyy>/<ddd>/``, of
    an hour in ``nrt/<ddd>/<hh>/`` and, for some stations, pieces of 15 minutes at 1 s in
    ``highrate/<yyyy>/<ddd>/<a-x>/`` (a folder for each hour, named by a letter; the IGS keeps older ones in
    ``highrate_permanent``)."""
    root = _slash(BKG_ROOT) + tree + "/"

    def fine(folder: str) -> _Kind:
        return _Kind("15 minute 1 s", "15M", "01S", lambda t: f"{root}{folder}/{t:%Y}/{_doy(t)}/{chr(97 + t.hour)}/",
                     "BKG 15 minute files are online after each quarter of an hour")

    kinds = [_Kind("daily 30 s", "01D", "30S", lambda t: f"{root}obs/{t:%Y}/{_doy(t)}/", _BKG_DAILY),
             _Kind("hourly 30 s", "01H", "30S", lambda t: f"{root}nrt/{_doy(t)}/{t:%H}/",
                   "BKG hourly files are online after the hour"),
             fine("highrate")]
    if tree == "IGS":
        kinds.append(fine("highrate_permanent"))
    return _Archive(f"BKG {tree}", "BKG", tuple(kinds))


def _ring() -> _Archive:
    """RING: files of a day at 30 s in ``<yyyy>/<ddd>/`` and of an hour at 1 s in ``<yyyy>/<ddd>/<hh>/``."""
    root = _slash(RING_RINEX)
    return _Archive("RING", "RING", (
        _Kind("daily 30 s", "01D", "30S", lambda t: f"{root}{t:%Y}/{_doy(t)}/",
              "RING daily files are online after midnight UTC"),
        _Kind("hourly 1 s", "01H", "01S", lambda t: f"{root}{t:%Y}/{_doy(t)}/{t:%H}/",
              "RING hourly files are online after the hour")), _ring_hint())


def _archives(station: Station) -> list[_Archive]:
    """The archives that keep the station, in the order they are tried: BKG's before RING's."""
    found = []
    if "EPN" in station.networks:
        found.append(_bkg("EUREF"))
    if "IGS" in station.networks:
        found.append(_bkg("IGS"))
    if "RING" in station.networks:
        found.append(_ring())
    return found


def _checked(station: Station) -> None:
    """The id of the station ends up in names of files and folders: a ``ValueError`` unless it is nine capital letters
    or digits."""
    if not _ID.fullmatch(station.id):
        raise ValueError(f"{station.id!r} is not the id of a station (nine capital letters or digits, as AQUI00ITA)")


def _gps_span(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """The flight as times in GPS time without a zone (what :func:`rinex.header` gives), the start not after the end."""
    for when in (start, end):
        if not isinstance(when, datetime) or when.tzinfo is not None:
            raise ValueError("the start and the end of the flight are datetimes in GPS time without a time zone, as "
                             "rinex.header gives them")
    if end < start:
        raise ValueError(f"the end of the flight ({end}) is before its start ({start})")
    return start, end


def _gps_now() -> datetime:
    """Now in GPS time without a zone (the clock of the program, :func:`_now`, is UTC)."""
    utc = _now().astimezone(timezone.utc).replace(tzinfo=None)
    return utc + timedelta(seconds=rinex.gps_minus_utc(utc))


def _slots(period: timedelta, a: datetime, b: datetime) -> Iterator[datetime]:
    """The starts of the files of length ``period`` (counted from midnight) that overlap the time from ``a`` to
    ``b``."""
    midnight = a.replace(hour=0, minute=0, second=0, microsecond=0)
    slot = midnight + (a - midnight) // period * period
    while slot < b:
        yield slot
        slot += period


class _Unreachable(Exception):
    """A site that cannot be asked: ``site`` is its name, the message says why."""

    def __init__(self, site: str, message: str):
        super().__init__(message)
        self.site = site


def _name_start(match: re.Match) -> datetime | None:
    """The time a file begins, by its name; None for a date that does not exist."""
    try:
        return datetime(int(match["year"]), 1, 1) + timedelta(days=int(match["day"]) - 1, hours=int(match["hour"]),
                                                              minutes=int(match["minute"]))
    except (ValueError, OverflowError):
        return None


def _find(station: Station, archive: _Archive, kind: _Kind, slot: datetime) -> RemoteFile | None:
    """The file of ``station`` of this ``kind`` that begins at ``slot``, as the listing of the folder of the archive
    has it, or None. Of the names that are the station's, for this time, length and interval, R is taken before S
    before U."""
    folder = kind.folder(slot)
    try:
        names = download.listing(folder)
    except download.NotPublished:
        return None  # a folder that is not there has no file
    except download.DownloadError as exc:
        hint = f" ({archive.hint})" if archive.hint else ""
        raise _Unreachable(archive.site, f"{archive.site} could not be reached: {exc}{hint}") from exc
    best = None
    for name in names:
        match = _OBS_NAME.fullmatch(name)
        if (match is None or match["id"] != station.id or _name_start(match) != slot
                or _seconds(match["period"]) != _seconds(kind.period_code)
                or _seconds(match["rate"]) != _seconds(kind.rate_code)):
            continue
        key = ("RSU".index(match["source"]), name)
        if best is None or key < best:
            best = key
    if best is None:
        return None
    return RemoteFile(folder + best[1], best[1], slot, slot + kind.period, kind.interval)


def _missing(station: Station, archive: _Archive, kind: _Kind, slot: datetime) -> str:
    """Why there is no file: which one it would be and where it is not and, if it is not long since it was due, when it
    usually comes online. The name has its source as ``[RSU]``: it says what is missing, no file is asked for by it."""
    name = f"{station.id}_[RSU]_{slot:%Y}{_doy(slot)}{slot:%H%M}_{kind.period_code}_{kind.rate_code}_MO.crx.gz"
    text = f"{archive.label}, {kind.label} files: {name} is not in {kind.folder(slot)}"
    if _gps_now() < slot + kind.period + LATE:
        text += f" - not published yet: {kind.when}"
    return text


def _cover(station: Station, archive: _Archive, interval: float, start: datetime, end: datetime,
           whole: bool) -> tuple[list[RemoteFile], bool, list[str]]:
    """The files of one archive, with epochs ``interval`` seconds apart, for the flight and :data:`MARGIN` on each side
    of it; whether they hold it (all of it when ``whole``, else the flight, and the margins as far as there are files);
    why not. The kinds of file are tried in the order of the archive, each for the time that the ones before did not
    cover: the file of a day, and for a day that has none the files of its hours. Raises :class:`_Unreachable`."""
    found: dict[str, RemoteFile] = {}
    windows = [(start - MARGIN, end + MARGIN)]
    misses: list[tuple[datetime, datetime, str]] = []
    for kind in (k for k in archive.kinds if k.interval == interval):
        left = []
        for a, b in windows:
            for slot in _slots(kind.period, a, b):
                remote = _find(station, archive, kind, slot)
                if remote is not None:
                    found[remote.name] = remote
                else:
                    window = (max(a, slot), min(b, slot + kind.period))  # what this file would have covered
                    left.append(window)
                    misses.append((*window, _missing(station, archive, kind, slot)))
        windows = left
    needed = windows if whole else [(a, b) for a, b in windows if a <= end and b > start]
    notes = [note for a, b, note in misses if any(a < d and c < b for c, d in needed)]
    return sorted(found.values(), key=lambda f: f.start), not needed, notes


def _choose(station: Station, interval: float, start: datetime, end: datetime, whole: bool,
            down: dict[str, str]) -> tuple[list[RemoteFile] | None, list[str]]:
    """The files of the first archive that holds the flight (:func:`_cover`), or None and what the archives lack. A site
    that cannot be reached is put in ``down`` with the reason and is not asked again."""
    notes: list[str] = []
    for archive in _archives(station):
        if archive.site in down:
            continue
        try:
            files, complete, why = _cover(station, archive, interval, start, end, whole)
        except _Unreachable as exc:
            down[exc.site] = str(exc)
            continue
        if complete:
            return files, notes
        notes += why
    return None, notes


def _no_files(station: Station, start: datetime, end: datetime, notes: dict[float, list[str]], down: dict[str, str],
              other: tuple[float, bool] | None) -> str:
    """The message when no archive has the files: the time, the sites that did not answer, for each rate the first of
    the files that are missing and, when a rate was asked for, whether the other rate is there."""
    until = f"{end:%H:%M}" if start.date() == end.date() else f"{end:%Y-%m-%d %H:%M}"
    lines = [f"{station.id}: no observation files can be had for {start:%Y-%m-%d %H:%M} to {until} (GPS time)."]
    lines += down.values()
    for interval, why in notes.items():
        if not why:
            continue  # every archive that could be asked was out of reach: the sites above say so
        lines.append(f"{_RATE_TEXT[interval]} files:")
        lines += [f"  {note}" for note in why[:_SHOWN]]
        if len(why) > _SHOWN:
            lines.append(f"  and {len(why) - _SHOWN} more")
    if other is not None:
        interval, there = other
        if there:
            lines.append(f"The {_RATE_TEXT[interval]} files for these hours are online: ask for the rate "
                         f"\"{interval:g}s\" (or \"auto\").")
        else:
            lines.append(f"The {_RATE_TEXT[interval]} files for these hours are not there either.")
    return "\n".join(lines)


def observation_files(station: Station, start: datetime, end: datetime, rate: str = "auto") -> list[RemoteFile]:
    """The files that hold the observations of ``station`` for a flight from ``start`` to ``end`` (GPS time without a
    zone) and :data:`MARGIN` on each side of it, in time order. The station is looked for in the archive of each of its
    networks, BKG's before RING's, and the files are all of one archive. The names are those of the listings of the
    folders (``download.listing``): none is made up.

    ``rate`` is ``"1s"``, ``"30s"`` or ``"auto"``. 1 s files are BKG's pieces of 15 minutes (``highrate``) or RING's of
    an hour; 30 s files are the file of a day where the archive has one, else the files of its hours (BKG's ``nrt``).
    With ``"auto"``, 1 s when its files hold the flight and both margins, else 30 s, and when neither is whole the 1 s
    files that hold the flight. With a rate asked for, the files that hold the flight, and the margins as far as the
    archive has files for them.

    A ``ValueError`` when there are none: it names the files that are missing and where they are not, says when such
    files usually come online if the time is recent, whether the other rate is there, and that BKG or RING could not be
    reached (RING is on the port 44324, which some firewalls close)."""
    _checked(station)
    start, end = _gps_span(start, end)
    rate = str(rate).strip().lower()
    if rate != "auto" and rate not in _RATES:
        raise ValueError(f"the rate is \"1s\", \"30s\" or \"auto\", not {rate!r}")
    if not _archives(station):
        raise ValueError(f"{station.id} is in none of the networks ({', '.join(NETWORKS)}): there is no archive that "
                         "this program reads to ask for its observations")
    down: dict[str, str] = {}
    # (interval, whole): 1 s if it holds the margins too, else 30 s, and last the 1 s that holds the flight
    plan = [(1.0, True), (30.0, False), (1.0, False)] if rate == "auto" else [(_RATES[rate], False)]
    notes: dict[float, list[str]] = {}
    for interval, whole in plan:
        files, why = _choose(station, interval, start, end, whole, down)
        if files is not None:
            log.info("%s: %d files with an epoch every %s", station.id, len(files), _RATE_TEXT[interval])
            return files
        if not whole:
            notes[interval] = why
    other = None
    if rate != "auto" and any(archive.site not in down for archive in _archives(station)):  # else none was looked at
        spare = 30.0 if _RATES[rate] == 1.0 else 1.0
        other = (spare, _choose(station, spare, start, end, False, down)[0] is not None)
    raise ValueError(_no_files(station, start, end, notes, down, other))


def _inside(folder: Path, name: str, pattern: re.Pattern, owner: str) -> Path:
    """``folder / name`` for a ``name`` that is wholly the name of a file of the archive (``pattern``) and that, once
    resolved, is directly in ``folder``. A name from a listing is data from the network: it must not write anywhere else
    (``..``, a drive, an absolute path, a new line). Otherwise a ``ValueError``."""
    if pattern.fullmatch(name) is None or (folder / name).resolve().parent != folder.resolve():
        raise ValueError(f"{name!r} is not the name of a file of the archive, or would not stay in the folder of "
                         f"{owner}")
    return folder / name


def _fetch(url: str, path: Path, progress) -> None:
    """``download.fetch``, its failures given as the ``ValueError`` that this module raises for files it cannot have."""
    try:
        download.fetch(url, path, progress)
    except download.NotPublished as exc:
        raise ValueError(f"{path.name} was in the listing of the archive and is not there now: {exc}") from exc
    except download.DownloadError as exc:
        hint = f" ({_ring_hint()})" if url.startswith(_slash(RING_RINEX)) else ""
        raise ValueError(f"{path.name} could not be downloaded: {exc}{hint}") from exc


def _downloaded(url: str, name: str, path: Path, decode: Callable[[Path], object], progress) -> None:
    """``path`` holds the file at ``url`` and ``decode(path)`` works on it: the copy that is there, else fetched. A
    file that does not decode (its integrity is that of the checksum of gzip and of the decoder) is removed and fetched
    again, once; a second failure is a ``ValueError`` naming the file. A missing decoder is no damage: that error is
    raised at once, the file is not fetched again."""
    problem: ValueError | None = None
    for _attempt in range(2):
        progress.check()
        if not path.exists():
            _fetch(url, path, progress)
        try:
            decode(path)
            return
        except ValueError as exc:
            if _native.CRINEX_MISSING in str(exc):
                raise
            problem = exc
            path.unlink(missing_ok=True)
    raise ValueError(f"{name} ({url}) does not decode, not even when downloaded again: {problem}") from problem


def _uncovered(station: Station, info: rinex.RinexInfo, gaps: list[tuple[datetime, datetime]], start: datetime,
               end: datetime) -> str | None:
    """Why a base does not cover the flight, or None: a hole between two of its epochs during the flight, or a first
    epoch after the start or a last one before the end. Holes in the margins do not matter."""
    flight = f"{start:%Y-%m-%d %H:%M:%S} to {end:%H:%M:%S}" if start.date() == end.date() else f"{start} to {end}"
    inside = [(a, b) for a, b in gaps if a < end and b > start]
    if inside:
        a, b = inside[0]
        more = f" (and {len(inside) - 1} more)" if len(inside) > 1 else ""
        return (f"{station.id}: the observations have no epoch from {a:%Y-%m-%d %H:%M:%S} to {b:%H:%M:%S} (GPS time) "
                f"during the flight, {flight}{more}: use the other rate, or another station")
    if info.first is None or info.last is None or info.first > start or info.last < end:
        return (f"{station.id}: the observations run from {info.first} to {info.last} (GPS time) and do not cover the "
                f"flight, {flight}")
    return None


def download_observations(station: Station, start: datetime, end: datetime, folder: str | Path, progress=None,
                          rate: str = "auto") -> Path:
    """The observations of ``station`` for the flight from ``start`` to ``end`` (GPS time without a zone) as one RINEX
    file, ``<folder>/<id>_<YYYYmmdd-HHMM>_<HHMM>.obs`` (the start of the flight, then the time of its end). The files
    that :func:`observation_files` finds (``rate`` as there) are fetched one at a time into ``stations/<id>`` of the
    GNSS folder, where they stay as the archive names them and a second call finds them; they are decoded (Hatanaka,
    gzip) and joined (:func:`rinex.join`).

    A file that does not decode is fetched again once, then refused naming it. The base has to cover the flight: a hole
    in its epochs during the flight, or epochs that begin after its start or end before its end, are a ``ValueError``
    and no file is left; holes in the margins are not. A file that is not there, a site that cannot be reached (the
    message of RING names its port, 44324) and files that do not join are a ``ValueError`` too."""
    progress = as_progress(progress)
    _checked(station)
    files = observation_files(station, start, end, rate)
    start, end = _gps_span(start, end)
    cache = _stations_dir() / station.id
    owner = f"the station {station.id}"
    paths = [_inside(cache, remote.name, _OBS_NAME, owner) for remote in files]  # all names, before any fetch
    for n, (remote, path) in enumerate(zip(files, paths)):
        _downloaded(remote.url, remote.name, path, rinex.plain, progress.sub(0.9 * n / len(files), 0.9 / len(files)))
    out = Path(folder) / f"{station.id}_{start:%Y%m%d-%H%M}_{end:%H%M}.obs"
    progress(0.9, f"Joining the {len(paths)} files of {station.id}")
    try:
        info, gaps = rinex.join(paths, out)
    except ValueError as exc:
        raise ValueError(f"{station.id}: the files cannot be joined: {exc}") from exc
    problem = _uncovered(station, info, gaps, start, end)
    if problem:
        out.unlink(missing_ok=True)
        raise ValueError(problem)
    progress(1.0, f"{station.id}: observations from {info.first:%Y-%m-%d %H:%M:%S} to {info.last:%H:%M:%S} (GPS time)")
    return out


def _navigation_name(day: date) -> tuple[str, str]:
    """The name of the navigation file of a day and its address, from the listing of BKG's folder
    ``IGS/BRDC/<yyyy>/<ddd>/``: of ``BRDC00WRD`` the S copy, else R, else U. A ``ValueError`` when it is not there,
    which says when it comes if the day is recent."""
    where = f"{_slash(BKG_ROOT)}IGS/BRDC/{day:%Y}/{_doy(day)}/"
    stamp = f"{day:%Y}{_doy(day)}0000"
    pattern = re.compile(rf"{_NAV_PRODUCT}_(?P<source>[RSU])_{stamp}_01D_MN\.rnx\.gz")
    try:
        names = download.listing(where)
    except download.NotPublished:
        names = []
    except download.DownloadError as exc:
        raise ValueError(f"BKG could not be reached: {exc}") from exc
    found = sorted((m for m in map(pattern.fullmatch, names) if m), key=lambda m: ("SRU".index(m["source"]), m.string))
    if found:
        return found[0].string, where + found[0].string
    name = f"{_NAV_PRODUCT}_[RSU]_{stamp}_01D_MN.rnx.gz"
    text = f"No broadcast navigation file for {day:%Y-%m-%d}: {name} is not in {where}"
    if _gps_now() < datetime(day.year, day.month, day.day) + timedelta(days=1) + LATE:
        text += f" - not published yet: {_BKG_DAILY}"
    raise ValueError(text)


def _navigation_file(day: date, name: str, url: str, folder: Path, progress) -> Path:
    """The navigation file of ``day`` decompressed into ``folder``. When the day is over it is kept (``navigation`` in
    the GNSS folder) and used again. BKG writes the file of the day all day long, so until an hour after the end of the
    day it is fetched every time and not kept: a copy of the middle of a day is never taken for the whole of it."""
    kept = download.gnss_dir() / "navigation"
    final = datetime(day.year, day.month, day.day) + timedelta(days=1) + NAV_SETTLE <= _gps_now()
    kept.mkdir(parents=True, exist_ok=True)
    plain = _inside(folder, name[:-3], _NAV_NAME, "the navigation files")
    with tempfile.TemporaryDirectory(prefix=".nav-", dir=kept) as scratch:
        packed = _inside(kept if final else Path(scratch), name, _NAV_NAME, "the navigation files")
        _downloaded(url, name, packed, lambda path: rinex.gunzip(path, plain, name), progress)
    return plain


def download_navigation(start: datetime, end: datetime, folder: str | Path, progress=None) -> list[Path]:
    """The broadcast navigation messages for the days of a flight from ``start`` to ``end`` (GPS time without a zone):
    of each day the file ``BRDC00WRD_<S|R|U>_<yyyy><ddd>0000_01D_MN.rnx.gz`` of BKG's ``IGS/BRDC/<yyyy>/<ddd>/``,
    decompressed into ``folder`` without its ``.gz``, in the order of the days (a flight over midnight takes two). All
    the days are looked up in the archive before any is fetched; a day that is not there is a ``ValueError`` that names
    the file and says when it comes if the day is recent. A file that does not decode is fetched again once."""
    progress = as_progress(progress)
    start, end = _gps_span(start, end)
    folder = Path(folder)
    days = [start.date() + timedelta(days=n) for n in range((end.date() - start.date()).days + 1)]
    chosen = [_navigation_name(day) for day in days]
    folder.mkdir(parents=True, exist_ok=True)
    return [_navigation_file(day, name, url, folder, progress.sub(n / len(days), 1 / len(days)))
            for n, (day, (name, url)) in enumerate(zip(days, chosen))]
