"""A run of the bench: the steps of a set of photos in a child process, its memory sampled, one record per run.

:func:`run` reads the facts of the machine, of the disk that holds the photos and of the photos (:mod:`.machine`),
starts the child (:mod:`.steps`, the same interpreter, with the environment the timing of the stages needs) in a work
folder of its own, samples every :data:`SAMPLE_SECONDS` the memory of the child and of its children, and when the child
ends builds the record (:mod:`.record`), judges it and writes it. A run that fails, dies or is cancelled leaves a record
too, with its error and the steps done; photos that are online-only are left out and make the record not valid. What
a run leaves beside the records - the work folder of a run that failed (kept with the child's log, its result and its
project, for the reason to be read without running again), one it could not remove, or one left by any stop after
the folder was made (a job or a log that cannot be written, a child that cannot start, a record that is refused or
cannot be written, a cancel whose cleanup failed) - is said in the log by the name of the folder, never by its path.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.chunk import IMAGE_EXTENSIONS
from ..core.document import Document
from ..engine.timing import LIVE_LOG_ENV
from ..io import raw as rawio
from . import machine, record
from . import steps as child

log = logging.getLogger(__name__)

# the memory of the child and of its children is sampled every half second: an estimate (a step shorter than that
# keeps the peak the child reads itself at its start and at its end)
SAMPLE_SECONDS = 0.5
PSUTIL_MISSING = 'the bench needs psutil: pip install "pyarchinit-photogrammetry[bench]"'
JOB = "job.json"
LOG = "child.log"


@dataclass(frozen=True)
class _Plan:
    """What every repeat of a run shares."""

    name: str
    steps: str
    mapper: str
    engine: str
    local: tuple[Path, ...]
    out: Path
    records: Path
    keep: bool
    env: dict
    disk: dict
    set: dict
    settings: dict
    private: tuple[Path, ...]
    names: tuple[str, ...]
    extra_results: Callable | None
    targets: str | None
    reference: Path | None
    crs: str | None
    check: tuple[str, ...] = ()
    after: dict | None = None
    align_engine: str = child.ENGINES[0]
    matches_from: Path | None = None
    markers: Path | None = None
    optimize: str | None = None
    solver: str | None = None


def photo_cache_mb(env: dict) -> int | None:
    """The photo cache the child runs with: the value of ``PPG_PHOTO_CACHE_MB`` in ``env`` as a whole number of
    megabytes (as :func:`..engine.ortho.photo_cache_budget` reads it), None when unset or not valid."""
    from ..engine.ortho import PHOTO_CACHE_ENV

    value = env.get(PHOTO_CACHE_ENV)
    if value is None:
        return None
    text = value.strip()
    return int(text) if text.isascii() and text.isdigit() else None


def run(photos: Path, out: Path, name: str, steps: str = "align", repeat: int = 1, keep: bool = False,
        records: Path | None = None, *, mapper: str = "auto", engine: str = "auto", align_engine: str = "auto",
        extra_results: Callable | None = None, targets: str | None = None, reference: Path | None = None,
        crs: str | None = None, seed: int | None = None, check_points=(), after: dict | None = None,
        matches_from: Path | None = None, markers: Path | None = None, optimize: str = "colmap",
        solver: str = "auto") -> list[Path]:
    """``repeat`` runs of the chain ``steps`` (``align``, ``all`` or ``match``, :data:`.steps.CHAINS`) on the photos
    of the folder ``photos``, each in the work folder ``out/<name>-<i>`` (removed after, unless ``keep`` or the run
    failed: then it stays with the child's log, said in the log by its name), the records written to ``records``
    (default ``out``). Returned: the path of each record.

    Refused, in this order and before anything runs: a name that is not one a file can take (ValueError), unknown
    steps, a mapper that is not one of :data:`.steps.MAPPERS`, an engine that is not one of :data:`.steps.ENGINES`
    or a repeat below 1 (ValueError), ``out`` or ``records`` inside ``photos`` (ValueError; by the identity of the
    folders, whatever the case of the letters), ``records`` inside a work folder (ValueError: it is removed after its
    run), a work folder of one of the repeats that exists already (FileExistsError; nothing is deleted in silence),
    psutil missing (RuntimeError naming the extra), a ``seed`` below 0 or a ``PPG_RANSAC_SEED`` in the environment
    that is not a whole number (ValueError, as the child would refuse it), every photo online-only (RuntimeError; no
    child is started). Photos that are online-only are left out of the run and make the record not valid.

    ``seed`` (0 or more) fixes the RANSAC of the geometric verification of the matches in the child, through
    ``PPG_RANSAC_SEED`` (:func:`..engine.sfm.verification_options`): runs with the same seed verify the same pairs, so
    the repeats compare like with like. The record keeps the seed the child runs with as ``settings["ransac_seed"]``:
    ``seed``, else the variable inherited from the environment, else ``-1`` (COLMAP's default, a seed from the clock).

    ``targets``, ``reference`` and ``crs`` go to the child, which detects the markers and imports the reference when
    they are given; ``all`` detects :data:`.steps.DEFAULT_TARGETS` when no targets are given, as ``ppg process`` does,
    ``align`` and ``match`` detect none. ``mapper`` (``auto``, the default, ``incremental`` or ``global``) goes to the
    child, which aligns with it; the record keeps it as ``settings["mapper_requested"]``, while ``settings["mapper"]``
    stays the mapper the run used (the one ``auto`` picked, or the one a global alignment fell back to). ``engine``
    (``auto``, the default, ``classic`` or ``native``) goes to the child in the job, which matches with it and
    nothing else (the alignment takes ``align_engine``, the depth maps run classic: :func:`.steps.call`); the record
    keeps it as ``settings["engine_requested"]``, while the engine of the step says what ran. ``PPG_ENGINE`` of this
    process's environment is not passed on to the child: the engines of a run are the job's. ``extra_results``,
    called with the finished project (a :class:`..core.document.Document`) after the last step of a run that did not
    fail, returns a dict of numbers merged into the results; an exception in it, or a value that is not a number, is
    kept as ``results["error"]`` and makes the record not valid, the times stay. ``check_points``: the labels (or
    codes, as the reference names them) of the markers the child makes check points right after
    ``import_reference``, in the job under ``check``. ``after``: ``{step: callable}``, hooks of this process, never
    written to the job: the job names their steps, the child keeps the project as it was right after each
    (:func:`.steps.snapshot_hooks`) and, when the run did not fail, each hook gets that project (a Document) and
    returns numbers merged into the results before those of ``extra_results``, with the same rules. A
    KeyboardInterrupt while the child runs terminates it and its children, writes the record with the error
    ``cancelled``, removes the work folder unless ``keep`` and is raised again.

    ``align_engine`` (``auto``, the default, ``classic`` or ``native``; a word that is not one of them is refused as
    one of ``engine`` is, right after it) goes to the child in the job, which aligns with it and nothing else. The
    record keeps it as ``settings["align_engine_requested"]``; the engine of the step ``align_cameras`` says what ran
    (the native mapper, or the classic alignment with the reason) and ``settings["native_fallback"]`` why the native
    mapper handed its block to the classic chain, when it did.

    The options of the real sets (refused by :func:`check` as it says): ``matches_from``, the work folder of a run
    kept by ``keep``: each run copies its project, its database and its native stores into its own work folder,
    drops the alignment and every product and starts the chain at ``align_cameras`` (:func:`.steps.reuse_matches`;
    the kept folder is only read); the record has ``settings["matches_reused"]`` True and no ``match_photos``.
    ``markers``, a project (``.ppg``, opened and never saved) whose active chunk has markers measured on the photos:
    right before ``align_cameras`` the child copies every marker into its chunk (:func:`.steps.copy_markers`), the
    record keeps how many as ``settings["markers"]`` and ``settings["checks"]`` and the projections left out, on
    photos the run does not have, as ``results["markers_projections_left_out"]``; never a label. ``optimize``
    (``colmap``, the default, or ``ppg``) and ``solver`` (``auto``, the default, ``native`` or ``scipy``; ``ppg``
    only) are the ``engine`` and the ``solver`` of ``optimize_cameras`` in the chain ``optimize``, kept as
    ``settings["optimize_engine"]`` and ``settings["optimize_solver"]`` (None in a chain without it).
    ``PPG_NATIVE_TRACKS_PER_PHOTO`` and ``PPG_NATIVE_FINAL_ALL_TRACKS`` of this process's environment reach the
    child with the rest of it and are kept as ``settings["native_tracks_per_photo"]`` (a whole number, else a
    ValueError before anything runs) and ``settings["native_final_all_tracks"]`` (False for ``0``, as the mapper
    reads it), None when unset."""
    from ..engine import native_map, sfm

    photos = Path(photos).resolve()
    out, records = check(out, name, steps, repeat, records, photos=photos, mapper=mapper, engine=engine,
                         align_engine=align_engine, matches_from=matches_from, markers=markers, optimize=optimize,
                         solver=solver)
    if seed is not None and int(seed) < 0:
        raise ValueError("seed must be 0 or more")
    # the two variables of the native mapper as it reads them: blank is unset
    tracks, final_all = ((os.environ.get(var) or "").strip() or None
                         for var in (native_map.TRACKS_ENV, native_map.FINAL_ALL_TRACKS_ENV))
    if tracks is not None and not (tracks.isascii() and tracks.isdigit()):
        raise ValueError(f"{native_map.TRACKS_ENV} must be a whole number, not {tracks!r}")
    env = dict(os.environ)
    env.pop("PPG_ENGINE", None)  # the engines of a run are the job's, never the one the computer asks for
    env.update(LIVE_LOG_ENV)
    if seed is not None:
        env[sfm.RANSAC_SEED_ENV] = str(int(seed))
    verification_seed = sfm.ransac_seed(env)  # the seed the child verifies with; a text that is not one is refused here
    files = rawio.photo_files([photos], IMAGE_EXTENSIONS)[0]
    local = tuple(p for p in files if not machine.is_online_only(p))
    if not local:
        raise RuntimeError("every photo is online-only: make them available offline first or copy them to a local disk")
    set_ = machine.photo_set_facts(files)
    disk = machine.disk_facts(photos)
    disk["read_mb_s"] = machine.read_rate(list(local))
    if targets is None and steps == "all":
        targets = child.DEFAULT_TARGETS
    reference = Path(reference).resolve() if reference else None
    settings = {**{key: None for key in child.SFM_SETTINGS}, **child.match_settings(), "pycolmap": None,
                "photo_cache_mb": photo_cache_mb(env), "georeferencing": None, "targets": targets,
                "mapper_requested": mapper, "engine_requested": engine, "align_engine_requested": align_engine,
                "ransac_seed": verification_seed, "matches_reused": matches_from is not None, "markers": None,
                "checks": None, "optimize_engine": optimize if steps == "optimize" else None,
                "optimize_solver": solver if steps == "optimize" else None,
                "native_tracks_per_photo": int(tracks) if tracks is not None else None,
                "native_final_all_tracks": final_all != "0" if final_all is not None else None}
    if markers is not None:  # the counts of the project, said by the record even when the run stops before them
        settings["markers"], settings["checks"] = markers_project(markers)
    matches_from = Path(matches_from).resolve() if matches_from is not None else None
    markers = Path(markers).resolve() if markers is not None else None
    private = (photos, out, Path.home()) + tuple(p for p in (reference, matches_from, markers) if p is not None)
    plan = _Plan(name=name, steps=steps, mapper=mapper, engine=engine, local=local, out=out, records=records,
                 keep=keep, env=env, disk=disk, set=set_, settings=settings, private=private,
                 names=tuple(p.name for p in files), extra_results=extra_results, targets=targets,
                 reference=reference, crs=crs, check=tuple(str(c) for c in check_points or ()),
                 after=dict(after or {}), align_engine=align_engine, matches_from=matches_from, markers=markers,
                 optimize=optimize if steps == "optimize" else None, solver=solver if steps == "optimize" else None)
    return [_one_run(plan, index) for index in range(1, int(repeat) + 1)]


def check(out: Path, name: str, steps: str = "align", repeat: int = 1, records: Path | None = None, *,
          photos: Path | None = None, mapper: str = "auto", engine: str = "auto",
          align_engine: str = "auto", matches_from: Path | None = None, markers: Path | None = None,
          optimize: str = "colmap", solver: str = "auto") -> tuple[Path, Path]:
    """What :func:`run` refuses before anything runs, in its order, for a caller that must know before it makes its
    set (``ppg bench synthetic`` renders its photos after this): a name that is not one a file can take
    (ValueError), unknown steps, a mapper that is not one of :data:`.steps.MAPPERS`, an engine that is not one of
    :data:`.steps.ENGINES` or a repeat below 1 (ValueError), ``photos`` - when given - that is not a folder or holds
    ``out`` or ``records`` (ValueError), ``records`` inside a work folder (ValueError), a work folder of one of the
    repeats that exists already (FileExistsError), psutil missing (RuntimeError), ``photos`` - when given - without a
    photo (ValueError). Returned: ``out`` and ``records`` resolved (``records`` is ``out`` when not given). The
    engine of the matching (``engine``) is checked first, then that of the alignment (``align_engine``).

    After the engines, the options of the real sets (ValueError each, the text naming the option and never a path):
    ``optimize`` not one of :data:`.steps.OPTIMIZE_ENGINES`, ``solver`` not one of the solvers, or other than
    ``auto`` with ``colmap``, either not the default without the chain ``optimize``; ``matches_from`` or ``markers``
    with the chain ``match``; ``matches_from`` that is not a kept run (:func:`.steps.kept_run`: what is missing), or
    whose photos are not those of ``photos`` (when given); ``markers`` that is not a project whose active chunk has
    a marker measured on a photo."""
    record.check_name(name)
    if steps not in child.CHAINS:
        raise ValueError(f"steps must be {', '.join(list(child.CHAINS)[:-1])} or {list(child.CHAINS)[-1]}, "
                         f"not {steps!r}")
    if mapper not in child.MAPPERS:
        raise ValueError(f"mapper must be {', '.join(child.MAPPERS[:-1])} or {child.MAPPERS[-1]}, not {mapper!r}")
    for word, value in (("engine", engine), ("align_engine", align_engine)):
        if value not in child.ENGINES:
            raise ValueError(f"{word} must be {', '.join(child.ENGINES[:-1])} or {child.ENGINES[-1]}, not {value!r}")
    _real_set_options(steps, matches_from, markers, optimize, solver)
    if int(repeat) < 1:
        raise ValueError("repeat must be 1 or more")
    out = Path(out).resolve()
    records = Path(records).resolve() if records else out
    if photos is not None:
        photos = Path(photos).resolve()
        if not photos.is_dir():
            raise ValueError(f"{photos} is not a folder of photos")
        if _inside(out, photos) or _inside(records, photos):
            raise ValueError("the work folder and the records folder must not be inside the folder of the photos")
    for index in range(1, int(repeat) + 1):
        work = out / f"{name}-{index}"
        if under(records, work):
            raise ValueError("the records folder must not be inside a work folder, which is removed after its run")
        if work.exists():
            raise FileExistsError(f"the work folder {work} exists: delete it or choose another name")
    _psutil()
    files = rawio.photo_files([photos], IMAGE_EXTENSIONS)[0] if photos is not None else None
    if photos is not None and not files:
        raise ValueError(f"no photos in {photos}")
    if matches_from is not None and files is not None:
        project, _ = child.kept_run(matches_from)
        # the photos the child will have: those that are not online-only (run() leaves those out), as it compares
        local = {p.name for p in files if not machine.is_online_only(p)}
        if child.kept_photos(project) != local:
            online = len(files) - len(local)
            raise ValueError("--matches-from: the photos of the kept run are not the photos of this run"
                             + (f" ({online} online-only photos of this run left out: make them available offline)"
                                if online else ""))
    return out, records


def _real_set_options(steps: str, matches_from, markers, optimize: str, solver: str) -> None:
    """The options of :func:`check` for the real sets, refused with a ValueError that names the option and never
    a path."""
    from ..engine import bundle

    if optimize not in child.OPTIMIZE_ENGINES:
        raise ValueError(f"--optimize must be {' or '.join(child.OPTIMIZE_ENGINES)}, not {optimize!r}")
    if solver not in bundle.SOLVERS:
        raise ValueError(f"--solver must be {', '.join(bundle.SOLVERS[:-1])} or {bundle.SOLVERS[-1]}, "
                         f"not {solver!r}")
    if optimize == "colmap" and solver != "auto":
        raise ValueError(f"--solver {solver} is an option of --optimize ppg: COLMAP's adjustment has no solver to "
                         f"choose")
    if steps != "optimize" and (optimize, solver) != (child.OPTIMIZE_ENGINES[0], "auto"):
        raise ValueError("--optimize and --solver are options of --steps optimize")
    for option, value in (("--matches-from", matches_from), ("--markers", markers)):
        if value is not None and steps == "match":
            raise ValueError(f"{option} needs the alignment: not with --steps match")
    if matches_from is not None:
        child.kept_run(matches_from)
    if markers is not None:
        markers_project(markers)


def markers_project(path: Path) -> tuple[int, int]:
    """How many markers, and how many check points, the project at ``path`` gives ``--markers``: a project file
    whose active chunk has a marker measured on a photo (:func:`.steps.markers_chunk`). Opened and never saved;
    refused with a ValueError that names the option, never the path."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("--markers is not a project file: give a .ppg whose chunk has its markers measured")
    try:
        doc = Document.open(path)
    except Exception as exc:  # noqa: BLE001 - whatever stops the reading: its type, never the path
        raise ValueError(f"--markers: the project cannot be read ({type(exc).__name__})") from None
    chunk = child.markers_chunk(doc)
    return len(chunk.markers), sum(1 for m in chunk.markers if m.checkpoint)


def under(folder: Path, work: Path) -> bool:
    """Whether ``folder`` is ``work`` or lies under it, by their spellings with the case folded (``os.path.normcase``,
    then ``casefold``) at a separator: neither exists yet, so the file system cannot be asked, and the disks of macOS
    and Windows fold case (refusing too much on a disk that does not is harmless)."""
    folder_s, work_s = (os.path.normcase(str(p)).casefold() for p in (folder, work))
    return folder_s == work_s or folder_s.startswith(work_s.rstrip(os.sep) + os.sep)


def _inside(folder: Path, photos: Path) -> bool:
    """Whether ``folder`` is ``photos`` or lies under it: by the identity the file system gives the folders of its
    path that exist (``os.path.samefile``), since a file system that folds the case of names, or a link, would let
    another spelling of the same folder through a comparison of the paths."""
    for ancestor in (folder, *folder.parents):
        try:
            if ancestor.exists() and os.path.samefile(ancestor, photos):
                return True
        except OSError:
            continue
    return False


def _psutil():
    try:
        import psutil
    except ImportError:
        raise RuntimeError(PSUTIL_MISSING) from None
    return psutil


def _one_run(plan: _Plan, index: int, run_in: Callable | None = None) -> Path:
    """One repeat in its work folder, run by ``run_in`` (:func:`_run_in`, the child of the steps; ``ppg bench
    adjust`` gives its own, with a plan that has what the functions here read). Whatever stops the run once the
    folder is made (a job or a log that cannot be written, a child that cannot start, a record that is refused or
    cannot be written, a cancel whose cleanup failed) leaves the folder: that is said in the log, by the folder's
    name and the type of the exception, never a path, before the exception goes on (a folder kept by request is
    not said)."""
    work = plan.out / f"{plan.name}-{index}"
    work.mkdir(parents=True)
    try:
        return (run_in or _run_in)(plan, index, work)
    except BaseException as exc:
        if work.exists() and not plan.keep:
            log.warning("the run stopped (%s): the work folder %s is left in the output folder",
                        type(exc).__name__, work.name)
        raise


def _run_in(plan: _Plan, index: int, work: Path) -> Path:
    job = {"photos": [str(p) for p in plan.local], "work": str(work), "steps": plan.steps, "mapper": plan.mapper,
           "engine": plan.engine, "align_engine": plan.align_engine, "targets": plan.targets,
           "reference": str(plan.reference) if plan.reference else None, "crs": plan.crs,
           "check": list(plan.check) or None, "after": list(plan.after or ()) or None,
           "matches_from": str(plan.matches_from) if plan.matches_from else None,
           "markers": str(plan.markers) if plan.markers else None, "optimize": plan.optimize, "solver": plan.solver}
    (work / JOB).write_text(json.dumps(job, indent=1), encoding="utf-8")
    rec = record.new(plan.name, machine.machine_facts(), plan.disk, plan.set, dict(plan.settings))
    peaks: dict[str, int] = {}
    with open(work / LOG, "wb") as log_file:
        proc = subprocess.Popen(_command(work / JOB), stdin=subprocess.DEVNULL, stdout=log_file,
                                stderr=subprocess.STDOUT, env=plan.env, close_fds=False)
    try:
        while proc.poll() is None:
            time.sleep(SAMPLE_SECONDS)
            _sample(proc.pid, work / child.RESULT, peaks)
    except KeyboardInterrupt:
        _terminate(proc)
        warnings = _collect(rec, work, peaks, plan, ended=time.time(), cancelled=True)
        _finish(rec, work, plan, index, cancelled=True, warnings=warnings)
        raise
    warnings = _collect(rec, work, peaks, plan, ended=time.time(), code=proc.returncode)
    if rec["error"] is None and (plan.extra_results is not None or plan.after):
        _extra(rec, work, plan)
    return _finish(rec, work, plan, index, warnings=warnings)


def _command(job: Path) -> list[str]:
    """The child: this interpreter, by its absolute path (posix_spawn needs one), running the module of the steps."""
    return [os.path.abspath(sys.executable), "-m", "pyarchinit_photogrammetry.bench.steps", str(job)]


def _sample(pid: int, result: Path, peaks: dict[str, int]) -> None:
    """One sample of the resident memory of the child ``pid`` and of its children, given to the step that is running
    (the last one the child started and did not end) as its peak when it is higher; nothing when the child is gone
    or no step is running."""
    psutil = _psutil()
    try:
        proc = psutil.Process(pid)
        procs = [proc, *proc.children(recursive=True)]
    except psutil.Error:
        return
    rss = 0
    for p in procs:
        try:
            rss += p.memory_info().rss
        except psutil.Error:
            continue
    step = _running_step(_read(result))
    if step is not None:
        peaks[step] = max(peaks.get(step, 0), rss)


def _terminate(proc: subprocess.Popen) -> None:
    """The child and its children killed, and waited for."""
    psutil = _psutil()
    try:
        kids = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        kids = []
    for kid in kids:
        try:
            kid.kill()
        except psutil.Error:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    psutil.wait_procs(kids, timeout=10)
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:  # pragma: no cover - a child that will not die
        pass


def _read(result: Path) -> dict | None:
    """The result the child wrote, None when there is none yet or it cannot be read."""
    try:
        data = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _running_step(data: dict | None) -> str | None:
    entries = (data or {}).get("steps") or []
    last = entries[-1] if entries else None
    return last.get("step") if isinstance(last, dict) and last.get("end") is None else None


def _collect(rec: dict, work: Path, peaks: dict[str, int], plan: _Plan, ended: float, code: int | None = None,
             cancelled: bool = False) -> list[str]:
    """The steps, the settings, the results and the error of the record from what the child wrote. A step the child
    did not end (it died in it, or was cancelled) gets the seconds up to ``ended`` and says so; a child that ended
    without finishing the chain and without an error gives ``<step>: child ended with code N``. The texts of the
    exceptions the child wrote are scrubbed of paths, photo names, CRS codes and coordinates; the texts of the runner
    itself (the codes, ``cancelled``) are not. Returned: the warnings the child wrote (``warnings`` of its result;
    none from the child of the steps, those of ``ppg bench adjust``), scrubbed the same way, for :func:`_finish`:
    the judge of a record resets its warnings, so they are added after it."""
    data = _read(work / child.RESULT) or {}
    entries = [e for e in data.get("steps") or [] if isinstance(e, dict) and e.get("step")]

    def scrubbed(text) -> str:
        return record.scrub(str(text), plan.private, plan.names)

    error = scrubbed(data["error"]) if data.get("error") else None
    running, steps = None, []
    for e in entries:
        step = str(e["step"])
        entry = {"step": step, "seconds": e.get("seconds"), "peak_memory_mb": 0.0, "stages": e.get("stages") or {},
                 "engine": e.get("engine")}
        if "photo_cache" in e:
            entry["photo_cache"] = e["photo_cache"]
        if e.get("end") is None:
            running = step
            entry["seconds"] = round(max(ended - float(e.get("start") or ended), 0.0), 3)
            entry["error"] = f"{step}: cancelled" if cancelled else f"{step}: child ended with code {code}"
        elif e.get("error"):
            entry["error"] = scrubbed(e["error"])
        own = float(e.get("peak_memory_mb") or 0.0)
        entry["peak_memory_mb"] = round(max(own, peaks.get(step, 0) / 2**20), 1)
        steps.append(entry)
    if cancelled:
        error = "cancelled"
    elif error is None and not data.get("done"):
        where = running or (steps[-1]["step"] if steps else "setup")
        error = f"{where}: child ended with code {code}"
    rec["steps"] = steps
    rec["settings"].update(data.get("settings") or {})
    rec["results"] = dict(data.get("results") or {})
    rec["error"] = error
    return [scrubbed(w) for w in data.get("warnings") or []]


def _extra(rec: dict, work: Path, plan: _Plan) -> None:
    """The numbers the hooks of ``after`` give for the project as it was right after their steps, then those
    ``extra_results`` gives for the finished project, merged into the results; what goes wrong in them (a step whose
    project the child did not keep among them) is the error of the results."""
    try:
        for step, hook in (plan.after or {}).items():
            snapshot = work / child.SNAPSHOT.format(step=step)
            if not snapshot.is_file():
                raise ValueError(f"no project after {step}: the step did not run")
            rec["results"].update(child.numbers(hook(Document.open(snapshot)), f"the hook after {step}"))
        if plan.extra_results is not None:
            rec["results"].update(child.numbers(plan.extra_results(Document.open(work / child.PROJECT)),
                                                "extra_results"))
    except Exception as exc:
        rec["results"]["error"] = record.scrub(f"extra_results: {record.describe(exc)}", plan.private, plan.names)


def _finish(rec: dict, work: Path, plan: _Plan, index: int, cancelled: bool = False, warnings=()) -> Path:
    """The record judged, the child's ``warnings`` (of :func:`_collect`) added after the judge's, and written; then
    the work folder removed - unless kept by request, or the run failed (``rec["error"]`` set and not
    ``cancelled``): then it stays with the child's log and its result, said here by its name, so that the reason of
    a failure after an hour can be read without running the hour again. A record that is refused (a path in it) or
    cannot be written leaves the work folder for inspection (said by :func:`_one_run`); a work folder that cannot be
    removed stays, said here, by its name."""
    record.judge(rec)
    rec["warnings"].extend(warnings)
    path = record.write(rec, plan.records, plan.private, index=index)
    if plan.keep:
        return path
    if rec["error"] is not None and not cancelled:
        log.warning("the run failed: the work folder %s is kept in the output folder with the child's log", work.name)
        return path
    try:
        shutil.rmtree(work)
    except OSError as exc:
        log.warning("the work folder %s could not be removed (%s): it is left in the output folder", work.name,
                    type(exc).__name__)
    return path
