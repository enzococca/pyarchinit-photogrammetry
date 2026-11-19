//! Forward-mode dual numbers: a value and its derivatives with respect to `N` variables, so that
//! the code that projects a point also gives its Jacobian.

use std::ops::{Add, Div, Mul, Neg, Sub};

use crate::camera::Real;

/// A value `v` and its derivatives `d[i]` with respect to the variable `i`. The operations follow
/// the rules of differentiation; the value part is computed with the same `f64` operations as the
/// value alone, so `project::<Dual<N>>` gives the pixels of `project::<f64>` bit for bit.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Dual<const N: usize> {
    pub v: f64,
    pub d: [f64; N],
}

impl<const N: usize> Dual<N> {
    /// A value that depends on no variable.
    pub fn constant(v: f64) -> Self {
        Dual { v, d: [0.0; N] }
    }

    /// The variable number `i` (below `N`), at the value `v`.
    pub fn variable(v: f64, i: usize) -> Self {
        assert!(i < N, "variable {i} of a dual number with {N} variables");
        let mut d = [0.0; N];
        d[i] = 1.0;
        Dual { v, d }
    }

    /// The square root; at zero its derivatives are not a number (a division by zero).
    pub fn sqrt(self) -> Self {
        let v = self.v.sqrt();
        let mut d = self.d;
        for x in d.iter_mut() {
            *x /= 2.0 * v;
        }
        Dual { v, d }
    }
}

impl<const N: usize> Real for Dual<N> {
    fn from_f64(x: f64) -> Self {
        Dual::constant(x)
    }

    fn value(self) -> f64 {
        self.v
    }
}

impl<const N: usize> Neg for Dual<N> {
    type Output = Self;

    fn neg(self) -> Self {
        Dual { v: -self.v, d: self.d.map(|x| -x) }
    }
}

impl<const N: usize> Add for Dual<N> {
    type Output = Self;

    fn add(self, rhs: Self) -> Self {
        let mut d = self.d;
        for (x, y) in d.iter_mut().zip(rhs.d) {
            *x += y;
        }
        Dual { v: self.v + rhs.v, d }
    }
}

impl<const N: usize> Sub for Dual<N> {
    type Output = Self;

    fn sub(self, rhs: Self) -> Self {
        let mut d = self.d;
        for (x, y) in d.iter_mut().zip(rhs.d) {
            *x -= y;
        }
        Dual { v: self.v - rhs.v, d }
    }
}

impl<const N: usize> Mul for Dual<N> {
    type Output = Self;

    fn mul(self, rhs: Self) -> Self {
        let mut d = self.d;
        for (x, y) in d.iter_mut().zip(rhs.d) {
            *x = *x * rhs.v + self.v * y;
        }
        Dual { v: self.v * rhs.v, d }
    }
}

impl<const N: usize> Div for Dual<N> {
    type Output = Self;

    fn div(self, rhs: Self) -> Self {
        let denominator = rhs.v * rhs.v;
        let mut d = self.d;
        for (x, y) in d.iter_mut().zip(rhs.d) {
            *x = (*x * rhs.v - self.v * y) / denominator;
        }
        Dual { v: self.v / rhs.v, d }
    }
}

impl<const N: usize> Add<f64> for Dual<N> {
    type Output = Self;

    fn add(self, rhs: f64) -> Self {
        Dual { v: self.v + rhs, d: self.d }
    }
}

impl<const N: usize> Sub<f64> for Dual<N> {
    type Output = Self;

    fn sub(self, rhs: f64) -> Self {
        Dual { v: self.v - rhs, d: self.d }
    }
}

impl<const N: usize> Mul<f64> for Dual<N> {
    type Output = Self;

    fn mul(self, rhs: f64) -> Self {
        Dual { v: self.v * rhs, d: self.d.map(|x| x * rhs) }
    }
}

impl<const N: usize> Div<f64> for Dual<N> {
    type Output = Self;

    fn div(self, rhs: f64) -> Self {
        Dual { v: self.v / rhs, d: self.d.map(|x| x / rhs) }
    }
}

impl<const N: usize> Add<Dual<N>> for f64 {
    type Output = Dual<N>;

    fn add(self, rhs: Dual<N>) -> Dual<N> {
        Dual { v: self + rhs.v, d: rhs.d }
    }
}

impl<const N: usize> Sub<Dual<N>> for f64 {
    type Output = Dual<N>;

    fn sub(self, rhs: Dual<N>) -> Dual<N> {
        Dual { v: self - rhs.v, d: rhs.d.map(|y| -y) }
    }
}

impl<const N: usize> Mul<Dual<N>> for f64 {
    type Output = Dual<N>;

    fn mul(self, rhs: Dual<N>) -> Dual<N> {
        Dual { v: self * rhs.v, d: rhs.d.map(|y| self * y) }
    }
}

impl<const N: usize> Div<Dual<N>> for f64 {
    type Output = Dual<N>;

    fn div(self, rhs: Dual<N>) -> Dual<N> {
        let denominator = rhs.v * rhs.v;
        Dual { v: self / rhs.v, d: rhs.d.map(|y| -(self * y) / denominator) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::camera::Real;

    /// `got` within `tol` of `want`, relative to the larger of the two (both zero passes).
    fn close(got: f64, want: f64, tol: f64) -> bool {
        let scale = got.abs().max(want.abs());
        scale == 0.0 || (got - want).abs() <= tol * scale
    }

    #[test]
    fn dual_arithmetic_follows_the_rules() {
        // x and y carry three derivative components each, so that the rules are checked for
        // general tangents and not only for the unit ones of a variable
        for (x0, y0) in [(1.5, 2.0), (-0.75, 3.25), (10.0, 0.1)] {
            let x = Dual::<3> { v: x0, d: [1.0, 0.0, -0.5] };
            let y = Dual::<3> { v: y0, d: [0.0, 1.0, 2.0] };
            let product = x * y;
            let quotient = x / y;
            let root = Dual::<3> { v: x0.abs(), d: x.d }.sqrt();
            assert_eq!(product.v, x0 * y0);
            assert_eq!(quotient.v, x0 / y0);
            assert_eq!(root.v, x0.abs().sqrt());
            for k in 0..3 {
                // d(x y) = x dy + y dx
                let want = x0 * y.d[k] + y0 * x.d[k];
                assert!(close(product.d[k], want, 1e-15), "d(x y)[{k}] at ({x0}, {y0}): {}", product.d[k]);
                // d(x / y) = (y dx - x dy) / y^2
                let want = (y0 * x.d[k] - x0 * y.d[k]) / (y0 * y0);
                assert!(close(quotient.d[k], want, 1e-15), "d(x / y)[{k}] at ({x0}, {y0}): {}", quotient.d[k]);
                // d(sqrt x) = dx / (2 sqrt x)
                let want = x.d[k] / (2.0 * x0.abs().sqrt());
                assert!(close(root.d[k], want, 1e-15), "d(sqrt x)[{k}] at {}: {}", x0.abs(), root.d[k]);
            }
        }
    }

    #[test]
    fn constants_variables_and_the_operations_with_f64() {
        let x = Dual::<2>::variable(3.0, 0);
        let c = Dual::<2>::constant(4.0);
        assert_eq!(x, Dual { v: 3.0, d: [1.0, 0.0] });
        assert_eq!(c, Dual { v: 4.0, d: [0.0, 0.0] });
        assert_eq!(x.value(), 3.0);
        assert_eq!(Dual::<2>::from_f64(4.0), c);
        assert_eq!(x + c, Dual { v: 7.0, d: [1.0, 0.0] });
        assert_eq!(x - c, Dual { v: -1.0, d: [1.0, 0.0] });
        assert_eq!(c - x, Dual { v: 1.0, d: [-1.0, 0.0] });
        assert_eq!(x * c, Dual { v: 12.0, d: [4.0, 0.0] });
        assert_eq!(x / c, Dual { v: 0.75, d: [0.25, 0.0] });
        assert_eq!(c / x, Dual { v: 4.0 / 3.0, d: [-4.0 / 9.0, 0.0] });
        assert_eq!(-x, Dual { v: -3.0, d: [-1.0, 0.0] });
        assert_eq!(x.sqrt(), Dual { v: 3f64.sqrt(), d: [1.0 / (2.0 * 3f64.sqrt()), 0.0] });
        // a plain f64 on either side is the same as its constant
        assert_eq!(x + 4.0, x + c);
        assert_eq!(4.0 + x, c + x);
        assert_eq!(x - 4.0, x - c);
        assert_eq!(4.0 - x, c - x);
        assert_eq!(x * 4.0, x * c);
        assert_eq!(4.0 * x, c * x);
        assert_eq!(x / 4.0, x / c);
        assert_eq!(4.0 / x, c / x);
    }
}
