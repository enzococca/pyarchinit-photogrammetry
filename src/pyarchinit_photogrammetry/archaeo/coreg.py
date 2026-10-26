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
than :data:`MIN_SLOPE` degrees (or a tenth of their root mean square) is not measured, the fit does
not move the DEM along it (the shift stays 0, or where the initial search put it) and
``horizontal`` is False. When the shift does not settle within ``max_iter`` iterations (DEMs
further apart than their slopes can tell, or not the same ground), the state of the iterations with
which the DEMs agreed best is kept if it is clearly better (:data:`CLEARLY`) than the state the
iterations started from, else that one; ``converged`` is False either way.

**The initial search.** The fit is a local one: it finds a shift only when it is shorter than the
scale of the relief - a fraction of the width of the features of the ground. Two surveys
georeferenced with different GPS, metres apart, on a site of pits and walls are beyond it. So the
slopes of the two DEMs are first correlated at every shift at once (:func:`_search`): both are
averaged on a lattice of at most :data:`SEARCH_SIDE` cells a side (the reference in blocks of its
cells, the DEM read at the same centres), smoothed alike, and the normalised cross-correlation of
their slope vectors (east, north) is computed for every shift by FFT with the masks of both (only
the cells present in both at a shift count, with their own means and variances: Padfield 2012),
where the two overlap by at least :data:`SEARCH_OVERLAP` of the most they can. Why this method:
slopes do not see the vertical bias (metres between two GPS) and a tilt only adds a constant to
them, which the normalisation takes away; a hillshade would also depend on the direction of the
light. A correlation tries every shift, so how long the shift is does not matter, only that the
ground shows something - the slopes of a tell, the walls of a trench, the few ditches of a level
field - whereas a pyramid of fits of Nuth & Kääb on smoothed DEMs reaches further only where the
ground has relief at larger scales (a tell, not a level field with a few features). The masks keep
the irregular outline of the surveys out of the result, where a phase correlation would need a
rectangle inside both. The best few shifts (:data:`SEARCH_CANDIDATES`) are correlated again
without the cells that changed there - beyond ``k`` NMAD once bias and plane are taken away, as in
the fit: the steep walls of a pit dug in between, or vegetation that grew, pull the correlation -
and the best of them is ``found`` when it correlates at least :data:`SEARCH_MIN_SCORE`, no shift
elsewhere correlates almost as well (:data:`SEARCH_MARGIN`; else ``ambiguous``: ground that
repeats itself, ploughing, rows of vines, a grid of pits) and the shifts nearly as good lie around
it, not along a line (:data:`SEARCH_RIDGE`; ground that is the same along one direction, one long
ditch or furrows: ``ambiguous`` too). Flat ground (slopes spreading less than
:data:`SEARCH_MIN_RELIEF` degrees) and DEMs of different ground give ``none``. With ``search`` only
shifts up to that length are looked at; when a clearly better match lies further, the best one
within reach only looks like the ground and is not used (``beyond``).

The fit is always run from the DEM as it is, as above. When the search found a shift, that fit
ending within :data:`SEARCH_AGREE` cells of the search of it confirms it (``confirmed``: the small
shifts of the phases of an excavation, nothing changes for them). Otherwise the fit is run again
from the shift found and kept (``used``) if it leaves at most :data:`SEARCH_BETTER` as much of the
sloped ground in disagreement - differences beyond ``k`` times the smaller NMAD of the two fits, on
the cells sloping at least :data:`MIN_SLOPE` degrees, the ground that tells a horizontal shift -
and agrees on at least :data:`SEARCH_GAIN` of it more; else the fit from the DEM as it is stays
(``rejected``). The NMAD does not decide between the two: on level ground with a few features,
those a wrong shift leaves out of place are taken for ground that changed and the NMAD of the rest
is as small; and reading a DEM between its cells averages the noise of up to four of them, so the
NMAD of a fit also depends on where its shift falls between the cells (down to a half). A shift
found but not verified this way is never applied. DEMs that hardly overlap as they are (a GPS error
larger than the surveys) leave the fit from the DEM as it is nothing to start from: the fit from the
shift found is then used if it settles, else ``ValueError`` as without the search.

The result (:class:`Coregistration`) says where the DEM was - ``dx``, ``dy``: it showed the ground
that far east and north of the reference; ``dz``: that much higher at ``centre``; ``tilt_x``,
``tilt_y``: metres per metre - and how well the two DEMs agree on the stable ground before and after
(NMAD, RMSE, median); ``initial`` (off, none, ambiguous, beyond, confirmed, used, rejected),
``initial_shift`` (the shift the search found, or the better one beyond ``search``),
``initial_score`` (its correlation), ``search_cell`` and ``method_used`` ("search+nuth_kaab" when
the fit started from the search) say what the search did. The coregistered DEM is ``z(x + dx, y +
dy) - (dz + tilt_x (x - x0) + tilt_y (y - y0))`` written on the grid of the input DEM (float32, NaN
where empty).

Large DEMs are measured on a regular sample of at most :data:`MAX_SAMPLES` cells of the reference
(their slopes from the full-resolution neighbours); the whole DEM is then shifted.

:func:`coregister_chunk` coregisters the DEM of a chunk onto the DEM of another chunk (or a kept
version, or a file) and keeps the result as the active version of its DEM, the DEM as it was among
the versions; :mod:`.timeline` coregisters the phases of an excavation before measuring volumes.

``method="xdem"`` uses the library xdem (``pip install pyarchinit-photogrammetry[coreg]``:
``NuthKaab`` and, with ``tilt``, ``Deramp``, without the initial search) if it can be imported;
``"auto"`` and ``"nuth_kaab"`` are the implementation of this module.
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
# the initial search (see the module docstring)
SEARCH_SIDE = 512  # cells: the larger side of the lattice of the search (the DEMs averaged in blocks)
SEARCH_MIN_FACTOR = 2  # the cells of the search are at least this many cells of the reference a side
SEARCH_SMOOTH = 1.0  # cells of the lattice: sigma of the Gaussian both DEMs are smoothed with
SEARCH_OVERLAP = 0.3  # shifts at which the DEMs overlap less than this share of the most they can are not tried
SEARCH_MIN_CELLS = 64  # cells of the lattice in common, at least
SEARCH_MIN_RELIEF = 1.0  # degrees: slopes of the lattice spreading less (either DEM): flat ground, nothing to look for
SEARCH_MIN_SCORE = 0.2  # least correlation of the slopes at the shift found
SEARCH_CANDIDATES = 4  # the best shifts tried again without the cells that changed there
SEARCH_MARGIN = 0.1  # another shift correlating within this share of the best one: ambiguous
SEARCH_RIDGE = 10.0  # cells: the shifts nearly as good spread further along a line (3 times across): ambiguous
SEARCH_AGREE = 1.5  # cells of the search: the fit from the DEM as it is ending this close to it agrees with it
SEARCH_BETTER = 0.5  # the fit from the search leaves at most this share of the sloped ground in disagreement
# that the fit from the DEM as it is leaves...
SEARCH_GAIN = 0.05  # ...and agrees on at least this share of the sloped ground more


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
    horizontal: bool = True  # False: flat or one-sided ground, the shift that cannot be measured not fitted
    tilt: bool = True
    method: str = "nuth_kaab"
    # the initial search: off, none (no clear match), ambiguous (several shifts match: periodic ground),
    # beyond (the best match is further than ``search``), confirmed (the fit from the DEM as it is
    # reached it), used (the fit started from it), rejected (the DEMs did not agree better there)
    initial: str = "off"
    initial_shift: tuple[float, float] | None = None  # (m) the shift the search found
    initial_score: float = float("nan")  # correlation of the slopes at that shift (1: the same)
    search_cell: float = 0.0  # (m) the cells of the search
    method_used: str = "nuth_kaab"  # or "search+nuth_kaab": the fit started from the shift of the search
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
                  "median_before", "median_after", "resolution", "initial_score", "search_cell"):
            v = float(getattr(self, k))
            out[k] = v if math.isfinite(v) else None
        out.update(shift=self.shift, centre=[float(self.centre[0]), float(self.centre[1])], cells=int(self.cells),
                   compared=int(self.compared), step=int(self.step), iterations=int(self.iterations),
                   converged=bool(self.converged), horizontal=bool(self.horizontal), tilt=bool(self.tilt),
                   method=self.method, initial=self.initial,
                   initial_shift=None if self.initial_shift is None else [float(v) for v in self.initial_shift],
                   method_used=self.method_used, reference=self.reference, dem=self.dem, path=self.path)
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
        with np.errstate(invalid="ignore"):
            self.sloped = tan >= math.tan(math.radians(MIN_SLOPE))  # the ground that tells a horizontal shift
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


# ---------------------------------------------------------------- the initial search
@dataclass
class _Found:
    """What :func:`_search` found: the shift (m), the correlation of the slopes there and the best
    correlation of another shift; ``status``: found, ambiguous, beyond (the shift is the better one
    further than ``search``) or none."""

    dx: float = 0.0
    dy: float = 0.0
    score: float = float("nan")
    second: float = float("nan")
    status: str = "none"
    cell: float = 0.0  # (m) the cells of the search
    note: str = ""


def _reduce(z: np.ndarray, f: int) -> np.ndarray:
    """Means of the blocks of ``f`` x ``f`` cells of ``z`` (NaN where less than half of a block has
    a value), a band of rows at a time: float32."""
    if f <= 1:
        return z
    h, w = z.shape
    rows, cols = -(-h // f), -(-w // f)
    out = np.empty((rows, cols), np.float32)
    step = max(1, BLOCK_ROWS // f)
    for b0 in range(0, rows, step):
        b1 = min(rows, b0 + step)
        band = np.full(((b1 - b0) * f, cols * f), np.nan, np.float32)
        src = z[b0 * f:b1 * f]
        band[:src.shape[0], :w] = src
        valid = np.isfinite(band)
        total = np.where(valid, band, 0.0).reshape(b1 - b0, f, cols, f).sum(axis=(1, 3), dtype=np.float64)
        count = valid.reshape(b1 - b0, f, cols, f).sum(axis=(1, 3))
        with np.errstate(invalid="ignore", divide="ignore"):
            out[b0:b1] = np.where(count >= f * f / 2, total / count, np.nan)
    return out


def _smooth(z: np.ndarray, sigma: float) -> np.ndarray:
    """``z`` smoothed by a Gaussian of ``sigma`` cells, the empty cells left out (and left empty)."""
    from scipy import ndimage

    valid = np.isfinite(z)
    if sigma <= 0 or not valid.any():
        return z.astype(np.float64)
    base = float(np.median(z[valid]))
    num = ndimage.gaussian_filter(np.where(valid, z - base, 0.0).astype(np.float64), sigma, mode="constant")
    den = ndimage.gaussian_filter(valid.astype(np.float64), sigma, mode="constant")
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(valid & (den > 0.25), num / den + base, np.nan)


def _grid_slopes(z: np.ndarray, east: float, north: float):
    """East and north gradient of a whole grid (Horn), NaN at the edges and next to empty cells."""
    p = np.pad(z.astype(np.float64), 1, constant_values=np.nan)
    a, b, c = p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:]
    d, f = p[1:-1, :-2], p[1:-1, 2:]
    g, hh, i = p[2:, :-2], p[2:, 1:-1], p[2:, 2:]
    gx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * east)
    gy = ((a + 2 * b + c) - (g + 2 * hh + i)) / (8 * north)
    return gx, gy


class _Lattice:
    """The two DEMs on one lattice of cells ``f`` times those of the reference: the reference
    averaged in blocks, the DEM averaged in blocks of about the same size and read at the centres of
    the cells of the lattice that cover it, both smoothed alike (:data:`SEARCH_SMOOTH`: the block
    means and the reading filter the two differently near the shortest wavelength the lattice
    holds); their heights, slopes (east, north) and the cells whose slopes count (not steeper than
    ``max_slope``; for the reference inside ``stable`` and outside ``exclude``). ``offset``: the
    lattice row and column of the first cell of the DEM."""

    def __init__(self, zr, tr_r, zd, tr_d, stable, exclude, f: int, max_slope: float):
        from rasterio.transform import Affine

        east, north = abs(tr_r.a) * f, abs(tr_r.e) * f
        self.east, self.north = east, north
        steep = math.tan(math.radians(max_slope))
        self.zr = _smooth(_reduce(zr, f), SEARCH_SMOOTH)
        self.tr_r = Affine(east, 0.0, tr_r.c, 0.0, -north, tr_r.f)
        self.ref = _grid_slopes(self.zr, east, north)
        with np.errstate(invalid="ignore"):
            self.mr = np.hypot(*self.ref) <= steep
        if stable is not None or exclude is not None:
            rr, cc = np.mgrid[0:self.zr.shape[0], 0:self.zr.shape[1]]
            x, y = tr_r.c + (cc + 0.5) * east, tr_r.f - (rr + 0.5) * north
            rr = np.minimum(rr * f + f // 2, zr.shape[0] - 1)
            cc = np.minimum(cc * f + f // 2, zr.shape[1] - 1)
            self.mr &= _areas(stable, exclude, x, y, rr, cc, zr.shape)
        h, w = zd.shape
        xd0, xd1 = sorted((tr_d.c, tr_d.c + w * tr_d.a))
        yd0, yd1 = sorted((tr_d.f, tr_d.f + h * tr_d.e))
        c0, c1 = math.floor((xd0 - tr_r.c) / east), math.ceil((xd1 - tr_r.c) / east)
        r0, r1 = math.floor((tr_r.f - yd1) / north), math.ceil((tr_r.f - yd0) / north)
        g = max(1, int(round(east / abs(tr_d.a))))
        cols, rows = np.meshgrid(np.arange(c0, c1), np.arange(r0, r1))
        tr_g = Affine(tr_d.a * g, 0.0, tr_d.c, 0.0, tr_d.e * g, tr_d.f)  # the blocks of g x g cells
        self.zd = _smooth(bilinear(_reduce(zd, g), tr_g, tr_r.c + (cols + 0.5) * east, tr_r.f - (rows + 0.5) * north,
                                   strict=False), SEARCH_SMOOTH)
        self.tr_d = Affine(east, 0.0, tr_r.c + c0 * east, 0.0, -north, tr_r.f - r0 * north)
        self.dem = _grid_slopes(self.zd, east, north)
        with np.errstate(invalid="ignore"):
            self.md = np.hypot(*self.dem) <= steep
        self.offset = (r0, c0)

    def changed(self, dx: float, dy: float, mask: np.ndarray, k: float) -> np.ndarray:
        """The cells of ``mask`` (reference lattice) where the DEM read ``dx``, ``dy`` further differs
        from the reference beyond ``k`` NMAD once the bias and a plane (robust fit) are taken away:
        the ground that changed, as the fit of Nuth & Kääb finds it."""
        rows, cols = np.nonzero(mask)
        x = self.tr_r.c + (cols + 0.5) * self.east
        y = self.tr_r.f - (rows + 0.5) * self.north
        d = bilinear(self.zd, self.tr_d, x + dx, y + dy) - self.zr[rows, cols]
        ok = np.isfinite(d)
        out = np.zeros(mask.shape, bool)
        if int(ok.sum()) < SEARCH_MIN_CELLS:
            return out
        u, v = x - x[ok].mean(), y - y[ok].mean()
        _, _, plane, _ = _fit(d[ok], None, None, u[ok], v[ok], True, shift=False)
        inl, _, _ = _inliers(d - (plane[0] + plane[1] * u + plane[2] * v), ok, k)
        out[rows[ok & ~inl], cols[ok & ~inl]] = True
        return out


def _masked_ncc(ref, mr, dem, md):
    """Normalised cross-correlation of the slopes of the reference (``ref`` = (east, north) on the
    mask ``mr``) and of the DEM for every offset of the DEM grid, both masks taken into account
    (Padfield 2012: only the cells present in both at that offset count, with their own means and
    variances), by FFT. Returns ``(ncc, overlap)`` laid out by offset: index ``(a, b)`` is the
    offset ``(a - rows of the reference + 1, b - columns of the reference + 1)`` of the DEM grid."""
    from scipy import fft

    (hr, wr), (hd, wd) = mr.shape, md.shape
    shape = (fft.next_fast_len(hr + hd - 1, real=True), fft.next_fast_len(wr + wd - 1, real=True))

    def spectrum(a):
        return fft.rfft2(a, s=shape, workers=-1)

    def back(s):
        out = fft.irfft2(s, s=shape, workers=-1)
        out = np.roll(out, (hr - 1, wr - 1), axis=(0, 1))
        return out[:hr + hd - 1, :wr + wd - 1]

    def centred(parts, mask):
        out = []
        for p in parts:
            q = np.where(mask, p, 0.0)
            q[mask] -= q[mask].mean()  # the correlation does not change, the sums lose less to rounding
            out.append(q)
        return out

    rx, ry = centred(ref, mr)
    dx, dy = centred(dem, md)
    fmr, fmd = spectrum(mr.astype(np.float64)), spectrum(md.astype(np.float64))
    frx, fry, fdx, fdy = spectrum(rx), spectrum(ry), spectrum(dx), spectrum(dy)
    n = np.maximum(np.rint(back(np.conj(fmr) * fmd)), 0.0)
    srx, sry = back(np.conj(frx) * fmd), back(np.conj(fry) * fmd)
    sdx, sdy = back(np.conj(fmr) * fdx), back(np.conj(fmr) * fdy)
    srr = back(np.conj(spectrum(rx * rx + ry * ry)) * fmd)
    sdd = back(np.conj(fmr) * spectrum(dx * dx + dy * dy))
    srd = back(np.conj(frx) * fdx + np.conj(fry) * fdy)
    some = n >= 1
    inv = 1.0 / np.where(some, n, 1.0)
    cov = srd - (srx * sdx + sry * sdy) * inv
    vr = srr - (srx * srx + sry * sry) * inv
    vd = sdd - (sdx * sdx + sdy * sdy) * inv
    tiny = 1e-9 * max(float(vr[some].max(initial=0.0)), float(vd[some].max(initial=0.0)), 1e-300)
    ok = some & (vr > tiny) & (vd > tiny)
    ncc = np.full(n.shape, np.nan)
    ncc[ok] = cov[ok] / np.sqrt(vr[ok] * vd[ok])
    return ncc, n


def _subcell(score: np.ndarray, p) -> tuple[float, float]:
    """The maximum of the quadratic through the 3 x 3 values around the cell ``p`` (rows, columns),
    as an offset from it; (0, 0) when they do not make a maximum."""
    a, b = p
    win = score[a - 1:a + 2, b - 1:b + 2]
    if win.shape != (3, 3) or not np.isfinite(win).all():
        return 0.0, 0.0
    v, u = np.mgrid[-1:2, -1:2]
    x = np.column_stack([np.ones(9), u.ravel(), v.ravel(), u.ravel() ** 2, (u * v).ravel(), v.ravel() ** 2])
    c = np.linalg.lstsq(x, win.ravel(), rcond=None)[0]
    hess = np.array([[2 * c[3], c[4]], [c[4], 2 * c[5]]])
    if np.linalg.eigvalsh(hess).max() >= 0:
        return 0.0, 0.0
    du, dv = np.linalg.solve(hess, -c[1:3])
    if abs(du) > 1 or abs(dv) > 1:
        return 0.0, 0.0
    return float(dv), float(du)


@dataclass
class _Map:
    """The correlation of the slopes at every shift tried (:func:`_ncc_map`): ``score`` (-inf where
    not tried) and the shift in cells of the lattice (rows south ``sr``, columns east ``sc``);
    ``beyond``: the index of the best shift longer than ``search``, ``beyond_score`` its correlation."""

    score: np.ndarray
    valid: np.ndarray
    sr: np.ndarray
    sc: np.ndarray
    beyond: tuple | None = None
    beyond_score: float = -np.inf


def _ncc_map(lat: _Lattice, mr: np.ndarray, search: float | None) -> _Map | None:
    """The correlation of the slopes of the reference cells ``mr`` with the DEM at every shift at
    which they overlap enough (:data:`SEARCH_OVERLAP`) and, with ``search``, not longer than that;
    None when there are too few cells."""
    if int(mr.sum()) < SEARCH_MIN_CELLS or int(lat.md.sum()) < SEARCH_MIN_CELLS:
        return None
    ncc, n = _masked_ncc(lat.ref, mr, lat.dem, lat.md)
    hr, wr = mr.shape
    sr, sc = np.mgrid[0:ncc.shape[0], 0:ncc.shape[1]]
    sr, sc = sr - (hr - 1) + lat.offset[0], sc - (wr - 1) + lat.offset[1]
    valid = np.isfinite(ncc) & (n >= max(SEARCH_MIN_CELLS, SEARCH_OVERLAP * float(n.max())))
    beyond, beyond_score = None, -np.inf
    if search is not None:
        inside = np.hypot(sr * lat.north, sc * lat.east) <= search
        far = valid & ~inside
        if far.any():
            beyond = np.unravel_index(int(np.argmax(np.where(far, ncc, -np.inf))), ncc.shape)
            beyond_score = float(ncc[beyond])
        valid &= inside
    if not valid.any():
        return None
    return _Map(np.where(valid, ncc, -np.inf), valid, sr, sc, beyond, beyond_score)


def _shift_at(lat: _Lattice, mp: _Map, p) -> tuple[float, float]:
    """The shift (m, east and north) of the maximum of ``mp`` around the index ``p``."""
    ddr, ddc = _subcell(mp.score, p)
    return float((mp.sc[p] + ddc) * lat.east), float(-(mp.sr[p] + ddr) * lat.north)


def _peaks(mp: _Map, floor: float) -> list:
    """The indices of the local maxima of ``mp`` correlating at least ``floor``, best first (at most
    :data:`SEARCH_CANDIDATES`)."""
    from scipy import ndimage

    top = mp.valid & (mp.score >= floor) & (mp.score == ndimage.maximum_filter(mp.score, size=5, mode="nearest"))
    idx = np.flatnonzero(top)
    idx = idx[np.argsort(mp.score.ravel()[idx])[::-1][:SEARCH_CANDIDATES]]
    return [np.unravel_index(int(i), mp.score.shape) for i in idx]


def _analyse(lat: _Lattice, mp: _Map) -> _Found:
    """The best shift of ``mp`` and whether it is ``found``, ``ambiguous`` or ``none`` (see
    :func:`_search`)."""
    from scipy import ndimage

    found = _Found(cell=lat.east)
    score = mp.score
    p = np.unravel_index(int(np.argmax(score)), score.shape)
    best = found.score = float(score[p])
    if best < SEARCH_MIN_SCORE:
        found.note = "low correlation"
        return found
    eight = np.ones((3, 3), bool)
    lab, _ = ndimage.label(score >= best / 2, structure=eight)
    others = mp.valid & (lab != lab[p])  # outside the hill of the best shift
    found.second = float(score[others].max()) if others.any() else float("nan")
    lab, _ = ndimage.label(score >= (1 - SEARCH_MARGIN) * best, structure=eight)
    rows, cols = np.nonzero(lab == lab[p])  # the shifts nearly as good, around the best one
    spread = np.sqrt(np.clip(np.linalg.eigvalsh(np.cov(np.vstack([rows - p[0], cols - p[1]]))), 0, None)) \
        if rows.size > 2 else np.zeros(2)
    found.note = f"plateau {rows.size} cells, spread {spread[1]:.2f} x {spread[0]:.2f}"
    if found.second >= (1 - SEARCH_MARGIN) * best or (spread[1] > SEARCH_RIDGE and spread[1] > 3 * spread[0]):
        found.status = "ambiguous"
        return found
    around = mp.valid[max(p[0] - 1, 0):p[0] + 2, max(p[1] - 1, 0):p[1] + 2]
    if around.shape != (3, 3) or not around.all():
        found.note += "; at the edge of the shifts looked at"
        return found
    found.dx, found.dy = _shift_at(lat, mp, p)
    found.status = "found"
    return found


def _relief(slopes, mask) -> float:
    """The spread (root mean square about their mean) of the slope vectors of the cells of ``mask``."""
    if int(mask.sum()) < SEARCH_MIN_CELLS:
        return 0.0
    return float(math.sqrt(np.var(slopes[0][mask]) + np.var(slopes[1][mask])))


def _search(zr, tr_r, zd, tr_d, stable=None, exclude=None, search: float | None = None,
            max_slope: float = MAX_SLOPE, k: float = OUTLIER_K, progress: Progress | None = None) -> _Found:
    """The horizontal shift of ``zd`` on ``zr`` found by correlating their slopes (see the module
    docstring): on a lattice of at most :data:`SEARCH_SIDE` cells a side (:class:`_Lattice`), every
    shift at which the two DEMs overlap enough is tried at once (:func:`_masked_ncc`). The best few
    shifts (:data:`SEARCH_CANDIDATES`) are tried again without the cells that changed there
    (:meth:`_Lattice.changed`: the steep walls of a pit dug in between, vegetation that grew, pull
    the correlation), and the one correlating best then is kept: ``found`` when its correlation is at
    least :data:`SEARCH_MIN_SCORE`, no other shift correlates almost as well (``ambiguous``:
    :data:`SEARCH_MARGIN`) and the shifts close to it in correlation lie around it, not along a line
    (a ridge: ground that is the same along one direction, ``ambiguous`` too)."""
    progress = as_progress(progress)
    res = abs(tr_r.a)
    side = max(zr.shape + (zd.shape[0] * abs(tr_d.e) / abs(tr_r.e), zd.shape[1] * abs(tr_d.a) / res))
    f = max(SEARCH_MIN_FACTOR, int(math.ceil(side / SEARCH_SIDE)))
    progress(0.0, f"Coregistration: initial search on cells of {f * res:.3f} m")
    lat = _Lattice(zr, tr_r, zd, tr_d, stable, exclude, f, max_slope)
    progress.check()
    relief = min(_relief(lat.ref, lat.mr), _relief(lat.dem, lat.md))
    if relief < math.tan(math.radians(SEARCH_MIN_RELIEF)):
        return _Found(cell=lat.east, note=f"flat ground (slopes spread {math.degrees(math.atan(relief)):.2f} degrees)")
    first = _ncc_map(lat, lat.mr, search)
    if first is None:
        return _Found(cell=lat.east, note="too few cells in common")
    top = float(first.score.max())
    if first.beyond is not None and first.beyond_score >= SEARCH_MIN_SCORE and \
            top <= (1 - SEARCH_MARGIN) * first.beyond_score:
        # the best shift within reach only looks like the ground: a clearly better one is further
        b = first.beyond
        return _Found(dx=float(first.sc[b] * lat.east), dy=float(-first.sr[b] * lat.north),
                      score=first.beyond_score, second=top, status="beyond", cell=lat.east,
                      note=f"the best shift within {search} m correlates {top:.2f}")
    peaks = _peaks(first, max(SEARCH_MIN_SCORE / 2, top / 2))
    chosen, left_out = first, 0
    best = -np.inf
    for i, p in enumerate(peaks):
        progress(0.2 + 0.7 * i / len(peaks), "Coregistration: initial search, the best shifts again")
        progress.check()
        dx, dy = _shift_at(lat, first, p)
        changed = lat.changed(dx, dy, lat.mr, k)
        mp = _ncc_map(lat, lat.mr & ~changed, search) if changed.any() else first
        if mp is None:
            continue
        near = mp.score[max(p[0] - 2, 0):p[0] + 3, max(p[1] - 2, 0):p[1] + 3]
        if float(near.max()) > best:
            best, chosen, left_out = float(near.max()), mp, int(changed.sum())
    found = _analyse(lat, chosen)
    if left_out:
        found.note += f"; {left_out} cells that changed left out"
    where = f" {found.dx:+.3f} {found.dy:+.3f} m" if found.status == "found" else ""
    progress(1.0, f"Coregistration: initial search {found.status}{where} (slope correlation {found.score:.2f})")
    return found


@dataclass
class _Run:
    """One fit of Nuth & Kääb (:func:`_iterate`): the state it ended in and how the DEMs agree there."""

    dx: float
    dy: float
    plane: np.ndarray
    horizontal: bool
    converged: bool
    iterations: int
    nmad: float  # on the stable cells at the end
    inl: np.ndarray  # the stable cells at the end
    ok: np.ndarray  # the cells compared at the end
    after: np.ndarray  # the differences left


FEW = "too few stable cells: the two DEMs differ almost everywhere"


def _iterate(m: _Measure, zd, tr_d, start, tilt: bool, max_iter: int, k: float, tolerance: float,
             progress: Progress, label: str = "") -> _Run:
    """The fit of Nuth & Kääb from the shift ``start`` (see the module docstring)."""
    dx, dy = float(start[0]), float(start[1])
    plane = np.zeros(3)
    horizontal, converged, it = False, False, 0
    # if the shift never settles (DEMs further apart than the slopes can tell, not the same ground),
    # the closest state measured - the one it started from unless another agreed clearly better
    first = best = None

    def clearly_better(nmad: float) -> bool:
        return nmad < min(best[0], (1 - CLEARLY) * first)

    for it in range(1, max_iter + 1):
        progress.check()
        dh = bilinear(zd, tr_d, m.x + dx, m.y + dy) - m.z - m.plane(plane)
        ok = m.base & np.isfinite(dh)
        inl, _med, nmad = _inliers(dh, ok, k)
        if first is None:
            first, best = nmad, (nmad, dx, dy, plane.copy())
        elif clearly_better(nmad):
            best = (nmad, dx, dy, plane.copy())
        if it == 1:  # the shift is still in the differences: no cell left out yet (see the module docstring)
            inl, nmad = ok, _nmad(dh[ok])
        if int(inl.sum()) < MIN_CELLS:
            if it == 1:
                raise ValueError(FEW)
            break  # the iterations went off the DEM: back to the closest state below
        ddx, ddy, inc, horizontal = _fit(dh[inl], m.sx[inl], m.sy[inl], m.u[inl], m.v[inl], tilt and it > 1)
        plane = plane + inc
        dx, dy = dx + ddx, dy + ddy
        progress(it / max_iter, f"Coregistration{label}: iteration {it}, shift {dx:+.4f} {dy:+.4f} m, "
                                f"NMAD {nmad:.4f} m")
        if math.hypot(ddx, ddy) < tolerance * m.resolution:
            converged = True
            break
    # the vertical bias (median) and the tilt (robust plane) once more, on the DEM shifted
    raw = bilinear(zd, tr_d, m.x + dx, m.y + dy) - m.z
    ok = m.base & np.isfinite(raw)
    inl, _, nmad = _inliers(raw - m.plane(plane), ok, k)
    if not converged and best is not None and (int(inl.sum()) < MIN_CELLS or not clearly_better(nmad)):
        _, dx, dy, plane = best
        raw = bilinear(zd, tr_d, m.x + dx, m.y + dy) - m.z
        ok = m.base & np.isfinite(raw)
        inl, _, _ = _inliers(raw - m.plane(plane), ok, k)
    if int(inl.sum()) < MIN_CELLS:
        raise ValueError(FEW)
    _, _, plane, _ = _fit(raw[inl], None, None, m.u[inl], m.v[inl], tilt, shift=False)
    after = raw - m.plane(plane)
    inl, _, _ = _inliers(after, ok, k)
    return _Run(dx=dx, dy=dy, plane=plane, horizontal=horizontal, converged=converged, iterations=it,
                nmad=_nmad(after[inl]), inl=inl, ok=ok, after=after)


def _agreement(m: _Measure, run: _Run, sigma: float, k: float) -> float:
    """The share of the sloped cells compared at the end of ``run`` (:attr:`_Measure.sloped`: the
    ground that tells a horizontal shift) whose difference is within ``k`` ``sigma`` of the bias."""
    sloped = run.ok & m.sloped
    if not sloped.any() or not run.inl.any():
        return 0.0
    bias = float(np.median(run.after[run.inl]))
    return float(np.mean(np.abs(run.after[sloped] - bias) <= max(k * sigma, MIN_THRESHOLD)))


def _better(m: _Measure, other: _Run, run: _Run, k: float) -> bool:
    """Whether the fit from the initial search (``other``) is kept instead of the fit from the DEM as
    it is (``run``): of the sloped ground (:func:`_agreement`, ``k`` times the smaller NMAD of the
    two) it leaves at most :data:`SEARCH_BETTER` as much in disagreement, and at least
    :data:`SEARCH_GAIN` of it less. Not the NMAD: on level ground with a few features, those a wrong
    shift leaves out of place are taken for ground that changed and the NMAD of the rest is as small;
    and reading the DEM between its cells averages the noise of up to four of them, so the NMAD of a
    fit also depends on where its shift falls between the cells (down to a half)."""
    sigma = min(run.nmad, other.nmad)
    a, b = _agreement(m, run, sigma, k), _agreement(m, other, sigma, k)
    return 1 - b <= SEARCH_BETTER * (1 - a) and b - a >= SEARCH_GAIN


def _estimate(zr, tr_r, zd, tr_d, stable=None, exclude=None, tilt: bool = True, max_iter: int = 10,
              max_slope: float = MAX_SLOPE, k: float = OUTLIER_K, tolerance: float = TOLERANCE,
              max_samples: int = MAX_SAMPLES, search: float | None = None,
              progress: Progress | None = None) -> Coregistration:
    """The shift, bias and tilt of ``zd`` (grid ``tr_d``) on ``zr`` (grid ``tr_r``): the initial
    search (unless ``search`` is 0), the fit from the DEM as it is and, when the search found a
    shift that fit did not reach, the fit from there, kept if the DEMs agree clearly better."""
    progress = as_progress(progress)
    centre = _centre(tr_d, zd.shape)
    m = _Measure(zr, tr_r, stable, exclude, max_slope, max_samples, centre)
    if int(m.base.sum()) < MIN_CELLS:
        raise ValueError("too few cells to compare: the DEMs do not overlap, or the areas given leave out "
                         "almost all of them")
    found = None
    if search is None or search > 0:
        found = _search(zr, tr_r, zd, tr_d, stable, exclude, search, max_slope, k, progress.sub(0.0, 0.15))
    searched = found is not None and found.status == "found"
    try:
        run = _iterate(m, zd, tr_d, (0.0, 0.0), tilt, max_iter, k, tolerance, progress.sub(0.15, 0.4))
    except ValueError:
        if not searched:
            raise
        run = None  # the DEMs hardly overlap as they are (a GPS error larger than the surveys)
    initial = "off" if found is None else found.status
    used = False
    if searched:
        near = SEARCH_AGREE * found.cell
        if run is not None and math.hypot(run.dx - found.dx, run.dy - found.dy) <= near:
            initial = "confirmed"
        else:
            try:
                other = _iterate(m, zd, tr_d, (found.dx, found.dy), tilt, max_iter, k, tolerance,
                                 progress.sub(0.55, 0.4), " from the initial search")
            except ValueError:  # too few cells in common there
                other = None
            if run is None:  # nothing to compare with: the fit from the shift found, if it settles
                if other is None or not other.converged:
                    raise ValueError(FEW)
                run, initial, used = other, "used", True
            elif other is not None and math.hypot(run.dx - other.dx, run.dy - other.dy) <= near:
                initial = "confirmed"  # both fits ended at the same shift
            elif other is not None and _better(m, other, run, k):
                run, initial, used = other, "used", True
            else:
                initial = "rejected"
    before = bilinear(zd, tr_d, m.x, m.y) - m.z  # the DEM as it is
    dx, dy, plane, inl = run.dx, run.dy, run.plane, run.inl
    nb, rb, mb = m.stats(before[inl & np.isfinite(before)])
    na, ra, ma = m.stats(run.after[inl])
    progress(0.95, f"Coregistration: shift {dx:+.4f} {dy:+.4f} m, dz {plane[0]:+.4f} m, NMAD {nb:.4f} -> {na:.4f} m")
    tried = found is not None and found.status in ("found", "beyond")
    return Coregistration(dx=float(dx), dy=float(dy), dz=float(plane[0]), tilt_x=float(plane[1]),
                          tilt_y=float(plane[2]), centre=(float(centre[0]), float(centre[1])), nmad_before=nb,
                          nmad_after=na, rmse_before=rb, rmse_after=ra, median_before=mb, median_after=ma,
                          cells=int(inl.sum()), compared=int(run.ok.sum()), step=int(m.step),
                          resolution=float(m.resolution), iterations=run.iterations, converged=run.converged,
                          horizontal=run.horizontal, tilt=bool(tilt), initial=initial,
                          initial_shift=(found.dx, found.dy) if tried else None,
                          initial_score=found.score if found is not None else float("nan"),
                          search_cell=found.cell if found is not None else 0.0,
                          method_used="search+nuth_kaab" if used else "nuth_kaab",
                          stable_index=(m.rr[inl], m.cc[inl]))


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
               tolerance: float = TOLERANCE, max_samples: int = MAX_SAMPLES, search: float | None = None,
               progress: Progress | None = None) -> Coregistration:
    """The DEM ``dem_path`` moved onto ``reference_path`` over the stable ground (see the module
    docstring); with ``out_path`` the coregistered DEM is written there.

    ``stable``: the area where the ground may be stable (a shapely geometry in the coordinates of
    the DEMs, or a boolean grid of the shape of the reference); ``exclude``: areas that changed (the
    excavation), a geometry or a list. ``tilt``: a plane besides the vertical bias. ``max_slope``:
    degrees; ``k``: NMAD of the differences beyond which a cell changed; ``tolerance``: cells.
    ``search``: the longest shift (m) the initial search looks for - None: any at which the DEMs
    overlap enough, 0: no initial search. ``method``: ``"auto"`` / ``"nuth_kaab"`` (this module) or
    ``"xdem"``. A ``ValueError`` when the DEMs do not overlap or differ almost everywhere."""
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
                       max_slope=max_slope, k=k, tolerance=tolerance, max_samples=max_samples, search=search,
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
                        max_samples=max_samples, search=0)
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
                         horizontal=applied.horizontal, tilt=bool(tilt), method="xdem", method_used="xdem",
                         reference=str(reference_path), dem=str(dem_path), stable_index=(m.rr[inl], m.cc[inl]))
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
                     method: str = "auto", name: str | None = None, search: float | None = None,
                     progress: Progress | None = None) -> dict:
    """The DEM of ``chunk`` coregistered onto ``reference`` (:func:`reference_dem`): the result
    becomes the active DEM (named ``name``, default "DEM coregistrato su <reference>"), the DEM as it
    was stays among the versions; the measures go into ``chunk.meta["dem"]["coregistration"]`` and
    are returned with ``name`` and ``version`` (the id of the DEM kept). ``search``: see
    :func:`coregister`."""
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
                            method=method, search=search, progress=progress.sub(0.0, 0.95))
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
    name = (name or versions.label("DEM coregistrato su {0}", label))[:80]
    versions.set_active_name(chunk, "dem", name)
    progress(1.0, f"DEM coregistered on {label}: shift {result.shift:.4f} m, dz {result.dz:+.4f} m")
    return {**record, "name": name}


def summary(result: Coregistration | dict) -> str:
    """One line: shift, bias, tilt and the agreement before/after (for logs and the command line)."""
    r = result.to_dict() if isinstance(result, Coregistration) else result

    def num(v):
        return float("nan") if v is None else round(float(v), 4) + 0.0  # never "-0.0000"

    tilt = math.hypot(num(r.get("tilt_x") or 0.0), num(r.get("tilt_y") or 0.0)) * 1000
    before = "-" if r.get("nmad_before") is None else f"{num(r['nmad_before']):.4f}"  # DEMs apart as they were
    text = (f"dx {num(r['dx']):+.4f} m, dy {num(r['dy']):+.4f} m, dz {num(r['dz']):+.4f} m, tilt {tilt:.2f} mm/m; "
            f"NMAD {before} -> {num(r.get('nmad_after')):.4f} m ({r['cells']} stable cells)")
    initial, start = r.get("initial"), r.get("initial_shift")
    if initial in ("used", "rejected") and start:
        found = (f"initial search {num(start[0]):+.2f} {num(start[1]):+.2f} m (slope correlation "
                 f"{num(r.get('initial_score')):.2f})")
        text += f"; {found}, the fit started there" if initial == "used" else \
            f"; {found} rejected: the DEMs did not agree better there"
    elif initial == "ambiguous":
        text += "; initial search ambiguous (several shifts match: periodic ground?), not used"
    elif initial == "beyond" and start:
        text += (f"; initial search: a better match at {num(start[0]):+.2f} {num(start[1]):+.2f} m lies beyond the "
                 f"search distance, not used")
    if not r.get("horizontal", True):
        text += "; horizontal shift not measurable along the ground (flat or one-sided)"
    if not r.get("converged", True):
        text += "; NOT CONVERGED: the closest state found (DEMs too far apart, or not the same ground?)"
    return text
