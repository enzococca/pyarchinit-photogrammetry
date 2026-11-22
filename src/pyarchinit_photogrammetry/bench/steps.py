"""The child of the bench: the steps of one run, in a process of its own.

    python -m pyarchinit_photogrammetry.bench.steps <job.json>

The job names the local photos, the work folder, the chain (``align``, ``all`` or ``match``, :data:`CHAINS`), the
mapper of the alignment (one of :data:`MAPPERS`), the engine of the matching (one of :data:`ENGINES`; the alignment
and the depth maps run classic whatever the job asks: the bench measures the matching) and, when the set has them,
the targets to detect and the reference to import. The child makes a project in the work folder, adds the photos and
runs public methods of :class:`..core.chunk.Chunk` only, in the order ``ppg process`` runs them (:data:`ALL_STEPS`; a
test keeps the two equal) with the defaults of today written out (:data:`ARGUMENTS`), so that a record says what
ran. It rewrites ``result.json`` when a step starts and when it ends,
so that a child that dies leaves the step it died in: the seconds of each step as the child measures them around it,
the higher of its resident memory at the start and at the end (the parent's samples can only raise it), the stages and
the engine the chunk recorded, the counts of the photo cache when the step has them; a step that fails is listed with
its seconds up to the failure and its error, and the steps after it are not run. The settings of the engine and the
results (numbers only) follow the steps.
"""
from __future__ import annotations

import inspect
import json
import os
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
# the engines of the matching ``ppg bench run`` chooses from (--engine), the first its default: those of the switch
# (engine/switch.py), whose default is auto. The job carries the choice to match_photos alone; the record keeps it as
# settings["engine_requested"], while the engine of the step says what ran
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
CHAINS = {"align": ALIGN_STEPS, "all": ALL_STEPS, "match": MATCH_STEPS}
# the arguments of the steps beside what the job gives (the targets, the reference, the mapper, the engine): the
# defaults of ppg process and of the methods of Chunk today, written out so that a record says what ran even after a
# default changes (a test compares them with the signatures)
ARGUMENTS = {"match_photos": {"quality": "high", "keypoint_limit": 40000, "tiepoint_limit": 8000,
                              "pair_selection": "auto"},
             "align_cameras": {}, "update_transform": {}, "build_depth_maps": {"quality": "medium"},
             "build_point_cloud": {}, "build_model": {"surface": "arbitrary"}, "build_texture": {}, "build_dem": {},
             "build_orthomosaic": {}}
# the keys of chunk.meta["sfm"] a record keeps as settings: never image_root or names. "engine", "features" and
# "routes" are the native matching's (its record names the engine, where the features were found and the routes the
# pairs took; the classic record names no engine and no routes): facts of the run, not settings, for the table
# (table.RUN_FACTS). The last four are the native matching's settings read from the environment (the JPEG decoded at
# a DCT scale, and the second stage: on, its ratio test, its band), None in a classic record: settings, so runs that
# differ in them are not one measurement
SFM_SETTINGS = ("pairs", "matcher", "quality", "gpu", "gpu_pairs", "cpu_pairs", "mapper", "global_fallback", "engine",
                "features", "routes", "native_dct", "native_guided", "native_guided_ratio", "native_guided_band")
MATCH_SETTINGS = ("max_ratio", "max_distance", "cross_check")
PROJECT = "project.ppg"
RESULT = "result.json"
CHUNK_LABEL = "Chunk 1"


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
        self.save()


def wanted(step: str, job: dict, chunk, imported: bool) -> bool:
    """Whether ``step`` runs: the markers when the job names targets, the reference when it gives one, the transform
    when a reference was imported, a camera has a reference position or the chunk has scale bars (as ppg process
    decides); every other step always."""
    if step == "detect_markers":
        return bool(job.get("targets"))
    if step == "import_reference":
        return bool(job.get("reference"))
    if step == "update_transform":
        return imported or any(c.reference.location for c in chunk.cameras) or bool(chunk.scalebars)
    return True


def call(chunk, step: str, job: dict):
    """``step`` run on ``chunk``: the targets, the reference, the mapper (the first of :data:`MAPPERS` when the job
    names none) and the engine of the matching (the first of :data:`ENGINES` when the job names none) from the job,
    the rest with :data:`ARGUMENTS`. The two other steps that take an engine, the alignment and the depth maps, run
    classic by the bench's own word: a run measures the matching, and what the computer asks for (``PPG_ENGINE``,
    which the runner keeps out of the child anyway) never changes it."""
    if step == "detect_markers":
        return chunk.detect_markers(job["targets"])
    if step == "import_reference":
        return chunk.import_reference(job["reference"], crs=job.get("crs"))
    kwargs = dict(ARGUMENTS[step])
    if step == "match_photos":
        kwargs["engine"] = job.get("engine", ENGINES[0])
    elif step in switch.STEPS:
        kwargs["engine"] = "classic"
    if step == "align_cameras":
        kwargs["mapper"] = job.get("mapper", MAPPERS[0])
    return getattr(chunk, step)(**kwargs)


def run_chain(chunk, job: dict, result: Result) -> bool:
    """The chain of the job on ``chunk``, each step written to ``result`` as it starts and ends and the project saved
    after it; False at the first step that raises (its error in the result), True when the chain ran to its end."""
    imported = False
    for step in CHAINS[job["steps"]]:
        if not wanted(step, job, chunk, imported):
            continue
        entry, clock = result.start(step)
        try:
            value = call(chunk, step, job)
        except BaseException as exc:
            result.end(entry, clock, chunk, error=exc)
            return False
        result.end(entry, clock, chunk)
        imported = imported or step == "import_reference"
        try:
            result.note(step, chunk, value)
            chunk.document.save()
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
