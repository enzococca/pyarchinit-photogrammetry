//! SIFT on the CPU: the twin of the GPU kernel of `gpu_sift`, step for step - the doubled base of
//! octave -1, the separable Gaussian blurs clamped at the borders, the differences, the extrema of
//! the middle difference with their quadratic refinement and the peak and edge tests, the
//! orientation histogram, the 4 x 4 x 8 descriptor laid out as VLFeat's and quantised as COLMAP's -
//! in the same f32 arithmetic and the same order of operations, so that a photo extracted here and
//! on a GPU gives the same key points and descriptors (to the last bits the two libms and the
//! fused multiplications of a GPU differ by) and the same final order: COLMAP's cap on the largest
//! scales, then by row, column, scale and orientation. The work of one photo is shared out among
//! the threads of rayon: the rows of the blurs, the differences and the extrema, the candidates of
//! the orientations, the oriented key points of the descriptors; every stage keeps the row order,
//! so the answer does not depend on the threads.
//!
//! Where the twins differ: the GPU keeps at most 2^17 candidates and 2^17 oriented key points per
//! octave (the size of its buffers), in no particular order, where the CPU keeps them all. And a
//! known defect of the GPU kernel's halving, to be fixed in Task 4 of the native engine plan: its
//! `decimate` reads the finer octave with a stride of twice the coarser width (gpu_sift.rs), which
//! is the finer width only when that is even, so an odd width shears every coarser octave; the
//! twin halves with the true stride, and on such a photo its points are the right ones.
//!
//! Units as [`crate::gpu_sift::Sift::extract`]: `(x, y, sigma, orientation)` in the pixels of the
//! image with the centre of the first pixel at (0, 0); the caller adds COLMAP's 0.5.

use rayon::prelude::*;

use crate::gpu_sift::{Options, DESC};

const PI: f32 = std::f32::consts::PI;
/// The index of the first level of an octave relative to the level of sigma0 (VLFeat's s_min).
const S_MIN: i32 = -1;
/// The nominal smoothing of the photo (VLFeat's sigman).
const SIGMA_N: f32 = 0.5;

/// The constants of the scale space for one octave resolution, as the GPU kernel computes them.
struct Scales {
    s_res: usize,
    sigma0: f32,
    sigmak: f32,
    dsigma0: f32,
    /// 1 / octave resolution (the kernel's `s_scale`).
    s_scale: f32,
}

impl Scales {
    fn new(s_res: usize) -> Scales {
        let sigma0 = 1.6f32 * 2f32.powf(1.0 / s_res as f32);
        let sigmak = 2f32.powf(1.0 / s_res as f32);
        let dsigma0 = sigma0 * (1.0 - 1.0 / (sigmak * sigmak)).sqrt();
        Scales { s_res, sigma0, sigmak, dsigma0, s_scale: 1.0 / s_res as f32 }
    }

    /// The Gaussian level whose gradient serves a key point of scale `s` (VLFeat: the integer
    /// scale, within the levels whose gradient exists; the kernel clamps it to the three middle
    /// levels whatever the octave resolution, and so does the twin).
    fn level_of(s: f32) -> usize {
        ((s + 0.5).floor() as i32).clamp(0, 2) as usize + 1
    }
}

/// The Gaussian levels of one octave, `bw x bh` each, row-major.
struct Octave {
    w: usize,
    h: usize,
    levels: Vec<Vec<f32>>,
}

impl Octave {
    /// The pixel `(x, y)` of `level`, the coordinates clamped into the image.
    #[inline]
    fn pix(&self, level: usize, x: i32, y: i32) -> f32 {
        let xx = x.clamp(0, self.w as i32 - 1) as usize;
        let yy = y.clamp(0, self.h as i32 - 1) as usize;
        self.levels[level][yy * self.w + xx]
    }

    /// VLFeat's gradient: central differences inside, one-sided at the borders.
    #[inline]
    fn gradient(&self, level: usize, x: i32, y: i32) -> (f32, f32) {
        let (w, h) = (self.w as i32, self.h as i32);
        let gx = if x == 0 {
            self.pix(level, 1, y) - self.pix(level, 0, y)
        } else if x == w - 1 {
            self.pix(level, x, y) - self.pix(level, x - 1, y)
        } else {
            0.5 * (self.pix(level, x + 1, y) - self.pix(level, x - 1, y))
        };
        let gy = if y == 0 {
            self.pix(level, x, 1) - self.pix(level, x, 0)
        } else if y == h - 1 {
            self.pix(level, x, y) - self.pix(level, x, y - 1)
        } else {
            0.5 * (self.pix(level, x, y + 1) - self.pix(level, x, y - 1))
        };
        (gx, gy)
    }
}

/// Key points `(x, y, sigma, orientation)` in the pixels of `image` (the centre of the first pixel
/// at (0, 0)) and their 128-byte descriptors, as [`crate::gpu_sift::Sift::extract`] gives them:
/// `image` grey values in [0, 1], row-major `w x h`; at most `o.max_features` points, the largest
/// scales kept, sorted by row, column, scale and orientation. The same two checks as the GPU: an
/// image smaller than 8 x 8 (or of another length than `w x h`) and a first octave below -1 are
/// errors.
pub fn extract(image: &[f32], w: usize, h: usize, o: &Options) -> Result<(Vec<[f32; 4]>, Vec<u8>), String> {
    if image.len() != w * h || w < 8 || h < 8 {
        return Err("image: w x h float values, at least 8 x 8".into());
    }
    let sc = Scales::new(o.octave_resolution.max(1) as usize);
    let n_levels = sc.s_res + 3;
    // the base of the first octave: doubled (as VLFeat), copied, or halved
    let (mut bw, mut bh, mut base) = match o.first_octave {
        -1 => (2 * w, 2 * h, upsample2(image, w, h)),
        0 => (w, h, image.to_vec()),
        n if n > 0 => {
            let mut cur = image.to_vec();
            let (mut cw, mut ch) = (w, h);
            for _ in 0..n {
                let (nw, nh) = (cw / 2, ch / 2);
                cur = decimate(&cur, cw, nw, nh);
                cw = nw;
                ch = nh;
            }
            (cw, ch, cur)
        }
        _ => return Err("first_octave: -1, 0 or positive".into()),
    };
    let max_orient = o.max_orientations.max(1) as usize;
    let mut keypoints: Vec<[f32; 4]> = Vec::new();
    let mut descriptors: Vec<u8> = Vec::new();
    let mut octave = o.first_octave;
    let mut prev: Option<Octave> = None; // the finer octave
    for oct in 0..o.num_octaves {
        if bw < 8 || bh < 8 {
            break;
        }
        let xper = 2f32.powi(octave);
        // the first level: the base, or the finer octave's level s_min + S halved, smoothed to
        // sigma0 * sigmak^s_min when the base is smoother than that (the first octave only: a
        // halved level carries that smoothing already)
        let mut level0 = match prev.take() {
            None => std::mem::take(&mut base),
            Some(finer) => {
                let s_best = ((S_MIN + sc.s_res as i32).min(S_MIN + n_levels as i32 - 1) - S_MIN) as usize;
                decimate(&finer.levels[s_best], finer.w, bw, bh)
            }
        };
        if oct == 0 {
            let sa = sc.sigma0 * sc.sigmak.powi(S_MIN);
            let sb = SIGMA_N * 2f32.powi(-octave);
            if sa > sb {
                level0 = blur(&level0, bw, bh, &gaussian((sa * sa - sb * sb).sqrt()));
            }
        }
        let mut levels = Vec::with_capacity(n_levels);
        levels.push(level0);
        for s in 1..n_levels {
            let sd = sc.dsigma0 * sc.sigmak.powi(s as i32 + S_MIN);
            let next = blur(&levels[s - 1], bw, bh, &gaussian(sd));
            levels.push(next);
        }
        let current = Octave { w: bw, h: bh, levels };
        let dogs: Vec<Vec<f32>> =
            (0..n_levels - 1).map(|d| difference(&current.levels[d], &current.levels[d + 1])).collect();
        let mut candidates: Vec<[f32; 4]> = Vec::new();
        for d in 1..n_levels - 2 {
            // the DoG index d corresponds to the Gaussian level s = d - 1 of the octave
            extrema(&dogs[d - 1], &dogs[d], &dogs[d + 1], bw, bh, (d as f32) - 1.0, o, &mut candidates);
        }
        // one candidate's orientations after the other's: the order of the candidates, whatever the
        // threads (the final sort needs no order, the cap's ties do)
        let per_candidate: Vec<Vec<[f32; 4]>> =
            candidates.par_iter().map(|c| orientations(&current, c, &sc, max_orient, o.upright)).collect();
        let oriented: Vec<[f32; 4]> = per_candidate.into_iter().flatten().collect();
        let descs: Vec<[u8; DESC]> = oriented.par_iter().map(|k| descriptor(&current, k, &sc)).collect();
        for (k, d) in oriented.iter().zip(&descs) {
            keypoints.push([k[0] * xper, k[1] * xper, k[2] * xper, k[3]]);
            descriptors.extend_from_slice(d);
        }
        prev = Some(current);
        bw /= 2;
        bh /= 2;
        octave += 1;
    }
    Ok(cap_and_order(keypoints, descriptors, o.max_features))
}

/// COLMAP's cap - the key points of the largest scales when there are too many - then the order
/// of the GPU kernel: by row, column, scale, orientation.
fn cap_and_order(mut keypoints: Vec<[f32; 4]>, mut descriptors: Vec<u8>, max_features: usize)
    -> (Vec<[f32; 4]>, Vec<u8>) {
    let pick = |idx: &[usize], keypoints: &[[f32; 4]], descriptors: &[u8]| -> (Vec<[f32; 4]>, Vec<u8>) {
        let kp: Vec<[f32; 4]> = idx.iter().map(|&i| keypoints[i]).collect();
        let mut d = Vec::with_capacity(idx.len() * DESC);
        for &i in idx {
            d.extend_from_slice(&descriptors[i * DESC..(i + 1) * DESC]);
        }
        (kp, d)
    };
    if keypoints.len() > max_features {
        let mut idx: Vec<usize> = (0..keypoints.len()).collect();
        idx.sort_by(|&a, &b| keypoints[b][2].partial_cmp(&keypoints[a][2]).unwrap_or(std::cmp::Ordering::Equal));
        idx.truncate(max_features);
        idx.sort_unstable();
        (keypoints, descriptors) = pick(&idx, &keypoints, &descriptors);
    }
    let mut idx: Vec<usize> = (0..keypoints.len()).collect();
    idx.sort_by(|&a, &b| {
        let (ka, kb) = (&keypoints[a], &keypoints[b]);
        [1usize, 0, 2, 3].iter().map(|&i| ka[i].partial_cmp(&kb[i]).unwrap_or(std::cmp::Ordering::Equal))
            .find(|o| *o != std::cmp::Ordering::Equal).unwrap_or(std::cmp::Ordering::Equal)
    });
    pick(&idx, &keypoints, &descriptors)
}

// ------------------------------------------------------------------------------- the scale space

/// VLFeat's doubling: a pixel copied, the one between two the mean (rows, then columns); the base
/// of octave -1, computed as the GPU kernel computes it before the upload.
fn upsample2(img: &[f32], w: usize, h: usize) -> Vec<f32> {
    let (w2, h2) = (2 * w, 2 * h);
    let mut rows = vec![0.0f32; w2 * h];
    for y in 0..h {
        for x in 0..w {
            let v = img[y * w + x];
            let n = img[y * w + (x + 1).min(w - 1)];
            rows[y * w2 + 2 * x] = v;
            rows[y * w2 + 2 * x + 1] = 0.5 * (v + n);
        }
    }
    let mut out = vec![0.0f32; w2 * h2];
    for y in 0..h {
        let yn = (y + 1).min(h - 1);
        for x in 0..w2 {
            let v = rows[y * w2 + x];
            let n = rows[yn * w2 + x];
            out[(2 * y) * w2 + x] = v;
            out[(2 * y + 1) * w2 + x] = 0.5 * (v + n);
        }
    }
    out
}

/// Every other pixel of `img` (`w` wide): `nw x nh`, as VLFeat halves an octave.
fn decimate(img: &[f32], w: usize, nw: usize, nh: usize) -> Vec<f32> {
    let mut out = vec![0.0f32; nw * nh];
    for y in 0..nh {
        for x in 0..nw {
            out[y * nw + x] = img[(2 * y) * w + 2 * x];
        }
    }
    out
}

/// The weights of the GPU kernel's Gaussian: radius ceil(4 sigma), at least 1 and at most 47,
/// exp(-i^2 / 2 sigma^2) for i = 0..=radius, each divided by the sum over the whole window.
fn gaussian(sigma: f32) -> Vec<f32> {
    let radius = ((4.0 * sigma).ceil().max(1.0) as usize).min(47);
    let mut w = vec![0.0f32; radius + 1];
    let mut total = 0.0f32;
    for (i, v) in w.iter_mut().enumerate() {
        *v = (-((i * i) as f32) / (2.0 * sigma * sigma)).exp();
        total += if i == 0 { *v } else { 2.0 * *v };
    }
    for v in &mut w {
        *v /= total;
    }
    w
}

/// The separable blur of the kernel, the coordinates clamped at the borders: the rows, then the
/// columns of the result; every pixel sums its 2 r + 1 terms from -r to r, as the kernel does. The
/// rows are shared out among the threads.
fn blur(src: &[f32], w: usize, h: usize, weights: &[f32]) -> Vec<f32> {
    let r = weights.len() - 1;
    let mut tmp = vec![0.0f32; w * h];
    tmp.par_chunks_mut(w).zip(src.par_chunks(w)).for_each(|(out, row)| {
        for k in 0..=2 * r {
            let wk = weights[k.abs_diff(r)];
            let shift = k as isize - r as isize;
            // the interior (x + shift inside the row) without the clamp, the two borders with it;
            // a row narrower than the radius has no interior and every pixel clamps, as on the GPU
            let lo = ((-shift).max(0) as usize).min(w);
            let hi = ((w as isize - shift.max(0)).max(0) as usize).max(lo);
            for x in lo..hi {
                out[x] += row[(x as isize + shift) as usize] * wk;
            }
            for x in (0..lo).chain(hi..w) {
                out[x] += row[(x as isize + shift).clamp(0, w as isize - 1) as usize] * wk;
            }
        }
    });
    let mut dst = vec![0.0f32; w * h];
    dst.par_chunks_mut(w).enumerate().for_each(|(y, out)| {
        for k in 0..=2 * r {
            let wk = weights[k.abs_diff(r)];
            let yy = (y as isize + k as isize - r as isize).clamp(0, h as isize - 1) as usize;
            let row = &tmp[yy * w..(yy + 1) * w];
            for (o, v) in out.iter_mut().zip(row) {
                *o += v * wk;
            }
        }
    });
    dst
}

/// The difference of two Gaussian levels, `b - a`.
fn difference(a: &[f32], b: &[f32]) -> Vec<f32> {
    let mut d = vec![0.0f32; a.len()];
    d.par_chunks_mut(4096).zip(a.par_chunks(4096)).zip(b.par_chunks(4096)).for_each(|((d, a), b)| {
        for ((d, a), b) in d.iter_mut().zip(a).zip(b) {
            *d = b - a;
        }
    });
    d
}

// ------------------------------------------------------------------------------------ the extrema

/// VLFeat's extrema of the middle difference `d1` (its neighbours `d0` and `d2`), as the kernel
/// finds them: a pixel above (or below) its 26 neighbours and above 0.8 of the peak threshold,
/// then up to five quadratic refinements that may move it by a pixel, the peak and edge tests.
/// Appends `(x, y, s, value)` with the refinement applied, `s` the scale of `d1`, in row order.
fn extrema(d0: &[f32], d1: &[f32], d2: &[f32], w: usize, h: usize, s: f32, o: &Options, out: &mut Vec<[f32; 4]>) {
    let at = |level: usize, x: i32, y: i32| -> f32 {
        let i = y as usize * w + x as usize;
        match level {
            0 => d0[i],
            1 => d1[i],
            _ => d2[i],
        }
    };
    let (peak, edge) = (o.peak_threshold, o.edge_threshold);
    let (wi, hi) = (w as i32, h as i32);
    let rows: Vec<Vec<[f32; 4]>> = (1..hi - 1).into_par_iter().map(|y0| {
        let mut found = Vec::new();
        for x0 in 1..wi - 1 {
            let (mut x, mut y) = (x0, y0);
            let v = at(1, x, y);
            if v.abs() < 0.8 * peak {
                continue;
            }
            let (mut is_max, mut is_min) = (v > 0.0, v < 0.0);
            'neighbours: for l in 0..3 {
                for dy in -1..=1 {
                    for dx in -1..=1 {
                        if l == 1 && dx == 0 && dy == 0 {
                            continue;
                        }
                        let n = at(l, x + dx, y + dy);
                        is_max = is_max && v > n;
                        is_min = is_min && v < n;
                        if !(is_max || is_min) {
                            break 'neighbours;
                        }
                    }
                }
            }
            if !(is_max || is_min) {
                continue;
            }
            let (mut bx, mut by, mut bs) = (0.0f32, 0.0f32, 0.0f32);
            let (mut gx, mut gy, mut gs) = (0.0f32, 0.0f32, 0.0f32);
            let (mut dxx, mut dyy, mut dxy) = (0.0f32, 0.0f32, 0.0f32);
            for _ in 0..5 {
                let c = at(1, x, y);
                gx = 0.5 * (at(1, x + 1, y) - at(1, x - 1, y));
                gy = 0.5 * (at(1, x, y + 1) - at(1, x, y - 1));
                gs = 0.5 * (at(2, x, y) - at(0, x, y));
                dxx = at(1, x + 1, y) + at(1, x - 1, y) - 2.0 * c;
                dyy = at(1, x, y + 1) + at(1, x, y - 1) - 2.0 * c;
                let dss = at(2, x, y) + at(0, x, y) - 2.0 * c;
                dxy = 0.25 * (at(1, x + 1, y + 1) + at(1, x - 1, y - 1) - at(1, x + 1, y - 1) - at(1, x - 1, y + 1));
                let dxs = 0.25 * (at(2, x + 1, y) + at(0, x - 1, y) - at(2, x - 1, y) - at(0, x + 1, y));
                let dys = 0.25 * (at(2, x, y + 1) + at(0, x, y - 1) - at(2, x, y - 1) - at(0, x, y + 1));
                // H b = -g, H symmetric (Cramer)
                let (a11, a12, a13, a22, a23, a33) = (dxx, dxy, dxs, dyy, dys, dss);
                let det = a11 * (a22 * a33 - a23 * a23) - a12 * (a12 * a33 - a23 * a13) + a13 * (a12 * a23 - a22 * a13);
                if det.abs() < 1e-20 {
                    bx = 0.0;
                    by = 0.0;
                    bs = 0.0;
                } else {
                    let (r1, r2, r3) = (-gx, -gy, -gs);
                    bx = (r1 * (a22 * a33 - a23 * a23) - a12 * (r2 * a33 - a23 * r3) + a13 * (r2 * a23 - a22 * r3))
                        / det;
                    by = (a11 * (r2 * a33 - a23 * r3) - r1 * (a12 * a33 - a23 * a13) + a13 * (a12 * r3 - r2 * a13))
                        / det;
                    bs = (a11 * (a22 * r3 - r2 * a23) - a12 * (a12 * r3 - r2 * a13) + r1 * (a12 * a23 - a22 * a13))
                        / det;
                }
                let mx = if bx > 0.6 && x < wi - 2 { 1 } else if bx < -0.6 && x > 1 { -1 } else { 0 };
                let my = if by > 0.6 && y < hi - 2 { 1 } else if by < -0.6 && y > 1 { -1 } else { 0 };
                if mx == 0 && my == 0 {
                    break;
                }
                x += mx;
                y += my;
            }
            let val = at(1, x, y) + 0.5 * (gx * bx + gy * by + gs * bs);
            let tr = dxx + dyy;
            let det2 = dxx * dyy - dxy * dxy;
            let score = tr * tr / det2;
            let ok = val.abs() > peak && score < (edge + 1.0) * (edge + 1.0) / edge && score >= 0.0
                && bx.abs() < 1.5 && by.abs() < 1.5 && bs.abs() < 1.5;
            if ok {
                found.push([x as f32 + bx, y as f32 + by, s + bs, val]);
            }
        }
        found
    }).collect();
    out.extend(rows.into_iter().flatten());
}

// ------------------------------------------------------------------- orientations and descriptors

/// An angle brought into [0, 2 pi] as the kernel does it.
#[inline]
fn mod2pi(a: f32) -> f32 {
    let mut r = a;
    while r > 2.0 * PI {
        r -= 2.0 * PI;
    }
    while r < 0.0 {
        r += 2.0 * PI;
    }
    r
}

/// The oriented key points `(x, y, sigma, orientation)` of one candidate `(x, y, s, value)`, in
/// the pixels of the octave: the Gaussian-weighted histogram of 36 bins of the gradient
/// directions around it, smoothed six times, its peaks above 0.8 of the highest refined by a
/// parabola, the strongest first, at most `max_orient` of them; one point with orientation 0 when
/// `upright`.
fn orientations(oct: &Octave, c: &[f32; 4], sc: &Scales, max_orient: usize, upright: bool) -> Vec<[f32; 4]> {
    let (x, y, s) = (c[0], c[1], c[2]);
    let sigma = sc.sigma0 * 2f32.powf(s * sc.s_scale);
    if upright {
        return vec![[x, y, sigma, 0.0]];
    }
    let level = Scales::level_of(s);
    let winf = 1.5f32;
    let (xi, yi) = ((x + 0.5) as i32, (y + 0.5) as i32);
    let big_w = (3.0 * winf * sigma).floor() as i32;
    let wsig = winf * sigma;
    let mut hist = [0.0f32; 36];
    let (w, h) = (oct.w as i32, oct.h as i32);
    for ys in (-big_w).max(1 - yi)..=big_w.min(h - 2 - yi) {
        for xs in (-big_w).max(1 - xi)..=big_w.min(w - 2 - xi) {
            let (dx, dy) = ((xi + xs) as f32 - x, (yi + ys) as f32 - y);
            let r2 = dx * dx + dy * dy;
            if r2 >= (big_w * big_w) as f32 + 0.6 {
                continue;
            }
            let (gx, gy) = oct.gradient(level, xi + xs, yi + ys);
            let m = (gx * gx + gy * gy).sqrt(); // the kernel's length(g)
            let ang = mod2pi(gy.atan2(gx));
            let wgt = (-r2 / (2.0 * wsig * wsig)).exp();
            let fbin = 36.0 * ang / (2.0 * PI);
            let bin = (fbin - 0.5).floor() as i32;
            let rbin = fbin - bin as f32 - 0.5;
            hist[((bin + 36) % 36) as usize] += (1.0 - rbin) * m * wgt;
            hist[((bin + 1 + 36) % 36) as usize] += rbin * m * wgt;
        }
    }
    // smoothed six times with [1 1 1]/3, in place as VLFeat
    for _ in 0..6 {
        let mut prev = hist[35];
        let first = hist[0];
        for i in 0..36 {
            let nxt = if i == 35 { first } else { hist[i + 1] };
            let cur = hist[i];
            hist[i] = (prev + cur + nxt) / 3.0;
            prev = cur;
        }
    }
    let maxh = hist.iter().fold(0.0f32, |a, &b| a.max(b));
    // the peaks, the strongest first (up to four kept, max_orient given out)
    let (mut best_th, mut best_h, mut n) = ([0.0f32; 4], [0.0f32; 4], 0usize);
    for i in 0..36 {
        let (h0, hm, hp) = (hist[i], hist[(i + 35) % 36], hist[(i + 1) % 36]);
        if h0 > 0.8 * maxh && h0 > hm && h0 > hp {
            let di = -0.5 * (hp - hm) / (hp + hm - 2.0 * h0);
            let th = 2.0 * PI * (i as f32 + di + 0.5) / 36.0;
            let mut j = n;
            while j > 0 && best_h[j - 1] < h0 {
                if j < 4 {
                    best_h[j] = best_h[j - 1];
                    best_th[j] = best_th[j - 1];
                }
                j -= 1;
            }
            if j < 4 {
                best_h[j] = h0;
                best_th[j] = th;
            }
            n = (n + 1).min(4);
        }
    }
    (0..n.min(max_orient)).map(|i| [x, y, sigma, best_th[i]]).collect()
}

/// The descriptor of one oriented key point `(x, y, sigma, orientation)` in the pixels of the
/// octave: the 4 x 4 x 8 histogram of VLFeat (bins of 3 sigma, the Gaussian window of 2 bins, the
/// gradients turned by the orientation, VLFeat's way round), normalised to unit L2, clipped at
/// 0.2, normalised again, then COLMAP's bytes (the square root of the L1-normalised value, x 512,
/// rounded to the nearest, ties to even as the GPU rounds).
fn descriptor(oct: &Octave, kp: &[f32; 4], sc: &Scales) -> [u8; DESC] {
    let (x, y, sigma, th) = (kp[0], kp[1], kp[2], kp[3]);
    let s = (sigma / sc.sigma0).max(1e-6).log2() / sc.s_scale;
    let level = Scales::level_of(s);
    let magnif = 3.0f32;
    let sbp = magnif * sigma;
    let big_w = (2f32.sqrt() * sbp * 2.5 + 0.5).floor() as i32;
    let (xi, yi) = ((x + 0.5) as i32, (y + 0.5) as i32);
    let (st0, ct0) = (th.sin(), th.cos());
    let wsigma = 2.0f32;
    let mut d = [0.0f32; DESC];
    let (w, h) = (oct.w as i32, oct.h as i32);
    for dyi in (-big_w).max(1 - yi)..=big_w.min(h - 2 - yi) {
        for dxi in (-big_w).max(1 - xi)..=big_w.min(w - 2 - xi) {
            let (gx, gy) = oct.gradient(level, xi + dxi, yi + dyi);
            let m = (gx * gx + gy * gy).sqrt();
            let ang = mod2pi(gy.atan2(gx));
            let (dx, dy) = ((xi + dxi) as f32 - x, (yi + dyi) as f32 - y);
            let nx = (ct0 * dx + st0 * dy) / sbp;
            let ny = (-st0 * dx + ct0 * dy) / sbp;
            let nt = 8.0 * mod2pi(th - ang) / (2.0 * PI); // VLFeat's bins run this way round
            let win = (-(nx * nx + ny * ny) / (2.0 * wsigma * wsigma)).exp();
            let (binx, biny, bint) = ((nx - 0.5).floor() as i32, (ny - 0.5).floor() as i32, nt.floor() as i32);
            let (rbinx, rbiny, rbint) = (nx - (binx as f32 + 0.5), ny - (biny as f32 + 0.5), nt - bint as f32);
            for dbinx in 0..2 {
                for dbiny in 0..2 {
                    for dbint in 0..2 {
                        let (bx, by) = (binx + dbinx, biny + dbiny);
                        if (-2..2).contains(&bx) && (-2..2).contains(&by) {
                            let wgt = win * m * (1.0 - dbinx as f32 - rbinx).abs() * (1.0 - dbiny as f32 - rbiny).abs()
                                * (1.0 - dbint as f32 - rbint).abs();
                            let bt = (bint + dbint) % 8;
                            d[((by + 2) * 32 + (bx + 2) * 8 + bt) as usize] += wgt;
                        }
                    }
                }
            }
        }
    }
    // VLFeat: unit L2, clipped at 0.2, unit again; COLMAP: L1 root, x 512, bytes
    let mut n2 = 0.0f32;
    for v in &d {
        n2 += v * v;
    }
    n2 = n2.sqrt().max(1e-12);
    for v in &mut d {
        *v = (*v / n2).min(0.2);
    }
    n2 = 0.0;
    for v in &d {
        n2 += v * v;
    }
    n2 = n2.sqrt().max(1e-12);
    let mut n1 = 0.0f32;
    for v in &mut d {
        *v /= n2;
        n1 += *v;
    }
    n1 = n1.max(1e-12);
    let mut out = [0u8; DESC];
    for (o, v) in out.iter_mut().zip(&d) {
        *o = ((v / n1).sqrt() * 512.0).round_ties_even().clamp(0.0, 255.0) as u8;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The blur is the kernel's, pixel by pixel: every pixel the sum from -r to r of the clamped
    /// neighbour times the weight, rows then columns - written here as one scalar loop, which the
    /// blurred rows (interior and borders apart, the columns as row operations) must equal bit
    /// for bit; also on rows and columns narrower than the radius (8 pixels against radii of 13
    /// and 45), where every pixel clamps.
    #[test]
    fn the_blur_is_the_clamped_separable_sum_of_the_kernel() {
        let cases = [(37usize, 23usize, 0.6f32), (37, 23, 1.2491), (37, 23, 2.0159), (37, 23, 3.1),
                     (8, 8, 3.1), (8, 8, 11.1), (8, 23, 3.1), (23, 8, 3.1)];
        for (w, h, sigma) in cases {
            let img: Vec<f32> = (0..w * h).map(|i| ((i * 7919 % 1000) as f32) / 1000.0).collect();
            let weights = gaussian(sigma);
            let r = weights.len() as i32 - 1;
            let weight = |k: i32| weights[k.unsigned_abs() as usize];
            assert!((weights[0] + 2.0 * weights[1..].iter().sum::<f32>() - 1.0).abs() < 1e-5);
            let mut rows = vec![0.0f32; w * h];
            for y in 0..h as i32 {
                for x in 0..w as i32 {
                    let mut acc = 0.0f32;
                    for k in -r..=r {
                        acc += img[y as usize * w + (x + k).clamp(0, w as i32 - 1) as usize] * weight(k);
                    }
                    rows[y as usize * w + x as usize] = acc;
                }
            }
            let mut want = vec![0.0f32; w * h];
            for y in 0..h as i32 {
                for x in 0..w as i32 {
                    let mut acc = 0.0f32;
                    for k in -r..=r {
                        acc += rows[(y + k).clamp(0, h as i32 - 1) as usize * w + x as usize] * weight(k);
                    }
                    want[y as usize * w + x as usize] = acc;
                }
            }
            assert!(blur(&img, w, h, &weights) == want, "{w} x {h}, sigma {sigma}: not the scalar sum");
        }
    }
}
