"""Raw receiver logs told by their content and converted to RINEX for the PPK (``gnss.convert``): u-blox and RTCM 3
logs, a DJI D-RTK 2/3 base log (``.DAT``, which DJI documents as RTCM 3) read as RTCM 3 only after its frames are
checked, conversions kept in a cache whose key names the converter.

There is no public D-RTK ``.DAT`` file: the D-RTK tests use RTCM 3 frames - the RTKLIB sample
``GMSD7_20121014_64k.rtcm3`` of tests/data/gnss/phase4 (README.md there), with a station message 1005 written here
from the RTCM 10403 layout - under a ``.DAT`` name. They show that a file made of RTCM 3 frames is read and that one
that is not is refused; they say nothing of a real D-RTK file."""
from __future__ import annotations

import binascii
import gzip
import os
import random
import struct
import zlib
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

import convbin_helpers as ubx
from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.gnss import convert, events, rinex

DATA = Path(__file__).parent / "data" / "gnss" / "phase4"
CRINEX = Path(__file__).parent / "data" / "crinex" / "kraw_rinex402.crx"
GPS_EPOCH = datetime(1980, 1, 6)
DAY_GMSD7 = datetime(2012, 10, 14)  # the day of the RTCM 3 sample (its first epoch: 2012-10-13 23:59:58 GPS)

CORE = _native.kernel("raw_to_rinex") is not None and (_native.api() or 0) >= _native.CONVBIN_API
needs_a_converter = pytest.mark.skipif(not _native.convbin_available(),
                                       reason="no converter: native core older than 0.8 and no convbin")

# a made-up position of the base (ECEF, m; RTCM 1005 keeps 0.1 mm)
STATION = (-3607665.1234, 4147868.5678, 3223717.9012)


def _bits(fields: list[tuple[int, int]]) -> bytes:
    """Values packed most significant bit first, each in its width (negative ones in two's complement), padded with
    zero bits to whole bytes."""
    value = width = 0
    for field, bits in fields:
        value = (value << bits) | (field & ((1 << bits) - 1))
        width += bits
    pad = -width % 8
    return (value << pad).to_bytes((width + pad) // 8, "big")


def _frame(payload: bytes) -> bytes:
    """An RTCM 3 frame: preamble 0xD3, 6 reserved bits and the 10-bit length, the payload, CRC-24Q of all before."""
    head = bytes([0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF]) + payload
    return head + convert.crc24q(head).to_bytes(3, "big")


def _message_1005(station_id: int, xyz: tuple[float, float, float]) -> bytes:
    """RTCM 3 message 1005 (stationary RTK reference station ARP), RTCM 10403: DF002 type, DF003 station id, DF021
    ITRF year, DF022-DF024 GPS/GLONASS/Galileo, DF141 reference station, DF025 X (0.1 mm), DF142 oscillator, a reserved
    bit, DF026 Y, DF364 quarter cycle, DF027 Z."""
    x, y, z = (round(v * 1e4) for v in xyz)
    return _frame(_bits([(1005, 12), (station_id, 12), (0, 6), (1, 1), (1, 1), (0, 1), (0, 1), (x, 38), (0, 1),
                         (0, 1), (y, 38), (0, 2), (z, 38)]))


def _message_1006(station_id: int, xyz: tuple[float, float, float], height: float) -> bytes:
    """RTCM 3 message 1006: 1005 followed by DF028, the antenna height above the mark (16 bits, 0.1 mm)."""
    x, y, z = (round(v * 1e4) for v in xyz)
    return _frame(_bits([(1006, 12), (station_id, 12), (0, 6), (1, 1), (1, 1), (0, 1), (0, 1), (x, 38), (0, 1),
                         (0, 1), (y, 38), (0, 2), (z, 38), (round(height * 1e4), 16)]))


def _gps_seconds(when: datetime) -> float:
    return (when - GPS_EPOCH).total_seconds()


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "converted"


def _dji_dat(folder: Path, with_station: bool = True, station: bytes | None = None) -> Path:
    """The RTCM 3 sample under the name a D-RTK gives its logs, after a few bytes that are not a frame (a log may
    start in the middle of a message) and, first, a station message (the sample's station id, 611)."""
    path = folder / "DJI_202210140000_001.DAT"
    if station is None:
        station = _message_1005(611, STATION) if with_station else b""
    path.write_bytes(b"\x00\x17\x42" + station + (DATA / "GMSD7_20121014_64k.rtcm3").read_bytes())
    return path


def test_crc24q_known_frames():
    # the check value of CRC-24/LTE-A (Qualcomm's CRC-24Q, polynomial 0x864CFB, initial 0): "123456789" -> 0xCDE703
    assert convert.crc24q(b"123456789") == 0xCDE703
    assert len(convert.CRC24Q) == 256 and convert.CRC24Q[1] == 0x864CFB
    data = (DATA / "GMSD7_20121014_64k.rtcm3").read_bytes()
    frames = list(convert.rtcm3_frames(data))
    assert len(frames) >= 200
    first_type, first_offset, first_size = frames[0]
    assert (first_offset, first_type) == (0, 1077)
    for kind, offset, size in frames[:20]:
        body = data[offset:offset + size - 3]
        assert convert.crc24q(body) == int.from_bytes(data[offset + size - 3:offset + size], "big")
    damaged = bytearray(data[:first_size])
    damaged[10] ^= 0x01
    assert list(convert.rtcm3_frames(bytes(damaged))) == []


def test_kinds_by_content(tmp_path):
    rtcm = tmp_path / "base.dat"  # wrong names on purpose: the content decides
    rtcm.write_bytes(b"\xff\xfe" + (DATA / "GMSD7_20121014_64k.rtcm3").read_bytes())
    ubx = tmp_path / "rover.bin"
    ubx.write_bytes((DATA / "ubx_20080526_64k.ubx").read_bytes())
    packed = tmp_path / "station.obs"
    packed.write_bytes(gzip.compress(CRINEX.read_bytes()))
    plain = tmp_path / "rover.ubx"
    plain.write_bytes((DATA / "ubx_20080526_64k.obs").read_bytes())
    noise = tmp_path / "noise.rtcm3"
    noise.write_bytes(random.Random(4).randbytes(65536))
    empty = tmp_path / "empty.ubx"
    empty.write_bytes(b"")
    assert convert.raw_kind(rtcm) == "rtcm3"
    assert convert.raw_kind(ubx) == "ubx"
    assert convert.raw_kind(packed) == "rinex"
    assert convert.raw_kind(plain) == "rinex"
    assert convert.raw_kind(noise) is None
    assert convert.raw_kind(empty) is None


@needs_a_converter
def test_ubx_converted_once_then_cached(tmp_path, cache, monkeypatch):
    log = tmp_path / "rover.bin"
    log.write_bytes((DATA / "ubx_20080526_64k.ubx").read_bytes())
    first = convert.to_rinex(log, cache)
    assert first.kind == "ubx" and first.source == log
    assert first.obs.is_file() and first.nav is not None and first.nav.is_file()
    assert first.epochs > 0 and first.events == 0
    start, end = first.span
    assert _gps_seconds(datetime(2008, 5, 26)) < start <= end < _gps_seconds(datetime(2008, 5, 27))
    assert first.station_position is not None  # u-blox: the receiver's own solution in the header
    obs_text = first.obs.read_text()
    # the conversion is the one of tests/test_convbin.py: same epochs as the official convbin
    expected = [line for line in (DATA / "ubx_20080526_64k.obs").read_text().splitlines() if line.startswith(">")]
    assert [line for line in obs_text.splitlines() if line.startswith(">")] == expected

    calls = []
    real = _native.raw_to_rinex
    monkeypatch.setattr(_native, "raw_to_rinex", lambda *a, **k: calls.append(a) or real(*a, **k))
    again = convert.to_rinex(log, cache)
    assert calls == []  # from the cache
    assert (again.obs, again.nav, again.epochs, again.span, again.station_position) == (
        first.obs, first.nav, first.epochs, first.span, first.station_position)

    os.utime(log, ns=(log.stat().st_atime_ns, log.stat().st_mtime_ns + 10**9))  # the log changed: converted again
    third = convert.to_rinex(log, cache)
    assert len(calls) == 1 and third.obs.is_file()
    assert not first.obs.exists()  # the conversion of its earlier state is gone
    assert not [p for p in cache.rglob("*") if p.name.endswith(".part")]

    third.obs.unlink()  # a conversion removed in part from the cache: made again
    fourth = convert.to_rinex(log, cache)
    assert len(calls) == 2 and fourth.obs.is_file() and fourth.epochs == third.epochs


@needs_a_converter
def test_cache_key_has_the_converter(tmp_path, cache, monkeypatch):
    log = DATA / "ubx_20080526_64k.ubx"
    first = convert.to_rinex(log, cache)
    calls = []
    real = _native.raw_to_rinex
    monkeypatch.setattr(_native, "raw_to_rinex", lambda *a, **k: calls.append(a) or real(*a, **k))
    monkeypatch.setattr(_native, "convbin_identity", lambda: "convbin /somewhere/else 1 2")
    second = convert.to_rinex(log, cache)
    assert len(calls) == 1  # another converter: converted again
    assert second.obs != first.obs and second.obs.is_file()
    convert.to_rinex(log, cache)
    assert len(calls) == 1


@needs_a_converter
def test_dji_dat_read_as_rtcm3(tmp_path, cache):
    dat = _dji_dat(tmp_path)
    assert convert.raw_kind(dat) == "rtcm3"
    with pytest.raises(ValueError, match="approximate time"):
        convert.to_rinex(dat, cache)
    done = convert.to_rinex(dat, cache, approx_time=DAY_GMSD7)
    assert done.kind == "rtcm3" and done.epochs == 64
    assert done.station_position == pytest.approx(STATION, abs=1e-4)
    assert done.span == (pytest.approx(_gps_seconds(datetime(2012, 10, 13, 23, 59, 58))),
                         pytest.approx(_gps_seconds(datetime(2012, 10, 14, 0, 1, 1))))
    assert done.nav is not None and done.nav.is_file()
    assert not [w for w in done.warnings if "week" in w]  # GPS ephemerides in the log: the week was checked
    # dji_dat is to_rinex for a D-RTK base, GPS seconds taken as well as a datetime
    same = convert.dji_dat(dat, cache, approx_time=_gps_seconds(DAY_GMSD7) + 3600.0)
    assert same.obs == done.obs  # the same day: the same conversion, from the cache


@needs_a_converter
def test_dji_dat_without_station_or_ephemerides(tmp_path, cache):
    """Without a station message the position is None; without GPS ephemerides (1019) the week cannot be checked and
    a warning says so."""
    data = (DATA / "GMSD7_20121014_64k.rtcm3").read_bytes()
    kept = b"".join(data[offset:offset + size] for kind, offset, size in convert.rtcm3_frames(data) if kind != 1019)
    dat = tmp_path / "DJI_002.DAT"
    dat.write_bytes(kept)
    done = convert.to_rinex(dat, cache, approx_time=DAY_GMSD7)
    assert done.station_position is None
    assert any("week" in w for w in done.warnings)


def test_dat_that_is_not_rtcm_refused(tmp_path, cache):
    dat = tmp_path / "DJI_003.DAT"
    dat.write_bytes(random.Random(7).randbytes(200_000))
    for call in (lambda: convert.to_rinex(dat, cache, approx_time=DAY_GMSD7),
                 lambda: convert.to_rinex(dat, cache, kind="rtcm3", approx_time=DAY_GMSD7),
                 lambda: convert.dji_dat(dat, cache, approx_time=DAY_GMSD7)):
        with pytest.raises(ValueError, match=r"DJI_003\.DAT: not an RTCM 3 log.*DJI Terra \(4\.4 or later\).*"
                                             r"DJI Assistant 2"):
            call()
    # a u-blox log called .DAT is not read as one either: a .DAT is read as RTCM 3 or not at all
    ubx_dat = tmp_path / "DJI_004.DAT"
    ubx_dat.write_bytes((DATA / "ubx_20080526_64k.ubx").read_bytes())
    with pytest.raises(ValueError, match="DJI Terra"):
        convert.to_rinex(ubx_dat, cache, approx_time=DAY_GMSD7)
    with pytest.raises(ValueError, match="DJI Terra"):
        convert.to_rinex(ubx_dat, cache, kind="ubx")
    # RTCM 3 frames only here and there in other data: refused, not converted in part
    frames = (DATA / "GMSD7_20121014_64k.rtcm3").read_bytes()[:3000]
    sparse = tmp_path / "DJI_005.DAT"
    sparse.write_bytes(random.Random(8).randbytes(30_000) + frames + random.Random(9).randbytes(30_000))
    with pytest.raises(ValueError, match="DJI Terra"):
        convert.to_rinex(sparse, cache, approx_time=DAY_GMSD7)
    assert not cache.exists() or not any(cache.iterdir())


@needs_a_converter
def test_rtcm_wrong_week_refused(tmp_path, cache):
    dat = _dji_dat(tmp_path)
    # whole weeks off, and days off that RTKLIB rounds to the week before or after (the sample starts 2 s before the
    # end of a GPS week: 4 days late puts it a week late)
    for days in (14, -14, -7, 7, 10, 4):
        with pytest.raises(ValueError, match="week"):
            convert.to_rinex(dat, cache, approx_time=_gps_seconds(DAY_GMSD7) + days * 86400.0)
    assert not [p for p in cache.rglob("*") if p.is_file()]  # nothing kept of a refused conversion
    for hours in (-30, 30):  # within the half week: the right week
        assert convert.to_rinex(dat, cache, approx_time=_gps_seconds(DAY_GMSD7) + hours * 3600.0).epochs == 64


@needs_a_converter
def test_refused_conversion_keeps_the_good_one(tmp_path, cache):
    """A conversion of the same file for another day that is refused leaves the good one in the cache."""
    dat = _dji_dat(tmp_path)
    good = convert.to_rinex(dat, cache, approx_time=DAY_GMSD7)
    with pytest.raises(ValueError, match="week"):
        convert.to_rinex(dat, cache, approx_time=_gps_seconds(DAY_GMSD7) + 14 * 86400.0)
    assert good.obs.is_file()


@needs_a_converter
def test_failed_or_cancelled_conversion_leaves_nothing(tmp_path, cache, monkeypatch):
    rtcm = tmp_path / "base.rtcm3"
    rtcm.write_bytes((DATA / "GMSD7_20121014_64k.rtcm3").read_bytes())
    with pytest.raises(ValueError, match="base.rtcm3"):
        convert.to_rinex(rtcm, cache, kind="ubx")  # the converter finds no u-blox observations
    assert not any(cache.iterdir())

    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

    progress = Progress(lambda fraction, message: progress.cancel())  # cancelled at its first message
    stopped = []
    monkeypatch.setattr(_native, "convert_cancel", lambda: stopped.append(True))
    real = _native.raw_to_rinex

    def slow(*args, **kwargs):  # long enough for the watcher to see the cancel
        time.sleep(0.4)
        return real(*args, **kwargs)

    monkeypatch.setattr(_native, "raw_to_rinex", slow)
    with pytest.raises(Cancelled):
        convert.to_rinex(rtcm, cache, approx_time=DAY_GMSD7, progress=progress)
    assert stopped and not any(cache.iterdir())


@needs_a_converter
def test_concurrent_conversions_of_one_file(tmp_path, cache):
    log = DATA / "ubx_20080526_64k.ubx"
    results, errors = [], []

    def run():
        try:
            results.append(convert.to_rinex(log, cache))
        except Exception as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len({r.obs for r in results}) == 1 and results[0].obs.is_file()
    assert [p.name for p in cache.iterdir()] == [results[0].obs.parent.name]


@needs_a_converter
def test_rtcm_1006_antenna_height_in_delta_h(tmp_path, cache):
    """The antenna height of message 1006 goes to ANTENNA: DELTA H/E/N, as RINEX keeps it: the position of the
    station stays that of the antenna reference point of the message, the height not added to it."""
    from pyarchinit_photogrammetry.gnss import rinex

    dat = _dji_dat(tmp_path, station=_message_1006(611, STATION, 1.8034))
    done = convert.to_rinex(dat, cache, approx_time=DAY_GMSD7)
    assert done.station_position == pytest.approx(STATION, abs=1e-4)
    assert rinex.header(done.obs).antenna_delta_hen == pytest.approx((1.8034, 0.0, 0.0), abs=1e-4)


def test_not_a_raw_log_refused(tmp_path, cache):
    other = tmp_path / "notes.txt"
    other.write_text("not a log\n" * 100)
    with pytest.raises(ValueError, match="notes.txt: not a raw receiver log"):
        convert.to_rinex(other, cache)
    with pytest.raises(ValueError, match="kind"):
        convert.to_rinex(DATA / "ubx_20080526_64k.ubx", cache, kind="trimble")


def test_rinex_given_as_it_is(tmp_path, cache):
    done = convert.to_rinex(DATA / "ubx_20080526_64k.obs", cache)
    assert done.kind == "rinex" and done.obs == DATA / "ubx_20080526_64k.obs" and done.nav is None
    assert done.epochs is None and done.events is None
    assert done.span[0] < done.span[1]
    assert not cache.exists() or not any(cache.iterdir())


# --- RTCM 3 MSM written by receivers of before the standard, events counted, logs named (real-data trials) --------


def _msm(message: int, epoch_ms: int, multiple: int = 1) -> bytes:
    """An MSM frame without satellites: DF002 type, DF003 station, the epoch time (30 bits), the multiple message bit,
    IODS, reserved, clock steering, external clock, smoothing and its interval, then the satellite and signal masks
    (64 and 32 bits, all zero: no cells)."""
    return _frame(_bits([(message, 12), (611, 12), (epoch_ms, 30), (multiple, 1), (0, 3), (0, 7), (0, 2), (0, 2),
                         (0, 1), (0, 3), (0, 64), (0, 32)]))


def _msm_epoch(frame: bytes) -> int:
    return (int.from_bytes(frame[3:11], "big") >> 10) & ((1 << 30) - 1)


def test_beidou_msm_in_gps_time_found_and_moved(tmp_path):
    """Three epochs whose BeiDou MSM carry the GPS time of the epoch (a receiver of 2012): told, and a copy with the
    BeiDou epochs 14 s back and their CRC right, every other byte the same. The standard (14 s behind) is not touched."""
    tows = (86_398_000, 86_399_000, 86_400_000)
    pre = b"".join(_msm(1077, t) + _msm(1087, 5_000) + _msm(1127, t, 0) for t in tows)
    survey = convert.rtcm3_survey(_write_bytes(tmp_path / "pre.rtcm3", pre))
    assert survey["bds_gps_time"] == 3 and survey["bds_standard"] == 0 and survey["types"][1127] == 3
    moved = tmp_path / "moved.rtcm3"
    assert convert._bds_moved_copy(tmp_path / "pre.rtcm3", moved) == 3
    frames = list(convert.rtcm3_frames(moved.read_bytes()))
    assert [m for m, _o, _s in frames] == [1077, 1087, 1127] * 3  # every CRC right
    data = moved.read_bytes()
    bds = [_msm_epoch(data[o:o + n]) for m, o, n in frames if m == 1127]
    assert bds == [t - 14_000 for t in tows]
    assert data != pre and len(data) == len(pre)
    standard = b"".join(_msm(1077, t) + _msm(1127, t - 14_000, 0) for t in tows)
    survey = convert.rtcm3_survey(_write_bytes(tmp_path / "std.rtcm3", standard))
    assert survey["bds_gps_time"] == 0 and survey["bds_standard"] == 3 and survey["repeated"] == 0
    # every MSM saying it ends its epoch: RTKLIB writes an epoch for each, the same time repeated
    repeated = b"".join(_msm(1077, t, 0) + _msm(1127, t - 14_000, 0) for t in tows)
    assert convert.rtcm3_survey(_write_bytes(tmp_path / "rep.rtcm3", repeated))["repeated"] == 3
    assert convert._msm_low_only({1071: 3, 1121: 3, 1019: 2}) == [1071, 1121]
    assert convert._msm_low_only({1071: 3, 1074: 1}) is None and convert._msm_low_only({1004: 3, 1071: 1}) is None


def _write_bytes(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


@pytest.mark.skipif(not CORE, reason="native core 0.8 needed")
def test_gmsd7_beidou_moved_keeps_every_system(tmp_path, cache):
    """The RTKLIB sample GMSD7 (Trimble NetR9, 2012) writes its BeiDou MSM in GPS time: converted as it is RTKLIB
    keeps BeiDou alone, 14 s late; from the moved copy every system, from 23:59:44."""
    done = convert.to_rinex(DATA / "GMSD7_20121014_64k.rtcm3", cache, approx_time=DAY_GMSD7)
    info = rinex.header(done.obs)
    assert set(rinex._obs_types(rinex._header_lines(done.obs), info.version)) >= {"G", "R", "C"}
    assert info.first == datetime(2012, 10, 13, 23, 59, 44)
    assert any("BeiDou MSM of the log carry their epoch in GPS time" in w for w in done.warnings)
    logs = [line for line in rinex._header_lines(done.obs) if line[:60].strip().startswith("log:")]
    assert logs and all(line[:60].split("log:")[1].strip() == "GMSD7_20121014_64k.rtcm3" for line in logs)


@pytest.mark.skipif(not CORE, reason="native core 0.8 needed")
def test_msm1_to_3_refused_by_name(tmp_path, cache):
    path = _write_bytes(tmp_path / "low.rtcm3", b"".join(_msm(1071, t) + _msm(1121, t - 14_000, 0)
                                                         for t in range(86_390_000, 86_400_000, 1000)))
    with pytest.raises(ValueError, match="MSM1-3 only.*1071, 1121.*MSM4 or MSM7"):
        convert.to_rinex(path, cache, approx_time=DAY_GMSD7)


@pytest.mark.skipif(not CORE, reason="native core 0.8 needed")
def test_events_counted_in_the_rinex_and_log_named_without_folder(tmp_path, cache):
    """A time mark between two raw epochs and none after: one event (convrnx counts one more when a time mark ends its
    scan); the header names the log, not its folder."""
    data = (DATA / "ubx_20080526_64k.ubx").read_bytes()
    epochs = ubx.raw_epochs(data)
    week, tow = epochs[len(epochs) // 2][1:]
    log = tmp_path / "deep" / "folder" / "rover.ubx"
    log.parent.mkdir(parents=True)
    log.write_bytes(ubx.insert_time_marks(data, [ubx.tim_tm2(week, tow + 0.25, count=1)],
                                          [week * ubx.WEEK + tow + 0.25]))
    done = convert.to_rinex(log, cache)
    assert done.events == 1 == len(events.rinex_events(done.obs).shots)
    logs = [line[:60] for line in rinex._header_lines(done.obs) if "log:" in line[:60]]
    assert logs and all(line.split("log:")[1].strip() == "rover.ubx" for line in logs)
    # a log ending with a time mark (TIM-TM2 after the last epoch): still the events written
    last = tmp_path / "last.ubx"
    week, tow = epochs[-1][1:]
    last.write_bytes(data + ubx.tim_tm2(week, tow + 0.1, count=1))
    assert convert.to_rinex(last, cache).events == len(events.rinex_events(convert.to_rinex(last, cache).obs).shots)


def _sbf(block_id: int, body: bytes) -> bytes:
    """A Septentrio SBF block: "$@", CRC-16-CCITT of the rest, the ID, the length (a multiple of 4), the body."""
    length = 8 + len(body) + (-(8 + len(body)) % 4)
    rest = struct.pack("<HH", block_id, length) + body + bytes(length - 8 - len(body))
    return b"$@" + struct.pack("<H", binascii.crc_hqx(rest, 0)) + rest


def _unicore(message: int, body: bytes) -> bytes:
    """A Unicore binary message: 0xAA 0x44 0xB5, a 24-byte header (bytes 4-5 the message ID, 6-7 the length of the
    body), the body and the CRC-32 (NovAtel's: reflected 0xEDB88320, from 0) of header and body."""
    header = b"\xaa\x44\xb5\x00" + struct.pack("<HH", message, len(body)) + bytes(16)
    crc = zlib.crc32(header + body, 0xFFFFFFFF) ^ 0xFFFFFFFF
    return header + body + struct.pack("<I", crc)


def _rt17(kind: int, data: bytes) -> bytes:
    """A Trimble RT-17 packet: STX, status, type, length, data, checksum (sum of status to data), ETX."""
    body = bytes([0, kind, len(data)]) + data
    return b"\x02" + body + bytes([sum(body) & 0xFF]) + b"\x03"


def test_other_receivers_told_by_content(tmp_path):
    rng = random.Random(7)
    sbf = _write_bytes(tmp_path / "a.bin", b"\x01\x02" + b"".join(_sbf(4027, rng.randbytes(40)) for _ in range(4)))
    uni = _write_bytes(tmp_path / "b.bin", b"".join(_unicore(12, rng.randbytes(60)) for _ in range(4)))
    rt17 = _write_bytes(tmp_path / "c.bin", b"".join(_rt17(0x57, rng.randbytes(30)) for _ in range(4)))
    assert convert.raw_kind(sbf) == "sbf" and convert.raw_kind(uni) == "unicore" and convert.raw_kind(rt17) == "rt17"
    broken = bytearray(sbf.read_bytes())
    for k in (4, 52, 100, 148):  # a byte of every block changed: no CRC holds
        broken[k + 10] ^= 0xFF
    assert convert.raw_kind(_write_bytes(tmp_path / "d.bin", bytes(broken))) is None
    # NovAtel (0xAA 0x44 0x12) is not told: its kind is given
    assert convert.raw_kind(_write_bytes(tmp_path / "e.bin", b"\xaa\x44\x12" + rng.randbytes(500))) is None


@pytest.mark.skipif(not CORE, reason="native core 0.8 needed")
@pytest.mark.parametrize("kind", ["sbf", "unicore", "rt17"])
def test_other_receivers_reach_their_decoders(tmp_path, cache, kind):
    """Frames without observations: the decoder of the core is reached and finds nothing - a clear refusal."""
    rng = random.Random(9)
    frames = {"sbf": lambda: _sbf(4027, rng.randbytes(40)), "unicore": lambda: _unicore(12, rng.randbytes(60)),
              "rt17": lambda: _rt17(0x57, rng.randbytes(30))}[kind]
    path = _write_bytes(tmp_path / f"{kind}.bin", b"".join(frames() for _ in range(20)))
    with pytest.raises(ValueError):
        convert.to_rinex(path, cache, approx_time=DAY_GMSD7)
    assert not [p for p in cache.rglob("*") if p.is_file()]
