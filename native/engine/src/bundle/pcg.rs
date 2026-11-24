//! Conjugate gradients on the reduced camera system, which is never formed (Agarwal, Snavely,
//! Seitz and Szeliski 2010, "Bundle adjustment in the large"): a product `S x` is
//! `B x - E C^-1 E^T x`, computed from the Jacobian blocks of the observations in parallel passes
//! over the rows (in fixed chunks of residual blocks, each summed into its own vector) and the
//! point blocks, every sum folded in a fixed order. The preconditioner is the block-Jacobi of `S`:
//! the diagonal blocks of the camera blocks (the photos and the markers of the bundle adjustment)
//! and the border block, assembled with the assemblers of `schur` and factorised dense.
//! The iteration stops when the decrease of the quadratic model stagnates (the Q-criterion of
//! Nash and Sofer as Ceres applies it, with the forcing `PCG_FORCING`), on an exact solution, or
//! after `PCG_MAX_ITERATIONS`.

use rayon::prelude::*;

use super::lm::Shape;
use super::schur::{
    back_substitute, border_columns, camera_column, point_inverses, split_sizes, Block, Context, Reduced,
};

/// The chunks of residual blocks a product sums apart (a vector of the reduced system each),
/// whatever the number of threads, folded in their order.
const PRODUCT_CHUNKS: usize = 64;
/// Columns per task of the fold.
const FOLD_CHUNK: usize = 256;

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
/// column-major); when a pivot of the block is not positive, the factor of its diagonal alone.
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

/// The diagonal blocks of the reduced system (the camera blocks before the border, the border),
/// factorised, and its right-hand side.
fn preconditioner(cx: &Context) -> (Vec<DiagonalBlock>, Vec<f64>) {
    let sh = cx.shape;
    let n = sh.n;
    let assemble = |start: usize, len: usize, fill: &dyn Fn(&mut [f64], &mut [f64])| {
        let (mut block, mut rhs) = (vec![0.0; len * len], vec![0.0; len]);
        fill(&mut block, &mut rhs);
        (DiagonalBlock::new(start, len, block), rhs)
    };
    let cameras: Vec<(DiagonalBlock, Vec<f64>)> = (0..sh.border_block)
        .into_par_iter()
        .map(|c| {
            let (start, len) = (sh.cam_col[c], sh.cam_size[c]);
            let slab = Block { start, len };
            assemble(start, len, &|block, rhs| camera_column(cx, c, &slab, block, rhs))
        })
        .collect();
    let (start, len) = (sh.border_start, n - sh.border_start);
    let slab = Block { start, len };
    let border = assemble(start, len, &|block, rhs| border_columns(cx, &slab, block, rhs));
    let mut blocks = Vec::with_capacity(cameras.len() + 1);
    let mut b = vec![0.0; n];
    for (block, rhs) in cameras.into_iter().chain(std::iter::once(border)) {
        if block.len > 0 {
            b[block.start..block.start + block.len].copy_from_slice(&rhs);
            blocks.push(block);
        }
    }
    (blocks, b)
}

/// What a product keeps between two: the camera part of every row at the vector (`u`), the
/// Schur term of every point block (`y = V^-1 W^T x`), the partial sums of the chunks of residual
/// blocks.
struct Scratch {
    u: Vec<f64>,
    y: Vec<f64>,
    partials: Vec<Vec<f64>>,
}

impl Scratch {
    fn new(red: &Reduced) -> Scratch {
        let (rows, n) = (red.lin.residuals.len(), red.shape.n);
        let partials = vec![vec![0.0; n]; PRODUCT_CHUNKS];
        Scratch { u: vec![0.0; rows], y: vec![0.0; red.shape.n_point_cols], partials }
    }
}

/// A buffer of one entry per row split by chunk of residual blocks: the first block of each
/// chunk, its end, its rows.
fn split_rows<'a>(sh: &Shape, buffer: &'a mut [f64]) -> Vec<(usize, usize, &'a mut [f64])> {
    let mut pieces = Vec::with_capacity(sh.chunks.len());
    let mut rest = buffer;
    for w in sh.chunks.windows(2) {
        let (head, tail) = rest.split_at_mut(sh.row_start[w[1]] - sh.row_start[w[0]]);
        pieces.push((w[0], w[1], head));
        rest = tail;
    }
    pieces
}

/// `y_p = V^-1 sum_k J_p^T W u_k` of the point block `p` (of `P` coordinates when it is not 0),
/// zero when it is held; `v` a scratch of its size.
#[inline(always)]
fn schur_term<const P: usize>(cx: &Context, u: &[f64], p: usize, yp: &mut [f64], v: &mut [f64]) {
    let (sh, lin) = (cx.shape, cx.lin);
    yp.fill(0.0);
    let ps = if P > 0 { P } else { yp.len() };
    let Some((vinv, _)) = cx.point(p, ps) else { return };
    let v = &mut v[..ps];
    v.fill(0.0);
    for &k in sh.point_res.of(p) {
        let ppos = sh.width(k) - ps;
        for (row, jr) in sh.jac_rows(lin, k) {
            let wu = lin.weights[row] * u[row];
            let jp = &jr[ppos..ppos + ps];
            for q in 0..ps {
                v[q] += jp[q] * wu;
            }
        }
    }
    for a in 0..ps {
        let mut t = 0.0;
        for b in 0..ps {
            t += vinv[a * ps + b] * v[b];
        }
        yp[a] = t;
    }
}

/// `out = (S + damping) x`, `S` the reduced system, without forming it: the rows at `x`, the
/// Schur term of each point block, then the weighted rows less their point part summed on their
/// camera blocks.
fn product(cx: &Context, scratch: &mut Scratch, x: &[f64], out: &mut [f64]) {
    let (sh, lin) = (cx.shape, cx.lin);
    let Scratch { u, y, partials } = scratch;
    // the camera part of every row, chunk by chunk of residual blocks
    let pieces: Vec<(usize, usize, &mut [f64])> = split_rows(sh, u);
    pieces.into_par_iter().for_each(|(first, end, u)| {
        let row0 = sh.row_start[first];
        u.fill(0.0);
        for k in first..end {
            let cams = sh.cam_slice(k);
            for (row, jr) in sh.jac_rows(lin, k) {
                let mut v = 0.0;
                for &(c, pos) in cams {
                    let (s, pos) = (sh.cam_col[c as usize], pos as usize);
                    for a in 0..sh.cam_size[c as usize] {
                        v += jr[pos + a] * x[s + a];
                    }
                }
                u[row - row0] = v;
            }
        }
    });
    let u = &*u;
    // the Schur term of every point block
    let largest = sh.max_point;
    let slices = split_sizes(y, sh.point_size.iter().copied());
    slices.into_par_iter().enumerate().for_each_init(
        || vec![0.0; largest],
        |v, (p, yp)| {
            if yp.len() == 3 {
                schur_term::<3>(cx, u, p, yp, v);
            } else {
                schur_term::<0>(cx, u, p, yp, v);
            }
        },
    );
    let y = &*y;
    // every row weighted, less its point part, scattered on its camera blocks: in fixed chunks of
    // residual blocks, each into its own vector, walking the Jacobian in its order
    let n = sh.n;
    let per = sh.blocks.len().div_ceil(PRODUCT_CHUNKS).max(1);
    partials.par_iter_mut().enumerate().for_each(|(chunk, partial)| {
        partial.fill(0.0);
        let first = (chunk * per).min(sh.blocks.len());
        let end = ((chunk + 1) * per).min(sh.blocks.len());
        for k in first..end {
            let cams = sh.cam_slice(k);
            let point = sh.point_of(k);
            for (row, jr) in sh.jac_rows(lin, k) {
                let mut v = u[row];
                if let Some((p, ppos, ps)) = point {
                    let c0 = sh.point_col[p];
                    for q in 0..ps {
                        v -= jr[ppos + q] * y[c0 + q];
                    }
                }
                let tr = lin.weights[row] * v;
                for &(c, pos) in cams {
                    let (s, pos, size) = (sh.cam_col[c as usize], pos as usize, sh.cam_size[c as usize]);
                    let (out, jc) = (&mut partial[s..s + size], &jr[pos..pos + size]);
                    for a in 0..size {
                        out[a] += jc[a] * tr;
                    }
                }
            }
        }
    });
    // the damping, then the chunks folded in their order, column by column
    let partials = &*partials;
    out.par_chunks_mut(FOLD_CHUNK).enumerate().for_each(|(i, out)| {
        let start = i * FOLD_CHUNK;
        for (a, o) in out.iter_mut().enumerate() {
            let col = start + a;
            let mut v = cx.damping[col] * x[col];
            for partial in partials {
                v += partial[col];
            }
            *o = v;
        }
    });
    debug_assert_eq!(out.len(), n);
}

/// The preconditioned conjugate gradients on `apply` (the damped reduced system) from zero,
/// until the Q-criterion (`forcing`), an exact solution or `max_iterations`: the iterate
/// reached and whether it converged (`false` when `max_iterations` stopped it, or the
/// preconditioned residual lost its sign), or an error when the system is not positive definite.
fn conjugate_gradients(
    mut apply: impl FnMut(&[f64], &mut [f64]),
    blocks: &[DiagonalBlock],
    b: &[f64],
    forcing: f64,
    max_iterations: usize,
) -> Result<(Vec<f64>, bool), String> {
    let n = b.len();
    let dot = |a: &[f64], c: &[f64]| a.iter().zip(c).map(|(x, y)| x * y).sum::<f64>();
    let precondition = |r: &[f64], z: &mut [f64]| {
        for block in blocks {
            block.apply(r, z);
        }
    };
    let mut x = vec![0.0; n];
    if dot(b, b) == 0.0 {
        return Ok((x, true));
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
            return Ok((x, true));
        }
        precondition(&r, &mut z);
        let rz_next = dot(&r, &z);
        if !(rz_next > 0.0) {
            return Ok((x, rz_next == 0.0));
        }
        let beta = rz_next / rz;
        for i in 0..n {
            p[i] = z[i] + beta * p[i];
        }
        rz = rz_next;
    }
    Ok((x, false))
}

/// One step of the damped normal equations, as `schur::dense_step`, with the reduced system
/// solved by the conjugate gradients stopped by `forcing` or `max_iterations` (a step of the
/// adjustment takes the iterate reached either way).
pub(crate) fn pcg_step_with(
    red: &Reduced,
    g: &[f64],
    damping: &[f64],
    forcing: f64,
    max_iterations: usize,
) -> Result<Vec<f64>, String> {
    pcg_solve(red, g, damping, forcing, max_iterations).map(|(delta, _)| delta)
}

/// `pcg_step_with` that refuses an iterate `max_iterations` stopped before the Q-criterion
/// (`forcing`) was met: the solve of a column of an inverse (`lm::covariance_block`), where a
/// truncated iterate is short exactly in the poorly determined directions.
pub(crate) fn pcg_step_converged(
    red: &Reduced,
    g: &[f64],
    damping: &[f64],
    forcing: f64,
    max_iterations: usize,
) -> Result<Vec<f64>, String> {
    match pcg_solve(red, g, damping, forcing, max_iterations)? {
        (delta, true) => Ok(delta),
        (_, false) => Err(format!("the conjugate gradients did not converge in {max_iterations} iterations")),
    }
}

fn pcg_solve(
    red: &Reduced,
    g: &[f64],
    damping: &[f64],
    forcing: f64,
    max_iterations: usize,
) -> Result<(Vec<f64>, bool), String> {
    let points = point_inverses(red, g, damping);
    let cx = Context { shape: red.shape, lin: red.lin, g, damping, points: &points };
    let n = red.shape.n;
    let mut delta = vec![0.0; n + red.shape.n_point_cols];
    let mut converged = true;
    if n > 0 {
        let (blocks, b) = preconditioner(&cx);
        let mut scratch = Scratch::new(red);
        let apply = |p: &[f64], q: &mut [f64]| product(&cx, &mut scratch, p, q);
        let (x, done) = conjugate_gradients(apply, &blocks, &b, forcing, max_iterations)?;
        delta[..n].copy_from_slice(&x);
        converged = done;
    }
    back_substitute(&cx, &mut delta);
    Ok((delta, converged))
}

/// One step of the damped normal equations, as `schur::dense_step`: the reduced system solved
/// by the preconditioned conjugate gradients without being formed, the points back-substituted.
pub(crate) fn pcg_step(red: &Reduced, g: &[f64], damping: &[f64]) -> Result<Vec<f64>, String> {
    pcg_step_with(red, g, damping, PCG_FORCING, PCG_MAX_ITERATIONS)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bundle::residuals::Loss;
    use crate::bundle::schur::tests::{moved, small_block};
    use crate::bundle::schur::{dense_step, dense_system};
    use crate::bundle::solve::testing::system;

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

    #[test]
    fn the_product_and_the_preconditioner_are_those_of_the_dense_system() {
        for (loss, lambda) in [(Loss::None, 1e-4), (Loss::Huber(0.5), 1e-2), (Loss::Cauchy(0.3), 1.0)] {
            let pb = small_block();
            let sys = system(&pb, moved(&pb), loss);
            let (red, damping) = (sys.reduced(), sys.damping(lambda));
            let points = point_inverses(&red, &sys.g, &damping);
            let cx = Context { shape: &sys.shape, lin: &sys.lin, g: &sys.g, damping: &damping, points: &points };
            let n = sys.shape.n;
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
            let mut scratch = Scratch::new(&red);
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
            let pb = small_block();
            let sys = system(&pb, moved(&pb), loss);
            let (red, g, damping) = (sys.reduced(), &sys.g, sys.damping(lambda));
            let dense = dense_step(&red, g, &damping).unwrap();
            let scale = dense.iter().fold(0.0, |a: f64, x| a.max(x.abs()));
            // no forcing: the iteration runs until the residual is exactly zero or the limit; the
            // block mixes focal lengths, metres and radians, and 1e-6 of the step is what the
            // conjugate gradients reach in double precision on such a system (measured 1e-7)
            let pcg = pcg_step_with(&red, g, &damping, 0.0, 2000).unwrap();
            for (k, (got, want)) in pcg.iter().zip(&dense).enumerate() {
                assert!((got - want).abs() <= 1e-6 * scale, "column {k}: {got} vs {want} ({loss:?})");
            }
            // with the forcing of the solver the step is rough, but it is a descent direction
            let rough = pcg_step(&red, g, &damping).unwrap();
            let descent: f64 = rough.iter().zip(g).map(|(d, gk)| d * gk).sum();
            assert!(descent < 0.0, "{loss:?}: g . delta = {descent}");
            assert!(rough.iter().all(|x| x.is_finite()));
        }
        // an indefinite system (a negative damping) is refused
        let pb = small_block();
        let sys = system(&pb, moved(&pb), Loss::None);
        let negative: Vec<f64> = sys.damping(1e-4).iter().map(|x| -1e6 * x).collect();
        let error = pcg_step(&sys.reduced(), &sys.g, &negative).unwrap_err();
        assert_eq!(error, "the reduced system is not positive definite");
    }
}
