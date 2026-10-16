"""Orthomosaic generation (GeoTIFF RGBA in the chunk CRS).

Every ground cell is lifted to the surface (DEM or mesh), projected into the
photos, and coloured from the best photo that really sees it (occlusion test
against the mesh when one exists): ``mosaic`` keeps one photo per cell
(sharp, like a true orthophoto), ``average`` blends all valid photos.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress
from .dem import NODATA, ensure_level, estimate_gsd, read_dem, write_geotiff
from .visibility import ImageCache, project_valid, raycaster_for_chunk, readable_cameras, sample


def _surface_grid(chunk: Chunk, resolution: float, surface: str):
    """Elevation grid at ``resolution`` (NaN = no surface) and its origin."""
    if surface == "model" or chunk.dem_path is None:
        from .dem import grid_points, surface_points_world

        pts = surface_points_world(chunk, "model" if chunk.model is not None else "point_cloud")
        z, origin, _ = grid_points(pts, resolution, reducer="max")
        from rasterio.fill import fillnodata

        mask = np.isfinite(z).astype(np.uint8)
        filled = fillnodata(np.nan_to_num(z).astype(np.float32), mask=mask, max_search_distance=10)
        z = np.where(mask > 0, z, np.where(filled != 0, filled, np.nan))
        return z, origin
    import cv2

    dem, transform, _, dem_res = read_dem(chunk.dem_path)
    origin = (transform.c, transform.f)
    h = int(np.ceil(dem.shape[0] * dem_res / resolution))
    w = int(np.ceil(dem.shape[1] * dem_res / resolution))
    # ortho pixel centre -> fractional DEM pixel index (pixel centres on integers)
    cols = ((np.arange(w) + 0.5) * resolution / dem_res - 0.5).astype(np.float32)
    rows = ((np.arange(h) + 0.5) * resolution / dem_res - 0.5).astype(np.float32)
    mx, my = np.meshgrid(cols, rows)
    valid = np.isfinite(dem).astype(np.float32)
    filled = np.nan_to_num(dem).astype(np.float32)
    num = cv2.remap(filled * valid, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    den = cv2.remap(valid, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = np.where(den > 0.5, num / den, np.nan)
    return z.astype(np.float64), origin


def hidden_cells(chunk: Chunk, origin, shape, resolution: float, hide_classes) -> np.ndarray:
    """Cells of a grid (``origin`` = top-left corner, ``shape``, ``resolution``) whose visible
    surface - the highest point of the dense cloud there - belongs to ``hide_classes``.

    The classes are gridded at the point spacing (or the pixel, when coarser), widened by one
    cell so that the edges of a hidden object go too; cells without points stay visible."""
    from scipy import ndimage

    from ..core.pointcloud import NOISE_CLASSES

    pc = chunk.point_cloud
    if pc is None:
        raise RuntimeError("the classes come from the dense cloud: build it first")
    keep = pc.class_mask(exclude=NOISE_CLASSES)
    xyz = chunk.to_world(pc.xyz[keep])
    codes = pc.classification[keep]
    h, w = shape
    spacing = (chunk.meta.get("dem") or {}).get("resolution") or 0.0
    if not spacing:
        area = max(np.ptp(xyz[:, 0]) * np.ptp(xyz[:, 1]), 1e-12)
        spacing = 1.5 * np.sqrt(area / max(len(xyz), 1))
    cs = max(float(spacing), resolution)
    hc, wc = int(np.ceil(h * resolution / cs)) + 1, int(np.ceil(w * resolution / cs)) + 1
    col = np.floor((xyz[:, 0] - origin[0]) / cs).astype(np.int64)
    row = np.floor((origin[1] - xyz[:, 1]) / cs).astype(np.int64)
    ok = (col >= 0) & (col < wc) & (row >= 0) & (row < hc)
    idx, z, codes = row[ok] * wc + col[ok], xyz[ok, 2], codes[ok]
    top = np.full(hc * wc, -np.inf)
    np.maximum.at(top, idx, z)
    grid = np.zeros(hc * wc, np.int16) - 1  # -1: no point
    first = z >= top[idx]
    grid[idx[first]] = codes[first]
    hidden = np.isin(grid, np.asarray([int(c) for c in hide_classes])).reshape(hc, wc)
    hidden = ndimage.binary_dilation(hidden)
    rows = np.minimum(((np.arange(h) + 0.5) * resolution / cs).astype(np.int64), hc - 1)
    cols = np.minimum(((np.arange(w) + 0.5) * resolution / cs).astype(np.int64), wc - 1)
    return hidden[np.ix_(rows, cols)]


def build_orthomosaic(chunk: Chunk, resolution: float | None = None, surface: str = "dem", blending: str = "mosaic",
                      hide_classes=None, progress: Progress | None = None, tile: int = 512,
                      skip_unreadable: bool = False) -> Path:
    """``hide_classes``: classes of the dense cloud left out of the orthomosaic (transparent
    where they are the visible surface: excavation tools, people, vegetation). Photos that
    cannot be read (e.g. kept online only by Dropbox) stop the step
    (:class:`~.visibility.UnreadablePhotos`) unless ``skip_unreadable``."""
    progress = progress or Progress()
    progress(0.0, "Checking the photos")
    cams, bad = readable_cameras(chunk, skip_unreadable)
    ensure_level(chunk)
    if resolution is None:
        resolution = estimate_gsd(chunk)
    progress(0.02, f"Orthomosaic at {resolution:.4g} m/px from {len(cams)} photos")
    z, origin = _surface_grid(chunk, resolution, surface)
    h, w = z.shape
    rgba = np.zeros((4, h, w), np.uint8)
    raycaster = raycaster_for_chunk(chunk)
    cache = ImageCache(chunk, max_items=4)
    inv = np.linalg.inv(chunk.transform)
    centers = {c.key: c.center for c in cams}
    abs_tol = 3.0 * resolution / chunk.scale  # occlusion tolerance in internal units
    up_int = inv[:3, :3] @ np.array([0.0, 0.0, 1.0])
    up_int /= np.linalg.norm(up_int)
    tiles = [(r, c) for r in range(0, h, tile) for c in range(0, w, tile)]
    for t_i, (r0, c0) in enumerate(tiles):
        zz = z[r0:r0 + tile, c0:c0 + tile]
        valid = np.isfinite(zz)
        if not valid.any():
            continue
        rr, cc = np.nonzero(valid)
        X = origin[0] + (c0 + cc + 0.5) * resolution
        Y = origin[1] - (r0 + rr + 0.5) * resolution
        world = np.column_stack([X, Y, zz[rr, cc]])
        pts = world @ inv[:3, :3].T + inv[:3, 3]
        n = len(pts)
        # only the photos that see this tile (probe a sparse subset of its cells)
        probe = pts[:: max(1, n // 400)]
        tcams = [c for c in cams if project_valid(chunk, c, probe, margin=-0.05 * chunk.sensor(c).width)[2].any()]
        if not tcams:
            continue
        scores = np.full((len(tcams), n), -np.inf, np.float32)
        pix = np.full((len(tcams), n, 2), np.nan, np.float32)
        for k, cam in enumerate(tcams):
            px, depth, ok = project_valid(chunk, cam, pts)
            if not ok.any():
                continue
            sensor = chunk.sensor(cam)
            ray = pts[ok] - centers[cam.key]
            ray /= np.linalg.norm(ray, axis=1, keepdims=True)
            nadir = np.clip(-(ray @ up_int), 0, 1)  # 1 when looking straight down onto the surface
            off = np.hypot((px[ok, 0] / sensor.width - 0.5), (px[ok, 1] / sensor.height - 0.5))
            gsd = depth[ok] / sensor.focal_px
            scores[k, ok] = (nadir ** 2) * (1 - off) / np.maximum(gsd, 1e-12)
            pix[k, ok] = px[ok]
        order = np.argsort(-scores, axis=0)
        colors = np.zeros((n, 3), np.float64)
        weight = np.zeros(n, np.float64)
        chosen = np.full(n, -1)
        if blending == "average":
            for k, cam in enumerate(tcams):
                ok = np.isfinite(scores[k])
                if raycaster is not None and ok.any():
                    vis = raycaster.visible_from(centers[cam.key], pts[ok], abs_tol=abs_tol)
                    idx = np.nonzero(ok)[0]
                    ok[idx[~vis]] = False
                if ok.any():
                    wgt = scores[k, ok].astype(np.float64)
                    colors[ok] += sample(cache.get(cam), pix[k, ok]) * wgt[:, None]
                    weight[ok] += wgt
            got = weight > 0
            colors[got] /= weight[got, None]
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
                    if raycaster is not None:
                        vis = raycaster.visible_from(centers[tcams[k].key], pts[sel], abs_tol=abs_tol)
                        still.append(sel[~vis])
                        sel = sel[vis]
                    chosen[sel] = k
                pending = np.concatenate(still) if still else np.zeros(0, int)
            # cells hidden in every candidate photo (steep faces): take the best photo anyway
            fallback = (chosen < 0) & np.isfinite(scores.max(axis=0))
            chosen[fallback] = order[0, fallback]
            for k in np.unique(chosen[chosen >= 0]):
                sel = np.nonzero(chosen == k)[0]
                colors[sel] = sample(cache.get(tcams[k]), pix[k, sel])
            got = chosen >= 0
        tile_rgba = rgba[:, r0:r0 + tile, c0:c0 + tile]
        bgr = np.clip(colors[got], 0, 255).astype(np.uint8)
        tile_rgba[0, rr[got], cc[got]] = bgr[:, 2]
        tile_rgba[1, rr[got], cc[got]] = bgr[:, 1]
        tile_rgba[2, rr[got], cc[got]] = bgr[:, 0]
        tile_rgba[3, rr[got], cc[got]] = 255
        progress(0.05 + 0.9 * (t_i + 1) / len(tiles), f"Orthomosaic tile {t_i + 1}/{len(tiles)}")
    hidden_classes = sorted({int(c) for c in hide_classes}) if hide_classes else None
    if hidden_classes:
        progress(0.96, "Hiding the chosen classes")
        rgba[:, hidden_cells(chunk, origin, (h, w), resolution, hidden_classes)] = 0
    path = write_geotiff(chunk.data_dir / "orthomosaic.tif", rgba, origin, resolution, chunk.crs,
                         photometric="RGB")
    chunk._set_product("orthomosaic", path)
    chunk.meta["orthomosaic"] = {"resolution": resolution, "surface": surface, "blending": blending,
                                 "hidden_classes": hidden_classes}
    if bad:
        chunk.meta["orthomosaic"]["skipped_photos"] = [c.label for c in bad]
    progress(1.0, f"Orthomosaic {w}x{h} px")
    return path


__all__ = ["build_orthomosaic", "hidden_cells", "NODATA"]
