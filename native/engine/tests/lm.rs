//! The Levenberg-Marquardt of the bundle adjustment, generic over the residual (`bundle::lm`):
//! the bundle adjustment of milestone 1 keeps its solution (recorded in
//! `tests/data/grid_block_60_400_3.json` from the code before the solver was made generic), a
//! model whose eliminated blocks are not points of three coordinates and have floors, a loss per
//! residual block.

use std::path::PathBuf;

use ppg_engine::bundle::synthetic::grid_block;
use ppg_engine::bundle::{solve, LinearSolver, Options, Problem, Solution};
use serde_json::Value;

/// The two solvers the recorded solution holds.
const RECORDED: [(&str, LinearSolver); 2] = [("dense", LinearSolver::Dense), ("sparse", LinearSolver::Sparse)];

/// SplitMix64 with a fixed seed: the noise of the recorded block, the same at every run.
struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    fn uniform(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }

    /// Box-Muller.
    fn normal(&mut self) -> f64 {
        let u1 = self.uniform().max(1e-300);
        let u2 = self.uniform();
        (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos()
    }
}

/// The grid block of 60 photos and 24 000 points with 0.3 px of noise on its tie observations, so
/// that the minimum of the cost is a number and not the round-off of an exact block.
fn recorded_block() -> Problem {
    let (mut pb, _) = grid_block(60, 400, 3);
    let mut rng = Rng(30);
    for o in pb.obs.iter_mut() {
        o.xy = [o.xy[0] + 0.3 * rng.normal(), o.xy[1] + 0.3 * rng.normal()];
    }
    pb
}

fn record_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/data/grid_block_60_400_3.json")
}

fn number(x: f64) -> String {
    format!("{x:.16e}")
}

fn solution_json(sol: &Solution) -> String {
    let centres: Vec<String> =
        sol.centres.iter().map(|c| format!("[{}]", c.map(number).join(", "))).collect();
    let rotations: Vec<String> = sol
        .rotations
        .iter()
        .map(|r| format!("[{}]", r.iter().flatten().map(|x| number(*x)).collect::<Vec<_>>().join(", ")))
        .collect();
    format!(
        "{{\"cost\": {}, \"iterations\": {},\n  \"centres\": [\n    {}],\n  \"rotations\": [\n    {}]}}",
        number(sol.cost),
        sol.iterations,
        centres.join(",\n    "),
        rotations.join(",\n    ")
    )
}

/// Writes the recorded solution (run once, with `--ignored`, on the code before the refactor).
#[test]
#[ignore]
fn record_the_solution() {
    let pb = recorded_block();
    let mut parts = Vec::new();
    for (name, solver) in RECORDED {
        let opts = Options { linear_solver: solver, ..Options::default() };
        let sol = solve(&pb, &opts, &mut |_, _| true).unwrap();
        assert_eq!(sol.linear_solver, solver);
        parts.push(format!("\"{name}\": {}", solution_json(&sol)));
    }
    let path = record_path();
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(&path, format!("{{{}}}\n", parts.join(",\n"))).unwrap();
}

/// The angle between two rotations, from the skew part of `A B^T` (accurate for small angles).
fn angle_between(a: &[[f64; 3]; 3], b: &[[f64; 3]; 3]) -> f64 {
    let mut m = [[0.0; 3]; 3];
    for i in 0..3 {
        for j in 0..3 {
            m[i][j] = (0..3).map(|k| a[i][k] * b[j][k]).sum();
        }
    }
    let v = [m[2][1] - m[1][2], m[0][2] - m[2][0], m[1][0] - m[0][1]];
    0.5 * (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt()
}

#[test]
fn the_refactor_keeps_the_solution() {
    let text = std::fs::read_to_string(record_path()).expect("the recorded solution");
    let record: Value = serde_json::from_str(&text).unwrap();
    let pb = recorded_block();
    for (name, solver) in RECORDED {
        let want = &record[name];
        let opts = Options { linear_solver: solver, ..Options::default() };
        let sol = solve(&pb, &opts, &mut |_, _| true).unwrap();
        assert_eq!(sol.linear_solver, solver);
        assert_eq!(sol.iterations as u64, want["iterations"].as_u64().unwrap(), "{name}: iterations");
        let cost = want["cost"].as_f64().unwrap();
        assert!((sol.cost - cost).abs() <= 1e-12 * cost, "{name}: cost {} against {cost}", sol.cost);
        let centres = want["centres"].as_array().unwrap();
        let rotations = want["rotations"].as_array().unwrap();
        assert_eq!((centres.len(), rotations.len()), (sol.centres.len(), sol.rotations.len()));
        for (i, (c, r)) in centres.iter().zip(rotations).enumerate() {
            let c: Vec<f64> = c.as_array().unwrap().iter().map(|x| x.as_f64().unwrap()).collect();
            let r: Vec<f64> = r.as_array().unwrap().iter().map(|x| x.as_f64().unwrap()).collect();
            let off = (0..3).map(|k| (sol.centres[i][k] - c[k]).abs()).fold(0.0, f64::max);
            assert!(off <= 1e-10, "{name}: centre {i} is {off} m from the recorded one");
            let rec = [[r[0], r[1], r[2]], [r[3], r[4], r[5]], [r[6], r[7], r[8]]];
            let angle = angle_between(&sol.rotations[i], &rec);
            assert!(angle <= 1e-10, "{name}: rotation {i} is {angle} rad from the recorded one");
        }
    }
}
