"""Coregistration of DEMs: two surveys of the same ground brought onto each other.

Surveys of different days are never perfectly registered: a shift of a few centimetres or a slight
tilt between two DEMs becomes false excavation and fill on every slope. :func:`coregister` moves a
DEM onto a reference over the ground that did not change between the two.

**Stable ground**: the cells present in both DEMs, not steeper than ``max_slope`` (walls and the
sides of a trench are where DEMs are least reliable), inside ``stable`` and outside ``exclude`` (the
excavation areas), without the cells that changed - further than ``k`` NMAD from the median
difference. The changed cells are looked for again at every iteration, on the differences left by
the shift found so far (cells left out early come back once the DEMs agree better).

**The model** of Nuth & Kääb (2011): on ground of slope ``α`` facing the aspect ``ψ``, a DEM that
shows the ground shifted by a length ``a`` towards the azimuth ``b`` differs from the reference by
``dh = a cos(b - ψ) tan α`` (their ``dh / tan α = a cos(b - ψ) + c``), that is ``-∇z · (dx, dy)``
with the slope of the reference. The two components of the shift are fitted by least squares
(Huber weights) on the stable cells *together with* the vertical bias and, with ``tilt``, a plane:
fitted one after the other, a tilt would be taken for a shift on a hill whose sides face the
directions of the tilt (a tell, a mound), and the two would take many iterations to part. The DEM
is then shifted by what was found (bilinear interpolation) and the fit done again, until the shift
changes by less than ``tolerance`` cells or after ``max_iter`` iterations. The first iteration
leaves no cell out and fits no tilt: the shift is still in the differences, and a tilt would take
in part of a wide shallow change (a surface trowelled down by a couple of centimetres). At the end
the vertical bias (median) and the tilt (robust plane) are measured again on the shifted DEM.

On flat ground, or where the slopes all face one way (one plane, the two sides of a straight
valley along it), a horizontal shift cannot be told from a vertical one, and changes no volume: a
direction along which the slopes of the stable ground, beyond what a plane explains, spread by less
than :data:`MIN_SLOPE` degrees (or a tenth of their root mean square) is not measured, the shift
along it stays 0 and ``horizontal`` is False. When the shift does not settle within ``max_iter``
iterations (DEMs further apart than their slopes can tell, or not the same ground), the state of
the iterations with which the DEMs agreed best is kept if it is clearly better (:data:`CLEARLY`)
than the DEM as it is, else the DEM is not shifted; ``converged`` is False either way.

The result (:class:`Coregistration`) says where the DEM was - ``dx``, ``dy``: it showed the ground
that far east and north of the reference; ``dz``: that much higher at ``centre``; ``tilt_x``,
``tilt_y``: metres per metre - and how well the two DEMs agree on the stable ground before and after
(NMAD, RMSE, median). The coregistered DEM is ``z(x + dx, y + dy) - (dz + tilt_x (x - x0) +
tilt_y (y - y0))`` written on the grid of the input DEM (float32, NaN where empty).

Large DEMs are measured on a regular sample of at most :data:`MAX_SAMPLES` cells of the reference
(their slopes from the full-resolution neighbours); the whole DEM is then shifted.

:func:`coregister_chunk` coregisters the DEM of a chunk onto the DEM of another chunk (or a kept
version, or a file) and keeps the result as the active version of its DEM, the DEM as it was among
the versions; :mod:`.timeline` coregisters the phases of an excavation before measuring volumes.

``method="xdem"`` uses the library xdem (``pip install pyarchinit-photogrammetry[coreg]``:
``NuthKaab`` and, with ``tilt``, ``Deramp``) if it can be imported; ``"auto"`` and ``"nuth_kaab"``
are the implementation of this module.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from ..core.progress import Progress, as_progress

METHODS = ("auto", "nuth_kaab", "xdem")
MAX_SLOPE = 60.0  # degrees: steeper cells (walls, the sides of a trench) are not stable ground
MIN_SLOPE = 3.0  # degrees: least spread of the slopes along a direction for a shift along it to be measured
OUTLIER_K = 3.0  # NMAD from the median difference: further, the ground changed
MIN_THRESHOLD = 0.001  # m: the least outlier threshold (DEMs equal to the float precision)
MIN_CELLS = 100  # stable cells needed
TOLERANCE = 0.01  # cells: the shift is final when an iteration moves it by less
CLEARLY = 0.1  # a shift that never settles is kept only if the NMAD went down by this share at least
MAX_SAMPLES = 2_000_000  # cells of the reference used to measure (a regular sample of larger DEMs)
HUBER = 1.345  # NMAD: the Huber weights of the fits
IRLS_ROUNDS = 5  # reweighted least squares of each fit
BLOCK_ROWS = 512  # rows of a DEM written or read at a time
NMAD_FACTOR = 1.4826


@dataclass
class Coregistration:
    """What :func:`coregister` found (see the module docstring); lengths in metres."""

    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0
    tilt_x: float = 0.0
    tilt_y: float = 0.0
    centre: tuple[float, float] = (0.0, 0.0)
    nmad_before: float = float("nan")
    nmad_after: float = float("nan")
    rmse_before: float = float("nan")
    rmse_after: float = float("nan")
    median_before: float = float("nan")
    median_after: float = float("nan")
    cells: int = 0  # stable cells at the end (of the sample)
    compared: int = 0  # cells of the sample in both DEMs, not too steep, inside the areas given
    step: int = 1  # one cell every ``step`` rows and columns of the reference was used
    resolution: float = 0.0  # of the reference
    iterations: int = 0
    converged: bool = False
    horizontal: bool = True  # False: flat or one-sided ground, the shift that cannot be measured left at 0
    tilt: bool = True
    method: str = "nuth_kaab"
    reference: str = ""
    dem: str = ""
    path: str | None = None  # the coregistered DEM, when written
    stable_index: tuple | None = field(default=None, repr=False)  # rows, cols of the stable cells (reference grid)

    @property
    def shift(self) -> float:
        """Length of the horizontal shift (m)."""
        return float(math.hypot(self.dx, self.dy))

    def correction(self, x, y):
        """The height taken away at ``(x, y)`` once the DEM is shifted: ``dz`` plus the tilt."""
        return self.dz + self.tilt_x * (np.asarray(x, float) - self.centre[0]) + self.tilt_y * (
            np.asarray(y, float) - self.centre[1])

    def to_dict(self) -> dict:
        """JSON values (the stable cells left out)."""
        out = {}
        for k in ("dx", "dy", "dz", "tilt_x", "tilt_y", "nmad_before", "nmad_after", "rmse_before", "rmse_after",
                  "median_before", "median_after", "resolution"):
            v = float(getattr(self, k))
            out[k] = v if math.isfinite(v) else None
        out.update(shift=self.shift, centre=[float(self.centre[0]), float(self.centre[1])], cells=int(self.cells),
                   compared=int(self.compared), step=int(self.step), iterations=int(self.iterations),
                   converged=bool(self.converged), horizontal=bool(self.horizontal), tilt=bool(self.tilt),
                   method=self.method, reference=self.reference, dem=self.dem, path=self.path)
        return out


# ---------------------------------------------------------------- rasters
def _read(path):
    """``(heights float32 with NaN, transform, crs)``: the empty cells (no-data value or mask of the
    file) as NaN, without the copies of a masked array (a DEM can have 150 million cells)."""
    import rasterio
    from rasterio.enums import MaskFlags

    with rasterio.open(path) as src:
        z = src.read(1, out_dtype="float32")
        nodata = src.nodata
        if nodata is not None and not math.isnan(nodata):
            z[z == np.float32(nodata)] = np.nan
        if MaskFlags.per_dataset in src.mask_flag_enums[0] and MaskFlags.nodata not in src.mask_flag_enums[0]:
            z[src.read_masks(1) == 0] = np.nan  # an internal mask
        return z, src.transform, src.crs


def _check(tr_a, tr_b, crs_a, crs_b) -> None:
    for tr in (tr_a, tr_b):
        if tr.b != 0 or tr.d != 0:
            raise ValueError("rotated rasters cannot be coregistered: write them north up first")
    if crs_a and crs_b and crs_a != crs_b:
        raise ValueError(f"the two DEMs are in different coordinate systems ({crs_a} and {crs_b})")


def bilinear(z: np.ndarray, transform, x, y, strict: bool = True) -> np.ndarray:
    """Heights of the grid ``z`` (``transform`` of a north-up raster, cell centres at +0.5) at the
    points ``x, y`` by bilinear interpolation; NaN outside. ``strict``: NaN as soon as one of the
    four cells around is empty, else the empty ones are left out when the others weigh at least a
    half."""
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    fc = (x - transform.c) / transform.a - 0.5
    fr = (y - transform.f) / transform.e - 0.5
    c0, r0 = np.floor(fc), np.floor(fr)
    t, s = fc - c0, fr - r0
    h, w = z.shape
    c0, r0 = c0.astype(np.int64), r0.astype(np.int64)
    edge_c, edge_r = (c0 == w - 1) & (t == 0), (r0 == h - 1) & (s == 0)  # exactly on the last centre
    c0, t = np.where(edge_c, c0 - 1, c0), np.where(edge_c, 1.0, t)
    r0, s = np.where(edge_r, r0 - 1, r0), np.where(edge_r, 1.0, s)
    inside = (c0 >= 0) & (r0 >= 0) & (c0 + 1 < w) & (r0 + 1 < h)
    out = np.full(x.shape, np.nan)
    if not inside.any():
        return out
    c0, r0, t, s = c0[inside], r0[inside], t[inside], s[inside]
    corners = [(z[r0, c0], (1 - s) * (1 - t)), (z[r0, c0 + 1], (1 - s) * t), (z[r0 + 1, c0], s * (1 - t)),
               (z[r0 + 1, c0 + 1], s * t)]
    if strict:
        out[inside] = sum(v.astype(np.float64) * wt for v, wt in corners)
        return out
    num = sum(np.where(np.isfinite(v), v, 0.0) * wt for v, wt in corners)
    den = sum(np.isfinite(v) * wt for v, wt in corners)
    with np.errstate(invalid="ignore", divide="ignore"):
        out[inside] = np.where(den >= 0.5, num / den, np.nan)
    return out


def _shifted_rows(z: np.ndarray, r0: int, r1: int, off_r: float, off_c: float) -> np.ndarray:
    """Rows ``r0:r1`` of ``z`` read ``off_r`` rows and ``off_c`` columns further (bilinear, the
    empty cells left out when the others weigh at least a half): a translation of the grid."""
    h, w = z.shape
    fr, fc = int(math.floor(off_r)), int(math.floor(off_c))
    s, t = off_r - fr, off_c - fc
    n = r1 - r0
    win = np.full((n + 1, w + 1), np.nan, np.float32)  # win[a, b] = z[r0 + fr + a, fc + b]
    sr0, sc0 = r0 + fr, fc
    ar0, ar1 = max(sr0, 0), min(sr0 + n + 1, h)
    ac0, ac1 = max(sc0, 0), min(sc0 + w + 1, w)
    if ar1 > ar0 and ac1 > ac0:
        win[ar0 - sr0:ar1 - sr0, ac0 - sc0:ac1 - sc0] = z[ar0:ar1, ac0:ac1]
    valid = np.isfinite(win)
    v = np.where(valid, win, 0.0).astype(np.float64)
    num = np.zeros((n, w))
    den = np.zeros((n, w))
    for wt, rows, cols in (((1 - s) * (1 - t), slice(0, n), slice(0, w)), ((1 - s) * t, slice(0, n), slice(1, w + 1)),
                           (s * (1 - t), slice(1, n + 1), slice(0, w)), (s * t, slice(1, n + 1), slice(1, w + 1))):
        if wt > 0:
            num += wt * v[rows, cols]
            den += wt * valid[rows, cols]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den >= 0.5 - 1e-9, num / den, np.nan)


def _profile(shape, transform, crs) -> dict:
    """A float32 GeoTIFF with NaN where empty, tiled and deflated, BigTIFF when needed."""
    h, w = shape
    prof = {"driver": "GTiff", "height": h, "width": w, "count": 1, "dtype": "float32", "transform": transform,
            "nodata": float("nan"), "compress": "deflate", "BIGTIFF": "IF_SAFER"}
    if crs:
        prof["crs"] = crs
    if h >= 256 and w >= 256:
        prof.update(tiled=True, blockxsize=256, blockysize=256)
    return prof


def _overviews(dst, shape) -> None:
    from rasterio.enums import Resampling

    factors = [f for f in (2, 4, 8, 16, 32) if min(shape) // f >= 64]
    if factors:
        dst.build_overviews(factors, Resampling.average)


def _write(path: Path, z: np.ndarray, transform, crs, dx: float, dy: float, plane, centre,
           progress: Progress) -> Path:
    """The coregistered DEM on the grid of ``z`` (see the module docstring)."""
    import rasterio
    from rasterio.windows import Window

    h, w = z.shape
    off_c, off_r = dx / transform.a, dy / transform.e
    u = transform.c + (np.arange(w) + 0.5) * transform.a - centre[0]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **_profile(z.shape, transform, crs)) as dst:
        for r0 in range(0, h, BLOCK_ROWS):
            progress.check()
            r1 = min(h, r0 + BLOCK_ROWS)
            block = _shifted_rows(z, r0, r1, off_r, off_c)
            v = transform.f + (np.arange(r0, r1) + 0.5) * transform.e - centre[1]
            block -= plane[0] + plane[1] * u[None, :] + plane[2] * v[:, None]
            dst.write(block.astype(np.float32), 1, window=Window(0, r0, w, r1 - r0))
            progress(r1 / h, "Coregistered DEM")
        _overviews(dst, z.shape)
    return path


# ---------------------------------------------------------------- measuring
def _sample(z: np.ndarray, max_samples: int):
    """Rows and columns of the cells used to measure: every cell with its 3 x 3 neighbours, or one
    every ``step`` rows and columns of a large DEM."""
    h, w = z.shape
    n = int(np.count_nonzero(np.isfinite(z)))
    step = max(1, int(math.ceil(math.sqrt(n / max_samples)))) if n > max_samples else 1
    rr, cc = np.meshgrid(np.arange(1, h - 1, step), np.arange(1, w - 1, step), indexing="ij")
    rr, cc = rr.ravel(), cc.ravel()
    keep = np.isfinite(z[rr, cc])
    return rr[keep], cc[keep], step


def _gradient(z: np.ndarray, rr, cc, east: float, north: float):
    """East and north gradient of ``z`` at the cells ``rr, cc`` (Horn's 3 x 3 weights); ``east``,
    ``north``: the size of a cell along +x and along the rows towards the north."""
    def at(dr, dc):
        return z[rr + dr, cc + dc].astype(np.float64)

    a, b, c = at(-1, -1), at(-1, 0), at(-1, 1)
    d, f = at(0, -1), at(0, 1)
    g, hh, i = at(1, -1), at(1, 0), at(1, 1)
    gx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * east)
    gy = ((a + 2 * b + c) - (g + 2 * hh + i)) / (8 * north)
    return gx, gy


def _areas(stable, exclude, x, y, rr, cc, shape) -> np.ndarray:
    """The cells (at ``x, y``; ``rr, cc`` on the grid of the reference) inside ``stable`` (a
    geometry, or a boolean grid of the reference) and outside ``exclude`` (geometries)."""
    keep = np.ones(np.shape(x), bool)
    for geom, inside in ((stable, True), (exclude, False)):
        if geom is None:
            continue
        if isinstance(geom, np.ndarray):
            if geom.shape != tuple(shape):
                raise ValueError(f"the mask of the stable ground must have the shape of the reference DEM {shape}")
            hit = geom.astype(bool)[rr, cc]
        else:
            import shapely

            g = geom
            if isinstance(g, (list, tuple)):
                g = shapely.union_all([p for p in g if p is not None and not p.is_empty]) if g else None
            if g is None or g.is_empty:
                continue
            shapely.prepare(g)
            hit = shapely.contains_xy(g, x, y)
        keep &= hit if inside else ~hit
    return keep


def _nmad(d: np.ndarray, med: float | None = None) -> float:
    if d.size == 0:
        return float("nan")
    med = float(np.median(d)) if med is None else med
    return float(NMAD_FACTOR * np.median(np.abs(d - med)))


def _inliers(dh: np.ndarray, ok: np.ndarray, k: float, rounds: int = 10):
    """The cells of ``ok`` within ``k`` NMAD of the median of ``dh`` (median and NMAD taken again
    on those left, until nothing changes): ``(mask, median, nmad)``."""
    m = ok.copy()
    med = nmad = float("nan")
    for _ in range(rounds):
        d = dh[m]
        if d.size == 0:
            break
        med = float(np.median(d))
        nmad = _nmad(d, med)
        new = ok & (np.abs(dh - med) <= max(k * nmad, MIN_THRESHOLD))
        if np.array_equal(new, m):
            break
        m = new
    return m, med, nmad


def _fit(dh: np.ndarray, sx, sy, u: np.ndarray, v: np.ndarray, tilt: bool, shift: bool = True):
    """Least squares with Huber weights of ``dh = sx ddx + sy ddy + a0 (+ ax u + ay v)`` (``sx``, ``sy``:
    minus the slope of the reference east and north; ``u``, ``v``: metres from the centre): ``(ddx,
    ddy, (a0, ax, ay), measured)``. The shift only along the directions the ground measures (see
    the module docstring; ``measured``: both); without shift and tilt the bias is the median."""
    n = dh.size
    scale = max(float(np.abs(u).max()), float(np.abs(v).max()), 1e-9) if n else 1.0
    b = np.column_stack([np.ones(n), u / scale, v / scale]) if tilt else np.ones((n, 1))
    dirs = np.zeros((0, 2))
    measured = shift
    if shift:
        s = np.column_stack([sx, sy])
        rest = s - b @ np.linalg.lstsq(b.T @ b, b.T @ s, rcond=None)[0]  # what a plane cannot take from the shift
        ev, vec = np.linalg.eigh(rest.T @ rest / max(n, 1))
        spread = np.sqrt(np.clip(ev, 0.0, None))
        keep = spread >= max(math.tan(math.radians(MIN_SLOPE)), 0.1 * math.sqrt(float(np.mean(sx ** 2 + sy ** 2))))
        dirs = vec[:, keep].T
        measured = bool(keep.all())
    med = float(np.median(dh)) if n else 0.0
    if not len(dirs) and not tilt:
        return 0.0, 0.0, np.array([med, 0.0, 0.0]), measured
    x = np.column_stack([np.column_stack([sx, sy]) @ dirs.T, b]) if len(dirs) else b
    r = dh - med  # the first weights from the median
    coef = np.zeros(x.shape[1])
    for _ in range(IRLS_ROUNDS):
        c = HUBER * max(_nmad(r), 1e-9)
        w = np.minimum(1.0, c / np.maximum(np.abs(r), 1e-300))
        xw = x * w[:, None]
        coef = np.linalg.lstsq(xw.T @ x, xw.T @ dh, rcond=None)[0]
        r = dh - x @ coef
    nd = len(dirs)
    move = dirs.T @ coef[:nd] if nd else np.zeros(2)
    p = coef[nd:]
    plane = np.array([p[0], p[1] / scale, p[2] / scale]) if tilt else np.array([p[0], 0.0, 0.0])
    return float(move[0]), float(move[1]), plane, measured


class _Measure:
    """The sample of the reference with its slopes, and where the ground may be stable."""

    def __init__(self, zr, tr_r, stable, exclude, max_slope: float, max_samples: int, centre):
        rr, cc, self.step = _sample(zr, max_samples)
        self.rr, self.cc = rr, cc
        self.x = tr_r.c + (cc + 0.5) * tr_r.a
        self.y = tr_r.f + (rr + 0.5) * tr_r.e
        self.z = zr[rr, cc].astype(np.float64)
        gx, gy = _gradient(zr, rr, cc, tr_r.a, -tr_r.e)
        self.sx, self.sy = -gx, -gy  # dh = sx dx + sy dy for a DEM that shows the ground (dx, dy) further
        tan = np.hypot(gx, gy)
        self.base = np.isfinite(tan) & (tan <= math.tan(math.radians(max_slope)))
        self.base &= _areas(stable, exclude, self.x, self.y, rr, cc, zr.shape)
        self.u, self.v = self.x - centre[0], self.y - centre[1]
        self.resolution = abs(tr_r.a)

    def plane(self, p) -> np.ndarray:
        return p[0] + p[1] * self.u + p[2] * self.v

    def stats(self, d: np.ndarray) -> tuple[float, float, float]:
        """NMAD, RMSE and median of differences."""
        if not d.size:
            return float("nan"), float("nan"), float("nan")
        return _nmad(d), float(np.sqrt(np.mean(d ** 2))), float(np.median(d))


def _centre(transform, shape) -> tuple[float, float]:
    return (transform.c + transform.a * shape[1] / 2, transform.f + transform.e * shape[0] / 2)


def _estimate(zr, tr_r, zd, tr_d, stable=None, exclude=None, tilt: bool = True, max_iter: int = 10,
              max_slope: float = MAX_SLOPE, k: float = OUTLIER_K, tolerance: float = TOLERANCE,
              max_samples: int = MAX_SAMPLES, progress: Progress | None = None) -> Coregistration:
    """The shift, bias and tilt of ``zd`` (grid ``tr_d``) on ``zr`` (grid ``tr_r``)."""
    progress = as_progress(progress)
    centre = _centre(tr_d, zd.shape)
    m = _Measure(zr, tr_r, stable, exclude, max_slope, max_samples, centre)
    if int(m.base.sum()) < MIN_CELLS:
        raise ValueError("too few cells to compare: the DEMs do not overlap, or the areas given leave out "
                         "almost all of them")
    few = "too few stable cells: the two DEMs differ almost everywhere"
    dx = dy = 0.0
    plane = np.zeros(3)
    horizontal, converged, it = False, False, 0
    # if the shift never settles (DEMs further apart than the slopes can tell, not the same ground),
    # the closest state measured - the DEM as it is unless another agreed clearly better
    start = best = None

    def clearly_better(nmad: float) -> bool:
        return nmad < min(best[0], (1 - CLEARLY) * start)

    for it in range(1, max_iter + 1):
        progress.check()
        dh = bilinear(zd, tr_d, m.x + dx, m.y + dy) - m.z - m.plane(plane)
        ok = m.base & np.isfinite(dh)
        inl, _med, nmad = _inliers(dh, ok, k)
        if start is None:
            start, best = nmad, (nmad, dx, dy, plane.copy())
        elif clearly_better(nmad):
            best = (nmad, dx, dy, plane.copy())
        if it == 1:  # the shift is still in the differences: no cell left out yet (see the module docstring)
            inl, nmad = ok, _nmad(dh[ok])
        if int(inl.sum()) < MIN_CELLS:
            raise ValueError(few)
        ddx, ddy, inc, horizontal = _fit(dh[inl], m.sx[inl], m.sy[inl], m.u[inl], m.v[inl], tilt and it > 1)
        plane = plane + inc
        dx, dy = dx + ddx, dy + ddy
        progress(0.9 * it / max_iter, f"Coregistration: iteration {it}, shift {dx:+.4f} {dy:+.4f} m, "
                                      f"NMAD {nmad:.4f} m")
        if math.hypot(ddx, ddy) < tolerance * m.resolution:
            converged = True
            break
    # the vertical bias (median) and the tilt (robust plane) once more, on the DEM shifted
    raw = bilinear(zd, tr_d, m.x + dx, m.y + dy) - m.z
    ok = m.base & np.isfinite(raw)
    inl, _, nmad = _inliers(raw - m.plane(plane), ok, k)
    if not converged and best is not None and not clearly_better(nmad):
        _, dx, dy, plane = best
        raw = bilinear(zd, tr_d, m.x + dx, m.y + dy) - m.z
        ok = m.base & np.isfinite(raw)
        inl, _, _ = _inliers(raw - m.plane(plane), ok, k)
    if int(inl.sum()) < MIN_CELLS:
        raise ValueError(few)
    _, _, plane, _ = _fit(raw[inl], None, None, m.u[inl], m.v[inl], tilt, shift=False)
    after = raw - m.plane(plane)
    inl, _, _ = _inliers(after, ok, k)
    before = bilinear(zd, tr_d, m.x, m.y) - m.z
    nb, rb, mb = m.stats(before[inl & np.isfinite(before)])
    na, ra, ma = m.stats(after[inl])
    progress(0.95, f"Coregistration: shift {dx:+.4f} {dy:+.4f} m, dz {plane[0]:+.4f} m, NMAD {nb:.4f} -> {na:.4f} m")
    return Coregistration(dx=float(dx), dy=float(dy), dz=float(plane[0]), tilt_x=float(plane[1]),
                          tilt_y=float(plane[2]), centre=(float(centre[0]), float(centre[1])), nmad_before=nb,
                          nmad_after=na, rmse_before=rb, rmse_after=ra, median_before=mb, median_after=ma,
                          cells=int(inl.sum()), compared=int(ok.sum()), step=int(m.step),
                          resolution=float(m.resolution), iterations=it, converged=converged,
                          horizontal=horizontal, tilt=bool(tilt), stable_index=(m.rr[inl], m.cc[inl]))


def _method(method: str) -> str:
    if method not in METHODS:
        raise ValueError(f"method: one of {', '.join(METHODS)}")
    if method == "xdem":
        try:
            import xdem  # noqa: F401
        except Exception as exc:
            raise RuntimeError("xdem is not installed: pip install pyarchinit-photogrammetry[coreg] (or use "
                               "method='nuth_kaab')") from exc
        return "xdem"
    return "nuth_kaab"


def xdem_available() -> bool:
    """Whether ``method="xdem"`` can be used."""
    try:
        _method("xdem")
        return True
    except RuntimeError:
        return False


def coregister(reference_path, dem_path, out_path=None, stable=None, exclude=None, tilt: bool = True,
               max_iter: int = 10, method: str = "auto", max_slope: float = MAX_SLOPE, k: float = OUTLIER_K,
               tolerance: float = TOLERANCE, max_samples: int = MAX_SAMPLES,
               progress: Progress | None = None) -> Coregistration:
    """The DEM ``dem_path`` moved onto ``reference_path`` over the stable ground (see the module
    docstring); with ``out_path`` the coregistered DEM is written there.

    ``stable``: the area where the ground may be stable (a shapely geometry in the coordinates of
    the DEMs, or a boolean grid of the shape of the reference); ``exclude``: areas that changed (the
    excavation), a geometry or a list. ``tilt``: a plane besides the vertical bias. ``max_slope``:
    degrees; ``k``: NMAD of the differences beyond which a cell changed; ``tolerance``: cells.
    ``method``: ``"auto"`` / ``"nuth_kaab"`` (this module) or ``"xdem"``. A ``ValueError`` when the
    DEMs do not overlap or differ almost everywhere."""
    progress = as_progress(progress)
    method = _method(method)
    if method == "xdem":
        return _coregister_xdem(reference_path, dem_path, out_path, stable, exclude, tilt, max_iter, max_slope, k,
                                max_samples, progress)
    progress(0.0, "Coregistration: reading the DEMs")
    zr, tr_r, crs_r = _read(reference_path)
    zd, tr_d, crs_d = _read(dem_path)
    _check(tr_r, tr_d, crs_r, crs_d)
    result = _estimate(zr, tr_r, zd, tr_d, stable=stable, exclude=exclude, tilt=tilt, max_iter=max_iter,
                       max_slope=max_slope, k=k, tolerance=tolerance, max_samples=max_samples,
                       progress=progress.sub(0.02, 0.78))
    del zr
    result.reference, result.dem = str(reference_path), str(dem_path)
    if out_path is not None:
        _write(Path(out_path), zd, tr_d, crs_d, result.dx, result.dy, (result.dz, result.tilt_x, result.tilt_y),
               result.centre, progress.sub(0.8, 0.2))
        result.path = str(out_path)
    progress(1.0, f"Coregistration: shift {result.shift:.4f} m, dz {result.dz:+.4f} m, NMAD "
                  f"{result.nmad_before:.4f} -> {result.nmad_after:.4f} m")
    return result


def _xdem_mask(zr, tr_r, zd, tr_d, stable, exclude, max_slope: float, k: float, max_samples: int) -> np.ndarray:
    """The stable ground on the whole grid of the reference, for xdem: slope, areas and the cells
    whose difference (before coregistration) is further than ``k`` NMAD from the median, the median
    and NMAD from the sample of :class:`_Measure`; block by block."""
    m = _Measure(zr, tr_r, stable, exclude, max_slope, max_samples, _centre(tr_d, zd.shape))
    dh = bilinear(zd, tr_d, m.x, m.y) - m.z
    ok = m.base & np.isfinite(dh)
    _, med, nmad = _inliers(dh, ok, k)
    limit = max(k * nmad, MIN_THRESHOLD) if math.isfinite(nmad) else np.inf
    h, w = zr.shape
    mask = np.zeros(zr.shape, bool)
    for r0 in range(1, h - 1, BLOCK_ROWS):
        r1 = min(h - 1, r0 + BLOCK_ROWS)
        rr, cc = np.mgrid[r0:r1, 1:w - 1]
        rr, cc = rr.ravel(), cc.ravel()
        gx, gy = _gradient(zr, rr, cc, tr_r.a, -tr_r.e)
        good = np.isfinite(gx) & np.isfinite(gy) & (np.hypot(gx, gy) <= math.tan(math.radians(max_slope)))
        x, y = tr_r.c + (cc + 0.5) * tr_r.a, tr_r.f + (rr + 0.5) * tr_r.e
        good &= _areas(stable, exclude, x, y, rr, cc, zr.shape)
        d = bilinear(zd, tr_d, x, y) - zr[rr, cc]
        good &= np.isfinite(d) & (np.abs(d - med) <= limit)
        mask[rr[good], cc[good]] = True
    return mask


def _coregister_xdem(reference_path, dem_path, out_path, stable, exclude, tilt, max_iter, max_slope, k, max_samples,
                     progress: Progress) -> Coregistration:
    """The same with xdem: ``NuthKaab`` (+ ``Deramp`` with ``tilt``) fitted on the stable ground of
    this module (:func:`_xdem_mask`). What xdem applied is then measured on its result, so that the
    shift, bias and tilt read as those of :func:`coregister` whatever the conventions of the version
    of xdem; the agreement before and after is measured as :func:`coregister` does."""
    import rasterio
    import xdem

    progress(0.0, "Coregistration (xdem): reading the DEMs")
    zr, tr_r, crs_r = _read(reference_path)
    zd, tr_d, crs_d = _read(dem_path)
    _check(tr_r, tr_d, crs_r, crs_d)
    mask = _xdem_mask(zr, tr_r, zd, tr_d, stable, exclude, max_slope, k, max_samples)
    if int(mask.sum()) < MIN_CELLS:
        raise ValueError("too few cells to compare: the DEMs do not overlap, or the areas given leave out "
                         "almost all of them")
    progress(0.2, "Coregistration (xdem): Nuth & Kääb")
    ref = xdem.DEM(str(reference_path))
    tba = xdem.DEM(str(dem_path))
    same = tba.shape == ref.shape and tba.transform == ref.transform
    on_ref = tba if same else tba.reproject(ref)  # xdem fits two DEMs on one grid
    nk = xdem.coreg.NuthKaab(max_iterations=max_iter)
    pipeline = nk + xdem.coreg.Deramp(poly_order=1) if tilt else nk
    pipeline.fit(ref, on_ref, inlier_mask=mask)
    aligned = pipeline.apply(on_ref)  # Deramp works in pixels of the grid it was fitted on
    if not same or aligned.shape != tba.shape or aligned.transform != tba.transform:
        aligned = aligned.reproject(tba)
    za = np.ma.masked_invalid(np.ma.asarray(aligned.data, dtype=np.float32)).filled(np.nan).reshape(zd.shape)
    progress(0.7, "Coregistration (xdem): measuring the result")
    # what xdem did: the input DEM measured on its result (nothing changed between the two)
    applied = _estimate(za, tr_d, zd, tr_d, tilt=tilt, max_iter=max_iter, max_slope=89.0, k=1e9,
                        max_samples=max_samples)
    # agreement with the reference on the stable ground of this module, before and after
    m = _Measure(zr, tr_r, stable, exclude, max_slope, max_samples, applied.centre)
    after = bilinear(za, tr_d, m.x, m.y) - m.z
    before = bilinear(zd, tr_d, m.x, m.y) - m.z
    inl, _, _ = _inliers(after, m.base & np.isfinite(after), k)
    nb, rb, mb = m.stats(before[inl & np.isfinite(before)])
    na, ra, ma = m.stats(after[inl])
    out = Coregistration(dx=applied.dx, dy=applied.dy, dz=applied.dz, tilt_x=applied.tilt_x, tilt_y=applied.tilt_y,
                         centre=applied.centre, nmad_before=nb, nmad_after=na, rmse_before=rb, rmse_after=ra,
                         median_before=mb, median_after=ma, cells=int(inl.sum()),
                         compared=int((m.base & np.isfinite(after)).sum()), step=int(m.step),
                         resolution=float(m.resolution), iterations=max_iter, converged=True,
                         horizontal=applied.horizontal, tilt=bool(tilt), method="xdem", reference=str(reference_path),
                         dem=str(dem_path), stable_index=(m.rr[inl], m.cc[inl]))
    if out_path is not None:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **_profile(zd.shape, tr_d, crs_d)) as dst:
            dst.write(za.astype(np.float32), 1)
            _overviews(dst, zd.shape)
        out.path = str(out_path)
    progress(1.0, f"Coregistration (xdem): shift {out.shift:.4f} m, dz {out.dz:+.4f} m")
    return out


# ---------------------------------------------------------------- chunks
def reference_dem(reference) -> tuple[Path, str, dict]:
    """``(path, label, info)`` of a reference: a chunk (its active DEM), ``(chunk, version id)`` (a
    kept version of its DEM; ``None``: the active one) or the path of a DEM."""
    from ..core import versions

    if isinstance(reference, (tuple, list)) and len(reference) == 2 and hasattr(reference[0], "data_dir"):
        chunk, vid = reference
        if vid is None:
            return reference_dem(chunk)
        entry = next((e for e in versions.versions(chunk, "dem") if e["id"] == vid), None)
        if entry is None:
            raise KeyError(f"{chunk.label}: no kept DEM {vid}")
        return (versions.version_file(chunk, "dem", vid), f"{chunk.label}, {entry['name']}",
                {"reference_chunk": chunk.key, "reference_version": vid})
    if hasattr(reference, "dem_path"):
        path = reference.dem_path
        if path is None or not path.exists():
            raise RuntimeError(f"the chunk {reference.label} has no DEM")
        return path, reference.label, {"reference_chunk": reference.key, "reference_version": None}
    path = Path(reference)
    if not path.exists():
        raise FileNotFoundError(path)
    return path, path.name, {"reference_file": str(path)}


def coregister_chunk(chunk, reference, stable=None, exclude=None, tilt: bool = True, max_iter: int = 10,
                     method: str = "auto", name: str | None = None, progress: Progress | None = None) -> dict:
    """The DEM of ``chunk`` coregistered onto ``reference`` (:func:`reference_dem`): the result
    becomes the active DEM (named ``name``, default "DEM coregistrato su <reference>"), the DEM as it
    was stays among the versions; the measures go into ``chunk.meta["dem"]["coregistration"]`` and
    are returned with ``name`` and ``version`` (the id of the DEM kept)."""
    from ..core import versions

    progress = as_progress(progress)
    src = chunk.dem_path
    if src is None or not src.exists():
        raise RuntimeError("build the DEM first")
    ref_path, label, info = reference_dem(reference)
    if Path(ref_path).resolve() == src.resolve():
        raise ValueError("the reference is the DEM itself: choose another chunk or a kept version")
    tmp = chunk.data_dir / "dem.coreg.tmp.tif"
    try:
        result = coregister(ref_path, src, tmp, stable=stable, exclude=exclude, tilt=tilt, max_iter=max_iter,
                            method=method, progress=progress.sub(0.0, 0.95))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    params = {k: v for k, v in (chunk.meta.get("dem") or {}).items() if k != "coregistration"}
    kept = versions.keep_active(chunk, "dem", move=True)
    try:
        own = chunk.own_product_path("dem")
        tmp.replace(own)
        chunk._set_product("dem", own)
    except BaseException:
        if kept is not None:
            versions.restore(chunk, "dem", kept)
        tmp.unlink(missing_ok=True)
        raise
    record = {k: v for k, v in result.to_dict().items() if k not in ("reference", "dem", "path")}
    record.update(info, reference=label, version=kept["id"] if kept else None,
                  when=datetime.now().isoformat(timespec="seconds"))
    chunk.meta["dem"] = {**params, "coregistration": record}
    name = (name or f"DEM coregistrato su {label}")[:80]
    versions.set_active_name(chunk, "dem", name)
    progress(1.0, f"DEM coregistered on {label}: shift {result.shift:.4f} m, dz {result.dz:+.4f} m")
    return {**record, "name": name}


def summary(result: Coregistration | dict) -> str:
    """One line: shift, bias, tilt and the agreement before/after (for logs and the command line)."""
    r = result.to_dict() if isinstance(result, Coregistration) else result

    def num(v):
        return float("nan") if v is None else round(float(v), 4) + 0.0  # never "-0.0000"

    tilt = math.hypot(num(r.get("tilt_x") or 0.0), num(r.get("tilt_y") or 0.0)) * 1000
    text = (f"dx {num(r['dx']):+.4f} m, dy {num(r['dy']):+.4f} m, dz {num(r['dz']):+.4f} m, tilt {tilt:.2f} mm/m; "
            f"NMAD {num(r.get('nmad_before')):.4f} -> {num(r.get('nmad_after')):.4f} m ({r['cells']} stable cells)")
    if not r.get("horizontal", True):
        text += "; horizontal shift not measurable along the ground (flat or one-sided)"
    if not r.get("converged", True):
        text += "; NOT CONVERGED: the closest state found (DEMs too far apart, or not the same ground?)"
    return text
