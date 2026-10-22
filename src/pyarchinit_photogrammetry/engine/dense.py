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

import logging
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

from .. import _native
from ..core.chunk import QUALITY_DOWNSCALE, Chunk
from ..core.pointcloud import PointCloud
from ..core.progress import Progress, as_progress
from . import colmap_log
from .visibility import UnreadablePhotos, unreadable_photos  # noqa: F401 - part of this module's API

log = logging.getLogger(__name__)

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


def aerial_survey(chunk: Chunk | None) -> bool:
    """The photos are a survey from the air: their GPS positions spread over the site (at least ten
    times their accuracy), not an object, a turntable or a trench of a few metres."""
    if chunk is None:
        return False
    from ..markers.gcp import cameras_span_their_accuracy

    try:
        return bool(cameras_span_their_accuracy(chunk))
    except Exception:  # pragma: no cover - references in a CRS that cannot be read
        return False


def choose_backend(backend: str, chunk: Chunk | None = None) -> str:
    """``auto``: COLMAP's PatchMatch with an NVIDIA GPU, OpenMVS when it is installed, the
    multi-view PatchMatch of the native core for a survey from the air (:func:`aerial_survey`),
    else two-view SGBM. From the air the steps of SGBM's disparities (1/16 pixel, about 10 cm at
    110 m on depth maps of 1000 pixels) drew contour-like terraces and stripes on the DEM of a tell,
    and PatchMatch covers more of the edges of the flight (Tell Sultan: 79 % of the ground of
    Metashape's DEM against 59 %, about eight times the time of SGBM); on a bowl photographed on a
    turntable it swelled the rim by 8 mm, so close objects and trenches keep SGBM."""
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
    if patchmatch_available() and aerial_survey(chunk):
        return "patchmatch"
    return "cpu"


def build_depth_maps(chunk: Chunk, quality: str = "medium", filter_mode: str = "moderate", backend: str = "auto",
                     progress: Progress | None = None, skip_unreadable: bool = False, gpu: str | None = None) -> None:
    """Depth maps of the aligned photos. Photos that cannot be read stop the step
    (:class:`UnreadablePhotos`) unless ``skip_unreadable``: then they are left out. ``gpu``
    (``auto``, ``all``, ``off``, an adapter index or part of its name; None: ``PPG_GPU``): the
    PatchMatch kernel runs on the GPUs, which share the photos with the CPU."""
    import pycolmap

    progress = as_progress(progress)
    progress(0.0, "Checking the photos")
    bad = unreadable_photos(chunk)
    if bad and (not skip_unreadable or len(bad) == len(chunk.aligned_cameras)):
        raise UnreadablePhotos(bad)
    backend = choose_backend(backend, chunk)
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

    with colmap_log.relay(progress.sub(0.0, 0.1), ws, phases={"undistort": (0.0, 1.0)}):
        pycolmap.undistort_images(str(ws), str(rec_dir), photo_folder(chunk), image_names=keep, undistort_options=uopts)
    chunk.meta["dense"] = {"backend": backend, "quality": quality, "filter": filter_mode}
    if bad:
        chunk.meta["dense"]["skipped_photos"] = [c.label for c in bad]
    sub = progress.sub(0.1, 0.9)
    if backend == "cpu":
        _cpu_depth_maps(chunk, ws, filter_mode, sub)
    elif backend == "patchmatch":
        _patchmatch_depth_maps(chunk, ws, filter_mode, sub, gpu=gpu)
    elif backend == "colmap":
        popts = pycolmap.PatchMatchOptions()
        popts.geom_consistency = True
        pycolmap.patch_match_stereo(str(ws), options=popts)
        mask_depth_maps(chunk, ws)
    elif backend == "openmvs":
        subprocess.run([_tool("InterfaceCOLMAP"), "-i", str(ws), "-o", str(ws / "scene.mvs"), "--image-folder",
                        str(ws / "images")], check=True, close_fds=False)
    else:
        raise ValueError(f"unknown dense backend {backend!r}")
    progress(1.0, "Depth maps ready")


MAX_FUSION_NEIGHBOURS = 64  # covisible depth maps a point is checked against on the GPU


def gpu_fusion_device(gpu: str | None) -> int | None:
    """The GPU the depth maps are fused on (``None``: COLMAP's fusion on the CPU): the first of
    :func:`_native.gpu_devices` for the setting, when the native core has the fusion kernel."""
    if _native.kernel("gpu_fuse") is None:
        return None
    try:
        devices = _native.gpu_devices(gpu)
    except Exception:  # noqa: BLE001
        return None
    return devices[0] if devices else None


def _covisibility(rec, ids: list[int], limit: int, min_shared: int = 20, sample: int = 300_000) -> dict[int, list[int]]:
    """For every image the images sharing at least ``min_shared`` tie points with it, the most
    shared first, at most ``limit``: the tracks of (a sample of ``sample`` of) the points as a
    sparse points x images matrix, and its Gram matrix."""
    from scipy.sparse import coo_matrix

    index = {iid: n for n, iid in enumerate(ids)}
    step = max(1, rec.num_points3D() // sample)
    rows, cols = [], []
    for n, p in enumerate(rec.points3D.values()):
        if n % step:
            continue
        for el in p.track.elements:
            c = index.get(el.image_id)
            if c is not None:
                rows.append(n)
                cols.append(c)
    if not rows:
        return {iid: [] for iid in ids}
    rows = np.unique(np.asarray(rows, np.int64), return_inverse=True)[1]
    cols = np.asarray(cols, np.int64)
    min_shared = max(3, min_shared // step)
    m = coo_matrix((np.ones(len(rows), np.float32), (rows, cols)), shape=(int(rows.max()) + 1, len(ids))).tocsr()
    shared = (m.T @ m).tocsr()
    out = {}
    for iid in ids:
        row = shared.getrow(index[iid])
        pairs = [(int(c), int(v)) for c, v in zip(row.indices, row.data) if c != index[iid] and v >= min_shared]
        pairs.sort(key=lambda p: -p[1])
        out[iid] = [ids[c] for c, _ in pairs[:limit]]
    return out


def _gpu_fusion(chunk: Chunk, ws: Path, min_pixels: int, device: int, progress: Progress) -> PointCloud:
    """COLMAP's fusion of the depth maps done on the GPU, map by map (``ppg_core.gpu_fuse``):
    every pixel of a map against the maps covisible with it (:func:`_covisibility`, the maps kept
    on the device in a cache), the consistent pixels (reprojection within 2 px, depth within 1 %,
    normals within 20 degrees) averaged into a point emitted by the earliest map that holds it,
    the colour of its own pixel; the confidence of the point is the number of maps that agree on
    its depth, as :func:`confidence.depth_map_support` counts it, computed in the same pass."""
    from .confidence import depth_maps

    rec = _read_workspace_model(ws)
    entries = {e["name"]: e for e in depth_maps(chunk)}
    images = [img for _, img in sorted(rec.images.items()) if img.name in entries]
    if not images:
        raise RuntimeError("no depth maps to fuse")
    ids = [img.image_id for img in images]
    order = {iid: i for i, iid in enumerate(ids)}
    progress(0.02, f"Fusing {len(ids)} depth maps on the GPU: the covisible maps of each")
    neighbours = _covisibility(rec, ids, MAX_FUSION_NEIGHBOURS)
    cams: dict[int, tuple] = {}  # iid -> (K, r, t, w, h) of its depth map

    def load(iid: int):
        e = entries[rec.images[iid].name]
        depth = read_colmap_array(e["path"])
        if depth.ndim == 3:
            depth = depth[..., 0]
        depth = np.ascontiguousarray(depth, np.float32)
        h, w = depth.shape
        fx, fy, cx, cy = e["K"]
        sx, sy = w / e["size"][0], h / e["size"][1]  # a depth map smaller than the photo
        pose = e["pose"]
        cams[iid] = ((float(fx * sx), float(fy * sy), float(cx * sx), float(cy * sy)),
                     np.ascontiguousarray(pose[:3, :3], np.float64), np.ascontiguousarray(pose[:3, 3], np.float64), w, h)
        return depth

    # the maps on the device are keyed by image id, unique within this workspace only: what another
    # chunk or project left there goes, and only the maps sent by this call count as present
    _native.gpu_fuse_forget(device)
    on_device: set[int] = set()

    def args(iid: int):
        if iid in on_device:
            k, r, t, _, _ = cams[iid]
            return (iid, None, k, r, t, order[iid])
        depth = load(iid)
        k, r, t, _, _ = cams[iid]
        on_device.add(iid)
        return (iid, depth, k, r, t, order[iid])

    xyz_parts, rgb_parts, normal_parts, support_parts = [], [], [], []
    total = 0
    for n, iid in enumerate(ids):
        progress.check()
        ref = args(iid)
        neigh = [args(j) for j in neighbours.get(iid, [])]
        xyz, normals, pixel, _pixels, support = _native.gpu_fuse(ref, neigh, min_pixels=min_pixels, device=device)
        on_device &= set(_native.gpu_fuse_cached(device))  # the slots the device gave to other maps
        if len(xyz):
            k, _, _, w, h = cams[iid]
            e = entries[rec.images[iid].name]
            # the photo decoded at the scale of the depth map (a JPEG decodes at 1/2, 1/4, 1/8 for free)
            ratio = e["size"][1] / h
            flag = cv2.IMREAD_COLOR
            for factor, reduced in ((8, cv2.IMREAD_REDUCED_COLOR_8), (4, cv2.IMREAD_REDUCED_COLOR_4), (2, cv2.IMREAD_REDUCED_COLOR_2)):
                if ratio >= factor:
                    flag = reduced
                    break
            img = cv2.imread(str(ws / "images" / rec.images[iid].name), flag | cv2.IMREAD_IGNORE_ORIENTATION)
            if img is None:
                rgb = np.zeros((len(xyz), 3), np.uint8)
            else:
                ih, iw = img.shape[:2]
                rows = np.minimum(((pixel // w) * (ih / h)).astype(np.int64), ih - 1)
                cols = np.minimum(((pixel % w) * (iw / w)).astype(np.int64), iw - 1)
                rgb = np.ascontiguousarray(img[rows, cols][:, ::-1])
            xyz_parts.append(np.asarray(xyz, np.float64))
            normal_parts.append(np.asarray(normals, np.float32))
            rgb_parts.append(rgb)
            support_parts.append(np.minimum(np.asarray(support), 255).astype(np.uint8))
            total += len(xyz)
        if n % 10 == 0 or n == len(ids) - 1:
            progress(0.05 + 0.9 * (n + 1) / len(ids), f"Fusing depth maps on the GPU: {n + 1}/{len(ids)}, {total:,} points")
    if not xyz_parts:
        raise RuntimeError("dense reconstruction produced no points")
    return PointCloud(np.concatenate(xyz_parts), np.concatenate(rgb_parts), np.concatenate(normal_parts),
                      confidence=np.concatenate(support_parts))


def build_point_cloud(chunk: Chunk, point_colors: bool = True, progress: Progress | None = None,
                      gpu: str | None = None) -> PointCloud:
    """The dense cloud from the depth maps: on a GPU (``gpu``: the setting of :func:`gpu_fusion_device`,
    default the ``PPG_GPU`` variable) with :func:`_gpu_fusion`, else COLMAP's fusion."""
    import pycolmap

    progress = as_progress(progress)
    ws = _workspace(chunk)
    info = chunk.meta.get("dense")
    if not info or not ws.exists():
        raise RuntimeError("build depth maps first")
    progress(0.0, "Fusing depth maps")
    pc = None
    if info["backend"] == "openmvs":
        subprocess.run([_tool("DensifyPointCloud"), str(ws / "scene.mvs"), "-w", str(ws)], check=True,
                       close_fds=False)
        pc = _inside_region(chunk, PointCloud.load(ws / "scene_dense.ply"))
    elif (device := gpu_fusion_device(gpu)) is not None:
        try:
            pc = _gpu_fusion(chunk, ws, FILTER_MODES.get(info["filter"], FILTER_MODES["moderate"])[2], device,
                             progress.sub(0.0, 0.95))
            pc = _inside_region(chunk, pc)
            info["fusion"] = "gpu"
        except Exception as exc:  # noqa: BLE001 - the GPU failed: COLMAP's fusion instead
            log.warning("GPU fusion failed (%s): COLMAP's fusion on the CPU", exc)
            progress(0.0, f"GPU fusion failed ({exc}): COLMAP's fusion on the CPU")
            pc = None
    if pc is None:
        info["fusion"] = "colmap"
        fopts = pycolmap.StereoFusionOptions()
        fopts.min_num_pixels = FILTER_MODES.get(info["filter"], FILTER_MODES["moderate"])[2]
        fopts.max_reproj_error = 2.0
        fopts.max_depth_error = 0.01
        fopts.max_normal_error = 20.0
        if aerial_survey(chunk):
            # at most 10 pixels in a point: without a limit a fused point takes in the pixels chained
            # from photo to photo, and with the 15-20 photos of a drone survey that see every spot the
            # cloud came out three to four times sparser than its depth maps (Tell Sultan sample:
            # 0.9 M points, 3.3 M with the limit, Metashape 4.2 M). Close range keeps COLMAP's fusion:
            # from small depth maps the points of fewer pixels were noisy enough to make two chunks of
            # the synthetic trench aligned by their clouds miss by 12 mm one time in two
            fopts.max_num_pixels = 10
        fopts.cache_size = _fusion_cache_gb()
        out = ws / "fused.ply"
        with colmap_log.relay(progress.sub(0.0, 0.85), ws, phases={"fusing": (0.0, 1.0)}):
            pycolmap.stereo_fusion(str(out), str(ws), input_type="geometric", options=fopts, output_type="PLY")
        pc = PointCloud.load(out)
        pc = _inside_region(chunk, pc)
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


def _neighbors(rec, max_neighbors: int = 4, wide=frozenset()) -> dict[int, list[int]]:
    """Best stereo partners per image: many shared tie points, a useful parallax,
    similar viewing direction and a baseline roughly parallel to the image plane
    (otherwise rectification degenerates). The images in ``wide`` - faces of 360-degree photos,
    turned wherever the camera pointed - take partners looking up to 50 degrees away (a face of
    another panorama is always within 45 degrees), the others up to 30."""
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
            turn = 50.0 if i in wide or j in wide else 30.0
            if 2.0 <= parallax <= 40.0 and axis_angle <= turn and forward <= 0.6:
                scored.append((n * min(1.0, parallax / 6.0) * min(1.0, 20.0 / parallax), j))
        out[i] = [j for _, j in sorted(scored, reverse=True)[:max_neighbors]]
    return out


def _panorama_images(chunk: Chunk, rec) -> set[int]:
    """Images of ``rec`` that are faces of 360-degree photos."""
    from .panorama import face_names

    faces = face_names(chunk)
    return {iid for iid, image in rec.images.items() if image.name in faces}


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
    native = _native.kernel("disparity_to_depth")
    if native is not None:
        return native(disp, float(max(min_disp, 0.5)), float(f), float(baseline), float(cx), float(cy), R1,
                      (float(K_i[0, 0]), float(K_i[1, 1]), float(K_i[0, 2]), float(K_i[1, 2])), (h, w))
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
    native = _native.kernel("depth_normals")
    if native is not None and depth.dtype == np.float32:
        return native(depth, (float(K[0, 0]), float(K[1, 1]), float(K[0, 2]), float(K[1, 2])))
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


def _inside_region(chunk: Chunk, pc: PointCloud) -> PointCloud:
    """The points of the reconstruction region (all of them without one)."""
    from .region import get_region, inside_internal

    region = get_region(chunk)
    if region is None or not len(pc):
        return pc
    keep = inside_internal(chunk, pc.xyz, region)
    return pc if keep.all() else pc.subset(keep)


def _masked_cameras(chunk: Chunk) -> dict:
    """Cameras with a mask by their COLMAP image name."""
    from .masks import mask_path

    names = (chunk.meta.get("sfm") or {}).get("names") or {}
    out = {}
    for c in chunk.cameras:
        if str(c.key) in names and mask_path(chunk, c) is not None:
            out[names[str(c.key)]] = c
    return out


def mask_depth_maps(chunk: Chunk, ws: Path) -> int:
    """Depth maps written by another engine set to zero where their photos are masked and
    outside the reconstruction region; returns the maps changed."""
    from .masks import mask_for_undistorted
    from .region import depth_mask, get_region

    masked = _masked_cameras(chunk)
    region = get_region(chunk)
    if not masked and region is None:
        return 0
    rec = _read_workspace_model(ws)
    n = 0
    for image in rec.images.values():
        cam = masked.get(image.name)
        if cam is None and region is None:
            continue
        K = _K(rec.cameras[image.camera_id])
        for kind in ("geometric", "photometric"):
            path = ws / "stereo" / "depth_maps" / f"{image.name}.{kind}.bin"
            if not path.exists():
                continue
            depth = read_colmap_array(path).astype(np.float32)
            keep = np.ones(depth.shape, bool)
            if cam is not None:
                m = mask_for_undistorted(chunk, cam, K, depth.shape)
                keep &= m if m is not None else True
            if region is not None:
                keep &= depth_mask(chunk, depth, K, _pose(image), region) | (depth <= 0)
            if keep.all():
                continue
            depth[~keep] = 0
            write_colmap_array(path, depth)
            n += 1
    return n


class _GrayCache:
    """Grey photos of the workspace, shared by the threads (a photo is the partner of several)."""

    def __init__(self, ws: Path, rec, size: int):
        import threading
        from collections import OrderedDict

        self.ws, self.rec, self.size = ws, rec, size
        self.items: OrderedDict = OrderedDict()
        self.lock = threading.Lock()

    def get(self, iid: int):  # None for a photo left out of the undistortion (unreadable)
        with self.lock:
            if iid in self.items:
                self.items.move_to_end(iid)
                return self.items[iid]
        path = self.ws / "images" / self.rec.images[iid].name
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION) if path.exists() else None
        with self.lock:
            self.items[iid] = img
            while len(self.items) > self.size:
                self.items.popitem(last=False)
        return img


def depth_workers(width: int, height: int, partners: int = 6, budget: float = 3e9) -> int:
    """Photos computed at the same time: one per two cores, at most six, and within ``budget``
    bytes of depth estimates (a 20 Mpx photo at full size needs about half a gigabyte)."""
    import os

    per_photo = (partners + 3) * 4.0 * width * height
    return int(max(1, min(6, (os.cpu_count() or 2) // 2, budget // max(per_photo, 1.0))))


def _cpu_depth_maps(chunk: Chunk, ws: Path, filter_mode: str, progress: Progress, workers: int | None = None) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    rec = _read_workspace_model(ws)
    neigh = _neighbors(rec, wide=_panorama_images(chunk, rec))
    min_agree, tol, _ = FILTER_MODES.get(filter_mode, FILTER_MODES["moderate"])
    (ws / "stereo" / "depth_maps").mkdir(parents=True, exist_ok=True)
    (ws / "stereo" / "normal_maps").mkdir(parents=True, exist_ok=True)
    ids = list(rec.images)
    if not workers:
        cam = max(rec.cameras.values(), key=lambda c: c.width * c.height)
        workers = depth_workers(cam.width, cam.height, max((len(v) for v in neigh.values()), default=1))
    gray = _GrayCache(ws, rec, max(12, 3 * workers))
    masked = _masked_cameras(chunk)
    from .region import depth_mask, get_region

    region = get_region(chunk)

    def one(iid) -> str | None:
        """Depth and normal maps of one photo; the name of the photo when written."""
        image = rec.images[iid]
        pose_i, K_i = _pose(image), _K(rec.cameras[image.camera_id])
        zr = _depth_range(rec, image, pose_i)
        gi = gray.get(iid) if zr is not None else None
        if gi is None:
            return None
        estimates = []
        for jid in neigh.get(iid, []):
            gj = gray.get(jid)
            if gj is None:
                continue
            other = rec.images[jid]
            d = _pair_depth(gi, gj, K_i, _K(rec.cameras[other.camera_id]), pose_i, _pose(other), zr)
            if d is not None:
                estimates.append(d)
        if not estimates:
            return None
        depth = _combine(estimates, min_agree, tol)
        if image.name in masked:  # no depth where the photo is masked
            from .masks import mask_for_undistorted

            keep = mask_for_undistorted(chunk, masked[image.name], K_i, depth.shape)
            if keep is not None:
                depth[~keep] = 0
        if region is not None:  # no depth outside the reconstruction region
            depth[~depth_mask(chunk, depth, K_i, pose_i, region)] = 0
        if (depth > 0).sum() <= 100:
            return None
        name = image.name
        out_d = ws / "stereo" / "depth_maps" / f"{name}.geometric.bin"
        out_n = ws / "stereo" / "normal_maps" / f"{name}.geometric.bin"
        out_d.parent.mkdir(parents=True, exist_ok=True)
        out_n.parent.mkdir(parents=True, exist_ok=True)
        write_colmap_array(out_d, depth)
        write_colmap_array(out_n, _normals_from_depth(depth, K_i))
        return name

    done: dict[int, str] = {}
    if workers <= 1:
        for n_done, iid in enumerate(ids):
            name = one(iid)
            if name:
                done[iid] = name
            progress((n_done + 1) / len(ids), f"Depth map {n_done + 1}/{len(ids)}")
    else:
        progress(0.0, f"Depth maps: {workers} photos at a time")
        with ThreadPoolExecutor(workers) as pool:
            futures = {pool.submit(one, iid): iid for iid in ids}
            try:
                for n_done, fut in enumerate(as_completed(futures)):
                    name = fut.result()
                    if name:
                        done[futures[fut]] = name
                    progress((n_done + 1) / len(ids), f"Depth map {n_done + 1}/{len(ids)}")
            except BaseException:  # cancelled or failed: the photos not started are dropped
                for fut in futures:
                    fut.cancel()
                raise
    names = [done[iid] for iid in ids if iid in done]
    if not names:
        raise RuntimeError("no depth map could be computed (check overlap between photos)")
    (ws / "stereo" / "fusion.cfg").write_text("\n".join(names) + "\n")


# ---------------------------------------------------------------- PatchMatch (native core)
PATCHMATCH_COST = {"none": 0.8, "mild": 0.65, "moderate": 0.5, "aggressive": 0.35}  # the largest 1 - NCC kept
PATCHMATCH_PARTNERS = 6
PATCHMATCH_MIN_VIEWS = 2


def patchmatch_available() -> bool:
    return _native.kernel("patchmatch_depth") is not None


def _patchmatch_depth_maps(chunk: Chunk, ws: Path, filter_mode: str, progress: Progress, iterations: int = 4,
                           init: bool = True, gpu: str | None = None) -> None:
    """Depth maps by PatchMatch multi-view stereo in the native core: a plane per pixel (depth and
    normal) checked against up to six partners at once by normalised cross correlation, the best
    three counting (a partner where the point is hidden does not), starting from the two-view
    SGBM depths of the photo where they exist (``init``) - so a few iterations are enough - and
    from random planes elsewhere. Slanted surfaces are matched without the fronto-parallel bias of
    a rectified pair; the normals are those of the planes."""
    kernel = _native.kernel("patchmatch_depth")
    if kernel is None:
        raise RuntimeError("the PatchMatch depth maps need the native core (pyarchinit-photogrammetry-core with "
                           "patchmatch_depth): use the CPU backend")
    job = _PatchMatchJob(chunk, ws, filter_mode, iterations, init)
    ids = list(job.rec.images)
    from . import nodes

    files_dir = chunk.data_dir.parent
    others = nodes.wanted(files_dir)
    if others and len(ids) >= 2 * nodes.DEPTH_UNIT_PHOTOS:
        # the photos in units for the nodes present and for this program (its own worker)
        project = files_dir.parent / files_dir.name[:-len(".files")] if files_dir.name.endswith(".files") else None
        units = [{"kind": "depth_maps", "project": str(project), "chunk_dir": chunk.data_dir.name, "workspace": str(ws),
                  "filter_mode": filter_mode, "iterations": int(iterations), "init": bool(init),
                  "image_ids": [int(i) for i in ids[k:k + nodes.DEPTH_UNIT_PHOTOS]]}
                 for k in range(0, len(ids), nodes.DEPTH_UNIT_PHOTOS)]
        progress(0.0, f"PatchMatch depth maps: {len(units)} units of work shared with {len(others)} other computer(s)")
        results = nodes.run(files_dir, units, progress.sub(0.0, 0.98), "units of depth maps", gpu=gpu,
                            project_path=project, local=lambda payload: {"names": job.run(payload["image_ids"], gpu=gpu)})
        names = [n for r in results for n in r.get("result", {}).get("names", [])]
        chunk.meta["dense"]["nodes"] = sorted({r.get("by") for r in results if r.get("by")})
        nodes.forget(files_dir, [r["name"] for r in results])
    else:
        names = job.run(ids, gpu=gpu, progress=progress)
    progress(1.0, f"PatchMatch: {len(names)} depth maps")
    if not names:
        raise RuntimeError("no depth map could be computed (check overlap between photos)")
    (ws / "stereo" / "fusion.cfg").write_text("\n".join(names) + "\n")


def patchmatch_job(chunk: Chunk, ws: Path, filter_mode: str, iterations: int, init: bool) -> "_PatchMatchJob":
    """The PatchMatch work of a chunk's workspace, for a node of the network (:mod:`.nodes`)."""
    return _PatchMatchJob(chunk, ws, filter_mode, iterations, init)


class _PatchMatchJob:
    """The depth maps of the photos of a workspace, one photo at a time (:meth:`one`) or a list
    of photos over the GPUs and the CPU together (:meth:`run`): one queue of photos, a worker
    per GPU and one for the CPU kernel (which uses every core), each as fast as it goes."""

    def __init__(self, chunk: Chunk, ws: Path, filter_mode: str, iterations: int, init: bool):
        self.chunk, self.ws, self.iterations, self.init = chunk, ws, int(iterations), bool(init)
        self.kernel = _native.kernel("patchmatch_depth")
        self.gpu_kernel = _native.kernel("gpu_patchmatch_depth")
        # in a survey from the air a plane needs two photos seeing its window (core 0.2.1): with
        # one, windows at the borders of the block matched metres off, and the fused points of
        # three such maps made pits and a step in the DEM where the dense cloud ends (Tell
        # Sultan). Close up, the pixels at the frame borders that a single partner sees are as
        # good as the others
        self.views = {"min_views": PATCHMATCH_MIN_VIEWS if aerial_survey(chunk) else 1} if (_native.api() or 0) >= 4 else {}
        self.rec = _read_workspace_model(ws)
        self.neigh = _neighbors(self.rec, max_neighbors=PATCHMATCH_PARTNERS, wide=_panorama_images(chunk, self.rec))
        self.max_cost = PATCHMATCH_COST.get(filter_mode, PATCHMATCH_COST["moderate"])
        self.min_agree, self.tol, _ = FILTER_MODES.get(filter_mode, FILTER_MODES["moderate"])
        (ws / "stereo" / "depth_maps").mkdir(parents=True, exist_ok=True)
        (ws / "stereo" / "normal_maps").mkdir(parents=True, exist_ok=True)
        self.gray = _GrayCache(ws, self.rec, 4 * (PATCHMATCH_PARTNERS + 1))
        self.masked = _masked_cameras(chunk)
        from .region import get_region

        self.region = get_region(chunk)

    def devices(self, gpu: str | None) -> list[tuple[int, str]]:
        """The GPUs to use (index, name): those that answer."""
        out = []
        for d in (_native.gpu_devices(gpu) if self.gpu_kernel is not None else []):
            try:
                out.append((d, _native.kernel("gpu_device_name")(d)))
            except Exception as exc:  # noqa: BLE001 - a GPU that does not work: the others, or the CPU alone
                log.warning("GPU %s unavailable for PatchMatch (%s)", d, exc)
        return out

    def one(self, iid: int, depth_fn=None) -> str | None:
        """The depth map of one photo with ``depth_fn`` (the CPU kernel by default); its name
        when written."""
        from .region import depth_mask

        depth_fn = depth_fn or self.kernel
        rec = self.rec
        image = rec.images[iid]
        pose_i, K_i = _pose(image), _K(rec.cameras[image.camera_id])
        zr = _depth_range(rec, image, pose_i)
        gi = self.gray.get(iid) if zr is not None else None
        if gi is None:
            return None
        sources, estimates = [], []
        for jid in self.neigh.get(iid, []):
            gj = self.gray.get(jid)
            if gj is None:
                continue
            other = rec.images[jid]
            K_j, pose_j = _K(rec.cameras[other.camera_id]), _pose(other)
            rel = pose_j @ np.linalg.inv(pose_i)  # reference camera -> partner camera
            kc = _K_cv(K_j)
            sources.append((np.ascontiguousarray(gj, np.float32) / 255.0, (kc[0, 0], kc[1, 1], kc[0, 2], kc[1, 2]),
                            np.ascontiguousarray(rel[:3, :3]), np.ascontiguousarray(rel[:3, 3])))
            if self.init:
                d = _pair_depth(gi, gj, K_i, K_j, pose_i, pose_j, zr)
                if d is not None:
                    estimates.append(d)
        if not sources:
            return None
        start = _combine(estimates, min(self.min_agree, 1), max(self.tol, 0.01)) if estimates else None
        kc = _K_cv(K_i)
        depth, normals, _cost = depth_fn(np.ascontiguousarray(gi, np.float32) / 255.0,
                                         (kc[0, 0], kc[1, 1], kc[0, 2], kc[1, 2]), sources, (float(zr[0]), float(zr[1])),
                                         init_depth=start, init_normals=None if start is None else
                                         _normals_from_depth(start, K_i), iterations=self.iterations,
                                         max_cost=self.max_cost, **self.views)
        if image.name in self.masked:  # no depth where the photo is masked
            from .masks import mask_for_undistorted

            keep = mask_for_undistorted(self.chunk, self.masked[image.name], K_i, depth.shape)
            if keep is not None:
                depth[~keep] = 0
        if self.region is not None:  # no depth outside the reconstruction region
            depth[~depth_mask(self.chunk, depth, K_i, pose_i, self.region)] = 0
        if (depth > 0).sum() <= 100:
            return None
        normals[depth <= 0] = 0
        for folder, arr in (("depth_maps", depth), ("normal_maps", normals)):
            out = self.ws / "stereo" / folder / f"{image.name}.geometric.bin"
            out.parent.mkdir(parents=True, exist_ok=True)  # names with folders (faces of 360-degree photos)
            write_colmap_array(out, arr)
        return image.name

    def run(self, ids, gpu: str | None = None, progress: Progress | None = None) -> list[str]:
        """The depth maps of ``ids`` over the GPUs and the CPU; the names written."""
        import threading

        devices = self.devices(gpu)
        self.chunk.meta.setdefault("dense", {})["gpu"] = ", ".join(n for _, n in devices) or None
        lock = threading.Lock()
        pending = list(ids)
        names: list[str] = []
        done = {"cpu": 0, "gpu": 0}
        failure: list[BaseException] = []
        stop = threading.Event()
        gpu_kernel, kernel = self.gpu_kernel, self.kernel

        def worker(kind: str, device):
            def on_gpu(*args, **kwargs):
                return gpu_kernel(*args, device=device, **kwargs)

            fn = kernel if kind == "cpu" else on_gpu
            while not stop.is_set():
                with lock:
                    if not pending or failure:
                        return
                    iid = pending.pop(0)
                try:
                    name = self.one(iid, fn)
                except Exception as exc:  # noqa: BLE001
                    if kind == "gpu":  # the CPU kernel takes the photo instead
                        log.warning("GPU PatchMatch failed on a photo (%s): the CPU kernel", exc)
                        try:
                            name = self.one(iid, kernel)
                        except Exception as again:  # noqa: BLE001
                            with lock:
                                failure.append(again)
                            return
                    else:
                        with lock:
                            failure.append(exc)
                        return
                with lock:
                    if name:
                        names.append(name)
                    done[kind] += 1

        threads = [threading.Thread(target=worker, args=("gpu", d), name=f"ppg-patchmatch-gpu-{d}", daemon=True)
                   for d, _ in devices]
        threads.append(threading.Thread(target=worker, args=("cpu", None), name="ppg-patchmatch-cpu", daemon=True))
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(0.5)
                if progress is not None:
                    with lock:
                        g, c = done["gpu"], done["cpu"]
                    progress((g + c) / max(len(ids), 1), f"PatchMatch depth map {g + c}/{len(ids)}"
                             + (f" ({g} on the GPU, {c} on the CPU)" if devices else ""))
                    progress.check()
        except BaseException:
            stop.set()
            raise
        if failure:
            raise failure[0]
        return names


def _combine(estimates: list[np.ndarray], min_agree: int, tol: float) -> np.ndarray:
    """Merge the depth maps of one photo computed with different partners.

    Where two or more partners see a pixel, at least ``min_agree + 1`` of them must
    agree within ``tol`` (relative). Pixels seen by a single partner are kept unless
    ``min_agree >= 2`` (aggressive filtering): SGBM already did a left-right check.
    """
    native = _native.kernel("combine_depths")
    if native is not None and all(e.dtype == np.float32 for e in estimates):
        return native(np.stack(estimates), int(min_agree), float(tol))
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
