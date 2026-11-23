"""The camera optimisation refines each group of the intrinsics of a camera only where the block determines it
(the rule of the rounds of the native mapper taken group by group and on one scale, with both formulations of
``optimize_cameras``): the covariance of the free intrinsics by the native core (API 18) against a dense inverse of
the normal matrix, the variance factor of the tie points, the marginal sd of the focal length and of what each
other group moves at the corner of the photo, judged one after the other on synthetic blocks, what the optimisation
holds and what it still refines, the guard on a focal length or a radial term that runs away, the option that frees
everything whatever the block says, what happens without the core, and the weak ring, a nadir grid and the trench
optimised."""
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
needs_core = pytest.mark.skipif(not _native.has("intrinsics_covariance")
                                or (_native.api() or 0) < _native.INTRINSICS_API, reason=bundle.INTRINSICS_MISSING)


# ------------------------------------------------------------------- the covariance of the free intrinsics (API 18)
def _dense_inverse(pb: bundle.Problem, drop=()) -> np.ndarray:
    """``inv(J^T J)`` of ``pb`` at the parameters it holds, dense, the columns ``drop`` taken out first (NaN in
    their rows and columns)."""
    jac = bundle.evaluate(pb).jacobian.toarray()
    keep = np.setdiff1d(np.arange(jac.shape[1]), np.asarray(drop, int))
    out = np.full((jac.shape[1], jac.shape[1]), np.nan)
    out[np.ix_(keep, keep)] = np.linalg.inv(jac[:, keep].T @ jac[:, keep])
    return out


def _dense_variances(pb: bundle.Problem, drop=()) -> np.ndarray:
    """The diagonal of :func:`_dense_inverse`."""
    return np.diag(_dense_inverse(pb, drop)).copy()


@needs_core
@pytest.mark.parametrize("name", ["control_points", "principal_point", "model_RADIAL", "model_FULL_OPENCV",
                                  "everything"])
def test_the_covariance_of_the_free_intrinsics_is_the_block_of_the_dense_inverse(name):
    """``bundle.intrinsics_covariance`` (``ppg_core.bundle_intrinsics_covariance``): the block of the inverse of the
    normal matrix at the free intrinsics of the cameras, at the parameters of the problem, which the core takes
    from the reduced system - every term of it, and the columns each camera has."""
    pb = bundle_problems.case(name)
    at = bundle.problem_at(pb, bundle.adjust(copy.deepcopy(pb), max_iterations=200, solver="native"))
    matrix, counts = bundle.intrinsics_covariance(at)
    lay = bundle._Layout(at)
    cols = lay.camera_cols[0]
    want = _dense_inverse(at)[np.ix_(cols, cols)]
    assert counts == [len(lay.free_intrinsics[0])] and matrix.shape == want.shape and len(cols) > 0
    scale = np.sqrt(np.outer(want.diagonal(), want.diagonal()))
    assert np.abs(matrix - want).max() <= 1e-6 * scale.max() and np.abs((matrix - want) / scale).max() <= 1e-5
    assert np.allclose(matrix, matrix.T, rtol=0, atol=1e-9 * scale.max()) and (matrix.diagonal() > 0).all()
    assert matrix.diagonal() == pytest.approx(want.diagonal(), rel=1e-6)


@needs_core
def test_a_camera_with_nothing_free_has_no_covariance_block_and_a_bad_problem_is_refused():
    pb = bundle_problems.case("fixed_intrinsics")
    matrix, counts = bundle.intrinsics_covariance(pb)
    assert matrix.shape == (0, 0) and counts == [0]
    with pytest.raises(ValueError, match="loss"):
        bundle.intrinsics_covariance(pb, loss="tukey")
    broken = copy.deepcopy(bundle_problems.case("control_points"))
    broken.obs_image = broken.obs_image.copy()
    broken.obs_image[0] = len(broken.centres)  # a photo the problem does not have
    with pytest.raises(ValueError, match="photo"):
        bundle.intrinsics_covariance(broken)


@needs_core
def test_a_block_whose_gauge_is_free_has_no_covariance():
    """Tie points alone with no photo held: the normal matrix is singular (the block may sit anywhere at any
    size), and the core says so instead of inverting it."""
    pb = bundle_problems.case("ties")
    pb.fixed_images = set()
    with pytest.raises(RuntimeError, match="positive"):
        bundle.intrinsics_covariance(pb)


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
    got, _counts = bundle.intrinsics_covariance(pb, reference_flight=first)
    want = _dense_variances(pb, drop=range(lay.bias_col[first], lay.bias_col[first] + 3))
    assert got.diagonal() == pytest.approx(want[lay.camera_cols[0]], rel=1e-6)


# ------------------------------------------------------------------- the marginal sds of the groups of a block
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


def _sd(pb: bundle.Problem) -> float:
    """The relative sd of the focal length of the one camera of ``pb`` (the first judgement of
    ``bundle.judge_intrinsics``); infinite when the block has no covariance with it free."""
    sds, held, _factor = bundle.judge_intrinsics(pb)
    if sds[0]["focal"] is None:
        kind, why = held[0]["focal"]
        assert kind == "no covariance" and "positive" in why
        return float("inf")
    return sds[0]["focal"]


@needs_core
def test_the_marginal_sd_of_the_focal_length_tells_a_pitched_block_from_a_nadir_one():
    """The first judgement of ``bundle.judge_intrinsics``: the relative sd of the focal length of each camera with
    every intrinsic the problem frees free with it, scaled by the variance factor of the tie points. Photos pitched
    along the strips tell the focal length from the height (well under :data:`bundle.FOCAL_SD_HOLD`); looking
    straight down over nearly flat ground they do not, whatever the control points on that ground say."""
    pitched = _drone_block(bundle_problems.TILT)
    sds, held, factor = bundle.judge_intrinsics(pitched)
    assert factor == pytest.approx(NOISE ** 2, rel=0.05)
    assert len(sds) == 1 and 0 < sds[0]["focal"] < 0.002 < bundle.FOCAL_SD_HOLD == 0.005 and held == [{}]
    # the same quantity by a dense inverse: the factor in the accuracy of the tie points (the control points and
    # the measured markers keep theirs), the larger variance of fx and fy, over fx
    lay = bundle._Layout(pitched)
    scaled = dataclasses.replace(pitched, pixel_sigma=pitched.pixel_sigma * np.sqrt(factor))
    dense = _dense_variances(scaled)[lay.camera_cols[0]][:2]
    assert sds[0]["focal"] == pytest.approx(np.sqrt(dense.max()) / pitched.cameras[0][1][0], rel=1e-5)
    nadir, held, _factor = bundle.judge_intrinsics(_drone_block(0.0))
    assert nadir[0]["focal"] > 0.02 > bundle.FOCAL_SD_HOLD and held[0]["focal"] == ("sd", nadir[0]["focal"])


def test_the_groups_of_the_intrinsics_of_the_models_of_the_core():
    """``bundle.intrinsic_groups``: the parameters of a camera model by group, in COLMAP's order - what the camera
    optimisation holds or refines together."""
    assert bundle.intrinsic_groups("SIMPLE_PINHOLE") == {"focal": (0,), "principal_point": (1, 2), "radial": (),
                                                         "tangential": ()}
    assert bundle.intrinsic_groups("PINHOLE") == {"focal": (0, 1), "principal_point": (2, 3), "radial": (),
                                                  "tangential": ()}
    assert bundle.intrinsic_groups("SIMPLE_RADIAL")["radial"] == (3,)
    assert bundle.intrinsic_groups("RADIAL") == {"focal": (0,), "principal_point": (1, 2), "radial": (3, 4),
                                                 "tangential": ()}
    assert bundle.intrinsic_groups("OPENCV") == {"focal": (0, 1), "principal_point": (2, 3), "radial": (4, 5),
                                                 "tangential": (6, 7)}
    assert bundle.intrinsic_groups("FULL_OPENCV")["radial"] == (4, 5, 8, 9, 10, 11)
    for model, params in bundle_problems.MODEL_PARAMS.items():  # every parameter in one group, once
        indices = sorted(k for group in bundle.intrinsic_groups(model).values() for k in group)
        assert indices == list(range(len(params))), model


def test_what_ran_away_in_an_adjustment():
    """``bundle.runaways``: the groups of a camera that an adjustment moved too far - a focal length by more than
    :data:`bundle.FOCAL_MOVE_HOLD` of its start, the first radial term by more than :data:`bundle.K1_MOVE_HOLD` -
    among those it refined, with by how much."""
    start = [1000.0, 1000.0, 600.0, 450.0, -0.04, 0.01, 0.0, 0.0]
    free = [0, 1, 4, 5, 6, 7]
    assert bundle.FOCAL_MOVE_HOLD == 0.2 and bundle.K1_MOVE_HOLD == 0.5
    assert bundle.runaways("OPENCV", start, start, free) == {}
    near = [1150.0, 1150.0, 600.0, 450.0, -0.4, 0.3, 0.0, 0.0]
    assert bundle.runaways("OPENCV", start, near, free) == {}
    far = [1000.0, 1300.0, 600.0, 450.0, -0.04, 0.01, 0.0, 0.0]
    assert bundle.runaways("OPENCV", start, far, free) == {"focal": pytest.approx(0.3)}
    bent = [1000.0, 1000.0, 600.0, 450.0, -0.64, 0.01, 0.0, 0.0]
    assert bundle.runaways("OPENCV", start, bent, free) == {"radial": pytest.approx(0.6)}
    both = [700.0, 700.0, 600.0, 450.0, 0.6, 0.01, 0.0, 0.0]
    ran = bundle.runaways("OPENCV", start, both, free)
    assert ran == {"focal": pytest.approx(0.3), "radial": pytest.approx(0.64)}
    assert bundle.runaways("OPENCV", start, both, [4, 5]) == {"radial": pytest.approx(0.64)}  # the focal was held
    assert bundle.runaways("OPENCV", start, both, [0, 1]) == {"focal": pytest.approx(0.3)}
    lost = bundle.runaways("OPENCV", start, [float("nan")] * 8, free)
    assert lost == {"focal": float("inf"), "radial": float("inf")}
    radial = [1000.0, 600.0, 450.0, 0.0, 0.0]  # f, cx, cy, k1, k2
    assert bundle.runaways("RADIAL", radial, [1000.0, 600.0, 450.0, 0.7, 0.0], [0, 3, 4]) == {
        "radial": pytest.approx(0.7)}
    pinhole = [1000.0, 1000.0, 600.0, 450.0]
    assert bundle.runaways("PINHOLE", pinhole, [1000.0, 1000.0, 700.0, 450.0], [0, 1, 2, 3]) == {}


def test_how_the_corners_of_the_photo_move_with_the_intrinsics():
    """``bundle.corner_jacobians``: the derivative of the pixel at each corner of the photo by the parameters asked
    for, against the formulas of the OPENCV model at the rays of those corners; ``bundle.corner_sd``: the sd of
    what a group moves there, at the corner where it is largest."""
    w, h, f = 1200.0, 900.0, 1000.0
    params = [f, f, 600.0, 450.0, -0.02, 0.01, 0.0005, -0.0003]
    jacs = bundle.corner_jacobians("OPENCV", params, w, h, [4, 5, 6, 7, 2, 3])  # k1, k2, p1, p2, cx, cy
    assert jacs.shape == (4, 2, 6)
    corners = [(0.0, 0.0), (w, 0.0), (0.0, h), (w, h)]
    for jac, (u, v) in zip(jacs, corners):
        x, y = (u - 600.0) / f, (v - 450.0) / f
        r2 = x * x + y * y
        want = np.array([[f * x * r2, f * x * r2 ** 2, f * 2 * x * y, f * (r2 + 2 * x * x), 1.0, 0.0],
                         [f * y * r2, f * y * r2 ** 2, f * (r2 + 2 * y * y), f * 2 * x * y, 0.0, 1.0]])
        assert jac == pytest.approx(want, rel=1e-6, abs=1e-6)
    radial = bundle.corner_jacobians("RADIAL", [f, 600.0, 450.0, -0.02, 0.01], w, h, [3])
    assert radial[:, :, 0] == pytest.approx(jacs[:, :, 0], rel=1e-6)
    assert bundle.corner_jacobians("PINHOLE", [f, f, 600.0, 450.0], w, h, []).shape == (4, 2, 0)
    # the radial terms move the four corners alike; tangential terms that go together do not: the worst counts
    k1 = np.array([[1e-6]])
    assert bundle.corner_sd("OPENCV", params, w, h, [4], k1) == pytest.approx(
        1e-3 * np.linalg.norm(jacs[0, :, 0]), rel=1e-6)
    together = 1e-8 * np.array([[1.0, 0.9], [0.9, 1.0]])  # p1 and p2 with a correlation of 0.9
    each = [np.sqrt(np.trace(jac[:, 2:4] @ together @ jac[:, 2:4].T)) for jac in jacs]
    assert max(each) > 1.5 * min(each)
    assert bundle.corner_sd("OPENCV", params, w, h, [6, 7], together) == pytest.approx(max(each), rel=1e-9)
    assert bundle.corner_sd("OPENCV", params, w, h, [2, 3], np.diag([0.09, 0.16])) == pytest.approx(0.5)


def _oracle(pb: bundle.Problem, factor: float, held=()) -> tuple[np.ndarray, list]:
    """The covariance of the intrinsics of the one camera of ``pb`` that are still free once the parameters
    ``held`` have left the adjustment, by a dense inverse, the variance factor ``factor`` of the tie points in
    their accuracy; with the parameters its rows stand for."""
    free = [k for k in pb.free_intrinsics[0] if k not in held]
    probe = dataclasses.replace(pb, free_intrinsics=[free], pixel_sigma=pb.pixel_sigma * np.sqrt(factor))
    cols = bundle._Layout(probe).camera_cols[0]
    return _dense_inverse(probe)[np.ix_(cols, cols)], free


@needs_core
@pytest.mark.parametrize("tilt, references", [(bundle_problems.TILT, "control"), (0.0, "control"),
                                              (bundle_problems.TILT, "gnss")])
def test_each_group_is_judged_by_what_it_moves_at_the_corner_of_the_photo(tilt, references):
    """``bundle.judge_intrinsics`` with every intrinsic free: the focal length by its relative sd, then the
    tangential terms, the principal point and the radial terms by the sd, in pixels, of what each moves at the
    corner of the photo - each from the covariance of the adjustment without what was held before it. The same
    figures and the same verdicts come from a dense inverse of the normal matrix, taken again after every hold."""
    pb = _drone_block(tilt, references, free=range(8))
    model, params, width, height = pb.cameras[0]
    groups = bundle.intrinsic_groups(model)
    sds, held, factor = bundle.judge_intrinsics(pb)
    assert factor == pytest.approx(NOISE ** 2, rel=0.05) and len(sds) == len(held) == 1
    assert bundle.JUDGED == ("focal", "tangential", "principal_point", "radial") and set(sds[0]) == set(bundle.JUDGED)
    gone, verdicts = [], {}
    for group in bundle.JUDGED:
        cov, free = _oracle(pb, factor, gone)
        index = [free.index(k) for k in groups[group]]
        block = cov[np.ix_(index, index)]
        if group == "focal":
            want, limit = np.sqrt(block.diagonal().max()) / params[0], bundle.FOCAL_SD_HOLD
        else:
            want = bundle.corner_sd(model, params, width, height, list(groups[group]), block)
            limit = bundle.GROUP_SD_HOLD_PX
        assert sds[0][group] == pytest.approx(want, rel=1e-4), group
        assert abs(want / limit - 1.0) > 0.02, (group, want)  # no verdict of this test hangs on the last digit
        if want > limit:
            verdicts[group] = ("sd", pytest.approx(want, rel=1e-4))
            gone += list(groups[group])
    assert held[0] == verdicts
    assert bundle.FOCAL_SD_HOLD == 0.005 and bundle.GROUP_SD_HOLD_PX == 0.5
    if tilt == 0.0:  # looking straight down: nothing of the calibration is determined
        assert set(held[0]) == set(bundle.JUDGED)
    else:  # pitched: the focal length and the radial terms are
        assert "focal" not in held[0] and "radial" not in held[0]


@needs_core
def test_the_order_decides_between_two_groups_that_trade():
    """The order of the judgement is fixed, from what a real lens needs least to what it needs most: focal length,
    tangential terms, principal point, radial terms. Pitched photos on control points, every intrinsic free: the
    tangential terms (0.53 px at the corner) and the principal point (1.87 px) trade, and both are beyond half a
    pixel. The tangential terms come first and are held; the principal point, judged without them, is determined
    (0.34 px) and is refined - though of the two it was the further beyond its limit: a rule that gave up the
    worst group first would have held it, and refined the tangential terms (0.10 px without it)."""
    pb = _drone_block(bundle_problems.TILT, free=range(8))
    model, params, width, height = pb.cameras[0]
    sds, held, factor = bundle.judge_intrinsics(pb)
    assert bundle.JUDGED == ("focal", "tangential", "principal_point", "radial")
    assert held[0] == {"tangential": ("sd", sds[0]["tangential"])}
    assert bundle.GROUP_SD_HOLD_PX < sds[0]["tangential"] < 0.6 and 0 < sds[0]["principal_point"] < 0.4

    def corner(columns, gone=()):
        cov, free = _oracle(pb, factor, gone)
        index = [free.index(k) for k in columns]
        return bundle.corner_sd(model, params, width, height, list(columns), cov[np.ix_(index, index)])

    # by a dense inverse: with everything free the principal point is the further beyond the limit of the two
    assert corner((2, 3)) > 1.5 and corner((2, 3)) > 2 * corner((6, 7)) > 2 * bundle.GROUP_SD_HOLD_PX
    assert sds[0]["tangential"] == pytest.approx(corner((6, 7)), rel=1e-4)
    assert sds[0]["principal_point"] == pytest.approx(corner((2, 3), gone=(6, 7)), rel=1e-4)
    assert corner((6, 7), gone=(2, 3)) < 0.2  # what the other choice would have left of the tangential terms


@needs_core
def test_what_is_held_leaves_the_adjustment_before_the_next_group_is_judged():
    """The radial terms of a block that cannot tell its focal length from its height: judged with the focal length
    free they look as lost as it is - they trade with it -, judged once it is held they are what the block gives
    them. The figure of ``judge_intrinsics`` is the second: that of the same problem without its focal length."""
    nadir = _drone_block(0.0, "gnss")  # positions of every photo: the radial terms are determined, the focal not
    model, params, width, height = nadir.cameras[0]
    sds, held, factor = bundle.judge_intrinsics(nadir)
    assert set(held[0]) == {"focal"} and held[0]["focal"] == ("sd", sds[0]["focal"]) and sds[0]["focal"] > 0.05
    assert 0 < sds[0]["radial"] < bundle.GROUP_SD_HOLD_PX
    alone = dataclasses.replace(nadir, free_intrinsics=[[4, 5]])
    assert bundle.judge_intrinsics(alone)[0][0]["radial"] == pytest.approx(sds[0]["radial"], rel=1e-3)
    scaled = dataclasses.replace(nadir, pixel_sigma=nadir.pixel_sigma * np.sqrt(factor))
    matrix, _counts = bundle.intrinsics_covariance(scaled)
    joint = bundle.corner_sd(model, params, width, height, [4, 5], matrix[2:, 2:])  # of fx, fy, k1, k2: the focal free
    assert joint > 5 * sds[0]["radial"] and joint > bundle.GROUP_SD_HOLD_PX
    # nothing judged is free: the core is not asked
    fixed = dataclasses.replace(nadir, free_intrinsics=[[]])
    assert bundle.judge_intrinsics(fixed) == ([dict.fromkeys(bundle.JUDGED)], [{}], None)


@needs_core
def test_without_a_covariance_the_group_being_judged_is_held_and_the_next_one_judged_without_it(monkeypatch):
    """A normal matrix that cannot be inverted with the focal length free (photos looking straight down, nothing
    that places the block): the focal length is held for want of a covariance, and the radial terms are then
    judged in the adjustment without it. A core that never gives a covariance: every group is held."""
    nadir = _drone_block(0.0, "none")
    real = bundle.intrinsics_covariance
    calls = []

    def singular_with_the_focal(pb, **kwargs):
        calls.append(list(pb.free_intrinsics[0]))
        if 0 in pb.free_intrinsics[0]:
            raise RuntimeError("pivot 99 of the reduced system is not positive")
        return real(pb, **kwargs)

    monkeypatch.setattr(bundle, "intrinsics_covariance", singular_with_the_focal)
    sds, held, _factor = bundle.judge_intrinsics(nadir)
    assert calls == [[0, 1, 4, 5], [4, 5]]
    assert held[0]["focal"] == ("no covariance", "pivot 99 of the reduced system is not positive")
    assert sds[0]["focal"] is None and sds[0]["radial"] > 0
    assert ("radial" in held[0]) == (sds[0]["radial"] > bundle.GROUP_SD_HOLD_PX)

    def never(pb, **kwargs):
        raise RuntimeError("pivot 3 of the reduced system is not positive")

    monkeypatch.setattr(bundle, "intrinsics_covariance", never)
    sds, held, _factor = bundle.judge_intrinsics(_drone_block(0.0, "none", free=range(8)))
    why = ("no covariance", "pivot 3 of the reduced system is not positive")
    assert held[0] == dict.fromkeys(bundle.JUDGED, why) and sds[0] == dict.fromkeys(bundle.JUDGED)


@needs_core
def test_colmap_holds_a_group_for_every_camera_or_for_none():
    """``together``: two cameras of which one - on one photo that sees the middle of its frame alone - does not
    determine its focal length nor what its radial terms do at the corner. The other, which does, is held with it,
    and the reason names the camera it follows; without ``together`` each camera keeps its own verdict."""
    rng = np.random.default_rng(5)
    pb, truth = bundle_problems.block(rng, model="RADIAL", tilt=bundle_problems.TILT)
    pb = bundle._without_held(pb, np.bincount(pb.obs_point, minlength=len(pb.points)) >= 2)
    pb.obs_xy = pb.obs_xy + rng.normal(0, NOISE, pb.obs_xy.shape)
    pb.gcps = [(k, truth["markers"][k].copy(), np.full(3, 0.002)) for k in range(5)]
    last = len(pb.centres) - 1
    _model, params, _width, _height = pb.cameras[0]
    keep = (pb.obs_image != last) | (np.linalg.norm(pb.obs_xy - np.array(params[1:3]), axis=1) < 200.0)
    pb.obs_image, pb.obs_point, pb.obs_xy = pb.obs_image[keep], pb.obs_point[keep], pb.obs_xy[keep]
    keep = pb.mobs_image != last
    pb.mobs_image, pb.mobs_marker, pb.mobs_xy = pb.mobs_image[keep], pb.mobs_marker[keep], pb.mobs_xy[keep]
    assert 20 < int((pb.obs_image == last).sum()) < 60
    pb.cameras = [pb.cameras[0], pb.cameras[0]]
    pb.image_camera = np.array([0] * last + [1])
    pb.free_intrinsics = [[0, 3, 4], [0, 3, 4]]

    sds, held, _factor = bundle.judge_intrinsics(pb)
    assert held[0] == {} and 0 < sds[0]["focal"] < bundle.FOCAL_SD_HOLD and 0 < sds[0]["radial"] < 0.5
    assert held[1] == {"focal": ("sd", sds[1]["focal"]), "radial": ("sd", sds[1]["radial"])}
    assert sds[1]["focal"] > 0.02 and sds[1]["radial"] > 100.0

    sds, held, _factor = bundle.judge_intrinsics(pb, together=True)
    assert held[0] == {"focal": ("with", 1), "radial": ("with", 1)}
    assert held[1] == {"focal": ("sd", sds[1]["focal"]), "radial": ("sd", sds[1]["radial"])}
    assert 0 < sds[0]["focal"] < bundle.FOCAL_SD_HOLD and 0 < sds[0]["radial"] < 0.5  # its own figures stay


@needs_core
def test_the_sd_is_marginal_over_what_the_problem_frees():
    """With the distortion and the principal point held the focal length of the nadir block looks far better
    determined than it is with them free: the sd is taken with every group the adjustment frees."""
    alone, radial, everything = (_sd(_drone_block(0.0, free=free)) for free in ([0, 1], None, range(8)))
    assert alone < radial <= everything
    # a camera whose focal length is not free has no sd of it: the groups it frees are judged
    sds, _held, factor = bundle.judge_intrinsics(_drone_block(0.0, free=[4, 5]))
    assert sds[0]["focal"] is None and sds[0]["radial"] > 0 and factor == pytest.approx(NOISE ** 2, rel=0.05)


@needs_core
def test_the_sd_of_a_block_nothing_places_and_of_one_held_by_positions_with_a_bias():
    """A block nothing places has no gauge of its own: the first photo holds its place and its orientation and a
    position of the farthest one, as loose as the block is wide, its size - a gauge that adds
    nothing the block does not have (the two photos :func:`bundle.problem_from_chunk` holds are more than a gauge:
    the sd with them is smaller). Positions with a bias per flight and no control point: the bias of the first
    flight is held at zero. Either way the sd is the block's: small when the photos are pitched, large - or no
    covariance at all - when they look straight down."""
    free = _drone_block(bundle_problems.TILT, "none")
    assert free.fixed_images == {0, 17} and not free.absolute
    sd = _sd(free)
    assert 0 < sd < bundle.FOCAL_SD_HOLD
    held = np.sqrt(bundle.intrinsics_covariance(free)[0].diagonal()[:2].max() * bundle.tie_variance_factor(free))
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
        sds.append(_sd(small))
    assert sds[2] == pytest.approx(sds[1], rel=1e-4)  # ten times looser: the same
    assert sds[0] == pytest.approx(sds[1], rel=1e-3)  # ten times tighter: a ten thousandth smaller
    monkeypatch.undo()
    assert bundle.GAUGE_SIGMA_EXTENTS <= 1.0
    large = _large_block(10, 40)
    assert len(large.centres) == 400 and len(large.obs_image) > 50000
    probe, _flight = bundle._with_gauge(large)
    assert probe.fixed_images == {0} and len(probe.gnss) == 1
    variances = bundle.intrinsics_covariance(probe)[0].diagonal()  # at a pixel of accuracy: no factor helps
    assert 0 < np.sqrt(variances[0]) / bundle_problems.F < 0.002
    sds, held, factor = bundle.judge_intrinsics(large)
    assert 0 < sds[0]["focal"] < 0.001 and factor == pytest.approx(NOISE ** 2, rel=0.5)
    assert 0 < sds[0]["radial"] < 0.1 and held == [{}]


@needs_core
def test_what_holds_nothing_does_not_break_the_covariance():
    """What an adjustment carries without being held by it must not make the normal matrix singular - the sd would
    then be missing for a block that determines its focal length. A photo with fewer than three observations has
    no pose of its own: it is held as it is. Control points whose markers no photo measures place nothing: the
    block gets the gauge of a free one. Scale bars between such markers give no size: the position that holds the
    size of a free block is added."""
    pitched = _drone_block(bundle_problems.TILT)
    sd = _sd(pitched)
    assert 0 < sd < bundle.FOCAL_SD_HOLD
    # a photo that lost its tie points and its markers (cleaned away): the sd of the others' block
    blind = dataclasses.replace(pitched)
    last = len(blind.centres) - 1
    keep, mkeep = blind.obs_image != last, blind.mobs_image != last
    blind.obs_image, blind.obs_point, blind.obs_xy = blind.obs_image[keep], blind.obs_point[keep], blind.obs_xy[keep]
    blind.mobs_image, blind.mobs_marker = blind.mobs_image[mkeep], blind.mobs_marker[mkeep]
    blind.mobs_xy = blind.mobs_xy[mkeep]
    assert _sd(blind) == pytest.approx(sd, rel=0.2)
    probe, _flight = bundle._with_gauge(blind)
    assert probe.fixed_images == {last}
    # control points that no photo measures: nothing places the block
    unseen = dataclasses.replace(pitched, mobs_image=np.zeros(0, int), mobs_marker=np.zeros(0, int),
                                 mobs_xy=np.zeros((0, 2)))
    assert unseen.absolute  # five control points are stated
    free = _sd(dataclasses.replace(unseen, gcps=[], markers=np.zeros((0, 3))))  # its tie points alone
    assert 0 < free < bundle.FOCAL_SD_HOLD
    assert _sd(unseen) == pytest.approx(free, rel=1e-6)
    probe, _flight = bundle._with_gauge(unseen)
    assert probe.fixed_images == {0} and len(probe.gnss) == 1 and len(probe.gcps) == 5
    # scale bars between markers no photo measures: no size, and no marker to adjust - the gauge of a free block
    bars = dataclasses.replace(unseen, gcps=[], bars=[(0, 2, 30.0, 0.001), (1, 3, 30.0, 0.001)])
    assert _sd(bars) == pytest.approx(free, rel=1e-6)
    probe, _flight = bundle._with_gauge(bars)
    assert len(probe.markers) == 0 and probe.bars == [] and len(probe.gnss) == 1
    # a bar between two measured markers gives the size: no position is added, and the sd is the free block's
    sized = dataclasses.replace(pitched, gcps=[], bars=[(0, 2, float(np.linalg.norm(pitched.markers[0]
                                                                                    - pitched.markers[2])), 0.001)])
    probe, _flight = bundle._with_gauge(sized)
    assert probe.fixed_images == {0} and probe.gnss == [] and len(probe.bars) == 1
    assert _sd(sized) == pytest.approx(free, rel=0.05)


# ------------------------------------------------------------------- the optimisation of the cameras
ENGINES = ("colmap", "ppg")  # the two formulations of optimize_cameras
GPS = dict(accuracy=(5.0, 5.0, 10.0), status=None)  # positions of a drone without RTK: they hold nothing
PITCH = 0.4  # rad: the photos of a block that determines its focal length lean this much
NOTHING_HELD = {"focal": [], "tangential": [], "principal_point": [], "radial": []}


def _block(folder, pitch: float = 0.0):
    """The synthetic block of the optimisation tests (5 strips of 8 photos 40 m above ground with 3 dm of relief, a
    COLMAP model with half a pixel of noise) with coarse positions, five control points on its ground and the
    fifth of them, in the middle, a check point; ``pitch``: its photos lean forward, the strips opposite ways."""
    doc, ch = _rtk_block(folder, pitch=pitch, **GPS)
    markers = _add_control_points(ch)
    markers[-1].checkpoint = True
    gcp.update_transform(ch)
    return doc, ch


def _calibration(ch) -> list:
    return [list(s.params) for s in ch.sensors.values()]


def _spy_on_colmap(monkeypatch, after=None, before=None) -> list:
    """Every adjustment the program asks of COLMAP noted as ``(focal refined, principal point refined, distortion
    refined, cameras held, fixed points)``; ``before(constant points)`` runs before each and ``after(rec, call
    number, constant points)`` after it."""
    calls = []
    real = sfm._adjust

    def adjust(rec, opts, constant_points=(), fixed_cameras=()):
        if before is not None:
            before(constant_points)
        calls.append((bool(opts.refine_focal_length), bool(opts.refine_principal_point),
                      bool(opts.refine_extra_params), set(fixed_cameras), len(constant_points)))
        real(rec, opts, constant_points, fixed_cameras)
        if after is not None:
            after(rec, len(calls), constant_points)

    monkeypatch.setattr(sfm, "_adjust", adjust)
    return calls


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_a_nadir_grid_keeps_what_it_does_not_determine_and_a_pitched_one_refines_it(tmp_path, engine):
    """Photos looking straight down over nearly flat ground cannot tell the focal length from the height, whatever
    the control points on that ground say, nor - with that focal length held - their radial terms from a dome of
    the block, which control points at its corners do not see: the optimisation holds both for that sensor, the
    report says so in numbers, and the block still bends to its control points. The point in the middle, a check
    point, ends where an optimisation with the whole calibration fixed leaves it. Pitched photos determine both:
    the calibration is refined as before."""
    _doc, ch = _block(tmp_path / "nadir")
    before = _calibration(ch)
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    assert check["hold_undetermined"] is True and check["checked"] is True and check["reason"] is None
    assert check["held"] == {"focal": [0], "tangential": [], "principal_point": [], "radial": [0]}
    assert len(check["focal_sd"]) == len(check["radial_sd"]) == 1 and check["principal_point_sd"] == []  # not asked
    (sensor, sd), radial = check["focal_sd"][0], check["radial_sd"][0][1]
    assert sensor == 0 and check["radial_sd"][0][0] == 0 and sd > 0.05 > bundle.FOCAL_SD_HOLD
    assert 2.0 < radial < 5.0 and bundle.GROUP_SD_HOLD_PX == 0.5  # 3.2 px at the corner of the photo
    assert check["notes"] == [f"sensor 0: focal held: undetermined, sd {100 * sd:.2f} %",
                              f"sensor 0: radial held: undetermined, sd {radial:.2f} px at the image corner"]
    assert 0.2 < check["variance_factor"] < 0.4  # half a pixel of noise at a pixel of accuracy
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED
    after = _calibration(ch)
    assert after[0][:6] == before[0][:6]  # the focal length, the principal point (not asked), the radial terms
    if engine == "ppg":  # our formulation has the tangential terms, and this block determines them (0.08 px)
        assert check["tangential_sd"][0][0] == 0 and 0 < check["tangential_sd"][0][1] < 0.2
        assert after[0][6:] != before[0][6:] and np.abs(after[0][6:]).max() < 1e-3
    else:  # COLMAP's has none here: nothing of the calibration is left to refine
        assert check["tangential_sd"] == [] and after == before
    assert rep["control_points"] == 4 and rep["after"]["control"] < 0.005
    # the point in the middle: within the 13 mm of the gate of the weak ring - and where the whole calibration
    # fixed leaves it, 2.8 mm (the radial terms refined with the focal length held left it 62 mm off under
    # COLMAP's formulation and 44 mm under ours; everything freed, 115 and 278 mm)
    assert rep["after"]["check"] < 0.013
    _doc, fixed = _block(tmp_path / "fixed")
    fixed.sensors[0].fixed = True
    fixed.optimize_cameras(engine=engine)
    held = fixed.meta["sfm"]["optimize"]
    assert held["intrinsics"] == sfm._check_record(True, sfm.NOTHING_TO_JUDGE) and _calibration(fixed) == before
    assert rep["after"]["check"] == pytest.approx(held["after"]["check"], abs=0.0005)
    assert held["after"]["check"] < 0.004

    _doc, ch = _block(tmp_path / "pitched", pitch=PITCH)
    before = _calibration(ch)
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    assert check["checked"] is True and check["held"] == NOTHING_HELD and check["notes"] == []
    assert 0 < check["focal_sd"][0][1] < bundle.FOCAL_SD_HOLD
    assert 0 < check["radial_sd"][0][1] < bundle.GROUP_SD_HOLD_PX
    assert len(check["tangential_sd"]) == (1 if engine == "ppg" else 0)
    assert rep["focal_held"] is None
    after = _calibration(ch)
    assert after != before and after[0][0] == pytest.approx(before[0][0], rel=0.01)  # refined, near the true one
    assert after[0][0] != before[0][0] and after[0][4] == pytest.approx(K1, abs=0.003)
    assert rep["after"]["control"] < 0.005 and rep["after"]["check"] < 0.02


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_a_pitched_grid_asked_for_its_principal_point_keeps_it_and_its_focal_length(tmp_path, engine):
    """What the fixed order gives where two groups trade (focal length, tangential terms, principal point, radial
    terms; what is held leaves the adjustment before the next group is judged). Pitched photos asked for their
    principal point: with it free the focal length is beyond its limit (0.55 % with COLMAP's formulation, 1.4 %
    with ours) and, first in the order, is held; the principal point, judged without it, is beyond its own (1.2
    and 1.4 px) and is held too; the distortion is refined. A rule that gave up the group furthest beyond its
    limit first would have held the principal point alone (3.0 and 11 px with the focal length free) and refined
    the focal length, which is determined without it (0.19 % and 0.34 %)."""
    _doc, ch = _block(tmp_path, pitch=PITCH)
    before = _calibration(ch)
    ch.optimize_cameras(engine=engine, fit_principal_point=True)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    assert check["checked"] is True and check["held"] == {**NOTHING_HELD, "focal": [0], "principal_point": [0]}
    focal, principal_point = check["focal_sd"][0][1], check["principal_point_sd"][0][1]
    assert bundle.FOCAL_SD_HOLD < focal < 0.02 and 1.0 < principal_point < 2.0
    assert check["notes"] == [f"sensor 0: focal held: undetermined, sd {100 * focal:.2f} %",
                              f"sensor 0: principal point held: undetermined, sd {principal_point:.2f} px at the "
                              "image corner"]
    assert 0 < check["radial_sd"][0][1] < bundle.GROUP_SD_HOLD_PX
    assert all(0 < sd < bundle.GROUP_SD_HOLD_PX for _sensor, sd in check["tangential_sd"])
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED
    after = _calibration(ch)
    assert after[0][:4] == before[0][:4] and after[0][4:6] != before[0][4:6]  # the distortion alone is refined
    assert rep["after"]["control"] < 0.005 and rep["after"]["check"] < 0.006  # 3.2 mm and 2.6 mm at the middle


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_freeing_what_the_block_does_not_determine_is_the_users_choice(tmp_path, monkeypatch, engine):
    """``hold_undetermined=False``: the optimisation of before - nothing is asked of the core, every calibration
    is refined as the other options say, on the nadir block too."""
    _doc, ch = _block(tmp_path)
    before = _calibration(ch)
    asked = []
    monkeypatch.setattr(bundle, "judge_intrinsics", lambda pb, **kwargs: asked.append(pb))
    calls, problems = _spy_on_colmap(monkeypatch), []
    real = bundle.adjust
    monkeypatch.setattr(bundle, "adjust", lambda pb, **kwargs: problems.append(pb) or real(pb, **kwargs))
    ch.optimize_cameras(engine=engine, hold_undetermined=False)
    rep = ch.meta["sfm"]["optimize"]
    assert rep["intrinsics"] == sfm._check_record(False) == {
        "hold_undetermined": False, "checked": False, "reason": None, "variance_factor": None, "focal_sd": [],
        "tangential_sd": [], "principal_point_sd": [], "radial_sd": [], "held": NOTHING_HELD, "notes": []}
    assert rep["focal_held"] is None and asked == []
    assert _calibration(ch)[0][0] != before[0][0]
    if engine == "colmap":
        assert calls and all(call[:4] == (True, False, True, set()) for call in calls)
    else:
        assert [pb.free_intrinsics for pb in problems] == [[[0, 1, 4, 5, 6, 7]]]


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_without_the_core_the_calibration_is_refined_as_before_and_the_log_says_so_once(tmp_path, monkeypatch,
                                                                                        caplog, engine):
    """Where the core cannot give the covariance (missing, older than API 18, switched off) nothing changes: the
    calibration is refined, and one line of the log at INFO says that the check needs the native core."""
    _doc, ch = _block(tmp_path)
    before = _calibration(ch)
    monkeypatch.setattr(bundle, "covariance_ready", lambda pb: bundle.INTRINSICS_MISSING)
    with caplog.at_level(logging.INFO, logger="pyarchinit_photogrammetry.engine.sfm"):
        ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    assert rep["intrinsics"] == sfm._check_record(True, bundle.INTRINSICS_MISSING)
    assert rep["intrinsics"]["checked"] is False and rep["intrinsics"]["reason"] == bundle.INTRINSICS_MISSING
    assert rep["focal_held"] is None and _calibration(ch)[0][0] != before[0][0]
    lines = [r for r in caplog.records if "native core" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert "API 18" in lines[0].getMessage()


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_a_block_without_a_covariance_keeps_its_calibration(tmp_path, monkeypatch, engine):
    """A normal matrix that cannot be inverted (an intrinsic the block does not hold at all) is the same answer:
    each group that would be refined is held, and the notes say why."""
    _doc, ch = _block(tmp_path, pitch=PITCH)
    before = _calibration(ch)

    def no_covariance(pb, **kwargs):
        raise RuntimeError("pivot 7 of the reduced system is not positive")

    monkeypatch.setattr(bundle, "intrinsics_covariance", no_covariance)
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    groups = ["focal", "tangential", "radial"] if engine == "ppg" else ["focal", "radial"]
    assert check["checked"] is True and 0.2 < check["variance_factor"] < 0.4
    assert check["held"] == {group: ([0] if group in groups else []) for group in bundle.JUDGED}
    assert all(check[f"{group}_sd"] == [[0, None]] for group in groups) and check["principal_point_sd"] == []
    why = "undetermined, no covariance (pivot 7 of the reduced system is not positive)"
    assert check["notes"] == [f"sensor 0: {group} held: {why}" for group in groups]
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED
    assert _calibration(ch) == before and rep["after"]["control"] < 0.005


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("group", ["focal", "tangential", "principal_point", "radial"])
def test_what_the_check_holds_stays_out_of_the_adjustment(tmp_path, monkeypatch, engine, group):
    """Each group is held on its own verdict, whatever the verdicts of the others: the group the check holds is
    the one thing the adjustment does not refine, the record says which and why."""
    _doc, ch = _block(tmp_path, pitch=PITCH)
    before = _calibration(ch)
    sds = {"focal": 0.001, "tangential": 0.11, "principal_point": 0.22, "radial": 0.33}
    sds[group] = 0.02 if group == "focal" else 2.0
    verdict = ([sds], [{group: ("sd", sds[group])}], 0.25)
    monkeypatch.setattr(bundle, "judge_intrinsics", lambda pb, together=False: verdict)
    calls, problems = _spy_on_colmap(monkeypatch), []
    real = bundle.adjust

    def adjust(pb, **kwargs):
        problems.append(copy.deepcopy(pb.free_intrinsics))
        return real(pb, **kwargs)

    monkeypatch.setattr(bundle, "adjust", adjust)
    ch.optimize_cameras(engine=engine, fit_principal_point=True)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    has = engine == "ppg" or group != "tangential"  # COLMAP's camera of this block has no tangential terms
    assert check["held"] == {name: ([0] if name == group and has else []) for name in bundle.JUDGED}
    assert check["focal_sd"] == [[0, sds["focal"]]] and check["principal_point_sd"] == [[0, sds["principal_point"]]]
    assert check["radial_sd"] == [[0, sds["radial"]]] and check["variance_factor"] == 0.25
    assert check["tangential_sd"] == ([[0, sds["tangential"]]] if engine == "ppg" else [])
    words = {"focal": "focal held: undetermined, sd 2.00 %",
             "tangential": "tangential held: undetermined, sd 2.00 px at the image corner",
             "principal_point": "principal point held: undetermined, sd 2.00 px at the image corner",
             "radial": "radial held: undetermined, sd 2.00 px at the image corner"}
    assert check["notes"] == ([f"sensor 0: {words[group]}"] if has else [])
    assert rep["focal_held"] == (sfm.FOCAL_HELD_UNDETERMINED if group == "focal" else None)
    after = _calibration(ch)
    index = {"focal": [0, 1], "principal_point": [2, 3], "radial": [4, 5], "tangential": [6, 7]}
    for name, columns in index.items():
        same = [after[0][k] == before[0][k] for k in columns]
        if name == group and has:
            assert all(same), name
        elif engine == "ppg" or name != "tangential":
            assert not any(same), name  # refined
    if engine == "colmap":  # (focal, principal point, distortion) of every adjustment of the run
        flags = {"focal": (False, True, True), "tangential": (True, True, True),
                 "principal_point": (True, False, True), "radial": (True, True, False)}[group]
        assert calls and all(call[:3] == flags for call in calls)
    else:
        assert problems == [[[k for k in range(8) if k not in index[group]]]]


@needs_core
def test_colmap_refines_the_distortion_whole_or_not_at_all(tmp_path, monkeypatch):
    """COLMAP's adjustment refines the distortion of its cameras whole: where the check holds the tangential terms
    of a camera that has them (a calibration gave them: its model keeps them) and not its radial terms, the radial
    terms are held with them, the notes say so, and the adjustment refines no distortion."""
    _doc, ch = _block(tmp_path, pitch=PITCH)
    rec = sfm.load_reconstruction(ch)  # its camera with the tangential terms: not reduced to one focal length
    assert [cam.model.name for cam in rec.cameras.values()] == ["OPENCV"]
    verdict = ([{"focal": 0.001, "tangential": 1.62, "principal_point": None, "radial": 0.12}],
               [{"tangential": ("sd", 1.62)}], 0.25)
    monkeypatch.setattr(bundle, "judge_intrinsics", lambda pb, together=False: verdict)
    record, groups = sfm._colmap_calibration_check(ch, rec, hold_undetermined=True, positions=False,
                                                   use_markers=True, fit_principal_point=False, marker_weight=4)
    assert groups == {"tangential", "radial"}
    assert record["held"] == {"focal": [], "tangential": [0], "principal_point": [], "radial": [0]}
    assert record["notes"] == ["sensor 0: tangential held: undetermined, sd 1.62 px at the image corner",
                               "sensor 0: radial held: with the tangential terms (COLMAP's adjustment refines "
                               "the distortion whole or not at all)"]
    monkeypatch.setattr(sfm, "_colmap_calibration_check", lambda *args, **kwargs: (record, set(groups)))
    calls = _spy_on_colmap(monkeypatch)
    ch.optimize_cameras(engine="colmap")
    assert calls and all(call[:3] == (True, False, False) for call in calls)
    assert ch.meta["sfm"]["optimize"]["intrinsics"] == record


@needs_core
def test_the_guard_runs_our_adjustment_again_with_what_ran_away_held(tmp_path, monkeypatch):
    """A focal length that the adjustment moves by more than :data:`bundle.FOCAL_MOVE_HOLD` of its start, on a
    block whose covariance said it was determined (the valley is not a straight line): the adjustment runs again
    from the same start with that focal length held, and the notes say by how much it had moved. A first radial
    term that changes by more than :data:`bundle.K1_MOVE_HOLD`: again, with the radial terms held."""
    real = bundle.adjust

    def sliding(change):
        problems = []

        def adjust(pb, **kwargs):
            problems.append(copy.deepcopy(pb.free_intrinsics))
            res = real(pb, **kwargs)
            if len(problems) == 1:  # the first run slides along the valley
                start = np.asarray(pb.cameras[0][1], float)
                res.camera_params[0] = change(np.array(res.camera_params[0], float), start)
            return res

        monkeypatch.setattr(bundle, "adjust", adjust)
        return problems

    def longer(params, start):
        params[:2] = 1.3 * start[:2]
        return params

    def bent(params, start):
        params[4] = start[4] + 0.8
        return params

    _doc, ch = _block(tmp_path / "focal", pitch=PITCH)
    before = _calibration(ch)
    problems = sliding(longer)
    ch.optimize_cameras(engine="ppg")
    rep = ch.meta["sfm"]["optimize"]
    assert problems == [[[0, 1, 4, 5, 6, 7]], [[4, 5, 6, 7]]]
    check = rep["intrinsics"]
    assert check["held"] == {**NOTHING_HELD, "focal": [0]}
    assert 0 < check["focal_sd"][0][1] < bundle.FOCAL_SD_HOLD
    assert check["notes"] == ["sensor 0: focal held: moved 30.0 % in the optimisation, run again with it held"]
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED
    after = _calibration(ch)
    assert after[0][:2] == before[0][:2] and after[0][4:6] != before[0][4:6] and rep["after"]["control"] < 0.005

    _doc, ch = _block(tmp_path / "radial", pitch=PITCH)
    before = _calibration(ch)
    problems = sliding(bent)
    ch.optimize_cameras(engine="ppg")
    rep = ch.meta["sfm"]["optimize"]
    assert problems == [[[0, 1, 4, 5, 6, 7]], [[0, 1, 6, 7]]]
    check = rep["intrinsics"]
    assert check["held"] == {**NOTHING_HELD, "radial": [0]}
    assert check["notes"] == ["sensor 0: radial held: k1 moved by 0.80 in the optimisation, run again with it held"]
    assert rep["focal_held"] is None
    after = _calibration(ch)
    assert after[0][4:6] == before[0][4:6] and after[0][0] != before[0][0]


@needs_core
def test_the_guard_runs_colmaps_adjustment_again_from_the_start(tmp_path, monkeypatch):
    """The same guard on COLMAP's formulation, whose free pass and steps towards the control points change the
    chunk before the model is written: the chunk goes back to where it started (the cameras, the calibration, the
    georeference, the markers), and the whole optimisation runs again with the focal lengths - or the distortion -
    held."""
    def run(folder, slide):
        _doc, ch = _block(folder, pitch=PITCH)
        state = {"before": _calibration(ch), "transform": np.array(ch.transform, float), "starts": [],
                 "markers": {m.key: np.array(m.position, float) for m in ch.markers},
                 "centres": {c.key: np.array(c.center, float) for c in ch.cameras}}
        slid = []

        def after(rec, number, constant_points):
            if constant_points and not slid:  # the first run slides along the valley on its control points
                slid.append(1)
                for cam in rec.cameras.values():
                    cam.params = slide(list(cam.params))

        def before(constant_points):
            if not constant_points:  # the free pass, the first adjustment of a run: the chunk as it is then
                state["starts"].append((np.array(ch.transform, float), _calibration(ch),
                                        {m.key: np.array(m.position, float) for m in ch.markers},
                                        {c.key: np.array(c.center, float) for c in ch.cameras}))

        calls = _spy_on_colmap(monkeypatch, after, before)
        ch.optimize_cameras(engine="colmap")
        monkeypatch.undo()
        return ch, state, calls

    ch, state, calls = run(tmp_path / "focal", lambda p: [1.3 * p[0], *p[1:]])
    rep = ch.meta["sfm"]["optimize"]
    free_passes = [call for call in calls if call[4] == 0]
    assert [call[:4] for call in free_passes] == [(True, False, True, set()), (False, False, True, set())]
    assert len(state["starts"]) == 2
    for transform, calibration, markers, centres in state["starts"]:  # each run starts from the chunk as it was
        assert np.allclose(transform, state["transform"], atol=1e-12) and calibration == state["before"]
        assert all(np.allclose(markers[k], state["markers"][k], atol=1e-9) for k in markers)
        assert all(np.allclose(centres[k], state["centres"][k], atol=1e-12) for k in centres)
    check = rep["intrinsics"]
    assert check["held"] == {**NOTHING_HELD, "focal": [0]}
    assert 0 < check["focal_sd"][0][1] < bundle.FOCAL_SD_HOLD
    assert check["notes"] == ["sensor 0: focal held: moved 30.0 % in the optimisation, run again with it held"]
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED
    after = _calibration(ch)
    assert after[0][:2] == state["before"][0][:2] and after[0][4:6] != state["before"][0][4:6]
    assert rep["control_points"] == 4 and rep["after"]["control"] < 0.005

    ch, state, calls = run(tmp_path / "radial", lambda p: [*p[:3], p[3] + 0.8, *p[4:]])  # RADIAL: f, cx, cy, k1, k2
    rep = ch.meta["sfm"]["optimize"]
    free_passes = [call for call in calls if call[4] == 0]
    assert [call[:4] for call in free_passes] == [(True, False, True, set()), (True, False, False, set())]
    check = rep["intrinsics"]
    assert check["held"] == {**NOTHING_HELD, "radial": [0]}
    assert check["notes"] == ["sensor 0: radial held: k1 moved by 0.80 in the optimisation, run again with it held"]
    assert rep["focal_held"] is None
    after = _calibration(ch)
    assert after[0][4:] == state["before"][0][4:] and after[0][0] != state["before"][0][0]


def test_colmap_holds_a_group_for_every_sensor_or_for_none():
    """COLMAP's adjustment refines the focal lengths of all its cameras or of none: when the check holds the focal
    length of one sensor, the other sensors that would refine theirs are held with it and the notes say so; a
    sensor that would not refine it (a fixed calibration) is not named. Its distortion likewise, and whole: radial
    terms that are held take the tangential terms of the sensors that have them, and the other way round."""
    record = sfm._check_record(True)
    sfm._hold(record, "focal", 2, "undetermined, sd 11.42 %")
    sfm._for_every_sensor(record, "focal", {0: True, 2: True, 5: False})
    assert record["held"] == {**NOTHING_HELD, "focal": [2, 0]}
    assert record["notes"] == ["sensor 2: focal held: undetermined, sd 11.42 %",
                               "sensor 0: focal held: with sensor 2 (COLMAP's adjustment holds it for every sensor "
                               "or for none)"]
    sfm._for_every_sensor(record, "principal_point", {0: True, 2: True})  # nothing held: nothing to follow
    assert record["held"]["principal_point"] == [] and len(record["notes"]) == 2
    # the distortion: nothing of it held, both kinds held, or a kind no sensor has - nothing to add
    every = {0: True, 2: True}
    assert sfm._whole_distortion(record, {"focal"}, every, every) == {"focal"}
    assert sfm._whole_distortion(record, {"radial", "tangential"}, every, every) == {"radial", "tangential"}
    assert sfm._whole_distortion(record, {"radial"}, {0: False, 2: False}, every) == {"radial"}
    assert len(record["notes"]) == 2
    sfm._hold(record, "radial", 0, "k1 moved by 0.80 in the optimisation, run again with it held")
    sfm._hold(record, "radial", 2, "with sensor 0 (COLMAP's adjustment holds it for every sensor or for none)")
    assert sfm._whole_distortion(record, {"radial"}, {0: False, 2: True}, every) == {"radial", "tangential"}
    assert record["held"] == {"focal": [2, 0], "tangential": [2], "principal_point": [], "radial": [0, 2]}
    assert record["notes"][-1] == ("sensor 2: tangential held: with the radial terms (COLMAP's adjustment refines "
                                   "the distortion whole or not at all)")
    other = sfm._check_record(True)
    sfm._hold(other, "tangential", 1, "undetermined, sd 1.62 px at the image corner")
    assert sfm._whole_distortion(other, {"focal", "tangential"}, {1: True}, {1: True, 3: True}) == {
        "focal", "tangential", "radial"}
    assert other["held"]["radial"] == [1, 3] and len(other["notes"]) == 3


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_a_fixed_calibration_asks_nothing_of_the_core(tmp_path, monkeypatch, engine):
    """Where the adjustment refines no intrinsic there is nothing to judge: the covariance is not asked for."""
    asked = []
    real = bundle.intrinsics_covariance
    monkeypatch.setattr(bundle, "intrinsics_covariance", lambda pb, **kwargs: asked.append(1) or real(pb, **kwargs))
    _doc, ch = _block(tmp_path)
    ch.sensors[0].fixed = True
    before = _calibration(ch)
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    assert rep["intrinsics"] == sfm._check_record(True, sfm.NOTHING_TO_JUDGE) and asked == []
    assert sfm.NOTHING_TO_JUDGE == "no intrinsic is refined"
    assert _calibration(ch) == before and rep["focal_held"] is None


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("principal_point", [False, True])
def test_where_the_positions_hold_the_block_the_rest_of_the_calibration_is_judged(tmp_path, engine, principal_point):
    """Accurate positions that hold the block keep the focal length of the alignment as before; the distortion,
    which is still refined, and a principal point that was asked for are judged on the adjustment with those
    positions - which determines them: they are refined."""
    _doc, ch = _rtk_block(tmp_path)
    before = _calibration(ch)
    ch.optimize_cameras(engine=engine, fit_principal_point=principal_point)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    assert rep["focal_held"] == sfm.FOCAL_HELD_BY_POSITIONS and rep["gnss"]["photos"] == 40
    assert check["checked"] is True and check["focal_sd"] == []  # no focal length to judge
    assert check["held"] == NOTHING_HELD and check["notes"] == []
    assert 0 < check["radial_sd"][0][1] < 0.2 < bundle.GROUP_SD_HOLD_PX  # 0.08 px
    assert len(check["tangential_sd"]) == (1 if engine == "ppg" else 0)
    after = _calibration(ch)
    assert after[0][0] == pytest.approx(before[0][0], rel=1e-9)
    assert after[0][4] != before[0][4] and after[0][4] == pytest.approx(K1, abs=0.002)  # the distortion is refined
    if principal_point:
        assert 0 < check["principal_point_sd"][0][1] < 0.4 < bundle.GROUP_SD_HOLD_PX  # 0.16 and 0.26 px
        assert after[0][2:4] != before[0][2:4] and after[0][2:4] == pytest.approx(before[0][2:4], abs=1.0)
    else:
        assert check["principal_point_sd"] == [] and after[0][2:4] == before[0][2:4]


@needs_core
def test_a_block_nothing_places_has_its_distortion_judged(tmp_path):
    """Our adjustment of a block that nothing places keeps the focal length as before (it cannot be told from the
    depth); the distortion is judged on that block with the gauge of a free one: photos looking straight down do
    not determine the radial terms, pitched ones do."""
    for name, pitch in (("nadir", 0.0), ("pitched", PITCH)):
        _doc, ch = _rtk_block(tmp_path / name, pitch=pitch, **GPS)
        for cam in ch.cameras:
            cam.reference.enabled = False
        before = _calibration(ch)
        ch.optimize_cameras(engine="ppg")
        rep = ch.meta["sfm"]["optimize"]
        check = rep["intrinsics"]
        assert rep["focal_held"] == sfm.FOCAL_HELD_NO_REFERENCES and check["checked"] is True
        assert check["focal_sd"] == [] and check["held"]["focal"] == [] and len(check["radial_sd"]) == 1
        after = _calibration(ch)
        assert after[0][:2] == before[0][:2]
        if pitch:
            assert check["held"] == NOTHING_HELD and 0 < check["radial_sd"][0][1] < bundle.GROUP_SD_HOLD_PX
            assert after[0][4:6] != before[0][4:6]
        else:
            assert check["held"] == {**NOTHING_HELD, "radial": [0]} and check["radial_sd"][0][1] > 2.0  # 7.6 px
            assert after[0][4:6] == before[0][4:6]


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_the_record_of_the_check_holds_numbers_and_no_names(tmp_path, engine):
    """What the optimisation keeps of the check is numbers and fixed words: the key of a sensor, never its label,
    the label of a photo or a path."""
    doc, ch = _block(tmp_path)
    ch.sensors[0].label = "Hasselblad of the dig"
    ch.optimize_cameras(engine=engine, fit_principal_point=True)
    record = ch.meta["sfm"]["optimize"]["intrinsics"]
    assert record["notes"] and record["focal_sd"] and record["principal_point_sd"] and record["radial_sd"]
    text = json.dumps(record)
    assert "Hasselblad" not in text and ".jpg" not in text.lower()
    for cam in ch.cameras:
        assert cam.label not in text
    for folder in (doc.path.parent, ch.data_dir, tmp_path):
        assert str(folder) not in text and folder.name not in text
    doc.save()  # the record is saved with the project
    import pyarchinit_photogrammetry as ppg

    assert ppg.Document.open(doc.path).chunk.meta["sfm"]["optimize"]["intrinsics"] == record


def test_the_reason_the_focal_length_was_held_counts_the_sensors():
    """``report["focal_held"]``: the reason the optimisation already has (the positions hold the block) stays; else
    the check's, with how many of the sensors it judged when it did not hold the focal length of them all. A
    principal point or a distortion held is no reason of the focal length."""
    record = {"held": {"focal": [], "tangential": [1], "principal_point": [0], "radial": [1]},
              "focal_sd": [[0, 0.001], [1, 0.002]]}
    assert sfm._focal_held(None, record) is None
    assert sfm._focal_held(sfm.FOCAL_HELD_BY_POSITIONS, record) == sfm.FOCAL_HELD_BY_POSITIONS
    record = {"held": {**NOTHING_HELD, "focal": [1]}, "focal_sd": [[0, 0.001], [1, 0.2]]}
    assert sfm._focal_held(None, record) == "the block does not determine the focal length of 1 of 2 sensors"
    assert sfm._focal_held(sfm.FOCAL_HELD_BY_POSITIONS, record) == sfm.FOCAL_HELD_BY_POSITIONS
    record = {"held": {**NOTHING_HELD, "focal": [0, 1]}, "focal_sd": [[0, 0.1], [1, 0.2]]}
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
    assert rep["focal_held"] is None and _calibration(ch)[0][0] != before[0][0]

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
    assert "sensor 0: radial held: undetermined, sd " in said and "px at the image corner" in said


def test_the_dialog_offers_the_choice_and_the_window_says_what_was_held(tmp_path, monkeypatch):
    """*Ottimizza camere* has a tick, on by default, for the parts of the calibration the block does not determine;
    the answer reaches the chunk, and the log of the window says for how many sensors the focal length was kept,
    and which other parts of the calibration were held."""
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
        record = sfm._check_record(kwargs["hold_undetermined"])
        if kwargs["hold_undetermined"]:
            record.update(checked=True, focal_sd=[[0, 0.11]],
                          held={"focal": [0], "tangential": [0], "principal_point": [0], "radial": [0]})
        self.meta.setdefault("sfm", {})["optimize"] = {
            "reprojection_error": 0.5, "focal_held": sfm.FOCAL_HELD_UNDETERMINED if kwargs["hold_undetermined"]
            else None, "intrinsics": record}
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
        assert fields["hold"].default is True and "calibrazione" in fields["hold"].label
        assert "solo dove il blocco la determina" in intro and "peggio di mezzo pixel" in intro
        assert calls == [True, False, True]
        said = [e[2] for e in w.log_pane.entries if "Errore di riproiezione" in e[2]]  # the line of each result
        kept = "focale dell'allineamento tenuta: il blocco non la determina (1 sensori su 1)"
        assert [kept in line for line in said] == [True, False, True]
        others = ("parti della calibrazione tenute ferme perché il blocco non le determina: distorsione "
                  "tangenziale, punto principale, distorsione radiale")
        assert [others in line for line in said] == [True, False, True]
        assert not any(line.count("focale dell'allineamento tenuta") > 1 for line in said)
    finally:
        w.setWindowModified(False)
        w.close()


def test_the_report_names_the_parts_of_the_calibration_that_were_held(tmp_path):
    """The parameters of the optimisation in the report (the PDF and its tables): beside the focal length held,
    with its reason, a row for the other parts of the calibration the check held, by their names in both
    languages; no such row when it held none of them, nor for an optimisation that kept no record of them."""
    from pyarchinit_photogrammetry.io import report_content as rc

    def rows(ch, language):
        w = rc.Words(language)
        return [(label, value) for step, label, value in rc.parameter_rows(ch, w) if step == w("step_optimize")]

    _doc, ch = _block(tmp_path)
    record = sfm._check_record(True)
    record.update(checked=True, focal_sd=[[0, 0.11]],
                  held={"focal": [0], "tangential": [], "principal_point": [0], "radial": [0]})
    ch.meta.setdefault("sfm", {})["optimize"] = {"reprojection_error": 0.5, "intrinsics": record,
                                                 "focal_held": sfm.FOCAL_HELD_UNDETERMINED}
    english = rows(ch, "en")
    assert ("Focal length of the alignment held", f"yes ({sfm.FOCAL_HELD_UNDETERMINED})") in english
    assert ("Other parts of the calibration held", "principal point, radial distortion") in english
    assert all(label in {t[1] for t in rc.TEXT.values()} for label, _value in english), english  # no raw key
    assert ("Altre parti della calibrazione tenute", "punto principale, distorsione radiale") in rows(ch, "it")
    record["held"] = {**NOTHING_HELD, "tangential": [0]}
    assert ("Other parts of the calibration held", "tangential distortion") in rows(ch, "en")
    for held in ({**NOTHING_HELD, "focal": [0]}, NOTHING_HELD, {"focal": [0]}):
        record["held"] = held
        assert not any("calibration" in label for label, _value in rows(ch, "en")), held
    del ch.meta["sfm"]["optimize"]["intrinsics"]  # a report of before the check
    assert not any("calibration" in label for label, _value in rows(ch, "en"))


# ------------------------------------------------------------------- the weak ring and the trench
@pytest.fixture(scope="module")
def weak_ring(tmp_path_factory):
    """The weak ring of the gate of the native mapper (``write_weak_ring_dataset`` with its defaults: 80 nadir
    photos along one closed strip over flat ground, 12 targets under it) as ``ppg bench synthetic weak-ring``
    prepares it: matched, aligned by the native mapper, its targets georeferenced with the six of odd code check
    points. ``project``: the path of the project (each test optimises a copy); the first lines of its gate as the
    alignment left them: ``aligned`` (how many photos, by which engine) and ``shape`` (``truth.ring_shape``, before
    any optimisation)."""
    import os
    import types

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
    aligned = (ch.align_cameras(engine="native"), dict(ch.meta["engine"]["align_cameras"]))
    ch.import_reference(root / "set" / "gcp.csv", crs=scene.crs)
    steps.set_check(ch, [str(code) for code in truth.WEAK_RING_CHECK])
    ch.update_transform()
    doc.save()
    return types.SimpleNamespace(project=doc.path, aligned=aligned, shape=truth.ring_shape(doc, scene))


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
def test_the_weak_ring_keeps_the_focal_length_of_its_alignment(weak_ring, tmp_path, engine):
    """The weak ring cannot tell its focal length from its height (the native mapper held it, at the value of the
    EXIF); the optimisation on its six control targets holds it too, with either formulation, and says so. With
    the focal length held the ring determines its radial terms (0.12 px at the corner of the photo), which the
    mapper left at zero: they are refined. It does not determine the tangential terms of our formulation (1.6 px):
    they are held. The cameras stay at their height: before this rule the same optimisation moved the focal length
    ten times and the cameras 131 m up."""
    ch = _copy_of(weak_ring.project, tmp_path / engine).chunk
    before, heights = _calibration(ch), np.array([ch.to_world(c.center)[2] for c in ch.aligned_cameras])
    assert before[0][4:] == [0.0, 0.0, 0.0, 0.0]  # the lens the native mapper left unmodelled
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    sensor = check["focal_sd"][0][0]
    assert check["checked"] is True and check["held"]["focal"] == [sensor] and check["held"]["radial"] == []
    assert check["held"]["principal_point"] == [] and check["principal_point_sd"] == []
    assert check["focal_sd"][0][1] > 0.05  # 10 % (COLMAP's formulation) and 14 % (ours) measured
    assert 0 < check["radial_sd"][0][1] < 0.3 < bundle.GROUP_SD_HOLD_PX  # 0.12 px
    notes = [f"sensor {sensor}: focal held: undetermined, sd {100 * check['focal_sd'][0][1]:.2f} %"]
    if engine == "ppg":
        tangential = check["tangential_sd"][0][1]
        assert check["held"]["tangential"] == [sensor] and tangential > 1.0  # 1.62 px
        notes.append(f"sensor {sensor}: tangential held: undetermined, sd {tangential:.2f} px at the image corner")
    else:
        assert check["held"]["tangential"] == [] and check["tangential_sd"] == []
    assert check["notes"] == notes
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED and rep["control_points"] == 6
    after = _calibration(ch)
    assert after[0][:4] == before[0][:4] and after[0][4] != 0.0  # the focal length exactly, the radial terms refined
    assert after[0][6:] == [0.0, 0.0]
    rose = np.array([ch.to_world(c.center)[2] for c in ch.aligned_cameras]) - heights
    assert np.abs(rose).max() < 0.5  # in the coordinate system: where the alignment put them, the lens fitted


@needs_core
@pytest.mark.slow
@pytest.mark.parametrize("engine", ["auto", "ppg"])
def test_the_weak_ring_meets_the_four_lines_of_its_gate(weak_ring, tmp_path, engine):
    """The gate of the weak ring, whole: every photo aligned by the native mapper with no fallback; the shape of
    the ring within ``WEAK_RING_GATE["shape"]`` of its diameter before any optimisation; and after the
    optimisation - with the formulation the program runs by default, COLMAP's, and with our own - the control
    targets within ``WEAK_RING_GATE["control_m"]`` and the check targets within ``WEAK_RING_GATE["check_m"]``. The
    radial distortion found with the focal length held is the true one."""
    from pyarchinit_photogrammetry.testing.synthetic import WEAK_RING_GATE

    assert weak_ring.aligned == (80, {"requested": "native", "used": "native", "fallback": None})
    assert weak_ring.shape["truth_targets"] >= 6
    assert weak_ring.shape["truth_target_shape"] <= WEAK_RING_GATE["shape"]  # 0.00037 measured
    ch = _copy_of(weak_ring.project, tmp_path / "gate").chunk
    ch.optimize_cameras(engine=engine)
    assert ch.meta["sfm"]["optimize"]["engine_choice"] == {"requested": engine,
                                                           "used": "colmap" if engine == "auto" else "ppg"}
    assert _calibration(ch)[0][4] == pytest.approx(-0.04, abs=0.005)  # the lens the photos were rendered with
    errors = _marker_rmse(ch)
    assert errors["optimized_control_rmse"] <= WEAK_RING_GATE["control_m"]  # 1.00 mm (COLMAP's), 1.22 mm (ours)
    assert errors["optimized_check_rmse"] <= WEAK_RING_GATE["check_m"]  # 8.87 mm and 8.88 mm


@needs_core
@pytest.mark.slow
def test_freed_on_the_weak_ring_the_focal_length_runs_away_as_before(weak_ring, tmp_path):
    """``hold_undetermined=False`` on the weak ring is the optimisation of before: the focal length is refined and
    slides along its valley with the height of the cameras (1000 px to several thousand), the targets off by
    centimetres."""
    ch = _copy_of(weak_ring.project, tmp_path / "freed").chunk
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
    """The trench (oblique photos around an excavation with relief) determines its whole calibration: the
    optimisation refines it as before, with either formulation, and the metric gates of the project hold after it -
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
    assert check["checked"] is True and check["held"] == NOTHING_HELD and check["notes"] == []
    assert 0 < check["focal_sd"][0][1] < 0.002 < bundle.FOCAL_SD_HOLD  # well determined: 0.014 % and 0.021 %
    assert 0 < check["radial_sd"][0][1] < 0.2 < bundle.GROUP_SD_HOLD_PX  # 0.05 px at the corner of the photo
    assert all(0 < sd < 0.2 for _sensor, sd in check["tangential_sd"]) and check["principal_point_sd"] == []
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
