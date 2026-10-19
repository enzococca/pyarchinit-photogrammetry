"""Digital elevation model (GeoTIFF, float32, in the chunk CRS)."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ..core import crs as crs_mod
from ..core import geometry
from ..core.chunk import Chunk
from ..core.pointcloud import NOISE_CLASSES
from ..core.progress import Progress, as_progress

NODATA = -32767.0


def ensure_level(chunk: Chunk) -> None:
    """A local, non-georeferenced chunk is rotated so that its estimated "up"
    becomes +Z (the scale and the internal products are untouched)."""
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


def grid_points(pts: np.ndarray, resolution: float, bounds=None, reducer: str = "max"):
    """Rasterise points; returns ``(array, (xmin, ymax), shape)``. Empty cells = NaN."""
    if bounds is None:
        xmin, ymin = pts[:, 0].min(), pts[:, 1].min()
        xmax, ymax = pts[:, 0].max(), pts[:, 1].max()
    else:
        xmin, ymin, xmax, ymax = bounds
    xmin = np.floor(xmin / resolution) * resolution
    ymax = np.ceil(ymax / resolution) * resolution
    w = int(np.ceil((xmax - xmin) / resolution)) + 1
    h = int(np.ceil((ymax - ymin) / resolution)) + 1
    col = ((pts[:, 0] - xmin) / resolution).astype(np.int64)
    row = ((ymax - pts[:, 1]) / resolution).astype(np.int64)
    ok = (col >= 0) & (col < w) & (row >= 0) & (row < h)
    idx = row[ok] * w + col[ok]
    z = pts[ok, 2]
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


def write_geotiff(path: Path, array: np.ndarray, origin, resolution: float, crs: str | None, nodata=None,
                  photometric: str | None = None) -> Path:
    import rasterio
    from rasterio.transform import from_origin

    arr = array if array.ndim == 3 else array[None]
    profile = {
        "driver": "GTiff", "height": arr.shape[1], "width": arr.shape[2], "count": arr.shape[0],
        "dtype": str(arr.dtype), "transform": from_origin(origin[0], origin[1], resolution, resolution),
        "compress": "deflate", "tiled": True, "blockxsize": 256, "blockysize": 256, "BIGTIFF": "IF_SAFER",
    }
    if arr.shape[1] < 256 or arr.shape[2] < 256:
        profile.update(tiled=False)
        profile.pop("blockxsize")
        profile.pop("blockysize")
    if not crs_mod.is_local(crs):
        profile["crs"] = crs_mod.to_pyproj(crs).to_wkt()
    if nodata is not None:
        profile["nodata"] = nodata
    if photometric:
        profile["photometric"] = photometric
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr)
        if arr.shape[0] == 4:
            from rasterio.enums import ColorInterp

            dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        factors = [f for f in (2, 4, 8, 16, 32) if min(arr.shape[1:]) // f >= 64]
        if factors:
            from rasterio.enums import Resampling

            dst.build_overviews(factors, Resampling.average)
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
    up = lambda a: np.repeat(np.repeat(a[pad:-pad, pad:-pad], coarse, axis=0), coarse, axis=1)[:h, :w]
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


def dem_from_points(pts: np.ndarray, resolution: float, reducer: str = "max", despike: bool = True,
                    interpolate: bool = True, max_fill: int | None = None):
    """Grid of the points (``reducer`` per cell), spikes and stray cells removed
    (:func:`remove_spikes`, :func:`remove_stray_cells`), the cells without points interpolated
    across the ground the survey covers (:func:`covered_ground`) up to ``max_fill`` cells from a
    point (default four coarse cells). Returns ``(dem, origin, cells removed)``."""
    dem, origin, _ = grid_points(pts, resolution, reducer=reducer)
    removed = 0
    if despike:
        dem, spikes = remove_spikes(dem, resolution)
        dem, strays = remove_stray_cells(dem, resolution)
        removed = spikes + strays
    if interpolate:
        from rasterio.fill import fillnodata

        mask = np.isfinite(dem)
        # cells the search does not reach keep their NaN (with zeros they came out at height 0)
        filled = fillnodata(dem.astype(np.float32), mask=mask.astype(np.uint8),
                            max_search_distance=max_fill or 4 * COARSE, smoothing_iterations=0)
        dem = np.where(mask, dem, filled)
        dem[~covered_ground(mask)] = np.nan  # outside the surveyed ground the cells stay empty
    return dem, origin, removed


def build_dem(chunk: Chunk, resolution: float | None = None, source: str = "point_cloud", interpolate: bool = True,
              classes=None, progress: Progress | None = None, despike: bool = True) -> Path:
    """DEM of the highest surface point in each cell. ``classes`` (dense cloud): only these
    classes, e.g. the ground for a terrain model (DTM); the gaps they leave (under a removed
    tool or bush) are interpolated from around."""
    progress = as_progress(progress)
    ensure_level(chunk)
    progress(0.05, "Collecting surface points")
    classes = None if classes is None else sorted({int(c) for c in classes})
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
    path = write_geotiff(chunk.data_dir / "dem.tif", out, origin, resolution, chunk.crs, nodata=NODATA)
    chunk._set_product("dem", path)
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
