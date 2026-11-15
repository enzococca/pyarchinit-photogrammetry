//! `ppg-bench`: the kernels of `ppg_engine` run without Python, to see what a computer or a CI
//! runner has and how long a kernel takes on it. What it prints on the standard output is JSON;
//! an error goes to the standard error with exit code 1, a command line it does not know with
//! exit code 2.
//!
//! - `ppg-bench adapters`: the GPU adapters wgpu finds, an array with the `index`, `name`,
//!   `backend` and `device_type` of each.
//! - `ppg-bench match [--n 4096] [--device N]`: the matching of descriptors, timed on two
//!   generated sets of `n` descriptors of which the right matches are known; it prints `device`
//!   (the name of the adapter), `n`, `dim`, `matches`, `correct` and `seconds` (the time of one
//!   call, the first on a new matcher: not a repeated or a warmed timing). Without `--device`
//!   the first adapter that is no software rasteriser is taken; when there is none the answer
//!   is `{"skipped": "no adapter"}`, with exit code 0. `n` is 46336 at most (`MAX_N`): more is
//!   a command line the binary refuses, before a device is opened.

use std::process::ExitCode;
use std::time::Instant;

use ppg_engine::gpu;
use serde_json::{json, Value};

/// The descriptors of each set when `--n` is not given: a choice, not a measured figure.
const DEFAULT_N: usize = 4096;

/// The most descriptors `--n` takes: derived, not measured. The buffer of the matching kernel
/// that grows fastest holds its partial results, 8 bytes for every tile of the first set (128
/// descriptors, `TILE_A` in gpu.rs of the engine; the last tile counts whole) and every
/// descriptor of the second, and the kernel binds it whole as a storage buffer: for two sets of
/// `n` that is ceil(n / 128) * n * 8 bytes, within 128 MiB up to 46336 (362 tiles, 134 189 056
/// bytes) and above it from 46337 (363 tiles, 134 562 648 bytes). 128 MiB is the storage binding
/// wgpu 24 promises on every modern backend (`max_storage_buffer_binding_size` of
/// `Limits::default()`). The engine does not say what the device it opens holds, and with a
/// buffer beyond that the kernel, which has no error scope, ends in a panic of wgpu: the figure
/// is what can be said without the device. Many devices hold more, and an adapter below the
/// defaults of wgpu (a downlevel one) holds less.
const MAX_N: usize = 46336;

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

/// The matching of two sets of `n` descriptors on the adapter `device`, timed: the record to
/// print.
fn time_matching(n: usize, device: Option<usize>) -> Result<Value, String> {
    let index = match device {
        Some(index) => index,
        // a software rasteriser (the type wgpu calls Cpu) is not timed as if it were a GPU: it is
        // taken only when it is named
        None => match gpu::adapters().into_iter().find(|adapter| adapter.device_type != "Cpu") {
            Some(adapter) => adapter.index,
            None => return Ok(json!({"skipped": "no adapter"})),
        },
    };
    let mut matcher = gpu::Matcher::new(Some(index))?;
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
    let correct = matches.iter().filter(|pair| twin_of[pair[1] as usize] == pair[0] as usize).count();
    Ok(json!({"device": matcher.info.name, "n": n, "dim": gpu::DIM, "matches": matches.len(),
              "correct": correct, "seconds": seconds}))
}

enum Command {
    Adapters,
    Match { n: usize, device: Option<usize> },
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
            let (mut n, mut device) = (DEFAULT_N, None);
            while let Some(option) = words.next() {
                match option {
                    "--n" => {
                        n = whole_number(option, words.next())?;
                        if n == 0 {
                            return Err("--n takes 1 or more: two empty sets have nothing to match".into());
                        }
                        if n > MAX_N {
                            return Err(format!(
                                "--n takes {MAX_N} at most: the kernel's working buffer for more is above the \
                                 128 MiB a device is sure to bind"
                            ));
                        }
                    }
                    "--device" => device = Some(whole_number(option, words.next())?),
                    _ => return Err(format!("match has no option {option}")),
                }
            }
            Ok(Command::Match { n, device })
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
        Ok(Command::Match { n, device }) => time_matching(n, device),
        Err(problem) => {
            eprintln!("ppg-bench: {problem}");
            eprintln!("usage: ppg-bench adapters");
            eprintln!("       ppg-bench match [--n {DEFAULT_N}] [--device N]");
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
        // every tile of 128 descriptors of the first and every descriptor of the second), against
        // the 128 MiB of a storage binding: the two figures MAX_N is derived from
        let partials = |n: usize| n.div_ceil(128) * n * 8;
        let binding = 128 << 20;
        assert!(partials(MAX_N) <= binding, "{} bytes for {MAX_N} descriptors", partials(MAX_N));
        assert!(partials(MAX_N + 1) > binding, "{} bytes for one descriptor more", partials(MAX_N + 1));
    }

    #[test]
    fn the_largest_size_is_taken_and_one_more_is_refused() {
        let (most, more) = (MAX_N.to_string(), (MAX_N + 1).to_string());
        let taken = command(&words(&["match", "--n", &most]));
        assert!(matches!(taken, Ok(Command::Match { n: MAX_N, device: None })), "{most} descriptors are refused");
        // the refusal says how many are taken, with or without an adapter named
        let refused = command(&words(&["match", "--n", &more, "--device", "0"])).err();
        assert!(refused.as_ref().is_some_and(|problem| problem.contains(&most)), "{more} descriptors: {refused:?}");
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
