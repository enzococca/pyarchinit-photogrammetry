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
from pyarchinit_photogrammetry.engine import switch
from test_bench_table import colmap_database

# the only reasons a record is not valid for, and the name of its file
REASONS = (r"slow disk link: \d+ Mb/s", r"slow disk: \d+ MB/s", r"online-only photos: \d+", r"failed: .+")
FILE_NAME = re.compile(r"^t-\d{14}-\d+\.json$")
STEP_KEYS = {"step", "seconds", "peak_memory_mb", "stages", "engine"}
SETTINGS = ["pairs", "matcher", "quality", "gpu", "gpu_pairs", "cpu_pairs", "mapper", "global_fallback",
            "native_fallback", "engine", "features", "routes", "native_dct", "native_guided", "native_guided_ratio",
            "native_guided_band", "native_guided_check", "max_ratio", "max_distance", "cross_check", "pycolmap",
            "photo_cache_mb", "georeferencing", "targets", "mapper_requested", "engine_requested",
            "align_engine_requested", "ransac_seed", "matches_reused", "markers", "checks", "optimize_engine",
            "optimize_solver", "native_tracks_per_photo", "native_final_all_tracks"]
# the record of the engine of a step that ran classic because auto, the default of a run, chose so
AUTO_CLASSIC = {"requested": "auto", "used": "classic", "fallback": None}

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
for step in set(job.get("after") or []) & {"match_photos", "align_cameras"}:  # the steps it ran
    (work / f"project.after-{step}.ppg").write_bytes((work / "project.ppg").read_bytes())
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


def stub_chunk(calls: list, saves: list, gps: bool = False, scalebars: bool = False, report=None, data_dir=None):
    """A chunk whose methods only note their call: the chain of the child without an engine. ``report`` is what
    ``update_transform`` returns; ``data_dir`` the folder of its data (one that is not there, by default: no
    database, no reconstruction)."""
    camera = types.SimpleNamespace(reference=types.SimpleNamespace(location=[1.0, 2.0, 3.0] if gps else None),
                                   aligned=True)
    chunk = types.SimpleNamespace(meta={}, cameras=[camera], aligned_cameras=[camera],
                                  scalebars=[object()] if scalebars else [], product_path=lambda name: None,
                                  data_dir=Path(data_dir) if data_dir else Path("no-such-chunk-data"),
                                  document=types.SimpleNamespace(save=lambda: saves.append(1)))

    def method(step):
        def run(*args, **kwargs):
            calls.append((step, args, kwargs))
            return report if step == "update_transform" else None
        return run

    for step in steps.ALL_STEPS + ("optimize_cameras",):
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
    # match: the matching alone (the time gate of the native matching on a large set, without the mapper); the
    # markers before it, when the job names targets, as in the two other chains
    # optimize: the chain of align and the optimization of the cameras after it, with the defaults of ppg optimize
    assert steps.CHAINS == {"align": steps.ALIGN_STEPS, "all": steps.ALL_STEPS, "match": steps.MATCH_STEPS,
                            "optimize": steps.ALIGN_STEPS + ("optimize_cameras",)}
    assert steps.CHAINS["optimize"][-1] == "optimize_cameras" and steps.ARGUMENTS["optimize_cameras"] == {}
    assert steps.MATCH_STEPS == ("detect_markers", "match_photos") == steps.ALIGN_STEPS[:2]
    # the engines ``run`` chooses from (--engine), the first its default: those of the switch, whose default is auto
    assert steps.ENGINES == switch.ENGINES == ("auto", "classic", "native") and steps.ENGINES[0] == "auto"


def test_the_arguments_of_the_steps_are_the_defaults_of_today():
    for step, arguments in steps.ARGUMENTS.items():
        params = inspect.signature(getattr(Chunk, step)).parameters
        for key, value in arguments.items():
            assert params[key].default == value, (step, key)
    assert steps.ARGUMENTS["match_photos"] == {"quality": "high", "keypoint_limit": 40000, "tiepoint_limit": 8000,
                                               "pair_selection": "auto"}
    # the engine reaches the three switched steps from the job (steps.call), never through the arguments
    assert not any("engine" in arguments for arguments in steps.ARGUMENTS.values())
    assert all("engine" in inspect.signature(getattr(Chunk, step)).parameters for step in switch.STEPS)
    process = inspect.signature(cli.process).parameters
    assert process["quality"].default == steps.ARGUMENTS["match_photos"]["quality"]
    assert process["dense_quality"].default == steps.ARGUMENTS["build_depth_maps"]["quality"]
    assert process["surface"].default.default == steps.ARGUMENTS["build_model"]["surface"]  # a typer option
    assert set(steps.ARGUMENTS) == set(steps.ALL_STEPS + ("optimize_cameras",)) - {"detect_markers",
                                                                                    "import_reference"}
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
    # the engine of the job (auto when it names none) goes to the matching, its engine of the alignment (auto when
    # it names none) to the alignment; the depth maps, the other step that takes one, run classic whatever the job
    # or the computer asks (PPG_ENGINE of the parent never reaches the child)
    assert calls == [("detect_markers", ("aruco_4x4_50",), {}),
                     ("match_photos", (), {**steps.ARGUMENTS["match_photos"], "engine": "auto"}),
                     ("align_cameras", (), {"mapper": "global", "engine": "auto"}),
                     ("import_reference", ("/data/gcp.csv",), {"crs": "EPSG:3004"}),
                     ("update_transform", (), {}), ("build_depth_maps", (), {"quality": "medium", "engine": "classic"}),
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
    assert calls[1] == ("align_cameras", (), {"mapper": "auto", "engine": "auto"})  # none named: the defaults
    calls = []
    steps.run_chain(stub_chunk(calls, [], gps=True, report=REPORT), align, steps.Result(tmp_path / "gps.json"))
    assert [c[0] for c in calls] == ["match_photos", "align_cameras", "update_transform"]


def test_the_engine_of_the_job_reaches_the_matching_and_the_matching_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(steps, "_rss_mb", lambda: 50.0)
    for engine in steps.ENGINES:
        calls = []
        job = {"steps": "all", "targets": None, "reference": None, "crs": None, "engine": engine}
        assert steps.run_chain(stub_chunk(calls, []), job, steps.Result(tmp_path / f"{engine}.json")) is True
        by_step = {step: kwargs for step, _args, kwargs in calls}
        assert by_step["match_photos"]["engine"] == engine
        # the alignment keeps its own default (the job names no engine for it), the depth maps run classic
        assert by_step["align_cameras"]["engine"] == "auto" and by_step["build_depth_maps"]["engine"] == "classic"
        assert not any("engine" in kwargs for step, kwargs in by_step.items() if step not in switch.STEPS)


def test_the_align_engine_of_the_job_reaches_the_alignment_and_the_alignment_alone(tmp_path, monkeypatch):
    """``align_engine`` of the job is the ``engine`` of ``Chunk.align_cameras``, whatever engine the job names for
    the matching; the matching keeps its own, the depth maps run classic and no other step gets an engine."""
    monkeypatch.setattr(steps, "_rss_mb", lambda: 50.0)
    for engine in steps.ENGINES:
        calls = []
        job = {"steps": "all", "targets": None, "reference": None, "crs": None, "align_engine": engine}
        assert steps.run_chain(stub_chunk(calls, []), job, steps.Result(tmp_path / f"{engine}.json")) is True
        by_step = {step: kwargs for step, _args, kwargs in calls}
        assert by_step["align_cameras"] == {"mapper": "auto", "engine": engine}
        assert by_step["match_photos"]["engine"] == "auto" and by_step["build_depth_maps"]["engine"] == "classic"
        assert not any("engine" in kwargs for step, kwargs in by_step.items() if step not in switch.STEPS)
    calls = []
    job = {"steps": "align", "targets": None, "reference": None, "crs": None, "engine": "classic",
           "align_engine": "native", "mapper": "global"}
    assert steps.run_chain(stub_chunk(calls, []), job, steps.Result(tmp_path / "both.json")) is True
    assert calls == [("match_photos", (), {**steps.ARGUMENTS["match_photos"], "engine": "classic"}),
                     ("align_cameras", (), {"mapper": "global", "engine": "native"})]


def test_the_fallback_of_the_native_mapper_is_a_setting_of_the_record(tmp_path, monkeypatch):
    """``native_fallback`` of ``chunk.meta["sfm"]``, the reason the native mapper handed the alignment to the
    classic chain, is kept among the settings beside the mapper that aligned and its own fallback; None when the
    native mapper aligned or was not asked."""
    monkeypatch.setattr(steps, "_rss_mb", lambda: 50.0)
    assert steps.SFM_SETTINGS[steps.SFM_SETTINGS.index("global_fallback") + 1] == "native_fallback"
    chunk = stub_chunk([], [])
    chunk.meta["sfm"] = {"mapper": "incremental", "native_fallback": "placed 3 of 80 photos",
                         "native_mapper": {"engine": "native", "fallback": "placed 3 of 80 photos"},
                         "image_root": "/data/photos", "names": {"1": "IMG_0001.jpg"}}
    settings = steps.settings_of(chunk)
    assert settings["native_fallback"] == "placed 3 of 80 photos" and settings["mapper"] == "incremental"
    assert settings["global_fallback"] is None
    assert not {"native_mapper", "image_root", "names"} & set(settings)  # the report of the mapper stays a project's
    job = {"steps": "align", "targets": None, "reference": None, "crs": None, "align_engine": "native"}
    result = steps.Result(tmp_path / "result.json")
    assert steps.run_chain(chunk, job, result) is True
    assert result.data["settings"]["native_fallback"] == "placed 3 of 80 photos"
    assert steps.settings_of(stub_chunk([], []))["native_fallback"] is None


def test_the_match_chain_stops_after_the_matching(tmp_path, monkeypatch):
    """``--steps match``: the markers (when the job names targets) and the matching, nothing after them; the
    results hold what the matching gives and no number of an alignment."""
    monkeypatch.setattr(steps, "_rss_mb", lambda: 50.0)
    calls, saves = [], []
    job = {"steps": "match", "targets": "aruco_4x4_50", "reference": "/data/gcp.csv", "crs": "EPSG:3004",
           "engine": "native"}
    result = steps.Result(tmp_path / "result.json")
    assert steps.run_chain(stub_chunk(calls, saves, gps=True, report=REPORT), job, result) is True
    assert [c[0] for c in calls] == ["detect_markers", "match_photos"] and len(saves) == 2
    assert calls[1][2]["engine"] == "native"
    data = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert data["done"] is True and data["error"] is None and [e["step"] for e in data["steps"]] == ["detect_markers",
                                                                                                    "match_photos"]
    assert data["results"] == {} and "georeferencing" not in data["settings"]  # a stub: no database to count in
    calls = []
    plain = {"steps": "match", "targets": None, "reference": None, "crs": None}
    assert steps.run_chain(stub_chunk(calls, []), plain, steps.Result(tmp_path / "plain.json")) is True
    assert [c[0] for c in calls] == ["match_photos"]


def test_the_verified_pairs_are_noted_after_the_matching_from_the_database_of_the_chunk(tmp_path, monkeypatch):
    """After ``match_photos`` the results hold ``verified_pairs``, counted in ``database.db`` of the sfm folder of the
    chunk (:func:`pairs.verified`: at least 15 inliers and a configuration the mapper takes); nothing when the
    database is not there."""
    monkeypatch.setattr(steps, "_rss_mb", lambda: 50.0)
    data_dir = tmp_path / "chunk_0"
    (data_dir / "sfm").mkdir(parents=True)
    colmap_database(data_dir / "sfm" / "database.db", ["a.jpg", "b.jpg", "c.jpg"],
                    {("a.jpg", "b.jpg"): (40, 2), ("a.jpg", "c.jpg"): (15, 3), ("b.jpg", "c.jpg"): (14, 2)})
    assert steps.matching_results(stub_chunk([], [], data_dir=data_dir)) == {"verified_pairs": 2}
    assert steps.matching_results(stub_chunk([], [])) == {}
    calls = []
    job = {"steps": "align", "targets": None, "reference": None, "crs": None}
    result = steps.Result(tmp_path / "result.json")
    assert steps.run_chain(stub_chunk(calls, [], data_dir=data_dir), job, result) is True
    assert result.data["results"] == {"verified_pairs": 2, "cameras": 1, "aligned": 1}


def a_marker(label: str, code: int | None = None):
    return types.SimpleNamespace(label=label, code=code, checkpoint=False)


def test_the_check_markers_of_the_job_are_set_right_after_the_reference(tmp_path, monkeypatch):
    """``check: [labels]``: those markers (by label, or by code as the reference file names them) become check
    points right after ``import_reference``, so the georeferencing and the optimization leave them out."""
    monkeypatch.setattr(steps, "_rss_mb", lambda: 50.0)
    calls, seen = [], {}
    chunk = stub_chunk(calls, [], report=REPORT)
    chunk.markers = [a_marker("target 1", 1), a_marker("target 2", 2), a_marker("C")]
    real = steps.call

    def call(chunk_, step, job):
        if step == "update_transform":
            seen.update({m.label: m.checkpoint for m in chunk_.markers})
        return real(chunk_, step, job)

    monkeypatch.setattr(steps, "call", call)
    job = {"steps": "align", "targets": None, "reference": "/data/gcp.csv", "crs": None, "check": ["1", "C"]}
    assert steps.run_chain(chunk, job, steps.Result(tmp_path / "result.json")) is True
    assert seen == {"target 1": True, "target 2": False, "C": True}
    # a label the chunk does not have is an error of the step of the reference: the check would be missing
    chunk.markers = [a_marker("target 1", 1)]
    result = steps.Result(tmp_path / "missing.json")
    assert steps.run_chain(chunk, {**job, "check": ["7"]}, result) is False
    assert result.data["error"].startswith("import_reference: ValueError")


def test_the_hook_after_a_step_adds_its_numbers_to_the_results(tmp_path, monkeypatch):
    """A job key ``after: {step: callable}`` (in-process): the callable gets the document right after that step
    and its numbers land in the results; an error in it is an error of the step."""
    from pyarchinit_photogrammetry.markers import gcp

    monkeypatch.setattr(gcp, "camera_reference_errors", lambda chunk: {})
    monkeypatch.setattr(steps, "_rss_mb", lambda: 50.0)
    calls, order = [], []
    chunk = stub_chunk(calls, [], report=REPORT)
    chunk.markers = []

    def shape(doc):
        order.append([c[0] for c in calls])
        return {"truth_target_shape": 0.001, "truth_targets": 12}

    job = {"steps": "optimize", "targets": None, "reference": "/data/gcp.csv", "crs": None,
           "after": {"update_transform": shape}}
    result = steps.Result(tmp_path / "result.json")
    assert steps.run_chain(chunk, job, result) is True
    assert order == [["match_photos", "align_cameras", "import_reference", "update_transform"]]
    # the published optimization (COLMAP's formulation, the solver of the program) unless the job names another
    assert calls[-1] == ("optimize_cameras", (), {"engine": "colmap", "solver": "auto"})
    assert result.data["results"]["truth_target_shape"] == 0.001 and result.data["results"]["truth_targets"] == 12
    job["after"] = {"align_cameras": lambda doc: {"label": "a word"}}
    result = steps.Result(tmp_path / "word.json")
    assert steps.run_chain(stub_chunk([], [], report=REPORT), job, result) is False
    assert result.data["error"].startswith("align_cameras: TypeError")


def test_the_errors_after_the_optimization_are_noted(tmp_path, monkeypatch):
    """After ``optimize_cameras`` the results gain ``optimized_control_rmse`` and ``optimized_check_rmse``: the
    errors of the markers the chunk reports then, as :func:`steps.georeferencing_results` reads them."""
    from pyarchinit_photogrammetry.markers import gcp

    monkeypatch.setattr(gcp, "camera_reference_errors", lambda chunk: {})
    monkeypatch.setattr(steps, "_rss_mb", lambda: 50.0)
    rows = [{"label": "a", "type": "control", "error": 0.003}, {"label": "b", "type": "control", "error": 0.004},
            {"label": "c", "type": "check", "error": 0.006}, {"label": "d", "type": "check", "error": None}]
    monkeypatch.setattr(gcp, "marker_errors", lambda chunk: rows)
    chunk = stub_chunk([], [], report=REPORT)
    chunk.markers = []
    job = {"steps": "optimize", "targets": None, "reference": "/data/gcp.csv", "crs": None}
    result = steps.Result(tmp_path / "result.json")
    assert steps.run_chain(chunk, job, result) is True
    res = result.data["results"]
    assert res["optimized_control_rmse"] == pytest.approx(np.sqrt((0.003 ** 2 + 0.004 ** 2) / 2))
    assert res["optimized_check_rmse"] == pytest.approx(0.006)
    assert res["control_rmse"] == 0.002  # the georeferencing before it stays


def test_check_and_after_reach_the_child_and_the_hook_runs_on_the_project_of_its_step(tmp_path, stand_in):
    """``runner.run(check_points=..., after=...)``: the check labels go to the child in the job; the hooks stay in
    this process (never written to the job): the job names their steps, the child keeps the project as it was right
    after each of them, and the hook runs on that project when the run ended, its numbers in the results."""
    photos = noise_photos(tmp_path / "photos")
    seen = []

    def hook(doc):
        seen.append(doc)
        return {"truth_target_shape": 0.002}

    r = the_record(tmp_path / "o", runner.run(photos, tmp_path / "o", name="t", keep=True, check_points=("1", "3"),
                                              after={"align_cameras": hook}))
    job = json.loads((tmp_path / "o" / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["check"] == ["1", "3"] and job["after"] == ["align_cameras"]
    assert r["results"]["truth_target_shape"] == 0.002 and r["valid"] is True
    assert len(seen) == 1 and len(seen[0].chunks) == 1
    # a hook whose step left no project (the child did not reach it) is an error of the results
    r = the_record(tmp_path / "n", runner.run(photos, tmp_path / "n", name="t", after={"update_transform": hook}))
    assert r["results"]["error"].startswith("extra_results: ") and r["valid"] is False


def test_the_weak_ring_is_a_set_of_the_cli_and_its_options_are_checked(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.testing import synthetic

    pytest.importorskip("psutil")
    monkeypatch.setattr(runner, "run", lambda *a, **k: pytest.fail("the runner must not start"))
    monkeypatch.setattr(synthetic, "write_weak_ring_dataset", lambda *a, **k: pytest.fail("nothing is rendered"))
    invoke = lambda *args: CliRunner().invoke(cli.app, ["bench", "synthetic", *args])  # noqa: E731
    result = invoke("weak-ring", str(tmp_path / "a"), "--mapper", "bogus")
    assert result.exit_code == 2 and "--mapper" in result.output
    result = invoke("weak-ring", str(tmp_path / "a"), "--steps", "all")
    assert result.exit_code == 2 and "--steps" in result.output
    result = invoke("trench", str(tmp_path / "a"), "--mapper", "bogus")
    assert result.exit_code == 2
    calls = []
    monkeypatch.setattr(runner, "run", lambda *a, **k: calls.append((a, k)) or [])

    def write(out_dir, **kwargs):
        (Path(out_dir) / "photos").mkdir(parents=True)
        (Path(out_dir) / "photos" / "WRING_0000.jpg").write_bytes(b"")
        return types.SimpleNamespace(crs="EPSG:3004", targets={}, cameras=[{"label": "WRING_0000"}], diameter=60.0)

    monkeypatch.setattr(synthetic, "write_weak_ring_dataset", write)
    result = invoke("weak-ring", str(tmp_path / "a"), "--steps", "optimize", "--mapper", "global", "--repeat", "1")
    assert result.exit_code == 0, result.output
    (photos, out, name), kwargs = calls[-1]
    assert name == "synthetic-weak-ring" and photos == tmp_path / "a" / "synthetic-weak-ring-set" / "photos"
    assert kwargs["steps"] == "optimize" and kwargs["mapper"] == "global" and kwargs["repeat"] == 1
    assert kwargs["targets"] == steps.DEFAULT_TARGETS and kwargs["crs"] == "EPSG:3004"
    assert kwargs["reference"] == tmp_path / "a" / "synthetic-weak-ring-set" / "gcp.csv"
    assert kwargs["check_points"] == ("1", "3", "5", "7", "9", "11")
    assert list(kwargs["after"]) == ["update_transform"] and callable(kwargs["extra_results"])


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


def test_an_unknown_engine_is_refused_before_any_child(tmp_path, monkeypatch):
    pytest.importorskip("psutil")
    photos = noise_photos(tmp_path / "photos")
    monkeypatch.setattr(runner.subprocess, "Popen", child_only(no_child))
    for engine in ("turbo", "Native", "", None):
        with pytest.raises(ValueError, match="engine must be auto, classic or native"):
            runner.run(photos, tmp_path / "out", name="t", engine=engine)
        with pytest.raises(ValueError, match="engine must be"):
            runner.check(tmp_path / "out", "t", engine=engine)
    assert not (tmp_path / "out").exists()
    for engine in steps.ENGINES:
        runner.check(tmp_path / "out", "t", engine=engine)  # the three choices pass the checks
    runner.check(tmp_path / "out", "t", steps="match")  # the chain of the matching alone is one of the three


def test_an_unknown_engine_of_the_alignment_is_refused_before_any_child(tmp_path, monkeypatch):
    pytest.importorskip("psutil")
    photos = noise_photos(tmp_path / "photos")
    monkeypatch.setattr(runner.subprocess, "Popen", child_only(no_child))
    for engine in ("bogus", "Native", "", None):
        with pytest.raises(ValueError, match="align_engine must be auto, classic or native"):
            runner.run(photos, tmp_path / "out", name="t", align_engine=engine)
        with pytest.raises(ValueError, match="align_engine must be"):
            runner.check(tmp_path / "out", "t", align_engine=engine)
    assert not (tmp_path / "out").exists()
    for engine in steps.ENGINES:
        runner.check(tmp_path / "out", "t", align_engine=engine)  # the three choices pass the checks
    # the engine of the matching is refused first, as the options of the command come
    with pytest.raises(ValueError, match="^engine must be"):
        runner.check(tmp_path / "out", "t", engine="turbo", align_engine="bogus")


# --- the run on the small set -----------------------------------------------------------------------------------------

def test_a_small_run_writes_one_record(small_set, tmp_path, monkeypatch):
    monkeypatch.setenv("PPG_ENGINE", "classic")  # the engine the computer asks for is not this run's: the job's, auto
    out = tmp_path / "out"
    paths = runner.run(small_set, out, name="t", extra_results=lambda doc: {"chunks": len(doc.chunks)})
    r = the_record(out, paths)
    assert list(r) == list(record.KEYS) and r["schema"] == 1 and r["name"] == "t"
    assert [s["step"] for s in r["steps"]] == ["match_photos", "align_cameras"]
    for s in r["steps"]:
        assert set(s) == STEP_KEYS and s["seconds"] > 0 and s["peak_memory_mb"] > 0
    # the matching with the engine of the run (auto, the default: the native engine since the native matching passed
    # its gate, the classic one where the native core is missing), the alignment with the run's engine of the
    # alignment (auto, the default: the classic one, which auto also picks for 14 photos without GPS)
    native = _native.native_match_kernels() is not None
    used = "native" if native else "classic"
    assert r["steps"][0]["engine"] == {"requested": "auto", "used": used, "fallback": None}
    assert r["steps"][1]["engine"] == AUTO_CLASSIC
    assert {"features", "pairs"} <= set(r["steps"][0]["stages"]) and isinstance(r["steps"][1]["stages"], dict)
    assert r["set"] == {"photos": 14, "bytes": sum(p.stat().st_size for p in small_set.iterdir()), "online_only": 0,
                        "gps": 0}
    assert r["results"]["aligned"] == r["set"]["photos"] == r["results"]["cameras"] == 14
    assert r["results"]["reprojection_rms_px"] > 0 and r["results"]["chunks"] == 1
    # the pairs the matching verified (14 photos: at most 91) and the tie points a registered photo sees on average
    # (to a tenth), the two numbers the gate of the native matching reads against the classic one
    assert 14 <= r["results"]["verified_pairs"] <= 91 and isinstance(r["results"]["verified_pairs"], int)
    assert r["results"]["tie_points_per_photo"] > 100 and r["results"]["tie_points_per_photo"] == \
        round(r["results"]["tie_points_per_photo"], 1)
    assert list(r["settings"]) == SETTINGS
    # pairs: None when every pair of a small set is matched on the CPU alone by the classic engine (no GPU, as on the
    # CI runners): the exhaustive matching of COLMAP lists no pairs (engine/sfm.py, the pair step of match_photos)
    pairs = r["settings"]["pairs"]
    assert r["settings"]["quality"] == "high" and (pairs is None or pairs > 0) and r["settings"]["mapper"]
    # the mapper asked for (the default) beside the one the run used: auto picks incremental on 14 photos without GPS
    assert r["settings"]["mapper_requested"] == "auto" and r["settings"]["mapper"] == "incremental"
    # the engine asked for beside what the record of the matching says: the native record names its engine, where
    # the points were found and the routes of the pairs; the classic record names no engine, its features cpu or
    # gpu, no routes
    assert r["settings"]["engine_requested"] == "auto"
    # the engine of the alignment asked for, and no fallback of the native mapper, which was not asked
    assert r["settings"]["align_engine_requested"] == "auto" and r["settings"]["native_fallback"] is None
    if native:
        assert r["settings"]["engine"] == "native" and r["settings"]["features"] in ("cpu", "gpu", "gpu+cpu")
        assert sum(r["settings"]["routes"].values()) > 0
    else:
        assert r["settings"]["engine"] is None
        assert r["settings"]["features"] in ("cpu", "gpu") and r["settings"]["routes"] is None
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


def test_a_match_run_on_the_small_set_matches_and_stops(small_set, tmp_path):
    """``--steps match`` on the smallest set: the matching alone, done, its verified pairs the one result."""
    out = tmp_path / "out"
    r = the_record(out, runner.run(small_set, out, name="t", steps="match"))
    assert [s["step"] for s in r["steps"]] == ["match_photos"] and r["error"] is None
    used = "native" if _native.native_match_kernels() is not None else "classic"  # auto: native since its gate
    assert r["steps"][0]["engine"] == {"requested": "auto", "used": used, "fallback": None}
    assert list(r["results"]) == ["verified_pairs"] and 14 <= r["results"]["verified_pairs"] <= 91
    assert r["settings"]["engine_requested"] == "auto" and r["settings"]["mapper"] is None  # no alignment ran
    assert list(r["settings"]) == SETTINGS and r["valid"] == (r["invalid_reasons"] == [])
    assert not (out / "t-1").exists()


def test_a_small_run_aligned_by_the_native_mapper_says_so_in_its_record(small_set, tmp_path):
    """``align_engine="native"`` on the smallest set: the alignment is the native global mapper's, the engine of
    the step says so and the settings keep what was asked and no fallback; where the native core has no mapper the
    classic alignment runs and the engine of the step keeps the reason."""
    out = tmp_path / "out"
    r = the_record(out, runner.run(small_set, out, name="t", align_engine="native"))
    assert [s["step"] for s in r["steps"]] == ["match_photos", "align_cameras"] and r["error"] is None
    assert r["settings"]["align_engine_requested"] == "native" and r["settings"]["engine_requested"] == "auto"
    assert r["settings"]["mapper_requested"] == "auto" and list(r["settings"]) == SETTINGS
    engine = r["steps"][1]["engine"]
    assert set(engine) == {"requested", "used", "fallback"} and engine["requested"] == "native"
    if _native.native_mapper() is not None:
        assert engine == {"requested": "native", "used": "native", "fallback": None}
        assert r["settings"]["native_fallback"] is None and r["settings"]["mapper"] == "global"
        # the stages of the native mapper, timed under the names COLMAP's global mapper gives its own
        assert {"view_graph", "rotation_averaging", "global_positioning", "bundle_adjustment"} <= \
            set(r["steps"][1]["stages"])
    else:  # no mapper in the native core of this computer: the classic alignment, and why
        assert engine["used"] == "classic" and "is not installed" in engine["fallback"]
        assert r["settings"]["native_fallback"] is None and r["settings"]["mapper"] == "incremental"
    assert r["results"]["aligned"] == r["results"]["cameras"] == 14 and r["results"]["reprojection_rms_px"] > 0
    for where, text in strings_of(r):  # the report of the native mapper stays in the project: no name, no path
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
        plan = runner._Plan(name=name, steps="align", mapper="auto", engine="auto", local=(), out=tmp_path / "out",
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


def test_the_engine_asked_for_goes_to_the_child_in_the_job_and_never_through_the_environment(tmp_path, monkeypatch,
                                                                                            stand_in):
    """``engine`` reaches the child in the job (``steps.call`` gives it to the matching) and the record keeps it as
    ``engine_requested``; ``PPG_ENGINE`` of the parent's environment is not passed on, so the engine the computer
    asks for never changes what a run measures, nor the steps the bench keeps classic."""
    monkeypatch.setenv("PPG_ENGINE", "native")
    photos = noise_photos(tmp_path / "photos")
    r = the_record(tmp_path / "classic", runner.run(photos, tmp_path / "classic", name="t", engine="classic",
                                                    keep=True))
    job = json.loads((tmp_path / "classic" / "t-1" / "job.json").read_text(encoding="utf-8"))
    (_cmd, kwargs), = stand_in.calls
    assert job["engine"] == "classic" and r["settings"]["engine_requested"] == "classic"
    assert "PPG_ENGINE" not in kwargs["env"] and list(r["settings"]) == SETTINGS
    r = the_record(tmp_path / "native", runner.run(photos, tmp_path / "native", name="t", engine="native", keep=True))
    job = json.loads((tmp_path / "native" / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["engine"] == "native" and r["settings"]["engine_requested"] == "native"
    r = the_record(tmp_path / "auto", runner.run(photos, tmp_path / "auto", name="t", keep=True))
    job = json.loads((tmp_path / "auto" / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["engine"] == "auto" and r["settings"]["engine_requested"] == "auto"
    assert all("PPG_ENGINE" not in kwargs["env"] for _cmd, kwargs in stand_in.calls)
    # the chain of the matching alone goes to the child as its steps
    r = the_record(tmp_path / "match", runner.run(photos, tmp_path / "match", name="t", steps="match", keep=True))
    job = json.loads((tmp_path / "match" / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["steps"] == "match" and job["targets"] is None and r["settings"]["targets"] is None


def test_the_engine_of_the_alignment_asked_for_goes_to_the_child_in_the_job_and_is_kept_in_the_record(
        tmp_path, monkeypatch, stand_in):
    """``align_engine`` reaches the child in the job (``steps.call`` gives it to the alignment) and the record keeps
    it as ``align_engine_requested``, beside the engine of the matching and apart from it; ``PPG_ENGINE`` of the
    parent's environment is not passed on."""
    monkeypatch.setenv("PPG_ENGINE", "native")
    photos = noise_photos(tmp_path / "photos")
    r = the_record(tmp_path / "native", runner.run(photos, tmp_path / "native", name="t", align_engine="native",
                                                   keep=True))
    job = json.loads((tmp_path / "native" / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["align_engine"] == "native" and job["engine"] == "auto"
    assert r["settings"]["align_engine_requested"] == "native" and r["settings"]["engine_requested"] == "auto"
    assert r["settings"]["native_fallback"] is None and list(r["settings"]) == SETTINGS
    r = the_record(tmp_path / "classic", runner.run(photos, tmp_path / "classic", name="t", engine="native",
                                                    align_engine="classic", keep=True))
    job = json.loads((tmp_path / "classic" / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["align_engine"] == "classic" and job["engine"] == "native"
    assert r["settings"]["align_engine_requested"] == "classic" and r["settings"]["engine_requested"] == "native"
    r = the_record(tmp_path / "auto", runner.run(photos, tmp_path / "auto", name="t", keep=True))
    job = json.loads((tmp_path / "auto" / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["align_engine"] == "auto" and r["settings"]["align_engine_requested"] == "auto"
    assert all("PPG_ENGINE" not in kwargs["env"] for _cmd, kwargs in stand_in.calls)


def test_align_engine_native_of_the_command_reaches_the_alignment_of_the_chunk(tmp_path, monkeypatch, stand_in):
    """``ppg bench run --align-engine native``: the word goes from the command to the runner, into the job of the
    child and, by ``steps.call``, to ``Chunk.align_cameras(engine="native")``; the record keeps it. Without the
    option the alignment is asked for ``auto``."""
    from typer.testing import CliRunner

    monkeypatch.setattr(steps, "_rss_mb", lambda: 50.0)
    photos = noise_photos(tmp_path / "photos")

    def aligned_with(folder: str, *options) -> tuple[dict, dict]:
        """The keywords ``Chunk.align_cameras`` gets in the chain of the job a run of the command wrote, and the
        settings of its record."""
        out = tmp_path / folder
        result = CliRunner().invoke(cli.app, ["bench", "run", str(photos), str(out), "--name", "t", "--keep",
                                              *options])
        assert result.exit_code == 0, result.output
        job = json.loads((out / "t-1" / "job.json").read_text(encoding="utf-8"))
        calls = []
        assert steps.run_chain(stub_chunk(calls, []), job, steps.Result(tmp_path / f"{folder}.json")) is True
        assert [c[0] for c in calls] == ["match_photos", "align_cameras"]
        (written,) = out.glob("t-*.json")
        return calls[1][2], json.loads(written.read_text(encoding="utf-8"))["settings"]

    kwargs, settings = aligned_with("native", "--align-engine", "native")
    assert kwargs == {"mapper": "auto", "engine": "native"}
    assert settings["align_engine_requested"] == "native" and settings["engine_requested"] == "auto"
    kwargs, settings = aligned_with("both", "--align-engine", "classic", "--engine", "native", "--mapper", "global")
    assert kwargs == {"mapper": "global", "engine": "classic"}
    assert settings["align_engine_requested"] == "classic" and settings["engine_requested"] == "native"
    kwargs, settings = aligned_with("default")
    assert kwargs == {"mapper": "auto", "engine": "auto"} and settings["align_engine_requested"] == "auto"


def test_align_engine_is_an_option_of_run_and_of_synthetic_checked_before_anything_runs(tmp_path, monkeypatch):
    """``--align-engine`` of ``ppg bench run`` and of ``ppg bench synthetic`` (every set): a word that is not one of
    the three engines is a usage error (exit 2) that names the option, before a set is rendered or a run starts;
    one of the three goes to the runner as ``align_engine``, ``auto`` without the option."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.testing import synthetic

    pytest.importorskip("psutil")
    invoke = lambda *args: CliRunner().invoke(cli.app, ["bench", *args])  # noqa: E731
    words = lambda result: " ".join(re.sub(r"[\u2500-\u257f]", " ", result.output).split())  # noqa: E731
    photos = noise_photos(tmp_path / "photos")
    monkeypatch.setattr(runner, "run", lambda *a, **k: pytest.fail("the runner must not start"))
    for writer in ("write_dataset", "write_ring_dataset", "write_weak_ring_dataset"):
        monkeypatch.setattr(synthetic, writer, lambda *a, **k: pytest.fail("nothing is rendered"))
    result = invoke("run", str(photos), str(tmp_path / "o"), "--name", "n", "--align-engine", "bogus")
    assert result.exit_code == 2 and "--align-engine" in words(result) and "not 'bogus'" in words(result)
    assert "auto, classic or native" in words(result)
    for which in ("trench", "ring", "weak-ring"):
        result = invoke("synthetic", which, str(tmp_path / "s"), "--align-engine", "bogus")
        assert result.exit_code == 2 and "--align-engine" in words(result) and "not 'bogus'" in words(result), which
    assert not (tmp_path / "o").exists() and not (tmp_path / "s").exists()
    # the engine of the matching is its own option: a wrong word there names --engine, not --align-engine
    result = invoke("run", str(photos), str(tmp_path / "o"), "--name", "n", "--engine", "turbo", "--align-engine",
                    "native")
    assert result.exit_code == 2 and "--align-engine" not in words(result) and "not 'turbo'" in words(result)
    calls = []
    monkeypatch.setattr(runner, "run", lambda *a, **k: calls.append((a, k)) or [])
    result = invoke("run", str(photos), str(tmp_path / "o"), "--name", "n", "--align-engine", "native")
    assert result.exit_code == 0, result.output
    assert calls[-1][1]["align_engine"] == "native" and calls[-1][1]["engine"] == "auto"
    result = invoke("run", str(photos), str(tmp_path / "o"), "--name", "n")
    assert result.exit_code == 0 and calls[-1][1]["align_engine"] == "auto"

    def write(out_dir, **kwargs):
        (Path(out_dir) / "photos").mkdir(parents=True)
        (Path(out_dir) / "photos" / "SET_0000.jpg").write_bytes(b"")
        (Path(out_dir) / "gcp.csv").write_text("# label,X,Y,Z (EPSG:3004)\n", encoding="utf-8")
        return types.SimpleNamespace(crs="EPSG:3004", targets={}, cameras=[{"label": "SET_0000"}], diameter=60.0)

    for writer in ("write_dataset", "write_ring_dataset", "write_weak_ring_dataset"):
        monkeypatch.setattr(synthetic, writer, write)
    for which, chain in (("trench", "all"), ("ring", "align"), ("weak-ring", "optimize")):
        options = ("--steps", "optimize") if which == "weak-ring" else ()
        result = invoke("synthetic", which, str(tmp_path / which), "--align-engine", "native", "--repeat", "1",
                        *options)
        assert result.exit_code == 0, result.output
        (_photos, _out, name), kwargs = calls[-1]
        assert name == f"synthetic-{which}" and kwargs["steps"] == chain
        assert kwargs["align_engine"] == "native" and kwargs["engine"] == "auto" and "mapper" not in kwargs
    result = invoke("synthetic", "weak-ring", str(tmp_path / "plain"), "--repeat", "1")
    assert result.exit_code == 0 and calls[-1][1]["align_engine"] == "auto"


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


# --- the real sets: the matches of a kept run, the markers of a project, the optimization ---------------------------

def stamps(*files: Path) -> list[tuple[bytes, int]]:
    """The bytes and the time of change of each file: what a run must leave as it found it."""
    return [(f.read_bytes(), f.stat().st_mtime_ns) for f in files]


def folder_listing(folder: Path) -> dict[str, tuple[int, int]]:
    """Every file under ``folder``, by its path under it, with its size and its time of change."""
    return {str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in folder.rglob("*") if p.is_file()}


def kept_database(work: Path) -> Path:
    (db,) = work.rglob("database.db")
    return db


def test_the_optimization_of_the_job_reaches_optimize_cameras(tmp_path, monkeypatch):
    """``optimize`` and ``solver`` of the job are the ``engine`` and the ``solver`` of ``Chunk.optimize_cameras``;
    without them the published optimization, COLMAP's formulation with the solver of the program."""
    from pyarchinit_photogrammetry.markers import gcp

    monkeypatch.setattr(gcp, "camera_reference_errors", lambda chunk: {})
    monkeypatch.setattr(gcp, "marker_errors", lambda chunk: [])
    monkeypatch.setattr(steps, "_rss_mb", lambda: 50.0)
    for job_part, expected in (({}, {"engine": "colmap", "solver": "auto"}),
                               ({"optimize": "ppg", "solver": "native"}, {"engine": "ppg", "solver": "native"})):
        calls = []
        chunk = stub_chunk(calls, [], report=REPORT)
        chunk.markers = []
        job = {"steps": "optimize", "targets": None, "reference": "/data/gcp.csv", "crs": None, **job_part}
        assert steps.run_chain(chunk, job, steps.Result(tmp_path / "result.json")) is True
        assert calls[-1] == ("optimize_cameras", (), expected)


def test_the_options_of_the_real_sets_are_checked_before_anything_runs(tmp_path, monkeypatch):
    """``--optimize`` (colmap or ppg), ``--solver`` (auto, native or scipy; COLMAP's adjustment has none to choose),
    both for ``--steps optimize`` only; ``--matches-from`` a kept run and ``--markers`` a project, neither with
    ``--steps match``: each refusal a usage error (exit 2) before a child starts, none naming a path."""
    from typer.testing import CliRunner

    pytest.importorskip("psutil")
    monkeypatch.setattr(runner.subprocess, "Popen", child_only(no_child))
    photos = noise_photos(tmp_path / "photos")
    invoke = lambda *args: CliRunner().invoke(cli.app, ["bench", "run", str(photos), str(tmp_path / "o"),  # noqa
                                                        "--name", "n", *args])
    words = lambda result: " ".join(re.sub(r"[─-╿]", " ", result.output).split())  # noqa: E731
    for args, said in ((("--steps", "optimize", "--optimize", "auto"), "--optimize"),
                       (("--steps", "optimize", "--optimize", "ppg", "--solver", "fast"), "--solver"),
                       (("--steps", "optimize", "--solver", "native"), "--solver"),
                       (("--optimize", "ppg"), "--optimize"), (("--solver", "auto"), "--solver")):
        result = invoke(*args)
        assert result.exit_code == 2 and said in words(result), (args, result.output)
    empty = tmp_path / "KeptRunX"
    empty.mkdir()
    for args in (("--matches-from", str(empty)), ("--matches-from", str(tmp_path / "MissingY")),
                 ("--markers", str(tmp_path / "MissingZ.ppg")), ("--markers", str(empty))):
        result = invoke(*args)
        assert result.exit_code == 2 and args[0] in words(result), (args, result.output)
        assert not any(w in words(result).replace(" ", "") for w in ("KeptRunX", "MissingY", "MissingZ")), \
            result.output
    assert "project.ppg" in words(invoke("--matches-from", str(empty)))  # what is missing, never where
    # a folder with a project but no database: it was not matched
    (empty / "project.ppg").write_text(json.dumps({"format": "pyarchinit-photogrammetry", "version": 1, "meta": {},
                                                   "chunks": [{"key": 0, "label": "Chunk 1", "cameras": []}]}))
    result = invoke("--matches-from", str(empty))
    assert result.exit_code == 2 and "database.db" in words(result) and "KeptRunX" not in words(result)
    with pytest.raises(ValueError, match="steps match"):
        runner.check(tmp_path / "o", "n", "match", photos=photos, markers=tmp_path / "none.ppg")
    with pytest.raises(ValueError, match="solver"):
        runner.check(tmp_path / "o", "n", "optimize", photos=photos, optimize="colmap", solver="scipy")
    assert not (tmp_path / "o").exists()


def test_the_optimization_asked_for_goes_to_the_child_and_is_kept_in_the_settings(tmp_path, monkeypatch, stand_in):
    from typer.testing import CliRunner

    photos = noise_photos(tmp_path / "photos")
    out = tmp_path / "o"
    result = CliRunner().invoke(cli.app, ["bench", "run", str(photos), str(out), "--name", "t", "--keep", "--steps",
                                          "optimize", "--optimize", "ppg", "--solver", "scipy"])
    assert result.exit_code == 0, result.output
    job = json.loads((out / "t-1" / "job.json").read_text(encoding="utf-8"))
    assert job["optimize"] == "ppg" and job["solver"] == "scipy"
    (written,) = out.glob("t-*.json")
    settings = json.loads(written.read_text(encoding="utf-8"))["settings"]
    assert settings["optimize_engine"] == "ppg" and settings["optimize_solver"] == "scipy"
    assert settings["matches_reused"] is False and settings["markers"] is None and settings["checks"] is None
    r = the_record(tmp_path / "d", runner.run(photos, tmp_path / "d", name="t", steps="optimize"))
    assert r["settings"]["optimize_engine"] == "colmap" and r["settings"]["optimize_solver"] == "auto"
    r = the_record(tmp_path / "a", runner.run(photos, tmp_path / "a", name="t"))  # no optimization in the chain
    assert r["settings"]["optimize_engine"] is None and r["settings"]["optimize_solver"] is None


def test_the_variables_of_the_native_mapper_reach_the_child_and_the_settings(tmp_path, monkeypatch, stand_in):
    """``PPG_NATIVE_TRACKS_PER_PHOTO`` and ``PPG_NATIVE_FINAL_ALL_TRACKS`` of the parent reach the child's
    environment and are kept as ``native_tracks_per_photo`` (a whole number) and ``native_final_all_tracks``
    (False for ``0``, as the mapper reads it); None when unset. A count that is not a whole number is refused before
    a child starts."""
    from pyarchinit_photogrammetry.engine import native_map

    photos = noise_photos(tmp_path / "photos")
    monkeypatch.setenv(native_map.TRACKS_ENV, "300")
    monkeypatch.setenv(native_map.FINAL_ALL_TRACKS_ENV, "0")
    r = the_record(tmp_path / "set", runner.run(photos, tmp_path / "set", name="t"))
    (_cmd, kwargs), = stand_in.calls
    assert kwargs["env"][native_map.TRACKS_ENV] == "300" and kwargs["env"][native_map.FINAL_ALL_TRACKS_ENV] == "0"
    assert r["settings"]["native_tracks_per_photo"] == 300 and r["settings"]["native_final_all_tracks"] is False
    monkeypatch.setenv(native_map.FINAL_ALL_TRACKS_ENV, "1")
    r = the_record(tmp_path / "on", runner.run(photos, tmp_path / "on", name="t"))
    assert r["settings"]["native_final_all_tracks"] is True
    monkeypatch.setenv(native_map.TRACKS_ENV, " ")  # blank: unset, as the mapper reads it
    monkeypatch.delenv(native_map.FINAL_ALL_TRACKS_ENV)
    r = the_record(tmp_path / "unset", runner.run(photos, tmp_path / "unset", name="t"))
    assert r["settings"]["native_tracks_per_photo"] is None and r["settings"]["native_final_all_tracks"] is None
    monkeypatch.setenv(native_map.TRACKS_ENV, "many")
    with pytest.raises(ValueError, match=native_map.TRACKS_ENV):
        runner.run(photos, tmp_path / "bad", name="t")
    assert len(stand_in.calls) == 3 and not (tmp_path / "bad").exists()


def test_a_run_on_the_matches_of_a_kept_run_aligns_without_matching(small_set, tmp_path):
    """``matches_from`` a work folder kept by ``keep``: the run copies its project and its database (and the native
    stores), drops the alignment and starts at ``align_cameras``; the record has no ``match_photos``,
    ``matches_reused`` true, and the kept folder is left as it was."""
    first = runner.run(small_set, tmp_path / "kept", name="k", keep=True)
    kept = tmp_path / "kept" / "k-1"
    files = (kept / "project.ppg", kept_database(kept))
    store = files[1].parent / steps.NATIVE_STORES / "features" / "probe.store"  # a store of the native matching
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"store of the kept run")
    before = stamps(*files)
    listing = folder_listing(kept)
    out = tmp_path / "out"
    r = the_record(out, runner.run(small_set, out, name="t", matches_from=kept, keep=True))
    assert [s["step"] for s in r["steps"]] == ["align_cameras"] and r["error"] is None
    assert r["settings"]["matches_reused"] is True and list(r["settings"]) == SETTINGS
    assert r["results"]["aligned"] == r["results"]["cameras"] == 14 and r["results"]["reprojection_rms_px"] > 0
    assert stamps(*files) == before and len(first) == 1
    assert folder_listing(kept) == listing  # no file gained, lost or changed in the kept folder
    copied = kept_database(out / "t-1")
    assert copied != files[1]
    assert (copied.parent / store.relative_to(files[1].parent)).read_bytes() == b"store of the kept run"
    for where, text in strings_of(r):
        assert str(tmp_path) not in text, where


def markers_project(scene_dir: Path, folder: Path) -> Path:
    """A project of the trench (all its photos) with its targets detected and measured, targets 4 and 5 check
    points; never aligned."""
    doc = ppg.Document()
    chunk = doc.add_chunk("Saggio")
    doc.save(folder / "markers.ppg")
    chunk.add_photos([scene_dir / "photos"])
    chunk.detect_markers("aruco_4x4_50")
    chunk.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    for m in chunk.markers:
        m.checkpoint = m.code in (4, 5)
    doc.save()
    return folder / "markers.ppg"


def test_a_run_with_the_markers_of_a_project_optimizes_on_them(small_set, scene_dir, tmp_path):
    """``markers`` a project whose chunk has markers measured: before the alignment the child copies every marker
    (label, reference, check flag, its projections by camera label) into its chunk, leaving out and counting the
    projections on photos the run does not have; the optimization then gives the control and check errors. The
    record holds counts, never a label; the project is never written."""
    project = markers_project(scene_dir, tmp_path / "m")
    before = stamps(project)
    source = ppg.Document.open(project).chunks[0]
    names = {p.stem for p in small_set.iterdir()}
    labels = {c.key: c.label for c in source.cameras}
    left_out = sum(1 for m in source.markers for k in m.projections if labels[k] not in names)
    assert left_out > 0
    out = tmp_path / "out"
    r = the_record(out, runner.run(small_set, out, name="t", steps="optimize", markers=project, keep=True))
    assert r["error"] is None, r["error"]
    assert [s["step"] for s in r["steps"]] == ["match_photos", "align_cameras", "update_transform",
                                               "optimize_cameras"]
    assert r["settings"]["markers"] == len(source.markers)
    assert r["settings"]["checks"] == sum(m.checkpoint for m in source.markers) == 2
    assert r["results"]["markers_projections_left_out"] == left_out
    assert r["results"]["optimized_control_rmse"] < 0.01 and r["results"]["optimized_check_rmse"] < 0.01
    assert r["settings"]["optimize_engine"] == "colmap" and r["settings"]["optimize_solver"] == "auto"
    run_chunk = ppg.Document.open(out / "t-1" / "project.ppg").chunks[0]
    assert sorted((m.label, m.checkpoint) for m in run_chunk.markers) == \
        sorted((m.label, m.checkpoint) for m in source.markers)
    run_labels = {c.key: c.label for c in run_chunk.cameras}
    for m in run_chunk.markers:
        src = next(s for s in source.markers if s.label == m.label)
        assert m.reference.location == src.reference.location
        assert {run_labels[k] for k in m.projections} == {labels[k] for k in src.projections} & names
    assert stamps(project) == before
    for where, text in strings_of(r):
        assert not any(m.label in text for m in source.markers) and str(tmp_path) not in text, where


def test_matches_from_compares_the_kept_photos_with_the_local_ones(tmp_path, monkeypatch, stand_in):
    """The photos of the kept run are compared with the photos of this run that are not online-only, as the child
    compares them; the refusal counts the online-only photos and names none."""
    photos = noise_photos(tmp_path / "photos")
    kept = tmp_path / "kept"
    project = kept / "project.ppg"
    (project.parent / "project.ppg.files" / "chunk_0" / "sfm").mkdir(parents=True)
    (project.parent / "project.ppg.files" / "chunk_0" / "sfm" / "database.db").write_bytes(b"")
    cameras = [{"path": str(p)} for p in sorted(photos.iterdir())]
    project.write_text(json.dumps({"format": "pyarchinit-photogrammetry", "chunks": [{"key": 0, "cameras": cameras}]}))
    runner.check(tmp_path / "o", "t", photos=photos, matches_from=kept)  # the same photos: accepted
    online = sorted(photos.iterdir())[0]
    monkeypatch.setattr(machine, "is_online_only", lambda p: Path(p).name == online.name)
    with pytest.raises(ValueError, match="1 online-only") as refused:
        runner.check(tmp_path / "o", "t", photos=photos, matches_from=kept)
    assert online.stem not in str(refused.value) and str(tmp_path) not in str(refused.value)
    assert not stand_in.calls
