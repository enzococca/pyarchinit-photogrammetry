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

/// The integer dot product of two descriptors (at most 128 x 255^2, below 2^24). Written so that
/// the compiler vectorises it: the products of bytes summed into lanes of 32 bits, which LLVM
/// turns into `udot` where the dot product instructions are on at compile time (every Apple
/// computer: `aarch64-apple-darwin` enables `dotprod`), `umull`/`uadalp` on other ARM CPUs and
/// `pmaddwd` on x86_64. The loops that call it for many descriptors are also compiled a second
/// time for AVX2 on x86_64 and for `dotprod` on ARM CPUs that have it without the target saying
/// so ([`dispatch`], `dispatched!`), chosen at run time.
#[inline(always)]
pub fn dot(x: &[u8; DIM], y: &[u8; DIM]) -> u32 {
    x.iter().zip(y).map(|(&p, &q)| u32::from(p) * u32::from(q)).sum()
}

/// The CPU features the copies of a loop of dot products were compiled for, best first.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kernel {
    /// AVX2 on x86_64.
    Avx2,
    /// The dot product instructions of an ARM CPU whose target does not enable them.
    DotProd,
    /// What the target enables.
    Portable,
}

/// The best copy of the loops of dot products for this CPU.
pub fn dispatch() -> Kernel {
    #[cfg(target_arch = "x86_64")]
    if std::arch::is_x86_feature_detected!("avx2") {
        return Kernel::Avx2;
    }
    #[cfg(all(target_arch = "aarch64", not(target_feature = "dotprod")))]
    if std::arch::is_aarch64_feature_detected!("dotprod") {
        return Kernel::DotProd;
    }
    Kernel::Portable
}

/// `fn $name(kernel: Kernel, args) -> R`, which runs the `#[inline(always)]` function `$body` on
/// the arguments in a copy compiled with the features of `kernel`, so that its loops of dot
/// products are vectorised for them; a kernel this CPU or target has not runs the portable copy.
#[macro_export]
#[doc(hidden)]
macro_rules! dispatched {
    ($(#[$meta:meta])* $vis:vis fn $name:ident($($arg:ident: $ty:ty),* $(,)?) -> $ret:ty = $body:ident;) => {
        $(#[$meta])*
        $vis fn $name(kernel: $crate::matching::Kernel, $($arg: $ty),*) -> $ret {
            #[cfg(target_arch = "x86_64")]
            #[target_feature(enable = "avx2")]
            unsafe fn avx2($($arg: $ty),*) -> $ret {
                $body($($arg),*)
            }
            #[cfg(all(target_arch = "aarch64", not(target_feature = "dotprod")))]
            #[target_feature(enable = "dotprod")]
            unsafe fn dotprod($($arg: $ty),*) -> $ret {
                $body($($arg),*)
            }
            match kernel {
                // SAFETY: the guard checks that the CPU has the features of the copy
                #[cfg(target_arch = "x86_64")]
                $crate::matching::Kernel::Avx2 if std::arch::is_x86_feature_detected!("avx2") => unsafe {
                    avx2($($arg),*)
                },
                #[cfg(all(target_arch = "aarch64", not(target_feature = "dotprod")))]
                $crate::matching::Kernel::DotProd if std::arch::is_aarch64_feature_detected!("dotprod") => unsafe {
                    dotprod($($arg),*)
                },
                _ => $body($($arg),*),
            }
        }
    };
}

crate::dispatched! {
    /// `(best, index, second)` of `query` over the descriptors of `set`, as a thread of the kernel
    /// scans them: indices upward, a strictly larger score becomes the best (so the lowest index
    /// wins a tie), the best it replaces or an equal score the second best; nothing seen is -1 and 0
    /// once written out, which is also what a score of 0 gives.
    fn scan(query: &[u8; DIM], set: &[u8]) -> [u32; 3] = scan_body;
}

#[inline(always)]
fn scan_body(query: &[u8; DIM], set: &[u8]) -> [u32; 3] {
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
    let kernel = dispatch();
    let mut out: Vec<[u32; 3]> =
        a.par_chunks_exact(DIM).map(|row| scan(kernel, row.try_into().expect("a row of DIM bytes"), b)).collect();
    out.par_extend(
        b.par_chunks_exact(DIM).map(|column| scan(kernel, column.try_into().expect("a column of DIM bytes"), a)));
    out
}

/// COLMAP's matches of the pair on the CPU, as [`crate::gpu::Matcher::matches`] gives them on a
/// GPU: the bests of [`bests_cpu`] through [`select_matches`] (the ratio test, the distance test
/// and, with `cross_check`, the same from B to A).
pub fn matches_cpu(a: &[u8], b: &[u8], max_ratio: f32, max_distance: f32, cross_check: bool) -> Vec<[u32; 2]> {
    let bests = bests_cpu(a, b);
    select_matches(&bests, a.len() / DIM, b.len() / DIM, max_ratio, max_distance, cross_check)
}

/// The bests of every pair of two blocks of photos, in the layout of
/// [`crate::gpu::Matcher::bests_block`]: the entry `i * b.len() + j` is [`bests_cpu`] of `a[i]`
/// and `b[j]` (nothing for a pair with an empty photo). The pairs are shared out among the
/// threads of rayon, and the rows of each pair among them again.
///
/// # Panics
///
/// When the length of a photo is not a multiple of 128, as [`bests_cpu`] panics where
/// [`crate::gpu::Matcher::bests`] gives an error: the twins of the CPU take whole descriptors
/// for granted, the GPU ones check before they bind a buffer.
pub fn bests_block_cpu(a: &[&[u8]], b: &[&[u8]]) -> Vec<Vec<[u32; 3]>> {
    let pairs: Vec<(usize, usize)> = (0..a.len()).flat_map(|i| (0..b.len()).map(move |j| (i, j))).collect();
    pairs.par_iter().map(|&(i, j)| bests_cpu(a[i], b[j])).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The dot product one byte at a time, as plain integers.
    fn plain(x: &[u8], y: &[u8]) -> u32 {
        let mut sum = 0u32;
        for k in 0..DIM {
            sum += u32::from(x[k]) * u32::from(y[k]);
        }
        sum
    }

    fn random_bytes(seed: u64, n: usize) -> Vec<u8> {
        let mut state = seed;
        (0..n)
            .map(|_| {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                (state >> 56) as u8
            })
            .collect()
    }

    #[test]
    fn the_dot_product_is_exact_on_every_kernel() {
        // random descriptors, then the extremes: all 255 (128 x 255^2, the largest score), all 0,
        // and 255 against 0 and 1
        let mut set = random_bytes(1, 300 * DIM);
        set.extend(std::iter::repeat(255u8).take(DIM));
        set.extend(std::iter::repeat(0u8).take(DIM));
        set.extend((0..DIM).map(|k| if k % 2 == 0 { 255 } else { 0 }));
        set.extend((0..DIM).map(|k| if k % 2 == 0 { 1 } else { 255 }));
        let n = set.len() / DIM;
        let at = |i: usize| -> &[u8; DIM] { set[i * DIM..(i + 1) * DIM].try_into().unwrap() };
        assert_eq!(dot(at(300), at(300)), 128 * 255 * 255);
        for i in 0..n {
            for j in 0..n {
                assert_eq!(dot(at(i), at(j)), plain(at(i), at(j)), "({i}, {j})");
            }
        }
        // the scans of every kernel this computer has against the one the target enables
        let kernels = [Kernel::Portable, Kernel::Avx2, Kernel::DotProd];
        println!("this CPU: {:?}", dispatch());
        for i in 0..n {
            let want = scan(Kernel::Portable, at(i), &set);
            let mut naive = (-1i64, 0u32, -1i64);
            for j in 0..n {
                let score = i64::from(plain(at(i), at(j)));
                if score > naive.0 {
                    naive = (score, j as u32, naive.0);
                } else if score > naive.2 {
                    naive.2 = score;
                }
            }
            assert_eq!(want, [naive.0.max(0) as u32, naive.1, naive.2.max(0) as u32], "row {i}");
            for kernel in kernels {
                assert_eq!(scan(kernel, at(i), &set), want, "row {i}, {kernel:?}");
            }
        }
    }
}
