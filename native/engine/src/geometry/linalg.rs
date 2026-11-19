//! The singular value decomposition the two-view geometry shares (`solvers`, `two_view`): one-sided
//! Jacobi rotations, accurate to roundoff on every matrix. nalgebra's is not used: its special code
//! for a static 3 x 3 matrix takes the eigenvectors of the Gram matrix, which lose the square of the
//! condition number, and its general code, measured on the scenes of tests/solvers.rs, returned
//! factorisations off by 1e-6 on a fundamental matrix and by 4e-2 on an essential matrix (two equal
//! singular values), and by 1e-11 to 1e-8 on random matrices of the sizes used here.

use nalgebra::{DMatrix, Matrix3};

/// The bound on the sweeps of the Jacobi decomposition, which converges in about ten on a 9 x 9
/// matrix; a matrix that reaches it is decomposed to the accuracy reached.
const MAX_SWEEPS: usize = 60;

/// A singular value decomposition by one-sided Jacobi rotations (Hestenes): `v` is orthogonal,
/// the columns of `av = A v` are orthogonal and their norms are the singular values `s`, in
/// descending order; the left singular vectors are the columns of `av` divided by them, where
/// they are not zero.
pub(crate) struct Svd {
    pub(crate) v: DMatrix<f64>,
    pub(crate) av: DMatrix<f64>,
    pub(crate) s: Vec<f64>,
}

/// The decomposition of a square or tall matrix: pairs of columns of `A v` are rotated until
/// every two are orthogonal (to a tolerance of roundoff times the root of the number of rows, as
/// LAPACK's), the same rotations accumulated in `v`.
pub(crate) fn jacobi_svd(a: &DMatrix<f64>) -> Svd {
    let n = a.ncols();
    let tolerance = f64::EPSILON * (a.nrows() as f64).sqrt();
    let mut av = a.clone();
    let mut v = DMatrix::<f64>::identity(n, n);
    for _ in 0..MAX_SWEEPS {
        let mut rotated = false;
        for p in 0..n {
            for q in p + 1..n {
                let alpha = av.column(p).norm_squared();
                let beta = av.column(q).norm_squared();
                let gamma = av.column(p).dot(&av.column(q));
                if gamma.abs() <= tolerance * (alpha * beta).sqrt() {
                    continue;
                }
                rotated = true;
                // the rotation that makes the two columns orthogonal, by the smaller angle
                let zeta = (beta - alpha) / (2.0 * gamma);
                let t = zeta.signum() / (zeta.abs() + (1.0 + zeta * zeta).sqrt());
                let c = 1.0 / (1.0 + t * t).sqrt();
                let s = c * t;
                rotate_columns(&mut av, p, q, c, s);
                rotate_columns(&mut v, p, q, c, s);
            }
        }
        if !rotated {
            break;
        }
    }
    let norms: Vec<f64> = (0..n).map(|k| av.column(k).norm()).collect();
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&i, &j| norms[j].total_cmp(&norms[i]));
    Svd {
        v: DMatrix::from_fn(n, n, |i, j| v[(i, order[j])]),
        av: DMatrix::from_fn(a.nrows(), n, |i, j| av[(i, order[j])]),
        s: order.iter().map(|&k| norms[k]).collect(),
    }
}

/// The columns `p` and `q` of `m` replaced by `c p - s q` and `s p + c q`.
fn rotate_columns(m: &mut DMatrix<f64>, p: usize, q: usize, c: f64, s: f64) {
    for row in 0..m.nrows() {
        let (x, y) = (m[(row, p)], m[(row, q)]);
        m[(row, p)] = c * x - s * y;
        m[(row, q)] = s * x + c * y;
    }
}

/// A 3 x 3 matrix as a dynamic one, for the decomposition.
pub(crate) fn dynamic(m: &Matrix3<f64>) -> DMatrix<f64> {
    DMatrix::from_fn(3, 3, |i, j| m[(i, j)])
}

/// The matrix scaled to a Frobenius norm of 1; `None` when it is zero or not finite.
pub(crate) fn unit(m: &Matrix3<f64>) -> Option<Matrix3<f64>> {
    let norm = m.norm();
    (norm > 0.0 && norm.is_finite()).then(|| m / norm)
}

#[cfg(test)]
mod tests {
    use super::*;
    use nalgebra::Vector3;

    /// Knuth's MMIX generator (as the tests of the crate), for the matrices of this check.
    struct Lcg(u64);

    impl Lcg {
        /// A value in [-1, 1).
        fn symmetric(&mut self) -> f64 {
            self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            2.0 * ((self.0 >> 11) as f64 / (1u64 << 53) as f64) - 1.0
        }
    }

    #[test]
    fn the_jacobi_decomposition_is_exact_to_roundoff() {
        let mut random = Lcg(7);
        for trial in 0..200 {
            // random matrices of the sizes used, and essential matrices (two equal singular
            // values, one zero), on which nalgebra's decomposition failed
            let (rows, cols) = [(3, 3), (4, 4), (9, 9), (3, 3)][trial % 4];
            let a = if trial % 4 == 3 {
                let t = Vector3::new(random.symmetric(), random.symmetric(), random.symmetric());
                let axis = Vector3::new(random.symmetric(), random.symmetric(), random.symmetric()).normalize();
                let angle = random.symmetric();
                let k = Matrix3::new(0.0, -axis.z, axis.y, axis.z, 0.0, -axis.x, -axis.y, axis.x, 0.0);
                let r = Matrix3::identity() + k * angle.sin() + k * k * (1.0 - angle.cos());
                dynamic(&(Matrix3::new(0.0, -t.z, t.y, t.z, 0.0, -t.x, -t.y, t.x, 0.0) * r))
            } else {
                DMatrix::from_fn(rows, cols, |_, _| random.symmetric())
            };
            let svd = jacobi_svd(&a);
            // roundoff: a few units of machine epsilon times the size of the matrix
            let roundoff = 64.0 * f64::EPSILON * a.norm();
            let recomposed = &svd.av * svd.v.transpose();
            let error = (&a - recomposed).norm();
            assert!(error <= roundoff, "trial {trial}: |A - A V V^T| = {error}");
            let identity = DMatrix::<f64>::identity(cols, cols);
            assert!((svd.v.transpose() * &svd.v - identity).norm() <= 64.0 * f64::EPSILON, "trial {trial}: V");
            for k in 1..cols {
                assert!(svd.s[k - 1] >= svd.s[k], "trial {trial}: singular values {:?}", svd.s);
                for j in 0..k {
                    let dot = svd.av.column(j).dot(&svd.av.column(k));
                    assert!(dot.abs() <= 64.0 * f64::EPSILON * svd.s[0] * svd.s[0], "trial {trial}: {j}, {k}: {dot}");
                }
            }
            if trial % 4 == 3 {
                let same = (svd.s[0] - svd.s[1]).abs() <= 1e-13 * svd.s[0] && svd.s[2] <= 1e-13 * svd.s[0];
                assert!(same, "trial {trial}: an essential matrix has singular values {:?}", svd.s);
            }
        }
    }
}
