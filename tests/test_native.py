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


def test_best_photos_of_every_cell(monkeypatch, rng):
    from scipy.spatial.transform import Rotation

    class Ctx:
        pass

    n = 30000
    pts = np.column_stack([rng.uniform(-20, 40, n), rng.uniform(-30, 20, n), rng.uniform(-2, 3, n)])
    sensors, cams = {}, []
    for j in range(7):
        world_from_cam = np.eye(4)
        world_from_cam[:3, :3] = Rotation.from_euler("xyz", [178.0 - 4 * j, 3.0 + j, 25.0 * j], degrees=True).as_matrix()
        world_from_cam[:3, 3] = [10.0 + 3 * j, -4.0 + 2 * j, 30.0 + j]
        cam = _Cam(world_from_cam)
        cam.key = j
        model = ("OPENCV", "SIMPLE_RADIAL", "FULL_OPENCV")[j % 3]
        sensors[j] = _Sensor(model, MODELS[model]).s
        cams.append(cam)
    ctx = Ctx()
    ctx.chunk = type("C", (), {"sensor": lambda self, cam: sensors[cam.key]})()
    ctx.centres = {c.key: c.transform[:3, 3] for c in cams}
    ctx.up_int, ctx.min_cos = np.array([0.0, 0.0, 1.0]), 0.34
    (ia, sa, pa), (ib, sb, pb) = both(monkeypatch, lambda: ortho._candidates(ctx, pts, cams, 4))
    assert (ia[0] >= 0).sum() > 5000 and (ia[1] >= 0).sum() > 1000
    assert np.array_equal(ia, ib)
    ok = ia >= 0
    assert np.allclose(sa[ok], sb[ok], rtol=1e-6)
    assert np.all(sa[:-1][ok[1:]] >= sa[1:][ok[1:]])  # best first
    assert np.allclose(pa[ok], pb[ok], atol=1e-3)


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


def _plane_views(width=320, height=240, focal=300.0):
    """A textured plane z = 2 + 0.3 x seen by a camera at the origin and four around it."""
    rng = np.random.default_rng(0)
    coef = rng.normal(size=(40, 4))
    k = (focal, focal, width / 2 - 0.5, height / 2 - 0.5)
    n_w, c_w = np.array([-0.3, 0.0, 1.0]), 2.0

    def texture(x, y):
        v = sum(np.sin(a * 6 * x + b * 6 * y + c * 3) * 0.5 ** abs(d) for a, b, c, d in coef)
        return (v - v.min()) / np.ptp(v)

    def render(rot, t):
        ys, xs = np.mgrid[0:height, 0:width].astype(float)
        rays = np.stack([(xs - k[2]) / focal, (ys - k[3]) / focal, np.ones_like(xs)], -1) @ rot
        centre = -rot.T @ t
        pts = centre + rays * ((c_w - n_w @ centre) / (rays @ n_w))[..., None]
        return texture(pts[..., 0], pts[..., 1]).astype(np.float32), (pts @ rot.T + t)[..., 2].astype(np.float32)

    def turn(ay):
        return np.array([[np.cos(ay), 0, np.sin(ay)], [0, 1, 0], [-np.sin(ay), 0, np.cos(ay)]])

    ref, truth = render(np.eye(3), np.zeros(3))
    sources = []
    for dx, dy, ay in ((0.25, 0, -0.08), (-0.25, 0, 0.08), (0, 0.2, 0.0), (0, -0.2, 0.0)):
        rot = turn(ay)
        t = -rot @ np.array([dx, dy, 0.0])
        sources.append((render(rot, t)[0], k, rot, t))
    return ref, truth, k, sources, -n_w / np.linalg.norm(n_w)


def test_patchmatch_finds_a_slanted_plane():
    ref, truth, k, sources, normal = _plane_views()
    depth, normals, cost = ppg_core.patchmatch_depth(ref, k, sources, (1.0, 4.0), iterations=4, max_cost=0.5)
    valid = depth > 0
    err = np.abs(depth[valid] - truth[valid]) / truth[valid]
    assert valid.mean() > 0.9 and np.median(err) < 0.005 and np.percentile(err, 90) < 0.02
    angle = np.degrees(np.arccos(np.clip(normals[valid] @ normal, -1, 1)))
    assert np.median(angle) < 15 and (cost[valid] <= 0.5).all()
    # started from the right depths the planes stay there; a wrong start is corrected
    start = truth * np.where(np.arange(truth.size).reshape(truth.shape) % 7 == 0, 1.3, 1.0).astype(np.float32)
    d2, _, _ = ppg_core.patchmatch_depth(ref, k, sources, (1.0, 4.0), init_depth=start, iterations=2)
    ok = d2 > 0
    assert np.median(np.abs(d2[ok] - truth[ok]) / truth[ok]) < 0.005


def test_patchmatch_depth_maps_of_the_trench(processed, scene, tmp_path):
    """The multi-view depth maps give a DEM at least as good as the two-view ones (on the
    synthetic trench about three times better), over the surface that is there."""
    import rasterio

    import pyarchinit_photogrammetry as ppg

    doc, _, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    copy = ppg.Document.open(d / doc.path.name).chunk
    copy.build_depth_maps(quality="high", backend="patchmatch")
    assert copy.meta["dense"]["backend"] == "patchmatch"
    copy.build_point_cloud()
    copy.build_dem()
    with rasterio.open(copy.dem_path) as src:
        dem = src.read(1)
        rows, cols = np.nonzero(dem != src.nodata)
        sel = np.random.default_rng(1).choice(len(rows), 4000, replace=False)
        xs, ys = rasterio.transform.xy(src.transform, rows[sel], cols[sel])
    err = dem[rows[sel], cols[sel]] - scene.world_height(np.array(xs), np.array(ys))
    assert abs(np.median(err)) < 0.001
    assert 1.4826 * np.median(np.abs(err - np.median(err))) < 0.0015
    assert np.percentile(np.abs(err), 90) < 0.003


def test_auto_takes_patchmatch_for_surveys_from_the_air(processed, monkeypatch):
    """Two-view SGBM for the trench (no GPS), PatchMatch when the GPS of the photos spreads over a
    site; an NVIDIA GPU or OpenMVS keep their precedence."""
    from pyarchinit_photogrammetry.engine import dense

    if not dense.patchmatch_available():
        pytest.skip("native core without PatchMatch")
    _doc, ch, _report = processed
    monkeypatch.setattr(dense.shutil, "which", lambda name: None)  # no OpenMVS here
    assert not dense.aerial_survey(ch) and dense.choose_backend("auto", ch) == "cpu"
    saved = [(c.reference.location, c.reference.accuracy, c.reference.enabled, c.meta.get("reference_crs"))
             for c in ch.cameras]
    try:
        for i, cam in enumerate(ch.aligned_cameras):  # a flight over 200 m, GPS known to 5 m
            cam.reference.location = [1000.0 + 10.0 * (i % 5) * 4, 2000.0 + 10.0 * (i // 5) * 4, 100.0]
            cam.reference.accuracy, cam.reference.enabled = [5.0, 5.0, 10.0], True
            cam.meta["reference_crs"] = ch.crs
        assert dense.aerial_survey(ch) and dense.choose_backend("auto", ch) == "patchmatch"
        assert dense.choose_backend("cpu", ch) == "cpu"
    finally:
        for cam, (loc, acc, enabled, ref_crs) in zip(ch.cameras, saved):
            cam.reference.location, cam.reference.accuracy, cam.reference.enabled = loc, acc, enabled
            if ref_crs is None:
                cam.meta.pop("reference_crs", None)
            else:
                cam.meta["reference_crs"] = ref_crs


def test_gpu_matching_agrees_with_the_brute_force(rng):
    """The GPU matcher (wgpu) picks the matches COLMAP's rules pick, checked against numpy."""
    if _native.gpu_device("auto") is None:
        pytest.skip("no GPU (wgpu adapter) on this computer")

    def descriptors(n):
        v = rng.random((n, 128), np.float32) ** 2
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        return np.clip(np.round(v * 512), 0, 255).astype(np.uint8)

    def brute_force(a, b, max_ratio, max_distance, cross):
        d = a.astype(np.int64) @ b.astype(np.int64).T
        dist = lambda s: np.arccos(np.minimum(s / 512.0 ** 2, 1.0))  # noqa: E731

        def bests(m):
            order = np.argsort(-m, axis=1)[:, :2]
            rows = np.arange(len(m))
            best, second = m[rows, order[:, 0]], m[rows, order[:, 1]]
            return best, order[:, 0], (dist(best) <= max_distance) & (dist(best) < max_ratio * dist(second)) & (best > 0)

        b1, j1, ok1 = bests(d)
        if not cross:
            return {(int(i), int(j1[i])) for i in np.flatnonzero(ok1)}
        _b2, i2, ok2 = bests(d.T)
        return {(int(i), int(j1[i])) for i in np.flatnonzero(ok1) if i2[j1[i]] == i and ok2[j1[i]]}

    a, b = descriptors(2000), descriptors(1700)
    b[:900] = np.clip(a[:900].astype(np.int32) + rng.integers(-6, 7, (900, 128)), 0, 255).astype(np.uint8)
    for cross in (True, False):
        got = {(int(i), int(j)) for i, j in _native.gpu_match(a, b, 0.8, 0.7, cross)}
        want = brute_force(a, b, 0.8, 0.7, cross)
        assert got == want and len(got) >= 850
    # cached descriptors (keys) give the same answer, and a stricter distance fewer matches
    again = {(int(i), int(j)) for i, j in _native.gpu_match(a, b, 0.8, 0.7, True, a_key=11, b_key=12)}
    assert again == brute_force(a, b, 0.8, 0.7, True)
    assert len(_native.gpu_match(a, b, 0.8, 0.02, True, a_key=11, b_key=12)) < len(again)  # the noise is more
    assert len(_native.gpu_match(a[:0], b, 0.8, 0.7, True)) == 0
    _native.gpu_forget()


def test_gpu_patchmatch_agrees_with_the_cpu_kernel():
    """The PatchMatch kernel on the GPU (wgpu) finds the slanted plane the CPU kernel finds."""
    if _native.gpu_device("auto") is None or not hasattr(ppg_core, "gpu_patchmatch_depth"):
        pytest.skip("no GPU (wgpu adapter) or a native core without the GPU kernels")
    ref, truth, k, sources, normal = _plane_views()
    depth, normals, cost = ppg_core.gpu_patchmatch_depth(ref, k, sources, (1.0, 4.0), iterations=4, max_cost=0.5)
    valid = depth > 0
    err = np.abs(depth[valid] - truth[valid]) / truth[valid]
    assert valid.mean() > 0.9 and np.median(err) < 0.005 and np.percentile(err, 90) < 0.02
    angle = np.degrees(np.arccos(np.clip(normals[valid] @ normal, -1, 1)))
    assert np.median(angle) < 15 and (cost[valid] <= 0.5).all()
    # the two kernels agree where both have a depth
    d_cpu, _, _ = ppg_core.patchmatch_depth(ref, k, sources, (1.0, 4.0), iterations=4, max_cost=0.5)
    both = valid & (d_cpu > 0)
    assert both.mean() > 0.85 and np.median(np.abs(depth[both] - d_cpu[both]) / d_cpu[both]) < 0.005
    # started from the right depths the planes stay there; a wrong start is corrected
    start = truth * np.where(np.arange(truth.size).reshape(truth.shape) % 7 == 0, 1.3, 1.0).astype(np.float32)
    d2, _, _ = ppg_core.gpu_patchmatch_depth(ref, k, sources, (1.0, 4.0), init_depth=start, iterations=2)
    ok = d2 > 0
    assert np.median(np.abs(d2[ok] - truth[ok]) / truth[ok]) < 0.005


def test_gpu_sift_finds_colmaps_points(scene_dir, tmp_path):
    """The SIFT kernel of the GPU, on the photo as COLMAP sees it (its grey, its reduction),
    finds the key points COLMAP's extractor finds, with the same descriptors, in a fixed order."""
    import sqlite3

    import cv2
    import pycolmap

    from pyarchinit_photogrammetry.engine import sfm

    device = _native.gpu_device("auto")
    if device is None or _native.kernel("gpu_sift") is None:
        pytest.skip("no GPU (wgpu adapter) or no GPU SIFT in the native core")
    photo = scene_dir / "photos" / "IMG_0000.jpg"
    (tmp_path / "im").mkdir()
    shutil.copy(photo, tmp_path / "im" / photo.name)
    ext = pycolmap.FeatureExtractionOptions()
    ext.max_image_size = 600  # the 1200-pixel photo reduced by two, as quality "high"
    ext.sift.max_num_features = 40000
    reader = pycolmap.ImageReaderOptions()
    reader.camera_model = "SIMPLE_PINHOLE"
    pycolmap.extract_features(str(tmp_path / "db.db"), str(tmp_path / "im"), reader_options=reader, extraction_options=ext)
    con = sqlite3.connect(str(tmp_path / "db.db"))
    r, c, data = con.execute("select rows, cols, data from keypoints").fetchone()
    theirs = np.frombuffer(data, np.float32).reshape(r, c)  # x, y, a11, a12, a21, a22
    r, c, data = con.execute("select rows, cols, data from descriptors").fetchone()
    their_desc = np.frombuffer(data, np.uint8).reshape(r, c)
    con.close()
    g = cv2.transform(cv2.imread(str(photo), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION), sfm._REC709_BGR)
    small = sfm._rescale_as_colmap(g, 600, 450)
    raw, desc = _native.gpu_sift(small.astype(np.float32) / 255.0, max_features=40000, device=device)
    raw, desc = np.asarray(raw, np.float32), np.asarray(desc, np.uint8)
    kp = raw.copy()
    kp[:, :2] = (kp[:, :2] + 0.5) * 2.0  # COLMAP's pixel centre, back to the full photo
    kp[:, 2] *= 2.0
    scale = np.hypot(theirs[:, 2], theirs[:, 4])
    d = np.hypot(theirs[:, None, 0] - kp[None, :, 0], theirs[:, None, 1] - kp[None, :, 1])
    j = d.argmin(axis=1)
    twins = (d.min(axis=1) < 0.5) & (np.abs(np.log(kp[j, 2] / scale)) < 0.1)
    assert twins.mean() > 0.95, f"{twins.mean():.3f} of COLMAP's points found"
    assert abs(len(kp) - len(theirs)) < 0.05 * len(theirs)
    a = desc[j[twins]].astype(float)
    b = their_desc[twins].astype(float)
    similarity = np.einsum("ij,ij->i", a, b) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1))
    assert np.median(similarity) > 0.99
    again, desc_again = _native.gpu_sift(small.astype(np.float32) / 255.0, max_features=40000, device=device)
    assert np.array_equal(np.asarray(again), raw) and np.array_equal(np.asarray(desc_again), desc)
    assert np.all(np.diff(raw[:, 1]) >= 0)  # by row, then column: the same order every time
