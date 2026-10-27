"""Post-processed kinematic (PPK) positions with RTKLIB: the options of a run, the run on copies of the RINEX files of
a rover and a base, the solution RTKLIB writes (a .pos file, also one made with RTKLIB's own programs or with Emlid
Studio) and where it puts the antenna at the time of a shot. Times are seconds of GPS time since the GPS epoch, as the
shots of a Timestamp.MRK.

A position the program keeps - the base of a solution, the reference of a photo - is ``[lon, lat, h]``, longitude
first; a position typed by the user, and the options of RTKLIB, are latitude, longitude, height."""
from __future__ import annotations

import itertools
import math
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from .. import _native
from . import rinex
from .dji import GPS_EPOCH, wrap_longitude

QUALITY = {1: "fix", 2: "float", 3: "sbas", 4: "dgps", 5: "single", 6: "ppp"}  # the Q of a row of RTKLIB's solution
# the status of a position by RTKLIB's Q, as for the RTK flag of a DJI photo - the one table of it, also for the Q
# column of a positions file: dgps counts as float; any other Q (sbas, ppp, none) as single
STATUS = {1: "fix", 2: "float", 4: "float", 5: "single"}
RANK = {"fix": 0, "float": 1, "single": 2}  # the statuses from the best to the worst

MAX_GAP_S = 1.0  # a time between two epochs further apart than this has no position: none is guessed across a gap
# a typed position of a base this far from the position of its RINEX header (horizontally, in height, metres) is
# likely a mistake: latitude and longitude swapped or without their sign, a height above the sea level
FAR_FROM_HEADER_M = (1000.0, 30.0)
TIME_RESOLUTION_S = 1e-3  # the times of a solution are written to the millisecond
_SAME_EPOCH_S = 1e-6  # a time this close to an epoch is the epoch: times of 8e8 s are floats to 1.2e-7 s

MODES = ("kinematic", "static", "static-start", "dgps")  # RTKLIB's positioning modes that work with a base
# the bits of RTKLIB's pos1-navsys, by the letter of the constellation in satellite ids
SYSTEM_BITS = {"G": 1, "S": 2, "R": 4, "E": 8, "J": 16, "C": 32, "I": 64}

WEEK = 604800.0  # seconds
_GPS_EPOCH = GPS_EPOCH.replace(tzinfo=None)
_TIME_SCALES = {"GPST": None, "UTC": 0.0, "JST": 9 * 3600.0}  # the times of a .pos: seconds ahead of UTC, or GPS
# the time systems of a RINEX file that rinex.header converts to GPS time; it reads any other one as GPS time
_RINEX_TIME_SYSTEMS = ("GPS", "GAL", "QZS", "IRN", "BDT", "GLO")

# the files of a run in the work folder: RTKLIB never sees the names of the user's files (it expands "*" and "%r" in
# them, and decompresses some extensions with a shell command). Every run writes its options and its solution under a
# name of its own, solution-<YYYYmmdd-HHMMSS>.conf and .pos (and RTKLIB the time marks of the rover in
# <name>_events.pos): the solutions of earlier runs stay, and the photos name the one of their run
SOLUTION_PREFIX = "solution"
EVENTS_SUFFIX = "_events.pos"
INPUTS_FOLDER = "rtklib_inputs"  # in the work folder: only the copies of a run, emptied when a run starts
# RTKLIB copies the paths of its files into buffers of 1024 bytes: a path this long or longer is refused before it runs
MAX_PATH_BYTES = 1000

BASELINE_WARNING_KM = 20.0  # a base further than this from the flight: the ambiguities are hard to fix
SAME_BASE_M = 0.05  # one base typed at two positions further apart than this in one chunk: one of them is likely wrong
MEMORY = "gnss_ppk"  # chunk.meta[MEMORY]["last"]: the last PPK run of the chunk (last_run), where the dialog starts

_FIELDS = re.compile(r"[,\s]+")  # RTKLIB separates the fields with spaces, or with out-fieldsep (a comma)
_LEGEND = re.compile(r"lat/lon/height=([^/,)]*)/([^,)]*)")
_COLUMNS = ("latitude(deg)", "longitude(deg)", "height(m)", "Q", "ns", "sdn(m)", "sde(m)", "sdu(m)")


@dataclass
class Solution:
    """The positions RTKLIB computed for the antenna of the rover, one row per epoch in time order."""

    times: np.ndarray  # (N,) float64: seconds of GPS time since the GPS epoch (GPS week x 604800 + seconds of week)
    llh: np.ndarray  # (N, 3): latitude, longitude (degrees), height above the WGS84 ellipsoid (m)
    q: np.ndarray  # (N,) int: RTKLIB's quality, 1 fix, 2 float, 4 dgps, 5 single (``QUALITY``)
    ns: np.ndarray  # (N,) int: the satellites used
    sd: np.ndarray  # (N, 3): standard deviations east, north, up (m)
    # name; position [lon, lat, h] of the mark the base stands on (longitude first, as every position the program
    # keeps: the reference of a photo, its antenna); position_source "header" | "typed"; antenna: its type and radome
    # in the RINEX header of the base ("" when it names none); antenna_delta_hen [h, e, n] (m): the antenna above the
    # mark from that header, which RTKLIB adds (None when the header has none); file. Empty from read_pos
    base: dict
    path: Path  # the .pos file
    warnings: list[str] = field(default_factory=list)
    rover: str = ""  # the name of the rover's observation file; empty from read_pos
    # the files of the run, absolute: "rover" and "base" (paths; "base" a list of paths when the base is in several
    # files), "navs" (a list of paths); empty from read_pos
    inputs: dict = field(default_factory=dict)


# --- the options of a run ---------------------------------------------------------------------------------------


def _base_position(value: Sequence[float]) -> tuple[float, float, float]:
    """The position of a base as three numbers: latitude in [-90, 90], longitude in [-180, 360) degrees, a finite
    height; a ``ValueError`` saying what is wrong otherwise."""
    try:
        lat, lon, h = (float(v) for v in value)
    except (TypeError, ValueError):
        raise ValueError("the position of the base is three numbers: latitude, longitude (degrees) and height above "
                         "the ellipsoid (m)") from None
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"the latitude of the base, {lat}, is not between -90 and 90 degrees")
    if not -180.0 <= lon < 360.0:
        raise ValueError(f"the longitude of the base, {lon}, is not between -180 and 360 degrees")
    if not math.isfinite(h):
        raise ValueError(f"the height of the base, {h}, is not a number")
    return lat, lon, h


def _navsys(systems: str | Iterable[str]) -> int:
    """RTKLIB's pos1-navsys of ``systems``: "all", or the letters of the constellations (G R E C J I S) in a string
    or a list, in any case."""
    if isinstance(systems, str) and systems.strip().lower() == "all":
        return sum(SYSTEM_BITS.values())
    letters = re.sub(r"[\s,+]", "", systems if isinstance(systems, str) else "".join(systems)).upper()
    bits = 0
    for letter in letters:
        if letter not in SYSTEM_BITS:
            raise ValueError(f"no satellite system has the letter {letter!r}: give \"all\" or letters of G R E C J I S")
        bits |= SYSTEM_BITS[letter]
    if not bits:
        raise ValueError("no satellite system given: give \"all\" or letters of G R E C J I S")
    return bits


def config(base_position: Sequence[float] | None, mode: str = "kinematic", elevation_mask: float = 15.0,
           systems: str | Iterable[str] = "all", interpolate_base: bool = False) -> str:
    """The options file of RTKLIB for a PPK run (the text of rnx2rtkp's -k file). The rover moving (``mode``
    "kinematic") or still ("static", "static-start"; "dgps" for code only); the forward and backward solutions
    combined, the ambiguities fixed and held; the satellites ``elevation_mask`` degrees above the horizon or more, of
    the constellations ``systems`` ("all" or their letters G R E C J I S), on two frequencies. The base at
    ``base_position`` (latitude, longitude in degrees, height above the ellipsoid in metres: the mark it stands on),
    to which RTKLIB adds the antenna height of the base's RINEX header (ANTENNA: DELTA H/E/N), as RINEX means it.
    With None the base is at the position of its RINEX header (rinexhead): RTKLIB-EX adds the antenna height once
    then too, RTKLIB 2.4.3 twice, which is why ``solve`` always writes the position. The solution in latitude,
    longitude and ellipsoidal height, timed in GPS weeks and seconds of GPS time. With ``interpolate_base`` RTKLIB
    interpolates the observations of the base to the epochs of the rover (``misc-timeinterp``), for a base whose epochs
    are further apart than the rover's. Only option names of RTKLIB's table, no file of any kind."""
    if mode not in MODES:
        raise ValueError(f"positioning mode {mode!r}: it is one of {', '.join(MODES)}")
    mask = float(elevation_mask)
    if not 0.0 <= mask < 90.0:
        raise ValueError(f"the elevation mask, {elevation_mask}, is not between 0 and 90 degrees")
    options = [("pos1-posmode", mode), ("pos1-frequency", "l1+l2"), ("pos1-soltype", "combined"),
               ("pos1-elmask", f"{mask:g}"), ("pos1-navsys", str(_navsys(systems))), ("pos2-armode", "fix-and-hold")]
    if interpolate_base:
        options.append(("misc-timeinterp", "on"))
    if base_position is None:
        options.append(("ant2-postype", "rinexhead"))
    else:
        lat, lon, h = _base_position(base_position)
        options += [("ant2-postype", "llh"), ("ant2-pos1", repr(lat)), ("ant2-pos2", repr(lon)), ("ant2-pos3", repr(h))]
    options += [("ant2-anttype", "*"),  # "*": the antenna of the RINEX header, and its height above the mark
                ("out-solformat", "llh"), ("out-outhead", "on"), ("out-timesys", "gpst"), ("out-timeform", "tow"),
                ("out-height", "ellipsoidal")]
    return "# RTKLIB options of a PPK run (pyarchinit-photogrammetry)\n" + "".join(
        f"{name:<18}={value}\n" for name, value in options)


# --- the solution -----------------------------------------------------------------------------------------------


class _NoPositions(ValueError):
    """A solution file without positions: RTKLIB computed nothing."""


def _file_seconds(day: str, clock: str) -> float:
    """The seconds since 1980-01-06 00:00:00 of a time written as GPS week and seconds of the week (``1316
    518400.000``) or as the calendar (``2005/04/02 00:00:00.000``), in the time scale of the file."""
    if "/" in day:
        year, month, mday = (int(v) for v in day.split("/"))
        hours, minutes, seconds = clock.split(":")
        days = (date(year, month, mday) - _GPS_EPOCH.date()).days
        return days * 86400.0 + int(hours) * 3600.0 + int(minutes) * 60.0 + float(seconds)
    return int(day) * WEEK + float(clock)


def _gps_seconds(seconds: float, scale: str) -> float:
    """Seconds of GPS time of a time of the file in ``scale`` (GPST, UTC or JST)."""
    ahead = _TIME_SCALES[scale]
    if ahead is None:
        return seconds
    utc = seconds - ahead
    return utc + rinex.gps_minus_utc(_GPS_EPOCH + timedelta(seconds=utc))


def read_pos(path: str | Path) -> Solution:
    """The solution of an RTKLIB .pos file in latitude, longitude and ellipsoidal height on WGS84, also one made with
    RTKLIB's programs or Emlid Studio: the times (GPS week and seconds, or the calendar; GPS time, UTC or JST) in
    seconds of GPS time since the GPS epoch, the rows in time order, the standard deviations east, north, up. The
    columns are found by their names in the header. A file without positions, in x/y/z or e/n/u, in degrees,
    minutes and seconds, on another datum or with heights above the geoid is a ``ValueError`` saying so, a row with a
    number that is not finite (nan, inf) one naming its line; a file that does not say what its heights are is read
    as RTKLIB's default (above the ellipsoid), with a warning."""
    path = Path(path)
    legend = columns = None
    rows: list[tuple[int, str]] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for number, line in enumerate(f, 1):
            text = line.strip()
            if not text:
                continue
            if text.startswith("%"):
                body = text[1:].strip()
                if body.startswith("("):  # "% (lat/lon/height=WGS84/ellipsoidal,Q=1:fix,...)"
                    legend = body
                else:
                    fields = _FIELDS.split(body)
                    if "Q" in fields and "ns" in fields:  # "%  GPST  latitude(deg) longitude(deg) ... Q  ns ..."
                        columns = fields
                continue
            rows.append((number, text))
    if not rows:
        raise _NoPositions(f"{path.name}: nothing was computed: the solution has no positions")
    if columns is None:
        raise ValueError(f"{path.name}: not a solution file of RTKLIB: no line names its columns")
    scale, names = columns[0], columns[1:]
    if scale not in _TIME_SCALES:
        raise ValueError(f"{path.name}: its times are in {scale}, which is not GPS time (GPST), UTC or JST")
    if any(name.startswith("latitude(d'") for name in names):
        raise ValueError(f"{path.name}: latitude and longitude in degrees, minutes and seconds: write them in decimal "
                         "degrees (out-degform=deg)")
    missing = [name for name in _COLUMNS if name not in names]
    if missing:
        raise ValueError(f"{path.name}: not a solution in latitude, longitude and height (out-solformat=llh): no "
                         f"column {', '.join(missing)}")
    warnings = []
    match = _LEGEND.search(legend) if legend else None
    if match is None:
        warnings.append(f"{path.name} does not say what its heights are: they are read as ellipsoidal heights, "
                        "RTKLIB's default")
    else:
        datum, height = match.group(1).strip(), match.group(2).strip()
        if datum != "WGS84":
            raise ValueError(f"{path.name}: positions on the {datum} datum: PPK positions are on WGS84")
        if height != "ellipsoidal":
            raise ValueError(f"{path.name}: {height} heights, above the geoid: the heights must be above the ellipsoid "
                             "(out-height=ellipsoidal)")
    where = [names.index(name) + 2 for name in _COLUMNS]  # the time takes the first two fields of a row
    times, values = [], []
    for number, text in rows:
        fields = _FIELDS.split(text)
        try:
            if len(fields) <= max(where):
                raise ValueError
            seconds = _file_seconds(fields[0], fields[1])
            row = [float(fields[i]) for i in where]
        except ValueError:
            raise ValueError(f"{path.name}, line {number}: not a row of a solution: {text[:80]}") from None
        if not (math.isfinite(seconds) and all(math.isfinite(v) for v in row)):  # C writes nan, -nan, inf
            raise ValueError(f"{path.name}, line {number}: a number that is not finite in a row of the solution: "
                             f"{text[:80]}")
        times.append(_gps_seconds(seconds, scale))
        values.append(row)
    order = np.argsort(np.array(times), kind="stable")
    table = np.array(values, dtype=np.float64)[order]
    q, ns = table[:, 3], table[:, 4]
    if not (np.array_equal(q, np.round(q)) and np.array_equal(ns, np.round(ns))):
        raise ValueError(f"{path.name}: the quality Q and the number of satellites ns are not whole numbers")
    return Solution(times=np.array(times, dtype=np.float64)[order], llh=table[:, 0:3].copy(), q=q.astype(int),
                    ns=ns.astype(int), sd=table[:, [6, 5, 7]].copy(),  # RTKLIB writes sdn, sde, sdu
                    base={}, path=path, warnings=warnings)


# --- the antenna at the time of a shot --------------------------------------------------------------------------


def status(q: int) -> str:
    """The status of a position of quality ``q`` (RTKLIB's Q): "fix", "float" (also dgps) or "single" (also sbas, ppp
    and any other)."""
    return STATUS.get(int(q), "single")


def _worse(qa: np.ndarray, qb: np.ndarray) -> np.ndarray:
    """Of the qualities ``qa`` and ``qb`` (RTKLIB's Q), the one whose status is worse (fix, float, single); of two with
    the same status the larger."""
    ra, rb = (np.array([RANK[status(v)] for v in qs], dtype=int) for qs in (qa, qb))
    return np.where((rb > ra) | ((rb == ra) & (qb > qa)), qb, qa)


def at(solution: Solution, gps_seconds) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Where ``solution`` puts the antenna at the times ``gps_seconds`` (N of them, in seconds of GPS time since the
    GPS epoch, as its ``times``, which must be in ascending order, as ``read_pos`` gives them): ``(llh, q, sd, ok)``,
    the latitude, longitude and height (N, 3), the quality Q (N,), the standard deviations east, north, up (N, 3) and
    whether there is a position at all (N,).

    A time on an epoch takes that epoch as it is. A time between two epochs at most :data:`MAX_GAP_S` apart, give or
    take the millisecond the times of a solution are written to, takes the straight line between them (the longitude
    across the 180th meridian the short way, within [-180, 180)), the Q of the one with the worse status and the
    larger deviation on each axis. Where no such pair is around it - at the ends of the solution, next to a gap - a
    time within that millisecond of an epoch takes the epoch. Any other time (in a gap, before the first epoch, after
    the last) has no position, nor has one that would take an epoch with a number that is not finite: ``ok`` False,
    ``llh`` and ``sd`` NaN, ``q`` 0. A position is never taken from an epoch further away."""
    t = np.asarray(gps_seconds, dtype=np.float64).reshape(-1)
    times = np.asarray(solution.times, dtype=np.float64)
    llh, sd = np.full((len(t), 3), np.nan), np.full((len(t), 3), np.nan)
    q = np.zeros(len(t), dtype=int)
    if not len(times):
        return llh, q, sd, np.zeros(len(t), dtype=bool)
    last = len(times) - 1
    finite = np.isfinite(solution.llh).all(axis=1) & np.isfinite(solution.sd).all(axis=1)
    after = np.searchsorted(times, t)  # the first epoch at or after each time
    lo, hi = np.clip(after - 1, 0, last), np.clip(after, 0, last)
    nearest = np.where(np.abs(times[hi] - t) < np.abs(times[lo] - t), hi, lo)
    off = np.abs(times[nearest] - t)
    close = (after > 0) & (after <= last) & (times[hi] - times[lo] <= MAX_GAP_S + TIME_RESOLUTION_S)
    on_epoch = (off <= _SAME_EPOCH_S) | (~close & (off <= TIME_RESOLUTION_S))
    between = ~on_epoch & close & finite[lo] & finite[hi]
    on_epoch &= finite[nearest]
    k = nearest[on_epoch]
    llh[on_epoch], q[on_epoch], sd[on_epoch] = solution.llh[k], solution.q[k], solution.sd[k]
    a, b = lo[between], hi[between]
    weight = ((t[between] - times[a]) / (times[b] - times[a]))[:, None]
    step = solution.llh[b] - solution.llh[a]
    step[:, 1] = (step[:, 1] + 180.0) % 360.0 - 180.0  # across the 180th meridian the short way
    line = solution.llh[a] + weight * step
    line[:, 1] = wrap_longitude(line[:, 1])
    llh[between] = line
    q[between] = _worse(solution.q[a], solution.q[b])
    sd[between] = np.maximum(solution.sd[a], solution.sd[b])
    return llh, q, sd, on_epoch | between


# --- a run of RTKLIB --------------------------------------------------------------------------------------------


def work_folder(chunk) -> Path:
    """The work folder of the PPK runs of ``chunk``: ``gnss/ppk`` in the folder of its data, a folder of the program
    where every run writes its solution and the copies of its inputs (the ``folder`` of :func:`solve`)."""
    return Path(chunk.data_dir) / "gnss" / "ppk"


def last_run(chunk) -> dict | None:
    """The last PPK run that gave photos of ``chunk`` their positions (recorded by :func:`.apply.apply_ppk`): ``{"mrk",
    "photos", "rover", "base", "navs", "base_name", "position_source", "position", "replace_fix", "solution"}``, the
    files as absolute paths (None for those a solution read from a file does not know; ``base`` a list of paths when
    the base is in several files), ``position`` the typed one ``[lon, lat, h]`` (None for the one of the header); None
    when there was none."""
    record = (getattr(chunk, "meta", None) or {}).get(MEMORY) if chunk is not None else None
    last = record.get("last") if isinstance(record, dict) else None
    return dict(last) if isinstance(last, dict) else None


def distance_m(a: Sequence[float], b: Sequence[float], heights: bool = True) -> float:
    """The distance in metres between two positions ``[lon, lat, h]`` (degrees, metres), across and, with ``heights``,
    in height: plenty to tell centimetres apart within a few kilometres."""
    across = horizontal_m(float(a[1]), float(a[0]), float(b[1]), float(b[0]))
    return math.hypot(across, float(b[2]) - float(a[2])) if heights else across


def other_typed_position(chunk, name: str, position: Sequence[float],
                         exclude: Iterable = ()) -> tuple[list[float], float] | None:
    """Of the positions ``[lon, lat, h]`` at which the base ``name`` was typed for the PPK positions of the photos of
    ``chunk`` (but those of the cameras whose keys are in ``exclude``), the one furthest from ``position`` ``[lon,
    lat, h]`` and how far it is (m), when that is more than :data:`SAME_BASE_M`: the same base at two places, one of
    them likely mistyped. None otherwise."""
    skip = set(exclude)
    furthest = None
    for cam in getattr(chunk, "cameras", None) or []:
        gnss = cam.meta.get("gnss") or {}
        base = gnss.get("base") or {}
        if (cam.key in skip or gnss.get("source") != "ppk" or base.get("name") != name
                or base.get("position_source") != "typed" or not base.get("position")):
            continue
        apart = distance_m(position, base["position"])
        if apart > SAME_BASE_M and (furthest is None or apart > furthest[1]):
            furthest = ([float(v) for v in base["position"]], apart)
    return furthest


def baseline_km(solution: Solution) -> float | None:
    """How far the base of ``solution`` is from the flight: the horizontal distance in kilometres from its position to
    the median position of the rover in the solution; None without the position of the base or without positions."""
    position = (solution.base or {}).get("position")
    llh = np.asarray(solution.llh, dtype=np.float64).reshape(-1, 3)
    if not position or not len(llh):
        return None
    lon0, lat0 = float(position[0]), float(position[1])
    east = (llh[:, 1] - lon0 + 180.0) % 360.0 - 180.0  # across the 180th meridian the short way
    return horizontal_m(lat0, lon0, float(np.median(llh[:, 0])), lon0 + float(np.median(east))) / 1000.0


def _on_windows() -> bool:
    return os.name == "nt"


def _short_path(path: str) -> str:
    """The short (8.3) form Windows gives ``path``, an existing file or folder, or ``path`` itself when it gives
    none (short names can be switched off on a volume)."""
    import ctypes
    from ctypes import wintypes

    get = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    get.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    get.restype = wintypes.DWORD
    size = get(path, None, 0)
    if not size:
        return path
    buffer = ctypes.create_unicode_buffer(size)
    return buffer.value if get(path, buffer, size) else path


def _rtklib_folder(folder: Path) -> str:
    """The absolute path of the work folder as RTKLIB is given it. On Windows RTKLIB opens files with the ANSI C
    runtime, which cannot open a path that is not ASCII: such a path becomes its short (8.3) name, and is refused
    when Windows gives none that is ASCII. RTKLIB reads "%r" and "%b" in a path as the names of the stations: a path
    with them is refused."""
    path = str(Path(folder).resolve())
    if _on_windows() and not path.isascii():
        short = _short_path(path)
        if not short.isascii():
            raise RuntimeError(f"RTKLIB opens files with the ANSI C runtime of Windows, which cannot open {path}, and "
                               "Windows has no short name for it: move the project, where the PPK works, to a folder "
                               "whose path has only plain ASCII letters")
        path = short
    for keyword in ("%r", "%b"):
        if keyword in path:
            raise RuntimeError(f"RTKLIB reads {keyword} in a path as the name of a station and cannot work in "
                               f"{path}: move the project, where the PPK works, to a folder without {keyword} in its "
                               "path")
    return path


def _observations(path: Path, role: str) -> tuple[rinex.RinexInfo, tuple[datetime, datetime]]:
    """The header of an observation file and the time its epochs span (GPS time); a ``ValueError`` for a
    navigation file or a file without epochs."""
    info = rinex.header(path)
    if info.kind != "obs":
        raise ValueError(f"the {role} {path.name} holds navigation messages, not observations")
    span = rinex.time_span(path)
    if span is None:
        raise ValueError(f"the {role} {path.name} has no observation epochs")
    return info, span


def span_text(start: datetime | float, end: datetime | float) -> str:
    """The time from ``start`` to ``end`` of GPS time - datetimes, or seconds since the GPS epoch - written to the
    second: ``"2005-04-02 00:00:00 to 2005-04-02 00:59:30 GPS time"``."""
    def text(when: datetime | float) -> str:
        if not isinstance(when, datetime):
            when = _GPS_EPOCH + timedelta(seconds=float(when))
        return f"{when:%Y-%m-%d %H:%M:%S}"

    return f"{text(start)} to {text(end)} GPS time"


def navigation_info(path: str | Path, read=rinex.header) -> rinex.RinexInfo | None:
    """The header of a navigation file, None when ``path`` is not one (or cannot be read); ``read`` reads a header, as
    :func:`rinex.header` (a caller can give one that keeps the headers it read)."""
    try:
        info = read(Path(path))
    except (ValueError, OSError):
        return None
    return info if info.kind == "nav" else None


def _navigation(navs, rover: Path, bases: Sequence[Path]) -> list[tuple[Path, rinex.RinexInfo]]:
    """The navigation files of the run: those given (one or more; a ``ValueError`` for a file of observations), or
    those beside the rover, or, when there are none, beside the files of the base."""
    if isinstance(navs, (str, os.PathLike)):
        navs = [navs]
    if navs:
        given = []
        for path in map(Path, navs):
            info = rinex.header(path)
            if info.kind != "nav":
                raise ValueError(f"{path.name} holds observations, not navigation messages: give the navigation file "
                                 "of the base or of the drone (.nav, .##n, .##p, *_MN.rnx)")
            given.append((path, info))
        return given
    for obs in (rover, *bases):
        found = [(path, info) for path in rinex.find_companions(obs) if (info := navigation_info(path))]
        if found:
            return found
    raise ValueError(f"no navigation file beside the rover {rover.name} or the base {_names(bases)}: add the "
                     "navigation file of the base or of the drone (.nav, .##n, .##g, .##p or *_MN.rnx)")


def base_name(path: str | Path, info: rinex.RinexInfo) -> str:
    """The name of a base: the marker of the header of its RINEX file ``path``, else the name of the file without its
    extension (and without .gz, .Z or .zip)."""
    return info.marker or Path(rinex._without_packing(Path(path).name)).stem


def horizontal_m(lat0: float, lon0: float, lat1: float, lon1: float) -> float:
    """The horizontal distance in metres between two points (degrees), on a sphere as large as the earth: plenty to
    tell metres from hundreds of metres."""
    east = math.radians(float(wrap_longitude(lon1 - lon0))) * math.cos(math.radians(lat0))
    return 6371000.0 * math.hypot(math.radians(lat1 - lat0), east)


def offset_from_header(position: Sequence[float], info: rinex.RinexInfo) -> tuple[float, float] | None:
    """How far a typed ``position`` (latitude, longitude, height above the ellipsoid) of a base is from the position of
    the RINEX header ``info`` of its file: ``(horizontal, height)`` in metres, the height typed less the header's;
    None when the header has no position."""
    if info.approx_xyz is None:
        return None
    lat, lon, h = (float(v) for v in position)
    lat0, lon0, h0 = rinex.ecef_to_llh(info.approx_xyz)
    return horizontal_m(lat0, lon0, lat, lon), h - h0


def far_from_header(offset: tuple[float, float] | None) -> bool:
    """The offset of :func:`offset_from_header` is more than :data:`FAR_FROM_HEADER_M`."""
    return offset is not None and (offset[0] > FAR_FROM_HEADER_M[0] or abs(offset[1]) > FAR_FROM_HEADER_M[1])


def antenna_type(info: rinex.RinexInfo) -> str:
    """The type and radome of the antenna of a RINEX header ``info`` in one line (``"TRM57971.00 NONE"``), "" when it
    names none."""
    return " ".join((info.antenna or "").split())


def antenna_warning(name: str, info: rinex.RinexInfo) -> str:
    """The warning of every run: RTKLIB is given no calibration of the base's antenna, so the offset of its phase
    centre from the reference point of the antenna is not applied - every height is that much too low."""
    kind = antenna_type(info)
    which = (f"of the antenna {kind} of the base {name} is not applied" if kind else
             f"of the antenna of the base {name} is not applied (its RINEX header names no antenna type)")
    return (f"the phase-centre offset {which}: no antenna calibration is given to RTKLIB yet, so the heights can be a "
            "few centimetres too low, more than 10 cm with some choke-ring antennas (calibrated antennas come in a "
            "later version)")


def _base(path: Path, info: rinex.RinexInfo,
          position: Sequence[float] | None) -> tuple[dict, str | None, tuple[float, float, float]]:
    """The base of the solution, typed or from its header (its ``position`` ``[lon, lat, h]``), the warning for a
    position from the header or for a typed one far from it (:func:`far_from_header`), and the position as RTKLIB
    is given it (latitude, longitude, height)."""
    record = {"name": base_name(path, info), "position": None, "position_source": None, "antenna": antenna_type(info),
              "antenna_delta_hen": None if info.antenna_delta_hen is None else list(info.antenna_delta_hen),
              "file": path.name}
    if position is not None:
        lat, lon, h = _base_position(position)
        offset = offset_from_header((lat, lon, h), info)
        warning = None
        if far_from_header(offset):
            apart = f"{offset[0] / 1000.0:.1f} km" if offset[0] >= 1000.0 else f"{offset[0]:.0f} m"
            warning = (f"the typed position of the base {record['name']} is {apart} away horizontally and "
                       f"{offset[1]:+.1f} m in height from the position of its RINEX header: check that latitude and "
                       "longitude are not swapped or without their sign, and that the height is above the ellipsoid, "
                       "not above the sea level")
        return {**record, "position": [lon, lat, h], "position_source": "typed"}, warning, (lat, lon, h)
    if info.approx_xyz is None:
        raise ValueError(f"the base {path.name} has no position in its RINEX header: type the position of the mark it "
                         "stands on (latitude, longitude, height above the ellipsoid)")
    lat, lon, h = rinex.ecef_to_llh(info.approx_xyz)
    warning = (f"the position of the base {record['name']} comes from its RINEX header (latitude {lat:.7f}, "
               f"longitude {lon:.7f}, {h:.2f} m): it can be metres off, and the whole survey moves with it. Type the "
               "surveyed position of the base for an accurate absolute position")
    return {**record, "position": [lon, lat, h], "position_source": "header"}, warning, (lat, lon, h)


def inside(path: str | Path, folder: str | Path) -> bool:
    """``path`` is in ``folder`` (or in a folder inside it), also when the two are written differently (letter case
    on a file system that ignores it, links)."""
    path, folder = Path(path).resolve(), Path(folder).resolve()
    if path.is_relative_to(folder):
        return True
    return folder.is_dir() and any(parent.is_dir() and os.path.samefile(parent, folder) for parent in path.parents)


def _fresh_folder(path: Path) -> None:
    """``path`` as an empty folder, without what an earlier run left in it."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    path.mkdir()


def _copy(source: Path, target: Path, compressed: str | None) -> None:
    """``source`` into ``target`` as plain text: a gzip file decompressed a piece at a time, a file under other layers
    (the Unix compress, zip, Hatanaka) from its decompressed copy (:func:`rinex.plain`: the header of the file made it
    already)."""
    if compressed == "gzip":
        rinex.gunzip(source, target)
    else:
        shutil.copyfile(rinex.plain(source), target)


def _names(paths: Sequence[Path]) -> str:
    return ", ".join(path.name for path in paths)


def _base_files(base: str | os.PathLike | Sequence[str | os.PathLike]) -> list[Path]:
    """The files of a base: ``base`` is one file or several; a ``ValueError`` when it is none."""
    if isinstance(base, (str, os.PathLike)):
        return [Path(base)]
    files = [Path(path) for path in base]
    if not files:
        raise ValueError("no file given for the base")
    return files


def _run_files(work: Path) -> tuple[Path, Path, Path]:
    """The options file, the solution and the time marks of a new run in ``work``: ``solution-<YYYYmmdd-HHMMSS>.conf``,
    ``.pos`` and ``_events.pos`` (local time), with ``-2``, ``-3`` ... after the time when a run of the same second
    has them."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for n in itertools.count(1):
        stem = f"{SOLUTION_PREFIX}-{stamp}" + (f"-{n}" if n > 1 else "")
        files = (work / f"{stem}.conf", work / f"{stem}.pos", work / f"{stem}{EVENTS_SUFFIX}")
        if not any(f.exists() for f in files):
            return files
    raise AssertionError("unreachable")  # pragma: no cover


def _short_enough(paths: Iterable[Path]) -> None:
    """A ``ValueError`` for a path RTKLIB would be given that is :data:`MAX_PATH_BYTES` bytes long or longer: RTKLIB
    copies paths into buffers of 1024 bytes."""
    for path in paths:
        size = len(os.fsencode(str(path)))
        if size >= MAX_PATH_BYTES:
            raise ValueError(f"RTKLIB would be given {path}, a path {size} bytes long, and it takes paths shorter than "
                             f"{MAX_PATH_BYTES} bytes: move the project, where the PPK works, to a folder with a "
                             "shorter path")


def _remove(*paths: Path) -> None:
    """The files of a run that gave no solution: none of them stays."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - a file held open elsewhere: a run of its own
            pass


def solve(rover: str | Path, base: str | Path | Sequence[str | Path], navs=None,
          base_position: Sequence[float] | None = None, *, folder: str | Path) -> Solution:
    """The PPK solution of the antenna of ``rover`` (its RINEX observations) against ``base`` (the RINEX observations
    of a base: one file that covers the whole flight, or several that make it - pieces of an hour, say - which
    :func:`rinex.join` joins in the order of their time, in any order given; a hole between them inside the time of
    the rover is a warning), with the navigation files ``navs`` (one or more; None: those beside the rover, or beside
    the base when the rover has none, as a DJI drone does not always write one). Any file may be packed: gzip, the Unix
    compress, a zip archive of one file, Hatanaka (:func:`rinex.plain`). When the epochs of the base are further apart
    than those of the rover (by their ``INTERVAL``) RTKLIB interpolates the base to the epochs of the rover. The base
    at ``base_position`` (latitude, longitude, height above the ellipsoid of the mark it stands on) or, with None, at
    the position of its RINEX header, with a warning: that one can be metres off; a typed one far from the header's
    (:func:`far_from_header`) has a warning too. Either way RTKLIB adds the antenna height of the base's header,
    recorded in ``Solution.base["antenna_delta_hen"]``, but not the offset of the phase centre of the base's antenna
    (RTKLIB is given no antenna calibration): every run warns of it, naming the antenna of the header
    (:func:`antenna_warning`). ``Solution.base["position"]`` is ``[lon, lat, h]``. Before RTKLIB runs, a
    ``ValueError`` when the rover and the base have no time in common, when there is no navigation file, when the
    base has no position, when the files of the base are not of one receiver (:func:`rinex.join`), when an input is
    in ``folder`` or when a path RTKLIB would be given is too long (:data:`MAX_PATH_BYTES`).

    RTKLIB works in ``folder`` (made when missing; a folder of the program) on copies of the files with plain names in
    its subfolder ``INPUTS_FOLDER``, which holds nothing else, every layer taken off them (a base in several files is
    one file there), removed after the run: it never sees the user's names, and never a packed file (it would run a
    shell command for it). Every run writes its options and its solution under a name of its own
    (``solution-<YYYYmmdd-HHMMSS>.conf`` and ``.pos``), and the solutions of earlier runs stay; a run that gives no
    solution leaves nothing. A ``ValueError`` when RTKLIB computed nothing (with its message), a ``RuntimeError`` when
    it stopped; a message of RTKLIB that is an error but came with positions is a warning of the solution."""
    rover, pieces = Path(rover), _base_files(base)
    if len(pieces) == 1:
        return _solve(rover, pieces, pieces[0], [], navs, base_position, folder)
    with tempfile.TemporaryDirectory(prefix="ppg-base-") as scratch:  # the base in one file, for the length of the run
        joined = Path(scratch) / "base.obs"
        _info, holes = rinex.join(pieces, joined)
        return _solve(rover, pieces, joined, holes, navs, base_position, folder)


def _solve(rover: Path, pieces: list[Path], base: Path, holes: list[tuple[datetime, datetime]], navs,
           base_position: Sequence[float] | None, folder: str | Path) -> Solution:
    """:func:`solve` with the files of the base - ``pieces``, as the user gave them - and the file that RTKLIB is given
    a copy of: that one file, or the file the pieces make joined, with the ``holes`` (GPS time) between them."""
    rover_info, rover_span = _observations(rover, "rover")
    base_info, base_span = _observations(base, "base")
    names = _names(pieces)
    warnings = [f"{name} writes its times in {info.time_system}, which is not a time system of RINEX: they are "
                "read as GPS time, so the time the rover and the base have in common may be off"
                for name, info in ((rover.name, rover_info), (names, base_info))
                if info.time_system not in _RINEX_TIME_SYSTEMS]
    if max(rover_span[0], base_span[0]) > min(rover_span[1], base_span[1]):
        raise ValueError(f"the rover {rover.name} ({span_text(*rover_span)}) and the base {names} "
                         f"({span_text(*base_span)}) have no time in common: the base has to record while the rover "
                         "does")
    warnings += [f"the base has no observations from {span_text(start, end)}, while the rover records: the positions "
                 "there are single (from the rover alone) at best" for start, end in holes
                 if start < rover_span[1] and end > rover_span[0]]
    nav_files = _navigation(navs, rover, pieces)
    base_record, base_warning, mark = _base(pieces[0], base_info, base_position)
    base_record["file"] = names
    if base_warning:
        warnings.append(base_warning)
    warnings.append(antenna_warning(base_record["name"], base_info))
    # a base whose epochs are further apart than the rover's: RTKLIB interpolates it to the epochs of the rover
    interpolate = (rover_info.interval is not None and base_info.interval is not None
                   and base_info.interval > rover_info.interval + 1e-6)

    folder = Path(folder)
    for source in (rover, *pieces, *(path for path, _ in nav_files)):  # the program writes and deletes files in there
        if inside(source, folder):
            raise ValueError(f"{source} is in {folder}, the work folder of the PPK, where the program writes and "
                             f"deletes files: move it out of {folder} first")
    folder.mkdir(parents=True, exist_ok=True)
    work = Path(_rtklib_folder(folder))
    copies = work / INPUTS_FOLDER  # RTKLIB matches the names of its inputs ignoring the case: nothing else in there
    inputs = [(rover, rover_info, copies / "rover.obs"), (base, base_info, copies / "base.obs")]
    inputs += [(path, info, copies / f"nav{i}.nav") for i, (path, info) in enumerate(nav_files, 1)]
    conf, out, events = _run_files(work)
    _short_enough([conf, out, events, *(target for _, _, target in inputs)])
    try:
        # always the position, also the one of the header: RTKLIB 2.4.3 adds the antenna height twice with rinexhead
        conf.write_text(config(mark, interpolate_base=interpolate), encoding="ascii")
        try:
            _fresh_folder(copies)
            for source, info, target in inputs:
                _copy(source, target, info.compressed)
            code, message = _native.ppk_solve(str(conf), [str(target) for _, _, target in inputs], str(out))
        finally:
            shutil.rmtree(copies, ignore_errors=True)
        if code != 0:
            raise RuntimeError(f"RTKLIB stopped with code {code}: {message or 'no message'}")
        nothing = f"nothing was computed: {message or 'RTKLIB wrote no positions'}"
        if not out.exists():
            raise ValueError(nothing)
        try:
            solution = read_pos(out)
        except _NoPositions:
            raise ValueError(nothing) from None
    except BaseException:
        _remove(conf, out, events)
        raise
    if message.lower().startswith("error"):
        warnings.append(f"RTKLIB: {message}")
    solution.base = base_record
    solution.rover = rover.name
    solution.path = folder.resolve() / out.name
    solution.inputs = {"rover": str(rover.absolute()),
                       "base": str(pieces[0].absolute()) if len(pieces) == 1 else [str(p.absolute()) for p in pieces],
                       "navs": [str(path.absolute()) for path, _ in nav_files]}
    solution.warnings = warnings + solution.warnings
    return solution
