"""The frame of a local coordinate system and the motion that sets it.

Pure functions, no chunk: a :class:`Frame` is built from three picked points or from a plane
fitted to points, and :func:`motion` turns it into the 4x4 similarity that moves the chunk's
current coordinates into the new system.

Angle conventions (degrees):

* ``tilt_deg``: angle between the new z axis and the current z axis, 0..180.
* ``tilt_direction_deg``: azimuth, clockwise from the current +y (north), of the steepest descent
  of the new horizontal plane (the direction the new plane falls toward); 0 when untilted.
* ``heading_deg``: angle of the new x axis in the current plan, counter-clockwise from the
  current +x (so a pure rotation about z by theta has heading theta), in (-180, 180].
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..core import geometry as geo

REASONS = ("crs", "markers", "aligned_to", "scalebars", "collinear", "plane_points", "plane_rough")
COLLINEAR_SIN = 1e-3
MIN_PLANE_POINTS = 30
MAX_PLANE_NMAD = 0.10
HUBER_K = 1.345


class OrientRefused(ValueError):
    """The orientation cannot be done; ``reason`` is one of :data:`REASONS`."""

    def __init__(self, reason: str, message: str = "", names: list[str] | None = None):
        super().__init__(message or reason)
        self.reason = reason
        self.names = list(names or [])


def _angles(axes: np.ndarray) -> tuple[float, float, float]:
    z, x = axes[2], axes[0]
    tilt = float(np.degrees(np.arccos(np.clip(z[2], -1.0, 1.0))))
    direction = float(np.degrees(np.arctan2(z[0], z[1]))) if np.hypot(z[0], z[1]) > 1e-12 else 0.0
    heading = float(np.degrees(np.arctan2(x[1], x[0])))
    return tilt, direction, heading


@dataclass
class Frame:
    origin: np.ndarray
    axes: np.ndarray  # rows x, y, z

    @property
    def tilt_deg(self) -> float:
        return _angles(self.axes)[0]

    @property
    def tilt_direction_deg(self) -> float:
        return _angles(self.axes)[1]

    @property
    def heading_deg(self) -> float:
        return _angles(self.axes)[2]


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def _frame(origin, x_hint: np.ndarray, normal: np.ndarray) -> Frame:
    """x_hint projected on the plane of ``normal``; y = z x x (right-handed)."""
    n = _unit(normal)
    x = x_hint - (x_hint @ n) * n
    if np.linalg.norm(x) < 1e-9 * max(np.linalg.norm(x_hint), 1e-300):
        raise OrientRefused("collinear", "the x direction is perpendicular to the plane")
    x = _unit(x)
    y = np.cross(n, x)
    return Frame(np.asarray(origin, dtype=float).copy(), np.vstack([x, y, n]))


def frame_from_points(a, b, c=None, x_along_ab: bool = True, flip: bool = False) -> Frame:
    """The frame set by points A, B (and C): A origin, x along A->B (or the current +x)."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    ab = b - a
    if np.linalg.norm(ab) == 0:
        raise OrientRefused("collinear", "A and B coincide")
    if c is None:
        plan = np.array([ab[0], ab[1], 0.0])
        if np.linalg.norm(plan) < COLLINEAR_SIN * np.linalg.norm(ab):
            raise OrientRefused("collinear", "A-B is vertical: it gives no direction in plan")
        return _frame(a, plan, np.array([0.0, 0.0, -1.0 if flip else 1.0]))
    c = np.asarray(c, dtype=float)
    ac = c - a
    cross = np.cross(ab, ac)
    sin = np.linalg.norm(cross) / (np.linalg.norm(ab) * max(np.linalg.norm(ac), 1e-300))
    if sin < COLLINEAR_SIN:
        raise OrientRefused("collinear", "the three points are on a line")
    n = _unit(cross)
    if n[2] < 0:
        n = -n
    if flip:
        n = -n
    return _frame(a, ab if x_along_ab else np.array([1.0, 0.0, 0.0]), n)


def _nmad(r: np.ndarray) -> float:
    return float(1.4826 * np.median(np.abs(r - np.median(r))))


def fit_plane(xyz) -> tuple[np.ndarray, np.ndarray, float]:
    """Robust plane: ``(centre, unit normal, NMAD of the residuals)``."""
    p = np.asarray(xyz, dtype=float)
    w = np.ones(len(p))
    centre = p.mean(0)
    normal = np.array([0.0, 0.0, 1.0])
    previous = None
    for _ in range(21):  # least squares, then 20 reweightings
        centre = (w[:, None] * p).sum(0) / w.sum()
        q = (p - centre) * np.sqrt(w)[:, None]
        normal = np.linalg.svd(q, full_matrices=False)[2][2]
        r = (p - centre) @ normal
        scale = _nmad(r)
        k = HUBER_K * max(scale, 1e-12)
        w = np.where(np.abs(r) <= k, 1.0, k / np.maximum(np.abs(r), 1e-300))
        if previous is not None and np.max(np.abs(w - previous)) < 1e-9:
            break
        previous = w
    centre = (w[:, None] * p).sum(0) / w.sum()
    r = (p - centre) @ normal
    return centre, normal, _nmad(r)


def frame_from_plane(xyz, toward, a=None, b=None, flip: bool = False) -> Frame:
    """The frame of a plane fitted to ``xyz``; z on the side of the point ``toward``."""
    p = np.asarray(xyz, dtype=float)
    if len(p) < MIN_PLANE_POINTS:
        raise OrientRefused("plane_points", f"{len(p)} points: at least {MIN_PLANE_POINTS} are needed")
    centre, n, nmad = fit_plane(p)
    size = float(np.max(p.max(0) - p.min(0)))
    if nmad > MAX_PLANE_NMAD * size:
        raise OrientRefused("plane_rough", "the points are not on a plane")
    if (np.asarray(toward, dtype=float) - centre) @ n < 0:
        n = -n
    if flip:
        n = -n
    origin = centre
    x_hint = np.array([1.0, 0.0, 0.0])
    if a is not None:
        a = np.asarray(a, dtype=float)
        origin = a - ((a - centre) @ n) * n
        if b is not None:
            hint = np.asarray(b, dtype=float) - a
            if np.linalg.norm(hint - (hint @ n) * n) > 1e-12:
                x_hint = hint
    return _frame(origin, x_hint, n)


def motion(frame: Frame, quota: float = 0.0, origin_xy=(0.0, 0.0), scale: float = 1.0) -> np.ndarray:
    """p' = scale * R (p - origin) + (X0, Y0, quota), R = ``frame.axes``."""
    r = np.asarray(frame.axes, dtype=float)
    t = np.array([origin_xy[0], origin_xy[1], quota], dtype=float)
    return geo.compose_similarity(scale, r, t - scale * r @ frame.origin)


def describe(m) -> dict:
    s, r, t = geo.decompose_similarity(np.asarray(m, dtype=float))
    tilt, direction, heading = _angles(r)
    return {
        "tilt_deg": tilt,
        "tilt_direction_deg": direction,
        "heading_deg": heading,
        "translation": [float(v) for v in t],
        "scale": float(s),
    }


def scale_for(a, b, distance: float) -> float:
    """The factor that makes |AB| equal ``distance`` (in the new units)."""
    d = float(np.linalg.norm(np.asarray(b, dtype=float) - np.asarray(a, dtype=float)))
    if not distance > 0 or not d > 0:
        raise ValueError("the distance and |AB| must be positive")
    return float(distance) / d
