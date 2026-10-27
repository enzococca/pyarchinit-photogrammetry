"""Post-processed kinematic positions: what the RINEX files of a base and a rover hold, the solution RTKLIB computes
from them and the position of the camera it gives at every shot."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from pyproj import Geod, Transformer

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.gnss import apply as gnss_apply
from pyarchinit_photogrammetry.gnss import atx, dji, download, frames, positions, ppk, rinex, stations
import gnss_helpers
from gnss_helpers import (EPOCHS, MRK_LAT, POS_COLUMNS, POS_LEGEND, POS_ROWS, PPK_BASE, PPK_NAV, PPK_ROVER, START_2005,
                          WEEK, needs_rtklib)
from gnss_helpers import drone_rows as _drone_rows
from gnss_helpers import enu as _enu
from gnss_helpers import fake_rnx2rtkp as _fake_rnx2rtkp
from gnss_helpers import jpeg as _jpeg  # synthetic photos with the time of their camera
from gnss_helpers import mrk_line as _mrk_line
from gnss_helpers import pos_text as _pos_text
from gnss_helpers import ppk_flight as _ppk_flight
from gnss_helpers import rtklib_option_names as _rtklib_option_names
from gnss_helpers import rtklib_options as _options
from gnss_helpers import snapshot as _snapshot  # the references of a chunk

fake_rtklib = gnss_helpers.fake_rtklib  # the fixture: RTKLIB as a script that logs its runs

DATA = Path(__file__).parent / "data" / "rtklib"
ROVER = DATA / "07590920.05o"
BASE = DATA / "30400920.05o"


# --- RINEX files written by the tests ----------------------------------------------------------------------------


def _line(content: str, label: str) -> str:
    """A header line: the content in the first 60 columns, the label after them."""
    return content.ljust(60) + label


def _write(path: Path, lines: list[str]) -> Path:
    """``lines`` with "\\n" line ends on every system (not the "\\r\\n" of a text file on Windows)."""
    path.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
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


HATANAKA_NAMES = ["base.crx", "base.24d", "BASE.24D.gz", "base.crx.gz"]
needs_a_crinex_decoder = pytest.mark.skipif(_native.kernel("crx2rnx") is None and _native._crx2rnx_program() is None,
                                            reason="neither the native core 0.6 (API 9) nor a CRX2RNX program")


@pytest.mark.parametrize("name", HATANAKA_NAMES)
def test_a_name_does_not_make_a_file_hatanaka(tmp_path, name):
    """A file is Hatanaka by its first line, not by its name: what is called .crx and is not RINEX is not RINEX."""
    path = tmp_path / name
    path.write_bytes(b"whatever")
    with pytest.raises(ValueError, match="not a RINEX file") as error:
        rinex.header(path)
    assert name in str(error.value)


@needs_a_crinex_decoder
@pytest.mark.parametrize("name", HATANAKA_NAMES)
def test_hatanaka_files_are_read(tmp_path, monkeypatch, name):
    """Compact RINEX, alone or in a gzip file, is read as the RINEX it holds (tests/test_gnss_rinex_files.py): the
    files that used to be refused by their names."""
    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))  # the decompressed copy stays with the test
    crx = DATA.parent / "crinex" / "07590920.05d"  # the sample of station 0759, RTKLIB's, in Compact RINEX 1.0
    path = tmp_path / name
    path.write_bytes(gzip.compress(crx.read_bytes()) if name.lower().endswith(".gz") else crx.read_bytes())
    info = rinex.header(path)
    assert info.compressed == ("gzip+hatanaka" if name.lower().endswith(".gz") else "hatanaka")
    assert (info.marker, info.antenna, info.interval) == ("0759", "TRM29659.00", 30.0)
    assert rinex.time_span(path) == rinex.time_span(ROVER)


@pytest.mark.parametrize("gz", [False, True])
def test_hatanaka_files_are_refused_without_a_decoder(tmp_path, monkeypatch, gz):
    """Without the native core 0.6 or a CRX2RNX, a file whose first line is that of Compact RINEX is refused, with its
    name and what to install - the other files are read all the same."""
    def no_decoder(*_args):
        raise RuntimeError(_native.CRINEX_MISSING)

    monkeypatch.setattr(_native, "crx2rnx", no_decoder)
    first = _line("3.0                 COMPACT RINEX FORMAT", "CRINEX VERS   / TYPE")
    path = _write(tmp_path / "base.rnx", [first, V3_HEADER[0], END_OF_HEADER])
    if gz:
        packed = tmp_path / "base2.rnx.gz"
        packed.write_bytes(gzip.compress(path.read_bytes()))
        path = packed
    for call in (rinex.header, rinex.time_span):
        with pytest.raises(ValueError, match="Hatanaka") as error:
            call(path)
        assert "CRX2RNX" in str(error.value) and path.name in str(error.value)
    assert rinex.header(ROVER).marker == "0759"


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


def test_companions_take_the_daily_broadcast_files_of_the_igs(tmp_path):
    """The RINEX 2 broadcast file of the IGS (brdcDDD0.YYn GPS, .YYg GLONASS, also .gz, any letter case) beside an
    observation file of another name: of its day and of the days around it (a flight across midnight) when the name of
    the observations says the day, of its year when only the extension says it, any otherwise."""
    _touch(tmp_path, "07590920.05o", "brdc0920.05n", "BRDC0920.05G", "brdc0910.05n.gz", "brdc0930.05n", "brdc0950.05n",
           "brdc0920.04n", "brdc0920.05o", "brdc092.05n", "notes.txt")
    # (sorted by name here: Windows sorts paths ignoring the letter case)
    assert sorted(_names(rinex.find_companions(tmp_path / "07590920.05o"))) == [
        "BRDC0920.05G", "brdc0910.05n.gz", "brdc0920.05n", "brdc0930.05n"]
    # a RINEX 3 long name gives its day too; the first day of a year takes the last of the year before
    _touch(tmp_path, "ABCD00ITA_R_20240010000_01D_30S_MO.rnx", "brdc0010.24n", "brdc3640.23n", "brdc3650.23n")
    assert _names(rinex.find_companions(tmp_path / "ABCD00ITA_R_20240010000_01D_30S_MO.rnx")) == [
        "brdc0010.24n", "brdc3650.23n"]
    # only the year in the extension; no day nor year in the name of a DJI file: every broadcast file there
    year = tmp_path / "anno"
    year.mkdir()
    _touch(year, "site.05o", "brdc0920.05n", "brdc2000.05g", "brdc0920.06n")
    assert _names(rinex.find_companions(year / "site.05o")) == ["brdc0920.05n", "brdc2000.05g"]
    dji = tmp_path / "dji"
    dji.mkdir()
    _touch(dji, "DJI_202405121030_012_Rinex.obs", "brdc1330.24n", "brdc0920.05n")
    assert _names(rinex.find_companions(dji / "DJI_202405121030_012_Rinex.obs")) == ["brdc0920.05n", "brdc1330.24n"]


@pytest.mark.parametrize("name, magic, kind", [("base.05o.Z", b"\x1f\x9d\x90", "compress (.Z)"),
                                                ("base.05d.Z", b"\x1f\x9d\x90", "compress (.Z)"),
                                                ("base.05o.zip", b"PK\x03\x04", "zip"),
                                                ("BASE.ZIP", b"PK\x03\x04", "zip"),
                                                ("base.05o", b"\x1f\x9d\x90", "compress (.Z)"),  # renamed
                                                ("base.obs", b"PK\x03\x04", "zip")])
def test_files_compressed_with_compress_or_zip_are_read_or_said_to_be_damaged(tmp_path, name, magic, kind):
    """The archives of the stations give files compressed with the Unix compress (.Z) or zipped (they are read:
    tests/test_gnss_rinex_files.py). A file that starts like one and holds nothing of the kind is told by its first
    bytes, whatever its name, and named when it is read."""
    path = tmp_path / name
    path.write_bytes(magic + bytes(range(256)))
    assert rinex.compression(path) == (("compress",) if kind.startswith("compress") else ("zip",))
    for call in (rinex.header, rinex.time_span):
        with pytest.raises(ValueError) as error:
            call(path)
        assert name in str(error.value)


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


def test_llh_to_ecef_is_the_inverse_of_ecef_to_llh():
    """The forward conversion beside the inverse one: as PROJ makes it (WGS 84, EPSG:4979 to EPSG:4978) to a
    micrometre, and back to the same latitude, longitude and height."""
    geocentric = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    for lat, lon, h in ((35.160875039, 139.613837253, 70.1535), (-33.9, 151.2, -10.5), (89.999, -179.5, 8000.0),
                        (0.0, 0.0, 0.0), (-90.0, 12.5, 100.0)):
        xyz = rinex.llh_to_ecef((lat, lon, h))
        assert all(type(v) is float for v in xyz)
        assert np.allclose(xyz, geocentric.transform(lon, lat, h), rtol=0, atol=1e-6), (lat, lon, h)
        assert np.allclose(rinex.ecef_to_llh(xyz), (lat, lon, h), rtol=0, atol=1e-9)
    assert rinex.llh_to_ecef(np.array([35.0, 139.0, 70.0])) == rinex.llh_to_ecef([35.0, 139.0, 70.0])
    with pytest.raises(ValueError, match="three"):
        rinex.llh_to_ecef((35.0, 139.0))


def test_the_base_position_of_a_header_is_in_japan():
    lat, lon, h = rinex.ecef_to_llh(rinex.header(BASE).approx_xyz)
    assert 35.0 < lat < 36.0 and 139.0 < lon < 140.0 and 0.0 < h < 100.0


# --- the options of RTKLIB --------------------------------------------------------------------------------------


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
    (dict(base_position=None, solution="smoothed"), "solution"),
    (dict(base_position=None, static_single=True), "static"),  # a single position of a moving rover
    (dict(base_position=None, mode="dgps", static_single=True), "static"),
])
def test_config_refuses_what_rtklib_cannot_take(arguments, match):
    with pytest.raises(ValueError, match=match):
        ppk.config(**arguments)


def test_config_of_a_single_static_solution():
    """A receiver standing still for a session (a base whose mark is sought), solved forward and written as one
    position, its antenna and the antenna's height above the mark from its header; by default the forward and backward
    solutions combined, a position for every epoch."""
    options = _options(ppk.config((35.16, 139.61, 70.0), mode="static", rover_antenna="*", solution="forward",
                                  static_single=True))
    assert options["pos1-posmode"] == "static" and options["pos1-soltype"] == "forward"
    assert options["out-solstatic"] == "single" and options["ant1-anttype"] == "*"
    assert _options(ppk.config(None, mode="static-start", static_single=True))["out-solstatic"] == "single"
    default = _options(ppk.config(None))
    assert default["pos1-soltype"] == "combined" and "out-solstatic" not in default
    assert _options(ppk.config(None, solution="backward"))["pos1-soltype"] == "backward"


def _rtklib_options_table() -> list[tuple[str, int, str]]:
    """RTKLIB's table of options (sysopts of options.c) in its order: the name, the format (0 integer, 1 number,
    2 text, 3 one of a list) and the comment, which for a list is its "number:label" items."""
    source = (Path(__file__).parents[1] / "native" / "vendor" / "rtklib" / "src" / "options.c").read_text()
    macros = dict(re.findall(r'^#define\s+(\w+)\s+"([^"]*)"', source, re.MULTILINE))
    table = source[source.index("opt_t sysopts[]"):]
    table = table[:table.index("};")]
    entries = re.findall(r'\{"([^"]+)",\s*(\d+),\s*\(void \*\)[^,]*,\s*(\w+|"[^"]*")\s*\}', table)
    return [(name, int(kind), macros[comment] if comment in macros else comment.strip('"'))
            for name, kind, comment in entries]


def _rtklib_enum(value: str, comment: str) -> int | None:
    """The number RTKLIB's str2enum (options.c) gives ``value`` from a comment like "0:off,1:on": the digits before
    the ":" that comes before the first match of ``value`` that has them, or a number written as such; None when
    RTKLIB refuses the value (and keeps its default)."""
    start = 0
    while (i := comment.find(value, start)) >= 0:
        start = i + 1
        digits = re.search(r"(\d+):$", comment[:i])
        if digits:
            return int(digits.group(1))
    i = comment.find(value[:30] + ":")
    number = re.match(r"\d+", comment[i:]) if i >= 0 else None
    return int(number.group()) if number else None


def test_every_value_config_writes_is_one_rtklib_takes():
    # RTKLIB skips an option it does not know and keeps its default for a value it cannot read, with only a message
    # on stderr: every line of every kind of options file must be taken as meant
    table = _rtklib_options_table()
    assert len(table) > 100 and ("pos1-posmode", 3, "0:single,1:dgps,2:kinematic,3:static,4:static-start,5:movingbase,"
                                 "6:fixed,7:ppp-kine,8:ppp-static,9:ppp-fixed") in table
    texts = [ppk.config(None), ppk.config((35.16, 139.61, 70.0)), ppk.config((-33.9, 151.2, -10.5), elevation_mask=0.0),
             ppk.config(None, elevation_mask=89.5), ppk.config((35.16, 139.61, 70.0), interpolate_base=True)]
    texts += [ppk.config((45.0, 11.0, 50.0), mode=mode) for mode in ppk.MODES]
    texts += [ppk.config((45.0, 359.9, 100.0), systems=systems) for systems in ("all", "G", "GRECJIS", ["c", "G"],
                                                                                "JIS", "E,R")]
    texts += [ppk.config(None, antenna_file="/work/ppk/rtklib_inputs/antennas.atx"),
              ppk.config((45.0, 11.0, 50.0), antenna_file="/Users/Enzo Cocca/Città/antennas.atx", rover_antenna="*"),
              ppk.config(None, antenna_file="C:\\Users\\enzo\\ppk\\antennas.atx", rover_antenna="TRM57971.00     NONE")]
    texts += [ppk.config((45.0, 11.0, 50.0), mode="static", rover_antenna="*", solution="forward", static_single=True),
              ppk.config(None, mode="static-start", solution="backward", static_single=True)]
    seen = set()
    for text in texts:
        for name, value in _options(text).items():
            # RTKLIB takes the first option whose name holds the one written (searchopt): it must be that one
            option = next(entry for entry in table if name in entry[0])
            assert option[0] == name, (name, option[0])
            kind, comment = option[1], option[2]
            if kind == 3:
                labels = {label: int(number) for number, label in (item.split(":", 1) for item in comment.split(","))}
                assert value in labels and _rtklib_enum(value, comment) == labels[value], (name, value, comment)
            elif kind == 0:
                bits = sum(int(n) for n in re.findall(r"(\d+):", comment)) if ":" in comment else None
                assert re.fullmatch(r"\d+", value) and (bits is None or int(value) & ~bits == 0), (name, value)
            elif kind == 1:
                assert math.isfinite(float(value)), (name, value)
            seen.add((name, value))
    assert ("pos1-posmode", "static-start") in seen and ("ant2-postype", "rinexhead") in seen
    assert ("pos1-navsys", "127") in seen and ("ant2-anttype", "*") in seen and ("misc-timeinterp", "on") in seen
    assert ("pos1-posopt2", "on") in seen and ("ant1-anttype", "*") in seen
    assert ("file-rcvantfile", "/work/ppk/rtklib_inputs/antennas.atx") in seen
    assert ("out-solstatic", "single") in seen and ("pos1-soltype", "forward") in seen
    assert ("pos1-soltype", "backward") in seen and ("pos1-soltype", "combined") in seen
    # the reader of the test refuses what RTKLIB refuses, and does not take a label for a longer one
    assert _rtklib_enum("kinematik", "0:single,1:dgps,2:kinematic") is None
    assert _rtklib_enum("combined", "0:forward,1:backward,2:combined,3:combined-nophasereset") == 2


# --- solution files ---------------------------------------------------------------------------------------------

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
    path.write_text("\n".join(lines) + "\n", newline="\n")
    _check_rows(ppk.read_pos(path))


def test_read_pos_of_a_file_that_does_not_say_its_heights(tmp_path):
    # RTKLIB leaves out the legend with out-outhead=off: the heights are taken as RTKLIB's default, said in a warning
    path = tmp_path / "rover.pos"
    path.write_text(_pos_text(legend=None), newline="")
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


def test_static_position_reads_the_one_position(tmp_path):
    """The one position of a single static solution: latitude, longitude, height, its quality and its deviations east,
    north, up (RTKLIB writes sdn, sde, sdu); a file of several positions is no single static solution."""
    single = tmp_path / "single.pos"
    single.write_text(_pos_text([POS_ROWS[1]]), newline="")
    (lat, lon, h), q, sd = ppk.static_position(single)
    assert (lat, lon, h) == pytest.approx(POS_ROWS[1][1:4], abs=1e-10) and q == 1
    assert sd == pytest.approx((0.02, 0.01, 0.03), abs=1e-12)
    assert all(type(v) is float for v in (lat, lon, h, *sd)) and type(q) is int
    several = tmp_path / "all.pos"
    several.write_text(_pos_text(), newline="")
    with pytest.raises(ValueError, match="all.pos: 3 positions"):
        ppk.static_position(several)
    empty = tmp_path / "empty.pos"
    empty.write_text(_pos_text([]), newline="")
    with pytest.raises(ValueError, match="nothing was computed"):
        ppk.static_position(empty)


@pytest.mark.parametrize("field, written", [("  0.0100", "     nan"), ("   75.6841", "      -nan"),
                                            ("  0.0300", "     inf"), ("518400.000", "nan")],
                         ids=["sdn nan", "height -nan", "sdu inf", "time nan"])
def test_read_pos_refuses_a_row_with_a_number_that_is_not_finite(tmp_path, field, written):
    # C's printf writes a number that is not one as nan, -nan or inf: never the position or accuracy of a camera
    text = _pos_text()
    assert text.count(field) == 1  # in the row at 00:00:00, the 7th line of the file
    path = tmp_path / "rover.pos"
    path.write_text(text.replace(field, written), newline="")
    with pytest.raises(ValueError, match=r"rover\.pos, line 7: a number that is not finite"):
        ppk.read_pos(path)


# --- solve: the checks before RTKLIB runs, the files it is given ------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _user_files(folder: Path) -> dict[str, bytes]:
    """Every file under ``folder`` and its bytes."""
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


RUN_NAME = r"solution-\d{8}-\d{6}(?:-\d+)?"  # the name of the files of a run: its time, a number after it if taken


def _run_files(run: dict) -> tuple[str, str]:
    """The names of the options file and of the solution a run of the fake rnx2rtkp was given: the same name of the
    run, .conf and .pos."""
    conf, out = Path(run["args"][1]).name, Path(run["args"][3]).name
    assert re.fullmatch(RUN_NAME + r"\.conf", conf) and out == conf[:-len(".conf")] + ".pos", (conf, out)
    return conf, out


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
    copies = work.resolve() / ppk.INPUTS_FOLDER
    expected = [str(copies / name) for name in ("rover.obs", "base.obs", "nav1.nav")]
    conf, out = _run_files(run)
    assert run["args"] == ["-k", str(work.resolve() / conf), "-o", str(work.resolve() / out), *expected]
    assert run["folder"] == ["base.obs", "nav1.nav", "rover.obs"]  # the copies, nothing else
    # the copies RTKLIB read are the user's files, the base decompressed
    assert run["inputs"] == [_sha256(PPK_ROVER.read_bytes()), _sha256(PPK_BASE.read_bytes()),
                             _sha256(PPK_NAV.read_bytes())]
    # the base from its header is written as its position, as a typed one (RTKLIB 2.4.3 would add the antenna height
    # of the header twice with rinexhead)
    mark = list(rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz))
    options = _options(run["options"])
    assert options == _options(ppk.config(mark)) and options["ant2-postype"] == "llh"
    # the copies are gone, the options and the solution of the run stay; the user's folder is as it was
    assert sorted(p.name for p in work.iterdir()) == [conf, out]
    assert _user_files(user) == before
    # the solution: the rows of the file, where it is, the files of the run, the base from its header (longitude
    # first, as every position the program keeps) with the warning, the antenna of its header not calibrated
    _check_rows(solution)
    assert solution.path == work.resolve() / out
    assert solution.inputs == {"rover": str(rover.absolute()), "base": str(base.absolute()),
                               "navs": [str(nav.absolute())]}
    assert solution.base == {"name": "0759", "position": pytest.approx([mark[1], mark[0], mark[2]]),
                             "position_source": "header", "antenna": "TRM29659.00",
                             "antenna_delta_hen": [0.0, 0.0, 0.0], "file": "base è.05o.gz",
                             "antenna_calibration": None, "frame": "unknown", "epoch": None, "sd": None, "detail": {}}
    header, antenna = solution.warnings
    assert "0759" in header and "header" in header and "metres" in header
    assert f"latitude {mark[0]:.7f}, longitude {mark[1]:.7f}" in header
    assert antenna == ("the phase-centre offset of the antenna TRM29659.00 of the base 0759 is not applied: no "
                       "antenna calibrations were given to RTKLIB, so the heights can be a few centimetres too low, "
                       "more than 10 cm with some choke-ring antennas. The calibrations of the IGS (igs20.atx, 62 MB) "
                       "are downloaded once by the PPK window, with its option for the antenna of the base, or by "
                       "`ppg gnss atx --download`")


def test_solve_with_a_typed_base_position(tmp_path, fake_rtklib):
    # typed as latitude, longitude, height; given to RTKLIB so; kept as every position of the program: [lon, lat, h]
    work = tmp_path / "work"
    solution = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], (35.16, 139.61, 70.0), folder=work)
    options = _options(fake_rtklib()[0]["options"])
    assert options["ant2-postype"] == "llh"
    assert [float(options[f"ant2-pos{i}"]) for i in (1, 2, 3)] == [35.16, 139.61, 70.0]
    assert solution.base == {"name": "0759", "position": [139.61, 35.16, 70.0], "position_source": "typed",
                             "antenna": "TRM29659.00", "antenna_delta_hen": [0.0, 0.0, 0.0], "file": "07590920.05o",
                             "antenna_calibration": None, "frame": "unknown", "epoch": None, "sd": None, "detail": {}}
    (antenna,) = solution.warnings  # only the one of every run: the antenna of the base is not calibrated
    assert antenna.startswith("the phase-centre offset of the antenna TRM29659.00 of the base 0759 is not applied")
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
    typed = rinex.ecef_to_llh(rinex.header(base).approx_xyz)  # where its header puts it: no warning about the base
    solution = ppk.solve(rover, base, [PPK_NAV], typed, folder=tmp_path / "work")
    time, antenna = solution.warnings
    assert "rover.24o" in time and "UTC" in time
    assert antenna.startswith("the phase-centre offset of the antenna TRM57971.00 NONE of the base BASE")


def test_solve_when_rtklib_computes_nothing_or_fails(tmp_path, fake_rtklib, monkeypatch):
    work = tmp_path / "work"
    first = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)  # a solution from an earlier run is there
    earlier = sorted(p.name for p in work.iterdir())
    assert earlier == sorted([first.path.name, first.path.with_suffix(".conf").name])
    # exit status 0 and no solution: nothing was computed, and the earlier solution is not taken for this one
    monkeypatch.delenv("PPG_FAKE_POS")
    monkeypatch.setenv("PPG_FAKE_MESSAGE", "error : no obs data")
    with pytest.raises(ValueError, match="nothing was computed: error : no obs data"):
        ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)
    # a solution file without positions
    empty = tmp_path / "empty.pos"
    empty.write_text(_pos_text(rows=[]), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(empty))
    with pytest.raises(ValueError, match="nothing was computed: error : no obs data"):
        ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)
    # a full solution with an error message: the message is a warning (after the one of every run)
    monkeypatch.setenv("PPG_FAKE_POS", str(tmp_path / "tools" / "canned.pos"))
    monkeypatch.setenv("PPG_FAKE_MESSAGE", "error : no erp data")
    solution = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], (35.16, 139.61, 70.0), folder=work)
    _check_rows(solution)
    assert solution.warnings[1:] == ["RTKLIB: error : no erp data"] and "phase-centre" in solution.warnings[0]
    # RTKLIB stopped
    monkeypatch.setenv("PPG_FAKE_EXIT", "1")
    monkeypatch.setenv("PPG_FAKE_MESSAGE", "error : no sat ant pcv in igs.atx")
    with pytest.raises(RuntimeError, match="1.*no sat ant pcv"):
        ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)
    # every time, the copies of the inputs are gone and a run without a solution leaves nothing: the files of the two
    # runs that gave one stay, each under its own name
    assert len(fake_rtklib()) == 5 and solution.path != first.path
    assert sorted(p.name for p in work.iterdir()) == sorted(
        earlier + [solution.path.name, solution.path.with_suffix(".conf").name])


def test_solve_refuses_a_gzip_file_cut_short(tmp_path, fake_rtklib):
    packed = gzip.compress(PPK_BASE.read_bytes())
    base = tmp_path / "base.05o.gz"
    base.write_bytes(packed[:len(packed) * 2 // 3])  # a download that stopped: the epochs before the cut are read
    assert rinex.time_span(base)
    work = tmp_path / "work"
    with pytest.raises(ValueError, match="cut short"):
        ppk.solve(PPK_ROVER, base, [PPK_NAV], folder=work)
    assert fake_rtklib() == [] and list(work.iterdir()) == []  # no copy left behind, no options of a run


def _ignores_case(folder: Path) -> bool:
    """The file system of ``folder`` takes names in any letter case for the same file (macOS, Windows)."""
    probe = folder / "case_probe"
    probe.write_text("")
    try:
        return (folder / "CASE_PROBE").exists()
    finally:
        probe.unlink()


def test_solve_refuses_inputs_inside_the_work_folder(tmp_path, fake_rtklib):
    # the program writes and deletes files in the work folder: no input may be there, under any name
    work = tmp_path / "work"
    (work / "data").mkdir(parents=True)
    named_as_a_copy = work / "base.obs"  # a rover under the name of the base's copy
    nested_rover, nested_nav = work / "data" / "rover.obs", work / "data" / "nav.05n"
    for path, original in ((named_as_a_copy, PPK_ROVER), (nested_rover, PPK_ROVER), (nested_nav, PPK_NAV)):
        shutil.copyfile(original, path)
    for rover, nav, misplaced in ((named_as_a_copy, PPK_NAV, named_as_a_copy), (nested_rover, PPK_NAV, nested_rover),
                                  (PPK_ROVER, nested_nav, nested_nav)):
        with pytest.raises(ValueError, match="work folder") as error:
            ppk.solve(rover, PPK_BASE, [nav], folder=work)
        # what to do, not a choice the window and the command line do not give (they fix the work folder)
        assert str(misplaced) in str(error.value) and f"move it out of {work} first" in str(error.value)
    if _ignores_case(tmp_path):  # the work folder written in another letter case is the same folder
        with pytest.raises(ValueError, match="work folder"):
            ppk.solve(named_as_a_copy, PPK_BASE, [PPK_NAV], folder=tmp_path / "WORK")
    assert named_as_a_copy.read_bytes() == nested_rover.read_bytes() == PPK_ROVER.read_bytes()
    assert nested_nav.read_bytes() == PPK_NAV.read_bytes()
    assert fake_rtklib() == [] and sorted(p.name for p in work.iterdir()) == ["base.obs", "data"]


def test_solve_runs_rtklib_on_a_folder_of_its_copies_only(tmp_path, fake_rtklib):
    # what an earlier run (or anyone) left among the copies: RTKLIB matches the names of its inputs ignoring the case,
    # so a Rover.OBS beside rover.obs would be read as a second rover on a file system that tells them apart
    work = tmp_path / "work"
    copies = work / ppk.INPUTS_FOLDER
    (copies / "old").mkdir(parents=True)
    (copies / "Rover.OBS").write_bytes(PPK_BASE.read_bytes())
    (copies / "nav7.nav").write_bytes(PPK_NAV.read_bytes())
    (copies / "old" / "rover.obs").write_text("x")
    (work / "notes.txt").write_text("the work folder itself is not emptied")
    ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)
    assert fake_rtklib()[-1]["folder"] == ["base.obs", "nav1.nav", "rover.obs"]
    assert sorted(p.name for p in work.iterdir()) == sorted(["notes.txt", *_run_files(fake_rtklib()[-1])])
    if os.name != "nt":  # a link in place of the folder of the copies: the link goes, what it points to stays
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "keep.txt").write_text("not RTKLIB's")
        copies.symlink_to(elsewhere, target_is_directory=True)
        ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)
        assert fake_rtklib()[-1]["folder"] == ["base.obs", "nav1.nav", "rover.obs"]
        assert sorted(p.name for p in elsewhere.iterdir()) == ["keep.txt"] and not copies.exists()


def test_solve_records_the_antenna_height_of_the_base(tmp_path, fake_rtklib):
    """The antenna above the mark in the base's RINEX header, which RTKLIB adds to the position of the mark: in the
    base of the solution, so that it can be shown ("mark + 1.80 m from the RINEX header")."""
    raised = _raised_base(tmp_path, 1.8)
    for position in (None, (35.16, 139.61, 70.0)):
        solution = ppk.solve(PPK_ROVER, raised, [PPK_NAV], position, folder=tmp_path / "work")
        assert solution.base["antenna_delta_hen"] == [1.8, 0.0, 0.0]
        assert _options(fake_rtklib()[-1]["options"])["ant2-anttype"] == "*"  # RTKLIB adds it
    # a header without the line
    header = [h for h in V2_HEADER if not h.endswith("ANTENNA: DELTA H/E/N")]
    base = _rinex2(tmp_path / "base.24o", header)
    rover = _rinex2(tmp_path / "rover.24o", V2_HEADER)
    solution = ppk.solve(rover, base, [PPK_NAV], (45.0, 11.0, 50.0), folder=tmp_path / "work")
    assert solution.base["antenna_delta_hen"] is None


def test_solve_says_that_the_phase_centre_of_the_base_antenna_is_not_applied(tmp_path, fake_rtklib):
    """RTKLIB is given no antenna calibration: the offset of the phase centre of the base's antenna from its reference
    point is not applied, and every height is that much too low. Every run says so, naming the antenna of the base's
    header, or that the header names none."""
    rover = _rinex2(tmp_path / "rover.24o", V2_HEADER)
    for antenna, said in (("1234                TRM57971.00     NONE", "of the antenna TRM57971.00 NONE of the base"),
                          ("1234                LEIAR25.R4      LEIT", "of the antenna LEIAR25.R4 LEIT of the base"),
                          ("", "of the antenna of the base BASE is not applied (its RINEX header names no antenna "
                               "type)")):
        header = [_line(antenna, "ANT # / TYPE") if h.endswith("ANT # / TYPE") else h for h in V2_HEADER]
        base = _rinex2(tmp_path / "base.24o", header)
        typed = rinex.ecef_to_llh(rinex.header(base).approx_xyz)  # where its header puts it: no warning about that
        solution = ppk.solve(rover, base, [PPK_NAV], typed, folder=tmp_path / "work")
        (warning,) = solution.warnings
        assert said in warning and "heights can be a few centimetres too low" in warning, warning
        assert "choke-ring" in warning and "ppg gnss atx --download" in warning and "later version" not in warning
        assert solution.base["antenna"] == " ".join(antenna[20:].split())


# --- the calibration of the base's antenna ------------------------------------------------------------------------

ATX_EXCERPT = Path(__file__).parent / "data" / "gnss" / "igs20_excerpt.atx"  # see tests/data/gnss/README.md
CALIBRATED = "TRM29659.00     NONE"  # its entry in the excerpt is synthetic: 0.100 m up on G01 and G02


def _base_of(folder: Path, antenna: str) -> Path:
    """A RINEX 2 base whose header names ``antenna`` (its type and radome; "" for none) as its antenna."""
    named = _line("1234                " + antenna if antenna else "", "ANT # / TYPE")
    header = [named if h.endswith("ANT # / TYPE") else h for h in V2_HEADER]
    return _rinex2(folder / "base.24o", header)


def _solve_with(folder: Path, antenna: str, **kwargs) -> ppk.Solution:
    """``solve`` of a rover against a base whose header names ``antenna``, typed where that header puts it (no warning
    about its position)."""
    folder.mkdir(parents=True, exist_ok=True)
    rover = _rinex2(folder / "rover.24o", V2_HEADER)
    base = _base_of(folder, antenna)
    return ppk.solve(rover, base, [PPK_NAV], rinex.ecef_to_llh(rinex.header(base).approx_xyz), folder=folder / "work",
                     **kwargs)


def test_solve_gives_rtklib_the_calibration_of_the_antenna_of_the_base(tmp_path, fake_rtklib):
    user = tmp_path / "user"
    user.mkdir()
    atx = user / "antennas.ATX"  # whatever its name: RTKLIB reads a copy that is called what it needs
    shutil.copyfile(ATX_EXCERPT, atx)
    before = _user_files(user)
    work = tmp_path / "work"
    mark = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    solution = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], mark, folder=work, atx=atx)
    (run,) = fake_rtklib()
    copies = work.resolve() / ppk.INPUTS_FOLDER
    options = _options(run["options"])
    # the file of the receiver antennas is a copy beside the other copies, with the extension RTKLIB knows it by, and
    # its phase centre variations are on; the run is the one it was without a file in all else
    assert options["file-rcvantfile"] == str(copies / ppk.ANTENNA_COPY) and ppk.ANTENNA_COPY.endswith(".atx")
    assert options["pos1-posopt2"] == "on" and "ant1-anttype" not in options and "file-satantfile" not in options
    assert {k: v for k, v in options.items() if k not in ("file-rcvantfile", "pos1-posopt2")} == _options(
        ppk.config(mark))
    assert run["folder"] == sorted(["base.obs", "nav1.nav", "rover.obs", ppk.ANTENNA_COPY])
    assert run["antenna"] == _sha256(ATX_EXCERPT.read_bytes())  # RTKLIB read the user's file
    assert run["inputs"] == [_sha256(PPK_ROVER.read_bytes()), _sha256(PPK_BASE.read_bytes()),
                             _sha256(PPK_NAV.read_bytes())]  # and the antenna file is no input
    # the copies are gone, the user's folder is as it was
    assert not copies.exists() and _user_files(user) == before
    # the antenna of the base is in the excerpt: no warning of an offset that is not applied; the record of what was
    # given, in the base and so in every photo
    assert solution.warnings == []
    assert solution.base["antenna_calibration"] == {"file": "antennas.ATX", "entry": CALIBRATED,
                                                    "without_radome": False, "up_m": pytest.approx(0.1)}
    # without a file: the run as it was, the offset not applied, and nothing recorded
    plain = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], mark, folder=tmp_path / "plain", atx=None)
    options = _options(fake_rtklib()[-1]["options"])
    assert options == _options(ppk.config(mark)) and fake_rtklib()[-1]["antenna"] is None
    assert fake_rtklib()[-1]["folder"] == ["base.obs", "nav1.nav", "rover.obs"]
    assert plain.base["antenna_calibration"] is None
    (warning,) = plain.warnings
    assert "phase-centre offset of the antenna TRM29659.00 of the base 0759 is not applied" in warning


# header antenna, entry given to RTKLIB, whether it is the type without the radome, its L1 offset up (m), what the
# warning says (None: no warning)
CALIBRATION_CASES = [
    ("TRM29659.00     NONE", CALIBRATED, False, 0.1, None),
    ("TRM29659.00", CALIBRATED, False, 0.1, None),  # no radome in the header is no radome: NONE
    ("TRM29659.00     SCIS", CALIBRATED, True, 0.1,
     ["TRM29659.00 SCIS of the base BASE", "radome", "igs20.atx", "calibration of TRM29659.00 NONE", "millimetres"]),
    ("ASH700829.2     NONE", "ASH700829.2     SNOW", True, 0.06954,
     ["ASH700829.2 NONE of the base BASE", "radome", "calibration of ASH700829.2 SNOW"]),
    ("ASH700829.2", "ASH700829.2     SNOW", False, 0.06954,  # the only entry of the type has a dome the header omits
     ["ASH700829.2 of the base BASE", "header writes it", "calibration of ASH700829.2 SNOW"]),
    ("RM29659.00     NONE", CALIBRATED, False, 0.1,  # RTKLIB's search finds the words in the middle of a name
     ["RM29659.00 NONE of the base BASE", "header writes it", "calibration of TRM29659.00 NONE", "another antenna"]),
    ("LEIAR25.R4      LEIT", None, False, None,
     ["antenna LEIAR25.R4 LEIT of the base BASE is not applied", "not in igs20.atx", "no calibration",
      "heights can be a few centimetres too low", "choke-ring"]),
    ("", None, False, None,
     ["antenna of the base BASE is not applied (its RINEX header names no antenna type)", "igs20.atx",
      "heights can be a few centimetres too low", "choke-ring"]),
    # RTKLIB's search would land on the satellite BLOCK IIA, whose offsets (2.25 m up) are no receiver's: the file is
    # not given to it
    ("BLOCK IIA", None, False, None, ["antenna BLOCK IIA of the base BASE is not applied", "not in igs20.atx"]),
]


@pytest.mark.parametrize("antenna, entry, without_radome, up, said", CALIBRATION_CASES,
                         ids=[c[0] or "no antenna" for c in CALIBRATION_CASES])
def test_solve_says_which_calibration_it_gave_rtklib(tmp_path, fake_rtklib, antenna, entry, without_radome, up, said):
    """RTKLIB finds the calibration of an antenna by the words of its name, and takes the type without the radome when
    it does not find the radome: the program says which entry it gave RTKLIB, or that the antenna is not calibrated."""
    atx = tmp_path / "igs20.atx"
    shutil.copyfile(ATX_EXCERPT, atx)
    solution = _solve_with(tmp_path, antenna, atx=atx)
    assert solution.base["antenna_calibration"] == {"file": "igs20.atx", "entry": entry,
                                                    "without_radome": without_radome,
                                                    "up_m": pytest.approx(up) if up is not None else None}
    run = fake_rtklib()[-1]
    # RTKLIB is given the file when the antenna is in it; when it is not it would apply nothing, and the run is as it
    # was without a file
    assert (run["antenna"] is not None) == (entry is not None)
    assert ("file-rcvantfile" in _options(run["options"])) == (entry is not None)
    if said is None:
        assert solution.warnings == []
        return
    (warning,) = solution.warnings
    for text in said:
        assert text in warning, (text, warning)
    assert "later version" not in warning and " yet" not in warning


def test_solve_uses_the_calibrations_the_program_keeps_and_never_downloads(tmp_path, fake_rtklib, monkeypatch):
    gnss = tmp_path / "gnss"
    monkeypatch.setenv("PPG_GNSS_DIR", str(gnss))

    def no_network(*args, **kwargs):
        raise AssertionError("solve downloaded something")

    monkeypatch.setattr(download, "fetch", no_network)
    # nothing kept: the run of every version before
    plain = _solve_with(tmp_path / "a", CALIBRATED)
    assert fake_rtklib()[-1]["antenna"] is None and plain.base["antenna_calibration"] is None
    assert "no antenna calibrations were given to RTKLIB" in plain.warnings[0]
    assert "ppg gnss atx --download" in plain.warnings[0]  # how to have them
    # igs20.atx kept by the program: used, by default
    kept = gnss / "atx" / "igs20.atx"
    kept.parent.mkdir(parents=True)
    shutil.copyfile(ATX_EXCERPT, kept)
    year_old = time.time() - 400 * 86400.0  # even a copy that is a year old: asking for one is not a download
    os.utime(kept, (year_old, year_old))
    solution = _solve_with(tmp_path / "b", CALIBRATED)
    assert fake_rtklib()[-1]["antenna"] == _sha256(ATX_EXCERPT.read_bytes()) and solution.warnings == []
    assert solution.base["antenna_calibration"]["file"] == "igs20.atx"
    # an antenna that is not in it
    other = _solve_with(tmp_path / "c", "SEPCHOKE_B3E6   NONE")
    assert other.base["antenna_calibration"]["entry"] is None
    assert "not in igs20.atx" in other.warnings[0] and fake_rtklib()[-1]["antenna"] is None
    # None: not used
    none = _solve_with(tmp_path / "d", CALIBRATED, atx=None)
    assert fake_rtklib()[-1]["antenna"] is None and none.base["antenna_calibration"] is None
    # a copy that is not an ANTEX file (a page the hotel gave): the run goes on without it, and says so
    kept.write_text("<html><body>You are connected to the hotel wifi</body></html>")
    broken = _solve_with(tmp_path / "e", CALIBRATED)
    assert fake_rtklib()[-1]["antenna"] is None and broken.base["antenna_calibration"] is None
    assert len(broken.warnings) == 2 and "igs20.atx" in broken.warnings[0] and "ANTEX" in broken.warnings[0]
    assert "phase-centre offset of the antenna TRM29659.00" in broken.warnings[1]


def test_solve_refuses_an_antenna_file_it_cannot_give_rtklib(tmp_path, fake_rtklib):
    rover = _rinex2(tmp_path / "rover.24o", V2_HEADER)
    base = _rinex2(tmp_path / "base.24o", V2_HEADER)
    typed = rinex.ecef_to_llh(rinex.header(base).approx_xyz)
    html = tmp_path / "page.atx"
    html.write_text("<html><body>You are connected to the hotel wifi</body></html>")
    cut = tmp_path / "cut.atx"
    cut.write_bytes(ATX_EXCERPT.read_bytes()[:-100])
    for given, match in ((tmp_path / "nothing.atx", "nothing.atx.*not a file"), (tmp_path, "not a file"),
                         (html, "page.atx.*ANTEX"), (cut, "cut.atx.*cut short")):
        with pytest.raises(ValueError, match=match):
            ppk.solve(rover, base, [PPK_NAV], typed, folder=tmp_path / "work", atx=given)
    assert fake_rtklib() == []  # before RTKLIB ran
    # the path RTKLIB would be given has to be one it reads back as it is: a "#" is the start of a comment in its
    # options file. A file the user gave is refused; the one the program keeps is left out, with a note, as the
    # run without it was a good run
    hashed = tmp_path / "Scavo #3" / "ppk"
    with pytest.raises(ValueError, match="#"):
        ppk.solve(rover, base, [PPK_NAV], typed, folder=hashed, atx=ATX_EXCERPT)
    assert fake_rtklib() == []


def test_solve_leaves_out_the_calibrations_it_keeps_when_rtklib_cannot_read_their_path(tmp_path, fake_rtklib,
                                                                                        monkeypatch):
    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))
    kept = tmp_path / "gnss" / "atx" / "igs20.atx"
    kept.parent.mkdir(parents=True)
    shutil.copyfile(ATX_EXCERPT, kept)
    rover = _rinex2(tmp_path / "rover.24o", V2_HEADER)
    base = _base_of(tmp_path, CALIBRATED)
    typed = rinex.ecef_to_llh(rinex.header(base).approx_xyz)
    solution = ppk.solve(rover, base, [PPK_NAV], typed, folder=tmp_path / "Scavo #3" / "ppk")
    run = fake_rtklib()[-1]
    assert run["antenna"] is None and "file-rcvantfile" not in _options(run["options"])
    assert solution.base["antenna_calibration"] is None
    assert len(solution.warnings) == 2 and "#" in solution.warnings[0] and "igs20.atx" in solution.warnings[0]
    assert "phase-centre offset of the antenna TRM29659.00" in solution.warnings[1]
    # a work folder whose path RTKLIB reads as it is: the calibrations are used
    ok = ppk.solve(rover, base, [PPK_NAV], typed, folder=tmp_path / "Scavo 3" / "ppk")
    assert fake_rtklib()[-1]["antenna"] == _sha256(ATX_EXCERPT.read_bytes()) and ok.warnings == []


def test_solve_with_the_options_of_a_static_run(tmp_path, fake_rtklib):
    """The rover standing still, solved forward and written as one position, its antenna from its header: the options
    RTKLIB is given are those of ``config``; options it cannot take are refused before anything is written."""
    work = tmp_path / "work"
    for wrong, match in ((dict(solution="smoothed"), "solution"), (dict(static_single=True), "static"),
                         (dict(mode="ppp-static"), "mode")):
        with pytest.raises(ValueError, match=match):
            ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work, **wrong)
    assert fake_rtklib() == [] and not work.exists()
    mark = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    static = dict(mode="static", rover_antenna="*", solution="forward", static_single=True)
    solution = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], mark, folder=work, **static)
    assert _options(fake_rtklib()[-1]["options"]) == _options(ppk.config(mark, **static))
    assert solution.rover_calibration is None  # no file of antenna calibrations was given


def _rover_of(folder: Path, antenna: str) -> Path:
    """A RINEX 2 rover whose header names ``antenna`` (its type and radome) as its antenna."""
    named = _line("5678                " + antenna, "ANT # / TYPE")
    return _rinex2(folder / "rover.24o", [named if h.endswith("ANT # / TYPE") else h for h in V2_HEADER])


# the antenna of the rover's header, the antenna of the base's header, whether RTKLIB is given the file, the entries it
# takes for the rover and for the base
ROVER_CALIBRATION_CASES = [
    ("TRM57971.00     NONE", CALIBRATED, True, "TRM57971.00     NONE", CALIBRATED),
    ("TRM57971.00     NONE", "LEIAR25.R4      LEIT", True, "TRM57971.00     NONE", None),  # for the rover alone
    ("LEIAR25.R4      LEIT", CALIBRATED, True, None, CALIBRATED),
    ("LEIAR25.R4      LEIT", "LEIAR25.R4      LEIT", False, None, None),
]


@pytest.mark.parametrize("rover_antenna, base_antenna, given, rover_entry, base_entry", ROVER_CALIBRATION_CASES)
def test_solve_calibrates_the_antenna_of_the_rover_when_asked(tmp_path, fake_rtklib, rover_antenna, base_antenna,
                                                               given, rover_entry, base_entry):
    """With the antenna of the rover from its header (``rover_antenna="*"``: a receiver standing on a mark, as a base
    placed by a static baseline) the antenna calibrations are given to RTKLIB when either antenna is in them, and the
    solution says which entry RTKLIB took for each."""
    atx = tmp_path / "igs20.atx"
    shutil.copyfile(ATX_EXCERPT, atx)
    rover, base = _rover_of(tmp_path, rover_antenna), _base_of(tmp_path, base_antenna)
    typed = rinex.ecef_to_llh(rinex.header(base).approx_xyz)
    solution = ppk.solve(rover, base, [PPK_NAV], typed, folder=tmp_path / "work", atx=atx, mode="static",
                         rover_antenna="*")
    run = fake_rtklib()[-1]
    assert (run["antenna"] is not None) == given and ("file-rcvantfile" in _options(run["options"])) == given
    assert solution.rover_calibration["file"] == "igs20.atx" and solution.rover_calibration["entry"] == rover_entry
    assert solution.base["antenna_calibration"]["entry"] == base_entry
    # the rover's antenna not asked for: nothing said of it, the file given for the antenna of the base alone
    plain = ppk.solve(rover, base, [PPK_NAV], typed, folder=tmp_path / "plain", atx=atx, mode="static")
    assert plain.rover_calibration is None
    assert (fake_rtklib()[-1]["antenna"] is not None) == (base_entry is not None)


# the role of the receiver, the record of the calibration of its antenna, and what the warning says (None: nothing)
ROLE_CASES = [
    ("base", None, ["antenna TRM29659.00 NONE of the base BASE is not applied", "heights", "too low"]),
    ("station", None, ["antenna TRM29659.00 NONE of the station BASE is not applied", "no antenna calibrations were "
                       "given to RTKLIB", "the mark found for your base", "too low"]),
    ("your base", None, ["antenna TRM29659.00 NONE of your base BASE is not applied", "mark found", "too high"]),
    ("ppp", None, ["antenna TRM29659.00 NONE of your base BASE is not applied", "in a PPP", "height", "too high"]),
    ("station", dict(entry=None), ["of the station BASE", "not in igs20.atx", "for your base", "too low"]),
    ("your base", dict(entry=None), ["of your base BASE", "not in igs20.atx", "too high"]),
    ("ppp", dict(entry=None), ["of your base BASE", "not in igs20.atx", "in a PPP", "too high"]),
    ("station", dict(entry="TRM29659.00     SCIS", without_radome=True),
     ["TRM29659.00 NONE of the station BASE", "radome", "phase-centre offset can be a few millimetres off"]),
    ("ppp", dict(entry="TRM29659.00     SCIS", without_radome=True),
     ["TRM29659.00 NONE of your base BASE", "radome", "height found can be a few millimetres off", "in a PPP"]),
    ("your base", dict(entry=CALIBRATED), None),
]


@pytest.mark.parametrize("role, record, said", ROLE_CASES)
def test_antenna_warning_names_the_receiver_by_its_role(tmp_path, role, record, said):
    """The base of a PPK, the station of a static baseline, the user's base it places and the user's base of a PPP: the
    warning names the receiver as such and says which way an antenna that is not calibrated moves what is found -
    what is found from a base or a station too low, the mark of the user's base too high."""
    info = rinex.header(_base_of(tmp_path, CALIBRATED))
    calibration = None if record is None else {"file": "igs20.atx", "entry": None, "without_radome": False,
                                               "up_m": None, **record}
    warning = ppk.antenna_warning("BASE", info, calibration, role=role)
    if said is None:
        assert warning is None
        return
    for text in said:
        assert text in warning, (text, warning)
    assert "later version" not in warning
    with pytest.raises(ValueError, match="role"):
        ppk.antenna_warning("BASE", info, calibration, role="drone")


def test_solve_gives_no_calibrations_that_rtklib_would_take_from_a_satellite(tmp_path, fake_rtklib):
    """RTKLIB's search does not tell a satellite's antenna from a receiver's: an antenna of the rover that it would find
    in a satellite (BLOCK IIA, 2.25 m up) keeps the file from RTKLIB, also when the antenna of the base is in it."""
    atx = tmp_path / "igs20.atx"
    shutil.copyfile(ATX_EXCERPT, atx)
    rover, base = _rover_of(tmp_path, "BLOCK IIA"), _base_of(tmp_path, CALIBRATED)
    typed = rinex.ecef_to_llh(rinex.header(base).approx_xyz)
    solution = ppk.solve(rover, base, [PPK_NAV], typed, folder=tmp_path / "work", atx=atx, mode="static",
                         rover_antenna="*")
    run = fake_rtklib()[-1]
    assert run["antenna"] is None and "file-rcvantfile" not in _options(run["options"])
    assert solution.rover_calibration is None and solution.base["antenna_calibration"] is None
    left_out, base_note = solution.warnings
    assert "igs20.atx" in left_out and "satellite" in left_out and "BLOCK IIA" in left_out and "rover" in left_out
    assert "phase-centre offset of the antenna TRM29659.00 NONE of the base BASE" in base_note


@pytest.mark.skipif(os.name == "nt", reason="RTKLIB is given the short ASCII name of the folder on Windows")
def test_solve_writes_options_with_a_path_that_is_not_ascii(tmp_path, fake_rtklib):
    # the options file holds the path of the copy of the antenna file, as the system names it: a project folder with
    # letters of Italian is not a reason for the run to fail
    work = tmp_path / "Città di Roma" / "ppk"
    mark = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    solution = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], mark, folder=work, atx=ATX_EXCERPT)
    run = fake_rtklib()[-1]
    assert run["antenna"] == _sha256(ATX_EXCERPT.read_bytes()) and solution.warnings == []
    assert "Città di Roma" in _options(run["options"])["file-rcvantfile"]


def test_solve_writes_a_solution_file_per_run(tmp_path, fake_rtklib, monkeypatch):
    """Two flights of one chunk, each with its own run: each run writes its options and its solution under a name of
    its own - the time of the run, a number after it when a run of the same second took it - and the solutions of the
    earlier runs stay; every photo records the solution of its own run."""
    when = datetime(2026, 10, 26, 15, 30, 12)

    class Clock(datetime):  # every run in the same second
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(ppk, "datetime", Clock)
    work = tmp_path / "work"
    work.mkdir()
    assert [p.name for p in ppk._run_files(work)] == ["solution-20261026-153012.conf", "solution-20261026-153012.pos",
                                                       "solution-20261026-153012_events.pos"]
    (work / "solution-20261026-153012_events.pos").write_text("")  # RTKLIB's time marks of a run: taken too
    assert ppk._run_files(work)[1].name == "solution-20261026-153012-2.pos"
    (work / "solution-20261026-153012_events.pos").unlink()

    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    first_flight = _ppk_flight(tmp_path / "volo1", [(1, START_2005, (0, 0, 150)), (2, START_2005 + 30.0, (0, 0, 150))])
    second_flight = _ppk_flight(tmp_path / "volo2", [(1, START_2005 + 3540.0, (0, 0, 150)),
                                                     (2, START_2005 + 3570.0, (0, 0, 150))])
    ch = ppg.Document().add_chunk()
    ch.add_photos([first_flight, second_flight])
    runs = []
    for flight in (first_flight, second_flight):
        solution = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)
        assert gnss_apply.apply_ppk(ch, flight / "Timestamp.MRK", solution)["matched"] == 2
        runs.append(solution.path.name)
    assert runs == ["solution-20261026-153012.pos", "solution-20261026-153012-2.pos"]
    assert sorted(p.name for p in work.iterdir()) == sorted(runs + [Path(r).with_suffix(".conf").name for r in runs])
    for cam in ch.cameras:
        assert cam.meta["gnss"]["solution"] == runs[Path(cam.path).parent == second_flight.resolve()], cam.path
    assert [Path(call["args"][3]).name for call in fake_rtklib()] == runs


def test_solve_refuses_paths_rtklib_cannot_take(tmp_path, fake_rtklib, monkeypatch):
    """RTKLIB copies the paths of its files into buffers of 1024 bytes: a work folder where they would be 1000 bytes
    long or longer is refused before RTKLIB runs, saying what to do, and nothing is left in it."""
    assert ppk.MAX_PATH_BYTES == 1000
    if os.name != "nt":  # a path as long only where the system takes it
        deep = tmp_path
        while len(os.fsencode(str(deep))) < 970:
            deep = deep / ("p" * max(1, min(200, 970 - len(os.fsencode(str(deep))) - 1)))
        with pytest.raises(ValueError, match="bytes long, and it takes paths shorter than 1000 bytes: move the "
                                             "project") as error:
            ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=deep)
        assert str(deep.resolve()) in str(error.value)
        assert fake_rtklib() == [] and list(deep.iterdir()) == []
    # anywhere: the longest path RTKLIB is given is the one of the time marks of the rover beside the solution
    work = tmp_path / "work"
    longest = len(os.fsencode(str(work.resolve() / "solution-20261026-153012_events.pos")))
    monkeypatch.setattr(ppk, "MAX_PATH_BYTES", longest)
    with pytest.raises(ValueError, match="_events.pos, a path"):
        ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)
    assert fake_rtklib() == [] and list(work.iterdir()) == []
    monkeypatch.setattr(ppk, "MAX_PATH_BYTES", longest + 1)
    ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=work)
    assert len(fake_rtklib()) == 1


def test_the_work_folder_rtklib_is_given(tmp_path, monkeypatch):
    plain, accented = tmp_path / "work", tmp_path / "lavoro è"
    plain.mkdir(), accented.mkdir()
    # on macOS and Linux RTKLIB opens UTF-8 paths: any path but RTKLIB's keywords
    monkeypatch.setattr(ppk, "_on_windows", lambda: False)
    assert ppk._rtklib_folder(accented) == str(accented.resolve())
    for keyword in ("%r", "%b"):
        folder = tmp_path / f"100{keyword}"
        folder.mkdir()
        with pytest.raises(RuntimeError, match=f"move the project, where the PPK works, to a folder without {keyword}"):
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
    with pytest.raises(RuntimeError, match="move the project, where the PPK works, to a folder whose path has only "
                                           "plain ASCII letters"):
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
# a fixed station, every epoch is fixed on every build: the accurate checks are made on static runs. EPOCHS
# (gnss_helpers): the 120 epochs from 00:00:00 to 00:59:30 every 30 s, in both files.


def _rtklib_run(folder: Path, base_position=None, base: Path = PPK_BASE, mode: str = "static",
                antenna_file: Path | None = None) -> ppk.Solution:
    """RTKLIB run by the core with the options of ``config`` on the sample data (3040 the rover, 0759 the base), with
    the antenna calibrations of ``antenna_file`` if one is given."""
    folder.mkdir(parents=True, exist_ok=True)
    conf, out = folder / "ppk.conf", folder / "rover.pos"
    conf.write_text(ppk.config(base_position, mode=mode, antenna_file=str(antenna_file) if antenna_file else None))
    code, message = _native.ppk_solve(str(conf), [str(PPK_ROVER), str(base), str(PPK_NAV)], str(out))
    assert code == 0, message
    return ppk.read_pos(out)


def _raised_base(folder: Path, height: float) -> Path:
    """A copy of the base's observations with its antenna ``height`` above the mark in the header."""
    lines = PPK_BASE.read_text().splitlines(keepends=True)
    delta = _line(f"{height:14.4f}        0.0000        0.0000", "ANTENNA: DELTA H/E/N") + "\n"
    base = folder / "07590920.05o"
    base.write_text("".join(delta if line.rstrip().endswith("ANTENNA: DELTA H/E/N") else line for line in lines),
                    newline="\n")
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
    every epoch in GPS seconds, some of them fixed, the fixed ones where the static solution puts the station. The
    base at the position of its header, with the warning, or typed there, without it."""
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
        assert solution.base["position"] == pytest.approx([mark[1], mark[0], mark[2]])  # longitude first
        # the copies of the inputs are gone: the options and the solution of the run (RTKLIB writes the time marks of
        # the rover beside the solution)
        stem = solution.path.stem
        assert re.fullmatch(RUN_NAME, stem) and solution.path.suffix == ".pos"
        assert {p.name for p in solution.path.parent.iterdir()} - {f"{stem}_events.pos"} == {f"{stem}.conf",
                                                                                             f"{stem}.pos"}
        assert fixed.sum() >= 0.10 * EPOCHS  # 38 to 115 of 120 from build to build
        assert solution.base["antenna_delta_hen"] == [0.0, 0.0, 0.0]
        assert "phase-centre offset of the antenna TRM29659.00" in solution.warnings[-1]  # every run
        if name == "header":
            assert len(solution.warnings) == 2 and "0759" in solution.warnings[0] and "header" in solution.warnings[0]
        else:
            assert len(solution.warnings) == 1


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


@needs_rtklib
def test_calibrated_base_raises_the_heights(tmp_path, monkeypatch):
    """The excerpt puts the phase centre of the antenna of the base 0.100 m above its reference point (a synthetic
    entry for TRM29659.00): the base is higher by that much than the program thought it, and every height of the rover
    is higher by as much. RTKLIB, not a stand-in."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    # a static run: every epoch fixed in both, at the same place in the plane and 0.100 m higher
    plain = _rtklib_run(tmp_path / "static-plain")
    calibrated = _rtklib_run(tmp_path / "static-calibrated", antenna_file=ATX_EXCERPT)
    assert (plain.q == 1).all() and (calibrated.q == 1).all() and len(plain.times) == EPOCHS
    rise = calibrated.llh[:, 2] - plain.llh[:, 2]
    assert np.all(np.abs(rise - 0.100) < 0.001), (rise.min(), rise.max())
    assert np.all(np.abs(_enu(calibrated.llh, plain.llh[0])[:, :2] - _enu(plain.llh, plain.llh[0])[:, :2]) < 0.001)
    # solve, the rover moving as a drone: which epochs RTKLIB fixes changes with the calibration (a kinematic run
    # is a chain of decisions), so the heights of the epochs fixed in both runs are compared
    mark = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    without = ppk.solve(PPK_ROVER, PPK_BASE, base_position=mark, folder=tmp_path / "plain", atx=None)
    with_atx = ppk.solve(PPK_ROVER, PPK_BASE, base_position=mark, folder=tmp_path / "calibrated", atx=ATX_EXCERPT)
    assert np.allclose(with_atx.times, without.times, rtol=0, atol=0.01)
    both = (without.q == 1) & (with_atx.q == 1)
    assert both.sum() >= 8, ((without.q == 1).sum(), (with_atx.q == 1).sum())
    assert np.median(with_atx.llh[both, 2] - without.llh[both, 2]) == pytest.approx(0.100, abs=0.002)
    # the offset is not applied in one run and applied in the other, and the program says so
    assert len(without.warnings) == 1 and "phase-centre offset of the antenna TRM29659.00" in without.warnings[0]
    assert with_atx.warnings == [] and with_atx.base["antenna_calibration"]["entry"] == CALIBRATED


@needs_rtklib
def test_rtklib_takes_the_entry_the_search_says(tmp_path, monkeypatch):
    """What ``atx.lookup`` says RTKLIB uses is what RTKLIB uses: whichever way the header spells the antenna of the base
    (the radome of the file, no radome, a radome the file does not have, words of the name), the heights rise by the
    offset of the entry the program names. Real RTKLIB."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    mark = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    bases = tmp_path / "bases"
    bases.mkdir()
    lines = PPK_BASE.read_text().splitlines(keepends=True)
    without = ppk.solve(PPK_ROVER, PPK_BASE, base_position=mark, folder=tmp_path / "plain", atx=None)
    for i, (antenna, rise) in enumerate([("TRM29659.00     NONE", 0.1), ("TRM29659.00", 0.1),
                                         ("TRM29659.00     SCIS", 0.1),  # a radome the file does not have
                                         ("RM29659.00     NONE", 0.1),  # the words of a name, not the name
                                         ("TRM41249.00     NONE", 0.0)]):  # not in the file: not given to RTKLIB
        named = _line(f"{'':20}{antenna}", "ANT # / TYPE") + "\n"
        path = bases / f"b{i}.05o"
        text = "".join(named if line.rstrip().endswith("ANT # / TYPE") else line for line in lines)
        path.write_text(text, newline="\n")
        assert rinex.header(path).antenna == antenna.strip()
        solution = ppk.solve(PPK_ROVER, path, base_position=mark, folder=tmp_path / f"run{i}", atx=ATX_EXCERPT)
        entry = solution.base["antenna_calibration"]["entry"]
        assert (entry is not None) == (rise != 0.0), (antenna, entry)
        both = (without.q == 1) & (solution.q == 1)
        assert both.sum() >= 8, (antenna, both.sum())
        assert np.median(solution.llh[both, 2] - without.llh[both, 2]) == pytest.approx(rise, abs=0.002), antenna


# --- the position of the camera at every shot -------------------------------------------------------------------


def _solution(times, llh, q=None, sd=None) -> ppk.Solution:
    """A solution with the epochs ``times`` (seconds of GPS time) at ``llh`` (latitude, longitude, height), fix
    unless ``q`` says otherwise, with the standard deviations ``sd`` (east, north, up)."""
    count = len(times)
    return ppk.Solution(times=np.asarray(times, dtype=np.float64), llh=np.asarray(llh, dtype=np.float64),
                        q=np.asarray([1] * count if q is None else q), ns=np.full(count, 8),
                        sd=np.asarray([[0.003, 0.004, 0.009]] * count if sd is None else sd, dtype=np.float64),
                        base={}, path=Path("synthetic.pos"))


def test_at_takes_an_epoch_or_the_line_between_two_epochs_a_second_apart():
    # a drone flying east at 15 m/s recorded at 5 Hz (3 m between epochs), then two epochs a second apart
    east = math.degrees(15.0 / (6378137.0 * math.cos(math.radians(35.132))))  # degrees of longitude in a second
    seconds = np.array([0.0, 0.2, 0.4, 1.4])
    llh = np.c_[np.full(4, 35.132066), 139.624301 + east * seconds, 75.69 + 0.05 * seconds]
    asked = START_2005 + np.array([0.2, 1.4, 0.0, 0.4 + 3e-7, 0.1, 0.9, 0.2009])
    position, q, sd, ok = ppk.at(_solution(START_2005 + seconds, llh), asked)
    assert position.shape == sd.shape == (7, 3) and q.shape == ok.shape == (7,)
    assert ok.all() and q.tolist() == [1] * 7
    # an epoch, also with the float noise of a time of 8e8 s: the epoch as it is
    assert np.array_equal(position[:4], llh[[1, 3, 0, 2]])
    # half-way between two epochs 0.2 s apart, between two epochs a second apart, and 0.9 ms after an epoch: on the
    # line between the epochs around it, not on the epoch - 13.5 mm further east at 15 m/s
    expected = np.array([(llh[0] + llh[1]) / 2, (llh[2] + llh[3]) / 2, llh[1] + 0.0045 * (llh[2] - llh[1])])
    assert position[4:, :2] == pytest.approx(expected[:, :2], abs=1e-10)  # degrees: 1e-10 is 0.01 mm
    assert position[4:, 2] == pytest.approx(expected[:, 2], abs=1e-7)  # metres; a time of 8e8 s is a float to 1e-7 s
    assert _enu(position[6], llh[1])[0] == pytest.approx([0.0135, 0.0, 0.000045], abs=1e-5)
    assert sd == pytest.approx(np.array([[0.003, 0.004, 0.009]] * 7))


def test_at_leaves_out_the_gaps_and_the_times_outside_the_solution():
    times = START_2005 + np.array([0.0, 1.0, 2.0, 5.0, 6.0005, 7.01])
    llh = np.array([[35.0, 139.0, 70.0 + i] for i in range(6)])
    asked = START_2005 + np.array([3.5, -0.5, 7.5, 6.5, 7.0125, -0.0009, 7.0109, 2.0008, 4.9992, 0.5, 5.5, 1.9995])
    position, q, sd, ok = ppk.at(_solution(times, llh), asked)
    # in a gap of 3 s, before the first epoch, after the last, between two epochs 1.0095 s apart, 2.5 ms after the
    # last epoch: no position, not even the one of the nearest epoch
    assert ok.tolist() == [False] * 5 + [True] * 7
    assert np.isnan(position[:5]).all() and np.isnan(sd[:5]).all() and q[:5].tolist() == [0] * 5
    # within a millisecond of the first and of the last epoch, and of the epochs on the two sides of the gap: those
    # epochs, as no pair of epochs a second apart is around those times
    assert position[5:9].tolist() == llh[[0, 5, 2, 3]].tolist()
    # exactly a second apart, 1.0005 s apart (the times of a solution are written to the millisecond), and half a
    # millisecond before an epoch with a pair of epochs a second apart around it: on the line between them
    assert position[9] == pytest.approx([35.0, 139.0, 70.5], abs=1e-12)
    assert position[10] == pytest.approx([35.0, 139.0, 73.0 + 0.5 / 1.0005], abs=1e-6)  # times of 8e8 s: 1e-7 s
    assert position[11] == pytest.approx([35.0, 139.0, 71.9995], abs=1e-6)


def test_at_takes_the_worse_quality_and_the_larger_deviations_of_the_two_epochs():
    times = START_2005 + np.array([0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2])
    solution = _solution(times, [[35.0, 139.0, 70.0]] * 7, q=[1, 2, 5, 1, 3, 4, 6],
                         sd=[[0.01, 0.05, 0.02], [0.03, 0.01, 0.02], [0.30, 0.40, 0.90], [0.02, 0.02, 0.04],
                             [0.50, 0.50, 0.90], [0.20, 0.30, 0.40], [0.60, 0.10, 0.20]])
    _position, q, sd, ok = ppk.at(solution, START_2005 + np.array([0.1, 0.3, 0.5, 0.2, 0.9, 1.1]))
    # the worse is the worse status, not the larger Q: sbas (3) counts as single, dgps (4) as float, ppp (6) single
    assert ok.all() and q.tolist() == [2, 5, 5, 2, 3, 6]
    assert [ppk.status(v) for v in q] == ["float", "single", "single", "float", "single", "single"]
    assert sd == pytest.approx(np.array([[0.03, 0.05, 0.02], [0.30, 0.40, 0.90], [0.30, 0.40, 0.90],
                                         [0.03, 0.01, 0.02], [0.50, 0.50, 0.90], [0.60, 0.30, 0.40]]))


def test_at_gives_no_position_from_an_epoch_with_a_number_that_is_not_finite():
    times = START_2005 + np.array([0.0, 0.2, 0.4, 0.6, 0.8])
    llh = np.array([[35.0, 139.0, 70.0 + i] for i in range(5)])
    llh[2, 0] = np.nan  # a latitude
    sd = np.array([[0.003, 0.004, 0.009]] * 5)
    sd[4, 2] = np.inf  # a standard deviation
    position, q, deviations, ok = ppk.at(_solution(times, llh, sd=sd),
                                         START_2005 + np.array([0.4, 0.3, 0.5, 0.8, 0.7, 0.1, 0.6]))
    # on those epochs, or between them and their neighbours: no position; elsewhere as usual
    assert ok.tolist() == [False] * 5 + [True] * 2
    assert np.isnan(position[:5]).all() and np.isnan(deviations[:5]).all() and q[:5].tolist() == [0] * 5
    assert position[5] == pytest.approx([35.0, 139.0, 70.5], abs=1e-6) and position[6].tolist() == llh[3].tolist()


def test_at_goes_across_the_antimeridian_the_short_way():
    times = START_2005 + np.array([0.0, 1.0, 2.0])
    llh = [[-16.5, 179.9999990, 10.0], [-16.5, -179.9999990, 10.0], [-16.5, 179.9999990, 10.0]]
    position, _q, _sd, ok = ppk.at(_solution(times, llh), START_2005 + np.array([0.25, 0.75, 1.25, 1.75]))
    assert ok.all()
    # eastwards over 180 degrees, 2e-6 degrees of longitude in a second (not 360), then back westwards
    assert position[:, 1] == pytest.approx([179.9999995, -179.9999995, -179.9999995, 179.9999995], abs=1e-9)
    assert position[:, 0] == pytest.approx([-16.5] * 4) and position[:, 2] == pytest.approx([10.0] * 4)


def test_the_camera_below_an_antenna_by_the_antimeridian():
    # 330 mm east of an antenna 0.01 m west of the 180th meridian, 330 mm west of one 0.01 m east of it: the camera on
    # the other side, its longitude within [-180, 180), where PROJ puts it (lever_arm: (lat, lon, h) in, (lon, lat, h)
    # out)
    for lon, arm in ((179.9999999, (0, 330, 150)), (-179.9999999, (0, -330, 150))):
        camera = dji.lever_arm(-16.5, lon, 10.0, arm)
        assert -180.0 <= camera[0] < 180.0 and math.copysign(1.0, camera[0]) == -math.copysign(1.0, lon)
        assert camera[2] == pytest.approx(9.85, abs=1e-9)
        assert np.linalg.norm(_ecef(*camera) - _camera_ecef((-16.5, lon, 10.0), arm)) < 1e-3
    # elsewhere the longitude is as it was
    assert dji.lever_arm(41.134, 14.266, 94.35, (0, 0, 150)) == (14.266, 41.134, pytest.approx(94.2))


def _gps_shot(when: datetime) -> dji.MrkShot:
    """A shot taken at ``when`` in GPS time."""
    week, seconds = divmod((when - datetime(1980, 1, 6)).total_seconds(), WEEK)
    return dji.MrkShot(index=1, gps_week=int(week), gps_seconds=seconds, offset_ned_mm=(0.0, 0.0, 150.0), lat=35.0,
                       lon=139.0, ellh=70.0, std=(0.01, 0.01, 0.02), flag=50)


def test_the_utc_of_a_shot_takes_the_leap_seconds_of_its_day():
    utc = timezone.utc
    assert _gps_shot(datetime(2005, 4, 2)).utc == datetime(2005, 4, 1, 23, 59, 47, tzinfo=utc)  # 13 s in 2005
    assert _gps_shot(datetime(2024, 5, 12, 8, 30, 33)).utc == datetime(2024, 5, 12, 8, 30, 15, tzinfo=utc)
    # the leap second at the end of 2016: GPS time 17 s ahead before it, 18 s after it
    assert _gps_shot(datetime(2017, 1, 1, 0, 0, 10)).utc == datetime(2016, 12, 31, 23, 59, 53, tzinfo=utc)
    assert _gps_shot(datetime(2017, 1, 1, 0, 0, 18)).utc == datetime(2017, 1, 1, tzinfo=utc)


def _ecef(lon: float, lat: float, h: float) -> np.ndarray:
    return np.array(Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True).transform(lon, lat, h))


def _camera_ecef(antenna, arm) -> np.ndarray:
    """ECEF (m) of a camera ``arm`` (north, east, down in mm) from the antenna at ``antenna`` (latitude, longitude,
    height), along the axes of the horizon at the antenna and with PROJ: not with the formula of the program."""
    lat, lon = math.radians(antenna[0]), math.radians(antenna[1])
    north = np.array([-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)])
    east = np.array([-math.sin(lon), math.cos(lon), 0.0])
    up = np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])
    return _ecef(antenna[1], antenna[0], antenna[2]) + (arm[0] * north + arm[1] * east - arm[2] * up) / 1000.0


# shot, seconds after 00:00:00 GPS time, lever arm (north, east, down in mm)
_DRONE_SHOTS = [(1, -30.0, (-2, 11, 189)),  # before the first epoch: the base started later
                (2, 0.0, (120, -330, 189)),  # an epoch, fix
                (3, 30.0004, (0, 0, 150)),  # within a millisecond of an epoch, float
                (4, 75.0, (35, -40, 170)),  # between two epochs 30 s apart: a gap of the solution
                (5, 90.0, (-60, 25, 160)),  # dgps
                (6, 120.0, (80, 90, 140)),  # single
                (7, 150.4, (-15, 200, 180)),  # between two epochs a second apart
                (8, 3570.0, (10, -10, 175)),  # the last epoch
                (9, 3580.0, (5, 5, 150))]  # after the last epoch


def test_ppk_positions_of_the_cameras_at_their_shots(tmp_path, fake_rtklib, monkeypatch):
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    solution = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=tmp_path / "work")  # the base at its header position
    assert solution.rover == "30400920.05o" and solution.base["position_source"] == "header"
    shots = [(index, START_2005 + seconds, arm) for index, seconds, arm in _DRONE_SHOTS]
    folder = _ppk_flight(tmp_path / "100MEDIA", shots)
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])

    result = gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)

    assert {k: v for k, v in result.items() if k not in ("warnings", "baseline_km")} == {
        "matched": 6, "shots": 9, "photos": 9, "status": {"fix": 2, "float": 3, "single": 1},
        "unmatched": ["DJI_0001", "DJI_0004", "DJI_0009"], "shots_unmatched": [1, 4, 9], "kept_fix": 0}
    # the base 0759 (where its header puts it) from the median position of the drone in the solution: 3.3 km
    mark = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    median = np.median(solution.llh, axis=0)
    apart = Geod(ellps="WGS84").inv(mark[1], mark[0], median[1], median[0])[2] / 1000.0
    assert result["baseline_km"] == pytest.approx(apart, rel=0.01) and 3.0 < apart < 3.6
    # the warnings of the solution (the base at the position of its header, its antenna not calibrated), then the one
    # of the photos in the gaps of the solution; none of a base far from the flight
    assert result["warnings"][:-1] == solution.warnings and "header" in result["warnings"][0]
    assert re.match(r"3 photos\b", result["warnings"][-1]) and "gap" in result["warnings"][-1]

    def epoch(seconds: float) -> np.ndarray:
        return solution.llh[int(np.argmin(np.abs(solution.times - (START_2005 + seconds))))]

    # the antenna of the solution at the shot (between two epochs a second apart: on the line between them), the
    # status of its quality Q (dgps is float; between two epochs the worse) and twice its standard deviations east,
    # north, up (the larger of the two epochs), at least 0.01 m for fix, 0.20 for float and 1 for single
    expected = {2: (epoch(0.0), "fix", 1, [0.01, 0.01, 0.018]),
                3: (epoch(30.0), "float", 2, [0.2, 0.2, 0.2]),
                5: (epoch(90.0), "float", 4, [0.5, 0.6, 1.0]),
                6: (epoch(120.0), "single", 5, [1.0, 1.0, 1.8]),
                7: (epoch(150.0) + 0.4 * (epoch(151.0) - epoch(150.0)), "float", 2, [0.2, 0.2, 0.24]),
                8: (epoch(3570.0), "fix", 1, [0.01, 0.01, 0.018])}
    arms = {index: arm for index, _seconds, arm in _DRONE_SHOTS}
    for index, (antenna, state, q, accuracy) in expected.items():
        cam = ch.camera(f"DJI_{index:04d}")
        # the camera below the antenna of the solution, not below the antenna of the MRK line (metres away)
        assert np.linalg.norm(_ecef(*cam.reference.location) - _camera_ecef(antenna, arms[index])) < 1e-3, index
        assert cam.reference.accuracy == pytest.approx(accuracy) and cam.reference.enabled
        assert cam.meta["reference_crs"] == "EPSG:4979"
        g = cam.meta["gnss"]
        assert (g["source"], g["status"], g["q"], g["height"], g["mrk"], g["shot"]) == (
            "ppk", state, q, "ellipsoidal", "Timestamp.MRK", index)
        assert g["accuracy"] == cam.reference.accuracy and g["lever_arm_ned_mm"] == list(arms[index])
        assert g["antenna"] == pytest.approx([antenna[1], antenna[0], antenna[2]], abs=1e-9)
        assert g["base"] == solution.base and (g["rover"], g["solution"]) == ("30400920.05o", solution.path.name)
        json.dumps(cam.meta)  # saved with the project
    # the time of the shot in UTC, as for an MRK: GPS time less the 13 leap seconds of 2005
    assert ch.camera("DJI_0002").meta["gnss"]["utc"] == "2005-04-01T23:59:47+00:00"
    assert ch.camera("DJI_0003").meta["gnss"]["utc"] == "2005-04-02T00:00:17.000400+00:00"
    # the photos in the gaps of the solution keep the position they had
    assert [ch.camera(f"DJI_{i:04d}").meta["gnss"]["source"] for i in (1, 4, 9)] == ["exif"] * 3
    # the run, for the dialog of the next one: its files, its base from the header (no typed position)
    assert ppk.last_run(ch) == {"mrk": str((folder / "Timestamp.MRK").absolute()), "photos": None,
                                "rover": str(PPK_ROVER.absolute()), "base": str(PPK_BASE.absolute()),
                                "navs": [str(PPK_NAV.absolute())], "base_name": "0759", "position_source": "header",
                                "position": None, "frame": "unknown", "epoch": None, "station": None,
                                "replace_fix": False, "solution": solution.path.name}
    json.dumps(ch.meta)

    again = _snapshot(ch)
    assert gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution) == result
    assert _snapshot(ch) == again


def test_ppk_of_an_mrk_outside_the_solution_changes_nothing(tmp_path):
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    solution = ppk.read_pos(pos)
    assert solution.rover == "" and solution.base == {}
    # the photos and the MRK of the next day
    folder = _ppk_flight(tmp_path / "100MEDIA", [(i, START_2005 + 86400.0 + 2.0 * i, (0, 0, 150)) for i in (1, 2, 3)])
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    before = _snapshot(ch)
    with pytest.raises(ValueError, match="same flight") as error:
        gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)
    text = str(error.value)
    assert "Timestamp.MRK" in text and "drone.pos" in text
    assert "2005-04-03 00:00:02" in text and "2005-04-02 00:00:00" in text and "2005-04-02 00:59:30" in text
    assert _snapshot(ch) == before
    # a solution without positions (a .pos file never gives one): the same refusal
    empty = _solution([], np.zeros((0, 3)), sd=np.zeros((0, 3)))
    with pytest.raises(ValueError, match="solution synthetic.pos has no positions: they are not of the same flight"):
        gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", empty)
    assert _snapshot(ch) == before


def test_ppk_warns_when_the_solution_is_far_from_the_drones_own_positions(tmp_path):
    # the observations of the base taken for the rover's, or those of another receiver over the same hours: the
    # solution puts the antenna far from where the drone's own receiver put it in the MRK, a warning says how far
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    solution = ppk.read_pos(pos)
    shots = [(i, START_2005 + 30.0 * i, (0, 0, 150)) for i in (1, 2, 3)]
    geod = Geod(ellps="WGS84")
    for name, lat in (("near", MRK_LAT), ("far", MRK_LAT + 0.001)):  # 2.5 m, then 113 m from the solution
        folder = _ppk_flight(tmp_path / name, shots, lat=lat)
        ch = ppg.Document().add_chunk()
        ch.add_photos([folder])
        result = gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)
        assert result["matched"] == 3  # the positions are given all the same
        apart = np.median([geod.inv(139.62428, lat, cam.meta["gnss"]["antenna"][0], cam.meta["gnss"]["antenna"][1])[2]
                           for cam in ch.cameras])
        if name == "near":
            assert apart < 3.0 and result["warnings"] == []
        else:
            (warning,) = result["warnings"]
            said = re.match(r"the solution drone\.pos puts the antenna (\d+) m \(median\) from where the drone's own "
                            r"receiver put it in Timestamp\.MRK", warning)
            assert said and abs(int(said.group(1)) - apart) <= 2.0 and "base" in warning, warning


def _onboard_flight(folder: Path, solution: ppk.Solution, epochs, fixes, north_m: float = 0.0, floats=()) -> Path:
    """A DJI flight with a shot on each of the ``epochs`` (indices) of ``solution``: its photos and its Timestamp.MRK,
    whose lines put the antenna where the solution does (``north_m`` metres to the north), with the RTK flag 50 (fix)
    for the shots in ``fixes``, 34 (float) for those in ``floats``, 16 (single) for the others."""
    shots = [(i + 1, float(solution.times[k]), (0, 0, 150)) for i, k in enumerate(epochs)]
    _ppk_flight(folder, shots)
    lines = []
    for (index, gps, (north, east, down)), k in zip(shots, epochs):
        week, seconds = divmod(gps, WEEK)
        lat, lon, h = solution.llh[k]
        lines.append(f"{index}\t{seconds:.6f}\t[{int(week)}]\t{north:6d},N\t{east:6d},E\t{down:6d},V\t"
                     f"{lat + north_m / 111195.0:.10f},Lat\t{lon:.10f},Lon\t{h:.4f},Ellh\t0.010, 0.010, 0.020\t"
                     f"{50 if index in fixes else 34 if index in floats else 16},Q\n")
    (folder / "Timestamp.MRK").write_text("".join(lines), newline="\n")
    return folder


def test_ppk_keeps_the_onboard_rtk_fixes(tmp_path):
    """The photos whose position is an RTK fix of the drone itself - the XMP of a DJI photo, a DJI MRK - keep it: the
    PPK is for the others. They are neither matched nor unmatched, the result counts them; kept far from the PPK
    positions of the same shots, a warning says what it likely means. With ``replace_fix`` they take the PPK position
    too, with a warning for those that are no longer fix."""
    from pyarchinit_photogrammetry.core.camera import Camera

    for source, status, onboard in [("dji_xmp", "fix", True), ("dji_mrk", "fix", True), ("dji_mrk", "float", False),
                                    ("dji_xmp", "single", False), ("ppk", "fix", False),
                                    ("positions_file", "fix", False), ("exif", None, False)]:
        cam = Camera(key=0, label="x", path="x.jpg", sensor_key=0, meta={"gnss": {"source": source, "status": status}})
        assert gnss_apply.onboard_fix(cam) is onboard, (source, status)

    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    solution = ppk.read_pos(pos)
    epochs = [0, 1, 2, 4, 120]  # fix, float, fix, single, fix (the 120th row is the last epoch, 3570 s)
    assert [ppk.status(solution.q[k]) for k in epochs] == ["fix", "float", "fix", "single", "fix"]

    def flight(name: str, north_m: float = 0.0):
        folder = _onboard_flight(tmp_path / name, solution, epochs, fixes={1, 2, 3}, north_m=north_m)
        ch = ppg.Document().add_chunk()
        ch.add_photos([folder])
        assert gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK")["status"] == {"fix": 3, "single": 2}
        return ch, folder

    # the onboard fixes where the PPK puts them: kept, no warning; the two single photos take the PPK
    ch, folder = flight("concordi")
    before = {c.label: c.meta["gnss"] for c in ch.cameras}
    result = gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)
    assert (result["matched"], result["kept_fix"], result["status"]) == (2, 3, {"single": 1, "fix": 1})
    assert (result["unmatched"], result["shots_unmatched"], result["warnings"]) == ([], [], [])
    sources = {c.label: c.meta["gnss"]["source"] for c in ch.cameras}
    assert sources == {"DJI_0001": "dji_mrk", "DJI_0002": "dji_mrk", "DJI_0003": "dji_mrk", "DJI_0004": "ppk",
                       "DJI_0005": "ppk"}
    assert all(ch.camera(f"DJI_{i:04d}").meta["gnss"] == before[f"DJI_{i:04d}"] for i in (1, 2, 3))
    # the onboard fixes 30 cm north of the PPK positions of the same shots: the base in another frame, or misplaced
    ch, folder = flight("discordi", north_m=0.30)
    result = gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)
    assert (result["matched"], result["kept_fix"]) == (2, 3)
    (warning,) = result["warnings"]
    assert warning.startswith("3 photos with an onboard RTK fix kept their positions, which are 30 cm (median) from "
                              "the PPK positions of the same shots: the base is likely in another reference frame")
    assert "or at a wrong position" in warning and "compute again replacing the onboard fixes too" in warning
    # 4 cm: within what two good solutions give
    ch, folder = flight("vicini", north_m=0.04)
    assert gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)["warnings"] == []
    # replace_fix: every photo takes the PPK position; one onboard fix is now float, and a warning says so
    ch, folder = flight("sostituiti", north_m=0.30)
    result = gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution, replace_fix=True)
    assert (result["matched"], result["kept_fix"], result["status"]) == (5, 0, {"fix": 3, "float": 1, "single": 1})
    assert result["warnings"] == ["1 photo had an onboard RTK fix and now has a PPK position that is not fix (float "
                                  "or single)"]
    assert {c.meta["gnss"]["source"] for c in ch.cameras} == {"ppk"} and ppk.last_run(ch)["replace_fix"] is True
    # the XMP of a DJI photo: its altitude may be above the sea level, so only the horizontal distance counts
    ch, folder = flight("xmp")
    for cam in ch.cameras:
        if gnss_apply.onboard_fix(cam):
            lon, lat, h = cam.reference.location
            gnss_apply.set_camera_position(cam, [lon, lat, h - 45.0], "EPSG:4326", [0.02, 0.02, 0.04], "dji_xmp",
                                           status="fix", extra={"flag": 50})
    result = gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)
    assert (result["kept_fix"], result["warnings"]) == (3, [])
    # a flight of onboard fixes only: nothing changes, nothing is recorded
    ch, folder = flight("solo_fix")
    for cam in ch.cameras:
        cam.meta["gnss"]["status"] = "fix"
    before = _snapshot(ch)
    result = gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)
    assert (result["matched"], result["kept_fix"], result["unmatched"]) == (0, 5, [])
    assert _snapshot(ch) == before and ppk.last_run(ch) is None


def test_ppk_warns_of_the_photos_it_gives_a_worse_state_than_the_drone_did(tmp_path):
    """The PPK is for the photos that are not fix, and it can do worse than the drone: a photo whose onboard state (the
    XMP of a DJI photo, a DJI MRK) is float and whose PPK state is single is counted in a warning, and takes the PPK
    position all the same. A PPK state as good or better, a position that is not the drone's own and an onboard fix
    (it has its own warning) are not counted in it."""
    rows = _drone_rows()
    rows[5] = (*rows[5][:4], 5, *rows[5][5:])  # the sixth epoch is single as the fifth is
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=rows), newline="")
    solution = ppk.read_pos(pos)
    epochs = [0, 1, 2, 4, 5, 120]
    assert [ppk.status(solution.q[k]) for k in epochs] == ["fix", "float", "fix", "single", "single", "fix"]

    def flight(name: str, floats, fixes=(1,)):
        """Six photos with the position of an MRK: an onboard fix for those in ``fixes``, a float for those in
        ``floats``, a single for the others."""
        folder = _onboard_flight(tmp_path / name, solution, epochs, fixes=set(fixes), floats=set(floats))
        ch = ppg.Document().add_chunk()
        ch.add_photos([folder])
        gnss_apply.apply_mrk(ch, folder / "Timestamp.MRK")
        assert [gnss_apply.onboard_state(c) for c in ch.cameras] == [
            "fix" if i in fixes else "float" if i in floats else "single" for i in range(1, 7)]
        return ch, folder

    def worse(result: dict) -> list[str]:
        return [w for w in result["warnings"] if "worse state" in w]

    # the PPK is single where the drone had a float (photos 4 and 5), float where it had a float (2), fix where it had
    # a float (6) or a single (3): two photos worse
    ch, folder = flight("volo", floats={2, 4, 5, 6})
    result = gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)
    assert (result["matched"], result["kept_fix"]) == (5, 1)
    assert result["status"] == {"fix": 2, "float": 1, "single": 2}
    (warning,) = result["warnings"]
    assert warning == ("2 photos got a worse state from the PPK than from the drone's own RTK (float onboard, single "
                       "in the PPK): their PPK positions are used all the same. The base may be too far from the "
                       "flight or share too few satellites with the drone: check its distance and its observations")
    # the PPK position is the one they have, whatever the warning says of them
    assert [(c.meta["gnss"]["source"], c.meta["gnss"]["status"]) for c in ch.cameras] == [
        ("dji_mrk", "fix"), *(("ppk", state) for state in ("float", "fix", "single", "single", "fix"))]
    # the same run again: the photos are the PPK's now, and the warning is not repeated
    assert worse(gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)) == []

    # one photo only, in the singular: the float of photo 4 is the XMP of a DJI photo, the one of photo 5 comes from a
    # file of positions, not from the drone, and is not counted
    ch, folder = flight("uno", floats={4, 5})
    xmp = ch.camera("DJI_0004")
    gnss_apply.set_camera_position(xmp, list(xmp.reference.location), "EPSG:4326", [0.2, 0.2, 0.2], dji.SOURCE_XMP,
                                   status="float")
    other = ch.camera("DJI_0005")
    gnss_apply.set_camera_position(other, list(other.reference.location), "EPSG:4979", [0.2, 0.2, 0.2],
                                   "positions_file", status="float", height="ellipsoidal")
    assert (gnss_apply.onboard_state(xmp), gnss_apply.onboard_state(other)) == ("float", None)
    (warning,) = worse(gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution))
    assert warning.startswith("1 photo got a worse state from the PPK than from the drone's own RTK (float onboard, "
                              "single in the PPK): its PPK position is used all the same.")
    assert (xmp.meta["gnss"]["source"], xmp.meta["gnss"]["status"]) == ("ppk", "single")
    assert (other.meta["gnss"]["source"], other.meta["gnss"]["status"]) == ("ppk", "single")

    # a PPK as good as the drone, or better: nothing worse
    ch, folder = flight("pari", floats={2, 6})
    assert gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)["warnings"] == []
    # replace_fix: the onboard fix of photo 2 is float now, which has its own warning; the floats of photos 4 and 5 are
    # single, which has this one - each photo is counted once
    ch, folder = flight("sostituiti", floats={4, 5}, fixes={1, 2})
    result = gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution, replace_fix=True)
    assert (result["matched"], result["kept_fix"]) == (6, 0)
    first, second = result["warnings"]
    assert first == "1 photo had an onboard RTK fix and now has a PPK position that is not fix (float or single)"
    assert second.startswith("2 photos got a worse state from the PPK than from the drone's own RTK")


def test_ppk_says_how_far_the_base_is_from_the_flight(tmp_path):
    """The baseline: the horizontal distance from the base to the median position of the drone in the solution, in
    the result; beyond 20 km a warning (the ambiguities are hard to fix, the positions mostly float)."""
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    solution = ppk.read_pos(pos)
    assert ppk.baseline_km(solution) is None  # a solution read from a file knows no base
    folder = _ppk_flight(tmp_path / "volo", [(i, START_2005 + 30.0 * i, (0, 0, 150)) for i in (1, 2, 3)])
    lat, lon = np.median(solution.llh[:, 0]), np.median(solution.llh[:, 1])
    geod = Geod(ellps="WGS84")
    for km in (3.0, 25.0):
        lon_b, lat_b, _back = geod.fwd(lon, lat, 90.0, km * 1000.0)  # due east
        solution.base = {"name": "STAZ", "position": [lon_b, lat_b, 60.0], "position_source": "typed"}
        ch = ppg.Document().add_chunk()
        ch.add_photos([folder])
        result = gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)
        assert result["baseline_km"] == pytest.approx(km, rel=0.005)
        far = [w for w in result["warnings"] if "from the flight" in w]
        if km < 20.0:
            assert far == []
        else:
            (warning,) = far
            said = re.fullmatch(r"the base STAZ is (\d+\.\d) km from the flight: beyond 20 km the ambiguities are hard "
                                r"to fix, and the positions are mostly float", warning)
            assert said and float(said.group(1)) == pytest.approx(result["baseline_km"], abs=0.05), warning
    # across the 180th meridian the short way
    across = _solution([0.0, 1.0], [[-16.5, 179.99, 10.0], [-16.5, 179.99, 10.0]])
    across.base = {"position": [-179.99, -16.5, 10.0]}
    assert ppk.baseline_km(across) == pytest.approx(2.135, abs=0.01)


def test_ppk_says_when_one_base_was_typed_at_two_positions(tmp_path):
    """The same base typed at two positions more than 5 cm apart in one chunk: one of them is likely mistyped. A
    warning when the photos of another flight were computed against the other position; none when the run gives its
    photos their positions again, or when the two are within 5 cm."""
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    solution = ppk.read_pos(pos)
    first = _ppk_flight(tmp_path / "volo1", [(i, START_2005 + 30.0 * i, (0, 0, 150)) for i in (1, 2)])
    second = _ppk_flight(tmp_path / "volo2", [(i, START_2005 + 30.0 * i + 600.0, (0, 0, 150)) for i in (1, 2)])
    ch = ppg.Document().add_chunk()
    ch.add_photos([first, second])
    here = [139.6138, 35.1609, 71.25]

    def run(folder: Path, position) -> list[str]:
        solution.base = {"name": "0759", "position": list(position), "position_source": "typed"}
        return [w for w in gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)["warnings"] if "typed" in w]

    assert run(first, here) == []
    assert run(first, [here[0], here[1], here[2] + 0.30]) == []  # the same flight again: its photos are replaced
    assert run(second, [here[0], here[1], here[2] + 0.33]) == []  # 3 cm from the first flight's
    (warning,) = run(second, [here[0], here[1], here[2] + 0.10])  # 20 cm
    assert warning == ("the base 0759 was typed at another position for other photos of this chunk (latitude "
                       "35.16090000, longitude 139.61380000, 71.550 m), 20 cm from this one: one of the two is likely "
                       "mistyped; compute again the flights that used the wrong one")
    assert ppk.other_typed_position(ch, "0759", [here[0], here[1], here[2] + 0.30]) == (
        [pytest.approx(v) for v in [here[0], here[1], here[2] + 0.10]], pytest.approx(0.20))
    assert ppk.other_typed_position(ch, "ALTRA", here) is None  # another base


def test_one_table_of_rtklib_states_for_ppk_and_positions_files(tmp_path):
    # RTKLIB's quality Q: 1 fix, 2 float, 4 dgps counted as float, any other single - for a PPK solution and for the Q
    # column of a positions file alike, as numbers and as RTKLIB's names of them
    expected = {0: "single", 1: "fix", 2: "float", 3: "single", 4: "float", 5: "single", 6: "single", 7: "single"}
    assert {q: ppk.status(q) for q in expected} == expected
    path = tmp_path / "positions.csv"
    path.write_text("label,x,y,z,q\n" + "".join(f"{q}.jpg,1,2,3,{q}\n" for q in range(1, 7))
                    + "".join(f"{name}.jpg,1,2,3,{name.upper()}\n" for name in ppk.QUALITY.values()), newline="\n")
    rows = positions.read_positions(path)
    assert [row["status"] for row in rows] == [expected[q] for q in range(1, 7)] + [expected[q] for q in ppk.QUALITY]
    # 0 in a positions file is DJI's RtkFlag 0, no RTK at all, as before
    path.write_text("label,x,y,z,q\na.jpg,1,2,3,0\n", newline="\n")
    assert positions.read_positions(path)[0]["status"] == "none"


@needs_rtklib
def test_ppk_positions_from_rtklib_at_the_shots(tmp_path, monkeypatch):
    """The solution RTKLIB computes from the sample data, with shots on its epochs and one between two of them: every
    camera where the solution puts the antenna plus the lever arm, with the status and accuracy of its Q and
    deviations."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    solution = ppk.solve(PPK_ROVER, PPK_BASE, folder=tmp_path / "work")
    epochs = [0, 7, 33, 64, 101, 119]
    shots = [(i + 1, float(solution.times[k]), (40 * i - 100, 150 - 60 * i, 140 + 10 * i))
             for i, k in enumerate(epochs)]
    shots.append((7, float(solution.times[50]) + 15.0, (0, 0, 150)))  # 15 s from the epochs around it
    folder = _ppk_flight(tmp_path / "100MEDIA", shots)
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])

    result = gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)

    assert (result["matched"], result["unmatched"], result["shots_unmatched"]) == (6, ["DJI_0007"], [7])
    assert result["warnings"][:-1] == solution.warnings and re.match(r"1 photo\b", result["warnings"][-1])
    for (index, _gps, arm), k in zip(shots, epochs):
        cam = ch.camera(f"DJI_{index:04d}")
        assert np.linalg.norm(_ecef(*cam.reference.location) - _camera_ecef(solution.llh[k], arm)) < 1e-3, index
        q = int(solution.q[k])
        state = {1: "fix", 2: "float", 4: "float"}.get(q, "single")
        assert (cam.meta["gnss"]["status"], cam.meta["gnss"]["q"]) == (state, q)
        floor = {"fix": 0.01, "float": 0.20, "single": 1.0}[state]
        assert cam.reference.accuracy == pytest.approx(np.maximum(2 * solution.sd[k], floor).tolist())


# --- ppg gnss ppk -----------------------------------------------------------------------------------------------


def _ppk_project(tmp_path: Path, shots=_DRONE_SHOTS) -> tuple[Path, Path]:
    """A saved project with the photos of a DJI flight on the day of the RTKLIB sample data and its Timestamp.MRK beside
    them: for each ``(index, seconds after 00:00:00 GPS time, lever arm)`` of ``shots`` a photo and its line."""
    folder = _ppk_flight(tmp_path / "100MEDIA", [(index, START_2005 + seconds, arm) for index, seconds, arm in shots])
    doc = ppg.Document()
    doc.add_chunk("Volo").add_photos([folder])
    doc.save(tmp_path / "volo.ppg")
    return tmp_path / "volo.ppg", folder


def _ppk_command(project: Path, folder: Path, *options: str) -> list[str]:
    return ["gnss", "ppk", str(project), "--mrk", str(folder / "Timestamp.MRK"), "--rover", str(PPK_ROVER),
            "--base", str(PPK_BASE), *options]


_TERMINAL_CODES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")  # the colors and styles a terminal is told (CSI sequences)


def _plain_words(output: str) -> str:
    """What a command printed, as plain words on one line. Typer prints a usage error in a box that wraps the message
    and, where GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set (GitHub's runners), in forced colors that cut
    ``--base-position`` into three styled pieces: the codes, the box and the line breaks are not what a test of the
    message is about."""
    return " ".join(re.sub("[\u2500-\u257f]", " ", _TERMINAL_CODES.sub("", output)).split())


def test_ppk_command_line(tmp_path, fake_rtklib, monkeypatch):
    """ppg gnss ppk with RTKLIB's solution of a drone (the fake rnx2rtkp: no native core needed): the counts per
    status, the base, the warnings - the base at the position of its header among them - and what took no position;
    the positions saved, the fix-only filter decided again, RTKLIB working in the folder of the chunk."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    project, folder = _ppk_project(tmp_path)
    doc = ppg.Document.open(project)
    gnss_apply.only_fix(doc.chunk, True)  # only the fix positions: the filter is decided again after the PPK
    doc.save()
    run = CliRunner().invoke

    result = run(app, _ppk_command(project, folder, "--nav", str(PPK_NAV)))

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "6 of 9 photos took the PPK position of their shot (float 3, fix 2, single 1)"
    # the base, latitude and longitude said by name, and how far it is from the median position of the drone
    mark = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    drone = ppk.read_pos(pos)
    drone.base = {"position": [mark[1], mark[0], mark[2]]}
    assert lines[1] == (f"base 0759: latitude {mark[0]:.8f}, longitude {mark[1]:.8f}, {mark[2]:.3f} m above the "
                        "ellipsoid (from its RINEX header, frame unknown: WGS 84), antenna 0.000 m above it; "
                        f"{ppk.baseline_km(drone):.1f} km from the flight")
    # the solution of the run, in the work folder of the chunk
    work = (project.parent / "volo.ppg.files" / "chunk_0" / "gnss" / "ppk").resolve()
    (call,) = fake_rtklib()
    conf, out = _run_files(call)
    assert lines[2] == f"solution: {work / out}"
    warnings = [line for line in lines if line.startswith("warning: ")]
    assert len(warnings) == 3
    assert warnings[0].startswith("warning: the position of the base 0759 comes from its RINEX header")
    assert "metres" in warnings[0]
    assert warnings[1].startswith("warning: the phase-centre offset of the antenna TRM29659.00 of the base 0759 is not "
                                  "applied")
    assert f"3 photos were taken where the solution {out} has no position" in warnings[2]
    assert "without a PPK position (3): DJI_0001, DJI_0004, DJI_0009" in lines
    assert "shots of the MRK without a PPK position (3): 1, 4, 9" in lines
    # RTKLIB worked in the folder of the chunk, on the files given
    assert call["args"][:4] == ["-k", str(work / conf), "-o", str(work / out)]
    assert call["inputs"] == [_sha256(p.read_bytes()) for p in (PPK_ROVER, PPK_BASE, PPK_NAV)]
    # the positions are in the project, the photos in the gaps keep theirs, the fix-only filter decided again
    ch = ppg.Document.open(project).chunk
    states = {c.label: (c.meta["gnss"]["source"], c.meta["gnss"].get("status")) for c in ch.cameras}
    assert states == {"DJI_0001": ("exif", None), "DJI_0002": ("ppk", "fix"), "DJI_0003": ("ppk", "float"),
                      "DJI_0004": ("exif", None), "DJI_0005": ("ppk", "float"), "DJI_0006": ("ppk", "single"),
                      "DJI_0007": ("ppk", "float"), "DJI_0008": ("ppk", "fix"), "DJI_0009": ("exif", None)}
    assert ch.camera("DJI_0002").meta["gnss"]["base"]["position_source"] == "header"
    assert ch.camera("DJI_0002").meta["gnss"]["solution"] == out
    enabled = {c.label for c in ch.cameras if c.reference.enabled}
    assert enabled == {"DJI_0002", "DJI_0008"}
    assert sorted(ch.meta["gnss_only_fix"]["disabled"]) == sorted(c.key for c in ch.cameras
                                                                  if c.label not in enabled)
    assert ppk.last_run(ch)["base"] == str(PPK_BASE.absolute())  # the run, for the dialog of the next one

    # the base at its surveyed position (a few metres from the header's), written with points and spaces: no warning
    # about it, RTKLIB given it, kept as every position of the program: longitude first
    result = run(app, _ppk_command(project, folder, "--nav", str(PPK_NAV), "--base-position",
                                   " 35.1609000, 139.6138000 ,71.25 "))
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert re.fullmatch(r"base 0759: latitude 35\.16090000, longitude 139\.61380000, 71\.250 m above the ellipsoid "
                        r"\(typed, frame unknown: WGS 84\), antenna 0\.000 m above it; \d+\.\d km from the flight",
                        lines[1]), lines[1]
    assert not any("RINEX header" in line for line in lines if line.startswith("warning: "))
    options = _options(fake_rtklib()[-1]["options"])
    assert [float(options[f"ant2-pos{i}"]) for i in (1, 2, 3)] == [35.1609, 139.6138, 71.25]
    ch = ppg.Document.open(project).chunk
    base = ch.camera("DJI_0002").meta["gnss"]["base"]
    assert base["position_source"] == "typed" and base["position"] == [139.6138, 35.1609, 71.25]
    assert ch.camera("DJI_0002").meta["gnss"]["solution"] == _run_files(fake_rtklib()[-1])[1] != out
    assert ppk.last_run(ch)["position"] == [139.6138, 35.1609, 71.25]

    # typed far from the header's position (a wrong degree, a height above the sea level): computed, with a warning
    result = run(app, _ppk_command(project, folder, "--nav", str(PPK_NAV), "--base-position", "35.6,139.6,20"))
    assert result.exit_code == 0, result.output
    (far,) = [line for line in result.output.splitlines() if "typed position" in line]
    said = re.match(r"warning: the typed position of the base 0759 is ([\d.]+) km away horizontally and "
                    r"(-[\d.]+) m in height from the position of its RINEX header: check that latitude and longitude "
                    r"are not swapped or without their sign, and that the height is above the ellipsoid, not above "
                    r"the sea level$", far)
    assert said, far
    apart = Geod(ellps="WGS84").inv(mark[1], mark[0], 139.6, 35.6)[2] / 1000.0
    assert abs(float(said.group(1)) - apart) < 0.2 and float(said.group(2)) == pytest.approx(20 - mark[2], abs=0.05)


def test_ppk_command_line_keeps_the_onboard_fixes_unless_told(tmp_path, fake_rtklib, monkeypatch):
    """ppg gnss ppk after ppg gnss mrk: the photos with an RTK fix of the drone itself keep it and the command says how
    many; --replace-fix gives them the PPK position too; a flight of onboard fixes only changes nothing, and that is
    no error."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    solution = ppk.read_pos(pos)
    run = CliRunner().invoke

    def project(name: str, fixes, epochs=(0, 1, 2, 4, 120)) -> tuple[Path, Path]:
        folder = _onboard_flight(tmp_path / name / "100MEDIA", solution, list(epochs), fixes=fixes)
        doc = ppg.Document()
        doc.add_chunk("Volo").add_photos([folder])
        doc.save(tmp_path / name / "volo.ppg")
        mrk = run(app, ["gnss", "mrk", str(tmp_path / name / "volo.ppg"), str(folder / "Timestamp.MRK")])
        assert mrk.exit_code == 0, mrk.output
        return tmp_path / name / "volo.ppg", folder

    path, folder = project("misto", fixes={1, 2, 3})
    result = run(app, _ppk_command(path, folder, "--nav", str(PPK_NAV)))
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[:2] == ["2 of 5 photos took the PPK position of their shot (single 1, fix 1)",
                         "3 photos with an RTK fix of the drone itself kept their positions (--replace-fix gives them "
                         "the PPK one)"]
    ch = ppg.Document.open(path).chunk
    assert [c.meta["gnss"]["source"] for c in ch.cameras] == ["dji_mrk"] * 3 + ["ppk"] * 2
    assert not any("without a PPK position" in line for line in lines)

    result = run(app, _ppk_command(path, folder, "--nav", str(PPK_NAV), "--replace-fix"))
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "5 of 5 photos took the PPK position of their shot (fix 3, float 1, single 1)"
    assert not any("kept" in line for line in lines)
    assert ("warning: 1 photo had an onboard RTK fix and now has a PPK position that is not fix (float or single)"
            in lines)
    assert {c.meta["gnss"]["source"] for c in ppg.Document.open(path).chunk.cameras} == {"ppk"}

    path, folder = project("solo_fix", fixes={1, 2, 3, 4, 5})
    before = _snapshot(ppg.Document.open(path).chunk)
    result = run(app, _ppk_command(path, folder, "--nav", str(PPK_NAV)))
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert ("no photo took a PPK position: 5 photos kept their onboard RTK fix (--replace-fix gives them the PPK one)"
            in lines)
    assert not any("without a PPK position" in line for line in lines)  # all of them are accounted for
    assert _snapshot(ppg.Document.open(path).chunk) == before

    # a photo in the folder that is not in the chunk: left without a PPK position, and the line does not say that the
    # photos of the flight all have an onboard fix - it says how many kept it and how many are left without
    path, folder = project("con_resto", fixes={1, 2, 3, 4, 5})
    _jpeg(folder / "DJI_0099.JPG", None)
    before = _snapshot(ppg.Document.open(path).chunk)
    result = run(app, _ppk_command(path, folder, "--nav", str(PPK_NAV)))
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert ("no photo took a PPK position: 5 photos kept their onboard RTK fix (--replace-fix gives them the PPK one) "
            "and 1 is without a PPK position") in lines
    assert "without a PPK position (1): DJI_0099" in lines
    assert not any("the photos of the flight" in line for line in lines)
    assert _snapshot(ppg.Document.open(path).chunk) == before

    # one photo that kept its fix, one more left without: the singular forms
    path, folder = project("uno", fixes={1}, epochs=(0,))
    _jpeg(folder / "DJI_0099.JPG", None)
    result = run(app, _ppk_command(path, folder, "--nav", str(PPK_NAV)))
    assert result.exit_code == 0, result.output
    assert ("no photo took a PPK position: 1 photo kept its onboard RTK fix (--replace-fix gives it the PPK one) and 1 "
            "is without a PPK position") in result.output.splitlines()


def test_ppk_command_line_says_what_went_wrong_and_changes_nothing(tmp_path, fake_rtklib, monkeypatch):
    """Without a navigation file, with a rover of another day, with a position of the base that is not three numbers
    or out of range, with an MRK of another day and without RTKLIB: an error that says so, the project as it was."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    project, folder = _ppk_project(tmp_path)
    before = _snapshot(ppg.Document.open(project).chunk)
    run = CliRunner().invoke

    def refused(*options: str, rover: Path = PPK_ROVER, base: Path = PPK_BASE, mrk: Path | None = None,
                code: int = 1) -> str:
        command = _ppk_command(project, folder, *options)
        for name, value in (("--rover", rover), ("--base", base), ("--mrk", mrk)):
            if value is not None:
                command[command.index(name) + 1] = str(value)
        result = run(app, command)
        assert result.exit_code == code, result.output
        assert _snapshot(ppg.Document.open(project).chunk) == before
        return _plain_words(result.output)

    # files that are not there, or a folder named as a file: refused before anything is read (a usage error)
    for option in ("--mrk", "--rover", "--base"):
        assert "does not exist" in refused(**{option[2:]: tmp_path / "missing.obs"}, code=2)
    assert "does not exist" in refused("--nav", str(tmp_path / "missing.nav"), code=2)
    assert "is a directory" in refused(rover=tmp_path, code=2)
    # no navigation file beside the rover or the base, and none given
    alone = tmp_path / "rinex"
    alone.mkdir()
    shutil.copyfile(PPK_ROVER, alone / "drone.obs")
    shutil.copyfile(PPK_BASE, alone / "base.05o")
    output = refused(rover=alone / "drone.obs", base=alone / "base.05o")
    assert "error: no navigation file beside the rover drone.obs or the base base.05o" in output
    # the rover of another day
    assert "have no time in common" in refused("--nav", str(PPK_NAV), rover=_rinex2(tmp_path / "rover.24o", V2_HEADER))
    # a position of the base that is not three numbers with a point for decimals: a usage error
    for text in ("35,1;139,6;70", "35.1,139.6", "north,east,70"):
        assert "--base-position" in refused("--nav", str(PPK_NAV), "--base-position", text, code=2)
    # out of range
    assert "the latitude of the base, 135.1" in refused("--nav", str(PPK_NAV), "--base-position", "135.1,139.6,70")
    # the photos and the MRK of the next day, out of the time of the solution
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    next_day = _ppk_flight(tmp_path / "next", [(1, START_2005 + 86400.0, (0, 0, 150))])
    assert "they are not of the same flight" in refused("--nav", str(PPK_NAV), mrk=next_day / "Timestamp.MRK")
    # without RTKLIB: neither the native core (switched off here) nor PPG_RNX2RTKP
    monkeypatch.delenv("PPG_RNX2RTKP")
    output = refused("--nav", str(PPK_NAV))
    assert "error: PPK needs RTKLIB" in output and "pyarchinit-photogrammetry-core" in output


@needs_rtklib
def test_ppk_command_line_with_rtklib(tmp_path, monkeypatch):
    """ppg gnss ppk on the RTKLIB sample data with RTKLIB in the native core: every photo shot on an epoch of the
    solution takes the camera below the antenna of the solution, with the status of its Q; the one shot between two
    epochs 30 s apart keeps its position."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    solution = ppk.solve(PPK_ROVER, PPK_BASE, folder=tmp_path / "first")  # the same run the command makes
    epochs = [0, 7, 33, 64, 101, 119]
    shots = [(i + 1, float(solution.times[k]) - START_2005, (40 * i - 100, 150 - 60 * i, 140 + 10 * i))
             for i, k in enumerate(epochs)]
    shots.append((7, float(solution.times[50]) + 15.0 - START_2005, (0, 0, 150)))
    project, folder = _ppk_project(tmp_path, shots)

    result = CliRunner().invoke(app, _ppk_command(project, folder))  # the navigation file found beside the rover

    assert result.exit_code == 0, result.output
    states = [ppk.status(solution.q[k]) for k in epochs]
    counts = ", ".join(f"{state} {n}" for state, n in sorted(Counter(states).items(), key=lambda p: -p[1]))
    assert result.output.splitlines()[0] == f"6 of 7 photos took the PPK position of their shot ({counts})"
    assert "without a PPK position (1): DJI_0007" in result.output
    ch = ppg.Document.open(project).chunk
    for (index, _seconds, arm), k, state in zip(shots, epochs, states):
        cam = ch.camera(f"DJI_{index:04d}")
        assert np.linalg.norm(_ecef(*cam.reference.location) - _camera_ecef(solution.llh[k], arm)) < 1e-3, index
        assert (cam.meta["gnss"]["source"], cam.meta["gnss"]["status"]) == ("ppk", state)
    assert ch.camera("DJI_0007").meta["gnss"]["source"] == "exif"


# --- the PPK step of the GNSS pane ------------------------------------------------------------------------------


@pytest.fixture
def qt_app(tmp_path):
    """The application of the interface, its preferences in INI files of the test."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    return app


DJI_FLIGHT = "DJI_202604021000_001"


def _dji_card(tmp_path: Path, shots=(30.0, 60.0, 90.0)) -> Path:
    """The folder of a flight as a DJI drone writes it: the Timestamp.MRK (shots ``shots`` seconds after 00:00:00 GPS
    time on the day of the RTKLIB sample data) beside the RINEX observations and the navigation messages of its
    receiver, the raw file, and the AppleDouble file macOS leaves on an external drive."""
    folder = tmp_path / f"{DJI_FLIGHT}_Rilievo"
    folder.mkdir()
    (folder / f"{DJI_FLIGHT}_Timestamp.MRK").write_text(
        "".join(_mrk_line(i, START_2005 + seconds, (0, 0, 150)) for i, seconds in enumerate(shots, 1)), newline="\n")
    shutil.copyfile(PPK_ROVER, folder / f"{DJI_FLIGHT}_Rinex.obs")
    shutil.copyfile(DATA / "30400920.05n", folder / f"{DJI_FLIGHT}_PPKNAV.nav")
    (folder / f"{DJI_FLIGHT}_PPKRAW.bin").write_bytes(b"\xb5\x62")
    (folder / f"._{DJI_FLIGHT}_PPKNAV.nav").write_bytes(b"\x00\x05\x16\x07")
    return folder


def _station(tmp_path: Path) -> Path:
    """The observations of the base 0759 in a folder of their own beside its navigation file; returns them."""
    station = tmp_path / "stazione"
    station.mkdir()
    shutil.copyfile(PPK_NAV, station / "07590920.05n")
    return Path(shutil.copyfile(PPK_BASE, station / "07590920.05o"))


def _choose(dialog, field, path) -> None:
    """``path`` typed in the file ``field`` of the PPK ``dialog``, and the files read at once, without the pause."""
    field.edit.setText(str(path))
    dialog.refresh()


def _wait_until(condition, seconds: float = 5.0) -> bool:
    """Run the event loop until ``condition()`` is true, for ``seconds`` at most; whether it became true. A timer of a
    few hundred milliseconds can wait much longer in the event loop of a loaded machine (a CI runner): a test waits for
    what the timer does, not for the time it asked for."""
    from PySide6.QtTest import QTest

    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(20)
    return bool(condition())


def test_the_drone_rinex_beside_its_mrk(tmp_path, qt_app):
    from pyarchinit_photogrammetry.gui.ppk_dialog import rover_beside

    card = _dji_card(tmp_path)
    mrk = card / f"{DJI_FLIGHT}_Timestamp.MRK"
    assert rover_beside(mrk) == card / f"{DJI_FLIGHT}_Rinex.obs"
    # two flights in one folder: the one of the MRK; an MRK without the name of its flight cannot tell them apart
    (card / "DJI_202604021100_002_Rinex.obs").write_text("", newline="\n")
    assert rover_beside(mrk) == card / f"{DJI_FLIGHT}_Rinex.obs"
    (card / "Timestamp.MRK").write_text("", newline="\n")
    assert rover_beside(card / "Timestamp.MRK") is None
    # a receiver that is not DJI's: the only observation file there, hidden files left out
    other = tmp_path / "volo"
    other.mkdir()
    (other / "Timestamp.MRK").write_text("", newline="\n")
    assert rover_beside(other / "Timestamp.MRK") is None
    (other / "drone.24o").write_text("", newline="\n")
    (other / "._drone.24o").write_text("", newline="\n")
    (other / "drone.24n").write_text("", newline="\n")
    assert rover_beside(other / "Timestamp.MRK") == other / "drone.24o"
    (other / "drone2.obs").write_text("", newline="\n")
    assert rover_beside(other / "Timestamp.MRK") is None


def test_ppk_dialog_finds_the_files_and_checks_the_base(tmp_path, qt_app, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from pyarchinit_photogrammetry.gui import ppk_dialog
    from pyarchinit_photogrammetry.gui.ppk_dialog import PpkDialog

    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))  # no copy of igs20.atx
    card = _dji_card(tmp_path)
    drone_nav = card / f"{DJI_FLIGHT}_PPKNAV.nav"
    dlg = PpkDialog(card / f"{DJI_FLIGHT}_Timestamp.MRK")
    # the shots of the MRK, read at once; the drone's observations beside it and its navigation file (not the
    # AppleDouble one); the drone's receiver and the time it covers, to the second (its last epoch is at 00:59:29.996)
    assert dlg.mrk_info.text() == "Scatti: 3, dal 2005-04-02 00:00:30 al 2005-04-02 00:01:30 (ora GPS)."
    assert dlg.rover.path() == card / f"{DJI_FLIGHT}_Rinex.obs"
    assert dlg.nav_paths() == [drone_nav]
    assert dlg.rover_info.text() == ("Ricevitore TRIMBLE 5700; osservazioni dal 2005-04-02 00:00:00 al "
                                     "2005-04-02 00:59:29 (ora GPS).")
    assert dlg.problem() == "Scegli le osservazioni della base."
    assert "Aggiungi" in dlg.base_info.text()  # one base file, or several that cover the flight together
    assert not dlg.antenna_warning.isVisibleTo(dlg)
    # the base of a station in a folder of its own: what its header says, its navigation file after the drone's
    base = _station(tmp_path)
    station = base.parent
    _choose(dlg, dlg.base, base)
    info = dlg.base_info.text()
    for said in ("Base 0759", "TRIMBLE 5700", "TRM29659.00", "2005-04-02 00:00:00", "2005-04-02 00:59:30",
                 "Posizione nell'intestazione", "altezza dell'antenna nulla"):
        assert said in info, (said, info)
    # a note in sight, not a tooltip: the phase-centre offset of the antenna of its header is applied from igs20.atx,
    # downloaded once (on by default); without it, it is not applied and the heights can be too low
    assert dlg.atx_download.isChecked() and dlg.antenna_warning.isVisibleTo(dlg)
    assert dlg.antenna_warning.text().startswith("Lo scostamento del centro di fase dell'antenna TRM29659.00 della "
                                                 "base viene applicato con le calibrazioni dell'IGS (igs20.atx)")
    dlg.atx_download.setChecked(False)
    assert dlg.antenna_warning.text().startswith(
        "Lo scostamento del centro di fase dell'antenna TRM29659.00 della base non viene applicato")
    assert "più basse di qualche centimetro" in dlg.antenna_warning.text()
    assert dlg.values()["atx"] == "auto"
    dlg.atx_download.setChecked(True)
    assert dlg.nav_paths() == [drone_nav, station / "07590920.05n"]
    # the position of the header by default, with its warning; the fields of a typed position off
    assert dlg.from_header.isChecked() and dlg.header_warning.isVisibleTo(dlg)
    assert "sbagliata di metri" in dlg.header_warning.text() and not dlg.lat.isEnabled()
    assert dlg.problem() is None
    # the onboard fixes kept unless told, said beside the choice
    assert not dlg.replace_fix.isChecked() and dlg.fix_note.text().startswith("Le foto con un fix RTK del drone")
    assert dlg.values() == {"rover": card / f"{DJI_FLIGHT}_Rinex.obs", "base": base,
                            "navs": [drone_nav, station / "07590920.05n"], "base_position": None,
                            "replace_fix": False, "base_mode": "header", "base_frame": None, "base_epoch": None,
                            "station": None, "atx": "download"}
    dlg.replace_fix.setChecked(True)
    assert dlg.values()["replace_fix"] is True
    dlg.replace_fix.setChecked(False)
    # typed: the warning goes, three numbers with a comma or a point for the decimals
    dlg.typed.setChecked(True)
    assert not dlg.header_warning.isVisibleTo(dlg) and dlg.lat.isEnabled()
    assert dlg.problem().startswith("Scrivi la posizione della base")
    dlg.lat.setText("35,1609")
    dlg.lon.setText(" 139.6138 ")
    dlg.ellh.setText("71,25")
    assert dlg.problem() is None and dlg.values()["base_position"] == (35.1609, 139.6138, 71.25)
    assert dlg.height() is not None  # the field of the height does not hide QWidget.height()
    dlg.lat.setText("95")
    dlg.accept()  # refused, the reason in the dialog
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert dlg.error.text() == "La latitudine della base deve essere fra -90 e 90 gradi."
    dlg.lat.setText("35,1609")
    dlg.lon.setText("-181")
    assert dlg.problem() == "La longitudine della base deve essere fra -180 e 180 gradi."
    dlg.lon.setText("139.6138")
    # a navigation file taken away stays away when the files change, one added stays
    dlg.navs.setCurrentRow(0)
    dlg.b_remove_nav.click()
    assert dlg.nav_paths() == [station / "07590920.05n"]
    added = Path(shutil.copyfile(DATA / "30400920.05n", tmp_path / "brdc.05n"))
    monkeypatch.setattr(ppk_dialog.QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(added)], ""))
    dlg.b_add_nav.click()
    assert dlg.nav_paths() == [station / "07590920.05n", added]
    # the base with the antenna 1.5 m above its mark, in another folder
    high = tmp_path / "alta"
    high.mkdir()
    _choose(dlg, dlg.base, _raised_base(high, 1.5))
    assert "aggiunge l'altezza dell'antenna del file della base, 1,500 m" in dlg.base_info.text()
    assert dlg.nav_paths() == [added]  # no navigation file beside that base
    # no navigation file at all
    dlg.navs.setCurrentRow(0)
    dlg.b_remove_nav.click()
    assert dlg.problem().startswith("Nessun file di navigazione")
    _choose(dlg, dlg.base, base)  # the one added and taken away again is gone, the base's is back
    assert dlg.nav_paths() == [station / "07590920.05n"] and dlg.problem() is None
    # what cannot be a base: a navigation file, the drone's own file, a Hatanaka file, a missing file
    _choose(dlg, dlg.base, station / "07590920.05n")
    assert "navigazione, non osservazioni" in dlg.base_info.text() and "base" in dlg.problem()
    _choose(dlg, dlg.base, card / f"{DJI_FLIGHT}_Rinex.obs")
    assert "stesso file" in dlg.problem()
    crx = tmp_path / "base.crx"
    crx.write_text("1.0                 COMPACT RINEX FORMAT                    CRINEX VERS   / TYPE\n")

    def no_decoder(*_args):
        raise RuntimeError(_native.CRINEX_MISSING)

    with monkeypatch.context() as no_crx2rnx:  # a Hatanaka file is read when there is a decoder: without, it is said so
        no_crx2rnx.setattr(_native, "crx2rnx", no_decoder)
        _choose(dlg, dlg.base, crx)  # read in the background: the dialog says so meanwhile
        assert _wait_until(lambda: "CRX2RNX" in dlg.base_info.text()), dlg.base_info.text()
        assert "CRX2RNX" in dlg.problem()
    _choose(dlg, dlg.base, tmp_path / "nowhere.05o")
    assert dlg.base_info.text() == "Il file non esiste." and dlg.problem() is not None
    # a typed base needs no position in the header; the header one does
    _choose(dlg, dlg.base, base)
    dlg.from_header.setChecked(True)
    assert dlg.problem() is None


def test_ppk_dialog_checks_the_times_the_work_folder_and_a_typed_base(tmp_path, qt_app, monkeypatch):
    """Before the run: rover and base with hours in common, the shots of the MRK among the drone's observations, no
    input in the work folder of the PPK, the navigation files still there; a typed position far from the header's is
    a warning, not a refusal; *Togli* takes away at the first click a file both added by hand and found."""
    from pyarchinit_photogrammetry.gui import ppk_dialog
    from pyarchinit_photogrammetry.gui.i18n import number
    from pyarchinit_photogrammetry.gui.ppk_dialog import PpkDialog

    card = _dji_card(tmp_path)
    rover = card / f"{DJI_FLIGHT}_Rinex.obs"
    base = _station(tmp_path)
    work = tmp_path / "volo.ppg.files" / "chunk_0" / "gnss" / "ppk"
    dlg = PpkDialog(card / f"{DJI_FLIGHT}_Timestamp.MRK", work=work)
    _choose(dlg, dlg.base, base)
    assert dlg.problem() is None
    # a base of another day: no hours in common with the drone
    _choose(dlg, dlg.base, _rinex2(tmp_path / "base.24o", V2_HEADER))
    assert dlg.problem() == ("Il drone (dal 2005-04-02 00:00:00 al 2005-04-02 00:59:29) e la base (dal 2024-05-12 "
                             "10:30:00 al 2024-05-12 10:30:02) non hanno ore in comune: la base deve registrare mentre "
                             "il drone vola.")
    _choose(dlg, dlg.base, base)
    # the MRK of another flight: its shots outside the drone's observations; some outside only: said, not refused
    other = tmp_path / "altro"
    other.mkdir()
    (other / "Timestamp.MRK").write_text(_mrk_line(1, START_2005 + 86400.0, (0, 0, 150)), newline="\n")
    late = PpkDialog(other / "Timestamp.MRK", work=work)
    _choose(late, late.rover, rover)
    _choose(late, late.base, base)
    assert late.problem() == ("Gli scatti del Timestamp.MRK (dal 2005-04-03 00:00:00 al 2005-04-03 00:00:00) sono "
                              "fuori dalle osservazioni del drone (dal 2005-04-02 00:00:00 al 2005-04-02 00:59:29): "
                              "non sono dello stesso volo.")
    (other / "parte.MRK").write_text("".join(_mrk_line(i, START_2005 + s, (0, 0, 150))
                                             for i, s in ((1, 30.0), (2, 3600.0), (3, 3630.0))), newline="\n")
    part = PpkDialog(other / "parte.MRK", work=work)
    _choose(part, part.rover, rover)
    _choose(part, part.base, base)
    assert part.mrk_info.text() == ("Scatti: 3, dal 2005-04-02 00:00:30 al 2005-04-02 01:00:30 (ora GPS). Scatti fuori "
                                    "dalle osservazioni del drone: 2; le loro foto terranno la posizione che hanno.")
    assert part.problem() is None
    # an MRK that is not one, or without shots
    (other / "rotto.MRK").write_text("not an MRK line\n", newline="\n")
    broken = PpkDialog(other / "rotto.MRK")
    assert broken.mrk_info.text().startswith("Il file non si legge: rotto.MRK, line 1: not a DJI Timestamp.MRK line")
    assert broken.problem().startswith("Il Timestamp.MRK non si può usare: Il file non si legge: rotto.MRK")
    (other / "vuoto.MRK").write_text("", newline="\n")
    assert PpkDialog(other / "vuoto.MRK").problem() == "Il Timestamp.MRK non si può usare: Il file non ha scatti."
    # an input in the work folder of the PPK, where the program writes and deletes files
    work.mkdir(parents=True)
    _choose(dlg, dlg.base, shutil.copyfile(PPK_BASE, work / "base.05o"))
    assert dlg.problem() == (f"base.05o è nella cartella di lavoro del PPK ({work}), dove il programma scrive e "
                             "cancella file: spostalo fuori da lì.")
    _choose(dlg, dlg.base, base)
    # a navigation file listed, then deleted
    extra = Path(shutil.copyfile(DATA / "30400920.05n", tmp_path / "brdc.05n"))
    monkeypatch.setattr(ppk_dialog.QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(extra)], ""))
    dlg.b_add_nav.click()
    extra.unlink()
    assert dlg.problem() == "brdc.05n non esiste più."
    dlg.navs.setCurrentRow(dlg.nav_paths().index(extra))
    dlg.b_remove_nav.click()
    # Togli on a navigation file found beside the base and added by hand too: away at the first click
    station_nav = base.parent / "07590920.05n"
    monkeypatch.setattr(ppk_dialog.QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(station_nav)], ""))
    dlg.b_add_nav.click()
    assert dlg.nav_paths().count(station_nav) == 1
    dlg.navs.setCurrentRow(dlg.nav_paths().index(station_nav))
    dlg.b_remove_nav.click()
    assert station_nav not in dlg.nav_paths() and dlg.problem() is None
    # typed near the header's position: no warning; far from it, horizontally or in height: a warning, not a refusal
    mark = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    dlg.typed.setChecked(True)
    dlg.lat.setText("35,1609")
    dlg.lon.setText("139,6138")
    dlg.ellh.setText("71,25")
    assert not dlg.far_warning.isVisibleTo(dlg) and dlg.problem() is None
    dlg.lon.setText("139,7138")  # 0.1 degrees east: 9 km
    said = re.match(r"La posizione scritta è a (\d+,\d) km in pianta e a ([-\d,]+) m in quota da quella "
                    r"dell'intestazione del file della base: controlla", dlg.far_warning.text())
    apart = Geod(ellps="WGS84").inv(mark[1], mark[0], 139.7138, 35.1609)[2] / 1000.0
    assert said and abs(float(said.group(1).replace(",", ".")) - apart) < 0.1 and dlg.far_warning.isVisibleTo(dlg)
    assert dlg.problem() is None
    dlg.lon.setText("139,6138")
    dlg.ellh.setText("25")  # above the sea level
    assert dlg.far_warning.isVisibleTo(dlg)
    assert f"a {number(25.0 - mark[2], 1)} m in quota" in dlg.far_warning.text()
    assert "Latitudine e longitudine in gradi; la quota in metri sull'ellissoide" in " ".join(
        label.text() for label in dlg.findChildren(type(dlg.far_warning)) if label.isVisibleTo(dlg))


def test_ppk_dialog_reads_the_files_after_a_pause_in_typing_and_once(tmp_path, qt_app, monkeypatch):
    """A file name typed a key at a time reads nothing until a pause; then the base is read once, and only its folder
    is looked at (the drone's was already); reading again, checking, typing a position read nothing more."""
    from pyarchinit_photogrammetry.gui.ppk_dialog import PpkDialog

    card = _dji_card(tmp_path)
    base = _station(tmp_path)
    dlg = PpkDialog(card / f"{DJI_FLIGHT}_Timestamp.MRK")
    headers, folders = [], []
    real_header, real_companions = rinex.header, rinex.find_companions
    monkeypatch.setattr(rinex, "header", lambda path: headers.append(Path(path).name) or real_header(path))
    monkeypatch.setattr(rinex, "find_companions",
                        lambda obs: folders.append(Path(obs).name) or real_companions(obs))
    text = str(base)
    for i in range(1, len(text) + 1):
        dlg.base.edit.setText(text[:i])
    assert headers == [] and folders == [] and dlg.base_info.text().startswith("Il RINEX di una base")
    assert _wait_until(lambda: dlg.base_info.text().startswith("Base 0759")), dlg.base_info.text()  # the pause is over
    assert headers == ["07590920.05o", "07590920.05n"] and folders == ["07590920.05o"]
    dlg.refresh()
    assert dlg.problem() is None
    dlg.typed.setChecked(True)
    for key in "35,16":
        dlg.lat.setText(dlg.lat.text() + key)
    assert headers == ["07590920.05o", "07590920.05n"] and folders == ["07590920.05o"]


def _cut_labels(dialog) -> list:
    """The word-wrapped labels the dialog shows less tall than their text needs at their width."""
    from PySide6.QtWidgets import QLabel

    return [(label.text()[:40], label.height(), label.heightForWidth(label.width()))
            for label in dialog.findChildren(QLabel) if label.isVisible() and label.wordWrap() and label.text()
            and label.heightForWidth(label.width()) > label.height()]


def test_ppk_dialog_shows_all_it_says(tmp_path, qt_app, monkeypatch):
    """No text of the dialog is cut: opened without a base, then with one (five lines more), typed far from the
    header (a warning more), with an error under the form. It grows with what it says, stays on the screen and
    scrolls only when the screen is full; on a short screen the form scrolls and still nothing is cut."""
    from pyarchinit_photogrammetry.gui.ppk_dialog import PpkDialog

    card = _dji_card(tmp_path)
    base = _station(tmp_path)
    dlg = PpkDialog(card / f"{DJI_FLIGHT}_Timestamp.MRK")
    dlg.show()

    def shown() -> None:
        qt_app.processEvents()
        assert _cut_labels(dlg) == []
        room = dlg.screen().availableGeometry()
        assert room.contains(dlg.frameGeometry())
        if dlg.scroll.verticalScrollBar().isVisible():  # the form scrolls only when the screen is full
            assert dlg.frameGeometry().height() >= room.height() - 1

    try:
        shown()
        opened = dlg.height()
        _choose(dlg, dlg.base, base)
        shown()
        # it grew with the description of the base, unless the screen was full already
        full = dlg.screen().availableGeometry().height() - 1
        assert dlg.height() > opened or dlg.frameGeometry().height() >= full
        dlg.typed.setChecked(True)
        dlg.lat.setText("35,6")
        dlg.lon.setText("139,6")
        dlg.ellh.setText("20")
        shown()
        assert dlg.far_warning.isVisible()
        dlg.ellh.setText("")
        dlg.accept()
        shown()
        assert dlg.error.isVisible() and dlg.error.text().startswith("Scrivi la posizione della base")
        # a short screen: as tall as the screen lets it be, the form scrolls, nothing cut
        monkeypatch.setattr(dlg, "_room", lambda: 420)
        dlg.refresh()
        qt_app.processEvents()
        assert dlg.height() == 420 and dlg.scroll.verticalScrollBar().isVisible()
        assert _cut_labels(dlg) == []
    finally:
        dlg.close()


def test_the_ppk_step_says_which_bases_and_how_they_were_placed(qt_app, monkeypatch):
    from pyarchinit_photogrammetry.core.camera import Camera
    from pyarchinit_photogrammetry.gui.gnss_pane import ppk_state

    ch = ppg.Document().add_chunk()

    def photo(key: int, gnss: dict) -> None:
        cam = Camera(key=key, label=f"IMG_{key}", path=f"IMG_{key}.JPG", sensor_key=0)
        cam.meta["gnss"] = gnss
        ch.cameras.append(cam)

    photo(0, {"source": "exif"})
    # no PPK positions: what the step needs, and that RTKLIB is missing when it is
    monkeypatch.setenv("PPG_NATIVE", "0")
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    assert ppk_state(ch).startswith("Nessuna posizione PPK.") and "nucleo nativo 0.5" in ppk_state(ch)
    monkeypatch.setenv("PPG_RNX2RTKP", "rnx2rtkp")
    assert "nucleo nativo" not in ppk_state(ch)
    # the photos by status; a base placed by its header is said even when other runs typed it; a solution of another
    # program (no base recorded) says nothing about its base
    photo(1, {"source": "ppk", "status": "fix", "base": {"name": "BASE", "position_source": "typed"}})
    photo(2, {"source": "ppk", "status": "float", "base": {"name": "BASE", "position_source": "header"}})
    photo(3, {"source": "ppk", "status": "fix", "base": {"name": "CORS", "position_source": "typed"}})
    photo(4, {"source": "ppk", "status": "fix", "base": {}})
    text = ppk_state(ch)
    assert text.startswith("PPK: 4 foto su 5 (fix 3, float 1). La base BASE è nella posizione della sua intestazione "
                           "RINEX, che può essere sbagliata di metri")
    assert text.endswith("della base. Base CORS: posizione scritta, sistema sconosciuto (WGS 84).")
    # one base typed at two positions: within 5 cm nothing, further a warning that one is likely wrong
    typed = {"name": "CORS", "position_source": "typed", "position": [11.0, 44.0, 50.0]}
    photo(5, {"source": "ppk", "status": "fix", "base": typed})
    photo(6, {"source": "ppk", "status": "fix", "base": {**typed, "position": [11.0, 44.0, 50.03]}})
    assert ppk_state(ch).endswith("Base CORS: posizione scritta, sistema sconosciuto (WGS 84).")
    photo(7, {"source": "ppk", "status": "fix", "base": {**typed, "position": [11.0, 44.0, 49.88]}})
    assert ppk_state(ch).endswith("Base CORS: posizione scritta, sistema sconosciuto (WGS 84). La base CORS è stata "
                                  "scritta in posizioni diverse in questo chunk, fino a 15 cm l'una dall'altra: se è "
                                  "lo stesso punto, una è sbagliata; calcola di nuovo i voli con quella giusta.")

    # where each base comes from, its frame and the epoch of its coordinates: typed in a frame, a permanent station,
    # a static baseline from a station, a static PPP
    other = ppg.Document().add_chunk()
    itrf = {"frame": "ITRF2020", "epoch": 2026.8213}
    for key, base in enumerate([
            {"name": "RDN", "position_source": "typed", "frame": "ETRF2000", "epoch": None},
            {"name": "AQUI", "position_source": "station", **itrf,
             "detail": {"station": "AQUI00ITA", "coordinates": {"source": "EPN"}}},
            {"name": "MIA", "position_source": "baseline", **itrf, "detail": {"station": "AQUI00ITA"}},
            {"name": "SOLA", "position_source": "ppp", **itrf, "detail": {"centre": "ESA", "kind": "final"}}]):
        cam = Camera(key=key, label=f"IMG_{key}", path=f"IMG_{key}.JPG", sensor_key=0)
        cam.meta["gnss"] = {"source": "ppk", "status": "fix", "base": base}
        other.cameras.append(cam)
    assert ppk_state(other) == (
        "PPK: 4 foto su 4 (fix 4). Base RDN: posizione scritta, ETRF2000 (RDN2008). Base AQUI: coordinate della "
        "stazione permanente AQUI00ITA (EPN), ITRF2020 all'epoca 2026,82. Base MIA: calcolata con una linea di base "
        "dalla stazione AQUI00ITA, ITRF2020 all'epoca 2026,82. Base SOLA: calcolata con il PPP statico (ESA final), "
        "ITRF2020 all'epoca 2026,82.")


def test_ppk_dialog_starts_from_the_last_run_of_the_chunk(tmp_path, qt_app, fake_rtklib, monkeypatch):
    """The dialog starts from the last PPK run of the chunk: the base, its typed position, the navigation files still
    there (not those beside the drone's observations of another flight), the choice about the onboard fixes; the
    drone's observations too for the MRK of that run. The same base typed elsewhere for other photos of the chunk is a
    warning in the dialog; the photos of the chunk with an onboard fix are counted beside the choice."""
    from pyarchinit_photogrammetry.gui.ppk_dialog import PpkDialog

    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    base = _station(tmp_path)
    extra = tmp_path / "effemeridi"
    extra.mkdir()
    brdc = Path(shutil.copyfile(DATA / "30400920.05n", extra / "brdc0920.05n"))
    drone = tmp_path / "drone"
    drone.mkdir()
    rover = Path(shutil.copyfile(PPK_ROVER, drone / "volo1_Rinex.obs"))
    rover_nav = Path(shutil.copyfile(DATA / "30400920.05n", drone / "volo1_Rinex.nav"))
    first = _ppk_flight(tmp_path / "volo1", [(i, START_2005 + 30.0 * i, (0, 0, 150)) for i in (1, 2)])
    ch = ppg.Document().add_chunk()
    ch.add_photos([first])
    assert PpkDialog(first / "Timestamp.MRK", chunk=ch).base.path() is None  # no run yet: nothing to start from

    solution = ppk.solve(rover, base, [base.parent / "07590920.05n", brdc, rover_nav], (35.1609, 139.6138, 71.25),
                         folder=tmp_path / "work")
    gnss_apply.apply_ppk(ch, first / "Timestamp.MRK", solution, replace_fix=True)
    ch.cameras[0].meta["gnss"] = {**ch.cameras[0].meta["gnss"], "source": "dji_xmp", "status": "fix"}

    # another flight: its own drone file, the base and its typed position of the last run, its navigation files but
    # the one beside the drone file of the other flight, the onboard fixes replaced again
    card = _dji_card(tmp_path)
    dlg = PpkDialog(card / f"{DJI_FLIGHT}_Timestamp.MRK", chunk=ch)
    assert dlg.rover.path() == card / f"{DJI_FLIGHT}_Rinex.obs" and dlg.base.path() == base
    assert dlg.typed.isChecked() and (dlg.lat.text(), dlg.lon.text(), dlg.ellh.text()) == ("35.1609", "139.6138",
                                                                                           "71.25")
    assert dlg.nav_paths() == [card / f"{DJI_FLIGHT}_PPKNAV.nav", base.parent / "07590920.05n", brdc]
    assert dlg.replace_fix.isChecked() and dlg.fix_note.text().startswith("1 foto del chunk hanno un fix RTK")
    assert dlg.problem() is None and not dlg.reuse_warning.isVisibleTo(dlg)
    assert dlg.values()["base_position"] == (35.1609, 139.6138, 71.25)
    # the same base typed 11 m away: a warning in the dialog, not a refusal
    dlg.lat.setText("35.1610")
    assert dlg.reuse_warning.isVisibleTo(dlg) and dlg.problem() is None
    said = re.match(r"La base 0759 è già stata usata in questo chunk nella posizione scritta 35,16090000, "
                    r"139,61380000, 71,250 m, a (\d+),(\d\d) m da questa: se è lo stesso punto, una delle due è "
                    r"sbagliata", dlg.reuse_warning.text())
    assert said and 10.0 < float(f"{said.group(1)}.{said.group(2)}") < 12.0, dlg.reuse_warning.text()
    dlg.lat.setText("35.1609")
    assert not dlg.reuse_warning.isVisibleTo(dlg)
    # the flight of the last run: its drone file too; a navigation file gone is not listed
    brdc.unlink()
    again = PpkDialog(first / "Timestamp.MRK", chunk=ch)
    assert again.rover.path() == rover and again.base.path() == base
    assert again.nav_paths() == [rover_nav, base.parent / "07590920.05n"]
    # the photos of this flight are given their positions again: the base typed elsewhere for them is no warning
    again.lat.setText("35.1610")
    assert not again.reuse_warning.isVisibleTo(again)
    # a run with the position of the header: the dialog starts there
    gnss_apply.apply_ppk(ch, first / "Timestamp.MRK", ppk.solve(rover, base, folder=tmp_path / "work"))
    header = PpkDialog(card / f"{DJI_FLIGHT}_Timestamp.MRK", chunk=ch)
    assert header.from_header.isChecked() and header.lat.text() == "" and not header.replace_fix.isChecked()
    # a run with a base in several files: the dialog starts with them ("2 file"); one of them gone, without them
    piece = Path(shutil.copyfile(base, base.parent / "07590920_2.05o"))
    ch.meta[ppk.MEMORY]["last"]["base"] = [str(base), str(piece)]
    several = PpkDialog(card / f"{DJI_FLIGHT}_Timestamp.MRK", chunk=ch)
    assert several.base.paths() == [base, piece] and several.base.edit.text() == "2 file"
    assert several.rover.path() == card / f"{DJI_FLIGHT}_Rinex.obs"
    piece.unlink()
    assert PpkDialog(card / f"{DJI_FLIGHT}_Timestamp.MRK", chunk=ch).base.paths() == []
    # a run with a rover in several files (rinex.join): a dialog of the same MRK, which asks for one, does not take them
    ch.meta[ppk.MEMORY]["last"]["rover"] = [str(rover), str(rover)]
    pieces = PpkDialog(first / "Timestamp.MRK", chunk=ch)
    assert pieces.rover.path() is None and pieces.base.paths() == []


def _sample_answer(parent, mrk, work=None, chunk=None) -> dict:
    """What the PPK dialog answers in the window tests: the RTKLIB sample data, the base at its header's position, the
    onboard fixes kept."""
    return {"rover": PPK_ROVER, "base": PPK_BASE, "navs": [PPK_NAV], "base_position": None, "replace_fix": False}


@pytest.fixture
def ppk_window(tmp_path, qt_app):
    """A window with the saved project of the DJI flight of _DRONE_SHOTS (the photos with the GPS of their EXIF)."""
    from pyarchinit_photogrammetry.gui import main_window as mw

    project, folder = _ppk_project(tmp_path)
    w = mw.MainWindow(ppg.Document.open(project), autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qt_app.processEvents()
    yield w, folder, mw
    w.setWindowModified(False)
    w.close()
    qt_app.processEvents()


def test_ppk_step_of_the_gnss_pane(ppk_window, fake_rtklib, monkeypatch, tmp_path):
    """Calcola PPK...: the MRK, the files and the base asked (the dialogs replaced), RTKLIB in the background (the fake
    rnx2rtkp), the positions to the photos, the counts in the log, the base at the position of its header also in a
    message and in the pane; an error of the run said, nothing changed."""
    w, folder, mw = ppk_window
    pane, chunk = w.gnss_pane, w.chunk
    assert pane.b_ppk.isEnabled() and pane.states["ppk"].text().startswith("Nessuna posizione PPK")
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    errors, boxes, asked = [], [], []
    monkeypatch.setattr(w, "_error", lambda summary, details="", logged=True: errors.append(summary))
    monkeypatch.setattr(mw, "message", lambda parent, kind, title, text, *a, **k: boxes.append((kind, text)))
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(folder / "Timestamp.MRK"), ""))
    answer = {"rover": PPK_ROVER, "base": PPK_BASE, "navs": [PPK_NAV], "base_position": None, "replace_fix": False}

    def ask(parent, mrk, work=None, chunk=None):
        asked.append((Path(mrk), Path(work), chunk))
        return dict(answer)

    monkeypatch.setattr(mw.PpkDialog, "ask", staticmethod(ask))
    w.setWindowModified(False)

    pane.b_ppk.click()
    w.runner.wait()

    # the dialog was given the MRK, the work folder of the chunk, which no input may be in, and the chunk (its last run)
    assert errors == [] and asked == [(folder / "Timestamp.MRK", chunk.data_dir / "gnss" / "ppk", chunk)]
    states = {c.label: c.meta["gnss"]["status"] for c in chunk.cameras if c.meta["gnss"]["source"] == "ppk"}
    assert states == {"DJI_0002": "fix", "DJI_0003": "float", "DJI_0005": "float", "DJI_0006": "single",
                      "DJI_0007": "float", "DJI_0008": "fix"}
    run = chunk.camera("DJI_0002").meta["gnss"]["solution"]  # the solution file of the run, kept in the work folder
    assert re.fullmatch(RUN_NAME + r"\.pos", run) and (chunk.data_dir / "gnss" / "ppk" / run).is_file()
    assert w.isWindowModified() and ppk.last_run(chunk)["solution"] == run
    text = pane.states["ppk"].text()
    assert text.startswith("PPK: 6 foto su 9 (float 3, fix 2, singola 1).") and "0759" in text
    assert "intestazione RINEX" in text and "metri" in text
    assert "PPK 6" in pane.states["positions"].text()
    logged = [entry[2] for entry in w.log_pane.entries]
    assert "PPK: 6 foto su 9 hanno la posizione corretta del loro scatto (float 3, fix 2, singola 1)" in logged
    assert "Senza corrispondenza (3): DJI_0001, DJI_0004, DJI_0009" in logged
    assert "Scatti dell'MRK senza posizione PPK (3): 1, 4, 9" in logged
    # the base, how far from the flight, the solution; the warnings of the run, the antenna of the base among them
    assert any(re.fullmatch(rf"Base 0759 \(antenna TRM29659\.00\): posizione dell'intestazione RINEX, sistema "
                            rf"sconosciuto \(WGS 84\); a 3,\d km dal volo; soluzione {run}", t) for t in logged), logged
    assert any(t.startswith("the position of the base 0759 comes from its RINEX header") for t in logged)
    assert any(t.startswith("the phase-centre offset of the antenna TRM29659.00 of the base 0759 is not applied")
               for t in logged)
    assert any(t.startswith(f"3 photos were taken where the solution {run} has no position") for t in logged)
    ((kind, said),) = boxes  # the base at the position of its header: also in a message
    assert kind == "warning" and "0759" in said and "intestazione RINEX" in said and "metri" in said

    # the base at a typed position: no message, the pane says it; 3 km from the header's, the log says that too; the
    # position kept longitude first, a solution file of its own
    boxes.clear()
    answer["base_position"] = (35.132, 139.623, 70.25)
    w.gnss_ppk()
    w.runner.wait()
    assert errors == [] and boxes == []
    assert "Base 0759: posizione scritta, sistema sconosciuto (WGS 84)." in pane.states["ppk"].text()
    assert "intestazione" not in pane.states["ppk"].text()
    assert chunk.camera("DJI_0002").meta["gnss"]["base"]["position"] == [139.623, 35.132, 70.25]
    assert chunk.camera("DJI_0002").meta["gnss"]["solution"] != run
    assert any(re.match(r"the typed position of the base 0759 is 3\.\d km away horizontally", entry[2])
               for entry in w.log_pane.entries)

    # an error of the run: said, nothing changed
    before = _snapshot(chunk)
    answer["rover"] = _rinex2(tmp_path / "rover.24o", V2_HEADER)
    w.setWindowModified(False)
    w.gnss_ppk()
    w.runner.wait()
    assert len(errors) == 1 and errors[0].startswith("Calcolo PPK: ") and "no time in common" in errors[0]
    assert _snapshot(chunk) == before and not w.isWindowModified()

    # in the menu Strumenti > GNSS
    menu = next(a.menu() for a in w.menuBar().actions() if a.text() == "&Strumenti")
    gnss = next(a.menu() for a in menu.actions() if a.menu() is not None and a.text() == "GNSS")
    assert [a.text() for a in gnss.actions()] == ["File MRK di DJI...", "File di posizioni...", "Correzioni PPK...",
                                                 "Pannello GNSS"]


def test_ppk_step_asks_for_rtklib_and_for_the_photos(ppk_window, fake_rtklib, monkeypatch, tmp_path):
    """Without RTKLIB the pane and the command say what to install before asking anything; an MRK copied away from
    its photos asks for their folder, and nothing runs without it."""
    w, folder, mw = ppk_window
    pane, chunk = w.gnss_pane, w.chunk
    said = []
    monkeypatch.setattr(w, "_warn", lambda text: said.append(text))
    rnx2rtkp = os.environ["PPG_RNX2RTKP"]
    monkeypatch.delenv("PPG_RNX2RTKP")  # and the native core is switched off
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: pytest.fail("a file was asked for"))
    pane.set_state(chunk)
    assert "nucleo nativo" in pane.states["ppk"].text()
    w.gnss_ppk()
    assert len(said) == 1 and "nucleo nativo" in said[0] and "pyarchinit-photogrammetry-core" in said[0]

    monkeypatch.setenv("PPG_RNX2RTKP", rnx2rtkp)
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    card = tmp_path / "scheda"
    card.mkdir()
    shutil.copyfile(folder / "Timestamp.MRK", card / "Timestamp.MRK")
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(card / "Timestamp.MRK"), ""))
    monkeypatch.setattr(mw.PpkDialog, "ask", staticmethod(_sample_answer))
    monkeypatch.setattr(mw, "message", lambda *a, **k: None)
    monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", lambda *a, **k: "")
    w.gnss_ppk()  # the folder of the photos not given: nothing runs
    w.runner.wait()
    assert fake_rtklib() == [] and all(c.meta["gnss"]["source"] == "exif" for c in chunk.cameras)
    monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))
    w.gnss_ppk()
    w.runner.wait()
    assert len(fake_rtklib()) == 1 and sum(c.meta["gnss"]["source"] == "ppk" for c in chunk.cameras) == 6


def test_ppk_step_waits_for_a_running_step(ppk_window, monkeypatch):
    """While another step runs *Calcola PPK* is off, also after the pane is read again, and the command asks nothing:
    it says in the status bar what the menu would."""
    w, folder, mw = ppk_window
    pane = w.gnss_pane
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: pytest.fail("a file was asked for"))
    assert pane.b_ppk.isEnabled()
    w._busy(True)
    assert not pane.b_ppk.isEnabled()
    pane.set_state(w.chunk)
    assert not pane.b_ppk.isEnabled()
    w._busy(False)
    assert pane.b_ppk.isEnabled()
    w.runner.thread = object()  # a step running (put back before the window closes, which would wait for it)
    try:
        assert w.runner.busy
        w.gnss_ppk()
        assert w.statusBar().currentMessage() == "Un'elaborazione e' gia' in corso"
    finally:
        w.runner.thread = None


def test_ppk_step_says_when_no_photo_took_a_position(ppk_window, fake_rtklib, monkeypatch, tmp_path):
    """A solution that has no position at any shot (every one in a gap): said in a message, the warnings of the run in
    the log, nothing changed, no message about the base."""
    w, folder, mw = ppk_window
    chunk = w.chunk
    pos = tmp_path / "gaps.pos"
    pos.write_text(_pos_text(rows=[(START_2005 + 10.0, 35.1320660, 139.6243010, 75.690, 1, 7, 0.004, 0.003, 0.009),
                                   (START_2005 + 3590.0, 35.1320670, 139.6243020, 75.700, 1, 7, 0.004, 0.003, 0.009)]),
                   newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    said = []
    monkeypatch.setattr(w, "_warn", lambda text: said.append(text))
    monkeypatch.setattr(mw, "message", lambda *a, **k: pytest.fail("a message about the base of a run that placed "
                                                                    "nothing"))
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(folder / "Timestamp.MRK"), ""))
    monkeypatch.setattr(mw.PpkDialog, "ask", staticmethod(_sample_answer))
    before = _snapshot(chunk)
    w.setWindowModified(False)

    w.gnss_ppk()
    w.runner.wait()

    assert said == ["Nessuna foto del chunk ha preso una posizione PPK: controlla la cartella delle foto e che l'MRK "
                    "sia del volo delle osservazioni del drone."]
    assert _snapshot(chunk) == before and not w.isWindowModified() and len(fake_rtklib()) == 1
    assert ppk.last_run(chunk) is None  # a run that placed nothing is not the run the next dialog starts from
    logged = [entry[2] for entry in w.log_pane.entries]
    assert any(re.match(rf"9 photos were taken where the solution {RUN_NAME}\.pos has no position", t) for t in logged)
    assert any(t.startswith("the position of the base 0759 comes from its RINEX header") for t in logged)


def test_ppk_step_says_when_the_onboard_fixes_kept_their_positions(ppk_window, fake_rtklib, monkeypatch, tmp_path):
    """Every photo of the flight has an RTK fix of the drone itself and keeps it (those shot where the solution has no
    position too): nothing changed, and a message says how to give them the PPK position; with some photos placed,
    the log counts those that kept their fix."""
    w, folder, mw = ppk_window
    chunk = w.chunk
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    infos = []
    monkeypatch.setattr(w, "_info", lambda title, text: infos.append(text))
    monkeypatch.setattr(w, "_warn", lambda text: pytest.fail(f"a warning: {text}"))
    monkeypatch.setattr(mw, "message", lambda *a, **k: None)
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(folder / "Timestamp.MRK"), ""))
    monkeypatch.setattr(mw.PpkDialog, "ask", staticmethod(_sample_answer))
    for cam in chunk.cameras:  # as the XMP of a DJI drone with an RTK fix gives them
        cam.meta["gnss"] = {**cam.meta["gnss"], "source": "dji_xmp", "status": "fix"}
    before = _snapshot(chunk)
    w.setWindowModified(False)

    w.gnss_ppk()
    w.runner.wait()

    assert infos == ["Nessuna foto ha preso la posizione PPK: 9 foto di questo volo hanno un fix RTK del drone stesso "
                     "e lo tengono. Per dar loro la posizione PPK calcola di nuovo con «Sostituisci anche i fix RTK "
                     "del drone»."]
    assert _snapshot(chunk) == before and not w.isWindowModified()
    # two photos without the fix take the PPK position, the others keep their fix: counted in the log
    for label in ("DJI_0002", "DJI_0003"):
        chunk.camera(label).meta["gnss"]["status"] = "float"
    w.gnss_ppk()
    w.runner.wait()
    assert {c.label for c in chunk.cameras if c.meta["gnss"]["source"] == "ppk"} == {"DJI_0002", "DJI_0003"}
    logged = [entry[2] for entry in w.log_pane.entries]
    assert "Foto che hanno tenuto il fix RTK del drone stesso: 7" in logged and w.isWindowModified()


# --- the base placed in five ways, the stations near the survey ----------------------------------------------------

AQUI = stations.Station("AQUI00ITA", "L'Aquila", ("EPN", "RING"), 42.368239, 13.350248, 712.986,
                        (4592507.5523, 1089876.3523, 4276392.9031))
M0SE = stations.Station("M0SE00ITA", "Rome - The 'Moses' of Michelangelo", ("EPN", "IGS"), 41.893113, 12.493258,
                        120.554, (4642432.52, 1028629.389, 4236854.206))
INGR = stations.Station("INGR00ITA", "Roma", ("RING",), 41.828, 12.514667, 98.0, (4644563.0, 1030160.0, 4231470.0))
MEDI = stations.Station("MEDI00ITA", "Medicina(BO)", ("EPN", "IGS"), 44.519956, 11.646813, 50.043,
                        (4461400.47, 919593.86, 4449504.94))


def test_ppk_dialog_base_modes(tmp_path, qt_app, monkeypatch):
    """The five ways to place the base and what values() gives for each: from the RINEX header, typed in a frame (in
    ITRF2020 with the epoch of its coordinates, the flight's by default), a permanent station as the base (no base
    file: its observations are downloaded), a static baseline from a station, a static PPP; the station chosen in the
    stations dialog near the flight; what is downloaded said for each; the base in several files; the calibration of
    the base antenna downloaded once (on by default)."""
    from pyarchinit_photogrammetry.gnss import base as gbase
    from pyarchinit_photogrammetry.gui import ppk_dialog
    from pyarchinit_photogrammetry.gui.ppk_dialog import PpkDialog

    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))  # no copy of igs20.atx
    card = _dji_card(tmp_path)
    base = _station(tmp_path)
    dlg = PpkDialog(card / f"{DJI_FLIGHT}_Timestamp.MRK")
    _choose(dlg, dlg.base, base)
    # from the header, the default: no frame, no epoch, no station; igs20.atx downloaded once when missing
    assert dlg.mode() == "header" and dlg.problem() is None
    v = dlg.values()
    assert (v["base_mode"], v["base_position"], v["base_frame"], v["base_epoch"], v["station"], v["atx"]) == (
        "header", None, None, None, None, "download")
    assert dlg.downloads.text() == ("Da scaricare: igs20.atx, le calibrazioni delle antenne dell'IGS (62 MB), una "
                                    "volta sola.")
    assert not dlg.b_station.isVisibleTo(dlg) and not dlg.frame.isEnabled()
    # typed, in the frame of its coordinates: ETRF2000 has no epoch; ITRF2020 needs one, the flight's by default
    dlg.typed.setChecked(True)
    for edit, text in ((dlg.lat, "35,1609"), (dlg.lon, "139,6138"), (dlg.ellh, "71,25")):
        edit.setText(text)
    assert [(dlg.frame.itemData(i), dlg.frame.itemText(i)) for i in range(dlg.frame.count())] == [
        ("unknown", "Sconosciuto (WGS 84)"), ("ITRF2020", "ITRF2020"), ("ETRF2000", "ETRF2000 (RDN2008)"),
        ("ETRF2014", "ETRF2014"), ("ETRF2020", "ETRF2020")]
    v = dlg.values()
    assert (v["base_mode"], v["base_position"], v["base_frame"], v["base_epoch"]) == ("typed", (35.1609, 139.6138,
                                                                                               71.25), "unknown", None)
    dlg.frame.setCurrentIndex(dlg.frame.findData("ETRF2000"))
    assert dlg.values()["base_frame"] == "ETRF2000" and not dlg.epoch.isVisibleTo(dlg)
    dlg.frame.setCurrentIndex(dlg.frame.findData("ITRF2020"))
    shot = dji.read_mrk(card / f"{DJI_FLIGHT}_Timestamp.MRK")[0]
    # no epoch written: the middle of the drone's observations, the rule of the run and of ppg gnss ppk
    info = rinex.header(dlg.rover.path())
    middle = gbase.middle_epoch(info.first, info.last)
    assert f"{middle:.4f}" == f"{gbase.flight_epoch(dlg.rover.path()):.4f}"
    assert dlg.epoch.isVisibleTo(dlg) and dlg.epoch.text() == ""
    assert dlg.epoch.placeholderText() == (f"vuota: {middle:.4f}".replace(".", ",") +
                                           ", la metà delle osservazioni del drone")
    assert dlg.problem() is None and dlg.values()["base_epoch"] is None
    dlg.epoch.setText("2026-10-27")  # a date is an epoch too
    assert dlg.values()["base_epoch"] == pytest.approx(frames.decimal_year(datetime(2026, 10, 27)))
    dlg.epoch.setText("ieri")
    assert dlg.problem().startswith("Scrivi l'epoca della posizione in ITRF2020")
    dlg.epoch.setText("2026,82")
    assert dlg.problem() is None and dlg.values()["base_epoch"] == 2026.82
    # a permanent station as the base: no file of the base, the station chosen near the flight, what is downloaded
    dlg.from_station.setChecked(True)
    assert dlg.mode() == "station" and not dlg.base.isEnabled() and dlg.b_station.isVisibleTo(dlg)
    assert dlg.problem() == "Scegli la stazione permanente (Scegli la stazione...)."
    asked = []

    def choose(parent, lat, lon, when=None):
        asked.append((lat, lon, when))
        return AQUI

    monkeypatch.setattr(ppk_dialog.StationsDialog, "ask", staticmethod(choose))
    dlg.b_station.click()
    ((lat, lon, when),) = asked  # where the drone flew and when
    assert (round(lat, 4), round(lon, 4), when) == (round(shot.lat, 4), round(shot.lon, 4), shot.utc)
    assert dlg.station_info.text().startswith("AQUI00ITA - L'Aquila (EPN, RING), a ")
    assert dlg.problem() is None
    v = dlg.values()
    assert (v["base_mode"], v["base"], v["station"], v["base_position"], v["base_frame"]) == (
        "station", None, AQUI, None, None)
    assert dlg.downloads.text().startswith("Da scaricare: le coordinate della stazione AQUI00ITA (EPN, IGS o RING) e "
                                           "le sue osservazioni per le ore del volo, a 1 s o a 30 s")
    assert "i file di navigazione del volo, se mancano" in dlg.downloads.text()
    assert dlg.downloads.text().endswith("Le posizioni delle foto saranno in ITRF2020, all'epoca del volo.")
    assert dlg.atx_download.isChecked() and not dlg.atx_download.isEnabled()  # downloaded for a station anyway
    # a static baseline from the station: the files of the base and the station
    dlg.by_baseline.setChecked(True)
    assert dlg.base.isEnabled() and dlg.problem() is None
    v = dlg.values()
    assert (v["base_mode"], v["base"], v["station"]) == ("baseline", base, AQUI)
    assert "una linea di base statica vuole un'ora o più" in dlg.downloads.text()
    assert "saranno in ITRF2020, all'epoca della sessione della tua base" in dlg.downloads.text()
    # a static PPP: the files of the base, no station
    dlg.by_ppp.setChecked(True)
    assert not dlg.b_station.isVisibleTo(dlg) and dlg.problem() is None
    v = dlg.values()
    assert (v["base_mode"], v["base"], v["station"]) == ("ppp", base, None)
    assert "i prodotti precisi" in dlg.downloads.text() and "4 ore" in dlg.downloads.text()
    # the base in several files: added with Aggiungi..., the field says how many, values() gives the list
    pieces = [Path(shutil.copyfile(base, base.parent / f"pezzo{i}.05o")) for i in (1, 2)]
    monkeypatch.setattr(ppk_dialog.QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(p) for p in pieces], ""))
    dlg.base.b_add.click()
    dlg.refresh()
    assert dlg.base.edit.text() == "3 file" and dlg.base.paths() == [base, *pieces]
    assert dlg.values()["base"] == [base, *pieces] and dlg.problem() is None
    assert dlg.base_info.text().startswith("Base 0759: 3 file, ricevitore TRIMBLE 5700")
    for pattern in ("*.obs", "*.??o", "*.??d", "*.crx", "*.rnx", "*.gz", "*.Z", "*.zip"):  # every way a base comes
        assert pattern in dlg.base.filters and pattern in dlg.rover.filters
    # back to the header: the files of the base, the atx choice of the user again
    dlg.from_header.setChecked(True)
    assert dlg.atx_download.isEnabled() and dlg.values()["station"] is None


def test_ppk_dialog_reads_packed_files_in_the_background(tmp_path, qt_app, monkeypatch):
    """A file of the base packed in gzip (as Hatanaka, the Unix compress or zip) is read in a thread of its own, never
    in the thread of the interface: meanwhile the dialog says it is reading, and OK waits; then it describes it."""
    from pyarchinit_photogrammetry.gui.ppk_dialog import PpkDialog

    card = _dji_card(tmp_path)
    folder = tmp_path / "pacchi"
    folder.mkdir()
    packed = folder / "07590920.05o.gz"
    packed.write_bytes(gzip.compress(PPK_BASE.read_bytes()))
    shutil.copyfile(PPK_NAV, folder / "07590920.05n")
    read = []
    real = rinex.header
    monkeypatch.setattr(rinex, "header", lambda path: read.append(
        (Path(path).name, threading.current_thread() is threading.main_thread())) or real(path))
    dlg = PpkDialog(card / f"{DJI_FLIGHT}_Timestamp.MRK")
    _choose(dlg, dlg.base, packed)
    assert dlg.base_info.text() == "Lettura di 07590920.05o.gz in corso..."
    assert dlg.problem() == "Il programma sta ancora leggendo 07590920.05o.gz: attendi un momento."
    assert _wait_until(lambda: dlg.base_info.text().startswith("Base 0759")), dlg.base_info.text()
    assert ("07590920.05o.gz", False) in read and ("07590920.05o.gz", True) not in read
    assert dlg.nav_paths() == [card / f"{DJI_FLIGHT}_PPKNAV.nav", folder / "07590920.05n"]
    assert dlg.problem() is None
    count = len(read)
    dlg.refresh()  # read once while the file stays the same
    assert len(read) == count


def test_stations_dialog_lists_by_distance(qt_app, monkeypatch):
    """The permanent stations near the survey, nearest first, within the distance of a base: the list read in a thread
    (no file of any station looked for: that is done when one is used), the networks, the distance in km; OK only
    with a station chosen."""
    from PySide6.QtWidgets import QDialogButtonBox

    from pyarchinit_photogrammetry.gui.stations_dialog import StationsDialog

    calls = []
    monkeypatch.setattr(stations, "station_list", lambda refresh=False, progress=None: calls.append(
        (refresh, threading.current_thread() is threading.main_thread())) or [AQUI, INGR, MEDI, M0SE])
    monkeypatch.setattr(stations, "download_observations", lambda *a, **k: pytest.fail("a file was looked for"))
    rome = (41.9028, 12.4964)
    dlg = StationsDialog(*rome, datetime(2026, 10, 27, 9, 30))
    assert _wait_until(lambda: dlg.table.rowCount() > 0)
    assert calls == [(False, False)]
    heads = [dlg.table.horizontalHeaderItem(c).text() for c in range(dlg.table.columnCount())]
    assert heads == ["Stazione", "Reti", "Distanza (km)"]
    rows = [[dlg.table.item(r, c).text() for c in range(3)] for r in range(dlg.table.rowCount())]
    geod = Geod(ellps="GRS80")
    km = [geod.inv(rome[1], rome[0], s.lon, s.lat)[2] / 1000.0 for s in (M0SE, INGR, AQUI)]
    assert rows == [["M0SE00ITA - Rome - The 'Moses' of Michelangelo", "EPN, IGS", f"{km[0]:.1f}".replace(".", ",")],
                    ["INGR00ITA - Roma", "RING", f"{km[1]:.1f}".replace(".", ",")],
                    ["AQUI00ITA - L'Aquila", "EPN, RING", f"{km[2]:.1f}".replace(".", ",")]]  # Medicina: 290 km
    assert "27/10/2026" in dlg.intro.text() and "quando la stazione viene usata" in dlg.intro.text()
    assert dlg.status.text() == "3 stazioni entro 200 km."
    ok = dlg.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert dlg.selected() is None and not ok.isEnabled()
    dlg.table.selectRow(1)
    assert dlg.selected() == INGR and ok.isEnabled()


def test_stations_dialog_offline_retry(qt_app, monkeypatch):
    """Without a connection and without a saved list: what went wrong in the dialog; Riprova reads the list again."""
    from pyarchinit_photogrammetry.gui.stations_dialog import StationsDialog

    calls = []

    def listing(refresh=False, progress=None):
        calls.append(refresh)
        if len(calls) == 1:
            raise download.DownloadError("https://gnss-metadata.eu/v1/sitelog/geojson?network=EPN&pagination=0: could "
                                         "not connect (offline) (after 3 attempts)", "https://gnss-metadata.eu")
        return [AQUI, M0SE]

    monkeypatch.setattr(stations, "station_list", listing)
    dlg = StationsDialog(41.9028, 12.4964)
    assert _wait_until(lambda: dlg.b_retry.isVisibleTo(dlg))
    assert dlg.status.text().startswith("L'elenco delle stazioni non si scarica: https://gnss-metadata.eu")
    assert dlg.status.text().endswith("Controlla la connessione e premi Riprova.") and dlg.table.rowCount() == 0
    dlg.b_retry.click()
    assert _wait_until(lambda: dlg.table.rowCount() == 2)
    assert not dlg.b_retry.isVisibleTo(dlg) and calls == [False, False]


def _station_downloads(monkeypatch, tmp_path: Path, station: stations.Station) -> dict:
    """The archives of the stations replaced: the observations of ``station`` for a flight are those of 0759 of the
    RTKLIB sample data, its coordinates those of their header in ITRF2020, the navigation messages those of 0759; no
    igs20.atx can be had. Returns the calls, by what was asked."""
    calls: dict[str, list] = {"observations": [], "coordinates": [], "navigation": [], "atx": []}
    mark = rinex.header(PPK_BASE).approx_xyz

    def observations(asked, start, end, folder, progress=None, rate="auto"):
        calls["observations"].append((asked.id, start, end, Path(folder)))
        Path(folder).mkdir(parents=True, exist_ok=True)
        return Path(shutil.copyfile(PPK_BASE, Path(folder) / f"{asked.id}_flight.obs"))

    def coordinates(asked, epoch, progress=None):
        calls["coordinates"].append((asked.id, epoch))
        return stations.StationCoordinates(asked, tuple(mark), rinex.ecef_to_llh(mark), "ITRF2020", epoch, "IGS",
                                           (0.001, 0.001, 0.002), "RTKLIB sample data")

    def navigation(start, end, folder, progress=None):
        calls["navigation"].append((start, end, Path(folder)))
        Path(folder).mkdir(parents=True, exist_ok=True)
        return [Path(shutil.copyfile(PPK_NAV, Path(folder) / "BRDC00WRD_S_20050920000_01D_MN.rnx"))]

    def no_atx(refresh=False, progress=None):
        calls["atx"].append(refresh)
        raise download.DownloadError("https://files.igs.org/pub/station/general/igs20.atx: could not connect", "")

    monkeypatch.setattr(stations, "station_list", lambda refresh=False, progress=None: [station])
    monkeypatch.setattr(stations, "download_observations", observations)
    monkeypatch.setattr(stations, "coordinates", coordinates)
    monkeypatch.setattr(stations, "download_navigation", navigation)
    monkeypatch.setattr(atx, "atx_file", no_atx)
    return calls


def _rover_alone(tmp_path: Path) -> Path:
    """The drone's observations in a folder without any navigation file."""
    folder = tmp_path / "drone_solo"
    folder.mkdir()
    return Path(shutil.copyfile(PPK_ROVER, folder / "3040_Rinex.obs"))


GSI_0759 = stations.Station("0759", "GSI 0759", ("IGS",), *rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz),
                            tuple(rinex.header(PPK_BASE).approx_xyz))


def test_ppk_step_with_a_permanent_station_as_the_base(ppk_window, fake_rtklib, monkeypatch, tmp_path):
    """A permanent station as the base of the flight: its observations for the hours of the flight and its coordinates
    in ITRF2020 at the middle of the flight downloaded in the background, the navigation messages too when none is
    beside the drone's file; the photos in ITRF2020 at the epoch of the base; the log and the pane name the station,
    the frame and the epoch."""
    w, folder, mw = ppk_window
    pane, chunk = w.gnss_pane, w.chunk
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    calls = _station_downloads(monkeypatch, tmp_path, GSI_0759)
    errors = []
    monkeypatch.setattr(w, "_error", lambda summary, details="", logged=True: errors.append(summary))
    monkeypatch.setattr(mw, "message", lambda *a, **k: pytest.fail("a message for a base of a station"))
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(folder / "Timestamp.MRK"), ""))
    rover = _rover_alone(tmp_path)
    answer = {"rover": rover, "base": None, "navs": [], "base_position": None, "replace_fix": False,
              "base_mode": "station", "base_frame": None, "base_epoch": None, "station": GSI_0759, "atx": "download"}
    monkeypatch.setattr(mw.PpkDialog, "ask", staticmethod(lambda *a, **k: dict(answer)))

    w.gnss_ppk()
    w.runner.wait()

    assert errors == []
    start, end = rinex.time_span(rover)
    gnss = chunk.data_dir / "gnss"
    ((sid, first, last, where),) = calls["observations"]
    assert (sid, first, last, where) == ("0759", start, end, gnss / "base")
    middle = frames.decimal_year(start + (end - start) / 2 - timedelta(seconds=13))
    assert [(s, round(e, 6)) for s, e in calls["coordinates"]] == [("0759", round(middle, 6))]
    assert calls["navigation"] == [(start, end, gnss / "base")]  # none beside the drone's file
    cam = chunk.camera("DJI_0002")
    assert cam.meta["reference_crs"] == "EPSG:9989" and cam.meta["gnss"]["frame"] == "ITRF2020"
    assert cam.meta["gnss"]["epoch"] == pytest.approx(middle)
    assert cam.meta["gnss"]["base"]["position_source"] == "station"
    logged = [entry[2] for entry in w.log_pane.entries]
    epoch = f"{middle:.2f}".replace(".", ",")
    assert any(re.fullmatch(rf"Base 0759 \(antenna TRM29659\.00\): coordinate della stazione permanente 0759 \(IGS\), "
                            rf"ITRF2020 all'epoca {epoch}; a 3,\d km dal volo; soluzione {RUN_NAME}\.pos", t)
               for t in logged), logged
    assert f"Base 0759: coordinate della stazione permanente 0759 (IGS), ITRF2020 all'epoca {epoch}." in (
        pane.states["ppk"].text())
    # the next dialog starts from the station
    assert ppk.last_run(chunk)["station"]["id"] == "0759" and ppk.last_run(chunk)["position_source"] == "station"


def test_ppk_dialog_starts_from_a_station_of_the_last_run(tmp_path, qt_app, fake_rtklib, monkeypatch):
    """After a run with a permanent station as the base the dialog starts with that station, and after a typed base in
    a frame with that frame and its epoch."""
    from pyarchinit_photogrammetry.gui.ppk_dialog import PpkDialog

    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    flight = _ppk_flight(tmp_path / "volo", [(i, START_2005 + 30.0 * i, (0, 0, 150)) for i in (1, 2)])
    ch = ppg.Document().add_chunk()
    ch.add_photos([flight])
    lat, lon, h = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    detail = {"station": "0759", "name": "GSI 0759", "networks": ["IGS"],
              "coordinates": {"source": "IGS", "frame": "ITRF2020", "epoch": 2005.25,
                              "xyz": list(rinex.header(PPK_BASE).approx_xyz), "sigma_xyz": None}}
    position = ppk.BasePosition(lat, lon, h, "station", frame="ITRF2020", epoch=2005.25, detail=detail)
    gnss_apply.apply_ppk(ch, flight / "Timestamp.MRK", ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], position,
                                                                 folder=tmp_path / "work"))
    dlg = PpkDialog(flight / "Timestamp.MRK", chunk=ch)
    assert dlg.mode() == "station" and dlg.station.id == "0759" and dlg.station.networks == ("IGS",)
    assert dlg.station_info.text().startswith("0759 - GSI 0759 (IGS)")
    typed = ppk.BasePosition(35.1609, 139.6138, 71.25, "typed", frame="ITRF2020", epoch=2005.5)
    gnss_apply.apply_ppk(ch, flight / "Timestamp.MRK", ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], typed,
                                                                 folder=tmp_path / "work"))
    again = PpkDialog(flight / "Timestamp.MRK", chunk=ch)
    assert again.mode() == "typed" and again.frame.currentData() == "ITRF2020" and again.epoch.text() == "2005,5000"
    assert again.values()["base_position"] == (35.1609, 139.6138, 71.25)


def test_ppk_step_downloads_the_antenna_calibrations_once(ppk_window, fake_rtklib, monkeypatch, tmp_path):
    """With the calibration of the base antenna asked for (on by default), igs20.atx is downloaded in the background
    before RTKLIB when the program has no copy, once: RTKLIB is given it and the heights are corrected; without the
    option nothing is downloaded and the log says the offset is not applied, and how to have it."""
    w, folder, mw = ppk_window
    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))  # no copy of igs20.atx from another test
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    downloads = []

    def fetched(refresh=False, progress=None):
        downloads.append(threading.current_thread() is threading.main_thread())
        atx.atx_dir().mkdir(parents=True, exist_ok=True)
        return Path(shutil.copyfile(ATX_EXCERPT, atx.atx_dir() / atx.FILE_NAME))

    monkeypatch.setattr(atx, "atx_file", fetched)
    monkeypatch.setattr(mw, "message", lambda *a, **k: None)
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(folder / "Timestamp.MRK"), ""))
    answer = {**_sample_answer(None, None), "base_mode": "header", "atx": "auto"}
    monkeypatch.setattr(mw.PpkDialog, "ask", staticmethod(lambda *a, **k: dict(answer)))
    w.gnss_ppk()
    w.runner.wait()
    assert downloads == [] and fake_rtklib()[-1]["antenna"] is None
    logged = [entry[2] for entry in w.log_pane.entries]
    assert any("is not applied" in t and "ppg gnss atx --download" in t for t in logged)
    answer["atx"] = "download"
    for _run in range(2):
        w.gnss_ppk()
        w.runner.wait()
    assert downloads == [False]  # once, in the background
    assert fake_rtklib()[-1]["antenna"] is not None
    assert w.chunk.camera("DJI_0002").meta["gnss"]["base"]["antenna_calibration"]["entry"] == "TRM29659.00     NONE"


def test_cli_ppk_with_a_station_base(tmp_path, fake_rtklib, monkeypatch):
    """ppg gnss ppk --base-station: the station's observations for the flight and its coordinates downloaded (here
    replaced by the RTKLIB sample data), the navigation too when there is none beside the drone's file, the base line
    naming the station, the frame and the epoch; a station that is not in the list and a station with files of a base
    are refused."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    calls = _station_downloads(monkeypatch, tmp_path, GSI_0759)
    project, folder = _ppk_project(tmp_path)
    rover = _rover_alone(tmp_path)
    run = CliRunner().invoke
    command = ["gnss", "ppk", str(project), "--mrk", str(folder / "Timestamp.MRK"), "--rover", str(rover)]

    result = run(app, [*command, "--base-station", "0759"])

    assert result.exit_code == 0, result.output
    start, end = rinex.time_span(rover)
    middle = frames.decimal_year(start + (end - start) / 2 - timedelta(seconds=13))
    lines = result.output.splitlines()
    lat, lon, h = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    assert any(re.fullmatch(rf"base 0759: latitude {lat:.8f}, longitude {lon:.8f}, {h:.3f} m above the ellipsoid "
                            rf"\(the permanent station 0759, coordinates from IGS, ITRF2020 at {middle:.4f}\), "
                            r"antenna 0\.000 m above it; 3\.\d km from the flight", line) for line in lines), lines
    assert "navigation files downloaded for the flight: BRDC00WRD_S_20050920000_01D_MN.rnx" in lines
    assert [c[:3] for c in calls["observations"]] == [("0759", start, end)] and len(calls["navigation"]) == 1
    ch = ppg.Document.open(project).chunk
    gnss = ch.camera("DJI_0002").meta["gnss"]
    assert gnss["frame"] == "ITRF2020" and gnss["epoch"] == pytest.approx(middle)
    assert gnss["base"]["position_source"] == "station"
    # what cannot be: a station with the files of a base, two ways at once, a station that is not in the list
    for options, said in (([*command, "--base-station", "0759", "--base", str(PPK_BASE)], "--base-station"),
                          ([*command, "--base", str(PPK_BASE), "--base-ppp", "--base-position", "35,139,70"],
                           "one of"),
                          ([*command, "--base-station", "NOPE"], "NOPE")):
        result = run(app, options)
        assert result.exit_code != 0 and said in _plain_words(result.output), result.output
    # a typed base in ITRF2020 takes the epoch given, or that of the middle of the flight
    result = run(app, [*command, "--base", str(PPK_BASE), "--base-position", "35.1609,139.6138,71.25",
                       "--base-frame", "ITRF2020"])
    assert result.exit_code == 0, result.output
    assert f"(typed, ITRF2020 at {middle:.4f})" in result.output
    result = run(app, [*command, "--base", str(PPK_BASE), "--base-position", "35.1609,139.6138,71.25",
                       "--base-frame", "ETRF2000", "--base-epoch", "2005.3"])
    assert result.exit_code == 0, result.output
    assert "(typed, ETRF2000)" in result.output
    gnss = ppg.Document.open(project).chunk.camera("DJI_0002").meta["gnss"]
    assert gnss["frame"] == "ETRF2000" and gnss["base"]["frame"] == "ETRF2000"


def _placed_bases(monkeypatch) -> list[tuple]:
    """The static baseline (gnss.base.baseline) and the static PPP (gnss.ppp.solve_static) that place the user's base
    replaced: 0759 of the RTKLIB sample data placed at the position of its header in ITRF2020, with a warning each.
    Returns their calls."""
    from pyarchinit_photogrammetry.gnss import base as gbase
    from pyarchinit_photogrammetry.gnss import ppp

    llh = rinex.ecef_to_llh(rinex.header(PPK_BASE).approx_xyz)
    calls: list[tuple] = []

    def baseline(base_obs, station, folder, navs=None, progress=None, *, station_obs=None, station_position=None,
                 atx="auto"):
        calls.append(("baseline", station.id, Path(folder), atx))
        position = ppk.BasePosition(*llh, "baseline", frame="ITRF2020", epoch=2005.25, sd=(0.001, 0.001, 0.002),
                                    detail={"station": station.id})
        return gbase.BaselineResult(position, 2, (0.001, 0.001, 0.002), 3.3, 1.0, Path(folder) / "baseline.pos",
                                    ["the static baseline from 0759 is float: a few centimetres off"])

    def solve_static(obs, folder, products=None, atx="auto", progress=None):
        calls.append(("ppp", Path(folder), atx))
        position = ppk.BasePosition(*llh, "ppp", frame="ITRF2020", epoch=2005.25, sd=(0.01, 0.012, 0.03),
                                    detail={"centre": "ESA", "kind": "final"})
        return ppp.PppResult(position=position, hours=1.0,
                             products=ppp.Products([PPK_BASE], [PPK_BASE], [PPK_BASE], "ESA", "final"),
                             solution=Path(folder) / "ppp.pos",
                             warnings=["your base 0759 observed for 1 h: a static PPP of less than 2 hours is 10-20 cm "
                                       "off at best"])

    monkeypatch.setattr(gbase, "baseline", baseline)
    monkeypatch.setattr(ppp, "solve_static", solve_static)
    return calls


PLACED_WARNINGS = {"baseline": "the static baseline from 0759 is float: a few centimetres off",
                   "ppp": "your base 0759 observed for 1 h: a static PPP of less than 2 hours is 10-20 cm off at best"}


def _own_base_alone(tmp_path: Path) -> Path:
    """The user's base (0759 of the RTKLIB sample data) in a folder without any navigation file."""
    folder = tmp_path / "own_base"
    folder.mkdir()
    return Path(shutil.copyfile(PPK_BASE, folder / "0759_base.obs"))


@pytest.mark.parametrize("mode", ["baseline", "ppp"])
def test_ppk_step_with_the_base_placed_by_a_baseline_or_a_ppp(ppk_window, fake_rtklib, monkeypatch, tmp_path, mode):
    """The user's base placed by a static baseline from a permanent station or by a static PPP, then the PPK of the
    flight against it - the same set-up as ppg gnss ppk (gnss.base.prepare_base): in the GNSS folder of the chunk, the
    navigation of the flight downloaded when none is beside the drone's file or the base's; the photos in ITRF2020 at
    the epoch of the base; its warnings and the navigation downloaded in the log."""
    w, folder, mw = ppk_window
    chunk = w.chunk
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    downloads = _station_downloads(monkeypatch, tmp_path, GSI_0759)
    placed = _placed_bases(monkeypatch)
    errors = []
    monkeypatch.setattr(w, "_error", lambda summary, details="", logged=True: errors.append(summary))
    monkeypatch.setattr(mw, "message", lambda *a, **k: pytest.fail("a message for a computed base"))
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(folder / "Timestamp.MRK"), ""))
    rover = _rover_alone(tmp_path)
    answer = {"rover": rover, "base": [_own_base_alone(tmp_path)], "navs": [], "base_position": None,
              "replace_fix": False, "base_mode": mode, "base_frame": None, "base_epoch": None,
              "station": GSI_0759 if mode == "baseline" else None, "atx": "auto"}
    monkeypatch.setattr(mw.PpkDialog, "ask", staticmethod(lambda *a, **k: dict(answer)))

    w.gnss_ppk()
    w.runner.wait()

    assert errors == []
    gnss = chunk.data_dir / "gnss"
    assert placed == ([("baseline", "0759", gnss / "baseline", "auto")] if mode == "baseline" else
                      [("ppp", gnss / "ppp", "auto")])
    start, end = rinex.time_span(rover)
    assert downloads["navigation"] == [(start, end, gnss / "base")] and downloads["atx"] == []
    cam = chunk.camera("DJI_0002")
    assert cam.meta["gnss"]["frame"] == "ITRF2020" and cam.meta["gnss"]["epoch"] == pytest.approx(2005.25)
    assert cam.meta["gnss"]["base"]["position_source"] == mode
    logged = [entry[2] for entry in w.log_pane.entries]
    assert PLACED_WARNINGS[mode] in logged, logged
    assert "File di navigazione scaricati per il volo: BRDC00WRD_S_20050920000_01D_MN.rnx" in logged
    assert ppk.last_run(chunk)["position_source"] == mode


@pytest.mark.parametrize("mode", ["baseline", "ppp"])
def test_cli_ppk_with_the_base_placed_by_a_baseline_or_a_ppp(tmp_path, fake_rtklib, monkeypatch, mode):
    """ppg gnss ppk --base-baseline / --base-ppp: the user's base placed first, the warnings of the placing and the
    navigation downloaded for the flight printed, the base line naming how it was placed; --atx off gives a baseline
    no antenna calibrations and is refused for a PPP, which needs them, before anything runs."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    downloads = _station_downloads(monkeypatch, tmp_path, GSI_0759)
    placed = _placed_bases(monkeypatch)
    project, folder = _ppk_project(tmp_path)
    rover = _rover_alone(tmp_path)
    run = CliRunner().invoke
    command = ["gnss", "ppk", str(project), "--mrk", str(folder / "Timestamp.MRK"), "--rover", str(rover), "--base",
               str(_own_base_alone(tmp_path)), *(["--base-baseline", "0759"] if mode == "baseline" else ["--base-ppp"])]

    result = run(app, command)

    assert result.exit_code == 0, result.output
    gnss = ppg.Document.open(project).chunk.data_dir / "gnss"
    assert placed == ([("baseline", "0759", gnss / "baseline", "auto")] if mode == "baseline" else
                      [("ppp", gnss / "ppp", "auto")])
    lines = result.output.splitlines()
    assert f"warning: {PLACED_WARNINGS[mode]}" in lines
    assert "navigation files downloaded for the flight: BRDC00WRD_S_20050920000_01D_MN.rnx" in lines
    how = ("by a static baseline from the station 0759" if mode == "baseline" else
           "by a static PPP on the final products of ESA")
    assert f"({how}, ITRF2020 at 2005.2500)" in result.output
    assert len(downloads["navigation"]) == 1
    placed.clear()
    result = run(app, [*command, "--atx", "off"])
    if mode == "baseline":
        assert result.exit_code == 0 and placed == [("baseline", "0759", gnss / "baseline", None)], result.output
    else:
        assert result.exit_code == 2 and placed == [], result.output
        assert "a static PPP needs the antenna calibrations of the IGS" in _plain_words(result.output)


def test_cli_ppk_refuses_options_it_would_ignore(tmp_path, fake_rtklib, monkeypatch):
    """--base-frame and --base-epoch are those of --base-position: refused without it; --atx off with a permanent
    station as the base downloads no antenna calibrations."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    downloads = _station_downloads(monkeypatch, tmp_path, GSI_0759)
    project, folder = _ppk_project(tmp_path)
    run = CliRunner().invoke
    command = ["gnss", "ppk", str(project), "--mrk", str(folder / "Timestamp.MRK"), "--rover",
               str(_rover_alone(tmp_path))]
    for options in (["--base", str(PPK_BASE), "--base-frame", "ITRF2020"],
                    ["--base", str(PPK_BASE), "--base-epoch", "2005.25"],
                    ["--base-station", "0759", "--base-frame", "ETRF2000"]):
        result = run(app, [*command, *options])
        assert result.exit_code == 2, result.output
        assert "--base-frame and --base-epoch are those of --base-position" in _plain_words(result.output)
    assert downloads == {"observations": [], "coordinates": [], "navigation": [], "atx": []}
    result = run(app, [*command, "--base-station", "0759", "--atx", "off"])
    assert result.exit_code == 0, result.output
    assert len(downloads["observations"]) == 1 and downloads["atx"] == []
    result = run(app, [*command, "--base-station", "0759"])
    assert result.exit_code == 0 and downloads["atx"] == [False], result.output


def _single_row(path: Path, llh, q: int = 1) -> Path:
    """A static solution of RTKLIB: one row at the start of the RTKLIB sample hour."""
    path.write_text(_pos_text([(START_2005, *llh, q, 7, 0.0011, 0.0007, 0.0024)]), newline="")
    return path


def test_cli_baseline(tmp_path, fake_rtklib, monkeypatch):
    """ppg gnss baseline: the user's base placed by a static baseline from a permanent station (its downloads replaced,
    RTKLIB's single static solution given), what it found and how to use it in the PPK; a station that is not in the
    list is refused."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    own = DATA / "30400920.05o"  # 3040, still for the hour, is the user's base; 0759 the station
    llh = rinex.ecef_to_llh(rinex.header(own).approx_xyz)
    monkeypatch.setenv("PPG_FAKE_POS", str(_single_row(tmp_path / "static.pos", llh)))
    calls = _station_downloads(monkeypatch, tmp_path, GSI_0759)
    run = CliRunner().invoke

    result = run(app, ["gnss", "baseline", str(own), "--station", "0759", "--folder", str(tmp_path / "linea")])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    middle = calls["coordinates"][0][1]
    at = next(i for i, line in enumerate(lines) if line.startswith("your base"))  # after the progress
    assert lines[at] == (f"your base 3040: latitude {llh[0]:.8f}, longitude {llh[1]:.8f}, {llh[2]:.3f} m above the "
                         f"ellipsoid, ITRF2020 at {middle:.4f}")
    assert re.fullmatch(r"fix \(Q=1\), 3\.\d{3} km from the station 0759, 1\.00 hours together; standard deviations "
                        r"0\.0007 / 0\.0011 / 0\.0024 m \(east / north / up\)", lines[at + 1]), lines[at + 1]
    assert (f"in the PPK: --base-position {llh[0]:.8f},{llh[1]:.8f},{llh[2]:.4f} --base-frame ITRF2020 "
            f"--base-epoch {middle:.4f} (or --base-baseline 0759)") in lines
    assert calls["observations"][0][3] == tmp_path / "linea" / "station"
    result = run(app, ["gnss", "baseline", str(own), "--station", "XXXX"])
    assert result.exit_code == 1 and "XXXX" in result.output


def test_cli_ppp(tmp_path, monkeypatch):
    """ppg gnss ppp: the user's base placed by a static PPP (its computation replaced), what it found, the warnings and
    how to use it in the PPK."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.gnss import ppp

    own = DATA / "30400920.05o"
    llh = rinex.ecef_to_llh(rinex.header(own).approx_xyz)
    run = CliRunner().invoke
    found = ppk.BasePosition(*llh, "ppp", frame="ITRF2020", epoch=2005.25, sd=(0.01, 0.012, 0.03),
                             detail={"centre": "ESA", "kind": "final"})
    asked = []

    def solve_static(obs, folder, products=None, atx="auto", progress=None):
        asked.append((obs, Path(folder), atx))
        return ppp.PppResult(position=found, hours=1.0, products=ppp.Products([own], [own], [own], "ESA", "final"),
                             solution=Path(folder) / "ppp" / "solution.pos",
                             warnings=["your base 3040 observed for 1 h: a static PPP of less than 2 hours is 10-20 "
                                       "cm off at best"])

    monkeypatch.setattr(ppp, "solve_static", solve_static)
    result = run(app, ["gnss", "ppp", str(own), "--folder", str(tmp_path / "ppp")])
    assert result.exit_code == 0, result.output
    assert asked == [([own], tmp_path / "ppp", "auto")]
    lines = result.output.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("your base"))
    assert lines[at] == (f"your base 3040: latitude {llh[0]:.8f}, longitude {llh[1]:.8f}, {llh[2]:.3f} m above the "
                         "ellipsoid, ITRF2020 at 2005.2500")
    assert lines[at + 1] == ("static PPP on the final products of ESA, 1.00 hours; standard deviations 0.0100 / "
                             "0.0120 / 0.0300 m (east / north / up)")
    assert ("warning: your base 3040 observed for 1 h: a static PPP of less than 2 hours is 10-20 cm off at best"
            in lines)
    assert (f"in the PPK: --base-position {llh[0]:.8f},{llh[1]:.8f},{llh[2]:.4f} --base-frame ITRF2020 "
            "--base-epoch 2005.2500 (or --base-ppp)") in lines


def test_cli_atx(tmp_path, monkeypatch):
    """ppg gnss atx: where the program keeps igs20.atx, or that it has none and how to have it; --download brings it
    (here replaced); ppg gnss ppk --atx takes auto, download or off."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))
    run = CliRunner().invoke
    result = run(app, ["gnss", "atx"])
    assert result.exit_code == 0, result.output
    assert "igs20.atx: not in the program" in result.output and "ppg gnss atx --download" in result.output

    def fetched(refresh=False, progress=None):
        atx.atx_dir().mkdir(parents=True, exist_ok=True)
        return Path(shutil.copyfile(ATX_EXCERPT, atx.atx_dir() / atx.FILE_NAME))

    monkeypatch.setattr(atx, "atx_file", fetched)
    result = run(app, ["gnss", "atx", "--download"])
    assert result.exit_code == 0, result.output
    assert f"igs20.atx: {atx.atx_dir() / atx.FILE_NAME}" in result.output
    result = run(app, ["gnss", "ppk", str(tmp_path / "x.ppg"), "--mrk", str(PPK_NAV), "--rover", str(PPK_ROVER),
                       "--base", str(PPK_BASE), "--atx", "sometimes"])
    assert result.exit_code != 0 and "--atx" in _plain_words(result.output)


# --- ppg doctor ---------------------------------------------------------------------------------------------------


def test_doctor_says_where_the_ppk_runs(tmp_path, monkeypatch):
    """The PPK line of ppg doctor (and of Aiuto > Diagnostica ambiente): RTKLIB inside the native core with its version,
    the rnx2rtkp named by PPG_RNX2RTKP, or neither, with what to install."""
    from pyarchinit_photogrammetry import doctor
    from pyarchinit_photogrammetry.engine import isolate

    class Core:  # the native core 0.5
        __version__, API = "0.5.0", 8

        @staticmethod
        def ppk_solve(conf, infiles, outfile):
            return 0, ""

    class OldCore:  # 0.4, without RTKLIB
        __version__, API = "0.4.1", 7

    monkeypatch.setenv("PPG_NATIVE", "1")
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    monkeypatch.setattr(_native, "_core", lambda: Core)
    check = doctor.ppk_check()
    assert (check.name, check.level, check.detail) == ("PPK (RTKLIB)", doctor.OK,
                                                       "RTKLIB inside the native core 0.5.0 (API 8)")
    monkeypatch.setenv("PPG_NATIVE", "0")
    check = doctor.ppk_check()
    assert check.level == doctor.WARN and check.detail == ("RTKLIB is inside the native core 0.5.0, which is switched "
                                                           "off (PPG_NATIVE=0)")
    monkeypatch.setenv("PPG_NATIVE", "1")
    monkeypatch.setattr(_native, "_core", lambda: OldCore)
    check = doctor.ppk_check()
    assert check.level == doctor.WARN and check.detail == "the native core 0.4.1 has no RTKLIB (0.5 or later has it)"
    assert 'pip install -U "pyarchinit-photogrammetry-core>=0.5"' in check.hint and "PPG_RNX2RTKP" in check.hint
    monkeypatch.setattr(_native, "_core", lambda: None)
    assert doctor.ppk_check().detail == "no native core, and no rnx2rtkp named by PPG_RNX2RTKP"
    tool = _fake_rnx2rtkp(tmp_path)
    monkeypatch.setenv("PPG_RNX2RTKP", str(tool))
    check = doctor.ppk_check()
    assert (check.level, check.detail) == (doctor.OK, f"external rnx2rtkp named by PPG_RNX2RTKP: {tool}")
    monkeypatch.setenv("PPG_RNX2RTKP", str(tmp_path / "nowhere" / "rnx2rtkp"))
    check = doctor.ppk_check()
    assert check.level == doctor.WARN and "not a program that can run" in check.detail
    # in the report, after the native core
    monkeypatch.setattr(isolate, "run_isolated", lambda fn, timeout=None: None)  # no SpatiaLite probe here
    report = doctor.run_checks(gui=False)
    names = [c.name for c in report.checks]
    assert names.index("PPK (RTKLIB)") == names.index("Native core (Rust)") + 1
    assert "[!!] PPK (RTKLIB)" in report.text()
