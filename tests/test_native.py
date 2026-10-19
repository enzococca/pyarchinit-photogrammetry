"""The native kernels (``ppg_core``, Rust) give what the numpy code gives: each one is compared
with the Python function it replaces (``PPG_NATIVE=0`` switches the extension off)."""
from __future__ import annotations

import shutil

import numpy as np
import pytest

ppg_core = pytest.importorskip("ppg_core")

from pyarchinit_photogrammetry import _native  # noqa: E402
from pyarchinit_photogrammetry.engine import confidence, dense, ortho, visibility  # noqa: E402


def both(monkeypatch, fn):
    """``fn()`` with the numpy code and with the native kernels."""
    monkeypatch.setenv("PPG_NATIVE", "0")
    reference = fn()
    monkeypatch.setenv("PPG_NATIVE", "1")
    return reference, fn()


def test_switch():
    assert _native.version() == ppg_core.__version__
    assert _native.supports("OPENCV") and not _native.supports("OPENCV_FISHEYE")


def test_depth_fusion_identical(monkeypatch, rng):
    h, w, k = 240, 320, 4
    true = (40 + 5 * rng.random((h, w))).astype(np.float32)
    est = []
    for _ in range(k):
        e = true * (1 + 0.003 * rng.standard_normal((h, w))).astype(np.float32)
        e[rng.random((h, w)) < 0.25] = 0
        bad = rng.random((h, w)) < 0.05
        e[bad] *= rng.uniform(0.5, 1.5, bad.sum()).astype(np.float32)
        est.append(e.astype(np.float32))
    for min_agree in (0, 1, 2):
        for tol in (0.01, 0.003):
            a, b = both(monkeypatch, lambda: dense._combine(est, min_agree, tol))
            assert b.dtype == np.float32 and np.array_equal(a, b), (min_agree, tol)
    one = [est[0]]  # a single partner
    a, b = both(monkeypatch, lambda: dense._combine(one, 1, 0.01))
    assert np.array_equal(a, b)


def test_normals_identical(monkeypatch, rng):
    h, w = 120, 160
    yy, xx = np.mgrid[0:h, 0:w]
    depth = (20 + 0.02 * xx + 0.01 * yy + 0.3 * np.sin(xx / 7.0) * np.cos(yy / 9.0)).astype(np.float32)
    depth[rng.random((h, w)) < 0.1] = 0
    K = np.array([[300.0, 0, 80.5], [0, 305.0, 60.25], [0, 0, 1]])
    a, b = both(monkeypatch, lambda: dense._normals_from_depth(depth, K))
    assert a.shape == b.shape == (h, w, 3) and b.dtype == np.float32
    assert np.array_equal(a, b)


def test_disparity_to_depth_matches(monkeypatch):
    import cv2

    rng = np.random.default_rng(3)
    h, w = 300, 400
    img_i = cv2.GaussianBlur((rng.random((h, w)) * 255).astype(np.uint8), (0, 0), 1.5)
    # the same textured plane seen by a second camera moved sideways and turned a little
    K = np.array([[420.0, 0, 200.5], [0, 420.0, 150.5], [0, 0, 1]])
    rvec = np.array([0.0, 0.03, 0.01])
    R, _ = cv2.Rodrigues(rvec)
    pose_i = np.eye(4)
    pose_j = np.eye(4)
    pose_j[:3, :3] = R
    pose_j[:3, 3] = [-0.4, 0.02, 0.0]
    Hm = K @ (R - np.outer(pose_j[:3, 3], [0, 0, 1]) / 6.0) @ np.linalg.inv(K)
    img_j = cv2.warpPerspective(img_i, Hm, (w, h))
    a, b = both(monkeypatch, lambda: dense._pair_depth(img_i, img_j, K, K, pose_i, pose_j, (3.0, 12.0)))
    assert a is not None and b is not None and a.shape == b.shape == (h, w)
    both_set = (a > 0) & (b > 0)
    assert (a > 0).sum() > 5000
    # a rounding at .5 px can move a depth to the next pixel: (almost) all pixels are the same
    assert np.mean((a > 0) == (b > 0)) > 0.999
    assert np.mean(a[both_set] == b[both_set]) > 0.999
    assert np.max(np.abs(a[both_set] - b[both_set]) / a[both_set]) < 0.02


MODELS = {
    "SIMPLE_PINHOLE": [500.0, 320.5, 240.5],
    "PINHOLE": [500.0, 505.0, 320.5, 240.5],
    "SIMPLE_RADIAL": [500.0, 320.5, 240.5, -0.05],
    "RADIAL": [500.0, 320.5, 240.5, -0.05, 0.01],
    "OPENCV": [500.0, 505.0, 320.5, 240.5, -0.05, 0.01, 0.001, -0.002],
    "FULL_OPENCV": [500.0, 505.0, 320.5, 240.5, -0.05, 0.01, 0.001, -0.002, 0.001, 0.02, -0.01, 0.002],
}


class _Sensor:
    def __init__(self, model, params):
        from pyarchinit_photogrammetry.core.camera import Sensor

        self.s = Sensor(key=0, label="s", width=640, height=480, model=model, params=list(params))

    def __getattr__(self, name):
        return getattr(self.s, name)


class _Cam:
    def __init__(self, transform):
        self.key, self.transform = 0, transform

    @property
    def cam_from_world(self):
        return np.linalg.inv(self.transform)


class _Chunk:
    def __init__(self, sensor):
        self._sensor = sensor

    def sensor(self, cam):
        return self._sensor.s


def _camera_and_points(rng, n=20000):
    from scipy.spatial.transform import Rotation

    world_from_cam = np.eye(4)
    world_from_cam[:3, :3] = Rotation.from_euler("xyz", [178.0, 3.0, 25.0], degrees=True).as_matrix()
    world_from_cam[:3, 3] = [10.0, -4.0, 30.0]
    pts = np.column_stack([rng.uniform(-20, 40, n), rng.uniform(-30, 20, n), rng.uniform(-2, 3, n)])
    return _Cam(world_from_cam), pts


def test_projection_of_every_model(monkeypatch, rng):
    cam, pts = _camera_and_points(rng)
    for model, params in MODELS.items():
        chunk = _Chunk(_Sensor(model, params))
        (pa, da, oa), (pb, db, ob) = both(monkeypatch, lambda: visibility.project_valid(chunk, cam, pts))
        assert oa.sum() > 1000, model
        assert np.array_equal(oa, ob), model
        assert np.allclose(da, db, rtol=0, atol=1e-9)
        assert np.array_equal(np.isnan(pa), np.isnan(pb)), model
        assert np.nanmax(np.abs(pa - pb)) < 1e-8, model


def test_orthomosaic_scores(monkeypatch, rng):
    cam, pts = _camera_and_points(rng)
    up = np.array([0.0, 0.0, 1.0])
    centres = {0: cam.transform[:3, 3]}
    for model in ("SIMPLE_RADIAL", "OPENCV"):
        chunk = _Chunk(_Sensor(model, MODELS[model]))
        (pa, sa), (pb, sb) = both(monkeypatch, lambda: ortho._scores(chunk, cam, pts, centres, up, 0.34))
        assert np.array_equal(np.isfinite(sa), np.isfinite(sb)) and np.isfinite(sa).sum() > 1000
        ok = np.isfinite(sa)
        assert np.max(np.abs(sa[ok] - sb[ok]) / np.abs(sa[ok])) < 1e-6
        assert np.max(np.abs(pa[ok] - pb[ok])) < 1e-8


def test_confidence_and_orthomosaic_of_the_trench(processed, tmp_path, monkeypatch):
    import rasterio

    import pyarchinit_photogrammetry as ppg

    doc, ch, _ = processed
    pts = ch.point_cloud.xyz
    a, b = both(monkeypatch, lambda: confidence.depth_map_support(ch, pts))
    assert a.max() >= 5 and np.mean(a == b) > 0.9999 and np.max(np.abs(a.astype(int) - b)) <= 1

    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    copy = ppg.Document.open(d / doc.path.name).chunk

    def build(name):
        path = copy.build_orthomosaic(resolution=0.005)
        with rasterio.open(path) as src:
            return src.read()

    ra, rb = both(monkeypatch, lambda: build("o"))
    assert ra.shape == rb.shape
    same = np.all(ra == rb, axis=0)
    assert same.mean() > 0.9999, same.mean()
