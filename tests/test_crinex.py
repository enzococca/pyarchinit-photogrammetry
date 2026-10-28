"""Hatanaka-compressed RINEX (Compact RINEX 1.0, 3.0 and 3.1) decoded by the native core as the official
CRX2RNX of RNXCMP 4.2.0 decodes it, byte for byte: tests/data/crinex holds the files and what CRX2RNX made
of them (``*.expected``), described in its README.md. Without the core the program runs a CRX2RNX it is
given, and without either it says what to install."""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from pyarchinit_photogrammetry import _native

DATA = Path(__file__).parent / "data" / "crinex"

CORE = _native.kernel("crx2rnx") is not None
needs_a_decoder = pytest.mark.skipif(not CORE and _native._crx2rnx_program() is None,
                                     reason="neither the native core 0.6 (API 9) nor a CRX2RNX program")
needs_the_core = pytest.mark.skipif(not CORE, reason="native core without the CRINEX decoder (older than 0.6, API 9)")


def _assert_as_crx2rnx(source: Path, expected: Path, tmp_path: Path) -> None:
    """``source`` decoded into a new file gives the bytes of ``expected``; otherwise the first line that differs."""
    out = tmp_path / "decoded.rnx"
    _native.crx2rnx(source, out)
    decoded, wanted = out.read_bytes(), expected.read_bytes()
    if decoded != wanted:
        a, b = decoded.split(b"\n"), wanted.split(b"\n")
        k = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        pytest.fail(f"{source.name}, line {k + 1} of the output: decoded {a[k:k + 1]!r}, CRX2RNX {b[k:k + 1]!r} "
                    f"({len(a)} and {len(b)} lines)")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["decoded.rnx"]  # nothing beside it


@needs_a_decoder
def test_crinex_1_as_crx2rnx(tmp_path):
    """Compact RINEX 1.0 of a RINEX 2.10 file (RTKLIB's station 0759: GPS, four types, at most nine satellites)."""
    _assert_as_crx2rnx(DATA / "07590920.05d", DATA / "07590920.05d.expected", tmp_path)


@needs_a_decoder
def test_crinex_3_as_crx2rnx(tmp_path):
    """Compact RINEX 3.0 as an archive gives it: an hour of an EPN station at 30 s (RINEX 3.04, GPS with 12
    types and GLONASS with 8, satellites coming and going, blank fields, signal strengths)."""
    name = "KRAW00POL_R_20262991000_01H_30S_MO.crx"
    _assert_as_crx2rnx(DATA / name, DATA / (name + ".expected"), tmp_path)


@needs_a_decoder
def test_crinex_3_1_as_crx2rnx(tmp_path):
    """Compact RINEX 3.1 of a RINEX 4.02 file: receiver clock offsets and the picoseconds of the epochs on the
    clock line, events, observations and offsets of every shape, indicators without their observation."""
    _assert_as_crx2rnx(DATA / "kraw_rinex402.crx", DATA / "kraw_rinex402.crx.expected", tmp_path)


@needs_a_decoder
def test_events_copied_as_crx2rnx(tmp_path):
    """Event records of RINEX 2 copied as they are: header information (flag 4) with comment lines, an external
    event (flag 5) with no record, cycle slip records (flag 6), and a flag 4 that drops an observation type,
    after which the records have three fields."""
    _assert_as_crx2rnx(DATA / "events.05d", DATA / "events.05d.expected", tmp_path)


@needs_a_decoder
def test_rinex_2_continuation_lines_as_crx2rnx(tmp_path):
    """RINEX 2.11 with more than 12 satellites (the list goes on a second line) and more than five types (two
    lines a satellite), receiver clock offsets in columns 69-80, values of every shape."""
    _assert_as_crx2rnx(DATA / "kraw299k.26d", DATA / "kraw299k.26d.expected", tmp_path)


@needs_a_decoder
def test_windows_line_ends_read_the_same(tmp_path):
    """A file with carriage returns before its line feeds decodes to the same RINEX (line feeds only)."""
    crlf = tmp_path / "07590920.05d"
    crlf.write_bytes((DATA / "07590920.05d").read_bytes().replace(b"\n", b"\r\n"))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _assert_as_crx2rnx(crlf, DATA / "07590920.05d.expected", out_dir)


@needs_the_core
def test_cut_short_names_the_line(tmp_path):
    """A file cut short (a download that stopped) is refused with its name and the line where it ends, inside
    a line or between two lines of an epoch; no output is left, finished or not."""
    name = "KRAW00POL_R_20262991000_01H_30S_MO.crx"
    data = (DATA / name).read_bytes()
    cut = int(len(data) * 0.6)
    assert data[cut - 1:cut] != b"\n" and data[:cut].count(b"\n") == 1374  # inside line 1375, an epoch's data
    end_of_line = data.rindex(b"\n", 0, cut) + 1  # line 1374 complete, the epoch goes on
    for size, line in ((cut, 1375), (end_of_line, 1375)):
        src, dst = tmp_path / name, tmp_path / "decoded.rnx"
        src.write_bytes(data[:size])
        with pytest.raises(ValueError) as error:
            _native.crx2rnx(src, dst)
        assert re.match(rf"{re.escape(name)}, line {line}: ", str(error.value)), str(error.value)
        assert sorted(p.name for p in tmp_path.iterdir()) == [name]  # neither decoded.rnx nor decoded.rnx.part


@needs_the_core
def test_damaged_file_names_the_line(tmp_path):
    """A file that is not what the format allows is refused with its name and the line: a letter in a number,
    a difference with no value before it to add it to (the start of the series is lost), a first epoch that
    is not given in full."""
    src, dst = tmp_path / "07590920.05d", tmp_path / "decoded.rnx"
    lines = (DATA / "07590920.05d").read_bytes().split(b"\n")
    # line 20: the first epoch, given in full ("&" in its first column); line 22: its first satellite
    assert lines[19].startswith(b"&05  4  2  0  0") and lines[21].startswith(b"3&55923622160 ")
    for index, damaged, what in ((21, b"3&5592x622160" + lines[21][13:], '"3&5592x622160" is not a number'),
                                 (21, b"55923622160" + lines[21][13:], "a difference with no value before it"),
                                 (19, b" " + lines[19][1:], "where a whole one is due")):
        src.write_bytes(b"\n".join(lines[:index] + [damaged] + lines[index + 1:]))
        with pytest.raises(ValueError, match=rf"^07590920\.05d, line {index + 1}: ") as error:
            _native.crx2rnx(src, dst)
        assert what in str(error.value), str(error.value)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["07590920.05d"]


@needs_the_core
def test_not_crinex_refused(tmp_path):
    """What is not Compact RINEX (plain RINEX, gzip, nothing, a version that does not exist) is refused with
    its name, and nothing is written; a file that is not there is an OSError."""
    plain = Path(__file__).parent / "data" / "rtklib" / "07590920.05o"
    gz = tmp_path / "base.crx.gz"
    gz.write_bytes(b"\x1f\x8b\x08\x00" + bytes(40))
    empty = tmp_path / "empty.crx"
    empty.write_bytes(b"")
    version = tmp_path / "base.crx"
    version.write_bytes(b"2.0" + (DATA / "07590920.05d").read_bytes()[3:])  # CRINEX VERS 2.0 does not exist
    dst = tmp_path / "out" / "decoded.rnx"
    dst.parent.mkdir()
    for src in (plain, gz, empty, version):
        with pytest.raises(ValueError, match=rf"^{re.escape(src.name)}, line 1: ") as error:
            _native.crx2rnx(src, dst)
        assert "Compact RINEX" in str(error.value), str(error.value)
        assert list(dst.parent.iterdir()) == []
    with pytest.raises(FileNotFoundError):
        _native.crx2rnx(tmp_path / "nowhere.crx", dst)
    assert list(dst.parent.iterdir()) == []


@needs_the_core
def test_epochs_without_satellites_given_in_full(tmp_path):
    """An epoch with no satellite given in full: in RINEX 3 its line has lost the blank columns 36-41 with the
    other trailing spaces of a Compact RINEX line, and is still an epoch, as the first one, after an event, and as
    the line the next one differs from; in RINEX 2 the line ends at column 32."""
    header3 = ["3.0                 COMPACT RINEX FORMAT                    CRINEX VERS   / TYPE",
               "RNX2CRX ver.4.2.0                       27-Oct-26 05:23     CRINEX PROG / DATE",
               "     3.04           OBSERVATION DATA    M (MIXED)           RINEX VERSION / TYPE",
               "G    1 C1C                                                  SYS / # / OBS TYPES",
               "                                                            END OF HEADER"]
    epochs3 = ["> 2026 10 26 10 00 00.0000000  0  0", "",  # the first epoch, no satellite: 35 columns
               " " * 19 + "3" + " " * 14 + "1" + " " * 6 + "G05", "", "3&21213460160",  # 10:00:30, differenced
               "> 2026 10 26 10 00 45.0000000  4  1", "AFTER THE EVENT".ljust(60) + "COMMENT",
               "> 2026 10 26 10 01 00.0000000  0  0", "3&-123456789"]  # after the event, a clock offset alone
    header2 = ["1.0                 COMPACT RINEX FORMAT                    CRINEX VERS   / TYPE",
               "RNX2CRX ver.4.2.0                       27-Oct-26 05:23     CRINEX PROG / DATE",
               "     2.11           OBSERVATION DATA    G (GPS)             RINEX VERSION / TYPE",
               "     1    C1                                                # / TYPES OF OBSERV",
               "                                                            END OF HEADER"]
    epochs2 = ["&26 10 26 10  0  0.0000000  0  0", "", " " * 16 + "3" + " " * 14 + "1G05", "", "3&21213460160"]
    src, dst = tmp_path / "empty.crx", tmp_path / "empty.rnx"
    src.write_text("\n".join(header3 + epochs3) + "\n")
    _native.crx2rnx(src, dst)
    assert dst.read_text().splitlines()[3:] == [
        "> 2026 10 26 10 00 00.0000000  0  0", "> 2026 10 26 10 00 30.0000000  0  1", "G05  21213460.160",
        "> 2026 10 26 10 00 45.0000000  4  1", "AFTER THE EVENT".ljust(60) + "COMMENT",
        "> 2026 10 26 10 01 00.0000000  0  0" + " " * 6 + " -.000123456789"]
    src.write_text("\n".join(header2 + epochs2) + "\n")
    _native.crx2rnx(src, dst)
    assert dst.read_text().splitlines()[3:] == [" 26 10 26 10  0  0.0000000  0  0",
                                                " 26 10 26 10  0 30.0000000  0  1G05", "  21213460.160"]


@needs_the_core
def test_negative_clock_offsets_are_written_as_they_are(tmp_path):
    """CRX2RNX 4.2.0 writes a negative receiver clock offset that is a whole number of tenths of a millisecond
    (RINEX 3) or of tenths of a second (RINEX 2) one tenth nearer zero (-0.001 s as -.000900000000, -0.5 s as
    -.400000000), which is why the test files have none; the core writes the value the file holds."""
    header3 = ["3.0                 COMPACT RINEX FORMAT                    CRINEX VERS   / TYPE",
               "RNX2CRX ver.4.2.0                       27-Oct-26 05:23     CRINEX PROG / DATE",
               "     3.04           OBSERVATION DATA    M (MIXED)           RINEX VERSION / TYPE",
               "G    1 C1C                                                  SYS / # / OBS TYPES",
               "                                                            END OF HEADER"]
    epochs3 = ["> 2026 10 26 10 00 00.0000000  0  1      G05", "3&-1000000000", "3&21213460160",
               " " * 19 + "3", "500000000", "-1466060"]
    header2 = ["1.0                 COMPACT RINEX FORMAT                    CRINEX VERS   / TYPE",
               "RNX2CRX ver.4.2.0                       27-Oct-26 05:23     CRINEX PROG / DATE",
               "     2.11           OBSERVATION DATA    G (GPS)             RINEX VERSION / TYPE",
               "     1    C1                                                # / TYPES OF OBSERV",
               "                                                            END OF HEADER"]
    epochs2 = ["&26 10 26 10  0  0.0000000  0  1G05", "3&-500000000", "3&21213460160",
               " " * 16 + "3", "400000000", "-1466060"]
    src, dst = tmp_path / "clock.crx", tmp_path / "clock.rnx"
    src.write_text("\n".join(header3 + epochs3) + "\n")
    _native.crx2rnx(src, dst)
    # the clock offset in columns 42-56 (F15.12), the satellites only in the compressed epoch line
    assert dst.read_text().splitlines()[3:] == [
        "> 2026 10 26 10 00 00.0000000  0  1" + " " * 6 + " -.001000000000", "G05  21213460.160",
        "> 2026 10 26 10 00 30.0000000  0  1" + " " * 6 + " -.000500000000", "G05  21211994.100"]
    src.write_text("\n".join(header2 + epochs2) + "\n")
    _native.crx2rnx(src, dst)
    # the clock offset in columns 69-80 (F12.9), after the list of satellites padded to column 68
    assert dst.read_text().splitlines()[3:] == [
        " 26 10 26 10  0  0.0000000  0  1G05".ljust(68) + " -.500000000", "  21213460.160",
        " 26 10 26 10  0 30.0000000  0  1G05".ljust(68) + " -.100000000", "  21211994.100"]


# --- without the native core ---------------------------------------------------------------------------------------


def _fake_crx2rnx(folder: Path, name: str = "CRX2RNX") -> Path:
    """A stand-in for CRX2RNX: copies its standard input to its standard output, or writes PPG_FAKE_MESSAGE to
    standard error and exits with 1 when the input starts with "broken"."""
    script = folder / name
    script.write_text(f"""#!{sys.executable}
import os, sys
data = sys.stdin.buffer.read()
if data.startswith(b"broken"):
    sys.stderr.write(os.environ.get("PPG_FAKE_MESSAGE", "ERROR : something went wrong") + "\\n")
    sys.exit(1)
sys.stdout.buffer.write(data)
""")
    script.chmod(0o755)
    if os.name == "nt":  # no shebang lines on Windows: a batch file starts the script
        wrapper = folder / (name + ".cmd")
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        return wrapper
    return script


def test_without_the_core_runs_crx2rnx(tmp_path, monkeypatch):
    """Without the decoder in the native core (missing, older than 0.6 or switched off) the program named by
    PPG_CRX2RNX, or CRX2RNX in the PATH, decodes: the file on its standard input, the RINEX from its standard
    output, written beside the destination and renamed when the program is done."""
    monkeypatch.setattr(_native, "_core", lambda: None)
    tools = tmp_path / "tools"
    tools.mkdir()
    src, dst = tmp_path / "base.crx", tmp_path / "out" / "base.rnx"
    dst.parent.mkdir()
    src.write_bytes(b"what the program decodes\n")
    monkeypatch.setenv("PPG_CRX2RNX", str(_fake_crx2rnx(tools)))
    _native.crx2rnx(src, dst)
    assert dst.read_bytes() == b"what the program decodes\n" and list(dst.parent.iterdir()) == [dst]
    # what it says when it cannot: the file's name and its message, nothing left
    dst.unlink()
    src.write_bytes(b"broken\n")
    monkeypatch.setenv("PPG_FAKE_MESSAGE", "ERROR at line 7. : invalid format")
    with pytest.raises(ValueError, match=r"^base\.crx: .*ERROR at line 7\. : invalid format"):
        _native.crx2rnx(src, dst)
    assert list(dst.parent.iterdir()) == []
    # CRX2RNX (or crx2rnx) in the PATH, when PPG_CRX2RNX names nothing
    monkeypatch.delenv("PPG_CRX2RNX")
    src.write_bytes(b"found in the PATH\n")
    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    _fake_crx2rnx(path_dir, "crx2rnx")
    monkeypatch.setenv("PATH", str(path_dir))
    _native.crx2rnx(str(src), str(dst))
    assert dst.read_bytes() == b"found in the PATH\n"
    # a PPG_CRX2RNX that is not there
    monkeypatch.setenv("PPG_CRX2RNX", str(tmp_path / "nowhere" / "CRX2RNX"))
    with pytest.raises(RuntimeError, match="PPG_CRX2RNX"):
        _native.crx2rnx(src, dst)


def test_without_anything_says_what_to_install(tmp_path, monkeypatch):
    """Without the core and without a CRX2RNX the message says what to install: the native core 0.6 (a pip install
    that upgrades an older one; the applications include it), or CRX2RNX."""
    monkeypatch.setattr(_native, "_core", lambda: None)
    monkeypatch.delenv("PPG_CRX2RNX", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    src, dst = tmp_path / "base.crx", tmp_path / "base.rnx"
    src.write_bytes(b"1.0                 COMPACT RINEX FORMAT\n")
    with pytest.raises(RuntimeError) as error:
        _native.crx2rnx(src, dst)
    text = str(error.value)
    assert 'pip install -U "pyarchinit-photogrammetry-core>=0.6"' in text and "CRX2RNX" in text
    assert "the applications include it" in text
    assert not dst.exists()


def test_doctor_says_where_hatanaka_is_decoded(tmp_path, monkeypatch):
    """ppg doctor: the Hatanaka decoder inside the native core (its version and API), switched off, missing from an
    older core, a CRX2RNX named by PPG_CRX2RNX or in the PATH, or nothing, with what to install; after the PPK in the
    report."""
    from pyarchinit_photogrammetry import doctor
    from pyarchinit_photogrammetry.engine import isolate

    class Core:  # the native core 0.6
        __version__, API = "0.6.0", 9

        @staticmethod
        def crx2rnx(src, dst):
            pass

    class OldCore:  # 0.5, RTKLIB without the decoder
        __version__, API = "0.5.0", 8

    monkeypatch.setenv("PPG_NATIVE", "1")
    monkeypatch.delenv("PPG_CRX2RNX", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(_native, "_core", lambda: Core)
    check = doctor.crinex_check()
    assert (check.name, check.level, check.detail) == ("Hatanaka (Compact RINEX)", doctor.OK,
                                                       "decoded inside the native core 0.6.0 (API 9)")
    monkeypatch.setenv("PPG_NATIVE", "0")
    check = doctor.crinex_check()
    assert check.level == doctor.WARN and "switched off (PPG_NATIVE=0)" in check.detail
    monkeypatch.setenv("PPG_NATIVE", "1")
    monkeypatch.setattr(_native, "_core", lambda: OldCore)
    check = doctor.crinex_check()
    assert check.level == doctor.WARN and check.detail.startswith("the native core 0.5.0 has no Hatanaka decoder")
    assert 'pip install -U "pyarchinit-photogrammetry-core>=0.6"' in check.hint and "PPG_CRX2RNX" in check.hint
    monkeypatch.setattr(_native, "_core", lambda: None)
    assert doctor.crinex_check().detail.startswith("no native core, and no CRX2RNX")
    tool = _fake_crx2rnx(tmp_path)
    check = doctor.crinex_check()  # in the PATH (on Windows a batch file, whatever the case of its extension)
    assert check.level == doctor.OK and os.path.normcase(check.detail) == os.path.normcase(
        f"external CRX2RNX: {shutil.which(tool.name) or tool}")
    monkeypatch.setenv("PPG_CRX2RNX", str(tmp_path / "nowhere" / "CRX2RNX"))
    check = doctor.crinex_check()
    assert check.level == doctor.WARN and "not a program that can run" in check.detail
    monkeypatch.setattr(isolate, "run_isolated", lambda fn, timeout=None: None)  # no SpatiaLite probe here
    names = [c.name for c in doctor.run_checks(gui=False).checks]
    assert names.index("Hatanaka (Compact RINEX)") == names.index("PPK (RTKLIB)") + 1


def test_the_check_says_whether_the_core_decodes_hatanaka(monkeypatch):
    """``--check`` says whether the Hatanaka decoder is inside the native core, and fails only as the packaged
    application without it (every archive of the permanent stations is Compact RINEX); a CRX2RNX named by
    PPG_CRX2RNX or the core switched off changes nothing: the check is about the build."""
    pytest.importorskip("PySide6")
    from pyarchinit_photogrammetry.gui import check_crinex

    class Core:
        __version__ = "0.6.0"

        @staticmethod
        def crx2rnx(src, dst):
            pass

    class OldCore:
        __version__ = "0.5.0"

    monkeypatch.setenv("PPG_NATIVE", "0")
    monkeypatch.setenv("PPG_CRX2RNX", "CRX2RNX")
    monkeypatch.setattr(_native, "_core", lambda: Core)
    assert check_crinex(frozen=True) == ("Hatanaka decoder in the core 0.6.0", True)
    monkeypatch.setattr(_native, "_core", lambda: OldCore)
    assert check_crinex(frozen=True) == ("missing (the native core 0.5.0 has no Hatanaka decoder)", False)
    assert check_crinex(frozen=False) == ("missing (the native core 0.5.0 has no Hatanaka decoder)", True)
    monkeypatch.setattr(_native, "_core", lambda: None)
    assert check_crinex(frozen=True) == ("missing (no native core)", False)
    assert check_crinex()[1] is True  # this interpreter is not a packaged application
