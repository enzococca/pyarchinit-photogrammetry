"""The variances of the free intrinsics of a bundle problem by the native core (API 17), against a dense inverse of
its normal matrix."""
from __future__ import annotations

import copy

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

