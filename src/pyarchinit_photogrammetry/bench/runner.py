"""A run of the bench: the steps of a set of photos in a child process, its memory sampled, one record per run.

:func:`run` reads the facts of the machine, of the disk that holds the photos and of the photos (:mod:`.machine`),
starts the child (:mod:`.steps`, the same interpreter, with the environment the timing of the stages needs) in a work
folder of its own, samples every :data:`SAMPLE_SECONDS` the memory of the child and of its children, and when the child
ends builds the record (:mod:`.record`), judges it and writes it. A run that fails, dies or is cancelled leaves a record
too, with its error and the steps done; photos that are online-only are left out and make the record not valid. What
the run writes beside the records (a work folder it could not remove, one left by a child that could not start) is
said in the log by the name of the folder, never by its path.
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
        records: Path | None = None, *, extra_results: Callable | None = None, targets: str | None = None,
        reference: Path | None = None, crs: str | None = None) -> list[Path]:
    """``repeat`` runs of the chain ``steps`` (``align`` or ``all``, :data:`.steps.CHAINS`) on the photos of the folder
    ``photos``, each in the work folder ``out/<name>-<i>`` (deleted after unless ``keep``), the records written to
    ``records`` (default ``out``). Returned: the path of each record.

    Refused, in this order and before anything runs: a name that is not one a file can take (ValueError), unknown
    steps or a repeat below 1 (ValueError), ``out`` or ``records`` inside ``photos`` (ValueError; by the identity of
    the folders, whatever the case of the letters), ``records`` inside a work folder (ValueError: it is removed after
    its run), a work folder of one of the repeats that exists already (FileExistsError; nothing is deleted in
    silence), psutil missing (RuntimeError naming the extra), every photo online-only (RuntimeError; no child is
    started). Photos that are online-only are left out of the run and make the record not valid.

    ``targets``, ``reference`` and ``crs`` go to the child, which detects the markers and imports the reference when
    they are given; ``all`` detects :data:`.steps.DEFAULT_TARGETS` when no targets are given, as ``ppg process`` does,
    ``align`` detects none. ``extra_results``, called with the finished project (a :class:`..core.document.Document`)
    after the last step of a run that did not fail, returns a dict of numbers merged into the results; an exception
    in it, or a value that is not a number, is kept as ``results["error"]`` and makes the record not valid, the times
    stay. A KeyboardInterrupt while the child runs terminates it and its children, writes the record with the error
    ``cancelled``, removes the work folder unless ``keep`` and is raised again."""
    record.check_name(name)
    if steps not in child.CHAINS:
        raise ValueError(f"steps must be {' or '.join(child.CHAINS)}, not {steps!r}")
    if int(repeat) < 1:
        raise ValueError("repeat must be 1 or more")
    photos, out = Path(photos).resolve(), Path(out).resolve()
    records = Path(records).resolve() if records else out
    if not photos.is_dir():
        raise ValueError(f"{photos} is not a folder of photos")
    if _inside(out, photos) or _inside(records, photos):
        raise ValueError("the work folder and the records folder must not be inside the folder of the photos")
    for index in range(1, int(repeat) + 1):
        work = out / f"{name}-{index}"
        if records == work or work in records.parents:
            raise ValueError("the records folder must not be inside a work folder, which is removed after its run")
        if work.exists():
            raise FileExistsError(f"the work folder {work} exists: delete it or choose another name")
    _psutil()
    files = rawio.photo_files([photos], IMAGE_EXTENSIONS)[0]
    if not files:
        raise ValueError(f"no photos in {photos}")
    local = tuple(p for p in files if not machine.is_online_only(p))
    if not local:
        raise RuntimeError("every photo is online-only: make them available offline first")
    set_ = machine.photo_set_facts(files)
    disk = machine.disk_facts(photos)
    disk["read_mb_s"] = machine.read_rate(list(local))
    env = dict(os.environ)
    env.update(LIVE_LOG_ENV)
    if targets is None and steps == "all":
        targets = child.DEFAULT_TARGETS
    reference = Path(reference).resolve() if reference else None
    settings = {**{key: None for key in child.SFM_SETTINGS}, **child.match_settings(), "pycolmap": None,
                "photo_cache_mb": photo_cache_mb(env), "georeferencing": None, "targets": targets}
    private = (photos, out, Path.home()) + ((reference,) if reference else ())
    plan = _Plan(name=name, steps=steps, local=local, out=out, records=records, keep=keep, env=env, disk=disk,
                 set=set_, settings=settings, private=private, names=tuple(p.name for p in files),
                 extra_results=extra_results, targets=targets, reference=reference, crs=crs)
    return [_one_run(plan, index) for index in range(1, int(repeat) + 1)]


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


def _one_run(plan: _Plan, index: int) -> Path:
    """One repeat in its work folder. Whatever stops the run once the folder is made (a job or a log that cannot be
    written, a child that cannot start, a record that is refused or cannot be written, a cancel whose cleanup
    failed) leaves the folder: that is said in the log, by the folder's name and the type of the exception, never a
    path, before the exception goes on (a folder kept by request is not said)."""
    work = plan.out / f"{plan.name}-{index}"
    work.mkdir(parents=True)
    try:
        return _run_in(plan, index, work)
    except BaseException as exc:
        if work.exists() and not plan.keep:
            log.warning("the run stopped (%s): the work folder %s is left in the output folder",
                        type(exc).__name__, work.name)
        raise


def _run_in(plan: _Plan, index: int, work: Path) -> Path:
    job = {"photos": [str(p) for p in plan.local], "work": str(work), "steps": plan.steps, "targets": plan.targets,
           "reference": str(plan.reference) if plan.reference else None, "crs": plan.crs}
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
        _collect(rec, work, peaks, plan, ended=time.time(), cancelled=True)
        _finish(rec, work, plan, index)
        raise
    _collect(rec, work, peaks, plan, ended=time.time(), code=proc.returncode)
    if rec["error"] is None and plan.extra_results is not None:
        _extra(rec, work, plan)
    return _finish(rec, work, plan, index)


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
             cancelled: bool = False) -> None:
    """The steps, the settings, the results and the error of the record from what the child wrote. A step the child
    did not end (it died in it, or was cancelled) gets the seconds up to ``ended`` and says so; a child that ended
    without finishing the chain and without an error gives ``<step>: child ended with code N``. The texts of the
    exceptions the child wrote are scrubbed of paths, photo names, CRS codes and coordinates; the texts of the runner
    itself (the codes, ``cancelled``) are not."""
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


def _extra(rec: dict, work: Path, plan: _Plan) -> None:
    """The numbers ``extra_results`` gives for the finished project merged into the results; what goes wrong in it
    is the error of the results."""
    try:
        extra = dict(plan.extra_results(Document.open(work / child.PROJECT)))
        for key, value in extra.items():
            if not isinstance(value, (int, float)):
                raise TypeError(f"extra_results gave {key!r} that is not a number")
        rec["results"].update(extra)
    except Exception as exc:
        rec["results"]["error"] = record.scrub(f"extra_results: {record.describe(exc)}", plan.private, plan.names)


def _finish(rec: dict, work: Path, plan: _Plan, index: int) -> Path:
    """The record judged and written, then the work folder removed unless kept. A record that is refused (a path in
    it) or cannot be written leaves the work folder for inspection (said by :func:`_one_run`); a work folder that
    cannot be removed stays, said here, by its name."""
    record.judge(rec)
    path = record.write(rec, plan.records, plan.private, index=index)
    if not plan.keep:
        try:
            shutil.rmtree(work)
        except OSError as exc:
            log.warning("the work folder %s could not be removed (%s): it is left in the output folder", work.name,
                        type(exc).__name__)
    return path
