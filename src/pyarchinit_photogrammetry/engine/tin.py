"""Triangulated irregular network (TIN) of a chunk: the surface as triangles between measured points.

A DEM grid cannot hold the foot of a wall or the edge of a trench sharply; spot heights and break
lines taken in the field can, as the edges of a TIN. The vertices come from the DEM (a greedy
refinement: the cells the triangles miss the most are added until every cell is within a
tolerance), from the ground of the dense cloud or from the spot heights alone; the spot heights of
the chunk are always vertices (their measured height wins over the DEM), the break lines are
constraint edges the triangles never cross.

The kernels - the constrained Delaunay triangulation (the crate spade), the rasterisation, the
contour segments and the exact volumes - are in the native core (``ppg_core``, API 10). Without it
the numpy fallbacks run: scipy's Delaunay triangulation, which cannot hold constraints (they are
ignored, with a warning in the report and in the log), and the kernels' own rasterisation, contour
and volume formulas in numpy.

The TIN is kept as ``<chunk data>/tin/tin.npz`` + ``tin.json`` (product ``tin``,
``chunk.meta["tin"]``), in local coordinates: world = local + ``origin`` (a round number of
kilometres), in double precision.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import numpy as np

from .. import _native
from ..core import stamps
from ..core.progress import Progress, as_progress

log = logging.getLogger(__name__)

MAX_VERTICES = 2_000_000  # the greedy refinement and the thinning of the cloud stop there
FLOOR_CELLS = 0.2  # the default tolerance is never below this part of the DEM's cell (5 cm -> 1 cm)
NOT_CONSTRAINED = ("the native core (pyarchinit-photogrammetry-core 0.7, API 10) is not installed: the break lines "
                   "and the boundary are vertices of the TIN but not constraints, the triangles may cross them")
KINDS = ("breakline", "soft", "boundary")
FOLDER = "tin"


# ---------------------------------------------------------------------------------------------- kernels
class Triangulation(NamedTuple):
    triangles: np.ndarray  # (t, 3) int64, counter-clockwise
    constrained: np.ndarray  # (k, 2) int64, smaller index first
    new_xy: np.ndarray  # (j, 2) vertices made where constraints cross (index n + row)
    new_on: np.ndarray  # (j, 2) the constraint (point indices) each new vertex lies on
    native: bool  # constraints honoured (the native core) or ignored (scipy)


def _empty(shape, dtype):
    return np.zeros(shape, dtype)


def triangulate(xy: np.ndarray, constraints=None) -> Triangulation:
    """Constrained Delaunay triangulation of ``xy`` (n x 2, local coordinates) with ``constraints``
    (m x 2 point indices). Native: spade, constraints split where they cross (new vertices).
    Fallback: scipy's Delaunay, the constraints ignored (``constrained`` = those that happen to be
    edges)."""
    xy = np.ascontiguousarray(xy, np.float64).reshape(-1, 2)
    cons = np.ascontiguousarray(_empty((0, 2), np.int64) if constraints is None or len(constraints) == 0
                                else np.asarray(constraints, np.int64).reshape(-1, 2))
    fn = _native.tin_kernel("tin_triangulate")
    if fn is not None:
        tri, con, nxy, non = fn(xy, cons)
        return Triangulation(np.asarray(tri, np.int64), np.asarray(con, np.int64), np.asarray(nxy, np.float64),
                             np.asarray(non, np.int64), True)
    tri = _scipy_delaunay(xy)
    con = _edges_present(tri, cons)
    return Triangulation(tri, con, _empty((0, 2), np.float64), _empty((0, 2), np.int64), False)


def _scipy_delaunay(xy: np.ndarray) -> np.ndarray:
    if len(xy) < 3:
        return _empty((0, 3), np.int64)
    from scipy.spatial import Delaunay, QhullError

    try:
        tri = Delaunay(xy).simplices.astype(np.int64)
    except QhullError:  # all the points on a line
        return _empty((0, 3), np.int64)
    p = xy[tri]
    area = (p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1]) - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1])
    tri = tri[area != 0]
    flip = area[area != 0] < 0
    tri[flip] = tri[flip][:, [0, 2, 1]]
    return tri


def triangle_edges(triangles: np.ndarray) -> np.ndarray:
    """The unique edges (smaller index first) of ``triangles``."""
    if len(triangles) == 0:
        return _empty((0, 2), np.int64)
    e = np.concatenate([triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]])
    return np.unique(np.sort(e, axis=1), axis=0)


def _edges_present(triangles: np.ndarray, cons: np.ndarray) -> np.ndarray:
    if len(cons) == 0 or len(triangles) == 0:
        return _empty((0, 2), np.int64)
    edges = {tuple(e) for e in triangle_edges(triangles).tolist()}
    keep = [tuple(sorted(c)) for c in cons.tolist() if tuple(sorted(c)) in edges]
    return np.array(sorted(set(keep)), np.int64).reshape(-1, 2)


def rasterize(xy, z, triangles, x0: float, y0: float, cell: float, width: int, height: int) -> np.ndarray:
    """The TIN on a ``height`` x ``width`` float32 grid of cells of side ``cell`` whose top-left
    corner is (``x0``, ``y0``): each cell centre linear in its triangle, NaN outside them."""
    xy = np.ascontiguousarray(xy, np.float64).reshape(-1, 2)
    z = np.ascontiguousarray(z, np.float64)
    tri = np.ascontiguousarray(triangles, np.int64).reshape(-1, 3)
    width, height = int(width), int(height)
    fn = _native.tin_kernel("tin_rasterize")
    if fn is not None:
        return np.asarray(fn(xy, z, tri, float(x0), float(y0), float(cell), width, height))
    out = np.full((height, width), np.nan, np.float32)
    if len(tri) == 0 or width == 0 or height == 0:
        return out
    _rasterize_numpy(xy, z, tri, float(x0), float(y0), float(cell), out)
    return out


RASTER_SLACK = 1e-6  # cells: the box of a triangle widened against the rounding of its corners (as the kernel)
RASTER_EPS = -1e-9  # barycentric tolerance: cells on an edge or on the hull despite the rounding (as the kernel)


def _rasterize_numpy(xy, z, tri, x0, y0, cell, out, pairs: int = 4_000_000) -> None:
    """What the kernel does, in numpy: every triangle against the cell centres of its box, in blocks
    of triangles of about ``pairs`` (triangle, cell) pairs; a cell takes the first triangle (in
    order) that holds it, with the kernel's formulas - the same values to the last bit."""
    height, width = out.shape
    p = xy[tri]
    h = z[tri]
    det = (p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1]) - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1])
    lo, hi = p.min(axis=1), p.max(axis=1)
    r0 = np.maximum(np.ceil((y0 - hi[:, 1]) / cell - 0.5 - RASTER_SLACK), 0)
    r1 = np.minimum(np.floor((y0 - lo[:, 1]) / cell - 0.5 + RASTER_SLACK), height - 1)
    c0 = np.maximum(np.ceil((lo[:, 0] - x0) / cell - 0.5 - RASTER_SLACK), 0)
    c1 = np.minimum(np.floor((hi[:, 0] - x0) / cell - 0.5 + RASTER_SLACK), width - 1)
    ok = (det != 0) & np.isfinite(det) & (r1 >= r0) & (c1 >= c0)
    idx = np.nonzero(ok)[0]
    if len(idx) == 0:
        return
    r0, c0 = r0[idx].astype(np.int64), c0[idx].astype(np.int64)
    bw = c1[idx].astype(np.int64) - c0 + 1
    count = (r1[idx].astype(np.int64) - r0 + 1) * bw
    ends = np.cumsum(count)
    start = 0
    flat = out.reshape(-1)
    while start < len(idx):  # blocks of triangles, in order
        stop = int(np.searchsorted(ends, (ends[start - 1] if start else 0) + pairs, side="right"))
        stop = max(stop, start + 1)
        k = np.repeat(np.arange(start, stop), count[start:stop])
        first = np.cumsum(count[start:stop]) - count[start:stop]
        j = np.arange(len(k)) - np.repeat(first, count[start:stop])
        row = r0[k] + j // bw[k]
        col = c0[k] + j % bw[k]
        t = idx[k]
        x = x0 + (col + 0.5) * cell
        y = y0 - (row + 0.5) * cell
        a, b_, c = p[t, 0], p[t, 1], p[t, 2]
        l1 = ((x - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (y - a[:, 1])) / det[t]
        l2 = ((b_[:, 0] - a[:, 0]) * (y - a[:, 1]) - (x - a[:, 0]) * (b_[:, 1] - a[:, 1])) / det[t]
        l0 = 1.0 - l1 - l2
        inside = (l0 >= RASTER_EPS) & (l1 >= RASTER_EPS) & (l2 >= RASTER_EPS)
        cells = row[inside] * width + col[inside]
        cells, keep = np.unique(cells, return_index=True)  # the first triangle of each cell
        sel = np.nonzero(inside)[0][keep]
        free = np.isnan(flat[cells])
        sel, cells = sel[free], cells[free]
        hh = h[t[sel]]
        flat[cells] = (l0[sel] * hh[:, 0] + l1[sel] * hh[:, 1] + l2[sel] * hh[:, 2]).astype(np.float32)
        start = stop


def contour_segments(xy, z, triangles, levels) -> tuple[np.ndarray, np.ndarray]:
    """The contour segments of the TIN at ``levels``: ``(segments (s, 2, 2), level index (s,))``,
    triangle by triangle and level by level; in each triangle a corner is above a level when its
    height is >= the level, the segment joins the crossings of the first two edges that change side,
    each edge interpolated from its smaller vertex index (the same point from both of its triangles)."""
    xy = np.ascontiguousarray(xy, np.float64).reshape(-1, 2)
    z = np.ascontiguousarray(z, np.float64)
    tri = np.ascontiguousarray(triangles, np.int64).reshape(-1, 3)
    levels = np.ascontiguousarray(levels, np.float64).ravel()
    fn = _native.tin_kernel("tin_contours")
    if fn is not None:
        s, k = fn(xy, z, tri, levels)
        return np.asarray(s), np.asarray(k, np.int64)
    if len(tri) == 0 or len(levels) == 0:
        return _empty((0, 2, 2), np.float64), _empty(0, np.int64)
    p = xy[tri]  # (t, 3, 2)
    h = z[tri]  # (t, 3)
    lo, hi = h.min(axis=1), h.max(axis=1)
    segs, tris, idx = [], [], []
    for k, level in enumerate(levels):
        sel = np.nonzero((level >= lo) & (level <= hi))[0]
        if len(sel) == 0:
            continue
        ps, hs = p[sel], h[sel]
        pts = np.zeros((len(sel), 2, 2))
        count = np.zeros(len(sel), np.int64)
        r = np.arange(len(sel))
        for e in range(3):
            # each edge from its smaller vertex index: both triangles of an edge give the same point
            ts = tri[sel]
            fwd = ts[:, e] < ts[:, (e + 1) % 3]
            a = np.where(fwd, e, (e + 1) % 3)
            b = np.where(fwd, (e + 1) % 3, e)
            ha, hb = hs[r, a], hs[r, b]
            pa, pb = ps[r, a], ps[r, b]
            cross = (ha >= level) != (hb >= level)
            with np.errstate(divide="ignore", invalid="ignore"):  # edges that do not cross: not used
                s = (level - ha) / (hb - ha)
                x = pa[:, 0] + s * (pb[:, 0] - pa[:, 0])
                y = pa[:, 1] + s * (pb[:, 1] - pa[:, 1])
            for m in (0, 1):
                put = cross & (count == m)
                pts[put, m, 0] = x[put]
                pts[put, m, 1] = y[put]
            count += cross
        ok = count == 2
        segs.append(pts[ok])
        tris.append(sel[ok])
        idx.append(np.full(int(ok.sum()), k, np.int64))
    if not segs:
        return _empty((0, 2, 2), np.float64), _empty(0, np.int64)
    segs, tris, idx = np.concatenate(segs), np.concatenate(tris), np.concatenate(idx)
    order = np.lexsort((idx, tris))  # triangle by triangle, then level by level: the native order
    return segs[order], idx[order]


def prism_volume(xy, z, triangles, plane) -> tuple[float, float, float]:
    """Exact volumes between the TIN and the plane z = a x + b y + c (``plane`` = (a, b, c), or a
    height): ``(above, below, area)``, the volume of the TIN above the plane, below it, and the area
    in plan of the triangles."""
    xy = np.ascontiguousarray(xy, np.float64).reshape(-1, 2)
    z = np.ascontiguousarray(z, np.float64)
    tri = np.ascontiguousarray(triangles, np.int64).reshape(-1, 3)
    a, b, c = (0.0, 0.0, float(plane)) if np.isscalar(plane) else (float(v) for v in plane)
    fn = _native.tin_kernel("tin_volume")
    if fn is not None:
        return tuple(float(v) for v in fn(xy, z, tri, (a, b, c)))
    if len(tri) == 0:
        return 0.0, 0.0, 0.0
    p = xy[tri]
    area = 0.5 * np.abs((p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1])
                        - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1]))
    d = z[tri] - (a * p[:, :, 0] + b * p[:, :, 1] + c)
    total = area * d.sum(axis=1) / 3.0
    npos = (d > 0).sum(axis=1)
    above = np.where(npos == 3, total, 0.0)
    below = np.where(npos == 0, -total, 0.0)
    for want, sign in ((1, 1.0), (2, -1.0)):  # one corner alone on its side
        rows = np.nonzero(npos == want)[0]
        if len(rows) == 0:
            continue
        dd = d[rows] * sign  # the lone corner positive
        k = np.argmax(dd > 0, axis=1) if want == 1 else np.argmax(dd >= 0, axis=1)
        r = np.arange(len(rows))
        dk, da, db = dd[r, k], dd[r, (k + 1) % 3], dd[r, (k + 2) % 3]
        with np.errstate(divide="ignore", invalid="ignore"):
            lone = area[rows] * (dk / (dk - da)) * (dk / (dk - db)) * dk / 3.0
        lone = np.where(dk == 0, 0.0, lone)
        if want == 1:
            above[rows] = lone
            below[rows] = lone - total[rows]
        else:
            below[rows] = lone
            above[rows] = total[rows] + lone
    return float(above.sum()), float(below.sum()), float(area.sum())


# ----------------------------------------------------------------------------------------------- the TIN
@dataclass
class Tin:
    """A TIN in local coordinates: world = ``xy`` + ``origin``; ``z`` in the chunk's heights."""

    origin: np.ndarray
    xy: np.ndarray
    z: np.ndarray
    triangles: np.ndarray
    constrained: np.ndarray = field(default_factory=lambda: _empty((0, 2), np.int64))
    spots: np.ndarray = field(default_factory=lambda: _empty(0, np.int64))  # vertices that are spot heights
    source: str = "dem"
    made: str = ""
    info: dict = field(default_factory=dict)

    @property
    def world_xy(self) -> np.ndarray:
        return self.xy + self.origin

    @property
    def world_xyz(self) -> np.ndarray:
        return np.column_stack([self.xy + self.origin, self.z])

    @property
    def n_vertices(self) -> int:
        return len(self.xy)

    @property
    def n_triangles(self) -> int:
        return len(self.triangles)

    def save(self, folder: Path) -> Path:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "tin.npz"
        tmp = folder / "tin.tmp.npz"
        np.savez_compressed(tmp, origin=self.origin, xy=self.xy, z=self.z, triangles=self.triangles,
                            constrained=self.constrained, spots=self.spots)
        tmp.replace(path)
        (folder / "tin.json").write_text(json.dumps({"source": self.source, "made": self.made, "info": self.info},
                                                    indent=1, default=_jsonable))
        return path

    @classmethod
    def load(cls, path: Path) -> "Tin":
        path = Path(path)
        if path.is_dir():
            path = path / "tin.npz"
        with np.load(path) as f:
            arrays = {k: f[k] for k in f.files}
        meta = json.loads((path.parent / "tin.json").read_text()) if (path.parent / "tin.json").exists() else {}
        return cls(arrays["origin"], arrays["xy"], arrays["z"], arrays["triangles"], arrays["constrained"],
                   arrays.get("spots", _empty(0, np.int64)), meta.get("source", ""), meta.get("made", ""),
                   meta.get("info", {}))


def _jsonable(v):
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.tolist()
    raise TypeError(type(v))


def round_origin(xy: np.ndarray, step: float = 1000.0) -> np.ndarray:
    """A round origin (``step`` metres) near the middle of ``xy``: 0 for local frames near zero."""
    if len(xy) == 0:
        return np.zeros(2)
    mid = (np.nanmin(xy, axis=0) + np.nanmax(xy, axis=0)) / 2
    return np.round(mid / step) * step


def chunk_tin(chunk) -> Tin | None:
    """The TIN of ``chunk``, or None."""
    p = chunk.product_path("tin")
    return Tin.load(p) if p is not None and p.exists() else None


# ------------------------------------------------------------------------------------- what goes into it
def chunk_spots(chunk) -> np.ndarray:
    """The spot heights of the chunk's plan drawings as (k, 3) world x, y, z (those without a height left out)."""
    rows = []
    for q in (chunk.meta.get("drawings") or {}).get("quote") or []:
        z = q.get("quota_q", q.get("z"))
        try:
            x, y, z = float(q["x"]), float(q["y"]), float(z)
        except (KeyError, TypeError, ValueError):
            continue
        if all(math.isfinite(v) for v in (x, y, z)):
            rows.append((x, y, z))
    return np.array(rows, np.float64).reshape(-1, 3)


def normalise_lines(lines) -> list[dict]:
    """Break lines as dicts ``{id, kind, coords (n, 2 or 3)}`` (``kind`` breakline, soft or boundary)."""
    out = []
    for k, ln in enumerate(lines or []):
        kind = str(ln.get("kind") or "breakline")
        if kind not in KINDS:
            raise ValueError(f"break line {ln.get('id', k)}: kind {kind!r} is not one of {', '.join(KINDS)}")
        coords = [tuple(float(v) for v in c) for c in ln.get("coords") or []]
        if len(coords) < 2:
            continue
        width = max(len(c) for c in coords)
        arr = np.full((len(coords), 3), np.nan)
        for i, c in enumerate(coords):
            arr[i, :len(c)] = c[:3]
        if width < 3:
            arr[:, 2] = np.nan
        if not np.isfinite(arr[:, :2]).all() or np.isinf(arr[:, 2]).any():
            raise ValueError(f"break line {ln.get('id', k)}: a vertex is not finite")
        out.append({"id": ln.get("id", k), "kind": kind, "coords": arr})
    return out


class _Dem:
    """The DEM of a chunk read once (whole, or reduced when it is very large)."""

    MAX_CELLS = 40_000_000

    def __init__(self, path: Path):
        import rasterio

        with rasterio.open(path) as src:
            f = max(1, math.ceil(math.sqrt(src.width * src.height / self.MAX_CELLS)))
            shape = (max(1, src.height // f), max(1, src.width // f))
            a = src.read(1, out_shape=shape, masked=True).astype(np.float64)
            self.z = np.ma.filled(a, np.nan)
            self.z[~np.isfinite(self.z)] = np.nan
            tr = src.transform
            self.factor = f
            self.res = abs(tr.a) * src.width / shape[1]
            self.x0, self.y0 = tr.c, tr.f
            if abs(tr.b) > 0 or abs(tr.d) > 0 or tr.e > 0:
                raise ValueError("the DEM is rotated or not north-up: not supported by the TIN")
        self.height, self.width = self.z.shape

    def centres(self, rows, cols, origin=(0.0, 0.0)):
        """Cell centres, minus ``origin``: computed as the rasteriser computes them (the corner minus the
        origin, then the cells), so that a vertex on a cell centre is exactly where its cell is sampled."""
        return (self.x0 - origin[0]) + (cols + 0.5) * self.res, (self.y0 - origin[1]) - (rows + 0.5) * self.res

    def sample(self, x, y) -> np.ndarray:
        """Bilinear heights at world ``x``, ``y`` (NaN where a corner is missing)."""
        c = (np.asarray(x, float) - self.x0) / self.res - 0.5
        r = (self.y0 - np.asarray(y, float)) / self.res - 0.5
        c0, r0 = np.floor(c).astype(int), np.floor(r).astype(int)
        fc, fr = c - c0, r - r0
        c0 = np.clip(c0, 0, self.width - 1)
        r0 = np.clip(r0, 0, self.height - 1)
        c1, r1 = np.clip(c0 + 1, 0, self.width - 1), np.clip(r0 + 1, 0, self.height - 1)
        z = self.z
        v = (z[r0, c0] * (1 - fc) * (1 - fr) + z[r0, c1] * fc * (1 - fr) + z[r1, c0] * (1 - fc) * fr
             + z[r1, c1] * fc * fr)
        near = z[np.clip(np.rint(r).astype(int), 0, self.height - 1), np.clip(np.rint(c).astype(int), 0, self.width - 1)]
        return np.where(np.isfinite(v), v, near)  # a missing corner: the nearest cell

    def noise(self, windows: int = 3, side: int = 340) -> float:
        """NMAD of the cells around a 3 x 3 median, on ``windows`` x ``windows`` windows of at most
        ``side`` cells spread over the DEM (a ring survey has nothing in the middle); 0 when no window
        has 100 cells with a height."""
        from scipy import ndimage

        h, w = self.z.shape
        res = []
        for fr in (np.arange(windows) + 0.5) / windows:
            for fc in (np.arange(windows) + 0.5) / windows:
                r0, c0 = max(0, int(fr * h) - side // 2), max(0, int(fc * w) - side // 2)
                win = self.z[r0:r0 + side, c0:c0 + side]
                ok = np.isfinite(win)
                if ok.sum() < 100:
                    continue
                med = ndimage.median_filter(np.where(ok, win, np.nanmedian(win)), size=3)
                res.append((win - med)[ok])
        if not res:
            return 0.0
        res = np.concatenate(res)
        return float(1.4826 * np.median(np.abs(res - np.median(res))))


def _block_worst(err: np.ndarray, block: int) -> tuple[np.ndarray, np.ndarray]:
    """The row and column of the largest error of every ``block`` x ``block`` block that has one (> 0)."""
    h, w = err.shape
    hb, wb = math.ceil(h / block), math.ceil(w / block)
    pad = np.zeros((hb * block, wb * block), err.dtype)
    pad[:h, :w] = err
    blocks = pad.reshape(hb, block, wb, block).transpose(0, 2, 1, 3).reshape(hb, wb, block * block)
    k = np.argmax(blocks, axis=2)
    best = np.take_along_axis(blocks, k[..., None], axis=2)[..., 0]
    bi, bj = np.nonzero(best > 0)
    kk = k[bi, bj]
    return bi * block + kk // block, bj * block + kk % block


def _within(points_xy: np.ndarray, centres_xy: np.ndarray, radius: float) -> np.ndarray:
    """Which ``centres_xy`` lie within ``radius`` of any of ``points_xy``."""
    if len(points_xy) == 0 or len(centres_xy) == 0:
        return np.zeros(len(centres_xy), bool)
    from scipy.spatial import cKDTree

    d, _ = cKDTree(points_xy).query(centres_xy, distance_upper_bound=radius)
    return np.isfinite(d)


class _Builder:
    """Vertices and constraints of a TIN being built: fixed ones (spot heights, break lines) and the others."""

    def __init__(self, origin):
        self.origin = np.asarray(origin, float)
        self.fixed_xyz = _empty((0, 3), np.float64)  # local x, y and z
        self.fixed_spot = _empty(0, bool)
        self.cons: list[tuple[int, int]] = []
        self.boundary: list[np.ndarray] = []

    def add_fixed(self, xyz_world: np.ndarray, spot: bool) -> np.ndarray:
        xyz = np.asarray(xyz_world, float).reshape(-1, 3).copy()
        xyz[:, :2] -= self.origin
        start = len(self.fixed_xyz)
        self.fixed_xyz = np.vstack([self.fixed_xyz, xyz])
        self.fixed_spot = np.concatenate([self.fixed_spot, np.full(len(xyz), spot)])
        return np.arange(start, start + len(xyz))

    def spot_mask(self, n: int) -> np.ndarray:
        """Which of ``n`` vertices (fixed first) are spot heights."""
        out = np.zeros(n, bool)
        out[:len(self.fixed_spot)] = self.fixed_spot
        return out

    def triangulate(self, free_xyz: np.ndarray):
        """The triangulation of the fixed vertices then ``free_xyz`` (local); new vertices get their
        height along the constraint they lie on. Returns (xy, z, Triangulation)."""
        xyz = np.vstack([self.fixed_xyz, free_xyz])
        cons = np.array(self.cons, np.int64).reshape(-1, 2)
        t = triangulate(xyz[:, :2], cons)
        xy, z = xyz[:, :2], xyz[:, 2]
        if len(t.new_xy):
            a, b = t.new_on[:, 0], t.new_on[:, 1]
            seg = xy[b] - xy[a]
            s = np.einsum("ij,ij->i", t.new_xy - xy[a], seg) / np.maximum(np.einsum("ij,ij->i", seg, seg), 1e-300)
            s = np.clip(s, 0, 1)
            xy = np.vstack([xy, t.new_xy])
            z = np.concatenate([z, z[a] + s * (z[b] - z[a])])
        return xy, z, t


def _trim(xy, triangles, keep_cell=None, boundary=None, max_edge=None) -> np.ndarray:
    """The triangles kept: centroid on a cell ``keep_cell(x, y)`` says True, inside the boundary
    polygons (local), no edge longer than ``max_edge``."""
    if len(triangles) == 0:
        return triangles
    p = xy[triangles]
    cx, cy = p[:, :, 0].mean(axis=1), p[:, :, 1].mean(axis=1)
    keep = np.ones(len(triangles), bool)
    if keep_cell is not None:
        keep &= keep_cell(cx, cy)
    if boundary:
        import shapely

        inside = np.zeros(len(triangles), bool)
        for poly in boundary:
            inside |= shapely.contains_xy(poly, cx, cy)
        keep &= inside
    if max_edge is not None:
        e = np.max(np.stack([np.hypot(*(p[:, 1] - p[:, 0]).T), np.hypot(*(p[:, 2] - p[:, 1]).T),
                             np.hypot(*(p[:, 0] - p[:, 2]).T)]), axis=0)
        keep &= e <= max_edge
    return triangles[keep]


def _covering(xy, triangles, mask, x0, y0, res) -> np.ndarray:
    """Which ``triangles`` contain the centre of a ``True`` cell of ``mask`` (a grid of cells of side
    ``res``, top-left corner ``x0``, ``y0``): the triangles a raster of the TIN shows over those cells."""
    out = np.zeros(len(triangles), bool)
    rr, cc = np.nonzero(mask)
    if len(rr) == 0 or len(triangles) == 0:
        return out
    px, py = x0 + (cc + 0.5) * res, y0 - (rr + 0.5) * res
    p = xy[triangles]
    lo, hi = p.min(axis=1), p.max(axis=1)
    # the triangles whose box holds a cell of the mask (an integral image of the mask)
    h, w = mask.shape
    slack = 1e-6  # cells: a centre on a corner's row or column is not lost to the rounding
    c0 = np.clip(np.ceil((lo[:, 0] - x0) / res - 0.5 - slack), 0, w).astype(int)
    c1 = np.clip(np.floor((hi[:, 0] - x0) / res - 0.5 + slack), -1, w - 1).astype(int)
    r0 = np.clip(np.ceil((y0 - hi[:, 1]) / res - 0.5 - slack), 0, h).astype(int)
    r1 = np.clip(np.floor((y0 - lo[:, 1]) / res - 0.5 + slack), -1, h - 1).astype(int)
    s = np.zeros((h + 1, w + 1), np.int64)
    s[1:, 1:] = np.cumsum(np.cumsum(mask, axis=0), axis=1)
    box = (c1 >= c0) & (r1 >= r0)
    cand = np.nonzero(box)[0]
    n = (s[r1[cand] + 1, c1[cand] + 1] - s[r0[cand], c1[cand] + 1] - s[r1[cand] + 1, c0[cand]] + s[r0[cand], c0[cand]])
    cand = cand[n > 0]
    if len(cand) == 0:
        return out
    a, b, c = p[cand, 0], p[cand, 1], p[cand, 2]
    det = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1])
    step = max(1, 20_000_000 // len(cand))
    for k in range(0, len(px), step):  # every cell against every candidate, in blocks
        x, y = px[k:k + step, None], py[k:k + step, None]
        with np.errstate(divide="ignore", invalid="ignore"):
            l1 = ((x - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (y - a[:, 1])) / det
            l2 = ((b[:, 0] - a[:, 0]) * (y - a[:, 1]) - (x - a[:, 0]) * (b[:, 1] - a[:, 1])) / det
        inside = (l1 >= -1e-9) & (l2 >= -1e-9) & (1 - l1 - l2 >= -1e-9)
        out[cand[inside.any(axis=0)]] = True
    return out


def _compact(xy, z, triangles, constrained, spot_mask):
    """Only the vertices the triangles use, numbered again."""
    used = np.unique(triangles) if len(triangles) else _empty(0, np.int64)
    remap = np.full(len(xy), -1, np.int64)
    remap[used] = np.arange(len(used))
    con = remap[constrained] if len(constrained) else _empty((0, 2), np.int64)
    con = con[(con >= 0).all(axis=1)] if len(con) else con
    spots = np.nonzero(spot_mask[used])[0].astype(np.int64) if len(used) else _empty(0, np.int64)
    return xy[used], z[used], remap[triangles], np.sort(con, axis=1), spots


def build_tin(chunk, source: str = "dem", max_error: float | None = None, breaklines=None, boundary=None,
              spots: bool = True, classes=None, progress: Progress | None = None) -> Tin:
    """Build the TIN of ``chunk`` and keep it (product ``tin``).

    ``source``: ``"dem"`` (greedy refinement of the DEM until every cell is within ``max_error``,
    default twice the DEM's noise - the NMAD around a 3 x 3 median - and at least a fifth of a cell; at most 2 M vertices),
    ``"cloud"`` (the ground of the dense cloud, class 2 when it has it or ``classes``, thinned to its
    point spacing) or ``"spots"`` (the spot heights and the break lines only). ``breaklines``: dicts
    ``{id, kind, coords}`` (kind ``breakline``: constraint edges, their heights from the line when it
    has z, otherwise from the DEM at its vertices; ``soft``: their vertices only; ``boundary``: a
    closed constraint outside which no triangle is kept). ``boundary``: a polygon (shapely, world)
    that bounds the TIN as a boundary line does. ``spots``: the chunk's spot heights as vertices."""
    progress = as_progress(progress)
    if source not in ("dem", "cloud", "spots"):
        raise ValueError(f"source {source!r}: dem, cloud or spots")
    lines = normalise_lines(breaklines)
    if boundary is not None:
        lines.append({"id": "boundary", "kind": "boundary", "coords": _polygon_coords(boundary)})
    dem_path = chunk.product_path("dem")
    dem = _Dem(dem_path) if dem_path is not None and dem_path.exists() else None
    if source == "dem" and dem is None:
        raise RuntimeError("build the DEM first")
    spot_xyz = chunk_spots(chunk) if spots else _empty((0, 3), np.float64)
    warnings: list[str] = []
    info: dict = {"source": source}

    # the origin, from what the TIN will cover
    if dem is not None:
        extent = np.array([[dem.x0, dem.y0 - dem.height * dem.res], [dem.x0 + dem.width * dem.res, dem.y0]])
    else:
        parts = [spot_xyz[:, :2]] + [ln["coords"][:, :2] for ln in lines]
        extent = np.vstack([p for p in parts if len(p)]) if any(len(p) for p in parts) else _empty((0, 2), float)
    origin = round_origin(extent)
    b = _Builder(origin)
    if len(spot_xyz):
        b.add_fixed(spot_xyz, spot=True)
    bounds_local = []
    for ln in lines:
        c = ln["coords"].copy()
        missing = ~np.isfinite(c[:, 2])
        if missing.any():
            if dem is None:
                raise ValueError(f"break line {ln['id']}: no heights and no DEM to read them from")
            c[missing, 2] = dem.sample(c[missing, 0], c[missing, 1])
            if not np.isfinite(c[:, 2]).all():
                raise ValueError(f"break line {ln['id']}: a vertex outside the DEM")
        idx = b.add_fixed(c, spot=False)
        if ln["kind"] in ("breakline", "boundary"):
            b.cons += [(int(i), int(j)) for i, j in zip(idx[:-1], idx[1:]) if i != j]
        if ln["kind"] == "boundary":
            import shapely

            ring = c[:, :2] - origin
            if not np.allclose(ring[0], ring[-1]):
                b.cons.append((int(idx[-1]), int(idx[0])))
            bounds_local.append(shapely.Polygon(ring))
    if b.cons and _native.tin_kernel("tin_triangulate") is None:
        warnings.append(NOT_CONSTRAINED)
        log.warning(NOT_CONSTRAINED)

    if source == "dem":
        progress(0.02, "TIN from the DEM")
        xy, z, tri, con, spot_idx, info_dem = _refine_dem(b, dem, origin, max_error, bounds_local,
                                                          progress.sub(0.02, 0.9))
        info.update(info_dem)
        if info_dem.get("noise") == 0:
            warnings.append(f"the noise of the DEM could not be measured: tolerance of {info_dem['max_error']:.4g} m "
                            f"({FLOOR_CELLS:g} of a cell)")
        if info_dem["capped"]:
            warnings.append(f"the refinement stopped at {MAX_VERTICES:,} vertices before reaching the tolerance")
    else:
        free = _empty((0, 3), np.float64)
        max_edge = None
        if source == "cloud":
            progress(0.05, "Ground points of the dense cloud")
            free, spacing = _cloud_points(chunk, classes)
            free[:, :2] -= origin
            if len(b.fixed_xyz):  # the measured heights win: cloud points next to them go
                free = free[~_within(b.fixed_xyz[:, :2], free[:, :2], spacing)]
            max_edge = 10 * spacing  # holes of the cloud stay holes
            info["spacing"] = spacing
        progress(0.4, "Triangulating")
        xy, z, t = b.triangulate(free)
        tri = _trim(xy, t.triangles, None, bounds_local, max_edge)
        xy, z, tri, con, spot_idx = _compact(xy, z, tri, t.constrained, b.spot_mask(len(xy)))
    if len(tri) == 0:
        raise RuntimeError("the TIN has no triangles: too few points")
    tin = Tin(origin, xy, z, tri, con, spot_idx, source, datetime.now().isoformat(timespec="seconds"), {})
    progress(0.93, "Measuring the TIN against the DEM")
    info.update(n_vertices=tin.n_vertices, n_triangles=tin.n_triangles, n_constrained=len(con),
                n_spots=len(spot_idx), n_breaklines=sum(ln["kind"] == "breakline" for ln in lines),
                native=_native.tin_kernel("tin_triangulate") is not None, warnings=warnings)
    if dem is not None:
        info.update(_dem_error(tin, dem, spot_xyz, lines))
    tin.info = info
    folder = chunk.data_dir / FOLDER
    chunk._before_product_write("tin")  # a script that builds it again can be undone (core.versions.COPIED)
    path = tin.save(folder)
    chunk._set_product("tin", path)
    stamps.stamp(chunk, "tin")
    chunk.meta["tin"] = {"file": str(path.relative_to(chunk.data_dir)), "source": source, "made": tin.made,
                         **{k: v for k, v in info.items() if k not in ("source",)}}
    progress(1.0, f"TIN: {tin.n_vertices} vertices, {tin.n_triangles} triangles, {len(con)} constrained edges")
    return tin


def _polygon_coords(polygon) -> np.ndarray:
    import shapely

    geom = polygon if isinstance(polygon, shapely.Geometry) else shapely.Polygon(polygon)
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    c = np.asarray(geom.exterior.coords, float)
    out = np.full((len(c), 3), np.nan)
    out[:, :c.shape[1]] = c[:, :3]
    if c.shape[1] < 3:
        out[:, 2] = np.nan
    return out[:-1] if len(out) > 1 and np.allclose(out[0, :2], out[-1, :2]) else out


def _cloud_points(chunk, classes) -> tuple[np.ndarray, float]:
    """Ground points of the dense cloud (world), one per cell of the point spacing (coarser beyond 2 M points)."""
    from .dem import point_spacing, surface_points_world

    if classes is None:
        try:
            pts = surface_points_world(chunk, "point_cloud", [2])
        except RuntimeError as exc:
            if "build the point cloud" in str(exc):
                raise
            pts = surface_points_world(chunk, "point_cloud", None)
    else:
        pts = surface_points_world(chunk, "point_cloud", classes)
    spacing = point_spacing(pts[:, :2])
    if spacing <= 0:
        raise RuntimeError("the dense cloud is too small for a TIN")
    box = float(np.ptp(pts[:, 0]) * np.ptp(pts[:, 1]))
    spacing = max(spacing, math.sqrt(box / MAX_VERTICES))
    key = np.floor((pts[:, :2] - pts[:, :2].min(axis=0)) / spacing).astype(np.int64)
    _, first = np.unique(key[:, 0] * (int(key[:, 1].max()) + 1) + key[:, 1], return_index=True)
    return pts[np.sort(first)].astype(np.float64), float(spacing)


def _refine_dem(b: _Builder, dem: _Dem, origin, max_error, bounds_local, progress):
    """Greedy refinement: the DEM cells on a coarse grid, then, round after round, the cell of the
    largest error of every block about as large as a triangle, until every cell is within the
    tolerance. Triangles whose centroid falls on an empty cell of the DEM are dropped (the TIN follows
    the DEM's holes and concave edges)."""
    if max_error is None:
        noise = dem.noise()
        floor = FLOOR_CELLS * dem.res  # below a fifth of a cell the vertices would follow the gridding
        tol = max(2 * noise, floor)
        tolerance = "noise" if 2 * noise > floor else "floor"
    else:
        noise = None
        tol = float(max_error)
        tolerance = "given"
    if tol <= 0:
        raise ValueError("max_error must be positive")
    valid = np.isfinite(dem.z)
    h, w = dem.z.shape
    rr, cc = np.nonzero(valid)
    local = np.column_stack(dem.centres(rr, cc, origin))
    measure = valid.copy()
    if bounds_local:  # outside the boundary nothing is wanted
        import shapely

        inside = np.zeros(len(rr), bool)
        for poly in bounds_local:
            inside |= shapely.contains_xy(poly, local[:, 0], local[:, 1])
        measure[rr[~inside], cc[~inside]] = False
    # cells within a cell of a spot height or of a break line vertex are not measured: the vertex holds them
    held = _within(b.fixed_xyz[:, :2], local, dem.res * 1.01)
    measure[rr[held], cc[held]] = False
    n_measure = int(measure.sum())
    if n_measure + len(b.fixed_xyz) < 3:
        raise RuntimeError("the DEM has too few cells")

    def keep_cell(x, y):  # triangles over the DEM's empty cells are dropped
        col = np.floor((x + origin[0] - dem.x0) / dem.res).astype(int)
        row = np.floor((dem.y0 - (y + origin[1])) / dem.res).astype(int)
        inside = (col >= 0) & (col < w) & (row >= 0) & (row < h)
        out = np.zeros(len(x), bool)
        out[inside] = valid[row[inside], col[inside]]
        return out

    step = max(2, min(h, w) // 16)
    grid = np.zeros((h, w), bool)
    grid[::step, ::step] = True
    grid[-1, ::step] = grid[::step, -1] = True
    grid[-1, -1] = True
    chosen = grid & measure
    mr, mc = np.nonzero(measure)
    if len(mr):  # the extreme cells: the corners of what the DEM covers
        for k in (np.argmin(mc + mr), np.argmax(mc + mr), np.argmin(mc - mr), np.argmax(mc - mr)):
            chosen[mr[k], mc[k]] = True
    capped = False
    rounds = 0
    while True:
        rounds += 1
        r, c = np.nonzero(chosen)
        x, y = dem.centres(r, c, origin)
        free = np.column_stack([x, y, dem.z[r, c]])
        xy, z, t = b.triangulate(free)
        tri = _trim(xy, t.triangles, keep_cell, bounds_local)
        gx0, gy0 = dem.x0 - origin[0], dem.y0 - origin[1]
        for _ in range(10):  # no triangle over an empty cell of the DEM: the TIN invents no ground
            grid_tin = rasterize(xy, z, tri, gx0, gy0, dem.res, w, h)
            over = ~valid & np.isfinite(grid_tin)
            if not over.any():
                break
            tri = tri[~_covering(xy, tri, over, gx0, gy0, dem.res)]
        err = np.abs(grid_tin.astype(np.float64) - dem.z)
        err[measure & ~np.isfinite(grid_tin)] = np.inf  # a cell the triangles miss
        err[~measure | chosen | (err <= tol)] = 0.0
        n_bad = int((err > 0).sum())
        n_vert = len(xy)
        progress(min(0.95, 1 - n_bad / max(n_measure, 1)), f"TIN: {n_vert} vertices, {n_bad} cells beyond {tol:.4g} m")
        if n_bad == 0:
            break
        if n_vert >= MAX_VERTICES:
            capped = True
            break
        block = max(1, int(math.sqrt(n_measure / max(n_vert, 1))))
        br, bc = _block_worst(err, block)
        if len(br) > MAX_VERTICES - n_vert:
            worst = np.argsort(err[br, bc])[::-1][:MAX_VERTICES - n_vert]
            br, bc = br[worst], bc[worst]
        chosen[br, bc] = True
    xy, z, tri, con, spot_idx = _compact(xy, z, tri, t.constrained, b.spot_mask(len(xy)))
    info = {"max_error": tol, "tolerance": tolerance, "noise": noise, "rounds": rounds, "dem_cells": n_measure, "capped": capped,
            "dem_factor": dem.factor}
    return xy, z, tri, con, spot_idx, info


def _dem_error(tin: Tin, dem: _Dem, spot_xyz, lines) -> dict:
    """The largest and the 95th percentile difference between the TIN and the DEM over the cells it
    covers, the cells next to a spot height or a break line vertex left out."""
    grid_tin = rasterize(tin.xy, tin.z, tin.triangles, dem.x0 - tin.origin[0], dem.y0 - tin.origin[1], dem.res,
                         dem.width, dem.height)
    d = grid_tin.astype(np.float64) - dem.z
    ok = np.isfinite(d)
    held = [spot_xyz[:, :2]] + [ln["coords"][:, :2] for ln in lines]
    held = np.vstack([h for h in held if len(h)]) if any(len(h) for h in held) else _empty((0, 2), float)
    if len(held) and ok.any():
        r, c = np.nonzero(ok)
        x, y = dem.centres(r, c)
        near = _within(held, np.column_stack([x, y]), 1.5 * dem.res)
        ok[r[near], c[near]] = False
    if not ok.any():
        return {}
    a = np.abs(d[ok])
    return {"error_max": float(a.max()), "error_p95": float(np.percentile(a, 95)),
            "error_mean": float(a.mean()), "dem_resolution": float(dem.res)}


# --------------------------------------------------------------------------------------------- products
def tin_to_dem(chunk, resolution: float | None = None, name: str | None = None,
               progress: Progress | None = None) -> Path:
    """The TIN rasterised as the active DEM (the DEM as it was stays among the versions, named
    "DEM dal TIN"): on the grid of the DEM when there is one (and no ``resolution``), otherwise
    on the TIN's extent with cells of ``resolution`` (default: a tenth of the mean edge)."""
    from ..core import versions
    from .dem import NODATA, write_geotiff

    progress = as_progress(progress)
    tin = chunk_tin(chunk)
    if tin is None:
        raise RuntimeError("build the TIN first")
    src = chunk.product_path("dem")
    if src is not None and src.exists() and resolution is None:
        import rasterio

        with rasterio.open(src) as ds:
            tr, w, h = ds.transform, ds.width, ds.height
        res, x0, y0 = abs(tr.a), tr.c, tr.f
    else:
        if resolution is None:
            e = triangle_edges(tin.triangles)
            resolution = float(np.mean(np.hypot(*(tin.xy[e[:, 1]] - tin.xy[e[:, 0]]).T))) / 10
        res = float(resolution)
        lo, hi = tin.world_xy.min(axis=0), tin.world_xy.max(axis=0)
        x0, y0 = math.floor(lo[0] / res) * res, math.ceil(hi[1] / res) * res
        w, h = int(math.ceil((hi[0] - x0) / res)), int(math.ceil((y0 - lo[1]) / res))
    progress(0.1, f"Rasterising the TIN at {res:.4g} m")
    grid = rasterize(tin.xy, tin.z, tin.triangles, x0 - tin.origin[0], y0 - tin.origin[1], res, w, h)
    out = np.where(np.isfinite(grid), grid, NODATA).astype(np.float32)
    tmp = chunk.data_dir / "dem.tin.tmp.tif"
    write_geotiff(tmp, out, (x0, y0), res, chunk.crs, nodata=NODATA)
    chunk._before_product_write("dem")
    kept = versions.keep_active(chunk, "dem", move=True)
    try:
        own = chunk.own_product_path("dem")
        tmp.replace(own)
        chunk._set_product("dem", own)
        stamps.stamp(chunk, "dem")
    except BaseException:
        if kept is not None:
            versions.restore(chunk, "dem", kept)
        tmp.unlink(missing_ok=True)
        raise
    chunk.meta["dem"] = {"resolution": res, "source": "tin", "classes": None, "tin": tin.made,
                         "version": kept["id"] if kept else None}
    versions.set_active_name(chunk, "dem", (name or versions.label("DEM dal TIN"))[:80])
    progress(1.0, f"DEM from the TIN {w}x{h} px")
    return own


def contours(tin: Tin, interval: float, base: float = 0.0, index_every: int = 5) -> list[dict]:
    """Contour lines of the TIN (exact: linear on each triangle), joined into lines:
    ``[{"elevation", "index", "coords" (N, 2) world}]`` as :func:`..archaeo.terrain.contours`."""
    import shapely

    if not interval > 0:
        raise ValueError("the interval must be positive")
    if int(index_every) < 1:
        raise ValueError("index_every must be 1 or more: an index contour every N levels")
    lo, hi = float(tin.z.min()), float(tin.z.max())
    ns = np.arange(int(np.ceil((lo - base) / interval)), int(np.floor((hi - base) / interval)) + 1)
    levels = np.round(base + ns * interval, 9)
    segs, idx = contour_segments(tin.xy, tin.z, tin.triangles, levels)
    out = []
    for k, (n, level) in enumerate(zip(ns, levels)):
        s = segs[idx == k]
        if len(s) == 0:
            continue
        merged = shapely.line_merge(shapely.MultiLineString(list(s)))
        for line in getattr(merged, "geoms", [merged]):
            coords = np.asarray(line.coords)[:, :2] + tin.origin
            out.append({"elevation": float(level), "index": int(n) % index_every == 0, "coords": coords})
    return out


def tin_contours(chunk, interval: float | None = None, base: float = 0.0, index_every: int = 5) -> list[dict]:
    """Contour lines of the chunk's TIN; ``interval`` default: a round step giving about 50 levels."""
    from ..archaeo.terrain import CONTOUR_INTERVALS

    tin = chunk_tin(chunk)
    if tin is None:
        raise RuntimeError("build the TIN first")
    if interval is None:
        relief = float(tin.z.max() - tin.z.min())
        interval = next((iv for iv in CONTOUR_INTERVALS if relief / iv <= 50), CONTOUR_INTERVALS[-1])
    return contours(tin, interval, base, index_every)


def volume(tin: Tin, reference=None) -> dict:
    """Exact cut and fill of the TIN against ``reference``, with the keys of
    :func:`..archaeo.terrain.volume`: a height or a plane ``(a, b, c)`` (z = a x + b y + c, world
    coordinates) is the surface before the work - ``cut_m3`` what was removed (the TIN below it),
    ``fill_m3`` what was added; ``None`` uses the lowest vertex as floor: ``cut_m3`` the deposit above it."""
    if reference is None:
        plane = (0.0, 0.0, float(tin.z.min()))
    elif np.isscalar(reference):
        plane = (0.0, 0.0, float(reference))
    else:
        a, b_, c = (float(v) for v in reference)
        plane = (a, b_, c + a * tin.origin[0] + b_ * tin.origin[1])  # the plane in the local frame
    above, below, area = prism_volume(tin.xy, tin.z, tin.triangles, plane)
    cut, fill = (above, 0.0) if reference is None else (below, above)
    return {"area_m2": area, "cut_m3": cut, "fill_m3": fill, "net_m3": cut - fill, "above_m3": above,
            "below_m3": below, "quota_min": float(tin.z.min()), "quota_max": float(tin.z.max())}


def tin_volume(chunk, reference=None) -> dict:
    tin = chunk_tin(chunk)
    if tin is None:
        raise RuntimeError("build the TIN first")
    return volume(tin, reference)


def report(tin: Tin) -> str:
    """The TIN in a few lines: vertices, triangles, constrained edges, the error against the DEM, the warnings."""
    i = tin.info
    lines = [f"TIN ({tin.source}): {tin.n_vertices} vertices, {tin.n_triangles} triangles, "
             f"{len(tin.constrained)} constrained edges, {len(tin.spots)} spot heights"]
    if "max_error" in i:
        how = {"noise": "twice the noise of the DEM", "floor": f"{FLOOR_CELLS:g} of a cell of the DEM",
               "given": "given"}.get(i.get("tolerance"), "")
        lines.append(f"tolerance {i['max_error']:.4g} m ({how}), {i.get('rounds', 0)} rounds of refinement"
                     if how else f"tolerance {i['max_error']:.4g} m, {i.get('rounds', 0)} rounds of refinement")
    if "error_max" in i:
        lines.append(f"against the DEM: max {i['error_max']:.4f} m, 95% within {i['error_p95']:.4f} m")
    lines += [f"warning: {w}" for w in i.get("warnings", [])]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------- exports
def export(tin: Tin, path, crs: str | None = None) -> Path:
    """Write the TIN: ``.dxf`` (3DFACE on layer TIN, constrained edges as 3D lines on layer
    LINEE_DI_ROTTURA, spot heights as points on QUOTE), ``.ply`` / ``.obj`` (a mesh in world
    coordinates, double precision in the PLY), ``.gpkg`` (tables ``tin_triangoli``, ``tin_spigoli``
    with the constrained edges, ``tin_quote``, all with Z) or ``.xml`` / ``.landxml`` (LandXML 1.2,
    ``<Surface>`` with a TIN definition and the break lines)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    xyz = tin.world_xyz
    if ext == ".dxf":
        return _export_dxf(tin, xyz, path)
    if ext == ".ply":
        from ..io.mesh_ply import write_mesh_ply

        return write_mesh_ply(path, xyz, tin.triangles, double=True)
    if ext == ".obj":
        tmp = path.with_suffix(".obj.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("# TIN of pyarchinit-photogrammetry\n")
            np.savetxt(f, xyz, fmt="v %.6f %.6f %.6f")
            np.savetxt(f, tin.triangles + 1, fmt="f %d %d %d")
        tmp.replace(path)
        return path
    if ext == ".gpkg":
        return _export_gpkg(tin, xyz, path, crs)
    if ext in (".xml", ".landxml"):
        return _export_landxml(tin, xyz, path, crs)
    raise ValueError(f"{path.name}: .dxf, .ply, .obj, .gpkg or .xml (LandXML)")


def _constrained_lines(tin: Tin, xyz: np.ndarray) -> list[np.ndarray]:
    """The constrained edges joined into 3D polylines (chains through the vertices with two of them)."""
    if len(tin.constrained) == 0:
        return []
    adj: dict[int, list[int]] = {}
    for a, b in tin.constrained.tolist():
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    seen = set()
    out = []
    starts = [v for v, n in adj.items() if len(n) != 2] + list(adj)
    for s in starts:
        for nb in adj[s]:
            e = (min(s, nb), max(s, nb))
            if e in seen:
                continue
            chain = [s, nb]
            seen.add(e)
            while len(adj[chain[-1]]) == 2:
                nxt = next((v for v in adj[chain[-1]] if (min(chain[-1], v), max(chain[-1], v)) not in seen), None)
                if nxt is None:
                    break
                seen.add((min(chain[-1], nxt), max(chain[-1], nxt)))
                chain.append(nxt)
            out.append(xyz[chain])
    return out


def _export_dxf(tin: Tin, xyz: np.ndarray, path: Path) -> Path:
    import ezdxf

    doc = ezdxf.new("R2010")
    doc.layers.add("TIN", color=8)
    doc.layers.add("LINEE_DI_ROTTURA", color=1)
    doc.layers.add("QUOTE", color=2)
    msp = doc.modelspace()
    for t in tin.triangles:
        a, b, c = (tuple(map(float, xyz[k])) for k in t)
        msp.add_3dface([a, b, c, c], dxfattribs={"layer": "TIN"})
    for line in _constrained_lines(tin, xyz):
        msp.add_polyline3d([tuple(map(float, p)) for p in line], dxfattribs={"layer": "LINEE_DI_ROTTURA"})
    for k in tin.spots:
        msp.add_point(tuple(map(float, xyz[k])), dxfattribs={"layer": "QUOTE"})
    tmp = path.with_suffix(".tmp.dxf")
    doc.saveas(tmp)
    tmp.replace(path)
    return path


def _export_gpkg(tin: Tin, xyz: np.ndarray, path: Path, crs) -> Path:
    import shapely

    from ..io import gpkg

    srs = gpkg.srs_of(crs)
    if path.exists() and not gpkg.is_geopackage(path):
        gpkg.refuse_foreign(path)
        path.unlink()
    p = xyz[tin.triangles]
    tris = shapely.polygons(np.concatenate([p, p[:, :1]], axis=1))
    area = shapely.area(tris)
    tables = [
        ("tin_triangoli", "POLYGON", {"area": "REAL", "quota_min": "REAL", "quota_max": "REAL"},
         [(g, {"area": float(a), "quota_min": float(t[:, 2].min()), "quota_max": float(t[:, 2].max())})
          for g, a, t in zip(tris, area, p)], "TIN: triangoli"),
        ("tin_spigoli", "LINESTRING", {"tipo": "TEXT"},
         [(shapely.LineString(line), {"tipo": "linea di rottura"}) for line in _constrained_lines(tin, xyz)],
         "TIN: linee di rottura"),
        ("tin_quote", "POINT", {"quota": "REAL"},
         [(shapely.Point(*xyz[k]), {"quota": float(xyz[k, 2])}) for k in tin.spots], "TIN: quote"),
    ]
    con = gpkg.connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        gpkg.create(con, srs)
        for table, geom_type, cols, feats, ident in tables:
            names = gpkg.ensure_table(con, table, geom_type, cols, srs[0], ident, recreate=True, z=True)
            gpkg.replace_rows(con, table, names, feats, srs[0], z=True)
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return path


LANDXML_NS = "http://www.landxml.org/schema/LandXML-1.2"


def _export_landxml(tin: Tin, xyz: np.ndarray, path: Path, crs) -> Path:
    from xml.sax.saxutils import quoteattr

    from .. import __version__
    from ..core import crs as crs_mod

    now = datetime.now()
    code = crs_mod.epsg_code(crs) if crs else None
    tmp = path.with_suffix(path.suffix + ".tmp")
    lo, hi = xyz[:, 2].min(), xyz[:, 2].max()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(f'<LandXML xmlns="{LANDXML_NS}" version="1.2" date="{now:%Y-%m-%d}" time="{now:%H:%M:%S}">\n')
        f.write('  <Units><Metric linearUnit="meter" areaUnit="squareMeter" volumeUnit="cubicMeter" '
                'angularUnit="decimal degrees" directionUnit="decimal degrees"/></Units>\n')
        if code is not None:
            f.write(f'  <CoordinateSystem epsgCode="{code}"/>\n')
        f.write(f'  <Application name="pyarchinit-photogrammetry" version={quoteattr(__version__)}/>\n')
        f.write('  <Surfaces>\n')
        f.write(f'    <Surface name="TIN" desc={quoteattr(tin.source)}>\n')
        lines = _constrained_lines(tin, xyz)
        if lines:
            f.write('      <SourceData>\n        <Breaklines>\n')
            for k, line in enumerate(lines):
                pts = " ".join(f"{y:.4f} {x:.4f} {z:.4f}" for x, y, z in line)
                f.write(f'          <Breakline brkType="standard" name="{k + 1}"><PntList3D>{pts}</PntList3D>'
                        '</Breakline>\n')
            f.write('        </Breaklines>\n      </SourceData>\n')
        f.write(f'      <Definition surfType="TIN" elevMin="{lo:.4f}" elevMax="{hi:.4f}">\n        <Pnts>\n')
        for k, (x, y, z) in enumerate(xyz):  # LandXML: northing easting elevation
            f.write(f'          <P id="{k + 1}">{y:.4f} {x:.4f} {z:.4f}</P>\n')
        f.write('        </Pnts>\n        <Faces>\n')
        for a, b, c in tin.triangles + 1:
            f.write(f'          <F>{a} {b} {c}</F>\n')
        f.write('        </Faces>\n      </Definition>\n    </Surface>\n  </Surfaces>\n</LandXML>\n')
    tmp.replace(path)
    return path


def read_landxml(path) -> tuple[np.ndarray, np.ndarray]:
    """The first TIN surface of a LandXML file: vertices (n, 3) world x, y, z and triangles (t, 3)."""
    import xml.etree.ElementTree as ET

    root = ET.parse(path).getroot()
    ns = {"l": root.tag[1:].split("}")[0]} if root.tag.startswith("{") else {"l": ""}
    q = (lambda tag: f"l:{tag}") if ns["l"] else (lambda tag: tag)
    definition = root.find(f".//{q('Surface')}/{q('Definition')}", ns)
    if definition is None:
        raise ValueError(f"{path}: no TIN surface")
    ids, pts = [], []
    for p in definition.find(q("Pnts"), ns):
        n, e, z = (float(v) for v in p.text.split()[:3])
        ids.append(int(p.get("id")))
        pts.append((e, n, z))
    index = {i: k for k, i in enumerate(ids)}
    faces = [[index[int(v)] for v in f.text.split()[:3]] for f in definition.find(q("Faces"), ns)
             if f.get("i") != "1"]
    return np.array(pts, float), np.array(faces, np.int64).reshape(-1, 3)
