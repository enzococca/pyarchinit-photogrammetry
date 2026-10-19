"""Stratigraphic units proposed from the units SAM finds on the orthomosaic (or from any unit drawn
without its number): a US number, a kind and a description for each, with what the survey
measures - plan area, elevations from the DEM, Munsell colour of the orthomosaic, the class of the
dense cloud - and the stratigraphic relations their contacts suggest. Written into the US forms of
PyArchInit (each with its ``node_uuid``), they become the nodes and the edges of the Extended
Matrix that PyArchInit and EMStudio build from the forms.

* :func:`measure_units` - area, perimeter, elevations inside each unit (DEM), the class most of the
  dense points inside it have, the Munsell colour.
* :func:`contacts` - the pairs of units that touch in plan: length of the contact, the share of
  each outline on it, whether one lies inside the other, and the step of the DEM across it (the
  median of a narrow band on each side, the cells right on the line left out).
* :func:`propose` - the proposal. Numbers from ``first_us``, the highest units first; ``USM``
  where the cloud says masonry; the relations, each with its reason:

  * a unit inside another (``enclosed_share`` of its outline on the contact) and not higher than
    it: the fill of a cut through it, and the cut (a negative unit, *Taglio*, numbered after its
    fill) is proposed too: fill *Riempie* cut, cut *Taglia* the unit around;
  * a unit higher than the other along their contact (by ``step`` m at least): *Copre*;
  * a layer against masonry and not higher than it: *Si appoggia a* (higher: *Copre*).

  Units whose cloud class is vegetation, finds or excavation equipment are proposed unticked.
* :func:`propose_for_chunk` - the same on the products of a chunk (DEM, orthomosaic with its colour
  calibration, classified dense cloud).
* :func:`write_proposal` - the chosen units and relations in PyArchInit: the US forms (the site form
  too), the relations in both directions (never twice).
"""
from __future__ import annotations

import copy
import math
import os
from datetime import date

import numpy as np

from ..core.progress import Progress, as_progress

STEP_M = 0.02  # a contact with a smaller step says nothing about which unit is above
ENCLOSED_SHARE = 0.75
# class of the dense cloud -> (unita_tipo, d_stratigrafica as a key of TEXTS)
KIND_BY_CLASS = {64: ("USM", "Struttura muraria"), 65: ("US", "Deposito"), 66: ("US", "Taglio"),
                 67: ("US", "Pavimento"), 70: ("US", "Banco naturale"), 2: ("US", "Strato")}
NOT_UNITS = {3, 4, 5, 68, 71}  # vegetation, finds, excavation equipment: not stratigraphic units
RELATIONS = {"Copre": "Coperto da", "Riempie": "Riempito da", "Taglia": "Tagliato da",
             "Si appoggia a": "Gli si appoggia", "Uguale a": "Uguale a", "Si lega a": "Si lega a"}
TEXTS = {
    "it": {"unit": "Proposta dalla segmentazione dell'ortofoto ({day}): {area} m2 in pianta",
           "z": ", quote {z0}-{z1} m", "colour": ", colore Munsell {chip}", "class": ", nuvola: {name}",
           "cut": "Taglio proposto dal contorno del suo riempimento {fill}, dentro {around} ({day})",
           "Strato": "Strato", "Taglio": "Taglio", "Riempimento": "Riempimento", "Deposito": "Deposito",
           "Struttura muraria": "Struttura muraria", "Pavimento": "Pavimento", "Banco naturale": "Banco naturale"},
    "en": {"unit": "Proposed from the segmentation of the orthomosaic ({day}): {area} m2 in plan",
           "z": ", elevations {z0}-{z1} m", "colour": ", Munsell colour {chip}", "class": ", cloud: {name}",
           "cut": "Cut proposed from the outline of its fill {fill}, inside {around} ({day})",
           "Strato": "Layer", "Taglio": "Cut", "Riempimento": "Fill", "Deposito": "Deposit",
           "Struttura muraria": "Masonry structure", "Pavimento": "Floor", "Banco naturale": "Natural bedrock"},
}


def _parts(geom):
    return list(getattr(geom, "geoms", [geom]))


def _filled(geom):
    """The polygon(s) without their holes: what a unit surrounds."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    return unary_union([Polygon(p.exterior) for p in _parts(geom) if not p.is_empty])


class _Dem:
    """Values of a DEM inside polygons (a coarser read for large ones)."""

    def __init__(self, path):
        import rasterio

        self.src = rasterio.open(path)
        self.res = abs(self.src.res[0])

    def close(self) -> None:
        self.src.close()

    def values(self, geom, max_cells: int = 250_000) -> np.ndarray:
        from affine import Affine
        from rasterio.enums import Resampling
        from rasterio.features import geometry_mask
        from rasterio.windows import Window, from_bounds

        if geom.is_empty:
            return np.zeros(0)
        src = self.src
        try:
            win = from_bounds(*geom.bounds, transform=src.transform).round_offsets().round_lengths()
            win = win.intersection(Window(0, 0, src.width, src.height))
        except Exception:  # outside the DEM
            return np.zeros(0)
        win = Window(win.col_off, win.row_off, max(1, int(win.width) + 1), max(1, int(win.height) + 1)) \
            .intersection(Window(0, 0, src.width, src.height))
        f = max(1.0, math.sqrt(win.width * win.height / max_cells))
        h, w = max(1, int(win.height / f)), max(1, int(win.width / f))
        z = src.read(1, window=win, out_shape=(h, w), masked=True,
                     resampling=Resampling.average if f > 1 else Resampling.nearest)
        tr = src.window_transform(win) @ Affine.scale(win.width / w, win.height / h)
        inside = ~geometry_mask([geom], (h, w), tr, all_touched=min(h, w) < 4)
        vals = np.asarray(z.filled(np.nan), float)[inside & ~np.ma.getmaskarray(z)]
        return vals[np.isfinite(vals)]


def measure_units(units, dem_path=None, ortho_path=None, ccm=None, cloud=None,
                  progress: Progress | None = None) -> list[dict]:
    """Per unit: ``area_m2``, ``perimeter_m``, ``z_min``/``z_mean``/``z_max`` (None without a DEM),
    ``colour`` (:func:`archaeo.munsell.area_colour`, None without an orthomosaic or colour-science),
    ``cloud_class`` and ``class_share`` (the class most of the dense points inside have, among the
    classified ones; ``cloud``: ``(x, y, classes)`` in world coordinates)."""
    from shapely import intersects_xy

    progress = as_progress(progress)
    dem = _Dem(dem_path) if dem_path else None
    colours = ortho_path is not None
    out = []
    try:
        for i, u in enumerate(units):
            progress.check()
            progress(i / max(len(units), 1), f"Unit {i + 1}/{len(units)}")
            g = u.geometry
            m = {"area_m2": float(g.area), "perimeter_m": float(g.length), "z_min": None, "z_mean": None,
                 "z_max": None, "colour": None, "cloud_class": None, "class_share": 0.0}
            if dem is not None:
                z = dem.values(g)
                if len(z):
                    m.update(z_min=float(z.min()), z_mean=float(z.mean()), z_max=float(z.max()))
            if colours:
                try:
                    from .munsell import area_colour

                    m["colour"] = area_colour(ortho_path, g, ccm)
                except ImportError:  # no colour-science: no colours at all
                    colours = False
                except Exception:  # no pixel of the orthomosaic inside
                    m["colour"] = None
            if cloud is not None and len(cloud[0]):
                x, y, cls = cloud
                minx, miny, maxx, maxy = g.bounds
                near = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
                inside = np.flatnonzero(near)[intersects_xy(g, x[near], y[near])]
                c = cls[inside]
                c = c[(c > 1) & (c != 7) & (c != 18)]  # classified, not noise
                if len(c):
                    codes, counts = np.unique(c, return_counts=True)
                    k = int(np.argmax(counts))
                    m["cloud_class"], m["class_share"] = int(codes[k]), float(counts[k] / len(c))
            out.append(m)
    finally:
        if dem is not None:
            dem.close()
    return out


def _lines(geom):
    """The line parts of an intersection (it may hold points where two outlines only touch)."""
    out = []
    for g in getattr(geom, "geoms", [geom]):
        if g.geom_type in ("MultiLineString", "GeometryCollection"):
            out += _lines(g)
        elif g.geom_type in ("LineString", "LinearRing") and g.length:
            out.append(g)
    return out


def contacts(units, dem_path=None, tolerance: float = 0.03, band: float = 0.06, min_length: float = 0.05,
             progress: Progress | None = None) -> list[dict]:
    """Pairs of units that touch: ``{"a", "b"`` (indices), ``"length_m"``, ``"share_a"``,
    ``"share_b"`` (the fraction of each outline on the contact), ``"inside": "a"|"b"|None`` (the
    one lying within the other's outline), ``"step_m"`` (DEM on a's side minus b's, None without
    a DEM or without cells on a side)}. ``tolerance``: the gap still taken as a contact (SAM
    polygons traced on a raster do not share their vertices); ``band``: the width of the strip
    sampled on each side."""
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    progress = as_progress(progress)
    geoms = [u.geometry for u in units]
    tree = STRtree([g.buffer(tolerance) for g in geoms])
    dem = _Dem(dem_path) if dem_path else None
    out = []
    try:
        for i, a in enumerate(geoms):
            progress.check()
            progress(i / max(len(geoms), 1), "Contacts")
            for j in sorted(int(k) for k in tree.query(a) if int(k) > i):
                b = geoms[j]
                if a.distance(b) > tolerance:
                    continue
                on_a = a.boundary.intersection(b.buffer(tolerance))
                on_b = b.boundary.intersection(a.buffer(tolerance))
                la, lb = on_a.length, on_b.length
                if max(la, lb) < min_length:
                    continue
                inside = "b" if b.buffer(-tolerance).within(_filled(a)) else \
                    "a" if a.buffer(-tolerance).within(_filled(b)) else None
                c = {"a": i, "b": j, "length_m": float((la + lb) / 2), "inside": inside, "step_m": None,
                     "share_a": float(la / max(a.boundary.length, 1e-9)),
                     "share_b": float(lb / max(b.boundary.length, 1e-9))}
                if dem is not None:
                    line = unary_union(_lines(on_a) or [on_a])
                    w = max(band, 3 * dem.res)
                    strip = line.buffer(w).difference(line.buffer(max(dem.res, tolerance)))
                    za, zb = dem.values(strip.intersection(a)), dem.values(strip.intersection(b))
                    if len(za) >= 3 and len(zb) >= 3:
                        c["step_m"] = float(np.median(za) - np.median(zb))
                out.append(c)
    finally:
        if dem is not None:
            dem.close()
    return out


def _name(unit) -> str:
    return f"{unit.tipo or 'US'} {unit.us}" if unit.us else (unit.tipo or "US") + " ?"


def _text(lang: str, key: str, **kw) -> str:
    return TEXTS.get(lang, TEXTS["it"])[key].format(**kw)


def _description(m: dict, lang: str, day: str, class_names: dict) -> str:
    text = _text(lang, "unit", day=day, area=f"{m['area_m2']:.2f}")
    if m.get("z_min") is not None:
        text += _text(lang, "z", z0=f"{m['z_min']:.3f}", z1=f"{m['z_max']:.3f}")
    if m.get("colour"):
        text += _text(lang, "colour", chip=m["colour"]["chip"])
    if m.get("cloud_class") is not None:
        name = class_names.get(m["cloud_class"], str(m["cloud_class"]))
        text += _text(lang, "class", name=name)
    return text + "."


def propose(units, measures: list[dict], touching: list[dict], first_us: int, area: str | None = None,
            sito: str | None = None, step: float = STEP_M, enclosed_share: float = ENCLOSED_SHARE,
            cuts: bool = True, lang: str = "it", class_names: dict | None = None) -> dict:
    """The proposal (see the module docstring) from :func:`measure_units` and :func:`contacts`:
    ``{"units": [{"key", "unit"`` (a copy with ``us``, ``tipo``, ``area``, ``sito``), ``"source"``
    (index of the unit given, None for a cut), ``"kind": "unit"|"cut"``, ``"fields"`` (of the US
    form), ``"measures"``, ``"cloud_name"``, ``"use"}``, a cut also ``"fill"`` (the key of its
    fill)], ``"relations": [{"upper", "lower"`` (keys), ``"relation"``,
    ``"inverse"``, ``"why": "higher"|"fill"|"cut"|"abuts"``, ``"step_m"``, ``"contact_m"``,
    ``"around"`` (for a fill, the key of the unit around it), ``"use"}]}``. Units keep a number
    they already have."""
    from .stratigraphy import UnitPolygon

    day = date.today().isoformat()
    names = class_names or {}
    kinds = []  # (unita_tipo, key of the stratigraphic definition)
    for u, m in zip(units, measures):
        tipo, defin = "US", "Strato"
        cls = m.get("cloud_class")
        if cls in KIND_BY_CLASS and m.get("class_share", 0) >= 0.5:
            tipo, defin = KIND_BY_CLASS[cls]
        kinds.append((u.tipo if u.us and u.tipo else tipo, defin))
    # the fills: inside another unit, not masonry nor a cut, and not higher than the unit around
    fill_of, cut_in = {}, {}
    for c in touching:
        if c["inside"] is None:
            continue
        inner, outer = (c["b"], c["a"]) if c["inside"] == "b" else (c["a"], c["b"])
        share = c["share_b"] if c["inside"] == "b" else c["share_a"]
        step_in = None if c["step_m"] is None else (-c["step_m"] if c["inside"] == "b" else c["step_m"])
        if share < enclosed_share or kinds[inner][0] == "USM" or (step_in is not None and step_in > step) \
                or measures[inner].get("cloud_class") in NOT_UNITS:
            continue
        found = cut_in if kinds[inner][1] == "Taglio" else fill_of  # an empty cut seen as such by the cloud
        if inner not in found or units[outer].geometry.area < units[found[inner]].geometry.area:
            found[inner] = outer  # the smallest unit around it
    # numbers: the highest units first, each cut after its fill
    order = sorted(range(len(units)), key=lambda i: (measures[i].get("z_mean") is None,
                                                     -(measures[i].get("z_mean") or 0.0), -units[i].geometry.area))
    taken = {str(u.us) for u in units if u.us}
    number = int(first_us)

    def next_number():
        nonlocal number
        while str(number) in taken:
            number += 1
        taken.add(str(number))
        number += 1
        return str(number - 1)

    items, key_of, cut_key = [], {}, {}
    for i in order:
        u, m = units[i], measures[i]
        tipo, defin = kinds[i][0], _text(lang, "Riempimento" if i in fill_of else kinds[i][1])
        unit = copy.copy(u)
        unit.attributes = dict(u.attributes or {})
        unit.us = str(u.us) if u.us else next_number()
        unit.tipo, unit.area, unit.sito = tipo, u.area or area, u.sito or sito
        fields = {"d_stratigrafica": defin, "descrizione": _description(m, lang, day, names)}
        if m.get("colour"):
            fields["colore"] = m["colour"]["chip"]
        if m.get("z_min") is not None:
            fields.update(quota_min_abs=round(m["z_min"], 3), quota_max_abs=round(m["z_max"], 3))
        key_of[i] = len(items)
        cls = m.get("cloud_class")
        items.append({"key": len(items), "unit": unit, "source": i, "kind": "unit", "fields": fields, "measures": m,
                      "use": cls not in NOT_UNITS, "cloud_name": names.get(cls, str(cls)) if cls is not None else ""})
        if cuts and i in fill_of:
            around = units[fill_of[i]]
            cut = UnitPolygon(u.geometry, us=next_number(), area=unit.area, tipo="US",
                              attributes={"stratigraph_index_us": 2}, sito=unit.sito)
            cut_key[i] = len(items)
            items.append({"key": len(items), "unit": cut, "source": None, "kind": "cut", "fill": key_of[i],
                          "fields": {"d_stratigrafica": _text(lang, "Taglio"),
                                     "descrizione": _text(lang, "cut", fill=_name(unit), around="{around}", day=day)},
                          "measures": {"area_m2": float(u.geometry.area)}, "use": True, "around": fill_of[i]})
    for it in items:  # the name of the unit around a cut, now that it has its number
        if it["kind"] == "cut":
            it["fields"]["descrizione"] = it["fields"]["descrizione"].replace(
                "{around}", _name(items[key_of[it.pop("around")]]["unit"]))
    rels = []

    def add(upper, lower, relation, why, c, around=None):
        rels.append({"upper": upper, "lower": lower, "relation": relation, "inverse": RELATIONS[relation],
                     "why": why, "step_m": c.get("step_m"), "contact_m": c.get("length_m"),
                     "use": items[upper]["use"] and items[lower]["use"], "around": around})

    for c in touching:
        a, b = c["a"], c["b"]
        s = c["step_m"]
        if cut_in.get(b) == a or cut_in.get(a) == b:
            inner, outer = (b, a) if cut_in.get(b) == a else (a, b)
            add(key_of[inner], key_of[outer], "Taglia", "cut", c)
            continue
        if fill_of.get(b) == a or fill_of.get(a) == b:
            inner, outer = (b, a) if fill_of.get(b) == a else (a, b)
            if inner in cut_key:
                add(key_of[inner], cut_key[inner], "Riempie", "fill", c, around=key_of[outer])
                add(cut_key[inner], key_of[outer], "Taglia", "cut", c)
            continue
        wall_a, wall_b = kinds[a][0] == "USM", kinds[b][0] == "USM"
        if wall_a != wall_b:
            wall, layer = (a, b) if wall_a else (b, a)
            up = None if s is None else (s if layer == a else -s)  # the layer above the wall
            if up is not None and up > step:
                add(key_of[layer], key_of[wall], "Copre", "higher", c)
            else:
                add(key_of[layer], key_of[wall], "Si appoggia a", "abuts", c)
        elif not (wall_a and wall_b) and s is not None and abs(s) > step:
            upper, lower = (a, b) if s > 0 else (b, a)
            add(key_of[upper], key_of[lower], "Copre", "higher", c)
    return {"units": items, "relations": rels}


def _cloud_xyc(chunk):
    """``(x, y, classes)`` of the dense cloud in world coordinates, None when it is not classified."""
    counts = chunk.point_classes()
    if not counts or not any(int(k) > 1 and int(k) not in (7, 18) for k in counts):
        return None
    pc = chunk.point_cloud
    world = chunk.to_world(pc.xyz)
    return world[:, 0], world[:, 1], np.asarray(pc.classification)


def propose_for_chunk(chunk, units, first_us: int, area: str | None = None, sito: str | None = None,
                      progress: Progress | None = None, **kw) -> dict:
    """:func:`propose` on the products of ``chunk``: its DEM, its orthomosaic with the colour
    calibration (*Calibra il colore*), its classified dense cloud; ``kw`` go to :func:`propose`
    (``step``, ``cuts``, ``lang``...) and ``tolerance`` to :func:`contacts`."""
    from ..core.pointcloud import ARCHAEO_CLASSES, ASPRS_CLASSES

    progress = as_progress(progress)
    dem = chunk.dem_path if chunk.dem_path is not None and os.path.exists(chunk.dem_path) else None
    ortho = chunk.orthomosaic_path if chunk.orthomosaic_path is not None and os.path.exists(chunk.orthomosaic_path) \
        else None
    ccm = ((chunk.meta or {}).get("colour_chart") or {}).get("ccm")
    progress(0.02, "Dense cloud")
    cloud = _cloud_xyc(chunk)
    names = {int(k): str(v).split(" / ")[0] for k, v in {**ASPRS_CLASSES, **ARCHAEO_CLASSES}.items()}
    names.update({int(k): str(v) for k, v in ((chunk.meta or {}).get("classes") or {}).items()})
    tolerance = kw.pop("tolerance", None)
    if tolerance is None:  # two pixels of the orthomosaic, at least 2 cm
        res = 0.0
        if ortho is not None:
            import rasterio

            with rasterio.open(ortho) as src:
                res = abs(src.res[0])
        tolerance = max(0.02, 2 * res)
    measures = measure_units(units, dem, ortho, ccm, cloud, progress=progress.sub(0.05, 0.6))
    touching = contacts(units, dem, tolerance=tolerance, progress=progress.sub(0.65, 0.3))
    progress(0.97, "Proposal")
    return propose(units, measures, touching, first_us, area=area, sito=sito, class_names=names, **kw)


def write_proposal(pa, proposal: dict, sito: str, keys=None) -> dict:
    """The units of ``proposal`` (those with ``use``, or the ``keys`` given) as US forms of
    PyArchInit (``create_us``: ``node_uuid`` and ``entity_uuid`` with them, the site form too) and
    their relations with ``use``, in both directions: ``{"created": [names], "existing": [names
    already there, left as they are], "relations": n added, "missing": [...]}``."""
    from .timeline import write_relations

    chosen = {it["key"] for it in proposal["units"] if (it["use"] if keys is None else it["key"] in set(keys))}
    created, existing = [], []
    for it in proposal["units"]:
        if it["key"] not in chosen:
            continue
        u = it["unit"]
        try:
            pa.create_us(u.sito or sito, u.area or "", u.us, u.tipo or "US", **it["fields"])
            created.append(_name(u))
        except FileExistsError:
            existing.append(_name(u))
    units = {it["key"]: it["unit"] for it in proposal["units"]}
    rels = [{"upper": units[r["upper"]], "lower": units[r["lower"]], "relation": r["relation"],
             "inverse": r["inverse"]} for r in proposal["relations"]
            if r["use"] and r["upper"] in chosen and r["lower"] in chosen]
    out = write_relations(pa, rels, site=sito)
    return {"created": created, "existing": existing, "relations": out["added"], "missing": out["missing"]}
