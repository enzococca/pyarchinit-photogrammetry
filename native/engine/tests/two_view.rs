//! The geometric verification of a pair (`geometry::two_view`, on `geometry::ransac`) on random
//! scenes of two pinhole cameras of 2000 x 1500 pixels with a focal length of 1000: 300 matches
//! with 0.5 px of noise, 30 % of them outliers built at least `OUTLIER_DISTANCE` away from where
//! the true geometry would put them. The tests ask for no outlier among the inliers, which holds
//! only for outliers that far: within the 4 px the threshold leaves the inliers, a model can tilt
//! the epipolar lines (the bas-relief ambiguity) by one or two pixels at the inliers and by tens
//! of pixels far along the lines, where a random outlier is then counted, and the inlier count
//! prefers that model; COLMAP's verification absorbs such outliers too. The key points of the
//! second photo are shuffled and the matches given in a random order with a quality of their
//! own, so that the inliers come back as pairs of indices of the input, whatever the order of the
//! sampling.

use nalgebra::{Matrix3, Vector3};
use ppg_engine::camera::Camera;
use ppg_engine::geometry::ransac::{self, lo_ransac, Lcg, Sampling, Translation};
use ppg_engine::geometry::solvers::{sampson_sq, transfer_sq};
use ppg_engine::geometry::two_view::{self, pair_seed, verify, Config, TwoView};

const FOCAL: f64 = 1000.0;
const CX: f64 = 1000.0;
const CY: f64 = 750.0;
const WIDTH: f64 = 2000.0;
const HEIGHT: f64 = 1500.0;
const N_MATCHES: usize = 300;
const OUTLIER_SHARE: f64 = 0.3;
/// An outlier is at least this far, in pixels (the Sampson or the transfer error), from where
/// the true geometry puts it: beyond the reach of any model that keeps the inliers (module doc).
const OUTLIER_DISTANCE: f64 = 100.0;
const SCENES: u64 = 6;
/// The plane of the planar scenes, `n . X = d` in the frame of the first camera.
const PLANE_NORMAL: [f64; 3] = [-0.3, 0.2, 1.0];
const PLANE_D: f64 = 4.0;

/// Knuth's MMIX linear congruential generator, written here so that the scenes depend on no
/// crate and on no version of one (as in ppg-bench).
struct Random(u64);

impl Random {
    fn bits(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        self.0
    }

    /// A value in [0, 1): the upper 53 bits, as many as an f64 holds.
    fn unit(&mut self) -> f64 {
        (self.bits() >> 11) as f64 / (1u64 << 53) as f64
    }

    fn uniform_in(&mut self, low: f64, high: f64) -> f64 {
        low + (high - low) * self.unit()
    }

    /// A standard normal value (Box-Muller).
    fn normal(&mut self) -> f64 {
        let u1 = 1.0 - self.unit();
        let u2 = self.unit();
        (-2.0 * u1.ln()).sqrt() * (std::f64::consts::TAU * u2).cos()
    }

    fn below(&mut self, n: usize) -> usize {
        (self.unit() * n as f64) as usize
    }

    /// A unit vector whose components are drawn from `[-1, 1]` (rejecting the short ones).
    fn direction(&mut self) -> Vector3<f64> {
        loop {
            let v = Vector3::new(self.uniform_in(-1.0, 1.0), self.uniform_in(-1.0, 1.0), self.uniform_in(-1.0, 1.0));
            if v.norm() > 0.5 {
                return v.normalize();
            }
        }
    }

    /// Fisher-Yates.
    fn shuffle<T>(&mut self, items: &mut [T]) {
        for i in (1..items.len()).rev() {
            let j = self.below(i + 1);
            items.swap(i, j);
        }
    }
}

fn skew(v: &Vector3<f64>) -> Matrix3<f64> {
    Matrix3::new(0.0, -v.z, v.y, v.z, 0.0, -v.x, -v.y, v.x, 0.0)
}

/// The rotation of `angle` radians about the unit vector `axis` (Rodrigues).
fn rotation(axis: &Vector3<f64>, angle: f64) -> Matrix3<f64> {
    let k = skew(axis);
    Matrix3::identity() + k * angle.sin() + k * k * (1.0 - angle.cos())
}

fn calibration() -> Matrix3<f64> {
    Matrix3::new(FOCAL, 0.0, CX, 0.0, FOCAL, CY, 0.0, 0.0, 1.0)
}

fn camera() -> Camera {
    Camera::new("SIMPLE_PINHOLE", &[FOCAL, CX, CY], WIDTH, HEIGHT).unwrap()
}

/// The pixel of a point given in a camera's frame, when the point is in front of the camera and
/// inside the photo.
fn pixel(p: &Vector3<f64>) -> Option<[f64; 2]> {
    if p.z < 0.5 {
        return None;
    }
    let x = FOCAL * p.x / p.z + CX;
    let y = FOCAL * p.y / p.z + CY;
    (x >= 0.0 && x < WIDTH && y >= 0.0 && y < HEIGHT).then_some([x, y])
}

/// The angle of the rotation `d`, in radians, exact for small angles.
fn angle_of(d: &Matrix3<f64>) -> f64 {
    let sine = (d - d.transpose()).norm() / (2.0 * 2f64.sqrt());
    let cosine = (d.trace() - 1.0) / 2.0;
    sine.atan2(cosine)
}

/// The angle between two directions, in radians.
fn angle_between(a: &Vector3<f64>, b: &Vector3<f64>) -> f64 {
    a.cross(b).norm().atan2(a.dot(b))
}

#[derive(Clone, Copy, PartialEq)]
enum Kind {
    /// Points in a box 3 to 6 units in front of the first camera, the second camera rotated by
    /// 0.1 to 0.3 rad and moved by 0.5 to 1.2 units, mostly sideways.
    General,
    /// The points on the plane `PLANE_NORMAL . X = PLANE_D`, the cameras as `General`.
    Planar,
    /// The second camera rotated by 0.1 to 0.2 rad about the centre of the first, not moved; the
    /// points as `Planar` (a pure rotation has no epipolar geometry, so the points may lie anywhere).
    Rotation,
}

/// A pair: the key points of both photos (COLMAP pixels, `[x, y, scale, orientation]`), the
/// matches in a random order with their quality, which of them are true, and the truth.
struct Pair {
    kp1: Vec<[f32; 4]>,
    kp2: Vec<[f32; 4]>,
    matches: Vec<[u32; 2]>,
    quality: Vec<f32>,
    truth: Vec<bool>,
    r: Matrix3<f64>,
    t: Vector3<f64>,
}

impl Pair {
    fn new(seed: u64, kind: Kind, noise: f64) -> Pair {
        Pair::sized(seed, kind, noise, N_MATCHES)
    }

    /// As `new`, with `n_matches` matches in place of `N_MATCHES`.
    fn sized(seed: u64, kind: Kind, noise: f64, n_matches: usize) -> Pair {
        let mut random = Random(seed.wrapping_mul(7919).wrapping_add(17));
        let r = rotation(&random.direction(), random.uniform_in(0.1, 0.3));
        let t = if kind == Kind::Rotation {
            Vector3::zeros()
        } else {
            let sideways = if random.unit() < 0.5 { -1.0 } else { 1.0 } * random.uniform_in(0.4, 1.0);
            Vector3::new(sideways, random.uniform_in(-0.3, 0.3), random.uniform_in(-0.3, 0.3)).normalize()
                * random.uniform_in(0.5, 1.2)
        };
        let n_outliers = (OUTLIER_SHARE * n_matches as f64) as usize;
        let n_inliers = n_matches - n_outliers;
        let (mut x1, mut x2) = (Vec::new(), Vec::new());
        while x1.len() < n_matches {
            let x = random.uniform_in(-1.5, 1.5);
            let y = random.uniform_in(-1.1, 1.1);
            let z = match kind {
                Kind::General => random.uniform_in(3.0, 6.0),
                _ => (PLANE_D - PLANE_NORMAL[0] * x - PLANE_NORMAL[1] * y) / PLANE_NORMAL[2],
            };
            let p1 = Vector3::new(x, y, z);
            if let (Some(a), Some(b)) = (pixel(&p1), pixel(&(r * p1 + t))) {
                x1.push(a);
                x2.push(b);
            }
        }
        // the true geometry, to keep the outliers away from it
        let k = calibration();
        let k_inverse = k.try_inverse().unwrap();
        let fundamental = k_inverse.transpose() * skew(&t) * r * k_inverse;
        let homography = k * (r + t * Vector3::from(PLANE_NORMAL).transpose() / PLANE_D) * k_inverse;
        // measured as the verification measures them: the Sampson error of the epipolar
        // geometry (the distance from the epipolar line in one photo can be large while the
        // Sampson error is small) and the transfer error of the homography
        let predicted_distance = |a: [f64; 2], b: [f64; 2]| -> f64 {
            let transfer = transfer_sq(&homography, a, b).sqrt();
            if kind == Kind::Rotation {
                return transfer;
            }
            let epipolar = sampson_sq(&fundamental, a, b).sqrt();
            if kind == Kind::Planar {
                epipolar.min(transfer)
            } else {
                epipolar
            }
        };
        let mut truth = vec![true; n_matches];
        for i in n_inliers..n_matches {
            truth[i] = false;
            loop {
                let b = [random.uniform_in(0.0, WIDTH), random.uniform_in(0.0, HEIGHT)];
                if predicted_distance(x1[i], b) >= OUTLIER_DISTANCE {
                    x2[i] = b;
                    break;
                }
            }
        }
        for (a, b) in x1.iter_mut().zip(&mut x2) {
            for v in a.iter_mut().chain(b.iter_mut()) {
                *v += noise * random.normal();
            }
        }
        // the second photo's key points shuffled, the matches in a random order; the quality is
        // the ratio of the two best distances, lower for the true matches on the whole
        let mut place: Vec<usize> = (0..n_matches).collect();
        random.shuffle(&mut place);
        let mut kp2 = vec![[0.0f32; 4]; n_matches];
        for (i, &j) in place.iter().enumerate() {
            kp2[j] = [x2[i][0] as f32, x2[i][1] as f32, 1.0, 0.0];
        }
        let kp1: Vec<[f32; 4]> = x1.iter().map(|p| [p[0] as f32, p[1] as f32, 1.0, 0.0]).collect();
        let mut order: Vec<usize> = (0..n_matches).collect();
        random.shuffle(&mut order);
        let matches: Vec<[u32; 2]> = order.iter().map(|&i| [i as u32, place[i] as u32]).collect();
        let quality: Vec<f32> = order
            .iter()
            .map(|&i| if truth[i] { random.uniform_in(0.4, 0.75) } else { random.uniform_in(0.6, 0.8) } as f32)
            .collect();
        let truth = order.iter().map(|&i| truth[i]).collect();
        Pair { kp1, kp2, matches, quality, truth, r, t }
    }

    fn verify(&self, calibrated: bool, o: &two_view::Options, seed: u64) -> TwoView {
        verify(&camera(), &self.kp1, &camera(), &self.kp2, &self.matches, &self.quality, calibrated, o, seed)
    }

    /// Of the inliers of `v`, how many are true matches and how many are not.
    fn count(&self, v: &TwoView) -> (usize, usize) {
        let mut good = 0;
        let mut bad = 0;
        for m in &v.inliers {
            let i = self.matches.iter().position(|x| x == m).expect("an inlier is one of the matches");
            if self.truth[i] {
                good += 1;
            } else {
                bad += 1;
            }
        }
        (good, bad)
    }

    fn true_count(&self) -> usize {
        self.truth.iter().filter(|&&t| t).count()
    }
}

fn with_pose() -> two_view::Options {
    two_view::Options { compute_relative_pose: true, ..Default::default() }
}

/// The inliers hold at least 95 % of the true matches and none of the others. On a plane or a
/// pure rotation (`degenerate`) the inliers are the epipolar model's, which the points do not
/// determine (any `[e]x H` fits coplanar points, and a pure rotation leaves the fundamental
/// matrix free altogether): the flat support absorbs a few outliers, as COLMAP's verification
/// absorbs them (two to six of 210 true matches on these scenes); up to 5 % of the true count is
/// allowed.
fn check_inliers(pair: &Pair, v: &TwoView, what: &str, degenerate: bool) {
    let (good, bad) = pair.count(v);
    let wanted = pair.true_count();
    assert!(good as f64 >= 0.95 * wanted as f64, "{what}: {good} of {wanted} true matches among the inliers");
    if degenerate {
        assert!(bad as f64 <= 0.05 * wanted as f64, "{what}: {bad} outliers among the inliers");
    } else {
        assert_eq!(bad, 0, "{what}: {bad} outliers among the inliers");
    }
    assert_eq!(v.inliers.len(), good + bad);
}

#[test]
fn a_general_scene_is_calibrated_with_a_focal_and_uncalibrated_without() {
    for seed in 1..=SCENES {
        let pair = Pair::new(seed, Kind::General, 0.5);
        let v = pair.verify(true, &with_pose(), seed);
        assert_eq!(v.config, Config::Calibrated, "scene {seed}");
        check_inliers(&pair, &v, &format!("scene {seed}, calibrated"), false);
        assert!(v.e.is_some() && v.f.is_some() && v.h.is_some(), "scene {seed}: E, F and H are estimated");
        let (q, t) = v.cam2_from_cam1.expect("the relative pose");
        let rotation_error = angle_of(&(q.to_rotation_matrix().matrix() * pair.r.transpose()));
        assert!(rotation_error < 5e-3, "scene {seed}: the rotation is {rotation_error} rad off");
        let direction_error = angle_between(&t, &pair.t);
        assert!(direction_error < 1e-2, "scene {seed}: the translation is {direction_error} rad off");
        assert!((t.norm() - 1.0).abs() < 1e-9, "scene {seed}: |t| = {}", t.norm());

        let v = pair.verify(false, &with_pose(), seed);
        assert_eq!(v.config, Config::Uncalibrated, "scene {seed}");
        check_inliers(&pair, &v, &format!("scene {seed}, uncalibrated"), false);
        assert!(v.e.is_none() && v.f.is_some() && v.h.is_some(), "scene {seed}: F and H only");
        assert!(v.cam2_from_cam1.is_none(), "scene {seed}: no pose without an essential matrix");
    }
}

#[test]
fn a_plane_seen_from_two_places_is_planar() {
    for seed in 1..=SCENES {
        let pair = Pair::new(seed, Kind::Planar, 0.5);
        for calibrated in [true, false] {
            let v = pair.verify(calibrated, &with_pose(), seed);
            assert_eq!(v.config, Config::Planar, "scene {seed}, calibrated {calibrated}");
            check_inliers(&pair, &v, &format!("scene {seed}, planar, calibrated {calibrated}"), true);
            // the pose of the homography: the rotation, the direction of the translation and
            // its length over the distance of the plane (`PLANE_NORMAL` is not a unit vector: the
            // distance is `PLANE_D` over its norm). The translation carries the error of the
            // homography divided by its own length, 0.13 to 0.32 here: a few hundredths of a radian
            let (q, t) = v.cam2_from_cam1.expect("the relative pose");
            let rotation_error = angle_of(&(q.to_rotation_matrix().matrix() * pair.r.transpose()));
            assert!(rotation_error < 5e-3, "scene {seed}: the rotation is {rotation_error} rad off");
            let direction_error = angle_between(&t, &pair.t);
            assert!(direction_error < 5e-2, "scene {seed}: the translation is {direction_error} rad off");
            let length = pair.t.norm() * Vector3::from(PLANE_NORMAL).norm() / PLANE_D;
            assert!((t.norm() - length).abs() < 5e-2 * length, "scene {seed}: |t| = {} for {length}", t.norm());
            // without the pose the configuration is left undecided between the two, as COLMAP
            // leaves it
            let v = pair.verify(calibrated, &Default::default(), seed);
            assert_eq!(v.config, Config::PlanarOrPanoramic, "scene {seed}, calibrated {calibrated}");
            assert!(v.cam2_from_cam1.is_none());
        }
    }
}

#[test]
fn a_pure_rotation_is_panoramic() {
    // 0.1 px of noise: the homography of the inliers is a rotation to the tolerance COLMAP uses
    // (with 0.5 px COLMAP itself calls the pair planar)
    for seed in 1..=SCENES {
        let pair = Pair::new(seed, Kind::Rotation, 0.1);
        for calibrated in [true, false] {
            let v = pair.verify(calibrated, &with_pose(), seed);
            assert_eq!(v.config, Config::Panoramic, "scene {seed}, calibrated {calibrated}");
            check_inliers(&pair, &v, &format!("scene {seed}, rotation, calibrated {calibrated}"), true);
            let (q, t) = v.cam2_from_cam1.expect("the relative pose");
            let rotation_error = angle_of(&(q.to_rotation_matrix().matrix() * pair.r.transpose()));
            assert!(rotation_error < 2e-3, "scene {seed}: the rotation is {rotation_error} rad off");
            assert_eq!(t, Vector3::zeros(), "scene {seed}: a panorama has no translation");
        }
    }
}

#[test]
fn matches_that_do_not_move_in_the_border_band_are_a_watermark() {
    // 200 points in the band of a tenth of the diagonal (250 px) along the left and right edges,
    // the same in both photos to 0.3 px
    let mut random = Random(99);
    let band = 0.1 * (WIDTH * WIDTH + HEIGHT * HEIGHT).sqrt();
    let mut kp1 = Vec::new();
    let mut kp2 = Vec::new();
    for i in 0..200 {
        let x = if i % 2 == 0 { random.uniform_in(0.0, band) } else { random.uniform_in(WIDTH - band, WIDTH) };
        let y = random.uniform_in(0.0, HEIGHT);
        kp1.push([x as f32, y as f32, 1.0, 0.0]);
        kp2.push([(x + 0.3 * random.normal()) as f32, (y + 0.3 * random.normal()) as f32, 1.0, 0.0]);
    }
    let matches: Vec<[u32; 2]> = (0..200).map(|i| [i, i]).collect();
    let quality = vec![0.6f32; 200];
    for calibrated in [true, false] {
        let o = two_view::Options::default();
        let v = verify(&camera(), &kp1, &camera(), &kp2, &matches, &quality, calibrated, &o, 5);
        assert_eq!(v.config, Config::Watermark, "calibrated {calibrated}");
        assert!(v.inliers.len() >= 190, "calibrated {calibrated}: {} inliers", v.inliers.len());
        // without the detection the identity is a homography
        let o = two_view::Options { detect_watermark: false, ..Default::default() };
        let v = verify(&camera(), &kp1, &camera(), &kp2, &matches, &quality, calibrated, &o, 5);
        assert_eq!(v.config, Config::PlanarOrPanoramic, "calibrated {calibrated}");
    }
}

#[test]
fn ten_matches_are_degenerate() {
    let pair = Pair::new(3, Kind::General, 0.0);
    let (matches, quality) = (&pair.matches[..10], &pair.quality[..10]);
    for calibrated in [true, false] {
        let o = two_view::Options::default();
        let v = verify(&camera(), &pair.kp1, &camera(), &pair.kp2, matches, quality, calibrated, &o, 1);
        assert_eq!(v.config, Config::Degenerate, "calibrated {calibrated}");
        assert!(v.inliers.is_empty());
        assert!(v.cam2_from_cam1.is_none());
        // below min_num_inliers no model is estimated (COLMAP's early return)
        assert!(v.e.is_none() && v.f.is_none() && v.h.is_none(), "calibrated {calibrated}: a model was estimated");
    }
    // no matches at all, and a single one
    let v = verify(&camera(), &pair.kp1, &camera(), &pair.kp2, &[], &[], true, &Default::default(), 1);
    assert_eq!(v.config, Config::Degenerate);
    assert!(v.inliers.is_empty() && v.e.is_none() && v.f.is_none() && v.h.is_none());
    let o = two_view::Options::default();
    let v = verify(&camera(), &pair.kp1, &camera(), &pair.kp2, &matches[..1], &quality[..1], true, &o, 1);
    assert_eq!(v.config, Config::Degenerate);
}

#[test]
fn matches_without_a_quality_are_taken_in_their_order() {
    for seed in 1..=SCENES {
        let pair = Pair::new(seed, Kind::General, 0.5);
        let o = two_view::Options::default();
        let v = verify(&camera(), &pair.kp1, &camera(), &pair.kp2, &pair.matches, &[], true, &o, seed);
        assert_eq!(v.config, Config::Calibrated, "scene {seed}");
        check_inliers(&pair, &v, &format!("scene {seed}, no quality"), false);
    }
}

#[test]
fn a_homography_without_a_fundamental_matrix_leaves_the_pair_planar_or_panoramic() {
    // six exact matches of a plane with min_num_inliers lowered to four: the seven-point solver
    // has no sample, the homography has its four. Without a prior focal length COLMAP keeps the
    // fundamental matrix's inliers, so the pair is planar or panoramic with none; with one, the
    // essential matrix (five points) takes over and its six inliers are the pair's
    let pair = Pair::new(1, Kind::Planar, 0.0);
    let matches: Vec<[u32; 2]> =
        pair.matches.iter().zip(&pair.truth).filter(|(_, &t)| t).map(|(m, _)| *m).take(6).collect();
    let quality = vec![0.5f32; 6];
    let o = two_view::Options { min_num_inliers: 4, ..Default::default() };
    let v = verify(&camera(), &pair.kp1, &camera(), &pair.kp2, &matches, &quality, false, &o, 3);
    assert_eq!(v.config, Config::PlanarOrPanoramic);
    assert!(v.inliers.is_empty() && v.f.is_none() && v.h.is_some() && v.e.is_none());
    let v = verify(&camera(), &pair.kp1, &camera(), &pair.kp2, &matches, &quality, true, &o, 3);
    assert_eq!(v.config, Config::PlanarOrPanoramic);
    assert_eq!(v.inliers.len(), 6);
    assert!(v.f.is_none() && v.h.is_some() && v.e.is_some());
}

#[test]
fn the_same_seed_gives_the_same_result_and_another_seed_the_same_configuration() {
    for (seed, kind) in [(2, Kind::General), (4, Kind::Planar)] {
        let pair = Pair::new(seed, kind, 0.5);
        let first = pair.verify(true, &with_pose(), 1234);
        let again = pair.verify(true, &with_pose(), 1234);
        assert_eq!(first, again, "scene {seed}: the same seed, the same result");
        for other_seed in [1, 77, u64::MAX] {
            let other = pair.verify(true, &with_pose(), other_seed);
            assert_eq!(other.config, first.config, "scene {seed}, seed {other_seed}");
            check_inliers(&pair, &other, &format!("scene {seed}, seed {other_seed}"), kind == Kind::Planar);
        }
    }
}

#[test]
fn the_configurations_are_numbered_as_colmaps() {
    assert_eq!(Config::Undefined as u32, 0);
    assert_eq!(Config::Degenerate as u32, 1);
    assert_eq!(Config::Calibrated as u32, 2);
    assert_eq!(Config::Uncalibrated as u32, 3);
    assert_eq!(Config::Planar as u32, 4);
    assert_eq!(Config::Panoramic as u32, 5);
    assert_eq!(Config::PlanarOrPanoramic as u32, 6);
    assert_eq!(Config::Watermark as u32, 7);
    for n in 0..8 {
        assert_eq!(Config::from_u32(n).map(|c| c as u32), Some(n));
    }
    assert_eq!(Config::from_u32(8), None);
}

#[test]
fn pair_seed_is_the_same_in_both_orders_and_is_fnv1a() {
    assert_eq!(pair_seed("IMG_0001.JPG", "IMG_0002.JPG"), pair_seed("IMG_0002.JPG", "IMG_0001.JPG"));
    assert_ne!(pair_seed("IMG_0001.JPG", "IMG_0002.JPG"), pair_seed("IMG_0001.JPG", "IMG_0003.JPG"));
    // FNV-1a 64 of the names sorted and joined by a NUL byte, computed outside this crate
    assert_eq!(pair_seed("IMG_0002.JPG", "IMG_0001.JPG"), 7093375131204520798);
    assert_eq!(pair_seed("b", "a"), 16560467112517592754);
}

#[test]
fn lo_ransac_finds_the_shift_of_the_majority_and_stops_early() {
    let mut random = Random(5);
    let x1: Vec<[f64; 2]> = (0..400).map(|_| [random.uniform_in(0.0, WIDTH), random.uniform_in(0.0, HEIGHT)]).collect();
    // the first 70 % moved by (12, -7) with a pixel of noise, the rest anywhere else
    let x2: Vec<[f64; 2]> = x1
        .iter()
        .enumerate()
        .map(|(i, a)| {
            if i < 280 {
                [a[0] + 12.0 + random.normal(), a[1] - 7.0 + random.normal()]
            } else {
                loop {
                    let b = [random.uniform_in(0.0, WIDTH), random.uniform_in(0.0, HEIGHT)];
                    if (b[0] - a[0] - 12.0).hypot(b[1] - a[1] + 7.0) > 20.0 {
                        break b;
                    }
                }
            }
        })
        .collect();
    let o = ransac::Options::default();
    let report = lo_ransac::<Translation>(&x1, &x2, &o, Sampling::Prosac, 42).expect("a model");
    assert!((report.model.0[0] - 12.0).abs() < 0.3 && (report.model.0[1] + 7.0).abs() < 0.3, "{:?}", report.model);
    assert!(report.support.num_inliers >= 275 && report.support.num_inliers <= 280, "{}", report.support.num_inliers);
    assert!(report.inlier_mask[..280].iter().filter(|&&m| m).count() >= 275);
    assert!(report.inlier_mask[280..].iter().all(|&m| !m), "an outlier counted as an inlier");
    assert_eq!(report.inlier_mask.len(), 400);
    // the trials are bounded as COLMAP bounds them by the inlier ratio assumed, 25 %: for a sample
    // of one, ceil(ln 0.001 / ln 0.75 * 3) = 73, fewer than the minimum of 100 (which does not
    // lift the bound)
    assert_eq!(report.num_trials, 73);
    // the same seed gives the same report, bit for bit
    let again = lo_ransac::<Translation>(&x1, &x2, &o, Sampling::Prosac, 42).expect("a model");
    assert_eq!(report.model, again.model);
    assert_eq!(report.inlier_mask, again.inlier_mask);
    assert_eq!(report.num_trials, again.num_trials);
    // fewer points than the sample needs, and a sample that fits nothing
    assert!(lo_ransac::<Translation>(&[], &[], &o, Sampling::Prosac, 1).is_none());
    let unrelated = lo_ransac::<Translation>(&x1[..5], &x2[395..], &o, Sampling::Uniform, 1);
    assert!(unrelated.is_some_and(|r| r.support.num_inliers >= 1));
}

#[test]
fn the_generator_is_seeded_and_draws_below_a_bound() {
    let mut a = Lcg::new(7);
    let mut b = Lcg::new(7);
    let mut c = Lcg::new(8);
    let va: Vec<u64> = (0..8).map(|_| a.bits()).collect();
    let vb: Vec<u64> = (0..8).map(|_| b.bits()).collect();
    let vc: Vec<u64> = (0..8).map(|_| c.bits()).collect();
    assert_eq!(va, vb);
    assert_ne!(va, vc);
    let mut seen = [false; 10];
    for _ in 0..1000 {
        let k = a.below(10);
        assert!(k < 10);
        seen[k] = true;
    }
    assert!(seen.iter().all(|&s| s), "every value below the bound is drawn");
    assert_eq!(a.below(1), 0);
}

#[test]
fn the_defaults_are_pycolmaps_of_the_installed_version() {
    // pinned by tests/test_native.py against the installed pycolmap; written here so that the
    // Rust side cannot drift on its own
    let o = two_view::Options::default();
    assert_eq!(o.ransac.max_error, 4.0);
    assert_eq!(o.ransac.confidence, 0.999);
    assert_eq!(o.ransac.min_num_trials, 100);
    assert_eq!(o.ransac.max_num_trials, 10000);
    assert_eq!(o.ransac.min_inlier_ratio, 0.25);
    assert_eq!(o.ransac.dyn_num_trials_multiplier, 3.0);
    assert_eq!(o.min_num_inliers, 15);
    assert_eq!(o.min_e_f_inlier_ratio, 0.95);
    assert_eq!(o.max_h_inlier_ratio, 0.8);
    assert_eq!(o.watermark_min_inlier_ratio, 0.7);
    assert_eq!(o.watermark_border_size, 0.1);
    assert_eq!(o.watermark_detection_max_error, 4.0);
    assert!(o.detect_watermark);
    assert!(o.use_sampson_refinement);
    assert!(!o.compute_relative_pose);
}

/// A timing probe, not a check (`cargo test --release -p ppg_engine --test two_view -- --ignored
/// --nocapture`): `verify` on synthetic pairs of 8000 matches, 30 % of them outliers, calibrated
/// (E, F and H) and uncalibrated (F and H), the best of a few runs each; it prints the times and a
/// fingerprint of the inliers (their count and an FNV-1a hash), so that two builds can be compared
/// for speed and for the same results.
#[test]
#[ignore]
fn timing_of_verify_on_eight_thousand_matches() {
    use std::time::Instant;
    let o = two_view::Options::default();
    let mut total = [0.0f64; 2];
    for seed in 1..=4u64 {
        let pair = Pair::sized(seed, Kind::General, 0.5, 8000);
        for (k, calibrated) in [true, false].into_iter().enumerate() {
            let mut best = f64::INFINITY;
            let mut result = None;
            for _ in 0..3 {
                let start = Instant::now();
                let v = pair.verify(calibrated, &o, seed);
                best = best.min(start.elapsed().as_secs_f64());
                result = Some(v);
            }
            let v = result.expect("three runs");
            let mut hash: u64 = 0xcbf29ce484222325;
            for m in &v.inliers {
                for byte in m.iter().flat_map(|i| i.to_le_bytes()) {
                    hash ^= u64::from(byte);
                    hash = hash.wrapping_mul(0x100000001b3);
                }
            }
            total[k] += best;
            println!("scene {seed} calibrated {calibrated}: {:.1} ms, {:?}, {} inliers, hash {hash:016x}",
                     best * 1e3, v.config, v.inliers.len());
        }
    }
    println!("total: calibrated {:.1} ms, uncalibrated {:.1} ms", total[0] * 1e3, total[1] * 1e3);
}
