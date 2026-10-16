"""Terrain tools on DEMs: contour lines, cut/fill volumes, slope, hillshade."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..engine.dem import NODATA, read_dem, write_geotiff


CONTOUR_INTERVALS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 2.5, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0)


def dem_range(dem_path: str | Path) -> tuple[float, float]:
    """Lowest and highest elevation of a DEM, from a reduced reading (fast on large DEMs)."""
    import rasterio

    with rasterio.open(dem_path) as src:
        f = max(1, max(src.width, src.height) // 1500)
        a = src.read(1, out_shape=(max(1, src.height // f), max(1, src.width // f)), masked=True).astype(float)
    a = np.ma.masked_invalid(a)
    if a.count() == 0:
        raise ValueError("DEM senza quote valide")
    return float(a.min()), float(a.max())


def suggest_interval(dem_path: str | Path, levels: int = 50) -> float:
    """A round contour interval giving at most about ``levels`` levels over the relief of the
    DEM: 5 cm on a trench with 1 m of relief, 50 cm on a tell 20 m high."""
    lo, hi = dem_range(dem_path)
    return next((iv for iv in CONTOUR_INTERVALS if (hi - lo) / iv <= levels), CONTOUR_INTERVALS[-1])


def contours(dem_path: str | Path, interval: float, base: float = 0.0, index_every: int = 5,
             simplify: bool = True, min_area_px: float = 25.0, smooth: float = 0.0) -> list[dict]:
    """Contour lines of a DEM. Returns ``[{"elevation", "index", "coords" (N,2) world}]``.

    With ``simplify`` every line keeps only the vertices needed to stay within half a pixel
    of the traced line (a DEM traced pixel by pixel gives a vertex every pixel), and closed
    rings smaller than ``min_area_px`` pixels (noise of the surface) are left out: on a
    real DEM that is about a sixth of the vertices for the same drawing. ``smooth`` (metres)
    smooths the DEM first with a Gaussian of that width, ignoring the cells without data:
    on a drone DSM (vegetation, clods) 0.2-0.5 m removes most of the small rings."""
    import contourpy
    import shapely

    z, tr, _, res = read_dem(Path(dem_path))
    lo, hi = np.nanmin(z), np.nanmax(z)
    if smooth > 0:
        from scipy import ndimage

        empty = np.isnan(z)
        sigma = smooth / abs(res)
        num = ndimage.gaussian_filter(np.where(empty, 0.0, z).astype(np.float32), sigma)
        den = ndimage.gaussian_filter((~empty).astype(np.float32), sigma)
        z = np.where(empty, np.nan, num / np.maximum(den, 1e-6))
        del num, den
    gen = contourpy.contour_generator(z=np.ma.masked_invalid(z), line_type=contourpy.LineType.Separate)
    out = []
    for n in range(int(np.ceil((lo - base) / interval)), int(np.floor((hi - base) / interval)) + 1):
        level = round(base + n * interval, 9)  # 294.15, not 294.15000000000003
        for line in gen.lines(level):
            if len(line) < 2:
                continue
            if simplify:
                if len(line) > 3 and np.array_equal(line[0], line[-1]):
                    x, y = line[:, 0], line[:, 1]
                    if 0.5 * abs(np.dot(x[:-1], y[1:]) - np.dot(x[1:], y[:-1])) < min_area_px:
                        continue
                line = shapely.get_coordinates(shapely.simplify(shapely.linestrings(line), 0.5))
                if len(line) < 2:
                    continue
            col, row = line[:, 0], line[:, 1]
            x = tr.c + (col + 0.5) * tr.a
            y = tr.f + (row + 0.5) * tr.e
            out.append({"elevation": float(level), "index": n % index_every == 0, "coords": np.column_stack([x, y])})
    return out


def export_contours(lines: list[dict], path: str | Path, crs: str | None = None) -> Path:
    """Write contours as DXF (2.5D polylines with elevation), GeoPackage (``.gpkg``: table
    ``curve_di_livello`` with ``quota`` and ``direttrice``) or GeoJSON (any other name)."""
    path = Path(path)
    if path.suffix.lower() == ".gpkg":
        from shapely.geometry import LineString

        from ..io import gpkg

        feats = [(LineString(c["coords"]) if len(c["coords"]) >= 2 else None,
                  {"quota": float(c["elevation"]), "direttrice": int(bool(c["index"]))}) for c in lines]
        return gpkg.write_table(path, "curve_di_livello", "LINESTRING", {"quota": "REAL", "direttrice": "INTEGER"},
                                feats, crs, identifier="Curve di livello")
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
