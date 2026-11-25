//! The normal equations reduced to the camera blocks of a model (`lm::Model`): the point blocks
//! eliminated (the Schur complement), what is left ordered as the camera blocks (for the bundle
//! adjustment the blocks of the photos - the pose of a free photo, then its rolling shutter -, the
//! markers and, last, the border of the free intrinsics and the flight biases); the dense Cholesky
//! factorisation of faer on it; the points back-substituted. Forming the reduced system is
//! parallel over its column blocks, each written by one thread in a fixed order, and the sums that
//! end in the border are folded chunk by chunk in a fixed order, so that two runs give the same
//! bits. The assemblers of the column blocks write through a `Slab`, so that the same code fills
//! the dense matrix, the pattern of the sparse one (`sparse`) and the diagonal blocks of the
//! preconditioner of the conjugate gradients (`pcg`).

use faer::dyn_stack::{MemBuffer, MemStack};
use faer::linalg::cholesky::llt::{factor, solve as llt_solve};
use faer::{MatMut, MatRef};
use rayon::prelude::*;

use super::lm::{sym_inverse, well_conditioned_n, Lin, Shape};

/// Point blocks (and residual blocks) per chunk of the sums that end in the border.
const CHUNK: usize = 1024;
/// The sizes up to which the Schur terms of a tie point are summed on the stack: the columns of
/// the camera block, the width of a row, the camera blocks of a residual block.
const SMALL_CAMERA: usize = 16;
const SMALL_WIDTH: usize = 48;
const SMALL_TARGETS: usize = 8;

/// The linearised model a step is solved on: its shape, its linearisation and the point
/// coordinates held on their floor for this step.
pub(crate) struct Reduced<'a> {
    pub shape: &'a Shape,
    pub lin: &'a Lin,
    pub held: &'a [bool],
}

/// The point blocks after the elimination, one record each from `Shape::inv_start` (one place in
/// memory for the passes that read them in the order of the residual blocks): whether it is held
/// (1) or not (0), the inverse of its damped `J^T W J` (its size squared, row-major) and
/// `y = V^-1 g`. A block is held when it is singular beyond round-off (no usable observation, or a
/// single view of a tie point): it keeps its place in this step. Damping alone would not do: along
/// the null direction the step would be round-off divided by the damping.
pub(crate) struct PointInverses {
    pub records: Vec<f64>,
}

/// What every column block reads.
pub(crate) struct Context<'a> {
    pub shape: &'a Shape,
    pub lin: &'a Lin,
    pub g: &'a [f64],
    pub damping: &'a [f64],
    pub points: &'a PointInverses,
}

impl Context<'_> {
    /// The inverse of the point block `p` (of `s` coordinates) and its `y`, `None` when it is held.
    #[inline]
    pub(crate) fn point(&self, p: usize, s: usize) -> Option<(&[f64], &[f64])> {
        let at = self.shape.inv_start[p];
        let record = &self.points.records[at..at + 1 + s * s + s];
        if record[0] != 0.0 {
            return None;
        }
        Some((&record[1..1 + s * s], &record[1 + s * s..]))
    }

    /// `J_c^T W J_p` of the residual block `k` for its camera block at `pos` (`size` columns) and
    /// its point block at `ppos` (`ps` columns, `P` when it is not 0), added into `out`
    /// (`size x ps`, row-major).
    #[inline(always)]
    pub(crate) fn camera_point<const P: usize>(
        &self,
        k: usize,
        pos: usize,
        size: usize,
        ppos: usize,
        ps: usize,
        out: &mut [f64],
    ) {
        let ps = if P > 0 { P } else { ps };
        let (sh, lin) = (self.shape, self.lin);
        for (row, jr) in sh.jac_rows(lin, k) {
            let w = lin.weights[row];
            let jp = &jr[ppos..ppos + ps];
            for a in 0..size {
                let wa = w * jr[pos + a];
                let o = &mut out[a * ps..a * ps + ps];
                for q in 0..ps {
                    o[q] += wa * jp[q];
                }
            }
        }
    }
}

/// Splits `buffer` into consecutive slices of the sizes given.
pub(crate) fn split_sizes<'a>(buffer: &'a mut [f64], sizes: impl Iterator<Item = usize>) -> Vec<&'a mut [f64]> {
    let mut out = Vec::new();
    let mut rest = buffer;
    for s in sizes {
        let (head, tail) = rest.split_at_mut(s);
        out.push(head);
        rest = tail;
    }
    out
}

/// Where the entries of a column block of the reduced system go: `at` is the position of the
/// entry of the column `a` of the block at the reduced row `row`, `keeps` says whether the rows
/// of the block that starts at `start` are stored at all. The dense matrix keeps every row, the
/// pattern of the sparse matrix every row its blocks couple with, a block on its own only its
/// rows: the assemblers skip what a slab does not keep.
pub(crate) trait Slab {
    fn keeps(&self, start: usize) -> bool;
    fn at(&self, a: usize, row: usize) -> usize;
    /// The distance between the entries of a row in two consecutive columns:
    /// `at(a, row) = at(0, row) + a * stride()`.
    fn stride(&self) -> usize;
}

/// The dense reduced matrix, column-major with `n` rows per column; a column block is the slab
/// of its columns.
pub(crate) struct Full(pub usize);

impl Slab for Full {
    #[inline]
    fn keeps(&self, _: usize) -> bool {
        true
    }

    #[inline]
    fn at(&self, a: usize, row: usize) -> usize {
        a * self.0 + row
    }

    #[inline]
    fn stride(&self) -> usize {
        self.0
    }
}

/// The `len` columns of one block with their own rows only, from `start`: the border of the
/// sparse matrix, the diagonal blocks of the preconditioner.
pub(crate) struct Block {
    pub start: usize,
    pub len: usize,
}

impl Slab for Block {
    #[inline]
    fn keeps(&self, start: usize) -> bool {
        (self.start..self.start + self.len).contains(&start)
    }

    #[inline]
    fn at(&self, a: usize, row: usize) -> usize {
        debug_assert!(self.keeps(row), "row {row} outside the block at {}", self.start);
        a * self.len + row - self.start
    }

    #[inline]
    fn stride(&self) -> usize {
        self.len
    }
}

pub(crate) fn det3(m: &[[f64; 3]; 3]) -> f64 {
    m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
}

/// Whether a symmetric 3 x 3 matrix (row-major) is positive definite beyond round-off: its
/// determinant above `1e-9` of the product of its diagonal (a block of rank two, the two rows of a
/// single view, has a determinant of round-off that is positive half of the time).
pub(crate) fn well_conditioned(v: &[f64; 9]) -> bool {
    let m = [[v[0], v[1], v[2]], [v[3], v[4], v[5]], [v[6], v[7], v[8]]];
    det3(&m) > 1e-9 * v[0] * v[4] * v[8]
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

/// `V = J_p^T W J_p` of the point block `p` (`ps` coordinates, `P` when it is not 0) into `v`.
#[inline(always)]
fn point_information<const P: usize>(sh: &Shape, lin: &Lin, p: usize, ps: usize, v: &mut [f64]) {
    let s = if P > 0 { P } else { ps };
    v[..s * s].fill(0.0);
    for &k in sh.point_res.of(p) {
        let pos = sh.width(k) - s;
        for (row, jr) in sh.jac_rows(lin, k) {
            let w = lin.weights[row];
            let jp = &jr[pos..pos + s];
            for a in 0..s {
                let wa = w * jp[a];
                for b in 0..s {
                    v[s * a + b] += wa * jp[b];
                }
            }
        }
    }
}

/// The point blocks eliminated: each `V = J_p^T W J_p` over its residual blocks, held when it is
/// singular beyond round-off (`lm::well_conditioned_n`; a coordinate held on its floor counts as
/// one of unit information, its column being zero), then damped and inverted by a small dense
/// Cholesky (the closed form for a block of three).
pub(crate) fn point_inverses(red: &Reduced, g: &[f64], damping: &[f64]) -> PointInverses {
    let (sh, lin) = (red.shape, red.lin);
    let n = sh.n;
    let mut out = PointInverses { records: vec![0.0; *sh.inv_start.last().unwrap()] };
    let largest = sh.max_point;
    let records = split_sizes(&mut out.records, sh.point_size.iter().map(|s| 1 + s * s + s));
    records.into_par_iter().enumerate().for_each_init(
        || vec![0.0; largest * largest],
        |v, (p, record)| {
            let s = sh.point_size[p];
            let c0 = sh.point_col[p];
            if s == 3 {
                point_information::<3>(sh, lin, p, s, v);
            } else {
                point_information::<0>(sh, lin, p, s, v);
            }
            let v = &mut v[..s * s];
            for q in 0..s {
                if red.held[c0 + q] {
                    v[s * q + q] = 1.0;
                }
            }
            let (held, rest) = record.split_at_mut(1);
            let (vinv, y) = rest.split_at_mut(s * s);
            if !well_conditioned_n(v, s) {
                held[0] = 1.0;
                return;
            }
            for q in 0..s {
                v[s * q + q] += damping[n + c0 + q];
            }
            if !sym_inverse(v, s, vinv) {
                vinv.fill(0.0);
                held[0] = 1.0;
                return;
            }
            let gp = &g[n + c0..n + c0 + s];
            for (a, ya) in y.iter_mut().enumerate() {
                *ya = (0..s).map(|b| vinv[s * a + b] * gp[b]).sum();
            }
        },
    );
    out
}

/// Scratch of a column block: `J_c^T W J_p`, `Y = W V^-1` and the `J_c2^T W J_p` of another block.
#[derive(Default)]
struct ColumnScratch {
    wcp: Vec<f64>,
    y: Vec<f64>,
    w2: Vec<f64>,
    /// The camera blocks of a residual block the column keeps: their position in its rows, their
    /// first reduced column, their size, their place in `w2`.
    targets: Vec<(usize, usize, usize, usize)>,
}

/// The Schur terms of the residual block `k` (its camera block `c` at `pos`, its point block `p`
/// of `ps` coordinates, `P` when it is not 0, not held) in the column block of `c`.
#[inline(always)]
fn schur_terms<S: Slab, const P: usize>(
    cx: &Context,
    c: usize,
    k: usize,
    pos: usize,
    (p, ppos, ps): (usize, usize, usize),
    slab: &S,
    block: &mut [f64],
    rhs: &mut [f64],
    scratch: &mut ColumnScratch,
) {
    let ps = if P > 0 { P } else { ps };
    let sh = cx.shape;
    let Some((vinv, yp)) = cx.point(p, ps) else { return };
    let len = sh.cam_size[c];
    // W = J_c^T W J_p, Y = W V^-1, the right-hand side + W y, then - W' Y^T for every block of the
    // point
    let ColumnScratch { wcp, y, .. } = scratch;
    wcp[..len * ps].fill(0.0);
    cx.camera_point::<P>(k, pos, len, ppos, ps, wcp);
    for a in 0..len {
        let wa = &wcp[a * ps..a * ps + ps];
        for q in 0..ps {
            let mut t = 0.0;
            for r in 0..ps {
                t += wa[r] * vinv[r * ps + q];
            }
            y[a * ps + q] = t;
        }
        let mut t = 0.0;
        for q in 0..ps {
            t += wa[q] * yp[q];
        }
        rhs[a] += t;
    }
    let lin = cx.lin;
    for &k2 in sh.point_res.of(p) {
        // the blocks of this residual the column keeps, their J^T W J_p in one pass over its rows
        let ppos2 = sh.width(k2) - ps;
        let targets = &mut scratch.targets;
        targets.clear();
        let mut total = 0;
        for (c2, pos2) in sh.cameras_of(k2) {
            let s2 = sh.cam_col[c2];
            if c2 >= c && slab.keeps(s2) {
                targets.push((pos2, s2, sh.cam_size[c2], total));
                total += sh.cam_size[c2];
            }
        }
        if total == 0 {
            continue;
        }
        let w2 = &mut scratch.w2[..total * ps];
        w2.fill(0.0);
        for (row, jr) in sh.jac_rows(lin, k2) {
            let w = lin.weights[row];
            let jp = &jr[ppos2..ppos2 + ps];
            for &(pos2, _, size2, off) in targets.iter() {
                for b in 0..size2 {
                    let wb = w * jr[pos2 + b];
                    let o = &mut w2[(off + b) * ps..(off + b) * ps + ps];
                    for q in 0..ps {
                        o[q] += wb * jp[q];
                    }
                }
            }
        }
        let stride = slab.stride();
        for &(_, s2, size2, off) in targets.iter() {
            for b in 0..size2 {
                let wb = &w2[(off + b) * ps..(off + b) * ps + ps];
                let r0 = slab.at(0, s2 + b);
                for a in 0..len {
                    let ya = &y[a * ps..a * ps + ps];
                    let mut term = 0.0;
                    for q in 0..ps {
                        term += wb[q] * ya[q];
                    }
                    block[r0 + a * stride] -= term;
                }
            }
        }
    }
}

/// `schur_terms` for a tie point (three coordinates) seen from a camera block of at most
/// `SMALL_CAMERA` columns through residual blocks of rows at most `SMALL_WIDTH` wide: the same
/// sums, held on the stack.
#[inline(always)]
fn schur_terms_3<S: Slab>(
    cx: &Context,
    c: usize,
    k: usize,
    pos: usize,
    (p, ppos): (usize, usize),
    slab: &S,
    block: &mut [f64],
    rhs: &mut [f64],
) {
    let (sh, lin) = (cx.shape, cx.lin);
    let Some((vinv, yp)) = cx.point(p, 3) else { return };
    let len = sh.cam_size[c];
    let mut wcp = [[0.0; 3]; SMALL_CAMERA];
    for (row, jr) in sh.jac_rows(lin, k) {
        let w = lin.weights[row];
        let jp = [jr[ppos], jr[ppos + 1], jr[ppos + 2]];
        for a in 0..len {
            let wa = w * jr[pos + a];
            for q in 0..3 {
                wcp[a][q] += wa * jp[q];
            }
        }
    }
    let mut y = [[0.0; 3]; SMALL_CAMERA];
    for a in 0..len {
        let wa = wcp[a];
        for q in 0..3 {
            y[a][q] = wa[0] * vinv[q] + wa[1] * vinv[3 + q] + wa[2] * vinv[6 + q];
        }
        rhs[a] += wa[0] * yp[0] + wa[1] * yp[1] + wa[2] * yp[2];
    }
    let stride = slab.stride();
    for &k2 in sh.point_res.of(p) {
        let ppos2 = sh.width(k2) - 3;
        let mut targets = [(0usize, 0usize, 0usize, 0usize); SMALL_TARGETS];
        let (mut n_targets, mut total) = (0, 0);
        for (c2, pos2) in sh.cameras_of(k2) {
            let s2 = sh.cam_col[c2];
            if c2 >= c && slab.keeps(s2) {
                targets[n_targets] = (pos2, s2, sh.cam_size[c2], total);
                n_targets += 1;
                total += sh.cam_size[c2];
            }
        }
        if total == 0 {
            continue;
        }
        let targets = &targets[..n_targets];
        let mut w2 = [[0.0; 3]; SMALL_WIDTH];
        for (row, jr) in sh.jac_rows(lin, k2) {
            let w = lin.weights[row];
            let jp = [jr[ppos2], jr[ppos2 + 1], jr[ppos2 + 2]];
            for &(pos2, _, size2, off) in targets {
                for b in 0..size2 {
                    let wb = w * jr[pos2 + b];
                    for q in 0..3 {
                        w2[off + b][q] += wb * jp[q];
                    }
                }
            }
        }
        for &(_, s2, size2, off) in targets {
            for b in 0..size2 {
                let wb = w2[off + b];
                let r0 = slab.at(0, s2 + b);
                for a in 0..len {
                    let ya = y[a];
                    block[r0 + a * stride] -= wb[0] * ya[0] + wb[1] * ya[1] + wb[2] * ya[2];
                }
            }
        }
    }
}

/// The column block of the camera block `c` (not of the border): its diagonal block, the blocks
/// after it that share a residual block with it (direct terms) or a point block (the Schur terms),
/// the border; and its part of the right-hand side. `block` holds the columns of `c` as `slab` lays
/// them out.
pub(crate) fn camera_column<S: Slab>(cx: &Context, c: usize, slab: &S, block: &mut [f64], rhs: &mut [f64]) {
    let (sh, lin) = (cx.shape, cx.lin);
    let (s_c, len) = (sh.cam_col[c], sh.cam_size[c]);
    if len == 0 {
        return;
    }
    for a in 0..len {
        block[slab.at(a, s_c + a)] += cx.damping[s_c + a];
        rhs[a] = -cx.g[s_c + a];
    }
    let largest_point = sh.max_point;
    let mut scratch = ColumnScratch {
        wcp: vec![0.0; len * largest_point],
        y: vec![0.0; len * largest_point],
        w2: vec![0.0; sh.max_width * largest_point],
        targets: Vec::new(),
    };
    let small = len <= SMALL_CAMERA && sh.max_width <= SMALL_WIDTH && sh.max_cameras <= SMALL_TARGETS;
    for &(k, pos) in sh.camera_res.of(c) {
        // the direct terms: this block with itself and with the blocks after it in the residual
        for (row, jr) in sh.jac_rows(lin, k) {
            let w = lin.weights[row];
            for (c2, pos2) in sh.cameras_of(k) {
                let s2 = sh.cam_col[c2];
                if c2 < c || (c2 != c && !slab.keeps(s2)) {
                    continue;
                }
                let (j2, jc, stride) = (&jr[pos2..pos2 + sh.cam_size[c2]], &jr[pos..pos + len], slab.stride());
                for (b, v) in j2.iter().enumerate() {
                    let (r0, wb) = (slab.at(0, s2 + b), w * v);
                    for a in 0..len {
                        block[r0 + a * stride] += wb * jc[a];
                    }
                }
            }
        }
        let Some(point) = sh.point_of(k) else { continue };
        if point.2 == 3 && small {
            schur_terms_3(cx, c, k, pos, (point.0, point.1), slab, block, rhs);
        } else if point.2 == 3 {
            schur_terms::<S, 3>(cx, c, k, pos, point, slab, block, rhs, &mut scratch);
        } else {
            schur_terms::<S, 0>(cx, c, k, pos, point, slab, block, rhs, &mut scratch);
        }
    }
}

/// The Schur terms of the point block `p` (of `P` coordinates when it is not 0) in the border:
/// `F` the `J_b^T W J_p` of the border, `h -= F V^-1 F^T`, `r += F y`; `f` a scratch of the
/// border times the largest point block.
#[inline(always)]
fn border_schur<const P: usize>(cx: &Context, p: usize, f: &mut [f64], h: &mut [f64], r: &mut [f64]) {
    let sh = cx.shape;
    let ps = if P > 0 { P } else { sh.point_size[p] };
    let Some((vinv, yp)) = cx.point(p, ps) else { return };
    let (s_b, nb) = (sh.border_start, sh.n - sh.border_start);
    let mut any = false;
    for &k in sh.point_res.of(p) {
        if !sh.cameras_of(k).any(|(c, _)| sh.is_border(c)) {
            continue;
        }
        if !any {
            f[..nb * ps].fill(0.0);
            any = true;
        }
        let ppos = sh.width(k) - ps;
        for (c, pos) in sh.cameras_of(k).filter(|(c, _)| sh.is_border(*c)) {
            let off = sh.cam_col[c] - s_b;
            let size = sh.cam_size[c];
            cx.camera_point::<P>(k, pos, size, ppos, ps, &mut f[off * ps..(off + size) * ps]);
        }
    }
    if !any {
        return;
    }
    let mut fv = [0.0; 64];
    let mut fv_long = Vec::new();
    let fv: &mut [f64] = if ps <= 64 {
        &mut fv[..ps]
    } else {
        fv_long.resize(ps, 0.0);
        &mut fv_long
    };
    for q in 0..nb {
        let fq = &f[q * ps..(q + 1) * ps];
        if fq.iter().all(|x| *x == 0.0) {
            continue;
        }
        for (b, out) in fv.iter_mut().enumerate() {
            let mut t = 0.0;
            for a in 0..ps {
                t += fq[a] * vinv[a * ps + b];
            }
            *out = t;
        }
        for q2 in 0..nb {
            let f2 = &f[q2 * ps..(q2 + 1) * ps];
            let mut t = 0.0;
            for b in 0..ps {
                t += fv[b] * f2[b];
            }
            h[q * nb + q2] -= t;
        }
        let mut t = 0.0;
        for b in 0..ps {
            t += fq[b] * yp[b];
        }
        r[q] += t;
    }
}

/// The columns of the border: their direct terms over the residual blocks that touch it and the
/// Schur terms of the point blocks, both chunk by chunk and folded in a fixed order.
pub(crate) fn border_columns<S: Slab>(cx: &Context, slab: &S, block: &mut [f64], rhs: &mut [f64]) {
    let (sh, lin) = (cx.shape, cx.lin);
    let (n, s_b) = (sh.n, sh.border_start);
    let nb = n - s_b;
    if nb == 0 {
        return;
    }
    for k in 0..nb {
        block[slab.at(k, s_b + k)] += cx.damping[s_b + k];
        rhs[k] = -cx.g[s_b + k];
    }
    let direct: Vec<Vec<f64>> = sh
        .border_res
        .par_chunks(CHUNK)
        .map(|chunk| {
            let mut h = vec![0.0; nb * nb];
            for &k in chunk {
                for (row, jr) in sh.jac_rows(lin, k) {
                    let w = lin.weights[row];
                    for (c, pos) in sh.cameras_of(k).filter(|(c, _)| sh.is_border(*c)) {
                        let off = sh.cam_col[c] - s_b;
                        for a in 0..sh.cam_size[c] {
                            let wa = w * jr[pos + a];
                            for (c2, pos2) in sh.cameras_of(k).filter(|(c2, _)| sh.is_border(*c2)) {
                                let off2 = sh.cam_col[c2] - s_b;
                                for b in 0..sh.cam_size[c2] {
                                    h[(off + a) * nb + off2 + b] += wa * jr[pos2 + b];
                                }
                            }
                        }
                    }
                }
            }
            h
        })
        .collect();
    let starts: Vec<usize> = (0..sh.point_size.len()).step_by(CHUNK).collect();
    let largest = sh.max_point;
    let schur: Vec<(Vec<f64>, Vec<f64>)> = starts
        .into_par_iter()
        .map(|start| {
            let mut h = vec![0.0; nb * nb];
            let mut r = vec![0.0; nb];
            let mut f = vec![0.0; nb * largest];
            for p in start..(start + CHUNK).min(sh.point_size.len()) {
                if sh.point_size[p] == 3 {
                    border_schur::<3>(cx, p, &mut f, &mut h, &mut r);
                } else {
                    border_schur::<0>(cx, p, &mut f, &mut h, &mut r);
                }
            }
            (h, r)
        })
        .collect();
    for h in &direct {
        for q in 0..nb {
            for q2 in 0..nb {
                block[slab.at(q2, s_b + q)] += h[q * nb + q2];
            }
        }
    }
    for (h, r) in &schur {
        for q in 0..nb {
            rhs[q] += r[q];
            for q2 in 0..nb {
                block[slab.at(q2, s_b + q)] += h[q * nb + q2];
            }
        }
    }
}

/// Solves `A x = b` for a symmetric positive-definite `A` (`n x n`, column-major, its lower
/// triangle read) in place: the factor replaces `A`, the solution `b`. An error names the pivot
/// that is not positive.
pub(crate) fn cholesky_solve_in_place(matrix: &mut [f64], rhs: &mut [f64]) -> Result<(), String> {
    cholesky_solve_columns(matrix, rhs, 1)
}

/// `cholesky_solve_in_place` for `columns` right-hand sides at once (`rhs` column-major, `n` x
/// `columns`): one factorisation.
pub(crate) fn cholesky_solve_columns(matrix: &mut [f64], rhs: &mut [f64], columns: usize) -> Result<(), String> {
    let n = if columns == 0 { 0 } else { rhs.len() / columns };
    debug_assert_eq!(matrix.len(), n * n);
    if n == 0 {
        return Ok(());
    }
    let par = faer::get_global_parallelism();
    let scratch = factor::cholesky_in_place_scratch::<f64>(n, par, Default::default())
        .or(llt_solve::solve_in_place_scratch::<f64>(n, columns, par));
    let mut buffer = MemBuffer::new(scratch);
    let stack = MemStack::new(&mut buffer);
    let a = MatMut::from_column_major_slice_mut(matrix, n, n);
    factor::cholesky_in_place(a, factor::LltRegularization::default(), par, stack, Default::default())
        .map_err(|factor::LltError::NonPositivePivot { index }| {
            format!("pivot {index} of the reduced system is not positive")
        })?;
    let l = MatRef::from_column_major_slice(matrix, n, n);
    llt_solve::solve_in_place(l, MatMut::from_column_major_slice_mut(rhs, n, columns), par, stack);
    Ok(())
}

/// The step of the point block `p` (of `P` coordinates when it is not 0) into `out`, `acc` a
/// scratch of its size.
#[inline(always)]
fn point_step<const P: usize>(cx: &Context, cams: &[f64], p: usize, out: &mut [f64], acc: &mut [f64]) {
    let sh = cx.shape;
    let ps = if P > 0 { P } else { out.len() };
    let Some((vinv, _)) = cx.point(p, ps) else { return };
    let c0 = sh.n + sh.point_col[p];
    let acc = &mut acc[..ps];
    acc.copy_from_slice(&cx.g[c0..c0 + ps]);
    for &k in sh.point_res.of(p) {
        let ppos = sh.width(k) - ps;
        for (row, jr) in sh.jac_rows(cx.lin, k) {
            let w = cx.lin.weights[row];
            let mut u = 0.0;
            for (c, pos) in sh.cameras_of(k) {
                let s = sh.cam_col[c];
                for a in 0..sh.cam_size[c] {
                    u += jr[pos + a] * cams[s + a];
                }
            }
            let jp = &jr[ppos..ppos + ps];
            for q in 0..ps {
                acc[q] += w * jp[q] * u;
            }
        }
    }
    for a in 0..ps {
        let mut t = 0.0;
        for b in 0..ps {
            t += vinv[a * ps + b] * acc[b];
        }
        out[a] = -t;
    }
}

/// The point blocks from the step of the camera blocks (the first `shape.n` entries of `delta`):
/// `delta_p = -V^-1 (g_p + sum_k J_p^T W J_cam delta_cam)` over the residual blocks `k` of each
/// point block; a held block does not move.
pub(crate) fn back_substitute(cx: &Context, delta: &mut [f64]) {
    let sh = cx.shape;
    let (cams, point_cols) = delta.split_at_mut(sh.n);
    let cams = &*cams;
    let largest = sh.max_point;
    let slices = split_sizes(point_cols, sh.point_size.iter().copied());
    slices.into_par_iter().enumerate().for_each_init(
        || vec![0.0; largest],
        |acc, (p, out)| {
            if out.len() == 3 {
                point_step::<3>(cx, cams, p, out, acc);
            } else {
                point_step::<0>(cx, cams, p, out, acc);
            }
        },
    );
}

/// The column blocks of the camera blocks before the border, each with its own slice of `s`
/// (column-major, `stride` entries per column) and of `b`.
fn column_blocks<'s>(
    sh: &Shape,
    stride: usize,
    s: &'s mut [f64],
    b: &'s mut [f64],
) -> Vec<(&'s mut [f64], &'s mut [f64])> {
    let mut blocks = Vec::with_capacity(sh.border_block);
    let (mut rest_s, mut rest_b) = (s, b);
    for &len in &sh.cam_size[..sh.border_block] {
        let (head_s, tail_s) = rest_s.split_at_mut(len * stride);
        let (head_b, tail_b) = rest_b.split_at_mut(len);
        blocks.push((head_s, head_b));
        rest_s = tail_s;
        rest_b = tail_b;
    }
    blocks
}

/// The dense reduced system: its matrix (`n` x `n`, column-major; the lower triangle and the
/// whole diagonal blocks are written) and its right-hand side, assembled in parallel by column
/// block.
pub(crate) fn dense_system(cx: &Context) -> (Vec<f64>, Vec<f64>) {
    let sh = cx.shape;
    let n = sh.n;
    let mut s = vec![0.0; n * n];
    let mut b = vec![0.0; n];
    let slab = Full(n);
    let (camera_s, border_s) = s.split_at_mut(sh.border_start * n);
    let (camera_b, border_b) = b.split_at_mut(sh.border_start);
    let blocks = column_blocks(sh, n, camera_s, camera_b);
    blocks.into_par_iter().enumerate().for_each(|(c, (block, rhs))| camera_column(cx, c, &slab, block, rhs));
    border_columns(cx, &slab, border_s, border_b);
    (s, b)
}

/// One step of the damped normal equations `(J^T W J + diag(damping)) delta = -g`, in the columns
/// of the model: the point blocks eliminated, the reduced system solved by a dense Cholesky, the
/// point blocks back-substituted. An error when the reduced system is not positive definite.
pub(crate) fn dense_step(red: &Reduced, g: &[f64], damping: &[f64]) -> Result<Vec<f64>, String> {
    let points = point_inverses(red, g, damping);
    let cx = Context { shape: red.shape, lin: red.lin, g, damping, points: &points };
    let n = red.shape.n;
    let mut delta = vec![0.0; n + red.shape.n_point_cols];
    if n > 0 {
        let (mut s, mut b) = dense_system(&cx);
        cholesky_solve_in_place(&mut s, &mut b)?;
        delta[..n].copy_from_slice(&b);
    }
    back_substitute(&cx, &mut delta);
    Ok(delta)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::bundle::problem::{compose, exp_rotation, CameraBlock, Layout, Obs, Parameters, Problem};
    use crate::bundle::residuals::{evaluate_blocks, gradient_and_diagonal, triplets, Loss, Structure};
    use crate::bundle::solve::testing::system;
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
        // the two rows of one view, awkwardly turned: rank two, a determinant of round-off
        let (a, b) = ([0.6, 0.8, 0.3], [-0.8, 0.6, 0.1]);
        let mut rank2 = [0.0; 9];
        for i in 0..3 {
            for j in 0..3 {
                rank2[3 * i + j] = a[i] * a[j] + b[i] * b[j];
            }
        }
        assert!(!well_conditioned(&rank2));
        assert!(well_conditioned(&v));
        assert!(!well_conditioned(&[0.0; 9]));
        assert!(det3(&[[1.0, 2.0, 3.0], [0.0, 4.0, 5.0], [0.0, 0.0, 6.0]]) == 24.0);
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
    pub(crate) fn small_block() -> Problem {
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
            // the bar measures the distance of its markers, so that the exact block costs nothing
            bars: vec![(0, 1, 10.04f64.sqrt(), 0.002)],
            gnss: vec![(0, centres[0], [0.02; 3], 7), (1, centres[1], [0.02; 3], 2), (2, centres[2], [0.02; 3], 7)],
            fixed_images: vec![false, true, false],
            rolling_shutter: true,
            gnss_bias: true,
            ..Problem::default()
        }
    }

    /// The parameters of the block moved away from the truth, with velocities and biases.
    pub(crate) fn moved(pb: &Problem) -> Parameters {
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

    /// The inverse and the test of conditioning of a block of any size are those of a block of
    /// three for three, and invert a block of five.
    #[test]
    fn a_block_of_any_size_is_inverted_and_tested_as_a_block_of_three() {
        use crate::bundle::lm::{sym_inverse, well_conditioned_n};
        let v = [4.0, 1.0, 0.5, 1.0, 3.0, -0.25, 0.5, -0.25, 2.0];
        let mut inv = [0.0; 9];
        assert!(sym_inverse(&v, 3, &mut inv));
        assert_eq!(inv, sym3_inverse(&v).unwrap());
        assert!(well_conditioned_n(&v, 3));
        // a block of five: the diagonal of a block of three and two scales, coupled
        let mut five = [0.0; 25];
        for i in 0..5 {
            for j in 0..5 {
                five[5 * i + j] = if i == j { 4.0 + i as f64 } else { 0.5 / (1.0 + (i + j) as f64) };
            }
        }
        let mut inv = [0.0; 25];
        assert!(well_conditioned_n(&five, 5) && sym_inverse(&five, 5, &mut inv));
        for i in 0..5 {
            for j in 0..5 {
                let got: f64 = (0..5).map(|k| five[5 * i + k] * inv[5 * k + j]).sum();
                assert!((got - if i == j { 1.0 } else { 0.0 }).abs() < 1e-14, "({i}, {j}) = {got}");
            }
        }
        // rank four: a scale that duplicates a coordinate
        let mut rank4 = five;
        for k in 0..5 {
            rank4[5 * 4 + k] = rank4[k];
            rank4[5 * k + 4] = rank4[5 * k];
        }
        rank4[24] = rank4[0];
        assert!(!well_conditioned_n(&rank4, 5));
        assert!(!sym_inverse(&[1.0, 2.0, 2.0, 1.0], 2, &mut [0.0; 4]));
    }

    fn reduced_step_equals_the_full_step(loss: Loss, lambda: f64) {
        let pb = small_block();
        pb.check().unwrap();
        let lay = Layout::new(&pb);
        let st = Structure::new(&pb);
        let params = moved(&pb);
        let sys = system(&pb, params.clone(), loss);
        // photos of 12, 6 and 12 columns, two markers, a border of 6 intrinsics and 6 biases
        assert_eq!(sys.shape.cam_size, vec![12, 6, 12, 3, 3, 4, 2, 3, 3]);
        assert_eq!((sys.shape.border_start, sys.shape.n), (36, 48));
        let ev = evaluate_blocks(&pb, &lay, &st, &params, loss, true);
        assert!(ev.weights.iter().any(|w| *w < 1.0) || loss == Loss::None);
        // the weights and the gradient of the model are those of the residuals of the problem
        assert_eq!(&sys.lin.weights[..ev.weights.len()], &ev.weights[..]);
        let (g_layout, _) = gradient_and_diagonal(&pb, &lay, &st, &ev);
        let g = sys.to_layout(&sys.g);
        for (k, (a, b)) in g.iter().zip(&g_layout).enumerate() {
            assert!((a - b).abs() <= 1e-12 * b.abs().max(1.0), "gradient {k}: {a} vs {b}");
        }
        let damping = sys.damping(lambda);
        let delta = sys.to_layout(&dense_step(&sys.reduced(), &sys.g, &damping).unwrap());
        let damping = sys.to_layout(&damping);

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
        let params = moved(&pb);
        let ev = evaluate_blocks(&pb, &lay, &st, &params, Loss::None, true);
        let rows: Vec<f64> = st.point_obs.of(0).iter().map(|&j| ev.residuals[2 * j]).collect();
        assert!(!rows.is_empty() && rows.iter().all(|r| *r == 1e3));
        let sys = system(&pb, params, Loss::None);
        let damping = sys.damping(1e-4);
        let delta = sys.to_layout(&dense_step(&sys.reduced(), &sys.g, &damping).unwrap());
        assert_eq!(&delta[lay.point_col..lay.point_col + 3], &[0.0; 3]);
        assert!(delta.iter().all(|x| x.is_finite()));
        assert!(delta[lay.point_col + 3..lay.point_col + 6].iter().any(|x| *x != 0.0));
    }
}
