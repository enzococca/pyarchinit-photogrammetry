"""The matching step of the native engine: ``match_photos(engine="native")``.

The features of the photos are extracted by the native core (``ppg_core`` API 13,
:func:`.._native.native_match_kernels`) into a feature store, the pairs that overlap are found on a small store
(the generic preselection), the pairs are matched in two stages into a match store, and the result is exported to
COLMAP's database - cameras, images, key points, matches and two-view geometries as the classic step writes them -
for the classic mapper (:func:`.sfm.align_cameras`) to run on it. The arguments and the effects are those of
:func:`.sfm.match_photos`: ``database.db`` in the sfm folder of the chunk, ``chunk.meta["sfm"]`` with the same keys
plus ``engine`` (``native``), ``features`` (``gpu``, ``cpu`` or ``gpu+cpu``: who detected the points) and ``routes``
(the pairs by the route of the matching: ``guided``, ``brute_force``, ``dropped``);
``chunk.meta["timings"]["match_photos"]`` with the stages ``features``, ``preselection``, ``pairs`` and ``export``.
``guided_matching`` is ignored: the native matching always matches along the epipolar lines.

Layout, under ``<sfm>/native/``: ``features/`` (the feature store: every photo, in the pixels of the full photo with
COLMAP's centre), ``small/`` (the store of the preselection, deleted at the end of the step), ``matches/`` (the match
store). A fresh matching works in ``<sfm>/native.tmp/`` and ``<sfm>/database.native.tmp.db`` and moves them into
place (``os.replace``) only when it has finished, so a cancel, a failure or a full disk leaves the project as it was;
anything ``.tmp`` left by a crash is removed at the start of the next step. ``reset=False`` continues: the stores of
an earlier native matching - trusted when the record of the chunk says ``native`` with the same quality, point limit,
masks and photo folder, and the alignment can take more photos (:func:`.sfm._continuable`) - take the photos missing
from them and the pairs with one of them, in place; without them, a classic database that can be continued is
imported into fresh stores first (:func:`import_database`); else everything is done again. The export keeps the ids
of the photos, cameras, rigs and frames of the database it continues: the model continues from them.

The thresholds of the two-stage matching live here (:data:`STAGE1_POINTS`, :data:`TRUST_INLIERS`,
:data:`DROP_INLIERS`) and go to the core with the options; ``PPG_NATIVE_DCT=0`` decodes every JPEG whole (no DCT
scaling), ``PPG_NATIVE_CPU_SHARE=0`` leaves the pairs to the GPUs when there is one. The database is read with
:func:`.sfm.read_only_uri` and photos are named as :func:`.sfm.image_root_and_names` names them (POSIX relative
paths, blanks allowed): the stores key them by name and never make a file name of one.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

import numpy as np

from .. import _native
from ..core.chunk import QUALITY_DOWNSCALE, Chunk
from ..core.progress import Progress, as_progress
from . import sfm, timing

log = logging.getLogger(__name__)

# the two-stage matching of a pair (native/engine/src/pairs.rs): the points of the largest scale of each photo the
# first stage matches; the inliers of the first stage from which it is trusted and the second stage follows the
# epipolar lines; below that, the inliers from which a pair that was not preselected still goes to brute force
STAGE1_POINTS = 8000
TRUST_INLIERS = 40
DROP_INLIERS = 8

STORES = "native"  # <sfm>/native/{features,small,matches}
TMP = "native.tmp"  # where a fresh matching works, moved into place when it has finished
OLD = "native.old.tmp"  # the stores being replaced, for the moment of the move
TMP_DB = "database.native.tmp.db"  # the database being written, moved into place when it has finished
FEATURES, SMALL, MATCHES = "features", "small", "matches"
INDEX = "index.json"
READERS = 4  # the threads that decode and reduce the photos while the kernels work (as the classic GPU path)

RIGS_REASON = "rigs of 360-degree photos: the native engine does not do them"


def dct_enabled() -> bool:
    """``PPG_NATIVE_DCT`` (default on): a JPEG decoded at the DCT scale its reduction allows."""
    return os.environ.get("PPG_NATIVE_DCT", "1").strip() != "0"


def cpu_share_enabled() -> bool:
    """``PPG_NATIVE_CPU_SHARE`` (default on): the CPU takes pairs beside the GPUs."""
    return os.environ.get("PPG_NATIVE_CPU_SHARE", "1").strip() != "0"


def _kernels():
    k = _native.native_match_kernels()
    if k is None:
        raise RuntimeError(f"the native core with the native matching (API {_native.NATIVE_MATCH_API}) is not "
                           "available")
    return k


def cannot_run(chunk: Chunk, matcher: str, reset: bool) -> str | None:
    """Why the native step cannot do the matching of ``chunk``, or None when nothing keeps it: the native core
    missing, switched off (``PPG_NATIVE=0``) or below API 13; a learned ``matcher``; rigs of 360-degree photos;
    processing nodes that would share the points (:mod:`.nodes`, as the classic step counts them: present, and
    enough photos to detect); a camera model the engine does not know (the model the photos are aligned with,
    :func:`.sfm.mapping_camera`). A chunk without enabled photos or without a folder is not the engine's to refuse:
    the step says so itself, whichever engine runs it."""
    from . import nodes, panorama

    if not _native.enabled():
        return "the native core is switched off (PPG_NATIVE=0)"
    if _native._core() is None:
        return "the native core (ppg_core) is not installed"
    if _native.native_match_kernels() is None:
        return (f"the native core {_native.version()} (API {_native.api()}) has no native matching: API "
                f"{_native.NATIVE_MATCH_API} is needed")
    if matcher != "sift":
        return f"learned matcher '{matcher}': the native engine matches SIFT points only"
    try:
        _root, names = sfm.image_root_and_names(chunk)
    except RuntimeError:
        return None
    if panorama.rig_configs(chunk, names):
        return RIGS_REASON
    for skey in sorted({c.sensor_key for c in chunk.cameras if c.enabled}):
        sensor = chunk.sensors[skey]
        model = sfm.mapping_camera(sensor)[0]
        if not _native.supports(model):
            return f"camera model {model} of sensor '{sensor.label}': the native engine does not know it"
    try:
        files_dir = chunk.data_dir.parent
    except RuntimeError:
        return None
    if nodes.wanted(files_dir):
        db_path = chunk.data_dir / "sfm" / "database.db"
        existing = sfm._db_image_names(db_path) if not reset and db_path.exists() else set()
        if len([n for n in names.values() if n not in existing]) >= 2 * nodes.FEATURE_UNIT_PHOTOS:
            return "processing nodes share the points of the photos: the native engine does not use them"
    return None


def match_photos(chunk: Chunk, quality: str = "high", keypoint_limit: int = 40000, tiepoint_limit: int = 8000,
                 pair_selection: str = "auto", progress: Progress | None = None, exclude_stationary: bool = False,
                 generic: bool | None = None, reference: str | None = None,
                 neighbours: int = sfm.REFERENCE_NEIGHBOURS, apply_masks: str = "keypoints",
                 guided_matching: bool = False, reset: bool = True, gpu: str | None = None) -> None:
    """The matching of :func:`.sfm.match_photos` done by the native engine (the module's docstring): the same
    arguments, the same ``database.db`` for the alignment, the same record in ``chunk.meta["sfm"]`` with ``engine``,
    ``features`` and ``routes`` beside, the seconds of the step and of its stages (``features``, ``preselection``,
    ``pairs``, ``export``) in ``chunk.meta["timings"]["match_photos"]``, also when the step fails or is cancelled
    (then with the name of the type of the error under ``failed``). ``guided_matching`` is ignored, with a line in the
    log. What stops the step comes out as it is: :class:`Cancelled`, an ``OSError`` of a full disk, the errors of the
    core; the project is left as it was."""
    stages, started = timing.Stages(), time.perf_counter()
    failed = None
    try:
        _match_photos(chunk, stages, quality=quality, keypoint_limit=keypoint_limit, tiepoint_limit=tiepoint_limit,
                      pair_selection=pair_selection, progress=progress, exclude_stationary=exclude_stationary,
                      generic=generic, reference=reference, neighbours=neighbours, apply_masks=apply_masks,
                      guided_matching=guided_matching, reset=reset, gpu=gpu)
    except BaseException as exc:
        failed = type(exc).__name__
        raise
    finally:
        timing.store(chunk, "match_photos", time.perf_counter() - started, stages.as_dict(),
                     **({"failed": failed} if failed else {}))


def _remove_leftovers(work: Path) -> None:
    """What a step that died left in the sfm folder: its work folder, the stores it was replacing, its database
    being written, and the store of the preselection (made again by the step that needs it)."""
    for folder in (work / TMP, work / OLD, work / STORES / SMALL):
        shutil.rmtree(folder, ignore_errors=True)
    for pattern in (f"{TMP_DB}*", "database.db.tmp*"):
        for path in work.glob(pattern):
            path.unlink(missing_ok=True)


def _stores_present(stores: Path) -> bool:
    return (stores / FEATURES / INDEX).is_file() and (stores / MATCHES / INDEX).is_file()


def _stores_trusted(chunk: Chunk, stores: Path, root: Path, quality: str, keypoint_limit: int,
                    apply_masks: str) -> bool:
    """Whether the stores in ``stores`` are those of a native matching of these photos with these settings: the
    record of the chunk says so (an earlier classic matching, or other settings, made the database from other
    points, and the stores would be trusted over it)."""
    info = chunk.meta.get("sfm") or {}
    return (_stores_present(stores) and info.get("engine") == "native" and info.get("quality") == quality
            and info.get("keypoint_limit") == int(keypoint_limit) and info.get("apply_masks") == apply_masks
            and info.get("image_root") == str(root))


def _camera(sensor) -> tuple:
    """A camera as the kernels take it: the model the photos are aligned with (:func:`.sfm.mapping_camera`), the
    size of the sensor, a prior focal length (as both classic paths give every camera)."""
    model, params = sfm.mapping_camera(sensor)
    return model, [float(p) for p in params], int(sensor.width), int(sensor.height), True


def _relay(sub: Progress, text: str, noun: str):
    """The progress callback of a step of the core (``(done, total, message)``) onto ``sub``; an exception of
    ``sub`` (a cancel) stops the step."""

    def call(done, total, _message):
        sub(done / total if total else 1.0, f"{text}: {done} of {total}" + (f" {noun}" if noun else ""))

    return call


def _features_word(gpu: int, cpu: int) -> str:
    return "gpu+cpu" if gpu and cpu else ("gpu" if gpu else "cpu")


def _gpu_devices(gpu: str | None) -> tuple[list[int], str | None]:
    """The GPUs the kernels work on for the setting ``gpu`` (:func:`.._native.gpu_devices`) and their names for the
    log; one that does not answer is left out with a warning, as the classic step leaves it."""
    devices, names = [], []
    for d in _native.gpu_devices(gpu):
        try:
            names.append(_native.kernel("gpu_device_name")(d))
            devices.append(d)
        except Exception as exc:  # noqa: BLE001 - a GPU that does not work: the others, or the CPU alone
            log.warning("GPU %s unavailable for the native matching (%s)", d, exc)
    return devices, (", ".join(names) if devices else None)


def _match_photos(chunk: Chunk, stages: timing.Stages, *, quality: str, keypoint_limit: int, tiepoint_limit: int,
                  pair_selection: str, progress: Progress | None, exclude_stationary: bool, generic: bool | None,
                  reference: str | None, neighbours: int, apply_masks: str, guided_matching: bool, reset: bool,
                  gpu: str | None) -> None:
    """The matching of :func:`match_photos`, each of its parts timed as a stage of ``stages``."""
    from ..markers import gcp
    from . import panorama
    from .masks import colmap_mask_folder

    k = _kernels()
    progress = as_progress(progress)
    if apply_masks not in sfm.MASK_MODES:
        raise ValueError(f"apply_masks: one of {', '.join(sfm.MASK_MODES)}")
    generic, reference = sfm._preselection(pair_selection, generic, reference)
    if guided_matching:
        log.info("the native matching always matches along the epipolar lines (guided)")
    try:  # the matching needs no system: what the alignment would refuse is only said here
        gcp.preflight(chunk)
    except Exception as exc:  # noqa: BLE001 - whatever it is: the references may still be switched off before it
        log.warning("the alignment will not place the chunk by the positions of the photos while they are switched on: "
                    "%s", exc)
    work = sfm._sfm_dir(chunk)
    db_path = work / "database.db"
    _remove_leftovers(work)
    sfm._interrupted_names_back(db_path)  # the names a classic matching that died in COLMAP left aliased
    root, names = sfm.image_root_and_names(chunk)
    if panorama.rig_configs(chunk, names):
        raise RuntimeError(RIGS_REASON)
    downscale = QUALITY_DOWNSCALE.get(quality, 2)
    stores = work / STORES
    everyone = sorted(names.values())

    # what to continue: the stores of an earlier native matching, a classic database, or nothing
    continuable = (not reset) and sfm._continuable(chunk, db_path, root, names, "sift")
    keep_stores = continuable and _stores_trusted(chunk, stores, root, quality, keypoint_limit, apply_masks)
    if keep_stores:  # photos taken out of the chunk since: their features would go into the database
        keep_stores = set(k.store_names(str(stores / FEATURES))) <= set(everyone)
    keep_import = continuable and not keep_stores
    keep = keep_stores or keep_import
    if not reset and not keep:
        progress(0.0, "No alignment to continue: every photo again")
    wd = stores if keep_stores else work / TMP  # the folder the stores are made in
    tmp_db = work / TMP_DB
    old_info = chunk.meta.get("sfm") or {}
    try:
        if keep_import:
            progress(0.0, "Importing the matches of the alignment to continue")
            imported = import_database(db_path, wd / FEATURES, wd / MATCHES)
            log.info("%d photos of the database imported into the stores of the native matching", imported)
        features_dir, small_dir, matches_dir = wd / FEATURES, wd / SMALL, wd / MATCHES

        def names_in(store_dir: Path) -> set[str]:
            # the store opened for the question: a handle kept open would not see what the kernels wrote since
            return set(k.store_names(str(store_dir))) if (store_dir / INDEX).is_file() else set()

        existing = names_in(features_dir) if keep else set()
        by_sensor: dict[int, list[str]] = {}
        for c in chunk.cameras:
            if c.enabled:
                by_sensor.setdefault(c.sensor_key, []).append(names[c.key])
        new = [n for group in by_sensor.values() for n in group if n not in existing]
        n = len(names)
        if generic is None:
            generic = n > sfm.EXHAUSTIVE_UP_TO  # every pair is cheap enough up to there
        # the size of the preselection images: the one the stores were made with when continuing them (the raster
        # of a JPEG decoded at a DCT scale depends on it), else the preselection's when it runs
        small_size = old_info.get("preselection_size") if keep_stores else (sfm.PRESELECTION_SIZE if generic else None)
        devices, gpu_names = _gpu_devices(gpu)
        dct, cpu_share = dct_enabled(), cpu_share_enabled()
        sensor_of = {names[c.key]: c.sensor_key for c in chunk.cameras if c.enabled}

        def long_side(name: str) -> int:
            s = chunk.sensors[sensor_of[name]]
            return max(int(s.width), int(s.height))

        def limit_of(name: str) -> int:
            return int(long_side(name) / downscale)

        def small_limit_of(name: str) -> int | None:
            return min(int(small_size), long_side(name)) if small_size else None

        with stages.stage("features"):  # the points of the photos
            masks = colmap_mask_folder(chunk, names, work / "masks") if apply_masks == "keypoints" else None
            progress(0.0, f"Detecting points in {len(new)} photos" + (" (with masks)" if masks else ""))
            jobs = [(name, str(root / name), str(masks / f"{name}.png") if masks is not None else None, limit_of(name),
                     small_limit_of(name)) for name in new]
            sub = progress.sub(0.0, 0.45)
            report = k.features(str(features_dir), str(small_dir) if small_size else None, jobs, int(keypoint_limit),
                                sfm.PRESELECTION_POINTS, devices, dct, cpu_share, READERS,
                                _relay(sub, "Detecting points", "photos"))
            for line in report["log"]:
                log.info("native features: %s", line)
            counts = {"gpu": report["gpu"], "cpu": report["cpu"]}
            for name in report["unsupported"]:  # files the core does not decode: OpenCV does, the kernels take pixels
                given = _features_from_pixels(k, root, name, masks, features_dir, small_dir if small_size else None,
                                              limit_of(name), small_limit_of(name), int(keypoint_limit), devices)
                if given is None:
                    log.warning("%s: unreadable, no key points", name)
                else:
                    counts[given] += 1
            for name in report["unreadable"]:
                log.warning("%s: unreadable, no key points", name)
            with_features = names_in(features_dir)
            if generic:  # the photos continued have no preselection image: theirs, into the small store
                present_small = names_in(small_dir)
                small_jobs = [(name, str(root / name), str(masks / f"{name}.png") if masks is not None else None,
                               min(sfm.PRESELECTION_SIZE, long_side(name)), None)
                              for name in everyone if name not in present_small and name in with_features]
                if small_jobs:
                    more = k.features(str(small_dir), None, small_jobs, sfm.PRESELECTION_POINTS,
                                      sfm.PRESELECTION_POINTS, devices, dct, cpu_share, READERS,
                                      _relay(sub, "Reducing photos for the pairs", "photos"))
                    for line in more["log"]:
                        log.info("native features: %s", line)
            features = _features_word(counts["gpu"], counts["cpu"])
            sub(1.0, f"Points detected in {len(with_features)} photos"
                + (f" on the {features.upper()}" if counts["gpu"] else ""))

        present = sorted(with_features & set(everyone))
        cams = {name: _camera(chunk.sensors[sensor_of[name]]) for name in present}
        pairs: set[tuple[str, str]] | None = None
        chosen: set[tuple[str, str]] = set()  # the pairs a preselection chose: never dropped by the first stage
        parts = []
        if generic:
            progress(0.45, "Finding the pairs of photos that overlap")
            with stages.stage("preselection"):
                small_names = [name for name in sorted(names_in(small_dir)) if name in cams]
                sub = progress.sub(0.45, 0.1)
                found_pairs = k.preselect(str(small_dir), {name: cams[name] for name in small_names}, devices,
                                          sfm.PRESELECTION_INLIERS,
                                          _relay(sub, "Finding the pairs of photos that overlap", ""))
                for line in found_pairs["log"]:
                    log.info("native preselection: %s", line)
                weights = {(a, b): int(w) for a, b, w in found_pairs["weights"]}
                found = (sfm.strongest_pairs(weights, sfm.PRESELECTION_PAIRS_PER_PHOTO)
                         if sfm.PRESELECTION_PAIRS_PER_PHOTO else set(weights))
                found |= set(sfm._reference_pairs(chunk, names, neighbors=8) or [])
                shutil.rmtree(small_dir, ignore_errors=True)  # its only use
                sub(1.0, f"{len(found):,} pairs of photos overlap")
            if found:  # nothing found on the small images: every pair
                pairs, parts = set(found), ["generic"]
                chosen |= set(found)
        if reference:
            finder = {"source": sfm._reference_pairs, "estimated": sfm._estimated_pairs,
                      "sequential": sfm._sequential_pairs}[reference]
            with stages.stage("preselection"):
                found = finder(chunk, names, max(int(neighbours), 1))
            if found:
                pairs = (pairs or set()) | set(found)
                chosen |= set(found)
                parts.append(reference)
            elif not pairs:
                raise RuntimeError(f"the {reference} preselection needs "
                                   + ("camera reference positions" if reference == "source" else "an alignment"))
        mode = "+".join(parts) if pairs else "exhaustive"
        if pairs is None:
            pairs = {(a, b) for i, a in enumerate(everyone) for b in everyone[i + 1:]}
        if keep:  # only the pairs with a photo added since
            added = set(new)
            pairs = {p for p in pairs if p[0] in added or p[1] in added}
        pair_list = [(a, b, (a, b) in chosen) for a, b in sorted(pairs) if a in cams and b in cams]

        with stages.stage("pairs"):
            progress(0.55, f"Matching photos ({mode}, {len(pair_list):,} pairs)"
                     + (f" on {gpu_names} and the CPU" if devices and cpu_share else f" on {gpu_names}" if devices
                        else ""))
            options = {"stage1_points": STAGE1_POINTS, "trust_inliers": TRUST_INLIERS, "drop_inliers": DROP_INLIERS,
                       "max_num_matches": max(int(tiepoint_limit) * 4, 8192)}
            sub = progress.sub(0.55, 0.35)
            matched = k.match_pairs(str(features_dir), str(matches_dir), pair_list, cams, devices, options,
                                    _relay(sub, "Matching photos", "pairs"), cpu_share)
            for line in matched["log"]:
                log.warning("native matching: %s", line)
            by_device = {str(name): int(count) for name, count in matched["by_device"].items()}
            gpu_shares = {name: count for name, count in by_device.items() if name != "CPU"}
            routes = {"guided": int(matched["guided"]), "brute_force": int(matched["brute_force"]),
                      "dropped": int(matched["dropped"])}
            sub(1.0, f"Matched {matched['done']:,} pairs: {routes['guided']:,} along the epipolar lines, "
                     f"{routes['brute_force']:,} by brute force, {routes['dropped']:,} dropped")

        with stages.stage("export"):  # COLMAP's database for the mapper, then everything into place
            progress(0.9, "Writing the matches for the alignment")
            export_database(chunk, tmp_db, descriptors=False, stores=wd, previous=db_path if keep else None,
                            added=new if keep else ())
            if exclude_stationary:
                removed = sfm.remove_stationary_matches(tmp_db)
            else:  # a continuation keeps the count of the step it continues (the export applied its removal again)
                removed = old_info.get("stationary_removed") if keep else None
            os.replace(tmp_db, db_path)
            if wd != stores:
                if stores.exists():
                    os.replace(stores, work / OLD)
                os.replace(wd, stores)
                shutil.rmtree(work / OLD, ignore_errors=True)
            shutil.rmtree(stores / SMALL, ignore_errors=True)
    except BaseException:
        if wd != stores:
            shutil.rmtree(wd, ignore_errors=True)
        else:
            shutil.rmtree(stores / SMALL, ignore_errors=True)
        for path in work.glob(f"{TMP_DB}*"):
            path.unlink(missing_ok=True)
        raise

    gpu_pairs = sum(gpu_shares.values())
    chunk.meta["sfm"] = {"engine": "native", "image_root": str(root), "names": {str(k_): v for k_, v in names.items()},
                         "features": features, "matcher": "sift", "quality": quality, "pair_selection": mode,
                         "pairs": len(pair_list), "pairs_unmatched": 0, "generic": bool(generic),
                         "reference": reference,
                         "neighbours": int(neighbours), "masks": masks is not None, "apply_masks": apply_masks,
                         "guided_matching": bool(guided_matching), "stationary_removed": removed, "rigs": 0,
                         "keypoint_limit": int(keypoint_limit), "tiepoint_limit": int(tiepoint_limit),
                         "continued": keep, "added": new if keep else None, "nodes": [],
                         "gpu": ", ".join(gpu_shares) if gpu_shares else None, "gpu_pairs": gpu_pairs,
                         "gpu_shares": gpu_shares or None, "cpu_pairs": by_device.get("CPU", 0), "routes": routes,
                         "preselection_size": small_size}
    if keep and old_info.get("gps_priors"):
        chunk.meta["sfm"]["gps_priors"] = old_info["gps_priors"]  # the priors already in the database: same axes
    progress(1.0, "Matching finished" + (f" ({removed:,} stationary matches left out)" if removed else ""))


def _features_from_pixels(k, root: Path, name: str, masks: Path | None, features_dir: Path, small_dir: Path | None,
                          limit: int, small_limit: int | None, keypoint_limit: int, devices: list[int]) -> str | None:
    """The features of a photo the core does not decode (a format outside JPEG, PNG and TIFF), from the pixels OpenCV
    reads - COLMAP's grey of the raster with the EXIF orientation ignored, as the classic GPU path reads them -:
    ``gpu`` or ``cpu`` for who extracted them, None when OpenCV does not read the file either (or the photo is too
    small for the kernels)."""
    import cv2

    img = cv2.imread(str(root / name), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        return None
    if img.ndim == 3:
        img = cv2.transform(img, sfm._REC709_BGR)
    h, w = img.shape
    mask = str(masks / f"{name}.png") if masks is not None else None
    given = k.features_pixels(str(features_dir), str(small_dir) if small_dir is not None else None, name,
                              np.ascontiguousarray(img, np.uint8), (w, h), limit, small_limit, keypoint_limit,
                              sfm.PRESELECTION_POINTS, devices, mask=mask)
    for line in given["log"]:
        log.info("native features: %s", line)
    if given["done"] != 1:
        return None
    return "gpu" if given["gpu"] else "cpu"


# ----------------------------------------------------------------------------------- COLMAP's database
def _clear_features_and_matches(db_path: Path) -> None:
    """The key points, descriptors, matches and two-view geometries of a copied database gone: they are written
    again from the stores (COLMAP's documented sqlite layout, as :func:`.sfm.remove_stationary_matches` edits it)."""
    import sqlite3

    con = sqlite3.connect(str(db_path))
    try:
        for table in ("keypoints", "descriptors", "matches", "two_view_geometries"):
            con.execute(f"DELETE FROM {table}")  # noqa: S608 - a table name of COLMAP's layout
        con.commit()
    finally:
        con.close()


def _new_camera(db, sensor) -> tuple[int, int]:
    """A camera for ``sensor`` and the rig it is the reference sensor of, written to ``db`` as the classic GPU path
    writes them: ``(camera_id, rig_id)``."""
    import pycolmap

    model, params, width, height, _prior = _camera(sensor)
    camera = pycolmap.Camera(model=model, width=width, height=height, params=params)
    camera.has_prior_focal_length = True
    camera_id = db.write_camera(camera)
    rig = pycolmap.Rig()
    rig.add_ref_sensor(pycolmap.sensor_t(pycolmap.SensorType.CAMERA, camera_id))
    return camera_id, db.write_rig(rig)


def _new_image(db, name: str, camera_id: int, rig_id: int) -> int:
    """An image for the photo ``name`` and its frame in ``rig_id``, written to ``db``: the image id."""
    import pycolmap

    image_id = db.write_image(pycolmap.Image(name=name, camera_id=camera_id))
    frame = pycolmap.Frame()
    frame.rig_id = rig_id
    frame.add_data_id(pycolmap.data_t(pycolmap.sensor_t(pycolmap.SensorType.CAMERA, camera_id), image_id))
    db.write_frame(frame)
    return image_id


def _image_ids(db, chunk: Chunk, sensor_of: dict[str, int], held: set[str], continued: bool, added) -> dict[str, int]:
    """The image id of every photo to export, in ``db``: of a continued database (``continued``), the ids its images
    have, and a new image - with the camera and the rig of its sensor, or new ones - for each of ``added``; of a
    fresh database, a camera and a rig per sensor and an image per photo, as the classic step writes them."""
    import pycolmap

    ids: dict[str, int] = {}
    if continued:
        images = {im.name: im for im in db.read_all_images()}
        camera_of_sensor = {sensor_of[n]: int(im.camera_id) for n, im in images.items() if n in sensor_of}
        rig_of_camera = {int(r.ref_sensor_id.id): int(r.rig_id) for r in db.read_all_rigs()}
        ids = {n: int(im.image_id) for n, im in images.items() if n in held}
        for name in added:
            if name not in held or name in images:
                continue
            skey = sensor_of[name]
            if skey not in camera_of_sensor:  # a sensor new to the database: its camera and rig
                camera_of_sensor[skey], rig_id = _new_camera(db, chunk.sensors[skey])
                rig_of_camera[camera_of_sensor[skey]] = rig_id
            camera_id = camera_of_sensor[skey]
            if camera_id not in rig_of_camera:  # a camera of the database that is the reference sensor of no rig
                rig = pycolmap.Rig()
                rig.add_ref_sensor(pycolmap.sensor_t(pycolmap.SensorType.CAMERA, camera_id))
                rig_of_camera[camera_id] = db.write_rig(rig)
            ids[name] = _new_image(db, name, camera_id, rig_of_camera[camera_id])
        return ids
    by_sensor: dict[int, list[str]] = {}
    for name in sorted(held):
        by_sensor.setdefault(sensor_of[name], []).append(name)
    for skey, photos in by_sensor.items():
        camera_id, rig_id = _new_camera(db, chunk.sensors[skey])
        for name in photos:
            ids[name] = _new_image(db, name, camera_id, rig_id)
    return ids


def _two_view_geometry(r: dict):
    """COLMAP's two-view geometry of a pair of the match store: the configuration, the inlier matches, the models
    estimated and the relative pose when there is one."""
    import pycolmap

    tvg = pycolmap.TwoViewGeometry()
    tvg.config = pycolmap.TwoViewGeometryConfiguration(int(r["config"]))
    tvg.inlier_matches = np.ascontiguousarray(r["inliers"], np.uint32)
    for key in ("F", "E", "H"):
        if r[key] is not None:
            setattr(tvg, key, np.ascontiguousarray(r[key], np.float64))
    if r["qvec"] is not None:
        w, x, y, z = (float(v) for v in r["qvec"])
        tvg.cam2_from_cam1 = pycolmap.Rigid3d(pycolmap.Rotation3d(np.array([x, y, z, w])),
                                              np.asarray(r["tvec"], np.float64))
    return tvg


def export_database(chunk: Chunk, db_path: Path, descriptors: bool = False, *, stores: Path | None = None,
                    previous: Path | None = None, added=()) -> None:
    """COLMAP's database of the stores of the native matching (``stores``: ``<sfm>/native`` of the chunk when not
    given), written whole to ``db_path`` - a temporary file beside it, moved into place when complete -: the cameras
    by sensor as the classic step writes them (the model of :func:`.sfm.mapping_camera`, a prior focal length), a
    rig and a frame per photo, the key points as four columns, the matches and two-view geometries of every pair of
    the match store whose photos are in the database, the descriptors only when asked (the mapper needs none). The
    photos are those of the chunk that the feature store holds. ``previous`` (a database to continue; ``db_path``
    itself when it is there and none is given): copied first, its features and matches cleared and written again,
    so that its images, cameras, rigs, frames and pose priors keep their ids (the model of the alignment names them)
    and the photos of ``added`` come after them, with the camera and the rig of their sensor; the stationary matches
    the step it continues left out are left out again (:func:`.sfm.remove_stationary_matches`, when the record of
    the chunk has a count). A chunk without the feature store is a FileNotFoundError."""
    import pycolmap

    k = _kernels()
    db_path = Path(db_path)
    stores = Path(stores) if stores is not None else sfm._sfm_dir(chunk) / STORES
    if not (stores / FEATURES / INDEX).is_file():
        raise FileNotFoundError(f"{stores / FEATURES}: no feature store of the native matching")
    features = k.feature_store(str(stores / FEATURES))
    matches = k.match_store(str(stores / MATCHES)) if (stores / MATCHES / INDEX).is_file() else None
    _root, names = sfm.image_root_and_names(chunk)
    sensor_of = {names[c.key]: c.sensor_key for c in chunk.cameras if c.key in names}
    held = set(features.names()) & set(names.values())
    tmp = db_path.with_name(db_path.name + ".tmp")
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)
    if previous is None and db_path.exists():
        previous = db_path
    continued = previous is not None and Path(previous).exists()
    if continued:
        shutil.copyfile(previous, tmp)
        _clear_features_and_matches(tmp)
    db = pycolmap.Database.open(str(tmp))
    try:
        with pycolmap.DatabaseTransaction(db):
            ids = _image_ids(db, chunk, sensor_of, held, continued, added)
            for name, image_id in ids.items():
                kp, desc, _implementation = features.read(name, descriptors)
                db.write_keypoints(image_id, np.ascontiguousarray(kp, np.float32))
                if descriptors:
                    db.write_descriptors(image_id, pycolmap.FeatureDescriptors(pycolmap.FeatureExtractorType.SIFT,
                                                                               np.ascontiguousarray(desc, np.uint8)))
            if matches is not None:
                for a, b in matches.pairs():
                    if a not in ids or b not in ids:
                        continue
                    r = matches.read(a, b)
                    if r is None:
                        continue
                    db.write_matches(ids[a], ids[b], np.ascontiguousarray(r["matches"], np.uint32))
                    db.write_two_view_geometry(ids[a], ids[b], _two_view_geometry(r))
    finally:
        db.close()
    if continued and (chunk.meta.get("sfm") or {}).get("stationary_removed") is not None:
        sfm.remove_stationary_matches(tmp)
    os.replace(tmp, db_path)


def _four_columns(kp: np.ndarray) -> np.ndarray:
    """COLMAP's key points as the four columns of the store: the six of its extractor (x, y and the affine shape
    a11, a12, a21, a22) become ``(x, y, sqrt(a11^2 + a21^2), atan2(a21, a11))``; four are kept; two get a scale of 1
    and no orientation."""
    kp = np.asarray(kp, np.float32)
    if kp.shape[1] == 6:
        a11, a21 = kp[:, 2], kp[:, 4]
        return np.column_stack([kp[:, 0], kp[:, 1], np.hypot(a11, a21), np.arctan2(a21, a11)]).astype(np.float32)
    if kp.shape[1] >= 4:
        return np.ascontiguousarray(kp[:, :4])
    return np.column_stack([kp[:, 0], kp[:, 1], np.ones(len(kp), np.float32), np.zeros(len(kp), np.float32)])


def _blob(data, count: int) -> np.ndarray | None:
    """A float64 blob of ``count`` numbers of COLMAP's two-view geometries, or None when it is NULL or empty."""
    if data is None or len(data) < 8 * count:
        return None
    return np.frombuffer(bytes(data), np.float64, count).copy()


def import_database(db_path: Path, features_dir: Path, matches_dir: Path) -> int:
    """The features and the matches of COLMAP's database ``db_path`` (a classic matching) into the feature store
    ``features_dir`` and the match store ``matches_dir`` (made when missing): every photo with key points and
    descriptors, its key points as four columns (:func:`_four_columns`) and COLMAP's image id in its header, as
    ``imported``; every pair of the two-view geometries (and of the matches) with its matches, inliers,
    configuration, models and pose, as ``imported``. The names a classic matching that died left aliased are put
    back first (:func:`.sfm._interrupted_names_back`); the database is read read-only. Returns the photos
    imported."""
    import sqlite3

    k = _kernels()
    db_path = Path(db_path)
    sfm._interrupted_names_back(db_path)
    features = k.feature_store(str(features_dir))
    matches = k.match_store(str(matches_dir))
    con = sqlite3.connect(sfm.read_only_uri(db_path), uri=True)
    try:
        images = {int(i): (str(n), int(c)) for i, n, c in con.execute("SELECT image_id, name, camera_id FROM images")}
        sizes = {int(c): (int(w), int(h)) for c, w, h in con.execute("SELECT camera_id, width, height FROM cameras")}
        imported = without_descriptors = 0
        for image_id, rows, cols, data in con.execute("SELECT image_id, rows, cols, data FROM keypoints"):
            if image_id not in images or not rows or not data:
                continue
            row = con.execute("SELECT rows, cols, data FROM descriptors WHERE image_id = ?", (image_id,)).fetchone()
            if row is None or not row[0] or not row[2]:
                without_descriptors += 1  # a database the native step exported for the mapper: nothing to match with
                continue
            kp = _four_columns(np.frombuffer(bytes(data), np.float32).reshape(int(rows), int(cols)))
            desc = np.frombuffer(bytes(row[2]), np.uint8).reshape(int(row[0]), int(row[1]))
            if len(desc) != len(kp):
                log.warning("%s: %d key points and %d descriptors in the database: not imported", images[image_id][0],
                            len(kp), len(desc))
                continue
            name, camera_id = images[image_id]
            width, height = sizes.get(camera_id, (0, 0))
            features.write(name, kp, np.ascontiguousarray(desc), width, height, int(image_id))
            imported += 1
        raw = {}
        for pair_id, rows, cols, data in con.execute("SELECT pair_id, rows, cols, data FROM matches"):
            raw[int(pair_id)] = (np.frombuffer(bytes(data), np.uint32).reshape(int(rows), int(cols)) if rows and data
                                 else np.zeros((0, 2), np.uint32))
        geometries = {}
        for pair_id, rows, cols, data, config, f, e, h, qvec, tvec in con.execute(
                "SELECT pair_id, rows, cols, data, config, F, E, H, qvec, tvec FROM two_view_geometries"):
            inliers = (np.frombuffer(bytes(data), np.uint32).reshape(int(rows), int(cols)) if rows and data
                       else np.zeros((0, 2), np.uint32))
            geometries[int(pair_id)] = (inliers, int(config), _blob(f, 9), _blob(e, 9), _blob(h, 9), _blob(qvec, 4),
                                        _blob(tvec, 3))
    finally:
        con.close()
    empty = np.zeros((0, 2), np.uint32)
    unknown_configs = 0
    for pair_id in sorted(set(raw) | set(geometries)):
        id1, id2 = divmod(pair_id, sfm.COLMAP_MAX_IMAGES)
        if id1 not in images or id2 not in images:
            continue
        a, b = images[id1][0], images[id2][0]
        inliers, config, f, e, h, qvec, tvec = geometries.get(pair_id, (empty, 0, None, None, None, None, None))
        if config > 7:  # MULTIPLE, CALIBRATED_RIG: not configurations the verification gives; the inliers count
            unknown_configs += 1
            config = 0
        pose = qvec is not None and tvec is not None and not (np.allclose(qvec, [1, 0, 0, 0]) and np.allclose(tvec, 0))
        matches.write(a, b, np.ascontiguousarray(raw.get(pair_id, empty)), np.ascontiguousarray(inliers), config,
                      F=None if f is None or not f.any() else f.reshape(3, 3),
                      E=None if e is None or not e.any() else e.reshape(3, 3),
                      H=None if h is None or not h.any() else h.reshape(3, 3),
                      qvec=qvec if pose else None, tvec=tvec if pose else None)
    matches.flush()
    if without_descriptors:
        log.warning("%d photos of the database have key points but no descriptors: not imported, their points are "
                    "detected again", without_descriptors)
    if unknown_configs:
        log.warning("%d pairs of the database had a configuration the native engine does not give: kept as undefined, "
                    "with their inliers", unknown_configs)
    return imported
