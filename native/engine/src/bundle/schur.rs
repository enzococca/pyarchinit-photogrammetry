//! The normal equations reduced to the cameras: the 3 x 3 blocks of the points eliminated (the
//! Schur complement), what is left ordered as the blocks of the photos (the pose of a free photo,
//! then its rolling shutter), the markers and, last, the border of the free intrinsics and the
//! flight biases; the dense Cholesky factorisation of faer on it; the points back-substituted.
//! Forming the reduced system is parallel over its column blocks, each written by one thread in
//! a fixed order, and the sums that end in the border are folded chunk by chunk in a fixed order,
//! so that two runs give the same bits.

use faer::dyn_stack::{MemBuffer, MemStack};
use faer::linalg::cholesky::llt::{factor, solve as llt_solve};
use faer::{MatMut, MatRef};
use rayon::prelude::*;

use super::problem::{Layout, Problem};
use super::residuals::{Evaluated, Structure, OFF_CENTRE, OFF_DELTA, OFF_INTR, OFF_POINT};

/// Points per chunk of the sums that end in the border.
const CHUNK: usize = 1024;

/// The columns of the reduced system and the column of `Layout` each one stands for.
pub(crate) struct Reduced {
    pub photo_start: Vec<usize>,
    /// 0 for a fixed photo without rolling shutter, 6 for a free photo or a fixed one with
    /// rolling shutter, 12 for a free photo with rolling shutter.
    pub photo_len: Vec<usize>,
    pub marker_start: usize,
    pub border_start: usize,
    pub camera_start: Vec<usize>,
    pub bias_start: usize,
    pub size: usize,
    pub layout_col: Vec<usize>,
}

impl Reduced {
    pub(crate) fn new(pb: &Problem, lay: &Layout) -> Reduced {
        let n = pb.centres.len();
        let rs = if pb.rolling_shutter { 6 } else { 0 };
        let mut layout_col = Vec::new();
        let mut photo_start = Vec::with_capacity(n);
        let mut photo_len = Vec::with_capacity(n);
        for i in 0..n {
            photo_start.push(layout_col.len());
            let mut len = 0;
            if let Some(c0) = lay.image_col[i] {
                layout_col.extend(c0..c0 + 6);
                len += 6;
            }
            if let Some(r0) = lay.rs_col_of(i) {
                layout_col.extend(r0..r0 + 6);
                len += rs;
            }
            photo_len.push(len);
        }
        let marker_start = layout_col.len();
        layout_col.extend(lay.marker_col..lay.marker_col + 3 * pb.markers.len());
        let border_start = layout_col.len();
        let mut camera_start = Vec::with_capacity(pb.cameras.len());
        for c in 0..pb.cameras.len() {
            camera_start.push(layout_col.len());
            layout_col.extend(lay.camera_cols(c));
        }
        let bias_start = layout_col.len();
        layout_col.extend(lay.bias_col..lay.bias_col + 3 * lay.flights.len());
        let size = layout_col.len();
        Reduced { photo_start, photo_len, marker_start, border_start, camera_start, bias_start, size, layout_col }
    }
}

/// The 3 x 3 block of a point after the elimination: the inverse of its damped `J^T W J` and
/// `y = V^-1 g`; `held` when the block is singular (no usable observation): the point keeps its
/// place in this step.
struct PointBlock {
    vinv: [f64; 9],
    y: [f64; 3],
    held: bool,
}

/// What every column block reads.
struct Context<'a> {
    pb: &'a Problem,
    lay: &'a Layout,
    st: &'a Structure,
    red: &'a Reduced,
    ev: &'a Evaluated,
    g: &'a [f64],
    damping: &'a [f64],
    points: &'a [PointBlock],
}

impl Context<'_> {
    /// Entries of a Jacobian row for the block of the photo `i` (the pose, then the rolling
    /// shutter), into `out`; the length of the block.
    #[inline]
    fn camera_entries(&self, i: usize, row: &[f64], out: &mut [f64; 12]) -> usize {
        let mut len = 0;
        if self.lay.image_col[i].is_some() {
            out[..6].copy_from_slice(&row[OFF_DELTA..OFF_DELTA + 6]);
            len = 6;
        }
        if self.pb.rolling_shutter {
            out[len..len + 6].copy_from_slice(&row[self.st.off_rs..self.st.off_rs + 6]);
            len += 6;
        }
        len
    }

    /// The number and the first border column of the free intrinsics of the camera of photo `i`.
    #[inline]
    fn intrinsics(&self, i: usize) -> (usize, usize) {
        let c = self.pb.image_camera[i];
        (self.pb.cameras[c].free.len(), self.red.camera_start[c])
    }
}

/// Inverse of a symmetric positive-definite 3 x 3 matrix (row-major) through its Cholesky
/// factor; `None` when a pivot is not positive (or not a number).
pub(crate) fn sym3_inverse(v: &[f64; 9]) -> Option<[f64; 9]> {
    if !(v[0] > 0.0) {
        return None;
    }
    let l00 = v[0].sqrt();
    let l10 = v[3] / l00;
    let l20 = v[6] / l00;
    let d11 = v[4] - l10 * l10;
    if !(d11 > 0.0) {
        return None;
    }
    let l11 = d11.sqrt();
    let l21 = (v[7] - l20 * l10) / l11;
    let d22 = v[8] - l20 * l20 - l21 * l21;
    if !(d22 > 0.0) {
        return None;
    }
    let l22 = d22.sqrt();
    // the inverse of L, lower triangular, then V^-1 = L^-T L^-1
    let m00 = 1.0 / l00;
    let m11 = 1.0 / l11;
    let m22 = 1.0 / l22;
    let m10 = -l10 * m00 * m11;
    let m21 = -l21 * m11 * m22;
    let m20 = (l10 * l21 - l20 * l11) * m00 * m11 * m22;
    let (i01, i02, i12) = (m10 * m11 + m20 * m21, m20 * m22, m21 * m22);
    Some([
        m00 * m00 + m10 * m10 + m20 * m20,
        i01,
        i02,
        i01,
        m11 * m11 + m21 * m21,
        i12,
        i02,
        i12,
        m22 * m22,
    ])
}

fn point_blocks(
    pb: &Problem,
    lay: &Layout,
    st: &Structure,
    ev: &Evaluated,
    g: &[f64],
    damping: &[f64],
) -> Vec<PointBlock> {
    (0..pb.points.len())
        .into_par_iter()
        .map(|p| {
            let mut v = [0.0; 9];
            for &j in st.point_obs.of(p) {
                for row in 2 * j..2 * j + 2 {
                    let (jr, w) = (ev.obs_row(st, row), ev.weights[row]);
                    for a in 0..3 {
                        let wa = w * jr[OFF_POINT + a];
                        for b in 0..3 {
                            v[3 * a + b] += wa * jr[OFF_POINT + b];
                        }
                    }
                }
            }
            let c0 = lay.point_col + 3 * p;
            for k in 0..3 {
                v[4 * k] += damping[c0 + k];
            }
            match sym3_inverse(&v) {
                Some(vinv) => {
                    let gp = &g[c0..c0 + 3];
                    let y = [
                        vinv[0] * gp[0] + vinv[1] * gp[1] + vinv[2] * gp[2],
                        vinv[3] * gp[0] + vinv[4] * gp[1] + vinv[5] * gp[2],
                        vinv[6] * gp[0] + vinv[7] * gp[1] + vinv[8] * gp[2],
                    ];
                    PointBlock { vinv, y, held: false }
                }
                None => PointBlock { vinv: [0.0; 9], y: [0.0; 3], held: true },
            }
        })
        .collect()
}

/// The column block of the photo `i`: its diagonal block, the blocks of the photos after it that
/// share a point with it (the Schur terms), the markers it observes, the border; and its part of
/// the right-hand side. `block` holds the `photo_len[i]` columns, column-major, `size` rows each.
fn photo_column(cx: &Context, i: usize, block: &mut [f64], rhs: &mut [f64]) {
    let (red, lay, st, ev, pb) = (cx.red, cx.lay, cx.st, cx.ev, cx.pb);
    let c_i = red.photo_len[i];
    if c_i == 0 {
        return;
    }
    let (n, s_i) = (red.size, red.photo_start[i]);
    for k in 0..c_i {
        let lc = red.layout_col[s_i + k];
        block[k * n + s_i + k] += cx.damping[lc];
        rhs[k] = -cx.g[lc];
    }
    let (k_c, intr) = cx.intrinsics(i);
    let mut jc = [0.0; 12];
    for &j in st.image_obs.of(i) {
        let p = pb.obs[j].point;
        let mut w_ip = [[0.0; 3]; 12];
        for row in 2 * j..2 * j + 2 {
            let (jr, w) = (ev.obs_row(st, row), ev.weights[row]);
            cx.camera_entries(i, jr, &mut jc);
            for a in 0..c_i {
                let wa = w * jc[a];
                for b in 0..c_i {
                    block[b * n + s_i + a] += wa * jc[b];
                }
                for q in 0..k_c {
                    block[a * n + intr + q] += wa * jr[OFF_INTR + q];
                }
                for k in 0..3 {
                    w_ip[a][k] += wa * jr[OFF_POINT + k];
                }
            }
        }
        let pt = &cx.points[p];
        if pt.held {
            continue;
        }
        // Y = W V^-1, the right-hand side + W y, then - W' Y^T for every observation of the point
        let mut y = [[0.0; 3]; 12];
        for a in 0..c_i {
            for k in 0..3 {
                y[a][k] = w_ip[a][0] * pt.vinv[k] + w_ip[a][1] * pt.vinv[3 + k] + w_ip[a][2] * pt.vinv[6 + k];
            }
            rhs[a] += w_ip[a][0] * pt.y[0] + w_ip[a][1] * pt.y[1] + w_ip[a][2] * pt.y[2];
        }
        for &j2 in st.point_obs.of(p) {
            let i2 = pb.obs[j2].image;
            let (c2, s2) = (red.photo_len[i2], red.photo_start[i2]);
            let (k2, intr2) = cx.intrinsics(i2);
            let mut w2 = [[0.0; 3]; 12];
            let mut f2 = [[0.0; 3]; 12];
            for row in 2 * j2..2 * j2 + 2 {
                let (jr, w) = (ev.obs_row(st, row), ev.weights[row]);
                cx.camera_entries(i2, jr, &mut jc);
                for a in 0..c2 {
                    let wa = w * jc[a];
                    for k in 0..3 {
                        w2[a][k] += wa * jr[OFF_POINT + k];
                    }
                }
                for q in 0..k2 {
                    let wq = w * jr[OFF_INTR + q];
                    for k in 0..3 {
                        f2[q][k] += wq * jr[OFF_POINT + k];
                    }
                }
            }
            if c2 > 0 && s2 >= s_i {
                for a2 in 0..c2 {
                    for a in 0..c_i {
                        block[a * n + s2 + a2] -= w2[a2][0] * y[a][0] + w2[a2][1] * y[a][1] + w2[a2][2] * y[a][2];
                    }
                }
            }
            for q in 0..k2 {
                for a in 0..c_i {
                    block[a * n + intr2 + q] -= f2[q][0] * y[a][0] + f2[q][1] * y[a][1] + f2[q][2] * y[a][2];
                }
            }
        }
    }
    for &j in st.image_mobs.of(i) {
        let m0 = red.marker_start + 3 * pb.mobs[j].point;
        for row in 2 * j..2 * j + 2 {
            let jr = ev.mobs_row(st, row);
            cx.camera_entries(i, jr, &mut jc);
            for a in 0..c_i {
                let ja = jc[a];
                for b in 0..c_i {
                    block[b * n + s_i + a] += ja * jc[b];
                }
                for k in 0..3 {
                    block[a * n + m0 + k] += ja * jr[OFF_POINT + k];
                }
                for q in 0..k_c {
                    block[a * n + intr + q] += ja * jr[OFF_INTR + q];
                }
            }
        }
    }
    if lay.image_col[i].is_some() {
        for &j in st.image_gnss.of(i) {
            let f = lay.flight_index(pb.gnss[j].3);
            for k in 0..3 {
                let jc = ev.gnss_jac[j][k];
                block[(OFF_CENTRE + k) * n + s_i + OFF_CENTRE + k] += jc * jc;
                if let Some(f) = f {
                    block[(OFF_CENTRE + k) * n + red.bias_start + 3 * f + k] += ev.gnss_jac[j][3 + k] * jc;
                }
            }
        }
    }
    if pb.rolling_shutter {
        let off = c_i - 6;
        for k in 0..6 {
            let jr = ev.rs_jac[i][k];
            block[(off + k) * n + s_i + off + k] += jr * jr;
        }
    }
}

/// The three columns of the marker `m`: its observations, its control point, its bars (the
/// other marker of a bar after it, and the border), and its right-hand side.
fn marker_column(cx: &Context, m: usize, block: &mut [f64], rhs: &mut [f64]) {
    let (red, lay, st, ev, pb) = (cx.red, cx.lay, cx.st, cx.ev, cx.pb);
    let (n, s_m) = (red.size, red.marker_start + 3 * m);
    for k in 0..3 {
        let lc = lay.marker_col + 3 * m + k;
        block[k * n + s_m + k] += cx.damping[lc];
        rhs[k] = -cx.g[lc];
    }
    for &j in st.marker_mobs.of(m) {
        let (k_c, intr) = cx.intrinsics(pb.mobs[j].image);
        for row in 2 * j..2 * j + 2 {
            let jr = ev.mobs_row(st, row);
            for a in 0..3 {
                let ja = jr[OFF_POINT + a];
                for b in 0..3 {
                    block[b * n + s_m + a] += ja * jr[OFF_POINT + b];
                }
                for q in 0..k_c {
                    block[a * n + intr + q] += ja * jr[OFF_INTR + q];
                }
            }
        }
    }
    for &j in st.marker_gcps.of(m) {
        for k in 0..3 {
            let jv = ev.gcp_jac[j][k];
            block[k * n + s_m + k] += jv * jv;
        }
    }
    for &end in st.marker_bars.of(m) {
        let (j, mine) = (end / 2, 3 * (end % 2));
        let other = if end % 2 == 0 { pb.bars[j].1 } else { pb.bars[j].0 };
        let jm = &ev.bar_jac[j][mine..mine + 3];
        let jo = &ev.bar_jac[j][3 - mine..6 - mine];
        for k in 0..3 {
            for l in 0..3 {
                block[l * n + s_m + k] += jm[k] * jm[l];
            }
        }
        if other > m {
            let s_o = red.marker_start + 3 * other;
            for k in 0..3 {
                for l in 0..3 {
                    block[l * n + s_o + k] += jo[k] * jm[l];
                }
            }
        }
    }
}

/// The columns of the border: the free intrinsics (their own terms photo by photo, the Schur
/// terms of the points chunk by chunk, both folded in a fixed order) and the biases.
fn border_columns(cx: &Context, block: &mut [f64], rhs: &mut [f64]) {
    let (red, lay, st, ev, pb) = (cx.red, cx.lay, cx.st, cx.ev, cx.pb);
    let (n, s_b) = (red.size, red.border_start);
    let nb = n - s_b;
    if nb == 0 {
        return;
    }
    let kt = red.bias_start - s_b;
    for k in 0..nb {
        let lc = red.layout_col[s_b + k];
        block[k * n + s_b + k] += cx.damping[lc];
        rhs[k] = -cx.g[lc];
    }
    let n_photos = pb.centres.len();
    let per_photo: Vec<Vec<f64>> = (0..n_photos)
        .into_par_iter()
        .map(|i| {
            let (k_c, _) = cx.intrinsics(i);
            let mut h = vec![0.0; k_c * k_c];
            let mut add = |jr: &[f64], w: f64| {
                for q in 0..k_c {
                    let wq = w * jr[OFF_INTR + q];
                    for q2 in 0..k_c {
                        h[q * k_c + q2] += wq * jr[OFF_INTR + q2];
                    }
                }
            };
            for &j in st.image_obs.of(i) {
                for row in 2 * j..2 * j + 2 {
                    add(ev.obs_row(st, row), ev.weights[row]);
                }
            }
            for &j in st.image_mobs.of(i) {
                for row in 2 * j..2 * j + 2 {
                    add(ev.mobs_row(st, row), 1.0);
                }
            }
            h
        })
        .collect();
    for (i, h) in per_photo.iter().enumerate() {
        let (k_c, intr) = cx.intrinsics(i);
        let off = intr - s_b;
        for q in 0..k_c {
            for q2 in 0..k_c {
                block[(off + q2) * n + s_b + off + q] += h[q * k_c + q2];
            }
        }
    }
    if kt > 0 {
        let starts: Vec<usize> = (0..pb.points.len()).step_by(CHUNK).collect();
        let chunks: Vec<(Vec<f64>, Vec<f64>)> = starts
            .into_par_iter()
            .map(|start| {
                let mut h = vec![0.0; kt * kt];
                let mut r = vec![0.0; kt];
                let mut f = vec![0.0; 3 * kt];
                for p in start..(start + CHUNK).min(pb.points.len()) {
                    let pt = &cx.points[p];
                    if pt.held {
                        continue;
                    }
                    f.fill(0.0);
                    let mut any = false;
                    for &j in st.point_obs.of(p) {
                        let (k_c, intr) = cx.intrinsics(pb.obs[j].image);
                        if k_c == 0 {
                            continue;
                        }
                        any = true;
                        let off = intr - s_b;
                        for row in 2 * j..2 * j + 2 {
                            let (jr, w) = (ev.obs_row(st, row), ev.weights[row]);
                            for q in 0..k_c {
                                let wq = w * jr[OFF_INTR + q];
                                for k in 0..3 {
                                    f[3 * (off + q) + k] += wq * jr[OFF_POINT + k];
                                }
                            }
                        }
                    }
                    if !any {
                        continue;
                    }
                    for q in 0..kt {
                        let fq = [f[3 * q], f[3 * q + 1], f[3 * q + 2]];
                        if fq == [0.0; 3] {
                            continue;
                        }
                        let fv = [
                            fq[0] * pt.vinv[0] + fq[1] * pt.vinv[3] + fq[2] * pt.vinv[6],
                            fq[0] * pt.vinv[1] + fq[1] * pt.vinv[4] + fq[2] * pt.vinv[7],
                            fq[0] * pt.vinv[2] + fq[1] * pt.vinv[5] + fq[2] * pt.vinv[8],
                        ];
                        for q2 in 0..kt {
                            h[q * kt + q2] -= fv[0] * f[3 * q2] + fv[1] * f[3 * q2 + 1] + fv[2] * f[3 * q2 + 2];
                        }
                        r[q] += fq[0] * pt.y[0] + fq[1] * pt.y[1] + fq[2] * pt.y[2];
                    }
                }
                (h, r)
            })
            .collect();
        for (h, r) in &chunks {
            for q in 0..kt {
                rhs[q] += r[q];
                for q2 in 0..kt {
                    block[q2 * n + s_b + q] += h[q * kt + q2];
                }
            }
        }
    }
    for (j, (_, _, _, flight)) in pb.gnss.iter().enumerate() {
        if let Some(f) = lay.flight_index(*flight) {
            let off = red.bias_start - s_b + 3 * f;
            for k in 0..3 {
                let jb = ev.gnss_jac[j][3 + k];
                block[(off + k) * n + s_b + off + k] += jb * jb;
            }
        }
    }
}

/// Solves `A x = b` for a symmetric positive-definite `A` (`n x n`, column-major, its lower
/// triangle read) in place: the factor replaces `A`, the solution `b`. An error names the pivot
/// that is not positive.
pub(crate) fn cholesky_solve_in_place(matrix: &mut [f64], rhs: &mut [f64]) -> Result<(), String> {
    let n = rhs.len();
    debug_assert_eq!(matrix.len(), n * n);
    if n == 0 {
        return Ok(());
    }
    let par = faer::get_global_parallelism();
    let mut buffer = MemBuffer::new(factor::cholesky_in_place_scratch::<f64>(n, par, Default::default()));
    let stack = MemStack::new(&mut buffer);
    let a = MatMut::from_column_major_slice_mut(matrix, n, n);
    factor::cholesky_in_place(a, factor::LltRegularization::default(), par, stack, Default::default())
        .map_err(|factor::LltError::NonPositivePivot { index }| {
            format!("pivot {index} of the reduced system is not positive")
        })?;
    let l = MatRef::from_column_major_slice(matrix, n, n);
    llt_solve::solve_in_place(l, MatMut::from_column_major_slice_mut(rhs, n, 1), par, stack);
    Ok(())
}

/// The points from the step of the cameras: `delta_p = -V^-1 (g_p + sum_j w_j J_X^T J_cam delta_cam)`
/// over the observations `j` of each point; a held point does not move.
fn back_substitute(cx: &Context, delta: &mut [f64]) {
    let (lay, st, ev, pb) = (cx.lay, cx.st, cx.ev, cx.pb);
    let n_point_cols = 3 * pb.points.len();
    let (head, rest) = delta.split_at_mut(lay.point_col);
    let (point_cols, tail) = rest.split_at_mut(n_point_cols);
    let (head, tail) = (&*head, &*tail);
    let read = |col: usize| if col < lay.point_col { head[col] } else { tail[col - lay.point_col - n_point_cols] };
    point_cols.par_chunks_mut(3).enumerate().for_each(|(p, out)| {
        let pt = &cx.points[p];
        if pt.held {
            return;
        }
        let c0 = lay.point_col + 3 * p;
        let mut acc = [cx.g[c0], cx.g[c0 + 1], cx.g[c0 + 2]];
        for &j in st.point_obs.of(p) {
            let i = pb.obs[j].image;
            let c = pb.image_camera[i];
            for row in 2 * j..2 * j + 2 {
                let (jr, w) = (ev.obs_row(st, row), ev.weights[row]);
                let mut u = 0.0;
                if let Some(c0) = lay.image_col[i] {
                    for a in 0..6 {
                        u += jr[OFF_DELTA + a] * read(c0 + a);
                    }
                }
                for q in 0..pb.cameras[c].free.len() {
                    u += jr[OFF_INTR + q] * read(lay.camera_col[c] + q);
                }
                if let Some(r0) = lay.rs_col_of(i) {
                    for a in 0..6 {
                        u += jr[st.off_rs + a] * read(r0 + a);
                    }
                }
                for k in 0..3 {
                    acc[k] += w * jr[OFF_POINT + k] * u;
                }
            }
        }
        for k in 0..3 {
            out[k] = -(pt.vinv[3 * k] * acc[0] + pt.vinv[3 * k + 1] * acc[1] + pt.vinv[3 * k + 2] * acc[2]);
        }
    });
}

/// One step of the damped normal equations `(J^T W J + diag(damping)) delta = -g`, in the columns
/// of `lay`: the points eliminated, the reduced system solved by a dense Cholesky, the points
/// back-substituted. An error when the reduced system is not positive definite.
pub(crate) fn dense_step(
    pb: &Problem,
    lay: &Layout,
    st: &Structure,
    red: &Reduced,
    ev: &Evaluated,
    g: &[f64],
    damping: &[f64],
) -> Result<Vec<f64>, String> {
    let points = point_blocks(pb, lay, st, ev, g, damping);
    let cx = Context { pb, lay, st, red, ev, g, damping, points: &points };
    let n = red.size;
    let mut delta = vec![0.0; lay.size];
    if n > 0 {
        let mut s = vec![0.0; n * n];
        let mut b = vec![0.0; n];
        {
            let (photo_s, rest_s) = s.split_at_mut(red.marker_start * n);
            let (marker_s, border_s) = rest_s.split_at_mut((red.border_start - red.marker_start) * n);
            let (photo_b, rest_b) = b.split_at_mut(red.marker_start);
            let (marker_b, border_b) = rest_b.split_at_mut(red.border_start - red.marker_start);
            let mut photo_blocks = Vec::with_capacity(pb.centres.len());
            let (mut rest_s, mut rest_b) = (photo_s, photo_b);
            for &len in &red.photo_len {
                let (head_s, tail_s) = rest_s.split_at_mut(len * n);
                let (head_b, tail_b) = rest_b.split_at_mut(len);
                photo_blocks.push((head_s, head_b));
                rest_s = tail_s;
                rest_b = tail_b;
            }
            photo_blocks.into_par_iter().enumerate().for_each(|(i, (block, rhs))| photo_column(&cx, i, block, rhs));
            marker_s
                .par_chunks_mut(3 * n)
                .zip_eq(marker_b.par_chunks_mut(3))
                .enumerate()
                .for_each(|(m, (block, rhs))| marker_column(&cx, m, block, rhs));
            border_columns(&cx, border_s, border_b);
        }
        cholesky_solve_in_place(&mut s, &mut b)?;
        for (col, &lc) in red.layout_col.iter().enumerate() {
            delta[lc] = b[col];
        }
    }
    back_substitute(&cx, &mut delta);
    Ok(delta)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bundle::problem::{compose, exp_rotation, CameraBlock, Obs, Parameters};
    use crate::bundle::residuals::{evaluate_blocks, gradient_and_diagonal, triplets, Loss};
    use crate::camera::{project, Model};

    #[test]
    fn sym3_inverse_inverts_and_refuses_a_singular_block() {
        let v = [4.0, 1.0, 0.5, 1.0, 3.0, -0.25, 0.5, -0.25, 2.0];
        let inv = sym3_inverse(&v).unwrap();
        for i in 0..3 {
            for j in 0..3 {
                let got: f64 = (0..3).map(|k| v[3 * i + k] * inv[3 * k + j]).sum();
                assert!((got - if i == j { 1.0 } else { 0.0 }).abs() < 1e-14, "({i}, {j}) = {got}");
            }
        }
        assert_eq!(sym3_inverse(&[0.0; 9]), None);
        assert_eq!(sym3_inverse(&[1.0, 1.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0]), None);
        assert_eq!(sym3_inverse(&[f64::NAN, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]), None);
    }

    #[test]
    fn cholesky_solves_and_names_a_pivot_that_is_not_positive() {
        // A = [[4, 2, 0], [2, 5, 1], [0, 1, 3]] (lower triangle), b = A [1, -2, 3]
        let mut a = vec![4.0, 2.0, 0.0, 0.0, 5.0, 1.0, 0.0, 0.0, 3.0];
        let mut b = vec![4.0 - 4.0, 2.0 - 10.0 + 3.0, -2.0 + 9.0];
        cholesky_solve_in_place(&mut a, &mut b).unwrap();
        for (got, want) in b.iter().zip([1.0, -2.0, 3.0]) {
            assert!((got - want).abs() < 1e-14, "{b:?}");
        }
        let mut a = vec![1.0, 2.0, 2.0, 1.0];
        let mut b = vec![1.0, 1.0];
        let error = cholesky_solve_in_place(&mut a, &mut b).unwrap_err();
        assert_eq!(error, "pivot 1 of the reduced system is not positive");
        assert_eq!(cholesky_solve_in_place(&mut [], &mut []), Ok(()));
    }

    fn camera(model: Model, params: &[f64], free: &[usize]) -> CameraBlock {
        CameraBlock { model, params: params.to_vec(), width: 1200.0, height: 900.0, free: free.to_vec() }
    }

    /// Three photos looking down from 10 m (the second of another camera and fixed), nine points
    /// on the ground, two markers with a control point and a bar, GNSS positions of two flights,
    /// the rolling shutter: every residual of the problem, with exact observations.
    fn small_block() -> Problem {
        let r = [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]];
        let centres = vec![[0.0, 0.0, 10.0], [1.0, 0.5, 10.0], [2.0, 0.0, 10.0]];
        let cameras = vec![
            camera(Model::OpenCv, &[1000.0, 1000.0, 600.0, 450.0, -0.02, 0.01, 0.0, 0.0], &[0, 1, 4, 5]),
            camera(Model::SimpleRadial, &[900.0, 600.0, 450.0, -0.01], &[0, 3]),
        ];
        let image_camera = vec![0, 1, 0];
        let mut points = Vec::new();
        for a in 0..3 {
            for b in 0..3 {
                points.push([a as f64 - 0.5, b as f64 - 1.0, 0.1 * (a + b) as f64]);
            }
        }
        let markers = vec![[0.5, -1.5, 0.0], [1.5, 1.5, 0.2]];
        let observe = |world: &[[f64; 3]]| {
            let mut out = Vec::new();
            for (i, c) in centres.iter().enumerate() {
                let cam = &cameras[image_camera[i]];
                for (p, x) in world.iter().enumerate() {
                    let rel = [x[0] - c[0], x[1] - c[1], x[2] - c[2]];
                    let z = -rel[2];
                    let (u, v) = project(cam.model, &cam.params, rel[0] / z, -rel[1] / z);
                    if u > 0.0 && u < cam.width && v > 0.0 && v < cam.height {
                        out.push(Obs { image: i, point: p, xy: [u, v] });
                    }
                }
            }
            out
        };
        let obs = observe(&points);
        let mobs = observe(&markers);
        assert!(obs.len() > 20 && mobs.len() >= 4);
        Problem {
            rotations: vec![r; 3],
            centres: centres.clone(),
            image_camera,
            cameras,
            points,
            obs,
            markers: markers.clone(),
            mobs,
            gcps: vec![(0, markers[0], [0.01; 3])],
            bars: vec![(0, 1, 3.5, 0.002)],
            gnss: vec![(0, centres[0], [0.02; 3], 7), (1, centres[1], [0.02; 3], 2), (2, centres[2], [0.02; 3], 7)],
            fixed_images: vec![false, true, false],
            rolling_shutter: true,
            gnss_bias: true,
            ..Problem::default()
        }
    }

    /// The parameters of the block moved away from the truth, with velocities and biases.
    fn moved(pb: &Problem) -> Parameters {
        let mut params = Parameters::initial(pb);
        for (k, c) in params.centres.iter_mut().enumerate() {
            c[0] += 0.05 * (k as f64 + 1.0);
            c[2] -= 0.1;
        }
        params.rotations[0] = compose(&exp_rotation([0.01, -0.02, 0.005]), &params.rotations[0]);
        for (k, p) in params.points.iter_mut().enumerate() {
            p[1] += 0.03 * (k % 3) as f64 - 0.02;
        }
        params.markers[1][2] += 0.04;
        params.camera_params[0][0] *= 1.01;
        params.camera_params[1][3] = 0.0;
        params.rolling_shutter = (0..3).map(|i| [0.1 * i as f64, -0.05, 0.02, 0.001, -0.002, 0.0005]).collect();
        params.bias = vec![[0.1, -0.2, 0.3], [-0.05, 0.05, 0.1]];
        params
    }

    fn reduced_step_equals_the_full_step(loss: Loss, lambda: f64) {
        let pb = small_block();
        pb.check().unwrap();
        let lay = Layout::new(&pb);
        let st = Structure::new(&pb);
        let red = Reduced::new(&pb, &lay);
        assert_eq!(red.photo_len, vec![12, 6, 12]);
        assert_eq!((red.marker_start, red.border_start, red.bias_start, red.size), (30, 36, 42, 48));
        let params = moved(&pb);
        let ev = evaluate_blocks(&pb, &lay, &st, &params, loss, true);
        assert!(ev.weights.iter().any(|w| *w < 1.0) || loss == Loss::None);
        let (g, d) = gradient_and_diagonal(&pb, &lay, &st, &ev);
        let damping: Vec<f64> = d.iter().map(|x| lambda * x.max(1e-6)).collect();
        let delta = dense_step(&pb, &lay, &st, &red, &ev, &g, &damping).unwrap();

        // the full normal equations from the entries of the Jacobian and the weights of the rows
        let n = lay.size;
        let mut rows: Vec<Vec<(usize, f64)>> = vec![Vec::new(); st.n_rows];
        for (row, col, value) in triplets(&pb, &lay, &st, &ev) {
            rows[row].push((col, value));
        }
        let mut h = vec![0.0; n * n];
        for (row, entries) in rows.iter().enumerate() {
            let w = if row < st.mobs_row { ev.weights[row] } else { 1.0 };
            for &(c1, v1) in entries {
                for &(c2, v2) in entries {
                    h[c2 * n + c1] += w * v1 * v2;
                }
            }
        }
        for k in 0..n {
            h[k * n + k] += damping[k];
        }
        let mut full: Vec<f64> = g.iter().map(|x| -x).collect();
        cholesky_solve_in_place(&mut h, &mut full).unwrap();
        let scale = full.iter().fold(0.0, |a: f64, x| a.max(x.abs()));
        assert!(scale > 1e-3);
        for (k, (got, want)) in delta.iter().zip(&full).enumerate() {
            assert!((got - want).abs() <= 1e-9 * scale, "column {k}: {got} vs {want} ({loss:?}, lambda {lambda})");
        }
    }

    #[test]
    fn the_reduced_step_is_the_step_of_the_full_normal_equations() {
        reduced_step_equals_the_full_step(Loss::None, 1e-4);
        reduced_step_equals_the_full_step(Loss::Huber(0.5), 1e-2);
        reduced_step_equals_the_full_step(Loss::Cauchy(0.3), 1.0);
    }

    #[test]
    fn a_point_without_usable_observations_is_held() {
        let mut pb = small_block();
        // the first point far above the cameras: behind every photo that sees it
        pb.points[0][2] = 50.0;
        let lay = Layout::new(&pb);
        let st = Structure::new(&pb);
        let red = Reduced::new(&pb, &lay);
        let params = moved(&pb);
        let ev = evaluate_blocks(&pb, &lay, &st, &params, Loss::None, true);
        let rows: Vec<f64> = st.point_obs.of(0).iter().map(|&j| ev.residuals[2 * j]).collect();
        assert!(!rows.is_empty() && rows.iter().all(|r| *r == 1e3));
        let (g, d) = gradient_and_diagonal(&pb, &lay, &st, &ev);
        let damping: Vec<f64> = d.iter().map(|x| 1e-4 * x.max(1e-6)).collect();
        let delta = dense_step(&pb, &lay, &st, &red, &ev, &g, &damping).unwrap();
        assert_eq!(&delta[lay.point_col..lay.point_col + 3], &[0.0; 3]);
        assert!(delta.iter().all(|x| x.is_finite()));
        assert!(delta[lay.point_col + 3..lay.point_col + 6].iter().any(|x| *x != 0.0));
    }
}
