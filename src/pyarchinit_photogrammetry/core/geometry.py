"""Geometric helpers: similarity transforms, triangulation and rotations.

All transforms are 4x4 homogeneous matrices stored as ``numpy.ndarray``.
A *similarity* transform maps ``x -> s * R @ x + t``.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def identity() -> np.ndarray:
    return np.eye(4)


def compose_similarity(scale: float, rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = scale * np.asarray(rotation, dtype=float)
    m[:3, 3] = np.asarray(translation, dtype=float)
    return m


def decompose_similarity(m: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    """Return ``(scale, rotation, translation)`` of a similarity matrix."""
    a = np.asarray(m, dtype=float)[:3, :3]
    scale = float(np.cbrt(np.linalg.det(a)))
    return scale, a / scale, np.asarray(m, dtype=float)[:3, 3].copy()


def apply(m: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Apply a 4x4 transform to an ``(N, 3)`` (or ``(3,)``) array."""
    pts = np.asarray(points, dtype=float)
    single = pts.ndim == 1
    pts = np.atleast_2d(pts)
    out = pts @ m[:3, :3].T + m[:3, 3]
    return out[0] if single else out


def umeyama(src: np.ndarray, dst: np.ndarray, with_scale: bool = True, weights: np.ndarray | None = None) -> np.ndarray:
    """Least-squares similarity transform mapping ``src`` onto ``dst``.

    Implements Umeyama (1991), optionally weighted (e.g. ``1 / accuracy**2``).
    ``src`` and ``dst`` are ``(N, 3)`` arrays with N >= 3 non-collinear points.
    Returns a 4x4 matrix ``M`` such that ``apply(M, src) ~= dst``.
    """
    src = np.asarray(src, dtype=float)
    dst = np.asarray(dst, dtype=float)
    if src.shape != dst.shape or src.shape[0] < 3:
        raise ValueError("umeyama needs two (N,3) arrays with N >= 3")
    w = np.ones(len(src)) if weights is None else np.asarray(weights, float)
    w = w / w.sum()
    mu_s, mu_d = w @ src, w @ dst
    xs, xd = src - mu_s, dst - mu_d
    cov = (xd * w[:, None]).T @ xs
    u, d, vt = np.linalg.svd(cov)
    sign = np.eye(3)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        sign[2, 2] = -1
    rot = u @ sign @ vt
    if with_scale:
        var_s = (w[:, None] * xs ** 2).sum()
        scale = float(np.trace(np.diag(d) @ sign) / var_s)
    else:
        scale = 1.0
    t = mu_d - scale * rot @ mu_s
    return compose_similarity(scale, rot, t)


@dataclass
class PinholeView:
    """Minimal projective camera used for triangulation: ``x ~ K [R|t] X``.

    ``world_from_cam`` is the 4x4 camera pose in the chunk frame.
    Image observations passed to :func:`triangulate` must already be
    *normalised* (undistorted, multiplied by K^-1), i.e. ``(x/z, y/z)``.
    """

    world_from_cam: np.ndarray

    @property
    def cam_from_world(self) -> np.ndarray:
        return np.linalg.inv(self.world_from_cam)

    @property
    def center(self) -> np.ndarray:
        return self.world_from_cam[:3, 3]


def triangulate(views: list[PinholeView], normalized: np.ndarray, refine: bool = True) -> np.ndarray:
    """Triangulate one point from >= 2 normalised observations.

    Linear DLT followed by a Gauss-Newton refinement of the angular error.
    """
    if len(views) < 2:
        raise ValueError("at least two views are needed to triangulate")
    rows = []
    for view, (x, y) in zip(views, np.asarray(normalized, dtype=float)):
        p = view.cam_from_world[:3, :]
        rows.append(x * p[2] - p[0])
        rows.append(y * p[2] - p[1])
    _, _, vt = np.linalg.svd(np.asarray(rows))
    xh = vt[-1]
    point = xh[:3] / xh[3]
    if not refine:
        return point
    from scipy.optimize import least_squares

    def residuals(pt):
        res = []
        for view, (x, y) in zip(views, normalized):
            pc = view.cam_from_world[:3, :3] @ pt + view.cam_from_world[:3, 3]
            res.extend([pc[0] / pc[2] - x, pc[1] / pc[2] - y])
        return np.asarray(res)

    return least_squares(residuals, point, method="lm").x


def point_on_ray(points: np.ndarray, origin, direction, radius: float, min_points: int = 15) -> np.ndarray | None:
    """Where the ray meets the surface: the points within ``radius`` of the ray that belong to
    the first surface it crosses define a local plane (least squares), and the ray is
    intersected with it. Robust on textureless spots (targets, rulers) where the dense cloud
    is thin, as long as the neighbourhood is locally flat."""
    v = points - origin
    t = v @ direction
    perp = np.linalg.norm(v - np.outer(t, direction), axis=1)
    ok = (t > 0) & (perp < radius)
    if ok.sum() < min_points:
        return None
    near, tn = points[ok], t[ok]
    t0 = np.percentile(tn, 5)
    first = near[tn <= t0 + 2 * radius]
    if len(first) < min_points:
        first = near[np.argsort(tn)[:min_points]]
    c = first.mean(axis=0)
    normal = np.linalg.svd(first - c)[2][-1]
    denom = float(normal @ direction)
    if abs(denom) < 1e-6:  # grazing ray: fall back to the depth of the first points
        return origin + float(np.median(tn[tn <= t0 + 2 * radius])) * direction
    return origin + float(normal @ (c - origin) / denom) * direction


def rotation_from_euler_ypr(yaw: float, pitch: float, roll: float) -> np.ndarray:
    """Rotation matrix from yaw/pitch/roll in degrees (Z-Y-X convention)."""
    y, p, r = np.radians([yaw, pitch, roll])
    rz = np.array([[np.cos(y), -np.sin(y), 0], [np.sin(y), np.cos(y), 0], [0, 0, 1]])
    ry = np.array([[np.cos(p), 0, np.sin(p)], [0, 1, 0], [-np.sin(p), 0, np.cos(p)]])
    rx = np.array([[1, 0, 0], [0, np.cos(r), -np.sin(r)], [0, np.sin(r), np.cos(r)]])
    return rz @ ry @ rx


def look_at(eye: np.ndarray, target: np.ndarray, up: np.ndarray = (0, 0, 1)) -> np.ndarray:
    """Camera pose (world_from_cam, OpenCV convention: +Z forward, +Y down)."""
    eye = np.asarray(eye, float)
    z = np.asarray(target, float) - eye
    z /= np.linalg.norm(z)
    up = np.asarray(up, float)
    x = np.cross(z, up)
    if np.linalg.norm(x) < 1e-9:
        x = np.cross(z, np.array([0.0, 1.0, 0.0]))
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = x, y, z, eye
    return m
