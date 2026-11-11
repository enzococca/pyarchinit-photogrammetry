"""Colour correction between the photos of a chunk: exposure gains and lens vignetting (what
Metashape calls *Calibrate Colors*).

The photos of a survey differ in exposure (automatic exposure, passing clouds) and darken
towards their corners (vignetting): an orthomosaic or a texture made of several of them shows
where one photo ends and the next begins. :func:`estimate` measures both on surface points seen
by several photos - once the gain of each photo and the falloff of its lens are taken away, a
point has the same colour in every photo. For a point ``p`` seen by photo ``i`` at the
normalised radius ``r`` (distance from the principal point over the half diagonal), channel ``c``:

    log I_ic(p) = log L_c(p) + log g_ic + v_s(r),      v_s(r) = b1 r^2 + b2 r^4 + b3 r^6

with one falloff ``v_s`` per sensor, the same for the three channels. The unknowns are found by
alternating least squares with outliers (occlusions, highlights, moving objects) left out at
each round. The fit leaves one constant free (every photo brighter, every point darker): it is
set where the mosaic takes its colours - over points spread evenly on the DEM (or the model), the
correction of the photo each of them would be painted from is zero in the middle (the mean of
the middle half) - so the mosaic keeps the tone of the photos it is made of. The mean over all
the photos does not: the photos a mosaic prefers (the nadir ones, and those at the edges, which
paint most of it) are often exposed differently from the others, and the points of the cloud
miss the edges. A photo with too few observations takes the gain of its neighbours; a falloff the
observations do not hold stays zero, and is held constant beyond the radius it was measured to; a
correction that does not lower the colour scatter is not applied.
:meth:`Correction.correct` takes gain and falloff away from a photo as it is read.
"""
from __future__ import annotations

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress
from .visibility import project_valid, read_photo, readable_cameras, sample

SAMPLES = 20000     # surface points used for the estimate
# colours are read on the photos decoded at a quarter of their size and blurred: misregistration and noise
# matter less, and decoding is quick
MIN_OBSERVATIONS = 50   # of a photo, for its gain to be fitted
NEIGHBOURS = 4          # measured photos a photo with fewer observations takes its gain from
MIN_FALLOFF_OBSERVATIONS = 20 * MIN_OBSERVATIONS  # of a sensor, for its falloff to be fitted...
MIN_FALLOFF_SPREAD = 0.3  # ...over this range of radii (of the half diagonal) at least
MAX_FALLOFF = 1.5       # log: a fitted falloff beyond this (4.5 times) is not a lens, it is a fit without data
MIN_IMPROVEMENT = 0.02  # a correction that lowers the colour scatter by less than this is not applied


def _surface_points(chunk: Chunk, n: int, seed: int = 0) -> np.ndarray:
    """Up to ``n`` points of the surface (internal frame): the dense cloud without noise, else
    the tie points."""
    from ..core.pointcloud import NOISE_CLASSES

    pc = chunk.point_cloud
    xyz = None
    if pc is not None and len(pc):
        xyz = pc.xyz[pc.class_mask(exclude=NOISE_CLASSES)] if pc.classification is not None else pc.xyz
    if xyz is None or not len(xyz):
        tp = chunk.tie_points
        xyz = tp.xyz if tp is not None else np.zeros((0, 3))
    if len(xyz) > n:
        xyz = xyz[np.random.default_rng(seed).choice(len(xyz), n, replace=False)]
    return np.asarray(xyz, np.float64)


def _mosaic_points(chunk: Chunk, n: int, seed: int = 0) -> np.ndarray | None:
    """Up to ``n`` points spread evenly over the surface the orthomosaic and the texture are made on
    (internal frame): the cells of the DEM, else points on the faces of the model; ``None`` without
    either. The cloud does not do: it is denser where the photos overlap most, and it misses the
    edges of the survey, which a mosaic is mostly made of."""
    path = chunk.dem_path
    if path is not None and path.exists():
        import rasterio
        from rasterio.enums import Resampling

        with rasterio.open(path) as src:
            step = max(1.0, np.sqrt(src.width * src.height / n))
            h, w = max(1, int(src.height / step)), max(1, int(src.width / step))
            z = src.read(1, out_shape=(h, w), resampling=Resampling.nearest, masked=True)
            rr, cc = np.nonzero(~np.ma.getmaskarray(z) & np.isfinite(z.filled(np.nan)))
            col, row, t = (cc + 0.5) * src.width / w, (rr + 0.5) * src.height / h, src.transform
            x, y = t.a * col + t.b * row + t.c, t.d * col + t.e * row + t.f
        if len(rr):
            return chunk.to_internal(np.column_stack([x, y, z.filled(np.nan)[rr, cc]]).astype(np.float64))
    mesh = chunk.model
    if mesh is not None and len(mesh.faces):
        rng = np.random.default_rng(seed)
        tri = np.asarray(mesh.vertices, np.float64)[np.asarray(mesh.faces)]
        area = np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
        if area.sum() > 0:
            f = rng.choice(len(tri), n, p=area / area.sum())
            u, v = rng.random(n), rng.random(n)
            flip = u + v > 1
            u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
            return tri[f, 0] + u[:, None] * (tri[f, 1] - tri[f, 0]) + v[:, None] * (tri[f, 2] - tri[f, 0])
    return None


def _score(sensor, centre, up, pts, px, depth) -> np.ndarray:
    """How well a photo sees points, as :func:`engine.ortho._scores` ranks the photos of the
    orthomosaic: frontal, central, fine."""
    ray = pts - centre
    nadir = np.clip(-(ray @ up) / np.maximum(np.linalg.norm(ray, axis=1), 1e-12), 0, 1)
    off = np.hypot(px[:, 0] / sensor.width - 0.5, px[:, 1] / sensor.height - 0.5)
    return nadir ** 2 * (1 - off) * sensor.focal_px / np.maximum(depth, 1e-12)


def _radius(sensor, px: np.ndarray) -> np.ndarray:
    k = sensor.K
    half = 0.5 * np.hypot(sensor.width, sensor.height)
    return np.hypot(px[:, 0] - k[0, 2], px[:, 1] - k[1, 2]) / half


def _falloff(coeffs, r: np.ndarray) -> np.ndarray:
    r2 = r * r
    return coeffs[0] * r2 + coeffs[1] * r2 * r2 + coeffs[2] * r2 * r2 * r2


def _group_mean(index: np.ndarray, values: np.ndarray, n: int) -> np.ndarray:
    """Mean of ``values`` (m, c) by ``index`` (m) into ``n`` groups (nan for an empty group)."""
    count = np.bincount(index, minlength=n).astype(np.float64)
    out = np.column_stack([np.bincount(index, weights=values[:, c], minlength=n) for c in range(values.shape[1])])
    with np.errstate(invalid="ignore", divide="ignore"):
        return out / count[:, None]


def observations(chunk: Chunk, cams=None, samples: int = SAMPLES, raycaster=None,
                 progress: Progress | None = None) -> dict:
    """Colours of surface points in the photos that see them: point, photo and sensor indices,
    normalised radius, log colour (BGR) and score (how well the photo sees the point: what the
    orthomosaic ranks its photos by) of every observation, and the centres of the photos; points
    seen by fewer than two photos and saturated or black colours are left out. The points are
    those of the cloud and as many spread evenly over the DEM or the model (``mosaic``: which of
    the points these are, when there are any): the cloud alone misses the edges of the survey, and
    the photos there would have no observations."""
    progress = as_progress(progress)
    cams = list(cams) if cams is not None else readable_cameras(chunk, skip_unreadable=True)[0]
    pts = _surface_points(chunk, samples)
    over = _mosaic_points(chunk, samples)
    mosaic = np.zeros(len(pts) + (len(over) if over is not None else 0), bool)
    mosaic[len(pts):] = True
    if over is not None:
        pts = np.vstack([pts, over])
    sensors = sorted({c.sensor_key for c in cams})
    s_index = {k: i for i, k in enumerate(sensors)}
    up = np.linalg.inv(chunk.transform)[:3, :3] @ np.array([0.0, 0.0, 1.0])
    up /= np.linalg.norm(up)
    rows = []
    for i, cam in enumerate(cams):
        progress.check()
        px, depth, ok = project_valid(chunk, cam, pts, margin=8.0)
        idx = np.nonzero(ok)[0]
        if raycaster is not None and len(idx):
            idx = idx[raycaster.visible_from(cam.center, pts[idx], rel_tol=5e-3)]
        if len(idx):
            import cv2

            sensor_i = chunk.sensor(cam)
            small = cv2.GaussianBlur(read_photo(cam, cv2.IMREAD_REDUCED_COLOR_4), (3, 3), 0)  # decoded at 1/4
            scale = np.array([small.shape[1] / sensor_i.width, small.shape[0] / sensor_i.height])
            col = sample(small, px[idx] * scale).astype(np.float64)
            good = (col.min(axis=1) >= 6) & (col.max(axis=1) <= 249)
            idx, col = idx[good], col[good]
            r = _radius(sensor_i, px[idx])
            score = _score(sensor_i, cam.center, up, pts[idx], px[idx], depth[idx])
            rows.append((idx, np.full(len(idx), i), np.full(len(idx), s_index[cam.sensor_key]), r, np.log(col),
                         score))
        progress((i + 1) / len(cams), f"Colours of the photos {i + 1}/{len(cams)}")
    if not rows:
        raise RuntimeError("no surface point is seen by the photos: build the dense cloud or align first")
    point, photo, sensor, radius, logc, score = (np.concatenate([r[k] for r in rows]) for k in range(6))
    seen = np.bincount(point, minlength=len(pts))
    keep = seen[point] >= 2
    return {"point": point[keep], "photo": photo[keep], "sensor": sensor[keep], "radius": radius[keep],
            "log": logc[keep], "score": score[keep], "cams": cams, "sensors": sensors, "points": len(pts),
            "centres": np.array([c.center for c in cams], np.float64).reshape(len(cams), 3),
            "mosaic": mosaic if mosaic.any() else None}


def _least_squares(point, photo, sensor, r, y, n_pts: int, n_cams: int, n_sens: int, falloff: bool):
    """Log gains of the photos and (``falloff``) the falloff coefficients of the sensors fitting
    ``y``; the gains of the photos that have observations sum to zero, the others are zero. The
    levels of the points are not returned: they are the means of what is left (:func:`_scatter`)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import lsqr

    m = len(y)
    centre = float(y.mean()) if m else 0.0  # the constant goes to the levels: taken off before, not left to lsqr
    rows = [np.arange(m), np.arange(m)]
    cols = [point, n_pts + photo]
    vals = [np.ones(m), np.ones(m)]
    n = n_pts + n_cams
    if falloff:  # per sensor: r^2, r^4, r^6 (the constant is the level of the points)
        r2 = r * r
        for k, v in enumerate((r2, r2 * r2, r2 * r2 * r2)):
            rows.append(np.arange(m))
            cols.append(n + 3 * sensor + k)
            vals.append(v)
        n += 3 * n_sens
    # the gauge: sum of the log gains = 0, weighted like the data. Only over the photos that have
    # observations: a photo without any is a free unknown, and it would take the whole sum on itself
    # (the others then keep the constant that belongs to the levels)
    seen = np.nonzero(np.bincount(photo, minlength=n_cams))[0]
    rows.append(np.full(len(seen), m))
    cols.append(n_pts + seen)
    vals.append(np.full(len(seen), np.sqrt(m) / max(len(seen), 1)))
    a = coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(m + 1, n)).tocsr()
    x = lsqr(a, np.append(y - centre, 0.0), damp=1e-6, atol=1e-10, btol=1e-10, iter_lim=5000)[0]
    return x[n_pts:n_pts + n_cams], (x[n_pts + n_cams:].reshape(n_sens, 3) if falloff else None)


def _scatter(point, values, sel, n_pts: int):
    """What is left of ``values`` (m, 3) once the mean of every point over the observations ``sel``
    is taken away (nan for the points without any), and those means (the levels of the points)."""
    levels = _group_mean(point[sel], values[sel], n_pts)
    return values - levels[point], levels


def _rms(res) -> float:
    return float(np.sqrt(np.mean(res ** 2))) if res.size else 0.0


def _held(coeffs, r_max: float) -> bool:
    """Whether a fitted falloff is one a lens can have: within ``MAX_FALLOFF`` up to the largest
    radius it was measured to."""
    return bool(np.abs(_falloff(coeffs, np.linspace(0.0, r_max, 21))).max() <= MAX_FALLOFF)


def _middle(values: np.ndarray) -> np.ndarray:
    """Mean of the middle half of every column: where most of the values are, like a median, and
    between the two when they come in two groups (photos exposed in two ways), where a median jumps
    to one of them."""
    ordered = np.sort(values, axis=0)
    quarter = len(ordered) // 4
    return ordered[quarter:len(ordered) - quarter].mean(axis=0)


def _preferred(point, photo, radius, score, sel):
    """Index of the observation of every point (among ``sel``) the mosaic would take its colour
    from: the best score, or without scores the photo that sees the point nearest to its centre."""
    idx = np.nonzero(sel)[0]
    order = idx[np.lexsort((radius[idx] if score is None else -score[idx], point[idx]))]
    return order[np.r_[True, np.diff(point[order]) > 0]] if len(order) else order


def _from_neighbours(gains, measured, centres):
    """Gains for the photos that were not measured: the mean of the nearest measured photos (the
    same pass, the same light), or the middle of all of them when the positions are not known."""
    out = np.zeros((len(gains), 3))
    if not measured.any():
        return out
    if centres is None:
        out[:] = np.median(gains[measured], axis=0)
        return out
    known = np.nonzero(measured)[0]
    for i in np.nonzero(~measured)[0]:
        d = np.linalg.norm(centres[known] - centres[i], axis=1)
        out[i] = gains[known[np.argsort(d)[:NEIGHBOURS]]].mean(axis=0)
    return out


def solve(obs: dict, rounds: int = 3, vignetting: bool = True) -> dict:
    """Gains (photo x BGR) and falloff coefficients (sensor x 3) from :func:`observations`:
    least squares on all the observations at once, outliers left out between rounds.

    Only the photos with ``MIN_OBSERVATIONS`` or more are fitted (``measured``); the others take
    the gain of their neighbours (``obs["centres"]``), moved towards what their few observations
    say. The constant the fit leaves free is set where the mosaic takes its colours: over the
    points spread evenly on its surface (``obs["mosaic"]``; without them, all the points) the
    correction of the photo each of them is painted from (the best ``obs["score"]``; without
    scores, the photo that sees the point nearest to its centre) is zero in the middle
    (:func:`_middle`). ``rms_before`` and ``rms_after`` are the scatter of the same observations around the
    mean of their point, without and with the correction; a correction that does not lower it by
    ``MIN_IMPROVEMENT`` is not applied (``applied`` False, gains 1, no falloff)."""
    point, photo, sensor, r, logc = obs["point"], obs["photo"], obs["sensor"], obs["radius"], obs["log"]
    n_pts, n_cams, n_sens = obs["points"], len(obs["cams"]), len(obs["sensors"])
    score, centres = obs.get("score"), obs.get("centres")
    use = np.ones(len(point), bool)
    vig = np.zeros((n_sens, 3))
    reach = np.zeros(n_sens)  # the radius the falloff of each sensor is measured to
    gains = np.zeros((n_cams, 3))
    measured = np.zeros(n_cams, bool)
    fit = np.zeros(len(point), bool)
    f = np.zeros(len(point))
    for rnd in range(rounds):
        measured = np.bincount(photo[use], minlength=n_cams) >= MIN_OBSERVATIONS
        fit = use & measured[photo]  # the few observations of the other photos say nothing sure
        if not fit.any():
            break
        if vignetting:  # on the mean of the three channels
            _g, v = _least_squares(point[fit], photo[fit], sensor[fit], r[fit], logc[fit].mean(axis=1),
                                   n_pts, n_cams, n_sens, True)
            vig[:] = 0.0
            for s in range(n_sens):
                rs = r[fit & (sensor == s)]
                if len(rs) < MIN_FALLOFF_OBSERVATIONS:
                    continue
                lo, hi = np.percentile(rs, [2, 98])
                if hi - lo >= MIN_FALLOFF_SPREAD and _held(v[s], hi):
                    vig[s], reach[s] = v[s], hi
        f = _falloff(vig[sensor].T, np.minimum(r, np.where(reach > 0, reach, 1.0)[sensor]))
        gains[:] = 0.0
        for c in range(3):
            gains[:, c], _ = _least_squares(point[fit], photo[fit], sensor[fit], r[fit], (logc[:, c] - f)[fit],
                                            n_pts, n_cams, n_sens, False)
        if rnd < rounds - 1:
            res, _levels = _scatter(point, logc - gains[photo] - f[:, None], fit, n_pts)
            err = np.abs(res).max(axis=1)
            mad = float(np.median(err[fit]))
            judged = measured[photo] & np.isfinite(err)
            use = np.where(judged, err <= max(3.0 * 1.4826 * mad, 0.02), use)
    # the photos that were not measured: what their neighbours have, and their own few observations
    res, levels = _scatter(point, logc - gains[photo] - f[:, None], fit, n_pts)
    filled = _from_neighbours(gains, measured, centres)
    own = use & ~measured[photo] & np.isfinite(res).all(axis=1)
    count = np.bincount(photo[own], minlength=n_cams)
    for i in np.nonzero(count)[0]:
        mine = np.median(res[own & (photo == i)], axis=0)
        filled[i] = (count[i] * mine + MIN_OBSERVATIONS * filled[i]) / (count[i] + MIN_OBSERVATIONS)
    gains[~measured] = filled[~measured]
    # the gauge: nothing changes, in the middle, where the mosaic takes its colours
    on = use & obs["mosaic"][point] if obs.get("mosaic") is not None else use
    best = _preferred(point, photo, r, score, on if on.any() else use)
    if len(best):
        gains -= _middle(gains[photo[best]] + f[best, None])
    before, _l = _scatter(point, logc, fit, n_pts)
    rms_before, rms_after = _rms(before[fit]), _rms(res[fit])
    applied = bool(fit.any() and rms_after <= (1.0 - MIN_IMPROVEMENT) * rms_before)
    if not applied:
        gains[:], vig[:], reach[:] = 0.0, 0.0, 0.0
    return {"gains": np.exp(gains), "vignetting": vig, "vignetting_radius": np.where(vig.any(axis=1), reach, 0.0),
            "rms_before": rms_before, "rms_after": min(rms_after, rms_before), "used": int(fit.sum()),
            "measured": measured, "applied": applied}


def estimate(chunk: Chunk, samples: int = SAMPLES, vignetting: bool = True, occlusion: bool = True,
             progress: Progress | None = None) -> dict:
    """Measure the gains of the photos and the vignetting of the lenses, store them in
    ``chunk.meta["colour"]`` (used by the orthomosaic and the texture) and return them. A
    correction that does not lower the colour scatter is not applied: ``applied`` False, no gains
    and no falloff (the orthomosaic and the texture then use the photos as they are)."""
    progress = as_progress(progress)
    raycaster = None
    if occlusion:
        from .visibility import raycaster_for_chunk

        raycaster = raycaster_for_chunk(chunk)
    obs = observations(chunk, samples=samples, raycaster=raycaster, progress=progress.sub(0.0, 0.85))
    progress(0.86, f"Colour correction: {len(obs['point']):,} observations")
    sol = solve(obs, vignetting=vignetting)
    applied = sol["applied"]
    cams, sensors = (obs["cams"], obs["sensors"]) if applied else ((), ())
    info = {
        "gains": {str(c.key): [round(float(v), 5) for v in sol["gains"][i]] for i, c in enumerate(cams)},
        "vignetting": {str(k): [round(float(v), 6) for v in sol["vignetting"][i]] for i, k in enumerate(sensors)},
        # the falloff is measured up to this radius and held beyond it
        "vignetting_radius": {str(k): round(float(sol["vignetting_radius"][i]), 4) for i, k in enumerate(sensors)},
        "observations": int(sol["used"]),
        "rms_before": round(sol["rms_before"], 4), "rms_after": round(sol["rms_after"], 4),
        "applied": applied,
        # the photos with too few observations: their gain comes from their neighbours
        "from_neighbours": [c.key for i, c in enumerate(cams) if not sol["measured"][i]],
    }
    chunk.meta["colour"] = info
    if applied:
        progress(1.0, f"Colour correction: log-colour scatter {info['rms_before']:.3f} -> {info['rms_after']:.3f}"
                 + (f"; {len(info['from_neighbours'])} photos with too few observations take the gain of their "
                    "neighbours" if info["from_neighbours"] else ""))
    else:
        progress(1.0, f"Colour correction not applied: it does not lower the log-colour scatter "
                      f"({info['rms_before']:.3f})")
    return info


class Correction:
    """The stored colour correction of a chunk, applied to photos as they are read."""

    def __init__(self, chunk: Chunk, info: dict | None = None):
        info = info if info is not None else (chunk.meta.get("colour") or {})
        self.chunk = chunk
        self.gains = {int(k): np.asarray(v, np.float32) for k, v in (info.get("gains") or {}).items()}
        self.vignetting = {int(k): np.asarray(v, np.float64) for k, v in (info.get("vignetting") or {}).items()}
        # the radius the falloff was measured to: held beyond it (a polynomial runs away where it has no data)
        self.radius = {int(k): float(v) for k, v in (info.get("vignetting_radius") or {}).items() if v}
        self._maps: dict = {}

    def __bool__(self) -> bool:
        return bool(self.gains or self.vignetting)

    def _factor(self, sensor, shape) -> np.ndarray | None:
        """Per-pixel factor that takes the falloff away (h x w float32), ``None`` when there is none."""
        coeffs = self.vignetting.get(sensor.key)
        if coeffs is None or not np.any(coeffs):
            return None
        key = (sensor.key, shape)
        if key not in self._maps:
            import cv2

            h, w = shape
            step = 8  # computed on a coarse grid, then enlarged: the falloff is smooth
            yy, xx = np.mgrid[0:h:step, 0:w:step].astype(np.float64)
            k = sensor.K
            sx, sy = w / sensor.width, h / sensor.height  # a photo read at another size
            r = np.hypot((xx + 0.5) / sx - k[0, 2], (yy + 0.5) / sy - k[1, 2]) / (0.5 * np.hypot(sensor.width, sensor.height))
            coarse = np.exp(-_falloff(coeffs, np.minimum(r, self.radius.get(sensor.key, np.inf)))).astype(np.float32)
            self._maps[key] = cv2.resize(coarse, (w, h), interpolation=cv2.INTER_LINEAR)
        return self._maps[key]

    def correct(self, cam, img: np.ndarray) -> np.ndarray:
        """The photo of ``cam`` (BGR uint8) with its gain and its lens falloff taken away."""
        gain = self.gains.get(cam.key)
        factor = self._factor(self.chunk.sensor(cam), img.shape[:2])
        if gain is None and factor is None:
            return img
        out = img.astype(np.float32)
        if factor is not None:
            out *= factor[..., None]
        if gain is not None:
            out /= gain[None, None, :]
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)
