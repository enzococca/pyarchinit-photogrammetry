"""The native bundle adjustment of the core (API 12) against scipy, the reference, on every kind of
residual (the gate of milestone 1, row 1): a cost not above scipy's, a gradient at the end as small as
scipy's, the parameters equal within a tenth of their sigma; the same agreement under a robust loss on
a block with wrong matches; and faster on the case with every residual at once."""
from __future__ import annotations

import copy
import statistics

import numpy as np
import pytest

from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.engine import bundle
from pyarchinit_photogrammetry.testing import bundle_problems

pytest.importorskip("pycolmap")
pytestmark = pytest.mark.skipif(not _native.has("bundle") or (_native.api() or 0) < _native.BUNDLE_API,
                                reason=bundle.NATIVE_MISSING)

CONVERGED = ("function", "gradient", "parameters", "function_and_parameters")  # the words of both solvers


def _gradient_at_the_end(pb: bundle.Problem, res: bundle.Result) -> float:
    """The largest entry of the gradient where ``res`` ended, by the native evaluation of the problem started
    there - the same evaluator for both solvers. A problem starts its rolling-shutter velocities and GNSS
    biases at zero, so ``problem_at`` cannot carry them: on such a problem it is the figure of the solver."""
    if pb.rolling_shutter or pb.gnss_bias:
        return float(res.gradient_max)
    return float(np.abs(bundle.evaluate(bundle.problem_at(pb, res)).gradient).max())


@pytest.mark.parametrize("name", bundle_problems.CASES)
def test_the_native_adjustment_reaches_scipys_optimum(name):
    pb = bundle_problems.case(name)
    ref = bundle.adjust(copy.deepcopy(pb), max_iterations=200, solver="scipy")
    nat = bundle.adjust(copy.deepcopy(pb), max_iterations=200, solver="native")
    assert ref.solver == "scipy" and nat.solver == "native" and nat.fallback is None
    assert nat.termination in CONVERGED
    assert nat.cost <= ref.cost * (1 + 1e-6)  # gate row 1
    g_ref, g_nat = _gradient_at_the_end(pb, ref), _gradient_at_the_end(pb, nat)
    assert g_nat <= max(g_ref, 1e-9 * max(1.0, ref.cost))  # as small as scipy's
    if ref.termination in CONVERGED:
        gap = np.abs(bundle_problems.parameter_gap(pb, nat, ref))
        assert (gap <= 0.1 * bundle_problems.sigmas(pb, ref)).all()  # a tenth of their sigma
    else:  # the velocity prior leaves a flat valley where scipy can stop at its budget: the cost decides there
        assert pb.rolling_shutter, f"scipy stopped unconverged ({ref.termination}) on {name}"


@pytest.mark.parametrize("loss", ["huber", "cauchy"])
def test_robust_losses_agree_with_scipy(loss):
    """Tie points alone, 2 % of the observations moved 40 px: under Huber and Cauchy (scale 1 px) the native
    solver ends with a cost not above scipy's with the same loss."""
    pb = bundle_problems.case("ties")
    rng = np.random.default_rng(40)
    wrong = rng.choice(len(pb.obs_xy), size=len(pb.obs_xy) // 50, replace=False)
    angle = rng.uniform(0, 2 * np.pi, len(wrong))
    pb.obs_xy = pb.obs_xy.copy()
    pb.obs_xy[wrong] += 40.0 * np.column_stack([np.cos(angle), np.sin(angle)])
    ref = bundle.adjust(copy.deepcopy(pb), max_iterations=200, solver="scipy", loss=loss, loss_scale=1.0)
    nat = bundle.adjust(copy.deepcopy(pb), max_iterations=200, solver="native", loss=loss, loss_scale=1.0)
    assert ref.solver == "scipy" and nat.solver == "native" and nat.fallback is None
    assert nat.cost <= ref.cost * (1 + 1e-6)


@pytest.mark.slow
def test_the_native_adjustment_is_faster_than_scipy_on_the_everything_case():
    pb = bundle_problems.case("everything")
    seconds = {}
    for solver in ("native", "scipy"):
        runs = [bundle.adjust(copy.deepcopy(pb), max_iterations=200, solver=solver) for _ in range(3)]
        assert all(r.solver == solver for r in runs)
        seconds[solver] = statistics.median(r.seconds for r in runs)
    assert seconds["native"] < seconds["scipy"]
