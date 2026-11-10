"""Raw receiver logs told by their content and converted to RINEX for the PPK (``gnss.convert``): u-blox and RTCM 3
logs, a DJI D-RTK 2/3 base log (``.DAT``, which DJI documents as RTCM 3) read as RTCM 3 only after its frames are
checked, conversions kept in a cache whose key names the converter.

There is no public D-RTK ``.DAT`` file: the D-RTK tests use RTCM 3 frames - the RTKLIB sample
``GMSD7_20121014_64k.rtcm3`` of tests/data/gnss/phase4 (README.md there), with a station message 1005 written here
from the RTCM 10403 layout - under a ``.DAT`` name. They show that a file made of RTCM 3 frames is read and that one
that is not is refused; they say nothing of a real D-RTK file."""
from __future__ import annotations

import gzip
import os
import random
from datetime import datetime
from pathlib import Path

import pytest

from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.gnss import convert

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


def _gps_seconds(when: datetime) -> float:
    return (when - GPS_EPOCH).total_seconds()


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "converted"


def _dji_dat(folder: Path, with_station: bool = True) -> Path:
    """The RTCM 3 sample under the name a D-RTK gives its logs, after a few bytes that are not a frame (a log may
    start in the middle of a message) and, first, a station message (the sample's station id, 611)."""
    path = folder / "DJI_202210140000_001.DAT"
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
    for weeks in (2, -2, -1):
        with pytest.raises(ValueError, match="week"):
            convert.to_rinex(dat, cache, approx_time=_gps_seconds(DAY_GMSD7) + weeks * 7 * 86400.0)
    assert not [p for p in cache.rglob("*") if p.is_file()]  # nothing kept of a refused conversion


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
