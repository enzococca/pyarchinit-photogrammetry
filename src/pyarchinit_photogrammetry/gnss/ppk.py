"""Post-processed kinematic (PPK) positions with RTKLIB: the options of a run, the run on copies of the RINEX files of
a rover and a base, the solution RTKLIB writes (a .pos file, also one made with RTKLIB's own programs or with Emlid
Studio) and where it puts the antenna at the time of a shot. Times are seconds of GPS time since the GPS epoch, as the
shots of a Timestamp.MRK."""
from __future__ import annotations

import gzip
import math
import os
import re
import shutil
import zlib
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
_RANK = {"fix": 0, "float": 1, "single": 2}  # the statuses from the best to the worst

MAX_GAP_S = 1.0  # a time between two epochs further apart than this has no position: none is guessed across a gap
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
# them, and decompresses some extensions with a shell command)
OPTIONS_FILE = "ppk.conf"
SOLUTION_FILE = "solution.pos"
INPUTS_FOLDER = "rtklib_inputs"  # in the work folder: only the copies of a run, emptied when a run starts

_FIELDS =re.compile(r"[,\s]+")  # RTKLIB separates the fields with spaces, or with out-fieldsep (a comma)
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
    # name; position [lat, lon, h] of the mark the base stands on; position_source "header" | "typed";
    # antenna_delta_hen [h, e, n] (m): the antenna above the mark from the RINEX header of the base, which RTKLIB
    # adds (None when the header has none); file. Empty from read_pos
    base: dict
    path: Path  # the .pos file
    warnings: list[str] = field(default_factory=list)
    rover: str = ""  # the name of the rover's observation file; empty from read_pos


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
           systems: str | Iterable[str] = "all") -> str:
    """The options file of RTKLIB for a PPK run (the text of rnx2rtkp's -k file). The rover moving (``mode``
    "kinematic") or still ("static", "static-start"; "dgps" for code only); the forward and backward solutions
    combined, the ambiguities fixed and held; the satellites ``elevation_mask`` degrees above the horizon or more, of
    the constellations ``systems`` ("all" or their letters G R E C J I S), on two frequencies. The base at
    ``base_position`` (latitude, longitude in degrees, height above the ellipsoid in metres: the mark it stands on),
    to which RTKLIB adds the antenna height of the base's RINEX header (ANTENNA: DELTA H/E/N), as RINEX means it.
    With None the base is at the position of its RINEX header (rinexhead): RTKLIB-EX adds the antenna height once
    then too, RTKLIB 2.4.3 twice, which is why ``solve`` always writes the position. The solution in latitude,
    longitude and ellipsoidal height, timed in GPS weeks and seconds of GPS time. Only option names of RTKLIB's
    table, no file of any kind."""
    if mode not in MODES:
        raise ValueError(f"positioning mode {mode!r}: it is one of {', '.join(MODES)}")
    mask = float(elevation_mask)
    if not 0.0 <= mask < 90.0:
        raise ValueError(f"the elevation mask, {elevation_mask}, is not between 0 and 90 degrees")
    options = [("pos1-posmode", mode), ("pos1-frequency", "l1+l2"), ("pos1-soltype", "combined"),
               ("pos1-elmask", f"{mask:g}"), ("pos1-navsys", str(_navsys(systems))), ("pos2-armode", "fix-and-hold")]
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
    ra, rb = (np.array([_RANK[status(v)] for v in qs], dtype=int) for qs in (qa, qb))
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
    whose files every run replaces (the ``folder`` of :func:`solve`)."""
    return Path(chunk.data_dir) / "gnss" / "ppk"


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
                               "Windows has no short name for it: choose a work folder whose path has only ASCII "
                               "letters")
        path = short
    for keyword in ("%r", "%b"):
        if keyword in path:
            raise RuntimeError(f"RTKLIB reads {keyword} in a path as the name of a station and cannot work in "
                               f"{path}: choose a work folder without {keyword} in its path")
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


def _navigation_info(path: Path) -> rinex.RinexInfo | None:
    """The header of a navigation file, None when ``path`` is not one."""
    try:
        info = rinex.header(path)
    except (ValueError, OSError):
        return None
    return info if info.kind == "nav" else None


def _navigation(navs, rover: Path, base: Path) -> list[tuple[Path, rinex.RinexInfo]]:
    """The navigation files of the run: those given (one or more; a ``ValueError`` for a file of observations), or
    those beside the rover, or, when there are none, beside the base."""
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
    for obs in (rover, base):
        found = [(path, info) for path in rinex.find_companions(obs) if (info := _navigation_info(path))]
        if found:
            return found
    raise ValueError(f"no navigation file beside the rover {rover.name} or the base {base.name}: add the navigation "
                     "file of the base or of the drone (.nav, .##n, .##g, .##p or *_MN.rnx)")


def _base(path: Path, info: rinex.RinexInfo, position: Sequence[float] | None) -> tuple[dict, str | None]:
    """The base of the solution, typed or from its header, and the warning for a position from the header."""
    stem = path.name[:-3] if path.name.lower().endswith(".gz") else path.name
    record = {"name": info.marker or Path(stem).stem, "position": None, "position_source": None,
              "antenna_delta_hen": None if info.antenna_delta_hen is None else list(info.antenna_delta_hen),
              "file": path.name}
    if position is not None:
        return {**record, "position": list(_base_position(position)), "position_source": "typed"}, None
    if info.approx_xyz is None:
        raise ValueError(f"the base {path.name} has no position in its RINEX header: type the position of the mark it "
                         "stands on (latitude, longitude, height above the ellipsoid)")
    lat, lon, h = rinex.ecef_to_llh(info.approx_xyz)
    warning = (f"the position of the base {record['name']} comes from its RINEX header ({lat:.7f}, {lon:.7f}, "
               f"{h:.2f} m): it can be metres off, and the whole survey moves with it. Type the surveyed position of "
               "the base for an accurate absolute position")
    return {**record, "position": [lat, lon, h], "position_source": "header"}, warning


def _inside(path: Path, folder: Path) -> bool:
    """``path`` is in ``folder`` (or in a folder inside it), also when the two are written differently (letter case
    on a file system that ignores it, links)."""
    path, folder = path.resolve(), folder.resolve()
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
    """``source`` into ``target`` as plain text, a gzip file decompressed a piece at a time."""
    if compressed != "gzip":
        shutil.copyfile(source, target)
        return
    try:
        with gzip.open(source, "rb") as packed, open(target, "wb") as plain:
            shutil.copyfileobj(packed, plain, 1 << 20)
    except (EOFError, gzip.BadGzipFile, zlib.error) as error:
        raise ValueError(f"{source.name} is cut short or damaged ({error}): download it again") from None


def solve(rover: str | Path, base: str | Path, navs=None, base_position: Sequence[float] | None = None, *,
          folder: str | Path) -> Solution:
    """The PPK solution of the antenna of ``rover`` (its RINEX observations) against ``base`` (the RINEX observations
    of a base), with the navigation files ``navs`` (one or more; None: those beside the rover, or beside the base
    when the rover has none, as a DJI drone does not always write one). The base at ``base_position`` (latitude,
    longitude, height above the ellipsoid of the mark it stands on) or, with None, at the position of its RINEX
    header, with a warning: that one can be metres off. Either way RTKLIB adds the antenna height of the base's
    header, recorded in ``Solution.base["antenna_delta_hen"]``. Before RTKLIB runs, a ``ValueError`` when the rover
    and the base have no time in common, when there is no navigation file, when the base has no position or when an
    input is in ``folder``.

    RTKLIB works in ``folder`` (made when missing; a folder of the program: its ppk.conf and solution.pos are
    replaced) on copies of the files with plain names in its subfolder ``INPUTS_FOLDER``, which holds nothing else,
    the gzip ones decompressed, removed after the run: it never sees the user's names. A ``ValueError`` when RTKLIB
    computed nothing (with its message), a ``RuntimeError`` when it stopped; a message of RTKLIB that is an error but
    came with positions is a warning of the solution."""
    rover, base = Path(rover), Path(base)
    rover_info, rover_span = _observations(rover, "rover")
    base_info, base_span = _observations(base, "base")
    warnings = [f"{path.name} writes its times in {info.time_system}, which is not a time system of RINEX: they are "
                "read as GPS time, so the time the rover and the base have in common may be off"
                for path, info in ((rover, rover_info), (base, base_info))
                if info.time_system not in _RINEX_TIME_SYSTEMS]
    if max(rover_span[0], base_span[0]) > min(rover_span[1], base_span[1]):
        raise ValueError(f"the rover {rover.name} ({span_text(*rover_span)}) and the base {base.name} "
                         f"({span_text(*base_span)}) have no time in common: the base has to record while the rover "
                         "does")
    nav_files = _navigation(navs, rover, base)
    base_record, base_warning = _base(base, base_info, base_position)
    if base_warning:
        warnings.append(base_warning)

    folder = Path(folder)
    for source in (rover, base, *(path for path, _ in nav_files)):  # the files of a run are replaced and removed
        if _inside(source, folder):
            raise ValueError(f"{source} is in the work folder {folder}, whose files are replaced at every run: choose "
                             "a work folder apart from the observations")
    folder.mkdir(parents=True, exist_ok=True)
    work = Path(_rtklib_folder(folder))
    copies = work / INPUTS_FOLDER  # RTKLIB matches the names of its inputs ignoring the case: nothing else in there
    inputs = [(rover, rover_info, copies / "rover.obs"), (base, base_info, copies / "base.obs")]
    inputs += [(path, info, copies / f"nav{i}.nav") for i, (path, info) in enumerate(nav_files, 1)]
    conf, out = work / OPTIONS_FILE, work / SOLUTION_FILE
    # always the position, also the one of the header: RTKLIB 2.4.3 adds the antenna height twice with rinexhead
    conf.write_text(config(base_record["position"]), encoding="ascii")
    out.unlink(missing_ok=True)  # the solution of an earlier run is not the solution of this one
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
    if message.lower().startswith("error"):
        warnings.append(f"RTKLIB: {message}")
    solution.base = base_record
    solution.rover = rover.name
    solution.path = folder.resolve() / SOLUTION_FILE
    solution.warnings = warnings + solution.warnings
    return solution
