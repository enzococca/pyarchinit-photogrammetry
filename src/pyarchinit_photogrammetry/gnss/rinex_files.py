"""RINEX files as they are stored: what a file is packed in (gzip, the Unix compress, zip, Hatanaka, in layers) told
by its first bytes, the plain text of it in a cache of decoded copies made once and pruned, gzip read as it is. Nothing
here reads RINEX itself (see :mod:`.rinex`, which gives these names again for the code that imported them from it); a
Hatanaka file is decoded by the native core or a CRX2RNX (``_native.crx2rnx``)."""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import tempfile
import time
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .. import _native
from . import download, lzw


def without_packing(name: str) -> str:
    """``name`` without the extension of a gzip file, a file of the Unix compress or a zip archive."""
    for extension in (".gz", ".z", ".zip"):
        if name.lower().endswith(extension):
            return name[:-len(extension)]
    return name


def _is_gzip(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == _GZIP


def _open(path: Path, encoding: str = "ascii", errors: str = "replace"):
    """The file as text, a gzip file decompressed on the fly (by its first bytes, not by its name). Bytes that are not
    ASCII are replaced, not refused."""
    if _is_gzip(path):
        return gzip.open(path, "rt", encoding=encoding, errors=errors)
    return open(path, "rt", encoding=encoding, errors=errors)


# --- files packed in layers ---------------------------------------------------------------------------------------

_HEAD_BYTES = 1 << 16  # the part of a file looked at to tell what it is packed in
_MAX_LAYERS = 4
_GZIP = b"\x1f\x8b"
_ZIP = (b"PK\x03\x04", b"PK\x05\x06")  # an archive with files, an empty one


def _is_crinex(head: bytes) -> bool:
    """The first line of ``head`` is that of a Hatanaka file: ``CRINEX VERS   / TYPE`` where a header has its labels."""
    return head.split(b"\n", 1)[0][60:].strip().startswith(b"CRINEX")


def _kind(head: bytes, outermost: bool) -> str | None:
    """The layer a file starts with, told by its first bytes ``head``: "gzip", "compress" (the Unix compress),
    "zip" (only for the outermost layer: the directory of an archive is at its end) or "hatanaka"; None for text."""
    if head.startswith(_GZIP):
        return "gzip"
    if head.startswith(lzw.MAGIC):
        return "compress"
    if outermost and head.startswith(_ZIP):
        return "zip"
    return "hatanaka" if _is_crinex(head) else None


def _member(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    """The one file of the zip archive called ``name`` (the folders in it and the resource forks that macOS adds are
    not files); a ``ValueError`` naming the archive and what it holds when that is not one file."""
    members = [m for m in archive.infolist()
               if not m.is_dir() and not (m.filename.startswith("__MACOSX/")
                                          or PurePosixPath(m.filename).name.startswith("._"))]
    if not members:
        raise ValueError(f"{name} holds no file")
    if len(members) > 1:
        raise ValueError(f"{name} holds {len(members)} files ({', '.join(m.filename for m in members)}): unzip it and "
                         "give the one you want")
    return members[0]


def _zip_head(path: Path) -> bytes:
    """The first bytes of the one file in the zip archive ``path``; nothing when it holds none or several, or cannot
    be read."""
    try:
        with zipfile.ZipFile(path) as archive, archive.open(_member(archive, path.name)) as member:
            return member.read(_HEAD_BYTES)
    except (ValueError, zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError):
        return b""


def _inflate_head(kind: str, data: bytes) -> bytes:
    """The first bytes of what ``data``, the beginning of a gzip or compress stream, holds; what is read before a break,
    nothing if it breaks at once."""
    if kind == "compress":
        return lzw.head(data, _HEAD_BYTES)
    try:
        return zlib.decompressobj(31).decompress(data, _HEAD_BYTES)
    except zlib.error:
        return b""


def compression(path: str | Path) -> tuple[str, ...]:
    """What the file is packed in, from the outside in: "gzip", "compress" (the Unix compress, .Z), "zip" (an archive of
    one file) and "hatanaka" (Compact RINEX), told by the first bytes of the file - and by those of what is inside - and
    by the first line of a Hatanaka file. The name is only a hint, and not looked at: a RINEX file called .crx is
    RINEX. A text file has none (``()``). A zip archive is looked into only as the outermost layer. The file is not read
    further, so a damaged file shows its layers here and fails when :func:`plain` opens it. A ``FileNotFoundError`` for
    a file that is not there."""
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(_HEAD_BYTES)
    layers: list[str] = []
    kind = _kind(head, True)
    if kind == "zip":
        layers.append("zip")
        head = _zip_head(path)
        kind = _kind(head, False)
    while kind in ("gzip", "compress") and len(layers) < _MAX_LAYERS:
        layers.append(kind)
        head = _inflate_head(kind, head)
        kind = _kind(head, False)
    if kind == "hatanaka" and len(layers) < _MAX_LAYERS:
        layers.append("hatanaka")
    return tuple(layers)


@contextmanager
def _writing(target: Path):
    """``target`` written through ``target`` + ".part", which becomes ``target`` when the block ends well and is gone
    when it does not: ``target`` is never half a file."""
    part = target.with_name(target.name + ".part")
    try:
        with open(part, "wb") as f:
            yield f
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)


def gunzip(src: str | Path, dst: str | Path, name: str | None = None) -> None:
    """The gzip file ``src`` decompressed into ``dst`` a piece at a time (through ``dst`` + ".part"); a ``ValueError``
    naming the file - ``name``, or ``src``'s - when it is cut short or damaged."""
    src, dst = Path(src), Path(dst)
    try:
        with gzip.open(src, "rb") as packed, _writing(dst) as plain_file:
            shutil.copyfileobj(packed, plain_file, 1 << 20)
    except (EOFError, gzip.BadGzipFile, zlib.error) as error:
        raise ValueError(f"{name or src.name} is cut short or damaged ({error}): download it again") from None


def _renamed(error: Exception, src: Path, name: str) -> str:
    """The message of ``error``, which names ``src``, with ``name`` (the file the user gave) in its place."""
    text = str(error)
    return text.replace(src.name, name, 1) if src.name != name else text


def _undo_gzip(src: Path, dst: Path, name: str) -> None:
    gunzip(src, dst, name)


def _undo_compress(src: Path, dst: Path, name: str) -> None:
    try:
        lzw.decompress(src, dst)
    except ValueError as error:
        raise ValueError(_renamed(error, src, name)) from None


def _undo_zip(src: Path, dst: Path, name: str) -> None:
    try:
        with zipfile.ZipFile(src) as archive:
            with archive.open(_member(archive, name)) as packed, _writing(dst) as plain_file:
                shutil.copyfileobj(packed, plain_file, 1 << 20)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as error:
        raise ValueError(f"{name} is not a zip archive that can be read ({error})") from None


def _undo_hatanaka(src: Path, dst: Path, name: str) -> None:
    try:
        _native.crx2rnx(src, dst)
    except ValueError as error:
        raise ValueError(_renamed(error, src, name)) from None
    except RuntimeError as error:  # no decoder: the core older than 0.6 and no CRX2RNX
        raise ValueError(f"{name}: {error}") from None


_UNDO = {"gzip": _undo_gzip, "compress": _undo_compress, "zip": _undo_zip, "hatanaka": _undo_hatanaka}


def _whole_lines(path: Path, name: str) -> None:
    """A ``ValueError`` unless the text in ``path``, which a compress layer gave, ends with a newline. compress writes
    no end mark and no checksum, so a file cut short decodes to its beginning without a word; RINEX, Compact RINEX and
    navigation files always end with a line end (and an empty text is left to say what it is). The error names the
    file ``name`` and the line the text stops in."""
    size = path.stat().st_size
    if size == 0:
        return
    with open(path, "rb") as f:
        f.seek(size - 1)
        if f.read(1) == b"\n":
            return
        f.seek(0)
        line = 1 + sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 20), b""))
    raise ValueError(f"{name}, line {line}: the text ends in the middle of this line, the file is cut short or "
                     "damaged: download it again")


def _unpack(path: Path, layers: tuple[str, ...], target: Path) -> None:
    """The text of ``path`` under all its ``layers``, written to ``target``: each layer undone into a file of a private
    folder beside ``target``, the last one renamed to ``target``, which is there only when it is whole (and is never
    written by two runs at once). The text a compress layer gives (that of RINEX or of Compact RINEX) has to end with a
    newline, or the file is cut short. Errors name ``path``, the file the user gave."""
    scratch = Path(tempfile.mkdtemp(prefix=".unpack-", dir=target.parent))
    try:
        source = path
        for i, layer in enumerate(layers):
            out = scratch / f"layer{i + 1}"
            _UNDO[layer](source, out, path.name)
            if layer == "compress" and layers[i + 1:i + 2] in ((), ("hatanaka",)):
                _whole_lines(out, path.name)
            source = out
        os.replace(source, target)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:16]


# the decoded copies of the cache of the program (prune_cache): those not used for so many days go, then the least used
# beyond so many bytes - a day of a station at 1 s is gigabytes decoded -; a copy used, or a scratch folder of an
# unpacking made, within IN_USE seconds is never touched: a run may be reading or writing it
CACHE_DAYS = 14
CACHE_BYTES = 1 << 30
IN_USE = 3600.0
_COPY = re.compile(r"[0-9a-f]{16}-[0-9a-f]{16}\.rnx")  # the name _plain gives a decoded copy: nothing else is pruned


def _plain(path: Path, layers: tuple[str, ...], folder: str | Path | None) -> Path:
    """:func:`plain` for a file whose ``layers`` are known. The copy is called after a hash of the whole path of the
    file and a hash of its size, its modification time, its layers and - with a Hatanaka layer - who decodes it
    (:func:`.._native.crinex_decoder`): the same file changed, or decoded by a new decoder, has another copy, and the
    copies of its earlier states are removed. A copy used again is marked used (its access time) for
    :func:`prune_cache`."""
    if not layers or layers == ("gzip",):
        return path
    stat = path.stat()
    cache = Path(folder) if folder is not None else download.cache_dir("rinex")
    whose = _hash(str(path.resolve()))
    decoder = _native.crinex_decoder() if "hatanaka" in layers else ""
    copy = cache / f"{whose}-{_hash(f'{stat.st_size}|{stat.st_mtime_ns}|{chr(43).join(layers)}|{decoder}')}.rnx"
    if not copy.exists():
        cache.mkdir(parents=True, exist_ok=True)
        _unpack(path, layers, copy)
        for old in cache.glob(f"{whose}-*.rnx"):
            if old != copy:
                old.unlink(missing_ok=True)
    else:
        try:
            os.utime(copy, ns=(time.time_ns(), copy.stat().st_mtime_ns))
        except OSError:  # a cache that cannot be written: used as it is
            pass
    return copy


def prune_cache(folder: str | Path | None = None, days: float = CACHE_DAYS, size: int = CACHE_BYTES,
                now: float | None = None) -> list[Path]:
    """The cache of decoded copies (``folder``, by default the program's, :func:`download.cache_dir`) kept within
    ``days`` and ``size`` bytes: the copies (named as :func:`_plain` names them, ``<hash>-<hash>.rnx``) not used for
    ``days`` go, then the least used until the copies take ``size`` bytes at most; the scratch folders of an unpacking
    that stopped (``.unpack-*``) and the copies never finished (``<hash>-<hash>.rnx.part``) go too. What was used or
    made within :data:`IN_USE` seconds of ``now`` is never removed (a run may be reading or writing it), nor a file
    that cannot be removed (open elsewhere on Windows), nor anything the program did not write there - another
    ``.rnx`` included. Returns what was removed. :func:`..stations.download_observations` calls it before it
    downloads."""
    cache = Path(folder) if folder is not None else download.cache_dir("rinex")
    now = time.time() if now is None else now
    removed: list[Path] = []
    copies: list[tuple[float, int, Path]] = []
    try:
        entries = list(cache.iterdir())
    except OSError:
        return removed
    for entry in entries:
        try:
            stat = entry.stat()
        except OSError:
            continue
        used = max(stat.st_atime, stat.st_mtime)
        ours = (entry.name.startswith(".unpack-") and entry.is_dir()
                or entry.name.endswith(".part") and _COPY.fullmatch(entry.name[:-5]) is not None)
        if _COPY.fullmatch(entry.name) and entry.is_file():
            copies.append((used, stat.st_size, entry))
        elif ours and now - used >= IN_USE:
            if _removed(entry):
                removed.append(entry)
    copies.sort(key=lambda item: item[0])
    total = sum(item[1] for item in copies)
    for used, bytes_, entry in copies:
        if now - used < IN_USE:
            continue
        if now - used >= days * 86400.0 or total > size:
            if _removed(entry):
                removed.append(entry)
                total -= bytes_
    return removed


def _removed(path: Path) -> bool:
    """``path`` removed (a folder with what is in it); False when it cannot be (held open elsewhere)."""
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError:
        return False
    return True


def plain(path: str | Path, folder: str | Path | None = None) -> Path:
    """The file to read the text of ``path`` from: ``path`` itself when it is text or only gzip (RINEX is read through
    gzip as it is), otherwise a decompressed copy of it - under all its layers: the Unix compress, zip (an archive of
    one file), Hatanaka - in ``folder`` (by default the cache of the program, :func:`download.cache_dir`), made once and
    used again as long as the size and the time of the file are the same. The copy is written beside itself and renamed
    when it is whole, so a file that is damaged or cut short leaves nothing: it is a ``ValueError`` naming the file (and
    the line, for a Hatanaka file and for text under the Unix compress that does not end with a newline, which has no
    checksum to tell it is cut short), or the files of an archive that holds several. A Hatanaka file needs a decoder
    (``_native.crx2rnx``): without one it is a ``ValueError`` that says what to install."""
    path = Path(path)
    return _plain(path, compression(path), folder)
