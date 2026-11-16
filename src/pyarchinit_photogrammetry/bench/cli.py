"""The commands of the bench: ``ppg bench run``, ``ppg bench synthetic`` (what it measures against the truth of its
sets is :mod:`.truth`) and ``ppg bench table``.

``ppg bench`` is a command of the program and measures the whole of it: the steps of a set of photos in a child
process, timed, with their memory and their results, one record per run (:mod:`.runner`). ``ppg-bench``, one hyphen
apart, is the Rust binary of ``native/bench``, which times the kernels of the engine alone, without Python.

Every command checks its arguments before anything runs (:func:`.runner.check`: a usage error, exit 2; psutil
missing is an error, exit 1), ``synthetic`` before it renders its set; what stops a run once it started is an error
(exit 1) said after the records already written, which stay.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

bench_app = typer.Typer(help="The bench: a version of the program measured on a set of photos - the steps timed in a "
                             "child process, their memory, their results, one record per run -, on the synthetic sets "
                             "with the errors against their truth, and the table of a folder of records")

SETS = ("trench", "ring")
STEPS_HELP = ("align (the matching, the alignment, the georeferencing when the photos carry a GPS position) or all "
              "(the default targets detected first; then depth maps, dense cloud, model, texture, DEM, orthophoto)")
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
    from . import steps as child

    if steps not in child.CHAINS:
        raise typer.BadParameter(f"{' or '.join(child.CHAINS)}, not {steps!r}", param_hint="--steps")
    _, records_folder = _check(runner.check, out, name, steps, repeat, records, photos=photos)
    _say(_run(runner.run, records_folder, name, photos, out, name, steps=steps, repeat=repeat, keep=keep,
              records=records))


@bench_app.command("synthetic")
def bench_synthetic(which: str = typer.Argument(..., help="trench or ring"),
                    out: Path = typer.Argument(..., help="the folder of the set (OUT/synthetic-<set>-set, which must "
                                                         "not exist yet), of the work folders and, without "
                                                         "--records, of the records"),
                    repeat: int = typer.Option(3, "--repeat", help="how many runs: a figure is a baseline with three"),
                    records: Optional[Path] = typer.Option(None, "--records", help=RECORDS_HELP)):
    """A synthetic set written in a folder of its own under OUT and run as synthetic-trench or synthetic-ring, the
    errors against the truth of the set among the results (the keys that start with truth_). The set stays after
    the runs: delete its folder to run it again in the same OUT.

    trench: the 28 photos of the synthetic trench of the tests, all the steps with the defaults of ppg process, its
    targets detected and its gcp.csv imported; truth_control_rmse and truth_check_rmse are the errors on the targets
    after a fit that leaves targets 4 and 5 out and measures them, as the metric tests do.

    ring: 72 photos around a mound, a GPS position in the EXIF of each, the steps of align with its targets detected,
    georeferenced by the positions of the photos; truth_target_rmse (over truth_targets targets found),
    truth_camera_rmse, truth_camera_height_mean (estimated minus true) and truth_camera_height_rmse against the true
    targets and poses of the scene."""
    from . import runner, truth
    from . import steps as child
    from ..testing import synthetic

    if which not in SETS:
        raise typer.BadParameter(f"{' or '.join(SETS)}, not {which!r}")
    out = Path(out)
    name = f"synthetic-{which}"
    set_dir = out / f"{name}-set"
    if set_dir.exists():
        raise typer.BadParameter(f"{set_dir} exists (the set of an earlier run): delete it or give another OUT")
    steps = "all" if which == "trench" else "align"
    _, records_folder = _check(runner.check, out, name, steps, repeat, records)
    if which == "trench":
        scene = synthetic.write_dataset(set_dir)
        kwargs = dict(reference=set_dir / "gcp.csv", crs=scene.crs, extra_results=truth.trench_truth)
    else:
        scene = synthetic.write_ring_dataset(set_dir)
        kwargs = dict(extra_results=lambda doc: truth.ring_truth(doc, scene))
    typer.echo(f"the set is written: {len(scene.cameras)} photos")
    # the synthetic sets are stamped with the targets ppg process detects by default (testing.synthetic._stamp_target)
    _say(_run(runner.run, records_folder, name, set_dir / "photos", out, name, steps=steps, repeat=repeat,
              records=records, targets=child.DEFAULT_TARGETS, **kwargs))


@bench_app.command("table")
def bench_table(folder: Path = typer.Argument(..., help="the folder of the records")):
    """The table of a folder of records: per run, program and step the median, the least and the most of the seconds
    of the valid runs, the peak memory, the stages, the engine; the results as medians; the records left out, by
    reason. A figure is a baseline with three runs of the same settings on one machine."""
    from . import table

    records = table.load(folder)
    if not records:
        typer.echo(f"error: no record in {folder}", err=True)
        raise typer.Exit(1)
    typer.echo(table.render(*table.summarise(records)))


def _check(check, *args, **kwargs) -> tuple[Path, Path]:
    """``check`` (:func:`.runner.check`) called: what it refuses is a usage error (exit 2), psutil missing an error
    (exit 1)."""
    try:
        return check(*args, **kwargs)
    except (ValueError, FileExistsError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)


def _run(run, folder: Path, run_name: str, *args, **kwargs) -> list[Path]:
    """``run`` (:func:`.runner.run`) called with ``args`` and ``kwargs``, its records returned. What stops it once it
    started - a record refused for a path it still holds (ValueError), a folder that cannot be made or removed
    (OSError), every photo online-only (RuntimeError) - is an error (exit 1), said after the records of the runs
    already done, which stay in ``folder`` under ``run_name``."""
    before = _records(folder, run_name)
    try:
        return run(*args, **kwargs)
    except (ValueError, OSError, RuntimeError) as exc:
        _say(sorted(_records(folder, run_name) - before))
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)


def _records(folder: Path, name: str) -> set[Path]:
    """The records of the run ``name`` in ``folder`` now (none when the folder is not there yet)."""
    return set(folder.glob(f"{name}-*.json")) if folder.is_dir() else set()


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
