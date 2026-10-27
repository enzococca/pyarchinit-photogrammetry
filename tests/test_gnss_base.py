"""The base of a PPK from a permanent station (gnss/base.py): the station as the base of a flight, or the user's own
base - a receiver left on a mark nobody surveyed - placed by a static baseline from a station. RTKLIB's sample data
stand in for both: 3040, still for the hour, is the user's base, and 0759, 3.3 km away, the station, with the position
of its RINEX header as its coordinates. The archives are never reached: the downloads are replaced."""
from __future__ import annotations

import hashlib
import logging
import math
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

import test_gnss_ppk
from pyarchinit_photogrammetry.gnss import atx, base, download, frames, ppk, rinex, stations
from test_gnss_ppk import START_2005, _enu, _line, _options, _pos_text, needs_rtklib
from test_gnss_rinex_files import _on_label, _piece, _records, _rewrite_header

fake_rtklib = test_gnss_ppk.fake_rtklib  # the fixture of the tests of the PPK: RTKLIB as a stand-in program

DATA = Path(__file__).parent / "data" / "rtklib"
OWN = DATA / "30400920.05o"  # the user's base: 3040, standing still from 00:00:00 to 00:59:30 every 30 s
STATION_OBS = DATA / "07590920.05o"  # the station: 0759, the same hour
NAV = DATA / "07590920.05n"
ATX_EXCERPT = Path(__file__).parent / "data" / "gnss" / "igs20_excerpt.atx"  # TRM29659.00 NONE: 0.100 m up (synthetic)
# the middle of the hour, 00:29:45 GPS time, as a decimal year of UTC (GPS time was 13 s ahead in 2005)
MID_2005 = frames.decimal_year(datetime(2005, 4, 2, 0, 29, 32))

# The headers of the two stations of GSI do not agree with each other: with 0759 at the position of its own header,
# every way RTKLIB solves this hour puts 3040 about 0.120 m west, 0.001 m north and 0.125 m below the position of the
# header of 3040 - the fixed solutions (L1 alone or L1 and L2, the ionosphere or the troposphere estimated, elevation
# masks from 5 to 20 degrees) within 8 mm of it, the float ones (the ionosphere-free combination too) within 2.1 cm;
# the code alone (DGPS) is decimetres off the header too. The base found is checked against the header of 3040 moved by
# as much, within the 3 cm across and 6 cm in height that a static baseline of an hour holds.
HEADERS_APART = np.array([-0.120, 0.001, -0.125])  # east, north, up (m)


@pytest.fixture(autouse=True)
def gnss_folder(tmp_path, monkeypatch):
    """The downloads and caches of the test in a folder of its own: no copy of igs20.atx from another test."""
    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))
    return tmp_path / "gnss"


def _header_llh(path: Path) -> tuple[float, float, float]:
    return rinex.ecef_to_llh(rinex.header(path).approx_xyz)


def _station_0759() -> stations.Station:
    xyz = rinex.header(STATION_OBS).approx_xyz
    return stations.Station("0759", "GSI 0759", ("IGS",), *_header_llh(STATION_OBS), tuple(xyz))


def _station_position(frame: str = "ITRF2020", epoch: float | None = 2005.25, llh=None) -> ppk.BasePosition:
    lat, lon, h = _header_llh(STATION_OBS) if llh is None else llh
    return ppk.BasePosition(lat, lon, h, "station", frame=frame, epoch=epoch, sd=(0.002, 0.003, 0.006),
                            detail={"station": "0759", "citation": "RTKLIB sample data"})


def _llh(position: ppk.BasePosition) -> np.ndarray:
    return np.array([position.lat, position.lon, position.h])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _no_downloads(monkeypatch) -> None:
    """Any download is a failure of the test."""
    def refused(*args, **kwargs):
        raise AssertionError("nothing is downloaded here")

    for module, name in ((stations, "download_observations"), (stations, "download_navigation"),
                         (stations, "coordinates"), (atx, "atx_file"), (download, "fetch")):
        monkeypatch.setattr(module, name, refused)


def _single_static_row(tmp_path: Path, q: int = 1, llh=None, sd=(0.0011, 0.0007, 0.0024)) -> Path:
    """A solution as RTKLIB writes a single static one: one row, at the time of the first epoch; ``sd`` is sdn, sde,
    sdu, as in the file."""
    lat, lon, h = _header_llh(OWN) if llh is None else llh
    pos = tmp_path / "single.pos"
    pos.write_text(_pos_text([(START_2005, lat, lon, h, q, 7, *sd)]), newline="")
    return pos


# --- the user's base by a static baseline, with RTKLIB --------------------------------------------------------------


@needs_rtklib
def test_baseline_finds_the_base(tmp_path, monkeypatch):
    """3040 as the user's base, 0759 as the station at the position of its header: the mark of 3040 where the header
    of 3040 puts it (moved by the disagreement of the two headers), fixed, 3.335 km from the station, in one position
    of the whole hour that RTKLIB writes as a single row."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    r = base.baseline(OWN, _station_0759(), tmp_path / "work", station_obs=STATION_OBS,
                      station_position=_station_position(), atx=None)
    offset = _enu(_llh(r.position), _header_llh(OWN))[0] - HEADERS_APART
    assert math.hypot(offset[0], offset[1]) < 0.03 and abs(offset[2]) < 0.06, offset
    assert r.q == 1
    assert abs(r.baseline_km - 3.335) < 0.01
    assert ppk.static_position(r.solution) == ((r.position.lat, r.position.lon, r.position.h), 1, r.sd)
    assert r.position.source == "baseline" and r.position.sd == r.sd and all(0.0 < v < 0.01 for v in r.sd)
    assert r.hours == 1.0  # 120 epochs 30 s apart, whatever the milliseconds of the clocks of the receivers
    assert r.solution.parent == (tmp_path / "work").resolve() / "baseline" and r.solution.suffix == ".pos"
    # fixed, near, an hour: only the antennas, which no calibration corrects here (atx=None), are told of - the two
    # receivers by their roles, and which way each moves the mark
    assert len(r.warnings) == 2, r.warnings
    station_note, own_note = r.warnings
    assert "TRM29659.00 of the station 0759" in station_note and "your base can be a few centimetres too low" in (
        station_note)
    assert "TRM29659.00 of your base 3040" in own_note and "too high" in own_note
    assert not [w for w in r.warnings if "the base" in w or "later version" in w]


def _raised(source: Path, target: Path, height: float) -> Path:
    """A copy of ``source`` whose header puts the antenna ``height`` m above the mark."""
    delta = _line(f"{height:14.4f}        0.0000        0.0000", "ANTENNA: DELTA H/E/N")
    return _rewrite_header(source, target,
                           lambda line: delta if line.rstrip().endswith("ANTENNA: DELTA H/E/N") else line)


@needs_rtklib
def test_baseline_gives_the_mark_under_the_antenna(tmp_path, monkeypatch):
    """The antenna height of the header of the user's base (ANTENNA: DELTA H/E/N) is taken off by RTKLIB: the position
    is of the mark on the ground. 3040 whose header says that its antenna is 1.5 m above the mark - the observations
    are those of the same antenna - is found 1.5 m lower, in the same place across."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    own = tmp_path / "own"
    own.mkdir()
    raised = _raised(OWN, own / "30400920.05o", 1.5)
    assert rinex.header(raised).antenna_delta_hen == (1.5, 0.0, 0.0)
    plain = base.baseline(OWN, _station_0759(), tmp_path / "plain", station_obs=STATION_OBS,
                          station_position=_station_position(), atx=None)
    lowered = base.baseline(raised, _station_0759(), tmp_path / "raised", [NAV], station_obs=STATION_OBS,
                            station_position=_station_position(), atx=None)
    assert plain.q == lowered.q == 1
    offset = _enu(_llh(lowered.position), _llh(plain.position))[0]
    assert np.allclose(offset, [0.0, 0.0, -1.5], rtol=0, atol=0.005), offset
    assert lowered.position.detail["antenna_delta_hen"] == [1.5, 0.0, 0.0]


def _to_enu(origin) -> np.ndarray:
    """The rows east, north, up at ``origin`` (latitude, longitude in degrees) of the turn from X, Y, Z."""
    phi, lam = np.radians(origin[0]), np.radians(origin[1])
    return np.array([[-np.sin(lam), np.cos(lam), 0.0],
                     [-np.sin(phi) * np.cos(lam), -np.sin(phi) * np.sin(lam), np.cos(phi)],
                     [np.cos(phi) * np.cos(lam), np.cos(phi) * np.sin(lam), np.sin(phi)]])


def _moved(llh, enu) -> tuple[float, float, float]:
    """``llh`` moved by ``enu`` metres east, north and up, exactly (through X, Y, Z)."""
    return rinex.ecef_to_llh(np.add(rinex.llh_to_ecef(llh), _to_enu(llh).T @ np.asarray(enu)))


def _enu_between(llh, origin) -> np.ndarray:
    """East, north, up (m) of ``llh`` from ``origin``, exactly (through X, Y, Z)."""
    return _to_enu(origin) @ np.subtract(rinex.llh_to_ecef(llh), rinex.llh_to_ecef(origin))


@needs_rtklib
def test_baseline_moves_with_the_station(tmp_path, monkeypatch):
    """The mark is the coordinates given to the station plus the baseline: the station moved by (1, -0.5, 0.25) m east,
    north and up moves the mark of the user's base by as much, within millimetres - real RTKLIB uses the position it
    is given, not the one of the header of the station."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    station = _station_0759()
    shift = (1.0, -0.5, 0.25)
    plain = base.baseline(OWN, station, tmp_path / "plain", station_obs=STATION_OBS,
                          station_position=_station_position(), atx=None)
    moved = base.baseline(OWN, station, tmp_path / "moved", station_obs=STATION_OBS,
                          station_position=_station_position(llh=_moved(_header_llh(STATION_OBS), shift)), atx=None)
    assert plain.q == moved.q == 1
    assert np.allclose(_enu_between(_llh(moved.position), _llh(plain.position)), shift, rtol=0, atol=0.003)


@needs_rtklib
def test_the_reversed_baseline_finds_the_other_header(tmp_path, monkeypatch):
    """The baseline the other way, 0759 placed from 3040 held at the position of its header, finds the disagreement of
    the two headers with the opposite sign: it is theirs, not of which receiver is the base."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    xyz = rinex.header(OWN).approx_xyz
    station_3040 = stations.Station("3040", "GSI 3040", ("IGS",), *_header_llh(OWN), tuple(xyz))
    held = ppk.BasePosition(*_header_llh(OWN), "station", frame="ITRF2020", epoch=2005.25)
    r = base.baseline(STATION_OBS, station_3040, tmp_path / "work", station_obs=OWN, station_position=held, atx=None)
    assert r.q == 1
    assert np.allclose(_enu(_llh(r.position), _header_llh(STATION_OBS))[0], -HEADERS_APART, rtol=0, atol=0.008)


@needs_rtklib
def test_baseline_calibrates_both_antennas(tmp_path, monkeypatch):
    """With the antenna calibrations both antennas are corrected: the excerpt puts the phase centre of TRM29659.00
    0.100 m above its reference point (a synthetic entry), the antenna of both 0759 and 3040. Both calibrated, the
    offsets cancel; the antenna of the user's base alone not in the file, its phase centre is taken for the mark and
    the mark is found 0.100 m too high; the antenna of the station alone not in it, the file is still given to RTKLIB
    for the user's base, and the mark is found 0.100 m too low. Each is told of."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    station = _station_0759()

    def run(name: str, own: Path, station_obs: Path, atx_file) -> base.BaselineResult:
        return base.baseline(own, station, tmp_path / name, [NAV], station_obs=station_obs,
                             station_position=_station_position(), atx=atx_file)

    def renamed(source: Path, name: str) -> Path:
        folder = tmp_path / name
        folder.mkdir()
        return _rewrite_header(source, folder / source.name, _on_label("ANT # / TYPE", f"{'':20}TRM41249.00     NONE"))

    plain = run("plain", OWN, STATION_OBS, None)
    both = run("both", OWN, STATION_OBS, ATX_EXCERPT)
    assert both.warnings == []
    assert both.position.detail["antenna_calibration"]["entry"] == "TRM29659.00     NONE"
    assert abs(both.position.h - plain.position.h) < 0.002
    own_not = run("own-not", renamed(OWN, "own"), STATION_OBS, ATX_EXCERPT)
    assert own_not.position.h - both.position.h == pytest.approx(0.100, abs=0.002)
    (note,) = own_not.warnings
    assert "TRM41249.00 NONE of your base 3040" in note and "igs20_excerpt.atx" in note and "too high" in note
    station_not = run("station-not", OWN, renamed(STATION_OBS, "station"), ATX_EXCERPT)
    assert station_not.position.h - both.position.h == pytest.approx(-0.100, abs=0.002)
    (note,) = station_not.warnings
    assert "TRM41249.00 NONE of the station 0759" in note and "too low" in note
    assert station_not.position.detail["antenna_calibration"]["entry"] == "TRM29659.00     NONE"


def _halves(folder: Path, navigation: bool = True, apart: bool = False) -> list[Path]:
    """The hour of 3040 in two files of half an hour, in ``folder``, with the broadcast navigation file of the day
    beside them as the IGS names it (``brdc0920.05n``: beside both, one file) when ``navigation``; ``apart``, the
    second half in a folder of its own, where there is none."""
    folder.mkdir(parents=True, exist_ok=True)
    header, records = _records(OWN)
    first = _piece(folder / "first.05o", header, records[:60])
    if apart:
        (folder / "later").mkdir()
    second = _piece(folder / "later" / "second.05o" if apart else folder / "second.05o", header, records[60:])
    if navigation:
        shutil.copyfile(DATA / "30400920.05n", folder / "brdc0920.05n")
    return [second, first]  # not in the order of their time: the second half first


@needs_rtklib
def test_baseline_with_the_base_in_two_files(tmp_path, monkeypatch):
    """A base recorded in two files (half an hour each, given in any order) is the base of the whole hour, with the
    navigation file found beside them as for one file - a station file with none beside it."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    station = tmp_path / "station"
    station.mkdir()
    station_obs = Path(shutil.copyfile(STATION_OBS, station / STATION_OBS.name))
    whole = base.baseline(OWN, _station_0759(), tmp_path / "whole", station_obs=station_obs,
                          station_position=_station_position(), atx=None)
    halves = base.baseline(_halves(tmp_path / "own"), _station_0759(), tmp_path / "halves", station_obs=station_obs,
                           station_position=_station_position(), atx=None)
    assert halves.q == 1 and halves.hours == 1.0
    assert np.all(np.abs(_enu(_llh(halves.position), _llh(whole.position))[0]) < 0.001)
    assert sorted(p.name for p in (tmp_path / "halves").iterdir()) == ["baseline"]  # nothing joined left behind


def _refused(*args, **kwargs):
    raise AssertionError("the navigation messages are not downloaded here")


@pytest.mark.parametrize("apart", [False, True], ids=["files together", "files apart"])
@pytest.mark.parametrize("station_given", [True, False], ids=["station given", "station downloaded"])
def test_baseline_takes_the_navigation_beside_the_files_of_the_base(tmp_path, fake_rtklib, monkeypatch,
                                                                    station_given, apart):
    """With no navigation file given, those beside the files of the base are the run's - beside any of them (here
    beside the file given second), each once (a file beside two of them) - whether the station's observations are
    given or downloaded, with none beside them: none is downloaded."""
    monkeypatch.setenv("PPG_FAKE_POS", str(_single_static_row(tmp_path)))
    monkeypatch.setattr(stations, "download_navigation", _refused)
    station = tmp_path / "station"
    station.mkdir()
    lone_station = Path(shutil.copyfile(STATION_OBS, station / STATION_OBS.name))

    def observations(st, start, end, folder, progress=None, rate="auto"):
        Path(folder).mkdir(parents=True, exist_ok=True)
        return Path(shutil.copyfile(STATION_OBS, Path(folder) / "0759_20050402-0000_0059.obs"))

    monkeypatch.setattr(stations, "download_observations", observations)
    r = base.baseline(_halves(tmp_path / "own", apart=apart), _station_0759(), tmp_path / "work",
                      station_obs=lone_station if station_given else None, station_position=_station_position(),
                      atx=None)
    assert r.q == 1
    (run,) = fake_rtklib()
    assert run["inputs"][1:] == [_sha256(STATION_OBS), _sha256(DATA / "30400920.05n")]  # the station, one navigation


def test_baseline_downloads_the_navigation_only_when_there_is_none(tmp_path, fake_rtklib, monkeypatch):
    """No navigation file beside the files of the base: the one beside the station's file when it is given, and when
    there is none there either the broadcast messages of the session, downloaded into the station folder."""
    monkeypatch.setenv("PPG_FAKE_POS", str(_single_static_row(tmp_path)))
    asked: list[tuple] = []

    def navigation(start, end, folder, progress=None):
        asked.append((start, end, Path(folder)))
        Path(folder).mkdir(parents=True, exist_ok=True)
        return [Path(shutil.copyfile(NAV, Path(folder) / "BRDC00WRD_S_20050920000_01D_MN.rnx"))]

    monkeypatch.setattr(stations, "download_navigation", navigation)
    pieces = _halves(tmp_path / "own", navigation=False)
    base.baseline(pieces, _station_0759(), tmp_path / "beside", station_obs=STATION_OBS,  # 07590920.05n beside it
                  station_position=_station_position(), atx=None)
    assert asked == [] and fake_rtklib()[-1]["inputs"][2:] == [_sha256(NAV)]
    station = tmp_path / "station"
    station.mkdir()
    lone_station = Path(shutil.copyfile(STATION_OBS, station / STATION_OBS.name))
    work = tmp_path / "work"
    base.baseline(pieces, _station_0759(), work, station_obs=lone_station, station_position=_station_position(),
                  atx=None)
    assert asked == [(*rinex.time_span(OWN), work / "station")]
    assert fake_rtklib()[-1]["inputs"][2:] == [_sha256(NAV)]


# --- the user's base by a static baseline: the run, its frame and its warnings (RTKLIB a stand-in) ------------------


@pytest.mark.parametrize("frame, epoch", [("ITRF2020", 2005.25), ("ETRF2000", 2005.25), ("unknown", None)])
def test_baseline_keeps_the_station_frame_and_epoch(tmp_path, fake_rtklib, monkeypatch, frame, epoch):
    """The mark of the user's base is the station's coordinates plus the baseline: in the frame of the station's
    position and at the epoch of its coordinates. Given the observations and the position of the station, nothing is
    downloaded; RTKLIB is asked for the base standing still, the forward solution written as one position, the antenna
    of the base from its header."""
    _no_downloads(monkeypatch)
    monkeypatch.setenv("PPG_FAKE_POS", str(_single_static_row(tmp_path, sd=(0.0011, 0.0007, 0.0024))))
    station_position = _station_position(frame, epoch)
    r = base.baseline(OWN, _station_0759(), tmp_path / "work", station_obs=STATION_OBS,
                      station_position=station_position, atx=None)
    assert r.position.source == "baseline" and r.position.frame == frame and r.position.epoch == epoch
    lat, lon, h = _header_llh(OWN)  # as the file writes them: degrees to 1e-9, metres to 1e-4
    assert (r.position.lat, r.position.lon, r.position.h) == (pytest.approx(lat, abs=1e-9),
                                                              pytest.approx(lon, abs=1e-9), pytest.approx(h, abs=1e-4))
    assert r.sd == r.position.sd == (0.0007, 0.0011, 0.0024)  # RTKLIB writes sdn, sde, sdu: east, north, up
    assert r.q == 1
    assert r.warnings[-1].startswith("the phase-centre offset of the antenna TRM29659.00 of your base 3040")
    # across, as ppk.baseline_km measures a base from a flight: BASELINE_WARNING_KM means the same in both
    assert r.baseline_km == ppk.horizontal_m(station_position.lat, station_position.lon, r.position.lat,
                                             r.position.lon) / 1000.0
    (run,) = fake_rtklib()
    options = _options(run["options"])
    assert options["pos1-posmode"] == "static" and options["pos1-soltype"] == "forward"
    assert options["out-solstatic"] == "single" and options["ant1-anttype"] == "*"
    assert options["ant2-postype"] == "llh" and float(options["ant2-pos3"]) == pytest.approx(station_position.h)
    assert run["inputs"][:2] == [_sha256(OWN), _sha256(STATION_OBS)]  # the user's base is the rover of the run
    detail = r.position.detail
    assert detail["station"] == "0759" and detail["networks"] == ["IGS"] and detail["base"] == "3040"
    assert detail["station_position"] == {"lat": station_position.lat, "lon": station_position.lon,
                                          "h": station_position.h, "source": "station", "frame": frame,
                                          "epoch": epoch, "sd": [0.002, 0.003, 0.006],
                                          "detail": {"station": "0759", "citation": "RTKLIB sample data"}}
    assert detail["citation"] == "RTKLIB sample data"
    assert detail["q"] == 1 and detail["status"] == "fix" and detail["hours"] == 1.0
    assert detail["baseline_km"] == r.baseline_km and detail["solution"] == r.solution.name
    assert detail["antenna"] == "TRM29659.00" and detail["antenna_delta_hen"] == [0.0, 0.0, 0.0]
    assert detail["antenna_calibration"] is None  # no file of antenna calibrations was given


def test_float_baseline_warns(tmp_path, fake_rtklib, monkeypatch, caplog):
    """A static solution that is not fixed is a warning, in the result and in the log: the position can be decimetres
    off, and every position of the survey with it."""
    monkeypatch.setenv("PPG_FAKE_POS", str(_single_static_row(tmp_path, q=2, sd=(0.04, 0.03, 0.09))))
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        r = base.baseline(OWN, _station_0759(), tmp_path / "work", station_obs=STATION_OBS,
                          station_position=_station_position(), atx=None)
    assert r.q == 2 and r.position.detail["status"] == "float"
    (warning,) = [w for w in r.warnings if "float" in w]
    assert "3040" in warning and "not fixed" in warning and "decimetres" in warning
    assert warning in [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


@pytest.mark.parametrize("q", [5, 4])
def test_baseline_refuses_a_solution_neither_fixed_nor_float(tmp_path, fake_rtklib, monkeypatch, q):
    """A single (Q=5) or code-differential (Q=4) static solution is a position of the code alone, metres off: no base,
    as a station with only an approximate position is none. The solution stays, and the error names it."""
    monkeypatch.setenv("PPG_FAKE_POS", str(_single_static_row(tmp_path, q=q, sd=(1.2, 0.9, 2.5))))
    with pytest.raises(ValueError, match=f"Q={q}") as error:
        base.baseline(OWN, _station_0759(), tmp_path / "work", station_obs=STATION_OBS,
                      station_position=_station_position(), atx=None)
    (kept,) = (tmp_path / "work" / "baseline").glob("solution-*.pos")
    assert str(kept.resolve()) in str(error.value) and "metres off" in str(error.value)


def test_baseline_counts_the_session_without_the_holes_of_the_base(tmp_path, fake_rtklib, monkeypatch, caplog):
    """The hours of the session are the time the base observed with the station less its holes, and a hole of more
    than a minute is told of: here the base has none from 00:19:30 to 00:40:00, 20 minutes of the hour."""
    monkeypatch.setenv("PPG_FAKE_POS", str(_single_static_row(tmp_path)))
    header, records = _records(OWN)
    before = _piece(tmp_path / "before.05o", header, records[:40])  # to 00:19:30
    after = _piece(tmp_path / "after.05o", header, records[80:])  # from 00:40:00
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        r = base.baseline([before, after], _station_0759(), tmp_path / "work", [NAV], station_obs=STATION_OBS,
                          station_position=_station_position(), atx=None)
    assert r.hours == pytest.approx(40 / 60, abs=1e-12) and r.position.detail["hours"] == r.hours
    (hole,) = [w for w in r.warnings if "no observations" in w]
    assert hole.startswith("your base 3040 has no observations from 2005-04-02 00:19:29") and "20 minutes" in hole
    assert "40 minutes" in " ".join(r.warnings)  # under an hour, counted without the hole
    assert hole in [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    # a hole of a minute counts, but is not told of
    short = _piece(tmp_path / "short.05o", header, records[42:])  # from 00:21:00
    r = base.baseline([before, short], _station_0759(), tmp_path / "other", [NAV], station_obs=STATION_OBS,
                      station_position=_station_position(), atx=None)
    assert r.hours == pytest.approx(59 / 60, abs=1e-12) and not [w for w in r.warnings if "no observations" in w]


def test_baseline_warns_of_a_far_station_and_a_short_session(tmp_path, fake_rtklib, monkeypatch, caplog):
    """A station further than ppk.BASELINE_WARNING_KM and a session shorter than an hour are warnings, in the result
    and in the log."""
    monkeypatch.setenv("PPG_FAKE_POS", str(_single_static_row(tmp_path)))
    header, records = _records(OWN)
    half = _piece(tmp_path / "half.05o", header, records[:60])  # 00:00:00 to 00:29:30
    lat, lon, h = _header_llh(STATION_OBS)
    far = _station_position(llh=(lat + 0.27, lon, h))  # 30 km to the north
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        r = base.baseline(half, _station_0759(), tmp_path / "work", [NAV], station_obs=STATION_OBS,
                          station_position=far, atx=None)
    assert 30.0 < r.baseline_km < 35.0 and r.hours == 0.5
    (distance,) = [w for w in r.warnings if "km from the station" in w]
    assert f"{r.baseline_km:.1f} km from the station 0759" in distance and "20 km" in distance
    (session,) = [w for w in r.warnings if "minutes" in w]
    assert "30 minutes" in session and "an hour" in session
    logged = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert distance in logged and session in logged


def test_baseline_refuses_a_station_that_did_not_record_with_the_base(tmp_path, fake_rtklib):
    header, records = _records(OWN)
    early = _piece(tmp_path / "early.05o", header, records[:40])  # to 00:19:30
    station_header, station_records = _records(STATION_OBS)
    late = _piece(tmp_path / "late.05o", station_header, station_records[80:])  # from 00:40:00
    with pytest.raises(ValueError, match="no time in common") as error:
        base.baseline(early, _station_0759(), tmp_path / "work", [NAV], station_obs=late,
                      station_position=_station_position(), atx=None)
    assert "3040" in str(error.value) and "0759" in str(error.value)
    assert fake_rtklib() == []


def test_baseline_downloads_what_it_is_not_given(tmp_path, fake_rtklib, monkeypatch):
    """Without the observations and the position of the station: its observations for the hours of the base, its
    coordinates at the middle of them, the antenna calibrations and, when the base has no navigation file beside it,
    the broadcast navigation messages, all into the folder of the baseline; the PPK runs on them."""
    monkeypatch.setenv("PPG_FAKE_POS", str(_single_static_row(tmp_path)))
    asked: list[tuple] = []
    station = stations.Station("AQUI00ITA", "L'Aquila", ("EPN",), 42.37, 13.35, 713.0,
                               (4592507.0, 1089876.0, 4276392.0))
    xyz = rinex.header(STATION_OBS).approx_xyz

    def observations(st, start, end, folder, progress=None, rate="auto"):
        asked.append(("observations", st.id, start, end, Path(folder), rate))
        Path(folder).mkdir(parents=True, exist_ok=True)
        return Path(shutil.copyfile(STATION_OBS, Path(folder) / "AQUI00ITA_20050402-0000_0059.obs"))

    def coordinates(st, epoch, progress=None):
        asked.append(("coordinates", st.id, epoch))
        return stations.StationCoordinates(st, tuple(xyz), _header_llh(STATION_OBS), "ITRF2020", epoch, "EPN",
                                           (0.001, 0.001, 0.002), "EPN, CC BY 4.0")

    def navigation(start, end, folder, progress=None):
        asked.append(("navigation", start, end, Path(folder)))
        return [Path(shutil.copyfile(NAV, Path(folder) / "BRDC00WRD_S_20050920000_01D_MN.rnx"))]

    def antennas(refresh=False, progress=None):
        asked.append(("atx",))
        return tmp_path / "igs20.atx"

    monkeypatch.setattr(stations, "download_observations", observations)
    monkeypatch.setattr(stations, "coordinates", coordinates)
    monkeypatch.setattr(stations, "download_navigation", navigation)
    monkeypatch.setattr(atx, "atx_file", antennas)
    alone = tmp_path / "alone"  # the base without its navigation file
    alone.mkdir()
    own = Path(shutil.copyfile(OWN, alone / OWN.name))
    work = tmp_path / "work"
    r = base.baseline(own, station, work)
    start, end = rinex.time_span(OWN)
    assert asked == [("coordinates", "AQUI00ITA", pytest.approx(MID_2005, abs=1e-9)),
                     ("observations", "AQUI00ITA", start, end, work / "station", "auto"),
                     ("navigation", start, end, work / "station"), ("atx",)]
    assert r.position.frame == "ITRF2020" and r.position.epoch == pytest.approx(MID_2005, abs=1e-9)
    assert r.position.detail["station_position"]["detail"]["coordinates"]["source"] == "EPN"
    assert r.position.detail["citation"] == "EPN, CC BY 4.0"
    (run,) = fake_rtklib()
    assert run["inputs"] == [_sha256(OWN), _sha256(STATION_OBS), _sha256(NAV)]
    # a base with its navigation file beside it: none is downloaded
    asked.clear()
    base.baseline(OWN, station, tmp_path / "other")
    assert [entry[0] for entry in asked] == ["coordinates", "observations", "atx"]


# --- a station as the base of a flight ------------------------------------------------------------------------------

AQUI = stations.Station("AQUI00ITA", "L'Aquila", ("EPN", "IGS"), 42.368, 13.350, 713.0,
                        (4592507.0, 1089876.0, 4276392.0))
AQUI_XYZ = (4592507.4171, 1089876.6158, 4276393.1953)  # at the middle of the flight


def _replaced_station(monkeypatch, tmp_path, source="EPN", sigma=(0.0012, 0.0008, 0.0011)) -> list[tuple]:
    """The coordinates, the observations and the antenna calibrations of a station as stand-ins that log their
    calls."""
    calls: list[tuple] = []

    def coordinates(st, epoch, progress=None):
        calls.append(("coordinates", st.id, epoch))
        llh = rinex.ecef_to_llh(AQUI_XYZ)
        if source == "approximate":
            return stations.StationCoordinates(st, st.xyz, (st.lat, st.lon, st.h), "ITRF2020", epoch, "approximate",
                                               None, "approximate position from the site log: good to metres only")
        return stations.StationCoordinates(st, AQUI_XYZ, llh, "ITRF2020", epoch, source, sigma,
                                           f"{source} coordinates, CC BY 4.0")

    def observations(st, start, end, folder, progress=None, rate="auto"):
        calls.append(("observations", st.id, start, end, Path(folder), rate))
        Path(folder).mkdir(parents=True, exist_ok=True)
        return Path(shutil.copyfile(STATION_OBS, Path(folder) / "AQUI00ITA_20050402-0000_0059.obs"))

    def antennas(refresh=False, progress=None):
        calls.append(("atx",))
        return tmp_path / "igs20.atx"

    monkeypatch.setattr(stations, "coordinates", coordinates)
    monkeypatch.setattr(stations, "download_observations", observations)
    monkeypatch.setattr(atx, "atx_file", antennas)
    return calls


def _enu_sd(sigma, lat: float, lon: float) -> tuple[float, float, float]:
    """The standard deviations east, north, up of X, Y, Z with independent ``sigma``, each a sum of squares."""
    sx, sy, sz = (s * s for s in sigma)
    phi, lam = math.radians(lat), math.radians(lon)
    east = sx * math.sin(lam) ** 2 + sy * math.cos(lam) ** 2
    north = (sx * math.cos(lam) ** 2 + sy * math.sin(lam) ** 2) * math.sin(phi) ** 2 + sz * math.cos(phi) ** 2
    up = (sx * math.cos(lam) ** 2 + sy * math.sin(lam) ** 2) * math.cos(phi) ** 2 + sz * math.sin(phi) ** 2
    return math.sqrt(east), math.sqrt(north), math.sqrt(up)


@pytest.mark.parametrize("source, kind", [("EPN", "formal"), ("IGS", "formal"), ("RING", "scatter")])
def test_station_base_for_the_flight(tmp_path, monkeypatch, source, kind):
    """The station's observations for the hours of the drone's RINEX and its coordinates in ITRF2020 at the middle of
    the flight (the epoch of the base, and so of the positions of the photos), with their standard deviations turned
    from X, Y, Z to east, north, up; the antenna calibrations made sure of for the PPK that follows."""
    calls = _replaced_station(monkeypatch, tmp_path, source)
    folder = tmp_path / "bases"
    observations, position = base.station_base(OWN, AQUI, folder)  # the hour of 3040 as the flight
    start, end = rinex.time_span(OWN)
    assert observations == folder / "AQUI00ITA_20050402-0000_0059.obs" and observations.is_file()
    assert calls == [("coordinates", "AQUI00ITA", pytest.approx(MID_2005, abs=1e-9)),
                     ("observations", "AQUI00ITA", start, end, folder, "auto"), ("atx",)]
    assert position.source == "station" and position.frame == "ITRF2020"
    assert position.epoch == pytest.approx(MID_2005, abs=1e-9)
    lat, lon, h = rinex.ecef_to_llh(AQUI_XYZ)
    assert (position.lat, position.lon, position.h) == (lat, lon, h)
    assert position.sd == pytest.approx(_enu_sd((0.0012, 0.0008, 0.0011), lat, lon), abs=1e-12)
    assert position.detail == {"station": "AQUI00ITA", "name": "L'Aquila", "networks": ["EPN", "IGS"],
                               "coordinates": {"source": source, "frame": "ITRF2020",
                                               "epoch": pytest.approx(MID_2005, abs=1e-9), "xyz": list(AQUI_XYZ),
                                               "sigma_xyz": [0.0012, 0.0008, 0.0011]},
                               "sigma": kind, "citation": f"{source} coordinates, CC BY 4.0"}


def test_station_base_without_sigma_has_no_deviations(tmp_path, monkeypatch):
    _replaced_station(monkeypatch, tmp_path, "RING", sigma=None)
    _observations, position = base.station_base(OWN, AQUI, tmp_path / "bases")
    assert position.sd is None and position.detail["sigma"] is None
    assert position.detail["coordinates"]["sigma_xyz"] is None


def test_station_base_refuses_approximate_coordinates(tmp_path, monkeypatch):
    """A station whose coordinates none of the EPN, the IGS and RING gives is not a base: its position from the site
    log is metres off, and the survey would move with it. Nothing is downloaded for it."""
    calls = _replaced_station(monkeypatch, tmp_path, "approximate")
    with pytest.raises(ValueError, match="approximate") as error:
        base.station_base(OWN, AQUI, tmp_path / "bases")
    assert "AQUI00ITA" in str(error.value) and "metres" in str(error.value)
    assert [call[0] for call in calls] == ["coordinates"]
    with pytest.raises(ValueError, match="approximate"):  # nor is it a station to measure a baseline from
        base.baseline(OWN, AQUI, tmp_path / "work", station_obs=STATION_OBS, atx=None)


def test_station_base_goes_on_without_the_antenna_calibrations(tmp_path, monkeypatch, caplog):
    """igs20.atx that cannot be downloaded is a warning in the log: the PPK runs without it, and says so."""
    _replaced_station(monkeypatch, tmp_path)

    def offline(refresh=False, progress=None):
        raise download.DownloadError("https://files.igs.org/pub/station/general/igs20.atx: no network", "")

    monkeypatch.setattr(atx, "atx_file", offline)
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        observations, position = base.station_base(OWN, AQUI, tmp_path / "bases")
    assert observations.is_file() and position.source == "station"
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "igs20.atx" in warning and "no network" in warning


def test_station_base_says_what_it_does_and_can_be_cancelled(tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

    _replaced_station(monkeypatch, tmp_path)
    seen: list[float] = []
    base.station_base(OWN, AQUI, tmp_path / "bases", progress=lambda fraction, message: seen.append(fraction))
    assert seen == sorted(seen) and seen[-1] == 1.0
    progress = Progress()
    progress.cancel_requested = True
    with pytest.raises(Cancelled):
        base.station_base(OWN, AQUI, tmp_path / "other", progress=progress)
