"""The GNSS positions of the photos summed up per flight, and how far the alignment moved the cameras from them."""
from __future__ import annotations

from collections import Counter

import numpy as np

from ..core.chunk import Chunk
from ..engine import bundle
from ..markers import gcp

STATUSES = ("fix", "float", "single", "none")


def _status(camera) -> str:
    """One of :data:`STATUSES`: a photo with a position and no RTK data is ``"none"``, a status nobody knows
    counts as ``"single"`` (as the accuracy it is given does)."""
    state = (camera.meta.get("gnss") or {}).get("status")
    if state is None:
        return "none"
    return state if state in STATUSES else "single"


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
            "no_position": len(members) - len(positioned),
            "disabled": sum(1 for c in positioned if not c.reference.enabled),
            "accuracy_median": np.median(accuracies, axis=0).tolist() if accuracies else None,
            "residual_rms": np.sqrt(np.mean(deltas ** 2, axis=0)).tolist() if len(deltas) else None,
            "residual_max": float(np.max(np.linalg.norm(deltas, axis=1))) if len(deltas) else None,
        })
    return out
