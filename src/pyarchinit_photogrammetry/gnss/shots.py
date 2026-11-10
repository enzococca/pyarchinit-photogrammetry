"""Shots of a camera as data, whatever records them: the lines of a DJI ``Timestamp.MRK``, the external events of a
receiver (RINEX epoch flag 5, u-blox TIM-TM2, an ``_events.pos``), the camera messages of an autopilot log (ArduPilot
CAM/TRIG, PX4 camera_capture/camera_trigger). A shot has its time in GPS time and, when the source gives them, the
position of the antenna, its accuracy and the attitude of the drone; a set of shots says what it was read from and what
was left out of it."""
from __future__ import annotations

from dataclasses import dataclass, field

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
    file), ``warnings`` (sentences about what was read), ``duplicates`` (how many double shots were left out)."""

    shots: list[Shot]
    kind: str
    name: str = ""
    warnings: list[str] = field(default_factory=list)
    duplicates: int = 0

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {self.kind!r}")
