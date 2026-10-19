"""Field check: what is missing before leaving the site, right after the alignment.

* coverage: on a grid over the area of the tie points (the middle 98 %), at the height of the
  surface there (the DEM when there is one, else the median of the tie points of the cell, the
  empty cells interpolated), how many aligned photos frame each cell; cells seen by fewer than
  ``min_photos`` (three: two give a point, a third checks it) are the gaps to photograph again.
  A GeoTIFF of counts, the product ``coverage``;
* photos not aligned, and blurred ones (:mod:`.quality`, estimated if not done yet);
* markers measured in fewer than two photos (no position from the photos) or in two only (no
  check of their measurements), and markers with a reference never found in the photos.

The surface is not used as an occluder: a cell behind a wall counts as seen (fast, and on open
ground the same). The report goes to ``chunk.meta["field_check"]``.
"""
from __future__ import annotations

import datetime

import numpy as np

from ..core import crs as crs_mod
from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress

MIN_PHOTOS = 3
CELLS = 400  # cells along the longer side by default


def _surface(chunk: Chunk, gx: np.ndarray, gy: np.ndarray, xyz: np.ndarray, bounds) -> np.ndarray:
    """Heights (world) of the grid cells: from the DEM when there is one, else the median of
    the tie points in each cell, the empty cells interpolated inside the area of the points
    (NaN outside). ``bounds``: left, bottom, right, top of the grid."""
    from scipy.interpolate import LinearNDInterpolator
    from scipy.stats import binned_statistic_2d

    h, w = gx.shape
    dem = chunk.dem_path
    if dem is not None and dem.exists():
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.windows import from_bounds

        with rasterio.open(dem) as src:
            window = from_bounds(*bounds, transform=src.transform)
            z = src.read(1, window=window, out_shape=(h, w), boundless=True, masked=True,
                         resampling=Resampling.average).astype(np.float64).filled(np.nan)
        if np.isfinite(z).mean() > 0.2:
            return z
    step_x, step_y = gx[0, 1] - gx[0, 0], gy[1, 0] - gy[0, 0]
    xe = np.r_[gx[0] - step_x / 2, gx[0, -1] + step_x / 2]
    ye = np.r_[gy[:, 0] - step_y / 2, gy[-1, 0] + step_y / 2]
    flip_y = ye[0] > ye[-1]
    stat = binned_statistic_2d(xyz[:, 0], xyz[:, 1], xyz[:, 2], "median",
                               bins=[xe, ye[::-1] if flip_y else ye]).statistic.T
    z = stat[::-1] if flip_y else stat
    known = np.isfinite(z)
    if known.sum() >= 4 and not known.all():
        fill = LinearNDInterpolator(np.column_stack([gx[known], gy[known]]), z[known])(gx[~known], gy[~known])
        z[~known] = fill
    return z


def coverage(chunk: Chunk, resolution: float | None = None, progress: Progress | None = None):
    """Count of aligned photos framing every cell of a grid over the tie points (see the module
    docstring). Returns ``(counts (h, w) int, transform, heights)``; counts -1 outside the area."""
    from rasterio.transform import from_origin

    from .visibility import project_valid

    progress = as_progress(progress)
    tp = chunk.tie_points
    if tp is None or len(tp) < 10:
        raise RuntimeError("align the photos first")
    xyz = chunk.to_world(np.asarray(tp.xyz, float))
    lo, hi = np.percentile(xyz[:, :2], 1, axis=0), np.percentile(xyz[:, :2], 99, axis=0)
    span = hi - lo
    res = float(resolution) if resolution else float(max(span.max() / CELLS, 1e-6))
    lo, hi = lo - 2 * res, hi + 2 * res
    w, h = max(1, int(np.ceil((hi[0] - lo[0]) / res))), max(1, int(np.ceil((hi[1] - lo[1]) / res)))
    transform = from_origin(lo[0], hi[1], res, res)  # rows go down from the top edge
    cols, rows = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    gx, gy = lo[0] + cols * res, hi[1] - rows * res
    z = _surface(chunk, gx, gy, xyz, (lo[0], lo[1], lo[0] + w * res, hi[1]))
    inside = np.isfinite(z)
    pts = chunk.to_internal(np.column_stack([gx[inside], gy[inside], z[inside]]))
    counts = np.zeros(int(inside.sum()), np.int32)
    cams = chunk.aligned_cameras
    for i, cam in enumerate(cams):
        progress.check()
        _px, _depth, ok = project_valid(chunk, cam, pts, margin=0.0)
        counts += np.asarray(ok, bool)
        if i % 20 == 0:
            progress(0.1 + 0.85 * (i + 1) / len(cams), f"Coverage: photo {i + 1}/{len(cams)}")
    out = np.full((h, w), -1, np.int32)
    out[inside] = counts
    return out, transform, z


def field_check(chunk: Chunk, min_photos: int = MIN_PHOTOS, resolution: float | None = None,
                quality: bool = True, progress: Progress | None = None) -> dict:
    """The checks of the module docstring; writes the coverage raster (product ``coverage``) and
    returns the report (also in ``chunk.meta["field_check"]``)."""
    import rasterio

    from ..markers import gcp

    progress = as_progress(progress)
    report: dict = {"date": datetime.datetime.now().isoformat(timespec="seconds"), "min_photos": int(min_photos)}
    enabled = [c for c in chunk.cameras if c.enabled]
    report["photos"] = len(enabled)
    report["unaligned"] = [c.label for c in enabled if not c.aligned]
    if quality:
        from .quality import THRESHOLD, estimate_quality

        missing = [c for c in enabled if c.meta.get("quality") is None]
        if missing:
            estimate_quality(chunk, missing, progress=progress.sub(0.0, 0.3))
        report["blurred"] = [c.label for c in enabled if (c.meta.get("quality") or 1.0) < THRESHOLD]
    markers = []
    for m in chunk.markers:
        views = gcp.views_of(chunk, m)
        status = ("not found" if views == 0 and m.reference.location else "one photo" if views == 1
                  else "two photos" if views == 2 else None)
        if status:
            markers.append({"label": m.label, "views": views, "status": status,
                            "reference": m.reference.location is not None})
    report["markers"] = markers
    tp = chunk.tie_points
    conf = getattr(tp, "confidence", None) if tp is not None else None
    if conf is not None and len(conf):
        report["tie_points_weak"] = float(np.mean(np.asarray(conf) < min_photos))
    if chunk.aligned_cameras and tp is not None and len(tp) >= 10:
        counts, transform, _z = coverage(chunk, resolution, progress=progress.sub(0.3, 0.65))
        area = counts >= 0
        weak = area & (counts < min_photos)
        folder = chunk.data_dir / "fieldcheck"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "coverage.tif"
        with rasterio.open(path, "w", driver="GTiff", width=counts.shape[1], height=counts.shape[0], count=1,
                           dtype="int16", crs=None if crs_mod.is_local(chunk.crs) else chunk.crs, transform=transform,
                           nodata=-1, compress="deflate") as dst:
            dst.write(np.clip(counts, -1, 32767).astype(np.int16), 1)
        chunk._set_product("coverage", path)
        res = abs(transform.a)
        report.update({"resolution": res, "cells": int(area.sum()), "weak_cells": int(weak.sum()),
                       "weak_fraction": float(weak.sum() / max(area.sum(), 1)),
                       "weak_area": float(weak.sum() * res * res),
                       "median_photos": float(np.median(counts[area])) if area.any() else 0.0})
    chunk.meta["field_check"] = report
    progress(1.0, "Field check done")
    return report
