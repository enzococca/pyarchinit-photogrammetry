"""The commands of the bench - ``ppg bench run``, ``ppg bench synthetic``, ``ppg bench table`` - and what ``synthetic``
measures against the truth of its sets (:func:`trench_truth`, :func:`ring_truth`).

``ppg bench`` is a command of the program and measures the whole of it: the steps of a set of photos in a child
process, timed, with their memory and their results, one record per run (:mod:`.runner`). ``ppg-bench``, one hyphen
apart, is the Rust binary of ``native/bench``, which times the kernels of the engine alone, without Python.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import typer

bench_app = typer.Typer(help="The bench: a version of the program measured on a set of photos - the steps timed in a "
                             "child process, their memory, their results, one record per run -, on the synthetic sets "
                             "with the errors against their truth, and the table of a folder of records")

STEPS = ("align", "all")
SETS = ("trench", "ring")
# the targets of the synthetic sets: the trench and the ring are stamped with the same dictionary
TARGETS = "aruco_4x4_50"
# the check points of the trench: the split of tests/test_pipeline.py::test_checkpoints (targets 4 and 5 left out of
# the fit and measured, the other six control)
TRENCH_CHECK = ("target 4", "target 5")
STEPS_HELP = ("align (the markers when targets are given, the matching, the alignment, the references when the set "
              "has them) or all (then depth maps, dense cloud, model, texture, DEM, orthophoto)")
RECORDS_HELP = "the folder of the records (default: OUT)"


@bench_app.command("run")
def bench_run(photos: Path = typer.Argument(..., help="the folder of the photos"),
              out: Path = typer.Argument(..., help="the output folder: a work folder per run (removed after unless "
                                                   "--keep) and, without --records, the records"),
              name: str = typer.Option(..., "--name", help="the name of the run (letters, digits, '.', '_', '-'): "
                                                           "say the set and what is measured, e.g. trench-28-align"),
              steps: str = typer.Option("align", "--steps", help=STEPS_HELP),
              repeat: int = typer.Option(1, "--repeat", help="how many runs: a figure is a baseline with three"),
              keep: bool = typer.Option(False, "--keep", help="keep the work folders (the projects) after the runs"),
              records: Optional[Path] = typer.Option(None, "--records", help=RECORDS_HELP)):
    """Runs of a set of photos: the steps in a child process, each timed, with its peak memory, the stages of the
    engine and the results of the run; one record per run, valid or not and why. The record holds no path, no photo
    name, no coordinate and no CRS."""
    from . import runner

    if steps not in STEPS:
        raise typer.BadParameter(f"{' or '.join(STEPS)}, not {steps!r}", param_hint="--steps")
    _say(_run(runner.run, photos, out, name, steps=steps, repeat=repeat, keep=keep, records=records))


@bench_app.command("synthetic")
def bench_synthetic(which: str = typer.Argument(..., help="trench or ring"),
                    out: Path = typer.Argument(..., help="the folder of the set (its photos and gcp.csv), of the work "
                                                         "folders and, without --records, of the records"),
                    repeat: int = typer.Option(3, "--repeat", help="how many runs: a figure is a baseline with three"),
                    records: Optional[Path] = typer.Option(None, "--records", help=RECORDS_HELP)):
    """A synthetic set written in OUT and run as synthetic-trench or synthetic-ring, the errors against the truth of
    the set among the results (the keys that start with truth_).

    trench: the 28 photos of the synthetic trench of the tests, all the steps, its targets detected and its gcp.csv
    imported; truth_control_rmse and truth_check_rmse are the errors on the targets after a fit that leaves targets
    4 and 5 out and measures them, as the metric tests do.

    ring: 72 photos around a mound, a GPS position in the EXIF of each, the steps of align with its targets detected,
    georeferenced by the positions of the photos; truth_target_rmse (over truth_targets targets found),
    truth_camera_rmse, truth_camera_height_mean (estimated minus true) and truth_camera_height_rmse against the true
    targets and poses of the scene."""
    from . import runner
    from ..testing import synthetic

    if which not in SETS:
        raise typer.BadParameter(f"{' or '.join(SETS)}, not {which!r}")
    out = Path(out)
    if which == "trench":
        scene = synthetic.write_dataset(out)
        kwargs = dict(steps="all", targets=TARGETS, reference=out / "gcp.csv", crs=scene.crs,
                      extra_results=trench_truth)
    else:
        scene = synthetic.write_ring_dataset(out)
        kwargs = dict(steps="align", targets=TARGETS, extra_results=lambda doc: ring_truth(doc, scene))
    typer.echo(f"the set is written: {len(scene.cameras)} photos")
    _say(_run(runner.run, out / "photos", out, f"synthetic-{which}", repeat=repeat, records=records, **kwargs))


@bench_app.command("table")
def bench_table(folder: Path = typer.Argument(..., help="the folder of the records")):
    """The table of a folder of records: per run, program and step the median, the least and the most of the seconds
    of the valid runs, the peak memory, the stages; the results as medians; the records left out, by reason. A figure
    is a baseline with three runs."""
    from . import table

    records = table.load(folder)
    if not records:
        typer.echo(f"error: no record in {folder}", err=True)
        raise typer.Exit(1)
    typer.echo(table.render(*table.summarise(records)))


def _run(run, *args, **kwargs) -> list[Path]:
    """``run`` (:func:`.runner.run`) called; what it refuses before it starts (a ValueError) is a usage error, what
    stops it (a work folder that exists, psutil missing, every photo online-only) an error."""
    try:
        return run(*args, **kwargs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)


def _say(paths) -> None:
    """One line per record written: its file name, its steps and their seconds, whether it is valid and why not,
    its warnings."""
    for path in paths:
        rec = json.loads(Path(path).read_text(encoding="utf-8"))
        steps = rec.get("steps") or []
        seconds = sum(float(s.get("seconds") or 0.0) for s in steps)
        state = "valid" if rec.get("valid") else "not valid (" + "; ".join(rec.get("invalid_reasons") or []) + ")"
        warnings = rec.get("warnings") or []
        typer.echo(f"{Path(path).name}: {len(steps)} steps, {seconds:.1f} s, {state}"
                   + (f"; warnings: {', '.join(warnings)}" if warnings else ""))


def _rms(values) -> float:
    return float(np.sqrt(np.mean(np.square(np.asarray(values, float)))))


def trench_truth(doc) -> dict:
    """The errors of the trench against its ``gcp.csv``, the references its chain imported, with the split of the
    metric tests (``tests/test_pipeline.py::test_checkpoints``): :data:`TRENCH_CHECK` as check points, the fit done
    again on the other targets, in memory, the chunk left as it was found. ``truth_control_rmse`` and
    ``truth_check_rmse``, in metres. A ValueError when that fit cannot be made or a group has no error (targets not
    found)."""
    chunk = doc.chunk
    if chunk is None:
        raise ValueError("the project has no chunk")
    before = np.array(chunk.transform, float)
    flags = {m.key: m.checkpoint for m in chunk.markers}
    try:
        for m in chunk.markers:
            m.checkpoint = m.label in TRENCH_CHECK
        report = chunk.update_transform()
    finally:
        for m in chunk.markers:
            m.checkpoint = flags[m.key]
        chunk.transform = before
    if report.get("method") != "markers":
        raise ValueError(f"the fit on the control targets was not made (method {report.get('method')})")
    if report.get("control_rmse") is None or report.get("check_rmse") is None:
        raise ValueError("the control or the check targets have no error: targets not found")
    return {"truth_control_rmse": float(report["control_rmse"]), "truth_check_rmse": float(report["check_rmse"])}


def ring_truth(doc, scene) -> dict:
    """The errors of the ring against the truth of its scene, after the georeferencing by the positions of its photos:
    the targets the chain found and triangulated against ``scene.targets`` (``truth_target_rmse`` in metres, over
    ``truth_targets`` targets; no RMSE when none was found) and the centres of the aligned cameras against the true
    poses of ``scene.cameras`` (``truth_camera_rmse``; of the heights alone ``truth_camera_height_mean``, estimated
    minus true, which keeps the sign of heights that landed too high or too low, and ``truth_camera_height_rmse``).
    The truth goes the way the positions of the photos went: from the scene's system to EPSG:4326 as the set was
    written, then to the chunk's system as the program reads them. A ValueError when the chunk is not georeferenced
    or no aligned camera has a true pose."""
    from pyproj import Transformer

    from ..core import crs as crs_mod

    chunk = doc.chunk
    if chunk is None or crs_mod.is_local(chunk.crs) or crs_mod.is_geographic(chunk.crs):
        raise ValueError("the ring is not georeferenced: nothing to compare with the truth")
    to_wgs84 = Transformer.from_crs(scene.crs, "EPSG:4326", always_xy=True)

    def in_chunk_crs(points) -> np.ndarray:
        pts = np.atleast_2d(np.asarray(points, float))
        lon, lat = to_wgs84.transform(pts[:, 0], pts[:, 1])
        return crs_mod.transform_points(np.column_stack([lon, lat, pts[:, 2]]), "EPSG:4326", chunk.crs)

    truth = {cam["label"]: np.asarray(cam["world_from_cam"], float)[:3, 3] for cam in scene.cameras}
    cameras = [c for c in chunk.aligned_cameras if c.label in truth]
    if not cameras:
        raise ValueError("no aligned camera of the ring has a true pose")
    markers = [m for m in chunk.markers if m.code in scene.targets and m.position is not None]
    results = {"truth_targets": len(markers)}
    if markers:
        estimated = chunk.to_world(np.array([m.position for m in markers], float).reshape(-1, 3))
        d = estimated - in_chunk_crs([scene.targets[m.code] for m in markers])
        results["truth_target_rmse"] = _rms(np.linalg.norm(d, axis=1))
    estimated = chunk.to_world(np.array([c.center for c in cameras], float).reshape(-1, 3))
    d = estimated - in_chunk_crs([truth[c.label] for c in cameras])
    results.update(truth_camera_rmse=_rms(np.linalg.norm(d, axis=1)), truth_camera_height_mean=float(np.mean(d[:, 2])),
                   truth_camera_height_rmse=_rms(d[:, 2]))
    return results
