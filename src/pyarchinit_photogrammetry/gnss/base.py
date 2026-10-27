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
import tempfile
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
# what the sigma of the coordinates of a station is, by where they come from: the formal error of a multi-year solution
# (EPN, IGS), the scatter of the daily positions about their line (RING)
SIGMA_KINDS = {"EPN": "formal", "IGS": "formal", "RING": "scatter"}
# in the folder of a baseline: what is downloaded for it, and the work folder of RTKLIB (ppk.solve refuses inputs in
# the latter, so the two are side by side)
STATION_FOLDER = "station"
WORK_FOLDER = "baseline"
_A, _E2 = 6378137.0, 6.69437999014e-3  # the WGS84 ellipsoid: semi-major axis (m), first eccentricity squared


@dataclass
class BaselineResult:
    """What a static baseline found for the user's own base (:func:`baseline`). ``position`` is the mark under its
    antenna, a :class:`.ppk.BasePosition` of source ``"baseline"`` in the frame and at the epoch of the coordinates of
    the station, with RTKLIB's standard deviations; ``q`` RTKLIB's quality of the solution (1 fix, 2 float:
    :data:`.ppk.QUALITY`), ``sd`` its standard deviations east, north, up (m), ``baseline_km`` the distance from the
    station to the mark, ``hours`` how long the base and the station observed together, ``solution`` the file of
    RTKLIB's solution, ``warnings`` what to tell the user (also in the log)."""

    position: ppk.BasePosition
    q: int
    sd: tuple[float, float, float]
    baseline_km: float
    hours: float
    solution: Path
    warnings: list[str] = field(default_factory=list)


# --- helpers --------------------------------------------------------------------------------------------------------


def _span(path: Path, what: str) -> tuple[datetime, datetime]:
    """The first and last epoch (GPS time) of the observation file ``path``, which is ``what``; a ``ValueError`` naming
    it for a file of navigation messages or without epochs."""
    if rinex.header(path).kind != "obs":
        raise ValueError(f"{what} {path.name} holds navigation messages, not observations")
    span = rinex.time_span(path)
    if span is None:
        raise ValueError(f"{what} {path.name} has no observation epochs")
    return span


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


def _geocentric(lat: float, lon: float, h: float) -> np.ndarray:
    """X, Y, Z (m) of a position on the WGS84 ellipsoid."""
    phi, lam = math.radians(lat), math.radians(lon)
    n = _A / math.sqrt(1.0 - _E2 * math.sin(phi) ** 2)
    return np.array([(n + h) * math.cos(phi) * math.cos(lam), (n + h) * math.cos(phi) * math.sin(lam),
                     (n * (1.0 - _E2) + h) * math.sin(phi)])


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


def _navigation_beside(paths: Sequence[Path]) -> bool:
    """A navigation file is beside one of the observation files ``paths``, where :func:`.ppk.solve` looks for one."""
    return any(ppk.navigation_info(companion) for path in paths for companion in rinex.find_companions(path))


def _observed(path: Path, span: tuple[datetime, datetime]) -> tuple[datetime, datetime]:
    """The time the observations of ``path``, whose epochs span ``span``, stand for: from the first epoch to the end of
    the interval of the last (n epochs a second apart are n seconds of observations)."""
    step = rinex.epoch_interval(path) or 0.0
    return span[0], span[1] + timedelta(seconds=step)


def _own_antenna_note(name: str, info: rinex.RinexInfo, calibration: dict | None) -> str | None:
    """What to tell about the phase centre of the antenna of the user's base ``name`` (``info``: the header of its RINEX
    file; ``calibration``: what the antenna file gave it, ``Solution.rover_calibration``), or None. An offset that is
    not applied puts the phase centre at the reference point of the antenna, and the mark found that much too high."""
    kind = ppk.antenna_type(info)
    if calibration is None or calibration["entry"] is None:
        which = (f"the antenna {kind} of the base {name}" if kind else
                 f"the antenna of the base {name} (its RINEX header names no antenna type)")
        why = ("no antenna calibrations were given to RTKLIB" if calibration is None else
               f"it is not in {calibration['file']}, so RTKLIB has no calibration for it")
        return (f"the phase-centre offset of {which} is not applied: {why}, and the mark found can be a few "
                "centimetres too high, more than 10 cm with some choke-ring antennas")
    file, used = calibration["file"], " ".join(calibration["entry"].split())
    if calibration["without_radome"]:
        return (f"the antenna {kind} of the base {name} is not in {file} with its radome: RTKLIB used the calibration "
                f"of {used}, the same antenna type with another radome, so the mark found can be a few millimetres "
                "off")
    if not ppk.same_antenna(calibration["entry"], kind):
        return (f"the antenna {kind} of the base {name} is not in {file} as its RINEX header writes it: RTKLIB used "
                f"the calibration of {used}, the nearest name it found, so the mark found can be a few millimetres "
                "off, and more if it is another antenna")
    return None


def _quality_notes(name: str, station: stations.Station, q: int, km: float, hours: float) -> list[str]:
    """What to tell about a static baseline of quality ``q`` (RTKLIB's Q), ``km`` long, of ``hours``."""
    notes = []
    status = ppk.status(q)
    if q != 1:
        off = "metres" if status == "single" else "decimetres"
        notes.append(f"the static solution of the base {name} is {status} (Q={q}), not fixed: its position can be "
                     f"{off} off, and every position of the survey with it. A longer session, or a station nearer the "
                     "base, helps RTKLIB fix it")
    if km > ppk.BASELINE_WARNING_KM:
        notes.append(f"the base {name} is {km:.1f} km from the station {station.id}: beyond "
                     f"{ppk.BASELINE_WARNING_KM:g} km the ambiguities are hard to fix and the atmosphere is not the "
                     "same above the two, so the position can be centimetres or decimetres off. A nearer station is "
                     "better")
    if hours < MIN_HOURS:
        notes.append(f"the base {name} and the station {station.id} observed together for {round(hours * 60)} minutes: "
                     f"a static baseline wants an hour or more, and longer the further the station")
    return notes


# --- a station as the base of a flight ------------------------------------------------------------------------------


def station_base(rover: str | Path, station: stations.Station, folder: str | Path,
                 progress=None) -> tuple[Path, ppk.BasePosition]:
    """A permanent station as the base of the flight whose RINEX observations are ``rover``: the file of the station's
    observations for the time of the flight (:func:`.stations.download_observations`, at 1 s when the archive has them
    for the whole flight, else at 30 s, with ten minutes on each side), into ``folder``, and its position. ``folder``
    must not be the work folder of the PPK of the flight, where :func:`.ppk.solve` refuses inputs.

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
    start, end = _span(Path(rover), "the rover")
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
    permanent ``station``: RTKLIB solves the base (``base_obs``, its RINEX observations: one file, or several that
    :func:`.rinex.join` joins) as a rover standing still for the whole session against the station, forward, into one
    position (:func:`.ppk.config` ``mode="static", rover_antenna="*", solution="forward", static_single=True``), with
    the navigation files ``navs`` (None: those beside the base, as :func:`.ppk.solve` looks for them). RTKLIB takes off
    the antenna height of the header of the base's RINEX file: the position is of the mark on the ground.

    The station is given by its observations ``station_obs`` and its position ``station_position`` (a
    :class:`.ppk.BasePosition`, as :func:`station_base` makes it), or, for those not given, the program downloads them
    into the ``station`` folder of ``folder``: its observations for the time of the base (at 1 s or 30 s), its
    coordinates at the middle of the session (refused when only approximate) and, when no navigation file is given or
    beside the base, the broadcast navigation messages. With both given nothing of the station is downloaded. RTKLIB
    works in the ``baseline`` folder of ``folder`` (:func:`.ppk.solve`: its solution stays there). ``atx`` is the file
    of antenna calibrations, as for :func:`.ppk.solve`; with "auto" igs20.atx is downloaded first when the program has
    no recent copy (not having it is a warning), so that both antennas, the station's and the base's, are corrected.

    The mark is the station's coordinates plus the baseline: its :class:`.ppk.BasePosition` is in the frame and at the
    epoch of the station's position, of source ``"baseline"``, with RTKLIB's standard deviations east, north, up and in
    ``detail`` the station, its position (with its own detail and citation), the base's antenna, its height and
    calibration, ``baseline_km``, ``q``, ``status``, ``hours`` and the name of the solution file. The warnings -
    those of :func:`.ppk.solve`, the antenna of the base not calibrated, a solution that is not fixed, a station
    further than :data:`.ppk.BASELINE_WARNING_KM`, a session under :data:`MIN_HOURS` - are in the result and in the
    log.

    A ``ValueError`` when the base and the station have no time in common, when a file is not one of observations,
    when the coordinates of the station are only approximate, and for what :func:`.ppk.solve` refuses."""
    progress = as_progress(progress)
    pieces = [Path(base_obs)] if isinstance(base_obs, (str, os.PathLike)) else [Path(p) for p in base_obs]
    if not pieces:
        raise ValueError("no file given for the base")
    folder = Path(folder)
    progress(0.0, f"The base by a static baseline from {station.id}")
    if len(pieces) == 1:
        return _baseline(pieces, pieces[0], station, folder, navs, progress, station_obs, station_position, atx)
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".base-", dir=folder) as scratch:  # the base in one file, beside the rest
        joined = Path(scratch) / "base.obs"
        rinex.join(pieces, joined)
        return _baseline(pieces, joined, station, folder, navs, progress, station_obs, station_position, atx)


def _baseline(pieces: list[Path], own: Path, station: stations.Station, folder: Path, navs, progress,
              station_obs: str | Path | None, station_position: ppk.BasePosition | None, atx) -> BaselineResult:
    """:func:`baseline` with the files of the base as the user gave them (``pieces``) and the one file they make
    (``own``)."""
    info = rinex.header(own)
    span = _span(own, "the base")
    name = ppk.base_name(pieces[0], info)
    warnings: list[str] = []
    fetched = folder / STATION_FOLDER
    if station_position is None:  # first: no download for a station that cannot be a base
        station_position = _station_position(station, _middle_epoch(*span), progress.sub(0.0, 0.1))
    if station_obs is None:
        station_obs = stations.download_observations(station, *span, fetched, progress.sub(0.1, 0.5), rate="auto")
        if navs is None and not _navigation_beside(pieces):
            navs = stations.download_navigation(*span, fetched, progress.sub(0.6, 0.1))
    station_obs = Path(station_obs)
    station_span = _span(station_obs, f"the station {station.id}")
    if max(span[0], station_span[0]) > min(span[1], station_span[1]):
        raise ValueError(f"the base {name} ({ppk.span_text(*span)}) and the station {station.id} "
                         f"({ppk.span_text(*station_span)}) have no time in common: the station has to record while "
                         "the base does")
    if isinstance(atx, str) and atx == "auto":
        note = _antenna_calibrations(progress.sub(0.7, 0.2))
        if note:
            warnings.append(note)
    progress(0.9, f"RTKLIB: the static baseline from {station.id} to {name}")
    solution = ppk.solve(own, station_obs, navs, station_position, folder=folder / WORK_FOLDER, atx=atx,
                         mode="static", rover_antenna="*", solution="forward", static_single=True)
    (lat, lon, h), q, sd = ppk.static_position(solution.path)
    sp = station_position
    km = float(np.linalg.norm(_geocentric(lat, lon, h) - _geocentric(sp.lat, sp.lon, sp.h))) / 1000.0
    (begin, finish), (first, last) = _observed(own, span), _observed(station_obs, station_span)
    hours = round((min(finish, last) - max(begin, first)).total_seconds()) / 3600.0  # epochs are a few ms off a second
    warnings += solution.warnings
    own_note = _own_antenna_note(name, info, solution.rover_calibration)
    if own_note:
        warnings.append(own_note)
    warnings += _quality_notes(name, station, q, km, hours)
    for text in warnings:
        log.warning("%s", text)
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
