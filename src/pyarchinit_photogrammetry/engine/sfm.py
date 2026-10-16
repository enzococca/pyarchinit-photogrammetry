"""Structure from motion with COLMAP (via pycolmap).

Photos are registered in a COLMAP database inside ``<chunk>/sfm``; the best
(largest) reconstruction is kept in ``<chunk>/sfm/model`` and imported into
the chunk (camera poses, refined intrinsics, tie points).

Photos are aligned with square pixels (one focal length) unless their sensor carries a
calibration that needs two: self-calibrating ``fx`` and ``fy`` separately can end in a
degenerate solution (on the turntable test scene, 1 alignment in 6 converged to
fy = 0.3 x the true focal length, with a folded camera ring and a wrong scale). The
result is written back in the model of the sensor (``OPENCV`` with fx = fy and no
tangential distortion); *Optimize cameras* refines every parameter afterwards.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path, PurePosixPath

import numpy as np

from ..core import crs as crs_mod
from ..core.chunk import QUALITY_DOWNSCALE, Chunk
from ..core.pointcloud import PointCloud
from ..core.progress import Progress

log = logging.getLogger(__name__)


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


def mapping_camera(sensor) -> tuple[str, list[float]]:
    """COLMAP model and parameters used to align the photos of ``sensor``: a single focal
    length (``RADIAL`` for ``OPENCV``, ``SIMPLE_PINHOLE`` for ``PINHOLE``) unless the
    sensor has fx != fy or tangential distortion, which only a calibration can give."""
    p = [float(v) for v in sensor.params]
    square = sensor.model in ("OPENCV", "PINHOLE") and abs(p[0] - p[1]) <= 1e-9 * abs(p[0])
    if square and sensor.model == "OPENCV" and p[6] == 0.0 and p[7] == 0.0:
        return "RADIAL", [p[0], p[2], p[3], p[4], p[5]]
    if square and sensor.model == "PINHOLE":
        return "SIMPLE_PINHOLE", [p[0], p[2], p[3]]
    return sensor.model, p


def sensor_model_params(model: str, params, sensor_model: str) -> tuple[str, list[float]]:
    """The parameters of a camera aligned with :func:`mapping_camera` in the model of its
    sensor (the two describe the same projection)."""
    p = [float(v) for v in params]
    if model == "RADIAL" and sensor_model == "OPENCV":
        return "OPENCV", [p[0], p[0], p[1], p[2], p[3], p[4], 0.0, 0.0]
    if model == "SIMPLE_PINHOLE" and sensor_model == "PINHOLE":
        return "PINHOLE", [p[0], p[0], p[1], p[2]]
    return model, p


def _restore_sensor_models(chunk: Chunk, rec) -> None:
    """Cameras of a reconstruction aligned with square pixels back to their sensor model."""
    import pycolmap

    names = {v: int(k) for k, v in chunk.meta["sfm"]["names"].items()}
    models: dict[int, str] = {}
    for image in rec.images.values():
        key = names.get(image.name)
        if key is not None:
            models[image.camera_id] = chunk.sensors[chunk.camera(key).sensor_key].model
    for cid, sensor_model in models.items():
        cam = rec.cameras[cid]
        model, params = sensor_model_params(cam.model.name, cam.params, sensor_model)
        if model != cam.model.name:
            cam.model = getattr(pycolmap.CameraModelId, model)
            cam.params = params


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
        model, params = mapping_camera(sensor)
        reader.camera_model = model
        reader.camera_params = ",".join(f"{p:.10g}" for p in params)
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
    _restore_sensor_models(chunk, best)
    model_dir = work / "model"
    shutil.rmtree(model_dir, ignore_errors=True)
    model_dir.mkdir()
    best.write(str(model_dir))
    chunk._set_product("reconstruction", model_dir)
    progress(0.9, f"Aligned {best.num_reg_images()} of {len(chunk.meta['sfm']['names'])} photos")
    n = import_reconstruction(chunk, best)
    progress(1.0, "Alignment finished")
    return n


def drop_missing_points(rec) -> int:
    """Unlink the observations of 3D points that a model does not hold (the models written
    from imported poses before version 0.5 referred to points seen by a single photo, which
    were not written); returns how many were unlinked."""
    n = 0
    for iid in list(rec.images):
        image = rec.images[iid]
        for idx in image.get_observation_point2D_idxs():
            if not rec.exists_point3D(image.points2D[idx].point3D_id):
                image.reset_point3D_for_point2D(idx)
                n += 1
    return n


def repair_reconstruction(chunk: Chunk) -> int:
    """:func:`drop_missing_points` on the chunk's model, rewritten when something changed."""
    p = chunk.product_path("reconstruction")
    if p is None or not p.exists():
        return 0
    rec = load_reconstruction(chunk)
    n = drop_missing_points(rec)
    if n:
        rec.write(str(p))
        log.info("COLMAP model repaired: %d observations of missing points removed", n)
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


def write_reconstruction_from_chunk(chunk: Chunk, max_points: int = 60000, max_obs_per_image: int = 4000,
                                    points: PointCloud | None = None) -> Path:
    """COLMAP model of a chunk whose poses come from elsewhere (Metashape, Photo3D, merged
    chunks...): cameras from the sensors, images with the camera poses, and the tie points
    (or ``points``, internal coordinates) as 3D points observed by every camera that sees
    them inside its frame (visibility by projection). Lets the dense step run without
    aligning again. Returns the model folder."""
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
    cloud = chunk.tie_points if points is None else points
    pts = cloud.xyz if cloud is not None else np.zeros((0, 3))
    rgb = cloud.rgb if cloud is not None and cloud.rgb is not None else None
    if len(pts) > max_points:
        idx = np.random.default_rng(0).choice(len(pts), max_points, replace=False)
        pts = pts[idx]
        rgb = rgb[idx] if rgb is not None else None
    seen = []  # per camera: (indices of the visible points, their pixels)
    for img_id, c in enumerate(cams, 1):
        s = chunk.sensors[c.sensor_key]
        if not len(pts):
            seen.append((np.zeros(0, int), np.zeros((0, 2))))
            continue
        px, depth = c.project(s, pts)
        ok = np.nonzero((depth > 0) & (px[:, 0] >= 0) & (px[:, 1] >= 0) & (px[:, 0] < s.width) & (px[:, 1] < s.height))[0]
        if len(ok) > max_obs_per_image:
            ok = np.sort(np.random.default_rng(img_id).choice(ok, max_obs_per_image, replace=False))
        seen.append((ok, px[ok]))
    # a 3D point needs two observations: the photos refer only to points that are written
    views = np.bincount(np.concatenate([ok for ok, _ in seen]), minlength=len(pts)) if len(pts) else np.zeros(0, int)
    tracks: dict[int, list[tuple[int, int]]] = {}
    image_lines = []
    for img_id, (c, (ok, uv)) in enumerate(zip(cams, seen), 1):
        cfw = np.linalg.inv(c.transform)
        obs = []
        for pid, (x, y) in zip(ok, uv):
            if views[pid] < 2:
                continue
            tracks.setdefault(int(pid) + 1, []).append((img_id, len(obs)))
            obs.append((x, y, int(pid) + 1))
        q = pycolmap.Rotation3d(cfw[:3, :3]).quat  # x, y, z, w
        t = cfw[:3, 3]
        image_lines.append(f"{img_id} " + " ".join(_g(v) for v in (q[3], q[0], q[1], q[2], t[0], t[1], t[2]))
                           + f" {sensor_ids[c.sensor_key]} {names[c.key]}\n"
                           + " ".join(f"{_g(x)} {_g(y)} {p}" for x, y, p in obs) + "\n")
    with open(txt / "images.txt", "w") as f:
        f.writelines(image_lines)
    with open(txt / "points3D.txt", "w") as f:
        for pid, tr in tracks.items():
            x, y, z = pts[pid - 1]
            r, g, b = (rgb[pid - 1] if rgb is not None else (128, 128, 128))
            f.write(f"{pid} {_g(x)} {_g(y)} {_g(z)} {int(r)} {int(g)} {int(b)} 0.5 " + " ".join(f"{i} {j}" for i, j in tr) + "\n")
    rec = pycolmap.Reconstruction()
    rec.read_text(str(txt))
    out.mkdir(parents=True)
    rec.write(str(out))
    chunk._set_product("reconstruction", out)
    return out


def _square_pixels(chunk: Chunk, rec) -> None:
    """Cameras of a reconstruction with one focal length for the adjustment (see
    :func:`mapping_camera`): fx and fy left free drift apart on some blocks."""
    import pycolmap

    names = {v: int(k) for k, v in chunk.meta["sfm"]["names"].items()}
    done = set()
    for image in rec.images.values():
        key = names.get(image.name)
        if key is None or image.camera_id in done:
            continue
        done.add(image.camera_id)
        cam = rec.cameras[image.camera_id]

        class _S:  # the camera as a sensor for mapping_camera
            model, params = cam.model.name, list(cam.params)

        model, params = mapping_camera(_S)
        if model != cam.model.name:
            cam.model = getattr(pycolmap.CameraModelId, model)
            cam.params = params


def _control_points(chunk: Chunk, rec, copies: int) -> list[int]:
    """Markers with an enabled reference (not check points) as fixed 3D points of the
    reconstruction, observed where they were measured in the photos. Each is added
    ``copies`` times: a marker measurement weighs that many tie point observations."""
    import pycolmap

    names = chunk.meta["sfm"]["names"]
    by_name = {img.name: iid for iid, img in rec.images.items() if img.has_pose}
    pids = []
    for m in chunk.markers:
        if m.checkpoint or not m.reference.enabled or not m.reference.location:
            continue
        obs = []
        for cam_key, proj in m.projections.items():
            iid = by_name.get(names.get(str(cam_key), ""))
            if iid is not None:
                obs.append((iid, proj.x, proj.y))
        if len(obs) < 2:
            continue
        X = chunk.to_internal(np.asarray([m.reference.location], float))[0]
        for _ in range(copies):
            track = pycolmap.Track()
            for iid, x, y in obs:  # marker projections are in COLMAP's pixel convention
                pts = rec.images[iid].points2D
                pts.append(pycolmap.Point2D(np.array([x, y], float)))
                track.add_element(iid, len(pts) - 1)
            pids.append(rec.add_point3D(X, track, np.array([255, 0, 0], np.uint8)))
    return pids


def optimize_cameras(chunk: Chunk, fit_principal_point: bool = False, use_markers: bool = True,
                     marker_weight: int = 4, progress: Progress | None = None) -> float:
    """Global bundle adjustment of poses, calibration and tie points.

    With ``use_markers`` and at least 3 control points (markers with an enabled reference
    and two or more projections; check points are left out) the markers enter the
    adjustment as fixed points at their reference coordinates, so the block bends to fit
    them (a deformation such as the "dome" of a wrong radial distortion is corrected, not
    only the similarity of the georeference). The report (reprojection error, control
    and check point errors before and after) goes into ``chunk.meta["sfm"]["optimize"]``.
    Returns the mean reprojection error in pixels."""
    import pycolmap

    from ..markers import gcp

    progress = progress or Progress()
    rec = load_reconstruction(chunk)
    drop_missing_points(rec)  # models written from imported poses by earlier versions

    def errors():
        rows = gcp.marker_errors(chunk)
        out = {}
        for kind in ("control", "check"):
            e = [r["error"] for r in rows if r["type"] == kind and r["error"] is not None]
            out[kind] = float(np.sqrt(np.mean(np.square(e)))) if e else None
        return out

    controls = [m for m in chunk.markers if not m.checkpoint and m.reference.enabled and m.reference.location]
    constrained = use_markers and len(controls) >= 3
    if constrained:
        gcp.update_transform(chunk)  # the reference coordinates in the internal frame
    before = errors() if constrained else {}
    progress(0.1, "Optimizing camera alignment" + (" on the control points" if constrained else ""))
    _square_pixels(chunk, rec)
    opts = pycolmap.BundleAdjustmentOptions()
    try:
        opts.refine_principal_point = fit_principal_point
    except AttributeError:  # pragma: no cover - option name differs between versions
        pass
    if constrained:  # the block first finds its own shape and calibration, then bends to the control points
        pycolmap.bundle_adjustment(rec, opts)
        _restore_sensor_models(chunk, rec)
        import_reconstruction(chunk, rec)
        gcp.update_transform(chunk)
        _square_pixels(chunk, rec)
        progress(0.5, "Optimizing camera alignment on the control points")
    pids = _control_points(chunk, rec, max(1, int(marker_weight))) if constrained else []
    if len(pids) >= 3 * max(1, int(marker_weight)):
        config = pycolmap.BundleAdjustmentConfig()
        for iid, image in rec.images.items():
            if image.has_pose:
                config.add_image(iid)
        for pid in pids:
            config.add_constant_point(pid)  # with three or more fixed points the gauge is fixed too
        pycolmap.create_default_bundle_adjuster(opts, config, rec).solve()
        added: dict[int, int] = {}
        for pid in pids:
            for el in rec.points3D[pid].track.elements:
                added[el.image_id] = added.get(el.image_id, 0) + 1
            rec.delete_point3D(pid)
        for iid, n in added.items():  # the marker observations were appended last
            pts = rec.images[iid].points2D
            for _ in range(n):
                pts.pop()
    else:
        pids = []
        pycolmap.bundle_adjustment(rec, opts)
    _restore_sensor_models(chunk, rec)
    rec.update_point_3d_errors()  # the adjustment does not refresh the stored errors
    rec.write(str(chunk.product_path("reconstruction")))
    import_reconstruction(chunk, rec)
    err = float(rec.compute_mean_reprojection_error())
    report = {"reprojection_error": err, "control_points": len(pids) // max(1, int(marker_weight))}
    if pids:
        gcp.update_transform(chunk)
        report.update({"before": before, "after": errors()})
    else:
        chunk.triangulate_markers()
    chunk.meta.setdefault("sfm", {})["reprojection_error"] = err
    chunk.meta["sfm"]["optimize"] = report
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
