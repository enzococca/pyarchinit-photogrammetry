"""Orthomosaic generation (GeoTIFF RGBA in the chunk CRS).

Every ground cell is lifted to the surface (the DEM, or the mesh with vertical rays), projected
into the photos, and coloured from the best photo that really sees it (occlusion test against
the mesh when one exists). Blending (``BLENDINGS``):

* ``mosaic`` (default): the details of every cell come from its best photo, as in a true
  orthophoto, while the low frequencies are blended across the seams between photos over a few
  ``blend_sigma`` cells: different exposures leave no visible edge, nothing is blurred;
* ``disabled``: one photo per cell, nothing blended (the seams show where exposures differ);
* ``average``: every photo that sees a cell, weighted by how well it sees it (soft).

With a colour correction (:mod:`.colour`: exposure gains of the photos, vignetting of the lenses)
the photos are corrected as they are read.

The orthomosaic is made tile by tile and every tile is written at once, so its size is limited
by the disk, not by the memory (45,000 x 46,000 px for a tell at 1.35 cm/px): the surface is read
one window at a time, every tile looks only at the few photos that see it best (found from the
ground footprints of the photos), and tiles are processed in parallel - also by the other
computers started as nodes on the project folder (:mod:`.nodes`): a tile depends only on the
photos, the surface and its grid, never on the tiles around it.

Patches (:mod:`.patches`) are polygons whose cells take their colours from photos chosen by
hand (or from every photo but some): every build applies them, and an update redraws only the
cells of the patches that changed.
"""
from __future__ import annotations

import os
import queue
import threading
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import numpy as np

from .. import _native
from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress
from ..io.files import replace_file
from .dem import NODATA, ensure_level, estimate_gsd
from .visibility import project_valid, raycaster_for_chunk, read_photo, readable_cameras, sample


BLENDINGS = ("mosaic", "disabled", "average")
BLEND_SIGMA = 8.0  # cells: width of the transition between two photos (Gaussian sigma) in "mosaic"


def correction_for(chunk: Chunk, enabled: bool | None = None, progress: Progress | None = None):
    """The colour correction to apply (:class:`.colour.Correction`), or None. ``enabled``: None =
    the one measured in the chunk, if any; True = that one, measured now when missing; False = none."""
    from .colour import Correction, estimate

    if enabled is False:
        return None
    if not chunk.meta.get("colour"):
        if not enabled:
            return None
        estimate(chunk, progress=progress)
    corr = Correction(chunk)
    return corr if corr else None


def stored_blending(meta: dict | None) -> tuple[str, float]:
    """Blending and its width an orthomosaic was built with (from its ``chunk.meta`` entry):
    files made before the seamless mosaic (no ``blend_sigma``) were one photo per cell."""
    meta = meta or {}
    blending = meta.get("blending", "mosaic")
    if blending == "mosaic" and "blend_sigma" not in meta:
        return "disabled", 0.0
    return blending, float(meta.get("blend_sigma", BLEND_SIGMA))


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


def _surface_heights(surface, x, y) -> np.ndarray:
    """Heights of the surface of the orthomosaic at world points (NaN where it has none)."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    if isinstance(surface, _MeshSurface):
        start = np.column_stack([x, y, np.full(len(x), surface.top)]) @ surface.inv[:3, :3].T + surface.inv[:3, 3]
        with surface.lock:
            t = surface.raycaster.hit_distance(start, np.repeat(surface.down[None], len(start), 0))
        return np.where(np.isfinite(t), surface.top - t * surface.chunk.scale, np.nan)
    from rasterio.windows import Window

    t = surface.t
    colf = (x - t.c) / t.a - 0.5  # fractional DEM pixel, centres on integers
    rowf = (y - t.f) / t.e - 0.5
    a0 = max(int(np.floor(colf.min())) - 1, 0)
    a1 = min(int(np.ceil(colf.max())) + 2, surface.width)
    b0 = max(int(np.floor(rowf.min())) - 1, 0)
    b1 = min(int(np.ceil(rowf.max())) + 2, surface.height)
    if a1 <= a0 or b1 <= b0:
        return np.full(len(x), np.nan)
    dem = np.asarray(surface._src().read(1, window=Window(a0, b0, a1 - a0, b1 - b0)), np.float64)
    valid = np.isfinite(dem)
    if surface.nodata is not None:
        valid &= dem != surface.nodata
    c, r = colf - a0, rowf - b0
    c0, r0 = np.floor(c).astype(int), np.floor(r).astype(int)
    num = np.zeros(len(x))
    den = np.zeros(len(x))
    for dr in (0, 1):
        for dc in (0, 1):
            rr, cc = r0 + dr, c0 + dc
            w = (1 - np.abs(r - rr)) * (1 - np.abs(c - cc))
            ok = (rr >= 0) & (rr < dem.shape[0]) & (cc >= 0) & (cc < dem.shape[1])
            ok[ok] &= valid[rr[ok], cc[ok]]
            num[ok] += w[ok] * dem[rr[ok], cc[ok]]
            den[ok] += w[ok]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0.25, num / den, np.nan)


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
    """Decoded photos shared by the tile workers (least recently used ones dropped), with the
    colour correction applied (:class:`.colour.Correction`) when there is one."""

    def __init__(self, max_items: int, correction=None, loader=None):
        self.max_items = max_items
        self.correction = correction or None
        self.loader = loader  # the image of a camera (any dtype and channels); the photo itself by default
        self._items: OrderedDict[int, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, cam) -> np.ndarray:
        with self._lock:
            img = self._items.get(cam.key)
            if img is not None:
                self._items.move_to_end(cam.key)
                return img
        img = self.loader(cam) if self.loader is not None else read_photo(cam)  # decoded outside the lock
        if self.correction is not None:
            img = self.correction.correct(cam, img)
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
    """Pixels and score (frontal, central, fine) of points in a photo; -inf where not seen, where
    its mask hides them, or seen from further than the vertical than the angle whose cosine is
    ``min_cos``."""
    from .masks import hidden_by_mask, mask_path

    sensor = chunk.sensor(cam)
    if _native.supports(sensor.model):
        px, score = _native.kernel("ortho_scores")(np.ascontiguousarray(pts, np.float64), cam.cam_from_world,
                                                   sensor.model, [float(v) for v in sensor.params],
                                                   float(sensor.width), float(sensor.height),
                                                   [float(v) for v in centres[cam.key]], [float(v) for v in up_int],
                                                   float(min_cos))
        if mask_path(chunk, cam) is not None:
            score[hidden_by_mask(chunk, cam, px)] = -np.inf
        return px, score
    px, score = _scores_numpy(chunk, cam, pts, centres, up_int, min_cos)
    if mask_path(chunk, cam) is not None:
        score[hidden_by_mask(chunk, cam, px)] = -np.inf
    return px, score


def _scores_numpy(chunk: Chunk, cam, pts, centres, up_int, min_cos: float = 0.0):
    sensor = chunk.sensor(cam)
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
    those that reach cells the best ones do not. With a float source a photo reaches the cells
    where its image has a value, read only for the best photos and for those that could reach
    a cell still uncovered."""
    float_source = getattr(ctx, "float_output", False)
    ranked = []
    for cam in near:
        px, s = _scores(ctx.chunk, cam, probe, ctx.centres, ctx.up_int, ctx.min_cos)
        seen = np.isfinite(s)
        if seen.any():
            ranked.append((float(np.where(seen, s, 0).sum()), cam, seen, px))
    ranked.sort(key=lambda r: -r[0])

    def reached(cam, seen, px):
        if not float_source:
            return seen
        idx = np.nonzero(seen)[0]
        out = seen.copy()
        out[idx[~np.isfinite(_sample_photo(ctx, cam, px[idx].astype(np.float32))).all(axis=1)]] = False
        return out

    tcams = [r[1] for r in ranked[:ctx.max_cameras]]
    covered = np.zeros(len(probe), bool)
    for _score, cam, seen, px in ranked[:ctx.max_cameras]:
        covered |= reached(cam, seen, px)
    for _score, cam, seen, px in ranked[ctx.max_cameras:]:  # the cells the best photos do not reach
        if (seen & ~covered).any():
            seen = reached(cam, seen, px)
            if (seen & ~covered).any():
                tcams.append(cam)
                covered |= seen
    return tcams


CANDIDATES = 4  # photos tried for a cell, best first, before its best one is taken anyway (occlusions)
GHOST_TOLERANCE = 30.0  # colour levels: a photo further than this from the others shows something that moved
HOLE_CELLS = 100  # holes of at most this many cells are filled from their edges (fill_holes)


def _colour(ctx, pts, near, want_choice: bool = False, colours: bool = True):
    """Colours ``(n, 3)`` (BGR) of surface points ``pts`` (internal frame) from the photos
    ``near``, and which points got one; with ``want_choice`` also the key of the photo of every
    point (-1 for none). ``colours=False`` only chooses the photos (none is read)."""
    return _paint(ctx, pts, _rank(ctx, _probe(pts), near) if ctx.blending == "average" else near, want_choice,
                  colours)


def _candidates(ctx, pts, cams, k: int = CANDIDATES):
    """The ``k`` best of ``cams`` for every point, each point on its own (so its photo never
    depends on the tile around it): indices into ``cams`` (k, n; -1 for none), scores (float32,
    best first, an equal score behind the earlier photo) and pixels (k, n, 2, float32)."""
    from .masks import hidden_by_mask, mask_path

    n = len(pts)
    sensors = [ctx.chunk.sensor(c) for c in cams]
    native = _native.kernel("ortho_candidates")
    if native is not None and cams and all(_native.supports(s.model) for s in sensors):
        masked = [j for j, c in enumerate(cams) if mask_path(ctx.chunk, c) is not None]
        extra = 2 if masked else 0  # some candidates may be masked: a few more to fall back on
        idx, sc, px = native(np.ascontiguousarray(pts, np.float64), np.stack([c.cam_from_world[:3] for c in cams]),
                             [s.model for s in sensors], [[float(v) for v in s.params] for s in sensors],
                             [(float(s.width), float(s.height)) for s in sensors],
                             np.array([ctx.centres[c.key] for c in cams], np.float64), [float(v) for v in ctx.up_int],
                             float(ctx.min_cos), k + extra)
        if masked:
            for j in masked:
                r, c = np.nonzero(idx == j)
                if len(r):
                    hide = hidden_by_mask(ctx.chunk, cams[j], px[r, c].astype(np.float64))
                    idx[r[hide], c[hide]], sc[r[hide], c[hide]] = -1, -np.inf
            order = np.argsort(-sc, axis=0, kind="stable")[:k]
            idx, sc = np.take_along_axis(idx, order, 0), np.take_along_axis(sc, order, 0)
            px = np.take_along_axis(px, order[..., None], 0)
        return idx, sc, px
    idx = np.full((k, n), -1, np.int32)
    sc = np.full((k, n), -np.inf, np.float32)
    px = np.full((k, n, 2), np.nan, np.float32)
    for j, cam in enumerate(cams):
        p, s = _scores(ctx.chunk, cam, pts, ctx.centres, ctx.up_int, ctx.min_cos)
        s = s.astype(np.float32)
        cols = np.nonzero(s > sc[k - 1])[0]  # enters the best k
        if not len(cols):
            continue
        all_s = np.vstack([sc[:, cols], s[None, cols]])
        all_i = np.vstack([idx[:, cols], np.full((1, len(cols)), j, np.int32)])
        all_p = np.concatenate([px[:, cols], p[None, cols].astype(np.float32)], axis=0)
        order = np.argsort(-all_s, axis=0, kind="stable")[:k]  # the new photo last: behind equal scores
        sc[:, cols] = np.take_along_axis(all_s, order, 0)
        idx[:, cols] = np.take_along_axis(all_i, order, 0)
        px[:, cols] = np.take_along_axis(all_p, order[..., None], 0)
    return idx, sc, px


VALUE_SEARCH = 4  # with a float source, photos beyond the best ones looked at first for a value
VALUE_BLOCK = 65536  # cells whose photo is looked for among more photos at once (memory: cells x photos)


def _paint_values(ctx, pts, tcams, occlusion, visible):
    """The photo of every cell with a float source, whose image has no value over parts of its
    photo (a thermal frame covers the middle of the visible photo only, a registered band leaves
    its borders empty): the best candidate that sees the cell and has a value there. A cell
    whose ``CANDIDATES`` best photos have none there looks among ``CANDIDATES + VALUE_SEARCH``,
    then twice as many, up to all the photos: better photos without a value can be many (on a
    flight at two heights the low visible photos frame the cell at their edge, outside their
    narrower thermal frames). A photo is read only where a cell needs it. A cell hidden in every
    candidate that has a value takes the best of those anyway, as :func:`_paint` does. Returns
    the values (n, 3), the photo of each cell (index into ``tcams``, -1 for none) and the cells
    painted from a photo that does not see them."""
    n = len(pts)
    colors = np.zeros((n, 3), np.float64)
    chosen = np.full(n, -1)
    hidden = np.full(n, -1)  # the best candidate with a value that does not see the cell
    hidden_val = np.zeros((n, 3), np.float64)
    todo = np.arange(n)  # cells without a candidate that has a value yet
    searched, width = 0, min(len(tcams), CANDIDATES)
    while len(todo) and searched < width:
        block = VALUE_BLOCK if searched else max(len(todo), 1)
        further = []
        for b in range(0, len(todo), block):
            part = todo[b:b + block]
            cand_i, _cand_s, cand_px = _candidates(ctx, pts[part], tcams, width)
            pending = np.arange(len(part))
            for rank in range(searched, width):
                cand = cand_i[rank, pending]
                has = cand >= 0  # no candidate at this rank: none beyond it either
                pending, cand = pending[has], cand[has]
                if not len(pending):
                    break
                still = []
                for k in np.unique(cand):
                    sel = pending[cand == k]
                    vals = _sample_photo(ctx, tcams[k], cand_px[rank, sel])
                    ok = np.isfinite(vals).all(axis=1)
                    still.append(sel[~ok])
                    sel, vals = sel[ok], vals[ok]
                    cells = part[sel]
                    if occlusion and len(cells):
                        vis = visible(tcams[k], cells)
                        unseen = cells[~vis]
                        first = hidden[unseen] < 0
                        hidden[unseen[first]], hidden_val[unseen[first]] = k, vals[~vis][first]
                        still.append(sel[~vis])
                        sel, vals, cells = sel[vis], vals[vis], cells[vis]
                    colors[cells], chosen[cells] = vals, k
                pending = np.concatenate(still) if still else pending[:0]
            if len(pending) and width < len(tcams):  # every candidate without a value, more photos beyond
                cells = part[pending[cand_i[width - 1, pending] >= 0]]
                further.append(cells[hidden[cells] < 0])
        todo = np.concatenate(further) if further else todo[:0]
        searched, width = width, min(len(tcams), max(width + VALUE_SEARCH, 2 * width))
    fallback = (chosen < 0) & (hidden >= 0)
    chosen[fallback], colors[fallback] = hidden[fallback], hidden_val[fallback]
    return colors, chosen, fallback


def _paint(ctx, pts, tcams, want_choice: bool = False, colours: bool = True, info: dict | None = None):
    """Like :func:`_colour`, from the photos ``tcams`` already chosen for the tile: every point
    takes its colour from the best of them that sees it (whatever other points are painted
    with it, so a part of a tile comes out as in the whole tile). ``info["fallback"]``: the
    points that no candidate photo sees, painted from the best one anyway."""
    chunk = ctx.chunk
    n = len(pts)
    colors = np.zeros((n, 3), np.float64)
    got = np.zeros(n, bool)
    keys = np.full(n, -1)
    if not tcams or not n:
        return (colors, got, keys) if want_choice else (colors, got)
    chosen = np.full(n, -1)

    occlusion = ctx.raycaster is not None and getattr(ctx, "occlusion", True)

    def visible(cam, sel):
        with ctx.lock:
            return ctx.raycaster.visible_from(ctx.centres[cam.key], pts[sel], abs_tol=ctx.abs_tol)

    if ctx.blending == "average":
        scores = np.full((len(tcams), n), -np.inf, np.float32)
        pix = np.full((len(tcams), n, 2), np.nan, np.float32)
        for k, cam in enumerate(tcams):
            px, s = _scores(chunk, cam, pts, ctx.centres, ctx.up_int, ctx.min_cos)
            scores[k] = s
            ok = np.isfinite(s)
            pix[k, ok] = px[ok]
        order = np.argsort(-scores, axis=0)
        weight = np.zeros(n, np.float64)
        for k, cam in enumerate(tcams):
            ok = np.isfinite(scores[k])
            if occlusion and ok.any():
                idx = np.nonzero(ok)[0]
                ok[idx[~visible(cam, idx)]] = False
            if ok.any():
                wgt = scores[k, ok].astype(np.float64)
                if colours:
                    vals = _sample_photo(ctx, cam, pix[k, ok])
                    fin = np.isfinite(vals).all(axis=1)  # a float source without a value there: not this photo
                    idx_ok = np.nonzero(ok)[0]
                    colors[idx_ok[fin]] += vals[fin] * wgt[fin, None]
                    weight[idx_ok[fin]] += wgt[fin]
                else:
                    weight[ok] += wgt
        got = weight > 0
        colors[got] /= weight[got, None]
        chosen[got] = order[0, got]
    elif getattr(ctx, "ghosting", False):
        return _paint_ghost_free(ctx, pts, tcams, colors, chosen, keys, want_choice, colours, info, occlusion, visible)
    elif getattr(ctx, "float_output", False):  # values read as the photos are chosen: some have none at a cell
        colors, chosen, fallback = _paint_values(ctx, pts, tcams, occlusion, visible)
        if info is not None:
            info["fallback"] = fallback
        got = chosen >= 0
    else:
        cand_i, _cand_s, cand_px = _candidates(ctx, pts, tcams)
        rank_of = np.full(n, -1)
        pending = np.arange(n)
        for rank in range(min(cand_i.shape[0], len(tcams))):
            if len(pending) == 0:
                break
            cand = cand_i[rank, pending]
            ok = cand >= 0
            pending, cand = pending[ok], cand[ok]
            still = []
            for k in np.unique(cand):
                sel = pending[cand == k]
                if occlusion:
                    vis = visible(tcams[k], sel)
                    still.append(sel[~vis])
                    sel = sel[vis]
                rank_of[sel] = rank
            pending = np.concatenate(still) if still else np.zeros(0, int)
        # cells hidden in every candidate photo (steep faces): take the best photo anyway
        fallback = (rank_of < 0) & (cand_i[0] >= 0)
        rank_of[fallback] = 0
        if info is not None:
            info["fallback"] = fallback
        got = rank_of >= 0
        cells = np.nonzero(got)[0]
        chosen[cells] = cand_i[rank_of[cells], cells]
        if colours and len(cells):
            px_of = cand_px[rank_of[cells], cells]
            for k in np.unique(chosen[cells]):
                mine = chosen[cells] == k
                colors[cells[mine]] = _sample_photo(ctx, tcams[k], px_of[mine])
    if want_choice:
        keys[got] = np.array([tcams[k].key for k in chosen[got]], dtype=keys.dtype) if got.any() else keys[got]
        return colors, got, keys
    return colors, got


def _paint_ghost_free(ctx, pts, tcams, colors, chosen, keys, want_choice, colours, info, occlusion, visible):
    """:func:`_paint` with the ghosting filter: every candidate photo that sees a cell gives its
    colour; where three or more see it, the best one that agrees with their median colour (within
    ``GHOST_TOLERANCE``) is taken, so a person or a vehicle seen by a few photos only is left out."""
    n = len(pts)
    cand_i, _cand_s, cand_px = _candidates(ctx, pts, tcams)
    k_n = cand_i.shape[0]
    vis = np.zeros((k_n, n), bool)
    col = np.full((k_n, n, 3), np.nan)
    for rank in range(k_n):
        cand = cand_i[rank]
        for k in np.unique(cand[cand >= 0]):
            sel = np.nonzero(cand == k)[0]
            if occlusion:
                sel = sel[visible(tcams[k], sel)]
            vis[rank, sel] = True
            col[rank, sel] = _sample_photo(ctx, tcams[k], cand_px[rank, sel])
    seen_by = vis.sum(axis=0)
    some = seen_by > 0  # the median only where a photo sees the cell (no all-NaN slices)
    med = np.full((n, 3), np.nan)
    if some.any():
        med[some] = np.nanmedian(np.where(vis[:, some, None], col[:, some], np.nan), axis=0)
    with np.errstate(invalid="ignore"):
        agree = vis & (np.abs(col - med[None]).max(axis=2) <= GHOST_TOLERANCE)
    first_vis = np.where(vis.any(axis=0), vis.argmax(axis=0), -1)
    first_agree = np.where(agree.any(axis=0), agree.argmax(axis=0), -1)
    rank_of = np.where((seen_by >= 3) & (first_agree >= 0), first_agree, first_vis)
    fallback = (rank_of < 0) & (cand_i[0] >= 0)
    rank_of[fallback] = 0
    if info is not None:
        info["fallback"] = fallback
    got = rank_of >= 0
    cells = np.nonzero(got)[0]
    chosen[cells] = cand_i[rank_of[cells], cells]
    if colours and len(cells):
        picked = col[rank_of[cells], cells]
        need = np.isnan(picked).any(axis=1)  # the fallback: a photo that does not see the cell
        picked[need] = 0.0
        colors[cells] = picked
        if need.any():
            fb = cells[need]
            for k in np.unique(chosen[fb]):
                mine = fb[chosen[fb] == k]
                colors[mine] = _sample_photo(ctx, tcams[k], cand_px[0, mine])
    if want_choice:
        keys[got] = np.array([tcams[k].key for k in chosen[got]], dtype=keys.dtype) if got.any() else keys[got]
        return colors, got, keys
    return colors, got


def _fill_holes(rgb: np.ndarray, painted: np.ndarray, surface: np.ndarray, group: np.ndarray) -> np.ndarray:
    """Cells of the surface no photo painted, in holes of at most ``HOLE_CELLS`` cells, filled from
    the cells around them of the same group (a patch, or the rest): ``rgb`` (h, w, 3) float, the
    cells filled set in ``painted``. Larger holes stay empty."""
    import cv2

    holes = surface & ~painted
    if not holes.any():
        return np.zeros_like(painted)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(holes.astype(np.uint8), connectivity=4)
    small = np.zeros(count, bool)
    small[1:] = stats[1:, cv2.CC_STAT_AREA] <= HOLE_CELLS
    small &= ~np.isin(np.arange(count), np.unique(labels[[0, -1], :]).tolist() + np.unique(labels[:, [0, -1]]).tolist())
    fill = small[labels] & holes  # holes touching the edge of the window may continue outside it
    filled = np.zeros_like(painted)
    img = np.clip(rgb, 0, 255).astype(np.uint8)
    for g in np.unique(group[fill]):
        target = fill & (group == g)
        unknown = target | (group != g) | ~painted  # only the painted cells of the same group are known
        out = cv2.inpaint(img, unknown.astype(np.uint8), 3, cv2.INPAINT_TELEA)
        rgb[target] = out[target]
        filled |= target
    return filled


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


def _ground_patches(ctx, box, r0: int, c0: int, zz: np.ndarray) -> None:
    """Inside a patch on the ground around it, the heights of that ground (in place); a
    later patch covers an earlier one."""
    from shapely import contains_xy

    patches = [p for p in ctx.patches if p.ground is not None and p.bounds[0] <= box[2] and p.bounds[2] >= box[0]
               and p.bounds[1] <= box[3] and p.bounds[3] >= box[1]]
    if not patches:
        return
    th, tw = zz.shape
    rows, cols = np.mgrid[0:th, 0:tw]
    X = ctx.origin[0] + (c0 + cols.ravel() + 0.5) * ctx.resolution
    Y = ctx.origin[1] - (r0 + rows.ravel() + 0.5) * ctx.resolution
    flat = zz.reshape(-1)
    for patch in patches:
        inside = contains_xy(patch.geom, X, Y)
        if inside.any():
            flat[inside] = patch.ground_heights(X[inside], Y[inside])


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
    the pixels of the unpatched orthomosaic of another program (``ctx.base``). With the
    seamless mosaic the cells within ``ctx.margin`` around the tile are made too (not
    written): the blending of the cells near its edge looks at them."""
    res = ctx.resolution
    th, tw = min(ctx.tile, ctx.shape[0] - r0), min(ctx.tile, ctx.shape[1] - c0)
    box = (ctx.origin[0] + c0 * res, ctx.origin[1] - (r0 + th) * res,
           ctx.origin[0] + (c0 + tw) * res, ctx.origin[1] - r0 * res)
    m = ctx.margin
    er0, ec0, eh, ew = r0 - m, c0 - m, th + 2 * m, tw + 2 * m
    ebox = (box[0] - m * res, box[1] - m * res, box[2] + m * res, box[3] + m * res)
    core = (slice(m, m + th), slice(m, m + tw))
    redo = getattr(ctx, "redo", None)
    zz = ctx.surface.tile(er0, ec0, eh, ew)
    _ground_patches(ctx, ebox, er0, ec0, zz)
    valid = np.isfinite(zz)
    todo = None
    if redo is not None:  # only the cells of the patches that changed
        rows, cols = np.mgrid[0:eh, 0:ew]
        todo = _in_any(redo, ebox, ctx.origin[0] + (ec0 + cols.ravel() + 0.5) * res,
                       ctx.origin[1] - (er0 + rows.ravel() + 0.5) * res).reshape(eh, ew)
        if not todo[core].any():
            return None
        if not (valid & todo)[core].any():
            t = todo[core]
            from_base = t if ctx.base is not None else np.zeros_like(t)
            return np.zeros((4, th, tw), np.uint8), t & ~from_base, from_base
    if not valid[core].any():
        return None
    rr, cc = np.nonzero(valid)
    X = ctx.origin[0] + (ec0 + cc + 0.5) * res
    Y = ctx.origin[1] - (er0 + rr + 0.5) * res
    frame = getattr(ctx, "frame", None)  # an elevation: cells on a plane or a cylinder (.elevation)
    world = np.column_stack([X, Y, zz[rr, cc]]) if frame is None else frame.point(X, Y, zz[rr, cc])
    pts = world @ ctx.inv[:3, :3].T + ctx.inv[:3, 3]
    in_core = (rr >= m) & (rr < m + th) & (cc >= m) & (cc < m + tw)
    if frame is not None:  # the photos look at the surface from its viewer side, not from above
        import copy

        u_mid, v_mid = X[in_core].mean(), Y[in_core].mean()
        view = ctx.inv[:3, :3] @ frame.view(np.array([u_mid]), np.array([v_mid]))[0]
        ctx = copy.copy(ctx)
        ctx.up_int = view / np.linalg.norm(view)
        near = list(ctx.cams)
    else:
        # the photos of this tile: those whose footprint meets it, ranked on a sample of all its
        # cells - with or without patches, and when only some cells are made again
        near = [ctx.cams[i] for i in ctx.footprints.candidates(ebox, float(np.median(world[in_core, 2])))]
    probe = _probe(pts[in_core])
    average = ctx.blending == "average"
    tcams = _rank(ctx, probe, near) if average else near  # mosaic: every cell picks among all its photos
    blend = ctx.blend_sigma > 0
    if todo is not None and m == 0:  # from here on, the cells to make only (the others matter around a seam)
        keep = todo[rr, cc]
        rr, cc, X, Y, pts, in_core = rr[keep], cc[keep], X[keep], Y[keep], pts[keep], in_core[keep]
    n = len(pts)
    owner = _patch_owner(ctx, ebox, X, Y) if ctx.patches else np.full(n, -1)
    group = owner.copy()  # blending: cells of a patch painted by its photos, the others with the rest
    colors = np.zeros((n, 3), np.float64)
    got = np.zeros(n, bool)
    keys = np.full(n, -1)
    seen = np.zeros(n, bool)  # painted from a photo that sees the point (not the fallback)
    base = np.zeros(n, bool)  # cells left to the unpatched orthomosaic of another program
    cams_by_key = {c.key: c for c in tcams}

    def paint(idx, cams):
        info = {}
        c, g, kk = _paint(ctx, pts[idx], cams, want_choice=True, info=info)
        colors[idx[g]], got[idx[g]], keys[idx[g]] = c[g], True, kk[g]
        fb = info.get("fallback", np.zeros(len(idx), bool))
        seen[idx[g & ~fb]] = True
        cams_by_key.update((c.key, c) for c in cams)
        return g

    for i in np.unique(owner):
        idx = np.nonzero(owner == i)[0]
        if i < 0:
            if redo is not None and ctx.base is not None:
                base[idx] = True  # a patch taken away: the original pixels again
                continue
            cams = tcams
        else:
            patch = ctx.patches[i]
            cams = (patch.cams if patch.mode == "assign"
                    else _rank(ctx, probe, patch.photos(near)) if average else patch.photos(near))
        g = paint(idx, cams)
        miss = idx[~g]
        if i >= 0 and len(miss) and ctx.patches[i].mode == "assign":  # not seen by its photos: as without it
            if redo is not None and ctx.base is not None:
                base[miss] = True
            else:
                paint(miss, tcams)
                group[miss] = -1
    if blend and got.any():
        colors = _blend(ctx, pts, rr, cc, (eh, ew), colors, got, keys, seen, group, tcams, cams_by_key)
    if getattr(ctx, "fill_holes", False) and got.any() and not got.all():
        rgb = np.zeros((eh, ew, 3), np.float64)
        rgb[rr, cc] = colors
        painted = np.zeros((eh, ew), bool)
        painted[rr, cc] = got
        groups = np.full((eh, ew), -2)
        groups[rr, cc] = group
        surface = np.zeros((eh, ew), bool)
        surface[rr, cc] = ~base  # cells of another program's orthomosaic are not ours to fill
        filled = _fill_holes(rgb, painted, surface, groups)
        if filled.any():
            colors = rgb[rr, cc]
            got = got | filled[rr, cc]
    sel = got & in_core
    r_out, c_out = rr[sel] - m, cc[sel] - m
    if getattr(ctx, "float_output", False):  # the channels as they are (reflectance bands), NaN where nothing
        out = np.full((4, th, tw), np.nan, np.float32)
        vals = colors[sel].astype(np.float32)
        out[0, r_out, c_out] = vals[:, 0]
        out[1, r_out, c_out] = vals[:, 1]
        out[2, r_out, c_out] = vals[:, 2]
        out[3, r_out, c_out] = 1.0
        if ctx.hidden is not None:
            out[:, ctx.hidden.tile(r0, c0, th, tw)] = np.nan
    else:
        out = np.zeros((4, th, tw), np.uint8)
        bgr = np.clip(colors[sel], 0, 255).astype(np.uint8)
        out[0, r_out, c_out] = bgr[:, 2]
        out[1, r_out, c_out] = bgr[:, 1]
        out[2, r_out, c_out] = bgr[:, 0]
        out[3, r_out, c_out] = 255
        if ctx.hidden is not None:
            out[:, ctx.hidden.tile(r0, c0, th, tw)] = 0
    if redo is None:
        return out
    todo_core, valid_core = todo[core], valid[core]
    from_base = np.zeros((th, tw), bool)
    b = base & in_core
    from_base[rr[b] - m, cc[b] - m] = True
    from_base &= todo_core
    if ctx.base is not None:  # cells of a changed patch without surface: the original pixels
        from_base |= todo_core & ~valid_core
    return out, todo_core & ~from_base, from_base


def _sample_photo(ctx, cam, px: np.ndarray) -> np.ndarray:
    """Colours of the image of ``cam`` (the photo, or the image a ``source`` gives) at COLMAP
    pixels ``px`` of its sensor: an image of another size than the sensor (a thermal band on a
    reduced grid of the visible photo) is sampled at the same points, its pixels scaled."""
    img = ctx.cache.get(cam)
    sensor = ctx.chunk.sensor(cam)
    h, w = img.shape[:2]
    if (w, h) != (sensor.width, sensor.height) and len(px):
        px = np.asarray(px, np.float32) * np.array([w / sensor.width, h / sensor.height], np.float32)
    return sample(img, px)


def _photo_colours(ctx, cam, pts):
    """Colours (BGR) of ``pts`` in one photo and where the photo sees them, decided as
    :func:`_paint` decides: the same colour whichever photo a cell takes."""
    px, s = _scores(ctx.chunk, cam, pts, ctx.centres, ctx.up_int, ctx.min_cos)
    ok = np.isfinite(s)
    idx = np.nonzero(ok)[0]
    if len(idx) and ctx.raycaster is not None and getattr(ctx, "occlusion", True):
        with ctx.lock:
            vis = ctx.raycaster.visible_from(ctx.centres[cam.key], pts[idx], abs_tol=ctx.abs_tol)
        ok[idx[~vis]] = False
    col = np.zeros((len(pts), 3), np.float64)
    if ok.any():
        col[ok] = _sample_photo(ctx, cam, px[ok].astype(np.float32))
        bad = ~np.isfinite(col).all(axis=1)  # a float source without a value there (a band's border)
        if bad.any():
            ok &= ~bad
            col[bad] = 0.0
    return col, ok


def _blend(ctx, pts, rr, cc, shape, colors, got, keys, seen, group, tcams, cams_by_key) -> np.ndarray:
    """Seamless colours of the cells (``rr``, ``cc`` on a grid of ``shape``): two bands. The
    low band of every photo (its colours smoothed over ``ctx.blend_sigma`` cells where it sees
    the surface) is averaged with weights that pass from one photo to the next across a seam;
    the details stay those of the photo of the cell. Away from seams nothing changes.

    ``group``: -1 for the cells outside the patches (and those of a patch its photos do not see),
    else the patch. The cells of group -1 are weighted by the photos they would take without any
    patch, those of a patch only by the cells of the same patch: a patch never changes the
    pixels around it, and taking it away gives them back exactly."""
    import cv2

    sigma = float(ctx.blend_sigma)
    ksize = 2 * int(np.ceil(3 * sigma)) + 1
    h, w = shape
    flat = rr * w + cc
    n = len(flat)

    def grid(values):
        g = np.zeros((h * w,) + values.shape[1:], np.float32)
        g[flat] = values
        return g.reshape((h, w) + values.shape[1:])

    def smooth(img):
        return cv2.GaussianBlur(img, (ksize, ksize), sigma, borderType=cv2.BORDER_CONSTANT)

    base_keys = keys.copy()  # the photo of every cell as if there were no patches
    patched = (group >= 0) & got
    if patched.any() and (group < 0).any():
        idx = np.nonzero(patched)[0]
        _c, g, kk = _paint(ctx, pts[idx], tcams, want_choice=True, colours=False)
        base_keys[idx] = np.where(g, kk, -1)
    out = colors.copy()
    sampled: dict[int, tuple] = {}  # photo -> colours, sees, sampled yet (over the cells, filled as needed)

    def photo(key, cells):
        """Colours of photo ``key`` at ``cells`` and where it sees them: those already painted
        from it (when it sees them), the others sampled."""
        col = np.zeros((len(cells), 3), np.float64)
        vis = np.zeros(len(cells), bool)
        mine = (keys[cells] == key) & seen[cells]
        col[mine], vis[mine] = colors[cells[mine]], True
        rest = cells[~mine]
        if len(rest):
            c, ok = _photo_colours(ctx, cams_by_key[key], pts[rest])
            col[~mine], vis[~mine] = c, ok
        return col, vis

    for g_id in np.unique(group):
        members = group == g_id
        target = members & got
        if not target.any():
            continue
        field = base_keys if g_id < 0 else np.where(members & got, keys, -1)
        cams = [int(k) for k in np.unique(field[field >= 0])]
        if len(cams) < 2:
            continue
        num = np.zeros((n, 3), np.float64)
        den = np.zeros(n, np.float64)
        other = np.zeros(n, np.float64)  # weight of the photos that are not the cell's own
        low_own = np.zeros((n, 3), np.float64)
        has_own = np.zeros(n, bool)
        for key in cams:
            weight = smooth(grid((field == key).astype(np.float32))).reshape(-1)[flat]
            need = target & (weight > 1e-3)
            if not need.any():
                continue
            # the photo's colours wherever they enter its low band at those cells
            reach = cv2.dilate(grid(need.astype(np.float32)), np.ones((ksize, ksize), np.uint8)).reshape(-1)[flat] > 0
            cells = np.nonzero(reach)[0]
            if key not in sampled:
                sampled[key] = (np.zeros((n, 3), np.float64), np.zeros(n, bool), np.zeros(n, bool))
            col, vis, known = sampled[key]
            fresh = cells[~known[cells]]
            if len(fresh):
                c, v = photo(key, fresh)
                col[fresh], vis[fresh] = c, v
                known[fresh] = True
            v_s = smooth(grid(vis.astype(np.float32))).reshape(-1)[flat]
            c_s = smooth(grid((col * vis[:, None]).astype(np.float32))).reshape(-1, 3)[flat]
            good = need & vis & (v_s > 1e-3)
            low = c_s[good] / v_s[good, None]
            num[good] += weight[good, None] * low
            den[good] += weight[good]
            own = keys[good] == key
            other[good] += np.where(own, 0.0, weight[good])
            gi = np.nonzero(good)[0]
            low_own[gi[own]] = low[own]
            has_own[gi[own]] = True
        upd = target & has_own & (den > 1e-6) & (other > 1e-3 * den)  # another photo takes part
        out[upd] = num[upd] / den[upd, None] + colors[upd] - low_own[upd]
    return out


def _context(chunk: Chunk, resolution: float, cams, surface, raycaster, blending: str = "mosaic",
             max_angle: float = 70.0, max_cameras: int = 8, workers: int | None = None, hide_classes=None,
             patches=None, tile: int = 512, lock=None, blend_sigma: float = BLEND_SIGMA, correction=None,
             ghosting: bool = False, fill_holes: bool = False, source=None, float_output: bool = False):
    """Everything a tile needs: ``surface`` gives the grid (its ``origin`` and ``shape``). ``source``:
    the image of a camera instead of its photo (three channels of any dtype, e.g. reflectance
    bands); ``float_output``: tiles of float32 values as they are, NaN where nothing."""
    if blending not in BLENDINGS:
        raise ValueError(f"blending: one of {', '.join(BLENDINGS)}")
    ctx = _Context()
    ctx.chunk, ctx.resolution, ctx.blending, ctx.tile, ctx.max_cameras = chunk, resolution, blending, tile, max_cameras
    ctx.blend_sigma = float(blend_sigma) if blending == "mosaic" else 0.0
    ctx.margin = int(np.ceil(3 * ctx.blend_sigma)) if ctx.blend_sigma > 0 else 0
    ctx.ghosting, ctx.fill_holes = bool(ghosting) and blending != "average", bool(fill_holes)
    if ctx.fill_holes:  # a hole is filled from the cells around it: they must be in the window
        ctx.margin = max(ctx.margin, 8)
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
    ctx.cache = _PhotoCache(max(12, 3 * ctx.workers), correction, loader=source)
    ctx.float_output = bool(float_output)
    ctx.hidden = _ClassGrid(chunk, ctx.origin, resolution, hide_classes) if hide_classes else None
    from .patches import prepare

    ctx.patches = prepare(chunk, patches or [], ctx.cams)
    for patch in ctx.patches:
        if patch.surface == "ground":
            patch.ground_from(lambda x, y: _surface_heights(surface, x, y), resolution)
    ctx.redo, ctx.base = None, None
    ctx.occlusion = True  # False: every photo sees every cell it frames (a preview from one photo)
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
                      max_angle: float = 70.0, patches=None, blend_sigma: float = BLEND_SIGMA,
                      colour_correction: bool | None = None, ghosting: bool = False,
                      fill_holes: bool = False, source=None, out_path: Path | None = None) -> Path:
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
    from the photos assigned to them. ``blending``: see :data:`BLENDINGS`; ``blend_sigma``: the
    width of the seams of the mosaic (cells). ``colour_correction``: gains of the photos and
    vignetting (:mod:`.colour`): None = the one measured in the chunk if any, True = measured now
    when missing, False = none. ``ghosting``: where three or more photos see a cell, the best one
    that agrees with the others (people and vehicles seen by a few photos left out).
    ``fill_holes``: holes of at most ``HOLE_CELLS`` cells no photo sees filled from their edges."""
    from rasterio.transform import from_origin

    from ..core import crs as crs_mod

    progress = as_progress(progress)
    progress(0.0, "Checking the photos")
    cams, bad = readable_cameras(chunk, skip_unreadable)
    ensure_level(chunk)
    if resolution is None:
        resolution = estimate_gsd(chunk)
    old = dict(chunk.meta.get("orthomosaic") or {})
    if patches is None:
        patches = old.get("patches") or []
    if source is not None:  # another image of every camera (reflectance bands): its values as they are
        correction, ghosting, fill_holes = None, False, False
    else:
        correction = correction_for(chunk, colour_correction, progress.sub(0.005, 0.005))
    progress(0.01, "Surface for the orthomosaic")
    raycaster = raycaster_for_chunk(chunk)
    lock = threading.Lock()
    surf = _surface(chunk, resolution, surface, raycaster, lock)
    hidden_classes = sorted({int(c) for c in hide_classes}) if hide_classes else None
    ctx = _context(chunk, resolution, cams, surf, raycaster, blending, max_angle, max_cameras, workers,
                   hidden_classes, patches, tile, lock, blend_sigma, correction, ghosting, fill_holes,
                   source=source, float_output=source is not None)
    h, w = ctx.shape
    progress(0.02, f"Orthomosaic {w}x{h} px at {resolution:.4g} m/px from {len(cams)} photos"
             + (f", {len(ctx.patches)} patches" if ctx.patches else ""))
    tiles = [(r, c) for r in range(0, h, tile) for c in range(0, w, tile)]
    files_dir = _nodes_folder(chunk, len(tiles)) if source is None else None
    shared = None
    if files_dir is not None:  # what a node needs besides the chunk to make the same tiles
        shared = (files_dir, {"surface": surface, "origin": [float(v) for v in ctx.origin],
                              "shape": [int(h), int(w)], "resolution": float(resolution), "tile": int(tile),
                              "blending": blending, "blend_sigma": float(blend_sigma), "max_angle": float(max_angle),
                              "max_cameras": int(max_cameras), "hide_classes": hidden_classes,
                              "patches": [dict(p) for p in patches or []], "colour": correction is not None,
                              "ghosting": bool(ghosting), "fill_holes": bool(fill_holes),
                              "cameras": [c.key for c in ctx.cams]})
    profile = {"driver": "GTiff", "height": h, "width": w, "count": 4, "dtype": "uint8",
               "transform": from_origin(ctx.origin[0], ctx.origin[1], resolution, resolution),
               "compress": "deflate", "photometric": "RGB", "BIGTIFF": "IF_SAFER"}
    if source is not None:
        profile.update(dtype="float32", nodata=float("nan"))
        profile.pop("photometric")
    if h >= 256 and w >= 256:
        profile.update(tiled=True, blockxsize=256, blockysize=256)
    if not crs_mod.is_local(chunk.crs):
        profile["crs"] = crs_mod.to_pyproj(chunk.crs).to_wkt()
    product = out_path is None
    out_path = Path(out_path) if out_path is not None else chunk.data_dir / "orthomosaic.tif"
    tmp = out_path.with_name(out_path.stem + ".tmp.tif")
    try:
        _write_tiles(ctx, tmp, profile, tiles, ctx.workers, progress, shared)
    except BaseException:
        tmp.unlink(missing_ok=True)  # cancelled or failed: no half-written file left behind
        raise
    replace_file(tmp, out_path)  # in one move, under a program reading the orthomosaic (RustGIS through the link)
    if not product:
        progress(1.0, f"Orthomosaic {w}x{h} px")
        return out_path
    chunk._set_product("orthomosaic", out_path)
    chunk.meta["orthomosaic"] = {"resolution": resolution, "surface": surface, "blending": blending,
                                 "blend_sigma": float(ctx.blend_sigma), "colour_correction": correction is not None,
                                 "ghosting": ctx.ghosting, "fill_holes": ctx.fill_holes,
                                 "hidden_classes": hidden_classes, "max_angle": float(max_angle),
                                 "max_cameras": int(max_cameras)}
    if patches:
        chunk.meta["orthomosaic"]["patches"] = [dict(p) for p in patches]
        chunk.meta["orthomosaic"]["applied_patches"] = [dict(p) for p in patches]
    if bad:
        chunk.meta["orthomosaic"]["skipped_photos"] = [c.label for c in bad]
    progress(1.0, f"Orthomosaic {w}x{h} px")
    return out_path


def _one_blas_thread():
    """BLAS on one thread while the tiles are made: OpenBLAS (numpy on Linux and Windows) splits a
    product among its threads in a way that depends on how many calls run at once, and a row that
    lands in a remainder block is rounded by another kernel: one bit in a cell's coordinates was
    enough to pick another photo, so one worker and four gave different pixels. The tiles are
    already in parallel: one BLAS thread each is also the faster way."""
    try:
        from threadpoolctl import threadpool_limits
    except ImportError:  # pragma: no cover - a dependency of the program
        import contextlib

        return contextlib.nullcontext()
    return threadpool_limits(limits=1, user_api="blas")


def _run_tiles(ctx, tiles, workers: int, progress: Progress, put, start: float = 0.03, span: float = 0.9) -> None:
    """Make the tiles in parallel (at most twice the workers waiting) and hand each one to
    ``put(r0, c0, block)`` in this thread as soon as it is ready; the same pixels with any number
    of workers (:func:`_one_blas_thread`)."""
    done = 0
    with _one_blas_thread(), ThreadPoolExecutor(workers) as pool:
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


def _write_tiles(ctx, tmp: Path, profile: dict, tiles, workers: int, progress: Progress, shared=None) -> None:
    """Make the tiles in parallel and write each one as soon as it is ready, then the overviews;
    ``shared``: the nodes take part (:func:`_share_tiles`)."""
    import rasterio
    from rasterio.enums import ColorInterp, Resampling
    from rasterio.windows import Window

    h, w = ctx.shape
    with rasterio.open(tmp, "w", **profile) as dst:
        if profile.get("dtype") == "uint8":
            dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]

        def put(r0, c0, block):
            dst.write(block, window=Window(c0, r0, block.shape[2], block.shape[1]))

        if shared is not None:
            _share_tiles(ctx, tiles, put, progress, shared)
        else:
            _run_tiles(ctx, tiles, workers, progress, put)
        factors = [f for f in (2, 4, 8, 16, 32, 64, 128) if min(h, w) // f >= 64]
        if factors:
            progress(0.95, "Overviews of the orthomosaic")
            dst.build_overviews(factors, Resampling.average)


# ---------------------------------------------------------------- tiles made by other computers
def _nodes_folder(chunk: Chunk, tiles: int):
    """The files folder of the project when the nodes present take part in making ``tiles`` tiles
    (:func:`.nodes.sharing`), else None."""
    from . import nodes

    return nodes.sharing(chunk, -(-tiles // nodes.ORTHO_UNIT_TILES))


def _share_tiles(ctx, tiles, put, progress: Progress, shared, start: float = 0.03, span: float = 0.9,
                 what: str = "Orthomosaic") -> None:
    """The tiles made by the nodes present and by this program, in units of
    :data:`~.nodes.ORTHO_UNIT_TILES` tiles, each handed to ``put(r0, c0, block)`` in this thread
    as soon as it is here (the same pixels as :func:`_run_tiles`). ``shared``: the files folder and
    what a node needs besides the chunk to make the same tiles (:func:`node_tiles`); ``what``: the
    step, for its message."""
    from .. import __version__
    from . import nodes

    files_dir, spec = shared
    n = nodes.ORTHO_UNIT_TILES
    state = nodes.share_chunk(files_dir, ctx.chunk, ortho=spec)
    units = [{"kind": "ortho_tiles", "version": __version__, "state": state,
              "tiles": [[int(r), int(c)] for r, c in tiles[k:k + n]]} for k in range(0, len(tiles), n)]
    here: queue.Queue = queue.Queue()  # the tiles made by this program's worker, written in this thread

    def local(payload, quiet):
        _run_tiles(ctx, [tuple(t) for t in payload["tiles"]], ctx.workers, quiet,
                   lambda r0, c0, block: here.put((r0, c0, block)))
        return {}

    def write_here():
        while True:
            try:
                r0, c0, block = here.get_nowait()
            except queue.Empty:
                return
            put(r0, c0, block)

    def write_theirs(got):
        res = got.get("result") or {}
        if not res.get("file"):
            return
        path = nodes.folder(files_dir) / "done" / res["file"]
        with np.load(path) as data:
            for key in res.get("tiles", []):
                r0, c0 = (int(v) for v in key.split("_"))
                put(r0, c0, data[key])
        path.unlink(missing_ok=True)

    progress(start, f"{what}: {len(units)} units of work shared with {len(nodes.wanted(files_dir))} other computer(s)")
    try:
        results = nodes.run(files_dir, units, progress.sub(start, span), "units of tiles",
                            project_path=ctx.chunk.document.path, local=local, each=write_theirs, tick=write_here)
    finally:
        nodes.forget_state(files_dir, state)
    nodes.forget(files_dir, [r["name"] for r in results])


_NODE: dict = {}  # on a node: the step whose units it does (kept for its next units)
_NODE_LOCK = threading.Lock()


def node_tiles(payload: dict, mapping, out_dir) -> dict:
    """A unit of :func:`_share_tiles` on a node: its tiles made from the chunk and the options the
    main program shared, saved as ``<unit>.npz`` for it."""
    out_dir = Path(out_dir)
    with _NODE_LOCK:
        if _NODE.get("state") != payload["state"]:
            _NODE.clear()  # the photos and the mesh of the step before go first
            _NODE.update(state=payload["state"], ctx=_node_context(out_dir.parent, payload["state"], mapping))
        ctx = _NODE["ctx"]
    blocks: dict[str, np.ndarray] = {}
    _run_tiles(ctx, [tuple(t) for t in payload["tiles"]], ctx.workers, as_progress(None),
               lambda r0, c0, block: blocks.__setitem__(f"{r0}_{c0}", block))
    out = out_dir / f"{payload['name']}.npz"
    np.savez_compressed(out, **blocks)
    return {"file": out.name, "tiles": sorted(blocks)}


def _node_context(root: Path, state: str, mapping):
    """The context of the tiles of a shared step, rebuilt on a node (:func:`node_tiles`)."""
    from . import nodes

    chunk, data = nodes.node_chunk(root, state, mapping)
    spec = data["ortho"]
    by_key = {c.key: c for c in chunk.cameras}
    cams = [by_key[k] for k in spec["cameras"]]
    resolution = float(spec["resolution"])
    correction = correction_for(chunk) if spec["colour"] else None
    if spec["colour"] and correction is None:
        raise RuntimeError("the colour correction of the main program is missing")
    raycaster = raycaster_for_chunk(chunk)
    lock = threading.Lock()
    origin, shape = tuple(spec["origin"]), tuple(spec["shape"])
    if spec.get("render"):  # the orthophoto of a terrain texture: the model from above on its grid
        surf = _on_grid(_MeshSurface(chunk, raycaster, resolution, lock), origin, shape, resolution)
    else:
        surf = _surface(chunk, resolution, spec["surface"], raycaster, lock)
        if (tuple(surf.origin), tuple(surf.shape)) != (origin, shape):
            raise RuntimeError("the surface of the orthomosaic is not the same on this computer")
    return _context(chunk, resolution, cams, surf, raycaster, spec["blending"], spec["max_angle"],
                    spec["max_cameras"], None, spec.get("hide_classes"), spec.get("patches"), int(spec["tile"]), lock,
                    spec["blend_sigma"], correction, spec.get("ghosting", False), spec.get("fill_holes", False))


def render_surface(chunk: Chunk, origin, shape, resolution: float, cams=None, raycaster=None,
                   progress: Progress | None = None, workers: int | None = None, max_cameras: int = 8,
                   max_angle: float = 88.0, tile: int = 512, patches=None, correction=None) -> np.ndarray:
    """RGBA ``(4, h, w)`` of the model seen from above over a grid (``origin`` = top-left corner,
    world units): the orthophoto engine in memory (seamless mosaic), used for the texture of a
    terrain model - its tiles also made by the nodes present (:mod:`.nodes`). ``patches`` take
    their colours from the photos assigned to them; ``correction``: a colour correction
    (:func:`correction_for`, the one measured in the chunk)."""
    progress = as_progress(progress)
    cams = list(chunk.aligned_cameras if cams is None else cams)
    raycaster = raycaster if raycaster is not None else raycaster_for_chunk(chunk)
    if raycaster is None:
        raise RuntimeError("build the model first")
    lock = threading.Lock()
    surface = _on_grid(_MeshSurface(chunk, raycaster, resolution, lock), origin, shape, resolution)
    ctx = _context(chunk, resolution, cams, surface, raycaster, "mosaic", max_angle, max_cameras, workers,
                   None, patches, tile, lock, correction=correction)
    h, w = ctx.shape
    out = np.zeros((4, h, w), np.uint8)

    def put(r0, c0, block):
        out[:, r0:r0 + block.shape[1], c0:c0 + block.shape[2]] = block

    tiles = [(r, c) for r in range(0, h, tile) for c in range(0, w, tile)]
    files_dir = _nodes_folder(chunk, len(tiles))
    if files_dir is not None:
        spec = {"render": True, "origin": [float(v) for v in ctx.origin], "shape": [int(h), int(w)],
                "resolution": float(resolution), "tile": int(tile), "blending": "mosaic", "blend_sigma": BLEND_SIGMA,
                "max_angle": float(max_angle), "max_cameras": int(max_cameras),
                "patches": [dict(p) for p in patches or []], "colour": correction is not None,
                "cameras": [c.key for c in ctx.cams]}
        _share_tiles(ctx, tiles, put, progress, (files_dir, spec), start=0.0, span=1.0, what="Orthophoto of the model")
    else:
        _run_tiles(ctx, tiles, ctx.workers, progress, put, start=0.0, span=1.0)
    return out


def render_area(chunk: Chunk, origin, shape, resolution: float, cams=None, surface: str = "dem", raycaster=None,
                max_angle: float = 89.0, patches=None, blending: str = "mosaic", max_cameras: int = 8,
                blend_sigma: float = BLEND_SIGMA, correction=None) -> np.ndarray:
    """RGBA ``(4, h, w)`` of a small grid on the surface of the orthomosaic, in this thread
    (the preview of a patch from one photo)."""
    cams = list(readable_cameras(chunk, True)[0] if cams is None else cams)
    raycaster = raycaster if raycaster is not None else raycaster_for_chunk(chunk)
    lock = threading.Lock()
    surf = _on_grid(_surface(chunk, resolution, surface, raycaster, lock), origin, shape, resolution)
    ctx = _context(chunk, resolution, cams, surf, raycaster, blending, max_angle, max_cameras, 1, None,
                   patches, max(int(shape[0]), int(shape[1]), 1), lock, blend_sigma, correction)
    block = _tile(ctx, 0, 0)
    return np.zeros((4, int(shape[0]), int(shape[1])), np.uint8) if block is None else block


__all__ = ["build_orthomosaic", "correction_for", "hidden_cells", "render_area", "render_surface",
           "stored_blending", "BLENDINGS", "NODATA"]
