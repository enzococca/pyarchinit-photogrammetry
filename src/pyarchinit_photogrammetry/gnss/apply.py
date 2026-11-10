"""Putting a position and its accuracy on a camera."""
from __future__ import annotations

import copy
import math
from collections import Counter
from pathlib import Path

import numpy as np

from ..core.camera import Camera, Reference
from ..core.chunk import Chunk
from . import dji, frames, lever, ppk, rinex, shots as shots_module
from .shots import Pairing, Shot, ShotSet, photo_time  # noqa: F401  (photo_time: read here by positions)

FACTOR = 2.0
FLOORS = {"fix": 0.01, "float": 0.20, "single": 1.0}
NO_RTK = [5.0, 5.0, 10.0]

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
                        status: str | None = None, height: str = "exif", extra: dict | None = None, *,
                        frame: str | None = None, epoch: float | None = None) -> None:
    """Use ``location`` (in ``crs``) as the reference of ``cam`` and record in ``cam.meta["gnss"]`` where it
    comes from, its status, whether the height is ``"exif"`` or ``"ellipsoidal"``, its reference ``frame`` and its
    ``epoch``.

    The ``frame`` is one of :data:`.frames.FRAMES`, by default the frame of the datum of ``crs``
    (:func:`.frames.frame_of`; ``"unknown"`` for WGS 84 and for any datum that is none of them), and must be that one: a
    GNSS position is written in the geographic system of its frame (:func:`.frames.crs_of`), a position of a file keeps
    the system of the file.
    The ``epoch`` is the decimal year of the coordinates (:func:`.frames.decimal_year`; for a position computed against
    a base, the epoch of the base's coordinates in a frame that moves with time), None when it is not known - which a
    position in such a frame (ITRF2020) cannot be: it could not be converted (:func:`.frames.frame_and_epoch`). A
    ``ValueError``, and nothing changed, for a frame that is not the one of ``crs``, for a frame the program does not
    know, and for an epoch that is not a year or is missing in ITRF2020."""
    frame, epoch = _frame_and_epoch(crs, frame, epoch)
    cam.reference = Reference(location=list(location), accuracy=list(accuracy), enabled=True)
    cam.meta["reference_crs"] = crs
    cam.meta["gnss"] = {"source": source, "status": status, "accuracy": list(accuracy), "height": height,
                        **(extra or {}), "frame": frame, "epoch": epoch}


def _frame_and_epoch(crs: str, frame: str | None, epoch: float | None) -> tuple[str, float | None]:
    """The frame and the epoch :func:`set_camera_position` records for a position in ``crs``, checked: a frame that is
    not the one of ``crs`` first, then the epoch (:func:`.frames.frame_and_epoch`)."""
    own = frames.frame_of(crs)
    own = frames.UNKNOWN if own in (None, frames.WGS84) else own  # a position on WGS 84: nobody knows its frame
    if frame is not None:
        frames.crs_of(frame)  # a frame the program knows
        given = frames.UNKNOWN if frame == frames.WGS84 else frame
        if given != own:
            raise ValueError(f"the frame of a position in {crs} is {own}, not {given}: the positions in {given} are "
                             f"written in {frames.crs_of(given)}")
    return frames.frame_and_epoch(own, epoch)


def apply_mrk(chunk: Chunk, mrk_path: str | Path, photos_folder: str | Path | None = None) -> dict:
    """Give the photos of ``chunk`` the position of their shot in a DJI ``Timestamp.MRK``: the camera, not the
    antenna (the lever arm of the shot is applied), in ``EPSG:4979`` with its ellipsoidal height, with the
    accuracy of the shot's standard deviations and RTK flag; ``meta["gnss"]`` keeps the lever arm and the antenna
    position of the line, the frame ``"unknown"`` (the drone's own RTK: a network in ETRF, the drone's own base...) and
    the epoch of the shot. It replaces any position the photo had, so doing it again changes nothing.

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
    result = apply_shots(chunk, shots_module.from_mrk(mrk_path), photos_folder, by="auto")
    return {key: result[key] for key in _RESULT}


_RESULT = ("matched", "shots", "photos", "status", "unmatched", "shots_unmatched")  # what apply_mrk returns
_PPK_RESULT = _RESULT + ("kept_fix", "baseline_km", "warnings")  # and apply_ppk


def _check_lever(shotset: ShotSet, body_lever_mm) -> None:
    if body_lever_mm is not None and any(s.lever_ned_mm is not None for s in shotset.shots):
        raise ValueError(f"{shotset.name or shotset.kind}: its shots carry their own lever arm (north, east, down): "
                         "a lever arm in the body of the drone is not for them")


def _camera(shot: Shot, lat: float, lon: float, h: float, body_lever_mm) -> tuple[list[float], dict | None, bool]:
    """The camera ``[lon, lat, h]`` of ``shot`` whose antenna is at ``lat, lon, h``: with the arm of the shot (an MRK)
    or ``body_lever_mm`` turned with its attitude (:func:`.lever.camera_from_antenna`); what ``meta["gnss"]["lever"]``
    keeps of it; whether only its vertical part was applied (no attitude)."""
    if shot.lever_ned_mm is not None:
        lon_c, lat_c, h_c, _note = lever.camera_from_antenna(lat, lon, h, ned_mm=shot.lever_ned_mm)
        return [lon_c, lat_c, h_c], {"ned_mm": list(shot.lever_ned_mm)}, False
    if body_lever_mm is None:
        return [float(lon), float(lat), float(h)], None, False
    body = [float(v) for v in body_lever_mm]  # plain numbers: the project is saved as JSON
    attitude = None if shot.attitude is None else [float(v) for v in shot.attitude]
    lon_c, lat_c, h_c, note = lever.camera_from_antenna(lat, lon, h, body_frd_mm=body, attitude=attitude)
    return [float(lon_c), float(lat_c), float(h_c)], {"body_frd_mm": body, "attitude": attitude, "note": note}, \
        note is not None


def _source_record(pairing: Pairing, shot: Shot, antenna: list[float], lever_record: dict | None) -> dict:
    """What ``meta["gnss"]`` keeps of the shot a position comes from: for a DJI MRK as it always did
    (:func:`_shot_record`); for any other source ``shots`` (its ``kind``, ``file``, the ``index`` and ``gps_time`` of the
    shot, the ``latency_s`` added to it, ``by`` how its photo was paired), ``lever`` (the arm, None without one), the
    ``antenna`` ``[lon, lat, h]`` and the ``utc`` of the shot."""
    if pairing.kind == "mrk":
        return _shot_record(pairing.name, shot, antenna)
    return {"shots": {"kind": pairing.kind, "file": pairing.name,
                      "index": None if shot.index is None else int(shot.index), "gps_time": float(shot.gps_time),
                      "latency_s": pairing.latency_s, "by": pairing.by, "confirmed": pairing.confirmed},
            "lever": lever_record, "antenna": [float(v) for v in antenna], "utc": shots_module.utc(shot).isoformat()}


def _pairing_warnings(pairing: Pairing, vertical_only: int) -> list[str]:
    """What the pairing and the lever arm leave to say: shots without a valid time, doubles left out, photos given
    only the vertical part of the arm."""
    warnings = list(pairing.warnings)
    if pairing.photos_ambiguous:
        n = len(pairing.photos_ambiguous)
        warnings.append(f"{n} photo{'s have' if n > 1 else ' has'} more than one shot of {pairing.name} within "
                        f"{shots_module.NEAR_SHOT_S:g} s of {'their' if n > 1 else 'its'} time, which cannot tell "
                        f"which is {'theirs' if n > 1 else 'its own'}: left without a shot (the first, "
                        f"{pairing.photos_ambiguous[0].name})")
    if pairing.invalid:
        n = len(pairing.invalid)
        warnings.append(f"{n} shot{'s' if n > 1 else ''} of {pairing.name} without a valid time left out")
    if pairing.left_doubles:
        n = len(pairing.left_doubles)
        warnings.append(f"{n} double shot{'s' if n > 1 else ''} of {pairing.name} (within "
                        f"{shots_module.DOUBLE_SHOT_S:g} s of the one before) left out: the first of each kept")
    if vertical_only:
        photos = f"{vertical_only} photos have" if vertical_only > 1 else "1 photo has"
        warnings.append(f"{photos} no attitude of the drone in {pairing.name}: only the vertical part of the lever "
                        "arm was applied (vertical only: no attitude)")
    return warnings


def _report(pairing: Pairing) -> dict:
    """How the photos were paired, for the result of :func:`apply_shots` and :func:`apply_ppk_shots`."""
    return {"by": pairing.by, "clock_s": pairing.clock_s, "latency_s": pairing.latency_s,
            "extra_shots": [s.index for s in pairing.extra_shots],
            "photos_without_shot": [f.stem for f in pairing.photos_without_shot], "doubles": pairing.doubles,
            "invalid": len(pairing.invalid), "confirmed": pairing.confirmed,
            "photos_ambiguous": [f.stem for f in pairing.photos_ambiguous]}


def apply_shots(chunk: Chunk, shotset: ShotSet, photos_folder: str | Path | None = None, *, by: str = "auto",
                latency_s: float = 0.0, body_lever_mm=None) -> dict:
    """Give the photos of ``chunk`` the position of their shot in ``shotset`` (a DJI MRK, an autopilot log, an
    ``_events.pos``...): the camera, not the antenna - the arm of the shot (an MRK) or ``body_lever_mm`` (forward,
    right, down in mm in the body of the drone) turned with the attitude of the shot, only its vertical part without
    one (:func:`.lever.camera_from_antenna`, with a warning) -, with the accuracy of the shot's standard deviations
    and status (:func:`accuracy`; deviations without a status: twice them, never below a centimetre), in the frame ``"unknown"`` at the epoch of the shot. An ellipsoidal height stays
    one (``EPSG:4979``), any other is above the sea level (``height`` ``"msl"``, ``EPSG:4326``: never taken for an
    ellipsoidal one). The photos are paired with the shots by :func:`.shots.pair` (``by``, ``latency_s``: the times of
    the shots, not their positions, which the source gives at the shots only), with its errors.

    A DJI MRK is recorded as :func:`apply_mrk` always did; any other source keeps in ``meta["gnss"]`` its kind as
    ``source``, ``shots``, ``lever``, ``antenna`` and ``utc`` (:func:`_source_record`). A ``ValueError`` and nothing
    changed for valid shots without the position of the antenna (the events of a receiver: those take theirs from a
    PPK solution, :func:`apply_ppk_shots`) and for an arm in the body with shots that carry their own.

    Returns what :func:`apply_mrk` returns with ``by``, ``clock_s``, ``latency_s``, ``extra_shots`` (their indices),
    ``photos_without_shot`` and ``photos_ambiguous`` (labels), ``doubles``, ``invalid`` (a count), ``confirmed``
    (:class:`.shots.Pairing`) and ``warnings`` (among them, a pairing by number or by order the times cannot confirm)."""
    _check_lever(shotset, body_lever_mm)
    missing = sum(s.valid and s.antenna is None for s in shotset.shots)
    if missing:
        raise ValueError(f"{shotset.name or shotset.kind}: {missing} of its shots carry no position of the antenna (the "
                         "events of a receiver): they take one from a PPK solution")
    pairing = shots_module.pair(chunk, shotset, photos_folder, by=by, latency_s=latency_s)
    mrk = pairing.kind == "mrk"
    placed, vertical_only = [], 0
    for f, shot, cam in pairing.kept:
        lat, lon, h = shot.antenna
        location, lever_record, vertical = _camera(shot, lat, lon, h, body_lever_mm)
        vertical_only += vertical
        state = shot.status
        north, east, up = shot.std if shot.std is not None else (None, None, None)
        if state is None and shot.std is not None and not mrk:  # deviations without a status: they say enough
            acc = [max(FACTOR * float(v), FLOORS["fix"]) for v in (east, north, up)]
        else:
            acc = accuracy(state, [east, north, up] if shot.std is not None else None)
        when = shots_module.utc(shot)
        if mrk:
            set_camera_position(cam, location, frames.crs_of(frames.UNKNOWN), acc, dji.SOURCE_MRK, status=state,
                                height="ellipsoidal", extra=_shot_record(pairing.name, shot, [lon, lat, h]),
                                frame=frames.UNKNOWN, epoch=frames.decimal_year(when))
        else:
            ellipsoidal = shot.height == "ellipsoidal"
            set_camera_position(cam, location, frames.crs_of(frames.UNKNOWN) if ellipsoidal else "EPSG:4326", acc,
                                pairing.kind, status=state, height="ellipsoidal" if ellipsoidal else "msl",
                                extra=_source_record(pairing, shot, [lon, lat, h], lever_record),
                                frame=frames.UNKNOWN, epoch=frames.decimal_year(when))
        placed.append((f, shot, state))
    warnings = _pairing_warnings(pairing, vertical_only)
    if latency_s and not mrk:
        warnings.append(f"the latency of {latency_s * 1000:g} ms moved the times of the shots, not their positions: "
                        f"{pairing.name} gives the positions at the shots only")
    return {**_result(pairing, placed), **_report(pairing), "warnings": warnings}


def apply_ppk(chunk: Chunk, mrk_path: str | Path, solution: ppk.Solution,
              photos_folder: str | Path | None = None, replace_fix: bool = False) -> dict:
    """Give the photos of ``chunk`` the PPK position of their shot in a DJI ``Timestamp.MRK``: the antenna where the
    post-processed ``solution`` puts it at the GPS time of the shot (:func:`.ppk.at`: an epoch, or the line between
    two epochs at most a second apart) plus the lever arm of the shot, with its ellipsoidal height, in the frame of the
    base of the solution (``base["frame"]``: the positions of a base in ITRF2020 are in ITRF2020, ``EPSG:9989``; those
    of a base whose frame nobody knows - typed, from the RINEX header, a solution read from a file - in ``EPSG:4979``,
    :func:`.frames.crs_of`). A position of the rover is the base's coordinates plus the baseline: in a frame that moves
    with time its epoch is the one of the base's coordinates (``base["epoch"]``, which can be years before the flight);
    in any other the epoch of the shot. The status from the quality Q of the solution (1 fix, 2 and 4 float, any other
    single) and the accuracy from its standard deviations (twice, never below the floor of the status).
    ``meta["gnss"]`` keeps what it keeps for an MRK, the antenna being the one of the solution, with the ``q`` and the
    ``base`` (its ``position`` ``[lon, lat, h]``, its ``frame`` and ``epoch``), ``rover`` and ``solution`` (the
    name of the solution file of the run) of the solution. It replaces any position the photo had, so doing it again
    changes nothing - but an RTK fix of the drone itself (:func:`onboard_fix`: the XMP of a DJI photo, a DJI MRK),
    which the photo keeps unless ``replace_fix``: the PPK is for the photos that are not fix.

    The photos are paired with the shots and checked against their times as by :func:`apply_mrk`, with its errors. A
    photo whose shot falls where the solution has no position (more than a second between two of its epochs, before
    its first or after its last) keeps the position it had and is among the unmatched, its shot among the shots
    unmatched. A ``ValueError`` and nothing changed when no shot of the MRK falls within the time of the solution (the
    MRK and the solution of different flights), when the solution has no positions, when its base is in a frame the
    program does not know, or in ITRF2020 without the epoch of its coordinates. A run that gives photos their positions
    is recorded in ``chunk.meta`` (:func:`.ppk.last_run`).

    Returns what :func:`apply_mrk` returns - the photos that kept their onboard fix are neither matched nor unmatched -
    with ``"kept_fix"`` (how many kept it), ``"baseline_km"`` (:func:`.ppk.baseline_km`, None without the position of
    the base) and ``"warnings"``: those of the solution (the base at the position of its RINEX header can be metres off,
    the phase-centre offset of its antenna not applied when RTKLIB had no calibration for it:
    :func:`.ppk.antenna_warning`), how many photos fell where the solution has no position,
    whether the solution puts the antenna more than :data:`FAR_FROM_MRK_M` (median) from where the drone's own receiver
    put it in the MRK (the observations of the base taken for the rover's, those of another receiver), a base more
    than :data:`.ppk.BASELINE_WARNING_KM` from the flight, a base typed elsewhere for other photos of the chunk
    (:func:`.ppk.other_typed_position`), the onboard fixes kept more than :data:`FIX_DISAGREES_M` (median, in height
    too when both heights are ellipsoidal) from the PPK positions of the same shots, the onboard fixes replaced by
    positions that are not fix, and the photos whose onboard float (a DJI XMP or MRK) is single in the PPK, which take
    the PPK position all the same."""
    result = apply_ppk_shots(chunk, shots_module.from_mrk(mrk_path), solution, photos_folder, replace_fix=replace_fix)
    return {key: result[key] for key in _PPK_RESULT}


def apply_ppk_shots(chunk: Chunk, shotset: ShotSet, solution: ppk.Solution, photos_folder: str | Path | None = None,
                    *, by: str = "auto", latency_s: float = 0.0, body_lever_mm=None, replace_fix: bool = False) -> dict:
    """Give the photos of ``chunk`` the PPK position of their shot in ``shotset`` (a DJI MRK, the events of a
    receiver, an autopilot log...): the antenna where ``solution`` puts it at the GPS time of the shot plus
    ``latency_s`` (the camera exposes that long after the event), plus the arm of the shot (an MRK) or ``body_lever_mm``
    (forward, right, down in mm in the body of the drone) turned with the attitude of the shot, only its vertical part
    without one (with a warning). Everything else - the frame and epoch, the status and accuracy, the onboard fixes
    kept unless ``replace_fix``, the errors, the warnings, the run recorded - as :func:`apply_ppk`, whose contract this
    is; the photos paired by :func:`.shots.pair` (``by``, ``latency_s``). ``meta["gnss"]`` keeps for a source other
    than an MRK what :func:`apply_shots` keeps (the antenna being the one of the solution), and the run its ``shots``
    (``kind``, ``file``, ``by``, ``latency_s``, ``body_lever_mm``, ``confirmed``) with ``mrk`` None. The events of
    the rover's receiver (:data:`.ppk.EVENT_KINDS`) are checked against the time marks RTKLIB wrote for the run
    (:func:`.ppk.events_check`), its warnings among the others. For any source but a DJI MRK (whose results
    stay as they were) a shot between two epochs adds in quadrature to the accuracy of its photo how far the straight
    line between them may be from the path of the drone (:func:`.ppk.interpolation_error`, kept as
    ``meta["gnss"]["interpolation_m"]``), with a warning when that is more than
    :data:`.ppk.INTERPOLATION_WARNING_M` (a receiver logging at 1 Hz on a drone that turns).

    Returns what :func:`apply_ppk` returns with what :func:`apply_shots` adds about the pairing."""
    _check_lever(shotset, body_lever_mm)
    pairing = shots_module.pair(chunk, shotset, photos_folder, by=by, latency_s=latency_s)
    late = float(latency_s)
    name = Path(solution.path).name
    times = np.asarray(solution.times, dtype=np.float64)
    shot_times = np.array([shot.gps_time + late for shot in pairing.shots if shot.valid])
    if not len(times) or not np.any((shot_times >= times[0] - ppk.TIME_RESOLUTION_S)
                                    & (shot_times <= times[-1] + ppk.TIME_RESOLUTION_S)):
        shots = (f"its shots were taken from {ppk.span_text(shot_times.min(), shot_times.max())}" if len(shot_times)
                 else "it has no shots")
        covered = f"has positions from {ppk.span_text(times[0], times[-1])}" if len(times) else "has no positions"
        raise ValueError(f"{pairing.name}: {shots}, and the solution {name} {covered}: they are not of the same flight")
    base = solution.base or {}
    # rover = base + baseline: the positions are in the frame of the base and, in a frame that moves with time, at the
    # epoch of its coordinates (checked here, before anything changes)
    frame = base.get("frame") or frames.UNKNOWN
    system = frames.crs_of(frame)
    held = frames.frame_and_epoch(frame, base.get("epoch"))[1] if frame in frames.DYNAMIC else None
    when = [shot.gps_time + late for _f, shot, _cam in pairing.kept]
    llh, q, sd, ok = ppk.at(solution, when)
    # the straight line between two epochs off the path of a drone that turns: in the accuracy of the photos (not for
    # a DJI MRK, whose results stay as they always were)
    curve = np.zeros(len(when)) if pairing.kind == "mrk" else np.nan_to_num(ppk.interpolation_error(solution, when))
    curved = []  # the photos whose line is more than INTERPOLATION_WARNING_M off
    positions, apart = [], []  # all of them first: nothing changes when something goes wrong
    kept, disagree, downgraded = [], [], 0  # the onboard fixes kept, their distance from the PPK, those replaced worse
    worse = 0  # the photos whose onboard float or single is better than the state the PPK gives them
    vertical_only = 0  # the photos given only the vertical part of an arm in the body
    for (f, shot, cam), antenna, quality, deviations, found, off in zip(pairing.kept, llh, q, sd, ok, curve):
        onboard, before = onboard_fix(cam), onboard_state(cam)
        if not found:
            if onboard and not replace_fix:
                kept.append((f, shot))
            continue
        lat, lon, h = (float(v) for v in antenna)
        state = ppk.status(quality)
        location, lever_record, vertical = _camera(shot, lat, lon, h, body_lever_mm)
        if shot.antenna is not None:
            apart.append(ppk.horizontal_m(shot.antenna[0], shot.antenna[1], lat, lon))
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
        vertical_only += vertical
        extra = {**_source_record(pairing, shot, [lon, lat, h], lever_record), "q": int(quality),
                 "base": copy.deepcopy(solution.base), "rover": solution.rover, "solution": name}
        acc = accuracy(state, [float(v) for v in deviations])
        if off > 0:
            acc = [math.hypot(v, float(off)) for v in acc]
            extra["interpolation_m"] = round(float(off), 4)
            if off > ppk.INTERPOLATION_WARNING_M:
                curved.append(float(off))
        positions.append((f, shot, cam, location, acc, state, extra))
    baseline = ppk.baseline_km(solution)
    retyped = None
    if base.get("position_source") == "typed" and base.get("position"):
        retyped = ppk.other_typed_position(chunk, base.get("name"), base["position"],
                                           exclude={cam.key for _f, _shot, cam, *_rest in positions})
    placed = []
    for f, shot, cam, location, acc, state, extra in positions:
        set_camera_position(cam, location, system, acc, "ppk", status=state, height="ellipsoidal", extra=extra,
                            frame=frame, epoch=frames.decimal_year(shots_module.utc(shot)) if held is None else held)
        placed.append((f, shot, state))
    warnings = list(solution.warnings)
    gaps = len(pairing.kept) - len(placed) - len(kept)
    if gaps:
        photos = "photos were" if gaps > 1 else "photo was"
        keep = "they keep the position they had" if gaps > 1 else "it keeps the position it had"
        warnings.append(f"{gaps} {photos} taken where the solution {name} has no position (a gap of more than "
                        f"{ppk.MAX_GAP_S:g} s between two of its epochs, or before the first or after the last): "
                        f"{keep}")
    if curved:
        rate = ppk.epoch_rate_hz(solution)
        photos = f"{len(curved)} photos were" if len(curved) > 1 else "1 photo was"
        warnings.append(f"{photos} taken between epochs of {name} where the drone turns or changes speed: the straight "
                        f"line between the epochs is up to {max(curved) * 100:.0f} cm off its path there (the "
                        f"receiver logs at {rate:.3g} Hz). Their accuracy includes it; log at 5 Hz or more")
    if apart and float(np.median(apart)) > FAR_FROM_MRK_M:
        where = (f"drone's own receiver put it in {pairing.name}" if pairing.kind == "mrk"
                 else f"{pairing.name} puts it")
        warnings.append(f"the solution {name} puts the antenna {float(np.median(apart)):.0f} m (median) from where the "
                        f"{where}: the rover's observations may be those of the base, or of another receiver")
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
    warnings += _pairing_warnings(pairing, vertical_only)
    if shotset.kind in ppk.EVENT_KINDS:  # the events of the rover: checked against RTKLIB's time marks
        warnings += ppk.events_check(solution, shotset)
    if placed:
        _remember(chunk, pairing, photos_folder, solution, replace_fix, body_lever_mm)
    return {**_result(pairing, placed, kept), "kept_fix": len(kept), "baseline_km": baseline, "warnings": warnings,
            **_report(pairing)}


def _station_of(base: dict) -> dict | None:
    """The permanent station of a base that is one (``position_source`` ``"station"``) or that a static baseline from
    one placed (``"baseline"``), as the PPK dialog chooses it again: ``{"id", "name", "networks", "lat", "lon", "h",
    "xyz"}`` from what its position records (:mod:`.base`); None for any other base, or one that records no station."""
    source, detail = base.get("position_source"), base.get("detail") or {}
    if source not in ("station", "baseline") or not detail.get("station"):
        return None
    try:
        if source == "station":
            xyz = [float(v) for v in (detail.get("coordinates") or {})["xyz"]]
            lat, lon, h = rinex.ecef_to_llh(xyz)
        else:
            where = detail["station_position"]
            lat, lon, h = (float(where[k]) for k in ("lat", "lon", "h"))
            xyz = list(rinex.llh_to_ecef((lat, lon, h)))
    except (KeyError, TypeError, ValueError):
        return None
    return {"id": str(detail["station"]), "name": str(detail.get("name") or detail["station"]),
            "networks": [str(n) for n in detail.get("networks") or []], "lat": lat, "lon": lon, "h": h,
            "xyz": [float(v) for v in xyz]}


def _remember(chunk: Chunk, pairing: Pairing, photos_folder: str | Path | None, solution: ppk.Solution,
              replace_fix: bool, body_lever_mm=None) -> None:
    """Record the run in ``chunk.meta`` (:func:`.ppk.last_run`): its files, its base and how it was placed (the typed
    position ``[lon, lat, h]``, the frame and the epoch of its position, the permanent station it came from), whether
    it replaced the onboard fixes; for shots other than those of a DJI MRK, ``mrk`` None and ``shots`` (``kind``,
    ``file``, ``by``, ``latency_s``, ``body_lever_mm``, ``confirmed``)."""
    base, inputs = solution.base or {}, solution.inputs or {}
    typed = base.get("position_source") == "typed" and base.get("position")
    files = [s.source.get("file") for s in pairing.shots if s.source.get("file")]
    source = str(Path(files[0]).absolute()) if files else None
    last = {
        "mrk": source if pairing.kind == "mrk" else None,
        "photos": str(Path(photos_folder).absolute()) if photos_folder else None,
        "rover": inputs.get("rover"), "base": inputs.get("base"), "navs": list(inputs.get("navs") or []),
        "base_name": base.get("name"), "position_source": base.get("position_source"),
        "position": [float(v) for v in base["position"]] if typed else None, "frame": base.get("frame"),
        "epoch": base.get("epoch"), "station": _station_of(base), "replace_fix": bool(replace_fix),
        "solution": Path(solution.path).name}
    if pairing.kind != "mrk":
        last["shots"] = {"kind": pairing.kind, "file": source, "by": pairing.by, "latency_s": pairing.latency_s,
                         "body_lever_mm": None if body_lever_mm is None else [float(v) for v in body_lever_mm],
                         "confirmed": pairing.confirmed}
    chunk.meta[ppk.MEMORY] = {"last": last}


def _shot_record(mrk: str, shot: Shot, antenna: list[float]) -> dict:
    """What ``meta["gnss"]`` keeps of the MRK shot a position comes from, to check the camera against it: the MRK and
    the shot, its time in UTC, its lever arm and the position ``[lon, lat, h]`` of the antenna."""
    return {"mrk": mrk, "shot": shot.index, "utc": shots_module.utc(shot).isoformat(),
            "lever_arm_ned_mm": list(shot.lever_ned_mm), "antenna": list(antenna)}


def _result(pairing: Pairing, placed: list[tuple[Path, Shot, str]], kept: list[tuple[Path, Shot]] = ()) -> dict:
    """What :func:`apply_mrk` returns once the ``(photo, shot, status)`` of ``placed`` took the position of their
    shot; the ``(photo, shot)`` of ``kept`` kept a position of their shot they had (an onboard RTK fix): neither
    matched nor unmatched."""
    files = {f for f, _shot, _state in placed} | {f for f, _shot in kept}
    used = {id(shot) for _f, shot, _state in placed} | {id(shot) for _f, shot in kept}
    return {"matched": len(placed), "shots": len(pairing.shots), "photos": len(pairing.files),
            "status": dict(Counter(state for _f, _shot, state in placed)),
            "unmatched": [f.stem for f, _shot in pairing.paired if f not in files],
            "shots_unmatched": [s.index for s in pairing.shots if id(s) not in used]}


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
