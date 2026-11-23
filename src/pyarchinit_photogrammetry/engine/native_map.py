"""The alignment of the native engine: the global mapper of the native core behind ``align_cameras(engine="native")``.

The mapper (``ppg_core.NativeMapper``, API 16: :func:`.._native.native_mapper`) starts from COLMAP's database that
the matching wrote (:func:`read_database`: the cameras, the key points and the verified pairs with their inlier
matches) and goes through its stages one by one - the view graph, the rotations averaged, the tracks, the global
positioning (in the frame of the GPS of the photos when there are three positions or more, :func:`gnss_input`), the
rounds of adjustment - then writes the model in COLMAP's format for the rest of the alignment
(:func:`global_mapping`). Each stage is timed in ``chunk.meta["timings"]["align_cameras"]`` under COLMAP's name of
it where COLMAP has one (``view_graph``, ``rotation_averaging``, ``track_establishment``, ``global_positioning``,
``bundle_adjustment``, ``write``), and reported in ``chunk.meta["sfm"]["native_mapper"]`` (numbers and words: never
the name of a photo or a path).

The block is judged as milestone 1 judges the classic global one: right after the rotations (the photos placed),
right after the positioning (the cameras on the GPS of the photos, :func:`.sfm.fits_references`: the early check,
kept in ``chunk.meta["sfm"]["global_check"]``) and on the model written (:func:`.sfm._global_check`, the median
reprojection error, the share of the observations kept). A block that fails a check raises
:class:`NativeMapperRejected`; :func:`.sfm.align_cameras` then hands the alignment to the classic chain with its own
rules, and the reason is kept (``chunk.meta["sfm"]["native_fallback"]``, then the record of the engine).

What the native mapper does not do stays classic (:func:`cannot_run`): the incremental and hierarchical mappers,
photos added to an alignment, rigs, calibrations held fixed, camera models it does not know; and without the native
core of API 16 or with ``PPG_NATIVE=0`` nothing changes. ``auto`` gives it the alignment only once the step is in
:data:`.switch.AUTO_NATIVE_STEPS` and when the global mapper is the one ``auto`` picks (:func:`auto_takes`).

Two variables tune the mapper for the trials of the plan: ``PPG_NATIVE_TRACKS_PER_PHOTO`` (the tracks kept per photo
for the positioning and the first rounds; by default a quarter of the tie point limit of the matching, 500 at least,
as the classic global mapper) and ``PPG_NATIVE_FINAL_ALL_TRACKS=0`` (no last round with every track).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

import numpy as np

from .. import _native
from ..core.chunk import Chunk
from ..core.progress import Progress
from . import sfm, timing

log = logging.getLogger(__name__)

# the checks of the block the mapper built (a sound block of the synthetic baseline is at 0.5-1.6 px)
MAX_MEDIAN_REPROJECTION_PX = 2.0
MIN_KEPT_SHARE = 0.5  # of the observations the last filter of the rounds saw
PLACED_SHARE = 0.9  # of the photos: as the classic global check (sfm._global_check)
# the camera models of the mapper (native/engine/src/camera.rs, MODELS)
MODELS = ("SIMPLE_PINHOLE", "PINHOLE", "SIMPLE_RADIAL", "RADIAL", "OPENCV", "FULL_OPENCV")
# a verified pair, as milestone 2 counts them (bench/pairs.py): COLMAP's min_num_inliers, and a configuration the
# mapper takes (not UNDEFINED, DEGENERATE or WATERMARK)
MIN_INLIERS = 15
NOT_VERIFIED = frozenset({0, 1, 7})
DEFAULT_ACCURACY = [5.0, 5.0, 10.0]  # m: a position without its accuracy, as the pose priors of the classic engine
TRACKS_ENV = "PPG_NATIVE_TRACKS_PER_PHOTO"
FINAL_ALL_TRACKS_ENV = "PPG_NATIVE_FINAL_ALL_TRACKS"

# the stages of the mapper by the name its progress gives them: the name of the stage in the timings, and the text
# of the progress (the tests watch them to act at a given moment)
STAGES = {"view graph": "view_graph", "rotations": "rotation_averaging", "tracks": "track_establishment",
          "positioning": "global_positioning", "refinement": "bundle_adjustment"}
PROGRESS_STAGES = {"view graph": "Building the view graph", "rotations": "Averaging the rotations of the cameras",
                   "tracks": "Establishing the tracks", "positioning": "Positioning the cameras",
                   "refinement": "Adjusting the block"}
PROGRESS_WRITE = "Writing the model"

CONTINUATION_REASON = "photos added to an alignment are registered by the classic incremental mapper"
RIGS_REASON = "rigs are aligned by the classic engine"
HELD_REASON = "calibrations held fixed are aligned by the classic engine"


class NativeMapperRejected(RuntimeError):
    """The block of the native mapper refused by one of its checks; ``why`` says why (``placed N of M photos``,
    ``built a block whose cameras miss the GPS of the photos by X m``, ``median reprojection error X px``, ``kept X %
    of the observations``)."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def core_unavailable() -> str | None:
    """Why the native core cannot run the mapper on this computer, or None: switched off (``PPG_NATIVE=0``), missing
    or below API 16 - the reasons of :func:`cannot_run` that are of the installation, not of the request."""
    if not _native.enabled():
        return "PPG_NATIVE=0"
    if _native.native_mapper() is None:
        return f"the native core with the mapper (API {_native.MAPPER_API}) is not installed"
    return None


def cannot_run(chunk: Chunk, mapper: str, reset: bool) -> str | None:
    """Why the native mapper cannot align ``chunk``, or None when nothing keeps it: the native core switched off,
    missing or too old (:func:`core_unavailable`); the incremental or hierarchical ``mapper`` asked; a continuation
    (``reset`` False on an aligned chunk: the classic incremental mapper registers the photos added); rigs of
    360-degree photos; calibrations held fixed; a camera model the mapper does not know (the model the photos are
    aligned with, :func:`.sfm.mapping_camera`). A mapper that is not one, or a chunk without photos, is not the
    engine's to refuse: the alignment says so itself, whichever engine runs it."""
    reason = core_unavailable()
    if reason is not None:
        return reason
    if mapper in ("incremental", "hierarchical"):
        return f"mapper '{mapper}' asked"
    if not reset and chunk.aligned_cameras:
        return CONTINUATION_REASON
    if (chunk.meta.get("sfm") or {}).get("rigs"):
        return RIGS_REASON
    try:
        sfm.image_root_and_names(chunk)
    except RuntimeError:
        return None
    if sfm._held_names(chunk) or any(getattr(chunk.sensors.get(c.sensor_key), "fixed", False)
                                     for c in chunk.cameras if c.enabled):
        return HELD_REASON
    for skey in sorted({c.sensor_key for c in chunk.cameras if c.enabled}):
        sensor = chunk.sensors.get(skey)
        if sensor is None:  # a camera without its sensor: the alignment says what it does
            continue
        model = sfm.mapping_camera(sensor)[0]
        if model not in MODELS:
            return f"camera model {model} is not in the native mapper"
    return None


def auto_takes(chunk: Chunk, mapper: str) -> bool:
    """Whether ``auto`` gives the alignment of ``chunk`` to the native mapper (once the step is in
    :data:`.switch.AUTO_NATIVE_STEPS`): the ``mapper`` asked is ``auto`` or ``global`` and the one ``auto`` picks for
    the chunk is the global one (:func:`.sfm.auto_mapper`)."""
    if mapper not in ("auto", "global"):
        return False
    info = chunk.meta.get("sfm") or {}
    n = len(info.get("names") or {})
    return sfm.auto_mapper(chunk, n, fixed=bool(sfm._held_names(chunk)), rigs=bool(info.get("rigs"))) == "global"


def _camera_model_names() -> dict[int, str]:
    import pycolmap

    return {int(m.value): name for name, m in pycolmap.CameraModelId.__members__.items()}


def read_database(db_path: Path, names: dict[int, str]) -> dict:
    """The arrays of ``NativeMapper`` from COLMAP's database at ``db_path`` (opened read-only), for the photos of
    ``names`` (camera key -> name in the database): ``images`` (``(name, image_id, camera index)`` by image id),
    ``cameras`` (``(camera_id, model, params, width, height, prior_focal)`` of the cameras of those photos),
    ``keypoints`` (float32, x and y of every key point) and ``keypoint_offsets``, ``pairs`` (the indices of the two
    photos of every verified pair - at least :data:`MIN_INLIERS` inlier matches and a configuration not in
    :data:`NOT_VERIFIED` -), ``pair_config``, ``pair_f`` (the fundamental matrix, NaN without one), ``matches`` (the
    inlier matches, key point indices within each photo) and ``match_offsets``."""
    wanted = set(names.values())
    models = _camera_model_names()
    con = sqlite3.connect(sfm.read_only_uri(Path(db_path)), uri=True)
    try:
        rows = [(int(i), str(nm), int(c)) for i, nm, c in
                con.execute("SELECT image_id, name, camera_id FROM images ORDER BY image_id") if str(nm) in wanted]
        cam_rows = {int(r[0]): r for r in con.execute(
            "SELECT camera_id, model, width, height, params, prior_focal_length FROM cameras")}
        camera_ids = sorted({c for _i, _nm, c in rows})
        cam_index = {cid: k for k, cid in enumerate(camera_ids)}
        cameras = []
        for cid in camera_ids:
            _cid, model, width, height, params, prior = cam_rows[cid]
            cameras.append((cid, models[int(model)], [float(v) for v in np.frombuffer(params, np.float64)],
                            int(width), int(height), bool(prior)))
        images = [(nm, i, cam_index[c]) for i, nm, c in rows]
        index = {i: k for k, (i, _nm, _c) in enumerate(rows)}
        keypoints, offsets = [], [0]
        for i, _nm, _c in rows:
            got = con.execute("SELECT rows, cols, data FROM keypoints WHERE image_id = ?", (i,)).fetchone()
            if got is None or not got[0] or got[2] is None:
                points = np.zeros((0, 2), np.float32)
            else:
                points = np.frombuffer(got[2], np.float32).reshape(int(got[0]), int(got[1]))[:, :2]
            keypoints.append(points)
            offsets.append(offsets[-1] + len(points))
        pairs, configs, fs, matches, match_offsets = [], [], [], [], [0]
        for pair_id, n_rows, data, config, f in con.execute(
                "SELECT pair_id, rows, data, config, F FROM two_view_geometries ORDER BY pair_id"):
            if int(n_rows or 0) < MIN_INLIERS or int(config) in NOT_VERIFIED or data is None:
                continue
            a, b = divmod(int(pair_id), sfm.COLMAP_MAX_IMAGES)
            if a not in index or b not in index:
                continue
            pairs.append((index[a], index[b]))
            configs.append(int(config))
            fm = np.frombuffer(f, np.float64) if f else np.zeros(0)
            fs.append(fm if fm.size == 9 else np.full(9, np.nan))
            m = np.frombuffer(data, np.uint32).reshape(int(n_rows), 2)
            matches.append(m)
            match_offsets.append(match_offsets[-1] + len(m))
    finally:
        con.close()
    return {
        "images": images,
        "cameras": cameras,
        "keypoints": np.ascontiguousarray(np.vstack(keypoints) if keypoints else np.zeros((0, 2)), np.float32),
        "keypoint_offsets": np.asarray(offsets, np.int64),
        "pairs": np.asarray(pairs, np.int64).reshape(-1, 2),
        "pair_config": np.asarray(configs, np.int32),
        "pair_f": np.asarray(fs, np.float64).reshape(-1, 9),
        "matches": np.ascontiguousarray(np.vstack(matches) if matches else np.zeros((0, 2)), np.uint32),
        "match_offsets": np.asarray(match_offsets, np.int64),
    }


def gnss_input(chunk: Chunk, names) -> tuple[dict | None, dict]:
    """The positions of the photos for the positioning of the mapper: ``names`` are the photos of the mapper in its
    order (the names of ``images`` of :func:`read_database`). The reference positions of their cameras on metric axes
    less a round offset, as :func:`.sfm._write_pose_priors` puts them (the same choice of the coordinate system), with
    their accuracy (:data:`DEFAULT_ACCURACY` without one) and their flight (:func:`.bundle.flights`): ``{"image",
    "xyz", "sigma", "flight"}``, None with fewer than three positions. The second value is ``{"crs", "offset"}`` for
    the report (empty with None)."""
    from ..markers.gcp import camera_references
    from .bundle import flights

    keys = {v: int(k) for k, v in ((chunk.meta.get("sfm") or {}).get("names") or {}).items()}
    by_key = {c.key: c for c in chunk.cameras}
    index, cams = {}, []
    for i, name in enumerate(names):
        cam = by_key.get(keys.get(name))
        if cam is not None and cam.enabled and cam.reference.enabled and cam.reference.location:
            index[cam.key] = i
            cams.append(cam)
    positions, crs = camera_references(chunk, cams)
    cams = [c for c in cams if c.key in positions]
    if len(cams) < 3:
        return None, {}
    offset = np.round(np.mean([positions[c.key] for c in cams], axis=0))
    flight = flights(cams)
    sigma = [np.resize(np.maximum(np.asarray(c.reference.accuracy or DEFAULT_ACCURACY, float).reshape(-1)[:3], 1e-3), 3)
             for c in cams]
    gnss = {"image": np.array([index[c.key] for c in cams], np.int64),
            "xyz": np.array([np.asarray(positions[c.key], float) - offset for c in cams], np.float64),
            "sigma": np.array(sigma, np.float64),
            "flight": np.array([flight[c.key] for c in cams], np.int64)}
    return gnss, {"crs": crs, "offset": [float(v) for v in offset]}


class _Relay:
    """The progress callback of the mapper's stages: ``(done, total, stage)`` as a fraction of ``progress``, the
    text of a stage said once when it begins; a cancel of ``progress`` raises :class:`Cancelled` from inside the
    stage, which stops the mapper. It never calls the mapper."""

    def __init__(self, progress: Progress) -> None:
        self.progress = progress
        self.stage: str | None = None

    def __call__(self, done: int, total: int, stage: str) -> None:
        text = PROGRESS_STAGES.get(stage, stage) if stage != self.stage else ""
        self.stage = stage
        self.progress(min(1.0, done / total) * 0.95 if total else 0.0, text)
        self.progress.check()


def _plain(value):
    """``value`` (what the mapper reports) in the plain types of JSON: tuples as lists, numpy as Python."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def _opencv_sensors(chunk: Chunk, arrays: dict) -> list[bool]:
    """One flag per camera of ``arrays``: its photos are of a sensor whose model is OPENCV."""
    keys = {v: int(k) for k, v in (chunk.meta["sfm"].get("names") or {}).items()}
    by_key = {c.key: c for c in chunk.cameras}
    flags = [False] * len(arrays["cameras"])
    for name, _image_id, c in arrays["images"]:
        cam = by_key.get(keys.get(name))
        sensor = chunk.sensors.get(cam.sensor_key) if cam is not None else None
        if sensor is not None and sensor.model == "OPENCV":
            flags[c] = True
    return flags


def tracks_per_photo(chunk: Chunk, tiepoint_limit: int | None = None) -> int:
    """The tracks kept per photo: ``PPG_NATIVE_TRACKS_PER_PHOTO``, else a quarter of the tie point limit (500 at
    least) - ``tiepoint_limit`` when given, as the classic mappers take it, else the limit of the matching - as the
    classic global mapper keeps them."""
    word = os.environ.get(TRACKS_ENV, "").strip()
    if word:
        return max(1, int(word))
    limit = (chunk.meta.get("sfm") or {}).get("tiepoint_limit", 8000) if tiepoint_limit is None else tiepoint_limit
    return max(500, int(limit or 0) // 4)


def global_mapping(chunk: Chunk, db_path: Path, root: str, out: Path, n: int, progress: Progress,
                   stages: timing.Stages, tiepoint_limit: int | None = None, use_gps: bool | None = None) -> dict:
    """The block of the ``n`` photos of ``chunk`` built by the native mapper from the database ``db_path``, written in
    ``out/0`` in COLMAP's format and read back with the colours of the points from the photos under ``root``:
    ``{0: pycolmap.Reconstruction}``, as :func:`.sfm._global_mapping` returns its blocks. The stages are timed in
    ``stages`` and reported in ``chunk.meta["sfm"]["native_mapper"]``; a block that fails a check (the module) raises
    :class:`NativeMapperRejected`, a cancel of ``progress`` comes out as :class:`Cancelled`, and nothing is written
    outside ``out``. ``tiepoint_limit`` (None: the limit of the matching) sets the tracks per photo
    (:func:`tracks_per_photo`); ``use_gps`` False positions the block without the positions of the photos."""
    cls = _native.native_mapper()
    if cls is None:
        raise RuntimeError(core_unavailable() or "the native mapper is not available")
    info = chunk.meta["sfm"]
    report = info["native_mapper"] = {"engine": "native"}
    names = {int(k): v for k, v in (info.get("names") or {}).items()}
    relay = _Relay(progress)
    with stages.stage("view_graph"):  # the database read, and the view graph built from it
        arrays = read_database(db_path, names)
        options = {"opencv_sensor": _opencv_sensors(chunk, arrays),
                   "final_all_tracks": os.environ.get(FINAL_ALL_TRACKS_ENV, "").strip() != "0"}
        report["input"] = {"photos": len(arrays["images"]), "cameras": len(arrays["cameras"]),
                           "pairs": len(arrays["pairs"]), "matches": len(arrays["matches"])}
        mapper = cls(**arrays, options=options)
        try:
            report["view_graph"] = _plain(mapper.view_graph(relay))
        except BaseException:
            _keep_report(report, mapper)
            raise
    try:
        return _stages(chunk, mapper, arrays, relay, stages, report, root, out, n,
                       tracks_per_photo(chunk, tiepoint_limit), use_gps is None or bool(use_gps))
    finally:
        _keep_report(report, mapper)


def _keep_report(report: dict, mapper) -> None:
    """The report of ``mapper`` into ``report``; an error of ``report()`` itself never hides the error (or the
    cancel) that is on its way out, and is kept as a word."""
    try:
        report["report"] = _plain(mapper.report())
    except Exception as exc:  # noqa: BLE001 - the report is for the record only
        report["report"] = {"error": type(exc).__name__}


def _stages(chunk: Chunk, mapper, arrays: dict, relay: _Relay, stages: timing.Stages, report: dict, root: str,
            out: Path, n: int, per_photo: int, with_positions: bool = True) -> dict:
    """The stages of :func:`global_mapping` after the view graph: the checks, the model written and read back."""
    import pycolmap

    info = chunk.meta["sfm"]
    with stages.stage("rotation_averaging"):
        report["rotations"] = rotations = _plain(mapper.rotations(relay))
    if rotations["registered"] < PLACED_SHARE * n:
        info["global_check"] = {"after": "rotation_averaging", "fits": False, "residual_m": None}
        raise NativeMapperRejected(f"placed {rotations['registered']} of {n} photos")
    with stages.stage("track_establishment"):
        report["tracks"] = {**_plain(mapper.tracks(per_photo, relay)), "per_photo": per_photo}
    gnss, frame = gnss_input(chunk, [image[0] for image in arrays["images"]]) if with_positions else (None, {})
    with stages.stage("global_positioning"):
        position = mapper.position(gnss, relay)
    centres = np.asarray(position["centres"], float)
    placed = int(np.isfinite(centres).all(axis=1).sum())
    report["position"] = {**_plain({k: v for k, v in position.items() if k != "centres"}), "placed": placed,
                          "positions": 0 if gnss is None else len(gnss["image"]), **frame}
    # the early check, right after the positioning, as milestone 1's for the classic global mapper
    residual = float("nan")
    if placed < PLACED_SHARE * n:
        why = f"placed {placed} of {n} photos"
    else:
        fits = True
        if gnss is not None:
            ok = np.isfinite(centres[gnss["image"]]).all(axis=1)
            fits, residual = sfm.fits_references(centres[gnss["image"]][ok], gnss["xyz"][ok],
                                                 np.mean(gnss["sigma"][ok][:, :2], axis=1))
        why = None if fits else f"built a block whose cameras miss the GPS of the photos by {residual:.0f} m"
    info["global_check"] = {"after": "global_positioning", "fits": why is None,
                            "residual_m": None if np.isnan(residual) else float(residual)}
    if why:
        raise NativeMapperRejected(why)
    with stages.stage("bundle_adjustment"):
        report["refine"] = _plain(mapper.refine(relay))
    with stages.stage("write"):
        relay.progress(0.95, PROGRESS_WRITE)
        folder = Path(out) / "0"
        folder.mkdir(parents=True, exist_ok=True)
        mapper.write_model(str(folder))
        rec = pycolmap.Reconstruction(str(folder))
        rec.extract_colors_for_all_images(str(root))
    done = _plain(mapper.report())
    why = sfm._global_check(chunk, {0: rec}, n, record=False)
    if why is None and done["median_reprojection_px"] > MAX_MEDIAN_REPROJECTION_PX:
        why = f"median reprojection error {done['median_reprojection_px']:.2f} px"
    if why is None and done["tracks_kept_share"] < MIN_KEPT_SHARE:
        why = f"kept {100.0 * done['tracks_kept_share']:.0f} % of the observations"
    if why:
        raise NativeMapperRejected(why)
    relay.progress(1.0, "")
    return {0: rec}
