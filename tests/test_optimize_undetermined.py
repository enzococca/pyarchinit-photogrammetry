"""The variances of the free intrinsics of a bundle problem by the native core (API 17), against a dense inverse of
its normal matrix; the variance factor of the tie points and the marginal sd of the focal length of each camera of
a problem, on synthetic blocks that determine it (pitched photos) and that do not (photos looking straight down
over nearly flat ground)."""
from __future__ import annotations

import copy
import dataclasses

import numpy as np
import pytest

from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.engine import bundle
from pyarchinit_photogrammetry.testing import bundle_problems

pytest.importorskip("pycolmap")
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
