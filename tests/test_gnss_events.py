"""Shots as data and the external events of a RINEX file (epoch flag 5): the times of the events in GPS time to a
tenth of a microsecond, doubles left out, events kept when pieces of a file are joined; the packing of RINEX files apart
from their reading, and heights above the sea level of a log (``"msl"``) taken as those of the EXIF."""
from __future__ import annotations

import gzip
import shutil
import struct
from datetime import date
from pathlib import Path

import numpy as np
import pytest

import convbin_helpers as ubx
from pyarchinit_photogrammetry.gnss import events, rinex, rinex_files, shots
from pyarchinit_photogrammetry.gnss import ppk as ppk_module
from gnss_helpers import PPK_BASE, PPK_NAV, PPK_ROVER, needs_rtklib
from gnss_helpers import header_and_body, rinex_lines, rinex_piece, rinex_records

DATA = Path(__file__).parent / "data"
WHOLE = DATA / "rtklib" / "07590920.05o"  # RINEX 2.10, GPS, 120 epochs at 30 s from 2005-04-02 00:00:00
TM2 = DATA / "gnss" / "phase4" / "ubx_20080526_64k_tm2.obs"  # RINEX 3.04 that convbin wrote with three TIM-TM2 events

GPS_START = date(1980, 1, 6)


def _week_tow(year: int, month: int, day: int, hour: int, minute: int, seconds: float) -> tuple[int, float]:
    days = (date(year, month, day) - GPS_START).days
    return days // 7, (days % 7) * 86400 + hour * 3600 + minute * 60 + seconds


def _gps(week: int, tow: float) -> float:
    return week * 604800.0 + tow


def _event_v2(hour: int, minute: int, seconds: float) -> str:
    """An external event of RINEX 2 on 2 April 2005, no special lines after it."""
    return f" 05  4  2 {hour:2d} {minute:2d}{seconds:11.7f}  5  0"


def _with_events(target: Path, after: dict[int, list[str]], source: Path = WHOLE) -> Path:
    """The sample of station 0759 (or ``source``) with the event lines ``after[i]`` after its record ``i``."""
    header, records = rinex_records(source)
    lines = list(header)
    for i, record in enumerate(records):
        lines += record + after.get(i, [])
    target.write_text("\n".join(lines) + "\n", newline="\n")
    return target


# three events between the epochs of the sample (records at 30 s): after 00:00:30, 00:24:30 and 00:49:30
V2_EVENTS = {1: [_event_v2(0, 0, 45.1234567)], 49: [_event_v2(0, 24, 31.0000001)], 99: [_event_v2(0, 49, 59.9999999)]}
V2_TIMES = [(0, 0, 45.1234567), (0, 24, 31.0000001), (0, 49, 59.9999999)]


def _check_times(shotset, expected: list[tuple[int, float]]) -> None:
    assert [s.index for s in shotset.shots] == list(range(1, len(expected) + 1))
    for shot, (week, tow) in zip(shotset.shots, expected):
        assert shot.source["gps_week"] == week
        assert shot.source["tow"] == pytest.approx(tow, abs=1e-9)  # to a tenth of a microsecond, and better
        assert shot.gps_time == pytest.approx(_gps(week, tow), abs=2.5e-7)  # a float of 1.4e9 s: one unit in the last
        assert shot.valid and shot.antenna is None and shot.height is None and shot.attitude is None


def test_rinex3_events():
    shotset = events.rinex_events(TM2)
    assert isinstance(shotset, shots.ShotSet) and shotset.kind == "rinex_events" and shotset.name == TM2.name
    expected = [_week_tow(2008, 5, 26, 5, 59, 35.5), _week_tow(2008, 5, 26, 5, 59, 50.25),
                _week_tow(2008, 5, 26, 6, 0, 5.1234568)]
    assert expected[0] == (1481, 107975.5)
    _check_times(shotset, expected)
    assert shotset.duplicates == 0 and shotset.warnings == []
    assert shotset.shots[0].source["kind"] == "rinex_events" and shotset.shots[0].source["line"] == 140
    assert shotset.shots[0].source["time_system"] == "GPS"
    # an observation file without events: no shots and a word that says so
    empty = events.rinex_events(DATA / "gnss" / "phase4" / "ubx_20080526_64k.obs")
    assert empty.shots == [] and empty.warnings == ["the receiver recorded no events"]


def test_rinex2_events(tmp_path):
    path = _with_events(tmp_path / "evts0920.05o", V2_EVENTS)
    shotset = events.rinex_events(path)
    _check_times(shotset, [_week_tow(2005, 4, 2, *t) for t in V2_TIMES])
    # packed: read through the plain copy, the same shots
    packed = tmp_path / "evts0920.05o.gz"
    with open(path, "rb") as src, gzip.open(packed, "wb") as dst:
        shutil.copyfileobj(src, dst)
    again = events.rinex_events(packed)
    assert [s.gps_time for s in again.shots] == [s.gps_time for s in shotset.shots]
    # an event with special lines after it (a comment): they are its, not records
    comment = "EXTERNAL EVENT".ljust(60) + "COMMENT"
    path2 = _with_events(tmp_path / "comm0920.05o", {1: [_event_v2(0, 0, 45.5)[:-1] + "1", comment]})
    assert [s.source["tow"] for s in events.rinex_events(path2).shots] == [_week_tow(2005, 4, 2, 0, 0, 45.5)[1]]
    with pytest.raises(ValueError, match="navigation"):
        events.rinex_events(DATA / "rtklib" / "07590920.05n")


def _with_time_system(source: Path, target: Path, system: str, leap: int | None = None) -> Path:
    lines = rinex_lines(source)
    out = []
    for line in lines:
        if line[60:].strip() == "TIME OF FIRST OBS":
            line = line[:48] + f"{system:<3}" + line[51:]
        if line[60:].strip() == "END OF HEADER" and leap is not None:
            out.append(f"{leap:6d}".ljust(60) + "LEAP SECONDS")
        out.append(line)
    target.write_text("\n".join(out) + "\n", newline="\n")
    return target


def test_events_in_utc_time_system(tmp_path):
    """The times of a file in UTC (or in GLONASS time, which RINEX writes as UTC) are GPS time less the leap seconds:
    13 in 2005, 14 in 2008; the leap seconds of the header when it gives them. Galileo time is GPS time, BeiDou time is
    14 s behind."""
    v2 = _with_events(tmp_path / "evts0920.05o", V2_EVENTS)
    base = [s.gps_time for s in events.rinex_events(v2).shots]
    for system, shift in (("GPS", 0), ("GAL", 0), ("UTC", 13), ("GLO", 13), ("BDT", 14)):
        shotset = events.rinex_events(_with_time_system(v2, tmp_path / f"{system}.05o", system))
        assert [s.gps_time - b for s, b in zip(shotset.shots, base)] == pytest.approx([shift] * 3, abs=1e-6), system
        assert shotset.shots[0].source["time_system"] == system
    week, tow = _week_tow(2005, 4, 2, 0, 0, 45.1234567)
    utc = events.rinex_events(_with_time_system(v2, tmp_path / "utc.05o", "UTC")).shots[0]
    assert utc.source["gps_week"] == week and utc.source["tow"] == pytest.approx(tow + 13, abs=1e-9)
    leap = events.rinex_events(_with_time_system(v2, tmp_path / "leap.05o", "UTC", leap=20)).shots
    assert leap[0].gps_time - base[0] == pytest.approx(20, abs=1e-6)
    # RINEX 3 in UTC: 14 leap seconds in May 2008
    v3 = events.rinex_events(_with_time_system(TM2, tmp_path / "tm2_utc.obs", "UTC")).shots
    assert v3[0].source["tow"] == pytest.approx(107975.5 + 14, abs=1e-9)


def test_events_across_midnight_and_the_week(tmp_path):
    """An event of Saturday 2 April 2005 at 23:59:50.5 UTC is 13 leap seconds later in GPS time: Sunday 00:00:03.5,
    second 3.5 of the next GPS week."""
    v2 = _with_events(tmp_path / "late0920.05o", {119: [_event_v2(23, 59, 50.5)]})
    gps = events.rinex_events(v2).shots[0]
    assert (gps.source["gps_week"], gps.source["tow"]) == _week_tow(2005, 4, 2, 23, 59, 50.5)
    utc = events.rinex_events(_with_time_system(v2, tmp_path / "utc.05o", "UTC")).shots[0]
    week, _ = _week_tow(2005, 4, 3, 0, 0, 0.0)
    assert date(2005, 4, 3).weekday() == 6 and utc.source["gps_week"] == week
    assert utc.source["tow"] == pytest.approx(3.5, abs=1e-9)
    assert utc.gps_time == pytest.approx(_gps(week, 3.5), abs=2.5e-7)


def test_double_events_removed(tmp_path):
    """Two events within DOUBLE_EVENT_S (a bouncing contact) are one shot: the first is kept, the second counted."""
    assert events.DOUBLE_EVENT_S == 0.005
    after = {1: [_event_v2(0, 0, 45.0), _event_v2(0, 0, 45.004)], 2: [_event_v2(0, 1, 15.0)],
             3: [_event_v2(0, 1, 45.0), _event_v2(0, 1, 45.006)]}
    shotset = events.rinex_events(_with_events(tmp_path / "dbl0920.05o", after))
    assert [s.source["tow"] for s in shotset.shots] == pytest.approx(
        [_week_tow(2005, 4, 2, 0, 0, 45.0)[1], _week_tow(2005, 4, 2, 0, 1, 15.0)[1],
         _week_tow(2005, 4, 2, 0, 1, 45.0)[1], _week_tow(2005, 4, 2, 0, 1, 45.006)[1]], abs=1e-9)
    assert [s.index for s in shotset.shots] == [1, 2, 3, 4]
    assert shotset.duplicates == 1 and any("1 double event" in w for w in shotset.warnings)


def _events_of(path: Path) -> list[str]:
    return [line for line in header_and_body(rinex_lines(path))[1] if line[26:29] == "  5"]


def test_join_keeps_external_events(tmp_path):
    """Events near the cuts of a file in pieces are written once, in their place among the epochs: an event in the
    common epochs of two pieces, one only in the piece that repeats the epoch before it, one that only the later piece
    has although the earlier one has the epochs around it."""
    whole = _with_events(tmp_path / "whole.05o", V2_EVENTS)
    header, records = rinex_records(whole)  # each epoch with the lines up to the next: an event after it is its
    assert [i for i, record in enumerate(records) if record[-1][26:29] == "  5"] == [1, 49, 99]
    expected_body = header_and_body(rinex_lines(whole))[1]

    def without_event(record: list[str]) -> list[str]:
        return [line for line in record if line[26:29] != "  5"]

    def joined(name: str, parts: list[list[list[str]]]) -> list[str]:
        pieces = [rinex_piece(tmp_path / f"{name}{k}.05o", header, part, last_obs=True) for k, part in enumerate(parts)]
        rinex.join(pieces, tmp_path / f"{name}.05o")
        return header_and_body(rinex_lines(tmp_path / f"{name}.05o"))[1]

    # the pieces share two epochs and the events among them
    assert joined("a", [records[:51], records[49:101], records[99:]]) == expected_body
    # the earlier piece cut before the event, the later one repeats the epoch before it: the event is not lost
    body = joined("b", [records[:49] + [without_event(records[49])], records[49:99] + [without_event(records[99])],
                        records[99:]])
    assert body == expected_body and len(_events_of(tmp_path / "b.05o")) == 3
    # the earlier piece has the epochs around an event but not the event, the later one has it: written in its place
    body = joined("c", [records[:49] + [without_event(records[49])] + records[50:53], records[49:]])
    assert body == expected_body
    assert [s.gps_time for s in events.rinex_events(tmp_path / "c.05o").shots] == [
        s.gps_time for s in events.rinex_events(whole).shots]
    # the later piece starts with the event the earlier one ends with: written once, no false double
    event_49 = [records[49][-1]]
    body = joined("d", [records[:50], [event_49] + records[50:]])
    assert body == expected_body and events.rinex_events(tmp_path / "d.05o").duplicates == 0
    # the later piece starts with an event earlier than the last epoch written: put back in its place
    body = joined("e", [records[:49] + [without_event(records[49])] + records[50:53], [event_49] + records[50:]])
    assert body == expected_body


def test_rinex_files_reexported():
    """The packing of RINEX files lives in rinex_files; what phase 3 imported from rinex is still there."""
    for name in ("compression", "plain", "prune_cache", "gunzip", "CACHE_DAYS", "CACHE_BYTES", "IN_USE"):
        assert getattr(rinex, name) is getattr(rinex_files, name), name
    assert rinex_files.without_packing("base.05o.gz") == "base.05o"
    assert rinex_files.without_packing("BASE.ZIP") == "BASE" and rinex_files.without_packing("a.crx") == "a.crx"
    for name in ("header", "time_span", "join", "joinable", "epoch_interval", "find_companions", "gps_minus_utc",
                 "ecef_to_llh", "llh_to_ecef", "RinexInfo"):
        assert hasattr(rinex, name), name


def test_shot_and_shotset_data():
    shot = shots.Shot(index=3, gps_time=1_000_000.5)
    assert (shot.antenna, shot.height, shot.std, shot.status, shot.lever_ned_mm, shot.attitude) == (None,) * 6
    assert shot.valid and shot.source == {}
    assert "mrk" in shots.KINDS and "rinex_events" in shots.KINDS and len(shots.KINDS) == 8
    shotset = shots.ShotSet(shots=[shot], kind="rinex_events")
    assert shotset.name == "" and shotset.warnings == [] and shotset.duplicates == 0
    with pytest.raises(ValueError, match="kind"):
        shots.ShotSet(shots=[], kind="nonsense")


def test_msl_height_counts_as_exif(monkeypatch):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.camera import Camera
    from pyarchinit_photogrammetry.gnss import apply as gnss_apply
    from pyarchinit_photogrammetry.gnss import heights, quality

    assert "msl" in quality.HEIGHTS
    monkeypatch.setattr(ppg.markers.gcp, "camera_reference_errors", lambda chunk: {})

    def chunk(kinds, crs="EPSG:4979"):
        ch = ppg.Document().add_chunk()
        ch.crs = "EPSG:32633"
        for key, kind in enumerate(kinds):
            cam = Camera(key=key, label=f"c{key}", path=f"c{key}.jpg", sensor_key=0)
            gnss_apply.set_camera_position(cam, [12.4964 + key * 1e-4, 41.9028, 100.0], crs, [0.02, 0.02, 0.04],
                                           "ardupilot", status="fix", height=kind)
            ch.cameras.append(cam)
        return ch

    assert quality.mixed_heights(chunk(["msl", "exif"])) == []
    assert quality.mixed_heights(chunk(["msl", "ellipsoidal"])) == ["ellipsoidal", "exif"]
    assert quality.summary(chunk(["msl", "msl"]))[0]["heights"] == {"msl": 2}
    assert len(heights.exif_heights_in_ellipsoidal_systems(chunk(["msl", "ellipsoidal"]))) == 1
    assert heights.check(chunk(["msl"] * 2)) == heights.check(chunk(["exif"] * 2)) != []


# --- time marks of a u-blox log (UBX-TIM-TM2) and RTKLIB's _events.pos ------------------------------------------------

PHASE4 = DATA / "gnss" / "phase4"


def _tm2(count: int, flags: int, rising: tuple[int, int, int], falling: tuple[int, int, int], *, acc_ns: int = 20,
         channel: int = 0) -> bytes:
    """A TIM-TM2 frame (u-blox Interface Description, UBX-18010854) with its rising and falling edges each given as
    (week, milliseconds of the week, nanoseconds of the millisecond)."""
    payload = struct.pack("<BBHHHIIIII", channel, flags, count, rising[0], falling[0], rising[1], rising[2],
                          falling[1], falling[2], acc_ns)
    return ubx.ubx_frame(0x0D, 0x03, payload)


RISING = 1 << 7


def _write(path: Path, *frames: bytes) -> Path:
    path.write_bytes(b"".join(frames))
    return path


def test_ubx_frames(tmp_path):
    a = ubx.ubx_frame(0x01, 0x07, bytes(92))
    b = ubx.ubx_frame(0x0D, 0x03, bytes(28))
    noise = b"$GPGGA,123519,4807.038,N*47\r\n\xb5"  # NMEA and a lone sync byte between the frames
    path = _write(tmp_path / "log.ubx", a, noise, b)
    frames = list(events.ubx_frames(path))
    assert [(c, i, len(p)) for c, i, p, _ in frames] == [(0x01, 0x07, 92), (0x0D, 0x03, 28)]
    assert frames[0][3] == 0 and frames[1][3] == len(a) + len(noise)
    assert list(events.ubx_frames(_write(tmp_path / "empty.ubx"))) == []


def test_tim_tm2_falling_and_rising(tmp_path):
    week = 2300
    marks = [
        # count 1: falling 0.2 s after the rising edge; count 2: both, the falling edge in the next week
        _tm2(1, ubx.FALLING | RISING | ubx.BASE_GNSS | ubx.TIME_VALID, (week, 100_000, 125), (week, 100_200, 999_999)),
        _tm2(2, ubx.FALLING | RISING | ubx.BASE_GNSS | ubx.TIME_VALID, (week, 604_799_999, 500_000),
             (week + 1, 0, 1), acc_ns=7),
        # count 3: no new edge (the receiver repeats the last one): no shot
        _tm2(3, ubx.BASE_GNSS | ubx.TIME_VALID, (week, 3, 0), (week, 4, 0)),
        # count 4: rising only
        _tm2(4, RISING | ubx.BASE_GNSS | ubx.TIME_VALID, (week + 1, 2_000, 0), (week + 1, 1_000, 0)),
    ]
    path = _write(tmp_path / "marks.ubx", *marks)
    falling = events.ubx_time_marks(path)
    assert falling.kind == "ubx_tim_tm2" and falling.name == "marks.ubx" and falling.duplicates == 0
    assert [s.index for s in falling.shots] == [1, 2]
    assert [(s.source["gps_week"], s.source["tow"]) for s in falling.shots] == [(week, pytest.approx(100.200999999,
                                                                                                    abs=1e-12)),
                                                                                (week + 1, pytest.approx(1e-9,
                                                                                                         abs=1e-15))]
    assert falling.shots[0].source["tow_ms"] == 100_200 and falling.shots[0].source["tow_sub_ns"] == 999_999
    assert falling.shots[1].gps_time == pytest.approx((week + 1) * 604800.0, abs=1e-6)
    assert [s.source["acc_ns"] for s in falling.shots] == [20, 7]
    assert falling.shots[0].source["edge"] == "falling" and falling.shots[0].source["time_base"] == "GNSS"
    assert all(s.valid and s.antenna is None for s in falling.shots)
    rising = events.ubx_time_marks(path, edge="rising")
    assert [s.index for s in rising.shots] == [1, 2, 4]
    assert [s.source["tow"] for s in rising.shots] == pytest.approx([100.000000125, 604799.9995, 2.0], abs=1e-9)
    # GNSS time, but the log does not say of which system: taken as GPS time, and said
    assert any("GPS time" in w for w in falling.warnings)
    with pytest.raises(ValueError, match="edge"):
        events.ubx_time_marks(path, edge="both")
    # only the other edge in the log: no shots, and a word that names it
    only_rising = events.ubx_time_marks(_write(tmp_path / "r.ubx", marks[3]))
    assert only_rising.shots == [] and any("rising" in w for w in only_rising.warnings)
    none = events.ubx_time_marks(_write(tmp_path / "none.ubx", ubx.ubx_frame(0x01, 0x07, bytes(92))))
    assert none.shots == [] and none.warnings == [events.NO_TIME_MARKS]
    # the same edge sent twice, and a bounce within DOUBLE_EVENT_S: left out and counted
    flags = ubx.FALLING | ubx.BASE_GNSS | ubx.TIME_VALID
    twice = events.ubx_time_marks(_write(tmp_path / "twice.ubx", _tm2(1, flags, (week, 0, 0), (week, 1000, 0)),
                                         _tm2(1, flags, (week, 0, 0), (week, 1000, 0)),
                                         _tm2(2, flags, (week, 0, 0), (week, 1002, 0)),
                                         _tm2(3, flags, (week, 0, 0), (week, 2000, 0))))
    assert [s.index for s in twice.shots] == [1, 3] and twice.duplicates == 2
    assert any("double" in w for w in twice.warnings)


def test_tim_tm2_gnss_time_base_from_the_time_pulse_settings(tmp_path):
    """A CFG-TP5 of the time pulse 0 (version 1) in the log says which GNSS the time grid is: GPS, no warning; BeiDou,
    Galileo or GLONASS, whose week and seconds the Interface Description does not define for TIM-TM2, marks not valid
    and a warning naming the system; none, read as GPS time with a warning."""
    def tp5(grid: int) -> bytes:
        flags = (1 << 0) | (grid << 7)  # active, gridUtcGnss in bits 7-10
        return ubx.ubx_frame(0x06, 0x31, struct.pack("<BBHhhIIIIiI", 0, 1, 0, 50, 0, 1, 1, 0, 0, 0, flags))

    mark = _tm2(1, ubx.FALLING | ubx.BASE_GNSS | ubx.TIME_VALID, (2300, 0, 0), (2300, 5000, 0))
    gps = events.ubx_time_marks(_write(tmp_path / "gps.ubx", tp5(1), mark))
    assert gps.warnings == [] and gps.shots[0].source["time_base"] == "GPS"
    for grid, name in ((2, "GLONASS"), (3, "BeiDou"), (4, "Galileo")):
        other = events.ubx_time_marks(_write(tmp_path / f"{name}.ubx", tp5(grid), mark))
        assert len(other.shots) == 1 and not other.shots[0].valid, name
        assert other.shots[0].source["time_base"] == name
        assert any(name in w and "not valid" in w for w in other.warnings), other.warnings
    unknown = events.ubx_time_marks(_write(tmp_path / "unknown.ubx", mark))
    assert unknown.shots[0].valid and any("GPS time" in w for w in unknown.warnings)


def test_tim_tm2_utc_base_to_gps(tmp_path):
    flags = ubx.FALLING | ubx.BASE_UTC | (1 << 5) | ubx.TIME_VALID
    # 2020: 18 leap seconds; the last 10 s of a UTC week are GPS time of the next week
    week = 2100
    marks = events.ubx_time_marks(_write(tmp_path / "utc.ubx", _tm2(1, flags, (week, 0, 0), (week, 1_000, 250)),
                                         _tm2(2, flags, (week, 0, 0), (week, 604_790_000, 0))))
    assert [(s.source["gps_week"], s.source["tow"]) for s in marks.shots] == [
        (week, pytest.approx(19.00000025, abs=1e-12)), (week + 1, pytest.approx(8.0, abs=1e-12))]
    assert marks.shots[0].source["time_base"] == "UTC" and marks.shots[0].source["leap_seconds"] == 18
    assert not any("GPS time" in w for w in marks.warnings)
    # 2008: 14 leap seconds
    old = events.ubx_time_marks(_write(tmp_path / "old.ubx", _tm2(1, flags, (1481, 0, 0), (1481, 107_976_250, 0))))
    assert old.shots[0].source["tow"] == pytest.approx(107990.25, abs=1e-12)


def test_bad_checksum_skipped_and_counted(tmp_path):
    flags = ubx.FALLING | ubx.BASE_GNSS | ubx.TIME_VALID
    good1 = _tm2(1, flags, (2300, 0, 0), (2300, 1000, 0))
    bad = bytearray(_tm2(2, flags, (2300, 0, 0), (2300, 2000, 0)))
    bad[-1] ^= 0xFF
    good3 = _tm2(3, flags, (2300, 0, 0), (2300, 3000, 0))
    path = _write(tmp_path / "bad.ubx", good1, bytes(bad), good3)
    skipped: list[int] = []
    frames = list(events.ubx_frames(path, skipped=skipped))
    assert len(frames) == 2 and skipped == [len(good1)]
    marks = events.ubx_time_marks(path)
    assert [s.index for s in marks.shots] == [1, 3]
    assert any("1 damaged UBX frame" in w for w in marks.warnings)
    # sync bytes by chance inside other data (RTCM 3 in the same log): no known class or a length no UBX message has,
    # not counted
    chance = b"\xd3\x00\x13" + b"\xb5\x62\x77\x01\x10\x00" + bytes(30) + b"\xb5\x62\x02\x15\xff\xff" + bytes(10)
    mixed = _write(tmp_path / "mixed.ubx", good1, chance, good3)
    skipped = []
    assert len(list(events.ubx_frames(mixed, skipped=skipped))) == 2 and skipped == []
    assert not any("damaged" in w for w in events.ubx_time_marks(mixed).warnings)


def test_tim_tm2_count_unwrapped(tmp_path):
    """The counter of TIM-TM2 has 16 bits: after 65535 it starts again from 0, the shots go on counting."""
    flags = ubx.FALLING | ubx.BASE_GNSS | ubx.TIME_VALID
    frames = [_tm2(count, flags, (2300, 0, 0), (2300, 1000 * (i + 1), 0))
              for i, count in enumerate((65534, 65535, 0, 1))]
    marks = events.ubx_time_marks(_write(tmp_path / "wrap.ubx", *frames))
    assert [s.index for s in marks.shots] == [65534, 65535, 65536, 65537]
    assert [s.source["count"] for s in marks.shots] == [65534, 65535, 0, 1]


def test_invalid_time_mark(tmp_path):
    marks = events.ubx_time_marks(_write(tmp_path / "inv.ubx", _tm2(1, ubx.FALLING | ubx.BASE_GNSS, (2300, 0, 0),
                                                                    (2300, 1000, 0))))
    assert len(marks.shots) == 1 and not marks.shots[0].valid
    assert any("not valid" in w for w in marks.warnings)
    # RTKLIB's line for a time mark it could not place: a shot that is not valid, in time order with the others
    pos = tmp_path / "run_events.pos"
    pos.write_text(
        "% program   : RTKLIB ver.demo5 b34k\n"
        "% (lat/lon/height=WGS84/ellipsoidal,Q=1:fix,2:float,3:sbas,4:dgps,5:single,6:ppp,ns=# of satellites)\n"
        "%  GPST          latitude(deg) longitude(deg)  height(m)   Q  ns   sdn(m)   sde(m)   sdu(m)  sdne(m)  sdeu(m)"
        "  sdun(m) age(s)  ratio\n"
        "1316   518445.123   35.648312345  139.789012345    61.2345   1   9   0.0040   0.0030   0.0090  -0.0010"
        "   0.0010  -0.0020   0.00   12.3\n"
        "1316   518400.500   Q=0, Time mark is not valid\n"
        "1316   518475.000   35.648312400  139.789012400    61.2400   2   8   0.0500   0.0400   0.0900   0.0000"
        "   0.0000   0.0000   0.00    0.0\n")
    shotset = events.events_pos(pos)
    assert shotset.kind == "events_pos" and [s.index for s in shotset.shots] == [1, 2, 3]
    first, second, third = shotset.shots
    assert not first.valid and first.antenna is None and first.status == "none"
    assert first.gps_time == pytest.approx(1316 * 604800.0 + 518400.5, abs=1e-6)
    assert second.valid and second.antenna == pytest.approx((35.648312345, 139.789012345, 61.2345), abs=1e-9)
    assert second.height == "ellipsoidal" and second.status == "fix"
    assert second.std == pytest.approx((0.004, 0.003, 0.009))  # north, east, up
    assert third.status == "float" and third.source["q"] == 2 and third.source["ns"] == 8
    assert any("1 time mark" in w and "not valid" in w for w in shotset.warnings)
    # only marks RTKLIB could not place: shots all the same, none valid
    lines = pos.read_text().splitlines()
    only = tmp_path / "only_events.pos"
    only.write_text("\n".join(lines[:3] + [lines[4]]) + "\n")
    assert [s.valid for s in events.events_pos(only).shots] == [False]


# the time marks that tests/data/gnss/phase4/ubx_20080526_64k_tm2.obs was converted from (README.md there): frame and
# the GPS time it is put after
WEEK_1481 = 1481 * 604800.0
MARKS = [
    (ubx.tim_tm2(1481, 107975.5, count=1), WEEK_1481 + 107975.5),
    (ubx.tim_tm2(1481, 107990.25 - 14, flags=ubx.FALLING | ubx.BASE_UTC | ubx.TIME_VALID, count=2),
     WEEK_1481 + 107990.25),
    (ubx.tim_tm2(1481, 108005.123456789, count=3), WEEK_1481 + 108005.123456789),
    (ubx.tim_tm2(1481, 108015.0, flags=(1 << 7) | ubx.BASE_GNSS | ubx.TIME_VALID, count=4), WEEK_1481 + 108015.0),
]


def test_same_events_as_convbin(tmp_path):
    """The u-blox log read by the program and the RINEX file convbin made of it: the same events at the same times."""
    data = (PHASE4 / "ubx_20080526_64k.ubx").read_bytes()
    log = tmp_path / "ubx_20080526_64k_tm2.ubx"
    log.write_bytes(ubx.insert_time_marks(data, [m for m, _ in MARKS], [t for _, t in MARKS]))
    marks = events.ubx_time_marks(log)
    converted = events.rinex_events(TM2)
    assert [s.index for s in marks.shots] == [1, 2, 3] and len(converted.shots) == 3
    for mark, event in zip(marks.shots, converted.shots):
        assert mark.source["gps_week"] == event.source["gps_week"]
        assert mark.source["tow"] == pytest.approx(event.source["tow"], abs=1e-7)  # RINEX writes 1e-7 s
    assert marks.shots[1].source["time_base"] == "UTC" and marks.shots[1].source["leap_seconds"] == 14
    assert [s.source["tow"] for s in marks.shots] == pytest.approx([107975.5, 107990.25, 108005.123456789], abs=1e-9)


@needs_rtklib
def test_events_pos_read(tmp_path, monkeypatch):
    """The time marks of a rover processed by RTKLIB (``<solution>_events.pos``) read as shots: times of events of the
    rover, the positions RTKLIB interpolated between the epochs around each one (within 1 mm of a linear interpolation
    of the solution)."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    after = {1: [_event_v2(0, 0, 45.125)], 49: [_event_v2(0, 24, 31.5)], 79: [_event_v2(0, 39, 50.25)],
             99: [_event_v2(0, 49, 50.25)]}
    rover = _with_events(tmp_path / "3040evts.05o", after, source=PPK_ROVER)
    solution = ppk_module.solve(rover, PPK_BASE, [PPK_NAV], folder=tmp_path / "work")
    pos = solution.path.with_name(solution.path.stem + ppk_module.EVENTS_SUFFIX)
    shotset = events.events_pos(pos)
    expected = [_gps(*_week_tow(2005, 4, 2, *t)) for t in ((0, 0, 45.125), (0, 24, 31.5), (0, 39, 50.25),
                                                          (0, 49, 50.25))]
    # every mark is written: RTKLIB-EX 2.5.1 as released lost the marks of the epochs whose single point positioning
    # it skips (rtkpos.c: the time mark of the epoch was not taken there; the local fix is in
    # native/vendor/rtklib/VERSION) - two of these four with this pair
    assert shotset.kind == "events_pos" and [s.index for s in shotset.shots] == list(range(1, len(shotset.shots) + 1))
    assert len(shotset.shots) == 4
    for shot in shotset.shots:
        assert min(abs(shot.gps_time - t) for t in expected) < 1e-3  # RTKLIB writes the times to the millisecond
    placed = [s for s in shotset.shots if s.valid]
    assert placed, shotset.warnings
    for shot in placed:
        assert shot.height == "ellipsoidal" and shot.status in ("fix", "float", "single")
        lat, lon, h = (np.interp(shot.gps_time, solution.times, solution.llh[:, i]) for i in range(3))
        assert shot.antenna[0] == pytest.approx(lat, abs=1e-8) and shot.antenna[1] == pytest.approx(lon, abs=1e-8)
        assert shot.antenna[2] == pytest.approx(h, abs=1e-3)


# --- validity of the marks, gaps of the counter (real-data trials) -------------------------------------------------


def _event_v2_not_valid(hour: int, minute: int, seconds: float) -> list[str]:
    """An external event of RINEX 2 as RTKLIB writes a mark without a valid time: one special line, its comment."""
    return [f" 05  4  2 {hour:2d} {minute:2d}{seconds:11.7f}  5  1", f"{' Time mark is not valid':<60}COMMENT"]


def test_rinex_marks_without_valid_time(tmp_path):
    """RTKLIB writes a TIM-TM2 mark without a valid time with the comment "Time mark is not valid": read back as not
    valid, the warning telling marks before the first fix from those taken after losing it."""
    after = {1: _event_v2_not_valid(0, 0, 45.0), 49: [_event_v2(0, 24, 31.0)], 99: _event_v2_not_valid(0, 49, 50.0)}
    shotset = events.rinex_events(_with_events(tmp_path / "inv0920.05o", after))
    assert [s.valid for s in shotset.shots] == [False, True, False]
    assert [s.source["time_valid"] for s in shotset.shots] == [False, True, False]
    warning = [w for w in shotset.warnings if "without a valid time" in w]
    assert len(warning) == 1 and "1 before its first fix" in warning[0] and "1 after losing it in flight" in warning[0]
    # every event read for the check of RTKLIB's marks: the invalid ones too
    assert len(events.rinex_event_times(tmp_path / "inv0920.05o")) == 3


def test_tim_tm2_valid_lost_in_flight_and_counter_gaps(tmp_path):
    """Flags 0xcd, 0x8d, 0xcd: the middle mark without a valid time (the fix lost in flight); a counter that jumps from
    3 to 6 says that two pulses were never reported."""
    frames = [_tm2(count, flags, (2300, 0, 0), (2300, 1000 * (i + 1), 0))
              for i, (count, flags) in enumerate(((1, 0xCD), (2, 0x8D), (3, 0xCD), (6, 0xCD)))]
    marks = events.ubx_time_marks(_write(tmp_path / "lost.ubx", *frames))
    assert [s.valid for s in marks.shots] == [True, False, True, True]
    assert any("1 time mark without a valid time" in w and "1 after losing it in flight" in w
               and "before its first fix" not in w for w in marks.warnings)
    assert any("2 pulses of the event input never reported" in w for w in marks.warnings)
    whole = events.ubx_time_marks(_write(tmp_path / "whole.ubx", *frames[:3]))
    assert not any("never reported" in w for w in whole.warnings)


def test_tim_tm2_counters_per_channel_wraps_and_resets(tmp_path):
    """Two event inputs counted apart (EXTINT0 1, 2, 3; EXTINT1 7, 8): no gap. A wrap 65534, 65535, 0, 1: no gap,
    indices going on. A drop not near the wrap (40, 41, 3, 4: the receiver restarted) is a reset, said, the gap across
    it not counted, the indices going on after the last."""
    def frames(counts, channels=None):
        channels = channels or [0] * len(counts)
        return [_tm2(c, 0xCD, (2300, 0, 0), (2300, 1000 * (i + 1), 0), channel=ch)
                for i, (c, ch) in enumerate(zip(counts, channels))]
    two = events.ubx_time_marks(_write(tmp_path / "two.ubx", *frames([1, 7, 2, 8, 3], [0, 1, 0, 1, 0])))
    assert not any("never reported" in w or "started again" in w for w in two.warnings), two.warnings
    assert any("2 event inputs" in w for w in two.warnings)
    wrap = events.ubx_time_marks(_write(tmp_path / "wrap.ubx", *frames([65534, 65535, 0, 1])))
    assert [s.index for s in wrap.shots] == [65534, 65535, 65536, 65537]
    assert not any("never reported" in w or "started again" in w for w in wrap.warnings), wrap.warnings
    reset = events.ubx_time_marks(_write(tmp_path / "reset.ubx", *frames([40, 41, 3, 4, 6])))
    assert [s.index for s in reset.shots] == [40, 41, 42, 43, 45]
    assert any("started again 1 time (41 -> 3)" in w for w in reset.warnings), reset.warnings
    assert any("1 pulse of the event input never reported" in w for w in reset.warnings), reset.warnings


def test_event_epochs_as_rtklib_gives_them(tmp_path):
    """An event goes to the observation epoch after it, or to the one before when it is earlier than that epoch (a
    receiver that writes its events after the epoch that follows them): two events then share an epoch."""
    after = {1: [_event_v2(0, 0, 45.0)], 2: [_event_v2(0, 0, 50.0)], 49: [_event_v2(0, 24, 31.0)]}
    path = _with_events(tmp_path / "late0920.05o", after)
    assert [epoch for _t, epoch in events.rinex_event_epochs(path)] == [2, 2, 50]


def _rinex_with(target: Path, after: dict[int, list[str]], skip: int | None = None, source: Path = WHOLE) -> Path:
    """As :func:`_with_events`, record ``skip`` left out (a gap); ``after[-1]`` goes before the first record."""
    header, records = rinex_records(source)
    lines = list(header) + after.get(-1, [])
    for i, record in enumerate(records):
        if i != skip:
            lines += record + after.get(i, [])
    target.write_text("\n".join(lines) + "\n", newline="\n")
    return target


def _epoch_at(index: int, minute: int, seconds: float, source: Path = WHOLE) -> list[str]:
    """Record ``index`` of ``source`` written again at 00:minute:seconds: the observations of an event, off the grid."""
    record = list(rinex_records(source)[1][index])
    record[0] = record[0][:13] + f"{minute:2d}{seconds:11.7f}" + record[0][26:]
    return record


@pytest.mark.parametrize("case, after, skip, epochs", [
    ("three", {1: [_event_v2(0, 0, 40.0), _event_v2(0, 0, 45.0), _event_v2(0, 0, 50.0)]}, None, [2, 2, 2]),
    ("gap", {9: [_event_v2(0, 5, 10.0)], 11: [_event_v2(0, 5, 40.0)]}, 10, [10, 11]),
    ("first", {-1: [_event_v2(0, 0, 0.0)]}, None, [0]),
    ("observed", {9: [_event_v2(0, 4, 40.0)] + _epoch_at(9, 4, 40.0), 11: [_event_v2(0, 5, 40.0)]}, None, [10, 12]),
])
def test_event_epochs_with_the_changed_rinex_reader(tmp_path, case, after, skip, epochs):
    """The epochs RTKLIB gives events with the local change of rinex.c: three events between two epochs all go to
    the next (the first kept); an epoch after a gap, or the first, is an epoch like any other; an epoch off the grid
    right after an event (the observations of the event) is discarded, the event going to the next."""
    path = _rinex_with(tmp_path / f"{case}0920.05o", after, skip)
    assert [epoch for _t, epoch in events.rinex_event_epochs(path)] == epochs

