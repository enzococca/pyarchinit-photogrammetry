"""From raster masks (SAM, classification) to stratigraphic-unit polygons."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


# pyunitastratigrafiche.stratigraph_index_us, as PyArchInit's layer style reads it
STRATIGRAPHIC_INDEX = {1: "Caratterizzazione", 2: "Contorno US"}
# the other columns of pyunitastratigrafiche kept in ``UnitPolygon.attributes`` under the same names
# the kinds of unit of PyArchInit's US form (unita_tipo)
US_TYPES = ["US", "USM", "USR", "USD", "USV", "USVs", "USVn", "SF", "VSF", "CON", "DOC", "TSU", "SE"]
UNIT_FIELDS = ("stratigraph_index_us", "tipo_us_s", "rilievo_originale", "disegnatore", "data", "tipo_doc", "nome_doc")
# the stratigraphic relations of PyArchInit's US forms (Italian, English, German, as its own table of
# reciprocal relations) -> (the relation said from the start of the edge, the edge of the Extended
# Matrix / s3dgraphy, declared from the other end: True; None for the relations without a direction)
RELATION_EDGES = {
    "copre": ("Copre", "overlies", False), "coperto da": ("Copre", "overlies", True),
    "taglia": ("Taglia", "cuts", False), "tagliato da": ("Taglia", "cuts", True),
    "riempie": ("Riempie", "fills", False), "riempito da": ("Riempie", "fills", True),
    "si appoggia a": ("Si appoggia a", "abuts", False), "gli si appoggia": ("Si appoggia a", "abuts", True),
    "posteriore a": ("Posteriore a", "is_after", False), "anteriore a": ("Posteriore a", "is_after", True),
    "uguale a": ("Uguale a", "equals", None), "si lega a": ("Si lega a", "bonded_to", None),
    "covers": ("Covers", "overlies", False), "covered by": ("Covers", "overlies", True),
    "cuts": ("Cuts", "cuts", False), "cut by": ("Cuts", "cuts", True),
    "fills": ("Fills", "fills", False), "filled by": ("Fills", "fills", True),
    "abuts": ("Abuts", "abuts", False), "supports": ("Abuts", "abuts", True),
    "same as": ("Same as", "equals", None), "connected to": ("Connected to", "bonded_to", None),
    "liegt über": ("Liegt über", "overlies", False), "liegt unter": ("Liegt über", "overlies", True),
    "schneidet": ("Schneidet", "cuts", False), "wird geschnitten": ("Schneidet", "cuts", True),
    "verfüllt": ("Verfüllt", "fills", False), "wird verfüllt durch": ("Verfüllt", "fills", True),
    "stützt sich auf": ("Stützt sich auf", "abuts", False), "wird gestüzt von": ("Stützt sich auf", "abuts", True),
    "bindet an": ("Bindet an", "bonded_to", None), "entspricht": ("Entspricht", "equals", None),
}
# columns of pyarchinit_quote (spot heights)
QUOTE_FIELDS = ("sito_q", "area_q", "us_q", "unita_tipo_q", "quota_q", "unita_misu_q", "data", "disegnatore",
                "rilievo_originale")


@dataclass
class UnitPolygon:
    """A stratigraphic unit drawn on the orthomosaic (world coordinates). ``sito``, ``area``,
    ``us`` and ``tipo`` are ``scavo_s``, ``area_s``, ``us_s`` and ``unita_tipo_s`` of
    PyArchInit's ``pyunitastratigrafiche``; its other columns (:data:`UNIT_FIELDS`) live in
    ``attributes``."""

    geometry: object  # shapely (Multi)Polygon
    us: str | None = None
    area: str | None = None
    tipo: str = "US"  # US / USM / USR ...
    attributes: dict = field(default_factory=dict)
    sito: str | None = None

    @property
    def wkt(self) -> str:
        return self.geometry.wkt

    @property
    def stratigraphic_index(self) -> int:
        try:
            return int((self.attributes or {}).get("stratigraph_index_us") or 1)
        except (TypeError, ValueError):
            return 1

    def layer_attributes(self) -> dict:
        """The unit as a feature of the drawing layers: the columns of pyunitastratigrafiche."""
        a = self.attributes or {}
        out = {"scavo_s": self.sito or "", "area_s": self.area or "", "us_s": self.us or "",
               "unita_tipo_s": self.tipo or "US", "stratigraph_index_us": self.stratigraphic_index}
        for k in UNIT_FIELDS[1:]:
            out[k] = a.get(k, "") if a.get(k) is not None else ""
        return out

    def to_dict(self) -> dict:
        """JSON-able record (saved with the project)."""
        return {"wkt": self.geometry.wkt, "sito": self.sito, "area": self.area, "us": self.us, "tipo": self.tipo,
                "attributes": {k: v for k, v in (self.attributes or {}).items() if not str(k).startswith("_")}}

    @classmethod
    def from_dict(cls, d: dict) -> "UnitPolygon":
        from shapely import wkt

        return cls(wkt.loads(d["wkt"]), us=d.get("us"), area=d.get("area"), tipo=d.get("tipo") or "US",
                   attributes=dict(d.get("attributes") or {}), sito=d.get("sito"))


def _polygons(geom) -> list:
    """The polygons with an area in ``geom`` (a polygon, a multipolygon, a collection)."""
    from shapely.geometry import Polygon

    if isinstance(geom, Polygon):
        return [geom] if not geom.is_empty and geom.area > 0 else []
    return [p for part in getattr(geom, "geoms", []) for p in _polygons(part)]


def units_from_polygons(polygons, tipo: str = "US", attributes=None, sito: str | None = None) -> list[UnitPolygon]:
    """Units from ``polygons`` - shapely (Multi)Polygons, or vertex lists ``(N, 2)`` in the coordinates
    of the chunk - each with its ``attributes`` (one dict for all, or a list with one per polygon):
    ``us``, ``area``, ``tipo``, ``sito`` and the columns of pyunitastratigrafiche (:data:`UNIT_FIELDS`);
    any other key stays an attribute of the unit. An invalid outline is made valid; one that is empty or
    encloses no area is left out."""
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.geometry.base import BaseGeometry
    from shapely.validation import make_valid

    polygons = list(polygons)
    if attributes is None or isinstance(attributes, dict):
        rows = [dict(attributes or {}) for _ in polygons]
    else:
        rows = [dict(a or {}) for a in attributes]
        if len(rows) != len(polygons):
            raise ValueError(f"{len(rows)} dicts of attributes for {len(polygons)} polygons")
    units = []
    for p, a in zip(polygons, rows):
        try:
            geom = p if isinstance(p, BaseGeometry) else Polygon(np.asarray(p, dtype=float)[:, :2])
        except (ValueError, IndexError, TypeError):  # fewer than three vertices, or no vertices at all
            continue
        parts = _polygons(geom if geom.is_valid else make_valid(geom))
        if not parts:
            continue
        geom = parts[0] if len(parts) == 1 else MultiPolygon(parts)
        us, area = a.pop("us", None), a.pop("area", None)
        kind, site = a.pop("tipo", None) or tipo, a.pop("sito", None) or sito
        units.append(UnitPolygon(geom, us=str(us) if us not in (None, "") else None,
                                 area=str(area) if area not in (None, "") else None, tipo=str(kind or "US"),
                                 attributes=a, sito=str(site) if site else None))
    return units


def reshape_polygon(polygon, line):
    """QGIS's *Reshape Features* on one polygon: ``line`` (a shapely LineString or its vertices)
    crosses the outline at least twice; the outline between the first and the last crossing is
    replaced by the line between them, on the side that keeps the larger polygon - a line that goes
    out and comes back adds what it encloses, one that cuts through takes away the smaller part.
    The holes inside the new outline stay. None when the line does not cross the outline twice."""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import substring
    from shapely.validation import make_valid

    line = line if isinstance(line, LineString) else LineString(line)
    ring = LineString(polygon.exterior.coords)
    crossing = ring.intersection(line)
    points = []
    for g in getattr(crossing, "geoms", [crossing]):
        if g.is_empty:
            continue
        if g.geom_type == "Point":
            points.append(g)
        elif g.geom_type == "LineString":  # the line runs along the outline for a while: its ends
            coords = list(g.coords)
            points += [Point(coords[0]), Point(coords[-1])]
    if len(points) < 2:
        return None
    points.sort(key=line.project)
    t0, t1 = line.project(points[0]), line.project(points[-1])
    a, b = ring.project(points[0]), ring.project(points[-1])
    if t1 - t0 <= 0 or a == b:
        return None
    path = list(substring(line, t0, t1).coords)
    total = ring.length

    def arc(start, end):  # along the outline from start to end, round the closing vertex when needed
        if start <= end:
            part = substring(ring, start, end)
            return list(part.coords) if part.geom_type == "LineString" else []
        return list(substring(ring, start, total).coords) + list(substring(ring, 0.0, end).coords)[1:]

    best = None
    for outline in (path + arc(b, a)[1:], arc(a, b) + path[::-1][1:]):
        if len(outline) < 4:
            continue
        shape = Polygon(outline)
        if not shape.is_valid:
            shape = make_valid(shape)
        for p in _polygons(shape):
            if best is None or p.area > best.area:
                best = p
    if best is None:
        return None
    for hole in polygon.interiors:
        h = Polygon(hole)
        if best.contains(h):
            best = best.difference(h)
    return best


def stratigraphic_edges(declarations) -> list[tuple[str, str, str, str | None]]:
    """The relations declared in the US forms as edges, each fact once. ``declarations``:
    ``(us, [[relation, other us, ...], ...])`` (the ``rapporti`` of each form); returns ``(from,
    relation, to, edge)``: the relation said from the start of the edge (``Coperto da 1`` in the
    form of US 2 is ``1 Copre 2``, the same edge as ``Copre 2`` in the form of US 1) and its edge of
    the Extended Matrix (:data:`RELATION_EDGES`; None for a relation it does not know, kept as it
    was declared)."""
    out, seen = [], set()
    for us, rels in declarations:
        us = str(us).strip()
        for rel in rels or []:
            name, other = str(rel[0]).strip(), str(rel[1]).strip() if len(rel) > 1 else ""
            if not name or not other or other == us:
                continue
            forward, edge, inverse = RELATION_EDGES.get(name.lower(), (name, None, False))
            a, b = (other, us) if inverse else (us, other)
            key = (edge or forward, *sorted((a, b))) if inverse is None else (edge or forward, a, b)
            if key not in seen:
                seen.add(key)
                out.append((a, forward, b, edge))
    return out


def mask_to_polygons(mask: np.ndarray, transform, simplify: float = 0.0, min_area: float = 0.0):
    """Binary mask (rows x cols) + raster transform -> shapely MultiPolygon in world coordinates."""
    from rasterio.features import shapes
    from shapely.geometry import MultiPolygon, shape
    from shapely.ops import unary_union

    polys = [shape(geom) for geom, val in shapes(mask.astype(np.uint8), mask=mask.astype(bool), transform=transform)
             if val == 1]
    if not polys:
        return MultiPolygon()
    geom = unary_union(polys)
    if simplify > 0:
        geom = geom.simplify(simplify, preserve_topology=True)
    parts = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    parts = [p for p in parts if p.area >= min_area]
    return MultiPolygon(parts)


def orthomosaic_window(path: str | Path):
    """Read an orthomosaic as RGB uint8 array plus its transform and CRS."""
    import rasterio

    with rasterio.open(path) as src:
        arr = src.read([1, 2, 3])
        alpha = src.read(4) if src.count >= 4 else np.full(arr.shape[1:], 255, np.uint8)
        return np.moveaxis(arr, 0, -1), alpha, src.transform, src.crs


def export_units(units: list[UnitPolygon], path: str | Path, crs: str | None = None) -> Path:
    """GeoJSON or DXF of the unit polygons."""
    import json

    from shapely.geometry import mapping

    path = Path(path)
    if path.suffix.lower() == ".dxf":
        import ezdxf

        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        for u in units:
            layer = f"US_{u.us}" if u.us else "US"
            if layer not in doc.layers:
                doc.layers.add(layer)
            parts = [u.geometry] if u.geometry.geom_type == "Polygon" else list(u.geometry.geoms)
            for p in parts:
                msp.add_lwpolyline(list(p.exterior.coords), close=True, dxfattribs={"layer": layer})
                for ring in p.interiors:
                    msp.add_lwpolyline(list(ring.coords), close=True, dxfattribs={"layer": layer})
        doc.saveas(path)
        return path
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"us": u.us, "area": u.area, "tipo": u.tipo, **u.attributes},
         "geometry": mapping(u.geometry)} for u in units]}
    if crs:
        fc["crs"] = {"type": "name", "properties": {"name": crs}}
    path.write_text(json.dumps(fc))
    return path
