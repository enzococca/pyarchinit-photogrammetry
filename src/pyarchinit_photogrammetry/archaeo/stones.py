"""Stone-by-stone drawing: every stone of a wall elevation or of a paving outlined by SAM 2.1 on
the orthophoto, kept by its size and shape, and written as a draughtsman draws it.

* :func:`find_stones` - SAM's automatic masks over the raster (an orthomosaic, or the orthophoto
  of an elevation in its own metres), scanned in tiles (:func:`ai.sam.auto_units`) at a pixel
  fine enough for the smallest stone; the masks become a partition (smaller ones cut out of
  larger ones) and :func:`keep_stones` keeps the stones.
* :func:`keep_stones` - polygons whose equivalent diameter (``sqrt(4 area / pi)``) is between
  ``min_size`` and ``max_size`` metres and whose solidity (area over its convex hull) is at least
  ``min_solidity``: the mortar, the soil and the wall face as a whole go, the stones stay.
* :func:`export_stones` - the outlines as closed polylines on the layer ``PIETRE`` of a DXF, an
  SVG at a given scale (millimetres on paper), or GeoJSON.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from ..core.progress import Progress, as_progress

LAYER = "PIETRE"


def size_of(polygon) -> float:
    """Equivalent diameter of a polygon: the diameter of the circle with its area."""
    return math.sqrt(4.0 * polygon.area / math.pi)


def keep_stones(polygons, min_size: float = 0.05, max_size: float = 1.0, min_solidity: float = 0.75) -> list:
    """The polygons that look like stones (see the module docstring), the largest first."""
    out = []
    for g in polygons:
        for p in getattr(g, "geoms", [g]):
            if p.is_empty or p.area <= 0:
                continue
            hull = p.convex_hull.area
            if min_size <= size_of(p) <= max_size and (hull <= 0 or p.area / hull >= min_solidity):
                out.append(p)
    return sorted(out, key=lambda p: -p.area)


def find_stones(raster: str | os.PathLike, segmenter, bounds=None, min_size: float = 0.05, max_size: float = 1.0,
                min_solidity: float = 0.75, thoroughness: str = "alta", resolution: float | None = None,
                progress: Progress | None = None) -> list:
    """The stones of ``raster`` inside ``bounds`` (its coordinates; None: all of it) as shapely
    polygons (see the module docstring). ``resolution`` (m/px): default an eighth of the smallest
    stone, never finer than the raster."""
    import rasterio

    from ..ai.sam import THOROUGHNESS, auto_units

    progress = as_progress(progress)
    with rasterio.open(raster) as src:
        native = abs(src.res[0])
    res = max(native, float(resolution or min_size / 8.0))
    diameter_px = min_size / res
    # SAM drops the objects under min_area pixels (500 by default) and prompts on a grid: both follow the
    # smallest stone, so that a stone of a few pixels is still asked for and kept
    grid = int(min(64, max(32, round(1024 / max(1.5 * diameter_px, 1)))))
    sam_options = {"min_area": max(4, int(math.pi * (diameter_px / 2) ** 2 / 2)),
                   "points_per_side": max(grid, THOROUGHNESS.get(thoroughness, {}).get("points_per_side", 32))}
    units = auto_units(None, segmenter, path=raster, bounds=bounds, resolution=res, tile=1024, overlap=128,
                       min_area_m2=math.pi * (min_size / 2) ** 2 * 0.5, thoroughness=thoroughness, exclusive=True,
                       max_tiles=400, progress=progress.sub(0, 0.95), sam_options=sam_options)
    stones = keep_stones([u.geometry for u in units], min_size, max_size, min_solidity)
    progress(1.0, f"{len(stones)} stones")
    return stones


def _rings(polygon):
    yield list(polygon.exterior.coords)
    for hole in polygon.interiors:
        yield list(hole.coords)


def export_stones(stones, path: str | os.PathLike, scale: float = 20.0, title: str | None = None) -> Path:
    """The outlines of ``stones`` in ``path``: ``.dxf`` (closed polylines on the layer
    ``PIETRE``, metres), ``.svg`` (at 1:``scale``, millimetres on paper) or ``.geojson``."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".dxf":
        import ezdxf

        doc = ezdxf.new("R2010")
        doc.units = ezdxf.units.M
        doc.layers.add(LAYER, color=7)
        msp = doc.modelspace()
        for s in stones:
            for ring in _rings(s):
                msp.add_lwpolyline([(x, y) for x, y, *_ in ring], close=True, dxfattribs={"layer": LAYER})
        doc.saveas(path)
    elif suffix == ".svg":
        x0, y0, x1, y1 = _bounds(stones)
        k = 1000.0 / scale  # millimetres on paper for a metre
        w, h = (x1 - x0) * k + 20, (y1 - y0) * k + 20
        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.1f}mm" height="{h:.1f}mm" '
                 f'viewBox="0 0 {w:.2f} {h:.2f}">', f"<title>{title or LAYER} 1:{scale:g}</title>",
                 '<g fill="none" stroke="black" stroke-width="0.25" stroke-linejoin="round">']
        for s in stones:
            d = " ".join("M " + " L ".join(f"{(x - x0) * k + 10:.2f} {(y1 - y) * k + 10:.2f}" for x, y, *_ in ring)
                         + " Z" for ring in _rings(s))
            parts.append(f'<path d="{d}"/>')
        parts += ["</g>", "</svg>"]
        path.write_text("\n".join(parts), encoding="utf-8")
    elif suffix in (".geojson", ".json"):
        from shapely.geometry import mapping

        features = [{"type": "Feature", "properties": {"n": i + 1, "area_m2": round(s.area, 5),
                                                         "size_m": round(size_of(s), 4)}, "geometry": mapping(s)}
                    for i, s in enumerate(stones)]
        path.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    else:
        raise ValueError(f"{path.name}: DXF, SVG or GeoJSON")
    return path


def _bounds(stones):
    xs0, ys0, xs1, ys1 = zip(*(s.bounds for s in stones)) if stones else ((0,), (0,), (1,), (1,))
    return min(xs0), min(ys0), max(xs1), max(ys1)
