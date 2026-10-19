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
