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


def _check_shots(shotset, expected) -> None:
    assert [s.index for s in shotset.shots] == [e[1] for e in expected]
    for shot, (_inst, _img, week, tow_ms, time_us, lat, lng, alt, roll, pitch, yaw, status, hacc, vacc) in zip(
            shotset.shots, expected):
        assert shot.gps_time == week * 604800 + tow_ms / 1000.0
        assert shot.source["gps_week"] == week and shot.source["tow"] == tow_ms / 1000.0
        assert shot.source["time_us"] == time_us
        assert shot.antenna == pytest.approx((lat * 1e-7, lng * 1e-7, alt * 0.01), abs=1e-9)
        assert shot.height == "msl"
        assert shot.attitude == pytest.approx((roll * 0.01, pitch * 0.01, yaw * 0.01), abs=1e-9)
        assert shot.status == {6: "fix", 5: "float"}[status]
        assert shot.std == pytest.approx((hacc * 0.01, hacc * 0.01, vacc * 0.01))
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
    assert info == [{"instance": 0, "cam": 20, "trig": 0, "no_fix": 3, "first": expected[3][2] * 604800 +
                     expected[3][3] / 1000.0, "last": expected[-1][2] * 604800 + expected[-1][3] / 1000.0,
                     "first_img": 1, "last_img": 20}]


def test_two_cameras_need_a_choice(tmp_path):
    log, expected = flight(10, instances=(0, 1))
    path = _write(tmp_path, log)
    with pytest.raises(ValueError, match=r"2 cameras.*0, 1"):
        autopilot.ardupilot(path)
    second = autopilot.ardupilot(path, camera=1)
    _check_shots(second, [e for e in expected if e[0] == 1])
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


def test_gps_time_against_boot_clock(tmp_path):
    """A CAM whose GPS time disagrees with the boot clock (a stale or wrong week) is said; status from the GPS in
    use only."""
    log, expected = flight(10)
    rows = [r for r in log.records if r[0] == "CAM"]
    name, (type_id, fmt, values) = rows[4]
    values = list(values)
    values[4] = values[4] - 1  # one week early
    log.records[log.records.index(rows[4])] = (name, (type_id, fmt, tuple(values)))
    shotset = autopilot.ardupilot(_write(tmp_path, log))
    assert any("boot clock" in w and "1 " in w for w in shotset.warnings)
    bad = [s for s in shotset.shots if s.source["img"] == 5][0]
    assert bad.source["clock_disagrees_s"] == pytest.approx(-604800.0, abs=1.0)


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
    from datetime import timedelta
    log, expected = flight(6, fix_until=6)
    path = _write(tmp_path, log)
    shotset = autopilot.ardupilot(path)
    utcs = [shots.utc(s).replace(tzinfo=None) - timedelta(seconds=0) for s in shotset.shots]
    ch = _photos(tmp_path / "photos", [f"IMG_{i:04d}.JPG" for i in range(1, 6)], utcs[:5])
    result = gnss_apply.apply_shots(ch, shotset, tmp_path / "photos")
    assert result["matched"] == 5
    cams = sorted(ch.cameras, key=lambda c: c.label)
    for cam, e in zip(cams, expected):
        gnss = cam.meta["gnss"]
        assert gnss["height"] == "msl" and gnss["status"] == "fix"
        assert cam.reference.location[2] == pytest.approx(e[7] * 0.01, abs=1e-6)


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


@pytest.mark.skipif(not os.environ.get("PPG_DATAFLASH_SAMPLE"), reason="a real DataFlash log only in the workflow")
def test_real_log_if_given():
    """A real log (the workflow downloads one; never in the repository): read whole, every message decoded."""
    path = Path(os.environ["PPG_DATAFLASH_SAMPLE"])
    with autopilot.read_dataflash(path) as df:
        assert "FMT" in df.formats and len(df.formats) > 10
        for name in df.formats:
            df.messages(name)
        assert df.undecoded == 0
    for cam in autopilot.cameras(path):
        assert cam["cam"] + cam["trig"] > 0
