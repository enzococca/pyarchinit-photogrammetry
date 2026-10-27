"""The GNSS positions of the photos summed up per flight, and how far the alignment moved the cameras from them."""
from __future__ import annotations

from collections import Counter

import numpy as np

from ..core.chunk import Chunk
from ..engine import bundle
from ..markers import gcp
from . import frames

STATUSES = ("fix", "float", "single", "none")
HEIGHTS = ("ellipsoidal", "exif")


def _status(camera) -> str:
    """One of :data:`STATUSES`: a photo with a position and no RTK data is ``"none"``, a status nobody knows
    counts as ``"single"`` (as the accuracy it is given does)."""
    state = (camera.meta.get("gnss") or {}).get("status")
    if state is None:
        return "none"
    return state if state in STATUSES else "single"


def _height(camera) -> str:
    """The kind of height of the position: one of :data:`HEIGHTS`, or ``"unknown"`` for a position that does not
    say (read from another program)."""
    kind = (camera.meta.get("gnss") or {}).get("height")
    return kind if kind in HEIGHTS else "unknown"


def _frame(camera) -> str:
    """The reference frame of the position: one of :data:`.frames.FRAMES`, ``"unknown"`` for a position that does not
    say (written before the frames, read from another program) - as :mod:`..markers.gcp` converts it: as before
    (:func:`.frames.position_frame`)."""
    return frames.position_frame(camera.meta.get("gnss"))


def _accuracy(camera) -> list[float] | None:
    """The accuracy of the reference as it is now (not the one recorded when it was set: the user may have
    edited it), three values; ``None`` when it has none."""
    values = list(camera.reference.accuracy or [])
    if len(values) == 1:
        values *= 3
    return [float(v) for v in values] if len(values) == 3 else None


def summary(chunk: Chunk) -> list[dict]:
    """One entry per flight of the photos of ``chunk`` (see :func:`engine.bundle.flights`: a pause of more than
    20 minutes starts another; flight 0 holds the photos without a date), in the order of the flights:

    * ``flight``, ``photos``: the number of the flight and how many photos it has;
    * ``status``: ``{"fix", "float", "single", "none"}`` -> how many photos have a position of that status. A
      photo with a position and no RTK data is ``"none"``; a status that is none of these (``"unknown"``)
      counts as ``"single"``. Photos with no position at all are in none of them but in ``no_position``;
    * ``sources``: ``{source: n}`` for the photos with a position (``"unknown"`` for one that records none);
    * ``heights``: ``{kind: n}`` for the photos with a position, the kind of their height: ``"ellipsoidal"`` (GNSS,
      MRK), ``"exif"`` (the altitude of the EXIF) or ``"unknown"``;
    * ``frames``: ``{frame: n}`` for the photos with a position, the reference frame of each (one of
      :data:`.frames.FRAMES`: ``"ITRF2020"`` for a PPK against a permanent station, ``"unknown"`` for the drone's own
      RTK, the GPS of a photo and the positions written before the frames);
    * ``accuracy_median``: the median per axis of the accuracy of the references, in metres (``None`` if no
      photo of the flight has a position);
    * ``disabled``: the photos whose reference is switched off (they are counted in everything else too);
    * ``residual_rms`` (per axis, metres) and ``residual_max`` (the largest distance, metres): how far the
      aligned cameras are from their references, in the CRS of the chunk. ``None`` for a flight with no aligned
      camera, and for every flight while the chunk is not georeferenced."""
    cameras = list(chunk.cameras)
    if not cameras:
        return []
    flight_of = bundle.flights(cameras)
    errors = gcp.camera_reference_errors(chunk)
    out = []
    for number in sorted(set(flight_of.values())):
        members = [c for c in cameras if flight_of[c.key] == number]
        positioned = [c for c in members if c.reference.location]
        status = Counter({s: 0 for s in STATUSES})
        status.update(_status(c) for c in positioned)
        sources = Counter((c.meta.get("gnss") or {}).get("source") or "unknown" for c in positioned)
        accuracies = [a for a in map(_accuracy, positioned) if a is not None]
        deltas = np.array([errors[c.key]["delta"] for c in members
                           if "delta" in errors.get(c.key, {})], float).reshape(-1, 3)
        out.append({
            "flight": number,
            "photos": len(members),
            "status": dict(status),
            "sources": dict(sources),
            "heights": dict(Counter(_height(c) for c in positioned)),
            "frames": dict(Counter(_frame(c) for c in positioned)),
            "no_position": len(members) - len(positioned),
            "disabled": sum(1 for c in positioned if not c.reference.enabled),
            "accuracy_median": np.median(accuracies, axis=0).tolist() if accuracies else None,
            "residual_rms": np.sqrt(np.mean(deltas ** 2, axis=0)).tolist() if len(deltas) else None,
            "residual_max": float(np.max(np.linalg.norm(deltas, axis=1))) if len(deltas) else None,
        })
    return out


def mixed_heights(chunk: Chunk, vertical: float = 1.0) -> list[str]:
    """The kinds of height (``"ellipsoidal"``, ``"exif"``, ``"unknown"``, sorted) of the photos of ``chunk`` whose
    reference is switched on and accurate in height (under ``vertical`` metres), when they are more than one: an
    ellipsoidal height and one of the EXIF of the same place can be tens of metres apart, and both would pull the
    block. An empty list when the heights are of one kind."""
    kinds = set()
    for cam in chunk.cameras:
        acc = _accuracy(cam)
        if cam.enabled and cam.reference.enabled and cam.reference.location and acc is not None and acc[2] < vertical:
            kinds.add(_height(cam))
    return sorted(kinds) if len(kinds) > 1 else []
