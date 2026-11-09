"""Masks from a colour range of the orthophoto: the water of a flooded trench, a tarpaulin, the sand around a wreck -
every cell of the orthophoto whose hue, saturation and value fall in a range, cleaned (an opening drops the specks,
the holes filled), kept as polygons, and written as a layer of free drawings (:mod:`..core.free_drawings`) with the
heights of the DEM: one storage for drawn and computed areas, so that the crop of the products
(``MainWindow.crop_products``), the stable height (:mod:`.stable_height`, its ``area``/``exclude``) and every export
of the free drawings read them as they read a polygon drawn by hand (since 1.7.6).

The range: hue in degrees 0-360 (a range whose low end is above its high end wraps through 0: the reds, e.g.
350-10), saturation and value 0-100 %. The orthophoto is read at most :data:`MASK_SIDE` pixels a side (an overview of
the file), so that a mask of an orthophoto of 45000 pixels costs as much as one of 4000. :func:`sample` gives the range
around the colour of a small disc of the orthophoto. No Qt here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from ..core.progress import Progress, as_progress

if TYPE_CHECKING:  # pragma: no cover
    from ..core.chunk import Chunk

log = logging.getLogger("pyarchinit_photogrammetry")

MASK_SIDE = 4000  # pixels a side, at most, of the copy of the orthophoto a mask is computed on (R6)
SAMPLE_SIDE = 512  # pixels a side, at most, of the window read around a point by :func:`sample`
DEFAULT_TOLERANCE = (15.0, 20.0, 20.0)  # degrees of hue, % of saturation and value around a sampled colour
LAYER_NAME = "Maschera colore"  # the default layers: "Maschera colore 1", "Maschera colore 2", ...
LAYER_COLOUR = "#00a5e0"
SOURCE_KEY, SOURCE_VALUE = "maschera", "colore"  # the attribute that marks the polygons of a colour mask
KEEP = ("largest", "all")
_CHANNELS = {"hue": "tinta", "saturation": "saturazione", "value": "valore"}


class MaskError(ValueError):
    """A mask that cannot be made: ``str()`` is the English message, ``italian`` the Italian one (the window shows the
    Italian one in Italian, the command line the English one), as :class:`.ties.TieError`."""

    def __init__(self, english: str, italian: str):
        super().__init__(english)
        self.english = english
        self.italian = italian


@dataclass(frozen=True)
class HsvRange:
    """A range of colours: ``hue`` (low, high) degrees 0-360, wrapping through 0 when low > high; ``sat`` and ``val``
    (low, high) in %."""

    hue: tuple[float, float]
    sat: tuple[float, float] = (0.0, 100.0)
    val: tuple[float, float] = (0.0, 100.0)

    def __post_init__(self):
        for what, (lo, hi), top in (("hue", self.hue, 360.0), ("saturation", self.sat, 100.0),
                                    ("value", self.val, 100.0)):
            if not (np.isfinite(lo) and np.isfinite(hi) and 0.0 <= lo <= top and 0.0 <= hi <= top):
                raise MaskError(f"{what} range {lo:g}-{hi:g}: from 0 to {top:g}",
                                f"intervallo di {_CHANNELS[what]} {lo:g}-{hi:g}: da 0 a {top:g}")
        for what, (lo, hi) in (("saturation", self.sat), ("value", self.val)):
            if lo > hi:
                raise MaskError(f"{what} range {lo:g}-{hi:g}: the low end above the high end",
                                f"intervallo di {_CHANNELS[what]} {lo:g}-{hi:g}: il minimo è sopra il massimo")

    @property
    def wraps(self) -> bool:
        return self.hue[0] > self.hue[1]

    def to_dict(self) -> dict:
        return {"hue": [float(v) for v in self.hue], "sat": [float(v) for v in self.sat],
                "val": [float(v) for v in self.val]}

    @classmethod
    def from_dict(cls, d: dict) -> "HsvRange":
        return cls(tuple(d["hue"]), tuple(d.get("sat", (0, 100))), tuple(d.get("val", (0, 100))))

    def text(self) -> str:
        return (f"hue {self.hue[0]:g}-{self.hue[1]:g} degrees, saturation {self.sat[0]:g}-{self.sat[1]:g} %, "
                f"value {self.val[0]:g}-{self.val[1]:g} %")


@dataclass
class Ortho:
    """A copy of (a window of) the orthophoto: ``rgb`` (rows, cols, 3) uint8, ``valid`` (rows, cols) bool (the alpha
    or nodata of the file), ``transform`` (rasterio Affine of the copy), ``cell`` metres (its side), ``crs``."""

    rgb: np.ndarray
    valid: np.ndarray
    transform: object
    cell: float
    crs: object = None


@dataclass
class MaskResult:
    """The mask: ``polygons`` (shapely, world coordinates of the chunk, after the margin), ``area`` m2 (of them),
    ``share`` (the cells of the mask over the valid cells of the orthophoto read, 0-1: before the margin), ``cells`` (cells of the copy in the mask, before the
    margin), ``cell`` metres (the side of a cell of the copy), ``holes`` (rings inside the polygons: kept here, left out
    of a layer of free drawings, which has one ring per polygon), ``range``."""

    polygons: list
    area: float
    share: float
    cells: int
    cell: float
    holes: int = 0
    range: HsvRange | None = None
    options: dict = field(default_factory=dict)


def _orthophoto(chunk: "Chunk") -> Path:
    from ..core import stamps

    path = chunk.orthomosaic_path
    if path is None or not Path(path).exists():
        raise MaskError(f"the chunk {chunk.label} has no orthophoto: build it first",
                        f"il chunk {chunk.label} non ha un'ortofoto: costruiscila prima")
    if stamps.is_stale(chunk, "orthomosaic"):
        raise MaskError(f"the orthophoto of {chunk.label} was built in another system of the chunk: rebuild it first",
                        f"l'ortofoto di {chunk.label} è stata costruita in un altro sistema del chunk: rifalla prima")
    return Path(path)


def read_orthophoto(chunk: "Chunk", side: int | None = None, bounds=None) -> Ortho:
    """The orthophoto of ``chunk`` (within ``bounds`` (xmin, ymin, xmax, ymax) when given) read at most ``side``
    pixels a side (None: :data:`MASK_SIDE`) - GDAL takes an overview when there is one. :class:`MaskError` without an orthophoto, with one to rebuild,
    or with ``bounds`` outside it."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import Affine
    from rasterio.windows import Window, from_bounds

    path = _orthophoto(chunk)
    with rasterio.open(path) as src:
        if src.count < 3:
            raise MaskError(f"the orthophoto of {chunk.label} has {src.count} band(s): a colour mask needs red, "
                            "green and blue", f"l'ortofoto di {chunk.label} ha {src.count} bande: una maschera dal "
                            "colore vuole rosso, verde e blu")
        full = Window(0, 0, src.width, src.height)
        if bounds is not None:
            x0, y0, x1, y1 = (float(v) for v in bounds)
            win = from_bounds(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), src.transform)
            try:
                win = win.round_offsets().round_lengths().intersection(full)
            except Exception:  # rasterio WindowError: no overlap
                raise _outside() from None
            if win.width < 1 or win.height < 1:
                raise _outside()
        else:
            win = full
        side = MASK_SIDE if side is None else side
        f = max(1.0, max(win.width, win.height) / float(max(1, side)))
        out = (max(1, int(round(win.height / f))), max(1, int(round(win.width / f))))
        rgb = src.read((1, 2, 3), window=win, out_shape=(3, *out), resampling=Resampling.average)
        valid = src.read_masks(1, window=win, out_shape=out, resampling=Resampling.nearest) > 0
        if src.count >= 4 and src.colorinterp[3].name == "alpha":
            valid &= src.read(4, window=win, out_shape=out, resampling=Resampling.nearest) > 0
        tr = src.window_transform(win) @ Affine.scale(win.width / out[1], win.height / out[0])
        crs = src.crs
    if rgb.dtype != np.uint8:  # a 16 bit orthophoto: stretched on its own range
        top = float(np.nanmax(rgb)) if rgb.size else 1.0
        rgb = np.clip(rgb.astype(np.float64) * (255.0 / max(top, 1.0)), 0, 255).astype(np.uint8)
    rgb = np.ascontiguousarray(np.moveaxis(rgb, 0, -1))
    cell = float(np.sqrt(abs(tr.a * tr.e - tr.b * tr.d)))
    return Ortho(rgb, valid, tr, cell, crs)


def _outside() -> MaskError:
    return MaskError("the area asked is outside the orthophoto", "l'area chiesta è fuori dall'ortofoto")


def _hsv_bounds(lo: float, hi: float, scale: float, top: int) -> tuple[int, int]:
    return int(np.clip(np.floor(lo * scale), 0, top)), int(np.clip(np.ceil(hi * scale), 0, top))


def in_range(rgb: np.ndarray, rng: HsvRange) -> np.ndarray:
    """The cells of ``rgb`` (rows, cols, 3 uint8, red green blue) whose colour is in ``rng`` (bool)."""
    import cv2

    hsv = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2HSV)  # hue 0-179 (degrees / 2), 0-255 the others
    s0, s1 = _hsv_bounds(rng.sat[0], rng.sat[1], 2.55, 255)
    v0, v1 = _hsv_bounds(rng.val[0], rng.val[1], 2.55, 255)
    lo, hi = rng.hue
    h1 = 179 if hi >= 359.0 else min(179, int(np.ceil(hi / 2.0)))
    if lo >= 360.0:  # 360 is 0: "360 -> 20" are the hues from 0 to 20, not every hue
        out = cv2.inRange(hsv, (0, s0, v0), (h1, s1, v1))
    elif not rng.wraps:
        out = cv2.inRange(hsv, (int(np.floor(lo / 2.0)), s0, v0), (h1, s1, v1))
    else:  # through 0: the reds
        h0 = min(179, int(np.floor(lo / 2.0)))
        out = cv2.inRange(hsv, (h0, s0, v0), (179, s1, v1)) | cv2.inRange(hsv, (0, s0, v0), (h1, s1, v1))
    return out > 0


def mask_cells(image: Ortho, rng: HsvRange, opening: float = 0.05, fill_holes: bool = True, keep: str = "largest",
               min_area: float = 0.05) -> np.ndarray:
    """The cells of ``image`` in the mask (bool, its grid): in ``rng`` and valid, opened by a disc of ``opening``
    metres (the specks smaller than it dropped), the holes filled, then the largest region (``keep`` "largest") or
    every region of ``min_area`` m2 or more ("all"; the largest too must reach it)."""
    import cv2

    if keep not in KEEP:
        raise MaskError(f"keep: one of {', '.join(KEEP)}", f"regioni: una fra {', '.join(KEEP)}")
    m = in_range(image.rgb, rng) & image.valid
    k = int(round(float(opening) / image.cell)) if opening and opening > 0 else 0
    if k >= 2:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        m = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_OPEN, kernel) > 0
    if fill_holes:
        from scipy.ndimage import binary_fill_holes

        m = binary_fill_holes(m)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), connectivity=4)
    if n <= 1:
        return np.zeros_like(m, bool)
    sizes = stats[1:, cv2.CC_STAT_AREA]
    least = max(1.0, float(min_area or 0.0) / image.cell ** 2)
    if keep == "largest":
        best = int(np.argmax(sizes))
        chosen = [best + 1] if sizes[best] >= least else []
    else:
        chosen = [i + 1 for i in np.flatnonzero(sizes >= least)]
    if not chosen:
        return np.zeros_like(m, bool)
    return np.isin(labels, chosen)


def _split(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon" and not g.is_empty]


def polygons_of(cells: np.ndarray, image: Ortho, margin: float = 0.0) -> list:
    """The polygons (world) of the mask ``cells`` of ``image``: the outlines of the cells, simplified within half a
    cell, grown by ``margin`` metres (shrunk when negative; those that touch then merge)."""
    import shapely
    from rasterio.features import shapes
    from shapely.geometry import shape

    if not cells.any():
        return []
    polys = [shape(g) for g, v in shapes(cells.astype(np.uint8), mask=cells, transform=image.transform,
                                         connectivity=4) if v]
    polys = [p.simplify(0.5 * image.cell, preserve_topology=True) for p in polys]
    polys = [p if p.is_valid else p.buffer(0) for p in polys]
    if margin:
        merged = shapely.union_all([p.buffer(float(margin)) for p in polys])
        polys = _split(merged)
    else:
        polys = [q for p in polys for q in _split(p)]
    return sorted(polys, key=lambda p: -p.area)


def mask(chunk: "Chunk", rng: HsvRange, opening: float = 0.05, fill_holes: bool = True, keep: str = "largest",
         min_area: float = 0.05, margin: float = 0.0, bounds=None, progress: Progress | None = None,
         side: int | None = None, image: Ortho | None = None) -> MaskResult:
    """The mask of the colours ``rng`` on the orthophoto of ``chunk`` (see the module and :func:`mask_cells`):
    ``bounds`` (xmin, ymin, xmax, ymax) to look only there, ``margin`` metres around the polygons, ``side`` the pixels a
    side of the copy read (``image``: one already read). ValueError without an orthophoto or with one to rebuild."""
    p = as_progress(progress)
    p(0.0, "Lettura dell'ortofoto...")
    if image is None:
        image = read_orthophoto(chunk, side=side, bounds=bounds)
    p.check()
    p(0.4, "Maschera dei colori...")
    cells = mask_cells(image, rng, opening=opening, fill_holes=fill_holes, keep=keep, min_area=min_area)
    p.check()
    p(0.7, "Poligoni della maschera...")
    polys = polygons_of(cells, image, margin=margin)
    area = float(sum(pg.area for pg in polys))
    valid_area = float(image.valid.sum()) * image.cell ** 2
    holes = sum(len(pg.interiors) for pg in polys)
    p(1.0, "")
    share = min(1.0, float(cells.sum()) * image.cell ** 2 / valid_area) if valid_area > 0 else 0.0
    return MaskResult(polys, area, share, int(cells.sum()), image.cell, holes,
                      rng, {"opening": opening, "fill_holes": fill_holes, "keep": keep, "min_area": min_area,
                            "margin": margin})


def _circular_median(deg: np.ndarray) -> float:
    """The median of angles in degrees, around their circular mean (the hue of the reds, on both sides of 0)."""
    a = np.radians(deg)
    m = float(np.degrees(np.arctan2(np.sin(a).mean(), np.cos(a).mean())))
    shifted = (deg - m + 180.0) % 360.0
    return float((np.median(shifted) + m - 180.0) % 360.0)


def sample(chunk: "Chunk", x: float, y: float, radius: float = 0.1,
           tolerance: tuple[float, float, float] = DEFAULT_TOLERANCE) -> HsvRange:
    """The range of colours around the median colour of a disc of ``radius`` metres of the orthophoto at world ``x,
    y``: hue +- ``tolerance[0]`` degrees (wrapping through 0), saturation and value +- ``tolerance[1:]`` %. ValueError
    when the point is outside the orthophoto or where it has no value."""
    import cv2

    r = max(float(radius), 0.0)
    try:
        image = read_orthophoto(chunk, side=SAMPLE_SIDE, bounds=(x - r, y - r, x + r, y + r))
    except ValueError as exc:
        if "outside" in str(exc):
            raise MaskError(f"the point {x:.3f}, {y:.3f} is outside the orthophoto",
                            f"il punto {x:.3f}, {y:.3f} è fuori dall'ortofoto") from None
        raise
    rows, cols = np.mgrid[0:image.rgb.shape[0], 0:image.rgb.shape[1]]
    tr = image.transform
    cx = tr.c + (cols + 0.5) * tr.a + (rows + 0.5) * tr.b
    cy = tr.f + (cols + 0.5) * tr.d + (rows + 0.5) * tr.e
    disc = (np.hypot(cx - x, cy - y) <= max(r, 0.5 * image.cell)) & image.valid
    if not disc.any():  # a radius below a cell: the cell under the point
        from rasterio.transform import rowcol

        rr, c = (int(v) for v in rowcol(image.transform, x, y))
        if 0 <= rr < image.valid.shape[0] and 0 <= c < image.valid.shape[1] and image.valid[rr, c]:
            disc[rr, c] = True
    if not disc.any():
        raise MaskError(f"the orthophoto has no value at {x:.3f}, {y:.3f}",
                        f"l'ortofoto non ha valori in {x:.3f}, {y:.3f}")
    px = image.rgb[disc].reshape(-1, 1, 3).astype(np.float32) / 255.0
    hsv = cv2.cvtColor(px, cv2.COLOR_RGB2HSV).reshape(-1, 3)  # hue 0-360, saturation and value 0-1
    h = _circular_median(hsv[:, 0])
    s, v = float(np.median(hsv[:, 1])) * 100.0, float(np.median(hsv[:, 2])) * 100.0
    th, ts, tv = (float(t) for t in tolerance)
    hue = (0.0, 360.0) if th >= 180.0 else (round((h - th) % 360.0, 1) % 360.0, round((h + th) % 360.0, 1))
    return HsvRange(hue, (round(max(0.0, s - ts), 1), round(min(100.0, s + ts), 1)),
                    (round(max(0.0, v - tv), 1), round(min(100.0, v + tv), 1)))


def next_layer_name(chunk: "Chunk") -> str:
    """"Maschera colore N" with the first N not taken by a layer of the free drawings of ``chunk``."""
    from ..core import free_drawings

    taken = {x["name"] for x in free_drawings.layers(chunk)}
    n = 1
    while f"{LAYER_NAME} {n}" in taken:
        n += 1
    return f"{LAYER_NAME} {n}"


def _heights(chunk: "Chunk", xy: np.ndarray) -> list[float]:
    from ..core import stamps
    from ..core.free_drawings import fill_missing, raster_values

    if chunk.dem_path is None or not chunk.dem_path.exists():
        raise MaskError(f"the chunk {chunk.label} has no DEM for the heights of the mask: build it first",
                        f"il chunk {chunk.label} non ha un DEM per le quote della maschera: costruiscilo prima")
    if stamps.is_stale(chunk, "dem"):
        raise MaskError(f"the DEM of {chunk.label} was built in another system of the chunk: rebuild it first",
                        f"il DEM di {chunk.label} è stato costruito in un altro sistema del chunk: rifallo prima")
    zs = fill_missing(raster_values(chunk.dem_path, xy), xy)
    if zs is None:
        raise MaskError(f"the DEM of {chunk.label} has no height under the mask",
                        f"il DEM di {chunk.label} non ha quote sotto la maschera")
    return zs


def _empty() -> MaskError:
    return MaskError("the mask is empty: no polygon to write", "la maschera è vuota: nessun poligono da scrivere")


def to_layer(chunk: "Chunk", result: MaskResult, layer: str | None = None, replace: bool = True) -> list[str]:
    """The polygons of ``result`` as free drawings of ``chunk`` in the layer ``layer`` (None: "Maschera colore N", the
    first free), the heights of the DEM at their vertices (filled along the outline where it has none); each with
    the attributes ``maschera`` "colore" and the range. ``replace``: the polygons of an earlier colour mask in that
    layer are taken away first (those drawn by hand stay). The holes are left out (a free drawing has one ring, said
    in the log). Returns the ids of the drawings added. ValueError when there is nothing to write, without a DEM, or
    with one to rebuild (nothing written)."""
    from ..core import free_drawings

    if not result.polygons:
        raise _empty()
    name = (layer or "").strip() or next_layer_name(chunk)
    rings = []
    for pg in result.polygons:
        xy = np.asarray(pg.exterior.coords, float)[:-1, :2]
        if len(xy) >= 3:
            rings.append(xy)
    if not rings:
        raise _empty()
    zs = [_heights(chunk, xy) for xy in rings]  # every height read before anything is written
    attrs = {SOURCE_KEY: SOURCE_VALUE}
    if result.range is not None:
        rng = result.range
        attrs.update({"tinta": f"{rng.hue[0]:g}-{rng.hue[1]:g}", "saturazione": f"{rng.sat[0]:g}-{rng.sat[1]:g}",
                      "valore": f"{rng.val[0]:g}-{rng.val[1]:g}"})
    if name not in {x["name"] for x in free_drawings.layers(chunk)}:
        free_drawings.set_layer(chunk, name, colour=LAYER_COLOUR)
    elif replace:
        old = [f["id"] for f in free_drawings.features(chunk, name)
               if (f.get("attributes") or {}).get(SOURCE_KEY) == SOURCE_VALUE]
        if old:
            free_drawings.remove(chunk, old)
            log.info("Layer %s: %d poligoni della maschera precedente sostituiti", name, len(old))
    ids = []
    for i, (xy, z) in enumerate(zip(rings, zs), 1):
        xyz = np.column_stack([xy, np.asarray(z, float)])
        rec = free_drawings.add(chunk, "polygon", xyz.tolist(), layer=name,
                                name=f"{name} - {i}" if len(rings) > 1 else name, attributes=dict(attrs))
        ids.append(rec["id"])
    if result.holes:
        log.warning("Layer %s: %d buchi della maschera lasciati fuori (un disegno libero ha un solo contorno)", name,
                    result.holes)
    return ids


def union(chunk: "Chunk", layer: str):
    """The polygons of the free-drawing layer ``layer`` of ``chunk`` (in its current world) as one shapely geometry
    (Polygon or MultiPolygon): the area of a mask for the crop and the stable height. ValueError when the layer has no
    polygon."""
    import shapely
    from shapely.geometry import Polygon

    from ..core.free_drawings import polygons_in_world

    polys = [Polygon(r[:, :2]) for r in polygons_in_world(chunk, layer) if len(r) >= 3]
    polys = [p if p.is_valid else p.buffer(0) for p in polys]
    polys = [p for p in polys if not p.is_empty]
    if not polys:
        raise MaskError(f"no polygon in the layer {layer} of {chunk.label}",
                        f"nessun poligono nel layer {layer} di {chunk.label}")
    return shapely.union_all(polys)


__all__ = ["MASK_SIDE", "MaskError", "HsvRange", "Ortho", "MaskResult", "read_orthophoto", "in_range", "mask_cells", "polygons_of",
           "mask", "sample", "next_layer_name", "to_layer", "union"]
