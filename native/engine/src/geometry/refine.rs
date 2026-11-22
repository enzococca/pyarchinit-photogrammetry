//! The refinement of a model on its inliers after RANSAC (`two_view::Options::use_sampson_refinement`):
//! least squares by Levenberg-Marquardt on the Sampson error for the essential matrix (through its
//! pose on SO(3) x S2, the parametrisation of COLMAP's `RefineRelativePose`) and for the fundamental
//! matrix (as `U diag(1, s, 0) V^T`, its rank kept at two), and on the transfer error for the
//! homography (its entries, on points normalised as Hartley normalises them, so that the entries are
//! of order one whatever the scale of the pixels). The refined model replaces RANSAC's only when its
//! support over all the correspondences is not worse.

use nalgebra::{DMatrix, DVector, Matrix3, Vector3};

use super::linalg::{columns, jacobi_svd, normalise, unit};
use super::ransac::{Model, Report, Support};
use super::solvers::pose_from_essential;
use super::two_view::{Essential, Fundamental, Homography};

/// A model with a local parametrisation about itself, for the least squares on its inliers.
trait Refinable: Clone {
    const PARAMS: usize;
    const RESIDUALS: usize;
    /// The model moved by `delta` (`PARAMS` values) along its parametrisation.
    fn perturbed(&self, delta: &[f64]) -> Self;
    /// The signed residuals (`RESIDUALS` values) of a correspondence.
    fn residuals(&self, a: [f64; 2], b: [f64; 2], out: &mut [f64]);
}

/// The step of the central differences of the Jacobian: the parameters are angles, ratios of
/// singular values or entries of a matrix of unit norm.
const DIFFERENCE_STEP: f64 = 1e-6;

/// Levenberg-Marquardt from `start` on the correspondences: at most 20 steps, each from the
/// normal equations with the diagonal damped, accepted when it lowers the sum of squared
/// residuals (the damping then divided by 3, otherwise multiplied by 10 and the step tried
/// again); stops when a step lowers the cost by less than 1e-10 of it, or the damping passes
/// 1e8. Gives the best model reached.
fn levenberg_marquardt<R: Refinable>(start: &R, x1: &[[f64; 2]], x2: &[[f64; 2]]) -> R {
    let (p, k) = (R::PARAMS, R::RESIDUALS);
    let mut scratch = vec![0.0; k];
    let mut cost_of = |model: &R| -> f64 {
        x1.iter().zip(x2).fold(0.0, |sum, (&a, &b)| {
            model.residuals(a, b, &mut scratch);
            sum + scratch.iter().map(|r| r * r).sum::<f64>()
        })
    };
    let mut current = start.clone();
    let mut cost = cost_of(&current);
    if !cost.is_finite() {
        return current;
    }
    let mut lambda = 1e-3;
    let mut delta = vec![0.0; p];
    for _ in 0..20 {
        // the Jacobian by central differences, accumulated into the normal equations
        let mut jtj = DMatrix::<f64>::zeros(p, p);
        let mut jtr = DVector::<f64>::zeros(p);
        let moved: Vec<(R, R)> = (0..p)
            .map(|j| {
                delta.fill(0.0);
                delta[j] = DIFFERENCE_STEP;
                let plus = current.perturbed(&delta);
                delta[j] = -DIFFERENCE_STEP;
                (plus, current.perturbed(&delta))
            })
            .collect();
        let mut r = vec![0.0; k];
        let (mut plus, mut minus) = (vec![0.0; k], vec![0.0; k]);
        let mut jac = vec![0.0; k * p];
        for (&a, &b) in x1.iter().zip(x2) {
            current.residuals(a, b, &mut r);
            for (j, (mp, mm)) in moved.iter().enumerate() {
                mp.residuals(a, b, &mut plus);
                mm.residuals(a, b, &mut minus);
                for c in 0..k {
                    jac[c * p + j] = (plus[c] - minus[c]) / (2.0 * DIFFERENCE_STEP);
                }
            }
            for c in 0..k {
                let row = &jac[c * p..(c + 1) * p];
                for i in 0..p {
                    jtr[i] += row[i] * r[c];
                    for j in 0..p {
                        jtj[(i, j)] += row[i] * row[j];
                    }
                }
            }
        }
        if !(jtj.iter().all(|v| v.is_finite()) && jtr.iter().all(|v| v.is_finite())) {
            break;
        }
        let mut accepted = false;
        let mut converged = false;
        while lambda <= 1e8 {
            let mut damped = jtj.clone();
            for i in 0..p {
                damped[(i, i)] += lambda * jtj[(i, i)].max(1e-12);
            }
            let Some(step) = damped.lu().solve(&(-&jtr)) else {
                lambda *= 10.0;
                continue;
            };
            let candidate = current.perturbed(step.as_slice());
            let candidate_cost = cost_of(&candidate);
            if candidate_cost < cost {
                converged = cost - candidate_cost <= 1e-10 * cost;
                current = candidate;
                cost = candidate_cost;
                lambda /= 3.0;
                accepted = true;
                break;
            }
            lambda *= 10.0;
        }
        if !accepted || converged {
            break;
        }
    }
    current
}

/// The signed Sampson error (the square root of `sampson_sq` with the sign of `b^T F a`); zero
/// where the gradient vanishes.
fn sampson_signed(f: &Matrix3<f64>, a: [f64; 2], b: [f64; 2]) -> f64 {
    let fa = f * Vector3::new(a[0], a[1], 1.0);
    let ftb = f.transpose() * Vector3::new(b[0], b[1], 1.0);
    let numerator = fa.x * b[0] + fa.y * b[1] + fa.z;
    let denominator = fa.x * fa.x + fa.y * fa.y + ftb.x * ftb.x + ftb.y * ftb.y;
    if denominator > 0.0 && denominator.is_finite() {
        numerator / denominator.sqrt()
    } else {
        0.0
    }
}

/// The rotation of the vector `w` (Rodrigues; the angle is its norm).
pub(crate) fn exp_so3(w: &Vector3<f64>) -> Matrix3<f64> {
    let angle = w.norm();
    let k = Matrix3::new(0.0, -w.z, w.y, w.z, 0.0, -w.x, -w.y, w.x, 0.0);
    if angle < 1e-12 {
        return Matrix3::identity() + k;
    }
    Matrix3::identity() + k * (angle.sin() / angle) + k * k * ((1.0 - angle.cos()) / (angle * angle))
}

/// Two unit vectors orthogonal to the unit vector `t` and to each other.
fn tangent_basis(t: &Vector3<f64>) -> (Vector3<f64>, Vector3<f64>) {
    let axis = if t.x.abs() <= t.y.abs() && t.x.abs() <= t.z.abs() {
        Vector3::x()
    } else if t.y.abs() <= t.z.abs() {
        Vector3::y()
    } else {
        Vector3::z()
    };
    let b1 = t.cross(&axis).normalize();
    (b1, t.cross(&b1))
}

/// The essential matrix as a pose: the rotation moved on SO(3), the unit translation on its
/// sphere (five parameters, COLMAP's `RefineRelativePose`); the residual is the Sampson error in
/// normalised coordinates. The essential matrix of the pose is computed once with it, not for
/// every correspondence.
#[derive(Clone)]
struct EssentialPose {
    r: Matrix3<f64>,
    t: Vector3<f64>,
    b1: Vector3<f64>,
    b2: Vector3<f64>,
    e: Matrix3<f64>,
}

impl EssentialPose {
    fn new(r: Matrix3<f64>, t: Vector3<f64>) -> EssentialPose {
        let t = t.normalize();
        let (b1, b2) = tangent_basis(&t);
        let e = Matrix3::new(0.0, -t.z, t.y, t.z, 0.0, -t.x, -t.y, t.x, 0.0) * r;
        EssentialPose { r, t, b1, b2, e }
    }

    fn essential(&self) -> Matrix3<f64> {
        self.e
    }
}

impl Refinable for EssentialPose {
    const PARAMS: usize = 5;
    const RESIDUALS: usize = 1;

    fn perturbed(&self, d: &[f64]) -> EssentialPose {
        let r = exp_so3(&Vector3::new(d[0], d[1], d[2])) * self.r;
        EssentialPose::new(r, self.t + self.b1 * d[3] + self.b2 * d[4])
    }

    fn residuals(&self, a: [f64; 2], b: [f64; 2], out: &mut [f64]) {
        out[0] = sampson_signed(&self.e, a, b);
    }
}

/// The fundamental matrix as `U diag(1, s, 0) V^T`, `U` and `V` rotations moved on SO(3) and `s`
/// free (seven parameters, the rank kept at two); the residual is the Sampson error in pixels.
/// The matrix they compose is computed once with them, not for every correspondence.
#[derive(Clone)]
struct FundamentalFactors {
    u: Matrix3<f64>,
    v: Matrix3<f64>,
    s: f64,
    f: Matrix3<f64>,
}

impl FundamentalFactors {
    /// `None` when `f` has rank below two or is not finite.
    fn of(f: &Matrix3<f64>) -> Option<FundamentalFactors> {
        if !f.iter().all(|v| v.is_finite()) {
            return None;
        }
        let svd = jacobi_svd(&columns(f));
        let s = &svd.s;
        if !(s[0] > 0.0 && s[1] > 1e-10 * s[0]) {
            return None;
        }
        let column = |c: &[f64; 3], scale: f64| Vector3::new(c[0], c[1], c[2]) / scale;
        let (u1, u2) = (column(&svd.av[0], s[0]), column(&svd.av[1], s[1]));
        let (v1, v2) = (column(&svd.v[0], 1.0), column(&svd.v[1], 1.0));
        let u = Matrix3::from_columns(&[u1, u2, u1.cross(&u2)]);
        let v = Matrix3::from_columns(&[v1, v2, v1.cross(&v2)]);
        Some(FundamentalFactors::new(u, v, s[1] / s[0]))
    }

    fn new(u: Matrix3<f64>, v: Matrix3<f64>, s: f64) -> FundamentalFactors {
        let f = u * Matrix3::from_diagonal(&Vector3::new(1.0, s, 0.0)) * v.transpose();
        FundamentalFactors { u, v, s, f }
    }

    fn fundamental(&self) -> Matrix3<f64> {
        self.f
    }
}

impl Refinable for FundamentalFactors {
    const PARAMS: usize = 7;
    const RESIDUALS: usize = 1;

    fn perturbed(&self, d: &[f64]) -> FundamentalFactors {
        FundamentalFactors::new(
            exp_so3(&Vector3::new(d[0], d[1], d[2])) * self.u,
            exp_so3(&Vector3::new(d[3], d[4], d[5])) * self.v,
            self.s + d[6],
        )
    }

    fn residuals(&self, a: [f64; 2], b: [f64; 2], out: &mut [f64]) {
        out[0] = sampson_signed(&self.f, a, b);
    }
}

/// The homography by its entries but the largest, which is held (eight parameters; the matrix is
/// of unit norm); the residuals are the transfer error in the second photo, as RANSAC measures it.
#[derive(Clone)]
struct HomographyEntries {
    h: Matrix3<f64>,
    held: usize,
}

impl HomographyEntries {
    fn of(h: &Matrix3<f64>) -> Option<HomographyEntries> {
        let h = unit(h)?;
        let held = (0..9).max_by(|&i, &j| h[i].abs().total_cmp(&h[j].abs())).expect("nine entries");
        Some(HomographyEntries { h, held })
    }
}

impl Refinable for HomographyEntries {
    const PARAMS: usize = 8;
    const RESIDUALS: usize = 2;

    fn perturbed(&self, d: &[f64]) -> HomographyEntries {
        let mut h = self.h;
        let mut next = d.iter();
        for i in 0..9 {
            if i != self.held {
                h[i] += next.next().expect("eight values");
            }
        }
        HomographyEntries { h: unit(&h).unwrap_or(self.h), held: self.held }
    }

    fn residuals(&self, a: [f64; 2], b: [f64; 2], out: &mut [f64]) {
        let p = self.h * Vector3::new(a[0], a[1], 1.0);
        if p.z != 0.0 && p.z.is_finite() {
            out[0] = p.x / p.z - b[0];
            out[1] = p.y / p.z - b[1];
        } else {
            out[0] = 0.0;
            out[1] = 0.0;
        }
    }
}

/// The correspondences the mask selects.
fn selected(mask: &[bool], x1: &[[f64; 2]], x2: &[[f64; 2]]) -> (Vec<[f64; 2]>, Vec<[f64; 2]>) {
    let keep = |x: &[[f64; 2]]| x.iter().zip(mask).filter(|(_, &m)| m).map(|(p, _)| *p).collect();
    (keep(x1), keep(x2))
}

/// The report with `model` in place of its own when the support of `model` over all the
/// correspondences is not worse; the inliers counted again either way.
fn kept_if_not_worse<M: Model>(report: Report<M>, model: M, x1: &[[f64; 2]], x2: &[[f64; 2]], max_error: f64)
                               -> Report<M> {
    let max_residual = max_error * max_error;
    let residuals: Vec<f64> = x1.iter().zip(x2).map(|(&a, &b)| model.residual_sq(a, b)).collect();
    let support = Support::evaluate(&residuals, max_residual);
    if report.support.better_than(&support) {
        return report;
    }
    let inlier_mask = residuals.iter().map(|&r| r <= max_residual).collect();
    Report { model, support, inlier_mask, num_trials: report.num_trials }
}

/// COLMAP's `RefineEssentialMatrix`: the pose of the essential matrix on its inliers, refined by
/// least squares on the Sampson error, recomposed; the report kept as it is when the matrix has no
/// pose (no point in front of both cameras).
pub(crate) fn refine_essential(report: Report<Essential>, x1: &[[f64; 2]], x2: &[[f64; 2]], max_error: f64)
                               -> Report<Essential> {
    let (i1, i2) = selected(&report.inlier_mask, x1, x2);
    let Some((r, t, _)) = pose_from_essential(&report.model.0, &i1, &i2) else {
        return report;
    };
    let pose = levenberg_marquardt(&EssentialPose::new(r, t), &i1, &i2);
    match unit(&pose.essential()) {
        Some(e) => kept_if_not_worse(report, Essential(e), x1, x2, max_error),
        None => report,
    }
}

/// The fundamental matrix refined on its inliers by least squares on the Sampson error, its rank
/// kept at two.
pub(crate) fn refine_fundamental(report: Report<Fundamental>, x1: &[[f64; 2]], x2: &[[f64; 2]], max_error: f64)
                                 -> Report<Fundamental> {
    let (i1, i2) = selected(&report.inlier_mask, x1, x2);
    let Some(factors) = FundamentalFactors::of(&report.model.0) else {
        return report;
    };
    let refined = levenberg_marquardt(&factors, &i1, &i2);
    match unit(&refined.fundamental()) {
        Some(f) => kept_if_not_worse(report, Fundamental(f), x1, x2, max_error),
        None => report,
    }
}

/// The homography refined on its inliers by least squares on the transfer error.
pub(crate) fn refine_homography(report: Report<Homography>, x1: &[[f64; 2]], x2: &[[f64; 2]], max_error: f64)
                                -> Report<Homography> {
    let (i1, i2) = selected(&report.inlier_mask, x1, x2);
    match refined_homography(&report.model.0, &i1, &i2) {
        Some(h) => kept_if_not_worse(report, Homography(h), x1, x2, max_error),
        None => report,
    }
}

/// The homography `h` (`x2 ~ h x1`) refined on the correspondences by least squares on the transfer
/// error, in Hartley's normalised coordinates (`T2 h T1^-1` has entries of order one, so the step of
/// the numeric Jacobian means the same for each; the result is brought back to the pixels). `None`
/// when the points cannot be normalised (they coincide) or the matrix is not finite.
fn refined_homography(h: &Matrix3<f64>, x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Option<Matrix3<f64>> {
    let (n1, n2) = (normalise(x1)?, normalise(x2)?);
    let entries = HomographyEntries::of(&(n2.matrix * h * n1.inverse))?;
    let refined = levenberg_marquardt(&entries, &n1.points, &n2.points);
    unit(&(n2.inverse * refined.h * n1.matrix))
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::solvers::transfer_sq;

    #[test]
    fn the_refinement_recovers_a_pixel_scale_homography_with_perspective() {
        // the homography of a tilted plane in pixels (entries from 1e-7 to hundreds), points on a
        // grid over the photo, a start a few pixels off at the corners: the transfer error falls to
        // roundoff, which the raw entries with a fixed step could not do without the normalisation
        let k = Matrix3::new(1000.0, 0.0, 1000.0, 0.0, 1000.0, 750.0, 0.0, 0.0, 1.0);
        let r = exp_so3(&(Vector3::new(0.2, 1.0, 0.1).normalize() * 0.2));
        let n = Vector3::new(-0.3, 0.2, 1.0).normalize();
        let truth = k * (r + Vector3::new(0.8, 0.1, 0.05) * n.transpose() / 4.0) * k.try_inverse().unwrap();
        let x1: Vec<[f64; 2]> =
            (0..60).map(|i| [100.0 + 200.0 * (i % 10) as f64, 125.0 + 250.0 * (i / 10) as f64]).collect();
        let x2: Vec<[f64; 2]> = x1
            .iter()
            .map(|a| {
                let p = truth * Vector3::new(a[0], a[1], 1.0);
                [p.x / p.z, p.y / p.z]
            })
            .collect();
        // the start: the scales off by two parts in a thousand, the translation by 3 and 2 px,
        // the perspective terms by 1e-6 (a few pixels at the corners of the photo)
        let mut start = truth;
        start[(0, 0)] *= 1.002;
        start[(1, 1)] *= 0.998;
        start[(0, 2)] += 3.0;
        start[(1, 2)] -= 2.0;
        start[(2, 0)] += 1e-6;
        start[(2, 1)] -= 1e-6;
        let error = |h: &Matrix3<f64>| x1.iter().zip(&x2).map(|(&a, &b)| transfer_sq(h, a, b)).sum::<f64>();
        assert!(error(&start) > 100.0 && error(&start) < 1e5, "the start is pixels off: {}", error(&start));
        let refined = refined_homography(&start, &x1, &x2).expect("a refined homography");
        assert!(error(&refined) < 1e-12, "{} left of {}", error(&refined), error(&start));
        // points that coincide cannot be normalised
        assert!(refined_homography(&start, &[[1.0, 2.0]; 5], &x2[..5]).is_none());
    }

    #[test]
    fn the_factors_of_a_fundamental_matrix_recompose_it() {
        let skew = Matrix3::new(0.0, -0.3, 2.0, 0.3, 0.0, -1.0, -2.0, 1.0, 0.0);
        let f = skew * Matrix3::new(1.0, 0.2, 0.0, -0.2, 1.0, 0.0, 0.0, 0.0, 1.0);
        let factors = FundamentalFactors::of(&f).unwrap();
        let back = factors.fundamental();
        let (a, b) = (back / back.norm(), f / f.norm());
        assert!((a - b).norm().min((a + b).norm()) < 1e-12);
        assert!(FundamentalFactors::of(&Matrix3::zeros()).is_none());
        assert!(FundamentalFactors::of(&Matrix3::from_element(f64::NAN)).is_none());
    }
}
