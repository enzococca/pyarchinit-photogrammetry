"""Mesh generation: Poisson (arbitrary 3D) or 2.5D height field (terrain), from the
dense cloud or the tie points, or volumetric fusion of the depth maps (:mod:`.tsdf`).

Poisson reconstruction (PoissonRecon inside Open3D) can terminate the whole process
on some clouds: it runs in an isolated child process (:mod:`.isolate`), with smooth
PCA normals oriented by the dense normals. On several threads Open3D 0.19 often fails
("Failed to close loop", segmentation faults); the reconstruction is then run again on
one thread, which does not, then at a shallower depth, and only then the height field
is used, so a mesh is always produced and the log says which path was taken.

PoissonRecon needs about 1.3-1.8 KB per input point at depths 11-12: a cloud of 13.6 M points
at depth 12 filled 24.8 GB on a 24 GB Mac and died after an hour. The points given to it are
therefore thinned to a budget of the physical memory (:func:`poisson_point_budget`) and the
depth is kept at 11 above 4 M points; the colours of the mesh still come from the whole cloud.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

import numpy as np

from ..core import geometry
from ..core.chunk import Chunk
from ..core.pointcloud import NOISE_CLASSES
from ..core.progress import Progress, as_progress
from .isolate import IsolatedCrash, run_isolated

log = logging.getLogger("pyarchinit_photogrammetry")

FACE_RATIO = {"high": 1 / 5, "medium": 1 / 15, "low": 1 / 45}
POISSON_MEMORY_SHARE = 0.4  # of the physical memory, for the Poisson process
POISSON_BYTES_PER_POINT = 2000  # measured 1.3-1.8 KB per point at depths 11-12
POISSON_DEEP_POINTS = 4_000_000  # above this, depth 12 (about twice the memory of 11) is not used
POISSON_MAX_DEPTH_FOR_MANY = 11


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


def physical_memory() -> int | None:
    """Bytes of physical memory, None when it cannot be told."""
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except Exception:  # noqa: BLE001 - not installed or not working here
        pass
    if sys.platform == "win32":
        total = _windows_physical_memory()
        if total:
            return total
    try:
        pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and size > 0:
            return int(pages) * int(size)
    except (AttributeError, OSError, ValueError):
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                                 timeout=5, close_fds=False)
            return int(out.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return None


def _windows_physical_memory() -> int | None:
    """``ullTotalPhys`` of ``kernel32.GlobalMemoryStatusEx`` (Windows: no ``sysconf``; psutil is
    optional and has no wheel for every platform the program runs on)."""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),  # DWORD
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    try:
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullTotalPhys) or None
    except (AttributeError, OSError):
        return None


def poisson_point_budget() -> int | None:
    """Most points given to the Poisson reconstruction: ``PPG_POISSON_MAX_POINTS`` (0: no limit),
    else 0.4 of the physical memory at 2000 bytes a point (24 GB: 4.8 M points); None: no limit
    (the memory is unknown)."""
    env = os.environ.get("PPG_POISSON_MAX_POINTS", "").strip()
    if env:
        try:
            value = int(float(env))
        except ValueError:
            log.warning("PPG_POISSON_MAX_POINTS=%r is not a number of points: ignored", env)
        else:
            return value if value > 0 else None
    ram = physical_memory()
    if not ram:
        return None
    return max(100_000, int(POISSON_MEMORY_SHARE * ram / POISSON_BYTES_PER_POINT))


def _thin_for_poisson(xyz: np.ndarray, normals: np.ndarray | None):
    """``(xyz, normals, (from, to) or None)``: a random sample without replacement (seeded,
    normals kept with their points) when the cloud exceeds :func:`poisson_point_budget`."""
    budget = poisson_point_budget()
    if budget is None or len(xyz) <= budget:
        return xyz, normals, None
    idx = np.sort(np.random.default_rng(0).choice(len(xyz), budget, replace=False))
    log.info("Poisson on %d of %d points: the memory of this computer allows about %d", budget, len(xyz), budget)
    return (np.ascontiguousarray(xyz[idx]), None if normals is None else np.ascontiguousarray(normals[idx]),
            (int(len(xyz)), int(budget)))


def build_model(chunk: Chunk, source: str = "point_cloud", surface: str = "arbitrary", face_count="medium",
                voxel_size: float | None = None, vertex_colors: bool = True, classes=None,
                fill_holes: float | None = None, progress: Progress | None = None):
    """Build the mesh and save it as the ``model`` product (``model.ply``, internal frame).

    ``source``: ``"point_cloud"``, ``"tie_points"`` or ``"depth_maps"`` (TSDF fusion of
    the depth maps, :mod:`.tsdf`; ``voxel_size`` in chunk CRS units, default twice the
    size of a depth-map pixel on the surface). ``face_count``: ``"high"``/``"medium"``/
    ``"low"`` (1/5, 1/15, 1/45 of the points; for depth maps, of the points they give at
    their own resolution) or a number of faces. ``vertex_colors``: paint the vertices
    with the colours of the points (of the photos for depth maps). ``classes``: only the
    dense points of these classes (the surface closes over what is left out); points
    classified as noise are never used.

    ``fill_holes`` (height field only; metres, the units of the chunk CRS): closes the holes
    of the surface whose gap between measured points is at most this. ``None`` (default)
    closes nothing: the surface stays where there are points, and what is closed on request
    is interpolated, not measured; ``chunk.meta["model"]`` says how much (``holes_filled``,
    ``holes_left``, ``interpolated_area`` in square metres in plan).
    """
    progress = as_progress(progress)
    if fill_holes is not None and fill_holes < 0:
        raise ValueError("fill_holes is the largest gap to close, in metres: it cannot be negative")
    fill = float(fill_holes) / chunk.scale if fill_holes else None
    if classes is not None and source != "point_cloud":
        raise ValueError("the classes are those of the dense cloud: use source='point_cloud'")
    if source == "depth_maps":
        report = {}
        mesh = _model_from_depth_maps(chunk, surface, face_count, voxel_size, vertex_colors, progress,
                                      fill_holes=fill, report=report)
        chunk.meta["model"] = {"source": source, "surface": surface, "classes": None,
                               **_height_field_report(chunk, report, fill_holes)}
        return _save_model(chunk, mesh, progress)
    pc = chunk.point_cloud if source == "point_cloud" else chunk.tie_points
    if pc is not None and (classes is not None or np.isin(pc.classification, NOISE_CLASSES).any()):
        pc = pc.subset(pc.class_mask(classes, exclude=NOISE_CLASSES))  # noise never makes a surface
    if pc is None or len(pc) < 100:
        raise RuntimeError(f"not enough points in {source}" + (" of the chosen classes" if classes is not None else "")
                           + " to build a mesh")
    target = _target_faces(len(pc), face_count)
    report: dict = {}
    progress(0.0, f"Building {surface} mesh from {len(pc):,} points")
    up = up_vector_internal(chunk)
    if surface == "height_field":
        mesh = _height_field(pc.xyz, up, fill, report)
        report["surface_used"] = "height_field"
    else:
        cams = np.array([c.center for c in chunk.aligned_cameras], dtype=np.float64)
        try:
            mesh = _poisson(pc, target, progress.sub(0.0, 0.8), cam_centers=cams if len(cams) else None,
                            report=report)
            report["surface_used"] = "poisson"
        except PoissonFailed as exc:
            log.warning("%s; building a 2.5D height field instead", exc)
            progress(0.8, "Poisson failed, building a height field instead")
            report = {}  # from the whole cloud: the Poisson figures do not apply
            mesh = _height_field(pc.xyz, up, fill, report)
            report["surface_used"] = "height_field"
    if len(mesh.faces) > target * 1.2:
        mesh = _decimate(mesh, target)
    if pc.rgb is not None and vertex_colors:
        _paint(mesh, pc.xyz, pc.rgb)
    chunk.meta["model"] = {"source": source, "surface": surface,
                           "classes": None if classes is None else sorted({int(c) for c in classes}),
                           **_height_field_report(chunk, report, fill_holes)}
    return _save_model(chunk, mesh, progress)


def _height_field_report(chunk: Chunk, report: dict, fill_holes: float | None) -> dict:
    """The report of a height field in metres: the gap asked for and the area that was
    interpolated to close holes (in plan, square metres). Other surfaces: unchanged."""
    if report.get("surface_used") != "height_field":
        return report
    area = report.get("interpolated_area", 0.0) * chunk.scale ** 2
    return {**report, "fill_holes": float(fill_holes) if fill_holes else None,
            "interpolated_area": round(float(area), 4)}


def _model_from_depth_maps(chunk: Chunk, surface: str, face_count, voxel_size: float | None, vertex_colors: bool,
                           progress: Progress, fill_holes: float | None = None, report: dict | None = None):
    """Surface fused from the depth maps, as a height field if asked, decimated and painted
    with the fused colours."""
    import trimesh

    from .tsdf import fuse_depth_maps

    voxel = None if voxel_size is None else float(voxel_size) / chunk.scale
    fused = fuse_depth_maps(chunk, voxel_size=voxel, colors=vertex_colors, progress=progress.sub(0.0, 0.85))
    target = _target_faces(fused.equivalent_points, face_count)
    if surface == "height_field":
        progress(0.86, "Height field from the fused surface")
        mesh = _height_field(fused.vertices, up_vector_internal(chunk), fill_holes, report)
        if report is not None:
            report["surface_used"] = "height_field"
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
    from .region import crop_mesh

    mesh = crop_mesh(chunk, mesh)  # nothing outside the reconstruction region
    path = chunk.data_dir / "model.ply"
    chunk._before_product_write("model")
    mesh.export(str(path))
    chunk._set_product("model", path)
    chunk.products.pop("model_textured", None)
    progress(1.0, f"Mesh: {len(mesh.faces):,} faces")
    return mesh


class PoissonFailed(RuntimeError):
    """Every Poisson attempt crashed or failed."""


def _poisson(pc, target_faces: int, progress: Progress, cam_centers: np.ndarray | None = None,
             report: dict | None = None):
    """Poisson surface in an isolated process.

    PoissonRecon in Open3D 0.19 fails non-deterministically on several threads (``Failed
    to close loop``, segmentation fault or a silent exit: 15 runs in 20 on the turntable
    demo, none of 20 on one thread): all cores first, then one thread at the same depth,
    then one thread with a shallower octree, before giving up with :class:`PoissonFailed`.
    The points are thinned to the memory of the computer first (:func:`_thin_for_poisson`;
    ``report["thinned_points"]`` = (from, to)) and the depth kept at 11 above 4 M points."""
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
    xyz, normals, thinned = _thin_for_poisson(xyz, normals)
    if thinned and report is not None:
        report["thinned_points"] = list(thinned)
    depth = poisson_depth(target_faces)
    if len(xyz) > POISSON_DEEP_POINTS and depth > POISSON_MAX_DEPTH_FOR_MANY:
        log.info("Poisson depth %d -> %d on %d points (memory)", depth, POISSON_MAX_DEPTH_FOR_MANY, len(xyz))
        depth = POISSON_MAX_DEPTH_FOR_MANY
    if report is not None:
        report["poisson_points"] = int(len(xyz))
    errors = []
    for attempt, (d, threads) in enumerate(((depth, -1), (depth, 1), (depth - 1, 1))):
        progress(0.2, f"Poisson reconstruction (depth {d})"
                 + (f", attempt {attempt + 1}" + (" on one thread" if threads == 1 else "") if attempt else ""))
        try:
            verts, faces = run_isolated(worker, xyz, normals, cam_centers, d, threads,
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


def _poisson_worker(xyz: np.ndarray, normals: np.ndarray | None, cam_centers: np.ndarray | None, depth: int,
                    threads: int = -1):
    """Open3D Poisson in a child process on ``threads`` threads (-1: all): returns (vertices,
    triangles).

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
                                                                                 linear_fit=False,
                                                                                 n_threads=int(threads))
    densities = np.asarray(densities)
    if len(densities):
        mesh.remove_vertices_by_mask(densities < np.quantile(densities, 0.04))
    bbox = pcd.get_axis_aligned_bounding_box()
    ext = bbox.get_extent() * 0.02
    mesh = mesh.crop(o3d.geometry.AxisAlignedBoundingBox(bbox.min_bound - ext, bbox.max_bound + ext))
    mesh.remove_unreferenced_vertices()
    return np.asarray(mesh.vertices, dtype=np.float64), np.asarray(mesh.triangles, dtype=np.int64)


def _poisson_pycolmap_worker(xyz: np.ndarray, normals: np.ndarray | None, cam_centers: np.ndarray | None,
                             depth: int, threads: int = -1):  # pragma: no cover
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


EDGE_FACTOR = 4.0  # longest edge of a face of the height field, in local point spacings
SPACING_NEIGHBOURS = 8


def _plan_spacing(xy: np.ndarray, k: int = SPACING_NEIGHBOURS, block: int = 1_000_000) -> np.ndarray:
    """Local spacing of the points in plan: the distance of each to its ``k``-th neighbour."""
    from scipy.spatial import cKDTree

    tree = cKDTree(xy)
    k = min(k + 1, len(xy))  # the first neighbour is the point itself
    out = np.empty(len(xy))
    for i in range(0, len(xy), block):  # in blocks: only the last distance is kept
        out[i:i + block] = tree.query(xy[i:i + block], k=k, workers=-1)[0][:, -1]
    return out


def _height_field(xyz: np.ndarray, up: np.ndarray, fill_holes: float | None = None, report: dict | None = None):
    """2.5D surface: Delaunay triangulation of the points in plan, without the faces that
    span more than the points around them allow.

    The limit on the edges is local: an edge is kept when it is shorter than ``EDGE_FACTOR``
    times the spacing of the points at its two ends (mean of the two, each the distance to
    the 8th neighbour in plan). One limit for the whole cloud, set by its densest part, left
    the sparser parts full of holes. Areas without points and the long faces across the
    outside margin are still cut, each against the spacing next to it.

    ``fill_holes`` (units of ``xyz``): the holes that are left are closed with the Delaunay
    faces over them when none of those faces has an edge longer than this (the largest gap
    between measured points that gets bridged). What reaches the outline of the cloud is the
    outside, never closed. ``report`` receives ``holes_filled``, ``holes_left`` and
    ``interpolated_area`` (area in plan of the faces that close holes, units of ``xyz``
    squared): that surface is interpolated, not measured.
    """
    import trimesh
    from scipy.spatial import Delaunay

    z = up / np.linalg.norm(up)
    x = np.cross(z, [1.0, 0, 0]) if abs(z[0]) < 0.9 else np.cross(z, [0, 1.0, 0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    basis = np.vstack([x, y, z])
    local = xyz @ basis.T
    e = local[:, :2]
    tri = Delaunay(e)
    faces = tri.simplices
    spacing = _plan_spacing(e)
    keep = np.ones(len(faces), bool)
    longest = np.zeros(len(faces))
    for a, b in ((0, 1), (1, 2), (2, 0)):
        length = np.linalg.norm(e[faces[:, a]] - e[faces[:, b]], axis=1)
        keep &= length < EDGE_FACTOR * 0.5 * (spacing[faces[:, a]] + spacing[faces[:, b]])
        longest = np.maximum(longest, length)
    filled, holes, left = _holes_to_fill(tri.neighbors, keep, longest, fill_holes)
    if report is not None:
        p = e[faces[filled]]
        u, v = p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]
        report["interpolated_area"] = float(0.5 * np.abs(u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]).sum())
        report["holes_filled"], report["holes_left"] = holes, left
    mesh = trimesh.Trimesh(xyz, faces[keep | filled], process=True)
    # orient faces upwards
    if (mesh.face_normals @ z).mean() < 0:
        mesh.invert()
    return mesh


def _holes_to_fill(neighbors: np.ndarray, keep: np.ndarray, longest: np.ndarray,
                   fill_holes: float | None) -> tuple[np.ndarray, int, int]:
    """Faces cut from the triangulation that close a hole no wider than ``fill_holes`` (a
    mask), the number of holes closed and of those left open.

    The faces that were cut form groups of neighbours: a group that reaches the outline of
    the triangulation is the outside margin; the others are holes, closed as a whole when
    their longest edge is within ``fill_holes``."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    out = np.zeros(len(keep), bool)
    cut = np.flatnonzero(~keep)
    if not len(cut):
        return out, 0, 0
    index = np.full(len(keep), -1)
    index[cut] = np.arange(len(cut))
    nb = neighbors[cut]  # -1: no neighbour, the face is on the outline
    rows = np.repeat(np.arange(len(cut)), 3)
    cols = np.where(nb >= 0, index[nb], -1).ravel()
    ok = cols >= 0
    n, label = connected_components(coo_matrix((np.ones(ok.sum(), bool), (rows[ok], cols[ok])),
                                               shape=(len(cut), len(cut))), directed=False)
    outside = np.zeros(n, bool)
    outside[label[(nb < 0).any(axis=1)]] = True
    width = np.zeros(n)
    np.maximum.at(width, label, longest[cut])
    close = ~outside & (width <= (fill_holes or 0.0))
    out[cut[close[label]]] = True
    return out, int(close.sum()), int((~outside & ~close).sum())


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
