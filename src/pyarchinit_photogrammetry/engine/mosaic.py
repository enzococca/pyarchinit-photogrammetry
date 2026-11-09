"""The orthophoto or the DEM of several tied chunks merged into one raster, with the source of every cell.

Several surveys of one site - three years of an excavation, the parts of a long trench flown on different days - once
tied together (:mod:`.ties`) can be shown as one product: the orthophoto (or the DEM) of each chunk placed where its
chunk is now, one over the other in an order of priority.

**Placement** (ruling R8 of the plan): the merged raster is in the system of the first chunk of the list. Each source
raster is placed through its stamp (:mod:`..core.stamps`): one built in another transform of its chunk (a tie solve
after it) is moved with the chunk - in plan by ``T_now inv(T_built)``, the heights of a DEM by ``z' = m20 x + m21 y +
m22 z + m23`` - as the 2D view places it (``gui.chunk_views.overlay_image``), never used where it was built. Between
two projected systems it is reprojected; a local frame and a projected system are refused with the words of the ties
(:class:`.ties.TieError`), and so are two frames that the program joins through their epochs and PROJ only by an
approximation (ITRF2020 with any other, ETRF2014/2020 with ETRF2000: :func:`frame_problem`), since GDAL would warp the
pixels by that approximation. Every source is read through a ``rasterio.vrt.WarpedVRT`` on the grid of the merged raster,
:data:`MOSAIC_BLOCK` cells a side at a time: the memory is bounded by the block, never by the rasters (a DEM of 150 M
cells is never loaded whole). The cell size is by default the finest of the sources.

**Priority**: without areas a cell takes the first chunk of the list that has data there. With areas (polygons, each
for one chunk of the list, in the order of their priority) a cell inside the areas takes the chunk of the first area
that has data there, else the list; ``background`` decides the cells outside every area: ``"faded"`` (the default)
from the list, the orthophoto blended :data:`FADE` toward white (a DEM keeps its heights), ``"none"`` left empty
(transparent, NaN).

**Files** in ``<project>.ppg.files/mosaics/<id>/``: ``mosaic.tif`` (RGBA uint8 for the orthophoto, float32 with NaN for
the DEM), ``sources.tif`` (uint8: 0 none, ``i + 1`` the ``i``-th chunk of the list), ``areas.gpkg`` (layer ``aree``,
attributes ``chunk`` and ``priority``; only with areas) and ``mosaic.json`` (the record). The record is appended to
``doc.meta["mosaics"]``; it is stale (:func:`is_stale`) when the transform of any of its chunks is not the one it was
built with. No Qt here.
"""
from __future__ import annotations

import json
import math
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from ..core import crs as crs_mod
from ..core import stamps
from ..core.progress import Progress, as_progress
from . import ties

if TYPE_CHECKING:  # pragma: no cover
    from ..core.document import Document

META_KEY = "mosaics"
FOLDER = "mosaics"  # inside the files folder of the project
MOSAIC_BLOCK = 2048  # cells a side of a block read and written at a time
FADE = 0.5  # the background outside the areas: blended this much toward white
PRODUCTS = ("orthomosaic", "dem")
BACKGROUNDS = ("faded", "none")
MAX_CHUNKS = 254  # the sources band is uint8: 0 none, 1..254 the chunks of the list
SNAP = 1e-6  # of a cell: the grid snapped to whole cells within this
EDGE_POINTS = 9  # points per side of a source raster carried into the system for its extent
ALPHA_VALID = 128  # an orthophoto cell is data when its alpha (after the resampling) is at least this
WARP_TOLERANCE = 1e-9  # pixels: the warp all but exact, the same cells whatever the block (0 is refused by rasterio).
# It makes GDAL check its approximation at every pixel: a reprojected source warps slower than with GDAL's 0.125 px.
GDAL_CACHE_MB = 256  # GDAL's block cache during a build: each window is read once, a larger cache would not help
TILT_NOTE_DEG = 0.5  # a stale source tilted more than this is placed in plan only: noted, as in the 2D view
LABELS = {"orthomosaic": "Ortofoto unita", "dem": "DEM unito"}  # data names (as the labels of the ties), not UI text
FILES = {"mosaic": "mosaic.tif", "sources": "sources.tif", "areas": "areas.gpkg", "record": "mosaic.json"}
AREAS_LAYER = "aree"


class MosaicError(ValueError):
    """A merged product that cannot be made: ``str()`` is the English message, ``italian`` the Italian one (the window
    shows the Italian one in Italian, the command line the English one), as :class:`.ties.TieError`."""

    def __init__(self, english: str, italian: str):
        super().__init__(english)
        self.english = english
        self.italian = italian


# --------------------------------------------------------------------------- the sources
@dataclass
class _Source:
    """One source raster placed in the system of the merged product."""

    chunk: object
    index: int  # its place in the list (0 = first)
    path: Path
    move: np.ndarray | None  # T_now inv(T_built) when built in another transform
    src_transform: object  # the affine of the file moved in plan (in the system of the chunk)
    same: bool  # the chunk is in the system of the product (or both in a local frame)
    bounds: tuple[float, float, float, float]  # in the system of the product
    cell: float  # its cell size in the system of the product
    count: int
    alpha: int  # 1-based band of its alpha (0: none)
    nodata: float | None
    tilt: float = 0.0  # degrees of tilt of the move (placed in plan only)


def _dummy_crs():
    import rasterio.crs

    return rasterio.crs.CRS.from_epsg(3857)  # a frame for the warp only, the same on both sides


def _vrt_crs(src_crs: str | None, dst_crs: str | None, same: bool):
    import rasterio.crs

    if same:
        if crs_mod.is_local(dst_crs):
            d = _dummy_crs()
        else:
            d = rasterio.crs.CRS.from_user_input(crs_mod.horizontal(dst_crs))
        return d, d
    return (rasterio.crs.CRS.from_user_input(crs_mod.horizontal(src_crs)),
            rasterio.crs.CRS.from_user_input(crs_mod.horizontal(dst_crs)))


def _to_system(xy: np.ndarray, src_crs: str | None, dst_crs: str | None, same: bool, chunk=None) -> np.ndarray:
    """Plan positions from ``src_crs`` to ``dst_crs`` by the program's route (as ``ties._to_system``: positions that
    carry no epoch, a :class:`..gnss.frames.Legacy`); only pairs that :func:`frame_problem` lets through come here."""
    if same:
        return np.asarray(xy, float)
    from ..gnss.frames import Legacy

    pts = np.column_stack([xy, np.zeros(len(xy))])
    return crs_mod.transform_points(pts, crs_mod.horizontal(src_crs), crs_mod.horizontal(dst_crs),
                                    legacy=Legacy("the cells of a merged raster", chunk))[:, :2]


def frame_problem(src: str | None, dst: str | None) -> bool:
    """Between ``src`` and ``dst`` GDAL would warp the pixels by PROJ's own transformation where the program goes by
    the frames and their epochs (:func:`..core.crs.transform_points`): a system in ITRF2020 (positions that move with
    time) with any other, or two different European frames (ETRF2014/ETRF2020 and ETRF2000 - RDN2008, ETRS89): PROJ's
    ballpark or null, centimetres to a metre off. Such a pair is refused, never warped silently."""
    if src == dst or crs_mod.is_local(src) or crs_mod.is_local(dst):
        return False
    from ..gnss import frames

    a, b = crs_mod.horizontal(src), crs_mod.horizontal(dst)
    return bool(frames.dynamic(a) or frames.dynamic(b) or frames.dynamic(src) or frames.dynamic(dst)
                or frames.european_hop(a, b) or frames.european_hop(src, dst))


def _frame_error(what_en: str, what_it: str, src: str | None, dst: str | None) -> MosaicError:
    return MosaicError(f"{what_en} is in {src} and the merged product in {dst}: between these two frames the pixels "
                       "would be placed by PROJ's approximate transformation (centimetres to a metre off), not by "
                       "the epoch of the survey. Put the chunk in the system of the first (or make that chunk first), "
                       "then build again",
                       f"{what_it} è in {src} e il prodotto unito in {dst}: fra questi due sistemi i pixel sarebbero "
                       "collocati dalla trasformazione approssimata di PROJ (da centimetri a un metro di errore), non "
                       "all'epoca del rilievo. Porta il chunk nel sistema del primo (o mettilo primo), poi ricostruisci")


def _product_path(chunk, product: str) -> Path | None:
    path = chunk.dem_path if product == "dem" else chunk.orthomosaic_path
    return path if path is not None and Path(path).exists() else None


def _source(chunk, index: int, product: str, system: str | None) -> _Source:
    import rasterio
    from affine import Affine
    from rasterio.enums import ColorInterp

    path = _product_path(chunk, product)
    if path is None:
        what_en, what_it = ("DEM", "il DEM") if product == "dem" else ("orthophoto", "l'ortofoto")
        raise MosaicError(f"the chunk {chunk.label} has no {what_en}: build it first, or leave the chunk out",
                          f"il chunk {chunk.label} non ha {what_it}: costruiscilo prima, o lascia fuori il chunk")
    with rasterio.open(path) as src:
        tr, w, h, count, dtype = src.transform, src.width, src.height, src.count, src.dtypes[0]
        interp = list(src.colorinterp)
        nodata = src.nodata
    if product == "orthomosaic" and dtype != "uint8":
        raise MosaicError(f"the orthophoto of {chunk.label} is not of 8 bits ({dtype}): it cannot be merged",
                          f"l'ortofoto di {chunk.label} non è a 8 bit ({dtype}): non si può unire")
    alpha = next((i + 1 for i, c in enumerate(interp) if c == ColorInterp.alpha), 0)
    built = stamps.stamp_of(chunk, product)
    move = None
    tilt = 0.0
    if built is not None and not stamps.same_transform(built, chunk.transform):
        move = np.asarray(chunk.transform, float) @ np.linalg.inv(np.asarray(built, float))
        from ..core import geometry

        rot = geometry.decompose_similarity(move)[1]
        tilt = float(np.degrees(np.arccos(np.clip(rot[2, 2], -1.0, 1.0))))
        tr = Affine(move[0, 0], move[0, 1], move[0, 3], move[1, 0], move[1, 1], move[1, 3]) @ tr
    same = chunk.crs == system or (crs_mod.is_local(chunk.crs) and crs_mod.is_local(system))
    t = np.linspace(0.0, 1.0, EDGE_POINTS)
    edge = np.concatenate([np.column_stack([t * w, np.zeros_like(t)]), np.column_stack([t * w, np.full_like(t, h)]),
                           np.column_stack([np.zeros_like(t), t * h]), np.column_stack([np.full_like(t, w), t * h])])
    cr = np.array([tr @ (c, r) for c, r in edge], float)
    pts = _to_system(cr, chunk.crs, system, same, chunk)
    centre = np.array([tr @ (w / 2, h / 2), tr @ (w / 2 + 1, h / 2), tr @ (w / 2, h / 2 + 1)], float)
    c3 = _to_system(centre, chunk.crs, system, same, chunk)
    du, dv = c3[1] - c3[0], c3[2] - c3[0]
    cell = math.sqrt(abs(du[0] * dv[1] - du[1] * dv[0]))
    bounds = (float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max()))
    return _Source(chunk=chunk, index=index, path=Path(path), move=move, src_transform=tr, same=same, bounds=bounds,
                   cell=float(cell), count=int(count), alpha=alpha, nodata=nodata, tilt=tilt)


class _Reader:
    """The source raster read through a WarpedVRT on the grid of the merged product, one window at a time."""

    def __init__(self, s: _Source, product: str, system: str | None, grid, width: int, height: int):
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.vrt import WarpedVRT

        self.s, self.product, self.system, self.grid = s, product, system, grid
        src_crs, dst_crs = _vrt_crs(s.chunk.crs, system, s.same)
        self._src = rasterio.open(s.path)
        kwargs = {"src_crs": src_crs, "src_transform": s.src_transform, "crs": dst_crs, "transform": grid,
                  "width": width, "height": height, "resampling": Resampling.bilinear, "tolerance": WARP_TOLERANCE}
        if product == "dem":
            kwargs["dtype"] = "float32"
            kwargs["nodata"] = float("nan")
            if s.nodata is None:
                kwargs["add_alpha"] = True
        elif not s.alpha and s.nodata is None:
            kwargs["add_alpha"] = True
        self.vrt = WarpedVRT(self._src, **kwargs)

    def close(self) -> None:
        self.vrt.close()
        self._src.close()

    def read(self, window):
        """``(values, valid)``: RGB uint8 ``(3, h, w)`` or heights float32 ``(h, w)``, and the cells with data."""
        mask = self.vrt.dataset_mask(window=window)
        if self.product == "dem":
            z = self.vrt.read(1, window=window).astype(np.float32)
            valid = (mask > 0) & np.isfinite(z)
            if self.s.move is not None:
                z = self._moved_heights(z, window)
            return z, valid
        n = self.s.count - (1 if self.s.alpha else 0)
        bands = [b for b in range(1, self.s.count + 1) if b != self.s.alpha][:3]
        arr = self.vrt.read(bands, window=window)
        if n < 3:  # grey (and alpha)
            arr = np.repeat(arr[:1], 3, axis=0)
        valid = mask >= ALPHA_VALID
        return arr.astype(np.uint8), valid

    def _moved_heights(self, z: np.ndarray, window) -> np.ndarray:
        """The heights moved with the chunk: ``z' = m20 x + m21 y + m22 z + m23`` with ``x, y`` where the cell was in
        the world the DEM was built in (as ``gui.chunk_views.overlay_image``)."""
        m = self.s.move
        h, w = z.shape
        cols = window.col_off + np.arange(w) + 0.5
        rows = window.row_off + np.arange(h) + 0.5
        g = self.grid
        xs = g.c + cols * g.a
        ys = g.f + rows * g.e
        inv = np.linalg.inv(m[:2, :2])  # where the cell was before the move: inv(plan) (x - t)
        if self.s.same:  # affine in the columns and the rows: no (h, w) array of positions
            dx, dy = xs - m[0, 3], ys - m[1, 3]
            x0 = inv[0, 0] * dx[None, :] + inv[0, 1] * dy[:, None]
            y0 = inv[1, 0] * dx[None, :] + inv[1, 1] * dy[:, None]
        else:
            X, Y = np.meshgrid(xs, ys)
            xy = _to_system(np.column_stack([X.ravel(), Y.ravel()]), self.system, self.s.chunk.crs, False,
                            self.s.chunk)
            del X, Y
            xy -= m[:2, 3]
            x0 = (inv[0, 0] * xy[:, 0] + inv[0, 1] * xy[:, 1]).reshape(h, w)
            y0 = (inv[1, 0] * xy[:, 0] + inv[1, 1] * xy[:, 1]).reshape(h, w)
        return (m[2, 0] * x0 + m[2, 1] * y0 + m[2, 2] * z + m[2, 3]).astype(np.float32)


# --------------------------------------------------------------------------- areas
def _polygon(value):
    import shapely
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.geometry.base import BaseGeometry

    if isinstance(value, BaseGeometry):
        geom = value
    elif isinstance(value, str):
        geom = shapely.from_wkt(value)
    else:
        xy = np.asarray(value, float)
        geom = Polygon(xy[:, :2]) if xy.ndim == 2 and len(xy) >= 3 else Polygon()
    if not isinstance(geom, (Polygon, MultiPolygon)) or geom.is_empty:
        raise MosaicError("an area of a merged product is a polygon", "un'area di un prodotto unito è un poligono")
    return geom if geom.is_valid else geom.buffer(0)


def _areas(doc, chunks: list, areas) -> list[tuple[int, object]]:
    """``[(index of the chunk in the list, polygon)]`` in the order given."""
    from .chunks import resolve_chunk

    out = []
    for item in areas or []:
        try:
            ch, poly = item
        except (TypeError, ValueError):
            raise MosaicError("an area is a pair (chunk, polygon)", "un'area è una coppia (chunk, poligono)") from None
        try:
            ch = resolve_chunk(doc, ch)
        except (KeyError, ValueError):
            raise MosaicError(f"no chunk {ch!r} for an area", f"nessun chunk {ch!r} per un'area") from None
        idx = next((i for i, c in enumerate(chunks) if c is ch), None)
        if idx is None:
            raise MosaicError(f"the area of {ch.label} is of a chunk not in the list: add the chunk, or leave the area "
                              "out",
                              f"l'area di {ch.label} è di un chunk che non è nell'elenco: aggiungi il chunk, o lascia "
                              "fuori l'area")
        out.append((idx, _polygon(poly)))
    return out


# --------------------------------------------------------------------------- build
def _new_id(doc) -> str:
    taken = {r.get("id") for r in mosaics_of(doc)}
    while True:
        mid = "m" + uuid.uuid4().hex[:8]
        if mid not in taken and not (Path(doc.files_dir) / FOLDER / mid).exists():
            return mid


def _next_name(doc, product: str) -> str:
    base = LABELS[product]
    numbers = [0]
    for r in mosaics_of(doc):
        name = str(r.get("name") or "")
        rest = name[len(base):].strip() if name.startswith(base) else ""
        if rest.isdigit():
            numbers.append(int(rest))
    return f"{base} {max(numbers) + 1}"


def folder(doc, mid: str) -> Path:
    """The folder of the merged product ``mid`` (``<project>.ppg.files/mosaics/<id>``)."""
    return Path(doc.files_dir) / FOLDER / mid


def path_of(doc, record: dict, which: str = "mosaic") -> Path | None:
    """The file ``which`` ("mosaic", "sources", "areas", "record") of ``record``; None when it has none."""
    rel = (record.get("files") or {}).get(which)
    return None if not rel else Path(doc.files_dir) / rel


def _check_chunks(doc, chunks) -> list:
    from .chunks import resolve_chunk

    if not chunks:
        raise MosaicError("no chunk to merge: give at least one", "nessun chunk da unire: indicane almeno uno")
    out = []
    for item in chunks:
        try:
            ch = resolve_chunk(doc, item)
        except (KeyError, ValueError):
            raise MosaicError(f"no chunk {item!r} in the document", f"nessun chunk {item!r} nel documento") from None
        if any(c is ch for c in out):
            raise MosaicError(f"the chunk {ch.label} is twice in the list", f"il chunk {ch.label} è due volte "
                                                                             "nell'elenco")
        out.append(ch)
    if len(out) > MAX_CHUNKS:
        raise MosaicError(f"{len(out)} chunks: at most {MAX_CHUNKS} can be merged",
                          f"{len(out)} chunk: se ne possono unire al massimo {MAX_CHUNKS}")
    first = out[0]
    for ch in out[1:]:
        if not ties._compatible(ch.crs, first.crs):
            raise ties._not_same_frame(ch.label, ch.crs, first.label, first.crs)
        if frame_problem(ch.crs, first.crs):
            raise _frame_error(f"the chunk {ch.label}", f"il chunk {ch.label}", ch.crs, first.crs)
    return out


def build(doc: "Document", chunks: list, product: str = "orthomosaic", areas: list | None = None,
          resolution: float | None = None, background: str = "faded", name: str | None = None,
          block: int = MOSAIC_BLOCK, progress: Progress | None = None) -> dict:
    """The ``product`` ("orthomosaic" or "dem") of ``chunks`` (Chunk objects, keys or labels, in their order of
    priority) merged into one raster (see the module docstring); ``areas``: ``[(chunk, polygon)]`` (a shapely polygon,
    WKT or an ``(n, 2+)`` ring, in the system of the first chunk) in their order of priority; ``resolution``: the cell
    (default the finest of the sources); ``background``: "faded" or "none" (the cells outside every area, with areas).
    The record (``id, name, product, chunks, labels, areas, resolution, crs, files, placed, when, counts, background,
    size, bounds, moved, moved_tilt_deg, sources``) is appended to ``doc.meta["mosaics"]`` and returned; the document
    is not saved. :class:`MosaicError` for what cannot be merged (also two frames that only PROJ's approximation would
    join: :func:`frame_problem`), :class:`.ties.TieError` for a local frame with a projected system. GDAL's block cache
    is held at :data:`GDAL_CACHE_MB` meanwhile."""
    import rasterio

    with rasterio.Env(GDAL_CACHEMAX=GDAL_CACHE_MB):
        return _build(doc, chunks, product, areas, resolution, background, name, block, progress)


def _build(doc, chunks, product, areas, resolution, background, name, block, progress) -> dict:
    import rasterio
    from affine import Affine
    from rasterio import features
    from rasterio.windows import Window

    from .dem import finish_geotiff, geotiff_profile

    p = as_progress(progress)
    if product == "ortho":
        product = "orthomosaic"
    if product not in PRODUCTS:
        raise MosaicError(f"product {product!r}: 'orthomosaic' or 'dem'", f"prodotto {product!r}: 'orthomosaic' o 'dem'")
    if background not in BACKGROUNDS:
        raise MosaicError(f"background {background!r}: 'faded' or 'none'", f"sfondo {background!r}: 'faded' o 'none'")
    block = int(block)
    if block < 16:
        raise MosaicError("a block is at least 16 cells a side", "un blocco è di almeno 16 celle per lato")
    chs = _check_chunks(doc, chunks)
    system = chs[0].crs
    area_list = _areas(doc, chs, areas)
    p(0.0, "Lettura delle sorgenti...")
    sources = [_source(c, i, product, system) for i, c in enumerate(chs)]
    finest = min(sources, key=lambda s: s.cell)
    if resolution is None:
        res = float(f"{finest.cell:.9g}")  # the cell of a north-up source exactly, not its rounding through the system
    else:
        try:
            res = float(resolution)
        except (TypeError, ValueError):
            res = float("nan")
        if not (math.isfinite(res) and res > 0):
            raise MosaicError(f"resolution {resolution!r}: a cell size above 0 is needed",
                              f"risoluzione {resolution!r}: serve una dimensione della cella maggiore di 0")
    left = min(s.bounds[0] for s in sources)
    bottom = min(s.bounds[1] for s in sources)
    right = max(s.bounds[2] for s in sources)
    top = max(s.bounds[3] for s in sources)
    if area_list and background == "none":  # nothing outside the areas: the grid over them only
        import shapely

        ab = shapely.union_all([g for _i, g in area_list]).bounds
        left, bottom, right, top = max(left, ab[0]), max(bottom, ab[1]), min(right, ab[2]), min(top, ab[3])
        if right <= left or top <= bottom:
            raise MosaicError("the areas lie outside every source raster",
                              "le aree sono fuori da tutti i raster di partenza")
    ax, ay = finest.bounds[0], finest.bounds[3]  # the grid on the cells of the finest source (its corner)
    left = ax + math.floor((left - ax) / res + SNAP) * res  # whole cells, a rounding of the division not counted
    top = ay + math.ceil((top - ay) / res - SNAP) * res
    width = max(1, int(math.ceil((right - left) / res - SNAP)))
    height = max(1, int(math.ceil((top - bottom) / res - SNAP)))
    grid = Affine(res, 0.0, left, 0.0, -res, top)

    mid = _new_id(doc)
    final = folder(doc, mid)
    work = final.with_name(f".{mid}.building")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    counts = np.zeros(len(chs), np.int64)
    readers: list[_Reader] = []
    try:
        readers = [_Reader(s, product, system, grid, width, height) for s in sources]
        dem = product == "dem"
        out_profile = geotiff_profile(height, width, 1 if dem else 4, "float32" if dem else "uint8", grid, system,
                                      nodata=float("nan") if dem else None)
        src_profile = geotiff_profile(height, width, 1, "uint8", grid, system, nodata=0)
        blocks = [(r0, c0) for r0 in range(0, height, block) for c0 in range(0, width, block)]
        with rasterio.open(work / FILES["mosaic"], "w", **out_profile) as dst, \
                rasterio.open(work / FILES["sources"], "w", **src_profile) as dsrc:
            for n, (r0, c0) in enumerate(blocks):
                p(0.05 + 0.85 * n / len(blocks), "Unione dei raster...")
                bh, bw = min(block, height - r0), min(block, width - c0)
                win = Window(c0, r0, bw, bh)
                btr = grid @ Affine.translation(c0, r0)
                bl, bt = btr.c, btr.f
                bbox = (bl - res, bt - (bh + 1) * res, bl + (bw + 1) * res, bt + res)  # a cell more: curved edges
                cache: dict[int, tuple] = {}

                def data(i: int):
                    if i not in cache:
                        b = sources[i].bounds
                        if b[2] < bbox[0] or b[0] > bbox[2] or b[3] < bbox[1] or b[1] > bbox[3]:
                            cache[i] = None
                        else:
                            cache[i] = readers[i].read(win)
                    return cache[i]

                src_band = np.zeros((bh, bw), np.uint8)
                inside = np.zeros((bh, bw), bool)
                for idx, geom in area_list:  # the areas first, in their order
                    m = features.geometry_mask([geom], out_shape=(bh, bw), transform=btr, invert=True)
                    if not m.any():
                        continue
                    inside |= m
                    d = data(idx)
                    if d is None:
                        continue
                    take = m & (src_band == 0) & d[1]
                    src_band[take] = idx + 1
                outside = ~inside
                listed = (src_band == 0) if (not area_list or background == "faded") else (src_band == 0) & inside
                for i in range(len(chs)):  # then the list
                    if not listed.any():
                        break
                    d = data(i)
                    if d is None:
                        continue
                    take = listed & d[1]
                    src_band[take] = i + 1
                    listed &= ~take
                if dem:
                    out = np.full((bh, bw), np.nan, np.float32)
                    for i in np.unique(src_band):
                        if i:
                            sel = src_band == i
                            out[sel] = cache[int(i) - 1][0][sel]
                    dst.write(out[None], window=win)
                else:
                    out = np.zeros((4, bh, bw), np.uint8)
                    for i in np.unique(src_band):
                        if i:
                            sel = src_band == i
                            out[:3, sel] = cache[int(i) - 1][0][:, sel]
                    out[3] = np.where(src_band > 0, 255, 0).astype(np.uint8)
                    if area_list and background == "faded":
                        fade = outside & (src_band > 0)
                        if fade.any():
                            rgb = out[:3, fade].astype(np.float64)
                            out[:3, fade] = np.round(rgb + FADE * (255.0 - rgb)).astype(np.uint8)
                    dst.write(out, window=win)
                dsrc.write(src_band[None], window=win)
                counts += np.bincount(src_band.ravel(), minlength=len(chs) + 1)[1:]
            p(0.92, "Panoramiche...")
            finish_geotiff(dst)
            finish_geotiff(dsrc)
        for r in readers:
            r.close()
        readers = []
        rel = f"{FOLDER}/{mid}"
        files = {"mosaic": f"{rel}/{FILES['mosaic']}", "sources": f"{rel}/{FILES['sources']}",
                 "areas": None, "record": f"{rel}/{FILES['record']}"}
        if area_list:
            from shapely.geometry import MultiPolygon

            from ..io import gpkg

            feats = [(g if isinstance(g, MultiPolygon) else MultiPolygon([g]),
                      {"chunk": chs[i].label, "priority": k + 1}) for k, (i, g) in enumerate(area_list)]
            gpkg.write_table(work / FILES["areas"], AREAS_LAYER, "MULTIPOLYGON", {"chunk": "TEXT", "priority": "INTEGER"},
                             feats, None if crs_mod.is_local(system) else system)
            files["areas"] = f"{rel}/{FILES['areas']}"
        record = {
            "id": mid, "name": name or _next_name(doc, product), "product": product,
            "chunks": [int(c.key) for c in chs], "labels": {str(c.key): c.label for c in chs},
            "areas": [{"chunk": int(chs[i].key), "priority": k + 1, "wkt": g.wkt} for k, (i, g) in enumerate(area_list)],
            "background": background if area_list else None, "resolution": float(res),
            "crs": system, "files": files,
            "placed": {str(c.key): np.asarray(c.transform, float).tolist() for c in chs},
            "built_in": {str(s.chunk.key): stamps.stamp_of(s.chunk, product) for s in sources},
            "moved": [int(s.chunk.key) for s in sources if s.move is not None],
            "moved_tilt_deg": {str(s.chunk.key): round(s.tilt, 3) for s in sources if s.move is not None},
            "sources": {str(s.chunk.key): _file_state(doc, s.path) for s in sources},
            "when": datetime.now().isoformat(timespec="seconds"),
            "counts": {str(c.key): int(counts[i]) for i, c in enumerate(chs)},
            "size": [int(width), int(height)], "bounds": [float(left), float(top - height * res),
                                                         float(left + width * res), float(top)],
        }
        (work / FILES["record"]).write_text(json.dumps(record, indent=1))
        p.check()
        work.rename(final)
    except BaseException:
        for r in readers:
            r.close()
        shutil.rmtree(work, ignore_errors=True)
        raise
    doc.meta.setdefault(META_KEY, []).append(record)
    p(1.0, "Prodotto unito pronto")
    return record


# --------------------------------------------------------------------------- the records
def mosaics_of(doc: "Document") -> list[dict]:
    """The records of the merged products of the document."""
    return [r for r in (doc.meta.get(META_KEY) or []) if isinstance(r, dict)]


def record_of(doc: "Document", mid: str) -> dict:
    """The record ``mid``; :class:`MosaicError` when there is none."""
    for r in mosaics_of(doc):
        if r.get("id") == mid:
            return r
    raise MosaicError(f"no merged product {mid!r} in the document", f"nessun prodotto unito {mid!r} nel documento")


def remove(doc: "Document", mid: str, delete_files: bool = True) -> dict:
    """The merged product ``mid`` taken out of the document (its folder deleted unless ``delete_files`` is False: the
    window puts it aside for Annulla); the record returned."""
    rec = record_of(doc, mid)
    rest = [r for r in mosaics_of(doc) if r.get("id") != mid]
    if rest:
        doc.meta[META_KEY] = rest
    else:
        doc.meta.pop(META_KEY, None)
    if delete_files:
        shutil.rmtree(folder(doc, mid), ignore_errors=True)
    return rec


def _label(doc, record: dict, key: int) -> str:
    for c in doc.chunks:
        if int(c.key) == int(key):
            return c.label
    return str((record.get("labels") or {}).get(str(key), key))


def _relative(doc, path) -> str:
    """``path`` relative to the files folder of the project (posix separators) when it lies inside it, else absolute:
    moving or renaming the project folder keeps the relative ones the same."""
    path = Path(path)
    try:
        return path.resolve().relative_to(Path(doc.files_dir).resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def _file_state(doc, path) -> dict:
    """What tells a source raster from one built again later: its path (relative to the files folder of the project
    when inside it, :func:`_relative`) and its time of change (ns)."""
    path = Path(path)
    try:
        mtime = int(path.stat().st_mtime_ns)
    except OSError:
        mtime = None
    return {"file": _relative(doc, path), "mtime_ns": mtime}


def _same_state(doc, was: dict, now: dict) -> bool:
    """The recorded state of a source and the present one agree; an absolute path recorded by a build before the paths
    were relative (or of a project moved since) is compared in its relative form too."""
    if was.get("mtime_ns") != now.get("mtime_ns"):
        return False
    a, b = str(was.get("file")), str(now.get("file"))
    return a == b or (Path(a).is_absolute() and _relative(doc, a) == b)


def is_stale(doc: "Document", record: dict) -> list[str]:
    """The labels of the chunks of ``record`` to merge again: placed elsewhere since it was built (their transform
    differs from the one recorded), no longer in the document, or with a source raster built again (another file,
    another time of change, another stamp; records without ``sources`` are checked on the transforms only)."""
    chunks = {int(c.key): c for c in doc.chunks}
    product = record.get("product") or "orthomosaic"
    out = []
    for key in record.get("chunks") or []:
        ch = chunks.get(int(key))
        placed = (record.get("placed") or {}).get(str(key))
        stale = ch is None or placed is None or not stamps.same_transform(placed, ch.transform)
        was = (record.get("sources") or {}).get(str(key))
        if not stale and was is not None:
            path = _product_path(ch, product)
            now = _file_state(doc, path) if path is not None else None
            built = (record.get("built_in") or {}).get(str(key))
            stamp = stamps.stamp_of(ch, product)
            stale = (now is None or not _same_state(doc, was, now) or (built is None) != (stamp is None)
                     or (built is not None and not stamps.same_transform(built, stamp)))
        if stale:
            out.append(_label(doc, record, key))
    return out


def source_at(doc: "Document", mid: str, x: float, y: float) -> str | None:
    """The label of the chunk the cell of the merged product ``mid`` at ``(x, y)`` (in its system) comes from; None
    outside it or where no chunk has data."""
    import rasterio
    from rasterio.windows import Window

    rec = record_of(doc, mid)
    path = path_of(doc, rec, "sources")
    if path is None or not path.exists():
        return None
    with rasterio.open(path) as src:
        r, c = src.index(x, y)
        if not (0 <= r < src.height and 0 <= c < src.width):
            return None
        v = int(src.read(1, window=Window(c, r, 1, 1))[0, 0])
    keys = rec.get("chunks") or []
    if v == 0 or v > len(keys):
        return None
    return _label(doc, rec, keys[v - 1])


def read_areas(doc: "Document", path, system: str | None) -> list[tuple[object, object]]:
    """The areas of a GeoPackage: ``[(chunk, polygon)]`` from every polygon of its feature tables, the chunk named by
    the attribute ``chunk`` (label or key), ordered by the attribute ``priority`` when there is one (else as in the
    file); the polygons carried into ``system`` when the table says another projected system. :class:`MosaicError`
    for a file that is not a GeoPackage, a polygon without a chunk, a file in a projected system for chunks in a local
    frame, no polygon."""
    import sqlite3

    import shapely
    from shapely.geometry import MultiPolygon, Polygon

    from ..io import gpkg
    from .chunks import resolve_chunk

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if not gpkg.is_geopackage(path):
        raise MosaicError(f"{path.name} is not a GeoPackage", f"{path.name} non è un GeoPackage")
    con = sqlite3.connect(str(path))
    try:
        tables = [(r[0], gpkg.table_srs(con, r[0])) for r in
                  con.execute("SELECT table_name FROM gpkg_contents WHERE data_type = 'features'")]
    finally:
        con.close()
    target = crs_mod.epsg_code(system)
    if not crs_mod.is_local(system) and target is None:
        raise MosaicError(f"the chunks are in {system}, which a GeoPackage cannot name: the areas of {path.name} cannot "
                          "be checked against it", f"i chunk sono in {system}, che un GeoPackage non sa indicare: le "
                          f"aree di {path.name} non si possono verificare")
    found = []
    for table, srs in tables:
        for geom, values in gpkg.read_table(path, table):
            if not isinstance(geom, (Polygon, MultiPolygon)) or geom.is_empty:
                continue
            item = values.get("chunk")
            if item is None or str(item).strip() == "":
                raise MosaicError(f"a polygon of {path.name} ({table}) has no chunk: the attribute 'chunk' names it",
                                  f"un poligono di {path.name} ({table}) non ha chunk: lo indica l'attributo 'chunk'")
            try:
                ch = resolve_chunk(doc, item if not isinstance(item, (int, np.integer)) else int(item))
            except (KeyError, ValueError):
                raise MosaicError(f"no chunk {item!r} for an area of {path.name}",
                                  f"nessun chunk {item!r} per un'area di {path.name}") from None
            if srs is None or srs <= 0:  # a table that says no system: only for chunks in a local frame
                if not crs_mod.is_local(system):
                    raise MosaicError(f"the table {table} of {path.name} says no system and the chunks are in "
                                      f"{system}: save it in their system", f"la tabella {table} di {path.name} non "
                                      f"indica un sistema e i chunk sono in {system}: salvala nel loro sistema")
            else:
                if crs_mod.is_local(system):
                    raise MosaicError(f"{path.name} is in EPSG:{srs} and the chunks are in a local frame",
                                      f"{path.name} è in EPSG:{srs} e i chunk sono in un sistema locale")
                if srs != target:
                    src = f"EPSG:{srs}"
                    if frame_problem(src, system):
                        raise _frame_error(f"the table {table} of {path.name}", f"la tabella {table} di {path.name}",
                                           src, system)
                    geom = shapely.transform(geom, lambda xy, s=src, c=ch: _to_system(xy, s, system, False, c))
            prio = values.get("priority")
            try:
                prio = float(prio) if prio is not None else math.inf
            except (TypeError, ValueError):
                prio = math.inf
            found.append((prio, len(found), ch, geom))
    if not found:
        raise MosaicError(f"no polygon in {path.name}", f"nessun poligono in {path.name}")
    found.sort(key=lambda t: (t[0], t[1]))
    return [(ch, geom) for _p, _n, ch, geom in found]


__all__ = ["META_KEY", "MOSAIC_BLOCK", "FADE", "PRODUCTS", "BACKGROUNDS", "MosaicError", "build", "mosaics_of",
           "record_of", "remove", "is_stale", "source_at", "folder", "path_of", "read_areas", "frame_problem"]
