//! Depth maps: fusion of the estimates of one photo, disparity back to depth, normals, and the
//! agreement of points with a depth map (confidence). Each function reproduces the numpy code
//! of `engine/dense.py` and `engine/confidence.py` operation by operation.

use rayon::prelude::*;
use std::sync::atomic::{AtomicU32, Ordering};

/// `engine.dense._combine`: the estimates of one photo made with different partners, `k` maps of
/// `h * w` float32 values (0 = no depth), merged by the median rule of the Python code, in float32.
pub fn combine(data: &[f32], k: usize, h: usize, w: usize, min_agree: usize, tol: f64) -> Vec<f32> {
    let plane = h * w;
    let tol = tol.max(1e-6) as f32;
    let mut out = vec![0f32; plane];
    out.par_chunks_mut(w.max(1)).enumerate().for_each(|(row, orow)| {
        let mut vals = vec![0f32; k];
        let mut sorted = vec![0f32; k];
        for (col, o) in orow.iter_mut().enumerate() {
            let mut n = 0;
            for e in 0..k {
                let v = data[e * plane + row * w + col];
                if v > 0.0 {
                    vals[n] = v;
                    n += 1;
                }
            }
            if n == 0 {
                *o = 0.0;
                continue;
            }
            sorted[..n].copy_from_slice(&vals[..n]);
            sorted[..n].sort_unstable_by(|a, b| a.partial_cmp(b).unwrap());
            let med = if n % 2 == 1 { sorted[n / 2] } else { (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0 };
            let lim = tol * med;
            // np.nanmean of the agreeing values: summed in the order of the partners
            let (mut agree, mut sum) = (0usize, 0f32);
            for &v in &vals[..n] {
                if (v - med).abs() <= lim {
                    agree += 1;
                    sum += v;
                }
            }
            let mut depth = if agree > 0 { sum / agree as f32 } else { f32::NAN };
            let keep = if min_agree == 0 {
                if !depth.is_finite() {
                    depth = med;
                }
                true
            } else {
                agree > min_agree || (n == 1 && min_agree < 2)
            };
            *o = if keep && depth.is_finite() { depth } else { 0.0 };
        }
    });
    out
}

/// Pinhole intrinsics `(fx, fy, cx, cy)`.
pub type Intrinsics = (f64, f64, f64, f64);

/// The end of `engine.dense._pair_depth`: disparities of the rectified pair (valid above
/// `min_valid`) to points of camera i (`r1`: rectification rotation, applied transposed), projected
/// with `k` into an `out_h x out_w` z-buffer that keeps the nearest point of every pixel.
#[allow(clippy::too_many_arguments)]
pub fn disparity_to_depth(disp: &[f32], h: usize, w: usize, min_valid: f64, f: f64, baseline: f64, cx: f64, cy: f64,
                          r1: &[[f64; 3]; 3], k: Intrinsics, out_h: usize, out_w: usize) -> Vec<f32> {
    assert_eq!(disp.len(), h * w, "disparity map of the wrong size");
    let threshold = min_valid as f32;
    let fb = f * baseline;
    let (kfx, kfy, kcx, kcy) = k;
    let zbuffer: Vec<AtomicU32> = (0..out_h * out_w).map(|_| AtomicU32::new(f32::INFINITY.to_bits())).collect();
    disp.par_chunks(w.max(1)).enumerate().for_each(|(yy, row)| {
        for (xx, &d) in row.iter().enumerate() {
            if !(d > threshold) {
                continue;
            }
            let z = fb / d as f64;
            let px = (xx as f64 - cx) * z / f;
            let py = (yy as f64 - cy) * z / f;
            // pts_rect @ R1
            let x = px * r1[0][0] + py * r1[1][0] + z * r1[2][0];
            let y = px * r1[0][1] + py * r1[1][1] + z * r1[2][1];
            let zc = px * r1[0][2] + py * r1[1][2] + z * r1[2][2];
            if !(zc > 0.0) {
                continue;
            }
            let u = (kfx * x / zc + kcx).round_ties_even();
            let v = (kfy * y / zc + kcy).round_ties_even();
            if !(u >= 0.0 && u < out_w as f64 && v >= 0.0 && v < out_h as f64) {
                continue;
            }
            // positive float32 values sort like their bits: an atomic minimum is the z-buffer
            zbuffer[v as usize * out_w + u as usize].fetch_min((zc as f32).to_bits(), Ordering::Relaxed);
        }
    });
    zbuffer
        .into_iter()
        .map(|a| {
            let z = f32::from_bits(a.into_inner());
            if z.is_finite() { z } else { 0.0 }
        })
        .collect()
}

/// `engine.dense._normals_from_depth`: normals (facing the camera) from central differences of
/// the back-projected depth map, computed in float64 and returned as float32 `h x w x 3`.
pub fn normals(depth: &[f32], h: usize, w: usize, k: Intrinsics) -> Vec<f32> {
    let (fx, fy, cx, cy) = k;
    let point = |r: usize, c: usize| -> [f64; 3] {
        let d = depth[r * w + c] as f64;
        [(c as f64 - cx) * d / fx, (r as f64 - cy) * d / fy, d]
    };
    let mut out = vec![0f32; h * w * 3];
    out.par_chunks_mut((w * 3).max(1)).enumerate().for_each(|(r, orow)| {
        for c in 0..w {
            let d = depth[r * w + c];
            if !(d > 0.0) || c == 0 || c + 1 >= w || r == 0 || r + 1 >= h {
                continue; // no depth, or a border pixel (one of the differences is zero)
            }
            let (a0, a1) = (point(r, c + 1), point(r, c - 1));
            let (b0, b1) = (point(r + 1, c), point(r - 1, c));
            let dx = [a0[0] - a1[0], a0[1] - a1[1], a0[2] - a1[2]];
            let dy = [b0[0] - b1[0], b0[1] - b1[1], b0[2] - b1[2]];
            let mut n = [dx[1] * dy[2] - dx[2] * dy[1], dx[2] * dy[0] - dx[0] * dy[2], dx[0] * dy[1] - dx[1] * dy[0]];
            let norm = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]).sqrt();
            if !(norm > 0.0) {
                continue;
            }
            for v in n.iter_mut() {
                *v /= norm;
            }
            let p = point(r, c);
            if n[0] * p[0] + n[1] * p[1] + n[2] * p[2] > 0.0 {
                for v in n.iter_mut() {
                    *v = -*v;
                }
            }
            for (j, v) in n.iter().enumerate() {
                orow[c * 3 + j] = *v as f32;
            }
        }
    });
    out
}

/// `engine.confidence._agreeing`: which candidate points (`cand`, indices into `points`) a depth
/// map agrees with - its surface within `tol` x the distance (or the point's `abs_tol`) of the point.
#[allow(clippy::too_many_arguments)]
pub fn agree(points: &[f64], cand: &[i64], depth: &[f32], h: usize, w: usize, k: Intrinsics, rot: &[[f64; 3]; 3],
             t: &[f64; 3], tol: f64, abs_tol: Option<&[f64]>) -> Vec<bool> {
    let (fx, fy, cx, cy) = k;
    cand.par_iter()
        .map(|&i| {
            let i = i as usize;
            let (x, y, z) = (points[3 * i], points[3 * i + 1], points[3 * i + 2]);
            // points @ rot.T + t
            let pcx = x * rot[0][0] + y * rot[0][1] + z * rot[0][2] + t[0];
            let pcy = x * rot[1][0] + y * rot[1][1] + z * rot[1][2] + t[1];
            let zc = x * rot[2][0] + y * rot[2][1] + z * rot[2][2] + t[2];
            if !(zc > 1e-9) {
                return false;
            }
            let u = (fx * pcx / zc + cx).round_ties_even();
            let v = (fy * pcy / zc + cy).round_ties_even();
            if !(u >= 0.0 && u < w as f64 && v >= 0.0 && v < h as f64) {
                return false;
            }
            let d = depth[v as usize * w + u as usize] as f64;
            let mut limit = tol * zc;
            if let Some(a) = abs_tol {
                limit = limit.max(a[i]);
            }
            d > 0.0 && (d - zc).abs() <= limit
        })
        .collect()
}
