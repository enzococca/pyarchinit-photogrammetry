"""The reconstruction region: an oriented box, in world coordinates (the chunk CRS), outside which
the surface is left out (Metashape's *Region*). With a region the depth maps have no depth outside
it, the dense cloud and the mesh are cut to it (a Poisson surface no longer balloons beyond the
object), so the DEM and the orthomosaic cover it only; the trench, not the field around it; the
pot, not the table.

The box is ``center`` (world), ``size`` (lengths along its axes) and ``rotation`` (its axes as the
columns of a 3x3 matrix, world frame); it is kept in ``chunk.meta["region"]``.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..core.chunk import Chunk


@dataclass
class Region:
    center: np.ndarray
    size: np.ndarray
    rotation: np.ndarray

    def __post_init__(self):
        self.center = np.asarray(self.center, np.float64).reshape(3)
        self.size = np.abs(np.asarray(self.size, np.float64).reshape(3))
        r = np.asarray(self.rotation, np.float64).reshape(3, 3)
        u, _s, vt = np.linalg.svd(r)  # the nearest rotation
        r = u @ vt
        if np.linalg.det(r) < 0:
            r[:, 2] = -r[:, 2]
        self.rotation = r

    def local(self, pts: np.ndarray) -> np.ndarray:
        """World points in the frame of the box (centre at 0, axes as its edges)."""
        return (np.asarray(pts, np.float64).reshape(-1, 3) - self.center) @ self.rotation

    def contains(self, pts: np.ndarray) -> np.ndarray:
        return np.all(np.abs(self.local(pts)) <= self.size / 2, axis=1)

    def corners(self) -> np.ndarray:
        """The 8 corners (world): bit 0 along the first axis, bit 1 the second, bit 2 the third."""
        signs = np.array([[(i >> k & 1) * 2 - 1 for k in range(3)] for i in range(8)], np.float64)
        return self.center + (signs * self.size / 2) @ self.rotation.T

    EDGES = [(0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7)]

    def footprint(self) -> tuple[float, float, float, float]:
        c = self.corners()
        return float(c[:, 0].min()), float(c[:, 1].min()), float(c[:, 0].max()), float(c[:, 1].max())

    def to_dict(self) -> dict:
        return {"center": self.center.tolist(), "size": self.size.tolist(), "rotation": self.rotation.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "Region":
        return cls(d["center"], d["size"], d["rotation"])


def rotation_z(angle_deg: float) -> np.ndarray:
    a = np.radians(angle_deg)
    return np.array([[np.cos(a), -np.sin(a), 0.0], [np.sin(a), np.cos(a), 0.0], [0.0, 0.0, 1.0]])


def get_region(chunk: Chunk) -> Region | None:
    d = (getattr(chunk, "meta", None) or {}).get("region")
    return Region.from_dict(d) if d else None


def set_region(chunk: Chunk, region: Region | None) -> None:
    if region is None:
        chunk.meta.pop("region", None)
    else:
        chunk.meta["region"] = region.to_dict()


def _world_points(chunk: Chunk, source: str) -> np.ndarray:
    pc = chunk.point_cloud if source == "point_cloud" else None
    if pc is None or not len(pc):
        pc = chunk.tie_points
    if pc is None or not len(pc):
        raise RuntimeError("align the photos first: the region is set around the tie points")
    return chunk.to_world(pc.xyz)


def reset_region(chunk: Chunk, source: str = "tie_points", margin: float = 0.05, angle_deg: float = 0.0,
                 keep: float = 98.0) -> Region:
    """A box around the points of ``source`` (tie points, or the dense cloud), the ``keep``
    percent of them in the middle, widened by ``margin`` of its size; turned by ``angle_deg``
    about the vertical. Stored and returned."""
    w = _world_points(chunk, source)
    rot = rotation_z(angle_deg)
    loc = w @ rot
    lo, hi = np.percentile(loc, (100 - keep) / 2, axis=0), np.percentile(loc, 100 - (100 - keep) / 2, axis=0)
    size = (hi - lo) * (1 + 2 * margin)
    region = Region(rot @ ((lo + hi) / 2), np.maximum(size, 1e-6), rot)
    set_region(chunk, region)
    return region


def region_from_box(chunk: Chunk, box, zmin: float | None = None, zmax: float | None = None) -> Region:
    """A box over a plan rectangle (xmin, ymin, xmax, ymax), from ``zmin`` to ``zmax`` (default:
    the heights of the tie points inside, widened by 10 %). Stored and returned."""
    x0, y0, x1, y1 = (float(v) for v in box)
    if zmin is None or zmax is None:
        w = _world_points(chunk, "point_cloud")
        inside = (w[:, 0] >= x0) & (w[:, 0] <= x1) & (w[:, 1] >= y0) & (w[:, 1] <= y1)
        z = w[inside, 2] if inside.any() else w[:, 2]
        lo, hi = np.percentile(z, 0.5), np.percentile(z, 99.5)
        pad = 0.1 * max(hi - lo, 1e-3)
        zmin = lo - pad if zmin is None else zmin
        zmax = hi + pad if zmax is None else zmax
    region = Region([(x0 + x1) / 2, (y0 + y1) / 2, (zmin + zmax) / 2], [x1 - x0, y1 - y0, zmax - zmin], np.eye(3))
    set_region(chunk, region)
    return region


def inside_internal(chunk: Chunk, pts: np.ndarray, region: Region | None = None) -> np.ndarray:
    """Which internal-frame points lie inside the region (all of them without one)."""
    region = region or get_region(chunk)
    pts = np.asarray(pts, np.float64).reshape(-1, 3)
    if region is None:
        return np.ones(len(pts), bool)
    return region.contains(chunk.to_world(pts))


def depth_mask(chunk: Chunk, depth: np.ndarray, K: np.ndarray, cam_from_world: np.ndarray,
               region: Region | None = None) -> np.ndarray | None:
    """True for the pixels of a depth map (COLMAP's MVS convention: pixel (col, row) is the ray
    ``K^-1 (col, row, 1)``) whose surface lies inside the region; None without a region."""
    region = region or get_region(chunk)
    if region is None:
        return None
    h, w = depth.shape
    rows, cols = np.nonzero(depth > 0)
    out = np.zeros((h, w), bool)
    if not len(rows):
        return out
    z = depth[rows, cols].astype(np.float64)
    cam = np.column_stack([(cols - K[0, 2]) / K[0, 0] * z, (rows - K[1, 2]) / K[1, 1] * z, z])
    rot, t = cam_from_world[:3, :3], cam_from_world[:3, 3]
    internal = (cam - t) @ rot  # R^T (p - t)
    out[rows, cols] = region.contains(chunk.to_world(internal))
    return out


def crop_mesh(chunk: Chunk, mesh, region: Region | None = None):
    """A trimesh mesh (internal frame) without the faces whose centre lies outside the region."""
    region = region or get_region(chunk)
    if region is None or not len(mesh.faces):
        return mesh
    keep = inside_internal(chunk, np.asarray(mesh.triangles_center), region)
    if keep.all():
        return mesh
    mesh = mesh.copy()
    mesh.update_faces(keep)
    mesh.remove_unreferenced_vertices()
    return mesh
