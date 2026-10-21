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

from .. import _native
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


REFERENCE_NEIGHBOURS = 20  # the pairs of every photo with its nearest ones (by position) or the ones around it (in sequence)
PRESELECTIONS = ("source", "estimated", "sequential")  # the reference preselections, named as Metashape names them
MASK_MODES = ("none", "keypoints", "tiepoints")  # what the masks of the photos apply to


def _nearest_pairs(positions: dict[str, np.ndarray], neighbours: int) -> set[tuple[str, str]]:
    """Every photo with its ``neighbours`` nearest ones (planimetric distance)."""
    from scipy.spatial import cKDTree

    names = list(positions)
    if len(names) < 2:
        return set()
    pts = np.array([np.asarray(positions[n], float)[:2] for n in names])
    tree = cKDTree(pts)
    k = min(neighbours + 1, len(names))
    _, idx = tree.query(pts, k=k)
    pairs = set()
    for i, row in enumerate(np.atleast_2d(idx)):
        for j in row:
            if i != j:
                pairs.add(tuple(sorted((names[i], names[int(j)]))))
    return pairs


def _reference_positions(chunk: Chunk, names: dict[int, str]) -> dict[str, np.ndarray]:
    """The reference positions of the photos (the GPS of the drone) on metric axes, by name."""
    cams = [c for c in chunk.cameras if c.enabled and c.key in names and c.reference.location]
    if not cams:
        return {}
    pts = np.array([c.reference.location for c in cams], float)
    src = cams[0].meta.get("reference_crs", chunk.crs)
    if crs_mod.is_geographic(src):
        utm = crs_mod.utm_for_lonlat(*pts[:, :2].mean(0))
        pts = crs_mod.transform_points(pts, src, utm)
    return {names[c.key]: p for c, p in zip(cams, pts)}


def _reference_pairs(chunk: Chunk, names: dict[int, str], neighbors: int = REFERENCE_NEIGHBOURS) -> list | None:
    """Candidate pairs from the reference positions of the cameras - the ``source`` preselection:
    every photo with its ``neighbors`` nearest - or None with fewer than three positions."""
    positions = _reference_positions(chunk, names)
    if len(positions) < 3:
        return None
    return sorted(_nearest_pairs(positions, neighbors))


def _estimated_pairs(chunk: Chunk, names: dict[int, str], neighbours: int = REFERENCE_NEIGHBOURS) -> list | None:
    """Candidate pairs from the positions a previous alignment estimated - the ``estimated``
    preselection: every aligned photo with its ``neighbours`` nearest, the photos not aligned
    yet with every other photo - or None without an alignment."""
    aligned = {names[c.key]: np.asarray(c.center, float) for c in chunk.cameras
               if c.enabled and c.key in names and c.transform is not None}
    if len(aligned) < 3:
        return None
    pairs = _nearest_pairs(aligned, neighbours)
    everyone = list(names.values())
    for a in everyone:
        if a not in aligned:
            pairs.update(tuple(sorted((a, b))) for b in everyone if b != a)
    return sorted(pairs)


def _sequential_pairs(chunk: Chunk, names: dict[int, str], overlap: int = REFERENCE_NEIGHBOURS) -> list:
    """Candidate pairs of photos taken one after the other - the ``sequential`` preselection, a
    video or a walk around an object: every photo with the ``overlap`` before and after it in
    the order of the capture times (the file names where the EXIF has none)."""
    cams = [c for c in chunk.cameras if c.enabled and c.key in names]
    order = sorted(cams, key=lambda c: (str(c.meta.get("datetime") or ""), names[c.key]))
    seq = [names[c.key] for c in order]
    pairs = set()
    for i, a in enumerate(seq):
        for b in seq[i + 1:i + 1 + max(int(overlap), 1)]:
            pairs.add(tuple(sorted((a, b))))
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


def _preselection(pair_selection: str, generic, reference) -> tuple[bool | None, str | None]:
    """The generic and reference preselections asked for: by ``generic`` and ``reference`` when
    either is given, else by the older ``pair_selection`` word."""
    if generic is None and reference is None:
        words = {"auto": (None, None), "exhaustive": (False, None), "generic": (True, None),
                 "reference": (False, "source"), "sequential": (False, "sequential")}
        if pair_selection not in words:
            raise ValueError(f"unknown pair selection {pair_selection!r}")
        return words[pair_selection]
    if reference in ("", "none"):
        reference = None
    if reference is not None and reference not in PRESELECTIONS:
        raise ValueError(f"unknown reference preselection {reference!r}: one of {', '.join(PRESELECTIONS)}")
    return (None if generic is None else bool(generic)), reference


def _db_image_names(db_path: Path) -> set[str]:
    import pycolmap

    db = pycolmap.Database.open(str(db_path))
    try:
        return {im.name for im in db.read_all_images()}
    finally:
        db.close()


def _continuable(chunk: Chunk, db_path: Path, root, names: dict[int, str]) -> bool:
    """Whether the matches and the alignment of the chunk can take more photos: database and
    model exist, the photos sit in the same folder, every photo of the database is still there."""
    info = chunk.meta.get("sfm") or {}
    model = chunk.product_path("reconstruction")
    if not db_path.exists() or model is None or not model.exists() or info.get("image_root") != str(root):
        return False
    return _db_image_names(db_path) <= set(names.values())


GPU_CACHE_PHOTOS = 96  # descriptors of this many photos kept in memory for the GPU stream


def _match_with_gpu(db_path: Path, pairs, mopts, devices: list[int], progress: Progress, work: Path) -> dict:
    """The pairs matched by the GPUs and the CPU together: one queue of pairs, a stream per GPU
    (:func:`_native.gpu_match`, a thread that lets the GIL go) taking them one by one from the
    front, the CPU stream (COLMAP's matcher, with its geometric verification) in chunks from the
    back - a first chunk to measure it by, then the rest split by the speeds measured, since every
    call re-reads the features - each as fast as it goes until the queue is empty. The matches of
    the GPUs go into the database between the chunks of the CPU and are verified by COLMAP at the
    end (``verify_matches``), so the alignment sees no difference. The pairs are sorted: a GPU
    keeps the descriptors of the current photo on the device."""
    import collections
    import sqlite3
    import threading
    import time

    import pycolmap

    db = pycolmap.Database.open(str(db_path))
    try:
        ids = {im.name: int(im.image_id) for im in db.read_all_images()}
    finally:
        db.close()
    by_id = {v: k for k, v in ids.items()}
    queue = collections.deque(sorted(pairs))
    total = len(queue)
    lock = threading.Lock()
    results: dict[tuple[int, int], np.ndarray] = {}
    done = {"gpu": 0, "cpu": 0}
    by_device = {d: 0 for d in devices}
    failure: list[BaseException] = []
    cache: collections.OrderedDict[int, np.ndarray] = collections.OrderedDict()
    sift = mopts.sift
    # the descriptors are read without taking any lock on the database (sqlite ``immutable``: the
    # rows of the descriptors never change while the CPU matcher writes its matches): a reader
    # holding a shared lock would make COLMAP's commit fail, and COLMAP aborts the process on it
    reader = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, check_same_thread=False)

    def descriptors(image_id: int) -> np.ndarray:
        with lock:
            got = cache.get(image_id)
            if got is not None:
                cache.move_to_end(image_id)
                return got
            row = reader.execute("SELECT rows, cols, data FROM descriptors WHERE image_id = ?", (int(image_id),)).fetchone()
            if row is None or not row[2]:
                got = np.zeros((0, 128), np.uint8)
            else:
                got = np.ascontiguousarray(np.frombuffer(row[2], np.uint8).reshape(int(row[0]), int(row[1])))
            cache[image_id] = got
            while len(cache) > GPU_CACHE_PHOTOS:
                cache.popitem(last=False)
            return got

    def gpu_stream(device: int):
        try:
            while True:
                with lock:
                    if not queue or failure:
                        return
                    a, b = queue.popleft()
                ia, ib = ids[a], ids[b]
                m = _native.gpu_match(descriptors(ia), descriptors(ib), sift.max_ratio, sift.max_distance,
                                      sift.cross_check, device, a_key=ia, b_key=ib)
                with lock:
                    results[(ia, ib)] = np.asarray(m, dtype=np.uint32)
                    done["gpu"] += 1
                    by_device[device] += 1
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            with lock:
                failure.append(exc)

    gpu_pairs: list[tuple[str, str]] = []

    def flush() -> None:
        """The matches of the GPUs so far into the database (between the chunks of the CPU)."""
        with lock:
            batch = dict(results)
            results.clear()
        if not batch:
            return
        d = pycolmap.Database.open(str(db_path))
        try:
            for (ia, ib), m in batch.items():
                d.write_matches(ia, ib, np.ascontiguousarray(m.reshape(-1, 2), dtype=np.uint32))
                gpu_pairs.append((by_id[ia], by_id[ib]))
        finally:
            d.close()

    threads = [threading.Thread(target=gpu_stream, args=(d,), name=f"ppg-gpu-matching-{d}", daemon=True)
               for d in devices]
    started = time.perf_counter()
    for t in threads:
        t.start()
    least = max(8, 4 * (os.cpu_count() or 4))
    list_path = work / "pairs_cpu.txt"
    cpu_rate = None  # pairs per second of the CPU stream, measured on its first chunk
    try:
        while True:
            with lock:
                if failure or not queue:
                    break
                left = len(queue)
                if cpu_rate is None:
                    n = max(1, left // 4)  # a first chunk to measure the CPU by, the GPUs meanwhile measured too
                else:
                    gpu_rate = done["gpu"] / max(time.perf_counter() - started, 1e-3)
                    n = int(round(left * cpu_rate / max(cpu_rate + gpu_rate, 1e-9)))
                n = min(left, max(n, min(least, left)))
                batch = [queue.pop() for _ in range(n)]
            list_path.write_text("\n".join(f"{a} {b}" for a, b in batch))
            popts = pycolmap.ImportedPairingOptions()
            popts.match_list_path = str(list_path)
            t0 = time.perf_counter()
            pycolmap.match_image_pairs(str(db_path), matching_options=mopts, pairing_options=popts)
            cpu_rate = n / max(time.perf_counter() - t0, 1e-3)
            with lock:
                done["cpu"] += n
                g, c = done["gpu"], done["cpu"]
            flush()
            progress(0.55 + 0.4 * min(1.0, (g + c) / max(total, 1)),
                     f"Matching photos: {g:,} pairs on the GPU, {c:,} on the CPU")
    finally:
        for t in threads:
            t.join()
        reader.close()
    if failure:
        raise RuntimeError(f"GPU matching failed: {failure[0]}") from failure[0]
    flush()
    if gpu_pairs:  # COLMAP's geometric verification of what the GPUs matched
        progress(0.95, f"Verifying {len(gpu_pairs):,} pairs matched on the GPU")
        gpu_list = work / "pairs_gpu.txt"
        gpu_list.write_text("\n".join(f"{a} {b}" for a, b in gpu_pairs))
        pycolmap.verify_matches(str(db_path), str(gpu_list), pycolmap.TwoViewGeometryOptions())
    return {"gpu_pairs": done["gpu"], "cpu_pairs": done["cpu"], "by_device": by_device}


def match_photos(chunk: Chunk, quality: str = "high", keypoint_limit: int = 40000, tiepoint_limit: int = 8000,
                 pair_selection: str = "auto", progress: Progress | None = None,
                 exclude_stationary: bool = False, generic: bool | None = None, reference: str | None = None,
                 neighbours: int = REFERENCE_NEIGHBOURS, apply_masks: str = "keypoints",
                 guided_matching: bool = False, reset: bool = True, gpu: str | None = None) -> None:
    """Detect and match the points of the photos (SIFT), with the choices of Metashape's *Align
    Photos*: ``quality`` (the accuracy: the photos at full size or reduced), the ``generic``
    preselection (the pairs that overlap found first on the photos reduced to 1024 pixels,
    :func:`_preselected_pairs`; None: from ``EXHAUSTIVE_UP_TO`` photos up), the ``reference``
    preselection - ``source`` (every photo with its ``neighbours`` nearest by the reference
    positions, the GPS of the drone), ``estimated`` (by the positions of a previous alignment),
    ``sequential`` (the photos before and after it in the order of the capture) - with the pairs
    of both preselections matched when both are on and every pair when neither is. The older
    ``pair_selection`` word (``auto``, ``exhaustive``, ``generic``, ``sequential``, ``reference``)
    stands in when ``generic`` and ``reference`` are not given.

    ``apply_masks``: the masks of the photos (:mod:`.masks`) applied to the ``keypoints`` (no
    point detected in the masked parts), to the ``tiepoints`` (a point masked in one photo is
    dropped everywhere after the alignment: masking a few photos is enough) or to ``none``.
    ``guided_matching``: the matches searched again with the geometry of the pair (more tie
    points, slower). ``exclude_stationary``: matches that do not move between two photos are
    dropped - the background of a turntable photographed from a fixed camera. ``reset`` False
    keeps the matches and the alignment there are and only detects and matches the photos added
    since (:func:`align_cameras` then registers them into the block); when they cannot be kept
    everything is done again. The faces of 360-degree photos (:mod:`.panorama`) become the
    frames of a rig."""
    import pycolmap

    from .masks import colmap_mask_folder
    from .panorama import panorama_cameras, rig_configs

    progress = as_progress(progress)
    if apply_masks not in MASK_MODES:
        raise ValueError(f"apply_masks: one of {', '.join(MASK_MODES)}")
    generic, reference = _preselection(pair_selection, generic, reference)
    work = _sfm_dir(chunk)
    db_path = work / "database.db"
    root, names = image_root_and_names(chunk)
    rigs = rig_configs(chunk, names)
    keep = (not reset) and not rigs and _continuable(chunk, db_path, root, names)
    if not reset and not keep:
        progress(0.0, "No alignment to continue: every photo again")
    if not keep and db_path.exists():
        db_path.unlink()
    existing = _db_image_names(db_path) if keep else set()
    downscale = QUALITY_DOWNSCALE.get(quality, 2)

    by_sensor: dict[int, list[str]] = {}
    for c in chunk.cameras:
        if c.enabled:
            by_sensor.setdefault(c.sensor_key, []).append(names[c.key])
    new = [n for group in by_sensor.values() for n in group if n not in existing]

    masks = colmap_mask_folder(chunk, names, work / "masks") if apply_masks == "keypoints" else None
    faces = {c.sensor_key for c in panorama_cameras(chunk) if c.enabled} if rigs else set()
    progress(0.0, f"Detecting points in {len(new)} photos" + (" (with masks)" if masks else ""))
    for i, (skey, image_names) in enumerate(by_sensor.items()):
        image_names = [n for n in image_names if n not in existing]
        if not image_names:
            continue
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
    mopts.guided_matching = bool(guided_matching)
    n = len(names)
    if generic is None:
        generic = n > EXHAUSTIVE_UP_TO and not rigs  # every pair is cheap enough up to there
    pairs: set[tuple[str, str]] | None = None
    parts = []
    if generic:
        progress(0.5, "Finding the pairs of photos that overlap")
        found = _preselected_pairs(chunk, work, root, by_sensor, masks, names)
        if found:  # nothing found on the small images: every pair
            pairs, parts = set(found), ["generic"]
    if reference:
        finder = {"source": _reference_pairs, "estimated": _estimated_pairs, "sequential": _sequential_pairs}[reference]
        found = finder(chunk, names, max(int(neighbours), 1))
        if found:
            pairs = (pairs or set()) | set(found)
            parts.append(reference)
        elif not pairs:
            raise RuntimeError(f"the {reference} preselection needs "
                               + ("camera reference positions" if reference == "source" else "an alignment"))
    mode = "+".join(parts) if pairs else "exhaustive"
    if keep:  # only the pairs with a photo added since
        added, everyone = set(new), list(names.values())
        if pairs is None:
            pairs = {tuple(sorted((a, b))) for a in added for b in everyone if a != b}
        else:
            pairs = {p for p in pairs if p[0] in added or p[1] in added}
    # the GPUs: the pairs shared with the CPU (not for rigs, nor with the guided matching, which is COLMAP's)
    devices, gpu_names = [], []
    if not rigs and not guided_matching:
        for d in _native.gpu_devices(gpu):
            try:
                gpu_names.append(_native.kernel("gpu_device_name")(d))
                devices.append(d)
            except Exception as exc:  # noqa: BLE001 - a GPU that does not work: the others, or the CPU alone
                log.warning("GPU %s unavailable for the matching (%s)", d, exc)
    gpu_name = ", ".join(gpu_names) if devices else None
    if devices and pairs is None:
        everyone = sorted(names.values())
        pairs = {(a, b) for i, a in enumerate(everyone) for b in everyone[i + 1:]}
    progress(0.55, f"Matching photos ({mode}" + (f", {len(pairs):,} pairs)" if pairs is not None else ")")
             + (f" on {gpu_name} and the CPU" if devices else ""))
    shares = None
    if pairs is None:
        pycolmap.match_exhaustive(str(db_path), matching_options=mopts)
    elif pairs and devices:
        shares = _match_with_gpu(db_path, pairs, mopts, devices, progress, work)
    elif pairs:
        list_path = work / "pairs.txt"
        list_path.write_text("\n".join(f"{a} {b}" for a, b in sorted(pairs)))
        popts = pycolmap.ImportedPairingOptions()
        popts.match_list_path = str(list_path)
        pycolmap.match_image_pairs(str(db_path), matching_options=mopts, pairing_options=popts)
    removed = remove_stationary_matches(db_path) if exclude_stationary else None
    old = chunk.meta.get("sfm") or {}
    chunk.meta["sfm"] = {"image_root": str(root), "names": {str(k): v for k, v in names.items()},
                         "quality": quality, "pair_selection": mode, "pairs": len(pairs) if pairs is not None else None,
                         "generic": bool(generic), "reference": reference, "neighbours": int(neighbours),
                         "masks": masks is not None, "apply_masks": apply_masks, "guided_matching": bool(guided_matching),
                         "stationary_removed": removed, "rigs": len(rigs),
                         "keypoint_limit": int(keypoint_limit), "tiepoint_limit": int(tiepoint_limit),
                         "continued": keep, "added": new if keep else None,
                         "gpu": gpu_name if shares else None, "gpu_pairs": shares["gpu_pairs"] if shares else 0,
                         "gpu_shares": ({name: shares["by_device"].get(d, 0) for d, name in zip(devices, gpu_names)}
                                        if shares else None),
                         "cpu_pairs": shares["cpu_pairs"] if shares else (len(pairs) if pairs else None)}
    if keep and old.get("gps_priors"):
        chunk.meta["sfm"]["gps_priors"] = old["gps_priors"]  # the priors already in the database: same axes
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


GLOBAL_FIT_FACTOR = 3.0  # cameras of a global block may miss their GPS by this times its accuracy (median, horizontal)


def fits_references(centres, references, accuracies, factor: float = GLOBAL_FIT_FACTOR,
                    floor: float = 1.0) -> tuple[bool, float]:
    """Whether the camera ``centres`` of a reconstruction (internal frame) sit on their
    ``references`` (metres, projected axes; ``accuracies`` in metres) once a single similarity
    places them: the 90th percentile of the horizontal residuals within ``factor`` times the
    median accuracy (never less than ``floor`` metres). A sound survey fits within the noise of
    its GPS (Tell Sultan, 425 photos: 2.2 m for an accuracy of 7 m); a block the global mapper
    assembled from parts at different scales fits no similarity at all (Tell Tuqan, 997 photos:
    115 m, with every part consistent inside and the targets reprojected within 3 px). Fewer
    than ten references, or references spanning less than ten times their accuracy (an object,
    a trench), cannot judge the block: ``(True, nan)``. Returns ``(fits, residual)``."""
    from ..core import geometry

    src = np.asarray(centres, float).reshape(-1, 3)
    dst = np.asarray(references, float).reshape(-1, 3)
    acc = np.asarray(accuracies, float).reshape(-1)
    if len(src) < 10 or len(src) != len(dst):
        return True, float("nan")
    typical = float(np.median(acc))
    if float(np.linalg.norm(np.ptp(dst[:, :2], axis=0))) < 10.0 * typical:
        return True, float("nan")
    m = geometry.umeyama(src, dst, with_scale=True)
    placed = (m[:3, :3] @ src.T).T + m[:3, 3]
    residual = float(np.percentile(np.linalg.norm(placed[:, :2] - dst[:, :2], axis=1), 90))
    return residual <= max(factor * typical, floor), residual


def _block_fits_references(chunk: Chunk, rec) -> tuple[bool, float]:
    """:func:`fits_references` for a reconstruction of the chunk's photos against the positions
    of their cameras (the GPS of the photos), brought onto metric axes (the UTM zone of the
    site when they are geographic)."""
    from ..markers.gcp import projected_crs_for

    names = {v: int(k) for k, v in chunk.meta["sfm"]["names"].items()}
    by_key = {c.key: c for c in chunk.cameras}
    groups: dict[str, list] = {}
    for image in rec.images.values():
        cam = by_key.get(names.get(image.name)) if image.has_pose else None
        if cam is None or not (cam.reference.enabled and cam.reference.location):
            continue
        groups.setdefault(cam.meta.get("reference_crs", chunk.crs), []).append((image, cam))
    src, dst, acc = [], [], []
    for ref_crs, pairs in groups.items():
        locs = np.array([cam.reference.location for _, cam in pairs], float).reshape(-1, 3)
        if crs_mod.is_geographic(ref_crs):
            locs = crs_mod.transform_points(locs, ref_crs, projected_crs_for(ref_crs, locs[0]))
        for (image, cam), loc in zip(pairs, locs):
            src.append(np.asarray(image.projection_center(), float))
            dst.append(loc)
            acc.append(float(np.mean(cam.reference.accuracy[:2])) if cam.reference.accuracy else 5.0)
    return fits_references(src, dst, acc) if src else (True, float("nan"))


def _write_pose_priors(chunk: Chunk, db_path: Path, names: dict[int, str], previous: dict | None = None) -> dict | None:
    """The reference positions of the photos (the GPS of the drone, with their accuracy) into the
    COLMAP database as pose priors, on metric axes less a round offset: the incremental mapper
    then holds the block to them (``use_prior_position``), as Metashape's alignment holds it to
    the camera references. ``previous`` (the report of an earlier call, when the database is
    kept) keeps the same axes and only gives the photos without a prior theirs. Returns the
    report (``count``, ``crs``, ``offset``), None with fewer than three positions."""
    import pycolmap

    from ..markers.gcp import projected_crs_for

    cams = [c for c in chunk.cameras
            if c.enabled and c.key in names and c.reference.enabled and c.reference.location]
    if len(cams) < 3:
        return None
    pts = np.array([c.reference.location for c in cams], float)
    src = cams[0].meta.get("reference_crs", chunk.crs)
    if previous and previous.get("crs"):
        crs, offset = previous["crs"], np.asarray(previous["offset"], float)
    else:
        crs = projected_crs_for(src, pts[0, :2]) if crs_mod.is_geographic(src) else src
        offset = None
    if crs != src:
        pts = crs_mod.transform_points(pts, src, crs)
    if offset is None:
        offset = np.round(pts.mean(axis=0))
    by_name = {names[c.key]: (p - offset, c.reference.accuracy or [5.0, 5.0, 10.0]) for c, p in zip(cams, pts)}
    db = pycolmap.Database.open(str(db_path))
    try:
        images = db.read_all_images()
        have = set()
        if previous and db.num_pose_priors():  # the priors written before stay
            for image in images:
                try:
                    if db.exists_pose_prior(image.image_id, True):
                        have.add(image.name)
                except Exception:  # pragma: no cover - the lookup differs between versions
                    pass
        n = 0
        for image in images:
            got = by_name.get(image.name)
            if got is None or image.name in have:
                continue
            pos, acc = got
            acc = np.resize(np.maximum(np.asarray(acc, float).reshape(-1)[:3], 1e-3), 3)
            prior = pycolmap.PosePrior(position=np.asarray(pos, float), position_covariance=np.diag(acc ** 2),
                                       coordinate_system=pycolmap.PosePriorCoordinateSystem.CARTESIAN)
            prior.corr_data_id = image.data_id
            db.write_pose_prior(prior)
            n += 1
    finally:
        db.close()
    return {"count": n + len(have), "crs": crs, "offset": [float(v) for v in offset]}


def _drop_masked_tie_points(chunk: Chunk, rec, names: dict[int, str]) -> int:
    """The masks applied to the tie points: every 3D point with an observation inside the masked
    part of a photo is dropped (masking a few photos is enough). Returns how many."""
    from . import masks as masks_mod

    by_name = {v: k for k, v in names.items()}
    drop = set()
    for image in rec.images.values():
        if not image.has_pose or image.name not in by_name:
            continue
        cam = chunk.camera(by_name[image.name])
        if masks_mod.mask_path(chunk, cam) is None:
            continue
        idxs = list(image.get_observation_point2D_idxs())
        if not idxs:
            continue
        px = np.array([image.points2D[i].xy for i in idxs], float)
        hidden = masks_mod.hidden_by_mask(chunk, cam, px)
        drop.update(int(image.points2D[i].point3D_id) for i, h in zip(idxs, hidden) if h)
    for pid in drop:
        if rec.exists_point3D(pid):
            rec.delete_point3D(pid)
    return len(drop)


ADAPTIVE_MIN_GAIN = 0.01  # the wider lens model is kept when the reprojection error drops by this share at least
ADAPTIVE_ITERATIONS = 50


def _sane_camera(cam) -> bool:
    """Intrinsics within physical bounds: the two focal lengths within 2 % of each other, the
    principal point within 5 % of the centre, tangential distortion below 0.01, radial below 1."""
    p = [float(v) for v in cam.params]
    name = cam.model.name
    if name not in ("OPENCV", "OPENCV_FISHEYE", "PINHOLE", "FULL_OPENCV"):
        return True
    w, h = float(cam.width), float(cam.height)
    fx, fy, cx, cy = p[:4]
    if fx <= 0 or fy <= 0 or abs(fx / fy - 1.0) > 0.02:
        return False
    if abs(cx - w / 2) > 0.05 * w or abs(cy - h / 2) > 0.05 * h:
        return False
    if name in ("OPENCV", "OPENCV_FISHEYE", "FULL_OPENCV") and (abs(p[4]) > 1.0 or abs(p[5]) > 1.0):
        return False
    if name in ("OPENCV", "FULL_OPENCV") and (abs(p[6]) > 0.01 or abs(p[7]) > 0.01):
        return False
    return True


def adaptive_camera_fit(chunk: Chunk, rec, fixed_cameras=()) -> tuple:
    """Metashape's *adaptive camera model fitting*, done our way: the parameters of the lens that
    only a sound block can bear - the principal point, the tangential distortion, two focal
    lengths - are refined on a copy of the reconstruction and kept only when they prove
    reliable: the mean reprojection error drops by :data:`ADAPTIVE_MIN_GAIN` at least and every
    camera stays within physical bounds (:func:`_sane_camera`). Returns ``(reconstruction,
    report)``: the copy when kept, ``rec`` itself otherwise."""
    import pycolmap

    rec.update_point_3d_errors()
    before = float(rec.compute_mean_reprojection_error())
    trial = pycolmap.Reconstruction(rec)
    widened = 0
    for cid, cam in trial.cameras.items():
        if cid in fixed_cameras:
            continue
        p = [float(v) for v in cam.params]
        if cam.model.name == "RADIAL":
            cam.model, cam.params = pycolmap.CameraModelId.OPENCV, [p[0], p[0], p[1], p[2], p[3], p[4], 0.0, 0.0]
        elif cam.model.name == "SIMPLE_PINHOLE":
            cam.model, cam.params = pycolmap.CameraModelId.PINHOLE, [p[0], p[0], p[1], p[2]]
        elif cam.model.name == "RADIAL_FISHEYE":
            cam.model, cam.params = pycolmap.CameraModelId.OPENCV_FISHEYE, [p[0], p[0], p[1], p[2], p[3], p[4], 0.0, 0.0]
        elif cam.model.name not in ("OPENCV", "PINHOLE", "OPENCV_FISHEYE", "FULL_OPENCV"):
            continue
        widened += 1
    if not widened:
        return rec, {"kept": False, "before_px": before, "after_px": before, "why": "no camera to widen"}
    opts = pycolmap.BundleAdjustmentOptions()
    opts.refine_focal_length = True
    opts.refine_principal_point = True
    opts.refine_extra_params = True
    _adjust(trial, _at_most(opts, ADAPTIVE_ITERATIONS), fixed_cameras=fixed_cameras)
    trial.update_point_3d_errors()
    after = float(trial.compute_mean_reprojection_error())
    sane = all(_sane_camera(c) for cid, c in trial.cameras.items() if cid not in fixed_cameras)
    kept = bool(sane and np.isfinite(after) and after <= before * (1.0 - ADAPTIVE_MIN_GAIN))
    report = {"kept": kept, "before_px": before, "after_px": after, "sane": bool(sane)}
    log.info("adaptive camera model fitting: %.3f -> %.3f px, %s", before, after, "kept" if kept else "not kept")
    return (trial if kept else rec), report


def _continuation_model(model_dir: Path, db_path: Path, out: Path) -> Path:
    """A copy of the model with every camera in the model of the database, for the alignment to
    continue from it (the model is kept in the models of the sensors, OPENCV; the database has the
    cameras COLMAP matched the photos with, RADIAL for square pixels: COLMAP wants them equal)."""
    import pycolmap

    rec = pycolmap.Reconstruction(str(model_dir))
    db = pycolmap.Database.open(str(db_path))
    try:
        wanted = {int(c.camera_id): c.model.name for c in db.read_all_cameras()}
    finally:
        db.close()
    back = {("OPENCV", "RADIAL"): lambda p: [(p[0] + p[1]) / 2, p[2], p[3], p[4], p[5]],
            ("PINHOLE", "SIMPLE_PINHOLE"): lambda p: [(p[0] + p[1]) / 2, p[2], p[3]],
            ("OPENCV_FISHEYE", "RADIAL_FISHEYE"): lambda p: [(p[0] + p[1]) / 2, p[2], p[3], p[4], p[5]]}
    for cid, cam in rec.cameras.items():
        want = wanted.get(cid)
        if want is None or want == cam.model.name:
            continue
        convert = back.get((cam.model.name, want))
        if convert is None:
            raise RuntimeError(f"camera {cid}: a {cam.model.name} model cannot continue as {want}")
        cam.model, cam.params = getattr(pycolmap.CameraModelId, want), convert([float(v) for v in cam.params])
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    rec.write(str(out))
    return out


def align_cameras(chunk: Chunk, progress: Progress | None = None, mapper: str = "auto", reset: bool = True,
                  use_gps: bool | None = None, adaptive_fitting: bool = False) -> int:
    """Cameras and tie points from the matches of :func:`match_photos`. ``mapper``: ``incremental``
    (one photo after the other, COLMAP), ``global`` (all at once, GLOMAP: much faster with many
    photos) or ``auto`` - global from ``GLOBAL_FROM`` photos, incremental for fewer, for 360-degree
    rigs and calibrations held fixed, and whenever the global one leaves photos out or builds a
    block whose cameras do not sit on the GPS of the photos (:func:`fits_references`).

    ``use_gps`` (None: when the photos have them): the reference positions of the photos, with
    their accuracy, hold the block during the incremental alignment - COLMAP's pose priors,
    :func:`_write_pose_priors` - as Metashape's alignment holds it to the camera references: a
    long chain or a ring of photos no longer drifts. ``adaptive_fitting``: Metashape's adaptive
    camera model fitting (:func:`adaptive_camera_fit`). ``reset`` False, after
    ``match_photos(reset=False)``, keeps the alignment there is and registers the photos added
    since into it (one photo after the other)."""
    import pycolmap

    progress = as_progress(progress)
    work = _sfm_dir(chunk)
    db_path = work / "database.db"
    if not db_path.exists():
        raise RuntimeError("run match_photos() before align_cameras()")
    info = chunk.meta["sfm"]
    root = info["image_root"]
    out = work / "mapper"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    names = {int(k): v for k, v in info.get("names", {}).items()}
    n = len(names)
    fixed = _fixed_camera_ids(chunk, db_path)
    rigs = bool(info.get("rigs"))
    if mapper not in ("auto", "incremental", "global"):
        raise ValueError(f"unknown mapper {mapper!r}")
    model_dir = work / "model"
    continuing = (not reset) and bool(info.get("continued")) and model_dir.exists()
    if continuing:
        mapper = "incremental"
    elif mapper == "auto":
        mapper = "global" if n >= GLOBAL_FROM and not fixed and not rigs else "incremental"
    priors = None
    if use_gps is None or use_gps:
        priors = _write_pose_priors(chunk, db_path, names, info.get("gps_priors") if continuing else None)
        if priors:
            log.info("pose priors of %d photos (%s, offset %s)", priors["count"], priors["crs"], priors["offset"])
        elif use_gps:
            progress(0.0, "No reference positions of the photos: aligning without them")
    info["gps_priors"] = priors
    recs = {}
    if mapper == "global":
        progress(0.0, f"Aligning {n} cameras at once (global)")
        try:
            recs = _global_mapping(chunk, db_path, root, out, n)
        except Exception as exc:  # pragma: no cover - a block the global solver cannot handle
            progress(0.05, f"Global alignment failed ({exc}): one photo at a time")
        best = max(recs.values(), key=lambda r: r.num_reg_images()) if recs else None
        why = None
        if best is None or best.num_reg_images() < 0.9 * n:
            why = f"placed {best.num_reg_images() if best else 0} of {n} photos"
        else:
            fits, residual = _block_fits_references(chunk, best)
            if not fits:  # parts at different scales (Tell Tuqan): the matches are sound, the block is not
                why = f"built a block whose cameras miss the GPS of the photos by {residual:.0f} m"
        if why:
            log.warning("global alignment %s: aligning one photo at a time", why)
            progress(0.1, f"Global alignment {why}: one photo at a time")
            chunk.meta["sfm"]["global_fallback"] = why
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True)
            recs, mapper = {}, "incremental"
        else:
            chunk.meta["sfm"].pop("global_fallback", None)
    if mapper == "incremental":
        opts = pycolmap.IncrementalPipelineOptions()
        opts.multiple_models = not continuing
        opts.min_model_size = 3
        opts.extract_colors = True
        if fixed:  # faces of 360-degree photos (exact calibration, rig known), sensors held by the user
            opts.constant_cameras = fixed
            opts.ba_refine_sensor_from_rig = False
        if priors:
            opts.use_prior_position = True
            opts.use_robust_loss_on_prior_position = True  # a wrong GPS fix does not pull the block
        ratio = os.environ.get("PPG_BA_GLOBAL_RATIO")  # experiments: fewer global adjustments while the block grows
        if ratio:
            opts.ba_global_frames_ratio = float(ratio)
            opts.ba_global_points_ratio = float(ratio)
        progress(0.0, "Aligning cameras" + (" on the GPS of the photos" if priors else "")
                 + (f" (adding {len(info.get('added') or [])} photos)" if continuing else ""))
        start = str(_continuation_model(model_dir, db_path, work / "continue")) if continuing else ""
        recs = pycolmap.incremental_mapping(str(db_path), root, str(out), opts, input_path=start)
        shutil.rmtree(work / "continue", ignore_errors=True)
    if not recs:
        raise RuntimeError("alignment failed: no reconstruction could be initialised")
    info["mapper"] = mapper
    best = max(recs.values(), key=lambda r: r.num_reg_images())
    info["masked_tie_points"] = _drop_masked_tie_points(chunk, best, names) if info.get("apply_masks") == "tiepoints" else 0
    if adaptive_fitting:
        progress(0.85, "Adaptive camera model fitting")
        best, info["adaptive_fitting"] = adaptive_camera_fit(chunk, best, _rec_fixed_cameras(chunk, best))
    else:
        info.pop("adaptive_fitting", None)
    _restore_sensor_models(chunk, best)
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


STEP_RESIDUAL_PX = 15.0  # continuation of the fixed markers: each step starts within this reprojection error
MAX_STEPS = 40
STEP_ITERATIONS = 12  # adjustment iterations of the intermediate steps (the last step gets them all)
SUBSET_OBSERVATIONS = 2_500_000  # the continuation runs on the longest tracks up to this many observations
FREE_PASS_ITERATIONS = 20  # the free adjustment before the control points: the block is already adjusted
FINAL_PASS_ITERATIONS = 30  # the adjustment of all the tie points after the continuation on the subset
STALE_POINT_PX = 16.0  # points off by more than this (mean) after the re-triangulation are dropped


def _at_most(opts, iterations: int):
    """A copy of the adjustment options with at most ``iterations`` solver iterations."""
    import copy

    out = copy.deepcopy(opts)
    out.ceres.solver_options.max_num_iterations = min(iterations, opts.ceres.solver_options.max_num_iterations)
    return out


def _point_residuals_px(rec, pids) -> np.ndarray:
    """Reprojection residuals (px) of all the observations of the given points of ``rec``;
    infinite behind a camera."""
    out = []
    for pid in pids:
        p = rec.points3D[pid]
        for el in p.track.elements:
            image = rec.images[el.image_id]
            xc = image.cam_from_world() * p.xyz
            if xc[2] <= 0:
                out.append(np.inf)
                continue
            proj = np.asarray(rec.cameras[image.camera_id].img_from_cam(xc[None, :]))[0]
            out.append(float(np.linalg.norm(proj - image.points2D[el.point2D_idx].xy)))
    return np.array(out, float)


def _adjust_towards(rec, opts, pids, copies, starts, fixed_cameras=(), progress=None) -> int:
    """Bundle adjustment with the markers as fixed points reached by continuation: the fixed points
    (``pids``, ``copies`` per marker, at their references) are moved in steps from where the block
    sees them (``starts``, one per marker; None = the reference) to their references, with an
    adjustment at each step, so that no step starts far from its solution. A single adjustment
    with the points fixed far from where the block sees them (metres at close range: hundreds of
    pixels) diverges, the calibration running away (Tell Tuqan). Returns the number of steps."""
    refs = np.array([rec.points3D[pid].xyz for pid in pids[::copies]])
    starts = np.array([refs[i] if s is None else np.asarray(s, float) for i, s in enumerate(starts)])
    px = _point_residuals_px(rec, pids[::copies])
    median = float(np.median(px)) if len(px) else 0.0
    steps = MAX_STEPS if not np.isfinite(median) else int(np.clip(np.ceil(median / STEP_RESIDUAL_PX), 1, MAX_STEPS))
    for k in range(1, steps + 1):
        f = k / steps
        for i, pid in enumerate(pids):
            rec.points3D[pid].xyz = starts[i // copies] + f * (refs[i // copies] - starts[i // copies])
        step_opts = opts if k == steps else _at_most(opts, STEP_ITERATIONS)
        _adjust(rec, step_opts, constant_points=pids, fixed_cameras=fixed_cameras)
        if progress is not None:
            progress(k / steps, f"Adjustment on the control points, step {k} of {steps}")
    return steps


def _subset(rec, keep_pids, max_observations: int):
    """A copy of ``rec`` with the points of the longest tracks (and ``keep_pids``) up to about
    ``max_observations`` observations: the continuation runs on it in a fraction of the time.
    Every photo keeps its share of points first (its longest tracks), so that no pose is left
    loose: the longest tracks alone crowd where the photos overlap most."""
    import copy

    lengths = {pid: p.track.length() for pid, p in rec.points3D.items()}
    sub = copy.deepcopy(rec)
    kept = set(keep_pids)
    images = [im for im in rec.images.values() if im.has_pose]
    quota = max(100, max_observations // (2 * max(1, len(images))))
    for image in images:
        ids = [p.point3D_id for p in image.points2D if p.has_point3D() and p.point3D_id in lengths]
        ids.sort(key=lambda pid: -lengths[pid])
        kept.update(ids[:quota])
    budget = max_observations - sum(lengths[pid] for pid in kept)
    for pid in sorted(lengths, key=lengths.get, reverse=True):
        if budget <= 0:
            break
        if pid not in kept and lengths[pid] <= budget:
            kept.add(pid)
            budget -= lengths[pid]
    for pid in lengths:
        if pid not in kept:
            sub.delete_point3D(pid)
    return sub


def _transfer_poses(src, dst) -> None:
    """The poses and the intrinsics of ``src`` into ``dst`` (the same frames and cameras)."""
    for fid, frame in src.frames.items():
        if frame.has_pose:
            dst.frames[fid].rig_from_world = frame.rig_from_world
    for cid, cam in src.cameras.items():
        dst.cameras[cid].params = cam.params


def _retriangulate(rec, skip=(), batch: int = 2_000_000) -> int:
    """Every point of ``rec`` (but ``skip``) triangulated again from its observations with the
    current poses and intrinsics (least squares of the rays, all at once): after the cameras have
    moved, the points would start an adjustment hundreds of pixels off and it would not converge.
    Points whose rays do not meet keep their position. Returns the number of points moved."""
    skip = set(skip)
    pids = np.array([pid for pid in rec.points3D.keys() if pid not in skip], dtype=np.int64)
    if len(pids) == 0:
        return 0
    lookup = np.full(int(pids.max()) + 1, -1, dtype=np.int64)
    lookup[pids] = np.arange(len(pids))
    n = len(pids)
    A = np.zeros((n, 3, 3))
    b = np.zeros((n, 3))
    count = np.zeros(n, dtype=np.int64)
    pending: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    size = 0

    def flush():
        nonlocal pending, size
        if not pending:
            return
        ids = np.concatenate([p[0] for p in pending])
        P = np.concatenate([p[1] for p in pending])
        Pc = np.concatenate([p[2] for p in pending])
        for i in range(3):
            for j in range(3):
                A[:, i, j] += np.bincount(ids, weights=P[:, i, j], minlength=n)
            b[:, i] += np.bincount(ids, weights=Pc[:, i], minlength=n)
        count[:] += np.bincount(ids, minlength=n)
        pending, size = [], 0

    for image in rec.images.values():
        if not image.has_pose:
            continue
        obs = [(p.point3D_id, p.xy) for p in image.points2D if p.has_point3D()]
        if not obs:
            continue
        ids = np.array([o[0] for o in obs], dtype=np.int64)
        ids = np.where(ids < len(lookup), lookup[np.minimum(ids, len(lookup) - 1)], -1)
        sel = ids >= 0
        if not sel.any():
            continue
        xy = np.array([o[1] for o in obs], float)[sel]
        ids = ids[sel]
        norm = np.asarray(rec.cameras[image.camera_id].cam_from_img(xy), float)
        d = np.column_stack([norm, np.ones(len(norm))])
        u = d @ image.cam_from_world().rotation.matrix()  # directions in the world (R^T d, as rows)
        u /= np.linalg.norm(u, axis=1, keepdims=True)
        P = np.eye(3)[None] - u[:, :, None] * u[:, None, :]
        c = np.asarray(image.projection_center(), float)
        pending.append((ids, P, P @ c))
        size += len(ids)
        if size >= batch:
            flush()
    flush()
    det = np.linalg.det(A)
    scale = np.trace(A, axis1=1, axis2=2) ** 3 / 27.0
    valid = (count >= 2) & (det > 1e-5 * np.maximum(scale, 1e-300))  # rays at least a tenth of a degree apart
    if not valid.any():
        return 0
    X = np.linalg.solve(A[valid], b[valid][:, :, None])[:, :, 0]
    good = np.isfinite(X).all(axis=1)
    moved = 0
    for pid, x, ok in zip(pids[valid], X, good):
        if ok:
            rec.points3D[int(pid)].xyz = x
            moved += 1
    return moved


def _drop_stale_points(rec, max_px: float, skip=()) -> int:
    """Points whose stored mean reprojection error (``update_point_3d_errors`` first) is above
    ``max_px`` deleted, but ``skip``; returns how many."""
    skip = set(skip)
    bad = [pid for pid, p in rec.points3D.items() if p.error > max_px and pid not in skip]
    for pid in bad:
        rec.delete_point3D(pid)
    return len(bad)


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
        _adjust(rec, _at_most(opts, FREE_PASS_ITERATIONS), fixed_cameras=fixed_cams)
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
    steps = 0
    if len(placed) >= 3 and gcp._non_collinear(placed):
        starts = [m.position for m, _ in points if m.key in used]  # where the block sees them now
        observations = sum(p.track.length() for p in rec.points3D.values())
        if observations > SUBSET_OBSERVATIONS:  # the continuation on the strongest tracks, then everything
            sub = _subset(rec, pids, SUBSET_OBSERVATIONS)
            steps = _adjust_towards(sub, opts, pids, copies, starts, fixed_cams, progress.sub(0.5, 0.3))
            _transfer_poses(sub, rec)
            for pid in pids:
                rec.points3D[pid].xyz = sub.points3D[pid].xyz
            del sub
            progress(0.8, "Adjustment of all the tie points on the control points")
            _retriangulate(rec, skip=pids)  # the other points follow the cameras before the adjustment
            rec.update_point_3d_errors()
            dropped = _drop_stale_points(rec, STALE_POINT_PX, skip=pids)  # what no longer fits the cameras
            log.info("control points: %d steps, %d tie points dropped, %d kept", steps, dropped, rec.num_points3D())
            _adjust(rec, _at_most(opts, FINAL_PASS_ITERATIONS), constant_points=pids, fixed_cameras=fixed_cams)
        else:
            steps = _adjust_towards(rec, opts, pids, copies, starts, fixed_cams, progress.sub(0.5, 0.45))
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
              "scale_bars": sum(1 for sb in bars if {sb.marker_a, sb.marker_b} <= used), "steps": steps}
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
