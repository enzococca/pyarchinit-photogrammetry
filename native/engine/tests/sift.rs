//! SIFT on the CPU (`sift_cpu`), the twin of the GPU kernel (`gpu_sift`): on one synthetic image
//! - blobs of three sizes on a seeded texture, 640 x 480 - the extraction gives the same answer at
//! every run, in the order of the GPU kernel (row, column, scale, orientation), COLMAP's cap keeps
//! the largest scales, the two checks of the GPU refuse the same inputs; and, on every adapter that
//! opens a device, the GPU's points have their twins among the CPU's: at least 95 % within 0.5 px
//! and 10 % of scale, the counts within 5 %, the median cosine similarity of the twins' descriptors
//! above 0.99 (the thresholds of `tests/test_native.py::test_gpu_sift_finds_colmaps_points`).

use ppg_engine::gpu;
use ppg_engine::gpu_sift::{Options, Sift, DESC};
use ppg_engine::sift_cpu;

const W: usize = 640;
const H: usize = 480;

/// Knuth's MMIX linear congruential generator, as in the other tests of this crate.
struct Lcg(u64);

impl Lcg {
    fn unit(&mut self) -> f32 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        ((self.0 >> 40) as f32) / (1u64 << 24) as f32
    }
}

/// The image: value noise at four grid spacings (the structure of a photo, so that points are
/// found everywhere, the finest grids standing for its grain) and blobs of three sizes - sigma
/// 2.5, 6 and 16 pixels, whose extrema fall in different octaves - on a grey of 0.5, clipped to
/// [0, 1]; the same at every run.
fn image() -> Vec<f32> {
    let mut random = Lcg(2026);
    let mut img = vec![0.5f32; W * H];
    for (spacing, amplitude) in [(3usize, 0.10f32), (8, 0.12), (32, 0.12), (128, 0.14)] {
        let (gw, gh) = (W / spacing + 2, H / spacing + 2);
        let grid: Vec<f32> = (0..gw * gh).map(|_| random.unit() - 0.5).collect();
        for y in 0..H {
            let fy = y as f32 / spacing as f32;
            let (gy, ty) = (fy as usize, fy.fract());
            for x in 0..W {
                let fx = x as f32 / spacing as f32;
                let (gx, tx) = (fx as usize, fx.fract());
                let top = grid[gy * gw + gx] * (1.0 - tx) + grid[gy * gw + gx + 1] * tx;
                let bottom = grid[(gy + 1) * gw + gx] * (1.0 - tx) + grid[(gy + 1) * gw + gx + 1] * tx;
                img[y * W + x] += amplitude * (top * (1.0 - ty) + bottom * ty);
            }
        }
    }
    for (sigma, count) in [(2.5f32, 24usize), (6.0, 16), (16.0, 8)] {
        for _ in 0..count {
            let cx = 40.0 + random.unit() * (W as f32 - 80.0);
            let cy = 40.0 + random.unit() * (H as f32 - 80.0);
            let sign = if random.unit() < 0.5 { -0.35 } else { 0.35 };
            let reach = (3.0 * sigma).ceil() as i64;
            for y in (cy as i64 - reach).max(0)..=(cy as i64 + reach).min(H as i64 - 1) {
                for x in (cx as i64 - reach).max(0)..=(cx as i64 + reach).min(W as i64 - 1) {
                    let (dx, dy) = (x as f32 - cx, y as f32 - cy);
                    img[y as usize * W + x as usize] += sign * (-(dx * dx + dy * dy) / (2.0 * sigma * sigma)).exp();
                }
            }
        }
    }
    img.iter().map(|v| v.clamp(0.0, 1.0)).collect()
}

/// The key points are in the order of the GPU kernel: by row, then column, scale, orientation.
fn assert_ordered(kp: &[[f32; 4]]) {
    for (i, pair) in kp.windows(2).enumerate() {
        let (a, b) = (pair[0], pair[1]);
        let key = |k: [f32; 4]| [k[1], k[0], k[2], k[3]];
        assert!(key(a) <= key(b), "key points {i} and {} out of order: {a:?} then {b:?}", i + 1);
    }
}

/// The cosine similarity of two descriptors.
fn cosine(a: &[u8], b: &[u8]) -> f64 {
    let dot: f64 = a.iter().zip(b).map(|(&p, &q)| f64::from(p) * f64::from(q)).sum();
    let norm = |d: &[u8]| d.iter().map(|&p| f64::from(p) * f64::from(p)).sum::<f64>().sqrt();
    dot / (norm(a) * norm(b)).max(1e-12)
}

/// Of the points of `a`, the share with a twin in `b` - the nearest point within 0.5 px, the one
/// with the closest orientation when several are that near, with a scale within 10 % - and the
/// median cosine similarity of the descriptors of the twins.
fn agreement(a: &[[f32; 4]], da: &[u8], b: &[[f32; 4]], db: &[u8]) -> (f64, f64) {
    let turn = 2.0 * std::f32::consts::PI;
    let mut cosines = Vec::new();
    for (i, p) in a.iter().enumerate() {
        let mut best: Option<(f32, f32, usize)> = None; // distance, orientation difference, index
        for (j, q) in b.iter().enumerate() {
            let d = (p[0] - q[0]).hypot(p[1] - q[1]);
            let dth = (p[3] - q[3]).rem_euclid(turn).min(turn - (p[3] - q[3]).rem_euclid(turn));
            let near = d < 0.5;
            let better = match best {
                None => true,
                Some((bd, bth, _)) => if near && bd < 0.5 { dth < bth } else { d < bd },
            };
            if better {
                best = Some((d, dth, j));
            }
        }
        if let Some((d, _, j)) = best {
            if d < 0.5 && (p[2] / b[j][2]).ln().abs() < 0.1 {
                cosines.push(cosine(&da[i * DESC..(i + 1) * DESC], &db[j * DESC..(j + 1) * DESC]));
            }
        }
    }
    let twins = cosines.len() as f64 / a.len().max(1) as f64;
    cosines.sort_by(|x, y| x.partial_cmp(y).expect("a cosine is a number"));
    let median = cosines.get(cosines.len() / 2).copied().unwrap_or(0.0);
    (twins, median)
}

#[test]
fn cpu_sift_is_repeatable_and_ordered() {
    let img = image();
    let o = Options { max_features: 40000, ..Options::default() };
    let (kp, desc) = sift_cpu::extract(&img, W, H, &o).expect("the image is extracted");
    eprintln!("{} key points on the textured 640 x 480 image", kp.len());
    assert!(kp.len() > 2000, "{} key points on a textured 640 x 480 image", kp.len());
    assert_eq!(desc.len(), kp.len() * DESC);
    assert_ordered(&kp);
    for (i, k) in kp.iter().enumerate() {
        let inside = k[0] >= 0.0 && k[0] < W as f32 && k[1] >= 0.0 && k[1] < H as f32;
        assert!(inside, "key point {i} outside the image: {k:?}");
        assert!(k[2] > 0.0 && (0.0..=2.0 * std::f32::consts::PI + 0.2).contains(&k[3]), "key point {i}: {k:?}");
    }
    // the blobs of sigma 16 live in the coarsest octaves: scales up to the largest are present
    assert!(kp.iter().any(|k| k[2] > 10.0), "no key point of a large scale among {}", kp.len());
    let (again, desc_again) = sift_cpu::extract(&img, W, H, &o).expect("the image is extracted again");
    assert!(kp == again && desc == desc_again, "two runs differ");
    // COLMAP's cap keeps the largest scales, the order stays
    let capped = Options { max_features: 100, ..Options::default() };
    let (few, few_desc) = sift_cpu::extract(&img, W, H, &capped).expect("the capped extraction");
    assert_eq!((few.len(), few_desc.len()), (100, 100 * DESC));
    assert_ordered(&few);
    let mut scales: Vec<f32> = kp.iter().map(|k| k[2]).collect();
    scales.sort_by(|x, y| y.partial_cmp(x).expect("a scale is a number"));
    let mut kept: Vec<f32> = few.iter().map(|k| k[2]).collect();
    kept.sort_by(|x, y| y.partial_cmp(x).expect("a scale is a number"));
    assert_eq!(kept, scales[..100].to_vec(), "the cap did not keep the 100 largest scales");
    // upright: one point per candidate, orientation 0
    let upright = Options { max_features: 40000, upright: true, ..Options::default() };
    let (up, _) = sift_cpu::extract(&img, W, H, &upright).expect("the upright extraction");
    assert!(up.iter().all(|k| k[3] == 0.0) && !up.is_empty() && up.len() <= kp.len());
    assert_ordered(&up);
    // the first octave at the image's own size, or one below it: fewer points, still ordered
    let at_zero = Options { max_features: 40000, first_octave: 0, ..Options::default() };
    let (zero, _) = sift_cpu::extract(&img, W, H, &at_zero).expect("first octave 0");
    assert!(!zero.is_empty() && zero.len() < kp.len());
    assert_ordered(&zero);
    let (one, _) = sift_cpu::extract(&img, W, H, &Options { first_octave: 1, ..at_zero }).expect("first octave 1");
    assert!(!one.is_empty() && one.len() < zero.len());
    // a flat image has no extremum
    let flat = vec![0.5f32; W * H];
    let (none, no_desc) = sift_cpu::extract(&flat, W, H, &o).expect("a flat image is extracted");
    assert!(none.is_empty() && no_desc.is_empty());
    // the two checks of the GPU kernel
    let small = sift_cpu::extract(&img[..49], 7, 7, &o).err();
    assert!(small.as_ref().is_some_and(|m| m.contains("8 x 8")), "{small:?}");
    let wrong = sift_cpu::extract(&img[..W * H - 1], W, H, &o).err();
    assert!(wrong.as_ref().is_some_and(|m| m.contains("w x h")), "{wrong:?}");
    let below = sift_cpu::extract(&img, W, H, &Options { first_octave: -2, ..o }).err();
    assert!(below.as_ref().is_some_and(|m| m.contains("first_octave")), "{below:?}");
}

#[test]
fn cpu_and_gpu_sift_agree() {
    let adapters = gpu::adapters();
    if adapters.is_empty() {
        eprintln!("no GPU adapter: the kernels are not compared");
        return;
    }
    let img = image();
    let o = Options { max_features: 40000, ..Options::default() };
    let (cpu, cpu_desc) = sift_cpu::extract(&img, W, H, &o).expect("the CPU extraction");
    for adapter in &adapters {
        let sift = match Sift::new(Some(adapter.index)) {
            Ok(sift) => sift,
            Err(problem) => {
                // an adapter that opens no device (a GL one of a runner) is reported and left out
                eprintln!("adapter {} ({}) skipped: {problem}", adapter.index, adapter.name);
                continue;
            }
        };
        let (gpu, gpu_desc) = sift.extract(&img, W, H, &o).unwrap_or_else(|p| panic!("{}: {p}", adapter.name));
        let (twins, median) = agreement(&gpu, &gpu_desc, &cpu, &cpu_desc);
        eprintln!("adapter {} ({}): {} points on the GPU, {} on the CPU, {:.4} twins, median cosine {median:.5}",
                  adapter.index, adapter.name, gpu.len(), cpu.len(), twins);
        assert!(twins > 0.95, "{}: {twins:.3} of the GPU's points have a CPU twin", adapter.name);
        assert!((cpu.len() as f64 - gpu.len() as f64).abs() < 0.05 * gpu.len() as f64,
                "{}: {} points on the CPU, {} on the GPU", adapter.name, cpu.len(), gpu.len());
        assert!(median > 0.99, "{}: median cosine similarity {median:.4}", adapter.name);
    }
}
