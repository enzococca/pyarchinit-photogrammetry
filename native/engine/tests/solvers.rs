//! The minimal solvers of two views (`geometry::solvers`) on random scenes: two cameras of
//! 2000 x 1500 pixels with a focal length of 1000, the second rotated and moved by a known
//! amount, 200 points in front of both. On exact data each solver gives, among its solutions,
//! one that fits every point of the scene and is the truth up to scale; on data with 0.5 px of
//! noise the n-point solvers keep the median error below a pixel; the pose of the essential
//! matrix is the known one; degenerate input gives no solution and no NaN; `Camera::normalised`
//! inverts `pixel` for the six models.

use nalgebra::{Matrix3, Vector3};
use ppg_engine::camera::Camera;
use ppg_engine::geometry::solvers::{
    essential_five_point, fundamental_eight_point, fundamental_seven_point, homography_dlt, homography_four_point,
    pose_from_essential, sampson_sq, transfer_sq,
};

const FOCAL: f64 = 1000.0;
const CX: f64 = 1000.0;
const CY: f64 = 750.0;
const WIDTH: f64 = 2000.0;
const HEIGHT: f64 = 1500.0;
const N_POINTS: usize = 200;
/// The scenes each test runs on: the seeds 1 to `SCENES` of the generator.
const SCENES: u64 = 12;
/// The plane of the planar scenes, `n . X = d` in the frame of the first camera.
const PLANE_NORMAL: [f64; 3] = [-0.3, 0.2, 1.0];
const PLANE_D: f64 = 4.0;

/// Knuth's MMIX linear congruential generator, written here so that the scenes depend on no
/// crate and on no version of one (as in ppg-bench).
struct Lcg(u64);

impl Lcg {
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

    /// A unit vector whose components are drawn from `[-1, 1]` (rejecting the short ones).
    fn direction(&mut self) -> Vector3<f64> {
        loop {
            let v = Vector3::new(self.uniform_in(-1.0, 1.0), self.uniform_in(-1.0, 1.0), self.uniform_in(-1.0, 1.0));
            if v.norm() > 0.5 {
                return v.normalize();
            }
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

/// Normalised coordinates of a pixel (the cameras of the scenes are pinholes).
fn normalised(p: [f64; 2]) -> [f64; 2] {
    [(p[0] - CX) / FOCAL, (p[1] - CY) / FOCAL]
}

/// A scene: the pose of the second camera (cam2_from_cam1, `X2 = R X1 + t`) and the pixels of
/// `N_POINTS` points in both photos, in the same order.
struct Scene {
    r: Matrix3<f64>,
    t: Vector3<f64>,
    x1: Vec<[f64; 2]>,
    x2: Vec<[f64; 2]>,
}

impl Scene {
    /// The second camera rotated by 0.1 to 0.3 rad about a random axis and moved by 0.5 to 1.2
    /// units, mostly sideways; the points in a box 3 to 6 units in front of the first camera, or
    /// on the plane `PLANE_NORMAL . X = PLANE_D` when `planar`; each seen by both cameras.
    fn new(seed: u64, planar: bool) -> Scene {
        let mut random = Lcg(seed);
        let r = rotation(&random.direction(), random.uniform_in(0.1, 0.3));
        let sideways = if random.unit() < 0.5 { -1.0 } else { 1.0 } * random.uniform_in(0.4, 1.0);
        let direction = Vector3::new(sideways, random.uniform_in(-0.3, 0.3), random.uniform_in(-0.3, 0.3)).normalize();
        let t = direction * random.uniform_in(0.5, 1.2);
        let (mut x1, mut x2) = (Vec::new(), Vec::new());
        while x1.len() < N_POINTS {
            let x = random.uniform_in(-1.5, 1.5);
            let y = random.uniform_in(-1.1, 1.1);
            let z = if planar {
                (PLANE_D - PLANE_NORMAL[0] * x - PLANE_NORMAL[1] * y) / PLANE_NORMAL[2]
            } else {
                random.uniform_in(3.0, 6.0)
            };
            let p1 = Vector3::new(x, y, z);
            if let (Some(a), Some(b)) = (pixel(&p1), pixel(&(r * p1 + t))) {
                x1.push(a);
                x2.push(b);
            }
        }
        Scene { r, t, x1, x2 }
    }

    fn essential(&self) -> Matrix3<f64> {
        skew(&self.t) * self.r
    }

    fn fundamental(&self) -> Matrix3<f64> {
        let k_inverse = calibration().try_inverse().unwrap();
        k_inverse.transpose() * self.essential() * k_inverse
    }

    /// The homography of the plane of a planar scene, in pixels.
    fn homography(&self) -> Matrix3<f64> {
        let n = Vector3::from(PLANE_NORMAL);
        let k = calibration();
        k * (self.r + self.t * n.transpose() / PLANE_D) * k.try_inverse().unwrap()
    }

    fn normalised(&self) -> (Vec<[f64; 2]>, Vec<[f64; 2]>) {
        (self.x1.iter().map(|&p| normalised(p)).collect(), self.x2.iter().map(|&p| normalised(p)).collect())
    }

    /// The pixels with Gaussian noise of `sigma` on every coordinate.
    fn noisy(&self, random: &mut Lcg, sigma: f64) -> (Vec<[f64; 2]>, Vec<[f64; 2]>) {
        let jitter = |points: &[[f64; 2]], random: &mut Lcg| -> Vec<[f64; 2]> {
            points.iter().map(|p| [p[0] + sigma * random.normal(), p[1] + sigma * random.normal()]).collect()
        };
        (jitter(&self.x1, random), jitter(&self.x2, random))
    }
}

fn first<const N: usize>(points: &[[f64; 2]]) -> [[f64; 2]; N] {
    points[..N].try_into().unwrap()
}

fn finite(m: &Matrix3<f64>) -> bool {
    m.iter().all(|v| v.is_finite())
}

/// Frobenius distance between `a` and `b` after scaling both to a norm of 1, with the sign that
/// makes it smallest: zero when they are equal up to scale.
fn distance_up_to_scale(a: &Matrix3<f64>, b: &Matrix3<f64>) -> f64 {
    let (a, b) = (a / a.norm(), b / b.norm());
    (a - b).norm().min((a + b).norm())
}

/// The largest Sampson error (not squared) of `f` over the correspondences.
fn largest_sampson(f: &Matrix3<f64>, x1: &[[f64; 2]], x2: &[[f64; 2]]) -> f64 {
    x1.iter().zip(x2).map(|(&a, &b)| sampson_sq(f, a, b).sqrt()).fold(0.0, f64::max)
}

/// The largest transfer error (not squared) of `h` over the correspondences.
fn largest_transfer(h: &Matrix3<f64>, x1: &[[f64; 2]], x2: &[[f64; 2]]) -> f64 {
    x1.iter().zip(x2).map(|(&a, &b)| transfer_sq(h, a, b).sqrt()).fold(0.0, f64::max)
}

fn median(mut values: Vec<f64>) -> f64 {
    values.sort_by(f64::total_cmp);
    values[values.len() / 2]
}

/// Of `solutions`, the one closest to `truth` up to scale: its distance and its residual.
fn closest(solutions: &[Matrix3<f64>], truth: &Matrix3<f64>, residual: impl Fn(&Matrix3<f64>) -> f64) -> (f64, f64) {
    solutions
        .iter()
        .map(|m| (distance_up_to_scale(m, truth), residual(m)))
        .min_by(|a, b| a.0.total_cmp(&b.0))
        .expect("at least one solution")
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

#[test]
fn the_five_point_solver_finds_the_essential_matrix_of_exact_data() {
    for seed in 1..=SCENES {
        // a plane is not degenerate for the five-point solver: it gives two essential matrices
        // that fit every point of the plane, and one of them is the truth
        for planar in [false, true] {
            let scene = Scene::new(seed, planar);
            let (n1, n2) = scene.normalised();
            let solutions = essential_five_point(&first::<5>(&n1), &first::<5>(&n2));
            assert!(
                !solutions.is_empty() && solutions.len() <= 10,
                "scene {seed} (planar {planar}): {} solutions",
                solutions.len()
            );
            assert!(solutions.iter().all(finite), "scene {seed} (planar {planar}): a solution is not finite");
            let (distance, residual) = closest(&solutions, &scene.essential(), |e| largest_sampson(e, &n1, &n2));
            assert!(
                residual < 1e-9,
                "scene {seed} (planar {planar}): the closest solution leaves a Sampson error of {residual}"
            );
            assert!(distance < 1e-8, "scene {seed} (planar {planar}): the closest solution is {distance} off");
        }
    }
}

#[test]
fn the_seven_point_solver_finds_the_fundamental_matrix_of_exact_data() {
    for seed in 1..=SCENES {
        let scene = Scene::new(seed, false);
        let solutions = fundamental_seven_point(&first::<7>(&scene.x1), &first::<7>(&scene.x2));
        assert!(!solutions.is_empty() && solutions.len() <= 3, "scene {seed}: {} solutions", solutions.len());
        assert!(solutions.iter().all(finite), "scene {seed}: a solution is not finite");
        let truth = scene.fundamental();
        let (distance, residual) = closest(&solutions, &truth, |f| largest_sampson(f, &scene.x1, &scene.x2));
        assert!(residual < 1e-9, "scene {seed}: the closest solution leaves a Sampson error of {residual} px");
        assert!(distance < 1e-8, "scene {seed}: the closest solution is {distance} from the truth");
    }
}

#[test]
fn the_eight_point_solver_finds_the_fundamental_matrix_of_exact_data() {
    for seed in 1..=SCENES {
        let scene = Scene::new(seed, false);
        let truth = scene.fundamental();
        // the minimal sample, then every point of the scene
        for n in [8, N_POINTS] {
            let f = fundamental_eight_point(&scene.x1[..n], &scene.x2[..n]).expect("a solution");
            assert!(finite(&f), "scene {seed}, {n} points: the solution is not finite");
            let residual = largest_sampson(&f, &scene.x1, &scene.x2);
            assert!(residual < 1e-9, "scene {seed}, {n} points: a Sampson error of {residual} px");
            let distance = distance_up_to_scale(&f, &truth);
            assert!(distance < 1e-8, "scene {seed}, {n} points: {distance} from the truth");
        }
    }
}

#[test]
fn the_eight_point_solver_fits_noisy_data_within_a_pixel() {
    let mut random = Lcg(777);
    for seed in 1..=SCENES {
        let scene = Scene::new(seed, false);
        let (x1, x2) = scene.noisy(&mut random, 0.5);
        let f = fundamental_eight_point(&x1, &x2).expect("a solution");
        let errors: Vec<f64> = x1.iter().zip(&x2).map(|(&a, &b)| sampson_sq(&f, a, b).sqrt()).collect();
        let middle = median(errors);
        assert!(middle < 1.0, "scene {seed}: a median Sampson error of {middle} px");
    }
}

#[test]
fn the_four_point_solver_and_the_dlt_find_the_homography_of_exact_data() {
    for seed in 1..=SCENES {
        let scene = Scene::new(seed, true);
        let truth = scene.homography();
        let minimal = homography_four_point(&first::<4>(&scene.x1), &first::<4>(&scene.x2)).expect("a solution");
        let all = homography_dlt(&scene.x1, &scene.x2).expect("a solution");
        for (h, what) in [(minimal, "four points"), (all, "every point")] {
            assert!(finite(&h), "scene {seed}, {what}: the solution is not finite");
            let residual = largest_transfer(&h, &scene.x1, &scene.x2);
            assert!(residual < 1e-9, "scene {seed}, {what}: a transfer error of {residual} px");
            let distance = distance_up_to_scale(&h, &truth);
            assert!(distance < 1e-8, "scene {seed}, {what}: {distance} from the truth");
        }
    }
}

#[test]
fn the_dlt_fits_noisy_data_within_a_pixel() {
    let mut random = Lcg(778);
    for seed in 1..=SCENES {
        let scene = Scene::new(seed, true);
        let (x1, x2) = scene.noisy(&mut random, 0.5);
        let h = homography_dlt(&x1, &x2).expect("a solution");
        let errors: Vec<f64> = x1.iter().zip(&x2).map(|(&a, &b)| transfer_sq(&h, a, b).sqrt()).collect();
        let middle = median(errors);
        assert!(middle < 1.0, "scene {seed}: a median transfer error of {middle} px");
    }
}

#[test]
fn pose_from_essential_recovers_the_rotation_and_the_direction_of_the_translation() {
    for seed in 1..=SCENES {
        let scene = Scene::new(seed, false);
        let (n1, n2) = scene.normalised();
        let truth = scene.essential();
        let solutions = essential_five_point(&first::<5>(&n1), &first::<5>(&n2));
        let estimated = solutions
            .iter()
            .min_by(|a, b| distance_up_to_scale(a, &truth).total_cmp(&distance_up_to_scale(b, &truth)))
            .expect("a solution");
        // from the true essential matrix and from the one the five-point solver found
        for (e, what) in [(truth, "the truth"), (*estimated, "the five-point solution")] {
            let (r, t, count) = pose_from_essential(&e, &n1, &n2).expect("a pose");
            assert_eq!(count, N_POINTS, "scene {seed}, {what}: points in front of both cameras");
            let rotation_error = angle_of(&(r * scene.r.transpose()));
            assert!(rotation_error < 1e-6, "scene {seed}, {what}: the rotation is {rotation_error} rad off");
            let direction_error = angle_between(&t, &scene.t);
            assert!(direction_error < 1e-6, "scene {seed}, {what}: the translation is {direction_error} rad off");
            assert!((t.norm() - 1.0).abs() < 1e-12, "scene {seed}, {what}: |t| = {}", t.norm());
        }
    }
}

#[test]
fn degenerate_input_gives_no_solution_and_never_a_nan() {
    // the same point everywhere
    let same = |n: usize| vec![[640.0, 480.0]; n];
    assert!(essential_five_point(&[[0.1, 0.2]; 5], &[[0.1, 0.2]; 5]).is_empty());
    assert!(fundamental_seven_point(&[[640.0, 480.0]; 7], &[[640.0, 480.0]; 7]).is_empty());
    assert!(fundamental_eight_point(&same(8), &same(8)).is_none());
    assert!(fundamental_eight_point(&same(200), &same(200)).is_none());
    assert!(homography_four_point(&[[640.0, 480.0]; 4], &[[640.0, 480.0]; 4]).is_none());
    assert!(homography_dlt(&same(50), &same(50)).is_none());

    // points of a line in space, whose images are lines in both photos
    let r = rotation(&Vector3::y(), 0.05);
    let t = Vector3::new(0.8, 0.1, 0.0);
    let (x1, x2): (Vec<[f64; 2]>, Vec<[f64; 2]>) = (0..N_POINTS)
        .map(|i| {
            let s = i as f64 / (N_POINTS - 1) as f64;
            let p = Vector3::new(-0.5 + s, 0.3 - 0.4 * s, 4.0 + 0.5 * s);
            (pixel(&p).unwrap(), pixel(&(r * p + t)).unwrap())
        })
        .unzip();
    let n1: Vec<[f64; 2]> = x1.iter().map(|&p| normalised(p)).collect();
    let n2: Vec<[f64; 2]> = x2.iter().map(|&p| normalised(p)).collect();
    assert!(essential_five_point(&first::<5>(&n1), &first::<5>(&n2)).is_empty());
    assert!(fundamental_seven_point(&first::<7>(&x1), &first::<7>(&x2)).is_empty());
    assert!(fundamental_eight_point(&x1, &x2).is_none());
    assert!(homography_four_point(&first::<4>(&x1), &first::<4>(&x2)).is_none());
    assert!(homography_dlt(&x1, &x2).is_none());

    // two of the five points the same
    let scene = Scene::new(4, false);
    let (n1, n2) = scene.normalised();
    let (mut s1, mut s2) = (first::<5>(&n1), first::<5>(&n2));
    s1[4] = s1[0];
    s2[4] = s2[0];
    assert!(essential_five_point(&s1, &s2).is_empty());

    // a coordinate that is not a number, too few points, lengths that differ
    let mut bad = scene.x1[..8].to_vec();
    bad[3][0] = f64::NAN;
    assert!(fundamental_eight_point(&bad, &scene.x2[..8]).is_none());
    assert!(fundamental_eight_point(&scene.x1[..7], &scene.x2[..7]).is_none());
    assert!(fundamental_eight_point(&scene.x1[..9], &scene.x2[..8]).is_none());
    assert!(homography_dlt(&scene.x1[..3], &scene.x2[..3]).is_none());
    assert!(homography_dlt(&scene.x1[..5], &scene.x2[..4]).is_none());
    let nan = [[f64::NAN; 2]; 5];
    assert!(essential_five_point(&nan, &nan).is_empty());
    assert!(fundamental_seven_point(&[[f64::INFINITY; 2]; 7], &[[0.0; 2]; 7]).is_empty());

    // the pose of a matrix that is not essential, of a matrix that is not a number, of no points
    assert!(pose_from_essential(&Matrix3::zeros(), &n1, &n2).is_none());
    assert!(pose_from_essential(&Matrix3::from_element(f64::NAN), &n1, &n2).is_none());
    assert!(pose_from_essential(&scene.essential(), &[], &[]).is_none());
    assert!(pose_from_essential(&scene.essential(), &n1[..3], &n2[..2]).is_none());

    // the errors of a point sent to infinity, and of a zero matrix, are not NaN
    let to_infinity = Matrix3::new(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0);
    assert!(transfer_sq(&to_infinity, [0.0, 0.0], [1.0, 1.0]).is_infinite());
    assert!(!sampson_sq(&Matrix3::zeros(), [1.0, 2.0], [3.0, 4.0]).is_nan());
}

/// The six models with the parameter vectors of camera.rs's own tests (private there, copied).
const CASES: [(&str, &[f64]); 6] = [
    ("SIMPLE_PINHOLE", &[1234.5, 655.3, 471.7]),
    ("PINHOLE", &[1234.5, 1240.25, 655.3, 471.7]),
    ("SIMPLE_RADIAL", &[1234.5, 655.3, 471.7, -0.0837]),
    ("RADIAL", &[1234.5, 655.3, 471.7, -0.0837, 0.0121]),
    ("OPENCV", &[1234.5, 1240.25, 655.3, 471.7, -0.0837, 0.0121, 0.00073, -0.00042]),
    (
        "FULL_OPENCV",
        &[1234.5, 1240.25, 655.3, 471.7, -0.0837, 0.0121, 0.00073, -0.00042, 0.0019, 0.047, -0.0112, 0.0023],
    ),
];

#[test]
fn normalised_inverts_pixel_for_the_six_models() {
    for (name, params) in CASES {
        let camera = Camera::new(name, params, 1300.0, 950.0).unwrap();
        // 50 normalised points of [-0.6, 0.6]^2: 10 columns by 5 rows, as camera.rs's grid
        for i in 0..10 {
            for j in 0..5 {
                let (u, v) = (-0.6 + 1.2 * i as f64 / 9.0, -0.6 + 1.2 * j as f64 / 4.0);
                let (x, y) = camera.pixel(u, v);
                let (u1, v1) = camera.normalised(x, y);
                assert!(
                    (u1 - u).abs() < 1e-9 && (v1 - v).abs() < 1e-9,
                    "{name} at ({u}, {v}): the pixel ({x}, {y}) comes back as ({u1}, {v1})"
                );
            }
        }
    }
}
