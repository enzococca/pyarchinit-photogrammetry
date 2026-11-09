"""The water level of a chunk and the refraction correction of its dense cloud (since 1.7.6).

A trench or a wreck under a few centimetres of still water is photographed through the surface: seen from above, the
bottom looks shallower than it is, by the refractive index ``n`` of the water (1.34 sea, 1.33 fresh). With the level
``L`` of the water known, a point seen at the height ``z`` below it is put back at ``z' = L - n (L - z)``; points above
the level stay as they are (:func:`corrected`). The correction is paraxial: it holds for photos taken close to the
vertical ("correzione parassiale: vale per foto quasi nadirali"), as is the rule over a flooded trench.

The level comes from the edge of the water (:func:`level_from_edge`): the water polygon - a layer of free drawings, a
colour mask of :mod:`.colour_mask` for instance, or a colour range of the orthophoto - its outline sampled every cell of
the DEM, the DEM read two cells on the land side of it, the median of those heights the level, their NMAD its spread.
Or it is given by hand (:func:`set_level`). It is stored in ``chunk.meta["water"]`` (``level`` a world height in the
system of the chunk, stamped with the transform of that moment like a raster, :func:`..core.stamps.stamp_entry`) and
:func:`level_of` gives it in the current world: moved with the chunk when its transform changed since.

:func:`correct_cloud` writes the corrected cloud as a new version of the dense cloud (the one before stays among the
versions: making it active again undoes the correction) and, only when asked, builds the DEM again from it as a new
version of the DEM. No Qt here.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np

from ..core.progress import Progress, as_progress

if TYPE_CHECKING:  # pragma: no cover
    from ..core.chunk import Chunk

log = logging.getLogger("pyarchinit_photogrammetry")

N_SEA = 1.34
N_FRESH = 1.33
N_RANGE = (1.0, 2.0)
LAND_STEP = 2  # cells of the DEM between the outline of the water and the heights read on the land side (R7)
MIN_EDGE_CELLS = 10  # heights of the DEM along the edge needed for a level
NMAD = 1.4826
NOTE = "correzione parassiale: vale per foto quasi nadirali"
NOTE_EN = "paraxial correction: it holds for photos close to the vertical"
SOURCES = ("edge", "hand")


class WaterError(ValueError):
    """A level or a correction that cannot be made: ``str()`` is the English message, ``italian`` the Italian one (the
    window shows the Italian one in Italian, the command line the English one), as :class:`.colour_mask.MaskError`."""

    def __init__(self, english: str, italian: str):
        super().__init__(english)
        self.english = english
        self.italian = italian


def corrected(z, level: float, n: float) -> np.ndarray:
    """The heights ``z`` seen through water whose surface is at ``level``, put back where they are: ``L - n (L - z)``
    below the level, unchanged above it (paraxial refraction)."""
    z = np.asarray(z, dtype=np.float64)
    L = float(level)
    return np.where(z < L, L - float(n) * (L - z), z)


def _check_n(n: float) -> float:
    n = float(n)
    if not (np.isfinite(n) and N_RANGE[0] <= n <= N_RANGE[1]):
        raise WaterError(f"refractive index {n:g}: from {N_RANGE[0]:g} to {N_RANGE[1]:g} (sea {N_SEA:g}, fresh "
                         f"water {N_FRESH:g})", f"indice di rifrazione {n:g}: da {N_RANGE[0]:g} a {N_RANGE[1]:g} "
                         f"(mare {N_SEA:g}, acqua dolce {N_FRESH:g})")
    return n


def _decimal(v: float) -> str:
    return f"{v:.2f}".replace(".", ",")


# ------------------------------------------------------------------ the level from the edge of the water
def _dem(chunk: "Chunk"):
    from ..core import stamps

    path = chunk.dem_path
    if path is None or not path.exists():
        raise WaterError(f"the chunk {chunk.label} has no DEM for the level of the water: build it first",
                         f"il chunk {chunk.label} non ha un DEM per il livello dell'acqua: costruiscilo prima")
    if stamps.is_stale(chunk, "dem"):
        raise WaterError(f"the DEM of {chunk.label} was built in another system of the chunk: rebuild it first",
                         f"il DEM di {chunk.label} è stato costruito in un altro sistema del chunk: rifallo prima")
    return path


def _no_water() -> WaterError:
    return WaterError("no water found: the mask is empty", "nessuna acqua trovata: la maschera è vuota")


def water_polygons(chunk: "Chunk", water, progress: Progress | None = None) -> list:
    """The polygons of the water: ``water`` a shapely (Multi)Polygon, an :class:`.colour_mask.HsvRange` (the largest
    region of those colours of the orthophoto) or the name of a layer of free drawings. :class:`WaterError` (or the
    :class:`.colour_mask.MaskError` of the mask) when there is none."""
    from shapely.geometry.base import BaseGeometry

    from . import colour_mask

    if isinstance(water, colour_mask.HsvRange):
        geom = colour_mask.mask(chunk, water, progress=progress).polygons
    elif isinstance(water, str):
        geom = [colour_mask.union(chunk, water)]
    elif isinstance(water, BaseGeometry):
        geom = [water]
    elif isinstance(water, (list, tuple)):
        geom = list(water)
    else:
        raise TypeError(f"water: a polygon, a colour range or a layer name, not {type(water).__name__}")
    polys = []
    for g in geom:
        for p in getattr(g, "geoms", [g]):
            if p.geom_type == "Polygon" and not p.is_empty and p.area > 0:
                polys.append(p if p.is_valid else p.buffer(0))
    polys = [q for p in polys for q in getattr(p, "geoms", [p]) if not q.is_empty and q.area > 0]
    if not polys:
        raise _no_water()
    return polys


def edge_points(polygons, cell: float, step: int = LAND_STEP) -> np.ndarray:
    """The points on the land side of the outline of ``polygons`` (their outer rings and their holes - islands),
    sampled every ``cell`` metres along it and ``step`` cells away from it along its normal: (n, 2). A point whose
    two sides are both water or both land (a narrow arm) is left out."""
    import shapely
    from shapely.geometry import LineString

    water = shapely.union_all(list(polygons))
    out = []
    d = float(step) * float(cell)
    for poly in getattr(water, "geoms", [water]):
        for ring in [poly.exterior, *poly.interiors]:
            line = LineString(ring.coords)
            length = line.length
            if length <= 0:
                continue
            s = np.arange(0.0, length, float(cell)) + 0.5 * min(float(cell), length)
            s = s[s < length]
            if not len(s):
                continue
            h = 0.5 * float(cell)
            a = shapely.get_coordinates(shapely.line_interpolate_point(line, np.clip(s - h, 0, length)))
            b = shapely.get_coordinates(shapely.line_interpolate_point(line, np.clip(s + h, 0, length)))
            p = shapely.get_coordinates(shapely.line_interpolate_point(line, s))
            t = b - a
            norm = np.hypot(t[:, 0], t[:, 1])
            ok = norm > 0
            nrm = np.zeros_like(t)
            nrm[ok] = np.column_stack([t[ok, 1], -t[ok, 0]]) / norm[ok, None]
            plus, minus = p + d * nrm, p - d * nrm
            in_plus = shapely.contains_xy(water, plus[:, 0], plus[:, 1])
            in_minus = shapely.contains_xy(water, minus[:, 0], minus[:, 1])
            pick = ok & (in_plus != in_minus)
            out.append(np.where(in_plus[:, None], minus, plus)[pick])
    return np.concatenate(out) if out else np.zeros((0, 2))


def level_from_edge(chunk: "Chunk", water, progress: Progress | None = None) -> dict:
    """The level of the water of ``chunk`` from its edge (see the module): ``water`` as :func:`water_polygons`.
    Returns ``{"level", "spread" (NMAD), "p10", "p90", "edge_cells", "x", "y" (the centre of the water)}``; writes
    nothing (:func:`set_level` stores it). :class:`WaterError` without a DEM, with one to rebuild, without water, or
    when the outline of the water lies outside the DEM."""
    import rasterio

    from ..core.free_drawings import raster_values

    p = as_progress(progress)
    path = _dem(chunk)
    p(0.0, "Riva dell'acqua...")
    polys = water_polygons(chunk, water, progress=p.sub(0.0, 0.6))
    p.check()
    p(0.6, "Lettura del DEM lungo la riva...")
    with rasterio.open(path) as src:
        cell = float(min(abs(src.res[0]), abs(src.res[1])))
    pts = edge_points(polys, cell)
    z = np.array([np.nan if v is None else v for v in raster_values(path, pts)], float) if len(pts) else np.zeros(0)
    z = z[np.isfinite(z)]
    if len(z) < MIN_EDGE_CELLS:
        raise WaterError(f"the edge of the water lies outside the DEM of {chunk.label}: {len(z)} height(s) on the "
                         f"land side (at least {MIN_EDGE_CELLS})",
                         f"la riva dell'acqua è fuori dal DEM di {chunk.label}: {len(z)} quote dalla parte della "
                         f"terra (almeno {MIN_EDGE_CELLS})")
    level = float(np.median(z))
    import shapely

    c = shapely.union_all(polys).centroid
    p(1.0, "")
    return {"level": level, "spread": float(NMAD * np.median(np.abs(z - level))),
            "p10": float(np.percentile(z, 10)), "p90": float(np.percentile(z, 90)), "edge_cells": int(len(z)),
            "x": float(c.x), "y": float(c.y)}


# ------------------------------------------------------------------ the stored level
def _centre(chunk: "Chunk") -> tuple[float, float]:
    """A world point of the chunk's data: the centre of the DEM, else of the dense cloud, else the internal origin."""
    path = chunk.dem_path
    if path is not None and path.exists():
        import rasterio

        with rasterio.open(path) as src:
            b = src.bounds
        return 0.5 * (b.left + b.right), 0.5 * (b.bottom + b.top)
    cloud = chunk.product_path("point_cloud")
    if cloud is not None and cloud.exists():
        from ..core.pointcloud import PointCloud

        xyz = chunk.to_world(PointCloud.sample_xyz(cloud, at_most=10000))
        if len(xyz):
            c = np.median(xyz, axis=0)
            return float(c[0]), float(c[1])
    o = chunk.to_world(np.zeros((1, 3)))[0]
    return float(o[0]), float(o[1])


def set_level(chunk: "Chunk", level: float, source: str = "hand", n: float = N_SEA, **info) -> dict:
    """Store the level of the water of ``chunk``: ``level`` a world height in its system now, ``source`` "edge" (from
    :func:`level_from_edge`, whose result goes in ``info``) or "hand", ``n`` the refractive index. ``info`` may give
    ``x``, ``y`` (where the water is: the level follows the chunk there when its transform changes; default the
    centre of the data), ``spread``, ``p10``, ``p90``, ``edge_cells``. Returns the record (``chunk.meta["water"]``)."""
    from ..core import stamps

    level = float(level)
    if not np.isfinite(level):
        raise WaterError(f"level {level}: a number is needed", f"livello {level}: serve un numero")
    if source not in SOURCES:
        raise ValueError(f"source {source!r}: one of {', '.join(SOURCES)}")
    n = _check_n(n)
    x, y = info.get("x"), info.get("y")
    if x is None or y is None:
        x, y = _centre(chunk)
    record = {"level": level, "n": n, "source": source, "spread": info.get("spread"),
              "edge_cells": info.get("edge_cells"), "p10": info.get("p10"), "p90": info.get("p90"),
              "x": float(x), "y": float(y), "when": datetime.now().isoformat(timespec="seconds")}
    if source == "hand":
        record.update(spread=None, edge_cells=None, p10=None, p90=None)
    stamps.stamp_entry(chunk, record)
    old = chunk.meta.get("water") or {}
    if old.get("correction"):  # the correction made with the earlier level stays told
        record["correction"] = old["correction"]
    chunk.meta["water"] = record
    return dict(record)


def level_of(chunk: "Chunk") -> dict | None:
    """The level of the water of ``chunk`` in its current world: ``{"level", "n", "source", "spread"}`` (None without
    one). When the transform of the chunk changed since the level was set, the level is moved with it (the point of
    the water at the level, taken back to the chunk's coordinates and out again through the transform now)."""
    rec = chunk.meta.get("water")
    if not isinstance(rec, dict) or rec.get("level") is None:
        return None
    level = float(rec["level"])
    built_in = rec.get("built_in")
    from ..core import stamps

    if built_in is not None and not stamps.same_transform(built_in, chunk.transform):
        x = float(rec.get("x") or 0.0)
        y = float(rec.get("y") or 0.0)
        old = np.asarray(built_in, float).reshape(4, 4)
        now = np.asarray(chunk.transform, float).reshape(4, 4)
        inner = np.linalg.solve(old, np.array([x, y, level, 1.0]))
        level = float((now @ inner)[2])
    return {"level": level, "n": float(rec.get("n") or N_SEA), "source": rec.get("source"),
            "spread": rec.get("spread")}


# ------------------------------------------------------------------ the correction of the dense cloud
def _dem_resolution(chunk: "Chunk") -> float | None:
    """The cell of the DEM there is (the DEM built again on the same grid spacing), else the one recorded."""
    path = chunk.dem_path
    if path is not None and path.exists():
        import rasterio

        with rasterio.open(path) as src:
            return float(abs(src.res[0]))
    return (chunk.meta.get("dem") or {}).get("resolution")


def correct_cloud(chunk: "Chunk", n: float | None = None, rebuild_dem: bool = False,
                  progress: Progress | None = None) -> dict:
    """The dense cloud of ``chunk`` corrected for the refraction under its water level (:func:`level_of`): a new
    version of the dense cloud, named "Nuvola corretta per la rifrazione (n 1,34)", the cloud before kept among the
    versions (make it active again to undo). ``n``: the refractive index (None: the one stored with the level).
    ``rebuild_dem``: the DEM built again from the corrected cloud, as a new version of the DEM (off by default).
    Returns ``{"points", "below", "version" (the id of the cloud kept), "name", "level", "n", "note", "dem",
    "dem_version"}``. :class:`WaterError` without a level, without a dense cloud, on a cloud already corrected, or
    with no point below the level (nothing written)."""
    from ..core import versions
    from ..core.pointcloud import PointCloud

    p = as_progress(progress)
    lv = level_of(chunk)
    if lv is None:
        raise WaterError(f"the chunk {chunk.label} has no water level: set it first (from the edge or by hand)",
                         f"il chunk {chunk.label} non ha un livello dell'acqua: impostalo prima (dalla riva o a mano)")
    n = _check_n(lv["n"] if n is None else n)
    src = chunk.product_path("point_cloud")
    if src is None or not src.exists():
        raise WaterError(f"the chunk {chunk.label} has no dense cloud: build it first",
                         f"il chunk {chunk.label} non ha una nuvola densa: costruiscila prima")
    if (chunk.meta.get("dense") or {}).get("refraction"):
        raise WaterError(f"the dense cloud of {chunk.label} is already corrected for the refraction: make the "
                         "cloud before it active to correct it again",
                         f"la nuvola densa di {chunk.label} è già corretta per la rifrazione: rendi attiva quella "
                         "di prima per correggerla di nuovo")
    L = float(lv["level"])
    p(0.0, "Correzione della rifrazione...")
    pc = PointCloud.load(src)
    world = chunk.to_world(pc.xyz)
    below = world[:, 2] < L
    nb = int(below.sum())
    if nb == 0:
        raise WaterError(f"no point of the dense cloud of {chunk.label} lies below the level {L:.3f}: nothing to "
                         "correct", f"nessun punto della nuvola densa di {chunk.label} è sotto il livello "
                         f"{L:.3f}: niente da correggere")
    p.check()
    world = world[below]
    world[:, 2] = corrected(world[:, 2], L, n)
    xyz = pc.xyz.copy()
    xyz[below] = chunk.to_internal(world)  # the points above the level keep their coordinates to the bit
    out = PointCloud(xyz, pc.rgb, pc.normals, pc.classification, pc.confidence,
                     dict(pc.labels), pc.manual)
    if out.normals is not None:  # z stretched by n below the level: normals by the inverse, in world
        m = np.asarray(chunk.transform, float)[:3, :3]
        r = m / np.cbrt(np.linalg.det(m))
        nw = out.normals[below].astype(np.float64) @ r.T
        nw[:, 2] /= n
        nw /= np.maximum(np.linalg.norm(nw, axis=1, keepdims=True), 1e-12)
        normals = out.normals.copy()
        normals[below] = (nw @ r).astype(np.float32)  # back: r is a rotation (r^-1 = r^T)
        out.normals = normals
    p(0.5, "Scrittura della nuvola corretta...")
    tmp = chunk.data_dir / "point_cloud.water.tmp.ply"
    try:
        out.save_ply(tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    params = dict(chunk.meta.get("dense") or {})
    chunk._before_product_write("point_cloud")
    kept = versions.keep_active(chunk, "point_cloud", move=True)
    try:
        own = chunk.own_product_path("point_cloud")
        tmp.replace(own)
        chunk._set_product("point_cloud", own)
    except BaseException:
        if kept is not None:
            versions.restore(chunk, "point_cloud", kept)
        tmp.unlink(missing_ok=True)
        raise
    if kept is not None and not kept.get("params"):
        # an empty record of parameters is not written back when the version is made active again: the cloud kept
        # says it is not corrected, so that making it active undoes the mark of the correction too
        kept["params"] = {"refraction": None}
    record = {"level": L, "n": n, "source": lv.get("source"), "points": int(len(pc)), "below": nb,
              "version": kept["id"] if kept else None, "when": datetime.now().isoformat(timespec="seconds"),
              "note": NOTE}
    chunk.meta["dense"] = {**params, "refraction": record}
    name = versions.label("Nuvola corretta per la rifrazione (n {0})", _decimal(n))[:80]
    versions.set_active_name(chunk, "point_cloud", name)
    wrec = chunk.meta.get("water")
    told = wrec.get("correction") if isinstance(wrec, dict) else None
    if isinstance(wrec, dict):
        wrec["correction"] = dict(record)
    log.info("Nuvola corretta per la rifrazione: %d punti su %d sotto il livello %.3f, n %.2f (%s)", nb, len(pc), L,
             n, NOTE)
    result = {**record, "name": name, "dem": None, "dem_version": None}
    if rebuild_dem:
        p(0.6, "DEM dalla nuvola corretta...")
        resolution = _dem_resolution(chunk)
        dem_meta = chunk.meta.get("dem")
        dem_kept = versions.keep_active(chunk, "dem", move=True)
        try:
            path = chunk.build_dem(resolution=resolution, progress=p.sub(0.6, 0.4))
        except BaseException:  # the whole correction undone: the cloud before back too
            if dem_kept is not None:
                versions.restore(chunk, "dem", dem_kept)
            if dem_meta is not None:
                chunk.meta["dem"] = dem_meta
            if kept is not None:
                versions.restore(chunk, "point_cloud", kept)
                chunk.meta["dense"] = params
            if isinstance(wrec, dict):
                wrec.pop("correction", None)
                if told is not None:
                    wrec["correction"] = told
            raise
        versions.set_active_name(chunk, "dem", versions.label("DEM dalla nuvola corretta (n {0})", _decimal(n))[:80])
        result.update(dem=str(path), dem_version=dem_kept["id"] if dem_kept else None)
    p(1.0, "")
    return result


__all__ = ["N_SEA", "N_FRESH", "N_RANGE", "LAND_STEP", "MIN_EDGE_CELLS", "NOTE", "NOTE_EN", "WaterError", "corrected",
           "water_polygons", "edge_points", "level_from_edge", "set_level", "level_of", "correct_cloud"]
