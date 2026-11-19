//! The minimal solvers of two views, on points given as `[x, y]`: the essential matrix of five
//! normalised correspondences (Nister 2004), the fundamental matrix of seven (the cubic of a
//! two-dimensional null space) and of eight or more (Hartley's normalised eight-point algorithm),
//! the homography of four or more (the direct linear transform), the Sampson and transfer errors
//! that measure a model on a correspondence, and the pose of an essential matrix chosen by the
//! cheirality of the triangulated points. The estimators are arranged as COLMAP's `estimators/`
//! (BSD 3-clause, see native/NOTICE); the coefficient tables COLMAP generates offline are computed
//! here by polynomial arithmetic.
//!
//! Conventions: `x2^T F x1 = 0` and `x2^T E x1 = 0` for a correspondence `(x1, x2)`, `x2 ~ H x1`,
//! the pose is cam2_from_cam1 (`X2 = R X1 + t`) and `E = [t]_x R`. The matrices returned have a
//! Frobenius norm of 1. Every function gives no solution (an empty vector or `None`), never NaN
//! and never a panic, on degenerate or non-finite input.
//!
//! The null vectors and the decompositions come from a singular value decomposition computed here
//! by one-sided Jacobi rotations, accurate to roundoff on every matrix. nalgebra's is not used:
//! its special code for a static 3 x 3 matrix takes the eigenvectors of the Gram matrix, which
//! lose the square of the condition number, and its general code, measured on the scenes of
//! tests/solvers.rs, returned factorisations off by 1e-6 on a fundamental matrix and by 4e-2 on
//! an essential matrix (two equal singular values), and by 1e-11 to 1e-8 on random matrices of
//! the sizes used here. A tall least-squares system is reduced first to the R of nalgebra's QR
//! decomposition, which has the same right singular vectors and came out exact to roundoff.

use std::ops::{Add, Mul, Sub};

use nalgebra::{DMatrix, Matrix3, SMatrix, SVector, Vector3, LU, QR};

/// A singular value below this fraction of the largest counts as zero: the system has lost rank,
/// the configuration is degenerate. Exact degeneracy computes to about 1e-16 of the largest; a
/// configuration this close to it gives no usable model anyway.
const RANK_TOLERANCE: f64 = 1e-10;

/// The bound on the sweeps of the Jacobi decomposition, which converges in about ten on a 9 x 9
/// matrix; a matrix that reaches it is decomposed to the accuracy reached.
const MAX_SWEEPS: usize = 60;

/// A solution of the five-point constraints is accepted when its residual is below this fraction
/// of the bound on it (the norms of the constraint matrix and of the monomials): a converged one
/// is at roundoff, 1e-15; a root the back-substitution could not place leaves 1e-4 or more.
const CONVERGED: f64 = 1e-10;

/// The essential matrices of five correspondences in normalised coordinates (Nister 2004, as
/// COLMAP's `EssentialMatrixFivePointEstimator`): up to ten, each of unit Frobenius norm. The
/// null space of the epipolar constraints gives `E = x X + y Y + z Z + W`; the ten cubic
/// constraints on E (`det E = 0`, `2 E E^T E - tr(E E^T) E = 0`) are reduced by Gauss-Jordan
/// elimination to a 3 x 3 matrix of polynomials in z whose determinant, of degree ten, vanishes
/// at the solutions; x and y follow from its null vector. No solution when the five points are
/// degenerate (fewer than five independent constraints) or not finite.
pub fn essential_five_point(x1: &[[f64; 2]; 5], x2: &[[f64; 2]; 5]) -> Vec<Matrix3<f64>> {
    five_point(x1, x2).unwrap_or_default()
}

fn five_point(x1: &[[f64; 2]; 5], x2: &[[f64; 2]; 5]) -> Option<Vec<Matrix3<f64>>> {
    if !(finite(x1) && finite(x2)) {
        return None;
    }
    // E = x X + y Y + z Z + W over the null space of the epipolar constraints, W the vector of
    // the smallest singular value (COLMAP's order)
    let rows: Vec<[f64; 9]> = x1.iter().zip(x2).map(|(&a, &b)| epipolar_row(a, b)).collect();
    let basis = null_space(&rows, 4)?;
    let [e_w, e_z, e_y, e_x] = [0, 1, 2, 3].map(|i| as_matrix(&basis[i]));
    // the ten cubic constraints, reduced so that each monomial of the left block is written in
    // the ten monomials of the right block (the Gauss-Jordan elimination, here an LU solve)
    let constraints = five_point_constraints(&[e_x, e_y, e_z, e_w]);
    let left: SMatrix<f64, 10, 10> = constraints.fixed_view::<10, 10>(0, 0).into_owned();
    let right: SMatrix<f64, 10, 10> = constraints.fixed_view::<10, 10>(0, 10).into_owned();
    let reduced = LU::new(left).solve(&right)?;
    if !reduced.iter().all(|v| v.is_finite()) {
        return None;
    }
    // Nister's 3 x 3 matrix B(z): the rows <e> - z <f> of the pairs (x^2 z, x^2), (y^2 z, y^2)
    // and (x y z, x y), each a polynomial in z times x, one times y and one alone (the columns
    // of the right block are x z^2, x z, x, y z^2, y z, y, z^3, z^2, z, 1); ascending powers
    let mut b: [[Vec<f64>; 3]; 3] = Default::default();
    for (k, (e, f)) in [(4, 5), (6, 7), (8, 9)].into_iter().enumerate() {
        let r = |i: usize, j: usize| reduced[(i, j)];
        b[k][0] = vec![r(e, 2), r(e, 1) - r(f, 2), r(e, 0) - r(f, 1), -r(f, 0)];
        b[k][1] = vec![r(e, 5), r(e, 4) - r(f, 5), r(e, 3) - r(f, 4), -r(f, 3)];
        b[k][2] = vec![r(e, 9), r(e, 8) - r(f, 9), r(e, 7) - r(f, 8), r(e, 6) - r(f, 7), -r(f, 6)];
    }
    // det B(z), of degree ten in z: each real root gives a solution
    let cofactor =
        |c1: usize, c2: usize| poly_sub(&poly_mul(&b[1][c1], &b[2][c2]), &poly_mul(&b[1][c2], &b[2][c1]));
    let determinant = poly_add(
        &poly_sub(&poly_mul(&b[0][0], &cofactor(1, 2)), &poly_mul(&b[0][1], &cofactor(0, 2))),
        &poly_mul(&b[0][2], &cofactor(0, 1)),
    );
    let mut solutions = Vec::new();
    for z in real_roots(&determinant) {
        let b_at = Matrix3::from_fn(|r, c| poly_eval(&b[r][c], z));
        // the null vector of B(z) is (x, y, 1) up to scale: the cross product of its two most
        // independent rows; a vanishing third component (COLMAP's bound) is no solution
        let b_rows: [Vector3<f64>; 3] = std::array::from_fn(|r| b_at.row(r).transpose());
        let n = [b_rows[0].cross(&b_rows[1]), b_rows[1].cross(&b_rows[2]), b_rows[2].cross(&b_rows[0])]
            .into_iter()
            .max_by(|p, q| p.norm().total_cmp(&q.norm()))
            .expect("three candidates");
        let norm = n.norm();
        if !(norm > 0.0 && n.z.abs() > 1e-10 * norm) {
            continue;
        }
        let Some((x, y, z)) = refine(&constraints, n.x / n.z, n.y / n.z, z) else {
            continue;
        };
        let e = e_x * x + e_y * y + e_z * z + e_w;
        if let Some(e) = unit(&e) {
            solutions.push(e);
        }
    }
    Some(solutions)
}

/// The fundamental matrices of seven correspondences (as COLMAP's
/// `FundamentalMatrixSevenPointEstimator`, with the points normalised as in the eight-point
/// algorithm): up to three, each of unit Frobenius norm. The null space of the epipolar
/// constraints has two dimensions, `F_a` and `F_b`; `det(F_a + lambda (F_b - F_a)) = 0` is a cubic
/// in lambda. No solution when the points are degenerate or not finite.
pub fn fundamental_seven_point(x1: &[[f64; 2]; 7], x2: &[[f64; 2]; 7]) -> Vec<Matrix3<f64>> {
    seven_point(x1, x2).unwrap_or_default()
}

fn seven_point(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Option<Vec<Matrix3<f64>>> {
    if !(finite(x1) && finite(x2)) {
        return None;
    }
    let (n1, n2) = (normalise(x1)?, normalise(x2)?);
    let rows: Vec<[f64; 9]> = n1.points.iter().zip(&n2.points).map(|(&a, &b)| epipolar_row(a, b)).collect();
    let basis = null_space(&rows, 2)?;
    let (f_a, f_b) = (as_matrix(&basis[0]), as_matrix(&basis[1]));
    let d = f_b - f_a;
    // det(F_a + lambda D) is a cubic in lambda, read from its values at 0, 1, -1 and 2
    let (p0, p1) = (f_a.determinant(), f_b.determinant());
    let (pm, p2) = ((f_a - d).determinant(), (f_a + d * 2.0).determinant());
    let c2 = (p1 + pm) / 2.0 - p0;
    let odd = (p1 - pm) / 2.0;
    let c3 = ((p2 - p0 - 4.0 * c2) / 2.0 - odd) / 3.0;
    let c1 = odd - c3;
    let solutions = real_roots(&[p0, c1, c2, c3])
        .into_iter()
        .filter_map(|lambda| unit(&(n2.matrix.transpose() * (f_a + d * lambda) * n1.matrix)))
        .collect();
    Some(solutions)
}

/// The fundamental matrix of eight or more correspondences (Hartley's normalised eight-point
/// algorithm, as COLMAP's `FundamentalMatrixEightPointEstimator`): the points of each photo
/// centred and scaled to a root mean square distance of `sqrt 2`, the null vector of the
/// epipolar constraints (the least-squares one when there are more than eight), rank two
/// enforced by zeroing the smallest singular value, the normalisation undone; of unit Frobenius
/// norm. `None` with fewer than eight points, with lengths that differ, or when the points are
/// degenerate or not finite.
pub fn fundamental_eight_point(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Option<Matrix3<f64>> {
    if x1.len() < 8 || x1.len() != x2.len() || !(finite(x1) && finite(x2)) {
        return None;
    }
    let (n1, n2) = (normalise(x1)?, normalise(x2)?);
    let rows: Vec<[f64; 9]> = n1.points.iter().zip(&n2.points).map(|(&a, &b)| epipolar_row(a, b)).collect();
    let f = rank_two(&as_matrix(&null_space(&rows, 1)?[0]));
    unit(&(n2.matrix.transpose() * f * n1.matrix))
}

/// The homography of four correspondences: `homography_dlt` on the minimal sample.
pub fn homography_four_point(x1: &[[f64; 2]; 4], x2: &[[f64; 2]; 4]) -> Option<Matrix3<f64>> {
    homography_dlt(x1, x2)
}

/// The homography `x2 ~ H x1` of four or more correspondences by the direct linear transform (as
/// COLMAP's `HomographyMatrixEstimator`): the points of each photo normalised as in the
/// eight-point algorithm, the null vector of the two linear constraints of each correspondence
/// (the least-squares one when there are more than four), the normalisation undone; of unit
/// Frobenius norm. `None` with fewer than four points, with lengths that differ, or when the
/// points are degenerate (three of four on a line) or not finite.
pub fn homography_dlt(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Option<Matrix3<f64>> {
    if x1.len() < 4 || x1.len() != x2.len() || !(finite(x1) && finite(x2)) {
        return None;
    }
    let (n1, n2) = (normalise(x1)?, normalise(x2)?);
    let rows: Vec<[f64; 9]> =
        n1.points.iter().zip(&n2.points).flat_map(|(&a, &b)| homography_rows(a, b)).collect();
    let h = as_matrix(&null_space(&rows, 1)?[0]);
    unit(&(n2.inverse * h * n1.matrix))
}

/// COLMAP's `ComputeSquaredSampsonError`: the square of `b^T F a` over the squared norm of its
/// gradient (the first two components of `F a` and of `F^T b`), the first-order approximation of
/// the squared geometric distance of the correspondence from the epipolar constraint. Zero over
/// zero (both points on the epipoles) is zero, not NaN.
pub fn sampson_sq(f: &Matrix3<f64>, a: [f64; 2], b: [f64; 2]) -> f64 {
    let fa = f * Vector3::new(a[0], a[1], 1.0);
    let ftb = f.transpose() * Vector3::new(b[0], b[1], 1.0);
    let numerator = fa.x * b[0] + fa.y * b[1] + fa.z;
    let denominator = fa.x * fa.x + fa.y * fa.y + ftb.x * ftb.x + ftb.y * ftb.y;
    if denominator == 0.0 {
        if numerator == 0.0 {
            0.0
        } else {
            f64::INFINITY
        }
    } else {
        numerator * numerator / denominator
    }
}

/// COLMAP's homography residual: the squared distance between `b` and the point `h` maps `a` to,
/// in the second photo only; infinite when `h` sends `a` to infinity.
pub fn transfer_sq(h: &Matrix3<f64>, a: [f64; 2], b: [f64; 2]) -> f64 {
    let p = h * Vector3::new(a[0], a[1], 1.0);
    if p.z == 0.0 {
        return f64::INFINITY;
    }
    let (dx, dy) = (b[0] - p.x / p.z, b[1] - p.y / p.z);
    dx * dx + dy * dy
}

/// The pose of an essential matrix: of the four of its decomposition (COLMAP's
/// `DecomposeEssentialMatrix`, `R = U W V^T` or `U W^T V^T` with the determinants of U and V made
/// positive, `t` plus or minus the last column of U), the one with the most correspondences in
/// front of both cameras once triangulated, with that count (`PoseFromEssentialMatrix`,
/// `CheckCheirality`: a point counts when its depth in both cameras is above machine epsilon and
/// below a thousand baselines; a later pose with as many points replaces an earlier one). `x1`,
/// `x2` are normalised coordinates; the pose is cam2_from_cam1 with `t` of unit norm. `None` when
/// no point is in front of both cameras under any pose, when `e` has rank below two (the zero
/// matrix among them), when the lengths differ, or when `e` or the points are not finite.
pub fn pose_from_essential(
    e: &Matrix3<f64>,
    x1: &[[f64; 2]],
    x2: &[[f64; 2]],
) -> Option<(Matrix3<f64>, Vector3<f64>, usize)> {
    if x1.len() != x2.len() || !(finite(x1) && finite(x2)) || !e.iter().all(|v| v.is_finite()) {
        return None;
    }
    let (r1, r2, t) = decompose_essential(e)?;
    let mut best: Option<(Matrix3<f64>, Vector3<f64>, usize)> = None;
    for (r, t) in [(r1, t), (r2, t), (r1, -t), (r2, -t)] {
        let count = x1.iter().zip(x2).filter(|(&a, &b)| in_front(&r, &t, a, b)).count();
        if best.is_none_or(|(_, _, n)| count >= n) {
            best = Some((r, t, count));
        }
    }
    best.filter(|(_, _, count)| *count > 0)
}

fn decompose_essential(e: &Matrix3<f64>) -> Option<(Matrix3<f64>, Matrix3<f64>, Vector3<f64>)> {
    let svd = jacobi_svd(&dynamic(e));
    let s = &svd.s;
    // a matrix of rank below two (the zero matrix among them) has no pose
    if !(s[0] > 0.0 && s[1] > RANK_TOLERANCE * s[0]) {
        return None;
    }
    // U and V in SO(3): the first two columns from the decomposition, the third their cross
    // product (COLMAP makes the determinants positive by negating U or V as a whole)
    let column = |m: &DMatrix<f64>, k: usize, scale: f64| Vector3::new(m[(0, k)], m[(1, k)], m[(2, k)]) / scale;
    let (u1, u2) = (column(&svd.av, 0, s[0]), column(&svd.av, 1, s[1]));
    let (v1, v2) = (column(&svd.v, 0, 1.0), column(&svd.v, 1, 1.0));
    let t = u1.cross(&u2).normalize();
    let u = Matrix3::from_columns(&[u1, u2, t]);
    let v_t = Matrix3::from_columns(&[v1, v2, v1.cross(&v2)]).transpose();
    let w = Matrix3::new(0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0);
    Some((u * w * v_t, u * w.transpose() * v_t, t))
}

/// COLMAP's `CheckCheirality` for one correspondence: triangulated, the point has a depth above
/// machine epsilon and below a thousand baselines in both cameras.
fn in_front(r: &Matrix3<f64>, t: &Vector3<f64>, a: [f64; 2], b: [f64; 2]) -> bool {
    let max_depth = 1000.0 * t.norm();
    let Some(p) = triangulate(r, t, a, b) else {
        return false;
    };
    let (depth1, depth2) = (p.z, (r * p + t).z);
    depth1 > f64::EPSILON && depth1 < max_depth && depth2 > f64::EPSILON && depth2 < max_depth
}

/// COLMAP's `TriangulatePoint`: the point of the frame of the first camera where the rays of a
/// correspondence meet, the homogeneous least-squares solution of the four linear constraints
/// (`x P[2] - P[0]` and `y P[2] - P[1]` of `P1 = [I | 0]` and `P2 = [R | t]`); `None` at
/// infinity.
fn triangulate(r: &Matrix3<f64>, t: &Vector3<f64>, a: [f64; 2], b: [f64; 2]) -> Option<Vector3<f64>> {
    let rows: [[f64; 4]; 4] = [
        [-1.0, 0.0, a[0], 0.0],
        [0.0, -1.0, a[1], 0.0],
        [b[0] * r[(2, 0)] - r[(0, 0)], b[0] * r[(2, 1)] - r[(0, 1)], b[0] * r[(2, 2)] - r[(0, 2)], b[0] * t.z - t.x],
        [b[1] * r[(2, 0)] - r[(1, 0)], b[1] * r[(2, 1)] - r[(1, 1)], b[1] * r[(2, 2)] - r[(1, 2)], b[1] * t.z - t.y],
    ];
    let svd = jacobi_svd(&DMatrix::from_fn(4, 4, |i, j| rows[i][j]));
    let x = svd.v.column(3);
    (x[3] != 0.0).then(|| Vector3::new(x[0] / x[3], x[1] / x[3], x[2] / x[3]))
}

fn finite(points: &[[f64; 2]]) -> bool {
    points.iter().all(|p| p[0].is_finite() && p[1].is_finite())
}

/// The row of the epipolar constraint `b^T F a = 0` over the row-major entries of F.
fn epipolar_row(a: [f64; 2], b: [f64; 2]) -> [f64; 9] {
    [b[0] * a[0], b[0] * a[1], b[0], b[1] * a[0], b[1] * a[1], b[1], a[0], a[1], 1.0]
}

/// The two rows of the direct linear transform for `b ~ H a` over the row-major entries of H.
fn homography_rows(a: [f64; 2], b: [f64; 2]) -> [[f64; 9]; 2] {
    [
        [-a[0], -a[1], -1.0, 0.0, 0.0, 0.0, b[0] * a[0], b[0] * a[1], b[0]],
        [0.0, 0.0, 0.0, -a[0], -a[1], -1.0, b[1] * a[0], b[1] * a[1], b[1]],
    ]
}

/// The 3 x 3 matrix whose row-major entries are the nine components of `v`.
fn as_matrix(v: &SVector<f64, 9>) -> Matrix3<f64> {
    Matrix3::from_row_slice(v.as_slice())
}

/// A 3 x 3 matrix as a dynamic one, for the decomposition.
fn dynamic(m: &Matrix3<f64>) -> DMatrix<f64> {
    DMatrix::from_fn(3, 3, |i, j| m[(i, j)])
}

/// A singular value decomposition by one-sided Jacobi rotations (Hestenes): `v` is orthogonal,
/// the columns of `av = A v` are orthogonal and their norms are the singular values `s`, in
/// descending order; the left singular vectors are the columns of `av` divided by them, where
/// they are not zero.
struct Svd {
    v: DMatrix<f64>,
    av: DMatrix<f64>,
    s: Vec<f64>,
}

/// The decomposition of a square or tall matrix: pairs of columns of `A v` are rotated until
/// every two are orthogonal (to a tolerance of roundoff times the root of the number of rows, as
/// LAPACK's), the same rotations accumulated in `v`. Accurate to roundoff on every matrix; see
/// the module doc for why nalgebra's is not used.
fn jacobi_svd(a: &DMatrix<f64>) -> Svd {
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

/// The matrix scaled to a Frobenius norm of 1; `None` when it is zero or not finite.
fn unit(m: &Matrix3<f64>) -> Option<Matrix3<f64>> {
    let norm = m.norm();
    (norm > 0.0 && norm.is_finite()).then(|| m / norm)
}

/// A basis of the null space of the linear system `A f = 0` whose rows are given, of the dimension
/// `dim` a configuration in general position leaves: the right singular vectors of the `dim`
/// smallest singular values, the smallest first. `None` when the next singular value is below
/// `RANK_TOLERANCE` times the largest (A has lost rank: the configuration is degenerate) or when
/// the decomposition fails.
fn null_space(rows: &[[f64; 9]], dim: usize) -> Option<Vec<SVector<f64, 9>>> {
    // at least nine rows (the rest zero), so that every right singular vector is computed; a
    // taller system is reduced to the R of its QR decomposition, with the same right singular
    // vectors
    let n = rows.len().max(9);
    let mut a = DMatrix::from_fn(n, 9, |i, j| rows.get(i).map_or(0.0, |row| row[j]));
    if n > 9 {
        a = QR::new(a).r();
    }
    let svd = jacobi_svd(&a);
    if !(svd.s[0] > 0.0 && svd.s[8 - dim] > RANK_TOLERANCE * svd.s[0]) {
        return None;
    }
    Some((9 - dim..9).rev().map(|k| SVector::from_iterator(svd.v.column(k).iter().copied())).collect())
}

/// The matrix of rank two closest to `f`: its smallest singular value zeroed.
fn rank_two(f: &Matrix3<f64>) -> Matrix3<f64> {
    let svd = jacobi_svd(&dynamic(f));
    Matrix3::from_fn(|i, j| svd.av[(i, 0)] * svd.v[(j, 0)] + svd.av[(i, 1)] * svd.v[(j, 1)])
}

/// Hartley's normalisation of a set of points: the similarity `matrix` that moves their centroid
/// to the origin and makes their root mean square distance from it `sqrt 2`, the `points` it
/// gives and its `inverse`.
struct Normalisation {
    points: Vec<[f64; 2]>,
    matrix: Matrix3<f64>,
    inverse: Matrix3<f64>,
}

/// `None` when the points coincide (the scale is not defined).
fn normalise(points: &[[f64; 2]]) -> Option<Normalisation> {
    let n = points.len() as f64;
    let (sx, sy) = points.iter().fold((0.0, 0.0), |(x, y), p| (x + p[0], y + p[1]));
    let (cx, cy) = (sx / n, sy / n);
    let rms = (points.iter().map(|p| (p[0] - cx).powi(2) + (p[1] - cy).powi(2)).sum::<f64>() / n).sqrt();
    if !(rms > 0.0 && rms.is_finite()) {
        return None;
    }
    let s = 2f64.sqrt() / rms;
    Some(Normalisation {
        points: points.iter().map(|p| [(p[0] - cx) * s, (p[1] - cy) * s]).collect(),
        matrix: Matrix3::new(s, 0.0, -s * cx, 0.0, s, -s * cy, 0.0, 0.0, 1.0),
        inverse: Matrix3::new(1.0 / s, 0.0, cx, 0.0, 1.0 / s, cy, 0.0, 0.0, 1.0),
    })
}

/// Exponents of x, y and z of the 20 monomials of degree up to three, in the order of the
/// columns of Nister's 10 x 20 matrix: first the ten the elimination removes, then
/// `x z^2, x z, x, y z^2, y z, y, z^3, z^2, z, 1`, whose coefficients are read after it.
const MONOMIALS: [[usize; 3]; 20] = [
    [3, 0, 0],
    [0, 3, 0],
    [2, 1, 0],
    [1, 2, 0],
    [2, 0, 1],
    [2, 0, 0],
    [0, 2, 1],
    [0, 2, 0],
    [1, 1, 1],
    [1, 1, 0],
    [1, 0, 2],
    [1, 0, 1],
    [1, 0, 0],
    [0, 1, 2],
    [0, 1, 1],
    [0, 1, 0],
    [0, 0, 3],
    [0, 0, 2],
    [0, 0, 1],
    [0, 0, 0],
];

/// The column of the monomial `x^i y^j z^k` (`i + j + k` at most three), `usize::MAX` otherwise.
const COLUMN: [[[usize; 4]; 4]; 4] = column_table();

const fn column_table() -> [[[usize; 4]; 4]; 4] {
    let mut table = [[[usize::MAX; 4]; 4]; 4];
    let mut m = 0;
    while m < MONOMIALS.len() {
        let [i, j, k] = MONOMIALS[m];
        table[i][j][k] = m;
        m += 1;
    }
    table
}

/// A polynomial in x, y and z of degree up to three: its coefficients over `MONOMIALS`.
#[derive(Clone, Copy)]
struct Poly3([f64; 20]);

impl Poly3 {
    /// `a x + b y + c z + d`.
    fn linear(a: f64, b: f64, c: f64, d: f64) -> Poly3 {
        let mut p = [0.0; 20];
        p[12] = a;
        p[15] = b;
        p[18] = c;
        p[19] = d;
        Poly3(p)
    }

    fn scaled(self, s: f64) -> Poly3 {
        Poly3(self.0.map(|c| c * s))
    }
}

impl Add for Poly3 {
    type Output = Poly3;

    fn add(self, rhs: Poly3) -> Poly3 {
        Poly3(std::array::from_fn(|i| self.0[i] + rhs.0[i]))
    }
}

impl Sub for Poly3 {
    type Output = Poly3;

    fn sub(self, rhs: Poly3) -> Poly3 {
        Poly3(std::array::from_fn(|i| self.0[i] - rhs.0[i]))
    }
}

impl Mul for Poly3 {
    type Output = Poly3;

    /// The product, of degree up to three by the construction of the constraints (a higher one
    /// has no column).
    fn mul(self, rhs: Poly3) -> Poly3 {
        let mut out = [0.0; 20];
        for (i, &a) in self.0.iter().enumerate() {
            if a == 0.0 {
                continue;
            }
            let [p, q, r] = MONOMIALS[i];
            for (j, &b) in rhs.0.iter().enumerate() {
                if b == 0.0 {
                    continue;
                }
                let [s, t, u] = MONOMIALS[j];
                out[COLUMN[p + s][q + t][r + u]] += a * b;
            }
        }
        Poly3(out)
    }
}

/// Nister's 10 x 20 matrix: the coefficients, over `MONOMIALS`, of the ten cubic constraints on
/// `E = x X + y Y + z Z + W` (`basis` = X, Y, Z, W): `det E = 0` in the first row, the nine
/// entries of `2 E E^T E - tr(E E^T) E = 0` in the others.
fn five_point_constraints(basis: &[Matrix3<f64>; 4]) -> SMatrix<f64, 10, 20> {
    let e: [[Poly3; 3]; 3] = std::array::from_fn(|r| {
        std::array::from_fn(|c| Poly3::linear(basis[0][(r, c)], basis[1][(r, c)], basis[2][(r, c)], basis[3][(r, c)]))
    });
    let det = e[0][0] * (e[1][1] * e[2][2] - e[1][2] * e[2][1]) - e[0][1] * (e[1][0] * e[2][2] - e[1][2] * e[2][0])
        + e[0][2] * (e[1][0] * e[2][1] - e[1][1] * e[2][0]);
    let eet: [[Poly3; 3]; 3] =
        std::array::from_fn(|i| std::array::from_fn(|j| e[i][0] * e[j][0] + e[i][1] * e[j][1] + e[i][2] * e[j][2]));
    let trace = eet[0][0] + eet[1][1] + eet[2][2];
    let mut a = SMatrix::<f64, 10, 20>::zeros();
    let mut set_row = |row: usize, p: Poly3| {
        for (column, coefficient) in p.0.into_iter().enumerate() {
            a[(row, column)] = coefficient;
        }
    };
    set_row(0, det);
    for i in 0..3 {
        for j in 0..3 {
            let m = (eet[i][0] * e[0][j] + eet[i][1] * e[1][j] + eet[i][2] * e[2][j]).scaled(2.0) - trace * e[i][j];
            set_row(1 + 3 * i + j, m);
        }
    }
    a
}

/// The monomials of `MONOMIALS` at (x, y, z) and their derivatives in x, y and z.
fn monomials_at(x: f64, y: f64, z: f64) -> ([f64; 20], [[f64; 20]; 3]) {
    let power = |base: f64, exponent: usize| base.powi(exponent as i32);
    let derivative = |base: f64, exponent: usize| match exponent {
        0 => 0.0,
        _ => exponent as f64 * power(base, exponent - 1),
    };
    let mut m = [0.0; 20];
    let mut d = [[0.0; 20]; 3];
    for (k, [i, j, l]) in MONOMIALS.into_iter().enumerate() {
        let (xi, yj, zl) = (power(x, i), power(y, j), power(z, l));
        m[k] = xi * yj * zl;
        d[0][k] = derivative(x, i) * yj * zl;
        d[1][k] = xi * derivative(y, j) * zl;
        d[2][k] = xi * yj * derivative(z, l);
    }
    (m, d)
}

/// Gauss-Newton on the ten constraints from (x, y, z): a solution computed through the
/// elimination and the roots carries their roundoff (1e-9 of E when the elimination is badly
/// conditioned); a few steps on the constraints themselves, which are exact to roundoff, bring it
/// to roundoff. Stops when a step does not lower the residual; `None` when the residual left is
/// not that of a solution (`CONVERGED`).
fn refine(constraints: &SMatrix<f64, 10, 20>, mut x: f64, mut y: f64, mut z: f64) -> Option<(f64, f64, f64)> {
    let evaluate = |x: f64, y: f64, z: f64| {
        let (m, d) = monomials_at(x, y, z);
        let monomials = SVector::<f64, 20>::from(m);
        let residual = constraints * monomials;
        let jacobian =
            SMatrix::<f64, 10, 3>::from_fn(|row, col| (0..20).map(|k| constraints[(row, k)] * d[col][k]).sum());
        (residual, jacobian, monomials.norm())
    };
    let (mut residual, mut jacobian, mut scale) = evaluate(x, y, z);
    for _ in 0..5 {
        let Some(inverse) = (jacobian.transpose() * jacobian).try_inverse() else {
            break;
        };
        let step = inverse * (jacobian.transpose() * residual);
        let (nx, ny, nz) = (x - step[0], y - step[1], z - step[2]);
        let (next_residual, next_jacobian, next_scale) = evaluate(nx, ny, nz);
        if !(next_residual.norm() < residual.norm()) {
            break;
        }
        (x, y, z, residual, jacobian, scale) = (nx, ny, nz, next_residual, next_jacobian, next_scale);
    }
    (residual.norm() <= CONVERGED * constraints.norm() * scale).then_some((x, y, z))
}

/// The polynomials in one variable are slices of coefficients in ascending powers.
fn poly_mul(a: &[f64], b: &[f64]) -> Vec<f64> {
    let mut out = vec![0.0; a.len() + b.len() - 1];
    for (i, x) in a.iter().enumerate() {
        for (j, y) in b.iter().enumerate() {
            out[i + j] += x * y;
        }
    }
    out
}

fn poly_add(a: &[f64], b: &[f64]) -> Vec<f64> {
    (0..a.len().max(b.len())).map(|k| a.get(k).unwrap_or(&0.0) + b.get(k).unwrap_or(&0.0)).collect()
}

fn poly_sub(a: &[f64], b: &[f64]) -> Vec<f64> {
    (0..a.len().max(b.len())).map(|k| a.get(k).unwrap_or(&0.0) - b.get(k).unwrap_or(&0.0)).collect()
}

/// Horner's evaluation.
fn poly_eval(c: &[f64], z: f64) -> f64 {
    c.iter().rev().fold(0.0, |p, &coefficient| p * z + coefficient)
}

/// The real roots of a polynomial, in ascending order, to the last bit of the evaluation: between
/// two consecutive critical points (the real roots of the derivative, found the same way) the
/// polynomial is monotonic and holds at most one root, bracketed by a change of sign and bisected;
/// the outermost intervals end at Cauchy's bound of the roots; a critical point at which the
/// polynomial vanishes to roundoff is a multiple root. A leading coefficient below roundoff of
/// the largest lowers the degree (that root has gone to infinity); no roots when the coefficients
/// are all zero or not finite. COLMAP takes the eigenvalues of the companion matrix instead: on
/// the five-point polynomial, whose roots spread over orders of magnitude when a solution lies
/// towards infinity, those came out wrong by whole units (nalgebra's Schur form does no
/// balancing), so the root that was the true essential matrix was lost.
fn real_roots(coefficients: &[f64]) -> Vec<f64> {
    let largest = coefficients.iter().fold(0.0f64, |m, c| m.max(c.abs()));
    if !(largest > 0.0) || !coefficients.iter().all(|c| c.is_finite()) {
        return Vec::new();
    }
    let mut degree = coefficients.len() - 1;
    while degree > 0 && coefficients[degree].abs() <= 1e-14 * largest {
        degree -= 1;
    }
    let mut roots = roots_between_critical_points(&coefficients[..=degree]);
    roots.dedup_by(|a, b| (*a - *b).abs() <= 1e-12 * a.abs().max(b.abs()).max(1.0));
    roots
}

/// The real roots of `c` (ascending coefficients, the leading one not zero), in ascending order.
fn roots_between_critical_points(c: &[f64]) -> Vec<f64> {
    let n = c.len() - 1;
    if n == 0 {
        return Vec::new();
    }
    if n == 1 {
        return vec![-c[0] / c[1]];
    }
    let derivative: Vec<f64> = (1..=n).map(|k| k as f64 * c[k]).collect();
    let bound = 1.0 + (0..n).map(|k| (c[k] / c[n]).abs()).fold(0.0, f64::max);
    let mut points = vec![-bound];
    points.extend(roots_between_critical_points(&derivative).into_iter().filter(|z| z.abs() < bound));
    points.push(bound);
    let values: Vec<f64> = points.iter().map(|&z| poly_eval(c, z)).collect();
    // a critical point is a root when the polynomial vanishes there to the roundoff of Horner's
    // evaluation (a bound of the error of the evaluation, generous by a small factor)
    let vanishes = |k: usize| {
        let z = points[k].abs();
        let size: f64 = c.iter().enumerate().map(|(power, coefficient)| coefficient.abs() * z.powi(power as i32)).sum();
        k > 0 && k + 1 < points.len() && values[k].abs() <= 64.0 * f64::EPSILON * size
    };
    let mut roots = Vec::new();
    for k in 0..points.len() - 1 {
        if vanishes(k) {
            roots.push(points[k]);
        }
        if !vanishes(k) && !vanishes(k + 1) && (values[k] < 0.0) != (values[k + 1] < 0.0) {
            roots.push(bisect(c, points[k], points[k + 1], values[k]));
        }
    }
    roots
}

/// The root of `c` between `lo` and `hi`, where the polynomial changes sign (`p_lo` is its value
/// at `lo`), by bisection to the last bit (or to 200 halvings, for a root at zero).
fn bisect(c: &[f64], mut lo: f64, mut hi: f64, mut p_lo: f64) -> f64 {
    for _ in 0..200 {
        let mid = 0.5 * (lo + hi);
        if mid <= lo || mid >= hi {
            break;
        }
        let p_mid = poly_eval(c, mid);
        if p_mid == 0.0 {
            return mid;
        }
        if (p_mid < 0.0) == (p_lo < 0.0) {
            (lo, p_lo) = (mid, p_mid);
        } else {
            hi = mid;
        }
    }
    0.5 * (lo + hi)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Knuth's MMIX generator (as the tests of the crate), for the matrices of these checks.
    struct Lcg(u64);

    impl Lcg {
        /// A value in [-1, 1).
        fn symmetric(&mut self) -> f64 {
            self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            2.0 * ((self.0 >> 11) as f64 / (1u64 << 53) as f64) - 1.0
        }

        fn matrix(&mut self) -> Matrix3<f64> {
            Matrix3::from_fn(|_, _| self.symmetric())
        }
    }

    /// The value of a polynomial over `MONOMIALS` at (x, y, z).
    fn value(p: &[f64; 20], x: f64, y: f64, z: f64) -> f64 {
        p.iter()
            .zip(MONOMIALS)
            .map(|(c, [i, j, k])| c * x.powi(i as i32) * y.powi(j as i32) * z.powi(k as i32))
            .sum()
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

    #[test]
    fn the_constraints_of_the_five_point_solver_are_those_of_the_essential_matrix() {
        let mut random = Lcg(11);
        for _ in 0..20 {
            let basis: [Matrix3<f64>; 4] = std::array::from_fn(|_| random.matrix());
            let a = five_point_constraints(&basis);
            let (x, y, z) = (random.symmetric(), random.symmetric(), random.symmetric());
            let e = basis[0] * x + basis[1] * y + basis[2] * z + basis[3];
            let m = 2.0 * e * e.transpose() * e - (e * e.transpose()).trace() * e;
            let mut want = vec![e.determinant()];
            want.extend((0..3).flat_map(|i| (0..3).map(move |j| m[(i, j)])));
            for (row, want) in want.into_iter().enumerate() {
                let coefficients: [f64; 20] = std::array::from_fn(|column| a[(row, column)]);
                let got = value(&coefficients, x, y, z);
                assert!((got - want).abs() <= 1e-12 * want.abs().max(1.0), "row {row}: {got} against {want}");
            }
        }
    }

    #[test]
    fn real_roots_finds_the_roots_a_polynomial_was_built_from() {
        // five real roots and two complex pairs: (z^2 + 1) (z^2 - 2 z + 5) and the real factors
        let wanted = [-2.0, -0.5, 0.3, 1.0, 4.0];
        let mut p = vec![1.0];
        for root in wanted {
            p = poly_mul(&p, &[-root, 1.0]);
        }
        p = poly_mul(&p, &[1.0, 0.0, 1.0]);
        p = poly_mul(&p, &[5.0, -2.0, 1.0]);
        let mut roots = real_roots(&p);
        roots.sort_by(f64::total_cmp);
        assert_eq!(roots.len(), wanted.len(), "{roots:?}");
        for (got, want) in roots.iter().zip(wanted) {
            assert!((got - want).abs() < 1e-12, "{got} against {want}");
        }
        // roots spread over orders of magnitude, as the five-point polynomial has them when a
        // solution lies towards infinity (the case the companion matrix got wrong)
        let wanted = [-0.004, 0.03, 0.95, 1.2, 860.0];
        let mut p = vec![1.0];
        for root in wanted {
            p = poly_mul(&p, &[-root, 1.0]);
        }
        p = poly_mul(&p, &[1.0, 0.0, 1.0]);
        p = poly_mul(&p, &[5.0, -2.0, 1.0]);
        p = poly_mul(&p, &[2.0, 1.0, 1.0]);
        let roots = real_roots(&p);
        assert_eq!(roots.len(), wanted.len(), "{roots:?}");
        for (got, want) in roots.iter().zip(wanted) {
            assert!((got - want).abs() < 1e-12 * want.abs(), "{got} against {want}");
        }
        // z^3 (zero, once), (z - 1)^2 (one, once), z^2 + 1 (none), 2 - z, a constant, zero, not a
        // number; (z - 3)(z + 7) = z^2 + 4 z - 21
        assert_eq!(real_roots(&[0.0, 0.0, 0.0, 1.0]), vec![0.0]);
        assert_eq!(real_roots(&[1.0, -2.0, 1.0]), vec![1.0]);
        assert!(real_roots(&[1.0, 0.0, 1.0]).is_empty());
        assert_eq!(real_roots(&[2.0, -1.0]), vec![2.0]);
        assert!(real_roots(&[3.0]).is_empty());
        assert!(real_roots(&[0.0, 0.0]).is_empty());
        assert!(real_roots(&[f64::NAN, 1.0]).is_empty());
        let roots = real_roots(&[-21.0, 4.0, 1.0]);
        assert_eq!(roots.len(), 2);
        assert!((roots[0] + 7.0).abs() < 1e-14 && (roots[1] - 3.0).abs() < 1e-14, "{roots:?}");
    }
}
