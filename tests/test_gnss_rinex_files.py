"""RINEX files as the archives give them: compressed with Hatanaka, the Unix compress, gzip or zip, and in pieces (an
hour at a time). The files are RTKLIB's sample (RINEX 2.10, 120 epochs at 30 s, with event records), an hour of an EPN
station (RINEX 3.04) and the files of tests/data/crinex and tests/data/unix_compress, which describe where they
come from."""
from __future__ import annotations

import dataclasses
import gzip
import os
import re
import shutil
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.gnss import download, lzw, ppk, rinex
import test_gnss_ppk
from test_gnss_ppk import PPK_BASE, PPK_NAV, PPK_ROVER, _options, _sha256, needs_rtklib

DATA = Path(__file__).parent / "data"
WHOLE = DATA / "rtklib" / "07590920.05o"  # station 0759: RINEX 2.10, GPS, 120 epochs at 30 s, event records
CRINEX = DATA / "crinex"
HOUR = "KRAW00POL_R_20262991000_01H_30S_MO.crx"  # an hour of the EPN station KRAW00POL in Compact RINEX 3.0
COMPRESS = DATA / "unix_compress"

fake_rtklib = test_gnss_ppk.fake_rtklib  # the fixture of the tests of the PPK: RTKLIB as a stand-in program

needs_a_decoder = pytest.mark.skipif(_native.kernel("crx2rnx") is None and _native._crx2rnx_program() is None,
                                     reason="neither the native core 0.6 (API 9) nor a CRX2RNX program")


# --- helpers ---------------------------------------------------------------------------------------------------


def _files(folder: Path) -> list[str]:
    """The names of the files under ``folder`` (not of its folders); none when there is no folder."""
    if not folder.exists():
        return []
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())


def _gz(source: Path, target: Path) -> Path:
    target.write_bytes(gzip.compress(source.read_bytes()))
    return target


def _zip(target: Path, members: list[tuple[str, bytes]]) -> Path:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            archive.writestr(name, data)
    return target


def _lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def _header_and_body(lines: list[str]) -> tuple[list[str], list[str]]:
    end = next(i for i, line in enumerate(lines) if line.rstrip().endswith("END OF HEADER")) + 1
    return lines[:end], lines[end:]


_EPOCH_V2 = re.compile(r"^ \d\d +\d+ +\d+ +\d+ +\d+ +[\d.]+ +0 ")


def _records(path: Path) -> tuple[list[str], list[list[str]]]:
    """The header of a RINEX file and its records, each an epoch line with the lines up to the next epoch (the event
    records after an epoch are its)."""
    header, body = _header_and_body(_lines(path))
    records: list[list[str]] = []
    for line in body:
        if line.startswith(">") or _EPOCH_V2.match(line):
            records.append([line])
        else:
            records[-1].append(line)
    return header, records


def _label(line: str) -> str:
    return line[60:].strip()


def _last_obs_line(epoch: str, system: str = "GPS") -> str:
    """The TIME OF LAST OBS line of the observations whose last epoch line is ``epoch``."""
    fields = epoch.lstrip("> ").split()[:6]
    year = int(fields[0]) if epoch.startswith(">") else int(fields[0]) + (1900 if int(fields[0]) >= 80 else 2000)
    y, mo, d, h, mi = year, *(int(f) for f in fields[1:5])
    return f"{y:6d}{mo:6d}{d:6d}{h:6d}{mi:6d}{float(fields[5]):13.7f}     {system}".ljust(60) + "TIME OF LAST OBS"


def _with_last_obs(header: list[str], epoch: str) -> list[str]:
    """``header`` with a TIME OF LAST OBS line of the epoch ``epoch`` (after the TIME OF FIRST OBS line)."""
    lines = [line for line in header if _label(line) != "TIME OF LAST OBS"]
    i = next(k for k, line in enumerate(lines) if _label(line) == "TIME OF FIRST OBS")
    return lines[:i + 1] + [_last_obs_line(epoch)] + lines[i + 1:]


def _piece(path: Path, header: list[str], records: list[list[str]], last_obs: bool = False) -> Path:
    """``records`` with ``header`` as a RINEX file, with the TIME OF LAST OBS of its own last epoch when asked."""
    lines = _with_last_obs(header, records[-1][0]) if last_obs else list(header)
    for record in records:
        lines += record
    path.write_text("\n".join(lines) + "\n", newline="\n")
    return path


def _card(content: str, label: str) -> str:
    """A header line: the content in the first 60 columns, the label after them."""
    return content.ljust(60) + label


def _rewrite_header(source: Path, target: Path, change) -> Path:
    """A copy of ``source`` whose header lines go through ``change(line)`` (a new line, or None to leave it out)."""
    lines, in_header = [], True
    for line in _lines(source):
        if in_header:
            in_header = _label(line) != "END OF HEADER"
            line = change(line) if in_header else line
            if line is None:
                continue
        lines.append(line)
    target.write_text("\n".join(lines) + "\n", newline="\n")
    return target


def _on_label(label: str, content: str | None):
    """A ``change`` for :func:`_rewrite_header`: the line with ``label`` gets ``content`` (None: it goes)."""
    def change(line: str):
        if _label(line) != label:
            return line
        return None if content is None else _card(content, label)

    return change


def _epoch_time(record: list[str]) -> datetime:
    """The time of the epoch line of a record of a RINEX 2 file with a 2-digit year."""
    yy, mo, d, h, mi, s = record[0].split()[:6]
    return datetime(2000 + int(yy), int(mo), int(d), int(h), int(mi)) + timedelta(seconds=float(s))


@pytest.fixture(autouse=True)
def gnss_folder(tmp_path, monkeypatch):
    """The folder of the downloads and caches of the test, a folder of its own: the copies the tests make (some are
    large) do not pile up in the one the session shares."""
    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))
    return tmp_path / "gnss"


# --- the folders ------------------------------------------------------------------------------------------------


def test_the_folders_of_the_gnss_data(tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.ai.sam_env import data_dir

    # what the session sets up (tests/conftest.py): a folder that is not the user's, and PROJ never goes to the network
    # (a shell with PROJ_NETWORK=ON would make it download a geoid grid in silence)
    monkeypatch.undo()
    assert os.environ["PPG_GNSS_DIR"] and Path(os.environ["PPG_GNSS_DIR"]) != data_dir() / "gnss"
    assert os.environ["PROJ_NETWORK"] == "OFF"
    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))
    assert download.gnss_dir() == tmp_path / "gnss"
    cache = download.cache_dir("rinex")
    assert cache == tmp_path / "gnss" / "cache" / "rinex" and cache.is_dir()
    assert download.cache_dir("rinex") == cache  # again: it is there
    monkeypatch.delenv("PPG_GNSS_DIR")
    assert download.gnss_dir() == data_dir() / "gnss"
    monkeypatch.setenv("PPG_GNSS_DIR", "  ")  # blank: not set
    assert download.gnss_dir() == data_dir() / "gnss"
    for kind in ("", ".", "..", "a/b", "../x"):
        with pytest.raises(ValueError, match="folder"):
            download.cache_dir(kind)


# --- the Unix compress ------------------------------------------------------------------------------------------


def test_compress_z_as_the_original(tmp_path):
    out = tmp_path / "07590920.05d"
    lzw.decompress(CRINEX / "07590920.05d.Z", out)
    assert out.read_bytes() == (CRINEX / "07590920.05d").read_bytes()
    assert _files(tmp_path) == [out.name]


@pytest.mark.parametrize("name, size", [("b9-first40k.Z", 40960), ("b11-whole.Z", None)])
def test_compress_z_with_other_widths_and_a_cleared_dictionary(tmp_path, name, size):
    """Codes of 9 bits in a stream limited to 9 (compress writes 10 bits once the dictionary has 512 entries) and of 9
    to 11 bits, a clearing of the dictionary in each, made by the compress of the system: the groups of eight codes
    that compress pads when the width grows or the dictionary is cleared."""
    out = tmp_path / "original"
    lzw.decompress(COMPRESS / name, out)
    assert out.read_bytes() == WHOLE.read_bytes()[:size]


def _stream(groups: list[tuple[int, list[int]]], max_bits: int = 16, block: bool = True) -> bytes:
    """A compress stream: its header, then the groups of codes ``(width, codes)`` as compress writes them: ``width``
    bytes, the codes lowest bit first; all but the last group are whole."""
    data = bytearray([0x1F, 0x9D, max_bits | (0x80 if block else 0)])
    for i, (width, codes) in enumerate(groups):
        word = 0
        for k, code in enumerate(codes):
            word |= code << (k * width)
        size = width if i < len(groups) - 1 else -(-len(codes) * width // 8)
        data += word.to_bytes(width, "little")[:size]
    return bytes(data)


def _unpacked(tmp_path: Path, data: bytes, name: str = "made.Z") -> bytes:
    source = tmp_path / name
    source.write_bytes(data)
    target = tmp_path / "made"
    lzw.decompress(source, target)
    return target.read_bytes()


def test_compress_streams_made_by_hand(tmp_path):
    a, b, c, d = (ord(x) for x in "abcd")
    # block mode: 256 clears the dictionary, the first string is 257 (here "aa", then "aaa" is 258)
    assert _unpacked(tmp_path, _stream([(9, [a, 257, a])])) == b"aaaa"
    assert _unpacked(tmp_path, _stream([(9, [a, 257, 258, a])])) == b"aaaaaaa"  # a code just made: "aaa" is 258
    # without block mode 256 is the first string
    assert _unpacked(tmp_path, _stream([(9, [a, 256, a])], block=False)) == b"aaaa"
    assert _unpacked(tmp_path, _stream([(9, [a, b, 256, 258])], block=False)) == b"abababa"
    # a clear: the rest of its group is padding (here codes that would be wrong), the next group starts at the next
    # byte and is 9 bits wide again, with a dictionary of its own
    assert _unpacked(tmp_path, _stream([(9, [a, b, 256, 300, 300, 300, 300, 300]), (9, [c, d])])) == b"abcd"
    assert _unpacked(tmp_path, _stream([(9, [a, b, 256, 0, 0, 0, 0, 0]), (9, [c, d, 257])])) == b"abcdcd"
    # only the header: nothing; the bits left at the end of the last group, not enough for a code, are nothing
    assert _unpacked(tmp_path, _stream([])) == b""
    assert _unpacked(tmp_path, _stream([(9, [a, b])])) == b"ab"  # 18 bits in 3 bytes


@pytest.mark.parametrize("data, match", [
    (b"", "not a compress"),
    (b"\x1f\x9d", "not a compress"),
    (b"\x1f\x8b\x08\x00", "not a compress"),
    (bytes([0x1F, 0x9D, 0x88]), "8 bits"),  # compress writes 9 to 16
    (bytes([0x1F, 0x9D, 0x91]), "17 bits"),
    (_stream([(9, [97, 300])]), "damaged"),  # a code the dictionary does not have yet
    (_stream([(9, [300])]), "damaged"),  # the first code is a character
    (_stream([(9, [97, 256]), (9, [300])]), "damaged"),  # the first code after a clear is a character too
])
def test_damaged_compress_streams_are_refused_with_the_name_of_the_file(tmp_path, data, match):
    source = tmp_path / "made.Z"
    source.write_bytes(data)
    target = tmp_path / "made"
    with pytest.raises(ValueError, match=match) as error:
        lzw.decompress(source, target)
    assert "made.Z" in str(error.value)
    assert _files(tmp_path) == ["made.Z"]  # no target, no piece of one


# --- what a file is made of, and the file itself ------------------------------------------------------------------


def test_compression_is_told_by_the_first_bytes_not_by_the_name(tmp_path):
    crx = CRINEX / HOUR
    assert rinex.compression(WHOLE) == ()
    assert rinex.compression(PPK_NAV) == ()
    assert rinex.compression(crx) == ("hatanaka",)
    assert rinex.compression(_gz(WHOLE, tmp_path / "a.05o.gz")) == ("gzip",)
    assert rinex.compression(_gz(crx, tmp_path / "a.crx.gz")) == ("gzip", "hatanaka")
    assert rinex.compression(CRINEX / "07590920.05d.Z") == ("compress", "hatanaka")
    assert rinex.compression(COMPRESS / "b11-whole.Z") == ("compress",)
    assert rinex.compression(_zip(tmp_path / "a.zip", [("a.05o", WHOLE.read_bytes())])) == ("zip",)
    assert rinex.compression(_zip(tmp_path / "b.zip", [("b.crx", crx.read_bytes())])) == ("zip", "hatanaka")
    assert rinex.compression(_zip(tmp_path / "c.zip", [("c.05o.gz", gzip.compress(WHOLE.read_bytes()))])) == (
        "zip", "gzip")
    # the name is a hint at most: a RINEX file called .crx or .gz is RINEX, a Hatanaka file called .rnx is Hatanaka
    for name, source, layers in (("renamed.crx", WHOLE, ()), ("renamed.05d.gz", WHOLE, ()),
                                 ("renamed.rnx", crx, ("hatanaka",)),
                                 ("renamed.txt", CRINEX / "07590920.05d.Z", ("compress", "hatanaka"))):
        copy = tmp_path / name
        shutil.copyfile(source, copy)
        assert rinex.compression(copy) == layers, name
    # a file with the magic of gzip and nothing else is a gzip file that is damaged, to be told so when it is read
    broken = tmp_path / "broken.05o"
    broken.write_bytes(b"\x1f\x8b\x08\x00 nothing more")
    assert rinex.compression(broken) == ("gzip",)
    with pytest.raises(ValueError, match="broken.05o"):
        rinex.header(broken)
    with pytest.raises(FileNotFoundError):
        rinex.compression(tmp_path / "nowhere.05o")


@needs_a_decoder
def test_header_of_hatanaka_files(tmp_path, gnss_folder):
    """The header of a Hatanaka file, alone, in a .gz or in a .Z, is the header of the RINEX it holds, and says how the
    file is packed."""
    for crx, expected in ((CRINEX / HOUR, CRINEX / (HOUR + ".expected")), (CRINEX / "07590920.05d", WHOLE)):
        copy = tmp_path / ("plain-" + expected.name)
        shutil.copyfile(expected, copy)
        wanted = rinex.header(copy)
        assert wanted.compressed is None and wanted.first is not None and wanted.systems
        packed = _gz(crx, tmp_path / (crx.name + ".gz"))
        for path, layers in ((crx, "hatanaka"), (packed, "gzip+hatanaka")):
            info = rinex.header(path)
            assert info.compressed == layers, path.name
            assert dataclasses.replace(info, compressed=None) == wanted, path.name
            assert rinex.time_span(path) == rinex.time_span(copy), path.name
    z = CRINEX / "07590920.05d.Z"
    info = rinex.header(z)
    assert info.compressed == "compress+hatanaka"
    assert dataclasses.replace(info, compressed=None) == rinex.header(WHOLE)
    assert rinex.time_span(z) == rinex.time_span(WHOLE)
    archive = _zip(tmp_path / "hour.zip", [(HOUR, (CRINEX / HOUR).read_bytes())])
    info = rinex.header(archive)
    assert info.compressed == "zip+hatanaka" and info.marker == "KRAW00POL"
    # the decompressed copies, six files, and nothing else beside them
    assert len(_files(gnss_folder)) == 6 and all(f.endswith(".rnx") for f in _files(gnss_folder))


def test_header_of_files_packed_without_hatanaka(tmp_path, gnss_folder):
    """A .Z, a zip or a .gz of a RINEX file: its header, and how it is packed."""
    wanted = rinex.header(WHOLE)
    z = COMPRESS / "b11-whole.Z"  # the whole sample: the RINEX itself
    packed = {"compress": z, "zip": _zip(tmp_path / "a.zip", [("a.05o", WHOLE.read_bytes())]),
              "gzip": _gz(WHOLE, tmp_path / "a.05o.gz")}
    for layers, path in packed.items():
        info = rinex.header(path)
        assert info.compressed == layers, path.name
        assert dataclasses.replace(info, compressed=None) == wanted, path.name
        assert rinex.time_span(path) == rinex.time_span(WHOLE), path.name
    assert rinex.header(WHOLE).compressed is None
    assert [Path(f).suffix for f in _files(gnss_folder)] == [".rnx", ".rnx"]  # a gzip file is read as it is


def test_plain_is_the_same_file_for_text_and_for_gzip(tmp_path):
    cache = tmp_path / "cache"
    packed = _gz(WHOLE, tmp_path / "a.05o.gz")
    assert rinex.plain(WHOLE, cache) == WHOLE
    assert rinex.plain(packed, cache) == packed
    assert not cache.exists()  # nothing was made


def test_plain_reused_until_the_file_changes(tmp_path):
    packed = tmp_path / "base.05d.Z"
    shutil.copyfile(COMPRESS / "b11-whole.Z", packed)
    cache = tmp_path / "cache"
    first = rinex.plain(packed, cache)
    assert first.parent == cache and first.suffix == ".rnx" and first.read_bytes() == WHOLE.read_bytes()
    stamp = first.stat().st_mtime_ns
    assert rinex.plain(packed, cache) == first and first.stat().st_mtime_ns == stamp  # used again, not made again
    text = first.read_bytes()
    # the same size and another time: another copy, with the name of its own, and the one of the earlier state is gone
    later = packed.stat().st_mtime_ns + 5_000_000_000
    os.utime(packed, ns=(later, later))
    second = rinex.plain(packed, cache)
    assert second != first and second.read_bytes() == text
    assert _files(cache) == [second.name]
    # another size: another copy of what the file holds now
    shutil.copyfile(COMPRESS / "b9-first40k.Z", packed)
    third = rinex.plain(packed, cache)
    assert third not in (first, second) and third.read_bytes() == WHOLE.read_bytes()[:40960]
    other = tmp_path / "other.05d.Z"  # another file keeps its copy
    shutil.copyfile(COMPRESS / "b11-whole.Z", other)
    fourth = rinex.plain(other, cache)
    assert _files(cache) == sorted([third.name, fourth.name])  # no piece of a copy beside them
    # the folder is the cache of the program unless it is given
    assert rinex.plain(packed).parent == download.cache_dir("rinex")


def test_zip_with_one_rinex(tmp_path):
    cache = tmp_path / "cache"
    archive = _zip(tmp_path / "base.zip", [("30400920.05o", PPK_ROVER.read_bytes())])
    out = rinex.plain(archive, cache)
    assert out.read_bytes() == PPK_ROVER.read_bytes()
    assert rinex.header(archive).marker == "3040" and rinex.header(archive).compressed == "zip"
    # the folders and the resource forks that macOS puts in a zip are not files of the archive
    archive = _zip(tmp_path / "mac.zip", [("data/", b""), ("data/30400920.05o", PPK_ROVER.read_bytes()),
                                          ("__MACOSX/data/._30400920.05o", b"\x00\x05\x16\x07")])
    assert rinex.plain(archive, cache).read_bytes() == PPK_ROVER.read_bytes()
    # a zip of a gzip file: the layers one after the other, the last one is read as it is
    archive = _zip(tmp_path / "gz.zip", [("a.05o.gz", gzip.compress(PPK_ROVER.read_bytes()))])
    assert rinex.compression(archive) == ("zip", "gzip") and rinex.plain(archive, cache).read_bytes() == (
        PPK_ROVER.read_bytes())


def test_zip_with_two_files_refused(tmp_path):
    cache = tmp_path / "cache"
    archive = _zip(tmp_path / "both.zip", [("rover.05o", PPK_ROVER.read_bytes()), ("base.05o", WHOLE.read_bytes())])
    for call in (lambda: rinex.plain(archive, cache), lambda: rinex.header(archive)):
        with pytest.raises(ValueError, match="2 files") as error:
            call()
        assert "both.zip" in str(error.value) and "rover.05o" in str(error.value) and "base.05o" in str(error.value)
    empty = _zip(tmp_path / "empty.zip", [])
    with pytest.raises(ValueError, match="no file"):
        rinex.plain(empty, cache)
    assert not cache.exists() or _files(cache) == []


@pytest.mark.parametrize("name, data", [("base.05o.zip", b"PK\x03\x04" + bytes(range(256))),
                                        ("base.obs", b"PK\x03\x04 not an archive")])
def test_a_damaged_zip_is_named(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="zip") as error:
        rinex.plain(path, cache)
    assert name in str(error.value)
    assert not cache.exists() or _files(cache) == []
    with pytest.raises(ValueError, match="zip"):
        rinex.header(path)


@pytest.mark.parametrize("name, magic", [("base.05o.Z", b"\x1f\x9d\x90"), ("base.05o", b"\x1f\x9d\x90")])
def test_a_damaged_z_file_is_named(tmp_path, name, magic):
    path = tmp_path / name
    path.write_bytes(magic + bytes(range(256)))
    for call in (rinex.header, rinex.time_span):
        with pytest.raises(ValueError) as error:  # damaged, or text that is not RINEX: either way the file is named
            call(path)
        assert name in str(error.value)


@needs_a_decoder
def test_damaged_gz_names_the_file(tmp_path, gnss_folder):
    """A download that stopped, a file damaged in the middle, a Hatanaka file cut: the error names the file (and, for
    the decoder, the line), and the cache has nothing left of it."""
    crx = (CRINEX / HOUR).read_bytes()
    packed = gzip.compress(crx)
    flipped = bytearray(packed)
    flipped[len(flipped) // 2] ^= 0xFF
    z = (CRINEX / "07590920.05d.Z").read_bytes()
    cases = {"cut.crx.gz": packed[:len(packed) // 2], "flipped.crx.gz": bytes(flipped),
             "cut.crx": crx[:len(crx) // 2 + 7], "cut.05d.Z": z[:len(z) // 2 + 3]}
    for name, data in cases.items():
        path = tmp_path / name
        path.write_bytes(data)
        for call in (rinex.header, rinex.time_span, rinex.plain):
            with pytest.raises(ValueError) as error:
                call(path)
            text = str(error.value)
            assert name in text, (name, text)
            if name.endswith(".crx") or name.endswith(".Z"):
                assert re.search(r"line \d+", text), text
            else:
                assert "damaged" in text and "download" in text, text
        assert _files(gnss_folder) == [], name


def test_a_layer_damaged_inside_a_zip_names_the_zip_and_leaves_nothing(tmp_path, gnss_folder):
    """The layers are undone one after the other: a gzip file cut short in a zip archive is an error that names the
    archive, and the private folder of the layers is gone, with the half of the copy."""
    packed = gzip.compress(WHOLE.read_bytes())
    archive = _zip(tmp_path / "base.zip", [("base.05o.gz", packed[:len(packed) // 2])])
    assert rinex.compression(archive) == ("zip", "gzip")
    for call in (rinex.header, rinex.time_span, rinex.plain):
        with pytest.raises(ValueError, match="cut short or damaged") as error:
            call(archive)
        assert "base.zip" in str(error.value)
    assert _files(gnss_folder) == [] and not [p for p in gnss_folder.rglob(".unpack-*")]


def test_a_rinex_file_with_the_name_of_a_hatanaka_file_is_rinex(tmp_path):
    for name in ("plain.crx", "plain.05d", "PLAIN.05D.gz"):
        renamed = tmp_path / name
        shutil.copyfile(WHOLE, renamed)
        info = rinex.header(renamed)
        assert info.compressed is None and info.marker == "0759" and info.interval == 30.0, name


def _no_decoder(*_args):
    raise RuntimeError(_native.CRINEX_MISSING)


@pytest.mark.parametrize("name", ["base.crx", "base.crx.gz", "base.05d.Z", "base.zip"])
def test_hatanaka_files_without_a_decoder_say_what_to_install(tmp_path, monkeypatch, name):
    """Without the native core 0.6 and without a CRX2RNX, a file that is Hatanaka is refused, naming the file and
    what to install (the other files are read all the same)."""
    monkeypatch.setattr(_native, "crx2rnx", _no_decoder)
    crx = (CRINEX / HOUR).read_bytes()
    if name.endswith(".gz"):
        data = gzip.compress(crx)
    elif name.endswith(".Z"):
        data = (CRINEX / "07590920.05d.Z").read_bytes()
    elif name.endswith(".zip"):
        data = _zip(tmp_path / "make.zip", [("base.crx", crx)]).read_bytes()
    else:
        data = crx
    path = tmp_path / name
    path.write_bytes(data)
    for call in (rinex.header, rinex.time_span):
        with pytest.raises(ValueError, match="Hatanaka") as error:
            call(path)
        assert name in str(error.value) and "CRX2RNX" in str(error.value)
    assert rinex.header(_gz(WHOLE, tmp_path / "other.05o.gz")).marker == "0759"


# --- pieces: one file from several ---------------------------------------------------------------------------------

SPLICE = "RINEX FILE SPLICE; other post-header comments skipped"


def test_join_pieces_as_the_whole(tmp_path):
    """The sample in three pieces that have two epochs in common with their neighbours (the event records after an
    epoch go with it) is the sample again: the same epochs and lines, the TIME OF LAST OBS of its end."""
    header, records = _records(WHOLE)
    assert len(records) == 120 and sum(SPLICE in line for record in records for line in record) == 3
    pieces = [_piece(tmp_path / f"{k}.05o", header, records[a:b], last_obs=True)
              for k, (a, b) in enumerate([(0, 50), (48, 100), (98, 120)])]
    out = tmp_path / "joined.05o"
    info, gaps = rinex.join(pieces, out)
    assert gaps == []
    joined_header, joined_body = _header_and_body(_lines(out))
    whole_header, whole_body = _header_and_body(_lines(WHOLE))
    assert joined_body == whole_body
    assert joined_header == _with_last_obs(whole_header, records[-1][0])
    assert (info.first, info.last) == rinex.time_span(WHOLE) and info.interval == 30.0 and info.marker == "0759"
    assert info == rinex.header(out)
    # whatever the order of the pieces
    again = tmp_path / "again.05o"
    rinex.join(pieces[::-1], again)
    assert again.read_bytes() == out.read_bytes()
    # the same piece twice: the second is all repeats, and so are the event records after its epochs
    rinex.join([pieces[1], pieces[1]], again)
    assert _header_and_body(_lines(again))[1] == _header_and_body(_lines(pieces[1]))[1]
    assert _files(tmp_path) == sorted(["joined.05o", "again.05o", "0.05o", "1.05o", "2.05o"])  # no piece of a file


def test_join_one_file_keeps_it_with_its_events(tmp_path):
    """The records of RINEX 2 and 4 files with events of every kind (a header event that drops an observation type, an
    external event, cycle slip records before the epoch, a moved antenna) are written as they are."""
    for name in ("events.05d.expected", "kraw_rinex402.crx.expected", "kraw299k.26d.expected"):
        source = CRINEX / name
        out = tmp_path / ("joined-" + name)
        info, gaps = rinex.join([source], out)
        header, body = _header_and_body(_lines(source))
        joined_header, joined_body = _header_and_body(_lines(out))
        assert joined_body == body, name
        assert [line for line in joined_header if _label(line) != "TIME OF LAST OBS"] == [
            line for line in header if _label(line) != "TIME OF LAST OBS"], name
        assert gaps == [] and info.last == rinex.time_span(source)[1], name
        last = [line for line in joined_header if _label(line) == "TIME OF LAST OBS"]
        assert len(last) == 1, name


@needs_a_decoder
def test_join_a_piece_of_rinex_3_and_a_hatanaka_file(tmp_path):
    """The first 70 epochs of an hour of KRAW as text, and the whole hour in Hatanaka: the hour again (the statistics of
    a header that count the observations of one file, ``# OF SATELLITES`` and ``PRN / # OF OBS``, are of no piece)."""
    expected = CRINEX / (HOUR + ".expected")
    header, records = _records(expected)
    assert len(records) == 120
    first = _piece(tmp_path / "first.rnx", header, records[:70])
    out = tmp_path / "hour.rnx"
    info, gaps = rinex.join([CRINEX / HOUR, first], out)  # the first one given is the longer
    assert gaps == []
    joined_header, joined_body = _header_and_body(_lines(out))
    whole_header, whole_body = _header_and_body(_lines(expected))
    assert joined_body == whole_body
    statistics = ("# OF SATELLITES", "PRN / # OF OBS")
    assert joined_header == [line for line in whole_header if _label(line) not in statistics]
    assert info.marker == "KRAW00POL" and info.systems == ("G", "R") and info.compressed is None
    assert (info.first, info.last) == rinex.time_span(expected)
    # (the decoded hour is in the cache, which is in the folder of the test)
    assert [f for f in _files(tmp_path) if not f.startswith("gnss/")] == ["first.rnx", "hour.rnx"]


def test_join_reports_gaps(tmp_path):
    header, records = _records(WHOLE)
    first, middle, last = (_piece(tmp_path / f"{k}.05o", header, records[a:b])
                           for k, (a, b) in enumerate([(0, 40), (40, 80), (80, 120)]))
    out = tmp_path / "joined.05o"
    assert rinex.join([first, middle, last], out)[1] == []
    # a piece left out: a gap, from the last epoch before it to the first after it
    info, gaps = rinex.join([last, first], out)
    assert gaps == [(_epoch_time(records[39]), _epoch_time(records[80]))]
    assert (info.first, info.last) == (_epoch_time(records[0]), _epoch_time(records[119]))
    # one epoch missing is not a gap (a step of twice the interval), two are
    one = _piece(tmp_path / "one.05o", header, records[:50] + records[51:])
    assert rinex.join([one], out)[1] == []
    two = _piece(tmp_path / "two.05o", header, records[:50] + records[52:])
    assert rinex.join([two], out)[1] == [(_epoch_time(records[49]), _epoch_time(records[52]))]


def test_join_refuses_other_observation_types(tmp_path):
    header, records = _records(WHOLE)
    first = _piece(tmp_path / "first.05o", header, records[:60])
    out = tmp_path / "joined.05o"
    for name, types in (("fewer.05o", "     3    L1    C1    L2"), ("swapped.05o", "     4    C1    L1    L2    P2"),
                        ("other.05o", "     4    L1    C1    L2    C2")):
        other = _rewrite_header(first, tmp_path / name, _on_label("# / TYPES OF OBSERV", types))
        with pytest.raises(ValueError, match="observation types") as error:
            rinex.join([first, other], out)
        assert name in str(error.value) and "first.05o" in str(error.value)
    # RINEX 3: the types of one system, the others as they were
    hour = CRINEX / (HOUR + ".expected")
    other = _rewrite_header(hour, tmp_path / "glonass.rnx", lambda line: line.replace("S1C S2P", "S1C S2C")
                            if _label(line) == "SYS / # / OBS TYPES" else line)
    with pytest.raises(ValueError, match="observation types") as error:
        rinex.join([hour, other], out)
    assert "glonass.rnx" in str(error.value) and "system R" in str(error.value)
    # a header event inside a piece that gives other types than the header: the pieces after it would be read with them
    rest = _piece(tmp_path / "rest.05o", _records(CRINEX / "events.05d.expected")[0], _records(WHOLE)[1][60:])
    with pytest.raises(ValueError, match="observation types") as error:
        rinex.join([CRINEX / "events.05d.expected", rest], out)
    assert "events.05d.expected" in str(error.value)
    assert not out.exists() and not list(tmp_path.glob("*.part"))


def test_join_accepts_header_events_that_repeat_the_types(tmp_path):
    """The RINEX 4.02 sample gives its observation types again, unchanged, in an event: nothing changes for the
    pieces."""
    source = CRINEX / "kraw_rinex402.crx.expected"
    header, records = _records(source)
    pieces = [_piece(tmp_path / f"{k}.rnx", header, records[a:b]) for k, (a, b) in enumerate([(0, 60), (58, 122)])]
    out = tmp_path / "joined.rnx"
    assert rinex.join(pieces, out)[1] == []
    assert _header_and_body(_lines(out))[1] == _header_and_body(_lines(source))[1]


def test_join_refuses_pieces_that_are_not_of_one_receiver(tmp_path):
    header, records = _records(WHOLE)
    first = _piece(tmp_path / "first.05o", header, records[:60])
    second = _piece(tmp_path / "second.05o", header, records[60:])
    out = tmp_path / "joined.05o"
    cases = {
        "antenna": ("ANT # / TYPE", "                    TRM41249.00     TZGD", "antenna"),
        "height": ("ANTENNA: DELTA H/E/N", "        1.5000        0.0000        0.0000", "antenna"),
        "time system": ("TIME OF FIRST OBS", "  2005     4     2     0    30    0.0000000     GLO", "time system"),
    }
    for kind, (label, content, match) in cases.items():
        other = _rewrite_header(second, tmp_path / f"{kind}.05o".replace(" ", "-"), _on_label(label, content))
        with pytest.raises(ValueError, match=match) as error:
            rinex.join([first, other], out)
        assert other.name in str(error.value) and "first.05o" in str(error.value), kind
    # another major version of RINEX, a navigation file, nothing, the output among the pieces
    with pytest.raises(ValueError, match="version") as error:
        rinex.join([WHOLE, CRINEX / (HOUR + ".expected")], out)
    assert "RINEX 2" in str(error.value) and "RINEX 3" in str(error.value)
    with pytest.raises(ValueError, match="navigation") as error:
        rinex.join([first, PPK_NAV], out)
    assert PPK_NAV.name in str(error.value)
    with pytest.raises(ValueError, match="no file"):
        rinex.join([], out)
    before = second.read_bytes()
    with pytest.raises(ValueError, match="overwrite"):
        rinex.join([first, second], second)
    assert second.read_bytes() == before
    assert not out.exists() and not list(tmp_path.glob("*.part"))


def test_join_refuses_another_marker(tmp_path):
    header, records = _records(WHOLE)
    first = _piece(tmp_path / "first.05o", header, records[:60])
    other = _rewrite_header(_piece(tmp_path / "second.05o", header, records[60:]), tmp_path / "3040.05o",
                            _on_label("MARKER NAME", "3040"))
    out = tmp_path / "joined.05o"
    with pytest.raises(ValueError, match="marker") as error:
        rinex.join([first, other], out)
    text = str(error.value)
    assert "3040.05o" in text and "first.05o" in text and "0759" in text and "3040" in text
    assert not out.exists()


def test_join_names_a_gzip_piece_that_is_cut_short(tmp_path):
    header, records = _records(WHOLE)
    first = _piece(tmp_path / "first.05o", header, records[:60])
    second = _piece(tmp_path / "second.05o", header, records[58:])
    packed = gzip.compress(second.read_bytes())
    cut = tmp_path / "second.05o.gz"
    cut.write_bytes(packed[:len(packed) * 3 // 4])  # reading the header and the first epochs works, the end does not
    out = tmp_path / "joined.05o"
    with pytest.raises(ValueError, match="cut short or damaged") as error:
        rinex.join([first, cut], out)
    assert "second.05o.gz" in str(error.value)
    assert not out.exists() and not list(tmp_path.glob("*.part"))
    # whole, it is read as it is
    cut.write_bytes(packed)
    info, _gaps = rinex.join([first, cut], out)
    assert info.last == rinex.time_span(WHOLE)[1]


# --- the PPK with such files ------------------------------------------------------------------------------------


def test_solve_reads_compressed_inputs_as_they_are(tmp_path, fake_rtklib):
    """RTKLIB is given plain copies of a rover in a zip, a base under the Unix compress and a navigation file in a
    gzip (it would run a shell command for any of them): the bytes of the files they hold."""
    rover = _zip(tmp_path / "rover.zip", [("30400920.05o", PPK_ROVER.read_bytes())])
    base = tmp_path / "base.05d.Z"  # compress only: the Hatanaka layer needs the core, which the fake switches off
    shutil.copyfile(COMPRESS / "b11-whole.Z", base)
    nav = _gz(PPK_NAV, tmp_path / "nav.05n.gz")
    work = tmp_path / "work"
    solution = ppk.solve(rover, base, [nav], folder=work)
    (run,) = fake_rtklib()
    assert run["inputs"] == [_sha256(PPK_ROVER.read_bytes()), _sha256(PPK_BASE.read_bytes()),
                             _sha256(PPK_NAV.read_bytes())]
    assert run["folder"] == ["base.obs", "nav1.nav", "rover.obs"]
    assert solution.inputs == {"rover": str(rover.absolute()), "base": str(base.absolute()),
                               "navs": [str(nav.absolute())]}
    assert solution.base["file"] == "base.05d.Z" and solution.base["name"] == "0759"
    assert sorted(p.name for p in work.iterdir()) == sorted(
        [solution.path.name, solution.path.with_suffix(".conf").name])


def test_solve_with_a_base_in_pieces_gives_rtklib_one_file(tmp_path, fake_rtklib):
    header, records = _records(PPK_BASE)
    first = _piece(tmp_path / "first.05o", header, records[:70])
    second = _piece(tmp_path / "second.05o", header, records[68:])
    work = tmp_path / "work"
    solution = ppk.solve(PPK_ROVER, [second, first], [PPK_NAV], folder=work)  # not in the order of time
    (run,) = fake_rtklib()
    joined = tmp_path / "joined.05o"
    rinex.join([first, second], joined)
    assert run["inputs"][1] == _sha256(joined.read_bytes())
    assert run["folder"] == ["base.obs", "nav1.nav", "rover.obs"]
    assert solution.inputs["base"] == [str(second.absolute()), str(first.absolute())]
    assert solution.inputs["rover"] == str(PPK_ROVER.absolute())  # a single file stays a path
    assert solution.base["name"] == "0759" and solution.base["file"] == "second.05o, first.05o"
    assert solution.base["position_source"] == "header"
    assert sorted(p.name for p in work.iterdir()) == sorted(
        [solution.path.name, solution.path.with_suffix(".conf").name])
    # a list of one file is the file
    one = ppk.solve(PPK_ROVER, [PPK_BASE], [PPK_NAV], folder=work)
    assert one.inputs["base"] == str(PPK_BASE.absolute()) and one.base["file"] == PPK_BASE.name
    # pieces that are not of one station are refused before RTKLIB runs
    other = _rewrite_header(second, tmp_path / "other.05o", _on_label("MARKER NAME", "3040"))
    with pytest.raises(ValueError, match="marker"):
        ppk.solve(PPK_ROVER, [first, other], [PPK_NAV], folder=work)
    assert len(fake_rtklib()) == 2


def test_solve_warns_of_a_hole_in_the_base_while_the_rover_flies(tmp_path, fake_rtklib):
    base_header, base_records = _records(PPK_BASE)
    before = _piece(tmp_path / "before.05o", base_header, base_records[:40])  # to 00:19:30
    after = _piece(tmp_path / "after.05o", base_header, base_records[80:])  # from 00:40:00
    rover_header, rover_records = _records(PPK_ROVER)
    work = tmp_path / "work"
    solution = ppk.solve(PPK_ROVER, [before, after], [PPK_NAV], folder=work)
    (warning,) = [w for w in solution.warnings if "no observations" in w]
    assert warning.startswith("the base has no observations from 2005-04-02 00:19:30 to 2005-04-02 00:40:00 GPS time")
    # a rover that has landed before the hole: nothing to say
    early = _piece(tmp_path / "early.05o", rover_header, rover_records[:30])  # to 00:14:30
    solution = ppk.solve(early, [before, after], [PPK_NAV], folder=work)
    assert not [w for w in solution.warnings if "no observations" in w]
    # a rover that flies from the hole on
    late = _piece(tmp_path / "late.05o", rover_header, rover_records[50:])
    solution = ppk.solve(late, [before, after], [PPK_NAV], folder=work)
    assert len([w for w in solution.warnings if "no observations" in w]) == 1


def test_base_interval_longer_than_rover_interpolates(tmp_path, fake_rtklib):
    """RTKLIB interpolates the base (misc-timeinterp) when its epochs are further apart than the rover's."""
    work = tmp_path / "work"

    def options(rover_interval, base_interval) -> dict[str, str]:
        def given(interval):
            return _on_label("INTERVAL", None if interval is None else f"{interval:10.3f}")

        rover = _rewrite_header(PPK_ROVER, tmp_path / f"rover-{rover_interval}.05o", given(rover_interval))
        base = _rewrite_header(PPK_BASE, tmp_path / f"base-{base_interval}.05o", given(base_interval))
        assert (rinex.header(rover).interval, rinex.header(base).interval) == (rover_interval, base_interval)
        ppk.solve(rover, base, [PPK_NAV], folder=work)
        return _options(fake_rtklib()[-1]["options"])

    assert options(1.0, 30.0)["misc-timeinterp"] == "on"
    assert options(29.9, 30.0)["misc-timeinterp"] == "on"
    for rover_interval, base_interval in ((30.0, 30.0), (60.0, 30.0), (None, 30.0), (30.0, None), (None, None)):
        assert "misc-timeinterp" not in options(rover_interval, base_interval), (rover_interval, base_interval)
    # the interval of a base in pieces is the smallest of theirs
    header, records = _records(PPK_BASE)
    a = _piece(tmp_path / "a.05o", header, records[:60])
    b = _rewrite_header(_piece(tmp_path / "b.05o", header, records[58:]), tmp_path / "b60.05o",
                        _on_label("INTERVAL", f"{60.0:10.3f}"))
    rover = _rewrite_header(PPK_ROVER, tmp_path / "rover-45.05o", _on_label("INTERVAL", f"{45.0:10.3f}"))
    ppk.solve(rover, [a, b], [PPK_NAV], folder=work)
    assert "misc-timeinterp" not in _options(fake_rtklib()[-1]["options"])  # 30 s, the smaller of 30 and 60, under 45
    rover = _rewrite_header(PPK_ROVER, tmp_path / "rover-20.05o", _on_label("INTERVAL", f"{20.0:10.3f}"))
    ppk.solve(rover, [a, b], [PPK_NAV], folder=work)
    assert _options(fake_rtklib()[-1]["options"])["misc-timeinterp"] == "on"  # 30 s over 20


def test_config_interpolates_the_base_when_asked():
    assert _options(ppk.config(None, interpolate_base=True))["misc-timeinterp"] == "on"
    assert "misc-timeinterp" not in _options(ppk.config(None))
    assert "misc-timeinterp" not in _options(ppk.config(None, interpolate_base=False))


@needs_rtklib
@needs_a_decoder
def test_solve_with_a_base_in_pieces(tmp_path, monkeypatch):
    """The base in two pieces, the first half as text and the whole in Hatanaka (they share their first half), gives
    the rows of the solution that the whole file gives, as does the whole in Hatanaka under compress, as is."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    header, records = _records(PPK_BASE)
    half = _piece(tmp_path / "first-half.05o", header, records[:60])
    whole_crx = CRINEX / "07590920.05d"
    wanted = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=tmp_path / "whole")

    def same_rows(solution):
        for name in ("times", "llh", "q", "ns", "sd"):
            np.testing.assert_array_equal(getattr(solution, name), getattr(wanted, name), err_msg=name)

    pieces = ppk.solve(PPK_ROVER, [half, whole_crx], [PPK_NAV], folder=tmp_path / "pieces")
    same_rows(pieces)
    assert pieces.inputs["base"] == [str(half.absolute()), str(whole_crx.absolute())]
    assert pieces.base["name"] == "0759" and pieces.base["file"] == f"{half.name}, {whole_crx.name}"
    z = ppk.solve(PPK_ROVER, CRINEX / "07590920.05d.Z", [PPK_NAV], folder=tmp_path / "z")
    same_rows(z)
    assert z.base["file"] == "07590920.05d.Z"
    assert len(wanted.times) == 120


def test_companions_of_the_files_of_an_archive(tmp_path):
    """The navigation files that go with an observation file are found beside it when the files are packed as the
    archives give them: .Z and .zip as well as .gz, and Hatanaka (.24d, .crx) like the observations they hold."""
    for name in ("abcd0010.24d.Z", "abcd0010.24n.Z", "abcd0010.23n.Z", "brdc0010.24n.gz", "brdc0020.24n.Z",
                 "brdc0050.24n.Z", "notes.txt"):
        (tmp_path / name).write_bytes(b"")
    assert [p.name for p in rinex.find_companions(tmp_path / "abcd0010.24d.Z")] == [
        "abcd0010.24n.Z", "brdc0010.24n.gz", "brdc0020.24n.Z"]
    (tmp_path / "abcd0010.24o.zip").write_bytes(b"")
    assert [p.name for p in rinex.find_companions(tmp_path / "abcd0010.24o.zip")] == [
        "abcd0010.24n.Z", "brdc0010.24n.gz", "brdc0020.24n.Z"]
    folder = tmp_path / "long"
    folder.mkdir()
    for name in ("ABCD00ITA_R_20241330000_01H_30S_MO.crx.gz", "ABCD00ITA_R_20241330000_01H_MN.rnx.gz",
                 "ABCD00ITA_R_20241330000_01H_GN.rnx.Z", "ABCD00ITA_R_20241330000_01H_30S_MO.rnx.zip"):
        (folder / name).write_bytes(b"")
    assert [p.name for p in rinex.find_companions(folder / "ABCD00ITA_R_20241330000_01H_30S_MO.crx.gz")] == [
        "ABCD00ITA_R_20241330000_01H_GN.rnx.Z", "ABCD00ITA_R_20241330000_01H_MN.rnx.gz"]
