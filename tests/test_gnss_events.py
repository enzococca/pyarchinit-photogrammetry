"""Shots as data and the external events of a RINEX file (epoch flag 5): the times of the events in GPS time to a
tenth of a microsecond, doubles left out, events kept when pieces of a file are joined; the packing of RINEX files apart
from their reading, and heights above the sea level of a log (``"msl"``) taken as those of the EXIF."""
from __future__ import annotations

import gzip
import shutil
from datetime import date
from pathlib import Path

import pytest

from pyarchinit_photogrammetry.gnss import events, rinex, rinex_files, shots
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


def _with_events(target: Path, after: dict[int, list[str]]) -> Path:
    """The sample of station 0759 with the event lines ``after[i]`` after its record ``i``."""
    header, records = rinex_records(WHOLE)
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
