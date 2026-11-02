"""Break lines drawn on the plan (or on an elevation): the foot and the top of a wall, the edge of a
cut, the outline of the area to triangulate - the constraints of the TIN (:mod:`..engine.tin`) and
the lines a DEM edit keeps (:mod:`..engine.dem_edit`).

They are kept with the drawings of the chunk, ``chunk.meta["drawings"]["lines"]`` (the plan) and
``chunk.meta["drawings"]["frames"][<elevation id>]["lines"]`` (an elevation, in its ``(u, v)``), as
records ``{"id", "kind", "coords": [[x, y, z or None], ...], "attributes": {...}}``: ``kind``
``breakline`` (an edge the triangles never cross, with its heights), ``soft`` (its vertices only) or
``boundary`` (the TIN stops there). A line drawn on the plan has at each vertex the height of the DEM
there, read when it is drawn (None outside the DEM).
"""
from __future__ import annotations

import json
import math
import uuid
from pathlib import Path

KINDS = ("breakline", "soft", "boundary")
KIND_LABELS = {"breakline": "Linea di rottura", "soft": "Linea morbida", "boundary": "Contorno"}
COLOURS = {"breakline": "#ff7f0e", "soft": "#2ca02c", "boundary": "#d62728"}
DXF_COLOURS = {"breakline": 30, "soft": 3, "boundary": 1}  # AutoCAD colour index: orange, green, red
LAYER = "Linee di rottura"  # the layer of the 2D view
COLUMNS = {"ppg_id": "TEXT", "kind": "TEXT", "tipo": "TEXT", "etichetta": "TEXT", "z_min": "REAL", "z_max": "REAL"}


def new_record(kind: str, coords, attributes: dict | None = None) -> dict:
    """A record of a line ``kind`` through ``coords`` ((x, y) or (x, y, z); a z None or not finite: no height)."""
    if kind not in KINDS:
        raise ValueError(f"kind {kind!r}: one of {', '.join(KINDS)}")
    pts = []
    for c in coords:
        z = c[2] if len(c) > 2 else None
        z = float(z) if z is not None and math.isfinite(float(z)) else None
        pts.append([float(c[0]), float(c[1]), z])
    return {"id": uuid.uuid4().hex, "kind": kind, "coords": pts, "attributes": dict(attributes or {})}


def valid(rec) -> bool:
    """Whether ``rec`` is a line record that can be drawn (a kind, at least two vertices with x and y)."""
    try:
        return rec.get("kind") in KINDS and len(rec.get("coords") or []) >= 2 and \
            all(len(c) >= 2 and math.isfinite(float(c[0])) and math.isfinite(float(c[1])) for c in rec["coords"])
    except (TypeError, ValueError, AttributeError):
        return False


def records(chunk, frame: str | None = None) -> list[dict]:
    """The lines ``chunk`` keeps on the plan, or on the elevation ``frame`` (copies; broken records left out)."""
    data = (chunk.meta.get("drawings") or {}) if chunk is not None else {}
    if frame is not None:
        data = (data.get("frames") or {}).get(frame) or {}
    return [json.loads(json.dumps(r)) for r in data.get("lines") or [] if valid(r)]


def heights(rec: dict) -> list:
    return [c[2] if len(c) > 2 else None for c in rec["coords"]]


def label(rec: dict) -> str:
    """The label of a line on the map: its name, or its kind."""
    a = rec.get("attributes") or {}
    return str(a.get("etichetta") or "")


def for_tin(recs, kinds=KINDS) -> list[dict]:
    """``recs`` as the break lines of :func:`..engine.tin.build_tin` (``{id, kind, coords}``; a vertex without a
    height is read from the DEM there by the TIN)."""
    out = []
    for r in recs or []:
        if not valid(r) or r["kind"] not in kinds:
            continue
        coords = []
        for c in r["coords"]:
            z = c[2] if len(c) > 2 else None
            coords.append((float(c[0]), float(c[1])) + ((float(z),) if z is not None else (float("nan"),)))
        out.append({"id": r.get("id"), "kind": r["kind"], "coords": coords})
    return out


def as_arrays(recs, kinds=("breakline", "soft")) -> list:
    """``recs`` as (n, 3) arrays (NaN where a vertex has no height), for :func:`..engine.dem_edit.fill_area`."""
    import numpy as np

    return [np.array([[float(c[0]), float(c[1]), float("nan") if (len(c) < 3 or c[2] is None) else float(c[2])]
                      for c in r["coords"]], float) for r in recs or [] if valid(r) and r["kind"] in kinds]


def _row(rec: dict) -> dict:
    zs = [z for z in heights(rec) if z is not None]
    return {"ppg_id": rec.get("id"), "kind": rec["kind"], "tipo": KIND_LABELS[rec["kind"]], "etichetta": label(rec),
            "z_min": min(zs) if zs else None, "z_max": max(zs) if zs else None}


def _geometry(rec: dict, z: bool):
    from shapely.geometry import LineString

    if z:
        return LineString([(c[0], c[1], c[2]) for c in rec["coords"]])
    return LineString([(c[0], c[1]) for c in rec["coords"]])


def with_heights(rec: dict) -> bool:
    return all(z is not None for z in heights(rec))


def write_lines(recs, path, crs: str | None = None) -> Path:
    """The lines into ``path``: ``.dxf`` (layer ``LINEE``, a 3D polyline when every vertex has its height, the colour
    of its kind), ``.gpkg`` (table ``linee``, with Z when every line has its heights, columns :data:`COLUMNS`) or
    ``.geojson`` (property ``kind``, as ``ppg tin build --breaklines`` reads them)."""
    from ..io import gpkg

    path = Path(path)
    recs = [r for r in recs or [] if valid(r)]
    ext = path.suffix.lower()
    if ext == ".dxf":
        import ezdxf

        dxf = ezdxf.new("R2010", setup=True)
        dxf.units = ezdxf.units.M
        dxf.layers.add("LINEE", color=30)
        msp = dxf.modelspace()
        for r in recs:
            attribs = {"layer": "LINEE", "color": DXF_COLOURS[r["kind"]]}
            if r["kind"] == "soft":
                attribs["linetype"] = "DASHED"
            if with_heights(r):
                msp.add_polyline3d([tuple(map(float, c[:3])) for c in r["coords"]], dxfattribs=attribs)
            else:
                msp.add_lwpolyline([(float(c[0]), float(c[1])) for c in r["coords"]], dxfattribs=attribs)
        dxf.saveas(path)
        return path
    if ext == ".gpkg":
        z = bool(recs) and all(with_heights(r) for r in recs)
        srs = gpkg.srs_of(crs)
        gpkg.refuse_foreign(path)
        con = gpkg.connect(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            gpkg.create(con, srs)
            cols = gpkg.ensure_table(con, "linee", "LINESTRING", COLUMNS, srs[0], "Linee di rottura", recreate=True,
                                     z=z)
            gpkg.replace_rows(con, "linee", cols, [(_geometry(r, z), _row(r)) for r in recs], srs[0], z=z)
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
        return path
    if ext in (".geojson", ".json"):
        feats = [{"type": "Feature", "properties": {**_row(r), "id": r.get("id")},
                  "geometry": {"type": "LineString",
                               "coordinates": [[c[0], c[1]] + ([c[2]] if with_heights(r) else []) for c in r["coords"]]}}
                 for r in recs]
        path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, indent=1), encoding="utf-8")
        return path
    raise ValueError(f"{path.name}: .dxf, .gpkg or .geojson")
