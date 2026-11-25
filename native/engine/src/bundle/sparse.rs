//! The reduced camera system stored sparse: a camera block couples with the blocks after it that
//! share a residual block or a point block with it (for the bundle adjustment: a photo with the
//! photos it shares points with and with the markers it observes, a marker with the markers it
//! shares a bar with) and with the border; the border columns last. The lower triangle is kept in
//! compressed columns, every column of a block holding the same rows, and assembled in parallel
//! by column block with the assemblers of `schur`. faer's sparse Cholesky factorises it with its
//! fill-reducing ordering (approximate minimum degree, which puts the border, coupled with every
//! photo, last); the symbolic factorisation is computed once per solve, the numeric one at every
//! iteration.

use faer::dyn_stack::{MemBuffer, MemStack, StackReq};
use faer::linalg::cholesky::llt::factor::{LltError as PivotError, LltRegularization};
use faer::sparse::linalg::cholesky::{
    factorize_symbolic_cholesky, CholeskySymbolicParams, SymbolicCholesky, SymmetricOrdering,
};
use faer::sparse::{SparseColMatRef, SymbolicSparseColMat};
use faer::{Conj, MatMut, Side};
use rayon::prelude::*;

use super::lm::Shape;
use super::schur::{back_substitute, border_columns, camera_column, point_inverses, Block, Context, Reduced, Slab};

/// A row of the reduced system that is not in the pattern of a column block.
const NOT_IN_PATTERN: u32 = u32::MAX;

/// The columns of one block of the pattern: `stride` rows per column, `offset[row]` the
/// position of the reduced row `row` among them.
struct Pattern<'a> {
    stride: usize,
    offset: &'a [u32],
}

impl Slab for Pattern<'_> {
    #[inline]
    fn keeps(&self, _: usize) -> bool {
        true
    }

    #[inline]
    fn at(&self, a: usize, row: usize) -> usize {
        let k = self.offset[row];
        debug_assert_ne!(k, NOT_IN_PATTERN, "row {row} is not in the pattern of the column block");
        a * self.stride + k as usize
    }

    #[inline]
    fn stride(&self) -> usize {
        self.stride
    }
}

/// The reduced system of one solve: its pattern, the symbolic factorisation, and the buffers of
/// the values, of the factor and of faer's workspace, reused at every iteration.
pub(crate) struct SparseSystem {
    pattern: SymbolicSparseColMat<usize>,
    /// `None` for an empty reduced system (everything fixed).
    symbolic: Option<SymbolicCholesky<usize>>,
    values: Vec<f64>,
    factor: Vec<f64>,
    factor_scratch: MemBuffer,
    solve_scratch: MemBuffer,
}

fn out_of_memory<E>(_: E) -> String {
    "not enough memory for the sparse factorisation of the reduced system".to_string()
}

/// The rows of the column block of the camera block `c` (not of the border): its own, those of
/// the blocks after it (before the border) that share a residual block or a point block with it,
/// the border. `seen` is a scratch of one entry per camera block, stamped with `c`.
fn camera_rows(sh: &Shape, c: usize, seen: &mut [usize]) -> Vec<usize> {
    let (s_c, len) = (sh.cam_col[c], sh.cam_size[c]);
    let mut rows = Vec::new();
    if len == 0 {
        return rows;
    }
    rows.extend(s_c..s_c + len);
    let mut after = Vec::new();
    let mut add = |c2: usize, after: &mut Vec<usize>| {
        if c2 > c && !sh.is_border(c2) && sh.cam_size[c2] > 0 && seen[c2] != c {
            seen[c2] = c;
            after.push(c2);
        }
    };
    for &(k, _) in sh.camera_res.of(c) {
        for (c2, _) in sh.cameras_of(k) {
            add(c2, &mut after);
        }
        if let Some((p, _, _)) = sh.point_of(k) {
            for &k2 in sh.point_res.of(p) {
                for (c2, _) in sh.cameras_of(k2) {
                    add(c2, &mut after);
                }
            }
        }
    }
    after.sort_unstable();
    for c2 in after {
        rows.extend(sh.cam_col[c2]..sh.cam_col[c2] + sh.cam_size[c2]);
    }
    rows.extend(sh.border_start..sh.n);
    rows
}

/// The pattern of the lower triangle (with the whole diagonal block of every column block, of
/// which faer reads the lower part), in compressed columns; an error when there is no memory for
/// its arrays (the row lists of the blocks, one per block, are small and not reserved that way).
fn pattern(sh: &Shape) -> Result<SymbolicSparseColMat<usize>, String> {
    let n = sh.n;
    let n_cam = sh.cam_size.len();
    let cameras: Vec<Vec<usize>> = (0..sh.border_block)
        .into_par_iter()
        .map_init(|| vec![usize::MAX; n_cam], |seen, c| camera_rows(sh, c, seen))
        .collect();
    let border: Vec<usize> = (sh.border_start..n).collect();
    let blocks = || {
        cameras
            .iter()
            .zip(&sh.cam_size)
            .map(|(rows, &len)| (rows, len))
            .chain(std::iter::once((&border, n - sh.border_start)))
    };
    let entries: usize = blocks().map(|(rows, len)| rows.len() * len).sum();
    let mut col_ptr = Vec::new();
    col_ptr.try_reserve_exact(n + 1).map_err(out_of_memory)?;
    let mut row_idx = Vec::new();
    row_idx.try_reserve_exact(entries).map_err(out_of_memory)?;
    col_ptr.push(0);
    for (rows, len) in blocks() {
        for _ in 0..len {
            row_idx.extend_from_slice(rows);
            col_ptr.push(row_idx.len());
        }
    }
    debug_assert_eq!((col_ptr.len(), row_idx.len()), (n + 1, entries));
    Ok(SymbolicSparseColMat::new_checked(n, n, col_ptr, None, row_idx))
}

/// Splits `values` and `rhs` into the slabs of the column blocks of `lengths` columns each
/// (`col_ptr` says how many entries a column holds), the border as the rest.
fn split_blocks<'a>(
    col_ptr: &[usize],
    lengths: impl Iterator<Item = usize>,
    mut values: &'a mut [f64],
    mut rhs: &'a mut [f64],
) -> (Vec<(&'a mut [f64], &'a mut [f64])>, (&'a mut [f64], &'a mut [f64])) {
    let mut blocks = Vec::new();
    let mut col = 0;
    for len in lengths {
        let entries = col_ptr[col + len] - col_ptr[col];
        let (head_v, tail_v) = values.split_at_mut(entries);
        let (head_b, tail_b) = rhs.split_at_mut(len);
        blocks.push((head_v, head_b));
        values = tail_v;
        rhs = tail_b;
        col += len;
    }
    (blocks, (values, rhs))
}

impl SparseSystem {
    /// The pattern of the reduced system of `sh` and its symbolic factorisation, and nothing more:
    /// what `Auto` builds to judge the size of the factor (`factor_bytes`) before a value is
    /// allocated (`allocate`).
    pub(crate) fn new(sh: &Shape) -> Result<SparseSystem, String> {
        let pattern = pattern(sh)?;
        let symbolic = if sh.n == 0 {
            None
        } else {
            let ordering = SymmetricOrdering::Amd;
            let params = CholeskySymbolicParams::default();
            let symbolic = factorize_symbolic_cholesky(pattern.as_ref(), Side::Lower, ordering, params)
                .map_err(|e| format!("the symbolic factorisation of the reduced system failed: {e:?}"))?;
            Some(symbolic)
        };
        Ok(SparseSystem {
            pattern,
            symbolic,
            values: Vec::new(),
            factor: Vec::new(),
            factor_scratch: MemBuffer::try_new(StackReq::empty()).map_err(out_of_memory)?,
            solve_scratch: MemBuffer::try_new(StackReq::empty()).map_err(out_of_memory)?,
        })
    }

    /// The bytes of the values of the factor, what the symbolic factorisation says the numeric
    /// one will take.
    pub(crate) fn factor_bytes(&self) -> u64 {
        self.symbolic.as_ref().map_or(0, |s| s.len_val() as u64 * std::mem::size_of::<f64>() as u64)
    }

    /// Allocates the values of the matrix, the factor and faer's workspace, once (`step` calls it
    /// too); an error when there is no memory for them.
    pub(crate) fn allocate(&mut self) -> Result<(), String> {
        let Some(symbolic) = &self.symbolic else { return Ok(()) };
        if !self.factor.is_empty() {
            return Ok(());
        }
        let par = faer::get_global_parallelism();
        let entries = self.pattern.row_idx().len();
        self.values.try_reserve_exact(entries).map_err(out_of_memory)?;
        self.values.resize(entries, 0.0);
        self.factor.try_reserve_exact(symbolic.len_val()).map_err(out_of_memory)?;
        self.factor.resize(symbolic.len_val(), 0.0);
        let factor_scratch = symbolic.factorize_numeric_llt_scratch::<f64>(par, Default::default());
        self.factor_scratch = MemBuffer::try_new(factor_scratch).map_err(out_of_memory)?;
        let solve_scratch = symbolic.solve_in_place_scratch::<f64>(1, par);
        self.solve_scratch = MemBuffer::try_new(solve_scratch).map_err(out_of_memory)?;
        Ok(())
    }

    /// The nonzeros of the pattern of the reduced system (the lower triangle with whole diagonal
    /// blocks).
    #[cfg(test)]
    pub(crate) fn pattern_entries(&self) -> usize {
        self.pattern.row_idx().len()
    }

    /// The bytes of the value buffers: the values of the matrix, the factor, faer's workspace.
    #[cfg(test)]
    pub(crate) fn allocated_bytes(&self) -> usize {
        let f64s = self.values.capacity() + self.factor.capacity();
        f64s * std::mem::size_of::<f64>() + self.factor_scratch.len() + self.solve_scratch.len()
    }

    /// One step of the damped normal equations, as `schur::dense_step`: the reduced system
    /// assembled into the pattern, factorised and solved by faer's sparse Cholesky, the point
    /// blocks back-substituted. An error names a pivot that is not positive.
    pub(crate) fn step(&mut self, red: &Reduced, g: &[f64], damping: &[f64]) -> Result<Vec<f64>, String> {
        self.allocate()?;
        let sh = red.shape;
        let points = point_inverses(red, g, damping);
        let cx = Context { shape: sh, lin: red.lin, g, damping, points: &points };
        let n = sh.n;
        let mut delta = vec![0.0; n + sh.n_point_cols];
        let SparseSystem { pattern, symbolic, values, factor, factor_scratch, solve_scratch } = self;
        if let Some(symbolic) = symbolic {
            let (_, _, col_ptr, _, row_idx) = pattern.parts();
            values.fill(0.0);
            let mut b = vec![0.0; n];
            {
                let lengths = sh.cam_size[..sh.border_block].iter().copied();
                let (blocks, (border_v, border_b)) = split_blocks(col_ptr, lengths, values, &mut b);
                let scatter = |offset: &mut [u32], rows: &[usize], value: fn(usize) -> u32| {
                    for (k, &row) in rows.iter().enumerate() {
                        offset[row] = value(k);
                    }
                };
                blocks.into_par_iter().enumerate().for_each_init(
                    || vec![NOT_IN_PATTERN; n],
                    |offset, (c, (block, rhs))| {
                        let (start, len) = (sh.cam_col[c], sh.cam_size[c]);
                        if len == 0 {
                            return;
                        }
                        let rows = &row_idx[col_ptr[start]..col_ptr[start + 1]];
                        scatter(offset, rows, |position| position as u32);
                        let slab = Pattern { stride: rows.len(), offset };
                        camera_column(&cx, c, &slab, block, rhs);
                        scatter(offset, rows, |_| NOT_IN_PATTERN);
                    },
                );
                let border = Block { start: sh.border_start, len: n - sh.border_start };
                border_columns(&cx, &border, border_v, border_b);
            }
            let par = faer::get_global_parallelism();
            let matrix = SparseColMatRef::new(pattern.as_ref(), values);
            let stack = MemStack::new(factor_scratch);
            let regularization = LltRegularization::default();
            let llt = symbolic
                .factorize_numeric_llt(factor, matrix, Side::Lower, regularization, par, stack, Default::default())
                .map_err(|PivotError::NonPositivePivot { index }| {
                    format!("pivot {index} of the reduced system is not positive")
                })?;
            let stack = MemStack::new(solve_scratch);
            llt.solve_in_place_with_conj(Conj::No, MatMut::from_column_major_slice_mut(&mut b, n, 1), par, stack);
            delta[..n].copy_from_slice(&b);
        }
        back_substitute(&cx, &mut delta);
        Ok(delta)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bundle::problem::{Obs, Parameters};
    use crate::bundle::residuals::Loss;
    use crate::bundle::schur::dense_step;
    use crate::bundle::schur::tests::{moved, small_block};
    use crate::bundle::solve::testing::system;

    #[test]
    fn the_pattern_couples_a_photo_with_what_it_shares_and_the_border_last() {
        let pb = small_block();
        let sys = system(&pb, Parameters::initial(&pb), Loss::None);
        let sh = &sys.shape;
        // photos of 12, 6 and 12 columns, two markers, a border of 6 intrinsics and 6 biases
        assert_eq!(&sh.cam_size[..5], &[12, 6, 12, 3, 3]);
        assert_eq!((sh.border_start, sh.n), (36, 48));
        let pat = pattern(sh).unwrap();
        let (_, _, col_ptr, _, row_idx) = pat.parts();
        let rows_of = |col: usize| &row_idx[col_ptr[col]..col_ptr[col + 1]];
        let range = |from: usize, to: usize| (from..to).collect::<Vec<_>>();
        // every photo sees every point and both markers: the columns of a photo hold its own
        // rows, those of the photos after it, the two markers and the whole border, in that order
        for i in 0..3 {
            let seen: Vec<usize> = pb.mobs.iter().filter(|o| o.image == i).map(|o| o.point).collect();
            assert_eq!(seen, [0, 1], "photo {i}");
        }
        for col in 0..12 {
            assert_eq!(rows_of(col), &range(0, 48)[..], "column {col}");
        }
        for col in 12..18 {
            assert_eq!(rows_of(col), &range(12, 48)[..], "column {col}");
        }
        for col in 18..30 {
            assert_eq!(rows_of(col), &range(18, 48)[..], "column {col}");
        }
        // the first marker with the second (their bar) and the border; the second with the border
        for col in 30..33 {
            assert_eq!(rows_of(col), &range(30, 48)[..], "column {col}");
        }
        for col in 33..36 {
            assert_eq!(rows_of(col), &range(33, 48)[..], "column {col}");
        }
        // the border block is square
        for col in 36..48 {
            assert_eq!(rows_of(col), &range(36, 48)[..], "column {col}");
        }

        // two photos that share no point are not coupled: the third photo moved away from the
        // first two, with points of its own
        let mut apart = pb.clone();
        apart.centres[2] = [50.0, 0.0, 10.0];
        apart.obs.retain(|o| o.image != 2);
        apart.mobs.retain(|o| o.image != 2);
        for (p, x) in [[49.5, 0.0, 0.0], [50.5, 0.5, 0.0], [50.0, -0.5, 0.1]].into_iter().enumerate() {
            apart.points.push(x);
            let q = apart.points.len() - 1;
            apart.obs.push(Obs { image: 2, point: q, xy: [600.0 + 10.0 * p as f64, 450.0] });
        }
        apart.check().unwrap();
        let sys = system(&apart, Parameters::initial(&apart), Loss::None);
        let pat = pattern(&sys.shape).unwrap();
        let (_, _, col_ptr, _, row_idx) = pat.parts();
        let rows_of = |col: usize| &row_idx[col_ptr[col]..col_ptr[col + 1]];
        let first: Vec<usize> = (0..18).chain(30..48).collect();
        assert_eq!(rows_of(0), &first[..]);
        let third: Vec<usize> = (18..30).chain(36..48).collect();
        assert_eq!(rows_of(18), &third[..], "the third photo sees no marker and shares no point");
    }

    /// What `Auto` builds to judge the size of the factor must not be the factor: `new` allocates
    /// no value buffer; `allocate` allocates them, once.
    #[test]
    fn the_system_is_built_without_its_values_and_allocates_them_once() {
        let pb = small_block();
        let sys = system(&pb, Parameters::initial(&pb), Loss::None);
        let mut system = SparseSystem::new(&sys.shape).unwrap();
        assert!(system.factor_bytes() > 0 && system.pattern_entries() > 0);
        assert_eq!(system.allocated_bytes(), 0, "{} bytes of values allocated by new", system.allocated_bytes());
        system.allocate().unwrap();
        let allocated = system.allocated_bytes();
        let least = system.factor_bytes() as usize + 8 * system.pattern_entries();
        assert!(allocated >= least, "{allocated} bytes for a factor and a pattern of {least}");
        system.allocate().unwrap();
        assert_eq!(system.allocated_bytes(), allocated, "allocated twice");
    }

    fn sparse_step_equals_the_dense_step(loss: Loss, lambda: f64) {
        let pb = small_block();
        let sys = system(&pb, moved(&pb), loss);
        let red = sys.reduced();
        let damping = sys.damping(lambda);
        let dense = dense_step(&red, &sys.g, &damping).unwrap();
        let mut system = SparseSystem::new(&sys.shape).unwrap();
        assert!(system.factor_bytes() > 0 && system.pattern_entries() < 48 * 48);
        let sparse = system.step(&red, &sys.g, &damping).unwrap();
        let scale = dense.iter().fold(0.0, |a: f64, x| a.max(x.abs()));
        assert!(scale > 1e-3);
        for (k, (got, want)) in sparse.iter().zip(&dense).enumerate() {
            assert!((got - want).abs() <= 1e-9 * scale, "column {k}: {got} vs {want} ({loss:?}, lambda {lambda})");
        }
        // the same system again, with another damping: the buffers are reused
        let damping: Vec<f64> = damping.iter().map(|x| 10.0 * x).collect();
        let dense = dense_step(&red, &sys.g, &damping).unwrap();
        let sparse = system.step(&red, &sys.g, &damping).unwrap();
        for (k, (got, want)) in sparse.iter().zip(&dense).enumerate() {
            assert!((got - want).abs() <= 1e-9 * scale, "column {k} again: {got} vs {want}");
        }
    }

    #[test]
    fn the_sparse_step_is_the_dense_step() {
        sparse_step_equals_the_dense_step(Loss::None, 1e-4);
        sparse_step_equals_the_dense_step(Loss::Huber(0.5), 1e-2);
        sparse_step_equals_the_dense_step(Loss::Cauchy(0.3), 1.0);
    }

    #[test]
    fn a_pivot_that_is_not_positive_is_named_and_an_empty_system_is_solved() {
        let mut pb = small_block();
        // every photo fixed, no rolling shutter and no bias: nothing but points, an empty reduced system
        pb.fixed_images = vec![true; 3];
        pb.rolling_shutter = false;
        pb.gnss_bias = false;
        pb.cameras.iter_mut().for_each(|c| c.free.clear());
        pb.markers.clear();
        pb.mobs.clear();
        pb.gcps.clear();
        pb.bars.clear();
        let mut params = Parameters::initial(&pb);
        for (k, p) in params.points.iter_mut().enumerate() {
            p[1] += 0.03 * (k % 3) as f64 - 0.02;
        }
        let sys = system(&pb, params, Loss::None);
        assert_eq!(sys.shape.n, 0);
        let mut sparse_system = SparseSystem::new(&sys.shape).unwrap();
        assert_eq!(sparse_system.factor_bytes(), 0);
        let damping = sys.damping(1e-4);
        let dense = dense_step(&sys.reduced(), &sys.g, &damping).unwrap();
        let sparse = sparse_system.step(&sys.reduced(), &sys.g, &damping).unwrap();
        assert_eq!(sparse, dense);
        assert!(sparse.iter().any(|x| *x != 0.0));

        // a negative damping makes the reduced system indefinite: the pivot is named, as the
        // dense factorisation names it
        let pb = small_block();
        let sys = system(&pb, moved(&pb), Loss::None);
        let damping = sys.damping(-10.0);
        let mut sparse_system = SparseSystem::new(&sys.shape).unwrap();
        let error = sparse_system.step(&sys.reduced(), &sys.g, &damping).unwrap_err();
        assert!(error.starts_with("pivot ") && error.ends_with(" of the reduced system is not positive"), "{error}");
        assert!(dense_step(&sys.reduced(), &sys.g, &damping).is_err());
    }
}
