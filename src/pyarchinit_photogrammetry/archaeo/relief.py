"""Relief visualisations of a DEM: the faint walls, ditches, terraces and tracks that colours and
a single hillshade hide.

* ``svf`` - sky-view factor (Zakšek et al. 2011): the share of the sky seen from every cell,
  ``1 - mean(sin(horizon angle))`` over ``directions``, the horizon searched up to ``radius``
  metres (negative angles count as flat). 1 on open ground, lower in ditches and at the foot of
  walls; it does not depend on the direction of the light.
* ``openness_pos`` / ``openness_neg`` - positive and negative openness (Yokoyama et al. 2002):
  the mean over the directions of the zenith angle of the horizon (``90° - angle``), in degrees;
  the negative one on the DEM turned upside down. Ridges and wall tops stand out in the
  positive, ditches and pits in the negative.
* ``slrm`` - simple local relief model: the DEM minus its mean over a square of side
  ``2 radius``, in metres (the trend of the land removed: what stands out locally, positive or
  negative).
* ``msrm`` - multi-scale relief model (Orengo and Petrie 2018): the mean difference between
  successive low-pass filtered DEMs whose radii grow as ``k ** scaling``, for features from
  ``min_size`` to ``max_size`` metres, in metres: features of all those sizes at once.
* ``multi_hillshade`` - mean of the hillshades lit from ``directions`` azimuths (altitude 45°),
  0 to 1: no direction hides a feature parallel to the light.

The DEM is processed by tiles of ``tile`` cells with the margin the method needs (the result is
the same as on the whole DEM), several at a time, and written as a float32 GeoTIFF with the
georeference of the DEM (no data where the DEM has none).
"""
from __future__ import annotations

import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from ..core.progress import Progress, as_progress
from ..engine.dem import NODATA

METHODS = ("svf", "openness_pos", "openness_neg", "slrm", "msrm", "multi_hillshade")
MAX_RADIUS_CELLS = 400  # a horizon searched farther takes minutes even on a small DEM


def default_radius(resolution: float) -> float:
    """A search radius of about 20 cells, rounded: 1 m on a 5 cm drone DEM, 10 m on a 50 cm one."""
    r = 20 * resolution
    mag = 10 ** math.floor(math.log10(r))
    return float(min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= r - 1e-12), default=r))


def _cells(radius: float, resolution: float) -> int:
    return int(min(max(round(radius / resolution), 1), MAX_RADIUS_CELLS))


def _shifted(z: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """``z[r + dy, c + dx]`` for every cell, NaN outside."""
    out = np.full_like(z, np.nan)
    h, w = z.shape
    r0, r1, c0, c1 = max(0, -dy), min(h, h - dy), max(0, -dx), min(w, w - dx)
    if r1 > r0 and c1 > c0:
        out[r0:r1, c0:c1] = z[r0 + dy:r1 + dy, c0 + dx:c1 + dx]
    return out


def _steps(azimuth: float, radius_cells: int) -> list[tuple[int, int, float]]:
    """(row, column, distance in cells) of the cells met going ``radius_cells`` away towards
    ``azimuth`` (radians clockwise from north; rows grow towards the south)."""
    out, seen = [], set()
    sx, sy = math.sin(azimuth), -math.cos(azimuth)
    for s in range(1, radius_cells + 1):
        dx, dy = int(round(s * sx)), int(round(s * sy))
        if (dx, dy) in seen or (dx, dy) == (0, 0):
            continue
        seen.add((dx, dy))
        out.append((dy, dx, math.hypot(dx, dy)))
    return out


def horizon_angles(z: np.ndarray, resolution: float, radius_cells: int, directions: int = 16):
    """For every direction (clockwise from north) the elevation angle in radians of the highest
    point met within the radius, cell by cell (NaN where the direction leaves the DEM at once)."""
    for k in range(directions):
        best = np.full(z.shape, np.nan)
        for dy, dx, dist in _steps(2 * math.pi * k / directions, radius_cells):
            with np.errstate(invalid="ignore"):
                np.fmax(best, (_shifted(z, dy, dx) - z) / (dist * resolution), out=best)
        yield np.arctan(best)


def _mean_of(values) -> np.ndarray:
    """Mean of the arrays of ``values`` ignoring NaN (NaN where none is valid)."""
    total = count = None
    for v in values:
        ok = np.isfinite(v)
        total = np.where(ok, v, 0.0) if total is None else total + np.where(ok, v, 0.0)
        count = ok.astype(np.int32) if count is None else count + ok
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, total / np.maximum(count, 1), np.nan)


def mean_filter(z: np.ndarray, radius_cells: int) -> np.ndarray:
    """Mean of the valid cells in a square of side ``2 radius + 1`` (NaN where none)."""
    from scipy.ndimage import uniform_filter

    valid = np.isfinite(z)
    size = 2 * int(radius_cells) + 1
    s = uniform_filter(np.where(valid, z, 0.0), size, mode="constant")
    n = uniform_filter(valid.astype(np.float64), size, mode="constant")
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 1e-9, s / n, np.nan)


def _msrm_sizes(resolution: float, radius: float, min_size: float | None, max_size: float | None):
    """Smallest and largest feature of the multi-scale relief model (defaults: 4 cells and the
    diameter of the search radius)."""
    return float(min_size or 4 * resolution), float(max_size or 2 * radius)


def msrm_radii(resolution: float, min_size: float, max_size: float, scaling: float = 2.0) -> list[int]:
    """Radii in cells of the filters of the multi-scale relief model: ``k ** scaling`` for the
    integers ``k`` between the features of ``min_size`` and ``max_size`` metres."""
    def k_of(size):
        return max((size / resolution - 1) / 2, 0.0) ** (1.0 / scaling)

    k0, k1 = max(int(math.floor(k_of(min_size))), 1), int(math.ceil(k_of(max_size)))
    radii = sorted({min(max(1, int(round(k ** scaling))), MAX_RADIUS_CELLS) for k in range(k0, max(k1, k0 + 1) + 1)})
    return radii if len(radii) >= 2 else [radii[0], radii[0] + 1]


def _hillshade(z: np.ndarray, resolution: float, azimuth: float, altitude: float = 45.0) -> np.ndarray:
    gy, gx = np.gradient(z, resolution, resolution)  # gy: towards the south
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(gy, -gx)
    az, alt = np.radians(360 - azimuth + 90), np.radians(altitude)
    return np.clip(np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect), 0, 1)


def compute(z: np.ndarray, resolution: float, method: str, radius: float | None = None, directions: int = 16,
            min_size: float | None = None, max_size: float | None = None, scaling: float = 2.0) -> np.ndarray:
    """The visualisation ``method`` of a DEM array (NaN = no data); see the module docstring."""
    if method not in METHODS:
        raise ValueError(f"unknown relief visualisation {method!r} ({', '.join(METHODS)})")
    valid = np.isfinite(z)
    radius = float(radius or default_radius(resolution))
    cells = _cells(radius, resolution)
    if method == "svf":  # negative angles count as flat
        out = 1.0 - _mean_of(np.sin(np.clip(a, 0.0, None)) for a in horizon_angles(z, resolution, cells, directions))
    elif method in ("openness_pos", "openness_neg"):
        zz = z if method == "openness_pos" else -z
        out = _mean_of(90.0 - np.degrees(a) for a in horizon_angles(zz, resolution, cells, directions))
    elif method == "slrm":
        out = z - mean_filter(z, cells)
    elif method == "msrm":  # the mean of the differences of successive low-pass levels: the sum telescopes
        radii = msrm_radii(resolution, *_msrm_sizes(resolution, radius, min_size, max_size), scaling)
        out = (mean_filter(z, radii[0]) - mean_filter(z, radii[-1])) / (len(radii) - 1)
    else:
        out = _mean_of(_hillshade(z, resolution, a) for a in np.arange(directions) * 360.0 / directions)
    return np.where(valid, out, np.nan)


def margin_cells(method: str, resolution: float, radius: float | None = None, min_size: float | None = None,
                 max_size: float | None = None, scaling: float = 2.0) -> int:
    """Cells around a tile that the method reads (the tile then comes out as on the whole DEM)."""
    radius = float(radius or default_radius(resolution))
    if method == "msrm":
        return max(msrm_radii(resolution, *_msrm_sizes(resolution, radius, min_size, max_size), scaling)) + 1
    if method == "multi_hillshade":
        return 2
    return _cells(radius, resolution) + 1


def build_relief(dem_path: str | os.PathLike, out_path: str | os.PathLike, method: str, radius: float | None = None,
                 directions: int = 16, min_size: float | None = None, max_size: float | None = None,
                 scaling: float = 2.0, crs: str | None = None, tile: int = 1024, workers: int | None = None,
                 progress: Progress | None = None) -> dict:
    """``method`` of the DEM in ``dem_path`` written to ``out_path`` (see the module docstring).
    Returns the path, the method, its parameters and the 2nd and 98th percentiles of the values
    (the stretch of the colours)."""
    import rasterio
    from rasterio.windows import Window

    if method not in METHODS:
        raise ValueError(f"unknown relief visualisation {method!r} ({', '.join(METHODS)})")
    progress = as_progress(progress)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    sample = []
    with rasterio.open(dem_path) as src:
        res = abs(src.transform.a)
        radius = float(radius or default_radius(res))
        margin = margin_cells(method, res, radius, min_size, max_size, scaling)
        profile = {"driver": "GTiff", "height": src.height, "width": src.width, "count": 1, "dtype": "float32",
                   "transform": src.transform, "crs": src.crs or crs, "nodata": NODATA, "compress": "deflate",
                   "predictor": 3, "BIGTIFF": "IF_SAFER"}
        if src.width >= 256 and src.height >= 256:
            profile.update(tiled=True, blockxsize=256, blockysize=256)
        nodata = src.nodata
        tiles = [(c, r, min(tile, src.width - c), min(tile, src.height - r))
                 for r in range(0, src.height, tile) for c in range(0, src.width, tile)]

        def work(t):
            c, r, w, h = t
            c0, r0 = max(0, c - margin), max(0, r - margin)
            c1, r1 = min(src.width, c + w + margin), min(src.height, r + h + margin)
            with lock:  # a rasterio dataset is not for several threads at once
                z = src.read(1, window=Window(c0, r0, c1 - c0, r1 - r0)).astype(np.float64)
            if nodata is not None:
                z[z == nodata] = np.nan
            if not np.isfinite(z).any():
                return t, None
            out = compute(z, res, method, radius, directions, min_size, max_size, scaling)
            return t, out[r - r0:r - r0 + h, c - c0:c - c0 + w]

        with rasterio.open(out_path, "w", **profile) as dst, \
                ThreadPoolExecutor(max(1, workers or min(8, os.cpu_count() or 1))) as ex:
            for i, ((c, r, w, h), out) in enumerate(ex.map(work, tiles)):
                progress.check()
                if out is None:
                    block = np.full((h, w), NODATA, np.float32)
                else:
                    block = np.where(np.isfinite(out), out, NODATA).astype(np.float32)
                    v = out[np.isfinite(out)]
                    if v.size:
                        sample.append(v[::max(1, v.size // 20000)])
                dst.write(block, 1, window=Window(c, r, w, h))
                progress((i + 1) / len(tiles), f"Relief ({method}): tile {i + 1}/{len(tiles)}")
    values = np.concatenate(sample) if sample else np.zeros(1)
    lo, hi = (float(x) for x in np.percentile(values, [2, 98]))
    return {"path": out_path, "method": method, "radius_m": radius, "directions": int(directions),
            "min_size_m": min_size, "max_size_m": max_size, "scaling": scaling, "p2": lo, "p98": hi}
