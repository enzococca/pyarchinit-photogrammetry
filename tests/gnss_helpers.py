"""Helpers shared by the tests of the GNSS work (test_gnss_ppk, test_gnss_rinex_files, test_gnss_ppp): RTKLIB's
options and solution files as the tests read and write them, the mark of the tests that need RTKLIB, offsets east,
north and up, and RINEX observation files cut into their records, written in pieces, and with their header changed.
They live in a module of their own, so that a test file can be changed without breaking the others."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from pyarchinit_photogrammetry import _native

# --- RTKLIB's options and solutions ------------------------------------------------------------------------------

WEEK = 604800.0
START_2005 = 1316 * WEEK + 518400.0  # 2005-04-02 00:00:00 GPS time: GPS week 1316, Saturday
LEAP_2005 = 13  # GPS time was 13 s ahead of UTC in 2005

needs_rtklib = pytest.mark.skipif(not _native.supports("ppk"), reason="native core without RTKLIB (older than 0.5)")


def rtklib_options(text: str) -> dict[str, str]:
    """The ``name=value`` lines of an options file of RTKLIB, without its comments."""
    options = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, value = line.split("=", 1)
            options[name.strip()] = value
    return options


def rtklib_option_names() -> set[str]:
    """The names of the options RTKLIB knows, from its own table."""
    source = (Path(__file__).parents[1] / "native" / "vendor" / "rtklib" / "src" / "options.c").read_text()
    return set(re.findall(r'^\s*\{"([a-z0-9_-]+)",', source, re.MULTILINE | re.IGNORECASE))


POS_LEGEND = ("% (lat/lon/height=WGS84/ellipsoidal,Q=1:fix,2:float,3:sbas,4:dgps,5:single,6:ppp,"
              "ns=# of satellites)")
POS_COLUMNS = ("latitude(deg) longitude(deg)  height(m)   Q  ns   sdn(m)   sde(m)   sdu(m)  sdne(m)  sdeu(m)  sdun(m)"
               " age(s)  ratio")
# GPS seconds, latitude, longitude, height, Q, ns, sdn, sde, sdu: out of order, as the file need not be in order
POS_ROWS = [
    (START_2005 + 30.0, 35.132066167, 139.624300803, 75.6904, 2, 6, 0.1100, 0.1200, 0.1300),
    (START_2005, 35.132066159, 139.624300832, 75.6841, 1, 7, 0.0100, 0.0200, 0.0300),
    (START_2005 + 59.996, 35.132065640, 139.624303673, 75.7671, 5, 5, 1.1000, 1.2000, 2.3000),
]


def _pos_time(seconds: float, form: str, system: str) -> str:
    """A time of a solution row as RTKLIB writes it: GPS week and seconds of the week (tow) or the calendar (hms),
    in GPS time, UTC or Japan standard time."""
    seconds -= {"GPST": 0, "UTC": LEAP_2005, "JST": LEAP_2005 - 9 * 3600}[system]
    if form == "tow":
        week = int(seconds // WEEK)
        return f"{week:4d} {seconds - week * WEEK:10.3f}"
    when = datetime(1980, 1, 6) + timedelta(seconds=seconds)
    return f"{when:%Y/%m/%d %H:%M:%S}.{round(when.microsecond / 1000):03d}"


def pos_text(rows=POS_ROWS, form: str = "tow", system: str = "GPST", legend: str | None = POS_LEGEND,
             columns: str = POS_COLUMNS, sep: str = " ") -> str:
    """A solution file of RTKLIB in latitude, longitude and height."""
    lines = ["% program   : RTKLIB ver.EX 2.5.1", "% inp file  : rover.obs", "%"]
    if legend:
        lines.append(legend)
    lines.append(f"%  {system:<{16 if form == 'hms' else 8}}{sep}" + sep.join(columns.split()))
    for seconds, lat, lon, h, q, ns, sdn, sde, sdu in rows:
        values = [f"{lat:14.9f}", f"{lon:14.9f}", f"{h:10.4f}", f"{q:3d}", f"{ns:3d}", f"{sdn:8.4f}", f"{sde:8.4f}",
                  f"{sdu:8.4f}", "  0.0000", "  0.0000", "  0.0000", "  0.00", "   3.2"]
        lines.append(_pos_time(seconds, form, system) + sep + sep.join(values))
    return "\r\n".join(lines) + "\r\n"  # RTKLIB ends its lines with \r\n


def enu(llh: np.ndarray, origin) -> np.ndarray:
    """East, north, up offsets (m) of positions near ``origin``."""
    d = np.atleast_2d(llh) - np.asarray(origin)
    return np.c_[np.radians(d[:, 1]) * 6378137.0 * math.cos(math.radians(origin[0])), np.radians(d[:, 0]) * 6378137.0,
                 d[:, 2]]


# --- RINEX observation files -------------------------------------------------------------------------------------

EPOCH_V2 = re.compile(r"^ \d\d +\d+ +\d+ +\d+ +\d+ +[\d.]+ +0 ")


def rinex_lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


def header_and_body(lines: list[str]) -> tuple[list[str], list[str]]:
    end = next(i for i, line in enumerate(lines) if line.rstrip().endswith("END OF HEADER")) + 1
    return lines[:end], lines[end:]


def rinex_records(path: Path) -> tuple[list[str], list[list[str]]]:
    """The header of a RINEX file and its records, each an epoch line with the lines up to the next epoch (the event
    records after an epoch are its)."""
    header, body = header_and_body(rinex_lines(path))
    records: list[list[str]] = []
    for line in body:
        if line.startswith(">") or EPOCH_V2.match(line):
            records.append([line])
        else:
            records[-1].append(line)
    return header, records


def label(line: str) -> str:
    """The label of a header line: what is after its 60th column."""
    return line[60:].strip()


def card(content: str, name: str) -> str:
    """A header line: the content in the first 60 columns, its label ``name`` after them."""
    return content.ljust(60) + name


def last_obs_line(epoch: str, system: str = "GPS") -> str:
    """The TIME OF LAST OBS line of the observations whose last epoch line is ``epoch``."""
    fields = epoch.lstrip("> ").split()[:6]
    year = int(fields[0]) if epoch.startswith(">") else int(fields[0]) + (1900 if int(fields[0]) >= 80 else 2000)
    y, mo, d, h, mi = year, *(int(f) for f in fields[1:5])
    return f"{y:6d}{mo:6d}{d:6d}{h:6d}{mi:6d}{float(fields[5]):13.7f}     {system}".ljust(60) + "TIME OF LAST OBS"


def with_last_obs(header: list[str], epoch: str) -> list[str]:
    """``header`` with a TIME OF LAST OBS line of the epoch ``epoch`` (after the TIME OF FIRST OBS line)."""
    lines = [line for line in header if label(line) != "TIME OF LAST OBS"]
    i = next(k for k, line in enumerate(lines) if label(line) == "TIME OF FIRST OBS")
    return lines[:i + 1] + [last_obs_line(epoch)] + lines[i + 1:]


def rinex_piece(path: Path, header: list[str], records: list[list[str]], last_obs: bool = False) -> Path:
    """``records`` with ``header`` as a RINEX file, with the TIME OF LAST OBS of its own last epoch when asked."""
    lines = with_last_obs(header, records[-1][0]) if last_obs else list(header)
    for record in records:
        lines += record
    path.write_text("\n".join(lines) + "\n", newline="\n")
    return path


def rewrite_header(source: Path, target: Path, change) -> Path:
    """A copy of ``source`` whose header lines go through ``change(line)`` (a new line, or None to leave it out)."""
    lines, in_header = [], True
    for line in rinex_lines(source):
        if in_header:
            in_header = label(line) != "END OF HEADER"
            line = change(line) if in_header else line
            if line is None:
                continue
        lines.append(line)
    target.write_text("\n".join(lines) + "\n", newline="\n")
    return target


def on_label(name: str, content: str | None):
    """A ``change`` for :func:`rewrite_header`: the line labelled ``name`` gets ``content`` (None: it goes)."""
    def change(line: str):
        if label(line) != name:
            return line
        return None if content is None else card(content, name)

    return change
