"""Putting a position and its accuracy on a camera."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from ..core.camera import Camera, Reference
from ..core.chunk import IMAGE_EXTENSIONS, Chunk
from . import dji

FACTOR = 2.0
FLOORS = {"fix": 0.01, "float": 0.20, "single": 1.0}
NO_RTK = [5.0, 5.0, 10.0]

CLOCK_TOLERANCE_S = 3.0  # a photo belongs to its shot when its time is this close to the clock of the flight
ZONE_STEP_S = 900.0  # the camera keeps the local time: whole quarter hours from UTC ...
ZONE_TOLERANCE_S = 120.0  # ... give or take the drift of its clock ...
ZONE_MAX_S = 14 * 3600.0  # ... and never more than 14 hours


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
    status, used = Counter(), set()
    for _f, shot, cam in kept:
        state = dji.STATUS.get(shot.flag, "unknown")
        north, east, vertical = shot.std
        set_camera_position(cam, list(dji.camera_position(shot)), "EPSG:4979",
                            accuracy(state, [east, north, vertical]), "dji_mrk", status=state, height="ellipsoidal",
                            extra={"mrk": mrk_path.name, "shot": shot.index, "utc": shot.utc.isoformat(),
                                   "lever_arm_ned_mm": list(shot.offset_ned_mm),
                                   "antenna": [shot.lon, shot.lat, shot.ellh]})
        status[state] += 1
        used.add(id(shot))
    placed = {f for f, _shot, _cam in kept}
    return {"matched": sum(status.values()), "shots": len(shots), "photos": len(files), "status": dict(status),
            "unmatched": [f.stem for f, _shot in paired if f not in placed],
            "shots_unmatched": [s.index for s in shots if id(s) not in used]}


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
