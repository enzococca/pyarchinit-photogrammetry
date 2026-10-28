"""Antenna calibrations for the PPK: the ANTEX file of the IGS (``igs20.atx``), where the program keeps a copy of
it, the calibrations of the receiver antennas in it, and the search RTKLIB makes in it for the antenna of a base.

The phase centre of a GNSS antenna is not at its reference point: for an antenna of a station it is some centimetres
above it (more than 10 cm for some choke-ring antennas), by an amount that depends on the antenna, the radome and the
frequency. RTKLIB corrects for it when it is given the file with the calibrations of the antennas
(``file-rcvantfile``), finds the one of the base's antenna by the name in the header of its RINEX file, and applies
nothing when it finds none. :func:`lookup` makes the same search, so that the program can say which entry RTKLIB uses,
or that there is none.

An ANTEX file is made of antennas between ``START OF ANTENNA`` and ``END OF ANTENNA``; the labels are in columns 61 to
80 and the first line says ``ANTEX VERSION / SYST``. The first 20 characters of the line ``TYPE / SERIAL NO`` are the
type of the antenna and its radome (``TRM57971.00     NONE``: the type in the first 16, the radome in the last 4),
the next 20 the serial number of an individually calibrated receiver antenna, or the code of a satellite (``G01``).
Each frequency (``G01``, ``G02``, ``R01``...) has its ``NORTH / EAST / UP`` offset of the phase centre, in
millimetres, and its variations with the angle from the zenith."""
from __future__ import annotations

import functools
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from . import download

log = logging.getLogger(__name__)

ATX_URL = "https://files.igs.org/pub/station/general/igs20.atx"
FILE_NAME = "igs20.atx"  # in the ``atx`` folder of the GNSS data of the program
MAX_AGE_DAYS = 30  # a copy this old is downloaded again, when the address answers
_DAY = 86400.0
_SATELLITE = re.compile(r"[GRECJIS]\d\d")  # the code of a satellite, as the serial number of its antenna


# --- the copy the program keeps ---------------------------------------------------------------------------------


def atx_dir() -> Path:
    """The folder of the copy of ``igs20.atx``: ``atx`` in the GNSS data of the program (not made)."""
    return download.gnss_dir() / "atx"


def cached_atx() -> Path | None:
    """The copy of ``igs20.atx`` the program keeps, however old it is; None when there is none. Never downloads."""
    path = atx_dir() / FILE_NAME
    return path if path.is_file() else None


def atx_file(refresh: bool = False, progress=None) -> Path:
    """``igs20.atx``, the antenna calibrations of the IGS (about 60 MB), in the folder of the program: the copy it has
    when that is less than :data:`MAX_AGE_DAYS` days old and ``refresh`` is not set, else a new one from
    :data:`ATX_URL`. What is downloaded is checked to be a whole ANTEX file (:func:`validate`) before it replaces the
    copy, so a page that a network with a login gives instead never does; a copy that is not one is no copy. When the
    address does not answer, or answers with something that is not an ANTEX file, and there is a copy, the copy is
    returned and the reason is logged as a warning; with none, a :class:`.download.DownloadError`."""
    target = atx_dir() / FILE_NAME
    have = target.is_file() and _usable(target)
    if have and not refresh and time.time() - target.stat().st_mtime < MAX_AGE_DAYS * _DAY:
        return target
    staging = target.with_name(f"{FILE_NAME}.{uuid.uuid4().hex[:8]}.new")
    try:
        download.fetch(ATX_URL, staging, progress)
        try:
            validate(staging)
        except ValueError as exc:
            raise download.DownloadError(f"{ATX_URL}: {exc}", ATX_URL) from exc
        os.replace(staging, target)
    except download.DownloadError as exc:
        if not have:
            raise
        made = datetime.fromtimestamp(target.stat().st_mtime)
        log.warning("%s could not be downloaded again (%s): the copy of %s stays in use", FILE_NAME, exc,
                    f"{made:%Y-%m-%d}")
        return target
    finally:
        staging.unlink(missing_ok=True)
    return target


def _usable(path: Path) -> bool:
    try:
        validate(path)
    except (ValueError, OSError):
        return False
    return True


def validate(path: str | Path) -> None:
    """A ``ValueError`` naming the file unless ``path`` is an ANTEX file that is whole and has antennas: it starts with
    the line ``ANTEX VERSION / SYST`` and ends with ``END OF ANTENNA`` (a page of a network that wants a login, or a
    download that stopped half-way, does not)."""
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(4096)
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - 4096))
        tail = f.read()
    if b"ANTEX VERSION / SYST" not in head.split(b"\n", 1)[0][60:]:
        raise ValueError(f"{path.name} is not an ANTEX file: its first line should end with ANTEX VERSION / SYST")
    last = [line for line in tail.splitlines() if line.strip()][-1:]
    label = last[0][60:].strip() if last else b""
    if label.startswith(b"END OF HEADER"):
        raise ValueError(f"{path.name} has no antenna in it")
    if not label.startswith(b"END OF ANTENNA"):
        raise ValueError(f"{path.name} is cut short: it should end with END OF ANTENNA (a download that stopped?)")


# --- the antennas of a file ---------------------------------------------------------------------------------------


class _Antenna(NamedTuple):
    """One antenna of an ANTEX file, as RTKLIB reads it (``readantex`` of rtkcmn.c)."""

    type: str  # the first 20 characters of its TYPE / SERIAL NO line, no blanks at their end: RTKLIB's pcv.type
    satellite: bool  # the next 20 characters are a satellite's code: its offsets are x, y, z, not north, east, up
    offsets: dict  # frequency -> (north, east, up), metres; of a receiver antenna
    valid_from: datetime | None
    valid_until: datetime | None


def _antex_time(text: bytes) -> datetime | None:
    """The time of a ``VALID FROM`` or ``VALID UNTIL`` line: year, month, day, hour, minute and second of GPS time (the
    second to 0.1 us, kept to the microsecond below it); None when the line does not hold one."""
    try:
        year, month, day, hour, minute = (int(v) for v in text.split()[:5])
        seconds = float(text.split()[5])
        return datetime(year, month, day, hour, minute) + timedelta(microseconds=int(seconds * 1e6))
    except (ValueError, IndexError, OverflowError):
        return None


def _read(path: Path) -> tuple[_Antenna, ...]:
    """The antennas of an ANTEX file in the order of the file. Only the lines RTKLIB looks at are read: from column 61
    on a line holds its label (a line of data starts with a number there, and is left alone), the lines after a
    ``START OF ANTENNA`` are the antenna's until the next ``END OF ANTENNA``. A receiver antenna keeps the offsets of
    every frequency it has (RTKLIB uses those of GPS)."""
    antennas: list[_Antenna] = []
    now: dict | None = None
    with open(path, "rb") as f:
        for raw in f:
            if len(raw) < 61:
                continue
            tag = raw[60:].strip()
            if not tag[:1].isalpha() or tag.startswith(b"COMMENT"):
                continue
            if tag.startswith(b"START OF ANTENNA"):
                now = {"type": None, "satellite": False, "offsets": {}, "from": None, "until": None, "frequency": None}
            elif now is None:
                continue
            elif tag.startswith(b"END OF ANTENNA"):
                if now["type"] is not None:
                    antennas.append(_Antenna(now["type"], now["satellite"], now["offsets"], now["from"], now["until"]))
                now = None
            elif tag.startswith(b"TYPE / SERIAL NO"):
                now["type"] = raw[:20].decode("latin-1").rstrip(" ")
                now["satellite"] = bool(_SATELLITE.fullmatch(raw[20:40].decode("latin-1").rstrip(" ")))
            elif tag.startswith(b"VALID FROM"):
                now["from"] = _antex_time(raw[:60])
            elif tag.startswith(b"VALID UNTIL"):
                now["until"] = _antex_time(raw[:60])
            elif tag.startswith(b"START OF FREQUENCY"):
                words = raw[:60].split()
                now["frequency"] = words[0].decode("latin-1") if words else None
            elif tag.startswith(b"END OF FREQUENCY"):
                now["frequency"] = None
            elif tag.startswith(b"NORTH / EAST / UP") and now["frequency"] and not now["satellite"]:
                try:  # mm to m, to 0.1 um: the file gives hundredths of a millimetre
                    north, east, up = (round(float(v) / 1000.0, 7) for v in raw[:60].split()[:3])
                except ValueError:  # fewer than three numbers, or not numbers
                    continue
                now["offsets"][now["frequency"]] = (north, east, up)
    return tuple(antennas)


@functools.lru_cache(maxsize=4)
def _cached_read(path: str, mtime_ns: int, size: int) -> tuple[_Antenna, ...]:
    return _read(Path(path))


def _antennas(path: str | Path) -> tuple[_Antenna, ...]:
    """The antennas of the file, read once for as long as the file is the same (the same size and time)."""
    path = Path(path).resolve()
    stat = path.stat()
    return _cached_read(str(path), stat.st_mtime_ns, stat.st_size)


def receiver_antennas(path: str | Path) -> dict[str, dict]:
    """The receiver antennas of an ANTEX file, in the order of the file, by the characters of their type and radome
    (``"TRM57971.00     NONE"``, without the blanks at the end): ``{"offsets": {"G01": (north, east, up), ...},
    "valid_from": datetime | None, "valid_until": datetime | None}``, the offsets of the phase centre from the
    reference point of the antenna in metres, for each frequency the antenna has. Satellites are left out. An antenna
    listed twice is the first one, the one RTKLIB finds."""
    found: dict[str, dict] = {}
    for antenna in _antennas(path):
        if antenna.satellite or antenna.type in found:
            continue
        found[antenna.type] = {"offsets": dict(antenna.offsets), "valid_from": antenna.valid_from,
                               "valid_until": antenna.valid_until}
    return found


def lookup(path: str | Path, antenna: str) -> tuple[str | None, bool]:
    """The entry RTKLIB uses for the antenna ``antenna`` (the type and radome as a RINEX header writes them), and
    whether it is the type without the radome of the header; ``(None, False)`` when it finds none and applies no
    calibration.

    This is RTKLIB's own search (``searchpcv`` of rtkcmn.c), antenna by antenna in the order of the file, satellites
    included: the first entry whose type holds all the words of ``antenna`` - the first two, split at blanks, anywhere
    in the type and in any order - or, when there is none, the first entry whose type starts with the first word; that
    is the type without the radome of the header (``True``). A header without a radome is found in the first entry of
    its type in the file, whatever its radome, and the letters have to be the same."""
    words = [word for word in antenna.split(" ") if word][:2]
    if not words:
        return None, False
    types = [entry.type for entry in _antennas(path)]
    for kind in types:
        if all(word in kind for word in words):
            return kind, False
    for kind in types:
        if kind.startswith(words[0]):
            return kind, True
    return None, False


def up_offset(path: str | Path, entry: str) -> float | None:
    """The offset of the phase centre of L1 (G01) above the reference point of the receiver antenna ``entry`` of the
    file (as :func:`lookup` names it), in metres; None when the file has no such receiver antenna or it has no G01."""
    found = receiver_antennas(path).get(entry.rstrip())
    offset = found["offsets"].get("G01") if found else None
    return offset[2] if offset else None
