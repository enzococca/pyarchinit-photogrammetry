//! The points of the tracks: the point nearest a set of rays in least squares (`point`), and the
//! filter of the observations of the triangulated points (`filter`), arranged as COLMAP's
//! `ObservationManager` filters them in its global mapper (`src/colmap/sfm/observation_manager.cc`,
//! `FilterPoints3DWithLargeReprojectionError` with the normalised error, then
//! `FilterPoints3DWithSmallTriangulationAngle`; the angle of two rays as
//! `src/colmap/geometry/triangulation.cc`, `CalculateTriangulationAngle`).

use nalgebra::{Matrix3, Vector3};

use super::graph::check_cameras;
use super::input::Input;
use super::tracks::Tracks;
use super::Options;
use crate::camera::{transform, Camera, Pose};

/// Rays whose directions differ by less than this (the sine of their angle) are parallel.
pub const PARALLEL: f64 = 1e-12;
/// A point at this depth or less in front of a camera does not project into it (COLMAP's
/// `kMinDepth` of the normalised error).
const MIN_DEPTH: f64 = 1e-12;

/// The point nearest the rays `(origin, direction)` in least squares (the sum of its squared
/// distances to the lines of the rays), solved about the mean of the origins. `None` with fewer
/// than two rays, a direction of zero length or not finite, rays all parallel to the first within
/// `PARALLEL`, or a system that cannot be solved.
pub fn point(rays: &[([f64; 3], [f64; 3])]) -> Option<[f64; 3]> {
    if rays.len() < 2 {
        return None;
    }
    let mut mean = Vector3::zeros();
    for (o, _) in rays {
        mean += Vector3::from(*o);
    }
    mean /= rays.len() as f64;
    let mut directions = Vec::with_capacity(rays.len());
    for (_, d) in rays {
        let d = Vector3::from(*d);
        let norm = d.norm();
        if !(norm > 0.0 && norm.is_finite()) {
            return None;
        }
        directions.push(d / norm);
    }
    if directions.iter().all(|d| d.cross(&directions[0]).norm() <= PARALLEL) {
        return None;
    }
    let mut a = Matrix3::zeros();
    let mut b = Vector3::zeros();
    for ((o, _), d) in rays.iter().zip(&directions) {
        let m = Matrix3::identity() - d * d.transpose();
        a += m;
        b += m * (Vector3::from(*o) - mean);
    }
    let x = a.lu().solve(&b)? + mean;
    x.iter().all(|v| v.is_finite()).then(|| [x[0], x[1], x[2]])
}

/// What `filter` kept: one flag per observation of the tracks, and the observations that went for
/// their reprojection error (with the others of their point when fewer than two were left), for the
/// angle of their point's rays, and because their track has no point.
#[derive(Clone, Debug, PartialEq)]
pub struct Filtered {
    pub keep: Vec<bool>,
    pub by_reprojection: usize,
    pub by_angle: usize,
    pub no_point: usize,
}

/// The centre of the camera of `cam_from_world`.
fn centre(m: &Pose) -> Vector3<f64> {
    let r = Matrix3::from_fn(|i, j| m[i][j]);
    -(r.transpose() * Vector3::new(m[0][3], m[1][3], m[2][3]))
}

/// The angle of the rays from `a` and `b` meeting at `x`, in radians, the smaller of it and its
/// supplement (COLMAP's `CalculateTriangulationAngle`).
fn ray_angle(a: &Vector3<f64>, b: &Vector3<f64>, x: &Vector3<f64>) -> f64 {
    let (u, v) = (x - a, x - b);
    let angle = u.cross(&v).norm().atan2(u.dot(&v));
    angle.min(std::f64::consts::PI - angle)
}

/// The observations of the tracks kept with the points `points` (one per track, `None` when the
/// track has none) seen by the photos at `poses` (`cam_from_world`, one per photo of `input`,
/// `None` for a photo without a pose) through the cameras `cams` (one per camera of `input`):
///
/// - an observation goes when its reprojection error, in normalised coordinates, exceeds
///   `o.max_normalized_reprojection`, when the point is not in front of its camera or when its
///   photo has no pose; when fewer than two observations of a point are left, all of them go;
/// - the observations left go, all of them, when no two of their rays meet at
///   `o.min_triangulation_deg` or more;
/// - the observations of a track without a point go.
///
/// `Err` when the tracks do not fit `input` (`Tracks::check`), when `points`, `poses` or `cams`
/// have another length than the tracks, the photos or the cameras of `input`, or when a camera
/// does not match its entry of `input`.
pub fn filter(
    t: &Tracks,
    points: &[Option<[f64; 3]>],
    input: &Input,
    cams: &[Camera],
    poses: &[Option<Pose>],
    o: &Options,
) -> Result<Filtered, String> {
    filter_by(t, points, input, cams, poses, o.max_normalized_reprojection, None, o.min_triangulation_deg)
}

/// The test in pixels of the observations of a camera in `filter_in_pixels`: the largest error,
/// after a radial correction `radial` = `(k1, k2)` of the projection (the pixel moved by
/// `f (u, v) (k1 r^2 + k2 r^4)` from where the camera puts the point `(u, v)` of the camera frame,
/// `r^2 = u^2 + v^2`; zero for a camera whose radial terms were adjusted).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PixelTest {
    pub max_px: f64,
    pub radial: [f64; 2],
}

/// `filter` as COLMAP filters after its retriangulation (`IncrementalMapper::FilterPoints` with the
/// C++ defaults of `IncrementalMapper::Options`, which the retriangulation of its global mapper
/// uses): an observation goes also when its reprojection error in pixels, after the radial
/// correction of its camera, exceeds the `PixelTest::max_px` of its camera (`tests`, one per camera
/// of `input`; see `pixel_tests`), and the observations of a point go when no two of their rays
/// meet at `o.final_min_triangulation_deg` or more. `Err` as `filter`, or when `tests` has another
/// length than the cameras.
#[allow(clippy::too_many_arguments)]
pub fn filter_in_pixels(
    t: &Tracks,
    points: &[Option<[f64; 3]>],
    input: &Input,
    cams: &[Camera],
    poses: &[Option<Pose>],
    o: &Options,
    tests: &[PixelTest],
) -> Result<Filtered, String> {
    if tests.len() != cams.len() {
        return Err(format!("{} tests in pixels for {} cameras", tests.len(), cams.len()));
    }
    filter_by(t, points, input, cams, poses, o.max_normalized_reprojection, Some(tests),
              o.final_min_triangulation_deg)
}

/// The robust median absolute deviation scale `1.4826 MAD` of `values` (sorted in place) about
/// their median.
fn mad_scale(values: &mut [f64]) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let median = |v: &mut [f64]| {
        v.sort_by(f64::total_cmp);
        let n = v.len();
        if n % 2 == 1 { v[n / 2] } else { 0.5 * (v[n / 2 - 1] + v[n / 2]) }
    };
    let m = median(values);
    let mut dev: Vec<f64> = values.iter().map(|x| (x - m).abs()).collect();
    1.4826 * median(&mut dev)
}

/// Fewer observations of a camera than this leave its radial correction at zero.
pub const MIN_CORRECTION_OBSERVATIONS: usize = 20;

/// The test in pixels of each camera (ruling R-B1): a camera of `adjusted` (its radial terms
/// adjusted by the rounds) has COLMAP's `o.final_max_reprojection_px` and no correction. Any other
/// camera - its radial terms held, so that its errors carry the distortion its model lacks, the
/// corners most - is never spared: a radial correction `(k1, k2)` is fitted to the errors of its
/// observations (those within the normalised threshold, the points and the poses held, nothing
/// written back) by iteratively reweighted least squares under Huber's loss at 1.345 times the
/// robust scale of the errors, and its threshold is the larger of `o.final_max_reprojection_px`
/// and four times the robust scale (`1.4826 MAD`) of the corrected errors. A model without radial
/// terms (SIMPLE_PINHOLE, PINHOLE) is never adjusted in them, so its cameras always have the
/// correction fitted: it then stands for the distortion the model cannot hold at all.
pub fn pixel_tests(
    t: &Tracks,
    points: &[Option<[f64; 3]>],
    input: &Input,
    cams: &[Camera],
    poses: &[Option<Pose>],
    o: &Options,
    adjusted: &[bool],
) -> Vec<PixelTest> {
    let plain = PixelTest { max_px: o.final_max_reprojection_px, radial: [0.0; 2] };
    // per camera: the pixel offset of the projection from the principal point (f (u, v)), r^2 and
    // the error (key point minus projection)
    let mut samples: Vec<Vec<([f64; 2], f64, [f64; 2])>> = vec![Vec::new(); cams.len()];
    for (track, p) in points.iter().enumerate() {
        let Some(p) = p else { continue };
        for i in t.offsets[track]..t.offsets[track + 1] {
            let (image, k) = t.observations[i];
            let image = image as usize;
            let c = input.images[image].camera;
            if adjusted.get(c).copied().unwrap_or(false) {
                continue;
            }
            let Some(pose) = &poses[image] else { continue };
            let (x, y, z) = transform(pose, p[0], p[1], p[2]);
            if !(z >= MIN_DEPTH) {
                continue;
            }
            let (pu, pv) = (x / z, y / z);
            let px = input.keypoint(image, k);
            let cam = &cams[c];
            let (u, v) = cam.normalised(px[0], px[1]);
            if (pu - u).hypot(pv - v) > o.max_normalized_reprojection {
                continue;
            }
            let (qu, qv) = cam.pixel(pu, pv);
            let f = cam.focal();
            samples[c].push(([f * pu, f * pv], pu * pu + pv * pv, [px[0] - qu, px[1] - qv]));
        }
    }
    (0..cams.len())
        .map(|c| {
            if adjusted.get(c).copied().unwrap_or(false) || samples[c].len() < MIN_CORRECTION_OBSERVATIONS {
                return plain;
            }
            let s = &samples[c];
            let corrected = |k: [f64; 2]| -> Vec<f64> {
                s.iter()
                    .map(|(d, r2, e)| {
                        let g = k[0] * r2 + k[1] * r2 * r2;
                        (e[0] - d[0] * g).hypot(e[1] - d[1] * g)
                    })
                    .collect()
            };
            let mut k = [0.0; 2];
            for _ in 0..10 {
                let errors = corrected(k);
                let scale = mad_scale(&mut errors.clone()).max(1e-6);
                let (mut a, mut b) = ([[0.0; 2]; 2], [0.0; 2]);
                for ((d, r2, e), err) in s.iter().zip(&errors) {
                    let w = if *err <= 1.345 * scale { 1.0 } else { 1.345 * scale / err };
                    let basis = [*r2, r2 * r2];
                    for axis in 0..2 {
                        let row = [d[axis] * basis[0], d[axis] * basis[1]];
                        for i in 0..2 {
                            b[i] += w * row[i] * e[axis];
                            for j in 0..2 {
                                a[i][j] += w * row[i] * row[j];
                            }
                        }
                    }
                }
                let det = a[0][0] * a[1][1] - a[0][1] * a[1][0];
                if !(det.abs() > 1e-12 * (a[0][0] * a[1][1]).abs().max(f64::MIN_POSITIVE)) {
                    // the two terms not told apart (a narrow photo): k1 alone
                    k = if a[0][0] > 0.0 { [b[0] / a[0][0], 0.0] } else { [0.0; 2] };
                    continue;
                }
                k = [(b[0] * a[1][1] - b[1] * a[0][1]) / det, (a[0][0] * b[1] - a[1][0] * b[0]) / det];
            }
            if !k.iter().all(|x| x.is_finite()) {
                return plain;
            }
            let scale = mad_scale(&mut corrected(k));
            PixelTest { max_px: o.final_max_reprojection_px.max(4.0 * scale), radial: k }
        })
        .collect()
}

/// `filter` with its thresholds: the normalised error, the test in pixels per camera (`None`:
/// none) and the angle in degrees.
#[allow(clippy::too_many_arguments)]
fn filter_by(
    t: &Tracks,
    points: &[Option<[f64; 3]>],
    input: &Input,
    cams: &[Camera],
    poses: &[Option<Pose>],
    max_normalized: f64,
    tests: Option<&[PixelTest]>,
    min_deg: f64,
) -> Result<Filtered, String> {
    t.check(input)?;
    if points.len() != t.len() {
        return Err(format!("{} points for {} tracks", points.len(), t.len()));
    }
    if poses.len() != input.images.len() {
        return Err(format!("{} poses for {} images", poses.len(), input.images.len()));
    }
    check_cameras(input, cams)?;
    let centres: Vec<Option<Vector3<f64>>> = poses.iter().map(|p| p.as_ref().map(centre)).collect();
    let min_angle = min_deg.to_radians();
    let mut out = Filtered { keep: vec![false; t.observations.len()], by_reprojection: 0, by_angle: 0, no_point: 0 };
    let mut left = Vec::new();
    for (track, p) in points.iter().enumerate() {
        let range = t.offsets[track]..t.offsets[track + 1];
        let Some(p) = p else {
            out.no_point += range.len();
            continue;
        };
        left.clear();
        for i in range.clone() {
            let (image, k) = t.observations[i];
            let image = image as usize;
            let Some(pose) = &poses[image] else {
                continue;
            };
            let (x, y, z) = transform(pose, p[0], p[1], p[2]);
            if !(z >= MIN_DEPTH) {
                continue;
            }
            let px = input.keypoint(image, k);
            let c = input.images[image].camera;
            let cam = &cams[c];
            let (u, v) = cam.normalised(px[0], px[1]);
            let error = (x / z - u).hypot(y / z - v);
            let in_pixels = tests.is_none_or(|tests| {
                let test = &tests[c];
                let (nu, nv) = (x / z, y / z);
                let (pu, pv) = cam.pixel(nu, nv);
                let r2 = nu * nu + nv * nv;
                let g = cam.focal() * (test.radial[0] * r2 + test.radial[1] * r2 * r2);
                (pu + nu * g - px[0]).hypot(pv + nv * g - px[1]) <= test.max_px
            });
            if error <= max_normalized && in_pixels {
                left.push(i);
            }
        }
        if left.len() < 2 {
            out.by_reprojection += range.len();
            continue;
        }
        out.by_reprojection += range.len() - left.len();
        let x = Vector3::from(*p);
        let wide = (1..left.len()).any(|j| {
            let a = centres[t.observations[left[j]].0 as usize].expect("a photo with a pose");
            (0..j).any(|i| {
                let b = centres[t.observations[left[i]].0 as usize].expect("a photo with a pose");
                ray_angle(&a, &b, &x) >= min_angle
            })
        });
        if !wide {
            out.by_angle += left.len();
            continue;
        }
        for &i in &left {
            out.keep[i] = true;
        }
    }
    Ok(out)
}
