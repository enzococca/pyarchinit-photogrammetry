"""The lever arm from the antenna to the camera of a drone built by its owner: a fixed vector in the body of the drone
(forward, right, down in millimetres) turned into north, east, down with the attitude of the shot (yaw, pitch and roll
in the aeronautical order of ArduPilot and PX4, or the quaternion of PX4), then added to the antenna as the arm of a
DJI Timestamp.MRK is (:func:`.dji.lever_arm`). Without an attitude only its vertical part is applied, and the caller
says so."""
from __future__ import annotations

import math
from collections.abc import Sequence

from . import dji

NO_ATTITUDE = "vertical only: no attitude"


def body_to_ned(body_frd_mm: Sequence[float], roll: float, pitch: float, yaw: float) -> tuple[float, float, float]:
    """The vector ``body_frd_mm`` of the body (forward, right, down) in north, east, down, the drone at ``roll``,
    ``pitch`` and ``yaw`` degrees: turned by the yaw about down, then the pitch about the new right, then the roll
    about the new forward (Z-Y-X); the yaw from north clockwise, the pitch positive nose up, the roll positive right
    wing down. Units in, units out."""
    f, r, d = (float(v) for v in body_frd_mm)
    cr, sr = math.cos(math.radians(roll)), math.sin(math.radians(roll))
    cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    cy, sy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    north = cy * cp * f + (cy * sp * sr - sy * cr) * r + (cy * sp * cr + sy * sr) * d
    east = sy * cp * f + (sy * sp * sr + cy * cr) * r + (sy * sp * cr - cy * sr) * d
    down = -sp * f + cp * sr * r + cp * cr * d
    return north, east, down


def quaternion_to_euler(w: float, x: float, y: float, z: float) -> tuple[float, float, float]:
    """``(roll, pitch, yaw)`` in degrees of the Hamilton quaternion ``w, x, y, z`` that turns the body into NED (the
    ``q`` of PX4's ``vehicle_attitude``), in the order of :func:`body_to_ned`; the yaw within (-180, 180]. The
    quaternion need not be of unit length."""
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if not norm:
        raise ValueError("a quaternion of zero length is no attitude")
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2.0 * (w * y - z * x))))
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return math.degrees(roll), math.degrees(pitch), math.degrees(yaw)


def camera_from_antenna(lat: float, lon: float, h: float, *, ned_mm: Sequence[float] | None = None,
                        body_frd_mm: Sequence[float] | None = None,
                        attitude: Sequence[float] | None = None) -> tuple[float, float, float, str | None]:
    """The antenna ``(lat, lon, h)`` in (degrees, ellipsoidal or sea-level height in metres), ``(lon, lat, h, note)``
    of the camera out - mind the two orders, as :func:`.dji.lever_arm`. The arm is either ``ned_mm`` (north, east,
    down in mm: the arm of a DJI MRK, already turned) or ``body_frd_mm`` (forward, right, down in mm in the body of the
    drone), turned with ``attitude`` ``(roll, pitch, yaw)`` in degrees (:func:`body_to_ned`); without an attitude only
    its down part is applied (the drone taken as level) and ``note`` is :data:`NO_ATTITUDE`, otherwise None. Without
    an arm the camera is at the antenna. A ``ValueError`` for both arms at once."""
    if ned_mm is not None and body_frd_mm is not None:
        raise ValueError("a lever arm north, east, down and one in the body of the drone: give one, not both")
    note = None
    if body_frd_mm is not None:
        if attitude is None:
            ned_mm, note = (0.0, 0.0, float(body_frd_mm[2])), NO_ATTITUDE
        else:
            ned_mm = body_to_ned(body_frd_mm, *(float(v) for v in attitude))
    if ned_mm is None:
        return float(lon), float(lat), float(h), None
    return (*dji.lever_arm(lat, lon, h, ned_mm), note)
