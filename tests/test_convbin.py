"""Raw receiver logs converted to RINEX inside the native core (``raw_to_rinex``, API 11) as the official ``convbin``
of RTKLIB-EX 2.5.1 converts them: tests/data/gnss/phase4 holds samples of RTKLIB's test data and what ``convbin`` made
of them, described in its README.md. Time marks of a u-blox log (TIM-TM2) become event records (flag 5); damaged
files, cancelled runs and refused formats leave no file behind. Without the core the program runs a ``convbin`` it is
given, and without either it says what to install."""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

import convbin_helpers as ubx
from pyarchinit_photogrammetry import _native

DATA = Path(__file__).parent / "data" / "gnss" / "phase4"
RTKLIB_DATA = Path(__file__).parent / "data" / "rtklib"

CORE = _native.kernel("raw_to_rinex") is not None and (_native.api() or 0) >= _native.CONVBIN_API
needs_the_core = pytest.mark.skipif(not CORE, reason="native core without convbin (older than 0.8, API 11)")

GPS_EPOCH = datetime(1980, 1, 6)
WEEK_1481 = 1481 * ubx.WEEK
APPROX_GMSD7 = (datetime(2012, 10, 14) - GPS_EPOCH).total_seconds()  # convbin -tr 2012/10/14 00:00:00

# the time marks of README.md: the frames and the GPS time each one is put after
MARKS = [
    (ubx.tim_tm2(1481, 107975.5, count=1), WEEK_1481 + 107975.5),
    (ubx.tim_tm2(1481, 107990.25 - 14, flags=ubx.FALLING | ubx.BASE_UTC | ubx.TIME_VALID, count=2),
     WEEK_1481 + 107990.25),
    (ubx.tim_tm2(1481, 108005.123456789, count=3), WEEK_1481 + 108005.123456789),
    (ubx.tim_tm2(1481, 108015.0, flags=(1 << 7) | ubx.BASE_GNSS | ubx.TIME_VALID, count=4), WEEK_1481 + 108015.0),
]
EVENT_LINES = ["> 2008 05 26 05 59 35.5000000  5  0", "> 2008 05 26 05 59 50.2500000  5  0",
               "> 2008 05 26 06 00  5.1234568  5  0"]


def _with_time_marks(folder: Path) -> Path:
    path = folder / "ubx_20080526_64k_tm2.ubx"
    data = (DATA / "ubx_20080526_64k.ubx").read_bytes()
    path.write_bytes(ubx.insert_time_marks(data, [m for m, _ in MARKS], [t for _, t in MARKS]))
    return path


def _comparable(path: Path) -> list[str]:
    """The lines of a RINEX file but those that name the program and the day of the run, and the comment that names
    the input file (``log: <path>``, on more lines when the path is long: RTKLIB cuts it every 60 characters)."""
    out, in_log = [], False
    for line in path.read_text().splitlines():
        comment = line[60:].startswith("COMMENT")
        in_log = comment and (line.startswith("log: ") or (in_log and line.startswith("  ")))
        if not in_log and not line[60:].startswith("PGM / RUN BY / DATE"):
            out.append(line)
    return out


def _assert_same(made: Path, expected: Path) -> None:
    a, b = _comparable(made), _comparable(expected)
    if a != b:
        k = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        pytest.fail(f"{made.name}, line {k + 1}: made {a[k:k + 1]!r}, convbin {b[k:k + 1]!r} ({len(a)} and {len(b)})")


def _convert(fmt: str, src: Path, folder: Path, **kw) -> tuple[dict, Path, Path]:
    obs, nav = folder / (src.stem + ".obs"), folder / (src.stem + ".nav")
    result = _native.raw_to_rinex(fmt, src, obs, nav, **kw)
    return result, obs, nav


@needs_the_core
def test_ubx_as_official_convbin(tmp_path):
    result, obs, nav = _convert("ubx", DATA / "ubx_20080526_64k.ubx", tmp_path)
    assert result["code"] == 0, result
    _assert_same(obs, DATA / "ubx_20080526_64k.obs")
    _assert_same(nav, DATA / "ubx_20080526_64k.nav")
    assert result["epochs"] == 62 and result["events"] == 0 and result["navigation"] > 0
    assert sorted(p.name for p in tmp_path.iterdir()) == [nav.name, obs.name]


@needs_the_core
def test_rtcm3_as_official_convbin(tmp_path):
    result, obs, nav = _convert("rtcm3", DATA / "GMSD7_20121014_64k.rtcm3", tmp_path, approx_gps_seconds=APPROX_GMSD7)
    assert result["code"] == 0, result
    _assert_same(obs, DATA / "GMSD7_20121014_64k.obs")
    _assert_same(nav, DATA / "GMSD7_20121014_64k.nav")
    assert result["epochs"] == 64


@needs_the_core
def test_novatel_as_official_convbin(tmp_path):
    result, obs, nav = _convert("novatel", DATA / "oemv_200911218_64k.gps", tmp_path)
    assert result["code"] == 0, result
    _assert_same(obs, DATA / "oemv_200911218_64k.obs")
    _assert_same(nav, DATA / "oemv_200911218_64k.nav")
    assert result["epochs"] == 11


@needs_the_core
def test_time_marks_become_event_records(tmp_path):
    """Three falling edges, one of them timed in UTC (14 leap seconds in 2008), and a rising edge alone, which RTKLIB
    does not read: three event records at the GPS time of the edges, between the epochs."""
    src = _with_time_marks(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    result, obs, _nav = _convert("ubx", src, out)
    assert result["code"] == 0, result
    assert result["events"] == 3 and result["epochs"] == 62
    _assert_same(obs, DATA / "ubx_20080526_64k_tm2.obs")
    events = [line.rstrip() for line in obs.read_text().splitlines() if line.startswith(">") and line.split()[7] == "5"]
    assert events == EVENT_LINES


def test_rtcm3_needs_an_approximate_time(tmp_path):
    """RTCM 3 carries the time of week only: without an approximate time nothing runs and the message says why."""
    with pytest.raises(ValueError, match="approximate time"):
        _convert("rtcm3", DATA / "GMSD7_20121014_64k.rtcm3", tmp_path)
    assert list(tmp_path.iterdir()) == []


@needs_the_core
def test_rtcm3_needs_an_approximate_time_in_the_core(tmp_path):
    import ppg_core

    code, message, _counts = ppg_core.raw_to_rinex("rtcm3", str(DATA / "GMSD7_20121014_64k.rtcm3"),
                                                   str(tmp_path / "a.obs"), str(tmp_path / "a.nav"), None)
    assert code == -3 and "approximate time" in message
    assert list(tmp_path.iterdir()) == []


@needs_the_core
@pytest.mark.parametrize("data", [b"", bytes(range(256)) * 64, b"\xb5\x62\x02\x10\xff\xff" + b"\x00" * 100])
def test_damaged_file_leaves_nothing(tmp_path, data):
    """Empty, noise, a frame cut short: an error code and a message, and neither RINEX file nor a part of one."""
    src = tmp_path / "in" / "damaged.ubx"
    src.parent.mkdir()
    src.write_bytes(data)
    out = tmp_path / "out"
    out.mkdir()
    result, obs, nav = _convert("ubx", src, out)
    assert result["code"] < 0 and result["message"], result
    assert list(out.iterdir()) == []


@needs_the_core
def test_missing_file_leaves_nothing(tmp_path):
    result, _obs, _nav = _convert("ubx", tmp_path / "nowhere.ubx", tmp_path)
    assert result["code"] == -1 and "nowhere.ubx" in result["message"], result
    assert list(tmp_path.iterdir()) == []


@needs_the_core
@pytest.mark.parametrize("fmt", _native.RAW_FORMATS)
def test_every_format_is_dispatched(tmp_path, fmt):
    """Every accepted format reaches its decoder: noise gives a clean error, no crash and no file. The decoders that
    are not built (no licence checked for them, see native/vendor/rtklib/VERSION) say so."""
    import ppg_core

    src = tmp_path / "noise.bin"
    src.write_bytes(bytes((i * 37 + 11) % 251 for i in range(20000)))
    out = tmp_path / "out"
    out.mkdir()
    code, message, counts = ppg_core.raw_to_rinex(fmt, str(src), str(out / "a.obs"), str(out / "a.nav"),
                                                  APPROX_GMSD7)
    assert code < 0 and message, (code, message)
    if fmt in _native.RAW_FORMATS_BUILT:
        assert code == -2 and "no observations" in message
    else:
        assert code == -6 and "not built" in message
    assert counts["epochs"] == 0
    assert list(out.iterdir()) == []


@needs_the_core
@pytest.mark.parametrize("fmt", ["rinex", "javad", "UBX", "", "rtcm2", "binex"])
def test_other_formats_refused(tmp_path, fmt):
    import ppg_core

    code, message, _counts = ppg_core.raw_to_rinex(fmt, str(DATA / "ubx_20080526_64k.ubx"), str(tmp_path / "a.obs"),
                                                   str(tmp_path / "a.nav"), None)
    assert code == -3 and "format" in message
    with pytest.raises(ValueError, match="format"):
        _native.raw_to_rinex(fmt, DATA / "ubx_20080526_64k.ubx", tmp_path / "a.obs", tmp_path / "a.nav")
    assert list(tmp_path.iterdir()) == []


@needs_the_core
def test_paths_that_rtklib_would_rewrite_are_refused(tmp_path):
    """convrnx expands wild cards and %-keywords in its paths: such names, and paths of 1000 bytes or more, are
    refused before anything runs."""
    import ppg_core

    for obs in (tmp_path / "a*.obs", tmp_path / "a%r.obs", tmp_path / ("x" * 1000 + ".obs")):
        code, message, _counts = ppg_core.raw_to_rinex("ubx", str(DATA / "ubx_20080526_64k.ubx"), str(obs),
                                                       str(tmp_path / "a.nav"), None)
        assert code == -3, message
    assert list(tmp_path.iterdir()) == []


@needs_the_core
def test_other_rinex_versions(tmp_path):
    import ppg_core

    code, message, _ = ppg_core.raw_to_rinex("ubx", str(DATA / "ubx_20080526_64k.ubx"), str(tmp_path / "a.obs"),
                                             str(tmp_path / "a.nav"), None, "3.02")
    assert code == 0, message
    assert tmp_path.joinpath("a.obs").read_text().startswith("     3.02")
    for version in ("2.11", "4.00", "three"):
        code, message, _ = ppg_core.raw_to_rinex("ubx", str(DATA / "ubx_20080526_64k.ubx"), str(tmp_path / "b.obs"),
                                                 str(tmp_path / "b.nav"), None, version)
        assert code == -3 and "RINEX version" in message
    assert not tmp_path.joinpath("b.obs").exists()


def _big_ubx(folder: Path, copies: int = 150) -> Path:
    path = folder / "big.ubx"
    path.write_bytes((DATA / "ubx_20080526_64k.ubx").read_bytes() * copies)
    return path


@needs_the_core
def test_cancel_leaves_nothing(tmp_path):
    import ppg_core

    src = _big_ubx(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    done = threading.Event()

    def cancel():
        while not done.is_set():
            ppg_core.convert_cancel()
            time.sleep(0.002)

    canceller = threading.Thread(target=cancel)
    canceller.start()
    try:
        result, _obs, _nav = _convert("ubx", src, out)
    finally:
        done.set()
        canceller.join()
    assert result["code"] == 1, result
    assert list(out.iterdir()) == []
    # a cancel before a run does not stop it
    ppg_core.convert_cancel()
    result, obs, _nav = _convert("ubx", DATA / "ubx_20080526_64k.ubx", out)
    assert result["code"] == 0 and obs.exists()


PPK_STATIC = """\
pos1-posmode       =static
pos1-frequency     =l1+l2
pos1-soltype       =combined
pos1-elmask        =15
pos1-navsys        =1
pos2-armode        =fix-and-hold
ant2-postype       =rinexhead
out-solformat      =llh
out-outhead        =on
out-timesys        =gpst
out-timeform       =tow
out-height         =ellipsoidal
"""


def _solution_rows(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line.strip() and not line.startswith("%")]


@needs_the_core
def test_convert_and_ppk_never_together(tmp_path):
    """The conversion and the post-processing share RTKLIB's globals: run from two threads at once, each gives what it
    gives alone (they wait for each other on one lock)."""
    conf = tmp_path / "static.conf"
    conf.write_text(PPK_STATIC)
    files = [str(RTKLIB_DATA / name) for name in ("30400920.05o", "07590920.05o", "07590920.05n")]
    code, message = _native.ppk_solve(str(conf), files, str(tmp_path / "alone.pos"))
    assert code == 0, message
    src = _big_ubx(tmp_path, copies=40)
    alone = tmp_path / "alone"
    alone.mkdir()
    first, obs_alone, _ = _convert("ubx", src, alone)
    assert first["code"] == 0, first

    together = tmp_path / "together"
    together.mkdir()
    results = {}

    def convert():
        results["convert"] = _convert("ubx", src, together)

    def solve():
        results["ppk"] = _native.ppk_solve(str(conf), files, str(tmp_path / "together.pos"))

    threads = [threading.Thread(target=convert), threading.Thread(target=solve)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results["ppk"][0] == 0, results["ppk"]
    assert _solution_rows(tmp_path / "together.pos") == _solution_rows(tmp_path / "alone.pos")
    result, obs, _ = results["convert"]
    assert result == first
    assert _comparable(obs) == _comparable(obs_alone)


def _fake_convbin(folder: Path) -> Path:
    """A stand-in convbin: writes its arguments beside the observation file it is asked for, a RINEX line in each
    output, and convbin's last status line on its standard error."""
    script = folder / "convbin_fake.py"
    script.write_text(
        "import sys, json\n"
        "args = sys.argv[1:]\n"
        "obs = args[args.index('-o') + 1]; nav = args[args.index('-n') + 1]\n"
        "open(obs, 'w').write('     3.04           OBSERVATION DATA\\n')\n"
        "open(nav, 'w').write('     3.04           N: GNSS NAV DATA\\n')\n"
        "open(obs + '.args.json', 'w').write(json.dumps(args))\n"
        "sys.stderr.write('scanning: 2008/05/26 05:59:28 G\\r2008/05/26 05:59:25-05/26 06:00:26: O=62 N=9 E=1 T=3 \\n')\n")
    if os.name == "nt":
        launcher = folder / "convbin_fake.bat"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        launcher = folder / "convbin_fake"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
    return launcher


def test_without_the_core_runs_convbin(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("PPG_NATIVE", "0")
    monkeypatch.setenv("PPG_CONVBIN", str(_fake_convbin(tmp_path)))
    out = tmp_path / "out"
    out.mkdir()
    result = _native.raw_to_rinex("rtcm3", DATA / "GMSD7_20121014_64k.rtcm3", out / "b.obs", out / "b.nav",
                                  approx_gps_seconds=APPROX_GMSD7 + 3723.5)
    assert result == {"code": 0, "message": "2008/05/26 05:59:25-05/26 06:00:26: O=62 N=9 E=1 T=3",
                      "epochs": 62, "events": 3, "navigation": 9, "errors": 1}
    args = json.loads((out / "b.obs.part.args.json").read_text())
    assert args[:4] == ["-r", "rtcm3", "-v", "3.04"]
    assert args[args.index("-tr") + 1:args.index("-tr") + 3] == ["2012/10/14", "01:02:03.500"]
    assert args[args.index("-o") + 1] == str(out / "b.obs.part")
    assert ["-y", "S"] == args[args.index("S") - 1:args.index("S") + 1] and "I" in args
    assert args[-1] == str(DATA / "GMSD7_20121014_64k.rtcm3")
    assert (out / "b.obs").read_text().startswith("     3.04") and (out / "b.nav").exists()
    assert not (out / "b.obs.part").exists()
    assert "convbin" in _native.convbin_identity()


def test_without_the_core_a_failing_convbin_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("PPG_NATIVE", "0")
    tool = tmp_path / ("convbin_fails.bat" if os.name == "nt" else "convbin_fails")
    tool.write_text("@exit /b 1\n" if os.name == "nt" else "#!/bin/sh\necho 'log open error' >&2\nexit 1\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PPG_CONVBIN", str(tool))
    out = tmp_path / "out"
    out.mkdir()
    result = _native.raw_to_rinex("ubx", DATA / "ubx_20080526_64k.ubx", out / "a.obs", out / "a.nav")
    assert result["code"] != 0
    assert list(out.iterdir()) == []


def test_without_anything_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setenv("PPG_NATIVE", "0")
    monkeypatch.delenv("PPG_CONVBIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RuntimeError, match=r"pyarchinit-photogrammetry-core.*0\.8"):
        _native.raw_to_rinex("ubx", DATA / "ubx_20080526_64k.ubx", tmp_path / "a.obs", tmp_path / "a.nav")
    assert _native.convbin_identity() == ""
    assert list(tmp_path.iterdir()) == []


def test_formats_known_to_python():
    assert _native.RAW_FORMATS == ("ubx", "rtcm3", "novatel", "sbf", "rt17", "unicore")
    assert set(_native.RAW_FORMATS_BUILT) == {"ubx", "rtcm3", "novatel"}
    assert _native.FEATURES["convbin"] == "raw_to_rinex" and _native.CONVBIN_API == 11
