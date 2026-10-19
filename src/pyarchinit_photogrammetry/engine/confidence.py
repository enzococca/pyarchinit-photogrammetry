"""Confidence of the dense points and of the vertices of the model.

As in Metashape, the confidence of a point is the number of depth maps that agree on it: a
depth map agrees where its surface lies within ``tol`` of the distance of the point from the
camera (1 %, the depth tolerance of the fusion that made the cloud). It is computed for any
cloud - fused, cropped, cleaned, merged, imported - and for the vertices of a model, as long as
the depth maps of the chunk exist. Low values mark what few photos saw consistently: noise,
reflections, the sky, the edges of the survey; 0 on a model marks surfaces no depth map saw
(a hole the mesh closed). With a high overlap (drone flights) good surfaces reach 20-40.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .. import _native
from ..core.chunk import Chunk
from ..core.pointcloud import PointCloud
from ..core.progress import Progress

# ranges of the legend in the 3D view and of the statistics
BINS = [(0, 0), (1, 1), (2, 2), (3, 4), (5, 9), (10, 19), (20, 255)]


def bin_label(lo: int, hi: int) -> str:
    if lo == hi == 0:
        return "0 (nessuna mappa)"
    if lo == hi:
        return f"{lo} {'mappa' if lo == 1 else 'mappe'}"
    return f"{lo} o piu' mappe" if hi >= 255 else f"{lo}-{hi} mappe"


def histogram(confidence: np.ndarray) -> dict[int, int]:
    """Points per confidence value."""
    counts = np.bincount(np.asarray(confidence, np.uint8), minlength=256)
    return {int(c): int(n) for c, n in enumerate(counts) if n}


def _pinhole(cam) -> tuple[float, float, float, float]:
    p = np.asarray(cam.params, float)
    name = cam.model.name if hasattr(cam.model, "name") else str(cam.model)
    if "SIMPLE" in name or len(p) == 3:
        return p[0], p[0], p[1], p[2]
    return p[0], p[1], p[2], p[3]


def depth_maps(chunk: Chunk) -> list[dict]:
    """The depth maps of the chunk: ``{"name", "path", "K" (fx, fy, cx, cy), "pose" (3x4
    cam_from_world, internal frame), "size" (width, height of the camera)}``."""
    import pycolmap

    ws = chunk.data_dir / "dense"
    info = chunk.meta.get("dense") or {}
    if info.get("backend") == "openmvs":
        raise RuntimeError("the depth maps of OpenMVS are not read here: build them with the cpu or colmap backend")
    sparse = ws / "sparse"
    if not sparse.exists() or not (ws / "stereo" / "depth_maps").exists():
        raise RuntimeError("no depth maps: build them first (dense cloud, or depth maps only)")
    rec = pycolmap.Reconstruction()
    rec.read(str(sparse))
    out = []
    for img in rec.images.values():
        for kind in ("geometric", "photometric"):
            path = ws / "stereo" / "depth_maps" / f"{img.name}.{kind}.bin"
            if path.exists():
                break
        else:
            continue
        cam = rec.cameras[img.camera_id]
        out.append({"name": img.name, "path": path, "K": _pinhole(cam), "pose": np.asarray(img.cam_from_world().matrix()),
                     "size": (int(cam.width), int(cam.height))})
    if not out:
        raise RuntimeError("no depth maps: build them first (dense cloud, or depth maps only)")
    return out


class _Grid:
    """Points sorted by a coarse 3D cell, to find quickly the ones inside a box."""

    def __init__(self, points: np.ndarray, cells: int = 64):
        self.lo = points.min(axis=0)
        span = np.maximum(points.max(axis=0) - self.lo, 1e-9)
        self.size = float(span.max()) / cells
        self.dims = np.minimum(np.floor(span / self.size).astype(np.int64) + 1, cells + 1)
        q = np.minimum(np.floor((points - self.lo) / self.size).astype(np.int64), self.dims - 1)
        key = q[:, 0] + self.dims[0] * (q[:, 1] + self.dims[1] * q[:, 2])
        self.order = np.argsort(key, kind="stable")
        ncell = int(np.prod(self.dims))
        skey = key[self.order]
        self.start = np.searchsorted(skey, np.arange(ncell), side="left")
        self.end = np.searchsorted(skey, np.arange(ncell), side="right")

    def inside(self, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
        a = np.clip(np.floor((lo - self.lo) / self.size).astype(np.int64), 0, self.dims - 1)
        b = np.clip(np.floor((hi - self.lo) / self.size).astype(np.int64), 0, self.dims - 1)
        if np.any(hi < self.lo) or np.any(lo > self.lo + self.size * self.dims):
            return np.zeros(0, np.int64)
        ix, iy, iz = np.meshgrid(np.arange(a[0], b[0] + 1), np.arange(a[1], b[1] + 1), np.arange(a[2], b[2] + 1),
                                 indexing="ij")
        cells = (ix + self.dims[0] * (iy + self.dims[1] * iz)).ravel()
        s, e = self.start[cells], self.end[cells]
        keep = e > s
        s, n = s[keep], (e - s)[keep]
        if not len(n):
            return np.zeros(0, np.int64)
        offs = np.repeat(s - np.cumsum(np.r_[0, n[:-1]]), n) + np.arange(int(n.sum()))
        return self.order[offs]


def _agreeing(entry: dict, points: np.ndarray, grid: _Grid, tol: float, abs_tol: np.ndarray | None = None) -> np.ndarray:
    """Indices of the points on which one depth map agrees (within ``tol`` x the distance, or
    the point's own ``abs_tol`` when larger)."""
    from .dense import read_colmap_array

    depth = read_colmap_array(entry["path"])
    if depth.ndim != 2:
        depth = depth[..., 0]
    h, w = depth.shape
    fx, fy, cx, cy = entry["K"]
    sx, sy = w / entry["size"][0], h / entry["size"][1]  # a depth map smaller than the photo
    fx, cx, fy, cy = fx * sx, cx * sx, fy * sy, cy * sy
    pose = entry["pose"]
    rot, t = pose[:3, :3], pose[:3, 3]
    # box of the surface the depth map sees (every 4th pixel is enough), widened by the tolerance
    rows, cols = np.nonzero(depth[::4, ::4] > 0)
    if not len(rows):
        return np.zeros(0, np.int64)
    z = depth[::4, ::4][rows, cols].astype(np.float64)
    cam_pts = np.column_stack([(cols * 4 - cx) * z / fx, (rows * 4 - cy) * z / fy, z])
    world = (cam_pts - t) @ rot  # R^T (p - t)
    pad = 2 * tol * float(z.max()) + 4 * float(z.max()) / fx + (0.0 if abs_tol is None else float(abs_tol.max()))
    cand = grid.inside(world.min(axis=0) - pad, world.max(axis=0) + pad)
    if not len(cand):
        return cand
    native = _native.kernel("depth_agree")
    if native is not None:
        return cand[native(points, cand.astype(np.int64, copy=False), depth.astype(np.float32, copy=False),
                           (float(fx), float(fy), float(cx), float(cy)), np.ascontiguousarray(rot, np.float64),
                           np.ascontiguousarray(t, np.float64), float(tol), abs_tol)]
    pc = points[cand] @ rot.T + t
    zc = pc[:, 2]
    good = zc > 1e-9
    with np.errstate(divide="ignore", invalid="ignore"):
        u = np.rint(fx * pc[:, 0] / zc + cx)
        v = np.rint(fy * pc[:, 1] / zc + cy)
    good &= (u >= 0) & (u < w) & (v >= 0) & (v < h)
    idx = np.nonzero(good)[0]
    d = depth[v[idx].astype(np.int64), u[idx].astype(np.int64)]
    limit = tol * zc[idx]
    if abs_tol is not None:
        limit = np.maximum(limit, abs_tol[cand[idx]])
    ok = (d > 0) & (np.abs(d - zc[idx]) <= limit)
    return cand[idx[ok]]


def depth_map_support(chunk: Chunk, points: np.ndarray, tol: float = 0.01, progress: Progress | None = None,
                      workers: int | None = None, abs_tol: np.ndarray | None = None) -> np.ndarray:
    """For every point (internal frame) the number of depth maps that agree on it (uint8).
    ``abs_tol``: a tolerance of each point in internal units, used where larger than ``tol``
    times its distance (a mesh vertex cannot lie closer to the surface than about half an edge)."""
    progress = progress or Progress()
    maps = depth_maps(chunk)
    points = np.ascontiguousarray(points, np.float64)
    counts = np.zeros(len(points), np.uint16)
    if not len(points):
        return counts.astype(np.uint8)
    progress(0.02, f"Confidenza: {len(maps)} mappe di profondita'")
    grid = _Grid(points)
    workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))
    with ThreadPoolExecutor(workers) as pool:
        for i, idx in enumerate(pool.map(lambda e: _agreeing(e, points, grid, tol, abs_tol), maps)):
            counts[idx] += 1
            if i % 10 == 0 or i == len(maps) - 1:
                progress(0.05 + 0.9 * (i + 1) / len(maps), f"Confidenza: mappa {i + 1}/{len(maps)}")
    return np.minimum(counts, 255).astype(np.uint8)


def _summary(conf: np.ndarray, source: str) -> dict:
    hist = histogram(conf)
    n = max(len(conf), 1)
    return {"source": source, "points": int(len(conf)), "histogram": hist,
            "mean": float(np.mean(conf)) if len(conf) else 0.0, "max": int(conf.max()) if len(conf) else 0,
            "low": int(sum(v for c, v in hist.items() if c <= 2)), "low_percent": 100.0 * sum(
                v for c, v in hist.items() if c <= 2) / n}


def compute_point_confidence(chunk: Chunk, tol: float = 0.01, progress: Progress | None = None) -> dict:
    """Confidence of the dense cloud from the depth maps of the chunk, stored in its file.
    Returns the statistics."""
    progress = progress or Progress()
    pc = chunk.point_cloud
    if pc is None:
        raise RuntimeError("build the dense cloud first")
    conf = depth_map_support(chunk, pc.xyz, tol, progress.sub(0.0, 0.95))
    pc.confidence = conf
    chunk.point_cloud = pc
    info = _summary(conf, "mappe di profondita'")
    chunk.meta.setdefault("confidence", {})["point_cloud"] = {k: v for k, v in info.items() if k != "histogram"}
    progress(1.0, f"Confidenza di {len(pc):,} punti (media {info['mean']:.1f})")
    return info


def read_model_confidence(path) -> np.ndarray | None:
    """Vertex confidence stored in a model PLY (``None`` when it was never computed)."""
    try:
        return PointCloud.read_column(path, "confidence")
    except Exception:
        return None


def write_model(path, mesh, confidence: np.ndarray | None = None) -> Path:
    """Write a model PLY (internal frame) keeping every vertex property, the texture coordinates
    and the vertex confidence (``mesh``: :class:`~..io.mesh_ply.MeshData`, or a trimesh mesh)."""
    from ..io.mesh_ply import MeshData, write_mesh

    if not isinstance(mesh, MeshData):
        v = np.asarray(mesh.vertices, np.float64)
        fields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
        vc = mesh.visual.vertex_colors if getattr(mesh.visual, "kind", None) == "vertex" else None
        if vc is not None:
            fields += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
        arr = np.empty(len(v), fields)
        arr["x"], arr["y"], arr["z"] = v.T
        if vc is not None:
            arr["red"], arr["green"], arr["blue"] = np.asarray(vc)[:, :3].T
        mesh = MeshData(arr, np.asarray(mesh.faces, np.int64))
    if confidence is not None:
        mesh.set_field("confidence", np.minimum(np.asarray(confidence), 255))
        if mesh.vertices.dtype["confidence"].kind != "f":
            mesh.set_field("confidence", np.minimum(np.asarray(confidence), 255).astype(np.uint8), np.uint8)
    return write_mesh(path, mesh)


def half_edges(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Half the mean length of the edges around each vertex."""
    total = np.zeros(len(vertices))
    count = np.zeros(len(vertices))
    for a, b in ((0, 1), (1, 2), (2, 0)):
        length = np.linalg.norm(vertices[faces[:, a]] - vertices[faces[:, b]], axis=1)
        for k in (a, b):
            np.add.at(total, faces[:, k], length)
            np.add.at(count, faces[:, k], 1)
    return 0.5 * total / np.maximum(count, 1)


def compute_model_confidence(chunk: Chunk, tol: float = 0.01, progress: Progress | None = None) -> dict:
    """Confidence of the vertices of the model (stored in ``model.ply``): the depth maps that
    agree on each vertex, within 1 % of the distance or half the edges around the vertex; 0 where
    no depth map saw the surface there (a hole the mesh closed, a 2.5D surface across a wall)."""
    from ..io.mesh_ply import read_mesh

    progress = progress or Progress()
    path = chunk.product_path("model")
    if path is None or not path.exists():
        raise RuntimeError("build the model first")
    mesh = read_mesh(path)  # colours, texture coordinates and every other property are kept
    verts, faces = mesh.xyz, mesh.faces
    conf = depth_map_support(chunk, verts, tol, progress.sub(0.0, 0.9), abs_tol=half_edges(verts, faces))
    progress(0.92, "Salvataggio del modello")
    chunk._set_product("model", write_model(chunk.own_product_path("model"), mesh, conf))
    preview = chunk.data_dir / "model_preview.ply"
    if preview.exists():
        preview.unlink()  # made again (with the confidence) when shown
    info = _summary(conf, "mappe di profondita'")
    chunk.meta.setdefault("confidence", {})["model"] = {k: v for k, v in info.items() if k != "histogram"}
    progress(1.0, f"Confidenza di {len(conf):,} vertici (media {info['mean']:.1f})")
    return info


__all__ = ["BINS", "bin_label", "compute_model_confidence", "compute_point_confidence", "depth_map_support",
           "depth_maps", "histogram", "read_model_confidence", "write_model"]
