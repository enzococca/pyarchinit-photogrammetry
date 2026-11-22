//! The Levenberg-Marquardt of the bundle adjustment, generic over the residual (`bundle::lm`):
//! the bundle adjustment of milestone 1 keeps its solution (recorded in
//! `tests/data/grid_block_60_400_3.json` from the code before the solver was made generic), a
//! model whose eliminated blocks are not points of three coordinates and have floors, a loss per
//! residual block.

use std::path::PathBuf;

use ppg_engine::bundle::synthetic::grid_block;
use ppg_engine::bundle::lm::{solve_model, BlockJacobian, Model};
use ppg_engine::bundle::{solve, LinearSolver, Loss, Options, Problem, Solution};
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
        let (mut worst_centre, mut worst_angle) = (0.0f64, 0.0f64);
        for (i, (c, r)) in centres.iter().zip(rotations).enumerate() {
            let c: Vec<f64> = c.as_array().unwrap().iter().map(|x| x.as_f64().unwrap()).collect();
            let r: Vec<f64> = r.as_array().unwrap().iter().map(|x| x.as_f64().unwrap()).collect();
            let off = (0..3).map(|k| (sol.centres[i][k] - c[k]).abs()).fold(0.0, f64::max);
            assert!(off <= 1e-10, "{name}: centre {i} is {off} m from the recorded one");
            let rec = [[r[0], r[1], r[2]], [r[3], r[4], r[5]], [r[6], r[7], r[8]]];
            let angle = angle_between(&sol.rotations[i], &rec);
            assert!(angle <= 1e-10, "{name}: rotation {i} is {angle} rad from the recorded one");
            (worst_centre, worst_angle) = (worst_centre.max(off), worst_angle.max(angle));
        }
        let cost_off = (sol.cost - cost).abs() / cost;
        eprintln!("{name}: cost {cost_off:e} relative, centres {worst_centre:e} m, rotations {worst_angle:e} rad");
    }
}

/// The points of a line fitted by rays: one camera block, the anchor `o` of a line of direction
/// `E`, and a point block per point, its position `x` and the scales of its two rays. Residuals:
/// the point on the line at its parameter `t` (`x - o - t E`), the point on each of its rays
/// (`x - origin - scale direction`). Every residual is linear in the unknowns: the minimum is the
/// solution of the normal equations, with the scales at their floor where the floor holds.
struct RayLine {
    blocks: Vec<usize>,
    points: Vec<usize>,
    t: Vec<f64>,
    rays: Vec<[([f64; 3], [f64; 3]); 2]>,
    bounds: Vec<(usize, f64)>,
    anchor: [f64; 3],
    unknowns: Vec<[f64; 5]>,
    /// The lowest scale any step has left.
    lowest: f64,
}

const E: [f64; 3] = [1.0, 0.0, 0.0];
const FLOOR: f64 = 1e-3;

impl RayLine {
    fn new() -> RayLine {
        let n = 4;
        let truth: Vec<[f64; 3]> = (0..n).map(|p| [2.0 + p as f64, 1.0, 3.0]).collect();
        let t: Vec<f64> = (0..n).map(|p| p as f64).collect();
        let mut rays = Vec::new();
        for (p, x) in truth.iter().enumerate() {
            let d1 = normalized([0.3, -0.2 + 0.1 * p as f64, 1.0]);
            let d2 = normalized([-0.5, 0.4, 1.0 - 0.1 * p as f64]);
            // the rays miss the point by a few centimetres, so that the minimum is not zero
            let miss = 0.03 * (p as f64 + 1.0);
            let o1 = [x[0] - 4.0 * d1[0] + miss, x[1] - 4.0 * d1[1], x[2] - 4.0 * d1[2]];
            // the second ray of the first point passes 0.5 beyond it: its free scale would be
            // negative, the floor holds it
            let s2 = if p == 0 { -0.5 } else { 5.0 };
            let o2 = [x[0] - s2 * d2[0], x[1] - s2 * d2[1] - miss, x[2] - s2 * d2[2]];
            rays.push([(o1, d1), (o2, d2)]);
        }
        let bounds = (0..n).flat_map(|p| [(5 * p + 3, FLOOR), (5 * p + 4, FLOOR)]).collect();
        let unknowns = truth.iter().map(|x| [x[0] + 0.2, x[1] - 0.1, x[2] + 0.3, 3.0, 3.0]).collect();
        RayLine {
            blocks: vec![3],
            points: vec![5; n],
            t,
            rays,
            bounds,
            anchor: [1.5, 0.5, 2.5],
            unknowns,
            lowest: f64::INFINITY,
        }
    }

    /// The minimum in closed form: the normal equations of the linear residuals, the scales of
    /// `held` fixed at the floor. Unknowns: the anchor, then the five of every point.
    fn closed_form(&self, held: &[usize]) -> Vec<f64> {
        let n = 3 + 5 * self.points.len();
        let mut rows: Vec<(Vec<f64>, f64)> = Vec::new();
        for p in 0..self.points.len() {
            for k in 0..3 {
                // x_k - o_k = t E_k
                let mut a = vec![0.0; n];
                a[3 + 5 * p + k] = 1.0;
                a[k] = -1.0;
                rows.push((a, self.t[p] * E[k]));
            }
            for (i, (origin, d)) in self.rays[p].iter().enumerate() {
                for k in 0..3 {
                    // x_k - scale d_k = origin_k
                    let mut a = vec![0.0; n];
                    a[3 + 5 * p + k] = 1.0;
                    a[3 + 5 * p + 3 + i] = -d[k];
                    rows.push((a, origin[k]));
                }
            }
        }
        let free: Vec<usize> = (0..n).filter(|c| !held.contains(c)).collect();
        let m = free.len();
        let mut h = vec![0.0; m * m];
        let mut b = vec![0.0; m];
        for (a, mut rhs) in rows {
            for &c in held {
                rhs -= a[c] * FLOOR;
            }
            for (i, &ci) in free.iter().enumerate() {
                b[i] += a[ci] * rhs;
                for (j, &cj) in free.iter().enumerate() {
                    h[i * m + j] += a[ci] * a[cj];
                }
            }
        }
        // Gauss-Jordan with partial pivoting
        for col in 0..m {
            let pivot = (col..m).max_by(|&x, &y| h[x * m + col].abs().total_cmp(&h[y * m + col].abs())).unwrap();
            for j in 0..m {
                h.swap(col * m + j, pivot * m + j);
            }
            b.swap(col, pivot);
            for r in 0..m {
                if r != col {
                    let f = h[r * m + col] / h[col * m + col];
                    for j in 0..m {
                        h[r * m + j] -= f * h[col * m + j];
                    }
                    b[r] -= f * b[col];
                }
            }
        }
        let mut x = vec![FLOOR; n];
        for (i, &c) in free.iter().enumerate() {
            x[c] = b[i] / h[i * m + i];
        }
        x
    }
}

fn normalized(v: [f64; 3]) -> [f64; 3] {
    let n = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt();
    [v[0] / n, v[1] / n, v[2] / n]
}

impl Model for RayLine {
    fn camera_blocks(&self) -> &[usize] {
        &self.blocks
    }

    fn point_blocks(&self) -> &[usize] {
        &self.points
    }

    fn residual_blocks(&self) -> usize {
        3 * self.points.len()
    }

    fn linearize(&self, k: usize, out: &mut BlockJacobian) {
        let (p, kind) = (k / 3, k % 3);
        let u = &self.unknowns[p];
        out.clear();
        out.point = Some(p);
        out.loss = Loss::None;
        if kind == 0 {
            out.cameras.push(0);
            for row in 0..3 {
                out.residuals.push(u[row] - self.anchor[row] - self.t[p] * E[row]);
                // the anchor, then the five of the point
                let mut jac = [0.0; 8];
                jac[row] = -1.0;
                jac[3 + row] = 1.0;
                out.jacobian.extend_from_slice(&jac);
            }
        } else {
            let (origin, d) = self.rays[p][kind - 1];
            let scale = u[3 + kind - 1];
            for row in 0..3 {
                out.residuals.push(u[row] - origin[row] - scale * d[row]);
                let mut jac = [0.0; 5];
                jac[row] = 1.0;
                jac[3 + kind - 1] = -d[row];
                out.jacobian.extend_from_slice(&jac);
            }
        }
    }

    fn apply(&mut self, delta_cameras: &[f64], delta_points: &[f64]) {
        for k in 0..3 {
            self.anchor[k] += delta_cameras[k];
        }
        for (p, u) in self.unknowns.iter_mut().enumerate() {
            for k in 0..5 {
                u[k] += delta_points[5 * p + k];
            }
            self.lowest = self.lowest.min(u[3]).min(u[4]);
        }
    }

    fn save(&self) -> Vec<f64> {
        self.anchor.iter().chain(self.unknowns.iter().flatten()).copied().collect()
    }

    fn restore(&mut self, saved: &[f64]) {
        self.anchor.copy_from_slice(&saved[..3]);
        for (p, u) in self.unknowns.iter_mut().enumerate() {
            u.copy_from_slice(&saved[3 + 5 * p..8 + 5 * p]);
        }
    }

    fn lower_bounds(&self) -> &[(usize, f64)] {
        &self.bounds
    }

    fn point_coordinate(&self, index: usize) -> f64 {
        self.unknowns[index / 5][index % 5]
    }
}

#[test]
fn a_model_with_variable_point_blocks() {
    let mut m = RayLine::new();
    let held = [3 + 4];
    let want = m.closed_form(&held);
    // the floor is what holds the second scale of the first point: without it the minimum is
    // below the floor
    assert!(m.closed_form(&[])[3 + 4] < 0.0);
    for solver in [LinearSolver::Dense, LinearSolver::Sparse, LinearSolver::Pcg, LinearSolver::Auto] {
        let mut m = RayLine::new();
        let opts = Options { linear_solver: solver, ..Options::default() };
        let mut iterations = 0;
        let report = solve_model(&mut m, &opts, &mut |i, _| {
            iterations = i;
            true
        })
        .unwrap();
        assert_eq!(report.iterations, iterations);
        assert!(report.cost < report.cost_initial, "{solver:?}");
        let got = m.save();
        for (k, (a, b)) in got.iter().zip(&want).enumerate() {
            assert!((a - b).abs() <= 1e-9, "{solver:?}: unknown {k} is {a}, the closed form {b}");
        }
        assert!(m.lowest >= FLOOR, "{solver:?}: a step left a scale at {}", m.lowest);
        // the second scale of the first point (its coordinate 4) on its floor
        assert!((m.point_coordinate(4) - FLOOR).abs() <= 1e-15, "{}", m.point_coordinate(4));
    }
    m.restore(&want);
    assert_eq!(m.save(), want);
}

/// A scalar `x` pulled by three residual blocks `x - a`: two under Huber, one squared.
struct Pulled {
    blocks: Vec<usize>,
    x: f64,
}

const PULLS: [(f64, Loss); 3] = [(0.0, Loss::Huber(1.0)), (5.0, Loss::Huber(1.0)), (-4.0, Loss::None)];

impl Model for Pulled {
    fn camera_blocks(&self) -> &[usize] {
        &self.blocks
    }

    fn point_blocks(&self) -> &[usize] {
        &[]
    }

    fn residual_blocks(&self) -> usize {
        PULLS.len()
    }

    fn linearize(&self, k: usize, out: &mut BlockJacobian) {
        out.clear();
        out.cameras.push(0);
        out.residuals.push(self.x - PULLS[k].0);
        out.jacobian.push(1.0);
        out.loss = PULLS[k].1;
    }

    fn apply(&mut self, delta_cameras: &[f64], _: &[f64]) {
        self.x += delta_cameras[0];
    }

    fn save(&self) -> Vec<f64> {
        vec![self.x]
    }

    fn restore(&mut self, saved: &[f64]) {
        self.x = saved[0];
    }
}

fn pulled_cost(x: f64) -> f64 {
    0.5 * PULLS.iter().map(|(a, loss)| loss.rho((x - a) * (x - a))).sum::<f64>()
}

#[test]
fn a_loss_per_residual_block() {
    let mut m = Pulled { blocks: vec![1], x: 10.0 };
    let report = solve_model(&mut m, &Options::default(), &mut |_, _| true).unwrap();
    let initial = pulled_cost(10.0);
    assert!((report.cost_initial - initial).abs() <= 1e-14 * initial, "{} vs {initial}", report.cost_initial);
    let cost = pulled_cost(m.x);
    assert!((report.cost - cost).abs() <= 1e-14 * cost, "{} vs {cost}", report.cost);
    // the minimum: both Huber pulls on their linear part (slopes -1 and -1) against the squared
    // pull from -4: (x + 4) - 2 = 0
    assert!((m.x + 2.0).abs() < 1e-6, "x = {}", m.x);
    assert!(report.cost < initial);
}
