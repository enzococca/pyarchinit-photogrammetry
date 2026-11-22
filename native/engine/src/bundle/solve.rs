//! The bundle adjustment of milestone 1 as a model of the generic Levenberg-Marquardt
//! (`lm::solve_model`): the camera blocks are the photos (the pose of a free photo, then its
//! rolling shutter), the markers and, as the border, the free intrinsics of each camera and the
//! biases of the flights; the point blocks are the tie points; the residual blocks are the tie and
//! marker observations, the control points, the scale bars, the GNSS positions and the priors of
//! the rolling shutter, in the order of the rows of `_Model.residuals` of bundle.py. The rotations
//! are updated on their manifold by `exp(delta) R`; the points their inliers do not hold are
//! re-solved on their own after every accepted step (`residuals::refine_unheld_points`).

use super::lm::{solve_model_with_limits, BlockJacobian, Model, Report};
use super::problem::{Layout, Parameters, Problem};
use super::residuals::{
    bar, control, gnss, observation_linearized, observation_values, refine_unheld_points, rs_prior, variables, Loss,
    Structure, BEHIND_CAMERA_RESIDUAL, OFF_DELTA, OFF_INTR, OFF_POINT,
};

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

/// The function tolerance `solve` applies: the option's, floored at `ROBUST_FUNCTION_TOLERANCE`
/// under a robust loss.
pub(crate) fn effective_function_tolerance(loss: Loss, function_tolerance: f64) -> f64 {
    match loss {
        Loss::None => function_tolerance,
        Loss::Huber(_) | Loss::Cauchy(_) => function_tolerance.max(ROBUST_FUNCTION_TOLERANCE),
    }
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
    let mut model = BundleModel::new(pb, opts.loss);
    let report = solve_model_with_limits(&mut model, opts, observe, dense_max_columns, pcg_above)?;
    Ok(model.solution(report))
}

/// The bundle adjustment of a problem as a `Model`: the parameters it is at, the layout of their
/// columns, and the camera blocks of the reduced system with the layout column of each of their
/// columns.
pub(crate) struct BundleModel<'a> {
    pb: &'a Problem,
    lay: Layout,
    st: Structure,
    pub(crate) params: Parameters,
    loss: Loss,
    cam_size: Vec<usize>,
    point_size: Vec<usize>,
    /// The camera block of each photo (none for a fixed photo without rolling shutter), of each
    /// marker from `marker_block`, of the free intrinsics of each camera, of the bias of each
    /// flight from `bias_block`.
    photo_block: Vec<Option<usize>>,
    marker_block: usize,
    intr_block: Vec<Option<usize>>,
    bias_block: usize,
    border: usize,
    /// The layout column of each column of the reduced system.
    layout_col: Vec<usize>,
    /// The first residual block of the marker observations, the control points, the bars, the
    /// GNSS positions, the priors of the rolling shutter, and the end.
    mobs_block: usize,
    gcp_block: usize,
    bar_block: usize,
    gnss_block: usize,
    rs_block: usize,
    n_blocks: usize,
}

impl<'a> BundleModel<'a> {
    /// The model of `pb` (which passes `Problem::check`) at its own parameters, `loss` on the tie
    /// points.
    pub(crate) fn new(pb: &'a Problem, loss: Loss) -> BundleModel<'a> {
        let lay = Layout::new(pb);
        let st = Structure::new(pb);
        let params = Parameters::initial(pb);
        let (mut cam_size, mut layout_col) = (Vec::new(), Vec::new());
        let mut photo_block = Vec::with_capacity(pb.centres.len());
        for i in 0..pb.centres.len() {
            let start = layout_col.len();
            if let Some(c0) = lay.image_col[i] {
                layout_col.extend(c0..c0 + 6);
            }
            if let Some(r0) = lay.rs_col_of(i) {
                layout_col.extend(r0..r0 + 6);
            }
            let len = layout_col.len() - start;
            photo_block.push((len > 0).then(|| {
                cam_size.push(len);
                cam_size.len() - 1
            }));
        }
        let marker_block = cam_size.len();
        for m in 0..pb.markers.len() {
            cam_size.push(3);
            layout_col.extend(lay.marker_col + 3 * m..lay.marker_col + 3 * m + 3);
        }
        let border_block = cam_size.len();
        let mut intr_block = Vec::with_capacity(pb.cameras.len());
        for c in 0..pb.cameras.len() {
            let cols = lay.camera_cols(c);
            intr_block.push((!cols.is_empty()).then(|| {
                cam_size.push(cols.len());
                layout_col.extend(cols);
                cam_size.len() - 1
            }));
        }
        let bias_block = cam_size.len();
        for f in 0..lay.flights.len() {
            cam_size.push(3);
            layout_col.extend(lay.bias_col + 3 * f..lay.bias_col + 3 * f + 3);
        }
        let border = cam_size.len() - border_block;
        let mobs_block = pb.obs.len();
        let gcp_block = mobs_block + pb.mobs.len();
        let bar_block = gcp_block + pb.gcps.len();
        let gnss_block = bar_block + pb.bars.len();
        let rs_block = gnss_block + pb.gnss.len();
        let n_blocks = rs_block + if pb.rolling_shutter { pb.centres.len() } else { 0 };
        BundleModel {
            pb,
            lay,
            st,
            params,
            loss,
            cam_size,
            point_size: vec![3; pb.points.len()],
            photo_block,
            marker_block,
            intr_block,
            bias_block,
            border,
            layout_col,
            mobs_block,
            gcp_block,
            bar_block,
            gnss_block,
            rs_block,
            n_blocks,
        }
    }

    /// A step in the columns of the model (the reduced system, then the points) in the columns of
    /// the layout.
    pub(crate) fn to_layout(&self, delta_cameras: &[f64], delta_points: &[f64]) -> Vec<f64> {
        let mut delta = vec![0.0; self.lay.size];
        for (&lc, &x) in self.layout_col.iter().zip(delta_cameras) {
            delta[lc] = x;
        }
        delta[self.lay.point_col..self.lay.point_col + delta_points.len()].copy_from_slice(delta_points);
        delta
    }

    /// The adjusted block, from the parameters reached and the report of the solver.
    fn solution(self, report: Report) -> Solution {
        let params = self.params;
        Solution {
            rotations: params.rotations,
            centres: params.centres,
            camera_params: params.camera_params,
            points: params.points,
            markers: params.markers,
            rolling_shutter: self.pb.rolling_shutter.then_some(params.rolling_shutter),
            bias: self.lay.flights.iter().copied().zip(params.bias).collect(),
            cost_initial: report.cost_initial,
            cost: report.cost,
            gradient_max: report.gradient_max,
            iterations: report.iterations,
            termination: report.termination,
            linear_solver: report.linear_solver,
        }
    }

    /// A tie (`marker` false) or marker observation `j`: its camera blocks, the rows of its
    /// Jacobian from the rows of `Structure::row_len` of the residuals (the pose and the rolling
    /// shutter of its photo, the marker, the free intrinsics of its camera, the point).
    fn observation(&self, j: usize, marker: bool, out: &mut BlockJacobian) {
        let (pb, st, params) = (self.pb, &self.st, &self.params);
        let o = if marker { &pb.mobs[j] } else { &pb.obs[j] };
        let (x, sigma) =
            if marker { (params.markers[o.point], pb.marker_sigma) } else { (params.points[o.point], pb.pixel_sigma) };
        let (i, c) = (o.image, pb.image_camera[o.image]);
        let mut res = [0.0; 2];
        let mut rows = [0.0; 2 * (OFF_INTR + 12 + 6)];
        let rows = &mut rows[..2 * st.row_len];
        out.constant = observation_linearized(pb, st, params, o, x, sigma, &mut res, rows);
        out.residuals.extend_from_slice(&res);
        out.loss = if marker { Loss::None } else { self.loss };
        out.cameras.extend(self.photo_block[i]);
        if marker {
            out.cameras.push(self.marker_block + o.point);
        } else {
            out.point = Some(o.point);
        }
        out.cameras.extend(self.intr_block[c]);
        let k = pb.cameras[c].free.len();
        for jr in rows.chunks_exact(st.row_len) {
            if self.lay.image_col[i].is_some() {
                out.jacobian.extend_from_slice(&jr[OFF_DELTA..OFF_DELTA + 6]);
            }
            if pb.rolling_shutter {
                out.jacobian.extend_from_slice(&jr[st.off_rs..st.off_rs + 6]);
            }
            if marker {
                out.jacobian.extend_from_slice(&jr[OFF_POINT..OFF_POINT + 3]);
            }
            out.jacobian.extend_from_slice(&jr[OFF_INTR..OFF_INTR + k]);
            if !marker {
                out.jacobian.extend_from_slice(&jr[OFF_POINT..OFF_POINT + 3]);
            }
        }
    }

    /// The bias of the flight of the GNSS position `j` and its camera block, if it has one.
    fn bias(&self, j: usize) -> ([f64; 3], Option<usize>) {
        match self.lay.flight_index(self.pb.gnss[j].3) {
            Some(f) => (self.params.bias[f], Some(self.bias_block + f)),
            None => ([0.0; 3], None),
        }
    }
}

impl Model for BundleModel<'_> {
    fn camera_blocks(&self) -> &[usize] {
        &self.cam_size
    }

    fn point_blocks(&self) -> &[usize] {
        &self.point_size
    }

    fn residual_blocks(&self) -> usize {
        self.n_blocks
    }

    fn linearize(&self, k: usize, out: &mut BlockJacobian) {
        let (pb, params) = (self.pb, &self.params);
        if k < self.mobs_block {
            self.observation(k, false, out);
        } else if k < self.gcp_block {
            self.observation(k - self.mobs_block, true, out);
        } else if k < self.bar_block {
            let (m, target, sigma) = pb.gcps[k - self.gcp_block];
            let r = control(variables::<3>(params.markers[m], 0), target, sigma);
            out.cameras.push(self.marker_block + m);
            for value in r {
                out.residuals.push(value.v);
                out.jacobian.extend_from_slice(&value.d);
            }
        } else if k < self.gnss_block {
            let (a, b, distance, sigma) = pb.bars[k - self.bar_block];
            let (ma, mb) = (variables::<6>(params.markers[a], 0), variables::<6>(params.markers[b], 3));
            let r = bar(ma, mb, distance, sigma);
            out.cameras.extend([self.marker_block + a, self.marker_block + b]);
            out.residuals.push(r.v);
            out.jacobian.extend_from_slice(&r.d);
        } else if k < self.rs_block {
            let j = k - self.gnss_block;
            let (i, target, sigma, _) = pb.gnss[j];
            let (bias, bias_block) = self.bias(j);
            let r = gnss(variables::<6>(params.centres[i], 0), variables::<6>(bias, 3), target, sigma);
            let photo = self.lay.image_col[i].is_some().then(|| self.photo_block[i].expect("a free photo has a block"));
            out.cameras.extend(photo);
            out.cameras.extend(bias_block);
            for value in r {
                out.residuals.push(value.v);
                if let Some(b) = photo {
                    let mut row = [0.0; 12];
                    row[3..6].copy_from_slice(&value.d[..3]);
                    out.jacobian.extend_from_slice(&row[..self.cam_size[b]]);
                }
                if bias_block.is_some() {
                    out.jacobian.extend_from_slice(&value.d[3..6]);
                }
            }
        } else {
            let i = k - self.rs_block;
            let rs = params.rolling_shutter[i];
            let (sigma, sigma_angular) = pb.rs_sigmas(i);
            let (v, w) = (variables::<6>([rs[0], rs[1], rs[2]], 0), variables::<6>([rs[3], rs[4], rs[5]], 3));
            let r = rs_prior([v[0], v[1], v[2], w[0], w[1], w[2]], sigma, sigma_angular);
            let block = self.photo_block[i].expect("a photo with rolling shutter has a block");
            out.cameras.push(block);
            let off = self.cam_size[block] - 6;
            for value in r {
                out.residuals.push(value.v);
                let mut row = [0.0; 12];
                row[off..off + 6].copy_from_slice(&value.d);
                out.jacobian.extend_from_slice(&row[..self.cam_size[block]]);
            }
        }
    }

    fn residuals(&self, k: usize, out: &mut BlockJacobian) {
        let (pb, params) = (self.pb, &self.params);
        if k < self.gcp_block {
            let marker = k >= self.mobs_block;
            let (o, x, sigma) = if marker {
                let o = &pb.mobs[k - self.mobs_block];
                (o, params.markers[o.point], pb.marker_sigma)
            } else {
                (&pb.obs[k], params.points[pb.obs[k].point], pb.pixel_sigma)
            };
            out.loss = if marker { Loss::None } else { self.loss };
            match observation_values(pb, params, o, x, sigma) {
                Some(r) => out.residuals.extend_from_slice(&r),
                None => {
                    out.residuals.extend_from_slice(&[BEHIND_CAMERA_RESIDUAL; 2]);
                    out.constant = true;
                }
            }
        } else if k < self.bar_block {
            let (m, target, sigma) = pb.gcps[k - self.gcp_block];
            out.residuals.extend_from_slice(&control(params.markers[m], target, sigma));
        } else if k < self.gnss_block {
            let (a, b, distance, sigma) = pb.bars[k - self.bar_block];
            out.residuals.push(bar(params.markers[a], params.markers[b], distance, sigma));
        } else if k < self.rs_block {
            let j = k - self.gnss_block;
            let (i, target, sigma, _) = pb.gnss[j];
            out.residuals.extend_from_slice(&gnss(params.centres[i], self.bias(j).0, target, sigma));
        } else {
            let i = k - self.rs_block;
            let (sigma, sigma_angular) = pb.rs_sigmas(i);
            out.residuals.extend_from_slice(&rs_prior(params.rolling_shutter[i], sigma, sigma_angular));
        }
    }

    fn apply(&mut self, delta_cameras: &[f64], delta_points: &[f64]) {
        let delta = self.to_layout(delta_cameras, delta_points);
        self.params = self.params.step(self.pb, &self.lay, &delta);
    }

    fn save(&self) -> Vec<f64> {
        let p = &self.params;
        let mut out = Vec::new();
        out.extend(p.rotations.iter().flatten().flatten());
        out.extend(p.centres.iter().flatten());
        out.extend(p.camera_params.iter().flatten());
        out.extend(p.points.iter().chain(&p.markers).chain(&p.bias).flatten());
        out.extend(p.rolling_shutter.iter().flatten());
        out
    }

    fn restore(&mut self, saved: &[f64]) {
        let mut values = saved.iter().copied();
        let p = &mut self.params;
        let slots = p.rotations.iter_mut().flatten().flatten().chain(p.centres.iter_mut().flatten());
        let slots = slots.chain(p.camera_params.iter_mut().flatten());
        let slots = slots.chain(p.points.iter_mut().chain(p.markers.iter_mut()).chain(p.bias.iter_mut()).flatten());
        for slot in slots.chain(p.rolling_shutter.iter_mut().flatten()) {
            *slot = values.next().expect("the saved parameters");
        }
    }

    fn point_coordinate(&self, index: usize) -> f64 {
        self.params.points[index / 3][index % 3]
    }

    fn border_blocks(&self) -> usize {
        self.border
    }

    fn settle(&mut self) -> bool {
        refine_unheld_points(self.pb, &self.st, &mut self.params, self.loss) > 0
    }

    fn parameter_norm(&self) -> f64 {
        self.params.norm(self.pb, &self.lay)
    }
}

/// The linear system of the small blocks of the tests, as the solver sees it.
#[cfg(test)]
pub(crate) mod testing {
    use super::*;
    use crate::bundle::lm::{first_linearization, gradient_and_diagonal, Lin, Shape};
    use crate::bundle::schur::Reduced;

    pub(crate) struct System<'a> {
        pub model: BundleModel<'a>,
        pub shape: Shape,
        pub lin: Lin,
        pub g: Vec<f64>,
        pub d: Vec<f64>,
        pub held: Vec<bool>,
    }

    /// `pb` at `params` with `loss` on its tie points, linearised: the gradient and the diagonal in
    /// the columns of the model.
    pub(crate) fn system(pb: &Problem, params: Parameters, loss: Loss) -> System<'_> {
        let mut model = BundleModel::new(pb, loss);
        model.params = params;
        let (shape, lin) = first_linearization(&model).unwrap();
        let (g, d) = gradient_and_diagonal(&shape, &lin);
        let held = vec![false; shape.n_point_cols];
        System { model, shape, lin, g, d, held }
    }

    impl System<'_> {
        pub(crate) fn reduced(&self) -> Reduced<'_> {
            Reduced { shape: &self.shape, lin: &self.lin, held: &self.held }
        }

        /// The damping `lambda` times the diagonal floored at 1e-6.
        pub(crate) fn damping(&self, lambda: f64) -> Vec<f64> {
            self.d.iter().map(|x| lambda * x.max(1e-6)).collect()
        }

        /// A vector in the columns of the model in the columns of the layout.
        pub(crate) fn to_layout(&self, x: &[f64]) -> Vec<f64> {
            self.model.to_layout(&x[..self.shape.n], &x[self.shape.n..])
        }
    }
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
        use crate::bundle::lm::Stepper;
        use crate::bundle::schur::tests::small_block;
        use crate::bundle::sparse::SparseSystem;
        let pb = small_block();
        let sys = testing::system(&pb, Parameters::initial(&pb), Loss::None);
        let shape = &sys.shape;
        assert_eq!(shape.n, 48);
        let choose = |asked, dense_max_columns, pcg_above| {
            Stepper::choose(shape, asked, dense_max_columns, pcg_above).unwrap().solver()
        };
        let factor_bytes = SparseSystem::new(shape).unwrap().factor_bytes();
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
        let kept = Stepper::choose(shape, LinearSolver::Auto, 47, u64::MAX).unwrap();
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
