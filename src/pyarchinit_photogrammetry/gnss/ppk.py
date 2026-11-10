"""Post-processed kinematic (PPK) positions with RTKLIB: the options of a run, the run on copies of the RINEX files of
a rover and a base, the solution RTKLIB writes (a .pos file, also one made with RTKLIB's own programs or with Emlid
Studio) and where it puts the antenna at the time of a shot. Times are seconds of GPS time since the GPS epoch, as the
shots of a Timestamp.MRK.

A position the program keeps - the base of a solution, the reference of a photo - is ``[lon, lat, h]``, longitude
first; a position typed by the user, and the options of RTKLIB, are latitude, longitude, height."""
from __future__ import annotations

import copy
import itertools
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from .. import _native
from . import atx as antex
from . import frames, rinex, rinex_files
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
# two epochs this much more than MAX_GAP_S apart are still next to each other: a receiver whose clock jumps by a
# millisecond logs 1.001 s apart, which the floats of times of 8e8 s do not keep exactly
GAP_SLACK_S = 1.5e-3
INTERPOLATION_WARNING_M = 0.03  # the straight line between two epochs this far from the path: a warning

MODES = ("kinematic", "static", "static-start", "dgps")  # RTKLIB's positioning modes that work with a base
STATIC_MODES = ("static", "static-start")  # those of a rover standing still
# A rover standing still for the whole session ("static": the user's own base placed by a static baseline from a
# permanent station, tens of kilometres away) has the ionosphere and the troposphere estimated, slant delay by slant
# delay and as a zenith delay at each end: with RTKLIB's broadcast model and Saastamoinen alone the difference of the
# atmosphere above the two goes into the fixed position. On a real day of the EPN (2026-10-06, 2 hours at midday)
# baselines of 23 km were 2.3-2.7 cm off across and 2.8-3.3 cm in height that way, within 0.6 cm estimated, and one of
# 57 km 6.2 cm off across, 0.6 cm estimated. The rover of a flight (kinematic, static-start) keeps RTKLIB's models.
STATIC_ATMOSPHERE = (("pos1-ionoopt", "est-stec"), ("pos1-tropopt", "est-ztd"))
SOLUTIONS = ("forward", "backward", "combined")  # RTKLIB's pos1-soltype: the epochs in their order, reversed, or both
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
# the line of an _events.pos for a time mark between epochs RTKLIB could not solve ("<time>   Q=0, Time mark is not
# valid"): no position, left out by read_pos
INVALID_TIME_MARK = "Time mark is not valid"
EVENT_KINDS = ("rinex_events", "ubx_tim_tm2")  # the shots that are events of the rover's receiver (events_check)
MARK_TIME_S = 1e-3  # a shot and a time mark of RTKLIB this close are the same event: RTKLIB writes the millisecond
INPUTS_FOLDER = "rtklib_inputs"  # in the work folder: only the copies of a run, emptied when a run starts
# the copy of the antenna calibrations in there: RTKLIB tells an ANTEX file by its extension, ".atx" or ".ATX"
ANTENNA_COPY = "antennas.atx"
MAX_ANTENNA_NAME = 63  # RTKLIB keeps the type of a receiver antenna in 64 bytes (anttype[2][MAXANT]), the end included
# RTKLIB copies the paths of its files into buffers of 1024 bytes: a path this long or longer is refused before it runs
MAX_PATH_BYTES = 1000

BASELINE_WARNING_KM = 20.0  # a base further than this from the flight: the ambiguities are hard to fix
SAME_BASE_M = 0.05  # one base typed at two positions further apart than this in one chunk: one of them is likely wrong
# where the position of a base comes from: typed by the user, the position of its RINEX header, the coordinates of a
# permanent station, a baseline from a permanent station, a PPP solution
BASE_SOURCES = ("typed", "header", "station", "baseline", "ppp")
# the roles of the receivers of a run and how the messages name them: the rover and the base of the PPK of a flight,
# the user's own base and the permanent station of a static baseline that places it (and the user's base placed by a
# static PPP, of the antenna warnings: :func:`antenna_warning`)
ROLES = {"rover": "the rover", "base": "the base", "your base": "your base", "station": "the station",
         "ppp": "your base"}
RUN_ROLES = (("rover", "base"), ("your base", "station"))  # the roles of the rover and the base of a run of solve
# what an offset of a phase centre that is not applied does to the result, and what one of another radome or name
# does, by the role of the receiver whose antenna it is: the base of a PPK and the station of a static baseline are
# taken for lower than their phase centres are, and what is found from them comes out too low; the user's own base is
# taken for its phase centre, and its mark comes out too high - in a PPP with nothing to make up for it
_ANTENNA_EFFECTS = {
    "base": ("the heights can be a few centimetres too low", "the phase-centre offset can be a few millimetres off"),
    "station": ("the mark found for your base can be a few centimetres too low",
                "the phase-centre offset can be a few millimetres off"),
    "your base": ("the mark found can be a few centimetres too high",
                  "the phase-centre offset can be a few millimetres off"),
    "ppp": ("in a PPP the whole offset of the phase centre goes into the height: the mark found can be a few "
            "centimetres too high", "the height found can be a few millimetres off: in a PPP the whole offset of the "
            "antenna weighs on the height"),
}
MEMORY = "gnss_ppk"  # chunk.meta[MEMORY]["last"]: the last PPK run of the chunk (last_run), where the dialog starts

_FIELDS = re.compile(r"[,\s]+")  # RTKLIB separates the fields with spaces, or with out-fieldsep (a comma)
_LEGEND = re.compile(r"lat/lon/height=([^/,)]*)/([^,)]*)")
_COLUMNS = ("latitude(deg)", "longitude(deg)", "height(m)", "Q", "ns", "sdn(m)", "sde(m)", "sdu(m)")


@dataclass
class Solution:
    """The positions RTKLIB computed for the antenna of the rover, one row per epoch in time order."""

    times: np.ndarray  # (N,) float64: seconds of GPS time since the GPS epoch (GPS week x 604800 + seconds of week)
    llh: np.ndarray  # (N, 3): latitude, longitude (degrees), height above the WGS84 ellipsoid (m)
    q: np.ndarray  # (N,) int: RTKLIB's quality, 1 fix, 2 float, 4 dgps, 5 single, 6 ppp (``QUALITY``)
    ns: np.ndarray  # (N,) int: the satellites used
    sd: np.ndarray  # (N, 3): standard deviations east, north, up (m)
    # name; position [lon, lat, h] of the mark the base stands on (longitude first, as every position the program
    # keeps: the reference of a photo, its antenna); position_source: one of BASE_SOURCES; frame: the reference frame
    # of the position (frames.FRAMES, "unknown" when nobody knows it: typed, from the header), epoch: the decimal year
    # of its coordinates (None when not known), sd: its standard deviations [e, n, u] (m, None when not known),
    # detail: what its source says of it (BasePosition.detail); antenna: its type and radome
    # in the RINEX header of the base ("" when it names none); antenna_delta_hen [h, e, n] (m): the antenna above the
    # mark from that header, which RTKLIB adds (None when the header has none); file; antenna_calibration: None when
    # RTKLIB was given no file of antenna calibrations, else {"file": its name, "entry": the entry RTKLIB takes for
    # the antenna of the base (None: the antenna is not in the file, RTKLIB applies none), "without_radome": the entry
    # is the type without the radome of the header, "up_m": the offset of the phase centre of L1 above the reference
    # point of the antenna in the entry, m (None without an entry)}. Empty from read_pos
    base: dict
    path: Path  # the .pos file
    warnings: list[str] = field(default_factory=list)
    rover: str = ""  # the name of the rover's observation file (the names, joined by ", "); empty from read_pos
    # the files of the run, absolute: "rover" and "base" (paths; a list of paths for a rover or a base in several
    # files), "navs" (a list of paths); empty from read_pos
    inputs: dict = field(default_factory=dict)
    # with an antenna of the rover (solve's rover_antenna), what the antenna file gives it, as
    # base["antenna_calibration"] for the base: None without an antenna of the rover or without a file given to RTKLIB
    rover_calibration: dict | None = None
    # the holes in the observations of a rover given in several files, each as the epoch before it and the epoch after
    # it in GPS time (rinex.join); empty for a rover in one file, whose holes are not looked for, and from read_pos
    rover_holes: list[tuple[datetime, datetime]] = field(default_factory=list)


@dataclass
class BasePosition:
    """The position of the mark a base stands on - latitude, longitude (degrees), height above the ellipsoid (m) - and
    what is known of it: where it comes from (``source``, one of :data:`BASE_SOURCES`), its reference ``frame`` (one of
    :data:`.frames.FRAMES`: the positions computed from the base are in it; ``"unknown"`` when nobody knows it), the
    ``epoch`` of its coordinates (a decimal year; a position in ITRF2020 cannot be without it), its standard deviations
    ``sd`` east, north, up (m) when known, and ``detail``, what its source says of it (the station, the solution...),
    a dict of what JSON writes: it is copied into every photo and saved with the project.

    A position of the rover is the base's coordinates plus the baseline, so in a frame that moves with time (ITRF2020)
    the positions computed from the base are at the epoch of the base's coordinates, not at the epoch of the flight:
    coordinates of a station at 2015.0 make positions at 2015.0, which the program converts as such
    (:func:`.apply.apply_ppk`); carried to the day of the flight, they make positions of that day.

    A ``ValueError`` for a position off the earth, a source or a frame the program does not know, an epoch that is not
    a year or that is missing in ITRF2020 (:func:`.frames.frame_and_epoch`), deviations that are not three numbers of
    zero or more, a detail that is not a dict JSON can write."""

    lat: float
    lon: float
    h: float
    source: str
    frame: str = frames.UNKNOWN
    epoch: float | None = None
    sd: tuple | None = None
    detail: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.lat, self.lon, self.h = _base_position((self.lat, self.lon, self.h))
        if self.source not in BASE_SOURCES:
            raise ValueError(f"the position of a base comes from one of {', '.join(BASE_SOURCES)}, not from "
                             f"{self.source!r}")
        self.frame, self.epoch = frames.frame_and_epoch(self.frame, self.epoch)
        if self.sd is not None:
            try:
                sd = tuple(float(v) for v in self.sd)
            except (TypeError, ValueError):
                sd = ()
            if len(sd) != 3 or not all(math.isfinite(v) and v >= 0.0 for v in sd):
                raise ValueError(f"the standard deviations of the base are three numbers of metres (east, north, up), "
                                 f"not {self.sd!r}")
            self.sd = sd
        detail = {} if self.detail is None else self.detail
        try:
            if not isinstance(detail, dict):
                raise TypeError(f"a {type(detail).__name__}, not a dict")
            json.dumps(detail)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"the detail of the position of the base is copied into every photo and saved with the "
                             f"project: a dict of what JSON writes (text, numbers, lists, dicts) - {exc}") from None
        self.detail = copy.deepcopy(detail)


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


def path_problem(path: str) -> str | None:
    """Why ``path`` cannot be the antenna file of RTKLIB's options file, or None when it can. RTKLIB reads from a "#" to
    the end of the line as a comment, reads the file by lines, keeps a path in a buffer of 1024 bytes, and takes a file
    for ANTEX only when it is called ``.atx`` or ``.ATX`` (any other is an antenna file of the NGS to it)."""
    if not path.endswith((".atx", ".ATX")):
        return (f"the antenna file {path} should be called .atx (or .ATX): RTKLIB reads any other file as an antenna "
                "file of the NGS, not as ANTEX")
    if "#" in path:
        return (f"the path of the antenna file, {path}, has a \"#\" in it, which RTKLIB reads in its options file as "
                "the start of a comment: move the project, where the PPK works, to a folder without a \"#\" in its "
                "path")
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        return (f"the path of the antenna file, {path!r}, has a control character in it (a line end, a tab...), and "
                "RTKLIB reads its options file by lines")
    size = len(os.fsencode(path))
    if size >= MAX_PATH_BYTES:
        return (f"the path of the antenna file is {size} bytes long, and RTKLIB takes paths shorter than "
                f"{MAX_PATH_BYTES} bytes: move the project, where the PPK works, to a folder with a shorter path")
    return None


def config(base_position: Sequence[float] | None, mode: str = "kinematic", elevation_mask: float = 15.0,
           systems: str | Iterable[str] = "all", interpolate_base: bool = False,
           antenna_file: str | os.PathLike | None = None, rover_antenna: str = "", solution: str = "combined",
           static_single: bool = False) -> str:
    """The options file of RTKLIB for a PPK run (the text of rnx2rtkp's -k file). The rover moving (``mode``
    "kinematic") or still ("static", "static-start"; "dgps" for code only); the epochs solved forward and backward and
    the two solutions combined (``solution`` "combined"; "forward" or "backward" for one of them), the ambiguities
    fixed and held; the satellites ``elevation_mask`` degrees above the horizon or more, of the constellations
    ``systems`` ("all" or their letters G R E C J I S), on two frequencies. A rover still for the whole session
    ("static") has the ionosphere and the troposphere estimated (:data:`STATIC_ATMOSPHERE`: ``est-stec``,
    ``est-ztd``); the others keep RTKLIB's broadcast ionosphere and Saastamoinen. With ``static_single`` and a rover
    standing still RTKLIB writes one position for the whole session (``out-solstatic=single``: the last of the epochs
    with the best quality, at the time of the first epoch it solved) instead of one for every epoch. The base at
    ``base_position`` (latitude, longitude in degrees, height above the ellipsoid in metres: the mark it stands on),
    to which RTKLIB adds the antenna height of the base's RINEX header (ANTENNA: DELTA H/E/N), as RINEX means it.
    With None the base is at the position of its RINEX header (rinexhead): RTKLIB-EX adds the antenna height once
    then too, RTKLIB 2.4.3 twice, which is why ``solve`` always writes the position. The solution in latitude,
    longitude and ellipsoidal height, timed in GPS weeks and seconds of GPS time. With ``interpolate_base`` RTKLIB
    interpolates the observations of the base to the epochs of the rover (``misc-timeinterp``), for a base whose epochs
    are further apart than the rover's.

    With ``antenna_file``, an ANTEX file (it has to be called ``.atx``: RTKLIB tells the format by the extension),
    RTKLIB looks for the antenna of the base, named in the header of its RINEX file, in it (:func:`.atx.lookup` makes
    the same search) and applies the offset of its phase centre from the reference point, and the variations of it
    with the angle from the zenith; an antenna that is not in the file gets nothing. ``rover_antenna`` is the antenna
    of the rover for the same search: "*" for the one of the header of its RINEX file, "" (the default) for none,
    which is right for a drone; with "*" RTKLIB also takes the antenna height above the mark from that header, so the
    positions are of the mark the rover stands on. A path is refused that RTKLIB would read differently from the way it
    is written (:func:`path_problem`). Only option names of RTKLIB's table; the only file is ``antenna_file``."""
    if mode not in MODES:
        raise ValueError(f"positioning mode {mode!r}: it is one of {', '.join(MODES)}")
    if solution not in SOLUTIONS:
        raise ValueError(f"solution type {solution!r}: it is one of {', '.join(SOLUTIONS)}")
    if static_single and mode not in STATIC_MODES:
        raise ValueError(f"one position for the whole session (static_single) is for a rover standing still: the mode "
                         f"is one of {', '.join(STATIC_MODES)}, not {mode!r}")
    mask = float(elevation_mask)
    if not 0.0 <= mask < 90.0:
        raise ValueError(f"the elevation mask, {elevation_mask}, is not between 0 and 90 degrees")
    options = [("pos1-posmode", mode), ("pos1-frequency", "l1+l2"), ("pos1-soltype", solution),
               ("pos1-elmask", f"{mask:g}"), ("pos1-navsys", str(_navsys(systems))), ("pos2-armode", "fix-and-hold")]
    if mode == "static":
        options += STATIC_ATMOSPHERE
    if interpolate_base:
        options.append(("misc-timeinterp", "on"))
    if base_position is None:
        options.append(("ant2-postype", "rinexhead"))
    else:
        lat, lon, h = _base_position(base_position)
        options += [("ant2-postype", "llh"), ("ant2-pos1", repr(lat)), ("ant2-pos2", repr(lon)), ("ant2-pos3", repr(h))]
    options.append(("ant2-anttype", "*"))  # "*": the antenna of the RINEX header, and its height above the mark
    if rover_antenna:
        if "#" in rover_antenna or any(ord(c) < 32 or ord(c) == 127 for c in rover_antenna):
            raise ValueError(f"the antenna of the rover, {rover_antenna!r}, has a \"#\" or a control character in it, "
                             "which RTKLIB cannot read in its options file")
        if len(os.fsencode(rover_antenna)) > MAX_ANTENNA_NAME:
            raise ValueError(f"the antenna of the rover, {rover_antenna!r}, is longer than {MAX_ANTENNA_NAME} "
                             "characters, which RTKLIB cannot keep")
        options.append(("ant1-anttype", rover_antenna))
    if antenna_file:
        path = os.fspath(antenna_file)
        problem = path_problem(path)
        if problem:
            raise ValueError(problem)
        # posopt2 on: the variations with the angle from the zenith, not only the offset
        options += [("file-rcvantfile", path), ("pos1-posopt2", "on")]
    options += [("out-solformat", "llh"), ("out-outhead", "on"), ("out-timesys", "gpst"), ("out-timeform", "tow"),
                ("out-height", "ellipsoidal")]
    if static_single:
        options.append(("out-solstatic", "single"))
    return "# RTKLIB options of a PPK run (pyarchinit-photogrammetry)\n" + "".join(
        f"{name:<18}={value}\n" for name, value in options)


# --- the solution -----------------------------------------------------------------------------------------------


class NoPositions(ValueError):
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
    seconds of GPS time since the GPS epoch, the rows in time order, the standard deviations east, north, up, the
    quality Q of each row as RTKLIB writes it (:data:`QUALITY`: 6 for a PPP solution, :mod:`.ppp`). The
    columns are found by their names in the header. A file without positions, in x/y/z or e/n/u, in degrees,
    minutes and seconds, on another datum or with heights above the geoid is a ``ValueError`` saying so, a row with a
    number that is not finite (nan, inf) one naming its line; a file that does not say what its heights are is read
    as RTKLIB's default (above the ellipsoid), with a warning. The lines of an ``_events.pos`` for time marks RTKLIB
    could not place (:data:`INVALID_TIME_MARK`) are left out: :func:`.events.events_pos` reads them."""
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
            if text.endswith(INVALID_TIME_MARK):  # an _events.pos: a time mark RTKLIB could not place (events.py)
                continue
            rows.append((number, text))
    if not rows:
        raise NoPositions(f"{path.name}: nothing was computed: the solution has no positions")
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


def static_position(path: str | Path) -> tuple[tuple[float, float, float], int, tuple[float, float, float]]:
    """The one position of a static solution that RTKLIB wrote as a single one (:func:`config` ``static_single``) in
    the file ``path``: ``((latitude, longitude, height), Q, (east, north, up))`` - degrees and metres above the
    ellipsoid, RTKLIB's quality (1 fix, 2 float, 6 ppp: :data:`QUALITY`) and the standard deviations in metres, which
    the file has as sdn, sde, sdu. A ``ValueError`` for what :func:`read_pos` refuses (a file without positions among
    them) and for a file of more than one position: it is no single static solution."""
    solution = read_pos(path)
    if len(solution.times) != 1:
        raise ValueError(f"{solution.path.name}: {len(solution.times)} positions, not the one of a single static "
                         "solution (out-solstatic=single)")
    lat, lon, h = (float(v) for v in solution.llh[0])
    east, north, up = (float(v) for v in solution.sd[0])  # read_pos has them east, north, up
    return (lat, lon, h), int(solution.q[0]), (east, north, up)


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
    take the millisecond the times of a solution are written to and a millisecond the clock of a receiver jumps
    (:data:`GAP_SLACK_S`), takes the straight line between them (the longitude
    across the 180th meridian the short way, within [-180, 180)), the Q of the one with the worse status and the
    larger deviation on each axis. Where no such pair is around it - at the ends of the solution, next to a gap - a
    time within that millisecond and a half of an epoch takes the epoch (RTKLIB writes the epochs of a receiver
    whose clock is a fraction of a millisecond off at .999). Any other time (in a gap, before the first epoch, after
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
    close = (after > 0) & (after <= last) & (times[hi] - times[lo] <= MAX_GAP_S + GAP_SLACK_S)
    on_epoch = (off <= _SAME_EPOCH_S) | (~close & (off <= GAP_SLACK_S))
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


def _metres(llh: np.ndarray, d: np.ndarray) -> np.ndarray:
    """Differences ``d`` of latitude, longitude (degrees) and height near ``llh`` as north, east, up metres."""
    r = math.radians(1.0) * 6378137.0
    return np.c_[d[:, 0] * r, d[:, 1] * r * np.cos(np.radians(llh[:, 0])), d[:, 2]]


def interpolation_error(solution: Solution, gps_seconds) -> np.ndarray:
    """How far (metres, N,) the straight line :func:`at` draws between two epochs may be from the path of the antenna
    at each of ``gps_seconds``: the curvature of the path from the epochs around (the second difference of the epoch
    before, the two of the interval and the one after, each where it is there and as close: :data:`MAX_GAP_S`), as
    ``|p''| (t - t_a)(t_b - t) / 2`` - about ``|p- - 2 p0 + p+| / 8`` in the middle of an interval -, the larger of
    the two ends. 0 on an epoch, where nothing is interpolated, and where no epoch around gives a curvature; NaN
    where :func:`at` gives no position. A drone turning on a circle of 20 m at 10 m/s is 0.62 m off the line in the
    middle of epochs 1 s apart, 25 mm at 5 Hz, 6 mm at 10 Hz."""
    t = np.asarray(gps_seconds, dtype=np.float64).reshape(-1)
    _llh, _q, _sd, ok = at(solution, t)
    out = np.where(ok, 0.0, np.nan)
    times = np.asarray(solution.times, dtype=np.float64)
    llh = np.asarray(solution.llh, dtype=np.float64)
    n = len(times)
    if n < 3 or not ok.any():
        return out
    after = np.clip(np.searchsorted(times, t), 1, n - 1)
    lo, hi = after - 1, after
    inside = ok & (t - times[lo] > _SAME_EPOCH_S) & (times[hi] - t > _SAME_EPOCH_S)
    near = MAX_GAP_S + GAP_SLACK_S

    def curvature(k: np.ndarray) -> np.ndarray:  # |p''| at epoch k (m/s^2), NaN where an epoch around is missing
        good = (k >= 1) & (k <= n - 2)
        k0 = np.clip(k, 1, n - 2)
        h1, h2 = times[k0] - times[k0 - 1], times[k0 + 1] - times[k0]
        good &= (h1 > 0) & (h2 > 0) & (h1 <= near) & (h2 <= near)
        d1, d2 = llh[k0] - llh[k0 - 1], llh[k0 + 1] - llh[k0]
        for d in (d1, d2):
            d[:, 1] = (d[:, 1] + 180.0) % 360.0 - 180.0
        h1s, h2s = np.where(good, h1, 1.0)[:, None], np.where(good, h2, 1.0)[:, None]
        second = 2.0 * (d2 / h2s - d1 / h1s) / (h1s + h2s)
        value = np.linalg.norm(_metres(llh[k0], second), axis=1)
        return np.where(good & np.isfinite(value), value, np.nan)

    both = np.c_[curvature(lo), curvature(hi)]
    worst = np.where(np.isnan(both).all(axis=1), 0.0, np.nanmax(np.where(np.isnan(both), -1.0, both), axis=1))
    span = (t - times[lo]) * (times[hi] - t) / 2.0
    out[inside] = (worst * span)[inside]
    return out


def epoch_rate_hz(solution: Solution) -> float | None:
    """The rate the receiver logged at (epochs per second, from the median interval), None with fewer than 2 epochs."""
    steps = np.diff(np.asarray(solution.times, dtype=np.float64))
    steps = steps[steps > 0]
    return float(1.0 / np.median(steps)) if len(steps) else None


# --- the events of the rover against RTKLIB's time marks ----------------------------------------------------------


def _rover_observations(solution: Solution) -> tuple[list[Path], list[str]]:
    """The RINEX observations of the rover of ``solution``'s run - its files (``inputs["rover"]``), those of a raw log
    being its conversion (``inputs["converted"]``) - and what keeps them from being read: a conversion no longer in
    the cache (:func:`.convert.to_rinex` removes old ones), a file moved. Nothing for a solution read from a file."""
    inputs = solution.inputs or {}
    given = inputs.get("rover")
    rovers = [given] if isinstance(given, str) else list(given or [])
    obs = {item.get("source"): item.get("obs") for item in inputs.get("converted") or []}
    paths, notes = [], []
    for rover in rovers:
        path = Path(obs.get(rover) or rover)
        if path.is_file():
            paths.append(path)
        elif rover in obs:
            notes.append(f"the conversion of the rover {Path(rover).name} is no longer in the cache of conversions")
        else:
            notes.append(f"the file of the rover {Path(rover).name} is no longer where it was")
    return (paths, []) if not notes else ([], notes)


def _apart_s(times: np.ndarray, t: np.ndarray) -> np.ndarray:
    """How far each of ``t`` is from the nearest of the sorted ``times`` (seconds; inf when there are none)."""
    if not len(times):
        return np.full(len(t), np.inf)
    after = np.clip(np.searchsorted(times, t), 0, len(times) - 1)
    before = np.clip(after - 1, 0, len(times) - 1)
    return np.minimum(np.abs(times[after] - t), np.abs(times[before] - t))


def _solution_type(path: Path) -> str | None:
    """RTKLIB's pos1-soltype in the options file of the run beside the solution ``path``, None without one."""
    conf = path.with_suffix(".conf")
    try:
        for line in conf.read_text(errors="replace").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "pos1-soltype" and value.split():
                return value.split()[0].strip()
    except OSError:
        pass
    return None


def events_check(solution: Solution, shotset, tolerance_m: float = 0.001) -> list[str]:
    """The shots of ``shotset`` - the events of the rover's receiver (:data:`EVENT_KINDS`: the flag-5 epochs of its
    RINEX file, the TIM-TM2 of its u-blox log) - checked against the time marks RTKLIB wrote for the run of
    ``solution`` (``<solution>_events.pos``, :func:`.events.events_pos`); a list of warnings, empty when all agree.

    - Shots at no time mark (within :data:`MARK_TIME_S`) of the rover's observations - the events RTKLIB was given,
      :func:`.events.rinex_events` of its files, or of the conversion of a raw log -: the events of another receiver
      or flight, or shifted; the number and the largest gap. For a solution read from a file, whose rover is not
      known, or whose rover is no longer there (moved, or its conversion gone from the cache: said so), the shots are
      compared with the marks written in the file, which RTKLIB may simply not have written.
    - Marks of the rover RTKLIB did not write: it writes none outside the epochs it solved (before the first, after
      the last, in a gap).
    - Every mark of the file :func:`.rinex.gps_minus_utc` leap seconds from those of the rover: a rover file that
      writes its times in UTC, whose events RTKLIB reads as GPS time; said as that, and nothing else is checked.
    - Marks more than ``tolerance_m`` (metres, across or in height) from where :func:`at` puts the antenna at the time
      of the shot. In a backward solution RTKLIB places the marks from the epoch after them, and with a drone that
      speeds up or turns the two disagree by more than a millimetre: the warning says so.

    What it cannot see: a photo paired with the wrong event (one off, :func:`.shots.pair` checks that), and a shot
    where :func:`at` has no position (in a gap, between epochs more than :data:`MAX_GAP_S` apart: nothing to compare).
    Shots without a valid time are left out; nothing here is an error."""
    from . import events

    name = shotset.name or shotset.kind
    shots = [shot for shot in shotset.shots if shot.valid]
    if not shots:
        return []
    path = Path(solution.path)
    marks_path = path.with_name(path.stem + EVENTS_SUFFIX)
    if not marks_path.is_file():
        return [f"there are no time marks of RTKLIB beside the solution {path.name} ({marks_path.name}): the shots of "
                f"{name} were not checked against them"]
    try:
        marks = events.events_pos(marks_path).shots
    except NoPositions:
        marks = []
    except (ValueError, OSError) as exc:
        return [f"{marks_path.name} cannot be read ({exc}): the shots of {name} were not checked against it"]
    warnings = []
    given = None  # the time marks of the rover's observations: those RTKLIB was given
    rovers, notes = _rover_observations(solution)
    if notes:
        warnings.append(f"{'; '.join(notes)}: the shots of {name} are compared with the marks RTKLIB wrote in "
                        f"{marks_path.name} only")
    if rovers:
        try:
            given = np.array(sorted(s.gps_time for rover in rovers for s in events.rinex_events(rover).shots
                                    if s.valid), dtype=np.float64)
        except (ValueError, OSError):
            given = None
    written = np.array(sorted(m.gps_time for m in marks), dtype=np.float64)
    t = np.array([shot.gps_time for shot in shots], dtype=np.float64)
    if given is not None and len(given) and len(written):
        leap = rinex.gps_minus_utc(_GPS_EPOCH + timedelta(seconds=float(given[0])))
        shifted = any(np.all(_apart_s(given, written + sign * leap) <= MARK_TIME_S) for sign in (1, -1))
        if shifted and not np.all(_apart_s(given, written) <= MARK_TIME_S):
            return warnings + [
                f"every time mark RTKLIB wrote in {marks_path.name} is {leap} s from an event of the rover "
                f"{solution.rover}: its file writes its times in UTC, and RTKLIB writes the times of its events as "
                f"they are, without the {leap} leap seconds. The shots of {name} are in GPS time, at the right time "
                "in the solution: they were not checked against RTKLIB's marks"]
    reference = given if given is not None else written
    if not len(reference):
        where = f"the rover {solution.rover}" if given is not None else marks_path.name
        return warnings + [f"{where} has no time marks (events of the camera): the shots of {name} were not checked "
                           "against RTKLIB's"]
    apart = _apart_s(reference, t)
    off = apart > MARK_TIME_S
    if off.any():
        if given is not None:
            warnings.append(f"{int(off.sum())} of the {len(t)} shots of {name} are at no time mark of the rover "
                            f"{solution.rover}: up to {float(apart[off].max()):.3f} s from the nearest. The events "
                            "may be of another receiver or another flight, or their times shifted: check the photos")
        else:
            warnings.append(f"{int(off.sum())} of the {len(t)} shots of {name} are at no time mark RTKLIB wrote in "
                            f"{marks_path.name}: up to {float(apart[off].max()):.3f} s from the nearest. RTKLIB "
                            "writes no mark outside the epochs it solved, or the events may be of another receiver "
                            "or flight: check the photos")
    if given is not None and len(given):
        dropped = int((_apart_s(written, given) > MARK_TIME_S).sum())
        if dropped:
            warnings.append(f"RTKLIB wrote {len(given) - dropped} of the {len(given)} time marks of the rover in "
                            f"{marks_path.name}: {dropped} left out, outside the epochs it solved (before the first, "
                            "after the last, in a gap). Their shots take the position of the solution at their times "
                            "where it has one, not checked against RTKLIB's")
    placed = [m for m in marks if m.valid and m.antenna is not None]
    if placed:
        mark_times = np.array([m.gps_time for m in placed], dtype=np.float64)
        pairs = []
        for shot in shots:
            k = int(np.argmin(np.abs(mark_times - shot.gps_time)))
            if abs(mark_times[k] - shot.gps_time) <= MARK_TIME_S:
                pairs.append((shot, placed[k]))
        llh, _q, _sd, ok = at(solution, [shot.gps_time for shot, _ in pairs])
        gaps = [max(horizontal_m(mark.antenna[0], mark.antenna[1], lat, lon), abs(mark.antenna[2] - h))
                for (_shot, mark), (lat, lon, h), found in zip(pairs, llh, ok) if found]
        far = [gap for gap in gaps if gap > tolerance_m]
        if far:
            n = len(far)
            text = (f"{n} time mark{'s' if n > 1 else ''} of {marks_path.name} {'are' if n > 1 else 'is'} more than "
                    f"{tolerance_m * 1000:g} mm from where the solution {path.name} puts the antenna at the same "
                    f"time: up to {max(far) * 1000:.0f} mm, across or in height")
            if _solution_type(path) == "backward":
                text += (" (a backward solution: RTKLIB places the marks from the epoch after them, so with a drone "
                         "that speeds up or turns some millimetres are expected)")
            warnings.append(text)
    return warnings


# --- a run of RTKLIB --------------------------------------------------------------------------------------------


def work_folder(chunk) -> Path:
    """The work folder of the PPK runs of ``chunk``: ``gnss/ppk`` in the folder of its data, a folder of the program
    where every run writes its solution and the copies of its inputs (the ``folder`` of :func:`solve`)."""
    return Path(chunk.data_dir) / "gnss" / "ppk"


def last_run(chunk) -> dict | None:
    """The last PPK run that gave photos of ``chunk`` their positions (recorded by :func:`.apply.apply_ppk`): ``{"mrk",
    "photos", "rover", "base", "navs", "base_name", "position_source", "position", "frame", "epoch", "station",
    "replace_fix", "solution"}``, the files as absolute paths (None for those a solution read from a file does not
    know; ``rover`` and ``base`` each a path, or a list of paths when it came in several files), ``position_source``
    one of :data:`BASE_SOURCES`, ``position`` the typed one ``[lon, lat, h]`` (None for any other source), ``frame``
    and ``epoch`` those of the position of the base, ``station`` the permanent station of a base that is a station or
    was placed by a baseline from one (``{"id", "name", "networks", "lat", "lon", "h", "xyz"}``, None otherwise); a run
    recorded before the frames lacks the last three. A run with shots other than a DJI MRK (:func:`.apply.apply_ppk_shots`:
    the events of a receiver, an autopilot log) has ``mrk`` None and ``shots`` (``{"kind", "file", "by", "latency_s",
    "body_lever_mm", "confirmed"}``: the kind of the shots, their file as an absolute path, how the photos were paired,
    the latency added to the shots, the lever arm in the body of the drone or None, whether the times confirm the
    pairing, as :class:`.shots.Pairing` says). None when there was none."""
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


def _observations(path: Path, who: str) -> tuple[rinex.RinexInfo, tuple[datetime, datetime]]:
    """The header of an observation file and the time its epochs span (GPS time); a ``ValueError`` naming it as
    ``who`` ("the rover DJI_0001.obs") for a navigation file or a file without epochs."""
    info = rinex.header(path)
    if info.kind != "obs":
        raise ValueError(f"{who} holds navigation messages, not observations")
    span = rinex.time_span(path)
    if span is None:
        raise ValueError(f"{who} has no observation epochs")
    return info, span


def span_text(start: datetime | float, end: datetime | float) -> str:
    """The time from ``start`` to ``end`` of GPS time - datetimes, or seconds since the GPS epoch - written to the
    second: ``"2005-04-02 00:00:00 to 2005-04-02 00:59:30 GPS time"``."""
    def text(when: datetime | float) -> str:
        if not isinstance(when, datetime):
            when = _GPS_EPOCH + timedelta(seconds=float(when))
        return f"{when:%Y-%m-%d %H:%M:%S}"

    return f"{text(start)} to {text(end)} GPS time"


def navigation_info(path: str | Path, read=rinex.header, problems: list | None = None) -> rinex.RinexInfo | None:
    """The header of a navigation file, None when ``path`` is not one (or cannot be read); ``read`` reads a header, as
    :func:`rinex.header` (a caller can give one that keeps the headers it read). A file that cannot be read - cut
    short, damaged - is added to ``problems`` with its error, ``(path, error)``, for the caller to name it."""
    try:
        info = read(Path(path))
    except (ValueError, OSError) as exc:
        if problems is not None:
            problems.append((Path(path), exc))
        return None
    return info if info.kind == "nav" else None


def navigation_beside(paths: Iterable[str | Path],
                      problems: list | None = None) -> list[tuple[Path, rinex.RinexInfo]]:
    """The navigation files beside the observation files ``paths`` - their companions (:func:`rinex.find_companions`)
    that hold navigation messages (:func:`navigation_info`) - each once, in the order of ``paths``, with their
    headers; those that cannot be read go into ``problems`` (:func:`navigation_info`)."""
    found, seen = [], set()
    for obs in paths:
        for path in rinex.find_companions(obs):
            key = path.resolve()
            if key not in seen:
                seen.add(key)
                if info := navigation_info(path, problems=problems):
                    found.append((path, info))
    return found


def _navigation(navs, rovers: Sequence[Path], bases: Sequence[Path],
                roles: tuple[str, str] = RUN_ROLES[0],
                converted: Sequence[Path] = ()) -> list[tuple[Path, rinex.RinexInfo]]:
    """The navigation files of the run: those given (one or more; a ``ValueError`` for a file of observations), or
    those beside the files of the rover, or, when there are none, beside the files of the base
    (:func:`navigation_beside`); and, always and first, the navigation files ``converted`` from raw logs of the run
    (:func:`.convert.to_rinex`), the ephemerides of the flight itself, which RTKLIB merges with the others (a navigation
    file of another day beside a raw log would otherwise leave the run with no ephemeris); the receivers named by
    their ``roles`` in the messages."""
    whose = ("of the base or of the drone" if tuple(roles) == RUN_ROLES[0] else
             f"of {ROLES[roles[0]]} or of {ROLES[roles[1]]}")
    if isinstance(navs, (str, os.PathLike)):
        navs = [navs]
    own = [(Path(path), rinex.header(Path(path))) for path in converted]  # always: the ephemerides of the flight
    mine = {path.resolve() for path, _ in own}
    if navs:
        given = []
        for path in map(Path, navs):
            info = rinex.header(path)
            if info.kind != "nav":
                raise ValueError(f"{path.name} holds observations, not navigation messages: give the navigation file "
                                 f"{whose} (.nav, .##n, .##p, *_MN.rnx)")
            given.append((path, info))
        return own + [item for item in given if item[0].resolve() not in mine]
    problems: list = []
    found = navigation_beside(rovers, problems) or navigation_beside(bases, problems)
    if found or converted:
        return own + [item for item in found if item[0].resolve() not in mine]
    if problems:  # one is there and cannot be read: named, not "no navigation file"
        path, exc = problems[0]
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        raise ValueError(f"the navigation file {path.name} beside {ROLES[roles[0]]} {_names(rovers)} or "
                         f"{ROLES[roles[1]]} {_names(bases)} cannot be read{more}: {exc}. Download it again, or add "
                         f"another navigation file {whose} (.nav, .##n, .##g, .##p or *_MN.rnx)")
    raise ValueError(f"no navigation file beside {ROLES[roles[0]]} {_names(rovers)} or {ROLES[roles[1]]} "
                     f"{_names(bases)}: add the navigation file {whose} (.nav, .##n, .##g, .##p or *_MN.rnx)")


def base_name(path: str | Path, info: rinex.RinexInfo) -> str:
    """The name of a base: the marker of the header of its RINEX file ``path``, else the name of the file without its
    extension (and without .gz, .Z or .zip)."""
    return info.marker or Path(rinex_files.without_packing(Path(path).name)).stem


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


def same_antenna(entry: str, kind: str) -> bool:
    """The entry of an antenna file is the antenna ``kind`` of a header: the same type and the same radome, a radome the
    header omits being NONE (the first two words are compared, as RTKLIB's search uses no more)."""
    def words(text: str) -> tuple[str, str]:
        found = text.split()
        return (found[0] if found else ""), (found[1] if len(found) > 1 else "NONE")

    return words(entry) == words(kind)


def antenna_warning(name: str, info: rinex.RinexInfo, calibration: dict | None = None,
                    role: str = "base") -> str | None:
    """What to tell about the phase centre of the antenna of the receiver ``name``, whose RINEX header is ``info``, or
    None when there is nothing to tell. ``calibration`` is the record of what the antenna file gave it
    (:func:`antenna_calibration`: ``Solution.base["antenna_calibration"]``, ``Solution.rover_calibration``). ``role``
    says whose antenna it is - ``"base"``, the base of a PPK; ``"station"``, the permanent station of a static
    baseline; ``"your base"``, the user's base the baseline places; ``"ppp"``, the user's base placed by a static PPP -
    which the message names it by (:data:`ROLES`) and which way the error goes: an offset not applied puts what is
    found from a base or a station too low, and the mark of the user's base too high.

    Without a file of antenna calibrations (None) the offset of the phase centre of the antenna from its reference
    point is not applied, which every run says, naming the antenna of the header - for the base of a flight, with how
    to have the calibrations of the IGS (igs20.atx, downloaded once). With a file, that its antenna is
    not in it, so RTKLIB applies nothing for it, or that the antenna of the header is not the entry RTKLIB uses - it
    took the type without the radome, or a name that holds the header's - naming the entry; nothing when the entry
    is the antenna of the header."""
    if role not in _ANTENNA_EFFECTS:
        raise ValueError(f"the antenna of a receiver in the role {role!r}: the roles are {', '.join(_ANTENNA_EFFECTS)}")
    effect, other = _ANTENNA_EFFECTS[role]
    kind = antenna_type(info)
    who = f"{ROLES[role]} {name}"
    which = (f"of the antenna {kind} of {who} is not applied" if kind else
             f"of the antenna of {who} is not applied (its RINEX header names no antenna type)")
    if calibration is None:
        text = (f"the phase-centre offset {which}: no antenna calibrations were given to RTKLIB, so {effect}, more "
                "than 10 cm with some choke-ring antennas")
        if role == "base":  # the base of the PPK of a flight: how to have them
            text += (". The calibrations of the IGS (igs20.atx, 62 MB) are downloaded once by the PPK window, with its "
                     "option for the antenna of the base, or by `ppg gnss atx --download`")
        return text
    file, entry = calibration["file"], calibration["entry"]
    if entry is None and calibration.get("no_l1"):
        return (f"the phase-centre offset {which}: its entry {' '.join(calibration['no_l1'].split())} in {file} has no "
                f"offsets of L1 (G01), so RTKLIB has no calibration for it, and {effect}, more than 10 cm with some "
                "choke-ring antennas")
    if entry is None:
        why = (f"the antenna is not in {file}, so RTKLIB has no calibration for it" if kind else
               f"there is no antenna to look for in {file}, so RTKLIB has no calibration for it")
        return f"the phase-centre offset {which}: {why}, and {effect}, more than 10 cm with some choke-ring antennas"
    used = " ".join(entry.split())
    if calibration["without_radome"]:
        return (f"the antenna {kind} of {who} is not in {file} with its radome: RTKLIB used the calibration of {used}, "
                f"the same antenna type with another radome, so {other}")
    if not same_antenna(entry, kind):
        return (f"the antenna {kind} of {who} is not in {file} as its RINEX header writes it: RTKLIB used the "
                f"calibration of {used}, the nearest name it found, so {other}, and more if it is another antenna")
    return None


def _atx_source(atx) -> tuple[Path | None, list[str], bool]:
    """The antenna file a run is given, from the ``atx`` of :func:`solve`, a note for each reason that the one the
    program keeps cannot be used, and whether it is that one (the program's own file, which a run does without when it
    cannot be used; one named by the user is refused)."""
    if atx is None:
        return None, [], False
    if isinstance(atx, str) and atx == "auto":
        kept = antex.cached_atx()
        if kept is None:
            return None, [], True
        try:
            antex.validate(kept)
        except (ValueError, OSError) as exc:
            return None, [f"the antenna calibrations the program keeps cannot be used, and the run goes on without "
                          f"them: {exc}"], True
        return kept, [], True
    path = Path(atx)
    if not path.is_file():
        raise ValueError(f"the antenna calibrations {path} are not a file: give an ANTEX file (.atx), such as "
                         "igs20.atx")
    antex.validate(path)
    return path, [], False


def satellite_entry(source: Path | None, kind: str) -> str | None:
    """The antenna of a satellite that RTKLIB's search finds in the antenna file ``source`` for the receiver antenna
    ``kind`` - RTKLIB does not tell a satellite's antenna from a receiver's: "BLOCK IIA" is found by a header that holds
    those words - or None when it finds a receiver's antenna or none (or there is no file)."""
    if source is None:
        return None
    entry = antex.lookup(source, kind)[0]
    return entry if entry is not None and entry not in antex.receiver_antennas(source) else None


def antenna_calibration(source: Path | None, kind: str) -> dict | None:
    """The record of what the antenna file ``source`` gives the antenna ``kind`` - its type and radome as a RINEX
    header writes them, of the base (``Solution.base["antenna_calibration"]``) or of the rover
    (``Solution.rover_calibration``): ``{"file": its name, "entry": the entry RTKLIB takes (None: the antenna is not in
    it, RTKLIB applies nothing), "without_radome": the entry is the type without the radome of the header, "up_m": the
    offset of the phase centre of L1 above the reference point of the antenna in the entry, m}``; None without a
    file. An antenna for which RTKLIB's search finds a satellite's (:func:`satellite_entry`) has no entry, and neither
    has one whose entry gives no offsets of L1 (G01: RTKLIB has nothing to apply to L1), which the record names as
    ``"no_l1"`` for the warning (:func:`antenna_warning`)."""
    if source is None:
        return None
    entry, without_radome = antex.lookup(source, kind)
    if satellite_entry(source, kind):
        # a satellite's offsets are no receiver's: such an entry is no calibration, and the file is not given to
        # RTKLIB, which would apply it
        entry, without_radome = None, False
    up = antex.up_offset(source, entry) if entry is not None else None
    if entry is not None and up is None:  # no G01: not calibrated, said with the entry
        return {"file": source.name, "entry": None, "without_radome": False, "up_m": None, "no_l1": entry}
    return {"file": source.name, "entry": entry, "without_radome": without_radome, "up_m": up}


def _base(path: Path, info: rinex.RinexInfo, position: BasePosition | Sequence[float] | None,
          noun: str = "the base") -> tuple[dict, str | None, tuple[float, float, float]]:
    """The base of the solution: at ``position`` (a :class:`BasePosition`; three numbers are typed, in a frame nobody
    knows) or, with None, at the position of its header; its record (``position`` ``[lon, lat, h]``, where it comes
    from, its frame, epoch, deviations and detail), the warning for a position from the header or for a typed one far
    from it (:func:`far_from_header`), which names it by ``noun`` ("the station"), and the position as RTKLIB is given
    it (latitude, longitude, height)."""
    record = {"name": base_name(path, info), "position": None, "position_source": None, "antenna": antenna_type(info),
              "antenna_delta_hen": None if info.antenna_delta_hen is None else list(info.antenna_delta_hen),
              "file": path.name}
    if position is None:
        if info.approx_xyz is None:
            raise ValueError(f"the base {path.name} has no position in its RINEX header: type the position of the mark "
                             "it stands on (latitude, longitude, height above the ellipsoid)")
        position = BasePosition(*rinex.ecef_to_llh(info.approx_xyz), "header")
    elif not isinstance(position, BasePosition):
        position = BasePosition(*_base_position(position), "typed")
    lat, lon, h = position.lat, position.lon, position.h
    warning = None
    if position.source == "header":
        warning = (f"the position of {noun} {record['name']} comes from its RINEX header (latitude {lat:.7f}, "
                   f"longitude {lon:.7f}, {h:.2f} m): it can be metres off, and the whole survey moves with it. Type "
                   f"the surveyed position of {noun} for an accurate absolute position")
    elif position.source == "typed":
        offset = offset_from_header((lat, lon, h), info)
        if far_from_header(offset):
            apart = f"{offset[0] / 1000.0:.1f} km" if offset[0] >= 1000.0 else f"{offset[0]:.0f} m"
            warning = (f"the typed position of {noun} {record['name']} is {apart} away horizontally and "
                       f"{offset[1]:+.1f} m in height from the position of its RINEX header: check that latitude and "
                       "longitude are not swapped or without their sign, and that the height is above the ellipsoid, "
                       "not above the sea level")
    record.update(position=[lon, lat, h], position_source=position.source, frame=position.frame,
                  epoch=position.epoch, sd=None if position.sd is None else list(position.sd),
                  detail=copy.deepcopy(position.detail))
    return record, warning, (lat, lon, h)


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


def _files(given: str | os.PathLike | Sequence[str | os.PathLike], noun: str) -> list[Path]:
    """The files of a receiver, ``noun`` ("the base"): ``given`` is one file or several; a ``ValueError`` when it is
    none."""
    if isinstance(given, (str, os.PathLike)):
        return [Path(given)]
    files = [Path(path) for path in given]
    if not files:
        raise ValueError(f"no file given for {noun}")
    return files


def _one_file(pieces: list[Path], joined: Path) -> tuple[Path, list[tuple[datetime, datetime]]]:
    """The file RTKLIB is given a copy of for the files ``pieces`` of a receiver: the one file, or the file ``joined``
    they make (:func:`rinex.join`), with the holes (GPS time) between their epochs."""
    if len(pieces) == 1:
        return pieces[0], []
    _info, holes = rinex.join(pieces, joined)
    return joined, holes


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


@dataclass(frozen=True)
class RunFiles:
    """The files of a run of RTKLIB in its work folder (:func:`new_run`): ``folder`` the work folder as the program
    names it; ``copies`` the folder RTKLIB reads its inputs from (:data:`INPUTS_FOLDER`: nothing in it but the copies
    of a run, emptied when the run starts and removed after it); ``conf``, ``out`` and ``events`` the options, the
    solution and the time marks of the rover, under a name of the run's own (:func:`_run_files`). The paths are those
    RTKLIB is given (:func:`_rtklib_folder`)."""

    folder: Path
    copies: Path
    conf: Path
    out: Path
    events: Path


def new_run(folder: str | Path, sources: Iterable[str | Path], what: str = "PPK") -> RunFiles:
    """The files of a new run of RTKLIB - the ``what`` ("PPK", "PPP") - in the work folder ``folder``, a folder of the
    program, made when missing. A ``ValueError``, before anything is made, when one of ``sources`` - the files of the
    user the run reads - is in it, or in a folder inside it: the program writes and deletes files in there. A
    ``RuntimeError`` when RTKLIB could not open files in it (:func:`_rtklib_folder`)."""
    folder = Path(folder)
    for source in sources:
        if inside(source, folder):
            raise ValueError(f"{source} is in {folder}, the work folder of the {what}, where the program writes and "
                             f"deletes files: move it out of {folder} first")
    folder.mkdir(parents=True, exist_ok=True)
    work = Path(_rtklib_folder(folder))
    conf, out, events = _run_files(work)
    return RunFiles(folder, work / INPUTS_FOLDER, conf, out, events)


def copier(source: str | Path, compressed: str | None) -> Callable[[Path], None]:
    """What writes the file ``source``, packed as ``compressed`` says (``RinexInfo.compressed``, or the layers of a
    product), among the copies of a run (:func:`run_rtklib`): as plain text, a gzip file decompressed a piece at a
    time, a file under other layers from its decompressed copy (:func:`rinex.plain`)."""
    return lambda target: _copy(Path(source), target, compressed)


def mover(source: str | Path) -> Callable[[Path], None]:
    """What puts ``source`` - a file the program made for the run, the pieces of a receiver joined in its work folder -
    among the copies of a run (:func:`run_rtklib`): moved there, on the same disk, rather than copied once more."""
    return lambda target: os.replace(source, target)


def _no_positions(message: str) -> str:
    return f"nothing was computed: {message or 'RTKLIB wrote no positions'}"


def run_rtklib(files: RunFiles, options: Callable[[], str], inputs: Sequence[tuple[Path, Callable[[Path], object]]],
               others: Sequence[tuple[Path, Callable[[Path], object]]] = (),
               nothing: Callable[[str], str] = _no_positions) -> tuple[Solution, str]:
    """RTKLIB run by the core in the work folder of ``files`` (:func:`new_run`) on copies of its inputs, under names of
    their own in ``files.copies``: RTKLIB never sees the names of the user's files (it expands "*" and "%r" in them,
    and decompresses some extensions with a shell command). ``inputs`` are the files RTKLIB is given, in their order,
    and ``others`` the files its options name (an antenna file, an ERP): each the path of its copy and what writes it
    there (``write(path)``, as :func:`copier` makes it).

    First the paths RTKLIB would be given are checked (:func:`_short_enough`) and then ``options()`` gives the text of
    its options file, so either can refuse the run before anything is written; the options are written in the encoding
    of the system's paths, as RTKLIB opens them. The copies are removed after the run, whatever happens. A
    ``RuntimeError`` when RTKLIB stopped, a ``ValueError`` (``nothing(message)``) when it wrote no positions, and a run
    that gives no solution leaves nothing. The solution (:func:`read_pos`, its ``path`` in the work folder as the
    program names it) and RTKLIB's last message."""
    staged = [*inputs, *others]
    _short_enough([files.conf, files.out, files.events, *(target for target, _ in staged)])
    text = options()
    try:
        files.conf.write_bytes(os.fsencode(text))
        try:
            _fresh_folder(files.copies)
            for target, write in staged:
                write(target)
            code, message = _native.ppk_solve(str(files.conf), [str(target) for target, _ in inputs], str(files.out))
        finally:
            shutil.rmtree(files.copies, ignore_errors=True)
        if code != 0:
            raise RuntimeError(f"RTKLIB stopped with code {code}: {message or 'no message'}")
        if not files.out.exists():
            raise ValueError(nothing(message))
        try:
            solution = read_pos(files.out)
        except NoPositions:
            raise ValueError(nothing(message)) from None
    except BaseException:
        _remove(files.conf, files.out, files.events)
        raise
    solution.path = files.folder.resolve() / files.out.name
    return solution, message


# --- raw receiver logs ------------------------------------------------------------------------------------------


def gps_span(path: str | Path) -> tuple[float, float]:
    """The first and last epoch of the RINEX observations ``path`` in seconds of GPS time since the GPS epoch, from its
    epochs, converted from the time system of the file as its events are (:func:`.rinex.gps_ahead`: UTC and GLONASS
    time less the leap seconds of its header or of the day, BeiDou time less 14 s; :func:`.rinex.time_span` leaves
    UTC as it is). A ``ValueError`` for a file without epochs."""
    path = Path(path)
    info, _systems, leap, source = rinex._read_header(path)
    if info.kind != "obs":
        raise ValueError(f"{path.name}: a navigation file has no observation epochs")
    first = rinex._first_epoch(source, info.version)
    if first is None:
        raise ValueError(f"{path.name} has no observation epochs")
    last = rinex._last_epoch(source, info.version) or first
    return tuple((when - _GPS_EPOCH).total_seconds() + rinex.gps_ahead(info.time_system, when, leap)
                 for when in (first, last))


def _approx_span(path: Path) -> tuple[float, float]:
    """The time of the RINEX observations ``path``, GPS seconds, for an approximate time of a day: the TIME OF FIRST
    OBS and TIME OF LAST OBS of its header when it has both (nothing else read: a day of a station is gigabytes),
    else its epochs (:func:`gps_span`)."""
    info = rinex.header(path)
    if info.first is not None and info.last is not None:
        ahead = rinex.gps_ahead("UTC", info.first) if info.time_system == "UTC" else 0  # GLO, BDT converted already
        return tuple((when - _GPS_EPOCH).total_seconds() + ahead for when in (info.first, info.last))
    return gps_span(path)


def _raw(path: Path) -> bool:
    """Whether ``path`` is a raw receiver log to convert (:func:`.convert.raw_kind`, or a D-RTK ``.DAT``, which
    :func:`.convert.to_rinex` checks); anything else - RINEX, a file that cannot be read - goes on as it did."""
    from . import convert

    if not path.is_file():
        return False
    if path.suffix.lower() == ".dat":
        return True
    try:
        return convert.raw_kind(path) not in (None, "rinex")
    except OSError:
        return False


def _middle(paths: Sequence[Path]) -> float:
    """The middle of the time the RINEX observations ``paths`` span together, GPS seconds (:func:`_approx_span`)."""
    spans = [_approx_span(path) for path in paths]
    return (min(a for a, _ in spans) + max(b for _, b in spans)) / 2.0


def _converted(rovers: list[Path], bases: list[Path], roles: tuple[str, str], progress=None,
               approx_time: float | datetime | None = None) -> tuple[list[Path], list[Path], list]:
    """The RINEX files of the rover and of the base, one for each of ``rovers`` and ``bases``: a raw log converted
    (:func:`.convert.to_rinex`), any other file as it is; and the conversions (``Converted``), the rover's first. A log
    that carries the time of the week only (RTCM 3) is placed by ``approx_time`` when given, else by the middle of the
    time of the other receiver: of the rover for the base, of a base in RINEX for the rover (a ``ValueError`` naming
    ``approx_time`` when there is none)."""
    raw_rovers, raw_bases = [_raw(path) for path in rovers], [_raw(path) for path in bases]
    if not any(raw_rovers) and not any(raw_bases):
        return list(rovers), list(bases), []
    from . import convert

    rover_noun, base_noun = ROLES[roles[0]], ROLES[roles[1]]
    converted = []

    def convert_one(path: Path, approx: float | None, whose: str) -> Path:
        if approx_time is not None and _needs_time(path):
            approx, whose = convert._gps_seconds(approx_time), "approx_time, as given"
        try:
            item = convert.to_rinex(path, approx_time=approx, progress=progress)
        except ValueError as exc:
            if approx is None:
                raise
            when = span_text(approx, approx)[:19]
            source = (whose if approx_time is not None else f"the middle of the time of {whose}")
            raise ValueError(f"{exc} (the approximate time of the log, {when} GPS time, was taken from {source}: the "
                             "log has to be of the same day)") from None
        converted.append(item)
        return item.obs

    rover_approx = None
    if any(raw_rovers):
        plain_bases = [path for path, raw in zip(bases, raw_bases) if not raw]
        if plain_bases:
            rover_approx = _middle(plain_bases)
        elif approx_time is None and any(_needs_time(path) for path, raw in zip(rovers, raw_rovers) if raw):
            raise ValueError(f"{rover_noun} {_names(rovers)} is a log that carries the time of the week only (RTCM 3), "
                             f"and {base_noun} {_names(bases)} has no RINEX file to take the day from: give the "
                             "approximate time of the flight (approx_time, the day is enough)")
    rover_files = [convert_one(path, rover_approx if _needs_time(path) else None, f"{base_noun} {_names(bases)}")
                   if raw else path for path, raw in zip(rovers, raw_rovers)]
    base_approx = _middle(rover_files) if any(raw_bases) else None
    base_files = [convert_one(path, base_approx if _needs_time(path) else None, f"{rover_noun} {_names(rovers)}")
                  if raw else path for path, raw in zip(bases, raw_bases)]
    return rover_files, base_files, converted


def _needs_time(path: Path) -> bool:
    """Whether the raw log ``path`` carries the time of the week only (RTCM 3, a D-RTK ``.DAT``)."""
    from . import convert

    if path.suffix.lower() == ".dat":
        return True
    try:
        return convert.raw_kind(path) in _native.RAW_NEED_TIME
    except OSError:
        return False


def solve(rover: str | Path | Sequence[str | Path], base: str | Path | Sequence[str | Path], navs=None,
          base_position: BasePosition | Sequence[float] | None = None, *, folder: str | Path,
          atx: str | Path | None = "auto", mode: str = "kinematic", rover_antenna: str = "",
          solution: str = "combined", static_single: bool = False,
          roles: tuple[str, str] = RUN_ROLES[0], progress=None,
          approx_time: float | datetime | None = None) -> Solution:
    """The PPK solution of the antenna of ``rover`` (its RINEX observations: one file, or several that
    :func:`rinex.join` joins, whose holes ``Solution.rover_holes`` gives) against ``base`` (the RINEX observations of a
    base: one file that covers the whole flight, or several that make it - pieces of an hour, say - which
    :func:`rinex.join` joins in the order of their time, in any order given; a hole between them inside the time of
    the rover is a warning), with the navigation files ``navs`` (one or more; None: those beside the files of the
    rover, or beside those of the base when the rover has none, as a DJI drone does not always write one,
    :func:`navigation_beside`). Any file may be packed: gzip, the Unix
    compress, a zip archive of one file, Hatanaka (:func:`rinex.plain`). When the epochs of the base are further apart
    than those of the rover (by their ``INTERVAL`` or, when a header does not say, by what their first epochs show,
    :func:`rinex.epoch_interval`) RTKLIB interpolates the base to the epochs of the rover. The base
    at ``base_position``, the mark it stands on: a :class:`BasePosition` (where it comes from, its reference frame and
    the epoch of its coordinates, which ``Solution.base`` keeps as ``frame``, ``epoch``, ``sd`` and ``detail``: the
    positions of the solution are in that frame), or three numbers typed by the user - latitude, longitude, height
    above the ellipsoid - in a frame nobody knows (``"unknown"``); with None at the position of its RINEX header, with
    a warning: that one can be metres off; a typed one far from the header's (:func:`far_from_header`) has a warning
    too. Either way RTKLIB adds the antenna height of the base's header, recorded in
    ``Solution.base["antenna_delta_hen"]``.

    The offset of the phase centre of the base's antenna from its reference point is applied when RTKLIB is given
    the calibrations of the antennas: ``atx`` is an ANTEX file (``.atx``), or None for none, or "auto" (the default)
    for the copy of igs20.atx the program keeps (:func:`.atx.cached_atx`; never downloaded here, and left out with a
    note when it is not usable). RTKLIB finds the antenna of the base in it by the name in the header of the RINEX
    file, as :func:`.atx.lookup` does, and ``Solution.base["antenna_calibration"]`` says which entry it took; the
    antenna not being in the file, or not as the header writes it, is a warning (:func:`antenna_warning`), as is,
    without a file, that the offset is not applied and every height is that much too low.
    ``Solution.base["position"]`` is ``[lon, lat, h]``.

    The run is the rover moving with the forward and backward solutions combined, or what ``mode``, ``rover_antenna``,
    ``solution`` and ``static_single`` make of it, as in :func:`config`: a receiver standing still on a mark, solved
    forward into one position for the whole session, its antenna and the antenna's height above the mark from its
    header, say (the user's own base placed by a static baseline, :func:`.base.baseline`). With an antenna of the rover
    the file of antenna calibrations is given to RTKLIB when either antenna is in it, and never when RTKLIB's search
    would find a satellite's antenna for one of them (a note says so); ``Solution.rover_calibration`` says which entry
    RTKLIB took for the rover's antenna, for the caller to tell. Options RTKLIB cannot take are a ``ValueError``
    before any file is read or written. ``roles`` are the roles of the rover and the base (:data:`RUN_ROLES`): the
    rover and the base of a flight, or ``("your base", "station")`` for a static baseline, which the messages and
    warnings name them by and which tell which way an antenna of the base that is not calibrated moves the result
    (:func:`antenna_warning`).

    Before RTKLIB runs, a ``ValueError`` when the rover and the
    base have no time in common, when there is no navigation file, when the base has no position, when the files of
    the rover or of the base are not of one receiver (:func:`rinex.join`), when an input is in ``folder`` or in a
    folder inside it, when a path RTKLIB would be given is too long (:data:`MAX_PATH_BYTES`) or has a "#"
    (:func:`path_problem`), or when the file named by ``atx`` is not an ANTEX file.

    RTKLIB works in ``folder`` (made when missing; a folder of the program) on copies of the files with plain names in
    its subfolder ``INPUTS_FOLDER``, which holds nothing else but, when a file of antenna calibrations is given, the
    copy of it (``ANTENNA_COPY``); every layer is taken off the copies of the inputs (a rover or a base in several
    files is one file there), and all are removed after the run (:func:`new_run`, :func:`run_rtklib`): it never sees
    the user's names, and never a packed file (it would run a shell command for it). Every run writes its options and
    its solution under a name of its own
    (``solution-<YYYYmmdd-HHMMSS>.conf`` and ``.pos``), and the solutions of earlier runs stay; a run that gives no
    solution leaves nothing. A ``ValueError`` when RTKLIB computed nothing (with its message), a ``RuntimeError`` when
    it stopped; a message of RTKLIB that is an error but came with positions is a warning of the solution.

    A file of the rover or of the base may also be a raw receiver log (u-blox, RTCM 3, the ``.DAT`` of a DJI D-RTK
    2/3: :func:`.convert.raw_kind`), converted to RINEX first (:func:`.convert.to_rinex`, into its cache, ``progress``
    following it and able to cancel it), the rover first: an RTCM 3 log of the base, which carries the time of the
    week only, is placed by the middle of the time of the rover (:func:`gps_span`, GPS time), and one of the rover by
    the middle of the time of a base in RINEX - or by ``approx_time`` (GPS seconds since the GPS epoch, or a datetime:
    naive in GPS time, aware converted from UTC, as :func:`.convert.to_rinex` takes it) when given, which it needs
    when no RINEX file gives the day (an RTCM 3 rover and a raw base); a log that is not of that day is a ``ValueError`` saying where its
    approximate time came from. ``Solution.inputs`` keeps the files as they were given, and ``"converted"`` (only when
    something was converted) lists the conversions, ``{"source", "kind", "obs", "nav", "epochs", "events"}`` (paths
    absolute; ``nav`` None when the log gave none); the navigation files of the conversions are those of the run when
    none is given or found beside the files of the rover or of the base, and the warnings of the conversions are
    among those of the solution. The time marks of the rover (the events of the camera) come through the conversion:
    :func:`events_check` checks the shots against the marks RTKLIB writes."""
    run = {"mode": mode, "rover_antenna": rover_antenna, "solution": solution, "static_single": static_single}
    config(None, **run)  # what RTKLIB cannot take is refused before anything is read or written
    if tuple(roles) not in RUN_ROLES:
        raise ValueError(f"the roles of the rover and the base of a run are {' or '.join(map(str, RUN_ROLES))}, not "
                         f"{roles!r}")
    roles = tuple(roles)
    rovers, bases = _files(rover, ROLES[roles[0]]), _files(base, ROLES[roles[1]])
    rover_pieces, base_pieces, converted = _converted(rovers, bases, roles, progress, approx_time)
    if len(rovers) == 1 and len(bases) == 1:  # nothing to join: nothing is made before the checks
        return _solve(rovers, rover_pieces[0], [], bases, base_pieces[0], [], navs, base_position, folder, atx, run,
                      roles, base_pieces, converted)
    # files in pieces joined in the work folder, on its disk (a day of a station at 1 s is gigabytes), and moved among
    # the inputs of the run (mover): no copy in the temporary folder of the system, no second copy. The folders made
    # for it go again when the run is refused and nothing else came into them
    folder = Path(folder)
    made = [path for path in (folder, *folder.parents) if not path.exists()]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".join-", dir=folder) as scratch:
            rover_file, rover_holes = _one_file(rover_pieces, Path(scratch) / "rover.obs")
            base_file, base_holes = _one_file(base_pieces, Path(scratch) / "base.obs")
            return _solve(rovers, rover_file, rover_holes, bases, base_file, base_holes, navs, base_position, folder,
                          atx, run, roles, base_pieces, converted)
    except BaseException:
        for path in made:  # the deepest first
            try:
                path.rmdir()
            except OSError:  # something of an earlier step, or of the user, is in it
                break
        raise


def _solve(rovers: list[Path], rover: Path, rover_holes: list[tuple[datetime, datetime]], bases: list[Path],
           base: Path, base_holes: list[tuple[datetime, datetime]], navs,
           base_position: BasePosition | Sequence[float] | None, folder: str | Path, atx, run: dict,
           roles: tuple[str, str], base_pieces: Sequence[Path] | None = None, converted: Sequence = ()) -> Solution:
    """:func:`solve` with the files of the rover and of the base as the user gave them (``rovers``, ``bases``) and the
    files that RTKLIB is given copies of (``rover``, ``base``): the one file of each, or the file its pieces make
    joined, with the holes (GPS time) between their epochs. ``run`` holds the options of :func:`config` that are not
    the base's (mode, rover_antenna, solution, static_single), ``roles`` the roles of the rover and of the base;
    ``base_pieces`` the RINEX files of the base, one for each of ``bases`` (``bases`` themselves by default), and
    ``converted`` the raw logs among the files, converted to RINEX (:func:`_converted`)."""
    base_pieces = list(bases) if base_pieces is None else list(base_pieces)
    rover_noun, base_noun = ROLES[roles[0]], ROLES[roles[1]]
    rover_names, names = _names(rovers), _names(bases)
    rover_info, rover_span = _observations(rover, f"{rover_noun} {rover_names}")
    base_info, base_span = _observations(base, f"{base_noun} {names}")
    warnings = [f"{name} writes its times in {info.time_system}, which is not a time system of RINEX: they are "
                f"read as GPS time, so the time {rover_noun} and {base_noun} have in common may be off"
                for name, info in ((rover_names, rover_info), (names, base_info))
                if info.time_system not in _RINEX_TIME_SYSTEMS]
    if max(rover_span[0], base_span[0]) > min(rover_span[1], base_span[1]):
        raise ValueError(f"{rover_noun} {rover_names} ({span_text(*rover_span)}) and {base_noun} {names} "
                         f"({span_text(*base_span)}) have no time in common: {base_noun} has to record while "
                         f"{rover_noun} does")
    warnings += [f"{base_noun} has no observations from {span_text(start, end)}, while {rover_noun} records: the "
                 f"positions there are single (from {rover_noun} alone) at best" for start, end in base_holes
                 if start < rover_span[1] and end > rover_span[0]]
    warnings += [warning for item in converted for warning in item.warnings]
    nav_files = _navigation(navs, rovers, bases, roles, [item.nav for item in converted if item.nav is not None])
    base_record, base_warning, mark = _base(bases[0], base_info, base_position, base_noun)
    base_record["file"] = names
    if base_warning:
        warnings.append(base_warning)
    antenna_source, antenna_notes, automatic = _atx_source(atx)
    # a base whose epochs are further apart than the rover's: RTKLIB interpolates it to the epochs of the rover. The
    # intervals are those of the headers - which RINEX leaves optional - or what the first epochs show; a base in
    # several files is as far apart as its closest file
    rover_step = rinex.epoch_interval(rover, rover_info)
    steps = [rinex.epoch_interval(piece, base_info if len(bases) == 1 else None) for piece in base_pieces]
    base_step = min((step for step in steps if step is not None), default=None)
    interpolate = rover_step is not None and base_step is not None and base_step > rover_step + 1e-6

    files = new_run(folder, (*rovers, *bases, *(path for path, _ in nav_files),
                             *([antenna_source] if antenna_source else [])))
    # RTKLIB matches the names of its inputs ignoring the case: nothing in there but them and the antenna file
    copies = files.copies
    inputs = [(copies / "rover.obs", mover(rover) if len(rovers) > 1 else copier(rover, rover_info.compressed)),
              (copies / "base.obs", mover(base) if len(bases) > 1 else copier(base, base_info.compressed))]
    inputs += [(copies / f"nav{i}.nav", copier(path, info.compressed)) for i, (path, info) in enumerate(nav_files, 1)]
    # the file is given to RTKLIB when the antenna of the base, or the one of the rover it is told to look for, is in
    # it (without, RTKLIB would apply nothing, and the copy of a file of 60 MB would be for nothing): a copy among the
    # others, its path in the options, which RTKLIB has to read back as it is
    calibration = antenna_calibration(antenna_source, antenna_type(base_info))
    rover_kind = None  # the antenna of the rover RTKLIB looks for: the one of its header ("*"), or the one named
    if run["rover_antenna"]:
        rover_kind = antenna_type(rover_info) if run["rover_antenna"] == "*" else " ".join(run["rover_antenna"].split())
    rover_calibration = None if rover_kind is None else antenna_calibration(antenna_source, rover_kind)
    found = any(record is not None and record["entry"] is not None for record in (calibration, rover_calibration))
    searched = ((f"{base_noun} {base_record['name']}", antenna_type(base_info)),
                (f"{rover_noun} {base_name(rovers[0], rover_info)}", rover_kind))
    satellites = [(who, kind, entry) for who, kind in searched
                  if kind is not None and (entry := satellite_entry(antenna_source, kind))]
    if found and satellites:  # RTKLIB would apply the offsets of a satellite's antenna to a receiver
        who, kind, entry = satellites[0]
        antenna_notes.append(f"the antenna calibrations of {antenna_source.name} are left out of the run: for the "
                             f"antenna {kind} of {who} RTKLIB's search finds {' '.join(entry.split())}, the antenna of "
                             "a satellite, whose offsets are no receiver's")
        calibration, rover_calibration, found = None, None, False
    antenna_copy = copies / ANTENNA_COPY if found else None
    problem = path_problem(str(antenna_copy)) if antenna_copy else None
    if problem and not automatic:
        raise ValueError(problem)
    if problem:  # the file the program keeps: the run is a good run without it
        antenna_notes.append(f"the antenna calibrations of {antenna_source.name} are left out of the run: {problem}")
        calibration, rover_calibration, antenna_copy = None, None, None
    warnings += antenna_notes
    note = antenna_warning(base_record["name"], base_info, calibration, role=roles[1])
    if note:
        warnings.append(note)
    base_record["antenna_calibration"] = calibration
    others = [(antenna_copy, lambda target: shutil.copyfile(antenna_source, target))] if antenna_copy else []
    # always the position, also the one of the header: RTKLIB 2.4.3 adds the antenna height twice with rinexhead
    solution, message = run_rtklib(
        files, lambda: config(mark, interpolate_base=interpolate,
                              antenna_file=str(antenna_copy) if antenna_copy else None, **run), inputs, others)
    if message.lower().startswith("error"):
        warnings.append(f"RTKLIB: {message}")
    solution.base = base_record
    solution.rover = rover_names
    solution.inputs = {"rover": str(rovers[0].absolute()) if len(rovers) == 1 else [str(p.absolute()) for p in rovers],
                       "base": str(bases[0].absolute()) if len(bases) == 1 else [str(p.absolute()) for p in bases],
                       "navs": [str(path.absolute()) for path, _ in nav_files]}
    if converted:
        solution.inputs["converted"] = [
            {"source": str(item.source.absolute()), "kind": item.kind, "obs": str(item.obs.absolute()),
             "nav": str(item.nav.absolute()) if item.nav is not None else None, "epochs": item.epochs,
             "events": item.events} for item in converted]
    solution.rover_calibration = rover_calibration
    solution.rover_holes = list(rover_holes)
    solution.warnings = warnings + solution.warnings
    return solution
