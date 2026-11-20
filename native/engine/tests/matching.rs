//! The descriptor matching, exact on every device: the CPU twin (`matching`) against a brute
//! force written another way, and the GPU matcher (`gpu`) on both of its tile sizes against the
//! CPU twin, bit for bit, ties included. The descriptors are built as
//! `tests/test_native.py::test_gpu_matching_agrees_with_the_brute_force` builds them: 2000 of A
//! and 1700 of B, the first 900 of B noisy copies of the first 900 of A, plus duplicated
//! descriptors in both sets so that ties occur in both directions, across tiles and across the
//! threads of a workgroup.

use ppg_engine::gpu::{self, select_matches, Matcher, DIM};
use ppg_engine::matching;

const N_A: usize = 2000;
const N_B: usize = 1700;
const COPIES: usize = 900;

/// The norm of a descriptor before it is rounded to bytes: COLMAP's.
const NORM: f64 = 512.0;

/// Knuth's MMIX linear congruential generator, written here so that the descriptors depend on no
/// crate and on no version of one (as in ppg-bench).
struct Lcg(u64);

impl Lcg {
    fn bits(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        self.0
    }

    /// A value in [0, 1): the upper 53 bits, as many as an f64 holds.
    fn unit(&mut self) -> f64 {
        (self.bits() >> 11) as f64 / (1u64 << 53) as f64
    }

    /// A whole number in [low, high], both included, out of the upper 32 bits.
    fn between(&mut self, low: i32, high: i32) -> i32 {
        let span = (high - low + 1) as u64;
        low + (((self.bits() >> 32) * span) >> 32) as i32
    }
}

/// `n` descriptors as SIFT's bytes: the squares of uniform values (most components small, a few
/// large), a norm of `NORM`, each component rounded and clipped to 255.
fn descriptors(n: usize, random: &mut Lcg) -> Vec<u8> {
    let mut out = Vec::with_capacity(n * DIM);
    for _ in 0..n {
        let values: Vec<f64> = (0..DIM).map(|_| random.unit().powi(2)).collect();
        let norm = values.iter().map(|v| v * v).sum::<f64>().sqrt();
        out.extend(values.iter().map(|v| (v / norm * NORM).round().clamp(0.0, 255.0) as u8));
    }
    out
}

/// The descriptor `from` of `set` copied over the descriptor `to`.
fn duplicate(set: &mut [u8], from: usize, to: usize) {
    let source: Vec<u8> = set[from * DIM..(from + 1) * DIM].to_vec();
    set[to * DIM..(to + 1) * DIM].copy_from_slice(&source);
}

/// The two sets: A of `N_A`, B of `N_B` whose first `COPIES` are the first of A moved by noise in
/// [-6, 6] on every byte. Then the ties: in B, descriptors 64, 135 and 1699 are descriptor 5
/// again (three duplicated columns: in other tiles than their original for either tile size, and
/// 64 in a lower thread column of the workgroup - 64 falls in the first thread column with
/// either width, 5 in a later one: the second with the large tiles, the third with the small);
/// in A, descriptors 70, 1500 and 1999 are descriptor 3 again
/// (three duplicated rows, in other tiles).
fn pair() -> (Vec<u8>, Vec<u8>) {
    let mut random = Lcg(2026);
    let mut a = descriptors(N_A, &mut random);
    let mut b = descriptors(N_B, &mut random);
    for k in 0..COPIES * DIM {
        b[k] = (i32::from(a[k]) + random.between(-6, 6)).clamp(0, 255) as u8;
    }
    duplicate(&mut b, 5, 64);
    duplicate(&mut b, 5, 135);
    duplicate(&mut b, 5, 1699);
    duplicate(&mut a, 3, 70);
    duplicate(&mut a, 3, 1500);
    duplicate(&mut a, 3, 1999);
    (a, b)
}

/// The first `n_a` descriptors of `a` and the first `n_b` of `b`.
fn heads(a: &[u8], b: &[u8], n_a: usize, n_b: usize) -> (Vec<u8>, Vec<u8>) {
    (a[..n_a * DIM].to_vec(), b[..n_b * DIM].to_vec())
}

/// The sizes a tile edge or an empty set could get wrong, beside the full pair: one descriptor
/// against sets around the tile widths of both kernels, and nothing against something.
fn edge_cases(a: &[u8], b: &[u8]) -> Vec<(String, Vec<u8>, Vec<u8>)> {
    let mut cases = Vec::new();
    for (n_a, n_b) in [(1, 63), (1, 65), (1, 129), (63, 1), (65, 1), (129, 1), (31, 33), (0, 100), (100, 0)] {
        let (x, y) = heads(a, b, n_a, n_b);
        cases.push((format!("{n_a} x {n_b}"), x, y));
    }
    cases
}

/// The bests of both directions written another way than a scan: the full table of integer
/// scores, then for every line its largest value, the first position of it, and the second
/// element of the line sorted downward - equal to the largest on a tie, 0 when the line has one
/// element.
fn naive(a: &[u8], b: &[u8]) -> Vec<[u32; 3]> {
    let (n_a, n_b) = (a.len() / DIM, b.len() / DIM);
    if n_a == 0 || n_b == 0 {
        return Vec::new();
    }
    let mut table = vec![vec![0u32; n_b]; n_a];
    for (i, row) in table.iter_mut().enumerate() {
        for (j, cell) in row.iter_mut().enumerate() {
            *cell = (0..DIM).map(|k| u32::from(a[i * DIM + k]) * u32::from(b[j * DIM + k])).sum();
        }
    }
    let line = |scores: Vec<u32>| -> [u32; 3] {
        let best = *scores.iter().max().expect("a line has an element");
        let index = scores.iter().position(|&s| s == best).expect("the best is in the line") as u32;
        let mut sorted = scores;
        sorted.sort_unstable_by(|x, y| y.cmp(x));
        [best, index, sorted.get(1).copied().unwrap_or(0)]
    };
    let mut out: Vec<[u32; 3]> = table.iter().map(|row| line(row.clone())).collect();
    out.extend((0..n_b).map(|j| line(table.iter().map(|row| row[j]).collect())));
    out
}

/// `got` equals `want`, or the first entry that differs is named: the direction, the index, both
/// triples.
fn same(got: &[[u32; 3]], want: &[[u32; 3]], n_a: usize, what: &str) {
    assert_eq!(got.len(), want.len(), "{what}: {} bests instead of {}", got.len(), want.len());
    for (k, (g, w)) in got.iter().zip(want).enumerate() {
        if g != w {
            let (direction, index) = if k < n_a { ("descriptor", k) } else { ("descriptor of B", k - n_a) };
            panic!("{what}: {direction} {index} has (best, index, second) {g:?} instead of {w:?}");
        }
    }
}

#[test]
fn cpu_bests_are_the_brute_force() {
    let (a, b) = pair();
    let got = matching::bests_cpu(&a, &b);
    assert_eq!(got.len(), N_A + N_B);
    same(&got, &naive(&a, &b), N_A, "the full pair");
    // the ties were forced: descriptor 5 of A sees its copy in B four times, descriptor 3 of B
    // sees descriptor 3 of A four times; the lowest index wins and the second best equals the best
    assert_eq!(got[5][1], 5, "descriptor 5 of A: {:?}", got[5]);
    assert_eq!(got[5][0], got[5][2], "descriptor 5 of A: {:?}", got[5]);
    assert_eq!(got[N_A + 3][1], 3, "descriptor 3 of B: {:?}", got[N_A + 3]);
    assert_eq!(got[N_A + 3][0], got[N_A + 3][2], "descriptor 3 of B: {:?}", got[N_A + 3]);
    for (name, x, y) in edge_cases(&a, &b) {
        same(&matching::bests_cpu(&x, &y), &naive(&x, &y), x.len() / DIM, &name);
    }
    // descriptors of nothing but zeros: every score 0, index 0, second 0
    let zeros = vec![0u8; 3 * DIM];
    assert_eq!(matching::bests_cpu(&zeros[..2 * DIM], &zeros), vec![[0, 0, 0]; 5]);
}

#[test]
fn cpu_matches_are_select_matches_over_the_cpu_bests() {
    let (a, b) = pair();
    for cross in [true, false] {
        let got = matching::matches_cpu(&a, &b, 0.8, 0.7, cross);
        let want = select_matches(&matching::bests_cpu(&a, &b), N_A, N_B, 0.8, 0.7, cross);
        assert_eq!(got, want, "cross check {cross}");
        // the noisy copies are found: 900 less what the noise and the forced ties take away
        let copies = got.iter().filter(|m| m[0] == m[1] && (m[0] as usize) < COPIES).count();
        assert!(copies >= 850, "{copies} of the {COPIES} copies matched (cross check {cross})");
        // the tie: descriptor 5 of A has four equal bests in B, the ratio test refuses it
        assert!(!got.iter().any(|m| m[0] == 5), "descriptor 5 of A matched through a tie");
    }
    assert!(matching::matches_cpu(&a[..0], &b, 0.8, 0.7, true).is_empty());
}

#[test]
fn gpu_small_and_large_tiles_equal_the_cpu_twin() {
    let adapters = gpu::adapters();
    if adapters.is_empty() {
        eprintln!("no GPU adapter: the kernels are not compared");
        return;
    }
    let (a, b) = pair();
    let mut cases = vec![(format!("{N_A} x {N_B}"), a.clone(), b.clone())];
    cases.extend(edge_cases(&a, &b));
    let twins: Vec<Vec<[u32; 3]>> = cases.iter().map(|(_, x, y)| matching::bests_cpu(x, y)).collect();
    for adapter in &adapters {
        // the most workgroup memory the kernels use on this adapter, as the matcher chooses it
        let most = match Matcher::new(Some(adapter.index)) {
            Ok(matcher) => matcher.workgroup_bytes(),
            Err(problem) => {
                // an adapter that opens no device (a GL one of a runner) is reported and left out
                eprintln!("adapter {} ({}) skipped: {problem}", adapter.index, adapter.name);
                continue;
            }
        };
        assert!(most == 16384 || most == 32768, "adapter {}: {most} bytes of workgroup memory", adapter.index);
        let too_little = Matcher::with_workgroup_bytes(Some(adapter.index), 8192).err();
        assert!(too_little.as_ref().is_some_and(|p| p.contains("16384")), "8192 bytes: {too_little:?}");
        for bytes in [16384u32, 32768].into_iter().filter(|&bytes| bytes <= most) {
            let mut matcher = Matcher::with_workgroup_bytes(Some(adapter.index), bytes)
                .unwrap_or_else(|p| panic!("adapter {} with {bytes} bytes: {p}", adapter.index));
            assert_eq!(matcher.workgroup_bytes(), bytes);
            for ((name, x, y), twin) in cases.iter().zip(&twins) {
                let got = matcher.bests(0, x, 0, y).unwrap_or_else(|p| panic!("{name}: {p}"));
                same(&got, twin, x.len() / DIM, &format!("{} with {bytes} bytes, {name}", adapter.name));
            }
            eprintln!("adapter {} ({}) with {bytes} bytes: the bests of the CPU twin", adapter.index, adapter.name);
        }
    }
}
