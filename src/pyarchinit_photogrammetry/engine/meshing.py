"""Mesh generation: Poisson (arbitrary 3D) or 2.5D height field (terrain), from the
dense cloud or the tie points, or volumetric fusion of the depth maps (:mod:`.tsdf`).

Poisson reconstruction (PoissonRecon inside Open3D) can terminate the whole process
on some clouds: it runs in an isolated child process (:mod:`.isolate`), with smooth
PCA normals oriented by the dense normals, and falls back to a shallower depth and
then to the height field, so a mesh is always produced and the log says which path
was taken.
"""
from __future__ import annotations

import logging
import os

import numpy as np

from ..core import geometry
from ..core.chunk import Chunk
from ..core.pointcloud import NOISE_CLASSES
from ..core.progress import Progress
from .isolate import IsolatedCrash, run_isolated

log = logging.getLogger("pyarchinit_photogrammetry")

FACE_RATIO = {"high": 1 / 5, "medium": 1 / 15, "low": 1 / 45}


def up_vector_internal(chunk: Chunk) -> np.ndarray:
    """World +Z expressed in the internal frame. For non-georeferenced chunks the
    up direction is estimated from the cameras (opposite of their mean image +Y axis)."""
    _, rot, _ = geometry.decompose_similarity(chunk.transform)
    if not np.allclose(rot, np.eye(3)):
        return rot.T @ np.array([0.0, 0.0, 1.0])
    cams = chunk.aligned_cameras
    if not cams:
        return np.array([0.0, 0.0, 1.0])
    view = np.array([c.transform[:3, 2] for c in cams]).mean(0)
    if np.linalg.norm(view) > 0.7:  # cameras look the same way (nadir/aerial): up is opposite
        up = -view
    else:  # handheld photos around an object: image "up" is -Y of the camera
        up = np.array([-c.transform[:3, 1] for c in cams]).mean(0)
    return up / np.linalg.norm(up)


def _target_faces(n_points: int, face_count) -> int:
    if isinstance(face_count, (int, np.integer)):
        return int(face_count)
    return max(1000, int(n_points * FACE_RATIO.get(face_count, FACE_RATIO["medium"])))


def poisson_depth(target_faces: int) -> int:
    return int(np.clip(np.round(np.log2(max(target_faces, 1000)) / 2 + 2), 8, 12))


def build_model(chunk: Chunk, source: str = "point_cloud", surface: str = "arbitrary", face_count="medium",
                voxel_size: float | None = None, vertex_colors: bool = True, classes=None,
                progress: Progress | None = None):
    """Build the mesh and save it as the ``model`` product (``model.ply``, internal frame).

    ``source``: ``"point_cloud"``, ``"tie_points"`` or ``"depth_maps"`` (TSDF fusion of
    the depth maps, :mod:`.tsdf`; ``voxel_size`` in chunk CRS units, default twice the
    size of a depth-map pixel on the surface). ``face_count``: ``"high"``/``"medium"``/
    ``"low"`` (1/5, 1/15, 1/45 of the points; for depth maps, of the points they give at
    their own resolution) or a number of faces. ``vertex_colors``: paint the vertices
    with the colours of the points (of the photos for depth maps). ``classes``: only the
    dense points of these classes (the surface closes over what is left out); points
    classified as noise are never used.
    """
    progress = progress or Progress()
    if classes is not None and source != "point_cloud":
        raise ValueError("the classes are those of the dense cloud: use source='point_cloud'")
    if source == "depth_maps":
        mesh = _model_from_depth_maps(chunk, surface, face_count, voxel_size, vertex_colors, progress)
        chunk.meta["model"] = {"source": source, "surface": surface, "classes": None}
        return _save_model(chunk, mesh, progress)
    pc = chunk.point_cloud if source == "point_cloud" else chunk.tie_points
    if pc is not None and (classes is not None or np.isin(pc.classification, NOISE_CLASSES).any()):
        pc = pc.subset(pc.class_mask(classes, exclude=NOISE_CLASSES))  # noise never makes a surface
    if pc is None or len(pc) < 100:
        raise RuntimeError(f"not enough points in {source}" + (" of the chosen classes" if classes is not None else "")
                           + " to build a mesh")
    target = _target_faces(len(pc), face_count)
    progress(0.0, f"Building {surface} mesh from {len(pc):,} points")
    up = up_vector_internal(chunk)
    if surface == "height_field":
        mesh = _height_field(pc.xyz, up)
    else:
        cams = np.array([c.center for c in chunk.aligned_cameras], dtype=np.float64)
        try:
            mesh = _poisson(pc, target, progress.sub(0.0, 0.8), cam_centers=cams if len(cams) else None)
        except PoissonFailed as exc:
            log.warning("%s; building a 2.5D height field instead", exc)
            progress(0.8, "Poisson failed, building a height field instead")
            mesh = _height_field(pc.xyz, up)
    if len(mesh.faces) > target * 1.2:
        mesh = _decimate(mesh, target)
    if pc.rgb is not None and vertex_colors:
        _paint(mesh, pc.xyz, pc.rgb)
    chunk.meta["model"] = {"source": source, "surface": surface,
                           "classes": None if classes is None else sorted({int(c) for c in classes})}
    return _save_model(chunk, mesh, progress)


def _model_from_depth_maps(chunk: Chunk, surface: str, face_count, voxel_size: float | None, vertex_colors: bool,
                           progress: Progress):
    """Surface fused from the depth maps, as a height field if asked, decimated and painted
    with the fused colours."""
    import trimesh

    from .tsdf import fuse_depth_maps

    voxel = None if voxel_size is None else float(voxel_size) / chunk.scale
    fused = fuse_depth_maps(chunk, voxel_size=voxel, colors=vertex_colors, progress=progress.sub(0.0, 0.85))
    target = _target_faces(fused.equivalent_points, face_count)
    if surface == "height_field":
        progress(0.86, "Height field from the fused surface")
        mesh = _height_field(fused.vertices, up_vector_internal(chunk))
    else:
        mesh = trimesh.Trimesh(fused.vertices, fused.faces, process=True)
    if len(mesh.faces) > target * 1.2:
        progress(0.88, f"Decimating to {target:,} faces")
        mesh = _decimate(mesh, target)
    if fused.colors is not None:
        _paint(mesh, fused.vertices, fused.colors)
    return mesh


def _paint(mesh, xyz: np.ndarray, rgb: np.ndarray) -> None:
    """Vertex colours of the nearest coloured point."""
    import trimesh
    from scipy.spatial import cKDTree

    _, idx = cKDTree(xyz).query(mesh.vertices, k=1)
    colors = np.column_stack([rgb[idx], np.full(len(idx), 255, np.uint8)])
    mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=colors)


def _save_model(chunk: Chunk, mesh, progress: Progress):
    path = chunk.data_dir / "model.ply"
    mesh.export(str(path))
    chunk._set_product("model", path)
    chunk.products.pop("model_textured", None)
    progress(1.0, f"Mesh: {len(mesh.faces):,} faces")
    return mesh


class PoissonFailed(RuntimeError):
    """Every Poisson attempt crashed or failed."""


def _poisson(pc, target_faces: int, progress: Progress, cam_centers: np.ndarray | None = None):
    """Poisson surface in an isolated process.

    PoissonRecon fails non-deterministically on some clouds (``Failed to close loop``,
    segmentation fault or a silent exit): each depth is tried twice, then a shallower
    octree, before giving up with :class:`PoissonFailed`."""
    import trimesh

    try:
        import open3d  # noqa: F401
    except ImportError:
        worker = _poisson_pycolmap_worker
    else:
        worker = _poisson_worker
    xyz = np.ascontiguousarray(pc.xyz, dtype=np.float64)
    normals = None
    if pc.normals is not None and np.abs(pc.normals).sum() > 0:
        normals = np.ascontiguousarray(pc.normals, dtype=np.float64)
    depth = poisson_depth(target_faces)
    errors = []
    for attempt, d in enumerate((depth, depth, depth - 1, depth - 1)):
        progress(0.2, f"Poisson reconstruction (depth {d})" + (f", attempt {attempt + 1}" if attempt else ""))
        try:
            verts, faces = run_isolated(worker, xyz, normals, cam_centers, d,
                                        cancel_check=lambda: progress.cancel_requested)
        except InterruptedError:
            progress.check()
            raise
        except (IsolatedCrash, RuntimeError, TimeoutError) as exc:
            log.warning("Poisson reconstruction at depth %d failed: %s", d, _short(exc))
            errors.append(f"depth {d}: {_short(exc)}")
            continue
        if len(faces) < 10:
            errors.append(f"depth {d}: empty mesh")
            continue
        progress(1.0, "Poisson done")
        return trimesh.Trimesh(verts, faces, process=True)
    raise PoissonFailed("Poisson reconstruction failed (" + "; ".join(errors) + ")")


def _short(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[-1] if text else type(exc).__name__


def _poisson_worker(xyz: np.ndarray, normals: np.ndarray | None, cam_centers: np.ndarray | None, depth: int):
    """Open3D Poisson in a child process: returns (vertices, triangles).

    Normals are re-estimated with PCA (smooth, unit length) and oriented towards the
    camera that is nearest to each point (every point was seen from the side its
    normal points to), or with the dense normals when no camera is known: raw
    per-pixel normals from depth maps make PoissonRecon fail to close its iso-surface
    loops. ``linear_fit`` stays off for the same reason."""
    if os.environ.get("PPG_POISSON_SIMULATE_CRASH"):  # used by the test suite
        os.abort()
    import open3d as o3d

    pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(xyz))
    spacing = float(np.mean(pcd.compute_nearest_neighbor_distance()))
    pcd.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=spacing * 5, max_nn=30))
    est = np.asarray(pcd.normals)
    if cam_centers is not None and len(cam_centers):
        from scipy.spatial import cKDTree

        _, nearest = cKDTree(cam_centers).query(xyz)
        flip = np.einsum("ij,ij->i", est, cam_centers[nearest] - xyz) < 0
        pcd.normals = o3d.utility.Vector3dVector(np.ascontiguousarray(np.where(flip[:, None], -est, est)))
    elif normals is not None and len(normals) == len(est):
        flip = np.einsum("ij,ij->i", est, normals) < 0
        pcd.normals = o3d.utility.Vector3dVector(np.ascontiguousarray(np.where(flip[:, None], -est, est)))
    else:
        pcd.orient_normals_consistent_tangent_plane(15)
    mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=int(depth),
                                                                                 linear_fit=False)
    densities = np.asarray(densities)
    if len(densities):
        mesh.remove_vertices_by_mask(densities < np.quantile(densities, 0.04))
    bbox = pcd.get_axis_aligned_bounding_box()
    ext = bbox.get_extent() * 0.02
    mesh = mesh.crop(o3d.geometry.AxisAlignedBoundingBox(bbox.min_bound - ext, bbox.max_bound + ext))
    mesh.remove_unreferenced_vertices()
    return np.asarray(mesh.vertices, dtype=np.float64), np.asarray(mesh.triangles, dtype=np.int64)


def _poisson_pycolmap_worker(xyz: np.ndarray, normals: np.ndarray | None, cam_centers: np.ndarray | None,
                             depth: int):  # pragma: no cover
    """COLMAP's PoissonRecon (used only when Open3D is not installed)."""
    import tempfile
    from pathlib import Path

    import pycolmap
    import trimesh

    from ..core.pointcloud import PointCloud

    if normals is None:
        raise RuntimeError("pycolmap Poisson needs point normals (build the dense cloud first)")
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.ply", Path(tmp) / "out.ply"
        PointCloud(xyz, normals=normals.astype(np.float32)).save_ply(src)
        opts = pycolmap.PoissonMeshingOptions()
        opts.depth = int(depth)
        pycolmap.poisson_meshing(str(src), str(dst), opts)
        m = trimesh.load(str(dst), process=True, force="mesh")
        return np.asarray(m.vertices, dtype=np.float64), np.asarray(m.faces, dtype=np.int64)


def _height_field(xyz: np.ndarray, up: np.ndarray):
    import trimesh
    from scipy.spatial import Delaunay

    z = up / np.linalg.norm(up)
    x = np.cross(z, [1.0, 0, 0]) if abs(z[0]) < 0.9 else np.cross(z, [0, 1.0, 0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    basis = np.vstack([x, y, z])
    local = xyz @ basis.T
    tri = Delaunay(local[:, :2])
    faces = tri.simplices
    e = local[:, :2]
    edges = np.max([np.linalg.norm(e[faces[:, a]] - e[faces[:, b]], axis=1) for a, b in ((0, 1), (1, 2), (2, 0))], axis=0)
    faces = faces[edges < 4 * np.median(edges)]
    mesh = trimesh.Trimesh(xyz, faces, process=True)
    # orient faces upwards
    if (mesh.face_normals @ z).mean() < 0:
        mesh.invert()
    return mesh


def _decimate(mesh, target_faces: int):
    try:
        return mesh.simplify_quadric_decimation(face_count=int(target_faces))
    except Exception:
        try:
            import open3d as o3d

            m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(mesh.vertices),
                                          o3d.utility.Vector3iVector(mesh.faces))
            m = m.simplify_quadric_decimation(int(target_faces))
            import trimesh

            return trimesh.Trimesh(np.asarray(m.vertices), np.asarray(m.triangles), process=True)
        except Exception:
            return mesh
