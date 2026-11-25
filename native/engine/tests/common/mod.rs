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

use nalgebra::{Matrix3, Rotation3, Vector2, Vector3};
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
    /// A ring flown once (`ring`): nadir photos along one closed circle, each sharing points with
    /// its 3 neighbours on each side only.
    Ring,
    /// Nadir photos on a small grid (`patch`), every photo overlapping every other.
    Patch,
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
    /// The share of the key points displaced (a wrong feature taken for the right one in every pair
    /// of its photo), each by a distance drawn uniformly in `displaced_px` in a direction drawn
    /// uniformly.
    pub displaced_share: f64,
    pub displaced_px: [f64; 2],
    /// The focal length the camera starts from.
    pub start_focal: f64,
    pub prior_focal: bool,
    /// The fundamental matrix of each pair, by the eight-point solver on its exact true matches ...
    pub with_f: bool,
    /// ... or on the key points of its true matches (with their noise).
    pub f_from_keypoints: bool,
    pub seed: u64,
}

impl Default for InputOptions {
    fn default() -> Self {
        InputOptions {
            noise_px: 0.0,
            outlier_share: 0.0,
            displaced_share: 0.0,
            displaced_px: [0.0, 0.0],
            start_focal: FOCAL,
            prior_focal: true,
            with_f: false,
            f_from_keypoints: false,
            seed: 1,
        }
    }
}

fn rotation_xyz(roll: f64, pitch: f64, yaw: f64) -> Matrix3<f64> {
    Rotation3::from_euler_angles(roll, pitch, yaw).into_inner()
}

/// A scene of `n` photos of `kind`, drawn from `seed` (a ring 600 m across 40 m high, a patch of one row
/// of photos 4 m apart 20 m high).
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
        Kind::Ring => return ring(n, 600.0, 40.0, seed),
        Kind::Patch => return patch(n, 1, 4.0, 20.0, seed),
    };
    let views = views_of(&poses, &points);
    Scene { kind, poses, focal: FOCAL, cx: WIDTH / 2.0, cy: HEIGHT / 2.0, points, views }
}

/// The exact pixel of `x` in the photo at `pose`, `None` behind it or outside the photo.
fn pixel_of(pose: &Pose, x: &Vector3<f64>) -> Option<[f64; 2]> {
    let c = pose.to_cam(x);
    if c.z <= 0.0 {
        return None;
    }
    let pixel = [FOCAL * c.x / c.z + WIDTH / 2.0, FOCAL * c.y / c.z + HEIGHT / 2.0];
    let inside = pixel[0] > 1.0 && pixel[0] < WIDTH - 1.0 && pixel[1] > 1.0 && pixel[1] < HEIGHT - 1.0;
    inside.then_some(pixel)
}

/// Per photo the points it sees with their exact pixels, in the order of the points.
fn views_of(poses: &[Pose], points: &[Vector3<f64>]) -> Vec<Vec<(usize, [f64; 2])>> {
    poses
        .iter()
        .map(|pose| points.iter().enumerate().filter_map(|(k, p)| pixel_of(pose, p).map(|px| (k, px))).collect())
        .collect()
}

/// A ring flown once: `n` nadir photos at `height` over a flat ground with a little relief, their
/// centres on a circle of `diameter`, each photo's x axis along the circle but for a tilt of up
/// to 3 degrees and a yaw of up to 10 drawn per photo. The points are drawn
/// per window of four consecutive photos (`POINTS_PER_WINDOW` of them on the ground under the
/// middle of the window, inside all four photos) and seen by those four only: a photo shares
/// points with its 3 neighbours on each side and with no other photo. The rays are exact.
pub fn ring(n: usize, diameter: f64, height: f64, seed: u64) -> Scene {
    ring_with_relief(n, diameter, height, 2.0, seed)
}

/// `ring` over a ground whose heights are drawn within `relief` metres of zero (0: a flat ground,
/// where a nadir block cannot tell its focal length from its height).
pub fn ring_with_relief(n: usize, diameter: f64, height: f64, relief: f64, seed: u64) -> Scene {
    let mut random = Random::new(seed);
    let radius = diameter / 2.0;
    let angle = |i: f64| std::f64::consts::TAU * i / n as f64;
    let poses: Vec<Pose> = (0..n)
        .map(|i| {
            let (s, c) = angle(i as f64).sin_cos();
            // rows of cam_from_world: x along the circle, z down, y = z x x; then a tilt of up to
            // 3 degrees and a yaw of up to 10 (the rotations are not symmetric)
            let along = Matrix3::new(-s, c, 0.0, c, s, 0.0, 0.0, 0.0, -1.0);
            let (roll, pitch) = (random.uniform_in(-3.0, 3.0), random.uniform_in(-3.0, 3.0));
            let yaw = random.uniform_in(-10.0, 10.0);
            let rotation = rotation_xyz(roll.to_radians(), pitch.to_radians(), yaw.to_radians()) * along;
            Pose { rotation, centre: Vector3::new(radius * c, radius * s, height) }
        })
        .collect();
    let mut points = Vec::new();
    let mut views: Vec<Vec<(usize, [f64; 2])>> = vec![Vec::new(); n];
    for k in 0..n {
        let window: Vec<usize> = (0..4).map(|d| (k + d) % n).collect();
        let mut placed = 0;
        let mut attempts = 0;
        while placed < POINTS_PER_WINDOW {
            attempts += 1;
            assert!(attempts < 100 * POINTS_PER_WINDOW, "no ground under the window of photo {k}");
            let (s, c) = angle(k as f64 + 1.5 + random.uniform_in(-0.5, 0.5)).sin_cos();
            let rho = radius + random.uniform_in(-0.4, 0.4) * height;
            let x = Vector3::new(rho * c, rho * s, random.uniform_in(-relief, relief));
            let pixels: Vec<Option<[f64; 2]>> = window.iter().map(|&i| pixel_of(&poses[i], &x)).collect();
            if pixels.iter().any(|p| p.is_none()) {
                continue;
            }
            for (&i, px) in window.iter().zip(&pixels) {
                views[i].push((points.len(), px.expect("inside")));
            }
            points.push(x);
            placed += 1;
        }
    }
    for view in &mut views {
        view.sort_by_key(|&(p, _)| p);
    }
    Scene { kind: Kind::Ring, poses, focal: FOCAL, cx: WIDTH / 2.0, cy: HEIGHT / 2.0, points, views }
}

/// The points of each window of four photos of `ring`.
pub const POINTS_PER_WINDOW: usize = 40;

/// `nx` x `ny` nadir photos `spacing` apart at `height` (a tilt of up to 3 degrees and a yaw of up
/// to 10 drawn per photo), over points of a little relief spread
/// under them: every photo sees most of the others' points.
pub fn patch(nx: usize, ny: usize, spacing: f64, height: f64, seed: u64) -> Scene {
    let mut random = Random::new(seed);
    let down = Matrix3::new(1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, -1.0);
    let mut poses = Vec::new();
    for j in 0..ny {
        for i in 0..nx {
            let centre = Vector3::new(i as f64 * spacing, j as f64 * spacing, height);
            let (roll, pitch) = (random.uniform_in(-3.0, 3.0), random.uniform_in(-3.0, 3.0));
            let yaw = random.uniform_in(-10.0, 10.0);
            let tilt = rotation_xyz(roll.to_radians(), pitch.to_radians(), yaw.to_radians());
            poses.push(Pose { rotation: tilt * down, centre });
        }
    }
    let (wx, wy) = ((nx - 1) as f64 * spacing, (ny - 1) as f64 * spacing);
    let points: Vec<Vector3<f64>> = (0..40 * poses.len())
        .map(|_| {
            Vector3::new(random.uniform_in(-0.5 * height, wx + 0.5 * height),
                         random.uniform_in(-0.4 * height, wy + 0.4 * height), random.uniform_in(-1.0, 1.0))
        })
        .collect();
    let views = views_of(&poses, &points);
    Scene { kind: Kind::Patch, poses, focal: FOCAL, cx: WIDTH / 2.0, cy: HEIGHT / 2.0, points, views }
}

/// A lawnmower nadir survey: `strips` strips of `per_strip` photos along y, flown in turn up and
/// down, `along` metres apart in a strip and `across` metres between strips, at `height` over a
/// ground with a mound of `mound` metres (a Gaussian of 100 m) in the middle of the corridor and a
/// metre of relief; a tilt of up to 3 degrees and a yaw of up to 10 drawn per photo. The points are
/// drawn uniformly over the corridor, `density` per square metre, and each photo sees those inside
/// it.
pub fn corridor(strips: usize, per_strip: usize, along: f64, across: f64, height: f64, mound: f64, density: f64,
                seed: u64) -> Scene {
    let mut random = Random::new(seed);
    let down = Matrix3::new(1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, -1.0);
    let half_turn = Matrix3::new(-1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, 1.0);
    let length = (per_strip - 1) as f64 * along;
    let mut poses = Vec::new();
    for j in 0..strips {
        for k in 0..per_strip {
            let y = if j % 2 == 0 { k as f64 * along } else { length - k as f64 * along };
            let (roll, pitch) = (random.uniform_in(-3.0, 3.0), random.uniform_in(-3.0, 3.0));
            let yaw = random.uniform_in(-10.0, 10.0);
            let tilt = rotation_xyz(roll.to_radians(), pitch.to_radians(), yaw.to_radians());
            let heading = if j % 2 == 0 { down } else { down * half_turn };
            poses.push(Pose { rotation: tilt * heading, centre: Vector3::new(j as f64 * across, y, height) });
        }
    }
    let (x0, x1) = (-height, (strips - 1) as f64 * across + height);
    let (y0, y1) = (-height, length + height);
    let count = (density * (x1 - x0) * (y1 - y0)) as usize;
    let middle = Vector2::new(0.5 * (x0 + x1), 0.5 * length);
    let points: Vec<Vector3<f64>> = (0..count)
        .map(|_| {
            let (x, y) = (random.uniform_in(x0, x1), random.uniform_in(y0, y1));
            let r2 = (Vector2::new(x, y) - middle).norm_squared();
            Vector3::new(x, y, mound * (-r2 / (2.0 * 100.0 * 100.0)).exp() + random.uniform_in(-1.0, 1.0))
        })
        .collect();
    // the points by cells of the ground, so that a photo looks only at those under it
    let cell = height;
    let key = |x: f64, y: f64| (((x - x0) / cell).floor() as i64, ((y - y0) / cell).floor() as i64);
    let mut cells: std::collections::HashMap<(i64, i64), Vec<usize>> = std::collections::HashMap::new();
    for (p, x) in points.iter().enumerate() {
        cells.entry(key(x.x, x.y)).or_default().push(p);
    }
    let views = poses
        .iter()
        .map(|pose| {
            let (cx, cy) = key(pose.centre.x, pose.centre.y);
            let mut seen: Vec<(usize, [f64; 2])> = Vec::new();
            for i in cx - 2..=cx + 2 {
                for j in cy - 2..=cy + 2 {
                    for &p in cells.get(&(i, j)).map_or(&[][..], |v| &v[..]) {
                        if let Some(px) = pixel_of(pose, &points[p]) {
                            seen.push((p, px));
                        }
                    }
                }
            }
            seen.sort_by_key(|&(p, _)| p);
            seen
        })
        .collect();
    Scene { kind: Kind::Patch, poses, focal: FOCAL, cx: WIDTH / 2.0, cy: HEIGHT / 2.0, points, views }
}

/// A turntable seen from outside: `n` photos on a circle of 10 m radius 6 m above the ground,
/// each looking at the centre of the circle from above, over an object of points spread in a box
/// 8 m x 8 m x 4 m around that centre: a convergent block whose depths vary, which tells the focal
/// length.
pub fn convergent(n: usize, seed: u64) -> Scene {
    let mut random = Random::new(seed);
    let poses: Vec<Pose> = (0..n)
        .map(|i| {
            let a = std::f64::consts::TAU * i as f64 / n as f64;
            let centre = Vector3::new(10.0 * a.cos(), 10.0 * a.sin(), 6.0 + random.uniform_in(-0.5, 0.5));
            let target = Vector3::new(random.uniform_in(-0.3, 0.3), random.uniform_in(-0.3, 0.3), 1.0);
            let z = (target - centre).normalize();
            let x = z.cross(&Vector3::z()).normalize();
            let y = z.cross(&x);
            Pose { rotation: Matrix3::from_rows(&[x.transpose(), y.transpose(), z.transpose()]), centre }
        })
        .collect();
    let points: Vec<Vector3<f64>> = (0..60 * n)
        .map(|_| {
            Vector3::new(random.uniform_in(-4.0, 4.0), random.uniform_in(-4.0, 4.0), random.uniform_in(-1.0, 3.0))
        })
        .collect();
    let views = views_of(&poses, &points);
    Scene { kind: Kind::Patch, poses, focal: FOCAL, cx: WIDTH / 2.0, cy: HEIGHT / 2.0, points, views }
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
    /// `(index in the view of a, index in the view of b)`, in the order of the view of `a`.
    pub fn shared(&self) -> Vec<(usize, usize, Vec<(u32, u32)>)> {
        // per point the photos that see it, with its index in their view
        let mut seen_by: Vec<Vec<(usize, u32)>> = vec![Vec::new(); self.points.len()];
        for (i, view) in self.views.iter().enumerate() {
            for (k, &(p, _)) in view.iter().enumerate() {
                seen_by[p].push((i, k as u32));
            }
        }
        let mut out = Vec::new();
        let mut with: std::collections::BTreeMap<usize, Vec<(u32, u32)>> = std::collections::BTreeMap::new();
        for a in 0..self.poses.len() {
            with.clear();
            for (k, &(p, _)) in self.views[a].iter().enumerate() {
                for &(b, kb) in seen_by[p].iter().filter(|(b, _)| *b > a) {
                    with.entry(b).or_default().push((k as u32, kb));
                }
            }
            for (&b, common) in with.iter_mut() {
                let smaller = self.views[a].len().min(self.views[b].len());
                if common.len() >= MIN_SHARED && common.len() as f64 >= MIN_SHARED_SHARE * smaller as f64 {
                    out.push((a, b, std::mem::take(common)));
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
                let mut x = px[0] + o.noise_px * random.normal();
                let mut y = px[1] + o.noise_px * random.normal();
                if o.displaced_share > 0.0 && random.unit() < o.displaced_share {
                    let d = random.uniform_in(o.displaced_px[0], o.displaced_px[1]);
                    let a = random.uniform_in(0.0, std::f64::consts::TAU);
                    x += d * a.cos();
                    y += d * a.sin();
                }
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
                let at = |image: usize, k: u32| {
                    if o.f_from_keypoints {
                        let p = keypoints[images[image].keypoints.start + k as usize];
                        [p[0] as f64, p[1] as f64]
                    } else {
                        self.views[image][k as usize].1
                    }
                };
                let x1: Vec<[f64; 2]> = common.iter().map(|&(ka, _)| at(a, ka)).collect();
                let x2: Vec<[f64; 2]> = common.iter().map(|&(_, kb)| at(b, kb)).collect();
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
