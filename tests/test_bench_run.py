"""The bench runs the steps of a set of photos in a child process and writes one record per run that says whether the
measurement is valid and why: a slow disk link, a slow disk, photos that are online-only, a failure. The records may be
shared, so the tests look for paths, photo names, coordinates and a CRS in them. The runs below use the smallest
synthetic set the alignment tests align (the first 14 photos of the trench), photos of random noise, or a stand-in
child; never a real photo. The tests that start a child need psutil (the ``bench`` extra) and skip without it."""
from __future__ import annotations

import inspect
import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry import _native, cli
from pyarchinit_photogrammetry.bench import machine, record, runner, steps
from pyarchinit_photogrammetry.core.chunk import Chunk

# the only reasons a record is not valid for, and the name of its file
REASONS = (r"slow disk link: \d+ Mb/s", r"slow disk: \d+ MB/s", r"online-only photos: \d+", r"failed: .+")
FILE_NAME = re.compile(r"^t-\d{14}-\d+\.json$")
STEP_KEYS = {"step", "seconds", "peak_memory_mb", "stages", "engine"}
SETTINGS = ["pairs", "matcher", "quality", "gpu", "gpu_pairs", "cpu_pairs", "mapper", "global_fallback", "max_ratio",
            "max_distance", "cross_check", "pycolmap", "photo_cache_mb", "georeferencing"]

# A stand-in for the child: reads the job, writes a result and ends. ``done`` writes two finished steps and a project
# for extra_results; ``dies`` writes a step that has started and ends with code 9, as a child that crashed would.
STAND_IN = '''
import json, os, sys, time
from pathlib import Path
job = json.loads(Path(sys.argv[1]).read_text())
work, mode, now = Path(job["work"]), sys.argv[2], time.time()
def entry(step, done=True):
    e = {"step": step, "start": now, "end": None, "seconds": None, "peak_memory_mb": 40.0, "stages": {}, "engine": None}
    if done:
        e.update(end=now + 0.01, seconds=0.01)
    return e
result = work / "result.json"
if mode == "dies":
    result.write_text(json.dumps({"steps": [entry("match_photos", done=False)], "settings": {}, "results": {},
                                  "error": None, "done": False}))
    os._exit(9)
import pyarchinit_photogrammetry as ppg
doc = ppg.Document()
doc.add_chunk("Chunk 1")
doc.save(work / "project.ppg")
result.write_text(json.dumps({"steps": [entry("match_photos"), entry("align_cameras")],
                              "settings": {"quality": "high", "pycolmap": "0.0"},
                              "results": {"cameras": len(job["photos"]), "aligned": len(job["photos"])},
                              "error": None, "done": True}))
'''


def rec(**disk):
    base = {"bus": "USB", "link_mbps": 10000, "read_mb_s": 400.0, "solid_state": True}
    m = {"cores": 12, "load": 0.5, "on_battery": False, "dropbox_running": False, "adapters": []}
    return record.new("t", m, {**base, **disk}, {"photos": 9, "bytes": 1, "online_only": 0, "gps": 9}, {})


def the_record(out: Path, paths: list[Path]) -> dict:
    """The one record a run wrote, read back; the file is named by the run, its start and its index."""
    assert len(paths) == 1 and paths[0].parent == out and FILE_NAME.match(paths[0].name)
    assert [p.name for p in out.glob("*.json")] == [paths[0].name]
    return json.loads(paths[0].read_text(encoding="utf-8"))


def child_only(popen):
    """``popen`` in the place of ``subprocess.Popen`` for the child of the bench alone (``python -m ...``): the
    system tools machine.py starts through ``subprocess.run`` keep the real one."""
    real = subprocess.Popen

    def wrapper(cmd, **kwargs):
        if cmd[1:2] == ["-m"]:
            return popen(cmd, **kwargs)
        return real(cmd, **kwargs)

    return wrapper


def no_child(cmd, **kwargs):
    pytest.fail("no child must start")


def noise_photos(folder: Path, n: int = 3) -> Path:
    """``n`` photos of random noise, 320 x 240: nothing to match, nothing to align."""
    import cv2

    rng = np.random.default_rng(0)
    folder.mkdir(parents=True)
    for i in range(n):
        cv2.imwrite(str(folder / f"NOISE_{i:02d}.jpg"), rng.integers(0, 256, (240, 320, 3), dtype=np.uint8))
    return folder


@pytest.fixture
def small_set(scene_dir, tmp_path) -> Path:
    """The smallest set the alignment tests align: the first 14 photos of the trench, in a folder of their own."""
    pytest.importorskip("psutil")
    photos = tmp_path / "photos"
    photos.mkdir()
    for p in sorted((scene_dir / "photos").glob("*.jpg"))[:14]:
        shutil.copy(p, photos / p.name)
    return photos


@pytest.fixture
def stand_in(monkeypatch):
    """The child replaced by the stand-in above (``mode`` chooses what it does); the command and the keywords the
    runner started it with are kept in ``calls``."""
    pytest.importorskip("psutil")
    real = subprocess.Popen
    kept = types.SimpleNamespace(calls=[], mode="done")

    def popen(cmd, **kwargs):
        kept.calls.append((cmd, kwargs))
        return real([sys.executable, "-c", STAND_IN, cmd[-1], kept.mode], **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", child_only(popen))
    return kept


# --- the record ------------------------------------------------------------------------------------------------------

def test_a_new_record_has_exactly_the_keys_of_the_schema():
    r = rec()
    assert list(r) == list(record.KEYS) == ["schema", "name", "started", "program", "machine", "disk", "set",
                                             "settings", "steps", "results", "warnings", "valid", "invalid_reasons",
                                             "error"]
    assert r["schema"] == record.SCHEMA == 1 and r["name"] == "t" and r["program"] == ppg.__version__
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", r["started"])  # UTC, ISO 8601, to the second
    assert r["steps"] == [] and r["results"] == {} and r["warnings"] == [] and r["invalid_reasons"] == []
    assert r["valid"] is None and r["error"] is None  # not judged yet
    with pytest.raises(ValueError):
        record.new("../t", {}, {}, {}, {})


def test_a_slow_link_or_a_slow_disk_makes_the_record_not_valid():
    r = record.judge(rec(link_mbps=480, read_mb_s=32.0))
    assert r["valid"] is False and "480" in r["invalid_reasons"][0] and "32" in r["invalid_reasons"][1]
    assert r["invalid_reasons"] == ["slow disk link: 480 Mb/s", "slow disk: 32 MB/s"]


def test_an_unknown_link_and_a_running_dropbox_only_warn():
    r = rec(link_mbps=None); r["machine"]["dropbox_running"] = True
    r = record.judge(r)
    assert r["valid"] is True and r["invalid_reasons"] == [] and len(r["warnings"]) == 1


def test_online_only_photos_make_the_record_not_valid():
    r = rec(); r["set"]["online_only"] = 2
    assert record.judge(r)["valid"] is False
    assert r["invalid_reasons"] == ["online-only photos: 2"]


def test_an_error_makes_the_record_not_valid_and_the_reason_starts_with_failed():
    r = rec(); r["error"] = "align_cameras: RuntimeError: alignment failed"
    assert record.judge(r)["valid"] is False and r["invalid_reasons"] == ["failed: align_cameras: RuntimeError: "
                                                                            "alignment failed"]
    r = rec(); r["results"]["error"] = "extra_results: KeyError: 'truth'"
    assert record.judge(r)["valid"] is False and r["invalid_reasons"] == ["failed: extra_results: KeyError: 'truth'"]


def test_a_warning_needs_a_fact_that_is_known():
    r = rec(); r["machine"].update(load=None, on_battery=None, dropbox_running=None)
    assert record.judge(r)["warnings"] == [] and r["valid"] is True
    r = rec(); r["machine"].update(load=2.5, on_battery=True, dropbox_running=True)
    assert record.judge(r)["valid"] is True and len(r["warnings"]) == 3
    assert any("2.5" in w for w in r["warnings"])
    r = rec(); r["machine"]["load"] = 2.0  # at the threshold: no warning
    assert record.judge(r)["warnings"] == []


def test_a_record_that_holds_a_path_is_refused(tmp_path):
    r = rec(); r["settings"]["where"] = str(tmp_path / "photos")
    with pytest.raises(ValueError):
        record.write(r, tmp_path, private=(tmp_path,))


@pytest.mark.parametrize("text", ["/data/x", "~/photos", "~user", "C:\\photos", "d:/photos",
                                  "the photos of /private/zone/photos/a.jpg"])
def test_every_kind_of_path_is_refused_in_a_value_or_a_key(tmp_path, text):
    private = (Path("/private/zone"),)
    r = rec(); r["results"]["where"] = text
    with pytest.raises(ValueError):
        record.write(r, tmp_path, private=private)
    r = rec(); r["settings"][text] = 1
    with pytest.raises(ValueError):
        record.write(r, tmp_path, private=private)
    assert list(tmp_path.glob("*.json")) == []


def test_the_file_is_named_by_the_run_its_start_and_its_index_and_is_never_overwritten(tmp_path):
    r = record.judge(rec())
    first = record.write(r, tmp_path / "records", private=(tmp_path,))
    assert first.parent == tmp_path / "records" and FILE_NAME.match(first.name) and first.name.endswith("-1.json")
    assert first.name == f"t-{re.sub(r'[^0-9]', '', r['started'])}-1.json"
    assert json.loads(first.read_text(encoding="utf-8")) == r
    with pytest.raises(FileExistsError):
        record.write(r, tmp_path / "records", private=(tmp_path,))
    second = record.write(r, tmp_path / "records", private=(tmp_path,), index=2)
    assert second.name.endswith("-2.json") and sorted(p.name for p in (tmp_path / "records").iterdir()) == \
        sorted([first.name, second.name])
    r["name"] = "../t"
    with pytest.raises(ValueError):
        record.write(r, tmp_path / "records", private=(tmp_path,), index=3)


# --- the scrub of an error text ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("no such file: '/data/survey/photos'", "no such file: '<path>'"),  # a path of private
    ("database /other/place/sfm/database.db is locked", "database <path> is locked"),  # another absolute path
    ("cannot read ~/photos/IMG_0007.jpg", "cannot read <path>"),  # a home path, whole
    ("cannot read C:\\survey\\photos\\IMG_0007.jpg", "cannot read <path>"),  # a drive letter
    ("IMG_0007.jpg has no EXIF", "<photo> has no EXIF"),  # a photo name
    ("image IMG_0007 not registered", "image <photo> not registered"),  # the stem of a photo
    ("cannot transform to epsg:32637", "cannot transform to <crs>"),  # an EPSG code, case ignored
    ("point at 2480123.456 4500321.1", "point at <number> <number>"),  # a coordinate in metres
    ("at 12.345678 -0.123456", "at <number> -<number>"),  # a coordinate in degrees (the sign is no digit)
    ("alignment failed: no reconstruction could be initialised", "alignment failed: no reconstruction could be "
                                                                   "initialised"),
    ("3 photos, 12.5 px, 320 x 240, 0.001 m", "3 photos, 12.5 px, 320 x 240, 0.001 m"),  # short numbers stay
], ids=["private", "absolute", "home", "drive", "name", "stem", "epsg", "metres", "degrees", "plain", "numbers"])
def test_scrub_replaces_what_a_record_must_not_hold(text, expected):
    assert record.scrub(text, private=(Path("/data/survey/photos"),), names=("IMG_0007.jpg",)) == expected


def test_scrub_takes_the_private_paths_longest_first_and_then_the_photo():
    private = (Path("/data/survey"), Path("/data/survey/photos"))
    out = record.scrub("'/data/survey/photos/IMG_0007.jpg' in /data/survey", private, names=("IMG_0007.jpg",))
    assert out == "'<path>/<photo>' in <path>"


# --- the chain of the child -------------------------------------------------------------------------------------------

def test_the_chain_of_all_is_the_one_of_ppg_process():
    calls = re.findall(r"\bch\.(\w+)\(", inspect.getsource(cli.process))
    assert calls[0] == "add_photos" and calls[-1] == "generate_report"
    assert calls[1:-1] == list(steps.ALL_STEPS) == ["detect_markers", "match_photos", "align_cameras",
                                                     "import_reference", "update_transform", "build_depth_maps",
                                                     "build_point_cloud", "build_model", "build_texture", "build_dem",
                                                     "build_orthomosaic"]
    assert steps.ALL_STEPS[:len(steps.ALIGN_STEPS)] == steps.ALIGN_STEPS
    assert steps.CHAINS == {"align": steps.ALIGN_STEPS, "all": steps.ALL_STEPS}


def test_the_arguments_of_the_steps_are_the_defaults_of_today():
    for step, arguments in steps.ARGUMENTS.items():
        params = inspect.signature(getattr(Chunk, step)).parameters
        for key, value in arguments.items():
            assert params[key].default == value, (step, key)
    assert steps.ARGUMENTS["match_photos"] == {"quality": "high", "keypoint_limit": 40000, "tiepoint_limit": 8000,
                                               "pair_selection": "auto"}
    process = inspect.signature(cli.process).parameters
    assert process["quality"].default == steps.ARGUMENTS["match_photos"]["quality"]
    assert process["dense_quality"].default == steps.ARGUMENTS["build_depth_maps"]["quality"]
    assert process["surface"].default.default == steps.ARGUMENTS["build_model"]["surface"]  # a typer option
    assert set(steps.ARGUMENTS) == set(steps.ALL_STEPS) - {"detect_markers", "import_reference"}
    # the three settings of the matcher are read from the signature of the native matcher, never written out; today
    # they are 0.8, 0.7 and True (_native.py): a change there is a change of what the bench measures
    params = inspect.signature(_native.gpu_match).parameters
    assert steps.match_settings() == {k: params[k].default for k in ("max_ratio", "max_distance", "cross_check")}
    assert steps.match_settings() == {"max_ratio": 0.8, "max_distance": 0.7, "cross_check": True}


def test_the_reprojection_rms_lives_in_the_package_and_the_script_imports_it():
    script = Path(__file__).resolve().parents[1] / "benchmark" / "bench_ppg.py"
    text = script.read_text(encoding="utf-8")
    assert "from pyarchinit_photogrammetry.bench.steps import reprojection_rms" in text
    assert "def reprojection_rms" not in text and inspect.isfunction(steps.reprojection_rms)


def test_photo_cache_mb_is_the_variable_as_a_whole_number_or_none():
    assert runner.photo_cache_mb({}) is None
    assert runner.photo_cache_mb({"PPG_PHOTO_CACHE_MB": "512"}) == 512
    assert runner.photo_cache_mb({"PPG_PHOTO_CACHE_MB": " 0 "}) == 0
    assert runner.photo_cache_mb({"PPG_PHOTO_CACHE_MB": "lots"}) is None
    assert runner.photo_cache_mb({"PPG_PHOTO_CACHE_MB": "1.5"}) is None


# --- the run: what is refused before a child starts -------------------------------------------------------------------

def test_a_work_folder_inside_the_photos_a_bad_name_or_unknown_steps_are_refused(tmp_path, monkeypatch):
    photos = noise_photos(tmp_path / "photos")
    monkeypatch.setattr(runner.subprocess, "Popen", child_only(no_child))
    with pytest.raises(ValueError):
        runner.run(photos, photos / "out", name="t")
    with pytest.raises(ValueError):
        runner.run(photos, tmp_path / "out", name="../t")
    with pytest.raises(ValueError):
        runner.run(photos, tmp_path / "out", name="t", steps="dense")
    with pytest.raises(ValueError):
        runner.run(photos, tmp_path / "out", name="t", repeat=0)
    assert not (tmp_path / "out").exists()


def test_without_psutil_the_run_names_the_extra(tmp_path, monkeypatch):
    photos = noise_photos(tmp_path / "photos")
    monkeypatch.setitem(sys.modules, "psutil", None)  # an import raises ImportError
    monkeypatch.setattr(runner.subprocess, "Popen", child_only(no_child))
    with pytest.raises(RuntimeError, match=r'pip install "pyarchinit-photogrammetry\[bench\]"'):
        runner.run(photos, tmp_path / "out", name="t")


def test_every_photo_online_only_starts_no_child(tmp_path, monkeypatch):
    pytest.importorskip("psutil")
    photos = noise_photos(tmp_path / "photos")
    monkeypatch.setattr(machine, "is_online_only", lambda path: True)
    monkeypatch.setattr(runner.subprocess, "Popen", child_only(no_child))
    with pytest.raises(RuntimeError, match="online-only"):
        runner.run(photos, tmp_path / "out", name="t")
    assert not (tmp_path / "out").exists()


# --- the run on the small set -----------------------------------------------------------------------------------------

def test_a_small_run_writes_one_record(small_set, tmp_path):
    out = tmp_path / "out"
    paths = runner.run(small_set, out, name="t", extra_results=lambda doc: {"chunks": len(doc.chunks)})
    r = the_record(out, paths)
    assert list(r) == list(record.KEYS) and r["schema"] == 1 and r["name"] == "t"
    assert [s["step"] for s in r["steps"]] == ["match_photos", "align_cameras"]
    for s in r["steps"]:
        assert set(s) == STEP_KEYS and s["seconds"] > 0 and s["peak_memory_mb"] > 0
        assert s["engine"] == {"requested": "auto", "used": "classic", "fallback": None}
    assert {"features", "pairs"} <= set(r["steps"][0]["stages"]) and isinstance(r["steps"][1]["stages"], dict)
    assert r["set"] == {"photos": 14, "bytes": sum(p.stat().st_size for p in small_set.iterdir()), "online_only": 0,
                        "gps": 0}
    assert r["results"]["aligned"] == r["set"]["photos"] == r["results"]["cameras"] == 14
    assert r["results"]["reprojection_rms_px"] > 0 and r["results"]["chunks"] == 1
    assert list(r["settings"]) == SETTINGS
    assert r["settings"]["quality"] == "high" and r["settings"]["pairs"] > 0 and r["settings"]["mapper"]
    assert r["settings"]["max_ratio"] == 0.8 and r["settings"]["cross_check"] is True
    assert isinstance(r["settings"]["pycolmap"], str) and r["settings"]["pycolmap"]
    assert r["settings"]["photo_cache_mb"] is None and r["settings"]["georeferencing"] is None
    assert "image_root" not in r["settings"] and "names" not in r["settings"]
    assert isinstance(r["machine"]["adapters"], list) and "read_mb_s" in r["disk"]
    assert r["valid"] in (True, False) and r["error"] is None
    assert all(any(re.fullmatch(p, reason) for p in REASONS) for reason in r["invalid_reasons"])
    assert r["valid"] == (r["invalid_reasons"] == [])
    text = paths[0].read_text(encoding="utf-8")
    assert str(tmp_path) not in text and not any(p.name in text or p.stem in text for p in small_set.iterdir())
    assert not (out / "t-1").exists()


def test_a_failed_run_leaves_a_record_with_the_error(tmp_path):
    pytest.importorskip("psutil")
    photos = noise_photos(tmp_path / "photos")
    out = tmp_path / "out"
    paths = runner.run(photos, out, name="t")
    r = the_record(out, paths)
    assert r["error"] and r["valid"] is False
    assert any(reason.startswith("failed") for reason in r["invalid_reasons"])
    assert r["invalid_reasons"][-1] == f"failed: {r['error']}"
    assert all(s["seconds"] > 0 and s["peak_memory_mb"] > 0 for s in r["steps"])
    last = r["steps"][-1]
    assert last["step"] in ("align_cameras", "match_photos") and last["error"] == r["error"]
    assert last["error"].startswith(f"{last['step']}: ")
    assert all("error" not in s for s in r["steps"][:-1])  # the steps before it are complete
    assert [s["step"] for s in r["steps"]] == ["match_photos", "align_cameras"][:len(r["steps"])]
    assert r["results"].get("aligned", 0) < 3 and r["set"]["photos"] == 3
    text = paths[0].read_text(encoding="utf-8")
    assert str(tmp_path) not in text and "NOISE_" not in text and "/" not in r["error"]
    assert not (out / "t-1").exists()


def test_a_cancelled_run_leaves_a_record_and_no_work_folder(small_set, tmp_path, monkeypatch):
    started = []
    real = subprocess.Popen

    def popen(cmd, **kwargs):
        proc = real(cmd, **kwargs)
        started.append(proc)
        return proc

    samples = []

    def sample(pid, result, peaks):
        samples.append(pid)
        if len(samples) == 2:  # the child runs: the user presses Ctrl-C
            raise KeyboardInterrupt

    monkeypatch.setattr(runner.subprocess, "Popen", child_only(popen))
    monkeypatch.setattr(runner, "_sample", sample)
    out = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        runner.run(small_set, out, name="t")
    assert len(started) == 1 and started[0].poll() is not None  # terminated
    r = the_record(out, list(out.glob("*.json")))
    assert r["error"] == "cancelled" and r["valid"] is False and r["invalid_reasons"] == ["failed: cancelled"]
    assert not (out / "t-1").exists()


def test_a_slow_link_marks_the_run_and_does_not_stop_it(small_set, tmp_path, monkeypatch):
    monkeypatch.setattr(machine, "disk_facts", lambda path: {"bus": "USB", "link_mbps": 480, "solid_state": True})
    out = tmp_path / "out"
    r = the_record(out, runner.run(small_set, out, name="t"))
    assert r["valid"] is False and "slow disk link: 480 Mb/s" in r["invalid_reasons"]
    assert [s["step"] for s in r["steps"]] == ["match_photos", "align_cameras"] and r["error"] is None
    assert r["disk"]["link_mbps"] == 480 and r["disk"]["bus"] == "USB" and "read_mb_s" in r["disk"]


def test_a_run_without_a_gpu_ends_and_says_so(small_set, tmp_path, monkeypatch):
    monkeypatch.setattr(machine._native, "gpu_adapters", lambda: [])
    out = tmp_path / "out"
    r = the_record(out, runner.run(small_set, out, name="t"))
    assert r["machine"]["adapters"] == [] and [s["step"] for s in r["steps"]] == ["match_photos", "align_cameras"]
    assert r["results"]["aligned"] == 14


# --- the run with a stand-in child ------------------------------------------------------------------------------------

def test_a_photo_online_only_among_three_is_left_out_and_said(tmp_path, monkeypatch, stand_in):
    photos = noise_photos(tmp_path / "photos")
    monkeypatch.setattr(machine, "is_online_only", lambda path: Path(path).name == "NOISE_01.jpg")
    given = []
    monkeypatch.setattr(machine, "read_rate", lambda files, *a, **k: given.extend(files) or 250.0)
    out = tmp_path / "out"
    paths = runner.run(photos, out, name="t", keep=True)
    r = the_record(out, paths)
    assert r["valid"] is False and r["invalid_reasons"] == ["online-only photos: 1"]
    assert r["set"] == {"photos": 3, "bytes": (photos / "NOISE_00.jpg").stat().st_size +
                        (photos / "NOISE_02.jpg").stat().st_size, "online_only": 1, "gps": 0}
    assert r["disk"]["read_mb_s"] == 250.0 and [p.name for p in given] == ["NOISE_00.jpg", "NOISE_02.jpg"]
    job = json.loads((out / "t-1" / "job.json").read_text(encoding="utf-8"))  # kept
    assert [Path(p).name for p in job["photos"]] == ["NOISE_00.jpg", "NOISE_02.jpg"] and job["steps"] == "align"
    assert job["targets"] is None and job["reference"] is None and job["crs"] is None
    (cmd, kwargs), = stand_in.calls
    assert cmd[:3] == [sys.executable, "-m", "pyarchinit_photogrammetry.bench.steps"] and cmd[3] == str(out / "t-1" /
                                                                                                          "job.json")
    assert kwargs["close_fds"] is False and kwargs["env"]["GLOG_logbufsecs"] == "0" and "cwd" not in kwargs
    assert r["results"] == {"cameras": 2, "aligned": 2} and r["settings"]["quality"] == "high"


def test_extra_results_that_raise_are_recorded_and_the_times_are_kept(tmp_path, stand_in):
    photos = noise_photos(tmp_path / "photos")
    out = tmp_path / "out"

    def truth(doc):
        raise KeyError(str(tmp_path / "truth.csv"))

    r = the_record(out, runner.run(photos, out, name="t", extra_results=truth))
    assert r["valid"] is False and r["error"] is None
    assert r["results"]["error"].startswith("extra_results: KeyError: ") and str(tmp_path) not in r["results"]["error"]
    assert r["invalid_reasons"] == [f"failed: {r['results']['error']}"]
    assert [s["step"] for s in r["steps"]] == ["match_photos", "align_cameras"] and r["results"]["cameras"] == 3
    r = the_record(tmp_path / "words", runner.run(photos, tmp_path / "words", name="t",
                                                  extra_results=lambda doc: {"label": "a word"}))
    assert r["results"]["error"].startswith("extra_results: TypeError: ") and "label" in r["results"]["error"]


def test_a_child_that_dies_leaves_the_step_it_died_in(tmp_path, stand_in):
    stand_in.mode = "dies"
    photos = noise_photos(tmp_path / "photos")
    out = tmp_path / "out"
    r = the_record(out, runner.run(photos, out, name="t"))
    assert r["error"] == "match_photos: child ended with code 9" and r["valid"] is False
    assert [s["step"] for s in r["steps"]] == ["match_photos"]
    assert r["steps"][0]["error"] == r["error"] and r["steps"][0]["seconds"] > 0
    assert not (out / "t-1").exists()
