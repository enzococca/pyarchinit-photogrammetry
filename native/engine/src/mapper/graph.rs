//! The view graph of the mapper (step 1 of the design, after GLOMAP's): the focal lengths that
//! were guessed, estimated from the fundamental matrices of the pairs (Sweeney et al. 2015, as
//! GLOMAP's view-graph calibration, `estimators/view_graph_calibration.cc`), and the relative pose
//! of every verified pair estimated again from its inliers in normalised coordinates (as GLOMAP's
//! `estimators/relpose_estimation.cc` does with PoseLib; here the five-point essential matrix in
//! the LO-RANSAC of `geometry`), with its inliers and whether it has parallax.
//!
//! Every pair is estimated on its own, in parallel, with the seed of its two photo names; the
//! results are gathered in the order of the pairs and every sum runs in a fixed order, so that
//! two runs give the same bits.

use nalgebra::{DMatrix, DVector, Matrix3, Vector3};
use rayon::prelude::*;

use super::input::Input;
use super::Options;
use crate::camera::Camera;
use crate::geometry::linalg::{columns, singular_values};
use crate::geometry::ransac::{self, lo_ransac, Sampling};
use crate::geometry::refine::{refine_essential, refine_pose};
use crate::geometry::solvers::{pose_from_essential, sampson_sq};
use crate::geometry::two_view::{pair_seed, Config, Essential};

/// A camera with a guessed focal length is calibrated only when it is in at least this many pairs
/// with a fundamental matrix.
pub const MIN_CALIBRATION_PAIRS: usize = 10;
/// The scale of the Cauchy loss on the calibration residuals (GLOMAP's `thres_loss_function`).
const CALIBRATION_LOSS_SCALE: f64 = 1e-2;
/// An estimated focal length outside these ratios to the guessed one is rejected and the guess kept
/// (GLOMAP's `thres_lower_ratio`, `thres_higher_ratio`).
const FOCAL_RATIO_BOUNDS: (f64, f64) = (0.1, 10.0);
/// The iterations of the calibration, its step of the numeric derivatives (in the logarithm of
/// the focal length) and its tolerance on the relative decrease of the cost.
const CALIBRATION_ITERATIONS: usize = 100;
const CALIBRATION_STEP: f64 = 1e-6;
const CALIBRATION_TOLERANCE: f64 = 1e-12;
/// The rounds of refinement of a relative pose on its inliers, each followed by the choice of the
/// inliers under the refined pose.
const POSE_REFINEMENTS: usize = 2;
/// A refined pose is kept only with at least this share of RANSAC's inliers (`refinement_kept`).
const REFINED_INLIER_SHARE: f64 = 0.9;

/// What the calibration did to a camera without a prior focal length: the focal length before and
/// after (the same when it was not calibrated) and the pairs with a fundamental matrix it is in.
#[derive(Clone, Debug, PartialEq)]
pub struct FocalEstimate {
    pub camera: usize,
    pub before: f64,
    pub after: f64,
    pub pairs: usize,
}

/// The pairs whose fundamental matrix the calibration reads: those with one, of a calibrated or an
/// uncalibrated configuration (GLOMAP's choice: a planar or panoramic pair has no reliable one).
fn calibration_pairs(input: &Input) -> Vec<(usize, usize, Matrix3<f64>)> {
    input
        .pairs
        .iter()
        .filter(|p| p.config == Config::Calibrated as i32 || p.config == Config::Uncalibrated as i32)
        .filter_map(|p| {
            let f = p.f?;
            let (ca, cb) = (input.images[p.a].camera, input.images[p.b].camera);
            Some((ca, cb, Matrix3::from_row_slice(&f)))
        })
        .collect()
}

/// `(s1 - s2) / (s1 + s2)` of the two largest singular values of `K2^T F K1`, the essential matrix
/// the focal lengths `fa`, `fb` would give: zero when they are right (an essential matrix has two
/// equal singular values). `K` holds the focal length and the principal point of its camera.
fn calibration_residual(f: &Matrix3<f64>, fa: f64, ca: &Camera, fb: f64, cb: &Camera) -> f64 {
    let k1 = Matrix3::new(fa, 0.0, ca.cx, 0.0, fa, ca.cy, 0.0, 0.0, 1.0);
    let k2 = Matrix3::new(fb, 0.0, cb.cx, 0.0, fb, cb.cy, 0.0, 0.0, 1.0);
    let s = singular_values(&columns(&(k2.transpose() * f * k1)));
    let sum = s[0] + s[1];
    if sum > 0.0 && sum.is_finite() {
        (s[0] - s[1]) / sum
    } else {
        1.0
    }
}

fn cauchy(s: f64) -> (f64, f64) {
    let a2 = CALIBRATION_LOSS_SCALE * CALIBRATION_LOSS_SCALE;
    (a2 * (s / a2).ln_1p(), 1.0 / (1.0 + s / a2))
}

/// The focal lengths of the cameras without `prior_focal` that are in at least
/// `MIN_CALIBRATION_PAIRS` pairs with a fundamental matrix, estimated together by minimising the
/// Cauchy-robust sum of the squared `calibration_residual` of those pairs (the cameras with a prior
/// focal length held), by Levenberg-Marquardt on the logarithms of the focal lengths; each result
/// is written to `cams` (every focal length of the model) unless it is a tenth of the guess or less,
/// or ten times or more. One estimate per camera without a prior focal length, in their order.
///
/// `input` has passed `Input::check` and `cams` are its cameras (`check_cameras`); cameras that do
/// not match it give no estimate and are left as they are.
pub fn calibrate_focals(input: &Input, cams: &mut [Camera]) -> Vec<FocalEstimate> {
    if check_cameras(input, cams).is_err() {
        return Vec::new();
    }
    let pairs = calibration_pairs(input);
    let mut count = vec![0usize; cams.len()];
    for &(ca, cb, _) in &pairs {
        count[ca] += 1;
        if cb != ca {
            count[cb] += 1;
        }
    }
    let guessed: Vec<usize> = (0..cams.len()).filter(|&c| !input.cameras[c].prior_focal).collect();
    let free: Vec<usize> = guessed.iter().copied().filter(|&c| count[c] >= MIN_CALIBRATION_PAIRS).collect();
    let mut column = vec![usize::MAX; cams.len()];
    for (j, &c) in free.iter().enumerate() {
        column[c] = j;
    }
    let used: Vec<&(usize, usize, Matrix3<f64>)> =
        pairs.iter().filter(|(ca, cb, _)| column[*ca] != usize::MAX || column[*cb] != usize::MAX).collect();
    let before: Vec<f64> = cams.iter().map(|c| c.focal()).collect();
    let solved = if free.is_empty() || used.is_empty() {
        Vec::new()
    } else {
        solve_focals(&used, cams, &before, &free, &column)
    };
    for (j, &c) in free.iter().enumerate() {
        if let Some(&f) = solved.get(j) {
            let ratio = f / before[c];
            if f.is_finite() && ratio > FOCAL_RATIO_BOUNDS.0 && ratio < FOCAL_RATIO_BOUNDS.1 {
                cams[c].set_focal(f);
            }
        }
    }
    guessed.iter().map(|&c| FocalEstimate { camera: c, before: before[c], after: cams[c].focal(), pairs: count[c] })
        .collect()
}

/// The Levenberg-Marquardt of `calibrate_focals`: the focal lengths of the `free` cameras (`column`
/// gives the column of a camera, `usize::MAX` for a held one), from `before`.
fn solve_focals(pairs: &[&(usize, usize, Matrix3<f64>)], cams: &[Camera], before: &[f64], free: &[usize],
                column: &[usize]) -> Vec<f64> {
    let n = free.len();
    let mut theta: Vec<f64> = free.iter().map(|&c| before[c].ln()).collect();
    let focal = |theta: &[f64], c: usize| if column[c] == usize::MAX { before[c] } else { theta[column[c]].exp() };
    let residual = |theta: &[f64], k: usize, da: f64, db: f64| {
        let (ca, cb, f) = pairs[k];
        let (fa, fb) = if ca == cb {
            let x = focal(theta, *ca) * (da + db).exp();
            (x, x)
        } else {
            (focal(theta, *ca) * da.exp(), focal(theta, *cb) * db.exp())
        };
        calibration_residual(f, fa, &cams[*ca], fb, &cams[*cb])
    };
    let cost_of =
        |theta: &[f64]| (0..pairs.len()).map(|k| cauchy(residual(theta, k, 0.0, 0.0).powi(2)).0).sum::<f64>();
    let mut cost = cost_of(&theta);
    let mut lambda = 1e-4;
    let h = CALIBRATION_STEP;
    for _ in 0..CALIBRATION_ITERATIONS {
        // the normal equations of the reweighted problem (the weight of a residual the slope of
        // the loss), the derivatives by central differences on the cameras of each pair
        let mut jtj = DMatrix::<f64>::zeros(n, n);
        let mut g = DVector::<f64>::zeros(n);
        for (k, (ca, cb, _)) in pairs.iter().enumerate() {
            let r = residual(&theta, k, 0.0, 0.0);
            let (_, w) = cauchy(r * r);
            let mut entries: Vec<(usize, f64)> = Vec::with_capacity(2);
            if ca == cb {
                if column[*ca] != usize::MAX {
                    let d = (residual(&theta, k, h, 0.0) - residual(&theta, k, -h, 0.0)) / (2.0 * h);
                    entries.push((column[*ca], d));
                }
            } else {
                if column[*ca] != usize::MAX {
                    let d = (residual(&theta, k, h, 0.0) - residual(&theta, k, -h, 0.0)) / (2.0 * h);
                    entries.push((column[*ca], d));
                }
                if column[*cb] != usize::MAX {
                    let d = (residual(&theta, k, 0.0, h) - residual(&theta, k, 0.0, -h)) / (2.0 * h);
                    entries.push((column[*cb], d));
                }
            }
            for &(i, di) in &entries {
                g[i] += w * di * r;
                for &(j, dj) in &entries {
                    jtj[(i, j)] += w * di * dj;
                }
            }
        }
        let mut accepted = false;
        while lambda < 1e16 {
            let mut a = jtj.clone();
            for i in 0..n {
                a[(i, i)] += lambda * jtj[(i, i)].max(1e-12);
            }
            let Some(chol) = a.cholesky() else {
                lambda *= 10.0;
                continue;
            };
            let step = chol.solve(&(-&g));
            let trial: Vec<f64> = theta.iter().zip(step.iter()).map(|(t, d)| t + d).collect();
            let trial_cost = cost_of(&trial);
            if trial_cost.is_finite() && trial_cost < cost {
                let decrease = (cost - trial_cost) / cost.max(f64::MIN_POSITIVE);
                theta = trial;
                cost = trial_cost;
                lambda = (lambda / 10.0).max(1e-16);
                accepted = decrease > CALIBRATION_TOLERANCE && step.amax() > 1e-14;
                break;
            }
            lambda *= 10.0;
        }
        if !accepted {
            break;
        }
    }
    theta.iter().map(|t| t.exp()).collect()
}

/// `Err` unless `cams` are as many as the cameras of `input`: the contract of `calibrate_focals`
/// and `ViewGraph::build`, which index them by the cameras of the photos (an input that passed
/// `Input::check` has those indices in range).
pub fn check_cameras(input: &Input, cams: &[Camera]) -> Result<(), String> {
    if cams.len() != input.cameras.len() {
        return Err(format!("{} cameras given, {} in the input", cams.len(), input.cameras.len()));
    }
    Ok(())
}

/// A pair of the view graph: its index in `Input::pairs`, its photos, the relative pose
/// `cam_b_from_cam_a` (`X_b = R X_a + t`, `t` of unit norm), its inliers (their count and their
/// indices in `Input::matches`), the median triangulation angle of the inliers in degrees, whether
/// that reaches `Options::min_pair_parallax_deg`, and its weight (the inlier count).
///
/// An edge without parallax (`parallax == false`: a pair taken from one place, or with too short a
/// baseline for its depth) has a meaningful rotation only: its translation is the direction the
/// noise gave. The later stages take such an edge for the rotation averaging alone, never for the
/// positioning, the triangulation or anything else that reads its translation.
#[derive(Clone, Debug, PartialEq)]
pub struct Edge {
    pub pair: usize,
    pub a: usize,
    pub b: usize,
    pub rotation: [[f64; 3]; 3],
    pub translation: [f64; 3],
    pub inliers: usize,
    pub inlier_matches: Vec<u32>,
    pub parallax_deg: f64,
    pub parallax: bool,
    pub weight: f64,
}

/// The view graph: the edges in the order of the pairs, the pairs left out because their relative
/// pose could not be estimated (`left_out`) and those with fewer matches than
/// `Options::min_inliers` (`too_few`, not estimated); `refused` the reason when nothing was
/// estimated because the cameras do not match the input (`check_cameras`), the graph then empty.
#[derive(Clone, Debug, PartialEq)]
pub struct ViewGraph {
    pub edges: Vec<Edge>,
    pub left_out: usize,
    pub too_few: usize,
    pub refused: Option<String>,
}

enum Outcome {
    TooFew,
    LeftOut,
    Edge(Edge),
}

impl ViewGraph {
    /// The relative pose of every pair with at least `o.min_inliers` matches, from those matches in
    /// the normalised coordinates of `cams` (the cameras of `input`, after `calibrate_focals`):
    /// the five-point essential matrix in LO-RANSAC with the threshold `o.max_epipolar_error_px`
    /// over the mean focal length of the two photos and the seed `pair_seed` of their names, its
    /// refinement on the Sampson error, then the pose of the essential matrix. A pair whose
    /// estimation fails or keeps fewer than `o.min_inliers` inliers is left out.
    ///
    /// `input` has passed `Input::check`; `cams` that do not match it (`check_cameras`) give an
    /// empty graph with the reason in `refused`.
    pub fn build(input: &Input, cams: &[Camera], o: &Options) -> ViewGraph {
        if let Err(reason) = check_cameras(input, cams) {
            return ViewGraph { edges: Vec::new(), left_out: 0, too_few: 0, refused: Some(reason) };
        }
        let outcomes: Vec<Outcome> =
            (0..input.pairs.len()).into_par_iter().map(|p| relative_pose(input, cams, o, p)).collect();
        let mut graph = ViewGraph { edges: Vec::new(), left_out: 0, too_few: 0, refused: None };
        for outcome in outcomes {
            match outcome {
                Outcome::TooFew => graph.too_few += 1,
                Outcome::LeftOut => graph.left_out += 1,
                Outcome::Edge(e) => graph.edges.push(e),
            }
        }
        graph
    }
}

fn relative_pose(input: &Input, cams: &[Camera], o: &Options, p: usize) -> Outcome {
    let pair = &input.pairs[p];
    if pair.matches.len() < o.min_inliers {
        return Outcome::TooFew;
    }
    let (ia, ib) = (&input.images[pair.a], &input.images[pair.b]);
    let (cam_a, cam_b) = (&cams[ia.camera], &cams[ib.camera]);
    let normalised = |cam: &Camera, image: usize, k: u32| {
        let [x, y] = input.keypoint(image, k);
        let (u, v) = cam.normalised(x, y);
        [u, v]
    };
    let matches = &input.matches[pair.matches.clone()];
    let n1: Vec<[f64; 2]> = matches.iter().map(|m| normalised(cam_a, pair.a, m[0])).collect();
    let n2: Vec<[f64; 2]> = matches.iter().map(|m| normalised(cam_b, pair.b, m[1])).collect();
    let error = o.max_epipolar_error_px;
    let ro = ransac::Options {
        max_error: (error / cam_a.focal() + error / cam_b.focal()) / 2.0,
        max_num_trials: o.max_relpose_trials,
        ..ransac::Options::default()
    };
    let seed = pair_seed(&ia.name, &ib.name);
    let Some(report) = lo_ransac::<Essential>(&n1, &n2, &ro, Sampling::Uniform, seed) else {
        return Outcome::LeftOut;
    };
    let report = refine_essential(report, &n1, &n2, ro.max_error);
    let mut chosen: Vec<usize> = (0..matches.len()).filter(|&i| report.inlier_mask[i]).collect();
    if chosen.len() < o.min_inliers {
        return Outcome::LeftOut;
    }
    let pick = |chosen: &[usize], x: &[[f64; 2]]| chosen.iter().map(|&i| x[i]).collect::<Vec<_>>();
    let Some((mut r, mut t, _)) = pose_from_essential(&report.model.0, &pick(&chosen, &n1), &pick(&chosen, &n2))
    else {
        return Outcome::LeftOut;
    };
    // the pose refined on its inliers and the inliers chosen again under it, twice (RANSAC's
    // refinement is kept only when it loses no inlier, which a wrong match near its epipolar line
    // can decide against the accuracy of the pose); RANSAC's pose and inliers are kept when the
    // refinement loses too many of them (`refinement_kept`)
    let max_sq = ro.max_error * ro.max_error;
    let ransac = (r, t, chosen.clone());
    for _ in 0..POSE_REFINEMENTS {
        let (rr, tr) = refine_pose(&r, &t, &pick(&chosen, &n1), &pick(&chosen, &n2));
        let e = skew(&tr) * rr;
        let again: Vec<usize> = (0..matches.len()).filter(|&i| sampson_sq(&e, n1[i], n2[i]) <= max_sq).collect();
        if !refinement_kept(ransac.2.len(), again.len()) {
            (r, t, chosen) = ransac;
            break;
        }
        (r, t, chosen) = (rr, tr, again);
    }
    let (i1, i2) = (pick(&chosen, &n1), pick(&chosen, &n2));
    let parallax_deg = median_ray_angle_deg(&r, &i1, &i2);
    Outcome::Edge(Edge {
        pair: p,
        a: pair.a,
        b: pair.b,
        rotation: std::array::from_fn(|i| std::array::from_fn(|j| r[(i, j)])),
        translation: [t.x, t.y, t.z],
        inliers: chosen.len(),
        inlier_matches: chosen.iter().map(|&i| (pair.matches.start + i) as u32).collect(),
        parallax_deg,
        parallax: parallax_deg >= o.min_pair_parallax_deg,
        weight: chosen.len() as f64,
    })
}

/// Whether a refined pose with `refined` inliers replaces RANSAC's with `ransac`: when it keeps at
/// least `REFINED_INLIER_SHARE` of them. A refinement on the inliers by least squares moves the
/// pose by a fraction of the threshold and keeps nearly all of them; one that loses more has been
/// pulled away by inliers that were not (a wrong match on its epipolar line, a second motion).
fn refinement_kept(ransac: usize, refined: usize) -> bool {
    refined as f64 >= REFINED_INLIER_SHARE * ransac as f64
}

fn skew(t: &Vector3<f64>) -> Matrix3<f64> {
    Matrix3::new(0.0, -t.z, t.y, t.z, 0.0, -t.x, -t.y, t.x, 0.0)
}

/// The median, in degrees, of the angle between the two rays of each correspondence under the pose
/// `cam2_from_cam1` (`r`): the triangulation angle of a point in front of both cameras (the rays
/// from the two centres meet there), whatever the length of the baseline, so that a pair without
/// one gives angles of the noise alone.
fn median_ray_angle_deg(r: &Matrix3<f64>, x1: &[[f64; 2]], x2: &[[f64; 2]]) -> f64 {
    let rt = r.transpose();
    let mut angles: Vec<f64> = x1
        .iter()
        .zip(x2)
        .map(|(a, b)| {
            let d1 = Vector3::new(a[0], a[1], 1.0);
            let d2 = rt * Vector3::new(b[0], b[1], 1.0);
            d1.cross(&d2).norm().atan2(d1.dot(&d2)).to_degrees()
        })
        .collect();
    angles.sort_by(|p, q| p.total_cmp(q));
    let n = angles.len();
    if n == 0 {
        return 0.0;
    }
    if n % 2 == 1 {
        angles[n / 2]
    } else {
        (angles[n / 2 - 1] + angles[n / 2]) / 2.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The guard of the refinement of a relative pose. A pair whose refinement on its own inliers
    /// loses a tenth of them is not built from synthetic matches without staging the least squares
    /// themselves (on inliers within 1 px of their epipolar lines the refined pose moves by a
    /// fraction of a pixel), so the rule is pinned here and the path through it by the scenes.
    #[test]
    fn a_refined_pose_that_loses_a_tenth_of_the_inliers_is_not_kept() {
        assert!(refinement_kept(100, 100));
        assert!(refinement_kept(100, 103));
        assert!(refinement_kept(100, 90));
        assert!(!refinement_kept(100, 89));
        assert!(!refinement_kept(415, 0));
    }
}
