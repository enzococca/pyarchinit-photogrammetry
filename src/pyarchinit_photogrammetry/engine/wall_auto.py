"""A wall traced on the plan by the program: followed between two clicks, or proposed on the whole plan.

A wall stands out of the DEM: its crest is higher than the ground on both sides of it (:func:`crest_map`: the drop
on each side, looked for only up to the first thing higher - rubble against a face, the edge of a trench beside it, a
parallel wall are not taken for the wall), and the orthophoto often shows its edges. :func:`strength_map` turns both into a strength
between 0 and 1 for each cell; :func:`least_cost` finds the path of least cost between two cells, each move costing
its length times ``1 + COST_GAIN (1 - strength)``, so that a path runs along the crest of a wall rather than across
the ground; :func:`follow` does it between the points clicked on the plan (the two ends and any via points), on the
DEM of a window around them, and gives back the line simplified. :func:`proposals` vectorises the ridges of the whole
plan into polylines, the candidates of walls. Where the DEM is weak (a wall a few centimetres high) a mask of the
wall on the orthophoto, segmented by SAM 2.1 from the clicks (:func:`sam_mask`), raises the strength of its cells.

The line found runs on the crest; :func:`.wall_trace.snap_trace` moves it onto the face of the model.
"""
from __future__ import annotations

import numpy as np

from ..core.progress import Progress, as_progress

RIDGE_RADIUS = 2.0  # m: the ground is looked for within this on each side of a crest (half the widest top, and more)
EDGE_WEIGHT = 0.5  # the strongest edge of the orthophoto counts as this much of a crest
COST_GAIN = 20.0  # a move on ground of strength 0 costs 1 + this times its length; on a crest, its length
MASK_STRENGTH = 0.8  # the strength of a cell inside the mask of the wall (SAM), at least
MIN_CELL = 0.05  # m: the DEM is looked at with cells this large at least
WINDOW_MARGIN = 10.0  # m around the box of the clicked points: the DEM read to follow the wall
MAX_CELLS = 4_000_000  # cells at most in what is read: coarser cells over this
SIMPLIFY = 0.10  # m: the line found simplified within this (a trace of a few vertices, not one per cell)
SPECK = 0.05  # m: half the width of the specks and threads of crest opened away before proposals are thinned
HOLE = 0.25  # m2: a hole in the crests of a wall smaller than this is filled before they are thinned
SPUR = 1.0  # m: a branch of a skeleton shorter than this, from a junction to a free end, is a spur, not a wall
RIDGE = 0.5  # the strength over which a cell is on a ridge (``on_ridge`` of :func:`follow`)
RIDGE_HEIGHT = 0.2  # m of local relief that make a crest at full strength (an absolute scale: a spoil heap or a
#                     tower nearby does not flatten the low walls)
SMOOTH = 0.10  # m: the DEM opened and closed by a disc of this radius before its crests are looked for (stones and
#               joints of a top evened out, its faces kept sharp); round(SMOOTH / cell) cells: none on cells of 0.2 m
#               or more, 5 cells across at 2 cm
EDGE_STEP = 0.15  # m: the edge of a crest on a side is where the fall first reaches this, or half its height if lower
STEP_RISE = 0.5  # m: a side that meets something this much higher before any edge stands against a structure
MIN_TOP = 0.25  # m: a crest against a taller structure is that wide at least (not the foot of a face)
STEP_WEIGHT = 0.5  # the strength of a crest against a taller structure (a tower) is at most this
BLOCK = 0.05  # m: a cell higher than the one looked from by this hides the ground beyond it (:func:`crest_map`)
GRADE_REACH = 1.5  # m: a cell of the mask (SAM) is graded by its distance from the mask's edge over the largest such
#                    distance within this (the middle of a wall 3 m thick at most is the cheapest)


class FollowRefused(ValueError):
    """Points the wall cannot be followed between: ``reason`` "points" (fewer than two, or not numbers),
    "outside" (``point``, from 1, is outside the DEM) or "no_path"."""

    def __init__(self, reason: str, message: str, point: int | None = None):
        super().__init__(message)
        self.reason, self.point = reason, point


# ---------------------------------------------------------------- the strength of the cells
def _over_p98(values: np.ndarray) -> np.ndarray:
    """``values`` (>= 0, NaN as 0) over the 98th percentile of the positive ones, clipped to 1; zeros when none is
    positive."""
    v = np.nan_to_num(np.asarray(values, np.float64), nan=0.0)
    v = np.clip(v, 0.0, None)
    positive = v[v > 0]
    if positive.size == 0:
        return np.zeros_like(v)
    top = float(np.percentile(positive, 98))
    return np.clip(v / top, 0.0, 1.0) if top > 0 else np.zeros_like(v)


def _shifted(z: np.ndarray, dr: int, dc: int) -> np.ndarray:
    """``z`` moved so that each cell holds the value of the cell ``(dr, dc)`` away from it; NaN off the grid."""
    h, w = z.shape
    out = np.full_like(z, np.nan)
    if abs(dr) >= h or abs(dc) >= w:
        return out
    r0, r1, c0, c1 = max(0, -dr), h - max(0, dr), max(0, -dc), w - max(0, dc)
    out[r0:r1, c0:c1] = z[r0 + dr:r1 + dr, c0 + dc:c1 + dc]
    return out


def _smoothed(z: np.ndarray, radius: int) -> np.ndarray:
    """``z`` opened then closed by a disc of ``radius`` cells (0: as it is): stones standing out of a top and the
    joints between them, narrower than the disc, evened out; the faces of a wall and the gaps between walls wider than
    the disc kept. Its NaN cells are kept NaN (filled for the filter by their neighbours' mean)."""
    import cv2
    from scipy.ndimage import gaussian_filter

    if radius < 1:
        return z
    finite = np.isfinite(z)
    if not finite.any():
        return z
    filled = z
    if not finite.all():
        num = gaussian_filter(np.where(finite, z, 0.0), 3.0, mode="nearest")
        den = gaussian_filter(finite.astype(np.float64), 3.0, mode="nearest")
        with np.errstate(invalid="ignore", divide="ignore"):
            fill = np.where(den > 1e-6, num / den, float(np.nanmean(z)))
        filled = np.where(finite, z, fill)
    base = float(np.nanmean(z))  # float32 keeps millimetres on heights of a few metres about their mean
    disc = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    f = (filled - base).astype(np.float32)
    f = cv2.morphologyEx(cv2.morphologyEx(f, cv2.MORPH_OPEN, disc, borderType=cv2.BORDER_REPLICATE),
                         cv2.MORPH_CLOSE, disc, borderType=cv2.BORDER_REPLICATE)
    return np.where(finite, f.astype(np.float64) + base, np.nan)


def crest_map(dem: np.ndarray, resolution: float, reach: float = RIDGE_RADIUS, progress: Progress | None = None):
    """``(height, centred, walled)``: how much each cell of ``dem`` stands above the ground on both sides of it,
    how near the middle between its two edges it is, and whether one of those sides is a taller structure it stands
    against rather than ground.

    Across each of four directions (the grid's axes and diagonals) the ground is looked for on each side within
    ``reach`` m, but only up to the first cell higher than this one by :data:`BLOCK`: what lies beyond a higher thing
    - a wall seen from the rubble against it, a wall seen from the edge of a trench beside it - is not its ground.
    The drop on a side is the largest fall met so, ``height`` the smaller of the two sides' drops; the edge on a side
    is the first cell where the fall reaches :data:`EDGE_STEP` - or half the larger drop when that is less - and still
    does at the next cell (the face
    of the wall, not the foot of the rubble or the bottom of the trench beyond it). ``centred`` is the nearer edge's
    distance (plus a cell) over the farther one's, at most 1: 1 in the middle of the top of a wall, towards 0 at its
    edges. A side that meets something :data:`STEP_RISE` higher before any edge (the top of a wall against a tower)
    is ``walled`` (when the two edges are :data:`MIN_TOP` apart at least): its edge is where it is blocked and
    ``height`` is the other side's drop. Of the four directions
    the one whose strength - ``height`` over :data:`RIDGE_HEIGHT`, clipped to 1, times ``centred``, times
    :data:`STEP_WEIGHT` when walled - is the largest is kept. The DEM is opened and closed by a disc of radius
    :data:`SMOOTH` first. NaN
    cells (no data) count as neither ground nor higher; a NaN cell itself gives 0."""
    z = np.asarray(dem, np.float64)
    if z.ndim != 2:
        raise ValueError("the DEM must be a 2D array")
    finite = np.isfinite(z)
    z = _smoothed(z, int(round(SMOOTH / float(resolution))))
    best_h, best_c = np.zeros(z.shape), np.zeros(z.shape)
    best_w = np.zeros(z.shape, bool)
    best = np.full(z.shape, -1.0)
    progress = as_progress(progress)
    for i, (dr, dc) in enumerate(((0, 1), (1, 0), (1, 1), (1, -1))):
        progress(i / 4.0)  # each direction a quarter; a cancel asked is honoured here
        n = max(1, int(np.ceil(float(reach) / (float(resolution) * float(np.hypot(dr, dc))))))

        def scan(sgn, thr=None):
            """Side ``sgn``: ``(drop, blocked at, highest rise)`` (``thr`` None), or the first step where the fall
            reaches ``thr`` (n + 1 where none)."""
            blocked = np.zeros(z.shape, bool)
            at = np.full(z.shape, float(n + 1))
            rise = np.zeros(z.shape)
            out = np.zeros(z.shape) if thr is None else np.full(z.shape, float(n + 1))
            prev = np.zeros(z.shape, bool)
            with np.errstate(invalid="ignore"):
                for k in range(1, n + 1):
                    zk = _shifted(z, sgn * k * dr, sgn * k * dc)
                    ok = ~blocked & np.isfinite(zk)
                    if thr is None:
                        out = np.where(ok, np.maximum(out, z - zk), out)
                        rise = np.fmax(rise, zk - z)
                    else:  # a fall kept over two cells: a face, not the gap between two stones
                        hit = ok & (z - zk >= thr)
                        out = np.where((out > n) & prev & hit, float(k - 1), out)
                        prev = hit
                    newly = ~blocked & (zk > z + BLOCK)
                    at = np.where(newly, float(k), at)
                    blocked |= newly
            if thr is not None:
                out = np.where((out > n) & prev, float(n), out)
            return out if thr is not None else (out, at, rise)

        (d0, at0, rise0), (d1, at1, rise1) = scan(1), scan(-1)
        thr = np.minimum(0.5 * np.maximum(d0, d1), EDGE_STEP)
        thr = np.where(thr > 0, thr, np.inf)
        e0, e1 = scan(1, thr), scan(-1, thr)
        top = MIN_TOP / (float(resolution) * float(np.hypot(dr, dc)))  # in steps
        w0 = (e0 > n) & (e1 <= n) & (at0 <= n) & (rise0 >= STEP_RISE) & (at0 + e1 >= top)  # side 0 against
        w1 = (e1 > n) & (e0 <= n) & (at1 <= n) & (rise1 >= STEP_RISE) & (at1 + e0 >= top)  # something taller
        candidates = [(np.minimum(d0, d1), e0, e1, np.zeros(z.shape, bool), 1.0),
                      (np.where(w0, d1, 0.0), np.where(w0, at0, e0), e1, w0, STEP_WEIGHT),
                      (np.where(w1, d0, 0.0), e0, np.where(w1, at1, e1), w1, STEP_WEIGHT)]
        for height, a0, a1, walled, weight in candidates:
            # within a cell of the middle counts as the middle (a top an even number of cells wide has none)
            centred = np.minimum(1.0, (np.minimum(a0, a1) + 1.0) / np.maximum(a0, a1))
            score = np.clip(height / RIDGE_HEIGHT, 0.0, 1.0) * centred * weight
            take = (score > best) & ((weight == 1.0) | walled)
            best = np.where(take, score, best)
            best_h = np.where(take, height, best_h)
            best_c = np.where(take, centred, best_c)
            best_w = np.where(take, walled, best_w)
    best_h = np.where(finite, np.clip(best_h, 0.0, None), 0.0)
    return best_h, np.where(finite, best_c, 0.0), best_w & finite


def _graded(m: np.ndarray, resolution: float, apart: bool = False) -> np.ndarray:
    """The cells of ``m`` (bool) graded towards its middle: their distance from its edge over the largest such
    distance within :data:`GRADE_REACH` (the middle of the top of a wall 3 m thick at most is 1); ``apart``: that
    largest distance looked for inside ``m`` only, step by step from the cell (the top of a parallel wall nearby,
    another piece of ``m``, does not lower the grades of this one)."""
    import cv2
    from scipy.ndimage import distance_transform_edt, maximum_filter

    if not m.any():
        return np.zeros(m.shape)
    d = distance_transform_edt(m)
    steps = max(1, int(round(GRADE_REACH / float(resolution))))
    if apart:
        top = d.astype(np.float32)
        kernel = np.ones((3, 3), np.uint8)
        for _ in range(steps):
            top = np.where(m, cv2.dilate(top, kernel), 0.0).astype(np.float32)
        top = top.astype(np.float64)
    else:
        top = maximum_filter(d, size=2 * steps + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(m & (top > 0), d / top, 0.0)


def _filled(m: np.ndarray, cell: float) -> np.ndarray:
    """``m`` (bool) with its holes smaller than :data:`HOLE` m2 filled."""
    from scipy import ndimage

    holes = ndimage.binary_fill_holes(m) & ~m
    if not holes.any():
        return m
    lab, n = ndimage.label(holes)
    sizes = ndimage.sum(holes, lab, np.arange(1, n + 1))
    small = np.zeros(n + 1, bool)
    small[1:] = sizes * cell * cell < HOLE
    return m | small[lab]


def _ridge_strength(dem, resolution, ortho=None, mask=None, radius=RIDGE_RADIUS, progress=None):
    """``(strength, ridge)``: the strength of :func:`strength_map` and the cells on a crest (bool, the ``on_ridge``
    of :func:`follow`: ``height`` over :data:`RIDGE` x :data:`RIDGE_HEIGHT` - on both sides, or on the open side of a
    top against a taller structure - or inside ``mask``)."""
    z = np.asarray(dem, np.float64)
    if z.ndim != 2:
        raise ValueError("the DEM must be a 2D array")
    height, centred, walled = crest_map(z, float(resolution), float(radius), progress)
    tall = np.clip(height / RIDGE_HEIGHT, 0.0, 1.0)
    ridge = (tall > RIDGE) & ~walled
    s = tall * centred * np.where(walled, STEP_WEIGHT, 1.0)
    top = _filled(ridge, float(resolution)) & ~walled  # the tops of the walls: graded towards their middles
    graded = np.maximum(_graded(top, resolution, apart=True), np.where(ridge, centred, 0.0))
    s = np.where(top, np.where(ridge, tall, 1.0) * graded, s)
    if ortho is not None:
        import cv2

        g = np.asarray(ortho, np.float64)
        if g.shape != z.shape:
            raise ValueError("the orthophoto must be on the grid of the DEM")
        valid = np.isfinite(g)
        if valid.any():
            g = np.where(valid, g, float(np.nanmean(g))).astype(np.float32)
            mag = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
            edge = EDGE_WEIGHT * _over_p98(np.where(valid, mag, 0.0))
            s = np.where(ridge, s, np.maximum(s, edge))  # an edge of the photo off the crests: on one, its middle
    if mask is not None:
        m = np.asarray(mask, bool)
        if m.shape != z.shape:
            raise ValueError("the mask must be on the grid of the DEM")
        s = np.where(m, np.maximum(s, MASK_STRENGTH * _graded(m, resolution)), s)
        ridge = ridge | m
    return s, ridge | (walled & (tall > RIDGE))  # a top against a tower is on the wall too: the line is not weak


def strength_map(dem: np.ndarray, resolution: float, ortho: np.ndarray | None = None, mask: np.ndarray | None = None,
                 radius: float = RIDGE_RADIUS) -> np.ndarray:
    """How much each cell of ``dem`` (h, w; NaN = no data) looks like the middle of the crest of a wall, from 0 to
    1: the ``height`` of :func:`crest_map` (the ground looked for within ``radius`` m on each side) over
    :data:`RIDGE_HEIGHT` (an absolute scale), clipped to 1, times how near the middle of the top it is - the larger
    of its ``centred`` and its distance from the edge of the top (the cells over :data:`RIDGE`, holes under
    :data:`HOLE` filled) over the largest such distance on the same top within :data:`GRADE_REACH` - so that a path
    keeps to the middle of the top of a wall; times :data:`STEP_WEIGHT` on a crest against a taller structure; with
    ``ortho``
    (grey, the same grid), off the crests, the larger of it and :data:`EDGE_WEIGHT` times the Sobel magnitude over
    its 98th percentile; inside ``mask`` (bool, the same grid) :data:`MASK_STRENGTH` at least, graded towards the
    mask's middle (its distance from the mask's edge over the largest such distance within :data:`GRADE_REACH`). A
    cell without data counts 0 (its mask aside)."""
    return _ridge_strength(dem, resolution, ortho, mask, radius)[0]


# ---------------------------------------------------------------- the path of least cost
def _grid_graph(strength: np.ndarray, resolution: float):
    """The 8-connected grid of ``strength`` as one sparse matrix: each move its length times the mean of
    ``1 + COST_GAIN (1 - strength)`` at its two cells."""
    from scipy.sparse import coo_matrix

    h, w = strength.shape
    c = (1.0 + COST_GAIN * (1.0 - np.clip(np.nan_to_num(strength, nan=0.0), 0.0, 1.0))).ravel()
    index = np.arange(h * w, dtype=np.int32 if h * w < 2 ** 31 else np.int64).reshape(h, w)
    rows, cols, data = [], [], []
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        r0, c0 = max(0, -dr), max(0, -dc)
        a = index[r0:h - max(0, dr), c0:w - max(0, dc)].ravel()
        b = index[r0 + dr:h - max(0, dr) + dr, c0 + dc:w - max(0, dc) + dc].ravel()
        rows.append(a)
        cols.append(b)
        data.append(resolution * float(np.hypot(dr, dc)) * (c[a] + c[b]) / 2.0)
    return coo_matrix((np.concatenate(data), (np.concatenate(rows), np.concatenate(cols))),
                      shape=(h * w, h * w)).tocsr()


def _path_on(graph, shape, start, goal) -> tuple[np.ndarray, np.ndarray]:
    """The path of least cost on ``graph`` (:func:`_grid_graph` of a grid of ``shape``) from the cell ``start`` to
    ``goal``."""
    from scipy.sparse.csgraph import dijkstra

    h, w = shape
    (r0, c0), (r1, c1) = (int(v) for v in start), (int(v) for v in goal)
    for r, c in ((r0, c0), (r1, c1)):
        if not (0 <= r < h and 0 <= c < w):
            raise ValueError("a cell of the path is outside the grid")
    a, b = r0 * w + c0, r1 * w + c1
    if a == b:
        return np.array([r0]), np.array([c0])
    _dist, pred = dijkstra(graph, directed=False, indices=a, return_predecessors=True)
    if pred[b] < 0:
        raise FollowRefused("no_path", "no path between the two cells")
    path = [b]
    while path[-1] != a:
        path.append(int(pred[path[-1]]))
    path = np.asarray(path[::-1])
    return path // w, path % w


def least_cost(strength: np.ndarray, start, goal, resolution: float) -> tuple[np.ndarray, np.ndarray]:
    """The path of least cost from the cell ``start`` (row, col) to ``goal`` on the 8-connected grid of
    ``strength`` (h, w, 0-1), each move costing its length (``resolution`` m a cell) times the mean of
    ``1 + COST_GAIN (1 - strength)`` at its two cells: ``(rows, cols)`` of its cells, from start to goal."""
    s = np.asarray(strength, np.float64)
    return _path_on(_grid_graph(s, float(resolution)), s.shape, start, goal)


# ---------------------------------------------------------------- reading the plan
def _grid_of(path, bounds, cell: float):
    """The grid of cells ``cell`` m covering ``bounds`` (x0, y0, x1, y1) cut to the raster at ``path``, the cells
    made coarser when they would be over :data:`MAX_CELLS`: ``(transform, (h, w), cell)``."""
    import rasterio
    from affine import Affine

    with rasterio.open(path) as src:
        b = src.bounds
        res = abs(float(src.res[0]))
    x0, y0 = max(bounds[0], b.left), max(bounds[1], b.bottom)
    x1, y1 = min(bounds[2], b.right), min(bounds[3], b.top)
    cell = max(float(cell), res)
    if (x1 - x0) * (y1 - y0) / cell ** 2 > MAX_CELLS:
        cell = float(np.sqrt((x1 - x0) * (y1 - y0) / MAX_CELLS)) * 1.0001
    w, h = max(1, int(np.ceil((x1 - x0) / cell))), max(1, int(np.ceil((y1 - y0) / cell)))
    return Affine(cell, 0.0, x0, 0.0, -cell, y1), (h, w), cell


def _read_on(path, transform, shape, bands=None):
    """The raster at ``path`` resampled onto the grid ``transform`` / ``shape`` (average when the cells are coarser
    than its own): float64 (bands, h, w), NaN where it has no data (its nodata, an alpha of 0, outside it)."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    h, w = shape
    with rasterio.open(path) as src:
        nodata = src.nodata
        coarser = abs(transform.a) > abs(float(src.res[0])) * 1.001
        rs = Resampling.average if coarser else Resampling.bilinear
        idx = list(bands or range(1, src.count + 1))
        alpha = 4 if src.count >= 4 and 4 not in idx else None
        extra = {} if nodata is None else {"src_nodata": nodata, "nodata": nodata}
        with WarpedVRT(src, crs=src.crs, transform=transform, width=w, height=h, resampling=rs, **extra) as vrt:
            data = vrt.read(idx).astype(np.float64)
            valid = vrt.read_masks(1) > 0
            if alpha is not None:
                valid &= vrt.read(alpha) > 0
    data[:, ~valid] = np.nan
    if nodata is not None:
        data[data == float(nodata)] = np.nan
    return data


def _on_grid(mask, transform, shape):
    """A mask ``(array, its transform)`` in the chunk's coordinates put on the grid ``transform`` / ``shape``
    (nearest cell); an array already on the grid as it is."""
    if isinstance(mask, tuple):
        m, mtr = mask
        m = np.asarray(m, bool)
        h, w = shape
        rows, cols = np.mgrid[0:h, 0:w]
        x, y = transform @ (cols + 0.5, rows + 0.5)
        mc, mr = ~mtr @ (x, y)
        mc, mr = np.floor(mc).astype(np.int64), np.floor(mr).astype(np.int64)
        inside = (mr >= 0) & (mr < m.shape[0]) & (mc >= 0) & (mc < m.shape[1])
        out = np.zeros(shape, bool)
        out[inside] = m[mr[inside], mc[inside]]
        return out
    m = np.asarray(mask, bool)
    if m.shape != tuple(shape):
        raise ValueError("the mask must be on the grid of the DEM, or given with its transform")
    return m


def _plan_strength(chunk, bounds, cell, mask=None, radius=RIDGE_RADIUS, progress=None):
    """The strength of the cells of the plan of ``chunk`` in ``bounds``: ``(strength, ridge, transform, cell)``."""
    dem_path = chunk.dem_path
    transform, shape, cell = _grid_of(dem_path, bounds, cell)
    dem = _read_on(dem_path, transform, shape, bands=[1])[0]
    grey = None
    if chunk.orthomosaic_path is not None:
        try:
            rgb = _read_on(chunk.orthomosaic_path, transform, shape, bands=[1, 2, 3])
            grey = rgb.mean(axis=0)
        except Exception:  # noqa: BLE001 - an orthophoto that cannot be read: the DEM alone
            grey = None
    m = _on_grid(mask, transform, shape) if mask is not None else None
    return (*_ridge_strength(dem, cell, ortho=grey, mask=m, radius=radius, progress=progress), transform, cell)


# ---------------------------------------------------------------- lines
def simplify_line(points, tolerance: float = SIMPLIFY) -> np.ndarray:
    """The polyline ``points`` (N, 2) simplified within ``tolerance`` m (Douglas-Peucker), its ends kept."""
    from shapely.geometry import LineString

    p = np.asarray(points, np.float64).reshape(-1, 2)
    if len(p) < 3:
        return p.copy()
    out = np.asarray(LineString(p).simplify(float(tolerance), preserve_topology=False).coords, np.float64)
    return out if len(out) >= 2 else p[[0, -1]].copy()


def _length(points) -> float:
    p = np.asarray(points, np.float64).reshape(-1, 2)
    return float(np.hypot(*np.diff(p, axis=0).T).sum()) if len(p) > 1 else 0.0


def _share_on(line, strength, transform, level: float = RIDGE) -> float:
    """The share of the length of ``line`` on cells of ``strength`` over ``level``: sampled every half cell."""
    p = np.asarray(line, np.float64).reshape(-1, 2)
    run = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(p, axis=0).T))])
    if run[-1] <= 0:
        at = p[:1]
    else:
        t = np.linspace(0.0, run[-1], max(2, int(np.ceil(run[-1] / (abs(transform.a) / 2))) + 1))
        at = np.column_stack([np.interp(t, run, p[:, 0]), np.interp(t, run, p[:, 1])])
    c, r = ~transform @ (at[:, 0], at[:, 1])
    r = np.clip(np.floor(r).astype(np.int64), 0, strength.shape[0] - 1)
    c = np.clip(np.floor(c).astype(np.int64), 0, strength.shape[1] - 1)
    return float((strength[r, c] > level).mean())


# ---------------------------------------------------------------- follow the wall
def follow(chunk, points, mask=None, radius: float = RIDGE_RADIUS, progress: Progress | None = None) -> dict:
    """The line of the wall between the points clicked on the plan of ``chunk`` - the two ends and any via points
    in between, in order, in the chunk's coordinates: the path of least cost along the crest (:func:`least_cost`
    on :func:`strength_map`) of each leg, on the DEM of the box of the points widened by :data:`WINDOW_MARGIN`, at
    ``max(its cell, MIN_CELL)`` (coarser over :data:`MAX_CELLS`); ``mask``, the wall on the orthophoto
    (``(array, transform)`` as :func:`sam_mask` gives it), raises the strength of its cells. The legs joined, each
    simplified within :data:`SIMPLIFY` with its clicked points kept. Returns ``{"points": (N, 2), "on_ridge": the
    share of its length on cells of a crest (``height`` of :func:`crest_map` over :data:`RIDGE` x
    :data:`RIDGE_HEIGHT`, the tops against a taller structure included, or in the mask), "cell"}``.
    ``RuntimeError`` without a DEM; :class:`FollowRefused` (a ``ValueError``) for fewer than two points, a point
    outside the DEM, no path."""
    import rasterio

    progress = as_progress(progress)
    p = np.asarray(points, np.float64).reshape(-1, 2)
    if len(p) < 2:
        raise FollowRefused("points", "the wall is followed between two points at least")
    if not np.all(np.isfinite(p)):
        raise FollowRefused("points", "a point of the wall is not a number")
    if chunk.dem_path is None:
        raise RuntimeError("build the DEM first: the wall is followed on its relief")
    with rasterio.open(chunk.dem_path) as src:
        b = src.bounds
    outside = (p[:, 0] < b.left) | (p[:, 0] > b.right) | (p[:, 1] < b.bottom) | (p[:, 1] > b.top)
    if outside.any():
        k = int(np.argmax(outside)) + 1
        raise FollowRefused("outside", f"point {k} is outside the DEM", point=k)
    progress(0.0, "Relief of the DEM around the wall")
    lo, hi = p.min(axis=0) - WINDOW_MARGIN, p.max(axis=0) + WINDOW_MARGIN
    strength, ridge, transform, cell = _plan_strength(chunk, (lo[0], lo[1], hi[0], hi[1]), MIN_CELL, mask, radius,
                                                      progress.sub(0.0, 0.3))
    h, w = strength.shape
    progress(0.3, "The path along the wall")
    graph = _grid_graph(strength, cell)  # once for all the legs
    cols, rows = ~transform @ (p[:, 0], p[:, 1])
    cells = np.column_stack([np.clip(np.floor(rows), 0, h - 1), np.clip(np.floor(cols), 0, w - 1)]).astype(np.int64)
    line = [p[:1]]
    for i in range(len(p) - 1):
        rr, cc = _path_on(graph, (h, w), cells[i], cells[i + 1])
        x, y = transform @ (cc + 0.5, rr + 0.5)
        leg = np.column_stack([x, y])
        leg[0], leg[-1] = p[i], p[i + 1]  # the clicked points themselves, not the middles of their cells
        line.append(simplify_line(leg)[1:])
        progress(0.4 + 0.6 * (i + 1) / (len(p) - 1))
    out = np.vstack(line)
    return {"points": out, "on_ridge": _share_on(out, ridge.astype(np.float64), transform), "cell": float(cell)}


# ---------------------------------------------------------------- wall proposals
def _skeleton_graph(skeleton: np.ndarray):
    """The pixels of ``skeleton`` (bool) as a graph: ``{pixel: set(neighbours)}`` with the pixel as ``r * w + c``,
    8-connected but without the diagonal that a pixel joining both its ends 4-wise makes redundant (the staircase
    of a thinned line is no junction)."""
    h, w = skeleton.shape
    rr, cc = np.nonzero(skeleton)
    on = set((rr * w + cc).tolist())
    nb: dict[int, set] = {k: set() for k in on}
    for k in on:
        r, c = divmod(k, w)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if (dr or dc) and 0 <= r + dr < h and 0 <= c + dc < w and (r + dr) * w + c + dc in on:
                    if dr and dc and ((r + dr) * w + c in on or r * w + c + dc in on):
                        continue  # the diagonal step is made by the two 4-wise steps
                    nb[k].add((r + dr) * w + c + dc)
    return nb


def _branches(nb: dict) -> list[list[int]]:
    """The branches of the skeleton graph ``nb``: the runs of pixels between two pixels that are not on a line
    (ends, junctions), and the closed loops, each as its pixels in order."""
    node = {k for k, v in nb.items() if len(v) != 2}
    seen_steps: set = set()
    out = []
    for a in node:
        for b in nb[a]:
            if (a, b) in seen_steps:
                continue
            run, prev, cur = [a, b], a, b
            seen_steps.add((a, b))
            while cur not in node:
                nxt = next((n for n in nb[cur] if n != prev), None)
                if nxt is None:
                    break
                seen_steps.add((cur, nxt))
                prev, cur = cur, nxt
                run.append(cur)
            seen_steps.add((cur, prev))
            out.append(run)
    visited = {k for run in out for k in run}
    for k in nb:  # loops: every pixel on a line, none visited
        if k in visited or len(nb[k]) != 2:
            continue
        run, prev, cur = [k], None, k
        while True:
            nxt = next((n for n in nb[cur] if n != prev and (n not in run or (n == k and len(run) > 2))), None)
            if nxt is None or nxt == k:
                run.append(k)
                break
            prev, cur = cur, nxt
            run.append(cur)
        visited.update(run)
        out.append(run)
    return out


def _cluster(nb: dict) -> dict:
    """Each junction pixel (three neighbours or more) mapped to the first pixel of its group of touching junction
    pixels: a junction of a thinned line is often a few pixels."""
    junction = {k for k, v in nb.items() if len(v) >= 3}
    root: dict = {}
    for k in junction:
        if k in root:
            continue
        stack, root[k] = [k], k
        while stack:
            cur = stack.pop()
            for n in nb[cur]:
                if n in junction and n not in root:
                    root[n] = k
                    stack.append(n)
    return root


def _prune(branches: list[list[int]], nb: dict, spur_px: float, w: int) -> list[list[int]]:
    """Spurs (branches from a junction to a free end shorter than ``spur_px`` pixels) taken away, again while a
    junction loses its spurs and leaves another short branch free, then the branches that meet two by two at what
    was a junction joined into one, in one pass."""
    root = _cluster(nb)

    def length(run):
        r, c = np.divmod(np.asarray(run), w)
        return float(np.hypot(np.diff(r), np.diff(c)).sum())

    def end(k):
        return ("j", root[k]) if k in root else ("e", k)

    def degrees(work):
        degree: dict = {}
        for b in work:
            for k in (b[0], b[-1]):
                degree[end(k)] = degree.get(end(k), 0) + 1
        return degree

    work = [list(b) for b in branches if not (b[0] in root and b[-1] in root and root[b[0]] == root[b[-1]]
                                             and length(b) < spur_px)]
    lengths = [length(b) for b in work]
    while True:
        degree = degrees(work)
        keep = [i for i, b in enumerate(work)
                if not (lengths[i] < spur_px and sorted(degree[end(k)] >= 3 and end(k)[0] == "j"
                                                       for k in (b[0], b[-1])) == [False, True]
                        and min(degree[end(b[0])], degree[end(b[-1])]) == 1)]
        if len(keep) == len(work):
            break
        work, lengths = [work[i] for i in keep], [lengths[i] for i in keep]
    degree = degrees(work)
    at: dict = {}  # a junction left with two branches: where they join
    for i, b in enumerate(work):
        for k in (b[0], b[-1]):
            if end(k)[0] == "j" and degree[end(k)] == 2:
                at.setdefault(end(k), []).append(i)
    used = [False] * len(work)
    out = []

    def other(j, i):
        pair = at.get(j, [])
        return next((x for x in pair if x != i), None) if len(pair) == 2 and pair[0] != pair[1] else None

    def chain(i, b):
        """``b`` (branch ``i``, oriented) carried on through the joins at its last end."""
        run = list(b)
        while True:
            j = end(run[-1])
            nxt = other(j, i)
            if nxt is None or used[nxt]:
                return run
            used[nxt] = True
            c = work[nxt] if end(work[nxt][0]) == j else work[nxt][::-1]
            run += c[1:]
            i = nxt

    starts = [i for i, b in enumerate(work) if other(end(b[0]), i) is None or other(end(b[-1]), i) is None]
    for i in starts + list(range(len(work))):  # open chains from a free end first, then the loops
        if used[i]:
            continue
        used[i] = True
        b = work[i] if other(end(work[i][0]), i) is None else work[i][::-1]
        out.append(chain(i, b))
    return out


def _clean_mask(high: np.ndarray, cell: float) -> np.ndarray:
    """The cells of crests ``high`` (bool) cleaned before they are thinned: specks and threads under :data:`SPECK` m
    opened away (by ``round(SPECK / cell)`` cells: none on cells of 0.1 m or more, where a wall is 2-3 cells wide),
    holes smaller than :data:`HOLE` m2 filled (each would make a loop of the skeleton, cut into short branches)."""
    from scipy import ndimage

    r = int(round(SPECK / float(cell)))
    if r >= 1:
        high = ndimage.binary_opening(high, np.ones((2 * r + 1, 2 * r + 1), bool))
    return _filled(high, cell)


def proposals(chunk, min_height: float = 0.3, min_length: float = 3.0, radius: float = RIDGE_RADIUS,
              progress: Progress | None = None) -> list[np.ndarray]:
    """The walls of the plan of ``chunk`` as candidate traces: the cells of the DEM (at ``max(its cell,
    MIN_CELL)``, coarser over :data:`MAX_CELLS`) that stand over ``min_height`` above the ground on both sides
    within ``radius`` m (the ``height`` of :func:`crest_map`, the tops against a taller structure left out), specks
    opened away and small holes filled (:func:`_clean_mask`), thinned to a skeleton, cut at its junctions (spurs
    shorter than :data:`SPUR` taken away and what then meets two by two joined), each branch walked from an end into a polyline, simplified within :data:`SIMPLIFY` and kept
    when ``min_length`` m long at least; longest first, in the chunk's coordinates. ``RuntimeError`` without a
    DEM."""
    import cv2
    import rasterio

    progress = as_progress(progress)
    if chunk.dem_path is None:
        raise RuntimeError("build the DEM first: the walls are proposed from its relief")
    with rasterio.open(chunk.dem_path) as src:
        b = src.bounds
    transform, shape, cell = _grid_of(chunk.dem_path, (b.left, b.bottom, b.right, b.top), MIN_CELL)
    progress(0.0, "Relief of the DEM")
    dem = _read_on(chunk.dem_path, transform, shape, bands=[1])[0]
    height, _centred, walled = crest_map(dem, cell, float(radius), progress.sub(0.0, 0.5))
    high = _clean_mask((height > float(min_height)) & ~walled, cell)
    progress(0.5, "Crests of the walls")
    skeleton = cv2.ximgproc.thinning(high.astype(np.uint8) * 255) > 0
    nb = _skeleton_graph(skeleton)
    progress(0.65, "Crests of the walls")
    h, w = skeleton.shape
    branches = _branches(nb)
    progress(0.75, "Crests of the walls")
    runs = _prune(branches, nb, SPUR / cell, w)
    progress(0.85, "Crests of the walls")
    out = []
    for i, run in enumerate(runs):
        if i % 64 == 0:
            progress(0.85 + 0.15 * i / max(len(runs), 1))
        r, c = np.divmod(np.asarray(run), w)
        x, y = transform @ (c + 0.5, r + 0.5)
        line = simplify_line(np.column_stack([x, y]))
        if _length(line) >= float(min_length):
            out.append(line)
    progress(1.0)
    return sorted(out, key=_length, reverse=True)


# ---------------------------------------------------------------- SAM where the DEM is weak
def sam_mask(chunk, segmenter, points, margin: float = WINDOW_MARGIN, max_side: int = 1536):
    """The wall on the orthophoto of ``chunk`` around ``points`` (the clicks, chunk coordinates): the orthophoto of
    their box widened by ``margin`` m (at most ``max_side`` pixels a side, :func:`..ai.sam.read_orthomosaic`)
    segmented by ``segmenter`` (:class:`..ai.sam.Segmenter`, a worker process: never PyTorch here) from the points
    as positive clicks. Returns ``(mask, transform)``, the mask (h, w) bool of the image and its transform, for the
    ``mask`` of :func:`follow`."""
    from ..ai import sam

    if chunk.orthomosaic_path is None:
        raise RuntimeError("build the orthomosaic first: SAM looks at it")
    p = np.asarray(points, np.float64).reshape(-1, 2)
    lo, hi = p.min(axis=0) - float(margin), p.max(axis=0) + float(margin)
    image, valid, transform = sam.read_orthomosaic(chunk.orthomosaic_path, (lo[0], lo[1], hi[0], hi[1]),
                                                   max_side=int(max_side))
    cols, rows = ~transform @ (p[:, 0], p[:, 1])
    local = np.column_stack([cols, rows]) - 0.5  # corner-based -> pixel-centre coordinates expected by SAM
    local[:, 0] = np.clip(local[:, 0], 0.0, image.shape[1] - 1.0)
    local[:, 1] = np.clip(local[:, 1], 0.0, image.shape[0] - 1.0)
    mask = np.asarray(segmenter.segment_points(image, local, np.ones(len(local), np.int64)), bool)
    return mask & valid, transform
