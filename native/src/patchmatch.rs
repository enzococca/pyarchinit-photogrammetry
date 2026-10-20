//! PatchMatch multi-view stereo of one photo (`engine.dense`, backend "patchmatch"): every pixel
//! of the reference photo gets a plane - a depth along its ray and a normal - whose image in the
//! neighbouring photos looks like the window around the pixel (normalised cross correlation, the
//! mean of the best views, so that a view where the point is hidden does not count). Planes start
//! random (or from a depth map given, e.g. the two-view SGBM one), then, a few times over, every
//! pixel tries the planes of its neighbours (red-black checkerboard: half of the pixels at a time,
//! each reading only the other half, so the rows run in parallel) and random changes of its own.
//!
//! Coordinates are those of the arrays: the centre of pixel (x, y) is (x, y). Depth is the z of
//! the camera, normals are in the camera frame and face it.

use rayon::prelude::*;

pub const BAD: f32 = 2.0;
const MAX_VIEWS: usize = 32;

/// A neighbouring photo: its grey image and where it is seen from.
pub struct Source<'a> {
    pub img: &'a [f32],
    pub w: usize,
    pub h: usize,
    pub k: [f64; 4],
    /// reference camera -> this camera: x_src = r x_ref + t
    pub r: [[f64; 3]; 3],
    pub t: [f64; 3],
}

#[derive(Clone, Copy)]
pub struct Params {
    pub depth_min: f64,
    pub depth_max: f64,
    pub iterations: usize,
    pub half_window: usize,
    pub step: usize,
    pub top_k: usize,
    /// photos that must see the whole window for a plane to get a cost: with one photo any
    /// texture-poor window matches somewhere (the borders of a block of aerial photos came
    /// out metres off and made a step in the DEM where the dense cloud ends)
    pub min_views: usize,
    pub max_cost: f32,
    pub seed: u64,
}

pub struct Output {
    pub depth: Vec<f32>,
    pub normal: Vec<f32>,
    pub cost: Vec<f32>,
}

#[derive(Clone, Copy)]
struct Plane {
    d: f64,
    n: [f64; 3],
}

fn dot(a: &[f64; 3], b: &[f64; 3]) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

fn normalize(v: [f64; 3]) -> [f64; 3] {
    let l = dot(&v, &v).sqrt();
    if l > 0.0 {
        [v[0] / l, v[1] / l, v[2] / l]
    } else {
        [0.0, 0.0, -1.0]
    }
}

/// xorshift64*: one generator per pixel and pass, from the seed.
struct Rng(u64);

impl Rng {
    fn new(seed: u64, a: u64, b: u64) -> Rng {
        let mut s = seed ^ a.wrapping_mul(0x9E37_79B9_7F4A_7C15) ^ b.wrapping_mul(0xC2B2_AE3D_27D4_EB4F);
        if s == 0 {
            s = 0x2545_F491_4F6C_DD1D;
        }
        let mut r = Rng(s);
        r.next();
        r
    }

    fn next(&mut self) -> u64 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        self.0.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// uniform in [0, 1)
    fn uniform(&mut self) -> f64 {
        (self.next() >> 11) as f64 / (1u64 << 53) as f64
    }
}

fn bilinear(img: &[f32], w: usize, h: usize, u: f64, v: f64) -> Option<f32> {
    if !(u >= 0.0 && v >= 0.0) {
        return None; // also NaN
    }
    let x0 = u as usize;
    let y0 = v as usize;
    if x0 + 1 >= w || y0 + 1 >= h {
        return None;
    }
    let fx = (u - x0 as f64) as f32;
    let fy = (v - y0 as f64) as f32;
    let i = y0 * w + x0;
    let top = img[i] + (img[i + 1] - img[i]) * fx;
    let bottom = img[i + w] + (img[i + w + 1] - img[i + w]) * fx;
    Some(top + (bottom - top) * fy)
}

struct Problem<'a> {
    reference: &'a [f32],
    w: usize,
    kinv: [[f64; 3]; 3],
    sources: &'a [Source<'a>],
    p: Params,
    offsets: Vec<(isize, isize)>,
    mean: Vec<f32>,   // of the window of each pixel (NaN: the window leaves the photo)
    spread: Vec<f32>, // sqrt of the sum of squared deviations in the window
}

impl<'a> Problem<'a> {
    fn ray(&self, x: f64, y: f64) -> [f64; 3] {
        let k = &self.kinv;
        [k[0][0] * x + k[0][1] * y + k[0][2], k[1][0] * x + k[1][1] * y + k[1][2], 1.0]
    }

    /// The plane of `plane` (found at pixel `from`) along the ray of pixel `at`: None when the
    /// ray does not meet it in front of the camera, inside the depth range.
    fn transfer(&self, plane: Plane, from: (usize, usize), at: (usize, usize)) -> Option<Plane> {
        if from == at {
            return Some(plane);
        }
        let rf = self.ray(from.0 as f64, from.1 as f64);
        let ra = self.ray(at.0 as f64, at.1 as f64);
        let rho = plane.d * dot(&plane.n, &rf);
        let na = dot(&plane.n, &ra);
        if na >= -1e-9 {
            return None;
        }
        let d = rho / na;
        if d < self.p.depth_min || d > self.p.depth_max {
            return None;
        }
        Some(Plane { d, n: plane.n })
    }

    /// 1 - NCC of the window of (x, y) with its image in each photo through the plane, the mean
    /// of the `top_k` best photos; BAD when fewer than `min_views` photos see the whole window.
    fn cost(&self, x: usize, y: usize, plane: Plane) -> f32 {
        let i = y * self.w + x;
        let mean_r = self.mean[i];
        if !mean_r.is_finite() {
            return BAD;
        }
        let ray = self.ray(x as f64, y as f64);
        let nr = dot(&plane.n, &ray);
        if nr >= -1e-9 {
            return BAD; // the plane is seen edge-on or from behind
        }
        let rho = plane.d * nr;
        let count = self.offsets.len() as f32;
        let spread_r = self.spread[i];
        let mut costs = [BAD; MAX_VIEWS];
        let mut n_views = 0usize;
        for s in self.sources.iter().take(MAX_VIEWS) {
            // H = K_s (R + t n^T / rho) K_ref^-1
            let mut m = [[0.0f64; 3]; 3];
            for (a, row) in m.iter_mut().enumerate() {
                for (b, v) in row.iter_mut().enumerate() {
                    *v = s.r[a][b] + s.t[a] * plane.n[b] / rho;
                }
            }
            let mut mk = [[0.0f64; 3]; 3];
            for a in 0..3 {
                for b in 0..3 {
                    mk[a][b] = m[a][0] * self.kinv[0][b] + m[a][1] * self.kinv[1][b] + m[a][2] * self.kinv[2][b];
                }
            }
            let (fx, fy, cx, cy) = (s.k[0], s.k[1], s.k[2], s.k[3]);
            let hm = [[fx * mk[0][0] + cx * mk[2][0], fx * mk[0][1] + cx * mk[2][1], fx * mk[0][2] + cx * mk[2][2]],
                      [fy * mk[1][0] + cy * mk[2][0], fy * mk[1][1] + cy * mk[2][1], fy * mk[1][2] + cy * mk[2][2]],
                      [mk[2][0], mk[2][1], mk[2][2]]];
            let (mut ss, mut sss, mut srs) = (0.0f32, 0.0f32, 0.0f32);
            let mut seen = true;
            for &(dx, dy) in &self.offsets {
                let u = (x as isize + dx) as f64;
                let v = (y as isize + dy) as f64;
                let pz = hm[2][0] * u + hm[2][1] * v + hm[2][2];
                if pz <= 1e-12 {
                    seen = false;
                    break;
                }
                let pu = (hm[0][0] * u + hm[0][1] * v + hm[0][2]) / pz;
                let pv = (hm[1][0] * u + hm[1][1] * v + hm[1][2]) / pz;
                match bilinear(s.img, s.w, s.h, pu, pv) {
                    Some(val) => {
                        let r = self.reference[(y as isize + dy) as usize * self.w + (x as isize + dx) as usize];
                        ss += val;
                        sss += val * val;
                        srs += r * val;
                    }
                    None => {
                        seen = false;
                        break;
                    }
                }
            }
            if !seen {
                continue;
            }
            let mean_s = ss / count;
            let var_s = (sss - count * mean_s * mean_s).max(0.0);
            let c = if var_s < 1e-6 * count || spread_r < 1e-12 {
                BAD // no texture in the other photo: anything matches
            } else {
                let ncc = (srs - count * mean_r * mean_s) / (spread_r * var_s.sqrt());
                (1.0 - ncc).clamp(0.0, 2.0)
            };
            costs[n_views] = c;
            n_views += 1;
        }
        if n_views < self.p.min_views.max(1) {
            return BAD;
        }
        let best = &mut costs[..n_views];
        best.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let k = self.p.top_k.clamp(1, n_views);
        best[..k].iter().sum::<f32>() / k as f32
    }

    fn random_normal(&self, rng: &mut Rng, ray: &[f64; 3]) -> [f64; 3] {
        // a direction in the hemisphere facing the camera, around the ray back to it
        loop {
            let v = [rng.uniform() * 2.0 - 1.0, rng.uniform() * 2.0 - 1.0, rng.uniform() * 2.0 - 1.0];
            let l = dot(&v, &v);
            if l > 1e-6 && l <= 1.0 {
                let mut n = normalize(v);
                if dot(&n, ray) > 0.0 {
                    n = [-n[0], -n[1], -n[2]];
                }
                if dot(&n, &normalize(*ray)) < -0.2 {
                    return n;
                }
            }
        }
    }

    fn random_depth(&self, rng: &mut Rng) -> f64 {
        // uniform in inverse depth: as many hypotheses near as far, in pixels of disparity
        let (a, b) = (1.0 / self.p.depth_max, 1.0 / self.p.depth_min);
        1.0 / (a + (b - a) * rng.uniform())
    }

    fn perturb(&self, rng: &mut Rng, plane: Plane, ray: &[f64; 3], scale: f64) -> Plane {
        let inv = 1.0 / plane.d;
        let span = (1.0 / self.p.depth_min - 1.0 / self.p.depth_max) * scale;
        let mut d = 1.0 / (inv + (rng.uniform() * 2.0 - 1.0) * span);
        if !(d >= self.p.depth_min && d <= self.p.depth_max) {
            d = plane.d;
        }
        let mut n = normalize([plane.n[0] + (rng.uniform() * 2.0 - 1.0) * scale,
                               plane.n[1] + (rng.uniform() * 2.0 - 1.0) * scale,
                               plane.n[2] + (rng.uniform() * 2.0 - 1.0) * scale]);
        if dot(&n, ray) >= -1e-3 {
            n = plane.n;
        }
        Plane { d, n }
    }
}

/// Window statistics of the reference photo (mean and spread of the sampled pixels).
fn window_stats(img: &[f32], w: usize, h: usize, offsets: &[(isize, isize)], half: usize) -> (Vec<f32>, Vec<f32>) {
    let n = offsets.len() as f32;
    let rows: Vec<(Vec<f32>, Vec<f32>)> = (0..h)
        .into_par_iter()
        .map(|y| {
            let mut mean = vec![f32::NAN; w];
            let mut spread = vec![0.0f32; w];
            if y < half || y + half >= h {
                return (mean, spread);
            }
            for x in half..w.saturating_sub(half) {
                let (mut s, mut ss) = (0.0f32, 0.0f32);
                for &(dx, dy) in offsets {
                    let v = img[(y as isize + dy) as usize * w + (x as isize + dx) as usize];
                    s += v;
                    ss += v * v;
                }
                let m = s / n;
                mean[x] = m;
                spread[x] = (ss - n * m * m).max(0.0).sqrt();
            }
            (mean, spread)
        })
        .collect();
    let mut mean = Vec::with_capacity(w * h);
    let mut spread = Vec::with_capacity(w * h);
    for (m, s) in rows {
        mean.extend(m);
        spread.extend(s);
    }
    (mean, spread)
}

fn invert_k(k: [f64; 4]) -> [[f64; 3]; 3] {
    let (fx, fy, cx, cy) = (k[0], k[1], k[2], k[3]);
    [[1.0 / fx, 0.0, -cx / fx], [0.0, 1.0 / fy, -cy / fy], [0.0, 0.0, 1.0]]
}

/// Depth, normal and cost of every pixel of `reference` (w x h, grey 0-1), seen with intrinsics
/// `k` (fx, fy, cx, cy), from the `sources`. `init`: a depth map to start from (0 where unknown),
/// with its `init_normal` (3 per pixel, may be empty: then from the neighbouring depths).
pub fn patchmatch(reference: &[f32], w: usize, h: usize, k: [f64; 4], sources: &[Source], p: Params,
                  init: Option<&[f32]>, init_normal: Option<&[f32]>) -> Output {
    let half = p.half_window.max(1);
    let step = p.step.max(1) as isize;
    let mut offsets = Vec::new();
    let mut dy = -(half as isize);
    while dy <= half as isize {
        let mut dx = -(half as isize);
        while dx <= half as isize {
            offsets.push((dx, dy));
            dx += step;
        }
        dy += step;
    }
    let (mean, spread) = window_stats(reference, w, h, &offsets, half);
    let prob = Problem { reference, w, kinv: invert_k(k), sources, p, offsets, mean, spread };
    let n = w * h;
    // the planes to start from
    let mut planes: Vec<Plane> = (0..n)
        .into_par_iter()
        .map(|i| {
            let (x, y) = (i % w, i / w);
            let ray = prob.ray(x as f64, y as f64);
            let mut rng = Rng::new(p.seed, i as u64, 0);
            if let Some(d0) = init {
                let d = d0[i] as f64;
                if d >= p.depth_min && d <= p.depth_max {
                    let nn = init_normal.map(|nm| [nm[3 * i] as f64, nm[3 * i + 1] as f64, nm[3 * i + 2] as f64]);
                    let nn = match nn {
                        Some(v) if dot(&v, &v) > 0.5 && dot(&v, &ray) < -1e-3 => normalize(v),
                        _ => normalize([-ray[0], -ray[1], -ray[2]]),
                    };
                    return Plane { d, n: nn };
                }
            }
            Plane { d: prob.random_depth(&mut rng), n: prob.random_normal(&mut rng, &ray) }
        })
        .collect();
    let mut costs: Vec<f32> = (0..n).into_par_iter().map(|i| prob.cost(i % w, i / w, planes[i])).collect();
    let neighbours: [(isize, isize); 8] = [(-1, 0), (1, 0), (0, -1), (0, 1), (-5, 0), (5, 0), (0, -5), (0, 5)];
    for it in 0..p.iterations {
        for colour in 0..2usize {
            let pass = (it * 2 + colour) as u64 + 1;
            let updates: Vec<Vec<(usize, Plane, f32)>> = (0..h)
                .into_par_iter()
                .map(|y| {
                    let mut out = Vec::new();
                    let first = (y + colour) % 2;
                    let mut x = first;
                    while x < w {
                        let i = y * w + x;
                        if !prob.mean[i].is_finite() {
                            x += 2;
                            continue;
                        }
                        let mut best = planes[i];
                        let mut best_cost = costs[i];
                        let ray = prob.ray(x as f64, y as f64);
                        // propagation: the planes of the neighbours of the other colour
                        for &(dx, dy) in &neighbours {
                            let nx = x as isize + dx;
                            let ny = y as isize + dy;
                            if nx < 0 || ny < 0 || nx >= w as isize || ny >= h as isize {
                                continue;
                            }
                            let j = ny as usize * w + nx as usize;
                            if let Some(cand) = prob.transfer(planes[j], (nx as usize, ny as usize), (x, y)) {
                                let c = prob.cost(x, y, cand);
                                if c < best_cost {
                                    best = cand;
                                    best_cost = c;
                                }
                            }
                        }
                        // refinement: a random plane, then smaller and smaller changes of the best
                        let mut rng = Rng::new(p.seed, i as u64, pass);
                        let fresh = Plane { d: prob.random_depth(&mut rng), n: prob.random_normal(&mut rng, &ray) };
                        let c = prob.cost(x, y, fresh);
                        if c < best_cost {
                            best = fresh;
                            best_cost = c;
                        }
                        let mut scale = 0.25 / (1.0 + it as f64);
                        for _ in 0..3 {
                            let cand = prob.perturb(&mut rng, best, &ray, scale);
                            let c = prob.cost(x, y, cand);
                            if c < best_cost {
                                best = cand;
                                best_cost = c;
                            }
                            scale *= 0.25;
                        }
                        if best_cost < costs[i] {
                            out.push((i, best, best_cost));
                        }
                        x += 2;
                    }
                    out
                })
                .collect();
            for row in updates {
                for (i, plane, c) in row {
                    planes[i] = plane;
                    costs[i] = c;
                }
            }
        }
    }
    let mut depth = vec![0.0f32; n];
    let mut normal = vec![0.0f32; 3 * n];
    for i in 0..n {
        if costs[i] <= p.max_cost {
            depth[i] = planes[i].d as f32;
            normal[3 * i] = planes[i].n[0] as f32;
            normal[3 * i + 1] = planes[i].n[1] as f32;
            normal[3 * i + 2] = planes[i].n[2] as f32;
        }
    }
    Output { depth, normal, cost: costs }
}
