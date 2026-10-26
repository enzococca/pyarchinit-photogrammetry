"""Post-processed kinematic positions: what the RINEX files of a base and a rover hold, and the solution RTKLIB
computes from them."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from pyproj import Transformer

from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.gnss import ppk, rinex

DATA = Path(__file__).parent / "data" / "rtklib"
ROVER = DATA / "07590920.05o"
BASE = DATA / "30400920.05o"


# --- RINEX files written by the tests ----------------------------------------------------------------------------


def _line(content: str, label: str) -> str:
    """A header line: the content in the first 60 columns, the label after them."""
    return content.ljust(60) + label


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def _epoch2(when: datetime, sats: list[str], flag: int = 0) -> list[str]:
    """The epoch line of a RINEX 2 file, with the satellite list continued on the next lines after 12 satellites."""
    seconds = when.second + when.microsecond / 1e6
    head = (f" {when.year % 100:02d} {when.month:2d} {when.day:2d} {when.hour:2d} {when.minute:2d}{seconds:11.7f}"
            f"  {flag}{len(sats):3d}")
    lines = [head + "".join(sats[:12])]
    for start in range(12, len(sats), 12):
        lines.append(" " * 32 + "".join(sats[start:start + 12]))
    return lines


def _observations2(count: int) -> list[str]:
    """The observation lines of ``count`` satellites with two observation types each."""
    return [f"{20000000.0 + 1000 * i:14.3f}  {100000.0 + i:14.3f}  " for i in range(count)]


V2_HEADER = [
    _line("     2.11           OBSERVATION DATA    M (MIXED)", "RINEX VERSION / TYPE"),
    _line("converter           test                20240512 10:30:00 UTC", "PGM / RUN BY / DATE"),
    _line("BASE", "MARKER NAME"),
    _line("SN123               TRIMBLE NETR9       5.45", "REC # / TYPE / VERS"),
    _line("1234                TRM57971.00     NONE", "ANT # / TYPE"),
    _line("  4519803.1234   1051234.5678   4400123.9876", "APPROX POSITION XYZ"),
    _line("        1.0230        0.0000        0.0000", "ANTENNA: DELTA H/E/N"),
    _line("     2    C1    L1", "# / TYPES OF OBSERV"),
    _line("     1.000", "INTERVAL"),
    _line("  2024     5    12    10    30    0.0000000     GPS", "TIME OF FIRST OBS"),
]
END_OF_HEADER = _line("", "END OF HEADER")

V3_HEADER = [
    _line("     3.03           OBSERVATION DATA    M: Mixed", "RINEX VERSION / TYPE"),
    _line("converter           test                20240512 10:30:00 UTC", "PGM / RUN BY / DATE"),
    _line("ROVR", "MARKER NAME"),
    _line("SN456               LEICA GR50          4.40", "REC # / TYPE / VERS"),
    _line("5678                LEIAR25.R4      LEIT", "ANT # / TYPE"),
    _line("  4519803.1234   1051234.5678   4400123.9876", "APPROX POSITION XYZ"),
    _line("        0.0000        0.0000        0.0000", "ANTENNA: DELTA H/E/N"),
    _line("G   14 C1C L1C D1C S1C C2W L2W D2W S2W C5Q L5Q D5Q S5Q C1W", "SYS / # / OBS TYPES"),
    _line("       L1W", "SYS / # / OBS TYPES"),
    _line("R    4 C1C L1C D1C S1C", "SYS / # / OBS TYPES"),
    _line("E    4 C1C L1C D1C S1C", "SYS / # / OBS TYPES"),
    _line("    30.000", "INTERVAL"),
    _line("  2024     5    12    10    30   30.0000000     GPS", "TIME OF FIRST OBS"),
]


def _epoch3(when: datetime, count: int) -> list[str]:
    seconds = when.second + when.microsecond / 1e6
    lines = [f"> {when.year:4d} {when.month:02d} {when.day:02d} {when.hour:02d} {when.minute:02d}{seconds:11.7f}"
             f"  0{count:3d}"]
    lines += [f"G{i + 1:02d}  {20000000.0 + 1000 * i:14.3f}  " for i in range(count)]
    return lines


def _first_obs(header: list[str], text: str) -> list[str]:
    """The header with another TIME OF FIRST OBS line."""
    return [_line(text, "TIME OF FIRST OBS") if h.endswith("TIME OF FIRST OBS") else h for h in header]


def _rinex2(path: Path, header: list[str], epochs: int = 3, extra: list[str] | None = None) -> Path:
    """A RINEX 2.11 observation file: ``epochs`` epochs a second apart from 10:30:00, 14 satellites in each,
    the last of them Galileo."""
    start = datetime(2024, 5, 12, 10, 30, 0)
    sats = [f"G{i:2d}" for i in range(1, 13)] + ["R 5", "E21"]
    lines = list(header) + [END_OF_HEADER]
    for n in range(epochs):
        lines += _epoch2(start + timedelta(seconds=n), sats) + _observations2(len(sats))
    return _write(path, lines + (extra or []))


def _rinex3(path: Path, header: list[str], epochs: int = 3) -> Path:
    """A RINEX 3.03 observation file with ``epochs`` epochs 30 s apart from 10:30:30."""
    start = datetime(2024, 5, 12, 10, 30, 30)
    lines = list(header) + [END_OF_HEADER]
    for n in range(epochs):
        lines += _epoch3(start + timedelta(seconds=30 * n), 5)
    return _write(path, lines)


# --- headers ----------------------------------------------------------------------------------------------------


def test_header_of_a_rtklib_observation_file():
    info = rinex.header(ROVER)
    assert info.version == 2.10
    assert info.kind == "obs"
    assert info.marker == "0759"
    assert info.receiver == "TRIMBLE 5700"
    assert info.antenna == "TRM29659.00"
    assert info.approx_xyz == (-3976219.5082, 3382372.5671, 3652512.9849)
    assert info.antenna_delta_hen == (0.0, 0.0, 0.0)
    assert info.interval == 30.0
    assert info.first == datetime(2005, 4, 2, 0, 0, 0)
    assert info.time_system == "GPS"
    assert info.systems == ("G",)
    assert info.compressed is None


def test_last_epoch_comes_from_the_epochs_when_the_header_has_none():
    # these files have a TIME OF FIRST OBS and no TIME OF LAST OBS: the last epoch is the last line of the data
    assert rinex.header(ROVER).last == datetime(2005, 4, 2, 0, 59, 30, 5000)
    assert rinex.header(BASE).last == datetime(2005, 4, 2, 0, 59, 29, 996000)
    assert rinex.time_span(ROVER) == (datetime(2005, 4, 2, 0, 0, 0), datetime(2005, 4, 2, 0, 59, 30, 5000))
    assert rinex.time_span(BASE) == (datetime(2005, 4, 2, 0, 0, 0), datetime(2005, 4, 2, 0, 59, 29, 996000))


def test_last_epoch_is_found_whatever_the_size_of_the_window_read_from_the_end(monkeypatch):
    # a window too small to hold one epoch grows until it does
    for size in (50, 700, 5000):
        monkeypatch.setattr(rinex, "_TAIL_BYTES", size)
        assert rinex.time_span(ROVER)[1] == datetime(2005, 4, 2, 0, 59, 30, 5000)
        assert rinex.time_span(BASE)[1] == datetime(2005, 4, 2, 0, 59, 29, 996000)


def test_a_file_with_no_epoch_in_the_window_is_read_through(monkeypatch):
    monkeypatch.setattr(rinex, "_TAIL_BYTES", 50)
    monkeypatch.setattr(rinex, "_TAIL_LIMIT", 100)
    assert rinex.time_span(ROVER)[1] == datetime(2005, 4, 2, 0, 59, 30, 5000)


def test_coordinates_run_together_in_their_columns_are_still_read(tmp_path):
    header = [h for h in V2_HEADER if "APPROX POSITION" not in h]
    header.append(_line("-10000000.1234-20000000.1234-30000000.1234", "APPROX POSITION XYZ"))
    info = rinex.header(_rinex2(tmp_path / "wide.24o", header))
    assert info.approx_xyz == (-10000000.1234, -20000000.1234, -30000000.1234)


def test_header_of_a_rtklib_navigation_file():
    info = rinex.header(DATA / "07590920.05n")
    assert (info.version, info.kind, info.systems) == (2.10, "nav", ("G",))
    assert info.receiver == info.antenna == info.marker == ""
    assert info.approx_xyz is None and info.first is None and info.last is None


def test_rinex2_mixed_systems_come_from_the_satellite_ids_even_when_continued(tmp_path):
    # the Galileo satellite is the 14th: it is on the continuation line of the satellite list
    path = _rinex2(tmp_path / "mixed.24o", V2_HEADER)
    info = rinex.header(path)
    assert info.version == 2.11
    assert info.systems == ("G", "R", "E")
    assert info.marker == "BASE"
    assert info.receiver == "TRIMBLE NETR9"
    assert info.antenna == "TRM57971.00     NONE"
    assert info.approx_xyz == (4519803.1234, 1051234.5678, 4400123.9876)
    assert info.antenna_delta_hen == (1.023, 0.0, 0.0)
    assert info.interval == 1.0
    assert info.first == datetime(2024, 5, 12, 10, 30, 0)
    assert info.last == datetime(2024, 5, 12, 10, 30, 2)


def test_rinex2_single_system_is_read_from_the_file_type(tmp_path):
    glonass = _line("     2.11           OBSERVATION DATA    R (GLONASS)", "RINEX VERSION / TYPE")
    assert rinex.header(_rinex2(tmp_path / "glonass.24o", [glonass] + V2_HEADER[1:])).systems == ("R",)
    blank = _line("     2.11           OBSERVATION DATA", "RINEX VERSION / TYPE")
    assert rinex.header(_rinex2(tmp_path / "gps.24o", [blank] + V2_HEADER[1:])).systems == ("G",)


def test_events_do_not_end_the_observations(tmp_path):
    # an epoch with the flag 4 announces header lines: it is not an observation epoch
    event = [" 24  5 12 10 59 59.0000000  4  1", _line("moved to the second point", "COMMENT")]
    path = _rinex2(tmp_path / "event.24o", V2_HEADER, extra=event)
    assert rinex.header(path).last == datetime(2024, 5, 12, 10, 30, 2)


def test_declared_last_observation_is_not_replaced_by_the_epochs(tmp_path):
    header = V2_HEADER + [_line("  2024     5    12    11     0    0.0000000     GPS", "TIME OF LAST OBS")]
    path = _rinex2(tmp_path / "declared.24o", header)
    assert rinex.header(path).last == datetime(2024, 5, 12, 11, 0, 0)
    assert rinex.time_span(path)[1] == datetime(2024, 5, 12, 10, 30, 2)  # the epochs say what the file holds


def test_header_of_a_rinex3_file(tmp_path):
    path = _rinex3(tmp_path / "rover.rnx", V3_HEADER)
    info = rinex.header(path)
    assert (info.version, info.kind) == (3.03, "obs")
    assert info.systems == ("G", "R", "E")  # the continuation line of the GPS types is not a system
    assert info.marker == "ROVR"
    assert info.receiver == "LEICA GR50"
    assert info.antenna == "LEIAR25.R4      LEIT"
    assert info.interval == 30.0
    assert info.first == datetime(2024, 5, 12, 10, 30, 30)
    assert info.last == datetime(2024, 5, 12, 10, 31, 30)  # from the epochs: the header has no TIME OF LAST OBS


def test_a_rinex3_file_with_one_system_in_the_file_type(tmp_path):
    gps = _line("     3.03           OBSERVATION DATA    G: GPS", "RINEX VERSION / TYPE")
    header = [gps] + [h for h in V3_HEADER[1:] if "SYS /" not in h]
    assert rinex.header(_rinex3(tmp_path / "gps.rnx", header)).systems == ("G",)


def test_rinex3_mixed_file_without_observation_types_reads_the_satellite_ids(tmp_path):
    header = [h for h in V3_HEADER if "SYS /" not in h]
    path = _rinex3(tmp_path / "bare.rnx", header)
    assert rinex.header(path).systems == ("G",)  # the test epochs only hold GPS satellites


def test_header_of_a_dji_file_with_empty_fields(tmp_path):
    header = [
        _line("     3.03           OBSERVATION DATA    M: Mixed", "RINEX VERSION / TYPE"),
        _line("DJI RTK              DJI                 20240512 10:30:00 UTC", "PGM / RUN BY / DATE"),
        _line("", "MARKER NAME"),
        _line("                    DJI RTK", "REC # / TYPE / VERS"),
        _line("", "ANT # / TYPE"),
        _line("        0.0000        0.0000        0.0000", "APPROX POSITION XYZ"),
        _line("G    4 C1C L1C D1C S1C", "SYS / # / OBS TYPES"),
        _line("  2024     5    12    10    30   30.0000000     GPS", "TIME OF FIRST OBS"),
    ]
    path = _rinex3(tmp_path / "DJI_202405121030_012_Rinex.obs", header)
    info = rinex.header(path)
    assert info.marker == "" and info.antenna == ""
    assert info.receiver == "DJI RTK"
    assert info.approx_xyz is None  # all zeros: the position is unknown
    assert info.antenna_delta_hen is None and info.interval is None
    assert info.systems == ("G",)
    assert info.last == datetime(2024, 5, 12, 10, 31, 30)


def test_a_clock_offset_on_an_epoch_line_is_not_a_satellite(tmp_path):
    # two GLONASS satellites and the receiver clock offset (F12.9 from column 68): the blank slots after the
    # two satellites are not GPS
    epoch = " 24  5 12 10 30  0.0000000  0  2R 5R 6".ljust(68) + " 0.123456789"
    path = _write(tmp_path / "clock.24o", V2_HEADER + [END_OF_HEADER, epoch, *_observations2(2)])
    assert rinex.header(path).systems == ("R",)


def test_only_the_satellites_an_epoch_declares_are_read(tmp_path):
    # 13 declared: 12 on the epoch line and one on the continuation line, which has room for more
    sats = ["G 1", "G 2", "G 3", "G 4", "G 5", "G 6", "G 7", "G 8", "G 9", "G10", "G11", "G12", "C 1"]
    lines = _epoch2(datetime(2024, 5, 12, 10, 30), sats)
    assert len(lines) == 2
    lines[1] += "E21"  # a slot the epoch does not declare
    path = _write(tmp_path / "declared.24o", V2_HEADER + [END_OF_HEADER] + lines + _observations2(13))
    assert rinex.header(path).systems == ("G", "C")


def test_a_year_padded_with_a_space_in_an_epoch_is_read(tmp_path):
    header = [h for h in V2_HEADER if "TIME OF FIRST OBS" not in h]
    lines = []
    for seconds in (0, 30):
        epoch = _epoch2(datetime(2005, 4, 2, 0, 0, seconds), ["G 3"])
        lines += ["  5" + epoch[0][3:]] + _observations2(1)
    path = _write(tmp_path / "padded.05o", header + [END_OF_HEADER] + lines)
    assert rinex.time_span(path) == (datetime(2005, 4, 2, 0, 0, 0), datetime(2005, 4, 2, 0, 0, 30))
    assert rinex.header(path).systems == ("G",)


def test_two_digit_years_of_the_last_century(tmp_path):
    header = [h for h in V2_HEADER if "TIME OF FIRST OBS" not in h]
    lines = []
    for when in (datetime(1999, 12, 31, 23, 59, 59), datetime(2000, 1, 1, 0, 0, 0)):
        lines += _epoch2(when, ["G 3"]) + _observations2(1)
    path = _write(tmp_path / "millennium.99o", header + [END_OF_HEADER] + lines)
    assert rinex.time_span(path) == (datetime(1999, 12, 31, 23, 59, 59), datetime(2000, 1, 1, 0, 0, 0))
    only_old = _write(tmp_path / "old.99o", header + [END_OF_HEADER] + lines[:2])
    assert rinex.header(only_old).first == datetime(1999, 12, 31, 23, 59, 59)


@pytest.mark.parametrize("version", [2, 3])
def test_a_file_with_windows_line_ends_is_read_the_same(tmp_path, version):
    if version == 2:
        plain = _rinex2(tmp_path / "unix.24o", V2_HEADER)
    else:
        plain = _rinex3(tmp_path / "unix.rnx", V3_HEADER)
    dos = tmp_path / ("dos" + plain.suffix)
    dos.write_bytes(plain.read_bytes().replace(b"\n", b"\r\n"))
    assert b"\r\n" in dos.read_bytes()
    assert rinex.header(dos) == rinex.header(plain)
    assert rinex.time_span(dos) == rinex.time_span(plain)
    assert rinex.header(dos).marker in ("BASE", "ROVR")


def test_an_event_before_the_first_epoch_is_not_the_first_epoch(tmp_path):
    event = [" 24  5 12 10 20  0.0000000  3  1", _line("moved to the first point", "COMMENT")]
    lines = V2_HEADER + [END_OF_HEADER] + event
    for n in range(3):
        lines += _epoch2(datetime(2024, 5, 12, 10, 30, n), ["G 3"]) + _observations2(1)
    path = _write(tmp_path / "event.24o", [h for h in lines if "TIME OF FIRST OBS" not in h])
    assert rinex.time_span(path) == (datetime(2024, 5, 12, 10, 30, 0), datetime(2024, 5, 12, 10, 30, 2))


def test_cycle_slip_records_have_the_time_of_an_observation_epoch(tmp_path):
    # the flag 6 line is followed by observation-like records at a time of the observations: it is an epoch
    lines = V2_HEADER + [END_OF_HEADER]
    lines += _epoch2(datetime(2024, 5, 12, 10, 30, 0), ["G 3"]) + _observations2(1)
    lines += _epoch2(datetime(2024, 5, 12, 10, 30, 1), ["G 3"], flag=6) + _observations2(1)
    assert rinex.header(_write(tmp_path / "slips.24o", lines)).last == datetime(2024, 5, 12, 10, 30, 1)


def test_a_line_of_numbers_that_is_not_three_numbers_is_not_a_position(tmp_path):
    for content in ("        1.0000        2.0000        3.0000 9", "1.0 2.0", "x y z"):
        header = [h for h in V2_HEADER if "APPROX POSITION" not in h and "DELTA H/E/N" not in h]
        header += [_line(content, "APPROX POSITION XYZ"), _line(content, "ANTENNA: DELTA H/E/N")]
        info = rinex.header(_rinex2(tmp_path / "numbers.24o", header))
        assert info.approx_xyz is None and info.antenna_delta_hen is None, content


def test_a_gzip_file_is_read_as_it_is(tmp_path):
    plain = _rinex2(tmp_path / "plain.24o", V2_HEADER)
    packed = tmp_path / "packed.24o.gz"
    packed.write_bytes(gzip.compress(plain.read_bytes()))
    info = rinex.header(packed)
    assert info.compressed == "gzip"
    assert info.last == datetime(2024, 5, 12, 10, 30, 2)
    assert rinex.time_span(packed) == rinex.time_span(plain)
    assert info.systems == ("G", "R", "E")
    assert rinex.header(plain).compressed is None


def test_a_gzip_file_cut_short_still_gives_the_epochs_before_the_cut(tmp_path):
    plain = _rinex2(tmp_path / "plain.24o", V2_HEADER, epochs=400)
    packed = tmp_path / "cut.24o.gz"
    data = gzip.compress(plain.read_bytes())
    packed.write_bytes(data[:len(data) // 2])
    first, last = rinex.time_span(packed)
    assert first == datetime(2024, 5, 12, 10, 30, 0)
    assert datetime(2024, 5, 12, 10, 30, 10) < last < datetime(2024, 5, 12, 10, 36, 40)
    assert rinex.header(packed).systems == ("G", "R", "E")


@pytest.mark.parametrize("name", ["base.crx", "base.24d", "BASE.24D.gz", "base.crx.gz"])
def test_hatanaka_files_are_refused_by_name(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"whatever")
    with pytest.raises(ValueError, match="Hatanaka") as error:
        rinex.header(path)
    assert "CRX2RNX" in str(error.value) and name in str(error.value)


def test_hatanaka_files_are_refused_by_their_first_line(tmp_path):
    first = _line("3.0                 COMPACT RINEX FORMAT", "CRINEX VERS   / TYPE")
    path = _write(tmp_path / "base.rnx", [first, V3_HEADER[0], END_OF_HEADER])
    with pytest.raises(ValueError, match="Hatanaka"):
        rinex.header(path)
    packed = tmp_path / "base2.rnx.gz"
    packed.write_bytes(gzip.compress(path.read_bytes()))
    with pytest.raises(ValueError, match="Hatanaka"):
        rinex.time_span(packed)


def test_files_that_are_not_rinex_are_refused_with_their_name(tmp_path):
    text = _write(tmp_path / "notes.txt", ["just some words", "on two lines"])
    empty = tmp_path / "empty.24o"
    empty.write_bytes(b"")
    binary = tmp_path / "photo.24o"
    binary.write_bytes(bytes(range(256)) * 8)
    unfinished = _write(tmp_path / "unfinished.24o", V2_HEADER)  # the header never ends
    notgz = tmp_path / "broken.24o.gz"
    notgz.write_bytes(b"this is not gzip")
    meteo = _write(tmp_path / "weather.24m", [_line("     2.11           METEOROLOGICAL DATA", "RINEX VERSION / TYPE"),
                                              END_OF_HEADER])
    for path in (text, empty, binary, unfinished, notgz, meteo):
        for call in (rinex.header, rinex.time_span):
            with pytest.raises(ValueError) as error:
                call(path)
            assert path.name in str(error.value)


def test_a_missing_file_stays_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        rinex.header(tmp_path / "nothing.24o")


def test_a_navigation_file_has_no_observation_epochs(tmp_path):
    with pytest.raises(ValueError, match="navigation"):
        rinex.time_span(DATA / "07590920.05n")


def test_time_span_of_a_file_without_epochs_is_none(tmp_path):
    path = _write(tmp_path / "bare.24o", V2_HEADER + [END_OF_HEADER])
    assert rinex.time_span(path) is None
    info = rinex.header(path)
    assert info.first == datetime(2024, 5, 12, 10, 30, 0) and info.last is None


def test_rinex3_navigation_with_several_systems(tmp_path):
    lines = [
        _line("     3.04           NAVIGATION DATA     M: Mixed", "RINEX VERSION / TYPE"),
        _line("", "END OF HEADER"),
        "G01 2024 05 12 10 00 00 1.0D-04 1.0D-12 0.0D+00",
        "    1.0D+02 1.0D+01 4.0D-09 2.0D+00",
        "E05 2024 05 12 10 00 00 1.0D-04 1.0D-12 0.0D+00",
        "    1.0D+02 1.0D+01 4.0D-09 2.0D+00",
        "C07 2024 05 12 10 00 00 1.0D-04 1.0D-12 0.0D+00",
    ]
    info = rinex.header(_write(tmp_path / "BRDC_MN.rnx", lines))
    assert (info.kind, info.systems) == ("nav", ("G", "E", "C"))


# --- time systems -------------------------------------------------------------------------------------------------


def test_gps_minus_utc_follows_the_leap_seconds():
    for when, seconds in [(datetime(1980, 1, 6), 0), (datetime(1981, 6, 30, 23, 59, 59), 0),
                          (datetime(1981, 7, 1), 1), (datetime(1998, 12, 31, 23, 59, 59), 12),
                          (datetime(1999, 1, 1), 13), (datetime(2005, 4, 2), 13), (datetime(2006, 1, 1), 14),
                          (datetime(2009, 1, 1), 15), (datetime(2012, 7, 1), 16), (datetime(2015, 7, 1), 17),
                          (datetime(2016, 12, 31, 23, 59, 59), 17), (datetime(2017, 1, 1), 18),
                          (datetime(2024, 5, 12, 10, 30), 18)]:
        assert rinex.gps_minus_utc(when) == seconds, when


def test_glonass_time_is_utc_and_becomes_gps_time_with_the_leap_seconds_of_the_header(tmp_path):
    header = _first_obs(V2_HEADER, "  2024     5    12    10    30    0.0000000     GLO")
    header.append(_line("    17", "LEAP SECONDS"))  # not the 18 of the table: the header is what counts
    path = _rinex2(tmp_path / "glo.24o", header)
    info = rinex.header(path)
    assert info.time_system == "GLO"
    assert info.first == datetime(2024, 5, 12, 10, 30, 17)
    assert info.last == datetime(2024, 5, 12, 10, 30, 19)
    assert rinex.time_span(path) == (datetime(2024, 5, 12, 10, 30, 17), datetime(2024, 5, 12, 10, 30, 19))


def test_glonass_time_without_leap_seconds_in_the_header_uses_the_table(tmp_path):
    header = _first_obs(V2_HEADER, "  2024     5    12    10    30    0.0000000     GLO")
    path = _rinex2(tmp_path / "glo.24o", header)
    assert rinex.header(path).first == datetime(2024, 5, 12, 10, 30, 18)
    assert rinex.time_span(path)[1] == datetime(2024, 5, 12, 10, 30, 20)
    # a file of 2005: 13 s
    old = [" 05  4  2  0  0  0.0000000  0  1G 3", *_observations2(1)]
    header = _first_obs(V2_HEADER, "  2005     4     2     0     0    0.0000000     GLO")
    path = _write(tmp_path / "old.05o", header + [END_OF_HEADER] + old)
    assert rinex.header(path).first == datetime(2005, 4, 2, 0, 0, 13)
    assert rinex.header(path).last == datetime(2005, 4, 2, 0, 0, 13)


def test_beidou_time_is_fourteen_seconds_behind_gps_time(tmp_path):
    header = _first_obs(V3_HEADER, "  2024     5    12    10    30   30.0000000     BDT")
    path = _rinex3(tmp_path / "bdt.rnx", header)
    info = rinex.header(path)
    assert info.time_system == "BDT"
    assert info.first == datetime(2024, 5, 12, 10, 30, 44)
    assert info.last == datetime(2024, 5, 12, 10, 31, 44)
    assert rinex.time_span(path) == (datetime(2024, 5, 12, 10, 30, 44), datetime(2024, 5, 12, 10, 31, 44))


@pytest.mark.parametrize("system", ["GPS", "GAL", "QZS", "IRN"])
def test_other_time_systems_are_gps_time(tmp_path, system):
    header = _first_obs(V3_HEADER, f"  2024     5    12    10    30   30.0000000     {system}")
    info = rinex.header(_rinex3(tmp_path / "t.rnx", header))
    assert info.time_system == system
    assert (info.first, info.last) == (datetime(2024, 5, 12, 10, 30, 30), datetime(2024, 5, 12, 10, 31, 30))


def test_a_blank_time_system_is_the_default_of_the_systems_of_the_file(tmp_path):
    blank = "  2024     5    12    10    30    0.0000000"
    # a RINEX 2 file with GLONASS only is in GLONASS time
    glonass = _line("     2.11           OBSERVATION DATA    R (GLONASS)", "RINEX VERSION / TYPE")
    path = _rinex2(tmp_path / "glo.24o", _first_obs([glonass] + V2_HEADER[1:], blank))
    info = rinex.header(path)
    assert (info.time_system, info.first) == ("GLO", datetime(2024, 5, 12, 10, 30, 18))
    # a RINEX 3 file with BeiDou only, in BeiDou time
    bds = _line("     3.03           OBSERVATION DATA    C: BeiDou", "RINEX VERSION / TYPE")
    header = _first_obs([bds] + [h for h in V3_HEADER[1:] if "SYS /" not in h], blank)
    info = rinex.header(_rinex3(tmp_path / "bds.rnx", header))
    assert (info.time_system, info.first) == ("BDT", datetime(2024, 5, 12, 10, 30, 14))
    # mixed or GPS only: GPS time
    for name, letter in [("mixed", V2_HEADER[0]), ("gps", _line("     2.11           OBSERVATION DATA    G (GPS)",
                                                              "RINEX VERSION / TYPE"))]:
        header = _first_obs([letter] + V2_HEADER[1:], blank)
        info = rinex.header(_rinex2(tmp_path / f"{name}.24o", header))
        assert (info.time_system, info.first) == ("GPS", datetime(2024, 5, 12, 10, 30, 0))


def test_the_time_system_of_a_navigation_file_is_its_default():
    assert rinex.header(DATA / "07590920.05n").time_system == "GPS"


# --- navigation files next to an observation file -------------------------------------------------------------------


def _touch(folder: Path, *names: str) -> None:
    for name in names:
        (folder / name).write_bytes(b"")


def _names(paths: list[Path]) -> list[str]:
    return [p.name for p in paths]


def test_companions_of_a_rinex2_daily_file(tmp_path):
    _touch(tmp_path, "site.24o", "site.24n", "site.24g", "site.24p.gz", "site.24l", "site.24c", "site.23n",
           "other.24n", "other.24o", "notes.txt", "extra.nav")
    (tmp_path / "folder.nav").mkdir()
    found = rinex.find_companions(tmp_path / "site.24o")
    assert _names(found) == ["extra.nav", "site.24c", "site.24g", "site.24l", "site.24n", "site.24p.gz"]
    assert all(p.parent == tmp_path and p.is_file() for p in found)


def test_companions_of_a_dji_file(tmp_path):
    _touch(tmp_path, "DJI_202405121030_012_Rinex.obs", "DJI_202405121030_012_Rinex.nav",
           "DJI_202405121100_013_Rinex.obs", "DJI_202405121100_013_Rinex.nav.gz", "Timestamp.MRK")
    found = rinex.find_companions(tmp_path / "DJI_202405121030_012_Rinex.obs")
    # the navigation messages of the other flight come too: RTKLIB takes the ephemeris nearest in time
    assert _names(found) == ["DJI_202405121030_012_Rinex.nav", "DJI_202405121100_013_Rinex.nav.gz"]


def test_companions_of_a_compressed_observation_file(tmp_path):
    _touch(tmp_path, "site.24o.gz", "site.24n.gz", "site.24g", "site.24o")
    assert _names(rinex.find_companions(tmp_path / "site.24o.gz")) == ["site.24g", "site.24n.gz"]


def test_companions_of_a_rinex3_long_name_file(tmp_path):
    obs = "ABCD00ITA_R_20241330000_01D_30S_MO.rnx"
    _touch(tmp_path, obs, "ABCD00ITA_R_20241330000_01D_MN.rnx", "ABCD00ITA_R_20241330000_01D_GN.rnx.gz",
           "ABCD00ITA_R_20241330000_01D_EN.rnx", "EFGH00ITA_R_20241330000_01D_MN.rnx",
           "BRDC00IGS_R_20241330000_01D_MN.rnx", "ABCD00ITA_R_20241330000_01D_30S_MO.crx")
    # the combined broadcast file of the IGS (BRDC00IGS) is the usual navigation file of a station: it comes too,
    # and so does the one of another station
    assert _names(rinex.find_companions(tmp_path / obs)) == [
        "ABCD00ITA_R_20241330000_01D_EN.rnx", "ABCD00ITA_R_20241330000_01D_GN.rnx.gz",
        "ABCD00ITA_R_20241330000_01D_MN.rnx", "BRDC00IGS_R_20241330000_01D_MN.rnx",
        "EFGH00ITA_R_20241330000_01D_MN.rnx"]


def test_companions_leave_out_hidden_files_and_the_apple_double_ones_of_an_external_drive(tmp_path):
    _touch(tmp_path, "site.24o", "site.24n", "._site.24n", "._site.24o", "._x.nav", ".hidden.nav", "x.nav",
           "._ABCD00ITA_R_20241330000_01D_MN.rnx", "ABCD00ITA_R_20241330000_01D_MN.rnx")
    assert _names(rinex.find_companions(tmp_path / "site.24o")) == ["ABCD00ITA_R_20241330000_01D_MN.rnx", "site.24n",
                                                                    "x.nav"]


def test_companions_of_an_observation_file_alone(tmp_path):
    _touch(tmp_path, "site.24o")
    assert rinex.find_companions(tmp_path / "site.24o") == []


# --- the position in the header ---------------------------------------------------------------------------------


def _llh_to_ecef(lat: float, lon: float, h: float) -> tuple[float, float, float]:
    """The textbook forward conversion, to check the inverse against."""
    a, e2 = 6378137.0, 0.00669437999014
    lat, lon = math.radians(lat), math.radians(lon)
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return ((n + h) * math.cos(lat) * math.cos(lon), (n + h) * math.cos(lat) * math.sin(lon),
            (n * (1 - e2) + h) * math.sin(lat))


def test_ecef_to_llh_agrees_with_proj_to_a_millimetre():
    to_llh = Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)
    points = [(-3976219.5082, 3382372.5671, 3652512.9849),  # the base of the RTKLIB sample
              (4519803.1234, 1051234.5678, 4400123.9876),
              (-2694045.0, -4293642.0, 3857878.0),  # southern hemisphere, western longitude
              (6378137.0 + 8000.0, 0.0, 0.0),
              (-4.0e6, -3.0e6, -4.0e6)]
    for xyz in points:
        lon, lat, h = to_llh.transform(*xyz)
        got = rinex.ecef_to_llh(xyz)
        # a millimetre of latitude is 9e-9 degrees; the longitude shrinks with the cosine of the latitude
        assert abs(got[0] - lat) < 1e-9 and abs(got[1] - lon) < 1e-9 and abs(got[2] - h) < 1e-3, xyz


def test_ecef_to_llh_inverts_the_forward_conversion_everywhere():
    for lat, lon, h in [(0.0, 0.0, 0.0), (45.0, 12.5, 100.0), (-33.9, 151.2, 58.0), (89.99999, -45.0, 3000.0),
                        (-60.0, 179.9, -300.0), (10.0, -120.0, 20000.0), (-34.65, -78.69, -190000.0),
                        (35.0, 45.0, 5.7e6)]:
        got = rinex.ecef_to_llh(_llh_to_ecef(lat, lon, h))
        assert abs(got[0] - lat) < 1e-9 and abs(got[1] - lon) < 1e-9 and abs(got[2] - h) < 1e-3


def test_ecef_to_llh_is_exact_far_from_the_surface():
    # PROJ's own inverse is a few tenths of a millimetre off at 190 km below the surface; this one is not
    for xyz in [(1.0e6, -5.0e6, -3.5e6), (7.0e6, 7.0e6, 7.0e6), (-1.0e6, 2.0e6, 3.0e6)]:
        back = _llh_to_ecef(*rinex.ecef_to_llh(xyz))
        assert max(abs(a - b) for a, b in zip(back, xyz)) < 1e-6


def test_ecef_to_llh_at_the_poles():
    north = rinex.ecef_to_llh((0.0, 0.0, 6356752.3142))
    assert north[0] == pytest.approx(90.0, abs=1e-9) and north[2] == pytest.approx(0.0, abs=1e-3)
    south = rinex.ecef_to_llh((0.0, 0.0, -6356752.3142 - 100.0))
    assert south[0] == pytest.approx(-90.0, abs=1e-9) and south[2] == pytest.approx(100.0, abs=1e-3)


def test_ecef_to_llh_takes_any_sequence_of_three_numbers():
    xyz = (-3976219.5082, 3382372.5671, 3652512.9849)
    expected = rinex.ecef_to_llh(xyz)
    assert rinex.ecef_to_llh(list(xyz)) == expected
    assert rinex.ecef_to_llh(np.array(xyz)) == expected
    assert all(isinstance(v, float) for v in rinex.ecef_to_llh(np.array(xyz)))
    with pytest.raises(ValueError):
        rinex.ecef_to_llh((1.0, 2.0))


def test_the_base_position_of_a_header_is_in_japan():
    lat, lon, h = rinex.ecef_to_llh(rinex.header(BASE).approx_xyz)
    assert 35.0 < lat < 36.0 and 139.0 < lon < 140.0 and 0.0 < h < 100.0


# --- the options of RTKLIB --------------------------------------------------------------------------------------

PPK_ROVER = DATA / "30400920.05o"  # the static station 3040, processed as a moving rover
PPK_BASE = DATA / "07590920.05o"  # 0759, 3.3 km away
PPK_NAV = DATA / "07590920.05n"
WEEK = 604800.0
START_2005 = 1316 * WEEK + 518400.0  # 2005-04-02 00:00:00 GPS time: GPS week 1316, Saturday


def _options(text: str) -> dict[str, str]:
    """The ``name=value`` lines of an options file of RTKLIB, without its comments."""
    options = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, value = line.split("=", 1)
            options[name.strip()] = value
    return options


def _rtklib_option_names() -> set[str]:
    """The names of the options RTKLIB knows, from its own table."""
    source = (Path(__file__).parents[1] / "native" / "vendor" / "rtklib" / "src" / "options.c").read_text()
    return set(re.findall(r'^\s*\{"([a-z0-9_-]+)",', source, re.MULTILINE | re.IGNORECASE))


def test_config_with_a_typed_base_position():
    text = ppk.config((35.160875039, 139.613837253, 70.1535), elevation_mask=10.0, systems="GRE")
    options = _options(text)
    assert options["pos1-posmode"] == "kinematic"
    assert options["pos1-soltype"] == "combined"
    assert float(options["pos1-elmask"]) == 10.0
    assert options["pos1-frequency"] == "l1+l2"
    assert options["pos1-navsys"] == str(1 + 4 + 8)  # GPS, GLONASS, Galileo
    assert options["pos2-armode"] == "fix-and-hold"
    assert options["ant2-postype"] == "llh"
    assert float(options["ant2-pos1"]) == pytest.approx(35.160875039, abs=1e-10)
    assert float(options["ant2-pos2"]) == pytest.approx(139.613837253, abs=1e-10)
    assert float(options["ant2-pos3"]) == pytest.approx(70.1535, abs=1e-4)
    assert options["ant2-anttype"] == "*"  # the antenna height of the base's header is added to its mark
    assert options["out-solformat"] == "llh"
    assert options["out-height"] == "ellipsoidal"
    assert options["out-timesys"] == "gpst"
    assert options["out-timeform"] == "tow"
    assert options["out-outhead"] == "on"
    # only options RTKLIB knows, no file of any kind, values without a "#" (a comment for RTKLIB) or blanks
    assert set(options) <= _rtklib_option_names()
    assert not any(name.startswith("file-") for name in options)
    assert all(value == value.strip() and value for value in options.values())
    assert "#" not in "".join(line for line in text.splitlines() if not line.startswith("#"))


def test_config_with_the_base_of_the_rinex_header():
    options = _options(ppk.config(None, mode="static"))
    assert options["pos1-posmode"] == "static"
    assert options["ant2-postype"] == "rinexhead"
    assert not any(name.startswith("ant2-pos") and name != "ant2-postype" for name in options)
    assert options["pos1-navsys"] == str(1 + 2 + 4 + 8 + 16 + 32 + 64)  # "all": every system RTKLIB knows
    assert float(options["pos1-elmask"]) == 15.0
    # the systems as letters, in any order and case, as a list too
    assert _options(ppk.config(None, systems=["c", "G"]))["pos1-navsys"] == str(1 + 32)
    assert _options(ppk.config(None, systems="JIS"))["pos1-navsys"] == str(16 + 64 + 2)


@pytest.mark.parametrize("arguments, match", [
    (dict(base_position=(91.0, 10.0, 0.0)), "latitude"),
    (dict(base_position=(45.0, 360.0, 0.0)), "longitude"),
    (dict(base_position=(45.0, -180.5, 0.0)), "longitude"),
    (dict(base_position=(45.0, 10.0, float("nan"))), "height"),
    (dict(base_position=(45.0, 10.0)), "latitude, longitude"),
    (dict(base_position=None, mode="ppp-kine"), "mode"),
    (dict(base_position=None, systems="GX"), "X"),
    (dict(base_position=None, systems=""), "system"),
    (dict(base_position=None, elevation_mask=90.0), "elevation"),
    (dict(base_position=None, elevation_mask=-1.0), "elevation"),
])
def test_config_refuses_what_rtklib_cannot_take(arguments, match):
    with pytest.raises(ValueError, match=match):
        ppk.config(**arguments)


# --- solution files ---------------------------------------------------------------------------------------------

POS_LEGEND = ("% (lat/lon/height=WGS84/ellipsoidal,Q=1:fix,2:float,3:sbas,4:dgps,5:single,6:ppp,"
              "ns=# of satellites)")
POS_COLUMNS = ("latitude(deg) longitude(deg)  height(m)   Q  ns   sdn(m)   sde(m)   sdu(m)  sdne(m)  sdeu(m)  sdun(m)"
               " age(s)  ratio")
# GPS seconds, latitude, longitude, height, Q, ns, sdn, sde, sdu: out of order, as the file need not be in order
POS_ROWS = [
    (START_2005 + 30.0, 35.132066167, 139.624300803, 75.6904, 2, 6, 0.1100, 0.1200, 0.1300),
    (START_2005, 35.132066159, 139.624300832, 75.6841, 1, 7, 0.0100, 0.0200, 0.0300),
    (START_2005 + 59.996, 35.132065640, 139.624303673, 75.7671, 5, 5, 1.1000, 1.2000, 2.3000),
]
LEAP_2005 = 13  # GPS time was 13 s ahead of UTC in 2005


def _pos_time(seconds: float, form: str, system: str) -> str:
    """A time of a solution row as RTKLIB writes it: GPS week and seconds of the week (tow) or the calendar (hms),
    in GPS time, UTC or Japan standard time."""
    seconds -= {"GPST": 0, "UTC": LEAP_2005, "JST": LEAP_2005 - 9 * 3600}[system]
    if form == "tow":
        week = int(seconds // WEEK)
        return f"{week:4d} {seconds - week * WEEK:10.3f}"
    when = datetime(1980, 1, 6) + timedelta(seconds=seconds)
    return f"{when:%Y/%m/%d %H:%M:%S}.{round(when.microsecond / 1000):03d}"


def _pos_text(rows=POS_ROWS, form: str = "tow", system: str = "GPST", legend: str | None = POS_LEGEND,
              columns: str = POS_COLUMNS, sep: str = " ") -> str:
    """A solution file of RTKLIB in latitude, longitude and height."""
    lines = ["% program   : RTKLIB ver.EX 2.5.1", "% inp file  : rover.obs", "%"]
    if legend:
        lines.append(legend)
    lines.append(f"%  {system:<{16 if form == 'hms' else 8}}{sep}" + sep.join(columns.split()))
    for seconds, lat, lon, h, q, ns, sdn, sde, sdu in rows:
        values = [f"{lat:14.9f}", f"{lon:14.9f}", f"{h:10.4f}", f"{q:3d}", f"{ns:3d}", f"{sdn:8.4f}", f"{sde:8.4f}",
                  f"{sdu:8.4f}", "  0.0000", "  0.0000", "  0.0000", "  0.00", "   3.2"]
        lines.append(_pos_time(seconds, form, system) + sep + sep.join(values))
    return "\r\n".join(lines) + "\r\n"  # RTKLIB ends its lines with \r\n


def _check_rows(solution: ppk.Solution) -> None:
    """The rows of POS_ROWS, in time order."""
    order = np.argsort([r[0] for r in POS_ROWS])
    rows = [POS_ROWS[i] for i in order]
    assert solution.times.dtype == np.float64
    assert np.allclose(solution.times, [r[0] for r in rows], rtol=0, atol=1e-6)
    assert np.allclose(solution.llh, [r[1:4] for r in rows], rtol=0, atol=1e-10)
    assert solution.q.tolist() == [r[4] for r in rows] and solution.ns.tolist() == [r[5] for r in rows]
    assert solution.q.dtype.kind == "i" and solution.ns.dtype.kind == "i"
    # RTKLIB writes sdn, sde, sdu: the solution has them east, north, up
    assert np.allclose(solution.sd, [(r[7], r[6], r[8]) for r in rows], rtol=0, atol=1e-9)
    assert solution.llh.shape == solution.sd.shape == (3, 3)


@pytest.mark.parametrize("form, system", [("tow", "GPST"), ("hms", "GPST"), ("tow", "UTC"), ("hms", "UTC"),
                                          ("hms", "JST")])
def test_read_pos_gives_gps_seconds_whatever_the_time_of_the_file(tmp_path, form, system):
    path = tmp_path / "rover.pos"
    path.write_text(_pos_text(form=form, system=system), newline="")
    solution = ppk.read_pos(path)
    _check_rows(solution)
    assert solution.path == path and solution.base == {} and solution.warnings == []


def test_read_pos_reads_the_columns_by_their_names(tmp_path):
    # commas between the fields (out-fieldsep) and the velocities after the standard deviations (out-outvel)
    columns = POS_COLUMNS + " vn(m/s) ve(m/s) vu(m/s) sdvn sdve sdvu sdvne sdveu sdvun"
    text = _pos_text(columns=columns, sep=",", form="hms")
    lines = [line + (",   0.10000,  -0.20000,   0.00000" + ",  0.01000" * 6 if line[0].isdigit() else "")
             for line in text.split("\r\n") if line]
    path = tmp_path / "rover.pos"
    path.write_text("\n".join(lines) + "\n")
    _check_rows(ppk.read_pos(path))


def test_read_pos_of_a_file_that_does_not_say_its_heights(tmp_path):
    # RTKLIB leaves out the legend with out-outhead=off: the heights are taken as RTKLIB's default, said in a warning
    path = tmp_path / "rover.pos"
    path.write_text(_pos_text(legend=None))
    solution = ppk.read_pos(path)
    _check_rows(solution)
    assert len(solution.warnings) == 1 and "ellipsoidal" in solution.warnings[0]


@pytest.mark.parametrize("text, match", [
    (_pos_text(rows=[]), "nothing was computed"),
    ("", "nothing was computed"),
    (_pos_text(legend=POS_LEGEND.replace("ellipsoidal", "geodetic")), "ellipsoidal"),
    (_pos_text(legend=POS_LEGEND.replace("WGS84", "Tokyo")), "WGS84"),
    (_pos_text(legend="% (x/y/z-ecef=WGS84,Q=1:fix,2:float,3:sbas,4:dgps,5:single,6:ppp,ns=# of satellites)",
               columns=POS_COLUMNS.replace("latitude(deg) longitude(deg)  height(m)", "x-ecef(m) y-ecef(m) z-ecef(m)")),
     "latitude, longitude"),
    (_pos_text(legend="% (e/n/u-baseline=WGS84,Q=1:fix,2:float,3:sbas,4:dgps,5:single,6:ppp,ns=# of satellites)",
               columns=POS_COLUMNS.replace("latitude(deg) longitude(deg)  height(m)",
                                           "e-baseline(m) n-baseline(m) u-baseline(m)")),
     "latitude, longitude"),
    (_pos_text(columns=POS_COLUMNS.replace("latitude(deg) longitude(deg)", "latitude(d'\") longitude(d'\")")),
     "degrees"),
    (_pos_text().replace("%  GPST    ", "%  TAI     "), "TAI"),
    ("$GPGGA,000000.00,3507.92397,N,13937.45805,E,4,07,1.0,75.684,M,0.0,M,0.0,0000*6A\r\n", "RTKLIB"),
    (_pos_text().replace("1316 518430.000", "1316 518430.000 x"), "line"),
    (_pos_text() + "1316 518490.000   35.13\r\n", "line"),
], ids=["no rows", "empty", "geoid heights", "Tokyo datum", "xyz", "enu", "dms", "time system", "nmea", "not a number",
        "short row"])
def test_read_pos_refuses_what_is_not_a_solution_in_ellipsoidal_heights(tmp_path, text, match):
    path = tmp_path / "rover.pos"
    path.write_text(text, newline="")
    with pytest.raises(ValueError, match=match):
        ppk.read_pos(path)


# --- solve: the checks before RTKLIB runs, the files it is given ------------------------------------------------


def _fake_rnx2rtkp(folder: Path) -> Path:
    """A stand-in for rnx2rtkp: appends to run.log beside it a JSON line with its arguments, the options file and the
    SHA-256 of every input (the inputs as RTKLIB would find them), copies the file named by PPG_FAKE_POS to its -o
    file (none: no output, as RTKLIB when it computed nothing), writes PPG_FAKE_MESSAGE to stderr as rnx2rtkp writes
    its messages and exits with PPG_FAKE_EXIT."""
    script = folder / "rnx2rtkp"
    log = folder / "run.log"
    script.write_text(f"""#!{sys.executable}
import hashlib, json, os, shutil, sys
args = sys.argv[1:]
conf, out, files = args[args.index("-k") + 1], args[args.index("-o") + 1], args[4:]
entry = {{"args": args, "options": open(conf).read(),
          "inputs": [hashlib.sha256(open(f, "rb").read()).hexdigest() for f in files]}}
open({str(log)!r}, "a").write(json.dumps(entry) + "\\n")
if os.environ.get("PPG_FAKE_POS"):
    shutil.copyfile(os.environ["PPG_FAKE_POS"], out)
sys.stderr.write(os.environ.get("PPG_FAKE_MESSAGE", "processing : 2005/04/02 00:59:30.0 Q=1") + "\\r")
sys.exit(int(os.environ.get("PPG_FAKE_EXIT", "0")))
""")
    script.chmod(0o755)
    if os.name == "nt":  # no shebang lines on Windows: a batch file starts the script
        wrapper = folder / "rnx2rtkp.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        return wrapper
    return script


@pytest.fixture
def fake_rtklib(tmp_path, monkeypatch):
    """RTKLIB as the fake rnx2rtkp (the core switched off), writing the solution of POS_ROWS; returns the log of
    its runs (a list of dicts, read when called)."""
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setenv("PPG_NATIVE", "0")
    monkeypatch.setenv("PPG_RNX2RTKP", str(_fake_rnx2rtkp(tools)))
    pos = tools / "canned.pos"
    pos.write_text(_pos_text(), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    for name in ("PPG_FAKE_MESSAGE", "PPG_FAKE_EXIT"):
        monkeypatch.delenv(name, raising=False)

    def runs() -> list[dict]:
        log = tools / "run.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return runs


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _user_files(folder: Path) -> dict[str, bytes]:
    """Every file under ``folder`` and its bytes."""
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def test_solve_gives_rtklib_plain_copies_of_the_inputs(tmp_path, fake_rtklib):
    # the user's files have names RTKLIB would read as wild cards, keywords or comments, and a compressed base
    user = tmp_path / "campagna è 100%r #1"
    user.mkdir()
    rover = user / ("volo %r #1.05o" if os.name == "nt" else "volo * %r #1.05o")  # no * in a name on Windows
    shutil.copyfile(PPK_ROVER, rover)
    base = user / "base è.05o.gz"
    base.write_bytes(gzip.compress(PPK_BASE.read_bytes()))
    nav = user / "base è.05n"
    shutil.copyfile(PPK_NAV, nav)
    before = _user_files(user)
    work = tmp_path / "lavoro" / "ppk"  # made when missing

    solution = ppk.solve(rover, base, [nav], folder=work)

    (run,) = fake_rtklib()
    expected = [str(work.resolve() / name) for name in ("rover.obs", "base.obs", "nav1.nav")]
    assert run["args"] == ["-k", str(work.resolve() / "ppk.conf"), "-o", str(work.resolve() / "solution.pos"),
                           *expected]
    # the copies RTKLIB read are the user's files, the base decompressed
    assert run["inputs"] == [_sha256(PPK_ROVER.read_bytes()), _sha256(PPK_BASE.read_bytes()),
                             _sha256(PPK_NAV.read_bytes())]
    assert _options(run["options"]) == _options(ppk.config(None))
    # the copies are gone, the options and the solution stay; the user's folder is as it was
    assert sorted(p.name for p in work.iterdir()) == ["ppk.conf", "solution.pos"]
    assert _user_files(user) == before
    # the solution: the rows of the file, where it is, the base from its header with the warning
    _check_rows(solution)
    assert solution.path == work.resolve() / "solution.pos"
    assert solution.base["name"] == "0759" and solution.base["file"] == "base è.05o.gz"
    assert solution.base["position_source"] == "header"
    assert solution.base["position"] == pytest.approx(list(rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)))
    assert len(solution.warnings) == 1
    assert "0759" in solution.warnings[0] and "header" in solution.warnings[0] and "metres" in solution.warnings[0]


def test_solve_with_a_typed_base_position(tmp_path, fake_rtklib):
    work = tmp_path / "work"
    solution = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], (35.16, 139.61, 70.0), folder=work)
    options = _options(fake_rtklib()[0]["options"])
    assert options["ant2-postype"] == "llh"
    assert [float(options[f"ant2-pos{i}"]) for i in (1, 2, 3)] == [35.16, 139.61, 70.0]
    assert solution.base == {"name": "0759", "position": [35.16, 139.61, 70.0], "position_source": "typed",
                             "file": "07590920.05o"}
    assert solution.warnings == []
    with pytest.raises(ValueError, match="latitude"):
        ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], (135.16, 139.61, 70.0), folder=work)
    assert len(fake_rtklib()) == 1  # RTKLIB did not run again


def test_solve_finds_the_navigation_files_beside_the_rover_then_beside_the_base(tmp_path, fake_rtklib):
    drone, station = tmp_path / "drone", tmp_path / "station"
    drone.mkdir(), station.mkdir()
    rover = drone / "DJI_Rinex.obs"
    shutil.copyfile(PPK_ROVER, rover)
    base = station / "07590920.05o"
    shutil.copyfile(PPK_BASE, base)
    (station / "07590920.05n").write_bytes(PPK_NAV.read_bytes())
    # no nav beside the drone's file: the base's
    ppk.solve(rover, base, folder=tmp_path / "work")
    assert fake_rtklib()[-1]["inputs"][2:] == [_sha256(PPK_NAV.read_bytes())]
    # a nav beside the drone's file: that one (the base's is not added)
    shutil.copyfile(DATA / "30400920.05n", drone / "DJI_Rinex.nav")
    ppk.solve(rover, base, folder=tmp_path / "work")
    assert fake_rtklib()[-1]["inputs"][2:] == [_sha256((DATA / "30400920.05n").read_bytes())]
    # a single file is a list of one
    ppk.solve(rover, base, PPK_NAV, folder=tmp_path / "work")
    assert fake_rtklib()[-1]["inputs"][2:] == [_sha256(PPK_NAV.read_bytes())]


def test_solve_refuses_without_a_navigation_file(tmp_path, fake_rtklib):
    folder = tmp_path / "obs"
    folder.mkdir()
    rover, base = folder / "rover.05o", folder / "base.05o"
    shutil.copyfile(PPK_ROVER, rover)
    shutil.copyfile(PPK_BASE, base)
    (folder / "notes.nav").write_text("not a navigation file\n")  # a .nav that is not RINEX is not one
    with pytest.raises(ValueError, match="navigation") as error:
        ppk.solve(rover, base, folder=tmp_path / "work")
    assert "rover.05o" in str(error.value) and "base.05o" in str(error.value) and ".nav" in str(error.value)
    # a file given as navigation that holds observations
    with pytest.raises(ValueError, match="navigation"):
        ppk.solve(rover, base, [base], folder=tmp_path / "work")
    assert fake_rtklib() == [] and not (tmp_path / "work").exists()


def test_solve_refuses_rover_and_base_without_hours_in_common(tmp_path, fake_rtklib):
    rover = _rinex2(tmp_path / "rover.24o", V2_HEADER)  # 2024-05-12 10:30:00 to 10:30:02
    with pytest.raises(ValueError, match="in common") as error:
        ppk.solve(rover, PPK_BASE, [PPK_NAV], folder=tmp_path / "work")
    text = str(error.value)
    assert "rover.24o" in text and "07590920.05o" in text
    assert "2024-05-12 10:30:00" in text and "2005-04-02 00:00:00" in text and "2005-04-02 00:59:30" in text
    assert fake_rtklib() == [] and not (tmp_path / "work").exists()


def test_solve_refuses_observations_that_are_not_observations(tmp_path, fake_rtklib):
    with pytest.raises(ValueError, match="rover"):
        ppk.solve(PPK_NAV, PPK_BASE, [PPK_NAV], folder=tmp_path / "work")
    with pytest.raises(ValueError, match="base"):
        ppk.solve(PPK_ROVER, PPK_NAV, [PPK_NAV], folder=tmp_path / "work")
    empty = _rinex2(tmp_path / "empty.24o", V2_HEADER, epochs=0)
    with pytest.raises(ValueError, match="no observation"):
        ppk.solve(PPK_ROVER, empty, [PPK_NAV], folder=tmp_path / "work")
    assert fake_rtklib() == []


def test_solve_refuses_a_base_with_no_position(tmp_path, fake_rtklib):
    header = [h for h in V2_HEADER if not h.endswith("APPROX POSITION XYZ")]
    base = _rinex2(tmp_path / "base.24o", header)
    rover = _rinex2(tmp_path / "rover.24o", V2_HEADER)
    with pytest.raises(ValueError, match="position") as error:
        ppk.solve(rover, base, [PPK_NAV], folder=tmp_path / "work")
    assert "base.24o" in str(error.value)
    assert fake_rtklib() == []
    # with its position typed it runs
    solution = ppk.solve(rover, base, [PPK_NAV], (45.0, 11.0, 50.0), folder=tmp_path / "work")
    assert solution.base["name"] == "BASE" and solution.base["position_source"] == "typed"


def test_solve_warns_of_a_time_system_it_does_not_know(tmp_path, fake_rtklib):
    header = _first_obs(V2_HEADER, "  2024     5    12    10    30    0.0000000     UTC")
    rover = _rinex2(tmp_path / "rover.24o", header)
    base = _rinex2(tmp_path / "base.24o", V2_HEADER)
    solution = ppk.solve(rover, base, [PPK_NAV], (45.0, 11.0, 50.0), folder=tmp_path / "work")
    assert len(solution.warnings) == 1
    assert "rover.24o" in solution.warnings[0] and "UTC" in solution.warnings[0]


def test_solve_when_rtklib_computes_nothing_or_fails(tmp_path, fake_rtklib, monkeypatch):
    work = tmp_path / "work"
    ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)  # a solution from an earlier run is there
    assert (work / "solution.pos").exists()
    # exit status 0 and no solution: nothing was computed, and the earlier solution is not taken for this one
    monkeypatch.delenv("PPG_FAKE_POS")
    monkeypatch.setenv("PPG_FAKE_MESSAGE", "error : no obs data")
    with pytest.raises(ValueError, match="nothing was computed: error : no obs data"):
        ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)
    # a solution file without positions
    empty = tmp_path / "empty.pos"
    empty.write_text(_pos_text(rows=[]))
    monkeypatch.setenv("PPG_FAKE_POS", str(empty))
    with pytest.raises(ValueError, match="nothing was computed: error : no obs data"):
        ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)
    # a full solution with an error message: the message is a warning
    monkeypatch.setenv("PPG_FAKE_POS", str(tmp_path / "tools" / "canned.pos"))
    monkeypatch.setenv("PPG_FAKE_MESSAGE", "error : no erp data")
    solution = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], (35.16, 139.61, 70.0), folder=work)
    _check_rows(solution)
    assert solution.warnings == ["RTKLIB: error : no erp data"]
    # RTKLIB stopped
    monkeypatch.setenv("PPG_FAKE_EXIT", "1")
    monkeypatch.setenv("PPG_FAKE_MESSAGE", "error : no sat ant pcv in igs.atx")
    with pytest.raises(RuntimeError, match="1.*no sat ant pcv"):
        ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)
    # every time, the copies of the inputs are gone
    assert sorted(p.name for p in work.iterdir()) == ["ppk.conf", "solution.pos"]


def test_solve_refuses_a_gzip_file_cut_short(tmp_path, fake_rtklib):
    packed = gzip.compress(PPK_BASE.read_bytes())
    base = tmp_path / "base.05o.gz"
    base.write_bytes(packed[:len(packed) * 2 // 3])  # a download that stopped: the epochs before the cut are read
    assert rinex.time_span(base)
    work = tmp_path / "work"
    with pytest.raises(ValueError, match="cut short"):
        ppk.solve(PPK_ROVER, base, [PPK_NAV], folder=work)
    assert fake_rtklib() == [] and sorted(p.name for p in work.iterdir()) == ["ppk.conf"]  # no copy left behind


def test_solve_never_takes_a_users_file_for_its_copy(tmp_path, fake_rtklib):
    # the user's own rover.obs in the folder given as the work folder: never emptied, never removed
    rover = tmp_path / "rover.obs"
    shutil.copyfile(PPK_ROVER, rover)
    with pytest.raises(ValueError, match="work folder"):
        ppk.solve(rover, PPK_BASE, [PPK_NAV], folder=tmp_path)
    assert rover.read_bytes() == PPK_ROVER.read_bytes() and fake_rtklib() == []


def test_the_work_folder_rtklib_is_given(tmp_path, monkeypatch):
    plain, accented = tmp_path / "work", tmp_path / "lavoro è"
    plain.mkdir(), accented.mkdir()
    # on macOS and Linux RTKLIB opens UTF-8 paths: any path but RTKLIB's keywords
    monkeypatch.setattr(ppk, "_on_windows", lambda: False)
    assert ppk._rtklib_folder(accented) == str(accented.resolve())
    for keyword in ("%r", "%b"):
        folder = tmp_path / f"100{keyword}"
        folder.mkdir()
        with pytest.raises(RuntimeError, match=keyword):
            ppk._rtklib_folder(folder)
    # on Windows RTKLIB opens files with the ANSI C runtime: a path that is not ASCII becomes its short (8.3) name
    monkeypatch.setattr(ppk, "_on_windows", lambda: True)
    asked = []

    def short_path(path: str) -> str:
        asked.append(path)
        return "C:\\Users\\NICCOL~1\\LAVORO~1"

    monkeypatch.setattr(ppk, "_short_path", short_path)
    assert ppk._rtklib_folder(plain) == str(plain.resolve()) and asked == []  # ASCII: as it is
    assert ppk._rtklib_folder(accented) == "C:\\Users\\NICCOL~1\\LAVORO~1" and asked == [str(accented.resolve())]
    # no short name (switched off on the volume): a clear error
    monkeypatch.setattr(ppk, "_short_path", lambda path: path)
    with pytest.raises(RuntimeError, match="ASCII"):
        ppk._rtklib_folder(accented)


@pytest.mark.skipif(os.name != "nt", reason="the short names of Windows")
def test_short_path_on_windows(tmp_path):
    folder = tmp_path / "a folder with a long name"
    folder.mkdir()
    short = ppk._short_path(str(folder))
    assert Path(short).is_dir() and Path(short).resolve() == folder.resolve()


# --- solve with RTKLIB ------------------------------------------------------------------------------------------
# How many epochs RTKLIB fixes on these data in kinematic mode (fix and hold on 3.3 km of 30 s epochs) changes from
# build to build, and with a nanometre of the base: from about 40 to 115 of 120. In static mode, the station 3040 being
# a fixed station, every epoch is fixed on every build: the accurate checks are made on static runs.

needs_rtklib = pytest.mark.skipif(not _native.supports("ppk"), reason="native core without RTKLIB (older than 0.5)")
EPOCHS = 120  # 00:00:00 to 00:59:30 every 30 s, in both files


def _rtklib_run(folder: Path, base_position=None, base: Path = PPK_BASE, mode: str = "static") -> ppk.Solution:
    """RTKLIB run by the core with the options of ``config`` on the sample data (3040 the rover, 0759 the base)."""
    folder.mkdir(parents=True, exist_ok=True)
    conf, out = folder / "ppk.conf", folder / "rover.pos"
    conf.write_text(ppk.config(base_position, mode=mode))
    code, message = _native.ppk_solve(str(conf), [str(PPK_ROVER), str(base), str(PPK_NAV)], str(out))
    assert code == 0, message
    return ppk.read_pos(out)


def _enu(llh: np.ndarray, origin) -> np.ndarray:
    """East, north, up offsets (m) of positions near ``origin``."""
    d = np.atleast_2d(llh) - np.asarray(origin)
    return np.c_[np.radians(d[:, 1]) * 6378137.0 * math.cos(math.radians(origin[0])), np.radians(d[:, 0]) * 6378137.0,
                 d[:, 2]]


def _raised_base(folder: Path, height: float) -> Path:
    """A copy of the base's observations with its antenna ``height`` above the mark in the header."""
    lines = PPK_BASE.read_text().splitlines(keepends=True)
    delta = _line(f"{height:14.4f}        0.0000        0.0000", "ANTENNA: DELTA H/E/N") + "\n"
    base = folder / "07590920.05o"
    base.write_text("".join(delta if line.rstrip().endswith("ANTENNA: DELTA H/E/N") else line for line in lines))
    assert rinex.header(base).antenna_delta_hen == (height, 0.0, 0.0)
    return base


@needs_rtklib
def test_static_solution_of_the_rtklib_data(tmp_path, monkeypatch):
    """The options of ``config`` in RTKLIB: 3040 processed as the fixed station it is against 0759 at the position of
    its header, every epoch fixed at the same place within a centimetre, where the header of 3040 puts it give or
    take the metres of the base's header position."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    static = _rtklib_run(tmp_path)
    fixed = static.q == 1
    assert len(static.times) == EPOCHS and fixed.sum() > 0.8 * EPOCHS
    assert np.all(np.ptp(_enu(static.llh[fixed], static.llh[-1]), axis=0) < 0.01)
    header_3040 = rinex.header(PPK_ROVER).approx_xyz
    assert np.linalg.norm(np.subtract(_llh_to_ecef(*static.llh[-1]), header_3040)) < 10.0


@needs_rtklib
def test_solve_on_rtklib_data(tmp_path, monkeypatch):
    """solve with the rover moving (kinematic, as a drone), the navigation file found beside the rover: a row for
    every epoch in GPS seconds, at least a quarter of them fixed, the fixed ones where the static solution puts the
    station. The base at the position of its header, with the warning, or typed there, without it."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    static = _rtklib_run(tmp_path / "static")
    station = static.llh[static.q == 1].mean(axis=0)
    mark = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    for name, base_position in (("header", None), ("typed", mark)):
        solution = ppk.solve(PPK_ROVER, PPK_BASE, base_position=base_position, folder=tmp_path / name)
        assert np.allclose(solution.times, START_2005 + 30.0 * np.arange(EPOCHS), rtol=0, atol=0.01)
        assert solution.llh.shape == solution.sd.shape == (EPOCHS, 3) and np.all(solution.sd >= 0)
        assert np.all(solution.ns >= 4) and set(solution.q.tolist()) <= set(ppk.QUALITY)
        fixed = solution.q == 1
        assert fixed.any()
        offsets = np.abs(_enu(solution.llh[fixed], station))
        assert np.all(np.median(offsets, axis=0) < [0.02, 0.02, 0.03]), np.median(offsets, axis=0)
        assert solution.base["name"] == "0759" and solution.base["position_source"] == name
        assert solution.base["position"] == pytest.approx(list(mark))
        # the copies of the inputs are gone (RTKLIB writes the time marks of the rover beside the solution)
        assert {p.name for p in solution.path.parent.iterdir()} - {"solution_events.pos"} == {"ppk.conf",
                                                                                              "solution.pos"}
        if name == "header":
            assert fixed.sum() >= 0.25 * EPOCHS
            assert len(solution.warnings) == 1 and "0759" in solution.warnings[0] and "header" in solution.warnings[0]
        else:
            assert solution.warnings == []


@needs_rtklib
def test_the_base_typed_or_from_the_header_and_its_antenna_height_in_rtklib(tmp_path, monkeypatch):
    """The base typed at the position of its header gives the positions of the header, typed a metre higher every
    position is a metre higher; the antenna height in the base's header (ANTENNA: DELTA H/E/N) is added to its mark,
    typed or from the header, once (not again by RTKLIB's own reading of the header). Static runs."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    header = _rtklib_run(tmp_path / "header")
    station = header.llh[header.q == 1].mean(axis=0)
    lat, lon, h = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    raised = _raised_base(tmp_path, 1.5)
    for i, (position, base, rise) in enumerate([((lat, lon, h), PPK_BASE, 0.0), ((lat, lon, h + 1.0), PPK_BASE, 1.0),
                                                ((lat, lon, h), raised, 1.5), (None, raised, 1.5)]):
        run = _rtklib_run(tmp_path / f"run{i}", position, base)
        fixed = run.q == 1
        assert fixed.sum() > 0.8 * EPOCHS, (position, base.name, fixed.sum())
        offset = _enu(run.llh[fixed].mean(axis=0), station)[0]
        assert np.allclose(offset, [0.0, 0.0, rise], rtol=0, atol=0.005), (position, base.name, offset)
