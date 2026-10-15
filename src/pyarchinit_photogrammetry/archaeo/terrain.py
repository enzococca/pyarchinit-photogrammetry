"""Terrain tools on DEMs: contour lines, cut/fill volumes, slope, hillshade."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..engine.dem import NODATA, read_dem, write_geotiff


def contours(dem_path: str | Path, interval: float, base: float = 0.0, index_every: int = 5) -> list[dict]:
    """Contour lines of a DEM. Returns ``[{"elevation", "index", "coords" (N,2) world}]``."""
    import contourpy

    z, tr, _, res = read_dem(Path(dem_path))
    lo, hi = np.nanmin(z), np.nanmax(z)
    gen = contourpy.contour_generator(z=np.ma.masked_invalid(z), line_type=contourpy.LineType.Separate)
    out = []
    first = np.ceil((lo - base) / interval) * interval + base
    for k, level in enumerate(np.arange(first, hi, interval)):
        n = int(round((level - base) / interval))
        for line in gen.lines(level):
            if len(line) < 2:
                continue
            col, row = line[:, 0], line[:, 1]
            x = tr.c + (col + 0.5) * tr.a
            y = tr.f + (row + 0.5) * tr.e
            out.append({"elevation": float(level), "index": n % index_every == 0, "coords": np.column_stack([x, y])})
    return out


def export_contours(lines: list[dict], path: str | Path, crs: str | None = None) -> Path:
    """Write contours as DXF (2.5D polylines with elevation) or GeoJSON."""
    path = Path(path)
    if path.suffix.lower() == ".dxf":
        import ezdxf

        doc = ezdxf.new("R2010")
        doc.layers.add("CURVE_DIRETTRICI", color=1)
        doc.layers.add("CURVE_INTERMEDIE", color=8)
        msp = doc.modelspace()
        for c in lines:
            layer = "CURVE_DIRETTRICI" if c["index"] else "CURVE_INTERMEDIE"
            msp.add_lwpolyline(c["coords"].tolist(), dxfattribs={"layer": layer, "elevation": c["elevation"]})
        doc.saveas(path)
        return path
    feats = [{"type": "Feature", "properties": {"elevation": c["elevation"], "index": c["index"]},
              "geometry": {"type": "LineString", "coordinates": c["coords"].round(4).tolist()}} for c in lines]
    fc = {"type": "FeatureCollection", "features": feats}
    if crs:
        fc["crs"] = {"type": "name", "properties": {"name": crs}}
    path.write_text(json.dumps(fc))
    return path


def volume(dem_path: str | Path, reference: str | Path | float | None = None, polygon=None,
           difference_path: str | Path | None = None) -> dict:
    """Excavated and filled volume between the current surface (``dem_path``) and a reference.

    ``reference``: the surface *before* the work (another DEM, resampled onto this grid)
    or a horizontal plane at a given elevation (the original ground level). Then
    ``cut_m3`` ("scavo") is the volume removed, where the current surface lies *below* the
    reference, and ``fill_m3`` ("riporto") the volume added, where it lies above.
    ``reference=None`` uses the lowest point inside the area as a floor: ``cut_m3`` is the
    deposit standing above it (what would be excavated to reach that level), fill is 0.

    ``polygon`` (shapely geometry in world coordinates) restricts the computation.
    ``above_m3``/``below_m3`` give the raw volumes of the surface above/below the reference.
    """
    z, tr, _, res = read_dem(Path(dem_path))
    weight = np.isfinite(z).astype(np.float64)
    if polygon is not None:
        weight *= _coverage(polygon, z.shape, tr)
    area_mask = weight > 0
    if not area_mask.any():
        raise ValueError("the polygon does not overlap the DEM")
    if reference is None:
        ref = np.full_like(z, np.nanmin(z[area_mask]))  # lowest point inside the area
    elif isinstance(reference, (int, float)):
        ref = np.full_like(z, float(reference))
    else:
        ref = _resample_to(Path(reference), z.shape, tr)
    mask = area_mask & np.isfinite(ref)
    diff = np.where(mask, z - ref, np.nan)
    w = np.where(mask, weight, 0.0)  # fraction of each cell inside the polygon
    cell = abs(tr.a * tr.e)
    above = float(np.nansum(np.clip(diff, 0, None) * w) * cell)
    below = float(-np.nansum(np.clip(diff, None, 0) * w) * cell)
    if difference_path:
        write_geotiff(Path(difference_path), np.where(mask, diff, NODATA).astype(np.float32), (tr.c, tr.f), res,
                      None, nodata=NODATA)
    if reference is None:  # deposit above the floor of the area
        cut, fill = above, 0.0
    else:  # the reference is the earlier surface: removed where we are now below it
        cut, fill = below, above
    return {"area_m2": float(w.sum() * cell), "cut_m3": cut, "fill_m3": fill, "net_m3": cut - fill,
            "above_m3": above, "below_m3": below, "quota_min": float(np.nanmin(z[mask])),
            "quota_max": float(np.nanmax(z[mask]))}


def _coverage(polygon, shape, tr, sub: int = 8) -> np.ndarray:
    """Fraction (0..1) of each DEM cell covered by ``polygon``, from a ``sub`` x ``sub``
    supersampled rasterisation (cell-centre rasterisation loses a strip along the edges)."""
    from rasterio.features import geometry_mask
    from rasterio.transform import Affine

    minx, miny, maxx, maxy = polygon.bounds
    inv = ~tr
    c0, r0 = inv * (minx, maxy)
    c1, r1 = inv * (maxx, miny)
    ca, cb = sorted((c0, c1))
    ra, rb = sorted((r0, r1))
    ca, ra = max(int(np.floor(ca)) - 1, 0), max(int(np.floor(ra)) - 1, 0)
    cb, rb = min(int(np.ceil(cb)) + 1, shape[1]), min(int(np.ceil(rb)) + 1, shape[0])
    out = np.zeros(shape, np.float64)
    if cb <= ca or rb <= ra:
        return out
    fine_tr = tr * Affine.translation(ca, ra) * Affine.scale(1.0 / sub)
    fine = geometry_mask([polygon], out_shape=((rb - ra) * sub, (cb - ca) * sub), transform=fine_tr, invert=True)
    out[ra:rb, ca:cb] = fine.reshape(rb - ra, sub, cb - ca, sub).mean(axis=(1, 3))
    return out


def _resample_to(path: Path, shape, tr) -> np.ndarray:
    import cv2

    z2, tr2, _, _ = read_dem(path)
    rows, cols = np.mgrid[0:shape[0], 0:shape[1]]
    x = tr.c + (cols + 0.5) * tr.a
    y = tr.f + (rows + 0.5) * tr.e
    c2 = ((x - tr2.c) / tr2.a - 0.5).astype(np.float32)
    r2 = ((y - tr2.f) / tr2.e - 0.5).astype(np.float32)
    valid = np.isfinite(z2).astype(np.float32)
    num = cv2.remap(np.nan_to_num(z2).astype(np.float32) * valid, c2, r2, cv2.INTER_LINEAR, borderValue=0)
    den = cv2.remap(valid, c2, r2, cv2.INTER_LINEAR, borderValue=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0.5, num / den, np.nan)


def hillshade(dem_path: str | Path, out_path: str | Path, azimuth: float = 315, altitude: float = 45,
              z_factor: float = 1.0, crs: str | None = None) -> Path:
    z, tr, _, res = read_dem(Path(dem_path))
    gy, gx = np.gradient(np.nan_to_num(z, nan=np.nanmean(z)) * z_factor, res, res)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(gy, -gx)  # gy = dz/drow (southwards), gx = dz/dcol (eastwards)
    az, alt = np.radians(360 - azimuth + 90), np.radians(altitude)
    hs = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    out = np.clip(hs * 255, 0, 255).astype(np.uint8)
    out[~np.isfinite(z)] = 0
    return write_geotiff(Path(out_path), out, (tr.c, tr.f), res, crs, nodata=0)


def slope_degrees(dem_path: str | Path, out_path: str | Path, crs: str | None = None) -> Path:
    z, tr, _, res = read_dem(Path(dem_path))
    gy, gx = np.gradient(z, res, res)
    s = np.degrees(np.arctan(np.hypot(gx, gy)))
    return write_geotiff(Path(out_path), np.where(np.isfinite(s), s, NODATA).astype(np.float32), (tr.c, tr.f), res,
                         crs, nodata=NODATA)
