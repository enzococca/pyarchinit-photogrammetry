"""Depth maps and dense point cloud.

Backends
--------
``cpu``     Built-in, runs everywhere (macOS/Apple Silicon included). Depth maps
            come from semi-global matching (OpenCV SGBM) on rectified pairs of
            neighbouring photos, cross-checked between neighbours, then fused by
            COLMAP's stereo fusion (CPU).
``colmap``  COLMAP PatchMatch stereo; needs pycolmap built with CUDA (NVIDIA).
``openmvs`` OpenMVS ``DensifyPointCloud`` if installed and on the PATH.
``auto``    ``colmap`` when CUDA is available, else ``openmvs`` if found, else ``cpu``.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

from ..core.chunk import QUALITY_DOWNSCALE, Chunk
from ..core.pointcloud import PointCloud
from ..core.progress import Progress
from .visibility import UnreadablePhotos, unreadable_photos  # noqa: F401 - part of this module's API

FILTER_MODES = {  # min agreeing neighbours, relative depth tolerance, fusion min pixels
    "none": (0, 0.0, 2),
    "mild": (1, 0.02, 2),
    "moderate": (1, 0.01, 3),
    "aggressive": (2, 0.005, 3),
}


def _workspace(chunk: Chunk) -> Path:
    return chunk.data_dir / "dense"


def _fusion_cache_gb() -> float:
    """Half of the physical memory (COLMAP's default cache is 32 GB)."""
    import os

    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (ValueError, OSError, AttributeError):  # pragma: no cover - Windows
        total = 8.0
    return float(max(1.0, min(32.0, total * 0.5)))


def _tool(name: str) -> str:
    """Full path of a program: with it (and ``close_fds=False``) subprocess starts it with
    posix_spawn instead of fork(), whose atfork handlers crash after SpatiaLite has used PROJ."""
    return shutil.which(name) or name


def choose_backend(backend: str) -> str:
    if backend != "auto":
        return backend
    try:
        import pycolmap

        if pycolmap.has_cuda:
            return "colmap"
    except Exception:  # pragma: no cover
        pass
    if shutil.which("DensifyPointCloud") and shutil.which("InterfaceCOLMAP"):
        return "openmvs"
    return "cpu"


def build_depth_maps(chunk: Chunk, quality: str = "medium", filter_mode: str = "moderate", backend: str = "auto",
                     progress: Progress | None = None, skip_unreadable: bool = False) -> None:
    """Depth maps of the aligned photos. Photos that cannot be read stop the step
    (:class:`UnreadablePhotos`) unless ``skip_unreadable``: then they are left out."""
    import pycolmap

    progress = progress or Progress()
    progress(0.0, "Checking the photos")
    bad = unreadable_photos(chunk)
    if bad and (not skip_unreadable or len(bad) == len(chunk.aligned_cameras)):
        raise UnreadablePhotos(bad)
    backend = choose_backend(backend)
    ws = _workspace(chunk)
    shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True)
    rec_dir = chunk.product_path("reconstruction")
    if rec_dir is None:
        raise RuntimeError("align the cameras before building depth maps")
    from .sfm import repair_reconstruction

    repair_reconstruction(chunk)  # a model written from imported poses by earlier versions
    downscale = QUALITY_DOWNSCALE.get(quality, 4)
    max_side = max(max(s.width, s.height) for s in chunk.sensors.values())
    uopts = pycolmap.UndistortCameraOptions()
    uopts.max_image_size = int(max_side / downscale)
    progress(0.0, "Undistorting photos")
    names = chunk.meta["sfm"].get("names", {})
    keep = []
    if bad:  # only the readable photos
        skipped = {c.key for c in bad}
        keep = [names[str(c.key)] for c in chunk.aligned_cameras if c.key not in skipped and str(c.key) in names]
    from .sfm import photo_folder

    pycolmap.undistort_images(str(ws), str(rec_dir), photo_folder(chunk), image_names=keep, undistort_options=uopts)
    chunk.meta["dense"] = {"backend": backend, "quality": quality, "filter": filter_mode}
    if bad:
        chunk.meta["dense"]["skipped_photos"] = [c.label for c in bad]
    sub = progress.sub(0.1, 0.9)
    if backend == "cpu":
        _cpu_depth_maps(chunk, ws, filter_mode, sub)
    elif backend == "colmap":
        popts = pycolmap.PatchMatchOptions()
        popts.geom_consistency = True
        pycolmap.patch_match_stereo(str(ws), options=popts)
    elif backend == "openmvs":
        subprocess.run([_tool("InterfaceCOLMAP"), "-i", str(ws), "-o", str(ws / "scene.mvs"), "--image-folder",
                        str(ws / "images")], check=True, close_fds=False)
    else:
        raise ValueError(f"unknown dense backend {backend!r}")
    progress(1.0, "Depth maps ready")


def build_point_cloud(chunk: Chunk, point_colors: bool = True, progress: Progress | None = None) -> PointCloud:
    import pycolmap

    progress = progress or Progress()
    ws = _workspace(chunk)
    info = chunk.meta.get("dense")
    if not info or not ws.exists():
        raise RuntimeError("build depth maps first")
    progress(0.0, "Fusing depth maps")
    if info["backend"] == "openmvs":
        subprocess.run([_tool("DensifyPointCloud"), str(ws / "scene.mvs"), "-w", str(ws)], check=True,
                       close_fds=False)
        pc = PointCloud.load(ws / "scene_dense.ply")
    else:
        fopts = pycolmap.StereoFusionOptions()
        fopts.min_num_pixels = FILTER_MODES.get(info["filter"], FILTER_MODES["moderate"])[2]
        fopts.max_reproj_error = 2.0
        fopts.max_depth_error = 0.01
        fopts.max_normal_error = 20.0
        fopts.cache_size = _fusion_cache_gb()
        out = ws / "fused.ply"
        pycolmap.stereo_fusion(str(out), str(ws), input_type="geometric", options=fopts, output_type="PLY")
        pc = PointCloud.load(out)
        if len(pc):  # confidence: how many depth maps agree on each point
            from .confidence import depth_map_support

            try:
                pc.confidence = depth_map_support(chunk, pc.xyz, progress=progress.sub(0.85, 0.13))
            except Exception as exc:  # pragma: no cover - unreadable depth maps: a cloud without confidence
                progress(0.98, f"Confidence not computed: {exc}")
    if not point_colors:
        pc.rgb = None
    if len(pc) == 0:
        raise RuntimeError("dense reconstruction produced no points")
    chunk.point_cloud = pc
    chunk.meta["point_cloud_source"] = f"mappe di profondita' (qualita' {info.get('quality', '?')})"  # not imported
    progress(1.0, f"Point cloud: {len(pc):,} points")
    return pc


# ---------------------------------------------------------------- CPU backend
def _read_workspace_model(ws: Path):
    import pycolmap

    rec = pycolmap.Reconstruction()
    rec.read(str(ws / "sparse"))
    return rec


def _pose(image) -> np.ndarray:
    m = np.eye(4)
    m[:3, :] = image.cam_from_world().matrix()
    return m


def _K(cam) -> np.ndarray:
    """Calibration matrix as COLMAP's MVS uses it: depth-map pixel (col, row) is the
    ray through ``K^-1 (col, row, 1)``. (Fusion reprojects fused points exactly onto
    integer coordinates, see the tests.)"""
    fx, fy, cx, cy = cam.params[:4]
    return np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.0]])


def _K_cv(K: np.ndarray) -> np.ndarray:
    """Same camera for OpenCV image warping, where pixel centres are at integers of
    a frame whose origin is the first pixel centre (COLMAP SfM uses +0.5)."""
    k = K.copy()
    k[0, 2] -= 0.5
    k[1, 2] -= 0.5
    return k


def _neighbors(rec, max_neighbors: int = 4) -> dict[int, list[int]]:
    """Best stereo partners per image: many shared tie points, a useful parallax,
    similar viewing direction and a baseline roughly parallel to the image plane
    (otherwise rectification degenerates)."""
    shared: dict[int, dict[int, int]] = {i: {} for i in rec.images}
    for p in rec.points3D.values():
        ids = sorted({el.image_id for el in p.track.elements})
        for a_i, a in enumerate(ids):
            for b in ids[a_i + 1:]:
                shared[a][b] = shared[a].get(b, 0) + 1
                shared[b][a] = shared[b].get(a, 0) + 1
    poses = {i: _pose(img) for i, img in rec.images.items()}
    centers = {i: np.linalg.inv(m)[:3, 3] for i, m in poses.items()}
    axes = {i: m[2, :3] for i, m in poses.items()}  # optical axis in world frame
    pts = {i: np.array([rec.points3D[pid].xyz for pid in _point_ids(img, rec)]).reshape(-1, 3)
           for i, img in rec.images.items()}
    out = {}
    for i, cand in shared.items():
        if len(pts[i]) == 0:
            out[i] = []
            continue
        scene = np.median(pts[i], axis=0)
        scored = []
        for j, n in cand.items():
            if n < 20:
                continue
            a, b = centers[i] - scene, centers[j] - scene
            parallax = np.degrees(np.arccos(np.clip(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)), -1, 1)))
            axis_angle = np.degrees(np.arccos(np.clip(axes[i] @ axes[j], -1, 1)))
            cj = poses[i][:3, :3] @ centers[j] + poses[i][:3, 3]  # partner centre in camera i frame
            forward = abs(cj[2]) / max(np.linalg.norm(cj), 1e-12)
            if 2.0 <= parallax <= 40.0 and axis_angle <= 30.0 and forward <= 0.6:
                scored.append((n * min(1.0, parallax / 6.0) * min(1.0, 20.0 / parallax), j))
        out[i] = [j for _, j in sorted(scored, reverse=True)[:max_neighbors]]
    return out


def _point_ids(image, rec=None) -> list[int]:
    """3D points seen by an image; with ``rec`` only those it holds (a model written by
    earlier versions from imported poses could refer to points it does not contain)."""
    ids = [p.point3D_id for p in image.points2D if p.has_point3D()]
    return ids if rec is None else [i for i in ids if rec.exists_point3D(i)]


def _depth_range(rec, image, pose) -> tuple[float, float] | None:
    ids = _point_ids(image, rec)
    if len(ids) < 10:
        return None
    xyz = np.array([rec.points3D[i].xyz for i in ids])
    z = (xyz @ pose[:3, :3].T + pose[:3, 3])[:, 2]
    z = z[z > 0]
    if len(z) < 10:
        return None
    return float(np.percentile(z, 2)) * 0.8, float(np.percentile(z, 98)) * 1.25


def _pair_depth(img_i, img_j, K_i, K_j, pose_i, pose_j, zrange) -> np.ndarray | None:
    """Depth map of image i (in its own pixel grid) from the stereo pair (i, j)."""
    h, w = img_i.shape[:2]
    rel = pose_j @ np.linalg.inv(pose_i)  # cam_i -> cam_j
    R, T = rel[:3, :3], rel[:3, 3]
    zero = np.zeros(5)
    Kc_i, Kc_j = _K_cv(K_i), _K_cv(K_j)
    R1, R2, P1, P2, _, _, _ = cv2.stereoRectify(Kc_i, zero, Kc_j, zero, (w, h), R, T.reshape(3, 1), alpha=0,
                                                flags=cv2.CALIB_ZERO_DISPARITY)
    m1 = cv2.initUndistortRectifyMap(Kc_i, zero, R1, P1, (w, h), cv2.CV_32FC1)
    m2 = cv2.initUndistortRectifyMap(Kc_j, zero, R2, P2, (w, h), cv2.CV_32FC1)
    ri = cv2.remap(img_i, *m1, cv2.INTER_LINEAR)
    rj = cv2.remap(img_j, *m2, cv2.INTER_LINEAR)
    f, cx, cy = P1[0, 0], P1[0, 2], P1[1, 2]
    vertical = abs(P2[1, 3]) > abs(P2[0, 3])
    shift = P2[1, 3] if vertical else P2[0, 3]
    baseline = abs(shift) / f
    if vertical:
        ri, rj = ri.T.copy(), rj.T.copy()
    flip = shift > 0  # reference camera is on the right/bottom: mirror so it becomes the "left" one
    if flip:
        ri, rj = ri[:, ::-1].copy(), rj[:, ::-1].copy()
    zmin, zmax = zrange
    dmax, dmin = f * baseline / zmin, f * baseline / zmax
    min_disp = max(0, int(np.floor(dmin)) - 2)
    num = int(np.ceil((dmax - min_disp + 4) / 16.0)) * 16
    width = ri.shape[1]
    num = min(num, (width - min_disp - 32) // 16 * 16, 512)
    if num < 16:
        return None
    sgbm = cv2.StereoSGBM_create(minDisparity=min_disp, numDisparities=num, blockSize=5, P1=8 * 25, P2=32 * 25,
                                 disp12MaxDiff=1, uniquenessRatio=10, speckleWindowSize=100, speckleRange=2,
                                 mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)
    disp = sgbm.compute(ri, rj).astype(np.float32) / 16.0
    if flip:
        disp = disp[:, ::-1]
    if vertical:
        disp = disp.T
    valid = disp > max(min_disp, 0.5)
    yy, xx = np.nonzero(valid)
    d = disp[valid]
    z = f * baseline / d
    pts_rect = np.column_stack([(xx - cx) * z / f, (yy - cy) * z / f, z])
    pts_cam = pts_rect @ R1  # R1^T applied to row vectors
    zc = pts_cam[:, 2]
    good = zc > 0
    pts_cam, zc = pts_cam[good], zc[good]
    u = K_i[0, 0] * pts_cam[:, 0] / zc + K_i[0, 2]
    v = K_i[1, 1] * pts_cam[:, 1] / zc + K_i[1, 2]
    ui, vi = np.round(u).astype(int), np.round(v).astype(int)
    inside = (ui >= 0) & (ui < w) & (vi >= 0) & (vi < h)
    depth = np.full(h * w, np.inf, np.float32)
    np.minimum.at(depth, vi[inside] * w + ui[inside], zc[inside].astype(np.float32))
    depth[~np.isfinite(depth)] = 0
    return depth.reshape(h, w)


def _normals_from_depth(depth: np.ndarray, K: np.ndarray) -> np.ndarray:
    h, w = depth.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    x = (xx - K[0, 2]) * depth / K[0, 0]
    y = (yy - K[1, 2]) * depth / K[1, 1]
    pts = np.dstack([x, y, depth])
    dx = np.zeros_like(pts)
    dy = np.zeros_like(pts)
    dx[:, 1:-1] = pts[:, 2:] - pts[:, :-2]
    dy[1:-1, :] = pts[2:, :] - pts[:-2, :]
    n = np.cross(dx, dy)
    norm = np.linalg.norm(n, axis=2, keepdims=True)
    n = np.divide(n, norm, out=np.zeros_like(n), where=norm > 0)
    flip = (n * pts).sum(axis=2) > 0  # normals must face the camera
    n[flip] *= -1
    n[depth <= 0] = 0
    return n.astype(np.float32)


def write_colmap_array(path: Path, arr: np.ndarray) -> None:
    """Write a COLMAP dense ``.bin`` map (depth: HxW, normal: HxWx3)."""
    a = arr if arr.ndim == 3 else arr[:, :, None]
    h, w, c = a.shape
    with open(path, "wb") as fh:
        fh.write(f"{w}&{h}&{c}&".encode("ascii"))
        fh.write(np.transpose(a, (1, 0, 2)).astype("<f4").tobytes(order="F"))


def read_colmap_array(path: Path) -> np.ndarray:
    with open(path, "rb") as fh:
        raw = fh.read()
    parts = raw.split(b"&", 3)
    w, h, c = int(parts[0]), int(parts[1]), int(parts[2])
    data = np.frombuffer(parts[3], dtype="<f4")
    arr = data.reshape((w, h, c), order="F")
    return np.transpose(arr, (1, 0, 2)).squeeze()


def _cpu_depth_maps(chunk: Chunk, ws: Path, filter_mode: str, progress: Progress) -> None:
    rec = _read_workspace_model(ws)
    neigh = _neighbors(rec)
    min_agree, tol, _ = FILTER_MODES.get(filter_mode, FILTER_MODES["moderate"])
    (ws / "stereo" / "depth_maps").mkdir(parents=True, exist_ok=True)
    (ws / "stereo" / "normal_maps").mkdir(parents=True, exist_ok=True)
    gray_cache: dict[int, np.ndarray] = {}

    def gray(iid):  # None for a photo left out of the undistortion (unreadable)
        if iid not in gray_cache:
            path = ws / "images" / rec.images[iid].name
            gray_cache[iid] = (cv2.imread(str(path), cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION)
                               if path.exists() else None)
        return gray_cache[iid]

    done = []
    ids = list(rec.images)
    for n_done, iid in enumerate(ids):
        image = rec.images[iid]
        pose_i, K_i = _pose(image), _K(rec.cameras[image.camera_id])
        zr = _depth_range(rec, image, pose_i)
        estimates = []
        if zr is not None and gray(iid) is not None:
            for jid in neigh.get(iid, []):
                if gray(jid) is None:
                    continue
                other = rec.images[jid]
                d = _pair_depth(gray(iid), gray(jid), K_i, _K(rec.cameras[other.camera_id]), pose_i, _pose(other), zr)
                if d is not None:
                    estimates.append(d)
        if estimates:
            depth = _combine(estimates, min_agree, tol)
            if (depth > 0).sum() > 100:
                name = image.name
                out_d = ws / "stereo" / "depth_maps" / f"{name}.geometric.bin"
                out_n = ws / "stereo" / "normal_maps" / f"{name}.geometric.bin"
                out_d.parent.mkdir(parents=True, exist_ok=True)
                out_n.parent.mkdir(parents=True, exist_ok=True)
                write_colmap_array(out_d, depth)
                write_colmap_array(out_n, _normals_from_depth(depth, K_i))
                done.append(name)
        if len(gray_cache) > 12:
            gray_cache.clear()
        progress((n_done + 1) / len(ids), f"Depth map {n_done + 1}/{len(ids)}")
    if not done:
        raise RuntimeError("no depth map could be computed (check overlap between photos)")
    (ws / "stereo" / "fusion.cfg").write_text("\n".join(done) + "\n")


def _combine(estimates: list[np.ndarray], min_agree: int, tol: float) -> np.ndarray:
    """Merge the depth maps of one photo computed with different partners.

    Where two or more partners see a pixel, at least ``min_agree + 1`` of them must
    agree within ``tol`` (relative). Pixels seen by a single partner are kept unless
    ``min_agree >= 2`` (aggressive filtering): SGBM already did a left-right check.
    """
    import warnings

    warnings.filterwarnings("ignore", category=RuntimeWarning, message=".*(slice|empty).*")
    s = np.where(np.stack(estimates) > 0, np.stack(estimates), np.nan)
    n_obs = np.isfinite(s).sum(axis=0)
    with np.errstate(all="ignore"):
        med = np.nanmedian(s, axis=0)
        close = np.abs(s - med) <= max(tol, 1e-6) * med
        agree = close.sum(axis=0)
        depth = np.nanmean(np.where(close, s, np.nan), axis=0)
    if min_agree == 0:
        keep = n_obs >= 1
        depth = np.where(np.isfinite(depth), depth, med)
    else:
        need = min_agree + 1
        keep = (agree >= need) | ((n_obs == 1) & (min_agree < 2))
    depth[~keep | ~np.isfinite(depth)] = 0
    return depth.astype(np.float32)
