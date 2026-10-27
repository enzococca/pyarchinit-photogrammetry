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
from ..core.chunk import QUALITY_DOWNSCALE, Chunk
from ..core.pointcloud import PointCloud
from ..core.progress import Progress, as_progress
from . import colmap_log

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
    from ..markers.gcp import camera_references

    positions, _crs = camera_references(chunk, [c for c in chunk.cameras if c.enabled and c.key in names])
    return {names[key]: p for key, p in positions.items()}


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
_REC709_BGR = np.array([[0.0722, 0.7152, 0.2126]], np.float32)  # COLMAP's grey (Bitmap::CloneAsGrey), pixel for pixel


def _colmap_taps(dst: int, src: int) -> tuple[np.ndarray, np.ndarray]:
    """The source pixels and weights of every destination pixel of OpenImageIO's ``resize`` with
    the triangle filter (what COLMAP reduces the photos with): a triangle as wide as the step,
    centred on the destination pixel, normalised, all in single precision in its order."""
    f32 = np.float32
    ratio = f32(dst) / f32(src)
    rad = int(np.ceil(f32(1.0) / ratio))
    n = 2 * rad + 1
    x = np.arange(dst, dtype=f32)
    src_f = ((x + f32(0.5)) * (f32(1.0) / f32(dst))) * f32(src)
    src_i = np.floor(src_f).astype(np.int64)
    frac = (src_f - src_i).astype(f32)
    w = np.zeros((dst, n), f32)
    for i in range(n):
        arg = ratio * (f32(i - rad) - (frac - f32(0.5)))
        w[:, i] = np.where(np.abs(arg) < 1, f32(1.0) - np.abs(arg), f32(0.0)).astype(f32)
    total = w[:, 0].copy()
    for i in range(1, n):
        total += w[:, i]
    idx = np.clip(src_i[:, None] - rad + np.arange(n)[None, :], 0, src - 1)
    return idx, w / total[:, None]


def _rescale_as_colmap(img: np.ndarray, new_w: int, new_h: int) -> np.ndarray:
    """A grey photo (uint8) reduced as COLMAP reduces it before extracting the features:
    OpenImageIO's ``resize`` with the ``triangle`` filter, the sum of the taps in single precision
    in the same order, the result rounded as it rounds (99.98 % of the pixels equal, the rest off by
    one). The GPU extractor then finds the points COLMAP's extractor finds; a box filter
    (``cv2.INTER_AREA``) kept the image sharper and found a third more points, elsewhere."""
    f32 = np.float32
    h, w = img.shape
    if (new_w, new_h) == (w, h):
        return img
    src = img.astype(f32) * f32(1.0 / 255.0)
    ix, wx = _colmap_taps(new_w, w)
    iy, wy = _colmap_taps(new_h, h)
    live_x = [i for i in range(wx.shape[1]) if wx[:, i].any()]
    pel = np.zeros((new_h, new_w), f32)
    for j in range(wy.shape[1]):
        if not wy[:, j].any():
            continue
        rows = src[iy[:, j], :]
        for i in live_x:
            pel += (wy[:, j][:, None] * wx[:, i][None, :]).astype(f32) * rows[:, ix[:, i]]
    return np.clip(np.floor(pel * f32(255.0) + f32(0.5)), 0, 255).astype(np.uint8)
PRESELECTION_INLIERS = 15  # verified matches that make two photos a pair
PRESELECTION_PAIRS_PER_PHOTO = 20  # the pairs of each photo with the most matches are matched in full
EXHAUSTIVE_UP_TO = 40  # photos: every pair is matched in full (cheap enough), above it the pairs are preselected
BA_GLOBAL_RATIO = 1.1  # the incremental mapper adjusts the whole block when it grows by this ratio (COLMAP's 1.1)
BA_GLOBAL_REFINEMENTS = 5  # refinements of every global adjustment (COLMAP's 5)


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
                       names: dict[int, str], gpu_device=None, progress: Progress | None = None) -> list[tuple[str, str]]:
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

    progress = as_progress(progress)
    db_path = work / "preselection.db"
    db_path.unlink(missing_ok=True)
    try:
        if gpu_device is not None:  # the small photos on the GPU: a few milliseconds each
            try:
                _extract_features_gpu(db_path, root, chunk, list(by_sensor.items()),
                                      lambda s: min(PRESELECTION_SIZE, max(s.width, s.height)), PRESELECTION_POINTS,
                                      masks, gpu_device)
            except Exception as exc:  # noqa: BLE001
                log.warning("GPU SIFT failed in the preselection (%s): COLMAP's extractor", exc)
                db_path.unlink(missing_ok=True)
                gpu_device = None
        with colmap_log.relay(progress, work, phases={"features": (0.0, 0.4), "blocks2": (0.4, 0.6)}):
            for skey, image_names in by_sensor.items():
                if gpu_device is not None:
                    break
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


def _continuable(chunk: Chunk, db_path: Path, root, names: dict[int, str], matcher: str = "sift") -> bool:
    """Whether the matches and the alignment of the chunk can take more photos: database and
    model exist, the photos sit in the same folder, every photo of the database is still there,
    the points were found by the same ``matcher`` (SIFT's and the learned ones do not match)."""
    info = chunk.meta.get("sfm") or {}
    model = chunk.product_path("reconstruction")
    if not db_path.exists() or model is None or not model.exists() or info.get("image_root") != str(root):
        return False
    if (info.get("matcher") or "sift") != matcher:
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

    from ..core.progress import Cancelled

    db = pycolmap.Database.open(str(db_path))
    try:
        ids = {im.name: int(im.image_id) for im in db.read_all_images()}
    finally:
        db.close()
    by_id = {v: k for k, v in ids.items()}
    # the descriptors kept on the GPUs are keyed by image id, unique within one database only:
    # another database (another chunk, another project, the tests) starts from 1 again, and a
    # photo with the same count of points would be matched with the points of the old one
    _native.gpu_forget()
    queue = collections.deque(sorted(pairs))
    total = len(queue)
    lock = threading.Lock()
    results: dict[tuple[int, int], np.ndarray] = {}
    done = {"gpu": 0, "cpu": 0}
    by_device = {d: 0 for d in devices}
    failure: list[BaseException] = []
    cancelled: list[bool] = []
    reported = [0.0]
    cache: collections.OrderedDict[int, np.ndarray] = collections.OrderedDict()

    def report(force: bool = False) -> None:
        """The pairs done so far, from any stream, at most twice a second (the GPU streams report
        too: the first chunk of the CPU can take minutes, and the window must move meanwhile)."""
        now = time.perf_counter()
        with lock:
            if not force and now - reported[0] < 0.5:
                return
            reported[0] = now
            g, c = done["gpu"], done["cpu"]
        progress(0.55 + 0.4 * min(1.0, (g + c) / max(total, 1)),
                 f"Matching photos: {g:,} pairs on the GPU, {c:,} on the CPU")
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
                    if not queue or failure or cancelled:
                        return
                    a, b = queue.popleft()
                ia, ib = ids[a], ids[b]
                m = _native.gpu_match(descriptors(ia), descriptors(ib), sift.max_ratio, sift.max_distance,
                                      sift.cross_check, device, a_key=ia, b_key=ib)
                with lock:
                    results[(ia, ib)] = np.asarray(m, dtype=np.uint32)
                    done["gpu"] += 1
                    by_device[device] += 1
                report()
        except Cancelled:  # the user: the CPU stream stops at its next chunk
            cancelled.append(True)
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
                if failure or cancelled or not queue:
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
            flush()
            report(force=True)
    finally:
        for t in threads:
            t.join()
        reader.close()
    if failure:
        raise RuntimeError(f"GPU matching failed: {failure[0]}") from failure[0]
    progress.check()  # cancelled while a GPU stream was reporting: it stopped, the step stops here
    flush()
    if gpu_pairs:  # COLMAP's geometric verification of what the GPUs matched
        progress(0.95, f"Verifying {len(gpu_pairs):,} pairs matched on the GPU")
        gpu_list = work / "pairs_gpu.txt"
        gpu_list.write_text("\n".join(f"{a} {b}" for a, b in gpu_pairs))
        pycolmap.verify_matches(str(db_path), str(gpu_list), pycolmap.TwoViewGeometryOptions())
    return {"gpu_pairs": done["gpu"], "cpu_pairs": done["cpu"], "by_device": by_device}


def gpu_sift_device(gpu: str | None) -> int | None:
    """The GPU the key points are extracted on (``None``: the CPU extractor of COLMAP): the first
    of :func:`_native.gpu_devices` for the setting, when the native core has the SIFT kernel."""
    if _native.kernel("gpu_sift") is None:
        return None
    try:
        devices = _native.gpu_devices(gpu)
    except Exception:  # noqa: BLE001
        return None
    return devices[0] if devices else None


def _extract_features_gpu(db_path: Path, root: Path, chunk: Chunk, groups: list[tuple[int, list[str]]],
                          max_image_size, max_features: int, masks: Path | None, device: int,
                          progress: Progress | None = None, readers: int = 4) -> int:
    """COLMAP's feature extraction done with the SIFT kernel of the GPU: a camera per sensor
    (``groups``: sensor key, image names) with its rig and a frame per photo as COLMAP writes them,
    the photos read and reduced on the CPU in threads while the GPU extracts one after the other,
    the key points brought back to the pixels of the full photo (the centre of the first pixel at
    0.5, as COLMAP), those under a mask dropped, everything written into the database as the CPU
    extractor writes it. The image is COLMAP's, pixel for pixel: the Rec. 709 grey (cv2's 601
    weights give other pixels and a third of the points elsewhere), the raster of the file with the
    EXIF orientation ignored, reduced with :func:`_rescale_as_colmap` to the longer side of
    ``max_image_size`` (a number or a function of the sensor). Returns how many photos were done."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import cv2
    import pycolmap

    def load(name: str, limit: int):
        img = cv2.imread(str(root / name), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if img is None:
            return name, None, None, None
        if img.ndim == 3:
            img = cv2.transform(img, _REC709_BGR)
        h, w = img.shape
        if limit and max(w, h) > limit:  # COLMAP: the longer side to the limit, the other rounded
            f = limit / max(w, h)
            small = _rescale_as_colmap(img, max(int(np.floor(w * f + 0.5)), 8), max(int(np.floor(h * f + 0.5)), 8))
        else:
            small = img
        mask = None
        if masks is not None:
            m = cv2.imread(str(masks / f"{name}.png"), cv2.IMREAD_GRAYSCALE)
            if m is not None and m.shape == img.shape:
                mask = m
        return name, small.astype(np.float32) / 255.0, (w, h), mask

    done = 0
    total = sum(len(names) for _, names in groups)
    lock = threading.Lock()
    db = pycolmap.Database.open(str(db_path))
    try:
        for skey, image_names in groups:
            sensor = chunk.sensors[skey]
            model, params = mapping_camera(sensor)
            camera = pycolmap.Camera(model=model, width=int(sensor.width), height=int(sensor.height), params=list(params))
            camera.has_prior_focal_length = True
            camera_id = db.write_camera(camera)
            sensor_id = pycolmap.sensor_t(pycolmap.SensorType.CAMERA, camera_id)
            rig = pycolmap.Rig()
            rig.add_ref_sensor(sensor_id)
            rig_id = db.write_rig(rig)
            limit = int(max_image_size(sensor) if callable(max_image_size) else max_image_size)
            with ThreadPoolExecutor(max_workers=readers) as pool:
                for name, small, size, mask in pool.map(lambda n: load(n, limit), image_names):
                    if small is None:
                        log.warning("%s: unreadable, no key points", name)
                        continue
                    kp, desc = _native.gpu_sift(small, max_features=max_features, device=device)
                    kp = np.asarray(kp, np.float32)
                    desc = np.asarray(desc, np.uint8)
                    sx, sy = size[0] / small.shape[1], size[1] / small.shape[0]
                    kp[:, 0] = (kp[:, 0] + 0.5) * sx
                    kp[:, 1] = (kp[:, 1] + 0.5) * sy
                    kp[:, 2] *= 0.5 * (sx + sy)
                    if mask is not None and len(kp):
                        cx = np.clip(kp[:, 0].astype(int), 0, mask.shape[1] - 1)
                        cy = np.clip(kp[:, 1].astype(int), 0, mask.shape[0] - 1)
                        keep = mask[cy, cx] > 0
                        kp, desc = kp[keep], desc[keep]
                    with lock:
                        image = pycolmap.Image(name=name, camera_id=camera_id)
                        iid = db.write_image(image)
                        frame = pycolmap.Frame()
                        frame.rig_id = rig_id
                        frame.add_data_id(pycolmap.data_t(sensor_id, iid))
                        db.write_frame(frame)
                        db.write_keypoints(iid, np.ascontiguousarray(kp))
                        db.write_descriptors(iid, pycolmap.FeatureDescriptors(pycolmap.FeatureExtractorType.SIFT,
                                                                              np.ascontiguousarray(desc)))
                    done += 1
                    if progress is not None and (done % 8 == 0 or done == total):
                        progress(done / max(total, 1), f"Points detected on the GPU: {done} of {total} photos")
    finally:
        db.close()
    return done


def match_photos(chunk: Chunk, quality: str = "high", keypoint_limit: int = 40000, tiepoint_limit: int = 8000,
                 pair_selection: str = "auto", progress: Progress | None = None,
                 exclude_stationary: bool = False, generic: bool | None = None, reference: str | None = None,
                 neighbours: int = REFERENCE_NEIGHBOURS, apply_masks: str = "keypoints",
                 guided_matching: bool = False, reset: bool = True, gpu: str | None = None,
                 matcher: str = "sift") -> None:
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
    frames of a rig.

    ``matcher``: ``sift`` (COLMAP's SIFT, on the GPU when there is one), or learned key points
    matched by LightGlue (:mod:`.learned`): ``disk`` or ``aliked`` - more matches on thermal
    frames and on surfaces with little texture, slower. Not for rigs (SIFT is used) nor with the
    guided matching, which is COLMAP's."""
    import pycolmap

    from . import learned
    from .masks import colmap_mask_folder
    from .panorama import panorama_cameras, rig_configs

    progress = as_progress(progress)
    if apply_masks not in MASK_MODES:
        raise ValueError(f"apply_masks: one of {', '.join(MASK_MODES)}")
    if matcher not in learned.MATCHERS:
        raise ValueError(f"matcher: one of {', '.join(learned.MATCHERS)}")
    generic, reference = _preselection(pair_selection, generic, reference)
    work = _sfm_dir(chunk)
    db_path = work / "database.db"
    root, names = image_root_and_names(chunk)
    rigs = rig_configs(chunk, names)
    if matcher != "sift" and rigs:
        log.warning("learned features are not used for the rigs of 360-degree photos: SIFT")
        matcher = "sift"
    if matcher != "sift" and guided_matching:
        log.warning("the guided matching is COLMAP's (SIFT): left out with %s", learned.NAMES[matcher])
        guided_matching = False
    keep = (not reset) and not rigs and _continuable(chunk, db_path, root, names, matcher)
    if not reset and not keep:
        progress(0.0, "No alignment to continue: every photo again")
    if not keep and db_path.exists():
        db_path.unlink()
    if not keep:
        import shutil

        shutil.rmtree(work / "learned", ignore_errors=True)  # the descriptors of the database just removed
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
    from . import nodes

    files_dir = chunk.data_dir.parent
    others = nodes.wanted(files_dir) if not rigs and matcher == "sift" else []
    chunk_nodes: list[str] = []
    nodes_active = bool(others) and len(new) >= 2 * nodes.FEATURE_UNIT_PHOTOS
    sift_device = None
    features = "cpu"
    if matcher != "sift":
        # learned key points, in a worker process of their own (the GPU is free again for the preselection)
        groups = [(skey, [n for n in image_names if n not in existing]) for skey, image_names in by_sensor.items()]
        groups = [(k, n) for k, n in groups if n]
        turns = learned.quarter_turns(chunk, names)  # the networks are not invariant to rotation
        with learned.open_features(matcher) as feats:
            progress(0.0, f"Detecting points with {learned.NAMES[matcher]} on {feats.device.upper()}"
                     + (f", {sum(1 for k in turns.values() if k)} photos turned to the heading of the others"
                        if turns else ""))
            learned.extract_to_database(db_path, root, chunk, groups, lambda s: int(max(s.width, s.height) / downscale),
                                        feats, masks, progress.sub(0.0, 0.5), turns=turns)
        features = matcher
    elif nodes_active:
        # the photos in units of work for the other computers and this one (its own worker): COLMAP's
        # extractor runs there into a database of its own, the points come back and go into ours
        units, cameras = [], {}
        for skey, image_names in by_sensor.items():
            image_names = [n for n in image_names if n not in existing]
            if not image_names:
                continue
            sensor = chunk.sensors[skey]
            model, params = mapping_camera(sensor)
            cameras[skey] = {"model": model, "params": ",".join(f"{p:.10g}" for p in params),
                             "width": sensor.width, "height": sensor.height, "names": image_names}
            for k in range(0, len(image_names), nodes.FEATURE_UNIT_PHOTOS):
                units.append({"kind": "features", "root": str(root), "names": image_names[k:k + nodes.FEATURE_UNIT_PHOTOS],
                              "camera": {"model": model, "params": cameras[skey]["params"]},
                              "max_image_size": int(max(sensor.width, sensor.height) / downscale),
                              "max_num_features": int(keypoint_limit),
                              "mask_path": str(masks) if masks is not None else None})
        project = files_dir.parent / files_dir.name[:-len(".files")] if files_dir.name.endswith(".files") else None
        progress(0.0, f"Detecting points: {len(units)} units of work shared with {len(others)} other computer(s)")
        results = nodes.run(files_dir, units, progress.sub(0.0, 0.5), "units of photos", project_path=project)
        db = pycolmap.Database.open(str(db_path))
        try:
            camera_ids = {}
            for cam in cameras.values():
                camera = pycolmap.Camera(model=cam["model"], width=int(cam["width"]), height=int(cam["height"]),
                                         params=[float(v) for v in cam["params"].split(",")])
                camera.has_prior_focal_length = True
                cid = db.write_camera(camera)
                for n in cam["names"]:
                    camera_ids[n] = cid
        finally:
            db.close()
        nodes.import_features(db_path, nodes.folder(files_dir) / "done", [r["result"] for r in results], camera_ids)
        chunk_nodes = sorted({r.get("by") for r in results if r.get("by")})
        nodes.forget(files_dir, [r["name"] for r in results])
    elif (sift_device := (gpu_sift_device(gpu) if not rigs else None)) is not None:
        # the SIFT kernel of the GPU: the same points and descriptors as COLMAP's extractor, 15x faster
        groups = [(skey, [n for n in image_names if n not in existing]) for skey, image_names in by_sensor.items()]
        groups = [(k, n) for k, n in groups if n]
        try:
            _extract_features_gpu(db_path, root, chunk, groups,
                                  lambda s: int(max(s.width, s.height) / downscale), int(keypoint_limit), masks,
                                  sift_device, progress.sub(0.0, 0.5))
            features = "gpu"
        except Exception as exc:  # noqa: BLE001 - the GPU failed on these photos: COLMAP's extractor instead
            log.warning("GPU SIFT failed (%s): COLMAP's extractor on the CPU", exc)
            sift_device = None
    if sift_device is None and not nodes_active and matcher == "sift":
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
            with colmap_log.relay(progress.sub(0.5 * i / len(by_sensor), 0.5 / len(by_sensor)), work,
                                  phases={"features": (0.0, 1.0)}):
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
        found = _preselected_pairs(chunk, work, root, by_sensor, masks, names,
                                   gpu_device=gpu_sift_device(gpu) if not rigs else None,
                                   progress=progress.sub(0.5, 0.05))
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
    if not rigs and not guided_matching and matcher == "sift":
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
             + (f" on {gpu_name} and the CPU" if devices else "")
             + (f" with {learned.NAMES[matcher]}" if matcher != "sift" else ""))
    shares = None
    blocks = {"blocks": (0.0, 1.0), "blocks2": (0.0, 1.0)}  # COLMAP's blocks of pairs as the progress
    if matcher != "sift":
        if pairs is None:
            everyone = sorted(names.values())
            pairs = {(a, b) for i, a in enumerate(everyone) for b in everyone[i + 1:]}
        with learned.open_features(matcher) as feats:
            learned.match_pairs(db_path, pairs, feats, progress.sub(0.55, 0.4))
    elif pairs is None:
        with colmap_log.relay(progress.sub(0.55, 0.4), work, phases=blocks):
            pycolmap.match_exhaustive(str(db_path), matching_options=mopts)
    elif pairs and devices:
        shares = _match_with_gpu(db_path, pairs, mopts, devices, progress, work)
    elif pairs:
        list_path = work / "pairs.txt"
        list_path.write_text("\n".join(f"{a} {b}" for a, b in sorted(pairs)))
        popts = pycolmap.ImportedPairingOptions()
        popts.match_list_path = str(list_path)
        with colmap_log.relay(progress.sub(0.55, 0.4), work, phases=blocks):
            pycolmap.match_image_pairs(str(db_path), matching_options=mopts, pairing_options=popts)
    removed = remove_stationary_matches(db_path) if exclude_stationary else None
    old = chunk.meta.get("sfm") or {}
    chunk.meta["sfm"] = {"image_root": str(root), "names": {str(k): v for k, v in names.items()}, "features": features,
                         "matcher": matcher,
                         "quality": quality, "pair_selection": mode, "pairs": len(pairs) if pairs is not None else None,
                         "generic": bool(generic), "reference": reference, "neighbours": int(neighbours),
                         "masks": masks is not None, "apply_masks": apply_masks, "guided_matching": bool(guided_matching),
                         "stationary_removed": removed, "rigs": len(rigs),
                         "keypoint_limit": int(keypoint_limit), "tiepoint_limit": int(tiepoint_limit),
                         "continued": keep, "added": new if keep else None, "nodes": chunk_nodes,
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


GLOBAL_FROM = 40  # photos of a survey from the air: from here ``auto`` aligns them all at once (global SfM)


def limit_tie_points(db_path: Path, out_path: Path, per_image: int, progress: Progress | None = None,
                     min_per_pair: int = 100) -> dict:
    """A copy of the database for the mapper with at most ``per_image`` key points per photo in
    the verified matches - Metashape's *tie point limit*: for every photo the key points with the
    most verified matches (the longest tracks) stay, the other matches go. The incremental mapper
    runs a global bundle adjustment every time the block grows by a tenth: with the 40,000 points
    a photo of a drone survey it carried 24,000 observations per photo (Tell Tuqan: 4.7 M tie
    points, 7 hours); Metashape aligns with 4,000. The copy holds what the mapper reads (cameras,
    images, key points, priors, rigs, frames, the pruned two-view geometries), not the
    descriptors; without a photo above the limit nothing is written (``pruned`` False).
    ``min_per_pair``: every pair keeps at least so many of its matches (the first ones, COLMAP's
    order of the key points by scale) even outside the limit of its photos, so the weak links
    between distant strips survive (Tell Tuqan: with 4,000 points and no minimum the mapper
    stopped at 430 of 997 photos)."""
    import sqlite3

    import pycolmap

    progress = as_progress(progress)
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        pairs = src.execute("SELECT pair_id, rows, cols, data, config, F, E, H, qvec, tvec FROM two_view_geometries "
                            "WHERE rows > 0").fetchall()
        n_kp = {int(i): int(r) for i, r in src.execute("SELECT image_id, rows FROM keypoints")}
    finally:
        src.close()
    out_path = Path(out_path)
    if not pairs:
        out_path.unlink(missing_ok=True)
        return {"pruned": False, "pairs": 0}
    progress(0.1, "Counting the verified matches of every key point")
    ids, kps = [], []
    for pid, rows, cols, data, *_ in pairs:
        m = np.frombuffer(data, np.uint32).reshape(rows, cols)
        i1, i2 = divmod(int(pid), 2147483647)
        ids.append(np.full(rows, i1, np.int64))
        kps.append(m[:, 0].astype(np.int64))
        ids.append(np.full(rows, i2, np.int64))
        kps.append(m[:, 1].astype(np.int64))
    key = (np.concatenate(ids) << 32) | np.concatenate(kps)
    uniq, count = np.unique(key, return_counts=True)
    image = (uniq >> 32).astype(np.int64)
    kp = (uniq & 0xFFFFFFFF).astype(np.int64)
    keep: dict[int, np.ndarray] = {}
    pruned_images = 0
    for iid in np.unique(image):
        sel = np.nonzero(image == iid)[0]
        mask = np.zeros(n_kp.get(int(iid), int(kp[sel].max()) + 1), bool)
        if len(sel) > per_image:  # the most matched first, then the lower index (the larger scales of COLMAP)
            order = np.lexsort((kp[sel], -count[sel]))[:per_image]
            mask[kp[sel[order]]] = True
            pruned_images += 1
        else:
            mask[kp[sel]] = True
        keep[int(iid)] = mask
    total_before = int(sum(r for _, r, *_ in pairs))
    if not pruned_images:
        out_path.unlink(missing_ok=True)
        return {"pruned": False, "pairs": len(pairs), "matches": total_before, "per_image": per_image}
    progress(0.4, f"Tie point limit {per_image:,}: {pruned_images} photos pruned")
    out_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(out_path) + suffix).unlink(missing_ok=True)
    pycolmap.Database.open(str(out_path)).close()  # the schema of this pycolmap
    dst = sqlite3.connect(str(out_path))
    try:
        dst.execute("ATTACH DATABASE ? AS src", (str(db_path),))  # read only (SELECTs)
        for table in ("cameras", "images", "keypoints", "pose_priors", "rigs", "rig_sensors", "frames", "frame_data"):
            cols = [r[1] for r in dst.execute(f"PRAGMA table_info({table})")]
            if cols and dst.execute("SELECT name FROM src.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                names = ", ".join(cols)
                dst.execute(f"INSERT INTO {table} ({names}) SELECT {names} FROM src.{table}")
        dst.commit()
        dst.execute("DETACH DATABASE src")
        total_after = 0
        for pid, rows, cols, data, config, F, E, H, qvec, tvec in pairs:
            m = np.frombuffer(data, np.uint32).reshape(rows, cols)
            i1, i2 = divmod(int(pid), 2147483647)
            ok = keep[i1][np.minimum(m[:, 0], len(keep[i1]) - 1)] & keep[i2][np.minimum(m[:, 1], len(keep[i2]) - 1)]
            if min_per_pair and ok.sum() < min(min_per_pair, rows):
                ok[np.nonzero(~ok)[0][:min(min_per_pair, rows) - int(ok.sum())]] = True
            m2 = np.ascontiguousarray(m[ok])
            total_after += len(m2)
            dst.execute("INSERT INTO two_view_geometries (pair_id, rows, cols, data, config, F, E, H, qvec, tvec) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (pid, len(m2), cols, m2.tobytes(), config, F, E, H, qvec, tvec))
        dst.commit()
    finally:
        dst.close()
    progress(1.0, f"Tie point limit {per_image:,}: {total_after:,} of {total_before:,} verified matches kept")
    return {"pruned": True, "pairs": len(pairs), "matches": total_before, "kept": total_after, "per_image": per_image,
            "photos_pruned": pruned_images}


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
    from ..markers.gcp import camera_references

    names = {v: int(k) for k, v in chunk.meta["sfm"]["names"].items()}
    by_key = {c.key: c for c in chunk.cameras}
    pairs = []
    for image in rec.images.values():
        cam = by_key.get(names.get(image.name)) if image.has_pose else None
        if cam is not None and cam.reference.enabled and cam.reference.location:
            pairs.append((image, cam))
    positions, _crs = camera_references(chunk, [cam for _image, cam in pairs])
    pairs = [(image, cam) for image, cam in pairs if cam.key in positions]
    src = [np.asarray(image.projection_center(), float) for image, _cam in pairs]
    dst = [positions[cam.key] for _image, cam in pairs]
    acc = [float(np.mean(cam.reference.accuracy[:2])) if cam.reference.accuracy else 5.0 for _image, cam in pairs]
    return fits_references(src, dst, acc) if src else (True, float("nan"))


def _write_pose_priors(chunk: Chunk, db_path: Path, names: dict[int, str], previous: dict | None = None) -> dict | None:
    """The reference positions of the photos (the GPS of the drone, with their accuracy) into the
    COLMAP database as pose priors, on metric axes less a round offset: the incremental mapper
    then holds the block to them (``use_prior_position``), as Metashape's alignment holds it to
    the camera references. ``previous`` (the report of an earlier call, when the database is
    kept) keeps the same axes and only gives the photos without a prior theirs. Returns the
    report (``count``, ``crs``, ``offset``), None with fewer than three positions."""
    import pycolmap

    from ..markers.gcp import camera_references

    cams = [c for c in chunk.cameras
            if c.enabled and c.key in names and c.reference.enabled and c.reference.location]
    kept = bool(previous and previous.get("crs"))
    positions, crs = camera_references(chunk, cams, previous["crs"] if kept else None)
    if len(positions) < 3:
        return None
    offset = np.asarray(previous["offset"], float) if kept else np.round(np.mean(list(positions.values()), axis=0))
    by_name = {names[c.key]: (positions[c.key] - offset, c.reference.accuracy or [5.0, 5.0, 10.0])
               for c in cams if c.key in positions}
    # COLMAP's feature extraction writes a WGS84 prior from the EXIF GPS of every photo that has
    # one (a unique index per image): those go, ours take their place; the CARTESIAN ones written
    # here before stay when the database is kept
    import sqlite3

    con = sqlite3.connect(str(db_path))
    try:
        if previous:
            con.execute("DELETE FROM pose_priors WHERE coordinate_system != ?",
                        (int(pycolmap.PosePriorCoordinateSystem.CARTESIAN),))
        else:
            con.execute("DELETE FROM pose_priors")
        con.commit()
    finally:
        con.close()
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


def auto_mapper(chunk: Chunk, photos: int, fixed: bool = False, rigs: bool = False) -> str:
    """The mapper ``auto`` picks: global (all at once) from ``GLOBAL_FROM`` photos of a survey from the
    air - their GPS spread over the site (:func:`engine.dense.aerial_survey`) - without rigs or
    calibrations held fixed; incremental otherwise. On a room photographed from a few stations (four
    views each) the global one failed 1.5 % of the runs, 24 % with the stations 5 cm apart, and all
    the cameras were placed, so nothing noticed; the incremental one failed 0 of 300."""
    from .dense import aerial_survey

    return "global" if photos >= GLOBAL_FROM and not fixed and not rigs and aerial_survey(chunk) else "incremental"


def align_cameras(chunk: Chunk, progress: Progress | None = None, mapper: str = "auto", reset: bool = True,
                  use_gps: bool | None = None, adaptive_fitting: bool = False, tiepoint_limit: int | None = None,
                  ba_global_ratio: float | None = None, ba_global_refinements: int | None = None) -> int:
    """Cameras and tie points from the matches of :func:`match_photos`. ``mapper``: ``incremental``
    (one photo after the other, COLMAP), ``global`` (all at once, GLOMAP: much faster with many
    photos), ``hierarchical`` (the photos in overlapping clusters of up to 500, each reconstructed
    one photo after the other on its own core, then merged and adjusted together) or ``auto`` -
    global from ``GLOBAL_FROM`` photos of a survey from the air (their GPS spread over the site:
    :func:`engine.dense.aerial_survey`), incremental for fewer, for objects and close-range scenes (the
    global one failed now and then without noticing on a room photographed from a few stations,
    the incremental one never), for 360-degree rigs and calibrations held fixed, and whenever the
    global one leaves photos out or builds a block whose cameras do not sit on the GPS of the photos
    (:func:`fits_references`).

    ``use_gps`` (None: when the photos have them): the reference positions of the photos, with
    their accuracy, hold the block during the incremental alignment - COLMAP's pose priors,
    :func:`_write_pose_priors` - as Metashape's alignment holds it to the camera references: a
    long chain or a ring of photos no longer drifts. ``adaptive_fitting``: Metashape's adaptive
    camera model fitting (:func:`adaptive_camera_fit`). ``reset`` False, after
    ``match_photos(reset=False)``, keeps the alignment there is and registers the photos added
    since into it (one photo after the other).

    ``tiepoint_limit`` (None: the ``tiepoint_limit`` of :func:`match_photos`, 0 for none): at most
    so many key points per photo go into the incremental and hierarchical mappers, the most matched
    ones (:func:`limit_tie_points`, Metashape's tie point limit). ``ba_global_ratio`` and
    ``ba_global_refinements`` (None: :data:`BA_GLOBAL_RATIO`, :data:`BA_GLOBAL_REFINEMENTS`): the
    incremental mapper adjusts the whole block every time it grows by this ratio of photos or of
    points, with up to so many refinements each; with 1.1 and 5 COLMAP adjusted a block of a
    thousand photos sixty times."""
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
    if mapper not in ("auto", "incremental", "global", "hierarchical"):
        raise ValueError(f"unknown mapper {mapper!r}")
    model_dir = work / "model"
    continuing = (not reset) and bool(info.get("continued")) and model_dir.exists()
    if continuing:
        mapper = "incremental"
    elif mapper == "auto":
        mapper = auto_mapper(chunk, n, fixed=bool(fixed), rigs=rigs)
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
            with colmap_log.relay(progress.sub(0.0, 0.8), work, total=n, phases={"stage": (0.0, 1.0)}):
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
    if mapper in ("incremental", "hierarchical"):
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
        ratio = float(os.environ.get("PPG_BA_GLOBAL_RATIO") or ba_global_ratio or BA_GLOBAL_RATIO)
        refinements = int(os.environ.get("PPG_BA_GLOBAL_REFINEMENTS") or ba_global_refinements or BA_GLOBAL_REFINEMENTS)
        opts.ba_global_frames_ratio = ratio
        opts.ba_global_points_ratio = ratio
        opts.ba_global_max_refinements = refinements
        info["ba_schedule"] = {"ratio": ratio, "refinements": refinements}
        # Metashape's tie point limit: the mapper reads a copy of the database with the most
        # matched key points of every photo only (the whole database stays for everything else)
        mapper_db = db_path
        limit = info.get("tiepoint_limit") if tiepoint_limit is None else tiepoint_limit
        info.pop("tie_point_limit", None)
        if limit and not continuing and not rigs:
            stats = limit_tie_points(db_path, work / "database_mapper.db", int(limit), progress.sub(0.0, 0.05))
            if stats.get("pruned"):
                mapper_db = work / "database_mapper.db"
                info["tie_point_limit"] = stats
        progress(0.05, "Aligning cameras" + (" on the GPS of the photos" if priors else "")
                 + (f" (adding {len(info.get('added') or [])} photos)" if continuing else "")
                 + (" in clusters" if mapper == "hierarchical" else ""))
        # COLMAP's log as the progress: the photos placed so far over all of them
        with colmap_log.relay(progress.sub(0.05, 0.8), work, total=n, phases={"register": (0.0, 1.0)}):
            if mapper == "hierarchical":
                hopts = pycolmap.HierarchicalPipelineOptions()
                hopts.incremental_options = opts
                recs = pycolmap.hierarchical_mapping(str(mapper_db), root, str(out), hopts)
            else:
                start = str(_continuation_model(model_dir, db_path, work / "continue")) if continuing else ""
                recs = pycolmap.incremental_mapping(str(mapper_db), root, str(out), opts, input_path=start)
        shutil.rmtree(work / "continue", ignore_errors=True)
        if mapper_db != db_path:
            for suffix in ("", "-wal", "-shm"):
                Path(str(mapper_db) + suffix).unlink(missing_ok=True)
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


GNSS_MODES = ("auto", "off", "plain", "bias")  # the GNSS positions of the photos in optimize_cameras
GNSS_PRIOR_MAX_M = 0.5  # m: positions this accurate (mean horizontal accuracy: fix, float, PPK) hold the block
GNSS_PRIOR_SPREAD = 50.0  # the positions must spread across the block this many times their accuracy
BEND_FACTOR = 3.0  # an optimisation that leaves the cameras farther from their accurate positions than this
BEND_MARGIN_M = 0.3  # many times the offsets before, and this much more, is undone


def accurate_gnss(camera) -> bool:
    """The position of ``camera`` (its enabled reference) is accurate enough to hold the block in the
    optimisation: a mean horizontal accuracy within :data:`GNSS_PRIOR_MAX_M` - an RTK or PPK fix or float, not a
    single position nor the GPS of a drone without RTK (5/5/10 m)."""
    ref = camera.reference
    if not (camera.enabled and ref.enabled and ref.location and ref.accuracy):
        return False
    acc = np.resize(np.asarray(ref.accuracy, float).reshape(-1), 3)
    return bool(np.all(np.isfinite(acc)) and float(np.mean(acc[:2])) <= GNSS_PRIOR_MAX_M)


def _gnss_states(cameras) -> dict:
    """{RTK state: photos} of ``cameras`` (``"fix"``, ``"float"``, ...; ``"none"`` for a position without one)."""
    from collections import Counter

    return dict(Counter((c.meta.get("gnss") or {}).get("status") or "none" for c in cameras))


def _held_positions(chunk: Chunk, cameras) -> tuple[list, dict, str | None]:
    """The cameras among ``cameras`` whose accurate GNSS positions (:func:`accurate_gnss`) can hold the block, and
    those positions in the internal frame: ``(cameras, {key: position}, why not)``. Each position is read in the
    coordinate system of the chunk (one in another system is converted; one that cannot be - a chunk in local
    coordinates and a position in a real system - is left out) and brought into the internal frame by the
    chunk's transform. Three or more are needed, spread across the block (the second axis of their spread at
    least :data:`GNSS_PRIOR_SPREAD` times their accuracy): positions along a single strip cannot tell how the
    block is turned about it, and the adjustment, which first places the block on them, would turn it at random.
    ``why`` is ``"none"`` (no accurate position) or ``"place"`` (they cannot place the block)."""
    from ..core import crs as crs_mod
    from ..markers import gcp

    cams = [c for c in cameras if accurate_gnss(c)]
    if not cams:
        return [], {}, "none"
    if crs_mod.is_geographic(chunk.crs):
        return [], {}, "place"
    world, _crs = gcp.camera_references(chunk, cams, chunk.crs)
    cams = [c for c in cams if c.key in world and np.all(np.isfinite(world[c.key]))]
    if len(cams) < 3:
        return [], {}, "place"
    xyz = np.array([world[c.key] for c in cams], float)
    centred = xyz - xyz.mean(axis=0)
    axes = np.linalg.svd(centred, full_matrices=False)[2]
    across = float(np.ptp(centred @ axes[1]))
    typical = float(np.median([np.mean(np.resize(np.asarray(c.reference.accuracy, float), 3)[:2]) for c in cams]))
    if not across >= GNSS_PRIOR_SPREAD * typical:
        return [], {}, "place"
    internal = chunk.to_internal(xyz)
    return cams, {c.key: internal[n] for n, c in enumerate(cams)}, None


def fixed_points_hold(chunk: Chunk, use_markers: bool = True) -> bool:
    """Fixed points hold the block in :func:`optimize_cameras`: with ``use_markers``, 3 or more control points
    (markers with an enabled reference that are not check points) or 2 or more control scale bars. Then the GNSS
    positions of the photos take no part in it, whatever the engine (GNSS positions and fixed points together in
    COLMAP's pose-prior adjuster diverge). The one rule of the engines, of :func:`gnss_hold` and of the dialog."""
    from ..markers import gcp

    if not use_markers:
        return False
    controls = [m for m in chunk.markers if not m.checkpoint and m.reference.enabled and m.reference.location]
    return len(controls) >= 3 or len(gcp.control_scalebars(chunk)) >= 2


def gnss_hold(chunk: Chunk, use_markers: bool = True) -> dict:
    """What the GNSS positions of the photos do in :func:`optimize_cameras` with ``gnss="auto"`` (either engine):
    ``{"photos", "states", "accurate", "why"}`` - ``photos`` aligned photos hold the block with their positions
    (``states``: {RTK state: photos}); 0 when they do not, and ``why``: ``"none"`` (no aligned photo has an
    accurate position, :func:`accurate_gnss`), ``"markers"`` (fixed points hold it, :func:`fixed_points_hold`),
    ``"rigs"`` (faces of 360-degree photos) or ``"place"`` (the positions cannot place the block: fewer than
    three, along a line, a chunk in local coordinates); ``accurate``: the aligned photos with an accurate
    position."""
    accurate = [c for c in chunk.aligned_cameras if accurate_gnss(c)]
    out = {"photos": 0, "states": _gnss_states(accurate), "accurate": len(accurate), "why": None}
    if not accurate:
        out["why"] = "none"
    elif fixed_points_hold(chunk, use_markers):
        out["why"] = "markers"
    elif (chunk.meta.get("sfm") or {}).get("rigs"):
        out["why"] = "rigs"
    else:
        cams, _positions, out["why"] = _held_positions(chunk, accurate)
        out["photos"] = len(cams)
        out["states"] = _gnss_states(cams) if cams else out["states"]
    return out


def _gnss_priors(chunk: Chunk, rec) -> tuple[list, list]:
    """COLMAP pose priors for the images of ``rec`` whose photos have positions that can hold the block
    (:func:`_held_positions`): the position in the internal frame, its covariance the accuracies squared on the
    axes of the chunk's system carried into the internal frame (the rotation and the scale of
    ``chunk.transform``). Returns ``(priors, cameras)``; none for a reconstruction with rigs."""
    import pycolmap

    if any(len(list(f.image_ids)) > 1 for f in rec.frames.values()):
        return [], []
    names = {v: int(k) for k, v in chunk.meta["sfm"]["names"].items()}
    by_key = {c.key: c for c in chunk.cameras}
    images = {names[im.name]: iid for iid, im in rec.images.items() if im.has_pose and names.get(im.name) in by_key}
    cams, internal, _why = _held_positions(chunk, [by_key[k] for k in images])
    if not cams:
        return [], []
    s = float(chunk.scale)
    rot = np.asarray(chunk.transform, float)[:3, :3] / s
    priors = []
    for cam in cams:
        acc = np.maximum(np.resize(np.asarray(cam.reference.accuracy, float).reshape(-1), 3), 1e-3)
        prior = pycolmap.PosePrior(position=np.asarray(internal[cam.key], float),
                                   position_covariance=rot.T @ np.diag(acc ** 2) @ rot / s ** 2,
                                   coordinate_system=pycolmap.PosePriorCoordinateSystem.CARTESIAN)
        prior.corr_data_id = rec.images[images[cam.key]].data_id
        priors.append(prior)
    return priors, cams


def _adjust_with_priors(rec, opts, priors, fixed_cameras=()):
    """Bundle adjustment of the aligned images of ``rec`` held by the GNSS positions of the photos (COLMAP's
    pose-prior adjuster, ``priors`` from :func:`_gnss_priors`): it places the block on the positions, then adjusts
    tie points and positions together, each position weighted by its covariance. The positions fix the gauge
    and the soft modes of a free adjustment - the dome of a nadir block over flat ground, which the radial
    distortion and the focal length make up for in the photos.

    The focal length found by the alignment is held (the distortion, and the principal point when asked, are
    refined): with the positions holding the cameras, photos taken looking down from one height cannot tell the
    focal length from the distance to the ground - the focal length and every depth scaled together (the radial
    terms with them) leave each photo as it is - and refined it slid along that trade (a synthetic block at 40 m:
    from 1000 to 180 or 6900 px, the cameras on their positions, the ground 33 m above or 234 m below its
    place). The intrinsics of ``fixed_cameras`` are held. Returns the summary of the solve
    (:func:`_solver_outcome`)."""
    import copy

    import pycolmap

    opts = copy.deepcopy(opts)
    opts.refine_focal_length = False
    config = pycolmap.BundleAdjustmentConfig()
    for iid, image in rec.images.items():
        if image.has_pose:
            config.add_image(iid)
    for cid in fixed_cameras:
        config.set_constant_cam_intrinsics(cid)
    if fixed_cameras:
        opts.refine_sensor_from_rig = False
    prior_opts = pycolmap.PosePriorBundleAdjustmentOptions()
    return pycolmap.create_pose_prior_bundle_adjuster(opts, prior_opts, config, priors, rec).solve()


def _solver_outcome(summary, photos: int) -> dict:
    """How the adjustment held by the positions of ``photos`` photos ended (``summary`` of the pose-prior adjuster):
    ``{"termination", "usable"}``, written to the log - its brief report, and a warning when the solution cannot be
    used (Ceres then leaves the parameters as they were: the block stays as the alignment made it). Ending at the
    iteration cap (NO_CONVERGENCE) is usable."""
    termination = summary.termination_type.name
    usable = bool(summary.is_solution_usable())
    log.info("adjustment held by the GNSS positions of %d photos: %s", photos, summary.brief_report())
    if not usable:
        log.warning("the adjustment held by the GNSS positions of %d photos failed (%s): the block is left as the "
                    "alignment made it", photos, termination)
    return {"termination": termination, "usable": usable}


NET_CAMERAS = 5  # the safety net judges a block with at least this many accurate positions


def _gnss_offsets(chunk: Chunk, placed: bool = False, bias: dict | None = None) -> dict:
    """{camera key: distance in metres from its position} of the aligned photos with an accurate GNSS position
    (:func:`accurate_gnss`) in a georeferenced chunk (the positions and the cameras of
    :func:`markers.gcp.camera_reference_errors`). ``placed``: once the cameras are placed on the positions by the
    similarity that fits them best - the shape of the block against the shape of the positions, whatever the
    georeference (control points measured in another height system than the GNSS, a base on a wrong point: the
    whole block moves, nothing bends). ``bias`` ({camera key: vector in the chunk CRS}, :func:`_flight_bias`): the
    bias our adjustment estimated for the flight of each camera, which it adds to the centre before comparing it
    with the position (a measured position = the true one + the bias of its flight). Empty with fewer than
    :data:`NET_CAMERAS`; not a number for a camera that is not one."""
    from ..core import geometry
    from ..markers import gcp

    accurate = {c.key for c in chunk.cameras if accurate_gnss(c)}
    if len(accurate) < NET_CAMERAS:
        return {}
    rows = [(k, e) for k, e in gcp.camera_reference_errors(chunk).items()
            if k in accurate and "estimated" in e and np.all(np.isfinite(e["reference"]))]
    if len(rows) < NET_CAMERAS:
        return {}
    estimated = np.array([e["estimated"] for _k, e in rows], float)
    reference = np.array([e["reference"] for _k, e in rows], float)
    if bias:
        estimated = estimated + np.array([bias.get(k, np.zeros(3)) for k, _e in rows], float)
    if not np.all(np.isfinite(estimated)):
        return {k: float("nan") for k, _e in rows}
    if placed:
        m = geometry.umeyama(estimated, reference, with_scale=True)
        estimated = estimated @ m[:3, :3].T + m[:3, 3]
    return {k: float(d) for (k, _e), d in zip(rows, np.linalg.norm(estimated - reference, axis=1))}


def _flight_bias(chunk: Chunk) -> dict:
    """{camera key: bias of its flight in the chunk CRS} from the report of our adjustment with ``gnss="bias"``
    (``gnss_bias``, one vector per flight of :func:`.bundle.flights`); empty for any other run."""
    per_flight = ((chunk.meta.get("sfm") or {}).get("optimize") or {}).get("gnss_bias") or {}
    if not per_flight:
        return {}
    from .bundle import flights

    return {key: np.asarray(per_flight[str(flight)], float)
            for key, flight in flights(chunk.cameras).items() if str(flight) in per_flight}


def _keep_alignment(chunk: Chunk) -> dict | None:
    """What an optimisation changes, kept to put it back: a copy of the COLMAP model next to it (a copy that
    cannot be finished - a full disk - is removed and the error raised), the georeference, the marker positions,
    the rolling-shutter velocities our adjustment writes into the cameras, the report of the optimisation before
    (``meta["sfm"]["optimize"]``, which the PDF and the tables print as the optimisation step). None without a
    model (the optimisation says so)."""
    import copy

    model = chunk.product_path("reconstruction")
    if model is None or not model.is_dir():
        return None
    kept = model.with_name(model.name + ".before_optimize")
    shutil.rmtree(kept, ignore_errors=True)
    try:
        shutil.copytree(model, kept)
    except BaseException:
        shutil.rmtree(kept, ignore_errors=True)
        raise
    return {"model": kept, "transform": np.array(chunk.transform, float), "crs": chunk.crs,
            "markers": {m.key: None if m.position is None else np.array(m.position, float) for m in chunk.markers},
            "rolling_shutter": {c.key: list(c.meta["rolling_shutter"]) for c in chunk.cameras
                                if c.meta.get("rolling_shutter") is not None},
            "report": copy.deepcopy((chunk.meta.get("sfm") or {}).get("optimize")),
            "had_report": "optimize" in (chunk.meta.get("sfm") or {}), "restored": False}


def _put_back(chunk: Chunk, kept: dict) -> float:
    """The alignment kept by :func:`_keep_alignment` back in place: the model on disk, the cameras, the
    calibration and the tie points from it, the georeference, the markers, the rolling-shutter velocities and the
    report of the optimisation before (none when there was none: a run that stopped leaves no report of its own).
    Returns its mean reprojection error."""
    model = chunk.product_path("reconstruction")
    bent = model.with_name(model.name + ".bent")
    shutil.rmtree(bent, ignore_errors=True)
    os.replace(model, bent)
    try:
        os.replace(kept["model"], model)
    except OSError:  # never no model at all: the optimised one stays
        os.replace(bent, model)
        raise
    shutil.rmtree(bent, ignore_errors=True)
    rec = load_reconstruction(chunk)
    import_reconstruction(chunk, rec)
    chunk.transform = kept["transform"]
    chunk.crs = kept["crs"]
    for m in chunk.markers:
        if m.key in kept["markers"]:
            m.position = kept["markers"][m.key]
    for c in chunk.cameras:
        if c.key in kept["rolling_shutter"]:
            c.meta["rolling_shutter"] = list(kept["rolling_shutter"][c.key])
        else:
            c.meta.pop("rolling_shutter", None)
    rec.update_point_3d_errors()
    err = float(rec.compute_mean_reprojection_error())
    sfm_meta = chunk.meta.setdefault("sfm", {})
    sfm_meta["reprojection_error"] = err
    if kept["had_report"]:
        sfm_meta["optimize"] = kept["report"]
    else:
        sfm_meta.pop("optimize", None)
    kept["restored"] = True
    return err


def _undo_a_bend(chunk: Chunk, before: dict, kept: dict, err: float, progress: Progress) -> float:
    """The safety net of :func:`optimize_cameras`: the distances of the cameras from their accurate GNSS
    positions (:func:`_gnss_offsets`) before and after the adjustment, root mean square over the same photos -
    as they are when the georeference is the one of before, else once the best similarity places the block on
    its positions (the optimisation fitted a new georeference: control points, our own adjustment); after our
    adjustment with a bias per flight, each position less the bias of its flight. ``before`` holds both. When
    after is more than :data:`BEND_FACTOR` times before and :data:`BEND_MARGIN_M` more (or not a number), the
    optimisation is undone: either it bent the block - a soft mode that the photos alone do not hold - or the
    positions disagree with what held it (control points); the alignment of before is put back
    (:func:`_put_back`) and the warning says how to keep such an optimisation (switch off the camera
    references). The report gets ``camera_offsets`` (``cameras``, ``before``, ``after`` in metres, ``placed``,
    ``flight_bias`` when the biases were taken off) and ``reverted``; after a revert it describes the alignment
    kept (its ``reprojection_error``), the run undone is under ``undone``. Returns the mean reprojection error of
    the alignment kept."""
    placed = chunk.crs != kept["crs"] or not np.allclose(chunk.transform, kept["transform"], rtol=0.0, atol=1e-9)
    before = before[placed]
    bias = _flight_bias(chunk)
    after = _gnss_offsets(chunk, placed, bias)
    keys = sorted(set(before) & set(after))
    if not keys:
        return err
    b = float(np.sqrt(np.mean([before[k] ** 2 for k in keys])))
    a = float(np.sqrt(np.mean([after[k] ** 2 for k in keys])))
    if not np.isfinite(b):  # nothing to judge by
        return err
    bent = not np.isfinite(a) or a > max(BEND_FACTOR * b, b + BEND_MARGIN_M)
    report = chunk.meta.setdefault("sfm", {}).setdefault("optimize", {})
    offsets = {"cameras": len(keys), "before": b, "after": a, "placed": placed}
    if bias:
        offsets["flight_bias"] = True
    report["camera_offsets"] = offsets
    report["reverted"] = bool(bent)
    if not bent:
        return err
    log.warning("camera optimisation undone: the cameras ended %.3f m (RMS) off their GNSS positions, %.3f m before - "
                "either it bent the block or the GNSS positions disagree with the control points; the alignment "
                "before it is put back (to keep such an optimisation, switch off the camera references)", a, b)
    err = _put_back(chunk, kept)
    undone = {k: v for k, v in report.items() if k not in ("camera_offsets", "reverted")}
    # the report of this run describes the alignment kept, the run undone under "undone"
    chunk.meta["sfm"]["optimize"] = {"reprojection_error": err, "reverted": True, "camera_offsets": offsets,
                                     "undone": undone}
    progress(1.0, f"The optimisation was undone: the cameras ended {a:.3f} m off their GNSS positions ({b:.3f} m "
                  "before); it bent the block or the positions disagree with the control points (switch off the "
                  "camera references to keep it)")
    return err


def _optimize_own(chunk: Chunk, fit_principal_point: bool, use_markers: bool, gnss: str, rolling_shutter: bool,
                  progress: Progress, hold_focal: bool = False) -> float:
    """:func:`optimize_cameras` with our own adjustment (:mod:`.bundle`): control points weighted by
    their accuracy, scale bars as distances, GNSS positions of the photos (with a bias per flight),
    rolling shutter; ``hold_focal``: the focal length of the alignment kept."""
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
                                        rolling_shutter=rolling_shutter, fit_principal_point=fit_principal_point,
                                        hold_focal=hold_focal)
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
    key_of = {v: int(k) for k, v in chunk.meta["sfm"]["names"].items()}
    held = [chunk.camera(key_of[rec.images[ids["images"][i]].name]) for i, *_ in pb.gnss]
    report = {"engine": "ppg", "reprojection_error": err, "iterations": res.iterations, "converged": res.success,
              "control_points": len(pb.gcps), "scale_bars": len(pb.bars), "gnss_positions": len(pb.gnss),
              "gnss_bias": {str(f): [float(v) for v in lin @ b] for f, b in res.bias.items()},
              "gnss": {"mode": gnss, "photos": len(held), "states": _gnss_states(held)},
              "focal_held": bool(hold_focal)}
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
                     gnss: str = "auto", rolling_shutter: bool = False) -> float:
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

    ``gnss``: the GNSS positions of the photos. ``"auto"``: without fixed points (fewer than three
    control points in use, fewer than two control bars) the accurate positions - a mean horizontal
    accuracy within :data:`GNSS_PRIOR_MAX_M`: fix, float, PPK - hold the block, each weighted by its
    accuracy (COLMAP's pose priors, :func:`_adjust_with_priors`, in the internal frame of the chunk,
    as the incremental alignment holds it; the focal length of the alignment is kept): a free
    adjustment of a nadir block over flat ground lets it curve into a dome while the radial
    distortion and the focal length make up for it in the photos (82 RTK-fix captures of a DJI P4
    Multispectral: cameras from 3 cm to metres off their positions, the reprojection error
    unchanged). With fixed points the markers hold it as above:
    fixed points and positions together in COLMAP's pose-prior adjuster diverge (Tell Tuqan).
    ``"off"``: the positions take no part (the adjustment of before). Fixed points:
    :func:`fixed_points_hold`. ``report["gnss"]`` says how many photos held the block (``photos``,
    ``states``; ``termination`` and ``usable`` of the solve: a solve that cannot be used holds
    nothing), ``report["focal_held"]`` whether the focal length of the alignment was kept.

    A safety net for every engine: with accurate positions, the root mean square distance of the
    cameras from them is measured before and after (:func:`_undo_a_bend`; when the optimisation fits a
    new georeference, once the best similarity places the block on its positions: a new georeference
    does not count, a bend does); an optimisation that leaves it above :data:`BEND_FACTOR` times and
    :data:`BEND_MARGIN_M` more than before is undone - it bent the block, or the positions disagree
    with the control points (switch off the camera references to keep it) - and the alignment of
    before is put back, the model on disk included (``report["reverted"]``,
    ``report["camera_offsets"]``, the run undone in ``report["undone"]``, a warning in the log). It
    is put back as well when the optimisation stops with an error or is cancelled.

    ``engine="ppg"``: our own adjustment (:mod:`.bundle`) instead of COLMAP's, with the control
    points pulled to their coordinates by their accuracy, the bars as distances between their
    markers, the GNSS positions of the photos (``gnss`` "plain", or "bias" with a bias per
    flight, the focal length refined; "auto" is "plain" with the focal length of the alignment
    kept when the positions can hold the block and no fixed points do, else "off") and the
    ``rolling_shutter`` of the camera (a velocity per photo)."""
    progress = as_progress(progress)
    if engine not in ("colmap", "ppg"):
        raise ValueError(f"unknown adjustment engine {engine!r}: colmap or ppg")
    if gnss not in GNSS_MODES:
        raise ValueError(f"gnss: one of {', '.join(GNSS_MODES)}, not {gnss!r}")
    if engine == "colmap" and gnss in ("plain", "bias"):
        raise ValueError(f"gnss={gnss!r} is an option of engine='ppg': COLMAP's adjustment takes 'auto' or 'off'")
    before = {False: _gnss_offsets(chunk), True: _gnss_offsets(chunk, placed=True)}
    kept = None
    try:
        kept = _keep_alignment(chunk) if before[False] else None  # what the safety net puts back
        try:
            if engine == "ppg":
                mode, hold_focal = gnss, False
                if gnss == "auto":  # the positions hold the block as with COLMAP: no fixed points, focal length kept
                    held = not fixed_points_hold(chunk, use_markers) and bool(
                        _held_positions(chunk, chunk.aligned_cameras)[0])
                    mode, hold_focal = ("plain", True) if held else ("off", False)
                err = _optimize_own(chunk, fit_principal_point, use_markers, mode, rolling_shutter, progress,
                                    hold_focal=hold_focal)
            else:
                err = _optimize_colmap(chunk, fit_principal_point, use_markers, marker_weight, gnss, progress)
            if kept is not None:
                err = _undo_a_bend(chunk, before, kept, err, progress)
        except BaseException as exc:  # stopped half way (an error, a cancel): the alignment of before comes back
            if kept is not None and not kept["restored"]:
                try:
                    _put_back(chunk, kept)
                    log.warning("camera optimisation stopped (%s): the alignment before it is put back",
                                type(exc).__name__)
                except Exception:  # pragma: no cover - the original error is the one to see
                    log.exception("the alignment before the optimisation could not be put back")
            raise
    finally:
        if kept is not None:
            shutil.rmtree(kept["model"], ignore_errors=True)
    return err


def _optimize_colmap(chunk: Chunk, fit_principal_point: bool, use_markers: bool, marker_weight: int, gnss: str,
                     progress: Progress) -> float:
    """:func:`optimize_cameras` with COLMAP's adjustment."""
    import pycolmap

    from ..markers import gcp

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
    constrained = fixed_points_hold(chunk, use_markers)  # fewer control points do not place the chunk
    if constrained:
        gcp.update_transform(chunk)  # the reference coordinates and the bars in the internal frame
    before = errors() if constrained else {}
    priors, held = ([], []) if gnss == "off" or constrained else _gnss_priors(chunk, rec)
    solver = None
    progress(0.1, "Optimizing camera alignment" + (" on the control points and scale bars" if constrained else "")
             + (f" held by the GNSS positions of {len(priors)} photos" if priors else ""))
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
        if constrained and gnss != "off":  # the markers could not be fixed: the positions hold the block
            priors, held = _gnss_priors(chunk, rec)
        if priors:
            solver = _solver_outcome(_adjust_with_priors(rec, opts, priors, fixed_cams), len(priors))
        else:
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
    holding = bool(solver and solver["usable"])  # a solve that cannot be used held nothing
    report["gnss"] = {"mode": gnss, "photos": len(priors) if holding else 0,
                      "states": _gnss_states(held) if holding else {}, **(solver or {})}
    report["focal_held"] = holding
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
