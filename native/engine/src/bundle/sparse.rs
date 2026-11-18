//! The reduced camera system stored sparse: a photo couples with the photos it shares points
//! with, with the markers it observes and with the border; a marker with the markers it shares
//! a bar with and with the border; the border columns last. The lower triangle is kept in
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

use super::problem::{Layout, Problem};
use super::residuals::{Evaluated, Structure};
use super::schur::{
    back_substitute, border_columns, marker_column, photo_column, point_blocks, Block, Context, Reduced, Slab,
};

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

/// The rows of the column block of the photo `i`: its own, those of the photos after it that
/// share a point with it, of the markers it observes, of the border. `seen` is a scratch of one
/// entry per photo, stamped with `i`.
fn photo_rows(pb: &Problem, st: &Structure, red: &Reduced, i: usize, seen: &mut [usize]) -> Vec<usize> {
    let (s_i, c_i) = (red.photo_start[i], red.photo_len[i]);
    let mut rows = Vec::new();
    if c_i == 0 {
        return rows;
    }
    rows.extend(s_i..s_i + c_i);
    let mut after = Vec::new();
    for &j in st.image_obs.of(i) {
        for &j2 in st.point_obs.of(pb.obs[j].point) {
            let i2 = pb.obs[j2].image;
            if i2 > i && red.photo_len[i2] > 0 && seen[i2] != i {
                seen[i2] = i;
                after.push(i2);
            }
        }
    }
    after.sort_unstable();
    for i2 in after {
        rows.extend(red.photo_start[i2]..red.photo_start[i2] + red.photo_len[i2]);
    }
    let mut markers: Vec<usize> = st.image_mobs.of(i).iter().map(|&j| pb.mobs[j].point).collect();
    markers.sort_unstable();
    markers.dedup();
    for m in markers {
        rows.extend(red.marker_start + 3 * m..red.marker_start + 3 * m + 3);
    }
    rows.extend(red.border_start..red.size);
    rows
}

/// The rows of the three columns of the marker `m`: its own, those of the markers after it that
/// share a bar with it, the border.
fn marker_rows(pb: &Problem, st: &Structure, red: &Reduced, m: usize) -> Vec<usize> {
    let s_m = red.marker_start + 3 * m;
    let mut rows: Vec<usize> = (s_m..s_m + 3).collect();
    let mut others = Vec::new();
    for &end in st.marker_bars.of(m) {
        let bar = &pb.bars[end / 2];
        let other = if end % 2 == 0 { bar.1 } else { bar.0 };
        if other > m {
            others.push(other);
        }
    }
    others.sort_unstable();
    others.dedup();
    for other in others {
        rows.extend(red.marker_start + 3 * other..red.marker_start + 3 * other + 3);
    }
    rows.extend(red.border_start..red.size);
    rows
}

/// The pattern of the lower triangle (with the whole diagonal block of every column block, of
/// which faer reads the lower part), in compressed columns; an error when there is no memory for
/// its arrays (the row lists of the blocks, one per block, are small and not reserved that way).
fn pattern(pb: &Problem, st: &Structure, red: &Reduced) -> Result<SymbolicSparseColMat<usize>, String> {
    let n = red.size;
    let photos: Vec<Vec<usize>> = (0..pb.centres.len())
        .into_par_iter()
        .map_init(|| vec![usize::MAX; pb.centres.len()], |seen, i| photo_rows(pb, st, red, i, seen))
        .collect();
    let markers: Vec<Vec<usize>> = (0..pb.markers.len()).into_par_iter().map(|m| marker_rows(pb, st, red, m)).collect();
    let border: Vec<usize> = (red.border_start..n).collect();
    let blocks = || {
        photos
            .iter()
            .zip(&red.photo_len)
            .map(|(rows, &len)| (rows, len))
            .chain(markers.iter().map(|rows| (rows, 3)))
            .chain(std::iter::once((&border, n - red.border_start)))
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
    /// The pattern of `red` and its symbolic factorisation, and nothing more: what `Auto` builds
    /// to judge the size of the factor (`factor_bytes`) before a value is allocated (`allocate`).
    pub(crate) fn new(pb: &Problem, st: &Structure, red: &Reduced) -> Result<SparseSystem, String> {
        let pattern = pattern(pb, st, red)?;
        let symbolic = if red.size == 0 {
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
    /// assembled into the pattern, factorised and solved by faer's sparse Cholesky, the points
    /// back-substituted. An error names a pivot that is not positive.
    pub(crate) fn step(
        &mut self,
        pb: &Problem,
        lay: &Layout,
        st: &Structure,
        red: &Reduced,
        ev: &Evaluated,
        g: &[f64],
        damping: &[f64],
    ) -> Result<Vec<f64>, String> {
        self.allocate()?;
        let points = point_blocks(pb, lay, st, ev, g, damping);
        let cx = Context { pb, lay, st, red, ev, g, damping, points: &points };
        let n = red.size;
        let mut delta = vec![0.0; lay.size];
        let SparseSystem { pattern, symbolic, values, factor, factor_scratch, solve_scratch } = self;
        if let Some(symbolic) = symbolic {
            let (_, _, col_ptr, _, row_idx) = pattern.parts();
            values.fill(0.0);
            let mut b = vec![0.0; n];
            {
                let lengths = red.photo_len.iter().copied().chain(std::iter::repeat_n(3, pb.markers.len()));
                let (blocks, (border_v, border_b)) = split_blocks(col_ptr, lengths, values, &mut b);
                // the rows of a block are those of its first column; a thread scatters their
                // positions before assembling the block and clears them after it
                let scatter = |offset: &mut [u32], rows: &[usize], value: fn(usize) -> u32| {
                    for (k, &row) in rows.iter().enumerate() {
                        offset[row] = value(k);
                    }
                };
                blocks.into_par_iter().enumerate().for_each_init(
                    || vec![NOT_IN_PATTERN; n],
                    |offset, (k, (block, rhs))| {
                        let n_photos = pb.centres.len();
                        let (start, len) = if k < n_photos {
                            (red.photo_start[k], red.photo_len[k])
                        } else {
                            (red.marker_start + 3 * (k - n_photos), 3)
                        };
                        if len == 0 {
                            return;
                        }
                        let rows = &row_idx[col_ptr[start]..col_ptr[start + 1]];
                        scatter(offset, rows, |position| position as u32);
                        let slab = Pattern { stride: rows.len(), offset };
                        if k < n_photos {
                            photo_column(&cx, k, &slab, block, rhs);
                        } else {
                            marker_column(&cx, k - n_photos, &slab, block, rhs);
                        }
                        scatter(offset, rows, |_| NOT_IN_PATTERN);
                    },
                );
                let border = Block { start: red.border_start, len: n - red.border_start };
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
            for (col, &lc) in red.layout_col.iter().enumerate() {
                delta[lc] = b[col];
            }
        }
        back_substitute(&cx, &mut delta);
        Ok(delta)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bundle::problem::Obs;
    use crate::bundle::residuals::{evaluate_blocks, gradient_and_diagonal, Loss};
    use crate::bundle::schur::dense_step;
    use crate::bundle::schur::tests::{moved, small_block};

    #[test]
    fn the_pattern_couples_a_photo_with_what_it_shares_and_the_border_last() {
        let pb = small_block();
        let lay = Layout::new(&pb);
        let st = Structure::new(&pb);
        let red = Reduced::new(&pb, &lay);
        // photos of 12, 6 and 12 columns, two markers, a border of 6 intrinsics and 6 biases
        assert_eq!(red.photo_len, vec![12, 6, 12]);
        assert_eq!((red.marker_start, red.border_start, red.size), (30, 36, 48));
        let pat = pattern(&pb, &st, &red).unwrap();
        let (_, _, col_ptr, _, row_idx) = pat.parts();
        let rows_of = |col: usize| &row_idx[col_ptr[col]..col_ptr[col + 1]];
        let range = |from: usize, to: usize| (from..to).collect::<Vec<_>>();
        // every photo sees every point and both markers: the columns of a photo hold its own
        // rows, those of the photos after it, the two markers and the whole border, in that order
        for i in 0..3 {
            let seen: Vec<usize> = st.image_mobs.of(i).iter().map(|&j| pb.mobs[j].point).collect();
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
        let st = Structure::new(&apart);
        let red = Reduced::new(&apart, &Layout::new(&apart));
        let pat = pattern(&apart, &st, &red).unwrap();
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
        let st = Structure::new(&pb);
        let red = Reduced::new(&pb, &Layout::new(&pb));
        let mut system = SparseSystem::new(&pb, &st, &red).unwrap();
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
        let lay = Layout::new(&pb);
        let st = Structure::new(&pb);
        let red = Reduced::new(&pb, &lay);
        let params = moved(&pb);
        let ev = evaluate_blocks(&pb, &lay, &st, &params, loss, true);
        let (g, d) = gradient_and_diagonal(&pb, &lay, &st, &ev);
        let damping: Vec<f64> = d.iter().map(|x| lambda * x.max(1e-6)).collect();
        let dense = dense_step(&pb, &lay, &st, &red, &ev, &g, &damping).unwrap();
        let mut system = SparseSystem::new(&pb, &st, &red).unwrap();
        assert!(system.factor_bytes() > 0 && system.pattern_entries() < 48 * 48);
        let sparse = system.step(&pb, &lay, &st, &red, &ev, &g, &damping).unwrap();
        let scale = dense.iter().fold(0.0, |a: f64, x| a.max(x.abs()));
        assert!(scale > 1e-3);
        for (k, (got, want)) in sparse.iter().zip(&dense).enumerate() {
            assert!((got - want).abs() <= 1e-9 * scale, "column {k}: {got} vs {want} ({loss:?}, lambda {lambda})");
        }
        // the same system again, with another damping: the buffers are reused
        let damping: Vec<f64> = damping.iter().map(|x| 10.0 * x).collect();
        let dense = dense_step(&pb, &lay, &st, &red, &ev, &g, &damping).unwrap();
        let sparse = system.step(&pb, &lay, &st, &red, &ev, &g, &damping).unwrap();
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
        let lay = Layout::new(&pb);
        let st = Structure::new(&pb);
        let red = Reduced::new(&pb, &lay);
        assert_eq!(red.size, 0);
        let mut system = SparseSystem::new(&pb, &st, &red).unwrap();
        assert_eq!(system.factor_bytes(), 0);
        let mut params = crate::bundle::problem::Parameters::initial(&pb);
        for (k, p) in params.points.iter_mut().enumerate() {
            p[1] += 0.03 * (k % 3) as f64 - 0.02;
        }
        let ev = evaluate_blocks(&pb, &lay, &st, &params, Loss::None, true);
        let (g, d) = gradient_and_diagonal(&pb, &lay, &st, &ev);
        let damping: Vec<f64> = d.iter().map(|x| 1e-4 * x.max(1e-6)).collect();
        let dense = dense_step(&pb, &lay, &st, &red, &ev, &g, &damping).unwrap();
        let sparse = system.step(&pb, &lay, &st, &red, &ev, &g, &damping).unwrap();
        assert_eq!(sparse, dense);
        assert!(sparse.iter().any(|x| *x != 0.0));

        // a negative damping makes the reduced system indefinite: the pivot is named, as the
        // dense factorisation names it
        let pb = small_block();
        let lay = Layout::new(&pb);
        let st = Structure::new(&pb);
        let red = Reduced::new(&pb, &lay);
        let params = moved(&pb);
        let ev = evaluate_blocks(&pb, &lay, &st, &params, Loss::None, true);
        let (g, d) = gradient_and_diagonal(&pb, &lay, &st, &ev);
        let damping: Vec<f64> = d.iter().map(|x| -10.0 * x.max(1e-6)).collect();
        let mut system = SparseSystem::new(&pb, &st, &red).unwrap();
        let error = system.step(&pb, &lay, &st, &red, &ev, &g, &damping).unwrap_err();
        assert!(error.starts_with("pivot ") && error.ends_with(" of the reduced system is not positive"), "{error}");
        assert!(dense_step(&pb, &lay, &st, &red, &ev, &g, &damping).is_err());
    }
}
