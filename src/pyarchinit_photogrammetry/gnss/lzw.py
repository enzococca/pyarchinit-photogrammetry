"""Files compressed with the Unix ``compress`` (``.Z``), which the archives of the GNSS stations still give (``.d.Z``,
``.n.Z``, ``.o.Z``). Plain Python, decompression only.

The format: three bytes - 0x1F 0x9D, then the largest width of a code in the low five bits (9 to 16) and, in bit 7,
whether the code 256 clears the dictionary ("block mode", which compress writes) - and then the codes of a
Lempel-Ziv-Welch dictionary. The first 256 strings are the bytes; every code read adds a string to the dictionary,
the one of the code before it and the first byte of the one it names. A code is 9 bits wide until the dictionary needs
a tenth, and so on up to the largest width; its bits come lowest first. compress writes the codes eight at a time, in
as many bytes as their width: when the width grows or the dictionary is cleared, what is left of the group of eight is
padding, and the next group starts at the next byte. One quirk, shared by compress and every program that reads its
files: a stream starts with 511 as the largest code that fits, whatever its largest width, so a stream limited to 9
bits goes on with 10-bit codes once its dictionary has 512 entries."""
from __future__ import annotations

import io
import os
from collections.abc import Callable, Iterator
from pathlib import Path

MAGIC = b"\x1f\x9d"
MIN_BITS, MAX_BITS = 9, 16  # the widths compress writes
CLEAR = 256  # in block mode, the code that empties the dictionary
_BLOCK_MODE = 0x80  # in the third byte
_PIECE = 1 << 20  # bytes of output gathered before they are handed on


def chunks(read: Callable[[int], bytes], name: str = "the data", piece: int = _PIECE) -> Iterator[bytes]:
    """What a compress stream decompresses to, a piece (at least ``piece`` bytes, but the last) at a time. The stream
    is read with ``read(n)``, as a file's; ``name`` is what the errors call it. A ``ValueError`` when it is not a
    compress stream or has a code its dictionary cannot have. It has no end mark: a stream that stops stops there."""
    start = read(3)
    if len(start) < 3 or start[:2] != MAGIC:
        raise ValueError(f"{name} is not a compress (.Z) file")
    max_bits = start[2] & 0x1F
    if not MIN_BITS <= max_bits <= MAX_BITS:
        raise ValueError(f"{name} is not a compress (.Z) file: it says its codes are {max_bits} bits wide, and "
                         f"compress writes {MIN_BITS} to {MAX_BITS}")
    block = bool(start[2] & _BLOCK_MODE)
    limit = 1 << max_bits  # the dictionary stops growing here
    table = [bytes((i,)) for i in range(256)]
    if block:
        table.append(b"")  # 256 is the code that clears: it names no string
    bits, maxcode = MIN_BITS, (1 << MIN_BITS) - 1  # a dictionary with more than maxcode entries needs wider codes
    previous: bytes | None = None  # the string of the last code; none at the start and after a clear
    out: list[bytes] = []
    size = 0
    while True:
        if len(table) > maxcode:
            bits += 1
            maxcode = limit if bits == max_bits else (1 << bits) - 1
        group = read(bits)
        if not group:
            break
        word, mask, shift = int.from_bytes(group, "little"), (1 << bits) - 1, 0
        for _ in range(len(group) * 8 // bits):  # the codes that are whole
            if len(table) > maxcode:  # the width grew: what is left of the group is padding
                break
            code = (word >> shift) & mask
            shift += bits
            if block and code == CLEAR:
                del table[CLEAR + 1:]
                bits, maxcode, previous = MIN_BITS, (1 << MIN_BITS) - 1, None
                break  # and so is what is left of this group
            n = len(table)
            if previous is None:
                if code >= 256:
                    raise ValueError(f"{name} is damaged: it starts with a code ({code}) that is not a character")
                entry = table[code]
            else:
                if code < n:
                    entry = table[code]
                elif code == n:  # a string made of the one before it and its own first byte
                    entry = previous + previous[:1]
                else:
                    raise ValueError(f"{name} is damaged: it holds a code ({code}) that its dictionary does not have")
                if n < limit:
                    table.append(previous + entry[:1])
            previous = entry
            out.append(entry)
            size += len(entry)
        if size >= piece:
            yield b"".join(out)
            out, size = [], 0
    if out:
        yield b"".join(out)


def head(data: bytes, limit: int) -> bytes:
    """The first ``limit`` bytes (fewer when there are not so many) of what ``data``, the beginning of a compress
    stream, decompresses to: what is decoded before a break, nothing when it is not a compress stream."""
    out = bytearray()
    try:
        for piece in chunks(io.BytesIO(data).read, piece=1):
            out += piece
            if len(out) >= limit:
                break
    except ValueError:
        pass
    return bytes(out[:limit])


def decompress(src: str | Path, dst: str | Path) -> None:
    """The file ``src``, compressed with compress, decompressed into ``dst``. ``dst`` is written beside itself, as
    ``dst`` + ".part", and renamed when the whole is there: a file that is damaged is a ``ValueError`` naming it, and
    leaves nothing."""
    src, dst = Path(src), Path(dst)
    part = dst.with_name(dst.name + ".part")
    try:
        with open(src, "rb") as packed, open(part, "wb") as plain:
            for piece in chunks(packed.read, src.name):
                plain.write(piece)
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)
