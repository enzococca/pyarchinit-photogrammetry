"""Orthomosaic generation (GeoTIFF RGBA in the chunk CRS).

Every ground cell is lifted to the surface (the DEM, or the mesh with vertical rays), projected
into the photos, and coloured from the best photo that really sees it (occlusion test against
the mesh when one exists): ``mosaic`` keeps one photo per cell (sharp, like a true orthophoto),
``average`` blends the valid photos.

The orthomosaic is made tile by tile and every tile is written at once, so its size is limited
by the disk, not by the memory (45,000 x 46,000 px for a tell at 1.35 cm/px): the surface is read
one window at a time, every tile looks only at the few photos that see it best (found from the
ground footprints of the photos), and tiles are processed in parallel.

Patches (:mod:`.patches`) are polygons whose cells take their colours from photos chosen by
hand (or from every photo but some): every build applies them, and an update redraws only the
cells of the patches that changed.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress
from .dem import NODATA, ensure_level, estimate_gsd
from .visibility import project_valid, raycaster_for_chunk, read_photo, readable_cameras, sample


# ---------------------------------------------------------------- the surface under the cells
def _cells(length: float, resolution: float) -> int:
    """Cells of ``resolution`` covering ``length`` (6 x 0.05 / 0.05 is 6, not 7)."""
    return max(1, int(np.ceil(length / resolution - 1e-6)))


class _DemSurface:
    """Heights of the ortho cells from a DEM GeoTIFF, read one window at a time (bilinear, the
    cells without data around are left out). The orthomosaic covers the DEM."""

    def __init__(self, path: Path, resolution: float):
        import rasterio

        self.path = str(path)
        with rasterio.open(self.path) as src:
            self.t, self.nodata = src.transform, src.nodata
            self.width, self.height = src.width, src.height
            dem_res = abs(src.transform.a)
        self.origin = (self.t.c, self.t.f)
        self.shape = (_cells(self.height * dem_res, resolution), _cells(self.width * dem_res, resolution))
        self.resolution = resolution
        self._local = threading.local()

    def _src(self):
        src = getattr(self._local, "src", None)
        if src is None:
            import rasterio

            src = self._local.src = rasterio.open(self.path)
        return src

    def tile(self, r0: int, c0: int, th: int, tw: int) -> np.ndarray:
        import cv2
        from rasterio.windows import Window

        res, t = self.resolution, self.t
        x = self.origin[0] + (c0 + np.arange(tw) + 0.5) * res
        y = self.origin[1] - (r0 + np.arange(th) + 0.5) * res
        colf = (x - t.c) / t.a - 0.5  # fractional DEM pixel, centres on integers
        rowf = (y - t.f) / t.e - 0.5
        a0 = max(int(np.floor(colf.min())) - 1, 0)
        a1 = min(int(np.ceil(colf.max())) + 2, self.width)
        b0 = max(int(np.floor(rowf.min())) - 1, 0)
        b1 = min(int(np.ceil(rowf.max())) + 2, self.height)
        if a1 <= a0 or b1 <= b0:
            return np.full((th, tw), np.nan)
        dem = self._src().read(1, window=Window(a0, b0, a1 - a0, b1 - b0)).astype(np.float32)
        valid = np.isfinite(dem)
        if self.nodata is not None:
            valid &= dem != self.nodata
        mx, my = np.meshgrid((colf - a0).astype(np.float32), (rowf - b0).astype(np.float32))
        filled = np.where(valid, dem, 0).astype(np.float32)
        v = valid.astype(np.float32)
        num = cv2.remap(filled, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        den = cv2.remap(v, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(den > 0.5, num / den, np.nan).astype(np.float64)


class _GridSurface(_DemSurface):
    """Heights from a grid in memory (the highest dense point of each cell, gaps filled), sampled
    like a DEM: for a chunk with neither a DEM nor a model."""

    def __init__(self, chunk: Chunk, resolution: float):
        from rasterio.fill import fillnodata
        from rasterio.transform import from_origin

        from .dem import grid_points, surface_points_world

        pts = surface_points_world(chunk, "point_cloud")
        span = max(float(np.ptp(pts[:, 0])), float(np.ptp(pts[:, 1])), resolution)
        cell = max(resolution, span / 8000.0)  # a grid of at most about 8000 x 8000 cells
        z, origin, _ = grid_points(pts, cell, reducer="max")
        mask = np.isfinite(z).astype(np.uint8)
        filled = fillnodata(np.nan_to_num(z).astype(np.float32), mask=mask, max_search_distance=10)
        self.grid = np.where(mask > 0, z, np.where(filled != 0, filled, np.nan)).astype(np.float32)
        self.t = from_origin(origin[0], origin[1], cell, cell)
        self.nodata = None
        self.height, self.width = self.grid.shape
        self.origin = origin
        self.shape = (_cells(self.height * cell, resolution), _cells(self.width * cell, resolution))
        self.resolution = resolution

    def _src(self):
        grid = self.grid

        class _Reader:
            @staticmethod
            def read(_band, window):
                return grid[window.row_off:window.row_off + window.height, window.col_off:window.col_off + window.width]

        return _Reader()


class _MeshSurface:
    """Heights of the ortho cells from the mesh: the first surface met by a vertical ray from
    above (a true orthophoto of overhangs and walls)."""

    def __init__(self, chunk: Chunk, raycaster, resolution: float, lock: threading.Lock):
        self.chunk, self.raycaster, self.lock = chunk, raycaster, lock
        lo, hi = _model_world_bounds(chunk)
        self.resolution = resolution
        x0 = np.floor(lo[0] / resolution) * resolution
        y1 = np.ceil(hi[1] / resolution) * resolution
        self.origin = (x0, y1)
        self.shape = (int(np.ceil((y1 - lo[1]) / resolution)) + 1, int(np.ceil((hi[0] - x0) / resolution)) + 1)
        self.top = float(hi[2]) + 1.0
        inv = np.linalg.inv(chunk.transform)
        self.inv = inv
        down = inv[:3, :3] @ np.array([0.0, 0.0, -1.0])
        self.down = down / np.linalg.norm(down)

    def tile(self, r0: int, c0: int, th: int, tw: int) -> np.ndarray:
        res = self.resolution
        cc, rr = np.meshgrid(np.arange(tw), np.arange(th))
        x = self.origin[0] + (c0 + cc.ravel() + 0.5) * res
        y = self.origin[1] - (r0 + rr.ravel() + 0.5) * res
        start = np.column_stack([x, y, np.full(len(x), self.top)]) @ self.inv[:3, :3].T + self.inv[:3, 3]
        with self.lock:
            t = self.raycaster.hit_distance(start, np.repeat(self.down[None], len(start), 0))
        z = np.where(np.isfinite(t), self.top - t * self.chunk.scale, np.nan)
        return z.reshape(th, tw)


def _model_world_bounds(chunk: Chunk) -> tuple[np.ndarray, np.ndarray]:
    """Lowest and highest corner of the model in world coordinates (vertices read in blocks)."""
    from ..core.pointcloud import _read_binary_ply_vertices

    path = chunk.product_path("model")
    arr = _read_binary_ply_vertices(path, mmap=True) if path is not None else None
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    if arr is None:
        v = chunk.to_world(np.asarray(chunk.model.vertices))
        return v.min(axis=0), v.max(axis=0)
    for s in range(0, len(arr), 2_000_000):
        b = arr[s:s + 2_000_000]
        v = chunk.to_world(np.column_stack([b["x"], b["y"], b["z"]]).astype(np.float64))
        lo, hi = np.minimum(lo, v.min(axis=0)), np.maximum(hi, v.max(axis=0))
    return lo, hi


def _surface(chunk: Chunk, resolution: float, surface: str, raycaster, lock):
    if (surface == "model" or chunk.dem_path is None) and raycaster is not None:
        return _MeshSurface(chunk, raycaster, resolution, lock)
    if chunk.dem_path is not None and chunk.dem_path.exists():
        return _DemSurface(chunk.dem_path, resolution)
    return _GridSurface(chunk, resolution)


# ---------------------------------------------------------------- photos of a tile
class _Footprints:
    """Where every photo looks: its centre and the rays of its corners and centre (world
    frame). The ground box of a photo at any height is where these rays cross that height, so
    the photos of a tile are found without projecting anything."""

    def __init__(self, chunk: Chunk, cams):
        rot = chunk.transform[:3, :3] / chunk.scale
        centres, rays = [], []
        for cam in cams:
            s = chunk.sensor(cam)
            px = np.array([[0.0, 0.0], [s.width, 0.0], [s.width, s.height], [0.0, s.height],
                           [s.width / 2, s.height / 2]])
            try:
                xy = s.unproject(px)
            except Exception:  # pragma: no cover - a calibration that cannot be inverted at the corners
                k = s.K
                xy = (px - k[:2, 2]) / np.diag(k)[:2]
            d_cam = np.column_stack([xy, np.ones(len(xy))])
            d = (d_cam @ cam.transform[:3, :3].T) @ rot.T
            rays.append(d / np.linalg.norm(d, axis=1, keepdims=True))
            centres.append(chunk.to_world(cam.center[None])[0])
        self.centres = np.array(centres)  # (n, 3)
        self.rays = np.array(rays)  # (n, 5, 3)

    def candidates(self, box, z: float) -> np.ndarray:
        """Indices of the photos whose footprint on the plane at height ``z`` meets ``box``
        (xmin, ymin, xmax, ymax); a corner looking at the horizon counts as unbounded."""
        dz = self.rays[:, :, 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (z - self.centres[:, None, 2]) / dz
        far = ~(t > 0) | ~np.isfinite(t)
        p = self.centres[:, None, :2] + np.where(far[..., None], 0.0, t[..., None]) * self.rays[:, :, :2]
        lo, hi = p.min(axis=1), p.max(axis=1)
        # an unbounded corner: the footprint reaches the horizon on that side
        dirs = self.rays[:, :, :2]
        for k in range(2):
            lo[:, k] = np.where((far & (dirs[:, :, k] < 0)).any(axis=1), -np.inf, lo[:, k])
            hi[:, k] = np.where((far & (dirs[:, :, k] > 0)).any(axis=1), np.inf, hi[:, k])
        ok = (lo[:, 0] <= box[2]) & (hi[:, 0] >= box[0]) & (lo[:, 1] <= box[3]) & (hi[:, 1] >= box[1])
        ok &= ~far.all(axis=1)
        return np.nonzero(ok)[0]


class _PhotoCache:
    """Decoded photos shared by the tile workers (least recently used ones dropped)."""

    def __init__(self, max_items: int):
        self.max_items = max_items
        self._items: OrderedDict[int, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, cam) -> np.ndarray:
        with self._lock:
            img = self._items.get(cam.key)
            if img is not None:
                self._items.move_to_end(cam.key)
                return img
        img = read_photo(cam)  # decoded outside the lock: several photos at once
        with self._lock:
            self._items[cam.key] = img
            self._items.move_to_end(cam.key)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)
        return img


# ---------------------------------------------------------------- classes left out
class _ClassGrid:
    """The class of the visible surface - the highest point of the dense cloud - on a grid at
    the point spacing (or the pixel, when coarser), widened by one cell so that the edges of a
    hidden object go too; cells without points stay visible."""

    def __init__(self, chunk: Chunk, origin, resolution: float, hide_classes):
        from scipy import ndimage

        from ..core.pointcloud import NOISE_CLASSES

        pc = chunk.point_cloud
        if pc is None:
            raise RuntimeError("the classes come from the dense cloud: build it first")
        keep = pc.class_mask(exclude=NOISE_CLASSES)
        xyz = chunk.to_world(pc.xyz[keep])
        codes = pc.classification[keep]
        spacing = (chunk.meta.get("dem") or {}).get("resolution") or 0.0
        if not spacing:
            area = max(np.ptp(xyz[:, 0]) * np.ptp(xyz[:, 1]), 1e-12)
            spacing = 1.5 * np.sqrt(area / max(len(xyz), 1))
        self.cs = cs = max(float(spacing), resolution)
        self.origin, self.resolution = origin, resolution
        col = np.floor((xyz[:, 0] - origin[0]) / cs).astype(np.int64)
        row = np.floor((origin[1] - xyz[:, 1]) / cs).astype(np.int64)
        ok = (col >= 0) & (row >= 0)
        hc, wc = int(row[ok].max(initial=0)) + 2, int(col[ok].max(initial=0)) + 2
        idx, z, codes = row[ok] * wc + col[ok], xyz[ok, 2], codes[ok]
        top = np.full(hc * wc, -np.inf)
        np.maximum.at(top, idx, z)
        grid = np.zeros(hc * wc, np.int16) - 1  # -1: no point
        first = z >= top[idx]
        grid[idx[first]] = codes[first]
        hidden = np.isin(grid, np.asarray([int(c) for c in hide_classes])).reshape(hc, wc)
        self.hidden = ndimage.binary_dilation(hidden)

    def tile(self, r0: int, c0: int, th: int, tw: int) -> np.ndarray:
        hc, wc = self.hidden.shape
        rows = np.minimum(((r0 + np.arange(th) + 0.5) * self.resolution / self.cs).astype(np.int64), hc - 1)
        cols = np.minimum(((c0 + np.arange(tw) + 0.5) * self.resolution / self.cs).astype(np.int64), wc - 1)
        return self.hidden[np.ix_(rows, cols)]


def hidden_cells(chunk: Chunk, origin, shape, resolution: float, hide_classes) -> np.ndarray:
    """Cells of a grid (``origin`` = top-left corner, ``shape``, ``resolution``) whose visible
    surface - the highest point of the dense cloud there - belongs to ``hide_classes``."""
    return _ClassGrid(chunk, origin, resolution, hide_classes).tile(0, 0, *shape)


# ---------------------------------------------------------------- one tile
def _scores(chunk: Chunk, cam, pts, centres, up_int, min_cos: float = 0.0):
    """Pixels and score (frontal, central, fine) of points in a photo; -inf where not seen, or
    seen from further than the vertical than the angle whose cosine is ``min_cos``."""
    px, depth, ok = project_valid(chunk, cam, pts)
    score = np.full(len(pts), -np.inf, np.float32)
    if ok.any():
        sensor = chunk.sensor(cam)
        ray = pts[ok] - centres[cam.key]
        ray /= np.linalg.norm(ray, axis=1, keepdims=True)
        nadir = np.clip(-(ray @ up_int), 0, 1)  # 1 when looking straight down onto the surface
        off = np.hypot((px[ok, 0] / sensor.width - 0.5), (px[ok, 1] / sensor.height - 0.5))
        gsd = depth[ok] / sensor.focal_px
        s = (nadir ** 2) * (1 - off) / np.maximum(gsd, 1e-12)
        score[ok] = np.where(nadir >= min_cos, s, -np.inf)
    return px, score


class _Context:
    pass


def _probe(pts):
    """A sample of the cells of a tile, to rank its photos."""
    return pts[:: max(1, len(pts) // 400)]


def _rank(ctx, probe, near) -> list:
    """The photos of a tile: the ``max_cameras`` that see the cells of ``probe`` best, and
    those that reach cells the best ones do not."""
    ranked = []
    for cam in near:
        _px, s = _scores(ctx.chunk, cam, probe, ctx.centres, ctx.up_int, ctx.min_cos)
        seen = np.isfinite(s)
        if seen.any():
            ranked.append((float(np.where(seen, s, 0).sum()), cam, seen))
    ranked.sort(key=lambda r: -r[0])
    tcams = [r[1] for r in ranked[:ctx.max_cameras]]
    covered = np.zeros(len(probe), bool)
    for r in ranked[:ctx.max_cameras]:
        covered |= r[2]
    for _score, cam, seen in ranked[ctx.max_cameras:]:  # the cells the best photos do not reach
        if (seen & ~covered).any():
            tcams.append(cam)
            covered |= seen
    return tcams


def _colour(ctx, pts, near, want_choice: bool = False, colours: bool = True):
    """Colours ``(n, 3)`` (BGR) of surface points ``pts`` (internal frame) from the photos
    ``near``, and which points got one; with ``want_choice`` also the key of the photo of every
    point (-1 for none). ``colours=False`` only chooses the photos (none is read)."""
    return _paint(ctx, pts, _rank(ctx, _probe(pts), near), want_choice, colours)


def _paint(ctx, pts, tcams, want_choice: bool = False, colours: bool = True):
    """Like :func:`_colour`, from the photos ``tcams`` already chosen for the tile: every point
    takes its colour from the best of them that sees it (whatever other points are painted
    with it, so a part of a tile comes out as in the whole tile)."""
    chunk = ctx.chunk
    n = len(pts)
    colors = np.zeros((n, 3), np.float64)
    got = np.zeros(n, bool)
    keys = np.full(n, -1)
    if not tcams or not n:
        return (colors, got, keys) if want_choice else (colors, got)
    scores = np.full((len(tcams), n), -np.inf, np.float32)
    pix = np.full((len(tcams), n, 2), np.nan, np.float32)
    for k, cam in enumerate(tcams):
        px, s = _scores(chunk, cam, pts, ctx.centres, ctx.up_int, ctx.min_cos)
        scores[k] = s
        ok = np.isfinite(s)
        pix[k, ok] = px[ok]
    order = np.argsort(-scores, axis=0)
    chosen = np.full(n, -1)

    def visible(cam, sel):
        with ctx.lock:
            return ctx.raycaster.visible_from(ctx.centres[cam.key], pts[sel], abs_tol=ctx.abs_tol)

    if ctx.blending == "average":
        weight = np.zeros(n, np.float64)
        for k, cam in enumerate(tcams):
            ok = np.isfinite(scores[k])
            if ctx.raycaster is not None and ok.any():
                idx = np.nonzero(ok)[0]
                ok[idx[~visible(cam, idx)]] = False
            if ok.any():
                wgt = scores[k, ok].astype(np.float64)
                if colours:
                    colors[ok] += sample(ctx.cache.get(cam), pix[k, ok]) * wgt[:, None]
                weight[ok] += wgt
        got = weight > 0
        colors[got] /= weight[got, None]
        chosen[got] = order[0, got]
    else:
        pending = np.arange(n)
        for rank in range(min(4, len(tcams))):
            if len(pending) == 0:
                break
            cand = order[rank, pending]
            ok = np.isfinite(scores[cand, pending])
            pending, cand = pending[ok], cand[ok]
            still = []
            for k in np.unique(cand):
                sel = pending[cand == k]
                if ctx.raycaster is not None:
                    vis = visible(tcams[k], sel)
                    still.append(sel[~vis])
                    sel = sel[vis]
                chosen[sel] = k
            pending = np.concatenate(still) if still else np.zeros(0, int)
        # cells hidden in every candidate photo (steep faces): take the best photo anyway
        fallback = (chosen < 0) & np.isfinite(scores.max(axis=0))
        chosen[fallback] = order[0, fallback]
        for k in np.unique(chosen[chosen >= 0]) if colours else ():
            sel = np.nonzero(chosen == k)[0]
            colors[sel] = sample(ctx.cache.get(tcams[k]), pix[k, sel])
        got = chosen >= 0
    if want_choice:
        keys[got] = np.array([tcams[k].key for k in chosen[got]], dtype=keys.dtype) if got.any() else keys[got]
        return colors, got, keys
    return colors, got


def _patch_owner(ctx, box, X, Y) -> np.ndarray:
    """Index in ``ctx.patches`` of the patch every cell lies in (-1 outside; a later patch
    covers an earlier one)."""
    from shapely import contains_xy

    owner = np.full(len(X), -1)
    for i, patch in enumerate(ctx.patches):
        b = patch.bounds
        if b[0] > box[2] or b[2] < box[0] or b[1] > box[3] or b[3] < box[1]:
            continue
        owner[contains_xy(patch.geom, X, Y)] = i
    return owner


def _in_any(geoms, box, X, Y) -> np.ndarray:
    from shapely import contains_xy

    inside = np.zeros(len(X), bool)
    for g in geoms:
        b = g.bounds
        if b[0] > box[2] or b[2] < box[0] or b[1] > box[3] or b[3] < box[1]:
            continue
        inside |= contains_xy(g, X, Y)
    return inside


def _tile(ctx, r0: int, c0: int):
    """RGBA of one tile (4, th, tw), or None when the surface is empty there. When updating
    patches (``ctx.redo``: the polygons that changed) only the cells inside them are made:
    the result is then ``(rgba, done, from_base)``, the cells made and the cells that take
    the pixels of the unpatched orthomosaic of another program (``ctx.base``)."""
    res = ctx.resolution
    th, tw = min(ctx.tile, ctx.shape[0] - r0), min(ctx.tile, ctx.shape[1] - c0)
    box = (ctx.origin[0] + c0 * res, ctx.origin[1] - (r0 + th) * res,
           ctx.origin[0] + (c0 + tw) * res, ctx.origin[1] - r0 * res)
    redo = getattr(ctx, "redo", None)
    zz = ctx.surface.tile(r0, c0, th, tw)
    valid = np.isfinite(zz)
    todo = None
    if redo is not None:  # only the cells of the patches that changed
        rows, cols = np.mgrid[0:th, 0:tw]
        todo = _in_any(redo, box, ctx.origin[0] + (c0 + cols.ravel() + 0.5) * res,
                       ctx.origin[1] - (r0 + rows.ravel() + 0.5) * res).reshape(th, tw)
        if not todo.any():
            return None
        if not (valid & todo).any():
            from_base = todo if ctx.base is not None else np.zeros_like(todo)
            return np.zeros((4, th, tw), np.uint8), todo & ~from_base, from_base
    if not valid.any():
        return None
    rr, cc = np.nonzero(valid)
    X = ctx.origin[0] + (c0 + cc + 0.5) * res
    Y = ctx.origin[1] - (r0 + rr + 0.5) * res
    world = np.column_stack([X, Y, zz[rr, cc]])
    pts = world @ ctx.inv[:3, :3].T + ctx.inv[:3, 3]
    # the photos of this tile: those whose footprint meets it, ranked on a sample of all its
    # cells - with or without patches, and when only some cells are made again
    near = [ctx.cams[i] for i in ctx.footprints.candidates(box, float(np.median(world[:, 2])))]
    probe = _probe(pts)
    tcams = _rank(ctx, probe, near)
    if todo is not None:  # from here on, the cells to make only
        keep = todo[rr, cc]
        rr, cc, X, Y, pts = rr[keep], cc[keep], X[keep], Y[keep], pts[keep]
    n = len(pts)
    owner = _patch_owner(ctx, box, X, Y) if ctx.patches else np.full(n, -1)
    colors = np.zeros((n, 3), np.float64)
    got = np.zeros(n, bool)
    base = np.zeros(n, bool)  # cells left to the unpatched orthomosaic of another program
    for i in np.unique(owner):
        idx = np.nonzero(owner == i)[0]
        if i < 0:
            if redo is not None and ctx.base is not None:
                base[idx] = True  # a patch taken away: the original pixels again
                continue
            cams = tcams
        else:
            patch = ctx.patches[i]
            cams = patch.cams if patch.mode == "assign" else _rank(ctx, probe, patch.photos(near))
        c, g = _paint(ctx, pts[idx], cams)
        colors[idx[g]], got[idx[g]] = c[g], True
        miss = idx[~g]
        if i >= 0 and len(miss) and ctx.patches[i].mode == "assign":  # not seen by its photos: as without it
            if redo is not None and ctx.base is not None:
                base[miss] = True
            else:
                c, g = _paint(ctx, pts[miss], tcams)
                colors[miss[g]], got[miss[g]] = c[g], True
    out = np.zeros((4, th, tw), np.uint8)
    bgr = np.clip(colors[got], 0, 255).astype(np.uint8)
    out[0, rr[got], cc[got]] = bgr[:, 2]
    out[1, rr[got], cc[got]] = bgr[:, 1]
    out[2, rr[got], cc[got]] = bgr[:, 0]
    out[3, rr[got], cc[got]] = 255
    if ctx.hidden is not None:
        out[:, ctx.hidden.tile(r0, c0, th, tw)] = 0
    if redo is None:
        return out
    from_base = np.zeros((th, tw), bool)
    from_base[rr[base], cc[base]] = True
    if ctx.base is not None:  # cells of a changed patch without surface: the original pixels
        from_base |= todo & ~valid
    return out, todo & ~from_base, from_base


def _context(chunk: Chunk, resolution: float, cams, surface, raycaster, blending: str = "mosaic",
             max_angle: float = 70.0, max_cameras: int = 8, workers: int | None = None, hide_classes=None,
             patches=None, tile: int = 512, lock=None):
    """Everything a tile needs: ``surface`` gives the grid (its ``origin`` and ``shape``)."""
    ctx = _Context()
    ctx.chunk, ctx.resolution, ctx.blending, ctx.tile, ctx.max_cameras = chunk, resolution, blending, tile, max_cameras
    ctx.cams = list(cams)
    ctx.min_cos = float(np.cos(np.radians(min(max(float(max_angle), 0.0), 90.0))))
    ctx.lock = lock or threading.Lock()  # ray casting, one query at a time (each one is parallel inside)
    ctx.raycaster = raycaster
    ctx.surface = surface
    ctx.origin, ctx.shape = surface.origin, surface.shape
    ctx.inv = np.linalg.inv(chunk.transform)
    ctx.centres = {c.key: c.center for c in ctx.cams}
    ctx.abs_tol = 3.0 * resolution / chunk.scale  # occlusion tolerance in internal units
    up_int = ctx.inv[:3, :3] @ np.array([0.0, 0.0, 1.0])
    ctx.up_int = up_int / np.linalg.norm(up_int)
    ctx.footprints = _Footprints(chunk, ctx.cams)
    ctx.workers = int(workers or max(1, min(8, (os.cpu_count() or 4) - 2)))
    ctx.cache = _PhotoCache(max(12, 3 * ctx.workers))
    ctx.hidden = _ClassGrid(chunk, ctx.origin, resolution, hide_classes) if hide_classes else None
    from .patches import prepare

    ctx.patches = prepare(chunk, patches or [], ctx.cams)
    ctx.redo, ctx.base = None, None
    return ctx


def _on_grid(surface, origin, shape, resolution: float):
    """The same surface on another grid (top-left ``origin``, ``shape``)."""
    surface.origin = (float(origin[0]), float(origin[1]))
    surface.shape = (int(shape[0]), int(shape[1]))
    surface.resolution = resolution
    return surface


def build_orthomosaic(chunk: Chunk, resolution: float | None = None, surface: str = "dem", blending: str = "mosaic",
                      hide_classes=None, progress: Progress | None = None, tile: int = 512,
                      skip_unreadable: bool = False, workers: int | None = None, max_cameras: int = 8,
                      max_angle: float = 70.0, patches=None) -> Path:
    """``hide_classes``: classes of the dense cloud left out of the orthomosaic (transparent
    where they are the visible surface: excavation tools, people, vegetation). Photos that
    cannot be read (e.g. kept online only by Dropbox) stop the step
    (:class:`~.visibility.UnreadablePhotos`) unless ``skip_unreadable``. Every tile takes its
    colours from the ``max_cameras`` photos that see it best (more where they leave cells
    uncovered); ``workers`` tiles are made at once (default: the cores but two). Cells that
    every photo sees from further than ``max_angle`` degrees from the vertical stay
    transparent: grazing views (an orbit of oblique photos around the survey, the edge of a
    DEM interpolated beyond the reconstruction) only smear the photo over the surface.
    ``patches`` (default: the patches of the chunk, see :mod:`.patches`) take their colours
    from the photos assigned to them."""
    from rasterio.transform import from_origin

    from ..core import crs as crs_mod

    progress = progress or Progress()
    progress(0.0, "Checking the photos")
    cams, bad = readable_cameras(chunk, skip_unreadable)
    ensure_level(chunk)
    if resolution is None:
        resolution = estimate_gsd(chunk)
    old = dict(chunk.meta.get("orthomosaic") or {})
    if patches is None:
        patches = old.get("patches") or []
    progress(0.01, "Surface for the orthomosaic")
    raycaster = raycaster_for_chunk(chunk)
    lock = threading.Lock()
    surf = _surface(chunk, resolution, surface, raycaster, lock)
    hidden_classes = sorted({int(c) for c in hide_classes}) if hide_classes else None
    ctx = _context(chunk, resolution, cams, surf, raycaster, blending, max_angle, max_cameras, workers,
                   hidden_classes, patches, tile, lock)
    h, w = ctx.shape
    progress(0.02, f"Orthomosaic {w}x{h} px at {resolution:.4g} m/px from {len(cams)} photos"
             + (f", {len(ctx.patches)} patches" if ctx.patches else ""))
    tiles = [(r, c) for r in range(0, h, tile) for c in range(0, w, tile)]
    profile = {"driver": "GTiff", "height": h, "width": w, "count": 4, "dtype": "uint8",
               "transform": from_origin(ctx.origin[0], ctx.origin[1], resolution, resolution),
               "compress": "deflate", "photometric": "RGB", "BIGTIFF": "IF_SAFER"}
    if h >= 256 and w >= 256:
        profile.update(tiled=True, blockxsize=256, blockysize=256)
    if not crs_mod.is_local(chunk.crs):
        profile["crs"] = crs_mod.to_pyproj(chunk.crs).to_wkt()
    out_path = chunk.data_dir / "orthomosaic.tif"
    tmp = out_path.with_name("orthomosaic.tmp.tif")
    try:
        _write_tiles(ctx, tmp, profile, tiles, ctx.workers, progress)
    except BaseException:
        tmp.unlink(missing_ok=True)  # cancelled or failed: no half-written file left behind
        raise
    tmp.replace(out_path)
    chunk._set_product("orthomosaic", out_path)
    chunk.meta["orthomosaic"] = {"resolution": resolution, "surface": surface, "blending": blending,
                                 "hidden_classes": hidden_classes, "max_angle": float(max_angle),
                                 "max_cameras": int(max_cameras)}
    if patches:
        chunk.meta["orthomosaic"]["patches"] = [dict(p) for p in patches]
        chunk.meta["orthomosaic"]["applied_patches"] = [dict(p) for p in patches]
    if bad:
        chunk.meta["orthomosaic"]["skipped_photos"] = [c.label for c in bad]
    progress(1.0, f"Orthomosaic {w}x{h} px")
    return out_path


def _run_tiles(ctx, tiles, workers: int, progress: Progress, put, start: float = 0.03, span: float = 0.9) -> None:
    """Make the tiles in parallel (at most twice the workers waiting) and hand each one to
    ``put(r0, c0, block)`` in this thread as soon as it is ready."""
    done = 0
    with ThreadPoolExecutor(workers) as pool:
        queue = iter(tiles)
        running = {}
        try:
            while True:
                while len(running) < 2 * workers:
                    nxt = next(queue, None)
                    if nxt is None:
                        break
                    running[pool.submit(_tile, ctx, *nxt)] = nxt
                if not running:
                    break
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for fut in finished:
                    r0, c0 = running.pop(fut)
                    block = fut.result()
                    if block is not None:
                        put(r0, c0, block)
                    done += 1
                progress(start + span * done / len(tiles), f"Orthomosaic tile {done}/{len(tiles)}")
        except BaseException:
            for fut in running:
                fut.cancel()
            raise


def _write_tiles(ctx, tmp: Path, profile: dict, tiles, workers: int, progress: Progress) -> None:
    """Make the tiles in parallel and write each one as soon as it is ready, then the overviews."""
    import rasterio
    from rasterio.enums import ColorInterp, Resampling
    from rasterio.windows import Window

    h, w = ctx.shape
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        _run_tiles(ctx, tiles, workers, progress,
                   lambda r0, c0, block: dst.write(block, window=Window(c0, r0, block.shape[2], block.shape[1])))
        factors = [f for f in (2, 4, 8, 16, 32, 64, 128) if min(h, w) // f >= 64]
        if factors:
            progress(0.95, "Overviews of the orthomosaic")
            dst.build_overviews(factors, Resampling.average)


def render_surface(chunk: Chunk, origin, shape, resolution: float, cams=None, raycaster=None,
                   progress: Progress | None = None, workers: int | None = None, max_cameras: int = 8,
                   max_angle: float = 88.0, tile: int = 512, patches=None) -> np.ndarray:
    """RGBA ``(4, h, w)`` of the model seen from above over a grid (``origin`` = top-left corner,
    world units): the orthophoto engine in memory, used for the texture of a terrain model.
    ``patches`` take their colours from the photos assigned to them."""
    progress = progress or Progress()
    cams = list(chunk.aligned_cameras if cams is None else cams)
    raycaster = raycaster if raycaster is not None else raycaster_for_chunk(chunk)
    if raycaster is None:
        raise RuntimeError("build the model first")
    lock = threading.Lock()
    surface = _on_grid(_MeshSurface(chunk, raycaster, resolution, lock), origin, shape, resolution)
    ctx = _context(chunk, resolution, cams, surface, raycaster, "mosaic", max_angle, max_cameras, workers,
                   None, patches, tile, lock)
    h, w = ctx.shape
    out = np.zeros((4, h, w), np.uint8)

    def put(r0, c0, block):
        out[:, r0:r0 + block.shape[1], c0:c0 + block.shape[2]] = block

    _run_tiles(ctx, [(r, c) for r in range(0, h, tile) for c in range(0, w, tile)], ctx.workers, progress, put,
               start=0.0, span=1.0)
    return out


def render_area(chunk: Chunk, origin, shape, resolution: float, cams=None, surface: str = "dem", raycaster=None,
                max_angle: float = 89.0, patches=None, blending: str = "mosaic", max_cameras: int = 8) -> np.ndarray:
    """RGBA ``(4, h, w)`` of a small grid on the surface of the orthomosaic, in this thread
    (the preview of a patch from one photo)."""
    cams = list(readable_cameras(chunk, True)[0] if cams is None else cams)
    raycaster = raycaster if raycaster is not None else raycaster_for_chunk(chunk)
    lock = threading.Lock()
    surf = _on_grid(_surface(chunk, resolution, surface, raycaster, lock), origin, shape, resolution)
    ctx = _context(chunk, resolution, cams, surf, raycaster, blending, max_angle, max_cameras, 1, None,
                   patches, max(int(shape[0]), int(shape[1]), 1), lock)
    block = _tile(ctx, 0, 0)
    return np.zeros((4, int(shape[0]), int(shape[1])), np.uint8) if block is None else block


__all__ = ["build_orthomosaic", "hidden_cells", "render_area", "render_surface", "NODATA"]
