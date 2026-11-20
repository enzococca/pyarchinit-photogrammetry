//! SIFT on the CPU (`sift_cpu`), the twin of the GPU kernel (`gpu_sift`): on one synthetic image
//! - blobs of three sizes on a seeded texture, 640 x 480 - the extraction gives the same answer at
//! every run, in the order of the GPU kernel (row, column, scale, orientation), COLMAP's cap keeps
//! the largest scales, the two checks of the GPU refuse the same inputs; and, on every adapter that
//! opens a device, the GPU's points have their twins among the CPU's: at least 95 % within 0.5 px
//! and 10 % of scale, the counts within 5 %, the median cosine similarity of the twins' descriptors
//! above 0.99 (the thresholds of `tests/test_native.py::test_gpu_sift_finds_colmaps_points`), on
//! even and odd sizes alike - the octaves the GPU reaches by halving an odd width hold the CPU's
//! points too.

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

/// An image of `w x h` from `seed`: value noise at four grid spacings (the structure of a photo,
/// so that points are found everywhere, the finest grids standing for its grain) and blobs of
/// three sizes - sigma 2.5, 6 and 16 pixels, whose extrema fall in different octaves, as many per
/// area as the 640 x 480 image has and at least one of each - on a grey of 0.5, clipped to
/// [0, 1]; the same at every run.
fn image_of(w: usize, h: usize, seed: u64) -> Vec<f32> {
    let mut random = Lcg(seed);
    let mut img = vec![0.5f32; w * h];
    for (spacing, amplitude) in [(3usize, 0.10f32), (8, 0.12), (32, 0.12), (128, 0.14)] {
        let (gw, gh) = (w / spacing + 2, h / spacing + 2);
        let grid: Vec<f32> = (0..gw * gh).map(|_| random.unit() - 0.5).collect();
        for y in 0..h {
            let fy = y as f32 / spacing as f32;
            let (gy, ty) = (fy as usize, fy.fract());
            for x in 0..w {
                let fx = x as f32 / spacing as f32;
                let (gx, tx) = (fx as usize, fx.fract());
                let top = grid[gy * gw + gx] * (1.0 - tx) + grid[gy * gw + gx + 1] * tx;
                let bottom = grid[(gy + 1) * gw + gx] * (1.0 - tx) + grid[(gy + 1) * gw + gx + 1] * tx;
                img[y * w + x] += amplitude * (top * (1.0 - ty) + bottom * ty);
            }
        }
    }
    let margin = 40.0f32.min(w.min(h) as f32 / 4.0);
    for (sigma, count) in [(2.5f32, 24usize), (6.0, 16), (16.0, 8)] {
        for _ in 0..(count * w * h / (W * H)).max(1) {
            let cx = margin + random.unit() * (w as f32 - 2.0 * margin);
            let cy = margin + random.unit() * (h as f32 - 2.0 * margin);
            let sign = if random.unit() < 0.5 { -0.35 } else { 0.35 };
            let reach = (3.0 * sigma).ceil() as i64;
            for y in (cy as i64 - reach).max(0)..=(cy as i64 + reach).min(h as i64 - 1) {
                for x in (cx as i64 - reach).max(0)..=(cx as i64 + reach).min(w as i64 - 1) {
                    let (dx, dy) = (x as f32 - cx, y as f32 - cy);
                    img[y as usize * w + x as usize] += sign * (-(dx * dx + dy * dy) / (2.0 * sigma * sigma)).exp();
                }
            }
        }
    }
    img.iter().map(|v| v.clamp(0.0, 1.0)).collect()
}

/// The image of the tests, 640 x 480.
fn image() -> Vec<f32> {
    image_of(W, H, 2026)
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
    // an odd width and height: the twin halves every octave with the true stride
    let (ow, oh) = (641usize, 481usize);
    let odd = image_of(ow, oh, 2027);
    let (kp_odd, desc_odd) = sift_cpu::extract(&odd, ow, oh, &o).expect("the odd-sized image");
    let again_odd = sift_cpu::extract(&odd, ow, oh, &o).expect("the odd-sized image again");
    eprintln!("{} key points on the textured {ow} x {oh} image", kp_odd.len());
    assert!(kp_odd.len() > 2000 && kp_odd.iter().any(|k| k[2] > 10.0), "{} points at {ow} x {oh}", kp_odd.len());
    assert!(again_odd == (kp_odd.clone(), desc_odd), "two runs differ at {ow} x {oh}");
    assert_ordered(&kp_odd);
    // images whose coarsest octaves are narrower than the blur radius (40 x 40 reaches 10 x 10, a
    // radius of 13; one level per octave reaches 40 x 40 with a radius of 45) and the smallest
    // image the kernel accepts: extracted, repeatable, ordered, inside the image
    let one_level = Options { octave_resolution: 1, ..o };
    for (sw, sh, options) in [(40usize, 40usize, o), (48, 48, o), (44, 60, o), (8, 8, o), (160, 160, one_level)] {
        let small = image_of(sw, sh, 7);
        let (kp_s, desc_s) = sift_cpu::extract(&small, sw, sh, &options).unwrap_or_else(|p| panic!("{sw} x {sh}: {p}"));
        eprintln!("{sw} x {sh}, {} levels per octave: {} points", options.octave_resolution, kp_s.len());
        assert!(sw < 40 || !kp_s.is_empty(), "no point at {sw} x {sh}");
        assert_eq!(desc_s.len(), kp_s.len() * DESC);
        assert!(sift_cpu::extract(&small, sw, sh, &options).expect("again") == (kp_s.clone(), desc_s), "{sw} x {sh}");
        assert_ordered(&kp_s);
        let inside = |k: &[f32; 4]| k[0] >= 0.0 && k[0] < sw as f32 && k[1] >= 0.0 && k[1] < sh as f32;
        assert!(kp_s.iter().all(inside), "a key point outside the {sw} x {sh} image");
    }
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
    // the images: the textured one, an odd-sized one (641 wide: the kernel halves its octave 0
    // with a stride of 641, not 640, as the twin does) and two small ones whose coarsest octave
    // (10 and 11 pixels wide) is narrower than the blur radius
    let o = Options { max_features: 40000, ..Options::default() };
    let cases: Vec<(String, usize, usize, Vec<f32>)> = vec![
        (format!("{W} x {H}"), W, H, image()),
        ("641 x 481".into(), 641, 481, image_of(641, 481, 2027)),
        ("40 x 40".into(), 40, 40, image_of(40, 40, 7)),
        ("44 x 60".into(), 44, 60, image_of(44, 60, 7)),
    ];
    let on_cpu = |(name, w, h, img): &(String, usize, usize, Vec<f32>)| {
        sift_cpu::extract(img, *w, *h, &o).unwrap_or_else(|p| panic!("{name} on the CPU: {p}"))
    };
    let twins: Vec<(Vec<[f32; 4]>, Vec<u8>)> = cases.iter().map(on_cpu).collect();
    for adapter in &adapters {
        let sift = match Sift::new(Some(adapter.index)) {
            Ok(sift) => sift,
            Err(problem) => {
                // an adapter that opens no device (a GL one of a runner) is reported and left out
                eprintln!("adapter {} ({}) skipped: {problem}", adapter.index, adapter.name);
                continue;
            }
        };
        for ((name, w, h, img), (cpu, cpu_desc)) in cases.iter().zip(&twins) {
            let what = format!("adapter {} ({}), {name}", adapter.index, adapter.name);
            let (gpu, gpu_desc) = sift.extract(img, *w, *h, &o).unwrap_or_else(|p| panic!("{what}: {p}"));
            let (share, median) = agreement(&gpu, &gpu_desc, cpu, cpu_desc);
            eprintln!("{what}: {} points on the GPU, {} on the CPU, {share:.4} twins, median cosine {median:.5}",
                      gpu.len(), cpu.len());
            assert!(share > 0.95, "{what}: {share:.3} of the GPU's points have a CPU twin");
            assert!((cpu.len() as f64 - gpu.len() as f64).abs() < 0.05 * gpu.len() as f64,
                    "{what}: {} points on the CPU, {} on the GPU", cpu.len(), gpu.len());
            assert!(median > 0.99, "{what}: median cosine similarity {median:.4}");
        }
    }
}

/// The share of the points of `kp` whose scale is `at_least`, among all of them: the points of the
/// octaves the kernel reaches by halving.
fn large(kp: &[[f32; 4]], desc: &[u8], at_least: f32) -> (Vec<[f32; 4]>, Vec<u8>) {
    let idx: Vec<usize> = (0..kp.len()).filter(|&i| kp[i][2] >= at_least).collect();
    let mut d = Vec::with_capacity(idx.len() * DESC);
    for &i in &idx {
        d.extend_from_slice(&desc[i * DESC..(i + 1) * DESC]);
    }
    (idx.iter().map(|&i| kp[i]).collect(), d)
}

#[test]
fn gpu_halves_an_odd_width_as_the_cpu_does() {
    let adapters = gpu::adapters();
    if adapters.is_empty() {
        eprintln!("no GPU adapter: the halving is not compared");
        return;
    }
    // 641 wide: octave 0 is 641 x 481 and octave 1 is halved from it, so a kernel reading the
    // finer octave with a stride of twice the coarser width (640) shears every octave from the
    // first halving on; the points of those octaves are the ones of scale 4 and above (sigma0 of
    // 2.016 doubled), and they must have their twins among the CPU's as the finer ones do
    let (w, h) = (641usize, 481usize);
    let img = image_of(w, h, 2027);
    let o = Options { max_features: 40000, ..Options::default() };
    let (cpu, cpu_desc) = sift_cpu::extract(&img, w, h, &o).expect("the odd-width image on the CPU");
    let (cpu_large, _) = large(&cpu, &cpu_desc, 4.5);
    assert!(cpu_large.len() > 50, "{} points of scale 4.5 and above on the CPU", cpu_large.len());
    for adapter in &adapters {
        let sift = match Sift::new(Some(adapter.index)) {
            Ok(sift) => sift,
            Err(problem) => {
                eprintln!("adapter {} ({}) skipped: {problem}", adapter.index, adapter.name);
                continue;
            }
        };
        let what = format!("adapter {} ({}), {w} x {h}", adapter.index, adapter.name);
        let (gpu, gpu_desc) = sift.extract(&img, w, h, &o).unwrap_or_else(|p| panic!("{what}: {p}"));
        let (gpu_large, gpu_large_desc) = large(&gpu, &gpu_desc, 4.5);
        let (share, median) = agreement(&gpu_large, &gpu_large_desc, &cpu, &cpu_desc);
        eprintln!("{what}: {} points of scale 4.5 and above on the GPU, {} on the CPU, {share:.4} twins, \
                   median cosine {median:.5}", gpu_large.len(), cpu_large.len());
        assert!(share > 0.9, "{what}: {share:.3} of the GPU's points of the halved octaves have a CPU twin");
        assert!((cpu_large.len() as f64 - gpu_large.len() as f64).abs() < 0.1 * cpu_large.len() as f64,
                "{what}: {} points of the halved octaves on the CPU, {} on the GPU", cpu_large.len(), gpu_large.len());
        assert!(median > 0.99, "{what}: median cosine similarity {median:.4} on the halved octaves");
    }
}
