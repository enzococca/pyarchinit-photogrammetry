//! The synthetic ring of `ppg-bench map`: nadir photos along one closed circle flown once, each
//! sharing points with its neighbours only (the ring of the mapper tests at any size), the key
//! points with noise, verified pairs holding a share of wrong matches, and the positions of the
//! photos with noise. Everything is drawn from a 64-bit linear congruential generator seeded by the
//! caller: a ring is the same on every computer.

use nalgebra::{Matrix3, Rotation3, Vector3};

use super::input::{CameraIn, ImageIn, Input, PairIn};
use super::positioning::Gnss;

pub const FOCAL: f64 = 1000.0;
pub const WIDTH: f64 = 2000.0;
pub const HEIGHT: f64 = 1500.0;
/// The height of the flight, and the metres of ring per photo (the circle of 100 photos is 600 m
/// across).
pub const FLIGHT_HEIGHT: f64 = 40.0;
pub const DIAMETER_PER_PHOTO: f64 = 6.0;
/// The noise of the key points (pixels), the share of wrong matches of every pair, the noise of
/// the positions (metres, in plan and in height) and the accuracy recorded for them.
pub const NOISE_PX: f64 = 0.5;
pub const WRONG_SHARE: f64 = 0.1;
pub const GNSS_NOISE: [f64; 2] = [1.0, 2.0];
pub const GNSS_SIGMA: [f64; 3] = [5.0, 5.0, 10.0];

/// Knuth's MMIX linear congruential generator.
struct Random(u64);

impl Random {
    fn unit(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }

    fn uniform_in(&mut self, low: f64, high: f64) -> f64 {
        low + (high - low) * self.unit()
    }

    fn normal(&mut self) -> f64 {
        let u1 = 1.0 - self.unit();
        let u2 = self.unit();
        (-2.0 * u1.ln()).sqrt() * (std::f64::consts::TAU * u2).cos()
    }

    fn below(&mut self, n: usize) -> usize {
        ((self.unit() * n as f64) as usize).min(n.saturating_sub(1))
    }
}

/// A ring with its truth: the input of the mapper, the positions of its photos, the true centres,
/// the true points, the point each key point of each photo sees, the diameter.
pub struct Ring {
    pub input: Input,
    pub gnss: Gnss,
    pub centres: Vec<[f64; 3]>,
    pub points: Vec<[f64; 3]>,
    pub point_of: Vec<Vec<usize>>,
    pub diameter: f64,
}

/// The exact pixel of `x` in the photo of `cam_from_world` rotation `r` at `c`, `None` behind it or
/// outside it.
fn pixel_of(r: &Matrix3<f64>, c: &Vector3<f64>, x: &Vector3<f64>) -> Option<[f64; 2]> {
    let p = r * (x - c);
    if p.z <= 0.0 {
        return None;
    }
    let px = [FOCAL * p.x / p.z + WIDTH / 2.0, FOCAL * p.y / p.z + HEIGHT / 2.0];
    (px[0] > 1.0 && px[0] < WIDTH - 1.0 && px[1] > 1.0 && px[1] < HEIGHT - 1.0).then_some(px)
}

/// A ring of `photos` photos (8 or more) `DIAMETER_PER_PHOTO * photos` across at `FLIGHT_HEIGHT`,
/// each photo's x axis along the circle but for a tilt of up to 3 degrees and a yaw of up to 10;
/// `points_per_photo / 4` points (rounded up) per window of four consecutive photos on the ground
/// under the middle of the window, seen by those four only; the pairs of neighbours and of second
/// neighbours verified with their matches. One SIMPLE_PINHOLE camera of known focal length.
pub fn ring(photos: usize, points_per_photo: usize, seed: u64) -> Result<Ring, String> {
    if photos < 8 {
        return Err(format!("a ring of {photos} photos: 8 or more are needed"));
    }
    if points_per_photo < 4 {
        return Err(format!("{points_per_photo} points per photo: 4 or more are needed"));
    }
    let mut random = Random(seed ^ 0x9E37_79B9_7F4A_7C15);
    let n = photos;
    let diameter = DIAMETER_PER_PHOTO * n as f64;
    let radius = diameter / 2.0;
    let angle = |i: f64| std::f64::consts::TAU * i / n as f64;
    let mut rotations = Vec::with_capacity(n);
    let mut centres = Vec::with_capacity(n);
    for i in 0..n {
        let (s, c) = angle(i as f64).sin_cos();
        let along = Matrix3::new(-s, c, 0.0, c, s, 0.0, 0.0, 0.0, -1.0);
        let (roll, pitch) = (random.uniform_in(-3.0, 3.0), random.uniform_in(-3.0, 3.0));
        let yaw = random.uniform_in(-10.0, 10.0);
        let tilt = Rotation3::from_euler_angles(roll.to_radians(), pitch.to_radians(), yaw.to_radians());
        rotations.push(tilt.into_inner() * along);
        centres.push(Vector3::new(radius * c, radius * s, FLIGHT_HEIGHT));
    }
    let per_window = points_per_photo.div_ceil(4);
    let mut points = Vec::with_capacity(n * per_window);
    let mut views: Vec<Vec<(usize, [f64; 2])>> = vec![Vec::new(); n];
    for k in 0..n {
        let window: [usize; 4] = std::array::from_fn(|d| (k + d) % n);
        let mut placed = 0;
        let mut attempts = 0;
        while placed < per_window {
            attempts += 1;
            if attempts > 100 * per_window {
                return Err(format!("no ground under the window of photo {k}"));
            }
            let (s, c) = angle(k as f64 + 1.5 + random.uniform_in(-0.5, 0.5)).sin_cos();
            let rho = radius + random.uniform_in(-0.4, 0.4) * FLIGHT_HEIGHT;
            let x = Vector3::new(rho * c, rho * s, random.uniform_in(-2.0, 2.0));
            let pixels: Vec<Option<[f64; 2]>> =
                window.iter().map(|&i| pixel_of(&rotations[i], &centres[i], &x)).collect();
            if pixels.iter().any(Option::is_none) {
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
    let mut keypoints = Vec::new();
    let mut images = Vec::with_capacity(n);
    for (i, view) in views.iter().enumerate() {
        let start = keypoints.len();
        for &(_, px) in view {
            keypoints.push([(px[0] + NOISE_PX * random.normal()) as f32, (px[1] + NOISE_PX * random.normal()) as f32]);
        }
        let name = format!("ring_{i:05}.jpg");
        images.push(ImageIn { name, image_id: i as u32 + 1, camera: 0, keypoints: start..keypoints.len() });
    }
    let cameras = vec![CameraIn {
        camera_id: 1,
        model: "SIMPLE_PINHOLE".into(),
        params: vec![FOCAL, WIDTH / 2.0, HEIGHT / 2.0],
        width: WIDTH,
        height: HEIGHT,
        prior_focal: true,
    }];
    let mut pairs = Vec::new();
    let mut matches = Vec::new();
    for i in 0..n {
        for d in [1, 2] {
            let j = (i + d) % n;
            let (a, b) = (i.min(j), i.max(j));
            let common: Vec<(u32, u32)> = views[a]
                .iter()
                .enumerate()
                .filter_map(|(ka, &(p, _))| {
                    views[b].binary_search_by_key(&p, |&(q, _)| q).ok().map(|kb| (ka as u32, kb as u32))
                })
                .collect();
            let start = matches.len();
            for &(ka, kb) in &common {
                if random.unit() >= WRONG_SHARE {
                    matches.push([ka, kb]);
                    continue;
                }
                let right = views[b][kb as usize].1;
                loop {
                    let other = random.below(views[b].len());
                    let px = views[b][other].1;
                    if (px[0] - right[0]).hypot(px[1] - right[1]) >= 100.0 {
                        matches.push([ka, other as u32]);
                        break;
                    }
                }
            }
            pairs.push(PairIn { a, b, matches: start..matches.len(), config: 2, f: None });
        }
    }
    let mut gnss = Gnss { image: Vec::new(), xyz: Vec::new(), sigma: Vec::new(), flight: Vec::new() };
    for (i, c) in centres.iter().enumerate() {
        let (plan, height) = (GNSS_NOISE[0], GNSS_NOISE[1]);
        let noise = [plan * random.normal(), plan * random.normal(), height * random.normal()];
        gnss.image.push(i);
        gnss.xyz.push([c.x + noise[0], c.y + noise[1], c.z + noise[2]]);
        gnss.sigma.push(GNSS_SIGMA);
        gnss.flight.push(1);
    }
    let point_of = views.iter().map(|v| v.iter().map(|&(p, _)| p).collect()).collect();
    Ok(Ring {
        input: Input { images, cameras, keypoints, pairs, matches },
        gnss,
        centres: centres.iter().map(|c| [c.x, c.y, c.z]).collect(),
        points: points.iter().map(|x| [x.x, x.y, x.z]).collect(),
        point_of,
        diameter,
    })
}

/// The largest residual of `est` after the similarity (Umeyama's) that best takes it onto `truth`;
/// `None` with fewer than three points or a degenerate set.
pub fn shape_residual(est: &[[f64; 3]], truth: &[[f64; 3]]) -> Option<f64> {
    let n = est.len();
    if n < 3 || truth.len() != n {
        return None;
    }
    let e: Vec<Vector3<f64>> = est.iter().map(|x| Vector3::from(*x)).collect();
    let t: Vec<Vector3<f64>> = truth.iter().map(|x| Vector3::from(*x)).collect();
    let (me, mt) = (e.iter().sum::<Vector3<f64>>() / n as f64, t.iter().sum::<Vector3<f64>>() / n as f64);
    let mut cov = Matrix3::zeros();
    let mut var = 0.0;
    for (a, b) in e.iter().zip(&t) {
        cov += (b - mt) * (a - me).transpose();
        var += (a - me).norm_squared();
    }
    if !(var > 0.0) {
        return None;
    }
    let svd = cov.svd(true, true);
    let (u, vt) = (svd.u?, svd.v_t?);
    let mut d = Matrix3::identity();
    if (u * vt).determinant() < 0.0 {
        d[(2, 2)] = -1.0;
    }
    let r = u * d * vt;
    let s = svd.singular_values.component_mul(&d.diagonal()).sum() / var;
    Some(e.iter().zip(&t).map(|(a, b)| (s * r * (a - me) + mt - b).norm()).fold(0.0, f64::max))
}
