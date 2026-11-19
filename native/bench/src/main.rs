//! `ppg-bench`: the kernels of `ppg_engine` run without Python, to see what a computer or a CI
//! runner has and how long a kernel takes on it. What it prints on the standard output is JSON;
//! an error goes to the standard error with exit code 1, a command line it does not know with
//! exit code 2.
//!
//! - `ppg-bench adapters`: the GPU adapters wgpu finds, an array with the `index`, `name`,
//!   `backend` and `device_type` of each.
//! - `ppg-bench match [--n 4096] [--device N] [--workgroup-kb 16|32] [--cpu]`: the matching of
//!   descriptors, timed on two generated sets of `n` descriptors of which the right matches are
//!   known; it prints `device` (the name of the adapter), `n`, `dim`, `matches`, `correct`,
//!   `seconds` (the time of one call, the first on a new matcher: not a repeated or a warmed
//!   timing) and `workgroup_kb` (16 or 32: the workgroup memory of the kernels the matcher
//!   chose from the adapter's limits, or the one `--workgroup-kb` forced - 16 on any adapter
//!   exercises the small tiles). Without `--device` the first adapter that is no software
//!   rasteriser is taken; when there is none the answer is `{"skipped": "no adapter"}`, with
//!   exit code 0. `n` is 46336 at most with the tiles of 128 descriptors and 32768 with those of
//!   64 (`most_descriptors`): more is a command line the binary refuses, before a device is
//!   opened. With `--cpu` the CPU twin of the kernels (`matching::matches_cpu`) is timed instead,
//!   on any computer, with `"device": "cpu"` and no `workgroup_kb`; it takes no `--device` and no
//!   `--workgroup-kb`, and no bound on `n` (it binds no buffer).
//! - `ppg-bench adjust [--photos 200] [--points 2000] [--solver auto|dense|sparse|pcg] [--seed 1]`:
//!   the bundle adjustment of the synthetic grid block of `bundle::synthetic::grid_block`
//!   (`photos` photos, `points` points - `points / photos` per photo, rounded up - perturbed from
//!   exact observations), timed; it prints `photos`, `points`, `observations`, `solver` (the one
//!   used: what `auto` chose), `iterations`, `cost_initial`, `cost` and `seconds` (the time of
//!   `solve` alone, the block made before the clock starts).
//! - `ppg-bench features DIR [--limit 2016] [--max-features 8192] [--device N | --cpu]`: the
//!   features step of the native engine (`features::extract_all`) on the JPEG, PNG and TIFF files
//!   of the folder `DIR` (not of its subfolders), each reduced to `limit` on its long side (0 for
//!   none) with at most `max-features` points, into a store under the temporary folder of the
//!   system, removed after; it prints `device` (the name of the adapter, or `cpu`), `photos` (the
//!   files found), `done`, `unreadable` and `unsupported` (counts), `points_per_photo` (the mean
//!   over the photos done), `seconds` (the wall time of the step), `photos_per_second`, the
//!   thread-seconds by phase `decode_seconds`, `sift_seconds` and `write_seconds`, `limit` and
//!   `max_features` - never the name of a photo. On a GPU the CPU takes no photo, so the record
//!   is the device's; with `--cpu` the CPU twin does them all; without `--device` the first
//!   adapter that is no software rasteriser, else `{"skipped": "no adapter"}`.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::AtomicBool;
use std::time::Instant;

use ppg_engine::bundle::synthetic::grid_block;
use ppg_engine::bundle::{solve, LinearSolver, Options};
use ppg_engine::features::{extract_all, FeatureOptions, PhotoJob};
use ppg_engine::store::FeatureStore;
use ppg_engine::{gpu, gpu_sift, matching};
use serde_json::{json, Value};

/// The descriptors of each set when `--n` is not given: a choice, not a measured figure.
const DEFAULT_N: usize = 4096;

/// What `features` reduces the photos to and keeps of them when it is not told otherwise: the
/// program's defaults for the alignment (quality "high" on a 24-megapixel photo, COLMAP's cap of
/// points), copied here as choices.
const DEFAULT_LIMIT: usize = 2016;
const DEFAULT_MAX_FEATURES: usize = 8192;

/// The reader threads of `features`: a choice, not a measured figure (the decoding of a JPEG at
/// a DCT scale takes a fraction of the SIFT; a few readers keep a GPU fed).
const READERS: usize = 4;

/// The files `features` takes for photos, by extension (either case).
const PHOTO_EXTENSIONS: [&str; 5] = ["jpg", "jpeg", "png", "tif", "tiff"];

/// The block `adjust` makes when it is not told otherwise: choices, not measured figures.
const DEFAULT_PHOTOS: usize = 200;
const DEFAULT_POINTS: usize = 2000;
const DEFAULT_SEED: u64 = 1;

/// The storage binding wgpu 24 promises on every modern backend (`max_storage_buffer_binding_size`
/// of `Limits::default()`): 128 MiB. The engine does not say what the device it opens holds, and
/// with a buffer beyond that the kernel, which has no error scope, ends in a panic of wgpu. Many
/// devices hold more, and an adapter below the defaults of wgpu (a downlevel one) holds less.
const BINDING: usize = 128 << 20;

/// The most descriptors `--n` takes with tiles of `rows` descriptors of the first set (the
/// `rows` of `gpu::Tile`): derived, not measured. The buffer of the matching kernel that grows
/// fastest holds its partial results, 8 bytes for every tile of the first set (the last tile
/// counts whole) and every descriptor of the second, and the kernel binds it whole as a storage
/// buffer: for two sets of `n` that is ceil(n / rows) * n * 8 bytes, to stay within `BINDING`.
/// With the large tiles (128 rows) that is 46336 (362 tiles, 134 189 056 bytes; 46337 takes 363
/// tiles, 134 562 648 bytes); with the small ones (64 rows) 32768 (512 tiles, 128 MiB exactly).
/// The figure is what can be said without the device.
fn most_descriptors(rows: usize) -> usize {
    let fits = |n: usize| n.div_ceil(rows) * n * 8 <= BINDING;
    // n * n * 8 / rows <= the partials, so the answer is at most sqrt(BINDING / 8 * rows): from
    // there, or one above it against the rounding of the root, down to the first that fits
    let mut n = ((BINDING / 8 * rows) as f64).sqrt() as usize + 1;
    while !fits(n) {
        n -= 1;
    }
    n
}

/// The tiles of the matching kernels `--workgroup-kb` names, or the large ones when it is not
/// given: the bound on `--n` is theirs before a device is opened (an adapter with the small
/// tiles only is held to its own bound once it is).
fn tiles_asked(workgroup_kb: Option<u32>) -> gpu::Tile {
    match workgroup_kb {
        Some(16) => gpu::SMALL_TILE,
        _ => gpu::LARGE_TILE,
    }
}

// COLMAP's matching options as the program passes them: the defaults of `gpu_match` in
// src/pyarchinit_photogrammetry/_native.py, copied here (to be changed with them)
const MAX_RATIO: f32 = 0.8;
const MAX_DISTANCE: f32 = 0.7;
const CROSS_CHECK: bool = true;

/// The norm of a descriptor before it is rounded to bytes: COLMAP's, as the kernel takes the
/// descriptors (gpu.rs of the engine).
const NORM: f64 = 512.0;

/// The noise that makes the twin of a descriptor, as a part of its norm. A choice, not a
/// measured figure: twins at an angle of about 0.02, far inside the two thresholds of the
/// matching, so that every twin is expected to be found.
const NOISE: f64 = 0.02;

// the seeds of the two sets: any two numbers, fixed so that every run on every computer matches
// the same descriptors
const SEED_A: u64 = 7;
const SEED_B: u64 = 11;

/// A 64-bit linear congruential generator, written here so that the descriptors depend on no
/// crate and on no version of one: the multiplier and the increment are those of Knuth's MMIX.
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

    /// A whole number below `bound`, out of the upper 32 bits (the lower ones of such a
    /// generator repeat early).
    fn below(&mut self, bound: usize) -> usize {
        (((self.bits() >> 32) * bound as u64) >> 32) as usize
    }
}

fn normalised(mut values: Vec<f64>) -> Vec<f64> {
    let norm = values.iter().map(|v| v * v).sum::<f64>().sqrt();
    values.iter_mut().for_each(|v| *v /= norm);
    values
}

/// A descriptor with a norm of 1: the squares of `DIM` values in [0, 1), most components small
/// and a few large as in a SIFT descriptor.
fn descriptor(random: &mut Lcg) -> Vec<f64> {
    normalised((0..gpu::DIM).map(|_| random.unit().powi(2)).collect())
}

/// A vector with the norm `NOISE`, in a direction drawn from `random`.
fn noise(random: &mut Lcg) -> Vec<f64> {
    let direction = normalised((0..gpu::DIM).map(|_| random.unit() - 0.5).collect());
    direction.into_iter().map(|v| v * NOISE).collect()
}

/// The twin of a descriptor with a norm of 1: the descriptor moved by the noise, no component
/// below 0, with a norm of 1 again.
fn twin(descriptor: &[f64], random: &mut Lcg) -> Vec<f64> {
    let noise = noise(random);
    normalised(descriptor.iter().zip(noise).map(|(v, e)| (v + e).max(0.0)).collect())
}

/// A descriptor with a norm of 1 added to a set as the bytes the kernel takes: the norm of
/// `NORM`, every component rounded and kept at 255 at most.
fn push_bytes(set: &mut Vec<u8>, descriptor: &[f64]) {
    set.extend(descriptor.iter().map(|v| (v * NORM).round().min(255.0) as u8));
}

/// The numbers below `n` in an order drawn from `random` (Fisher-Yates).
fn permutation(n: usize, random: &mut Lcg) -> Vec<usize> {
    let mut order: Vec<usize> = (0..n).collect();
    for last in (1..n).rev() {
        order.swap(last, random.below(last + 1));
    }
    order
}

/// The two sets to match, `n` descriptors each as the kernel takes them (`n` x `DIM` bytes): the
/// second holds the twins of the first in another order. With them, for every descriptor of the
/// second set, the index of the descriptor of the first it is the twin of.
fn descriptor_sets(n: usize) -> (Vec<u8>, Vec<u8>, Vec<usize>) {
    let mut random = Lcg(SEED_A);
    let first: Vec<Vec<f64>> = (0..n).map(|_| descriptor(&mut random)).collect();
    let mut random = Lcg(SEED_B);
    let twin_of = permutation(n, &mut random);
    let (mut a, mut b) = (Vec::with_capacity(n * gpu::DIM), Vec::with_capacity(n * gpu::DIM));
    for descriptor in &first {
        push_bytes(&mut a, descriptor);
    }
    for &of in &twin_of {
        push_bytes(&mut b, &twin(&first[of], &mut random));
    }
    (a, b, twin_of)
}

/// The adapters as a JSON array: the fields of `AdapterInfo`, nothing added.
fn adapters() -> Value {
    gpu::adapters()
        .into_iter()
        .map(|a| json!({"index": a.index, "name": a.name, "backend": a.backend, "device_type": a.device_type}))
        .collect()
}

/// Where `match` runs: a GPU adapter, named or the first that is no software rasteriser, with the
/// tiles its workgroup memory takes or the ones `--workgroup-kb` forces; or the CPU twin.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Where {
    Gpu { device: Option<usize>, workgroup_kb: Option<u32> },
    Cpu,
}

/// The record of a matching of two sets of `n` descriptors on `device` that took `seconds`:
/// the matches, and how many of them join a descriptor to its twin.
fn record(device: &str, n: usize, matches: &[[u32; 2]], twin_of: &[usize], seconds: f64) -> Value {
    let correct = matches.iter().filter(|pair| twin_of[pair[1] as usize] == pair[0] as usize).count();
    json!({"device": device, "n": n, "dim": gpu::DIM, "matches": matches.len(), "correct": correct,
           "seconds": seconds})
}

/// The matching of two sets of `n` descriptors `on` a device, timed: the record to print.
fn time_matching(n: usize, on: Where) -> Result<Value, String> {
    let (device, workgroup_kb) = match on {
        Where::Cpu => {
            let (a, b, twin_of) = descriptor_sets(n);
            // what is timed is the twin alone, the bests of both directions and the matches
            // chosen among them, on every core rayon finds
            let start = Instant::now();
            let matches = matching::matches_cpu(&a, &b, MAX_RATIO, MAX_DISTANCE, CROSS_CHECK);
            let seconds = start.elapsed().as_secs_f64();
            return Ok(record("cpu", n, &matches, &twin_of, seconds));
        }
        Where::Gpu { device, workgroup_kb } => (device, workgroup_kb),
    };
    let index = match device {
        Some(index) => index,
        // a software rasteriser (the type wgpu calls Cpu) is not timed as if it were a GPU: it is
        // taken only when it is named
        None => match gpu::adapters().into_iter().find(|adapter| adapter.device_type != "Cpu") {
            Some(adapter) => adapter.index,
            None => return Ok(json!({"skipped": "no adapter"})),
        },
    };
    let mut matcher = match workgroup_kb {
        Some(kb) => gpu::Matcher::with_workgroup_bytes(Some(index), kb * 1024)?,
        None => gpu::Matcher::new(Some(index))?,
    };
    // an adapter with the small tiles only, taken without --workgroup-kb: the bound of its tiles,
    // below the one the command line was held to, holds before the kernel binds its buffer
    let rows = matcher.tile().rows;
    let most = most_descriptors(rows);
    if n > most {
        return Err(format!(
            "{}: {n} descriptors are more than the {most} its tiles of {rows} take (the kernel's working \
             buffer for more is above the {} MiB a device is sure to bind)",
            matcher.info.name, BINDING >> 20
        ));
    }
    let (a, b, twin_of) = descriptor_sets(n);
    // the device is there: what is timed is the matching alone, from the descriptors sent to the
    // GPU (under the key 0: the matcher does not keep them on it) to the matches chosen among what
    // it answers. `seconds` is ONE call, the first on a new matcher, and includes what a first
    // call pays: the working buffers of the kernel, made in it and kept for the calls after it,
    // and whatever a driver leaves to the first run of a pipeline. It is not a repeated or a
    // warmed timing: not the time of a pair among the many of a project
    let start = Instant::now();
    let matches = matcher.matches(0, &a, 0, &b, MAX_RATIO, MAX_DISTANCE, CROSS_CHECK)?;
    let seconds = start.elapsed().as_secs_f64();
    let mut answer = record(&matcher.info.name, n, &matches, &twin_of, seconds);
    answer["workgroup_kb"] = json!(matcher.workgroup_bytes() / 1024);
    Ok(answer)
}

/// The name of a solver of the reduced system on the command line and in the record.
fn solver_name(solver: LinearSolver) -> &'static str {
    match solver {
        LinearSolver::Auto => "auto",
        LinearSolver::Dense => "dense",
        LinearSolver::Sparse => "sparse",
        LinearSolver::Pcg => "pcg",
    }
}

fn solver_named(word: &str) -> Option<LinearSolver> {
    [LinearSolver::Auto, LinearSolver::Dense, LinearSolver::Sparse, LinearSolver::Pcg]
        .into_iter()
        .find(|s| solver_name(*s) == word)
}

/// The adjustment of the grid block of `photos` photos and about `points` points with `solver`,
/// timed: the record to print.
fn time_adjustment(photos: usize, points: usize, solver: LinearSolver, seed: u64) -> Result<Value, String> {
    let (pb, _) = grid_block(photos, points.div_ceil(photos), seed);
    let opts = Options { linear_solver: solver, ..Options::default() };
    // what is timed is `solve` alone: the block is made and the clock starts
    let start = Instant::now();
    let sol = solve(&pb, &opts, &mut |_, _| true)?;
    let seconds = start.elapsed().as_secs_f64();
    Ok(json!({"photos": pb.centres.len(), "points": pb.points.len(), "observations": pb.obs.len(),
              "solver": solver_name(sol.linear_solver), "iterations": sol.iterations,
              "cost_initial": sol.cost_initial, "cost": sol.cost, "seconds": seconds}))
}

/// Where `features` runs: a GPU adapter, named or the first that is no software rasteriser, or
/// the CPU twin of the kernel.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Extractor {
    Gpu(Option<usize>),
    Cpu,
}

/// The photos of the folder `dir` as jobs of the features step: its files with an extension of
/// `PHOTO_EXTENSIONS`, in the order of their names, each named by its file name.
fn photo_jobs(dir: &Path, limit: usize) -> Result<Vec<PhotoJob>, String> {
    let entries = fs::read_dir(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let mut names: Vec<String> = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|e| format!("{}: {e}", dir.display()))?;
        let name = entry.file_name().to_string_lossy().into_owned();
        let extension = Path::new(&name).extension().map(|e| e.to_string_lossy().to_lowercase());
        let is_photo = extension.is_some_and(|e| PHOTO_EXTENSIONS.contains(&e.as_str()));
        if is_photo && entry.file_type().map_err(|e| format!("{name}: {e}"))?.is_file() {
            names.push(name);
        }
    }
    names.sort();
    Ok(names
        .into_iter()
        .map(|name| PhotoJob { path: Some(dir.join(&name)), name, mask: None, limit, small_limit: None })
        .collect())
}

/// The features step on the photos of `dir` with `on`, timed: the record to print.
fn time_features(dir: &Path, limit: usize, max_features: usize, on: Extractor) -> Result<Value, String> {
    let jobs = photo_jobs(dir, limit)?;
    if jobs.is_empty() {
        return Err(format!("{}: no JPEG, PNG or TIFF file", dir.display()));
    }
    let (devices, device, cpu_threads) = match on {
        Extractor::Cpu => (Vec::new(), "cpu".to_owned(), 1),
        Extractor::Gpu(index) => {
            let adapters = gpu::adapters();
            let index = match index {
                Some(index) => index,
                None => match adapters.iter().find(|adapter| adapter.device_type != "Cpu") {
                    Some(adapter) => adapter.index,
                    None => return Ok(json!({"skipped": "no adapter"})),
                },
            };
            let name = adapters.into_iter().find(|adapter| adapter.index == index).map(|adapter| adapter.name);
            (vec![index], name.ok_or_else(|| format!("GPU adapter {index} does not exist"))?, 0)
        }
    };
    // the store lives for the run only, under the temporary folder of the system
    let store_dir = std::env::temp_dir().join(format!("ppg-bench-features-{}", std::process::id()));
    let _ = fs::remove_dir_all(&store_dir);
    let store = FeatureStore::create(&store_dir).map_err(|e| e.to_string())?;
    let sift = gpu_sift::Options { max_features, ..gpu_sift::Options::default() };
    let options = FeatureOptions { sift, dct: true, readers: READERS, cpu_threads };
    // what is timed is the whole step - decoding, extraction and writing together, as a project
    // pays it - from the jobs to the last file of the store
    let start = Instant::now();
    let report = extract_all(&jobs, &store, None, &options, &devices, &AtomicBool::new(false), &|_, _| {});
    let seconds = start.elapsed().as_secs_f64();
    drop(store);
    let _ = fs::remove_dir_all(&store_dir);
    let report = report?;
    if on != Extractor::Cpu && report.by_impl.1 > 0 {
        // a GPU that did not open leaves its photos to the CPU: not a timing of the device
        let why: Vec<&str> = report.log.iter().filter(|line| line.starts_with("GPU")).map(String::as_str).collect();
        let by_cpu = report.by_impl.1;
        return Err(format!("{device}: {by_cpu} photos done by the CPU, not by the GPU ({})", why.join("; ")));
    }
    Ok(json!({
        "device": device, "photos": jobs.len(), "done": report.done, "unreadable": report.unreadable.len(),
        "unsupported": report.unsupported.len(), "points_per_photo": report.points as f64 / report.done.max(1) as f64,
        "seconds": seconds, "photos_per_second": report.done as f64 / seconds, "decode_seconds": report.seconds.decode,
        "sift_seconds": report.seconds.sift, "write_seconds": report.seconds.write, "limit": limit,
        "max_features": max_features,
    }))
}

enum Command {
    Adapters,
    Match { n: usize, on: Where },
    Adjust { photos: usize, points: usize, solver: LinearSolver, seed: u64 },
    Features { dir: PathBuf, limit: usize, max_features: usize, on: Extractor },
}

/// The command of a command line, or what is wrong with it.
fn command(words: &[String]) -> Result<Command, String> {
    let mut words = words.iter().map(String::as_str);
    match words.next() {
        None => Err("no command".into()),
        Some("adapters") => match words.next() {
            None => Ok(Command::Adapters),
            Some(word) => Err(format!("adapters takes no argument: {word}")),
        },
        Some("match") => {
            let (mut n, mut device, mut workgroup_kb, mut cpu) = (DEFAULT_N, None, None, false);
            while let Some(option) = words.next() {
                match option {
                    "--n" => {
                        n = whole_number(option, words.next())?;
                        if n == 0 {
                            return Err("--n takes 1 or more: two empty sets have nothing to match".into());
                        }
                    }
                    "--device" => device = Some(whole_number(option, words.next())?),
                    "--workgroup-kb" => {
                        let word = words.next().ok_or("--workgroup-kb takes 16 or 32")?;
                        workgroup_kb = match word {
                            "16" => Some(16),
                            "32" => Some(32),
                            _ => return Err(format!("--workgroup-kb takes 16 or 32, not {word}")),
                        };
                    }
                    "--cpu" => cpu = true,
                    _ => return Err(format!("match has no option {option}")),
                }
            }
            if cpu {
                if device.is_some() || workgroup_kb.is_some() {
                    return Err("match --cpu takes no --device and no --workgroup-kb: the CPU twin has no adapter"
                        .into());
                }
                return Ok(Command::Match { n, on: Where::Cpu });
            }
            let rows = tiles_asked(workgroup_kb).rows;
            let most = most_descriptors(rows);
            if n > most {
                return Err(format!(
                    "--n takes {most} at most with tiles of {rows} descriptors: the kernel's working buffer for \
                     more is above the {} MiB a device is sure to bind",
                    BINDING >> 20
                ));
            }
            Ok(Command::Match { n, on: Where::Gpu { device, workgroup_kb } })
        }
        Some("adjust") => {
            let (mut photos, mut points) = (DEFAULT_PHOTOS, DEFAULT_POINTS);
            let (mut solver, mut seed) = (LinearSolver::Auto, DEFAULT_SEED);
            while let Some(option) = words.next() {
                match option {
                    "--photos" => {
                        photos = whole_number(option, words.next())?;
                        if photos == 0 {
                            return Err("--photos takes 1 or more: there is nothing to adjust without a photo".into());
                        }
                    }
                    "--points" => {
                        points = whole_number(option, words.next())?;
                        if points == 0 {
                            return Err("--points takes 1 or more: there is nothing to adjust without a point".into());
                        }
                    }
                    "--solver" => {
                        let word = words.next().ok_or("--solver takes auto, dense, sparse or pcg")?;
                        solver = solver_named(word)
                            .ok_or_else(|| format!("--solver takes auto, dense, sparse or pcg, not {word}"))?;
                    }
                    "--seed" => seed = whole_number(option, words.next())? as u64,
                    _ => return Err(format!("adjust has no option {option}")),
                }
            }
            Ok(Command::Adjust { photos, points, solver, seed })
        }
        Some("features") => {
            let dir = match words.next() {
                Some(word) if !word.starts_with("--") => PathBuf::from(word),
                Some(word) => return Err(format!("features takes the folder of the photos first, not {word}")),
                None => return Err("features takes the folder of the photos".into()),
            };
            let (mut limit, mut max_features, mut device, mut cpu) = (DEFAULT_LIMIT, DEFAULT_MAX_FEATURES, None, false);
            while let Some(option) = words.next() {
                match option {
                    "--limit" => limit = whole_number(option, words.next())?,
                    "--max-features" => {
                        max_features = whole_number(option, words.next())?;
                        if max_features == 0 {
                            return Err("--max-features takes 1 or more: a photo without points has no features".into());
                        }
                    }
                    "--device" => device = Some(whole_number(option, words.next())?),
                    "--cpu" => cpu = true,
                    _ => return Err(format!("features has no option {option}")),
                }
            }
            if cpu && device.is_some() {
                return Err("features --cpu takes no --device: the CPU twin has no adapter".into());
            }
            let on = if cpu { Extractor::Cpu } else { Extractor::Gpu(device) };
            Ok(Command::Features { dir, limit, max_features, on })
        }
        Some(word) => Err(format!("no command {word}")),
    }
}

/// The value of `option`: a whole number, 0 or more.
fn whole_number(option: &str, word: Option<&str>) -> Result<usize, String> {
    let word = word.ok_or_else(|| format!("{option} takes a number"))?;
    word.parse().map_err(|_| format!("{option} takes a whole number, not {word}"))
}

fn main() -> ExitCode {
    let words: Vec<String> = std::env::args_os().skip(1).map(|word| word.to_string_lossy().into_owned()).collect();
    let answer = match command(&words) {
        Ok(Command::Adapters) => Ok(adapters()),
        Ok(Command::Match { n, on }) => time_matching(n, on),
        Ok(Command::Adjust { photos, points, solver, seed }) => time_adjustment(photos, points, solver, seed),
        Ok(Command::Features { dir, limit, max_features, on }) => time_features(&dir, limit, max_features, on),
        Err(problem) => {
            eprintln!("ppg-bench: {problem}");
            eprintln!("usage: ppg-bench adapters");
            eprintln!("       ppg-bench match [--n {DEFAULT_N}] [--device N] [--workgroup-kb 16|32] [--cpu]");
            eprintln!(
                "       ppg-bench adjust [--photos {DEFAULT_PHOTOS}] [--points {DEFAULT_POINTS} (in all, rounded \
                 up to a multiple of the photos)] [--solver auto|dense|sparse|pcg] [--seed {DEFAULT_SEED}]"
            );
            eprintln!(
                "       ppg-bench features DIR [--limit {DEFAULT_LIMIT}] [--max-features {DEFAULT_MAX_FEATURES}] \
                 [--device N | --cpu]"
            );
            return ExitCode::from(2);
        }
    };
    match answer {
        Ok(answer) => {
            println!("{answer}");
            ExitCode::SUCCESS
        }
        Err(problem) => {
            eprintln!("ppg-bench: {problem}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A handful of descriptors: what is checked holds for each of them.
    const N: usize = 64;

    fn norm(values: impl Iterator<Item = f64>) -> f64 {
        values.map(|v| v * v).sum::<f64>().sqrt()
    }

    /// A command line as `command` takes it.
    fn words(line: &[&str]) -> Vec<String> {
        line.iter().map(|word| word.to_string()).collect()
    }

    #[test]
    fn the_largest_size_is_the_last_whose_partial_results_fit_a_storage_binding() {
        // the bytes of the kernel's buffer of partial results for two sets of n descriptors (8 for
        // every tile of `rows` descriptors of the first and every descriptor of the second),
        // against the 128 MiB of a storage binding: the figures the bound is derived from, for
        // the tiles of both kernels
        for (tile, most) in [(gpu::LARGE_TILE, 46336), (gpu::SMALL_TILE, 32768)] {
            let partials = |n: usize| n.div_ceil(tile.rows) * n * 8;
            assert_eq!(most_descriptors(tile.rows), most, "tiles of {} rows", tile.rows);
            assert!(partials(most) <= BINDING, "{} bytes for {most} descriptors", partials(most));
            assert!(partials(most + 1) > BINDING, "{} bytes for one descriptor more", partials(most + 1));
        }
    }

    #[test]
    fn the_largest_size_is_taken_and_one_more_is_refused() {
        let asked =
            [(None, vec![]), (Some(32), vec!["--workgroup-kb", "32"]), (Some(16), vec!["--workgroup-kb", "16"])];
        for (kb, option) in asked {
            let bound = most_descriptors(tiles_asked(kb).rows);
            let (most, more) = (bound.to_string(), (bound + 1).to_string());
            let mut line = vec!["match", "--n", &most];
            line.extend(&option);
            let taken = command(&words(&line));
            let on = Where::Gpu { device: None, workgroup_kb: kb };
            assert!(matches!(taken, Ok(Command::Match { n, on: o }) if n == bound && o == on), "{line:?} is refused");
            // the refusal says how many are taken, with or without an adapter named
            let mut line = vec!["match", "--n", &more, "--device", "0"];
            line.extend(&option);
            let refused = command(&words(&line)).err();
            assert!(refused.as_ref().is_some_and(|problem| problem.contains(&most)), "{line:?}: {refused:?}");
        }
        // the small tiles take fewer: a size the large ones take is refused with them
        assert!(command(&words(&["match", "--n", "46336", "--workgroup-kb", "16"])).is_err());
    }

    #[test]
    fn match_reads_where_it_runs() {
        let on = |line: &[&str]| match command(&words(line)) {
            Ok(Command::Match { n, on }) => Ok((n, on)),
            Ok(_) => panic!("{line:?} is another command"),
            Err(problem) => Err(problem),
        };
        assert_eq!(on(&["match"]), Ok((DEFAULT_N, Where::Gpu { device: None, workgroup_kb: None })));
        assert_eq!(on(&["match", "--device", "2", "--workgroup-kb", "16"]),
                   Ok((DEFAULT_N, Where::Gpu { device: Some(2), workgroup_kb: Some(16) })));
        assert_eq!(on(&["match", "--workgroup-kb", "32", "--n", "9"]),
                   Ok((9, Where::Gpu { device: None, workgroup_kb: Some(32) })));
        assert_eq!(on(&["match", "--cpu", "--n", "9"]), Ok((9, Where::Cpu)));
        // the CPU twin binds no buffer: no bound on n
        assert_eq!(on(&["match", "--n", "46337", "--cpu"]), Ok((46337, Where::Cpu)));
        let refused: [&[&str]; 4] = [&["match", "--workgroup-kb"], &["match", "--workgroup-kb", "24"],
                                     &["match", "--cpu", "--device", "0"], &["match", "--cpu", "--workgroup-kb", "16"]];
        for line in refused {
            assert!(on(line).is_err(), "{line:?} is taken");
        }
    }

    #[test]
    fn adjust_reads_its_options_and_has_its_defaults() {
        let Ok(Command::Adjust { photos, points, solver, seed }) = command(&words(&["adjust"])) else {
            panic!("adjust without options is refused")
        };
        assert_eq!((photos, points, solver, seed), (200, 2000, LinearSolver::Auto, 1));
        let line = ["adjust", "--solver", "pcg", "--points", "30", "--photos", "3", "--seed", "9"];
        let Ok(Command::Adjust { photos, points, solver, seed }) = command(&words(&line)) else {
            panic!("the options of adjust are refused")
        };
        assert_eq!((photos, points, solver, seed), (3, 30, LinearSolver::Pcg, 9));
        for word in ["auto", "dense", "sparse", "pcg"] {
            assert_eq!(solver_named(word).map(solver_name), Some(word));
        }
        assert_eq!(solver_named("cholesky"), None);
        let refused = command(&words(&["adjust", "--solver", "cholesky"])).err();
        assert!(refused.as_ref().is_some_and(|problem| problem.contains("auto, dense, sparse or pcg")), "{refused:?}");
    }

    #[test]
    fn features_reads_its_options_and_has_its_defaults() {
        let on = |line: &[&str]| match command(&words(line)) {
            Ok(Command::Features { dir, limit, max_features, on }) => Ok((dir, limit, max_features, on)),
            Ok(_) => panic!("{line:?} is another command"),
            Err(problem) => Err(problem),
        };
        let folder = PathBuf::from("photos");
        assert_eq!(on(&["features", "photos"]), Ok((folder.clone(), 2016, 8192, Extractor::Gpu(None))));
        assert_eq!(on(&["features", "photos", "--device", "1", "--limit", "0", "--max-features", "40000"]),
                   Ok((folder.clone(), 0, 40000, Extractor::Gpu(Some(1)))));
        assert_eq!(on(&["features", "photos", "--cpu", "--limit", "1024"]), Ok((folder, 1024, 8192, Extractor::Cpu)));
        let refused: [&[&str]; 7] = [&["features"], &["features", "--cpu"], &["features", "photos", "--n", "8"],
                                     &["features", "photos", "--limit"], &["features", "photos", "--limit", "many"],
                                     &["features", "photos", "--max-features", "0"],
                                     &["features", "photos", "--cpu", "--device", "0"]];
        for line in refused {
            assert!(on(line).is_err(), "{line:?} is taken");
        }
    }

    #[test]
    fn the_descriptors_have_the_norm_the_kernel_takes() {
        let (a, b, _) = descriptor_sets(N);
        assert_eq!((a.len(), b.len()), (N * gpu::DIM, N * gpu::DIM));
        // every component is rounded by half a unit at most: the norm is off by sqrt(128) / 2 at most
        let off = (gpu::DIM as f64).sqrt() / 2.0;
        for descriptor in a.chunks_exact(gpu::DIM).chain(b.chunks_exact(gpu::DIM)) {
            let length = norm(descriptor.iter().map(|&v| f64::from(v)));
            assert!((length - NORM).abs() <= off, "a norm of {length}");
        }
    }

    #[test]
    fn the_noise_is_two_percent_of_the_norm() {
        // of a descriptor with a norm of 1, which the noise is added to; 1e-12 is far above what
        // the sums of 128 f64 values lose and far below the figure
        let length = norm(noise(&mut Lcg(SEED_B)).into_iter());
        assert!((length - 0.02).abs() < 1e-12, "a norm of {length}");
    }

    #[test]
    fn the_second_set_holds_the_twins_of_the_first_in_another_order() {
        let (a, b, twin_of) = descriptor_sets(N);
        let in_order: Vec<usize> = (0..N).collect();
        let mut sorted = twin_of.clone();
        sorted.sort_unstable();
        assert_eq!(sorted, in_order, "every descriptor of the first set has one twin");
        assert_ne!(twin_of, in_order, "the twins are in the order of their descriptors");
        // a twin is its descriptor moved by the noise (2 % of 512 at most) and both are rounded to
        // bytes (sqrt(128) / 2 each at most); the noise leaves no twin with the bytes of its descriptor
        let far = NOISE * NORM + (gpu::DIM as f64).sqrt();
        for (twin, &of) in b.chunks_exact(gpu::DIM).zip(&twin_of) {
            let descriptor = &a[of * gpu::DIM..][..gpu::DIM];
            let apart = norm(twin.iter().zip(descriptor).map(|(&t, &d)| f64::from(t) - f64::from(d)));
            assert!(apart > 0.0 && apart <= far, "the twin of descriptor {of} is {apart} away");
        }
    }
}
