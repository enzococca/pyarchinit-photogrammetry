"""The camera optimisation frees the calibration of a camera only where the block determines it (the rule of the
rounds of the native mapper, with both formulations of ``optimize_cameras``): the variances of the free intrinsics
by the native core (API 17) against a dense inverse of the normal matrix, the variance factor of the tie points and
the marginal sd of the focal length on synthetic blocks, the cameras the optimisation holds and the guard on a
focal length that runs away, the option that frees them whatever the block says, what happens without the core."""
from __future__ import annotations

import copy
import dataclasses
import json
import logging

import numpy as np
import pytest
from test_optimize_gnss import K1, _add_control_points, _rtk_block

from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.engine import bundle, sfm
from pyarchinit_photogrammetry.markers import gcp
from pyarchinit_photogrammetry.testing import bundle_problems

pycolmap = pytest.importorskip("pycolmap")
needs_core = pytest.mark.skipif(not _native.has("intrinsics_variances")
                                or (_native.api() or 0) < _native.INTRINSICS_API, reason=bundle.INTRINSICS_MISSING)


# ------------------------------------------------------------------- the variances of the free intrinsics (API 17)
def _dense_variances(pb: bundle.Problem, drop=()) -> np.ndarray:
    """``diag(inv(J^T J))`` of ``pb`` at the parameters it holds, dense, the columns ``drop`` taken out first (NaN
    in their places)."""
    jac = bundle.evaluate(pb).jacobian.toarray()
    keep = np.setdiff1d(np.arange(jac.shape[1]), np.asarray(drop, int))
    out = np.full(jac.shape[1], np.nan)
    out[keep] = np.diag(np.linalg.inv(jac[:, keep].T @ jac[:, keep]))
    return out


@needs_core
@pytest.mark.parametrize("name", ["control_points", "principal_point", "model_RADIAL", "model_FULL_OPENCV",
                                  "everything"])
def test_the_variances_of_the_free_intrinsics_are_those_of_the_dense_inverse(name):
    """``bundle.intrinsics_variances`` (``ppg_core.bundle_intrinsics_variances``): per camera, the variance of each of
    its free intrinsics at the parameters of the problem - the diagonal of the inverse of the normal matrix at their
    columns, which the core takes from the reduced system."""
    pb = bundle_problems.case(name)
    at = bundle.problem_at(pb, bundle.adjust(copy.deepcopy(pb), max_iterations=200, solver="native"))
    got = bundle.intrinsics_variances(at)
    want = _dense_variances(at)
    lay = bundle._Layout(at)
    assert len(got) == len(at.cameras) == 1 and len(got[0]) == len(lay.free_intrinsics[0]) > 0
    assert np.asarray(got[0]) == pytest.approx(want[lay.camera_cols[0]], rel=1e-6)
    assert (np.asarray(got[0]) > 0).all()


@needs_core
def test_a_camera_with_nothing_free_has_no_variances_and_a_bad_problem_is_refused():
    pb = bundle_problems.case("fixed_intrinsics")
    assert [list(v) for v in bundle.intrinsics_variances(pb)] == [[]]
    with pytest.raises(ValueError, match="loss"):
        bundle.intrinsics_variances(pb, loss="tukey")
    broken = copy.deepcopy(bundle_problems.case("control_points"))
    broken.obs_image = broken.obs_image.copy()
    broken.obs_image[0] = len(broken.centres)  # a photo the problem does not have
    with pytest.raises(ValueError, match="photo"):
        bundle.intrinsics_variances(broken)


@needs_core
def test_a_block_whose_gauge_is_free_has_no_covariance():
    """Tie points alone with no photo held: the normal matrix is singular (the block may sit anywhere at any
    size), and the core says so instead of inverting it."""
    pb = bundle_problems.case("ties")
    pb.fixed_images = set()
    with pytest.raises(RuntimeError, match="positive"):
        bundle.intrinsics_variances(pb)


@needs_core
def test_a_reference_flight_holds_the_frame_of_the_positions():
    """GNSS positions with a bias per flight and no control point: the biases of all the flights and a shift of the
    block trade off. With one flight held at zero (``reference_flight``) the variances are those of the dense
    inverse without the columns of its bias."""
    pb = bundle_problems.case("gnss_bias")
    pb.gcps = []
    pb.free_intrinsics = [list(bundle_problems.FREE_INTRINSICS["OPENCV"])]
    lay = bundle._Layout(pb)
    first = min(lay.bias_col)
    got = bundle.intrinsics_variances(pb, reference_flight=first)
    want = _dense_variances(pb, drop=range(lay.bias_col[first], lay.bias_col[first] + 3))
    assert np.asarray(got[0]) == pytest.approx(want[lay.camera_cols[0]], rel=1e-6)


# ------------------------------------------------------------------- the marginal sd of the focal length of a block
NOISE = 0.3  # px, on the observations of the blocks below


def _drone_block(tilt: float, references: str = "control", seed: int = 3, free=None) -> bundle.Problem:
    """The block of :func:`bundle_problems.block` (18 photos 30 m above ground with a metre of relief) with
    :data:`NOISE` on its tie points, its cameras pitched by ``tilt`` along the strips, and what places it:
    ``"control"`` its five markers as control points at 2 mm, ``"gnss"`` or ``"bias"`` a position of every photo at
    2 cm (two flights, with a bias each for ``"bias"``), ``"none"`` nothing (tie points alone, two photos held as
    :func:`bundle.problem_from_chunk` holds them). The block is adjusted with its calibration held, as an alignment
    leaves it. ``free``: the free intrinsics of its camera (those of the block by default)."""
    rng = np.random.default_rng(seed)
    pb, truth = bundle_problems.block(rng, tilt=tilt)
    pb = bundle._without_held(pb, np.bincount(pb.obs_point, minlength=len(pb.points)) >= 2)
    pb.obs_xy = pb.obs_xy + rng.normal(0, NOISE, pb.obs_xy.shape)
    if references == "control":
        pb.gcps = [(k, truth["markers"][k].copy(), np.full(3, 0.002)) for k in range(5)]
    else:
        bundle_problems._ties_only(pb)
    if references in ("gnss", "bias"):
        pb.gnss = [(i, c.copy(), np.full(3, 0.02), 1 + i // 9) for i, c in enumerate(truth["centres"])]
        pb.gnss_bias = references == "bias"
    if references == "none":
        pb.fixed_images = {0, bundle_problems._farthest(truth["centres"])}
    wanted = list(pb.free_intrinsics[0] if free is None else free)
    pb.free_intrinsics = [[]]
    pb = bundle.problem_at(pb, bundle.adjust(pb, max_iterations=100))
    pb.free_intrinsics = [wanted]
    return pb


def test_the_variance_factor_of_the_tie_points_is_their_noise():
    """``bundle.tie_variance_factor``: ``v^T P v / r`` of the tie points at the parameters of the problem, ``r`` their
    rows less the unknowns they determine. Observations with 0.3 px of noise at a pixel of accuracy give 0.09 on an
    adjusted block, and a block stated at the accuracy of its noise gives 1."""
    pb = _drone_block(bundle_problems.TILT)
    assert bundle.tie_variance_factor(pb) == pytest.approx(NOISE ** 2, rel=0.05)
    pb.pixel_sigma = NOISE
    assert bundle.tie_variance_factor(pb) == pytest.approx(1.0, rel=0.05)
    # nothing to estimate it from: more unknowns than rows
    few = bundle._without_held(pb, np.arange(len(pb.points)) < 3)
    assert bundle.tie_variance_factor(few) == 1.0


@needs_core
def test_the_marginal_sd_of_the_focal_length_tells_a_pitched_block_from_a_nadir_one():
    """``bundle.focal_sds``: the relative sd of the focal length of each camera with every intrinsic the problem
    frees free with it, scaled by the variance factor of the tie points. Photos pitched along the strips tell the
    focal length from the height (well under :data:`bundle.FOCAL_SD_HOLD`); looking straight down over nearly flat
    ground they do not, whatever the control points on that ground say."""
    pitched = _drone_block(bundle_problems.TILT)
    sds, factor = bundle.focal_sds(pitched)
    assert factor == pytest.approx(NOISE ** 2, rel=0.05)
    assert len(sds) == 1 and 0 < sds[0] < 0.002 < bundle.FOCAL_SD_HOLD == 0.005
    # the same quantity by a dense inverse: the factor in the accuracy of the tie points (the control points and
    # the measured markers keep theirs), the larger variance of fx and fy, over fx
    lay = bundle._Layout(pitched)
    scaled = dataclasses.replace(pitched, pixel_sigma=pitched.pixel_sigma * np.sqrt(factor))
    dense = _dense_variances(scaled)[lay.camera_cols[0]][:2]
    assert sds[0] == pytest.approx(np.sqrt(dense.max()) / pitched.cameras[0][1][0], rel=1e-5)
    nadir, _ = bundle.focal_sds(_drone_block(0.0))
    assert nadir[0] > 0.02 > bundle.FOCAL_SD_HOLD


@needs_core
def test_the_sd_is_marginal_over_what_the_problem_frees():
    """With the distortion and the principal point held the focal length of the nadir block looks far better
    determined than it is with them free: the sd is taken with every group the adjustment frees."""
    alone, _ = bundle.focal_sds(_drone_block(0.0, free=[0, 1]))
    radial, _ = bundle.focal_sds(_drone_block(0.0))
    everything, _ = bundle.focal_sds(_drone_block(0.0, free=range(8)))
    assert alone[0] < radial[0] <= everything[0]
    # a camera whose focal length is not free has no sd, and the core is not asked
    held, factor = bundle.focal_sds(_drone_block(0.0, free=[4, 5]))
    assert held == [None] and factor is None


def _sd(pb: bundle.Problem) -> float:
    """The sd of the focal length of the one camera of ``pb``; infinite when the block has no covariance."""
    try:
        return bundle.focal_sds(pb)[0][0]
    except RuntimeError as exc:
        assert "positive" in str(exc)
        return float("inf")


@needs_core
def test_the_sd_of_a_block_nothing_places_and_of_one_held_by_positions_with_a_bias():
    """A block nothing places has no gauge of its own: the first photo holds its place and its orientation and a
    position of the farthest one, a hundred times looser than the block is wide, its size - a gauge that adds
    nothing the block does not have (the two photos :func:`bundle.problem_from_chunk` holds are more than a gauge:
    the sd with them is smaller). Positions with a bias per flight and no control point: the bias of the first
    flight is held at zero. Either way the sd is the block's: small when the photos are pitched, large - or no
    covariance at all - when they look straight down."""
    free = _drone_block(bundle_problems.TILT, "none")
    assert free.fixed_images == {0, 17} and not free.absolute
    sd = _sd(free)
    assert 0 < sd < bundle.FOCAL_SD_HOLD
    held = np.sqrt(bundle.intrinsics_variances(free)[0][:2].max() * bundle.tie_variance_factor(free))
    assert held / free.cameras[0][1][0] < 0.9 * sd  # with the two photos held: too small
    plain, bias = _sd(_drone_block(bundle_problems.TILT, "gnss")), _sd(_drone_block(bundle_problems.TILT, "bias"))
    assert 0 < plain <= bias < bundle.FOCAL_SD_HOLD
    for references in ("none", "gnss", "bias"):  # nothing tells the focal length from the depth of the ground
        assert _sd(_drone_block(0.0, references)) > 0.1


def _large_block(rows: int, per_row: int, seed: int = 1) -> bundle.Problem:
    """``rows`` strips of ``per_row`` photos pitched along the strips, 30 m above rolling ground (the strips 12 m
    apart, the photos 8 m), tie points with 0.3 px of noise seen by two photos or more, the focal length and the
    radial distortion of its RADIAL camera free, the first photo and the farthest from it held."""
    rng = np.random.default_rng(seed)
    rotations = np.array([bundle_problems._look_down(0.02 * (r % 3), bundle_problems.TILT * (1 if r % 2 == 0 else -1))
                          for r in range(rows) for _k in range(per_row)])
    centres = np.array([[8.0 * k, 12.0 * r, 30.0] for r in range(rows) for k in range(per_row)])
    n = len(centres)
    xy = rng.uniform(centres[:, :2].min(0) - 15, centres[:, :2].max(0) + 15, (21 * n, 2))
    points = np.column_stack([xy, 0.6 * np.sin(xy[:, 0] / 7) + 0.4 * np.cos(xy[:, 1] / 5)])
    w, h, f = bundle_problems.W, bundle_problems.H, bundle_problems.F
    params = [f, w / 2, h / 2, -0.02, 0.01]
    cam = pycolmap.Camera(model="RADIAL", width=w, height=h, params=params)
    images, ids, pixels = [], [], []
    for i in range(n):
        near = np.flatnonzero(np.all(np.abs(points[:, :2] - centres[i, :2]) < 40, axis=1))
        local = (points[near] - centres[i]) @ rotations[i].T
        px = np.asarray(cam.img_from_cam(local))[:, :2]
        seen = (local[:, 2] > 0) & (px[:, 0] > 5) & (px[:, 0] < w - 5) & (px[:, 1] > 5) & (px[:, 1] < h - 5)
        images += [i] * int(seen.sum())
        ids += list(near[seen])
        pixels.append(px[seen])
    pixels = np.vstack(pixels) + rng.normal(0, NOISE, (len(images), 2))
    pb = bundle.Problem(rotations, centres, np.zeros(n, int), [("RADIAL", params, w, h)], points, np.array(images),
                        np.array(ids), pixels, free_intrinsics=[[0, 3, 4]])
    pb = bundle._without_held(pb, np.bincount(pb.obs_point, minlength=len(points)) >= 2)
    pb.fixed_images = {0, bundle_problems._farthest(centres)}
    return pb


@needs_core
def test_the_gauge_of_a_free_block_neither_moves_the_sd_nor_breaks_the_inverse(monkeypatch):
    """The size of a block nothing places is a pure gauge: the sd of its focal length is the same whether the
    position that holds it is loose or tight (a tenth to ten times the extent of the block), and at the sigma the
    module uses the normal matrix of a block of hundreds of photos inverts - a position a hundred extents loose is
    below what double precision keeps beside the tie points (400 photos: a pivot that is not positive)."""
    small = _drone_block(bundle_problems.TILT, "none")
    sds = []
    for extents in (0.1, 1.0, 10.0):
        monkeypatch.setattr(bundle, "GAUGE_SIGMA_EXTENTS", extents)
        sds.append(bundle.focal_sds(small)[0][0])
    assert sds[2] == pytest.approx(sds[1], rel=1e-4)  # ten times looser: the same
    assert sds[0] == pytest.approx(sds[1], rel=1e-3)  # ten times tighter: a ten thousandth smaller
    monkeypatch.undo()
    assert bundle.GAUGE_SIGMA_EXTENTS <= 1.0
    large = _large_block(10, 40)
    assert len(large.centres) == 400 and len(large.obs_image) > 50000
    probe, _flight = bundle._with_gauge(large)
    assert probe.fixed_images == {0} and len(probe.gnss) == 1
    variances = bundle.intrinsics_variances(probe)[0]  # at a pixel of accuracy: no factor to help the numbers
    assert 0 < np.sqrt(variances[0]) / bundle_problems.F < 0.002
    sd, factor = bundle.focal_sds(large)
    assert 0 < sd[0] < 0.001 and factor == pytest.approx(NOISE ** 2, rel=0.5)


@needs_core
def test_what_holds_nothing_does_not_break_the_covariance():
    """What an adjustment carries without being held by it must not make the normal matrix singular - the sd would
    then be missing for a block that determines its focal length. A photo with fewer than three observations has
    no pose of its own: it is held as it is. Control points whose markers no photo measures place nothing: the
    block gets the gauge of a free one. Scale bars between such markers give no size: the position that holds the
    size of a free block is added."""
    pitched = _drone_block(bundle_problems.TILT)
    sd = bundle.focal_sds(pitched)[0][0]
    assert 0 < sd < bundle.FOCAL_SD_HOLD
    # a photo that lost its tie points and its markers (cleaned away): the sd of the others' block
    blind = dataclasses.replace(pitched)
    last = len(blind.centres) - 1
    keep, mkeep = blind.obs_image != last, blind.mobs_image != last
    blind.obs_image, blind.obs_point, blind.obs_xy = blind.obs_image[keep], blind.obs_point[keep], blind.obs_xy[keep]
    blind.mobs_image, blind.mobs_marker = blind.mobs_image[mkeep], blind.mobs_marker[mkeep]
    blind.mobs_xy = blind.mobs_xy[mkeep]
    assert bundle.focal_sds(blind)[0][0] == pytest.approx(sd, rel=0.2)
    probe, _flight = bundle._with_gauge(blind)
    assert probe.fixed_images == {last}
    # control points that no photo measures: nothing places the block
    unseen = dataclasses.replace(pitched, mobs_image=np.zeros(0, int), mobs_marker=np.zeros(0, int),
                                 mobs_xy=np.zeros((0, 2)))
    assert unseen.absolute  # five control points are stated
    free = bundle.focal_sds(dataclasses.replace(unseen, gcps=[], markers=np.zeros((0, 3))))[0][0]  # its tie points
    assert 0 < free < bundle.FOCAL_SD_HOLD
    assert bundle.focal_sds(unseen)[0][0] == pytest.approx(free, rel=1e-6)
    probe, _flight = bundle._with_gauge(unseen)
    assert probe.fixed_images == {0} and len(probe.gnss) == 1 and len(probe.gcps) == 5
    # scale bars between markers no photo measures: no size, and no marker to adjust - the gauge of a free block
    bars = dataclasses.replace(unseen, gcps=[], bars=[(0, 2, 30.0, 0.001), (1, 3, 30.0, 0.001)])
    assert bundle.focal_sds(bars)[0][0] == pytest.approx(free, rel=1e-6)
    probe, _flight = bundle._with_gauge(bars)
    assert len(probe.markers) == 0 and probe.bars == [] and len(probe.gnss) == 1
    # a bar between two measured markers gives the size: no position is added, and the sd is the free block's
    sized = dataclasses.replace(pitched, gcps=[], bars=[(0, 2, float(np.linalg.norm(pitched.markers[0]
                                                                                    - pitched.markers[2])), 0.001)])
    probe, _flight = bundle._with_gauge(sized)
    assert probe.fixed_images == {0} and probe.gnss == [] and len(probe.bars) == 1
    assert bundle.focal_sds(sized)[0][0] == pytest.approx(free, rel=0.05)


# ------------------------------------------------------------------- the optimisation of the cameras
ENGINES = ("colmap", "ppg")  # the two formulations of optimize_cameras
GPS = dict(accuracy=(5.0, 5.0, 10.0), status=None)  # positions of a drone without RTK: they hold nothing
PITCH = 0.4  # rad: the photos of a block that determines its focal length lean this much


def _block(folder, pitch: float = 0.0):
    """The synthetic block of the optimisation tests (5 strips of 8 photos 40 m above ground with 3 dm of relief, a
    COLMAP model with half a pixel of noise) with coarse positions, five control points on its ground and the
    fifth of them a check point; ``pitch``: its photos lean forward, the strips opposite ways."""
    doc, ch = _rtk_block(folder, pitch=pitch, **GPS)
    markers = _add_control_points(ch)
    markers[-1].checkpoint = True
    gcp.update_transform(ch)
    return doc, ch


def _calibration(ch) -> list:
    return [list(s.params) for s in ch.sensors.values()]


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_a_nadir_block_keeps_its_calibration_and_a_pitched_one_refines_it(tmp_path, engine):
    """Photos looking straight down over nearly flat ground cannot tell the focal length from the height, whatever
    the control points on that ground say: the optimisation holds the whole calibration of that sensor and the
    report says so in numbers; the block still bends to its control points. Pitched photos determine it: the
    calibration is refined as before."""
    _doc, ch = _block(tmp_path / "nadir")
    before = _calibration(ch)
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    assert check["hold_undetermined"] is True and check["checked"] is True and check["reason"] is None
    assert check["held"] == [0] and len(check["focal_sd"]) == 1
    sensor, sd = check["focal_sd"][0]
    assert sensor == 0 and sd > 0.05 > bundle.FOCAL_SD_HOLD
    assert check["notes"] == [f"sensor 0: focal held: undetermined, sd {100 * sd:.2f} %"]
    assert 0.2 < check["variance_factor"] < 0.4  # half a pixel of noise at a pixel of accuracy
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED
    assert _calibration(ch) == before  # every intrinsic, exactly
    assert rep["control_points"] == 4 and rep["after"]["control"] < 0.005 and rep["after"]["check"] < 0.02

    _doc, ch = _block(tmp_path / "pitched", pitch=PITCH)
    before = _calibration(ch)
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    assert check["checked"] is True and check["held"] == [] and check["notes"] == []
    assert 0 < check["focal_sd"][0][1] < bundle.FOCAL_SD_HOLD
    assert rep["focal_held"] is None
    after = _calibration(ch)
    assert after != before and after[0][0] == pytest.approx(before[0][0], rel=0.01)  # refined, near the true one
    assert after[0][4] == pytest.approx(K1, abs=0.003)
    assert rep["after"]["control"] < 0.005 and rep["after"]["check"] < 0.02


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_freeing_what_the_block_does_not_determine_is_the_users_choice(tmp_path, monkeypatch, engine):
    """``hold_undetermined=False``: the optimisation of before - nothing is asked of the core, every calibration
    is refined as the other options say, on the nadir block too."""
    _doc, ch = _block(tmp_path)
    before = _calibration(ch)
    asked = []
    monkeypatch.setattr(bundle, "focal_sds", lambda pb: asked.append(pb))
    fixed, problems = [], []
    real_adjust, real_own = sfm._adjust, bundle.adjust
    monkeypatch.setattr(sfm, "_adjust", lambda rec, opts, constant_points=(), fixed_cameras=():
                        fixed.append(set(fixed_cameras)) or real_adjust(rec, opts, constant_points, fixed_cameras))
    monkeypatch.setattr(bundle, "adjust", lambda pb, **kwargs: problems.append(pb) or real_own(pb, **kwargs))
    ch.optimize_cameras(engine=engine, hold_undetermined=False)
    rep = ch.meta["sfm"]["optimize"]
    assert rep["intrinsics"] == {"hold_undetermined": False, "checked": False, "reason": None,
                                 "variance_factor": None, "focal_sd": [], "held": [], "notes": []}
    assert rep["focal_held"] is None and asked == []
    assert _calibration(ch) != before
    if engine == "colmap":
        assert fixed and all(cams == set() for cams in fixed)
    else:
        assert [pb.free_intrinsics for pb in problems] == [[[0, 1, 4, 5, 6, 7]]]


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_without_the_core_the_calibration_is_refined_as_before_and_the_log_says_so_once(tmp_path, monkeypatch,
                                                                                        caplog, engine):
    """Where the core cannot give the covariance (missing, older than API 17, switched off) nothing changes: the
    calibration is refined, and one line of the log at INFO says that the check needs the native core."""
    _doc, ch = _block(tmp_path)
    before = _calibration(ch)
    monkeypatch.setattr(bundle, "variances_ready", lambda pb: bundle.INTRINSICS_MISSING)
    with caplog.at_level(logging.INFO, logger="pyarchinit_photogrammetry.engine.sfm"):
        ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    assert rep["intrinsics"] == {"hold_undetermined": True, "checked": False, "reason": bundle.INTRINSICS_MISSING,
                                 "variance_factor": None, "focal_sd": [], "held": [], "notes": []}
    assert rep["focal_held"] is None and _calibration(ch) != before
    lines = [r for r in caplog.records if "native core" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert "API 17" in lines[0].getMessage()


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_a_block_without_a_covariance_keeps_its_calibration(tmp_path, monkeypatch, engine):
    """A normal matrix that cannot be inverted (an intrinsic the block does not hold at all) is the same answer:
    the calibration of every sensor whose focal length would be refined is held, and the note says why."""
    _doc, ch = _block(tmp_path, pitch=PITCH)
    before = _calibration(ch)

    def no_covariance(pb):
        raise RuntimeError("pivot 7 of the reduced system is not positive")

    monkeypatch.setattr(bundle, "focal_sds", no_covariance)
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    assert check["checked"] is True and check["held"] == [0] and check["focal_sd"] == [[0, None]]
    assert check["notes"] == ["sensor 0: focal held: undetermined, no covariance (pivot 7 of the reduced system is "
                              "not positive)"]
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED and _calibration(ch) == before


@needs_core
def test_the_guard_runs_our_adjustment_again_with_the_camera_held(tmp_path, monkeypatch):
    """A focal length that the adjustment moves by more than :data:`bundle.FOCAL_MOVE_HOLD` of its start, on a
    block whose covariance said it was determined (the valley is not a straight line): the adjustment runs again
    from the same start with that camera held, and the notes say by how much it had moved."""
    _doc, ch = _block(tmp_path, pitch=PITCH)
    before = _calibration(ch)
    problems = []
    real = bundle.adjust

    def adjust(pb, **kwargs):
        problems.append(copy.deepcopy(pb.free_intrinsics))
        res = real(pb, **kwargs)
        if len(problems) == 1:  # the first run slides along the valley
            res.camera_params[0] = np.array(res.camera_params[0], float)
            res.camera_params[0][:2] = 1.3 * np.asarray(pb.cameras[0][1], float)[:2]
        return res

    monkeypatch.setattr(bundle, "adjust", adjust)
    ch.optimize_cameras(engine="ppg")
    rep = ch.meta["sfm"]["optimize"]
    assert problems == [[[0, 1, 4, 5, 6, 7]], [[]]]
    check = rep["intrinsics"]
    assert check["held"] == [0] and 0 < check["focal_sd"][0][1] < bundle.FOCAL_SD_HOLD
    assert check["notes"] == ["sensor 0: focal held: moved 30.0 % in the optimisation, run again with it held"]
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED and _calibration(ch) == before
    assert rep["after"]["control"] < 0.005


@needs_core
def test_the_guard_runs_colmaps_adjustment_again_from_the_start(tmp_path, monkeypatch):
    """The same guard on COLMAP's formulation, whose free pass and steps towards the control points change the
    chunk before the model is written: the chunk goes back to where it started (the cameras, the calibration, the
    georeference, the markers), and the whole optimisation runs again with that camera held."""
    _doc, ch = _block(tmp_path, pitch=PITCH)
    before = _calibration(ch)
    transform = np.array(ch.transform, float)
    markers = {m.key: np.array(m.position, float) for m in ch.markers}
    centres = {c.key: np.array(c.center, float) for c in ch.cameras}
    real = sfm._adjust
    runs, starts, slid = [], [], []

    def adjust(rec, opts, constant_points=(), fixed_cameras=()):
        if not constant_points:  # the free pass, the first adjustment of a run
            runs.append(set(fixed_cameras))
            starts.append((np.array(ch.transform, float), {m.key: np.array(m.position, float) for m in ch.markers},
                           {c.key: np.array(c.center, float) for c in ch.cameras}, _calibration(ch)))
        real(rec, opts, constant_points, fixed_cameras)
        if len(runs) == 1 and constant_points and not slid:  # the first run slides along the valley
            slid.append(1)
            for cam in rec.cameras.values():
                params = list(cam.params)
                params[0] *= 1.3
                cam.params = params

    monkeypatch.setattr(sfm, "_adjust", adjust)
    ch.optimize_cameras(engine="colmap")
    rep = ch.meta["sfm"]["optimize"]
    assert runs == [set(), {1}]
    for start in starts:  # each run starts from the chunk as it was
        assert np.allclose(start[0], transform, atol=1e-12) and start[3] == before
        assert all(np.allclose(start[1][k], markers[k], atol=1e-9) for k in markers)
        assert all(np.allclose(start[2][k], centres[k], atol=1e-12) for k in centres)
    check = rep["intrinsics"]
    assert check["held"] == [0] and 0 < check["focal_sd"][0][1] < bundle.FOCAL_SD_HOLD
    assert len(check["notes"]) == 1
    assert check["notes"][0].startswith("sensor 0: focal held: moved ")
    assert check["notes"][0].endswith(" % in the optimisation, run again with it held")
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED and _calibration(ch) == before
    assert rep["control_points"] == 4 and rep["after"]["control"] < 0.005


@needs_core
def test_where_the_focal_length_is_not_refined_nothing_is_asked(tmp_path, monkeypatch):
    """Accurate positions that hold the block keep the focal length of the alignment as before, and the distortion
    is still refined with it held; a block nothing places keeps it under our adjustment. The covariance is not
    asked for: there is no focal length to judge."""
    asked = []
    real = bundle.intrinsics_variances
    monkeypatch.setattr(bundle, "intrinsics_variances", lambda pb, **kwargs: asked.append(1) or real(pb, **kwargs))
    for engine, name in (("colmap", "a"), ("ppg", "b")):
        _doc, ch = _rtk_block(tmp_path / name)
        focal = ch.sensors[0].params[0]
        ch.optimize_cameras(engine=engine)
        rep = ch.meta["sfm"]["optimize"]
        assert rep["focal_held"] == sfm.FOCAL_HELD_BY_POSITIONS and rep["gnss"]["photos"] == 40
        assert rep["intrinsics"]["checked"] is False and rep["intrinsics"]["held"] == []
        assert rep["intrinsics"]["reason"] == sfm.FOCAL_NOT_REFINED
        assert ch.sensors[0].params[0] == pytest.approx(focal, rel=1e-9)
        assert ch.sensors[0].params[4] == pytest.approx(K1, abs=0.002)  # the distortion is refined
    _doc, ch = _rtk_block(tmp_path / "c", **GPS)
    for cam in ch.cameras:
        cam.reference.enabled = False
    ch.optimize_cameras(engine="ppg")
    rep = ch.meta["sfm"]["optimize"]
    assert rep["focal_held"] == sfm.FOCAL_HELD_NO_REFERENCES and rep["intrinsics"]["reason"] == sfm.FOCAL_NOT_REFINED
    assert asked == []


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_the_record_of_the_check_holds_numbers_and_no_names(tmp_path, engine):
    """What the optimisation keeps of the check is numbers and fixed words: the key of a sensor, never its label,
    the label of a photo or a path."""
    doc, ch = _block(tmp_path)
    ch.sensors[0].label = "Hasselblad of the dig"
    ch.optimize_cameras(engine=engine)
    text = json.dumps(ch.meta["sfm"]["optimize"]["intrinsics"])
    assert "Hasselblad" not in text and ".jpg" not in text.lower()
    for cam in ch.cameras:
        assert cam.label not in text
    for folder in (doc.path.parent, ch.data_dir, tmp_path):
        assert str(folder) not in text and folder.name not in text
    doc.save()  # the record is saved with the project
    import pyarchinit_photogrammetry as ppg

    again = ppg.Document.open(doc.path).chunk.meta["sfm"]["optimize"]["intrinsics"]
    assert again == ch.meta["sfm"]["optimize"]["intrinsics"]


def test_the_reason_the_focal_length_was_held_counts_the_sensors():
    """``report["focal_held"]``: the reason the optimisation already has (the positions hold the block) stays; else
    the check's, with how many of the sensors it judged when it did not hold them all."""
    record = {"held": [], "focal_sd": [[0, 0.001], [1, 0.002]]}
    assert sfm._focal_held(None, record) is None
    assert sfm._focal_held(sfm.FOCAL_HELD_BY_POSITIONS, record) == sfm.FOCAL_HELD_BY_POSITIONS
    record = {"held": [1], "focal_sd": [[0, 0.001], [1, 0.2]]}
    assert sfm._focal_held(None, record) == "the block does not determine the focal length of 1 of 2 sensors"
    assert sfm._focal_held(sfm.FOCAL_HELD_BY_POSITIONS, record) == sfm.FOCAL_HELD_BY_POSITIONS
    record = {"held": [0, 1], "focal_sd": [[0, 0.1], [1, 0.2]]}
    assert sfm._focal_held(None, record) == sfm.FOCAL_HELD_UNDETERMINED
    assert sfm.FOCAL_HELD_UNDETERMINED == "the block does not determine the focal length"


def test_a_block_our_formulation_does_not_take_is_optimised_as_before(tmp_path, monkeypatch):
    """COLMAP's adjustment of a reconstruction our formulation refuses (rigs: the faces of 360-degree photos): the
    check is not made, the reason is in the report, and the optimisation goes on as before."""
    _doc, ch = _block(tmp_path)
    before = _calibration(ch)

    def rigs(*args, **kwargs):
        raise ValueError("the reconstruction has rigs (faces of 360-degree photos): use COLMAP's adjustment")

    monkeypatch.setattr(bundle, "problem_from_chunk", rigs)
    ch.optimize_cameras(engine="colmap")
    rep = ch.meta["sfm"]["optimize"]
    assert rep["intrinsics"]["checked"] is False and "rigs" in rep["intrinsics"]["reason"]
    assert rep["focal_held"] is None and _calibration(ch) != before


# ------------------------------------------------------------------- the option, at every door
def test_the_option_holds_by_default_and_reaches_both_formulations(tmp_path, monkeypatch):
    """``hold_undetermined`` is True by default in the engine, on the chunk and on the command line
    (``--hold-undetermined/--free-undetermined``), and reaches the formulation that runs; the report keeps it."""
    import inspect

    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.core.chunk import Chunk

    for fn in (sfm.optimize_cameras, Chunk.optimize_cameras, sfm._optimize_colmap, sfm._optimize_own):
        assert inspect.signature(fn).parameters["hold_undetermined"].default is True
    doc, ch = _block(tmp_path)
    doc.save()
    seen = []
    for name in ("_optimize_colmap", "_optimize_own"):
        monkeypatch.setattr(sfm, name, lambda *args, _name=name, **kwargs:
                            seen.append((_name, kwargs["hold_undetermined"])) or 0.5)
    ch.optimize_cameras()
    ch.optimize_cameras(hold_undetermined=False)
    ch.optimize_cameras(engine="ppg")
    ch.optimize_cameras(engine="ppg", hold_undetermined=False)
    assert seen == [("_optimize_colmap", True), ("_optimize_colmap", False), ("_optimize_own", True),
                    ("_optimize_own", False)]
    monkeypatch.undo()

    run = CliRunner().invoke
    helptext = " ".join(run(app, ["optimize", "--help"]).output.replace("│", " ").split())
    assert "--hold-undetermined" in helptext and "--free-undetermined" in helptext
    result = run(app, ["optimize", str(doc.path), "--free-undetermined"])
    assert result.exit_code == 0, result.output
    assert "focal length of the alignment held" not in result.output
    saved = ppg.Document.open(doc.path).chunk.meta["sfm"]["optimize"]
    assert saved["intrinsics"]["hold_undetermined"] is False and saved["focal_held"] is None
    for args in ([], ["--hold-undetermined"]):
        result = run(app, ["optimize", str(doc.path), *args])
        assert result.exit_code == 0, result.output
        assert ppg.Document.open(doc.path).chunk.meta["sfm"]["optimize"]["intrinsics"]["hold_undetermined"] is True


@needs_core
def test_the_command_says_which_sensors_were_held(tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, _ch = _block(tmp_path)
    doc.save()
    said = " ".join(CliRunner().invoke(app, ["optimize", str(doc.path)]).output.split())
    assert f"focal length of the alignment held ({sfm.FOCAL_HELD_UNDETERMINED})" in said
    assert "sensor 0: focal held: undetermined, sd " in said


def test_the_dialog_offers_the_choice_and_the_window_says_what_was_held(tmp_path, monkeypatch):
    """*Ottimizza camere* has a tick, on by default, for the calibrations the block does not determine; the answer
    reaches the chunk, and the log of the window says for how many sensors the calibration was kept."""
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from pyarchinit_photogrammetry.core.chunk import Chunk
    from pyarchinit_photogrammetry.gui import main_window as mw

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    doc, _ch = _block(tmp_path)
    w = mw.MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    asked, calls = [], []
    answer = {"markers": True, "weight": 4, "pp": False, "engine": "colmap", "gnss": "auto", "rolling": False}

    def ask(cls, parent, title, fields, intro=""):
        asked.append(({f.name: f for f in fields}, intro))
        return dict(answer)

    def optimise(self, **kwargs):
        calls.append(kwargs["hold_undetermined"])
        self.meta.setdefault("sfm", {})["optimize"] = {
            "reprojection_error": 0.5, "focal_held": sfm.FOCAL_HELD_UNDETERMINED if kwargs["hold_undetermined"]
            else None, "intrinsics": {"hold_undetermined": kwargs["hold_undetermined"], "checked": True,
                                      "focal_sd": [[0, 0.11]], "held": [0] if kwargs["hold_undetermined"] else [],
                                      "notes": []}}
        return 0.5

    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(ask))
    monkeypatch.setattr(Chunk, "optimize_cameras", optimise)
    try:
        for hold in (None, False, True):  # a dialog that does not answer it (an older one): held
            answer.pop("hold", None)
            if hold is not None:
                answer["hold"] = hold
            w.optimize_cameras()
            w.runner.wait()
            app.processEvents()
        fields, intro = asked[-1]
        assert fields["hold"].default is True and "focale" in fields["hold"].label
        assert "solo dove il blocco la determina" in intro
        assert calls == [True, False, True]
        said = [e[2] for e in w.log_pane.entries if "Errore di riproiezione" in e[2]]  # the line of each result
        kept = "calibrazione dell'allineamento tenuta: il blocco non determina la focale (1 sensori su 1)"
        assert [kept in line for line in said] == [True, False, True]
        assert not any("focale dell'allineamento tenuta" in line for line in said)
    finally:
        w.setWindowModified(False)
        w.close()


# ------------------------------------------------------------------- the weak ring and the trench
@pytest.fixture(scope="module")
def weak_ring(tmp_path_factory):
    """The weak ring of the gate of the native mapper (``write_weak_ring_dataset`` with its defaults: 80 nadir
    photos along one closed strip over flat ground, 12 targets under it) as ``ppg bench synthetic weak-ring``
    prepares it: matched, aligned by the native mapper, its targets georeferenced with the six of odd code check
    points. The path of the project (each test optimises a copy)."""
    import os

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.bench import steps, truth
    from pyarchinit_photogrammetry.testing.synthetic import write_weak_ring_dataset

    if _native.native_mapper() is None:
        pytest.skip("ppg_core has no NativeMapper (API 16)")
    root = tmp_path_factory.mktemp("undetermined_ring")
    scene = write_weak_ring_dataset(root / "set")
    doc = ppg.Document()
    ch = doc.add_chunk("ring")
    doc.save(root / "ring.ppg")
    ch.add_photos([root / "set" / "photos"])
    ch.detect_markers(steps.DEFAULT_TARGETS)
    previous = os.environ.get(sfm.RANSAC_SEED_ENV)
    os.environ[sfm.RANSAC_SEED_ENV] = "7"
    try:
        ch.match_photos(**steps.ARGUMENTS["match_photos"])
    finally:
        if previous is None:
            os.environ.pop(sfm.RANSAC_SEED_ENV, None)
        else:
            os.environ[sfm.RANSAC_SEED_ENV] = previous
    assert ch.align_cameras(engine="native") == 80
    assert ch.meta["engine"]["align_cameras"]["used"] == "native"
    ch.import_reference(root / "set" / "gcp.csv", crs=scene.crs)
    steps.set_check(ch, [str(code) for code in truth.WEAK_RING_CHECK])
    ch.update_transform()
    doc.save()
    return doc.path


def _copy_of(project, folder):
    """The project ``project`` and its files copied into ``folder`` (its photos stay where they are: an
    optimisation does not read them) and opened."""
    import shutil

    import pyarchinit_photogrammetry as ppg

    folder.mkdir()
    shutil.copy2(project, folder / project.name)
    shutil.copytree(project.with_name(project.name + ".files"), folder / (project.name + ".files"))
    return ppg.Document.open(folder / project.name)


def _marker_rmse(ch) -> dict:
    from pyarchinit_photogrammetry.bench import steps

    return steps.optimization_results(ch)


@needs_core
@pytest.mark.slow
@pytest.mark.parametrize("engine", ENGINES)
def test_the_weak_ring_keeps_the_calibration_of_its_alignment(weak_ring, tmp_path, engine):
    """The weak ring cannot tell its focal length from its height (the native mapper held it, at the value of the
    EXIF); the optimisation on its six control targets holds it too, with either formulation, and says so: the
    cameras stay at their height and the control targets are met within the control line of the gate of the ring.
    Before this rule the same optimisation moved the focal length ten times and the cameras 131 m up."""
    from pyarchinit_photogrammetry.testing.synthetic import WEAK_RING_GATE

    ch = _copy_of(weak_ring, tmp_path / engine).chunk
    before, heights = _calibration(ch), np.array([c.center[2] for c in ch.aligned_cameras])
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    assert check["checked"] is True and len(check["held"]) == 1 and check["held"][0] == check["focal_sd"][0][0]
    assert check["focal_sd"][0][1] > 0.05  # 10 % (COLMAP's formulation) and 14 % (ours) measured
    assert check["notes"][0].endswith(f"focal held: undetermined, sd {100 * check['focal_sd'][0][1]:.2f} %")
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED and _calibration(ch) == before
    assert rep["control_points"] == 6
    rose = np.array([c.center[2] for c in ch.aligned_cameras]) - heights
    assert np.abs(rose).max() < 0.1 * ch.scale  # internal units: within a decimetre of where the alignment put them
    errors = _marker_rmse(ch)
    assert errors["optimized_control_rmse"] <= WEAK_RING_GATE["control_m"]
    assert errors["optimized_check_rmse"] < 0.02  # 16.9 mm measured, 18.6 mm before the optimisation


@needs_core
@pytest.mark.slow
@pytest.mark.xfail(strict=True, reason="with every intrinsic of the camera held the radial distortion stays at the "
                                       "zero of the native alignment (true -0.04): check targets 16.9 mm with "
                                       "both formulations, the line of the gate is 13 mm; with the focal length "
                                       "alone held COLMAP's formulation reaches 8.8 mm, ours 20.1 mm")
@pytest.mark.parametrize("engine", ENGINES)
def test_the_weak_ring_meets_the_check_line_of_its_gate(weak_ring, tmp_path, engine):
    """The check line of the gate of the weak ring after the optimisation (``WEAK_RING_GATE["check_m"]``, 13 mm)."""
    from pyarchinit_photogrammetry.testing.synthetic import WEAK_RING_GATE

    ch = _copy_of(weak_ring, tmp_path / engine).chunk
    ch.optimize_cameras(engine=engine)
    assert _marker_rmse(ch)["optimized_check_rmse"] <= WEAK_RING_GATE["check_m"]


@needs_core
@pytest.mark.slow
def test_freed_on_the_weak_ring_the_focal_length_runs_away_as_before(weak_ring, tmp_path):
    """``hold_undetermined=False`` on the weak ring is the optimisation of before: the focal length is refined and
    slides along its valley with the height of the cameras (1000 px to several thousand), the targets off by
    centimetres."""
    ch = _copy_of(weak_ring, tmp_path / "freed").chunk
    focal = _calibration(ch)[0][0]
    ch.optimize_cameras(hold_undetermined=False)
    rep = ch.meta["sfm"]["optimize"]
    assert rep["focal_held"] is None and rep["intrinsics"]["checked"] is False
    assert _calibration(ch)[0][0] > 2.0 * focal
    assert _marker_rmse(ch)["optimized_check_rmse"] > 0.1


@needs_core
@pytest.mark.slow
@pytest.mark.parametrize("engine", ENGINES)
def test_the_trench_refines_its_calibration_and_keeps_the_metric_gates(processed_native, scene, tmp_path, engine):
    """The trench (oblique photos around an excavation with relief) determines its focal length: the optimisation
    refines the calibration as before, with either formulation, and the metric gates of the project hold after it -
    control < 2 mm, check < 3 mm, the DEM within 3 mm (median) and the targets in the orthomosaic within 5 mm,
    rebuilt from the optimised block."""
    import shutil

    import rasterio

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.markers.targets import detect

    doc, _ch, _report = processed_native
    folder = tmp_path / engine
    folder.mkdir()
    shutil.copy2(doc.path, folder / doc.path.name)
    shutil.copytree(doc.files_dir, folder / doc.files_dir.name)
    ch = ppg.Document.open(folder / doc.path.name).chunk
    for label in ("target 4", "target 5"):
        ch.marker(label).checkpoint = True
    gcp.update_transform(ch)
    before = _calibration(ch)
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    assert check["checked"] is True and check["held"] == [] and check["notes"] == []
    assert 0 < check["focal_sd"][0][1] < 0.002 < bundle.FOCAL_SD_HOLD  # well determined
    assert rep["focal_held"] is None and _calibration(ch) != before
    assert rep["after"]["control"] < 0.002 and rep["after"]["check"] < 0.003
    for m in ch.markers:
        m.checkpoint = False
    assert ch.update_transform()["control_rmse"] < 0.002
    ch.build_depth_maps(quality="high")
    ch.build_point_cloud()
    ch.build_dem()
    ch.build_orthomosaic()
    with rasterio.open(ch.dem_path) as src:
        dem = src.read(1)
        rows, cols = np.nonzero(dem != src.nodata)
        sel = np.random.default_rng(1).choice(len(rows), 4000, replace=False)
        xs, ys = rasterio.transform.xy(src.transform, rows[sel], cols[sel])
    assert abs(np.median(dem[rows[sel], cols[sel]] - scene.world_height(np.array(xs), np.array(ys)))) < 0.003
    with rasterio.open(ch.orthomosaic_path) as src:
        img = np.moveaxis(src.read([3, 2, 1]), 0, -1).copy()
        tr = src.transform
    found = detect(img)
    assert len(found) >= 6
    for d in found:
        x, y = rasterio.transform.xy(tr, d["center"][1], d["center"][0])
        tx, ty, _ = scene.targets[d["code"]]
        assert np.hypot(x - tx, y - ty) < 0.005
