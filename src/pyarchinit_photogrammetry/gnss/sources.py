"""Where the shots of a flight come from, read the same way by the command line and by the window: a DJI
``Timestamp.MRK``, the events of the rover's receiver (its RINEX, or the raw log it was converted from), the time marks
of a u-blox log, an autopilot log (ArduPilot DataFlash or PX4 ULog), an ``_events.pos`` of RTKLIB; and the files of a
PPK run with shots of any of these sources - raw rover and base logs converted to RINEX first, the shots of the rover
read from its conversion."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .shots import ShotSet

KINDS = ("mrk", "events", "ubx", "log", "events-pos")  # the sources of the shots (``ppg gnss ppk --shots``)
LOG_MESSAGES = ("auto", "cam", "trig", "capture", "trigger")
ULOG_MAGIC = b"ULog\x01\x12\x35"


class OptionError(ValueError):
    """An option that does not fit the source of the shots: ``option`` names it (``"--message"``, ``"--camera"``)."""

    def __init__(self, text: str, option: str):
        super().__init__(text)
        self.option = option


def is_ulog(path: str | Path) -> bool:
    """``path`` is a PX4 log (ULog), told by its first bytes."""
    try:
        with open(path, "rb") as f:
            return f.read(len(ULOG_MAGIC)) == ULOG_MAGIC
    except OSError:
        return False


def check_log_options(path: str | Path, message: str = "auto", camera: int | None = None) -> None:
    """The message and camera asked of the autopilot log ``path``: an :class:`OptionError` when they do not fit it (a
    PX4 log has capture and trigger messages and one camera; an ArduPilot log cam and trig)."""
    if message not in LOG_MESSAGES:
        raise OptionError(f"one of {', '.join(LOG_MESSAGES)}", "--message")
    if is_ulog(path):
        if message in ("cam", "trig"):
            raise OptionError("a PX4 log has capture and trigger messages, not cam and trig", "--message")
        if camera is not None:
            raise OptionError("a PX4 log has one camera", "--camera")
    elif message in ("capture", "trigger"):
        raise OptionError("an ArduPilot log has cam and trig messages, not capture and trigger", "--message")


def log_shots(path: str | Path, message: str = "auto", camera: int | None = None) -> ShotSet:
    """The shots of an autopilot log: PX4 (ULog, :func:`.px4.px4`) or ArduPilot (DataFlash, :func:`.autopilot.ardupilot`),
    the options checked first (:func:`check_log_options`)."""
    from . import autopilot, px4

    check_log_options(path, message, camera)
    if is_ulog(path):
        return px4.px4(path, message)
    return autopilot.ardupilot(path, message, camera)


def read(kind: str, path: str | Path, edge: str = "falling", message: str = "auto",
         camera: int | None = None) -> ShotSet:
    """The shots of ``path`` read as ``kind`` (one of :data:`KINDS`); ``edge`` for the time marks of a u-blox log,
    ``message`` and ``camera`` for an autopilot log."""
    from . import events, shots

    path = Path(path)
    if kind == "mrk":
        return shots.from_mrk(path)
    if kind == "events":
        return events.rinex_events(path)
    if kind == "ubx":
        return events.ubx_time_marks(path, edge)
    if kind == "log":
        return log_shots(path, message, camera)
    if kind == "events-pos":
        return events.events_pos(path)
    raise OptionError(f"one of {', '.join(KINDS)}", "--shots")


_GPS_EPOCH = datetime(1980, 1, 6)


def is_raw(path: str | Path) -> bool:
    """Whether ``path`` is the raw log of a receiver, to convert to RINEX before a PPK: what
    :func:`.convert.raw_kind` knows, and every D-RTK ``.DAT`` - for a rover as for a base."""
    from . import convert

    path = Path(path)
    return path.suffix.lower() == ".dat" or convert.raw_kind(path) not in (None, "rinex")


def first_epoch(observations) -> float | None:
    """The first epoch of RINEX observations in GPS seconds: ``observations`` is their file or its header
    (:class:`.rinex.RinexInfo`); None when it is not known - a file that is not RINEX, one without epochs."""
    from . import rinex

    info = observations
    if not isinstance(info, rinex.RinexInfo):
        try:
            info = rinex.header(Path(observations))
        except (OSError, ValueError):
            return None
    return None if info.first is None else (info.first - _GPS_EPOCH).total_seconds()


def first_shot(shotset) -> float | None:
    """The time (GPS seconds) of the first shot of ``shotset`` with a valid time, None when there is none."""
    for shot in getattr(shotset, "shots", None) or ():
        if getattr(shot, "valid", True):
            return shot.gps_time
    return None


def base_time(rover, shotset=None) -> float | None:
    """The approximate time (GPS seconds) for the conversion of a raw base log, the same for a run and for the
    dialog that prepares it: the first epoch of the rover in RINEX (its file, its header, those of its conversion),
    else the first shot of ``shotset`` with a valid time; None when nothing tells."""
    when = first_epoch(rover)
    return when if when is not None else first_shot(shotset)


@dataclass
class PpkInputs:
    """The files of a PPK run as RINEX: the ``shotset``, the ``rover`` and ``bases`` observations (the conversions of
    raw logs in their place), the ``converted`` logs (:class:`.convert.Converted`, the rover's first) and the
    approximate time used for the bases (GPS seconds, None when none was needed or known)."""

    shotset: ShotSet
    rover: Path
    bases: list[Path]
    converted: list = field(default_factory=list)
    approx_time: float | None = None

    def navs(self, given=()) -> list[Path]:
        """The navigation files of the run: those of these conversions first, then the ``given`` ones that are not
        the navigation of a conversion (a file in the cache of the conversions belongs to the conversion that made
        it, which may be gone: the run takes those of its own)."""
        from . import download

        cache = download.cache_dir("converted").resolve()
        found = [Path(done.nav) for done in self.converted if done.nav]
        for path in given or ():
            path = Path(path)
            if path not in found and cache not in path.resolve().parents:
                found.append(path)
        return found


    def record(self, solution) -> None:
        """The record of a run made on these files (``solution.inputs``, what :func:`.ppk.last_run` remembers and
        the dialog starts again from) with the files as the user named them: a raw log in the place of its
        conversion, the conversions as a note of their own (``"converted"``, as :func:`.ppk.solve` writes it for
        the logs it converts itself: where the events of a raw rover are read), their navigation files only there."""
        if not self.converted:
            return
        inputs = dict(solution.inputs or {})
        made = {str(Path(done.obs).absolute()): str(Path(done.source).absolute()) for done in self.converted}
        navs = {str(Path(done.nav).absolute()) for done in self.converted if done.nav}

        def named(value):
            if isinstance(value, str):
                return made.get(value, value)
            return value if value is None else [made.get(item, item) for item in value]

        inputs["rover"], inputs["base"] = named(inputs.get("rover")), named(inputs.get("base"))
        inputs["navs"] = [nav for nav in inputs.get("navs") or [] if nav not in navs]
        notes = list(inputs.get("converted") or [])
        known = {note.get("source") for note in notes}
        notes += [{"source": made[str(Path(done.obs).absolute())], "kind": done.kind,
                   "obs": str(Path(done.obs).absolute()),
                   "nav": str(Path(done.nav).absolute()) if done.nav else None, "epochs": done.epochs,
                   "events": done.events}
                  for done in self.converted if made[str(Path(done.obs).absolute())] not in known]
        inputs["converted"] = notes
        solution.inputs = inputs


def ppk_inputs(kind: str, shots_file: str | Path | None, rover: str | Path, bases=(), *, approx_time=None,
               edge: str = "falling", message: str = "auto", camera: int | None = None, progress=None,
               reader=None) -> PpkInputs:
    """The shots and the observations of a PPK run: the shots of ``shots_file`` read as ``kind`` (for ``"events"`` and
    ``"ubx"`` the rover itself when no file is given; the events of a raw rover are those of its conversion), a raw
    rover or base log (:func:`is_raw`) converted to RINEX (:func:`.convert.to_rinex`). The approximate time of a
    conversion is ``approx_time`` when given; else, for the rover, the first shot with a valid time of a file of
    shots that is not the rover, and for a base :func:`base_time`: the first epoch of the rover (in RINEX, or
    converted), else that shot. ``reader(kind, path, edge, message, camera)`` reads the shots (default
    :func:`read`). The errors of the readers and of the conversions as they are; a cancelled ``progress`` stops a
    conversion."""
    from . import convert

    read_shots = reader or read
    rover = Path(rover)
    source = Path(shots_file) if shots_file is not None else (rover if kind in ("events", "ubx") else None)
    if source is None:
        raise ValueError(f"the shots of kind {kind} need their file")
    from_rover = kind == "events" and source.resolve() == rover.resolve()
    shotset = None if from_rover else read_shots(kind, source, edge, message, camera)
    converted = []
    if is_raw(rover):
        done = convert.to_rinex(rover, approx_time=approx_time if approx_time is not None else first_shot(shotset),
                                progress=progress)
        converted.append(done)
        rover = done.obs
    if from_rover:
        shotset = read_shots(kind, rover, edge, message, camera)
    when = approx_time
    observed = []
    for path in bases or ():
        path = Path(path)
        if is_raw(path):
            if when is None:
                when = base_time(rover, None if from_rover else shotset)
            done = convert.to_rinex(path, approx_time=when, progress=progress)
            converted.append(done)
            observed.append(done.obs)
        else:
            observed.append(path)
    return PpkInputs(shotset, rover, observed, converted, when)
