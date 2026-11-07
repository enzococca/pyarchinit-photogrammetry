"""Looking through a photo: the photo undistorted onto an ideal pinhole camera lands where its rays go (pinhole,
distorted and fisheye sensors), the view that looks through it puts every point of the scene on the pixel of the
photo that sees it, the photo that best matches a view and the order of the photos."""
from __future__ import annotations

import types

import cv2
import numpy as np
import pytest

from pyarchinit_photogrammetry.core.camera import Sensor
from pyarchinit_photogrammetry.engine import look_through as lt
from pyarchinit_photogrammetry.gui.scene import OrbitCamera


def _dots(sensor, rays, path):
    """A black photo of ``sensor`` with a white dot where each ray lands; the pixels (COLMAP) of the dots."""
    px = np.asarray(sensor.project(rays))
    img = np.zeros((sensor.height, sensor.width, 3), np.uint8)
    for u, v in px:
        cv2.circle(img, (int(round((u - 0.5) * 16)), int(round((v - 0.5) * 16))), 3 * 16, (255, 255, 255), -1,
                   lineType=cv2.LINE_AA, shift=4)
    cv2.imwrite(str(path), img)
    return px


def _centroid(rgba, near, r=8):
    x0, y0 = int(near[0]) - r, int(near[1]) - r
    win = rgba[max(y0, 0):y0 + 2 * r + 1, max(x0, 0):x0 + 2 * r + 1, 0].astype(np.float64)
    yy, xx = np.mgrid[0:win.shape[0], 0:win.shape[1]]
    s = win.sum()
    assert s > 0, "no dot there"
    return np.array([(xx * win).sum() / s + max(x0, 0), (yy * win).sum() / s + max(y0, 0)])


@pytest.mark.parametrize("model, params", [
    ("PINHOLE", [500.0, 520.0, 310.0, 195.0]),
    ("OPENCV", [480.0, 480.0, 305.0, 202.0, -0.22, 0.05, 0.002, -0.001]),
    ("OPENCV_FISHEYE", [260.0, 260.0, 300.0, 200.0, 0.08, -0.02, 0.0, 0.0]),
])
def test_the_undistorted_photo_has_each_ray_where_the_ideal_camera_puts_it(tmp_path, model, params):
    sensor = Sensor(1, "s", 600, 400, model, params)
    gx, gy = np.meshgrid(np.linspace(-0.45, 0.45, 5), np.linspace(-0.3, 0.3, 4))
    rays = np.column_stack([gx.ravel(), gy.ravel(), np.ones(gx.size)])
    _dots(sensor, rays, tmp_path / "photo.png")
    ideal = lt.ideal_camera(sensor)
    assert (ideal.width, ideal.height) == (600, 400) and ideal.f == pytest.approx(sensor.focal_px)
    rgba, maps = lt.photo_and_maps(tmp_path / "photo.png", sensor, ideal)
    assert rgba.shape == (400, 600, 4) and rgba.dtype == np.uint8
    expected = ideal.to_pixels(rays[:, :2]) - 0.5  # COLMAP pixels -> array indices
    for e in expected:
        if 10 < e[0] < ideal.width - 10 and 10 < e[1] < ideal.height - 10:
            assert np.linalg.norm(_centroid(rgba, e) - e) < 0.5
    # the same maps for the next photo of the sensor
    again, maps2 = lt.photo_and_maps(tmp_path / "photo.png", sensor, ideal, maps)
    assert maps2 is maps and np.array_equal(again, rgba)


def test_a_reduced_ideal_camera_and_a_reduced_read(tmp_path):
    """A photo larger than the view is read at 1/2, 1/4 or 1/8 and resampled to MAX_SIDE: the rays still land."""
    sensor = Sensor(1, "s", 4000, 3000, "OPENCV", [3200.0, 3200.0, 2010.0, 1490.0, -0.1, 0.02, 0.0, 0.0])
    rays = np.array([[0.0, 0.0, 1.0], [0.4, 0.3, 1.0], [-0.35, 0.2, 1.0]])
    px = np.asarray(sensor.project(rays))
    img = np.zeros((3000, 4000, 3), np.uint8)
    for u, v in px:
        cv2.circle(img, (int(round(u - 0.5)), int(round(v - 0.5))), 14, (255, 255, 255), -1)
    cv2.imwrite(str(tmp_path / "big.jpg"), img)
    ideal = lt.ideal_camera(sensor, max_side=800)
    assert (ideal.width, ideal.height) == (800, 600) and ideal.f == pytest.approx(640.0)
    assert max(lt.read_reduced(tmp_path / "big.jpg", 800).shape[:2]) == 1000  # 1/4: still >= 800
    rgba = lt.undistort_photo(tmp_path / "big.jpg", sensor, ideal)
    for e in ideal.to_pixels(rays[:, :2]) - 0.5:
        assert np.linalg.norm(_centroid(rgba, e, r=6) - e) < 0.6
    # a marker measured on the photo: where it is on the undistorted photo
    assert np.allclose(lt.photo_to_ideal(sensor, ideal, px), ideal.to_pixels(rays[:, :2]), atol=1e-6)


def test_an_unreadable_photo(tmp_path):
    sensor = Sensor(1, "s", 60, 40, "PINHOLE", [50.0, 50.0, 30.0, 20.0])
    (tmp_path / "empty.jpg").write_bytes(b"")
    with pytest.raises(OSError):
        lt.undistort_photo(tmp_path / "empty.jpg", sensor, lt.ideal_camera(sensor))


def _chunk(transform):
    T = np.asarray(transform, np.float64)
    return types.SimpleNamespace(transform=T, to_world=lambda p: np.asarray(p) @ T[:3, :3].T + T[:3, 3])


@pytest.mark.parametrize("view", [(640, 480), (500, 600), (900, 300)])
def test_the_view_through_the_photo_puts_points_on_their_pixels(view):
    """A similarity chunk -> world, a rotated camera: an OrbitCamera at the photo's pose with the field of view of
    ``fit_in_view`` projects every point where the ideal image (scaled into its rectangle) has it."""
    rng = np.random.default_rng(4)
    a = 0.4
    Rc = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1.0]])
    T = np.eye(4)
    T[:3, :3] = 2.5 * Rc
    T[:3, 3] = [1000.0, 2000.0, 50.0]
    b = 0.3
    Rcam = np.array([[1, 0, 0], [0, np.cos(b), -np.sin(b)], [0, np.sin(b), np.cos(b)]]) @ np.diag([1, -1, -1.0])
    cam = types.SimpleNamespace(transform=np.eye(4), label="IMG")
    cam.transform[:3, :3] = Rcam
    cam.transform[:3, 3] = [0.2, -0.1, 3.0]
    chunk = _chunk(T)
    centre, basis = lt.photo_pose(chunk, cam)
    assert np.allclose(basis @ basis.T, np.eye(3), atol=1e-9)
    sensor = Sensor(1, "s", 1200, 800, "SIMPLE_PINHOLE", [900.0, 600.0, 400.0])
    ideal = lt.ideal_camera(sensor, max_side=600)
    fov, (x0, y0, w, h) = lt.fit_in_view(ideal, *view)
    s = w / ideal.width
    assert h == pytest.approx(ideal.height * s) and (x0 == pytest.approx(0) or y0 == pytest.approx(0))
    oc = OrbitCamera(centre + basis[2] * 5.0, 5.0, fov=fov)
    oc.R = basis
    # points in front of the photo (camera frame), then in the world
    pc = np.column_stack([rng.uniform(-0.4, 0.4, 50), rng.uniform(-0.25, 0.25, 50), np.ones(50)]) * rng.uniform(
        1, 5, (50, 1))
    internal = pc @ Rcam.T + cam.transform[:3, 3]
    world = chunk.to_world(internal)
    x, y, _z, ok = oc.project(world, *view)
    assert ok.all()
    u = ideal.to_pixels(pc[:, :2] / pc[:, 2:])
    assert np.allclose(x, x0 + s * u[:, 0], atol=1e-6) and np.allclose(y, y0 + s * u[:, 1], atol=1e-6)


def test_order_and_best_photo():
    def cam(label, yaw, centre, when=None, aligned=True):
        R = np.array([[np.cos(yaw), 0, np.sin(yaw)], [0, 1, 0], [-np.sin(yaw), 0, np.cos(yaw)]])
        T = np.eye(4)
        T[:3, :3] = R
        T[:3, 3] = centre
        return types.SimpleNamespace(label=label, transform=T if aligned else None, aligned=aligned, enabled=True,
                                     meta={"datetime": when} if when else {})

    cams = [cam("B", 0.0, [0, 0, 0], "2024:01:01 10:00:02"), cam("A", 0.5, [1, 0, 0], "2024:01:01 10:00:01"),
            cam("C", 0.0, [5, 0, 0], "2024:01:01 10:00:03"), cam("D", 0.0, [0, 0, 0], aligned=False)]
    chunk = _chunk(np.eye(4))
    chunk.cameras = cams
    assert [c.label for c in lt.ordered_cameras(chunk)] == ["A", "B", "C"]
    # looking along +z from near the origin: B (same direction, same place); from x = 5: C
    assert lt.best_photo(chunk, [0.1, 0, 0], [0, 0, 1.0], 10.0).label == "B"
    assert lt.best_photo(chunk, [5, 0, 0.2], [0, 0, 1.0], 10.0).label == "C"
    # turned the other way: none
    assert lt.best_photo(chunk, [0, 0, 0], [0, 0, -1.0], 10.0) is None


def test_photos_without_a_time_come_after_the_dated_ones():
    def cam(label, when=None):
        return types.SimpleNamespace(label=label, aligned=True, enabled=True, transform=np.eye(4),
                                     meta={"datetime": when} if when else {})

    chunk = _chunk(np.eye(4))
    chunk.cameras = [cam("A"), cam("Z", "2024:01:01 10:00:02"), cam("B"), cam("Y", "2024:01:01 10:00:01")]
    assert [c.label for c in lt.ordered_cameras(chunk)] == ["Y", "Z", "A", "B"]
