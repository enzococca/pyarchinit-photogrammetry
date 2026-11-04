"""The frame of a local coordinate system and the motion that sets it.

Pure functions, no chunk: a :class:`Frame` is built from three picked points or from a plane
fitted to points, and :func:`motion` turns it into the 4x4 similarity that moves the chunk's
current coordinates into the new system.

Angle conventions (degrees):

* ``tilt_deg``: angle between the new z axis and the current z axis, 0..180.
* ``tilt_direction_deg``: azimuth, clockwise from the current +y (north), in [0, 360), of the
  steepest descent of the new horizontal plane (where it falls toward); 0 when untilted.
* ``heading_deg``: azimuth, clockwise from the current +y (north), in [0, 360), of the new x axis
  in the current plan.
"""
from __future__ import annotations

import copy as _copy
import json as _json
import logging as _logging
import os
import shutil as _shutil
import uuid as _uuid
from dataclasses import dataclass
from dataclasses import field as _field
from pathlib import Path as _Path

import numpy as np

from ..core import crs as crs_mod
from ..core import geometry as geo
from ..core import stamps

_log = _logging.getLogger(__name__)

REASONS = ("crs", "markers", "aligned_to", "scalebars", "collinear", "plane_points", "plane_rough", "nan")
COLLINEAR_SIN = 1e-3
MIN_PLANE_POINTS = 30
MAX_PLANE_NMAD = 0.10
HUBER_K = 1.345


class OrientRefused(ValueError):
    """The orientation cannot be done; ``reason`` is one of :data:`REASONS`."""

    def __init__(self, reason: str, message: str = "", names: list[str] | None = None):
        super().__init__(message or reason)
        self.reason = reason
        self.names = list(names or [])


def _angles(axes: np.ndarray) -> tuple[float, float, float]:
    z, x = axes[2], axes[0]
    tilt = float(np.degrees(np.arccos(np.clip(z[2], -1.0, 1.0))))
    direction = float(np.degrees(np.arctan2(z[0], z[1])) % 360.0) if np.hypot(z[0], z[1]) > 1e-12 else 0.0
    heading = float(np.degrees(np.arctan2(x[0], x[1])) % 360.0)
    return tilt, direction, heading


@dataclass
class Frame:
    origin: np.ndarray
    axes: np.ndarray  # rows x, y, z
    note: str = ""  # a short English reason when a requested choice was replaced by a default

    @property
    def tilt_deg(self) -> float:
        return _angles(self.axes)[0]

    @property
    def tilt_direction_deg(self) -> float:
        return _angles(self.axes)[1]

    @property
    def heading_deg(self) -> float:
        return _angles(self.axes)[2]


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def _frame(origin, x_hint: np.ndarray, normal: np.ndarray, note: str = "") -> Frame:
    """x_hint projected on the plane of ``normal``; y = z x x (right-handed)."""
    n = _unit(normal)
    x = x_hint - (x_hint @ n) * n
    if np.linalg.norm(x) < COLLINEAR_SIN * np.linalg.norm(x_hint):
        raise OrientRefused("collinear", "the x direction to keep is perpendicular to the plane")
    x = _unit(x)
    y = np.cross(n, x)
    return Frame(np.asarray(origin, dtype=float).copy(), np.vstack([x, y, n]), note)


def frame_from_points(a, b, c=None, x_along_ab: bool = True, flip: bool = False) -> Frame:
    """The frame set by points A, B (and C): A origin, x along A->B (or the current +x)."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b)) and (c is None or np.all(np.isfinite(c)))):
        raise OrientRefused("nan", "a point has no valid coordinates")
    ab = b - a
    if np.linalg.norm(ab) == 0:
        raise OrientRefused("collinear", "A and B coincide")
    if c is None:
        plan = np.array([ab[0], ab[1], 0.0])
        if np.linalg.norm(plan) < COLLINEAR_SIN * np.linalg.norm(ab):
            raise OrientRefused("collinear", "A-B is vertical: it gives no direction in plan")
        return _frame(a, plan, np.array([0.0, 0.0, -1.0 if flip else 1.0]))
    c = np.asarray(c, dtype=float)
    ac = c - a
    cross = np.cross(ab, ac)
    sin = np.linalg.norm(cross) / (np.linalg.norm(ab) * max(np.linalg.norm(ac), 1e-300))
    if sin < COLLINEAR_SIN:
        raise OrientRefused("collinear", "the three points are on a line")
    n = _unit(cross)
    if n[2] < 0:
        n = -n
    if flip:
        n = -n
    return _frame(a, ab if x_along_ab else np.array([1.0, 0.0, 0.0]), n)


def _nmad(r: np.ndarray) -> float:
    return float(1.4826 * np.median(np.abs(r - np.median(r))))


def fit_plane(xyz) -> tuple[np.ndarray, np.ndarray, float]:
    """Robust plane: ``(centre, unit normal, NMAD of the residuals)``."""
    p = np.asarray(xyz, dtype=float)
    w = np.ones(len(p))
    centre = p.mean(0)
    normal = np.array([0.0, 0.0, 1.0])
    previous = None
    for _ in range(21):  # least squares, then 20 reweightings
        centre = (w[:, None] * p).sum(0) / w.sum()
        q = (p - centre) * np.sqrt(w)[:, None]
        normal = np.linalg.svd(q, full_matrices=False)[2][2]
        r = (p - centre) @ normal
        scale = _nmad(r)
        k = HUBER_K * max(scale, 1e-12)
        w = np.where(np.abs(r) <= k, 1.0, k / np.maximum(np.abs(r), 1e-300))
        if previous is not None and np.max(np.abs(w - previous)) < 1e-9:
            break
        previous = w
    centre = (w[:, None] * p).sum(0) / w.sum()
    r = (p - centre) @ normal
    return centre, normal, _nmad(r)


def frame_from_plane(xyz, toward, a=None, b=None, flip: bool = False) -> Frame:
    """The frame of a plane fitted to ``xyz``; z on the side of the point ``toward``."""
    for given in (toward, a, b):
        if given is not None and not np.all(np.isfinite(np.asarray(given, dtype=float))):
            raise OrientRefused("nan", "a point has no valid coordinates")
    p = np.asarray(xyz, dtype=float).reshape(-1, 3)
    p = p[np.all(np.isfinite(p), axis=1)]
    if len(p) < MIN_PLANE_POINTS:
        raise OrientRefused("plane_points", f"{len(p)} points: at least {MIN_PLANE_POINTS} are needed")
    sv = np.linalg.svd(p - p.mean(0), compute_uv=False)
    if sv[0] <= 0 or sv[1] < 0.05 * sv[0]:
        raise OrientRefused("plane_points", "the region is too narrow to define a plane")
    centre, n, nmad = fit_plane(p)
    size = float(np.max(p.max(0) - p.min(0)))
    if nmad > MAX_PLANE_NMAD * size:
        raise OrientRefused("plane_rough", "the points are not on a plane")
    if (np.asarray(toward, dtype=float) - centre) @ n < 0:
        n = -n
    if flip:
        n = -n
    origin = centre
    x_hint = np.array([1.0, 0.0, 0.0])
    note = ""
    if b is not None and a is None:
        note = "B ignored without A: x kept"
    if a is not None:
        a = np.asarray(a, dtype=float)
        origin = a - ((a - centre) @ n) * n
        if b is not None:
            hint = np.asarray(b, dtype=float) - a
            if np.linalg.norm(hint - (hint @ n) * n) > COLLINEAR_SIN * max(np.linalg.norm(hint), 1e-300):
                x_hint = hint
            else:
                note = "A->B is along the normal: x kept"
    return _frame(origin, x_hint, n, note)


def motion(frame: Frame, quota: float = 0.0, origin_xy=(0.0, 0.0), scale: float = 1.0) -> np.ndarray:
    """p' = scale * R (p - origin) + (X0, Y0, quota), R = ``frame.axes``."""
    r = np.asarray(frame.axes, dtype=float)
    t = np.array([origin_xy[0], origin_xy[1], quota], dtype=float)
    return geo.compose_similarity(scale, r, t - scale * r @ frame.origin)


def describe(m) -> dict:
    s, r, t = geo.decompose_similarity(np.asarray(m, dtype=float))
    tilt, direction, heading = _angles(r)
    return {
        "tilt_deg": tilt,
        "tilt_direction_deg": direction,
        "heading_deg": heading,
        "translation": [float(v) for v in t],
        "scale": float(s),
    }


def scale_for(a, b, distance: float) -> float:
    """The factor that makes |AB| equal ``distance`` (in the new units)."""
    d = float(np.linalg.norm(np.asarray(b, dtype=float) - np.asarray(a, dtype=float)))
    if not distance > 0 or not d > 0:
        raise ValueError("the distance and |AB| must be positive")
    return float(distance) / d


# ---------------------------------------------------------------- a motion applied to a chunk and what it keeps
#
# A motion ``M`` (a similarity: rotation, translation, scale) set by the user on a chunk in a local system moves the
# chunk's transform (internal -> chunk coordinates: ``M @ T``) and everything kept in chunk coordinates with it, so that
# every drawing, reference and frame stays on the same point of the model. The stores are those of
# :func:`..gnss.heights.set_survey_crs`, carried by the same movers (``heights._Follow`` with ``M``: a point with a
# height as it is, a point of the plan at the height of the ground there), plus what a change of system leaves in
# the old one and a motion of the local system can move exactly: the elevations' frames, the drawings kept with them
# in (u, v), the proxies of the units. The rasters on a grid (DEM, orthomosaic, ...) are left as they are and read
# as stale through their stamps (:mod:`..core.stamps`).

# the keys of chunk.meta an orientation changes (restored by put_back): the stores moved, and the stamps it writes
MOVED_KEYS = ("drawings", "stones", "orthomosaic", "texture", "colour_chart", "region", "elevations", "proxies",
              "levelled", "built_in", "versions", "relief", "calculator", "multispectral", "thermal", "satellite")
# the keys of chunk.meta the program knows; the others are the data of plugins
PROGRAM_KEYS = frozenset((
    "active_versions", "built_in", "calculator", "chunk_alignment", "classes", "colour_chart", "confidence", "coverage",
    "date", "dem", "dem_edits", "dem_precision", "dense", "drawings", "elevations", "field_check", "gnss",
    "gnss_only_fix", "import", "layer_labels", "levelled", "live_links", "marker_excluded", "marker_frames", "merge",
    "metric", "model", "multispectral", "normal_map", "optimize", "orientation", "orthomosaic", "panorama",
    "point_cloud_cleaning", "point_cloud_source", "pottery", "precision", "proxies", "quality", "region", "relief",
    "satellite", "scans", "section_options", "sfm", "sharpness", "stones", "texture", "thermal", "tin", "versions",
    "video",
))
_LISTENERS: list[tuple] = []  # (callback, keys)


def aligned_to(chunk) -> list:
    """The chunks of the project aligned to ``chunk`` (*Allinea chunk*): their transforms are in its frame."""
    doc = getattr(chunk, "document", None)
    return [c for c in getattr(doc, "chunks", []) or []
            if c is not chunk and (c.meta.get("chunk_alignment") or {}).get("reference_key") == chunk.key]


def _control_markers(chunk) -> list:
    """The control markers with typed references that are not orientation markers: they give the orientation."""
    return [m for m in chunk.markers if m.reference.location and m.reference.enabled and not m.checkpoint
            and not getattr(m, "orientation", False)]


def check(chunk) -> None:
    """Refuses (:class:`OrientRefused`) the orientation of ``chunk`` when something else gives it: the world (a chunk
    in a projected or geographic system), three or more control markers with typed coordinates, the reference chunk
    it is aligned to."""
    if not crs_mod.is_local(chunk.crs):
        raise OrientRefused("crs", "the world gives the orientation: use markers with coordinates")
    controls = _control_markers(chunk)
    if len(controls) >= 3:
        raise OrientRefused("markers", "the orientation is given by the markers: change their coordinates",
                            [m.label for m in controls])
    alignment = chunk.meta.get("chunk_alignment")
    if alignment:
        doc = getattr(chunk, "document", None)
        ref = next((c for c in getattr(doc, "chunks", []) or [] if c.key == alignment.get("reference_key")), None)
        name = ref.label if ref is not None else str(alignment.get("reference") or alignment.get("reference_key"))
        raise OrientRefused("aligned_to", "orient the reference, the chunks aligned to it follow", [name])


def on_transform_changed(callback, keys=()):
    """Registers ``callback(chunk, M)``, called after the transform of a chunk was changed by ``M`` (an orientation);
    ``keys``: the keys of ``chunk.meta`` it moves itself (a plugin's data). Returns the function that unregisters it.
    Since 1.7.2."""
    item = (callback, tuple(keys or ()))
    _LISTENERS.append(item)

    def stop() -> None:
        if item in _LISTENERS:
            _LISTENERS.remove(item)

    return stop


def fire(chunk, m) -> None:
    """Tells the listeners (:func:`on_transform_changed`) that ``chunk`` moved by ``m``; a listener that fails is
    written in the log, the others are still told."""
    for callback, _keys in list(_LISTENERS):
        try:
            callback(chunk, np.array(m, float))
        except Exception:  # noqa: BLE001 - a plugin must not undo the orientation
            _log.exception("a listener of the change of the transform failed")


def _claimed() -> set:
    return {k for _cb, keys in _LISTENERS for k in keys}


# ---------------------------------------------------------------- the state an orientation can be undone to

@dataclass
class OrientState:
    """What :func:`apply` changes, to put it back (:func:`put_back`): per chunk its transform (the array itself and its
    values), system, the keys of :data:`MOVED_KEYS`, markers and scale bars; the file of the sections of the project;
    the text of the elevations' ``frame.json`` / ``profile.json``, the tags and the grid of their rasters; clones of
    the proxies' PLY files (under ``<chunk data>/undo/orient-<id>/``, removed by :func:`drop`)."""

    chunks: list = _field(default_factory=list)
    sections: _Path | None = None
    sections_text: str | None = None
    texts: dict = _field(default_factory=dict)  # path -> text
    rasters: dict = _field(default_factory=dict)  # path -> (tags, affine as 6 numbers)
    clones: dict = _field(default_factory=dict)  # path -> clone
    folders: list = _field(default_factory=list)


def _elevation_files(chunk):
    """``(frame.json, profile.json or None, [rasters])`` of each elevation of ``chunk``."""
    for e in chunk.meta.get("elevations") or []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        folder = chunk.data_dir / "elevations" / str(e["id"])
        rasters = [chunk.data_dir / e[k] for k in ("orthophoto", "relief") if e.get(k)]
        profile = chunk.data_dir / e["profile"] if e.get("profile") else None
        yield e, folder / "frame.json", profile, rasters


def capture(chunks, project_sections=None) -> OrientState:
    """The state of ``chunks`` and of the file of the sections ``project_sections`` (a path, or None) before an
    orientation."""
    import rasterio

    state = OrientState()
    for ch in chunks:
        state.chunks.append({
            "chunk": ch, "transform": ch.transform, "values": np.array(ch.transform, float).copy(), "crs": ch.crs,
            "meta": {k: _copy.deepcopy(ch.meta[k]) for k in MOVED_KEYS if k in ch.meta},
            "markers": [m.to_dict() for m in ch.markers], "scalebars": [s.to_dict() for s in ch.scalebars]})
        for _e, frame_json, profile, rasters in _elevation_files(ch):
            for path in (frame_json, profile):
                if path is not None and path.exists():
                    state.texts[path] = path.read_text(encoding="utf-8")
            for path in rasters:
                if path.exists():
                    with rasterio.open(path) as src:
                        state.rasters[path] = (dict(src.tags()), tuple(src.transform)[:6])
        proxies = [p for p in ch.meta.get("proxies") or [] if isinstance(p, dict) and p.get("file")]
        files = [ch.data_dir / p["file"] for p in proxies if (ch.data_dir / p["file"]).exists()]
        if files:
            folder = ch.data_dir / "undo" / f"orient-{_uuid.uuid4().hex[:12]}"
            from ..io.files import clone

            for i, path in enumerate(files):
                copy = folder / f"{i}_{path.name}"
                clone(path, copy)
                state.clones[path] = copy
            state.folders.append(folder)
    if project_sections is not None:
        state.sections = _Path(project_sections)
        if state.sections.exists():
            state.sections_text = state.sections.read_text(encoding="utf-8")
    return state


def put_back(state: OrientState) -> None:
    """Everything :func:`capture` saw, as it was: the transforms (in the same arrays), systems, keys of ``chunk.meta``,
    markers, scale bars, the files of the elevations and of the proxies and the sections of the project."""
    import rasterio
    from affine import Affine

    from ..core.marker import Marker, ScaleBar

    for saved in state.chunks:
        ch = saved["chunk"]
        array = saved["transform"]
        array[...] = saved["values"]
        ch.transform, ch.crs = array, saved["crs"]
        for key in MOVED_KEYS:
            if key in saved["meta"]:
                ch.meta[key] = _copy.deepcopy(saved["meta"][key])
            else:
                ch.meta.pop(key, None)
        ch.markers = [Marker.from_dict(d) for d in saved["markers"]]
        ch.scalebars = [ScaleBar.from_dict(dict(d)) for d in saved["scalebars"]]
    for path, text in state.texts.items():
        path.write_text(text, encoding="utf-8")
    for path, (tags, grid) in state.rasters.items():
        if path.exists():
            with rasterio.open(path, "r+") as dst:
                dst.update_tags(**tags)
                dst.transform = Affine(*grid)
    for path, copy in state.clones.items():
        if copy.exists():
            _shutil.copy2(copy, path)
    if state.sections is not None:
        if state.sections_text is None:
            state.sections.unlink(missing_ok=True)
        else:
            state.sections.parent.mkdir(parents=True, exist_ok=True)
            work = state.sections.with_name(state.sections.name + ".tmp")
            work.write_text(state.sections_text, encoding="utf-8")
            os.replace(work, state.sections)


def drop(state: OrientState) -> None:
    """The clones :func:`capture` made, deleted (the state cannot be put back after)."""
    for folder in state.folders:
        _shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------- the stores moved

def _point(m: np.ndarray, p) -> np.ndarray:
    p = np.asarray(p, float).reshape(3)
    return m[:3, 0] * p[0] + m[:3, 1] * p[1] + m[:3, 2] * p[2] + m[:3, 3]


def _tilt(info: dict | None, m: np.ndarray, s: float, axis: np.ndarray, centre: np.ndarray) -> dict | None:
    """The record of how the axis of a cylinder was chosen, after the motion: its point moved (without one, the moved
    ``centre`` of the cylinder gives its height), its angles those of the moved axis, its lengths scaled."""
    if info is None:
        return None
    out = dict(info)
    if out.get("centre") is not None:
        out["centre"] = [float(v) for v in _point(m, out["centre"])]
        out["centre_z"] = float(out["centre"][2])
    elif out.get("centre_z") is not None:
        out["centre_z"] = float(centre[2])
    tilt = float(np.degrees(np.arccos(np.clip(axis[2], -1.0, 1.0))))
    out["tilt_deg"] = tilt
    out["azimuth_deg"] = float(np.degrees(np.arctan2(axis[0], axis[1])) % 360.0) if tilt > 1e-9 else 0.0
    for key in ("residual_m", "fitted_radius", "points_drift_m"):
        if out.get(key) is not None:
            out[key] = float(out[key]) * s
    return out


def _moved_frame(frame, m: np.ndarray, s: float, r: np.ndarray, heights: bool):
    """``(new frame, c, v_is_height)``: ``frame`` moved by ``m``; a drawing at ``(u, v)`` is at ``(s u, s v + c)`` in
    the new one (``c``: a plane whose v measured the heights and still does, re-anchored on Z = 0)."""
    from . import elevation as el

    if frame.kind == "cylinder":
        axis = r @ frame.axis
        u_map = None
        if frame.u_map is not None:
            u_map = np.column_stack([frame.u_map[:, 0], frame.u_map[:, 1] * s])
        centre = _point(m, frame.centre)
        new = el.Cylinder(centre, axis, frame.radius * s, frame.inside, r @ frame.start,
                          tilt=_tilt(frame.tilt, m, s, axis / np.linalg.norm(axis), centre), u_map=u_map)
        return new, 0.0, False
    moved = el.Plane(_point(m, frame.origin), r @ frame.u_axis, r @ frame.v_axis)
    if heights and moved.v_axis[2] >= el.VERTICAL_TOLERANCE:
        anchored = el.with_heights(moved)
        c = float((moved.origin - anchored.origin) @ anchored.v_axis)
        return anchored, c, el.heights_measured(anchored)
    return moved, 0.0, False


def _uv(s: float, c: float):
    def f(uv):
        uv = np.asarray(uv, float).reshape(-1, 2)
        return np.column_stack([uv[:, 0] * s, uv[:, 1] * s + c])
    return f


def _frame_drawings(data: dict, s: float, c: float, mover, shapely) -> int:
    """The drawings kept with an elevation, in place: (u, v) -> (s u, s v + c) (``s`` 1 and ``c`` 0: as they are), and
    their world positions (spot heights, measuring points) carried by ``mover``. Returns how many."""
    from ..gnss import heights as hg

    n = 0
    f = _uv(s, c)
    moved = s != 1.0 or c != 0.0
    for unit in data.get("units") or []:
        if unit.get("wkt") and moved:
            unit["wkt"] = shapely.transform(shapely.from_wkt(unit["wkt"]), f).wkt
        n += 1
    for key, height in (("quote", "quota_q"), ("points", "z_superficie")):
        rows = [r for r in data.get(key) or [] if isinstance(r, dict)]
        for r in rows:
            if moved and "u" in r and "v" in r:
                r["u"], r["v"] = (float(v) for v in f([[r["u"], r["v"]]])[0])
        hg._move_rows([r for r in rows if "x" in r and "y" in r], height, mover)
        n += len(rows)
    for line in data.get("lines") or []:
        if moved:
            for coord in line.get("coords") or []:
                coord[0], coord[1] = (float(v) for v in f([[coord[0], coord[1]]])[0])
        n += 1
    for key, items in (data.get("stones") or {}).items():
        if moved:
            data["stones"][key] = [shapely.transform(shapely.from_wkt(w), f).wkt for w in items]
        n += len(items)
    return n


def _elevations(chunk, m, s, r, mover, drawings: dict | None, shapely):
    """The new entries of the elevations, the new texts of their files and the new tags and grids of their rasters
    (written later), and how many drawings kept with them were moved."""
    entries, texts, rasters, n = [], {}, {}, 0
    frames = (drawings or {}).get("frames") or {}
    import rasterio
    from affine import Affine

    from . import elevation as el

    seen = set()
    for e, frame_json, profile, files in _elevation_files(chunk):
        seen.add(e["id"])
        new = _copy.deepcopy(e)
        frame, c, heights = _moved_frame(el.frame_from_dict(e["frame"]), m, s, r, bool(e.get("v_is_height")))
        new["frame"] = frame.to_dict()
        new["v_is_height"] = heights
        if frame.kind == "cylinder" and frame.tilt is not None:
            new["tilt"] = dict(frame.tilt)
        if e.get("depth") is not None:
            new["depth"] = [float(v) * s for v in e["depth"]]
        if e.get("resolution") is not None:
            new["resolution"] = float(e["resolution"]) * s
        if e.get("bounds"):
            u0, v0, u1, v1 = (float(v) for v in e["bounds"])
            new["bounds"] = [u0 * s, v0 * s + c, u1 * s, v1 * s + c]
        if frame_json.exists():
            saved = _json.loads(frame_json.read_text(encoding="utf-8"))
            for key in ("frame", "depth", "resolution", "bounds", "v_is_height"):
                if key in new:
                    saved[key] = new[key]
            texts[frame_json] = _json.dumps(saved, indent=1)
        if profile is not None and profile.exists() and (s != 1.0 or c != 0.0):
            lines = _json.loads(profile.read_text(encoding="utf-8")).get("lines") or []
            f = _uv(s, c)
            texts[profile] = _json.dumps({"lines": [f(line).round(6).tolist() if len(line) else line
                                                    for line in lines]})
        for path in files:
            if not path.exists():
                continue
            with rasterio.open(path) as src:
                t = src.transform
            tags = {"ppg_frame": _json.dumps(new["frame"])}
            if new.get("depth") is not None:
                tags["ppg_depth"] = _json.dumps(new["depth"])
            grid = Affine(t.a * s, t.b * s, t.c * s, t.d * s, t.e * s, t.f * s + c)
            rasters[path] = (tags, tuple(grid)[:6])
        data = frames.get(e["id"])
        if isinstance(data, dict):
            n += _frame_drawings(data, s, c, mover, shapely)
        entries.append(new)
    for eid, data in frames.items():  # drawings of an elevation no longer listed: their world positions only
        if eid not in seen and isinstance(data, dict):
            n += _frame_drawings(data, 1.0, 0.0, mover, shapely)
    return entries, texts, rasters, n


def _proxies(chunk, m, s):
    """The new records of the proxies and their moved vertices (``{path: MeshData}``, written later)."""
    from ..io.mesh_ply import read_mesh

    records, meshes = [], {}
    for p in chunk.meta.get("proxies") or []:
        new = _copy.deepcopy(p)
        path = chunk.data_dir / p["file"] if isinstance(p, dict) and p.get("file") else None
        centre = None
        if path is not None and path.exists():
            mesh = read_mesh(path)
            xyz = mesh.xyz
            moved = xyz[:, :1] * m[:3, 0] + xyz[:, 1:2] * m[:3, 1] + xyz[:, 2:3] * m[:3, 2] + m[:3, 3]
            for k, name in enumerate(("x", "y", "z")):
                mesh.vertices[name] = moved[:, k]
            meshes[path] = mesh
            centre = xyz.mean(axis=0)
        if isinstance(new, dict):
            if centre is not None:
                def height(z, cx=centre[0], cy=centre[1]):
                    return float(_point(m, [cx, cy, float(z)])[2])

                for key in ("z_top", "z_bottom"):
                    if new.get(key) is not None:
                        new[key] = [height(z) for z in new[key]]
                if new.get("base_z") is not None:
                    new["base_z"] = height(new["base_z"])
            if new.get("volume_m3") is not None:
                new["volume_m3"] = float(new["volume_m3"]) * s ** 3
            if new.get("area_m2") is not None:
                new["area_m2"] = float(new["area_m2"]) * s ** 2
            if new.get("thickness") is not None:
                new["thickness"] = float(new["thickness"]) * s
        records.append(new)
    return records, meshes


def _write_proxies(meshes: dict) -> None:
    from ..io.mesh_ply import write_mesh

    for path, mesh in meshes.items():
        write_mesh(path, mesh)


def _write_rasters(rasters: dict) -> None:
    import rasterio
    from affine import Affine

    for path, (tags, grid) in rasters.items():
        with rasterio.open(path, "r+") as dst:
            dst.update_tags(**tags)
            dst.transform = Affine(*grid)


def move_stores(chunk, m, sections=None) -> dict:
    """Everything kept in the coordinates of ``chunk`` carried by the motion ``m`` (the chunk's transform is not
    changed here: the caller pre-multiplies it). The new values are built aside and written only when all of them are
    built; the files (the elevations' ``frame.json``, ``profile.json``, tags and grids of their rasters, the proxies'
    PLY, the ``sections`` of the project when given - written to their file) before ``chunk.meta``. Returns how many
    of each store moved."""
    import shapely

    from ..gnss import heights as hg

    m = np.asarray(m, float).reshape(4, 4)
    s, r, _t = geo.decompose_similarity(m)
    before = np.asarray(chunk.transform, float)
    extent: list = []

    def level() -> float:
        if not extent:
            extent.append(hg._extent_points(chunk, before))
        return hg._level(extent[0], before)

    mover = hg._Follow(m, hg._Ground(chunk.dem_path, chunk.crs, level))
    values, moved_sections = hg._drawn(chunk, mover, sections, plan_stones=True)
    values.pop("elevations", None)  # the crs written in each entry: here the entries are moved below
    meta = chunk.meta
    drawings = values.get("drawings") or {}
    counts = {
        "units": len(hg._units(drawings)), "quote": len(hg._rows(drawings, "quote")),
        "points": len(hg._rows(drawings, "points")),
        "lines": sum(1 for x in drawings.get("lines") or [] if isinstance(x, dict)),
        "free": sum(1 for x in drawings.get("free") or [] if isinstance(x, dict)),
        "stones": sum(len(v) for k, v in (values.get("stones") or {}).items()
                      if v and k != hg.PLAN_STONES_LEGACY),
        "patches": len((meta.get("orthomosaic") or {}).get("patches") or []),
        "texture_patches": sum(1 for p in (meta.get("texture") or {}).get("patches") or [] if p.get("polygon")),
        "colour_chart": int(bool((meta.get("colour_chart") or {}).get("bounds"))),
        "sections": len(moved_sections or []),
    }
    if meta.get("region"):
        values["region"] = hg._region_follows(meta["region"], np.eye(4), m)  # M itself: exact at any translation
        counts["region"] = 1
    entries, texts, rasters, n_frame = _elevations(chunk, m, s, r, mover, drawings, shapely)
    if meta.get("elevations"):
        values["elevations"] = entries
    counts["elevations"], counts["elevation_drawings"] = len(entries), n_frame
    records, meshes = _proxies(chunk, m, s)
    if meta.get("proxies"):
        values["proxies"] = records
    counts["proxies"] = len(meshes)
    typed = [mk for mk in chunk.markers if mk.reference.location]
    references = []
    for mk in typed:
        from ..core.camera import Reference

        references.append((mk, Reference(location=[float(v) for v in _point(m, mk.reference.location)],
                                         accuracy=[float(a) * s for a in mk.reference.accuracy],
                                         enabled=mk.reference.enabled)))
    counts["markers"] = len(references)
    # written: the files first, then the chunk
    for path, text in texts.items():
        path.write_text(text, encoding="utf-8")
    _write_rasters(rasters)
    _write_proxies(meshes)
    if moved_sections is not None:
        hg._write_sections(hg._sections_file(chunk), moved_sections)
    for mk, reference in references:
        mk.reference = reference
    meta.update(values)
    return counts


def _stamp_unstamped(chunk) -> None:
    """The products built before 1.7.2 (no stamp: read as current) stamped with the transform of the chunk now, before
    it changes: after the motion they read as stale. The elevations are moved with it: not here."""
    meta, products = chunk.meta, chunk.products or {}
    built = meta.get("built_in") or {}
    for name in (*stamps.GRID_PRODUCTS, "slope", "aspect"):
        if products.get(name) and name not in built:
            stamps.stamp(chunk, name)
    records = list((meta.get("relief") or {}).values()) + list((meta.get("calculator") or {}).values())
    ms = meta.get("multispectral") or {}
    if ms.get("reflectance"):
        records.append(ms)
    records += list((ms.get("indices") or {}).values()) + list((ms.get("zoning") or {}).values())
    th = meta.get("thermal") or {}
    if th.get("file"):
        records.append(th)
    if isinstance(th.get("anomaly"), dict):
        records.append(th["anomaly"])
    records += [x for kind in ("scenes", "stereo") for x in (meta.get("satellite") or {}).get(kind) or []]
    records += [x for name in ("dem", "orthomosaic") for x in (meta.get("versions") or {}).get(name) or []]
    for record in records:
        if isinstance(record, dict) and record.get("built_in") is None:
            stamps.stamp_entry(chunk, record)


def _sent_out(chunk) -> list[dict]:
    """What left the program in the old coordinates and stays there (ruling 10): the units and spot heights published
    to PyArchInit (a ``gid``), the QField packages and the exported files (said in a sentence, no names)."""
    d = chunk.meta.get("drawings") or {}
    names = [str(u.get("us") or (u.get("attributes") or {}).get("gid")) for u in d.get("units") or []
             if (u.get("attributes") or {}).get("gid") is not None]
    names += [f"quota {q.get('us_q') or q.get('gid')}" for q in d.get("quote") or [] if q.get("gid") is not None]
    out = [{"what": "pyarchinit", "names": names}] if names else []
    return out + [{"what": "qfield", "names": []}, {"what": "exports", "names": []}]


def _sections_said(sections: list | None, moved: bool, tilted: bool) -> list[dict]:
    if not sections:
        return []
    labels = [str(x.get("label")) for x in sections]
    if not moved:
        return [{"what": "sections", "names": labels}]
    out = []
    drawn = [str(x.get("label")) for x in sections if x.get("svg") or x.get("dxf") or x.get("dxf3d")]
    if drawn:
        out.append({"what": "section_drawings", "names": drawn})
    vertical = [str(x.get("label")) for x in sections if (x.get("kind") or "vertical") == "vertical"]
    if tilted and vertical:
        out.append({"what": "vertical_sections", "names": vertical})  # a vertical section stays vertical: another cut
    return out


def apply(chunk, m, move_aligned: bool = True) -> dict:
    """``chunk`` (in a local system) moved by the motion ``m``: its transform pre-multiplied (``m @ T``) and everything
    kept in its coordinates carried along (:func:`move_stores`), with the chunks aligned to it (``move_aligned``);
    the sections of the project moved when no other chunk of the project is in a local system (else kept);
    ``meta["levelled"]`` set (the levelling of the DEM must not undo it); the elevations moved with it stay current,
    the rasters on a grid are stale. The window stores its drawings into the chunk first. Refused as :func:`check`
    says. On any error everything is put back and the error raised again.

    Returns ``{"moved": {store: n}, "aligned": [labels moved], "aligned_left": [labels not moved], "stale":
    :func:`..core.stamps.stale_products`, "kept": [{"what", "names"}], "sent_out": [{"what", "names"}],
    "plugins_unaware": [keys of chunk.meta of plugins not listening], "state": the :class:`OrientState` to undo it}``."""
    from ..gnss import heights as hg

    check(chunk)
    m = np.asarray(m, float).reshape(4, 4)
    if not np.all(np.isfinite(m)):
        raise OrientRefused("nan", "the motion has no valid numbers")
    s, r, _t = geo.decompose_similarity(m)
    aligned = aligned_to(chunk)
    moving = [chunk] + (aligned if move_aligned else [])
    doc = getattr(chunk, "document", None)
    path = hg._sections_file(chunk)
    sections = None
    if path is not None and path.exists():
        try:
            sections = [dict(x) for x in _json.loads(path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            sections = None
    others = [c for c in getattr(doc, "chunks", []) or [] if all(c is not x for x in moving)
              and crs_mod.is_local(c.crs)]
    move_sections = bool(sections) and not others
    state = capture(moving, path)
    moved: dict = {}
    try:
        for ch in moving:
            current = [e for e in ch.meta.get("elevations") or [] if isinstance(e, dict)
                       and not stamps.entry_stale(ch, e)]
            _stamp_unstamped(ch)
            counts = move_stores(ch, m, sections if (ch is chunk and move_sections) else None)
            for key, n in counts.items():
                moved[key] = moved.get(key, 0) + n
            ch.transform = m @ np.asarray(ch.transform, float)
            ch.meta["levelled"] = True
            if s == 1.0 or np.isclose(s, 1.0, rtol=stamps.STAMP_RTOL, atol=0.0):
                ids = {e.get("id") for e in current}
                for e in ch.meta.get("elevations") or []:  # moved with it: current again (those current before)
                    if isinstance(e, dict) and e.get("id") in ids:
                        stamps.stamp_entry(ch, e)
    except BaseException:
        put_back(state)
        drop(state)
        raise
    tilted = not np.allclose(r[2], [0.0, 0.0, 1.0], atol=1e-12)
    kept = _sections_said(sections, move_sections, tilted)
    for ch in moving:
        fire(ch, m)
    claimed = _claimed()
    unaware = sorted({k for ch in moving for k in ch.meta if k not in PROGRAM_KEYS and k not in claimed})
    return {"moved": moved, "aligned": [c.label for c in aligned] if move_aligned else [],
            "aligned_left": [] if move_aligned else [c.label for c in aligned], "stale": stamps.stale_products(chunk),
            "kept": kept, "sent_out": _sent_out(chunk), "plugins_unaware": unaware, "state": state}
