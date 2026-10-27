"""The base of a PPK from a permanent station of the EPN, the IGS or RING (:mod:`.stations`), in one of two ways.

The station itself can be the base of the flight (:func:`station_base`): its observations for the hours of the flight
and its coordinates in ITRF2020 at the middle of the flight, which the EPN, the IGS and RING give to millimetres. Or
the user's own base - a receiver left on a mark that nobody surveyed - is placed by a static baseline from a station
(:func:`baseline`): RTKLIB solves the base as a rover standing still for the whole session against the station, and
the mark it finds, in the frame and at the epoch of the coordinates of the station, is the position of the base for
the PPK of the flight.

Positions are latitude, longitude (degrees) and height above the ellipsoid (m), as :class:`.ppk.BasePosition` keeps
them; times are GPS time without a time zone, as :func:`.rinex.header` gives them."""
from __future__ import annotations

import logging
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from ..core.progress import as_progress
from . import atx as antex
from . import download, frames, ppk, rinex, stations

log = logging.getLogger(__name__)

MIN_HOURS = 1.0  # a static baseline of a shorter session may not fix its ambiguities, or fix them wrong
GAP_WARNING_S = 60.0  # a hole in the observations of the user's base longer than this during the session is told of
FIX_OR_FLOAT = (1, 2)  # RTKLIB's Q of a static baseline that places a base: any other is a position of the code alone
# what the sigma of the coordinates of a station is, by where they come from: the formal error of a multi-year solution
# (EPN, IGS), the scatter of the daily positions about their line (RING)
SIGMA_KINDS = {"EPN": "formal", "IGS": "formal", "RING": "scatter"}
# in the folder of a baseline: what is downloaded for it, and the work folder of RTKLIB (ppk.solve refuses inputs in
# the latter, so the two are side by side)
STATION_FOLDER = "station"
WORK_FOLDER = "baseline"
ROLES = ("your base", "station")  # how the run of a baseline names its two receivers (ppk.RUN_ROLES)


@dataclass
class BaselineResult:
    """What a static baseline found for the user's own base (:func:`baseline`). ``position`` is the mark under its
    antenna, a :class:`.ppk.BasePosition` of source ``"baseline"`` in the frame and at the epoch of the coordinates of
    the station, with RTKLIB's standard deviations; ``q`` RTKLIB's quality of the solution (1 fix, 2 float:
    :data:`.ppk.QUALITY`), ``sd`` its standard deviations east, north, up (m), ``baseline_km`` the distance from the
    station to the mark across, as :func:`.ppk.baseline_km` measures it, ``hours`` how long the base and the station
    observed together, less the holes in the observations of the base, ``solution`` the file of RTKLIB's solution,
    ``warnings`` what to tell the user (also in the log)."""

    position: ppk.BasePosition
    q: int
    sd: tuple[float, float, float]
    baseline_km: float
    hours: float
    solution: Path
    warnings: list[str] = field(default_factory=list)


# --- helpers ---------------------------------------------------------------------------------------------------------


def _span(path: Path, who: str) -> tuple[datetime, datetime]:
    """The first and last epoch (GPS time) of the observation file ``path``; a ``ValueError`` naming it as ``who`` ("the
    rover DJI_0001.obs") for a file of navigation messages or without epochs."""
    if rinex.header(path).kind != "obs":
        raise ValueError(f"{who} holds navigation messages, not observations")
    span = rinex.time_span(path)
    if span is None:
        raise ValueError(f"{who} has no observation epochs")
    return span


def _session(pieces: Sequence[Path]) -> tuple[datetime, datetime, float]:
    """The first and the last epoch (GPS time) of the observations of the user's base, in one file or several, and the
    seconds between its epochs (those of its file that has them closest, as :func:`.rinex.join` writes them; 0 when
    none says)."""
    spans = [_span(piece, f"your base {piece.name}") for piece in pieces]
    steps = [step for piece in pieces if (step := rinex.epoch_interval(piece))]
    return min(a for a, _ in spans), max(b for _, b in spans), min(steps, default=0.0)


def _middle_epoch(start: datetime, end: datetime) -> float:
    """The decimal year of the middle of the time from ``start`` to ``end`` (GPS time, taken back to UTC)."""
    middle = start + (end - start) / 2
    return frames.decimal_year(middle - timedelta(seconds=rinex.gps_minus_utc(middle)))


def _enu_sd(sigma: Sequence[float] | None, lat: float, lon: float) -> tuple[float, float, float] | None:
    """The standard deviations east, north, up (m) at ``lat``, ``lon`` (degrees) of a position whose geocentric X, Y,
    Z have the independent standard deviations ``sigma`` (m): the covariance turned into the frame of the place, the
    square roots of its diagonal. None stays None."""
    if sigma is None:
        return None
    phi, lam = math.radians(lat), math.radians(lon)
    to_enu = np.array([[-math.sin(lam), math.cos(lam), 0.0],
                       [-math.sin(phi) * math.cos(lam), -math.sin(phi) * math.sin(lam), math.cos(phi)],
                       [math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi)]])
    covariance = to_enu @ np.diag(np.square(np.asarray(sigma, dtype=np.float64))) @ to_enu.T
    east, north, up = (float(v) for v in np.sqrt(np.diag(covariance)))
    return east, north, up


def _station_position(station: stations.Station, epoch: float, progress) -> ppk.BasePosition:
    """The coordinates of ``station`` at ``epoch`` (:func:`.stations.coordinates`) as the position of a base; a
    ``ValueError`` when only its approximate position is known: as a base it would move the whole survey by metres."""
    found = stations.coordinates(station, epoch, progress)
    if found.source == "approximate":
        raise ValueError(f"{station.id}: none of the EPN, the IGS and RING gave its coordinates (the log says why), "
                         "only its approximate position, good to metres: as a base it would move the whole survey by "
                         "as much. Choose another station")
    lat, lon, h = found.llh
    sigma = None if found.sigma is None else [float(v) for v in found.sigma]
    detail = {"station": station.id, "name": station.name, "networks": list(station.networks),
              "coordinates": {"source": found.source, "frame": found.frame, "epoch": found.epoch,
                              "xyz": [float(v) for v in found.xyz], "sigma_xyz": sigma},
              "sigma": SIGMA_KINDS.get(found.source) if sigma is not None else None, "citation": found.citation}
    return ppk.BasePosition(lat, lon, h, "station", frame=found.frame, epoch=found.epoch,
                            sd=_enu_sd(sigma, lat, lon), detail=detail)


def _antenna_calibrations(progress) -> str | None:
    """igs20.atx in the folder of the program, downloaded when it has no recent copy (:func:`.atx.atx_file`): the PPK
    that follows reads only that copy (``ppk.solve(atx="auto")``). When it cannot be had the PPK goes on without it;
    what to tell the user then (None when the file is there)."""
    try:
        antex.atx_file(progress=progress)
    except (download.DownloadError, ValueError, OSError) as exc:
        return (f"the antenna calibrations of the IGS ({antex.FILE_NAME}) could not be had, so the phase centres of "
                f"the antennas are not corrected: {exc}")
    return None


def _hours(own: tuple[datetime, datetime, float], station: tuple[datetime, datetime, float],
           holes: Sequence[tuple[datetime, datetime]]) -> tuple[float, list[tuple[datetime, datetime, float]]]:
    """How long the user's base (``own``: its first and last epoch and the seconds between its epochs) and the
    ``station`` (the same) observed together, in hours, and the ``holes`` of the base (each from the epoch before it to
    the epoch after it) in that time, with the seconds each takes from it. The time is from the later first epoch to
    the earlier end of the interval of the last epochs (n epochs a second apart are n seconds of observations), less
    the holes, to the second (the epochs of a receiver are a few milliseconds off one)."""
    begin = max(own[0], station[0])
    finish = min(own[1] + timedelta(seconds=own[2]), station[1] + timedelta(seconds=station[2]))
    lost = []
    for before, after in holes:
        gone = (min(after, finish) - max(before + timedelta(seconds=own[2]), begin)).total_seconds()
        if gone > 0.0:
            lost.append((before, after, gone))
    seconds = (finish - begin).total_seconds() - sum(gone for _, _, gone in lost)
    return round(seconds) / 3600.0, lost


def _notes(name: str, station: stations.Station, q: int, km: float, hours: float,
           lost: Sequence[tuple[datetime, datetime, float]]) -> list[str]:
    """What to tell about a static baseline of quality ``q`` (RTKLIB's Q: fix or float), ``km`` long, of ``hours``,
    with the holes ``lost`` in the observations of the user's base (:func:`_hours`)."""
    notes = []
    if q != 1:
        notes.append(f"the static solution of your base {name} is {ppk.status(q)} (Q={q}), not fixed: its position can "
                     "be decimetres off, and every position of the survey with it. A longer session, or a station "
                     "nearer the base, helps RTKLIB fix it")
    if km > ppk.BASELINE_WARNING_KM:
        notes.append(f"your base {name} is {km:.1f} km from the station {station.id}: beyond "
                     f"{ppk.BASELINE_WARNING_KM:g} km the ambiguities are hard to fix and the atmosphere is not the "
                     "same above the two, so the position can be centimetres or decimetres off. A nearer station is "
                     "better")
    long = [hole for hole in lost if hole[2] > GAP_WARNING_S]
    if long:
        before, after, gone = max(long, key=lambda hole: hole[2])
        more = f", and {len(long) - 1} more holes" if len(long) > 1 else ""
        notes.append(f"your base {name} has no observations from {ppk.span_text(before, after)} ({round(gone / 60)} "
                     f"minutes{more}): the session is counted without {'them' if more else 'it'}, {hours:.2f} hours")
    if hours < MIN_HOURS:
        notes.append(f"your base {name} and the station {station.id} observed together for {round(hours * 60)} "
                     "minutes: a static baseline wants an hour or more, and longer the further the station")
    return notes


# --- a station as the base of a flight ------------------------------------------------------------------------------


def station_base(rover: str | Path, station: stations.Station, folder: str | Path,
                 progress=None) -> tuple[Path, ppk.BasePosition]:
    """A permanent station as the base of the flight whose RINEX observations are ``rover``: the file of the station's
    observations for the time of the flight (:func:`.stations.download_observations`, at 1 s when the archive has them
    for the whole flight, else at 30 s, with ten minutes on each side), into ``folder``, and its position. ``folder``
    must not be the work folder of the PPK of the flight nor a folder inside it: :func:`.ppk.solve` refuses inputs
    anywhere in there. No navigation file is downloaded: the PPK of the flight takes the drone's, or one of
    :func:`.stations.download_navigation`.

    The position is the coordinates of the station (:func:`.stations.coordinates`: those of its marker, to which RTKLIB
    adds the antenna height of the header of its RINEX file, as for any base) in ITRF2020 at the middle of the flight:
    a :class:`.ppk.BasePosition` of source ``"station"``, frame ``"ITRF2020"`` and that epoch, so the positions of the
    photos are in ITRF2020 at the epoch of the flight. Its ``sd`` is the standard deviation of the coordinates turned
    from X, Y, Z to east, north and up, and ``detail`` says what they are: ``station``, ``name``, ``networks``,
    ``coordinates`` (``source`` EPN, IGS or RING, ``frame``, ``epoch``, ``xyz``, ``sigma_xyz``), ``sigma`` (``"formal"``
    for the EPN and the IGS, ``"scatter"`` of the daily positions for RING, None without) and ``citation``.

    The antenna calibrations of the IGS (igs20.atx) are downloaded too when the program has no recent copy, for the
    PPK that follows (``ppk.solve(atx="auto")`` reads only that copy); not having them is a warning in the log. A
    ``ValueError`` when the station has only its approximate position (good to metres: no base), when ``rover`` is not
    a file of observations, and when the observations cannot be had (:func:`.stations.download_observations` says
    why)."""
    progress = as_progress(progress)
    progress(0.0, f"{station.id}: the base of the flight")
    rover = Path(rover)
    start, end = _span(rover, f"the rover {rover.name}")
    position = _station_position(station, _middle_epoch(start, end), progress.sub(0.0, 0.1))
    observations = stations.download_observations(station, start, end, folder, progress.sub(0.1, 0.6), rate="auto")
    note = _antenna_calibrations(progress.sub(0.7, 0.3))
    if note:
        log.warning("%s", note)
    progress(1.0, f"{station.id}: the base of the flight, in ITRF2020 at {position.epoch:.4f}")
    return observations, position


# --- the user's own base by a static baseline -----------------------------------------------------------------------


def baseline(base_obs: str | Path | Sequence[str | Path], station: stations.Station, folder: str | Path, navs=None,
             progress=None, *, station_obs: str | Path | None = None,
             station_position: ppk.BasePosition | None = None, atx: str | Path | None = "auto") -> BaselineResult:
    """The mark of the user's own base - a receiver left on a mark nobody surveyed - by a static baseline from the
    permanent ``station``: RTKLIB solves the base (``base_obs``, its RINEX observations: one file, or several, which
    :func:`.ppk.solve` joins) as a rover standing still for the whole session against the station, forward, into one
    position (:func:`.ppk.config` ``mode="static", rover_antenna="*", solution="forward", static_single=True``). RTKLIB
    takes off the antenna height of the header of the base's RINEX file: the position is of the mark on the ground.

    The station is given by its observations ``station_obs`` and its position ``station_position`` (a
    :class:`.ppk.BasePosition`, as :func:`station_base` makes it), or, for those not given, the program downloads them
    into the ``station`` folder of ``folder``: its observations for the time of the base (at 1 s or 30 s) and its
    coordinates at the middle of the session (refused when only approximate). The navigation files are ``navs``, or,
    with None, those beside the files of the base (:func:`.ppk.navigation_beside`), else those beside the station's
    file when it is given, else the broadcast navigation messages of the session, downloaded into the ``station``
    folder (:func:`.stations.download_navigation`); with all given nothing is downloaded but, with ``atx="auto"``,
    igs20.atx. RTKLIB works in the ``baseline`` folder of ``folder`` (:func:`.ppk.solve`: its solution stays there).
    ``atx`` is the file of antenna calibrations, as for :func:`.ppk.solve`; with "auto" igs20.atx is downloaded first
    when the program has no recent copy (not having it is a warning), so that both antennas, the station's and the
    base's, are corrected.

    The mark is the station's coordinates plus the baseline: its :class:`.ppk.BasePosition` is in the frame and at the
    epoch of the station's position, of source ``"baseline"``, with RTKLIB's standard deviations east, north, up and in
    ``detail`` the station, its position (with its own detail and citation), the base's antenna, its height and
    calibration, ``baseline_km``, ``q``, ``status``, ``hours`` and the name of the solution file. The warnings name the
    two receivers "your base" and "the station": those of :func:`.ppk.solve`, the antenna of the base not calibrated,
    a solution that is float, not fixed, a station further than :data:`.ppk.BASELINE_WARNING_KM`, a hole of more than
    :data:`GAP_WARNING_S` seconds in the observations of the base, a session under :data:`MIN_HOURS` - they are in the
    result and in the log.

    A ``ValueError`` when the base and the station have no time in common, when a file is not one of observations,
    when the coordinates of the station are only approximate, for what :func:`.ppk.solve` refuses, and when RTKLIB's
    solution is neither fixed nor float (:data:`FIX_OR_FLOAT`): a position of the code alone, metres off - the file of
    the solution is kept, and named."""
    progress = as_progress(progress)
    pieces = [Path(base_obs)] if isinstance(base_obs, (str, os.PathLike)) else [Path(p) for p in base_obs]
    if not pieces:
        raise ValueError("no file given for your base")
    folder = Path(folder)
    progress(0.0, f"Your base by a static baseline from {station.id}")
    own = _session(pieces)
    first, last = own[0], own[1]
    info = rinex.header(pieces[0])  # its antenna, its height and its marker: the same in every file of one receiver
    name = ppk.base_name(pieces[0], info)
    fetched = folder / STATION_FOLDER
    if station_position is None:  # first: no download for a station that cannot be a base
        station_position = _station_position(station, _middle_epoch(first, last), progress.sub(0.0, 0.1))
    given = station_obs is not None
    if not given:
        station_obs = stations.download_observations(station, first, last, fetched, progress.sub(0.1, 0.5),
                                                     rate="auto")
    station_obs = Path(station_obs)
    station_span = _span(station_obs, f"the station {station.id} ({station_obs.name})")
    if max(first, station_span[0]) > min(last, station_span[1]):
        raise ValueError(f"your base {name} ({ppk.span_text(first, last)}) and the station {station.id} "
                         f"({ppk.span_text(*station_span)}) have no time in common: the station has to record while "
                         "your base does")
    if navs is None:
        # a file the archive downloaded is never looked beside: the navigation files of other days may be there
        beside = ppk.navigation_beside(pieces) or (ppk.navigation_beside([station_obs]) if given else [])
        navs = ([path for path, _ in beside] or
                stations.download_navigation(first, last, fetched, progress.sub(0.6, 0.1)))
    warnings: list[str] = []
    if isinstance(atx, str) and atx == "auto":
        note = _antenna_calibrations(progress.sub(0.7, 0.2))
        if note:
            warnings.append(note)
    progress(0.9, f"RTKLIB: the static baseline from {station.id} to {name}")
    solution = ppk.solve(pieces, station_obs, navs, station_position, folder=folder / WORK_FOLDER, atx=atx,
                         mode="static", rover_antenna="*", solution="forward", static_single=True, roles=ROLES)
    (lat, lon, h), q, sd = ppk.static_position(solution.path)
    if q not in FIX_OR_FLOAT:
        raise ValueError(f"RTKLIB found no relative position of your base {name} from the station {station.id}: its "
                         f"static solution is {ppk.QUALITY.get(q, 'of no known quality')} (Q={q}), not fixed (Q=1) "
                         "or float (Q=2) - a position of the code alone, metres off, which as a base would move the "
                         "whole survey by as much. The two receivers may not have observed the same satellites for "
                         f"long enough, or the navigation messages do not cover the session. The solution is kept in "
                         f"{solution.path}")
    km = ppk.baseline_km(solution)
    hours, lost = _hours(own, (*station_span, rinex.epoch_interval(station_obs) or 0.0), solution.rover_holes)
    warnings += solution.warnings
    own_note = ppk.antenna_warning(name, info, solution.rover_calibration, role=ROLES[0])
    if own_note:
        warnings.append(own_note)
    warnings += _notes(name, station, q, km, hours, lost)
    for text in warnings:
        log.warning("%s", text)
    sp = station_position
    detail = {"station": station.id, "name": station.name, "networks": list(station.networks),
              "station_position": {"lat": sp.lat, "lon": sp.lon, "h": sp.h, "source": sp.source, "frame": sp.frame,
                                   "epoch": sp.epoch, "sd": None if sp.sd is None else list(sp.sd),
                                   "detail": sp.detail},
              "citation": sp.detail.get("citation"), "base": name, "antenna": ppk.antenna_type(info),
              "antenna_delta_hen": None if info.antenna_delta_hen is None else list(info.antenna_delta_hen),
              "antenna_calibration": solution.rover_calibration, "baseline_km": km, "q": q,
              "status": ppk.status(q), "hours": hours, "solution": solution.path.name}
    position = ppk.BasePosition(lat, lon, h, "baseline", frame=sp.frame, epoch=sp.epoch, sd=sd, detail=detail)
    progress(1.0, f"{name}: {ppk.status(q)}, {km:.3f} km from {station.id}")
    return BaselineResult(position=position, q=q, sd=sd, baseline_km=km, hours=hours, solution=solution.path,
                          warnings=warnings)
