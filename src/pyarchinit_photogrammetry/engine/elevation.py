"""Orthophotos and DEMs on a plane or a cylinder: elevations of walls, wells, apses, towers.

A frame - :class:`Plane` or :class:`Cylinder`, in world coordinates (the chunk CRS, metres) - maps
a raster cell ``(u, v)`` (``u`` along the surface, left to right as seen by the viewer, ``v`` up) and
a depth ``d`` (towards the viewer) to the world. The surface of a cell is where a ray from the
viewer side meets the model; the orthophoto is the orthomosaic engine on that surface (same photo
choice, blending, colour correction), the DEM is the depth ``d`` of the surface: the relief of a
wall over its plane (a bulge, a lean), of a well over its cylinder.

``depth=(dmin, dmax)`` keeps only the surface within that band around the plane: what stands in
front of ``dmax`` (scaffolding, a section left in front of the wall) is passed through.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress


def _unit(v) -> np.ndarray:
    v = np.asarray(v, np.float64)
    n = np.linalg.norm(v)
    if n < 1e-12:
        raise ValueError("a direction of zero length")
    return v / n


@dataclass
class Plane:
    """``origin`` + u ``u_axis`` + v ``v_axis`` + d ``normal`` (``normal`` = u x v, towards the viewer)."""

    origin: np.ndarray
    u_axis: np.ndarray
    v_axis: np.ndarray
    kind: str = field(default="plane", init=False)

    def __post_init__(self):
        self.origin = np.asarray(self.origin, np.float64)
        self.u_axis = _unit(self.u_axis)
        v = np.asarray(self.v_axis, np.float64)
        self.v_axis = _unit(v - (v @ self.u_axis) * self.u_axis)

    @property
    def normal(self) -> np.ndarray:
        return np.cross(self.u_axis, self.v_axis)

    def point(self, u, v, d) -> np.ndarray:
        u, v, d = (np.asarray(a, np.float64) for a in (u, v, d))
        return self.origin + u[..., None] * self.u_axis + v[..., None] * self.v_axis + d[..., None] * self.normal

    def view(self, u, v) -> np.ndarray:
        u = np.asarray(u, np.float64)
        return np.broadcast_to(self.normal, u.shape + (3,)).copy()

    def coords(self, p: np.ndarray) -> np.ndarray:
        """``(u, v, d)`` of world points."""
        q = np.asarray(p, np.float64) - self.origin
        return np.column_stack([q @ self.u_axis, q @ self.v_axis, q @ self.normal])

    def max_depth(self) -> float:
        return np.inf

    def to_dict(self) -> dict:
        return {"kind": "plane", "origin": self.origin.tolist(), "u_axis": self.u_axis.tolist(),
                "v_axis": self.v_axis.tolist()}


@dataclass
class Cylinder:
    """A vertical (or any ``axis``) cylinder of ``radius`` around the line through ``centre``,
    seen from outside (a tower, a column) or from the axis (``inside``: a well, an apse);
    ``start``: the direction (perpendicular to the axis) where ``u`` = 0."""

    centre: np.ndarray
    axis: np.ndarray
    radius: float
    inside: bool = False
    start: np.ndarray | None = None
    kind: str = field(default="cylinder", init=False)

    def __post_init__(self):
        self.centre = np.asarray(self.centre, np.float64)
        self.axis = _unit(self.axis)
        s = np.asarray(self.start if self.start is not None else [1.0, 0.0, 0.0], np.float64)
        s = s - (s @ self.axis) * self.axis
        if np.linalg.norm(s) < 1e-9:
            s = np.array([0.0, 1.0, 0.0]) - self.axis[1] * self.axis
        self.start = _unit(s)
        self.radius = float(self.radius)

    def _e(self, u) -> np.ndarray:
        theta = np.asarray(u, np.float64) / self.radius * (-1.0 if self.inside else 1.0)
        other = np.cross(self.axis, self.start)
        return np.cos(theta)[..., None] * self.start + np.sin(theta)[..., None] * other

    def point(self, u, v, d) -> np.ndarray:
        v, d = np.asarray(v, np.float64), np.asarray(d, np.float64)
        r = self.radius - d if self.inside else self.radius + d
        return self.centre + v[..., None] * self.axis + r[..., None] * self._e(u)

    def view(self, u, v) -> np.ndarray:
        e = self._e(u)
        return -e if self.inside else e

    def coords(self, p: np.ndarray) -> np.ndarray:
        q = np.asarray(p, np.float64) - self.centre
        v = q @ self.axis
        radial = q - v[:, None] * self.axis
        other = np.cross(self.axis, self.start)
        theta = np.arctan2(radial @ other, radial @ self.start)
        r = np.linalg.norm(radial, axis=1)
        u = theta * self.radius * (-1.0 if self.inside else 1.0)
        d = self.radius - r if self.inside else r - self.radius
        return np.column_stack([u, v, d])

    def max_depth(self) -> float:
        return self.radius if self.inside else np.inf  # from inside a ray starts on the axis at most

    def to_dict(self) -> dict:
        return {"kind": "cylinder", "centre": self.centre.tolist(), "axis": self.axis.tolist(),
                "radius": self.radius, "inside": self.inside, "start": self.start.tolist()}


def frame_from_dict(d: dict):
    if d.get("kind") == "cylinder":
        return Cylinder(d["centre"], d["axis"], d["radius"], bool(d.get("inside")), d.get("start"))
    return Plane(d["origin"], d["u_axis"], d["v_axis"])


# ---------------------------------------------------------------- frames from what the user draws
def plane_from_line(chunk: Chunk, p1, p2, side=None) -> Plane:
    """The vertical plane through the trace ``p1``-``p2`` (plan coordinates of the chunk CRS) of a
    wall, seen from ``side`` (a plan point on the viewer's side; default: the side where most aligned
    photos are). ``u`` runs along the trace as the viewer sees it, ``v`` up from the lowest point of
    the model there."""
    a, b = np.asarray(p1, np.float64)[:2], np.asarray(p2, np.float64)[:2]
    along = np.array([*(b - a), 0.0])
    up = np.array([0.0, 0.0, 1.0])
    normal = _unit(np.cross(along, up))  # horizontal, perpendicular to the trace
    mid = (a + b) / 2
    if side is None:
        centres = chunk.to_world(np.array([c.center for c in chunk.aligned_cameras])) if chunk.aligned_cameras \
            else np.zeros((0, 3))
        votes = np.sign((centres[:, :2] - mid) @ normal[:2]).sum() if len(centres) else 1.0
        if votes < 0:
            normal = -normal
    elif (np.asarray(side, np.float64)[:2] - mid) @ normal[:2] < 0:
        normal = -normal
    u_axis = np.cross(up, normal)  # right of a viewer looking at the wall: up x towards the viewer
    start = a if (b - a) @ u_axis[:2] > 0 else b
    return Plane([start[0], start[1], 0.0], u_axis, up)


def plane_from_points(points, toward=None) -> Plane:
    """The plane fitted to three or more world points (markers on a wall), ``v`` as close to the
    vertical as the plane allows, facing ``toward`` (a world point; default the +y side of a vertical
    plane / up for a horizontal one)."""
    p = np.asarray(points, np.float64).reshape(-1, 3)
    if len(p) < 3:
        raise ValueError("three points at least")
    c = p.mean(axis=0)
    normal = np.linalg.eigh((p - c).T @ (p - c))[1][:, 0]
    if toward is not None and (np.asarray(toward, np.float64) - c) @ normal < 0:
        normal = -normal
    up = np.array([0.0, 0.0, 1.0])
    v = up - (up @ normal) * normal
    if np.linalg.norm(v) < 1e-6:  # a horizontal plane: v along y
        v = np.array([0.0, 1.0, 0.0]) - normal[1] * normal
    v = _unit(v)
    u = np.cross(v, normal)
    pc = p - c
    return Plane(c + min(pc @ u) * u + min(pc @ v) * v, u, v)


VERTICAL_TOLERANCE = np.cos(np.radians(1.0))  # a v axis within 1 degree of the vertical measures heights


def with_heights(plane: Plane) -> Plane:
    """``plane`` moved along its ``v`` axis so that ``v`` is the height (world Z) when that axis is
    vertical within a degree: the elevation of a wall, a section, read with its true heights.
    Other planes are returned as they are."""
    if plane.v_axis[2] < VERTICAL_TOLERANCE:
        return plane
    origin = plane.origin - (plane.origin[2] / plane.v_axis[2]) * plane.v_axis
    return Plane(origin, plane.u_axis, plane.v_axis)


def heights_measured(frame) -> bool:
    """Whether ``v`` of the frame is the height (world Z) of the point."""
    return bool(frame.kind == "plane" and frame.v_axis[2] >= VERTICAL_TOLERANCE and abs(frame.origin[2]) < 1e-6)


def plane_from_markers(points, a, b, c=None, d=None, toward=None, heights: bool = True) -> Plane:
    """The plane of markers with the axes chosen, as Metashape's planar projection on markers:
    the plane fitted to ``points`` (world, three or more, ``a`` and ``b`` among them), ``u`` along
    ``a`` -> ``b`` and ``u`` = 0 at ``a``; ``v`` along ``c`` -> ``d`` when given, else perpendicular to
    ``u`` in the plane and upwards. The plane faces ``toward`` (a world point on the viewer's side,
    the cameras): when ``a`` -> ``b`` runs from right to left as the viewer sees it, ``u`` grows the
    other way (``b`` at a negative ``u``) - the surface is never seen from behind.

    ``heights``: when ``v`` is vertical (within a degree), ``v`` is the world height (see
    :func:`with_heights`); otherwise ``v`` = 0 at ``a``."""
    p = np.asarray(points, np.float64).reshape(-1, 3)
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    if len(p) < 3:
        raise ValueError("three markers at least")
    centre = p.mean(axis=0)
    w, vecs = np.linalg.eigh((p - centre).T @ (p - centre))
    if w[1] < 1e-12 * max(w[2], 1e-30):
        raise ValueError("the markers lie on a line: they do not make a plane")
    normal = vecs[:, 0]
    if toward is not None and (np.asarray(toward, np.float64) - centre) @ normal < 0:
        normal = -normal
    u = b - a
    u = u - (u @ normal) * normal
    if np.linalg.norm(u) < 1e-9:
        raise ValueError("the two markers of the horizontal axis coincide (or are aligned with the view)")
    u = _unit(u)
    if c is not None and d is not None:
        v = np.asarray(d, np.float64) - np.asarray(c, np.float64)
        v = v - (v @ normal) * normal - (v @ u) * u
        if np.linalg.norm(v) < 1e-9:
            raise ValueError("the vertical axis is parallel to the horizontal one")
        v = _unit(v)
        if np.cross(u, v) @ normal < 0:
            u = -u  # seen from the cameras' side: u flips, v stays as chosen
    else:
        v = np.cross(normal, u)  # u x v = normal: the plane faces the viewer
        if abs(normal[2]) < VERTICAL_TOLERANCE and v[2] < 0:
            u, v = -u, -v  # upwards; a horizontal plane keeps u along a -> b
    origin = a - ((a - centre) @ normal) * normal
    plane = Plane(origin, u, v)
    return with_heights(plane) if heights else plane


def plane_from_view(centre, toward, up, heights: bool = True) -> Plane:
    """The plane of what a view shows: through ``centre`` (world), facing ``toward`` (a direction
    from the scene towards the viewer), ``v`` along the ``up`` of the screen, ``u`` to the right -
    the orthophoto of the model as the 3D view looks at it (Metashape's planar projection on the
    current view)."""
    normal = _unit(toward)
    v = np.asarray(up, np.float64)
    v = v - (v @ normal) * normal
    if np.linalg.norm(v) < 1e-9:
        raise ValueError("the up of the view is along the line of sight")
    v = _unit(v)
    plane = Plane(np.asarray(centre, np.float64), np.cross(v, normal), v)
    return with_heights(plane) if heights else plane


def cylinder_from_circle(chunk: Chunk, points, inside: bool = True) -> Cylinder:
    """The vertical cylinder through a circle fitted to plan points (three or more on the rim of a
    well or around a tower, chunk CRS)."""
    q = np.asarray(points, np.float64)[:, :2]
    if len(q) < 3:
        raise ValueError("three points at least")
    # the algebraic fit on the points shifted to their mean: on raw projected coordinates (UTM,
    # millions of metres) x^2 + y^2 swallows the radius in rounding (a tower of 10 m came out with
    # a radius of 1,385 km); the radius does not change with the shift
    mean = q.mean(axis=0)
    d = q - mean
    a = np.column_stack([2 * d, np.ones(len(d))])
    sol = np.linalg.lstsq(a, (d ** 2).sum(axis=1), rcond=None)[0]
    r = float(np.sqrt(sol[2] + sol[0] * sol[0] + sol[1] * sol[1]))
    cx, cy = float(sol[0] + mean[0]), float(sol[1] + mean[1])
    return Cylinder([cx, cy, 0.0], [0.0, 0.0, 1.0], r, inside, [q[0, 0] - cx, q[0, 1] - cy, 0.0])


# ---------------------------------------------------------------- the surface of a frame
class FrameSurface:
    """Depth ``d`` of the cells of a frame: rays from ``dmax`` towards the surface (the model),
    the first hit kept when it lies above ``dmin``. Grid: ``origin`` (u0, v1 top-left), ``shape``,
    ``resolution`` - the interface of the surfaces of :mod:`.ortho`."""

    def __init__(self, chunk: Chunk, raycaster, frame, resolution: float, lock: threading.Lock,
                 depth: tuple[float, float] | None = None, bounds=None):
        self.chunk, self.raycaster, self.frame, self.lock = chunk, raycaster, frame, lock
        self.resolution = float(resolution)
        lo, hi = (depth if depth is not None else (-np.inf, np.inf))
        verts = None
        if bounds is None or not np.isfinite([lo, hi]).all():
            from ..io.mesh_ply import read_mesh

            verts = frame.coords(chunk.to_world(read_mesh(chunk.product_path("model")).xyz))
        if not np.isfinite(hi):
            hi = float(np.nanmax(verts[:, 2])) + 0.05 if verts is not None and len(verts) else 1.0
        if not np.isfinite(lo):
            lo = float(np.nanmin(verts[:, 2])) - 0.05 if verts is not None and len(verts) else -1.0
        hi = min(hi, frame.max_depth())
        self.dmin, self.dmax = float(lo), float(hi)
        if bounds is None:
            band = verts[(verts[:, 2] >= lo) & (verts[:, 2] <= hi)]
            if not len(band):
                raise ValueError("no part of the model lies in the depth band of the frame")
            bounds = (band[:, 0].min(), band[:, 1].min(), band[:, 0].max(), band[:, 1].max())
        u0, v0, u1, v1 = (float(b) for b in bounds)
        self.bounds = (u0, v0, u1, v1)
        self.origin = (np.floor(u0 / resolution) * resolution, np.ceil(v1 / resolution) * resolution)
        self.shape = (int(np.ceil((self.origin[1] - v0) / resolution)) + 1,
                      int(np.ceil((u1 - self.origin[0]) / resolution)) + 1)
        self._inv = np.linalg.inv(chunk.transform)

    def cell_uv(self, r0: int, c0: int, th: int, tw: int):
        cc, rr = np.meshgrid(np.arange(tw), np.arange(th))
        u = self.origin[0] + (c0 + cc + 0.5) * self.resolution
        v = self.origin[1] - (r0 + rr + 0.5) * self.resolution
        return u, v

    def tile(self, r0: int, c0: int, th: int, tw: int) -> np.ndarray:
        u, v = self.cell_uv(r0, c0, th, tw)
        u, v = u.ravel(), v.ravel()
        start = self.frame.point(u, v, np.full(len(u), self.dmax))
        direction = -self.frame.view(u, v)
        start_int = start @ self._inv[:3, :3].T + self._inv[:3, 3]
        dir_int = direction @ self._inv[:3, :3].T
        dir_int /= np.linalg.norm(dir_int, axis=1, keepdims=True)
        with self.lock:
            t = self.raycaster.hit_distance(start_int, dir_int) * self.chunk.scale  # world units
        d = self.dmax - t
        d[~np.isfinite(t) | (d < self.dmin)] = np.nan
        return d.reshape(th, tw)


# ---------------------------------------------------------------- products
def elevations(chunk: Chunk) -> list[dict]:
    return list(chunk.meta.get("elevations") or [])


def cut_profile(chunk: Chunk, frame: Plane, bounds=None, simplify: float = 0.0) -> list[np.ndarray]:
    """The line where ``frame`` cuts the model, as polylines of ``(u, v)`` (frame metres): the
    profile of a section. ``bounds`` (u0, v0, u1, v1) keeps the parts inside; ``simplify`` in metres."""
    from shapely.geometry import MultiLineString, box
    from shapely.ops import linemerge

    from ..io.mesh_ply import read_mesh

    mesh = read_mesh(chunk.product_path("model"))
    xyz = chunk.to_world(np.asarray(mesh.xyz, np.float64))
    faces = np.asarray(mesh.faces, np.int64)
    s = (xyz - frame.origin) @ frame.normal
    above = s >= 0
    fa = above[faces]
    cross = fa.any(axis=1) & ~fa.all(axis=1)
    f = faces[cross]
    if not len(f):
        return []
    seg = []
    for i, j in ((0, 1), (1, 2), (2, 0)):
        vi, vj = f[:, i], f[:, j]
        edge = above[vi] != above[vj]
        t = s[vi] / np.where(edge, s[vi] - s[vj], 1.0)
        seg.append((xyz[vi] + t[:, None] * (xyz[vj] - xyz[vi]), edge))
    points = np.stack([p for p, _ in seg], axis=1)  # (F, 3 edges, 3)
    edges = np.stack([e for _, e in seg], axis=1)
    order = np.argsort(~edges, axis=1, kind="stable")[:, :2]  # the two edges that cross, in face order
    ends = np.take_along_axis(points, order[:, :, None], axis=1)
    uv = frame.coords(ends.reshape(-1, 3))[:, :2].reshape(-1, 2, 2)
    lines = MultiLineString([tuple(map(tuple, s2)) for s2 in uv if np.linalg.norm(s2[1] - s2[0]) > 1e-12])
    merged = linemerge(lines)
    if bounds is not None:
        merged = merged.intersection(box(*bounds))
    if simplify > 0:
        merged = merged.simplify(simplify)
    parts = getattr(merged, "geoms", [merged])
    return [np.asarray(g.coords) for g in parts if not g.is_empty and g.geom_type == "LineString" and len(g.coords) > 1]


def markers_on(chunk: Chunk, entry: dict, margin: float = 0.05) -> list[tuple[str, float, float, float]]:
    """``(label, u, v, d)`` of the markers of the chunk that lie on the elevation ``entry``: inside
    its bounds and its depth band (``margin`` metres more each way)."""
    frame = frame_from_dict(entry["frame"])
    ms = [m for m in chunk.markers if m.position is not None]
    if not ms:
        return []
    uvd = frame.coords(chunk.to_world(np.array([m.position for m in ms], np.float64)))
    lo, hi = entry.get("depth") or (-np.inf, np.inf)
    u0, v0, u1, v1 = entry.get("bounds") or (-np.inf, -np.inf, np.inf, np.inf)
    keep = (uvd[:, 2] >= lo - margin) & (uvd[:, 2] <= hi + margin) & (uvd[:, 0] >= u0 - margin) & \
        (uvd[:, 0] <= u1 + margin) & (uvd[:, 1] >= v0 - margin) & (uvd[:, 1] <= v1 + margin)
    return [(m.label, float(x), float(y), float(z)) for m, (x, y, z), k in zip(ms, uvd, keep) if k]


def profile_path(chunk: Chunk, entry: dict) -> Path | None:
    """The profile of a section (``profile.json``: polylines of ``(u, v)``), when the entry has one."""
    p = chunk.data_dir / entry["profile"] if entry.get("profile") else None
    return p if p is not None and p.exists() else None


def read_profile(chunk: Chunk, entry: dict) -> list[np.ndarray]:
    p = profile_path(chunk, entry)
    return [] if p is None else [np.asarray(line, np.float64) for line in json.loads(p.read_text())["lines"]]


def write_profile_dxf(chunk: Chunk, entry: dict, path) -> Path | None:
    """The profile of a section as a DXF in the coordinates of the elevation (u, v in metres; v the
    height when the plane is upright): polylines on the layer PROFILO, the markers on MARKER. None
    when the entry has no profile."""
    import ezdxf

    lines = read_profile(chunk, entry)
    if not lines:
        return None
    dxf = ezdxf.new("R2010")
    dxf.units = ezdxf.units.M
    msp = dxf.modelspace()
    dxf.layers.add("PROFILO", color=1)
    dxf.layers.add("MARKER", color=4)
    for line in lines:
        msp.add_lwpolyline([tuple(map(float, q)) for q in line], dxfattribs={"layer": "PROFILO"})
    for label, u, v, _d in markers_on(chunk, entry):
        msp.add_point((u, v), dxfattribs={"layer": "MARKER"})
        msp.add_text(label, height=max(0.02, 4 * float(entry.get("resolution") or 0.005)),
                     dxfattribs={"layer": "MARKER"}).set_placement((u, v))
    path = Path(path)
    dxf.saveas(path)
    return path


def build_elevation(chunk: Chunk, frame, resolution: float | None = None, name: str | None = None,
                    depth: tuple[float, float] | None = None, blending: str = "mosaic",
                    colour_correction: bool | None = None, ghosting: bool = False, fill_holes: bool = True,
                    progress: Progress | None = None, workers: int | None = None, max_angle: float = 80.0,
                    bounds=None, kind: str = "elevation") -> dict:
    """Orthophoto (RGBA) and DEM (depth ``d`` towards the viewer, float32) of the model on ``frame``,
    written in ``<chunk data>/elevations/<id>/`` (GeoTIFF in frame metres, no CRS; the frame in the
    tags and in ``frame.json``) and listed in ``chunk.meta["elevations"]``. Returns the entry.

    ``bounds``: ``(u0, v0, u1, v1)`` of the part to build (default: the model in the depth band).
    ``kind`` ``section``: nothing in front of the plane is kept (the depth band starts at 0) and the
    line where the plane cuts the model is saved as the profile (``profile.json``, a plane only)."""
    if kind == "section":
        if frame.kind != "plane":
            raise ValueError("a section is made on a plane")
        lo = depth[0] if depth is not None else -np.inf
        depth = (lo, 0.0)
    import rasterio
    from rasterio.enums import ColorInterp, Resampling
    from rasterio.transform import from_origin
    from rasterio.windows import Window

    from .dem import estimate_gsd
    from .ortho import _context, _run_tiles, correction_for
    from .visibility import raycaster_for_chunk, readable_cameras

    progress = as_progress(progress)
    if chunk.product_path("model") is None:
        raise RuntimeError("build the model first: an elevation is made on the model")
    progress(0.0, "Checking the photos")
    cams, _bad = readable_cameras(chunk, True)
    resolution = float(resolution or estimate_gsd(chunk))
    correction = correction_for(chunk, colour_correction, progress.sub(0.0, 0.02))
    raycaster = raycaster_for_chunk(chunk)
    lock = threading.Lock()
    surf = FrameSurface(chunk, raycaster, frame, resolution, lock, depth, bounds)
    tile = 512 if frame.kind == "plane" else max(32, min(512, int(0.15 * frame.radius / resolution)))
    ctx = _context(chunk, resolution, cams, surf, raycaster, blending, max_angle, 8, workers, None, [], tile, lock,
                   correction=correction, ghosting=ghosting, fill_holes=fill_holes)
    ctx.frame = frame
    h, w = surf.shape
    eid = uuid.uuid4().hex[:8]
    folder = chunk.data_dir / "elevations" / eid
    folder.mkdir(parents=True, exist_ok=True)
    transform = from_origin(surf.origin[0], surf.origin[1], resolution, resolution)
    tags = {"ppg_frame": json.dumps(frame.to_dict()), "ppg_depth": json.dumps([surf.dmin, surf.dmax])}
    profile = {"driver": "GTiff", "height": h, "width": w, "count": 4, "dtype": "uint8", "transform": transform,
               "compress": "deflate", "photometric": "RGB", "BIGTIFF": "IF_SAFER"}
    if h >= 256 and w >= 256:
        profile.update(tiled=True, blockxsize=256, blockysize=256)
    ortho_path = folder / "elevation.tif"
    tiles = [(r, c) for r in range(0, h, tile) for c in range(0, w, tile)]
    progress(0.03, f"Elevation {w}x{h} px at {resolution:.4g} m/px from {len(cams)} photos")
    with rasterio.open(ortho_path, "w", **profile) as dst:
        dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        dst.update_tags(**tags)
        _run_tiles(ctx, tiles, ctx.workers, progress, lambda r0, c0, block: dst.write(
            block, window=Window(c0, r0, block.shape[2], block.shape[1])), start=0.03, span=0.82)
        factors = [f for f in (2, 4, 8, 16, 32, 64) if min(h, w) // f >= 64]
        if factors:
            dst.build_overviews(factors, Resampling.average)
    progress(0.86, "Relief of the surface (DEM of the elevation)")
    dem_path = folder / "relief.tif"
    dprof = {"driver": "GTiff", "height": h, "width": w, "count": 1, "dtype": "float32", "transform": transform,
             "compress": "deflate", "nodata": float("nan"), "BIGTIFF": "IF_SAFER"}
    if h >= 256 and w >= 256:
        dprof.update(tiled=True, blockxsize=256, blockysize=256)
    with rasterio.open(dem_path, "w", **dprof) as dst:
        dst.update_tags(**tags)
        for i, (r0, c0) in enumerate(tiles):
            th, tw = min(tile, h - r0), min(tile, w - c0)
            dst.write(surf.tile(r0, c0, th, tw).astype(np.float32)[None], window=Window(c0, r0, tw, th))
            if i % 16 == 0:
                progress(0.86 + 0.12 * (i + 1) / len(tiles), f"Relief {i + 1}/{len(tiles)}")
    heights = heights_measured(frame)
    (folder / "frame.json").write_text(json.dumps({"frame": frame.to_dict(), "depth": [surf.dmin, surf.dmax],
                                                   "resolution": resolution, "kind": kind, "bounds": list(surf.bounds),
                                                   "v_is_height": heights}, indent=1))
    default = "Sezione" if kind == "section" else ("Prospetto" if frame.kind == "plane" else "Sviluppo cilindrico")
    entry = {"id": eid, "name": name or default, "kind": kind, "crs": chunk.crs,
             "frame": frame.to_dict(), "depth": [surf.dmin, surf.dmax], "resolution": resolution,
             "bounds": list(surf.bounds), "v_is_height": heights,
             "orthophoto": str(ortho_path.relative_to(chunk.data_dir)), "relief": str(dem_path.relative_to(chunk.data_dir)),
             "size": [w, h], "blending": blending, "colour_correction": correction is not None,
             "max_angle": float(max_angle), "max_cameras": 8, "blend_sigma": ctx.blend_sigma,
             "ghosting": bool(ghosting), "fill_holes": bool(fill_holes)}
    if kind == "section":
        progress(0.985, "Profile of the section")
        lines = cut_profile(chunk, frame, surf.bounds, simplify=resolution / 2)
        path = folder / "profile.json"
        path.write_text(json.dumps({"lines": [line.round(4).tolist() for line in lines]}))
        entry["profile"] = str(path.relative_to(chunk.data_dir))
    chunk.meta.setdefault("elevations", []).append(entry)
    progress(1.0, f"Elevation {w}x{h} px")
    return entry


def remove_elevation(chunk: Chunk, eid: str) -> bool:
    import shutil

    entries = elevations(chunk)
    keep = [e for e in entries if e["id"] != eid]
    if len(keep) == len(entries):
        return False
    shutil.rmtree(chunk.data_dir / "elevations" / eid, ignore_errors=True)
    chunk.meta["elevations"] = keep
    return True


def elevation_paths(chunk: Chunk, entry: dict) -> tuple[Path, Path]:
    return chunk.data_dir / entry["orthophoto"], chunk.data_dir / entry["relief"]


def entry_settings(entry: dict) -> dict:
    """How the orthophoto of an elevation was painted, to paint some of its cells again the same way. Entries made
    before these were stored (1.7.0) take the values build_elevation used then."""
    from .ortho import BLEND_SIGMA

    blending = entry.get("blending", "mosaic")
    return {"blending": blending,
            "blend_sigma": float(entry.get("blend_sigma", BLEND_SIGMA if blending == "mosaic" else 0.0)),
            "max_angle": float(entry.get("max_angle", 80.0)), "max_cameras": int(entry.get("max_cameras", 8)),
            "ghosting": bool(entry.get("ghosting", False)), "fill_holes": bool(entry.get("fill_holes", True)),
            "colour_correction": bool(entry.get("colour_correction", False))}


def frame_grid(chunk: Chunk, entry: dict, raycaster, lock: threading.Lock) -> FrameSurface:
    """The surface of an elevation on the grid of its file (depth band and bounds of the entry); ``RuntimeError`` when
    the file is missing or its grid is not the entry's."""
    import rasterio

    path = elevation_paths(chunk, entry)[0]
    if not path.exists():
        raise RuntimeError(f"the orthophoto of the elevation is missing: {path}")
    surf = FrameSurface(chunk, raycaster, frame_from_dict(entry["frame"]), float(entry["resolution"]), lock,
                        tuple(entry["depth"]), tuple(entry["bounds"]))
    with rasterio.open(path) as src:
        t, shape = src.transform, (src.height, src.width)
    tol = 1e-6 * max(1.0, abs(t.c), abs(t.f))
    if shape != surf.shape or abs(t.c - surf.origin[0]) > tol or abs(t.f - surf.origin[1]) > tol:
        raise RuntimeError(f"the file of the elevation does not match its record: {path}")
    return surf


# ---------------------------------------------------------------- drawings made on an elevation
def relief_at(chunk: Chunk, entry: dict, u: float, v: float) -> float | None:
    """The depth ``d`` of the surface at ``(u, v)`` of the elevation (its relief raster); None where it has none."""
    import rasterio

    _ortho, relief = elevation_paths(chunk, entry)
    if not relief.exists():
        return None
    with rasterio.open(relief) as src:
        val = float(next(src.sample([(float(u), float(v))]))[0])
        if (src.nodata is not None and val == src.nodata) or not np.isfinite(val):
            return None
    return val


def world_of(entry: dict, u: float, v: float, d: float | None = None, chunk: Chunk | None = None) -> np.ndarray | None:
    """The world point (the chunk's CRS) of the position ``(u, v)`` of a drawing on the elevation ``entry`` at the
    depth ``d``; ``d`` None: on the surface, the depth read from the relief of the elevation (``chunk`` is needed
    then) - None where the relief has no value."""
    if d is None:
        if chunk is None:
            raise ValueError("the depth of the surface is read from the relief of the elevation: give the chunk")
        d = relief_at(chunk, entry, u, v)
        if d is None:
            return None
    frame = frame_from_dict(entry["frame"])
    return frame.point(np.array([float(u)]), np.array([float(v)]), np.array([float(d)]))[0]


def frame_drawings(chunk: Chunk, eid: str) -> dict:
    """The drawings kept with the elevation ``eid`` (``chunk.meta["drawings"]["frames"][eid]``): ``units``,
    ``quote``, ``points``, ``lines`` (records as the window stores them, in ``(u, v)``) and ``stones``
    (``{eid: [WKT]}``)."""
    return dict(((chunk.meta.get("drawings") or {}).get("frames") or {}).get(eid) or {})


def _safe_stem(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name)) or "prospetto"


def _drawing_items(chunk: Chunk, entry: dict, data: dict | None = None) -> dict:
    """The drawings of ``entry`` as shapely geometries in ``(u, v)``: ``units`` [(geometry, record)], ``quote``
    [(point, record)], ``points`` [(point, record)], ``lines`` [(line, record)] (break lines,
    :mod:`..archaeo.breaklines`), ``stones`` [polygon] (those of this elevation), ``profile`` [line arrays]. ``data``: the drawings (as :func:`frame_drawings` gives them) when not those the chunk keeps."""
    from shapely import wkt
    from shapely.geometry import LineString, Point

    from ..archaeo import breaklines

    data = frame_drawings(chunk, entry["id"]) if data is None else data
    units = []
    for rec in data.get("units") or []:
        try:
            units.append((wkt.loads(rec["wkt"]), rec))
        except Exception:  # noqa: BLE001 - a broken record is left out of the drawing
            continue
    quote = [(Point(float(q["u"]), float(q["v"])), q) for q in data.get("quote") or [] if "u" in q and "v" in q]
    points = [(Point(float(q.get("u", q.get("x"))), float(q.get("v", q.get("y")))), q) for q in data.get("points") or []
              if ("u" in q and "v" in q) or ("x" in q and "y" in q)]
    stones = []
    for r in (data.get("stones") or {}).get(entry["id"]) or []:
        try:
            stones.append(wkt.loads(r))
        except Exception:  # noqa: BLE001
            continue
    lines = [(LineString([(float(c[0]), float(c[1])) for c in r["coords"]]), r) for r in data.get("lines") or []
             if breaklines.valid(r)]
    return {"units": units, "quote": quote, "points": points, "lines": lines, "stones": stones,
            "profile": read_profile(chunk, entry) if entry.get("kind") == "section" else []}


def _on_plane(entry: dict):
    """The function that places a ``(u, v)`` geometry on the plane of ``entry`` (``d`` = 0), in 3D world coordinates."""
    import shapely

    frame = frame_from_dict(entry["frame"])
    return lambda geom: shapely.transform(shapely.force_3d(geom), lambda c: frame.point(c[:, 0], c[:, 1],
                                                                                       np.zeros(len(c))), include_z=True)


UNIT_COLUMNS = {"us": "TEXT", "area": "TEXT", "tipo": "TEXT", "sito": "TEXT", "ppg_id": "TEXT"}
QUOTE_COLUMNS = {"quota_q": "REAL", "us_q": "TEXT", "area_q": "TEXT", "sito_q": "TEXT", "unita_misu_q": "TEXT",
                 "ppg_id": "TEXT"}


def world_tables(chunk: Chunk, entry: dict, data: dict | None = None) -> dict:
    """The drawings of the elevation ``entry`` placed on the survey, 3D in the CRS the elevation was made in:
    ``{name: (geometry type, {column: SQL type}, [(geometry with Z, values)])}`` for ``unita`` (MULTIPOLYGON, on the
    plane), ``quote`` (POINT, the point of the surface), ``punti`` (POINT), ``pietre`` (POLYGON), ``linee``
    (LINESTRING, the break lines on the plane) and, for a section,
    ``profilo`` (LINESTRING) - written with Z by :func:`write_world_tables` (the GeoPackage of the export, the live
    GeoPackage of QGIS and RustGIS)."""
    from shapely.geometry import LineString, MultiPolygon, Point

    items = _drawing_items(chunk, entry, data)
    place = _on_plane(entry)
    units = []
    for geom, rec in items["units"]:
        multi = geom if geom.geom_type == "MultiPolygon" else MultiPolygon([geom]) if geom.geom_type == "Polygon" \
            else None
        if multi is not None:
            units.append((place(multi), {"us": rec.get("us"), "area": rec.get("area"), "tipo": rec.get("tipo"),
                                         "sito": rec.get("sito"), "ppg_id": (rec.get("attributes") or {}).get("ppg_id")}))
    quote = []
    for p, rec in items["quote"]:
        world = Point(rec["x"], rec["y"], rec["quota_q"]) if rec.get("quota_q") not in (None, "") \
            and "x" in rec and "y" in rec else place(p)
        quote.append((world, {c: rec.get(c) for c in QUOTE_COLUMNS}))
    points = []
    for p, rec in items["points"]:
        world = Point(rec["x"], rec["y"], rec["z_superficie"]) if "z_superficie" in rec else place(p)
        points.append((world, {"label": rec.get("label")}))
    stones = [(place(st), {"n": i + 1, "area_m2": round(st.area, 5)}) for i, st in enumerate(items["stones"])
              if st.geom_type == "Polygon"]
    lines = [(place(ln), {"ppg_id": rec.get("id"), "kind": rec["kind"]}) for ln, rec in items["lines"]]
    out = {"unita": ("MULTIPOLYGON", dict(UNIT_COLUMNS), units),
           "quote": ("POINT", dict(QUOTE_COLUMNS), quote),
           "punti": ("POINT", {"label": "TEXT"}, points),
           "pietre": ("POLYGON", {"n": "INTEGER", "area_m2": "REAL"}, stones),
           "linee": ("LINESTRING", {"ppg_id": "TEXT", "kind": "TEXT"}, lines)}
    if entry.get("kind") == "section":
        out["profilo"] = ("LINESTRING", {"n": "INTEGER"},
                          [(place(LineString(line)), {"n": i + 1}) for i, line in enumerate(items["profile"])
                           if len(line) > 1])
    return out


def write_world_tables(con, tables: dict, srs_id: int, prefix: str = "") -> list[str]:
    """``tables`` (:func:`world_tables`) into an open GeoPackage connection, inside the caller's transaction, each named
    ``prefix + name``, with Z; their content replaced. Returns the names written."""
    from ..io import gpkg

    written = []
    for name, (gtype, columns, rows) in tables.items():
        table = prefix + name
        cols = gpkg.ensure_table(con, table, gtype, columns, srs_id, recreate=True, z=True)
        gpkg.replace_rows(con, table, cols, rows, srs_id, z=True)
        written.append(table)
    return written


def _unit_layer(rec: dict) -> str:
    return "USM" if str(rec.get("tipo") or "").upper().startswith("USM") else "US"


def _unit_label(rec: dict) -> str:
    return f"{rec.get('tipo') or 'US'} {rec.get('us') or '?'}"


def _rings(geom):
    for part in getattr(geom, "geoms", [geom]):
        if part.geom_type == "Polygon":
            yield np.asarray(part.exterior.coords)[:, :2]
            for hole in part.interiors:
                yield np.asarray(hole.coords)[:, :2]


def _write_drawings_dxf(items: dict, entry: dict, path: Path) -> Path:
    import ezdxf

    dxf = ezdxf.new("R2010")
    dxf.units = ezdxf.units.M
    msp = dxf.modelspace()
    for name, colour in (("US", 3), ("USM", 1), ("QUOTE", 2), ("PIETRE", 7), ("PUNTI", 6), ("PROFILO", 1),
                         ("LINEE", 30)):
        dxf.layers.add(name, color=colour)
    height = max(0.02, 4 * float(entry.get("resolution") or 0.005))
    for geom, rec in items["units"]:
        layer = _unit_layer(rec)
        for ring in _rings(geom):
            msp.add_lwpolyline([tuple(map(float, q)) for q in ring], close=True, dxfattribs={"layer": layer})
        c = geom.representative_point()
        msp.add_text(_unit_label(rec), height=height, dxfattribs={"layer": layer}).set_placement((c.x, c.y))
    for p, rec in items["quote"]:
        msp.add_point((p.x, p.y), dxfattribs={"layer": "QUOTE"})
        z = rec.get("quota_q")
        label = f"{float(z):.2f}" if z not in (None, "") else "?"
        msp.add_text(label, height=height, dxfattribs={"layer": "QUOTE"}).set_placement((p.x, p.y))
    for p, rec in items["points"]:
        msp.add_point((p.x, p.y), dxfattribs={"layer": "PUNTI"})
    for s in items["stones"]:
        for ring in _rings(s):
            msp.add_lwpolyline([tuple(map(float, q)) for q in ring], close=True, dxfattribs={"layer": "PIETRE"})
    for line in items["profile"]:
        msp.add_lwpolyline([tuple(map(float, q)) for q in line], dxfattribs={"layer": "PROFILO"})
    from ..archaeo.breaklines import DXF_COLOURS

    for ln, rec in items["lines"]:
        msp.add_lwpolyline([tuple(map(float, q)) for q in ln.coords],
                           dxfattribs={"layer": "LINEE", "color": DXF_COLOURS[rec["kind"]]})
    dxf.saveas(path)
    return path


def _write_drawings_svg(chunk: Chunk, items: dict, entry: dict, path: Path, scale: int | None) -> Path:
    """The drawings at 1:``scale`` (millimetres on paper) over the orthophoto of the elevation, a group per layer."""
    import base64
    import io

    import rasterio
    from PIL import Image

    from ..archaeo.sections import drawing_scale

    ortho, _relief = elevation_paths(chunk, entry)
    u0, v0, u1, v1 = (float(b) for b in entry.get("bounds") or (0.0, 0.0, 1.0, 1.0))
    image = ""
    if ortho.exists():
        with rasterio.open(ortho) as src:
            b = src.bounds
            u0, v0, u1, v1 = min(u0, b.left), min(v0, b.bottom), max(u1, b.right), max(v1, b.top)
            f = max(1.0, src.width / 4000, src.height / 4000)
            arr = src.read(out_shape=(src.count, max(1, int(src.height / f)), max(1, int(src.width / f))))
        rgba = np.moveaxis(arr[:4] if arr.shape[0] >= 4 else arr[:3], 0, -1).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(rgba).save(buf, "PNG")
        image = (b.left, b.top, b.right - b.left, b.top - b.bottom, base64.b64encode(buf.getvalue()).decode("ascii"))
    scale = int(scale or drawing_scale(u1 - u0, v1 - v0))
    k = 1000.0 / scale  # millimetres on paper for a metre
    margin = 10.0
    width, height = (u1 - u0) * k + 2 * margin, (v1 - v0) * k + 2 * margin

    def xy(u, v):
        return (u - u0) * k + margin, (v1 - v) * k + margin

    def path_of(rings, close=True):
        parts = []
        for ring in rings:
            pts = " L ".join("{:.2f} {:.2f}".format(*xy(float(q[0]), float(q[1]))) for q in ring)
            parts.append("M " + pts + (" Z" if close else ""))
        return " ".join(parts)

    from xml.sax.saxutils import escape

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
           f'width="{width:.1f}mm" height="{height:.1f}mm" viewBox="0 0 {width:.2f} {height:.2f}">',
           f"<title>{escape(str(entry.get('name') or ''))} 1:{scale}</title>"]
    if image:
        left, top, w, h, data = image
        x, y = xy(left, top)
        out.append(f'<image id="ortofoto" x="{x:.2f}" y="{y:.2f}" width="{w * k:.2f}" height="{h * k:.2f}" '
                   f'preserveAspectRatio="none" xlink:href="data:image/png;base64,{data}"/>')
    text = 2.5  # millimetres on paper
    for layer, colour in (("US", "#1b7f3b"), ("USM", "#c0392b")):
        out.append(f'<g id="{layer}" fill="none" stroke="{colour}" stroke-width="0.35" stroke-linejoin="round">')
        for geom, rec in items["units"]:
            if _unit_layer(rec) != layer:
                continue
            out.append(f'<path d="{path_of(_rings(geom))}"/>')
            c = geom.representative_point()
            x, y = xy(c.x, c.y)
            out.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{text}" fill="{colour}" stroke="none" '
                       f'text-anchor="middle">{escape(_unit_label(rec))}</text>')
        out.append("</g>")
    out.append('<g id="PIETRE" fill="none" stroke="#000000" stroke-width="0.25">')
    out += [f'<path d="{path_of(_rings(s))}"/>' for s in items["stones"]]
    out.append("</g>")
    out.append('<g id="PROFILO" fill="none" stroke="#ff2d2d" stroke-width="0.5">')
    out += [f'<path d="{path_of([line], close=False)}"/>' for line in items["profile"]]
    out.append("</g>")
    from ..archaeo.breaklines import COLOURS

    out.append('<g id="LINEE" fill="none" stroke-width="0.4">')
    out += [f'<path d="{path_of([np.asarray(ln.coords)], close=False)}" stroke="{COLOURS[rec["kind"]]}"'
            + (' stroke-dasharray="2 1"' if rec["kind"] == "soft" else "") + f' class="{rec["kind"]}"/>'
            for ln, rec in items["lines"]]
    out.append("</g>")
    out.append('<g id="PUNTI" fill="#ff5d8f" stroke="#000000" stroke-width="0.15">')
    for p, rec in items["points"]:
        x, y = xy(p.x, p.y)
        out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="0.6"/>')
        if rec.get("label"):
            out.append(f'<text x="{x + 1.0:.2f}" y="{y - 0.8:.2f}" font-size="{text}" fill="#000000" '
                       f'stroke="none">{escape(str(rec["label"]))}</text>')
    out.append("</g>")
    out.append('<g id="QUOTE" fill="#ffd166" stroke="#000000" stroke-width="0.15">')
    for p, rec in items["quote"]:
        x, y = xy(p.x, p.y)
        z = rec.get("quota_q")
        label = f"{float(z):.2f}" if z not in (None, "") else "?"
        out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="0.8"/>')
        out.append(f'<text x="{x + 1.2:.2f}" y="{y - 1.0:.2f}" font-size="{text}" fill="#000000" '
                   f'stroke="none">{escape(label)}</text>')
    out.append("</g>")
    out.append("</svg>")
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def _write_drawings_png(chunk: Chunk, items: dict, entry: dict, path: Path, max_side: int = 2008) -> Path:
    """The drawings over the orthophoto of the elevation as a picture (the thumbnail of its media in PyArchInit)."""
    import rasterio
    from PIL import Image, ImageDraw

    ortho, _relief = elevation_paths(chunk, entry)
    if ortho.exists():
        with rasterio.open(ortho) as src:
            b = src.bounds
            f = max(1.0, src.width / max_side, src.height / max_side)
            arr = src.read(out_shape=(src.count, max(1, int(src.height / f)), max(1, int(src.width / f))))
        rgb = np.moveaxis(arr[:3], 0, -1).astype(np.uint8)
        if arr.shape[0] >= 4:
            rgb = np.where(arr[3][..., None] > 0, rgb, 255).astype(np.uint8)
        image = Image.fromarray(rgb)
        left, top, sx, sy = b.left, b.top, image.width / (b.right - b.left), image.height / (b.top - b.bottom)
    else:
        u0, v0, u1, v1 = (float(x) for x in entry.get("bounds") or (0.0, 0.0, 1.0, 1.0))
        image = Image.new("RGB", (800, max(1, int(800 * (v1 - v0) / max(u1 - u0, 1e-9)))), "white")
        left, top, sx, sy = u0, v1, image.width / max(u1 - u0, 1e-9), image.height / max(v1 - v0, 1e-9)
    draw = ImageDraw.Draw(image)

    def px(ring):
        return [((float(q[0]) - left) * sx, (top - float(q[1])) * sy) for q in ring]

    for geom, rec in items["units"]:
        colour = (192, 57, 43) if _unit_layer(rec) == "USM" else (27, 127, 59)
        for ring in _rings(geom):
            draw.line(px(ring), fill=colour, width=3)
    for st in items["stones"]:
        for ring in _rings(st):
            draw.line(px(ring), fill=(0, 0, 0), width=2)
    for line in items["profile"]:
        draw.line(px(line), fill=(255, 45, 45), width=3)
    for ln, _rec in items["lines"]:
        draw.line(px(np.asarray(ln.coords)), fill=(255, 127, 14), width=3)
    for p, _rec in items["quote"] + items["points"]:
        (x, y), = px([(p.x, p.y)])
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(255, 209, 102), outline=(0, 0, 0))
    image.save(path)
    return path


def _write_drawings_world_gpkg(chunk: Chunk, entry: dict, path: Path, data: dict | None = None) -> Path:
    """GeoPackage of the drawings placed on the survey (:func:`world_tables`), 3D in the CRS of the elevation: QGIS 3D
    and RustGIS put them where they are."""
    from ..io import gpkg

    srs = gpkg.srs_of(entry.get("crs") or chunk.crs)
    gpkg.refuse_foreign(path)
    if path.exists():
        path.unlink()
    con = gpkg.connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        gpkg.create(con, srs)
        write_world_tables(con, world_tables(chunk, entry, data), srs[0])
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return path


def _write_drawings_gpkg(chunk: Chunk, items: dict, entry: dict, path: Path) -> Path:
    """GeoPackage of the elevation unfolded: the table ``frame`` (the elevation and its plane) and the drawings in
    ``(u, v)`` metres (no CRS: QGIS and RustGIS load them as a plan of the façade, over the orthophoto of the
    elevation, whose GeoTIFF is in the same frame), each row with ``geom_world``, the WKT of the drawing in the CRS of
    the elevation (on the plane, ``d`` = 0; a spot height on the surface)."""
    from shapely.geometry import Point

    from ..io import gpkg

    on_plane = _on_plane(entry)

    gpkg.refuse_foreign(path)
    if path.exists():
        path.unlink()
    con = gpkg.connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        gpkg.create(con, (-1, "", ""))
        con.execute("CREATE TABLE frame (fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, id TEXT, name TEXT, "
                    "kind TEXT, plane TEXT, v_is_height INTEGER, resolution REAL, crs TEXT)")
        con.execute("INSERT INTO gpkg_contents (table_name, data_type, identifier) VALUES ('frame', 'attributes', "
                    "'frame')")
        con.execute("INSERT INTO frame (id, name, kind, plane, v_is_height, resolution, crs) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (entry["id"], entry.get("name"), entry.get("kind") or "elevation", json.dumps(entry["frame"]),
                     int(bool(entry.get("v_is_height"))), entry.get("resolution"), entry.get("crs") or chunk.crs))
        unit_cols = {"us": "TEXT", "area": "TEXT", "tipo": "TEXT", "sito": "TEXT", "ppg_id": "TEXT",
                     "geom_world": "TEXT"}
        cols = gpkg.ensure_table(con, "unita", "MULTIPOLYGON", unit_cols, -1)
        rows = []
        for geom, rec in items["units"]:
            from shapely.geometry import MultiPolygon

            multi = geom if geom.geom_type == "MultiPolygon" else MultiPolygon([geom]) \
                if geom.geom_type == "Polygon" else None
            if multi is None:
                continue
            rows.append((multi, {"us": rec.get("us"), "area": rec.get("area"), "tipo": rec.get("tipo"),
                                 "sito": rec.get("sito"), "ppg_id": (rec.get("attributes") or {}).get("ppg_id"),
                                 "geom_world": on_plane(geom).wkt}))
        gpkg.replace_rows(con, "unita", cols, rows, -1)
        quote_cols = {"quota_q": "REAL", "us_q": "TEXT", "area_q": "TEXT", "sito_q": "TEXT", "unita_misu_q": "TEXT",
                      "ppg_id": "TEXT", "geom_world": "TEXT"}
        cols = gpkg.ensure_table(con, "quote", "POINT", quote_cols, -1)
        rows = []
        for p, rec in items["quote"]:
            world = Point(rec["x"], rec["y"], rec["quota_q"]) if rec.get("quota_q") not in (None, "") \
                and "x" in rec and "y" in rec else on_plane(p)
            rows.append((p, {**{c: rec.get(c) for c in quote_cols if c != "geom_world"}, "geom_world": world.wkt}))
        gpkg.replace_rows(con, "quote", cols, rows, -1)
        cols = gpkg.ensure_table(con, "punti", "POINT", {"label": "TEXT", "geom_world": "TEXT"}, -1)
        gpkg.replace_rows(con, "punti", cols, [(p, {"label": rec.get("label"), "geom_world": (
            Point(rec["x"], rec["y"], rec["z_superficie"]) if "z_superficie" in rec else on_plane(p)).wkt})
                                               for p, rec in items["points"]], -1)
        cols = gpkg.ensure_table(con, "pietre", "POLYGON", {"n": "INTEGER", "area_m2": "REAL", "geom_world": "TEXT"},
                                 -1)
        gpkg.replace_rows(con, "pietre", cols, [(s, {"n": i + 1, "area_m2": round(s.area, 5),
                                                     "geom_world": on_plane(s).wkt})
                                                for i, s in enumerate(items["stones"]) if s.geom_type == "Polygon"], -1)
        cols = gpkg.ensure_table(con, "linee", "LINESTRING", {"ppg_id": "TEXT", "kind": "TEXT", "geom_world": "TEXT"},
                                 -1)
        gpkg.replace_rows(con, "linee", cols, [(ln, {"ppg_id": rec.get("id"), "kind": rec["kind"],
                                                     "geom_world": on_plane(ln).wkt}) for ln, rec in items["lines"]], -1)
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return path


def export_drawings(chunk: Chunk, entry: dict, folder, scale: int | None = None, stem: str | None = None,
                    raster: bool = False) -> dict:
    """The drawings made on the elevation ``entry`` (:func:`frame_drawings`) into ``folder``:

    * ``<stem>_disegni.dxf`` in ``(u, v)`` metres (layers ``US``, ``USM``, ``QUOTE``, ``PIETRE``, ``PUNTI``, ``LINEE``
      and, for a section, ``PROFILO``);
    * ``<stem>_disegni.svg`` at 1:``scale`` (default the most detailed standard scale that fits) over the orthophoto,
      and ``<stem>_disegni.png``, the same as a picture;
    * ``<stem>_disegni.gpkg``, the elevation unfolded (:func:`_write_drawings_gpkg`, ``(u, v)``, no CRS), with the
      orthophoto beside it as ``<stem>_ortofoto.tif`` when ``raster``;
    * ``<stem>_disegni_mondo.gpkg``, the drawings in 3D on the survey (:func:`_write_drawings_world_gpkg`).

    ``stem``: the name of the elevation by default. Returns ``{"dxf", "svg", "png", "gpkg", "gpkg_world"}`` (and
    ``"ortho"`` with ``raster``)."""
    import shutil

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = stem or _safe_stem(entry.get("name") or entry["id"])
    items = _drawing_items(chunk, entry)
    out = {"dxf": _write_drawings_dxf(items, entry, folder / f"{stem}_disegni.dxf"),
           "svg": _write_drawings_svg(chunk, items, entry, folder / f"{stem}_disegni.svg", scale),
           "png": _write_drawings_png(chunk, items, entry, folder / f"{stem}_disegni.png"),
           "gpkg": _write_drawings_gpkg(chunk, items, entry, folder / f"{stem}_disegni.gpkg"),
           "gpkg_world": _write_drawings_world_gpkg(chunk, entry, folder / f"{stem}_disegni_mondo.gpkg")}
    ortho, _relief = elevation_paths(chunk, entry)
    if raster and ortho.exists():
        out["ortho"] = folder / f"{stem}_ortofoto.tif"
        if not out["ortho"].exists():
            shutil.copy2(ortho, out["ortho"])
    return out
