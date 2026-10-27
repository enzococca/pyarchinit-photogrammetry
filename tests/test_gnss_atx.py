"""The antenna calibrations (ANTEX) of the IGS: the search RTKLIB makes in them for the antenna of the base, the copy of
igs20.atx the program keeps, and the options that give the file to RTKLIB. What the PPK does with them is in
test_gnss_ppk.py."""
from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

from pyarchinit_photogrammetry.gnss import atx, download, ppk

DATA = Path(__file__).parent / "data" / "gnss"
EXCERPT = DATA / "igs20_excerpt.atx"  # real entries of igs20.atx, trimmed, and one synthetic: see README.md
SYNTHETIC = "TRM29659.00     NONE"  # 0.100 m up on G01 and G02, no variations: not the calibration of the IGS
DAY = 86400.0


# --- ANTEX files written by the tests ----------------------------------------------------------------------------


def _line(text: str, label: str) -> str:
    """A line of an ANTEX file: the text in the first 60 columns, the label in the 20 after them."""
    return text.ljust(60) + label.ljust(20)


def _time(when: datetime) -> str:
    return f"{when.year:6d}{when.month:6d}{when.day:6d}{when.hour:6d}{when.minute:6d}{when.second:5d}.0000000"


def _antenna(kind: str, serial: str = "", offsets: dict | None = None, valid_from: datetime | None = None,
             valid_until: datetime | None = None) -> list[str]:
    """One antenna of an ANTEX file: ``kind`` is the 20 characters of its type and radome (or the name of a
    satellite), ``serial`` what follows them, ``offsets`` the (north, east, up) in millimetres of each frequency."""
    offsets = {"G01": (1.0, 2.0, 3.0)} if offsets is None else offsets
    lines = [_line("", "START OF ANTENNA"), _line(kind.ljust(20) + serial, "TYPE / SERIAL NO"),
             _line("ROBOT               test                     1    27-OCT-26", "METH / BY / # / DATE"),
             _line("     0.0", "DAZI"), _line("     0.0  90.0   5.0", "ZEN1 / ZEN2 / DZEN"),
             _line(f"{len(offsets):6d}", "# OF FREQUENCIES")]
    if valid_from:
        lines.append(_line(_time(valid_from), "VALID FROM"))
    if valid_until:
        lines.append(_line(_time(valid_until), "VALID UNTIL"))
    for frequency, (north, east, up) in offsets.items():
        lines += [_line(f"   {frequency}", "START OF FREQUENCY"),
                  _line(f"{north:10.2f}{east:10.2f}{up:10.2f}", "NORTH / EAST / UP"),
                  "   NOAZI" + "    0.00" * 19, _line(f"   {frequency}", "END OF FREQUENCY")]
    return lines + [_line("", "END OF ANTENNA")]


def _antex(*antennas: list[str], eol: str = "\n") -> bytes:
    """An ANTEX file of ``antennas`` (lists of lines)."""
    lines = [_line("     1.4            M", "ANTEX VERSION / SYST"), _line("A", "PCV TYPE / REFANT"),
             _line("", "END OF HEADER")]
    return (eol.join(lines + [line for antenna in antennas for line in antenna]) + eol).encode("ascii")


def _options(text: str) -> dict[str, str]:
    """The ``name=value`` lines of an options file of RTKLIB, without its comments."""
    options = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, value = line.split("=", 1)
            options[name.strip()] = value
    return options


# --- what the file holds ----------------------------------------------------------------------------------------


def test_receiver_antennas_of_the_excerpt():
    found = atx.receiver_antennas(EXCERPT)
    # the receivers in the order of the file, by the 20 characters of their type and radome; the satellites (BLOCK
    # IIA, GLONASS-M) are not receivers
    assert list(found) == ["ASH700699.L1    NONE", "ASH700829.2     SNOW", "LEIAR25.R3      NONE",
                           "LEIAR25.R3      LEIT", SYNTHETIC, "TRM57971.00     NONE"]
    assert all(set(entry) == {"offsets", "valid_from", "valid_until"} for entry in found.values())
    # (north, east, up) in metres, from the millimetres of the file, for each frequency it has
    assert found[SYNTHETIC]["offsets"] == {"G01": (0.0, 0.0, 0.1), "G02": (0.0, 0.0, 0.1)}
    assert found["ASH700699.L1    NONE"]["offsets"] == {"G01": pytest.approx((0.00066, -0.00053, 0.03334))}
    assert list(found["LEIAR25.R3      NONE"]["offsets"]) == ["G01", "G02", "G05", "R01", "R02"]
    assert found["LEIAR25.R3      NONE"]["offsets"]["G01"] == pytest.approx((0.00080, 0.00058, 0.16059))
    assert found["TRM57971.00     NONE"]["offsets"]["R02"] == pytest.approx((-0.00041, 0.00120, 0.05785))
    assert all(entry["valid_from"] is None and entry["valid_until"] is None for entry in found.values())


def test_up_offset_is_the_l1_offset_above_the_reference_point():
    assert atx.up_offset(EXCERPT, SYNTHETIC) == pytest.approx(0.1)
    assert atx.up_offset(EXCERPT, "LEIAR25.R3      LEIT") == pytest.approx(0.16096)
    assert atx.up_offset(EXCERPT, "ASH700829.2     SNOW") == pytest.approx(0.06954)
    # the entry as lookup() gives it, or with the blanks at its end: the same
    assert atx.up_offset(EXCERPT, SYNTHETIC + "   ") == pytest.approx(0.1)
    # not an antenna of the file, not a receiver (a satellite's offset is not above anything)
    assert atx.up_offset(EXCERPT, "NOSUCH          NONE") is None
    assert atx.up_offset(EXCERPT, "BLOCK IIA") is None
    assert atx.up_offset(EXCERPT, "") is None


# the antenna of a header, and the entry RTKLIB takes for it (searchpcv of rtkcmn.c: first the first entry of the file
# whose type holds all the words of the header, up to two; else the first entry whose type starts with the first word)
LOOKUPS = [
    # the antenna and radome of the header
    ("TRM29659.00     NONE", (SYNTHETIC, False)),
    ("LEIAR25.R3      LEIT", ("LEIAR25.R3      LEIT", False)),
    ("TRM57971.00 NONE", ("TRM57971.00     NONE", False)),  # the blanks as another writer puts them
    ("  TRM57971.00      NONE  ", ("TRM57971.00     NONE", False)),
    # a header without a radome: the first entry of that type in the file
    ("TRM29659.00", (SYNTHETIC, False)),
    ("LEIAR25.R3", ("LEIAR25.R3      NONE", False)),
    ("ASH700829.2", ("ASH700829.2     SNOW", False)),
    # a radome the file does not have: the type without the radome, the first of that type in the file
    ("TRM29659.00     SCIS", (SYNTHETIC, True)),
    ("LEIAR25.R3      SCIS", ("LEIAR25.R3      NONE", True)),
    ("ASH700829.2     NONE", ("ASH700829.2     SNOW", True)),
    ("ASH700699.L1    SNOW", ("ASH700699.L1    NONE", True)),
    # the words of the header are searched for in the type of an entry, anywhere in it and in any order
    ("57971.00 NONE", ("TRM57971.00     NONE", False)),
    ("NONE TRM57971.00", ("TRM57971.00     NONE", False)),
    ("TRM57971.00 NONE and more", ("TRM57971.00     NONE", False)),  # no more than two words are used
    ("NONE", ("ASH700699.L1    NONE", False)),  # the first entry of the file that holds it
    # a word that is not at the start of the type is no start of the type: nothing in the second search
    ("57971.00 SCIS", (None, False)),
    ("RM29659.00     SCIS", (None, False)),
    # RTKLIB's search does not tell receivers from satellites: it takes the first entry of the file
    ("BLOCK IIA", ("BLOCK IIA", False)),
    ("GLONASS-M", ("GLONASS-M", False)),
    # not in the file: the letters have to be the same, and the header has to name an antenna
    ("trm29659.00     none", (None, False)),
    ("TRM29660.00     NONE", (None, False)),
    ("NOSUCH          NONE", (None, False)),
    ("", (None, False)),
    ("    ", (None, False)),
]


@pytest.mark.parametrize("antenna, expected", LOOKUPS, ids=[repr(a) for a, _ in LOOKUPS])
def test_lookup_with_and_without_radome(antenna, expected):
    assert atx.lookup(EXCERPT, antenna) == expected


def test_lookup_takes_the_first_entry_of_the_file_not_the_best_one(tmp_path):
    # RTKLIB reads a file in order and stops at the first entry that fits: a header without a radome gets the dome
    # that comes first, and a type that is the start of a longer name gets the shorter one when it is listed first
    path = tmp_path / "order.atx"
    path.write_bytes(_antex(_antenna("LEIAR25.R3      LEIT"), _antenna("LEIAR25.R3      NONE"),
                            _antenna("NOV501+CR       NONE"), _antenna("NOV501          NONE")))
    assert atx.lookup(path, "LEIAR25.R3") == ("LEIAR25.R3      LEIT", False)
    assert atx.lookup(path, "LEIAR25.R3      NONE") == ("LEIAR25.R3      NONE", False)
    # NOV501 is a part of NOV501+CR: the antenna of the file that is listed first is taken
    assert atx.lookup(path, "NOV501          NONE") == ("NOV501+CR       NONE", False)
    assert atx.lookup(path, "NOV501+CR       NONE") == ("NOV501+CR       NONE", False)


def test_unknown_antenna_not_calibrated():
    # an antenna that is not in the file has no entry, and so no offset: RTKLIB applies nothing for it
    entry, without_radome = atx.lookup(EXCERPT, "SEPCHOKE_B3E6   NONE")
    assert (entry, without_radome) == (None, False)
    assert atx.up_offset(EXCERPT, "SEPCHOKE_B3E6   NONE") is None
    # the file itself is not at fault: it has the antennas it has
    assert atx.lookup(EXCERPT, "TRM57971.00     NONE")[0] == "TRM57971.00     NONE"


def test_the_file_is_read_as_rtklib_reads_it(tmp_path):
    when = datetime(2008, 10, 16, 23, 59, 59)
    antennas = [
        # a receiver with a serial number (an individual calibration): a receiver all the same, found by its type
        _antenna("ASH701945E_M    NONE", "SN1234", {"G01": (1.5, -2.5, 30.0), "G02": (0.5, 0.25, 40.0)}),
        # the same type again, listed later: the first one is the one RTKLIB finds
        _antenna("ASH701945E_M    NONE", "SN9999", {"G01": (9.0, 9.0, 9.0)}),
        # validity (receivers have none in igs20.atx, but ANTEX allows it); a satellite is none of the receivers
        _antenna("JAVRINGANT_DM   NONE", "", {"G01": (0.0, 0.0, 1.0)}, when, datetime(2009, 1, 2, 3, 4, 5)),
        _antenna("BLOCK IIA", "G01", {"G01": (279.0, 0.0, 2253.47)}),
        _antenna("GALILEO-1", "E11", {"E01": (1.0, 2.0, 3.0)}),
    ]
    for eol in ("\n", "\r\n"):  # a file written on Windows as well
        path = tmp_path / f"eol{len(eol)}.atx"
        path.write_bytes(_antex(*antennas, eol=eol))
        found = atx.receiver_antennas(path)
        assert list(found) == ["ASH701945E_M    NONE", "JAVRINGANT_DM   NONE"]
        assert found["ASH701945E_M    NONE"]["offsets"] == {"G01": pytest.approx((0.0015, -0.0025, 0.03)),
                                                            "G02": pytest.approx((0.0005, 0.00025, 0.04))}
        javad = found["JAVRINGANT_DM   NONE"]
        assert javad["valid_from"] == datetime(2008, 10, 16, 23, 59, 59)
        assert javad["valid_until"] == datetime(2009, 1, 2, 3, 4, 5)
        assert atx.up_offset(path, "ASH701945E_M    NONE") == pytest.approx(0.03)
        assert atx.lookup(path, "BLOCK IIA") == ("BLOCK IIA", False)  # but a search finds them


def test_a_reading_that_changes_with_the_file(tmp_path):
    path = tmp_path / "antennas.atx"
    path.write_bytes(_antex(_antenna("TRM57971.00     NONE", offsets={"G01": (0.0, 0.0, 64.0)})))
    assert atx.up_offset(path, "TRM57971.00     NONE") == pytest.approx(0.064)
    path.write_bytes(_antex(_antenna("TRM57971.00     NONE", offsets={"G01": (0.0, 0.0, 70.0)}),
                            _antenna("TRM41249.00     NONE")))  # another size: another reading
    assert atx.up_offset(path, "TRM57971.00     NONE") == pytest.approx(0.07)
    assert atx.lookup(path, "TRM41249.00") == ("TRM41249.00     NONE", False)


@pytest.mark.parametrize("content, match", [
    (b"", "ANTEX"),
    (b"just some text\n" * 5, "ANTEX"),
    (b"<html>\n<body>Please log in</body>\n</html>\n", "ANTEX"),
    (_antex(_antenna("TRM57971.00     NONE"))[:-40], "cut short"),  # cut in the last antenna
    (_antex(), "no antenna"),
], ids=["empty", "text", "html page", "cut", "no antenna"])
def test_validate_refuses_what_is_not_a_whole_antex_file(tmp_path, content, match):
    path = tmp_path / "not.atx"
    path.write_bytes(content)
    with pytest.raises(ValueError, match=match) as error:
        atx.validate(path)
    assert "not.atx" in str(error.value)


def test_validate_accepts_the_excerpt_and_a_file_of_windows(tmp_path):
    atx.validate(EXCERPT)
    path = tmp_path / "windows.atx"
    path.write_bytes(_antex(_antenna("TRM57971.00     NONE"), eol="\r\n"))
    atx.validate(path)


# --- the options that give the file to RTKLIB ---------------------------------------------------------------------


def test_config_with_an_antenna_file():
    plain = _options(ppk.config((35.16, 139.61, 70.0)))
    assert "pos1-posopt2" not in plain and "ant1-anttype" not in plain
    assert not any(name.startswith("file-") for name in plain)
    with_file = _options(ppk.config((35.16, 139.61, 70.0), antenna_file="/work/ppk/rtklib_inputs/antennas.atx"))
    # the file of the receiver antennas, and its phase centre variations on (the offsets are used anyway); the rest
    # of the options as they were, and the antenna of the rover not named: none is calibrated for a drone
    assert with_file["file-rcvantfile"] == "/work/ppk/rtklib_inputs/antennas.atx"
    assert with_file["pos1-posopt2"] == "on"
    assert "ant1-anttype" not in with_file and "file-satantfile" not in with_file
    assert {k: v for k, v in with_file.items() if k not in ("file-rcvantfile", "pos1-posopt2")} == plain
    # the rover's antenna, "*": the one of the header of its file (a base measured against a station, in Task 7)
    starred = _options(ppk.config(None, antenna_file="/a.atx", rover_antenna="*"))
    assert starred["ant1-anttype"] == "*" and starred["file-rcvantfile"] == "/a.atx"
    assert _options(ppk.config(None, antenna_file="/a.atx", rover_antenna="TRM57971.00     NONE"))[
        "ant1-anttype"] == "TRM57971.00     NONE"
    # a path with blanks and letters: as it is (RTKLIB takes the text up to the end of the line)
    spaced = "/Users/Enzo Cocca/Città/ppk/rtklib_inputs/antennas.atx"
    assert _options(ppk.config(None, antenna_file=spaced))["file-rcvantfile"] == spaced


@pytest.mark.parametrize("arguments, match", [
    (dict(antenna_file="/Scavo #3/ppk/antennas.atx"), "#"),  # RTKLIB reads the rest of the line as a comment
    (dict(antenna_file="/a/antennas.txt"), "called .atx"),  # RTKLIB reads any other file as an NGS antenna file
    (dict(antenna_file="/a/antennas.Atx"), "called .atx"),
    (dict(antenna_file="/a\nb/antennas.atx"), "control character"),
    (dict(antenna_file="/a\tb/antennas.atx"), "control character"),
    (dict(antenna_file="/" + "a" * 1000 + ".atx"), "1000 bytes"),
    (dict(antenna_file="/a.atx", rover_antenna="TRM#"), "#"),
    (dict(antenna_file="/a.atx", rover_antenna="TRM\n"), "control character"),
    (dict(antenna_file="/a.atx", rover_antenna="x" * 64), "63 characters"),
])
def test_config_refuses_what_rtklib_would_read_differently(arguments, match):
    with pytest.raises(ValueError, match=match):
        ppk.config(None, **arguments)


# --- the copy of igs20.atx the program keeps -----------------------------------------------------------------------


@pytest.fixture
def published(tmp_path, monkeypatch):
    """A folder of the GNSS data of its own, and igs20.atx published as a file on the computer (the excerpt); returns
    the published file."""
    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))
    source = tmp_path / "published" / "igs20.atx"
    source.parent.mkdir()
    shutil.copyfile(EXCERPT, source)
    monkeypatch.setattr(atx, "ATX_URL", source.as_uri())
    return source


def _age(path: Path, days: float) -> None:
    then = time.time() - days * DAY
    os.utime(path, (then, then))


def test_the_address_of_the_file_is_the_one_of_the_igs():
    assert atx.ATX_URL == "https://files.igs.org/pub/station/general/igs20.atx"


def test_atx_file_cached_and_refreshed(published, tmp_path):
    gnss = tmp_path / "gnss"
    assert atx.cached_atx() is None and not gnss.exists()  # asking never downloads, nor makes a folder
    first = atx.atx_file()
    assert first == gnss / "atx" / "igs20.atx" and first.read_bytes() == EXCERPT.read_bytes()
    assert atx.cached_atx() == first
    assert sorted(p.name for p in first.parent.iterdir()) == ["igs20.atx"]  # nothing left beside it
    # published again, differently: a copy of 29 days is kept
    newer = EXCERPT.read_bytes().replace(b"igs20.atx, the antenna", b"igs21.atx, the antenna")
    assert newer != EXCERPT.read_bytes()
    published.write_bytes(newer)
    _age(first, 29)
    assert atx.atx_file() == first and first.read_bytes() == EXCERPT.read_bytes()
    # of 31 days: downloaded again, and the copy is new
    _age(first, 31)
    assert atx.atx_file() == first and first.read_bytes() == newer
    assert time.time() - first.stat().st_mtime < 60
    # asked to refresh: downloaded again whatever its age
    published.write_bytes(EXCERPT.read_bytes())
    assert atx.atx_file() == first and first.read_bytes() == newer  # a copy that was just made is kept
    assert atx.atx_file(refresh=True) == first and first.read_bytes() == EXCERPT.read_bytes()
    assert sorted(p.name for p in first.parent.iterdir()) == ["igs20.atx"]


def test_atx_file_reports_what_it_downloads(published):
    seen = []
    atx.atx_file(progress=lambda fraction, message: seen.append((fraction, message)))
    assert seen and seen[-1][0] == 1.0 and all(0.0 <= fraction <= 1.0 for fraction, _ in seen)


def test_atx_file_offline_uses_the_copy_it_has(published, tmp_path, caplog):
    first = atx.atx_file()
    published.unlink()  # the address no longer answers
    _age(first, 40)
    with caplog.at_level(logging.WARNING, logger=atx.log.name):
        assert atx.atx_file() == first
        assert atx.atx_file(refresh=True) == first
    assert first.read_bytes() == EXCERPT.read_bytes()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2 and all("igs20.atx" in w and "copy" in w for w in warnings), warnings
    assert sorted(p.name for p in first.parent.iterdir()) == ["igs20.atx"]


def test_atx_file_offline_without_a_copy_says_where_it_looked(published):
    published.unlink()
    with pytest.raises(download.DownloadError) as error:
        atx.atx_file()
    assert error.value.url == atx.ATX_URL and atx.cached_atx() is None
    assert not list((published.parent.parent / "gnss").rglob("*.atx*"))  # no file left half-way


def test_atx_file_keeps_what_it_has_when_the_download_is_not_an_antex_file(published, caplog):
    first = atx.atx_file()
    _age(first, 40)
    published.write_text("<html><body>You are connected to the hotel wifi</body></html>")
    with caplog.at_level(logging.WARNING, logger=atx.log.name):
        assert atx.atx_file() == first
    assert first.read_bytes() == EXCERPT.read_bytes()  # the page was not kept
    assert any("ANTEX" in r.getMessage() for r in caplog.records)
    # and with no copy to fall back on, an error that says what came
    first.unlink()
    with pytest.raises(download.DownloadError, match="ANTEX"):
        atx.atx_file()
    assert atx.cached_atx() is None and not list(first.parent.glob("*"))


def test_atx_file_does_not_keep_a_copy_that_is_not_an_antex_file(published):
    # a page saved in its place by something else, a file cut short: no copy at all, however recent
    kept = published.parent.parent / "gnss" / "atx" / "igs20.atx"
    kept.parent.mkdir(parents=True)
    for damaged in (b"<html><body>Please log in</body></html>", EXCERPT.read_bytes()[:-100]):
        kept.write_bytes(damaged)
        assert atx.cached_atx() == kept  # asking for the copy that is there is not judging it
        assert atx.atx_file() == kept and kept.read_bytes() == EXCERPT.read_bytes()  # downloaded again
    # and with no address that answers, an error, not the damaged copy
    kept.write_bytes(b"<html></html>")
    published.unlink()
    with pytest.raises(download.DownloadError):
        atx.atx_file()
    assert kept.read_bytes() == b"<html></html>"  # not touched
