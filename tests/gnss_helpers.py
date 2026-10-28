"""Helpers shared by the tests of the GNSS work (test_gnss, test_gnss_ppk, test_gnss_rinex_files, test_gnss_ppp,
test_gnss_frames): RTKLIB's options and solution files as the tests read and write them, the mark of the tests that need
RTKLIB, offsets east, north and up, RINEX observation files cut into their records, written in pieces, and with their
header changed, photos with their time and position, a DJI flight on the day of RTKLIB's sample data, and RTKLIB as a
stand-in program. They live in a module of their own, so that a test file can be changed without breaking the
others."""
from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

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


# --- photos, a DJI flight on the day of RTKLIB's sample data, RTKLIB as a stand-in program -------------------------

RTKLIB_DATA = Path(__file__).parent / "data" / "rtklib"
PPK_ROVER = RTKLIB_DATA / "30400920.05o"  # the static station 3040, processed as a moving rover
PPK_BASE = RTKLIB_DATA / "07590920.05o"  # 0759, 3.3 km away
PPK_NAV = RTKLIB_DATA / "07590920.05n"
EPOCHS = 120  # 00:00:00 to 00:59:30 every 30 s, in both files


def jpeg(path, xmp: str | None, when: datetime | None = None):
    """A photo with an EXIF position and, with ``when``, the local time the camera wrote."""
    img = Image.fromarray(np.random.default_rng(0).integers(0, 255, (48, 64, 3), dtype=np.uint8))
    exif = Image.Exif()
    exif.get_ifd(0x8825).update({1: "N", 2: (41, 8, 0.0), 3: "E", 4: (14, 15, 0.0), 5: 0, 6: 123.4})
    if when is not None:
        exif.get_ifd(0x8769)[36867] = when.strftime("%Y:%m:%d %H:%M:%S")  # DateTimeOriginal
    kwargs = {"exif": exif}
    if xmp is not None:
        kwargs["xmp"] = xmp.encode()
    img.save(path, **kwargs)
    return path


def snapshot(chunk):
    """The references of the photos of ``chunk`` and what is recorded of them: to check that nothing changed."""
    return {c.label: (c.reference.location, c.reference.accuracy, c.reference.enabled, dict(c.meta.get("gnss") or {}),
                      c.meta.get("reference_crs")) for c in chunk.cameras}


MRK_LAT = 35.13208  # the drone's own single position in the MRK lines: 2.5 m from the PPK one


def mrk_line(index: int, gps: float, arm: tuple[int, int, int], lat: float = MRK_LAT) -> str:
    """A line of a Timestamp.MRK: shot ``index`` at ``gps`` seconds of GPS time with the lever arm ``arm`` (north, east,
    down in mm) and the antenna at latitude ``lat``, where the drone's own single position put it."""
    week, seconds = divmod(gps, WEEK)
    north, east, down = arm
    return (f"{index}\t{seconds:.6f}\t[{int(week)}]\t{north:6d},N\t{east:6d},E\t{down:6d},V\t{lat:.8f},Lat\t"
            "139.62428000,Lon\t74.100,Ellh\t1.2, 1.4, 2.9\t16,Q\n")


def ppk_flight(folder: Path, shots, lat: float = MRK_LAT) -> Path:
    """A DJI flight on the day of the RTKLIB sample data: for each ``(index, gps, arm)`` of ``shots`` a photo
    DJI_<index>.JPG with the time its camera wrote (in Japan, UTC+9: GPS time less the 13 leap seconds of 2005, plus
    9 hours, to the second) and its line in the Timestamp.MRK beside the photos, the antenna at latitude ``lat``."""
    folder.mkdir(parents=True)
    for index, gps, _arm in shots:
        jpeg(folder / f"DJI_{index:04d}.JPG", None, when=datetime(1980, 1, 6) + timedelta(seconds=gps - LEAP_2005,
                                                                                           hours=9))
    (folder / "Timestamp.MRK").write_text("".join(mrk_line(*shot, lat=lat) for shot in shots), newline="\n")
    return folder


# the qualities of a drone's solution away from fix (sdn, sde, sdu as RTKLIB writes them), by epoch
NOT_FIX = {1: (2, (0.05, 0.04, 0.08)), 3: (4, (0.30, 0.25, 0.50)), 4: (5, (0.40, 0.45, 0.90))}


def drone_rows() -> list[tuple]:
    """The rows of a solution of a drone on the epochs of the RTKLIB sample data (every 30 s from 00:00:00 GPS time),
    the antenna a little further at every epoch: fix, but float at the second epoch, dgps at the fourth and single at
    the fifth; and one epoch more (float), a second after the sixth."""
    rows = []
    for k in range(EPOCHS):
        q, (sdn, sde, sdu) = NOT_FIX.get(k, (1, (0.004, 0.003, 0.009)))
        rows.append((START_2005 + 30.0 * k, 35.1320660 + 1e-7 * k, 139.6243010 + 1e-7 * k, 75.690 + 0.001 * k, q, 7,
                     sdn, sde, sdu))
    rows.insert(6, (START_2005 + 151.0, 35.1320671, 139.6243022, 75.712, 2, 6, 0.06, 0.07, 0.12))
    return rows


def fake_rnx2rtkp(folder: Path) -> Path:
    """A stand-in for rnx2rtkp: appends to run.log beside it a JSON line with its arguments, the options file, what
    the folder of the first input holds, the SHA-256 of every input (the inputs as RTKLIB would find them) and of the
    antenna file the options name (none: null), copies the file named by PPG_FAKE_POS to its -o
    file (none: no output, as RTKLIB when it computed nothing), writes PPG_FAKE_MESSAGE to stderr as rnx2rtkp writes
    its messages and exits with PPG_FAKE_EXIT."""
    script = folder / "rnx2rtkp"
    log = folder / "run.log"
    script.write_text(f"""#!{sys.executable}
import hashlib, json, os, shutil, sys
args = sys.argv[1:]
conf, out, files = args[args.index("-k") + 1], args[args.index("-o") + 1], args[4:]
options = open(conf).read()
antenna = [l.split("=", 1)[1] for l in options.splitlines() if l.startswith("file-rcvantfile")]
entry = {{"args": args, "options": options, "folder": sorted(os.listdir(os.path.dirname(files[0]))),
          "inputs": [hashlib.sha256(open(f, "rb").read()).hexdigest() for f in files],
          "antenna": hashlib.sha256(open(antenna[0], "rb").read()).hexdigest() if antenna else None}}
open({str(log)!r}, "a").write(json.dumps(entry) + "\\n")
if os.environ.get("PPG_FAKE_POS"):
    shutil.copyfile(os.environ["PPG_FAKE_POS"], out)
sys.stderr.write(os.environ.get("PPG_FAKE_MESSAGE", "processing : 2005/04/02 00:59:30.0 Q=1") + "\\r")
sys.exit(int(os.environ.get("PPG_FAKE_EXIT", "0")))
""")
    script.chmod(0o755)
    if os.name == "nt":  # no shebang lines on Windows: a batch file starts the script
        wrapper = folder / "rnx2rtkp.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        return wrapper
    return script


@pytest.fixture
def fake_rtklib(tmp_path, monkeypatch):
    """RTKLIB as the fake rnx2rtkp (the core switched off), writing the solution of POS_ROWS; returns the log of
    its runs (a list of dicts, read when called)."""
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setenv("PPG_NATIVE", "0")
    monkeypatch.setenv("PPG_RNX2RTKP", str(fake_rnx2rtkp(tools)))
    pos = tools / "canned.pos"
    pos.write_text(pos_text(), newline="")
    monkeypatch.setenv("PPG_FAKE_POS", str(pos))
    for name in ("PPG_FAKE_MESSAGE", "PPG_FAKE_EXIT"):
        monkeypatch.delenv(name, raising=False)

    def runs() -> list[dict]:
        log = tools / "run.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return runs
