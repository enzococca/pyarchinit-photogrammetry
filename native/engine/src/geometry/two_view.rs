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
//! configuration is `Planar` or `Panoramic`). The refinement of each model on its inliers is
//! `refine`.
//!
//! Key points are in COLMAP pixels (the centre of the first pixel at 0.5, 0.5); the matches are
//! pairs of indices into them with a quality each (the ratio of the two best descriptor
//! distances, lower is better), which orders the PROSAC sampling. The RANSAC is seeded by the
//! caller (`pair_seed` of the two photo names): the same pair gives the same result in every
//! run, every database and every thread.

use nalgebra::{Matrix3, Rotation3, UnitQuaternion, Vector3};

use super::linalg::{dynamic, jacobi_svd};
use super::ransac::{self, lo_ransac, Model, Report, Sampling, Translation};
use super::refine::{refine_essential, refine_fundamental, refine_homography};
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
pub(crate) struct Essential(pub(crate) Matrix3<f64>);

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
pub(crate) struct Fundamental(pub(crate) Matrix3<f64>);

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
pub(crate) struct Homography(pub(crate) Matrix3<f64>);

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

/// The verification of a pair: `kp1`, `kp2` the key points of the two photos (`[x, y, scale,
/// orientation]`, COLMAP pixels; only x and y are read), `matches` pairs of indices into them,
/// `quality` one value per match (lower is better; empty, the matches are taken in their order),
/// `calibrated` whether both cameras have a prior focal length (then the essential matrix is
/// estimated too, on `Camera::normalised` coordinates with the threshold scaled by the mean of
/// the two focal lengths, as COLMAP's `CamFromImgThreshold`), `seed` the seed of the RANSAC
/// (`pair_seed`; the fundamental, the homography and the watermark take the seeds after it). Fewer
/// matches than `min_num_inliers` are `Degenerate` at once, with no model estimated. The inliers
/// come back in the order of `matches`. An index outside the key points is a programming error
/// (a panic), as is a `quality` of another length than `matches`.
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
    // fewer matches than inliers wanted: degenerate at once, no model estimated (COLMAP's early return)
    if matches.len() < o.min_num_inliers {
        return out;
    }
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
    lo_ransac::<Translation>(&p1, &p2, &ro, Sampling::Uniform, seed)
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

#[cfg(test)]
mod tests {
    use super::super::refine::exp_so3;
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
}
