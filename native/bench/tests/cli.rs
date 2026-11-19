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

#[test]
fn match_joins_the_descriptors_to_their_twins_or_says_that_it_skipped() {
    let record = printed(&["match", "--n", "512"]);
    let Some(adapter) = first_gpu() else {
        assert_eq!(record, json!({"skipped": "no adapter"}));
        return;
    };
    assert_eq!(keys(&record), ["correct", "device", "dim", "matches", "n", "seconds"], "{record}");
    assert_eq!(record["device"], json!(adapter.name));
    assert_eq!(record["n"], json!(512));
    assert_eq!(record["dim"], json!(gpu::DIM));
    let matches = record["matches"].as_u64().expect("matches is a whole number");
    assert_eq!(record["correct"], json!(matches), "a match joins a descriptor to another than its twin: {record}");
    // 512 less a margin of six for twins that differ by noise of 2 % of the norm: not measured
    assert!(matches >= 506, "{matches} matches of 512: {record}");
    assert!(record["seconds"].as_f64().is_some_and(|seconds| seconds > 0.0), "{record}");
}

#[test]
fn match_without_options_takes_4096_descriptors() {
    let record = printed(&["match"]);
    if first_gpu().is_some() {
        assert_eq!(record["n"], json!(4096), "{record}");
    } else {
        assert_eq!(record, json!({"skipped": "no adapter"}));
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
    let refused: [&[&str]; 14] = [
        &[],                           // no command
        &["bogus"],                    // not a command
        &["adapters", "--n", "8"],     // adapters takes no option
        &["match", "--dim", "64"],     // the descriptors of the kernel have one length: there is no --dim
        &["match", "--n"],             // no value
        &["match", "--n", "many"],     // not a number
        &["match", "--n", "0"],        // nothing to match
        &["match", "--device", "-1"],  // not an index
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
    // 46336 is the most the binary takes (its MAX_N): one more is refused as `--n 0` is, and so is
    // the largest number the option reads, whatever adapters the computer has
    for n in [String::from("46337"), usize::MAX.to_string()] {
        let output = ppg_bench(&["match", "--n", &n]);
        let said = String::from_utf8_lossy(&output.stderr);
        assert_eq!(output.status.code(), Some(2), "ppg-bench match --n {n} said: {said}");
        assert!(output.stdout.is_empty(), "--n {n} printed {:?}", String::from_utf8_lossy(&output.stdout));
        assert!(said.starts_with("ppg-bench: --n takes 46336 at most"), "ppg-bench match --n {n} said: {said}");
        assert!(said.contains("usage: ppg-bench"), "ppg-bench match --n {n} said: {said}");
    }
}
