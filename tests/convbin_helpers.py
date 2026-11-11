"""Helpers of the tests of the conversion of raw receiver logs into RINEX (test_convbin): UBX frames written from the
u-blox Interface Description (UBX-18010854: a frame is 0xB5 0x62, class, id, a little-endian length of the payload,
the payload and the two bytes of the 8-bit Fletcher checksum over class, id, length and payload) and the TIM-TM2 time
mark message (class 0x0D, id 0x03, 28 bytes) put between the frames of a log. They are used by the tests and, once,
to make the fixtures of tests/data/gnss/phase4 (see its README.md)."""
from __future__ import annotations

import struct

WEEK = 604800.0

# TIM-TM2 flags (Interface Description, UBX-TIM-TM2): bit 2 newFallingEdge, bits 3-4 timeBase (0 receiver time,
# 1 GNSS time, 2 UTC), bit 6 time is valid, bit 7 newRisingEdge
FALLING = 1 << 2
TIME_VALID = 1 << 6
BASE_GNSS = 1 << 3
BASE_UTC = 2 << 3


def checksum(body: bytes) -> bytes:
    """The two bytes of the 8-bit Fletcher checksum of a UBX frame, over class, id, length and payload."""
    a = b = 0
    for byte in body:
        a = (a + byte) & 0xFF
        b = (b + a) & 0xFF
    return bytes((a, b))


def ubx_frame(cls: int, msg_id: int, payload: bytes) -> bytes:
    body = struct.pack("<BBH", cls, msg_id, len(payload)) + payload
    return b"\xb5\x62" + body + checksum(body)


def tim_tm2(week: int, seconds: float, *, flags: int = FALLING | BASE_GNSS | TIME_VALID, count: int = 0,
            channel: int = 0) -> bytes:
    """A TIM-TM2 frame whose falling edge (and rising edge, 1 ms before) is at ``seconds`` of the week ``week`` of the
    time base in ``flags``; the time of week as whole milliseconds plus nanoseconds, as the receiver gives it."""
    ms = int(seconds * 1000.0)
    sub_ns = int(round((seconds * 1000.0 - ms) * 1e6))
    rise_ms = ms - 1
    payload = struct.pack("<BBHHHIIIII", channel, flags, count, week, week, rise_ms, sub_ns, ms, sub_ns, 20)
    return ubx_frame(0x0D, 0x03, payload)


def ubx_frames(data: bytes):
    """(offset, class, id, payload) of every whole UBX frame of ``data``, in order; the bytes between them (NMEA,
    noise) are skipped."""
    i = 0
    while i + 8 <= len(data):
        if data[i] == 0xB5 and data[i + 1] == 0x62:
            length = struct.unpack_from("<H", data, i + 4)[0]
            end = i + 8 + length
            if end <= len(data) and data[end - 2:end] == checksum(data[i + 2:end - 2]):
                yield i, data[i + 2], data[i + 3], data[i + 6:end - 2]
                i = end
                continue
        i += 1


def raw_epochs(data: bytes) -> list[tuple[int, int, float]]:
    """(end offset of the frame, GPS week, time of week in s) of every RXM-RAW (0x02 0x10) or RXM-RAWX (0x02 0x15)
    frame of a UBX log."""
    out = []
    for offset, cls, msg_id, payload in ubx_frames(data):
        if cls == 0x02 and msg_id == 0x10:  # RXM-RAW: iTOW ms (I4), week (I2)
            tow_ms, week = struct.unpack_from("<iH", payload, 0)
            out.append((offset + 8 + len(payload), week, tow_ms / 1000.0))
        elif cls == 0x02 and msg_id == 0x15:  # RXM-RAWX: rcvTow (R8), week (U2)
            tow, week = struct.unpack_from("<dH", payload, 0)
            out.append((offset + 8 + len(payload), week, tow))
    return out


def insert_time_marks(data: bytes, marks: list[bytes], gps_seconds: list[float]) -> bytes:
    """``data`` with every frame of ``marks`` put right after the first raw observation epoch at or after the GPS time
    ``gps_seconds`` of the same index (seconds since 1980-01-06), as a receiver sends a time mark after the epoch that
    follows it."""
    epochs = raw_epochs(data)
    places = []
    for frame, when in zip(marks, gps_seconds):
        end = next(e for e, week, tow in epochs if week * WEEK + tow >= when)
        places.append((end, frame))
    out, last = bytearray(), 0
    for end, frame in sorted(places, key=lambda p: p[0]):
        out += data[last:end] + frame
        last = end
    out += data[last:]
    return bytes(out)
