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
//! The null vectors and the decompositions come from the singular value decomposition of
//! `linalg` (one-sided Jacobi rotations, accurate to roundoff on every matrix; the module doc
//! says why nalgebra's is not used), on fixed-size matrices on the stack. A tall least-squares
//! system is reduced first to the R of its QR decomposition (Givens rotations, one row at a time,
//! so that the rows are never stored), which has the same right singular vectors. The minimal
//! solvers allocate nothing on the heap for their linear algebra: they run once per RANSAC
//! hypothesis.

use std::ops::{Add, Mul, Sub};

use nalgebra::{Matrix3, SMatrix, SVector, Vector3, LU};

use super::linalg::{columns, jacobi_svd, singular_values_of, unit, Similarity};

/// A singular value below this fraction of the largest counts as zero: the system has lost rank,
/// the configuration is degenerate. Exact degeneracy computes to about 1e-16 of the largest; a
/// configuration this close to it gives no usable model anyway.
const RANK_TOLERANCE: f64 = 1e-10;

/// A solution of the five-point constraints is accepted when its residual is below this fraction
/// of the bound on it (the norms of the constraint matrix and of the monomials): a converged one
/// is at roundoff, 1e-15; a root the back-substitution could not place leaves 1e-4 or more.
const CONVERGED: f64 = 1e-10;

/// Two essential matrices of unit norm closer than this, up to sign, are the same solution: two
/// roots of the polynomial that the refinement brought onto one solution (each is then at
/// roundoff, 1e-15, from it; distinct solutions of five points are farther apart by orders of
/// magnitude).
const DUPLICATE_TOLERANCE: f64 = 1e-8;

/// The essential matrices of five correspondences in normalised coordinates (Nister 2004, as
/// COLMAP's `EssentialMatrixFivePointEstimator`): up to ten, each of unit Frobenius norm. The
/// null space of the epipolar constraints gives `E = x X + y Y + z Z + W`; the ten cubic
/// constraints on E (`det E = 0`, `2 E E^T E - tr(E E^T) E = 0`) are reduced by Gauss-Jordan
/// elimination to a 3 x 3 matrix of polynomials in z whose determinant, of degree ten, vanishes
/// at the solutions; x and y follow from its null vector. No solution when the five points are
/// degenerate (fewer than five independent constraints) or not finite. Two roots the refinement
/// brings onto one solution give it once (`DUPLICATE_TOLERANCE`).
pub fn essential_five_point(x1: &[[f64; 2]; 5], x2: &[[f64; 2]; 5]) -> Vec<Matrix3<f64>> {
    five_point(x1, x2).unwrap_or_default()
}

/// The essential matrices of five or more correspondences in normalised coordinates: the
/// five-point solver on the four right singular vectors of the smallest singular values of all
/// the epipolar constraints (COLMAP's estimator with more than five points, the local step of its
/// LO-RANSAC). With five points it is `essential_five_point`. No solution with fewer than five
/// points, with lengths that differ, or when the points are degenerate or not finite.
pub fn essential_n_point(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Vec<Matrix3<f64>> {
    if x1.len() < 5 || x1.len() != x2.len() {
        return Vec::new();
    }
    five_point(x1, x2).unwrap_or_default()
}

fn five_point(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Option<Vec<Matrix3<f64>>> {
    if !(finite(x1) && finite(x2)) {
        return None;
    }
    // E = x X + y Y + z Z + W over the null space of the epipolar constraints, W the vector of
    // the smallest singular value (COLMAP's order)
    let basis = null_space::<4>(x1.iter().zip(x2).map(|(&a, &b)| epipolar_row(a, b)))?;
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
    drop_duplicates(&mut solutions);
    Some(solutions)
}

/// Of matrices of unit norm, the later one of two closer than `DUPLICATE_TOLERANCE` up to sign is
/// dropped.
fn drop_duplicates(solutions: &mut Vec<Matrix3<f64>>) {
    let mut kept: Vec<Matrix3<f64>> = Vec::with_capacity(solutions.len());
    for e in solutions.drain(..) {
        let same = |k: &Matrix3<f64>| (k - e).norm().min((k + e).norm()) <= DUPLICATE_TOLERANCE;
        if !kept.iter().any(same) {
            kept.push(e);
        }
    }
    *solutions = kept;
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
    let (n1, n2) = (Similarity::of(x1)?, Similarity::of(x2)?);
    let basis = null_space::<2>(x1.iter().zip(x2).map(|(&a, &b)| epipolar_row(n1.apply(a), n2.apply(b))))?;
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
        .filter_map(|lambda| unit(&(n2.matrix().transpose() * (f_a + d * lambda) * n1.matrix())))
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
    let (n1, n2) = (Similarity::of(x1)?, Similarity::of(x2)?);
    let [null] = null_space::<1>(x1.iter().zip(x2).map(|(&a, &b)| epipolar_row(n1.apply(a), n2.apply(b))))?;
    let f = rank_two(&as_matrix(&null));
    unit(&(n2.matrix().transpose() * f * n1.matrix()))
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
    let (n1, n2) = (Similarity::of(x1)?, Similarity::of(x2)?);
    let [null] = null_space::<1>(x1.iter().zip(x2).flat_map(|(&a, &b)| homography_rows(n1.apply(a), n2.apply(b))))?;
    let h = as_matrix(&null);
    unit(&(n2.inverse() * h * n1.matrix()))
}

/// COLMAP's `ComputeSquaredSampsonError`: the square of `b^T F a` over the squared norm of its
/// gradient (the first two components of `F a` and of `F^T b`), the first-order approximation of
/// the squared geometric distance of the correspondence from the epipolar constraint. Infinite,
/// never zero or NaN, when the gradient vanishes (both points on the epipoles, the zero matrix) or
/// is not finite: a degenerate correspondence is never an inlier.
pub fn sampson_sq(f: &Matrix3<f64>, a: [f64; 2], b: [f64; 2]) -> f64 {
    let fa = f * Vector3::new(a[0], a[1], 1.0);
    let ftb = f.transpose() * Vector3::new(b[0], b[1], 1.0);
    let numerator = fa.x * b[0] + fa.y * b[1] + fa.z;
    let denominator = fa.x * fa.x + fa.y * fa.y + ftb.x * ftb.x + ftb.y * ftb.y;
    if !(denominator > 0.0 && denominator.is_finite()) {
        return f64::INFINITY;
    }
    let error = numerator * numerator / denominator;
    if error.is_nan() {
        f64::INFINITY
    } else {
        error
    }
}

/// COLMAP's homography residual: the squared distance between `b` and the point `h` maps `a` to,
/// in the second photo only; infinite when `h` sends `a` to infinity.
pub fn transfer_sq(h: &Matrix3<f64>, a: [f64; 2], b: [f64; 2]) -> f64 {
    let p = h * Vector3::new(a[0], a[1], 1.0);
    if p.z == 0.0 {
        return f64::INFINITY;
    }
    // one division, not two: RANSAC measures every correspondence under thousands of hypotheses
    let w = 1.0 / p.z;
    let (dx, dy) = (b[0] - p.x * w, b[1] - p.y * w);
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
    // the poses with -t are counted on the points triangulated with t (`points_in_front_both`)
    let (c1, c1_opposite) = points_in_front_both(&r1, &t, x1, x2);
    let (c2, c2_opposite) = points_in_front_both(&r2, &t, x1, x2);
    let mut best: Option<(Matrix3<f64>, Vector3<f64>, usize)> = None;
    for (r, t, count) in [(r1, t, c1), (r2, t, c2), (r1, -t, c1_opposite), (r2, -t, c2_opposite)] {
        if best.is_none_or(|(_, _, n)| count >= n) {
            best = Some((r, t, count));
        }
    }
    best.filter(|(_, _, count)| *count > 0)
}

/// How many of the correspondences (normalised coordinates) are in front of both cameras under
/// the pose cam2_from_cam1 `(r, t)`, once triangulated (COLMAP's `CheckCheirality`: a point counts
/// when its depth in both cameras is above machine epsilon and below a thousand baselines; none
/// when `t` is zero).
pub fn points_in_front(r: &Matrix3<f64>, t: &Vector3<f64>, x1: &[[f64; 2]], x2: &[[f64; 2]]) -> usize {
    x1.iter().zip(x2).filter(|(&a, &b)| in_front(r, t, a, b)).count()
}

/// `points_in_front` under `(r, t)` and under `(r, -t)`, from one triangulation of each
/// correspondence: negating `t` negates the last column of the linear system of `triangulate`,
/// which the Jacobi rotations carry through exactly (every dot product with that column, and so
/// every rotation, changes sign), so the point it gives is exactly the opposite one and both its
/// depths change sign; the counts are those `points_in_front` gives for each pose.
fn points_in_front_both(r: &Matrix3<f64>, t: &Vector3<f64>, x1: &[[f64; 2]], x2: &[[f64; 2]]) -> (usize, usize) {
    let max_depth = 1000.0 * t.norm();
    let ahead = |d1: f64, d2: f64| d1 > f64::EPSILON && d1 < max_depth && d2 > f64::EPSILON && d2 < max_depth;
    let (mut plus, mut minus) = (0, 0);
    for (&a, &b) in x1.iter().zip(x2) {
        if let Some(p) = triangulate(r, t, a, b) {
            let (depth1, depth2) = (p.z, (r * p + t).z);
            plus += usize::from(ahead(depth1, depth2));
            minus += usize::from(ahead(-depth1, -depth2));
        }
    }
    (plus, minus)
}

fn decompose_essential(e: &Matrix3<f64>) -> Option<(Matrix3<f64>, Matrix3<f64>, Vector3<f64>)> {
    let svd = jacobi_svd(&columns(e));
    let s = &svd.s;
    // a matrix of rank below two (the zero matrix among them) has no pose
    if !(s[0] > 0.0 && s[1] > RANK_TOLERANCE * s[0]) {
        return None;
    }
    // U and V in SO(3): the first two columns from the decomposition, the third their cross
    // product (COLMAP makes the determinants positive by negating U or V as a whole)
    let column = |c: &[f64; 3], scale: f64| Vector3::new(c[0], c[1], c[2]) / scale;
    let (u1, u2) = (column(&svd.av[0], s[0]), column(&svd.av[1], s[1]));
    let (v1, v2) = (column(&svd.v[0], 1.0), column(&svd.v[1], 1.0));
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
    let a: [[f64; 4]; 4] = std::array::from_fn(|j| std::array::from_fn(|i| rows[i][j]));
    let x = jacobi_svd(&a).v[3];
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
fn as_matrix(v: &[f64; 9]) -> Matrix3<f64> {
    Matrix3::from_row_slice(v)
}

/// A basis of the null space of the linear system `A f = 0` whose rows are given, of the dimension
/// `D` a configuration in general position leaves: the right singular vectors of the `D` smallest
/// singular values, the smallest first. `None` when the next singular value is below
/// `RANK_TOLERANCE` times the largest (A has lost rank: the configuration is degenerate).
///
/// A minimal sample has `9 - D` rows, and its `D` smallest singular values are zero: the basis is
/// then any orthonormal basis of the null space, taken from the Householder QR decomposition of
/// `A^T` (the last `D` columns of its Q), and the rank is told by the singular values of the rows
/// alone (no right singular vector accumulated), which is a fraction of the cost of the full
/// decomposition run on every hypothesis of a RANSAC.
fn null_space<const D: usize>(rows: impl IntoIterator<Item = [f64; 9]>) -> Option<[[f64; 9]; D]> {
    // at least nine rows (the rest zero), so that every right singular vector is computed; a
    // taller system is reduced to the R of its QR decomposition, with the same right singular
    // vectors: the first nine rows are kept as they are, a tenth turns them into R and every row
    // after is absorbed into it
    let mut a = [[0.0; 9]; 9];
    let mut count = 0;
    for row in rows {
        if count < 9 {
            a[count] = row;
        } else {
            if count == 9 {
                let first = a;
                a = [[0.0; 9]; 9];
                for row in first {
                    absorb(&mut a, row);
                }
            }
            absorb(&mut a, row);
        }
        count += 1;
    }
    if count + D == 9 && count > 0 {
        return minimal_null_space(&a[..count]);
    }
    // the decomposition takes the columns of the matrix whose rows `a` holds
    let columns: [[f64; 9]; 9] = std::array::from_fn(|j| std::array::from_fn(|i| a[i][j]));
    let svd = jacobi_svd(&columns);
    if !(svd.s[0] > 0.0 && svd.s[8 - D] > RANK_TOLERANCE * svd.s[0]) {
        return None;
    }
    Some(std::array::from_fn(|k| svd.v[8 - k]))
}

/// The null space of the `9 - D` rows (a minimal sample, see `null_space`): `None` when their
/// smallest singular value is below `RANK_TOLERANCE` times the largest.
fn minimal_null_space<const D: usize>(rows: &[[f64; 9]]) -> Option<[[f64; 9]; D]> {
    let m = rows.len();
    let mut columns = [[0.0; 9]; 9];
    columns[..m].copy_from_slice(rows);
    let mut values = [0.0; 9];
    singular_values_of(&mut columns[..m], &mut values[..m]);
    if !(values[0] > 0.0 && values[m - 1] > RANK_TOLERANCE * values[0]) {
        return None;
    }
    // Householder: A^T = H_0 H_1 ... H_{m-1} R, each H_k = I - beta_k v_k v_k^T acting on the
    // entries from k on; the null space of A is spanned by the columns m..9 of Q = H_0 ... H_{m-1}
    let mut at = [[0.0; 9]; 9];
    at[..m].copy_from_slice(rows);
    let mut reflectors = [([0.0; 9], 0.0); 9];
    for k in 0..m {
        let norm = at[k][k..].iter().map(|x| x * x).sum::<f64>().sqrt();
        if !(norm > 0.0) {
            continue;
        }
        let alpha = if at[k][k] > 0.0 { -norm } else { norm };
        let mut v = [0.0; 9];
        v[k..].copy_from_slice(&at[k][k..]);
        v[k] -= alpha;
        let length = v[k..].iter().map(|x| x * x).sum::<f64>();
        if !(length > 0.0) {
            continue;
        }
        let beta = 2.0 / length;
        for column in at.iter_mut().take(m).skip(k) {
            let projection = beta * (k..9).map(|i| v[i] * column[i]).sum::<f64>();
            for i in k..9 {
                column[i] -= projection * v[i];
            }
        }
        reflectors[k] = (v, beta);
    }
    let basis = std::array::from_fn(|n| {
        let mut e = [0.0; 9];
        e[8 - n] = 1.0;
        for (v, beta) in reflectors[..m].iter().rev() {
            let projection = beta * (0..9).map(|i| v[i] * e[i]).sum::<f64>();
            for i in 0..9 {
                e[i] -= projection * v[i];
            }
        }
        e
    });
    Some(basis)
}

/// The row `w` absorbed into the upper triangular `r` (rows) by Givens rotations: `r` becomes the
/// R of the QR decomposition of its rows and `w` (the orthogonal factor is not kept).
fn absorb(r: &mut [[f64; 9]; 9], mut w: [f64; 9]) {
    for k in 0..9 {
        if w[k] == 0.0 {
            continue;
        }
        let (x, y) = (r[k][k], w[k]);
        let h = (x * x + y * y).sqrt();
        if !(h > 0.0 && h.is_finite()) {
            continue;
        }
        let (c, s) = (x / h, y / h);
        r[k][k] = h;
        w[k] = 0.0;
        for j in k + 1..9 {
            let (p, q) = (r[k][j], w[j]);
            r[k][j] = c * p + s * q;
            w[j] = c * q - s * p;
        }
    }
}

/// The matrix of rank two closest to `f`: its smallest singular value zeroed.
fn rank_two(f: &Matrix3<f64>) -> Matrix3<f64> {
    let svd = jacobi_svd(&columns(f));
    Matrix3::from_fn(|i, j| svd.av[0][i] * svd.v[0][j] + svd.av[1][i] * svd.v[1][j])
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
    fn a_solution_found_twice_is_kept_once() {
        let mut random = Lcg(5);
        let a = unit(&random.matrix()).unwrap();
        let b = unit(&random.matrix()).unwrap();
        // the same solution again, with the other sign, and moved by less than the tolerance; a
        // distinct one; one moved by more than the tolerance
        let nudge = Matrix3::from_element(DUPLICATE_TOLERANCE / 10.0);
        let mut solutions = vec![a, -a, a + nudge, b, a + nudge * 100.0];
        drop_duplicates(&mut solutions);
        assert_eq!(solutions.len(), 3, "{solutions:?}");
        assert_eq!(solutions[0], a);
        assert_eq!(solutions[1], b);
        assert!((solutions[2] - a).norm() > DUPLICATE_TOLERANCE);
        let mut none: Vec<Matrix3<f64>> = Vec::new();
        drop_duplicates(&mut none);
        assert!(none.is_empty());
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

    #[test]
    fn both_signs_of_the_translation_are_counted_as_each_alone() {
        // random poses and correspondences, some in front, some behind, some far away
        let mut random = Lcg(17);
        for _ in 0..20 {
            let axis = Vector3::new(random.symmetric(), random.symmetric(), random.symmetric()).normalize();
            let k = Matrix3::new(0.0, -axis.z, axis.y, axis.z, 0.0, -axis.x, -axis.y, axis.x, 0.0);
            let angle = 0.3 * random.symmetric();
            let r = Matrix3::identity() + k * angle.sin() + k * k * (1.0 - angle.cos());
            let t = Vector3::new(random.symmetric(), random.symmetric(), random.symmetric());
            let x1: Vec<[f64; 2]> = (0..50).map(|_| [random.symmetric(), random.symmetric()]).collect();
            let x2: Vec<[f64; 2]> = (0..50).map(|_| [random.symmetric(), random.symmetric()]).collect();
            let both = points_in_front_both(&r, &t, &x1, &x2);
            assert_eq!(both, (points_in_front(&r, &t, &x1, &x2), points_in_front(&r, &(-t), &x1, &x2)));
        }
    }

    #[test]
    fn the_null_space_of_a_minimal_sample_is_orthonormal_and_null() {
        let mut random = Lcg(23);
        for m in [5usize, 7, 8] {
            let rows: Vec<[f64; 9]> = (0..m).map(|_| std::array::from_fn(|_| random.symmetric())).collect();
            let check = |basis: &[[f64; 9]]| {
                for (i, v) in basis.iter().enumerate() {
                    for row in &rows {
                        let residual: f64 = row.iter().zip(v).map(|(a, b)| a * b).sum();
                        assert!(residual.abs() < 1e-14, "{m} rows: |A v| = {residual}");
                    }
                    for (j, w) in basis.iter().enumerate() {
                        let d: f64 = v.iter().zip(w).map(|(a, b)| a * b).sum();
                        let want = if i == j { 1.0 } else { 0.0 };
                        assert!((d - want).abs() < 1e-14, "{m} rows: v{i} . v{j} = {d}");
                    }
                }
            };
            match m {
                5 => check(&null_space::<4>(rows.iter().copied()).unwrap()),
                7 => check(&null_space::<2>(rows.iter().copied()).unwrap()),
                _ => check(&null_space::<1>(rows.iter().copied()).unwrap()),
            }
            // a row repeated: the rank is lost and there is no null space of that dimension
            let mut repeated = rows.clone();
            repeated[m - 1] = repeated[0];
            match m {
                5 => assert!(null_space::<4>(repeated).is_none()),
                7 => assert!(null_space::<2>(repeated).is_none()),
                _ => assert!(null_space::<1>(repeated).is_none()),
            }
        }
    }
}
