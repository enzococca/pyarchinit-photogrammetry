//! Levenberg-Marquardt over a reduced camera system, generic over the residual: a `Model` says the
//! sizes of its camera blocks (kept in the reduced system) and of its point blocks (eliminated by
//! the Schur complement, of any size), and linearises each of its residual blocks; the loop, the
//! damping, the acceptance of a step and the linear solvers (dense, sparse, conjugate gradients,
//! `Auto`) are those of the bundle adjustment of milestone 1, which is one such model
//! (`solve::BundleModel`).
//!
//! The diagonal of the normal equations is damped (Marquardt), a step is taken when the cost
//! falls, the damping is moved by the quality of the step (Nielsen's rule, as in Madsen, Nielsen
//! and Tingleff). The loop stops on the relative decrease of the cost, the largest entry of the
//! gradient, the size of the step, the number of iterations, or when no damping gives a step that
//! lowers the cost. Point coordinates with a floor (`Model::lower_bounds`) are projected on it
//! after a step; one that sits on its floor with the gradient pushing it down is held for the
//! step (its column taken out of the system), so that the others reach the minimum on the floor.
//!
//! Every sum that crosses blocks is made in a fixed order: two runs give the same bits.

use rayon::prelude::*;

use super::pcg::{pcg_step, pcg_step_converged};
use super::residuals::{Loss, CURVATURE_FLOOR};
use super::schur::{
    cholesky_solve_columns, dense_step, dense_system, point_inverses, split_sizes, sym3_inverse, well_conditioned,
    Context, Reduced,
};
use super::solve::{choose_linear_solver, LinearSolver, Options, Termination, DENSE_MAX_COLUMNS, PCG_ABOVE_BYTES};
use super::sparse::SparseSystem;

/// The damping starts here (a trust region of 1e4 of Marquardt's scaled units) ...
const INITIAL_DAMPING: f64 = 1e-4;
/// ... never goes below this ...
const MIN_DAMPING: f64 = 1e-16;
/// ... and above this the iteration has made no progress.
const MAX_DAMPING: f64 = 1e32;
/// The diagonal that scales the damping is clamped to this range, so that a column without
/// information (a point without usable observations) is still damped and does not move.
const DIAGONAL_MIN: f64 = 1e-6;
const DIAGONAL_MAX: f64 = 1e32;
/// Residual blocks per chunk of the evaluation: each chunk is filled by one thread, the chunks
/// joined in their order.
const EVALUATION_CHUNK: usize = 1024;
/// Residual blocks per chunk of the sums of the border.
const BORDER_CHUNK: usize = 1024;
/// Rows of the quadratic regime that can hold a point block: three, as for a tie point.
const HELD_ROWS: usize = 3;

/// One residual block linearised: its residuals, already divided by their sigma; the camera
/// blocks it touches (each once) and its point block; its Jacobian row-major, each row the columns
/// of `cameras` in their order (each as many as its block has) then those of the point block; the
/// loss on its rows; `constant` when its residuals do not depend on the parameters (an observation
/// behind its camera: a zero Jacobian, a cost no step can change, left out of the tolerance on the
/// cost).
#[derive(Clone, Debug, Default)]
pub struct BlockJacobian {
    pub residuals: Vec<f64>,
    pub cameras: Vec<usize>,
    pub point: Option<usize>,
    pub jacobian: Vec<f64>,
    pub loss: Loss,
    pub constant: bool,
}

impl BlockJacobian {
    /// Empty, the loss squared: how a model starts to fill it.
    pub fn clear(&mut self) {
        self.residuals.clear();
        self.cameras.clear();
        self.point = None;
        self.jacobian.clear();
        self.loss = Loss::None;
        self.constant = false;
    }
}

/// The problem as the solver sees it. The camera blocks are the columns of the reduced system, in
/// their order; the last `border_blocks` of them are the border, the blocks that many residuals
/// share (the free intrinsics, the biases of the flights), whose sums are folded chunk by chunk.
/// The point blocks are eliminated; their coordinates are numbered in the order of the blocks
/// (`point_coordinate`, `lower_bounds`, the `delta_points` of `apply`). A residual block keeps the
/// same blocks and the same number of rows from one linearisation to the next.
pub trait Model: Sync {
    /// The size of each camera block.
    fn camera_blocks(&self) -> &[usize];
    /// The size of each point block.
    fn point_blocks(&self) -> &[usize];
    fn residual_blocks(&self) -> usize;
    /// The residual block `k` with its Jacobian.
    fn linearize(&self, k: usize, out: &mut BlockJacobian);
    /// The residual block `k` without its Jacobian (the cost of a trial step): only `residuals`,
    /// `loss` and `constant` are read.
    fn residuals(&self, k: usize, out: &mut BlockJacobian) {
        self.linearize(k, out);
    }
    /// Moves the parameters by a step of the camera blocks and of the point blocks. A coordinate
    /// with a lower bound must take its step by plain addition (`x + delta`): the projection on the
    /// floor is computed so that this sum is not below it.
    fn apply(&mut self, delta_cameras: &[f64], delta_points: &[f64]);
    /// The parameters, to put them back after a rejected step.
    fn save(&self) -> Vec<f64>;
    fn restore(&mut self, saved: &[f64]);
    /// Point-block coordinates with a floor, `(coordinate, floor)`: a step is projected on them.
    /// The coordinates are those of `point_coordinate`, below the number of point coordinates, the
    /// floors finite (checked before the adjustment starts); `apply` adds the step to them plainly.
    fn lower_bounds(&self) -> &[(usize, f64)] {
        &[]
    }
    /// The value of a point-block coordinate (read for the coordinates of `lower_bounds`).
    fn point_coordinate(&self, index: usize) -> f64 {
        panic!("a model with lower bounds gives its point coordinate {index}")
    }
    /// How many of the last camera blocks are the border.
    fn border_blocks(&self) -> usize {
        0
    }
    /// Work of the model on its own after the start and after every accepted step (the inner
    /// iterations of the points the bundle adjustment re-solves); `true` when it moved anything.
    /// It must not raise the cost.
    fn settle(&mut self) -> bool {
        false
    }
    /// The norm of the parameters the tolerance on the step is measured against.
    fn parameter_norm(&self) -> f64 {
        self.save().iter().map(|x| x * x).sum::<f64>().sqrt()
    }
}

/// What `solve_model` reports: the cost before and after, the largest entry of the gradient at the
/// end, the iterations run, why they stopped and the solver of the reduced system.
#[derive(Clone, Debug, PartialEq)]
pub struct Report {
    pub cost_initial: f64,
    pub cost: f64,
    pub gradient_max: f64,
    pub iterations: usize,
    pub termination: Termination,
    pub linear_solver: LinearSolver,
}

/// Indices grouped by a key, as compressed rows: the residual blocks of each camera block (with
/// the position of the block in their rows) and of each point block.
pub(crate) struct Lists<T> {
    offsets: Vec<usize>,
    items: Vec<T>,
}

impl<T: Copy + Default> Lists<T> {
    /// From `(key, item)` pairs given in the order of the items.
    fn new(n: usize, pairs: &[(usize, T)]) -> Lists<T> {
        let mut offsets = vec![0; n + 1];
        for &(key, _) in pairs {
            offsets[key + 1] += 1;
        }
        for g in 0..n {
            offsets[g + 1] += offsets[g];
        }
        let mut next = offsets.clone();
        let mut items = vec![T::default(); pairs.len()];
        for &(key, item) in pairs {
            items[next[key]] = item;
            next[key] += 1;
        }
        Lists { offsets, items }
    }

    #[inline]
    pub(crate) fn of(&self, g: usize) -> &[T] {
        &self.items[self.offsets[g]..self.offsets[g + 1]]
    }
}

/// What does not change while a model is solved: the columns of the camera and point blocks, the
/// rows and the Jacobian entries of each residual block, which blocks it touches and where they
/// sit in its rows.
pub(crate) struct Shape {
    pub cam_size: Vec<usize>,
    /// First column of each camera block in the reduced system.
    pub cam_col: Vec<usize>,
    /// The reduced system: its size, the first border block and its first column.
    pub n: usize,
    pub border_block: usize,
    pub border_start: usize,
    pub point_size: Vec<usize>,
    /// First coordinate of each point block (in `delta_points`), first entry of its record in
    /// `schur::PointInverses` (a flag, its inverse, its `y`), and the end.
    pub point_col: Vec<usize>,
    pub inv_start: Vec<usize>,
    pub n_point_cols: usize,
    /// First row and first Jacobian entry of each residual block, and the ends.
    pub row_start: Vec<usize>,
    pub jac_start: Vec<usize>,
    /// What the solver reads of each residual block, packed (one cache line for the passes that
    /// walk the blocks of a point in the order of the point).
    pub blocks: Vec<BlockInfo>,
    /// The camera blocks of the residual blocks with their position in the rows.
    pub cam_list: Vec<(u32, u32)>,
    /// The widest row of a residual block, the largest point block.
    pub max_width: usize,
    pub max_point: usize,
    /// The most camera blocks of a residual block.
    pub max_cameras: usize,
    /// Residual blocks of each camera block, with the position of the block in their rows.
    pub camera_res: Lists<(usize, usize)>,
    /// Residual blocks of each point block.
    pub point_res: Lists<usize>,
    /// Residual blocks that touch the border.
    pub border_res: Vec<usize>,
    /// Chunks of the evaluation: first residual block of each, and the end.
    pub chunks: Vec<usize>,
    /// The point block of each point coordinate, when the model has lower bounds (empty without).
    pub coordinate_block: Vec<usize>,
}

/// A residual block: its first row and their number, its first Jacobian entry, the width of its
/// rows, its camera blocks in `Shape::cam_list` and its point block (`u32::MAX` for none).
#[derive(Clone, Copy)]
pub(crate) struct BlockInfo {
    row: usize,
    jac: usize,
    rows: u32,
    width: u32,
    cams: u32,
    n_cams: u32,
    point: u32,
    /// The camera blocks themselves when there are at most two (most residual blocks): no
    /// second place in memory to read for them.
    inline: [(u32, u32); 2],
}

impl Shape {
    #[inline]
    pub(crate) fn rows(&self, k: usize) -> std::ops::Range<usize> {
        let b = &self.blocks[k];
        b.row..b.row + b.rows as usize
    }

    /// The width of the rows of the residual block `k`.
    #[inline]
    pub(crate) fn width(&self, k: usize) -> usize {
        self.blocks[k].width as usize
    }

    /// The Jacobian row `row` of the residual block `k`.
    #[inline]
    pub(crate) fn row<'a>(&self, lin: &'a Lin, k: usize, row: usize) -> &'a [f64] {
        let b = &self.blocks[k];
        let w = b.width as usize;
        let at = b.jac + (row - b.row) * w;
        &lin.jac[at..at + w]
    }

    /// The rows of the residual block `k` with their Jacobian rows (none when its rows are empty).
    #[inline]
    pub(crate) fn jac_rows<'a>(&self, lin: &'a Lin, k: usize) -> impl Iterator<Item = (usize, &'a [f64])> + 'a {
        let b = self.blocks[k];
        let w = b.width as usize;
        let rows = if w == 0 { 0 } else { b.rows as usize };
        let jac = &lin.jac[b.jac..b.jac + w * rows];
        (0..rows).map(move |i| (b.row + i, &jac[i * w..i * w + w]))
    }

    /// The camera blocks of `k` and their positions in its rows, as stored.
    #[inline]
    pub(crate) fn cam_slice(&self, k: usize) -> &[(u32, u32)] {
        let b = &self.blocks[k];
        if b.n_cams <= 2 {
            &b.inline[..b.n_cams as usize]
        } else {
            &self.cam_list[b.cams as usize..(b.cams + b.n_cams) as usize]
        }
    }

    /// The camera blocks of `k` and their positions in its rows.
    #[inline]
    pub(crate) fn cameras_of(&self, k: usize) -> impl Iterator<Item = (usize, usize)> + '_ {
        self.cam_slice(k).iter().map(|&(c, pos)| (c as usize, pos as usize))
    }

    /// The point block of `k`, with its position in its rows and its size.
    #[inline]
    pub(crate) fn point_of(&self, k: usize) -> Option<(usize, usize, usize)> {
        let b = &self.blocks[k];
        (b.point != u32::MAX).then(|| {
            let p = b.point as usize;
            (p, b.width as usize - self.point_size[p], self.point_size[p])
        })
    }

    pub(crate) fn is_border(&self, c: usize) -> bool {
        c >= self.border_block
    }
}

/// A linearisation: the cost (`0.5 sum rho(r^2)` over the rows, in their order) and the part of it
/// of the constant blocks, the residuals, and with the Jacobian the weight of each row in the
/// normal matrix and in the gradient.
pub(crate) struct Lin {
    pub cost: f64,
    pub constant_cost: f64,
    pub residuals: Vec<f64>,
    pub weights: Vec<f64>,
    pub slopes: Vec<f64>,
    pub jac: Vec<f64>,
    pub loss: Vec<Loss>,
    pub constant: Vec<bool>,
}

/// The first linearisation of `m`, which gives its shape.
pub(crate) fn first_linearization<M: Model>(m: &M) -> Result<(Shape, Lin), String> {
    let cam_size = m.camera_blocks().to_vec();
    let point_size = m.point_blocks().to_vec();
    let n_blocks = m.residual_blocks();
    let (n_cam, n_point) = (cam_size.len(), point_size.len());
    let border = m.border_blocks();
    if border > n_cam {
        return Err(format!("{border} border blocks of {n_cam} camera blocks"));
    }
    let chunks: Vec<usize> = (0..n_blocks).step_by(EVALUATION_CHUNK).chain(std::iter::once(n_blocks)).collect();
    struct Part {
        rows: Vec<usize>,
        jacs: Vec<usize>,
        cams: Vec<usize>,
        cam_counts: Vec<usize>,
        point: Vec<usize>,
        residuals: Vec<f64>,
        jac: Vec<f64>,
        loss: Vec<Loss>,
        constant: Vec<bool>,
    }
    let parts: Vec<Result<Part, String>> = chunks
        .par_windows(2)
        .map(|w| {
            let mut out = BlockJacobian::default();
            let mut part = Part {
                rows: Vec::new(),
                jacs: Vec::new(),
                cams: Vec::new(),
                cam_counts: Vec::new(),
                point: Vec::new(),
                residuals: Vec::new(),
                jac: Vec::new(),
                loss: Vec::new(),
                constant: Vec::new(),
            };
            for k in w[0]..w[1] {
                out.clear();
                m.linearize(k, &mut out);
                let mut width = 0;
                for (a, &c) in out.cameras.iter().enumerate() {
                    if c >= n_cam || out.cameras[..a].contains(&c) {
                        return Err(format!("residual block {k}: camera blocks {:?} of {n_cam}", out.cameras));
                    }
                    width += cam_size[c];
                }
                if let Some(p) = out.point {
                    if p >= n_point {
                        return Err(format!("residual block {k}: point block {p} of {n_point}"));
                    }
                    width += point_size[p];
                }
                if out.jacobian.len() != out.residuals.len() * width {
                    let (rows, entries) = (out.residuals.len(), out.jacobian.len());
                    return Err(format!("residual block {k}: {entries} Jacobian entries for {rows} rows of {width}"));
                }
                part.rows.push(out.residuals.len());
                part.jacs.push(out.jacobian.len());
                part.cams.extend_from_slice(&out.cameras);
                part.cam_counts.push(out.cameras.len());
                part.point.push(out.point.unwrap_or(usize::MAX));
                part.residuals.extend_from_slice(&out.residuals);
                part.jac.extend_from_slice(&out.jacobian);
                part.loss.push(out.loss);
                part.constant.push(out.constant);
            }
            Ok(part)
        })
        .collect();
    let parts: Vec<Part> = parts.into_iter().collect::<Result<_, _>>()?;
    let mut row_start = Vec::with_capacity(n_blocks + 1);
    let mut jac_start = Vec::with_capacity(n_blocks + 1);
    let mut cams_start = Vec::with_capacity(n_blocks + 1);
    let (mut cams, mut point) = (Vec::with_capacity(n_blocks), Vec::with_capacity(n_blocks));
    let (mut loss, mut constant) = (Vec::with_capacity(n_blocks), Vec::with_capacity(n_blocks));
    let (mut rows_at, mut jac_at, mut cams_at) = (0, 0, 0);
    for part in &parts {
        for ((r, j), c) in part.rows.iter().zip(&part.jacs).zip(&part.cam_counts) {
            row_start.push(rows_at);
            jac_start.push(jac_at);
            cams_start.push(cams_at);
            (rows_at, jac_at, cams_at) = (rows_at + r, jac_at + j, cams_at + c);
        }
        cams.extend_from_slice(&part.cams);
        point.extend_from_slice(&part.point);
        loss.extend_from_slice(&part.loss);
        constant.extend_from_slice(&part.constant);
    }
    row_start.push(rows_at);
    jac_start.push(jac_at);
    cams_start.push(cams_at);
    // the residuals and the Jacobian joined in parallel, each part into its own place
    let (mut residuals, mut jac) = (vec![0.0; rows_at], vec![0.0; jac_at]);
    let residual_slices = split_sizes(&mut residuals, parts.iter().map(|p| p.residuals.len()));
    let jac_slices = split_sizes(&mut jac, parts.iter().map(|p| p.jac.len()));
    parts.par_iter().zip(residual_slices).zip(jac_slices).for_each(|((part, r), j)| {
        r.copy_from_slice(&part.residuals);
        j.copy_from_slice(&part.jac);
    });
    drop(parts);

    let mut cam_col = Vec::with_capacity(n_cam);
    let mut n = 0;
    for &s in &cam_size {
        cam_col.push(n);
        n += s;
    }
    let border_block = n_cam - border;
    let border_start = cam_col.get(border_block).copied().unwrap_or(n);
    let (mut point_col, mut inv_start) = (Vec::with_capacity(n_point), Vec::with_capacity(n_point + 1));
    let (mut n_point_cols, mut n_inv) = (0, 0);
    for &s in &point_size {
        point_col.push(n_point_cols);
        inv_start.push(n_inv);
        n_point_cols += s;
        n_inv += 1 + s * s + s;
    }
    inv_start.push(n_inv);
    let fits = |x: usize| u32::try_from(x).ok().filter(|v| *v != u32::MAX);
    if fits(cams.len()).is_none() || fits(n_point).is_none() {
        return Err(format!("{} camera entries and {n_point} point blocks: too many", cams.len()));
    }
    let mut cam_list = Vec::with_capacity(cams.len());
    let mut blocks = Vec::with_capacity(n_blocks);
    let mut camera_pairs = Vec::with_capacity(cams.len());
    let mut point_pairs = Vec::with_capacity(n_blocks);
    let mut border_res = Vec::new();
    for k in 0..n_blocks {
        let mut pos = 0;
        let mut touches_border = false;
        for &c in &cams[cams_start[k]..cams_start[k + 1]] {
            cam_list.push((c as u32, pos as u32));
            camera_pairs.push((c, (k, pos)));
            pos += cam_size[c];
            touches_border |= c >= border_block;
        }
        if point[k] != usize::MAX {
            point_pairs.push((point[k], k));
            pos += point_size[point[k]];
        }
        if touches_border {
            border_res.push(k);
        }
        let rows = row_start[k + 1] - row_start[k];
        let (Some(rows), Some(width)) = (fits(rows), fits(pos)) else {
            return Err(format!("residual block {k}: {rows} rows of {pos}: too many"));
        };
        let listed = &cam_list[cams_start[k]..];
        let mut inline = [(0, 0); 2];
        for (slot, entry) in inline.iter_mut().zip(listed) {
            *slot = *entry;
        }
        blocks.push(BlockInfo {
            inline,
            row: row_start[k],
            jac: jac_start[k],
            rows,
            width,
            cams: cams_start[k] as u32,
            n_cams: (cams_start[k + 1] - cams_start[k]) as u32,
            point: if point[k] == usize::MAX { u32::MAX } else { point[k] as u32 },
        });
    }
    for &(i, floor) in m.lower_bounds() {
        if i >= n_point_cols || !floor.is_finite() {
            return Err(format!("lower bound {floor} on point coordinate {i} of {n_point_cols}"));
        }
    }
    // the point block of each coordinate, for the floors
    let coordinate_block: Vec<usize> = if m.lower_bounds().is_empty() {
        Vec::new()
    } else {
        (0..n_point).flat_map(|p| std::iter::repeat_n(p, point_size[p])).collect()
    };
    let max_point = point_size.iter().copied().max().unwrap_or(0);
    let shape = Shape {
        coordinate_block,
        camera_res: Lists::new(n_cam, &camera_pairs),
        point_res: Lists::new(n_point, &point_pairs),
        cam_size,
        cam_col,
        n,
        border_block,
        border_start,
        point_size,
        point_col,
        inv_start,
        n_point_cols,
        row_start,
        jac_start,
        max_width: blocks.iter().map(|b| b.width as usize).max().unwrap_or(0),
        max_point,
        max_cameras: blocks.iter().map(|b| b.n_cams as usize).max().unwrap_or(0),
        blocks,
        cam_list,
        border_res,
        chunks,
    };
    let mut lin =
        Lin { cost: 0.0, constant_cost: 0.0, residuals, weights: Vec::new(), slopes: Vec::new(), jac, loss, constant };
    finish(&shape, &mut lin, true);
    Ok((shape, lin))
}

/// `m` evaluated on `shape`: the residuals and, with `with_jacobian`, the Jacobian.
fn linearize<M: Model>(m: &M, shape: &Shape, with_jacobian: bool) -> Lin {
    let n_rows = *shape.row_start.last().unwrap();
    let n_blocks = shape.blocks.len();
    let mut residuals = vec![0.0; n_rows];
    let mut jac = if with_jacobian { vec![0.0; *shape.jac_start.last().unwrap()] } else { Vec::new() };
    let mut loss = vec![Loss::None; n_blocks];
    let mut constant = vec![false; n_blocks];
    {
        // the buffers split by chunk of residual blocks
        let mut pieces = Vec::with_capacity(shape.chunks.len());
        let (mut res_rest, mut jac_rest) = (&mut residuals[..], &mut jac[..]);
        let (mut loss_rest, mut constant_rest) = (&mut loss[..], &mut constant[..]);
        for w in shape.chunks.windows(2) {
            let rows = shape.row_start[w[1]] - shape.row_start[w[0]];
            let entries = if with_jacobian { shape.jac_start[w[1]] - shape.jac_start[w[0]] } else { 0 };
            let (r, rr) = res_rest.split_at_mut(rows);
            let (j, jr) = jac_rest.split_at_mut(entries);
            let (l, lr) = loss_rest.split_at_mut(w[1] - w[0]);
            let (c, cr) = constant_rest.split_at_mut(w[1] - w[0]);
            pieces.push((w[0], w[1], r, j, l, c));
            (res_rest, jac_rest, loss_rest, constant_rest) = (rr, jr, lr, cr);
        }
        pieces.into_par_iter().for_each(|(first, end, res, jac, loss, constant)| {
            let mut out = BlockJacobian::default();
            let (row0, jac0) = (shape.row_start[first], shape.jac_start[first]);
            for k in first..end {
                out.clear();
                if with_jacobian {
                    m.linearize(k, &mut out);
                } else {
                    m.residuals(k, &mut out);
                }
                let rows = shape.rows(k);
                assert_eq!(out.residuals.len(), rows.len(), "residual block {k} changed its rows");
                res[rows.start - row0..rows.end - row0].copy_from_slice(&out.residuals);
                if with_jacobian {
                    let (a, b) = (shape.jac_start[k] - jac0, shape.jac_start[k + 1] - jac0);
                    assert_eq!(out.jacobian.len(), b - a, "residual block {k} changed its Jacobian");
                    jac[a..b].copy_from_slice(&out.jacobian);
                }
                loss[k - first] = out.loss;
                constant[k - first] = out.constant;
            }
        });
    }
    let (weights, slopes) = (Vec::new(), Vec::new());
    let mut lin = Lin { cost: 0.0, constant_cost: 0.0, residuals, weights, slopes, jac, loss, constant };
    finish(shape, &mut lin, with_jacobian);
    lin
}

/// The cost of the rows in their order and, with the Jacobian, the weights of the rows: in the
/// gradient the slope of their loss, in the normal matrix its curvature floored at
/// `CURVATURE_FLOOR` of the slope (the Triggs correction, as scipy applies it); but a point block
/// its quadratic rows do not hold (`held_by_inliers`) would take a Newton step up to
/// `1 / CURVATURE_FLOOR` too long: all its rows take the slope (the first-order weight).
fn finish(shape: &Shape, lin: &mut Lin, with_jacobian: bool) {
    let (mut cost, mut constant_cost) = (0.0, 0.0);
    for k in 0..shape.blocks.len() {
        let loss = lin.loss[k];
        if lin.constant[k] {
            for r in &lin.residuals[shape.rows(k)] {
                let rho = loss.rho(r * r);
                cost += rho;
                constant_cost += rho;
            }
        } else {
            for r in &lin.residuals[shape.rows(k)] {
                cost += loss.rho(r * r);
            }
        }
    }
    lin.cost = 0.5 * cost;
    lin.constant_cost = 0.5 * constant_cost;
    if !with_jacobian {
        return;
    }
    let n_rows = lin.residuals.len();
    let mut slopes = vec![0.0; n_rows];
    let mut weights = vec![0.0; n_rows];
    {
        let row_counts = || shape.chunks.windows(2).map(|w| shape.row_start[w[1]] - shape.row_start[w[0]]);
        let slope_slices = split_sizes(&mut slopes, row_counts());
        let weight_slices = split_sizes(&mut weights, row_counts());
        let lin_now: &Lin = lin;
        slope_slices.into_par_iter().zip(weight_slices).zip(shape.chunks.par_windows(2)).for_each(
            |((slopes, weights), w)| {
                let row0 = shape.row_start[w[0]];
                for k in w[0]..w[1] {
                    let loss = lin_now.loss[k];
                    for row in shape.rows(k) {
                        let s = lin_now.residuals[row] * lin_now.residuals[row];
                        let slope = loss.slope(s);
                        slopes[row - row0] = slope;
                        weights[row - row0] = loss.curvature(s).max(CURVATURE_FLOOR * slope);
                    }
                }
            },
        );
    }
    let n_point = shape.point_size.len();
    let lin_now: &Lin = lin;
    let unheld: Vec<bool> = (0..n_point)
        .into_par_iter()
        .map(|p| {
            let blocks = shape.point_res.of(p);
            if blocks.iter().all(|&k| lin_now.loss[k] == Loss::None) {
                return false;
            }
            let rows = blocks.iter().flat_map(|&k| {
                let (_, pos, size) = shape.point_of(k).expect("a block of the point");
                shape.rows(k).map(move |row| (lin_now.loss[k], lin_now.residuals[row], (k, row, pos, size)))
            });
            let rows =
                rows.map(|(loss, r, (k, row, pos, size))| (loss, r, &shape.row(lin_now, k, row)[pos..pos + size]));
            !held_by_inliers(shape.point_size[p], rows)
        })
        .collect();
    for (p, unheld) in unheld.iter().enumerate() {
        if *unheld {
            for &k in shape.point_res.of(p) {
                for row in shape.rows(k) {
                    weights[row] = slopes[row];
                }
            }
        }
    }
    lin.slopes = slopes;
    lin.weights = weights;
}

/// Whether a symmetric `n x n` matrix (row-major) is positive definite beyond round-off: the
/// product of the pivots of its Cholesky factor (its determinant) above `1e-9` of the product of
/// its diagonal; for `n = 3` the test of milestone 1 (`schur::well_conditioned`).
pub(crate) fn well_conditioned_n(v: &[f64], n: usize) -> bool {
    if n == 3 {
        return well_conditioned(&[v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8]]);
    }
    let mut l = v.to_vec();
    if !cholesky_rows(&mut l, n) {
        return false;
    }
    let det: f64 = (0..n).map(|k| l[k * n + k] * l[k * n + k]).product();
    let diagonal: f64 = (0..n).map(|k| v[k * n + k]).product();
    det > 1e-9 * diagonal
}

/// The Cholesky factor of a symmetric `n x n` matrix (row-major, the lower triangle read and
/// written) in place; `false` when a pivot is not positive (or not a number).
fn cholesky_rows(a: &mut [f64], n: usize) -> bool {
    for j in 0..n {
        let mut d = a[j * n + j];
        for k in 0..j {
            d -= a[j * n + k] * a[j * n + k];
        }
        if !(d > 0.0) {
            return false;
        }
        let l = d.sqrt();
        a[j * n + j] = l;
        for i in j + 1..n {
            let mut s = a[i * n + j];
            for k in 0..j {
                s -= a[i * n + k] * a[j * n + k];
            }
            a[i * n + j] = s / l;
        }
    }
    true
}

/// The inverse of a symmetric positive-definite `n x n` matrix (row-major) through its Cholesky
/// factor, into `out`; for `n = 3` the closed form of milestone 1 (`schur::sym3_inverse`); `false`
/// when a pivot is not positive.
pub(crate) fn sym_inverse(v: &[f64], n: usize, out: &mut [f64]) -> bool {
    if n == 3 {
        let Some(inv) = sym3_inverse(&[v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8]]) else { return false };
        out.copy_from_slice(&inv);
        return true;
    }
    let mut l = v.to_vec();
    if !cholesky_rows(&mut l, n) {
        return false;
    }
    // the columns of the identity solved through L L^T
    for col in 0..n {
        let mut x = vec![0.0; n];
        x[col] = 1.0;
        for i in 0..n {
            let mut s = x[i];
            for k in 0..i {
                s -= l[i * n + k] * x[k];
            }
            x[i] = s / l[i * n + i];
        }
        for i in (0..n).rev() {
            let mut s = x[i];
            for k in i + 1..n {
                s -= l[k * n + i] * x[k];
            }
            x[i] = s / l[i * n + i];
        }
        for i in 0..n {
            out[i * n + col] = x[i];
        }
    }
    true
}

/// Whether the rows `(loss, r, J_point)` of a point block of `n` coordinates hold it: at least
/// `HELD_ROWS` of them in the quadratic regime of their loss, carrying at least the first-order
/// information of the other rows in every direction (their difference positive definite). Two
/// rows of nadir photos are nearly parallel, so three quadratic rows alone may leave a direction to
/// the floor, along which the Newton step would be `1 / CURVATURE_FLOOR` too long.
fn held_by_inliers<'a>(n: usize, rows: impl Iterator<Item = (Loss, f64, &'a [f64])>) -> bool {
    // the two information matrices on the stack for the usual sizes
    let mut short = [0.0; 2 * 64];
    let mut long = Vec::new();
    let buffer: &mut [f64] = if n <= 8 {
        &mut short[..2 * n * n]
    } else {
        long.resize(2 * n * n, 0.0);
        &mut long
    };
    let (quadratic_info, linear_info) = buffer.split_at_mut(n * n);
    let mut count = 0;
    for (loss, r, j) in rows {
        let s = r * r;
        let (weight, info) = if loss.curvature(s) > CURVATURE_FLOOR * loss.slope(s) {
            count += 1;
            (loss.curvature(s), &mut *quadratic_info)
        } else {
            (loss.slope(s), &mut *linear_info)
        };
        for a in 0..n {
            for b in 0..n {
                info[n * a + b] += weight * j[a] * j[b];
            }
        }
    }
    if count < HELD_ROWS {
        return false;
    }
    for (q, l) in quadratic_info.iter_mut().zip(linear_info.iter()) {
        *q -= l;
    }
    let difference = &*quadratic_info;
    let mut inverse = [0.0; 64];
    let mut inverse_long = Vec::new();
    let inverse: &mut [f64] = if n <= 8 {
        &mut inverse[..n * n]
    } else {
        inverse_long.resize(n * n, 0.0);
        &mut inverse_long
    };
    well_conditioned_n(difference, n) && sym_inverse(difference, n, inverse)
}

/// The gradient `J^T S r` and the diagonal of `J^T W J` in the columns of the model (the reduced
/// system, then the point coordinates): sums per camera block and per point block in parallel,
/// each written by one thread in the order of its residual blocks; those of the border chunk by
/// chunk of its residual blocks, folded in their order.
pub(crate) fn gradient_and_diagonal(shape: &Shape, lin: &Lin) -> (Vec<f64>, Vec<f64>) {
    let size = shape.n + shape.n_point_cols;
    let (mut g, mut d) = (vec![0.0; size], vec![0.0; size]);
    let add = |k: usize, pos: usize, g: &mut [f64], d: &mut [f64]| {
        for (row, jr) in shape.jac_rows(lin, k) {
            let (w, sr) = (lin.weights[row], lin.slopes[row] * lin.residuals[row]);
            for a in 0..g.len() {
                let v = jr[pos + a];
                g[a] += v * sr;
                d[a] += w * v * v;
            }
        }
    };
    {
        // the camera blocks before the border and the point blocks, in two parallel passes (a
        // camera block has many more residual blocks than a point block)
        let (g_cam, g_point) = g.split_at_mut(shape.n);
        let (d_cam, d_point) = d.split_at_mut(shape.n);
        let g_cam = split_sizes(&mut g_cam[..shape.border_start], shape.cam_size[..shape.border_block].iter().copied());
        let d_cam = split_sizes(&mut d_cam[..shape.border_start], shape.cam_size[..shape.border_block].iter().copied());
        g_cam.into_par_iter().zip(d_cam).enumerate().for_each(|(c, (g, d))| {
            for &(k, pos) in shape.camera_res.of(c) {
                add(k, pos, g, d);
            }
        });
        let g_point = split_sizes(g_point, shape.point_size.iter().copied());
        let d_point = split_sizes(d_point, shape.point_size.iter().copied());
        g_point.into_par_iter().zip(d_point).enumerate().for_each(|(p, (g, d))| {
            for &k in shape.point_res.of(p) {
                add(k, shape.width(k) - g.len(), g, d);
            }
        });
    }
    // the border: chunks of the residual blocks that touch it, in parallel, folded in order
    let nb = shape.n - shape.border_start;
    if nb > 0 {
        let sums: Vec<(Vec<f64>, Vec<f64>)> = shape
            .border_res
            .par_chunks(BORDER_CHUNK)
            .map(|chunk| {
                let (mut gs, mut ds) = (vec![0.0; nb], vec![0.0; nb]);
                for &k in chunk {
                    for (row, jr) in shape.jac_rows(lin, k) {
                        let (w, sr) = (lin.weights[row], lin.slopes[row] * lin.residuals[row]);
                        for (c, pos) in shape.cameras_of(k).filter(|(c, _)| shape.is_border(*c)) {
                            let off = shape.cam_col[c] - shape.border_start;
                            for a in 0..shape.cam_size[c] {
                                let v = jr[pos + a];
                                gs[off + a] += v * sr;
                                ds[off + a] += w * v * v;
                            }
                        }
                    }
                }
                (gs, ds)
            })
            .collect();
        for (gs, ds) in &sums {
            for k in 0..nb {
                g[shape.border_start + k] += gs[k];
                d[shape.border_start + k] += ds[k];
            }
        }
    }
    (g, d)
}

/// The coordinates on their floor that the gradient pushes down are held for the step: their
/// Jacobian columns and their entries of the gradient and of the diagonal set to zero (the
/// eliminated block then gives them no step, `schur::point_inverses`). Returns the mask of the
/// point coordinates held.
fn hold_on_floors<M: Model>(m: &M, shape: &Shape, lin: &mut Lin, g: &mut [f64], d: &mut [f64]) -> Vec<bool> {
    let mut held = vec![false; shape.n_point_cols];
    let bounds = m.lower_bounds();
    if bounds.is_empty() {
        return held;
    }
    for &(i, floor) in bounds {
        let col = shape.n + i;
        if m.point_coordinate(i) <= floor && g[col] > 0.0 {
            held[i] = true;
            g[col] = 0.0;
            d[col] = 0.0;
            let p = shape.coordinate_block[i];
            let q = i - shape.point_col[p];
            for &k in shape.point_res.of(p) {
                let pos = shape.width(k) - shape.point_size[p];
                for row in shape.rows(k) {
                    let at = shape.jac_start[k] + (row - shape.row_start[k]) * shape.width(k) + pos + q;
                    lin.jac[at] = 0.0;
                }
            }
        }
    }
    held
}

fn max_abs(values: &[f64]) -> f64 {
    values.iter().fold(0.0, |a: f64, x| a.max(x.abs()))
}

/// The function tolerance the loop applies: the option's, floored at `ROBUST_FUNCTION_TOLERANCE`
/// under a robust loss (the option's, or one of a residual block).
fn function_tolerance(opts: &Options, lin: &Lin) -> f64 {
    let robust = opts.loss != Loss::None || lin.loss.iter().any(|l| *l != Loss::None);
    let loss = if robust { Loss::Huber(1.0) } else { Loss::None };
    super::solve::effective_function_tolerance(loss, opts.function_tolerance)
}

/// The solver of the reduced system a run uses, with what it keeps between the iterations.
pub(crate) enum Stepper {
    Dense,
    Sparse(SparseSystem),
    Pcg,
}

impl Stepper {
    /// The solver asked for, or the one `Auto` stands for on this problem within the limits
    /// given: `choose_linear_solver` on the columns of the reduced system and the bytes of its
    /// sparse factor. The pattern and the symbolic factorisation, which say how big the factor
    /// would be, are built only above the dense limit; the values are allocated only once the
    /// sparse Cholesky is chosen; when either cannot be built (memory) the conjugate gradients are
    /// taken.
    pub(crate) fn choose(
        shape: &Shape,
        asked: LinearSolver,
        dense_max_columns: usize,
        pcg_above: u64,
    ) -> Result<Stepper, String> {
        match asked {
            LinearSolver::Dense => Ok(Stepper::Dense),
            LinearSolver::Sparse => {
                let mut system = SparseSystem::new(shape)?;
                system.allocate()?;
                Ok(Stepper::Sparse(system))
            }
            LinearSolver::Pcg => Ok(Stepper::Pcg),
            LinearSolver::Auto => {
                if choose_linear_solver(shape.n, 0, dense_max_columns, pcg_above) == LinearSolver::Dense {
                    return Ok(Stepper::Dense);
                }
                let Ok(mut system) = SparseSystem::new(shape) else { return Ok(Stepper::Pcg) };
                let chosen = choose_linear_solver(shape.n, system.factor_bytes(), dense_max_columns, pcg_above);
                if chosen == LinearSolver::Pcg || system.allocate().is_err() {
                    return Ok(Stepper::Pcg);
                }
                Ok(Stepper::Sparse(system))
            }
        }
    }

    pub(crate) fn solver(&self) -> LinearSolver {
        match self {
            Stepper::Dense => LinearSolver::Dense,
            Stepper::Sparse(_) => LinearSolver::Sparse,
            Stepper::Pcg => LinearSolver::Pcg,
        }
    }

    /// One step of the damped normal equations, in the columns of the model.
    pub(crate) fn step(
        &mut self,
        shape: &Shape,
        lin: &Lin,
        held: &[bool],
        g: &[f64],
        damping: &[f64],
    ) -> Result<Vec<f64>, String> {
        let red = Reduced { shape, lin, held };
        match self {
            Stepper::Dense => dense_step(&red, g, damping),
            Stepper::Sparse(system) => system.step(&red, g, damping),
            Stepper::Pcg => pcg_step(&red, g, damping),
        }
    }
}

/// Adjusts `m` from the parameters it holds. `observe(iteration, cost)` is called after every
/// iteration with the cost reached; `false` stops the adjustment with `Err("cancelled")`.
pub fn solve_model<M: Model>(
    m: &mut M,
    opts: &Options,
    observe: &mut dyn FnMut(usize, f64) -> bool,
) -> Result<Report, String> {
    solve_model_with_limits(m, opts, observe, DENSE_MAX_COLUMNS, PCG_ABOVE_BYTES)
}

/// Up to this many reduced columns `covariance_block` forms the reduced system dense: 8000 columns
/// make a matrix of 512 MB (8 bytes x 8000^2), factorised in place (the scratch of the
/// factorisation and the right-hand sides are a small part of it) - a block of about 1000 photos
/// with their poses and a few intrinsics stays under it. Above, each column comes from the
/// conjugate gradients (`COVARIANCE_FORCING`, `COVARIANCE_PCG_ITERATIONS`): only a few columns are
/// asked for, and a column whose iterations stop before they converge is refused (`Err`, no
/// covariance), never returned short.
pub(crate) const COVARIANCE_DENSE_MAX_COLUMNS: usize = 8000;
pub(crate) const COVARIANCE_FORCING: f64 = 1e-12;
pub(crate) const COVARIANCE_PCG_ITERATIONS: usize = 5000;

/// The block of the reduced columns `cols` of the inverse of the normal matrix `J^T W J` (the
/// curvature of the robust loss as the steps use it, undamped) at the parameters of `m`: by the
/// Schur complement, the same block of the inverse of the reduced system. Up to
/// `COVARIANCE_DENSE_MAX_COLUMNS` reduced columns the system is formed dense and factorised once;
/// above, each column `c` is the step of the conjugate gradients for the gradient `-e_c`, `Err`
/// when they do not converge.
/// Row-major, `cols.len()` x `cols.len()`; times the variance factor, the covariance of those
/// unknowns. `Err` when the reduced system is not positive definite (a gauge left free, a
/// parameter the observations do not hold) or a column is out of range: there is no
/// regularisation. Points whose block is singular are held, as in a step.
pub(crate) fn covariance_block<M: Model>(m: &M, cols: &[usize]) -> Result<Vec<f64>, String> {
    covariance_block_with_limits(m, cols, COVARIANCE_DENSE_MAX_COLUMNS, COVARIANCE_PCG_ITERATIONS)
}

/// `covariance_block` with the dense limit and the iterations of the conjugate gradients as
/// arguments (the tests lower them).
pub(crate) fn covariance_block_with_limits<M: Model>(
    m: &M,
    cols: &[usize],
    dense_max_columns: usize,
    pcg_iterations: usize,
) -> Result<Vec<f64>, String> {
    let (shape, lin) = first_linearization(m)?;
    let n = shape.n;
    if let Some(c) = cols.iter().find(|&&c| c >= n) {
        return Err(format!("column {c} of {n} reduced columns"));
    }
    let (g, _) = gradient_and_diagonal(&shape, &lin);
    let held = vec![false; shape.n_point_cols];
    let red = Reduced { shape: &shape, lin: &lin, held: &held };
    let damping = vec![0.0; n + shape.n_point_cols];
    let k = cols.len();
    let mut out = vec![0.0; k * k];
    if n > dense_max_columns {
        for (j, &c) in cols.iter().enumerate() {
            let mut unit = vec![0.0; n + shape.n_point_cols];
            unit[c] = -1.0;
            let column = pcg_step_converged(&red, &unit, &damping, COVARIANCE_FORCING, pcg_iterations)
                .map_err(|e| format!("column {c}: {e}"))?;
            if !column[..n].iter().all(|x| x.is_finite()) || !(column[c] > 0.0) {
                return Err(format!("column {c}: the reduced system is not positive definite"));
            }
            for (i, &r) in cols.iter().enumerate() {
                out[i * k + j] = column[r];
            }
        }
        return Ok(out);
    }
    let points = point_inverses(&red, &g, &damping);
    let cx = Context { shape: &shape, lin: &lin, g: &g, damping: &damping, points: &points };
    let (mut s, _) = dense_system(&cx);
    let mut rhs = vec![0.0; n * k];
    for (j, &c) in cols.iter().enumerate() {
        rhs[j * n + c] = 1.0;
    }
    cholesky_solve_columns(&mut s, &mut rhs, k)?;
    for i in 0..k {
        for j in 0..k {
            out[i * k + j] = rhs[j * n + cols[i]];
        }
    }
    Ok(out)
}

/// The reduced columns of the solutions `x` of the undamped normal equations `J^T W J x = b` of
/// the linearisation `lin` (the curvature of the robust loss as the steps use it), one for each
/// right side `b` of `rhs` (one value per reduced column, none on the points: by the Schur
/// complement the reduced system alone gives them). The solver is the one `Auto` takes for the
/// steps within `dense_max_columns` and `pcg_above`: dense, the reduced system formed and
/// factorised once for every right side; sparse or the conjugate gradients, once per right side.
/// `Err` when the reduced system is not positive definite or a right side has another length.
pub(crate) fn solve_normal_equations(
    shape: &Shape,
    lin: &Lin,
    rhs: &[Vec<f64>],
    dense_max_columns: usize,
    pcg_above: u64,
) -> Result<Vec<Vec<f64>>, String> {
    let n = shape.n;
    if let Some(b) = rhs.iter().find(|b| b.len() != n) {
        return Err(format!("a right side of {} values for {n} reduced columns", b.len()));
    }
    if n == 0 || rhs.is_empty() {
        return Ok(rhs.to_vec());
    }
    let held = vec![false; shape.n_point_cols];
    let damping = vec![0.0; n + shape.n_point_cols];
    if choose_linear_solver(n, 0, dense_max_columns, pcg_above) == LinearSolver::Dense {
        let zero = vec![0.0; n + shape.n_point_cols];
        let red = Reduced { shape, lin, held: &held };
        let points = point_inverses(&red, &zero, &damping);
        let cx = Context { shape, lin, g: &zero, damping: &damping, points: &points };
        let (mut system, _) = dense_system(&cx);
        let mut columns = rhs.concat();
        cholesky_solve_columns(&mut system, &mut columns, rhs.len())?;
        return Ok(columns.chunks(n).map(<[f64]>::to_vec).collect());
    }
    let mut stepper = Stepper::choose(shape, LinearSolver::Auto, dense_max_columns, pcg_above)?;
    rhs.iter()
        .map(|b| {
            // a step solves (J^T W J) delta = -g: the right side as minus a gradient
            let mut g = vec![0.0; n + shape.n_point_cols];
            for (gk, bk) in g.iter_mut().zip(b) {
                *gk = -bk;
            }
            let delta = stepper.step(shape, lin, &held, &g, &damping)?;
            if !delta[..n].iter().all(|x| x.is_finite()) {
                return Err("the reduced system is not positive definite".into());
            }
            Ok(delta[..n].to_vec())
        })
        .collect()
}

/// `solve_model` with the limits of `Auto` as arguments (the tests lower them to small blocks).
pub(crate) fn solve_model_with_limits<M: Model>(
    m: &mut M,
    opts: &Options,
    observe: &mut dyn FnMut(usize, f64) -> bool,
    dense_max_columns: usize,
    pcg_above: u64,
) -> Result<Report, String> {
    let (shape, mut lin) = first_linearization(m)?;
    let mut stepper = Stepper::choose(&shape, opts.linear_solver, dense_max_columns, pcg_above)?;
    let linear_solver = stepper.solver();
    let function_tolerance = function_tolerance(opts, &lin);
    if !lin.cost.is_finite() {
        return Err(format!("the cost of the problem as given is {}", lin.cost));
    }
    let cost_initial = lin.cost;
    // what the model settles on its own (the points their inliers do not hold) comes first
    if m.settle() {
        lin = linearize(m, &shape, true);
    }
    let (mut g, mut diag) = gradient_and_diagonal(&shape, &lin);
    let mut held = hold_on_floors(m, &shape, &mut lin, &mut g, &mut diag);
    let mut gradient_max = max_abs(&g);
    let mut lambda = INITIAL_DAMPING;
    let mut nu = 2.0;
    let mut iterations = 0;
    let mut termination = (gradient_max <= opts.gradient_tolerance).then_some(Termination::Gradient);
    while termination.is_none() {
        if iterations >= opts.max_iterations {
            termination = Some(Termination::MaxIterations);
            break;
        }
        iterations += 1;
        let damping: Vec<f64> = diag.iter().map(|d| lambda * d.clamp(DIAGONAL_MIN, DIAGONAL_MAX)).collect();
        let mut accepted = false;
        if let Ok(mut delta) = stepper.step(&shape, &lin, &held, &g, &damping) {
            if delta.iter().all(|x| x.is_finite()) {
                // the coordinates with a floor projected on it: the step that reaches the floor,
                // raised by round-off until the sum is not below it
                for &(i, floor) in m.lower_bounds() {
                    let value = m.point_coordinate(i);
                    let col = shape.n + i;
                    if value + delta[col] < floor {
                        delta[col] = floor - value;
                        while value + delta[col] < floor {
                            delta[col] = delta[col].next_up();
                        }
                    }
                }
                let ptol = opts.parameter_tolerance;
                let norm = m.parameter_norm();
                let saved = m.save();
                m.apply(&delta[..shape.n], &delta[shape.n..]);
                let trial = linearize(m, &shape, false);
                let decrease = lin.cost - trial.cost;
                // what the quadratic model promised: 0.5 delta^T (damping delta - g); for a step
                // clipped on a floor only an approximation, which only scales the damping (the step
                // is accepted on the true cost)
                let model_decrease =
                    0.5 * delta.iter().zip(&damping).zip(&g).map(|((d, m), gk)| d * (m * d - gk)).sum::<f64>();
                let step_norm = delta.iter().map(|x| x * x).sum::<f64>().sqrt();
                let tiny_step = step_norm <= ptol * (norm + ptol);
                // measured against the cost a step can change: not the constant of the observations
                // behind their camera, which would loosen the tolerance by orders of magnitude; and
                // only on a step that is not damped (after rejections, a damped step is small by
                // construction, not because the cost has converged)
                let live_cost = lin.cost - lin.constant_cost;
                let undamped = lambda <= INITIAL_DAMPING;
                let tiny_change =
                    undamped && trial.cost.is_finite() && decrease.abs() <= function_tolerance * live_cost;
                if trial.cost.is_finite() && decrease > 0.0 {
                    accepted = true;
                    let rho = if model_decrease > 0.0 { decrease / model_decrease } else { 1.0 };
                    m.settle();
                    lin = linearize(m, &shape, true);
                    (g, diag) = gradient_and_diagonal(&shape, &lin);
                    held = hold_on_floors(m, &shape, &mut lin, &mut g, &mut diag);
                    gradient_max = max_abs(&g);
                    lambda = (lambda * (1.0 / 3.0f64).max(1.0 - (2.0 * rho - 1.0).powi(3))).max(MIN_DAMPING);
                    nu = 2.0;
                } else {
                    m.restore(&saved);
                }
                if tiny_change {
                    termination = Some(Termination::Function);
                } else if accepted && gradient_max <= opts.gradient_tolerance {
                    termination = Some(Termination::Gradient);
                } else if tiny_step {
                    termination = Some(Termination::Parameters);
                }
            }
        }
        if !accepted && termination.is_none() {
            lambda *= nu;
            nu *= 2.0;
            if lambda > MAX_DAMPING {
                termination = Some(Termination::NoProgress);
            }
        }
        if !observe(iterations, lin.cost) {
            return Err("cancelled".into());
        }
    }
    Ok(Report {
        cost_initial,
        cost: lin.cost,
        gradient_max,
        iterations,
        termination: termination.expect("the loop ends with a termination"),
        linear_solver,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bundle::pcg::pcg_step_with;
    use crate::bundle::schur::cholesky_solve_in_place;

    /// A linear toy: two camera blocks before the border and two in it, point blocks of 2, 4 and 5
    /// coordinates (the second seen by a single block of two rows: singular, held), residual blocks
    /// coupling several camera blocks with a point block, camera blocks without a point, the
    /// border alone; entries from a fixed generator, some blocks under Huber.
    struct Toy {
        cams: Vec<usize>,
        points: Vec<usize>,
        blocks: Vec<(Vec<usize>, Option<usize>, Vec<f64>, Vec<f64>, Loss)>,
    }

    struct Lcg(u64);

    impl Lcg {
        fn next(&mut self) -> f64 {
            self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            ((self.0 >> 11) as f64 / (1u64 << 53) as f64) * 2.0 - 1.0
        }
    }

    impl Toy {
        fn new() -> Toy {
            let cams = vec![3, 4, 3, 2];
            let points = vec![2, 4, 5, 2, 5, 4];
            let mut rng = Lcg(7);
            let mut blocks = Vec::new();
            let mut add = |cams_of: Vec<usize>, point: Option<usize>, rows: usize, loss: Loss, rng: &mut Lcg| {
                let width: usize = cams_of.iter().map(|&c| cams[c]).sum::<usize>() + point.map_or(0, |p| points[p]);
                let jac = (0..rows * width).map(|_| rng.next()).collect();
                let res = (0..rows).map(|_| 3.0 * rng.next()).collect();
                blocks.push((cams_of, point, jac, res, loss));
            };
            for p in [0, 2, 3, 4, 5] {
                let rows = points[p] + 1;
                // each point seen through both cameras before the border, alone and with the border
                add(vec![0, 2], Some(p), rows, Loss::None, &mut rng);
                add(vec![1, 3], Some(p), rows, Loss::Huber(1.0), &mut rng);
                add(vec![0, 1], Some(p), rows, Loss::None, &mut rng);
                add(vec![2], Some(p), rows, Loss::Cauchy(2.0), &mut rng);
            }
            // the singular point block: two rows of one residual block
            add(vec![0, 1, 2], Some(1), 2, Loss::None, &mut rng);
            // camera blocks without a point, the border alone
            add(vec![0, 1], None, 5, Loss::None, &mut rng);
            add(vec![2, 3], None, 4, Loss::None, &mut rng);
            add(vec![3], None, 3, Loss::None, &mut rng);
            Toy { cams, points, blocks }
        }
    }

    impl Model for Toy {
        fn camera_blocks(&self) -> &[usize] {
            &self.cams
        }

        fn point_blocks(&self) -> &[usize] {
            &self.points
        }

        fn residual_blocks(&self) -> usize {
            self.blocks.len()
        }

        fn linearize(&self, k: usize, out: &mut BlockJacobian) {
            let (cams, point, jac, res, loss) = &self.blocks[k];
            out.cameras.extend_from_slice(cams);
            out.point = *point;
            out.jacobian.extend_from_slice(jac);
            out.residuals.extend_from_slice(res);
            out.loss = *loss;
        }

        fn apply(&mut self, _: &[f64], _: &[f64]) {}

        fn save(&self) -> Vec<f64> {
            Vec::new()
        }

        fn restore(&mut self, _: &[f64]) {}

        fn border_blocks(&self) -> usize {
            2
        }
    }

    /// The step of the full normal equations `(J^T W J + diag(damping)) delta = -g`, the columns of
    /// `held` left out (their step zero).
    fn full_step(shape: &Shape, lin: &Lin, g: &[f64], damping: &[f64], held: &[usize]) -> Vec<f64> {
        let size = shape.n + shape.n_point_cols;
        let mut h = vec![0.0; size * size];
        for k in 0..shape.blocks.len() {
            let mut cols = Vec::new();
            for (c, _) in shape.cameras_of(k) {
                cols.extend(shape.cam_col[c]..shape.cam_col[c] + shape.cam_size[c]);
            }
            if let Some((p, _, s)) = shape.point_of(k) {
                cols.extend(shape.n + shape.point_col[p]..shape.n + shape.point_col[p] + s);
            }
            for (row, jr) in shape.jac_rows(lin, k) {
                let w = lin.weights[row];
                for (a, &ca) in cols.iter().enumerate() {
                    for (b, &cb) in cols.iter().enumerate() {
                        h[cb * size + ca] += w * jr[a] * jr[b];
                    }
                }
            }
        }
        let free: Vec<usize> = (0..size).filter(|c| !held.contains(c)).collect();
        let m = free.len();
        let mut a = vec![0.0; m * m];
        let mut b: Vec<f64> = free.iter().map(|&c| -g[c]).collect();
        for (i, &ci) in free.iter().enumerate() {
            for (j, &cj) in free.iter().enumerate() {
                a[j * m + i] = h[cj * size + ci] + if ci == cj { damping[ci] } else { 0.0 };
            }
        }
        cholesky_solve_in_place(&mut a, &mut b).unwrap();
        let mut out = vec![0.0; size];
        for (i, &c) in free.iter().enumerate() {
            out[c] = b[i];
        }
        out
    }

    #[test]
    fn the_covariance_block_is_the_block_of_the_inverse_of_the_full_normal_equations() {
        let toy = Toy::new();
        let (shape, lin) = first_linearization(&toy).unwrap();
        let singular: Vec<usize> = (0..4).map(|q| shape.n + shape.point_col[1] + q).collect();
        let size = shape.n + shape.n_point_cols;
        // the border (the last camera columns) and a column of the first camera block
        let cols: Vec<usize> = std::iter::once(1).chain(shape.border_start..shape.n).collect();
        let got = covariance_block(&toy, &cols).unwrap();
        let k = cols.len();
        for (j, &c) in cols.iter().enumerate() {
            // the column `c` of the inverse: the step of the full equations for the gradient -e_c
            let mut g = vec![0.0; size];
            g[c] = -1.0;
            let want = full_step(&shape, &lin, &g, &vec![0.0; size], &singular);
            let scale = want.iter().fold(0.0, |a: f64, x| a.max(x.abs()));
            for (i, &r) in cols.iter().enumerate() {
                let (a, b) = (got[i * k + j], want[r]);
                assert!((a - b).abs() <= 1e-9 * scale, "entry ({r}, {c}): {a} vs {b}");
            }
        }
        assert!(covariance_block(&toy, &[shape.n]).is_err());
        // above the dense limit (lowered here) the columns come from the conjugate gradients
        let pcg = covariance_block_with_limits(&toy, &cols, 0, COVARIANCE_PCG_ITERATIONS).unwrap();
        let scale = got.iter().fold(0.0, |a: f64, x| a.max(x.abs()));
        for (a, b) in pcg.iter().zip(&got) {
            assert!((a - b).abs() <= 1e-6 * scale, "{a} vs {b}");
        }
    }

    /// A gauge left free (a column no residual holds): the covariance is refused, dense or not.
    #[test]
    fn a_covariance_of_a_singular_system_is_refused() {
        let mut toy = Toy::new();
        toy.cams.push(2); // a camera block no residual holds
        let (shape, _) = first_linearization(&toy).unwrap();
        let cols = [shape.n - 1];
        assert!(covariance_block(&toy, &cols).is_err());
        assert!(covariance_block_with_limits(&toy, &cols, 0, COVARIANCE_PCG_ITERATIONS).is_err());
    }

    #[test]
    fn variable_point_blocks_with_several_cameras_and_a_border_give_the_step_of_the_full_equations() {
        let toy = Toy::new();
        let (shape, lin) = first_linearization(&toy).unwrap();
        assert_eq!((shape.n, shape.border_block, shape.border_start), (12, 2, 7));
        assert!(lin.loss.iter().any(|l| *l != Loss::None));
        let (g, d) = gradient_and_diagonal(&shape, &lin);
        let held = vec![false; shape.n_point_cols];
        let red = Reduced { shape: &shape, lin: &lin, held: &held };
        let singular: Vec<usize> = (0..4).map(|q| shape.n + shape.point_col[1] + q).collect();
        for lambda in [1e-4, 1e-2, 1.0] {
            let damping: Vec<f64> = d.iter().map(|x| lambda * x.max(1e-6)).collect();
            let want = full_step(&shape, &lin, &g, &damping, &singular);
            let scale = want.iter().fold(0.0, |a: f64, x| a.max(x.abs()));
            assert!(scale > 1e-3);
            let dense = dense_step(&red, &g, &damping).unwrap();
            let sparse = SparseSystem::new(&shape).unwrap().step(&red, &g, &damping).unwrap();
            let pcg = pcg_step_with(&red, &g, &damping, 0.0, 2000).unwrap();
            for (name, got, tolerance) in [("dense", &dense, 1e-9), ("sparse", &sparse, 1e-9), ("pcg", &pcg, 1e-7)] {
                for &c in &singular {
                    assert_eq!(got[c], 0.0, "{name}: the singular block moved (lambda {lambda})");
                }
                for (k, (a, b)) in got.iter().zip(&want).enumerate() {
                    assert!((a - b).abs() <= tolerance * scale, "{name}, lambda {lambda}: column {k}: {a} vs {b}");
                }
            }
        }
    }
}
