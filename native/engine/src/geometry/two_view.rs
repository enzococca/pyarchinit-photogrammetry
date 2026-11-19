//! The geometric verification of a pair of photos: the configuration of the pair and its inlier
//! matches, decided as COLMAP's `EstimateTwoViewGeometry` decides them (Schonberger and Frahm
//! 2016; src/colmap/estimators/two_view_geometry.cc, BSD 3-clause, see native/NOTICE). The
//! essential matrix (when both cameras have a prior focal length), the fundamental matrix and the
//! homography are each estimated by LO-RANSAC (`ransac`) on the minimal solvers (`solvers`); the
//! configuration follows from their inlier counts: calibrated or uncalibrated when the epipolar
//! model holds and the homography explains less than `max_h_inlier_ratio` of its inliers, planar
//! or panoramic when it explains more, a watermark when the inliers do not move and all lie in
//! the border band of the photo, degenerate when no model gains `min_num_inliers`. With
//! `compute_relative_pose` the pose cam2_from_cam1 is recovered (from the essential matrix, or
//! from the homography, whose decomposition tells a plane from a pure rotation: then the
//! configuration is `Planar` or `Panoramic`).
//!
//! Key points are in COLMAP pixels (the centre of the first pixel at 0.5, 0.5); the matches are
//! pairs of indices into them with a quality each (the ratio of the two best descriptor
//! distances, lower is better), which orders the PROSAC sampling. The RANSAC is seeded by the
//! caller (`pair_seed` of the two photo names): the same pair gives the same result in every
//! run, every database and every thread.

use nalgebra::{DMatrix, DVector, Matrix3, Rotation3, UnitQuaternion, Vector3};

use super::linalg::{dynamic, jacobi_svd, unit};
use super::ransac::{self, lo_ransac, Model, Report, Sampling, Support};
use super::solvers::{
    essential_n_point, fundamental_eight_point, fundamental_seven_point, homography_dlt, points_in_front,
    pose_from_essential, sampson_sq, transfer_sq,
};
use crate::camera::Camera;

/// The options of the verification: the fields of COLMAP's `TwoViewGeometryOptions` the native
/// engine reads, with the defaults of `pycolmap.TwoViewGeometryOptions()` of the installed
/// pycolmap (4.2.1), pinned by `tests/test_native.py`. Not ported, each off by default and the
/// engine behaves as COLMAP with them off: `min_inlier_ratio` (0.0), `multiple_ignore_watermark`
/// and `multiple_models`, `filter_stationary_matches` with `stationary_matches_max_error`,
/// `force_H_use`, `use_degensac`; `ransac.random_seed` is the `seed` argument of `verify`, and
/// `ransac.num_threads` has no meaning here.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Options {
    pub ransac: ransac::Options,
    /// Fewer inliers than this under every model: the pair is degenerate.
    pub min_num_inliers: usize,
    /// The essential matrix is trusted when its inliers are more than this share of the
    /// fundamental matrix's.
    pub min_e_f_inlier_ratio: f64,
    /// The homography explains the pair (planar or panoramic) when its inliers are more than
    /// this share of the epipolar model's.
    pub max_h_inlier_ratio: f64,
    /// A watermark: at least this share of the inliers lies in the border band of both photos,
    /// and at least this share of them moves by one and the same translation.
    pub watermark_min_inlier_ratio: f64,
    /// The border band, as a fraction of the diagonal of the photo.
    pub watermark_border_size: f64,
    /// The largest residual, in pixels, of an inlier of the watermark's translation.
    pub watermark_detection_max_error: f64,
    pub detect_watermark: bool,
    /// After RANSAC, each model is refined on its inliers by least squares on the Sampson error
    /// (the transfer error for the homography) and its inliers counted again; the refined model
    /// is kept when its support is not worse.
    pub use_sampson_refinement: bool,
    /// Recover cam2_from_cam1, and tell `Planar` from `Panoramic`.
    pub compute_relative_pose: bool,
}

impl Default for Options {
    fn default() -> Options {
        Options {
            ransac: ransac::Options::default(),
            min_num_inliers: 15,
            min_e_f_inlier_ratio: 0.95,
            max_h_inlier_ratio: 0.8,
            watermark_min_inlier_ratio: 0.7,
            watermark_border_size: 0.1,
            watermark_detection_max_error: 4.0,
            detect_watermark: true,
            use_sampson_refinement: true,
            compute_relative_pose: false,
        }
    }
}

/// The configuration of a pair, numbered as `pycolmap.TwoViewGeometryConfiguration` (the values
/// written in COLMAP's `two_view_geometries` table).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u32)]
pub enum Config {
    Undefined = 0,
    Degenerate = 1,
    Calibrated = 2,
    Uncalibrated = 3,
    Planar = 4,
    Panoramic = 5,
    PlanarOrPanoramic = 6,
    Watermark = 7,
}

impl Config {
    /// The configuration of COLMAP's number; `None` for the ones this module never gives
    /// (`MULTIPLE` 8, `CALIBRATED_RIG` 9) and for any other number.
    pub fn from_u32(n: u32) -> Option<Config> {
        Some(match n {
            0 => Config::Undefined,
            1 => Config::Degenerate,
            2 => Config::Calibrated,
            3 => Config::Uncalibrated,
            4 => Config::Planar,
            5 => Config::Panoramic,
            6 => Config::PlanarOrPanoramic,
            7 => Config::Watermark,
            _ => return None,
        })
    }
}

/// What the verification of a pair gives: the configuration, the inlier matches (pairs of key
/// point indices, in the order of the matches given), the models that were estimated (of unit
/// Frobenius norm; `e` only with a prior focal length) and, with `compute_relative_pose`, the
/// pose cam2_from_cam1 (`X2 = R X1 + t`; `t` of unit norm from an essential matrix, scaled by the
/// inverse distance of the plane from a homography, zero for a panorama).
#[derive(Clone, Debug, PartialEq)]
pub struct TwoView {
    pub config: Config,
    pub inliers: Vec<[u32; 2]>,
    pub e: Option<Matrix3<f64>>,
    pub f: Option<Matrix3<f64>>,
    pub h: Option<Matrix3<f64>>,
    pub cam2_from_cam1: Option<(UnitQuaternion<f64>, Vector3<f64>)>,
}

/// The seed of the RANSAC of a pair: FNV-1a 64 of the two photo names sorted (byte order) and
/// joined by a NUL byte, the same for the pair in either order, in every run, every database and
/// every thread.
pub fn pair_seed(a: &str, b: &str) -> u64 {
    let (first, second) = if a <= b { (a, b) } else { (b, a) };
    let mut hash: u64 = 0xcbf29ce484222325;
    for byte in first.bytes().chain(std::iter::once(0u8)).chain(second.bytes()) {
        hash ^= byte as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    hash
}

/// The essential matrix, on normalised coordinates: the five-point solver for the sample, the
/// same machinery on all the inliers for the local step (as COLMAP's LO-RANSAC of it).
#[derive(Clone, Debug)]
struct Essential(Matrix3<f64>);

impl Model for Essential {
    const MIN_SAMPLES: usize = 5;
    const MIN_LOCAL: usize = 5;

    fn from_sample(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Vec<Essential> {
        essential_n_point(x1, x2).into_iter().map(Essential).collect()
    }

    fn from_inliers(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Vec<Essential> {
        essential_n_point(x1, x2).into_iter().map(Essential).collect()
    }

    fn residual_sq(&self, a: [f64; 2], b: [f64; 2]) -> f64 {
        sampson_sq(&self.0, a, b)
    }
}

/// The fundamental matrix, in pixels: the seven-point solver for the sample, the eight-point
/// algorithm on the inliers.
#[derive(Clone, Debug)]
struct Fundamental(Matrix3<f64>);

impl Model for Fundamental {
    const MIN_SAMPLES: usize = 7;
    const MIN_LOCAL: usize = 8;

    fn from_sample(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Vec<Fundamental> {
        match (x1.try_into(), x2.try_into()) {
            (Ok(a), Ok(b)) => fundamental_seven_point(a, b).into_iter().map(Fundamental).collect(),
            _ => Vec::new(),
        }
    }

    fn from_inliers(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Vec<Fundamental> {
        fundamental_eight_point(x1, x2).into_iter().map(Fundamental).collect()
    }

    fn residual_sq(&self, a: [f64; 2], b: [f64; 2]) -> f64 {
        sampson_sq(&self.0, a, b)
    }
}

/// The homography, in pixels: the direct linear transform of four points and of the inliers.
#[derive(Clone, Debug)]
struct Homography(Matrix3<f64>);

impl Model for Homography {
    const MIN_SAMPLES: usize = 4;
    const MIN_LOCAL: usize = 4;

    fn from_sample(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Vec<Homography> {
        homography_dlt(x1, x2).into_iter().map(Homography).collect()
    }

    fn from_inliers(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Vec<Homography> {
        homography_dlt(x1, x2).into_iter().map(Homography).collect()
    }

    fn residual_sq(&self, a: [f64; 2], b: [f64; 2]) -> f64 {
        transfer_sq(&self.0, a, b)
    }
}

/// A translation of the plane (COLMAP's `TranslationTransformEstimator<2>`, the model of a
/// watermark): one match gives it, the mean over the inliers refines it.
#[derive(Clone, Debug)]
struct Shift([f64; 2]);

impl Model for Shift {
    const MIN_SAMPLES: usize = 1;
    const MIN_LOCAL: usize = 1;

    fn from_sample(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Vec<Shift> {
        Self::from_inliers(x1, x2)
    }

    fn from_inliers(x1: &[[f64; 2]], x2: &[[f64; 2]]) -> Vec<Shift> {
        if x1.is_empty() {
            return Vec::new();
        }
        let n = x1.len() as f64;
        let (dx, dy) = x1.iter().zip(x2).fold((0.0, 0.0), |(sx, sy), (a, b)| (sx + b[0] - a[0], sy + b[1] - a[1]));
        vec![Shift([dx / n, dy / n])]
    }

    fn residual_sq(&self, a: [f64; 2], b: [f64; 2]) -> f64 {
        (b[0] - a[0] - self.0[0]).powi(2) + (b[1] - a[1] - self.0[1]).powi(2)
    }
}

/// The verification of a pair: `kp1`, `kp2` the key points of the two photos (`[x, y, scale,
/// orientation]`, COLMAP pixels; only x and y are read), `matches` pairs of indices into them,
/// `quality` one value per match (lower is better; empty, the matches are taken in their order),
/// `calibrated` whether both cameras have a prior focal length (then the essential matrix is
/// estimated too, on `Camera::normalised` coordinates with the threshold scaled by the mean of
/// the two focal lengths, as COLMAP's `CamFromImgThreshold`), `seed` the seed of the RANSAC
/// (`pair_seed`; the fundamental, the homography and the watermark take the seeds after it). The
/// inliers come back in the order of `matches`. An index outside the key points is a programming
/// error (a panic), as is a `quality` of another length than `matches`.
#[allow(clippy::too_many_arguments)]
pub fn verify(
    cam1: &Camera,
    kp1: &[[f32; 4]],
    cam2: &Camera,
    kp2: &[[f32; 4]],
    matches: &[[u32; 2]],
    quality: &[f32],
    calibrated: bool,
    o: &Options,
    seed: u64,
) -> TwoView {
    assert!(quality.is_empty() || quality.len() == matches.len(), "one quality per match");
    let mut out =
        TwoView { config: Config::Degenerate, inliers: Vec::new(), e: None, f: None, h: None, cam2_from_cam1: None };
    // the matches best first (a stable sort: equal qualities keep their order)
    let mut order: Vec<usize> = (0..matches.len()).collect();
    if !quality.is_empty() {
        order.sort_by(|&i, &j| quality[i].total_cmp(&quality[j]));
    }
    let point = |kp: &[[f32; 4]], i: u32| [kp[i as usize][0] as f64, kp[i as usize][1] as f64];
    let x1: Vec<[f64; 2]> = order.iter().map(|&k| point(kp1, matches[k][0])).collect();
    let x2: Vec<[f64; 2]> = order.iter().map(|&k| point(kp2, matches[k][1])).collect();
    let normalised = |cam: &Camera, x: &[[f64; 2]]| -> Vec<[f64; 2]> {
        x.iter()
            .map(|p| {
                let (u, v) = cam.normalised(p[0], p[1]);
                [u, v]
            })
            .collect()
    };
    let (n1, n2) = if calibrated { (normalised(cam1, &x1), normalised(cam2, &x2)) } else { (Vec::new(), Vec::new()) };

    // the three models
    let e_report = if calibrated {
        let mut eo = o.ransac;
        eo.max_error = (o.ransac.max_error / cam1.focal() + o.ransac.max_error / cam2.focal()) / 2.0;
        lo_ransac::<Essential>(&n1, &n2, &eo, Sampling::Prosac, seed)
            .map(|r| if o.use_sampson_refinement { refine_essential(r, &n1, &n2, eo.max_error) } else { r })
    } else {
        None
    };
    let f_report = lo_ransac::<Fundamental>(&x1, &x2, &o.ransac, Sampling::Prosac, seed.wrapping_add(1))
        .map(|r| if o.use_sampson_refinement { refine_fundamental(r, &x1, &x2, o.ransac.max_error) } else { r });
    let h_report = lo_ransac::<Homography>(&x1, &x2, &o.ransac, Sampling::Prosac, seed.wrapping_add(2))
        .map(|r| if o.use_sampson_refinement { refine_homography(r, &x1, &x2, o.ransac.max_error) } else { r });
    out.e = e_report.as_ref().map(|r| r.model.0);
    out.f = f_report.as_ref().map(|r| r.model.0);
    out.h = h_report.as_ref().map(|r| r.model.0);
    let (ne, nf, nh) = (inliers_of(&e_report), inliers_of(&f_report), inliers_of(&h_report));
    let min = o.min_num_inliers;
    if (e_report.is_none() && f_report.is_none() && h_report.is_none()) || (ne < min && nf < min && nh < min) {
        return out;
    }

    // the decision: whose inliers, which configuration (the ratios over zero inliers are infinite
    // or not a number, as COLMAP's doubles)
    let e_f = ne as f64 / nf as f64;
    let h_f = nh as f64 / nf as f64;
    let h_e = nh as f64 / ne as f64;
    let n = matches.len();
    let mut mask: Vec<bool>;
    let mut config: Config;
    if calibrated {
        if e_report.is_some() && e_f > o.min_e_f_inlier_ratio && ne >= min {
            mask = mask_of(&e_report, n);
            if h_e > o.max_h_inlier_ratio {
                config = Config::PlanarOrPanoramic;
                if nh > ne {
                    mask = mask_of(&h_report, n);
                }
            } else {
                config = Config::Calibrated;
            }
        } else if f_report.is_some() && nf >= min {
            mask = mask_of(&f_report, n);
            if h_f > o.max_h_inlier_ratio {
                config = Config::PlanarOrPanoramic;
                if nh > nf {
                    mask = mask_of(&h_report, n);
                }
            } else {
                config = Config::Uncalibrated;
            }
        } else if h_report.is_some() && nh >= min {
            mask = mask_of(&h_report, n);
            config = Config::PlanarOrPanoramic;
        } else {
            return out;
        }
    } else {
        // without a prior focal length the inliers are the fundamental matrix's whatever the
        // configuration (COLMAP's uncalibrated estimation)
        mask = mask_of(&f_report, n);
        config = if h_f > o.max_h_inlier_ratio { Config::PlanarOrPanoramic } else { Config::Uncalibrated };
    }

    // the inliers back in the order of the matches
    let mut chosen = vec![false; matches.len()];
    for (k, &i) in order.iter().enumerate() {
        chosen[i] = mask[k];
    }
    out.inliers = matches.iter().zip(&chosen).filter(|(_, &m)| m).map(|(m, _)| *m).collect();

    if o.detect_watermark && is_watermark(cam1, cam2, &x1, &x2, &mask, o, seed.wrapping_add(3)) {
        config = Config::Watermark;
    }
    out.config = config;

    if o.compute_relative_pose {
        let i1: Vec<[f64; 2]> = out.inliers.iter().map(|m| point(kp1, m[0])).collect();
        let i2: Vec<[f64; 2]> = out.inliers.iter().map(|m| point(kp2, m[1])).collect();
        relative_pose(&mut out, cam1, &normalised(cam1, &i1), cam2, &normalised(cam2, &i2));
    }
    out
}

fn inliers_of<M>(report: &Option<Report<M>>) -> usize {
    report.as_ref().map_or(0, |r| r.support.num_inliers)
}

/// The inlier mask of a report, or no inlier at all (`n` flags) without one.
fn mask_of<M>(report: &Option<Report<M>>, n: usize) -> Vec<bool> {
    report.as_ref().map_or_else(|| vec![false; n], |r| r.inlier_mask.clone())
}

/// COLMAP's `DetectWatermark` on the inliers (`mask`) of the pair: at least
/// `watermark_min_inlier_ratio` of them lie in the border band (`watermark_border_size` of the
/// diagonal) in both photos, and a translation of the plane, found by RANSAC with
/// `watermark_detection_max_error`, fits at least that share of them.
fn is_watermark(cam1: &Camera, cam2: &Camera, x1: &[[f64; 2]], x2: &[[f64; 2]], mask: &[bool], o: &Options, seed: u64)
                -> bool {
    let band = |cam: &Camera| o.watermark_border_size * (cam.width * cam.width + cam.height * cam.height).sqrt();
    let inside = |p: [f64; 2], cam: &Camera, band: f64| {
        p[0] >= band && p[0] <= cam.width - band && p[1] >= band && p[1] <= cam.height - band
    };
    let (band1, band2) = (band(cam1), band(cam2));
    let (mut p1, mut p2) = (Vec::new(), Vec::new());
    let mut in_border = 0;
    for (i, &m) in mask.iter().enumerate() {
        if m {
            p1.push(x1[i]);
            p2.push(x2[i]);
            if !inside(x1[i], cam1, band1) && !inside(x2[i], cam2, band2) {
                in_border += 1;
            }
        }
    }
    let n = p1.len();
    if n == 0 || (in_border as f64 / n as f64) < o.watermark_min_inlier_ratio {
        return false;
    }
    let mut ro = o.ransac;
    ro.min_inlier_ratio = o.watermark_min_inlier_ratio;
    ro.max_error = o.watermark_detection_max_error;
    lo_ransac::<Shift>(&p1, &p2, &ro, Sampling::Uniform, seed)
        .is_some_and(|r| r.support.num_inliers as f64 / n as f64 >= o.watermark_min_inlier_ratio)
}

/// COLMAP's `EstimateTwoViewGeometryPose` on the inliers (normalised coordinates): the pose of
/// the essential matrix for a calibrated or uncalibrated pair (none when it has no essential
/// matrix, or no point in front of both cameras), the pose of the homography for a planar or
/// panoramic one, which then becomes `Panoramic` when the translation is zero (the homography is
/// a rotation) and `Planar` otherwise. A pose that is not finite is none.
fn relative_pose(out: &mut TwoView, cam1: &Camera, n1: &[[f64; 2]], cam2: &Camera, n2: &[[f64; 2]]) {
    let quaternion = |r: Matrix3<f64>| UnitQuaternion::from_rotation_matrix(&Rotation3::from_matrix_unchecked(r));
    let finite =
        |r: &Matrix3<f64>, t: &Vector3<f64>| r.iter().all(|v| v.is_finite()) && t.iter().all(|v| v.is_finite());
    match out.config {
        Config::Calibrated | Config::Uncalibrated => {
            if let Some(e) = &out.e {
                out.cam2_from_cam1 = pose_from_essential(e, n1, n2)
                    .filter(|(r, t, _)| finite(r, t))
                    .map(|(r, t, _)| (quaternion(r), t));
            }
        }
        Config::PlanarOrPanoramic => {
            if let Some(h) = &out.h {
                let (r, t) = pose_from_homography(h, cam1, cam2, n1, n2);
                out.config = if t.norm() == 0.0 { Config::Panoramic } else { Config::Planar };
                if finite(&r, &t) {
                    out.cam2_from_cam1 = Some((quaternion(r), t));
                }
            }
        }
        _ => {}
    }
}

/// COLMAP's `PoseFromHomographyMatrix`: of the poses the decomposition of `h` gives, the one
/// with the most points in front of both cameras (a later one with as many wins).
fn pose_from_homography(h: &Matrix3<f64>, cam1: &Camera, cam2: &Camera, x1: &[[f64; 2]], x2: &[[f64; 2]])
                        -> (Matrix3<f64>, Vector3<f64>) {
    let calibration = |c: &Camera| Matrix3::new(c.fx, 0.0, c.cx, 0.0, c.fy, c.cy, 0.0, 0.0, 1.0);
    let mut best: Option<(Matrix3<f64>, Vector3<f64>, usize)> = None;
    for (r, t, _) in decompose_homography(h, &calibration(cam1), &calibration(cam2)) {
        let count = points_in_front(&r, &t, x1, x2);
        if best.is_none_or(|(_, _, n)| count >= n) {
            best = Some((r, t, count));
        }
    }
    let (r, t, _) = best.expect("the decomposition gives at least one pose");
    (r, t)
}

/// COLMAP's `DecomposeHomographyMatrix` (Malis and Vargas 2007, the analytical decomposition):
/// the calibration removed (`K2^-1 H K1`), the scale removed by the middle singular value and
/// the sign set for a rotation; a matrix that is a rotation to 1e-3 (`H^T H - I` in the infinity
/// norm) is a pure rotation, with a zero translation and normal; otherwise the four solutions
/// `(R, t, n)` with `H = R + t n^T` for each, `t` scaled by the inverse distance of the plane and
/// `n` its unit normal in the frame of the first camera.
fn decompose_homography(h: &Matrix3<f64>, k1: &Matrix3<f64>, k2: &Matrix3<f64>)
                        -> Vec<(Matrix3<f64>, Vector3<f64>, Vector3<f64>)> {
    let k2_inverse = k2.try_inverse().unwrap_or_else(Matrix3::identity);
    let mut hn = k2_inverse * h * k1;
    let middle = jacobi_svd(&dynamic(&hn)).s[1];
    if middle > 0.0 {
        hn /= middle;
    }
    if hn.determinant() < 0.0 {
        hn = -hn;
    }
    let s = hn.transpose() * hn - Matrix3::identity();
    if s.iter().fold(0.0f64, |m, v| m.max(v.abs())) < 1e-3 {
        return vec![(hn, Vector3::zeros(), Vector3::zeros())];
    }
    // the opposite of the minor of `s` at (row, col)
    let minor = |row: usize, col: usize| -> f64 {
        let col1 = if col == 0 { 1 } else { 0 };
        let col2 = if col == 2 { 1 } else { 2 };
        let row1 = if row == 0 { 1 } else { 0 };
        let row2 = if row == 2 { 1 } else { 2 };
        s[(row1, col2)] * s[(row2, col1)] - s[(row1, col1)] * s[(row2, col2)]
    };
    let sign = |v: f64| if v > 0.0 { 1.0 } else if v < 0.0 { -1.0 } else { 0.0 };
    let (m00, m11, m22) = (minor(0, 0), minor(1, 1), minor(2, 2));
    let (rt_m00, rt_m11, rt_m22) = (m00.max(0.0).sqrt(), m11.max(0.0).sqrt(), m22.max(0.0).sqrt());
    let (e12, e02, e01) = (sign(minor(1, 2)), sign(minor(0, 2)), sign(minor(0, 1)));
    let diagonal = [s[(0, 0)].abs(), s[(1, 1)].abs(), s[(2, 2)].abs()];
    let idx = (0..3).max_by(|&i, &j| diagonal[i].total_cmp(&diagonal[j])).expect("three entries");
    let (np1, np2) = match idx {
        0 => (
            Vector3::new(s[(0, 0)], s[(0, 1)] + rt_m22, s[(0, 2)] + e12 * rt_m11),
            Vector3::new(s[(0, 0)], s[(0, 1)] - rt_m22, s[(0, 2)] - e12 * rt_m11),
        ),
        1 => (
            Vector3::new(s[(0, 1)] + rt_m22, s[(1, 1)], s[(1, 2)] - e02 * rt_m00),
            Vector3::new(s[(0, 1)] - rt_m22, s[(1, 1)], s[(1, 2)] + e02 * rt_m00),
        ),
        _ => (
            Vector3::new(s[(0, 2)] + e01 * rt_m11, s[(1, 2)] + rt_m00, s[(2, 2)]),
            Vector3::new(s[(0, 2)] - e01 * rt_m11, s[(1, 2)] - rt_m00, s[(2, 2)]),
        ),
    };
    let trace = s.trace();
    let v = 2.0 * (1.0 + trace - m00 - m11 - m22).max(0.0).sqrt();
    let e_sii = sign(s[(idx, idx)]);
    let r = (2.0 + trace + v).max(0.0).sqrt();
    let n_t = (2.0 + trace - v).max(0.0).sqrt();
    let n1 = np1.normalize();
    let n2 = np2.normalize();
    let half_nt = 0.5 * n_t;
    let esii_t_r = e_sii * r;
    let t1_star = half_nt * (esii_t_r * n2 - n_t * n1);
    let t2_star = half_nt * (esii_t_r * n1 - n_t * n2);
    let rotation =
        |t_star: &Vector3<f64>, n: &Vector3<f64>| hn * (Matrix3::identity() - (2.0 / v) * t_star * n.transpose());
    let r1 = rotation(&t1_star, &n1);
    let t1 = r1 * t1_star;
    let r2 = rotation(&t2_star, &n2);
    let t2 = r2 * t2_star;
    vec![(r1, t1, n1), (r1, -t1, -n1), (r2, t2, n2), (r2, -t2, -n2)]
}

// ------------------------------------------------------------------- the refinement on the inliers

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
fn exp_so3(w: &Vector3<f64>) -> Matrix3<f64> {
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
/// normalised coordinates.
#[derive(Clone)]
struct EssentialPose {
    r: Matrix3<f64>,
    t: Vector3<f64>,
    b1: Vector3<f64>,
    b2: Vector3<f64>,
}

impl EssentialPose {
    fn new(r: Matrix3<f64>, t: Vector3<f64>) -> EssentialPose {
        let t = t.normalize();
        let (b1, b2) = tangent_basis(&t);
        EssentialPose { r, t, b1, b2 }
    }

    fn essential(&self) -> Matrix3<f64> {
        Matrix3::new(0.0, -self.t.z, self.t.y, self.t.z, 0.0, -self.t.x, -self.t.y, self.t.x, 0.0) * self.r
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
        out[0] = sampson_signed(&self.essential(), a, b);
    }
}

/// The fundamental matrix as `U diag(1, s, 0) V^T`, `U` and `V` rotations moved on SO(3) and `s`
/// free (seven parameters, the rank kept at two); the residual is the Sampson error in pixels.
#[derive(Clone)]
struct FundamentalFactors {
    u: Matrix3<f64>,
    v: Matrix3<f64>,
    s: f64,
}

impl FundamentalFactors {
    /// `None` when `f` has rank below two or is not finite.
    fn of(f: &Matrix3<f64>) -> Option<FundamentalFactors> {
        if !f.iter().all(|v| v.is_finite()) {
            return None;
        }
        let svd = jacobi_svd(&dynamic(f));
        let s = &svd.s;
        if !(s[0] > 0.0 && s[1] > 1e-10 * s[0]) {
            return None;
        }
        let column = |m: &DMatrix<f64>, k: usize, scale: f64| Vector3::new(m[(0, k)], m[(1, k)], m[(2, k)]) / scale;
        let (u1, u2) = (column(&svd.av, 0, s[0]), column(&svd.av, 1, s[1]));
        let (v1, v2) = (column(&svd.v, 0, 1.0), column(&svd.v, 1, 1.0));
        let u = Matrix3::from_columns(&[u1, u2, u1.cross(&u2)]);
        let v = Matrix3::from_columns(&[v1, v2, v1.cross(&v2)]);
        Some(FundamentalFactors { u, v, s: s[1] / s[0] })
    }

    fn fundamental(&self) -> Matrix3<f64> {
        self.u * Matrix3::from_diagonal(&Vector3::new(1.0, self.s, 0.0)) * self.v.transpose()
    }
}

impl Refinable for FundamentalFactors {
    const PARAMS: usize = 7;
    const RESIDUALS: usize = 1;

    fn perturbed(&self, d: &[f64]) -> FundamentalFactors {
        FundamentalFactors {
            u: exp_so3(&Vector3::new(d[0], d[1], d[2])) * self.u,
            v: exp_so3(&Vector3::new(d[3], d[4], d[5])) * self.v,
            s: self.s + d[6],
        }
    }

    fn residuals(&self, a: [f64; 2], b: [f64; 2], out: &mut [f64]) {
        out[0] = sampson_signed(&self.fundamental(), a, b);
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
fn refine_essential(report: Report<Essential>, x1: &[[f64; 2]], x2: &[[f64; 2]], max_error: f64) -> Report<Essential> {
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
fn refine_fundamental(report: Report<Fundamental>, x1: &[[f64; 2]], x2: &[[f64; 2]], max_error: f64)
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
fn refine_homography(report: Report<Homography>, x1: &[[f64; 2]], x2: &[[f64; 2]], max_error: f64)
                     -> Report<Homography> {
    let (i1, i2) = selected(&report.inlier_mask, x1, x2);
    let Some(entries) = HomographyEntries::of(&report.model.0) else {
        return report;
    };
    let refined = levenberg_marquardt(&entries, &i1, &i2);
    kept_if_not_worse(report, Homography(refined.h), x1, x2, max_error)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rotation(axis: Vector3<f64>, angle: f64) -> Matrix3<f64> {
        exp_so3(&(axis.normalize() * angle))
    }

    #[test]
    fn the_homography_of_a_plane_decomposes_into_its_pose() {
        // H = K (R + t n^T / d) K^-1 for a few poses and planes: one of the four solutions is the
        // truth, with t scaled by 1 / d and n the normal
        let k = Matrix3::new(1000.0, 0.0, 1000.0, 0.0, 1000.0, 750.0, 0.0, 0.0, 1.0);
        let cases = [
            (Vector3::new(0.2, 1.0, 0.1), 0.2, Vector3::new(0.8, 0.1, 0.05), Vector3::new(-0.3, 0.2, 1.0), 4.0),
            (Vector3::new(1.0, -0.5, 0.3), 0.35, Vector3::new(-0.5, 0.4, 0.2), Vector3::new(0.1, -0.1, 1.0), 2.5),
            (Vector3::new(0.3, 0.2, 1.0), 0.05, Vector3::new(0.1, -0.6, 0.3), Vector3::new(0.2, 0.1, 1.0), 3.0),
        ];
        for (axis, angle, t, n, d) in cases {
            let r = rotation(axis, angle);
            let n = n.normalize();
            let h = k * (r + t * n.transpose() / d) * k.try_inverse().unwrap();
            let solutions = decompose_homography(&h, &k, &k);
            assert_eq!(solutions.len(), 4);
            let closest = solutions
                .iter()
                .map(|(rs, ts, ns)| (rs - r).norm() + (ts - t / d).norm() + (ns - n).norm())
                .fold(f64::INFINITY, f64::min);
            assert!(closest < 1e-9, "the truth is not among the solutions: {closest}");
            for (rs, _, _) in &solutions {
                assert!((rs * rs.transpose() - Matrix3::identity()).norm() < 1e-9 && rs.determinant() > 0.0);
            }
        }
        // a pure rotation is one solution with no translation
        let r = rotation(Vector3::new(0.1, 1.0, 0.0), 0.15);
        let solutions = decompose_homography(&(k * r * k.try_inverse().unwrap() * 3.0), &k, &k);
        assert_eq!(solutions.len(), 1);
        assert!((solutions[0].0 - r).norm() < 1e-9 && solutions[0].1 == Vector3::zeros());
    }

    #[test]
    fn the_refinement_lowers_the_residuals_of_a_homography() {
        // the identity moved by a translation, started from a matrix off by a per cent
        let truth = Matrix3::new(1.0, 0.0, 5.0, 0.0, 1.0, -3.0, 0.0, 0.0, 1.0);
        let x1: Vec<[f64; 2]> = (0..50).map(|i| [37.0 * (i % 7) as f64 + 11.0, 23.0 * (i % 11) as f64 + 5.0]).collect();
        let x2: Vec<[f64; 2]> = x1.iter().map(|p| [p[0] + 5.0, p[1] - 3.0]).collect();
        let off = Matrix3::new(0.01, 0.0, 1.0, 0.0, -0.01, 0.5, 0.0, 0.0, 0.0);
        let start = HomographyEntries::of(&(truth + off)).unwrap();
        let refined = levenberg_marquardt(&start, &x1, &x2);
        let error = |h: &Matrix3<f64>| x1.iter().zip(&x2).map(|(&a, &b)| transfer_sq(h, a, b)).sum::<f64>();
        assert!(error(&refined.h) < 1e-12 * error(&start.h), "{} against {}", error(&refined.h), error(&start.h));
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
