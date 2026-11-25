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

/// `filter` as COLMAP's retriangulation filters (`IncrementalMapper::FilterPoints`, the filter of
/// the last adjustment of its global mapper): an observation goes also when its reprojection error
/// in pixels exceeds `o.final_max_reprojection_px`, and the observations of a point go when no two
/// of their rays meet at `o.final_min_triangulation_deg` or more. The observations of a camera of
/// `held` (one flag per camera of `input`; missing: not held) are not measured in pixels: its
/// intrinsics were not adjusted (a block that does not determine them, ruling R16), so its errors
/// hold what its lens model lacks, the observations near the corners most, and dropping them would
/// take from a later calibration the observations that tell the lens. COLMAP's filter of 4 px
/// follows an adjustment that refines the lens. `Err` as `filter`.
#[allow(clippy::too_many_arguments)]
pub fn filter_in_pixels(
    t: &Tracks,
    points: &[Option<[f64; 3]>],
    input: &Input,
    cams: &[Camera],
    poses: &[Option<Pose>],
    o: &Options,
    held: &[bool],
) -> Result<Filtered, String> {
    let max_px: Vec<f64> = (0..cams.len())
        .map(|c| if held.get(c).copied().unwrap_or(false) { f64::INFINITY } else { o.final_max_reprojection_px })
        .collect();
    filter_by(t, points, input, cams, poses, o.max_normalized_reprojection, Some(&max_px),
              o.final_min_triangulation_deg)
}

/// `filter` with its thresholds: the normalised error, the error in pixels per camera (`None`:
/// not measured) and the angle in degrees.
#[allow(clippy::too_many_arguments)]
fn filter_by(
    t: &Tracks,
    points: &[Option<[f64; 3]>],
    input: &Input,
    cams: &[Camera],
    poses: &[Option<Pose>],
    max_normalized: f64,
    max_px: Option<&[f64]>,
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
            let in_pixels = max_px.is_none_or(|max| {
                let (pu, pv) = cam.pixel(x / z, y / z);
                max[c] == f64::INFINITY || (pu - px[0]).hypot(pv - px[1]) <= max[c]
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
