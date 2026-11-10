"""ArduPilot DataFlash logs (``gnss.autopilot``): the binary and text formats, scaled fields, damaged and hostile
files, and the camera shots (CAM, TRIG) with their GPS time, position above the sea level, attitude and GPS status."""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import numpy as np
import pytest

from pyarchinit_photogrammetry.gnss import apply as gnss_apply
from pyarchinit_photogrammetry.gnss import autopilot, shots
import gnss_dataflash as dfw
from gnss_dataflash import LogWriter, flight


def _write(tmp_path: Path, log: LogWriter, name: str = "flight.bin", text: bool = False) -> Path:
    path = tmp_path / name
    if text:
        path.write_text(log.text())
    else:
        path.write_bytes(log.binary())
    return path


# the shot times from TimeUS and the fixes: the SMS of GPA is to the millisecond, so a time is within this of the
# truth (half a millisecond of rounding at each end of the interpolation, and the microseconds of TimeUS)
TIME_TOL_S = 0.0015


def _check_shots(shotset, expected, base: str = "gpa_sms", tol: float = TIME_TOL_S) -> None:
    assert [s.index for s in shotset.shots] == [e["img"] for e in expected]
    for shot, e in zip(shotset.shots, expected):
        assert abs(shot.gps_time - e["true"]) <= tol, (shot.source, e["true"])
        week, tow = shot.source["gps_week"], shot.source["tow"]
        assert week * 604800 + tow == pytest.approx(shot.gps_time, abs=1e-6) and 0 <= tow < 604800
        assert (shot.source["cam_gps_week"], shot.source["cam_tow_ms"]) == (e["cam_week"], e["cam_tow_ms"])
        assert shot.source["time_us"] == e["time_us"] and shot.source["time_base"] == base
        assert shot.antenna == pytest.approx((e["lat"] * 1e-7, e["lng"] * 1e-7, e["alt"] * 0.01), abs=1e-9)
        assert shot.height == "msl"
        assert shot.attitude == pytest.approx(e["attitude"], abs=1e-4)  # float32 degrees of master
        assert shot.status == (None if e["status"] is None else autopilot.GPS_STATUS[e["status"]])
        if e["hacc"] is None:
            assert shot.std is None
        else:
            assert shot.std == pytest.approx((e["hacc"] * 0.01, e["hacc"] * 0.01, e["vacc"] * 0.01))
        assert shot.valid


def test_binary_cam_messages(tmp_path):
    log, expected = flight(50)
    path = _write(tmp_path, log)
    shotset = autopilot.ardupilot(path)
    assert shotset.kind == "ardupilot_cam" and shotset.name == "flight.bin" and shotset.path == str(path)
    assert len(shotset.shots) == 50
    _check_shots(shotset, expected)
    assert [s.status for s in shotset.shots] == ["fix"] * 25 + ["float"] * 25
    assert all(np.diff([s.gps_time for s in shotset.shots]) > 0)
    first = shotset.shots[0]
    assert first.source["kind"] == "ardupilot_cam" and first.source["message"] == "CAM"
    assert first.source["gps_alt"] == pytest.approx(151.40) and first.source["rel_alt"] == pytest.approx(60.12)
    assert first.source["gps_status"] == 6 and first.source["gps_instance"] == 0
    json.dumps([s.source for s in shotset.shots])  # plain numbers: saved with the project
    assert all(type(v) in (int, float) for v in first.antenna + first.attitude + first.std)
    assert type(first.index) is int
    # the log as read
    with autopilot.read_dataflash(path) as df:
        assert df.kind == "binary" and df.truncated == 0 and df.skipped_bytes == 0
        assert {"FMT", "GPS", "GPA", "CAM", "UNIT", "MULT", "FMTU"} <= set(df.formats)
        assert df.formats["CAM"]["format"] == dfw.CAM_FORMAT[0]
        assert df.formats["CAM"]["columns"] == dfw.CAM_FORMAT[1].split(",")
        assert df.count("CAM") == 50 and len(df.messages("GPS")) == df.count("GPS")
        assert df.units["CAM"]["Lat"] == {"unit": "deg", "mult": 1e-7}


def test_text_log_same_as_binary(tmp_path):
    log, expected = flight(50)
    binary = autopilot.ardupilot(_write(tmp_path, log))
    text = autopilot.ardupilot(_write(tmp_path, log, "flight.log", text=True))
    assert text.kind == "ardupilot_cam" and len(text.shots) == 50
    _check_shots(text, expected)
    for a, b in zip(binary.shots, text.shots):
        assert (a.index, a.gps_time, a.status, a.height) == (b.index, b.gps_time, b.status, b.height)
        assert a.antenna == pytest.approx(b.antenna, abs=1e-9) and a.attitude == pytest.approx(b.attitude)
    with autopilot.read_dataflash(tmp_path / "flight.log") as df:
        assert df.kind == "text"
        assert np.allclose(df.messages("GPS")["Lat"], autopilot.read_dataflash(tmp_path / "flight.bin")
                           .messages("GPS")["Lat"])


def test_trig_when_no_cam_warns(tmp_path):
    log, expected = flight(12, message="TRIG")
    path = _write(tmp_path, log)
    shotset = autopilot.ardupilot(path)
    assert shotset.kind == "ardupilot_trig" and len(shotset.shots) == 12
    _check_shots(shotset, expected)
    assert any("TRIG" in w and "command" in w for w in shotset.warnings)
    assert "ardupilot_trig" not in shots.NUMBERED_KINDS
    with pytest.raises(ValueError, match="CAM"):
        autopilot.ardupilot(path, message="cam")
    # a log with neither
    empty = LogWriter()
    empty.fmt("GPS", *dfw.GPS_FORMAT)
    with pytest.raises(ValueError, match="no camera messages"):
        autopilot.ardupilot(_write(tmp_path, empty, "none.bin"))
    with pytest.raises(ValueError, match="message"):
        autopilot.ardupilot(path, message="photo")


def test_no_fix_rows_left_out(tmp_path):
    log, expected = flight(20, no_fix=3)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    assert [s.index for s in shotset.shots] == list(range(4, 21))
    _check_shots(shotset, expected[3:])
    assert any("3 CAM" in w and "GPS" in w for w in shotset.warnings)
    info = autopilot.cameras(tmp_path / "flight.bin")
    first, last = expected[3], expected[-1]
    assert info == [{"instance": 0, "cam": 20, "trig": 0, "no_fix": 3,
                     "first": first["cam_week"] * 604800 + first["cam_tow_ms"] / 1000.0,
                     "last": last["cam_week"] * 604800 + last["cam_tow_ms"] / 1000.0, "first_img": 1, "last_img": 20}]


def test_two_cameras_need_a_choice(tmp_path):
    log, expected = flight(10, instances=(0, 1))
    path = _write(tmp_path, log)
    with pytest.raises(ValueError, match=r"2 cameras.*0, 1"):
        autopilot.ardupilot(path)
    second = autopilot.ardupilot(path, camera=1)
    _check_shots(second, [e for e in expected if e["inst"] == 1])
    assert all(s.source["instance"] == 1 for s in second.shots)
    with pytest.raises(ValueError, match="camera 3"):
        autopilot.ardupilot(path, camera=3)
    assert [c["instance"] for c in autopilot.cameras(path)] == [0, 1]
    assert [c["cam"] for c in autopilot.cameras(path)] == [10, 10]


def test_truncated_last_message(tmp_path):
    log, expected = flight(10)
    data = log.binary()
    path = tmp_path / "cut.bin"
    path.write_bytes(data[:-7])  # the last message (a CAM, a GPS or a GPA) is cut
    with autopilot.read_dataflash(path) as df:
        assert df.truncated == 1
        full = autopilot.read_dataflash(_write(tmp_path, log))
        assert sum(df.count(n) for n in ("GPS", "GPA", "CAM")) == sum(full.count(n) for n in ("GPS", "GPA", "CAM")) - 1
        full.close()
    shotset = autopilot.ardupilot(path)
    assert len(shotset.shots) in (9, 10)
    assert any("cut" in w for w in shotset.warnings)
    # a text log cut in the middle of a line
    text = log.text()
    (tmp_path / "cut.log").write_text(text[: text.rindex(",") + 1])
    with autopilot.read_dataflash(tmp_path / "cut.log") as df:
        assert df.truncated == 1


def test_scaled_fields(tmp_path):
    log = LogWriter()
    log.units({"m": "m", "d": "deg", "s": "s"}, {"F": 1e-6, "C": 1e-3, "B": 1e-2, "G": 1e-7, "-": 0.0, "?": 1.0})
    log.fmt("TEST", "QLcCeEHhIfa", "TimeUS,Lat,Rol,Yaw,Alt,Dist,Mm,Raw,Ms,F,Arr")
    # Lat: L with its MULT G - one scaling, not two; Mm: H with MULT C; Raw: h with MULT '-' (none); F: f with '?'
    log.fmtu("TEST", "sdddmmm-s--", "FGBBBBC-C?-")
    arr = list(range(-16, 16))
    log.add("TEST", 1_500_000, -353632621, -1234, 35999, 58407, 4294967295, 2500, -7, 61234, 1.5, arr)
    path = _write(tmp_path, log)
    with autopilot.read_dataflash(path) as df:
        row = df.messages("TEST")[0]
        assert row["TimeUS"] == pytest.approx(1.5)
        assert row["Lat"] == pytest.approx(-35.3632621, abs=1e-12)
        assert row["Rol"] == pytest.approx(-12.34) and row["Yaw"] == pytest.approx(359.99)
        assert row["Alt"] == pytest.approx(584.07) and row["Dist"] == pytest.approx(42949672.95)
        assert row["Mm"] == pytest.approx(2.5) and row["Raw"] == -7 and row["Ms"] == pytest.approx(61.234)
        assert row["F"] == pytest.approx(1.5)
        assert list(row["Arr"]) == arr
        raw = df.messages("TEST", multipliers=False)[0]
        assert raw["TimeUS"] == 1_500_000 and raw["Mm"] == 2500 and raw["Ms"] == 61234
        assert raw["Lat"] == pytest.approx(-35.3632621, abs=1e-12) and raw["Alt"] == pytest.approx(584.07)
        assert df.units["TEST"]["Mm"] == {"unit": "m", "mult": 1e-3}
        assert df.units["TEST"]["Raw"] == {"unit": None, "mult": None}
        units = df.messages("UNIT")
        assert list(units["Label"]) == ["m", "deg", "s"]
    # the same from the text log
    with autopilot.read_dataflash(_write(tmp_path, log, "t.log", text=True)) as df:
        row = df.messages("TEST")[0]
        assert row["Lat"] == pytest.approx(-35.3632621, abs=1e-12) and row["Mm"] == pytest.approx(2.5)
        assert row["Rol"] == pytest.approx(-12.34) and row["TimeUS"] == pytest.approx(1.5)
        assert list(row["Arr"]) == arr
    # a text log that writes the ids of UNIT and MULT as their characters
    text = log.text().splitlines()
    for i, line in enumerate(text):
        if line.startswith(("UNIT,", "MULT,")):
            parts = line.split(", ")
            parts[2] = chr(int(parts[2]))
            text[i] = ", ".join(parts)
    (tmp_path / "c.log").write_text("\n".join(text) + "\n")
    with autopilot.read_dataflash(tmp_path / "c.log") as df:
        assert df.units["TEST"]["Mm"] == {"unit": "m", "mult": 1e-3} and df.bad_lines == 0
        assert df.messages("TEST")[0]["Mm"] == pytest.approx(2.5)


def test_fmt_redefined_mid_log(tmp_path):
    log = LogWriter()
    log.fmt("XYZ", "QH", "TimeUS,A", type_id=200)
    log.add("XYZ", 1, 10)
    log.add("XYZ", 2, 11)
    log.fmt("XYZ", "QHf", "TimeUS,A,B", type_id=200)  # same type, one field more
    log.add("XYZ", 3, 12, 0.5)
    log.fmt("ABC", "Qi", "TimeUS,V", type_id=200)  # the type given to another message
    log.add("ABC", 4, -5)
    for name, text in (("r.bin", False), ("r.log", True)):
        with autopilot.read_dataflash(_write(tmp_path, log, name, text=text)) as df:
            xyz = df.messages("XYZ", multipliers=False)
            assert list(xyz["TimeUS"]) == [1, 2, 3] and list(xyz["A"]) == [10, 11, 12]
            assert np.isnan(xyz["B"][:2]).all() and xyz["B"][2] == 0.5
            assert list(df.messages("ABC")["V"]) == [-5]
            assert df.formats["ABC"]["type"] == 200 and df.formats["XYZ"]["format"] == "QHf"


def test_damaged_bytes_skipped_and_counted(tmp_path):
    log, expected = flight(10)
    data = bytearray(log.binary())
    # garbage in the middle, a header byte broken
    middle = len(data) // 2
    data[middle:middle] = b"\x00\x13\xa3\x00garbage\xa3"
    path = tmp_path / "damaged.bin"
    path.write_bytes(bytes(data))
    with autopilot.read_dataflash(path) as df:
        assert df.skipped_bytes > 0 and df.truncated == 0
    shotset = autopilot.ardupilot(path)
    assert len(shotset.shots) >= 9
    assert any("skipped" in w for w in shotset.warnings)


@pytest.mark.parametrize("content", [b"", b"\x00" * 5000, os.urandom(20000), b"hello, world\nthis is no log\n",
                                     b"\xa3\x95\x80" + b"\xff" * 86, b"FMT, 128, 89, FMT, BBnNZ\n"])
def test_garbage_is_a_clear_error(tmp_path, content):
    path = tmp_path / "garbage.bin"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        autopilot.ardupilot(path)


def test_hostile_formats_never_crash(tmp_path):
    log = LogWriter()
    log.fmt("CAM", *dfw.CAM_FORMAT)
    data = bytearray(log.binary())

    def fmt(type_id: int, length: int, name: bytes, form: bytes, columns: bytes) -> bytes:
        import struct
        return b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", type_id, length, name, form, columns)

    data += fmt(150, 2, b"BAD1", b"Q", b"TimeUS")  # length below the header
    data += fmt(151, 40, b"BAD2", b"Qx", b"TimeUS,X")  # unknown character
    data += fmt(152, 20, b"BAD3", b"QQ", b"TimeUS,A")  # size disagrees with the length
    data += fmt(153, 19, b"BAD4", b"QQ", b"TimeUS")  # columns and characters disagree
    data += fmt(155, 19, b"\xff\xfe\x00\x00", b"QQ", b"A,B")  # a name that is not text
    data += fmt(154, 19, b"OK5", b"QQ", b"A,A")  # a column twice
    data += fmt(128, 30, b"FMT", b"BB", b"A,B")  # the format of FMT itself redefined
    data += b"\xa3\x95\x99" + b"\x00" * 17  # a message of the bad type 153: skipped by its length
    data += b"\xa3\x95\x9a" + b"\x01" * 16  # type 154: read with its columns renamed
    path = tmp_path / "hostile.bin"
    path.write_bytes(bytes(data))
    with autopilot.read_dataflash(path) as df:
        assert set(df.bad_formats) >= {"BAD1", "BAD2", "BAD3", "BAD4"} or len(df.bad_formats) >= 4
        assert df.undecoded >= 1
        assert df.formats["FMT"]["format"] == "BBnNZ"
        for name in df.formats:
            df.messages(name)
        assert df.messages("OK5").dtype.names == ("A", "A_2") and df.count("OK5") == 1
    with pytest.raises(ValueError, match="no camera messages"):
        autopilot.ardupilot(path)


def test_mutated_logs_give_shots_or_a_clear_error(tmp_path):
    """Bytes of a good log changed, cut or repeated at random: shots, or a ValueError - never another error."""
    log, _ = flight(8, instances=(0,))
    log.fmt("PARM", "QNf", "TimeUS,Name,Value")
    log.add("PARM", 1, "GPS_POS1_X", 0.0)
    good = log.binary()
    text = log.text().encode()
    rng = np.random.default_rng(7)
    for trial in range(300):
        data = bytearray(good if trial % 3 else text)
        for _ in range(int(rng.integers(1, 12))):
            k = int(rng.integers(0, len(data)))
            action = int(rng.integers(0, 4))
            if action == 0:
                data[k] = int(rng.integers(0, 256))
            elif action == 1:
                del data[k:k + int(rng.integers(1, 200))]
            elif action == 2:
                data[k:k] = data[max(0, k - 90):k]
            else:
                data[k:k] = bytes(rng.integers(0, 256, int(rng.integers(1, 40)), dtype=np.uint8))
        path = tmp_path / f"m{trial % 2}.{'bin' if trial % 3 else 'log'}"
        path.write_bytes(bytes(data))
        try:
            shotset = autopilot.ardupilot(path)
        except ValueError:
            continue
        json.dumps([s.source for s in shotset.shots])
        assert all(math.isfinite(s.gps_time) and s.height == "msl" for s in shotset.shots)


def test_wrong_week_in_gpstime_is_not_valid(tmp_path):
    """A CAM whose GPSTime names a fix a week away from its time (a wrong week, a bad fix) is not valid, said."""
    log, expected = flight(10)
    rows = [r for r in log.records if r[0] == "CAM"]
    name, (type_id, fmt, values) = rows[4]
    values = list(values)
    values[4] = values[4] - 1  # one week early
    log.records[log.records.index(rows[4])] = (name, (type_id, fmt, tuple(values)))
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    assert any("1 CAM shot whose GPSTime disagrees" in w for w in shotset.warnings)
    bad = [s for s in shotset.shots if s.source["img"] == 5][0]
    assert not bad.valid and bad.source["gps_time_disagrees_s"] == pytest.approx(604800.0, abs=1.0)
    assert abs(bad.gps_time - expected[4]["true"]) <= TIME_TOL_S  # its time is still that of TimeUS
    assert sum(s.valid for s in shotset.shots) == 9


@pytest.mark.parametrize("hz", [1, 2, 5, 10])
def test_shot_time_from_timeus_not_from_the_last_fix(tmp_path, hz):
    """GPSTime is the time of the last fix (as the firmware writes it): the shot time is TimeUS put in GPS time,
    within TIME_TOL_S of the truth at any rate of the receiver, with a clock 30 ppm fast."""
    log, expected = flight(40, gps_hz=hz, drift_ppm=30.0, interval_ms=1777)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    _check_shots(shotset, expected)
    errors = [s.gps_time - e["true"] for s, e in zip(shotset.shots, expected)]
    stale = [e["cam_week"] * 604800 + e["cam_tow_ms"] / 1000.0 - e["true"] for e in expected]
    assert max(map(abs, errors)) <= TIME_TOL_S
    assert min(stale) < -0.4 / hz  # what GPSTime alone would have given: up to a period early
    assert not any("disagrees" in w for w in shotset.warnings)


def test_two_shots_within_one_gps_period(tmp_path):
    offsets = [1.31, 1.35, 3.0, 3.05, 5.1, 7.3, 7.33]
    log, expected = flight(shot_offsets_s=offsets, gps_hz=5)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    _check_shots(shotset, expected)
    times = [s.gps_time for s in shotset.shots]
    assert np.diff(times) == pytest.approx(np.diff(offsets), abs=2 * TIME_TOL_S)
    assert expected[0]["cam_tow_ms"] == expected[1]["cam_tow_ms"]  # the same last fix for both
    # neither is taken for a double: the old GPSTime gave both the same time
    assert all(s.valid for s in shotset.shots) and shotset.duplicates == 0
    assert min(np.diff(times)) > shots.DOUBLE_SHOT_S


def test_one_bad_fix_and_jitter_are_outvoted(tmp_path):
    """Arrival times off by up to 2 ms, and one off by 80 ms: the medians of the neighbouring fixes keep every shot
    within 2.5 ms."""
    log, expected = flight(30, sms_jitter_ms=2.0, seed=5)
    gpa = [i for i, r in enumerate(log.records) if r[0] == "GPA"]
    k = gpa[len(gpa) // 2]
    name, (type_id, fmt, values) = log.records[k]
    values = list(values)
    values[8] += 80  # SMS of the GPA layout of 4.x
    log.records[k] = (name, (type_id, fmt, tuple(values)))
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    _check_shots(shotset, expected, tol=0.0025)


def test_clock_drift_over_a_long_flight(tmp_path):
    """Two hours at 50 ppm (0.36 s of drift in all): the local mapping follows it."""
    log, expected = flight(120, interval_ms=60_000, gps_hz=5, drift_ppm=50.0)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    _check_shots(shotset, expected)


def test_gps_outage(tmp_path):
    """No fixes for 30 s: the shots in it keep a time interpolated across the gap (valid, no status); a gap longer
    than MAX_GAP_S leaves its shots without a time (not valid)."""
    log, expected = flight(40, interval_ms=2000, outage=(20.0, 50.0), drift_ppm=50.0)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    _check_shots(shotset, expected)
    inside = [s for s, e in zip(shotset.shots, expected) if e["status"] is None]
    assert len(inside) >= 10 and all(s.valid and s.status is None for s in inside)
    assert any("no status" in w for w in shotset.warnings)
    long = autopilot.MAX_GAP_S + 30.0
    log, expected = flight(30, interval_ms=10_000, outage=(20.0, 20.0 + long), gps_hz=1)
    shotset = autopilot.ardupilot(_write(tmp_path, log, "long.bin"))
    lost = [s for s in shotset.shots if not s.valid]
    assert lost and all("gap" in s.source["no_gps_time"] for s in lost)
    assert any("without a GPS time (in a gap" in w for w in shotset.warnings)
    for s, e in zip(shotset.shots, expected):
        if s.valid:
            assert abs(s.gps_time - e["true"]) <= TIME_TOL_S


def test_without_gpa_the_times_of_writing_stand_for_the_arrivals(tmp_path):
    log, expected = flight(20, gpa=False)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    assert all(s.source["time_base"] == "gps_timeus" and s.std is None for s in shotset.shots)
    errors = [s.gps_time - e["true"] for s, e in zip(shotset.shots, expected)]
    # early by the processing of the fixes (12-16 ms here), never late
    assert all(-(dfw.PROCESS_US + 4000) * 1e-6 - TIME_TOL_S <= x <= -dfw.PROCESS_US * 1e-6 + TIME_TOL_S
               for x in errors)
    assert any("no GPA with SMS" in w for w in shotset.warnings)
    # no fixes at all: the GPSTime of the messages, said
    log = LogWriter()
    log.fmt("CAM", *dfw.CAM_FORMAT)
    log.add("CAM", 5_000_000, 0, 1, dfw.TOW0_MS, dfw.WEEK, 437712345, 112543210, 15000, 0, 0, 0, 0, 0)
    shotset = autopilot.ardupilot(_write(tmp_path, log, "bare.bin"))
    assert shotset.shots[0].source["time_base"] == "cam_gpstime"
    assert shotset.shots[0].gps_time == dfw.WEEK * 604800 + dfw.TOW0_MS / 1000.0
    assert any("early by up to a GPS period" in w for w in shotset.warnings)


def test_receiver_latency_stays_a_constant(tmp_path):
    """The latency of the receiver is not in the log: every time is early by it (what latency_s is for)."""
    log, expected = flight(20, latency_ms=40.0)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    errors = [s.gps_time - e["true"] for s, e in zip(shotset.shots, expected)]
    assert errors == pytest.approx([-0.040] * len(errors), abs=TIME_TOL_S)
    assert all(s.valid for s in shotset.shots)


@pytest.mark.parametrize("cam_layout, gps_layout, gpa_layout", [
    ("master", "4.x", "4.x"), ("3.x", "3.x", "3.x"), ("4.x", "no_u", "3.x")])
def test_layouts_of_other_versions(tmp_path, cam_layout, gps_layout, gpa_layout):
    """master's CAM (attitude in float degrees), the CAM before 4.4 (no I or Img, Roll/Pitch/Yaw), GPS without U and
    the GPA without I."""
    log, expected = flight(12, cam_layout=cam_layout, gps_layout=gps_layout, gpa_layout=gpa_layout)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    _check_shots(shotset, expected)
    text = autopilot.ardupilot(_write(tmp_path, log, "f.log", text=True))
    _check_shots(text, expected)


def test_gps_week_rolls_over_during_the_flight(tmp_path):
    log, expected = flight(30, tow0_ms=604_800_000 - 30_000)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    _check_shots(shotset, expected)
    weeks = {s.source["gps_week"] for s in shotset.shots}
    assert weeks == {dfw.WEEK, dfw.WEEK + 1}


def test_gps_status_static_and_ppp():
    assert autopilot.GPS_STATUS[7] == "single" and autopilot.GPS_STATUS[8] == "float"
    assert [autopilot.GPS_STATUS[k] for k in range(7)] == ["none", "none", "single", "single", "single", "float",
                                                           "fix"]


def test_ppp_status_from_the_log(tmp_path):
    log, _ = flight(6)
    for i, (name, record) in enumerate(log.records):
        if name == "GPS":
            type_id, fmt, values = record
            values = list(values)
            values[2] = 8
            log.records[i] = (name, (type_id, fmt, tuple(values)))
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    assert {s.status for s in shotset.shots} == {"float"} and {s.source["gps_status"] for s in shotset.shots} == {8}


def test_cam_defined_with_and_without_instance(tmp_path):
    """CAM defined again without I and Img (one log, two firmware layouts): one camera, no warning of numpy."""
    import warnings
    log, expected = flight(6)
    old = dfw.CAM_LAYOUTS["3.x"]
    type_id = log.formats["CAM"][0]
    log.fmt("CAM", *old, type_id=type_id)
    g = expected[-1]
    log.add("CAM", g["time_us"] + 500_000, g["cam_tow_ms"] + 400, g["cam_week"], g["lat"], g["lng"], g["alt"], 0, 0,
            0, 0, 0)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        shotset = autopilot.ardupilot(_write(tmp_path, log))
    assert len(shotset.shots) == 7 and shotset.shots[-1].index is None
    assert {s.source["instance"] for s in shotset.shots} == {0}
    assert [c["instance"] for c in autopilot.cameras(tmp_path / "flight.bin")] == [0]


def test_mission_planner_text_log(tmp_path):
    """A text log as Mission Planner writes it: CRLF line ends, the ids of UNIT and MULT as characters, a MSG with
    commas and characters that are line breaks to Python but not to the log."""
    log, expected = flight(10)
    log.fmt("MSG", "QZ", "TimeUS,Message")
    log.add("MSG", 1, "ArduCopter V4.5.7 (abc), frame QUAD\x0b\u2028\x1c end")
    lines = []
    for line in log.text().rstrip("\n").split("\n"):
        if line.startswith(("UNIT,", "MULT,")):
            parts = line.split(", ")
            parts[2] = chr(int(parts[2]))
            line = ", ".join(parts)
        lines.append(line)
    path = tmp_path / "mp.log"
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode())
    with autopilot.read_dataflash(path) as df:
        assert df.bad_lines == 0 and df.truncated == 0
        assert df.messages("MSG")["Message"][-1] == "ArduCopter V4.5.7 (abc), frame QUAD\x0b\u2028\x1c end"
        assert df.units["CAM"]["GPSTime"] == {"unit": "s", "mult": 1e-3}
    _check_shots(autopilot.ardupilot(path), expected)


def test_time_pairing_round_trip(tmp_path):
    """Photos whose numbers are not the image numbers of the log: paired by their times, each with its own shot."""
    from test_gnss_shots import _photos
    rng = np.random.default_rng(3)
    steps = rng.uniform(1.7, 2.3, 120) + np.where(np.arange(120) % 40 == 39, 9.0, 0.0)  # a turn every 40
    offsets = list(1.0 + np.cumsum(steps))
    log, expected = flight(shot_offsets_s=offsets)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    names = [f"DSC{7000 + i}.JPG" for i in range(120)]
    utcs = [shots.utc(s).replace(tzinfo=None) for s in shotset.shots]
    ch = _photos(tmp_path / "p", names, utcs)
    pairing = shots.pair(ch, shotset, tmp_path / "p")
    assert pairing.by == "time" and len(pairing.kept) == 120
    assert [(f.name, s.index) for f, s, _c in pairing.kept] == [(n, i) for i, n in enumerate(names, 1)]


def test_gps_not_in_use_and_far_gps_ignored(tmp_path):
    log = LogWriter()
    log.fmt("GPS", *dfw.GPS_FORMAT)
    log.fmt("GPA", *dfw.GPA_FORMAT)
    log.fmt("CAM", *dfw.CAM_FORMAT)
    week, tow = dfw.WEEK, dfw.TOW0_MS
    # GPS 1 in use (status 5), GPS 0 not (status 6): the status of the one in use
    log.add("GPS", 10_000_000, 0, 6, tow, week, 20, 70, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0)
    log.add("GPS", 10_000_100, 1, 5, tow, week, 20, 70, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 1)
    log.add("GPA", 10_000_200, 0, 1, 1, 1, 1, 0.0, 1, tow, 0, 0.0)
    log.add("GPA", 10_000_300, 1, 1, 40, 70, 1, 0.0, 1, tow, 0, 0.0)
    log.add("CAM", 10_100_000, 0, 1, tow + 100, week, 1, 1, 100, 0, 0, 0, 0, 0)
    log.add("CAM", 12_000_000, 0, 2, tow + 2000, week, 1, 1, 100, 0, 0, 0, 0, 0)  # 2 s from any GPS
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    a, b = shotset.shots
    assert a.status == "float" and a.std == pytest.approx((0.40, 0.40, 0.70)) and a.source["gps_instance"] == 1
    assert b.status is None and b.std is None
    assert any("1 CAM" in w and "no GPS" in w for w in shotset.warnings)


def test_gps_antenna_offset_said(tmp_path):
    log, _ = flight(4)
    log.fmt("PARM", "QNf", "TimeUS,Name,Value")
    log.add("PARM", 1, "GPS_POS1_X", 0.12)
    log.add("PARM", 1, "GPS_POS1_Z", 0.0)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    assert any("GPS_POS1_X" in w for w in shotset.warnings)


def test_apply_shots_pairs_by_number(tmp_path):
    from test_gnss_shots import _photos
    log, expected = flight(6, fix_until=6)
    path = _write(tmp_path, log)
    shotset = autopilot.ardupilot(path)
    utcs = [shots.utc(s).replace(tzinfo=None) for s in shotset.shots]
    ch = _photos(tmp_path / "photos", [f"IMG_{i:04d}.JPG" for i in range(1, 6)], utcs[:5])
    result = gnss_apply.apply_shots(ch, shotset, tmp_path / "photos")
    assert result["matched"] == 5
    cams = sorted(ch.cameras, key=lambda c: c.label)
    for cam, e in zip(cams, expected):
        gnss = cam.meta["gnss"]
        assert gnss["height"] == "msl" and gnss["status"] == "fix"
        assert cam.reference.location[2] == pytest.approx(e["alt"] * 0.01, abs=1e-6)


def test_big_log_is_read_fast(tmp_path):
    """About 20 MB of messages (a CAM every 2 s among GPS, GPA and a dense IMU-like message) read in seconds."""
    log, _ = flight(10)
    log.fmt("IMU", "QBffffffIIfBBHH", "TimeUS,I,GyrX,GyrY,GyrZ,AccX,AccY,AccZ,EG,EA,T,GH,AH,GHz,AHz")
    body = dfw.LogWriter()
    body.formats = dict(log.formats)
    body.records = []
    for k in range(4000):
        body.add("IMU", k, 0, 0.1, 0.2, 0.3, 0.0, 0.0, -9.8, 0, 0, 25.0, 1, 1, 400, 400)
    block = body.binary()
    path = tmp_path / "big.bin"
    with path.open("wb") as f:
        f.write(log.binary())
        for _ in range(20_000_000 // len(block)):
            f.write(block)
    t0 = time.perf_counter()
    with autopilot.read_dataflash(path) as df:
        n = df.count("IMU")
        assert n > 300_000
        acc = df.messages("IMU")["AccZ"]
        assert len(acc) == n and float(acc[-1]) == pytest.approx(-9.8, abs=1e-5)
    shotset = autopilot.ardupilot(path)
    assert len(shotset.shots) == 10
    assert time.perf_counter() - t0 < 20.0
    path.unlink()


@pytest.mark.skipif(not os.environ.get("PPG_DATAFLASH_SAMPLE"),
                    reason="no real DataFlash log given (PPG_DATAFLASH_SAMPLE): none is in the repository")
def test_real_log_if_given():
    """A real log named by PPG_DATAFLASH_SAMPLE (never in the repository): read whole, every message decoded."""
    path = Path(os.environ["PPG_DATAFLASH_SAMPLE"])
    with autopilot.read_dataflash(path) as df:
        assert "FMT" in df.formats and len(df.formats) > 10
        for name in df.formats:
            df.messages(name)
        assert df.undecoded == 0
    for cam in autopilot.cameras(path):
        assert cam["cam"] + cam["trig"] > 0


# --- clocks of processing times, firmware generations, receivers --------------------------------------------------


def _errors(shotset, expected) -> np.ndarray:
    return np.array([s.gps_time - e["true"] for s, e in zip(shotset.shots, expected)])


@pytest.mark.parametrize("hz", [1, 5, 10])
def test_one_sided_processing_delays(tmp_path, hz):
    """SMS the time the fix was processed, 0-20 ms after its arrival (uniform, as with a CAN receiver), the boot clock
    50 ppm fast: the clock is fitted to the least delayed fixes, the times within -3..+1.5 ms of the truth (the slope
    of two minutes of fixes, the truncation of SMS to the ms) - the mean of the fixes would put them 10 ms early."""
    log, expected = flight(60, gps_hz=hz, drift_ppm=50.0, sms="processing", processing_ms=20.0, seed=hz)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    errors = _errors(shotset, expected)
    assert -0.003 <= errors.min() and errors.max() <= 0.0015, errors
    assert {s.source["time_base"] for s in shotset.shots} == {"gpa_sms_uncorrected"}
    assert any("processed them" in w for w in shotset.warnings)
    assert all(s.valid for s in shotset.shots)


@pytest.mark.parametrize("sms", ["corrected", "processing", "loop"])
def test_clock_thousands_of_ppm_off(tmp_path, sms):
    """A board clock 3000 ppm off GPS time (real: 2778 ppm on a Plane 4.6 board): the slope comes from the data."""
    log, expected = flight(80, drift_ppm=3000.0, sms=sms, seed=2)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    errors = _errors(shotset, expected)
    low = -TIME_TOL_S if sms == "corrected" else -0.0025
    assert low <= errors.min() and errors.max() <= TIME_TOL_S, errors
    assert all(s.valid for s in shotset.shots)


def test_firmware_3x_semantics(tmp_path):
    """ArduPlane 3.6: CAM written at the next 50 Hz loop, 0-20 ms after the flash, fixes processed at a loop tick.
    With a clock 300 ppm off GPS time (a sawtooth of 67 s, five of them in the flight) the clock is right, and every
    time is late by exactly the writing of its CAM."""
    log, expected = flight(40, interval_ms=8000, firmware="3.x", sms="tick", drift_ppm=300.0, seed=4)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    errors = _errors(shotset, expected)
    late = np.array([e["late"] for e in expected])
    assert errors - late == pytest.approx(np.zeros(len(late)), abs=TIME_TOL_S), errors - late
    assert late.max() > 0.015 and late.min() < 0.005
    assert all(e["late"] > 0 for e in expected) and shotset.shots[0].source["firmware"] == "ArduPlane V3.6.0"
    assert any("V3.6.0 writes the camera messages" in w and "20 ms" in w for w in shotset.warnings)
    assert {s.source["time_base"] for s in shotset.shots} == {"gpa_sms_uncorrected"}
    # 4.x says nothing of the sort
    log, _ = flight(5)
    shotset = autopilot.ardupilot(_write(tmp_path, log, "new.bin"))
    assert shotset.shots[0].source["firmware"] == "ArduCopter V4.5.7"
    assert not any("50 Hz loop" in w for w in shotset.warnings)


def test_primary_switch_keeps_every_fix(tmp_path):
    """Receiver 0 in use for 150 s, then receiver 1 (40 ms more latency): every shot keeps a time from ONE receiver's
    clock fitted to all its fixes, not only those in use."""
    log, expected = flight(150, interval_ms=2000, receivers=2, switch_s=150.0, seed=6)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    assert all(s.valid for s in shotset.shots) and len(shotset.shots) == 150
    clocks = {s.source["clock_instance"] for s in shotset.shots}
    assert len(clocks) == 1
    latency = (0.0, 0.040)[clocks.pop()]
    assert _errors(shotset, expected) == pytest.approx([-latency] * 150, abs=TIME_TOL_S)
    # the status of the receiver in use at each shot
    assert {s.source["gps_instance"] for s in shotset.shots[:70]} == {0}
    assert {s.source["gps_instance"] for s in shotset.shots[80:]} == {1}


def test_blended_instance_is_the_last_choice(tmp_path):
    log, expected = flight(30, receivers=2, blended=True, seed=7)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    assert {s.source["clock_instance"] for s in shotset.shots} <= {0, 1}
    assert {s.source["gps_instance"] for s in shotset.shots} == {2}  # the status of the solution in use
    assert all(s.valid for s in shotset.shots)
    latency = (0.0, 0.040)[shotset.shots[0].source["clock_instance"]]
    assert _errors(shotset, expected) == pytest.approx([-latency] * 30, abs=TIME_TOL_S)


def test_other_receiver_where_one_has_no_fixes(tmp_path):
    """Receiver 0 has no fixes for 150 s (more than MAX_GAP_S): its shots take the clock of receiver 1, said."""
    log, expected = flight(150, interval_ms=2000, receivers=2, latencies_ms=(0.0, 0.0), seed=8)
    keep = []
    for name, record in log.records:
        if name in ("GPS", "GPA") and record[2][1] == 0 and 100e6 < record[2][0] - dfw.BOOT0_US < 250e6:
            continue
        keep.append((name, record))
    log.records = keep
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    assert all(s.valid for s in shotset.shots)
    assert {s.source["clock_instance"] for s in shotset.shots} == {0, 1}
    assert any("clocks of receivers 0, 1" in w for w in shotset.warnings)
    _check_shots(shotset, expected)


def test_duplicate_fixes_and_fixes_before_lock(tmp_path):
    """Fixes logged twice (same GMS) are kept once; fixes before the 3D fix carry a GPS time 1 s wrong and are not
    used for the clock."""
    log, expected = flight(30, duplicate_every=3, prelock=15, seed=9)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    _check_shots(shotset, expected)


def test_names_with_a_type_given_to_other_messages(tmp_path):
    """A type kept, given to a message not kept, then kept again: each read as its definition says."""
    log = LogWriter()
    log.fmt("AAA", "QH", "TimeUS,A", type_id=200)
    log.add("AAA", 1, 10)
    log.fmt("BBB", "Qi", "TimeUS,B", type_id=200)
    log.add("BBB", 2, -5)
    log.add("BBB", 3, -6)
    log.fmt("AAA", "QH", "TimeUS,A", type_id=200)
    log.add("AAA", 4, 11)
    for name, text in (("n.bin", False), ("n.log", True)):
        with autopilot.read_dataflash(_write(tmp_path, log, name, text=text), names=("AAA",)) as df:
            assert list(df.messages("AAA", multipliers=False)["A"]) == [10, 11]
            assert df.count("BBB") == 2
            with pytest.raises(KeyError, match="not kept"):
                df.messages("BBB")


def test_format_longer_than_its_fields(tmp_path):
    """BAR3 of Plane 3.6 declares 33 bytes for 26 of fields: read, the rest padding."""
    import struct
    log = LogWriter()
    log.fmt("XYZ", "QH", "TimeUS,A", type_id=150)
    data = bytearray(log.binary())
    data += b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", 151, 3 + 8 + 2 + 7, b"PAD", b"QH", b"TimeUS,A")
    data += b"\xa3\x95\x97" + struct.pack("<QH", 5, 42) + b"\x00" * 7
    data += b"\xa3\x95\x96" + struct.pack("<QH", 6, 43)
    path = tmp_path / "pad.bin"
    path.write_bytes(bytes(data))
    with autopilot.read_dataflash(path) as df:
        assert list(df.messages("PAD")["A"]) == [42] and list(df.messages("XYZ")["A"]) == [43]
        assert df.messages("PAD").dtype.names == ("TimeUS", "A") and not df.bad_formats


def test_old_mission_planner_text_logs(tmp_path):
    """A terminal download of an APM2 log: a preamble before the first FMT, a format without fields, flight modes by
    name, CAM and GPS without TimeUS. Read without damage; too old for shot times."""
    lines = ["", "1", "ArduCopter V3.1 (1d1b3fb2)", "Free RAM: 1044", "APM 2",
             "FMT, 128, 89, FMT, BBnNZ, Type,Length,Name,Format,Columns",
             "FMT, 10, 3, STRT, , ",
             "FMT, 130, 33, GPS, BIHBcLLeeEefI, Status,TimeMS,Week,NSats,HDop,Lat,Lng,RelAlt,Alt,Spd,GCrs,VZ,T",
             "FMT, 135, 29, CAM, IHLLeccC, GPSTime,GPSWeek,Lat,Lng,Alt,Roll,Pitch,Yaw",
             "FMT, 141, 7, MODE, Mh, Mode,ThrCrs",
             "STRT", "MODE, STABILIZE, 0",
             "GPS, 3, 645078570, 1752, 9, 1.61, 43.7712345, 11.2543210, 0.00, 150.00, 0.00, 0.00, 0.00, 10",
             "MODE, LOITER, 400", "MODE, 5, 400",
             "CAM, 645078570, 1752, 43.7712345, 11.2543210, 150.00, 1.00, 2.00, 359.00"]
    path = tmp_path / "apm2.log"
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode())
    with autopilot.read_dataflash(path) as df:
        assert df.kind == "text" and df.preamble == 4 and df.bad_lines == 0 and not df.bad_formats
        assert df.warnings == []
        assert list(df.messages("MODE")["Mode"]) == [-1, -1, 5]
        assert df.count("STRT") == 1 and df.count("CAM") == 1
    with pytest.raises(ValueError, match="too old"):
        autopilot.ardupilot(path)


def test_failing_units_close_the_log(tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.gnss import dataflash
    log, _ = flight(3)
    path = _write(tmp_path, log)
    closed = []
    monkeypatch.setattr(dataflash.DataFlash, "_resolve_units", lambda self: 1 / 0)
    original = dataflash.DataFlash.close
    monkeypatch.setattr(dataflash.DataFlash, "close", lambda self: (closed.append(self._mmap is not None),
                                                                   original(self)))
    with pytest.raises(ZeroDivisionError):
        autopilot.read_dataflash(path)
    assert closed == [True]


# --- the clock: glitches, bad fixes of sparse logs, the sawtooth of the processing delays ---------------------------


def _gps_records(log: LogWriter) -> list[int]:
    return [i for i, r in enumerate(log.records) if r[0] == "GPS"]


def _shift_gms(log: LogWriter, k: int, ms: int) -> None:
    """The GPS time of the k-th record of ``log`` (a GPS message of the 4.x layout) moved by ``ms``."""
    name, (type_id, fmt, values) = log.records[k]
    values = list(values)
    values[3] += ms
    log.records[k] = (name, (type_id, fmt, tuple(values)))


@pytest.mark.parametrize("hz", [1, 5])
@pytest.mark.parametrize("glitch_ms", [250, 1000])
@pytest.mark.parametrize("sms", ["corrected", "tick"])
def test_glitch_of_one_fix_left_out(tmp_path, hz, glitch_ms, sms):
    """One fix whose GPS time is off by 0.25 s or 1 s (more than a step, but for one fix only) is left out, said:
    every shot valid and on time (the same tolerances as without the glitch)."""
    log, expected = flight(80, gps_hz=hz, interval_ms=1777, sms=sms, drift_ppm=200.0, seed=1)
    rows = _gps_records(log)
    _shift_gms(log, rows[len(rows) // 2], glitch_ms)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    assert all(s.valid for s in shotset.shots)
    errors = _errors(shotset, expected)
    low = -TIME_TOL_S if sms == "corrected" else -0.003
    assert low <= errors.min() and errors.max() <= TIME_TOL_S, errors
    assert any("1 GPS fix with a time off" in w and "left out of the clock" in w for w in shotset.warnings)


def test_persistent_step_still_splits_the_clock(tmp_path):
    """A step of the receiver's time that holds (every fix after it 0.5 s later) is a new stretch of the clock, not a
    glitch: nothing left out."""
    log, _ = flight(40, gps_hz=5, seed=2)
    rows = _gps_records(log)
    for k in rows[len(rows) // 2:]:
        _shift_gms(log, k, 500)
    with autopilot.read_dataflash(_write(tmp_path, log)) as df:
        gps = autopilot._gps(df)
        clock = autopilot._clocks(gps, autopilot._sms(df, gps))[0]
    assert clock["glitches"] == 0 and int(clock["seg"][-1]) == 1 and len(clock["models"]) == 2


@pytest.mark.parametrize("step_ms", [0, 500])
def test_log_ending_in_a_jump_of_time(tmp_path, step_ms):
    """The log ends 1 s after the GPS time of its fixes jumped by 0.5 s (6 fixes: too few to be a step that holds,
    too late to be seen as a glitch among fixes that hold): a shot after the jump has no GPS time - a step would make
    it 0.5 s wrong - and is not valid, said; the shots before are timed as ever."""
    log, expected = flight(shot_offsets_s=[50.0, 99.0, 100.0], gps_hz=5, seed=5)
    gps = {r[1][2][0]: i for i, r in enumerate(log.records) if r[0] == "GPS"}
    rel = {t: (log.records[i][1][2][3] - dfw.TOW0_MS) / 1000.0 for t, i in gps.items()}
    cut = {t for t, x in rel.items() if x > 100.65}
    for t, i in gps.items():
        if 99.55 < rel[t] <= 100.65 and step_ms:
            _shift_gms(log, i, step_ms)
    log.records = [r for r in log.records if not (r[0] in ("GPS", "GPA") and r[1][2][0] in cut)]
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    errors = _errors(shotset, expected)
    assert np.abs(errors[:2]).max() <= TIME_TOL_S, errors
    if not step_ms:
        assert all(s.valid for s in shotset.shots)
        return
    assert [s.valid for s in shotset.shots] == [True, True, False]
    assert "jumps by +0.500 s in the last 6 fixes" in shotset.shots[2].source["no_gps_time"]
    assert any("1 CAM shot without a GPS time (the GPS time jumps" in w for w in shotset.warnings), shotset.warnings
    assert any("6 GPS fixes with a time off" in w for w in shotset.warnings)


@pytest.mark.parametrize("every, bad, off_ms", [(48, 3, 50), (48, 5, 150), (150, 1, 50)])
def test_bad_fixes_of_sparse_logs(tmp_path, every, bad, off_ms):
    """Fixes logged once every 9.6 s (3 of them 50 ms early, or 5 of them 150 ms) or once every 30 s (1 of them):
    the envelope leaves them out, the shots within 3.5 ms (a deterministic sawtooth, 200 ppm)."""
    log, expected = flight(225, gps_hz=5, interval_ms=1777, sms="tick", drift_ppm=200.0, log_every=every, seed=2)
    rows = _gps_records(log)
    for j in range(bad):
        _shift_gms(log, rows[len(rows) // 2 + j], off_ms)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    errors = _errors(shotset, expected)
    assert -0.0035 <= errors.min() and errors.max() <= 0.002, errors
    assert all(s.valid for s in shotset.shots)


@pytest.mark.parametrize("bad", [3, 4])
def test_bad_fixes_of_sparse_logs_within_the_spread(tmp_path, bad):
    """Fixes logged once every 9.6 s, some 30 ms early (under ENVELOPE_SPREAD_S): the level lies under at least 3
    fixes, so 3 of them move the shots by less than 4 ms; 4 of them make the level, the shots late by some 12 ms -
    the limit of a sparse log - and the share of fixes above the clock says so."""
    log, expected = flight(225, gps_hz=5, interval_ms=1777, sms="tick", drift_ppm=200.0, log_every=48, seed=2)
    rows = _gps_records(log)
    for j in range(bad):
        _shift_gms(log, rows[len(rows) // 2 + j], 30)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    errors = _errors(shotset, expected)
    assert -0.003 <= errors.min() and errors.max() <= (0.004 if bad == 3 else 0.013), errors
    assert any("above the clock" in w for w in shotset.warnings)


@pytest.mark.parametrize("hz, ppm, bad, off_ms, spacing", [
    (5, 300, 3, 12, 1), (5, 300, 3, 24, 1), (5, 300, 3, 36, 1), (1, 300, 3, 30, 1), (5, 50, 3, 30, 1),
    (5, 300, 3, 30, 200), (1, 300, 3, 30, 40), (1, 300, 6, 30, 40)])
def test_bad_fixes_within_the_spread_are_not_a_branch(tmp_path, hz, ppm, bad, off_ms, spacing):
    """A few fixes 12-36 ms above the others (under ENVELOPE_SPREAD_S, so not left out as bad fixes) in a dense log,
    together or 40 s apart: not a branch of the sawtooth one loop period above (too few, not spread, and the delays
    under the level fill the loop period) - the shots as in a clean log (the rule of 3 fixes put 20 of these 60 shots
    10-22 ms late)."""
    log, expected = flight(60, gps_hz=hz, interval_ms=30_000, sms="tick", drift_ppm=float(ppm), seed=3)
    rows = _gps_records(log)
    for j in range(bad):
        _shift_gms(log, rows[len(rows) // 2 + j * spacing], off_ms)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    errors = _errors(shotset, expected)
    assert np.abs(errors).max() <= TIME_TOL_S, errors
    assert all(s.valid for s in shotset.shots)


def test_envelope_takes_a_real_branch():
    """Delays in clusters one loop period apart (as on a real 4.0.9 log, processing times of a CAN receiver): 2 % of
    the fixes, spread over the window, caught the tick 20 ms above the main cluster, with an empty band between - the
    level is that branch, not the 2 % quantile of the lower one; the same fixes bunched in 20 s are bad fixes."""
    rng = np.random.default_rng(4)
    t = np.sort(rng.uniform(0.0, 600.0, 600))
    r = np.where(rng.uniform(size=600) < 0.45, -0.040, -0.020) + rng.uniform(-0.004, 0.004, 600)
    top = rng.choice(600, 12, replace=False)
    r[top] = rng.uniform(-0.001, 0.003, 12)
    assert abs(autopilot._envelope_level(r, t)) <= 0.003
    bunched = t.copy()
    bunched[top] = np.linspace(300.0, 320.0, 12)
    assert autopilot._envelope_level(r, bunched) <= -0.015
    # the same branch above a sawtooth that fills the loop period: bad fixes, not a branch
    r2 = rng.uniform(-0.040, -0.020, 600)
    r2[top] = r[top]
    assert autopilot._envelope_level(r2, t) <= -0.019


@pytest.mark.parametrize("ppm", [5, 20, 50, 100, 150])
def test_deterministic_sawtooth(tmp_path, ppm):
    """Fixes processed exactly at the ticks of a 50 Hz loop, 30 minutes at 5 Hz: 50 ppm or more from GPS time every
    window holds fixes that caught the tick and the times are within 1.5 ms; nearer to it the clock may sit on a
    lower branch, the times early by up to 20 ms (never late), and the warning says so."""
    log, expected = flight(60, gps_hz=5, interval_ms=30_000, sms="tick", drift_ppm=float(ppm), seed=3)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    errors = _errors(shotset, expected)
    if ppm >= 50:
        assert np.abs(errors).max() <= TIME_TOL_S, errors
    else:
        assert -0.0205 <= errors.min() and errors.max() <= TIME_TOL_S, errors
    assert any("early by up to 20 ms" in w and "varies along the flight" in w for w in shotset.warnings)
    assert all(s.source["sms_lag_ms"] < 2 for s in shotset.shots)
    assert all(abs(s.source["clock_ppm"] + ppm) <= 6 for s in shotset.shots)  # SMS in whole ms: a few ppm off
    slow = [w for w in shotset.warnings if "ppm from GPS time (under 35 ppm" in w]
    if ppm < 35:
        assert slow and slow[0].startswith("60 shots") and "fixes every 200 ms" in slow[0], shotset.warnings
        assert "early by up to 20 ms" in slow[0]
    else:
        assert not slow


@pytest.mark.parametrize("hz, early_ms", [(4, 10), (8, 5), (3, 0)])
def test_slow_clock_grain_of_the_fix_period(tmp_path, hz, early_ms):
    """A clock 2 ppm from GPS time with fixes whose period is not a multiple of the loop: they step through it, the
    times early by up to the greatest common divisor of the two (250 ms: 10 ms, 125 ms: 5 ms); 333 ms samples every
    delay - no warning."""
    log, expected = flight(60, gps_hz=hz, interval_ms=30_000, sms="tick", drift_ppm=2.0, seed=3)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    errors = _errors(shotset, expected)
    assert -(early_ms / 1000.0) - TIME_TOL_S <= errors.min() and errors.max() <= TIME_TOL_S, errors
    slow = [w for w in shotset.warnings if "under 35 ppm" in w]
    if early_ms:
        assert slow and f"early by up to {early_ms} ms" in slow[0], shotset.warnings
    else:
        assert not slow


@pytest.mark.parametrize("sms", ["corrected", "tick"])
def test_shots_at_the_ends_of_the_log(tmp_path, sms):
    """Shots 50 ms after the first fix and at the last one: the window of the clock is shifted, not cut."""
    log, expected = flight(shot_offsets_s=[0.05, 0.4, 50.0, 100.0, 149.9, 150.5], sms=sms, drift_ppm=200.0, seed=5)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    errors = _errors(shotset, expected)
    low = -TIME_TOL_S if sms == "corrected" else -0.003
    assert low <= errors.min() and errors.max() <= TIME_TOL_S, errors
    assert all(s.valid for s in shotset.shots)


def test_corrected_times_lag_kept(tmp_path):
    log, _ = flight(5)
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    assert all(s.source["sms_lag_ms"] >= 12 and s.source["time_base"] == "gpa_sms" for s in shotset.shots)
    assert all(s.source["fixes_above"] == 0.0 for s in shotset.shots)


def test_too_old_without_camera_messages(tmp_path):
    lines = ["FMT, 128, 89, FMT, BBnNZ, Type,Length,Name,Format,Columns",
             "FMT, 130, 33, GPS, BIHBcLLeeEefI, Status,TimeMS,Week,NSats,HDop,Lat,Lng,RelAlt,Alt,Spd,GCrs,VZ,T",
             "FMT, 135, 29, CAM, IHLLeccC, GPSTime,GPSWeek,Lat,Lng,Alt,Roll,Pitch,Yaw"]
    path = tmp_path / "old.log"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="no camera messages.*too old.*CAM, GPS"):
        autopilot.ardupilot(path)
