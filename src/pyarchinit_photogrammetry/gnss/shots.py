"""Shots of a camera as data, whatever records them: the lines of a DJI ``Timestamp.MRK``, the external events of a
receiver (RINEX epoch flag 5, u-blox TIM-TM2, an ``_events.pos``), the camera messages of an autopilot log (ArduPilot
CAM/TRIG, PX4 camera_capture/camera_trigger). A shot has its time in GPS time and, when the source gives them, the
position of the antenna, its accuracy and the attitude of the drone; a set of shots says what it was read from and what
was left out of it."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from ..core.camera import Camera
from ..core.chunk import IMAGE_EXTENSIONS, Chunk
from . import dji

# what a set of shots was read from: a DJI MRK, the external events of a RINEX file, the TIM-TM2 messages of a u-blox
# log, the CAM or TRIG messages of an ArduPilot log, the camera_capture or camera_trigger topics of a PX4 log, an
# _events.pos of RTKLIB or Emlid Studio
KINDS = ("mrk", "rinex_events", "ubx_tim_tm2", "ardupilot_cam", "ardupilot_trig", "px4_capture", "px4_trigger",
         "events_pos")
# the kinds of height of an antenna position: above the ellipsoid, above the sea level (AMSL, as autopilot logs give
# it: never to be taken for an ellipsoidal one)
HEIGHTS = ("ellipsoidal", "msl")
STATUSES = ("fix", "float", "single", "none")


@dataclass
class Shot:
    """One shot of the camera.

    ``index`` is the number of the image when the source gives it (the MRK, the event counter of TIM-TM2), else the
    place of the shot in its set (from 1). ``gps_time`` is in seconds of GPS time since 6 January 1980, as
    ``dji.MrkShot.gps_time`` and the times of a PPK solution. ``antenna`` is ``(lat, lon, h)`` of the antenna in
    degrees and metres, with ``height`` one of :data:`HEIGHTS`; ``std`` the accuracy ``(north, east, up)`` in metres;
    ``status`` one of :data:`STATUSES`; ``lever_ned_mm`` the lever arm from the antenna to the camera of this shot
    (north, east, down, mm: the MRK); ``attitude`` ``(roll, pitch, yaw)`` of the drone in degrees - each None when the
    source does not give it. ``valid`` is False for an event the receiver marks as without a valid time. ``source``
    says where the shot was read: the file, its kind, the line or message, and what the source adds (the GPS week and
    seconds of the week, the time system, the accuracy of the event time...)."""

    index: int | None
    gps_time: float
    antenna: tuple[float, float, float] | None = None
    height: str | None = None
    std: tuple[float, float, float] | None = None
    status: str | None = None
    lever_ned_mm: tuple[float, float, float] | None = None
    attitude: tuple[float, float, float] | None = None
    valid: bool = True
    source: dict = field(default_factory=dict)


@dataclass
class ShotSet:
    """The shots read from one source, in the order of their times: ``kind`` (one of :data:`KINDS`), ``name`` (of the
    file), ``warnings`` (sentences about what was read), ``duplicates`` (how many double shots were left out), ``path``
    (of the file, when the reader gives it)."""

    shots: list[Shot]
    kind: str
    name: str = ""
    warnings: list[str] = field(default_factory=list)
    duplicates: int = 0
    path: str = ""  # of the file read, when there is one (where its photos are by default)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {self.kind!r}")


# --- a DJI Timestamp.MRK as shots --------------------------------------------------------------------------------


def from_mrk(path: str | Path) -> ShotSet:
    """The shots of a DJI ``Timestamp.MRK`` (:func:`.dji.read_mrk`, with its errors): the index of each line, its GPS
    time (``source["gps_week"]`` and ``source["tow"]`` exact), the antenna with its ellipsoidal height, the standard
    deviations north, east, vertical, the status of its RTK flag (``"unknown"`` for an unlisted one, the flag in
    ``source["flag"]``) and the lever arm north, east, down of the line."""
    path = Path(path)
    shots = [Shot(index=m.index, gps_time=m.gps_time, antenna=(m.lat, m.lon, m.ellh), height="ellipsoidal",
                  std=m.std, status=dji.STATUS.get(m.flag, "unknown"), lever_ned_mm=m.offset_ned_mm,
                  source={"file": str(path), "kind": "mrk", "gps_week": m.gps_week, "tow": m.gps_seconds,
                          "flag": m.flag})
             for m in dji.read_mrk(path)]
    return ShotSet(shots=shots, kind="mrk", name=path.name, path=str(path))


def utc(shot: Shot) -> datetime:
    """The time of ``shot`` in UTC (with its time zone): its GPS time - from ``source["gps_week"]`` and
    ``source["tow"]`` when the source gives them, exact as :attr:`.dji.MrkShot.utc` - less the leap seconds of then."""
    week, tow = shot.source.get("gps_week"), shot.source.get("tow")
    gps = (dji.GPS_EPOCH + timedelta(weeks=week, seconds=tow) if week is not None and tow is not None
           else dji.GPS_EPOCH + timedelta(seconds=shot.gps_time))
    return gps - timedelta(seconds=dji._gps_minus_utc(gps.replace(tzinfo=None)))


# --- the photos paired with the shots ----------------------------------------------------------------------------

CLOCK_TOLERANCE_S = 3.0  # a photo belongs to its shot when its time is this close to the clock of the flight
ZONE_STEP_S = 900.0  # the camera keeps the local time: whole quarter hours from UTC ...
ZONE_TOLERANCE_S = 120.0  # ... give or take the drift of its clock ...
ZONE_MAX_S = 14 * 3600.0  # ... and never more than 14 hours
# paired by their times, a photo and a shot are within this of the clock of the flight: the camera writes whole
# seconds, so on a steady clock the offsets photo - shot of a flight lie in a band one second wide, and the clock is
# the middle of the band of 2 x PAIR_WINDOW_S holding the most of them (a tenth of a second either side for the delay
# of the camera); never the median, which the seconds of a camera shooting every whole number of seconds can push to
# an edge of the band
PAIR_WINDOW_S = 0.6
DOUBLE_SHOT_S = 0.02  # paired by their times, a shot this close after the one before is the same shot twice (a
# bouncing contact; the readers of the events already leave out those within 5 ms)
NEAR_SHOT_S = 1.0  # paired by their times, a photo whose shot has another this close cannot tell which one is its own
# a pairing by number or by order whose photos disagree with the times of their shots (by more than PAIR_WINDOW_S
# from the median) in more than this many photos - or this share of them, whichever is more - is refused; fewer are
# left without a shot, with a warning
DISAGREE_MAX = 2
DISAGREE_SHARE = 0.02
# the sources whose index is the number of the image (DJI MRK, ArduPilot CAM Img, PX4 camera_capture seq), which
# "auto" pairs by the numbers in the names of the photos; the counter of a receiver's events is no image number
NUMBERED_KINDS = ("mrk", "ardupilot_cam", "px4_capture")
BY = ("auto", "number", "time", "order")

_TRAILING_NUMBER = re.compile(r"(\d+)$")
_EPOCH = datetime(1980, 1, 6)  # photo and shot times as seconds from here (photos on the clock of their camera)


def photo_number(name: str) -> int | None:
    """The number of a photo in its name: the shot number of a DJI photo (:func:`.dji.shot_number`), else the digits
    at the end of its name (``IMG_0123.JPG`` 123, ``DSC01234.JPG`` 1234, ``0123.JPG`` 123); None without."""
    number = dji.shot_number(name)
    if number is not None:
        return number
    m = _TRAILING_NUMBER.search(Path(name).stem)
    return int(m.group(1)) if m else None


def photo_time(cam: Camera) -> datetime | None:
    """The time the camera wrote in the EXIF of the photo (its clock: the local time, to the second); None without
    one."""
    try:
        return datetime.strptime(str(cam.meta.get("datetime"))[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


@dataclass
class Pairing:
    """The photos of a folder paired with a set of shots (:func:`pair`).

    ``kept`` are the ``(photo, shot, camera)`` whose photo is in the chunk and was taken at its shot, in the order of
    the photos; ``extra_shots`` the shots (valid, not a double) no photo took; ``photos_without_shot`` the photos of
    the folder with no shot kept for them (no shot, not in the chunk, not on time); ``by`` how they were paired
    (``"number"``, ``"time"`` or ``"order"``); ``clock_s`` the clock of the camera less UTC (seconds) the photos agree
    on, None when no photo has a date. ``invalid`` are the shots without a valid time (never paired),
    ``left_doubles`` the shots left out as the second of a double when pairing by time, ``doubles`` those plus the
    doubles the source itself left out; ``paired`` every photo of the folder with the shot it was given first (None for
    none), before the times were checked; ``latency_s`` what was added to the time of every shot. ``confirmed`` says
    whether the times confirm the pairing: True paired by time, or by number or order and the times pair them so too;
    False by number or by order where the times cannot tell (no date, intervals so regular that a shift by whole shots
    looks like the clock of the camera): positions to check; None for a DJI MRK, checked as it always was.
    ``photos_ambiguous`` are the photos left without a shot because another shot was within :data:`NEAR_SHOT_S` of
    theirs (also in ``photos_without_shot``); ``warnings`` what the pairing leaves to say."""

    name: str  # of the source of the shots
    kind: str
    shots: list[Shot]  # all of the source
    files: list[Path]  # the photos of the folder, by name
    paired: list[tuple[Path, Shot | None]]
    kept: list[tuple[Path, Shot, Camera]]
    by: str
    clock_s: float | None = None
    latency_s: float = 0.0
    extra_shots: list[Shot] = field(default_factory=list)
    photos_without_shot: list[Path] = field(default_factory=list)
    invalid: list[Shot] = field(default_factory=list)
    left_doubles: list[Shot] = field(default_factory=list)
    doubles: int = 0
    confirmed: bool | None = None
    photos_ambiguous: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _folder_of(shotset: ShotSet, photos_folder: str | Path | None) -> Path:
    if photos_folder:
        return Path(photos_folder)
    files = [s.source.get("file") for s in shotset.shots if s.source.get("file")] + [shotset.path]
    files = [f for f in files if f]
    if not files:
        raise ValueError(f"{shotset.name or shotset.kind}: say in which folder the photos of these shots are")
    return Path(files[0]).parent


def pair(chunk: Chunk, shotset: ShotSet, photos_folder: str | Path | None = None, *, by: str = "auto",
         latency_s: float = 0.0) -> Pairing:
    """The photos of ``photos_folder`` (default: the folder of the file of the shots) paired with the shots of
    ``shotset``, ``latency_s`` added to the time of every shot (the camera exposes that long after the event, so
    positive), checked against the times of the photos in ``chunk``. Shots without a valid time are never paired.

    ``by`` is ``"number"`` (the index of a shot is the number in the name of its photo: :func:`photo_number`, the
    shot numbers of DJI photos when the folder has any), ``"time"`` (the clock of the camera found from the times of
    the photos and of the shots - a time zone give or take :data:`ZONE_TOLERANCE_S` - then each photo paired one to
    one, in time order, with the shot closest to it on that clock, within :data:`PAIR_WINDOW_S`; a shot within
    :data:`DOUBLE_SHOT_S` after the one before left out as a double; photos without a date left without a shot),
    ``"order"`` (only with as many photos as shots) or ``"auto"``: a DJI MRK as it always was (by the shot numbers
    when the photos have DJI names, else by order with as many photos as shots); for the other sources by number when
    the source numbers its images (:data:`NUMBERED_KINDS`) and at least half of its numbers are in the names of the
    photos, else by time when photos have a date, else by order with as many photos as shots.

    Paired by number or by order, the pairs are checked against the times of the photos as for a DJI MRK: the photos
    agree on one clock, a time zone from the UTC of the shots, and a photo more than :data:`CLOCK_TOLERANCE_S` from it
    keeps no shot. A ``ValueError`` when the photos cannot be paired: by order with counts that differ, a shot number
    twice, times that agree on no time zone or that fit more than one flight, fewer than half of the pairs on time."""
    if by not in BY:
        raise ValueError(f"by must be one of {', '.join(BY)}, not {by!r}")
    name, noun = shotset.name or shotset.kind, "MRK" if shotset.kind == "mrk" else "source"
    folder = _folder_of(shotset, photos_folder)
    files = sorted((f for f in folder.iterdir() if f.is_file() and not f.name.startswith(".")
                    and f.suffix.lower() in IMAGE_EXTENSIONS), key=lambda f: f.name)
    cameras = {Path(c.path).resolve(): c for c in chunk.cameras}
    in_chunk = [cameras.get(f.resolve()) for f in files]
    valid = [s for s in shotset.shots if s.valid]
    invalid = [s for s in shotset.shots if not s.valid]
    if shotset.shots and not valid:
        raise ValueError(f"{name}: its {len(shotset.shots)} shots have no valid time: they cannot be paired "
                         "with photos")
    dji_numbers = [dji.shot_number(f.name) for f in files]
    numbers = dji_numbers if any(n is not None for n in dji_numbers) else [photo_number(f.name) for f in files]
    if by == "auto":
        by = _auto(shotset.kind, valid, files, numbers, dji_numbers, in_chunk)
    dated = [(f, cam, photo_time(cam)) for f, cam in zip(files, in_chunk) if cam is not None]
    dated = [(f, cam, t) for f, cam, t in dated if t is not None]

    def timed():
        if not dated:
            raise ValueError(f"{name}: the photos of {folder} in the chunk have no date: they cannot be paired by "
                             "time")
        kept_shots, doubles = _without_doubles(valid, latency_s)
        return (*_by_time(dated, kept_shots, latency_s, name, noun), doubles)

    left_doubles: list[Shot] = []
    ambiguous: list[Path] = []
    warnings: list[str] = []
    confirmed = None
    if by == "time":
        pairs, clock, ambiguous, left_doubles = timed()
        given = {f: shot for f, shot, _cam in pairs}
        paired = [(f, given.get(f)) for f in files]
        kept = [(f, given[f], cam) for f, cam in zip(files, in_chunk) if f in given]
        confirmed = True
    else:
        if by == "number":
            if any(shot.index is None for shot in valid):
                raise ValueError(f"{name}: these shots carry no numbers: they cannot be paired by number")
            by_index = {}
            for shot in valid:
                if shot.index in by_index:
                    raise ValueError(f"{name}: shot {shot.index} appears twice")
                by_index[shot.index] = shot
            paired = [(f, by_index.get(n)) for f, n in zip(files, numbers)]
        else:
            if len(valid) != len(files):
                raise ValueError(_order_refused(name, len(valid), folder, len(files), shotset.kind))
            paired = list(zip(files, valid))
        candidates = [(f, shot, cam) for (f, shot), cam in zip(paired, in_chunk) if shot is not None and cam is not None]
        if shotset.kind == "mrk":  # as it always was
            kept, clock = _on_time(candidates, name, latency_s, noun)
        else:
            kept, clock, confirmed, warnings = _confirmed(candidates, timed, name, by, latency_s)
    used = {id(shot) for _f, shot, _cam in kept} | {id(s) for s in left_doubles}
    placed = {f for f, _shot, _cam in kept}
    return Pairing(name=name, kind=shotset.kind, shots=list(shotset.shots), files=files, paired=paired, kept=kept,
                   by=by, clock_s=clock, latency_s=float(latency_s),
                   extra_shots=[s for s in valid if id(s) not in used],
                   photos_without_shot=[f for f in files if f not in placed], invalid=invalid,
                   left_doubles=left_doubles, doubles=shotset.duplicates + len(left_doubles), confirmed=confirmed,
                   photos_ambiguous=ambiguous, warnings=warnings)


def _confirmed(candidates: list[tuple], timed, name: str, by: str,
               latency_s: float) -> tuple[list[tuple], float | None, bool, list[str]]:
    """A pairing by number or by order (``candidates``: ``(file, shot, camera)``) of a source other than a DJI MRK
    checked by the times of the photos: ``(kept, clock, confirmed, warnings)``.

    Without a date nothing is checked: all kept, not confirmed. Else the pairing by time (``timed``) is made too: a
    photo it gives another shot is a ``ValueError`` naming the first; when the times alone cannot pair the photos, the
    pairing is kept but not confirmed (a shift by whole shots cannot be excluded), with a warning. Then every photo
    must be within :data:`PAIR_WINDOW_S` of the clock of the offsets photo - shot (:func:`_band`), a time zone from
    UTC: more than
    :data:`DISAGREE_MAX` (or :data:`DISAGREE_SHARE` of them) that are not is a ``ValueError`` naming the first, fewer
    are left without a shot with a warning; photos without a date among dated ones keep no shot (with a warning)."""
    times = [photo_time(cam) for _f, _shot, cam in candidates]
    if not any(times):
        return candidates, None, False, [_not_confirmed(by, "the photos have no date")]
    late = timedelta(seconds=latency_s)
    offsets = [None if t is None else (t - (utc(shot).replace(tzinfo=None) + late)).total_seconds()
               for t, (_f, shot, _cam) in zip(times, candidates)]
    try:
        by_time, why = {f: shot for f, shot, _cam in timed()[0]}, None
    except ValueError as exc:
        by_time, why = None, exc
    if by_time is not None:
        clash = [(f, shot, by_time[f]) for f, shot, _cam in candidates if f in by_time and by_time[f] is not shot]
        if clash:
            f, shot, other = clash[0]
            raise ValueError(f"{name}: paired by {by}, {len(clash)} photo{'s' if len(clash) > 1 else ''} disagree with "
                             f"the times, which pair {'them' if len(clash) > 1 else 'it'} with other shots: the first, "
                             f"{f.name}, has shot {shot.index} by {by} and shot {other.index} by time. Pair them by "
                             "time")
    dated = [o for o in offsets if o is not None]
    clock = _band(dated)
    if _off_zone(clock) > ZONE_TOLERANCE_S:
        raise ValueError(f"{name}: paired by {by}, the photos were taken {_signed(clock)} (most of them) from the UTC of "
                         f"their shots, {_signed(_off_zone(clock))} from the nearest time zone: the times disagree "
                         f"with this pairing")
    disagree = [(p, o) for p, o in zip(candidates, offsets) if o is not None and abs(o - clock) > PAIR_WINDOW_S]
    if len(disagree) > max(DISAGREE_MAX, DISAGREE_SHARE * len(dated)):
        (f, _shot, _cam), o = disagree[0]
        raise ValueError(f"{name}: paired by {by}, {len(disagree)} of {len(dated)} photos disagree with the times of "
                         f"their shots by more than {PAIR_WINDOW_S:g} s (the first, {f.name}, by {_signed(o - clock)}): "
                         f"the {by} pairs photos with the shots of others. Pair them by time")
    kept = [p for p, o in zip(candidates, offsets) if o is not None and abs(o - clock) <= PAIR_WINDOW_S]
    warnings = []
    if disagree:
        n = len(disagree)
        warnings.append(f"{n} photo{'s' if n > 1 else ''} paired by {by} disagree{'' if n > 1 else 's'} with the time "
                        f"of {'their shots' if n > 1 else 'its shot'} by more than {PAIR_WINDOW_S:g} s and "
                        f"{'are' if n > 1 else 'is'} left without a shot (the first, {disagree[0][0][0].name})")
    undated = sum(o is None for o in offsets)
    if undated:
        warnings.append(f"{undated} photo{'s' if undated > 1 else ''} without a date left without a shot: the times "
                        f"cannot check {'them' if undated > 1 else 'it'}")
    if by_time is None:
        warnings.append(_not_confirmed(by, f"the times alone cannot pair the photos ({str(why).split(': ', 1)[-1]})"))
    return kept, clock, by_time is not None, warnings


def _not_confirmed(by: str, why: str) -> str:
    return (f"pairing by {by} not confirmed by the times: {why}; a shift by whole shots cannot be excluded, check the "
            "positions on a control point")


def _auto(kind: str, valid: list[Shot], files: list[Path], numbers: list, dji_numbers: list, in_chunk: list) -> str:
    """How ``"auto"`` pairs the photos (see :func:`pair`)."""
    if kind == "mrk":  # as it always was
        return "number" if any(n is not None for n in dji_numbers) else "order"
    indices = [s.index for s in valid if s.index is not None]
    names = {n for n in numbers if n is not None}
    if kind in NUMBERED_KINDS and indices and 2 * sum(i in names for i in indices) >= len(indices):
        return "number"
    if any(cam is not None and photo_time(cam) is not None for cam in in_chunk):
        return "time"
    return "order"


def _order_refused(name: str, shots: int, folder: Path, photos: int, kind: str) -> str:
    if kind == "mrk":
        return (f"{name} has {shots} shots and {folder} has {photos} photos, and the photo names carry no shot "
                "number: they cannot be paired by order")
    return (f"{name} has {shots} shots and {folder} has {photos} photos: they cannot be paired by order, and the photos "
            "cannot be paired by number or by time either (names without the numbers of the shots, no date)")


def _shot_seconds(shot: Shot, latency_s: float) -> float:
    """The UTC of ``shot`` plus the latency, in seconds from :data:`_EPOCH` (as the photo times)."""
    return (utc(shot).replace(tzinfo=None) - _EPOCH).total_seconds() + latency_s


def _without_doubles(shots: list[Shot], latency_s: float) -> tuple[list[tuple[Shot, float]], list[Shot]]:
    """The shots in time order with their seconds (:func:`_shot_seconds`), but those within :data:`DOUBLE_SHOT_S`
    after the one kept before them, which are returned apart."""
    kept, doubles = [], []
    for shot, t in sorted(((s, _shot_seconds(s, latency_s)) for s in shots), key=lambda item: item[1]):
        if kept and t - kept[-1][1] < DOUBLE_SHOT_S:
            doubles.append(shot)
        else:
            kept.append((shot, t))
    return kept, doubles


def _off_zone(offset):
    """Seconds from ``offset`` (local time less UTC; a number or an array) to the nearest time zone: a whole quarter
    hour within 14 hours."""
    zone = np.clip(np.round(np.asarray(offset, float) / ZONE_STEP_S) * ZONE_STEP_S, -ZONE_MAX_S, ZONE_MAX_S)
    off = np.abs(np.asarray(offset, float) - zone)
    return float(off) if np.ndim(off) == 0 else off


def clock_text(seconds: float) -> str:
    """The clock of the camera less UTC as it is written for a person: ``+8:59:59.5`` (hours:minutes:seconds, with
    the tenths when there are some) from a minute on, ``+0.5 s`` under a minute."""
    tenths = int(round(abs(float(seconds)) * 10))
    sign = "-" if seconds < 0 and tenths else "+"
    if tenths < 600:
        return f"{sign}{tenths / 10:.1f} s"
    whole, tenth = divmod(tenths, 10)
    hours, rest = divmod(whole, 3600)
    return f"{sign}{hours}:{rest // 60:02d}:{rest % 60:02d}" + (f".{tenth}" if tenth else "")


def _signed(seconds: float) -> str:
    return ("-" if seconds < 0 else "+") + str(timedelta(seconds=round(abs(seconds))))


def _no_zone(name: str, offset: float, noun: str) -> ValueError:
    return ValueError(f"{name}: the times of the photos and of the shots do not agree: by the clock of the camera "
                      f"the photos were taken {_signed(offset)} from the UTC of their shots, which is no time zone. "
                      f"These are not the photos of this {noun}, or the clock of the camera was wrong")


def _more_than_one_flight(name: str, clocks: list[float]) -> ValueError:
    return ValueError(f"{name}: the times of the photos fit the shots of more than one flight (at "
                      f"{', '.join(_signed(c) for c in clocks)} from UTC): keep the photos of each flight in a folder "
                      "of their own")


def _clock(offsets: np.ndarray, name: str, noun: str = "MRK") -> float:
    """The offset between the clock of the photos and UTC (seconds) of the flight of the shots of ``name``, from the
    offsets of photos paired with their shots: of the groups of offsets within :data:`CLOCK_TOLERANCE_S` of each other
    that lie on a time zone, the largest (its median), between groups as large the nearest to a time zone.
    ``ValueError`` when no group lies on a time zone, or when another group on a time zone has two photos or more and
    is at least half as large (another flight in the folder: the times cannot tell which one the shots are of - its
    own flight may have lost photos)."""
    s = np.sort(np.asarray(offsets, float))
    lo = np.searchsorted(s, s - CLOCK_TOLERANCE_S, "left")
    hi = np.searchsorted(s, s + CLOCK_TOLERANCE_S, "right")
    groups = sorted({(int(b - a), float(np.median(s[a:b]))) for a, b in zip(lo, hi)},
                    key=lambda g: (-g[0], _off_zone(g[1])))
    plausible = [g for g in groups if _off_zone(g[1]) <= ZONE_TOLERANCE_S]
    if not plausible:
        raise _no_zone(name, groups[0][1], noun)
    size, clock = plausible[0]
    # another flight: at least two photos and half as many as the best group (a single photo late is just late)
    rivals = sorted({round(c) for n, c in plausible
                     if n >= max(2, size / 2) and abs(c - clock) > 2 * CLOCK_TOLERANCE_S})
    if rivals:
        raise _more_than_one_flight(name, [clock] + rivals)
    return clock


def _on_time(pairs: list[tuple], name: str, latency_s: float = 0.0,
             noun: str = "MRK") -> tuple[list[tuple], float | None]:
    """The ``(file, shot, camera)`` pairs whose photo was taken at the time of its shot (see :func:`pair`) and the
    clock of the photos, or all of them and None when no photo has a date."""
    times = [photo_time(cam) for _f, _shot, cam in pairs]
    if not any(times):
        return pairs, None
    late = timedelta(seconds=latency_s)
    offsets = [None if t is None else (t - (utc(shot).replace(tzinfo=None) + late)).total_seconds()
               for t, (_f, shot, _cam) in zip(times, pairs)]
    clock = _clock(np.array([o for o in offsets if o is not None]), name, noun)
    kept = [p for p, o in zip(pairs, offsets) if o is not None and abs(o - clock) <= CLOCK_TOLERANCE_S]
    if 2 * len(kept) < len(pairs):
        raise ValueError(f"{name}: only {len(kept)} of {len(pairs)} photos were taken at the time of their shot "
                         f"(clock of the camera {_signed(clock)} from UTC): the {noun} is of another flight, or the "
                         "folder holds the photos of other flights too")
    return kept, clock


_BIN_S = 0.25  # the offsets between photos and shots counted in bins this wide ...
_WINDOW_BINS = 5  # ... over windows of this many bins (a photo written to the whole second spreads over one)
_AROUND_S = 60.0  # the pairs of photos and shots by chance: counted this far either side of a window
_SIGNIFICANT = 3.0  # a clock stands out of the pairs by chance by this many of their standard deviations
_BLOCK = 2_000_000  # offsets counted at a time
_CANDIDATES = 12  # clocks of the windows tried, and around the likeliest ...
_AROUND_BEST_S = np.arange(-3.0, 3.01, 0.5)  # ... these
# paired by their times, another way of pairing them with this many pairs fewer than the best, or more, is as likely:
# shots at intervals so regular that the times cannot tell which shot each photo is
AMBIGUOUS_PAIRS = 2
MIN_INTERVAL_S = 1.5  # shots closer than this (median) cannot be paired with photos timed to the whole second


def _ambiguous(name: str, found: list[tuple[float, int]]) -> ValueError:
    """The times pair the photos with the shots about as well on several clocks of the camera (``(clock, pairs)``)."""
    clocks = ", ".join(f"{_signed(c)} ({n} photos)" for c, n in found)
    return ValueError(f"{name}: the times of the photos fit more than one way of pairing them with the shots, on the "
                      f"clocks of the camera {clocks} from UTC: the photos cannot be paired by time, and paired by "
                      "number or by order they are not checked by the times either")


def _several_clocks(name: str, clocks: list[float]) -> ValueError:
    return ValueError(f"{name}: the times of the photos fit the shots on more than one clock of the camera, "
                      f"{', '.join(_signed(c) for c in clocks)} from UTC, as well as each other: the photos cannot be "
                      "paired by time, and paired by number or by order they are not checked by the times either")


def _off_any_zone(name: str, clock: float) -> ValueError:
    return ValueError(f"{name}: the times of the photos fit the shots best on a clock of the camera {_signed(clock)} "
                      f"from UTC, {_signed(_off_zone(clock))[1:]} from the nearest time zone (more than "
                      f"{ZONE_TOLERANCE_S:g} s): the photos cannot be paired by time")


def _clocks_of_times(photos: np.ndarray, shots: np.ndarray, name: str, noun: str) -> list[float]:
    """The clocks of the camera less UTC (seconds, to a bin) on which the photos may have their shots, the likeliest
    first: the windows of offsets photo - shot (:data:`_WINDOW_BINS` bins of :data:`_BIN_S`) on a time zone
    (:data:`ZONE_TOLERANCE_S`) that hold more pairs than they would by chance (the mean of the windows within
    :data:`_AROUND_S`) by :data:`_SIGNIFICANT` times the square root of those, by how many more, within
    :data:`ZONE_TOLERANCE_S` of the first, at most :data:`_CANDIDATES` a second or more apart. A ``ValueError`` when no
    window stands out, or when another window on a time zone more than :data:`ZONE_TOLERANCE_S` away stands out at
    least half as much."""
    keys, counts = [], []
    rows = max(1, _BLOCK // max(1, len(shots)))
    for start in range(0, len(photos), rows):
        k = np.floor((photos[start:start + rows, None] - shots[None, :]) / _BIN_S).astype(np.int64).ravel()
        u, c = np.unique(k, return_counts=True)
        keys.append(u)
        counts.append(c)
    keys, inverse = np.unique(np.concatenate(keys), return_inverse=True)
    counts = np.bincount(inverse, weights=np.concatenate(counts))
    total = np.concatenate([[0.0], np.cumsum(counts)])
    first = np.arange(len(keys))
    windows = total[np.searchsorted(keys, keys + _WINDOW_BINS, "left")] - total[first]
    around = int(round(_AROUND_S / _BIN_S))
    nearby = (total[np.searchsorted(keys, keys + _WINDOW_BINS // 2 + around, "left")]
              - total[np.searchsorted(keys, keys + _WINDOW_BINS // 2 - around, "left")])
    chance = nearby * _WINDOW_BINS / (2.0 * around)  # what a window holds by chance there
    excess = windows - chance
    needed = np.maximum(_SIGNIFICANT, _SIGNIFICANT * np.sqrt(chance))
    centres = (keys + _WINDOW_BINS / 2.0) * _BIN_S
    off = _off_zone(centres)
    plausible = np.flatnonzero((off <= ZONE_TOLERANCE_S) & (excess >= needed))
    if not len(plausible):
        if np.any(off <= ZONE_TOLERANCE_S) or not np.any(excess >= needed):
            raise ValueError(f"{name}: the times of the photos fit the times of the shots on no clock of the camera "
                             f"within {ZONE_TOLERANCE_S:g} s of a time zone: the photos cannot be paired by time")
        raise _off_any_zone(name, float(centres[np.argmax(excess)]))
    best = plausible[np.lexsort((off[plausible], -excess[plausible]))[0]]
    clock, size = float(centres[best]), float(excess[best])
    rivals = plausible[(excess[plausible] >= size / 2.0) & (np.abs(centres[plausible] - clock) > ZONE_TOLERANCE_S)]
    if len(rivals):
        found: list[tuple[float, float]] = []  # one clock for each group of rival windows: the fullest
        for i in rivals[np.argsort(centres[rivals])]:
            if found and centres[i] - found[-1][0] <= 2 * CLOCK_TOLERANCE_S:
                if excess[i] > found[-1][1]:
                    found[-1] = (float(centres[i]), float(excess[i]))
                continue
            found.append((float(centres[i]), float(excess[i])))
        raise _several_clocks(name, [clock] + [c for c, _n in found])
    near = plausible[np.abs(centres[plausible] - clock) <= ZONE_TOLERANCE_S]
    clocks: list[float] = []
    for i in near[np.lexsort((off[near], -excess[near]))]:
        if all(abs(centres[i] - c) >= 1.0 for c in clocks):
            clocks.append(float(centres[i]))
            if len(clocks) == _CANDIDATES:
                break
    return clocks


def _chain(photos: np.ndarray, shots: np.ndarray, clock: float, window: float) -> tuple[list[tuple[int, int]], float]:
    """The pairs ``(photo, shot)`` (indices into ``photos`` and ``shots``, both in time order) one to one and in the
    same order on both sides that best fit the ``clock``: as many as there can be, each weighing less the further its
    photo is from its shot on that clock, none ``window`` or more apart; and their weight."""
    m = len(shots)
    tree = [(0.0, -1)] * (m + 1)  # prefix maxima over the shots (Fenwick): best chain ending at or before a shot

    def best_before(j: int) -> tuple[float, int]:  # over the shots 0..j-1
        found = (0.0, -1)
        while j > 0:
            if tree[j][0] > found[0]:
                found = tree[j]
            j -= j & -j
        return found

    def put(j: int, item: tuple[float, int]) -> None:  # at shot j
        j += 1
        while j <= m:
            if item[0] > tree[j][0]:
                tree[j] = item
            j += j & -j

    nodes: list[tuple[int, int, int]] = []  # (photo, shot, previous node)
    best = (0.0, -1)
    for i, t in enumerate(photos):
        lo = np.searchsorted(shots, t - clock - window, "right")
        hi = np.searchsorted(shots, t - clock + window, "left")
        row = []
        for j in range(lo, hi):
            weight = 1.0 - ((t - clock - shots[j]) / window) ** 2
            if weight <= 0.0:
                continue
            value, previous = best_before(j)
            row.append((j, value + weight, previous))
        for j, value, previous in row:  # after the whole row: one shot per photo
            nodes.append((i, j, previous))
            put(j, (value, len(nodes) - 1))
            if value > best[0]:
                best = (value, len(nodes) - 1)
    chain, node = [], best[1]
    while node >= 0:
        i, j, node = nodes[node]
        chain.append((i, j))
    return chain[::-1], best[0]


def _band(offsets) -> float:
    """The middle of the band ``2 x PAIR_WINDOW_S`` wide holding the most of ``offsets`` (photo - shot, seconds): the
    clock of the camera less UTC of a flight (:data:`PAIR_WINDOW_S`)."""
    v = np.sort(np.asarray(offsets, float))
    ends = np.searchsorted(v, v + 2 * PAIR_WINDOW_S, "right")
    first = int(np.argmax(ends - np.arange(len(v))))
    return float((v[first] + v[ends[first] - 1]) / 2.0)


def _alignment(photos: np.ndarray, shots: np.ndarray, clock: float) -> tuple[int, float, float, list]:
    """``(pairs, weight, clock, chain)`` of the photos and the shots near ``clock`` (known to a second): paired
    loosely, the clock taken again as the middle of the band of the pairs (:func:`_band`), paired again within
    :data:`PAIR_WINDOW_S`."""
    chain, _weight = _chain(photos, shots, clock, PAIR_WINDOW_S + _WINDOW_BINS * _BIN_S / 2.0)
    if not chain:
        return 0, 0.0, clock, []
    clock = _band([photos[i] - shots[j] for i, j in chain])
    chain, weight = _chain(photos, shots, clock, PAIR_WINDOW_S)
    return len(chain), weight, clock, chain


def _by_time(dated: list[tuple[Path, Camera, datetime]], shots: list[tuple[Shot, float]], latency_s: float,
             name: str, noun: str) -> tuple[list[tuple[Path, Shot, Camera]], float | None, list[Path]]:
    """The photos with a date paired with the ``(shot, seconds)`` by their times (see :func:`pair`), the clock of the
    camera less UTC they agree on (:func:`_band`) and the photos left without a shot because another shot
    is within :data:`NEAR_SHOT_S` of theirs (which of the two is the photo's cannot be told): of the clocks of
    :func:`_clocks_of_times` and those around the likeliest, the one that pairs the most photos one to one (then the
    closest). A ``ValueError`` when the shots are too close together (:data:`MIN_INTERVAL_S`), when another clock
    pairs them almost as well (:data:`AMBIGUOUS_PAIRS`), or when fewer than half of them are paired."""
    if not dated or not shots:
        return [], None, []
    times = np.array([t for _shot, t in shots])
    if len(times) > 1 and float(np.median(np.diff(times))) < MIN_INTERVAL_S:
        raise ValueError(f"{name}: the shots came every {float(np.median(np.diff(times))):.2g} s (median): photos "
                         "timed to the whole second cannot be paired with them by time; pair them by number")
    dated = sorted(dated, key=lambda item: (item[2], item[0].name))
    photos = np.array([(t - _EPOCH).total_seconds() for _f, _cam, t in dated])
    clocks = _clocks_of_times(photos, times, name, noun)
    tried = clocks + [clocks[0] + d for d in _AROUND_BEST_S]
    found: list[tuple[int, float, float, list]] = []
    for clock in tried:
        result = _alignment(photos, times, clock)
        if result[0] and all(result[3] != other[3] for other in found):
            found.append(result)
    found.sort(key=lambda r: (-r[0], -r[1]))
    if not found:
        raise _off_any_zone(name, clocks[0])
    count, _weight, clock, chain = found[0]
    best = set(chain)
    # another way of pairing them: most of its pairs are not those of the best
    rivals = [r for r in found[1:] if r[0] >= count - AMBIGUOUS_PAIRS and 2 * len(best & set(r[3])) < r[0]]
    if rivals:
        raise _ambiguous(name, [(r[2], r[0]) for r in [found[0]] + rivals])
    if 2 * count < min(len(dated), len(shots)):
        raise ValueError(f"{name}: only {count} of {min(len(dated), len(shots))} photos have a shot at their time, on "
                         f"the clock of the camera that pairs the most ({_signed(clock)} from UTC): the photos cannot "
                         "be paired by time")
    clock = _band([photos[i] - times[j] for i, j in chain])
    near = [(i, j) for i, j in chain
            if (j > 0 and times[j] - times[j - 1] < NEAR_SHOT_S) or (j + 1 < len(times) and times[j + 1] - times[j]
                                                                       < NEAR_SHOT_S)]
    kept = [(i, j) for i, j in chain if (i, j) not in set(near)]
    return ([(dated[i][0], shots[j][0], dated[i][1]) for i, j in kept], clock,
            sorted((dated[i][0] for i, _j in near), key=lambda f: f.name))
