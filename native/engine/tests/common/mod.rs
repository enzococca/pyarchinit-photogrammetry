//! The synthetic scenes of the mapper tests (Tasks 3-7): cameras of known pose and focal length
//! over a ground with relief, the exact pixels of the points each photo sees, and the mapper's
//! `Input` built from them with the noise, the wrong matches and the fundamental matrices a test
//! asks for. Everything is drawn from a 64-bit linear congruential generator written here and
//! seeded by the test, so a scene is the same on every computer and in every run.
//!
//! Frames: the world has z up; a camera holds `cam_from_world = (R, -R C)`, so `x_cam = R (X - C)`,
//! its z axis looking along the view. Pixels are COLMAP's (the centre of the first pixel at 0.5,
//! 0.5): `x = f u + cx` with `cx = width / 2`.

#![allow(dead_code)]

use nalgebra::{Matrix3, Rotation3, Vector3};
use ppg_engine::geometry::solvers::fundamental_eight_point;
use ppg_engine::mapper::input::{CameraIn, ImageIn, Input, PairIn};

pub const FOCAL: f64 = 1000.0;
pub const WIDTH: f64 = 2000.0;
pub const HEIGHT: f64 = 1500.0;
/// Pairs that share fewer points than this, or less than `MIN_SHARED_SHARE` of the points the
/// smaller of their two views sees, are not matched (not "verified"): a pair that overlaps only in
/// a band at the edge of both photos has a relative pose good to a few tenths of a degree at best
/// (its estimate fits the noisy matches better than the truth does), which tells nothing of the
/// estimation.
pub const MIN_SHARED: usize = 30;
pub const MIN_SHARED_SHARE: f64 = 0.3;

/// Knuth's MMIX linear congruential generator (as in ppg-bench and the two-view tests).
pub struct Random(u64);

impl Random {
    pub fn new(seed: u64) -> Random {
        let mut r = Random(seed ^ 0x9E37_79B9_7F4A_7C15);
        r.bits();
        r
    }

    pub fn bits(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        self.0
    }

    /// A value in [0, 1): the upper 53 bits.
    pub fn unit(&mut self) -> f64 {
        (self.bits() >> 11) as f64 / (1u64 << 53) as f64
    }

    pub fn uniform_in(&mut self, low: f64, high: f64) -> f64 {
        low + (high - low) * self.unit()
    }

    /// A standard normal value (Box-Muller).
    pub fn normal(&mut self) -> f64 {
        let u1 = 1.0 - self.unit();
        let u2 = self.unit();
        (-2.0 * u1.ln()).sqrt() * (std::f64::consts::TAU * u2).cos()
    }

    pub fn below(&mut self, n: usize) -> usize {
        ((self.unit() * n as f64) as usize).min(n.saturating_sub(1))
    }
}

/// The kinds of scene.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// One strip of `n` photos 20 m over the ground, 5 m apart along x, looking down with up to
    /// 8 degrees of pitch and roll and 15 of yaw drawn per photo (a strip without any tilt leaves
    /// the focal length free in the fundamental matrices: every pair a pure translation).
    Strip,
    /// `n` photos from one centre, each turned 20 degrees about the y axis from the one before:
    /// no baseline, no parallax.
    Rotation,
}

/// One camera of a scene: `cam_from_world` rotation and centre.
#[derive(Clone, Debug)]
pub struct Pose {
    pub rotation: Matrix3<f64>,
    pub centre: Vector3<f64>,
}

impl Pose {
    pub fn to_cam(&self, x: &Vector3<f64>) -> Vector3<f64> {
        self.rotation * (x - self.centre)
    }
}

/// A scene with its truth: poses, the focal length of the one camera all photos share, the points
/// and, per photo, the points it sees with their exact pixels.
pub struct Scene {
    pub kind: Kind,
    pub poses: Vec<Pose>,
    pub focal: f64,
    pub cx: f64,
    pub cy: f64,
    pub points: Vec<Vector3<f64>>,
    /// Per photo, `(point, exact pixel)` in the order of the points.
    pub views: Vec<Vec<(usize, [f64; 2])>>,
}

/// What `Scene::input` builds the `Input` with.
#[derive(Clone, Debug)]
pub struct InputOptions {
    /// Gaussian noise added to every key point, in pixels.
    pub noise_px: f64,
    /// The share of the matches of every pair replaced by a wrong one (a key point of the second
    /// photo at least 100 pixels from the right one).
    pub outlier_share: f64,
    /// The focal length the camera starts from.
    pub start_focal: f64,
    pub prior_focal: bool,
    /// The fundamental matrix of each pair, by the eight-point solver on its exact true matches.
    pub with_f: bool,
    pub seed: u64,
}

impl Default for InputOptions {
    fn default() -> Self {
        InputOptions {
            noise_px: 0.0,
            outlier_share: 0.0,
            start_focal: FOCAL,
            prior_focal: true,
            with_f: false,
            seed: 1,
        }
    }
}

fn rotation_xyz(roll: f64, pitch: f64, yaw: f64) -> Matrix3<f64> {
    Rotation3::from_euler_angles(roll, pitch, yaw).into_inner()
}

/// A scene of `n` photos of `kind`, drawn from `seed`.
pub fn scene(n: usize, kind: Kind, seed: u64) -> Scene {
    let mut random = Random::new(seed);
    // the camera looking down: its z axis along -z of the world, its y axis along -y
    let down = Matrix3::new(1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, -1.0);
    let (poses, points) = match kind {
        Kind::Strip => {
            let height = 20.0;
            let spacing = 5.0;
            let tilt = 8f64.to_radians();
            let poses: Vec<Pose> = (0..n)
                .map(|i| {
                    let (roll, pitch) = (random.uniform_in(-tilt, tilt), random.uniform_in(-tilt, tilt));
                    let yaw = random.uniform_in(-15f64.to_radians(), 15f64.to_radians());
                    let centre = Vector3::new(i as f64 * spacing, random.uniform_in(-1.0, 1.0),
                                              height + random.uniform_in(-1.0, 1.0));
                    Pose { rotation: rotation_xyz(roll, pitch, yaw) * down, centre }
                })
                .collect();
            let length = (n.max(1) - 1) as f64 * spacing;
            let count = 300 * n.max(2);
            let points: Vec<Vector3<f64>> = (0..count)
                .map(|_| {
                    Vector3::new(random.uniform_in(-22.0, length + 22.0), random.uniform_in(-16.0, 16.0),
                                 random.uniform_in(-3.0, 3.0))
                })
                .collect();
            (poses, points)
        }
        Kind::Rotation => {
            let centre = Vector3::new(0.0, 0.0, 10.0);
            let poses: Vec<Pose> = (0..n)
                .map(|i| Pose { rotation: rotation_xyz(0.0, 20f64.to_radians() * i as f64, 0.0) * down, centre })
                .collect();
            let points: Vec<Vector3<f64>> = (0..300 * n.max(2))
                .map(|_| {
                    Vector3::new(random.uniform_in(-25.0, 25.0), random.uniform_in(-12.0, 12.0),
                                 random.uniform_in(-2.0, 2.0))
                })
                .collect();
            (poses, points)
        }
    };
    let (cx, cy) = (WIDTH / 2.0, HEIGHT / 2.0);
    let views = poses
        .iter()
        .map(|pose| {
            points
                .iter()
                .enumerate()
                .filter_map(|(k, p)| {
                    let x = pose.to_cam(p);
                    if x.z <= 0.0 {
                        return None;
                    }
                    let pixel = [FOCAL * x.x / x.z + cx, FOCAL * x.y / x.z + cy];
                    let inside = pixel[0] > 1.0 && pixel[0] < WIDTH - 1.0 && pixel[1] > 1.0 && pixel[1] < HEIGHT - 1.0;
                    inside.then_some((k, pixel))
                })
                .collect()
        })
        .collect();
    Scene { kind, poses, focal: FOCAL, cx, cy, points, views }
}

impl Scene {
    pub fn name(i: usize) -> String {
        format!("photo_{i:03}.jpg")
    }

    /// The true pose `cam_b_from_cam_a`: rotation and unit translation.
    pub fn relative(&self, a: usize, b: usize) -> (Matrix3<f64>, Vector3<f64>) {
        let (pa, pb) = (&self.poses[a], &self.poses[b]);
        let r = pb.rotation * pa.rotation.transpose();
        let t = pb.rotation * (pa.centre - pb.centre);
        let norm = t.norm();
        (r, if norm > 0.0 { t / norm } else { t })
    }

    /// The pairs that share enough points (`MIN_SHARED`, `MIN_SHARED_SHARE`), `a < b`, with the shared points as
    /// `(index in the view of a, index in the view of b)`.
    pub fn shared(&self) -> Vec<(usize, usize, Vec<(u32, u32)>)> {
        let mut out = Vec::new();
        for a in 0..self.poses.len() {
            for b in a + 1..self.poses.len() {
                let mut at_b = std::collections::HashMap::new();
                for (k, &(p, _)) in self.views[b].iter().enumerate() {
                    at_b.insert(p, k as u32);
                }
                let common: Vec<(u32, u32)> = self.views[a]
                    .iter()
                    .enumerate()
                    .filter_map(|(k, &(p, _))| at_b.get(&p).map(|&kb| (k as u32, kb)))
                    .collect();
                let smaller = self.views[a].len().min(self.views[b].len());
                if common.len() >= MIN_SHARED && common.len() as f64 >= MIN_SHARED_SHARE * smaller as f64 {
                    out.push((a, b, common));
                }
            }
        }
        out
    }

    /// The mapper's input: one SIMPLE_PINHOLE camera shared by every photo, the key points of each
    /// photo (its exact pixels plus noise) and the matches of every pair that shares enough points.
    pub fn input(&self, o: &InputOptions) -> Input {
        let mut random = Random::new(o.seed.wrapping_mul(0x2545_F491_4F6C_DD1D) ^ 0xA5A5);
        let mut keypoints = Vec::new();
        let mut images = Vec::new();
        for (i, view) in self.views.iter().enumerate() {
            let start = keypoints.len();
            for &(_, px) in view {
                let x = px[0] + o.noise_px * random.normal();
                let y = px[1] + o.noise_px * random.normal();
                keypoints.push([x as f32, y as f32]);
            }
            let keypoints = start..keypoints.len();
            images.push(ImageIn { name: Self::name(i), image_id: i as u32 + 1, camera: 0, keypoints });
        }
        let cameras = vec![CameraIn {
            camera_id: 1,
            model: "SIMPLE_PINHOLE".to_string(),
            params: vec![o.start_focal, self.cx, self.cy],
            width: WIDTH,
            height: HEIGHT,
            prior_focal: o.prior_focal,
        }];
        let mut pairs = Vec::new();
        let mut matches = Vec::new();
        for (a, b, common) in self.shared() {
            let start = matches.len();
            let n_wrong = (o.outlier_share * common.len() as f64).round() as usize;
            let mut wrong = vec![false; common.len()];
            let mut placed = 0;
            while placed < n_wrong {
                let k = random.below(common.len());
                if !wrong[k] {
                    wrong[k] = true;
                    placed += 1;
                }
            }
            for (k, &(ka, kb)) in common.iter().enumerate() {
                if !wrong[k] {
                    matches.push([ka, kb]);
                    continue;
                }
                let right = self.views[b][kb as usize].1;
                loop {
                    let other = random.below(self.views[b].len());
                    let px = self.views[b][other].1;
                    if (px[0] - right[0]).hypot(px[1] - right[1]) >= 100.0 {
                        matches.push([ka, other as u32]);
                        break;
                    }
                }
            }
            let f = if o.with_f {
                let x1: Vec<[f64; 2]> = common.iter().map(|&(ka, _)| self.views[a][ka as usize].1).collect();
                let x2: Vec<[f64; 2]> = common.iter().map(|&(_, kb)| self.views[b][kb as usize].1).collect();
                let f = fundamental_eight_point(&x1, &x2).expect("a fundamental matrix of the exact matches");
                let mut out = [0.0; 9];
                for r in 0..3 {
                    for c in 0..3 {
                        out[3 * r + c] = f[(r, c)];
                    }
                }
                Some(out)
            } else {
                None
            };
            let config = if o.prior_focal { 2 } else { 3 };
            pairs.push(PairIn { a, b, matches: start..matches.len(), config, f });
        }
        Input { images, cameras, keypoints, pairs, matches }
    }
}

/// The angle between two rotations, in degrees.
pub fn rotation_error_deg(r: &Matrix3<f64>, truth: &Matrix3<f64>) -> f64 {
    let d = r * truth.transpose();
    (((d.trace() - 1.0) / 2.0).clamp(-1.0, 1.0)).acos().to_degrees()
}

/// The angle between two directions, in degrees.
pub fn angle_deg(a: &Vector3<f64>, b: &Vector3<f64>) -> f64 {
    a.cross(b).norm().atan2(a.dot(b)).to_degrees()
}
