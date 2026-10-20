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
from ..core.progress import Progress, as_progress

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
    square = sensor.model in ("OPENCV", "PINHOLE", "OPENCV_FISHEYE") and abs(p[0] - p[1]) <= 1e-9 * abs(p[0])
    if square and sensor.model == "OPENCV" and p[6] == 0.0 and p[7] == 0.0:
        return "RADIAL", [p[0], p[2], p[3], p[4], p[5]]
    if square and sensor.model == "OPENCV_FISHEYE" and p[6] == 0.0 and p[7] == 0.0:
        return "RADIAL_FISHEYE", [p[0], p[2], p[3], p[4], p[5]]
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
    if model == "RADIAL_FISHEYE" and sensor_model == "OPENCV_FISHEYE":
        return "OPENCV_FISHEYE", [p[0], p[0], p[1], p[2], p[3], p[4], 0.0, 0.0]
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


def _same_file(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def photo_folder(chunk: Chunk) -> str:
    """Folder from which COLMAP reads the photos by their image names (``meta["sfm"]["names"]``).

    It is the common folder of the photos (``meta["sfm"]["image_root"]``) unless some photos
    were linked elsewhere (:func:`relink_photos`): then a folder of links named after the
    image names, inside the chunk data, points to every photo where it is now."""
    sfm = chunk.meta.get("sfm") or {}
    root, names = sfm.get("image_root"), sfm.get("names") or {}
    cams = {str(c.key): c for c in chunk.cameras}
    moved = {k: cams[k].path for k, name in names.items()
             if k in cams and not (root and _same_file(Path(root) / name, cams[k].path))}
    if root and not moved:
        return str(root)
    links = chunk.data_dir / "photo_links"
    shutil.rmtree(links, ignore_errors=True)
    for k, name in names.items():
        if k not in cams:
            continue
        target, link = Path(cams[k].path), links / name
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, link)
        except OSError:  # Windows without the right to create links: a hard link, else a copy
            try:
                os.link(target, link)
            except OSError:
                if target.exists():
                    shutil.copy2(target, link)
    return str(links)


def relink_photos(chunk: Chunk, folder, only_unreadable: bool = False) -> dict:
    """Point the cameras to the photos with the same file name in ``folder`` and its
    subfolders (empty files are not taken). When a name is found more than once, the copy
    whose parent folders match the old path best wins. The common folder of the photos
    (``meta["sfm"]["image_root"]``) follows when every photo of the model is now under one
    folder with the same relative names; otherwise :func:`photo_folder` links them."""
    from .visibility import unreadable_photos

    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(folder)
    index: dict[str, list[Path]] = {}
    for dirpath, _dirs, files in os.walk(folder):
        for f in files:
            path = Path(dirpath) / f
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".dng"}:
                index.setdefault(f.lower(), []).append(path)
    cams = chunk.unreadable_photos(chunk.cameras) if only_unreadable else list(chunk.cameras)
    relinked, not_found = 0, []
    for cam in cams:
        old = Path(cam.path)
        found = []
        for cand in index.get(old.name.lower(), []):
            try:
                if cand.stat().st_size > 0:
                    found.append(cand)
            except OSError:
                pass
        if not found:
            not_found.append(cam.label)
            continue

        def common_tail(p: Path) -> int:
            n = 0
            for a, b in zip(reversed(p.parent.parts), reversed(old.parent.parts)):
                if a.lower() != b.lower():
                    break
                n += 1
            return n

        best = max(found, key=common_tail)
        if not _same_file(best, old):
            cam.path = str(best.resolve())
            relinked += 1
    sfm = chunk.meta.get("sfm")
    if sfm and sfm.get("names"):
        by_key = {str(c.key): Path(c.path).resolve() for c in chunk.cameras}
        roots = set()
        for k, name in sfm["names"].items():
            if k in by_key:
                path, parts = by_key[k], PurePosixPath(name).parts
                if tuple(p.lower() for p in path.parts[-len(parts):]) != tuple(p.lower() for p in parts):
                    roots = set()
                    break
                roots.add(Path(*path.parts[:-len(parts)]))
        if len(roots) == 1:
            sfm["image_root"] = str(roots.pop())
    left = unreadable_photos(chunk)
    return {"relinked": relinked, "not_found": not_found, "unreadable": len(left),
            "image_root": (chunk.meta.get("sfm") or {}).get("image_root")}


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


PRESELECTION_SIZE = 1024  # pixels of the longer side of the photos matched to find the pairs
PRESELECTION_POINTS = 1024  # points per photo for that
PRESELECTION_INLIERS = 15  # verified matches that make two photos a pair
PRESELECTION_PAIRS_PER_PHOTO = 20  # the pairs of each photo with the most matches are matched in full
EXHAUSTIVE_UP_TO = 40  # photos: every pair is matched in full (cheap enough), above it the pairs are preselected


def strongest_pairs(weights: dict[tuple[str, str], int], per_photo: int, links: int = 10) -> set[tuple[str, str]]:
    """The ``per_photo`` pairs of every photo with the most verified matches (``weights``); a pair
    stays when it is among the strongest of either photo. Where the cut leaves groups of photos
    that the whole set joined - flights at different heights meet through few matches - the
    ``links`` strongest pairs between every two groups come back. (Tell Sultan, 425 photos at four
    heights: every photo overlapped 69 others, the full matching of all those pairs took 25 minutes.)"""
    by_photo: dict[str, list[tuple[int, tuple[str, str]]]] = {}
    for pair, w in weights.items():
        for photo in pair:
            by_photo.setdefault(photo, []).append((w, pair))
    kept: set[tuple[str, str]] = set()
    for pairs in by_photo.values():
        pairs.sort(reverse=True)
        kept.update(p for _, p in pairs[:per_photo])
    parent = {photo: photo for photo in by_photo}

    def root(photo):
        while parent[photo] != photo:
            parent[photo] = parent[parent[photo]]
            photo = parent[photo]
        return photo

    for a, b in kept:
        parent[root(a)] = root(b)
    between: dict[tuple[str, str], list[tuple[int, tuple[str, str]]]] = {}
    for pair, w in weights.items():
        ga, gb = root(pair[0]), root(pair[1])
        if ga != gb:
            between.setdefault((min(ga, gb), max(ga, gb)), []).append((w, pair))
    for pairs in between.values():
        pairs.sort(reverse=True)
        kept.update(p for _, p in pairs[:links])
    return kept


def _preselected_pairs(chunk: Chunk, work: Path, root, by_sensor: dict, masks,
                       names: dict[int, str]) -> list[tuple[str, str]]:
    """The pairs of photos that overlap, found as the generic preselection of Metashape finds them:
    a thousand points of every photo reduced to 1024 pixels, matched in every pair (a fraction of
    the full matching), the pairs with at least ``PRESELECTION_INLIERS`` verified matches kept -
    for each photo the ``PRESELECTION_PAIRS_PER_PHOTO`` with the most (:func:`strongest_pairs`).
    With GPS positions their nearest neighbours are added: a net for photos with little texture.
    The full matching then runs on these pairs only (Tell Sultan sample, 57 photos: 859 pairs of
    1596 in 7 s; orbits of oblique photos find the vertical ones that see the same ground, which a
    choice by the positions of the cameras alone would miss: in the whole survey of 425 photos at
    four heights only 72 % of the pairs that overlap are among the 80 nearest by GPS)."""
    import sqlite3

    import pycolmap

    db_path = work / "preselection.db"
    db_path.unlink(missing_ok=True)
    try:
        for skey, image_names in by_sensor.items():
            sensor = chunk.sensors[skey]
            ext = pycolmap.FeatureExtractionOptions()
            ext.max_image_size = min(PRESELECTION_SIZE, max(sensor.width, sensor.height))
            ext.sift.max_num_features = PRESELECTION_POINTS
            reader = pycolmap.ImageReaderOptions()
            model, params = mapping_camera(sensor)
            reader.camera_model = model
            reader.camera_params = ",".join(f"{p:.10g}" for p in params)
            if masks is not None:
                reader.mask_path = str(masks)
            pycolmap.extract_features(str(db_path), str(root), image_names=image_names,
                                      camera_mode=pycolmap.CameraMode.SINGLE, reader_options=reader,
                                      extraction_options=ext)
        pycolmap.match_exhaustive(str(db_path))
        con = sqlite3.connect(str(db_path))
        try:
            ids = dict(con.execute("SELECT image_id, name FROM images"))
            weights = {}
            for pair_id, rows in con.execute("SELECT pair_id, rows FROM two_view_geometries WHERE rows >= ?",
                                             (PRESELECTION_INLIERS,)):
                a, b = pycolmap.pair_id_to_image_pair(int(pair_id))
                weights[tuple(sorted((ids[a], ids[b])))] = int(rows)
        finally:
            con.close()
        pairs = strongest_pairs(weights, PRESELECTION_PAIRS_PER_PHOTO) if PRESELECTION_PAIRS_PER_PHOTO else set(weights)
    finally:
        for suffix in ("", "-shm", "-wal"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    pairs |= set(_reference_pairs(chunk, names, neighbors=8) or [])
    return sorted(pairs)


def match_photos(chunk: Chunk, quality: str = "high", keypoint_limit: int = 40000, tiepoint_limit: int = 8000,
                 pair_selection: str = "auto", progress: Progress | None = None,
                 exclude_stationary: bool = False) -> None:
    """Detect and match the points of the photos (SIFT); the masked parts of the photos
    (:mod:`.masks`) are left out. ``exclude_stationary``: matches that do not move between two
    photos are dropped - the background of a turntable photographed from a fixed camera. The
    faces of 360-degree photos (:mod:`.panorama`) become the frames of a rig.

    ``pair_selection``: ``exhaustive`` (every pair), ``generic`` (the pairs that overlap, found on
    small images first: :func:`_preselected_pairs`), ``reference`` (the nearest cameras by GPS),
    ``sequential`` (video); ``auto`` matches every pair up to ``EXHAUSTIVE_UP_TO`` photos and
    preselects above (360-degree rigs: every pair)."""
    import pycolmap

    from .masks import colmap_mask_folder
    from .panorama import panorama_cameras, rig_configs

    progress = as_progress(progress)
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

    masks = colmap_mask_folder(chunk, names, work / "masks")
    rigs = rig_configs(chunk, names)
    faces = {c.sensor_key for c in panorama_cameras(chunk) if c.enabled} if rigs else set()
    progress(0.0, f"Detecting points in {len(names)} photos" + (" (with masks)" if masks else ""))
    for i, (skey, image_names) in enumerate(by_sensor.items()):
        sensor = chunk.sensors[skey]
        ext = pycolmap.FeatureExtractionOptions()
        ext.max_image_size = int(max(sensor.width, sensor.height) / downscale)
        ext.sift.max_num_features = int(keypoint_limit)
        reader = pycolmap.ImageReaderOptions()
        model, params = mapping_camera(sensor)
        reader.camera_model = model
        reader.camera_params = ",".join(f"{p:.10g}" for p in params)
        if masks is not None:
            reader.mask_path = str(masks)
        # a rig needs a camera per face: the faces of the panoramas are in a folder each
        mode = pycolmap.CameraMode.PER_FOLDER if skey in faces else pycolmap.CameraMode.SINGLE
        pycolmap.extract_features(str(db_path), str(root), image_names=image_names,
                                  camera_mode=mode, reader_options=reader, extraction_options=ext)
        progress(0.5 * (i + 1) / len(by_sensor), f"Points detected for sensor {sensor.label}")
    if rigs:
        db = pycolmap.Database.open(str(db_path))
        try:
            pycolmap.apply_rig_config(rigs, db)
        finally:
            db.close()

    mopts = pycolmap.FeatureMatchingOptions()
    mopts.max_num_matches = max(int(tiepoint_limit) * 4, 8192)
    mopts.skip_image_pairs_in_same_frame = bool(rigs)  # same centre: nothing to triangulate
    n = len(names)
    mode = pair_selection
    if mode == "auto":
        mode = "exhaustive" if n <= EXHAUSTIVE_UP_TO or rigs else "generic"
    pairs = None
    if mode == "generic":
        progress(0.5, "Finding the pairs of photos that overlap")
        pairs = _preselected_pairs(chunk, work, root, by_sensor, masks, names)
        if not pairs:
            mode = "exhaustive"  # nothing found on the small images: every pair
    elif mode == "reference":
        pairs = _reference_pairs(chunk, names)
        if not pairs:
            raise RuntimeError("reference pair selection needs camera reference positions")
    progress(0.55, f"Matching photos ({mode}" + (f", {len(pairs):,} pairs)" if pairs else ")"))
    if mode == "exhaustive":
        pycolmap.match_exhaustive(str(db_path), matching_options=mopts)
    elif mode == "sequential":
        popts = pycolmap.SequentialPairingOptions()
        popts.overlap = 20
        pycolmap.match_sequential(str(db_path), matching_options=mopts, pairing_options=popts)
    elif mode in ("reference", "generic"):
        list_path = work / "pairs.txt"
        list_path.write_text("\n".join(f"{a} {b}" for a, b in pairs))
        popts = pycolmap.ImportedPairingOptions()
        popts.match_list_path = str(list_path)
        pycolmap.match_image_pairs(str(db_path), matching_options=mopts, pairing_options=popts)
    else:
        raise ValueError(f"unknown pair selection {pair_selection!r}")
    removed = remove_stationary_matches(db_path) if exclude_stationary else None
    chunk.meta["sfm"] = {"image_root": str(root), "names": {str(k): v for k, v in names.items()},
                         "quality": quality, "pair_selection": mode, "pairs": len(pairs) if pairs else None,
                         "masks": masks is not None, "stationary_removed": removed, "rigs": len(rigs),
                         "keypoint_limit": int(keypoint_limit), "tiepoint_limit": int(tiepoint_limit)}
    progress(1.0, "Matching finished" + (f" ({removed:,} stationary matches left out)" if removed else ""))


def remove_stationary_matches(db_path: Path, min_shift: float = 2.0, min_inliers: int = 15) -> int:
    """Drop the verified matches whose point does not move between the two photos (less than
    ``min_shift`` px, or 0.15 % of the photo): with a fixed camera and a turntable they are the
    background. Pairs left with fewer than ``min_inliers`` are dropped. Edits the COLMAP
    database (its documented sqlite layout); returns the matches removed."""
    import sqlite3

    con = sqlite3.connect(str(db_path))
    try:
        sizes = {i: max(w, h) for i, w, h in con.execute(
            "SELECT images.image_id, cameras.width, cameras.height FROM images JOIN cameras USING (camera_id)")}
        keypoints = {}
        for image_id, rows, cols, data in con.execute("SELECT image_id, rows, cols, data FROM keypoints"):
            if rows and data:
                keypoints[image_id] = np.frombuffer(data, np.float32).reshape(rows, cols)[:, :2]
        removed = 0
        updates = []
        for pair_id, rows, cols, data in con.execute(
                "SELECT pair_id, rows, cols, data FROM two_view_geometries WHERE rows > 0"):
            a, b = divmod(int(pair_id), 2147483647)
            if a not in keypoints or b not in keypoints or not data:
                continue
            m = np.frombuffer(data, np.uint32).reshape(rows, cols)
            shift = np.linalg.norm(keypoints[a][m[:, 0]] - keypoints[b][m[:, 1]], axis=1)
            limit = max(min_shift, 0.0015 * max(sizes.get(a, 0), sizes.get(b, 0)))
            keep = shift >= limit
            if keep.all():
                continue
            removed += int((~keep).sum())
            m = np.ascontiguousarray(m[keep]) if keep.sum() >= min_inliers else np.zeros((0, 2), np.uint32)
            updates.append((len(m), 2, m.tobytes(), pair_id))
        con.executemany("UPDATE two_view_geometries SET rows = ?, cols = ?, data = ? WHERE pair_id = ?", updates)
        con.commit()
        return removed
    finally:
        con.close()


GLOBAL_FROM = 40  # photos: from here ``auto`` aligns them all at once (global SfM) instead of one by one


def _global_mapping(chunk: Chunk, db_path: Path, root: str, out: Path, n: int):
    """All the cameras at once (COLMAP's global SfM, GLOMAP): rotations averaged over every pair,
    then positions, then the bundle adjustment on a selection of the tracks - ``tiepoint_limit / 4``
    per photo, as the tie point limit of Metashape, retriangulated afterwards. Tell Sultan sample
    (57 photos): 58 s against 293 s one photo at a time, the same cameras within 2 cm."""
    import pycolmap

    per_view = max(500, int((chunk.meta.get("sfm") or {}).get("tiepoint_limit", 8000)) // 4)
    opts = pycolmap.GlobalPipelineOptions()
    opts.mapper.track_required_tracks_per_view = per_view
    opts.mapper.keep_max_num_tracks = per_view * max(n, 1)
    return pycolmap.global_mapping(str(db_path), root, str(out), opts)


def align_cameras(chunk: Chunk, progress: Progress | None = None, mapper: str = "auto") -> int:
    """Cameras and tie points from the matches of :func:`match_photos`. ``mapper``: ``incremental``
    (one photo after the other, COLMAP), ``global`` (all at once, GLOMAP: much faster with many
    photos) or ``auto`` - global from ``GLOBAL_FROM`` photos, incremental for fewer, for 360-degree
    rigs and calibrations held fixed, and whenever the global one leaves photos out."""
    import pycolmap

    progress = as_progress(progress)
    work = _sfm_dir(chunk)
    db_path = work / "database.db"
    if not db_path.exists():
        raise RuntimeError("run match_photos() before align_cameras()")
    root = chunk.meta["sfm"]["image_root"]
    out = work / "mapper"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    n = len(chunk.meta["sfm"].get("names", {}))
    fixed = _fixed_camera_ids(chunk, db_path)
    rigs = bool(chunk.meta["sfm"].get("rigs"))
    if mapper not in ("auto", "incremental", "global"):
        raise ValueError(f"unknown mapper {mapper!r}")
    if mapper == "auto":
        mapper = "global" if n >= GLOBAL_FROM and not fixed and not rigs else "incremental"
    recs = {}
    if mapper == "global":
        progress(0.0, f"Aligning {n} cameras at once (global)")
        try:
            recs = _global_mapping(chunk, db_path, root, out, n)
        except Exception as exc:  # pragma: no cover - a block the global solver cannot handle
            progress(0.05, f"Global alignment failed ({exc}): one photo at a time")
        best = max(recs.values(), key=lambda r: r.num_reg_images()) if recs else None
        if best is None or best.num_reg_images() < 0.9 * n:
            progress(0.1, f"Global alignment placed {best.num_reg_images() if best else 0} of {n} photos: "
                          "one photo at a time")
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True)
            recs, mapper = {}, "incremental"
    if mapper == "incremental":
        opts = pycolmap.IncrementalPipelineOptions()
        opts.multiple_models = True
        opts.min_model_size = 3
        opts.extract_colors = True
        if fixed:  # faces of 360-degree photos (exact calibration, rig known), sensors held by the user
            opts.constant_cameras = fixed
            opts.ba_refine_sensor_from_rig = False
        progress(0.0, "Aligning cameras")
        recs = pycolmap.incremental_mapping(str(db_path), root, str(out), opts)
    if not recs:
        raise RuntimeError("alignment failed: no reconstruction could be initialised")
    chunk.meta["sfm"]["mapper"] = mapper
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


def _held_names(chunk: Chunk) -> set[str]:
    """COLMAP image names of the photos whose calibration is not refined: faces of 360-degree
    photos (virtual cameras known exactly) and photos of sensors with ``fixed`` calibration."""
    from .panorama import face_names

    names = (chunk.meta.get("sfm") or {}).get("names") or {}
    held = {names[str(c.key)] for c in chunk.cameras
            if str(c.key) in names and getattr(chunk.sensors.get(c.sensor_key), "fixed", False)}
    return face_names(chunk) | held


def _fixed_camera_ids(chunk: Chunk, db_path: Path) -> set[int]:
    """COLMAP cameras whose calibration is held (:func:`_held_names`) in the database ``db_path``."""
    import pycolmap

    wanted = _held_names(chunk)
    if not wanted:
        return set()
    db = pycolmap.Database.open(str(db_path))
    try:
        return {int(im.camera_id) for im in db.read_all_images() if im.name in wanted}
    finally:
        db.close()


def _rec_fixed_cameras(chunk: Chunk, rec) -> set[int]:
    """The cameras of a reconstruction whose calibration is held (:func:`_held_names`)."""
    wanted = _held_names(chunk)
    return {int(im.camera_id) for im in rec.images.values() if im.name in wanted} if wanted else set()


def _adjust(rec, opts, constant_points=(), fixed_cameras=()) -> None:
    """Bundle adjustment of the aligned images of ``rec``: ``constant_points`` held (they also
    fix the gauge when there are three or more), the intrinsics of ``fixed_cameras`` and the
    rigs not refined."""
    import pycolmap

    if not constant_points and not fixed_cameras:
        pycolmap.bundle_adjustment(rec, opts)
        return
    config = pycolmap.BundleAdjustmentConfig()
    for iid, image in rec.images.items():
        if image.has_pose:
            config.add_image(iid)
    for pid in constant_points:
        config.add_constant_point(pid)
    for cid in fixed_cameras:
        config.set_constant_cam_intrinsics(cid)
    if fixed_cameras:
        opts.refine_sensor_from_rig = False
    if not constant_points:
        config.fix_gauge(pycolmap.BundleAdjustmentGauge.THREE_POINTS)
    pycolmap.create_default_bundle_adjuster(opts, config, rec).solve()


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


MARKER_PIXEL_ACCURACY = 0.5  # px: how well a marker is measured in a photo (Metashape's default)


def _fixed_points(chunk: Chunk, rec, copies: int, points) -> tuple[list[int], set[int]]:
    """Markers as fixed 3D points of the reconstruction at the given positions (internal frame),
    observed where they were measured in the photos; ``points`` is a list of (marker, position).
    Each is added ``copies`` times: a marker measurement weighs that many tie point observations.
    Returns the point ids and the keys of the markers added (two photos or more)."""
    import pycolmap

    names = chunk.meta["sfm"]["names"]
    by_name = {img.name: iid for iid, img in rec.images.items() if img.has_pose}
    pids, added = [], set()
    for m, X in points:
        obs = []
        for cam_key, proj in m.projections.items():
            iid = by_name.get(names.get(str(cam_key), ""))
            if iid is not None and proj.measured:  # estimates from the model are not observations
                obs.append((iid, proj.x, proj.y))
        if len(obs) < 2:
            continue
        added.add(m.key)
        for _ in range(copies):
            track = pycolmap.Track()
            for iid, x, y in obs:  # marker projections are in COLMAP's pixel convention
                pts = rec.images[iid].points2D
                pts.append(pycolmap.Point2D(np.array([x, y], float)))
                track.add_element(iid, len(pts) - 1)
            pids.append(rec.add_point3D(np.asarray(X, float), track, np.array([255, 0, 0], np.uint8)))
    return pids, added


def _control_markers(chunk: Chunk) -> dict:
    """Markers with an enabled reference (not check points) -> their reference in the internal frame."""
    out = {}
    for m in chunk.markers:
        if not m.checkpoint and m.reference.enabled and m.reference.location:
            out[m.key] = chunk.to_internal(np.asarray([m.reference.location], float))[0]
    return out


def _control_points(chunk: Chunk, rec, copies: int) -> list[int]:
    """Markers with an enabled reference (not check points) as fixed 3D points of the
    reconstruction at their reference coordinates, observed where they were measured in the
    photos (see :func:`_fixed_points`)."""
    fixed = _control_markers(chunk)
    return _fixed_points(chunk, rec, copies, [(chunk.marker_by_key(k), X) for k, X in fixed.items()])[0]


def marker_precision(chunk: Chunk, marker) -> float | None:
    """Precision of a triangulated marker (internal units): the accuracy of its measurements in
    the photos (:data:`MARKER_PIXEL_ACCURACY`) carried to its distance from each camera and
    combined over the photos that see it; None when it is not placed."""
    if marker.position is None:
        return None
    inv = 0.0
    for cam_key in marker.projections:
        try:
            cam = chunk.camera(cam_key)
        except KeyError:
            continue
        sensor = chunk.sensors.get(cam.sensor_key)
        if cam.transform is None or sensor is None or not sensor.params:
            continue
        depth = float((marker.position - cam.transform[:3, 3]) @ cam.transform[:3, 2])
        if depth > 0:
            inv += (float(sensor.params[0]) / (MARKER_PIXEL_ACCURACY * depth)) ** 2
    return 1.0 / np.sqrt(inv) if inv > 0 else None


def bar_anchors(chunk: Chunk, bars, fixed: dict | None = None) -> dict:
    """Positions (internal frame) of the markers at the ends of control scale bars that give the
    bars their lengths, each marker moved as little as its precision allows: least squares of
    ``scale * length - distance`` (weighted by ``1 / accuracy**2``) and of the shifts (weighted by
    :func:`marker_precision`). Markers in ``fixed`` (control points: key -> internal position)
    stay where they are. Returns {marker key: position}."""
    from scipy.optimize import least_squares

    fixed = dict(fixed or {})
    s = float(chunk.scale)
    usable = [sb for sb in bars
              if all(chunk.marker_by_key(k).position is not None or k in fixed for k in (sb.marker_a, sb.marker_b))]
    keys = sorted({k for sb in usable for k in (sb.marker_a, sb.marker_b)} - set(fixed))
    if not usable or not keys:
        return {}
    start = {k: np.asarray(chunk.marker_by_key(k).position, float) for k in keys}
    sigma = {k: marker_precision(chunk, chunk.marker_by_key(k)) for k in keys}
    known = [v for v in sigma.values() if v]
    for k in keys:  # a marker without photos to judge it by: as the others
        sigma[k] = sigma[k] or (float(np.median(known)) if known else 1e-3 / s)
    idx = {k: i for i, k in enumerate(keys)}

    def pos(x, k):
        return fixed[k] if k in fixed else x[3 * idx[k]:3 * idx[k] + 3]

    def residuals(x):
        r = [(s * np.linalg.norm(pos(x, sb.marker_a) - pos(x, sb.marker_b)) - sb.distance) / max(sb.accuracy, 1e-6)
             for sb in usable]
        for k in keys:
            r.extend((x[3 * idx[k]:3 * idx[k] + 3] - start[k]) / sigma[k])
        return np.asarray(r)

    x0 = np.concatenate([start[k] for k in keys])
    x = least_squares(residuals, x0, x_scale=np.repeat([sigma[k] for k in keys], 3)).x
    return {k: x[3 * idx[k]:3 * idx[k] + 3] for k in keys}


def _marker_report(chunk: Chunk) -> dict:
    """RMSE of the control and check points and of the control and check scale bars (None: none)."""
    from ..markers import gcp

    out = {}
    rows = gcp.marker_errors(chunk)
    bar_rows = [r for r in gcp.scalebar_errors(chunk) if r["enabled"]]
    for kind in ("control", "check"):
        e = [r["error"] for r in rows if r["type"] == kind and r["error"] is not None]
        out[kind] = float(np.sqrt(np.mean(np.square(e)))) if e else None
        e = [r["error"] for r in bar_rows if r["type"] == kind and r["error"] is not None]
        out[f"scalebar_{kind}"] = float(np.sqrt(np.mean(np.square(e)))) if e else None
    return out


def _optimize_own(chunk: Chunk, fit_principal_point: bool, use_markers: bool, gnss: str, rolling_shutter: bool,
                  progress: Progress) -> float:
    """:func:`optimize_cameras` with our own adjustment (:mod:`.bundle`): control points weighted by
    their accuracy, scale bars as distances, GNSS positions of the photos (with a bias per flight),
    rolling shutter."""
    from ..markers import gcp
    from . import bundle

    rec = load_reconstruction(chunk)
    drop_missing_points(rec)
    controls = [m for m in chunk.markers if not m.checkpoint and m.reference.enabled and m.reference.location]
    bars = gcp.control_scalebars(chunk) if use_markers else []
    referenced = (use_markers and (len(controls) >= 3 or bars)) or gnss != "off"
    if referenced:
        gcp.update_transform(chunk)  # references, bars and GNSS positions in the internal frame
    before = _marker_report(chunk) if use_markers else {}
    pb, ids = bundle.problem_from_chunk(chunk, rec, use_markers=use_markers, gnss=gnss,
                                        rolling_shutter=rolling_shutter, fit_principal_point=fit_principal_point)
    res = bundle.adjust(pb, max_iterations=200, progress=progress.sub(0.05, 0.85))
    bundle.write_back(chunk, rec, ids, res)
    rec.update_point_3d_errors()
    rec.write(str(chunk.product_path("reconstruction")))
    import_reconstruction(chunk, rec)
    for n, key in enumerate(ids["markers"]):  # the adjusted marker positions, not a new triangulation
        chunk.marker_by_key(key).position = np.asarray(res.markers[n], float)
    if referenced:
        gcp.update_transform(chunk)
    err = float(rec.compute_mean_reprojection_error())
    lin = chunk.transform[:3, :3] if chunk.transform is not None else np.eye(3)
    report = {"engine": "ppg", "reprojection_error": err, "iterations": res.iterations, "converged": res.success,
              "control_points": len(pb.gcps), "scale_bars": len(pb.bars), "gnss_positions": len(pb.gnss),
              "gnss_bias": {str(f): [float(v) for v in lin @ b] for f, b in res.bias.items()}}
    if res.rolling_shutter is not None:
        speed = np.linalg.norm(lin @ res.rolling_shutter[:, :3].T, axis=0)
        report["rolling_shutter_m"] = {"median": float(np.median(speed)), "max": float(speed.max())}
    if use_markers:
        report.update({"before": before, "after": _marker_report(chunk)})
    chunk.meta.setdefault("sfm", {})["reprojection_error"] = err
    chunk.meta["sfm"]["optimize"] = report
    progress(1.0, f"Mean reprojection error {err:.3f} px")
    return err


def optimize_cameras(chunk: Chunk, fit_principal_point: bool = False, use_markers: bool = True,
                     marker_weight: int = 4, progress: Progress | None = None, engine: str = "colmap",
                     gnss: str = "off", rolling_shutter: bool = False) -> float:
    """Global bundle adjustment of poses, calibration and tie points.

    With ``use_markers`` the control points and the control scale bars take part in it, as in
    Metashape. Control points (markers with an enabled reference and two or more projections;
    check points are left out) are fixed points at their reference coordinates, so the block
    bends to fit them (a deformation such as the "dome" of a wrong radial distortion is
    corrected, not only the similarity of the georeference). Scale bars: the markers at their
    ends are fixed where the bars get their lengths, moved as little as the precision of each
    marker allows (:func:`bar_anchors`): with two bars or more, or with control points, the block
    bends towards the lengths (a single bar only gives the scale, set by the georeference). The
    constrained adjustment needs three fixed points or more, not on a line. The report
    (reprojection error, control/check point and bar errors before and after) goes into
    ``chunk.meta["sfm"]["optimize"]``. Returns the mean reprojection error in pixels.

    ``engine="ppg"``: our own adjustment (:mod:`.bundle`) instead of COLMAP's, with the control
    points pulled to their coordinates by their accuracy, the bars as distances between their
    markers, the GNSS positions of the photos (``gnss`` "plain", or "bias" with a bias per
    flight) and the ``rolling_shutter`` of the camera (a velocity per photo)."""
    import pycolmap

    from ..markers import gcp

    progress = as_progress(progress)
    if engine == "ppg":
        return _optimize_own(chunk, fit_principal_point, use_markers, gnss, rolling_shutter, progress)
    if engine != "colmap":
        raise ValueError(f"unknown adjustment engine {engine!r}: colmap or ppg")
    rec = load_reconstruction(chunk)
    drop_missing_points(rec)  # models written from imported poses by earlier versions

    def errors():
        out = {}
        rows = gcp.marker_errors(chunk)
        bar_rows = [r for r in gcp.scalebar_errors(chunk) if r["enabled"]]
        for kind in ("control", "check"):
            e = [r["error"] for r in rows if r["type"] == kind and r["error"] is not None]
            out[kind] = float(np.sqrt(np.mean(np.square(e)))) if e else None
            e = [r["error"] for r in bar_rows if r["type"] == kind and r["error"] is not None]
            out[f"scalebar_{kind}"] = float(np.sqrt(np.mean(np.square(e)))) if e else None
        return out

    controls = [m for m in chunk.markers if not m.checkpoint and m.reference.enabled and m.reference.location]
    bars = gcp.control_scalebars(chunk) if use_markers else []
    with_gcps = use_markers and len(controls) >= 3
    constrained = with_gcps or len(bars) >= 2  # fewer control points do not place the chunk
    if constrained:
        gcp.update_transform(chunk)  # the reference coordinates and the bars in the internal frame
    before = errors() if constrained else {}
    progress(0.1, "Optimizing camera alignment" + (" on the control points and scale bars" if constrained else ""))
    _square_pixels(chunk, rec)
    opts = pycolmap.BundleAdjustmentOptions()
    try:
        opts.refine_principal_point = fit_principal_point
    except AttributeError:  # pragma: no cover - option name differs between versions
        pass
    fixed_cams = _rec_fixed_cameras(chunk, rec)
    if constrained:  # the block first finds its own shape and calibration, then bends to the references
        _adjust(rec, opts, fixed_cameras=fixed_cams)
        _restore_sensor_models(chunk, rec)
        import_reconstruction(chunk, rec)
        gcp.update_transform(chunk)
        _square_pixels(chunk, rec)
        progress(0.5, "Optimizing camera alignment on the control points and scale bars")
    copies = max(1, int(marker_weight))
    points = []
    if constrained:
        fixed = _control_markers(chunk) if with_gcps else {}
        anchors = bar_anchors(chunk, bars, fixed) if bars else {}
        points = [(chunk.marker_by_key(k), X) for k, X in {**fixed, **anchors}.items()]
    pids, used = _fixed_points(chunk, rec, copies, points) if points else ([], set())
    placed = np.array([rec.points3D[pid].xyz for pid in pids[::copies]]) if pids else np.zeros((0, 3))
    if len(placed) >= 3 and gcp._non_collinear(placed):
        _adjust(rec, opts, constant_points=pids, fixed_cameras=fixed_cams)
    else:
        pids, points = _drop_points(rec, pids), []
        _adjust(rec, opts, fixed_cameras=fixed_cams)
    pids = _drop_points(rec, pids)
    _restore_sensor_models(chunk, rec)
    rec.update_point_3d_errors()  # the adjustment does not refresh the stored errors
    rec.write(str(chunk.product_path("reconstruction")))
    import_reconstruction(chunk, rec)
    err = float(rec.compute_mean_reprojection_error())
    used = used if points else set()
    report = {"reprojection_error": err,
              "control_points": sum(1 for m in controls if m.key in used) if with_gcps else 0,
              "scale_bars": sum(1 for sb in bars if {sb.marker_a, sb.marker_b} <= used)}
    if points or gcp.control_scalebars(chunk):
        gcp.update_transform(chunk)  # the bars and the control points on the adjusted block
    else:
        chunk.triangulate_markers()
    if points:
        report.update({"before": before, "after": errors()})
    chunk.meta.setdefault("sfm", {})["reprojection_error"] = err
    chunk.meta["sfm"]["optimize"] = report
    progress(1.0, f"Mean reprojection error {err:.3f} px")
    return err


def _drop_points(rec, pids: list[int]) -> list[int]:
    """Take the fixed marker points out of the reconstruction again (their observations were
    appended last to their images). Returns []."""
    added: dict[int, int] = {}
    for pid in pids:
        for el in rec.points3D[pid].track.elements:
            added[el.image_id] = added.get(el.image_id, 0) + 1
        rec.delete_point3D(pid)
    for iid, n in added.items():
        pts = rec.images[iid].points2D
        for _ in range(n):
            pts.pop()
    return []


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
