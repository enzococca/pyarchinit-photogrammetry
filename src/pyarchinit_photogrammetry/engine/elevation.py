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
from ..core.progress import Progress


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


def cylinder_from_circle(chunk: Chunk, points, inside: bool = True) -> Cylinder:
    """The vertical cylinder through a circle fitted to plan points (three or more on the rim of a
    well or around a tower, chunk CRS)."""
    q = np.asarray(points, np.float64)[:, :2]
    if len(q) < 3:
        raise ValueError("three points at least")
    a = np.column_stack([2 * q, np.ones(len(q))])
    sol = np.linalg.lstsq(a, (q ** 2).sum(axis=1), rcond=None)[0]
    cx, cy = sol[0], sol[1]
    r = float(np.sqrt(sol[2] + cx * cx + cy * cy))
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


def build_elevation(chunk: Chunk, frame, resolution: float | None = None, name: str | None = None,
                    depth: tuple[float, float] | None = None, blending: str = "mosaic",
                    colour_correction: bool | None = None, ghosting: bool = False, fill_holes: bool = True,
                    progress: Progress | None = None, workers: int | None = None, max_angle: float = 80.0) -> dict:
    """Orthophoto (RGBA) and DEM (depth ``d`` towards the viewer, float32) of the model on ``frame``,
    written in ``<chunk data>/elevations/<id>/`` (GeoTIFF in frame metres, no CRS; the frame in the
    tags and in ``frame.json``) and listed in ``chunk.meta["elevations"]``. Returns the entry."""
    import rasterio
    from rasterio.enums import ColorInterp, Resampling
    from rasterio.transform import from_origin
    from rasterio.windows import Window

    from .dem import estimate_gsd
    from .ortho import _context, _run_tiles, correction_for
    from .visibility import raycaster_for_chunk, readable_cameras

    progress = progress or Progress()
    if chunk.product_path("model") is None:
        raise RuntimeError("build the model first: an elevation is made on the model")
    progress(0.0, "Checking the photos")
    cams, _bad = readable_cameras(chunk, True)
    resolution = float(resolution or estimate_gsd(chunk))
    correction = correction_for(chunk, colour_correction, progress.sub(0.0, 0.02))
    raycaster = raycaster_for_chunk(chunk)
    lock = threading.Lock()
    surf = FrameSurface(chunk, raycaster, frame, resolution, lock, depth)
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
    (folder / "frame.json").write_text(json.dumps({"frame": frame.to_dict(), "depth": [surf.dmin, surf.dmax],
                                                   "resolution": resolution}, indent=1))
    entry = {"id": eid, "name": name or ("Prospetto" if frame.kind == "plane" else "Sviluppo cilindrico"),
             "frame": frame.to_dict(), "depth": [surf.dmin, surf.dmax], "resolution": resolution,
             "orthophoto": str(ortho_path.relative_to(chunk.data_dir)), "relief": str(dem_path.relative_to(chunk.data_dir)),
             "size": [w, h], "blending": blending, "colour_correction": correction is not None}
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
