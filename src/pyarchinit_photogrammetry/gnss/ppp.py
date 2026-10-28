"""The position of a base by static PPP - precise point positioning - on the precise products of the IGS and its
analysis centres: the user's own receiver, left for some hours on a mark that nobody surveyed, placed on its own,
without a permanent station, and then the base of the PPK of the flight.

RTKLIB solves the observations of the base (RINEX) as a receiver standing still for the whole session
(``ppp-static``), forward, into one position (``out-solstatic=single``), on two frequencies with the ionosphere-free
combination and the zenith delay of the troposphere estimated, with the precise orbits (SP3) and clocks (RINEX
clock) of the satellites - from RTKLIB-EX 2.5.1 no broadcast navigation is needed with them - the Earth rotation
parameters (ERP) and the antenna calibrations of the IGS (igs20.atx), for the satellites' antennas
(``file-satantfile``) and for the base's (``file-rcvantfile``). The products of a session are all of one centre and
of one kind, the best that is published: the final ones of ESA (about 4 days after the day), of CODE (about 9
days), of the IGS (two weeks or more), else the rapid ones of the IGS (about 17 hours); never the ultra-rapid ones,
which are half predicted. They are in IGS20 - ITRF2020 for the program - so the mark is in ITRF2020 at the epoch of
the middle of the session.

RTKLIB-EX makes float PPP solutions only (its ``ppp_ar`` is a stub): the quality RTKLIB gives them is 6 ("ppp"), and
a solution of any other quality is refused. What to expect: 10-20 cm with less than 2 hours of observations, a few
centimetres with 4 hours or more, 1-2 cm across and 2-4 cm in height after a day; under 4 hours a static baseline
from a permanent station nearby (:func:`.base.baseline`) places a base better.

The products are downloaded one file at a time over HTTPS from public archives that ask for no account - the
Navigation Support Office of ESA/ESOC, CODE at the University of Bern, the IGS at BKG - and kept, as the archives name
them, in ``products/<centre>`` of the GNSS folder of the program (:func:`.download.gnss_dir`). Times are GPS time
without a time zone, as :func:`.rinex.header` gives them."""
from __future__ import annotations

import gzip
import logging
import math
import os
import shutil
import tempfile
import urllib.parse
import uuid
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..core.progress import as_progress
from . import atx as antex
from . import download, frames, ppk, rinex, stations

log = logging.getLogger(__name__)

SHORT_H = 2.0  # a static PPP of fewer hours than this is 10-20 cm off at best
STATION_BETTER_H = 4.0  # under this a static baseline from a permanent station nearby places a base better
GRADIENTS_H = 4.0  # from this many hours the gradients of the troposphere are estimated too (est-ztdgrad)
FRAME = "ITRF2020"  # the frame of the products of the IGS and its centres (IGS20, which the program takes as ITRF2020)
Q_PPP = 6  # RTKLIB's quality of a PPP solution (ppk.QUALITY)
KINDS = ("final", "rapid")  # the kinds of products a base is placed on: never the ultra-rapid ones
FINAL_DELAY = timedelta(days=4)  # ESA's final products come about this long after the end of their day
RAPID_DELAY = timedelta(hours=17)  # the rapid ones of the IGS this long
# in the folder of solve_static: the products downloaded for the session, decompressed, and RTKLIB's work folder,
# side by side (nothing RTKLIB is given may be in its work folder, which is emptied before every run)
PRODUCTS_FOLDER = "products"
WORK_FOLDER = "ppp"
ERP_COPY = "rotation.erp"  # the ERP of the session among RTKLIB's inputs: RTKLIB reads only files called .erp or .ERP
# the IGS and its centres name their products by the long names of these patterns from GPS week 2238 on
LONG_NAMES = date(2022, 11, 27)
_GPS_EPOCH = date(1980, 1, 6)
# how the messages name the receiver of a PPP, the user's own on a mark: as the antenna warnings do ("your base")
_YOUR_BASE = ppk.ROLES["ppp"]
# what RTKLIB-EX's readerp looks for in an ERP file, as it spells it: the line of the version, the line that names the
# columns, and the characters of a line of numbers
_ERP_VERSION = ("version 2", "VERSION 2")
_ERP_COLUMNS = ("MJD", "mjd", "Xpole", "xpole", "Ypole", "ypole", "UT1", "ut1", "UTC", "utc", "TAI", "tai", "LOD",
                "lod")
_DATA_CHARACTERS = set("0123456789.-+ \t")


@dataclass(frozen=True)
class Centre:
    """Where the precise products of one analysis centre, of one ``kind`` (``"final"`` or ``"rapid"``), are kept, and
    how the files of a day are called. ``root`` is the address of the archive's folder of products, under which
    ``layout`` puts the files of a day in a folder for each GPS week (``"week"``: ``<root><wwww>/``) or for each year
    (``"year"``: ``<root><yyyy>/``). ``orbit``, ``clock`` and ``erp`` are the names of the files of the orbits (SP3),
    the clocks (RINEX clock) and the Earth rotation parameters, with ``{yyyy}`` and ``{ddd}`` for the year and the day
    of the year; with ``weekly_erp`` the ERP is one file for a GPS week, named by its first day (a Sunday). ``delay``:
    about how long after the end of a day its files are published - after the end of the GPS week of the day, with
    ``weekly``, for products that come a week at a time; ``credit``: whose products they are, as they are to be
    credited."""

    name: str
    kind: str
    root: str
    layout: str
    orbit: str
    clock: str
    erp: str
    delay: timedelta
    credit: str
    weekly_erp: bool = False
    weekly: bool = False

    def _folder(self, day: date) -> str:
        root = self.root if self.root.endswith("/") else self.root + "/"
        return root + (f"{_gps_week(day):04d}/" if self.layout == "week" else f"{day:%Y}/")

    def addresses(self, day: date) -> dict[str, str]:
        """The addresses of the files of ``day``: ``{"sp3": orbits, "clk": clocks, "erp": Earth rotation
        parameters}``; a weekly ERP file is the one of the week of ``day``, in the folder of its first day."""
        erp_day = _sunday(day) if self.weekly_erp else day
        return {"sp3": self._folder(day) + _named(self.orbit, day), "clk": self._folder(day) + _named(self.clock, day),
                "erp": self._folder(erp_day) + _named(self.erp, erp_day)}


def _gps_week(day: date) -> int:
    return (day - _GPS_EPOCH).days // 7


def _sunday(day: date) -> date:
    """The first day of the GPS week of ``day``."""
    return _GPS_EPOCH + timedelta(weeks=_gps_week(day))


def _named(pattern: str, day: date) -> str:
    return pattern.format(yyyy=f"{day:%Y}", ddd=f"{day.timetuple().tm_yday:03d}")


_BKG_PRODUCTS = stations.BKG_ROOT + "IGS/products/"
# in the order they are tried: the final products of ESA, CODE and the IGS, then the rapid ones of the IGS
CENTRES = (
    Centre("ESA", "final", "https://navigation-office.esa.int/products/gnss-products/", "week",
           "ESA0OPSFIN_{yyyy}{ddd}0000_01D_05M_ORB.SP3.gz", "ESA0OPSFIN_{yyyy}{ddd}0000_01D_30S_CLK.CLK.gz",
           "ESA0OPSFIN_{yyyy}{ddd}0000_01D_01D_ERP.ERP.gz", FINAL_DELAY,
           "ESA final orbits, clocks and Earth rotation parameters (ESA0OPSFIN) of the Navigation Support Office of "
           "ESA/ESOC"),
    Centre("CODE", "final", "https://www.aiub.unibe.ch/download/CODE/", "year",
           "COD0OPSFIN_{yyyy}{ddd}0000_01D_05M_ORB.SP3.gz", "COD0OPSFIN_{yyyy}{ddd}0000_01D_30S_CLK.CLK.gz",
           "COD0OPSFIN_{yyyy}{ddd}0000_01D_01D_ERP.ERP.gz", timedelta(days=9),
           "CODE final orbits, clocks and Earth rotation parameters (COD0OPSFIN) of the Center for Orbit "
           "Determination in Europe, Astronomical Institute of the University of Bern"),
    Centre("IGS", "final", _BKG_PRODUCTS, "week", "IGS0OPSFIN_{yyyy}{ddd}0000_01D_15M_ORB.SP3.gz",
           "IGS0OPSFIN_{yyyy}{ddd}0000_01D_30S_CLK.CLK.gz", "IGS0OPSFIN_{yyyy}{ddd}0000_07D_01D_ERP.ERP.gz",
           timedelta(days=17), "IGS final orbits, clocks and Earth rotation parameters (IGS0OPSFIN) of the "
           "International GNSS Service, from BKG", weekly_erp=True, weekly=True),
    Centre("IGS", "rapid", _BKG_PRODUCTS, "week", "IGS0OPSRAP_{yyyy}{ddd}0000_01D_15M_ORB.SP3.gz",
           "IGS0OPSRAP_{yyyy}{ddd}0000_01D_05M_CLK.CLK.gz", "IGS0OPSRAP_{yyyy}{ddd}0000_01D_01D_ERP.ERP.gz",
           RAPID_DELAY, "IGS rapid orbits, clocks and Earth rotation parameters (IGS0OPSRAP) of the International "
           "GNSS Service, from BKG"),
)


@dataclass
class Products:
    """The precise products of a session: the files of the orbits (``sp3``, SP3), of the clocks (``clk``, RINEX
    clock) and of the Earth rotation parameters (``erp``, the IGS format), each a list of paths, plain or packed in
    gzip; the ``centre`` they are of and their ``kind``, ``"final"`` or ``"rapid"``. A ``ValueError`` for a kind of
    products a base is not placed on (ultra-rapid ones are half predicted) and for a list without files."""

    sp3: list[Path]
    clk: list[Path]
    erp: list[Path]
    centre: str
    kind: str

    def __post_init__(self) -> None:
        for role, what in (("sp3", "orbits (SP3)"), ("clk", "clocks (RINEX clock)"),
                           ("erp", "Earth rotation parameters (ERP)")):
            value = getattr(self, role)
            files = [Path(value)] if isinstance(value, (str, os.PathLike)) else [Path(path) for path in value]
            if not files:
                raise ValueError(f"a static PPP needs the precise {what} of the days of the session: none given")
            setattr(self, role, files)
        if self.kind not in KINDS:
            raise ValueError(f"products of the kind {self.kind!r}: a base is placed on final or rapid products, never "
                             "on ultra-rapid ones, which are half predicted")
        if not str(self.centre).strip():
            raise ValueError("the products need the name of the centre they are of")
        self.centre = str(self.centre)


@dataclass
class PppResult:
    """What a static PPP found for a base (:func:`solve_static`): ``position``, the mark under its antenna, a
    :class:`.ppk.BasePosition` of source ``"ppp"`` in ITRF2020 at the epoch of the middle of the session, with RTKLIB's
    standard deviations east, north, up; ``hours`` that the base observed; the ``products`` it was computed with;
    ``solution``, the file of RTKLIB's solution; ``warnings``, what to tell the user (also in the log)."""

    position: ppk.BasePosition
    hours: float
    products: Products
    solution: Path
    warnings: list[str] = field(default_factory=list)


# --- the precise products ------------------------------------------------------------------------------------------


def _now() -> datetime:
    """The time now, in UTC (the tests replace it)."""
    return datetime.now(timezone.utc)


def _session(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """The session as times of GPS time without a zone (what :func:`.rinex.header` gives), the start not after the
    end; a ``ValueError`` otherwise."""
    for when in (start, end):
        if not isinstance(when, datetime) or when.tzinfo is not None:
            raise ValueError("the start and the end of the session are datetimes in GPS time without a time zone, as "
                             "rinex.header gives them")
    if end < start:
        raise ValueError(f"the end of the session ({end}) is before its start ({start})")
    return start, end


def _label(centre: Centre) -> str:
    return f"{centre.name} {centre.kind}"


def _site(centre: Centre) -> str:
    """The server of the archive of ``centre``: the IGS's final and rapid products are both at BKG, and one that cannot
    be reached is not asked again (a folder on the computer is a site of its own)."""
    parts = urllib.parse.urlsplit(centre.root)
    return parts.netloc or centre.root


def _ready(centre: Centre, last_day: date) -> datetime:
    """About when the products of ``centre`` for ``last_day`` are published (UTC): their usual delay after the end of
    the day or, for products that come a GPS week at a time (``weekly``), after the end of the week of the day."""
    end = _sunday(last_day) + timedelta(weeks=1) if centre.weekly else last_day + timedelta(days=1)
    return datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + centre.delay


def _when(centre: Centre, last_day: date) -> str:
    """What to add to the file of ``centre`` that is not there: that it is not published yet, before it usually is
    (:func:`_ready`), else that it should be by now."""
    return " - not published yet" if _now() < _ready(centre, last_day) else " - it should be published by now"


def _words(delay: timedelta) -> str:
    """``4 days``, ``17 hours``."""
    if delay >= timedelta(days=1) and delay % timedelta(days=1) == timedelta(0):
        return f"{delay.days} days"
    return f"{delay.total_seconds() / 3600.0:g} hours"


class _NoGood(Exception):
    """A file of an archive that is no good, not even downloaded again: the message says which and why."""


_PACKED_ERRORS = (EOFError, gzip.BadGzipFile, zlib.error)  # what reading a gzip file cut short or damaged raises


def _layers(path: Path) -> str | None:
    """How ``path`` is packed, as :class:`.rinex.RinexInfo` writes it ("gzip", "zip"...), None when it is not."""
    return "+".join(rinex.compression(path)) or None


def _text(path: Path) -> str:
    """The text of ``path``, plain or packed (gzip, the Unix compress, zip); a ``ValueError`` naming it when it is
    damaged or cut short."""
    layers = rinex.compression(path)
    if not layers:
        return path.read_bytes().decode("ascii", errors="replace")
    if layers == ("gzip",):
        try:
            return gzip.decompress(path.read_bytes()).decode("ascii", errors="replace")
        except _PACKED_ERRORS as exc:
            raise ValueError(f"{path.name} is cut short or damaged ({exc})") from None
    return rinex.plain(path).read_bytes().decode("ascii", errors="replace")


def _first_line(path: Path) -> str:
    with open(path, encoding="ascii", errors="replace") as f:
        return f.readline()


def _erp_rows(text: str) -> list[float]:
    """The MJD of each row of an ERP file of the IGS format (version 2), as RTKLIB-EX reads it (``readerp``): from a
    line with ``VERSION 2`` (or ``version 2``) in it, after the line that names the columns (MJD, Xpole...), the lines
    of numbers alone with five numbers or more; a file of several such parts is read part after part."""
    rows: list[float] = []
    state = 0
    for line in text.splitlines():
        if any(word in line for word in _ERP_VERSION):
            state = 1
        elif state == 1:
            if any(word in line for word in _ERP_COLUMNS):
                state = 2
        elif state == 2:
            numbers = line.split()
            if len(numbers) >= 5 and set(line.strip()) <= _DATA_CHARACTERS and any(c.isdigit() for c in line):
                try:
                    rows.append(float(numbers[0]))
                except ValueError:
                    continue
    return rows


def _check(path: Path, role: str, name: str) -> None:
    """A ``ValueError`` naming ``name`` unless the plain file ``path`` is a file of the ``role`` (``"sp3"``, ``"clk"``,
    ``"erp"``) that RTKLIB reads: an SP3-c or SP3-d file of orbits, a RINEX clock file, an ERP file of the IGS format
    with rows. A page of HTML that a network gave, or the file of another product, is not."""
    if role == "sp3":
        if not _first_line(path).startswith(("#c", "#d")):
            raise ValueError(f"{name} is not a file of precise orbits: an SP3-c or SP3-d file starts with #c or #d")
    elif role == "clk":
        line = _first_line(path)
        if "RINEX VERSION / TYPE" not in line[60:] or "C" not in (line[20:21], line[21:22]):
            raise ValueError(f"{name} is not a RINEX clock file of precise clocks")
    elif not _erp_rows(path.read_text(encoding="ascii", errors="replace")):
        raise ValueError(f"{name} is not a file of Earth rotation parameters of the IGS format (version 2) with rows")


_CLOCK_RECORDS = ("AS ", "AR ", "CR ", "DR ", "MS ")  # the records of a RINEX clock file, which all have an epoch


def _open_text(path: Path):
    """``path`` opened as text to read, plain or packed: a gzip file as it is read, a file under other layers (the Unix
    compress, zip) from its decompressed copy (:func:`.rinex.plain`)."""
    layers = rinex.compression(path)
    if layers == ("gzip",):
        return gzip.open(path, "rt", encoding="ascii", errors="replace")
    return open(rinex.plain(path) if layers else path, encoding="ascii", errors="replace")


def _sp3_span(path: Path) -> tuple[datetime, datetime]:
    """The time the orbits of the SP3 file ``path`` (plain or packed) cover, by its header: from its first epoch, as
    many intervals as it has epochs. A ``ValueError`` naming the file when the header does not say."""
    try:
        with _open_text(path) as f:
            first, second = f.readline(), f.readline()
    except _PACKED_ERRORS as exc:
        raise ValueError(f"{path.name} is cut short or damaged ({exc})") from None
    try:
        if not first.startswith(("#c", "#d")):
            raise ValueError
        year, month, day, hour, minute = (int(v) for v in first[3:20].split())
        start = datetime(year, month, day, hour, minute) + timedelta(seconds=float(first[20:31]))
        epochs, interval = int(first[32:39]), float(second[24:38])
        if epochs <= 0 or not interval > 0.0:
            raise ValueError
    except (ValueError, OverflowError):
        raise ValueError(f"{path.name}: its SP3 header does not say when its orbits start, how many epochs it has and "
                         "how far apart they are") from None
    return start, start + timedelta(seconds=epochs * interval)


def _clock_offset(path: Path) -> int:
    """How much further right than in RINEX clock 3.00 the epoch of a record of the clock file ``path`` is: 5
    characters from version 3.04 on, whose names of satellites and stations are longer."""
    with _open_text(path) as f:
        head = f.readline()
    try:
        return 5 if float(head[:9]) >= 3.04 else 0
    except ValueError:
        return 0


def _clock_time(line: str, offset: int) -> datetime | None:
    """The epoch of a record of a RINEX clock file (``AS``, ``AR``...), None for any other line."""
    if not line.startswith(_CLOCK_RECORDS):
        return None
    fields = line[8 + offset:34 + offset].split()
    try:
        year, month, day, hour, minute = (int(v) for v in fields[:5])
        return datetime(year, month, day, hour, minute) + timedelta(seconds=float(fields[5]))
    except (ValueError, IndexError, OverflowError):
        return None


def _clock_epochs(path: Path, offset: int):
    """The epochs of the records of the RINEX clock file ``path`` (plain or packed), in the order of the file."""
    with _open_text(path) as f:
        for line in f:
            if "END OF HEADER" in line[60:]:
                break
        for line in f:
            when = _clock_time(line, offset)
            if when is not None:
                yield when


def _last_clock_time(path: Path, offset: int) -> datetime | None:
    """The epoch of the last record of the plain RINEX clock file ``path``, read from its last 64 KB; None when they
    have none."""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - 65536))
        tail = f.read().decode("ascii", errors="replace")
    for line in reversed(tail.splitlines()):
        when = _clock_time(line, offset)
        if when is not None:
            return when
    return None


def _clock_span(path: Path) -> tuple[datetime, datetime]:
    """The time the clocks of the RINEX clock file ``path`` (plain or packed) cover: from its first epoch to its last,
    and the interval of its epochs (the step between its first two) after it. The last epoch is read from the end of a
    plain file, through a gzip file. A ``ValueError`` naming the file when it is damaged or has no records."""
    gzipped = rinex.compression(path) == ("gzip",)
    try:
        offset = _clock_offset(path)
        epochs = _clock_epochs(path, offset)
        try:
            first = next(epochs, None)
            if first is None:
                raise ValueError(f"{path.name} has no records of precise clocks")
            second = next((when for when in epochs if when != first), None)
            last = None if gzipped else _last_clock_time(rinex.plain(path), offset)
            if last is None:
                last = second or first
                for when in epochs:
                    last = when
        finally:
            epochs.close()
    except _PACKED_ERRORS as exc:
        raise ValueError(f"{path.name} is cut short or damaged ({exc})") from None
    step = abs((second - first).total_seconds()) if second is not None else 0.0
    return first, last + timedelta(seconds=step)


def _file_name(url: str) -> str:
    """The name of the file at ``url``, as the archive gives it."""
    return urllib.parse.unquote(url.rsplit("/", 1)[-1])


def _unpacked(folder: Path, url: str) -> Path:
    """Where the file at ``url`` goes decompressed: in ``folder``, its name without .gz."""
    name = _file_name(url)
    return folder / (name[:-3] if name.endswith(".gz") else name)


def _fetched(centre: Centre, role: str, url: str, folder: Path, progress) -> Path:
    """The file of ``role`` at ``url`` decompressed into ``folder`` (:func:`_unpacked`) and checked; the file as the
    archive gives it is kept in ``products/<centre>`` of the GNSS folder, and a copy there is used again. A copy, or a
    download, that is no good is removed and downloaded again once; then a :class:`_NoGood`. The errors of the download
    (:class:`.download.NotPublished` among them) go to the caller."""
    name = _file_name(url)
    cache = download.gnss_dir() / "products" / centre.name / name
    target = _unpacked(folder, url)
    problem: ValueError | None = None
    for _attempt in range(2):
        progress.check()
        if not cache.is_file():
            download.fetch(url, cache, progress)  # beside the file, then renamed: never half a file
        folder.mkdir(parents=True, exist_ok=True)
        try:
            rinex.gunzip(cache, target, name)  # through a .part beside the target: a target is whole or as it was
        except ValueError as exc:  # cut short or damaged
            problem = exc
            cache.unlink(missing_ok=True)
            continue
        try:
            _check(target, role, name)
            return target
        except ValueError as exc:  # whole, but not what it should be: nor is the target it became
            problem = exc
            cache.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
    raise _NoGood(f"{name} ({url}) is no good, not even downloaded again: {problem}")


def _of_centre(centre: Centre, days: list[date], folder: Path, progress) -> Products:
    """The products of ``centre`` for ``days``, decompressed into ``folder``: the files of the last day first - the
    newest are the ones an archive may not have yet - then the others, the ERP of a week once. Whatever went wrong
    goes to the caller, and what the call decompressed into ``folder`` is removed."""
    wanted: list[tuple[str, date, str]] = []
    for day in reversed(days):
        for role, url in centre.addresses(day).items():
            if all(url != other for _, _, other in wanted):
                wanted.append((role, day, url))
    there = {url for _, _, url in wanted if _unpacked(folder, url).exists()}  # from an earlier call: they stay
    got: dict[tuple[str, str], Path] = {}
    try:
        for n, (role, day, url) in enumerate(wanted):
            got[(role, url)] = _fetched(centre, role, url, folder, progress.sub(n / len(wanted), 1 / len(wanted)))
    except BaseException:
        for (_role, url), path in got.items():
            if url not in there:
                path.unlink(missing_ok=True)
        raise
    in_order = sorted(wanted, key=lambda entry: entry[1])
    return Products([got[(role, url)] for role, _, url in in_order if role == "sp3"],
                    [got[(role, url)] for role, _, url in in_order if role == "clk"],
                    [got[(role, url)] for role, _, url in in_order if role == "erp"], centre.name, centre.kind)


def _nothing(start: datetime, end: datetime, days: list[date], why: list[str]) -> str:
    """The message when no centre has the products of the session: what is missing where, and when the products
    come."""
    until = f"{end:%H:%M}" if start.date() == end.date() else f"{end:%Y-%m-%d %H:%M}"
    lines = [f"No precise orbits and clocks can be had for the session from {start:%Y-%m-%d %H:%M} to {until} (GPS "
             "time):"]
    lines += [f"  {text}" for text in why]
    after = datetime(days[-1].year, days[-1].month, days[-1].day, tzinfo=timezone.utc) + timedelta(days=1)
    rapid, final, now = after + RAPID_DELAY, after + FINAL_DELAY, _now()
    by_hand = "try again later, or give the products by hand (Products)"
    if now < rapid:
        lines.append(f"The rapid products of the IGS come about {_words(RAPID_DELAY)} after the end of the last day of "
                     f"the session (from about {rapid:%Y-%m-%d %H:%M} UTC), the final ones of ESA about "
                     f"{_words(FINAL_DELAY)} after it (from about {final:%Y-%m-%d}).")
    elif now < final:
        lines.append(f"The rapid products of the IGS should already be published - they come about "
                     f"{_words(RAPID_DELAY)} after the end of the last day of the session: {by_hand}. The final ones "
                     f"of ESA come about {_words(FINAL_DELAY)} after it (from about {final:%Y-%m-%d}).")
    else:
        lines.append(f"The final products of ESA and the rapid ones of the IGS should already be published - ESA's "
                     f"come about {_words(FINAL_DELAY)}, the IGS's rapid ones about {_words(RAPID_DELAY)} after the "
                     f"end of the last day of the session: {by_hand}.")
    lines.append("The ultra-rapid products, half predicted, are never used for a base.")
    if days[0] < LONG_NAMES:
        lines.append("The program asks for the products by the names the IGS and its centres give them from GPS week "
                     "2238 (27 November 2022) on: for an older session download the orbits, the clocks and the ERP by "
                     "hand and give them as Products.")
    return "\n".join(lines)


def products(start: datetime, end: datetime, folder: str | Path, progress=None) -> Products:
    """The precise products of a session from ``start`` to ``end`` (GPS time without a zone: its first and last
    epochs), decompressed into ``folder`` with their extensions (``.SP3``, ``.CLK``, ``.ERP``: RTKLIB tells the files
    by them): the orbits, the clocks and the Earth rotation parameters of every day of the session, all of one centre
    and of one kind, the first of :data:`CENTRES` that has them all - the final ones of ESA, of CODE, of the IGS, then
    the rapid ones of the IGS; never the ultra-rapid ones. A centre that has only some of the days is passed over as a
    whole; so is one whose archive cannot be reached (and the archive is not asked again for another kind of
    products) or that gives a file that is no good, with a warning in the log.

    The files are downloaded one at a time (:func:`.download.fetch`: HTTPS, three attempts) and kept as the archives
    name them in ``products/<centre>`` of the GNSS folder, where a later call finds them; each is checked - it is
    whole, and it is what it should be - and one that is not is downloaded again once. A ``ValueError`` when no centre
    has the products of the session: it says what is missing where, each file not published yet or due by now by the
    usual delay of its centre (for the final products of the IGS, which come a GPS week at a time, from the end of the
    week), and when the products come - the final ones of ESA about 4 days after the end of the last day of the
    session, the rapid ones of the IGS about 17 hours after it - or, once that time is gone, that they should already
    be published, and to try again later or to give them by hand."""
    progress = as_progress(progress)
    start, end = _session(start, end)
    progress.check()
    days = [start.date() + timedelta(days=n) for n in range((end.date() - start.date()).days + 1)]
    folder = Path(folder)
    why: list[str] = []
    down: dict[str, str] = {}
    for n, centre in enumerate(CENTRES):
        label, site = _label(centre), _site(centre)
        if site in down:
            why.append(f"{label}: not asked, its archive could not be reached")
            continue
        progress(n / len(CENTRES), f"Precise products: {label}")
        try:
            found = _of_centre(centre, days, folder, progress.sub(n / len(CENTRES), 1 / len(CENTRES)))
        except download.NotPublished as exc:
            name = exc.url.rsplit("/", 1)[-1]
            text = f"{label}: {name} is not in {exc.url[:len(exc.url) - len(name)]}{_when(centre, days[-1])}"
            log.info("%s", text)
            why.append(text)
            continue
        except download.DownloadError as exc:
            down[site] = str(exc)
            why.append(f"{label}: could not be downloaded: {exc}")
            log.warning("%s products: could not be downloaded, the next centre is tried: %s", label, exc)
            continue
        except _NoGood as exc:
            why.append(f"{label}: {exc}")
            log.warning("%s products: %s; the next centre is tried", label, exc)
            continue
        log.info("%s products of %d days", label, len(days))
        progress(1.0, f"Precise products: {label}, {len(days)} days")
        return found
    raise ValueError(_nothing(start, end, days, why))


# --- the options of RTKLIB -------------------------------------------------------------------------------------


def _eop_problem(path: str) -> str | None:
    """Why ``path`` cannot be the ERP file of RTKLIB's options file, or None when it can. RTKLIB reads only a file
    called ``.erp`` or ``.ERP`` as ERP, reads from a "#" to the end of the line as a comment, replaces the keywords
    of a time (``%Y``, ``%m``, ``%d``...) in the path of this file, reads its options file by lines and keeps a path in
    a buffer of 1024 bytes."""
    if not path.endswith((".erp", ".ERP")):
        return (f"the ERP file {path} should be called .erp (or .ERP): RTKLIB reads no other file as Earth rotation "
                "parameters")
    if "#" in path:
        return (f"the path of the ERP file, {path}, has a \"#\" in it, which RTKLIB reads in its options file as the "
                "start of a comment: move the project, where the PPP works, to a folder without a \"#\" in its path")
    if "%" in path:
        return (f"the path of the ERP file, {path}, has a \"%\" in it, which RTKLIB reads as the start of a time "
                "(%Y, %m, %d...) and replaces: move the project, where the PPP works, to a folder without a \"%\" in "
                "its path")
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        return (f"the path of the ERP file, {path!r}, has a control character in it (a line end, a tab...), and "
                "RTKLIB reads its options file by lines")
    size = len(os.fsencode(path))
    if size >= ppk.MAX_PATH_BYTES:
        return (f"the path of the ERP file is {size} bytes long, and RTKLIB takes paths shorter than "
                f"{ppk.MAX_PATH_BYTES} bytes: move the project, where the PPP works, to a folder with a shorter path")
    return None


def config(antenna_file: str, eop_files: list[str], hours: float) -> str:
    """The options file of RTKLIB for the static PPP of a base that observed for ``hours`` (the text of rnx2rtkp's -k
    file): the receiver standing still (``ppp-static``) solved forward into one position for the session
    (``out-solstatic=single``), on L1 and L2 with the ionosphere-free combination (``dual-freq``), the zenith delay of
    the troposphere estimated - and its gradients from :data:`GRADIENTS_H` hours on (``est-ztdgrad``) - the solid
    earth and pole tides (``pos1-tidecorr=5``: the ocean loading would need a BLQ file), the satellite and receiver
    antenna models, phase wind-up, eclipsing satellites left out and RAIM (``pos1-posopt1..5``), GPS and Galileo
    (``pos1-navsys=9``), precise orbits and clocks, the ambiguities left float (``pos2-armode=off``: RTKLIB-EX fixes
    none in PPP). The antenna of the receiver and its height above the mark are those of its RINEX header
    (``ant1-anttype=*``): the position is of the mark. ``antenna_file`` is the ANTEX file of the antennas of the
    satellites and of the receiver (``.atx``), ``eop_files`` the ERP file of the session, as a list of one: RTKLIB
    reads one ERP file (join those of several days with :func:`join_erp`). The solution in latitude, longitude and
    ellipsoidal height, timed in GPS weeks and seconds, as :func:`.ppk.read_pos` reads it.

    A ``ValueError`` for a path RTKLIB would read otherwise than written, for no ERP file or several, and for hours
    that are not a number above 0."""
    antenna = os.fspath(antenna_file)
    problem = ppk.path_problem(antenna)
    if problem:
        raise ValueError(problem)
    eops = [os.fspath(eop_files)] if isinstance(eop_files, (str, os.PathLike)) else [os.fspath(p) for p in eop_files]
    if not eops:
        raise ValueError("a static PPP needs the Earth rotation parameters (ERP) of the session: no ERP file given")
    if len(eops) > 1:
        raise ValueError(f"RTKLIB reads one ERP file (file-eopfile), not {len(eops)}: join the files of the days into "
                         "one first, in the order of the days (join_erp)")
    problem = _eop_problem(eops[0])
    if problem:
        raise ValueError(problem)
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        raise ValueError(f"the hours of the session are a number above 0, not {hours!r}") from None
    if not (math.isfinite(hours) and hours > 0.0):
        raise ValueError(f"the hours of the session are a number above 0, not {hours!r}")
    options = [("pos1-posmode", "ppp-static"), ("pos1-frequency", "l1+l2"), ("pos1-soltype", "forward"),
               ("pos1-elmask", "15"), ("pos1-ionoopt", "dual-freq"),
               ("pos1-tropopt", "est-ztdgrad" if hours >= GRADIENTS_H else "est-ztd"), ("pos1-tidecorr", "5"),
               ("pos1-posopt1", "on"), ("pos1-posopt2", "on"), ("pos1-posopt3", "on"), ("pos1-posopt4", "on"),
               ("pos1-posopt5", "on"), ("pos1-navsys", "9"), ("pos1-sateph", "precise"), ("pos2-armode", "off"),
               ("ant1-anttype", "*"), ("file-satantfile", antenna), ("file-rcvantfile", antenna),
               ("file-eopfile", eops[0]), ("out-solformat", "llh"), ("out-outhead", "on"), ("out-timesys", "gpst"),
               ("out-timeform", "tow"), ("out-height", "ellipsoidal"), ("out-solstatic", "single")]
    return "# RTKLIB options of a static PPP (pyarchinit-photogrammetry)\n" + "".join(
        f"{name:<18}={value}\n" for name, value in options)


def join_erp(paths: Sequence[str | Path], target: str | Path) -> Path:
    """The ERP files ``paths`` (plain or packed) written one after the other into ``target``, in the order of their
    first day whatever the order given; returns ``target``. RTKLIB reads one ERP file (``file-eopfile``): RTKLIB-EX
    2.5.1 reads a file of several, each starting again with its ``VERSION 2`` line, part after part, and looks the rows
    up by a binary search, so they go in the order of time. ``target`` is written beside itself and renamed when it is
    whole. A ``ValueError`` naming a file that is not an ERP file of the IGS format (version 2) with rows, and then no
    ``target`` is written."""
    files = [Path(path) for path in paths]
    if not files:
        raise ValueError("no ERP file to join")
    texts = []
    for path in files:
        text = _text(path)
        rows = _erp_rows(text)
        if not rows:
            raise ValueError(f"{path.name} is not a file of Earth rotation parameters of the IGS format (version 2) "
                             "with rows")
        texts.append((rows[0], text if text.endswith("\n") else text + "\n"))
    target = Path(target)
    work = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        with open(work, "w", encoding="ascii", errors="replace", newline="") as out:
            for _first, text in sorted(texts, key=lambda entry: entry[0]):
                out.write(text)
        os.replace(work, target)
    finally:
        work.unlink(missing_ok=True)
    return target


# --- the base by static PPP ------------------------------------------------------------------------------------


_NO_ANTEX = ("a static PPP needs the antenna calibrations of the IGS (igs20.atx): the precise orbits are of the "
             "centres of mass of the satellites, and the phase centres of their antennas are up to metres from them - "
             "without the offsets every position is decimetres off. Give atx=\"auto\" (igs20.atx, downloaded once into "
             "the folder of the program) or an ANTEX file")


def _antenna_file(atx, progress) -> Path:
    """The ANTEX file of the run: with "auto" the copy of igs20.atx of the program (:func:`.atx.atx_file`: downloaded
    when it has none, or an old one), else the file given, which must be an ANTEX file."""
    if isinstance(atx, str) and atx == "auto":
        try:
            return Path(antex.atx_file(progress=progress))
        except (download.DownloadError, ValueError, OSError) as exc:
            raise ValueError(f"a static PPP needs the antenna calibrations of the IGS ({antex.FILE_NAME}), and they "
                             f"could not be had: {exc}") from exc
    path = Path(atx)
    if not path.is_file():
        raise ValueError(f"the antenna calibrations {path} are not a file: give an ANTEX file (.atx), such as "
                         "igs20.atx, or \"auto\"")
    antex.validate(path)
    return path


def _hours(path: Path, info: rinex.RinexInfo, span: tuple[datetime, datetime],
           holes: list[tuple[datetime, datetime]]) -> float:
    """How long the base observed: from its first epoch to the end of the interval of its last (n epochs a second apart
    are n seconds), less the holes between the files it was given in (each hole from the epoch before it to the epoch
    after it)."""
    step = rinex.epoch_interval(path, info) or 0.0
    seconds = (span[1] - span[0]).total_seconds() + step
    seconds -= sum(max(0.0, (b - a).total_seconds() - step) for a, b in holes)
    return round(seconds) / 3600.0  # epochs are a few ms off a second


def _to_the_second(when: datetime) -> str:
    """``when`` written to the nearest second (the epochs of a receiver can be a few milliseconds off one)."""
    return f"{(when + timedelta(microseconds=500000)).replace(microsecond=0):%Y-%m-%d %H:%M:%S}"


def _middle(start: datetime, end: datetime) -> float:
    """The decimal year of the middle of the time from ``start`` to ``end`` (GPS time, taken back to UTC)."""
    middle = start + (end - start) / 2
    return frames.decimal_year(middle - timedelta(seconds=rinex.gps_minus_utc(middle)))


def _duration(seconds: float) -> str:
    """``45 seconds``, ``90 minutes`` (up to 2 hours), ``3.5 hours``."""
    if seconds < 90.0:
        return f"{round(seconds)} seconds"
    if seconds < 7200.0:
        return f"{round(seconds / 60.0)} minutes"
    return f"{round(seconds / 3600.0, 1):g} hours"


def _between(start: datetime, end: datetime) -> str:
    """``2005-04-02 00:00:00 to 2005-04-02 00:59:30 GPS time``, to the nearest second."""
    return f"{_to_the_second(start)} to {_to_the_second(end)} GPS time"


def _uncovered(spans, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """The parts of the time from ``start`` to ``end`` that none of the ``spans`` (from, to) covers."""
    missing: list[tuple[datetime, datetime]] = []
    reached = start
    for a, b in sorted(spans):
        if a > reached:
            missing.append((reached, min(a, end)))
        reached = max(reached, b)
        if reached >= end:
            break
    if reached < end:
        missing.append((reached, end))
    return [(a, b) for a, b in missing if b > a]


def _coverage_problem(found: Products, name: str, start: datetime, end: datetime) -> str | None:
    """Why the orbits or the clocks of ``found`` leave part of the session of the base ``name`` from ``start`` to
    ``end`` (its first and last epoch) without products - each file and the time it covers, the time left out and how
    long it is - or None when they cover all of it: RTKLIB computes nothing where it has no orbits or clocks, and the
    hours of the session would claim that time."""
    for what, files, span_of in (("orbits", found.sp3, _sp3_span), ("clocks", found.clk, _clock_span)):
        spans = [(span_of(path), path) for path in files]
        missing = _uncovered([span for span, _ in spans], start, end)
        if missing:
            seconds = sum((b - a).total_seconds() for a, b in missing)
            covers = "; ".join(f"{path.name} covers {_between(a, b)}" for (a, b), path in spans)
            left = ", ".join(_between(a, b) for a, b in missing)
            return (f"the precise {what} do not cover the session of {_YOUR_BASE} {name}, from "
                    f"{_between(start, end)}: {covers} - {_duration(seconds)} of the session ({left}) would have no "
                    f"{what}, and RTKLIB computes nothing there. Give the {what} of every day of the session")
    return None


def _session_notes(name: str, hours: float) -> list[str]:
    """What to tell about a session of ``hours``: under :data:`SHORT_H` a PPP is 10-20 cm off, under
    :data:`STATION_BETTER_H` a static baseline from a station nearby is better."""
    duration = _duration(hours * 3600.0)
    notes = []
    if hours < SHORT_H:
        notes.append(f"{_YOUR_BASE} {name} observed for {duration}: a static PPP of less than {SHORT_H:g} hours is "
                     "10-20 cm off at best, and every position of the survey with it")
    if hours < STATION_BETTER_H:
        notes.append(f"{_YOUR_BASE} {name} observed for {duration}: under {STATION_BETTER_H:g} hours a static "
                     "baseline from a permanent station nearby places it better than a PPP, to a few centimetres or "
                     "less")
    return notes


def _citation(found: Products, antennas: Path) -> str:
    """Whose products and antenna calibrations the position was computed with, as they are to be credited."""
    credit = next((centre.credit for centre in CENTRES if (centre.name, centre.kind) == (found.centre, found.kind)),
                  f"{found.kind} orbits, clocks and Earth rotation parameters of {found.centre}")
    of_igs = " of the IGS" if antennas.name == antex.FILE_NAME else ""
    return f"{credit}; antenna calibrations: {antennas.name}{of_igs}"


def _product(source: Path, role: str):
    """What writes the product ``source`` (``"sp3"`` or ``"clk"``) among the copies of the run, and checks the copy
    (:func:`_check`)."""
    copy = ppk.copier(source, _layers(source))

    def write(target: Path) -> None:
        copy(target)
        _check(target, role, source.name)

    return write


def _run(own: Path, info: rinex.RinexInfo, found: Products, antennas: Path, folder: Path,
         hours: float, joined: bool = False) -> tuple[Path, str]:
    """RTKLIB's static PPP of the base ``own`` (whose header is ``info``; ``joined``: the program joined it from pieces,
    and it is moved among the inputs rather than copied) in the work folder ``folder``: the solution file and RTKLIB's
    last message. As :func:`.ppk.solve` does (:func:`.ppk.new_run`, :func:`.ppk.run_rtklib`), RTKLIB
    works on plain copies of the inputs under names of their own in the subfolder ``INPUTS_FOLDER`` - the
    observations, the orbits (``.sp3``) and the clocks (``.clk``), which it tells by their extensions, the ERP of all
    the days in one file and the antenna file - removed after the run, and writes its options and its solution under
    a name of its own; a run that gives no solution leaves nothing."""
    files = ppk.new_run(folder, (own, antennas, *found.sp3, *found.clk, *found.erp), "PPP")
    copies = files.copies
    observations, rotation, antenna = copies / "base.obs", copies / ERP_COPY, copies / ppk.ANTENNA_COPY
    orbits = [copies / f"orbit{i}.sp3" for i in range(1, len(found.sp3) + 1)]
    clocks = [copies / f"clock{i}.clk" for i in range(1, len(found.clk) + 1)]
    inputs = [(observations, ppk.mover(own) if joined else ppk.copier(own, info.compressed)),
              *zip(orbits, (_product(source, "sp3") for source in found.sp3)),
              *zip(clocks, (_product(source, "clk") for source in found.clk))]
    others = [(rotation, lambda target: join_erp(found.erp, target)),
              (antenna, lambda target: shutil.copyfile(antennas, target))]
    # what RTKLIB would read otherwise than written is refused by config, before anything is written
    solution, message = ppk.run_rtklib(
        files, lambda: config(str(antenna), [str(rotation)], hours), inputs, others,
        nothing=lambda said: f"RTKLIB computed no PPP solution: {said or 'it wrote no positions'}")
    return solution.path, message


def solve_static(obs: Path | Sequence[Path], folder: Path, products: Products | None = None, atx="auto",
                 progress=None) -> PppResult:
    """The mark of the user's own base - a receiver left for some hours on a mark nobody surveyed - by static PPP:
    RTKLIB solves its RINEX observations ``obs`` (one file, or several that :func:`.rinex.join` joins, in any order;
    any of them may be packed) on its own, as a receiver standing still for the whole session, with the precise
    ``products`` of the days of the session, or, with None, those that :func:`products` downloads into the
    ``products`` folder of ``folder``; RTKLIB works in the ``ppp`` folder of ``folder``, where its solution stays. The
    options are those of :func:`config`; the ERP of several days is one file (:func:`join_erp`).

    ``atx`` is the ANTEX file of the antennas: "auto" for igs20.atx, which the program downloads once into its folder
    (:func:`.atx.atx_file`), or the path of an ANTEX file; never None - the precise orbits are of the centres of mass
    of the satellites, whose antennas are up to metres away, and without their offsets a PPP is decimetres off. RTKLIB
    finds the antenna of the base, named in the header of its RINEX file, in the same file, as :func:`.atx.lookup`
    does; an antenna that is not in it, or not with its radome, is a warning (in a PPP the whole offset of its phase
    centre goes into the height), one for which RTKLIB's search finds a satellite's antenna a ``ValueError``.

    The result (:class:`PppResult`) has the mark under the antenna - RTKLIB takes the antenna height of the header off
    - as a :class:`.ppk.BasePosition` of source ``"ppp"``, frame ``"ITRF2020"`` and the epoch of the middle of the
    session, with RTKLIB's standard deviations east, north, up and in ``detail`` the base, the centre and kind of the
    products and their files, whom to credit (``citation``), the hours, the session (first and last epoch, GPS time),
    the antenna, its height and calibration, RTKLIB's quality and the name of the solution file. The warnings - a
    session under :data:`SHORT_H` hours (10-20 cm) or under :data:`STATION_BETTER_H` (a station nearby is better),
    rapid products, the antenna of the base without its calibration, a session before GPS week 2238 (its products
    are of IGb14, made for igs14.atx, while the position is computed with the antenna file given and called
    ITRF2020), an error RTKLIB reported with its solution - are in the result and in the log.

    A ``ValueError`` for no antenna file, a file that is not one of observations or has no epochs, products that
    cannot be had (:func:`products` says why), orbits or clocks that leave part of the session without products (the
    SP3 files by their headers, the clock files by their epochs: every file and the time it covers, and the time left
    out, are named), an input in RTKLIB's work folder, a path RTKLIB would read otherwise, a solution without
    positions and one whose quality is not 6 (RTKLIB's PPP: the solution is kept and named, with what RTKLIB said); a
    ``RuntimeError`` when RTKLIB stopped."""
    progress = as_progress(progress)
    if atx is None:
        raise ValueError(_NO_ANTEX)
    pieces = [Path(obs)] if isinstance(obs, (str, os.PathLike)) else [Path(path) for path in obs]
    if not pieces:
        raise ValueError(f"no file given for {_YOUR_BASE}")
    folder = Path(folder)
    progress(0.0, f"{_YOUR_BASE.capitalize()} by static PPP")
    if len(pieces) == 1:
        return _solve_static(pieces, pieces[0], [], folder, products, atx, progress)
    # the base in one file, beside the rest, moved among the inputs of the run (no second copy)
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".base-", dir=folder) as scratch:
        joined = Path(scratch) / "base.obs"
        _info, holes = rinex.join(pieces, joined)
        return _solve_static(pieces, joined, holes, folder, products, atx, progress)


def _solve_static(pieces: list[Path], own: Path, holes: list[tuple[datetime, datetime]], folder: Path,
                  given: Products | None, atx, progress) -> PppResult:
    """:func:`solve_static` with the files of the base as the user gave them (``pieces``), the one file they make
    (``own``) and the holes between them."""
    info = rinex.header(own)
    if info.kind != "obs":
        raise ValueError(f"{_YOUR_BASE} {pieces[0].name} holds navigation messages, not observations")
    span = rinex.time_span(own)
    if span is None:
        raise ValueError(f"{_YOUR_BASE} {pieces[0].name} has no observation epochs")
    start, end = span
    name = ppk.base_name(pieces[0], info)
    hours = _hours(own, info, span, holes)
    antennas = _antenna_file(atx, progress.sub(0.0, 0.2))
    kind = ppk.antenna_type(info)
    satellite = ppk.satellite_entry(antennas, kind)
    if satellite:
        raise ValueError(f"for the antenna {kind} of {_YOUR_BASE} {name} RTKLIB's search in {antennas.name} finds "
                         f"{' '.join(satellite.split())}, the antenna of a satellite, whose offsets it would apply to "
                         f"the receiver: write the type of the antenna in the RINEX header of {_YOUR_BASE} (ANT # / "
                         "TYPE)")
    calibration = ppk.antenna_calibration(antennas, kind)
    found = given if given is not None else products(start, end, folder / PRODUCTS_FOLDER, progress.sub(0.2, 0.6))
    problem = _coverage_problem(found, name, start, end)
    if problem:
        raise ValueError(problem)
    warnings: list[str] = []
    note = ppk.antenna_warning(name, info, calibration, role="ppp")  # in a PPP it all weighs on the height
    if note:
        warnings.append(note)
    warnings += _session_notes(name, hours)
    if found.kind == "rapid":
        warnings.append(f"the precise products are rapid ones ({found.centre}): the final ones are a little better - "
                        f"ESA's come about {_words(FINAL_DELAY)} after the session - and {_YOUR_BASE} can be "
                        "computed again with them then")
    if start.date() < LONG_NAMES:
        warnings.append(f"{_YOUR_BASE} {name} observed before GPS week 2238 (27 November 2022), when the precise "
                        "products were of IGb14 and made for the antenna calibrations of igs14.atx (save those of the "
                        "third reprocessing of the IGS, repro3), while the position is computed with "
                        f"{antennas.name} and called ITRF2020: the two frames are millimetres apart, but the antenna "
                        "offsets of the two files differ by up to centimetres, and the mark can be that much off")
    progress(0.8, f"RTKLIB: the static PPP of {name}")
    path, message = _run(own, info, found, antennas, folder / WORK_FOLDER, hours, joined=len(pieces) > 1)
    if message.lower().startswith("error"):
        warnings.append(f"RTKLIB: {message}")
    (lat, lon, h), q, sd = ppk.static_position(path)
    if q != Q_PPP:
        said = f" RTKLIB said: {message}." if message else ""
        raise ValueError(f"RTKLIB made no PPP solution of {_YOUR_BASE} {name}: its solution is "
                         f"{ppk.QUALITY.get(q, 'of no known quality')} (Q={q}), not ppp (Q={Q_PPP}) - the precise "
                         "orbits and clocks may not fit the observations, or these are not on two frequencies."
                         f"{said} The solution is kept in {path}")
    for text in warnings:
        log.warning("%s", text)
    detail = {"base": name, "centre": found.centre, "kind": found.kind,
              "products": [p.name for p in (*found.sp3, *found.clk, *found.erp)],
              "citation": _citation(found, antennas), "hours": hours,
              "session": [_to_the_second(start), _to_the_second(end)],
              "antenna": kind, "antenna_delta_hen": None if info.antenna_delta_hen is None else
              list(info.antenna_delta_hen), "antenna_calibration": calibration, "q": q, "quality": ppk.QUALITY[q],
              "solution": path.name}
    position = ppk.BasePosition(lat, lon, h, "ppp", frame=FRAME, epoch=_middle(start, end), sd=sd, detail=detail)
    progress(1.0, f"{name}: static PPP, {hours:g} hours, {found.centre} {found.kind} products")
    return PppResult(position=position, hours=hours, products=found, solution=path, warnings=warnings)
