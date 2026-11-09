"""A drawing classified by its orientation along an axis: the lines of a plan or of an elevation sorted into lines
*along* the axis, lines *across* it, *pairs* of parallel lines (the two sides of one element: a frame, a plank) and
the rest. The program never names the elements: the plugins give the groups their meaning (the frames and seams of a
hull, the beds and joints of a wall) after a check plate.

The input is 2-D polylines in one plane (:func:`lines_from_layer`: world x, y of the free drawings of a layer;
:func:`lines_from_elevation`: the ``(u, v)`` drawings of an elevation; :func:`lines_from_file`: a GeoPackage, GeoJSON
or DXF). Each polyline is simplified (Douglas-Peucker, ``simplify``) and cut into its straight segments: a closed
polygon gives its edges, a curve of many vertices its straight pieces. A segment is *along* when its angle from the
axis is within ``along_deg``, *across* when within ``across_deg`` of 90 degrees, else *other*. Pieces of one line
(angle within ``join_deg``, lateral offset within ``join_offset``, gap up to ``join_gap``) are joined. Two parallel
lines (within ``pair_deg``) ``pair_min``-``pair_max`` apart whose extents overlap by ``pair_overlap`` of the shorter
are a pair when each is the other's nearest line and the next line of the same group beside each is at least
:data:`PAIR_ISOLATION` times farther (evenly spaced lines - the beds of a wall - are not pairs). The across lines left
are grouped by their position along the axis (bins of ``across_bin``) and side: the pieces of one frame drawn broken
(end to end, gaps up to ``join_gap``) become one element.

No Qt here. Since 1.7.7."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("pyarchinit_photogrammetry")

DEFAULTS: dict = {
    "simplify": 0.01,  # m, Douglas-Peucker tolerance
    "min_length": 0.05,  # m, shorter segments left out
    "along_deg": 15.0,  # |angle| <= this: along the axis
    "across_deg": 15.0,  # |angle - 90| <= this: across the axis
    "join_deg": 5.0,  # pieces of one line: angle between them
    "join_offset": 0.02,  # m, lateral offset between them
    "join_gap": 0.10,  # m, gap between them
    "across_bin": 0.10,  # m, across lines grouped by position along the axis
    "pair_deg": 5.0,  # pairs: parallel within this
    "pair_min": 0.02,  # m, distance from
    "pair_max": 0.50,  # m, distance to
    "pair_overlap": 0.5,  # overlap of the extents, share of the shorter
}
GROUPS = ("along", "across", "pair_along", "pair_across", "other")
GROUP_LABELS = {  # (Italian, English)
    "along": ("lungo l'asse", "along the axis"),
    "across": ("di traverso", "across the axis"),
    "pair_along": ("coppie lungo l'asse", "pairs along the axis"),
    "pair_across": ("coppie di traverso", "pairs across the axis"),
    "other": ("altre direzioni", "other directions"),
}
GROUP_COLOURS = {"along": "#1f77b4", "across": "#ff7f0e", "pair_along": "#2ca02c", "pair_across": "#d62728",
                 "other": "#8c8c8c"}
PAIR_ISOLATION = 1.5  # the next parallel line of each line of a pair is at least this many times farther
MIN_AXIS = 1e-6  # m, an axis shorter than this has no direction
SOURCE_KEY, SOURCE_VALUE = "classificazione", "orientamento"  # the attribute of the drawings written by to_layers
DEFAULT_PREFIX = "Classificato"


class OrientLinesError(ValueError):
    """A classification that cannot be made: ``str()`` is the English message, ``italian`` the Italian one (the
    window shows the Italian one in Italian, the command line the English one), as :class:`.ties.TieError`."""

    def __init__(self, english: str, italian: str):
        super().__init__(english)
        self.english = english
        self.italian = italian


@dataclass
class Segment:
    """A straight piece of an input line: ``source`` the index of the line, ``p0``/``p1`` its ends (``s0 <= s1``),
    ``angle`` degrees from the axis (-90..90), ``s0``/``s1`` positions along the axis, ``t`` the signed offset across
    it of the midpoint (positive on the left of the axis), ``group`` along|across|other (of the segment alone)."""

    id: int
    source: int
    p0: np.ndarray
    p1: np.ndarray
    length: float
    angle: float
    s0: float
    s1: float
    t: float
    group: str = ""


@dataclass
class Element:
    """A classified element: ``group`` one of :data:`GROUPS`, ``segments`` the ids of its segments, ``geometry`` a
    LineString (a line) or a Polygon (a pair, over the common extent of its two lines) in the input plane,
    ``position`` along the axis of its centre, ``side`` -1/0/1 (right of the axis, on it, left), ``width`` the mean
    distance of the two lines of a pair (None otherwise), ``angle`` degrees from the axis, ``offset`` the signed
    distance of its centre across the axis, ``length`` of the line or of the pair's common extent."""

    id: str
    group: str
    segments: list
    geometry: object
    position: float
    side: int
    width: float | None
    angle: float
    offset: float = 0.0
    length: float = 0.0


@dataclass
class Classification:
    """The result of :func:`classify`: ``axis`` ((x1, y1), (x2, y2)), ``params`` (the values used), ``segments``,
    ``elements``; ``frame`` None for a plan drawing (world x, y), the id of the elevation for one in its ``(u, v)``."""

    axis: tuple
    params: dict
    segments: list = field(default_factory=list)
    elements: list = field(default_factory=list)
    frame: str | None = None

    def counts(self) -> dict:
        """Elements per group (every group, also those with none)."""
        out = {g: 0 for g in GROUPS}
        for e in self.elements:
            out[e.group] += 1
        return out

    def element(self, eid: str) -> Element:
        for e in self.elements:
            if e.id == eid:
                return e
        raise KeyError(eid)

    def to_dict(self) -> dict:
        """JSON-ready: axis, params, frame, counts, segments and elements (geometries as WKT)."""
        return {"axis": [list(map(float, p)) for p in self.axis], "params": dict(self.params), "frame": self.frame,
                "counts": self.counts(),
                "segments": [{"id": s.id, "source": s.source, "p0": [float(v) for v in s.p0],
                              "p1": [float(v) for v in s.p1], "length": float(s.length), "angle": float(s.angle),
                              "s0": float(s.s0), "s1": float(s.s1), "t": float(s.t), "group": s.group}
                             for s in self.segments],
                "elements": [{"id": e.id, "group": e.group, "segments": list(e.segments), "wkt": e.geometry.wkt,
                              "position": float(e.position), "side": int(e.side),
                              "width": None if e.width is None else float(e.width), "angle": float(e.angle),
                              "offset": float(e.offset), "length": float(e.length)} for e in self.elements]}

    def plate(self, path, size=(2000, 1400), language: str = "it", names: dict | None = None,
              title: str | None = None) -> Path:
        """The check plate: the elements coloured by group with their ids, the axis. PNG or SVG by the suffix of
        ``path``; ``size`` in pixels. ``names``: {element id: text} written instead of the id (the plugins put their
        names). Returns the path."""
        return _plate(self, Path(path), size, language, names, title)


# ---------------------------------------------------------------- input

def _error(english: str, italian: str) -> OrientLinesError:
    return OrientLinesError(english, italian)


def _ring(xy: np.ndarray) -> np.ndarray:
    """A closed polyline of the ring ``xy`` (first vertex repeated at the end)."""
    xy = np.asarray(xy, float)
    if len(xy) and not np.array_equal(xy[0], xy[-1]):
        xy = np.vstack([xy, xy[:1]])
    return xy


def lines_from_layer(chunk, layer: str) -> list[np.ndarray]:
    """The lines and polygons of the free-drawing layer ``layer`` of ``chunk`` as ``(n, 2)`` arrays of world x, y (in
    its current world); a polygon as its ring closed, so that every edge is a line. OrientLinesError when the layer
    has none."""
    from ..core.free_drawings import features_in_world

    out = []
    for f in features_in_world(chunk, layer):
        xy = np.asarray(f["xyz"], float)[:, :2]
        if f["kind"] == "line" and len(xy) >= 2:
            out.append(xy)
        elif f["kind"] == "polygon" and len(xy) >= 3:
            out.append(_ring(xy))
    if not out:
        raise _error(f"no line or polygon in the layer {layer} of {chunk.label}",
                     f"nessuna linea o poligono nel layer {layer} di {chunk.label}")
    return out


def _elevation_entry(chunk, eid: str) -> dict:
    from .elevation import elevations

    for e in elevations(chunk):
        if e.get("id") == eid:
            return e
    raise _error(f"{chunk.label} has no elevation {eid}", f"{chunk.label} non ha il prospetto {eid}")


def lines_from_elevation(chunk, eid: str) -> list[np.ndarray]:
    """The drawings kept with the elevation ``eid`` as ``(n, 2)`` arrays of ``(u, v)``: its lines and the outlines of
    its units (rings closed, holes too). The stones are left out (they are what a plugin makes from a classification).
    OrientLinesError for an unknown elevation or one with no line."""
    from shapely import wkt

    from ..archaeo import breaklines
    from .elevation import frame_drawings

    _elevation_entry(chunk, eid)
    data = frame_drawings(chunk, eid)
    out = []
    for rec in data.get("lines") or []:
        if breaklines.valid(rec):
            out.append(np.array([[float(c[0]), float(c[1])] for c in rec["coords"]]))
    for rec in data.get("units") or []:
        try:
            geom = wkt.loads(rec["wkt"])
        except Exception:  # noqa: BLE001 - a broken record is left out
            continue
        for pg in getattr(geom, "geoms", [geom]):
            if pg.geom_type != "Polygon" or pg.is_empty:
                continue
            for ring in [pg.exterior, *pg.interiors]:
                out.append(np.asarray(ring.coords, float)[:, :2])
    if not out:
        raise _error(f"no line drawn on the elevation {eid} of {chunk.label}",
                     f"nessuna linea disegnata sul prospetto {eid} di {chunk.label}")
    return out


def lines_from_file(path) -> list[np.ndarray]:
    """The lines and polygons of a GeoPackage, GeoJSON or DXF (:func:`..io.free_drawings.read`) as ``(n, 2)`` arrays
    of x, y as the file has them (polygons closed). OrientLinesError when it has none."""
    from ..io import free_drawings as io_free

    path = Path(path)
    try:
        records = io_free.read(path)
    except FileNotFoundError:
        raise _error(f"no file {path}", f"il file {path} non c'è") from None
    out = []
    for r in records:
        xy = np.array([[float(c[0]), float(c[1])] for c in r["xyz"]], float)
        if r["kind"] == "line" and len(xy) >= 2:
            out.append(xy)
        elif r["kind"] == "polygon" and len(xy) >= 3:
            out.append(_ring(xy))
    if not out:
        raise _error(f"no line or polygon in {path.name}", f"nessuna linea o poligono in {path.name}")
    return out


def elevation_axis(chunk, eid: str) -> tuple:
    """The horizontal of the elevation ``eid`` on the plan: ((x1, y1), (x2, y2)) one metre along its u axis from its
    origin. OrientLinesError for a curved elevation or one whose u axis is vertical."""
    from .elevation import frame_from_dict

    entry = _elevation_entry(chunk, eid)
    frame = frame_from_dict(entry["frame"])
    if getattr(frame, "kind", "") != "plane":
        raise _error("the axis is taken from a flat elevation, not a developed one",
                     "l'asse si prende da un prospetto piano, non da uno sviluppato")
    u = np.asarray(frame.u_axis, float)[:2]
    if np.hypot(*u) < 1e-3:
        raise _error(f"the horizontal of the elevation {entry.get('name', eid)} is vertical on the plan",
                     f"l'orizzontale del prospetto {entry.get('name', eid)} è verticale sulla pianta")
    u = u / np.hypot(*u)
    o = np.asarray(frame.origin, float)[:2]
    return (float(o[0]), float(o[1])), (float(o[0] + u[0]), float(o[1] + u[1]))


# ---------------------------------------------------------------- parameters and geometry helpers

def checked_params(params: dict | None) -> dict:
    """:data:`DEFAULTS` updated with ``params`` (values as numbers); OrientLinesError for an unknown name or a value
    out of range."""
    out = dict(DEFAULTS)
    for k, v in (params or {}).items():
        if k not in DEFAULTS:
            raise _error(f"unknown parameter {k}: one of {', '.join(DEFAULTS)}",
                         f"parametro sconosciuto {k}: uno di {', '.join(DEFAULTS)}")
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            raise _error(f"the parameter {k} is a number, not {v!r}", f"il parametro {k} è un numero, non {v!r}") \
                from None
        if not math.isfinite(out[k]) or out[k] < 0:
            raise _error(f"the parameter {k} is a number of 0 or more", f"il parametro {k} è un numero da 0 in su")
    for k in ("along_deg", "across_deg", "join_deg", "pair_deg"):
        if out[k] > 90:
            raise _error(f"{k}: from 0 to 90 degrees", f"{k}: da 0 a 90 gradi")
    if out["along_deg"] + out["across_deg"] > 90:
        raise _error("along_deg and across_deg together at most 90 degrees",
                     "along_deg e across_deg insieme al massimo 90 gradi")
    if out["pair_min"] > out["pair_max"]:
        raise _error("pair_min above pair_max", "pair_min sopra pair_max")
    if not 0 < out["pair_overlap"] <= 1:
        raise _error("pair_overlap: above 0, up to 1", "pair_overlap: sopra 0, fino a 1")
    if out["across_bin"] <= 0:
        raise _error("across_bin: above 0", "across_bin: sopra 0")
    return out


def _fold(deg: float) -> float:
    """An angle of a direction (no sense) in (-90, 90]."""
    a = (deg + 90.0) % 180.0 - 90.0
    return 90.0 if a == -90.0 else a


def _angle_diff(a: float, b: float) -> float:
    """The angle between two directions (no sense), 0-90 degrees."""
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _mean_angle(angles, weights) -> float:
    """The weighted mean of directions (no sense): by the doubled angle."""
    th = np.radians(2 * np.asarray(angles, float))
    w = np.asarray(weights, float)
    return _fold(math.degrees(math.atan2(float(np.sum(w * np.sin(th))), float(np.sum(w * np.cos(th))))) / 2)


class _Frame:
    """The axis: ``origin``, unit ``u`` along it, ``n`` across it (left), local ``(s, t)`` of points and back."""

    def __init__(self, axis):
        try:
            a = np.asarray(axis, float).reshape(2, 2)
        except (TypeError, ValueError):
            raise _error("the axis is two points: ((x1, y1), (x2, y2))",
                         "l'asse sono due punti: ((x1, y1), (x2, y2))") from None
        if not np.all(np.isfinite(a)):
            raise _error("the axis has no valid numbers", "l'asse non ha numeri validi")
        d = a[1] - a[0]
        n = float(np.hypot(*d))
        if n < MIN_AXIS:
            raise _error("the axis has no length: its two points are the same",
                         "l'asse non ha lunghezza: i suoi due punti coincidono")
        self.points = a
        self.origin = a[0]
        self.u = d / n
        self.n = np.array([-self.u[1], self.u[0]])

    def st(self, p) -> np.ndarray:
        q = np.asarray(p, float) - self.origin
        return np.column_stack([q @ self.u, q @ self.n]) if q.ndim == 2 else np.array([q @ self.u, q @ self.n])

    def xy(self, st) -> np.ndarray:
        st = np.asarray(st, float)
        return self.origin + st[..., :1] * self.u + st[..., 1:2] * self.n


def checked_axis(axis) -> tuple:
    """``axis`` as ((x1, y1), (x2, y2)) of floats; OrientLinesError for one that is not two points or is shorter than
    :data:`MIN_AXIS`."""
    fr = _Frame(axis)
    return tuple(map(float, fr.points[0])), tuple(map(float, fr.points[1]))


# ---------------------------------------------------------------- segments

def _segments(lines, frame: _Frame, p: dict) -> list[Segment]:
    import shapely
    from shapely.geometry import LineString

    out: list[Segment] = []
    for src, line in enumerate(lines):
        xy = np.asarray(line, float)
        if xy.ndim != 2 or xy.shape[0] < 2 or xy.shape[1] < 2:
            continue
        xy = xy[:, :2]
        xy = xy[np.all(np.isfinite(xy), axis=1)]
        if len(xy) < 2:
            continue
        if p["simplify"] > 0:
            simple = shapely.simplify(LineString(xy), p["simplify"], preserve_topology=False)
            if not simple.is_empty and simple.geom_type == "LineString":
                xy = np.asarray(simple.coords, float)[:, :2]
        for a, b in zip(xy[:-1], xy[1:]):
            length = float(np.hypot(*(b - a)))
            if length < max(p["min_length"], 1e-12):
                continue
            sa, sb = frame.st(a), frame.st(b)
            if sb[0] < sa[0] or (sb[0] == sa[0] and sb[1] < sa[1]):
                a, b, sa, sb = b, a, sb, sa
            ang = _fold(math.degrees(math.atan2(sb[1] - sa[1], sb[0] - sa[0])))
            if abs(ang) <= p["along_deg"]:
                group = "along"
            elif abs(ang) >= 90.0 - p["across_deg"]:
                group = "across"
            else:
                group = "other"
            out.append(Segment(len(out), src, np.array(a), np.array(b), length, ang, float(sa[0]), float(sb[0]),
                               float((sa[1] + sb[1]) / 2), group))
    return out


@dataclass
class _Unit:
    """Joined pieces of one line: their segments in order and the line fitted through them."""

    group: str
    segs: list
    points: np.ndarray = None  # ordered vertices of the joined line
    centre: np.ndarray = None
    direction: np.ndarray = None  # unit
    angle: float = 0.0
    length: float = 0.0


def _oriented(seg: Segment, g: np.ndarray):
    """The ends of ``seg`` ordered along the direction ``g``."""
    if (seg.p1 - seg.p0) @ g >= 0:
        return seg.p0, seg.p1
    return seg.p1, seg.p0


def _cross(a, b) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def _runs(segs: list[Segment]) -> list[list[Segment]]:
    """The segments that follow each other on one drawn line (same source, a vertex in common): one run each. What
    was drawn as one line in one group stays one line, also where the simplification left a small kink."""
    runs: list[list[Segment]] = []
    for seg in sorted(segs, key=lambda s: s.id):
        if runs:
            prev = runs[-1][-1]
            ends = (prev.p0, prev.p1)
            if prev.source == seg.source and seg.id == prev.id + 1 and \
                    any(np.array_equal(e, q) for e in ends for q in (seg.p0, seg.p1)):
                runs[-1].append(seg)
                continue
        runs.append([seg])
    return runs


def _direction(angle: float, frame: "_Frame") -> np.ndarray:
    rad = math.radians(angle)
    return frame.u * math.cos(rad) + frame.n * math.sin(rad)


def _join(segs: list[Segment], g: np.ndarray, frame: "_Frame", p: dict) -> list[list[Segment]]:
    """The pieces of one line joined: the runs of each drawn line (:func:`_runs`), then the runs greedily by their
    position along ``g`` (the group's direction) when the angle between them is within ``join_deg``, the lateral
    offset within ``join_offset`` and the gap up to ``join_gap`` (angles as the mean of the pieces, by length)."""
    items = []
    for run in _runs(segs):
        ends = np.array([q for s in run for q in (s.p0, s.p1)])
        proj = ends @ g
        ang = _mean_angle([s.angle for s in run], [s.length for s in run])
        d = _direction(ang, frame)
        if d @ g < 0:
            d = -d
        items.append({"segs": run, "q0": ends[int(np.argmin(proj))], "q1": ends[int(np.argmax(proj))], "dir": d,
                      "angle": ang, "length": float(sum(s.length for s in run))})
    items.sort(key=lambda it: float(it["q0"] @ g))
    chains: list[dict] = []  # {"segs", "end", "dir", "angle", "angles", "weights"}
    for it in items:
        q0, d = it["q0"], it["dir"]
        best, best_score = None, None
        for ch in chains:
            if _angle_diff(ch["angle"], it["angle"]) > p["join_deg"]:
                continue
            e, de = ch["end"], ch["dir"]
            gap = float((q0 - e) @ de)
            if gap < -p["join_offset"] or gap > p["join_gap"]:
                continue
            off = max(abs(_cross(de, q0 - e)), abs(_cross(d, e - q0)))
            if off > p["join_offset"]:
                continue
            score = abs(gap) + off
            if best_score is None or score < best_score:
                best, best_score = ch, score
        if best is None:
            chains.append({"segs": list(it["segs"]), "end": it["q1"], "dir": d, "angle": it["angle"],
                           "angles": [it["angle"]], "weights": [it["length"]]})
        else:
            best["segs"] += it["segs"]
            if (it["q1"] - best["end"]) @ best["dir"] > 0:
                best["end"] = it["q1"]
            best["angles"].append(it["angle"])
            best["weights"].append(it["length"])
            best["angle"] = _mean_angle(best["angles"], best["weights"])
            nd = _direction(best["angle"], frame)
            best["dir"] = nd if nd @ g >= 0 else -nd
    return [c["segs"] for c in chains]


def _unit(group: str, segs: list[Segment], g: np.ndarray) -> _Unit:
    pts = []
    for s in sorted(segs, key=lambda s: float(_oriented(s, g)[0] @ g)):
        q0, q1 = _oriented(s, g)
        if not pts or not np.allclose(pts[-1], q0):
            pts.append(q0)
        pts.append(q1)
    pts = np.array(pts)
    w = np.array([s.length for s in segs])
    mids = np.array([(s.p0 + s.p1) / 2 for s in segs])
    ang = _mean_angle([s.angle for s in segs], w)
    return _Unit(group, list(segs), pts, (mids * w[:, None]).sum(0) / w.sum(), None, ang, float(w.sum()))


# ---------------------------------------------------------------- pairs

def _pairs(units: list[_Unit], frame: _Frame, p: dict) -> list[tuple[int, int, float, np.ndarray]]:
    """The pairs among ``units``: ``(i, j, width, corners)`` (corners of the polygon over the common extent)."""
    n = len(units)
    if n < 2:
        return []
    import shapely
    from shapely.geometry import LineString

    dirs = np.array([_direction(u.angle, frame) for u in units])
    angles = np.array([u.angle for u in units])
    near: list[list[tuple[float, int]]] = [[] for _ in range(n)]  # the lines beside each line, of its group
    info: dict = {}
    reach = PAIR_ISOLATION * p["pair_max"]  # farther lines matter neither for a pair nor for its isolation
    shapes = [LineString(u.points) for u in units]
    tree = shapely.STRtree(shapes)
    for i in range(n):
        x0, y0, x1, y1 = shapes[i].bounds
        for j in tree.query(shapely.box(x0 - reach, y0 - reach, x1 + reach, y1 + reach)):
            j = int(j)
            if j <= i or units[i].group != units[j].group:
                continue
            m_ang = _mean_angle([angles[i], angles[j]], [1.0, 1.0])
            rad = math.radians(m_ang)
            m = frame.u * math.cos(rad) + frame.n * math.sin(rad)
            nm = np.array([-m[1], m[0]])
            xi, xj = units[i].points @ m, units[j].points @ m
            lo, hi = max(xi.min(), xj.min()), min(xi.max(), xj.max())
            shorter = min(np.ptp(xi), np.ptp(xj))
            if hi <= lo or shorter <= 0 or (hi - lo) < p["pair_overlap"] * shorter - 1e-12:
                continue

            def at(k, x):  # the point of the fitted line of unit k at the position x along m
                c, d = units[k].centre, dirs[k]
                return c + ((x - c @ m) / (d @ m)) * d

            ends = [(at(i, lo), at(j, lo)), (at(i, hi), at(j, hi))]
            dist = float(np.mean([abs((a - b) @ nm) for a, b in ends]))
            if dist < p["pair_min"]:
                continue  # one line drawn twice, not two sides
            near[i].append((dist, j))
            near[j].append((dist, i))
            if _angle_diff(angles[i], angles[j]) <= p["pair_deg"]:  # a pair only of parallel lines
                info[(i, j)] = (dist, np.array([ends[0][0], ends[1][0], ends[1][1], ends[0][1]]))
    for lst in near:
        lst.sort()
    cands = []
    for (i, j), (dist, corners) in info.items():
        if dist > p["pair_max"]:
            continue
        ok = True
        for a, b in ((i, j), (j, i)):
            lst = near[a]
            if lst[0][1] != b and lst[0][0] < dist:
                ok = False
                break
            others = [d for d, k in lst if k != b]
            if others and others[0] < PAIR_ISOLATION * dist:
                ok = False
                break
        if ok:
            cands.append((dist, i, j, corners))
    cands.sort(key=lambda c: c[0])
    used: set = set()
    out = []
    for dist, i, j, corners in cands:
        if i in used or j in used:
            continue
        used |= {i, j}
        out.append((i, j, dist, corners))
    return out


# ---------------------------------------------------------------- classify

def _side(ts: np.ndarray, tol: float) -> int:
    lo, hi = float(np.min(ts)), float(np.max(ts))
    if lo < -tol and hi > tol:
        return 0
    m = float(np.mean(ts))
    if abs(m) <= tol:
        return 0
    return 1 if m > 0 else -1


def classify(lines, axis, params=None, frame: str | None = None) -> Classification:
    """``lines`` (2-D polylines in one plane, ``(n, 2)`` arrays) classified by their orientation along ``axis``
    ((x1, y1), (x2, y2)) with ``params`` over :data:`DEFAULTS` (see the module). ``frame``: the id of the elevation
    when the lines are its ``(u, v)`` (None: a plan). OrientLinesError (a ValueError) for an axis shorter than
    :data:`MIN_AXIS`, no line with a segment of ``min_length``, a parameter out of range."""
    from shapely.geometry import LineString, Polygon

    p = checked_params(params)
    fr = _Frame(axis)
    lines = list(lines or [])
    if not lines:
        raise _error("no line to classify", "nessuna linea da classificare")
    segs = _segments(lines, fr, p)
    if not segs:
        raise _error(f"no straight piece of {p['min_length']:g} m or more in the lines",
                     f"nessun tratto dritto di {p['min_length']:g} m o più nelle linee")
    units: list[_Unit] = []
    for group, g in (("along", fr.u), ("across", fr.n)):
        for chain in _join([s for s in segs if s.group == group], g, fr, p):
            units.append(_unit(group, chain, g))
    raw: list[dict] = []
    paired: set = set()
    tol = p["join_offset"]
    for i, j, width, corners in _pairs(units, fr, p):
        paired |= {i, j}
        ui, uj = units[i], units[j]
        st = fr.st(corners)
        group = "pair_along" if ui.group == "along" else "pair_across"
        poly = Polygon(corners)
        c = np.asarray(poly.centroid.coords[0])
        cst = fr.st(c)
        raw.append({"group": group, "segments": sorted(s.id for s in ui.segs + uj.segs), "geometry": poly,
                    "position": float(cst[0]), "offset": float(cst[1]), "side": _side(st[:, 1], tol),
                    "width": float(width), "angle": _mean_angle([ui.angle, uj.angle], [ui.length, uj.length]),
                    "length": float(np.mean([np.hypot(*(corners[1] - corners[0])),
                                             np.hypot(*(corners[2] - corners[3]))]))})
    # across lines left: grouped by position along the axis and side, joined when their extents across meet
    bins: dict = {}
    for k, u in enumerate(units):
        if k in paired:
            continue
        st = fr.st(u.points)
        if u.group == "along":
            raw.append(_line_element(u, st, tol))
            continue
        key = (math.floor(float(np.mean(st[:, 0])) / p["across_bin"]), _side(st[:, 1], tol))
        bins.setdefault(key, []).append((float(st[:, 1].min()), float(st[:, 1].max()), u))
    for key in sorted(bins):
        items = sorted(bins[key], key=lambda x: x[0])
        groups: list[list] = []
        for lo, hi, u in items:  # end to end (a small overlap allowed), not side by side
            if groups and -p["join_offset"] <= lo - groups[-1][1] <= p["join_gap"]:
                groups[-1][1] = max(groups[-1][1], hi)
                groups[-1][2].append(u)
            else:
                groups.append([lo, hi, [u]])
        for _lo, _hi, us in groups:
            if len(us) == 1:
                u = us[0]
            else:  # one element through the pieces, in their order across the axis
                segs_ = [s for x in us for s in x.segs]
                u = _unit("across", segs_, fr.n)
            raw.append(_line_element(u, fr.st(u.points), tol))
    for s in segs:
        if s.group == "other":
            st = fr.st(np.array([s.p0, s.p1]))
            mid = st.mean(axis=0)
            raw.append({"group": "other", "segments": [s.id], "geometry": LineString([s.p0, s.p1]),
                        "position": float(mid[0]), "offset": float(mid[1]), "side": _side(st[:, 1], tol),
                        "width": None, "angle": s.angle, "length": s.length})
    raw.sort(key=lambda r: (GROUPS.index(r["group"]), round(r["position"], 6), round(r["offset"], 6)))
    elements = [Element(str(k), r["group"], r["segments"], r["geometry"], r["position"], r["side"], r["width"],
                        r["angle"], r["offset"], r["length"]) for k, r in enumerate(raw, 1)]
    return Classification((tuple(map(float, fr.points[0])), tuple(map(float, fr.points[1]))), p, segs, elements,
                          frame)


def _line_element(u: _Unit, st: np.ndarray, tol: float) -> dict:
    from shapely.geometry import LineString

    c = st.mean(axis=0)
    return {"group": u.group, "segments": sorted(s.id for s in u.segs), "geometry": LineString(u.points),
            "position": float(c[0]), "offset": float(c[1]), "side": _side(st[:, 1], tol), "width": None,
            "angle": u.angle, "length": float(LineString(u.points).length)}


# ---------------------------------------------------------------- layers

def layer_name(group: str, prefix: str = DEFAULT_PREFIX) -> str:
    """The free-drawing layer of ``group`` written by :func:`to_layers`."""
    return f"{prefix} - {GROUP_LABELS[group][0]}"


def to_layers(chunk, result: Classification, prefix: str = DEFAULT_PREFIX) -> dict[str, int]:
    """The elements of ``result`` (a plan drawing: world x, y) as free drawings of ``chunk``, one layer per group
    (:func:`layer_name`), with the heights of the DEM at their vertices and the attributes ``gruppo`` (the group),
    ``elemento`` (its id), ``larghezza`` (m, of a pair), ``angolo``; the drawings of an earlier classification in those
    layers are taken away first (those drawn by hand stay). Returns ``{layer: drawings written}``. An element wholly
    outside the DEM takes the median height of the others (said in the log). OrientLinesError for an elevation's
    classification, without a DEM, with a DEM to rebuild or no height under the whole drawing (nothing written)."""
    from ..core import free_drawings, stamps
    from ..core.free_drawings import fill_missing, raster_values

    if result.frame is not None:
        raise _error("only a plan drawing is written as layers of free drawings: this one is on an elevation",
                     "solo un disegno della pianta si scrive come layer di disegni liberi: questo è su un prospetto")
    if not result.elements:
        raise _error("nothing classified: no layer to write", "niente di classificato: nessun layer da scrivere")
    prefix = (prefix or "").strip() or DEFAULT_PREFIX
    if chunk.dem_path is None or not chunk.dem_path.exists():
        raise _error(f"the chunk {chunk.label} has no DEM for the heights of the layers: build it first",
                     f"il chunk {chunk.label} non ha un DEM per le quote dei layer: costruiscilo prima")
    if stamps.is_stale(chunk, "dem"):
        raise _error(f"the DEM of {chunk.label} was built in another system of the chunk: rebuild it first",
                     f"il DEM di {chunk.label} è stato costruito in un altro sistema del chunk: rifallo prima")
    todo = []
    for e in result.elements:
        g = e.geometry
        if g.geom_type == "Polygon":
            kind, xy = "polygon", np.asarray(g.exterior.coords, float)[:-1, :2]
        else:
            kind, xy = "line", np.asarray(g.coords, float)[:, :2]
        z = fill_missing(raster_values(chunk.dem_path, xy), xy)
        todo.append([e, kind, xy, z])
    known = [v for _e, _k, _xy, z in todo if z is not None for v in z]
    if not known:
        raise _error(f"the DEM of {chunk.label} has no height under the drawing",
                     f"il DEM di {chunk.label} non ha quote sotto il disegno")
    outside = [item[0].id for item in todo if item[3] is None]
    if outside:  # wholly outside the DEM: at the median height of the others
        level = float(np.median(known))
        for item in todo:
            if item[3] is None:
                item[3] = [level] * len(item[2])
        log.warning("Disegno classificato: elementi fuori dal DEM alla quota mediana %.3f m: %s", level,
                    ", ".join(outside))
    todo = [(e, kind, np.column_stack([xy, np.asarray(z, float)])) for e, kind, xy, z in todo]
    names = {g: layer_name(g, prefix) for g in GROUPS}
    existing = {x["name"] for x in free_drawings.layers(chunk)}
    for g in {e.group for e, _k, _x in todo}:
        name = names[g]
        if name not in existing:
            free_drawings.set_layer(chunk, name, colour=GROUP_COLOURS[g])
        else:
            old = [f["id"] for f in free_drawings.features(chunk, name)
                   if (f.get("attributes") or {}).get(SOURCE_KEY) == SOURCE_VALUE]
            if old:
                free_drawings.remove(chunk, old)
    out: dict[str, int] = {}
    for e, kind, xyz in todo:
        attrs = {SOURCE_KEY: SOURCE_VALUE, "gruppo": e.group, "elemento": e.id, "angolo": round(float(e.angle), 2)}
        if e.width is not None:
            attrs["larghezza"] = round(float(e.width), 4)
        name = names[e.group]
        free_drawings.add(chunk, kind, xyz.tolist(), layer=name, name=f"{GROUP_LABELS[e.group][0]} {e.id}",
                          attributes=attrs)
        out[name] = out.get(name, 0) + 1
    return out


# ---------------------------------------------------------------- plate

def _plate(result: Classification, path: Path, size, language: str, names: dict | None, title: str | None) -> Path:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    from matplotlib.patches import Polygon as MplPolygon

    suffix = path.suffix.lower()
    if suffix not in (".png", ".svg"):
        raise _error(f"the plate is a PNG or an SVG, not {suffix or 'a file without suffix'}",
                     f"la tavola è un PNG o un SVG, non {suffix or 'un file senza estensione'}")
    lang = 0 if language == "it" else 1
    w, h = (max(200, int(v)) for v in size)
    fig = Figure(figsize=(w / 100, h / 100), dpi=100)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_aspect("equal")
    counts = result.counts()
    many = len(result.elements) > 400
    font = 7 if len(result.elements) > 120 else 9
    bounds = []
    for e in result.elements:
        colour = GROUP_COLOURS[e.group]
        g = e.geometry
        bounds.append(g.bounds)
        if g.geom_type == "Polygon":
            ax.add_patch(MplPolygon(np.asarray(g.exterior.coords)[:, :2], closed=True, facecolor=colour, alpha=0.35,
                                    edgecolor=colour, linewidth=1.2))
        else:
            xy = np.asarray(g.coords)
            ax.plot(xy[:, 0], xy[:, 1], color=colour, linewidth=1.4 if e.group != "other" else 0.8)
        if not many or e.group != "other":
            c = g.representative_point() if g.geom_type == "Polygon" else g.interpolate(0.5, normalized=True)
            label = (names or {}).get(e.id, e.id)
            ax.annotate(str(label), (c.x, c.y), fontsize=font, color="#202020", ha="center", va="center",
                        bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": colour, "lw": 0.6, "alpha": 0.85})
    if bounds:
        b = np.array(bounds)
        x0, y0, x1, y1 = b[:, 0].min(), b[:, 1].min(), b[:, 2].max(), b[:, 3].max()
    else:
        x0 = y0 = 0.0
        x1 = y1 = 1.0
    # the axis drawn across the whole drawing
    fr = _Frame(result.axis)
    corners = fr.st(np.array([[x0, y0], [x0, y1], [x1, y0], [x1, y1], *result.axis]))
    s_lo, s_hi = corners[:, 0].min(), corners[:, 0].max()
    a0, a1 = fr.xy(np.array([s_lo, 0.0])), fr.xy(np.array([s_hi, 0.0]))
    ax.annotate("", xy=tuple(a1), xytext=tuple(a0),
                arrowprops={"arrowstyle": "-|>", "color": "black", "lw": 1.0, "linestyle": "--"})
    ax.text(*a1, "  " + ("asse" if lang == 0 else "axis"), fontsize=9, va="center")
    pad = 0.05 * max(x1 - x0, y1 - y0, 1e-3)
    xs, ys = [x0, x1, a0[0], a1[0]], [y0, y1, a0[1], a1[1]]
    ax.set_xlim(min(xs) - pad, max(xs) + pad)
    ax.set_ylim(min(ys) - pad, max(ys) + pad)
    handles = []
    for g in GROUPS:
        label = f"{GROUP_LABELS[g][lang]} ({counts[g]})"
        if g.startswith("pair"):
            handles.append(Patch(facecolor=GROUP_COLOURS[g], alpha=0.5, edgecolor=GROUP_COLOURS[g], label=label))
        else:
            handles.append(Line2D([0], [0], color=GROUP_COLOURS[g], lw=2, label=label))
    ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=9, frameon=False)
    if title is None:
        title = "Disegno classificato per orientamento" if lang == 0 else "Drawing classified by orientation"
    ax.set_title(title, fontsize=11)
    unit = "m" if result.frame is None else "m (u, v)"
    ax.set_xlabel(unit, fontsize=8)
    ax.tick_params(labelsize=7)
    ax.ticklabel_format(useOffset=False, style="plain")  # coordinates as they are, no "+2.48e6"
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, format=suffix[1:])
    return path


__all__ = ["DEFAULTS", "GROUPS", "GROUP_LABELS", "GROUP_COLOURS", "PAIR_ISOLATION", "OrientLinesError", "Segment",
           "Element", "Classification", "lines_from_layer", "lines_from_elevation", "lines_from_file",
           "elevation_axis", "checked_axis", "checked_params", "classify", "to_layers", "layer_name"]
