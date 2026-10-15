"""Structure from motion with COLMAP (via pycolmap).

Photos are registered in a COLMAP database inside ``<chunk>/sfm``; the best
(largest) reconstruction is kept in ``<chunk>/sfm/model`` and imported into
the chunk (camera poses, refined intrinsics, tie points).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath

import numpy as np

from ..core import crs as crs_mod
from ..core.chunk import QUALITY_DOWNSCALE, Chunk
from ..core.pointcloud import PointCloud
from ..core.progress import Progress


def set_engine_verbosity(verbose: bool = False) -> None:
    """COLMAP logs every step; keep only warnings unless ``verbose``.
    Also controlled by the environment variable ``PPG_VERBOSE=1``."""
    import pycolmap

    verbose = verbose or os.environ.get("PPG_VERBOSE") == "1"
    try:
        pycolmap.logging.minloglevel = 0 if verbose else 1
        pycolmap.logging.stderrthreshold = 0 if verbose else 1
    except Exception:  # pragma: no cover
        pass


set_engine_verbosity()


def _sfm_dir(chunk: Chunk) -> Path:
    d = chunk.data_dir / "sfm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def image_root_and_names(chunk: Chunk) -> tuple[Path, dict[int, str]]:
    """Common folder of all photos and the COLMAP image name of each camera."""
    paths = {c.key: Path(c.path).resolve() for c in chunk.cameras if c.enabled}
    if not paths:
        raise RuntimeError("the chunk has no enabled photos")
    root = Path(os.path.commonpath([str(p.parent) for p in paths.values()]))
    names = {k: str(PurePosixPath(*p.relative_to(root).parts)) for k, p in paths.items()}
    return root, names


def _reference_pairs(chunk: Chunk, names: dict[int, str], neighbors: int = 20) -> list[tuple[str, str]] | None:
    """Candidate pairs from camera reference positions (k nearest neighbours)."""
    cams = [c for c in chunk.cameras if c.enabled and c.key in names and c.reference.location]
    if len(cams) < 3:
        return None
    pts = np.array([c.reference.location for c in cams], float)
    src = cams[0].meta.get("reference_crs", chunk.crs)
    if crs_mod.is_geographic(src):
        utm = crs_mod.utm_for_lonlat(*pts[:, :2].mean(0))
        pts = crs_mod.transform_points(pts, src, utm)
    from scipy.spatial import cKDTree

    tree = cKDTree(pts[:, :2])
    k = min(neighbors + 1, len(cams))
    _, idx = tree.query(pts[:, :2], k=k)
    pairs = set()
    for i, row in enumerate(np.atleast_2d(idx)):
        for j in row:
            if i != j:
                a, b = sorted((names[cams[i].key], names[cams[int(j)].key]))
                pairs.add((a, b))
    return sorted(pairs)


def match_photos(chunk: Chunk, quality: str = "high", keypoint_limit: int = 40000, tiepoint_limit: int = 8000,
                 pair_selection: str = "auto", progress: Progress | None = None) -> None:
    import pycolmap

    progress = progress or Progress()
    work = _sfm_dir(chunk)
    db_path = work / "database.db"
    if db_path.exists():
        db_path.unlink()
    root, names = image_root_and_names(chunk)
    downscale = QUALITY_DOWNSCALE.get(quality, 2)

    by_sensor: dict[int, list[str]] = {}
    for c in chunk.cameras:
        if c.enabled:
            by_sensor.setdefault(c.sensor_key, []).append(names[c.key])

    progress(0.0, f"Detecting points in {len(names)} photos")
    for i, (skey, image_names) in enumerate(by_sensor.items()):
        sensor = chunk.sensors[skey]
        ext = pycolmap.FeatureExtractionOptions()
        ext.max_image_size = int(max(sensor.width, sensor.height) / downscale)
        ext.sift.max_num_features = int(keypoint_limit)
        reader = pycolmap.ImageReaderOptions()
        reader.camera_model = sensor.model
        reader.camera_params = ",".join(f"{p:.10g}" for p in sensor.params)
        pycolmap.extract_features(str(db_path), str(root), image_names=image_names,
                                  camera_mode=pycolmap.CameraMode.SINGLE, reader_options=reader,
                                  extraction_options=ext)
        progress(0.5 * (i + 1) / len(by_sensor), f"Points detected for sensor {sensor.label}")

    mopts = pycolmap.FeatureMatchingOptions()
    mopts.max_num_matches = max(int(tiepoint_limit) * 4, 8192)
    n = len(names)
    mode = pair_selection
    if mode == "auto":
        mode = "exhaustive" if n <= 200 else ("reference" if _reference_pairs(chunk, names) else "sequential")
    progress(0.5, f"Matching photos ({mode})")
    if mode == "exhaustive":
        pycolmap.match_exhaustive(str(db_path), matching_options=mopts)
    elif mode == "sequential":
        popts = pycolmap.SequentialPairingOptions()
        popts.overlap = 20
        pycolmap.match_sequential(str(db_path), matching_options=mopts, pairing_options=popts)
    elif mode == "reference":
        pairs = _reference_pairs(chunk, names)
        if not pairs:
            raise RuntimeError("reference pair selection needs camera reference positions")
        list_path = work / "pairs.txt"
        list_path.write_text("\n".join(f"{a} {b}" for a, b in pairs))
        popts = pycolmap.ImportedPairingOptions()
        popts.match_list_path = str(list_path)
        pycolmap.match_image_pairs(str(db_path), matching_options=mopts, pairing_options=popts)
    else:
        raise ValueError(f"unknown pair selection {pair_selection!r}")
    chunk.meta["sfm"] = {"image_root": str(root), "names": {str(k): v for k, v in names.items()},
                         "quality": quality, "pair_selection": mode}
    progress(1.0, "Matching finished")


def align_cameras(chunk: Chunk, progress: Progress | None = None) -> int:
    import pycolmap

    progress = progress or Progress()
    work = _sfm_dir(chunk)
    db_path = work / "database.db"
    if not db_path.exists():
        raise RuntimeError("run match_photos() before align_cameras()")
    root = chunk.meta["sfm"]["image_root"]
    out = work / "mapper"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    opts = pycolmap.IncrementalPipelineOptions()
    opts.multiple_models = True
    opts.min_model_size = 3
    opts.extract_colors = True
    progress(0.0, "Aligning cameras")
    recs = pycolmap.incremental_mapping(str(db_path), root, str(out), opts)
    if not recs:
        raise RuntimeError("alignment failed: no reconstruction could be initialised")
    best = max(recs.values(), key=lambda r: r.num_reg_images())
    model_dir = work / "model"
    shutil.rmtree(model_dir, ignore_errors=True)
    model_dir.mkdir()
    best.write(str(model_dir))
    chunk._set_product("reconstruction", model_dir)
    progress(0.9, f"Aligned {best.num_reg_images()} of {len(chunk.meta['sfm']['names'])} photos")
    n = import_reconstruction(chunk, best)
    progress(1.0, "Alignment finished")
    return n


def load_reconstruction(chunk: Chunk):
    import pycolmap

    p = chunk.product_path("reconstruction")
    if p is None or not p.exists():
        raise RuntimeError("the chunk is not aligned")
    rec = pycolmap.Reconstruction()
    rec.read(str(p))
    return rec


def import_reconstruction(chunk: Chunk, rec) -> int:
    """Copy poses, intrinsics and tie points of a pycolmap reconstruction into the chunk."""
    names = {v: int(k) for k, v in chunk.meta["sfm"]["names"].items()}
    for c in chunk.cameras:
        c.transform = None
    n = 0
    for image in rec.images.values():
        if not image.has_pose:
            continue
        key = names.get(image.name)
        if key is None:
            continue
        cam = chunk.camera(key)
        cfw = np.eye(4)
        cfw[:3, :] = image.cam_from_world().matrix()
        cam.transform = np.linalg.inv(cfw)
        sensor = chunk.sensors[cam.sensor_key]
        rc = rec.cameras[image.camera_id]
        sensor.model = rc.model.name
        sensor.params = [float(p) for p in rc.params]
        n += 1
    if rec.num_points3D():
        pts = rec.points3D
        xyz = np.array([p.xyz for p in pts.values()])
        rgb = np.array([p.color for p in pts.values()], dtype=np.uint8)
        track = np.array([p.track.length() for p in pts.values()], dtype=np.float32)
        tp = PointCloud(xyz, rgb)
        tp.confidence = track
        chunk.tie_points = tp
    return n


def _g(v) -> str:
    return format(float(v), ".17g")


def write_reconstruction_from_chunk(chunk: Chunk, max_points: int = 60000, max_obs_per_image: int = 4000) -> Path:
    """COLMAP model of a chunk whose poses come from elsewhere (Metashape, Photo3D...):
    cameras from the sensors, images with the camera poses, and the tie points as 3D points
    observed by every camera that sees them inside its frame (visibility by projection).
    Lets the dense step run without aligning again. Returns the model folder."""
    import pycolmap

    root, names = image_root_and_names(chunk)
    chunk.meta.setdefault("sfm", {})["image_root"] = str(root)
    chunk.meta["sfm"]["names"] = {str(k): v for k, v in names.items()}
    out = _sfm_dir(chunk) / "model"
    shutil.rmtree(out, ignore_errors=True)
    txt = out.parent / "model_txt"
    shutil.rmtree(txt, ignore_errors=True)
    txt.mkdir(parents=True)
    cams = [c for c in chunk.cameras if c.aligned and c.key in names]
    if not cams:
        raise RuntimeError("no aligned camera to write")
    sensor_ids = {}
    with open(txt / "cameras.txt", "w") as f:
        for i, key in enumerate(sorted({c.sensor_key for c in cams}), 1):
            s = chunk.sensors[key]
            sensor_ids[key] = i
            f.write(f"{i} {s.model} {int(s.width)} {int(s.height)} " + " ".join(_g(v) for v in s.params) + "\n")
    pts = chunk.tie_points.xyz if chunk.tie_points is not None else np.zeros((0, 3))
    rgb = chunk.tie_points.rgb if chunk.tie_points is not None and chunk.tie_points.rgb is not None else None
    if len(pts) > max_points:
        idx = np.random.default_rng(0).choice(len(pts), max_points, replace=False)
        pts = pts[idx]
        rgb = rgb[idx] if rgb is not None else None
    tracks: dict[int, list[tuple[int, int]]] = {}
    image_lines = []
    for img_id, c in enumerate(cams, 1):
        s = chunk.sensors[c.sensor_key]
        cfw = np.linalg.inv(c.transform)
        obs = []
        if len(pts):
            px, depth = c.project(s, pts)
            ok = np.nonzero((depth > 0) & (px[:, 0] >= 0) & (px[:, 1] >= 0) & (px[:, 0] < s.width) & (px[:, 1] < s.height))[0]
            if len(ok) > max_obs_per_image:
                ok = np.random.default_rng(img_id).choice(ok, max_obs_per_image, replace=False)
            for j, pid in enumerate(ok):
                obs.append((px[pid, 0], px[pid, 1], int(pid) + 1))
                tracks.setdefault(int(pid) + 1, []).append((img_id, j))
        q = pycolmap.Rotation3d(cfw[:3, :3]).quat  # x, y, z, w
        t = cfw[:3, 3]
        image_lines.append(f"{img_id} " + " ".join(_g(v) for v in (q[3], q[0], q[1], q[2], t[0], t[1], t[2]))
                           + f" {sensor_ids[c.sensor_key]} {names[c.key]}\n"
                           + " ".join(f"{_g(x)} {_g(y)} {p}" for x, y, p in obs) + "\n")
    with open(txt / "images.txt", "w") as f:
        f.writelines(image_lines)
    with open(txt / "points3D.txt", "w") as f:
        for pid, tr in tracks.items():
            if len(tr) < 2:
                continue
            x, y, z = pts[pid - 1]
            r, g, b = (rgb[pid - 1] if rgb is not None else (128, 128, 128))
            f.write(f"{pid} {_g(x)} {_g(y)} {_g(z)} {int(r)} {int(g)} {int(b)} 0.5 " + " ".join(f"{i} {j}" for i, j in tr) + "\n")
    rec = pycolmap.Reconstruction()
    rec.read_text(str(txt))
    out.mkdir(parents=True)
    rec.write(str(out))
    chunk._set_product("reconstruction", out)
    return out


def optimize_cameras(chunk: Chunk, fit_principal_point: bool = False, progress: Progress | None = None) -> float:
    import pycolmap

    progress = progress or Progress()
    rec = load_reconstruction(chunk)
    progress(0.1, "Optimizing camera alignment")
    opts = pycolmap.BundleAdjustmentOptions()
    try:
        opts.refine_principal_point = fit_principal_point
    except AttributeError:  # pragma: no cover - option name differs between versions
        pass
    pycolmap.bundle_adjustment(rec, opts)
    rec.write(str(chunk.product_path("reconstruction")))
    import_reconstruction(chunk, rec)
    err = float(rec.compute_mean_reprojection_error())
    chunk.meta.setdefault("sfm", {})["reprojection_error"] = err
    progress(1.0, f"Mean reprojection error {err:.3f} px")
    return err


def camera_view_graph(chunk: Chunk) -> dict[int, dict[int, int]]:
    """Number of shared tie points between aligned cameras (for neighbour selection)."""
    rec = load_reconstruction(chunk)
    names = {v: int(k) for k, v in chunk.meta["sfm"]["names"].items()}
    img_to_key = {iid: names[img.name] for iid, img in rec.images.items() if img.name in names}
    graph: dict[int, dict[int, int]] = {k: {} for k in img_to_key.values()}
    for p in rec.points3D.values():
        ids = sorted({el.image_id for el in p.track.elements})
        keys = [img_to_key[i] for i in ids if i in img_to_key]
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                graph[a][b] = graph[a].get(b, 0) + 1
                graph[b][a] = graph[b].get(a, 0) + 1
    return graph
