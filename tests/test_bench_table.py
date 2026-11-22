"""The table of the bench (``bench/table.py``), its commands (``ppg bench run``, ``synthetic``, ``table``) and the
truth of its synthetic sets (``bench/truth.py``): the records of a folder summarised by run, program and step over the
valid runs - a figure is a baseline with three runs of the same settings on one machine -, the records left out counted
by reason, the results as medians; the commands from the command line, their checks before anything runs, what each
synthetic set writes and runs; and what ``synthetic`` measures against the truth. The records here are made by hand,
never by a run of the engine; the truth of the trench is measured on the project of the session's synthetic trench,
the truth of the ring on a chunk whose cameras and targets stand, behind a similarity, at the true poses and
positions of a small ring."""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import types
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.bench import pairs, record, runner, table, truth
from pyarchinit_photogrammetry.bench import steps as child
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.core.camera import Camera
from pyarchinit_photogrammetry.engine import sfm
from pyarchinit_photogrammetry.testing import synthetic

MACHINE = {"system": "Darwin", "release": "27.0.0", "arch": "arm64", "cpu": "CPU A", "cores": 12, "memory_gb": 24.0,
           "adapters": [], "load": 0.5, "on_battery": False, "dropbox_running": False}
DISK = {"bus": "PCI-Express", "link_mbps": None, "read_mb_s": 400.0, "solid_state": True}
SET = {"photos": 28, "bytes": 1, "online_only": 0, "gps": 0}
SETTINGS = {"pairs": 378, "matcher": "sift", "quality": "high", "gpu": "GPU A", "gpu_pairs": 100, "cpu_pairs": 278,
            "mapper": "incremental", "global_fallback": None, "engine": None, "features": "gpu", "routes": None,
            "max_ratio": 0.8, "max_distance": 0.7, "cross_check": True, "pycolmap": "4.2.1", "photo_cache_mb": None,
            "georeferencing": "markers", "targets": "aruco_4x4_50", "mapper_requested": "auto",
            "engine_requested": "auto"}
ENGINE = {"requested": "auto", "used": "classic", "fallback": None}
NATIVE = {"requested": "native", "used": "native", "fallback": None}
# the record of a native matching: what the native engine writes in chunk.meta["sfm"] beside the classic keys, which
# differ between repeats of one run (the routes count what each pair took, the features where they were found)
NATIVE_SETTINGS = {"engine_requested": "native", "engine": "native", "features": "gpu+cpu",
                   "routes": {"guided": 300, "brute_force": 70, "dropped": 8}}
TABLE_LOG = "pyarchinit_photogrammetry.bench.table"
PAIRS_LINE = re.compile(r"^verified A (\d+), verified B (\d+), lost (\d+) \((\d+\.\d\d) %\), gained (\d+)$")


def colmap_database(path: Path, names: list[str], geometries: dict[tuple[str, str], tuple[int, int]],
                    ids: dict[str, int] | None = None) -> Path:
    """A database with the two tables of COLMAP's the bench reads, ``images`` (``image_id``, ``name``) and
    ``two_view_geometries`` (``pair_id``, ``rows``, ``config``, the rest NULL): ``names`` numbered from 1 in their
    order (or by ``ids``), ``geometries`` a row per pair of names with its inlier count and its configuration, keyed
    as COLMAP keys a pair (the lower image id first)."""
    ids = ids or {name: i for i, name in enumerate(names, 1)}
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE images (image_id INTEGER PRIMARY KEY NOT NULL, name TEXT NOT NULL UNIQUE, "
                    "camera_id INTEGER NOT NULL)")
        con.execute("CREATE TABLE two_view_geometries (pair_id INTEGER PRIMARY KEY NOT NULL, rows INTEGER NOT NULL, "
                    "cols INTEGER NOT NULL, data BLOB, config INTEGER NOT NULL, F BLOB, E BLOB, H BLOB, qvec BLOB, "
                    "tvec BLOB)")
        con.executemany("INSERT INTO images VALUES (?, ?, 1)", [(ids[name], name) for name in names])
        for (a, b), (rows, config) in geometries.items():
            low, high = sorted((ids[a], ids[b]))
            con.execute("INSERT INTO two_view_geometries (pair_id, rows, cols, config) VALUES (?, ?, 2, ?)",
                        (low * sfm.COLMAP_MAX_IMAGES + high, rows, config))
        con.commit()
    finally:
        con.close()
    return path


def a_record(seconds: float, *, link=None, stages=None, peak=100.0, results=None, name="t", program=None,
             started=None, step="align_cameras", engine=ENGINE, settings=None, machine=None, set_facts=None) -> dict:
    """A judged record of one step of ``seconds``; ``link`` below 5000 Mb/s makes it not valid (``slow disk link``);
    ``settings``, ``machine`` and ``set_facts`` change entries of the defaults."""
    rec = record.new(name, {**MACHINE, **(machine or {})}, {**DISK, "link_mbps": link}, {**SET, **(set_facts or {})},
                     {**SETTINGS, **(settings or {})})
    if program:
        rec["program"] = program
    if started:
        rec["started"] = started
    rec["steps"] = [{"step": step, "seconds": seconds, "peak_memory_mb": peak, "stages": dict(stages or {}),
                     "engine": engine}]
    rec["results"] = dict(results or {})
    return record.judge(rec)


def four_records() -> list[dict]:
    """Three valid runs of the alignment (10, 12, 30 s) and one on a slow link (1 s), of the same name and program."""
    return [a_record(10.0, stages={"mapper": 8.0, "ba": 1.0}, peak=300.0, results={"cameras": 28, "aligned": 28}),
            a_record(12.0, stages={"mapper": 9.0, "ba": 2.0}, peak=500.0, results={"cameras": 28, "aligned": 27}),
            a_record(30.0, stages={"mapper": 25.0}, peak=400.0, results={"cameras": 28, "aligned": 28}),
            a_record(1.0, link=480, results={"cameras": 28, "aligned": 1})]


def written(folder: Path, records: list[dict]) -> Path:
    for i, rec in enumerate(records, 1):
        rec["started"] = f"2026-11-16T10:00:{i:02d}Z"
        record.write(rec, folder)
    return folder


def invoke(args: list[str]):
    return CliRunner().invoke(app, ["bench", *args])


def plain(result) -> str:
    """The output of a command as one line of words: a usage error is drawn in a panel whose lines wrap the message
    (a path takes most of a line), so a phrase is looked for across the lines, without the box."""
    return " ".join(re.sub(r"[─-╿]", " ", result.output).split())


def raising(exc: BaseException):
    def fail(*args, **kwargs):
        raise exc
    return fail


def a_photo_folder(folder: Path) -> Path:
    """A folder with one photo file (empty: the checks list it, nothing reads it)."""
    folder.mkdir(parents=True)
    (folder / "IMG_0001.jpg").write_bytes(b"")
    return folder


# --- summarise and render ---------------------------------------------------------------------------------------------

def test_three_valid_runs_and_one_on_a_slow_link_give_one_baseline_row():
    rows, left_out = table.summarise(four_records())
    assert len(rows) == 1 and left_out == {"slow disk link": 1}
    (row,) = rows
    assert (row["name"], row["program"], row["step"]) == ("t", ppg.__version__, "align_cameras")
    assert row["runs"] == 3 and row["seconds"] == 12.0 and row["baseline"] is True
    assert row["min"] == 10.0 and row["max"] == 30.0 and row["peak_memory_mb"] == 500.0
    assert row["stages"] == {"mapper": 9.0, "ba": 1.5} and row["engine"] == "classic"


def test_two_valid_runs_are_not_a_baseline():
    rows, left_out = table.summarise(four_records()[1:3])
    assert len(rows) == 1 and rows[0]["runs"] == 2 and rows[0]["baseline"] is False and left_out == {}
    text = table.render(rows, left_out)
    assert "not a baseline (n < 3)" in text and "21.0" in text  # the median of 12 and 30
    rows, _ = table.summarise(four_records())
    assert "not a baseline" not in table.render(rows, {})


def test_the_results_are_the_medians_of_the_valid_runs_and_the_table_shows_them_with_their_count():
    records = four_records()
    records[0]["results"]["control_rmse"] = 0.003
    records[1]["results"]["control_rmse"] = 0.001
    records[2]["results"]["error"] = "extra_results: ValueError: a word"  # not a number: no figure
    rows, left_out = table.summarise(records)
    (row,) = rows
    assert row["results"] == {"cameras": 28, "aligned": 28, "control_rmse": 0.002} and row["valid_runs"] == 3
    assert row["result_runs"] == {"cameras": 3, "aligned": 3, "control_rmse": 2}
    text = table.render(rows, left_out)
    results = text[text.index("Results"):]
    # a result present in some of the runs only shows its count beside its median; one in all of them does not
    assert "| cameras | aligned | control_rmse |" in results and "| 28 | 28 | 0.002 (2 of 3) |" in results
    assert "slow disk link" in text.splitlines()[-1] and "error" not in results


def test_the_rows_follow_the_names_the_programs_and_the_steps_as_the_records_list_them():
    old = a_record(5.0, program="1.9.0")
    old["steps"].insert(0, {"step": "match_photos", "seconds": 7.0, "peak_memory_mb": 50.0, "stages": {},
                            "engine": ENGINE})
    other = a_record(3.0, name="s", engine={"requested": "native", "used": "native", "fallback": None})
    rows, left_out = table.summarise([*four_records(), old, other])
    assert [(r["name"], r["program"], r["step"]) for r in rows] == [
        ("s", ppg.__version__, "align_cameras"), ("t", "1.9.0", "match_photos"), ("t", "1.9.0", "align_cameras"),
        ("t", ppg.__version__, "align_cameras")]
    assert rows[0]["engine"] == "native" and rows[1]["seconds"] == 7.0 and rows[1]["stages"] == {}
    assert left_out == {"slow disk link": 1}
    text = table.render(rows, left_out)
    assert text.index("| s |") < text.index("| t | 1.9.0 | match_photos |")
    assert text.index("| t | 1.9.0 | match_photos |") < text.index("| t | 1.9.0 | align_cameras |")


def test_programs_sort_as_versions():
    records = [a_record(1.0, program=p) for p in ("2.0.0", "main-abc", "2.0.0.dev1", "1.10.2", "1.9.0")]
    rows, _ = table.summarise(records)
    assert [r["program"] for r in rows] == ["1.9.0", "1.10.2", "2.0.0.dev1", "2.0.0", "main-abc"]
    # the key without packaging: runs of digits as numbers, the rest as text after them
    assert table._pieces("1.9.0") < table._pieces("1.10.2") < table._pieces("2.0.0") < table._pieces("2.0.0.dev1")


def test_runs_that_differ_in_their_settings_or_their_machine_are_marked_and_are_no_baseline():
    rows, _ = table.summarise([a_record(10.0), a_record(12.0), a_record(30.0)])
    assert rows[0]["mixed_settings"] is False and rows[0]["mixed_machines"] is False and rows[0]["baseline"] is True
    assert rows[0]["mixed_sets"] is False
    rows, _ = table.summarise([a_record(10.0), a_record(12.0), a_record(30.0, settings={"photo_cache_mb": 0})])
    assert rows[0]["mixed_settings"] is True and rows[0]["mixed_machines"] is False and rows[0]["baseline"] is False
    assert "| not a baseline (mixed settings) |" in table.render(rows, {})
    rows, _ = table.summarise([a_record(10.0), a_record(12.0), a_record(30.0, machine={"cpu": "CPU B"})])
    assert rows[0]["mixed_machines"] is True and rows[0]["mixed_settings"] is False and rows[0]["baseline"] is False
    assert "| not a baseline (mixed machines) |" in table.render(rows, {})
    for key, value in (("system", "Linux"), ("arch", "x86_64"), ("memory_gb", 16.0)):
        rows, _ = table.summarise([a_record(10.0), a_record(12.0), a_record(30.0, machine={key: value})])
        assert rows[0]["mixed_machines"] is True, key
    for key, value in (("cores", 8), ("load", 3.0), ("release", "26.1"), ("adapters", ["GPU A"])):
        rows, _ = table.summarise([a_record(10.0), a_record(12.0), a_record(30.0, machine={key: value})])
        assert rows[0]["mixed_machines"] is False, key
    # what two runs of one set with the same settings differ in by themselves is no mix: the version of pycolmap,
    # the count of pairs (above 40 photos the preselection's RANSAC has a random seed) and their split between the
    # GPU and the CPU (the GPU takes pairs as it goes), the mapper and its fallback (what the alignment did)
    rows, _ = table.summarise([a_record(10.0, settings={"pycolmap": "4.2.0", "pairs": 488}),
                               a_record(12.0, settings={"gpu_pairs": 68, "cpu_pairs": 310, "pairs": 494}),
                               a_record(30.0, settings={"mapper": "global", "global_fallback": "incremental: 3/72"})])
    assert rows[0]["mixed_settings"] is False and rows[0]["mixed_sets"] is False and rows[0]["baseline"] is True
    # another set under the same name (how many photos, their bytes) is a mix of sets; the counts of photos with
    # GPS and online-only are not compared
    rows, _ = table.summarise([a_record(10.0), a_record(12.0), a_record(30.0, set_facts={"photos": 29})])
    assert rows[0]["mixed_sets"] is True and rows[0]["mixed_settings"] is False and rows[0]["baseline"] is False
    assert "| not a baseline (mixed sets) |" in table.render(rows, {})
    rows, _ = table.summarise([a_record(10.0), a_record(12.0), a_record(30.0, set_facts={"bytes": 2})])
    assert rows[0]["mixed_sets"] is True and rows[0]["baseline"] is False
    rows, _ = table.summarise([a_record(10.0), a_record(12.0), a_record(30.0, set_facts={"gps": 28, "online_only": 1})])
    assert rows[0]["mixed_sets"] is False
    rows, _ = table.summarise([a_record(10.0, settings={"quality": "medium"}),
                               a_record(12.0, machine={"arch": "x"}, set_facts={"photos": 1})])
    assert rows[0]["baseline"] is False
    assert "| not a baseline (n < 3, mixed settings, mixed machines, mixed sets) |" in table.render(rows, {})


def test_runs_that_differ_in_the_mapper_asked_for_are_a_mix_under_one_name_and_two_rows_under_two():
    # the mapper a run was asked for (--mapper of ppg bench run) is a setting of the run and is compared, unlike the
    # mapper the run used, which is what the alignment did (auto picks one, a global one falls back)
    assert "mapper_requested" not in table.RUN_FACTS and "mapper" in table.RUN_FACTS
    asked = {"mapper_requested": "global", "mapper": "global"}
    rows, _ = table.summarise([a_record(10.0), a_record(12.0), a_record(30.0, settings=asked)])
    assert [(r["name"], r["mixed_settings"], r["baseline"]) for r in rows] == [("t", True, False)]
    assert "| not a baseline (mixed settings) |" in table.render(rows, {})
    rows, _ = table.summarise([a_record(s, name="t-auto") for s in (10.0, 12.0, 30.0)]
                              + [a_record(s, name="t-global", settings=asked) for s in (5.0, 6.0, 7.0)])
    assert [(r["name"], r["seconds"], r["mixed_settings"], r["baseline"]) for r in rows] == \
        [("t-auto", 12.0, False, True), ("t-global", 6.0, False, True)]


def test_runs_that_differ_in_the_engine_asked_for_are_a_mix_under_one_name_and_two_rows_under_two():
    """The engine a run was asked for (--engine) is a setting and is compared; what the record of the matching says
    the run did - its engine, where the features were found, the routes the pairs took - are facts of the run:
    three native repeats that differ in them are one measurement. Native and classic runs under one name are a mix
    (the engine column says both); under two names, two baselines side by side."""
    assert "engine_requested" not in table.RUN_FACTS
    assert {"engine", "features", "routes"} <= set(table.RUN_FACTS)
    classic = [a_record(s, step="match_photos") for s in (10.0, 12.0, 30.0)]
    native = [a_record(s, step="match_photos", engine=NATIVE, settings={**NATIVE_SETTINGS, "routes": {
        "guided": 300 + i, "brute_force": 70 - i, "dropped": 8}, "features": ("gpu", "gpu+cpu", "cpu")[i]})
        for i, s in enumerate((5.0, 6.0, 7.0))]
    rows, _ = table.summarise(native)
    assert [(r["engine"], r["mixed_settings"], r["baseline"]) for r in rows] == [("native", False, True)]
    rows, _ = table.summarise(classic + native)
    assert [(r["name"], r["engine"], r["mixed_settings"], r["baseline"]) for r in rows] == \
        [("t", "classic/native", True, False)]
    assert "| not a baseline (mixed settings) |" in table.render(rows, {})
    for rec in native:
        rec["name"] = "t-native"
    rows, _ = table.summarise(classic + native)
    assert [(r["name"], r["engine"], r["seconds"], r["mixed_settings"], r["baseline"]) for r in rows] == \
        [("t", "classic", 12.0, False, True), ("t-native", "native", 6.0, False, True)]


def test_the_two_results_of_the_matching_gate_come_right_after_the_photos_aligned():
    results = {"zeta": 1.0, "reprojection_rms_px": 0.5, "tie_points_per_photo": 812.4, "verified_pairs": 370,
               "aligned": 28, "cameras": 28, "control_rmse": 0.002}
    rows, left_out = table.summarise([a_record(10.0, results=results)])
    text = table.render(rows, left_out)
    assert "| name | program | runs | cameras | aligned | verified_pairs | tie_points_per_photo | " \
           "reprojection_rms_px | control_rmse | zeta |" in text
    assert "| 28 | 28 | 370 | 812.4 | 0.5 | 0.002 | 1 |" in text
    assert table.FIRST_RESULTS[:5] == ("cameras", "aligned", "verified_pairs", "tie_points_per_photo",
                                       "reprojection_rms_px")


def test_every_reason_of_a_record_left_out_is_counted_before_its_colon():
    failed = a_record(2.0, link=480)
    failed["error"] = "align_cameras: RuntimeError: no model"
    record.judge(failed)
    never_judged = a_record(2.0)
    never_judged["valid"], never_judged["invalid_reasons"] = None, []
    rows, left_out = table.summarise([failed, never_judged, a_record(4.0)])
    assert left_out == {"slow disk link": 1, "failed": 1, "not valid": 1} and len(rows) == 1 and rows[0]["runs"] == 1
    assert table.render(rows, left_out).splitlines()[-1] == "Records left out: failed 1, not valid 1, slow disk link 1."
    assert table.render(rows, {}).splitlines()[-1] == "Records left out: none."


def test_load_takes_the_records_of_the_schema_and_names_what_it_skips_by_file_name(tmp_path, caplog):
    folder = written(tmp_path / "records", four_records()[:2])
    (folder / "other-schema.json").write_text(json.dumps({**a_record(1.0), "schema": 2}), encoding="utf-8")
    (folder / "a-list.json").write_text("[1, 2]", encoding="utf-8")
    (folder / "broken.json").write_text("{", encoding="utf-8")
    (folder / "notes.txt").write_text("{}", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=TABLE_LOG):
        records = table.load(folder)
    assert [r["started"] for r in records] == ["2026-11-16T10:00:01Z", "2026-11-16T10:00:02Z"]
    skipped = [entry.getMessage() for entry in caplog.records if entry.name == TABLE_LOG]
    assert sorted(m.split(" ")[0] for m in skipped) == ["a-list.json", "broken.json", "other-schema.json"]
    assert not any(str(tmp_path) in m for m in skipped)


# --- the commands -----------------------------------------------------------------------------------------------------

def test_the_table_command_prints_the_table_of_a_folder(tmp_path):
    folder = written(tmp_path / "records", four_records())
    result = invoke(["table", str(folder)])
    assert result.exit_code == 0, result.output
    assert "12.0" in result.output and "| 3 |" in result.output and "slow disk link 1" in result.output
    empty = tmp_path / "empty"
    empty.mkdir()
    result = invoke(["table", str(empty)])
    assert result.exit_code == 1 and "no record" in result.output


def test_run_without_a_name_exits_2():
    result = invoke(["run", "a", "b"])
    assert result.exit_code == 2 and "--name" in result.output


def test_run_checks_its_arguments_before_the_runner_starts(tmp_path, monkeypatch):
    pytest.importorskip("psutil")
    monkeypatch.setattr(runner, "run", lambda *a, **k: pytest.fail("the runner must not start"))
    photos, out = a_photo_folder(tmp_path / "p"), tmp_path / "o"
    result = invoke(["run", str(tmp_path / "nowhere"), str(out), "--name", "n"])
    assert result.exit_code == 2 and "is not a folder of photos" in plain(result)
    result = invoke(["run", str(photos), str(out), "--name", "bad/name"])
    assert result.exit_code == 2 and "the name of a run is letters, digits" in plain(result)
    result = invoke(["run", str(photos), str(out), "--name", "n", "--repeat", "0"])
    assert result.exit_code == 2 and "repeat must be 1 or more" in plain(result)
    result = invoke(["run", str(photos), str(out), "--name", "n", "--steps", "some"])
    assert result.exit_code == 2 and "--steps" in plain(result)
    result = invoke(["run", str(photos), str(out), "--name", "n", "--mapper", "hierarchical"])
    assert result.exit_code == 2 and "--mapper" in plain(result) and "not 'hierarchical'" in plain(result)
    result = invoke(["run", str(photos), str(out), "--name", "n", "--engine", "turbo"])
    assert result.exit_code == 2 and "--engine" in plain(result) and "not 'turbo'" in plain(result)
    result = invoke(["synthetic", "ring", str(tmp_path / "s"), "--engine", "turbo"])
    assert result.exit_code == 2 and "--engine" in plain(result) and "not 'turbo'" in plain(result)
    # the wording of the range error is Click's and changes between versions: the exit code and the option named
    # are what matter (that --seed exists and reaches the runner is shown by test_run_passes_its_options...)
    result = invoke(["run", str(photos), str(out), "--name", "n", "--seed", "-1"])
    assert result.exit_code == 2 and "--seed" in plain(result)
    result = invoke(["run", str(photos), str(photos / "out"), "--name", "n"])
    assert result.exit_code == 2 and "must not be inside the folder of the photos" in plain(result)
    (out / "n-1").mkdir(parents=True)
    result = invoke(["run", str(photos), str(out), "--name", "n"])
    assert result.exit_code == 2 and "exists: delete it or choose another name" in plain(result)
    empty = tmp_path / "empty"
    empty.mkdir()
    result = invoke(["run", str(empty), str(tmp_path / "other"), "--name", "n"])
    assert result.exit_code == 2 and "no photos in" in plain(result)
    with pytest.raises(ValueError, match="no photos in"):
        runner.check(tmp_path / "other", "n", photos=empty)
    monkeypatch.setattr(runner, "_psutil", raising(RuntimeError(runner.PSUTIL_MISSING)))
    result = invoke(["run", str(photos), str(tmp_path / "other"), "--name", "n"])
    assert result.exit_code == 1 and "error: the bench needs psutil" in result.output


def test_run_passes_its_options_and_says_whether_each_record_is_valid(tmp_path, monkeypatch):
    pytest.importorskip("psutil")
    calls = []

    def fake_run(photos, out, name, **kwargs):
        calls.append((photos, out, name, kwargs))
        return [record.write(a_record(1.5, link=480, name=name), Path(out))]

    monkeypatch.setattr(runner, "run", fake_run)
    photos = a_photo_folder(tmp_path / "p")
    result = invoke(["run", str(photos), str(tmp_path / "o"), "--name", "n", "--steps", "all", "--repeat", "2",
                     "--keep", "--records", str(tmp_path / "r")])
    assert result.exit_code == 0, result.output
    assert calls == [(photos, tmp_path / "o", "n", {"steps": "all", "repeat": 2, "keep": True,
                                                   "records": tmp_path / "r", "mapper": "auto", "seed": None,
                                                   "engine": "auto"})]
    assert "not valid (slow disk link: 480 Mb/s)" in result.output and "1.5 s" in result.output
    result = invoke(["run", str(photos), str(tmp_path / "o2"), "--name", "n", "--mapper", "global", "--seed", "7"])
    assert result.exit_code == 0, result.output
    assert calls[1][3] == {"steps": "align", "repeat": 1, "keep": False, "records": None, "mapper": "global",
                           "seed": 7, "engine": "auto"}
    # the matching alone on the native engine: the time gate of the native matching on a large set
    result = invoke(["run", str(photos), str(tmp_path / "o3"), "--name", "n", "--steps", "match", "--engine",
                     "native"])
    assert result.exit_code == 0, result.output
    assert calls[2][3] == {"steps": "match", "repeat": 1, "keep": False, "records": None, "mapper": "auto",
                           "seed": None, "engine": "native"}


def test_what_stops_the_runner_after_it_started_is_an_error_that_prints_the_records_written(tmp_path, monkeypatch):
    pytest.importorskip("psutil")
    photos, out = a_photo_folder(tmp_path / "p"), tmp_path / "o"

    def one_record_then_a_refusal(p, o, name, **kwargs):
        record.write(a_record(1.5, name=name), Path(o))  # the record of the first repeat
        raise ValueError("the record holds a path (results.error): not written")  # the second, refused

    monkeypatch.setattr(runner, "run", one_record_then_a_refusal)
    result = invoke(["run", str(photos), str(out), "--name", "n"])
    assert result.exit_code == 1, result.output
    assert "error: the record holds a path (results.error): not written" in result.output
    assert "-1.json: 1 steps, 1.5 s, valid" in result.output
    offline = "every photo is online-only: make them available offline first or copy them to a local disk"
    for exc in (RuntimeError(offline), FileExistsError("the work folder exists")):
        monkeypatch.setattr(runner, "run", raising(exc))
        result = invoke(["run", str(photos), str(out), "--name", "n"])
        assert result.exit_code == 1 and f"error: {exc}" in result.output


def test_synthetic_takes_trench_ring_or_weak_ring(tmp_path):
    result = invoke(["synthetic", "bogus", str(tmp_path)])
    assert result.exit_code == 2 and "trench, ring or weak-ring" in result.output


def a_writer(prefix: str):
    """A stand-in for a writer of a set: one empty photo named like the set's and a gcp.csv in the folder given."""
    def write(out_dir, **kwargs):
        folder = Path(out_dir)
        (folder / "photos").mkdir(parents=True)
        (folder / "photos" / f"{prefix}_0000.jpg").write_bytes(b"")
        (folder / "gcp.csv").write_text("# label,X,Y,Z (EPSG:3004)\n", encoding="utf-8")
        return types.SimpleNamespace(crs="EPSG:3004", targets={}, cameras=[{"label": f"{prefix}_0000"}])
    return write


@pytest.fixture
def stand_ins(monkeypatch) -> list:
    """The writers of the two sets and the runner replaced: the writers put one photo of their own in the folder
    they are given, the runner notes what it is given and the photos it finds. Returned: those notes."""
    pytest.importorskip("psutil")
    calls = []

    def fake_run(photos, out, name, **kwargs):
        calls.append((photos, out, name, kwargs, sorted(p.name for p in Path(photos).iterdir())))
        return []

    monkeypatch.setattr(synthetic, "write_dataset", a_writer("IMG"))
    monkeypatch.setattr(synthetic, "write_ring_dataset", a_writer("RING"))
    monkeypatch.setattr(runner, "run", fake_run)
    return calls


def test_synthetic_writes_each_set_in_a_folder_of_its_own_and_runs_its_own_photos(tmp_path, stand_ins):
    out = tmp_path / "b"
    result = invoke(["synthetic", "trench", str(out)])
    assert result.exit_code == 0, result.output
    photos, out_, name, kwargs, seen = stand_ins[-1]
    assert (photos, out_, name) == (out / "synthetic-trench-set" / "photos", out, "synthetic-trench")
    assert seen == ["IMG_0000.jpg"]
    assert kwargs["steps"] == "all" and kwargs["repeat"] == 3 and kwargs["records"] is None
    assert "mapper" not in kwargs  # the synthetic sets keep the default mapper, auto
    assert kwargs["engine"] == "auto"  # --engine chooses the engine of the matching, auto without it
    assert kwargs["targets"] == child.DEFAULT_TARGETS == "aruco_4x4_50"
    assert kwargs["reference"] == out / "synthetic-trench-set" / "gcp.csv" and kwargs["crs"] == "EPSG:3004"
    assert kwargs["extra_results"] is truth.trench_truth
    result = invoke(["synthetic", "ring", str(out), "--repeat", "1", "--records", str(tmp_path / "rec"), "--engine",
                     "native"])
    assert result.exit_code == 0, result.output
    photos, out_, name, kwargs, seen = stand_ins[-1]
    assert (photos, out_, name) == (out / "synthetic-ring-set" / "photos", out, "synthetic-ring")
    assert seen == ["RING_0000.jpg"]  # its own photos, not the trench's
    assert kwargs["steps"] == "align" and kwargs["repeat"] == 1 and kwargs["records"] == tmp_path / "rec"
    assert kwargs["targets"] == child.DEFAULT_TARGETS and "reference" not in kwargs and "crs" not in kwargs
    assert "mapper" not in kwargs and callable(kwargs["extra_results"]) and len(stand_ins) == 2
    assert kwargs["engine"] == "native"
    assert (out / "synthetic-trench-set" / "photos" / "IMG_0000.jpg").exists()  # the set stays after the runs
    # the work folders (the projects) go unless --keep asks for them: the project of a kept run is what
    # ppg bench adjust takes
    assert all(call[3]["keep"] is False for call in stand_ins)
    result = invoke(["synthetic", "trench", str(tmp_path / "kept"), "--keep"])
    assert result.exit_code == 0, result.output
    assert stand_ins[-1][3]["keep"] is True and stand_ins[-1][2] == "synthetic-trench"


def test_adjust_takes_a_project_its_solvers_and_the_repeats(tmp_path, monkeypatch):
    """``ppg bench adjust PROJECT --name NAME``: the project, the name, the solvers (all three unless ``--solvers``
    names some), the repeats (three unless ``--repeat``) and the records folder go to ``adjust.run``; a solver it
    does not know or named twice, a repeat below 1, a bad name or a project that is not there are usage errors
    before any run."""
    pytest.importorskip("psutil")
    from pyarchinit_photogrammetry.bench import adjust

    calls = []

    def fake_run(project, name, **kwargs):
        calls.append((project, name, kwargs))
        return []

    monkeypatch.setattr(adjust, "run", fake_run)
    project = tmp_path / "kept" / "project.ppg"
    project.parent.mkdir()
    project.write_text("{}", encoding="utf-8")
    result = invoke(["adjust", str(project), "--name", "t"])
    assert result.exit_code == 0, result.output
    assert calls[-1] == (project, "t", {"solvers": adjust.SOLVERS, "repeat": 3, "records": None})
    result = invoke(["adjust", str(project), "--name", "t", "--solvers", "native,scipy", "--repeat", "1",
                     "--records", str(tmp_path / "rec")])
    assert result.exit_code == 0, result.output
    assert calls[-1] == (project, "t", {"solvers": ("native", "scipy"), "repeat": 1, "records": tmp_path / "rec"})
    result = invoke(["adjust", str(project), "--name", "t", "--solvers", "native,ceres"])
    assert result.exit_code == 2 and "native, colmap or scipy" in plain(result)
    for args in (["--solvers", "native,native"], ["--repeat", "0"]):
        assert invoke(["adjust", str(project), "--name", "t", *args]).exit_code == 2
    assert invoke(["adjust", str(project), "--name", "../t"]).exit_code == 2
    assert invoke(["adjust", str(tmp_path / "none.ppg"), "--name", "t"]).exit_code == 2
    assert len(calls) == 2


def test_a_second_synthetic_set_into_the_same_out_is_refused_before_anything_is_written(tmp_path, stand_ins):
    out = tmp_path / "b"
    assert invoke(["synthetic", "trench", str(out)]).exit_code == 0
    before = {p: p.stat().st_mtime_ns for p in (out / "synthetic-trench-set").rglob("*")}
    result = invoke(["synthetic", "trench", str(out)])
    assert result.exit_code == 2
    assert "exists (the set of an earlier run): delete it or give another OUT" in plain(result)
    assert {p: p.stat().st_mtime_ns for p in (out / "synthetic-trench-set").rglob("*")} == before
    assert len(stand_ins) == 1


def test_the_runners_checks_run_before_the_set_is_rendered(tmp_path, stand_ins, monkeypatch):
    out = tmp_path / "b"
    (out / "synthetic-ring-1").mkdir(parents=True)  # a work folder of a run that was kept
    result = invoke(["synthetic", "ring", str(out)])
    assert result.exit_code == 2 and "exists: delete it or choose another name" in plain(result)
    result = invoke(["synthetic", "trench", str(out), "--repeat", "0"])
    assert result.exit_code == 2 and "repeat must be 1 or more" in plain(result)
    result = invoke(["synthetic", "trench", str(out), "--records", str(out / "synthetic-trench-1" / "r")])
    assert result.exit_code == 2 and "must not be inside a work folder" in plain(result)
    for records in (out / "synthetic-trench-set", out / "synthetic-trench-set" / "photos" / "r"):
        result = invoke(["synthetic", "trench", str(out), "--records", str(records)])  # the set, rendered later
        assert result.exit_code == 2 and "must not be inside the folder of the set" in plain(result)
    monkeypatch.setattr(runner, "_psutil", raising(RuntimeError(runner.PSUTIL_MISSING)))
    result = invoke(["synthetic", "trench", str(out)])
    assert result.exit_code == 1 and "error: the bench needs psutil" in result.output
    assert not (out / "synthetic-trench-set").exists() and not (out / "synthetic-ring-set").exists()
    assert stand_ins == []


def test_an_os_error_of_the_checks_is_an_error_without_a_traceback_or_a_path(tmp_path, monkeypatch, stand_ins):
    secret = tmp_path / "secret"
    monkeypatch.setattr(runner, "check", raising(PermissionError(13, "Permission denied", str(secret))))
    photos = a_photo_folder(tmp_path / "p")
    commands = (["run", str(photos), str(tmp_path / "o"), "--name", "n"], ["synthetic", "trench", str(tmp_path / "b")])
    for args in commands:
        result = invoke(args)
        assert result.exit_code == 1 and isinstance(result.exception, SystemExit), result.output
        assert "error: PermissionError: Permission denied" in result.output and "secret" not in result.output
    assert stand_ins == [] and not (tmp_path / "b").exists()


# --- the pairs two runs verified --------------------------------------------------------------------------------------

def test_a_pair_is_verified_with_colmaps_least_inliers_and_a_configuration_the_mapper_takes():
    """What makes a two-view geometry a verified pair is COLMAP's: at least ``min_num_inliers`` inlier matches (a
    pair below it is DEGENERATE, its matches dropped) and a configuration other than UNDEFINED, DEGENERATE and
    WATERMARK (the ones the mapper does not use)."""
    import pycolmap

    assert pairs.MIN_INLIERS == pycolmap.TwoViewGeometryOptions().min_num_inliers == 15
    config = pycolmap.TwoViewGeometryConfiguration
    assert pairs.NOT_VERIFIED == frozenset({int(config.UNDEFINED), int(config.DEGENERATE), int(config.WATERMARK)})
    assert pairs.NOT_VERIFIED == frozenset({0, 1, 7})


def test_compare_counts_the_verified_pairs_by_photo_name_and_what_the_second_run_lost(tmp_path):
    """The two databases number their photos as their extractors finished them: pairs are compared by the names
    of the photos. Two copies of one database lose nothing; one verified row set to DEGENERATE in the second is
    one pair lost (and nothing gained), a row below 15 inliers or marked WATERMARK was no verified pair before."""
    names = ["IMG_0001.jpg", "IMG_0002.jpg", "sub dir/IMG_0003.jpg", "IMG_0004.jpg"]
    geometries = {("IMG_0001.jpg", "IMG_0002.jpg"): (40, 2), ("IMG_0001.jpg", "sub dir/IMG_0003.jpg"): (15, 3),
                  ("IMG_0002.jpg", "sub dir/IMG_0003.jpg"): (14, 2), ("IMG_0002.jpg", "IMG_0004.jpg"): (200, 7),
                  ("sub dir/IMG_0003.jpg", "IMG_0004.jpg"): (60, 4), ("IMG_0001.jpg", "IMG_0004.jpg"): (0, 1)}
    (tmp_path / "a").mkdir()
    db_a = colmap_database(tmp_path / "a" / "database.db", names, geometries)
    assert pairs.verified(db_a) == {("IMG_0001.jpg", "IMG_0002.jpg"), ("IMG_0001.jpg", "sub dir/IMG_0003.jpg"),
                                    ("IMG_0004.jpg", "sub dir/IMG_0003.jpg")}
    (tmp_path / "copy").mkdir()
    db_copy = Path(shutil.copy(db_a, tmp_path / "copy" / "database.db"))
    assert pairs.compare(db_a, db_copy) == {"verified_a": 3, "verified_b": 3, "lost": 0, "lost_percent": 0.0,
                                            "gained": 0}
    # the same pairs in a database whose photos are numbered the other way round: nothing lost
    (tmp_path / "b").mkdir()
    db_b = colmap_database(tmp_path / "b" / "database.db", names, geometries,
                           ids={name: len(names) - i for i, name in enumerate(names)})
    assert pairs.compare(db_a, db_b)["lost"] == 0 and pairs.compare(db_a, db_b)["gained"] == 0
    con = sqlite3.connect(str(db_b))
    con.execute("UPDATE two_view_geometries SET config = 1, rows = 0 WHERE rows = 60")  # a verified row, degenerate
    con.execute("UPDATE two_view_geometries SET config = 1 WHERE rows = 14")  # no verified pair before: no loss
    con.commit()
    con.close()
    assert pairs.compare(db_a, db_b) == {"verified_a": 3, "verified_b": 2, "lost": 1, "lost_percent": 33.33,
                                         "gained": 0}
    assert pairs.describe(pairs.compare(db_a, db_b)) == "verified A 3, verified B 2, lost 1 (33.33 %), gained 0"
    assert pairs.compare(db_b, db_a) == {"verified_a": 2, "verified_b": 3, "lost": 0, "lost_percent": 0.0,
                                         "gained": 1}
    # the databases are opened read-only: nothing is written to them
    before = {p: p.read_bytes() for p in (db_a, db_b)}
    pairs.compare(db_a, db_b)
    assert {p: p.read_bytes() for p in (db_a, db_b)} == before
    assert not list((tmp_path / "a").glob("database.db-*")) and not list((tmp_path / "b").glob("database.db-*"))


def test_the_pairs_command_takes_two_kept_work_folders_and_prints_numbers_only(tmp_path):
    """``ppg bench pairs A B``: a kept work folder each (``OUT/<name>-<i>`` of a run with ``--keep``: the project's
    ``database.db`` lies under it), one line of numbers; a folder with no ``database.db`` under it, or with two,
    is a usage error that says the count and never a path."""
    names = ["Scavo Nord/IMG_0001.jpg", "Scavo Nord/IMG_0002.jpg", "Scavo Nord/IMG_0003.jpg"]
    geometries = {(names[0], names[1]): (40, 2), (names[0], names[2]): (30, 2), (names[1], names[2]): (25, 2)}
    a = tmp_path / "t-1" / "project.ppg.files" / "chunk_0" / "sfm"
    b = tmp_path / "t-2" / "project.ppg.files" / "chunk_0" / "sfm"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    colmap_database(a / "database.db", names, geometries)
    colmap_database(b / "database.db", names, {**geometries, (names[1], names[2]): (12, 1)})
    result = invoke(["pairs", str(tmp_path / "t-1"), str(tmp_path / "t-2")])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["verified A 3, verified B 2, lost 1 (33.33 %), gained 0"]
    assert PAIRS_LINE.match(result.output.strip())
    assert str(tmp_path) not in result.output and "IMG_" not in result.output and "Scavo" not in result.output
    assert pairs.database(tmp_path / "t-1", "A") == a / "database.db"
    # a second database under B (a copy left by hand), none under C, a folder that is not there
    shutil.copy(a / "database.db", tmp_path / "t-2" / "database.db")
    result = invoke(["pairs", str(tmp_path / "t-1"), str(tmp_path / "t-2")])
    assert result.exit_code == 2 and "B holds 2 database.db" in plain(result) and str(tmp_path) not in result.output
    (tmp_path / "t-3").mkdir()
    result = invoke(["pairs", str(tmp_path / "t-3"), str(tmp_path / "t-1")])
    assert result.exit_code == 2 and "A holds 0 database.db" in plain(result) and str(tmp_path) not in result.output
    result = invoke(["pairs", str(tmp_path / "t-1"), str(tmp_path / "nowhere")])
    assert result.exit_code == 2 and "B is not a folder" in plain(result) and str(tmp_path) not in result.output
    with pytest.raises(ValueError, match="A holds 0 database.db"):
        pairs.database(tmp_path / "t-3", "A")


@pytest.mark.slow
def test_the_verified_pairs_of_the_trench_are_counted_in_its_database(processed):
    """On a real database of COLMAP (the session's trench): the verified pairs are pairs of the chunk's photos, there
    are as many as the matching with the mapper could use, and a database compared with itself loses nothing."""
    _, ch, _ = processed
    db = ch.data_dir / "sfm" / "database.db"
    assert db.is_file()
    verified = pairs.verified(db)
    photos = {name for _key, name in sfm.image_root_and_names(ch)[1].items()}
    assert 27 <= len(verified) <= 378 and all(a in photos and b in photos and a < b for a, b in verified)
    assert len(verified) == child.matching_results(ch)["verified_pairs"]
    assert pairs.compare(db, db) == {"verified_a": len(verified), "verified_b": len(verified), "lost": 0,
                                     "lost_percent": 0.0, "gained": 0}


# --- the truth of the synthetic sets ----------------------------------------------------------------------------------

@pytest.mark.slow
def test_the_truth_of_the_trench_is_the_split_of_the_metric_tests(processed):
    doc, ch, _ = processed
    before = np.array(ch.transform, float)
    result = truth.trench_truth(doc)
    assert set(result) == {"truth_control_rmse", "truth_check_rmse"}
    assert 0.0 < result["truth_control_rmse"] < 0.002 and 0.0 < result["truth_check_rmse"] < 0.003
    # the chunk as it was found: no check point, the fit of the chain
    assert not any(m.checkpoint for m in ch.markers) and np.array_equal(np.array(ch.transform, float), before)


def test_the_truth_of_the_trench_needs_the_fit_on_the_control_targets_and_both_groups():
    doc = ppg.Document()
    with pytest.raises(ValueError, match="no chunk"):
        truth.trench_truth(doc)
    ch = doc.add_chunk("empty")
    with pytest.raises(ValueError, match="was not made"):
        truth.trench_truth(doc)
    for code, position in enumerate(([0.0, 0.0, 0.0], [4.0, 0.0, 0.1], [4.0, 3.0, 0.0], [0.0, 3.0, 0.2])):
        ch.add_marker(f"target {code}", position=np.array(position), reference=position).code = code
    with pytest.raises(ValueError, match="targets not found"):  # four control targets placed, no check target
        truth.trench_truth(doc)
    assert not any(m.checkpoint for m in ch.markers)


def test_the_truth_of_the_ring_is_measured_in_the_chunks_system_through_its_transform(tmp_path):
    from pyproj import Transformer

    from pyarchinit_photogrammetry.core import geometry
    from pyarchinit_photogrammetry.markers.gcp import projected_crs_for

    scene = synthetic.write_ring_dataset(tmp_path / "ring", n=8, size=(320, 240))
    doc = ppg.Document()
    ch = doc.add_chunk("ring")
    with pytest.raises(ValueError, match="not georeferenced"):
        truth.ring_truth(doc, scene)
    # the system the program picks for the positions of the photos (the UTM zone of the scene) and a similarity that
    # is not the identity: the cameras and the targets stand in the internal frame behind it
    lon, lat = Transformer.from_crs(scene.crs, "EPSG:4326", always_xy=True).transform(*scene.targets[0][:2])
    ch.crs = projected_crs_for("EPSG:4326", (lon, lat))
    assert ch.crs.startswith("EPSG:326")
    to_utm = Transformer.from_crs(scene.crs, ch.crs, always_xy=True)
    angle = np.radians(30.0)
    rotation = np.array([[np.cos(angle), -np.sin(angle), 0.0], [np.sin(angle), np.cos(angle), 0.0], [0.0, 0.0, 1.0]])
    ch.transform = geometry.compose_similarity(0.5, rotation, [10.0, -20.0, 3.0])

    def in_utm(points) -> np.ndarray:
        pts = np.atleast_2d(np.asarray(points, float))
        x, y = to_utm.transform(pts[:, 0], pts[:, 1])
        return np.column_stack([x, y, pts[:, 2]])

    world = in_utm([cam["world_from_cam"][:3, 3] for cam in scene.cameras])
    for i, (cam, centre) in enumerate(zip(scene.cameras, ch.to_internal(world))):
        camera = Camera(key=i, label=cam["label"], path=str(tmp_path / "ring" / "photos" / f"{cam['label']}.jpg"),
                        sensor_key=0)
        camera.transform = np.eye(4)
        camera.transform[:3, 3] = centre
        ch.cameras.append(camera)
    codes = list(scene.targets)[:6]
    for code, position in zip(codes, ch.to_internal(in_utm([scene.targets[c] for c in codes]))):
        ch.add_marker(f"target {code}", position=position).code = code
    result = truth.ring_truth(doc, scene)
    assert set(result) == {"truth_targets", "truth_target_rmse", "truth_camera_rmse", "truth_camera_height_mean",
                           "truth_camera_height_rmse"}
    assert result["truth_targets"] == 6 and result["truth_target_rmse"] < 0.001
    assert result["truth_camera_rmse"] < 0.001 and abs(result["truth_camera_height_mean"]) < 0.001
    for camera, centre in zip(ch.cameras, ch.to_internal(world + [0.0, 0.0, 1.0])):  # every camera a metre too high
        camera.transform[:3, 3] = centre
    result = truth.ring_truth(doc, scene)
    assert result["truth_camera_rmse"] == pytest.approx(1.0, abs=0.001)
    assert result["truth_camera_height_mean"] == pytest.approx(1.0, abs=0.001)
    assert result["truth_camera_height_rmse"] == pytest.approx(1.0, abs=0.001)
    assert result["truth_target_rmse"] < 0.001
    for camera in ch.cameras:
        camera.transform = None
    with pytest.raises(ValueError, match="no aligned camera"):
        truth.ring_truth(doc, scene)
