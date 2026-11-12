"""Digital elevation model (GeoTIFF, float32, in the chunk CRS)."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ..core import crs as crs_mod
from ..core import geometry, stamps
from ..core.chunk import Chunk
from ..core.pointcloud import NOISE_CLASSES
from ..core.progress import Progress, as_progress

NODATA = -32767.0


def ensure_level(chunk: Chunk) -> None:
    """A local, non-georeferenced chunk is rotated so that its estimated "up"
    becomes +Z (the scale and the internal products are untouched); the free drawings follow it
    (:func:`..core.free_drawings.follow`)."""
    s, rot, t = geometry.decompose_similarity(chunk.transform)
    if not crs_mod.is_local(chunk.crs) or not np.allclose(rot, np.eye(3)) or chunk.meta.get("levelled"):
        return
    from .meshing import up_vector_internal

    up = up_vector_internal(chunk)
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(up, z)
    c = float(up @ z)
    if np.linalg.norm(v) < 1e-9:
        r = np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    else:
        vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
        r = np.eye(3) + vx + vx @ vx * (1 / (1 + c))
    chunk.transform = geometry.compose_similarity(s, r, t)
    chunk.meta["levelled"] = True
    from ..core import free_drawings

    free_drawings.follow(chunk)


def estimate_gsd(chunk: Chunk) -> float:
    """Median ground sampling distance (world units per pixel) of the aligned photos."""
    tp = chunk.tie_points
    if tp is None or not chunk.aligned_cameras:
        raise RuntimeError("align the cameras first")
    vals = []
    for cam in chunk.aligned_cameras:
        m = cam.cam_from_world
        z = (tp.xyz @ m[:3, :3].T + m[:3, 3])[:, 2]
        z = z[z > 0]
        if len(z):
            vals.append(np.median(z) / chunk.sensor(cam).focal_px)
    return float(np.median(vals) * chunk.scale)


def surface_points_world(chunk: Chunk, source: str, classes=None) -> np.ndarray:
    """Surface points in world coordinates; ``classes`` filters the dense cloud (noise never
    counts)."""
    if classes is not None and source != "point_cloud":
        raise ValueError("the classes are those of the dense cloud: use source='point_cloud'")
    if source == "model":
        mesh = chunk.model
        if mesh is None:
            raise RuntimeError("build the model first")
        import trimesh

        n = max(200_000, len(mesh.faces) * 8)
        pts, _ = trimesh.sample.sample_surface_even(mesh, n) if len(mesh.faces) < 2_000_000 else trimesh.sample.sample_surface(mesh, n)
        pts = np.vstack([pts, mesh.vertices])
    elif source == "tie_points":
        pts = chunk.tie_points.xyz
    else:
        pc = chunk.point_cloud
        if pc is None:
            raise RuntimeError("build the point cloud first")
        keep = pc.class_mask(classes, exclude=NOISE_CLASSES)
        if not keep.any():
            raise RuntimeError("the dense cloud has no point of the chosen classes")
        pts = pc.xyz[keep]
    return chunk.to_world(pts)


STREAM_FACES = 2_000_000  # models from this many faces on are sampled block by block (below: evenly, at once)
FACE_BLOCK = 250_000  # faces read at a time
SAMPLE_BLOCK = 2_000_000  # surface points made at a time


class ModelSurface:
    """The surface points :func:`surface_points_world` takes from a large model - eight per face,
    spread by area, and the vertices - made a block of faces at a time and put straight on the grid
    of the DEM, never all together: at once they took about 1.5 kB of memory per face (9 GB for a
    model of 4.5 million faces; a model of 1.3 GB did not fit in 24 GB and the system ended the
    program without a word). Here the memory is the vertex coordinates of the model, as the file
    holds them (12 bytes per vertex in single precision), a few blocks of points and the grid.

    The mesh is read through :class:`..io.mesh_ply.PlyMesh` (memory-mapped faces). The points of a
    block of faces come from a random generator seeded with the block: two passes over the model
    give the same points (the spacing first, when the resolution is not given, then the grid) and
    the same model gives the same DEM."""

    def __init__(self, chunk: Chunk, mesh, seed: int = 0):
        from .satellite import _physical_memory

        self.chunk, self.mesh, self.seed = chunk, mesh, seed
        coord = np.result_type(*(mesh.vertex_dtype[k] for k in "xyz"))
        need = mesh.n_vertices * 3 * coord.itemsize
        ram = _physical_memory()
        if need > 0.6 * ram:
            raise MemoryError(
                f"the model has {mesh.n_vertices / 1e6:.0f} million vertices: their coordinates take "
                f"{need / 1e9:.1f} GB and this computer has {ram / 1e9:.0f} GB of memory; decimate the model "
                "or build the DEM from the dense cloud")
        self.xyz = np.empty((mesh.n_vertices, 3), coord)
        lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
        for start in range(0, mesh.n_vertices, SAMPLE_BLOCK):
            v = mesh.vertices[start:start + SAMPLE_BLOCK]
            for k, name in enumerate("xyz"):
                self.xyz[start:start + SAMPLE_BLOCK, k] = v[name]
            world = chunk.to_world(self.xyz[start:start + SAMPLE_BLOCK])
            lo, hi = np.minimum(lo, world.min(axis=0)), np.maximum(hi, world.max(axis=0))
        self.bounds = (lo[0], lo[1], hi[0], hi[1])  # of all the points: the samples lie inside their faces
        self.samples = max(200_000, mesh.n_faces * 8)
        self.count = self.samples + mesh.n_vertices
        self._areas = np.array([self._face_areas(self._triangles(start)).sum()
                                for start in range(0, mesh.n_faces, FACE_BLOCK)])

    def _triangles(self, start: int) -> np.ndarray:
        faces = np.asarray(self.mesh._faces[self.mesh.index_field][start:start + FACE_BLOCK])
        return self.xyz[faces].astype(np.float64)  # (faces, 3 corners, 3)

    @staticmethod
    def _face_areas(tri: np.ndarray) -> np.ndarray:
        area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
        return np.nan_to_num(area, nan=0.0, posinf=0.0, neginf=0.0)

    def blocks(self):
        """The points in world coordinates, a block at a time: the vertices, then the samples of each
        block of faces, as many as its share of the area of the model."""
        for start in range(0, self.mesh.n_vertices, SAMPLE_BLOCK):
            yield self.chunk.to_world(self.xyz[start:start + SAMPLE_BLOCK])
        total = float(self._areas.sum())
        if total <= 0:
            return
        upto = np.rint(self.samples * np.cumsum(self._areas) / total).astype(np.int64)
        share = np.diff(upto, prepend=0)
        for b, start in enumerate(range(0, self.mesh.n_faces, FACE_BLOCK)):
            if share[b] <= 0:
                continue
            tri = self._triangles(start)
            cum = np.cumsum(self._face_areas(tri))
            rng = np.random.default_rng([self.seed, b])
            for done in range(0, int(share[b]), SAMPLE_BLOCK):
                k = min(SAMPLE_BLOCK, int(share[b]) - done)
                face = np.minimum(np.searchsorted(cum, rng.random(k) * cum[-1], side="right"), len(cum) - 1)
                u, v = rng.random(k), rng.random(k)
                fold = u + v > 1.0  # the half of the parallelogram outside the triangle, folded back
                u[fold], v[fold] = 1.0 - u[fold], 1.0 - v[fold]
                t = tri[face]
                pts = t[:, 0] + u[:, None] * (t[:, 1] - t[:, 0]) + v[:, None] * (t[:, 2] - t[:, 0])
                yield self.chunk.to_world(pts)

    def spacing(self) -> float:
        """:func:`point_spacing` of the points, without holding them."""
        xmin, ymin, xmax, ymax = self.bounds
        box = float((xmax - xmin) * (ymax - ymin))
        if box <= 0:
            return 0.0
        cell = 10.0 * np.sqrt(box / self.count)
        c0, r0 = int(np.floor(xmin / cell)), int(np.floor(ymin / cell))
        w, h = int(np.floor(xmax / cell)) - c0 + 1, int(np.floor(ymax / cell)) - r0 + 1
        seen = np.zeros(w * h, bool)
        for pts in self.blocks():
            col = np.clip(np.floor(pts[:, 0] / cell).astype(np.int64) - c0, 0, w - 1)
            row = np.clip(np.floor(pts[:, 1] / cell).astype(np.int64) - r0, 0, h - 1)
            seen[row * w + col] = True
        occupied = int(seen.sum()) * cell * cell
        return float(np.sqrt(min(occupied, box) / self.count))

    def grid(self, resolution: float, progress: Progress | None = None):
        """:func:`grid_points` of the points (the highest in each cell): ``(array, (xmin, ymax))``."""
        xmin, ymax, w, h = _grid_frame(self.bounds, resolution)
        arr = np.full(w * h, -np.inf)
        done = 0
        for pts in self.blocks():
            idx, z = _grid_cells(pts, xmin, ymax, w, h, resolution)
            np.maximum.at(arr, idx, z)
            done += len(pts)
            if progress is not None:
                progress(done / self.count, f"Model surface: {done / 1e6:.0f} of {self.count / 1e6:.0f} million points")
        arr[~np.isfinite(arr)] = np.nan
        return arr.reshape(h, w), (xmin, ymax)


def model_surface(chunk: Chunk) -> ModelSurface | None:
    """The model of the chunk as a :class:`ModelSurface` when it is large (``STREAM_FACES`` faces or
    more) and its file can be read in blocks; None for a smaller one, sampled at once as before, and
    for the files only trimesh reads."""
    from ..io.mesh_ply import PlyMesh

    path = chunk.product_path("model")
    if not path or not path.exists():
        raise RuntimeError("build the model first")
    try:
        mesh = PlyMesh(path)
    except (ValueError, KeyError, OSError):
        return None
    return ModelSurface(chunk, mesh) if mesh.n_faces >= STREAM_FACES else None


def point_spacing(xy: np.ndarray) -> float:
    """Mean distance between neighbouring points over the ground they cover: the occupied cells
    of a grid ten times coarser than an even spread over their box, so that the empty corners of a
    flight turned to the north or of an irregular site do not count as sparse ground (the box of the
    Tell Sultan sample is 1.4 times the ground its points cover)."""
    n = len(xy)
    box = float(np.ptp(xy[:, 0]) * np.ptp(xy[:, 1])) if n else 0.0
    if box <= 0:
        return 0.0
    cell = 10.0 * np.sqrt(box / n)
    occupied = len(np.unique(np.floor(xy / cell).astype(np.int64), axis=0)) * cell * cell
    return float(np.sqrt(min(occupied, box) / n))


def remove_spikes(dem: np.ndarray, resolution: float) -> tuple[np.ndarray, int]:
    """Single-cell spikes and pits - cells above all their neighbours with points, or below all of
    them, by more than five times the noise (NMAD of the cells against the mean of their neighbours)
    and at least two cells - take the median of those neighbours. With about one point per cell the
    highest point of a cell is now and then a stray one (Tell Sultan sample: 1.4 % of the cells,
    against 0.4 % of such cells in Metashape's DEM); the corner of a wall, which has neighbours as
    high as itself, stays. Empty cells do not count (the noise was measured against the median
    height of the whole DEM put in them: on a grid with gaps it came out metres and no spike went);
    cells with fewer than two neighbours are left to :func:`remove_stray_cells`. Returns the DEM
    and the cells changed."""
    from scipy import ndimage

    ok = np.isfinite(dem)
    if ok.sum() < 9:
        return dem, 0
    ring = np.ones((3, 3), bool)
    ring[1, 1] = False
    filled_low = np.where(ok, dem, -np.inf)
    filled_high = np.where(ok, dem, np.inf)
    highest = ndimage.maximum_filter(filled_low, footprint=ring, mode="constant", cval=-np.inf)
    lowest = ndimage.minimum_filter(filled_high, footprint=ring, mode="constant", cval=np.inf)
    count = ndimage.convolve(ok.astype(np.float64), ring.astype(np.float64), mode="constant")
    total = ndimage.convolve(np.where(ok, dem, 0.0).astype(np.float64), ring.astype(np.float64), mode="constant")
    judged = ok & (count >= 2)  # with one neighbour, a stray and a good cell look the same: they swapped
    if not judged.any():
        return dem, 0
    nmad = 1.4826 * float(np.median(np.abs(dem[judged] - total[judged] / count[judged])))
    threshold = max(5.0 * nmad, 2.0 * resolution)
    with np.errstate(invalid="ignore"):
        spikes = judged & ((dem - highest > threshold) | (lowest - dem > threshold))
    out = dem.copy()
    if spikes.any():  # the median of the neighbours with points
        rows, cols = np.nonzero(spikes)
        padded = np.pad(np.where(ok, dem, np.nan).astype(np.float64), 1, constant_values=np.nan)
        around = np.stack([padded[rows + 1 + dr, cols + 1 + dc] for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                           if dr or dc], axis=1)
        out[rows, cols] = np.nanmedian(around, axis=1)
    return out, int(spikes.sum())


def _grid_frame(bounds, resolution: float) -> tuple[float, float, int, int]:
    """The grid of :func:`grid_points` over ``bounds`` (xmin, ymin, xmax, ymax): its left and top
    edges, width and height in cells."""
    xmin, ymin, xmax, ymax = bounds
    xmin = np.floor(xmin / resolution) * resolution
    ymax = np.ceil(ymax / resolution) * resolution
    w = int(np.ceil((xmax - xmin) / resolution)) + 1
    h = int(np.ceil((ymax - ymin) / resolution)) + 1
    return xmin, ymax, w, h


def _grid_cells(pts: np.ndarray, xmin: float, ymax: float, w: int, h: int, resolution: float):
    """The cell (index in the flattened grid) and the height of the points that fall on the grid."""
    col = ((pts[:, 0] - xmin) / resolution).astype(np.int64)
    row = ((ymax - pts[:, 1]) / resolution).astype(np.int64)
    ok = (col >= 0) & (col < w) & (row >= 0) & (row < h)
    return row[ok] * w + col[ok], pts[ok, 2]


def grid_points(pts: np.ndarray, resolution: float, bounds=None, reducer: str = "max"):
    """Rasterise points; returns ``(array, (xmin, ymax), shape)``. Empty cells = NaN."""
    if bounds is None:
        bounds = pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()
    xmin, ymax, w, h = _grid_frame(bounds, resolution)
    idx, z = _grid_cells(pts, xmin, ymax, w, h, resolution)
    if reducer == "mean":
        s = np.bincount(idx, weights=z, minlength=w * h)
        n = np.bincount(idx, minlength=w * h)
        with np.errstate(invalid="ignore", divide="ignore"):
            arr = s / n
        arr[n == 0] = np.nan
    else:
        fn = np.maximum if reducer == "max" else np.minimum
        arr = np.full(w * h, -np.inf if reducer == "max" else np.inf)
        fn.at(arr, idx, z)
        arr[~np.isfinite(arr)] = np.nan
    return arr.reshape(h, w), (xmin, ymax), (h, w)


def geotiff_profile(height: int, width: int, count: int, dtype, transform, crs, nodata=None,
                    photometric: str | None = None) -> dict:
    """The rasterio profile of the GeoTIFFs of the program (deflate, tiled 256, BigTIFF when
    needed) for a raster of ``height`` x ``width`` cells and ``count`` bands on ``transform``;
    ``crs`` a string of the program (``None``/``LOCAL`` = no CRS) or the CRS object of a rasterio
    dataset, written as it is (an empty one = no CRS)."""
    profile = {
        "driver": "GTiff", "height": height, "width": width, "count": count, "dtype": str(np.dtype(dtype)),
        "transform": transform,
        "compress": "deflate", "tiled": True, "blockxsize": 256, "blockysize": 256, "BIGTIFF": "IF_SAFER",
    }
    if height < 256 or width < 256:
        profile.update(tiled=False)
        profile.pop("blockxsize")
        profile.pop("blockysize")
    if crs is not None and not isinstance(crs, str):
        if crs:  # a CRS object of rasterio (an empty one = no CRS)
            profile["crs"] = crs
    elif not crs_mod.is_local(crs):
        profile["crs"] = crs_mod.to_pyproj(crs).to_wkt()
    if nodata is not None:
        profile["nodata"] = nodata
    if photometric:
        profile["photometric"] = photometric
    return profile


def finish_geotiff(dst) -> None:
    """What :func:`write_geotiff` does once the cells are written: RGBA colour interpretation for
    four bands, then the overviews (average)."""
    if dst.count == 4:
        from rasterio.enums import ColorInterp

        dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
    factors = [f for f in (2, 4, 8, 16, 32) if min(dst.height, dst.width) // f >= 64]
    if factors:
        from rasterio.enums import Resampling

        dst.build_overviews(factors, Resampling.average)


def write_geotiff(path: Path, array: np.ndarray, origin, resolution: float, crs, nodata=None,
                  photometric: str | None = None, transform=None) -> Path:
    """A GeoTIFF (deflate, tiled, BigTIFF when needed, overviews) of ``array`` (bands first) with
    its top-left corner at ``origin`` and square cells of ``resolution``, or on ``transform`` (an
    affine of rasterio, as read from another raster) when given; ``crs`` a string of the program
    (``None``/``LOCAL`` = no CRS) or the CRS object of a rasterio dataset, written as it is."""
    import rasterio
    from rasterio.transform import from_origin

    arr = array if array.ndim == 3 else array[None]
    if transform is None:
        transform = from_origin(origin[0], origin[1], resolution, resolution)
    profile = geotiff_profile(arr.shape[1], arr.shape[2], arr.shape[0], arr.dtype, transform, crs, nodata,
                              photometric)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr)
        finish_geotiff(dst)
    return path


COARSE = 16  # DEM cells per side of the cells that decide which ground a survey covers


def covered_ground(mask: np.ndarray, coarse: int = COARSE) -> np.ndarray:
    """The ground a survey covers, on the grid of ``mask`` (the cells with points). It is decided
    on cells ``coarse`` times larger, where any point counts: the margins of a flight, seen by two
    or three photos, have a point every half metre where the centre has several per decimetre, and
    on the fine grid they were scattered dots (Tell Sultan, 425 photos: a quarter of the ground of
    Metashape's DEM). Coarse cells with fewer than two occupied neighbours - stray points around the
    survey - are left out, gaps of up to four coarse cells close and the holes inside fill. Along
    its edge the ground reaches at most ``coarse`` cells beyond the last point."""
    from scipy import ndimage

    h, w = mask.shape
    rows, cols = -(-h // coarse), -(-w // coarse)
    pad = 3  # coarse cells around, so that the closing does not eat the edges of the array
    occupied = np.zeros((rows + 2 * pad, cols + 2 * pad), bool)
    blocks = np.zeros((rows * coarse, cols * coarse), bool)
    blocks[:h, :w] = mask
    occupied[pad:-pad, pad:-pad] = blocks.reshape(rows, coarse, cols, coarse).any(axis=(1, 3))
    neighbours = ndimage.convolve(occupied.astype(np.uint8), np.ones((3, 3), np.uint8), mode="constant")
    occupied &= neighbours - 1 >= 2
    occupied = ndimage.binary_fill_holes(ndimage.binary_closing(occupied, iterations=2))
    inner = ndimage.binary_erosion(occupied)
    def up(a):
        return np.repeat(np.repeat(a[pad:-pad, pad:-pad], coarse, axis=0), coarse, axis=1)[:h, :w]

    near = ndimage.binary_dilation(mask, iterations=coarse)
    return up(inner) | (up(occupied) & near)


def remove_stray_cells(dem: np.ndarray, resolution: float, neighbours: int = 8) -> tuple[np.ndarray, int]:
    """Cells with a point and at most one around them (:func:`remove_spikes` cannot judge them) - the
    sparse margins of a flight, a stray point beside the survey - measured against the plane of the
    ``neighbours`` nearest cells with points: they go when they are off it by more than four times
    the scatter of those cells about it, and at least two cells. Interpolated, a stray point became
    a cone metres wide. Returns the DEM and the cells removed."""
    from scipy import ndimage
    from scipy.spatial import cKDTree

    ok = np.isfinite(dem)
    ring = np.ones((3, 3), np.uint8)
    ring[1, 1] = 0
    alone = ok & (ndimage.convolve(ok.astype(np.uint8), ring, mode="constant") <= 1)
    if not alone.any() or ok.sum() <= neighbours + 1:
        return dem, 0
    cells = np.argwhere(ok)
    tree = cKDTree(cells)
    targets = np.argwhere(alone)
    _, idx = tree.query(targets, k=neighbours + 1, workers=-1)
    near = cells[idx[:, 1:]]  # the nearest cells but the cell itself
    d = (near - targets[:, None, :]).astype(np.float64) * resolution  # rows, cols in metres
    z = dem[near[..., 0], near[..., 1]].astype(np.float64)
    a = np.concatenate([np.ones(z.shape + (1,)), d], axis=2)
    # a plane without each neighbour in turn, the one the others fit best: a stray among the
    # neighbours - the farthest of them tilted the plane of all eight enough to save the cell
    leave = 1.0 - np.eye(neighbours)
    ata = np.einsum("jk,mki,mkl->mjil", leave, a, a) + np.diag([1e-12, 1e-9, 1e-9])
    planes = np.linalg.solve(ata, np.einsum("jk,mki,mk->mji", leave, a, z)[..., None])[..., 0]
    resid = np.abs(z[:, None, :] - np.einsum("mki,mji->mjk", a, planes))
    resid[:, np.arange(neighbours), np.arange(neighbours)] = np.nan
    fit = np.nanmedian(resid, axis=2)
    best = np.argmin(fit, axis=1)
    coef = planes[np.arange(len(best)), best]
    scatter = 1.4826 * fit[np.arange(len(best)), best]
    off = np.abs(dem[targets[:, 0], targets[:, 1]] - coef[:, 0])
    stray = off > np.maximum(4.0 * scatter, 2.0 * resolution)
    out = dem.copy()
    out[targets[stray, 0], targets[stray, 1]] = np.nan
    return out, int(stray.sum())


def remove_pits(dem: np.ndarray, resolution: float, depth: float = 2.0, scale: float = 3.0, window: int = 5,
                sparse: float = 0.1) -> tuple[np.ndarray, int]:
    """Cells of the thinly covered parts of the DEM that lie more than ``depth`` metres above or
    below the ground around them are emptied (to be filled from around): the ground around is
    the median of the DEM over cells of ``scale`` metres, smoothed as the median of ``window``
    such cells a side; a part is thinly covered when fewer than ``sparse`` of the fine cells of
    its coarse cell have a height. At the edge of a survey from the air, where two or three
    photos see the ground at the border of their frames, a few wrong points of the dense cloud
    made pits of 5-20 m in the DEM of Tell Sultan; where the cloud is dense the DEM is trusted
    as it is, so the trenches and walls of an excavation keep their depth and height. Returns the
    DEM and the cells emptied."""
    from scipy import ndimage

    ok = np.isfinite(dem)
    n = max(1, int(round(scale / resolution)))
    H, W = dem.shape
    Hc, Wc = -(-H // n), -(-W // n)
    padded = np.full((Hc * n, Wc * n), np.nan)
    padded[:H, :W] = dem
    blocks = padded.reshape(Hc, n, Wc, n).transpose(0, 2, 1, 3).reshape(Hc, Wc, n * n)
    with np.errstate(all="ignore"):
        coarse = np.nanmedian(blocks, axis=2)
    filled = np.isfinite(blocks).mean(axis=2)
    if np.isfinite(coarse).sum() < window * window:
        return dem, 0
    around = ndimage.generic_filter(coarse, np.nanmedian, size=window, mode="constant", cval=np.nan)
    thin = filled < sparse
    ref = np.repeat(np.repeat(around, n, axis=0), n, axis=1)[:H, :W]
    judge = np.repeat(np.repeat(thin, n, axis=0), n, axis=1)[:H, :W]
    with np.errstate(invalid="ignore"):
        pits = ok & judge & np.isfinite(ref) & (np.abs(dem - ref) > depth)
    out = dem.copy()
    out[pits] = np.nan
    return out, int(pits.sum())


def dem_from_points(pts: np.ndarray, resolution: float, reducer: str = "max", despike: bool = True,
                    interpolate: bool = True, max_fill: int | None = None):
    """Grid of the points (``reducer`` per cell), spikes, stray cells and the pits of the thinly
    covered parts removed (:func:`remove_spikes`, :func:`remove_stray_cells`, :func:`remove_pits`),
    the cells without points interpolated across the ground the survey covers
    (:func:`covered_ground`) up to ``max_fill`` cells from a point (default four coarse cells).
    Returns ``(dem, origin, cells removed)``."""
    dem, origin, _ = grid_points(pts, resolution, reducer=reducer)
    dem, removed = _finish_dem(dem, resolution, despike, interpolate, max_fill)
    return dem, origin, removed


def _finish_dem(dem: np.ndarray, resolution: float, despike: bool, interpolate: bool, max_fill: int | None):
    """What :func:`dem_from_points` does to the grid of the points; returns ``(dem, cells removed)``."""
    removed = 0
    if despike:
        dem, spikes = remove_spikes(dem, resolution)
        dem, strays = remove_stray_cells(dem, resolution)
        dem, pits = remove_pits(dem, resolution)
        removed = spikes + strays + pits
    if interpolate:
        from rasterio.fill import fillnodata

        mask = np.isfinite(dem)
        # cells the search does not reach keep their NaN (with zeros they came out at height 0)
        filled = fillnodata(dem.astype(np.float32), mask=mask.astype(np.uint8),
                            max_search_distance=max_fill or 4 * COARSE, smoothing_iterations=0)
        dem = np.where(mask, dem, filled)
        dem[~covered_ground(mask)] = np.nan  # outside the surveyed ground the cells stay empty
    return dem, removed


def build_dem(chunk: Chunk, resolution: float | None = None, source: str = "point_cloud", interpolate: bool = True,
              classes=None, progress: Progress | None = None, despike: bool = True) -> Path:
    """DEM of the highest surface point in each cell. ``classes`` (dense cloud): only these
    classes, e.g. the ground for a terrain model (DTM); the gaps they leave (under a removed
    tool or bush) are interpolated from around."""
    progress = as_progress(progress)
    ensure_level(chunk)
    progress(0.05, "Collecting surface points")
    classes = None if classes is None else sorted({int(c) for c in classes})
    surface = model_surface(chunk) if source == "model" and classes is None else None
    if surface is not None:  # a large model: its points go on the grid block by block, never all in memory
        if resolution is None:
            resolution = float(max(2 * estimate_gsd(chunk), surface.spacing()))
        progress(0.1, f"Rasterising DEM at {resolution:.4g} m/px")
        dem, origin = surface.grid(resolution, progress.sub(0.1, 0.5))
        del surface
        dem, spikes = _finish_dem(dem, resolution, despike, interpolate, None)
    else:
        pts = surface_points_world(chunk, source, classes)
        if resolution is None:
            # a cell per point, as dense as the cloud (Metashape's default too): with 1.5 points per
            # cell the DEM of the Tell Sultan sample was 1.5 times coarser and further from Metashape's.
            # With classes, the spacing of the whole cloud: DEMs of other classes on the same grid
            whole = pts if classes is None else surface_points_world(chunk, source, None)
            resolution = float(max(2 * estimate_gsd(chunk), point_spacing(whole[:, :2])))
        progress(0.3, f"Rasterising DEM at {resolution:.4g} m/px")
        dem, origin, spikes = dem_from_points(pts, resolution, reducer="max" if source != "tie_points" else "mean",
                                              despike=despike, interpolate=interpolate,
                                              max_fill=None if classes is None else 100)
    out = np.where(np.isfinite(dem), dem, NODATA).astype(np.float32)
    chunk._before_product_write("dem")
    path = write_geotiff(chunk.data_dir / "dem.tif", out, origin, resolution, chunk.crs, nodata=NODATA)
    chunk._set_product("dem", path)
    stamps.stamp(chunk, "dem")
    chunk.meta["dem"] = {"resolution": resolution, "source": source, "classes": classes, "spikes_removed": spikes}
    progress(1.0, f"DEM {out.shape[1]}x{out.shape[0]} px")
    return path


def read_dem(path: Path):
    """Return ``(array with NaN, transform, crs, resolution)``."""
    import rasterio

    with rasterio.open(path) as src:
        arr = src.read(1).astype(np.float64)
        if src.nodata is not None:
            arr[arr == src.nodata] = np.nan
        return arr, src.transform, src.crs, src.res[0]
