"""Our own bundle adjustment: a block recovered from wrong poses, the scale from bars, the bias of
the GNSS positions, the rolling shutter of a moving camera; the same by the native solver of the
core (API 12), which gives the residuals of the reference and is chosen, or not, as asked."""
from __future__ import annotations

import logging

import numpy as np
import pytest

from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.core.progress import Cancelled, Progress
from pyarchinit_photogrammetry.engine import bundle

pycolmap = pytest.importorskip("pycolmap")

W, H, F = 1200, 900, 1000.0
NATIVE_MISSING = "the native core with the bundle adjustment (API 12) is not installed"
needs_native = pytest.mark.skipif(not _native.has("bundle"), reason=NATIVE_MISSING)
SOLVERS = ["scipy", pytest.param("native", marks=needs_native)]


def _look_down(yaw: float) -> np.ndarray:
    """world -> camera of a camera looking down (nadir), turned by ``yaw``."""
    down = np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]])  # x east, y south in the image, z down
    c, s = np.cos(yaw), np.sin(yaw)
    return np.array([[c, s, 0], [-s, c, 0], [0, 0, 1.0]]) @ down


def _block(rng, n_points=600, rolling=None):
    """A drone block: 3 strips of 6 photos 30 m above rolling ground, points and markers seen in
    the photos (COLMAP pixels, exact; ``rolling``: velocity per photo, metres per frame read)."""
    rotations, centres = [], []
    for row, y in enumerate((-12.0, 0.0, 12.0)):
        for x in np.linspace(-20, 20, 6):
            rotations.append(_look_down(0.02 * row))
            centres.append([x, y, 30.0])
    rotations, centres = np.array(rotations), np.array(centres)
    xy = rng.uniform([-25, -18], [25, 18], (n_points, 2))
    points = np.column_stack([xy, 0.6 * np.sin(xy[:, 0] / 7) + 0.4 * np.cos(xy[:, 1] / 5)])
    markers = np.array([[-15.0, -10, 0.1], [15, -10, 0.2], [15, 10, -0.1], [-15, 10, 0.3], [0, 0, 0.0]])
    cam = pycolmap.Camera(model="OPENCV", width=W, height=H, params=[F, F, W / 2, H / 2, -0.02, 0.01, 0, 0])
    vel = rolling if rolling is not None else np.zeros((len(centres), 3))

    def observe(world):
        img, idx, out = [], [], []
        for i, (r, c) in enumerate(zip(rotations, centres)):
            tau = np.zeros(len(world))
            for _ in range(4):  # the row read depends on where the point falls: a few rounds
                cam_pts = (world - c - vel[i] * tau[:, None]) @ r.T
                px = np.asarray(cam.img_from_cam(cam_pts))[:, :2]
                tau = px[:, 1] / H - 0.5
            inside = (px[:, 0] > 5) & (px[:, 0] < W - 5) & (px[:, 1] > 5) & (px[:, 1] < H - 5) & (cam_pts[:, 2] > 0)
            img += [i] * int(inside.sum())
            idx += list(np.flatnonzero(inside))
            out.append(px[inside])
        return np.array(img), np.array(idx), np.vstack(out)

    oi, op, oxy = observe(points)
    mi, mm, mxy = observe(markers)
    pb = bundle.Problem(rotations.copy(), centres.copy(), np.zeros(len(centres), int),
                        [("OPENCV", [F, F, W / 2, H / 2, -0.02, 0.01, 0, 0], W, H)], points.copy(), oi, op, oxy,
                        markers=markers.copy(), mobs_image=mi, mobs_marker=mm, mobs_xy=mxy,
                        free_intrinsics=[[0, 1, 4, 5]])
    return pb, {"rotations": rotations, "centres": centres, "points": points, "markers": markers}


def _gcps(truth, sigma=0.002, which=(0, 1, 2, 3)):
    return [(m, truth["markers"][m].copy(), np.full(3, sigma)) for m in which]


def _perturb(pb, rng, centre=0.3, angle=0.01, point=0.2):
    from scipy.spatial.transform import Rotation

    pb.centres = pb.centres + rng.normal(0, centre, pb.centres.shape)
    pb.rotations = Rotation.from_rotvec(rng.normal(0, angle, (len(pb.centres), 3))).as_matrix() @ pb.rotations
    pb.points = pb.points + rng.normal(0, point, pb.points.shape)
    pb.markers = pb.markers + rng.normal(0, point, pb.markers.shape)


@pytest.mark.parametrize("solver", SOLVERS)
def test_a_block_comes_back_to_its_control_points(solver):
    rng = np.random.default_rng(1)
    pb, truth = _block(rng)
    pb.gcps = _gcps(truth)
    _perturb(pb, rng)
    pb.cameras[0] = ("OPENCV", [F * 1.01, F * 1.01, W / 2, H / 2, 0, 0, 0, 0], W, H)  # the calibration too
    res = bundle.adjust(pb, solver=solver)
    assert res.solver == solver and res.fallback is None
    assert res.success and res.reprojection_before > 10 and res.reprojection_after < 0.01
    assert np.abs(res.centres - truth["centres"]).max() < 0.005
    assert np.abs(res.points - truth["points"]).max() < 0.01
    assert abs(res.camera_params[0][0] - F) < 0.5 and abs(res.camera_params[0][4] + 0.02) < 1e-3
    assert np.linalg.norm(res.markers[4] - truth["markers"][4]) < 0.005  # a marker that is not a control point
    # what the result says of the solve
    assert res.cost < 1e-6 and np.isfinite(res.gradient_max) and res.seconds > 0 and res.iterations > 0
    if solver == "native":
        assert res.linear_solver == "dense" and res.termination in ("function", "gradient", "parameters")
    else:
        assert res.linear_solver is None and res.termination in ("function", "gradient", "parameters")


@pytest.mark.parametrize("solver", SOLVERS)
def test_scale_bars_give_the_size_of_a_free_block(solver):
    rng = np.random.default_rng(2)
    pb, truth = _block(rng)
    for name in ("centres", "points", "markers"):  # the block 7% too big
        setattr(pb, name, getattr(pb, name) * 1.07)
    m = truth["markers"]
    pb.bars = [(0, 2, float(np.linalg.norm(m[0] - m[2])), 0.001), (1, 3, float(np.linalg.norm(m[1] - m[3])), 0.001)]
    pb.fixed_images = {0}  # no control point: the first photo holds the place
    res = bundle.adjust(pb, solver=solver)
    assert res.solver == solver
    got = np.linalg.norm(res.markers[0] - res.markers[2])
    assert abs(got - np.linalg.norm(m[0] - m[2])) < 0.002
    d_true = np.linalg.norm(truth["points"][10] - truth["points"][500])
    assert abs(np.linalg.norm(res.points[10] - res.points[500]) - d_true) < 0.005 * d_true


@pytest.mark.parametrize("solver", SOLVERS)
def test_the_bias_of_the_gnss_positions(solver):
    """A bias common to the photos of a flight, found with the block. The camera is calibrated:
    over flat ground a nadir block cannot tell its height from the focal length, so a free
    calibration and a free vertical bias would trade one for the other."""
    rng = np.random.default_rng(3)
    pb, truth = _block(rng)
    pb.free_intrinsics = [[]]
    bias = np.array([0.35, -0.2, 0.6])
    pb.gcps = _gcps(truth, which=(0, 1, 2))
    pb.gnss = [(i, c + bias + rng.normal(0, 0.01, 3), np.full(3, 0.02), 1) for i, c in enumerate(truth["centres"])]
    pb.gnss_bias = True
    _perturb(pb, rng, centre=0.1, point=0.05)
    res = bundle.adjust(pb, solver=solver)
    assert res.solver == solver
    assert np.linalg.norm(res.bias[1] - bias) < 0.02
    assert np.abs(res.centres - truth["centres"]).max() < 0.03  # each GNSS position has 1 cm of noise
    plain = bundle.Problem(**{**pb.__dict__, "gnss_bias": False})  # without the bias the block is pulled away
    assert np.abs(bundle.adjust(plain, solver=solver).centres - truth["centres"]).max() > 0.05


@pytest.mark.parametrize("solver", SOLVERS)
def test_rolling_shutter_of_a_moving_drone(solver):
    rng = np.random.default_rng(4)
    # 0.4 m along the strips while a frame is read, the strips flown one way and back
    rolling = np.array([[0.4 if (i // 6) % 2 == 0 else -0.4, 0.0, 0.0] for i in range(18)])
    pb, truth = _block(rng, rolling=rolling)
    pb.gcps = _gcps(truth)
    still = bundle.adjust(bundle.Problem(**pb.__dict__), solver=solver)
    pb.rolling_shutter = True
    moving = bundle.adjust(pb, solver=solver)
    assert still.solver == moving.solver == solver
    assert still.reprojection_after > 1.0 and moving.reprojection_after < 0.05
    assert np.abs(moving.rolling_shutter[:, :3] - rolling).max() < 0.02
    # the prior on the velocities leaves this problem a weak mode along the strips (the position of a camera
    # against its rolling-shutter velocity): its optimum has the centres 6 cm from the truth, where the native
    # solver converges (cost 1.41930, gradient 2e-5); scipy stops at its 100 evaluations 5 mm from the truth
    # with a higher cost (1.42049, gradient 0.3), and is still 1 cm away and above after 1000
    assert np.abs(moving.centres - truth["centres"]).max() < (0.1 if solver == "native" else 0.01)
    if solver == "native":
        assert moving.cost < 1.42 and moving.gradient_max < 1e-3


def _full_block(rng):
    """The block with every kind of residual: measured markers, three control points, two bars, the
    GNSS position of every photo with a bias per flight (two flights), the rolling shutter, a photo
    held, the calibration 1 % off; perturbed."""
    rolling = np.array([[0.4 if (i // 6) % 2 == 0 else -0.4, 0.0, 0.0] for i in range(18)])
    pb, truth = _block(rng, rolling=rolling)
    m = truth["markers"]
    pb.gcps = _gcps(truth, which=(0, 1, 2))
    pb.bars = [(0, 2, float(np.linalg.norm(m[0] - m[2])), 0.001), (1, 3, float(np.linalg.norm(m[1] - m[3])), 0.001)]
    bias = {1: np.array([0.35, -0.2, 0.6]), 2: np.array([-0.1, 0.25, -0.3])}
    pb.gnss = [(i, c + bias[1 + i // 9] + rng.normal(0, 0.01, 3), np.full(3, 0.02), 1 + i // 9)
               for i, c in enumerate(truth["centres"])]
    pb.gnss_bias = True
    pb.rolling_shutter = True
    pb.fixed_images = {3}
    _perturb(pb, rng)
    pb.cameras[0] = ("OPENCV", [F * 1.01, F * 1.01, W / 2, H / 2, 0, 0, 0, 0], W, H)
    return pb, truth


@needs_native
def test_the_native_residuals_are_those_of_the_reference():
    """At the start of a problem of every kind of residual the native evaluation gives the residual
    vector of the reference, its cost, the Jacobian on the structure of the reference, and the
    gradient of the cost; a robust loss changes the cost by scipy's formula, not the residuals."""
    rng = np.random.default_rng(5)
    pb, _truth = _full_block(rng)
    model = bundle._Model(pb)
    want = model.residuals(model.start())
    ev = bundle.evaluate(pb)
    assert ev.residuals.shape == want.shape and np.abs(ev.residuals - want).max() < 1e-9
    assert abs(ev.cost - 0.5 * np.sum(want ** 2)) <= 1e-12 * ev.cost
    structure = model.sparsity().tocoo()
    jac = ev.jacobian.tocoo()
    assert jac.shape == structure.shape == (len(want), model.lay.size)
    assert set(zip(jac.row.tolist(), jac.col.tolist())) == set(zip(structure.row.tolist(), structure.col.tolist()))
    assert np.abs(ev.gradient - ev.jacobian.T @ ev.residuals).max() <= 1e-9 * np.abs(ev.gradient).max()
    robust = bundle.evaluate(pb, loss="huber", loss_scale=2.0)
    assert np.array_equal(robust.residuals, ev.residuals)
    tie = 2 * len(pb.obs_image)  # the loss is on the tie points, the other rows stay squared
    s = want[:tie] ** 2
    rho = np.where(s <= 4.0, s, 4.0 * np.sqrt(s) - 4.0)
    assert abs(robust.cost - 0.5 * (rho.sum() + np.sum(want[tie:] ** 2))) <= 1e-12 * robust.cost


def test_the_solver_is_chosen_and_said(monkeypatch, caplog):
    """``solver="native"`` without the native solver falls back on scipy, says why and warns once;
    ``"auto"`` is scipy until the gate; a word that is not a solver, or a robust loss on the scipy
    path with other residuals than tie points, is refused."""
    rng = np.random.default_rng(6)
    pb, truth = _block(rng, n_points=40)
    tie_only = {"fixed_images": {0, 5}, "markers": np.zeros((0, 3)), "mobs_image": np.zeros(0, int),
                "mobs_marker": np.zeros(0, int), "mobs_xy": np.zeros((0, 2))}
    plain = bundle.Problem(**{**pb.__dict__, **tie_only})
    pb.gcps = _gcps(truth)
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    monkeypatch.delenv("PPG_NATIVE", raising=False)
    monkeypatch.setattr(_native, "has", lambda feature: False)
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        res = bundle.adjust(pb, max_iterations=2, solver="native")
    assert res.solver == "scipy" and "API 12" in res.fallback
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "API 12" in r.getMessage()]
    assert len(warnings) == 1
    res = bundle.adjust(pb, max_iterations=2, solver="auto")
    assert res.solver == "scipy" and res.fallback is None and res.linear_solver is None
    assert isinstance(res.termination, str) and np.isfinite(res.cost) and np.isfinite(res.gradient_max)
    assert res.seconds > 0
    with pytest.raises(ValueError, match="auto, native or scipy"):
        bundle.adjust(pb, solver="ceres")
    with pytest.raises(ValueError, match="control point"):  # scipy would weigh the control points by the loss too
        bundle.adjust(pb, solver="scipy", loss="huber")
    with pytest.raises(ValueError, match="none, huber or cauchy"):
        bundle.adjust(plain, solver="scipy", loss="tukey")
    robust = bundle.adjust(plain, max_iterations=2, solver="scipy", loss="huber", loss_scale=2.0)
    assert robust.solver == "scipy" and np.isfinite(robust.cost)  # tie points only: scipy's loss is the same
    monkeypatch.setenv("PPG_NATIVE", "0")
    assert "PPG_NATIVE=0" in bundle.native_ready(pb)


def test_the_fallback_keeps_the_squared_loss_on_a_block_with_references(monkeypatch, caplog):
    """``solver="native", loss="huber"`` on a block with control points where the native solver cannot run:
    scipy adjusts it with the squared loss, the result and the one warning say so, nothing is raised (a
    fallback is never an error); asked for by name, scipy still refuses a robust loss on such a block."""
    rng = np.random.default_rng(11)
    pb, truth = _block(rng, n_points=40)
    pb.gcps = _gcps(truth)
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    monkeypatch.setenv("PPG_NATIVE", "0")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        res = bundle.adjust(pb, max_iterations=2, solver="native", loss="huber", loss_scale=2.0)
    assert res.solver == "scipy" and "PPG_NATIVE=0" in res.fallback and "squared loss" in res.fallback
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "PPG_NATIVE=0" in warnings[0] and "squared loss" in warnings[0]
    assert res.cost == bundle.adjust(pb, max_iterations=2, solver="scipy").cost  # the same squared-loss solve
    with pytest.raises(ValueError, match="control point"):
        bundle.adjust(pb, max_iterations=2, solver="scipy", loss="huber")


@needs_native
def test_the_native_solver_is_taken_when_asked_or_set(monkeypatch, caplog):
    rng = np.random.default_rng(7)
    pb, truth = _block(rng, n_points=40)
    pb.gcps = _gcps(truth)
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    assert bundle.native_ready(pb) is None
    assert bundle.adjust(pb, max_iterations=2, solver="auto").solver == "scipy"
    monkeypatch.setenv("PPG_ENGINE", "native")
    res = bundle.adjust(pb, solver="auto")
    assert res.solver == "native" and res.fallback is None and res.linear_solver == "dense"
    assert bundle.adjust(pb, max_iterations=2, solver="scipy").solver == "scipy"
    fisheye = bundle.Problem(**{**pb.__dict__, "cameras": [("OPENCV_FISHEYE", [F, F, W / 2, H / 2, 0, 0, 0, 0], W, H)]})
    assert "OPENCV_FISHEYE" in bundle.native_ready(fisheye)
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        res = bundle.adjust(fisheye, max_iterations=2, solver="native")
    assert res.solver == "scipy" and "OPENCV_FISHEYE" in res.fallback
    assert any("OPENCV_FISHEYE" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


@needs_native
def test_a_progress_that_raises_stops_the_native_solve():
    class Stop(Exception):
        pass

    rng = np.random.default_rng(8)
    pb, truth = _block(rng)
    pb.gcps = _gcps(truth)
    _perturb(pb, rng)
    reports = []

    def stop_at_the_second(fraction, message):
        if "iteration" in message:
            reports.append(message)
            if len(reports) == 2:
                raise Stop(message)

    with pytest.raises(Stop, match="iteration 2"):
        bundle.adjust(pb, solver="native", progress=stop_at_the_second)
    assert len(reports) == 2  # the solve stopped there
    # the cancel of a Progress: its next report raises Cancelled, as in every step
    reports.clear()
    progress = Progress()

    def cancel_at_the_second(fraction, message):
        if "iteration" in message:
            reports.append(message)
            if len(reports) == 2:
                progress.cancel()

    progress.callback = cancel_at_the_second
    with pytest.raises(Cancelled):
        bundle.adjust(pb, solver="native", progress=progress)
    assert len(reports) == 2


@pytest.mark.parametrize("solver", SOLVERS)
def test_a_point_seen_once_is_held_by_both_solvers(solver):
    """A tie point with one observation cannot be adjusted: the native solver holds it, scipy would
    move it along its ray; both leave it where it starts and adjust the rest."""
    rng = np.random.default_rng(9)
    pb, truth = _block(rng)
    pb.gcps = _gcps(truth)
    _perturb(pb, rng)
    cam = pycolmap.Camera(model="OPENCV", width=W, height=H, params=[F, F, W / 2, H / 2, -0.02, 0.01, 0, 0])
    lone = np.array([[0.5, 0.5, 0.0]])
    px = np.asarray(cam.img_from_cam((lone - truth["centres"][8]) @ truth["rotations"][8].T))[:, :2]
    pb.points = np.vstack([pb.points, lone + 0.3])
    pb.obs_image = np.append(pb.obs_image, 8)
    pb.obs_point = np.append(pb.obs_point, len(pb.points) - 1)
    pb.obs_xy = np.vstack([pb.obs_xy, px])
    res = bundle.adjust(pb, solver=solver)
    assert res.solver == solver and res.points.shape == pb.points.shape
    assert np.array_equal(res.points[-1], pb.points[-1])
    assert np.abs(res.points[:-1] - truth["points"]).max() < 0.01
    assert np.abs(res.centres - truth["centres"]).max() < 0.005 and res.reprojection_after < 0.01


@needs_native
def test_the_problem_at_a_result_evaluates_to_its_cost():
    rng = np.random.default_rng(10)
    pb, truth = _block(rng)
    pb.gcps = _gcps(truth)
    _perturb(pb, rng)
    res = bundle.adjust(pb, solver="native")
    assert res.termination in ("function", "gradient", "parameters") and res.iterations > 0
    at = bundle.problem_at(pb, res)
    assert at.gcps is pb.gcps and at.free_intrinsics is pb.free_intrinsics and at.obs_xy is pb.obs_xy
    assert np.array_equal(at.centres, res.centres) and np.array_equal(at.rotations, res.rotations)
    assert at.cameras[0][1] == [float(v) for v in res.camera_params[0]] and at.cameras[0][2:] == pb.cameras[0][2:]
    assert np.array_equal(at.points, res.points) and np.array_equal(at.markers, res.markers)
    assert np.array_equal(pb.centres, bundle._Model(pb).unpack(bundle._Model(pb).start())[1])  # pb untouched
    ev = bundle.evaluate(at)
    assert abs(ev.cost - res.cost) <= 1e-9 * max(1.0, res.cost)
    assert abs(np.abs(ev.gradient).max() - res.gradient_max) <= 1e-9 * max(1.0, res.gradient_max)


def test_own_adjustment_of_the_trench(processed, tmp_path):
    """``optimize_cameras(engine="ppg")`` on the synthetic trench: control points pulled by their
    accuracy, two of them left as check points, the metric thresholds of the pipeline kept."""
    import shutil

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.markers import gcp

    doc, _, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    copy = ppg.Document.open(d / doc.path.name)
    ch = copy.chunk
    for m in ch.markers[:2]:
        m.checkpoint = True
    gcp.update_transform(ch)
    err = ch.optimize_cameras(engine="ppg")
    rep = ch.meta["sfm"]["optimize"]
    assert rep["engine"] == "ppg" and rep["control_points"] >= 4 and err < 0.5
    assert rep["after"]["control"] < 0.002 and rep["after"]["check"] < 0.003
    # the photos of the trench have a global shutter: asked for, the velocities stay close to nothing
    ch.optimize_cameras(engine="ppg", rolling_shutter=True)
    rep = ch.meta["sfm"]["optimize"]
    assert rep["rolling_shutter_m"]["median"] < 0.005
    assert rep["after"]["control"] < 0.002 and rep["after"]["check"] < 0.003
    copy.save()
    again = ppg.Document.open(d / doc.path.name).chunk  # the adjusted block is saved
    assert len(again.aligned_cameras) == len(ch.aligned_cameras)


def test_gnss_positions_in_the_coordinate_system_of_the_chunk(processed):
    """The GNSS term pulls each camera towards its position in the coordinate system of the chunk: a position read in
    longitude and latitude (EXIF, XMP, MRK) is converted first, never taken as metres; on a chunk still in local
    coordinates such a position cannot be placed and is left out."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core import crs as crs_mod
    from pyarchinit_photogrammetry.engine.sfm import load_reconstruction
    from pyarchinit_photogrammetry.gnss.apply import set_camera_position

    doc, _, _ = processed
    ch = ppg.Document.open(doc.path).chunk  # a copy in memory: nothing is saved
    assert ch.crs == "EPSG:3004" and len(ch.aligned_cameras) > 3
    for cam in ch.aligned_cameras:
        world = ch.to_world(np.asarray([cam.center], float))
        lonlat = crs_mod.transform_points(world, ch.crs, "EPSG:4979")[0]
        set_camera_position(cam, list(lonlat), "EPSG:4979", [0.01, 0.01, 0.02], "dji_mrk", status="fix",
                            height="ellipsoidal")
    rec = load_reconstruction(ch)
    pb, _ids = bundle.problem_from_chunk(ch, rec, use_markers=False, gnss="plain")
    assert len(pb.gnss) == len(ch.aligned_cameras)
    for index, target, _sigma, _flight in pb.gnss:  # each camera where its position says, within a centimetre
        assert np.linalg.norm(target - pb.centres[index]) * ch.scale < 0.01

    ch.crs = crs_mod.LOCAL
    pb, _ids = bundle.problem_from_chunk(ch, rec, use_markers=False, gnss="plain")
    assert pb.gnss == []
