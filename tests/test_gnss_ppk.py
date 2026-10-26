"""Post-processed kinematic positions: what the RINEX files of a base and a rover hold."""
from __future__ import annotations

import gzip
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from pyproj import Transformer

from pyarchinit_photogrammetry.gnss import rinex

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
           "ABCD00ITA_R_20241330000_01D_30S_MO.crx")
    assert _names(rinex.find_companions(tmp_path / obs)) == [
        "ABCD00ITA_R_20241330000_01D_EN.rnx", "ABCD00ITA_R_20241330000_01D_GN.rnx.gz",
        "ABCD00ITA_R_20241330000_01D_MN.rnx"]


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
