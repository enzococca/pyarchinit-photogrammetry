"""Putting a position and its accuracy on a camera."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from ..core.camera import Camera, Reference
from ..core.chunk import IMAGE_EXTENSIONS, Chunk
from . import dji

FACTOR = 2.0
FLOORS = {"fix": 0.01, "float": 0.20, "single": 1.0}
NO_RTK = [5.0, 5.0, 10.0]


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
    accuracy of the shot's standard deviations and RTK flag. It replaces any position the photo had, so doing it
    again changes nothing.

    The photos are the images in ``photos_folder`` (default: the folder of the MRK), in the order of their names.
    A DJI photo is paired with the line whose index is the shot number in its name; the photos that have none
    are paired by order, but only when the MRK has as many lines as the folder has photos. Returns
    ``{"matched", "shots", "photos", "status": {status: count}, "unmatched": [label]}``: ``unmatched`` are the
    photos of the folder left without a position (no line for them, or not in the chunk)."""
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
    status, unmatched = Counter(), []
    for f, shot in paired:
        cam = cameras.get(f.resolve())
        if cam is None or shot is None:
            unmatched.append(f.stem)
            continue
        state = dji.STATUS.get(shot.flag, "unknown")
        north, east, vertical = shot.std
        set_camera_position(cam, list(dji.camera_position(shot)), "EPSG:4979",
                            accuracy(state, [east, north, vertical]), "dji_mrk", status=state, height="ellipsoidal",
                            extra={"mrk": mrk_path.name, "shot": shot.index, "utc": shot.utc.isoformat()})
        status[state] += 1
    return {"matched": sum(status.values()), "shots": len(shots), "photos": len(files), "status": dict(status),
            "unmatched": unmatched}


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
