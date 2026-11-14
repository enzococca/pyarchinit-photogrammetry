"""Measures taken on the 3D view (*Misura distanza*, *Misura area*, *Coordinate di un punto* on the model or a cloud):
their values from the picked vertices in the chunk's coordinates (:mod:`..archaeo.measures`, as the free drawings are
measured) and their words. A chunk without scale or reference is measured in model units, and the words say so."""
from __future__ import annotations

import numpy as np

from .i18n import decimal_comma, tr


def is_metric(chunk) -> bool:
    """True when the coordinates of ``chunk`` are metres: a projected or geographic system, data measured in metres
    (``meta["metric"]``: an imported model, a laser scan, a chunk declared in metres or scaled from a known distance
    on the 3D view), markers with references, scale bars, or a transform that scales the model (a reference or a
    scale applied, a model imported in metres keeps its own transform). A chunk only aligned (identity transform,
    local frame, no references) is in model units."""
    if chunk is None:
        return False
    if (getattr(chunk, "meta", None) or {}).get("metric"):  # measured in metres also in local coordinates: an
        return True  # imported model, a laser scan (as *Allinea chunk* and the report take it)
    return metric_reason(chunk) is not None


def metric_reason(chunk) -> str | None:
    """Why the coordinates of ``chunk`` are metres apart from ``meta["metric"]``, in words: its system, the markers
    with references, the scale bars or the scale of its transform; None when nothing else says so (the flag alone
    decides, as *Il modello è in metri* sets it)."""
    if chunk is None:
        return None
    from ..core import crs as crs_mod

    crs = getattr(chunk, "crs", None)
    if not crs_mod.is_local(crs):
        return tr("il chunk è nel sistema {0}", str(crs))
    if any(getattr(sb, "enabled", True) for sb in getattr(chunk, "scalebars", []) or []):
        return tr("il chunk ha barre di scala")
    for m in getattr(chunk, "markers", []) or []:
        ref = getattr(m, "reference", None)
        if ref is not None and ref.location is not None and ref.enabled:
            return tr("il chunk ha marker con coordinate")
    try:
        if abs(float(chunk.scale) - 1.0) > 1e-9:
            return tr("il chunk ha una scala ({0})", decimal_comma(f"{float(chunk.scale):.6g}"))
    except Exception:  # noqa: BLE001 - a transform that cannot be read: not known as metres
        return None
    return None


def _n(value: float, decimals: int = 3) -> str:
    return decimal_comma(f"{value:.{decimals}f}")


def _a(value: float) -> str:
    """An area: four decimals (a square metre to the square centimetre)."""
    return _n(value, 4)


def _units(metric: bool) -> tuple[str, str]:
    return ("m", "m²") if metric else (tr("u"), tr("u²"))


def values(kind: str, world, draped=None) -> dict:
    """The measures of the vertices ``world`` ((N, 3), chunk coordinates): a line (``kind`` "line") its 3D length
    (straight segments), plan length, height difference first to last and, with ``draped`` (the line with points of
    the surface between its vertices), its length along the surface; a polygon its plan area, its area in its best
    plane, 3D and plan perimeters, slope; a point its coordinates. ValueError when there are too few vertices."""
    from ..archaeo import measures

    p = np.asarray(world, np.float64).reshape(-1, 3)
    if kind == "line":
        out = {"length_3d": measures.length_3d(p), "length_plan": measures.length_plan(p),
               "dz": float(p[-1, 2] - p[0, 2])}
        if draped is not None and len(draped) > len(p):
            out["length_surface"] = measures.length_3d(np.asarray(draped, np.float64).reshape(-1, 3))
        return out
    if kind == "polygon":
        slope, _aspect = measures.slope_aspect_of(p)
        return {"area_plan": measures.area_plan(p), "area_plane": measures.area_3d(p),
                "perimeter_3d": measures.length_3d(p, closed=True),
                "perimeter_plan": measures.length_plan(p, closed=True), "slope": slope}
    if len(p) < 1:
        raise ValueError("no point")
    return {"x": float(p[0, 0]), "y": float(p[0, 1]), "z": float(p[0, 2])}


def text(kind: str, v: dict, metric: bool, crs: str | None = None) -> str:
    """The measures ``v`` (:func:`values`, with ``model_surface`` for a polygon measured on the model) in words."""
    m, m2 = _units(metric)
    if kind == "line":
        out = tr("Distanza sul modello: 3D {0} {3}, in pianta {1} {3}, dislivello {2} {3}", _n(v["length_3d"]),
                 _n(v["length_plan"]), decimal_comma(f"{v['dz']:+.3f}"), m)
        if v.get("length_surface") is not None:
            out += tr(", lungo la superficie {0} {1}", _n(v["length_surface"]), m)
    elif kind == "polygon":
        out = tr("Area sul modello: in pianta {0} {2}, nel piano medio {1} {2}", _a(v["area_plan"]),
                 _a(v["area_plane"]), m2)
        if v.get("model_surface") is not None:
            out += tr(", superficie del modello {0} {1}", _a(v["model_surface"]), m2)
        else:
            out += tr(" (senza modello: area nel piano medio dei vertici)")
        out += tr("; perimetro 3D {0} {2}, in pianta {1} {2}; pendenza {3}°", _n(v["perimeter_3d"]),
                  _n(v["perimeter_plan"]), m, decimal_comma(f"{v['slope']:.1f}"))
    else:
        from ..core import crs as crs_mod

        frame = tr("sistema locale") if crs_mod.is_local(crs) else str(crs)
        out = tr("Punto sul modello: X {0}  Y {1}  Z {2} {3} ({4})", _n(v["x"]), _n(v["y"]), _n(v["z"]), m, frame)
    if not metric:
        out += tr(" - in unità del modello, non in metri: il chunk non ha scala né riferimenti")
    return out


def live_text(chunk, kind: str, world) -> str:
    """The measure being taken (the vertices so far and the end of the rubber band) in words, "" when there is
    nothing to say yet."""
    p = np.asarray(world, np.float64).reshape(-1, 3)
    metric = is_metric(chunk)
    m, m2 = _units(metric)
    if kind == "line" and len(p) >= 2:
        v = values("line", p)
        out = tr("3D {0} {3}, in pianta {1} {3}, dislivello {2} {3}", _n(v["length_3d"]), _n(v["length_plan"]),
                 decimal_comma(f"{v['dz']:+.3f}"), m)
    elif kind == "polygon" and len(p) >= 3:
        from ..archaeo import measures

        try:
            out = tr("In pianta {0} {2}, perimetro 3D {1} {3}", _a(measures.area_plan(p)),
                     _n(measures.length_3d(p, closed=True)), m2, m)
        except ValueError:  # three vertices in a row so far
            return ""
    else:
        return ""
    return out if metric else out + tr(" (unità del modello)")


AROUND_MARGIN = 0.5  # the faces read around a polygon: its footprint widened by this share of its larger side


def model_around(source, xyz, margin: float = AROUND_MARGIN):
    """The part of the model of ``source`` (a chunk or a :class:`.free_drawing_dialog.ModelSource`) that the surface
    inside the polygon ``xyz`` (world) can be measured on, as a trimesh in world coordinates at full resolution: the
    faces whose projection (vertical, or along the normal of a steep polygon, as
    :func:`..archaeo.measures.model_area_inside` projects) meets the footprint widened by ``margin`` of its larger side
    on every side. The PLY is read through its memory map, the faces block by block: a model of tens of millions of
    faces is never held whole. None when the file is not a binary PLY of triangles (the caller reads it whole) or the
    polygon has no plane."""
    from ..archaeo import measures
    from ..io.mesh_ply import PlyMesh

    path = source.product_path("model") if source is not None else None
    if path is None or not path.exists():
        return None
    try:
        mesh = PlyMesh(path, check=0)
        p = np.asarray(xyz, np.float64).reshape(-1, 3)
        centre, n = measures.best_plane(p)
    except (ValueError, KeyError, OSError):
        return None
    if abs(n[2]) < measures.STEEP_COS:
        axes = np.stack(measures._plane_frame(n))
    else:
        axes = np.eye(3)[:2]
    foot = (p - centre) @ axes.T
    lo, hi = foot.min(axis=0), foot.max(axis=0)
    pad = margin * float(max(hi - lo)) + 1e-9
    lo, hi = lo - pad, hi + pad
    proj = np.empty((mesh.n_vertices, 2), np.float32)  # about the polygon's centre: single precision is plenty
    block = 4_000_000
    for s in range(0, mesh.n_vertices, block):
        w = source.to_world(mesh.xyz(slice(s, s + block)))
        proj[s:s + len(w)] = (w - centre) @ axes.T
    kept = []
    for faces in mesh.face_chunks():
        fx, fy = proj[faces, 0], proj[faces, 1]
        ok = ((fx.max(axis=1) >= lo[0]) & (fx.min(axis=1) <= hi[0]) & (fy.max(axis=1) >= lo[1])
              & (fy.min(axis=1) <= hi[1]))
        if ok.any():
            kept.append(faces[ok])
    del proj
    import trimesh

    if not kept:
        return trimesh.Trimesh(np.zeros((0, 3)), np.zeros((0, 3), np.int64), process=False)
    faces = np.concatenate(kept)
    used, inverse = np.unique(faces, return_inverse=True)
    vertices = source.to_world(mesh.xyz(used))
    return trimesh.Trimesh(vertices, inverse.reshape(-1, 3), process=False)
