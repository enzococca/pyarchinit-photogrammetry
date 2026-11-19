//! `ppg-bench` run as a script or a CI job runs it: the exit code says whether it worked, and
//! what it prints on the standard output is JSON.

use std::process::{Command, Output};

use ppg_engine::gpu;
use serde_json::{json, Value};

/// The binary run with `args`.
fn ppg_bench(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_ppg-bench")).args(args).output().expect("ppg-bench starts")
}

/// What a run that worked printed: exit code 0 and one JSON value on the standard output.
fn printed(args: &[&str]) -> Value {
    let output = ppg_bench(args);
    let said = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(0), "ppg-bench {args:?} said: {said}");
    let stdout = String::from_utf8(output.stdout).expect("the output is UTF-8");
    serde_json::from_str(&stdout).unwrap_or_else(|e| panic!("ppg-bench {args:?} printed {stdout:?}: {e}"))
}

/// The keys of a JSON object, sorted.
fn keys(object: &Value) -> Vec<&str> {
    let mut keys: Vec<&str> = object.as_object().expect("a JSON object").keys().map(String::as_str).collect();
    keys.sort_unstable();
    keys
}

/// The adapter `match` takes when none is named: the first that is no software rasteriser.
fn first_gpu() -> Option<gpu::AdapterInfo> {
    gpu::adapters().into_iter().find(|adapter| adapter.device_type != "Cpu")
}

#[test]
fn adapters_prints_the_adapters_of_the_engine_as_a_json_array() {
    let listed = printed(&["adapters"]);
    let listed = listed.as_array().expect("a JSON array");
    // no adapter is a valid answer (CI runners): an empty array
    let adapters = gpu::adapters();
    assert_eq!(listed.len(), adapters.len());
    for (adapter, object) in adapters.iter().zip(listed) {
        // the fields AdapterInfo has, nothing added
        assert_eq!(keys(object), ["backend", "device_type", "index", "name"]);
        assert_eq!(object["index"], json!(adapter.index));
        assert_eq!(object["name"], json!(adapter.name));
        assert_eq!(object["backend"], json!(adapter.backend));
        assert_eq!(object["device_type"], json!(adapter.device_type));
    }
}

/// What every record of `match` on `n` descriptors says, whatever the device: the matches join
/// descriptors to their twins only, all but a handful of them, in a time.
fn joins_the_twins(record: &Value, n: u64) -> u64 {
    assert_eq!(record["n"], json!(n));
    assert_eq!(record["dim"], json!(gpu::DIM));
    let matches = record["matches"].as_u64().expect("matches is a whole number");
    assert_eq!(record["correct"], json!(matches), "a match joins a descriptor to another than its twin: {record}");
    // n less a margin of six for twins that differ by noise of 2 % of the norm: not measured
    assert!(matches >= n - 6, "{matches} matches of {n}: {record}");
    assert!(record["seconds"].as_f64().is_some_and(|seconds| seconds > 0.0), "{record}");
    matches
}

#[test]
fn match_joins_the_descriptors_to_their_twins_or_says_that_it_skipped() {
    let record = printed(&["match", "--n", "512"]);
    let Some(adapter) = first_gpu() else {
        assert_eq!(record, json!({"skipped": "no adapter"}));
        return;
    };
    assert_eq!(keys(&record), ["correct", "device", "dim", "matches", "n", "seconds", "workgroup_kb"], "{record}");
    assert_eq!(record["device"], json!(adapter.name));
    joins_the_twins(&record, 512);
    // the tiles the matcher chose from the adapter's workgroup memory
    assert!(record["workgroup_kb"] == json!(16) || record["workgroup_kb"] == json!(32), "{record}");
}

#[test]
fn match_without_options_takes_4096_descriptors() {
    let record = printed(&["match"]);
    if first_gpu().is_some() {
        assert_eq!(record["n"], json!(4096), "{record}");
        assert!(record["workgroup_kb"] == json!(16) || record["workgroup_kb"] == json!(32), "{record}");
    } else {
        assert_eq!(record, json!({"skipped": "no adapter"}));
    }
}

#[test]
fn match_on_the_cpu_joins_the_descriptors_to_their_twins_on_any_computer() {
    // the CPU twin needs no adapter: never skipped, no workgroup memory to report
    let record = printed(&["match", "--n", "512", "--cpu"]);
    assert_eq!(keys(&record), ["correct", "device", "dim", "matches", "n", "seconds"], "{record}");
    assert_eq!(record["device"], json!("cpu"));
    joins_the_twins(&record, 512);
}

#[test]
fn match_with_small_tiles_gives_the_matches_of_the_large_ones_and_of_the_cpu() {
    let cpu = joins_the_twins(&printed(&["match", "--n", "512", "--cpu"]), 512);
    let Some(adapter) = first_gpu() else {
        assert_eq!(printed(&["match", "--n", "512", "--workgroup-kb", "16"]), json!({"skipped": "no adapter"}));
        return;
    };
    // the small tiles on any adapter; the large ones where the adapter has the memory
    let small = printed(&["match", "--n", "512", "--workgroup-kb", "16"]);
    assert_eq!(small["device"], json!(adapter.name));
    assert_eq!(small["workgroup_kb"], json!(16), "{small}");
    assert_eq!(joins_the_twins(&small, 512), cpu, "the small tiles and the CPU twin disagree");
    let chosen = printed(&["match", "--n", "512"]);
    assert_eq!(joins_the_twins(&chosen, 512), cpu, "the tiles the matcher chose and the CPU twin disagree");
    if chosen["workgroup_kb"] == json!(32) {
        let large = printed(&["match", "--n", "512", "--workgroup-kb", "32"]);
        assert_eq!(large["workgroup_kb"], json!(32), "{large}");
        assert_eq!(joins_the_twins(&large, 512), cpu, "the large tiles and the CPU twin disagree");
    }
}

#[test]
fn an_adapter_that_is_named_and_does_not_exist_is_an_error() {
    let beyond = gpu::adapters().len().to_string(); // one more than the last index
    let output = ppg_bench(&["match", "--n", "16", "--device", &beyond]);
    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty(), "printed {:?}", String::from_utf8_lossy(&output.stdout));
    assert!(!output.stderr.is_empty(), "the error is not said");
}

#[test]
fn adjust_brings_a_synthetic_block_back_and_says_which_solver_it_used() {
    let record = printed(&["adjust", "--photos", "20", "--points", "300"]);
    let expected = ["cost", "cost_initial", "iterations", "observations", "photos", "points", "seconds", "solver"];
    assert_eq!(keys(&record), expected, "{record}");
    assert_eq!(record["photos"], json!(20));
    assert_eq!(record["points"], json!(300));
    assert!(record["observations"].as_u64().is_some_and(|n| n > 300), "{record}");
    // 20 photos are far below the dense limit: `auto` is the dense solver
    assert_eq!(record["solver"], json!("dense"));
    assert!(record["iterations"].as_u64().is_some_and(|k| k > 0), "{record}");
    assert!(record["seconds"].as_f64().is_some_and(|seconds| seconds > 0.0), "{record}");
    // the observations are exact: the perturbed block comes back to a cost of nothing
    let (cost, initial) = (record["cost"].as_f64().expect("cost"), record["cost_initial"].as_f64().expect("initial"));
    assert!(initial > 0.0 && cost < 1e-12 * initial, "{record}");
}

#[test]
fn adjust_takes_the_solver_and_the_seed_it_is_given() {
    let mut costs = Vec::new();
    for (word, used) in [("dense", "dense"), ("sparse", "sparse"), ("pcg", "pcg"), ("auto", "dense")] {
        let record = printed(&["adjust", "--photos", "6", "--points", "60", "--solver", word, "--seed", "2"]);
        assert_eq!(record["solver"], json!(used), "{record}");
        assert_eq!((&record["photos"], &record["points"]), (&json!(6), &json!(60)), "{record}");
        let cost = record["cost"].as_f64().expect("cost");
        let initial = record["cost_initial"].as_f64().expect("initial");
        assert!(cost < 1e-12 * initial, "--solver {word}: {record}");
        costs.push(initial);
    }
    // the same seed gives the same block to every solver; another seed another block
    assert!(costs.iter().all(|c| *c == costs[0]), "{costs:?}");
    let other = printed(&["adjust", "--photos", "6", "--points", "60", "--seed", "3"]);
    assert_ne!(other["cost_initial"].as_f64(), Some(costs[0]), "{other}");
}

#[test]
fn a_command_line_the_binary_does_not_know_is_refused() {
    let refused: [&[&str]; 19] = [
        &[],                           // no command
        &["bogus"],                    // not a command
        &["adapters", "--n", "8"],     // adapters takes no option
        &["match", "--dim", "64"],     // the descriptors of the kernel have one length: there is no --dim
        &["match", "--n"],             // no value
        &["match", "--n", "many"],     // not a number
        &["match", "--n", "0"],        // nothing to match
        &["match", "--device", "-1"],  // not an index
        &["match", "--workgroup-kb"],  // no value
        &["match", "--workgroup-kb", "24"],          // the kernels come in 16 and 32 KiB
        &["match", "--cpu", "--device", "0"],        // the CPU twin has no adapter ...
        &["match", "--cpu", "--workgroup-kb", "16"], // ... and no workgroup memory
        &["match", "--n", "32769", "--workgroup-kb", "16"], // beyond the bound of the small tiles
        &["adjust", "--n", "8"],       // adjust has no --n
        &["adjust", "--photos", "0"],  // no photo to adjust
        &["adjust", "--points", "0"],  // no point to adjust
        &["adjust", "--solver"],       // no value
        &["adjust", "--solver", "lu"], // not one of the solvers
        &["adjust", "--seed", "-1"],   // not a seed
    ];
    for args in refused {
        let output = ppg_bench(args);
        let said = String::from_utf8_lossy(&output.stderr);
        assert_eq!(output.status.code(), Some(2), "ppg-bench {args:?} said: {said}");
        assert!(output.stdout.is_empty(), "ppg-bench {args:?} printed {:?}", String::from_utf8_lossy(&output.stdout));
        assert!(said.contains("usage: ppg-bench"), "ppg-bench {args:?} said: {said}");
    }
}

#[test]
fn a_size_beyond_the_largest_is_refused_before_the_kernel_is_called() {
    // 46336 is the most the binary takes with the large tiles (128 descriptors) and 32768 with the
    // small ones (64): one more is refused as `--n 0` is, and so is the largest number the option
    // reads, whatever adapters the computer has; the refusal names the bound and the tiles
    let largest = usize::MAX.to_string();
    let small: &[&str] = &["--workgroup-kb", "16"];
    let beyond = [("46337", &[][..], "46336", "128"), (largest.as_str(), &[], "46336", "128"),
                  ("32769", small, "32768", "64"), ("46336", small, "32768", "64")];
    for (n, options, most, rows) in beyond {
        let mut args = vec!["match", "--n", n];
        args.extend(options);
        let output = ppg_bench(&args);
        let said = String::from_utf8_lossy(&output.stderr);
        assert_eq!(output.status.code(), Some(2), "ppg-bench {args:?} said: {said}");
        assert!(output.stdout.is_empty(), "{args:?} printed {:?}", String::from_utf8_lossy(&output.stdout));
        let start = format!("ppg-bench: --n takes {most} at most with tiles of {rows} descriptors");
        assert!(said.starts_with(&start), "ppg-bench {args:?} said: {said}");
        assert!(said.contains("usage: ppg-bench"), "ppg-bench {args:?} said: {said}");
    }
}
