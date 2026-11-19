"""The bench runs the steps of a set of photos in a child process and writes one record per run that says whether the
measurement is valid and why: a slow disk link, a slow disk, photos that are online-only, a failure. The records may be
shared, so the tests look for paths, photo names, coordinates and a CRS in them. The runs below use the smallest
synthetic set the alignment tests align (the first 14 photos of the trench), photos of random noise, or a stand-in
child; never a real photo. The tests that start a child need psutil (the ``bench`` extra) and skip without it."""
from __future__ import annotations

import inspect
import json
import logging
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path, PureWindowsPath

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
            "max_distance", "cross_check", "pycolmap", "photo_cache_mb", "georeferencing", "targets",
            "mapper_requested", "ransac_seed"]

# Made-up paths for the texts of real exceptions: a home, a photo folder with spaces and non-ASCII letters under a
# made-up root (never the mount point of the external disks: a private path is resolved, and no test makes the
# system look there), a work folder, and a home on Windows whose user name has two words
HOME = "/Users/fieldteam"
PHOTOS = "/srv/field-unit/Scavo Nord/Données été"
OUT = "/srv/field-unit/bench-work"
WIN_HOME = r"C:\Users\Field Team"
WIN_FILE = WIN_HOME + r"\Pictures\scavo 2024\IMG_0001.JPG"
PRIVATE = (Path(PHOTOS), Path(OUT), Path(HOME), WIN_HOME)
ERRNO_2 = "FileNotFoundError: [Errno 2] No such file or directory: "
# the texts real exceptions give (open(), pycolmap.Reconstruction, Chunk.import_reference; on Windows Python's own
# OSError text, which doubles the backslashes, and the repr of a Path, which writes slashes), each with what the
# scrub must leave of it
REAL_TEXTS = [
    pytest.param(ERRNO_2 + "'" + PHOTOS + "/IMG_0001.JPG'", ERRNO_2 + "'<path>'", id="open-private"),
    pytest.param(ERRNO_2 + "'/srv/other-unit/Site Alpha GCP/gcp.csv'", ERRNO_2 + "'<path>'", id="open-outside"),
    pytest.param(ERRNO_2 + "'" + HOME + "/Scavi/Site Alpha 2024/gcp.csv'", ERRNO_2 + "'<path>'", id="open-home"),
    pytest.param("OSError: cannot read " + HOME + "lovelace/notes/x.txt", "OSError: cannot read <path>",
                 id="prefix-of-home"),
    pytest.param('ValueError: [reconstruction.cc:995] rigs, cameras, frames, images, points3D files do not exist '
                 'at "' + PHOTOS + '/sparse 0"', "ValueError: [reconstruction.cc:995] rigs, cameras, frames, images, "
                 "points3D files do not exist at '<path>'", id="pycolmap"),
    pytest.param(ERRNO_2 + "'/srv/other-unit/Site Alpha GCP/gcp.csv'", ERRNO_2 + "'<path>'",
                 id="import-reference"),  # the same text as open-outside: Chunk.import_reference opens the file
    pytest.param("FileNotFoundError: " + str(FileNotFoundError(2, "No such file or directory", WIN_FILE)),
                 ERRNO_2 + "'<path>'", id="windows-oserror"),
    pytest.param(f"ValueError: no EXIF in {PureWindowsPath(WIN_FILE)!r}",
                 "ValueError: no EXIF in PureWindowsPath('<path>')", id="windows-repr"),
]
# a photo folder with an apostrophe, as Italian place names have, and a photo of the set with one; the texts of the
# re-review of the fix, with what the scrub must leave of them
POGGIO = "/srv/field-unit/Poggio d'Alto/foto"
NAMES = ("IMG_0001.JPG", "Sant'Elmo_001.jpg")
APOSTROPHE_TEXTS = [
    pytest.param("RuntimeError: can't read " + POGGIO + "/IMG_0001.JPG", "RuntimeError: can't read <path>",
                 id="cant-read"),
    pytest.param("ValueError: 'IMG_0001.JPG' not found in " + POGGIO, "ValueError: '<photo>' not found in <path>",
                 id="quoted-photo"),
    pytest.param("ValueError: can't open \"" + POGGIO + "/IMG_0001.JPG\"", "ValueError: can't open '<path>'",
                 id="cant-open"),
    pytest.param("RuntimeError: cannot read " + PHOTOS + "/Sant'Elmo_001.jpg", "RuntimeError: cannot read <path>",
                 id="photo-apostrophe"),
    pytest.param("RuntimeError: cannot read " + HOME + "/Scavi/Poggio d'Alto/gcp.csv",
                 "RuntimeError: cannot read <path>", id="home-apostrophe"),
]

# A stand-in for the child: reads the job, writes a result and ends. ``done`` writes two finished steps, the mapper it
# used (incremental, whatever the job asked for: a global alignment that fell back) and a project for extra_results;
# ``dies`` writes a step that has started and ends with code 9, as a child that crashed would;
# ``reference`` fails in import_reference with the path of the reference in its text; ``leaks`` writes a path into
# the settings, as a child whose text slipped past the scrub would.
STAND_IN = '''
import json, os, sys, time
from pathlib import Path
job = json.loads(Path(sys.argv[1]).read_text())
work, mode, now = Path(job["work"]), sys.argv[2], time.time()
def entry(step, done=True, error=None):
    e = {"step": step, "start": now, "end": None, "seconds": None, "peak_memory_mb": 40.0, "stages": {}, "engine": None}
    if done:
        e.update(end=now + 0.01, seconds=0.01)
    if error:
        e["error"] = error
    return e
def write(steps, settings=None, results=None, error=None, done=True):
    (work / "result.json").write_text(json.dumps({"steps": steps, "settings": settings or {},
                                                  "results": results or {}, "error": error, "done": done}))
if mode == "dies":
    write([entry("match_photos", done=False)], done=False)
    os._exit(9)
if mode == "reference":
    error = "import_reference: ValueError: cannot read " + str(job["reference"])
    write([entry("match_photos"), entry("align_cameras"), entry("import_reference", error=error)], error=error,
          done=False)
    sys.exit(1)
import pyarchinit_photogrammetry as ppg
doc = ppg.Document()
doc.add_chunk("Chunk 1")
doc.save(work / "project.ppg")
write([entry("match_photos"), entry("align_cameras")],
      {"quality": "/leak/y" if mode == "leaks" else "high", "pycolmap": "0.0", "mapper": "incremental"},
      {"cameras": len(job["photos"]), "aligned": len(job["photos"])})
'''
RUNNER_LOG = "pyarchinit_photogrammetry.bench.runner"


def rec(**disk):
    base = {"bus": "USB", "link_mbps": 10000, "read_mb_s": 400.0, "solid_state": True}
    m = {"cores": 12, "load": 0.5, "on_battery": False, "dropbox_running": False, "adapters": []}
    return record.new("t", m, {**base, **disk}, {"photos": 9, "bytes": 1, "online_only": 0, "gps": 9}, {})


def the_record(out: Path, paths: list[Path]) -> dict:
    """The one record a run wrote, read back; the file is named by the run, its start and its index."""
    assert len(paths) == 1 and paths[0].parent == out and FILE_NAME.match(paths[0].name)
    assert [p.name for p in out.glob("*.json")] == [paths[0].name]
    return json.loads(paths[0].read_text(encoding="utf-8"))


def strings_of(value, where: str = "record"):
    """Every string of a record read back, keys included, with where it sits (the JSON text would double the
    backslashes and escape the non-ASCII letters of a path)."""
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings_of(key, where + ".<key>")
            yield from strings_of(item, f"{where}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from strings_of(item, f"{where}[{i}]")


def said_by_name(caplog, folder: str, tmp_path: Path) -> bool:
    """Whether a log line of the runner names ``folder`` and no line that names it holds a path."""
    lines = [entry.getMessage() for entry in caplog.records if folder in entry.getMessage()]
    return bool(lines) and not any(str(tmp_path) in line or "/" in line or "\\" in line for line in lines)


def stub_chunk(calls: list, saves: list, gps: bool = False, scalebars: bool = False, report=None):
    """A chunk whose methods only note their call: the chain of the child without an engine. ``report`` is what
    ``update_transform`` returns."""
    camera = types.SimpleNamespace(reference=types.SimpleNamespace(location=[1.0, 2.0, 3.0] if gps else None),
                                   aligned=True)
    chunk = types.SimpleNamespace(meta={}, cameras=[camera], aligned_cameras=[camera],
                                  scalebars=[object()] if scalebars else [], product_path=lambda name: None,
                                  document=types.SimpleNamespace(save=lambda: saves.append(1)))

    def method(step):
        def run(*args, **kwargs):
            calls.append((step, args, kwargs))
            return report if step == "update_transform" else None
        return run

    for step in steps.ALL_STEPS:
        setattr(chunk, step, method(step))
    return chunk


REPORT = {"method": "markers", "crs": "EPSG:3004", "control_rmse": 0.002, "check_rmse": None, "scale": 1.0,
          "markers": [{"label": "A", "type": "control", "error": 0.1}, {"label": "B", "type": "check", "error": None}],
          "scalebars": [], "orientation_not_used": ["C"]}


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
def fast_disk(monkeypatch):
    """The disk under the test replaced by an internal SSD read at 250 MB/s: the exact reasons of a record must not
    depend on the link of the disk the temporary folder is on."""
    monkeypatch.setattr(machine, "disk_facts", lambda path: {"bus": "PCI-Express", "link_mbps": None,
                                                              "solid_state": True})
    monkeypatch.setattr(machine, "read_rate", lambda files, *a, **k: 250.0)


@pytest.fixture
def stand_in(monkeypatch, fast_disk):
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
    ("database /other/place/sfm/database.db is locked", "database <path>"),  # an unquoted path runs to the line's end
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


def test_scrub_takes_the_private_paths_longest_first_a_quoted_path_whole_and_then_the_photo(tmp_path):
    survey = tmp_path / "survey"  # private paths of this host: absolute whatever the system and its Python
    photos = survey / "photos"
    private = (survey, photos)
    assert record.scrub(f"in {photos} and {survey}", private) == "in <path> and <path>"
    out = record.scrub(f"'{photos}/IMG_0007.jpg' in {survey} and IMG_0007 too", private, names=("IMG_0007.jpg",))
    assert out == "'<path>' in <path> and <photo> too"
    assert record.scrub(f"cannot open {survey}/a b/c - try again: now", private) == \
        "cannot open <path> - try again: now"


@pytest.mark.parametrize("text, expected", REAL_TEXTS)
def test_scrub_on_the_text_of_a_real_exception(text, expected):
    assert record.scrub(text, PRIVATE, names=("IMG_0001.JPG",)) == expected


@pytest.mark.parametrize("text, expected", APOSTROPHE_TEXTS)
def test_scrub_with_an_apostrophe_in_a_path_a_photo_or_a_word(text, expected):
    """An apostrophe inside a word (can't, d'Alto, Sant'Elmo) is no quote: it neither opens nor closes a span and
    does not stop the tail of a path."""
    assert record.scrub(text, (Path(POGGIO),) + PRIVATE, names=NAMES) == expected


@pytest.mark.parametrize("text, expected", [
    ("id" + WIN_HOME.replace("\\", "\\\\") + "\\\\x", "id<path>"),  # backslashes doubled, as an OSError text
    ("id" + WIN_HOME.replace("\\", "/") + "/x", "id<path>"),  # slashes for backslashes, as the repr of a Path
    ("id" + WIN_HOME.upper() + "\\x", "id<path>"),  # another case of the letters
    ("id{home}/x: and id{HOME}", "id<path>: and id<path>"),  # what follows a separator is the path's
    ("id{home} and id{HOME}'", "id<path> and id<path>'"),  # a space, a quote: boundaries
    ("cannot read {home}lovelace", "cannot read <path>"),  # a longer name is another path, taken whole
], ids=["doubled", "slashes", "case", "separator", "boundaries", "whole"])
def test_scrub_finds_a_private_path_in_every_spelling_and_only_whole(tmp_path, text, expected):
    home = tmp_path / "fieldteam"  # absolute on this host, whatever the system and its Python
    text = text.replace("{home}", str(home)).replace("{HOME}", str(home).upper())
    assert record.scrub(text, (home, WIN_HOME)) == expected


def test_scrub_leaves_no_token_with_a_separator_after_its_stops(tmp_path):
    """What the stops of the path rules leave (a ' - ' inside a folder name, a private folder whose name is another
    folder's up to a space, a home that is the start of another user's) is taken by the last pass, token by token; a
    lone word of a folder name before ' - ' may remain."""
    home, scavo = tmp_path / "fieldteam", tmp_path / "Scavo"
    cases = [(f"{home}/Scavi/Nord - Sito Alfa/gcp.csv", (home,), "<path> - Sito <path>"),
             (f"{scavo} Nord/x.jpg", (scavo,), "<path> <path>"),
             (WIN_HOME + r"\Pictures\x.jpg", (r"C:\Users\Field",), "<path> <path>")]
    for text, private, expected in cases:
        out = record.scrub("cannot read " + text, private)
        assert out == "cannot read " + expected, text
        assert not any("/" in token or "\\" in token for token in out.split())
    assert record.scrub("speed in m/s and a/b", ()) == "speed in <path> and <path>"
    assert record.scrub("see https://example.org/guide and <path>", ()) == "see https://example.org/guide and <path>"


@pytest.mark.parametrize("text, expected", [
    ("RuntimeError: bad path:/srv/field-unit/Field Unit/Scavo Nord Est/x.jpg", "RuntimeError: bad path:<path>"),
    ("RuntimeError: cannot open file:///srv/field-unit/Scavo Nord Est/x.jpg", "RuntimeError: cannot open file:<path>"),
    ("OperationalError: sqlite:////srv/field-unit/My Data Folder/db.sqlite", "OperationalError: sqlite:<path>"),
    (r"RuntimeError: error:C:\Users\Field Team Lead\x.jpg", "RuntimeError: error:<path>"),
    ("see https://example.org/guide and http://example.org/a/b",
     "see https://example.org/guide and http://example.org/a/b"),
], ids=["after-colon", "file-uri", "sqlite-url", "drive-after-colon", "urls"])
def test_an_unquoted_path_right_after_a_colon_or_a_scheme_is_taken_whole_and_a_url_stays(text, expected):
    """Only the schemes of a URL keep a slash from starting a path: a path written right after ``word:``, a
    ``file://`` or ``sqlite://`` URI go whole, with the inner words of their folders; a web URL stays."""
    assert record.scrub(text, ()) == expected


def test_describe_keeps_the_strerror_of_an_os_error_never_its_file_name():
    exc = FileNotFoundError(2, "No such file or directory", PHOTOS + "/IMG_0001.JPG")
    assert record.describe(exc) == "FileNotFoundError: No such file or directory"
    assert record.describe(PermissionError(13, "Permission denied", PHOTOS, None, OUT)) == "PermissionError: " \
                                                                                           "Permission denied"
    assert record.describe(FileNotFoundError(PHOTOS + "/p.ppg")) == "FileNotFoundError"  # Document.open: the path alone
    assert record.describe(RuntimeError("alignment failed: no reconstruction could be initialised")) == \
        "RuntimeError: alignment failed: no reconstruction could be initialised"
    assert record.describe(KeyboardInterrupt()) == "KeyboardInterrupt"


def test_a_record_written_after_each_real_text_holds_no_piece_of_a_private_path_nor_a_user_name(tmp_path):
    """Every word of every folder of the private paths (a piece of a path, as a scrub that cuts at a space leaves it)
    and the user names, case ignored, in no string of the written record."""
    private = (Path(POGGIO),) + PRIVATE
    folders = {part for p in (PHOTOS, OUT, HOME, POGGIO) for part in Path(p).parts if part != "/"}
    folders |= {"Users", "Field Team"}
    pieces = {word for folder in folders for word in folder.split()} | {"fieldteam", "Field Team"}
    for index, case in enumerate(REAL_TEXTS + APOSTROPHE_TEXTS, 1):
        r = rec()
        r["error"] = record.scrub(case.values[0], private, names=NAMES)
        path = record.write(record.judge(r), tmp_path / "records", private, index=index)
        for where, text in strings_of(json.loads(path.read_text(encoding="utf-8"))):
            low = text.casefold()
            assert not any(piece.casefold() in low for piece in pieces), (case.id, where, text)


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
    # the targets ``all`` detects by default are those ppg process detects, which are those of Chunk.detect_markers
    assert steps.DEFAULT_TARGETS == process["targets"].default == "aruco_4x4_50"
    assert steps.DEFAULT_TARGETS == inspect.signature(Chunk.detect_markers).parameters["target_type"].default
    # the mappers ``run`` chooses from (--mapper), the first its default: that of Chunk.align_cameras, which ppg
    # process aligns with (it passes none), and of --mapper of ppg align; hierarchical is no choice of the bench
    assert steps.MAPPERS == ("auto", "incremental", "global")
    assert steps.MAPPERS[0] == inspect.signature(Chunk.align_cameras).parameters["mapper"].default == "auto"
    assert steps.MAPPERS[0] == inspect.signature(cli.align).parameters["mapper"].default.default  # a typer option
    assert "mapper" not in process


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


def test_wanted_decides_the_conditional_steps_as_ppg_process_does():
    plain = stub_chunk([], [])
    assert steps.wanted("detect_markers", {"targets": "aruco_4x4_50"}, plain, False)
    assert not steps.wanted("detect_markers", {"targets": None}, plain, False)
    assert steps.wanted("import_reference", {"reference": "/data/gcp.csv"}, plain, False)
    assert not steps.wanted("import_reference", {"reference": None}, plain, False)
    assert not steps.wanted("update_transform", {}, plain, False)
    assert steps.wanted("update_transform", {}, plain, True)  # a reference was imported
    assert steps.wanted("update_transform", {}, stub_chunk([], [], gps=True), False)  # a camera has a position
    assert steps.wanted("update_transform", {}, stub_chunk([], [], scalebars=True), False)  # scale bars
    assert all(steps.wanted(step, {}, plain, False) for step in steps.ALL_STEPS
               if step not in ("detect_markers", "import_reference", "update_transform"))


def test_georeferencing_results_keeps_numbers_only(monkeypatch):
    from pyarchinit_photogrammetry.markers import gcp

    monkeypatch.setattr(gcp, "camera_reference_errors", lambda chunk: {1: {"error": 0.3}, 2: {"error": None}, 3: {}})
    out = steps.georeferencing_results(stub_chunk([], []), REPORT)
    assert out == {"control_rmse": 0.002, "camera_rmse": pytest.approx(0.3), "markers": 1}  # no CRS, no label
    monkeypatch.setattr(gcp, "camera_reference_errors", lambda chunk: {})
    assert steps.georeferencing_results(stub_chunk([], []), {**REPORT, "control_rmse": None, "check_rmse": 0.01}) == \
        {"check_rmse": 0.01, "markers": 1}


def test_the_child_runs_the_chain_of_all_with_the_arguments_written_out(tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.markers import gcp

    monkeypatch.setattr(gcp, "camera_reference_errors", lambda chunk: {})
    monkeypatch.setattr(steps, "_rss_mb", lambda: 50.0)
    calls, saves = [], []
    chunk = stub_chunk(calls, saves, report=REPORT)
    result = steps.Result(tmp_path / "result.json")
    job = {"steps": "all", "targets": "aruco_4x4_50", "reference": "/data/gcp.csv", "crs": "EPSG:3004",
           "mapper": "global"}
    assert steps.run_chain(chunk, job, result) is True
    assert calls == [("detect_markers", ("aruco_4x4_50",), {}), ("match_photos", (), steps.ARGUMENTS["match_photos"]),
                     ("align_cameras", (), {"mapper": "global"}),
                     ("import_reference", ("/data/gcp.csv",), {"crs": "EPSG:3004"}),
                     ("update_transform", (), {}), ("build_depth_maps", (), {"quality": "medium"}),
                     ("build_point_cloud", (), {}), ("build_model", (), {"surface": "arbitrary"}),
                     ("build_texture", (), {}), ("build_dem", (), {}), ("build_orthomosaic", (), {})]
    assert len(saves) == len(steps.ALL_STEPS)  # the project saved after every step
    data = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert data == result.data and data["done"] is True and data["error"] is None
    assert [e["step"] for e in data["steps"]] == list(steps.ALL_STEPS)
    assert all(e["end"] and e["seconds"] >= 0 and e["peak_memory_mb"] == 50.0 and "error" not in e
               for e in data["steps"])
    assert data["settings"]["georeferencing"] == "markers" and data["settings"]["quality"] is None
    assert data["results"] == {"cameras": 1, "aligned": 1, "control_rmse": 0.002, "markers": 1}
    # align: the matching and the alignment alone without targets, reference, positions or scale bars
    align = {"steps": "align", "targets": None, "reference": None, "crs": None}
    calls, saves = [], []
    assert steps.run_chain(stub_chunk(calls, saves), align, steps.Result(tmp_path / "align.json")) is True
    assert [c[0] for c in calls] == ["match_photos", "align_cameras"]
    assert calls[1] == ("align_cameras", (), {"mapper": "auto"})  # a job that names no mapper: the default
    calls = []
    steps.run_chain(stub_chunk(calls, [], gps=True, report=REPORT), align, steps.Result(tmp_path / "gps.json"))
    assert [c[0] for c in calls] == ["match_photos", "align_cameras", "update_transform"]


def test_an_error_after_a_step_is_recorded_like_an_error_of_the_step(tmp_path, monkeypatch):
    monkeypatch.setattr(steps, "_rss_mb", lambda: 50.0)
    calls = []
    result = steps.Result(tmp_path / "result.json")
    job = {"steps": "all", "targets": None, "reference": "/data/gcp.csv", "crs": None}
    assert steps.run_chain(stub_chunk(calls, [], report=None), job, result) is False  # the report is not a dict
    data = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert [e["step"] for e in data["steps"]] == ["match_photos", "align_cameras", "import_reference",
                                                  "update_transform"]
    last = data["steps"][-1]
    assert last["end"] and last["error"] == data["error"]
    assert last["error"].startswith("update_transform: AttributeError")
    assert data["done"] is False and [c[0] for c in calls][-1] == "update_transform"


def test_a_job_the_child_cannot_read_or_parse_leaves_a_result_with_the_error(tmp_path):
    """A job file missing, not JSON or without the work folder: the child writes ``result.json`` beside it with the
    error (the type and the message, never the file name: :func:`record.describe`), ends with 1 and prints no
    traceback; the parent reads that error as it reads the one of a step."""
    job, result = tmp_path / "job.json", tmp_path / "result.json"
    job.write_text("{not json", encoding="utf-8")
    assert steps.main([str(job)]) == 1
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["error"].startswith("job: JSONDecodeError: ") and data["steps"] == [] and data["done"] is False
    result.unlink()
    job.write_text(json.dumps({"photos": [], "steps": "align"}), encoding="utf-8")  # no work folder in it
    assert steps.main([str(job)]) == 1
    assert json.loads(result.read_text(encoding="utf-8"))["error"] == "job: KeyError: 'work'"
    result.unlink()
    assert steps.main([str(tmp_path / "missing.json")]) == 1
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["error"].startswith("job: FileNotFoundError") and str(tmp_path) not in json.dumps(data)
    result.unlink()
    done = subprocess.run([sys.executable, "-m", "pyarchinit_photogrammetry.bench.steps", str(job)],
                          capture_output=True, text=True, timeout=300, close_fds=False)
    assert done.returncode == 1 and "Traceback" not in done.stderr and result.is_file(), done.stderr[-2000:]


def test_the_result_is_written_again_while_a_reader_holds_it(tmp_path, monkeypatch):
    real, held = steps.os.replace, []

    def replace(src, dst):
        if len(held) < 2:  # Windows: the parent reads the file every half second and holds it for a moment
            held.append(dst)
            raise PermissionError(13, "The process cannot access the file")
        return real(src, dst)

    monkeypatch.setattr(steps.os, "replace", replace)
    monkeypatch.setattr(steps.time, "sleep", lambda seconds: None)
    result = steps.Result(tmp_path / "result.json")
    result.data["done"] = True
    result.save()
    assert len(held) == 2 and json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["done"] is True
    always = []

    def never(src, dst):
        always.append(dst)
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(steps.os, "replace", never)
    with pytest.raises(PermissionError):
        result.save()
    assert 2 <= len(always) <= 10  # a few tries, then the error


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
    with pytest.raises(ValueError):  # the one way the bench could write beside the photos
        runner.run(photos, tmp_path / "out", name="t", records=photos / "records")
    with pytest.raises(ValueError):
        runner.run(tmp_path / "missing", tmp_path / "out", name="t")
    with pytest.raises(ValueError):  # the records inside a work folder would be removed with it
        runner.run(photos, tmp_path / "out", name="t", records=tmp_path / "out" / "t-1")
    with pytest.raises(ValueError):
        runner.run(photos, tmp_path / "out", name="t", repeat=2, records=tmp_path / "out" / "t-2" / "sub")
    with pytest.raises(ValueError):  # another case of the letters: one folder on the disks of macOS and Windows
        runner.run(photos, tmp_path / "out", name="t", records=tmp_path / "out" / "T-1")
    with pytest.raises(ValueError):
        runner.run(photos, tmp_path / "out", name="t", repeat=2, records=tmp_path / "out" / "T-2" / "sub")
    assert not (tmp_path / "out").exists()
    (tmp_path / "out" / "t-2").mkdir(parents=True)  # a stray work folder of a later repeat: refused before any child
    with pytest.raises(FileExistsError):
        runner.run(photos, tmp_path / "out", name="t", repeat=2)
    assert not (tmp_path / "out" / "t-1").exists()


def test_the_work_folder_inside_the_photos_is_refused_whatever_the_case_of_the_letters(tmp_path, monkeypatch):
    photos = noise_photos(tmp_path / "Photos")
    if not (tmp_path / "photos").exists():
        pytest.skip("this file system does not fold the case of names")
    monkeypatch.setattr(runner.subprocess, "Popen", child_only(no_child))
    with pytest.raises(ValueError):
        runner.run(photos, tmp_path / "photos" / "out", name="t")
    with pytest.raises(ValueError):
        runner.run(photos, tmp_path / "out", name="t", records=tmp_path / "photos" / "records")


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
    offline = "online-only: make them available offline first or copy them to a local disk"
    with pytest.raises(RuntimeError, match=offline):
        runner.run(photos, tmp_path / "out", name="t")
    assert not (tmp_path / "out").exists()


def test_an_unknown_mapper_is_refused_before_any_child(tmp_path, monkeypatch):
    pytest.importorskip("psutil")
    photos = noise_photos(tmp_path / "photos")
    monkeypatch.setattr(runner.subprocess, "Popen", child_only(no_child))
    for mapper in ("hierarchical", "Global", "", None):
        with pytest.raises(ValueError, match="mapper must be"):
            runner.run(photos, tmp_path / "out", name="t", mapper=mapper)
        with pytest.raises(ValueError, match="mapper must be"):
            runner.check(tmp_path / "out", "t", mapper=mapper)
    assert not (tmp_path / "out").exists()
    for mapper in steps.MAPPERS:
        runner.check(tmp_path / "out", "t", mapper=mapper)  # the three choices pass the checks


# --- the run on the small set -----------------------------------------------------------------------------------------

def test_a_small_run_writes_one_record(small_set, tmp_path, monkeypatch):
    monkeypatch.delenv("PPG_ENGINE", raising=False)  # the engine the computer asks for is not this run's: auto
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
    # pairs: None when every pair of a small set is matched on the CPU alone (no GPU, as on the CI runners): the
    # exhaustive matching of COLMAP lists no pairs (engine/sfm.py, the pair step of match_photos)
    pairs = r["settings"]["pairs"]
    assert r["settings"]["quality"] == "high" and (pairs is None or pairs > 0) and r["settings"]["mapper"]
    # the mapper asked for (the default) beside the one the run used: auto picks incremental on 14 photos without GPS
    assert r["settings"]["mapper_requested"] == "auto" and r["settings"]["mapper"] == "incremental"
    assert r["settings"]["max_ratio"] == 0.8 and r["settings"]["cross_check"] is True
    assert isinstance(r["settings"]["pycolmap"], str) and r["settings"]["pycolmap"]
    assert r["settings"]["photo_cache_mb"] is None and r["settings"]["georeferencing"] is None
    assert r["settings"]["targets"] is None  # align detects no markers
    assert "image_root" not in r["settings"] and "names" not in r["settings"]
    assert isinstance(r["machine"]["adapters"], list) and "read_mb_s" in r["disk"]
    assert r["valid"] in (True, False) and r["error"] is None
    assert all(any(re.fullmatch(p, reason) for p in REASONS) for reason in r["invalid_reasons"])
    assert r["valid"] == (r["invalid_reasons"] == [])
    for where, text in strings_of(r):
        assert str(tmp_path) not in text, where
        assert not any(p.name in text or p.stem in text for p in small_set.iterdir()), where
    assert not (out / "t-1").exists()


def test_a_failed_run_leaves_a_record_with_the_error(tmp_path, caplog):
    pytest.importorskip("psutil")
    photos = noise_photos(tmp_path / "photos")
    out = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger=RUNNER_LOG):
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
    for where, text in strings_of(r):
        assert str(tmp_path) not in text and "NOISE_" not in text, where
    assert "/" not in r["error"]
    # the work folder of a run that failed stays, with the child's log and its result, said in the log by its name
    assert (out / "t-1" / runner.LOG).is_file() and (out / "t-1" / steps.RESULT).is_file()
    assert said_by_name(caplog, "t-1", tmp_path)


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
    assert r["error"] == "cancelled" and r["valid"] is False and "failed: cancelled" in r["invalid_reasons"]
    assert not (out / "t-1").exists()


def test_a_slow_link_marks_the_run_and_does_not_stop_it(small_set, tmp_path, monkeypatch):
    monkeypatch.setattr(machine, "disk_facts", lambda path: {"bus": "USB", "link_mbps": 480, "solid_state": True})
    out = tmp_path / "out"
    r = the_record(out, runner.run(small_set, out, name="t"))
    assert r["valid"] is False and "slow disk link: 480 Mb/s" in r["invalid_reasons"]
    assert [s["step"] for s in r["steps"]] == ["match_photos", "align_cameras"] and r["error"] is None
    assert r["disk"]["link_mbps"] == 480 and r["disk"]["bus"] == "USB" and "read_mb_s" in r["disk"]


def test_a_run_whose_parent_lists_no_gpu_ends_and_records_an_empty_list(small_set, tmp_path, monkeypatch):
    """The list of adapters is patched in the parent alone (the child matches as the machine allows): the run ends
    and the record says ``adapters == []``, as a record made on a runner without a GPU would."""
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


def test_a_child_that_dies_leaves_the_step_it_died_in(tmp_path, stand_in, caplog):
    stand_in.mode = "dies"
    photos = noise_photos(tmp_path / "photos")
    out = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger=RUNNER_LOG):
        r = the_record(out, runner.run(photos, out, name="t"))
    assert r["error"] == "match_photos: child ended with code 9" and r["valid"] is False
    assert r["invalid_reasons"] == ["failed: match_photos: child ended with code 9"]
    assert [s["step"] for s in r["steps"]] == ["match_photos"]
    assert r["steps"][0]["error"] == r["error"] and r["steps"][0]["seconds"] > 0
    assert (out / "t-1" / runner.LOG).is_file() and said_by_name(caplog, "t-1", tmp_path)  # kept: the run failed


def test_finish_keeps_the_work_folder_of_a_failed_run_and_removes_the_one_of_a_cancelled_run(tmp_path, caplog):
    """After the record is written: the folder of a run that failed stays with the child's log, said in the log by
    its name; a cancelled run (its error is ``cancelled``) and a successful one remove it, as before; a folder kept
    by request (``--keep``) is not said."""
    def finish(name: str, error: str | None, keep: bool = False, cancelled: bool = False) -> Path:
        work = tmp_path / "out" / f"{name}-1"
        work.mkdir(parents=True)
        (work / runner.LOG).write_text("the child's log\n", encoding="utf-8")
        plan = runner._Plan(name=name, steps="align", mapper="auto", local=(), out=tmp_path / "out",
                            records=tmp_path / "out", keep=keep, env={}, disk={}, set={}, settings={},
                            private=(tmp_path,), names=(), extra_results=None, targets=None, reference=None, crs=None)
        r = rec()
        r["name"], r["error"] = name, error
        runner._finish(r, work, plan, 1, cancelled=cancelled)
        return work

    with caplog.at_level(logging.WARNING, logger=RUNNER_LOG):
        assert (finish("failed", "match_photos: RuntimeError: no matches") / runner.LOG).is_file()
        assert not finish("cancelled", "cancelled", cancelled=True).exists()
        assert not finish("done", None).exists()
        assert (finish("kept", "match_photos: RuntimeError: no matches", keep=True) / runner.LOG).is_file()
    assert said_by_name(caplog, "failed-1", tmp_path)
    assert not any(name in e.getMessage() for e in caplog.records for name in ("cancelled-1", "done-1", "kept-1"))


def test_the_texts_of_the_runner_are_not_scrubbed(tmp_path, stand_in):
    """A crash code with many digits and a photo whose stem is a digit of it stay as they are: only the text of an
    exception goes through the scrub."""
    stand_in.mode = "dies"
    photos = noise_photos(tmp_path / "photos")
    shutil.copy(photos / "NOISE_00.jpg", photos / "9.jpg")
    out = tmp_path / "out"
    r = the_record(out, runner.run(photos, out, name="t"))
    assert r["error"] == "match_photos: child ended with code 9" and r["steps"][0]["error"] == r["error"]


def test_all_detects_the_targets_of_ppg_process_by_default_and_align_does_not(tmp_path, stand_in):
    photos = noise_photos(tmp_path / "photos")
    r = the_record(tmp_path / "all", runner.run(photos, tmp_path / "all", name="t", steps="all", keep=True))
    job = json.loads((tmp_path / "all" / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["steps"] == "all" and job["targets"] == r["settings"]["targets"] == steps.DEFAULT_TARGETS
    r = the_record(tmp_path / "align", runner.run(photos, tmp_path / "align", name="t", keep=True))
    job = json.loads((tmp_path / "align" / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["targets"] is None and r["settings"]["targets"] is None
    r = the_record(tmp_path / "given", runner.run(photos, tmp_path / "given", name="t", targets="apriltag_36h11"))
    assert r["settings"]["targets"] == "apriltag_36h11" and list(r["settings"]) == SETTINGS


def test_the_mapper_asked_for_goes_to_the_child_and_is_kept_beside_the_one_the_run_used(tmp_path, stand_in):
    photos = noise_photos(tmp_path / "photos")
    r = the_record(tmp_path / "global", runner.run(photos, tmp_path / "global", name="t", mapper="global", keep=True))
    job = json.loads((tmp_path / "global" / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["mapper"] == "global" and r["settings"]["mapper_requested"] == "global"
    assert r["settings"]["mapper"] == "incremental" and list(r["settings"]) == SETTINGS  # what the run used
    r = the_record(tmp_path / "auto", runner.run(photos, tmp_path / "auto", name="t", keep=True))
    job = json.loads((tmp_path / "auto" / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["mapper"] == "auto" and r["settings"]["mapper_requested"] == "auto"


def test_the_seed_goes_to_the_child_as_ppg_ransac_seed_and_is_kept_in_the_record(tmp_path, monkeypatch, stand_in):
    """``seed`` fixes the RANSAC of the geometric verification in the child through ``PPG_RANSAC_SEED``; the record
    keeps the seed the child runs with: the one asked for, the one inherited from the environment, or ``-1`` (a seed
    from the clock) without either."""
    from pyarchinit_photogrammetry.engine import sfm

    monkeypatch.delenv(sfm.RANSAC_SEED_ENV, raising=False)
    photos = noise_photos(tmp_path / "photos")
    r = the_record(tmp_path / "seeded", runner.run(photos, tmp_path / "seeded", name="t", seed=7))
    (_cmd, kwargs), = stand_in.calls
    assert kwargs["env"][sfm.RANSAC_SEED_ENV] == "7" and r["settings"]["ransac_seed"] == 7
    assert list(r["settings"]) == SETTINGS
    r = the_record(tmp_path / "clock", runner.run(photos, tmp_path / "clock", name="t"))
    _cmd, kwargs = stand_in.calls[-1]
    assert sfm.RANSAC_SEED_ENV not in kwargs["env"] and r["settings"]["ransac_seed"] == -1
    monkeypatch.setenv(sfm.RANSAC_SEED_ENV, "3")
    r = the_record(tmp_path / "inherited", runner.run(photos, tmp_path / "inherited", name="t"))
    _cmd, kwargs = stand_in.calls[-1]
    assert kwargs["env"][sfm.RANSAC_SEED_ENV] == "3" and r["settings"]["ransac_seed"] == 3
    with pytest.raises(ValueError, match="seed must be 0 or more"):
        runner.run(photos, tmp_path / "negative", name="t", seed=-1)
    assert not (tmp_path / "negative").exists() and len(stand_in.calls) == 3


def test_the_machine_is_read_again_for_every_repeat(tmp_path, monkeypatch, stand_in):
    reads = []

    def facts():
        reads.append(1)
        return {"system": "Test", "cores": 4, "load": float(len(reads)), "adapters": [], "on_battery": None,
                "dropbox_running": None}

    monkeypatch.setattr(machine, "machine_facts", facts)
    photos = noise_photos(tmp_path / "photos")
    paths = runner.run(photos, tmp_path / "out", name="t", repeat=2)
    assert len(paths) == 2 and len(reads) == 2 and [p.name[-7:] for p in paths] == ["-1.json", "-2.json"]
    assert [json.loads(p.read_text(encoding="utf-8"))["machine"]["load"] for p in paths] == [1.0, 2.0]


def test_a_work_folder_that_cannot_be_removed_is_said_without_its_path(tmp_path, monkeypatch, stand_in, caplog):
    def busy(path, *args, **kwargs):
        raise OSError(16, "Resource busy", str(path))

    monkeypatch.setattr(runner.shutil, "rmtree", busy)
    photos = noise_photos(tmp_path / "photos")
    out = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger=RUNNER_LOG):
        r = the_record(out, runner.run(photos, out, name="t"))
    assert r["valid"] is True and (out / "t-1").is_dir() and said_by_name(caplog, "t-1", tmp_path)


def test_a_child_that_cannot_start_leaves_the_work_folder_and_says_so(tmp_path, monkeypatch, caplog):
    pytest.importorskip("psutil")

    def refused(cmd, **kwargs):
        raise OSError(8, "Exec format error", cmd[0])

    monkeypatch.setattr(runner.subprocess, "Popen", child_only(refused))
    photos = noise_photos(tmp_path / "photos")
    out = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger=RUNNER_LOG), pytest.raises(OSError):
        runner.run(photos, out, name="t")
    assert (out / "t-1" / "job.json").exists() and list(out.glob("*.json")) == []
    assert said_by_name(caplog, "t-1", tmp_path)


def test_a_record_that_cannot_be_written_leaves_the_work_folder_and_says_so(tmp_path, stand_in, caplog):
    (tmp_path / "records").write_text("a file, not a folder", encoding="utf-8")
    photos = noise_photos(tmp_path / "photos")
    out = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger=RUNNER_LOG), pytest.raises(OSError):
        runner.run(photos, out, name="t", records=tmp_path / "records" / "sub")
    assert (out / "t-1" / "result.json").exists() and said_by_name(caplog, "t-1", tmp_path)


def test_a_record_that_is_refused_leaves_the_work_folder_and_says_so(tmp_path, stand_in, caplog):
    stand_in.mode = "leaks"
    photos = noise_photos(tmp_path / "photos")
    out = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger=RUNNER_LOG), pytest.raises(ValueError):
        runner.run(photos, out, name="t")
    assert (out / "t-1" / "result.json").exists() and list(out.glob("*.json")) == []
    assert said_by_name(caplog, "t-1", tmp_path)


def test_a_job_that_cannot_be_written_leaves_the_work_folder_and_says_so(tmp_path, monkeypatch, caplog):
    pytest.importorskip("psutil")
    monkeypatch.setattr(runner.subprocess, "Popen", child_only(no_child))
    real = Path.write_text

    def refuse(self, *args, **kwargs):
        if self.name == runner.JOB:
            raise OSError(28, "No space left on device", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", refuse)
    photos = noise_photos(tmp_path / "photos")
    out = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger=RUNNER_LOG), pytest.raises(OSError):
        runner.run(photos, out, name="t")
    assert (out / "t-1").is_dir() and said_by_name(caplog, "t-1", tmp_path)


def test_the_reference_is_private_and_a_text_that_holds_it_is_scrubbed(tmp_path, monkeypatch, stand_in):
    stand_in.mode = "reference"
    photos = noise_photos(tmp_path / "photos")
    reference = tmp_path / "Riferimenti GCP" / "gcp.csv"
    reference.parent.mkdir()
    reference.write_text("# label,X,Y,Z\n", encoding="utf-8")
    privates = []
    real = record.write

    def write(rec, folder, private=(), index=1):
        privates.append(tuple(private))
        return real(rec, folder, private, index)

    monkeypatch.setattr(runner.record, "write", write)
    out = tmp_path / "out"
    r = the_record(out, runner.run(photos, out, name="t", reference=reference, crs="EPSG:3004", keep=True))
    job = json.loads((out / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["reference"] == str(reference.resolve()) and job["crs"] == "EPSG:3004"
    assert privates == [(photos.resolve(), out.resolve(), Path.home(), reference.resolve())]
    assert r["error"] == "import_reference: ValueError: cannot read <path>" and r["valid"] is False
    assert [s["step"] for s in r["steps"]] == ["match_photos", "align_cameras", "import_reference"]
    assert r["steps"][-1]["error"] == r["error"]
    assert not any("Riferimenti" in text for _, text in strings_of(r))
