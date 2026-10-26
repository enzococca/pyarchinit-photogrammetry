"""Putting a position and its accuracy on a camera."""
from __future__ import annotations

import copy
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from ..core.camera import Camera, Reference
from ..core.chunk import IMAGE_EXTENSIONS, Chunk
from . import dji, ppk

FACTOR = 2.0
FLOORS = {"fix": 0.01, "float": 0.20, "single": 1.0}
NO_RTK = [5.0, 5.0, 10.0]

CLOCK_TOLERANCE_S = 3.0  # a photo belongs to its shot when its time is this close to the clock of the flight
ZONE_STEP_S = 900.0  # the camera keeps the local time: whole quarter hours from UTC ...
ZONE_TOLERANCE_S = 120.0  # ... give or take the drift of its clock ...
ZONE_MAX_S = 14 * 3600.0  # ... and never more than 14 hours
# a PPK solution that puts the antenna this far (median, m) from the drone's own positions in the MRK: a warning
FAR_FROM_MRK_M = 50.0


def accuracy(status: str | None, std: list[float] | None) -> list[float]:
    """Accuracy in metres of a position: twice the declared standard deviations, never below the floor of
    its status (an unlisted status counts as single). Without RTK data (``"none"`` or no status) it is
    ``[5, 5, 10]``."""
    if status is None or status == "none":
        return list(NO_RTK)
    floor = FLOORS.get(status, FLOORS["single"])
    return [max(FACTOR * s, floor) for s in std] if std else [floor] * 3


def set_camera_position(cam: Camera, location: list[float], crs: str, accuracy: list[float], source: str,
                        status: str | None = None, height: str = "exif", extra: dict | None = None) -> None:
    """Use ``location`` (in ``crs``) as the reference of ``cam`` and record in ``cam.meta["gnss"]`` where it
    comes from, its status and whether the height is ``"exif"`` or ``"ellipsoidal"``."""
    cam.reference = Reference(location=list(location), accuracy=list(accuracy), enabled=True)
    cam.meta["reference_crs"] = crs
    cam.meta["gnss"] = {"source": source, "status": status, "accuracy": list(accuracy), "height": height,
                        **(extra or {})}


def apply_mrk(chunk: Chunk, mrk_path: str | Path, photos_folder: str | Path | None = None) -> dict:
    """Give the photos of ``chunk`` the position of their shot in a DJI ``Timestamp.MRK``: the camera, not the
    antenna (the lever arm of the shot is applied), in ``EPSG:4979`` with its ellipsoidal height, with the
    accuracy of the shot's standard deviations and RTK flag; ``meta["gnss"]`` keeps the lever arm and the antenna
    position of the line. It replaces any position the photo had, so doing it again changes nothing.

    The photos are the images in ``photos_folder`` (default: the folder of the MRK), in the order of their names.
    A DJI photo is paired with the line whose index is the shot number in its name; the photos that have none
    are paired by order, but only when the MRK has as many lines as the folder has photos. Every pair is then
    checked against the time the camera wrote in the photo (its local time): the photos must agree on one clock,
    a time zone from the UTC of the shots, and a photo more than 3 seconds from it keeps the position it had (a
    photo of another flight with the same shot number, a photo without a date). When no photo has a date the
    pairs are kept as they are. A ``ValueError`` and nothing changed when the times agree on no time zone, or when
    fewer than half of the pairs are on time (the MRK of another flight, a folder of several flights).

    Returns ``{"matched", "shots", "photos", "status": {status: count}, "unmatched": [label],
    "shots_unmatched": [index]}``: ``unmatched`` are the photos of the folder left without a position (no line
    for them, not in the chunk, or not on time), ``shots_unmatched`` the shots that gave no photo theirs."""
    pairing = _pair_shots(chunk, mrk_path, photos_folder)
    placed = []
    for f, shot, cam in pairing.kept:
        state = dji.STATUS.get(shot.flag, "unknown")
        north, east, vertical = shot.std
        set_camera_position(cam, list(dji.camera_position(shot)), "EPSG:4979",
                            accuracy(state, [east, north, vertical]), "dji_mrk", status=state, height="ellipsoidal",
                            extra=_shot_record(pairing.mrk, shot, [shot.lon, shot.lat, shot.ellh]))
        placed.append((f, shot, state))
    return _result(pairing, placed)


def apply_ppk(chunk: Chunk, mrk_path: str | Path, solution: ppk.Solution,
              photos_folder: str | Path | None = None) -> dict:
    """Give the photos of ``chunk`` the PPK position of their shot in a DJI ``Timestamp.MRK``: the antenna where the
    post-processed ``solution`` puts it at the GPS time of the shot (:func:`.ppk.at`: an epoch, or the line between
    two epochs at most a second apart) plus the lever arm of the shot, in ``EPSG:4979`` with its ellipsoidal height;
    the status from the quality Q of the solution (1 fix, 2 and 4 float, any other single) and the accuracy from its
    standard deviations (twice, never below the floor of the status). ``meta["gnss"]`` keeps what it keeps for an MRK,
    the antenna being the one of the solution, with the ``q`` and the ``base``, ``rover`` and ``solution`` (file name)
    of the solution. It replaces any position the photo had, so doing it again changes nothing.

    The photos are paired with the shots and checked against their times as by :func:`apply_mrk`, with its errors. A
    photo whose shot falls where the solution has no position (more than a second between two of its epochs, before
    its first or after its last) keeps the position it had and is among the unmatched, its shot among the shots
    unmatched. A ``ValueError`` and nothing changed when no shot of the MRK falls within the time of the solution (the
    MRK and the solution of different flights), or when the solution has no positions.

    Returns what :func:`apply_mrk` returns with ``"warnings"``: those of the solution (the base at the position of its
    RINEX header can be metres off), how many photos fell where the solution has no position, and whether the solution
    puts the antenna more than :data:`FAR_FROM_MRK_M` (median) from where the drone's own receiver put it in the MRK
    (the observations of the base taken for the rover's, those of another receiver)."""
    pairing = _pair_shots(chunk, mrk_path, photos_folder)
    name = Path(solution.path).name
    times = np.asarray(solution.times, dtype=np.float64)
    shot_times = np.array([shot.gps_time for shot in pairing.shots])
    if not len(times) or not np.any((shot_times >= times[0] - ppk.TIME_RESOLUTION_S)
                                    & (shot_times <= times[-1] + ppk.TIME_RESOLUTION_S)):
        shots = (f"its shots were taken from {ppk.span_text(shot_times.min(), shot_times.max())}" if len(shot_times)
                 else "it has no shots")
        covered = f"has positions from {ppk.span_text(times[0], times[-1])}" if len(times) else "has no positions"
        raise ValueError(f"{pairing.mrk}: {shots}, and the solution {name} {covered}: they are not of the same flight")
    llh, q, sd, ok = ppk.at(solution, [shot.gps_time for _f, shot, _cam in pairing.kept])
    positions, apart = [], []  # all of them first: nothing changes when something goes wrong
    for (f, shot, cam), antenna, quality, deviations, found in zip(pairing.kept, llh, q, sd, ok):
        if not found:
            continue
        lat, lon, h = (float(v) for v in antenna)
        state = ppk.status(quality)
        extra = {**_shot_record(pairing.mrk, shot, [lon, lat, h]), "q": int(quality),
                 "base": copy.deepcopy(solution.base), "rover": solution.rover, "solution": name}
        positions.append((f, shot, cam, list(dji.lever_arm(lat, lon, h, shot.offset_ned_mm)),
                          accuracy(state, [float(v) for v in deviations]), state, extra))
        apart.append(_horizontal_m(shot.lat, shot.lon, lat, lon))
    placed = []
    for f, shot, cam, location, acc, state, extra in positions:
        set_camera_position(cam, location, "EPSG:4979", acc, "ppk", status=state, height="ellipsoidal", extra=extra)
        placed.append((f, shot, state))
    warnings = list(solution.warnings)
    gaps = len(pairing.kept) - len(placed)
    if gaps:
        photos = "photos were" if gaps > 1 else "photo was"
        keep = "they keep the position they had" if gaps > 1 else "it keeps the position it had"
        warnings.append(f"{gaps} {photos} taken where the solution {name} has no position (a gap of more than "
                        f"{ppk.MAX_GAP_S:g} s between two of its epochs, or before the first or after the last): "
                        f"{keep}")
    if apart and float(np.median(apart)) > FAR_FROM_MRK_M:
        warnings.append(f"the solution {name} puts the antenna {float(np.median(apart)):.0f} m (median) from where the "
                        f"drone's own receiver put it in {pairing.mrk}: the rover's observations may be those of the "
                        "base, or of another receiver")
    return {**_result(pairing, placed), "warnings": warnings}


@dataclass
class _Pairing:
    """The photos of a folder paired with the shots of a ``Timestamp.MRK`` (:func:`_pair_shots`)."""

    mrk: str  # the name of the MRK file
    shots: list[dji.MrkShot]  # in the order of the file
    files: list[Path]  # the photos of the folder, by name
    paired: list[tuple[Path, dji.MrkShot | None]]  # every photo with its shot, None when the MRK has none for it
    kept: list[tuple[Path, dji.MrkShot, Camera]]  # the pairs whose photo is in the chunk and was taken at its shot


def _pair_shots(chunk: Chunk, mrk_path: str | Path, photos_folder: str | Path | None) -> _Pairing:
    """The photos of ``photos_folder`` (default: the folder of the MRK) paired with the shots of the DJI
    ``Timestamp.MRK`` ``mrk_path`` by shot number or by order, and checked against the times of the photos in
    ``chunk``, as :func:`apply_mrk` says; a ``ValueError`` when they cannot be."""
    mrk_path = Path(mrk_path)
    shots = dji.read_mrk(mrk_path)
    folder = Path(photos_folder) if photos_folder else mrk_path.parent
    files = sorted((f for f in folder.iterdir() if f.is_file() and not f.name.startswith(".")
                    and f.suffix.lower() in IMAGE_EXTENSIONS), key=lambda f: f.name)
    numbers = [dji.shot_number(f.name) for f in files]
    if any(n is not None for n in numbers):
        by_index = {}
        for shot in shots:
            if shot.index in by_index:
                raise ValueError(f"{mrk_path.name}: shot {shot.index} appears twice")
            by_index[shot.index] = shot
        paired = [(f, by_index.get(n)) for f, n in zip(files, numbers)]
    elif len(shots) == len(files):
        paired = list(zip(files, shots))
    else:
        raise ValueError(f"{mrk_path.name} has {len(shots)} shots and {folder} has {len(files)} photos, and the "
                         "photo names carry no shot number: they cannot be paired by order")
    cameras = {Path(c.path).resolve(): c for c in chunk.cameras}
    candidates = [(f, shot, cameras.get(f.resolve())) for f, shot in paired]
    kept = _on_time([p for p in candidates if p[1] is not None and p[2] is not None], mrk_path.name)
    return _Pairing(mrk_path.name, shots, files, paired, kept)


def _shot_record(mrk: str, shot: dji.MrkShot, antenna: list[float]) -> dict:
    """What ``meta["gnss"]`` keeps of the shot a position comes from, to check the camera against it: the MRK and the
    shot, its time in UTC, its lever arm and the position ``[lon, lat, h]`` of the antenna."""
    return {"mrk": mrk, "shot": shot.index, "utc": shot.utc.isoformat(), "lever_arm_ned_mm": list(shot.offset_ned_mm),
            "antenna": list(antenna)}


def _result(pairing: _Pairing, placed: list[tuple[Path, dji.MrkShot, str]]) -> dict:
    """What :func:`apply_mrk` returns once the ``(photo, shot, status)`` of ``placed`` took the position of their
    shot."""
    files, used = {f for f, _shot, _state in placed}, {id(shot) for _f, shot, _state in placed}
    return {"matched": len(placed), "shots": len(pairing.shots), "photos": len(pairing.files),
            "status": dict(Counter(state for _f, _shot, state in placed)),
            "unmatched": [f.stem for f, _shot in pairing.paired if f not in files],
            "shots_unmatched": [s.index for s in pairing.shots if id(s) not in used]}


def _horizontal_m(lat0: float, lon0: float, lat1: float, lon1: float) -> float:
    """The horizontal distance in metres between two points (degrees), on a sphere as large as the earth: plenty to
    tell metres from hundreds of metres."""
    east = math.radians(float(dji.wrap_longitude(lon1 - lon0))) * math.cos(math.radians(lat0))
    return 6371000.0 * math.hypot(math.radians(lat1 - lat0), east)


def _photo_time(cam: Camera) -> datetime | None:
    """The time the camera wrote in the EXIF of the photo (its clock: the local time, to the second); None without
    one."""
    try:
        return datetime.strptime(str(cam.meta.get("datetime"))[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _off_zone(offset: float) -> float:
    """Seconds from ``offset`` (local time less UTC) to the nearest time zone: a whole quarter hour within 14 hours."""
    zone = min(max(round(offset / ZONE_STEP_S) * ZONE_STEP_S, -ZONE_MAX_S), ZONE_MAX_S)
    return abs(offset - zone)


def _signed(seconds: float) -> str:
    return ("-" if seconds < 0 else "+") + str(timedelta(seconds=round(abs(seconds))))


def _clock(offsets: np.ndarray, name: str) -> float:
    """The offset between the clock of the photos and UTC (seconds) of the flight of the MRK ``name``: of the groups
    of offsets within :data:`CLOCK_TOLERANCE_S` of each other that lie on a time zone, the largest (its median),
    between groups as large the nearest to a time zone. ``ValueError`` when no group lies on a time zone, or when
    another group on a time zone has two photos or more and is at least half as large (another flight in the folder:
    the times cannot tell which one the MRK is of - its own flight may have lost photos)."""
    s = np.sort(np.asarray(offsets, float))
    lo = np.searchsorted(s, s - CLOCK_TOLERANCE_S, "left")
    hi = np.searchsorted(s, s + CLOCK_TOLERANCE_S, "right")
    groups = sorted({(int(b - a), float(np.median(s[a:b]))) for a, b in zip(lo, hi)},
                    key=lambda g: (-g[0], _off_zone(g[1])))
    plausible = [g for g in groups if _off_zone(g[1]) <= ZONE_TOLERANCE_S]
    if not plausible:
        raise ValueError(f"{name}: the times of the photos and of the shots do not agree: by the clock of the camera "
                         f"the photos were taken {_signed(groups[0][1])} from the UTC of their shots, which is no "
                         "time zone. These are not the photos of this MRK, or the clock of the camera was wrong")
    size, clock = plausible[0]
    # another flight: at least two photos and half as many as the best group (a single photo late is just late)
    rivals = sorted({round(c) for n, c in plausible
                     if n >= max(2, size / 2) and abs(c - clock) > 2 * CLOCK_TOLERANCE_S})
    if rivals:
        raise ValueError(f"{name}: the times of the photos fit the shots of more than one flight (at "
                         f"{', '.join(_signed(c) for c in [clock] + rivals)} from UTC): keep the photos of each flight "
                         "in a folder of their own")
    return clock


def _on_time(pairs: list[tuple], name: str) -> list[tuple]:
    """The ``(file, shot, camera)`` pairs whose photo was taken at the time of its shot (see :func:`apply_mrk`), or
    all of them when no photo has a date."""
    times = [_photo_time(cam) for _f, _shot, cam in pairs]
    if not any(times):
        return pairs
    offsets = [None if t is None else (t - shot.utc.replace(tzinfo=None)).total_seconds()
               for t, (_f, shot, _cam) in zip(times, pairs)]
    clock = _clock(np.array([o for o in offsets if o is not None]), name)
    kept = [p for p, o in zip(pairs, offsets) if o is not None and abs(o - clock) <= CLOCK_TOLERANCE_S]
    if 2 * len(kept) < len(pairs):
        raise ValueError(f"{name}: only {len(kept)} of {len(pairs)} photos were taken at the time of their shot "
                         f"(clock of the camera {_signed(clock)} from UTC): the MRK is of another flight, or the "
                         "folder holds the photos of other flights too")
    return kept


ONLY_FIX = "gnss_only_fix"


def only_fix(chunk: Chunk, on: bool = True) -> int:
    """Switch off the references of the photos whose position is not an RTK fix (``on``), or give them back.

    A photo counts as fix when ``meta["gnss"]["status"]`` is ``"fix"``: a position with no status (the EXIF GPS, a
    positions file with no status column) does not. Only references that are on and have a position are
    switched off; the keys of the ones switched off are kept in ``chunk.meta["gnss_only_fix"]["disabled"]``, so
    ``on=False`` switches back on exactly those and a reference the user had switched off by hand stays off. Doing
    it again with ``on=True`` takes back what was switched off before and decides anew (after positions were
    applied again). Returns how many references were switched off, or back on."""
    cameras = {c.key: c for c in chunk.cameras}
    before = (chunk.meta.get(ONLY_FIX) or {}).get("disabled") or []
    for key in before:
        if key in cameras:
            cameras[key].reference.enabled = True
    if not on:
        chunk.meta.pop(ONLY_FIX, None)
        return len([k for k in before if k in cameras])
    off = [c.key for c in chunk.cameras if c.reference.location and c.reference.enabled
           and (c.meta.get("gnss") or {}).get("status") != "fix"]
    for key in off:
        cameras[key].reference.enabled = False
    chunk.meta[ONLY_FIX] = {"disabled": off}
    return len(off)


def keep_only_fix(chunk: Chunk) -> None:
    """After positions were applied (they switch every reference they set on): when *only the fix positions* is on
    for ``chunk``, decide again which references it switches off."""
    if ONLY_FIX in chunk.meta:
        only_fix(chunk, True)
