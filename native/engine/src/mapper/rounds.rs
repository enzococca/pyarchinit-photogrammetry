//! The rounds of adjustment of the mapper (step 5 of the design): the bundle adjustment of
//! milestone 1 (`bundle::solve`) on the positioned block, the rotations free from the first round,
//! the positions of the photos as residuals with a bias per flight, the observations filtered,
//! the tracks triangulated again and the observations that fit taken back, the intrinsics freed in
//! turn, and a last adjustment with every track.
//!
//! The filter follows COLMAP's global mapper (`src/colmap/sfm/global_mapper.cc`): after the
//! positioning the normalised reprojection threshold is 10 times `Options::max_normalized_reprojection`
//! (`POSITIONED_FILTER_SCALE`, applied once at the start of `refine`); after the adjustment of round
//! `r` (from 1) it is `max(4 - r, 1)` times it (`filter_scale`: 3, 2, 1, COLMAP's `max(3 - ite, 1)`
//! of `IterativeBundleAdjustment`); the last adjustment with every track is filtered at the plain
//! threshold before and after (as `IterativeRetriangulateAndRefine`). Every filter is
//! `triangulate::filter`, which also drops the points whose rays meet at less than
//! `Options::min_triangulation_deg`.
//!
//! The rounds (`Options::rounds`, 3):
//! - each solves the registered photos, the points of the selected tracks with two observations
//!   kept or more, the positions of the photos (not those of `State::gnss_outliers`; three at
//!   least) with their sigmas, and with two flights or more a bias per flight, the first of them
//!   held at zero (`Problem::reference_flight`), under the robust loss of `Options::ba_loss_scale_px`
//!   on the tie points (Huber, or Cauchy with `Options::ba_loss_cauchy`); without positions the
//!   first registered photo is held;
//! - round 1 holds the intrinsics; from round 2 the focal lengths and the radial terms are free;
//!   the last round also frees the principal point and the decentring terms of the cameras whose
//!   sensor is OPENCV, only on a block held by the positions (`Frame::Gnss`): a camera of the mapper
//!   with one focal length (RADIAL, for a square OPENCV sensor) is widened to OPENCV for it, fx and
//!   fy free. Without positions the principal point is never freed, as COLMAP's mappers hold it.
//! - after the solve of round 1 on positions, the accuracy of the positions is estimated again
//!   (`estimate_sigmas`) and round 1 is solved once more with it.
//!
//! Then, unless `Options::final_all_tracks` is false, every track of three placed photos or more
//! without a point is triangulated with the poses held, filtered, and one last adjustment runs
//! with all of them (reported as round `rounds + 1`).
//!
//! Everything runs in a fixed order (the problems are built in the order of the photos and of the
//! tracks, the probes of `estimate_sigmas` drawn from a generator seeded here): two runs give the
//! same bits.

use std::time::Instant;

use nalgebra::{Matrix3, Vector3};

use super::input::Input;
use super::positioning::{Frame, Gnss};
use super::tracks::Tracks;
use super::triangulate;
use super::Options;
use crate::bundle::{self, CameraBlock, Loss, Obs, Problem, Solution};
use crate::camera::{project, transform, Camera, Model, Pose, MODELS};

/// The filter after the positioning: this many times the normalised threshold (COLMAP's global
/// mapper filters its positioned tracks at `10 * max_normalized_reproj_error`).
pub const POSITIONED_FILTER_SCALE: f64 = 10.0;
/// The lower bound of a sigma estimated again, metres, unless the recorded accuracy is below it.
pub const SIGMA_FLOOR_M: f64 = 0.3;
/// The probes of the stochastic trace of the redundancy of the positions (`estimate_sigmas`).
pub const SIGMA_PROBES: usize = 4;
/// The seed of the signs of the probes.
pub const SIGMA_PROBE_SEED: u64 = 7;
/// A camera widened to OPENCV whose `|fx - fy| / fx` ends above this is named in the notes.
pub const FX_FY_NOTE: f64 = 1e-3;
/// The fewest positions that enter a round (as the positioning needs three).
pub const MIN_POSITIONS: usize = 3;

/// The scale of the filter after the adjustment of round `round` (from 1): 3, 2, then 1.
pub fn filter_scale(round: usize) -> f64 {
    4usize.saturating_sub(round).max(1) as f64
}

/// COLMAP's name of a model.
pub fn model_name(model: Model) -> &'static str {
    match model {
        Model::SimplePinhole => MODELS[0],
        Model::Pinhole => MODELS[1],
        Model::SimpleRadial => MODELS[2],
        Model::Radial => MODELS[3],
        Model::OpenCv => MODELS[4],
        Model::FullOpenCv => MODELS[5],
    }
}

/// A camera of the mapper as the rounds adjust it: its model, its parameters in COLMAP's order and
/// the size of its photos.
#[derive(Clone, Debug, PartialEq)]
pub struct CameraState {
    pub model: Model,
    pub params: Vec<f64>,
    pub width: f64,
    pub height: f64,
}

impl CameraState {
    pub fn camera(&self) -> Camera {
        Camera::new(model_name(self.model), &self.params, self.width, self.height).expect("a camera of the six models")
    }

    /// The camera widened to OPENCV (the same projection, fx = fy, no decentring yet); `None` for a
    /// model that is OPENCV already or has more terms than OPENCV (FULL_OPENCV).
    fn widened(&self) -> Option<CameraState> {
        let p = &self.params;
        let params = match self.model {
            Model::SimplePinhole => vec![p[0], p[0], p[1], p[2], 0.0, 0.0, 0.0, 0.0],
            Model::Pinhole => vec![p[0], p[1], p[2], p[3], 0.0, 0.0, 0.0, 0.0],
            Model::SimpleRadial => vec![p[0], p[0], p[1], p[2], p[3], 0.0, 0.0, 0.0],
            Model::Radial => vec![p[0], p[0], p[1], p[2], p[3], p[4], 0.0, 0.0],
            Model::OpenCv | Model::FullOpenCv => return None,
        };
        Some(CameraState { model: Model::OpenCv, params, width: self.width, height: self.height })
    }
}

/// The groups of intrinsics of a model by their indices: focal lengths, radial terms, principal
/// point, decentring terms.
fn groups(model: Model) -> [&'static [usize]; 4] {
    match model {
        Model::SimplePinhole => [&[0], &[], &[1, 2], &[]],
        Model::Pinhole => [&[0, 1], &[], &[2, 3], &[]],
        Model::SimpleRadial => [&[0], &[3], &[1, 2], &[]],
        Model::Radial => [&[0], &[3, 4], &[1, 2], &[]],
        Model::OpenCv => [&[0, 1], &[4, 5], &[2, 3], &[6, 7]],
        Model::FullOpenCv => [&[0, 1], &[4, 5, 8, 9, 10, 11], &[2, 3], &[6, 7]],
    }
}

/// The names of the groups of `groups`, as the reports give them.
pub const INTRINSICS: [&str; 4] = ["focal", "radial", "principal_point", "decentring"];

/// The accuracy of the positions estimated again: the variance factors of the horizontal and the
/// vertical residuals (in units of the recorded accuracy), their redundancies, the sigmas they give
/// for the largest recorded accuracy (clamped as `State::gnss_sigma`), and the positions used.
#[derive(Clone, Debug, PartialEq)]
pub struct SigmaEstimate {
    pub factor: [f64; 2],
    pub redundancy: [f64; 2],
    pub sigma_h: f64,
    pub sigma_v: f64,
    pub positions: usize,
}

/// The block the rounds refine (built by the mapper from the positioning): per photo of the input
/// its `world_from_cam` rotation and centre (`None` for a photo not placed); the cameras of the
/// input and whether each one's sensor is OPENCV; the tracks, those selected, a point per track
/// (`None` for none) and a flag per observation of the tracks (kept by the last filter); the frame
/// of the positioning, the positions with the photos whose position is an outlier (ascending), the
/// sigmas used for each position, the estimate of their accuracy, the bias of each flight (sorted,
/// as the positioning gives them) and, after the rounds, the posterior covariance of the biases of
/// the last adjustment (`posterior_bias_covariance`; the reference flight has none) with the
/// variance factors of its groups (tie points, horizontal and vertical positions); the
/// observations of the tracks with a point that the last filter saw and those it kept; notes of
/// the rounds.
#[derive(Clone, Debug, PartialEq)]
pub struct State {
    pub world_from_cam: Vec<Option<[[f64; 3]; 3]>>,
    pub centres: Vec<Option<[f64; 3]>>,
    pub cameras: Vec<CameraState>,
    pub opencv_sensor: Vec<bool>,
    pub tracks: Tracks,
    pub selected: Vec<usize>,
    pub points: Vec<Option<[f64; 3]>>,
    pub keep: Vec<bool>,
    pub frame: Frame,
    pub gnss: Option<Gnss>,
    pub gnss_outliers: Vec<usize>,
    pub gnss_sigma: Vec<[f64; 3]>,
    pub sigma_estimate: Option<SigmaEstimate>,
    pub bias: Vec<(i64, [f64; 3])>,
    pub bias_covariance: Vec<(i64, [[f64; 3]; 3])>,
    pub variance_factors: Option<[f64; 3]>,
    pub last_filter: (usize, usize),
    pub notes: Vec<String>,
}

/// What a round did: its number (from 1; `rounds + 1` for the last adjustment with every track),
/// the groups of intrinsics it adjusted (`INTRINSICS`), the cost before and after its adjustment,
/// the observations adjusted, those its filter dropped and those it took back, its seconds.
#[derive(Clone, Debug, PartialEq)]
pub struct RoundReport {
    pub round: usize,
    pub intrinsics: Vec<String>,
    pub cost_initial: f64,
    pub cost: f64,
    pub observations: usize,
    pub filtered: usize,
    pub added: usize,
    pub seconds: f64,
}

fn transpose(r: &[[f64; 3]; 3]) -> [[f64; 3]; 3] {
    std::array::from_fn(|i| std::array::from_fn(|j| r[j][i]))
}

impl State {
    /// Whether the photo `i` is placed.
    pub fn placed(&self, i: usize) -> bool {
        self.world_from_cam[i].is_some() && self.centres[i].is_some()
    }

    /// The `cam_from_world` pose of each photo, `None` for a photo not placed.
    pub fn poses(&self) -> Vec<Option<Pose>> {
        (0..self.centres.len())
            .map(|i| {
                let (w, c) = (self.world_from_cam[i]?, self.centres[i]?);
                let r = Matrix3::from_fn(|a, b| w[b][a]);
                let t = -(r * Vector3::from(c));
                Some(std::array::from_fn(|a| [r[(a, 0)], r[(a, 1)], r[(a, 2)], t[a]]))
            })
            .collect()
    }

    pub fn cams(&self) -> Vec<Camera> {
        self.cameras.iter().map(CameraState::camera).collect()
    }

    /// The bias of `flight` (zero when it has none).
    fn bias_of(&self, flight: i64) -> [f64; 3] {
        self.bias.iter().find(|(f, _)| *f == flight).map_or([0.0; 3], |(_, b)| *b)
    }

    /// The observations kept of track `t`, as indices into `tracks.observations`.
    fn kept(&self, t: usize) -> impl Iterator<Item = usize> + '_ {
        (self.tracks.offsets[t]..self.tracks.offsets[t + 1]).filter(|&i| self.keep[i])
    }

    /// The reprojection error in pixels of every observation kept of a track with a point.
    pub fn reprojection_px(&self, input: &Input) -> Vec<f64> {
        let poses = self.poses();
        let mut out = Vec::new();
        for (t, x) in self.points.iter().enumerate() {
            let Some(x) = x else { continue };
            for i in self.kept(t) {
                let (image, k) = self.tracks.observations[i];
                let image = image as usize;
                if let Some(e) = self.error_px(input, &poses, image, k, x) {
                    out.push(e);
                }
            }
        }
        out
    }

    /// The reprojection error in pixels of the key point `k` of photo `image` against the point `x`.
    pub fn error_px(&self, input: &Input, poses: &[Option<Pose>], image: usize, k: u32, x: &[f64; 3]) -> Option<f64> {
        let pose = poses[image].as_ref()?;
        let (cx, cy, cz) = transform(pose, x[0], x[1], x[2]);
        let cam = &self.cameras[input.images[image].camera];
        let (u, v) = project(cam.model, &cam.params, cx / cz, cy / cz);
        let px = input.keypoint(image, k);
        Some((u - px[0]).hypot(v - px[1]))
    }
}

/// The positions that enter the rounds, as indices into `State::gnss`: those of placed photos but
/// the outliers, none when they are fewer than `MIN_POSITIONS`.
fn positions_used(state: &State) -> Vec<usize> {
    let Some(g) = &state.gnss else { return Vec::new() };
    let used: Vec<usize> = (0..g.image.len())
        .filter(|&k| {
            let i = g.image[k];
            i < state.centres.len() && state.placed(i) && state.gnss_outliers.binary_search(&i).is_err()
        })
        .collect();
    if used.len() < MIN_POSITIONS {
        Vec::new()
    } else {
        used
    }
}

/// A round's problem: the bundle problem and where its photos, points and positions come from.
struct Built {
    pb: Problem,
    photos: Vec<usize>,
    tracks: Vec<usize>,
    /// The index in `State::gnss` of each position of the problem.
    positions: Vec<usize>,
}

/// The problem of the placed photos, the tracks with a point and two kept observations or more
/// (`all_tracks` false: the selected ones only), the positions that enter the rounds with the
/// sigmas of `State::gnss_sigma`, their targets less the bias of their flight, `free` the
/// intrinsics adjusted per camera.
fn build(state: &State, input: &Input, free: &[Vec<usize>], all_tracks: bool) -> Built {
    let n = input.images.len();
    let mut index = vec![None; n];
    let mut photos = Vec::new();
    for i in 0..n {
        if state.placed(i) {
            index[i] = Some(photos.len());
            photos.push(i);
        }
    }
    let mut used = vec![false; state.cameras.len()];
    for &i in &photos {
        used[input.images[i].camera] = true;
    }
    let cameras = state
        .cameras
        .iter()
        .enumerate()
        .map(|(c, cam)| CameraBlock {
            model: cam.model,
            params: cam.params.clone(),
            width: cam.width,
            height: cam.height,
            free: if used[c] { free[c].clone() } else { Vec::new() },
        })
        .collect();
    let mut is_selected = vec![all_tracks; state.tracks.len()];
    for &t in &state.selected {
        is_selected[t] = true;
    }
    let (mut points, mut tracks, mut obs) = (Vec::new(), Vec::new(), Vec::new());
    for t in 0..state.tracks.len() {
        let Some(x) = state.points[t] else { continue };
        if !is_selected[t] || state.kept(t).count() < 2 {
            continue;
        }
        let p = points.len();
        points.push(x);
        tracks.push(t);
        for i in state.kept(t) {
            let (image, k) = state.tracks.observations[i];
            let image = image as usize;
            let at = index[image].expect("a kept observation has a placed photo");
            obs.push(Obs { image: at, point: p, xy: input.keypoint(image, k) });
        }
    }
    let positions = positions_used(state);
    let g = state.gnss.as_ref();
    let gnss: Vec<(usize, [f64; 3], [f64; 3], i64)> = positions
        .iter()
        .map(|&k| {
            let g = g.expect("positions");
            let b = state.bias_of(g.flight[k]);
            let at = index[g.image[k]].expect("a position of a placed photo");
            (at, std::array::from_fn(|a| g.xyz[k][a] - b[a]), state.gnss_sigma[k], g.flight[k])
        })
        .collect();
    let mut flights: Vec<i64> = gnss.iter().map(|g| g.3).collect();
    flights.sort_unstable();
    flights.dedup();
    let mut fixed_images = vec![false; photos.len()];
    if gnss.is_empty() && !photos.is_empty() {
        fixed_images[0] = true;
    }
    let pb = Problem {
        rotations: photos.iter().map(|&i| transpose(&state.world_from_cam[i].expect("placed"))).collect(),
        centres: photos.iter().map(|&i| state.centres[i].expect("placed")).collect(),
        image_camera: photos.iter().map(|&i| input.images[i].camera).collect(),
        cameras,
        points,
        obs,
        gnss,
        fixed_images,
        gnss_bias: flights.len() >= 2,
        reference_flight: flights.first().copied(),
        ..Problem::default()
    };
    Built { pb, photos, tracks, positions }
}

/// The solution of a round written into the state: poses, intrinsics, points, biases (the solved
/// bias of a flight is added to the one its targets were reduced by).
fn write_back(state: &mut State, b: &Built, sol: &Solution) {
    for (j, &i) in b.photos.iter().enumerate() {
        state.world_from_cam[i] = Some(transpose(&sol.rotations[j]));
        state.centres[i] = Some(sol.centres[j]);
    }
    for (c, params) in sol.camera_params.iter().enumerate() {
        state.cameras[c].params.clone_from(params);
    }
    for (p, &t) in b.tracks.iter().enumerate() {
        state.points[t] = Some(sol.points[p]);
    }
    for &(flight, inc) in &sol.bias {
        match state.bias.iter_mut().find(|(f, _)| *f == flight) {
            Some((_, b)) => (0..3).for_each(|a| b[a] += inc[a]),
            None => {
                state.bias.push((flight, inc));
                state.bias.sort_by_key(|(f, _)| *f);
            }
        }
    }
}

fn loss_of(o: &Options) -> Loss {
    if o.ba_loss_cauchy {
        Loss::Cauchy(o.ba_loss_scale_px)
    } else {
        Loss::Huber(o.ba_loss_scale_px)
    }
}

fn adjust(pb: &Problem, o: &Options, observe: &mut dyn FnMut() -> bool) -> Result<Solution, String> {
    let opts = bundle::Options { loss: loss_of(o), ..bundle::Options::default() };
    bundle::solve(pb, &opts, &mut |_, _| observe())
}

/// `triangulate::filter` at `scale` times the normalised threshold over the points of the state:
/// the flags of the observations replaced, the points of the tracks with no observation left
/// dropped; `State::last_filter` set.
fn filter(state: &mut State, input: &Input, o: &Options, scale: f64) -> Result<(), String> {
    let scaled = Options { max_normalized_reprojection: o.max_normalized_reprojection * scale, ..o.clone() };
    let f = triangulate::filter(&state.tracks, &state.points, input, &state.cams(), &state.poses(), &scaled)?;
    let mut seen = 0;
    for t in 0..state.tracks.len() {
        let range = state.tracks.offsets[t]..state.tracks.offsets[t + 1];
        if state.points[t].is_some() {
            seen += range.len();
        }
        if !range.clone().any(|i| f.keep[i]) {
            state.points[t] = None;
        }
    }
    state.keep = f.keep;
    state.last_filter = (seen, state.keep.iter().filter(|&&k| k).count());
    Ok(())
}

/// The tracks of `which` without a point triangulated from the rays of every observation in a
/// placed photo, the poses held.
fn triangulate_missing(state: &mut State, input: &Input, which: &[usize]) {
    let cams = state.cams();
    for &t in which {
        if state.points[t].is_some() {
            continue;
        }
        let rays: Vec<([f64; 3], [f64; 3])> = state
            .tracks
            .track(t)
            .iter()
            .filter_map(|&(image, k)| {
                let image = image as usize;
                let (w, c) = (state.world_from_cam[image]?, state.centres[image]?);
                let px = input.keypoint(image, k);
                let (u, v) = cams[input.images[image].camera].normalised(px[0], px[1]);
                let d = Matrix3::from_fn(|a, b| w[a][b]) * Vector3::new(u, v, 1.0);
                Some((c, [d.x, d.y, d.z]))
            })
            .collect();
        state.points[t] = triangulate::point(&rays);
    }
}

/// Filter at `scale`, triangulate again the tracks of `which` left without a point and take back
/// the observations that fit (a second filter): the observations dropped and taken back against the
/// flags before.
fn filter_and_add(
    state: &mut State,
    input: &Input,
    o: &Options,
    scale: f64,
    which: &[usize],
) -> Result<(usize, usize), String> {
    let before = state.keep.clone();
    filter(state, input, o, scale)?;
    triangulate_missing(state, input, which);
    filter(state, input, o, scale)?;
    let filtered = before.iter().zip(&state.keep).filter(|(b, k)| **b && !**k).count();
    let added = before.iter().zip(&state.keep).filter(|(b, k)| !**b && **k).count();
    Ok((filtered, added))
}

/// Knuth's MMIX linear congruential generator, for the signs of the probes.
struct Lcg(u64);

impl Lcg {
    fn sign(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        if self.0 >> 63 == 1 {
            1.0
        } else {
            -1.0
        }
    }
}

/// The fitted position of each position of `pb` in the solution `sol`: the centre of its photo
/// plus the solved bias of its flight.
fn fitted(pb: &Problem, sol: &Solution) -> Vec<[f64; 3]> {
    pb.gnss
        .iter()
        .map(|&(at, _, _, flight)| {
            let bias = sol.bias.iter().find(|(f, _)| *f == flight).map_or([0.0; 3], |(_, b)| *b);
            std::array::from_fn(|a| sol.centres[at][a] + bias[a])
        })
        .collect()
}

/// The accuracy of the positions estimated again from the solution `sol` of the problem `b`
/// (variance-component estimation, Förstner's: per axis group, horizontal and vertical,
/// `sigma^2 = v^T P v / r` with `r = n - tr(H)` the redundancy, `H = J_g N^-1 J_g^T` the part of
/// the hat matrix of the positions, in units of their sigmas). The trace is Hutchinson's stochastic
/// one through the solver: the targets moved by one sigma times a random sign each (`SIGMA_PROBES`
/// draws from `SIGMA_PROBE_SEED`), the problem solved again from `sol`, `H z` the move of the
/// fitted positions in sigmas, `z^T H z` an unbiased estimate of the trace (summed over the rows of
/// each group). The residuals of a fit are smaller than the noise by the share the block absorbs;
/// the redundancy takes that share out.
fn estimate_sigmas(
    b: &Built,
    sol: &Solution,
    o: &Options,
    observe: &mut dyn FnMut() -> bool,
) -> Result<Option<[f64; 4]>, String> {
    let m = b.pb.gnss.len();
    if m < MIN_POSITIONS {
        return Ok(None);
    }
    let base = fitted(&b.pb, sol);
    let (mut sum_h, mut sum_v) = (0.0, 0.0);
    for (j, &(_, target, sigma, _)) in b.pb.gnss.iter().enumerate() {
        let v: [f64; 3] = std::array::from_fn(|a| (base[j][a] - target[a]) / sigma[a]);
        sum_h += v[0] * v[0] + v[1] * v[1];
        sum_v += v[2] * v[2];
    }
    // the problem at the solution, the solved biases folded into the targets
    let mut start = b.pb.clone();
    start.rotations.clone_from(&sol.rotations);
    start.centres.clone_from(&sol.centres);
    start.points.clone_from(&sol.points);
    for (cam, params) in start.cameras.iter_mut().zip(&sol.camera_params) {
        cam.params.clone_from(params);
    }
    for g in &mut start.gnss {
        let bias = sol.bias.iter().find(|(f, _)| *f == g.3).map_or([0.0; 3], |(_, b)| *b);
        for a in 0..3 {
            g.1[a] -= bias[a];
        }
    }
    let mut random = Lcg(SIGMA_PROBE_SEED);
    let (mut trace_h, mut trace_v) = (0.0, 0.0);
    for _ in 0..SIGMA_PROBES {
        let mut probe = start.clone();
        let mut z = vec![[0.0; 3]; m];
        for (j, g) in probe.gnss.iter_mut().enumerate() {
            for a in 0..3 {
                z[j][a] = random.sign();
                g.1[a] += g.2[a] * z[j][a];
            }
        }
        let moved = adjust(&probe, o, observe)?;
        let moved_fit = fitted(&probe, &moved);
        for j in 0..m {
            let sigma = b.pb.gnss[j].2;
            // the base fit in the frame of the probe: the centre alone (the biases folded)
            let hz: [f64; 3] = std::array::from_fn(|a| (moved_fit[j][a] - sol.centres[b.pb.gnss[j].0][a]) / sigma[a]);
            trace_h += z[j][0] * hz[0] + z[j][1] * hz[1];
            trace_v += z[j][2] * hz[2];
        }
    }
    let k = SIGMA_PROBES as f64;
    let (r_h, r_v) = (2.0 * m as f64 - trace_h / k, m as f64 - trace_v / k);
    if !(r_h > 0.0 && r_v > 0.0) {
        return Ok(None);
    }
    Ok(Some([sum_h / r_h, sum_v / r_v, r_h, r_v]))
}

/// The sigma of a recorded accuracy `recorded` scaled by the variance factor `factor`, clamped to
/// `[min(SIGMA_FLOOR_M, recorded), recorded]`.
pub fn clamped_sigma(recorded: f64, factor: f64) -> f64 {
    (recorded * factor.sqrt()).clamp(SIGMA_FLOOR_M.min(recorded), recorded)
}

/// Whether the camera `c` of `input` has an OPENCV sensor.
fn opencv(state: &State, input: &Input, c: usize) -> bool {
    state.opencv_sensor.get(c).copied().unwrap_or(false) || input.cameras[c].model == "OPENCV"
}

/// The intrinsics adjusted per camera in round `round` of `rounds`, and the names of their groups.
fn intrinsics(state: &State, round: usize, rounds: usize, positions: bool) -> (Vec<Vec<usize>>, Vec<String>) {
    let mut named = [false; 4];
    let free = state
        .cameras
        .iter()
        .enumerate()
        .map(|(c, cam)| {
            if round < 2 {
                return Vec::new();
            }
            let principal = round >= rounds && positions && state.frame == Frame::Gnss && state.opencv_sensor[c];
            let groups = groups(cam.model);
            let take = if principal { 4 } else { 2 };
            let mut free = Vec::new();
            for (g, idx) in groups.iter().enumerate().take(take) {
                named[g] |= !idx.is_empty();
                free.extend_from_slice(idx);
            }
            free.sort_unstable();
            free
        })
        .collect();
    let names = INTRINSICS.iter().zip(named).filter(|(_, n)| *n).map(|(s, _)| s.to_string()).collect();
    (free, names)
}

/// The posterior covariance of the biases of the flights after the adjustment `sol` of `b`, with a
/// variance component per group of observations (ruling R14): the normal matrix is the sum of the
/// groups' each over its own factor, then inverted (`bundle::bias_covariance` on the problem with
/// the sigmas so scaled). The factor of the positions is the one `estimate_sigmas` found
/// (horizontal and vertical, the recorded accuracy times its square root, not clamped); the
/// factor of the tie points is `v^T P v / r` of their own residuals (twice their cost), `r` their
/// rows less the unknowns plus the part of the unknowns the positions take (`tr(H)` of the
/// positions, from the redundancy `estimate_sigmas` measured). The robust loss keeps its scale in
/// pixels. The covariance, and the factors (tie points, horizontal, vertical).
fn posterior_bias_covariance(
    state: &State,
    b: &Built,
    sol: &Solution,
    o: &Options,
) -> Result<(Vec<(i64, [[f64; 3]; 3])>, [f64; 3]), String> {
    let (fh, fv, taken) = match &state.sigma_estimate {
        Some(e) => (e.factor[0], e.factor[1], 3.0 * e.positions as f64 - e.redundancy[0] - e.redundancy[1]),
        None => (1.0, 1.0, 0.0),
    };
    let fit = fitted(&b.pb, sol);
    let gnss_cost: f64 = b
        .pb
        .gnss
        .iter()
        .zip(&fit)
        .map(|(&(_, target, sigma, _), f)| (0..3).map(|a| ((f[a] - target[a]) / sigma[a]).powi(2)).sum::<f64>())
        .sum::<f64>()
        / 2.0;
    let tie_cost = sol.cost - gnss_cost;
    let unknowns = bundle::Layout::new(&b.pb).size as f64;
    let redundancy = 2.0 * b.pb.obs.len() as f64 - (unknowns - taken);
    let ft = if redundancy > 0.0 && tie_cost > 0.0 { 2.0 * tie_cost / redundancy } else { 1.0 };
    let mut pb = b.pb.clone();
    let g = state.gnss.as_ref();
    for (j, &k) in b.positions.iter().enumerate() {
        let rec = g.expect("positions").sigma[k];
        pb.gnss[j].2 = [rec[0] * fh.sqrt(), rec[1] * fh.sqrt(), rec[2] * fv.sqrt()];
    }
    pb.pixel_sigma = b.pb.pixel_sigma * ft.sqrt();
    let scale = o.ba_loss_scale_px / pb.pixel_sigma;
    let loss = if o.ba_loss_cauchy { Loss::Cauchy(scale) } else { Loss::Huber(scale) };
    Ok((bundle::bias_covariance(&pb, sol, loss)?, [ft, fh, fv]))
}

/// Whether round `round` is the last adjustment (no adjustment with every track after it).
fn last_adjustment(o: &Options, round: usize) -> bool {
    !o.final_all_tracks && round == o.rounds
}

/// One adjustment of the state with the intrinsics `free` (and, for round 1 on positions, the
/// accuracy of the positions estimated again and a second solve with it): the costs and the
/// observations adjusted.
fn adjust_state(
    state: &mut State,
    input: &Input,
    o: &Options,
    free: &[Vec<usize>],
    all_tracks: bool,
    estimate: bool,
    covariance: bool,
    observe: &mut dyn FnMut() -> bool,
) -> Result<(f64, f64, usize), String> {
    let b = build(state, input, free, all_tracks);
    let sol = adjust(&b.pb, o, observe)?;
    write_back(state, &b, &sol);
    if covariance && b.pb.gnss_bias {
        let (covariance, factors) = posterior_bias_covariance(state, &b, &sol, o)?;
        state.bias_covariance = covariance;
        state.variance_factors = Some(factors);
    }
    let (cost_initial, mut cost) = (sol.cost_initial, sol.cost);
    if estimate && !b.positions.is_empty() {
        if let Some([fh, fv, rh, rv]) = estimate_sigmas(&b, &sol, o, observe)? {
            let g = state.gnss.as_ref().expect("positions");
            let mut largest = [0.0f64; 2];
            for k in 0..g.image.len() {
                let rec = g.sigma[k];
                state.gnss_sigma[k] =
                    [clamped_sigma(rec[0], fh), clamped_sigma(rec[1], fh), clamped_sigma(rec[2], fv)];
                largest = [largest[0].max(rec[0]).max(rec[1]), largest[1].max(rec[2])];
            }
            state.sigma_estimate = Some(SigmaEstimate {
                factor: [fh, fv],
                redundancy: [rh, rv],
                sigma_h: clamped_sigma(largest[0], fh),
                sigma_v: clamped_sigma(largest[1], fv),
                positions: b.positions.len(),
            });
            let again = build(state, input, free, all_tracks);
            let sol = adjust(&again.pb, o, observe)?;
            write_back(state, &again, &sol);
            cost = sol.cost;
        }
    }
    Ok((cost_initial, cost, b.pb.obs.len()))
}

/// The rounds of adjustment of the state (see the module): one report per round, and one for the
/// last adjustment with every track. `observe` is called between the steps and after every
/// iteration of a solve; `false` stops with `Err("cancelled")` (the state is then partly refined).
///
/// `Err` when the state does not fit `input` (the tracks, a photo, a camera), when
/// `o.ba_loss_scale_px` is not a positive finite number, or when a solve fails.
pub fn refine(
    state: &mut State,
    input: &Input,
    o: &Options,
    observe: &mut dyn FnMut() -> bool,
) -> Result<Vec<RoundReport>, String> {
    let n = input.images.len();
    if state.world_from_cam.len() != n || state.centres.len() != n || state.cameras.len() != input.cameras.len() {
        return Err(format!("the state holds {} rotations, {} centres and {} cameras for {n} images and {} cameras",
                           state.world_from_cam.len(), state.centres.len(), state.cameras.len(),
                           input.cameras.len()));
    }
    state.tracks.check(input)?;
    if state.points.len() != state.tracks.len() || state.keep.len() != state.tracks.observations.len() {
        return Err("the points or the flags of the state do not fit its tracks".into());
    }
    if !(o.ba_loss_scale_px > 0.0 && o.ba_loss_scale_px.is_finite()) {
        return Err(format!("ba_loss_scale_px must be a positive number, not {}", o.ba_loss_scale_px));
    }
    if !observe() {
        return Err("cancelled".into());
    }
    state.opencv_sensor = (0..input.cameras.len()).map(|c| opencv(state, input, c)).collect();
    let positions = !positions_used(state).is_empty();
    filter(state, input, o, POSITIONED_FILTER_SCALE)?;
    let mut reports = Vec::new();
    let mut last_free = vec![Vec::new(); input.cameras.len()];
    let mut last_names = Vec::new();
    let rounds = o.rounds;
    for round in 1..=rounds {
        let clock = Instant::now();
        if !observe() {
            return Err("cancelled".into());
        }
        if round >= 2 && round == rounds && positions && state.frame == Frame::Gnss {
            for c in 0..state.cameras.len() {
                if state.opencv_sensor[c] {
                    if let Some(w) = state.cameras[c].widened() {
                        state.cameras[c] = w;
                    }
                }
            }
        }
        let (free, names) = intrinsics(state, round, rounds, positions);
        let (cost_initial, cost, observations) =
            adjust_state(state, input, o, &free, false, round == 1, last_adjustment(o, round), observe)?;
        if !observe() {
            return Err("cancelled".into());
        }
        let selected = state.selected.clone();
        let (filtered, added) = filter_and_add(state, input, o, filter_scale(round), &selected)?;
        reports.push(RoundReport {
            round,
            intrinsics: names.clone(),
            cost_initial,
            cost,
            observations,
            filtered,
            added,
            seconds: clock.elapsed().as_secs_f64(),
        });
        last_free = free;
        last_names = names;
    }
    for (c, cam) in state.cameras.iter().enumerate() {
        if cam.model == Model::OpenCv && last_free[c].contains(&2) {
            let split = (cam.params[0] - cam.params[1]).abs() / cam.params[0];
            if split > FX_FY_NOTE {
                state.notes.push(format!(
                    "camera {c}: |fx - fy| / fx = {split:.2e} after the principal point was freed"
                ));
            }
        }
    }
    if o.final_all_tracks {
        let clock = Instant::now();
        if !observe() {
            return Err("cancelled".into());
        }
        let before = state.keep.clone();
        // tracks of two photos are not triangulated again (pycolmap's
        // `mapper.retriangulation.ignore_two_view_tracks`, true): two rays of a wrong match meet
        // anywhere along its epipolar line
        let placed_views = |t: usize| state.tracks.track(t).iter().filter(|&&(i, _)| state.placed(i as usize)).count();
        let every: Vec<usize> = (0..state.tracks.len()).filter(|&t| placed_views(t) >= 3).collect();
        triangulate_missing(state, input, &every);
        filter(state, input, o, 1.0)?;
        let (cost_initial, cost, observations) =
            adjust_state(state, input, o, &last_free, true, false, true, observe)?;
        if !observe() {
            return Err("cancelled".into());
        }
        filter(state, input, o, 1.0)?;
        let filtered = before.iter().zip(&state.keep).filter(|(b, k)| **b && !**k).count();
        let added = before.iter().zip(&state.keep).filter(|(b, k)| !**b && **k).count();
        reports.push(RoundReport {
            round: rounds + 1,
            intrinsics: last_names,
            cost_initial,
            cost,
            observations,
            filtered,
            added,
            seconds: clock.elapsed().as_secs_f64(),
        });
    }
    Ok(reports)
}
