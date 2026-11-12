"""A camera raw file written from an image: a minimal DNG (uncompressed, 16 bit; the RGGB mosaic of a camera, or
the three colours per pixel of the linear DNG a phone writes) with the tags a camera writes (body, lens, focal
length, time, GPS, orientation). For the tests and the demonstrations of the raw
import (:mod:`..io.raw`): no camera file is needed, and what the developed photo must look like is known."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

_BYTE, _ASCII, _SHORT, _LONG, _RATIONAL, _SRATIONAL = 1, 2, 3, 4, 5, 10
_SIZES = {_BYTE: 1, _ASCII: 1, _SHORT: 2, _LONG: 4, _RATIONAL: 8, _SRATIONAL: 8}
# XYZ (D65) -> linear sRGB: with it as the colour matrix the "camera" colours are linear sRGB
_XYZ_TO_SRGB = (3.2404542, -1.5371385, -0.4985314, -0.9692660, 1.8760108, 0.0415560, 0.0556434, -0.2040259, 1.0572252)
WHITE = 65535


def _pack(kind: int, values) -> bytes:
    if kind == _ASCII:
        return values.encode("ascii") + b"\x00"
    if kind == _BYTE:
        return bytes(values)
    if kind == _SHORT:
        return struct.pack(f"<{len(values)}H", *values)
    if kind == _LONG:
        return struct.pack(f"<{len(values)}L", *values)
    out = b""
    for v in values:  # rationals with a denominator of 10^6
        out += struct.pack("<lL" if kind == _SRATIONAL else "<LL", int(round(v * 1_000_000)), 1_000_000)
    return out


class _Ifd:
    """The entries of one TIFF directory; :meth:`build` lays it out at an offset, its long values after it."""

    def __init__(self):
        self.entries: dict[int, tuple[int, bytes]] = {}

    def add(self, tag: int, kind: int, values) -> None:
        self.entries[tag] = (kind, _pack(kind, values))

    def size(self) -> int:
        return 2 + 12 * len(self.entries) + 4 + sum(len(d) + len(d) % 2 for _k, d in self.entries.values() if len(d) > 4)

    def build(self, offset: int) -> bytes:
        head = struct.pack("<H", len(self.entries))
        extra = b""
        where = offset + 2 + 12 * len(self.entries) + 4
        for tag in sorted(self.entries):
            kind, data = self.entries[tag]
            count = len(data) // _SIZES[kind]
            if len(data) <= 4:
                value = data.ljust(4, b"\x00")
            else:
                value = struct.pack("<L", where + len(extra))
                extra += data + b"\x00" * (len(data) % 2)
            head += struct.pack("<HHL", tag, kind, count) + value
        return head + struct.pack("<L", 0) + extra


def linear(rgb: np.ndarray) -> np.ndarray:
    """An 8-bit sRGB-like image as linear values between 0 and 1: the inverse of the BT.709 curve LibRaw applies
    when it develops, so that the developed photo looks like ``rgb``."""
    v = np.asarray(rgb, np.float64) / 255.0
    return np.where(v < 0.081, v / 4.5, ((v + 0.099) / 1.099) ** (1 / 0.45))


def bayer(rgb: np.ndarray) -> np.ndarray:
    """The RGGB mosaic (uint16, linear, white at :data:`WHITE`) of an 8-bit sRGB-like image."""
    lin = linear(rgb)
    raw = np.empty(lin.shape[:2], np.float64)
    raw[0::2, 0::2] = lin[0::2, 0::2, 0]
    raw[0::2, 1::2] = lin[0::2, 1::2, 1]
    raw[1::2, 0::2] = lin[1::2, 0::2, 1]
    raw[1::2, 1::2] = lin[1::2, 1::2, 2]
    return np.clip(np.round(raw * WHITE), 0, WHITE).astype("<u2")


def _dms(value: float) -> list[float]:
    seconds = round(abs(value) * 3600, 4)
    d, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return [d, m, s]


def write_dng(path, rgb: np.ndarray, *, make: str = "PPG", model: str = "Synthetic", lens: str | None = None,
              focal_length_mm: float | None = None, focal_length_35mm: float | None = None,
              datetime: str | None = "2026:01:01 12:00:00", gps: tuple[float, float, float] | None = None,
              orientation: int = 1, xmp: str | None = None, mosaic: bool = True) -> Path:
    """Write ``rgb`` (H x W x 3, uint8, even sides) as a Bayer DNG at ``path``. ``gps`` is ``(lon, lat, alt)``;
    ``orientation`` the EXIF value (6 or 8: the camera was held upright; the pixels stay as the sensor read them);
    ``mosaic`` False writes a linear DNG (three colours per pixel, as the raws of the phones)."""
    rgb = np.asarray(rgb)
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.shape[0] % 2 or rgb.shape[1] % 2:
        raise ValueError("write_dng needs an H x W x 3 image with even sides")
    h, w = rgb.shape[:2]
    if mosaic:
        data = bayer(rgb).tobytes()
    else:
        data = np.clip(np.round(linear(rgb) * WHITE), 0, WHITE).astype("<u2").tobytes()

    exif = _Ifd()
    if datetime:
        exif.add(0x9003, _ASCII, datetime)
    if focal_length_mm:
        exif.add(0x920A, _RATIONAL, [focal_length_mm])
    if focal_length_35mm:
        exif.add(0xA405, _SHORT, [int(round(focal_length_35mm))])
    if lens:
        exif.add(0xA434, _ASCII, lens)
    gps_ifd = None
    if gps is not None:
        lon, lat, alt = gps
        gps_ifd = _Ifd()
        gps_ifd.add(0, _BYTE, [2, 3, 0, 0])
        gps_ifd.add(1, _ASCII, "N" if lat >= 0 else "S")
        gps_ifd.add(2, _RATIONAL, _dms(lat))
        gps_ifd.add(3, _ASCII, "E" if lon >= 0 else "W")
        gps_ifd.add(4, _RATIONAL, _dms(lon))
        gps_ifd.add(5, _BYTE, [0 if alt >= 0 else 1])
        gps_ifd.add(6, _RATIONAL, [abs(alt)])

    main = _Ifd()
    main.add(254, _LONG, [0])
    main.add(256, _LONG, [w])
    main.add(257, _LONG, [h])
    main.add(258, _SHORT, [16] if mosaic else [16, 16, 16])
    main.add(259, _SHORT, [1])
    main.add(262, _SHORT, [32803 if mosaic else 34892])  # colour filter array, or linear raw
    main.add(271, _ASCII, make)
    main.add(272, _ASCII, model)
    main.add(273, _LONG, [0])  # the strip: placed below
    main.add(274, _SHORT, [orientation])
    main.add(277, _SHORT, [1 if mosaic else 3])
    main.add(278, _LONG, [h])
    main.add(279, _LONG, [len(data)])
    main.add(284, _SHORT, [1])
    if datetime:
        main.add(306, _ASCII, datetime)
    if xmp:
        main.add(700, _BYTE, xmp.encode("utf-8"))
    if mosaic:
        main.add(33421, _SHORT, [2, 2])
        main.add(33422, _BYTE, [0, 1, 1, 2])  # RGGB
    main.add(0x8769, _LONG, [0])
    if gps_ifd is not None:
        main.add(0x8825, _LONG, [0])
    main.add(50706, _BYTE, [1, 4, 0, 0])
    main.add(50707, _BYTE, [1, 1, 0, 0])
    main.add(50708, _ASCII, f"{make} {model}")
    main.add(50714, _SHORT, [0])
    main.add(50717, _LONG, [WHITE])
    main.add(50721, _SRATIONAL, _XYZ_TO_SRGB)
    main.add(50728, _RATIONAL, [1.0, 1.0, 1.0])
    main.add(50778, _SHORT, [21])  # D65

    at_exif = 8 + main.size()
    at_gps = at_exif + exif.size()
    at_data = at_gps + (gps_ifd.size() if gps_ifd is not None else 0)
    main.add(0x8769, _LONG, [at_exif])
    if gps_ifd is not None:
        main.add(0x8825, _LONG, [at_gps])
    main.add(273, _LONG, [at_data])
    out = struct.pack("<2sHL", b"II", 42, 8) + main.build(8) + exif.build(at_exif)
    if gps_ifd is not None:
        out += gps_ifd.build(at_gps)
    assert len(out) == at_data, (len(out), at_data)
    path = Path(path)
    path.write_bytes(out + data)
    return path
