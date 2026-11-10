"""The PPK of photos shot on the events of the receiver and of raw receiver logs: a rover whose RINEX file or u-blox log
holds the time marks of the camera, the photos given the antenna where the solution puts it at each event plus the lever
arm; the events checked against the time marks RTKLIB writes (``<solution>_events.pos``: same times, same positions);
a u-blox rover or an RTCM 3 base converted before RTKLIB runs (``convert.to_rinex``), the approximate time of the RTCM
3 base taken from the rover.

The data are the RTKLIB samples of ``tests/data/gnss/phase4`` (README.md there): the u-blox log of 2008-05-26 (1 Hz,
the four TIM-TM2 frames of ``convbin_helpers.insert_time_marks``) is both rover and base - a zero baseline, every
position the same - and the RTCM 3 log of station GMSD7 likewise; the 30 s samples of ``tests/data/rtklib`` (3.3 km
apart) show the marks the combined solution drops."""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

import convbin_helpers as ubx
import test_gnss_shots as shots_test
import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.gnss import apply as gnss_apply
from pyarchinit_photogrammetry.gnss import convert, events, ppk, rinex
from pyarchinit_photogrammetry.gnss.shots import Shot, ShotSet
from gnss_helpers import PPK_BASE, PPK_NAV, PPK_ROVER, jpeg, needs_rtklib, pos_text, rinex_records

PHASE4 = Path(__file__).parent / "data" / "gnss" / "phase4"
UBX_LOG = PHASE4 / "ubx_20080526_64k.ubx"
UBX_OBS = PHASE4 / "ubx_20080526_64k.obs"  # what convbin made of the log, without the marks
UBX_NAV = PHASE4 / "ubx_20080526_64k.nav"
TM2_OBS = PHASE4 / "ubx_20080526_64k_tm2.obs"  # what convbin made of the log with the marks: three events
RTCM_LOG = PHASE4 / "GMSD7_20121014_64k.rtcm3"
RTCM_OBS = PHASE4 / "GMSD7_20121014_64k.obs"
RTCM_NAV = PHASE4 / "GMSD7_20121014_64k.nav"
GMSD = (30.5566, 131.0155, 130.0)  # about where station GMSD stands (Tanegashima): its log names no position

GPS_EPOCH = datetime(1980, 1, 6)
LEAP_2008 = 14
WEEK_1481 = 1481 * 604800.0
MARKS = [  # as in test_gnss_events: three falling edges (one with a UTC time base), a rising edge RTKLIB leaves out
    (ubx.tim_tm2(1481, 107975.5, count=1), WEEK_1481 + 107975.5),
    (ubx.tim_tm2(1481, 107990.25 - LEAP_2008, flags=ubx.FALLING | ubx.BASE_UTC | ubx.TIME_VALID, count=2),
     WEEK_1481 + 107990.25),
    (ubx.tim_tm2(1481, 108005.123456789, count=3), WEEK_1481 + 108005.123456789),
    (ubx.tim_tm2(1481, 108015.0, flags=(1 << 7) | ubx.BASE_GNSS | ubx.TIME_VALID, count=4), WEEK_1481 + 108015.0),
]
ZONE = timedelta(hours=9)  # the camera on the local time of Japan
needs_a_converter = pytest.mark.skipif(not _native.convbin_available(),
                                       reason="no converter: native core older than 0.8 and no convbin")


def _alone(source: Path, folder: Path) -> Path:
    """A copy of ``source`` in a folder of its own: no navigation file beside it."""
    folder.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copy(source, folder / source.name))


def _ubx_rover(folder: Path) -> Path:
    """The u-blox log with the time marks of the camera."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "rover_tm2.ubx"
    path.write_bytes(ubx.insert_time_marks(UBX_LOG.read_bytes(), [m for m, _ in MARKS], [t for _, t in MARKS]))
    return path


def _photos(folder: Path, gps_times) -> ppg.Chunk:
    """A photo per shot, named in order, with the time the camera wrote (local time, to the second)."""
    folder.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(gps_times, 1):
        utc = GPS_EPOCH + timedelta(seconds=t - LEAP_2008)
        jpeg(folder / f"IMG_{i:04d}.JPG", None, when=(utc + ZONE).replace(microsecond=0))
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    return ch


def _shifted(shotset: ShotSet, seconds: float) -> ShotSet:
    return ShotSet(shots=[Shot(index=s.index, gps_time=s.gps_time + seconds, source=dict(s.source))
                          for s in shotset.shots], kind=shotset.kind, name=shotset.name)


@needs_rtklib
def test_ppk_with_rover_events(tmp_path, monkeypatch):
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    shotset = events.rinex_events(TM2_OBS)
    assert len(shotset.shots) == 3
    solution = ppk.solve(TM2_OBS, _alone(UBX_OBS, tmp_path / "base"), [UBX_NAV], folder=tmp_path / "work")
    assert ppk.events_check(solution, shotset) == []
    ch = _photos(tmp_path / "photos", [s.gps_time for s in shotset.shots])
    result = gnss_apply.apply_ppk_shots(ch, shotset, solution, tmp_path / "photos", by="order",
                                        body_lever_mm=(0, 0, 250))
    # three shots 15 s apart: too few for the times alone to pair them, so by order, not confirmed
    assert result["matched"] == 3 and result["confirmed"] is False
    assert not any("time mark" in w for w in result["warnings"]), result["warnings"]
    llh, _q, _sd, ok = ppk.at(solution, [s.gps_time for s in shotset.shots])
    assert ok.all()
    for cam, (lat, lon, h) in zip(sorted(ch.cameras, key=lambda c: c.label), llh):
        assert cam.reference.location[0] == pytest.approx(lon, abs=1e-10)
        assert cam.reference.location[1] == pytest.approx(lat, abs=1e-10)
        assert cam.reference.location[2] == pytest.approx(h - 0.25, abs=1e-6)  # no attitude: the vertical part
        assert cam.meta["gnss"]["shots"]["kind"] == "rinex_events"
    last = ppk.last_run(ch)
    assert last["shots"]["kind"] == "rinex_events" and last["shots"]["confirmed"] is False


def test_last_run_records_whether_the_times_confirm_the_pairing(tmp_path):
    exposures = shots_test._exposures(60)
    ch = shots_test._photos(tmp_path / "f", [f"IMG_{i:04d}.JPG" for i in range(1, 61)], exposures)
    solution = shots_test._moving_solution(shots_test._gps(exposures[0]) - 5.0, 160.0, tmp_path / "rover.pos")
    result = gnss_apply.apply_ppk_shots(ch, shots_test._events(exposures), solution, tmp_path / "f", by="number")
    assert result["matched"] == 60 and result["confirmed"] is True
    assert ppk.last_run(ch)["shots"]["confirmed"] is True
    # a solution without RTKLIB's time marks beside it: the events were not checked, and it says so
    assert any("were not checked" in w for w in result["warnings"])


@needs_rtklib
def test_events_check_reports_a_shift(tmp_path, monkeypatch):
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    shotset = events.rinex_events(TM2_OBS)
    solution = ppk.solve(TM2_OBS, _alone(UBX_OBS, tmp_path / "base"), [UBX_NAV], folder=tmp_path / "work")
    warnings = ppk.events_check(solution, _shifted(shotset, 0.2))
    assert len(warnings) == 1, warnings
    assert "3 of the 3 shots" in warnings[0] and "0.200 s" in warnings[0]
    # a mark of the file 1.1 cm north of where the program puts the antenna at the same time
    marks = solution.path.with_name(solution.path.stem + ppk.EVENTS_SUFFIX)
    lines = marks.read_text().splitlines()
    rows = [i for i, line in enumerate(lines) if line and not line.startswith("%")]
    fields = lines[rows[1]].split()
    lines[rows[1]] = lines[rows[1]].replace(fields[2], f"{float(fields[2]) + 1e-7:.9f}", 1)
    marks.write_text("\n".join(lines) + "\n")
    warnings = ppk.events_check(solution, shotset)
    assert len(warnings) == 1 and "1 time mark" in warnings[0] and "11 mm" in warnings[0], warnings
    # nothing to check against: said so
    marks.unlink()
    assert any("not checked" in w for w in ppk.events_check(solution, shotset))


@needs_rtklib
def test_events_check_counts_the_marks_the_combined_solution_drops(tmp_path, monkeypatch):
    """RTKLIB-EX leaves out two of these four marks (test_gnss_events.test_events_pos_read pins it; with this build
    the forward and backward solutions alone lose the same two): the check counts them against the rover's events."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)

    def event(minute: int, seconds: float) -> str:
        return f" 05  4  2  0 {minute:2d}{seconds:11.7f}  5  0"

    after = {1: [event(0, 45.125)], 49: [event(24, 31.5)], 79: [event(39, 50.25)], 99: [event(49, 50.25)]}
    header, records = rinex_records(PPK_ROVER)
    lines = list(header)
    for i, record in enumerate(records):
        lines += record + after.get(i, [])
    rover = tmp_path / "3040evts.05o"
    rover.write_text("\n".join(lines) + "\n", newline="\n")
    solution = ppk.solve(rover, PPK_BASE, [PPK_NAV], folder=tmp_path / "work")
    warnings = ppk.events_check(solution, events.rinex_events(rover))
    assert len(warnings) == 1 and "2 of the 4 time marks" in warnings[0], warnings


@needs_rtklib
@needs_a_converter
def test_ppk_with_a_ubx_rover(tmp_path, monkeypatch):
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    log = _ubx_rover(tmp_path / "rover")
    base = _alone(UBX_OBS, tmp_path / "base")
    by_hand = ppk.solve(TM2_OBS, base, [UBX_NAV], folder=tmp_path / "work")
    raw = ppk.solve(log, base, folder=tmp_path / "work")  # no navigation file: the one of the conversion
    assert np.array_equal(raw.times, by_hand.times) and np.array_equal(raw.q, by_hand.q)
    assert np.allclose(raw.llh, by_hand.llh, rtol=0, atol=1e-9)
    assert raw.inputs["rover"] == str(log.absolute()) and raw.rover == log.name
    (converted,) = raw.inputs["converted"]
    assert converted["source"] == str(log.absolute()) and converted["kind"] == "ubx"
    assert converted["events"] == 3 and converted["epochs"] == 62
    assert raw.inputs["navs"] == [converted["nav"]]
    assert ppk.events_check(raw, events.ubx_time_marks(log)) == []
    # the photos paired with the marks of the log
    marks = events.ubx_time_marks(log)
    ch = _photos(tmp_path / "photos", [s.gps_time for s in marks.shots])
    result = gnss_apply.apply_ppk_shots(ch, marks, raw, tmp_path / "photos", by="order")
    assert result["matched"] == 3
    assert not any("time mark" in w for w in result["warnings"]), result["warnings"]


@needs_a_converter
def test_ppk_with_an_rtcm_base(tmp_path, monkeypatch):
    """The RTCM 3 log of the base converted with the middle of the rover's time, and its RINEX given to RTKLIB. The
    sample has too few BeiDou ephemerides for RTKLIB to compute anything: RTKLIB is stood in for by a solution of
    the rows of gnss_helpers, and what it is given is looked at."""
    rover = _alone(RTCM_OBS, tmp_path / "rover")
    seen, given = [], []
    to_rinex = convert.to_rinex

    def spy(path, *args, **kwargs):
        seen.append((Path(path).name, kwargs.get("approx_time")))
        return to_rinex(path, *args, **kwargs)

    def rtklib(conf, inputs, out):
        given.append([Path(path).read_bytes() for path in inputs])
        Path(out).write_text(pos_text(), newline="")
        return 0, ""

    monkeypatch.setattr(convert, "to_rinex", spy)
    monkeypatch.setattr(_native, "ppk_solve", rtklib)
    solution = ppk.solve(rover, RTCM_LOG, [RTCM_NAV], base_position=GMSD, folder=tmp_path / "work")
    first, last = rinex.time_span(rover)
    middle = ((first - GPS_EPOCH) + (last - GPS_EPOCH)).total_seconds() / 2
    assert seen == [(RTCM_LOG.name, pytest.approx(middle))]
    (converted,) = solution.inputs["converted"]
    assert converted["kind"] == "rtcm3" and converted["source"] == str(RTCM_LOG.absolute())
    assert converted["epochs"] == 64 and converted["events"] == 0
    assert solution.inputs["base"] == str(RTCM_LOG.absolute()) and solution.base["file"] == RTCM_LOG.name
    rover_copy, base_copy, *_navs = given[0]
    assert rover_copy == RTCM_OBS.read_bytes() and base_copy == Path(converted["obs"]).read_bytes()
    # an RTCM 3 log of the rover: placed by the middle of the time of the base
    seen.clear()
    solution = ppk.solve(RTCM_LOG, rover, [RTCM_NAV], base_position=GMSD, folder=tmp_path / "work")
    assert seen == [(RTCM_LOG.name, pytest.approx(middle))] and solution.inputs["rover"] == str(RTCM_LOG.absolute())


@needs_a_converter
def test_raw_base_far_in_time_says_so(tmp_path):
    rover = _alone(UBX_OBS, tmp_path / "rover")  # May 2008, and the RTCM 3 log of the base is of October 2012
    with pytest.raises(ValueError, match="approximate time.*rover"):
        ppk.solve(rover, RTCM_LOG, [UBX_NAV], base_position=GMSD, folder=tmp_path / "work")
    assert not (tmp_path / "work").exists() or not any((tmp_path / "work").glob("*.pos"))


def test_rover_span_in_gps_time(tmp_path):
    """The approximate time of a base taken from a rover whose file is in UTC: in GPS time, as everything else."""
    lines = UBX_OBS.read_text().splitlines()
    lines = [line[:48] + "UTC" + line[51:] if "TIME OF FIRST OBS" in line else line for line in lines]
    utc = tmp_path / "utc.obs"
    utc.write_text("\n".join(lines) + "\n")
    gps_first, gps_last = ppk.gps_span(UBX_OBS)
    first, last = ppk.gps_span(utc)
    assert first - gps_first == pytest.approx(LEAP_2008) and last - gps_last == pytest.approx(LEAP_2008)
