"""The child of the bench: the steps of one run, in a process of its own.

    python -m pyarchinit_photogrammetry.bench.steps <job.json>

The job names the local photos, the work folder, the chain (``align``, ``all``, ``match`` or ``optimize``,
:data:`CHAINS`), the
mapper of the alignment (one of :data:`MAPPERS`), the engine of the matching and the engine of the alignment (each
one of :data:`ENGINES`; the depth maps run classic whatever the job asks) and, when the set has them,
the targets to detect, the reference to import and the labels of its markers that are check points (``check``), and
the steps after which the project is kept as it was (``after``: :func:`snapshot_hooks`); for the real sets the kept
run whose matches it reuses (``matches_from``: :func:`reuse_matches`), the project whose markers it copies before the
alignment (``markers``: :func:`copy_markers`) and the formulation and solver of the optimization (``optimize``,
``solver``). The child makes a project in the work folder (or copies the kept run's), adds the photos and runs public
methods of :class:`..core.chunk.Chunk` only, in the order
``ppg process`` runs them (:data:`ALL_STEPS`; a test keeps the two equal) with the defaults of today written out
(:data:`ARGUMENTS`), so that a record says what ran. It rewrites ``result.json`` when a step starts and when it ends,
so that a child that dies leaves the step it died in: the seconds of each step as the child measures them around it,
the higher of its resident memory at the start and at the end (the parent's samples can only raise it), the stages and
the engine the chunk recorded, the counts of the photo cache when the step has them; a step that fails is listed with
its seconds up to the failure and its error, and the steps after it are not run. The settings of the engine and the
results (numbers only) follow the steps.
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np

from .. import _native
from ..core.document import Document
from ..engine import switch
from . import pairs, record

# the steps of ``align`` and of ``all``: the methods of Chunk that ppg process calls, in its order; detect_markers,
# import_reference and update_transform run when the job, or the chunk, gives them something to do
ALIGN_STEPS = ("detect_markers", "match_photos", "align_cameras", "import_reference", "update_transform")
# the steps of ``match``: the matching alone (the markers before it when the job names targets), for the time of the
# native matching against the classic one on a large set without a run of the mapper
MATCH_STEPS = ALIGN_STEPS[:2]
# the engines ``ppg bench run`` chooses from, for the matching (--engine) and for the alignment (--align-engine), the
# first the default of both: those of the switch (engine/switch.py), whose default is auto. The job carries the two
# choices apart, ``engine`` to match_photos alone and ``align_engine`` to align_cameras alone; the record keeps them
# as settings["engine_requested"] and settings["align_engine_requested"], while the engine of each step says what ran
ENGINES = switch.ENGINES
# the targets ``all`` detects when the job names none: the default of ppg process, which is that of
# Chunk.detect_markers (a test compares the three)
DEFAULT_TARGETS = "aruco_4x4_50"
# the mappers of the alignment ``ppg bench run`` chooses from (--mapper), the first its default: that of
# Chunk.align_cameras, which ppg process aligns with, and of ppg align (a test compares them); auto picks one for the
# set. The job carries the choice to align_cameras; the record keeps it as settings["mapper_requested"] beside the
# mapper the run used
MAPPERS = ("auto", "incremental", "global")
# os.replace of the result is tried again on a PermissionError, with short sleeps: on Windows the parent, which
# reads the file every half second, holds it for a moment (a few tries cover that moment; an estimate)
_REPLACE_TRIES = 5
ALL_STEPS = ALIGN_STEPS + ("build_depth_maps", "build_point_cloud", "build_model", "build_texture", "build_dem",
                           "build_orthomosaic")
# the steps of ``optimize``: the chain of align and the optimization of the cameras after it (the weak ring measures
# the control and check errors after it, ``optimized_control_rmse`` and ``optimized_check_rmse``)
OPTIMIZE_STEPS = ALIGN_STEPS + ("optimize_cameras",)
CHAINS = {"align": ALIGN_STEPS, "all": ALL_STEPS, "match": MATCH_STEPS, "optimize": OPTIMIZE_STEPS}
# the arguments of the steps beside what the job gives (the targets, the reference, the mapper, the engine): the
# defaults of ppg process and of the methods of Chunk today, written out so that a record says what ran even after a
# default changes (a test compares them with the signatures); optimize_cameras with the defaults of ppg optimize
ARGUMENTS = {"match_photos": {"quality": "high", "keypoint_limit": 40000, "tiepoint_limit": 8000,
                              "pair_selection": "auto"},
             "align_cameras": {}, "update_transform": {}, "build_depth_maps": {"quality": "medium"},
             "build_point_cloud": {}, "build_model": {"surface": "arbitrary"}, "build_texture": {}, "build_dem": {},
             "build_orthomosaic": {}, "optimize_cameras": {}}
# the keys of chunk.meta["sfm"] a record keeps as settings: never image_root or names, nor the report of the native
# mapper (native_mapper: its positioning names a CRS and an offset). "native_fallback" is the reason the native
# mapper handed the alignment to the classic chain (a block it refused, a failure of it; None when it aligned or was
# not asked), beside the mapper that aligned and that mapper's own fallback. "engine", "features" and
# "routes" are the native matching's (its record names the engine, where the features were found and the routes the
# pairs took; the classic record names no engine and no routes): facts of the run, as the fallbacks are, not
# settings, for the table (table.RUN_FACTS). The last five are the native matching's settings read from the
# environment (the JPEG decoded at a DCT scale, and the second stage: on, its ratio test, its band, its check against
# the first stage's rivals), None in a classic record: settings, so runs that differ in them are not one measurement
SFM_SETTINGS = ("pairs", "matcher", "quality", "gpu", "gpu_pairs", "cpu_pairs", "mapper", "global_fallback",
                "native_fallback", "engine", "features", "routes", "native_dct", "native_guided",
                "native_guided_ratio", "native_guided_band", "native_guided_check")
MATCH_SETTINGS = ("max_ratio", "max_distance", "cross_check")
PROJECT = "project.ppg"
# the project as it was right after a step the job names in ``after`` (:func:`snapshot_hooks`), in the work folder
SNAPSHOT = "project.after-{step}.ppg"
RESULT = "result.json"
CHUNK_LABEL = "Chunk 1"
# the words of --optimize (the formulation of optimize_cameras in the chain ``optimize``), the first its default: the
# published optimization (engine.sfm.OPTIMIZE_AUTO_ENGINE; a test compares them); its solver the program's (auto)
OPTIMIZE_ENGINES = ("colmap", "ppg")
# the step a run on the matches of a kept run starts at (``matches_from`` of the job): the steps before it are not run
REUSED_FROM = "align_cameras"
# the database of the matching, and the native stores beside it, in the sfm folder of a chunk
DATABASE = pairs.DATABASE
NATIVE_STORES = "native"


def match_settings() -> dict:
    """``max_ratio``, ``max_distance`` and ``cross_check`` of the matcher, read from the defaults of the native
    matcher's signature (:func:`.._native.gpu_match`), never written out: a record says what the program used."""
    params = inspect.signature(_native.gpu_match).parameters
    return {key: params[key].default for key in MATCH_SETTINGS}


def reprojection_rms(model) -> float:
    """RMS of the reprojection errors of all the observations of the tie points of the reconstruction at ``model``,
    in pixels of the photos (the measure of the Metashape script of ``benchmark/``)."""
    import pycolmap

    rec = pycolmap.Reconstruction(str(model))
    xyz = {pid: p.xyz for pid, p in rec.points3D.items()}
    squares, count = 0.0, 0
    for image in rec.images.values():
        seen = [(p.xy, xyz[p.point3D_id]) for p in image.points2D if p.has_point3D()]
        if not seen:
            continue
        pose = image.cam_from_world()
        cam = (pose.rotation.matrix() @ np.array([s[1] for s in seen]).T).T + pose.translation
        err = np.array(rec.cameras[image.camera_id].img_from_cam(cam)) - np.array([s[0] for s in seen])
        squares += float((err ** 2).sum())
        count += len(seen)
    return (squares / count) ** 0.5


def settings_of(chunk) -> dict:
    """The settings of the engine the record keeps: :data:`SFM_SETTINGS` of ``chunk.meta["sfm"]`` (None for what is
    not there yet) and the version of pycolmap."""
    sfm = chunk.meta.get("sfm")
    sfm = sfm if isinstance(sfm, dict) else {}
    settings = {key: sfm.get(key) for key in SFM_SETTINGS}
    settings["pycolmap"] = _pycolmap_version()
    return settings


def _pycolmap_version() -> str | None:
    try:
        import pycolmap
    except ImportError:  # pragma: no cover - the alignment needs it
        return None
    version = getattr(pycolmap, "__version__", None)
    return str(version) if version else None


def tie_points_per_photo(model) -> float:
    """The tie points a registered photo of the reconstruction at ``model`` sees, on average: the 2D points of each
    registered image that have a 3D point, averaged over the registered images (the measure the tests of the
    native matching hold against the classic one); 0.0 when no image is registered."""
    import pycolmap

    rec = pycolmap.Reconstruction(str(model))
    counts = [image.num_points3D for image in rec.images.values() if image.has_pose]
    return sum(counts) / len(counts) if counts else 0.0


def matching_results(chunk) -> dict:
    """``verified_pairs`` after the matching: the pairs of photos whose two-view geometry the mapper takes
    (:func:`.pairs.verified`), counted in the database of the chunk (``database.db`` in its sfm folder, read-only);
    nothing when the database is not there."""
    db = chunk.data_dir / "sfm" / pairs.DATABASE
    return {"verified_pairs": len(pairs.verified(db))} if db.is_file() else {}


def alignment_results(chunk) -> dict:
    """``cameras``, ``aligned`` and, when the reconstruction is there, ``tie_points_per_photo`` (to a tenth) and
    ``reprojection_rms_px`` after the alignment."""
    results = {"cameras": len(chunk.cameras), "aligned": len(chunk.aligned_cameras)}
    model = chunk.product_path("reconstruction")
    if model is not None and model.exists():
        results["tie_points_per_photo"] = round(tie_points_per_photo(model), 1)
        results["reprojection_rms_px"] = round(reprojection_rms(model), 3)
    return results


def georeferencing_results(chunk, report: dict) -> dict:
    """Numbers of the report of ``update_transform()``: ``control_rmse`` and ``check_rmse`` (each left out when the
    report has None), ``camera_rmse`` (the RMS of the errors of the camera references that have one; left out when
    none has) and ``markers`` (how many markers have an error). Never the CRS, a label or the markers set aside."""
    from ..markers import gcp

    results = {}
    for kind in ("control", "check"):
        value = report.get(f"{kind}_rmse")
        if value is not None:
            results[f"{kind}_rmse"] = float(value)
    errors = [e["error"] for e in gcp.camera_reference_errors(chunk).values() if e.get("error") is not None]
    if errors:
        results["camera_rmse"] = float(np.sqrt(np.mean(np.square(errors))))
    results["markers"] = sum(1 for m in report.get("markers") or [] if m.get("error") is not None)
    return results


def optimization_results(chunk) -> dict:
    """``optimized_control_rmse`` and ``optimized_check_rmse`` after ``optimize_cameras``: the errors of the control
    and check markers the chunk reports then (:func:`..markers.gcp.marker_errors`), as
    :func:`georeferencing_results` reads them from the report of ``update_transform``; each left out when no marker
    of its kind has an error."""
    from ..markers import gcp

    rows = gcp.marker_errors(chunk)
    results = {}
    for kind in ("control", "check"):
        errors = [r["error"] for r in rows if r["type"] == kind and r["error"] is not None]
        if errors:
            results[f"optimized_{kind}_rmse"] = float(np.sqrt(np.mean(np.square(errors))))
    return results


def set_check(chunk, labels) -> None:
    """The markers of ``labels`` made check points (the others keep their flag): each found by its label, else by
    its code, as the reference file names it (:func:`..markers.gcp.import_reference`). A ValueError when one is not
    in the chunk: the run would measure without that check point."""
    from ..markers import gcp

    for label in labels or ():
        marker = gcp._find_marker(chunk, str(label))
        if marker is None:
            raise ValueError(f"no marker {label} for the check points")
        marker.checkpoint = True


def numbers(values, source: str) -> dict:
    """``values`` (a dict a hook gave) when every value is a number, else a TypeError naming the key and
    ``source``."""
    values = dict(values)
    for key, value in values.items():
        if not isinstance(value, (int, float)):
            raise TypeError(f"{source} gave {key!r} that is not a number")
    return values


def snapshot_hooks(work: Path, names) -> dict:
    """The hooks of the child for the steps ``names`` a job lists in ``after``: each keeps the saved project as it is
    right after its step (:data:`SNAPSHOT` in ``work``) and adds nothing to the results. The parent's own hooks
    (:func:`.runner.run`, ``after``: never written to the job) run on those projects when the run ends."""
    def keep(step: str, doc) -> dict:
        shutil.copyfile(work / PROJECT, work / SNAPSHOT.format(step=step))
        return {}

    return {str(step): functools.partial(keep, str(step)) for step in names or ()}


def kept_run(folder: Path) -> tuple[Path, Path]:
    """The project file and the database of the matching of the kept run ``folder`` (a work folder kept by
    ``--keep``: exactly one :data:`PROJECT` under it, with one chunk, whose sfm folder holds :data:`DATABASE`).
    Refused with a ValueError that names what is missing, never the path. Nothing is written: the project file is
    read as text."""
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"--matches-from is not a folder: give the kept work folder of a run (OUT/<name>-<i>, "
                         f"--keep)")
    found = sorted(p for p in folder.rglob(PROJECT) if p.is_file())
    if len(found) != 1:
        raise ValueError(f"--matches-from holds {len(found)} {PROJECT} files: exactly one is needed, the project of "
                         f"a run kept with --keep")
    project = found[0]
    try:
        chunks = json.loads(project.read_text(encoding="utf-8"))["chunks"]
        keys = [int(c["key"]) for c in chunks]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"--matches-from: its {PROJECT} cannot be read ({type(exc).__name__})") from None
    if len(keys) != 1:
        raise ValueError(f"--matches-from: its {PROJECT} has {len(keys)} chunks, a run of the bench has one")
    database = project.with_name(project.name + ".files") / f"chunk_{keys[0]}" / "sfm" / DATABASE
    if not database.is_file():
        raise ValueError(f"--matches-from: its project has no {DATABASE} (the matching of the run): it was not "
                         f"matched, or its files were not kept")
    return project, database


def kept_photos(project: Path) -> set[str]:
    """The file names of the photos of the one chunk of the kept project ``project`` (read as text)."""
    (chunk,) = json.loads(Path(project).read_text(encoding="utf-8"))["chunks"]
    return {Path(c.get("path_abs") or c["path"]).name for c in chunk.get("cameras") or []}


def reuse_matches(kept: Path, work: Path, photos) -> object:
    """The project of the kept run ``kept`` (:func:`kept_run`) copied into ``work`` as :data:`PROJECT` with its
    database and its native stores (when they exist; the copies keep the times of the files), opened, its alignment
    and every product dropped, saved: its chunk, ready for ``align_cameras``. The kept folder is only read. A
    ValueError when the photos of the kept project are not ``photos``, the photos of this run."""
    project, database = kept_run(kept)
    shutil.copy2(project, work / PROJECT)
    doc = Document.open(work / PROJECT)
    chunk = doc.chunks[0]
    asked = {Path(p).resolve() for p in photos}
    if {Path(c.path).resolve() for c in chunk.cameras} != asked:
        raise ValueError("the photos of the kept run are not the photos of this run")
    sfm_dir = chunk.data_dir / "sfm"
    sfm_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(database, sfm_dir / DATABASE)
    if (database.parent / NATIVE_STORES).is_dir():
        shutil.copytree(database.parent / NATIVE_STORES, sfm_dir / NATIVE_STORES)
    chunk.reset_alignment()
    chunk.forget_precision()
    chunk.products.clear()  # their files were not copied: what the steps after the matching made, made again
    info = chunk.meta.get("sfm")
    if isinstance(info, dict):
        info.pop("optimize", None)
    for key in ("timings", "engine"):  # the records of the steps of the kept run: the matching's alone stays
        records = chunk.meta.get(key)
        if isinstance(records, dict):
            chunk.meta[key] = {k: v for k, v in records.items() if k == "match_photos"}
    doc.save()
    return chunk


def markers_chunk(doc) -> object:
    """The chunk of the project ``doc`` the markers are taken from: its active chunk, refused (ValueError) when no
    marker of it is measured on a photo."""
    chunk = doc.chunk
    if chunk is None or not any(m.projections for m in chunk.markers):
        raise ValueError("--markers: the active chunk of the project has no marker measured on a photo")
    return chunk


def copy_markers(chunk, project: Path) -> tuple[dict, int]:
    """Every marker of the chunk of the project ``project`` (:func:`markers_chunk`; opened, never saved) copied into
    ``chunk``: its label, code, reference (location, accuracy, whether it is used), check flag and its projections
    by the **label** of their camera; a marker of ``chunk`` with the same label is replaced. The CRS of the markers
    becomes the chunk's when the chunk has a local or geographic one, else their locations are transformed into
    it. Returned: ``{"markers": n, "checks": n}`` and how many projections were left out, on photos this run does
    not have."""
    from ..core import crs as crs_mod
    from ..core.camera import Reference
    from ..core.marker import Marker, Projection

    source = markers_chunk(Document.open(project))
    labels = {c.key: c.label for c in source.cameras}
    cameras = {c.label: c for c in chunk.cameras}
    transform = None
    if not crs_mod.is_local(source.crs) and source.crs != chunk.crs:
        if crs_mod.is_local(chunk.crs) or crs_mod.is_geographic(chunk.crs):
            chunk.crs = source.crs
            if source.meta.get("marker_frames"):
                chunk.meta["marker_frames"] = dict(source.meta["marker_frames"])
        else:
            transform = (source.crs, chunk.crs)
    left_out = 0
    for m in source.markers:
        chunk.markers = [old for old in chunk.markers if old.label != m.label]
        new = Marker(key=chunk._next_key(chunk.markers), label=m.label, code=m.code, target_type=m.target_type,
                     reference=Reference.from_dict(m.reference.to_dict()), checkpoint=bool(m.checkpoint))
        if transform is not None and new.reference.location is not None:
            xyz = np.asarray(new.reference.location, float)[None]
            new.reference.location = [float(v) for v in crs_mod.transform_points(xyz, *transform)[0]]
        for key, proj in m.projections.items():
            camera = cameras.get(labels.get(key))
            if camera is None:
                left_out += 1
                continue
            new.projections[camera.key] = Projection(proj.x, proj.y, proj.pinned)
        chunk.markers.append(new)
    chunk.document.save()
    return {"markers": len(source.markers), "checks": sum(1 for m in source.markers if m.checkpoint)}, left_out


def _rss_mb() -> float:
    """The resident memory of this process, in MiB (written ``mb``, as the memory of the machine is ``gb``)."""
    import psutil

    return psutil.Process().memory_info().rss / 2**20


def _timing_of(chunk, step: str) -> dict:
    timings = chunk.meta.get("timings")
    timing = timings.get(step) if isinstance(timings, dict) else None
    return timing if isinstance(timing, dict) else {}


class Result:
    """What the child writes for the parent in ``result.json``, rewritten whole (and atomically) at every change:
    ``steps`` (the entries described in the module), ``settings``, ``results``, ``error`` (of the step that failed, or
    of the setup) and ``done`` (the chain ran to its end)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = {"steps": [], "settings": {}, "results": {}, "error": None, "done": False}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def start(self, step: str) -> tuple[dict, float]:
        """The entry of ``step``, written as it starts; with it the clock its seconds are measured from."""
        entry = {"step": step, "start": time.time(), "end": None, "seconds": None, "peak_memory_mb": _rss_mb(),
                 "stages": {}, "engine": None}
        self.data["steps"].append(entry)
        self.save()
        return entry, time.perf_counter()

    def end(self, entry: dict, clock: float, chunk, error: BaseException | None = None) -> None:
        """``entry`` completed: its seconds, its peak, the stages, the engine and the photo cache of the chunk's own
        record of the step, and the error when the step raised (also the error of the whole result)."""
        step = entry["step"]
        entry["end"] = time.time()
        entry["seconds"] = round(time.perf_counter() - clock, 3)
        entry["peak_memory_mb"] = round(max(entry["peak_memory_mb"], _rss_mb()), 1)
        timing = _timing_of(chunk, step)
        entry["stages"] = timing["stages"] if isinstance(timing.get("stages"), dict) else {}
        engines = chunk.meta.get("engine")
        entry["engine"] = engines.get(step) if isinstance(engines, dict) else None
        if "photo_cache" in timing:
            entry["photo_cache"] = timing["photo_cache"]
        if error is not None:
            self.failed(entry, error)
        else:
            self.save()

    def failed(self, entry: dict, error: BaseException) -> None:
        """``entry`` and the whole result marked failed by ``error`` (:func:`.record.describe`: the type and the
        message, never the file name of an OSError; the parent scrubs the text)."""
        entry["error"] = self.data["error"] = f"{entry['step']}: {record.describe(error)}"
        self.save()

    def note(self, step: str, chunk, value) -> None:
        """The settings and the results after ``step``, which returned ``value``."""
        self.data["settings"].update(settings_of(chunk))
        if step == "match_photos":
            self.data["results"].update(matching_results(chunk))
        elif step == "align_cameras":
            self.data["results"].update(alignment_results(chunk))
        elif step == "update_transform":
            self.data["results"].update(georeferencing_results(chunk, value))
            self.data["settings"]["georeferencing"] = value.get("method")
        elif step == "optimize_cameras":
            self.data["results"].update(optimization_results(chunk))
        self.save()


def wanted(step: str, job: dict, chunk, imported: bool) -> bool:
    """Whether ``step`` runs: the markers when the job names targets, the reference when it gives one, the transform
    when a reference was imported, a camera has a reference position or the chunk has scale bars (as ppg process
    decides); the steps before :data:`REUSED_FROM` never on the matches of a kept run (``matches_from``); every other
    step always."""
    if step == "detect_markers":
        return bool(job.get("targets"))
    if step == "import_reference":
        return bool(job.get("reference"))
    if step == "update_transform":
        return imported or any(c.reference.location for c in chunk.cameras) or bool(chunk.scalebars)
    if job.get("matches_from") and step in ALIGN_STEPS[:ALIGN_STEPS.index(REUSED_FROM)]:
        return False  # the markers and the matching of the kept run are the project's already
    return True


def call(chunk, step: str, job: dict):
    """``step`` run on ``chunk``: the targets, the reference, the mapper (the first of :data:`MAPPERS` when the job
    names none), the engine of the matching (``engine``) and the engine of the alignment (``align_engine``; each the
    first of :data:`ENGINES` when the job names none) from the job, the rest with :data:`ARGUMENTS`. The depth maps,
    the other step that takes an engine, run classic by the bench's own word, and what the computer asks for
    (``PPG_ENGINE``, which the runner keeps out of the child anyway) never changes the engine of a step."""
    if step == "detect_markers":
        return chunk.detect_markers(job["targets"])
    if step == "import_reference":
        return chunk.import_reference(job["reference"], crs=job.get("crs"))
    kwargs = dict(ARGUMENTS[step])
    if step == "match_photos":
        kwargs["engine"] = job.get("engine", ENGINES[0])
    elif step == "align_cameras":
        kwargs["engine"] = job.get("align_engine", ENGINES[0])
    elif step in switch.STEPS:
        kwargs["engine"] = "classic"
    elif step == "optimize_cameras":
        kwargs["engine"] = job.get("optimize") or OPTIMIZE_ENGINES[0]
        kwargs["solver"] = job.get("solver") or "auto"
    if step == "align_cameras":
        kwargs["mapper"] = job.get("mapper", MAPPERS[0])
    return getattr(chunk, step)(**kwargs)


def run_chain(chunk, job: dict, result: Result) -> bool:
    """The chain of the job on ``chunk``, each step written to ``result`` as it starts and ends and the project saved
    after it; False at the first step that raises (its error in the result), True when the chain ran to its end.
    Right after ``import_reference`` the markers of ``job["check"]`` become check points (:func:`set_check`; a
    label the chunk lacks is an error of that step). ``job["after"]`` maps a step to a callable that gets the
    document once the step is noted and saved and returns numbers for the results (in-process; in the child the
    hooks of :func:`snapshot_hooks`); what goes wrong in it is an error of the step. Right before ``align_cameras``
    the markers of the project ``job["markers"]`` are copied into the chunk (:func:`copy_markers`): their counts go to
    the settings, the projections left out to the results, and the georeferencing then runs as after a reference; a
    project that cannot be read is the error ``markers: ...`` of the run."""
    imported = False
    hooks = job.get("after") or {}
    for step in CHAINS[job["steps"]]:
        if step == "align_cameras" and job.get("markers"):
            try:
                counts, left_out = copy_markers(chunk, Path(job["markers"]))
            except Exception as exc:  # a project that cannot be read: the run measures nothing without its markers
                result.data["error"] = f"markers: {record.describe(exc)}"
                result.save()
                return False
            result.data["settings"].update(counts)
            result.data["results"]["markers_projections_left_out"] = left_out
            result.save()
            imported = True  # markers with references: the chunk is georeferenced on them, as after a reference
        if not wanted(step, job, chunk, imported):
            continue
        entry, clock = result.start(step)
        try:
            value = call(chunk, step, job)
            if step == "import_reference":
                set_check(chunk, job.get("check"))
        except BaseException as exc:
            result.end(entry, clock, chunk, error=exc)
            return False
        result.end(entry, clock, chunk)
        imported = imported or step == "import_reference"
        try:
            result.note(step, chunk, value)
            chunk.document.save()
            if step in hooks:
                result.data["results"].update(numbers(hooks[step](chunk.document), f"the hook after {step}"))
                result.save()
        except Exception as exc:  # the step is done, its record is not: said like an error of the step
            result.failed(entry, exc)
            return False
    result.data["done"] = True
    result.save()
    return True


def main(argv: list[str] | None = None) -> int:
    """The child: the job of ``argv[0]`` run, ``result.json`` in its work folder; 0 when the chain ran to its end, 1
    when the job cannot be read, a step or the setup failed, 2 without a job. A job file that cannot be read or
    parsed, or that names no work folder, leaves ``result.json`` beside it, with the error (no traceback)."""
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        print("usage: python -m pyarchinit_photogrammetry.bench.steps <job.json>", file=sys.stderr)
        return 2
    job_file = Path(args[0])
    try:
        job = json.loads(job_file.read_text(encoding="utf-8"))
        work = Path(job["work"])
    except (OSError, ValueError, KeyError, TypeError) as exc:  # missing or unreadable, not JSON, not a job
        result = Result(job_file.parent / RESULT)  # beside the job, where the parent wrote it: its work folder
        result.data["error"] = f"job: {record.describe(exc)}"
        result.save()
        return 1
    result = Result(work / RESULT)
    job["after"] = snapshot_hooks(work, job.get("after"))  # names of steps in the job: the hooks of the child
    if job.get("matches_from"):
        try:
            chunk = reuse_matches(Path(job["matches_from"]), work, job["photos"])
        except Exception as exc:
            result.data["error"] = f"matches_from: {record.describe(exc)}"
            result.save()
            return 1
        return 0 if run_chain(chunk, job, result) else 1
    try:
        doc = Document()
        chunk = doc.add_chunk(CHUNK_LABEL)
        doc.save(work / PROJECT)
        chunk.add_photos([Path(p) for p in job["photos"]])
    except Exception as exc:
        result.data["error"] = f"add_photos: {record.describe(exc)}"
        result.save()
        return 1
    return 0 if run_chain(chunk, job, result) else 1


if __name__ == "__main__":
    sys.exit(main())
