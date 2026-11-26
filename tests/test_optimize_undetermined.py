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
import re

import numpy as np
import pytest
from cli_helpers import plain_words
from ring_helpers import assert_consistent, reference_rendering
from test_optimize_gnss import K1, K2, F, _add_control_points, _rtk_block

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


def _drone_block(tilt: float, references: str = "control", seed: int = 3, free=None,
                 noise: float = NOISE) -> bundle.Problem:
    """The block of :func:`bundle_problems.block` (18 photos 30 m above ground with a metre of relief) with
    ``noise`` (:data:`NOISE`) on its tie points, its cameras pitched by ``tilt`` along the strips, and what places it:
    ``"control"`` its five markers as control points at 2 mm, ``"gnss"`` or ``"bias"`` a position of every photo at
    2 cm (two flights, with a bias each for ``"bias"``), ``"none"`` nothing (tie points alone, two photos held as
    :func:`bundle.problem_from_chunk` holds them). The block is adjusted with its calibration held, as an alignment
    leaves it. ``free``: the free intrinsics of its camera (those of the block by default)."""
    rng = np.random.default_rng(seed)
    pb, truth = bundle_problems.block(rng, tilt=tilt)
    pb = bundle._without_held(pb, np.bincount(pb.obs_point, minlength=len(pb.points)) >= 2)
    pb.obs_xy = pb.obs_xy + rng.normal(0, noise, pb.obs_xy.shape)
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
def test_an_earlier_group_is_given_up_for_a_later_one_that_fails():
    """A later group that fails while an earlier one is still free is judged again without the earlier one, and
    where it then passes the earlier one is held in its place (ruling R25): what a lens needs more is not given up
    for what it needs less. Pitched photos with little noise, every intrinsic free: the tangential terms pass
    (0.47 px at the corner) and stay free; the principal point, judged with them free, fails (1.7 px). Without the
    tangential terms it is determined (0.2 px): the tangential terms are held for it, and it is refined. The focal
    length, which the block determines, is not given up: the tangential terms are enough."""
    pb = _drone_block(bundle_problems.TILT, free=range(8), noise=0.2)
    model, params, width, height = pb.cameras[0]
    sds, held, factor = bundle.judge_intrinsics(pb)

    def corner(columns, gone=()):
        cov, free = _oracle(pb, factor, gone)
        index = [free.index(k) for k in columns]
        return bundle.corner_sd(model, params, width, height, list(columns), cov[np.ix_(index, index)])

    before, after = corner((2, 3)), corner((2, 3), gone=(6, 7))
    assert corner((6, 7)) < 0.49 and before > 1.5 and after < 0.3  # by a dense inverse
    assert list(held[0]) == ["tangential"]
    kind, (later, with_it, without_it) = held[0]["tangential"]
    assert (kind, later) == ("for", "principal_point")
    assert with_it == pytest.approx(before, rel=1e-4) and without_it == pytest.approx(after, rel=1e-4)
    assert sds[0]["principal_point"] == pytest.approx(after, rel=1e-4)  # as it is refined
    assert sds[0]["tangential"] == pytest.approx(corner((6, 7)), rel=1e-4)  # as it was judged
    assert 0 < sds[0]["focal"] < bundle.FOCAL_SD_HOLD and 0 < sds[0]["radial"] < 0.2


def _conditional(matrix: np.ndarray, kept: list, gone: list) -> np.ndarray:
    """The covariance of the parameters ``kept`` of ``matrix`` once the parameters ``gone`` are held."""
    if not gone:
        return matrix[np.ix_(kept, kept)]
    cross = matrix[np.ix_(kept, gone)]
    return matrix[np.ix_(kept, kept)] - cross @ np.linalg.solve(matrix[np.ix_(gone, gone)], cross.T)


@needs_core
@pytest.mark.parametrize("tied", [True, False])
def test_the_focal_length_is_the_last_to_be_given_up_for_a_later_group(monkeypatch, tied):
    """The focal length is given up for a later group only when nothing else is enough. A covariance in which the
    radial terms fail at the corner (0.8 px) while the focal length passes (0.4 %): where the two trade (the
    radial terms are 0.2 px once the focal length is held) the focal length is held for them and they are
    refined; where they do not (0.8 px with or without it) the radial terms are held and the focal length stays
    free."""
    pb = _drone_block(bundle_problems.TILT)  # fx, fy, k1, k2 free
    model, params, width, height = pb.cameras[0]
    reach = np.linalg.norm(bundle.corner_jacobians(model, params, width, height, [4])[0][:, 0])  # px per unit of k1
    sd_f, sd_k = 0.004 * params[0], 0.8 / reach
    rho = np.sqrt(1.0 - (0.2 / 0.8) ** 2) if tied else 0.0
    full = np.diag([sd_f ** 2, sd_f ** 2, sd_k ** 2, 1e-16])
    full[0, 2] = full[2, 0] = rho * sd_f * sd_k
    asked = []

    def covariance(problem, **kwargs):
        free = list(problem.free_intrinsics[0])
        asked.append(free)
        kept = [[0, 1, 4, 5].index(k) for k in free]
        gone = [j for j in range(4) if j not in kept]
        return _conditional(full, kept, gone), [len(free)]

    monkeypatch.setattr(bundle, "intrinsics_covariance", covariance)
    monkeypatch.setattr(bundle, "tie_variance_factor", lambda problem: 1.0)
    sds, held, _factor = bundle.judge_intrinsics(pb)
    assert sds[0]["focal"] == pytest.approx(0.004)
    if tied:
        assert held[0] == {"focal": ("for", ("radial", pytest.approx(0.8, rel=1e-6), pytest.approx(0.2, rel=1e-6)))}
        assert sds[0]["radial"] == pytest.approx(0.2, rel=1e-6)
    else:
        assert held[0] == {"radial": ("sd", pytest.approx(0.8, rel=1e-6))}
        assert sds[0]["radial"] == pytest.approx(0.8, rel=1e-6)
    assert asked[0] == [0, 1, 4, 5]


@needs_core
def test_the_focal_length_is_not_given_up_for_what_a_lens_needs_less(tmp_path):
    """An earlier group goes for a later one only where a lens needs it less. The focal length stands first in the
    order because a flat block cannot tell it from its height, not because a lens can do without it: it is given
    up for the radial terms alone, never for the tangential terms or the principal point. A grid pitched a
    little, our formulation: the focal length passes (0.40 %); the tangential terms, judged with it free, fail
    (0.57 px) and would pass without it (0.42 px). They are held, and the focal length is refined."""
    _doc, ch = _block(tmp_path, pitch=0.25)
    rec = sfm.load_reconstruction(ch)
    pb, _ids = bundle.problem_from_chunk(ch, rec, use_markers=True, gnss="off")
    sds, held, _factor = bundle.judge_intrinsics(pb)
    assert held[0] == {"tangential": ("sd", sds[0]["tangential"])}
    assert 0 < sds[0]["focal"] < bundle.FOCAL_SD_HOLD and bundle.GROUP_SD_HOLD_PX < sds[0]["tangential"] < 0.7
    assert 0 < sds[0]["radial"] < 0.3  # judged without the tangential terms
    before = _calibration(ch)
    ch.optimize_cameras(engine="ppg")
    check = ch.meta["sfm"]["optimize"]["intrinsics"]
    assert check["held"] == {**NOTHING_HELD, "tangential": [0]}
    assert check["notes"] == [f"sensor 0: tangential held: undetermined, sd {sds[0]['tangential']:.2f} px at the "
                              "image corner"]
    after = _calibration(ch)
    assert after[0][0] != before[0][0] and after[0][0] == pytest.approx(F, rel=0.01)  # the focal length refined
    assert after[0][6:] == before[0][6:] and after[0][4] != before[0][4]


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
def test_the_focal_length_can_be_judged_alone(monkeypatch):
    """``judge_intrinsics(..., only=("focal",))``: the sd of the focal length as the whole judgement has it - every
    intrinsic that is free with it -, from one covariance; nothing else is judged, and nothing else is held."""
    nadir = _drone_block(0.0, "gnss")  # the focal length is not determined, the radial terms are
    whole, held, factor = bundle.judge_intrinsics(nadir)
    real = bundle.intrinsics_covariance
    calls = []

    def counted(pb, **kwargs):
        calls.append(list(pb.free_intrinsics[0]))
        return real(pb, **kwargs)

    monkeypatch.setattr(bundle, "intrinsics_covariance", counted)
    sds, alone, again = bundle.judge_intrinsics(nadir, only=("focal",))
    assert calls == [list(nadir.free_intrinsics[0])]  # asked once, with everything free
    assert sds == [{**dict.fromkeys(bundle.JUDGED), "focal": whole[0]["focal"]}]
    assert alone == [{"focal": held[0]["focal"]}] and again == factor
    pitched = _drone_block(bundle_problems.TILT)
    sds, alone, _factor = bundle.judge_intrinsics(pitched, only=("focal",))
    assert 0 < sds[0]["focal"] < bundle.FOCAL_SD_HOLD and alone == [{}] and sds[0]["radial"] is None
    # nothing of what is asked is free: the core is not asked
    del calls[:]
    radial = dataclasses.replace(nadir, free_intrinsics=[[4, 5]])
    assert bundle.judge_intrinsics(radial, only=("focal",)) == ([dict.fromkeys(bundle.JUDGED)], [{}], None)
    assert calls == []


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
def test_the_measurements_of_the_markers_follow_the_tie_points_where_they_are_tie_points():
    """COLMAP's adjustment has the measurements of the markers as tie point observations, each counted
    ``marker_weight`` times: in the judgement of that adjustment their accuracy is that of the tie points,
    variance factor included, over the square root of the count. Left at the pixel the problem states, on a block
    with 0.3 px of noise, they would weigh a tenth of what they do and the sd of the focal length would come out
    larger. Our own adjustment has them at the accuracy they state: nothing is scaled there."""
    pb = _drone_block(0.0)  # looking straight down: the control points are what the focal length has
    focal = pb.cameras[0][1][0]

    def dense(**sigmas) -> float:
        probe = dataclasses.replace(pb, **sigmas)
        return float(np.sqrt(_dense_variances(probe)[bundle._Layout(probe).camera_cols[0]][:2].max())) / focal

    sds, _held, factor = bundle.judge_intrinsics(pb, marker_weight=4)
    ties = pb.pixel_sigma * np.sqrt(factor)
    assert factor == pytest.approx(NOISE ** 2, rel=0.05)
    assert sds[0]["focal"] == pytest.approx(dense(pixel_sigma=ties, marker_sigma=ties / 2.0), rel=1e-5)
    without_the_factor = dense(pixel_sigma=ties, marker_sigma=pb.pixel_sigma / 2.0)
    assert without_the_factor > 1.05 * sds[0]["focal"]
    own, _held, _factor = bundle.judge_intrinsics(pb)
    assert own[0]["focal"] == pytest.approx(dense(pixel_sigma=ties), rel=1e-5)
    assert pb.marker_sigma == 0.5 and own[0]["focal"] != pytest.approx(sds[0]["focal"], rel=0.01)


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
    with ours) and, first in the order, is held; the principal point, judged without it, is beyond its own (0.9
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
    assert bundle.FOCAL_SD_HOLD < focal < 0.02 and 0.7 < principal_point < 2.0  # 0.92 px (COLMAP's), 1.41 px (ours)
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
        "tangential_sd": [], "principal_point_sd": [], "radial_sd": [], "held": NOTHING_HELD, "lens_prior": [],
        "notes": []}
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
def test_a_block_without_a_covariance_keeps_its_calibration(tmp_path, monkeypatch, caplog, engine):
    """A normal matrix that cannot be inverted (an intrinsic the block does not hold at all, or numbers the solver
    cannot take) leaves nothing to judge by: each group that would be refined is held. That is not a verdict on
    the block: the notes say the group was not judged, in their own words, and the log has them as warnings."""
    _doc, ch = _block(tmp_path, pitch=PITCH)
    before = _calibration(ch)

    def no_covariance(pb, **kwargs):
        raise RuntimeError("pivot 7 of the reduced system is not positive")

    monkeypatch.setattr(bundle, "intrinsics_covariance", no_covariance)
    with caplog.at_level(logging.INFO, logger="pyarchinit_photogrammetry.engine.sfm"):
        ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    groups = ["focal", "tangential", "radial"] if engine == "ppg" else ["focal", "radial"]
    assert check["checked"] is True and 0.2 < check["variance_factor"] < 0.4
    assert check["held"] == {group: ([0] if group in groups else []) for group in bundle.JUDGED}
    assert all(check[f"{group}_sd"] == [[0, None]] for group in groups) and check["principal_point_sd"] == []
    why = "not judged, the block gave no covariance (pivot 7 of the reduced system is not positive)"
    kept = f"; {sfm.FOCAL_KEPT_NO_COVARIANCE}"  # the focal length stays at the alignment's estimate
    assert check["notes"] == [f"sensor 0: {group} held: {why}{kept if group == 'focal' else ''}" for group in groups]
    assert not any("undetermined" in note for note in check["notes"])
    warned = [r.getMessage() for r in caplog.records
              if r.levelno == logging.WARNING and "not judged" in r.getMessage()]
    assert warned == [f"camera optimisation: {note}" for note in check["notes"]]
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED
    assert _calibration(ch) == before and rep["after"]["control"] < 0.005


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_without_a_covariance_the_focal_length_stays_where_the_alignment_put_it(tmp_path, monkeypatch, engine):
    """No covariance is no verdict that the block does not know its focal length: it is held at the alignment's
    estimate, never walked to the lens prior its photos state (an sd that cannot be computed is not an infinite
    one), and the note of the focal length says so."""
    _doc, ch = _block(tmp_path, pitch=PITCH)
    before = _calibration(ch)
    ch.sensors[0].focal_source, ch.sensors[0].focal_prior = "exif", 1.05 * F

    def no_covariance(pb, **kwargs):
        raise RuntimeError("pivot 7 of the reduced system is not positive")

    monkeypatch.setattr(bundle, "intrinsics_covariance", no_covariance)
    ch.optimize_cameras(engine=engine)
    check = ch.meta["sfm"]["optimize"]["intrinsics"]
    assert check["held"]["focal"] == [0] and check["lens_prior"] == []
    focal_notes = [note for note in check["notes"] if note.startswith("sensor 0: focal held")]
    assert focal_notes == ["sensor 0: focal held: not judged, the block gave no covariance (pivot 7 of the reduced "
                           "system is not positive); kept at the alignment's estimate, not moved towards the lens "
                           "prior"]
    assert _calibration(ch)[0][:2] == before[0][:2]


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
    monkeypatch.setattr(bundle, "judge_intrinsics", lambda pb, **kwargs: verdict)
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
def test_the_record_says_what_a_group_was_given_up_for(tmp_path, monkeypatch):
    """A group that passed and is held in place of a later one that failed with it free: the record holds it,
    the note names the later group with its sd with this one free and without it, and the adjustment refines the
    later group and not this one."""
    _doc, ch = _block(tmp_path, pitch=PITCH)
    sds = {"focal": 0.001, "tangential": 0.48, "principal_point": None, "radial": 0.04}
    verdict = ([sds], [{"tangential": ("for", ("radial", 0.91, 0.04))}], 0.25)
    monkeypatch.setattr(bundle, "judge_intrinsics", lambda pb, **kwargs: verdict)
    problems = []
    real = bundle.adjust

    def adjust(pb, **kwargs):
        problems.append(copy.deepcopy(pb.free_intrinsics))
        return real(pb, **kwargs)

    monkeypatch.setattr(bundle, "adjust", adjust)
    ch.optimize_cameras(engine="ppg")
    check = ch.meta["sfm"]["optimize"]["intrinsics"]
    assert check["held"] == {**NOTHING_HELD, "tangential": [0]}
    assert check["tangential_sd"] == [[0, 0.48]] and check["radial_sd"] == [[0, 0.04]]
    assert check["notes"] == ["sensor 0: tangential held: given up for the radial terms (0.91 px at the image "
                              "corner with it free, 0.04 px without)"]
    assert problems == [[[0, 1, 4, 5]]]
    other = sfm._check_record(True)
    sfm._hold(other, "focal", 3, sfm._given_up_note("principal_point", 1.66, 0.23))
    assert other["notes"] == ["sensor 3: focal held: given up for the principal point (1.66 px at the image "
                              "corner with it free, 0.23 px without)"]


@needs_core
def test_colmaps_judgement_weighs_the_markers_as_its_adjustment_does(tmp_path, monkeypatch):
    """COLMAP's adjustment counts each measurement of a marker ``marker_weight`` times among its tie point
    observations: its judgement is told so (the accuracy of the markers then follows that of the tie points,
    variance factor included); the judgement of our own adjustment is not."""
    seen = []
    real = bundle.judge_intrinsics

    def judge(pb, **kwargs):
        seen.append(kwargs.get("marker_weight"))
        return real(pb, **kwargs)

    monkeypatch.setattr(bundle, "judge_intrinsics", judge)
    for name, options in (("a", dict(engine="colmap")), ("b", dict(engine="colmap", marker_weight=9)),
                          ("c", dict(engine="ppg"))):
        _doc, ch = _block(tmp_path / name)
        ch.optimize_cameras(**options)
    assert seen == [4, 9, None]


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
    monkeypatch.setattr(bundle, "judge_intrinsics", lambda pb, **kwargs: verdict)
    record, groups, _lost = sfm._colmap_calibration_check(ch, rec, hold_undetermined=True, positions=False,
                                                          use_markers=True, fit_principal_point=False,
                                                          marker_weight=4)
    assert groups == {"tangential", "radial"}
    assert record["held"] == {"focal": [], "tangential": [0], "principal_point": [], "radial": [0]}
    assert record["notes"] == ["sensor 0: tangential held: undetermined, sd 1.62 px at the image corner",
                               "sensor 0: radial held: with the tangential terms (COLMAP's adjustment refines "
                               "the distortion whole or not at all)"]
    monkeypatch.setattr(sfm, "_colmap_calibration_check", lambda *args, **kwargs: (record, set(groups), {}))
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

# ------------------------------------------------------------------- the lens prior (ruling R24)
def test_a_sensor_keeps_the_focal_length_its_photos_state(tmp_path):
    """The focal length the photos state (their EXIF) is kept with the sensor apart from its parameters, which an
    alignment overwrites: ``Sensor.focal_prior``, in pixels, saved with the project. A focal length that was
    guessed has none, and a project of an older version loads without it."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.camera import Sensor

    doc = ppg.Document()
    ch = doc.add_chunk("priors")
    stated = ch._sensor_for({"make": "A", "model": "B", "width": 1200, "height": 900, "focal_length_35mm": 30.0})
    assert stated.focal_source == "exif" and stated.focal_prior == stated.params[0] == 1000.0
    guessed = ch._sensor_for({"make": "A", "model": "C", "width": 1200, "height": 900})
    assert guessed.focal_source == "guess" and guessed.focal_prior is None
    stated.params[0] = stated.params[1] = 2430.4  # an alignment overwrites the parameters
    doc.save(tmp_path / "priors.ppg")
    again = ppg.Document.open(tmp_path / "priors.ppg").chunk.sensors[stated.key]
    assert again.focal_prior == 1000.0 and again.params[0] == 2430.4 and again.focal_source == "exif"
    older = {k: v for k, v in stated.to_dict().items() if k not in ("focal_prior", "focal_target")}
    assert Sensor.from_dict(older).focal_prior is None and Sensor.from_dict(older).focal_target is None
    # where the optimisation last held a focal length the block does not determine: kept as well
    assert stated.focal_target is None and again.focal_target is None
    again.focal_target = 1009.7
    assert Sensor.from_dict(again.to_dict()).focal_target == 1009.7


def test_the_lens_prior_of_a_sensor():
    """``sfm.lens_prior``: the focal length the photos of a sensor state, in pixels - the one kept with the sensor
    or, for a project of an older version, the one the EXIF kept with each photo says again; None for a focal
    length that was guessed (also once an alignment has estimated it) or given by hand, whatever is kept."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.camera import Camera

    ch = ppg.Document().add_chunk("priors")
    sensor = ch._sensor_for({"make": "A", "model": "B", "width": 1200, "height": 900, "focal_length_35mm": 30.0})
    assert sfm.lens_prior(ch, sensor) == 1000.0
    sensor.focal_prior = None  # as a project of an older version has it
    assert sfm.lens_prior(ch, sensor) is None  # no photo to say it again
    ch.cameras.append(Camera(key=0, label="p", path="p.jpg", sensor_key=sensor.key,
                             meta={"width": 1200, "height": 900, "focal_length_35mm": 30.0}))
    assert sfm.lens_prior(ch, sensor) == 1000.0 and sensor.focal_prior == 1000.0  # kept from now on
    for source in ("guess", "aligned", None):
        sensor.focal_source = source
        assert sfm.lens_prior(ch, sensor) is None
    sensor.focal_source = "exif_mm"
    assert sfm.lens_prior(ch, sensor) == 1000.0


def _slide_along_the_valley(ch, factor: float) -> None:
    """``ch`` (the nadir block of :func:`_block`) as an alignment that slid along the valley of a flat nadir block
    leaves it: the focal length ``factor`` times longer, the cameras as many times higher above the ground, the
    radial terms by its square and its fourth power - the same photos, the same ground (the relief of the block,
    3 dm, is all that tells the two apart: 0.77 px of mean reprojection error for 0.63). The model on disk, the
    chunk and its georeference follow."""
    rec = sfm.load_reconstruction(ch)
    ground = float(np.mean([point.xyz[2] for point in rec.points3D.values()]))  # the ground of the model: level
    for cam in rec.cameras.values():
        f, _fy, cx, cy, k1, k2, p1, p2 = cam.params
        cam.params = [f * factor, f * factor, cx, cy, k1 * factor ** 2, k2 * factor ** 4, p1 * factor, p2 * factor]
    for image in rec.images.values():
        r = np.asarray(image.cam_from_world().matrix())[:3, :3]
        c = -r.T @ np.asarray(image.cam_from_world().matrix())[:3, 3]
        c = c - (factor - 1.0) * (ground - c[2]) / r[2][2] * r[2]  # back along its axis, factor times as far
        rec.frames[image.frame_id].set_cam_from_world(image.camera_id,
                                                      pycolmap.Rigid3d(pycolmap.Rotation3d(r), -r @ c))
    rec.write(str(ch.product_path("reconstruction")))
    sfm.import_reconstruction(ch, rec)
    gcp.update_transform(ch)


def _heights(ch) -> np.ndarray:
    """The heights of the cameras of ``ch`` above its control points, in the coordinate system."""
    ground = np.mean([m.reference.location[2] for m in ch.markers])
    return np.array([ch.to_world(c.center)[2] for c in ch.aligned_cameras]) - ground


def test_the_target_between_the_estimate_of_the_block_and_the_lens_prior():
    """``sfm.lens_target``: where a focal length the block does not determine is held (ruling R26) - between what
    the alignment estimated, with its marginal sd, and the lens prior, whose sd is 2 % of it (the focal lengths
    of the EXIF are rounded, crop factors vary), each weighed by the inverse of its variance. A block that hardly
    knows its focal length ends at the prior; one that nearly determines it stays near its estimate, whatever the
    photos state."""
    assert sfm.LENS_PRIOR_SD == 0.02
    near = sfm.lens_target(1000.0, 0.006, 1030.0)  # sd 6 px against 20.6 px
    assert near == pytest.approx((1000.0 / 6.0 ** 2 + 1030.0 / 20.6 ** 2) / (1.0 / 6.0 ** 2 + 1.0 / 20.6 ** 2))
    assert 1002.0 < near < 1003.0
    # a ring flown once, as COLMAP's mapper left it twice: an sd of 161 px, and of 254 px
    assert sfm.lens_target(747.28, 0.2153, 1000.0) == pytest.approx(996.15, abs=0.05)
    assert sfm.lens_target(3909.22, 0.0649, 1000.0) == pytest.approx(1018.0, abs=0.1)
    for sd in (None, float("inf"), float("nan")):  # no covariance: nothing to weigh, the estimate stays
        assert sfm.lens_target(3909.2, sd, 1000.0) == 3909.2
    assert sfm.lens_target(1000.0, 0.0, 1030.0) == 1000.0  # an estimate without error: itself
    assert sfm.lens_target(1000.0, 0.02, 1000.0) == 1000.0


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_an_undetermined_focal_length_is_held_between_its_estimate_and_the_lens_prior(tmp_path, engine):
    """A block that does not determine its focal length is not held where a free alignment left it (rulings R24,
    R26). The nadir grid slid along its valley - the focal length 1.3 times too long, the cameras 12 m too high:
    its estimate is worth next to nothing (an sd of 92 % of the focal length and more) and the target is the
    focal length the photos state, to a tenth of a pixel. The optimisation walks the focal length there in steps
    of a tenth at most, the block adjusted again at each with its calibration held, then holds it and goes on as
    on a block that was never off; the record has the estimate, its sd, the prior and the target. A second
    optimisation finds the focal length at its target and leaves it. Without a prior (a focal length that was
    guessed, or given by hand) it stays where the alignment left it, as before."""
    _doc, ch = _block(tmp_path / "prior")
    _slide_along_the_valley(ch, 1.3)
    assert _calibration(ch)[0][0] == pytest.approx(1.3 * F) and _heights(ch).mean() == pytest.approx(52.0, abs=0.5)
    slid = sfm.load_reconstruction(ch)
    slid.update_point_3d_errors()
    assert slid.compute_mean_reprojection_error() < 1.0  # the same photos and the same ground: still a fit
    assert np.mean([image.cam_from_world().inverse().translation[2] for image in slid.images.values()]) > 50  # m
    ch.sensors[0].focal_source, ch.sensors[0].focal_prior = "exif", F
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    after = _calibration(ch)[0]
    walked, = check["lens_prior"]
    assert set(walked) == {"sensor", "estimate", "sd", "prior", "prior_sd", "target", "steps"}
    assert (walked["sensor"], walked["prior"], walked["prior_sd"], walked["steps"]) == (0, F, 0.02, 3)
    # the sd of the grid as it was slid: 92 % with COLMAP's formulation, a few hundred times the focal with ours
    assert walked["estimate"] == pytest.approx(1.3 * F) and walked["sd"] > 0.5
    target = sfm.lens_target(walked["estimate"], walked["sd"], F)
    assert walked["target"] == pytest.approx(target, rel=1e-12) and F <= target < F + 0.2
    assert after[0] == pytest.approx(target, rel=1e-12) and after[1] == after[0]
    assert ch.sensors[0].focal_target == pytest.approx(target, rel=1e-12)
    assert check["notes"][0].startswith(
        f"sensor 0: focal walked to {target:.2f} px, between its estimate 1300.00 px (sd {100 * walked['sd']:.2f} %) "
        "and the lens prior 1000.00 px (sd 2.00 %), in 3 steps (reprojection ")
    assert check["held"] == {**NOTHING_HELD, "focal": [0], "radial": [0]}  # judged again where the block now is
    assert check["notes"][1].startswith("sensor 0: focal held: undetermined, sd ")
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED
    scale = target / F  # the radial terms carried back along the valley
    assert after[4] == pytest.approx(K1 * scale ** 2, rel=1e-6)
    assert after[5] == pytest.approx(K2 * scale ** 4, rel=1e-6)
    assert _heights(ch).mean() == pytest.approx(40.0, abs=0.2)
    assert rep["control_points"] == 4 and rep["after"]["control"] < 0.005 and rep["after"]["check"] < 0.013
    ch.optimize_cameras(engine=engine)  # again: the focal length is where it was put
    again = ch.meta["sfm"]["optimize"]["intrinsics"]
    assert again["lens_prior"] == [] and again["held"]["focal"] == [0]
    assert _calibration(ch)[0][0] == pytest.approx(target, rel=1e-12)

    _doc, ch = _block(tmp_path / "guessed")
    _slide_along_the_valley(ch, 1.3)
    for source in ("aligned", None):
        ch.sensors[0].focal_source, ch.sensors[0].focal_prior = source, F
        ch.optimize_cameras(engine=engine)
        check = ch.meta["sfm"]["optimize"]["intrinsics"]
        assert check["lens_prior"] == [] and check["held"]["focal"] == [0]
        assert _calibration(ch)[0][0] == pytest.approx(1.3 * F, rel=1e-9)  # where the alignment left it


def test_the_notes_of_the_walk(caplog):
    """``sfm._note_lens_prior``: what the walk did, at the head of the notes of the check - where the focal length
    was put and what was weighed, the lens prior itself for an estimate without an sd - and, as a warning, a block
    that did not follow, of which the record keeps no row."""
    record = sfm._check_record(True)
    record["notes"].append("sensor 0: focal held: undetermined, sd 9.70 %")
    rows = [{"sensor": 0, "estimate": 747.28, "sd": 0.2153, "prior": 1000.0, "target": 996.15, "steps": 4,
             "reprojection": [0.179, 0.183], "followed": True},
            {"sensor": 1, "estimate": 1300.0, "sd": None, "prior": 1000.0, "target": 1000.0, "steps": 3,
             "reprojection": [0.771, 0.64], "followed": True},
            {"sensor": 2, "estimate": 3909.22, "sd": 0.0649, "prior": 1000.0, "target": 1017.99, "steps": 15,
             "reprojection": [0.179, 0.512], "followed": False},
            {"sensor": 3, "estimate": 1300.0, "sd": None, "prior": 1000.0, "target": 1000.0, "steps": 3,
             "reprojection": [0.771, 0.5], "followed": True, "singular": "pivot 7 of the reduced system is not positive"}]
    with caplog.at_level(logging.INFO, logger="pyarchinit_photogrammetry.engine.sfm"):
        sfm._note_lens_prior(record, rows)
    assert record["notes"] == [
        "sensor 0: focal walked to 996.15 px, between its estimate 747.28 px (sd 21.53 %) and the lens prior "
        "1000.00 px (sd 2.00 %), in 4 steps (reprojection 0.179 -> 0.183 px)",
        "sensor 1: focal walked to 1000.00 px, the lens prior, its estimate 1300.00 px being without an sd, in 3 "
        "steps (reprojection 0.771 -> 0.640 px)",
        "sensor 2: focal left at 3909.22 px: the block does not follow to 1017.99 px, between its estimate "
        "3909.22 px (sd 6.49 %) and the lens prior 1000.00 px (sd 2.00 %) (reprojection 0.179 -> 0.512 px)",
        "sensor 3: focal walked to 1000.00 px, the lens prior, the block holding nothing of its estimate 1300.00 px "
        "(its normal matrix singular with only its focal length free: pivot 7 of the reduced system is not "
        "positive), in 3 steps "
        "(reprojection 0.771 -> 0.500 px)",
        "sensor 0: focal held: undetermined, sd 9.70 %"]
    assert record["lens_prior"] == [
        {"sensor": 0, "estimate": 747.28, "sd": 0.2153, "prior": 1000.0, "prior_sd": 0.02, "target": 996.15,
         "steps": 4},
        {"sensor": 1, "estimate": 1300.0, "sd": None, "prior": 1000.0, "prior_sd": 0.02, "target": 1000.0,
         "steps": 3},
        {"sensor": 3, "estimate": 1300.0, "sd": None, "prior": 1000.0, "prior_sd": 0.02, "target": 1000.0,
         "steps": 3}]
    said = [(r.levelno, r.getMessage()) for r in caplog.records if "focal" in r.getMessage()]
    assert [level for level, _text in said] == [logging.INFO, logging.INFO, logging.WARNING, logging.INFO]
    assert said[2][1] == "camera optimisation: " + record["notes"][2]


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_a_block_that_does_not_follow_is_left_as_the_alignment_left_it(tmp_path, monkeypatch, engine):
    """The net of the walk: a block whose mean reprojection error ends above ``LENS_PRIOR_WORSE`` times the one it
    started with (and ``LENS_PRIOR_SLACK_PX`` more) has not followed its focal length - nothing is written, the
    focal length is held where the alignment left it, the sensor keeps no target, and the notes say so first.
    The limits are set to nothing here, so that the slid grid, which follows, counts as a block that does not."""
    _doc, ch = _block(tmp_path)
    _slide_along_the_valley(ch, 1.3)
    ch.sensors[0].focal_source, ch.sensors[0].focal_prior = "exif", F
    monkeypatch.setattr(sfm, "LENS_PRIOR_WORSE", 0.0)
    monkeypatch.setattr(sfm, "LENS_PRIOR_SLACK_PX", 0.0)
    ch.optimize_cameras(engine=engine)
    check = ch.meta["sfm"]["optimize"]["intrinsics"]
    assert check["lens_prior"] == [] and ch.sensors[0].focal_target is None
    assert check["notes"][0].startswith("sensor 0: focal left at 1300.00 px: the block does not follow to 1000.")
    assert check["notes"][1].startswith("sensor 0: focal held: undetermined, sd ") and check["held"]["focal"] == [0]
    assert _calibration(ch)[0][0] == pytest.approx(1.3 * F, rel=1e-9)
    assert _heights(ch).mean() == pytest.approx(52.0, abs=0.5)


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_a_focal_length_the_block_nearly_determines_stays_near_its_estimate(tmp_path, engine):
    """The other end of ruling R26: a grid pitched a little knows its focal length to 0.6 or 0.7 % - beyond the
    limit, so it is not refined - and its photos state one 3 % too long (a rounded EXIF). The estimate of the
    block weighs ten times the prior: the focal length is held 0.3 % from where the alignment left it, not 3 %
    off at the value of the photos, the record says what was weighed, and the block ends as the same block does
    when its photos state the focal length of the alignment - its cameras a decimetre higher, where the value of
    the photos would have put them more than a metre off."""
    _doc, same = _block(tmp_path / "same", pitch=0.12)
    same.sensors[0].focal_source, same.sensors[0].focal_prior = "exif", F
    same.optimize_cameras(engine=engine)
    plain = same.meta["sfm"]["optimize"]
    assert plain["intrinsics"]["lens_prior"] == [] and _calibration(same)[0][0] == F  # at its target already
    _doc, ch = _block(tmp_path / "long", pitch=0.12)
    assert _calibration(ch)[0][0] == F  # the alignment has the true focal length
    ch.sensors[0].focal_source, ch.sensors[0].focal_prior = "exif", 1.03 * F
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    walked, = check["lens_prior"]
    assert walked["estimate"] == F and walked["prior"] == 1.03 * F and walked["steps"] == 1
    assert bundle.FOCAL_SD_HOLD < walked["sd"] < 0.008  # 0.63 % (COLMAP's formulation), 0.68 % (ours)
    target = sfm.lens_target(F, walked["sd"], 1.03 * F)
    assert walked["target"] == pytest.approx(target, rel=1e-12)
    assert F + 1.0 < target < F + 4.0  # 2.5 and 2.9 px: a tenth of the way to the prior
    assert _calibration(ch)[0][0] == pytest.approx(target, rel=1e-12)
    assert check["held"]["focal"] == [0] and rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED
    assert check["notes"][0].startswith(
        f"sensor 0: focal walked to {target:.2f} px, between its estimate 1000.00 px (sd {100 * walked['sd']:.2f} %) "
        "and the lens prior 1030.00 px (sd 2.00 %), in 1 steps (reprojection ")
    assert abs(_heights(ch).mean() - _heights(same).mean()) < 0.15  # 0.10 and 0.12 m; 1.17 m at the prior
    for kind in ("control", "check"):  # 2.8 against 2.9 mm, 24.0 against 25.0 mm
        assert rep["after"][kind] == pytest.approx(plain["after"][kind], rel=0.1)


@needs_core
@pytest.mark.parametrize("engine", ENGINES)
def test_a_focal_length_the_block_determines_is_not_brought_to_the_prior(tmp_path, engine):
    """The prior is for what the block does not determine: pitched photos determine their focal length, which is
    refined as before whatever the photos state (here a value a tenth short)."""
    _doc, ch = _block(tmp_path, pitch=PITCH)
    ch.sensors[0].focal_source, ch.sensors[0].focal_prior = "exif", 0.9 * F
    ch.optimize_cameras(engine=engine)
    check = ch.meta["sfm"]["optimize"]["intrinsics"]
    assert check["lens_prior"] == [] and check["held"] == NOTHING_HELD
    assert _calibration(ch)[0][0] == pytest.approx(F, rel=0.01)


@needs_core
def test_a_block_nothing_places_weighs_its_focal_length_against_the_lens_as_any_other(tmp_path):
    """Our adjustment of a block that nothing places holds its focal length, which it cannot tell from the depth
    as it is asked there: no verdict on the block. What the block knows of its focal length is asked apart - the
    sd it has with the focal length free - and weighed against the lens prior as anywhere else (ruling R26): the
    nadir grid slid along its valley ends at the focal length its photos state; pitched photos, which know their
    focal length to 0.7 % by their tie points alone, stay a third of a per cent from the one of the alignment
    when their photos state one 3 % longer.

    The nadir grid's sd with the focal length free is that of a matrix singular to the last bits (570 times the
    focal length on macOS): where the factorisation of a platform finds a pivot that is not positive instead
    (GitHub's Windows runner), the focal length is unbounded all the same and goes to the prior itself, its sd
    None (:func:`test_a_focal_length_singular_with_it_free_goes_to_the_lens_prior_on_every_platform`)."""
    _doc, ch = _block(tmp_path / "nadir")
    _slide_along_the_valley(ch, 1.3)
    for marker in ch.markers:
        marker.reference.enabled = False
    for cam in ch.cameras:
        cam.reference.enabled = False
    ch.sensors[0].focal_source, ch.sensors[0].focal_prior = "exif", F
    ch.optimize_cameras(engine="ppg")
    rep = ch.meta["sfm"]["optimize"]
    print(f"nadir grid: lens prior {rep['intrinsics']['lens_prior']}, notes {rep['intrinsics']['notes']}")
    assert rep["focal_held"] == sfm.FOCAL_HELD_NO_REFERENCES
    walked, = rep["intrinsics"]["lens_prior"]  # on every platform
    assert (walked["sensor"], walked["prior"], walked["prior_sd"], walked["steps"]) == (0, F, 0.02, 3)
    assert walked["estimate"] == pytest.approx(1.3 * F)
    assert not any("gave no covariance" in note for note in rep["intrinsics"]["notes"])
    if walked["sd"] is None:  # the focal length alone free meets a pivot that is not positive: the prior itself
        assert walked["target"] == F
    else:
        # macOS: the marginal sd, 570 times the focal length (target within 0.1 px of the prior); GitHub's Windows
        # runner: the matrix fails with the rest free and the focal length alone has a conditional sd of 6.65 %
        # (the radial terms held), which weighs it to 1015.2 px - both undetermined, both walked
        assert walked["sd"] > bundle.FOCAL_SD_HOLD
        assert walked["target"] == pytest.approx(sfm.lens_target(walked["estimate"], walked["sd"], F), rel=1e-12)
    assert F <= walked["target"] < (F + 0.1 if walked["sd"] is None or walked["sd"] > 1.0 else 1.02 * F)
    assert _calibration(ch)[0][:2] == [pytest.approx(walked["target"], rel=1e-12)] * 2

    _doc, ch = _block(tmp_path / "pitched", pitch=PITCH)
    for marker in ch.markers:
        marker.reference.enabled = False
    for cam in ch.cameras:
        cam.reference.enabled = False
    ch.sensors[0].focal_source, ch.sensors[0].focal_prior = "exif", 1.03 * F
    assert _calibration(ch)[0][0] == F
    ch.optimize_cameras(engine="ppg")
    rep = ch.meta["sfm"]["optimize"]
    assert rep["focal_held"] == sfm.FOCAL_HELD_NO_REFERENCES
    walked, = rep["intrinsics"]["lens_prior"]
    assert walked["estimate"] == F and walked["prior"] == 1.03 * F and walked["steps"] == 1
    assert 0.005 < walked["sd"] < 0.009  # 0.71 %
    target = sfm.lens_target(F, walked["sd"], 1.03 * F)
    assert walked["target"] == pytest.approx(target, rel=1e-12) and F + 2.0 < target < F + 5.0  # 3.2 px of 30
    assert _calibration(ch)[0][:2] == [pytest.approx(target, rel=1e-12)] * 2


SINGULAR = "pivot 7 of the reduced system is not positive"
TANGENTIAL = {6, 7}  # p1, p2 of the OPENCV model of the block


def _platform(monkeypatch, fails) -> list:
    """``bundle.intrinsics_covariance`` as a platform whose factorisation of the nadir grid's normal matrix fails
    where ``fails`` says - ``fails(free)``, ``free`` the set of the free intrinsics of the one camera, returns the
    error (a RuntimeError of the core) or None, where the real covariance is taken. Returns the free intrinsics of
    each call."""
    real, calls = bundle.intrinsics_covariance, []

    def covariance(pb, *args, **kwargs):
        calls.append([list(f) for f in pb.free_intrinsics])
        why = fails(set(pb.free_intrinsics[0]))
        if why is not None:
            raise RuntimeError(why)
        return real(pb, *args, **kwargs)

    monkeypatch.setattr(bundle, "intrinsics_covariance", covariance)
    return calls


def _singular_with_the_focal_free(monkeypatch, why: str = SINGULAR) -> list:
    """The platform of GitHub's Windows runner on the nadir grid: a pivot that is not positive (``why``) whenever
    the focal length is free, the matrix with it held as every platform has it."""
    return _platform(monkeypatch, lambda free: why if free & {0, 1} else None)


def _nadir_grid_nothing_places(folder):
    _doc, ch = _block(folder)
    _slide_along_the_valley(ch, 1.3)
    for item in (*ch.markers, *ch.cameras):
        item.reference.enabled = False
    ch.sensors[0].focal_source, ch.sensors[0].focal_prior = "exif", F
    return ch


@needs_core
def test_a_focal_length_singular_with_it_free_goes_to_the_lens_prior_on_every_platform(tmp_path, monkeypatch):
    """The nadir grid that nothing places is singular to the last bits with its focal length free: macOS factors
    its normal matrix (an sd of 570 times the focal length), GitHub's Windows runner finds a pivot that is not
    positive (CI runs 45290319162 and 45391469522: the focal length kept at 1.3 times the true one). Both say the
    same: the matrix with the focal length held inverts (every group of the check has its sd), and the focal
    length alone free - every other intrinsic held, the poses and the tie points with it - still meets a pivot that
    is not positive: a variance that has no bound, nothing of the estimate known, and the focal length goes to the
    lens prior, as the sd of 570 times sends it there."""
    ch = _nadir_grid_nothing_places(tmp_path)
    calls = _singular_with_the_focal_free(monkeypatch)
    ch.optimize_cameras(engine="ppg")
    rep = ch.meta["sfm"]["optimize"]
    assert rep["focal_held"] == sfm.FOCAL_HELD_NO_REFERENCES
    assert [[0, 1]] in calls  # confirmed with the focal length alone free
    walked, = rep["intrinsics"]["lens_prior"]
    assert (walked["sensor"], walked["estimate"], walked["sd"], walked["prior"]) == (0, pytest.approx(1.3 * F), None, F)
    assert walked["target"] == F and walked["steps"] == 3
    assert _calibration(ch)[0][:2] == [F, F]
    notes = rep["intrinsics"]["notes"]
    assert notes[0].startswith(f"sensor 0: focal walked to {F:.2f} px, the lens prior, the block holding nothing of "
                               f"its estimate {1.3 * F:.2f} px (its normal matrix singular with only its focal length "
                               f"free: {SINGULAR}), in 3 steps (reprojection ")
    assert not any("gave no covariance" in note for note in notes)


@needs_core
def test_a_pivot_the_focal_length_alone_does_not_meet_weighs_it_by_its_sd_alone(tmp_path, monkeypatch):
    """A matrix that fails with the focal length free together with the rest it is asked with (here the tangential
    terms) and inverts with the focal length alone free: the failure is not proved the focal length's, and its sd
    with only it free (a conditional sd, never above the marginal one) decides - above the line of the check
    (``bundle.FOCAL_SD_HOLD``) it is weighed against the lens prior as on any platform that factors the matrix."""
    ch = _nadir_grid_nothing_places(tmp_path)
    calls = _platform(monkeypatch, lambda free: SINGULAR if free & {0, 1} and free & TANGENTIAL else None)
    ch.optimize_cameras(engine="ppg")
    intr = ch.meta["sfm"]["optimize"]["intrinsics"]
    assert [[0, 1]] in calls
    walked, = intr["lens_prior"]
    assert walked["sd"] > bundle.FOCAL_SD_HOLD
    assert walked["target"] == pytest.approx(sfm.lens_target(walked["estimate"], walked["sd"], F), rel=1e-12)
    assert intr["notes"][0].startswith(f"sensor 0: focal walked to {walked['target']:.2f} px, between its estimate "
                                       f"{1.3 * F:.2f} px (sd ")
    assert not any("gave no covariance" in note for note in intr["notes"])


@needs_core
def test_a_focal_length_is_not_called_unbounded_for_a_group_the_check_could_not_judge(tmp_path, monkeypatch):
    """The review's I1: a block whose tangential terms the observations do not hold at all - the first matrix of
    the check fails, the tangential terms are held without a verdict, the rest is judged from the matrix without
    them - and whose focal-free question fails for the same tangential terms. The matrix with every focal length
    held and every other group free did not invert: nothing proves the focal length unbounded, and it is kept at the
    alignment's estimate with the warning, as before."""
    ch = _nadir_grid_nothing_places(tmp_path)
    _platform(monkeypatch, lambda free: SINGULAR if free & TANGENTIAL else None)
    ch.optimize_cameras(engine="ppg")
    intr = ch.meta["sfm"]["optimize"]["intrinsics"]
    assert intr["held"]["tangential"] == [0]
    assert any(note.startswith("sensor 0: tangential held: not judged, the block gave no covariance")
               for note in intr["notes"])
    assert intr["lens_prior"] == []
    assert f"sensor 0: focal: the block gave no covariance; {sfm.FOCAL_KEPT_NO_COVARIANCE}" in intr["notes"]
    assert _calibration(ch)[0][:2] == [pytest.approx(1.3 * F)] * 2


@needs_core
@pytest.mark.parametrize("why", ["the conjugate gradients did not converge in 5000 iterations",
                                 "a matrix that is not positive everywhere"])
def test_a_focal_length_without_a_covariance_for_another_reason_stays_where_the_alignment_put_it(
        tmp_path, monkeypatch, why):
    """Only a matrix that inverts with the focal length held and not with it alone free says that the focal length
    is unbounded. Another failure (conjugate gradients that stop, or a matrix that does not invert even with the
    focal length held) is no verdict: the focal length is kept at the alignment's estimate and the log warns (I3)."""
    ch = _nadir_grid_nothing_places(tmp_path)
    if why.startswith("a matrix"):
        _platform(monkeypatch, lambda free: "pivot 3 of it is not positive")
    else:
        _singular_with_the_focal_free(monkeypatch, why)
    ch.optimize_cameras(engine="ppg")
    intr = ch.meta["sfm"]["optimize"]["intrinsics"]
    assert intr["lens_prior"] == []
    assert f"sensor 0: focal: the block gave no covariance; {sfm.FOCAL_KEPT_NO_COVARIANCE}" in intr["notes"]
    assert _calibration(ch)[0][:2] == [pytest.approx(1.3 * F)] * 2


def _cameras(n: int) -> bundle.Problem:
    none = np.zeros((0, 3))
    return bundle.Problem(rotations=np.zeros((0, 3, 3)), centres=none, image_camera=np.zeros(0, int),
                          cameras=[("OPENCV", [F, F, 0, 0, 0, 0, 0, 0], 1, 1)] * n, points=none,
                          obs_image=np.zeros(0, int), obs_point=np.zeros(0, int), obs_xy=np.zeros((0, 2)),
                          free_intrinsics=[[4, 5]] * n)


def test_each_focal_length_without_a_covariance_is_asked_again_alone(monkeypatch):
    """``sfm._confirmed_focals``: each camera whose focal length had no covariance is asked again with only that
    focal length free (every other intrinsic of every camera held): a pivot that is not positive there is an
    unbounded focal length (the poses alone are a part of every matrix the check inverted); an sd above the line of
    the check is one to weigh against the prior; a small sd or another failure is no verdict (kept, with the
    warning)."""
    pb = _cameras(4)
    asked = [[0, 1, 4, 5]] * 4
    failed = ("no covariance", "pivot 9 of the reduced system is not positive")
    alone = {0: ("no covariance", SINGULAR), 1: ("sd", 0.3), 2: ("sd", 0.001),
             3: ("no covariance", "the conjugate gradients did not converge in 5000 iterations")}
    asks = []

    def judge(problem, only=None, **_kwargs):
        assert only == ("focal",)
        free = [list(f) for f in problem.free_intrinsics]
        asks.append(free)
        c, = (o for o, f in enumerate(free) if f)
        kind, value = alone[c]
        sds = [dict.fromkeys(bundle.JUDGED) for _c in free]
        if kind == "sd":
            sds[c]["focal"] = value
        return sds, [{"focal": alone[c]} if o == c else {} for o in range(len(free))], 1.0

    monkeypatch.setattr(bundle, "judge_intrinsics", judge)
    singular, undetermined = sfm._confirmed_focals(pb, asked, [0, 1, 2, 3], [{"focal": failed}] * 4)
    assert singular == {0: SINGULAR} and undetermined == {1: 0.3}
    assert asks == [[[0, 1] if o == c else [] for o in range(4)] for c in range(4)]
    # a camera whose focal length had its covariance is not asked again
    asks.clear()
    assert sfm._confirmed_focals(pb, asked, [0, 1], [{"focal": ("sd", 0.2)}, {}]) == ({}, {})
    assert asks == []


def test_two_focal_lengths_singular_only_together_stay_where_the_alignment_put_them(monkeypatch):
    """The review's M1: two sensors on the same flat grid whose ratio of focal lengths the block determines and
    whose common scale it does not - all focal lengths free, the matrix fails; each alone free, the other held, it
    inverts with a small sd. Neither is proved unbounded: both are kept at the alignment's estimate, with the
    warning, as before."""
    pb = _cameras(2)
    failed = ("no covariance", SINGULAR)

    def judge(problem, only=None, **_kwargs):
        c, = (o for o, f in enumerate(problem.free_intrinsics) if f)
        sds = [dict.fromkeys(bundle.JUDGED) for _c in problem.cameras]
        sds[c]["focal"] = 0.002
        return sds, [{} for _c in problem.cameras], 1.0

    monkeypatch.setattr(bundle, "judge_intrinsics", judge)
    assert sfm._confirmed_focals(pb, [[0, 1, 4, 5]] * 2, [0, 1], [{"focal": failed}] * 2) == ({}, {})


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
    helptext = plain_words(run(app, ["optimize", "--help"]).output)
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


def test_a_batch_takes_the_option(tmp_path, monkeypatch):
    """The step *optimize_cameras* of a batch has ``hold_undetermined`` among its parameters, True unless the step
    says otherwise; it reaches the chunk, and the form of the step in the batch window offers it."""
    from pyarchinit_photogrammetry import batch
    from pyarchinit_photogrammetry.core.chunk import Chunk

    assert batch.OPERATIONS["optimize_cameras"]["hold_undetermined"] is True
    assert batch.step("optimize_cameras")["params"]["hold_undetermined"] is True
    freed = batch.step("optimize_cameras", hold_undetermined=False)
    assert freed["params"] == {"fit_principal_point": False, "use_markers": True, "hold_undetermined": False}
    doc, _ch = _block(tmp_path)
    doc.save()
    seen = []
    monkeypatch.setattr(Chunk, "optimize_cameras", lambda self, **kwargs: seen.append(kwargs["hold_undetermined"]))
    report = batch.run_batch(doc, [freed, batch.step("optimize_cameras")], resume=False)
    assert seen == [False, True] and not report["failed"]
    pytest.importorskip("PySide6")
    from pyarchinit_photogrammetry.gui import batch_dialog

    form = {f.name: f for f in batch_dialog.fields("optimize_cameras", freed["params"])}
    assert form["hold_undetermined"].default is False and "calibrazione" in form["hold_undetermined"].label
    assert {f.name: f.default for f in batch_dialog.fields("optimize_cameras", {})}["hold_undetermined"] is True


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
    asked, calls, walked = [], [], []
    answer = {"markers": True, "weight": 4, "pp": False, "engine": "colmap", "gnss": "auto", "rolling": False}

    def ask(cls, parent, title, fields, intro=""):
        asked.append(({f.name: f for f in fields}, intro))
        return dict(answer)

    def optimise(self, **kwargs):
        calls.append(kwargs["hold_undetermined"])
        record = sfm._check_record(kwargs["hold_undetermined"])
        if kwargs["hold_undetermined"]:
            record.update(checked=True, focal_sd=[[0, 0.11]], lens_prior=list(walked),
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
        assert not any("valore dichiarato dalle foto" in line for line in said)
        # a focal length walked towards the one the photos state: said as such, with the three values
        walked.append({"sensor": 0, "estimate": 2430.4, "sd": 0.1, "prior": 1000.0, "prior_sd": 0.02,
                       "target": 1009.7, "steps": 10})
        w.optimize_cameras()
        w.runner.wait()
        app.processEvents()
        last = [e[2] for e in w.log_pane.entries if "Errore di riproiezione" in e[2]][-1]
        assert ("focale portata a 1010 px, tra la stima dell'allineamento (2430 px) e il valore dichiarato dalle "
                "foto (1000 px), e tenuta: il blocco non la determina (1 sensori su 1)") in last
        assert "focale dell'allineamento tenuta" not in last and others in last
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
    return types.SimpleNamespace(project=doc.path, aligned=aligned, shape=truth.ring_shape(doc, scene),
                                 reference=reference_rendering(scene))


def _assert_gate(calibration, errors) -> None:
    """The lines of the weak ring's gate and its lens, on the reference rendering (:func:`reference_rendering`)."""
    from pyarchinit_photogrammetry.testing.synthetic import WEAK_RING_GATE

    assert errors["optimized_control_rmse"] <= WEAK_RING_GATE["control_m"]  # 1.00 mm (COLMAP's), 1.22 mm (ours)
    assert errors["optimized_check_rmse"] <= WEAK_RING_GATE["check_m"]  # 8.87 mm and 8.88 mm
    # k1 is a diagnostic, not a line of the gate (ruling R-B2): the lens the photos were rendered with (-0.04) within
    # 0.010 - seeds 0-4 already spread -0.047 to -0.050, and with the radial terms of the mapper freed (R-B1) the
    # optimisation ends at -0.0345 on the reference rendering: the k1/dome valley of a flat block (Task C)
    assert calibration[4] == pytest.approx(-0.04, abs=0.010)


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
    the focal length held the ring determines its radial terms (0.12 px at the corner of the photo): the mapper
    judged them so with its focal length held and freed them (ruling R-B1, 0.26 px at the corner measured), and
    the optimisation refines them again. It does not determine the tangential terms of our formulation (1.6 px):
    they are held. The cameras stay at their height: before this rule the same optimisation moved the focal length
    ten times and the cameras 131 m up. Those figures and decisions on the reference rendering
    (:func:`reference_rendering`, ruling R29); on any other, decisions consistent with what the block reported."""
    ch = _copy_of(weak_ring.project, tmp_path / engine).chunk
    before, heights = _calibration(ch), np.array([ch.to_world(c.center)[2] for c in ch.aligned_cameras])
    mapper = ch.meta["sfm"]["native_mapper"]["report"]
    radial_free = [n for n in mapper["notes"] if ": radial free: " in n]
    assert all(": focal held: " in n or ": radial " in n for n in mapper["notes"]), mapper["notes"]
    if radial_free:  # R-B1: the focal length held, the radial terms freed where the block determines them
        assert before[0][4] != 0.0
    else:
        assert before[0][4] == 0.0
    assert before[0][6:] == [0.0, 0.0]  # no decentring terms from the mapper with the focal length held
    ch.optimize_cameras(engine=engine)
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    rose = np.array([ch.to_world(c.center)[2] for c in ch.aligned_cameras]) - heights
    print(f"weak ring ({engine}): reference {weak_ring.reference}, held {check['held']}, focal sd {check['focal_sd']}, "
          f"radial sd {check['radial_sd']}, tangential sd {check['tangential_sd']}, notes {check['notes']}, "
          f"cameras moved up to {np.abs(rose).max():.3f} m")
    assert check["checked"] is True and rep["control_points"] == 6
    assert_consistent(check, before, _calibration(ch))
    if not weak_ring.reference:
        return
    assert radial_free, mapper["notes"]  # the reference rendering: the mapper frees the radial terms (0.26 px)
    assert mapper["intrinsics_held"] == [cam for cam, _ in mapper["focal_sd"]]  # the focal length held (R16)
    sensor = check["focal_sd"][0][0]
    assert check["checked"] is True and check["held"]["focal"] == [sensor]
    assert check["held"]["principal_point"] == [] and check["principal_point_sd"] == []
    assert check["focal_sd"][0][1] > 0.05  # 10 % (COLMAP's formulation) and 14 % (ours) measured
    # the rule's own decision, not a figure: freed iff the sd at the corner is within the line (0.50 px measured
    # from the radial terms the mapper left, 0.12 px from a lens at zero before R-B1)
    assert 0 < check["radial_sd"][0][1] and (check["radial_sd"][0][1] <= bundle.GROUP_SD_HOLD_PX) == (
        check["held"]["radial"] == [])
    assert check["notes"][0] == f"sensor {sensor}: focal held: undetermined, sd {100 * check['focal_sd'][0][1]:.2f} %"
    if engine == "ppg":
        # held, undetermined on its own or given up for the radial terms (R25: 2.35 px with them free measured)
        assert check["held"]["tangential"] == [sensor]
        assert len(check["notes"]) == 2 and check["notes"][1].startswith(f"sensor {sensor}: tangential held: ")
    else:
        assert check["held"]["tangential"] == [] and check["tangential_sd"] == [] and len(check["notes"]) == 1
    assert rep["focal_held"] == sfm.FOCAL_HELD_UNDETERMINED and rep["control_points"] == 6
    after = _calibration(ch)
    assert after[0][:4] == before[0][:4] and after[0][4] != 0.0  # the focal length exactly, the radial terms refined
    assert after[0][6:] == [0.0, 0.0]
    assert np.abs(rose).max() < 0.5  # in the coordinate system: where the alignment put them, the lens fitted


@needs_core
@pytest.mark.slow
@pytest.mark.parametrize("engine", ["auto", "ppg"])
def test_the_weak_ring_meets_the_four_lines_of_its_gate(weak_ring, tmp_path, engine):
    """The gate of the weak ring, whole: every photo aligned by the native mapper with no fallback; the shape of
    the ring within ``WEAK_RING_GATE["shape"]`` of its diameter before any optimisation; and after the
    optimisation - with the formulation the program runs by default, COLMAP's, and with our own - the control
    targets within ``WEAK_RING_GATE["control_m"]`` and the check targets within ``WEAK_RING_GATE["check_m"]``. The
    radial distortion found with the focal length held is the true one. Those figures and decisions on the
    reference rendering only (:func:`reference_rendering`, ruling R29); on any other, decisions consistent with what
    the block reported (:func:`assert_consistent`), the figures printed."""
    from pyarchinit_photogrammetry.testing.synthetic import WEAK_RING_GATE

    assert weak_ring.aligned == (80, {"requested": "native", "used": "native", "fallback": None})
    ch = _copy_of(weak_ring.project, tmp_path / "gate").chunk
    before = _calibration(ch)[0]
    ch.optimize_cameras(engine=engine)
    assert ch.meta["sfm"]["optimize"]["engine_choice"] == {"requested": engine,
                                                           "used": "colmap" if engine == "auto" else "ppg"}
    calibration, errors = _calibration(ch)[0], _marker_rmse(ch)
    check = ch.meta["sfm"]["optimize"]["intrinsics"]
    print(f"weak ring gate ({engine}): PPG_GPU {_native.gpu_setting()}, reference {weak_ring.reference}, "
          f"focal {calibration[0]:.3f} px, k1 {calibration[4]:.5f}, held {check['held']}, "
          f"control {errors['optimized_control_rmse'] * 1000:.2f} mm, check {errors['optimized_check_rmse'] * 1000:.2f} mm, "
          f"shape {weak_ring.shape['truth_target_shape']:.5f}")
    assert_consistent(check, [before], [calibration])
    if not weak_ring.reference:
        return
    assert weak_ring.shape["truth_targets"] >= 6
    assert weak_ring.shape["truth_target_shape"] <= WEAK_RING_GATE["shape"]  # 0.00037 measured
    sensor = check["focal_sd"][0][0]
    assert check["held"]["focal"] == [sensor] and check["held"]["radial"] == []
    assert calibration[:4] == before[:4] and calibration[4] != 0.0  # the focal length held, the radial terms refined
    _assert_gate(calibration, errors)


@needs_core
@pytest.mark.slow
def test_the_weak_ring_with_many_tie_points_gives_up_its_tangential_terms_for_the_radial_ones(weak_ring, tmp_path,
                                                                                               monkeypatch):
    """The ring with many times the weight of its tie points - a block of thousands of tie points a photo, here
    their variance factor divided - and our formulation: every sd is smaller by the root of the divisor. The divisor
    is taken from the ring itself, so that the tangential terms, held at the ring's own weight, now pass clearly
    (0.35 px at the corner) and stay free: the tie points of a platform differ in their last bits (the CPU's SIFT
    against a GPU's, OpenCV's rendering of the texture), and a fixed divisor left the case 6 % under the line on one
    computer and over it on another. The radial terms, judged with them free, fail; held there, they would stay at
    the zero of the mapper and the check targets at 17 mm. They are judged again without the tangential terms, pass
    and are refined; the tangential terms are held for them: both lines of the gate hold. That case, its figures
    and the gate on the reference rendering (:func:`reference_rendering`, ruling R29); on any other, decisions
    consistent with what the block reported (:func:`assert_consistent`), the figures printed."""
    plain = _copy_of(weak_ring.project, tmp_path / "plain").chunk
    before = _calibration(plain)
    plain.optimize_cameras(engine="ppg")
    plain_check = plain.meta["sfm"]["optimize"]["intrinsics"]
    print(f"many tie points, plain: reference {weak_ring.reference}, held {plain_check['held']}, "
          f"tangential sd {plain_check['tangential_sd']}, notes {plain_check['notes']}")
    assert_consistent(plain_check, before, _calibration(plain))
    held_at = plain_check["tangential_sd"][0][1] if plain_check["tangential_sd"] else None  # 1.61 px measured (Mac)
    if held_at is None or held_at <= bundle.GROUP_SD_HOLD_PX:
        # the tangential terms pass at the ring's own weight: no case to make. On the reference rendering, from the
        # radial terms the mapper freed (R-B1), the plain ring is already that case: its tangential terms are given
        # up for the radial ones (2.35 px with them free, 0.50 px without, measured), and the gate holds
        if weak_ring.reference:
            sensor = plain_check["focal_sd"][0][0]
            assert plain_check["held"] == {**NOTHING_HELD, "focal": [sensor], "tangential": [sensor]}
            assert any("tangential held: given up for the radial terms" in n for n in plain_check["notes"])
            _assert_gate(_calibration(plain)[0], _marker_rmse(plain))
        return
    assert held_at > bundle.GROUP_SD_HOLD_PX
    divisor = (held_at / 0.35) ** 2  # about 21
    real = bundle.tie_variance_factor
    monkeypatch.setattr(bundle, "tie_variance_factor", lambda pb: real(pb) / divisor)
    ch = _copy_of(weak_ring.project, tmp_path / "many").chunk
    ch.optimize_cameras(engine="ppg")
    check = ch.meta["sfm"]["optimize"]["intrinsics"]
    sensor = check["focal_sd"][0][0]
    calibration, errors = _calibration(ch)[0], _marker_rmse(ch)
    print(f"many tie points: divisor {divisor:.1f}, held {check['held']}, notes {check['notes']}, tangential sd "
          f"{check['tangential_sd']}, radial sd {check['radial_sd']}, k1 {calibration[4]:.5f}, control "
          f"{errors['optimized_control_rmse'] * 1000:.2f} mm, check {errors['optimized_check_rmse'] * 1000:.2f} mm")
    assert_consistent(check, before, [calibration])
    if not weak_ring.reference:
        return
    assert check["held"] == {**NOTHING_HELD, "focal": [sensor], "tangential": [sensor]}
    assert 0.3 < check["tangential_sd"][0][1] < 0.4 and 0 < check["radial_sd"][0][1] < 0.1
    # the sd with the tangential terms free (0.68 px measured) is read back from the note and bounded, not spelt
    said = re.fullmatch(rf"sensor {sensor}: tangential held: given up for the radial terms \((\d\.\d\d) px at the "
                        r"image corner with it free, 0\.0\d px without\)", check["notes"][1])
    assert len(check["notes"]) == 2 and said and bundle.GROUP_SD_HOLD_PX < float(said.group(1)) < 1.0
    assert calibration[6:] == [0.0, 0.0]
    _assert_gate(calibration, errors)


@pytest.fixture(scope="module")
def classic_ring(tmp_path_factory):
    """The weak ring matched by the classic engine and not yet aligned: the path of the project (each test aligns
    copies of it with the classic alignment, which leaves the focal length somewhere else along its valley every
    time), and the scene."""
    import os

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.bench import steps
    from pyarchinit_photogrammetry.testing.synthetic import write_weak_ring_dataset

    root = tmp_path_factory.mktemp("classic_ring")
    scene = write_weak_ring_dataset(root / "set")
    doc = ppg.Document()
    ch = doc.add_chunk("ring")
    doc.save(root / "ring.ppg")
    ch.add_photos([root / "set" / "photos"])
    ch.detect_markers(steps.DEFAULT_TARGETS)
    previous = os.environ.get(sfm.RANSAC_SEED_ENV)
    os.environ[sfm.RANSAC_SEED_ENV] = "7"
    try:
        ch.match_photos(engine="classic", **steps.ARGUMENTS["match_photos"])
    finally:
        if previous is None:
            os.environ.pop(sfm.RANSAC_SEED_ENV, None)
        else:
            os.environ[sfm.RANSAC_SEED_ENV] = previous
    doc.save()
    return doc.path, scene, root / "set" / "gcp.csv"


@needs_core
@pytest.mark.slow
@pytest.mark.parametrize("run", [1, 2, 3])
def test_the_classic_weak_ring_meets_the_lines_of_the_gate(classic_ring, tmp_path, run):
    """The classic chain on the weak ring (rulings R24 and R26): COLMAP's incremental mapper refines the focal
    length of a block that does not determine it and leaves it anywhere along its valley (750 to 3900 px for a
    lens of 1000, another place each time). The optimisation judges it undetermined, walks it back to within two
    per cent of the focal length the photos state - the estimate of such a block weighs little beside the prior -
    and holds it there: the control targets end within ``WEAK_RING_GATE["control_m"]`` and the check targets
    within ``WEAK_RING_GATE["check_m"]``, whatever the alignment left - where the focal length held as the
    alignment left it gave 3 to 24 mm and 20 to 230 mm. Those figures and decisions on the reference rendering
    (:func:`reference_rendering`, ruling R29); on any other, decisions consistent with what the block reported."""
    from pyarchinit_photogrammetry.bench import steps, truth
    from pyarchinit_photogrammetry.testing.synthetic import WEAK_RING_GATE

    project, scene, reference = classic_ring
    doc = _copy_of(project, tmp_path / f"run{run}")
    ch = doc.chunk
    assert ch.align_cameras(engine="classic") == 80
    assert ch.meta["engine"]["align_cameras"]["used"] == "classic"
    ch.import_reference(reference, crs=scene.crs)
    steps.set_check(ch, [str(code) for code in truth.WEAK_RING_CHECK])
    ch.update_transform()
    sensor = next(iter(ch.sensors.values()))
    left, before = sensor.params[0], _calibration(ch)
    assert sensor.focal_source == "exif" and sfm.lens_prior(ch, sensor) == 1000.0
    ch.optimize_cameras()
    rep = ch.meta["sfm"]["optimize"]
    check = rep["intrinsics"]
    errors = _marker_rmse(ch)
    print(f"classic weak ring {run}: reference {reference_rendering(scene)}, left {left:.2f} px, held "
          f"{check['held']}, walked {check['lens_prior']}, control {errors['optimized_control_rmse'] * 1000:.2f} mm, "
          f"check {errors['optimized_check_rmse'] * 1000:.2f} mm")
    assert rep["engine_choice"]["used"] == "colmap"
    assert_consistent(check, before, _calibration(ch))
    if not reference_rendering(scene):
        return
    assert rep["engine_choice"]["used"] == "colmap" and check["held"]["focal"] == [sensor.key]
    walked, = check["lens_prior"]  # the mapper never leaves it at the prior: measured 716 to 3909 px
    assert walked["sensor"] == sensor.key and walked["estimate"] == pytest.approx(left) and walked["prior"] == 1000.0
    target = sfm.lens_target(left, walked["sd"], 1000.0)
    assert walked["target"] == pytest.approx(target, rel=1e-12) and walked["sd"] > 0.03
    assert abs(target / 1000.0 - 1.0) < 0.05  # 996 to 1018 px measured: the further the estimate, the more is left
    assert check["notes"][0].startswith(f"sensor {sensor.key}: focal walked to {target:.2f} px, between its "
                                        f"estimate {left:.2f} px (sd ")
    assert sensor.params[0] == pytest.approx(target, rel=1e-12)
    assert errors["optimized_control_rmse"] <= WEAK_RING_GATE["control_m"]  # 1.2 to 1.5 mm measured
    assert errors["optimized_check_rmse"] <= WEAK_RING_GATE["check_m"]  # 3.5 to 3.7 mm measured


@needs_core
@pytest.mark.slow
def test_freed_on_the_weak_ring_the_focal_length_runs_away_as_before(weak_ring, tmp_path):
    """``hold_undetermined=False`` on the weak ring is the optimisation of before: the focal length is refined and
    slides along its valley with the height of the cameras (1000 px to several thousand), the targets off by
    centimetres. The figures on the reference rendering (:func:`reference_rendering`, ruling R29)."""
    ch = _copy_of(weak_ring.project, tmp_path / "freed").chunk
    focal = _calibration(ch)[0][0]
    ch.optimize_cameras(hold_undetermined=False)
    rep = ch.meta["sfm"]["optimize"]
    print(f"freed weak ring: reference {weak_ring.reference}, focal {focal:.2f} -> {_calibration(ch)[0][0]:.2f} px, "
          f"check {_marker_rmse(ch)['optimized_check_rmse'] * 1000:.1f} mm")
    assert rep["focal_held"] is None and rep["intrinsics"]["checked"] is False
    if not weak_ring.reference:
        return
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
