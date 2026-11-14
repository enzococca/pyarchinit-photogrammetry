"""The sections and the elevations of a chunk on a web site (:mod:`.site`): the tab *Sezioni* of the page.

Every elevation or section of the chunk (``chunk.meta["elevations"]``, :mod:`..engine.elevation`) goes in
``data/sections/<n>/``:

* ``ortho.tif`` - its orthophoto as a Cloud Optimized GeoTIFF (WebP with its alpha) in the ``(u, v)`` metres of the
  elevation: ``u`` along the wall or the trace, ``v`` up;
* ``drawing.geojson`` - in the same ``(u, v)``: the profile of a section (where its plane cuts the model) and the
  drawings made on it (units, stones, break lines, measuring points, spot heights), each with its ``layer``;
* ``drawing.svg`` - the same drawing at a drawing scale (1:20, 1:50...), with a grid of the heights when ``v`` is
  the height;

and its trace on the plan - the line where its plane meets the ground, the arc of a cylinder - is a feature of
``sections.geojson`` beside the other drawings of the plan, with the ``section`` it opens.

Frames: ``v`` is the true height of a point for an upright plane whose heights were kept (``v_is_height``); in the
local frame of a site that ``v`` - and every height of a drawing - is taken down by the base of the heights of the
site, as the DEM is. Otherwise ``v`` is measured from the frame itself and says nothing of the place. The traces
on the plan are shifted as every other coordinate of the plan.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np

COLOUR = "#0b6bcb"  # the traces of the sections on the plan
TITLES = {"sections": {"it": "Sezioni e prospetti", "en": "Sections and elevations"}}
LAYERS = {  # the layers of a drawing: title, colour
    "profile": ({"it": "Profilo", "en": "Profile"}, "#ff2d2d"),
    "US": ({"it": "US", "en": "Units"}, "#1b7f3b"),
    "USM": ({"it": "USM", "en": "Masonry units"}, "#c0392b"),
    "stones": ({"it": "Pietre", "en": "Stones"}, "#000000"),
    "lines": ({"it": "Linee", "en": "Lines"}, "#ff7f0e"),
    "points": ({"it": "Punti di misura", "en": "Measuring points"}, "#ff5d8f"),
    "quote": ({"it": "Quote", "en": "Spot heights"}, "#f59e0b"),
}


def entries(chunk) -> list[dict]:
    """The elevations and sections of ``chunk`` that can go on a site: with an orthophoto or a profile."""
    from ..engine import elevation as E

    out = []
    for entry in E.elevations(chunk):
        try:
            ortho = E.elevation_paths(chunk, entry)[0]
        except (KeyError, TypeError):
            continue
        if ortho.exists() or E.profile_path(chunk, entry) is not None:
            out.append(entry)
    return out


def trace(entry: dict, bounds) -> dict | None:
    """The trace of the elevation ``entry`` on the plan, in the coordinates of the chunk, as a GeoJSON geometry: a
    line along ``u`` at the middle height for an upright plane or a cylinder, the outline of the frame for a plane
    that lies down (a plan slice, a sloping face)."""
    from ..engine.elevation import frame_from_dict

    frame = frame_from_dict(entry["frame"])
    u0, v0, u1, v1 = (float(b) for b in bounds)
    vm = (v0 + v1) / 2
    if frame.kind == "plane" and abs(float(frame.v_axis[2])) < 0.7:
        corners = frame.point(np.array([u0, u1, u1, u0, u0]), np.array([v0, v0, v1, v1, v0]), np.zeros(5))
        return {"type": "Polygon", "coordinates": [corners[:, :2].tolist()]}
    n = 2 if frame.kind == "plane" else max(16, min(256, int(abs(u1 - u0) / 0.05)))
    us = np.linspace(u0, u1, n)
    pts = frame.point(us, np.full(n, vm), np.zeros(n))
    return {"type": "LineString", "coordinates": pts[:, :2].tolist()}


def _geometry(geom, dv: float) -> dict:
    """A shapely geometry of ``(u, v)`` as a GeoJSON geometry with ``v`` less ``dv``."""
    from shapely.geometry import mapping

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            return [round(float(c[0]), 4), round(float(c[1]) - dv, 4)]
        return [walk(x) for x in c]

    m = mapping(geom)
    return {"type": m["type"], "coordinates": walk(m["coordinates"])}


def drawing_features(chunk, entry: dict, dv: float, dz: float, clean) -> list[dict]:
    """The profile and the drawings of ``entry`` as features in ``(u, v - dv)``; heights of the drawings less ``dz``;
    ``clean``: what makes the attributes fit for the page (:func:`.site._attributes`)."""
    from shapely.geometry import LineString

    from ..archaeo import breaklines
    from ..engine.elevation import _drawing_items, _unit_label, _unit_layer

    items = _drawing_items(chunk, entry)
    feats = []

    def add(layer, geom, props):
        feats.append({"type": "Feature", "properties": {"layer": layer, **clean(props)},
                      "geometry": _geometry(geom, dv)})

    for line in items["profile"]:
        if len(line) > 1:
            add("profile", LineString(line), {"label": ""})
    for geom, rec in items["units"]:
        layer = _unit_layer(rec)
        add(layer if layer in LAYERS else "US", geom,
            {"label": _unit_label(rec), **{k: v for k, v in rec.items() if k not in ("wkt", "layer")}})
    for geom in items["stones"]:
        add("stones", geom, {"label": ""})
    for geom, rec in items["lines"]:
        add("lines", geom, {"label": breaklines.label(rec), "tipo": breaklines.KIND_LABELS.get(rec.get("kind"), ""),
                            "colour": breaklines.COLOURS.get(rec.get("kind")),
                            **(rec.get("attributes") or {})})
    for geom, rec in items["points"]:
        add("points", geom, {k: v for k, v in rec.items() if k not in ("u", "v", "x", "y", "z", "d")})
    for geom, rec in items["quote"]:
        z = rec.get("quota_q")
        try:
            z = None if z in (None, "") else round(float(z) - dz, 4)
        except (TypeError, ValueError):
            z = None
        add("quote", geom, {"label": f"{z:.3f}" if z is not None else "", "quota": z,
                            **{k: v for k, v in rec.items() if k not in ("u", "v", "quota_q", "d")}})
    return feats


def _svg(features: list[dict], extent, name: str, heights: bool) -> str:
    """The drawing of a section at a drawing scale, in millimetres of paper, with a grid of its heights."""
    from ..archaeo.sections import drawing_scale, grid_step

    u0, v0, u1, v1 = extent
    scale = drawing_scale(u1 - u0, v1 - v0)
    k = 1000.0 / scale
    margin, top = 14.0, 10.0
    width, height = (u1 - u0) * k + 2 * margin, (v1 - v0) * k + margin + top

    def xy(u, v):
        return (u - u0) * k + margin, (v1 - v) * k + top

    def d_of(coords, close):
        pts = " L ".join("{:.2f} {:.2f}".format(*xy(c[0], c[1])) for c in coords)
        return "M " + pts + (" Z" if close else "")

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.1f}mm" height="{height:.1f}mm" '
           f'viewBox="0 0 {width:.2f} {height:.2f}">', f"<title>{escape(name)} 1:{scale}</title>",
           '<rect width="100%" height="100%" fill="#ffffff"/>',
           f'<text x="{margin:.1f}" y="6" font-size="4" font-family="sans-serif">{escape(name)} - 1:{scale}</text>']
    if heights and v1 > v0:
        step = grid_step(v1 - v0)
        out.append('<g stroke="#9aa5ae" stroke-width="0.13" font-size="2.2" font-family="sans-serif">')
        for v in np.arange(math.ceil(v0 / step) * step, v1 + 1e-9, step):
            x0, y = xy(u0, v)
            x1, _ = xy(u1, v)
            out.append(f'<line x1="{x0:.2f}" y1="{y:.2f}" x2="{x1:.2f}" y2="{y:.2f}"/>')
            out.append(f'<text x="{x0 - 1:.2f}" y="{y + 0.8:.2f}" text-anchor="end" stroke="none" '
                       f'fill="#5b6670">{v:.2f}</text>')
        out.append("</g>")
    for f in features:
        layer = f["properties"].get("layer")
        colour = f["properties"].get("colour") or LAYERS.get(layer, ({}, "#333333"))[1]
        g = f["geometry"]
        if g["type"] == "LineString":
            out.append(f'<path d="{d_of(g["coordinates"], False)}" fill="none" stroke="{escape(colour)}" '
                       f'stroke-width="{0.5 if layer == "profile" else 0.35}"/>')
        elif g["type"] in ("Polygon", "MultiPolygon"):
            polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
            d = " ".join(d_of(ring, True) for poly in polys for ring in poly)
            out.append(f'<path d="{d}" fill="none" stroke="{escape(colour)}" stroke-width="0.35"/>')
        elif g["type"] == "Point":
            x, y = xy(*g["coordinates"][:2])
            out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="0.7" fill="{escape(colour)}" stroke="#000" '
                       f'stroke-width="0.15"/>')
        label = f["properties"].get("label")
        if label and g["type"] != "LineString":
            if g["type"] == "Point":
                x, y = xy(*g["coordinates"][:2])
                x, y = x + 1.2, y - 1.0
            else:
                ring = np.asarray(g["coordinates"][0] if g["type"] == "Polygon" else g["coordinates"][0][0])
                x, y = xy(*ring[:, :2].mean(0))
            out.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="2.5" font-family="sans-serif">'
                       f'{escape(str(label))}</text>')
    out.append("</svg>")
    return "\n".join(out)


def write_sections(chunk, data: Path, rel: str, shift, quality: int, clean, scrub, progress=None,
                   only=None, prefix: str = "") -> tuple[list[dict], list[dict], int]:
    """The sections and elevations of ``chunk`` in ``data/sections/`` (see the module docstring): their entries for
    ``site.json``, the features of their traces on the plan (``sections.geojson``, written by the caller) and the
    bytes written. ``shift``: the frame of the site (:class:`.site._Shift`); ``clean``/``scrub``: what makes the
    attributes and the names fit for the page; ``only``: the ids to publish (None: all); ``prefix``: of the ids of the
    sections on the site (one per chunk, so that the ids of a site are unique)."""
    import tempfile

    from ..engine import elevation as E
    from .site import _raster_info, _Shift, _vrt
    from .web import export_cog

    out, traces, size = [], [], 0
    chosen = [e for e in entries(chunk) if only is None or e["id"] in only]
    for n, entry in enumerate(chosen, 1):
        if progress is not None:
            progress(n / max(len(chosen), 1), f"{chunk.label}: {entry.get('name') or 'sezione'}")
        heights = bool(entry.get("v_is_height"))
        dv = shift.dz if heights else 0.0
        folder = data / "sections" / str(n)
        folder.mkdir(parents=True, exist_ok=True)
        sid = f"{prefix}{n}"
        name = scrub(str(entry.get("name") or "Sezione"))
        bounds = E.bounds_of(chunk, entry)
        record = {"id": sid, "name": name, "kind": "section" if entry.get("kind") == "section" else "elevation",
                  "surface": (entry.get("frame") or {}).get("kind", "plane"), "heights": heights,
                  "resolution": round(float(entry.get("resolution") or 0.0), 6)}
        ortho = E.elevation_paths(chunk, entry)[0]
        extent = [bounds[0], bounds[1] - dv, bounds[2], bounds[3] - dv]
        if ortho.exists():
            info = _raster_info(ortho)
            bands = [(ortho, 1, "Red"), (ortho, 2, "Green"), (ortho, 3, "Blue"),
                     (ortho, 4 if info["count"] >= 4 else "mask,1", "Alpha")]
            with tempfile.TemporaryDirectory(prefix=".ppg-site-", dir=data) as tmp:
                vrt = _vrt(Path(tmp) / "ortho.vrt", info, bands, "Byte", _Shift(0.0, dv), False)
                cog = export_cog(vrt, folder / "ortho.tif", "webp", quality)
            b = _raster_info(cog)["bounds"]
            extent = [min(extent[0], b[0]), min(extent[1], b[1]), max(extent[2], b[2]), max(extent[3], b[3])]
            record["ortho"] = {"url": f"{rel}/sections/{n}/ortho.tif", "size": [info["width"], info["height"]]}
            size += cog.stat().st_size
        feats = drawing_features(chunk, entry, dv, shift.dz, clean)
        record["extent"] = [round(float(v), 4) for v in extent]
        if feats:
            path = folder / "drawing.geojson"
            path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False,
                                       separators=(",", ":")), encoding="utf-8")
            present = []
            for f in feats:
                if f["properties"]["layer"] not in present:
                    present.append(f["properties"]["layer"])
            record["drawing"] = {"url": f"{rel}/sections/{n}/drawing.geojson", "count": len(feats),
                                 "groups": [{"name": k, "title": LAYERS.get(k, ({"it": k, "en": k}, ""))[0],
                                             "colour": LAYERS.get(k, ({}, "#333333"))[1],
                                             "count": sum(f["properties"]["layer"] == k for f in feats)}
                                            for k in present]}
            size += path.stat().st_size
        svg = folder / "drawing.svg"
        svg.write_text(_svg(feats, record["extent"], name, heights), encoding="utf-8")
        record["svg"] = f"{rel}/sections/{n}/drawing.svg"
        size += svg.stat().st_size
        geom = trace(entry, bounds)
        if geom is not None:
            def move(c):
                if c and isinstance(c[0], (int, float)):
                    return shift.xy(c[0], c[1])
                return [move(x) for x in c]

            geom = {"type": geom["type"], "coordinates": move(geom["coordinates"])}
            record["trace"] = geom
            traces.append({"type": "Feature", "geometry": geom,
                           "properties": {"label": name, "section": sid, "tipo": record["kind"]}})
        out.append(record)
    return out, traces, size


__all__ = ["entries", "trace", "write_sections", "drawing_features", "TITLES", "COLOUR", "LAYERS"]
