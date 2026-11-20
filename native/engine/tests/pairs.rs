//! The two-stage matching of a pair (`pairs`) on synthetic pairs - two pinhole cameras of 2000 x
//! 1500 pixels, points in front of both with the scale of each key point from its depth,
//! descriptors from a seeded generator, the twins noisy copies - and the match store: a good pair
//! goes guided along the epipolar lines and finds the true matches, a pair whose large-scale
//! points have no twin goes to brute force when preselected and is dropped otherwise, a photo
//! without points is dropped, the cap keeps the best matches, the store round-trips, and
//! `match_all` gives the same results on the CPU, on every GPU adapter and with a device that
//! fails in the middle of the run.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use nalgebra::{Matrix3, Quaternion, UnitQuaternion, Vector3};
use ppg_engine::camera::Camera;
use ppg_engine::geometry::two_view::{self, Config, TwoView};
use ppg_engine::gpu::{self, Matcher, DIM};
use ppg_engine::matching;
use ppg_engine::pairs::{self, Device, Options, PairResult, Route};
use ppg_engine::store::{ExtractParams, FeatureStore, FeatureView, Impl, MatchStore, PhotoFeatures};

/// The norm of a descriptor before it is rounded to bytes: COLMAP's.
const NORM: f64 = 512.0;
const FOCAL: f64 = 1000.0;
const CX: f64 = 1000.0;
const CY: f64 = 750.0;
const WIDTH: f64 = 2000.0;
const HEIGHT: f64 = 1500.0;
/// The scale of a key point at a depth of one unit: the scale is this over the depth.
const SCALE_AT_UNIT: f64 = 40.0;
/// The noise on a projected key point, in pixels.
const PIXEL_NOISE: f64 = 0.3;

/// Knuth's MMIX linear congruential generator, written here so that the data depend on no crate
/// and on no version of one (as in ppg-bench).
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

    /// A whole number in [low, high], both included, out of the upper 32 bits.
    fn between(&mut self, low: i32, high: i32) -> i32 {
        let span = (high - low + 1) as u64;
        low + (((self.bits() >> 32) * span) >> 32) as i32
    }

    fn below(&mut self, n: usize) -> usize {
        (self.unit() * n as f64) as usize
    }

    /// Fisher-Yates.
    fn shuffle<T>(&mut self, items: &mut [T]) {
        for i in (1..items.len()).rev() {
            let j = self.below(i + 1);
            items.swap(i, j);
        }
    }
}

/// `n` descriptors as SIFT's bytes: the squares of uniform values (most components small, a few
/// large), a norm of `NORM`, each component rounded and clipped to 255.
fn descriptors(n: usize, random: &mut Lcg) -> Vec<u8> {
    let mut out = Vec::with_capacity(n * DIM);
    for _ in 0..n {
        let values: Vec<f64> = (0..DIM).map(|_| random.unit().powi(2)).collect();
        let norm = values.iter().map(|v| v * v).sum::<f64>().sqrt();
        out.extend(values.iter().map(|v| (v / norm * NORM).round().clamp(0.0, 255.0) as u8));
    }
    out
}

/// `source` moved by noise in [-6, 6] on every byte.
fn noisy(source: &[u8], random: &mut Lcg) -> Vec<u8> {
    source.iter().map(|&v| (i32::from(v) + random.between(-6, 6)).clamp(0, 255) as u8).collect()
}

/// A fresh folder for the fixtures of one test, emptied if a failed run left it.
fn folder(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("pairs_{name}"));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn skew(v: &Vector3<f64>) -> Matrix3<f64> {
    Matrix3::new(0.0, -v.z, v.y, v.z, 0.0, -v.x, -v.y, v.x, 0.0)
}

/// The rotation of `angle` radians about the unit vector `axis` (Rodrigues).
fn rotation(axis: &Vector3<f64>, angle: f64) -> Matrix3<f64> {
    let k = skew(axis);
    Matrix3::identity() + k * angle.sin() + k * k * (1.0 - angle.cos())
}

fn camera() -> Camera {
    Camera::new("SIMPLE_PINHOLE", &[FOCAL, CX, CY], WIDTH, HEIGHT).unwrap()
}

fn calibration() -> Matrix3<f64> {
    Matrix3::new(FOCAL, 0.0, CX, 0.0, FOCAL, CY, 0.0, 0.0, 1.0)
}

/// The pixel of the point `p` in the camera at the origin.
fn project(p: &Vector3<f64>) -> (f64, f64) {
    (FOCAL * p.x / p.z + CX, FOCAL * p.y / p.z + CY)
}

fn inside(x: f64, y: f64) -> bool {
    x > 50.0 && x < WIDTH - 50.0 && y > 50.0 && y < HEIGHT - 50.0
}

fn features(keypoints: Vec<[f32; 4]>, descriptors: Vec<u8>) -> PhotoFeatures {
    PhotoFeatures { width: WIDTH as u32, height: HEIGHT as u32, implementation: Impl::Cpu, image_id: None,
                    params: ExtractParams::default(), keypoints, descriptors }
}

/// A pair of photos: the second camera a small rotation and a translation of 0.8 units along x
/// from the first (`r`, `t` are cam2_from_cam1), `shared` points seen by both at a depth in
/// `shared_depth` (the key points with `PIXEL_NOISE`, the descriptors noisy copies of one
/// descriptor), `own` points of each photo alone at a depth in `own_depth` (descriptors of their
/// own), the scale of every key point `SCALE_AT_UNIT` over its depth; the points of both photos
/// shuffled. `truth` are the true correspondences (index in A, index in B).
struct Scene {
    a: PhotoFeatures,
    b: PhotoFeatures,
    truth: Vec<[u32; 2]>,
    r: Matrix3<f64>,
    t: Vector3<f64>,
}

fn pair_scene(seed: u64, shared: usize, shared_depth: (f64, f64), own: usize, own_depth: (f64, f64)) -> Scene {
    let mut random = Lcg(seed);
    let axis = Vector3::new(random.uniform_in(-1.0, 1.0), random.uniform_in(-1.0, 1.0), random.uniform_in(-1.0, 1.0))
        .normalize();
    let r = rotation(&axis, random.uniform_in(0.05, 0.1));
    let t = Vector3::new(0.8, random.uniform_in(-0.1, 0.1), random.uniform_in(-0.05, 0.05));
    let scale = |z: f64| (SCALE_AT_UNIT / z) as f32;
    let (mut kp_a, mut kp_b) = (Vec::new(), Vec::new());
    let (mut desc_a, mut desc_b) = (Vec::new(), Vec::new());
    while kp_a.len() < shared {
        let (x, y) = (random.uniform_in(50.0, WIDTH - 50.0), random.uniform_in(50.0, HEIGHT - 50.0));
        let z = random.uniform_in(shared_depth.0, shared_depth.1);
        let p = Vector3::new((x - CX) / FOCAL * z, (y - CY) / FOCAL * z, z);
        let q = r * p + t;
        let (xb, yb) = project(&q);
        if q.z <= 0.0 || !inside(xb, yb) {
            continue;
        }
        kp_a.push([(x + PIXEL_NOISE * random.normal()) as f32, (y + PIXEL_NOISE * random.normal()) as f32, scale(z),
                   0.0]);
        kp_b.push([(xb + PIXEL_NOISE * random.normal()) as f32, (yb + PIXEL_NOISE * random.normal()) as f32,
                   scale(q.z), 0.0]);
        let base = descriptors(1, &mut random);
        desc_a.extend(noisy(&base, &mut random));
        desc_b.extend(noisy(&base, &mut random));
    }
    for (kp, desc) in [(&mut kp_a, &mut desc_a), (&mut kp_b, &mut desc_b)] {
        for _ in 0..own {
            let z = random.uniform_in(own_depth.0, own_depth.1);
            kp.push([random.uniform_in(0.0, WIDTH) as f32, random.uniform_in(0.0, HEIGHT) as f32, scale(z), 0.0]);
        }
        desc.extend(descriptors(own, &mut random));
    }
    // the photos shuffled: the true correspondence of a point is no longer its own index
    let (mut order_a, mut order_b): (Vec<usize>, Vec<usize>) = ((0..kp_a.len()).collect(), (0..kp_b.len()).collect());
    random.shuffle(&mut order_a);
    random.shuffle(&mut order_b);
    let permute = |order: &[usize], kp: &[[f32; 4]], desc: &[u8]| -> PhotoFeatures {
        let keypoints = order.iter().map(|&i| kp[i]).collect();
        let descriptors = order.iter().flat_map(|&i| desc[i * DIM..(i + 1) * DIM].iter().copied()).collect();
        features(keypoints, descriptors)
    };
    let a = permute(&order_a, &kp_a, &desc_a);
    let b = permute(&order_b, &kp_b, &desc_b);
    // where every original point went
    let (mut at_a, mut at_b) = (vec![0u32; kp_a.len()], vec![0u32; kp_b.len()]);
    for (new, &old) in order_a.iter().enumerate() {
        at_a[old] = new as u32;
    }
    for (new, &old) in order_b.iter().enumerate() {
        at_b[old] = new as u32;
    }
    let truth = (0..shared).map(|i| [at_a[i], at_b[i]]).collect();
    Scene { a, b, truth, r, t }
}

impl Scene {
    /// The fundamental matrix of the pair, `x_b^T F x_a = 0`.
    fn fundamental(&self) -> Matrix3<f64> {
        let k_inverse = calibration().try_inverse().unwrap();
        k_inverse.transpose() * skew(&self.t) * self.r * k_inverse
    }

    /// The distance of the point of B of the match `m` from the epipolar line of its point of A.
    fn epipolar_distance(&self, m: [u32; 2]) -> f64 {
        let (pa, pb) = (self.a.keypoints[m[0] as usize], self.b.keypoints[m[1] as usize]);
        let l = self.fundamental() * Vector3::new(f64::from(pa[0]), f64::from(pa[1]), 1.0);
        (l.x * f64::from(pb[0]) + l.y * f64::from(pb[1]) + l.z).abs() / (l.x * l.x + l.y * l.y).sqrt()
    }
}

/// The two photos of `scene` put into a feature store in `dir`, and their views.
fn views(dir: &Path, scene: &Scene) -> (FeatureStore, FeatureView, FeatureView) {
    let store = FeatureStore::create(dir).unwrap();
    store.put("a.jpg", &scene.a).unwrap();
    store.put("b.jpg", &scene.b).unwrap();
    let a = store.get("a.jpg").unwrap();
    let b = store.get("b.jpg").unwrap();
    (store, a, b)
}

/// The options of the tests: COLMAP's defaults of the three tests, the thresholds of the routes
/// as the program passes them, the first stage on the `stage1_points` largest points.
fn options(stage1_points: usize) -> Options {
    Options { stage1_points, trust_inliers: 40, drop_inliers: 8, max_ratio: 0.8, max_distance: 0.7, cross_check: true,
              max_num_matches: 32768, two_view: two_view::Options::default() }
}

/// The `bests` of the CPU twin, ignoring the keys.
fn cpu(_: u64, a: &[u8], _: u64, b: &[u8]) -> Result<Vec<[u32; 3]>, String> {
    Ok(matching::bests_cpu(a, b))
}

/// How many of `truth` are among `matches`.
fn found(truth: &[[u32; 2]], matches: &[[u32; 2]]) -> usize {
    let set: HashSet<[u32; 2]> = matches.iter().copied().collect();
    truth.iter().filter(|m| set.contains(*m)).count()
}

/// The integer dot product of two descriptors.
fn dot(x: &[u8], y: &[u8]) -> u32 {
    x.iter().zip(y).map(|(&p, &q)| u32::from(p) * u32::from(q)).sum()
}

#[test]
fn a_good_pair_goes_guided_and_finds_the_true_matches() {
    let dir = folder("good");
    // 2,500 points seen by both, 4 to 6 units away; 500 of each photo's own, farther (smaller
    // scales): the 1,000 largest points of each photo are shared ones
    let scene = pair_scene(11, 2500, (4.0, 6.0), 500, (8.0, 12.0));
    let (_store, a, b) = views(&dir, &scene);
    let o = options(1000);
    let cam = camera();
    let result = pairs::match_pair(&a, &b, "a.jpg", "b.jpg", &cam, &cam, true, false, &o, &mut cpu).unwrap();
    assert_eq!(result.route, Route::Guided);
    assert_eq!(result.two_view.config, Config::Calibrated);
    let n = scene.truth.len();
    let among_matches = found(&scene.truth, &result.matches);
    assert!(among_matches * 100 >= n * 95, "{among_matches} of {n} true correspondences among the matches");
    let among_inliers = found(&scene.truth, &result.two_view.inliers);
    assert!(among_inliers * 100 >= n * 95, "{among_inliers} of {n} true correspondences among the inliers");
    assert!(result.matches.len() <= n + n / 20, "{} matches for {n} true correspondences", result.matches.len());
    // every match lies within the band around its epipolar line
    let max_error = o.two_view.ransac.max_error;
    for m in &result.matches {
        let d = scene.epipolar_distance(*m);
        assert!(d <= max_error, "match {m:?} is {d:.2} px from its epipolar line");
    }
    assert!(result.two_view.e.is_some() && result.two_view.f.is_some());
    // the same again: the route and the result are deterministic
    let again = pairs::match_pair(&a, &b, "a.jpg", "b.jpg", &cam, &cam, true, false, &o, &mut cpu).unwrap();
    assert_eq!(again, result);
    // without a prior focal length the pair is guided by the fundamental matrix of the first stage
    let plain = pairs::match_pair(&a, &b, "a.jpg", "b.jpg", &cam, &cam, false, false, &o, &mut cpu).unwrap();
    assert_eq!((plain.route, plain.two_view.config), (Route::Guided, Config::Uncalibrated));
    assert!(found(&scene.truth, &plain.two_view.inliers) * 100 >= n * 95);
    // the first stage on every point gives the guided route too
    let every = pairs::match_pair(&a, &b, "a.jpg", "b.jpg", &cam, &cam, true, false, &options(8000), &mut cpu).unwrap();
    assert_eq!(every.route, Route::Guided);
    assert!(found(&scene.truth, &every.two_view.inliers) * 100 >= n * 95);
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn a_pair_without_large_scale_matches_goes_to_brute_force() {
    let dir = folder("brute");
    // the 2,000 shared points are far (small scales); the 1,000 points of each photo's own are
    // close (the largest scales): the first stage, on the 1,000 largest of each, sees no twin
    let scene = pair_scene(12, 2000, (8.0, 12.0), 1000, (2.0, 3.0));
    let (_store, a, b) = views(&dir, &scene);
    let o = options(1000);
    let cam = camera();
    let dropped = pairs::match_pair(&a, &b, "a.jpg", "b.jpg", &cam, &cam, true, false, &o, &mut cpu).unwrap();
    assert_eq!(dropped.route, Route::Dropped);
    assert_eq!(dropped.two_view.config, Config::Undefined);
    assert!(dropped.matches.is_empty() && dropped.two_view.inliers.is_empty());
    assert!(dropped.two_view.f.is_none() && dropped.two_view.e.is_none() && dropped.two_view.h.is_none());
    let brute = pairs::match_pair(&a, &b, "a.jpg", "b.jpg", &cam, &cam, true, true, &o, &mut cpu).unwrap();
    assert_eq!(brute.route, Route::BruteForce);
    assert_eq!(brute.two_view.config, Config::Calibrated);
    let n = scene.truth.len();
    let among_inliers = found(&scene.truth, &brute.two_view.inliers);
    assert!(among_inliers * 100 >= n * 95, "{among_inliers} of {n} true correspondences among the inliers");
    // brute force is the matching of every point: the matches are those of the CPU twin
    let bests = matching::bests_cpu(a.descriptors(), b.descriptors());
    let n_a = a.keypoints().len();
    let n_b = b.keypoints().len();
    assert_eq!(brute.matches, gpu::select_matches(&bests, n_a, n_b, 0.8, 0.7, true));
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn a_photo_without_points_is_not_verified() {
    let dir = folder("empty");
    let mut scene = pair_scene(13, 300, (4.0, 6.0), 100, (8.0, 12.0));
    scene.b = features(Vec::new(), Vec::new());
    let (store, a, b) = views(&dir, &scene);
    assert_eq!(b.keypoints().len(), 0);
    let o = options(1000);
    let cam = camera();
    for preselected in [false, true] {
        let result = pairs::match_pair(&a, &b, "a.jpg", "b.jpg", &cam, &cam, true, preselected, &o, &mut cpu).unwrap();
        assert_eq!(result.route, Route::Dropped, "preselected: {preselected}");
        assert_eq!(result.two_view.config, Config::Undefined);
        assert!(result.matches.is_empty() && result.two_view.inliers.is_empty());
        // the empty photo on the other side too
        let result = pairs::match_pair(&b, &a, "b.jpg", "a.jpg", &cam, &cam, true, preselected, &o, &mut cpu).unwrap();
        assert_eq!(result.route, Route::Dropped, "preselected: {preselected}");
    }
    drop((a, b, store));
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn the_cap_keeps_the_best_matches() {
    let dir = folder("cap");
    let scene = pair_scene(14, 2500, (4.0, 6.0), 500, (8.0, 12.0));
    let (_store, a, b) = views(&dir, &scene);
    let cam = camera();
    let all = Options { max_num_matches: usize::MAX, ..options(1000) };
    let whole = pairs::match_pair(&a, &b, "a.jpg", "b.jpg", &cam, &cam, true, false, &all, &mut cpu).unwrap();
    assert!(whole.matches.len() > 1000, "{} matches", whole.matches.len());
    let hundred = Options { max_num_matches: 100, ..all };
    let capped = pairs::match_pair(&a, &b, "a.jpg", "b.jpg", &cam, &cam, true, false, &hundred, &mut cpu).unwrap();
    assert_eq!(capped.matches.len(), 100);
    assert_eq!(capped.route, Route::Guided);
    // the hundred kept are among the matches of the whole run, and no match left out has a
    // smaller distance (a larger dot product) than any of them
    let score = |m: &[u32; 2]| {
        let (i, j) = (m[0] as usize, m[1] as usize);
        dot(&a.descriptors()[i * DIM..(i + 1) * DIM], &b.descriptors()[j * DIM..(j + 1) * DIM])
    };
    let kept: HashSet<[u32; 2]> = capped.matches.iter().copied().collect();
    assert!(capped.matches.iter().all(|m| whole.matches.contains(m)));
    let worst_kept = capped.matches.iter().map(score).min().unwrap();
    let best_left = whole.matches.iter().filter(|m| !kept.contains(*m)).map(score).max().unwrap();
    assert!(best_left <= worst_kept, "a match of score {best_left} was left out for one of {worst_kept}");
    // the verification ran on the hundred
    assert!(capped.two_view.inliers.iter().all(|m| kept.contains(m)));
    assert!(capped.two_view.inliers.len() >= 90, "{} inliers of 100", capped.two_view.inliers.len());
    // in the order of the points of A, as every matcher of the program gives them
    assert!(capped.matches.windows(2).all(|w| w[0][0] < w[1][0]));
    fs::remove_dir_all(&dir).unwrap();
}

/// A pair result with every kind of content: `n` matches, the first `inliers` of them inliers,
/// random models and a pose when asked.
fn result(random: &mut Lcg, n: usize, inliers: usize, config: Config, route: Route, models: bool, pose: bool)
          -> PairResult {
    let matches: Vec<[u32; 2]> = (0..n).map(|i| [i as u32 * 3, random.below(50000) as u32]).collect();
    let matrix = |random: &mut Lcg| Matrix3::from_fn(|_, _| random.uniform_in(-1.0, 1.0));
    let (e, f, h) = if models {
        (Some(matrix(random)), Some(matrix(random)), Some(matrix(random)))
    } else {
        (None, None, None)
    };
    let cam2_from_cam1 = pose.then(|| {
        let q = Quaternion::new(random.normal(), random.normal(), random.normal(), random.normal());
        (UnitQuaternion::from_quaternion(q), Vector3::new(random.normal(), random.normal(), random.normal()))
    });
    let two_view = TwoView { config, inliers: matches[..inliers].to_vec(), e, f, h, cam2_from_cam1 };
    PairResult { matches, two_view, route }
}

#[test]
fn the_store_round_trips() {
    let dir = folder("store");
    let store = MatchStore::create(&dir).unwrap();
    assert!(store.pairs().is_empty() && store.get("a", "b").is_none() && store.is_empty());
    let mut random = Lcg(5);
    let guided = result(&mut random, 2000, 1500, Config::Calibrated, Route::Guided, true, true);
    let brute = result(&mut random, 300, 20, Config::Uncalibrated, Route::BruteForce, true, false);
    let dropped = result(&mut random, 0, 0, Config::Undefined, Route::Dropped, false, false);
    let imported = result(&mut random, 40, 16, Config::Planar, Route::Imported, false, false);
    let degenerate = result(&mut random, 10, 0, Config::Degenerate, Route::BruteForce, false, false);
    // names as a project has them: a folder, a blank, an upper-case extension
    store.put("sub/IMG 0002.JPG", "b.jpg", &guided).unwrap();
    store.put("a.jpg", "sub/IMG 0002.JPG", &brute).unwrap();
    store.put("a.jpg", "b.jpg", &dropped).unwrap();
    store.put("b.jpg", "c.jpg", &imported).unwrap();
    store.put("c.jpg", "d.jpg", &degenerate).unwrap();
    let names = |store: &MatchStore| store.pairs().into_iter().map(|(a, b)| format!("{a}|{b}")).collect::<Vec<_>>();
    assert_eq!(names(&store), ["a.jpg|b.jpg", "a.jpg|sub/IMG 0002.JPG", "b.jpg|c.jpg", "c.jpg|d.jpg",
                               "sub/IMG 0002.JPG|b.jpg"], "sorted");
    assert_eq!(store.len(), 5);
    assert_eq!(store.get("sub/IMG 0002.JPG", "b.jpg").as_ref(), Some(&guided));
    assert_eq!(store.get("a.jpg", "sub/IMG 0002.JPG").as_ref(), Some(&brute));
    assert_eq!(store.get("a.jpg", "b.jpg").as_ref(), Some(&dropped));
    assert_eq!(store.get("b.jpg", "c.jpg").as_ref(), Some(&imported));
    assert_eq!(store.get("c.jpg", "d.jpg").as_ref(), Some(&degenerate));
    // a pair is the two names in the order they were put; the other order is another pair
    assert!(store.get("b.jpg", "a.jpg").is_none());
    // a pair put again replaces its result
    let again = result(&mut random, 120, 100, Config::Calibrated, Route::Guided, true, true);
    store.put("a.jpg", "b.jpg", &again).unwrap();
    assert_eq!(store.len(), 5);
    assert_eq!(store.get("a.jpg", "b.jpg").as_ref(), Some(&again));
    // dropped without a flush: what was put is on disk all the same
    drop(store);

    let reopened = MatchStore::open(&dir).unwrap();
    assert_eq!(names(&reopened), ["a.jpg|b.jpg", "a.jpg|sub/IMG 0002.JPG", "b.jpg|c.jpg", "c.jpg|d.jpg",
                                  "sub/IMG 0002.JPG|b.jpg"]);
    assert_eq!(reopened.get("sub/IMG 0002.JPG", "b.jpg").as_ref(), Some(&guided));
    assert_eq!(reopened.get("a.jpg", "sub/IMG 0002.JPG").as_ref(), Some(&brute));
    assert_eq!(reopened.get("a.jpg", "b.jpg").as_ref(), Some(&again));
    assert_eq!(reopened.get("b.jpg", "c.jpg").as_ref(), Some(&imported));
    assert_eq!(reopened.get("c.jpg", "d.jpg").as_ref(), Some(&degenerate));
    assert!(reopened.get("missing.jpg", "a.jpg").is_none());
    // an opened store takes more pairs, into files of its own, and a flush puts them on disk
    let more = result(&mut random, 500, 400, Config::PlanarOrPanoramic, Route::Guided, true, true);
    reopened.put("d.jpg", "e.jpg", &more).unwrap();
    reopened.put("a.jpg", "b.jpg", &dropped).unwrap();
    reopened.flush().unwrap();
    let third = MatchStore::open(&dir).unwrap();
    assert_eq!(third.len(), 6);
    assert_eq!(third.get("d.jpg", "e.jpg").as_ref(), Some(&more));
    assert_eq!(third.get("a.jpg", "b.jpg").as_ref(), Some(&dropped));
    assert_eq!(third.get("sub/IMG 0002.JPG", "b.jpg").as_ref(), Some(&guided));
    drop((reopened, third));
    // a store is created once; a folder without one does not open
    assert!(MatchStore::create(&dir).is_err(), "created over an existing store");
    assert!(MatchStore::open(&dir.join("nowhere")).is_err());

    // a file whose magic or version is wrong is refused by name, with what was read
    let index: serde_json::Value = serde_json::from_slice(&fs::read(dir.join("index.json")).unwrap()).unwrap();
    let segments: Vec<String> =
        index["segments"].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_owned()).collect();
    assert!(segments.len() >= 2, "{segments:?}: the pairs of two sessions are in two files at least");
    let path = dir.join(&segments[0]);
    let good = fs::read(&path).unwrap();
    let mut bad = good.clone();
    bad[..4].copy_from_slice(b"PLY\n");
    fs::write(&path, &bad).unwrap();
    let refused = MatchStore::open(&dir).err().expect("a wrong magic is refused").to_string();
    assert!(refused.contains(&segments[0]) && refused.contains("PPGM"), "{refused}");
    let mut bad = good.clone();
    bad[4..8].copy_from_slice(&9u32.to_le_bytes());
    fs::write(&path, &bad).unwrap();
    let refused = MatchStore::open(&dir).err().expect("a wrong version is refused").to_string();
    assert!(refused.contains(&segments[0]) && refused.contains('9'), "{refused}");
    // and a file cut short
    fs::write(&path, &good[..good.len() - 100]).unwrap();
    let refused = MatchStore::open(&dir).err().expect("a short file is refused").to_string();
    assert!(refused.contains(&segments[0]), "{refused}");
    fs::write(&path, &good).unwrap();
    assert!(MatchStore::open(&dir).is_ok());

    // many pairs go into several files, all read back
    let dir_many = folder("store_many");
    let many = MatchStore::create(&dir_many).unwrap();
    let results: Vec<PairResult> =
        (0..400).map(|_| result(&mut random, 2000, 1000, Config::Calibrated, Route::Guided, true, true)).collect();
    for (k, r) in results.iter().enumerate() {
        many.put(&format!("p{k:03}.jpg"), &format!("p{:03}.jpg", k + 1), r).unwrap();
    }
    many.flush().unwrap();
    let files = fs::read_dir(&dir_many).unwrap().filter(|e| {
        e.as_ref().unwrap().path().extension().is_some_and(|x| x == "ppgm")
    }).count();
    assert!(files >= 2, "{files} files for {} pairs", results.len());
    let many = MatchStore::open(&dir_many).unwrap();
    assert_eq!(many.len(), results.len());
    for (k, r) in results.iter().enumerate() {
        assert_eq!(many.get(&format!("p{k:03}.jpg"), &format!("p{:03}.jpg", k + 1)).as_ref(), Some(r), "pair {k}");
    }
    drop(many);
    fs::remove_dir_all(&dir).unwrap();
    fs::remove_dir_all(&dir_many).unwrap();
}

/// Whether an adapter that opens no matcher fails the test: the one `ppg-bench` takes when none
/// is named (the first that is no software rasteriser) and, on a CI runner, every software
/// adapter the job names in `SOFTWARE_ADAPTERS` (`rust.yml`); any other (a GL adapter of a
/// runner that opens no device) is reported and left out.
fn must_open(adapter: &gpu::AdapterInfo, adapters: &[gpu::AdapterInfo]) -> bool {
    let chosen = adapters.iter().find(|a| a.device_type != "Cpu").map(|a| a.index);
    let index = adapter.index.to_string();
    let named = std::env::var("SOFTWARE_ADAPTERS").is_ok_and(|v| v.split_whitespace().any(|w| w == index));
    chosen == Some(adapter.index) || named
}

/// The matcher of `adapter`, or `None` when it opens none and may be left out (`must_open`).
fn matcher_of(adapter: &gpu::AdapterInfo, adapters: &[gpu::AdapterInfo]) -> Option<Matcher> {
    match Matcher::new(Some(adapter.index)) {
        Ok(matcher) => Some(matcher),
        Err(problem) if must_open(adapter, adapters) => {
            panic!("adapter {} ({}) opens no matcher: {problem}", adapter.index, adapter.name)
        }
        Err(problem) => {
            eprintln!("adapter {} ({}) skipped: {problem}", adapter.index, adapter.name);
            None
        }
    }
}

fn photo_name(i: usize) -> String {
    format!("photo {i:02}.jpg")
}

/// The feature store of a scene: `photos` photos in a row, 0.6 units apart along x with a small
/// rotation each, looking at `photos + 2` groups of `group` points 4 to 6 units away; photo `i`
/// sees the groups `i`, `i + 1` and `i + 2` (their points projected with noise, their descriptors
/// noisy copies of the group's, the scale from the depth), plus `alone` points of its own anywhere
/// in the photo, 8 to 12 units away (smaller scales), with descriptors of their own. So photo `i`
/// shares points with photos `i + 1` (two groups) and `i + 2` (one group) and with no other.
fn scene(dir: &Path, photos: usize, group: usize, alone: usize) -> FeatureStore {
    let mut random = Lcg(31);
    let groups: Vec<(Vec<Vector3<f64>>, Vec<u8>)> = (0..photos + 2)
        .map(|k| {
            let centre = 0.6 * k as f64 + 0.6;
            let points = (0..group)
                .map(|_| {
                    Vector3::new(centre + random.uniform_in(-1.5, 1.5), random.uniform_in(-1.5, 1.5),
                                 random.uniform_in(4.0, 6.0))
                })
                .collect();
            (points, descriptors(group, &mut random))
        })
        .collect();
    let store = FeatureStore::create(dir).unwrap();
    for i in 0..photos {
        let axis = Vector3::new(random.uniform_in(-1.0, 1.0), random.uniform_in(-1.0, 1.0),
                                random.uniform_in(-1.0, 1.0)).normalize();
        let r = rotation(&axis, random.uniform_in(-0.05, 0.05));
        let t = Vector3::new(-0.6 * i as f64, 0.0, 0.0);
        let mut keypoints: Vec<[f32; 4]> = Vec::new();
        let mut desc: Vec<u8> = Vec::new();
        for k in i..i + 3 {
            let (points, base) = &groups[k];
            for (m, p) in points.iter().enumerate() {
                let q = r * p + t;
                let (x, y) = project(&q);
                let (x, y) = (x + PIXEL_NOISE * random.normal(), y + PIXEL_NOISE * random.normal());
                assert!(x > 0.0 && x < WIDTH && y > 0.0 && y < HEIGHT, "photo {i}: a point of group {k} falls outside");
                keypoints.push([x as f32, y as f32, (SCALE_AT_UNIT / q.z) as f32, 0.0]);
                desc.extend(noisy(&base[m * DIM..(m + 1) * DIM], &mut random));
            }
        }
        for _ in 0..alone {
            let z = random.uniform_in(8.0, 12.0);
            keypoints.push([random.uniform_in(0.0, WIDTH) as f32, random.uniform_in(0.0, HEIGHT) as f32,
                            (SCALE_AT_UNIT / z) as f32, 0.0]);
        }
        desc.extend(descriptors(alone, &mut random));
        store.put(&photo_name(i), &features(keypoints, desc)).unwrap();
    }
    store
}

/// A device that answers with the CPU twin: one breaks at its `fail_at`-th call and raises
/// `broken`; the other waits for `broken` before its first call, so that the broken one has
/// pairs to itself until it fails and its pair must go back to the queue for the patient one.
struct Fake {
    name: &'static str,
    calls: usize,
    fail_at: Option<usize>,
    broken: Arc<AtomicBool>,
}

impl Device for Fake {
    fn name(&self) -> String {
        self.name.to_owned()
    }

    fn bests(&mut self, _: u64, a: &[u8], _: u64, b: &[u8]) -> Result<Vec<[u32; 3]>, String> {
        self.calls += 1;
        if self.fail_at.is_none() && self.calls == 1 {
            let started = Instant::now();
            while !self.broken.load(Ordering::SeqCst) {
                assert!(started.elapsed() < Duration::from_secs(30), "the broken device never broke");
                thread::yield_now();
            }
        }
        if self.fail_at == Some(self.calls) {
            self.broken.store(true, Ordering::SeqCst);
            return Err("broken on purpose".to_owned());
        }
        Ok(matching::bests_cpu(a, b))
    }
}

#[test]
fn match_all_is_the_same_on_every_device() {
    const PHOTOS: usize = 6;
    const GROUP: usize = 200;
    let dir = folder("all");
    let store = scene(&dir.join("features"), PHOTOS, GROUP, 400);
    let names = store.names();
    let cams: HashMap<String, Camera> = names.iter().map(|n| (n.clone(), camera())).collect();
    let calibrated: HashSet<String> = names.iter().cloned().collect();
    // the neighbours preselected, the next ones not; a pair that shares nothing of each kind
    let mut list: Vec<(String, String, bool)> = Vec::new();
    for i in 0..PHOTOS {
        if i + 1 < PHOTOS {
            list.push((photo_name(i), photo_name(i + 1), true));
        }
        if i + 2 < PHOTOS {
            list.push((photo_name(i), photo_name(i + 2), false));
        }
    }
    list.push((photo_name(0), photo_name(PHOTOS - 1), true));
    list.push((photo_name(0), photo_name(PHOTOS - 2), false));
    // in an order of its own: the run sorts the pairs
    Lcg(3).shuffle(&mut list);
    let total = list.len();
    let o = options(400);
    let never = AtomicBool::new(false);
    let calls = Mutex::new(Vec::new());
    let progress = |done: usize, total: usize| calls.lock().unwrap().push((done, total));
    let on_cpu = MatchStore::create(&dir.join("cpu")).unwrap();
    let report = pairs::match_all(&list, &store, &cams, &calibrated, &on_cpu, &[], &o, &never, &progress).unwrap();
    assert_eq!(report.done, total);
    assert_eq!(report.by_route, (2 * PHOTOS - 3, 1, 1), "guided, brute force, dropped");
    assert_eq!(report.stage1_by_device, vec![("CPU".to_owned(), total)]);
    assert!(report.log.is_empty(), "{:?}", report.log);
    let calls = calls.lock().unwrap().clone();
    assert_eq!((calls.first(), calls.last()), (Some(&(0, total)), Some(&(total, total))), "{calls:?}");
    assert!(calls.windows(2).all(|w| w[0].0 <= w[1].0 && w[0].1 == w[1].1), "{calls:?}");
    let mut sorted: Vec<(String, String)> = list.iter().map(|(a, b, _)| (a.clone(), b.clone())).collect();
    sorted.sort();
    assert_eq!(on_cpu.pairs(), sorted);
    let route_of = |a: usize, b: usize| on_cpu.get(&photo_name(a), &photo_name(b)).unwrap();
    for i in 0..PHOTOS {
        for (j, shared) in [(i + 1, 2 * GROUP), (i + 2, GROUP)] {
            if j >= PHOTOS {
                continue;
            }
            let r = route_of(i, j);
            assert_eq!(r.route, Route::Guided, "({i}, {j})");
            assert_eq!(r.two_view.config, Config::Calibrated, "({i}, {j})");
            let inliers = r.two_view.inliers.len();
            assert!(inliers >= shared * 8 / 10, "({i}, {j}): {inliers} inliers of {shared} shared points");
            assert!(r.matches.len() <= shared + shared / 10, "({i}, {j}): {} matches", r.matches.len());
        }
    }
    let nothing = route_of(0, PHOTOS - 1);
    assert_eq!((nothing.route, nothing.two_view.config), (Route::BruteForce, Config::Degenerate));
    assert!(nothing.two_view.inliers.is_empty());
    let dropped = route_of(0, PHOTOS - 2);
    assert_eq!((dropped.route, dropped.two_view.config), (Route::Dropped, Config::Undefined));
    assert!(dropped.matches.is_empty());
    let want: Vec<PairResult> = sorted.iter().map(|(a, b)| on_cpu.get(a, b).unwrap()).collect();
    let same = |other: &MatchStore, what: &str| {
        assert_eq!(other.pairs(), sorted, "{what}");
        for ((a, b), w) in sorted.iter().zip(&want) {
            let got = other.get(a, b).unwrap_or_else(|| panic!("{what}: ({a}, {b}) is not in the store"));
            assert_eq!(&got, w, "{what}: ({a}, {b})");
        }
    };

    // every GPU adapter, alone (no CPU thread): the same results, exactly
    let adapters = gpu::adapters();
    for adapter in &adapters {
        let Some(matcher) = matcher_of(adapter, &adapters) else { continue };
        let on_gpu = MatchStore::create(&dir.join(format!("gpu{}", adapter.index))).unwrap();
        let devices: Vec<Box<dyn Device>> = vec![Box::new(matcher)];
        let what = format!("adapter {} ({})", adapter.index, adapter.name);
        let on = pairs::match_on(&list, &store, &cams, &calibrated, &on_gpu, devices, 0, &o, &never, &|_, _| {})
            .unwrap_or_else(|p| panic!("{what}: {p}"));
        same(&on_gpu, &what);
        assert_eq!((on.done, on.by_route), (total, report.by_route), "{what}");
        assert_eq!(on.stage1_by_device.len(), 1, "{what}: {:?}", on.stage1_by_device);
        assert_eq!(on.stage1_by_device[0].1, total, "{what}: {:?}", on.stage1_by_device);
        assert!(on.stage1_by_device[0].0.contains(&adapter.name), "{what}: {:?}", on.stage1_by_device);
        assert!(on.log.is_empty(), "{what}: {:?}", on.log);
        // through the adapter index, with the CPU threads sharing the queue
        let both = MatchStore::create(&dir.join(format!("both{}", adapter.index))).unwrap();
        let on = pairs::match_all(&list, &store, &cams, &calibrated, &both, &[adapter.index], &o, &never, &|_, _| {})
            .unwrap_or_else(|p| panic!("{what}: {p}"));
        same(&both, &format!("{what} with the CPU"));
        assert_eq!(on.stage1_by_device.iter().map(|(_, n)| n).sum::<usize>(), total, "{what}");
        assert_eq!(on.stage1_by_device.len(), 2, "{what}: {:?}", on.stage1_by_device);
        eprintln!("{what}: the results of the CPU");
    }

    // a device that breaks at its fifth call, beside one that waits for the break before it
    // starts and no CPU thread: the broken device alone takes the pairs in order - three whole
    // pairs (the fourth in that order, which shares nothing, takes two calls) - and the pair it
    // breaks on goes back to the queue for the patient device, which does the rest; the results
    // are the same and the log says what happened
    let broken = Arc::new(AtomicBool::new(false));
    let on_fake = MatchStore::create(&dir.join("fake")).unwrap();
    let devices: Vec<Box<dyn Device>> = vec![
        Box::new(Fake { name: "broken GPU", calls: 0, fail_at: Some(5), broken: broken.clone() }),
        Box::new(Fake { name: "patient GPU", calls: 0, fail_at: None, broken: broken.clone() }),
    ];
    let on = pairs::match_on(&list, &store, &cams, &calibrated, &on_fake, devices, 0, &o, &never, &|_, _| {})
        .unwrap();
    assert!(broken.load(Ordering::SeqCst));
    same(&on_fake, "the fake devices");
    assert_eq!((on.done, on.by_route), (total, report.by_route));
    assert_eq!(on.log.len(), 1, "{:?}", on.log);
    let line = &on.log[0];
    assert!(line.starts_with("broken GPU") && line.contains("broken on purpose") && line.contains("retired"), "{line}");
    assert_eq!(on.stage1_by_device, vec![("broken GPU".to_owned(), 3), ("patient GPU".to_owned(), total - 3)]);
    // no device and no CPU thread is an error before any work
    let nobody = MatchStore::create(&dir.join("nobody")).unwrap();
    let r = pairs::match_on(&list, &store, &cams, &calibrated, &nobody, Vec::new(), 0, &o, &never, &|_, _| {});
    assert!(r.as_ref().is_err_and(|p| p.contains("no worker")), "{r:?}");
    assert!(nobody.is_empty());

    // an adapter that does not exist is logged and the CPU does the work
    let beyond = adapters.len();
    let fallen_back = MatchStore::create(&dir.join("beyond")).unwrap();
    let on = pairs::match_all(&list, &store, &cams, &calibrated, &fallen_back, &[beyond], &o, &never, &|_, _| {})
        .unwrap();
    same(&fallen_back, "an adapter that does not exist");
    assert!(on.log.iter().any(|l| l.contains(&format!("GPU {beyond}"))), "{:?}", on.log);

    // a cancel set before the run, a photo without a camera, a photo the store has not
    let cancelled = AtomicBool::new(true);
    let refused = MatchStore::create(&dir.join("refused")).unwrap();
    let r = pairs::match_all(&list, &store, &cams, &calibrated, &refused, &[], &o, &cancelled, &|_, _| {});
    assert_eq!(r.err().as_deref(), Some("cancelled"));
    assert!(refused.is_empty());
    let mut without = cams.clone();
    without.remove(&photo_name(3));
    let r = pairs::match_all(&list, &store, &without, &calibrated, &refused, &[], &o, &never, &|_, _| {});
    assert!(r.as_ref().is_err_and(|p| p.contains(&photo_name(3))), "{r:?}");
    let unknown = vec![(photo_name(0), "nowhere.jpg".to_owned(), true)];
    let r = pairs::match_all(&unknown, &store, &cams, &calibrated, &refused, &[], &o, &never, &|_, _| {});
    assert!(r.as_ref().is_err_and(|p| p.contains("nowhere.jpg")), "{r:?}");
    drop((on_cpu, on_fake, fallen_back, refused, store));
    fs::remove_dir_all(&dir).unwrap();
}
