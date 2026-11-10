"""Where the shots of a flight come from, read the same way by the command line and by the window: a DJI
``Timestamp.MRK``, the events of the rover's receiver (its RINEX, or the raw log it was converted from), the time marks
of a u-blox log, an autopilot log (ArduPilot DataFlash or PX4 ULog), an ``_events.pos`` of RTKLIB; and the files of a
PPK run with shots of any of these sources - raw rover and base logs converted to RINEX first, the shots of the rover
read from its conversion."""
from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class PpkInputs:
    """The files of a PPK run as RINEX: the ``shotset``, the ``rover`` and ``bases`` observations (the conversions of
    raw logs in their place), the ``converted`` logs (:class:`.convert.Converted`, the rover's first) and the
    approximate time used for them (GPS seconds, None when none was needed)."""

    shotset: ShotSet
    rover: Path
    bases: list[Path]
    converted: list = field(default_factory=list)
    approx_time: float | None = None


def ppk_inputs(kind: str, shots_file: str | Path | None, rover: str | Path, bases=(), *, approx_time=None,
               edge: str = "falling", message: str = "auto", camera: int | None = None, progress=None,
               reader=None) -> PpkInputs:
    """The shots and the observations of a PPK run: the shots of ``shots_file`` read as ``kind`` (for ``"events"`` and
    ``"ubx"`` the rover itself when no file is given; the events of a raw rover are those of its conversion), a raw
    rover or base log converted to RINEX (:func:`.convert.to_rinex`, with ``approx_time``: given, else the first shot
    with a valid time, else the first epoch of the rover). ``reader(kind, path, edge, message, camera)`` reads the
    shots (default :func:`read`). The errors of the readers and of the conversions as they are; a cancelled
    ``progress`` stops a conversion."""
    from . import convert

    read_shots = reader or read
    rover = Path(rover)
    source = Path(shots_file) if shots_file is not None else (rover if kind in ("events", "ubx") else None)
    if source is None:
        raise ValueError(f"the shots of kind {kind} need their file")
    from_rover = kind == "events" and source.resolve() == rover.resolve()
    shotset = None if from_rover else read_shots(kind, source, edge, message, camera)
    when = approx_time
    if when is None and shotset is not None:
        timed = [s.gps_time for s in shotset.shots if s.valid]
        when = timed[0] if timed else None
    converted = []
    if convert.raw_kind(rover) not in (None, "rinex"):
        done = convert.to_rinex(rover, approx_time=when, progress=progress)
        converted.append(done)
        rover = done.obs
        if when is None:
            when = done.span[0]
    if from_rover:
        shotset = read_shots(kind, rover, edge, message, camera)
    observed = []
    for path in bases or ():
        path = Path(path)
        if convert.raw_kind(path) not in (None, "rinex") or path.suffix.lower() == ".dat":
            done = convert.to_rinex(path, approx_time=when, progress=progress)
            converted.append(done)
            observed.append(done.obs)
        else:
            observed.append(path)
    return PpkInputs(shotset, rover, observed, converted, when)
