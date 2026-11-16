"""The table of the bench (``bench/table.py``) and its commands (``ppg bench run``, ``synthetic``, ``table``): the
records of a folder summarised by run, program and step over the valid runs - a figure is a baseline with three runs -,
the records left out counted by reason, the results as medians; the commands from the command line; and what
``synthetic`` measures against the truth of its sets. The records here are made by hand, never by a run of the engine;
the truth of the trench is measured on the project of the session's synthetic trench, the truth of the ring on a chunk
whose cameras and targets are put at the true poses and positions of a small ring."""
from __future__ import annotations

import json
import logging
import types
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.bench import cli as bench_cli
from pyarchinit_photogrammetry.bench import record, runner, table
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.core.camera import Camera
from pyarchinit_photogrammetry.testing import synthetic

MACHINE = {"cores": 12, "load": 0.5, "on_battery": False, "dropbox_running": False, "adapters": []}
DISK = {"bus": "PCI-Express", "link_mbps": None, "read_mb_s": 400.0, "solid_state": True}
SET = {"photos": 28, "bytes": 1, "online_only": 0, "gps": 0}
ENGINE = {"requested": "auto", "used": "classic", "fallback": None}
TABLE_LOG = "pyarchinit_photogrammetry.bench.table"


def a_record(seconds: float, *, link=None, stages=None, peak=100.0, results=None, name="t", program=None,
             started=None, step="align_cameras", engine=ENGINE) -> dict:
    """A judged record of one step of ``seconds``; ``link`` below 5000 Mb/s makes it not valid (``slow disk link``)."""
    rec = record.new(name, dict(MACHINE), {**DISK, "link_mbps": link}, dict(SET), {})
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


def test_the_results_are_the_medians_of_the_valid_runs_and_the_table_shows_them():
    records = four_records()
    records[0]["results"]["control_rmse"] = 0.003
    records[1]["results"]["control_rmse"] = 0.001
    records[2]["results"]["error"] = "extra_results: ValueError: a word"  # not a number: no figure
    rows, left_out = table.summarise(records)
    (row,) = rows
    assert row["results"] == {"cameras": 28, "aligned": 28, "control_rmse": 0.002} and row["valid_runs"] == 3
    text = table.render(rows, left_out)
    results = text[text.index("Results"):]
    assert "| cameras | aligned | control_rmse |" in results and "| 28 | 28 | 0.002 |" in results
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
    result = CliRunner().invoke(app, ["bench", "table", str(folder)])
    assert result.exit_code == 0, result.output
    assert "12.0" in result.output and "| 3 |" in result.output and "slow disk link 1" in result.output
    empty = tmp_path / "empty"
    empty.mkdir()
    result = CliRunner().invoke(app, ["bench", "table", str(empty)])
    assert result.exit_code == 1 and "no record" in result.output


def test_run_without_a_name_exits_2():
    result = CliRunner().invoke(app, ["bench", "run", "a", "b"])
    assert result.exit_code == 2 and "--name" in result.output


def test_run_passes_its_options_and_says_whether_each_record_is_valid(tmp_path, monkeypatch):
    calls = []

    def fake_run(photos, out, name, **kwargs):
        calls.append((photos, out, name, kwargs))
        return [record.write(a_record(1.5, link=480, name=name), Path(out))]

    monkeypatch.setattr(runner, "run", fake_run)
    result = CliRunner().invoke(app, ["bench", "run", str(tmp_path / "p"), str(tmp_path / "o"), "--name", "n",
                                      "--steps", "all", "--repeat", "2", "--keep", "--records", str(tmp_path / "r")])
    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path / "p", tmp_path / "o", "n",
                      {"steps": "all", "repeat": 2, "keep": True, "records": tmp_path / "r"})]
    assert "not valid (slow disk link: 480 Mb/s)" in result.output and "1.5 s" in result.output
    result = CliRunner().invoke(app, ["bench", "run", "a", "b", "--name", "n", "--steps", "some"])
    assert result.exit_code == 2 and "--steps" in result.output
    monkeypatch.setattr(runner, "run", lambda *a, **k: (_ for _ in ()).throw(FileExistsError("the work folder")))
    result = CliRunner().invoke(app, ["bench", "run", "a", "b", "--name", "n"])
    assert result.exit_code == 1 and "error: the work folder" in result.output
    monkeypatch.setattr(runner, "run", lambda *a, **k: (_ for _ in ()).throw(ValueError("no photos")))
    assert CliRunner().invoke(app, ["bench", "run", "a", "b", "--name", "n"]).exit_code == 2


def test_synthetic_takes_trench_or_ring(tmp_path):
    result = CliRunner().invoke(app, ["bench", "synthetic", "bogus", str(tmp_path)])
    assert result.exit_code == 2 and "trench or ring" in result.output


def test_synthetic_writes_the_set_and_runs_it_under_its_name_with_its_truth(tmp_path, monkeypatch):
    calls, writes = [], []

    def fake_write(out_dir, **kwargs):
        writes.append(Path(out_dir))
        (Path(out_dir) / "photos").mkdir(parents=True)
        (Path(out_dir) / "gcp.csv").write_text("# label,X,Y,Z (EPSG:3004)\n", encoding="utf-8")
        return types.SimpleNamespace(crs="EPSG:3004", targets={}, cameras=[{"label": "IMG_0000"}])

    def fake_run(photos, out, name, **kwargs):
        calls.append((photos, out, name, kwargs))
        return []

    monkeypatch.setattr(synthetic, "write_dataset", fake_write)
    monkeypatch.setattr(synthetic, "write_ring_dataset", fake_write)
    monkeypatch.setattr(runner, "run", fake_run)
    result = CliRunner().invoke(app, ["bench", "synthetic", "trench", str(tmp_path / "t")])
    assert result.exit_code == 0, result.output
    photos, out, name, kwargs = calls[-1]
    assert (photos, out, name) == (tmp_path / "t" / "photos", tmp_path / "t", "synthetic-trench")
    assert kwargs["steps"] == "all" and kwargs["repeat"] == 3 and kwargs["records"] is None
    assert kwargs["targets"] == "aruco_4x4_50" and kwargs["reference"] == tmp_path / "t" / "gcp.csv"
    assert kwargs["crs"] == "EPSG:3004" and kwargs["extra_results"] is bench_cli.trench_truth
    result = CliRunner().invoke(app, ["bench", "synthetic", "ring", str(tmp_path / "r"), "--repeat", "1",
                                      "--records", str(tmp_path / "rec")])
    assert result.exit_code == 0, result.output
    photos, out, name, kwargs = calls[-1]
    assert (photos, out, name) == (tmp_path / "r" / "photos", tmp_path / "r", "synthetic-ring")
    assert kwargs["steps"] == "align" and kwargs["repeat"] == 1 and kwargs["records"] == tmp_path / "rec"
    assert kwargs["targets"] == "aruco_4x4_50" and "reference" not in kwargs and "crs" not in kwargs
    assert callable(kwargs["extra_results"]) and writes == [tmp_path / "t", tmp_path / "r"]


# --- the truth of the synthetic sets ----------------------------------------------------------------------------------

@pytest.mark.slow
def test_the_truth_of_the_trench_is_the_split_of_the_metric_tests(processed):
    doc, ch, _ = processed
    before = np.array(ch.transform, float)
    truth = bench_cli.trench_truth(doc)
    assert set(truth) == {"truth_control_rmse", "truth_check_rmse"}
    assert 0.0 < truth["truth_control_rmse"] < 0.002 and 0.0 < truth["truth_check_rmse"] < 0.003
    # the chunk as it was found: no check point, the fit of the chain
    assert not any(m.checkpoint for m in ch.markers) and np.array_equal(np.array(ch.transform, float), before)


def test_the_truth_of_the_ring_is_measured_against_the_true_poses_and_targets(tmp_path):
    scene = synthetic.write_ring_dataset(tmp_path / "ring", n=8, size=(320, 240))
    doc = ppg.Document()
    ch = doc.add_chunk("ring")
    with pytest.raises(ValueError, match="not georeferenced"):
        bench_cli.ring_truth(doc, scene)
    ch.crs = scene.crs
    for i, cam in enumerate(scene.cameras):
        camera = Camera(key=i, label=cam["label"], path=str(tmp_path / "ring" / "photos" / f"{cam['label']}.jpg"),
                        sensor_key=0)
        camera.transform = np.array(cam["world_from_cam"], float)
        ch.cameras.append(camera)
    for code, xyz in list(scene.targets.items())[:6]:
        ch.add_marker(f"target {code}", position=np.asarray(xyz, float)).code = code
    truth = bench_cli.ring_truth(doc, scene)
    assert set(truth) == {"truth_targets", "truth_target_rmse", "truth_camera_rmse", "truth_camera_height_mean",
                          "truth_camera_height_rmse"}
    assert truth["truth_targets"] == 6 and truth["truth_target_rmse"] < 0.001 and truth["truth_camera_rmse"] < 0.001
    for camera in ch.cameras:
        camera.transform[2, 3] += 1.0  # every camera a metre too high
    truth = bench_cli.ring_truth(doc, scene)
    assert truth["truth_camera_rmse"] == pytest.approx(1.0, abs=0.001)
    assert truth["truth_camera_height_mean"] == pytest.approx(1.0, abs=0.001)
    assert truth["truth_camera_height_rmse"] == pytest.approx(1.0, abs=0.001)
    assert truth["truth_target_rmse"] < 0.001
    for camera in ch.cameras:
        camera.transform = None
    with pytest.raises(ValueError, match="no aligned camera"):
        bench_cli.ring_truth(doc, scene)
