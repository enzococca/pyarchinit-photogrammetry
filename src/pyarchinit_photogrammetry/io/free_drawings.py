"""Free drawings in 3D (:mod:`..core.free_drawings`) out of the program and back in.

* GeoPackage with Z: three tables, :data:`TABLES` (``disegni_3d_linee`` LINESTRINGZ, ``disegni_3d_poligoni``
  POLYGONZ, ``disegni_3d_punti`` POINTZ; ``z = 1`` in ``gpkg_geometry_columns``), columns ``ppg_id``, ``layer``,
  ``name``, ``attributes`` (the attributes as JSON text) and one column per attribute whose name fits a column
  (:data:`COLUMN_NAME`) and is not taken; the same tables go into the live GeoPackage of QGIS and RustGIS, one-way
  (:meth:`.qgis_link.LiveLink.write_free`).
* GeoJSON: the coordinates as they are, with the legacy ``crs`` member for a chunk in an EPSG system (none for a local
  one), properties ``ppg_id``, ``layer``, ``kind``, ``name``, ``attributes`` (an object), ``colour`` when it has one.
* DXF: one DXF layer per user layer (its colour), lines and polygons as 3D POLYLINE (polygons closed), points as POINT;
  the identity, layer, kind, name and attributes as XDATA of the application ``PPG`` (a JSON text in pieces of
  :data:`XDATA_PIECE` characters), so that the file comes back as it went out.
* CSV: one row per vertex, ``id,layer,kind,name,vertex,x,y,z``.

:func:`read` reads the GeoPackage (the three tables, or any table of points, lines or polygons, with Z or not), the
GeoJSON and the DXF written here; :func:`import_features` adds what it reads to a chunk: a feature without Z takes the
heights of the DEM (missing ones interpolated along the drawing, refused when no vertex is on the DEM), the layer from
its ``layer`` column, else the name of the file, a new identity when the one it has is already in the chunk. Since
1.7.2."""
from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path

import numpy as np

from ..core import free_drawings

log = logging.getLogger(__name__)

# table and geometry (with Z); ``gpkg_geometry_columns`` holds the base type (LINESTRING...) with z = 1
TABLES = {"line": ("disegni_3d_linee", "LINESTRINGZ"), "polygon": ("disegni_3d_poligoni", "POLYGONZ"),
          "point": ("disegni_3d_punti", "POINTZ")}
TITLES = {"line": "Disegni 3D - linee (ppg)", "polygon": "Disegni 3D - poligoni (ppg)",
          "point": "Disegni 3D - punti (ppg)"}  # the layers of QGIS and RustGIS
FREE_PREFIX = "disegni_3d_"
FORMATS = ("dxf", "gpkg", "geojson", "csv")
SUFFIXES = {".dxf": "dxf", ".gpkg": "gpkg", ".geojson": "geojson", ".json": "geojson", ".csv": "csv"}
COLUMN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
BASE_COLUMNS = ("ppg_id", "layer", "name", "attributes", "colour")
RESERVED = {"fid", "geom", *BASE_COLUMNS, "kind"}
XDATA_APP = "PPG"
XDATA_PIECE = 240  # characters of each XDATA string (DXF strings hold 255)
CSV_HEADER = ["id", "layer", "kind", "name", "vertex", "x", "y", "z"]
_KIND_OF = {"LineString": "line", "Polygon": "polygon", "Point": "point"}


# ---------------------------------------------------------------- writing
def _features(chunk, layer: str | None = None) -> list[dict]:
    return free_drawings.features(chunk, layer)


def _geometry(rec: dict):
    import shapely

    xyz = [tuple(float(v) for v in p) for p in rec["xyz"]]
    if rec["kind"] == "line":
        return shapely.LineString(xyz)
    if rec["kind"] == "polygon":
        return shapely.Polygon(xyz)
    return shapely.Point(xyz[0])


def attribute_columns(feats: list[dict]) -> list[str]:
    """The attributes of ``feats`` that become columns of their own: names fit for a column, not taken (case
    insensitive), in the order met."""
    out, taken = [], {c.lower() for c in RESERVED}
    for rec in feats:
        for key in (rec.get("attributes") or {}):
            if isinstance(key, str) and COLUMN_NAME.match(key) and key.lower() not in taken:
                taken.add(key.lower())
                out.append(key)
    return out


def _row(rec: dict, columns: list[str]) -> dict:
    attrs = rec.get("attributes") or {}
    values = {"ppg_id": rec["id"], "layer": rec["layer"], "name": rec.get("name") or "",
              "attributes": json.dumps(attrs, ensure_ascii=False, sort_keys=True), "colour": rec.get("colour")}
    for c in columns:
        v = attrs.get(c)
        if v is None or isinstance(v, str):
            values[c] = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            values[c] = v
        else:  # lists, objects, true/false: as JSON, as the attributes column has them
            values[c] = json.dumps(v, ensure_ascii=False, sort_keys=True)
    return values


def write_tables(con, chunk, srs_id: int, layer: str | None = None, kinds=None) -> list[str]:
    """The three tables of the free drawings of ``chunk`` (``layer`` only, when given) written in the open GeoPackage
    ``con`` (inside a transaction; :func:`.gpkg.create` done). ``kinds``: only the tables of those kinds (default all
    three). Returns the tables written."""
    from . import gpkg

    feats = _features(chunk, layer)
    written = []
    for kind, (table, gtype) in TABLES.items():
        if kinds is not None and kind not in kinds:
            continue
        mine = [r for r in feats if r["kind"] == kind]
        extra = attribute_columns(mine)
        # the base type in gpkg_geometry_columns (LINESTRING...), z = 1: the standard's way, as the elevations' tables
        cols = gpkg.ensure_table(con, table, gtype.removesuffix("Z"),
                                 {**{c: "TEXT" for c in BASE_COLUMNS}, **{c: "TEXT" for c in extra}},
                                 srs_id, identifier=TITLES[kind], recreate=True, z=True)
        gpkg.replace_rows(con, table, cols, [(_geometry(r), _row(r, extra)) for r in mine], srs_id, z=True)
        written.append(table)
    return written


def write_gpkg(chunk, path, layer: str | None = None) -> Path:
    """The free drawings of ``chunk`` (``layer`` only) into the GeoPackage ``path`` (its other tables kept; a file that
    is not a GeoPackage refused: :class:`FileExistsError`). The tables of the kinds that have no drawing are left out."""
    from . import gpkg

    path = Path(path)
    if path.exists() and not gpkg.is_geopackage(path):
        gpkg.refuse_foreign(path)
        path.unlink()  # empty (a save dialog left it): nothing to lose
    srs = gpkg.srs_of(chunk.crs)
    kinds = {r["kind"] for r in _features(chunk, layer)}
    con = gpkg.connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        gpkg.create(con, srs)
        write_tables(con, chunk, srs[0], layer, kinds=kinds)
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return path


def _crs_member(crs: str | None) -> dict | None:
    from ..core import crs as crs_mod

    code = crs_mod.epsg_code(crs) if crs else None
    return {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{code}"}} if code else None


def write_geojson(chunk, path, layer: str | None = None) -> Path:
    """The free drawings into a GeoJSON FeatureCollection, the coordinates as they are (x, y, z of the chunk)."""
    feats = []
    for rec in _features(chunk, layer):
        xyz = [[float(v) for v in p] for p in rec["xyz"]]
        if rec["kind"] == "line":
            geometry = {"type": "LineString", "coordinates": xyz}
        elif rec["kind"] == "polygon":
            geometry = {"type": "Polygon", "coordinates": [xyz + [xyz[0]]]}
        else:
            geometry = {"type": "Point", "coordinates": xyz[0]}
        props = {"ppg_id": rec["id"], "layer": rec["layer"], "kind": rec["kind"], "name": rec.get("name") or "",
                 "attributes": rec.get("attributes") or {}}
        if rec.get("colour"):
            props["colour"] = rec["colour"]
        feats.append({"type": "Feature", "properties": props, "geometry": geometry})
    data: dict = {"type": "FeatureCollection"}
    member = _crs_member(chunk.crs)
    if member is not None:
        data["crs"] = member
    data["features"] = feats
    path = Path(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _dxf_layer(name: str) -> str:
    """A name a DXF layer can have (``<>/\\":;?*|=``` and control characters replaced)."""
    out = re.sub(r'[<>/\\":;?*|=`\x00-\x1f]', "_", name).strip() or "0"
    return out[:255]


def write_dxf(chunk, path, layer: str | None = None) -> Path:
    """The free drawings into a DXF (R2010, metres): a DXF layer per user layer, 3D polylines and points, what the
    drawing is in the XDATA of ``PPG``."""
    import ezdxf
    from ezdxf import colors

    dxf = ezdxf.new("R2010", setup=True)
    dxf.units = ezdxf.units.M
    dxf.appids.add(XDATA_APP)
    msp = dxf.modelspace()
    colours = {x["name"]: x.get("colour") or free_drawings.DEFAULT_COLOUR for x in free_drawings.layers(chunk)}
    names: dict[str, str] = {}
    for rec in _features(chunk, layer):
        user = rec["layer"]
        if user not in names:
            name = _dxf_layer(user)
            while name in names.values() and name.lower() != _dxf_layer(user).lower():
                name += "_"
            names[user] = name
            if name not in dxf.layers:
                lay = dxf.layers.add(name)
                hexa = (colours.get(user) or free_drawings.DEFAULT_COLOUR).lstrip("#")
                try:
                    lay.rgb = (int(hexa[0:2], 16), int(hexa[2:4], 16), int(hexa[4:6], 16))
                except ValueError:
                    pass
        attribs = {"layer": names[user]}
        if rec.get("colour"):
            hexa = rec["colour"].lstrip("#")
            try:
                attribs["true_color"] = colors.rgb2int((int(hexa[0:2], 16), int(hexa[2:4], 16), int(hexa[4:6], 16)))
            except ValueError:
                pass
        xyz = [tuple(float(v) for v in p) for p in rec["xyz"]]
        if rec["kind"] == "point":
            entity = msp.add_point(xyz[0], dxfattribs=attribs)
        else:
            entity = msp.add_polyline3d(xyz, close=rec["kind"] == "polygon", dxfattribs=attribs)
        text = json.dumps({"ppg_id": rec["id"], "layer": user, "kind": rec["kind"], "name": rec.get("name") or "",
                           "attributes": rec.get("attributes") or {}, "colour": rec.get("colour")},
                          ensure_ascii=False, sort_keys=True)
        entity.set_xdata(XDATA_APP, [(1000, text[i:i + XDATA_PIECE]) for i in range(0, len(text), XDATA_PIECE)]
                         or [(1000, "{}")])
    path = Path(path)
    dxf.saveas(path)
    return path


def write_csv(chunk, path, layer: str | None = None) -> Path:
    """One row per vertex: ``id,layer,kind,name,vertex,x,y,z`` (vertices counted from 1)."""
    path = Path(path)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for rec in _features(chunk, layer):
            for i, (x, y, z) in enumerate(rec["xyz"], 1):
                w.writerow([rec["id"], rec["layer"], rec["kind"], rec.get("name") or "", i, repr(float(x)),
                            repr(float(y)), repr(float(z))])
    return path


def export(chunk, path, fmt: str | None = None, layer: str | None = None) -> Path:
    """The free drawings of ``chunk`` (``layer`` only) into ``path`` in ``fmt`` (one of :data:`FORMATS`; default by
    the suffix). ValueError for another format."""
    fmt = fmt or SUFFIXES.get(Path(path).suffix.lower())
    writer = {"gpkg": write_gpkg, "geojson": write_geojson, "dxf": write_dxf, "csv": write_csv}.get(fmt or "")
    if writer is None:
        raise ValueError(f"free drawings are exported as {', '.join(FORMATS)}, not {Path(path).suffix or fmt!r}")
    return writer(chunk, path, layer)


# ---------------------------------------------------------------- reading
def _parts(geom) -> list:
    """The single geometries of ``geom`` (a multi-part one taken apart)."""
    if geom is None or geom.is_empty:
        return []
    if hasattr(geom, "geoms"):
        return [g for part in geom.geoms for g in _parts(part)]
    return [geom]


def _coords(geom) -> tuple[str, list, bool] | None:
    """``(kind, [[x, y, z or None], ...], has_z)`` of a single geometry; a polygon's exterior ring open (its holes are
    counted by :func:`read` as ``holes``)."""
    kind = _KIND_OF.get(geom.geom_type)
    if kind is None:
        return None
    ring = geom.exterior.coords if kind == "polygon" else geom.coords
    pts = [list(c) for c in ring]
    if kind == "polygon" and len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    has_z = bool(geom.has_z)
    return kind, [[float(p[0]), float(p[1]), float(p[2]) if has_z and len(p) > 2 else None] for p in pts], has_z


def _record(kind, xyz, has_z, props: dict, attributes, holes: int = 0) -> dict:
    return {"id": str(props.get("ppg_id") or props.get("id") or "") or None, "kind": kind,
            "layer": props.get("layer") or None, "name": str(props.get("name") or ""),
            "attributes": attributes, "colour": props.get("colour") or None, "xyz": xyz, "has_z": has_z,
            "holes": holes}


def _holes(geom) -> int:
    return len(geom.interiors) if geom.geom_type == "Polygon" else 0


def _attributes_of(values: dict, skip=RESERVED | {"colour", "ppg_sig", "ppg_parte"}) -> dict:
    """The attributes of a row: its ``attributes`` JSON when it has one, else its other columns."""
    raw = values.get("attributes")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            got = json.loads(raw)
            if isinstance(got, dict):
                return got
        except ValueError:
            pass
    return {k: v for k, v in values.items() if k not in skip and v is not None}


def _read_gpkg(path: Path) -> list[dict]:
    import sqlite3

    from . import gpkg

    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute("SELECT table_name FROM gpkg_geometry_columns ORDER BY table_name")]
    finally:
        con.close()
    ours = [t for t, _g in TABLES.values() if t in tables]
    out = []
    for table in ours or tables:
        for geom, values in gpkg.read_table(path, table):
            for part in _parts(geom):
                got = _coords(part)
                if got is not None:
                    out.append(_record(*got, values, _attributes_of(values), _holes(part)))
    return out


def _read_geojson(path: Path) -> list[dict]:
    import shapely.geometry

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    feats = data.get("features") if data.get("type") == "FeatureCollection" else [data]
    out = []
    for f in feats or []:
        if not isinstance(f, dict) or not f.get("geometry"):
            continue
        props = dict(f.get("properties") or {})
        attrs = props.get("attributes") if isinstance(props.get("attributes"), dict) else _attributes_of(props)
        for part in _parts(shapely.geometry.shape(f["geometry"])):
            got = _coords(part)
            if got is not None:
                out.append(_record(*got, props, dict(attrs), _holes(part)))
    return out


def _read_dxf(path: Path) -> list[dict]:
    import ezdxf

    dxf = ezdxf.readfile(str(path))
    out = []
    for e in dxf.modelspace():
        props: dict = {}
        if e.has_xdata(XDATA_APP):
            text = "".join(str(v) for code, v in e.get_xdata(XDATA_APP) if code == 1000)
            try:
                props = json.loads(text) if text else {}
            except ValueError:
                props = {}
        if e.dxf.layer != "0":  # the default layer of a CAD drawing is no layer of the user's: the file's name
            props.setdefault("layer", e.dxf.layer)
        kind = e.dxftype()
        if kind == "POINT":
            p = e.dxf.location
            xyz, k = [[float(p.x), float(p.y), float(p.z)]], "point"
        elif kind == "POLYLINE":
            flat = e.is_2d_polyline  # a 2D polyline of a CAD drawing: its elevation is not a height of the survey
            xyz = [[float(v.dxf.location.x), float(v.dxf.location.y), None if flat else float(v.dxf.location.z)]
                   for v in e.vertices]
            k = "polygon" if e.is_closed else "line"
        elif kind == "LWPOLYLINE":
            xyz = [[float(x), float(y), None] for x, y, *_rest in e.get_points()]
            k = "polygon" if e.closed else "line"
        else:
            continue
        if not e.has_xdata(XDATA_APP) and all(p[2] == 0.0 for p in xyz):  # a flat CAD drawing at 0: no heights
            xyz = [[p[0], p[1], None] for p in xyz]
        has_z = all(p[2] is not None for p in xyz)
        attrs = props.get("attributes") if isinstance(props.get("attributes"), dict) else {}
        out.append(_record(props.get("kind") or k, xyz, has_z, props, dict(attrs)))
    return out


def read(path) -> list[dict]:
    """The drawings of a GeoPackage, a GeoJSON or a DXF: ``[{"id" (None when the file has none), "kind", "layer" (None
    when the file has none), "name", "attributes", "colour", "xyz" ([[x, y, z or None], ...]), "has_z"}]``."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".gpkg":
        return _read_gpkg(path)
    if ext in (".geojson", ".json"):
        return _read_geojson(path)
    if ext == ".dxf":
        return _read_dxf(path)
    raise ValueError(f"free drawings are read from GeoPackage, GeoJSON or DXF, not {ext!r}")


def fill_missing(values: list, xy) -> list | None:
    """``values`` (None where a vertex has none) with the missing ones interpolated along the polyline ``xy`` (by its
    length in plan; the nearest known value beyond the ends); None when no vertex has a value."""
    known = [i for i, v in enumerate(values) if v is not None]
    if not known:
        return None
    if len(known) == len(values):
        return [float(v) for v in values]
    xy = np.asarray(xy, float)[:, :2]
    t = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))]) if len(xy) > 1 else np.zeros(1)
    filled = np.interp(t, t[known], [float(values[i]) for i in known])
    return [float(values[i]) if values[i] is not None else float(filled[i]) for i in range(len(values))]


class StaleDem(ValueError):
    """The drawings of a file have no heights and the DEM they would take them from was built in another system of
    the chunk (an orientation since): its heights would land off the model. Rebuild the DEM first."""


def _dem_heights(chunk, xy) -> list:
    path = chunk.dem_path
    if path is None or not Path(path).exists():
        return [None] * len(xy)
    import rasterio

    with rasterio.open(path) as src:
        vals = [float(v[0]) for v in src.sample([(float(x), float(y)) for x, y in xy])]
        nodata = src.nodata
    return [None if (nodata is not None and v == nodata) or not np.isfinite(v) else v for v in vals]


def import_features(chunk, path, layer: str | None = None) -> dict:
    """The drawings of ``path`` (:func:`read`) added to ``chunk``: in ``layer``, else the file's ``layer`` column,
    else the name of the file; a feature without Z gets the heights of the DEM (:func:`fill_missing` along the drawing;
    skipped when no vertex has one); a new identity when the one it has is in the chunk already. Returns
    ``{"added", "dem_z" (of those added, how many took the DEM's heights), "skipped", "holes_dropped" (the holes of
    polygons left out: a free drawing has one ring)}``. :class:`StaleDem` (nothing added) when heights are wanted
    from a DEM to rebuild (:mod:`..core.stamps`)."""
    from ..core import stamps

    path = Path(path)
    records = list(read(path))
    wanting = any(not rec["has_z"] or any(p[2] is None for p in rec["xyz"]) for rec in records)
    if wanting and chunk.products.get("dem") and stamps.is_stale(chunk, "dem"):
        raise StaleDem(f"{path.name}: drawings without heights and the DEM is to be rebuilt (built in another "
                       "system of the chunk): rebuild it first")
    have = {r["id"] for r in free_drawings.features(chunk)}
    added = dem_z = skipped = holes = 0
    for rec in records:
        xyz = rec["xyz"]
        took_dem = False
        if not rec["has_z"] or any(p[2] is None for p in xyz):
            xy = [p[:2] for p in xyz]
            zs = [p[2] for p in xyz] if rec["has_z"] else [None] * len(xyz)
            from_dem = _dem_heights(chunk, xy)
            zs = fill_missing([z if z is not None else d for z, d in zip(zs, from_dem)], xy)
            if zs is None:
                skipped += 1
                continue
            xyz = [[p[0], p[1], z] for p, z in zip(xyz, zs)]
            took_dem = True
        wanted = rec.get("id")
        try:  # the identity of the file kept when it is new to the chunk
            new = free_drawings.add(chunk, rec["kind"], xyz, layer=layer or rec.get("layer") or path.stem,
                                    name=rec.get("name") or "", attributes=rec.get("attributes") or {},
                                    colour=rec.get("colour"), id=wanted if wanted and wanted not in have else None)
        except ValueError as exc:
            log.warning("Disegno non importato da %s: %s", path.name, exc)
            skipped += 1
            continue
        have.add(new["id"])
        added += 1
        dem_z += took_dem
        holes += int(rec.get("holes") or 0)
    if holes:
        log.warning("Disegni importati da %s: %d buchi di poligoni lasciati fuori (un disegno libero ha un solo "
                    "contorno)", path.name, holes)
    return {"added": added, "dem_z": dem_z, "skipped": skipped, "holes_dropped": holes}


__all__ = ["TABLES", "TITLES", "FREE_PREFIX", "FORMATS", "write_tables", "write_gpkg", "write_geojson", "write_dxf",
           "write_csv", "export", "read", "import_features", "fill_missing", "attribute_columns", "StaleDem"]
