"""PX4 logs (ULog): the reader against pyulog's counts, and the camera shots of logs written by tests/gnss_ulog.py."""
from __future__ import annotations

import contextlib
import re
import signal
import struct
from pathlib import Path

import numpy as np
import pytest

from pyarchinit_photogrammetry.gnss import px4, ulog
import gnss_ulog
from gnss_ulog import flight

DATA = Path(__file__).parent / "data" / "gnss" / "phase4"
TIME_TOL_S = 0.0015  # receivers that stamp their messages (median clock)
FIX_TOL_S = 0.0025  # fix epochs and processing times up to 30 ms late (envelope), logs of 40-80 s: the rate of the
# clock is a line through one-sided delays, a little off on so short a log


def _write(tmp_path: Path, data: bytes, name: str = "flight.ulg") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _errors(shotset, expected) -> np.ndarray:
    true = {e["seq"]: e["true"] for e in expected}
    return np.array([s.gps_time - true[s.index] for s in shotset.shots if s.valid])


def test_sample_ulog_counts():
    """pyulog's sample of appended data and two instances of a topic: every topic, instance and count as pyulog's
    table gives them, the size of each message (without the trailing padding a data message leaves out) too."""
    expected = {}
    for line in (DATA / "sample_appended_multiple_info.txt").read_text().splitlines():
        m = re.match(r"^ (\S+) \((\d+), (\d+)\)\s+(\d+)\s+(\d+)", line)
        if m:
            expected[(m.group(1), int(m.group(2)))] = (int(m.group(3)), int(m.group(4)))
    log = ulog.read_ulog(DATA / "sample_appended_multiple.ulg")
    assert log.appended and log.damaged == 0 and log.dropouts == 0
    assert {k for k, n in log.counts.items() if n} == set(expected)
    for key, (size, count) in expected.items():
        arr = log.data[key]
        assert len(arr) == count == log.counts[key], key
        trailing = 0
        for name in reversed(arr.dtype.names):
            if not name.startswith("_padding"):
                break
            trailing += arr.dtype.fields[name][0].itemsize
        assert arr.dtype.itemsize - trailing == size, key
    assert log.instances("actuator_outputs") == [0, 1]
    q = log.get("vehicle_attitude")["q"]
    assert np.allclose(np.linalg.norm(q, axis=1), 1.0, atol=1e-5)
    assert log.info["ver_hw"] == "PX4FMU_V4PRO" and log.info["sys_name"] == "PX4"


def test_camera_capture_shots(tmp_path):
    """40 captures, 2 with result 0: 38 shots numbered by seq, on time (the fix epochs of the GPS messages, their
    processing up to 30 ms late: the envelope), the AMSL altitude put on the ellipsoid with the receiver's separation,
    status and accuracy of the nearest GPS message, attitude from q."""
    w, expected = flight(40, failed=(5, 17))
    shotset = px4.px4(_write(tmp_path, w.binary()))
    assert shotset.kind == "px4_capture" and len(shotset.shots) == 38
    assert [s.index for s in shotset.shots] == [n for n in range(40) if n not in (5, 17)]
    errors = _errors(shotset, expected)
    assert np.abs(errors).max() <= FIX_TOL_S, errors
    assert any("2 camera_capture messages with a result other than 1" in w for w in shotset.warnings)
    by = {e["seq"]: e for e in expected}
    for s in shotset.shots:
        e = by[s.index]
        assert s.source["time_base"] == "gps_fix" and s.height == "ellipsoidal"
        assert s.antenna == pytest.approx((e["lat"], e["lon"], e["msl"] + gnss_ulog.SEPARATION), abs=1e-4)
        assert s.source["geoid_sep"] == pytest.approx(gnss_ulog.SEPARATION, abs=1e-3)
        assert s.status == "fix" and s.std == pytest.approx((0.02, 0.02, 0.03))
        assert s.attitude == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)
        assert abs(s.source["utc_minus_clock_ms"]) < 2 and s.source["feedback"] is True
    assert any("latency of the receiver" in w for w in shotset.warnings)
    assert not any("commands to shoot" in w for w in shotset.warnings)


@pytest.mark.parametrize("drift_ppm", [0.0, 3000.0])
def test_receiver_stamped_utc(tmp_path, drift_ppm):
    """A receiver that stamps its messages with its own UTC, off the grid of the fixes (DroneCAN): the clock is their
    median, time base gps_stamp; a boot clock 3000 ppm fast too."""
    w, expected = flight(30, gps="stamp", drift_ppm=drift_ppm)
    shotset = px4.px4(_write(tmp_path, w.binary()))
    assert {s.source["time_base"] for s in shotset.shots} == {"gps_stamp"}
    errors = _errors(shotset, expected)
    assert np.abs(errors).max() <= TIME_TOL_S, errors
    rate = -drift_ppm / (1.0 + drift_ppm * 1e-6)  # GPS time less boot time, against boot time
    assert all(abs(s.source["clock_ppm"] - rate) < 2 for s in shotset.shots)


def test_trigger_when_no_capture(tmp_path):
    """No camera_capture: the triggers, positioned from the estimate of the autopilot interpolated at their times;
    without feedback they are the commands to shoot, said."""
    w, expected = flight(20, captures=False, feedback=False)
    shotset = px4.px4(_write(tmp_path, w.binary()))
    assert shotset.kind == "px4_trigger" and len(shotset.shots) == 20
    assert np.abs(_errors(shotset, expected)).max() <= FIX_TOL_S
    by = {e["seq"]: e for e in expected}
    for s in shotset.shots:
        e = by[s.index]
        assert s.antenna == pytest.approx((e["lat"], e["lon"], e["msl"] + gnss_ulog.SEPARATION), abs=1e-6)
    assert any("commands to shoot, not the shots" in w for w in shotset.warnings)
    assert any("vehicle_global_position" in w for w in shotset.warnings)
    with pytest.raises(ValueError, match="no camera_capture messages"):
        px4.px4(tmp_path / "flight.ulg", topic="capture")


def test_captures_of_commands_said(tmp_path):
    w, _ = flight(10, feedback=False)
    shotset = px4.px4(_write(tmp_path, w.binary()))
    assert any("copy triggers without feedback" in w for w in shotset.warnings)
    assert all(s.source["feedback"] is False for s in shotset.shots)


def test_utc_checked_against_gps(tmp_path):
    """timestamp_utc 120 ms off (the real-time clock of the autopilot): the times come from the GPS clock, the
    difference kept and warned of."""
    w, expected = flight(20, utc_error_ms=120.0)
    shotset = px4.px4(_write(tmp_path, w.binary()))
    assert np.abs(_errors(shotset, expected)).max() <= FIX_TOL_S
    assert all(s.source["utc_minus_clock_ms"] == pytest.approx(120.0, abs=2.0) for s in shotset.shots)
    assert any(re.search(r"\+12[01] ms from the clock fitted", w) for w in shotset.warnings), shotset.warnings


def test_timestamp_utc_already_gps_detected(tmp_path):
    w, expected = flight(20, utc_is_gps=True)
    shotset = px4.px4(_write(tmp_path, w.binary()))
    assert np.abs(_errors(shotset, expected)).max() <= FIX_TOL_S
    assert any("it was GPS time already, not UTC" in w for w in shotset.warnings), shotset.warnings


def test_without_gps_times_timestamp_utc(tmp_path):
    """GPS messages without a UTC time: the shot times are timestamp_utc and the leap seconds, said."""
    w, expected = flight(10, gps_utc=False, utc_error_ms=0.0)
    shotset = px4.px4(_write(tmp_path, w.binary()))
    assert {s.source["time_base"] for s in shotset.shots} == {"timestamp_utc"}
    assert np.abs(_errors(shotset, expected)).max() <= 1e-5
    assert any("no GPS messages with a time" in w for w in shotset.warnings)


@pytest.mark.parametrize("separation", [None, 0.0])
def test_msl_to_ellipsoid(tmp_path, separation):
    """A receiver that gives no ellipsoidal height, or the same as the AMSL one (no geoid): the heights stay AMSL,
    marked msl, said; never taken for ellipsoidal."""
    w, expected = flight(10, separation=separation, layout="1.7" if separation is None else "1.15")
    shotset = px4.px4(_write(tmp_path, w.binary()))
    by = {e["seq"]: e for e in expected}
    for s in shotset.shots:
        assert s.height == "msl" and s.antenna[2] == pytest.approx(by[s.index]["msl"], abs=1e-3)
    assert any("marked 'msl'" in w for w in shotset.warnings)


def test_layout_of_px4_1_7(tmp_path):
    """The GPS messages of PX4 1.7 (lat, lon in 1e-7 degrees, alt and alt_ellipsoid in mm)."""
    w, expected = flight(10, layout="1.7")
    shotset = px4.px4(_write(tmp_path, w.binary()))
    assert np.abs(_errors(shotset, expected)).max() <= FIX_TOL_S
    assert {s.height for s in shotset.shots} == {"ellipsoidal"}


def test_appended_data(tmp_path):
    """The camera messages after shot 20 appended, the log before them cut in the middle of a message: every shot
    read, none damaged."""
    w, expected = flight(40, appended_from=20)
    path = _write(tmp_path, w.binary(cut=5))
    log = ulog.read_ulog(path)
    assert log.appended and log.damaged == 0
    shotset = px4.px4(path)
    assert [s.index for s in shotset.shots] == list(range(40))
    assert np.abs(_errors(shotset, expected)).max() <= FIX_TOL_S


def test_damaged_bytes_skipped_to_the_sync(tmp_path):
    w, expected = flight(10)
    for _ in range(3):
        w.sync()
    path = _write(tmp_path, w.binary(garbage=b"\x07\xff\xfe" * 7))
    log = ulog.read_ulog(path)
    assert log.damaged == 21 and len(log.get("camera_capture")) == 10
    assert any("21 damaged bytes" in w for w in px4.px4(path).warnings)


def test_no_camera_says_why(tmp_path):
    w = ulog_writer_without_camera()
    path = _write(tmp_path, w.binary())
    with pytest.raises(ValueError, match="left out"):
        px4.px4(path)


def ulog_writer_without_camera():
    w = gnss_ulog.ULogWriter()
    w.multi_info("excluded_optional_topics", ["['camera_capture', ", "'camera_trigger']"])
    w.fmt("camera_trigger", gnss_ulog.TRIGGER_FORMAT)
    return w


def test_not_a_ulog(tmp_path):
    with pytest.raises(ValueError, match="not a ULog"):
        ulog.read_ulog(_write(tmp_path, b"notalog" * 10, "x.ulg"))


@contextlib.contextmanager
def _within(seconds: float):
    """A read that does not end in ``seconds`` fails the test instead of hanging it (where SIGALRM exists)."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def stop(*_args):
        raise TimeoutError(f"still reading after {seconds} s")

    old = signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


_T_FORMAT = gnss_ulog.message("F", b"t:uint64_t timestamp;double v;")
_T_SUBSCRIBE = gnss_ulog.message("A", struct.pack("<BH", 0, 0) + b"t")


def _t(t: int) -> bytes:
    return gnss_ulog.message("D", struct.pack("<HQd", 0, t, t * 0.5))


def _read(tmp_path: Path, raw: bytes, **options):
    with _within(10.0):
        return ulog.read_ulog(_write(tmp_path, raw, "crafted.ulg"), **options)


@pytest.mark.parametrize("where", ["at_itself", "into_header", "past_the_end"])
def test_appended_offset_not_ahead_ignored(tmp_path, where):
    """An offset of appended data at or before the flag bits that give it, or past the end of the file, is no
    appended data: the log read once, in order (it looped for ever)."""
    body = _T_FORMAT + _T_SUBSCRIBE + _t(1) + _t(2)
    offset = {"at_itself": 16, "into_header": 3, "past_the_end": 10_000}[where]
    log = _read(tmp_path, gnss_ulog.header() + gnss_ulog.flag_bits(offset) + body)
    assert log.get("t")["timestamp"].tolist() == [1, 2]
    assert not log.appended and log.damaged == 0
    assert any("appended data" in w for w in log.warnings), log.warnings


def test_flag_bits_only_first(tmp_path):
    """Flag bits after other messages (the specification puts them first, right after the header) are ignored, an
    offset back to the start of the file with them."""
    raw = gnss_ulog.header() + _T_FORMAT + _T_SUBSCRIBE + _t(1) + gnss_ulog.flag_bits(16) + _t(2)
    log = _read(tmp_path, raw)
    assert log.get("t")["timestamp"].tolist() == [1, 2] and not log.appended


@pytest.mark.parametrize("blocks", [2, 3])
def test_appended_blocks_each_cut(tmp_path, blocks):
    """Data appended at two or three offsets, the log and every block but the last cut in the middle of a data
    message (as the specification allows at each offset): reading goes on at each offset, nothing damaged."""
    start = 16 + 3 + 40
    parts = [_T_FORMAT + _T_SUBSCRIBE + _t(1) + _t(2) + _t(3)[:7]]
    parts.append(_t(10) + _t(11) + _t(12)[:9])
    if blocks == 3:
        parts.append(_t(20) + _t(21)[:3])
    parts.append(_t(30) + _t(31))
    offsets, at = [], start
    for part in parts[:-1]:
        at += len(part)
        offsets.append(at)
    log = _read(tmp_path, gnss_ulog.header() + gnss_ulog.flag_bits(*offsets) + b"".join(parts))
    expected = [1, 2, 10, 11] + ([20] if blocks == 3 else []) + [30, 31]
    assert log.get("t")["timestamp"].tolist() == expected
    assert log.get("t")["v"].tolist() == [t * 0.5 for t in expected]
    assert log.appended and log.damaged == 0 and not log.warnings
