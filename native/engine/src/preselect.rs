//! The generic preselection: which pairs of photos overlap, found on the small features of the
//! photos (the store the features step fills at the preselection size, a thousand points of
//! each photo reduced to a thousand pixels) before the full matching, as the classic step finds
//! them with COLMAP's exhaustive matching of that store. Every pair of the photos is matched
//! with COLMAP's three tests, and the pairs with enough matches are verified (`geometry::two_view`,
//! the RANSAC seeded by the pair): the pairs with at least `min_inliers` inlier matches come back
//! with their count, the weights the ranking in Python works on (`sfm.strongest_pairs`).
//!
//! The matching goes through the queue of `sched` in blocks: the photos in groups of
//! `GROUP_PHOTOS`, a unit every pair of two groups (the pairs `i < j` of it). On a GPU a unit is
//! matched in dispatches of `Matcher::bests_block`, the photos of a sub-block concatenated: the
//! first dispatch is a probe of `PROBE_PAIRS` pairs, and every dispatch is timed to size the next
//! so that one stays around `DISPATCH_SECONDS` (Windows resets a driver that does not answer for
//! two seconds), within the working memory the device binds. Without a GPU the CPU twin
//! (`matching::bests_cpu`) matches the pairs of a unit on every core; with GPUs the CPU matches
//! nothing, as the design says. The verification then runs on the CPU threads, a unit per pair,
//! through the same queue. The kernels being exact and the RANSAC seeded, the weights are the
//! same whichever device did which unit.
//!
//! Key points are in the pixels of the full photo (the store's convention), the cameras the
//! full-size ones: the thresholds of the verification apply in the same pixels as in the classic
//! step, whose extractor scales the key points of the reduced photo back too.

use std::collections::{HashMap, HashSet};
use std::ops::Range;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::thread;
use std::time::Instant;

use rayon::prelude::*;

use crate::camera::Camera;
use crate::geometry::two_view::{self, pair_seed, verify};
use crate::gpu::{match_quality, select_matches, Matcher, DIM};
use crate::matching::bests_cpu;
use crate::sched::{self, Worker};
use crate::store::{FeatureStore, FeatureView};

/// The photos of a group: a unit of the matching is every pair of two groups, at most this many
/// photos squared. A choice, not a measured figure: a thousand pairs of a thousand descriptors
/// are a fraction of a second on a fast GPU and a few seconds on the CPU, and a project of a
/// thousand photos gives about five hundred units for the queue to share out.
pub const GROUP_PHOTOS: usize = 32;

/// The pairs of the first dispatch of a GPU: a probe, timed to size the dispatches after it.
const PROBE_PAIRS: usize = 64;

/// What a dispatch of a GPU aims to take, in seconds: half the second the design allows, for
/// the variance of a measurement, and a quarter of the two after which Windows resets a driver
/// that does not answer.
const DISPATCH_SECONDS: f64 = 0.5;

/// The options of the matching: COLMAP's three tests as the program passes them, and the
/// workgroup memory the kernels of every GPU are built for (`Matcher::with_workgroup_bytes`;
/// `None` for the adapter's own, which is what the Python binding passes - the tests and
/// `ppg-bench` force the small tiles with it).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct MatchOptions {
    pub max_ratio: f32,
    pub max_distance: f32,
    pub cross_check: bool,
    pub workgroup_bytes: Option<u32>,
}

/// The options of the preselection: the inlier matches that make two photos a pair
/// (`sfm.PRESELECTION_INLIERS`, passed from Python), the matching and the verification.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PreselectOptions {
    pub min_inliers: usize,
    pub matching: MatchOptions,
    pub two_view: two_view::Options,
}

/// The matches of one pair of photos (indices into the photos given): pairs of key point
/// indices and the quality of each (`gpu::match_quality`).
#[derive(Clone, Debug, PartialEq)]
pub struct PairMatches {
    pub a: usize,
    pub b: usize,
    pub matches: Vec<[u32; 2]>,
    pub quality: Vec<f32>,
}

/// What the matching of every pair gave: every pair of the photos (`a < b`), unit by unit in the
/// order of `blocks` and in order within a unit; the GPUs that did the matching (adapter index,
/// name, the workgroup memory of their kernels); how many units the GPUs and the CPU did; the
/// wall seconds and the log.
#[derive(Clone, Debug, Default)]
pub struct Matched {
    pub pairs: Vec<PairMatches>,
    pub gpus: Vec<(usize, String, u32)>,
    pub by_device: (usize, usize),
    pub seconds: f64,
    pub log: Vec<String>,
}

/// What the preselection gave: the pairs with at least `min_inliers` inlier matches, each as
/// the two names in byte order (`a < b`, as `sfm._preselected_pairs` keys them) with the count,
/// sorted by the two names (whatever the units of the queue were); how many pairs were tried
/// (every pair of the photos); the wall seconds of the two phases; the log (GPUs not opened or
/// without the block kernels, workers retired).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct PreselectReport {
    pub weights: Vec<(String, String, u32)>,
    pub pairs_tried: usize,
    pub seconds_matching: f64,
    pub seconds_verifying: f64,
    pub log: Vec<String>,
}

/// A unit of the matching: the pairs `(i, j)` with `i` in `a`, `j` in `b` and `i < j`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Block {
    pub a: Range<usize>,
    pub b: Range<usize>,
}

impl Block {
    /// The pairs of the block, `i < j`, in order.
    fn pairs(&self) -> impl Iterator<Item = (usize, usize)> + '_ {
        self.a.clone().flat_map(move |i| self.b.clone().filter(move |&j| i < j).map(move |j| (i, j)))
    }

    fn is_empty(&self) -> bool {
        // the pair of the first of A and the last of B is the one with the most room
        self.a.is_empty() || self.b.is_empty() || self.a.start + 1 >= self.b.end
    }
}

/// The groups of `GROUP_PHOTOS` photos of `n`, and the units: every pair of groups, a group
/// with itself included (its pairs `i < j`), in order.
pub fn blocks(n: usize) -> Vec<Block> {
    let groups: Vec<Range<usize>> =
        (0..n).step_by(GROUP_PHOTOS).map(|from| from..(from + GROUP_PHOTOS).min(n)).collect();
    let mut blocks = Vec::new();
    for (x, a) in groups.iter().enumerate() {
        for b in &groups[x..] {
            blocks.push(Block { a: a.clone(), b: b.clone() });
        }
    }
    blocks
}

/// What the workers of the matching share: the descriptors of the photos, the options, the
/// cancel flag of the run (a GPU looks at it before every dispatch); the units done by kind of
/// worker.
struct Matching<'a> {
    photos: &'a [&'a [u8]],
    o: &'a MatchOptions,
    cancel: &'a AtomicBool,
    by_gpu: AtomicUsize,
    by_cpu: AtomicUsize,
}

impl Matching<'_> {
    /// The matches of the pair `(i, j)` out of its bests: COLMAP's tests, and the quality of
    /// each match.
    fn pair(&self, i: usize, j: usize, bests: &[[u32; 3]]) -> PairMatches {
        let (n_a, n_b) = (self.photos[i].len() / DIM, self.photos[j].len() / DIM);
        let o = self.o;
        let matches = select_matches(bests, n_a, n_b, o.max_ratio, o.max_distance, o.cross_check);
        let quality = match_quality(bests, &matches);
        PairMatches { a: i, b: j, matches, quality }
    }
}

/// The smallest whole number whose square is `n` or more.
fn ceil_sqrt(n: usize) -> usize {
    let root = n.isqrt();
    if root * root < n { root + 1 } else { root }
}

/// A GPU matching the units in dispatches sized from the time the last one took.
struct GpuWorker<'a> {
    matcher: Matcher,
    index: usize,
    ctx: &'a Matching<'a>,
    /// The pairs the next dispatch aims at.
    pairs_per_dispatch: usize,
}

impl GpuWorker<'_> {
    /// `rows` x `cols` photos of at most `points` descriptors, the larger side halved (only
    /// the columns when `rows` is fixed) until the working buffers of the block fit the device.
    fn fit(&self, mut rows: usize, mut cols: usize, points: usize, rows_fixed: bool) -> Result<(usize, usize), String> {
        let limit = self.matcher.block_limit();
        loop {
            if self.matcher.block_bytes(&vec![points; rows], &vec![points; cols]) <= limit {
                return Ok((rows, cols));
            }
            if cols > 1 && (rows_fixed || cols >= rows) {
                cols = cols.div_ceil(2);
            } else if rows > 1 && !rows_fixed {
                rows = rows.div_ceil(2);
            } else {
                return Err(format!("{}: a block of {rows} x {cols} photos of {points} descriptors needs more working \
                                    memory than the {limit} bytes the device binds", self.matcher.info.name));
            }
        }
    }

    /// The next dispatch sized from this one: `pairs` in `seconds` give the rate, the rate times
    /// `DISPATCH_SECONDS` the pairs - at most four times as many as this one, at least half.
    fn measured(&mut self, pairs: usize, seconds: f64) {
        let rate = pairs as f64 / seconds.max(1e-6);
        let next = (rate * DISPATCH_SECONDS) as usize;
        self.pairs_per_dispatch = next.clamp((pairs / 2).max(1), pairs * 4);
    }
}

impl Worker<Block, Vec<PairMatches>> for GpuWorker<'_> {
    fn name(&self) -> String {
        format!("GPU {} ({})", self.index, self.matcher.info.name)
    }

    /// The unit in sub-blocks: rows of A chosen from the pairs the last dispatch earned, the
    /// columns of B of each dispatch too; a sub-block below the diagonal of the block (no `i <
    /// j` in it) is not dispatched. A cancel seen before a dispatch ends the unit with
    /// `"cancelled"`: the scheduler gives the unit back and ends the run as cancelled.
    fn run(&mut self, unit: &Block) -> Result<Vec<PairMatches>, String> {
        let photos = self.ctx.photos;
        let points = unit.a.clone().chain(unit.b.clone()).map(|i| photos[i].len() / DIM).max().unwrap_or(0);
        let mut out = Vec::new();
        let mut a_from = unit.a.start;
        while a_from < unit.a.end {
            let budget = self.pairs_per_dispatch;
            let rows = ceil_sqrt(budget).clamp(1, unit.a.end - a_from);
            let cols = (budget / rows).clamp(1, unit.b.len());
            let (rows, _) = self.fit(rows, cols, points, false)?;
            let a_chunk = a_from..a_from + rows;
            a_from += rows;
            let mut b_from = unit.b.start;
            while b_from < unit.b.end {
                if self.ctx.cancel.load(Ordering::SeqCst) {
                    return Err("cancelled".to_owned());
                }
                let cols = (self.pairs_per_dispatch / rows).clamp(1, unit.b.end - b_from);
                let (_, cols) = self.fit(rows, cols, points, true)?;
                let sub = Block { a: a_chunk.clone(), b: b_from..b_from + cols };
                b_from += cols;
                if sub.is_empty() {
                    continue;
                }
                let a: Vec<&[u8]> = sub.a.clone().map(|i| photos[i]).collect();
                let b: Vec<&[u8]> = sub.b.clone().map(|j| photos[j]).collect();
                let started = Instant::now();
                let bests = self.matcher.bests_block(&a, &b)?;
                self.measured(a.len() * b.len(), started.elapsed().as_secs_f64());
                for (i, j) in sub.pairs() {
                    out.push(self.ctx.pair(i, j, &bests[(i - sub.a.start) * cols + (j - sub.b.start)]));
                }
            }
        }
        // sub-block by sub-block until here, and the sub-blocks follow the timing: in the order
        // of the unit's pairs, as the CPU gives them, whatever the dispatches were
        out.sort_by_key(|pair| (pair.a, pair.b));
        self.ctx.by_gpu.fetch_add(1, Ordering::Relaxed);
        Ok(out)
    }
}

/// The CPU matching the pairs of a unit on every core of rayon.
struct CpuWorker<'a> {
    ctx: &'a Matching<'a>,
}

impl Worker<Block, Vec<PairMatches>> for CpuWorker<'_> {
    fn name(&self) -> String {
        "CPU".to_owned()
    }

    fn run(&mut self, unit: &Block) -> Result<Vec<PairMatches>, String> {
        let photos = self.ctx.photos;
        let pairs: Vec<(usize, usize)> = unit.pairs().collect();
        let out = pairs.par_iter().map(|&(i, j)| self.ctx.pair(i, j, &bests_cpu(photos[i], photos[j]))).collect();
        self.ctx.by_cpu.fetch_add(1, Ordering::Relaxed);
        Ok(out)
    }
}

/// The error of a phase, with the lines of its log after it.
fn failed(reason: String, lines: Vec<String>) -> String {
    if lines.is_empty() {
        reason
    } else {
        format!("{reason}\n{}", lines.join("\n"))
    }
}

/// Every pair of `photos` (their descriptors, n x 128 bytes each) matched with COLMAP's tests on
/// the GPUs `devices` (adapter indices; one that opens no matcher, or whose backend refuses the
/// kernels of a block, is logged and left to the others) or, when none is given or none opens,
/// on the CPU. `progress(done, total)` counts the units of the queue (the blocks of `blocks`).
/// An error, with the log of the run, on a cancel (`"cancelled"` when nothing else happened) or
/// when the queue is left with units and no worker.
pub fn match_blocks(photos: &[&[u8]], o: &MatchOptions, devices: &[usize], cancel: &AtomicBool,
                    progress: &(dyn Fn(usize, usize) + Sync)) -> Result<Matched, String> {
    let started = Instant::now();
    let ctx = Matching { photos, o, cancel, by_gpu: AtomicUsize::new(0), by_cpu: AtomicUsize::new(0) };
    let mut log = Vec::new();
    let mut opened = Vec::new();
    let mut gpus: Vec<Box<dyn Worker<Block, Vec<PairMatches>> + '_>> = Vec::new();
    for &index in devices {
        let matcher = match o.workgroup_bytes {
            Some(bytes) => Matcher::with_workgroup_bytes(Some(index), bytes),
            None => Matcher::new(Some(index)),
        };
        let mut matcher = match matcher {
            Ok(matcher) => matcher,
            Err(problem) => {
                log.push(format!("GPU {index} not opened ({problem}): the other workers do its share"));
                continue;
            }
        };
        // the kernels of a block built now: a backend that refuses them leaves the device out
        // of the preselection (its pair kernels are not affected)
        if let Err(problem) = matcher.prepare_block() {
            log.push(format!("GPU {index}: {problem}; the other workers do its share"));
            continue;
        }
        opened.push((index, matcher.info.name.clone(), matcher.workgroup_bytes()));
        gpus.push(Box::new(GpuWorker { matcher, index, ctx: &ctx, pairs_per_dispatch: PROBE_PAIRS }));
    }
    let cpu_threads = usize::from(gpus.is_empty());
    let cpu: Option<sched::Factory<'_, Block, Vec<PairMatches>>> = if cpu_threads > 0 {
        let ctx = &ctx;
        Some(Box::new(move || Box::new(CpuWorker { ctx }) as Box<dyn Worker<Block, Vec<PairMatches>> + '_>))
    } else {
        None
    };
    let run = match sched::run(blocks(photos.len()), gpus, cpu, cpu_threads, cancel, progress) {
        Ok(run) => run,
        Err((reason, lines)) => {
            log.extend(lines);
            return Err(failed(reason, log));
        }
    };
    log.extend(run.log);
    let pairs = run.results.into_iter().flat_map(|(_, pairs)| pairs).collect();
    let by_device = (ctx.by_gpu.load(Ordering::Relaxed), ctx.by_cpu.load(Ordering::Relaxed));
    Ok(Matched { pairs, gpus: opened, by_device, seconds: started.elapsed().as_secs_f64(), log })
}

/// What the verification workers share.
struct Verifying<'a> {
    pairs: &'a [PairMatches],
    views: &'a [FeatureView],
    names: &'a [String],
    cams: &'a HashMap<String, Camera>,
    calibrated: &'a HashSet<String>,
    o: &'a two_view::Options,
}

/// A CPU thread verifying one pair per unit: the inlier matches.
struct Verifier<'a> {
    ctx: &'a Verifying<'a>,
}

impl Worker<usize, u32> for Verifier<'_> {
    fn name(&self) -> String {
        "CPU".to_owned()
    }

    fn run(&mut self, unit: &usize) -> Result<u32, String> {
        let ctx = self.ctx;
        let pair = &ctx.pairs[*unit];
        let (name_a, name_b) = (&ctx.names[pair.a], &ctx.names[pair.b]);
        let calibrated = ctx.calibrated.contains(name_a) && ctx.calibrated.contains(name_b);
        let two_view = verify(&ctx.cams[name_a], ctx.views[pair.a].keypoints(), &ctx.cams[name_b],
                              ctx.views[pair.b].keypoints(), &pair.matches, &pair.quality, calibrated, ctx.o,
                              pair_seed(name_a, name_b));
        Ok(two_view.inliers.len() as u32)
    }
}

/// The preselection of the photos of `store` (their small features): every pair matched
/// (`match_blocks`) on the GPUs `devices` or the CPU, the pairs with enough matches verified on
/// the CPU threads with the cameras `cams` (one per photo name; a photo without one is an error
/// before any work) - `calibrated` names the photos with a prior focal length, a pair is
/// calibrated when both are in it - and the pairs with at least `o.min_inliers` inlier matches
/// returned with their count. `progress(done, total)` on one scale for both phases: `total` is
/// twice the pairs tried, the matching counts up to the pairs tried (by the units done) and the
/// verification counts the pairs after that, those it skips for want of matches at once. An
/// error on a cancel (`"cancelled"`), when no worker is left with units to do, or when a photo
/// has no camera or its features cannot be mapped.
#[allow(clippy::too_many_arguments)]
pub fn run(store: &FeatureStore, cams: &HashMap<String, Camera>, calibrated: &HashSet<String>, devices: &[usize],
           o: &PreselectOptions, cancel: &AtomicBool, progress: &(dyn Fn(usize, usize) + Sync))
    -> Result<PreselectReport, String> {
    let names = store.names();
    for name in &names {
        if !cams.contains_key(name) {
            return Err(format!("{name}: no camera for the photo"));
        }
    }
    let views: Vec<FeatureView> = names
        .iter()
        .map(|name| store.get(name).ok_or_else(|| format!("{name}: its features cannot be mapped")))
        .collect::<Result<_, _>>()?;
    if cancel.load(Ordering::SeqCst) {
        return Err("cancelled".to_owned());
    }
    let n = names.len();
    let pairs_tried = n * n.saturating_sub(1) / 2;
    let total = 2 * pairs_tried;
    let descriptors: Vec<&[u8]> = views.iter().map(FeatureView::descriptors).collect();
    let by_units = |done: usize, units: usize| progress(if units == 0 { 0 } else { done * pairs_tried / units }, total);
    let matched = match_blocks(&descriptors, &o.matching, devices, cancel, &by_units)?;
    let mut log = matched.log.clone();

    // the verification: the pairs with at least the inliers wanted among their matches (fewer
    // are degenerate at once), one unit each, on every core
    let started = Instant::now();
    let to_verify: Vec<usize> =
        (0..matched.pairs.len()).filter(|&k| matched.pairs[k].matches.len() >= o.two_view.min_num_inliers).collect();
    let skipped = pairs_tried - to_verify.len();
    let ctx = Verifying { pairs: &matched.pairs, views: &views, names: &names, cams, calibrated, o: &o.two_view };
    let factory: sched::Factory<'_, usize, u32> = {
        let ctx = &ctx;
        Box::new(move || Box::new(Verifier { ctx }) as Box<dyn Worker<usize, u32> + '_>)
    };
    let threads = thread::available_parallelism().map_or(1, |n| n.get());
    let verified = |done: usize, _: usize| progress(pairs_tried + skipped + done, total);
    let run = match sched::run(to_verify, Vec::new(), Some(factory), threads, cancel, &verified) {
        Ok(run) => run,
        Err((reason, lines)) => {
            log.extend(lines);
            return Err(failed(reason, log));
        }
    };
    log.extend(run.log);
    let mut weights: Vec<(String, String, u32)> = run
        .results
        .into_iter()
        .filter(|(_, inliers)| *inliers as usize >= o.min_inliers)
        .map(|(k, inliers)| {
            let pair = &matched.pairs[k];
            (names[pair.a].clone(), names[pair.b].clone(), inliers)
        })
        .collect();
    // unit by unit until here: by name, whatever the groups were
    weights.sort();
    if total > 0 {
        progress(total, total);
    }
    Ok(PreselectReport { weights, pairs_tried, seconds_matching: matched.seconds,
                         seconds_verifying: started.elapsed().as_secs_f64(), log })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_blocks_cover_every_pair_once() {
        for n in [0, 1, 2, 5, GROUP_PHOTOS, GROUP_PHOTOS + 1, 3 * GROUP_PHOTOS + 7] {
            let blocks = blocks(n);
            let groups = n.div_ceil(GROUP_PHOTOS);
            assert_eq!(blocks.len(), groups * (groups + 1) / 2, "{n} photos");
            let mut pairs: Vec<(usize, usize)> = blocks.iter().flat_map(Block::pairs).collect();
            let count = pairs.len();
            pairs.sort_unstable();
            pairs.dedup();
            assert_eq!(pairs.len(), count, "{n} photos: a pair twice");
            assert_eq!(count, n * n.saturating_sub(1) / 2, "{n} photos");
            assert!(pairs.iter().all(|&(i, j)| i < j && j < n));
        }
        // a block below the diagonal has no pair; one that touches it has
        assert!(Block { a: 8..16, b: 0..8 }.is_empty());
        assert!(Block { a: 8..16, b: 0..9 }.is_empty());
        assert!(!Block { a: 8..16, b: 0..10 }.is_empty());
        assert_eq!(Block { a: 8..16, b: 0..10 }.pairs().collect::<Vec<_>>(), vec![(8, 9)]);
        assert!(!Block { a: 0..3, b: 0..3 }.is_empty());
        assert_eq!(Block { a: 0..3, b: 0..3 }.pairs().count(), 3);
    }

    #[test]
    fn ceil_sqrt_rounds_up() {
        assert_eq!([0, 1, 2, 4, 5, 63, 64, 65, 1024].map(ceil_sqrt), [0, 1, 2, 2, 3, 8, 8, 9, 32]);
    }
}
