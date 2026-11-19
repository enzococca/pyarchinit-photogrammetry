//! The bundle adjustment of the native engine: the problem of `engine/bundle.py` and the columns
//! of its unknowns (`problem`), its residuals and their Jacobian by forward dual numbers
//! (`residuals`, `dual`), the normal equations reduced to the cameras by eliminating the points
//! (`schur`, dense; `sparse`, faer's sparse Cholesky; `pcg`, the conjugate gradients without
//! forming them), the Levenberg-Marquardt iteration over them (`solve`) and the synthetic blocks
//! of the tests and of `ppg-bench adjust` (`synthetic`).

use std::ops::Neg;

use crate::camera::Real;

pub mod dual;
mod pcg;
pub mod problem;
pub mod residuals;
mod schur;
pub mod solve;
mod sparse;
pub mod synthetic;

pub use dual::Dual;
pub use problem::{
    compose, exp_rotation, CameraBlock, Layout, Obs, Parameters, Problem, MARKER_PIXEL_ACCURACY,
    ROLLING_SHUTTER_SIGMA_M, ROLLING_SHUTTER_SIGMA_RAD,
};
pub use residuals::{evaluate, evaluate_at, Evaluation, Loss, BEHIND_CAMERA_RESIDUAL};
pub use solve::{
    choose_linear_solver, solve, LinearSolver, Options, Solution, Termination, DENSE_MAX_COLUMNS, DENSE_MAX_PHOTOS,
    PCG_ABOVE_BYTES, ROBUST_FUNCTION_TOLERANCE,
};

/// The scalar the residuals are written for: `f64` for the values alone (the cost of a trial
/// step), the dual numbers of `dual` for the derivatives. Each residual is written once, generic
/// over this trait.
pub trait Scalar: Real + Neg<Output = Self> {
    fn sqrt(self) -> Self;
    fn sin(self) -> Self;
    fn cos(self) -> Self;
}

impl Scalar for f64 {
    fn sqrt(self) -> Self {
        f64::sqrt(self)
    }

    fn sin(self) -> Self {
        f64::sin(self)
    }

    fn cos(self) -> Self {
        f64::cos(self)
    }
}

impl<const N: usize> Scalar for Dual<N> {
    fn sqrt(self) -> Self {
        Dual::sqrt(self)
    }

    fn sin(self) -> Self {
        let (s, c) = self.v.sin_cos();
        Dual { v: s, d: self.d.map(|x| x * c) }
    }

    fn cos(self) -> Self {
        let (s, c) = self.v.sin_cos();
        Dual { v: c, d: self.d.map(|x| -x * s) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A function written once over `Scalar`, evaluated in `f64` and in dual numbers.
    fn f<T: Scalar>(x: T, y: T) -> T {
        (x * x + y * y).sqrt() * x.sin() - y.cos() / x
    }

    #[test]
    fn the_dual_value_is_the_f64_value_and_its_derivatives_are_the_rules() {
        for (x0, y0) in [(1.5, 2.0), (-0.75, 3.25), (10.0, 0.1)] {
            let z = f(Dual::<2>::variable(x0, 0), Dual::<2>::variable(y0, 1));
            assert_eq!(z.v.to_bits(), f(x0, y0).to_bits(), "at ({x0}, {y0})");
            let r = (x0 * x0 + y0 * y0).sqrt();
            let dx = x0 / r * x0.sin() + r * x0.cos() + y0.cos() / (x0 * x0);
            let dy = y0 / r * x0.sin() + y0.sin() / x0;
            for (got, want) in [(z.d[0], dx), (z.d[1], dy)] {
                assert!((got - want).abs() <= 1e-14 * want.abs().max(1.0), "at ({x0}, {y0}): {got} vs {want}");
            }
        }
    }
}
