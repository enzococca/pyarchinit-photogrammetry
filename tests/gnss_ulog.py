"""A minimal ULog writer for the tests (from the specification of the PX4 guide, "ULog File Format"), and a survey
flight of PX4 written with it: the GPS messages, the camera triggers and captures, the estimate of the position."""
from __future__ import annotations

import math
import struct
from datetime import datetime, timedelta

import numpy as np

MAGIC = b"ULog\x01\x12\x35"
SYNC = bytes([0x2F, 0x73, 0x13, 0x20, 0x25, 0x0C, 0xBB, 0x12])
_PACK = {"int8_t": "b", "uint8_t": "B", "int16_t": "h", "uint16_t": "H", "int32_t": "i", "uint32_t": "I",
         "int64_t": "q", "uint64_t": "Q", "float": "f", "double": "d", "bool": "?", "char": "c"}
LEAP_2025 = 18


def _message(kind: str, body: bytes) -> bytes:
    return struct.pack("<HB", len(body), ord(kind)) + body


class ULogWriter:
    """Formats ``F``, subscriptions ``A``, data ``D``, info ``I``/``M``, dropouts ``O``, sync ``S``; ``binary()`` the
    file, with ``appended`` the data written after the offset a ``B`` message gives (the log before it cut in the
    middle of a message, as the specification allows)."""

    def __init__(self, start_us: int = 1_000_000) -> None:
        self.start_us = start_us
        self.formats: dict[str, list[tuple[str, int, str]]] = {}
        self.definitions: list[bytes] = []
        self.body: list[bytes] = []
        self.appended: list[bytes] = []
        self.ids: dict[tuple[str, int], int] = {}

    def fmt(self, name: str, fields: list[tuple[str, int, str]]) -> None:
        """``fields``: (type, array length or 0, name)."""
        self.formats[name] = fields
        text = name + ":" + "".join(f"{t}{f'[{n}]' if n else ''} {f};" for t, n, f in fields)
        self.definitions.append(_message("F", text.encode()))

    def info(self, key: str, value) -> None:
        if isinstance(value, str):
            raw, typ = value.encode(), f"char[{len(value.encode())}]"
        else:
            raw, typ = struct.pack("<I", value), "uint32_t"
        k = f"{typ} {key}".encode()
        self.definitions.append(_message("I", bytes([len(k)]) + k + raw))

    def multi_info(self, key: str, parts: list[str]) -> None:
        for i, part in enumerate(parts):
            k = f"char[{len(part.encode())}] {key}".encode()
            self.definitions.append(_message("M", bytes([int(i > 0), len(k)]) + k + part.encode()))

    def subscribe(self, name: str, multi_id: int = 0, appended: bool = False) -> None:
        msg_id = len(self.ids)
        self.ids[(name, multi_id)] = msg_id
        (self.appended if appended else self.body).append(
            _message("A", struct.pack("<BH", multi_id, msg_id) + name.encode()))

    def _pack(self, name: str, values: dict) -> bytes:
        out = b""
        for typ, n, field in self.formats[name]:
            if typ in self.formats:
                for k in range(n or 1):
                    out += self._pack(typ, values.get(field, [{}] * (n or 1))[k] if n else values.get(field, {}))
                continue
            code = _PACK[typ]
            if typ == "char":
                raw = str(values.get(field, "")).encode()[:n or 1]
                out += raw + b"\0" * ((n or 1) - len(raw))
            elif n:
                vals = list(values.get(field, [0] * n))
                out += struct.pack(f"<{n}{code}", *vals)
            else:
                out += struct.pack(f"<{code}", values.get(field, 0))
        return out

    def data(self, name: str, values: dict, multi_id: int = 0, appended: bool = False, strip: int = 0) -> None:
        """A data message of the subscription ``(name, multi_id)``; ``strip`` bytes of trailing padding left out."""
        payload = self._pack(name, values)
        if strip:
            payload = payload[:-strip]
        (self.appended if appended else self.body).append(
            _message("D", struct.pack("<H", self.ids[(name, multi_id)]) + payload))

    def dropout(self, ms: int) -> None:
        self.body.append(_message("O", struct.pack("<H", ms)))

    def sync(self) -> None:
        self.body.append(_message("S", SYNC))

    def binary(self, cut: int = 0, garbage: bytes = b"") -> bytes:
        """The file; with appended data a ``B`` message points at it, ``cut`` bytes of the last message before it
        lost (the log stopped in the middle of it). ``garbage`` bytes go before the last sync of the body."""
        head = MAGIC + bytes([1]) + struct.pack("<Q", self.start_us)
        flags = b""
        if self.appended:
            flags = _message("B", bytes(8) + bytes([1]) + bytes(7) + struct.pack("<3Q", 0, 0, 0))
        body = list(self.body)
        if garbage:
            for i in range(len(body) - 1, -1, -1):
                if body[i][2] == ord("S"):
                    body.insert(i, garbage)
                    break
        main = head + flags + b"".join(self.definitions) + b"".join(body)
        if cut:
            main = main[:-cut]
        if not self.appended:
            return main
        offset = len(main)
        flags = _message("B", bytes(8) + bytes([1]) + bytes(7) + struct.pack("<3Q", offset, 0, 0))
        main = head + flags + main[len(head) + len(flags):]
        return main + b"".join(self.appended)


GPS_FORMAT = [("uint64_t", 0, "timestamp"), ("uint64_t", 0, "timestamp_sample"), ("double", 0, "latitude_deg"),
              ("double", 0, "longitude_deg"), ("double", 0, "altitude_msl_m"), ("double", 0, "altitude_ellipsoid_m"),
              ("uint64_t", 0, "time_utc_usec"), ("uint32_t", 0, "device_id"), ("float", 0, "eph"),
              ("float", 0, "epv"), ("uint8_t", 0, "fix_type"), ("uint8_t", 0, "satellites_used"),
              ("uint8_t", 2, "_padding0")]
GPS_FORMAT_17 = [("uint64_t", 0, "timestamp"), ("uint64_t", 0, "time_utc_usec"), ("int32_t", 0, "lat"),
                 ("int32_t", 0, "lon"), ("int32_t", 0, "alt"), ("int32_t", 0, "alt_ellipsoid"), ("float", 0, "eph"),
                 ("float", 0, "epv"), ("uint8_t", 0, "fix_type"), ("uint8_t", 7, "_padding0")]
CAPTURE_FORMAT = [("uint64_t", 0, "timestamp"), ("uint64_t", 0, "timestamp_utc"), ("double", 0, "lat"),
                  ("double", 0, "lon"), ("uint32_t", 0, "seq"), ("float", 0, "alt"), ("float", 0, "ground_distance"),
                  ("float", 4, "q"), ("int8_t", 0, "result"), ("uint8_t", 3, "_padding0")]
TRIGGER_FORMAT = [("uint64_t", 0, "timestamp"), ("uint64_t", 0, "timestamp_utc"), ("uint32_t", 0, "seq"),
                  ("bool", 0, "feedback"), ("uint8_t", 3, "_padding0")]
GPOS_FORMAT = [("uint64_t", 0, "timestamp"), ("double", 0, "lat"), ("double", 0, "lon"), ("float", 0, "alt"),
               ("uint8_t", 4, "_padding0")]
UTC0 = datetime(2025, 5, 18, 12, 0, 0)  # the UTC time of the first fix
LAT0, LON0, MSL0, SEPARATION = 43.70, 10.40, 120.0, 47.25
BOOT0_US = 60_000_000


def gps_seconds(utc: datetime) -> float:
    """GPS time (s from 6 January 1980) of a UTC time of 2025 (18 leap seconds)."""
    return (utc - datetime(1980, 1, 6)).total_seconds() + LEAP_2025


def flight(shots: int = 40, *, interval_s: float = 2.0, gps_hz: float = 5.0, gps: str = "fix", drift_ppm: float = 0.0,
           processing_ms: float = 30.0, utc_error_ms: float = 0.0, utc_is_gps: bool = False, failed=(),
           feedback: bool = True, captures: bool = True, separation: float | None = SEPARATION, layout: str = "1.15",
           seed: int = 1, appended_from: int | None = None, gps_utc: bool = True) -> tuple[ULogWriter, list[dict]]:
    """A PX4 survey flight: the GPS messages of one receiver every 1/``gps_hz`` s - with ``gps="fix"``
    ``time_utc_usec`` the epoch of the fix and ``timestamp`` its processing, 0-``processing_ms`` later (uniform); with
    ``"stamp"`` the receiver's UTC at the message and ``timestamp`` within 0.5 ms of it -, ``vehicle_global_position``
    at 10 Hz, and ``shots`` triggers ``interval_s`` apart (with ``feedback``) each copied into ``camera_capture``
    (``result`` 0 for the indices in ``failed``; none with ``captures`` False). The boot clock runs ``drift_ppm``
    fast; ``timestamp_utc`` of the triggers is off by ``utc_error_ms`` (and is GPS time with ``utc_is_gps``). The
    drone flies north at 8 m/s, AMSL ``MSL0`` + 0.5 m per shot. ``separation`` the geoid of the receiver (None: none
    given, ellipsoid = AMSL); ``layout`` "1.15" (``sensor_gps`` fields) or "1.7" (``lat``/``alt`` in 1e-7 deg and mm).
    From the shot ``appended_from`` on the camera messages go to appended data. Returns the writer and per shot
    ``seq``, ``true`` (GPS time), ``lat``, ``lon``, ``msl``."""
    rng = np.random.default_rng(seed)
    w = ULogWriter(start_us=BOOT0_US - 5_000_000)
    w.info("sys_name", "PX4")
    w.info("ver_sw_release", (1 << 24) | (15 << 16) | (4 << 8) | 255)
    w.fmt("vehicle_gps_position", GPS_FORMAT if layout == "1.15" else GPS_FORMAT_17)
    w.fmt("camera_trigger", TRIGGER_FORMAT)
    w.fmt("camera_capture", CAPTURE_FORMAT)
    w.fmt("vehicle_global_position", GPOS_FORMAT)
    for name in ("vehicle_gps_position", "vehicle_global_position", "camera_trigger"):
        w.subscribe(name)
    w.subscribe("camera_capture", appended=False)
    g0 = gps_seconds(UTC0)
    scale = 1.0 + drift_ppm * 1e-6

    def boot_us(g: float) -> int:
        return int(round(BOOT0_US + (g - g0) * 1e6 * scale))

    def position(g: float) -> tuple[float, float, float]:
        north = 8.0 * (g - g0)
        return LAT0 + north / 111_195.0, LON0, MSL0 + 0.5 * (g - g0) / interval_s

    end = g0 + 3.0 + shots * interval_s
    events = []
    k = 0
    while g0 + k / gps_hz <= end:
        g = g0 + k / gps_hz
        k += 1
        lat, lon, msl = position(g)
        ell = msl + separation if separation is not None else msl
        if gps == "fix":
            t = boot_us(g) + int(rng.uniform(0, processing_ms * 1000.0))
            utc_us = int(round((g - LEAP_2025 + 315964800) * 1e6))
        else:
            stamp = g + rng.uniform(0.010, 0.150)  # a time of the message, off the grid
            t = boot_us(stamp) + int(rng.uniform(0, 500))
            utc_us = int(round((stamp - LEAP_2025 + 315964800) * 1e6))
        if not gps_utc:
            utc_us = 0
        if layout == "1.15":
            values = dict(timestamp=t, timestamp_sample=123, latitude_deg=lat, longitude_deg=lon, altitude_msl_m=msl,
                          altitude_ellipsoid_m=ell, time_utc_usec=utc_us, device_id=(7 << 16) | 5, eph=0.02,
                          epv=0.03, fix_type=6, satellites_used=20)
        else:
            values = dict(timestamp=t, time_utc_usec=utc_us, lat=int(round(lat * 1e7)), lon=int(round(lon * 1e7)),
                          alt=int(round(msl * 1000)), alt_ellipsoid=int(round(ell * 1000)), eph=0.02, epv=0.03,
                          fix_type=6)
        events.append((t, "vehicle_gps_position", values, False))
    k = 0
    while g0 + k * 0.1 <= end:
        g = g0 + k * 0.1
        k += 1
        lat, lon, msl = position(g)
        events.append((boot_us(g), "vehicle_global_position", dict(timestamp=boot_us(g), lat=lat, lon=lon, alt=msl),
                       False))
    expected = []
    for n in range(shots):
        g = g0 + 1.234567 + n * interval_s
        t = boot_us(g)
        utc = g - (0 if utc_is_gps else LEAP_2025) + 315964800 + utc_error_ms / 1000.0
        lat, lon, msl = position(g)
        late = appended_from is not None and n >= appended_from
        events.append((t, "camera_trigger", dict(timestamp=t, timestamp_utc=int(round(utc * 1e6)), seq=n,
                                                 feedback=feedback), late))
        if captures:
            yaw = 0.0
            q = [math.cos(math.radians(yaw) / 2), 0.0, 0.0, math.sin(math.radians(yaw) / 2)]
            events.append((t + 1, "camera_capture", dict(timestamp=t, timestamp_utc=int(round(utc * 1e6)), lat=lat,
                                                         lon=lon, seq=n, alt=msl, ground_distance=50.0, q=q,
                                                         result=0 if n in failed else 1), late))
        expected.append({"seq": n, "true": g, "lat": lat, "lon": lon, "msl": msl})
    for t, name, values, late in sorted(events, key=lambda e: e[0]):
        w.data(name, values, appended=late, strip=3 if name == "camera_capture" else 0)
    return w, expected
