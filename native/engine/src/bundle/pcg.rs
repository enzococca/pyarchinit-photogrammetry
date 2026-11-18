//! Conjugate gradients on the reduced camera system, which is never formed (Agarwal, Snavely,
//! Seitz and Szeliski 2010, "Bundle adjustment in the large"): a product `S x` is
//! `B x - E C^-1 E^T x`, computed from the Jacobian blocks of the observations in parallel passes
//! over the rows, the points, the photos and the markers, every sum folded in a fixed order. The
//! preconditioner is the block-Jacobi of `S`: the diagonal blocks of the photos and of the
//! markers and the border block, assembled with the assemblers of `schur` and factorised dense.
//! The iteration stops when the decrease of the quadratic model stagnates (the Q-criterion of
//! Nash and Sofer, with Ceres's forcing of a tenth), on an exact solution, or after
//! `PCG_MAX_ITERATIONS`.

use rayon::prelude::*;

use super::problem::{Layout, Problem};
use super::residuals::{Evaluated, Structure, OFF_CENTRE, OFF_INTR, OFF_POINT};
use super::schur::{
    back_substitute, border_columns, marker_column, photo_column, point_blocks, Block, Context, Reduced,
};

/// The most conjugate-gradient iterations of one step (Ceres's default).
pub(crate) const PCG_MAX_ITERATIONS: usize = 500;
/// The forcing: the iteration stops when `k (Q_k - Q_(k-1)) / Q_k` falls below it, `Q_k` the
/// quadratic model at the k-th iterate (the criterion of Ceres's `eta`). A hundredth of Ceres's
/// default of a tenth: the adjustment runs to a function tolerance of 1e-9, a thousand times
/// Ceres's, and measured on the blocks of the tests the forcing of a tenth stops the iteration on
/// the plateau where the block-Jacobi has settled the photos and the global modes are still to
/// come, so that the adjustment creeps (100 iterations on the control-point block, where the
/// dense solver takes 16); at a thousandth it takes 20 there and, on the noisy grid block, the 6
/// of the dense solver, reaching the same cost to twelve digits.
pub(crate) const PCG_FORCING: f64 = 1e-3;

/// Cholesky factor of a symmetric positive-definite `n x n` matrix, column-major, in place (the
/// lower triangle); `false` when a pivot is not positive.
fn cholesky(a: &mut [f64], n: usize) -> bool {
    for j in 0..n {
        let mut d = a[j * n + j];
        for k in 0..j {
            d -= a[k * n + j] * a[k * n + j];
        }
        if !(d > 0.0) {
            return false;
        }
        let l = d.sqrt();
        a[j * n + j] = l;
        for i in j + 1..n {
            let mut s = a[j * n + i];
            for k in 0..j {
                s -= a[k * n + i] * a[k * n + j];
            }
            a[j * n + i] = s / l;
        }
    }
    true
}

/// Solves `L L^T x = b` in place, `l` the factor of `cholesky`.
fn cholesky_solve(l: &[f64], n: usize, x: &mut [f64]) {
    for i in 0..n {
        let mut s = x[i];
        for k in 0..i {
            s -= l[k * n + i] * x[k];
        }
        x[i] = s / l[i * n + i];
    }
    for i in (0..n).rev() {
        let mut s = x[i];
        for k in i + 1..n {
            s -= l[i * n + k] * x[k];
        }
        x[i] = s / l[i * n + i];
    }
}

/// A diagonal block of the preconditioner: its rows and its Cholesky factor (lower,
/// column-major); when the block is not positive definite beyond round-off, the factor of its
/// diagonal alone.
struct DiagonalBlock {
    start: usize,
    len: usize,
    factor: Vec<f64>,
}

impl DiagonalBlock {
    fn new(start: usize, len: usize, mut block: Vec<f64>) -> DiagonalBlock {
        let diagonal: Vec<f64> = (0..len).map(|k| block[k * len + k]).collect();
        if !cholesky(&mut block, len) {
            block.fill(0.0);
            for (k, d) in diagonal.into_iter().enumerate() {
                block[k * len + k] = if d > 0.0 { d.sqrt() } else { 1.0 };
            }
        }
        DiagonalBlock { start, len, factor: block }
    }

    /// `z = M^-1 r` on the rows of the block.
    fn apply(&self, r: &[f64], z: &mut [f64]) {
        let rows = self.start..self.start + self.len;
        z[rows.clone()].copy_from_slice(&r[rows.clone()]);
        cholesky_solve(&self.factor, self.len, &mut z[rows]);
    }
}

/// The diagonal blocks of the reduced system (the photos, the markers, the border), factorised,
/// and its right-hand side.
fn preconditioner(cx: &Context) -> (Vec<DiagonalBlock>, Vec<f64>) {
    let (pb, red) = (cx.pb, cx.red);
    let n = red.size;
    let assemble = |start: usize, len: usize, fill: &dyn Fn(&mut [f64], &mut [f64])| {
        let (mut block, mut rhs) = (vec![0.0; len * len], vec![0.0; len]);
        fill(&mut block, &mut rhs);
        (DiagonalBlock::new(start, len, block), rhs)
    };
    let photos: Vec<(DiagonalBlock, Vec<f64>)> = (0..pb.centres.len())
        .into_par_iter()
        .map(|i| {
            let (start, len) = (red.photo_start[i], red.photo_len[i]);
            let slab = Block { start, len };
            assemble(start, len, &|block, rhs| photo_column(cx, i, &slab, block, rhs))
        })
        .collect();
    let markers: Vec<(DiagonalBlock, Vec<f64>)> = (0..pb.markers.len())
        .into_par_iter()
        .map(|m| {
            let start = red.marker_start + 3 * m;
            let slab = Block { start, len: 3 };
            assemble(start, 3, &|block, rhs| marker_column(cx, m, &slab, block, rhs))
        })
        .collect();
    let (start, len) = (red.border_start, n - red.border_start);
    let slab = Block { start, len };
    let border = assemble(start, len, &|block, rhs| border_columns(cx, &slab, block, rhs));
    let mut blocks = Vec::with_capacity(photos.len() + markers.len() + 1);
    let mut b = vec![0.0; n];
    for (block, rhs) in photos.into_iter().chain(markers).chain(std::iter::once(border)) {
        if block.len > 0 {
            b[block.start..block.start + block.len].copy_from_slice(&rhs);
            blocks.push(block);
        }
    }
    (blocks, b)
}

/// What a product keeps between two: the camera part of every tie row (`u`) and of every marker
/// row (`um`) at the vector, the Schur term of every point (`y = V^-1 W^T x`), the sums of the
/// photos (their block and the intrinsics of their camera) and of the markers.
struct Scratch {
    u: Vec<f64>,
    um: Vec<f64>,
    y: Vec<[f64; 3]>,
    photos: Vec<([f64; 12], [f64; 12])>,
    markers: Vec<[f64; 3]>,
}

impl Scratch {
    fn new(pb: &Problem) -> Scratch {
        Scratch {
            u: vec![0.0; 2 * pb.obs.len()],
            um: vec![0.0; 2 * pb.mobs.len()],
            y: vec![[0.0; 3]; pb.points.len()],
            photos: vec![([0.0; 12], [0.0; 12]); pb.centres.len()],
            markers: vec![[0.0; 3]; pb.markers.len()],
        }
    }
}

/// `J_cam . x` of the Jacobian row `jr` of the photo `i`: its block of the reduced system and
/// the free intrinsics of its camera.
#[inline]
fn camera_part(cx: &Context, i: usize, jr: &[f64], x: &[f64]) -> f64 {
    let mut jc = [0.0; 12];
    let c = cx.camera_entries(i, jr, &mut jc);
    let s = cx.red.photo_start[i];
    let mut t = 0.0;
    for a in 0..c {
        t += jc[a] * x[s + a];
    }
    let (k_c, intr) = cx.intrinsics(i);
    for q in 0..k_c {
        t += jr[OFF_INTR + q] * x[intr + q];
    }
    t
}

/// `out = (S + damping) x`, `S` the reduced system, without forming it: the rows at `x`, the
/// Schur term of each point, then the sums of the photos, of the markers and of the border.
fn product(cx: &Context, scratch: &mut Scratch, x: &[f64], out: &mut [f64]) {
    let (pb, lay, st, ev, red) = (cx.pb, cx.lay, cx.st, cx.ev, cx.red);
    let Scratch { u, um, y, photos, markers } = scratch;
    u.par_chunks_mut(2).enumerate().for_each(|(j, pair)| {
        let i = pb.obs[j].image;
        for (k, slot) in pair.iter_mut().enumerate() {
            *slot = camera_part(cx, i, ev.obs_row(st, 2 * j + k), x);
        }
    });
    um.par_chunks_mut(2).enumerate().for_each(|(j, pair)| {
        let o = &pb.mobs[j];
        let m0 = red.marker_start + 3 * o.point;
        for (k, slot) in pair.iter_mut().enumerate() {
            let jr = ev.mobs_row(st, 2 * j + k);
            let marker = jr[OFF_POINT] * x[m0] + jr[OFF_POINT + 1] * x[m0 + 1] + jr[OFF_POINT + 2] * x[m0 + 2];
            *slot = camera_part(cx, o.image, jr, x) + marker;
        }
    });
    y.par_iter_mut().enumerate().for_each(|(p, yp)| {
        let pt = &cx.points[p];
        *yp = [0.0; 3];
        if pt.held {
            return;
        }
        let mut v = [0.0; 3];
        for &j in st.point_obs.of(p) {
            for row in 2 * j..2 * j + 2 {
                let (jr, wu) = (ev.obs_row(st, row), ev.weights[row] * u[row]);
                for k in 0..3 {
                    v[k] += jr[OFF_POINT + k] * wu;
                }
            }
        }
        for k in 0..3 {
            yp[k] = pt.vinv[3 * k] * v[0] + pt.vinv[3 * k + 1] * v[1] + pt.vinv[3 * k + 2] * v[2];
        }
    });
    photos.par_iter_mut().enumerate().for_each(|(i, (own, intr))| {
        *own = [0.0; 12];
        *intr = [0.0; 12];
        let (s_i, c_i) = (red.photo_start[i], red.photo_len[i]);
        let (k_c, _) = cx.intrinsics(i);
        for a in 0..c_i {
            own[a] = cx.damping[red.layout_col[s_i + a]] * x[s_i + a];
        }
        let mut jc = [0.0; 12];
        for &j in st.image_obs.of(i) {
            let yp = &y[pb.obs[j].point];
            for row in 2 * j..2 * j + 2 {
                let jr = ev.obs_row(st, row);
                let point = jr[OFF_POINT] * yp[0] + jr[OFF_POINT + 1] * yp[1] + jr[OFF_POINT + 2] * yp[2];
                let t = ev.weights[row] * (u[row] - point);
                cx.camera_entries(i, jr, &mut jc);
                for a in 0..c_i {
                    own[a] += jc[a] * t;
                }
                for q in 0..k_c {
                    intr[q] += jr[OFF_INTR + q] * t;
                }
            }
        }
        for &j in st.image_mobs.of(i) {
            for row in 2 * j..2 * j + 2 {
                let (jr, t) = (ev.mobs_row(st, row), um[row]);
                cx.camera_entries(i, jr, &mut jc);
                for a in 0..c_i {
                    own[a] += jc[a] * t;
                }
                for q in 0..k_c {
                    intr[q] += jr[OFF_INTR + q] * t;
                }
            }
        }
        if lay.image_col[i].is_some() {
            for &j in st.image_gnss.of(i) {
                let bias = lay.flight_index(pb.gnss[j].3).map(|f| red.bias_start + 3 * f);
                for k in 0..3 {
                    let jc = ev.gnss_jac[j][k];
                    let mut t = jc * x[s_i + OFF_CENTRE + k];
                    if let Some(b0) = bias {
                        t += ev.gnss_jac[j][3 + k] * x[b0 + k];
                    }
                    own[OFF_CENTRE + k] += jc * t;
                }
            }
        }
        if pb.rolling_shutter {
            let off = c_i - 6;
            for k in 0..6 {
                let jr = ev.rs_jac[i][k];
                own[off + k] += jr * jr * x[s_i + off + k];
            }
        }
    });
    markers.par_iter_mut().enumerate().for_each(|(m, out)| {
        let s_m = red.marker_start + 3 * m;
        for k in 0..3 {
            out[k] = cx.damping[lay.marker_col + 3 * m + k] * x[s_m + k];
        }
        for &j in st.marker_mobs.of(m) {
            for row in 2 * j..2 * j + 2 {
                let (jr, t) = (ev.mobs_row(st, row), um[row]);
                for k in 0..3 {
                    out[k] += jr[OFF_POINT + k] * t;
                }
            }
        }
        for &j in st.marker_gcps.of(m) {
            for k in 0..3 {
                let jv = ev.gcp_jac[j][k];
                out[k] += jv * jv * x[s_m + k];
            }
        }
        for &end in st.marker_bars.of(m) {
            let (j, mine) = (end / 2, 3 * (end % 2));
            let other = if end % 2 == 0 { pb.bars[j].1 } else { pb.bars[j].0 };
            let s_o = red.marker_start + 3 * other;
            let jm = &ev.bar_jac[j][mine..mine + 3];
            let jo = &ev.bar_jac[j][3 - mine..6 - mine];
            let mut t = 0.0;
            for k in 0..3 {
                t += jm[k] * x[s_m + k] + jo[k] * x[s_o + k];
            }
            for k in 0..3 {
                out[k] += jm[k] * t;
            }
        }
    });
    // the vector: the photos, the markers, then the border folded in the order of the photos
    for (i, (own, _)) in photos.iter().enumerate() {
        let (s_i, c_i) = (red.photo_start[i], red.photo_len[i]);
        out[s_i..s_i + c_i].copy_from_slice(&own[..c_i]);
    }
    for (m, sums) in markers.iter().enumerate() {
        let s_m = red.marker_start + 3 * m;
        out[s_m..s_m + 3].copy_from_slice(sums);
    }
    for col in red.border_start..red.size {
        out[col] = cx.damping[red.layout_col[col]] * x[col];
    }
    for (i, (_, intr)) in photos.iter().enumerate() {
        let (k_c, intr0) = cx.intrinsics(i);
        for q in 0..k_c {
            out[intr0 + q] += intr[q];
        }
    }
    for (j, (i, _, _, flight)) in pb.gnss.iter().enumerate() {
        if let Some(b0) = lay.flight_index(*flight).map(|f| red.bias_start + 3 * f) {
            for k in 0..3 {
                let jb = ev.gnss_jac[j][3 + k];
                let mut t = jb * x[b0 + k];
                if lay.image_col[*i].is_some() {
                    t += ev.gnss_jac[j][k] * x[red.photo_start[*i] + OFF_CENTRE + k];
                }
                out[b0 + k] += jb * t;
            }
        }
    }
}

/// The preconditioned conjugate gradients on `apply` (the damped reduced system) from zero,
/// until the Q-criterion (`forcing`), an exact solution or `max_iterations`: the iterate
/// reached, or an error when the system is not positive definite.
fn conjugate_gradients(
    mut apply: impl FnMut(&[f64], &mut [f64]),
    blocks: &[DiagonalBlock],
    b: &[f64],
    forcing: f64,
    max_iterations: usize,
) -> Result<Vec<f64>, String> {
    let n = b.len();
    let dot = |a: &[f64], c: &[f64]| a.iter().zip(c).map(|(x, y)| x * y).sum::<f64>();
    let precondition = |r: &[f64], z: &mut [f64]| {
        for block in blocks {
            block.apply(r, z);
        }
    };
    let mut x = vec![0.0; n];
    if dot(b, b) == 0.0 {
        return Ok(x);
    }
    let mut r = b.to_vec();
    let mut z = vec![0.0; n];
    precondition(&r, &mut z);
    let mut p = z.clone();
    let mut q = vec![0.0; n];
    let mut rz = dot(&r, &z);
    // the quadratic model 0.5 x S x - b x, which is -0.5 x (b + r): nothing at x = 0
    let mut model = 0.0;
    for k in 1..=max_iterations {
        apply(&p, &mut q);
        let pq = dot(&p, &q);
        if !(pq > 0.0) {
            return Err("the reduced system is not positive definite".into());
        }
        let alpha = rz / pq;
        for i in 0..n {
            x[i] += alpha * p[i];
            r[i] -= alpha * q[i];
        }
        let next = -0.5 * x.iter().zip(b.iter().zip(&r)).map(|(xi, (bi, ri))| xi * (bi + ri)).sum::<f64>();
        let zeta = k as f64 * (next - model) / next;
        model = next;
        if zeta < forcing || dot(&r, &r) == 0.0 {
            break;
        }
        precondition(&r, &mut z);
        let rz_next = dot(&r, &z);
        if !(rz_next > 0.0) {
            break;
        }
        let beta = rz_next / rz;
        for i in 0..n {
            p[i] = z[i] + beta * p[i];
        }
        rz = rz_next;
    }
    Ok(x)
}

/// One step of the damped normal equations, as `schur::dense_step`, with the reduced system
/// solved by the conjugate gradients stopped by `forcing` or `max_iterations`.
fn pcg_step_with(
    pb: &Problem,
    lay: &Layout,
    st: &Structure,
    red: &Reduced,
    ev: &Evaluated,
    g: &[f64],
    damping: &[f64],
    forcing: f64,
    max_iterations: usize,
) -> Result<Vec<f64>, String> {
    let points = point_blocks(pb, lay, st, ev, g, damping);
    let cx = Context { pb, lay, st, red, ev, g, damping, points: &points };
    let mut delta = vec![0.0; lay.size];
    if red.size > 0 {
        let (blocks, b) = preconditioner(&cx);
        let mut scratch = Scratch::new(pb);
        let apply = |p: &[f64], q: &mut [f64]| product(&cx, &mut scratch, p, q);
        let x = conjugate_gradients(apply, &blocks, &b, forcing, max_iterations)?;
        for (col, &lc) in red.layout_col.iter().enumerate() {
            delta[lc] = x[col];
        }
    }
    back_substitute(&cx, &mut delta);
    Ok(delta)
}

/// One step of the damped normal equations, as `schur::dense_step`: the reduced system solved
/// by the preconditioned conjugate gradients without being formed, the points back-substituted.
pub(crate) fn pcg_step(
    pb: &Problem,
    lay: &Layout,
    st: &Structure,
    red: &Reduced,
    ev: &Evaluated,
    g: &[f64],
    damping: &[f64],
) -> Result<Vec<f64>, String> {
    pcg_step_with(pb, lay, st, red, ev, g, damping, PCG_FORCING, PCG_MAX_ITERATIONS)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bundle::residuals::{evaluate_blocks, gradient_and_diagonal, Loss};
    use crate::bundle::schur::tests::{moved, small_block};
    use crate::bundle::schur::{dense_step, dense_system};

    #[test]
    fn the_small_cholesky_solves_and_refuses_what_is_not_positive_definite() {
        // A = [[4, 2, 0], [2, 5, 1], [0, 1, 3]], column-major, b = A [1, -2, 3]
        let mut a = vec![4.0, 2.0, 0.0, 2.0, 5.0, 1.0, 0.0, 1.0, 3.0];
        assert!(cholesky(&mut a, 3));
        let mut x = vec![0.0, -5.0, 7.0];
        cholesky_solve(&a, 3, &mut x);
        for (got, want) in x.iter().zip([1.0, -2.0, 3.0]) {
            assert!((got - want).abs() < 1e-14, "{x:?}");
        }
        assert!(!cholesky(&mut [1.0, 2.0, 2.0, 1.0], 2));
        let block = DiagonalBlock::new(5, 2, vec![1.0, 2.0, 2.0, 4.0]);
        // not positive definite: the diagonal alone, 1 and 4
        let (r, mut z) = (vec![0.0, 0.0, 0.0, 0.0, 0.0, 3.0, 8.0], vec![0.0; 7]);
        block.apply(&r, &mut z);
        assert_eq!(z, vec![0.0, 0.0, 0.0, 0.0, 0.0, 3.0, 2.0]);
    }

    /// The reduced system of the small block at its moved parameters, with every residual.
    fn system(loss: Loss, lambda: f64) -> (Problem, Layout, Structure, Reduced, Evaluated, Vec<f64>, Vec<f64>) {
        let pb = small_block();
        let lay = Layout::new(&pb);
        let st = Structure::new(&pb);
        let red = Reduced::new(&pb, &lay);
        let ev = evaluate_blocks(&pb, &lay, &st, &moved(&pb), loss, true);
        let (g, d) = gradient_and_diagonal(&pb, &lay, &st, &ev);
        let damping: Vec<f64> = d.iter().map(|x| lambda * x.max(1e-6)).collect();
        (pb, lay, st, red, ev, g, damping)
    }

    #[test]
    fn the_product_and_the_preconditioner_are_those_of_the_dense_system() {
        for (loss, lambda) in [(Loss::None, 1e-4), (Loss::Huber(0.5), 1e-2), (Loss::Cauchy(0.3), 1.0)] {
            let (pb, lay, st, red, ev, g, damping) = system(loss, lambda);
            let points = point_blocks(&pb, &lay, &st, &ev, &g, &damping);
            let (lay, st, red, ev) = (&lay, &st, &red, &ev);
            let cx = Context { pb: &pb, lay, st, red, ev, g: &g, damping: &damping, points: &points };
            let n = red.size;
            assert_eq!(n, 48);
            let (s, b) = dense_system(&cx);
            // the dense assembly writes the lower triangle and the whole diagonal blocks
            let sym = |i: usize, j: usize| s[i.min(j) * n + i.max(j)];
            let (blocks, b_pcg) = preconditioner(&cx);
            assert_eq!(b_pcg, b, "{loss:?}: the right-hand side");
            assert_eq!(blocks.len(), 3 + 2 + 1);
            for block in &blocks {
                // the factor is the lower triangle of the buffer (the upper one is left as it was)
                let (s0, len, l) = (block.start, block.len, &block.factor);
                for i in 0..len {
                    for j in 0..len {
                        let llt: f64 = (0..=i.min(j)).map(|k| l[k * len + i] * l[k * len + j]).sum();
                        let want = sym(s0 + i, s0 + j);
                        let close = (llt - want).abs() <= 1e-12 * want.abs().max(1.0);
                        assert!(close, "{loss:?}: block {s0} ({i}, {j}): {llt} vs {want}");
                    }
                }
            }
            let mut scratch = Scratch::new(&pb);
            let mut out = vec![0.0; n];
            for seed in 0..3 {
                let x: Vec<f64> = (0..n).map(|k| ((k * 7 + seed * 13) % 11) as f64 - 5.0).collect();
                product(&cx, &mut scratch, &x, &mut out);
                for i in 0..n {
                    let want: f64 = (0..n).map(|j| sym(i, j) * x[j]).sum();
                    let scale = (0..n).map(|j| (sym(i, j) * x[j]).abs()).sum::<f64>().max(1.0);
                    assert!((out[i] - want).abs() <= 1e-12 * scale, "{loss:?}: row {i}: {} vs {want}", out[i]);
                }
            }
        }
    }

    #[test]
    fn the_conjugate_gradients_run_to_the_end_give_the_dense_step() {
        for (loss, lambda) in [(Loss::None, 1e-4), (Loss::Huber(0.5), 1e-2), (Loss::Cauchy(0.3), 1.0)] {
            let (pb, lay, st, red, ev, g, damping) = system(loss, lambda);
            let dense = dense_step(&pb, &lay, &st, &red, &ev, &g, &damping).unwrap();
            let scale = dense.iter().fold(0.0, |a: f64, x| a.max(x.abs()));
            // no forcing: the iteration runs until the residual is exactly zero or the limit; the
            // block mixes focal lengths, metres and radians, and 1e-6 of the step is what the
            // conjugate gradients reach in double precision on such a system (measured 1e-7)
            let pcg = pcg_step_with(&pb, &lay, &st, &red, &ev, &g, &damping, 0.0, 2000).unwrap();
            for (k, (got, want)) in pcg.iter().zip(&dense).enumerate() {
                assert!((got - want).abs() <= 1e-6 * scale, "column {k}: {got} vs {want} ({loss:?})");
            }
            // with the forcing of the solver the step is rough, but it is a descent direction
            let rough = pcg_step(&pb, &lay, &st, &red, &ev, &g, &damping).unwrap();
            let descent: f64 = rough.iter().zip(&g).map(|(d, gk)| d * gk).sum();
            assert!(descent < 0.0, "{loss:?}: g . delta = {descent}");
            assert!(rough.iter().all(|x| x.is_finite()));
        }
        // an indefinite system (a negative damping) is refused
        let (pb, lay, st, red, ev, g, damping) = system(Loss::None, 1e-4);
        let negative: Vec<f64> = damping.iter().map(|x| -1e6 * x).collect();
        let error = pcg_step(&pb, &lay, &st, &red, &ev, &g, &negative).unwrap_err();
        assert_eq!(error, "the reduced system is not positive definite");
    }
}
