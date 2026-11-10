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
import math
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
from gnss_helpers import LEAP_2005, PPK_BASE, PPK_NAV, PPK_ROVER, START_2005, enu, jpeg, needs_rtklib, pos_text
from gnss_helpers import rinex_records

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


def _event_2005(minute: int, seconds: float, hour: int = 0) -> str:
    """An external event of RINEX 2 on 2 April 2005, no special lines after it."""
    return f" 05  4  2 {hour:2d} {minute:2d}{seconds:11.7f}  5  0"


def _rover_2005(path: Path, after: dict[int, list[str]]) -> Path:
    """Station 3040 (the rover of the 2005 pair, 3.3 km from 0759) with the event lines ``after[i]`` after its record
    ``i`` (records every 30 s from 00:00:00)."""
    header, records = rinex_records(PPK_ROVER)
    lines = list(header)
    for i, record in enumerate(records):
        lines += record + after.get(i, [])
    path.write_text("\n".join(lines) + "\n", newline="\n")
    return path


@needs_rtklib
@pytest.mark.parametrize("solution_type", ["combined", "forward", "backward"])
def test_every_mark_between_epochs_written(tmp_path, monkeypatch, solution_type):
    """The 2005 pair (a real baseline of 3.3 km), four marks between epochs: RTKLIB writes all four in every mode.
    Upstream RTKLIB-EX 2.5.1 lost two of them: an epoch whose single point positioning it skips (rtkpos.c) did not
    take the time mark of the epoch (native/vendor/rtklib/VERSION)."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    after = {1: [_event_2005(0, 45.125)], 49: [_event_2005(24, 31.5)], 79: [_event_2005(39, 50.25)],
             99: [_event_2005(49, 50.25)]}
    rover = _rover_2005(tmp_path / "3040evts.05o", after)
    solution = ppk.solve(rover, PPK_BASE, [PPK_NAV], folder=tmp_path / "work", solution=solution_type)
    marks = events.events_pos(solution.path.with_name(solution.path.stem + ppk.EVENTS_SUFFIX))
    assert len(marks.shots) == 4
    assert ppk.events_check(solution, events.rinex_events(rover)) == []


@needs_rtklib
def test_mark_after_the_last_epoch_left_out(tmp_path, monkeypatch):
    """A mark after the last epoch (00:59:30) has no epoch to be placed on: RTKLIB writes the four others, and the
    check counts the one left out."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    after = {1: [_event_2005(0, 45.125)], 49: [_event_2005(24, 31.5)], 79: [_event_2005(39, 50.25)],
             99: [_event_2005(49, 50.25)], 119: [_event_2005(59, 45.0)]}
    rover = _rover_2005(tmp_path / "3040evts.05o", after)
    solution = ppk.solve(rover, PPK_BASE, [PPK_NAV], folder=tmp_path / "work")
    warnings = ppk.events_check(solution, events.rinex_events(rover))
    assert len(warnings) == 1 and "1 of the 5 time marks of the rover are not in" in warnings[0], warnings
    assert "outside the epochs it solved" in warnings[0]


@needs_rtklib
def test_marks_on_epochs_against_the_photos(tmp_path, monkeypatch):
    """Marks exactly on epochs (whole seconds) of the 2005 pair: RTKLIB writes each at the row of its epoch, and the
    photos placed with ``at`` are where RTKLIB's marks are, on positions that are not all the same (a float solution
    of a station 3.3 km from the base moves by decimetres). RTKLIB writes the epochs at the time of the receiver's
    clock corrected: 1 ms before the whole second at 10, 20 and 30 minutes (``at`` takes the epoch), 3 ms before it
    at 40 minutes, where ``at`` takes nothing (epochs 30 s apart) and the photo keeps its position."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    minutes = (10, 20, 30, 40)
    after = {2 * m: [_event_2005(m, 0.0)] for m in minutes}  # record 2m is the epoch of minute m
    rover = _rover_2005(tmp_path / "3040evts.05o", after)
    solution = ppk.solve(rover, PPK_BASE, [PPK_NAV], folder=tmp_path / "work")
    shotset = events.rinex_events(rover)
    assert ppk.events_check(solution, shotset) == []
    marks = events.events_pos(solution.path.with_name(solution.path.stem + ppk.EVENTS_SUFFIX)).shots
    assert [round(m.gps_time - START_2005) for m in marks] == [60 * m for m in minutes]
    row = {round(t - START_2005): llh for t, llh in zip(solution.times, solution.llh)}
    for mark in marks:  # each at the row of its epoch
        assert mark.antenna == pytest.approx(tuple(row[round(mark.gps_time - START_2005)]), abs=1e-9)
    spread = enu(np.array([m.antenna for m in marks]), marks[0].antenna)
    assert np.abs(spread).max() > 0.01  # not one position four times
    ch = ppg.Document().add_chunk()
    folder = tmp_path / "photos"
    folder.mkdir()
    for i, shot in enumerate(shotset.shots, 1):
        utc = GPS_EPOCH + timedelta(seconds=shot.gps_time - LEAP_2005)
        jpeg(folder / f"IMG_{i:04d}.JPG", None, when=(utc + ZONE).replace(microsecond=0))
    ch.add_photos([folder])
    result = gnss_apply.apply_ppk_shots(ch, shotset, solution, folder, by="order")
    assert result["matched"] == 3 and not any("time mark" in w for w in result["warnings"]), result["warnings"]
    assert any("1 photo was taken where the solution" in w for w in result["warnings"])
    assert result["unmatched"] == ["IMG_0004"]
    for cam, mark in zip(sorted(ch.cameras, key=lambda c: c.label)[:3], marks):
        lon, lat, h = cam.reference.location
        assert ppk.horizontal_m(lat, lon, mark.antenna[0], mark.antenna[1]) < 2e-4
        assert h == pytest.approx(mark.antenna[2], abs=2e-4)


def _moving(times: np.ndarray, path: Path) -> ppk.Solution:
    """A drone flying east at 10 m/s, fix all along, at ``times``."""
    east = math.degrees(10.0 / (6378137.0 * math.cos(math.radians(41.134))))
    llh = np.column_stack([np.full(len(times), 41.134), 14.266 + east * (times - times[0]),
                           np.full(len(times), 94.35)])
    return ppk.Solution(times=times, llh=llh, q=np.ones(len(times), dtype=int), ns=np.full(len(times), 8),
                        sd=np.array([[0.003, 0.004, 0.009]] * len(times)), base={}, path=path)


def test_events_check_tolerance_in_metres(tmp_path):
    """A synthetic flight at 5 Hz and RTKLIB's marks written by hand: a mark 0.8 mm north of the line is within
    1 mm, one 1.5 mm north and one 2 mm higher are not, whatever their latitude and longitude are in degrees."""
    times = START_2005 + np.arange(0.0, 60.0, 0.2)
    solution = _moving(times, tmp_path / "flight.pos")
    shot_times = START_2005 + np.array([10.1, 20.3, 30.5, 40.7])
    llh, _q, _sd, ok = ppk.at(solution, shot_times)
    assert ok.all()
    north = 1.0 / math.radians(1.0) / 6378137.0  # degrees of latitude in a metre
    offsets = [(0.0008 * north, 0.0), (0.0015 * north, 0.0), (0.0, 0.002), (0.0, 0.0)]
    rows = [(t, lat + dlat, lon, h + dh, 1, 8, 0.003, 0.004, 0.009)
            for t, (lat, lon, h), (dlat, dh) in zip(shot_times, llh, offsets)]
    marks = tmp_path / f"flight{ppk.EVENTS_SUFFIX}"
    marks.write_text(pos_text(rows), newline="")
    shotset = ShotSet(shots=[Shot(index=i, gps_time=float(t), source={"kind": "rinex_events"})
                             for i, t in enumerate(shot_times, 1)], kind="rinex_events", name="rover.obs")
    (warning,) = ppk.events_check(solution, shotset)
    assert "2 time marks" in warning and "up to 2 mm" in warning and "backward" not in warning
    assert ppk.events_check(solution, shotset, tolerance_m=0.003) == []
    # in a backward run the warning says why a few millimetres are expected
    (tmp_path / "flight.conf").write_text("pos1-soltype       =backward   # (0:forward,1:backward,2:combined)\n")
    (warning,) = ppk.events_check(solution, shotset)
    assert "backward solution" in warning
    # a solution read from a file (no rover known): shots RTKLIB did not write are not called another flight
    late = ShotSet(shots=shotset.shots + [Shot(index=5, gps_time=START_2005 + 70.0, source={})], kind="rinex_events")
    texts = ppk.events_check(solution, late)
    assert any("1 of the 5 shots" in w and "writes no mark outside the epochs" in w for w in texts), texts


def test_events_check_rover_in_utc(tmp_path):
    """A rover file that writes its times in UTC: RTKLIB writes its marks without the leap seconds. Said as that."""
    lines = TM2_OBS.read_text().splitlines()
    lines = [line[:48] + "UTC" + line[51:] if "TIME OF FIRST OBS" in line else line for line in lines]
    rover = tmp_path / "utc.obs"
    rover.write_text("\n".join(lines) + "\n")
    shotset = events.rinex_events(rover)  # in GPS time: 14 s after the times the file writes
    times = shotset.shots[0].gps_time - 30.0 + np.arange(0.0, 60.0, 0.2)
    solution = _moving(times, tmp_path / "flight.pos")
    solution.inputs = {"rover": str(rover), "base": "", "navs": []}
    solution.rover = rover.name
    rows = [(s.gps_time - LEAP_2008, 41.134, 14.266, 94.35, 1, 8, 0.003, 0.004, 0.009) for s in shotset.shots]
    (tmp_path / f"flight{ppk.EVENTS_SUFFIX}").write_text(pos_text(rows), newline="")
    (warning,) = ppk.events_check(solution, shotset)
    assert "14 s from an event of the rover" in warning and "UTC" in warning


def test_interpolation_error_on_a_turn(tmp_path):
    """A drone on a circle of 20 m at 10 m/s: the straight line between epochs is 0.62 m off the path in the middle
    of epochs 1 s apart, 25 mm at 5 Hz, 6 mm at 10 Hz; the estimate from the epochs around is within 10 %."""
    r, v = 20.0, 10.0
    lat0, metre = 41.134, 1.0 / math.radians(1.0) / 6378137.0
    for hz, expected in ((1, 0.62), (5, 0.025), (10, 0.0063)):
        times = START_2005 + np.arange(0.0, 30.0, 1.0 / hz)
        angle = v / r * (times - times[0])

        def where(a):
            return np.column_stack([lat0 + r * np.sin(a) * metre,
                                    14.266 + r * np.cos(a) * metre / math.cos(math.radians(lat0)),
                                    np.full(len(a), 94.35)])

        solution = ppk.Solution(times=times, llh=where(angle), q=np.ones(len(times), dtype=int),
                                ns=np.full(len(times), 8), sd=np.full((len(times), 3), 0.01), base={},
                                path=tmp_path / "circle.pos")
        middle = times[5:-5] + 0.5 / hz
        llh, _q, _sd, ok = ppk.at(solution, middle)
        truth = where(v / r * (middle - times[0]))
        off = np.linalg.norm(enu(llh, (lat0, 14.266, 94.35)) - enu(truth, (lat0, 14.266, 94.35)), axis=1)
        assert off == pytest.approx(expected, rel=0.03)
        estimate = ppk.interpolation_error(solution, middle)
        assert estimate == pytest.approx(off, rel=0.1)
        assert ppk.interpolation_error(solution, times[3:6]).tolist() == [0.0, 0.0, 0.0]  # on epochs
        assert np.isnan(ppk.interpolation_error(solution, [times[-1] + 5.0]))[0]  # no position


def test_turns_between_epochs_in_the_accuracy(tmp_path):
    """Photos of a drone turning, its receiver at 1 Hz: their accuracy includes the interpolation, with a warning."""
    exposures = shots_test._exposures(60)
    ch = shots_test._photos(tmp_path / "f", [f"IMG_{i:04d}.JPG" for i in range(1, 61)], exposures)
    t0 = shots_test._gps(exposures[0]) - 5.0
    times = t0 + np.arange(0.0, 160.0, 1.0)
    metre = 1.0 / math.radians(1.0) / 6378137.0
    angle = 0.5 * (times - t0)  # 10 m/s on a circle of 20 m
    llh = np.column_stack([41.134 + 20 * np.sin(angle) * metre,
                           14.266 + 20 * np.cos(angle) * metre / math.cos(math.radians(41.134)),
                           np.full(len(times), 94.35)])
    solution = ppk.Solution(times=times, llh=llh, q=np.ones(len(times), dtype=int), ns=np.full(len(times), 8),
                            sd=np.full((len(times), 3), 0.005), base={}, path=tmp_path / "rover.pos")
    result = gnss_apply.apply_ppk_shots(ch, shots_test._events(exposures), solution, tmp_path / "f", by="number")
    assert result["matched"] == 60
    (warning,) = [w for w in result["warnings"] if "turns" in w]
    assert "1 Hz" in warning and "5 Hz or more" in warning
    worst = max(cam.meta["gnss"].get("interpolation_m", 0.0) for cam in ch.cameras)
    assert 0.3 < worst < 0.65
    for cam in ch.cameras:
        off = cam.meta["gnss"].get("interpolation_m", 0.0)
        assert min(cam.reference.accuracy) >= off - 1e-9


@pytest.mark.parametrize("extra, interpolated", [(0.0009, True), (0.0010, True), (0.0011, True), (0.0025, False)])
def test_epochs_a_millisecond_more_than_a_second_apart(tmp_path, extra, interpolated):
    """A receiver whose clock jumps a millisecond logs epochs 1.001 s apart: still next to each other."""
    times = np.array([START_2005, START_2005 + 1.0 + extra])
    solution = _moving(times, tmp_path / "s.pos")
    _llh, _q, _sd, ok = ppk.at(solution, [START_2005 + 0.5])
    assert bool(ok[0]) is interpolated


@pytest.mark.parametrize("system, ahead", [("GPS", 0), ("UTC", 14), ("GLO", 14), ("BDT", 14)])
def test_spans_in_gps_time(tmp_path, system, ahead):
    lines = UBX_OBS.read_text().splitlines()
    lines = [line[:48] + system + line[51:] if "TIME OF FIRST OBS" in line else line for line in lines]
    other = tmp_path / "other.obs"
    other.write_text("\n".join(lines) + "\n")
    first, last = ppk.gps_span(UBX_OBS)
    assert ppk.gps_span(other) == pytest.approx((first + ahead, last + ahead))
    assert ppk._approx_span(other) == pytest.approx((first + ahead, last + ahead), abs=0.01)


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
def test_rtcm_base_plumbing_with_rtklib_stood_in(tmp_path, monkeypatch):
    """The plumbing of an RTCM 3 log of the base, not its solution: the log converted with the middle of the rover's
    time and its RINEX given to RTKLIB. The sample has two ephemerides (one GPS, one BeiDou), too few for RTKLIB to
    compute anything even from RINEX: RTKLIB is stood in for (it writes the rows of gnss_helpers) and what it is given
    is looked at. Nothing here says that a solution against an RTCM 3 base is right."""
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
    # an RTCM 3 rover and a raw base: no RINEX file gives the day, approx_time does
    other = tmp_path / "base" / "GMSD7_copy.rtcm3"
    other.parent.mkdir()
    shutil.copy(RTCM_LOG, other)
    with pytest.raises(ValueError, match="approx_time"):
        ppk.solve(RTCM_LOG, other, [RTCM_NAV], base_position=GMSD, folder=tmp_path / "work")
    seen.clear()
    day = datetime(2012, 10, 14)
    ppk.solve(RTCM_LOG, other, [RTCM_NAV], base_position=GMSD, folder=tmp_path / "work", approx_time=day)
    assert [name for name, _ in seen] == [RTCM_LOG.name, other.name]
    assert all(convert._gps_seconds(when) == convert._gps_seconds(day) for _, when in seen)


@needs_a_converter
def test_raw_base_far_in_time_says_so(tmp_path):
    rover = _alone(UBX_OBS, tmp_path / "rover")  # May 2008, and the RTCM 3 log of the base is of October 2012
    with pytest.raises(ValueError, match="approximate time.*rover"):
        ppk.solve(rover, RTCM_LOG, [UBX_NAV], base_position=GMSD, folder=tmp_path / "work")
    assert not (tmp_path / "work").exists() or not any((tmp_path / "work").glob("*.pos"))


@needs_rtklib
@needs_a_converter
def test_stale_navigation_beside_a_raw_rover(tmp_path, monkeypatch):
    """A navigation file of 2012 beside a u-blox log of 2008: the ephemerides of the log itself are still given to
    RTKLIB, with it, and the solution is the one without it."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    log = _ubx_rover(tmp_path / "rover")
    shutil.copy(RTCM_NAV, tmp_path / "rover" / "old_flight_Rinex.nav")
    base = _alone(UBX_OBS, tmp_path / "base")
    by_hand = ppk.solve(TM2_OBS, base, [UBX_NAV], folder=tmp_path / "work")
    raw = ppk.solve(log, base, folder=tmp_path / "work")
    assert np.array_equal(raw.times, by_hand.times) and np.allclose(raw.llh, by_hand.llh, rtol=0, atol=1e-9)
    (converted,) = raw.inputs["converted"]
    assert raw.inputs["navs"] == [converted["nav"], str((tmp_path / "rover" / "old_flight_Rinex.nav").absolute())]
    # and given by hand, the stale one alone would leave nothing: the conversion's come first all the same
    given = ppk.solve(log, base, [RTCM_NAV], folder=tmp_path / "work")
    assert np.allclose(given.llh, by_hand.llh, rtol=0, atol=1e-9) and given.inputs["navs"][0] == converted["nav"]


@needs_rtklib
@needs_a_converter
def test_raw_rover_and_raw_base(tmp_path, monkeypatch):
    """The u-blox log with the marks as the rover, the same log without them as the base (its position from the fix
    of the receiver the conversion writes): the solution of the two RINEX files convbin made."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    log = _ubx_rover(tmp_path / "rover")
    base_log = _alone(UBX_LOG, tmp_path / "base")
    by_hand = ppk.solve(TM2_OBS, _alone(UBX_OBS, tmp_path / "base_obs"), [UBX_NAV], folder=tmp_path / "work")
    raw = ppk.solve(log, base_log, folder=tmp_path / "work")
    assert [item["kind"] for item in raw.inputs["converted"]] == ["ubx", "ubx"]
    assert np.array_equal(raw.times, by_hand.times) and np.allclose(raw.llh, by_hand.llh, rtol=0, atol=1e-6)
    assert ppk.events_check(raw, events.ubx_time_marks(log)) == []


@needs_rtklib
@needs_a_converter
def test_raw_rover_in_two_pieces(tmp_path, monkeypatch):
    """The u-blox log cut in two between raw epochs (as a receiver that starts a new file): each piece converted, the
    two joined, the marks of both pieces checked."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    data = (_ubx_rover(tmp_path / "whole")).read_bytes()
    epochs = ubx.raw_epochs(data)
    cut = epochs[len(epochs) // 2][0]
    first, second = tmp_path / "rover" / "rover_001.ubx", tmp_path / "rover" / "rover_002.ubx"
    first.parent.mkdir()
    first.write_bytes(data[:cut])
    second.write_bytes(data[cut:])
    base = _alone(UBX_OBS, tmp_path / "base")
    by_hand = ppk.solve(TM2_OBS, base, [UBX_NAV], folder=tmp_path / "work")
    pieces = ppk.solve([second, first], base, [UBX_NAV], folder=tmp_path / "work")
    assert len(pieces.inputs["converted"]) == 2
    assert np.array_equal(pieces.times, by_hand.times) and np.allclose(pieces.llh, by_hand.llh, rtol=0, atol=1e-9)
    marks = ShotSet(shots=[s for path in (first, second) for s in events.ubx_time_marks(path).shots],
                    kind="ubx_tim_tm2", name="rover")
    assert len(marks.shots) == 3 and ppk.events_check(pieces, marks) == []


@needs_rtklib
@needs_a_converter
def test_converted_rover_gone_from_the_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    log = _ubx_rover(tmp_path / "rover")
    raw = ppk.solve(log, _alone(UBX_OBS, tmp_path / "base"), folder=tmp_path / "work")
    shutil.rmtree(Path(raw.inputs["converted"][0]["obs"]).parent)
    (warning,) = ppk.events_check(raw, events.ubx_time_marks(log))
    assert "no longer in the cache" in warning and "marks RTKLIB wrote" in warning


# --- the marks RTKLIB keeps: one per observation epoch (real-data trials) -----------------------------------------


@needs_rtklib
def test_two_events_in_one_epoch_said_as_such(tmp_path, monkeypatch):
    """Two events between the same two epochs (00:00:30 and 00:01:00): RTKLIB writes one mark for that epoch; the
    check says so, and that no shot is lost - not that the mark is outside the solution."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    after = {1: [_event_2005(0, 40.0), _event_2005(0, 50.0)], 49: [_event_2005(24, 31.5)]}
    rover = _rover_2005(tmp_path / "3040evts.05o", after)
    solution = ppk.solve(rover, PPK_BASE, [PPK_NAV], folder=tmp_path / "work")
    warnings = ppk.events_check(solution, events.rinex_events(rover))
    shared = [w for w in warnings if "same observation epoch" in w]
    assert len(shared) == 1 and shared[0].startswith("1 of the 3 events"), warnings
    assert not any("outside the epochs" in w or "although the solution has positions" in w for w in warnings)


@needs_rtklib
@pytest.mark.parametrize("case", ["three", "gap", "observed"])
def test_events_rtklib_reads_with_the_local_change(tmp_path, monkeypatch, case):
    """rinex.c changed (native/vendor/rtklib/VERSION): of three events between two epochs the first is written
    (upstream: none); two events around a gap (00:05:00 missing) are both written and the epoch after the gap is
    solved (upstream: both marks and that epoch lost); an epoch off the grid right after an event, the observations
    of the event, is still discarded. The check finds nothing to say but the events that share an epoch."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    header, records = rinex_records(PPK_ROVER)
    observed = list(records[9])
    observed[0] = observed[0][:13] + f"{4:2d}{40.0:11.7f}" + observed[0][26:]
    after, skip, written = {
        "three": ({1: [_event_2005(0, 40.0), _event_2005(0, 45.0), _event_2005(0, 50.0)]}, None, [40.0]),
        "gap": ({9: [_event_2005(5, 10.0)], 11: [_event_2005(5, 40.0)]}, 10, [310.0, 340.0]),
        "observed": ({9: [_event_2005(4, 40.0)] + observed}, None, [280.0]),
    }[case]
    lines = list(header)
    for i, record in enumerate(records):
        if i != skip:
            lines += record + after.get(i, [])
    rover = tmp_path / "3040evts.05o"
    rover.write_text("\n".join(lines) + "\n", newline="\n")
    solution = ppk.solve(rover, PPK_BASE, [PPK_NAV], folder=tmp_path / "work")
    marks = events.events_pos(solution.path.with_name(solution.path.stem + ppk.EVENTS_SUFFIX))
    assert [round(m.gps_time % 86400, 3) for m in marks.shots] == written
    solved = set(np.round(np.asarray(solution.times) % 86400, 1).tolist())
    assert solved == {float(t) for t in range(0, 3600, 30)} - ({300.0} if case == "gap" else set())
    warnings = ppk.events_check(solution, events.rinex_events(rover))
    if case == "three":
        assert len(warnings) == 1 and warnings[0].startswith("2 of the 3 events") and "the first" in warnings[0]
    else:
        assert warnings == []


@needs_rtklib
@pytest.mark.parametrize("solution_type", ["combined", "backward"])
def test_mark_on_the_first_epoch_of_the_rover(tmp_path, monkeypatch, solution_type):
    """An event at 00:00:00 before the first record: RTKLIB places a mark from the solved epoch before the one it goes
    to, so on the first epoch a forward or combined solution writes none - said as that; a backward one writes it."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    header, records = rinex_records(PPK_ROVER)
    rover = tmp_path / "3040evts.05o"
    lines = list(header) + [_event_2005(0, 0.0)] + [line for record in records for line in record]
    rover.write_text("\n".join(lines) + "\n", newline="\n")
    solution = ppk.solve(rover, PPK_BASE, [PPK_NAV], folder=tmp_path / "work", solution=solution_type)
    warnings = ppk.events_check(solution, events.rinex_events(rover))
    if solution_type == "backward":
        assert warnings == []
    else:
        assert len(warnings) == 1 and "1 of the 1 time marks of the rover go to the first observation epoch" in \
            warnings[0], warnings


def test_solution_times_to_the_microsecond(tmp_path):
    """The options of a run write the times of the solution to the microsecond: a shot between two epochs of a .pos
    written so is placed on the line between them to 1e-5 m, where the millisecond would put it up to 0.5 ms x the
    speed off."""
    assert "out-timendec      =6" in ppk.config(None)
    rows = []
    t0, speed = 518400.100416, 8.0  # m/s eastwards
    for k in range(10):
        t = t0 + 0.1 * k
        lon = 139.0 + speed * (t - t0) / 111320.0 / math.cos(math.radians(35.0))
        rows.append(f"1316 {t:13.6f}   35.000000000  {lon:13.9f}    61.0000   1  10   0.0040   0.0030   0.0090"
                    "  -0.0010   0.0010  -0.0020   0.00   12.3")
    path = tmp_path / "micro.pos"
    path.write_text("%  GPST          latitude(deg) longitude(deg)  height(m)   Q  ns   sdn(m)   sde(m)   sdu(m)  "
                    "sdne(m)  sdeu(m)  sdun(m) age(s)  ratio\n" + "\n".join(rows) + "\n")
    solution = ppk.read_pos(path)
    when = 1316 * 604800.0 + t0 + 0.0437
    llh, _q, _sd, ok = ppk.at(solution, [when])
    lon = 139.0 + speed * 0.0437 / 111320.0 / math.cos(math.radians(35.0))
    assert ok[0] and ppk.horizontal_m(35.0, lon, llh[0][0], llh[0][1]) < 1e-5


def test_mrk_shots_between_epochs_take_the_interpolation_too(tmp_path):
    """A DJI MRK (from 1.7.8, as every other source): a shot between two epochs of a turning drone has the
    interpolation in its accuracy."""
    exposures = shots_test._exposures(30)
    ch = shots_test._photos(tmp_path / "f", [f"IMG_{i:04d}.JPG" for i in range(1, 31)], exposures)
    t0 = shots_test._gps(exposures[0]) - 5.0
    times = t0 + np.arange(0.0, 90.0, 1.0)
    metre = 1.0 / math.radians(1.0) / 6378137.0
    angle = 0.5 * (times - t0)
    llh = np.column_stack([41.134 + 20 * np.sin(angle) * metre,
                           14.266 + 20 * np.cos(angle) * metre / math.cos(math.radians(41.134)),
                           np.full(len(times), 94.35)])
    solution = ppk.Solution(times=times, llh=llh, q=np.ones(len(times), dtype=int), ns=np.full(len(times), 8),
                            sd=np.full((len(times), 3), 0.005), base={}, path=tmp_path / "rover.pos")
    mrk = shots_test._events(exposures, kind="mrk", name="Timestamp.MRK", lever_ned_mm=(0.0, 0.0, 0.0))
    result = gnss_apply.apply_ppk_shots(ch, mrk, solution, tmp_path / "f", by="order")
    assert result["matched"] == 30
    worst = max(cam.meta["gnss"].get("interpolation_m", 0.0) for cam in ch.cameras)
    assert worst > 0.3
    assert any("turns" in w for w in result["warnings"])
