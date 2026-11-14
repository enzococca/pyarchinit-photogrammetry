"""The drawings kept with an elevation or a section, in its own frame.

What is drawn on an elevation (or a section) belongs to it, not to the plan: the window keeps it in
``chunk.meta["drawings"]["frames"][<elevation id>]`` (since 1.7.0), in the ``(u, v)`` metres of the elevation
(``u`` along the surface, ``v`` up - the height when ``entry["v_is_height"]``)::

    {"units":  [{"wkt", "us", "area", "tipo", "sito", "attributes": {"ppg_id", ...}, "layer"}],
     "quote":  [{"u", "v", "x", "y", "quota_q", "ppg_id", ... the columns of pyarchinit_quote}],
     "points": [{"u", "v", "x", "y", "z_superficie", "label", ... the values read there}],
     "lines":  [{"id", "kind", "coords": [[u, v, z or None], ...], "attributes"}],
     "stones": {<elevation id>: [WKT]}}

The plan drawings stay at the top of ``chunk.meta["drawings"]``. A vertex ``(u, v)`` is the world point
``frame.point(u, v, d)`` of the plane (or cylinder) of the elevation, ``d`` the depth towards the viewer: ``0`` on the
plane (units, lines, stones), the surface for spot heights and measuring points (their ``x``, ``y`` and height are
that world point); :func:`uv_of` is the way back. The world is the system the elevation was made in
(``entry["crs"]``, the chunk's unless the survey's system changed since).

This module reads and changes those drawings outside the window - for plugins, the command line and the tests -
and moves them where the window does not: an elevation removed (:func:`take_frame`, :func:`give_frame`), the
drawings of an elevation given to another one (:func:`transfer`), the drawings of the projects made before 1.7.0
(:func:`migrate_legacy`), the 3D DXF (:func:`write_dxf_3d`), the 3D view (:func:`world_features`) and the edits made
in QGIS on the elevation unfolded (:func:`read_unfolded`, :func:`apply_unfolded`). No Qt here.
"""
from __future__ import annotations

import copy
import json
import logging
import uuid
from pathlib import Path

import numpy as np

log = logging.getLogger("pyarchinit_photogrammetry")

KINDS = ("units", "quote", "points", "lines", "stones")
LEGACY_STONES_KEY = "elevation.tif"  # the key of the stones drawn on any elevation up to 1.6 (the file's name)
MIGRATED = "migrated_from_plan"  # the mark of a record moved from the plan by migrate_legacy
SNAPSHOT_SUFFIX = ".ppg.json"  # beside the unfolded GeoPackage: what was written there (read_unfolded)
_NO_SNAPSHOT = {"units": {}, "quote": {}, "points": {}, "lines": {}, "stones": []}


# ---------------------------------------------------------------- the frame of an elevation
def entries(chunk) -> list[dict]:
    """The elevations and sections of ``chunk`` (their entries in ``chunk.meta["elevations"]``)."""
    return [e for e in chunk.meta.get("elevations") or [] if isinstance(e, dict) and e.get("id")]


def entry_of(chunk, eid: str) -> dict | None:
    """The entry of the elevation ``eid`` of ``chunk``, None when it has none."""
    return next((e for e in entries(chunk) if e["id"] == eid), None)


def frame_of(entry: dict):
    """The plane (or cylinder) of the elevation ``entry``."""
    from .elevation import frame_from_dict

    return frame_from_dict(entry["frame"])


def world_of_uv(entry: dict, uv, d=0.0) -> np.ndarray:
    """The world points ``(N, 3)`` of the positions ``uv`` ``(N, 2)`` of the elevation ``entry`` at the depth ``d``
    (a number or one per point; ``0``: on its plane)."""
    uv = np.asarray(uv, np.float64).reshape(-1, 2)
    d = np.broadcast_to(np.asarray(d, np.float64), (len(uv),))
    return frame_of(entry).point(uv[:, 0], uv[:, 1], d)


def uv_of(entry: dict, xyz) -> np.ndarray:
    """``(u, v, d)`` ``(N, 3)`` of world points ``xyz`` ``(N, 3)`` on the elevation ``entry``: the way back of
    :func:`world_of_uv`."""
    return frame_of(entry).coords(np.asarray(xyz, np.float64).reshape(-1, 3))


def surface_depths(chunk, entry: dict, uv) -> np.ndarray:
    """The depth of the surface of the elevation (its relief raster) at each of ``uv`` ``(N, 2)``; NaN where it has
    none (or the elevation has no relief)."""
    from .elevation import elevation_paths

    uv = np.asarray(uv, np.float64).reshape(-1, 2)
    out = np.full(len(uv), np.nan)
    _ortho, relief = elevation_paths(chunk, entry)
    if not len(uv) or not relief.exists():
        return out
    import rasterio

    with rasterio.open(relief) as src:
        for i, val in enumerate(src.sample([(float(u), float(v)) for u, v in uv])):
            x = float(val[0])
            if np.isfinite(x) and (src.nodata is None or x != src.nodata):
                out[i] = x
    return out


# ---------------------------------------------------------------- reading
def _frames(chunk, create: bool = False) -> dict:
    if create:
        return chunk.meta.setdefault("drawings", {}).setdefault("frames", {})
    return (chunk.meta.get("drawings") or {}).get("frames") or {}


def has_drawings(chunk, eid: str) -> bool:
    """Whether the elevation ``eid`` has drawings of its own."""
    data = _frames(chunk).get(eid) or {}
    return any(data.get(k) for k in KINDS)


def counts(chunk, eid: str) -> dict:
    """``{kind: how many}`` of the drawings of the elevation ``eid`` (:data:`KINDS`)."""
    data = _frames(chunk).get(eid) or {}
    out = {k: len([r for r in data.get(k) or [] if isinstance(r, dict)]) for k in ("units", "quote", "points", "lines")}
    out["stones"] = len((data.get("stones") or {}).get(eid) or [])
    return out


def drawings(chunk, eid: str, world: bool = False) -> dict:
    """The drawings of the elevation ``eid`` as plain records with shapely geometries - copies: changing them changes
    nothing in the project. ``{"units": [...], "quote": [...], "points": [...], "lines": [...], "stones": [...]}``:

    * a unit: ``id`` (its ``ppg_id``), ``us``, ``area``, ``tipo``, ``sito``, ``layer``, ``attributes``, ``geometry``
      (a (Multi)Polygon in ``(u, v)``);
    * a spot height, a measuring point: ``id``, ``u``, ``v``, ``geometry`` (Point ``(u, v)``), ``record`` (as kept);
    * a line: ``id``, ``kind``, ``attributes``, ``geometry`` (LineString ``(u, v)``);
    * a stone: ``n`` (1, 2, ...), ``geometry`` (Polygon ``(u, v)``).

    ``world``: each also with ``world``, the geometry with Z in the system of the elevation (units, lines and stones
    on its plane; spot heights and measuring points on the surface, where they were read). Empty when the elevation
    has none; ``KeyError`` when ``chunk`` has no elevation ``eid``."""
    from .elevation import _drawing_items, _on_plane

    entry = entry_of(chunk, eid)
    if entry is None:
        raise KeyError(f"no elevation {eid!r} in the chunk {getattr(chunk, 'label', '')!r}")
    items = _drawing_items(chunk, entry, copy.deepcopy(_frames(chunk).get(eid) or {}))
    place = _on_plane(entry) if world else None
    from shapely.geometry import Point

    out = {"units": [], "quote": [], "points": [], "lines": [], "stones": []}
    for geom, rec in items["units"]:
        attrs = dict(rec.get("attributes") or {})
        row = {"id": attrs.get("ppg_id"), "us": rec.get("us"), "area": rec.get("area"), "tipo": rec.get("tipo"),
               "sito": rec.get("sito"), "layer": rec.get("layer"), "attributes": attrs, "geometry": geom}
        if place:
            row["world"] = place(geom)
        out["units"].append(row)
    for key, height in (("quote", "quota_q"), ("points", "z_superficie")):
        for p, rec in items[key]:
            row = {"id": rec.get("ppg_id") or rec.get("label"), "u": p.x, "v": p.y, "geometry": p, "record": rec}
            if place:
                row["world"] = Point(float(rec["x"]), float(rec["y"]), float(rec[height])) \
                    if rec.get(height) not in (None, "") and "x" in rec and "y" in rec else place(p)
            out[key].append(row)
    for geom, rec in items["lines"]:
        row = {"id": rec.get("id"), "kind": rec.get("kind"), "attributes": dict(rec.get("attributes") or {}),
               "geometry": geom}
        if place:
            row["world"] = place(geom)
        out["lines"].append(row)
    for i, geom in enumerate(items["stones"]):
        row = {"n": i + 1, "geometry": geom}
        if place:
            row["world"] = place(geom)
        out["stones"].append(row)
    return out


# ---------------------------------------------------------------- adding (plugins, command line)
def add_units(chunk, eid: str, units) -> int:
    """``units`` (:class:`..archaeo.stratigraphy.UnitPolygon`, their geometry in ``(u, v)``) into the drawings of
    the elevation ``eid`` of ``chunk``, each with a ``ppg_id``. Not for the elevation the window shows (the window
    keeps those: ``Context.add_units`` goes there). Returns how many."""
    if entry_of(chunk, eid) is None:
        raise KeyError(f"no elevation {eid!r} in the chunk {getattr(chunk, 'label', '')!r}")
    data = _frames(chunk, create=True).setdefault(eid, {"units": [], "quote": []})
    rows = data.setdefault("units", [])
    data.setdefault("quote", [])
    n = 0
    for u in units:
        rec = u.to_dict()
        rec["attributes"].setdefault("ppg_id", uuid.uuid4().hex)
        rec["layer"] = (u.attributes or {}).get("_layer")
        rows.append(rec)
        n += 1
    return n


# ---------------------------------------------------------------- an elevation removed, and back
def take_frame(chunk, eid: str) -> dict | None:
    """The drawings of the elevation ``eid`` out of ``chunk`` (its frame removed), returned for :func:`give_frame`;
    None when it had none."""
    drawings_ = chunk.meta.get("drawings")
    frames = (drawings_ or {}).get("frames")
    if not isinstance(frames, dict) or eid not in frames:
        return None
    data = frames.pop(eid)
    if not frames:
        drawings_.pop("frames", None)
        if not drawings_:
            chunk.meta.pop("drawings", None)
    return data


def give_frame(chunk, eid: str, data: dict | None) -> None:
    """The drawings ``data`` (:func:`take_frame`) back to the elevation ``eid``: they replace what it has."""
    if data is None:
        return
    _frames(chunk, create=True)[eid] = data


# ---------------------------------------------------------------- the drawings of one elevation on another
def _carry(source: dict, target: dict, uv, depths) -> np.ndarray:
    """``(u, v)`` of ``source`` to ``(u, v)`` of ``target`` through their world point at ``depths``."""
    uv = np.asarray(uv, np.float64).reshape(-1, 2)
    if not len(uv):
        return uv
    return uv_of(target, world_of_uv(source, uv, depths))[:, :2]


def transfer(chunk, source: str, target: str, copy_: bool = True, surface: bool = True) -> dict:
    """The drawings of the elevation ``source`` given to the elevation ``target`` of the same chunk (an elevation made
    again on another plane, a section moved): each vertex goes through its world point - on the surface of
    ``source`` (its relief) when ``surface`` and the relief has a value there, else on its plane - and lands at the
    ``(u, v)`` of ``target`` there. Spot heights and measuring points keep their world point (``x``, ``y``, height):
    their ``(u, v)`` are those of ``target``. They join the drawings ``target`` has, with new identities when copied
    (``copy_``), else they leave ``source``. Both elevations must be in the same system. Returns
    ``{kind: how many}``."""
    from shapely import transform as shapely_transform
    from shapely import wkt

    from .elevation import same_crs

    a, b = entry_of(chunk, source), entry_of(chunk, target)
    if a is None or b is None:
        raise KeyError("both elevations must be in the chunk")
    if source == target:
        raise ValueError("the same elevation")
    if not same_crs(a.get("crs") or chunk.crs, b.get("crs") or chunk.crs):
        raise ValueError("the two elevations are in different systems: make them again in the same one")
    data = copy.deepcopy(_frames(chunk).get(source) or {})
    if not any(data.get(k) for k in KINDS):
        return {k: 0 for k in KINDS}

    def move(uv):
        uv = np.asarray(uv, np.float64).reshape(-1, 2)
        d = surface_depths(chunk, a, uv) if surface else np.full(len(uv), np.nan)
        return _carry(a, b, uv, np.where(np.isfinite(d), d, 0.0))

    done = {k: 0 for k in KINDS}
    out = {"units": [], "quote": [], "points": [], "lines": [], "stones": []}
    for rec in data.get("units") or []:
        try:
            geom = shapely_transform(wkt.loads(rec["wkt"]), move)
        except Exception as exc:  # noqa: BLE001 - a broken record stays where it is, said
            log.warning("Unità non spostata sull'altro prospetto: %s", exc)
            continue
        rec = {**rec, "wkt": geom.wkt, "attributes": dict(rec.get("attributes") or {})}
        if copy_:
            rec["attributes"]["ppg_id"] = uuid.uuid4().hex
        out["units"].append(rec)
    for key, height in (("quote", "quota_q"), ("points", "z_superficie")):
        for rec in data.get(key) or []:
            if not isinstance(rec, dict):
                continue
            rec = dict(rec)
            if "u" not in rec or "v" not in rec:
                continue
            if "x" in rec and "y" in rec and rec.get(height) not in (None, ""):
                world = np.array([[float(rec["x"]), float(rec["y"]), float(rec[height])]])
                rec["u"], rec["v"] = (float(c) for c in uv_of(b, world)[0, :2])
            else:
                rec["u"], rec["v"] = (float(c) for c in move([[rec["u"], rec["v"]]])[0])
            if copy_ and key == "quote":
                rec["ppg_id"] = uuid.uuid4().hex
            out[key].append(rec)
    for rec in data.get("lines") or []:
        rec = copy.deepcopy(rec)
        coords = rec.get("coords") or []
        moved = move([[c[0], c[1]] for c in coords])
        rec["coords"] = [[float(m[0]), float(m[1])] + list(c[2:3]) for m, c in zip(moved, coords)]
        if copy_:
            rec["id"] = uuid.uuid4().hex
        out["lines"].append(rec)
    for text in (data.get("stones") or {}).get(source) or []:
        try:
            out["stones"].append(shapely_transform(wkt.loads(text), move).wkt)
        except Exception as exc:  # noqa: BLE001
            log.warning("Pietra non spostata sull'altro prospetto: %s", exc)
    frames = _frames(chunk, create=True)
    dest = frames.setdefault(target, {"units": [], "quote": []})
    for key in ("units", "quote", "points", "lines"):
        if out[key]:
            dest.setdefault(key, []).extend(out[key])
        done[key] = len(out[key])
    if out["stones"]:
        dest.setdefault("stones", {}).setdefault(target, []).extend(out["stones"])
    done["stones"] = len(out["stones"])
    dest.setdefault("units", [])
    dest.setdefault("quote", [])
    if not copy_:
        take_frame(chunk, source)
    return done


# ---------------------------------------------------------------- projects made before 1.7.0
def _plan_extent(chunk):
    """``(xmin, ymin, xmax, ymax)`` of where the plan of ``chunk`` is (its photos, markers and products, widened by a
    quarter and 10 m at least), None when nothing says it."""
    xy = []
    for cam in chunk.cameras:
        if cam.transform is not None:
            try:
                xy.append(chunk.to_world(np.asarray(cam.transform, float)[:3, 3].reshape(1, 3))[0, :2])
            except Exception:  # noqa: BLE001 - a chunk without a transform: the photos' references below
                pass
        loc = getattr(cam.reference, "location", None)
        if loc is not None and len(loc) >= 2:
            xy.append(np.asarray(loc[:2], float))
    for m in chunk.markers:
        pos = getattr(m, "position", None)
        if pos is not None:
            try:
                xy.append(chunk.to_world(np.asarray(pos, float).reshape(1, 3))[0, :2])
            except Exception:  # noqa: BLE001
                pass
    for path in (chunk.orthomosaic_path, chunk.dem_path):
        if path is not None and Path(path).exists():
            try:
                import rasterio

                with rasterio.open(path) as src:
                    b = src.bounds
                xy += [np.array([b.left, b.bottom]), np.array([b.right, b.top])]
            except Exception:  # noqa: BLE001
                pass
    xy = [p for p in xy if np.all(np.isfinite(p))]
    if not xy:
        return None
    a = np.vstack(xy)
    lo, hi = a.min(axis=0), a.max(axis=0)
    pad = max(10.0, 0.25 * float(np.hypot(*(hi - lo))))
    return (lo[0] - pad, lo[1] - pad, hi[0] + pad, hi[1] + pad)


def _within(b, box, margin: float) -> bool:
    return b[0] >= box[0] - margin and b[1] >= box[1] - margin and b[2] <= box[2] + margin and b[3] <= box[3] + margin


def _meets(b, box) -> bool:
    return not (b[2] < box[0] or b[0] > box[2] or b[3] < box[1] or b[1] > box[3])


def migrate_legacy(chunk) -> dict:
    """The drawings made on an elevation up to 1.6 - kept then among the plan's, in the elevation's ``(u, v)`` as if
    they were plan coordinates, and the stones under the key ``"elevation.tif"`` that every elevation shared - moved
    into the frame of their elevation, in ``chunk.meta`` only (the file is written when the project is saved). A
    drawing moves when it lies outside the plan (its photos, markers and products, :func:`_plan_extent`) and inside
    the ``(u, v)`` extent of exactly one elevation; one that fits several stays where it was, and so does everything
    when the plan has no extent to tell (said in the log): nothing is ever dropped. A spot height or a measuring
    point gets its world point on the surface of the elevation (its relief; on the plane where it has none), its
    ``x``, ``y`` becoming ``u``, ``v``; the values it had are kept under :data:`MIGRATED`. Returns
    ``{"units", "quote", "points", "stones", "ambiguous"}`` (how many moved, how many could not be given to one
    elevation)."""
    from shapely import wkt

    from .elevation import bounds_of

    report = {"units": 0, "quote": 0, "points": 0, "stones": 0, "ambiguous": 0}
    elevs = [e for e in entries(chunk) if e.get("frame")]
    data = chunk.meta.get("drawings") or {}
    stones = chunk.meta.get("stones") or {}
    legacy_stones = stones.get(LEGACY_STONES_KEY) if isinstance(stones, dict) else None
    if not elevs or not (data.get("units") or data.get("quote") or data.get("points") or legacy_stones):
        return report
    boxes = {}
    for e in elevs:
        try:
            boxes[e["id"]] = tuple(float(x) for x in bounds_of(chunk, e))
        except Exception as exc:  # noqa: BLE001 - an elevation without its files: no drawing given to it
            log.info("Prospetto %s senza estensione: nessun disegno vecchio gli viene dato (%s)", e.get("name"), exc)
    if not boxes:
        return report
    plan = _plan_extent(chunk)
    if plan is None:
        log.info("Disegni fatti sui prospetti prima della 1.7.0: la pianta non ha un'estensione, restano dove sono")
        return report

    def owner(b):
        if _meets(b, plan):
            return None
        fits = [eid for eid, box in boxes.items()
                if _within(b, box, max(0.05, 0.02 * max(box[2] - box[0], box[3] - box[1])))]
        if len(fits) == 1:
            return fits[0]
        if len(fits) > 1:
            report["ambiguous"] += 1
        return None

    by_entry = {e["id"]: e for e in elevs}
    frames = None
    kept_units, kept = [], {"quote": [], "points": []}
    for rec in data.get("units") or []:
        eid = None
        try:
            eid = owner(wkt.loads(rec["wkt"]).bounds)
        except Exception:  # noqa: BLE001 - a broken record stays where it was
            pass
        if eid is None:
            kept_units.append(rec)
            continue
        frames = _frames(chunk, create=True) if frames is None else frames
        dest = frames.setdefault(eid, {"units": [], "quote": []})
        dest.setdefault("units", []).append({**rec, MIGRATED: True})
        report["units"] += 1
    for key, height in (("quote", "quota_q"), ("points", "z_superficie")):
        for rec in data.get(key) or []:
            eid = None
            if isinstance(rec, dict) and "x" in rec and "y" in rec and "u" not in rec:
                try:
                    x, y = float(rec["x"]), float(rec["y"])
                    eid = owner((x, y, x, y))
                except (TypeError, ValueError):
                    pass
            if eid is None:
                kept[key].append(rec)
                continue
            entry = by_entry[eid]
            u, v = float(rec["x"]), float(rec["y"])
            d = surface_depths(chunk, entry, [[u, v]])[0]
            world = world_of_uv(entry, [[u, v]], d if np.isfinite(d) else 0.0)[0]
            moved = {**rec, "u": u, "v": v, "x": float(world[0]), "y": float(world[1]),
                     height: round(float(world[2]), 4),
                     MIGRATED: {k: rec.get(k) for k in ("x", "y", height) if k in rec}}
            frames = _frames(chunk, create=True) if frames is None else frames
            dest = frames.setdefault(eid, {"units": [], "quote": []})
            dest.setdefault(key, []).append(moved)
            report[key] += 1
    if report["units"] or report["quote"] or report["points"]:
        data["units"], data["quote"] = kept_units, kept["quote"]
        if "points" in data or kept["points"]:
            if kept["points"]:
                data["points"] = kept["points"]
            else:
                data.pop("points", None)
    if legacy_stones:
        left = []
        for text in legacy_stones:
            eid = None
            try:
                eid = owner(wkt.loads(text).bounds)
            except Exception:  # noqa: BLE001
                pass
            if eid is None:
                left.append(text)
                continue
            frames = _frames(chunk, create=True) if frames is None else frames
            dest = frames.setdefault(eid, {"units": [], "quote": []})
            dest.setdefault("stones", {}).setdefault(eid, []).append(text)
            report["stones"] += 1
        if report["stones"]:
            if left:
                stones[LEGACY_STONES_KEY] = left
            else:
                stones.pop(LEGACY_STONES_KEY, None)
                if not stones:
                    chunk.meta.pop("stones", None)
    moved = report["units"] + report["quote"] + report["points"] + report["stones"]
    if moved:
        log.info("Disegni fatti sui prospetti prima della 1.7.0 portati sui loro prospetti (%s): %d unità, %d quote, "
                 "%d punti, %d pietre", getattr(chunk, "label", ""), report["units"], report["quote"], report["points"],
                 report["stones"])
    if report["ambiguous"]:
        log.warning("%d disegni fatti sui prospetti prima della 1.7.0 cadono su più prospetti: restano dove erano",
                    report["ambiguous"])
    return report


# ---------------------------------------------------------------- 3D: DXF and the 3D view
def _rings(geom):
    """The rings of the polygons of ``geom`` with their Z (``(N, 3)``)."""
    for part in getattr(geom, "geoms", [geom]):
        if part.geom_type == "Polygon":
            yield np.asarray(part.exterior.coords)
            for hole in part.interiors:
                yield np.asarray(hole.coords)


def write_dxf_3d(chunk, entry: dict, path) -> Path:
    """The drawings of the elevation ``entry`` placed on the survey in 3D (:func:`..engine.elevation.world_tables`), as
    a DXF in the coordinates of the system of the elevation (the chunk's): units, stones, lines and the profile of a
    section as 3D polylines on the plane, spot heights and measuring points as points on the surface with their
    labels; layers ``US``, ``USM``, ``QUOTE``, ``PUNTI``, ``PIETRE``, ``LINEE``, ``PROFILO``."""
    import ezdxf

    from .elevation import world_tables

    path = Path(path)
    tables = world_tables(chunk, entry)
    dxf = ezdxf.new("R2010")
    dxf.units = ezdxf.units.M
    msp = dxf.modelspace()
    for name, colour in (("US", 3), ("USM", 1), ("QUOTE", 2), ("PIETRE", 7), ("PUNTI", 6), ("PROFILO", 1),
                         ("LINEE", 30)):
        dxf.layers.add(name, color=colour)
    height = max(0.02, 4 * float(entry.get("resolution") or 0.005))

    def poly(coords, layer, closed=False):
        pts = [tuple(float(c) for c in q[:3]) for q in coords]
        if closed and len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        if len(pts) >= 2:
            msp.add_polyline3d(pts, close=closed, dxfattribs={"layer": layer})

    for geom, values in tables["unita"][2]:
        layer = "USM" if str(values.get("tipo") or "").upper() == "USM" else "US"
        for ring in _rings(geom):
            poly(ring, layer, closed=True)
    for p, values in tables["quote"][2]:
        msp.add_point((p.x, p.y, p.z), dxfattribs={"layer": "QUOTE"})
        z = values.get("quota_q")
        msp.add_text(f"{float(z):.2f}" if z not in (None, "") else "?", height=height,
                     dxfattribs={"layer": "QUOTE"}).set_placement((p.x, p.y, p.z))
    for p, values in tables["punti"][2]:
        msp.add_point((p.x, p.y, p.z), dxfattribs={"layer": "PUNTI"})
    for geom, _values in tables["pietre"][2]:
        for ring in _rings(geom):
            poly(ring, "PIETRE", closed=True)
    for geom, _values in tables["linee"][2]:
        poly(geom.coords, "LINEE")
    for geom, _values in (tables.get("profilo") or ("", {}, []))[2]:
        poly(geom.coords, "PROFILO")
    dxf.saveas(path)
    return path


def world_features(chunk, eids=None, data: dict | None = None) -> list[dict]:
    """The drawings of the elevations of ``chunk`` (``eids``, all by default) in 3D, as the 3D view draws the free
    drawings: records ``id`` (``"elevation:<id>:<kind>:<n>"``), ``kind`` (line, polygon, point), ``xyz`` (world, in
    the chunk's system), ``elevation`` (its id). ``data``: ``{id: drawings}`` read in place of those the chunk keeps
    (the elevation the window shows). Elevations made in another system than the chunk's now are left out (said in
    the log)."""
    from .elevation import same_crs, world_tables

    out = []
    for entry in entries(chunk):
        eid = entry["id"]
        mine = (data or {}).get(eid)
        if eids is not None and eid not in eids:
            continue
        if not (any(mine.get(k) for k in KINDS) if mine is not None else has_drawings(chunk, eid)):
            continue
        if not same_crs(entry.get("crs") or chunk.crs, chunk.crs):
            log.info("Disegni del prospetto %s non mostrati in 3D: è in un altro sistema", entry.get("name"))
            continue
        try:
            tables = world_tables(chunk, entry, mine)
        except Exception as exc:  # noqa: BLE001 - a broken elevation: the others are shown
            log.warning("Disegni del prospetto %s non mostrati in 3D: %s", entry.get("name"), exc)
            continue
        n = 0

        def add(kind, coords):
            nonlocal n
            xyz = np.asarray([tuple(c[:3]) for c in coords], np.float64).reshape(-1, 3)
            if kind == "polygon" and len(xyz) > 1 and np.allclose(xyz[0], xyz[-1]):
                xyz = xyz[:-1]
            out.append({"id": f"elevation:{eid}:{kind}:{n}", "kind": kind, "xyz": xyz, "elevation": eid})
            n += 1

        for geom, _v in tables["unita"][2]:
            for ring in _rings(geom):
                add("polygon", ring)
        for geom, _v in tables["pietre"][2]:
            for ring in _rings(geom):
                add("polygon", ring)
        for geom, _v in tables["linee"][2]:
            add("line", geom.coords)
        for name in ("quote", "punti"):
            for p, _v in tables[name][2]:
                add("point", [(p.x, p.y, p.z)])
    return out


# ---------------------------------------------------------------- the elevation unfolded in QGIS, read back
def _key(geom) -> str:
    from shapely import wkt

    return wkt.dumps(geom, rounding_precision=7, trim=True)


def snapshot_path(gpkg_path) -> Path:
    p = Path(gpkg_path)
    return p.with_name(p.name + SNAPSHOT_SUFFIX)


def _read_rows(path) -> dict:
    """The rows of the unfolded GeoPackage as ``{kind: {key: (geometry, values)}}`` (stones: a list)."""
    from ..io import gpkg

    out = {"units": {}, "quote": {}, "points": {}, "lines": {}, "stones": []}
    for table, kind in (("unita", "units"), ("quote", "quote"), ("punti", "points"), ("linee", "lines")):
        for i, (geom, values) in enumerate(gpkg.read_table(path, table)):
            if geom is None or geom.is_empty:
                continue
            ident = values.get("ppg_id") if kind != "points" else values.get("label")
            key = str(ident) if ident not in (None, "") else f"new:{i}"
            out[kind][key] = (geom, {k: v for k, v in values.items() if k not in ("fid", "geom_world")})
    for geom, _values in gpkg.read_table(path, "pietre"):
        if geom is not None and not geom.is_empty:
            out["stones"].append(geom)
    return out


def write_snapshot(gpkg_path) -> Path:
    """What the unfolded GeoPackage ``gpkg_path`` holds now (as :func:`..engine.elevation.export_drawings` wrote it),
    beside it: :func:`read_unfolded` tells from it what was changed there since."""
    rows = _read_rows(gpkg_path)
    snap = {k: {key: [_key(g), json.loads(json.dumps(v, default=str))] for key, (g, v) in rows[k].items()}
            for k in ("units", "quote", "points", "lines")}
    snap["stones"] = sorted(_key(g) for g in rows["stones"])
    path = snapshot_path(gpkg_path)
    path.write_text(json.dumps(snap), encoding="utf-8")
    return path


def read_unfolded(gpkg_path) -> dict:
    """What was changed in the unfolded GeoPackage of an elevation (``<stem>_disegni.gpkg``, edited in QGIS) since it
    was written (its snapshot, :func:`write_snapshot`): ``{kind: {"changed": {key: (geometry, values)}, "added":
    [(geometry, values)], "deleted": [key]}}`` for ``units``, ``quote``, ``points`` (key: the ``label``) and ``lines``,
    and ``stones``: ``{"added": [geometry], "deleted": [WKT key]}``. Without a snapshot everything there counts as
    added. Geometries in ``(u, v)``."""
    snap_path = snapshot_path(gpkg_path)
    snap = json.loads(snap_path.read_text(encoding="utf-8")) if snap_path.exists() else _NO_SNAPSHOT
    rows = _read_rows(gpkg_path)
    out = {}
    for kind in ("units", "quote", "points", "lines"):
        before = snap.get(kind) or {}
        now = rows[kind]
        changed, added = {}, []
        for key, (geom, values) in now.items():
            old = before.get(key)
            if old is None:
                added.append((geom, values))
            elif old[0] != _key(geom) or json.dumps(old[1], sort_keys=True, default=str) != json.dumps(
                    json.loads(json.dumps(values, default=str)), sort_keys=True):
                changed[key] = (geom, values)
        out[kind] = {"changed": changed, "added": added, "deleted": [k for k in before if k not in now]}
    old = list(snap.get("stones") or [])
    now_keys = [_key(g) for g in rows["stones"]]
    left = list(old)
    added = []
    for g, k in zip(rows["stones"], now_keys):
        if k in left:
            left.remove(k)
        else:
            added.append(g)
    out["stones"] = {"added": added, "deleted": left}
    return out


def _empty_changes(changes: dict) -> bool:
    return not any(v.get("changed") or v.get("added") or v.get("deleted") for v in changes.values())


def _single(geom):
    """A multipolygon of one part (as the GeoPackage gives every unit back) as that polygon."""
    if geom.geom_type == "MultiPolygon" and len(geom.geoms) == 1:
        return geom.geoms[0]
    return geom


UNIT_FIELDS = ("us", "area", "tipo", "sito")
QUOTE_FIELDS = ("quota_q", "us_q", "area_q", "sito_q", "unita_misu_q")


def apply_unfolded(chunk, eid: str, gpkg_path, changes: dict | None = None) -> dict:
    """The edits made in QGIS on the unfolded GeoPackage of the elevation ``eid`` (:func:`read_unfolded`) into its
    drawings in ``chunk.meta``: a unit or a line changed there takes its new outline and fields, one added there is a
    new one (a ``ppg_id`` of its own), one deleted there goes; a spot height or a measuring point moved there gets its
    world point on the surface again (a spot height its ``quota_q`` from it, unless only its fields changed); stones
    added and deleted likewise. What was changed here meanwhile and not there stays. The snapshot is written again,
    so the same edits are not read twice. Returns ``{kind: how many changed, added or deleted}``."""
    entry = entry_of(chunk, eid)
    if entry is None:
        raise KeyError(f"no elevation {eid!r} in the chunk")
    changes = read_unfolded(gpkg_path) if changes is None else changes
    done = {k: 0 for k in KINDS}
    if _empty_changes(changes):
        return done
    data = _frames(chunk, create=True).setdefault(eid, {"units": [], "quote": []})
    data.setdefault("units", [])
    data.setdefault("quote", [])

    # units, by ppg_id
    ch_u = changes.get("units") or {}
    units = []
    for rec in data["units"]:
        pid = str((rec.get("attributes") or {}).get("ppg_id"))
        if pid in (ch_u.get("deleted") or []):
            done["units"] += 1
            continue
        if pid in (ch_u.get("changed") or {}):
            geom, values = ch_u["changed"][pid]
            rec = {**rec, "wkt": _single(geom).wkt, **{k: values.get(k) for k in UNIT_FIELDS if k in values}}
            done["units"] += 1
        units.append(rec)
    for geom, values in ch_u.get("added") or []:
        if geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        geom = _single(geom)
        units.append({"wkt": geom.wkt, "us": values.get("us"), "area": values.get("area"),
                      "tipo": values.get("tipo") or "US", "sito": values.get("sito"),
                      "attributes": {"ppg_id": uuid.uuid4().hex}, "layer": None})
        done["units"] += 1
    data["units"] = units

    # spot heights (by ppg_id) and measuring points (by label)
    for kind, ident, height, fields in (("quote", "ppg_id", "quota_q", QUOTE_FIELDS),
                                        ("points", "label", "z_superficie", ("label",))):
        part = changes.get(kind) or {}
        if not (part.get("changed") or part.get("added") or part.get("deleted")):
            continue
        rows = []

        def placed(rec, p, fields_only):
            if fields_only:
                return rec
            u, v = float(p.x), float(p.y)
            d = surface_depths(chunk, entry, [[u, v]])[0]
            world = world_of_uv(entry, [[u, v]], d if np.isfinite(d) else 0.0)[0]
            rec = {**rec, "u": u, "v": v, "x": float(world[0]), "y": float(world[1])}
            rec[height] = round(float(world[2]), 3 if kind == "quote" else 4)
            return rec

        for rec in data.get(kind) or []:
            key = str(rec.get(ident))
            if key in (part.get("deleted") or []):
                done[kind] += 1
                continue
            if key in (part.get("changed") or {}):
                p, values = part["changed"][key]
                moved = abs(float(p.x) - float(rec.get("u", p.x))) > 1e-9 or \
                    abs(float(p.y) - float(rec.get("v", p.y))) > 1e-9
                rec = {**rec, **{k: values.get(k) for k in fields if k in values and k != height}}
                if not moved and height in fields and values.get(height) is not None:
                    rec[height] = values[height]
                rec = placed(rec, p, not moved)
                done[kind] += 1
            rows.append(rec)
        for p, values in part.get("added") or []:
            if p.geom_type != "Point":
                continue
            rec = {k: values.get(k) for k in fields if values.get(k) is not None and k != height}
            if kind == "quote":
                rec["ppg_id"] = uuid.uuid4().hex
            elif not rec.get("label"):
                rec["label"] = f"P{len(rows) + 1}"
            rows.append(placed(rec, p, False))
            done[kind] += 1
        if rows or kind == "quote":
            data[kind] = rows
        else:
            data.pop(kind, None)

    # lines, by id
    part = changes.get("lines") or {}
    if part.get("changed") or part.get("added") or part.get("deleted"):
        from ..archaeo import breaklines

        lines = []
        for rec in data.get("lines") or []:
            key = str(rec.get("id"))
            if key in (part.get("deleted") or []):
                done["lines"] += 1
                continue
            if key in (part.get("changed") or {}):
                geom, values = part["changed"][key]
                rec = {**rec, "coords": [[float(c[0]), float(c[1]), None] for c in geom.coords]}
                if values.get("kind") in breaklines.KINDS:
                    rec["kind"] = values["kind"]
                done["lines"] += 1
            lines.append(rec)
        for geom, values in part.get("added") or []:
            if geom.geom_type != "LineString":
                continue
            kind = values.get("kind") if values.get("kind") in breaklines.KINDS else "breakline"
            lines.append(breaklines.new_record(kind, [(c[0], c[1]) for c in geom.coords]))
            done["lines"] += 1
        if lines:
            data["lines"] = lines
        else:
            data.pop("lines", None)

    # stones, by outline
    part = changes.get("stones") or {}
    if part.get("added") or part.get("deleted"):
        from shapely import wkt

        mine = list((data.get("stones") or {}).get(eid) or [])
        gone = list(part.get("deleted") or [])
        kept = []
        for text in mine:
            try:
                k = _key(wkt.loads(text))
            except Exception:  # noqa: BLE001
                kept.append(text)
                continue
            if k in gone:
                gone.remove(k)
                done["stones"] += 1
                continue
            kept.append(text)
        for g in part.get("added") or []:
            if g.geom_type == "Polygon":
                kept.append(g.wkt)
                done["stones"] += 1
        stones = dict(data.get("stones") or {})
        if kept:
            stones[eid] = kept
        else:
            stones.pop(eid, None)
        if stones:
            data["stones"] = stones
        else:
            data.pop("stones", None)
    write_snapshot(gpkg_path)
    return done


__all__ = ["KINDS", "LEGACY_STONES_KEY", "MIGRATED", "entries", "entry_of", "frame_of", "world_of_uv", "uv_of",
           "surface_depths", "has_drawings", "counts", "drawings", "add_units", "take_frame", "give_frame",
           "transfer", "migrate_legacy", "write_dxf_3d", "world_features", "snapshot_path", "write_snapshot",
           "read_unfolded", "apply_unfolded"]
