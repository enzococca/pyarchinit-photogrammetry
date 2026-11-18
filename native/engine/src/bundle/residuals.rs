//! The residuals of a problem and their Jacobian: tie and marker observations through the camera
//! models (`camera::project`), control points, scale bars, GNSS positions with the bias of their
//! flight, the prior of the rolling shutter. Each residual is written once, generic over
//! `Scalar`: in `f64` for the cost of a trial step, in dual numbers for the derivatives, computed
//! in parallel over the observations. Rows come in the order `_Model.residuals` of bundle.py
//! builds them, each divided by its sigma.

use rayon::prelude::*;

use super::dual::Dual;
use super::problem::{Layout, Obs, Parameters, Problem};
use super::Scalar;
use crate::camera::{project, Model};

/// The residual of an observation behind its camera (`np.nan_to_num(..., nan=1e3)` of the
/// Python model), whose Jacobian is zero.
pub const BEHIND_CAMERA_RESIDUAL: f64 = 1e3;

/// Depth at or below which a point is behind the camera.
const MIN_DEPTH: f64 = 1e-9;

/// The least weight of a residual in the normal matrix, as a fraction of its weight in the
/// gradient (`Loss::curvature`).
const CURVATURE_FLOOR: f64 = 0.01;

/// Where the derivatives of an observation sit in its two Jacobian rows (`Structure::row_len`
/// long): the rotation increment, the centre, the point (or marker), the free intrinsics of its
/// camera, then (at `Structure::off_rs`) the rolling shutter of its photo.
pub(crate) const OFF_DELTA: usize = 0;
pub(crate) const OFF_CENTRE: usize = 3;
pub(crate) const OFF_POINT: usize = 6;
pub(crate) const OFF_INTR: usize = 9;

/// The robust loss on the tie-point residuals: `rho(s)` of the squared residual `s`, its scale
/// in the units of the residuals (pixels divided by `pixel_sigma`). The cost is what scipy's
/// `loss="huber"` and `"cauchy"` give with `f_scale` = the scale.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Loss {
    None,
    Huber(f64),
    Cauchy(f64),
}

impl Loss {
    /// `rho(s)`.
    pub fn rho(self, s: f64) -> f64 {
        match self {
            Loss::None => s,
            Loss::Huber(a) => {
                if s <= a * a {
                    s
                } else {
                    2.0 * a * s.sqrt() - a * a
                }
            }
            Loss::Cauchy(a) => a * a * (s / (a * a)).ln_1p(),
        }
    }

    /// `rho'(s)`: the slope of the loss, the weight of the residual in the gradient.
    pub fn slope(self, s: f64) -> f64 {
        match self {
            Loss::None => 1.0,
            Loss::Huber(a) => {
                if s <= a * a {
                    1.0
                } else {
                    a / s.sqrt()
                }
            }
            Loss::Cauchy(a) => 1.0 / (1.0 + s / (a * a)),
        }
    }

    /// `rho' + 2 s rho''`, the curvature of `rho(r^2) / 2` in `r`: the weight of the residual in
    /// the normal matrix (the Triggs correction of a scalar residual, as scipy applies it). Where
    /// the loss is linear or concave in `r` it is floored at `CURVATURE_FLOOR` of the slope, so that
    /// a point whose observations are all in that regime still takes a bounded step.
    pub fn curvature(self, s: f64) -> f64 {
        let slope = self.slope(s);
        let curvature = match self {
            Loss::None => 1.0,
            Loss::Huber(a) => {
                if s <= a * a {
                    1.0
                } else {
                    0.0
                }
            }
            Loss::Cauchy(a) => slope * (a * a - s) / (a * a + s),
        };
        curvature.max(CURVATURE_FLOOR * slope)
    }
}

/// What `evaluate` gives: the cost `0.5 * sum(rho(r^2))`, the residuals, the gradient
/// `J^T rho'(r^2) r` in the columns of `Layout` and the Jacobian of the residuals as
/// `(row, column, value)` entries, one per entry of the structure of `_Model.sparsity`.
#[derive(Clone, Debug, PartialEq)]
pub struct Evaluation {
    pub cost: f64,
    pub residuals: Vec<f64>,
    pub gradient: Vec<f64>,
    pub jacobian: Vec<(usize, usize, f64)>,
}

/// Indices grouped by a key (the observations of each photo, of each point), each group in the
/// order of the indices: a stable counting sort.
pub(crate) struct Groups {
    offsets: Vec<usize>,
    items: Vec<usize>,
}

impl Groups {
    pub(crate) fn new(n: usize, keys: &[usize]) -> Groups {
        let mut offsets = vec![0; n + 1];
        for &k in keys {
            offsets[k + 1] += 1;
        }
        for g in 0..n {
            offsets[g + 1] += offsets[g];
        }
        let mut next = offsets.clone();
        let mut items = vec![0; keys.len()];
        for (j, &k) in keys.iter().enumerate() {
            items[next[k]] = j;
            next[k] += 1;
        }
        Groups { offsets, items }
    }

    pub(crate) fn of(&self, g: usize) -> &[usize] {
        &self.items[self.offsets[g]..self.offsets[g + 1]]
    }
}

/// What does not change while a problem is solved: its observations grouped by photo, point and
/// marker (`marker_bars` holds, for a marker, `2 * bar` for the first end of the bar and
/// `2 * bar + 1` for the second), the length of the Jacobian rows and where each kind of
/// residual starts.
pub(crate) struct Structure {
    pub image_obs: Groups,
    pub point_obs: Groups,
    pub image_mobs: Groups,
    pub marker_mobs: Groups,
    pub image_gnss: Groups,
    pub marker_gcps: Groups,
    pub marker_bars: Groups,
    pub row_len: usize,
    pub off_rs: usize,
    pub mobs_row: usize,
    pub gcp_row: usize,
    pub bar_row: usize,
    pub gnss_row: usize,
    pub rs_row: usize,
    pub n_rows: usize,
}

impl Structure {
    pub(crate) fn new(pb: &Problem) -> Structure {
        let n = pb.centres.len();
        let images = |list: &[Obs]| list.iter().map(|o| o.image).collect::<Vec<_>>();
        let targets = |list: &[Obs]| list.iter().map(|o| o.point).collect::<Vec<_>>();
        let kmax = pb.cameras.iter().map(|c| c.free.len()).max().unwrap_or(0);
        let off_rs = OFF_INTR + kmax;
        let rs = if pb.rolling_shutter { 6 } else { 0 };
        let mobs_row = 2 * pb.obs.len();
        let gcp_row = mobs_row + 2 * pb.mobs.len();
        let bar_row = gcp_row + 3 * pb.gcps.len();
        let gnss_row = bar_row + pb.bars.len();
        let rs_row = gnss_row + 3 * pb.gnss.len();
        let bar_ends: Vec<usize> = pb.bars.iter().flat_map(|b| [b.0, b.1]).collect();
        Structure {
            image_obs: Groups::new(n, &images(&pb.obs)),
            point_obs: Groups::new(pb.points.len(), &targets(&pb.obs)),
            image_mobs: Groups::new(n, &images(&pb.mobs)),
            marker_mobs: Groups::new(pb.markers.len(), &targets(&pb.mobs)),
            image_gnss: Groups::new(n, &pb.gnss.iter().map(|g| g.0).collect::<Vec<_>>()),
            marker_gcps: Groups::new(pb.markers.len(), &pb.gcps.iter().map(|g| g.0).collect::<Vec<_>>()),
            marker_bars: Groups::new(pb.markers.len(), &bar_ends),
            row_len: off_rs + rs,
            off_rs,
            mobs_row,
            gcp_row,
            bar_row,
            gnss_row,
            rs_row,
            n_rows: rs_row + rs * n,
        }
    }
}

/// The residuals at a point of the parameters and, when asked, the blocks of their Jacobian:
/// two rows of `Structure::row_len` per tie and marker observation, the few entries of the
/// other rows as they are. For each tie row, `weights` holds its weight in the normal matrix
/// (`Loss::curvature`) and `slopes` its weight in the gradient (`Loss::slope`); `behind_cost` is
/// the part of the cost that comes from observations behind their camera, which no step can change.
pub(crate) struct Evaluated {
    pub cost: f64,
    pub behind_cost: f64,
    pub residuals: Vec<f64>,
    pub weights: Vec<f64>,
    pub slopes: Vec<f64>,
    pub obs_jac: Vec<f64>,
    pub mobs_jac: Vec<f64>,
    pub gcp_jac: Vec<[f64; 3]>,
    /// Derivatives of a bar with respect to its first marker, then its second.
    pub bar_jac: Vec<[f64; 6]>,
    /// Derivatives of a GNSS residual with respect to the centre, then the bias.
    pub gnss_jac: Vec<[f64; 6]>,
    pub rs_jac: Vec<[f64; 6]>,
}

impl Evaluated {
    /// Jacobian row `row` (of `2 * obs`) of the tie observations.
    #[inline]
    pub(crate) fn obs_row(&self, st: &Structure, row: usize) -> &[f64] {
        &self.obs_jac[row * st.row_len..(row + 1) * st.row_len]
    }

    /// Jacobian row `row` (of `2 * mobs`) of the marker observations.
    #[inline]
    pub(crate) fn mobs_row(&self, st: &Structure, row: usize) -> &[f64] {
        &self.mobs_jac[row * st.row_len..(row + 1) * st.row_len]
    }
}

/// What one observation depends on, in the scalar `T`: the increment of the rotation (zero, but
/// for its derivatives), the centre, the point, the parameters of the camera (the fixed ones
/// constants) and, with the rolling shutter, the linear and angular velocity of the photo.
struct View<T> {
    delta: [T; 3],
    centre: [T; 3],
    point: [T; 3],
    params: [T; 12],
    velocity: Option<([T; 3], [T; 3])>,
}

/// The two residuals of an observation at the pixel `xy`, divided by `sigma`: the point in the
/// camera frame is `rel = X - C - v tau`, `x = R rel`, `x += (w tau) x x` with `tau` the time of
/// the observed row and `R = (I + [delta]x) R0`; then the camera model. `None` behind the camera.
fn reprojection<T: Scalar>(
    model: Model,
    r0: &[[f64; 3]; 3],
    view: &View<T>,
    xy: [f64; 2],
    tau: f64,
    sigma: f64,
) -> Option<[T; 2]> {
    let mut rel = [view.point[0] - view.centre[0], view.point[1] - view.centre[1], view.point[2] - view.centre[2]];
    if let Some((v, _)) = &view.velocity {
        for k in 0..3 {
            rel[k] = rel[k] - v[k] * tau;
        }
    }
    let y = [
        rel[0] * r0[0][0] + rel[1] * r0[0][1] + rel[2] * r0[0][2],
        rel[0] * r0[1][0] + rel[1] * r0[1][1] + rel[2] * r0[1][2],
        rel[0] * r0[2][0] + rel[1] * r0[2][1] + rel[2] * r0[2][2],
    ];
    let d = &view.delta;
    let mut x = [
        y[0] + (d[1] * y[2] - d[2] * y[1]),
        y[1] + (d[2] * y[0] - d[0] * y[2]),
        y[2] + (d[0] * y[1] - d[1] * y[0]),
    ];
    if let Some((_, w)) = &view.velocity {
        let wt = [w[0] * tau, w[1] * tau, w[2] * tau];
        let c = [wt[1] * x[2] - wt[2] * x[1], wt[2] * x[0] - wt[0] * x[2], wt[0] * x[1] - wt[1] * x[0]];
        x = [x[0] + c[0], x[1] + c[1], x[2] + c[2]];
    }
    if !(x[2].value() > MIN_DEPTH) {
        return None;
    }
    let (px, py) = project(model, &view.params, x[0] / x[2], x[1] / x[2]);
    Some([(px - xy[0]) / sigma, (py - xy[1]) / sigma])
}

/// A control point: the marker pulled towards `target` by its `sigma`.
fn control<T: Scalar>(m: [T; 3], target: [f64; 3], sigma: [f64; 3]) -> [T; 3] {
    [(m[0] - target[0]) / sigma[0], (m[1] - target[1]) / sigma[1], (m[2] - target[2]) / sigma[2]]
}

/// A scale bar: the distance between two markers against the measured one. Two markers at the
/// same place have no direction: the residual is `-distance / sigma` without derivatives.
fn bar<T: Scalar>(a: [T; 3], b: [T; 3], distance: f64, sigma: f64) -> T {
    let d = [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
    let n2 = d[0] * d[0] + d[1] * d[1] + d[2] * d[2];
    let n = if n2.value() > 0.0 { n2.sqrt() } else { T::from_f64(0.0) };
    (n - distance) / sigma
}

/// A GNSS position: measured = true position + bias of the flight.
fn gnss<T: Scalar>(centre: [T; 3], bias: [T; 3], target: [f64; 3], sigma: [f64; 3]) -> [T; 3] {
    [
        (centre[0] + bias[0] - target[0]) / sigma[0],
        (centre[1] + bias[1] - target[1]) / sigma[1],
        (centre[2] + bias[2] - target[2]) / sigma[2],
    ]
}

/// The prior of the rolling shutter: a still camera, within `sigma` and `sigma_angular`.
fn rs_prior<T: Scalar>(rs: [T; 6], sigma: f64, sigma_angular: f64) -> [T; 6] {
    [rs[0] / sigma, rs[1] / sigma, rs[2] / sigma, rs[3] / sigma_angular, rs[4] / sigma_angular, rs[5] / sigma_angular]
}

fn variables<const N: usize>(v: [f64; 3], off: usize) -> [Dual<N>; 3] {
    [Dual::variable(v[0], off), Dual::variable(v[1], off + 1), Dual::variable(v[2], off + 2)]
}

fn view_f64(pb: &Problem, params: &Parameters, i: usize, x: [f64; 3]) -> View<f64> {
    let c = pb.image_camera[i];
    let mut p = [0.0; 12];
    for (k, v) in params.camera_params[c].iter().take(12).enumerate() {
        p[k] = *v;
    }
    let velocity = pb.rolling_shutter.then(|| {
        let rs = params.rolling_shutter[i];
        ([rs[0], rs[1], rs[2]], [rs[3], rs[4], rs[5]])
    });
    View { delta: [0.0; 3], centre: params.centres[i], point: x, params: p, velocity }
}

/// The view of an observation with its unknowns as the variables `OFF_DELTA..`, `OFF_CENTRE..`,
/// `OFF_POINT..`, the free intrinsics from `OFF_INTR` and the rolling shutter after them.
fn view_dual<const N: usize>(pb: &Problem, params: &Parameters, i: usize, x: [f64; 3]) -> View<Dual<N>> {
    let c = pb.image_camera[i];
    let cam = &pb.cameras[c];
    let values = &params.camera_params[c];
    let mut p = [Dual::constant(0.0); 12];
    for (k, v) in values.iter().take(12).enumerate() {
        p[k] = Dual::constant(*v);
    }
    for (j, &k) in cam.free.iter().enumerate() {
        p[k] = Dual::variable(values[k], OFF_INTR + j);
    }
    let velocity = pb.rolling_shutter.then(|| {
        let rs = params.rolling_shutter[i];
        let off = OFF_INTR + cam.free.len();
        (variables([rs[0], rs[1], rs[2]], off), variables([rs[3], rs[4], rs[5]], off + 3))
    });
    View {
        delta: variables([0.0; 3], OFF_DELTA),
        centre: variables(params.centres[i], OFF_CENTRE),
        point: variables(x, OFF_POINT),
        params: p,
        velocity,
    }
}

/// Residuals and Jacobian rows of one observation through `Dual<N>`, `N` at least the number of
/// its unknowns; `true` when the point is behind the camera.
fn observation_rows<const N: usize>(
    pb: &Problem,
    st: &Structure,
    params: &Parameters,
    o: &Obs,
    x: [f64; 3],
    sigma: f64,
    res: &mut [f64],
    jac: &mut [f64],
) -> bool {
    let i = o.image;
    let cam = &pb.cameras[pb.image_camera[i]];
    let tau = o.xy[1] / cam.height - 0.5;
    let view = view_dual::<N>(pb, params, i, x);
    jac.fill(0.0);
    match reprojection(cam.model, &params.rotations[i], &view, o.xy, tau, sigma) {
        None => {
            res.fill(BEHIND_CAMERA_RESIDUAL);
            true
        }
        Some(r) => {
            let k = OFF_INTR + cam.free.len();
            for (row, value) in r.iter().enumerate() {
                res[row] = value.v;
                let out = &mut jac[row * st.row_len..(row + 1) * st.row_len];
                out[..k].copy_from_slice(&value.d[..k]);
                if pb.rolling_shutter {
                    out[st.off_rs..st.off_rs + 6].copy_from_slice(&value.d[k..k + 6]);
                }
            }
            false
        }
    }
}

/// The residuals of the observations `list` of the points `world` (two per observation, into
/// `res`, `behind` set where the point is behind its camera) and, when `jac` is given, their
/// Jacobian rows; in parallel over the observations.
fn observations(
    pb: &Problem,
    st: &Structure,
    params: &Parameters,
    list: &[Obs],
    world: &[[f64; 3]],
    sigma: f64,
    res: &mut [f64],
    behind: &mut [bool],
    jac: Option<&mut [f64]>,
) {
    match jac {
        None => {
            let rows = res.par_chunks_mut(2).zip_eq(behind.par_iter_mut()).zip_eq(list.par_iter());
            rows.for_each(|((res, behind), o)| {
                let cam = &pb.cameras[pb.image_camera[o.image]];
                let view = view_f64(pb, params, o.image, world[o.point]);
                let tau = o.xy[1] / cam.height - 0.5;
                *behind = match reprojection(cam.model, &params.rotations[o.image], &view, o.xy, tau, sigma) {
                    Some(r) => {
                        res.copy_from_slice(&r);
                        false
                    }
                    None => {
                        res.fill(BEHIND_CAMERA_RESIDUAL);
                        true
                    }
                };
            })
        }
        Some(jac) => res
            .par_chunks_mut(2)
            .zip_eq(behind.par_iter_mut())
            .zip_eq(jac.par_chunks_mut(2 * st.row_len))
            .zip_eq(list.par_iter())
            .for_each(|(((res, behind), jac), o)| {
                let cam = &pb.cameras[pb.image_camera[o.image]];
                let x = world[o.point];
                *behind = match OFF_INTR + cam.free.len() + if pb.rolling_shutter { 6 } else { 0 } {
                    0..=9 => observation_rows::<9>(pb, st, params, o, x, sigma, res, jac),
                    10..=15 => observation_rows::<15>(pb, st, params, o, x, sigma, res, jac),
                    16..=21 => observation_rows::<21>(pb, st, params, o, x, sigma, res, jac),
                    _ => observation_rows::<27>(pb, st, params, o, x, sigma, res, jac),
                };
            }),
    }
}

/// Every residual of `pb` at `params` and, with `with_jacobian`, the blocks of the Jacobian.
pub(crate) fn evaluate_blocks(
    pb: &Problem,
    lay: &Layout,
    st: &Structure,
    params: &Parameters,
    loss: Loss,
    with_jacobian: bool,
) -> Evaluated {
    let (n_obs, n_mobs) = (pb.obs.len(), pb.mobs.len());
    let mut residuals = vec![0.0; st.n_rows];
    let (mut obs_jac, mut mobs_jac) = if with_jacobian {
        (vec![0.0; 2 * n_obs * st.row_len], vec![0.0; 2 * n_mobs * st.row_len])
    } else {
        (Vec::new(), Vec::new())
    };
    let mut gcp_jac = Vec::new();
    let mut bar_jac = Vec::new();
    let mut gnss_jac = Vec::new();
    let mut rs_jac = Vec::new();
    let mut behind_obs = vec![false; n_obs];
    let mut behind_mobs = vec![false; n_mobs];
    {
        let (tie, rest) = residuals.split_at_mut(st.mobs_row);
        let (mob, rest) = rest.split_at_mut(2 * n_mobs);
        let (gcp, rest) = rest.split_at_mut(3 * pb.gcps.len());
        let (bars, rest) = rest.split_at_mut(pb.bars.len());
        let (positions, prior) = rest.split_at_mut(3 * pb.gnss.len());
        let jac = with_jacobian.then_some(&mut obs_jac[..]);
        observations(pb, st, params, &pb.obs, &params.points, pb.pixel_sigma, tie, &mut behind_obs, jac);
        let jac = with_jacobian.then_some(&mut mobs_jac[..]);
        observations(pb, st, params, &pb.mobs, &params.markers, pb.marker_sigma, mob, &mut behind_mobs, jac);
        for (j, (m, target, sigma)) in pb.gcps.iter().enumerate() {
            if with_jacobian {
                let r = control(variables::<3>(params.markers[*m], 0), *target, *sigma);
                for k in 0..3 {
                    gcp[3 * j + k] = r[k].v;
                }
                gcp_jac.push([r[0].d[0], r[1].d[1], r[2].d[2]]);
            } else {
                gcp[3 * j..3 * j + 3].copy_from_slice(&control(params.markers[*m], *target, *sigma));
            }
        }
        for (j, (a, b, distance, sigma)) in pb.bars.iter().enumerate() {
            if with_jacobian {
                let (ma, mb) = (variables::<6>(params.markers[*a], 0), variables::<6>(params.markers[*b], 3));
                let r = bar(ma, mb, *distance, *sigma);
                bars[j] = r.v;
                bar_jac.push(r.d);
            } else {
                bars[j] = bar(params.markers[*a], params.markers[*b], *distance, *sigma);
            }
        }
        for (j, (i, target, sigma, flight)) in pb.gnss.iter().enumerate() {
            let bias = lay.flight_index(*flight).map(|f| params.bias[f]).unwrap_or([0.0; 3]);
            if with_jacobian {
                let r = gnss(variables::<6>(params.centres[*i], 0), variables::<6>(bias, 3), *target, *sigma);
                for k in 0..3 {
                    positions[3 * j + k] = r[k].v;
                }
                gnss_jac.push([r[0].d[0], r[1].d[1], r[2].d[2], r[0].d[3], r[1].d[4], r[2].d[5]]);
            } else {
                positions[3 * j..3 * j + 3].copy_from_slice(&gnss(params.centres[*i], bias, *target, *sigma));
            }
        }
        if pb.rolling_shutter {
            for (i, rs) in params.rolling_shutter.iter().enumerate() {
                let (sigma, sigma_angular) = pb.rs_sigmas(i);
                if with_jacobian {
                    let (v, w) = (variables::<6>([rs[0], rs[1], rs[2]], 0), variables::<6>([rs[3], rs[4], rs[5]], 3));
                    let r = rs_prior([v[0], v[1], v[2], w[0], w[1], w[2]], sigma, sigma_angular);
                    let mut diagonal = [0.0; 6];
                    for k in 0..6 {
                        prior[6 * i + k] = r[k].v;
                        diagonal[k] = r[k].d[k];
                    }
                    rs_jac.push(diagonal);
                } else {
                    prior[6 * i..6 * i + 6].copy_from_slice(&rs_prior(*rs, sigma, sigma_angular));
                }
            }
        }
    }
    // the cost in a fixed order, so that two runs give the same bits
    let mut cost = 0.0;
    for (k, r) in residuals.iter().enumerate() {
        let s = r * r;
        cost += if k < st.mobs_row { loss.rho(s) } else { s };
    }
    let weights = residuals[..st.mobs_row].iter().map(|r| loss.curvature(r * r)).collect();
    let slopes = residuals[..st.mobs_row].iter().map(|r| loss.slope(r * r)).collect();
    let behind = BEHIND_CAMERA_RESIDUAL * BEHIND_CAMERA_RESIDUAL;
    let behind_cost = behind_obs.iter().filter(|b| **b).count() as f64 * loss.rho(behind)
        + behind_mobs.iter().filter(|b| **b).count() as f64 * behind;
    let cost = 0.5 * cost;
    Evaluated { cost, behind_cost, residuals, weights, slopes, obs_jac, mobs_jac, gcp_jac, bar_jac, gnss_jac, rs_jac }
}

/// Sums of one photo over its observations: the pose, the rolling shutter and the free
/// intrinsics of its camera.
#[derive(Clone, Copy)]
struct PhotoSums {
    g: [f64; 24],
    d: [f64; 24],
}

impl PhotoSums {
    const RS: usize = 6;
    const INTR: usize = 12;

    /// A row with the weight `w` in the normal matrix, `s r` its weighted residual in the gradient.
    fn add_row(&mut self, st: &Structure, rolling_shutter: bool, k: usize, row: &[f64], w: f64, sr: f64) {
        for a in 0..6 {
            let j = row[OFF_DELTA + a];
            self.g[a] += j * sr;
            self.d[a] += w * j * j;
        }
        if rolling_shutter {
            for a in 0..6 {
                let j = row[st.off_rs + a];
                self.g[Self::RS + a] += j * sr;
                self.d[Self::RS + a] += w * j * j;
            }
        }
        for q in 0..k {
            let j = row[OFF_INTR + q];
            self.g[Self::INTR + q] += j * sr;
            self.d[Self::INTR + q] += w * j * j;
        }
    }
}

/// The gradient `J^T W r` and the diagonal of `J^T W J` in the columns of `lay`, `W` the
/// weights of the loss: sums per photo, point and marker in parallel, each written by one
/// thread, the intrinsics folded in the order of the photos.
pub(crate) fn gradient_and_diagonal(
    pb: &Problem,
    lay: &Layout,
    st: &Structure,
    ev: &Evaluated,
) -> (Vec<f64>, Vec<f64>) {
    let n = pb.centres.len();
    let photos: Vec<PhotoSums> = (0..n)
        .into_par_iter()
        .map(|i| {
            let mut s = PhotoSums { g: [0.0; 24], d: [0.0; 24] };
            let k = pb.cameras[pb.image_camera[i]].free.len();
            for &j in st.image_obs.of(i) {
                for row in 2 * j..2 * j + 2 {
                    let sr = ev.slopes[row] * ev.residuals[row];
                    s.add_row(st, pb.rolling_shutter, k, ev.obs_row(st, row), ev.weights[row], sr);
                }
            }
            for &j in st.image_mobs.of(i) {
                for row in 2 * j..2 * j + 2 {
                    s.add_row(st, pb.rolling_shutter, k, ev.mobs_row(st, row), 1.0, ev.residuals[st.mobs_row + row]);
                }
            }
            for &j in st.image_gnss.of(i) {
                for a in 0..3 {
                    let (jc, r) = (ev.gnss_jac[j][a], ev.residuals[st.gnss_row + 3 * j + a]);
                    s.g[OFF_CENTRE + a] += jc * r;
                    s.d[OFF_CENTRE + a] += jc * jc;
                }
            }
            if pb.rolling_shutter {
                for a in 0..6 {
                    let (jr, r) = (ev.rs_jac[i][a], ev.residuals[st.rs_row + 6 * i + a]);
                    s.g[PhotoSums::RS + a] += jr * r;
                    s.d[PhotoSums::RS + a] += jr * jr;
                }
            }
            s
        })
        .collect();
    let mut g = vec![0.0; lay.size];
    let mut d = vec![0.0; lay.size];
    for (i, s) in photos.iter().enumerate() {
        if let Some(c0) = lay.image_col[i] {
            g[c0..c0 + 6].copy_from_slice(&s.g[..6]);
            d[c0..c0 + 6].copy_from_slice(&s.d[..6]);
        }
        if let Some(r0) = lay.rs_col_of(i) {
            g[r0..r0 + 6].copy_from_slice(&s.g[PhotoSums::RS..PhotoSums::RS + 6]);
            d[r0..r0 + 6].copy_from_slice(&s.d[PhotoSums::RS..PhotoSums::RS + 6]);
        }
        let c = pb.image_camera[i];
        let c0 = lay.camera_col[c];
        for q in 0..pb.cameras[c].free.len() {
            g[c0 + q] += s.g[PhotoSums::INTR + q];
            d[c0 + q] += s.d[PhotoSums::INTR + q];
        }
    }
    let points: Vec<([f64; 3], [f64; 3])> = (0..pb.points.len())
        .into_par_iter()
        .map(|p| {
            let (mut g, mut d) = ([0.0; 3], [0.0; 3]);
            for &j in st.point_obs.of(p) {
                for row in 2 * j..2 * j + 2 {
                    let (jr, w, sr) = (ev.obs_row(st, row), ev.weights[row], ev.slopes[row] * ev.residuals[row]);
                    for k in 0..3 {
                        let v = jr[OFF_POINT + k];
                        g[k] += v * sr;
                        d[k] += w * v * v;
                    }
                }
            }
            (g, d)
        })
        .collect();
    for (p, (gp, dp)) in points.iter().enumerate() {
        let c0 = lay.point_col + 3 * p;
        g[c0..c0 + 3].copy_from_slice(gp);
        d[c0..c0 + 3].copy_from_slice(dp);
    }
    let markers: Vec<([f64; 3], [f64; 3])> = (0..pb.markers.len())
        .into_par_iter()
        .map(|m| {
            let (mut g, mut d) = ([0.0; 3], [0.0; 3]);
            for &j in st.marker_mobs.of(m) {
                for row in 2 * j..2 * j + 2 {
                    let (jr, r) = (ev.mobs_row(st, row), ev.residuals[st.mobs_row + row]);
                    for k in 0..3 {
                        let v = jr[OFF_POINT + k];
                        g[k] += v * r;
                        d[k] += v * v;
                    }
                }
            }
            for &j in st.marker_gcps.of(m) {
                for k in 0..3 {
                    let (v, r) = (ev.gcp_jac[j][k], ev.residuals[st.gcp_row + 3 * j + k]);
                    g[k] += v * r;
                    d[k] += v * v;
                }
            }
            for &end in st.marker_bars.of(m) {
                let (j, off) = (end / 2, 3 * (end % 2));
                let r = ev.residuals[st.bar_row + j];
                for k in 0..3 {
                    let v = ev.bar_jac[j][off + k];
                    g[k] += v * r;
                    d[k] += v * v;
                }
            }
            (g, d)
        })
        .collect();
    for (m, (gm, dm)) in markers.iter().enumerate() {
        let c0 = lay.marker_col + 3 * m;
        g[c0..c0 + 3].copy_from_slice(gm);
        d[c0..c0 + 3].copy_from_slice(dm);
    }
    for (j, (_, _, _, flight)) in pb.gnss.iter().enumerate() {
        if let Some(b0) = lay.bias_col_of(*flight) {
            for k in 0..3 {
                let (v, r) = (ev.gnss_jac[j][3 + k], ev.residuals[st.gnss_row + 3 * j + k]);
                g[b0 + k] += v * r;
                d[b0 + k] += v * v;
            }
        }
    }
    (g, d)
}

/// The Jacobian as `(row, column, value)` entries in the columns of `lay`, one per entry of the
/// structure of `_Model.sparsity`.
pub(crate) fn triplets(pb: &Problem, lay: &Layout, st: &Structure, ev: &Evaluated) -> Vec<(usize, usize, f64)> {
    let mut out = Vec::new();
    let mut observation = |row: usize, i: usize, jr: &[f64], target_col: usize| {
        if let Some(c0) = lay.image_col[i] {
            for a in 0..6 {
                out.push((row, c0 + a, jr[OFF_DELTA + a]));
            }
        }
        let c = pb.image_camera[i];
        for q in 0..pb.cameras[c].free.len() {
            out.push((row, lay.camera_col[c] + q, jr[OFF_INTR + q]));
        }
        if let Some(r0) = lay.rs_col_of(i) {
            for a in 0..6 {
                out.push((row, r0 + a, jr[st.off_rs + a]));
            }
        }
        for k in 0..3 {
            out.push((row, target_col + k, jr[OFF_POINT + k]));
        }
    };
    for (j, o) in pb.obs.iter().enumerate() {
        for row in 2 * j..2 * j + 2 {
            observation(row, o.image, ev.obs_row(st, row), lay.point_col + 3 * o.point);
        }
    }
    for (j, o) in pb.mobs.iter().enumerate() {
        for row in 2 * j..2 * j + 2 {
            observation(st.mobs_row + row, o.image, ev.mobs_row(st, row), lay.marker_col + 3 * o.point);
        }
    }
    for (j, (m, _, _)) in pb.gcps.iter().enumerate() {
        for k in 0..3 {
            out.push((st.gcp_row + 3 * j + k, lay.marker_col + 3 * m + k, ev.gcp_jac[j][k]));
        }
    }
    for (j, (a, b, _, _)) in pb.bars.iter().enumerate() {
        for k in 0..3 {
            out.push((st.bar_row + j, lay.marker_col + 3 * a + k, ev.bar_jac[j][k]));
            out.push((st.bar_row + j, lay.marker_col + 3 * b + k, ev.bar_jac[j][3 + k]));
        }
    }
    for (j, (i, _, _, flight)) in pb.gnss.iter().enumerate() {
        for k in 0..3 {
            let row = st.gnss_row + 3 * j + k;
            if let Some(c0) = lay.image_col[*i] {
                out.push((row, c0 + OFF_CENTRE + k, ev.gnss_jac[j][k]));
            }
            if let Some(b0) = lay.bias_col_of(*flight) {
                out.push((row, b0 + k, ev.gnss_jac[j][3 + k]));
            }
        }
    }
    if pb.rolling_shutter {
        for i in 0..pb.centres.len() {
            let r0 = lay.rs_col_of(i).expect("rolling-shutter columns");
            for k in 0..6 {
                out.push((st.rs_row + 6 * i + k, r0 + k, ev.rs_jac[i][k]));
            }
        }
    }
    out
}

/// Residuals, cost, gradient and Jacobian of `pb` as given (`Parameters::initial`): the cameras
/// still, the biases zero. The problem must pass `Problem::check`.
pub fn evaluate(pb: &Problem, loss: Loss) -> Evaluation {
    evaluate_at(pb, &Parameters::initial(pb), loss)
}

/// Residuals, cost, gradient and Jacobian of `pb` at `params`, with `loss` on the tie points.
/// The problem must pass `Problem::check` and the parameters must be its.
pub fn evaluate_at(pb: &Problem, params: &Parameters, loss: Loss) -> Evaluation {
    pb.check().expect("a consistent problem");
    let lay = Layout::new(pb);
    params.fits(pb, &lay).expect("the parameters of the problem");
    let st = Structure::new(pb);
    let ev = evaluate_blocks(pb, &lay, &st, params, loss, true);
    let (gradient, _) = gradient_and_diagonal(pb, &lay, &st, &ev);
    let jacobian = triplets(pb, &lay, &st, &ev);
    Evaluation { cost: ev.cost, residuals: ev.residuals, gradient, jacobian }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_losses_are_scipys() {
        // huber: z below 1 unchanged, above 2 sqrt(z) - 1, times f_scale^2 of z = (r / f_scale)^2
        assert_eq!(Loss::Huber(1.0).rho(0.25), 0.25);
        assert_eq!(Loss::Huber(1.0).rho(4.0), 3.0);
        assert_eq!(Loss::Huber(2.0).rho(16.0), 4.0 * (2.0 * 2.0 - 1.0));
        assert!((Loss::Cauchy(2.0).rho(4.0) - 4.0 * 2f64.ln()).abs() < 1e-15);
        assert_eq!(Loss::None.rho(7.0), 7.0);
        // the slopes are the derivatives, by differences (away from the kink of Huber at a^2), the
        // curvatures the second derivatives of rho(r^2) / 2 in r where those are above the floor
        for loss in [Loss::Huber(1.5), Loss::Cauchy(0.7)] {
            for s in [0.1, 1.0, 3.0, 40.0] {
                let h = 1e-6 * s;
                let want = (loss.rho(s + h) - loss.rho(s - h)) / (2.0 * h);
                assert!((loss.slope(s) - want).abs() < 1e-7, "{loss:?} at {s}: {} vs {want}", loss.slope(s));
                let r = s.sqrt();
                let h = 1e-5;
                let (plus, minus) = (loss.rho((r + h) * (r + h)), loss.rho((r - h) * (r - h)));
                let second = (plus - 2.0 * loss.rho(s) + minus) / (2.0 * h * h);
                let want = second.max(CURVATURE_FLOOR * loss.slope(s));
                assert!((loss.curvature(s) - want).abs() < 1e-4, "{loss:?} at {s}: {} vs {want}", loss.curvature(s));
            }
        }
        assert_eq!((Loss::None.slope(40.0), Loss::None.curvature(40.0)), (1.0, 1.0));
        // Huber outliers have no curvature, Cauchy's is negative beyond the scale: the floor
        assert_eq!(Loss::Huber(1.0).curvature(16.0), CURVATURE_FLOOR * 0.25);
        assert_eq!(Loss::Cauchy(1.0).curvature(9.0), CURVATURE_FLOOR * 0.1);
        assert!((Loss::Cauchy(1.0).curvature(0.25) - 0.8 * 0.6).abs() < 1e-15);
    }

    #[test]
    fn groups_keep_the_order_of_the_indices() {
        let g = Groups::new(4, &[2, 0, 2, 3, 0, 2]);
        assert_eq!(g.of(0), &[1, 4]);
        assert_eq!(g.of(1), &[] as &[usize]);
        assert_eq!(g.of(2), &[0, 2, 5]);
        assert_eq!(g.of(3), &[3]);
    }

    #[test]
    fn the_dual_pixels_are_the_f64_pixels_and_a_point_behind_is_none() {
        let r0 = [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]];
        let params = [1000.0, 1000.0, 600.0, 450.0, -0.02, 0.01, 0.001, -0.002, 0.0, 0.0, 0.0, 0.0];
        let velocity = Some(([0.4, 0.0, 0.0], [0.0, 0.001, 0.0]));
        let plain = View { delta: [0.0; 3], centre: [1.0, 2.0, 30.0], point: [3.5, -1.0, 0.2], params, velocity };
        let dual = View::<Dual<15>> {
            delta: variables([0.0; 3], 0),
            centre: variables([1.0, 2.0, 30.0], 3),
            point: variables([3.5, -1.0, 0.2], 6),
            params: params.map(Dual::constant),
            velocity: Some((variables([0.4, 0.0, 0.0], 9), variables([0.0, 0.001, 0.0], 12))),
        };
        let xy = [700.0, 400.0];
        let a = reprojection(Model::OpenCv, &r0, &plain, xy, xy[1] / 900.0 - 0.5, 0.5).unwrap();
        let b = reprojection(Model::OpenCv, &r0, &dual, xy, xy[1] / 900.0 - 0.5, 0.5).unwrap();
        assert_eq!([a[0].to_bits(), a[1].to_bits()], [b[0].v.to_bits(), b[1].v.to_bits()]);
        // the derivative with respect to the depth of the point is that of a pinhole, roughly f / z^2
        assert!((b[0].d[8] / (-a[0] * 0.5 / 29.8 / 0.5)).abs() > 0.5 && b[0].d[8].is_finite());
        let behind = View { point: [3.5, -1.0, 31.0], ..plain };
        assert!(reprojection(Model::OpenCv, &r0, &behind, xy, 0.0, 0.5).is_none());
        // two markers at the same place: a bar without a direction, not NaN
        let same = bar(variables::<6>([1.0, 2.0, 3.0], 0), variables::<6>([1.0, 2.0, 3.0], 3), 2.0, 0.5);
        assert_eq!((same.v, same.d), (-4.0, [0.0; 6]));
    }
}
