"""A wall traced on the plan by the program: followed between two clicks, or proposed on the whole plan.

A wall stands out of the DEM: its crest is a ridge of the local relief (the DEM less its mean around, ``slrm`` of
:mod:`..archaeo.relief`), and the orthophoto often shows its edges. :func:`strength_map` turns both into a strength
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

RIDGE_RADIUS = 2.0  # m: the local relief is the DEM less its mean within this
EDGE_WEIGHT = 0.5  # the strongest edge of the orthophoto counts as this much of a crest
COST_GAIN = 20.0  # a move on ground of strength 0 costs 1 + this times its length; on a crest, its length
MASK_STRENGTH = 0.8  # the strength of a cell inside the mask of the wall (SAM), at least
MIN_CELL = 0.05  # m: the DEM is looked at with cells this large at least
WINDOW_MARGIN = 10.0  # m around the box of the clicked points: the DEM read to follow the wall
MAX_CELLS = 4_000_000  # cells at most in what is read: coarser cells over this
SIMPLIFY = 0.10  # m: the line found simplified within this (a trace of a few vertices, not one per cell)
SPUR = 1.0  # m: a branch of a skeleton shorter than this, from a junction to a free end, is a spur, not a wall
RIDGE = 0.5  # the strength over which a cell is on a ridge (``on_ridge`` of :func:`follow`)


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


def strength_map(dem: np.ndarray, resolution: float, ortho: np.ndarray | None = None, mask: np.ndarray | None = None,
                 radius: float = RIDGE_RADIUS) -> np.ndarray:
    """How much each cell of ``dem`` (h, w; NaN = no data) looks like the crest of a wall, from 0 to 1: the
    positive local relief within ``radius`` m over its 98th percentile; with ``ortho`` (grey, the same grid) the
    larger of it and :data:`EDGE_WEIGHT` times the Sobel magnitude over its 98th percentile; inside ``mask`` (bool,
    the same grid) :data:`MASK_STRENGTH` at least. A cell without data counts 0 (its mask aside)."""
    from ..archaeo.relief import compute

    z = np.asarray(dem, np.float64)
    if z.ndim != 2:
        raise ValueError("the DEM must be a 2D array")
    s = _over_p98(compute(z, float(resolution), "slrm", float(radius)))
    if ortho is not None:
        import cv2

        g = np.asarray(ortho, np.float64)
        if g.shape != z.shape:
            raise ValueError("the orthophoto must be on the grid of the DEM")
        valid = np.isfinite(g)
        if valid.any():
            g = np.where(valid, g, float(np.nanmean(g))).astype(np.float32)
            mag = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
            s = np.maximum(s, EDGE_WEIGHT * _over_p98(np.where(valid, mag, 0.0)))
    if mask is not None:
        m = np.asarray(mask, bool)
        if m.shape != z.shape:
            raise ValueError("the mask must be on the grid of the DEM")
        s = np.where(m, np.maximum(s, MASK_STRENGTH), s)
    return s


# ---------------------------------------------------------------- the path of least cost
def _grid_graph(strength: np.ndarray, resolution: float):
    """The 8-connected grid of ``strength`` as one sparse matrix: each move its length times the mean of
    ``1 + COST_GAIN (1 - strength)`` at its two cells."""
    from scipy.sparse import coo_matrix

    h, w = strength.shape
    c = 1.0 + COST_GAIN * (1.0 - np.clip(np.nan_to_num(strength, nan=0.0), 0.0, 1.0))
    index = np.arange(h * w).reshape(h, w)
    rows, cols, data = [], [], []
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        a = index[max(0, -dr):h - max(0, dr), max(0, -dc):w - max(0, dc)]
        r0, c0 = max(0, -dr), max(0, -dc)
        b = index[r0 + dr:r0 + dr + a.shape[0], c0 + dc:c0 + dc + a.shape[1]]
        ca = c.ravel()[a.ravel()]
        cb = c.ravel()[b.ravel()]
        rows.append(a.ravel())
        cols.append(b.ravel())
        data.append(resolution * np.hypot(dr, dc) * (ca + cb) / 2.0)
    return coo_matrix((np.concatenate(data), (np.concatenate(rows), np.concatenate(cols))),
                      shape=(h * w, h * w)).tocsr()


def least_cost(strength: np.ndarray, start, goal, resolution: float) -> tuple[np.ndarray, np.ndarray]:
    """The path of least cost from the cell ``start`` (row, col) to ``goal`` on the 8-connected grid of
    ``strength`` (h, w, 0-1), each move costing its length (``resolution`` m a cell) times the mean of
    ``1 + COST_GAIN (1 - strength)`` at its two cells: ``(rows, cols)`` of its cells, from start to goal."""
    from scipy.sparse.csgraph import dijkstra

    s = np.asarray(strength, np.float64)
    h, w = s.shape
    (r0, c0), (r1, c1) = (int(v) for v in start), (int(v) for v in goal)
    for r, c in ((r0, c0), (r1, c1)):
        if not (0 <= r < h and 0 <= c < w):
            raise ValueError("a cell of the path is outside the grid")
    a, b = r0 * w + c0, r1 * w + c1
    if a == b:
        return np.array([r0]), np.array([c0])
    _dist, pred = dijkstra(_grid_graph(s, float(resolution)), directed=False, indices=a, return_predecessors=True)
    if pred[b] < 0:
        raise ValueError("no path between the two cells")
    path = [b]
    while path[-1] != a:
        path.append(int(pred[path[-1]]))
    path = np.asarray(path[::-1])
    return path // w, path % w


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


def _plan_strength(chunk, bounds, cell, mask=None, radius=RIDGE_RADIUS):
    """The strength of the cells of the plan of ``chunk`` in ``bounds``: ``(strength, transform, cell)``."""
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
    return strength_map(dem, cell, ortho=grey, mask=m, radius=radius), transform, cell


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
    share of its length on cells of strength over 0.5, "cell"}``. ``RuntimeError`` without a DEM, ``ValueError``
    for fewer than two points or a point outside the DEM."""
    import rasterio

    progress = as_progress(progress)
    p = np.asarray(points, np.float64).reshape(-1, 2)
    if len(p) < 2:
        raise ValueError("the wall is followed between two points at least")
    if not np.all(np.isfinite(p)):
        raise ValueError("a point of the wall is not a number")
    if chunk.dem_path is None:
        raise RuntimeError("build the DEM first: the wall is followed on its relief")
    with rasterio.open(chunk.dem_path) as src:
        b = src.bounds
    outside = (p[:, 0] < b.left) | (p[:, 0] > b.right) | (p[:, 1] < b.bottom) | (p[:, 1] > b.top)
    if outside.any():
        raise ValueError(f"point {int(np.argmax(outside)) + 1} is outside the DEM")
    progress(0.0, "Relief of the DEM around the wall")
    lo, hi = p.min(axis=0) - WINDOW_MARGIN, p.max(axis=0) + WINDOW_MARGIN
    strength, transform, cell = _plan_strength(chunk, (lo[0], lo[1], hi[0], hi[1]), MIN_CELL, mask, radius)
    h, w = strength.shape
    progress(0.4, "The path along the wall")
    cols, rows = ~transform @ (p[:, 0], p[:, 1])
    cells = np.column_stack([np.clip(np.floor(rows), 0, h - 1), np.clip(np.floor(cols), 0, w - 1)]).astype(np.int64)
    line = [p[:1]]
    for i in range(len(p) - 1):
        rr, cc = least_cost(strength, cells[i], cells[i + 1], cell)
        x, y = transform @ (cc + 0.5, rr + 0.5)
        leg = np.column_stack([x, y])
        leg[0], leg[-1] = p[i], p[i + 1]  # the clicked points themselves, not the middles of their cells
        line.append(simplify_line(leg)[1:])
        progress(0.4 + 0.6 * (i + 1) / (len(p) - 1))
    out = np.vstack(line)
    return {"points": out, "on_ridge": _share_on(out, strength, transform), "cell": float(cell)}


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
    """Spurs (branches from a junction to a free end shorter than ``spur_px`` pixels) taken away, then the branches
    that meet two by two at what was a junction joined into one, again until nothing changes."""
    root = _cluster(nb)

    def length(run):
        r, c = np.divmod(np.asarray(run), w)
        return float(np.hypot(np.diff(r), np.diff(c)).sum())

    def end(k):
        return ("j", root[k]) if k in root else ("e", k)

    work = [list(b) for b in branches if not (b[0] in root and b[-1] in root and root[b[0]] == root[b[-1]]
                                             and length(b) < spur_px)]
    changed = True
    while changed:
        changed = False
        degree: dict = {}
        for b in work:
            for k in (b[0], b[-1]):
                degree[end(k)] = degree.get(end(k), 0) + 1
        keep = []
        for b in work:
            ends = [end(b[0]), end(b[-1])]
            free = [e for e in ends if e[0] == "e" or degree.get(e, 0) == 1]
            junction = [e for e in ends if e[0] == "j" and degree.get(e, 0) >= 3]
            if len(free) == 1 and junction and length(b) < spur_px:
                changed = True
                continue
            keep.append(b)
        work = keep
        degree = {}
        for b in work:
            for k in (b[0], b[-1]):
                degree[end(k)] = degree.get(end(k), 0) + 1
        for j, d in degree.items():  # two branches meeting at a junction left with two: one branch
            if j[0] != "j" or d != 2:
                continue
            pair = [b for b in work if end(b[0]) == j or end(b[-1]) == j]
            if len(pair) != 2 or pair[0] is pair[1]:
                continue
            a, b = pair
            a = a if end(a[-1]) == j else a[::-1]
            b = b if end(b[0]) == j else b[::-1]
            work = [x for x in work if x is not pair[0] and x is not pair[1]] + [a + b[1:]]
            changed = True
            break
    return work


def proposals(chunk, min_height: float = 0.3, min_length: float = 3.0, radius: float = RIDGE_RADIUS,
              progress: Progress | None = None) -> list[np.ndarray]:
    """The walls of the plan of ``chunk`` as candidate traces: the cells of the DEM (at ``max(its cell,
    MIN_CELL)``, coarser over :data:`MAX_CELLS`) whose local relief within ``radius`` m is over ``min_height``,
    thinned to a skeleton, cut at its junctions (spurs shorter than :data:`SPUR` taken away and what then meets two
    by two joined), each branch walked from an end into a polyline, simplified within :data:`SIMPLIFY` and kept
    when ``min_length`` m long at least; longest first, in the chunk's coordinates. ``RuntimeError`` without a
    DEM."""
    import cv2
    import rasterio

    from ..archaeo.relief import compute

    progress = as_progress(progress)
    if chunk.dem_path is None:
        raise RuntimeError("build the DEM first: the walls are proposed from its relief")
    with rasterio.open(chunk.dem_path) as src:
        b = src.bounds
    transform, shape, cell = _grid_of(chunk.dem_path, (b.left, b.bottom, b.right, b.top), MIN_CELL)
    progress(0.0, "Relief of the DEM")
    dem = _read_on(chunk.dem_path, transform, shape, bands=[1])[0]
    rel = compute(dem, cell, "slrm", float(radius))
    high = np.nan_to_num(rel, nan=0.0) > float(min_height)
    progress(0.5, "Crests of the walls")
    skeleton = cv2.ximgproc.thinning(high.astype(np.uint8) * 255) > 0
    nb = _skeleton_graph(skeleton)
    h, w = skeleton.shape
    runs = _prune(_branches(nb), nb, SPUR / cell, w)
    out = []
    for run in runs:
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
