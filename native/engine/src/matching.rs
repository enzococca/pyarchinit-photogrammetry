//! The descriptor matching on the CPU: the twin of the GPU kernels of `gpu`, with the same answer
//! bit for bit on any computer. The dot products of two descriptors of 128 bytes are integers
//! below 2^24, which the kernels compute exactly in f32 and this module as integers, so a project
//! matched on no GPU, or on the CPU and a GPU together, gets one answer. Ties are broken as the
//! kernels break them: the lowest index wins, in both directions, and on a tie the second best
//! equals the best - so that COLMAP's ratio test refuses a descriptor that two others match
//! equally well (a naive twin that keeps the second best strictly below the best would accept
//! it).

use rayon::prelude::*;

use crate::gpu::{select_matches, DIM};

/// The integer dot product of two descriptors.
#[inline]
fn dot(x: &[u8; DIM], y: &[u8; DIM]) -> u32 {
    x.iter().zip(y).map(|(&p, &q)| u32::from(p) * u32::from(q)).sum()
}

/// `(best, index, second)` of `query` over the descriptors of `set`, as a thread of the kernel
/// scans them: indices upward, a strictly larger score becomes the best (so the lowest index
/// wins a tie), the best it replaces or an equal score the second best; nothing seen is -1 and 0
/// once written out, which is also what a score of 0 gives.
fn scan(query: &[u8; DIM], set: &[u8]) -> [u32; 3] {
    let (mut best, mut second, mut index) = (-1i64, -1i64, 0u32);
    for (j, candidate) in set.chunks_exact(DIM).enumerate() {
        let candidate: &[u8; DIM] = candidate.try_into().expect("a chunk of DIM bytes");
        let score = i64::from(dot(query, candidate));
        if score > best {
            second = best;
            best = score;
            index = j as u32;
        } else if score > second {
            second = score;
        }
    }
    [best.max(0) as u32, index, second.max(0) as u32]
}

/// The best and second-best dot products of `a` (n_a x 128 bytes) and `b` (n_b x 128 bytes), in
/// the layout of [`crate::gpu::Matcher::bests`]: `(best, index, second)` for every descriptor of
/// A over B, then for every descriptor of B over A; nothing when either set is empty. Ties: the
/// lowest index wins, the second best of a tie equals the best. The rows are shared out among
/// the threads of rayon.
///
/// # Panics
///
/// When the length of a set is not a multiple of 128.
pub fn bests_cpu(a: &[u8], b: &[u8]) -> Vec<[u32; 3]> {
    assert!(a.len() % DIM == 0 && b.len() % DIM == 0, "descriptors are {DIM} bytes each");
    if a.is_empty() || b.is_empty() {
        return Vec::new();
    }
    let mut out: Vec<[u32; 3]> =
        a.par_chunks_exact(DIM).map(|row| scan(row.try_into().expect("a row of DIM bytes"), b)).collect();
    out.par_extend(b.par_chunks_exact(DIM).map(|column| scan(column.try_into().expect("a column of DIM bytes"), a)));
    out
}

/// COLMAP's matches of the pair on the CPU, as [`crate::gpu::Matcher::matches`] gives them on a
/// GPU: the bests of [`bests_cpu`] through [`select_matches`] (the ratio test, the distance test
/// and, with `cross_check`, the same from B to A).
pub fn matches_cpu(a: &[u8], b: &[u8], max_ratio: f32, max_distance: f32, cross_check: bool) -> Vec<[u32; 2]> {
    let bests = bests_cpu(a, b);
    select_matches(&bests, a.len() / DIM, b.len() / DIM, max_ratio, max_distance, cross_check)
}
