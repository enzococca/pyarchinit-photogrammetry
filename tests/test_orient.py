"""The frame of a local system and the motion that sets it (pure numpy)."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.core import geometry as geo
from pyarchinit_photogrammetry.engine import orient


def _plane_normal(tilt, direction):
    t, d = np.radians(tilt), np.radians(direction)
    return np.array([np.sin(t) * np.sin(d), np.sin(t) * np.cos(d), np.cos(t)])


def _in_plane(n, plan_deg):
    h = np.radians(plan_deg)
    hx, hy = np.cos(h), np.sin(h)
    return np.array([hx, hy, -(n[0] * hx + n[1] * hy) / n[2]])


def test_three_points_give_the_plane_and_x():
    n = _plane_normal(12, 40)
    a = np.array([10.0, -4.0, 2.0])
    b = a + 3.0 * _in_plane(n, 35)
    c = a + 2.0 * _in_plane(n, 130)
    f = orient.frame_from_points(a, b, c)
    m = orient.motion(f)
    assert np.allclose(geo.apply(m, a), 0, atol=1e-9)
    assert np.allclose(geo.apply(m, b), [np.linalg.norm(b - a), 0, 0], atol=1e-9)
    assert abs(geo.apply(m, c)[2]) < 1e-9
    d = orient.describe(m)
    assert d["tilt_deg"] == pytest.approx(12, abs=1e-9)
    assert d["heading_deg"] == pytest.approx(35, abs=1e-9)
    assert d["tilt_direction_deg"] == pytest.approx(40, abs=1e-9)
    assert f.tilt_deg == pytest.approx(12, abs=1e-9)


def test_x_kept_projected_on_the_plane():
    n = _plane_normal(12, 0)  # tilted toward +y: the current +x lies in the plane, heading exactly 0
    a = np.zeros(3)
    b = 3.0 * _in_plane(n, 35)
    c = 2.0 * _in_plane(n, 130)
    f = orient.frame_from_points(a, b, c, x_along_ab=False)
    assert f.heading_deg == pytest.approx(0, abs=1e-9)
    assert abs(f.axes[0] @ f.axes[2]) < 1e-12
    assert f.axes[0][0] > 0


def test_two_points_keep_the_horizontal():
    a = np.array([1.0, 2.0, 5.0])
    b = np.array([4.0, 6.0, 9.0])
    f = orient.frame_from_points(a, b)
    assert f.tilt_deg == 0
    m = orient.motion(f)
    pts = np.random.default_rng(1).normal(size=(10, 3))
    out = geo.apply(m, pts)
    assert np.allclose(out[:, 2], pts[:, 2] - a[2])
    assert np.allclose(geo.apply(m, b), [5.0, 0, 4.0], atol=1e-12)


def test_far_side_c_and_flip():
    a = np.zeros(3)
    b = np.array([1.0, 0, 0])
    c = np.array([-5.0, -3.0, 0.2])  # beyond the object, orientation of ABC is clockwise
    f = orient.frame_from_points(a, b, c)
    assert f.axes[2][2] > 0
    g = orient.frame_from_points(a, b, c, flip=True)
    assert g.axes[2][2] < 0
    for fr in (f, g):
        assert np.linalg.det(fr.axes) == pytest.approx(1.0)


def test_collinear_refused():
    with pytest.raises(orient.OrientRefused) as e:
        orient.frame_from_points([0, 0, 0], [1, 0, 0], [2, 0, 0])
    assert e.value.reason == "collinear"
    with pytest.raises(orient.OrientRefused):
        orient.frame_from_points([0, 0, 0], [0, 0, 0])


def _floor(n_points, tilt, outliers=0.1, seed=0):
    rng = np.random.default_rng(seed)
    n = _plane_normal(tilt, 70)
    u = _in_plane(n, 0)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    st = rng.uniform(-3, 3, size=(n_points, 2))
    pts = st[:, :1] * u + st[:, 1:] * v + rng.normal(0, 0.001, (n_points, 1)) * n
    k = int(outliers * n_points)
    pts[:k] += rng.uniform(0.05, 0.20, (k, 1)) * n
    return pts + np.array([5.0, 5.0, 1.0]), n


def test_fitted_plane_within_a_tenth_of_a_degree():
    pts, n = _floor(2000, 7.5)
    f = orient.frame_from_plane(pts, toward=pts.mean(0) + [0, 0, 10])
    ang = np.degrees(np.arccos(np.clip(f.axes[2] @ n, -1, 1)))
    assert ang < 0.1


def test_fitted_plane_refusals():
    pts, _ = _floor(29, 5, outliers=0)
    with pytest.raises(orient.OrientRefused) as e:
        orient.frame_from_plane(pts, toward=[0, 0, 9])
    assert e.value.reason == "plane_points"
    rng = np.random.default_rng(3)
    v = rng.normal(size=(500, 3))
    v[:, 2] = np.abs(v[:, 2])
    v /= np.linalg.norm(v, axis=1)[:, None]
    with pytest.raises(orient.OrientRefused) as e:
        orient.frame_from_plane(v, toward=[0, 0, 9])
    assert e.value.reason == "plane_rough"


def test_quota_origin_and_scale():
    n = _plane_normal(12, 40)
    a = np.array([3.0, 1.0, 2.0])
    b = a + 3.0 * _in_plane(n, 35)
    c = a + 2.0 * _in_plane(n, 130)
    f = orient.frame_from_points(a, b, c)
    m = orient.motion(f, quota=10, origin_xy=(100, 200), scale=2)
    assert np.allclose(geo.apply(m, a), [100, 200, 10], atol=1e-9)
    ab = np.linalg.norm(geo.apply(m, b) - geo.apply(m, a))
    assert ab == pytest.approx(2 * np.linalg.norm(b - a))
    assert orient.describe(m)["scale"] == pytest.approx(2)
    assert orient.scale_for(a, b, np.linalg.norm(b - a) * 2) == pytest.approx(2.0)
    with pytest.raises(ValueError):
        orient.scale_for(a, b, 0)
