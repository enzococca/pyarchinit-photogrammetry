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
each round; the mean log gain stays zero, so the overall brightness does not drift.
:func:`correct` takes both away from a photo as it is read.
"""
from __future__ import annotations

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress
from .visibility import project_valid, read_photo, readable_cameras, sample

SAMPLES = 20000     # surface points used for the estimate
# colours are read on the photos decoded at a quarter of their size and blurred: misregistration and noise
# matter less, and decoding is quick
MIN_OBSERVATIONS = 50


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
    normalised radius and log colour (BGR) of every observation; points seen by fewer than two
    photos and saturated or black colours are left out."""
    progress = progress or Progress()
    cams = list(cams) if cams is not None else readable_cameras(chunk, skip_unreadable=True)[0]
    pts = _surface_points(chunk, samples)
    sensors = sorted({c.sensor_key for c in cams})
    s_index = {k: i for i, k in enumerate(sensors)}
    rows = []
    for i, cam in enumerate(cams):
        progress.check()
        px, _depth, ok = project_valid(chunk, cam, pts, margin=8.0)
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
            r = _radius(chunk.sensor(cam), px[idx])
            rows.append((idx, np.full(len(idx), i), np.full(len(idx), s_index[cam.sensor_key]), r, np.log(col)))
        progress((i + 1) / len(cams), f"Colours of the photos {i + 1}/{len(cams)}")
    if not rows:
        raise RuntimeError("no surface point is seen by the photos: build the dense cloud or align first")
    point, photo, sensor, radius, logc = (np.concatenate([r[k] for r in rows]) for k in range(5))
    seen = np.bincount(point, minlength=len(pts))
    keep = seen[point] >= 2
    return {"point": point[keep], "photo": photo[keep], "sensor": sensor[keep], "radius": radius[keep],
            "log": logc[keep], "cams": cams, "sensors": sensors, "points": len(pts)}


def _least_squares(point, photo, sensor, r, y, n_pts: int, n_cams: int, n_sens: int, falloff: bool):
    """Levels of the points, log gains of the photos and (``falloff``) a constant and the
    falloff coefficients of the sensors fitting ``y``, the gains summing to zero."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import lsqr

    m = len(y)
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
    gauge = np.full(n_cams, np.sqrt(m) / n_cams)  # sum of the log gains = 0, weighted like the data
    rows.append(np.full(n_cams, m))
    cols.append(n_pts + np.arange(n_cams))
    vals.append(gauge)
    a = coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(m + 1, n)).tocsr()
    x = lsqr(a, np.append(y, 0.0), damp=1e-6, atol=1e-10, btol=1e-10, iter_lim=5000)[0]
    return x[:n_pts], x[n_pts:n_pts + n_cams], (x[n_pts + n_cams:].reshape(n_sens, 3) if falloff else None)


def solve(obs: dict, rounds: int = 3, vignetting: bool = True) -> dict:
    """Gains (photo x BGR) and falloff coefficients (sensor x 3) from :func:`observations`:
    least squares on all the observations at once, outliers left out between rounds."""
    point, photo, sensor, r, logc = obs["point"], obs["photo"], obs["sensor"], obs["radius"], obs["log"]
    n_pts, n_cams, n_sens = obs["points"], len(obs["cams"]), len(obs["sensors"])
    use = np.ones(len(point), bool)
    level0 = _group_mean(point, logc, n_pts)
    rms_before = float(np.sqrt(np.nanmean((logc - level0[point]) ** 2)))
    vig = np.zeros((n_sens, 3))
    gains = np.zeros((n_cams, 3))
    levels = np.zeros((n_pts, 3))
    spread = np.array([np.ptp(r[sensor == s]) if (sensor == s).any() else 0.0 for s in range(n_sens)])
    for rnd in range(rounds):
        if vignetting:  # on the mean of the three channels
            _l, _g, v = _least_squares(point[use], photo[use], sensor[use], r[use], logc[use].mean(axis=1),
                                       n_pts, n_cams, n_sens, True)
            counts = np.bincount(sensor[use], minlength=n_sens)
            vig = np.where(((counts >= 3 * MIN_OBSERVATIONS) & (spread > 0.2))[:, None], v, 0.0)
        f = _falloff(vig[sensor].T, r)
        for c in range(3):
            levels[:, c], gains[:, c], _ = _least_squares(point[use], photo[use], sensor[use], r[use],
                                                           (logc[:, c] - f)[use], n_pts, n_cams, n_sens, False)
        counts = np.bincount(photo[use], minlength=n_cams)
        valid = counts >= MIN_OBSERVATIONS
        gains[~valid] = 0.0  # too few observations: left as it is
        if valid.any():
            gains[valid] -= gains[valid].mean(axis=0)
        res = logc - levels[point] - gains[photo] - f[:, None]
        if rnd < rounds - 1:
            err = np.abs(res).max(axis=1)
            mad = float(np.median(err[use])) if use.any() else 0.0
            use = err <= max(3.0 * 1.4826 * mad, 0.02)
    res = logc - levels[point] - gains[photo] - _falloff(vig[sensor].T, r)[:, None]
    return {"gains": np.exp(gains), "vignetting": vig, "rms_before": rms_before,
            "rms_after": float(np.sqrt(np.mean(res[use] ** 2))), "used": int(use.sum())}


def estimate(chunk: Chunk, samples: int = SAMPLES, vignetting: bool = True, occlusion: bool = True,
             progress: Progress | None = None) -> dict:
    """Measure the gains of the photos and the vignetting of the lenses, store them in
    ``chunk.meta["colour"]`` (used by the orthomosaic and the texture) and return them."""
    progress = progress or Progress()
    raycaster = None
    if occlusion:
        from .visibility import raycaster_for_chunk

        raycaster = raycaster_for_chunk(chunk)
    obs = observations(chunk, samples=samples, raycaster=raycaster, progress=progress.sub(0.0, 0.85))
    progress(0.86, f"Colour correction: {len(obs['point']):,} observations")
    sol = solve(obs, vignetting=vignetting)
    info = {
        "gains": {str(c.key): [round(float(v), 5) for v in sol["gains"][i]] for i, c in enumerate(obs["cams"])},
        "vignetting": {str(k): [round(float(v), 6) for v in sol["vignetting"][i]] for i, k in enumerate(obs["sensors"])},
        "observations": int(sol["used"]),
        "rms_before": round(sol["rms_before"], 4), "rms_after": round(sol["rms_after"], 4),
    }
    chunk.meta["colour"] = info
    progress(1.0, f"Colour correction: log-colour scatter {info['rms_before']:.3f} -> {info['rms_after']:.3f}")
    return info


class Correction:
    """The stored colour correction of a chunk, applied to photos as they are read."""

    def __init__(self, chunk: Chunk, info: dict | None = None):
        info = info if info is not None else (chunk.meta.get("colour") or {})
        self.chunk = chunk
        self.gains = {int(k): np.asarray(v, np.float32) for k, v in (info.get("gains") or {}).items()}
        self.vignetting = {int(k): np.asarray(v, np.float64) for k, v in (info.get("vignetting") or {}).items()}
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
            coarse = np.exp(-_falloff(coeffs, r)).astype(np.float32)
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
