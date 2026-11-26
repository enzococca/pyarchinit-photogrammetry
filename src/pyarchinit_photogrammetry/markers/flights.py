"""The GPS of the photos flight by flight: the photos grouped by the time they were taken, the error of their positions
per flight, and one vertical offset per flight estimated with the georeference.

A drone survey flown on several batteries may carry a different height error on each flight: a DJI drone without RTK
starts its relative altitude again when it takes off with a new battery, so the heights of a whole flight can sit
metres above or below the others while its horizontal positions agree. The alignment places the photos by what they
see; the georeference on the GPS of the photos (:func:`.gcp.update_transform`) then meets a block whose flights do not
agree in height, and its error grows by metres. With :data:`OPTION` on (``chunk.meta["gps_height_by_flight"]``) the
fit estimates one vertical offset per flight together with the similarity (the biggest flight is the datum, its offset
zero); control points, when they place the chunk, still rule, and the offsets are then measured on the cameras and
reported. The offsets are kept in ``chunk.meta["gps_flights"]`` and the camera errors
(:func:`.gcp.camera_reference_errors`) are measured against the references less the offset of their flight.

Here a flight ends with a pause of more than :data:`GAP_S` seconds between two photos (in the order they were taken,
whatever the camera); the photos without a time are one group of their own (number 0, "no time"), never shifted. The
bias of the GNSS per flight that our camera adjustment can estimate (``gnss="bias"``) keeps its own, longer pause
(:data:`..engine.bundle.FLIGHT_GAP_S`).
"""
from __future__ import annotations

from datetime import datetime

import numpy as np

GAP_S = 90.0  # seconds: two photos taken further apart belong to different flights
OPTION = "gps_height_by_flight"  # chunk.meta: the fit estimates one vertical offset per flight
RECORD = "gps_flights"  # chunk.meta: the errors per flight and the offsets of the last georeference
NOTE_FACTOR = 3.0  # a flight whose mean dz is this many sd (within the flights) off the others is pointed out
MIN_PHOTOS = 3  # a flight with fewer photos is not pointed out (its mean says little)
NO_TIME = 0  # the number of the group of the photos without a time


def capture_time(camera) -> float | None:
    """The time ``camera`` was taken, in seconds (the EXIF ``DateTimeOriginal`` in ``meta["datetime"]``), or None."""
    text = str((camera.meta or {}).get("datetime") or "").strip()[:19]
    for form in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, form).timestamp()
        except ValueError:
            continue
    return None


def flight_groups(cameras, gap_s: float = GAP_S) -> dict[int, int]:
    """{camera key: flight}: the photos in the order they were taken, a new flight (numbered from 1) after a pause of
    more than ``gap_s`` seconds; the photos without a time are :data:`NO_TIME` (0)."""
    timed = sorted(((t, c.key) for c in cameras if (t := capture_time(c)) is not None), key=lambda p: p[0])
    out: dict[int, int] = {}
    number, last = 0, None
    for t, key in timed:
        if last is None or t - last > gap_s:
            number += 1
        out[key] = number
        last = t
    for c in cameras:
        out.setdefault(c.key, NO_TIME)
    return out


def timed_flights(groups: dict[int, int], keys) -> list[int]:
    """The flights (not :data:`NO_TIME`) of the cameras ``keys``, sorted."""
    return sorted({groups[k] for k in keys if groups.get(k, NO_TIME) != NO_TIME})


def _weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    return float(np.sum(values * weights) / np.sum(weights)) if np.sum(weights) > 0 else float(np.mean(values))


def fit_with_offsets(fit, src, dst, weights, flight, datum: int | None, iterations: int = 500,
                     tol: float = 1e-6) -> tuple[np.ndarray, dict[int, float]]:
    """The transform ``fit(src, dst_corrected, weights)`` (a similarity, :func:`.gcp.similarity_with_bars`) estimated
    together with one vertical offset per flight: ``dst = M @ src + offset[flight] * z``. ``flight`` gives the flight of
    every row (a negative number for a row that is never shifted - a control point -, :data:`NO_TIME` for a photo
    without a time, also never shifted); the ``datum`` flight is held at zero (None: every flight is free, the frame
    comes from the rows never shifted). The two are solved in turn until the offsets settle. Returns the transform
    and ``{flight: offset}`` (the datum included, 0.0): the offset is how far the flight's heights sit above where the
    fit puts its photos."""
    src, dst, w = (np.asarray(v, float) for v in (src, dst, weights))
    flight = np.asarray(flight, int)
    free = [f for f in sorted(set(flight.tolist())) if f > NO_TIME and f != datum]
    offsets = {f: 0.0 for f in free}
    m = fit(src, dst, w)
    for _ in range(iterations):
        shift = np.zeros(len(dst))
        for f, o in offsets.items():
            shift[flight == f] = o
        corrected = dst.copy()
        corrected[:, 2] -= shift
        m = fit(src, corrected, w)
        pred = src @ m[:3, :3].T + m[:3, 3]
        new = {f: _weighted_mean(dst[flight == f, 2] - pred[flight == f, 2], w[flight == f]) for f in free}
        change = max((abs(new[f] - offsets[f]) for f in free), default=0.0)
        offsets = new
        if change < tol:
            break
    if datum is not None:
        offsets[datum] = 0.0
    return m, dict(sorted(offsets.items()))


def offsets_against(transform, src, dst, weights, flight) -> dict[int, float]:
    """The vertical offset of every flight (not :data:`NO_TIME`) measured against a transform fitted without them
    (control points placed the chunk): the weighted mean of ``reference z - estimated z`` of its photos."""
    src, dst, w = (np.asarray(v, float) for v in (src, dst, weights))
    flight = np.asarray(flight, int)
    pred = src @ np.asarray(transform, float)[:3, :3].T + np.asarray(transform, float)[:3, 3]
    return {f: _weighted_mean(dst[flight == f, 2] - pred[flight == f, 2], w[flight == f])
            for f in sorted(set(flight.tolist())) if f > NO_TIME}


def camera_offsets(chunk) -> dict[int, float]:
    """{camera key: vertical offset of its flight} of the last georeference (empty when none was estimated)."""
    record = chunk.meta.get(RECORD) or {}
    return {int(k): float(v) for k, v in (record.get("camera_offsets") or {}).items()}


def flight_errors(chunk, errors: dict | None = None) -> list[dict]:
    """The error of the GPS of the photos per flight (:func:`flight_groups`), from the camera errors of
    :func:`.gcp.camera_reference_errors` (``errors``; read when not given) of the photos whose reference is switched
    on: ``{"flight", "photos", "start", "mean" [dx, dy, dz], "sd" [x, y, z], "rmse", "offset"}`` in metres (``mean``,
    ``sd`` and ``rmse`` after the offset of the flight, when one was estimated; ``offset`` None without one). The
    flights in time order, the photos without a time last. Empty while the chunk cannot compare them (not
    georeferenced)."""
    from . import gcp

    if errors is None:
        errors = gcp.camera_reference_errors(chunk)
    groups = flight_groups(chunk.cameras)
    by_key = {c.key: c for c in chunk.cameras}
    offsets = (chunk.meta.get(RECORD) or {}).get("offsets") or {}
    members: dict[int, list] = {}
    for key, e in errors.items():
        cam = by_key.get(key)
        if cam is None or "delta" not in e or not cam.reference.enabled or not np.all(np.isfinite(e["delta"])):
            continue
        members.setdefault(groups.get(key, NO_TIME), []).append(key)
    out = []
    for f in sorted(members, key=lambda n: (n == NO_TIME, n)):
        d = np.array([errors[k]["delta"] for k in members[f]], float).reshape(-1, 3)
        times = [t for k in members[f] if (t := capture_time(by_key[k])) is not None]
        out.append({"flight": f, "photos": len(d),
                    "start": datetime.fromtimestamp(min(times)).strftime("%Y-%m-%d %H:%M:%S") if times else None,
                    "mean": d.mean(axis=0).tolist(), "sd": d.std(axis=0, ddof=1).tolist() if len(d) > 1 else [0.0] * 3,
                    "rmse": float(np.sqrt((d ** 2).sum(axis=1).mean())),
                    "offset": float(offsets[str(f)]) if str(f) in offsets else None})
    return out


def off_flights(flights: list[dict]) -> list[int]:
    """The flights whose mean height error (before any offset) is more than :data:`NOTE_FACTOR` times the sd of the
    heights within the flights away from the others (the median of the flight means, each counted once per photo -
    the level of most photos): a flight whose GPS
    heights were measured from another zero. The sd is the one within the flights (pooled), not the one of all the
    photos, which the offset itself inflates. Only flights of :data:`MIN_PHOTOS` photos or more, with two flights or
    more; the photos without a time do not count."""
    timed = [r for r in flights if r["flight"] != NO_TIME and r["photos"] >= 1]
    if len(timed) < 2:
        return []
    raw = {r["flight"]: r["mean"][2] - (r["offset"] or 0.0) for r in timed}
    dof = sum(r["photos"] - 1 for r in timed)
    if dof <= 0:
        return []
    sd_within = float(np.sqrt(sum((r["photos"] - 1) * r["sd"][2] ** 2 for r in timed) / dof))
    level = float(np.median(np.repeat([raw[r["flight"]] for r in timed], [r["photos"] for r in timed])))
    limit = NOTE_FACTOR * max(sd_within, 1e-3)
    return [r["flight"] for r in timed if r["photos"] >= MIN_PHOTOS and abs(raw[r["flight"]] - level) > limit]


def record(chunk, option: bool, datum: int | None, offsets: dict[int, float], keys_flights: dict[int, int]) -> dict:
    """``chunk.meta["gps_flights"]`` after a georeference: the pause that splits the flights, whether the option was
    on, the datum flight, the offsets (by flight and by camera), the errors per flight (:func:`flight_errors`) and the
    flights :func:`off_flights` points out."""
    rec = {"gap_s": GAP_S, "by_flight": bool(option), "datum": datum,
           "offsets": {str(f): float(o) for f, o in offsets.items()},
           "camera_offsets": {str(k): float(offsets[f]) for k, f in keys_flights.items()
                              if f in offsets and offsets[f] != 0.0}}
    chunk.meta[RECORD] = rec
    rec["flights"] = flight_errors(chunk)
    rec["suggest"] = off_flights(rec["flights"])
    return rec


def summary_lines(rec: dict | None) -> list[str]:
    """The record of :func:`record` as lines of text (English, for the command line): one per flight, the offsets
    and the note on the flights whose height is off. Empty with a single group of photos."""
    if not rec or len(rec.get("flights") or []) < 2:
        return []
    out = []
    for r in rec["flights"]:
        name = f"flight {r['flight']}" if r["flight"] != NO_TIME else "no time"
        mean = " / ".join(f"{v:+.2f}" for v in r["mean"])
        sd = " / ".join(f"{v:.2f}" for v in r["sd"])
        offset = f", height offset {r['offset']:+.2f} m" if r.get("offset") is not None else ""
        out.append(f"GPS {name}: {r['photos']} photos, mean dX/dY/dZ {mean} m, sd {sd} m, RMSE {r['rmse']:.2f} m"
                   + offset)
    if rec.get("offsets"):
        out.append("GPS height by flight: one vertical offset per flight estimated with the georeference"
                   + (f" (datum flight {rec['datum']})" if rec.get("datum") is not None
                      else " (the control points give the heights)"))
    elif rec.get("suggest"):
        out.append(f"note: the GPS height of flight(s) {', '.join(map(str, rec['suggest']))} is off from the others by "
                   f"more than {NOTE_FACTOR:g} times the scatter within a flight (a drone that starts its altitude "
                   "again with every battery): try --gps-height-by-flight (ppg align) or gps_height_by_flight=True")
    return out
