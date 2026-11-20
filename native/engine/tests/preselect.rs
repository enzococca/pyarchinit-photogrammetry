//! The generic preselection (`preselect`) and the block matching it rests on: `Matcher::bests_block`
//! against `Matcher::bests` pair by pair and against `matching::bests_block_cpu`, on every adapter
//! and both tile sizes; `preselect::run` on twelve synthetic photos that share descriptors with
//! their two neighbours only, with consistent key points under a known geometry, on the CPU and
//! on the GPUs with the same weights; a cancel set before the run.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::sync::Mutex;

use nalgebra::{Matrix3, Vector3};
use ppg_engine::camera::Camera;
use ppg_engine::geometry::two_view;
use ppg_engine::gpu::{self, Matcher, DIM};
use ppg_engine::matching;
use ppg_engine::preselect::{self, MatchOptions, PreselectOptions};
use ppg_engine::store::{ExtractParams, FeatureStore, Impl, PhotoFeatures};

/// The norm of a descriptor before it is rounded to bytes: COLMAP's.
const NORM: f64 = 512.0;

/// The verified matches that make two photos a pair: `sfm.PRESELECTION_INLIERS`, typed here
/// only to pin the Python constant the program passes.
const MIN_INLIERS: usize = 15;

const FOCAL: f64 = 1000.0;
const CX: f64 = 1000.0;
const CY: f64 = 750.0;
const WIDTH: f64 = 2000.0;
const HEIGHT: f64 = 1500.0;
const PHOTOS: usize = 12;
/// The points of one group of the scene, seen by three photos in a row.
const GROUP: usize = 200;
/// The points of a photo that no other photo has.
const ALONE: usize = 400;

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

/// The descriptor `from` of `set` copied over the descriptor `to`.
fn duplicate(set: &mut [u8], from: usize, to: usize) {
    let source: Vec<u8> = set[from * DIM..(from + 1) * DIM].to_vec();
    set[to * DIM..(to + 1) * DIM].copy_from_slice(&source);
}

/// Two blocks of photos: 5 of A and 7 of B, of 65 to 1024 descriptors each, the first 150 of
/// every photo of B (half of a shorter one) noisy copies of the first of a photo of A (so that
/// the blocks hold real matches), and duplicated descriptors in both (ties across tiles and
/// across the threads of a workgroup, in both directions).
fn blocks() -> (Vec<Vec<u8>>, Vec<Vec<u8>>) {
    let mut random = Lcg(2027);
    let sizes_a = [1024, 300, 777, 640, 513];
    let sizes_b = [300, 1024, 129, 1000, 65, 450, 897];
    let mut a: Vec<Vec<u8>> = sizes_a.iter().map(|&n| descriptors(n, &mut random)).collect();
    let mut b: Vec<Vec<u8>> = sizes_b.iter().map(|&n| descriptors(n, &mut random)).collect();
    for (j, photo) in b.iter_mut().enumerate() {
        let shared = 150.min(photo.len() / DIM / 2) * DIM;
        let copies = noisy(&a[j % sizes_a.len()][..shared], &mut random);
        photo[..shared].copy_from_slice(&copies);
    }
    duplicate(&mut a[0], 3, 70);
    duplicate(&mut a[0], 3, 1000);
    duplicate(&mut a[2], 10, 700);
    duplicate(&mut b[1], 5, 64);
    duplicate(&mut b[1], 5, 135);
    duplicate(&mut b[1], 5, 1023);
    duplicate(&mut b[3], 7, 999);
    (a, b)
}

fn refs(photos: &[Vec<u8>]) -> Vec<&[u8]> {
    photos.iter().map(Vec::as_slice).collect()
}

/// `got` equals `want`, or the first entry that differs is named.
fn same(got: &[[u32; 3]], want: &[[u32; 3]], n_a: usize, what: &str) {
    assert_eq!(got.len(), want.len(), "{what}: {} bests instead of {}", got.len(), want.len());
    for (k, (g, w)) in got.iter().zip(want).enumerate() {
        if g != w {
            let (direction, index) = if k < n_a { ("descriptor", k) } else { ("descriptor of B", k - n_a) };
            panic!("{what}: {direction} {index} has (best, index, second) {g:?} instead of {w:?}");
        }
    }
}

/// The bests of every pair of the blocks, as `bests` gives them one pair at a time.
fn pair_by_pair(a: &[Vec<u8>], b: &[Vec<u8>]) -> Vec<Vec<[u32; 3]>> {
    let mut out = Vec::new();
    for x in a {
        for y in b {
            out.push(matching::bests_cpu(x, y));
        }
    }
    out
}

/// `got` (the result of a block call) equals `want` pair by pair.
fn same_block(got: &[Vec<[u32; 3]>], want: &[Vec<[u32; 3]>], a: &[Vec<u8>], b: &[Vec<u8>], what: &str) {
    assert_eq!(got.len(), a.len() * b.len(), "{what}: {} pairs", got.len());
    for (i, x) in a.iter().enumerate() {
        for (j, _) in b.iter().enumerate() {
            let k = i * b.len() + j;
            same(&got[k], &want[k], x.len() / DIM, &format!("{what}, pair ({i}, {j})"));
        }
    }
}

/// The blocks of the edge cases: an empty photo and a photo of one descriptor on either side,
/// sizes around the tile widths of both kernels.
fn edge_blocks() -> (Vec<Vec<u8>>, Vec<Vec<u8>>) {
    let mut random = Lcg(99);
    let a: Vec<Vec<u8>> = [1, 0, 63, 129].iter().map(|&n| descriptors(n, &mut random)).collect();
    let b: Vec<Vec<u8>> = [0, 1, 65, 31, 128].iter().map(|&n| descriptors(n, &mut random)).collect();
    (a, b)
}

#[test]
fn bests_block_cpu_is_bests_pair_by_pair() {
    let (a, b) = blocks();
    let got = matching::bests_block_cpu(&refs(&a), &refs(&b));
    same_block(&got, &pair_by_pair(&a, &b), &a, &b, "the CPU twin");
    // the copies are found: in the pair (0, 0) descriptor k of B is the copy of descriptor k of A
    let pair = &got[0];
    let found = (0..150).filter(|&k| pair[k][1] as usize == k).count();
    assert!(found >= 140, "{found} of 150 copies found in the pair (0, 0)");
    // the forced ties: the lowest index wins and the second best equals the best
    assert_eq!(pair[1024 + 3][1], 3, "descriptor 3 of B_0 against A_0: {:?}", pair[1024 + 3]);
    assert_eq!(pair[1024 + 3][0], pair[1024 + 3][2]);
    let (a, b) = edge_blocks();
    let got = matching::bests_block_cpu(&refs(&a), &refs(&b));
    same_block(&got, &pair_by_pair(&a, &b), &a, &b, "the CPU twin, edge cases");
    // the pairs (1, 0) and (0, 0) have an empty photo; (2, 1) is 63 descriptors against one
    assert!(got[b.len()].is_empty() && got[0].is_empty() && got[2 * b.len() + 1].len() == 64);
    assert!(matching::bests_block_cpu(&[], &refs(&b)).is_empty());
    assert!(matching::bests_block_cpu(&refs(&a), &[]).is_empty());
}

#[test]
fn bests_block_on_the_gpu_is_bests_pair_by_pair_on_every_adapter_and_tile() {
    let adapters = gpu::adapters();
    if adapters.is_empty() {
        eprintln!("no GPU adapter: the block kernel is not compared");
        return;
    }
    let (a, b) = blocks();
    let want = pair_by_pair(&a, &b);
    let (ea, eb) = edge_blocks();
    let want_edges = pair_by_pair(&ea, &eb);
    for adapter in &adapters {
        let most = match Matcher::new(Some(adapter.index)) {
            Ok(matcher) => matcher.workgroup_bytes(),
            Err(problem) => {
                eprintln!("adapter {} ({}) skipped: {problem}", adapter.index, adapter.name);
                continue;
            }
        };
        for bytes in [16384u32, 32768].into_iter().filter(|&bytes| bytes <= most) {
            let mut matcher = Matcher::with_workgroup_bytes(Some(adapter.index), bytes)
                .unwrap_or_else(|p| panic!("adapter {} with {bytes} bytes: {p}", adapter.index));
            let what = format!("{} with {bytes} bytes", adapter.name);
            let got = matcher.bests_block(&refs(&a), &refs(&b)).unwrap_or_else(|p| panic!("{what}: {p}"));
            same_block(&got, &want, &a, &b, &what);
            // the same as the pair kernel of the same matcher, pair by pair
            for (i, x) in a.iter().enumerate() {
                for (j, y) in b.iter().enumerate() {
                    let pair = matcher.bests(0, x, 0, y).unwrap();
                    same(&got[i * b.len() + j], &pair, x.len() / DIM, &format!("{what}, bests of ({i}, {j})"));
                }
            }
            let got = matcher.bests_block(&refs(&ea), &refs(&eb)).unwrap_or_else(|p| panic!("{what}: {p}"));
            same_block(&got, &want_edges, &ea, &eb, &format!("{what}, edge cases"));
            assert!(matcher.bests_block(&[], &refs(&b)).unwrap().is_empty());
            assert!(matcher.bests_block(&refs(&a), &[]).unwrap().is_empty());
            // a block of one photo on each side is the pair
            let one = matcher.bests_block(&[&a[1]], &[&b[2]]).unwrap();
            same(&one[0], &want[1 * b.len() + 2], a[1].len() / DIM, &format!("{what}, one pair"));
            let bad = matcher.bests_block(&[&a[0][..100]], &[&b[0]]).err();
            assert!(bad.is_some_and(|p| p.contains("128 bytes")), "a wrong length is taken");
            eprintln!("adapter {} ({}) with {bytes} bytes: the bests of the pair kernel", adapter.index, adapter.name);
        }
    }
}

/// A fresh folder for the fixtures of one test, emptied if a failed run left it.
fn folder(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("preselect_{name}"));
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

fn photo_name(i: usize) -> String {
    format!("photo {i:02}.jpg")
}

/// The small store of the scene: `PHOTOS` photos in a row, 0.6 units apart along x with a small
/// rotation each, looking at `PHOTOS + 2` groups of `GROUP` points 4 to 6 units away; photo `i`
/// sees the groups `i`, `i + 1` and `i + 2` (their points projected with 0.3 px of noise, their
/// descriptors noisy copies of the group's), plus `ALONE` points of its own anywhere in the photo
/// with descriptors of their own. So photo `i` shares descriptors with photos `i + 1` (two
/// groups) and `i + 2` (one group) and with no other photo.
fn scene(dir: &Path) -> FeatureStore {
    let mut random = Lcg(31);
    let groups: Vec<(Vec<Vector3<f64>>, Vec<u8>)> = (0..PHOTOS + 2)
        .map(|k| {
            let centre = 0.6 * k as f64 + 0.6;
            let points = (0..GROUP)
                .map(|_| {
                    Vector3::new(centre + random.uniform_in(-1.5, 1.5), random.uniform_in(-1.5, 1.5),
                                 random.uniform_in(4.0, 6.0))
                })
                .collect();
            (points, descriptors(GROUP, &mut random))
        })
        .collect();
    let store = FeatureStore::create(dir).unwrap();
    for i in 0..PHOTOS {
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
                let x = FOCAL * q.x / q.z + CX + 0.3 * random.normal();
                let y = FOCAL * q.y / q.z + CY + 0.3 * random.normal();
                assert!(x > 0.0 && x < WIDTH && y > 0.0 && y < HEIGHT, "photo {i}: a point of group {k} falls outside");
                keypoints.push([x as f32, y as f32, 2.0, 0.0]);
                desc.extend(noisy(&base[m * DIM..(m + 1) * DIM], &mut random));
            }
        }
        for _ in 0..ALONE {
            keypoints.push([random.uniform_in(0.0, WIDTH) as f32, random.uniform_in(0.0, HEIGHT) as f32, 2.0, 0.0]);
        }
        desc.extend(descriptors(ALONE, &mut random));
        let features = PhotoFeatures { width: WIDTH as u32, height: HEIGHT as u32, implementation: Impl::Cpu,
                                       image_id: None, params: ExtractParams::default(), keypoints, descriptors: desc };
        store.put(&photo_name(i), &features).unwrap();
    }
    store
}

fn options() -> PreselectOptions {
    let matching = MatchOptions { max_ratio: 0.8, max_distance: 0.7, cross_check: true, workgroup_bytes: None };
    PreselectOptions { min_inliers: MIN_INLIERS, matching, two_view: two_view::Options::default() }
}

/// The pairs of the scene: `(i, i + 1)` and `(i, i + 2)`, by name, `a < b`.
fn expected_pairs() -> Vec<(String, String)> {
    let mut pairs = Vec::new();
    for i in 0..PHOTOS {
        for j in [i + 1, i + 2] {
            if j < PHOTOS {
                pairs.push((photo_name(i), photo_name(j)));
            }
        }
    }
    pairs.sort();
    pairs
}

#[test]
fn run_finds_the_pairs_that_share_descriptors_on_the_cpu_and_on_the_gpus_alike() {
    let dir = folder("run");
    let store = scene(&dir);
    let names = store.names();
    let cams: HashMap<String, Camera> = names.iter().map(|n| (n.clone(), camera())).collect();
    let calibrated: HashSet<String> = names.iter().cloned().collect();
    let o = options();
    let never = AtomicBool::new(false);
    let calls = Mutex::new(Vec::new());
    let progress = |done: usize, total: usize| calls.lock().unwrap().push((done, total));
    let report = preselect::run(&store, &cams, &calibrated, &[], &o, &never, &progress).unwrap();
    assert_eq!(report.pairs_tried, PHOTOS * (PHOTOS - 1) / 2);
    let pairs: Vec<(String, String)> = report.weights.iter().map(|(a, b, _)| (a.clone(), b.clone())).collect();
    assert_eq!(pairs, expected_pairs(), "the pairs with {MIN_INLIERS} inliers or more");
    for (a, b, w) in &report.weights {
        assert!(a < b, "({a}, {b}) is not in byte order");
        assert!(*w as usize >= MIN_INLIERS, "({a}, {b}): {w} inliers");
        // a pair of neighbours shares two groups, the next one group: most of their points are inliers
        let (i, j) = (a[6..8].parse::<usize>().unwrap(), b[6..8].parse::<usize>().unwrap());
        let shared = if j == i + 1 { 2 * GROUP } else { GROUP };
        assert!(*w as usize >= shared * 8 / 10, "({a}, {b}): {w} inliers of {shared} shared points");
    }
    assert!(report.seconds_matching > 0.0 && report.seconds_verifying > 0.0);
    assert!(report.log.is_empty(), "{:?}", report.log);
    // the progress: from nothing to everything on one scale, never backwards
    let calls = calls.lock().unwrap().clone();
    assert_eq!(calls.first(), Some(&(0, 2 * report.pairs_tried)), "{calls:?}");
    assert_eq!(calls.last(), Some(&(2 * report.pairs_tried, 2 * report.pairs_tried)), "{calls:?}");
    assert!(calls.windows(2).all(|w| w[0].0 <= w[1].0 && w[0].1 == w[1].1), "{calls:?}");

    // without a prior focal length the same pairs come out (the fundamental matrix's inliers)
    let uncalibrated = preselect::run(&store, &cams, &HashSet::new(), &[], &o, &never, &|_, _| {}).unwrap();
    let pairs: Vec<(String, String)> = uncalibrated.weights.iter().map(|(a, b, _)| (a.clone(), b.clone())).collect();
    assert_eq!(pairs, expected_pairs());

    // the GPUs give the same weights, exactly (exact kernels, the RANSAC seeded by the pair)
    for adapter in gpu::adapters() {
        if Matcher::new(Some(adapter.index)).is_err() {
            continue;
        }
        let on_gpu = preselect::run(&store, &cams, &calibrated, &[adapter.index], &o, &never, &|_, _| {}).unwrap();
        assert_eq!(on_gpu.weights, report.weights, "adapter {} ({})", adapter.index, adapter.name);
        assert!(on_gpu.log.is_empty(), "adapter {}: {:?}", adapter.index, on_gpu.log);
        // the small tiles too
        let small = PreselectOptions { matching: MatchOptions { workgroup_bytes: Some(16384), ..o.matching }, ..o };
        let on_small =
            preselect::run(&store, &cams, &calibrated, &[adapter.index], &small, &never, &|_, _| {}).unwrap();
        assert_eq!(on_small.weights, report.weights, "adapter {} with the small tiles", adapter.index);
        eprintln!("adapter {} ({}): the weights of the CPU", adapter.index, adapter.name);
    }
    // an adapter that does not exist is logged and the CPU does the work
    let beyond = gpu::adapters().len();
    let fallen_back = preselect::run(&store, &cams, &calibrated, &[beyond], &o, &never, &|_, _| {}).unwrap();
    assert_eq!(fallen_back.weights, report.weights);
    assert!(fallen_back.log.iter().any(|l| l.contains(&format!("GPU {beyond}"))), "{:?}", fallen_back.log);
    drop(store);
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn a_cancel_set_before_the_run_is_an_error_and_a_missing_camera_too() {
    let dir = folder("cancel");
    let store = scene(&dir);
    let names = store.names();
    let cams: HashMap<String, Camera> = names.iter().map(|n| (n.clone(), camera())).collect();
    let calibrated: HashSet<String> = names.iter().cloned().collect();
    let o = options();
    let cancelled = AtomicBool::new(true);
    let refused = preselect::run(&store, &cams, &calibrated, &[], &o, &cancelled, &|_, _| {});
    assert_eq!(refused.err().as_deref(), Some("cancelled"));
    let mut without = cams.clone();
    without.remove(&photo_name(3));
    let refused = preselect::run(&store, &without, &calibrated, &[], &o, &AtomicBool::new(false), &|_, _| {});
    assert!(refused.as_ref().is_err_and(|p| p.contains(&photo_name(3))), "{refused:?}");
    drop(store);
    fs::remove_dir_all(&dir).unwrap();
}
