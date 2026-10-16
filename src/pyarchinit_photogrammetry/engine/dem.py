"""Digital elevation model (GeoTIFF, float32, in the chunk CRS)."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ..core import crs as crs_mod
from ..core import geometry
from ..core.chunk import Chunk
from ..core.pointcloud import NOISE_CLASSES
from ..core.progress import Progress

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


def build_dem(chunk: Chunk, resolution: float | None = None, source: str = "point_cloud", interpolate: bool = True,
              classes=None, progress: Progress | None = None) -> Path:
    """DEM of the highest surface point in each cell. ``classes`` (dense cloud): only these
    classes, e.g. the ground for a terrain model (DTM); the gaps they leave (under a removed
    tool or bush) are interpolated from around."""
    progress = progress or Progress()
    ensure_level(chunk)
    progress(0.05, "Collecting surface points")
    classes = None if classes is None else sorted({int(c) for c in classes})
    pts = surface_points_world(chunk, source, classes)
    if resolution is None:
        area = np.ptp(pts[:, 0]) * np.ptp(pts[:, 1])
        resolution = float(max(2 * estimate_gsd(chunk), np.sqrt(area / max(len(pts), 1)) * 1.5))
    progress(0.3, f"Rasterising DEM at {resolution:.4g} m/px")
    dem, origin, _ = grid_points(pts, resolution, reducer="max" if source != "tie_points" else "mean")
    if interpolate:
        from rasterio.fill import fillnodata

        mask = np.isfinite(dem).astype(np.uint8)
        filled = fillnodata(np.nan_to_num(dem, nan=0).astype(np.float32), mask=mask,
                            max_search_distance=30 if classes is None else 100, smoothing_iterations=0)
        dem = np.where(mask > 0, dem, filled)
        # keep holes outside the surveyed footprint empty
        from scipy import ndimage

        footprint = ndimage.binary_closing(mask > 0, iterations=10)
        footprint = ndimage.binary_fill_holes(footprint)
        dem[~footprint] = np.nan
    out = np.where(np.isfinite(dem), dem, NODATA).astype(np.float32)
    path = write_geotiff(chunk.data_dir / "dem.tif", out, origin, resolution, chunk.crs, nodata=NODATA)
    chunk._set_product("dem", path)
    chunk.meta["dem"] = {"resolution": resolution, "source": source, "classes": classes}
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
