"""Putting a position and its accuracy on a camera."""
from __future__ import annotations

import copy
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
# where the drone's own RTK puts a photo: the XMP of a DJI photo (added with the photos) and a DJI Timestamp.MRK
ONBOARD_SOURCES = (dji.SOURCE_XMP, dji.SOURCE_MRK)
# onboard RTK fixes kept beside PPK positions this far (median, m) from the PPK positions of the same shots: a warning
FIX_DISAGREES_M = 0.10


def onboard_state(cam: Camera) -> str | None:
    """The RTK state (``"fix"``, ``"float"``, ``"single"``, ...) the drone itself gave the position of ``cam``: from the
    XMP of a DJI photo or from a DJI ``Timestamp.MRK`` (:data:`ONBOARD_SOURCES`); None for a position from anywhere
    else."""
    gnss = cam.meta.get("gnss") or {}
    return gnss.get("status") if gnss.get("source") in ONBOARD_SOURCES else None


def onboard_fix(cam: Camera) -> bool:
    """The position of ``cam`` is an RTK fix of the drone itself: from the XMP of a DJI photo or from a DJI
    ``Timestamp.MRK`` (:data:`ONBOARD_SOURCES`), with the status ``"fix"``."""
    return onboard_state(cam) == "fix"


def _length(metres: float) -> str:
    """A distance as a sentence says it: in centimetres below a metre, else in metres."""
    return f"{metres * 100.0:.0f} cm" if metres < 1.0 else f"{metres:.2f} m"


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
                            accuracy(state, [east, north, vertical]), dji.SOURCE_MRK, status=state,
                            height="ellipsoidal",
                            extra=_shot_record(pairing.mrk, shot, [shot.lon, shot.lat, shot.ellh]))
        placed.append((f, shot, state))
    return _result(pairing, placed)


def apply_ppk(chunk: Chunk, mrk_path: str | Path, solution: ppk.Solution,
              photos_folder: str | Path | None = None, replace_fix: bool = False) -> dict:
    """Give the photos of ``chunk`` the PPK position of their shot in a DJI ``Timestamp.MRK``: the antenna where the
    post-processed ``solution`` puts it at the GPS time of the shot (:func:`.ppk.at`: an epoch, or the line between
    two epochs at most a second apart) plus the lever arm of the shot, in ``EPSG:4979`` with its ellipsoidal height;
    the status from the quality Q of the solution (1 fix, 2 and 4 float, any other single) and the accuracy from its
    standard deviations (twice, never below the floor of the status). ``meta["gnss"]`` keeps what it keeps for an MRK,
    the antenna being the one of the solution, with the ``q`` and the ``base`` (its ``position`` ``[lon, lat, h]``),
    ``rover`` and ``solution`` (the name of the solution file of the run) of the solution. It replaces any position the
    photo had, so doing it again changes nothing - but an RTK fix of the drone itself (:func:`onboard_fix`: the XMP
    of a DJI photo, a DJI MRK), which the photo keeps unless ``replace_fix``: the PPK is for the photos that are not
    fix.

    The photos are paired with the shots and checked against their times as by :func:`apply_mrk`, with its errors. A
    photo whose shot falls where the solution has no position (more than a second between two of its epochs, before
    its first or after its last) keeps the position it had and is among the unmatched, its shot among the shots
    unmatched. A ``ValueError`` and nothing changed when no shot of the MRK falls within the time of the solution (the
    MRK and the solution of different flights), or when the solution has no positions. A run that gives photos their
    positions is recorded in ``chunk.meta`` (:func:`.ppk.last_run`).

    Returns what :func:`apply_mrk` returns - the photos that kept their onboard fix are neither matched nor unmatched -
    with ``"kept_fix"`` (how many kept it), ``"baseline_km"`` (:func:`.ppk.baseline_km`, None without the position of
    the base) and ``"warnings"``: those of the solution (the base at the position of its RINEX header can be metres off,
    the phase-centre offset of its antenna is not applied), how many photos fell where the solution has no position,
    whether the solution puts the antenna more than :data:`FAR_FROM_MRK_M` (median) from where the drone's own receiver
    put it in the MRK (the observations of the base taken for the rover's, those of another receiver), a base more
    than :data:`.ppk.BASELINE_WARNING_KM` from the flight, a base typed elsewhere for other photos of the chunk
    (:func:`.ppk.other_typed_position`), the onboard fixes kept more than :data:`FIX_DISAGREES_M` (median, in height
    too when both heights are ellipsoidal) from the PPK positions of the same shots, the onboard fixes replaced by
    positions that are not fix, and the photos whose onboard float (a DJI XMP or MRK) is single in the PPK, which take
    the PPK position all the same."""
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
    kept, disagree, downgraded = [], [], 0  # the onboard fixes kept, their distance from the PPK, those replaced worse
    worse = 0  # the photos whose onboard float or single is better than the state the PPK gives them
    for (f, shot, cam), antenna, quality, deviations, found in zip(pairing.kept, llh, q, sd, ok):
        onboard, before = onboard_fix(cam), onboard_state(cam)
        if not found:
            if onboard and not replace_fix:
                kept.append((f, shot))
            continue
        lat, lon, h = (float(v) for v in antenna)
        state = ppk.status(quality)
        location = list(dji.lever_arm(lat, lon, h, shot.offset_ned_mm))
        apart.append(ppk.horizontal_m(shot.lat, shot.lon, lat, lon))
        if onboard and not replace_fix:
            kept.append((f, shot))
            if cam.reference.location and cam.meta.get("reference_crs") in ("EPSG:4326", "EPSG:4979"):
                # the heights compared only when both are ellipsoidal: the altitude of an EXIF may be above the sea
                ellipsoidal = (cam.meta.get("gnss") or {}).get("height") == "ellipsoidal"
                disagree.append(ppk.distance_m(cam.reference.location, location, heights=ellipsoidal))
            continue
        if onboard and state != "fix":
            downgraded += 1
        elif before in ("float", "single") and ppk.RANK[state] > ppk.RANK[before]:
            worse += 1
        extra = {**_shot_record(pairing.mrk, shot, [lon, lat, h]), "q": int(quality),
                 "base": copy.deepcopy(solution.base), "rover": solution.rover, "solution": name}
        positions.append((f, shot, cam, location, accuracy(state, [float(v) for v in deviations]), state, extra))
    base = solution.base or {}
    baseline = ppk.baseline_km(solution)
    retyped = None
    if base.get("position_source") == "typed" and base.get("position"):
        retyped = ppk.other_typed_position(chunk, base.get("name"), base["position"],
                                           exclude={cam.key for _f, _shot, cam, *_rest in positions})
    placed = []
    for f, shot, cam, location, acc, state, extra in positions:
        set_camera_position(cam, location, "EPSG:4979", acc, "ppk", status=state, height="ellipsoidal", extra=extra)
        placed.append((f, shot, state))
    warnings = list(solution.warnings)
    gaps = len(pairing.kept) - len(placed) - len(kept)
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
    if baseline is not None and baseline > ppk.BASELINE_WARNING_KM:
        warnings.append(f"the base {base.get('name')} is {baseline:.1f} km from the flight: beyond "
                        f"{ppk.BASELINE_WARNING_KM:g} km the ambiguities are hard to fix, and the positions are mostly "
                        "float")
    if retyped is not None:
        (lon0, lat0, h0), far = retyped
        warnings.append(f"the base {base.get('name')} was typed at another position for other photos of this chunk "
                        f"(latitude {lat0:.8f}, longitude {lon0:.8f}, {h0:.3f} m), {_length(far)} from this one: "
                        "one of the two is likely mistyped; compute again the flights that used the wrong one")
    if disagree and float(np.median(disagree)) > FIX_DISAGREES_M:
        photos = (f"{len(disagree)} photos with an onboard RTK fix kept their positions, which are"
                  if len(disagree) > 1 else "1 photo with an onboard RTK fix kept its position, which is")
        warnings.append(f"{photos} {_length(float(np.median(disagree)))} (median) from the PPK positions of the same "
                        "shots: the base is likely in another reference frame than the drone's RTK corrections (a "
                        "network in ETRF and a base in ITRF, or the other way round) or at a wrong position, and the "
                        "flight now mixes the two. Check the position of the base, or compute again replacing the "
                        "onboard fixes too, so that every photo is in the frame of the base")
    if downgraded:
        photos = f"{downgraded} photos" if downgraded > 1 else "1 photo"
        warnings.append(f"{photos} had an onboard RTK fix and now {'have' if downgraded > 1 else 'has'} a PPK "
                        "position that is not fix (float or single)")
    if worse:
        photos = f"{worse} photos" if worse > 1 else "1 photo"
        warnings.append(f"{photos} got a worse state from the PPK than from the drone's own RTK (float onboard, single "
                        f"in the PPK): {'their PPK positions are' if worse > 1 else 'its PPK position is'} used all "
                        "the same. The base may be too far from the flight or share too few satellites with the "
                        "drone: check its distance and its observations")
    if placed:
        _remember(chunk, mrk_path, photos_folder, solution, replace_fix)
    return {**_result(pairing, placed, kept), "kept_fix": len(kept), "baseline_km": baseline, "warnings": warnings}


def _remember(chunk: Chunk, mrk_path: str | Path, photos_folder: str | Path | None, solution: ppk.Solution,
              replace_fix: bool) -> None:
    """Record the run in ``chunk.meta`` (:func:`.ppk.last_run`): its files, its base and how it was placed (the typed
    position ``[lon, lat, h]``), whether it replaced the onboard fixes."""
    base, inputs = solution.base or {}, solution.inputs or {}
    typed = base.get("position_source") == "typed" and base.get("position")
    chunk.meta[ppk.MEMORY] = {"last": {
        "mrk": str(Path(mrk_path).absolute()), "photos": str(Path(photos_folder).absolute()) if photos_folder else None,
        "rover": inputs.get("rover"), "base": inputs.get("base"), "navs": list(inputs.get("navs") or []),
        "base_name": base.get("name"), "position_source": base.get("position_source"),
        "position": [float(v) for v in base["position"]] if typed else None, "replace_fix": bool(replace_fix),
        "solution": Path(solution.path).name}}


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


def _result(pairing: _Pairing, placed: list[tuple[Path, dji.MrkShot, str]],
            kept: list[tuple[Path, dji.MrkShot]] = ()) -> dict:
    """What :func:`apply_mrk` returns once the ``(photo, shot, status)`` of ``placed`` took the position of their
    shot; the ``(photo, shot)`` of ``kept`` kept a position of their shot they had (an onboard RTK fix): neither
    matched nor unmatched."""
    files = {f for f, _shot, _state in placed} | {f for f, _shot in kept}
    used = {id(shot) for _f, shot, _state in placed} | {id(shot) for _f, shot in kept}
    return {"matched": len(placed), "shots": len(pairing.shots), "photos": len(pairing.files),
            "status": dict(Counter(state for _f, _shot, state in placed)),
            "unmatched": [f.stem for f, _shot in pairing.paired if f not in files],
            "shots_unmatched": [s.index for s in pairing.shots if id(s) not in used]}


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
