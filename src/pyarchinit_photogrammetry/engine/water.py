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
version of the DEM.

Several water levels (since 1.9.1). A survey of several years or of several basins does not have one level and one
index: each chunk keeps its own level and effective factor ``n`` (a chunk per year: :func:`apply_table` sets them from
a table by chunk or by date of the survey, :func:`..archaeo.timeline.phase_date`), and within a chunk the water can be
given as **basins** (:func:`set_basins`): each a horizontal level or a plane fitted to points picked on the shoreline
(:func:`plane_from_shore`: a water surface seen tilted by a slightly tilted frame), with its own factor, inside its
polygons (or everywhere: the basin without polygons takes the points no other basin takes). The effective factor
can be measured (:func:`estimate_factor`): points of known true height under the water (control points measured with
a staff or GNSS, or markers whose reference lies below the water and whose position was triangulated through it) and
their apparent height (the DEM or the dense cloud before the correction, or the triangulated marker) give ``n`` by
least squares on ``L - z_true = n (L - z_apparent)`` (or ``n`` and the level together), with the residuals of every
point. Without basins everything works as before: one level, one ``n``. No Qt here.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
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


def corrected(z, level, n) -> np.ndarray:
    """The heights ``z`` seen through water whose surface is at ``level``, put back where they are: ``L - n (L - z)``
    below the level, unchanged above it (paraxial refraction). ``level`` and ``n``: numbers, or one per height."""
    z = np.asarray(z, dtype=np.float64)
    L = np.asarray(level, dtype=np.float64)  # a number, or one per height (basins, planes)
    return np.where(z < L, L - np.asarray(n, dtype=np.float64) * (L - z), z)


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


def edge_samples(polygons, cell: float, step: int = LAND_STEP) -> tuple[np.ndarray, np.ndarray]:
    """The outline of ``polygons`` (their outer rings and their holes - islands) sampled every ``cell`` metres: the
    points on it and the unit normals toward the land, ``(p, normal)`` each (n, 2). The land side is the side whose
    point ``step`` cells away is not water; a point whose two sides are both water or both land (a narrow arm) is
    left out."""
    import shapely
    from shapely.geometry import LineString

    water = shapely.union_all(list(polygons))
    pts, nrms = [], []
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
            nrm = np.where(in_plus[:, None], -nrm, nrm)
            pts.append(p[pick])
            nrms.append(nrm[pick])
    if not pts:
        return np.zeros((0, 2)), np.zeros((0, 2))
    return np.concatenate(pts), np.concatenate(nrms)


def edge_points(polygons, cell: float, step: int = LAND_STEP) -> np.ndarray:
    """The points on the land side of the outline of ``polygons``, ``step`` cells away from it along its normal,
    one every ``cell`` metres along it (see :func:`edge_samples`): (n, 2)."""
    p, nrm = edge_samples(polygons, cell, step)
    return p + float(step) * float(cell) * nrm


def _heights_at(path, pts: np.ndarray) -> np.ndarray:
    """The DEM ``path`` at ``pts``, interpolated between the centres of the four cells around each point (bilinear: the
    cell holding a point alone is off by up to half a cell times the slope, and on one side of it when the points fall
    on the cell edges). In the outer half of the border cells the height is the border cell's (the position clipped
    to the centres of the grid before the cells are chosen). NaN where a cell around has no value and outside the grid
    (a DEM written without nodata would give 0 there). One window of the file is read, the one around all the points
    (a long outline is not one reading per point), as float32: the box of all the points, so an outline running across
    a large DEM reads most of it - fine for trenches and wrecks, a band at a time would be needed for a pond across a
    survey of hundreds of millions of cells."""
    import rasterio
    from rasterio.windows import Window

    z = np.full(len(pts), np.nan)
    if not len(pts):
        return z
    with rasterio.open(path) as src:
        b, tr, (height, width) = src.bounds, src.transform, (src.height, src.width)
        inside = (pts[:, 0] >= b.left) & (pts[:, 0] <= b.right) & (pts[:, 1] >= b.bottom) & (pts[:, 1] <= b.top)
        if not inside.any():
            return z
        a, bb, c, d, e, f = tr.a, tr.b, tr.c, tr.d, tr.e, tr.f
        det = a * e - bb * d
        dx, dy = pts[inside, 0] - c, pts[inside, 1] - f
        cols, rows = (e * dx - bb * dy) / det, (a * dy - d * dx) / det
        fc = np.clip(cols - 0.5, 0.0, width - 1.0)  # in cell centres, clipped to the grid before the cells are chosen
        fr = np.clip(rows - 0.5, 0.0, height - 1.0)
        c0 = np.clip(np.floor(fc).astype(int), 0, max(width - 2, 0))
        r0 = np.clip(np.floor(fr).astype(int), 0, max(height - 2, 0))
        c1, r1 = np.minimum(c0 + 1, width - 1), np.minimum(r0 + 1, height - 1)
        tc, tr_ = fc - c0, fr - r0  # weights (0 or 1 at the border)
        col0, row0 = int(c0.min()), int(r0.min())
        win = Window(col0, row0, int(c1.max()) - col0 + 1, int(r1.max()) - row0 + 1)
        band = src.read(1, window=win, masked=True, out_dtype="float32")
    grid = np.ma.filled(band, np.nan)
    grid[~np.isfinite(grid)] = np.nan
    r0, r1, c0, c1 = r0 - row0, r1 - row0, c0 - col0, c1 - col0
    z00, z01, z10, z11 = (grid[r, c].astype(np.float64) for r, c in ((r0, c0), (r0, c1), (r1, c0), (r1, c1)))
    z[inside] = (z00 * (1 - tc) + z01 * tc) * (1 - tr_) + (z10 * (1 - tc) + z11 * tc) * tr_
    return z


def level_from_edge(chunk: "Chunk", water, progress: Progress | None = None, points: bool = False) -> dict:
    """The level of the water of ``chunk`` from its edge (see the module): ``water`` as :func:`water_polygons`.
    Each point of the outline is read on the land side at 2 and at 4 cells of the DEM along its normal and the
    height extrapolated to the outline (``2 z(2) - z(4)``: the rise of the bank over two cells taken away; where the
    4-cell reading is missing - outside the DEM, or water again - the 2-cell one alone); the median is the level.
    Returns ``{"level", "spread" (NMAD), "p10", "p90", "edge_cells", "slope_bias" (median of the 2-cell readings
    minus the level: what the extrapolation removed), "extrapolated" (points read at 4 cells too), "x", "y" (the
    centre of the water)}`` and, with ``points``, ``"points"``: the (n, 2) world positions of the heights read 2 cells
    up the bank that made the level (to show them); writes nothing (:func:`set_level` stores it). :class:`WaterError` without a DEM, with
    one to rebuild, without water, or when the outline of the water lies outside the DEM."""
    import rasterio
    import shapely

    p = as_progress(progress)
    path = _dem(chunk)
    p(0.0, "Riva dell'acqua...")
    polys = water_polygons(chunk, water, progress=p.sub(0.0, 0.6))
    p.check()
    p(0.6, "Lettura del DEM lungo la riva...")
    with rasterio.open(path) as src:
        cell = float(min(abs(src.res[0]), abs(src.res[1])))
    edge, nrm = edge_samples(polys, cell, LAND_STEP)
    near = edge + LAND_STEP * cell * nrm
    far = edge + 2 * LAND_STEP * cell * nrm
    both_z = _heights_at(path, np.concatenate([near, far]))  # one reading of the DEM for the two
    z2, z4 = both_z[:len(near)], both_z[len(near):]
    if len(far):
        wet = shapely.contains_xy(shapely.union_all(polys), far[:, 0], far[:, 1])
        z4[wet] = np.nan  # the far reading in the water: not the bank
    keep = np.isfinite(z2)
    z2, z4, near = z2[keep], z4[keep], near[keep]
    if len(z2) < MIN_EDGE_CELLS:
        raise WaterError(f"the edge of the water lies outside the DEM of {chunk.label}: {len(z2)} height(s) on the "
                         f"land side (at least {MIN_EDGE_CELLS})",
                         f"la riva dell'acqua è fuori dal DEM di {chunk.label}: {len(z2)} quote dalla parte della "
                         f"terra (almeno {MIN_EDGE_CELLS})")
    both = np.isfinite(z4)
    z = np.where(both, 2.0 * z2 - z4, z2)
    level = float(np.median(z))
    c = shapely.union_all(polys).centroid
    p(1.0, "")
    out = {"level": level, "spread": float(NMAD * np.median(np.abs(z - level))),
           "p10": float(np.percentile(z, 10)), "p90": float(np.percentile(z, 90)), "edge_cells": int(len(z)),
           "slope_bias": float(np.median(z2) - level), "extrapolated": int(both.sum()),
           "x": float(c.x), "y": float(c.y)}
    if points:
        out["points"] = near
    return out


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
    centre of the data), ``spread``, ``p10``, ``p90``, ``edge_cells``, ``slope_bias``. Returns the record (``chunk.meta["water"]``)."""
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
              "slope_bias": info.get("slope_bias"),
              "x": float(x), "y": float(y), "when": datetime.now().isoformat(timespec="seconds")}
    if source == "hand":
        record.update(spread=None, edge_cells=None, p10=None, p90=None, slope_bias=None)
    stamps.stamp_entry(chunk, record)
    old = chunk.meta.get("water") or {}
    if old.get("correction"):  # the correction made with the earlier level stays told
        record["correction"] = old["correction"]
    if old.get("basins"):  # the basins are stamped one by one: they stay as they are
        record["basins"] = old["basins"]
    for key in ("date", "factor_fit"):
        if old.get(key) is not None:
            record[key] = old[key]
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
    with no point below the level (nothing written). The whole cloud is read into memory (a few copies of it): fine for
    trenches and wrecks, heavy for clouds of tens of millions of points."""
    from ..core import versions
    from ..core.pointcloud import PointCloud

    p = as_progress(progress)
    lv = level_of(chunk)
    if lv is None:
        raise WaterError(f"the chunk {chunk.label} has no water level: set it first (from the edge or by hand)",
                         f"il chunk {chunk.label} non ha un livello dell'acqua: impostalo prima (dalla riva o a mano)")
    n_given = n
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
    basins = basins_of(chunk)
    if basins:  # a level and a factor for every point, from its basin (NaN: no water there)
        Lp, n_p, owner = surface_at(chunk, world[:, :2], basins, owner=True)
        if n_given is not None:
            n_p = np.where(np.isfinite(n_p), n, n_p)
        below = np.isfinite(Lp) & (world[:, 2] < Lp)
    else:
        Lp, n_p = np.full(len(world), L), np.full(len(world), n)
        below = world[:, 2] < L
    nb = int(below.sum())
    if nb == 0:
        raise WaterError(f"no point of the dense cloud of {chunk.label} lies below the level {L:.3f}: nothing to "
                         "correct", f"nessun punto della nuvola densa di {chunk.label} è sotto il livello "
                         f"{L:.3f}: niente da correggere")
    p.check()
    world = world[below]
    nb_n = n_p[below]
    world[:, 2] = corrected(world[:, 2], Lp[below], nb_n)
    xyz = pc.xyz.copy()
    xyz[below] = chunk.to_internal(world)  # the points above the level keep their coordinates to the bit
    out = PointCloud(xyz, pc.rgb, pc.normals, pc.classification, pc.confidence,
                     dict(pc.labels), pc.manual)
    if out.normals is not None:  # z stretched by n below the level: normals by the inverse, in world
        m = np.asarray(chunk.transform, float)[:3, :3]
        r = m / np.cbrt(np.linalg.det(m))
        nw = out.normals[below].astype(np.float64) @ r.T
        nw[:, 2] /= nb_n
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
    record = {"level": L, "n": n, "source": lv.get("source"), "points": int(len(pc)), "below": nb,
              "version": kept["id"] if kept else None, "when": datetime.now().isoformat(timespec="seconds"),
              "note": NOTE}
    if basins:
        record["basins"] = [{"name": b["name"], "n": float(n if n_given is not None else b["n"]),
                             "level": b.get("level"), "below": int((below & (owner == i)).sum())}
                            for i, b in enumerate(basins)]
    chunk.meta["dense"] = {**params, "refraction": record}
    many = basins and len({round(float(b["n"]), 6) for b in basins}) > 1 and n_given is None
    name = (versions.label("Nuvola corretta per la rifrazione ({0} bacini)", len(basins)) if many else
            versions.label("Nuvola corretta per la rifrazione (n {0})", _decimal(n if not basins or n_given is not None
                                                                              else float(basins[0]["n"]))))[:80]
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


# ------------------------------------------------------------------ several levels: basins, shore planes
def plane_from_shore(points) -> dict:
    """The water surface through points picked on the shoreline (world ``(x, y, z)``, at least 3): the plane
    ``z = level + a (x - x0) + b (y - y0)`` by least squares around their centre ``(x0, y0)``. Returns ``{"level",
    "a", "b", "x0", "y0", "residuals" (per point, m), "rmse", "tilt_deg", "points"}``. A plane needs three points
    not on a line; with the points nearly on a line the slope across it is unknown and :class:`WaterError` says so."""
    pts = np.asarray(points, np.float64).reshape(-1, 3)
    if len(pts) < 3 or not np.isfinite(pts).all():
        raise WaterError("a plane of the water needs at least three points of the shoreline",
                         "un piano dell'acqua vuole almeno tre punti della riva")
    x0, y0 = float(pts[:, 0].mean()), float(pts[:, 1].mean())
    A = np.column_stack([np.ones(len(pts)), pts[:, 0] - x0, pts[:, 1] - y0])
    sv = np.linalg.svd(A[:, 1:], compute_uv=False)
    if sv[-1] < 1e-6 * max(sv[0], 1e-12) or sv[-1] < 1e-6:
        raise WaterError("the points of the shoreline lie on a line: the tilt of the water across it is unknown",
                         "i punti della riva sono in linea: l'inclinazione dell'acqua di traverso non si conosce")
    (c, a, b), *_ = np.linalg.lstsq(A, pts[:, 2], rcond=None)
    res = pts[:, 2] - A @ np.array([c, a, b])
    return {"level": float(c), "a": float(a), "b": float(b), "x0": x0, "y0": y0,
            "residuals": [float(v) for v in res], "rmse": float(np.sqrt(np.mean(res ** 2))),
            "tilt_deg": float(np.degrees(np.arctan(np.hypot(a, b)))), "points": int(len(pts))}


def _polygon_wkt(polygon) -> str | None:
    """A polygon given as shapely geometry, WKT, a list of (x, y) or a list of rings, as WKT (None: everywhere)."""
    if polygon is None:
        return None
    import shapely
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.geometry.base import BaseGeometry

    if isinstance(polygon, str):
        geom = shapely.from_wkt(polygon)
    elif isinstance(polygon, BaseGeometry):
        geom = polygon
    else:  # a ring of (x, y), or a list of rings
        first = np.asarray(polygon[0], dtype=float)
        rings = [np.asarray(r, float)[:, :2] for r in polygon] if first.ndim == 2 else [np.asarray(polygon, float)[:, :2]]
        geom = MultiPolygon([Polygon(r) for r in rings]) if len(rings) > 1 else Polygon(rings[0])
    if geom.is_empty or geom.area <= 0:
        raise WaterError("the polygon of a basin is empty", "il poligono di un bacino è vuoto")
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom.wkt


def basin(name: str, level: float | None = None, n: float = N_SEA, polygon=None, shore=None,
          source: str | None = None) -> dict:
    """A basin of water for :func:`set_basins`: ``level`` a world height (horizontal water) or ``shore`` the points of
    its shoreline (a plane, :func:`plane_from_shore`), ``n`` its effective factor, ``polygon`` where it is (shapely,
    WKT, a ring of (x, y) or a list of rings; None: every point no other basin takes)."""
    n = _check_n(n)
    out = {"name": str(name), "n": n, "polygon": _polygon_wkt(polygon), "source": source}
    if shore is not None:
        fit = plane_from_shore(shore)
        out.update(level=fit["level"], a=fit["a"], b=fit["b"], x0=fit["x0"], y0=fit["y0"], shore=fit,
                   source=source or "shore")
    elif level is not None and np.isfinite(float(level)):
        out.update(level=float(level), a=0.0, b=0.0, x0=0.0, y0=0.0, source=source or "hand")
    else:
        raise WaterError(f"basin {name}: a level or the points of its shoreline are needed",
                         f"bacino {name}: serve un livello o i punti della sua riva")
    return out


def _moved(chunk: "Chunk", entry: dict):
    """The world of ``entry`` (stamped) to the world of ``chunk`` now: a 4x4 matrix, None when it did not move."""
    from ..core import stamps

    built_in = entry.get("built_in")
    if built_in is None or stamps.same_transform(built_in, chunk.transform):
        return None
    return np.asarray(chunk.transform, float).reshape(4, 4) @ np.linalg.inv(np.asarray(built_in, float).reshape(4, 4))


def set_basins(chunk: "Chunk", basins, n: float | None = None) -> list[dict]:
    """The basins of the water of ``chunk`` (a list of :func:`basin` records; [] or None: none, back to the single
    level), each stamped with the transform of now (they move with the chunk, as the level). When the chunk has no
    level yet, the level of the first basin at its centre becomes it (with ``n`` or the first basin's). At most one
    basin without a polygon. Returns the basins stored."""
    from ..core import stamps

    basins = [dict(b) for b in (basins or [])]
    if sum(b.get("polygon") is None for b in basins) > 1:
        raise WaterError("only one basin can be without a polygon (the water everywhere else)",
                         "un solo bacino può essere senza poligono (l'acqua dappertutto altrove)")
    names = [b["name"] for b in basins]
    if len(set(names)) != len(names):
        raise WaterError("two basins with the same name", "due bacini con lo stesso nome")
    for b in basins:
        b["n"] = _check_n(b["n"])
        stamps.stamp_entry(chunk, b)
    rec = chunk.meta.get("water")
    if not isinstance(rec, dict) or rec.get("level") is None:
        if not basins:
            return []
        b0 = basins[0]
        x, y = (b0["x0"], b0["y0"]) if b0.get("a") or b0.get("b") else _centre(chunk)
        set_level(chunk, b0["level"], source="hand", n=n if n is not None else b0["n"], x=x, y=y)
        rec = chunk.meta["water"]
    if basins:
        rec["basins"] = basins
    else:
        rec.pop("basins", None)
    return [dict(b) for b in basins]


def basins_of(chunk: "Chunk") -> list[dict]:
    """The basins of ``chunk`` in its world now (moved with the chunk when its transform changed): records with
    ``name``, ``n``, ``level``, ``a``, ``b``, ``x0``, ``y0`` (the plane ``z = level + a (x - x0) + b (y - y0)``) and
    ``polygon`` (WKT or None); [] without basins."""
    import shapely
    from shapely import affinity

    rec = chunk.meta.get("water")
    if not isinstance(rec, dict):
        return []
    out = []
    for b in rec.get("basins") or []:
        b = dict(b)
        M = _moved(chunk, b)
        if M is not None:  # three points of the plane and the outline moved with the chunk
            x0, y0 = float(b.get("x0") or 0.0), float(b.get("y0") or 0.0)
            pts = np.array([[x0, y0], [x0 + 1.0, y0], [x0, y0 + 1.0]])
            z = b["level"] + b["a"] * (pts[:, 0] - x0) + b["b"] * (pts[:, 1] - y0)
            w = np.column_stack([pts, z, np.ones(3)]) @ M.T
            fit = plane_from_shore(w[:, :3])
            b.update(level=fit["level"], a=fit["a"], b=fit["b"], x0=fit["x0"], y0=fit["y0"])
            if b.get("polygon"):
                g = shapely.from_wkt(b["polygon"])
                g = affinity.affine_transform(g, [M[0, 0], M[0, 1], M[1, 0], M[1, 1], M[0, 3] + M[0, 2] * b["level"],
                                                  M[1, 3] + M[1, 2] * b["level"]])
                b["polygon"] = g.wkt
        out.append(b)
    return out


def surface_at(chunk: "Chunk", xy, basins=None, owner: bool = False):
    """The level ``L`` and the factor ``n`` of the water at the world points ``xy`` ((m, 2)): from the basin whose
    polygon holds each point (the first one, in their order), else the basin without polygon, else NaN (no water
    there); without basins the single level and ``n`` of the chunk everywhere (NaN without a level). ``owner``: also
    the index of the basin of each point (-1)."""
    import shapely

    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    basins = basins_of(chunk) if basins is None else basins
    L = np.full(len(xy), np.nan)
    n = np.full(len(xy), np.nan)
    who = np.full(len(xy), -1, np.int64)
    if not basins:
        lv = level_of(chunk)
        if lv is not None:
            L[:], n[:] = lv["level"], lv["n"]
            who[:] = 0
        return (L, n, who) if owner else (L, n)
    free = None
    for i, b in enumerate(basins):
        if b.get("polygon") is None:
            free = i
            continue
        g = shapely.from_wkt(b["polygon"])
        inside = (who < 0) & shapely.contains_xy(g, xy[:, 0], xy[:, 1])
        who[inside] = i
    if free is not None:
        who[who < 0] = free
    for i, b in enumerate(basins):
        sel = who == i
        L[sel] = b["level"] + b["a"] * (xy[sel, 0] - b["x0"]) + b["b"] * (xy[sel, 1] - b["y0"])
        n[sel] = b["n"]
    return (L, n, who) if owner else (L, n)


# ------------------------------------------------------------------ the factor from control points
def _apparent_heights(chunk: "Chunk", xy: np.ndarray, source: str) -> np.ndarray:
    """The apparent heights (seen through the water, not corrected) at ``xy``: the DEM (``dem``) or the dense cloud
    (``cloud``: the median of the points within two point spacings)."""
    if (chunk.meta.get("dense") or {}).get("refraction"):
        raise WaterError("the active dense cloud is already corrected for the refraction: make the cloud before it "
                         "active to read the apparent heights", "la nuvola densa attiva è già corretta per la "
                         "rifrazione: rendi attiva quella di prima per leggere le quote apparenti")
    if source == "dem":
        return _heights_at(_dem(chunk), xy)
    from scipy.spatial import cKDTree

    pc = chunk.point_cloud
    if pc is None:
        raise WaterError(f"the chunk {chunk.label} has no dense cloud", f"il chunk {chunk.label} non ha una nuvola "
                         "densa")
    world = chunk.to_world(pc.xyz)
    tree = cKDTree(world[:, :2])
    d, _ = tree.query(world[np.random.default_rng(0).choice(len(world), min(len(world), 5000), replace=False), :2], k=2)
    radius = 2.0 * float(np.median(d[:, 1]))
    out = np.full(len(xy), np.nan)
    for k, idx in enumerate(tree.query_ball_point(xy, radius)):
        if idx:
            out[k] = float(np.median(world[idx, 2]))
    return out


def control_points(chunk: "Chunk", markers=None, points=None, source: str = "dem") -> list[dict]:
    """The control points for :func:`estimate_factor`: ``markers`` (labels; their reference is the true position,
    their triangulated position - through the water - the apparent one) and ``points`` (rows ``(x, y, z_true)``
    whose apparent height is read on the DEM or the dense cloud, ``source``, or ``(x, y, z_true, z_apparent)``, or
    dicts with ``label``, ``x``, ``y``, ``z`` and optionally ``apparent``)."""
    out = []
    for label in [] if markers is None else markers:
        m = chunk.marker(label)
        pos = m.position
        refxyz = m.reference.location if m.reference is not None else None
        if refxyz is None or pos is None:
            raise WaterError(f"marker {label}: a reference and a position are needed",
                             f"marker {label}: servono un riferimento e una posizione")
        app = chunk.to_world(np.asarray(pos, float)[None])[0]
        t = np.asarray(refxyz, float).reshape(-1)[:3]
        out.append({"label": str(label), "x": float(t[0]), "y": float(t[1]), "z": float(t[2]), "apparent": float(app[2])})
    rows, need = [], []
    for k, row in enumerate([] if points is None else points):
        if isinstance(row, dict):
            r = {"label": str(row.get("label", f"P{k + 1}")), "x": float(row["x"]), "y": float(row["y"]),
                 "z": float(row["z"]), "apparent": row.get("apparent")}
        else:
            v = [float(a) for a in row]
            r = {"label": f"P{k + 1}", "x": v[0], "y": v[1], "z": v[2], "apparent": v[3] if len(v) > 3 else None}
        if r["apparent"] is None or not np.isfinite(float(r["apparent"])):
            need.append(len(rows))
        rows.append(r)
    if need:
        z = _apparent_heights(chunk, np.array([[rows[i]["x"], rows[i]["y"]] for i in need]), source)
        for i, v in zip(need, z):
            rows[i]["apparent"] = float(v)
    return out + rows


def estimate_factor(chunk: "Chunk", controls, fit_level: bool = False, basin_name: str | None = None) -> dict:
    """The effective refraction factor ``n`` from ``controls`` (:func:`control_points`): least squares on
    ``L - z_true = n (L - z_apparent)`` with the level ``L`` of the water at each point (:func:`surface_at`;
    ``basin_name``: only the points of that basin), or - ``fit_level`` - ``n`` and a horizontal level together
    (``z_true = L (1 - n) + n z_apparent``, at least 3 points). Points above the water or without an apparent height
    are left out (said in ``left_out``). Returns ``{"n", "level", "rmse", "points", "residuals": [{"label", "x",
    "y", "z", "apparent", "corrected", "residual"}], "left_out"}``: the residual is true minus corrected height."""
    rows = [dict(r) for r in controls]
    if not rows:
        raise WaterError("no control point", "nessun punto di controllo")
    xy = np.array([[r["x"], r["y"]] for r in rows], float)
    zt = np.array([r["z"] for r in rows], float)
    za = np.array([np.nan if r.get("apparent") is None else r["apparent"] for r in rows], float)
    basins = basins_of(chunk)
    L, _n, who = surface_at(chunk, xy, basins, owner=True)
    keep = np.isfinite(za) & np.isfinite(zt)
    if basin_name is not None:
        idx = next((i for i, b in enumerate(basins) if b["name"] == basin_name), None)
        if idx is None:
            raise WaterError(f"no basin {basin_name}", f"nessun bacino {basin_name}")
        keep &= who == idx
    if fit_level:
        keep_fit = keep
        if keep_fit.sum() < 3:
            raise WaterError("the level and the factor together need three control points under the water",
                             "livello e fattore insieme vogliono tre punti di controllo sott'acqua")
        A = np.column_stack([np.ones(keep_fit.sum()), za[keep_fit]])
        (alpha, nfit), *_ = np.linalg.lstsq(A, zt[keep_fit], rcond=None)
        if abs(1.0 - nfit) < 1e-6:
            raise WaterError("the control points do not tell the level (factor 1)",
                             "i punti di controllo non dicono il livello (fattore 1)")
        level = float(alpha / (1.0 - nfit))
        L = np.full(len(rows), level)
        keep = keep_fit & (za < level)
    else:
        level = None
        keep &= np.isfinite(L) & (za < L)
        if keep.sum() < 1:
            raise WaterError("no control point under the water with an apparent height",
                             "nessun punto di controllo sott'acqua con una quota apparente")
        da, dt = L[keep] - za[keep], L[keep] - zt[keep]
        nfit = float(np.sum(da * dt) / np.sum(da * da))
    nfit = float(nfit)
    corr = np.where(np.isfinite(L) & (za < L), L - nfit * (L - za), za)
    res = zt - corr
    out_rows = []
    for k, r in enumerate(rows):
        out_rows.append({"label": r.get("label", f"P{k + 1}"), "x": float(xy[k, 0]), "y": float(xy[k, 1]),
                         "z": float(zt[k]), "apparent": None if not np.isfinite(za[k]) else float(za[k]),
                         "corrected": None if not np.isfinite(corr[k]) else float(corr[k]),
                         "residual": None if not keep[k] else float(res[k]), "used": bool(keep[k])})
    used = res[keep]
    return {"n": nfit, "level": level, "rmse": float(np.sqrt(np.mean(used ** 2))) if len(used) else None,
            "points": int(keep.sum()), "residuals": out_rows,
            "left_out": [r["label"] for r, k in zip(out_rows, keep) if not k], "basin": basin_name,
            "in_range": bool(N_RANGE[0] <= nfit <= N_RANGE[1])}


def apply_factor(chunk: "Chunk", fit: dict) -> dict:
    """The factor of :func:`estimate_factor` stored: on its basin, else on the level of the chunk (and the level
    too when it was fitted). Returns the record of the water."""
    n = _check_n(fit["n"])
    rec = chunk.meta.get("water")
    if fit.get("basin"):
        basins = (rec or {}).get("basins") or []
        for b in basins:
            if b["name"] == fit["basin"]:
                b["n"] = n
                break
        else:
            raise WaterError(f"no basin {fit['basin']}", f"nessun bacino {fit['basin']}")
    elif fit.get("level") is not None or not isinstance(rec, dict) or rec.get("level") is None:
        if fit.get("level") is None:
            raise WaterError(f"the chunk {chunk.label} has no water level", f"il chunk {chunk.label} non ha un "
                             "livello dell'acqua")
        set_level(chunk, fit["level"], source="hand", n=n)
        rec = chunk.meta["water"]
    else:
        rec["n"] = n
    rec["factor_fit"] = {k: fit[k] for k in ("n", "level", "rmse", "points", "basin")}
    rec["factor_fit"]["residuals"] = [{k: r[k] for k in ("label", "residual")} for r in fit["residuals"]]
    return dict(rec)


# ------------------------------------------------------------------ a level and a factor per chunk or per date
def read_table(path) -> list[dict]:
    """Rows of a CSV of water levels (``;`` or ``,``): columns ``chunk`` or ``date`` (YYYY-MM-DD), ``level`` (may be
    empty: the factor alone) and ``n``. Decimal commas are read with ``;`` separators."""
    import csv

    text = Path(path).read_text(encoding="utf-8-sig")
    delim = ";" if text.count(";") > text.count(",") / 2 and ";" in text else ","
    rows = []
    for row in csv.DictReader(text.splitlines(), delimiter=delim):
        r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}

        def num(v):
            return float(v.replace(",", ".")) if v else None

        rows.append({"chunk": r.get("chunk") or None, "date": r.get("date") or r.get("data") or None,
                     "level": num(r.get("level") or r.get("livello") or ""), "n": num(r.get("n") or "")})
    return rows


def apply_table(doc, rows) -> list[dict]:
    """A level and a factor for several chunks: each row ``{"chunk"}`` (label or key) or ``{"date"}`` (the chunks
    surveyed that day, :func:`..archaeo.timeline.phase_date`), with ``level`` (None: the level kept, only ``n``) and
    ``n``. Returns what was set, one entry per chunk; :class:`WaterError` (nothing written) when a row matches no chunk
    or sets ``n`` alone on a chunk without a level."""
    from datetime import date as _date

    from ..archaeo.timeline import phase_date

    plan = []
    for row in rows:
        if row.get("chunk"):
            found = [c for c in doc.chunks if c.label == str(row["chunk"])] or \
                [c for c in doc.chunks if str(c.key) == str(row["chunk"])]
        elif row.get("date"):
            day = _date.fromisoformat(str(row["date"])[:10])
            found = [c for c in doc.chunks if (phase_date(c) is not None and phase_date(c).date() == day)]
        else:
            raise WaterError("a row without chunk or date", "una riga senza chunk né data")
        if not found:
            what = row.get("chunk") or row.get("date")
            raise WaterError(f"no chunk for {what}", f"nessun chunk per {what}")
        for ch in found:
            if row.get("level") is None and level_of(ch) is None:
                raise WaterError(f"{ch.label}: no level to give the factor to", f"{ch.label}: nessun livello a cui "
                                 "dare il fattore")
            plan.append((ch, row))
    done = []
    for ch, row in plan:
        n = row.get("n")
        if row.get("level") is not None:
            rec = set_level(ch, row["level"], source="hand", n=n if n is not None else (level_of(ch) or {}).get("n", N_SEA))
        else:
            ch.meta["water"]["n"] = _check_n(n)
            rec = dict(ch.meta["water"])
        if row.get("date"):
            ch.meta["water"]["date"] = str(row["date"])[:10]
        done.append({"chunk": ch.label, "level": rec["level"], "n": float(ch.meta["water"]["n"]),
                     "date": row.get("date")})
    return done


__all__ = ["N_SEA", "N_FRESH", "N_RANGE", "SOURCES", "LAND_STEP", "MIN_EDGE_CELLS", "NOTE", "NOTE_EN", "WaterError", "corrected",
           "water_polygons", "edge_samples", "edge_points", "level_from_edge", "set_level", "level_of", "correct_cloud",
           "plane_from_shore", "basin", "set_basins", "basins_of", "surface_at", "control_points", "estimate_factor",
           "apply_factor", "read_table", "apply_table"]
