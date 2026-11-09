"""The height of a chunk on stable ground: a *height tie* between two chunks.

Two surveys of the same site often agree in plan (the GPS of a drone puts them within a few centimetres of each other)
and not in height (tens of centimetres, metres). Where the ground did not change between them - the sand around an
excavation, a road, a pavement - their DEMs must be at the same height: :func:`measure` reads both DEMs as they are
placed now (no horizontal shift) and finds how much higher chunk B is than chunk A there (:func:`..archaeo.coreg.
height_offset`: the median of the differences on the stable ground, or with ``plane`` a plane).

The result is kept as a :class:`HeightTie` in ``doc.meta["height_ties"]`` (not among the point ties of
:mod:`.ties`): one anchor for an offset, three for a plane (the three stable cells that span the largest triangle of
the stable area). Each anchor holds the point in the internal frame of A (on A's DEM) and the point in the internal
frame of B (the same world x, y, at the height of A plus the offset found there), so that the tie survives later
transforms of the chunks the way the point ties do; in the joint solve each anchor is one observation of z only, of
precision ``max(NMAD / sqrt(min(cells, 100)), 5 mm)`` (``sqrt(3)`` times that for each anchor of a plane).

**Stable area**: a free-drawing polygon layer (of A, else of B; the colour masks are such layers), a GeoPackage /
GeoJSON / DXF file, or nothing (the stable-ground rules of the coregistration: slope, cells that agree); ``exclude``
the same, optional.

**Placement**: each DEM is read in the current world of the chunk (:func:`dem_in_world`): a DEM built in another
transform of its chunk (its stamp, :mod:`..core.stamps`) is moved with the chunk in plan and in height, as the 2D view
places it; between two projected systems it is reprojected; a local frame and a projected system are refused with the
words of the ties. No Qt here.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from ..archaeo import coreg
from ..core import crs as crs_mod
from ..core import free_drawings, stamps
from ..core.progress import Progress, as_progress
from . import ties

if TYPE_CHECKING:  # pragma: no cover
    from ..core.chunk import Chunk
    from ..core.document import Document

META_KEY = "height_ties"
LABEL = "quota stabile"  # the name of a height tie: data of the project (as the labels of the ties), not UI text
MIN_PRECISION = 0.005  # metres: the least precision of a height tie
INDEPENDENT_CELLS = 100  # at most this many cells taken as independent samples (neighbouring cells are correlated)
NORTH_UP = 1e-9  # a moved grid whose rotation terms are below this share of its cell is still north up
BOUNDS_MARGIN = 4  # cells of the reference: the DEM of B read this far beyond the reference
HULL_VERTICES = 60  # at most this many vertices of the stable hull tried for the largest triangle


@dataclass
class HeightTie:
    """The height of chunk ``b`` over chunk ``a`` on stable ground (see the module docstring).

    ``anchors``: ``[{"a": [x, y, z] internal of A, "b": [x, y, z] internal of B, "precision": metres}]`` (one, or three
    for a plane); ``dz`` (B - A at the centre of the measure), ``nmad`` (after the correction), ``cells`` (stable cells
    of the sample); ``area``/``exclude``: where the stable ground was looked for (``{"layer": name}``, ``{"file":
    path}`` or None = automatic / nothing left out); ``placed``: the transforms of A and B when measured (a later
    horizontal move of one against the other calls for a new measure); ``when``: ISO time."""

    id: str
    name: str
    a: int
    b: int
    anchors: list[dict]
    dz: float
    nmad: float
    cells: int
    plane: bool = False
    active: bool = True
    area: dict | None = None
    exclude: dict | None = None
    placed: dict = field(default_factory=dict)
    when: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "a": int(self.a), "b": int(self.b),
                "anchors": [{"a": [float(v) for v in x["a"]], "b": [float(v) for v in x["b"]],
                             "precision": float(x["precision"])} for x in self.anchors],
                "dz": float(self.dz), "nmad": float(self.nmad), "cells": int(self.cells), "plane": bool(self.plane),
                "active": bool(self.active), "area": None if self.area is None else dict(self.area),
                "exclude": None if self.exclude is None else dict(self.exclude),
                "placed": {k: np.asarray(v, float).tolist() for k, v in (self.placed or {}).items()},
                "when": self.when}

    @classmethod
    def from_dict(cls, d: dict) -> "HeightTie":
        return cls(id=str(d["id"]), name=str(d.get("name", "")), a=int(d["a"]), b=int(d["b"]),
                   anchors=[{"a": [float(v) for v in x["a"]], "b": [float(v) for v in x["b"]],
                             "precision": float(x.get("precision", MIN_PRECISION))} for x in d.get("anchors") or []],
                   dz=float(d.get("dz", 0.0)), nmad=float(d.get("nmad", 0.0)), cells=int(d.get("cells", 0)),
                   plane=bool(d.get("plane", False)), active=bool(d.get("active", True)),
                   area=d.get("area"), exclude=d.get("exclude"), placed=dict(d.get("placed") or {}),
                   when=str(d.get("when", "")))


# --------------------------------------------------------------------------- the DEM in the world
def _system_of(system) -> tuple[str | None, str]:
    """``(crs, label)`` of ``system``: a chunk (its system and label) or a system."""
    if hasattr(system, "crs") and hasattr(system, "label"):
        return system.crs, system.label
    return system, str(system or crs_mod.LOCAL)


def _north_up(z: np.ndarray, tr, src_crs: str | None, dst_crs: str | None, bounds=None):
    """``z`` on the affine ``tr`` (rotated, or in the system ``src_crs``) resampled bilinearly onto a north-up grid of
    the same cell size in ``dst_crs``; two local frames (or two equal systems) by the rotation only. ``bounds``
    ``(left, bottom, right, top)`` in ``dst_crs``: the grid clipped to them (None when nothing is left)."""
    import rasterio.crs
    from affine import Affine
    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    h, w = z.shape
    corners = np.array([tr @ (c, r) for c, r in ((0, 0), (w, 0), (0, h), (w, h))], float)
    same = src_crs == dst_crs or (crs_mod.is_local(src_crs) and crs_mod.is_local(dst_crs))
    if same:
        dummy = rasterio.crs.CRS.from_epsg(3857)  # a frame for the warp only: the same on both sides
        s_crs = d_crs = dummy
    else:
        s_crs = rasterio.crs.CRS.from_user_input(crs_mod.horizontal(src_crs))
        d_crs = rasterio.crs.CRS.from_user_input(crs_mod.horizontal(dst_crs))
        pts = np.column_stack([corners, np.zeros(len(corners))])
        corners = crs_mod.transform_points(pts, crs_mod.horizontal(src_crs), crs_mod.horizontal(dst_crs))[:, :2]
    cell = math.sqrt(abs(tr.a * tr.e - tr.b * tr.d))
    x0, x1 = corners[:, 0].min(), corners[:, 0].max()
    y0, y1 = corners[:, 1].min(), corners[:, 1].max()
    if bounds is not None:
        x0, y0, x1, y1 = max(x0, bounds[0]), max(y0, bounds[1]), min(x1, bounds[2]), min(y1, bounds[3])
        if x1 <= x0 or y1 <= y0:
            return None
    nw = max(1, int(math.ceil((x1 - x0) / cell)))
    nh = max(1, int(math.ceil((y1 - y0) / cell)))
    dst_tr = Affine(cell, 0.0, x0, 0.0, -cell, y1)
    out = np.full((nh, nw), np.nan, np.float32)
    reproject(np.ascontiguousarray(z, np.float32), out, src_transform=tr, src_crs=s_crs, src_nodata=np.nan,
              dst_transform=dst_tr, dst_crs=d_crs, dst_nodata=np.nan, resampling=Resampling.bilinear)
    return out, dst_tr


def _read_window(path, bounds=None):
    """``(heights float32 with NaN, transform)`` of the DEM ``path``, only the cells within ``bounds`` (left, bottom,
    right, top in the coordinates of the file) when given (an empty array when they miss it), as :func:`coreg._read`."""
    import rasterio
    from rasterio.enums import MaskFlags
    from rasterio.windows import Window, from_bounds

    with rasterio.open(path) as src:
        window = None
        if bounds is not None:
            win = from_bounds(*bounds, transform=src.transform)
            c0, r0 = max(0, int(math.floor(win.col_off))), max(0, int(math.floor(win.row_off)))
            c1 = min(src.width, int(math.ceil(win.col_off + win.width)))
            r1 = min(src.height, int(math.ceil(win.row_off + win.height)))
            if c1 <= c0 or r1 <= r0:
                return np.zeros((0, 0), np.float32), src.transform
            window = Window(c0, r0, c1 - c0, r1 - r0)
        z = src.read(1, window=window, out_dtype="float32")
        tr = src.window_transform(window) if window is not None else src.transform
        nodata = src.nodata
        if nodata is not None and not math.isnan(nodata):
            z[z == np.float32(nodata)] = np.nan
        if MaskFlags.per_dataset in src.mask_flag_enums[0] and MaskFlags.nodata not in src.mask_flag_enums[0]:
            z[src.read_masks(1, window=window) == 0] = np.nan
        return z, tr


def _box_through(bounds, fn) -> tuple[float, float, float, float]:
    """The box ``(left, bottom, right, top)`` around the corners and edge middles of ``bounds`` carried by ``fn``
    (``(n, 2) -> (n, 2)``)."""
    l, b, r, t = bounds
    mx, my = (l + r) / 2, (b + t) / 2
    pts = fn(np.array([[l, b], [r, b], [l, t], [r, t], [mx, b], [mx, t], [l, my], [r, my]], float))
    return float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max())


def dem_in_world(chunk: "Chunk", system, bounds=None) -> tuple[np.ndarray, object]:
    """The DEM of ``chunk`` in its current world, in ``system`` (a system, or a chunk: its system): ``(heights float32
    with NaN, north-up affine)``. A DEM built in another transform of the chunk (its stamp) is moved with the chunk -
    the cells in plan by ``T_now inv(T_built)``, the heights ``z' = m20 x + m21 y + m22 z + m23`` - as the 2D view
    places it (``gui.chunk_views.overlay_image``); a rotation in plan resamples it north up. Between two projected
    systems it is reprojected (heights as they are); a local frame and a projected system: :class:`.ties.TieError`.
    ``bounds`` ``(left, bottom, right, top)`` in ``system``: only the part of the DEM there is read and warped (memory
    bounded by the extent asked, not by the DEM); an empty array when the DEM misses them. ``ValueError`` when the
    chunk has no DEM."""
    from affine import Affine

    dst_crs, dst_label = _system_of(system)
    if not ties._compatible(chunk.crs, dst_crs):
        raise ties._not_same_frame(chunk.label, chunk.crs, dst_label, dst_crs)
    path = chunk.dem_path
    if path is None or not Path(path).exists():
        raise ValueError(f"the chunk {chunk.label} has no DEM")
    same = chunk.crs == dst_crs or (crs_mod.is_local(chunk.crs) and crs_mod.is_local(dst_crs))
    built = stamps.stamp_of(chunk, "dem")
    move = None
    if built is not None and not stamps.same_transform(built, chunk.transform):
        move = np.asarray(chunk.transform, float) @ np.linalg.inv(np.asarray(built, float))
    file_bounds = None
    if bounds is not None:  # the bounds asked, carried back to the coordinates of the file
        file_bounds = tuple(float(v) for v in bounds)
        if not same:
            file_bounds = _box_through(file_bounds, lambda p: crs_mod.transform_points(
                np.column_stack([p, np.zeros(len(p))]), crs_mod.horizontal(dst_crs),
                crs_mod.horizontal(chunk.crs))[:, :2])
        if move is not None:
            back = np.linalg.inv(move)
            file_bounds = _box_through(file_bounds, lambda p: p @ back[:2, :2].T + back[:2, 3])
    z, tr = _read_window(path, file_bounds)
    if not z.size:
        return z, tr
    if move is not None:
        h, w = z.shape
        cols = np.arange(w) + 0.5
        for r0 in range(0, h, coreg.BLOCK_ROWS):  # the heights move with the chunk, a block of rows at a time
            r1 = min(h, r0 + coreg.BLOCK_ROWS)
            rows = (np.arange(r0, r1) + 0.5)[:, None]
            x = tr.a * cols[None, :] + tr.b * rows + tr.c
            y = tr.d * cols[None, :] + tr.e * rows + tr.f
            z[r0:r1] = (move[2, 0] * x + move[2, 1] * y + move[2, 2] * z[r0:r1] + move[2, 3]).astype(np.float32)
        tr = Affine(move[0, 0], move[0, 1], move[0, 3], move[1, 0], move[1, 1], move[1, 3]) @ tr
    rotated = abs(tr.b) > NORTH_UP * abs(tr.a) or abs(tr.d) > NORTH_UP * abs(tr.e)
    if rotated or not same:
        found = _north_up(z, tr, chunk.crs, dst_crs, bounds)
        if found is None:
            return np.zeros((0, 0), np.float32), tr
        z, tr = found
    elif tr.b or tr.d:
        tr = Affine(tr.a, 0.0, tr.c, 0.0, tr.e, tr.f)
    return z, tr


# --------------------------------------------------------------------------- areas
def _area_spec(area) -> dict | None:
    """``area`` as stored: None, ``{"layer": name}`` or ``{"file": path}``. A ``Path``, or a text ending in .gpkg,
    .geojson, .json or .dxf, is a file (checked when read: a misspelt path is an error, never a layer); any other text
    is the name of a layer."""
    if area is None or area == "" or area == {}:
        return None
    if isinstance(area, dict):
        if area.get("layer"):
            return {"layer": str(area["layer"])}
        if area.get("file"):
            return {"file": str(area["file"])}
        raise ValueError("an area is {'layer': name} or {'file': path}")
    if isinstance(area, Path) or Path(str(area)).suffix.lower() in (".gpkg", ".geojson", ".json", ".dxf"):
        return {"file": str(area)}
    return {"layer": str(area)}


def _geometry(doc: "Document", spec: dict | None, chunks: list, system: str | None, what: str):
    """The shapely geometry (in ``system``) of an area ``spec``; None for None. A file that says no system (a DXF, a
    GeoJSON without one) is taken in the system of the chunks; one in a local frame with projected chunks, or the
    reverse, is refused."""
    import shapely
    from shapely.geometry import Polygon

    if spec is None:
        return None
    rings: list[np.ndarray] = []
    if "layer" in spec:
        for chunk in chunks:  # the layer of A, else of B
            found = free_drawings.polygons_in_world(chunk, spec["layer"])
            if found:
                if not ties._compatible(chunk.crs, system):
                    raise ties._not_same_frame(chunk.label, chunk.crs, chunks[0].label, system)
                rings = [ties._to_system(r, chunk.crs, system, chunk, f"the {what} area") for r in found]
                break
        if not rings:
            raise ValueError(f"no polygon in the layer {spec['layer']!r} of the chunks measured ({what} area)")
    else:
        from ..io import free_drawings as io_free

        path = Path(spec["file"])
        if not path.exists():
            raise FileNotFoundError(path)
        for rec in io_free.read(path):
            if rec["kind"] != "polygon":
                continue
            xy = np.array([[p[0], p[1], 0.0] for p in rec["xyz"]], float)
            src = rec.get("crs") or rec.get("crs_unreadable")
            if rec.get("crs_unreadable"):
                raise ValueError(f"the file {path.name} names a system PROJ cannot read: {rec['crs_unreadable']}")
            if src and crs_mod.is_local(src) and not crs_mod.is_local(system):
                raise ValueError(f"the file {path.name} is in a local frame and the chunks are in {system}")
            if src and not crs_mod.is_local(system) and src != system:
                xy = crs_mod.transform_points(xy, crs_mod.horizontal(src), crs_mod.horizontal(system))
            elif src and crs_mod.is_local(system):
                raise ValueError(f"the file {path.name} is in {src} and the chunks are in a local frame")
            rings.append(xy)
        if not rings:
            raise ValueError(f"no polygon in the file {path.name} ({what} area)")
    polys = [Polygon(r[:, :2]) for r in rings if len(r) >= 3]
    polys = [p if p.is_valid else p.buffer(0) for p in polys]
    return shapely.union_all([p for p in polys if not p.is_empty])


# --------------------------------------------------------------------------- measure
def _precision(nmad: float, cells: int) -> float:
    """The precision of a height tie (ruling R2 of the plan)."""
    sigma = nmad / math.sqrt(max(1, min(int(cells), INDEPENDENT_CELLS))) if math.isfinite(nmad) else 0.0
    return max(float(sigma), MIN_PRECISION)


def _largest_triangle(xy: np.ndarray) -> np.ndarray:
    """Indices into ``xy`` of three points spanning the largest triangle (among the vertices of their convex hull)."""
    from scipy.spatial import ConvexHull

    try:
        hull = ConvexHull(xy).vertices
    except Exception as exc:  # noqa: BLE001 - all on a line, or too few
        raise ValueError("the stable ground lies along a line: a plane cannot be measured on it") from exc
    if len(hull) > HULL_VERTICES:
        hull = hull[np.linspace(0, len(hull) - 1, HULL_VERTICES).round().astype(int)]
    p = xy[hull]
    best, out = -1.0, None
    n = len(p)
    for i in range(n - 2):
        for j in range(i + 1, n - 1):
            k = np.arange(j + 1, n)
            area = 0.5 * np.abs((p[j, 0] - p[i, 0]) * (p[k, 1] - p[i, 1]) - (p[k, 0] - p[i, 0]) * (p[j, 1] - p[i, 1]))
            m = int(np.argmax(area))
            if area[m] > best:
                best, out = float(area[m]), (hull[i], hull[j], hull[k[m]])
    return np.array(out)


def measure(doc: "Document", chunk, reference, area=None, exclude=None, plane: bool = False, name: str | None = None,
            progress: Progress | None = None) -> HeightTie:
    """The height of ``chunk`` (B) over ``reference`` (A) on stable ground, as a :class:`HeightTie` (not stored:
    :func:`add`). ``area``/``exclude``: a free-drawing layer name, a file path or ``{"layer"}``/``{"file"}`` (see
    the module docstring); ``plane``: a plane (three anchors) instead of an offset (one). :class:`.ties.TieError` for
    a local frame with a projected system; ``ValueError`` when a chunk has no DEM, the DEMs do not overlap, the stable
    ground is too small."""
    progress = as_progress(progress)
    ca, cb = ties._chunk(doc, reference), ties._chunk(doc, chunk)
    if ca is cb:
        raise ValueError("a chunk cannot be measured against itself: choose another reference")
    if not ties._compatible(cb.crs, ca.crs):
        raise ties._not_same_frame(cb.label, cb.crs, ca.label, ca.crs)
    system = ca.crs
    progress(0.0, f"Stable height of {cb.label} on {ca.label}: reading the DEMs")
    zr, tr_r = dem_in_world(ca, ca)
    hr, wr = zr.shape
    margin = BOUNDS_MARGIN * abs(tr_r.a)
    ra = (tr_r.c, tr_r.c + wr * tr_r.a, tr_r.f + hr * tr_r.e, tr_r.f)
    zd, tr_d = dem_in_world(cb, ca, bounds=(ra[0] - margin, ra[2] - margin, ra[1] + margin, ra[3] + margin))
    hd, wd = zd.shape
    rb = (tr_d.c, tr_d.c + wd * tr_d.a, tr_d.f + hd * tr_d.e, tr_d.f)
    if not zd.size or ra[1] <= rb[0] or rb[1] <= ra[0] or ra[3] <= rb[2] or rb[3] <= ra[2]:
        raise ValueError(f"the DEMs of {cb.label} and {ca.label} do not overlap where they are placed now")
    area_spec, exclude_spec = _area_spec(area), _area_spec(exclude)
    stable_geom = _geometry(doc, area_spec, [ca, cb], system, "stable")
    exclude_geom = _geometry(doc, exclude_spec, [ca, cb], system, "excluded")
    progress(0.3, "Stable height: measuring")
    r = coreg.height_offset(zr, tr_r, zd, tr_d, stable=stable_geom, exclude=exclude_geom, plane=plane)
    rows, cols = r.stable_index
    xs = tr_r.c + (cols + 0.5) * tr_r.a
    ys = tr_r.f + (rows + 0.5) * tr_r.e
    if plane:
        picks = _largest_triangle(np.column_stack([xs, ys]))
    else:  # the stable cell nearest the middle of the stable ground: never a cell of the pit around which it lies
        picks = np.array([int(np.argmin(np.hypot(xs - xs.mean(), ys - ys.mean())))])
    precision = _precision(r.nmad_after, r.cells) * (math.sqrt(3.0) if plane else 1.0)
    anchors = []
    for i in picks:
        x, y = float(xs[i]), float(ys[i])
        za = float(zr[rows[i], cols[i]])  # a cell centre of A's grid: its height
        zb = za + float(r.correction(x, y))  # the height of B there: the offset measured, not one cell of B's noise
        wa = np.array([[x, y, za]])
        wb = ties._to_system(np.array([[x, y, zb]]), system, cb.crs, cb, "the anchors of a stable height")
        anchors.append({"a": [float(v) for v in ca.to_internal(wa)[0]], "b": [float(v) for v in cb.to_internal(wb)[0]],
                        "precision": float(precision)})
    progress(1.0, f"Stable height of {cb.label} on {ca.label}: dz {r.dz:+.4f} m, NMAD {r.nmad_after:.4f} m "
                  f"({r.cells} stable cells)")
    return HeightTie(id="", name=name or "", a=int(ca.key), b=int(cb.key), anchors=anchors, dz=float(r.dz),
                     nmad=float(r.nmad_after), cells=int(r.cells), plane=bool(plane), area=area_spec,
                     exclude=exclude_spec,
                     placed={"a": np.asarray(ca.transform, float).tolist(),
                             "b": np.asarray(cb.transform, float).tolist()},
                     when=datetime.now().isoformat(timespec="seconds"))


# --------------------------------------------------------------------------- storage
def heights_of(doc: "Document") -> list[HeightTie]:
    """The height ties of the document (new objects: change them and give them back with :func:`save_heights`)."""
    return [HeightTie.from_dict(d) for d in (doc.meta.get(META_KEY) or []) if isinstance(d, dict)]


def save_heights(doc: "Document", ties_: list[HeightTie]) -> None:
    """The height ties written in ``doc.meta["height_ties"]``; no key when there are none."""
    if ties_:
        doc.meta[META_KEY] = [t.to_dict() for t in ties_]
    else:
        doc.meta.pop(META_KEY, None)


def _next_name(existing: list[HeightTie]) -> str:
    numbers = [0]
    for t in existing:
        rest = (t.name or "")[len(LABEL):].strip() if (t.name or "").startswith(LABEL) else ""
        if rest.isdigit():
            numbers.append(int(rest))
    return f"{LABEL} {max(numbers) + 1}"


def add(doc: "Document", tie: HeightTie) -> HeightTie:
    """``tie`` stored among the height ties (a new id; ``"quota stabile N"`` without a name); returned."""
    for key in (tie.a, tie.b):
        ties._chunk(doc, int(key))  # KeyError for a chunk not in the document
    existing = heights_of(doc)
    taken = {t.id for t in existing}
    if not tie.id or tie.id in taken:
        while True:
            tie.id = "h" + uuid.uuid4().hex[:8]
            if tie.id not in taken:
                break
    if not tie.name:
        tie.name = _next_name(existing)
    save_heights(doc, existing + [tie])
    return tie


def _find(doc: "Document", tie_id: str) -> tuple[list[HeightTie], int]:
    existing = heights_of(doc)
    for i, t in enumerate(existing):
        if t.id == tie_id:
            return existing, i
    raise KeyError(f"no height tie {tie_id!r} in the document")


def remove(doc: "Document", tie_id: str) -> HeightTie:
    """The height tie ``tie_id`` taken away; returned. KeyError when there is none."""
    existing, i = _find(doc, tie_id)
    gone = existing.pop(i)
    save_heights(doc, existing)
    return gone


def remeasure(doc: "Document", tie_id: str, progress: Progress | None = None) -> HeightTie:
    """The height tie ``tie_id`` measured again where the chunks are now (same chunks, areas and kind; id, name and
    state kept); stored and returned."""
    existing, i = _find(doc, tie_id)
    old = existing[i]
    new = measure(doc, old.b, old.a, area=old.area, exclude=old.exclude, plane=old.plane, name=old.name,
                  progress=progress)
    return replace(doc, tie_id, new)


def replace(doc: "Document", tie_id: str, new: HeightTie) -> HeightTie:
    """The height tie ``tie_id`` replaced by the new measure ``new`` (of the same chunks: :func:`remeasure`, or a
    measure made in a worker with the same parameters), its id, name and state kept; stored and returned. KeyError
    when there is none."""
    existing, i = _find(doc, tie_id)
    old = existing[i]
    new.id, new.name, new.active = old.id, old.name, old.active
    existing[i] = new
    save_heights(doc, existing)
    return new


def horizontal_move(doc: "Document", tie: HeightTie) -> tuple[float, float] | None:
    """How far apart in plan the anchors of ``tie`` are now (the largest, in the system of A) and the limit past which
    it is to be measured again (R3: ``max(2 DEM cells of A, 0.10 m)``, as the solve says it with ``remeasure_height``);
    None when a chunk is gone, has no anchors or the two systems do not mix."""
    chunks = {c.key: c for c in doc.chunks}
    ca, cb = chunks.get(int(tie.a)), chunks.get(int(tie.b))
    if ca is None or cb is None or not tie.anchors or not ties._compatible(cb.crs, ca.crs):
        return None
    worst = 0.0
    try:
        for x in tie.anchors:
            wa = ca.to_world(np.array([x["a"]], float))[0]
            wb = ties._to_system(cb.to_world(np.array([x["b"]], float)), cb.crs, ca.crs, cb,
                                 "the anchors of a stable height")[0]
            worst = max(worst, float(np.hypot(wb[0] - wa[0], wb[1] - wa[1])))
    except (ValueError, ties.TieError):
        return None
    cell = ties._dem_cell(ca)
    return worst, max(ties.REMEASURE_CELLS * cell if cell else 0.0, ties.REMEASURE_MIN)


__all__ = ["META_KEY", "LABEL", "MIN_PRECISION", "HeightTie", "dem_in_world", "measure", "add", "heights_of",
           "save_heights", "remove", "remeasure", "replace", "horizontal_move"]
