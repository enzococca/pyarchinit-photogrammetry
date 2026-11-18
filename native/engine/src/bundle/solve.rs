//! Levenberg-Marquardt over the reduced camera system: the diagonal of the normal equations
//! damped (Marquardt), a step taken when the cost falls, the damping moved by the quality of
//! the step (Nielsen's rule, as in Madsen, Nielsen and Tingleff), the rotations updated on
//! their manifold by `exp(delta) R`. Stops on the relative decrease of the cost, the largest
//! entry of the gradient, the size of the step, the number of iterations, or when no damping
//! gives a step that lowers the cost.

use super::problem::{Layout, Parameters, Problem};
use super::residuals::{evaluate_blocks, gradient_and_diagonal, refine_unheld_points, Loss, Structure};
use super::schur::{dense_step, Reduced};

/// Up to this many free photos the reduced system is factorised dense (spec 8.1).
pub const DENSE_MAX_PHOTOS: usize = 2500;
/// A sparse factor that would take more bytes than this is replaced by the conjugate gradients.
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
    pub function_tolerance: f64,
    pub gradient_tolerance: f64,
    pub parameter_tolerance: f64,
}

impl Default for Options {
    /// Ceres's function tolerance (1e-6: a plateau of a robust loss, where wrong tracks crawl by
    /// 1e-7 of the cost per iteration, would otherwise run to the limit), the parameter tolerance of
    /// the Python adjustment (`xtol` 1e-9), the gradient at 1e-10.
    fn default() -> Self {
        Options {
            max_iterations: 100,
            loss: Loss::None,
            linear_solver: LinearSolver::Auto,
            function_tolerance: 1e-6,
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

/// The solver `Auto` stands for: dense up to `dense_max` free photos, otherwise sparse while
/// its factor fits in `pcg_above` bytes, the conjugate gradients beyond.
pub fn choose_linear_solver(
    free_photos: usize,
    symbolic_nnz_bytes: u64,
    dense_max: usize,
    pcg_above: u64,
) -> LinearSolver {
    if free_photos <= dense_max {
        LinearSolver::Dense
    } else if symbolic_nnz_bytes <= pcg_above {
        LinearSolver::Sparse
    } else {
        LinearSolver::Pcg
    }
}

fn max_abs(values: &[f64]) -> f64 {
    values.iter().fold(0.0, |a: f64, x| a.max(x.abs()))
}

/// Adjusts `pb` from the parameters it holds. `observe(iteration, cost)` is called after every
/// iteration with the cost reached; `false` stops the adjustment with `Err("cancelled")`.
pub fn solve(pb: &Problem, opts: &Options, observe: &mut dyn FnMut(usize, f64) -> bool) -> Result<Solution, String> {
    pb.check()?;
    let lay = Layout::new(pb);
    let st = Structure::new(pb);
    let red = Reduced::new(pb, &lay);
    let linear_solver = match opts.linear_solver {
        // the sparse solvers are not built yet: until they are, Auto takes the dense one whatever
        // the size of the block
        LinearSolver::Auto => match choose_linear_solver(lay.free_images.len(), 0, DENSE_MAX_PHOTOS, PCG_ABOVE_BYTES) {
            LinearSolver::Dense => LinearSolver::Dense,
            _ => LinearSolver::Dense,
        },
        LinearSolver::Dense => LinearSolver::Dense,
        LinearSolver::Sparse | LinearSolver::Pcg => return Err("not built yet".into()),
    };

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
        if let Ok(delta) = dense_step(pb, &lay, &st, &red, &ev, &g, &damping) {
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
                    undamped && trial.cost.is_finite() && decrease.abs() <= opts.function_tolerance * live_cost;
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
        assert_eq!(choose_linear_solver(10, 0, 2500, 4 * gib), LinearSolver::Dense);
        assert_eq!(choose_linear_solver(2500, 100 * gib, 2500, 4 * gib), LinearSolver::Dense);
        assert_eq!(choose_linear_solver(2501, gib, 2500, 4 * gib), LinearSolver::Sparse);
        assert_eq!(choose_linear_solver(2501, 4 * gib, 2500, 4 * gib), LinearSolver::Sparse);
        assert_eq!(choose_linear_solver(2501, 4 * gib + 1, 2500, 4 * gib), LinearSolver::Pcg);
        assert_eq!(choose_linear_solver(3, 0, 2, 0), LinearSolver::Sparse);
        assert_eq!(choose_linear_solver(3, 1, 2, 0), LinearSolver::Pcg);
        assert_eq!(DENSE_MAX_PHOTOS, 2500);
        assert_eq!(PCG_ABOVE_BYTES, 4 * gib);
    }

    #[test]
    fn the_defaults_and_the_solvers_not_built_yet() {
        let opts = Options::default();
        assert_eq!(opts.max_iterations, 100);
        assert_eq!((opts.loss, opts.linear_solver), (Loss::None, LinearSolver::Auto));
        assert_eq!((opts.function_tolerance, opts.gradient_tolerance, opts.parameter_tolerance), (1e-6, 1e-10, 1e-9));
        // one fixed photo and a point on its axis: a problem that is solved before it starts
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
            assert_eq!(solve(&pb, &opts, &mut |_, _| true), Err("not built yet".to_string()));
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
