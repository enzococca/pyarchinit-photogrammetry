//! What the two-view geometry shares (`solvers`, `two_view`, `refine`): Hartley's normalisation of a
//! set of points, and the singular value decomposition, one-sided
//! Jacobi rotations, accurate to roundoff on every matrix. nalgebra's is not used: its special code
//! for a static 3 x 3 matrix takes the eigenvectors of the Gram matrix, which lose the square of the
//! condition number, and its general code, measured on the scenes of tests/solvers.rs, returned
//! factorisations off by 1e-6 on a fundamental matrix and by 4e-2 on an essential matrix (two equal
//! singular values), and by 1e-11 to 1e-8 on random matrices of the sizes used here.

use nalgebra::Matrix3;

/// The bound on the sweeps of the Jacobi decomposition, which converges in about ten on a 9 x 9
/// matrix; a matrix that reaches it is decomposed to the accuracy reached.
const MAX_SWEEPS: usize = 60;

/// The most columns a decomposition takes (the 9 x 9 systems of the two-view solvers).
const MAX_COLUMNS: usize = 16;

/// A singular value decomposition by one-sided Jacobi rotations (Hestenes) of an `R` x `C` matrix
/// held as its `C` columns: `v` is orthogonal, the columns of `av = A v` are orthogonal and their
/// norms are the singular values `s`, in descending order; the left singular vectors are the
/// columns of `av` divided by them, where they are not zero. Every field holds columns (`v[k]` is
/// the k-th column of V). Fixed sizes on the stack: the decompositions of the RANSAC hypotheses
/// (9 x 9, 4 x 4, 3 x 3) allocate nothing.
pub(crate) struct Svd<const R: usize, const C: usize> {
    pub(crate) v: [[f64; C]; C],
    pub(crate) av: [[f64; R]; C],
    pub(crate) s: [f64; C],
}

/// The decomposition of a square or tall matrix given by its columns: pairs of columns of `A v`
/// are rotated while they are not orthogonal to a tolerance of roundoff times the root of the
/// number of rows (the cosine of their angle), the same rotations accumulated in `v`; the sweeps
/// stop when no pair was farther from orthogonal than that tolerance times the root of the number
/// of columns (LAPACK's `dgesvj` stops on the same bound). A column whose norm has fallen to
/// roundoff of the whole matrix (`EPSILON` times its Frobenius norm) is zero to the accuracy of
/// the decomposition and is rotated no more: the columns of a matrix with fewer independent rows
/// than columns (the zero-padded system of a minimal sample) live in the span of those rows, so
/// such a column always has a large cosine with some other, and each rotation only shrank it by
/// another factor of roundoff, down to underflow, where its cosine is no longer a number and every
/// sweep up to `MAX_SWEEPS` rotated it again. The squared norms of the
/// columns are computed at the start of each sweep and carried through its rotations by the exact
/// update of a Jacobi rotation (`alpha - t gamma`, `beta + t gamma`), so that each pair costs one
/// dot product instead of three; the singular values are the norms computed again at the end.
pub(crate) fn jacobi_svd<const R: usize, const C: usize>(a: &[[f64; R]; C]) -> Svd<R, C> {
    jacobi(a, true)
}

/// The singular values alone, in descending order (`v` is not accumulated and is left as the
/// identity, unordered).
pub(crate) fn singular_values<const R: usize, const C: usize>(a: &[[f64; R]; C]) -> [f64; C] {
    jacobi(a, false).s
}

/// The singular values of the matrix whose columns are `columns` (as many as fit in the
/// fixed-size arrays of the minimal solvers, up to nine), into `out` in descending order (one per
/// column); `columns` is left rotated. Nothing is accumulated besides the values.
pub(crate) fn singular_values_of<const R: usize>(columns: &mut [[f64; R]], out: &mut [f64]) {
    assert!(out.len() == columns.len(), "one singular value per column");
    orthogonalise::<R, 1>(columns, None);
    for (value, column) in out.iter_mut().zip(columns.iter()) {
        *value = dot(column, column).sqrt();
    }
    out.sort_unstable_by(|x, y| y.total_cmp(x));
}

fn jacobi<const R: usize, const C: usize>(a: &[[f64; R]; C], accumulate: bool) -> Svd<R, C> {
    let mut av = *a;
    let mut v = [[0.0; C]; C];
    for (k, column) in v.iter_mut().enumerate() {
        column[k] = 1.0;
    }
    orthogonalise(&mut av, if accumulate { Some(&mut v[..]) } else { None });
    let norms: [f64; C] = std::array::from_fn(|k| dot(&av[k], &av[k]).sqrt());
    // the columns by descending norm, a stable insertion sort (equal norms keep their order)
    let mut order: [usize; C] = std::array::from_fn(|k| k);
    for i in 1..C {
        let mut j = i;
        while j > 0 && norms[order[j - 1]] < norms[order[j]] {
            order.swap(j - 1, j);
            j -= 1;
        }
    }
    Svd {
        v: std::array::from_fn(|j| v[order[j]]),
        av: std::array::from_fn(|j| av[order[j]]),
        s: std::array::from_fn(|j| norms[order[j]]),
    }
}

/// The sweeps of the one-sided Jacobi rotations on the columns `av` (at most `MAX_COLUMNS`), the
/// rotations accumulated in the columns of `v` when it is given (one per column of `av`).
fn orthogonalise<const R: usize, const K: usize>(av: &mut [[f64; R]], mut v: Option<&mut [[f64; K]]>) {
    let columns = av.len();
    assert!(columns <= MAX_COLUMNS, "at most {MAX_COLUMNS} columns");
    let tolerance = f64::EPSILON * (R as f64).sqrt();
    let margin = tolerance * (columns as f64).sqrt();
    let floor = av.iter().flatten().map(|x| x * x).sum::<f64>() * f64::EPSILON * f64::EPSILON;
    let mut norms = [0.0; MAX_COLUMNS];
    for _ in 0..MAX_SWEEPS {
        for (norm, column) in norms.iter_mut().zip(av.iter()) {
            *norm = dot(column, column);
        }
        let mut rotated = false;
        for p in 0..columns {
            for q in p + 1..columns {
                let (alpha, beta) = (norms[p], norms[q]);
                if alpha <= floor || beta <= floor {
                    continue;
                }
                let gamma = dot(&av[p], &av[q]);
                let bound = (alpha * beta).sqrt();
                if gamma.abs() <= tolerance * bound {
                    continue;
                }
                rotated |= gamma.abs() > margin * bound;
                // the rotation that makes the two columns orthogonal, by the smaller angle
                let zeta = (beta - alpha) / (2.0 * gamma);
                let t = zeta.signum() / (zeta.abs() + (1.0 + zeta * zeta).sqrt());
                let c = 1.0 / (1.0 + t * t).sqrt();
                let s = c * t;
                rotate_columns(av, p, q, c, s);
                if let Some(v) = v.as_deref_mut() {
                    rotate_columns(v, p, q, c, s);
                }
                norms[p] = (alpha - t * gamma).max(0.0);
                norms[q] = (beta + t * gamma).max(0.0);
            }
        }
        if !rotated {
            break;
        }
    }
}

#[inline(always)]
fn dot<const N: usize>(x: &[f64; N], y: &[f64; N]) -> f64 {
    let mut sum = 0.0;
    for k in 0..N {
        sum += x[k] * y[k];
    }
    sum
}

/// The columns `p` and `q` (`p < q`) replaced by `c p - s q` and `s p + c q`.
#[inline(always)]
fn rotate_columns<const N: usize>(m: &mut [[f64; N]], p: usize, q: usize, c: f64, s: f64) {
    let (low, high) = m.split_at_mut(q);
    let (column_p, column_q) = (&mut low[p], &mut high[0]);
    for row in 0..N {
        let (x, y) = (column_p[row], column_q[row]);
        column_p[row] = c * x - s * y;
        column_q[row] = s * x + c * y;
    }
}

/// The columns of a 3 x 3 matrix, for the decomposition.
pub(crate) fn columns(m: &Matrix3<f64>) -> [[f64; 3]; 3] {
    std::array::from_fn(|c| std::array::from_fn(|r| m[(r, c)]))
}

/// The matrix scaled to a Frobenius norm of 1; `None` when it is zero or not finite.
pub(crate) fn unit(m: &Matrix3<f64>) -> Option<Matrix3<f64>> {
    let norm = m.norm();
    (norm > 0.0 && norm.is_finite()).then(|| m / norm)
}

/// Hartley's normalisation of a set of points: the similarity `matrix` that moves their centroid
/// to the origin and makes their root mean square distance from it `sqrt 2`, the `points` it
/// gives and its `inverse`.
pub(crate) struct Normalisation {
    pub(crate) points: Vec<[f64; 2]>,
    pub(crate) matrix: Matrix3<f64>,
    pub(crate) inverse: Matrix3<f64>,
}

/// `None` when the points coincide (the scale is not defined).
pub(crate) fn normalise(points: &[[f64; 2]]) -> Option<Normalisation> {
    let similarity = Similarity::of(points)?;
    Some(Normalisation {
        points: points.iter().map(|&p| similarity.apply(p)).collect(),
        matrix: similarity.matrix(),
        inverse: similarity.inverse(),
    })
}

/// Hartley's similarity of a set of points without the points it gives (nothing allocated, for
/// the minimal solvers): `apply` maps a point, `matrix` and `inverse` are the similarity and its
/// inverse as matrices.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Similarity {
    cx: f64,
    cy: f64,
    s: f64,
}

impl Similarity {
    /// `None` when the points coincide (the scale is not defined).
    pub(crate) fn of(points: &[[f64; 2]]) -> Option<Similarity> {
        let n = points.len() as f64;
        let (sx, sy) = points.iter().fold((0.0, 0.0), |(x, y), p| (x + p[0], y + p[1]));
        let (cx, cy) = (sx / n, sy / n);
        let rms = (points.iter().map(|p| (p[0] - cx).powi(2) + (p[1] - cy).powi(2)).sum::<f64>() / n).sqrt();
        if !(rms > 0.0 && rms.is_finite()) {
            return None;
        }
        Some(Similarity { cx, cy, s: 2f64.sqrt() / rms })
    }

    #[inline]
    pub(crate) fn apply(&self, p: [f64; 2]) -> [f64; 2] {
        [(p[0] - self.cx) * self.s, (p[1] - self.cy) * self.s]
    }

    pub(crate) fn matrix(&self) -> Matrix3<f64> {
        let s = self.s;
        Matrix3::new(s, 0.0, -s * self.cx, 0.0, s, -s * self.cy, 0.0, 0.0, 1.0)
    }

    pub(crate) fn inverse(&self) -> Matrix3<f64> {
        Matrix3::new(1.0 / self.s, 0.0, self.cx, 0.0, 1.0 / self.s, self.cy, 0.0, 0.0, 1.0)
    }
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

    /// The checks of a decomposition: `A = (A V) V^T` and `V^T V = I` to roundoff, the singular
    /// values descending, the columns of `A V` orthogonal; the singular values alone are the same.
    fn check<const R: usize, const C: usize>(a: &[[f64; R]; C], trial: usize) -> [f64; C] {
        let svd = jacobi_svd(a);
        let norm = a.iter().flatten().map(|x| x * x).sum::<f64>().sqrt();
        // roundoff: a few units of machine epsilon times the size of the matrix
        let roundoff = 64.0 * f64::EPSILON * norm;
        let mut error = 0.0;
        for r in 0..R {
            for c in 0..C {
                let recomposed: f64 = (0..C).map(|k| svd.av[k][r] * svd.v[k][c]).sum();
                error += (a[c][r] - recomposed).powi(2);
            }
        }
        let error = error.sqrt();
        assert!(error <= roundoff, "trial {trial}: |A - A V V^T| = {error}");
        let mut orthogonality = 0.0;
        for i in 0..C {
            for j in 0..C {
                let d: f64 = (0..C).map(|k| svd.v[i][k] * svd.v[j][k]).sum();
                orthogonality += (d - if i == j { 1.0 } else { 0.0 }).powi(2);
            }
        }
        assert!(orthogonality.sqrt() <= 64.0 * f64::EPSILON, "trial {trial}: V");
        for k in 1..C {
            assert!(svd.s[k - 1] >= svd.s[k], "trial {trial}: singular values {:?}", svd.s);
            for j in 0..k {
                let d = dot(&svd.av[j], &svd.av[k]);
                assert!(d.abs() <= 64.0 * f64::EPSILON * svd.s[0] * svd.s[0], "trial {trial}: {j}, {k}: {d}");
            }
        }
        let values = singular_values(a);
        for k in 0..C {
            assert!((values[k] - svd.s[k]).abs() <= roundoff, "trial {trial}: {values:?} against {:?}", svd.s);
        }
        svd.s
    }

    #[test]
    fn the_jacobi_decomposition_is_exact_to_roundoff() {
        let mut random = Lcg(7);
        for trial in 0..200 {
            // random matrices of the sizes used, and essential matrices (two equal singular
            // values, one zero), on which nalgebra's decomposition failed
            match trial % 4 {
                0 => {
                    let a: [[f64; 3]; 3] = std::array::from_fn(|_| std::array::from_fn(|_| random.symmetric()));
                    check(&a, trial);
                }
                1 => {
                    let a: [[f64; 4]; 4] = std::array::from_fn(|_| std::array::from_fn(|_| random.symmetric()));
                    check(&a, trial);
                }
                2 => {
                    let a: [[f64; 9]; 9] = std::array::from_fn(|_| std::array::from_fn(|_| random.symmetric()));
                    check(&a, trial);
                }
                _ => {
                    let t = Vector3::new(random.symmetric(), random.symmetric(), random.symmetric());
                    let axis =
                        Vector3::new(random.symmetric(), random.symmetric(), random.symmetric()).normalize();
                    let angle = random.symmetric();
                    let k = Matrix3::new(0.0, -axis.z, axis.y, axis.z, 0.0, -axis.x, -axis.y, axis.x, 0.0);
                    let r = Matrix3::identity() + k * angle.sin() + k * k * (1.0 - angle.cos());
                    let e = Matrix3::new(0.0, -t.z, t.y, t.z, 0.0, -t.x, -t.y, t.x, 0.0) * r;
                    let s = check(&columns(&e), trial);
                    let same = (s[0] - s[1]).abs() <= 1e-13 * s[0] && s[2] <= 1e-13 * s[0];
                    assert!(same, "trial {trial}: an essential matrix has singular values {s:?}");
                }
            }
        }
    }
}
