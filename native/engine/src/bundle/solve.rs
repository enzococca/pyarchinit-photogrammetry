//! Levenberg-Marquardt over the reduced camera system: the diagonal of the normal equations
//! damped (Marquardt), a step taken when the cost falls, the damping moved by the quality of
//! the step (Nielsen's rule, as in Madsen, Nielsen and Tingleff), the rotations updated on
//! their manifold by `exp(delta) R`. Stops on the relative decrease of the cost, the largest
//! entry of the gradient, the size of the step, the number of iterations, or when no damping
//! gives a step that lowers the cost.

use super::pcg::pcg_step;
use super::problem::{Layout, Parameters, Problem};
use super::residuals::{evaluate_blocks, gradient_and_diagonal, refine_unheld_points, Evaluated, Loss, Structure};
use super::schur::{dense_step, Reduced};
use super::sparse::SparseSystem;

/// The free photos up to which the reduced system is factorised dense (spec 8.1), when nothing
/// but their poses is adjusted: the limit `Auto` applies is `DENSE_MAX_COLUMNS`.
pub const DENSE_MAX_PHOTOS: usize = 2500;
/// Up to this many columns of the reduced system (the pose columns of `DENSE_MAX_PHOTOS` free
/// photos: a dense matrix of 1.8 GB) `Auto` factorises it dense, sparse above. The limit is
/// counted in columns, not in photos: with the rolling shutter a photo has twelve columns and
/// 2500 photos would make a matrix of 7.2 GB.
pub const DENSE_MAX_COLUMNS: usize = 6 * DENSE_MAX_PHOTOS;
/// Under Huber and Cauchy the function tolerance is at least this, Ceres's default: the cost of a
/// wrong track is a plateau along which the adjustment gains 1e-8 to 1e-7 of the cost per iteration
/// for ever, long after the rest of the block has converged.
pub const ROBUST_FUNCTION_TOLERANCE: f64 = 1e-6;
/// A sparse factor whose values would take more bytes than this (a quarter of the memory budget of
/// 16 GB) is replaced by the conjugate gradients; the pattern of the matrix and faer's workspace
/// are not counted.
pub const PCG_ABOVE_BYTES: u64 = 4 << 30;

/// The damping starts here (a trust region of 1e4 of Marquardt's scaled units) ...
const INITIAL_DAMPING: f64 = 1e-4;
/// ... never goes below this ...
const MIN_DAMPING: f64 = 1e-16;
/// ... and above this the iteration has made no progress.
const MAX_DAMPING: f64 = 1e32;
/// The diagonal that scales the damping is clamped to this range, so that a column without
/// information (a point without usable observations) is still damped and does not move.
const DIAGONAL_MIN: f64 = 1e-6;
const DIAGONAL_MAX: f64 = 1e32;

/// How the reduced system is solved: `Auto` chooses by `choose_linear_solver`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LinearSolver {
    Auto,
    Dense,
    Sparse,
    Pcg,
}

/// Why the iteration stopped.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Termination {
    /// The cost changed by less than `function_tolerance` of itself.
    Function,
    /// The largest entry of the gradient is below `gradient_tolerance`.
    Gradient,
    /// The step is shorter than `parameter_tolerance` of the parameters.
    Parameters,
    MaxIterations,
    /// No damping gives a step that lowers the cost.
    NoProgress,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Options {
    pub max_iterations: usize,
    pub loss: Loss,
    pub linear_solver: LinearSolver,
    /// Relative decrease of the cost below which an undamped step ends the adjustment; under Huber
    /// and Cauchy at least `ROBUST_FUNCTION_TOLERANCE`.
    pub function_tolerance: f64,
    pub gradient_tolerance: f64,
    pub parameter_tolerance: f64,
}

impl Default for Options {
    /// The tolerances of the Python adjustment (`ftol`, `xtol` 1e-9), the gradient at 1e-10.
    fn default() -> Self {
        Options {
            max_iterations: 100,
            loss: Loss::None,
            linear_solver: LinearSolver::Auto,
            function_tolerance: 1e-9,
            gradient_tolerance: 1e-10,
            parameter_tolerance: 1e-9,
        }
    }
}

/// The adjusted block: the fields of `bundle.Result` that the parameters give (`bias` per
/// flight, sorted; `rolling_shutter` when it was adjusted), the cost before and after, the
/// largest entry of the gradient at the end, the iterations run and why they stopped.
#[derive(Clone, Debug, PartialEq)]
pub struct Solution {
    pub rotations: Vec<[[f64; 3]; 3]>,
    pub centres: Vec<[f64; 3]>,
    pub camera_params: Vec<Vec<f64>>,
    pub points: Vec<[f64; 3]>,
    pub markers: Vec<[f64; 3]>,
    pub rolling_shutter: Option<Vec<[f64; 6]>>,
    pub bias: Vec<(i64, [f64; 3])>,
    pub cost_initial: f64,
    pub cost: f64,
    pub gradient_max: f64,
    pub iterations: usize,
    pub termination: Termination,
    pub linear_solver: LinearSolver,
}

impl Solution {
    /// The solution as parameters, to evaluate the problem at.
    pub fn parameters(&self) -> Parameters {
        Parameters {
            rotations: self.rotations.clone(),
            centres: self.centres.clone(),
            camera_params: self.camera_params.clone(),
            points: self.points.clone(),
            markers: self.markers.clone(),
            rolling_shutter: self.rolling_shutter.clone().unwrap_or_default(),
            bias: self.bias.iter().map(|(_, b)| *b).collect(),
        }
    }
}

/// The solver `Auto` stands for: dense up to `dense_max_columns` columns of the reduced system,
/// otherwise sparse while the values of its factor (`symbolic_nnz_bytes`, what the symbolic
/// factorisation says) fit in `pcg_above` bytes, the conjugate gradients beyond.
pub fn choose_linear_solver(
    reduced_columns: usize,
    symbolic_nnz_bytes: u64,
    dense_max_columns: usize,
    pcg_above: u64,
) -> LinearSolver {
    if reduced_columns <= dense_max_columns {
        LinearSolver::Dense
    } else if symbolic_nnz_bytes <= pcg_above {
        LinearSolver::Sparse
    } else {
        LinearSolver::Pcg
    }
}

/// The solver of the reduced system a run uses, with what it keeps between the iterations.
enum Stepper {
    Dense,
    Sparse(SparseSystem),
    Pcg,
}

impl Stepper {
    /// The solver asked for, or the one `Auto` stands for on this problem within the limits
    /// given (`solve` passes `DENSE_MAX_COLUMNS` and `PCG_ABOVE_BYTES`): `choose_linear_solver` on
    /// the columns of the reduced system and the bytes of its sparse factor. The pattern and the
    /// symbolic factorisation, which say how big the factor would be, are built only above the
    /// dense limit; the values are allocated only once the sparse Cholesky is chosen; when either
    /// cannot be built (memory) the conjugate gradients are taken.
    fn choose(
        pb: &Problem,
        st: &Structure,
        red: &Reduced,
        asked: LinearSolver,
        dense_max_columns: usize,
        pcg_above: u64,
    ) -> Result<Stepper, String> {
        match asked {
            LinearSolver::Dense => Ok(Stepper::Dense),
            LinearSolver::Sparse => {
                let mut system = SparseSystem::new(pb, st, red)?;
                system.allocate()?;
                Ok(Stepper::Sparse(system))
            }
            LinearSolver::Pcg => Ok(Stepper::Pcg),
            LinearSolver::Auto => {
                if choose_linear_solver(red.size, 0, dense_max_columns, pcg_above) == LinearSolver::Dense {
                    return Ok(Stepper::Dense);
                }
                let Ok(mut system) = SparseSystem::new(pb, st, red) else { return Ok(Stepper::Pcg) };
                let chosen = choose_linear_solver(red.size, system.factor_bytes(), dense_max_columns, pcg_above);
                if chosen == LinearSolver::Pcg || system.allocate().is_err() {
                    return Ok(Stepper::Pcg);
                }
                Ok(Stepper::Sparse(system))
            }
        }
    }

    fn solver(&self) -> LinearSolver {
        match self {
            Stepper::Dense => LinearSolver::Dense,
            Stepper::Sparse(_) => LinearSolver::Sparse,
            Stepper::Pcg => LinearSolver::Pcg,
        }
    }

    /// One step of the damped normal equations, in the columns of the layout.
    fn step(
        &mut self,
        pb: &Problem,
        lay: &Layout,
        st: &Structure,
        red: &Reduced,
        ev: &Evaluated,
        g: &[f64],
        damping: &[f64],
    ) -> Result<Vec<f64>, String> {
        match self {
            Stepper::Dense => dense_step(pb, lay, st, red, ev, g, damping),
            Stepper::Sparse(system) => system.step(pb, lay, st, red, ev, g, damping),
            Stepper::Pcg => pcg_step(pb, lay, st, red, ev, g, damping),
        }
    }
}

/// The function tolerance `solve` applies: the option's, floored at `ROBUST_FUNCTION_TOLERANCE`
/// under a robust loss.
pub(crate) fn effective_function_tolerance(loss: Loss, function_tolerance: f64) -> f64 {
    match loss {
        Loss::None => function_tolerance,
        Loss::Huber(_) | Loss::Cauchy(_) => function_tolerance.max(ROBUST_FUNCTION_TOLERANCE),
    }
}

fn max_abs(values: &[f64]) -> f64 {
    values.iter().fold(0.0, |a: f64, x| a.max(x.abs()))
}

/// Adjusts `pb` from the parameters it holds. `observe(iteration, cost)` is called after every
/// iteration with the cost reached; `false` stops the adjustment with `Err("cancelled")`.
pub fn solve(pb: &Problem, opts: &Options, observe: &mut dyn FnMut(usize, f64) -> bool) -> Result<Solution, String> {
    solve_with_limits(pb, opts, observe, DENSE_MAX_COLUMNS, PCG_ABOVE_BYTES)
}

/// `solve` with the limits of `Auto` as arguments (the tests lower them to small blocks).
pub(crate) fn solve_with_limits(
    pb: &Problem,
    opts: &Options,
    observe: &mut dyn FnMut(usize, f64) -> bool,
    dense_max_columns: usize,
    pcg_above: u64,
) -> Result<Solution, String> {
    pb.check()?;
    let lay = Layout::new(pb);
    let st = Structure::new(pb);
    let red = Reduced::new(pb, &lay);
    let mut stepper = Stepper::choose(pb, &st, &red, opts.linear_solver, dense_max_columns, pcg_above)?;
    let linear_solver = stepper.solver();

    let function_tolerance = effective_function_tolerance(opts.loss, opts.function_tolerance);
    let mut params = Parameters::initial(pb);
    let mut ev = evaluate_blocks(pb, &lay, &st, &params, opts.loss, true);
    if !ev.cost.is_finite() {
        return Err(format!("the cost of the problem as given is {}", ev.cost));
    }
    let cost_initial = ev.cost;
    // the points that their inliers do not hold (wrong tracks) are settled on their own first
    if refine_unheld_points(pb, &st, &mut params, opts.loss) > 0 {
        ev = evaluate_blocks(pb, &lay, &st, &params, opts.loss, true);
    }
    let (mut g, mut diag) = gradient_and_diagonal(pb, &lay, &st, &ev);
    let mut gradient_max = max_abs(&g);
    let mut lambda = INITIAL_DAMPING;
    let mut nu = 2.0;
    let mut iterations = 0;
    let mut termination = (gradient_max <= opts.gradient_tolerance).then_some(Termination::Gradient);
    while termination.is_none() {
        if iterations >= opts.max_iterations {
            termination = Some(Termination::MaxIterations);
            break;
        }
        iterations += 1;
        let damping: Vec<f64> = diag.iter().map(|d| lambda * d.clamp(DIAGONAL_MIN, DIAGONAL_MAX)).collect();
        let mut accepted = false;
        if let Ok(delta) = stepper.step(pb, &lay, &st, &red, &ev, &g, &damping) {
            if delta.iter().all(|x| x.is_finite()) {
                let candidate = params.step(pb, &lay, &delta);
                let trial = evaluate_blocks(pb, &lay, &st, &candidate, opts.loss, false);
                let decrease = ev.cost - trial.cost;
                // what the quadratic model promised: 0.5 delta^T (damping delta - g)
                let model_decrease =
                    0.5 * delta.iter().zip(&damping).zip(&g).map(|((d, m), gk)| d * (m * d - gk)).sum::<f64>();
                let step_norm = delta.iter().map(|x| x * x).sum::<f64>().sqrt();
                let ptol = opts.parameter_tolerance;
                let tiny_step = step_norm <= ptol * (params.norm(pb, &lay) + ptol);
                // measured against the cost a step can change: not the constant of the observations
                // behind their camera, which would loosen the tolerance by orders of magnitude; and
                // only on a step that is not damped (after rejections, a damped step is small by
                // construction, not because the cost has converged)
                let live_cost = ev.cost - ev.behind_cost;
                let undamped = lambda <= INITIAL_DAMPING;
                let tiny_change =
                    undamped && trial.cost.is_finite() && decrease.abs() <= function_tolerance * live_cost;
                if trial.cost.is_finite() && decrease > 0.0 {
                    accepted = true;
                    let rho = if model_decrease > 0.0 { decrease / model_decrease } else { 1.0 };
                    params = candidate;
                    refine_unheld_points(pb, &st, &mut params, opts.loss);
                    ev = evaluate_blocks(pb, &lay, &st, &params, opts.loss, true);
                    (g, diag) = gradient_and_diagonal(pb, &lay, &st, &ev);
                    gradient_max = max_abs(&g);
                    lambda = (lambda * (1.0 / 3.0f64).max(1.0 - (2.0 * rho - 1.0).powi(3))).max(MIN_DAMPING);
                    nu = 2.0;
                }
                if tiny_change {
                    termination = Some(Termination::Function);
                } else if accepted && gradient_max <= opts.gradient_tolerance {
                    termination = Some(Termination::Gradient);
                } else if tiny_step {
                    termination = Some(Termination::Parameters);
                }
            }
        }
        if !accepted && termination.is_none() {
            lambda *= nu;
            nu *= 2.0;
            if lambda > MAX_DAMPING {
                termination = Some(Termination::NoProgress);
            }
        }
        if !observe(iterations, ev.cost) {
            return Err("cancelled".into());
        }
    }
    Ok(Solution {
        rotations: params.rotations,
        centres: params.centres,
        camera_params: params.camera_params,
        points: params.points,
        markers: params.markers,
        rolling_shutter: pb.rolling_shutter.then_some(params.rolling_shutter),
        bias: lay.flights.iter().copied().zip(params.bias).collect(),
        cost_initial,
        cost: ev.cost,
        gradient_max,
        iterations,
        termination: termination.expect("the loop ends with a termination"),
        linear_solver,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bundle::problem::{CameraBlock, Obs};
    use crate::camera::Model;

    #[test]
    fn auto_chooses_by_the_limits_it_is_given() {
        let gib = 1u64 << 30;
        // the dense limit is counted in columns of the reduced system: 2500 free photos with
        // their poses alone, 1250 with the rolling shutter
        assert_eq!((DENSE_MAX_PHOTOS, DENSE_MAX_COLUMNS, PCG_ABOVE_BYTES), (2500, 15_000, 4 * gib));
        assert_eq!(choose_linear_solver(60, 0, DENSE_MAX_COLUMNS, 4 * gib), LinearSolver::Dense);
        assert_eq!(choose_linear_solver(15_000, 100 * gib, DENSE_MAX_COLUMNS, 4 * gib), LinearSolver::Dense);
        assert_eq!(choose_linear_solver(15_001, gib, DENSE_MAX_COLUMNS, 4 * gib), LinearSolver::Sparse);
        assert_eq!(choose_linear_solver(12 * 1250 + 1, gib, DENSE_MAX_COLUMNS, 4 * gib), LinearSolver::Sparse);
        assert_eq!(choose_linear_solver(15_001, 4 * gib, DENSE_MAX_COLUMNS, 4 * gib), LinearSolver::Sparse);
        assert_eq!(choose_linear_solver(15_001, 4 * gib + 1, DENSE_MAX_COLUMNS, 4 * gib), LinearSolver::Pcg);
        // the limits as arguments: a block of 60 photos above a limit of 300 columns
        assert_eq!(choose_linear_solver(379, 0, 300, 0), LinearSolver::Sparse);
        assert_eq!(choose_linear_solver(379, 1, 300, 0), LinearSolver::Pcg);
        assert_eq!(choose_linear_solver(300, 1, 300, 0), LinearSolver::Dense);
    }

    /// `Auto` on the small block (48 reduced columns), with the limits lowered to it: the dense
    /// solver up to 48 columns, the sparse one above while its factor fits, the conjugate gradients
    /// beyond; a solver asked by name is taken as it is.
    #[test]
    fn auto_takes_the_sparse_solver_above_the_dense_limit_and_the_conjugate_gradients_above_the_bytes() {
        use crate::bundle::schur::tests::small_block;
        let pb = small_block();
        let st = Structure::new(&pb);
        let red = Reduced::new(&pb, &Layout::new(&pb));
        assert_eq!(red.size, 48);
        let choose = |asked, dense_max_columns, pcg_above| {
            Stepper::choose(&pb, &st, &red, asked, dense_max_columns, pcg_above).unwrap().solver()
        };
        let factor_bytes = SparseSystem::new(&pb, &st, &red).unwrap().factor_bytes();
        assert!(factor_bytes > 0);
        assert_eq!(choose(LinearSolver::Auto, 48, u64::MAX), LinearSolver::Dense);
        assert_eq!(choose(LinearSolver::Auto, 47, u64::MAX), LinearSolver::Sparse);
        assert_eq!(choose(LinearSolver::Auto, 47, factor_bytes), LinearSolver::Sparse);
        assert_eq!(choose(LinearSolver::Auto, 47, factor_bytes - 1), LinearSolver::Pcg);
        assert_eq!(choose(LinearSolver::Auto, 0, 0), LinearSolver::Pcg);
        for asked in [LinearSolver::Dense, LinearSolver::Sparse, LinearSolver::Pcg] {
            assert_eq!(choose(asked, 0, 0), asked);
            assert_eq!(choose(asked, usize::MAX, u64::MAX), asked);
        }
        // the sparse system `Auto` keeps has its values; through `solve`, the solution says which
        // solver `Auto` took
        let kept = Stepper::choose(&pb, &st, &red, LinearSolver::Auto, 47, u64::MAX).unwrap();
        assert!(matches!(kept, Stepper::Sparse(ref system) if system.allocated_bytes() > 0));
        let through = |dense_max_columns, pcg_above| {
            let sol = solve_with_limits(&pb, &Options::default(), &mut |_, _| true, dense_max_columns, pcg_above);
            sol.unwrap().linear_solver
        };
        assert_eq!(through(48, u64::MAX), LinearSolver::Dense);
        assert_eq!(through(47, u64::MAX), LinearSolver::Sparse);
        assert_eq!(through(47, factor_bytes - 1), LinearSolver::Pcg);
    }

    /// The small block of `schur::tests` (every kind of residual: two cameras, a fixed photo,
    /// markers, a control point, a bar, GNSS with biases, the rolling shutter) moved a little from
    /// its exact state and solved by the three solvers of the reduced system: `Auto` is the dense
    /// one on 48 columns, each solver is the one that was asked, and all three reach the exact block.
    #[test]
    fn every_solver_of_the_reduced_system_solves_the_small_block() {
        use crate::bundle::problem::{compose, exp_rotation};
        use crate::bundle::schur::tests::small_block;
        let exact = small_block();
        let mut pb = exact.clone();
        // the free photos moved (the fixed one could not come back)
        for k in 0..pb.centres.len() {
            if !pb.fixed_images[k] {
                pb.centres[k][0] += 0.01 * (k as f64 + 1.0);
                pb.centres[k][2] -= 0.02;
            }
        }
        pb.rotations[0] = compose(&exp_rotation([0.002, -0.003, 0.001]), &pb.rotations[0]);
        for (k, p) in pb.points.iter_mut().enumerate() {
            p[1] += 0.006 * (k % 3) as f64 - 0.004;
        }
        pb.markers[1][2] += 0.01;
        pb.cameras[0].params[0] *= 1.002;
        pb.cameras[1].params[3] = 0.0;
        let auto = solve(&pb, &Options::default(), &mut |_, _| true).unwrap();
        assert_eq!(auto.linear_solver, LinearSolver::Dense);
        for solver in [LinearSolver::Dense, LinearSolver::Sparse, LinearSolver::Pcg] {
            let opts = Options { linear_solver: solver, ..Options::default() };
            let sol = solve(&pb, &opts, &mut |_, _| true).unwrap();
            assert_eq!(sol.linear_solver, solver);
            assert!(sol.cost < 1e-9 * sol.cost_initial, "{solver:?}: cost {} from {}", sol.cost, sol.cost_initial);
            assert_ne!(sol.termination, Termination::MaxIterations, "{solver:?}");
            let off = sol.centres.iter().zip(&exact.centres).map(|(a, b)| (a[0] - b[0]).abs()).fold(0.0, f64::max);
            assert!(off < 1e-6, "{solver:?}: centres {off} m off after {} iterations", sol.iterations);
            assert!((sol.camera_params[0][0] - 1000.0).abs() < 1e-6, "{solver:?}: f {}", sol.camera_params[0][0]);
        }
    }

    #[test]
    fn the_function_tolerance_is_scipys_and_the_robust_losses_floor_it() {
        assert_eq!(Options::default().function_tolerance, 1e-9);
        assert_eq!(effective_function_tolerance(Loss::None, 1e-9), 1e-9);
        assert_eq!(effective_function_tolerance(Loss::Huber(1.0), 1e-9), ROBUST_FUNCTION_TOLERANCE);
        assert_eq!(effective_function_tolerance(Loss::Cauchy(2.0), 1e-9), 1e-6);
        assert_eq!(effective_function_tolerance(Loss::Huber(1.0), 1e-3), 1e-3);
        assert_eq!(effective_function_tolerance(Loss::None, 1e-3), 1e-3);
    }

    #[test]
    fn the_defaults_and_a_problem_solved_before_it_starts() {
        let opts = Options::default();
        assert_eq!(opts.max_iterations, 100);
        assert_eq!((opts.loss, opts.linear_solver), (Loss::None, LinearSolver::Auto));
        assert_eq!((opts.function_tolerance, opts.gradient_tolerance, opts.parameter_tolerance), (1e-9, 1e-10, 1e-9));
        // one fixed photo and a point on its axis: a problem that is solved before it starts, with
        // an empty reduced system, whatever the solver
        let pb = Problem {
            rotations: vec![[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]],
            centres: vec![[0.0; 3]],
            image_camera: vec![0],
            cameras: vec![CameraBlock {
                model: Model::SimplePinhole,
                params: vec![1000.0, 600.0, 450.0],
                width: 1200.0,
                height: 900.0,
                free: vec![],
            }],
            points: vec![[0.0, 0.0, 5.0]],
            obs: vec![Obs { image: 0, point: 0, xy: [600.0, 450.0] }],
            fixed_images: vec![true],
            ..Problem::default()
        };
        for solver in [LinearSolver::Sparse, LinearSolver::Pcg] {
            let opts = Options { linear_solver: solver, ..Options::default() };
            let sol = solve(&pb, &opts, &mut |_, _| true).unwrap();
            assert_eq!((sol.iterations, sol.termination, sol.linear_solver), (0, Termination::Gradient, solver));
        }
        let sol = solve(&pb, &Options::default(), &mut |_, _| true).unwrap();
        assert_eq!((sol.iterations, sol.termination), (0, Termination::Gradient));
        assert_eq!(sol.linear_solver, LinearSolver::Dense);
        assert_eq!((sol.cost_initial, sol.cost, sol.gradient_max), (0.0, 0.0, 0.0));
        assert_eq!((&sol.rolling_shutter, &sol.bias), (&None, &vec![]));
        assert_eq!(sol.parameters(), Parameters::initial(&pb));
        let mut broken = pb;
        broken.obs[0].point = 1;
        assert!(solve(&broken, &Options::default(), &mut |_, _| true).unwrap_err().contains("tie observation 0"));
    }
}
