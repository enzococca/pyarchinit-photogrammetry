"""Cropping the products with a polygon, for export or inside the project.

The region is a polygon in the chunk CRS (drawn on the orthophoto or the DEM, a stratigraphic
unit, a site boundary), optionally with a height range: rasters keep the window of the polygon,
with no data (DEM) or transparent (orthophoto) outside it; the model keeps the faces whose
centre is inside (whole faces, as Metashape's crop selection); the dense cloud the points
inside. Large rasters are written in strips, never read whole.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ..core.chunk import Chunk


def as_polygon(region):
    """A shapely (multi)polygon from ``region``: a shapely geometry, an ``(N, 2)`` ring of
    vertices, a box ``(xmin, ymin, xmax, ymax)``, WKT or a GeoJSON geometry (dict)."""
    import shapely
    from shapely.geometry import box, shape

    if isinstance(region, shapely.Geometry):
        geom = region
    elif isinstance(region, str):
        geom = shapely.from_wkt(region)
    elif isinstance(region, dict):
        geom = shape(region.get("geometry", region))
    else:
        arr = np.asarray(region, float)
        if arr.ndim == 1 and arr.size == 4:
            geom = box(*arr)
        elif arr.ndim == 2 and arr.shape[0] >= 3 and arr.shape[1] >= 2:
            geom = shapely.Polygon(arr[:, :2])
        else:
            raise ValueError("the region must be a polygon (at least 3 vertices) or a box")
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"the region must be a polygon, not a {geom.geom_type}")
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
        polys = [g for g in getattr(geom, "geoms", [geom]) if g.geom_type in ("Polygon", "MultiPolygon")]
        geom = shapely.union_all(polys) if polys else geom
    if geom.is_empty or geom.area <= 0:
        raise ValueError("the region is empty")
    return geom


def inside_xy(region, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """True for the points inside the region (its border included)."""
    import shapely

    geom = as_polygon(region)
    shapely.prepare(geom)
    out = np.zeros(len(x), bool)
    step = 2_000_000
    for s in range(0, len(x), step):
        out[s:s + step] = shapely.intersects_xy(geom, x[s:s + step], y[s:s + step])
    return out


def crop_raster(src, dst, region, strip: int = 2048) -> Path:
    """Write the window of ``src`` (GeoTIFF) covering ``region``: no data outside the polygon
    (transparent for RGBA, the nodata value for a DEM)."""
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.windows import Window, from_bounds

    from .dem import NODATA

    geom = as_polygon(region)
    dst = Path(dst)
    with rasterio.open(src) as s:
        w = from_bounds(*geom.bounds, transform=s.transform)
        c0, r0 = max(int(np.floor(w.col_off)), 0), max(int(np.floor(w.row_off)), 0)
        c1 = min(int(np.ceil(w.col_off + w.width)), s.width)
        r1 = min(int(np.ceil(w.row_off + w.height)), s.height)
        if c1 <= c0 or r1 <= r0:
            raise ValueError("the region does not overlap the raster")
        win = Window(c0, r0, c1 - c0, r1 - r0)
        transform = s.window_transform(win)
        profile = s.profile.copy()
        width, height = int(win.width), int(win.height)
        profile.update(width=width, height=height, transform=transform, compress="deflate", BIGTIFF="IF_SAFER")
        if width >= 256 and height >= 256:
            profile.update(tiled=True, blockxsize=256, blockysize=256)
        else:
            profile.update(tiled=False)
            profile.pop("blockxsize", None)
            profile.pop("blockysize", None)
        rgba = s.count == 4 and np.dtype(s.dtypes[0]) == np.uint8 and (
            s.colorinterp[3] == rasterio.enums.ColorInterp.alpha or s.nodata is None)
        nodata = s.nodata
        if not rgba and nodata is None:
            nodata = NODATA if np.dtype(s.dtypes[0]).kind == "f" else 0
            profile["nodata"] = nodata
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp.tif")
        inside_any = False
        with rasterio.open(tmp, "w", **profile) as out:
            if rgba:
                out.colorinterp = s.colorinterp
            for r0 in range(0, height, strip):
                h = min(strip, height - r0)
                part = Window(win.col_off, win.row_off + r0, width, h)
                data = s.read(window=part)
                inside = geometry_mask([geom], out_shape=(h, width), transform=s.window_transform(part), invert=True,
                                       all_touched=False)
                inside_any |= bool(inside.any())
                if rgba:
                    data[:, ~inside] = 0
                else:
                    data[:, ~inside] = nodata
                out.write(data, window=Window(0, r0, width, h))
        if not inside_any:
            tmp.unlink(missing_ok=True)
            raise ValueError("the region does not cover any pixel of the raster")
        aux = Path(str(dst) + ".aux.xml")
        if aux.exists():
            aux.unlink()
        tmp.replace(dst)
    return dst


def faces_inside(chunk: Chunk, vertices: np.ndarray, faces: np.ndarray, region, zmin: float | None = None,
                 zmax: float | None = None) -> np.ndarray:
    """True for the faces (internal frame) whose centre lies inside the region (world XY) and
    between ``zmin`` and ``zmax`` (world heights)."""
    keep = np.zeros(len(faces), bool)
    step = 2_000_000
    for s in range(0, len(faces), step):
        f = faces[s:s + step]
        c = chunk.to_world((vertices[f[:, 0]] + vertices[f[:, 1]] + vertices[f[:, 2]]) / 3.0)
        k = np.ones(len(c), bool) if region is None else inside_xy(region, c[:, 0], c[:, 1])
        if zmin is not None:
            k &= c[:, 2] >= zmin
        if zmax is not None:
            k &= c[:, 2] <= zmax
        keep[s:s + step] = k
    return keep


def points_inside(chunk: Chunk, xyz: np.ndarray, region=None, zmin: float | None = None,
                  zmax: float | None = None) -> np.ndarray:
    """True for the points (internal frame) inside the region and the height range."""
    world = chunk.to_world(xyz)
    keep = np.ones(len(world), bool) if region is None else inside_xy(region, world[:, 0], world[:, 1])
    if zmin is not None:
        keep &= world[:, 2] >= zmin
    if zmax is not None:
        keep &= world[:, 2] <= zmax
    return keep


def crop_model(chunk: Chunk, region, zmin: float | None = None, zmax: float | None = None) -> dict:
    """Keep in the project only the faces of the model inside the region (colours and
    confidence kept; the textured model is left out until the texture is built again)."""
    import trimesh

    from .cleaning import delete_faces

    path = chunk.product_path("model")
    if path is None or not path.exists():
        raise RuntimeError("build the model first")
    mesh = trimesh.load(str(path), process=False, force="mesh")
    keep = faces_inside(chunk, np.asarray(mesh.vertices), np.asarray(mesh.faces), region, zmin, zmax)
    if not keep.any():
        raise ValueError("no face of the model lies inside the region")
    return delete_faces(chunk, keep, keep_selected=True)


def crop_point_cloud(chunk: Chunk, region, zmin: float | None = None, zmax: float | None = None) -> dict:
    """Keep in the project only the dense points inside the region."""
    from .cleaning import delete_points

    pc = chunk.point_cloud
    if pc is None:
        raise RuntimeError("build the dense cloud first")
    keep = points_inside(chunk, pc.xyz, region, zmin, zmax)
    if not keep.any():
        raise ValueError("no point of the dense cloud lies inside the region")
    return delete_points(chunk, keep, keep_selected=True)


def crop_raster_product(chunk: Chunk, product: str, region) -> Path:
    """Crop the DEM (``"dem"``) or the orthomosaic (``"orthomosaic"``) of the project; a raster of
    another program (e.g. exported by Metashape) is not touched: the cropped copy, in the
    chunk folder, takes its place in the project."""
    path = chunk.product_path(product)
    if path is None or not path.exists():
        raise RuntimeError(f"no {product} to crop")
    out = chunk.own_product_path(product)
    tmp = out.with_name(out.stem + "_ritaglio.tif")
    crop_raster(path, tmp, region)
    tmp.replace(out)
    chunk._set_product(product, out)
    return out


__all__ = ["as_polygon", "crop_model", "crop_point_cloud", "crop_raster", "crop_raster_product", "faces_inside",
           "inside_xy", "points_inside"]
