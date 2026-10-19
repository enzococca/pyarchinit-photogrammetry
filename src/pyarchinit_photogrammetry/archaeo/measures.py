"""Measures on the survey: surface area in 3D, slope and aspect, height above the ground.

* :func:`surface_area`: inside a plan polygon, the area in plan and the area of the surface - from
  the DEM (each cell counted ``sqrt(1 + gx^2 + gy^2)`` times, the cells on the border by the share
  of them inside) or from the faces of the model whose centre is inside;
* :func:`slope_aspect_at`: slope (degrees) and aspect (degrees clockwise from north, the direction
  the ground faces downhill) of the DEM at a point, from its 3 x 3 cells (Horn);
* :func:`aspect_raster`: the aspect of the whole DEM as a GeoTIFF (-1 where flat);
* :func:`ground_surface` / :func:`height_above_ground`: the ground from the points of the ground
  class (2) of the dense cloud, on a grid with the holes filled, and the height of every point
  above it (vegetation, walls, people); :func:`ground_dem_at` reads a DTM kept as a version of the
  DEM, for the height above the ground under the cursor.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

GROUND = 2


def _window(src, polygon, pad: int = 1):
    from rasterio.windows import Window, from_bounds

    win = from_bounds(*polygon.bounds, transform=src.transform).round_offsets().round_lengths()
    col0, row0 = max(int(win.col_off) - pad, 0), max(int(win.row_off) - pad, 0)
    col1 = min(int(win.col_off + win.width) + pad + 1, src.width)
    row1 = min(int(win.row_off + win.height) + pad + 1, src.height)
    return Window(col0, row0, max(col1 - col0, 0), max(row1 - row0, 0))


def surface_area(dem_path: str | Path, polygon, model=None) -> dict:
    """Areas inside ``polygon`` (shapely, plan, the CRS of the DEM): ``plan`` and ``surface``
    from the DEM (square metres), ``mean_slope`` (degrees), ``z_min``/``z_max``; with ``model``
    (a mesh in the same coordinates with ``vertices`` and ``faces``) also ``model_surface`` and
    ``model_plan`` from its faces whose centre is inside."""
    import rasterio
    from shapely import contains_xy

    from .terrain import _coverage

    out = {"plan_polygon": float(polygon.area)}
    with rasterio.open(dem_path) as src:
        win = _window(src, polygon)
        z = src.read(1, window=win, masked=True).astype(np.float64).filled(np.nan)
        tr = src.window_transform(win)
    res_x, res_y = abs(tr.a), abs(tr.e)
    cov = _coverage(polygon, z.shape, tr)
    valid = np.isfinite(z) & (cov > 0)
    if valid.any():
        zf = np.where(np.isfinite(z), z, np.nanmean(z[valid]))
        gy, gx = np.gradient(zf, res_y, res_x)
        factor = np.sqrt(1.0 + gx ** 2 + gy ** 2)
        cell = res_x * res_y
        out.update(plan=float((cov * cell)[valid].sum()), surface=float((cov * cell * factor)[valid].sum()),
                   mean_slope=float(np.degrees(np.arctan(np.sqrt(np.average((factor[valid] ** 2 - 1),
                                                                             weights=cov[valid]))))),
                   z_min=float(z[valid].min()), z_max=float(z[valid].max()))
    if model is not None:
        v = np.asarray(model.vertices, float)
        f = np.asarray(model.faces, int)
        tri = v[f]
        centre = tri.mean(axis=1)
        inside = contains_xy(polygon, centre[:, 0], centre[:, 1])
        cross = np.cross(tri[inside, 1] - tri[inside, 0], tri[inside, 2] - tri[inside, 0])
        out.update(model_surface=float(0.5 * np.linalg.norm(cross, axis=1).sum()),
                   model_plan=float(0.5 * np.abs(cross[:, 2]).sum()), model_faces=int(inside.sum()))
    return out


def slope_aspect_at(dem_path: str | Path, x: float, y: float) -> tuple[float, float] | None:
    """Slope and aspect (degrees) of the DEM at world ``(x, y)``, None outside or at its edge."""
    import rasterio
    from rasterio.windows import Window

    with rasterio.open(dem_path) as src:
        col, row = ~src.transform @ (x, y)
        c, r = int(np.floor(col)), int(np.floor(row))
        if not (1 <= c < src.width - 1 and 1 <= r < src.height - 1):
            return None
        z = src.read(1, window=Window(c - 1, r - 1, 3, 3), masked=True).astype(np.float64).filled(np.nan)
        rx, ry = abs(src.transform.a), abs(src.transform.e)
    if not np.isfinite(z).all():
        return None
    return _slope_aspect(z, rx, ry)


def _slope_aspect(z: np.ndarray, rx: float, ry: float) -> tuple[float, float]:
    """Horn's slope and aspect of the centre of a 3 x 3 window (row 0 to the north)."""
    dzdx = ((z[0, 2] + 2 * z[1, 2] + z[2, 2]) - (z[0, 0] + 2 * z[1, 0] + z[2, 0])) / (8 * rx)
    dzdy = ((z[0, 0] + 2 * z[0, 1] + z[0, 2]) - (z[2, 0] + 2 * z[2, 1] + z[2, 2])) / (8 * ry)  # northwards
    slope = float(np.degrees(np.arctan(np.hypot(dzdx, dzdy))))
    aspect = float((np.degrees(np.arctan2(-dzdx, -dzdy)) + 360.0) % 360.0) if slope > 1e-6 else -1.0
    return slope, aspect


def aspect_raster(dem_path: str | Path, out_path: str | Path, crs: str | None = None) -> Path:
    """Aspect of every cell of the DEM (degrees clockwise from north, downhill; -1 where flat)."""
    from ..engine.dem import NODATA, read_dem, write_geotiff

    z, tr, _, res = read_dem(Path(dem_path))
    gy, gx = np.gradient(z, res, res)  # gy: towards the south (rows go down)
    dzdx, dzdy = gx, -gy
    aspect = (np.degrees(np.arctan2(-dzdx, -dzdy)) + 360.0) % 360.0
    flat = np.hypot(dzdx, dzdy) < 1e-6
    out = np.where(flat, -1.0, aspect)
    out = np.where(np.isfinite(z), out, NODATA).astype(np.float32)
    return write_geotiff(Path(out_path), out, (tr.c, tr.f), res, crs, nodata=NODATA)


def ground_surface(xyz: np.ndarray, classes: np.ndarray, cell: float | None = None, ground=(GROUND,)):
    """Ground on a grid from the points of the ``ground`` classes: the median of each cell (a
    few points of vegetation taken for ground do not move it), the empty cells interpolated.
    Returns ``(z (h, w), x0, y0, cell)``, row 0 at ``y0`` (south)."""
    from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator

    sel = np.isin(classes, list(ground))
    pts = np.asarray(xyz, float)[sel]
    if len(pts) < 10:
        raise ValueError("too few ground points: classify the cloud first (class 2)")
    lo, hi = pts[:, :2].min(0), pts[:, :2].max(0)
    if cell is None:
        cell = float(max(np.sqrt(np.prod(np.maximum(hi - lo, 1e-9)) / len(pts)) * 3, 1e-4))
    w, h = int((hi[0] - lo[0]) / cell) + 1, int((hi[1] - lo[1]) / cell) + 1
    col = ((pts[:, 0] - lo[0]) / cell).astype(int)
    row = ((pts[:, 1] - lo[1]) / cell).astype(int)
    idx = row * w + col
    order = np.lexsort((pts[:, 2], idx))
    cells, start, count = np.unique(idx[order], return_index=True, return_counts=True)
    z = np.full(h * w, np.inf)
    z[cells] = pts[order[start + count // 2], 2]
    z = z.reshape(h, w)
    known = np.isfinite(z)
    if not known.all():
        rr, cc = np.nonzero(known)
        inter = LinearNDInterpolator(np.column_stack([cc, rr]), z[known])
        hr, hc = np.nonzero(~known)
        fill = inter(hc, hr)
        miss = ~np.isfinite(fill)
        if miss.any():
            fill[miss] = NearestNDInterpolator(np.column_stack([cc, rr]), z[known])(hc[miss], hr[miss])
        z[~known] = fill
    return z, float(lo[0]), float(lo[1]), cell


def height_above_ground(xyz: np.ndarray, classes: np.ndarray, cell: float | None = None, ground=(GROUND,)):
    """Height of every point above the ground of :func:`ground_surface` (metres; bilinear
    between the cells, the nearest cell outside the ground grid)."""
    z, x0, y0, cell = ground_surface(xyz, classes, cell, ground)
    h, w = z.shape
    p = np.asarray(xyz, float)
    fx = np.clip((p[:, 0] - x0) / cell - 0.5, 0, w - 1)
    fy = np.clip((p[:, 1] - y0) / cell - 0.5, 0, h - 1)
    c0, r0 = np.floor(fx).astype(int), np.floor(fy).astype(int)
    c1, r1 = np.minimum(c0 + 1, w - 1), np.minimum(r0 + 1, h - 1)
    tx, ty = fx - c0, fy - r0
    g = (z[r0, c0] * (1 - tx) * (1 - ty) + z[r0, c1] * tx * (1 - ty) + z[r1, c0] * (1 - tx) * ty
         + z[r1, c1] * tx * ty)
    return p[:, 2] - g


def ground_dem(chunk) -> Path | None:
    """A DEM of the ground (DTM) of the chunk: a kept version of the DEM built from the ground
    class only, else None."""
    from ..core import versions

    for e in reversed(versions.versions(chunk, "dem")):
        classes = (e.get("params") or {}).get("classes")
        if classes and set(int(c) for c in classes) <= {GROUND}:
            path = versions.version_file(chunk, "dem", e["id"])
            if path is not None and Path(path).exists():
                return Path(path)
    active = (chunk.meta.get("dem") or {}).get("classes")
    if active and set(int(c) for c in active) <= {GROUND} and chunk.dem_path is not None:
        return chunk.dem_path
    return None


def ground_dem_at(path: str | Path, x: float, y: float) -> float | None:
    import rasterio

    with rasterio.open(path) as src:
        col, row = ~src.transform @ (x, y)
        if not (0 <= col < src.width and 0 <= row < src.height):
            return None
        v = src.read(1, window=((int(row), int(row) + 1), (int(col), int(col) + 1)), masked=True)
    return None if np.ma.is_masked(v[0, 0]) else float(v[0, 0])
