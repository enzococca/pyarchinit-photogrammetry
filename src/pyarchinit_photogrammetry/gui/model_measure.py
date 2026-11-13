"""Measures taken on the 3D view (*Misura distanza*, *Misura area*, *Coordinate di un punto* on the model or a cloud):
their values from the picked vertices in the chunk's coordinates (:mod:`..archaeo.measures`, as the free drawings are
measured) and their words. A chunk without scale or reference is measured in model units, and the words say so."""
from __future__ import annotations

import numpy as np

from .i18n import decimal_comma, tr


def is_metric(chunk) -> bool:
    """True when the coordinates of ``chunk`` are metres: a projected or geographic system, markers with references,
    scale bars, or a transform that scales the model (a reference or a scale applied, a model imported in metres
    keeps its own transform). A chunk only aligned (identity transform, local frame, no references) is in model
    units."""
    if chunk is None:
        return False
    from ..core import crs as crs_mod

    if not crs_mod.is_local(getattr(chunk, "crs", None)):
        return True
    if any(getattr(sb, "enabled", True) for sb in getattr(chunk, "scalebars", []) or []):
        return True
    for m in getattr(chunk, "markers", []) or []:
        ref = getattr(m, "reference", None)
        if ref is not None and ref.location is not None and ref.enabled:
            return True
    try:
        return abs(float(chunk.scale) - 1.0) > 1e-9
    except Exception:  # noqa: BLE001 - a transform that cannot be read: not known as metres
        return False


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
