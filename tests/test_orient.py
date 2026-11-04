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
    assert d["heading_deg"] == pytest.approx(55, abs=1e-9)  # azimuth: 35 deg from +x = 55 from north
    assert d["tilt_direction_deg"] == pytest.approx(40, abs=1e-9)
    assert f.tilt_deg == pytest.approx(12, abs=1e-9)


def test_x_kept_projected_on_the_plane():
    # tilted toward +y the current +x lies in the plane: azimuth 90 (east); toward 40 it does not
    for direction in (0, 40):
        n = _plane_normal(12, direction)
        a = np.zeros(3)
        b = 3.0 * _in_plane(n, 35)
        c = 2.0 * _in_plane(n, 130)
        f = orient.frame_from_points(a, b, c, x_along_ab=False)
        ex = np.array([1.0, 0, 0])
        want = ex - (ex @ n) * n
        want /= np.linalg.norm(want)
        assert np.allclose(f.axes[0], want, atol=1e-12)
        assert abs(f.axes[0] @ f.axes[2]) < 1e-12
        az = np.degrees(np.arctan2(want[0], want[1])) % 360
        assert f.heading_deg == pytest.approx(az, abs=1e-9)
        if direction == 0:
            assert f.heading_deg == pytest.approx(90, abs=1e-9)


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
    for sin, ok in ((5e-4, False), (5e-3, True)):
        c = [0.5, sin, 0]  # AC at angle ~sin from AB, |AB| = |AC| about 1
        c = np.array(c) / np.linalg.norm(c)
        if ok:
            orient.frame_from_points([0, 0, 0], [1, 0, 0], c)
        else:
            with pytest.raises(orient.OrientRefused):
                orient.frame_from_points([0, 0, 0], [1, 0, 0], c)


def test_nan_refused():
    with pytest.raises(orient.OrientRefused) as e:
        orient.frame_from_points([0, 0, np.nan], [1, 0, 0], [0, 1, 0])
    assert e.value.reason == "nan"
    with pytest.raises(orient.OrientRefused):
        orient.frame_from_points([0, 0, 0], [1, np.nan, 0])
    pts, _ = _floor(31, 5, outliers=0)
    pts[0] = np.nan
    f = orient.frame_from_plane(pts, toward=[5, 5, 99])  # 30 finite points left
    assert np.isfinite(f.axes).all()
    pts[1] = np.nan
    with pytest.raises(orient.OrientRefused) as e:
        orient.frame_from_plane(pts, toward=[5, 5, 99])
    assert e.value.reason == "plane_points"


def test_degenerate_point_sets_refused():
    rng = np.random.default_rng(5)
    t = rng.uniform(0, 3, 50)
    line = np.c_[t, 2 * t, 0.1 * t]
    same = np.tile([1.0, 2.0, 3.0], (50, 1))
    strip = np.c_[rng.uniform(0, 3, 200), rng.uniform(0, 0.02, 200), rng.normal(0, 0.001, 200)]
    for pts in (line, same, strip):
        with pytest.raises(orient.OrientRefused) as e:
            orient.frame_from_plane(pts, toward=[0, 0, 9])
        assert e.value.reason == "plane_points"


def test_notes_on_replaced_choices():
    pts, n = _floor(100, 5, outliers=0)
    f = orient.frame_from_plane(pts, toward=[5, 5, 99], b=pts[0])
    assert "ignored" in f.note
    a = pts[0]
    f = orient.frame_from_plane(pts, toward=[5, 5, 99], a=a, b=a + n)
    assert "normal" in f.note
    assert orient.frame_from_plane(pts, toward=[5, 5, 99]).note == ""


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


def test_fitted_plane_with_clustered_outliers():
    rng = np.random.default_rng(7)
    n = _plane_normal(7.5, 70)
    u = _in_plane(n, 0)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    st = rng.uniform(-3, 3, size=(2000, 2))
    off = rng.normal(0, 0.001, 2000)
    off[(st[:, 0] > 1.5) & (np.arange(2000) % 3 == 0)] += 0.15  # one side of the floor, above it
    pts = st[:, :1] * u + st[:, 1:] * v + off[:, None] * n
    f = orient.frame_from_plane(pts, toward=pts.mean(0) + [0, 0, 10])
    # plain least squares tilts by about half a degree on this data; Huber must stay below 0.05
    assert np.degrees(np.arccos(np.clip(f.axes[2] @ n, -1, 1))) < 0.05


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
