"""Mesh generation: Poisson (arbitrary 3D) or 2.5D height field (terrain)."""
from __future__ import annotations

import numpy as np

from ..core import geometry
from ..core.chunk import Chunk
from ..core.progress import Progress

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


def build_model(chunk: Chunk, source: str = "point_cloud", surface: str = "arbitrary", face_count="medium",
                progress: Progress | None = None):
    import trimesh

    progress = progress or Progress()
    pc = chunk.point_cloud if source == "point_cloud" else chunk.tie_points
    if pc is None or len(pc) < 100:
        raise RuntimeError(f"not enough points in {source} to build a mesh")
    target = _target_faces(len(pc), face_count)
    progress(0.0, f"Building {surface} mesh from {len(pc):,} points")
    if surface == "height_field":
        mesh = _height_field(pc.xyz, up_vector_internal(chunk))
    else:
        mesh = _poisson(pc, target, progress.sub(0.0, 0.8))
    if len(mesh.faces) > target * 1.2:
        mesh = _decimate(mesh, target)
    if pc.rgb is not None:
        from scipy.spatial import cKDTree

        _, idx = cKDTree(pc.xyz).query(mesh.vertices, k=1)
        colors = np.column_stack([pc.rgb[idx], np.full(len(idx), 255, np.uint8)])
        mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=colors)
    path = chunk.data_dir / "model.ply"
    mesh.export(str(path))
    chunk._set_product("model", path)
    chunk.products.pop("model_textured", None)
    progress(1.0, f"Mesh: {len(mesh.faces):,} faces")
    return mesh


def _poisson(pc, target_faces: int, progress: Progress):
    import trimesh

    try:
        import open3d as o3d
    except ImportError:
        return _poisson_pycolmap(pc)
    pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pc.xyz))
    if pc.normals is not None and np.abs(pc.normals).sum() > 0:
        pcd.normals = o3d.utility.Vector3dVector(pc.normals.astype(np.float64))
    else:
        spacing = np.mean(pcd.compute_nearest_neighbor_distance())
        pcd.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=spacing * 5, max_nn=30))
        pcd.orient_normals_consistent_tangent_plane(15)
    depth = int(np.clip(np.round(np.log2(max(target_faces, 1000)) / 2 + 2), 8, 12))
    progress(0.2, f"Poisson reconstruction (depth {depth})")
    mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=depth, linear_fit=True)
    densities = np.asarray(densities)
    mesh.remove_vertices_by_mask(densities < np.quantile(densities, 0.04))
    bbox = pcd.get_axis_aligned_bounding_box()
    ext = bbox.get_extent() * 0.02
    bbox = o3d.geometry.AxisAlignedBoundingBox(bbox.min_bound - ext, bbox.max_bound + ext)
    mesh = mesh.crop(bbox)
    mesh.remove_unreferenced_vertices()
    progress(1.0, "Poisson done")
    return trimesh.Trimesh(np.asarray(mesh.vertices), np.asarray(mesh.triangles), process=True)


def _poisson_pycolmap(pc):  # pragma: no cover - used only without open3d
    import tempfile
    from pathlib import Path

    import pycolmap
    import trimesh

    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.ply", Path(tmp) / "out.ply"
        pc.save_ply(src)
        pycolmap.poisson_meshing(str(src), str(dst))
        return trimesh.load(str(dst), process=True, force="mesh")


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
