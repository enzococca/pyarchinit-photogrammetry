"""Editing the DEM: an area filled again from the ground around it (Metashape's *Edit DEM*).

Trees, tents, vehicles, spoil heaps or a hole of the reconstruction spoil contour lines and volumes:
:func:`fill_area` gives the cells inside a polygon new heights from the cells just outside it -
a constant height, the plane that fits them best, inverse-distance weighting or a linear
interpolation over their triangulation - optionally honouring breaklines (the edge of a wall,
of a step: their heights are kept and interpolated along them). The DEM in use is kept first as a
version (:mod:`..core.versions`), so the edit can be compared and undone; the edited DEM is
written in the chunk folder (never over a file of another program), with its overviews.
"""
from __future__ import annotations

import datetime

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress

METHODS = ("plane", "constant", "idw", "linear")
RING = 3  # cells around the area whose heights are the known ones


def _cells(transform, rows, cols):
    x = transform.c + (cols + 0.5) * transform.a
    y = transform.f + (rows + 0.5) * transform.e
    return x, y


def _densify(line: np.ndarray, step: float) -> np.ndarray:
    out = [line[0]]
    for a, b in zip(line[:-1], line[1:]):
        n = max(1, int(np.ceil(np.linalg.norm(b[:2] - a[:2]) / step)))
        t = np.linspace(0, 1, n + 1)[1:, None]
        out.extend(a + t * (b - a))
    return np.asarray(out)


def new_heights(known: np.ndarray, target: np.ndarray, method: str, value: float | None = None,
                power: float = 2.0, neighbours: int = 16) -> np.ndarray:
    """Heights at ``target`` (n, 2) from ``known`` (m, 3: x, y, z) by ``method`` (:data:`METHODS`)."""
    if method not in METHODS:
        raise ValueError(f"method: one of {', '.join(METHODS)}")
    if method == "constant":
        z = float(np.median(known[:, 2])) if value is None else float(value)
        return np.full(len(target), z)
    if len(known) < 3:
        raise ValueError("too few heights around the area: draw it on the DEM, with ground around it")
    if method == "plane":
        a = np.column_stack([known[:, 0] - known[:, 0].mean(), known[:, 1] - known[:, 1].mean(), np.ones(len(known))])
        coef = np.linalg.lstsq(a, known[:, 2], rcond=None)[0]
        return coef[0] * (target[:, 0] - known[:, 0].mean()) + coef[1] * (target[:, 1] - known[:, 1].mean()) + coef[2]
    from scipy.spatial import cKDTree

    tree = cKDTree(known[:, :2])
    k = min(neighbours, len(known))
    dist, idx = tree.query(target, k=k)
    dist, idx = np.atleast_2d(dist), np.atleast_2d(idx)
    w = 1.0 / np.maximum(dist, 1e-9) ** power
    idw = (w * known[idx, 2]).sum(axis=1) / w.sum(axis=1)
    if method == "idw":
        return idw
    from scipy.interpolate import LinearNDInterpolator

    z = LinearNDInterpolator(known[:, :2], known[:, 2])(target)
    return np.where(np.isfinite(z), z, idw)  # outside the triangulation of the known cells: IDW


def fill_area(chunk: Chunk, polygon, method: str = "plane", value: float | None = None, breaklines=None,
              keep_version: bool = True, version_name: str | None = None,
              progress: Progress | None = None) -> dict:
    """New heights for the DEM cells inside ``polygon`` (chunk CRS, (N, 2) or a shapely geometry)
    from the cells in a ring of :data:`RING` cells around it. ``breaklines``: lines (each (N, 2) or
    (N, 3); without z the heights of the DEM at their vertices) whose heights are kept along them.
    ``keep_version``: the DEM as it was is kept among the versions first (``version_name``: the
    name of the edited one). Returns ``{"cells", "method", "dz_mean", "dz_max", "version"}``."""
    import rasterio
    from rasterio.windows import Window
    from scipy import ndimage
    from shapely import contains_xy
    from shapely.geometry import Polygon

    from ..core import versions
    from .crop import as_polygon

    progress = as_progress(progress)
    src_path = chunk.dem_path
    if src_path is None or not src_path.exists():
        raise RuntimeError("build the DEM first")
    geom = as_polygon(polygon) if not hasattr(polygon, "geom_type") else polygon
    if isinstance(geom, Polygon) and not geom.is_valid:
        geom = geom.buffer(0)
    with rasterio.open(src_path) as src:
        t, nodata = src.transform, src.nodata
        res = abs(t.a)
        x0, y0, x1, y1 = geom.bounds
        c0 = max(int(np.floor((x0 - t.c) / t.a)) - RING - 2, 0)
        c1 = min(int(np.ceil((x1 - t.c) / t.a)) + RING + 2, src.width)
        r0 = max(int(np.floor((y1 - t.f) / t.e)) - RING - 2, 0)
        r1 = min(int(np.ceil((y0 - t.f) / t.e)) + RING + 2, src.height)
        if c1 <= c0 or r1 <= r0:
            raise ValueError("the area is outside the DEM")
        win = Window(c0, r0, c1 - c0, r1 - r0)
        z = src.read(1, window=win).astype(np.float64)
    valid = np.isfinite(z) & ((z != nodata) if nodata is not None and np.isfinite(nodata) else True)
    rows, cols = np.mgrid[r0:r1, c0:c1]
    x, y = _cells(t, rows, cols)
    inside = contains_xy(geom, x, y)
    if not inside.any():
        raise ValueError("the area does not cover any cell of the DEM")
    ring = ndimage.binary_dilation(inside, iterations=RING) & ~inside & valid
    known = np.column_stack([x[ring], y[ring], z[ring]])
    if breaklines:
        extra = []
        with rasterio.open(src_path) as src:
            for line in breaklines:
                ln = np.asarray(line, np.float64)
                if ln.shape[1] < 3:
                    zs = np.array([v[0] for v in src.sample([(px, py) for px, py in ln[:, :2]])], np.float64)
                    ln = np.column_stack([ln[:, :2], zs])
                ln = ln[np.isfinite(ln[:, 2]) & ((ln[:, 2] != nodata) if nodata is not None else True)]
                if len(ln) >= 2:
                    extra.append(_densify(ln, res))
        if extra:
            known = np.vstack([known] + extra)
    progress(0.3, f"DEM: {int(inside.sum()):,} cells, {len(known):,} heights around them")
    znew = new_heights(known, np.column_stack([x[inside], y[inside]]), method, value)
    z_out = z.copy()
    z_out[inside] = znew
    kept = None
    if keep_version:  # the DEM as it was stays among the versions, under its name
        when = datetime.datetime.now().strftime("%d/%m %H:%M")
        old_name = versions.active_info(chunk, "dem")["name"]
        kept = versions.keep_active(chunk, "dem", move=False)
        versions.rename(chunk, "dem", None, version_name or f"{old_name}, modificato {when}")
        src_path = chunk.dem_path
    own = chunk.own_product_path("dem")
    if own.resolve() != src_path.resolve():  # a DEM of another program: edited as a copy in the chunk
        import shutil

        tmp = own.with_name("dem.tmp.tif")
        shutil.copy2(src_path, tmp)
        tmp.replace(own)
        chunk._set_product("dem", own)
    out_z = np.where(np.isfinite(z_out), z_out, nodata if nodata is not None else np.nan).astype(np.float32)
    with rasterio.open(own, "r+") as dst:
        dst.write(out_z, 1, window=win)
        factors = dst.overviews(1)
    if factors:
        from . import overviews

        if not overviews.refresh(own, [(c0, r0, c1 - c0, r1 - r0)], factors):
            from rasterio.enums import Resampling

            with rasterio.open(own, "r+") as dst:
                dst.build_overviews(factors, Resampling.average)
    had = valid[inside]  # cells that had a height: how much they changed
    dz = znew[had] - z[inside][had]
    info = {"cells": int(inside.sum()), "method": method, "value": value,
            "dz_mean": float(np.mean(dz)) if len(dz) else 0.0, "dz_max": float(np.max(np.abs(dz))) if len(dz) else 0.0,
            "version": kept["id"] if kept else None}
    chunk.meta.setdefault("dem_edits", []).append({
        "polygon": np.asarray(geom.exterior.coords if hasattr(geom, "exterior") else [], float)[:, :2].tolist(),
        "method": method, "value": value, "breaklines": len(breaklines or []),
        "when": datetime.datetime.now().isoformat(timespec="seconds"), "cells": info["cells"]})
    progress(1.0, f"DEM: {info['cells']:,} cells filled ({method})")
    return info
