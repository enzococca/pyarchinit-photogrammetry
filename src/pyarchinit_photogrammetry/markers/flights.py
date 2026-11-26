"""The GPS of the photos flight by flight: the photos grouped by the time they were taken, the error of their positions
per flight, and one vertical offset per flight estimated with the georeference.

A drone survey flown on several batteries may carry a different height error on each flight: a DJI drone without RTK
starts its relative altitude again when it takes off with a new battery, so the heights of a whole flight can sit
metres above or below the others while its horizontal positions agree. The alignment places the photos by what they
see; the georeference on the GPS of the photos (:func:`.gcp.update_transform`) then meets a block whose flights do not
agree in height, and its error grows by metres. With :data:`OPTION` on (``chunk.meta["gps_height_by_flight"]``) the
fit estimates one vertical offset per flight together with the similarity (:func:`joint_fit`; GPS only: the biggest
flight is the datum, its offset zero; 1-2 control points with the GPS: every flight free); three or more control
points still place the chunk, and the offsets are then measured on the cameras and reported. Offsets the flights do
not determine are not applied (the plain fit, and the reason). The offsets are kept in ``chunk.meta["gps_flights"]``
with the transform they belong to (:func:`current`) and the camera errors (:func:`.gcp.camera_reference_errors`) are
measured against the references less the offset of their flight. The note on a flight whose heights are off comes
from the same joint model, run also with the option off.

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
NOTE_FACTOR = 3.0  # a flight whose offset is this many sd (within the flights) off the others is pointed out
NOTE_FLOOR = 1.0  # metres: a smaller offset between flights is the normal drift of a consumer GPS, never pointed out
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


SPREAD_FACTOR = 3.0  # a group of photos spans an area when its spread across is this many times their accuracy
# the joint solve is trusted when the condition number of its normal matrix, scaled to unit diagonal (a matrix of
# correlations, so units and the size of the site do not matter), is below this. Measured on synthetic surveys of
# 3 x 180 photos with GPS of 5 m: flights over the whole site ~10, one strip per battery ~90, one line per battery
# with 10 m of sway across ~800, with 5 m ~2700 (the tilt about the lines is then known to about a degree: metres of
# height across the site), exactly straight lines infinite. 1000 keeps the strips with a margin of ten and refuses
# the lines whose sway does not tell a tilt from an offset.
MAX_CONDITION = 1e3
MAX_ITERATIONS = 50


def _skew(v) -> np.ndarray:
    return np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])


def _rotation(omega) -> np.ndarray:
    """The rotation of the vector ``omega`` (Rodrigues)."""
    angle = float(np.linalg.norm(omega))
    if angle < 1e-15:
        return np.eye(3) + _skew(omega)
    k = _skew(np.asarray(omega, float) / angle)
    return np.eye(3) + np.sin(angle) * k + (1.0 - np.cos(angle)) * k @ k


def spans_area(xy: np.ndarray, accuracy: float) -> bool:
    """The points ``xy`` (n, 2) spread across their main direction more than :data:`SPREAD_FACTOR` times
    ``accuracy``: they span an area, not a line."""
    if len(xy) < 3:
        return False
    centred = xy - xy.mean(axis=0)
    axes = np.linalg.svd(centred, full_matrices=False)[2]
    return float(np.ptp(centred @ axes[-1])) > SPREAD_FACTOR * accuracy


def joint_fit(src, dst, weights, flight, datum: int | None, lengths=(), distances=(), bar_weights=()) -> dict:
    """The similarity ``dst = s R src + t`` and one vertical offset per flight (``+ offset[flight] * z``) solved
    together by weighted least squares (Gauss-Newton on scale, rotation, translation and offsets at once, from the
    plain similarity), with scale bars as further observations of the scale (as :func:`.gcp.similarity_with_bars`).
    ``flight`` gives the flight of every row: a negative number for a row never shifted (a control point),
    :data:`NO_TIME` for a photo without a time (never shifted); the ``datum`` flight is held at zero (None: every
    flight is free, the heights come from the rows never shifted).

    Returns ``{"transform", "offsets" {flight: offset, the datum 0.0}, "condition", "reason"}``: ``reason`` None when
    the offsets are determined and the solve converged, else why not (``"not determined: ..."``, ``"not converged"``)
    - the caller then keeps the plain fit. Determined means: some group of rows sharing one height (the rows never
    shifted, the datum, or a flight) spans an area in plan (:func:`spans_area`) - else a tilt about the lines is the
    same as the offsets - and the condition number of the normal matrix scaled to unit diagonal is below
    :data:`MAX_CONDITION`. The offset is how far the heights of the flight sit above where the fit puts its photos."""
    from ..core import geometry

    src, dst, w = (np.asarray(v, float) for v in (src, dst, weights))
    flight = np.asarray(flight, int)
    free = [f for f in sorted(set(flight.tolist())) if f > NO_TIME and f != datum]
    col = {f: i for i, f in enumerate(free)}
    accuracy = float(np.median(1.0 / np.sqrt(np.maximum(w, 1e-30))))
    groups = [~np.isin(flight, free)] + [flight == f for f in free]
    if not any(g.sum() >= 3 and spans_area(dst[g, :2], accuracy) for g in groups):
        reason = "not determined: no flight spans an area in plan (one line per flight: a tilt about the lines is " \
                 "the same as the offsets)"
        return {"transform": None, "offsets": {}, "condition": float("inf"), "reason": reason}
    # normalised coordinates: centred, unit spread (the normal matrix then compares like with like)
    wn = w / w.mean()
    ms, md = wn @ src / wn.sum(), wn @ dst / wn.sum()
    ss = float(np.sqrt(wn @ np.sum((src - ms) ** 2, axis=1) / wn.sum())) or 1.0
    sd = float(np.sqrt(wn @ np.sum((dst - md) ** 2, axis=1) / wn.sum())) or 1.0
    a, b = (src - ms) / ss, (dst - md) / sd
    d, dist, wb = (np.asarray(v, float) for v in (lengths, distances, bar_weights))
    wbn = wb / w.mean() if len(wb) else wb
    m0 = geometry.umeyama(a, b, with_scale=True, weights=wn)
    s0, rot, t = geometry.decompose_similarity(m0)
    log_s, t, off = float(np.log(s0)), np.asarray(t, float).copy(), np.zeros(len(free))
    k = 7 + len(free)
    sqw = np.sqrt(wn)
    rows_f = np.array([col.get(f, -1) for f in flight])
    converged, normal = False, None
    for _ in range(MAX_ITERATIONS):
        s = np.exp(log_s)
        p = s * a @ rot.T
        r = p + t - b
        shifted = rows_f >= 0
        r[shifted, 2] += off[rows_f[shifted]]
        n = len(a)
        jac = np.zeros((n, 3, k))
        jac[:, :, 0] = p
        jac[:, :, 1:4] = -np.stack([_skew(v) for v in p])
        jac[:, :, 4:7] = np.eye(3)
        jac[np.flatnonzero(shifted), 2, 7 + rows_f[shifted]] = 1.0
        jw = (jac * sqw[:, None, None]).reshape(-1, k)
        rw = (r * sqw[:, None]).reshape(-1)
        if len(d):  # the bars: (world scale * length - distance) / sd, in the units of the points
            scale_world = s * sd / ss
            rb = (scale_world * d - dist) / sd * np.sqrt(wbn)
            jb = np.zeros((len(d), k))
            jb[:, 0] = scale_world * d / sd * np.sqrt(wbn)
            jw, rw = np.vstack([jw, jb]), np.r_[rw, rb]
        normal = jw.T @ jw
        try:
            step = -np.linalg.solve(normal, jw.T @ rw)
        except np.linalg.LinAlgError:
            break
        log_s += step[0]
        rot = _rotation(step[1:4]) @ rot
        t = t + step[4:7]
        off = off + step[7:]
        if float(np.max(np.abs(step))) < 1e-12:
            converged = True
            break
    if normal is None:
        return {"transform": None, "offsets": {}, "condition": float("inf"), "reason": "not converged"}
    diag = np.sqrt(np.maximum(np.diag(normal), 1e-300))
    condition = float(np.linalg.cond(normal / np.outer(diag, diag)))
    if not np.isfinite(condition) or condition > MAX_CONDITION:
        reason = (f"not determined: the offsets and the tilt of the block trade off (condition number "
                  f"{condition:.3g} > {MAX_CONDITION:g})")
        return {"transform": None, "offsets": {}, "condition": condition, "reason": reason}
    if not converged:
        return {"transform": None, "offsets": {}, "condition": condition, "reason": "not converged"}
    s_world = float(np.exp(log_s)) * sd / ss
    transform = geometry.compose_similarity(s_world, rot, sd * t - s_world * rot @ ms + md)
    offsets = {f: float(sd * off[col[f]]) for f in free}
    if datum is not None:
        offsets[datum] = 0.0
    return {"transform": transform, "offsets": dict(sorted(offsets.items())), "condition": condition, "reason": None}


def offsets_against(transform, src, dst, weights, flight) -> dict[int, float]:
    """The vertical offset of every flight (not :data:`NO_TIME`) measured against a transform fitted without them
    (control points placed the chunk): the weighted mean of ``reference z - estimated z`` of its photos."""
    src, dst, w = (np.asarray(v, float) for v in (src, dst, weights))
    flight = np.asarray(flight, int)
    pred = src @ np.asarray(transform, float)[:3, :3].T + np.asarray(transform, float)[:3, 3]
    return {f: _weighted_mean(dst[flight == f, 2] - pred[flight == f, 2], w[flight == f])
            for f in sorted(set(flight.tolist())) if f > NO_TIME}


def residual_sd(transform, src, dst, flight, offsets: dict) -> float:
    """The pooled sd of the height residuals within the flights (each around its own mean) once ``transform`` and
    ``offsets`` are applied: the scatter of the GPS heights inside one flight."""
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    flight = np.asarray(flight, int)
    pred = src @ np.asarray(transform, float)[:3, :3].T + np.asarray(transform, float)[:3, 3]
    res = dst[:, 2] - pred[:, 2] - np.array([offsets.get(f, 0.0) for f in flight])
    total, dof = 0.0, 0
    for f in set(flight.tolist()):
        if f <= NO_TIME:
            continue
        r = res[flight == f]
        if len(r) > 1:
            total += float(np.sum((r - r.mean()) ** 2))
            dof += len(r) - 1
    return float(np.sqrt(total / dof)) if dof else 0.0


def off_flights(offsets: dict[int, float], photos: dict[int, int], sd_within: float) -> list[int]:
    """The flights whose height offset - from the joint model (:func:`joint_fit`: against the datum flight, the
    biggest, or against the control points when they take part) or measured against control points - is larger
    than ``max(NOTE_FACTOR x sd_within, NOTE_FLOOR)``: a flight whose GPS heights were measured from another zero.
    ``sd_within`` is the scatter of the heights within the flights once the offsets are applied
    (:func:`residual_sd`), not the one of all the photos, which the offset itself inflates; the floor keeps the
    normal drift of a consumer GPS between batteries quiet. Only flights of :data:`MIN_PHOTOS` photos or more, with
    two flights or more."""
    timed = [f for f in offsets if f != NO_TIME]
    if len(timed) < 2:
        return []
    limit = max(NOTE_FACTOR * sd_within, NOTE_FLOOR)
    return sorted(f for f in timed if photos.get(f, 0) >= MIN_PHOTOS and abs(offsets[f]) > limit)


def camera_offsets(chunk) -> dict[int, float]:
    """{camera key: vertical offset of its flight} of the last georeference (empty when none was estimated)."""
    return {int(k): float(v) for k, v in (current(chunk).get("camera_offsets") or {}).items()}


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
    offsets = current(chunk).get("offsets") or {}
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


def record(chunk, option: bool, datum: int | None, offsets: dict[int, float], keys_flights: dict[int, int]) -> dict:
    """``chunk.meta["gps_flights"]`` after a georeference: the pause that splits the flights, whether the option was
    on, the datum flight, the offsets (by flight and by camera), the transform they belong to (offsets of another
    transform are ignored: :func:`current`) and the errors per flight (:func:`flight_errors`). The caller adds
    ``suggest`` (the flights whose height looks off) and, when the option could not estimate them,
    ``by_flight_failed``."""
    rec = {"gap_s": GAP_S, "by_flight": bool(option), "datum": datum,
           "offsets": {str(f): float(o) for f, o in offsets.items()},
           "camera_offsets": {str(k): float(offsets[f]) for k, f in keys_flights.items()
                              if f in offsets and offsets[f] != 0.0},
           "transform": np.asarray(chunk.transform, float).reshape(-1).tolist()}
    chunk.meta[RECORD] = rec
    rec["flights"] = flight_errors(chunk)
    return rec


def current(chunk) -> dict:
    """``chunk.meta["gps_flights"]`` when it belongs to the transform the chunk has now, else an empty dict: a
    transform set outside the georeference (ties, the alignment of chunks, a hand orientation) leaves no stale
    offset applied."""
    rec = chunk.meta.get(RECORD) or {}
    stamp = rec.get("transform")
    if stamp is None:
        return rec  # written before the stamps (a project of this branch): taken as it is
    here = np.asarray(chunk.transform, float).reshape(-1)
    if len(stamp) != len(here) or not np.allclose(here, np.asarray(stamp, float), rtol=1e-12, atol=1e-9):
        return {}
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
    if rec.get("by_flight_failed"):
        out.append(f"GPS height by flight: offsets not determined ({rec['by_flight_failed']}); the plain fit is kept")
    elif rec.get("offsets"):
        if rec.get("measured"):
            out.append("GPS height by flight: the control points place the chunk; the offset of every flight is "
                       "measured on the cameras, not estimated")
        else:
            out.append("GPS height by flight: one vertical offset per flight estimated with the georeference"
                       + (f" (datum flight {rec['datum']})" if rec.get("datum") is not None
                          else " (every flight free: the control points give the heights)"))
    if rec.get("suggest") and not rec.get("by_flight"):
        out.append(f"note: the GPS height of flight(s) {', '.join(map(str, rec['suggest']))} is off from the others by "
                   f"more than {NOTE_FACTOR:g} times the scatter within a flight (and {NOTE_FLOOR:g} m; a drone that "
                   "starts its altitude again with every battery): try --gps-height-by-flight (ppg align, ppg "
                   "reference) or gps_height_by_flight=True")
    return out
