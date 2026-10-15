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
    """Cut/fill between a DEM and a reference (another DEM on the same grid, a
    horizontal plane at a given elevation, or the lowest point of the polygon).

    ``polygon`` (shapely geometry in world coordinates) restricts the computation.
    Positive difference = material above the reference (to be excavated, "scavo").
    """
    z, tr, _, res = read_dem(Path(dem_path))
    area_mask = np.isfinite(z)
    if polygon is not None:
        from rasterio.features import geometry_mask

        area_mask &= geometry_mask([polygon], out_shape=z.shape, transform=tr, invert=True)
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
    cell = abs(tr.a * tr.e)
    above = np.nansum(np.clip(diff, 0, None)) * cell
    below = -np.nansum(np.clip(diff, None, 0)) * cell
    if difference_path:
        write_geotiff(Path(difference_path), np.where(mask, diff, NODATA).astype(np.float32), (tr.c, tr.f), res,
                      None, nodata=NODATA)
    return {"area_m2": float(mask.sum() * cell), "cut_m3": float(above), "fill_m3": float(below),
            "net_m3": float(above - below), "quota_min": float(np.nanmin(z[mask])),
            "quota_max": float(np.nanmax(z[mask]))}


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
