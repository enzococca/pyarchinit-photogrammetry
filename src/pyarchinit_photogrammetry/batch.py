"""Batch processing: steps run over several chunks, the project saved after every step.

A batch is a list of steps ``{"op": name, "params": {...}, "chunks": "all" | "enabled" | [label
or key, ...]}`` (Metashape's *Batch Process*), kept as JSON. Every step runs on all its chunks
before the next one starts. After each step of each chunk the project is saved and so is the state
of the run (``<project>.ppg.files/batch.json``: the steps, those done, those failed), so a batch
stopped by a power cut, a crash or a cancel goes on from where it stopped (:func:`run_batch`
with ``resume``: the steps done are not run again, those that failed are tried again). A failing
step is tried again (``retries``); if it still fails the next steps of that chunk are skipped
(they would need its result) and the batch goes on with the other chunks, or stops
(``on_error="stop"``).

Paths in the parameters may use ``{project}`` (the folder of the project), ``{name}`` (its name)
and ``{chunk}`` (the label of the chunk).
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import time
from pathlib import Path

from .core.progress import Cancelled, Progress, as_progress

log = logging.getLogger("pyarchinit_photogrammetry")

# operation -> default parameters (the names of the arguments of the chunk methods)
OPERATIONS: dict[str, dict] = {
    "detect_markers": {"target_type": "aruco_4x4_50"},
    "import_reference": {"path": "", "crs": None, "columns": "nxyz"},
    "align_photos": {"quality": "high", "keypoint_limit": 40000, "tiepoint_limit": 8000, "pair_selection": "auto"},
    "match_photos": {"quality": "high", "keypoint_limit": 40000, "tiepoint_limit": 8000, "pair_selection": "auto"},
    "align_cameras": {},
    "update_transform": {},
    "optimize_cameras": {"fit_principal_point": False, "use_markers": True},
    "field_check": {"min_photos": 3},
    "build_depth_maps": {"quality": "medium", "filter_mode": "moderate", "skip_unreadable": False},
    "build_point_cloud": {},
    "build_model": {"source": "point_cloud", "surface": "arbitrary", "face_count": "medium"},
    "build_texture": {"texture_size": 4096, "mapping": "auto", "skip_unreadable": False},
    "build_dem": {"resolution": None, "source": "point_cloud"},
    "build_orthomosaic": {"resolution": None, "blending": "mosaic", "skip_unreadable": False},
    "calibrate_colours": {"vignetting": True},
    "estimate_precision": {},
    "export_point_cloud": {"path": "{project}/export/{chunk}_nuvola.las"},
    "export_model": {"path": "{project}/export/{chunk}_modello.ply"},
    "export_dem": {"path": "{project}/export/{chunk}_dem.tif"},
    "export_orthomosaic": {"path": "{project}/export/{chunk}_ortofoto.tif"},
    "generate_report": {"path": "{project}/export/{chunk}_report.html"},
}
STATE = "batch.json"


class BatchError(RuntimeError):
    """A step failed and the batch was told to stop."""


def step(op: str, chunks="all", **params) -> dict:
    """A step of a batch, its parameters completed with the defaults of :data:`OPERATIONS`."""
    if op not in OPERATIONS:
        raise ValueError(f"unknown operation {op!r}: one of {', '.join(OPERATIONS)}")
    unknown = set(params) - set(OPERATIONS[op])
    if unknown:
        raise ValueError(f"{op}: unknown parameters {', '.join(sorted(unknown))}")
    return {"op": op, "params": {**OPERATIONS[op], **params}, "chunks": chunks}


def load_steps(path: str | Path) -> list[dict]:
    """The steps of a batch file (a list, or ``{"steps": [...]}``), checked."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    steps = data.get("steps", []) if isinstance(data, dict) else data
    return [step(s["op"], s.get("chunks", "all"), **(s.get("params") or {})) for s in steps]


def save_steps(steps: list[dict], path: str | Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps({"steps": steps}, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def targets(doc, chunks) -> list:
    """The chunks a step runs on."""
    if chunks in (None, "all"):
        return list(doc.chunks)
    if chunks == "enabled":
        return [c for c in doc.chunks if c.enabled]
    out = []
    for ref in chunks:
        found = next((c for c in doc.chunks if c.label == str(ref) or str(c.key) == str(ref)), None)
        if found is None:
            raise ValueError(f"no chunk {ref!r} in the project")
        out.append(found)
    return out


def _fill(value, doc, chunk):
    if not isinstance(value, str) or "{" not in value:
        return value
    safe = re.sub(r"[^\w.-]+", "_", chunk.label).strip("_") or f"chunk{chunk.key}"
    return value.format(project=str(doc.path.parent), name=doc.path.stem, chunk=safe)


def run_step(doc, chunk, op: str, params: dict, progress: Progress | None = None):
    """One operation on one chunk (see :data:`OPERATIONS`)."""
    progress = as_progress(progress)
    p = {k: _fill(v, doc, chunk) for k, v in params.items()}
    if op == "align_photos":
        chunk.match_photos(progress=progress.sub(0.0, 0.5), **p)
        return chunk.align_cameras(progress=progress.sub(0.5, 0.5))
    if op == "update_transform":
        return chunk.update_transform(**p)
    if op == "import_reference":
        n = chunk.import_reference(p.pop("path"), **p)
        chunk.update_transform()
        return n
    if op == "field_check":
        from .engine.fieldcheck import field_check

        return field_check(chunk, progress=progress, **p)
    if op.startswith("export_") or op == "generate_report":
        path = Path(p.pop("path"))
        path.parent.mkdir(parents=True, exist_ok=True)
        return getattr(chunk, op)(path, **p)
    return getattr(chunk, op)(progress=progress, **p)


def _key(chunk, index: int) -> str:
    return f"{chunk.key}:{index}"


def state_path(doc) -> Path:
    return doc.files_dir / STATE


def pending(doc, steps: list[dict] | None = None) -> dict | None:
    """The state of an unfinished batch of the project (``steps`` given: only if it is the same
    batch), or None."""
    path = state_path(doc)
    if doc.path is None or not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if state.get("finished") or (steps is not None and state.get("steps") != steps):
        return None
    return state


def run_batch(doc, steps: list[dict], resume: bool = True, retries: int = 1, on_error: str = "continue",
              progress: Progress | None = None) -> dict:
    """Run ``steps`` (see the module docstring); returns ``{"done", "skipped", "failed", "seconds"}``
    (the keys ``chunk:step``). The project must have been saved once (``doc.path``)."""
    if doc.path is None:
        raise RuntimeError("save the project first: the batch saves it after every step")
    progress = as_progress(progress)
    steps = [step(s["op"], s.get("chunks", "all"), **(s.get("params") or {})) for s in steps]
    state = pending(doc, steps) if resume else None
    if state is None:
        state = {"steps": steps, "done": [], "failed": {}, "skipped": [],
                 "started": datetime.datetime.now().isoformat(timespec="seconds"), "finished": None}
    # resumed: what was done stays done, what failed (a disk away, a photo still downloading) is tried again
    done, failed, skipped = set(state["done"]), {}, set()
    work = [(i, s, c) for i, s in enumerate(steps) for c in targets(doc, s["chunks"])]
    t0 = time.time()

    def save_state():
        state.update(done=sorted(done), failed=failed, skipped=sorted(skipped))
        state_path(doc).parent.mkdir(parents=True, exist_ok=True)
        state_path(doc).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    save_state()
    broken: set[str] = set()  # chunks with a failed step
    for n, (i, s, chunk) in enumerate(work):
        key = _key(chunk, i)
        sub = progress.sub(n / len(work), 1.0 / len(work))
        if key in done:
            continue
        if str(chunk.key) in broken:  # an earlier step of this chunk failed: this one would too
            skipped.add(key)
            save_state()
            continue
        sub(0.0, f"Batch {n + 1}/{len(work)}: {s['op']} - {chunk.label}")
        for attempt in range(int(retries) + 1):
            try:
                run_step(doc, chunk, s["op"], s["params"], progress=sub)
                done.add(key)
                failed.pop(key, None)
                break
            except Cancelled:
                doc.save()
                save_state()
                raise
            except Exception as exc:  # noqa: BLE001 - any failure of a step is reported and retried
                log.warning("Batch: %s on %s failed (attempt %d): %s", s["op"], chunk.label, attempt + 1, exc)
                failed[key] = f"{type(exc).__name__}: {exc}"
        doc.save()
        if key in failed:
            broken.add(str(chunk.key))
            save_state()
            if on_error == "stop":
                raise BatchError(f"{s['op']} on {chunk.label}: {failed[key]}")
            continue
        save_state()
    state["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    save_state()
    progress(1.0, f"Batch finished: {len(done)} steps done, {len(failed)} failed, {len(skipped)} skipped")
    return {"done": sorted(done), "failed": failed, "skipped": sorted(skipped), "seconds": time.time() - t0}
