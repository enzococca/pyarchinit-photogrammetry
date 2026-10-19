"""Mesh from depth maps: volumetric (TSDF) fusion.

The depth maps of :func:`.dense.build_depth_maps` are fused into a truncated signed
distance field whose zero level is the surface (marching cubes), with vertex colours
averaged from the photos: the mesh built with *source data: depth maps*, without the
dense point cloud. Depth maps, photos and cameras are those of the undistorted dense
workspace; a depth is the z of the point in the camera frame (internal units).

The field lives in Open3D's hash-based ``VoxelBlockGrid``: only the blocks within the
truncation distance of a depth are allocated, so memory follows the surface area, not
the bounding box. Before integrating, the blocks touched by all depth maps are counted
and the voxel is enlarged when they would exceed the memory budget. The fusion runs in
a child process (:mod:`.isolate`) like the Poisson reconstruction: a native crash, or
the system killing it for memory, does not take the interface down; its progress is
relayed and cancelling kills it.

Pixel convention: depth-map pixel (col, row) is the ray through ``K^-1 (col, row, 1)``
(COLMAP's MVS, see ``dense._K``). Open3D's kernel truncates the projected coordinate
instead of rounding it, hence the half pixel added to the principal point. Colours are
sampled on the same rays (photo pixel centres at +0.5, as everywhere else).
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Cancelled, Progress, as_progress
from .isolate import IsolatedCrash, report, run_isolated

log = logging.getLogger("pyarchinit_photogrammetry")

VOXEL_FACTOR = 2.0  # default voxel: this many times the median size of a depth-map pixel on the surface
TRUNCATION = 4.0  # truncation distance of the signed distance, in voxels
BLOCK_RESOLUTION = 8  # voxels per side of a hash block
MIN_COMPONENT = 0.01  # pieces with fewer faces than this fraction of the largest one are dropped
_VOXEL_BYTES = {True: 12, False: 8}  # tsdf float32 + weight and colour 3 x uint16, or weight float32
_EXTRACT_BYTES = 16  # marching-cubes scratch per allocated voxel
_DEPTH_MAX = 1e10  # depths are never cut


@dataclass
class FusedMesh:
    """Surface of :func:`fuse_depth_maps`, in the internal frame."""

    vertices: np.ndarray  # (N, 3) float64
    faces: np.ndarray  # (M, 3) int64, front faces towards the cameras
    colors: np.ndarray | None  # (N, 3) uint8 RGB
    voxel_size: float  # internal units
    pixel_size: float  # median size of a depth-map pixel on the surface
    depth_maps: int

    @property
    def equivalent_points(self) -> int:
        """Points the depth maps give at their own resolution (what the face-count presets
        of the point-cloud path are based on)."""
        return int(len(self.vertices) * (self.voxel_size / self.pixel_size) ** 2)


def depth_map_frames(chunk: Chunk) -> list[dict]:
    """Depth maps of the dense workspace with their undistorted photo, calibration (scaled
    to the depth-map grid, COLMAP MVS convention) and ``cam_from_world`` (4x4)."""
    from . import dense

    ws = dense._workspace(chunk)
    info = chunk.meta.get("dense")
    if not info or not (ws / "sparse").exists():
        raise RuntimeError("build depth maps first")
    if info.get("backend") == "openmvs":
        raise RuntimeError("OpenMVS keeps its depth maps in its own format: build the depth maps with the "
                           "'cpu' or 'colmap' backend to mesh from them, or mesh the dense point cloud")
    rec = dense._read_workspace_model(ws)
    frames = []
    for image in sorted(rec.images.values(), key=lambda im: im.name):
        paths = [ws / "stereo" / "depth_maps" / f"{image.name}.{kind}.bin" for kind in ("geometric", "photometric")]
        path = next((p for p in paths if p.exists()), None)
        if path is None:
            continue
        cam = rec.cameras[image.camera_id]
        w, h = _array_size(path)
        K = dense._K(cam)
        K[0] *= w / cam.width  # COLMAP rescales focal length and principal point alike
        K[1] *= h / cam.height
        frames.append({"name": image.name, "depth": str(path), "image": str(ws / "images" / image.name),
                       "K": K, "cam_from_world": dense._pose(image)})
    if not frames:
        raise RuntimeError("no depth maps in the dense workspace: build depth maps first")
    return frames


def fuse_depth_maps(chunk: Chunk, voxel_size: float | None = None, min_views: int | None = None,
                    colors: bool = True, memory_gb: float | None = None,
                    progress: Progress | None = None) -> FusedMesh:
    """Fuse the chunk's depth maps into a surface (TSDF, marching cubes).

    ``voxel_size`` is in internal units (default :data:`VOXEL_FACTOR` times the median
    size of a depth-map pixel on the surface). A voxel counts once ``min_views`` depth
    maps have seen it (default: the number of depth maps the point-cloud fusion wants
    for the filtering mode of the depth maps). Pieces smaller than :data:`MIN_COMPONENT`
    of the largest one are dropped. ``memory_gb`` bounds the voxel grid (default: a
    quarter of the physical memory); the voxel grows when the surface needs more.
    """
    from . import dense

    progress = as_progress(progress)
    frames = depth_map_frames(chunk)
    if min_views is None:
        mode = chunk.meta["dense"].get("filter", "moderate")
        min_views = dense.FILTER_MODES.get(mode, dense.FILTER_MODES["moderate"])[2]
    min_views = max(1, min(int(min_views), len(frames)))
    budget = memory_gb * 1024 ** 3 if memory_gb else _memory_budget()
    progress(0.0, f"Fusing {len(frames)} depth maps")
    try:
        out = run_isolated(_fuse_worker, frames, voxel_size, int(min_views), bool(colors), float(budget),
                           cancel_check=lambda: progress.cancel_requested, progress=progress)
    except InterruptedError:
        progress.check()
        raise
    except Cancelled:
        raise
    except IsolatedCrash as exc:
        raise RuntimeError(f"the depth map fusion crashed ({_last_line(exc)}); "
                           "a larger voxel size needs less memory") from exc
    except RuntimeError as exc:
        log.warning("Depth map fusion failed:\n%s", exc)
        raise RuntimeError(f"the depth map fusion failed: {_last_line(exc)}") from exc
    if out["voxel_size"] > out["requested_voxel"] * 1.001:
        log.warning("Voxel size raised from %.4g to %.4g (chunk units) to fit %.1f GB of memory",
                    out["requested_voxel"] * chunk.scale, out["voxel_size"] * chunk.scale, budget / 1024 ** 3)
    fused = FusedMesh(out["vertices"], out["faces"], out["colors"], out["voxel_size"], out["pixel_size"],
                      len(frames))
    progress(1.0, f"Fused surface: {len(fused.faces):,} faces, voxel {fused.voxel_size * chunk.scale:.3g}")
    return fused


def _last_line(exc: BaseException) -> str:
    """Last line of the child's traceback, without the terminal colours of Open3D errors."""
    lines = [ln.strip() for ln in re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).splitlines() if ln.strip()]
    return lines[-1] if lines else type(exc).__name__


def _memory_budget() -> float:
    """Bytes the voxel grid may take: a quarter of the physical memory."""
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):  # pragma: no cover - Windows
        total = 8 * 1024 ** 3
    return max(1024 ** 3, total / 4)


def _array_size(path: Path) -> tuple[int, int]:
    """Width and height of a COLMAP dense ``.bin`` map from its header."""
    with open(path, "rb") as fh:
        width, height = fh.read(64).split(b"&", 3)[:2]
    return int(width), int(height)


# ---------------------------------------------------------------- child process
def _fuse_worker(frames: list[dict], voxel_size: float | None, min_views: int, colors: bool,
                 budget: float) -> dict:
    """The fusion itself (runs in the isolated child): returns the cleaned surface."""
    import open3d as o3d
    import open3d.core as o3c

    pixel = _pixel_size(frames)
    voxel = requested = float(voxel_size) if voxel_size else VOXEL_FACTOR * pixel
    # integrate around the cameras: the kernel works in float32
    origin = np.median([np.linalg.inv(f["cam_from_world"])[:3, 3] for f in frames], axis=0)
    per_voxel = _VOXEL_BYTES[colors] + _EXTRACT_BYTES
    for _ in range(8):
        blocks = _count_blocks(frames, voxel, origin)
        need = blocks * BLOCK_RESOLUTION ** 3 * per_voxel
        if need <= budget:
            break
        voxel *= max(1.15, math.sqrt(need / budget))  # the blocks cover a surface: about 1 / voxel^2
    else:
        raise MemoryError(f"the surface does not fit in {budget / 1024 ** 3:.1f} GB of memory")
    if colors:  # Open3D pairs the weight and colour types: (uint16, uint16) or (float32, float32)
        names, dtypes, channels = ["tsdf", "weight", "color"], [o3c.float32, o3c.uint16, o3c.uint16], [[1], [1], [3]]
    else:
        names, dtypes, channels = ["tsdf", "weight"], [o3c.float32, o3c.float32], [[1], [1]]
    grid = o3d.t.geometry.VoxelBlockGrid(names, dtypes, channels, voxel, BLOCK_RESOLUTION, int(blocks * 1.1) + 64)
    n = len(frames)
    for i, frame in enumerate(frames):
        depth = _load_depth(frame["depth"])
        image = o3d.t.geometry.Image(o3c.Tensor(depth))
        intrinsic, extrinsic = _intrinsic(frame), o3c.Tensor(_extrinsic(frame, origin))
        coords = grid.compute_unique_block_coordinates(image, intrinsic, extrinsic, 1.0, _DEPTH_MAX, TRUNCATION)
        rgb = _load_color(frame["image"], depth.shape) if colors else None
        if rgb is not None:
            grid.integrate(coords, image, o3d.t.geometry.Image(o3c.Tensor(rgb)), intrinsic, extrinsic, 1.0,
                           _DEPTH_MAX, TRUNCATION)
        else:
            grid.integrate(coords, image, intrinsic, extrinsic, 1.0, _DEPTH_MAX, TRUNCATION)
        report(0.15 + 0.7 * (i + 1) / n, f"Depth map {i + 1}/{n} integrated")
    report(0.86, "Extracting the surface")
    mesh = grid.extract_triangle_mesh(weight_threshold=min_views - 0.5)  # weight > threshold
    del grid
    if mesh.vertex.positions.shape[0] == 0:
        raise RuntimeError(f"no surface is seen by {min_views} depth maps")
    vertices = mesh.vertex.positions.numpy().astype(np.float64) + origin
    faces = mesh.triangle.indices.numpy().astype(np.int64)
    rgb = None
    if colors:
        rgb = np.round(np.clip(mesh.vertex.colors.numpy(), 0.0, 1.0) * 255).astype(np.uint8)
    del mesh
    report(0.95, "Removing small pieces")
    vertices, faces, rgb = _drop_small_components(vertices, faces, rgb)
    return {"vertices": vertices, "faces": faces, "colors": rgb, "voxel_size": voxel, "requested_voxel": requested,
            "pixel_size": pixel}


def _load_depth(path: str) -> np.ndarray:
    from .dense import read_colmap_array

    depth = np.array(read_colmap_array(Path(path)), dtype=np.float32, order="C")
    depth[~np.isfinite(depth) | (depth < 0)] = 0  # zero: no depth
    return depth


def _load_color(path: str, shape: tuple[int, int]) -> np.ndarray | None:
    """The photo as RGB float32 in 0..1 on the depth-map grid: pixel (col, row) of the
    depth map is photo coordinate (col / sx, row / sy), array position - 0.5."""
    import cv2

    img = cv2.imread(str(path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        return None
    h, w = shape
    mx = np.arange(w, dtype=np.float32) * np.float32(img.shape[1] / w) - np.float32(0.5)
    my = np.arange(h, dtype=np.float32) * np.float32(img.shape[0] / h) - np.float32(0.5)
    map_x, map_y = np.meshgrid(mx, my)
    rgb = cv2.remap(img, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return np.ascontiguousarray(rgb[:, :, ::-1], dtype=np.float32) / np.float32(255)


def _intrinsic(frame: dict):
    import open3d.core as o3c

    K = np.array(frame["K"], dtype=np.float64)
    K[:2, 2] += 0.5  # Open3D truncates projected coordinates, the depth-map grid needs rounding
    return o3c.Tensor(K)


def _extrinsic(frame: dict, origin: np.ndarray) -> np.ndarray:
    """``cam_from_world`` of a frame whose world origin moved to ``origin``."""
    m = np.array(frame["cam_from_world"], dtype=np.float64)
    m[:3, 3] += m[:3, :3] @ origin
    return m


def _pixel_size(frames: list[dict], sample: int = 30) -> float:
    """Median size of a depth-map pixel on the surface (depth / focal length)."""
    sizes = []
    for frame in frames[::max(1, len(frames) // sample)]:
        depth = _load_depth(frame["depth"])
        z = depth[depth > 0]
        if len(z):
            sizes.append(float(np.median(z)) / math.sqrt(frame["K"][0, 0] * frame["K"][1, 1]))
    if not sizes:
        raise RuntimeError("the depth maps are empty")
    return float(np.median(sizes))


def _count_blocks(frames: list[dict], voxel: float, origin: np.ndarray) -> int:
    """Number of hash blocks the integration will allocate (union over the depth maps)."""
    import open3d as o3d
    import open3d.core as o3c

    probe = o3d.t.geometry.VoxelBlockGrid(["tsdf", "weight"], [o3c.float32, o3c.float32], [[1], [1]], voxel,
                                          BLOCK_RESOLUTION, 16)
    seen: list[np.ndarray] = []
    for i, frame in enumerate(frames):
        image = o3d.t.geometry.Image(o3c.Tensor(_load_depth(frame["depth"])))
        coords = probe.compute_unique_block_coordinates(image, _intrinsic(frame),
                                                        o3c.Tensor(_extrinsic(frame, origin)), 1.0, _DEPTH_MAX,
                                                        TRUNCATION).numpy()
        seen.append(np.ascontiguousarray(coords, dtype=np.int32).view(np.dtype((np.void, 12))).ravel())
        if len(seen) > 16:
            seen = [np.unique(np.concatenate(seen))]
        report(0.15 * (i + 1) / len(frames), f"Sizing the volume ({i + 1}/{len(frames)})")
    return len(np.unique(np.concatenate(seen))) if seen else 0


def _drop_small_components(vertices: np.ndarray, faces: np.ndarray, colors: np.ndarray | None,
                           ratio: float = MIN_COMPONENT):
    """Keep the connected pieces with at least ``ratio`` times the faces of the largest."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    n = len(vertices)
    edges = np.concatenate([faces[:, :2], faces[:, 1:]])
    graph = coo_matrix((np.ones(len(edges), np.int8), (edges[:, 0], edges[:, 1])), shape=(n, n))
    _, labels = connected_components(graph, directed=False)
    piece = labels[faces[:, 0]]
    sizes = np.bincount(piece)
    faces = faces[sizes[piece] >= ratio * sizes.max()]
    used = np.unique(faces)
    remap = np.full(n, -1, np.int64)
    remap[used] = np.arange(len(used))
    return vertices[used], remap[faces], None if colors is None else colors[used]
