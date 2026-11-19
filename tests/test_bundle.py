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
from pyarchinit_photogrammetry.testing.bundle_problems import F, H, W, block, perturb

pycolmap = pytest.importorskip("pycolmap")

NATIVE_MISSING = "the native core with the bundle adjustment (API 12) is not installed"
needs_native = pytest.mark.skipif(not _native.has("bundle"), reason=NATIVE_MISSING)
SOLVERS = ["scipy", pytest.param("native", marks=needs_native)]


def _gcps(truth, sigma=0.002, which=(0, 1, 2, 3)):
    return [(m, truth["markers"][m].copy(), np.full(3, sigma)) for m in which]


@pytest.mark.parametrize("solver", SOLVERS)
def test_a_block_comes_back_to_its_control_points(solver):
    rng = np.random.default_rng(1)
    pb, truth = block(rng)
    pb.gcps = _gcps(truth)
    perturb(pb, rng)
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
    pb, truth = block(rng)
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
    pb, truth = block(rng)
    pb.free_intrinsics = [[]]
    bias = np.array([0.35, -0.2, 0.6])
    pb.gcps = _gcps(truth, which=(0, 1, 2))
    pb.gnss = [(i, c + bias + rng.normal(0, 0.01, 3), np.full(3, 0.02), 1) for i, c in enumerate(truth["centres"])]
    pb.gnss_bias = True
    perturb(pb, rng, centre=0.1, point=0.05)
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
    pb, truth = block(rng, rolling=rolling)
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
    pb, truth = block(rng, rolling=rolling)
    m = truth["markers"]
    pb.gcps = _gcps(truth, which=(0, 1, 2))
    pb.bars = [(0, 2, float(np.linalg.norm(m[0] - m[2])), 0.001), (1, 3, float(np.linalg.norm(m[1] - m[3])), 0.001)]
    bias = {1: np.array([0.35, -0.2, 0.6]), 2: np.array([-0.1, 0.25, -0.3])}
    pb.gnss = [(i, c + bias[1 + i // 9] + rng.normal(0, 0.01, 3), np.full(3, 0.02), 1 + i // 9)
               for i, c in enumerate(truth["centres"])]
    pb.gnss_bias = True
    pb.rolling_shutter = True
    pb.fixed_images = {3}
    perturb(pb, rng)
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
    pb, truth = block(rng, n_points=40)
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
    pb, truth = block(rng, n_points=40)
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
    # "auto" turned to the native solver on its own: the fallback is a line of the log, the dropped loss a warning
    monkeypatch.setattr(bundle, "NATIVE_BY_DEFAULT", True)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="pyarchinit_photogrammetry"):
        plain = bundle.adjust(pb, max_iterations=2, solver="auto")
        robust = bundle.adjust(pb, max_iterations=2, solver="auto", loss="huber", loss_scale=2.0)
    assert plain.solver == robust.solver == "scipy" and "squared loss" not in plain.fallback
    assert "squared loss" in robust.fallback
    said = [(r.levelno, r.getMessage()) for r in caplog.records if "PPG_NATIVE=0" in r.getMessage()]
    assert [level for level, _ in said] == [logging.INFO, logging.WARNING] and "squared loss" in said[1][1]


@needs_native
def test_the_native_solver_is_taken_when_asked_or_set(monkeypatch, caplog):
    rng = np.random.default_rng(7)
    pb, truth = block(rng, n_points=40)
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
    pb, truth = block(rng)
    pb.gcps = _gcps(truth)
    perturb(pb, rng)
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
    pb, truth = block(rng)
    pb.gcps = _gcps(truth)
    perturb(pb, rng)
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
    pb, truth = block(rng)
    pb.gcps = _gcps(truth)
    perturb(pb, rng)
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


def _copy_of_the_trench(processed, tmp_path):
    """The processed trench copied into ``tmp_path`` (project, files and photos) and opened: what a test that
    writes into the project works on."""
    import shutil

    import pyarchinit_photogrammetry as ppg

    doc, _, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    return ppg.Document.open(d / doc.path.name)


@pytest.mark.parametrize("solver", SOLVERS)
def test_own_adjustment_of_the_trench(processed, tmp_path, monkeypatch, solver):
    """``optimize_cameras(engine="ppg", solver=...)`` on the synthetic trench, by either solver: control
    points pulled by their accuracy, two of them left as check points, the metric thresholds of the
    pipeline kept; the report says which solver ran. The control points place the block, so the focal
    length is refined as before (the report says it was not held)."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.markers import gcp

    copy = _copy_of_the_trench(processed, tmp_path)
    ch = copy.chunk
    for m in ch.markers[:2]:
        m.checkpoint = True
    gcp.update_transform(ch)
    sensor = next(iter(ch.sensors.values()))
    focal = tuple(sensor.params[:2])
    seen = []
    real = bundle.adjust
    monkeypatch.setattr(bundle, "adjust", lambda pb, **kwargs: seen.append(pb) or real(pb, **kwargs))
    err = ch.optimize_cameras(engine="ppg", solver=solver)
    rep = ch.meta["sfm"]["optimize"]
    assert rep["engine"] == "ppg" and rep["control_points"] >= 4 and err < 0.5
    assert rep["after"]["control"] < 0.002 and rep["after"]["check"] < 0.003
    assert rep["solver"]["requested"] == solver and rep["solver"]["used"] == solver
    assert rep["solver"]["fallback"] is None and rep["solver"]["seconds"] > 0
    assert rep["solver"]["linear_solver"] == ("dense" if solver == "native" else None)
    assert rep["focal_held"] is None and seen[-1].absolute and seen[-1].free_intrinsics == [[0, 1, 4, 5, 6, 7]]
    assert tuple(sensor.params[:2]) != focal  # refined
    # the photos of the trench have a global shutter: asked for, the velocities stay close to nothing
    ch.optimize_cameras(engine="ppg", rolling_shutter=True, solver=solver)
    rep = ch.meta["sfm"]["optimize"]
    assert rep["rolling_shutter_m"]["median"] < 0.005
    assert rep["after"]["control"] < 0.002 and rep["after"]["check"] < 0.003
    copy.save()
    again = ppg.Document.open(copy.path).chunk  # the adjusted block is saved, the report with it
    assert len(again.aligned_cameras) == len(ch.aligned_cameras)
    assert again.meta["sfm"]["optimize"]["solver"]["used"] == solver


def test_a_solver_is_an_option_of_our_own_adjustment(processed):
    """``solver=`` belongs to ``engine="ppg"``: with COLMAP's adjustment any word but ``auto`` is refused before
    any work, as is a word that is not a solver."""
    import pyarchinit_photogrammetry as ppg

    doc, _, _ = processed
    ch = ppg.Document.open(doc.path).chunk  # a copy in memory: nothing is written
    with pytest.raises(ValueError, match="engine='ppg'"):
        ch.optimize_cameras(engine="colmap", solver="native")
    with pytest.raises(ValueError, match="auto, native or scipy"):
        ch.optimize_cameras(engine="ppg", solver="ceres")
    assert "optimize" not in ch.meta["sfm"]


@needs_native
def test_cancelling_the_native_optimisation_leaves_the_model_as_it_was(processed, tmp_path):
    """A cancel pressed while the native solver runs: the step stops with ``Cancelled`` at its next report and
    the reconstruction on disk is byte for byte what it was (the model is written only after the solve), no
    report of the run is left, nothing of the safety net stays beside the model."""
    copy = _copy_of_the_trench(processed, tmp_path)
    ch = copy.chunk
    model = ch.product_path("reconstruction")
    files = sorted(p for p in model.rglob("*") if p.is_file())
    before = {p: p.read_bytes() for p in files}
    progress = Progress()
    calls = []

    def cancel_at_the_third(fraction, message):
        calls.append(message)
        if len(calls) == 3:
            progress.cancel()

    progress.callback = cancel_at_the_third
    with pytest.raises(Cancelled):
        ch.optimize_cameras(engine="ppg", solver="native", progress=progress)
    assert len(calls) == 3 and "native" in calls[0] and "iteration 2" in calls[2]
    assert sorted(p for p in model.rglob("*") if p.is_file()) == files
    assert all(p.read_bytes() == before[p] for p in files)
    assert "optimize" not in ch.meta["sfm"]
    assert not model.with_name(model.name + ".before_optimize").exists()


def test_the_plain_problem_of_a_reconstruction_is_the_tie_part_of_the_chunks(processed):
    """``problem_from_reconstruction`` is what ``problem_from_chunk`` starts from: on the trench without its markers
    the two give the same tie points, poses, calibrations and ids; the plain problem holds the first photo and the
    farthest from it, frees every calibration but the principal point, has no reference of any kind, and one
    observation per element of the tracks of the posed images."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.engine.sfm import load_reconstruction

    doc, _, _ = processed
    ch = ppg.Document.open(doc.path).chunk  # a copy in memory: nothing is written
    rec = load_reconstruction(ch)
    plain, ids = bundle.problem_from_reconstruction(rec)
    pb, ids_chunk = bundle.problem_from_chunk(ch, rec, use_markers=False)
    for name in ("rotations", "centres", "image_camera", "points", "obs_image", "obs_point", "obs_xy"):
        assert np.array_equal(getattr(plain, name), getattr(pb, name)), name
    assert plain.cameras == pb.cameras and plain.fixed_images == pb.fixed_images
    assert plain.free_intrinsics == pb.free_intrinsics
    assert ids == {**ids_chunk, "markers": []} and ids_chunk["markers"] == []
    far = int(np.argmax(np.linalg.norm(plain.centres - plain.centres[0], axis=1)))
    assert far != 0 and plain.fixed_images == {0, far}
    assert [c[0] for c in plain.cameras] == ["OPENCV"] and plain.free_intrinsics == [[0, 1, 4, 5, 6, 7]]
    assert len(plain.markers) == 0 and len(plain.mobs_image) == 0 and plain.gcps == plain.bars == plain.gnss == []
    assert not plain.rolling_shutter and not plain.gnss_bias and plain.pixel_sigma == 1.0
    posed = {iid for iid, im in rec.images.items() if im.has_pose}
    assert len(plain.obs_image) == sum(sum(1 for el in p.track.elements if el.image_id in posed)
                                       for p in rec.points3D.values())
    assert len(plain.obs_image) == sum(rec.images[iid].num_points3D for iid in posed)
    assert len(plain.points) == rec.num_points3D() and len(plain.centres) == len(posed) == len(ch.aligned_cameras)


def test_a_fixed_sensor_holds_its_calibration_and_no_photo(processed):
    """A sensor marked ``fixed`` holds its calibration and nothing else: the poses held are the gauge rule's alone -
    none on a block placed by three control points, the first photo and the farthest from it on a block without
    references. The held cameras of ``_rec_fixed_cameras`` are COLMAP camera ids, never image ids: on the trench
    the one sensor's id is also an image id, the coincidence that once froze that photo's pose."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.engine.sfm import _rec_fixed_cameras, load_reconstruction

    doc, _, _ = processed
    ch = ppg.Document.open(doc.path).chunk  # a copy in memory: nothing is written
    for sensor in ch.sensors.values():
        sensor.fixed = True
    rec = load_reconstruction(ch)
    held = _rec_fixed_cameras(ch, rec)
    assert held == set(rec.cameras) and held & set(rec.images)  # a held camera id that is an image id too
    pb, _ids = bundle.problem_from_chunk(ch, rec)  # the control points of the trench place the block
    assert len(pb.gcps) >= 3 and pb.fixed_images == set() and pb.free_intrinsics == [[]]
    plain, _ids = bundle.problem_from_chunk(ch, rec, use_markers=False)
    far = int(np.argmax(np.linalg.norm(plain.centres - plain.centres[0], axis=1)))
    assert far != 0 and plain.fixed_images == {0, far} and plain.free_intrinsics == [[]]
    lay = bundle._Layout(plain)
    assert lay.size == 6 * (len(plain.centres) - 2) + 3 * len(plain.points)  # no intrinsic column, two photos held


def test_a_problem_says_whether_its_references_place_it_and_its_focal_length_can_be_held():
    """``Problem.absolute``: three control points or more, or three GNSS positions or more, place the block
    (bars give it a size, not a place); ``hold_focal_length`` takes the focal length out of the free parameters
    of every camera - one index for the models with a single focal length, two for the others - and leaves
    the rest, doing nothing more a second time."""
    rng = np.random.default_rng(12)
    pb, truth = block(rng, n_points=40)
    assert pb.absolute is False and pb.gcps == [] and pb.gnss == []
    pb.gcps = _gcps(truth, which=(0, 1))
    pb.bars = [(0, 2, 1.0, 0.001), (1, 3, 1.0, 0.001)]
    assert pb.absolute is False
    pb.gcps = _gcps(truth)
    assert pb.absolute is True
    pb.gcps = []
    pb.gnss = [(i, truth["centres"][i].copy(), np.full(3, 0.02), 0) for i in range(2)]
    assert pb.absolute is False
    pb.gnss.append((2, truth["centres"][2].copy(), np.full(3, 0.02), 0))
    assert pb.absolute is True
    assert pb.cameras[0][0] == "OPENCV" and pb.free_intrinsics == [[0, 1, 4, 5]]  # the block frees k1, k2 only
    bundle.hold_focal_length(pb)
    assert pb.free_intrinsics == [[4, 5]]
    bundle.hold_focal_length(pb)
    assert pb.free_intrinsics == [[4, 5]]
    two = bundle.Problem(**{**pb.__dict__, "cameras": [("SIMPLE_RADIAL", [F, W / 2, H / 2, 0.0], W, H),
                                                      ("PINHOLE", [F, F, W / 2, H / 2], W, H)],
                            "free_intrinsics": [[0, 1, 2, 3], [0, 1, 2, 3]]})
    bundle.hold_focal_length(two)
    assert two.free_intrinsics == [[1, 2, 3], [2, 3]]


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
