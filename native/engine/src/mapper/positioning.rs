//! The global positioning of the mapper (step 4 of the design): the centres of the photos and the
//! points of the selected tracks, the rotations held, with the positions of the photos (GNSS)
//! inside it when there are enough of them.
//!
//! The residual of an observation is GLOMAP's (`estimators/global_positioning.cc`, the
//! `BATAPairwiseDirectionError` of `estimators/cost_function.h`, after Zhuang et al. 2018): `s (X -
//! C) - v`, `v` the unit ray of the observation in the world, `X` the point, `C` the centre of the
//! photo and `s` a scale of that observation with a floor (`SCALE_FLOOR`): in the solve from the
//! random start a coordinate of the point's block (GLOMAP's), in the joint solve with the positions
//! eliminated in closed form, so that a point block is its three coordinates (`Scales`); under the
//! Huber loss of `Options::positioning_huber`, the observations of a camera without a prior focal
//! length weighed by a half (GLOMAP's `ScaledLoss`); tracks seen by fewer than `MIN_VIEWS` photos
//! left out.
//!
//! 1. **With GNSS** (at least 3 placed photos with a position) the block starts at the positions
//!    (`from_positions`). The averaged rotations live in a frame of their own, turned onto the
//!    frame of the positions by the rotation `Q` that best takes the directions between the photos
//!    of the verified pairs with parallax (the translation of the pair, put in the averaged frame)
//!    onto the directions between their positions: Horn's closed form in iteratively reweighted
//!    least squares with the Huber weights of `FRAME_HUBER_DEG` on the angle (ruling R11). When
//!    the positions spread little across the block (twice the square root of the second
//!    eigenvalue of the covariance of their horizontal coordinates below `POSITION_SPREAD_SIGMAS`
//!    times the median of their recorded horizontal accuracy), with fewer than `MIN_FRAME_PAIRS`
//!    pairs, directions that span little more than a line (`MIN_SPREAD`), or a median angle left
//!    above `MAX_FRAME_RESIDUAL_DEG` (baselines whose directions are the noise of the positions),
//!    the positions cannot orient the block: the start of step 4 is taken.
//! 2. In that first frame, a position that disagrees with the positions of most of its
//!    neighbours of the same flight across the direction of their pair (the rays give the
//!    direction between two photos, not their distance) is left out of the start
//!    (`local_by_directions`, the local pre-check of ruling R9); the offset of each flight (the
//!    first at zero) is solved from the pairs across two flights, whose centres differ along the
//!    direction of the pair (`flight_offsets`), and the frame is turned again on the positions
//!    less those offsets, the two in turn until the offsets settle (a flight of a single strip
//!    cannot hold the roll about its axis: a height offset per strip is a roll).
//! 3. Each centre starts at its position less its flight's offset; a placed photo without one
//!    (or whose position was left out) from its rays (`resect`: the centre nearest the lines from
//!    the points triangulated by the others, back along its rays), else at the mean of its
//!    neighbours. The selected tracks are triangulated from the starts with the rotations `Q R`
//!    and go through `triangulate::filter` (the reprojection left out: the start is only as good
//!    as the positions; the depth and the angle of the rays kept). Centres, points and biases are
//!    solved together (`bundle::lm`, the scales of the rays eliminated) with every position used
//!    as a residual `(C + b - g) / sigma` per axis under Huber 1 (in sigmas), `sigma` the recorded
//!    accuracy (estimated again from the residuals only in the rounds of adjustment that follow,
//!    where the redundancy of each position is known: the residuals of a fit are smaller than the
//!    noise), `b` the bias of the position's flight: one per flight when there are two flights or
//!    more, the first held at zero, starting at the offsets of step 2. The positions hold the
//!    gauge. The rays keep GLOMAP's scale: at this level the heights of nadir photos are weakly
//!    held (the depth along each ray is free) and follow their positions; the reprojection of the
//!    rounds holds them. The Huber loss bounds the pull of a gross fix; the positions then farther
//!    than `OUTLIER_SIGMAS` of their recorded accuracy from the solved centres and biases (the
//!    horizontal axes together, or the vertical) are the outliers (`gnss_outliers`), a position
//!    left out in step 2 that the solved block agrees with is taken back, and the block is solved
//!    once more when anything changed. The start then passes its check (`start_check`): at most
//!    `START_OUTLIER_SHARE` of the positions outliers, and no flight of `MIN_CHECKED_FLIGHT`
//!    positions or more with more than half of them.
//! 4. **The fallback** (the positions cannot orient the block, or the start failed its check):
//!    the rays alone, as without GNSS (step 5), put on the positions by one robust similarity
//!    (Umeyama's, iteratively reweighted with Huber 1 on the residual in sigmas) with, for two
//!    flights or more, a translation per flight (the first held at zero). No position is left out
//!    against that block (ruling R9: the rays alone drift, and a position far from it is often a
//!    good one where the drift is largest) but those that disagree with most of their neighbours
//!    on it (`local_outliers`); the frame is turned as in step 1 on the positions less those
//!    translations (the block on the positions, `Frame::Similarity`, when they cannot orient it,
//!    the positions farther than `OUTLIER_SIGMAS` from it its outliers), the centres start where
//!    the similarity put them (on its position a photo it did not place or placed farther than
//!    `OUTLIER_SIGMAS` from it) and are solved as in step 3. After a failed check the result with
//!    fewer outliers is kept; the notes say which (`Positioned::notes`).
//! 5. **Without GNSS**: centres and points start at random (GLOMAP's start: uniform in a cube of
//!    `RANDOM_EXTENT`, the scales at 1, from the generator of this file seeded with `SEED`), one
//!    centre and the first scale held (GLOMAP holds the first scale), the scales above their
//!    floors (here coordinates of the point blocks: eliminated, they stop short from a random
//!    start); `Frame::Free`.
//!
//! Everything runs in a fixed order and the solver sums in a fixed order: two runs give the same
//! bits.

use nalgebra::{DMatrix, DVector, Matrix2, Matrix3, Vector2, Vector3};

use super::graph::{check_cameras, ViewGraph};
use super::input::Input;
use super::rotation::Rotations;
use super::tracks::Tracks;
use super::triangulate;
use super::Options;
use crate::bundle::lm::{solve_model, BlockJacobian, Model};
use crate::bundle::{LinearSolver, Loss, Options as SolverOptions};
use crate::camera::{Camera, Pose};

/// The floor of the scale of an observation (GLOMAP's lower bound of its scales).
pub const SCALE_FLOOR: f64 = 1e-5;
/// Tracks seen by fewer placed photos are left out (GLOMAP's `min_num_view_per_track`).
pub const MIN_VIEWS: usize = 3;
/// The random start without GNSS: centres and points uniform in `[-RANDOM_EXTENT,
/// RANDOM_EXTENT]` on each axis (GLOMAP's `100 * RandVector3d(-1, 1)`), from the generator seeded
/// with `SEED` (GLOMAP's seed).
pub const RANDOM_EXTENT: f64 = 100.0;
pub const SEED: u64 = 1;
/// The positions orient the frame when twice the square root of the second eigenvalue of the
/// covariance of their horizontal coordinates is at least this many times the median of their
/// recorded horizontal accuracy.
pub const POSITION_SPREAD_SIGMAS: f64 = 10.0;
/// The fewest pairs that orient the frame, and the smallest ratio of the second singular value of
/// the scatter of their directions to the first.
pub const MIN_FRAME_PAIRS: usize = 10;
pub const MIN_SPREAD: f64 = 0.1;
/// The scale of the Huber weights on the angle of a pair when the frame is turned, in degrees.
pub const FRAME_HUBER_DEG: f64 = 5.0;
/// The baselines cannot orient the block when the median angle between the directions of the pairs
/// and of their positions, once the frame is turned, is above this, in degrees.
pub const MAX_FRAME_RESIDUAL_DEG: f64 = 20.0;
/// The scale of the Huber loss on the residuals of the positions, in sigmas.
pub const GNSS_HUBER: f64 = 1.0;
/// A position farther than this many sigmas from its centre after the positioning is an outlier.
pub const OUTLIER_SIGMAS: f64 = 5.0;
/// The weight of the observations of a camera without a prior focal length (GLOMAP's
/// `ScaledLoss` of 0.5 on them).
const UNCALIBRATED_WEIGHT: f64 = 0.5;
/// The iterations of a solve (GLOMAP's `max_num_iterations` of its positioning), and of the solve
/// from the random start (this package's figure: from the random start the solver of milestone 1
/// takes about 230 iterations on a ring of 100 photos, more than GLOMAP's 100).
const MAX_ITERATIONS: usize = 100;
const FREE_MAX_ITERATIONS: usize = 300;
/// The iterations of the reweighted least squares of the frame, and of the similarity with the
/// translations of the flights (solved in turn, so given more).
const IRLS_ITERATIONS: usize = 100;
const PLACE_ITERATIONS: usize = 500;
/// The start at the positions fails its check when more than this share of the positions are
/// outliers after its joint solve, or more than half of the positions of a flight of at least
/// `MIN_CHECKED_FLIGHT` positions.
const START_OUTLIER_SHARE: f64 = 0.1;
const MIN_CHECKED_FLIGHT: usize = 3;
/// The passes of the resection of the photos without a position, and the lines it keeps: within
/// this many times the median distance of the lines to the first centre.
const RESECT_PASSES: usize = 4;
const RESECT_SPREAD: f64 = 3.0;
/// The passes of the frame and the offsets of the flights in turn, and the largest change of an
/// offset, in metres, that ends them.
const FRAME_PASSES: usize = 10;
const FRAME_SETTLED: f64 = 1e-3;

/// The positions of photos: the index of the photo in `Input::images`, its position (metric axes,
/// less an offset), the recorded accuracy of each axis (one sigma, metres) and the flight it
/// belongs to.
#[derive(Clone, Debug, PartialEq)]
pub struct Gnss {
    pub image: Vec<usize>,
    pub xyz: Vec<[f64; 3]>,
    pub sigma: Vec<[f64; 3]>,
    pub flight: Vec<i64>,
}

/// The frame of the result: the frame of the positions turned onto from the baselines (`Gnss`),
/// the block solved without them and put on them by one similarity (`Similarity`), or a frame of
/// its own without positions (`Free`: the averaged rotations' frame, one centre and one scale
/// held).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Frame {
    Gnss,
    Similarity,
    Free,
}

/// The positioned block: per photo of the input its centre (`None` for a photo not placed by the
/// rotations or without observations and position); per selected track its point (`None` for a
/// track left out); the frame, and the rotation that takes the averaged rotations into it (the
/// `world_from_cam` of a photo is `frame_rotation * Rotations::world_from_cam`); the median angle
/// between the directions of the pairs and of the positions once the frame was turned (`Gnss`
/// only); the largest recorded accuracy of the positions used, horizontal and vertical (`None` in a
/// free frame); the bias of each flight, sorted by flight, the first zero (empty with one flight
/// or in a free frame; in the `Similarity` frame the translations of the flights); the photos
/// whose position is an outlier, ascending; the scales on their floor and the cost of the last
/// solve; notes on the start (the fallback to the rays alone, see `position`).
#[derive(Clone, Debug, PartialEq)]
pub struct Positioned {
    pub centres: Vec<Option<[f64; 3]>>,
    pub points: Vec<Option<[f64; 3]>>,
    pub frame: Frame,
    pub frame_rotation: [[f64; 3]; 3],
    pub frame_residual_deg: Option<f64>,
    pub sigma_h: Option<f64>,
    pub sigma_v: Option<f64>,
    pub bias: Vec<(i64, [f64; 3])>,
    pub gnss_outliers: Vec<usize>,
    pub scales_on_floor: usize,
    pub cost: f64,
    pub notes: Vec<String>,
}

/// Knuth's MMIX linear congruential generator.
struct Lcg(u64);

impl Lcg {
    /// A value in [-1, 1) from the upper 53 bits.
    fn symmetric(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        ((self.0 >> 11) as f64 / (1u64 << 53) as f64) * 2.0 - 1.0
    }

    fn vector(&mut self, extent: f64) -> [f64; 3] {
        let x = self.symmetric();
        let y = self.symmetric();
        let z = self.symmetric();
        [extent * x, extent * y, extent * z]
    }
}

fn matrix(r: &[[f64; 3]; 3]) -> Matrix3<f64> {
    Matrix3::from_fn(|i, j| r[i][j])
}

fn rows(r: &Matrix3<f64>) -> [[f64; 3]; 3] {
    std::array::from_fn(|i| std::array::from_fn(|j| r[(i, j)]))
}

/// The `cam_from_world` rows of a photo of `world_from_cam` at `centre`: `R^T`, `-R^T C`.
fn pose_of(world_from_cam: &Matrix3<f64>, centre: &Vector3<f64>) -> Pose {
    let r = world_from_cam.transpose();
    let t = -(r * centre);
    std::array::from_fn(|i| [r[(i, 0)], r[(i, 1)], r[(i, 2)], t[i]])
}

fn median(values: &mut [f64]) -> f64 {
    values.sort_by(|a, b| a.total_cmp(b));
    let n = values.len();
    if n == 0 {
        f64::NAN
    } else if n % 2 == 1 {
        values[n / 2]
    } else {
        0.5 * (values[n / 2 - 1] + values[n / 2])
    }
}

/// The observation of a selected track in the solve: its photo, its point (the index among the
/// points solved), its unit ray in the world, its weight and its scale at the start (`held` for
/// the scale GLOMAP holds).
struct RayIn {
    photo: usize,
    point: usize,
    ray: [f64; 3],
    weight: f64,
    scale: f64,
    held: bool,
}

enum Scale {
    /// The coordinate of the point blocks that holds it (`Scales::Explicit`).
    Free(usize),
    Held(f64),
    /// Eliminated in closed form from its residual (`Scales::Eliminated`).
    Eliminated,
}

/// How the free scales of the observations enter the solve: eliminated in closed form from each
/// residual, so that a point block is its three coordinates (`Eliminated`, what `position` uses),
/// or as coordinates of the point blocks (`Explicit`, GLOMAP's formulation: a point seen `k` times
/// is a dense block of `3 + k`), kept as the reference the elimination is proved against. The
/// two have the same minima: the scale of an observation appears in its residual alone, and the
/// loss is a function of the norm of that residual, smallest at the scale that minimises the norm,
/// `max(floor, v . (X - C) / |X - C|^2)`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scales {
    Eliminated,
    Explicit,
}

/// The scale that minimises `|s d - v|` with `s` at least `SCALE_FLOOR`, and whether it is above
/// the floor (the minimum of the unbounded problem).
fn best_scale(d: &[f64; 3], v: &[f64; 3]) -> (f64, bool) {
    let dd = d[0] * d[0] + d[1] * d[1] + d[2] * d[2];
    let projection = (d[0] * v[0] + d[1] * v[1] + d[2] * v[2]) / dd;
    if dd > 0.0 && projection > SCALE_FLOOR {
        (projection, true)
    } else {
        (SCALE_FLOOR, false)
    }
}

struct Ray {
    photo: usize,
    point: usize,
    ray: [f64; 3],
    weight: f64,
    scale: Scale,
}

/// A position in the solve: its photo, its flight (index among the flights, 0 the first), the
/// position and its sigma per axis.
#[derive(Clone)]
struct Fix {
    photo: usize,
    flight: usize,
    xyz: [f64; 3],
    sigma: [f64; 3],
}

/// The model of step 3 (and of step 5 without positions): camera blocks of 3 (the free centres in
/// the order of the photos, then the biases of the flights after the first, the border), point
/// blocks of 3 + the free scales of the point's observations; the residual blocks the rays, then
/// the positions.
struct PositionModel {
    cam_sizes: Vec<usize>,
    point_sizes: Vec<usize>,
    /// The camera block of the centre of each photo (`None`: held or not in the solve).
    block_of_photo: Vec<Option<usize>>,
    /// The photo of each centre block.
    photo_of_block: Vec<usize>,
    centres: Vec<[f64; 3]>,
    biases: Vec<[f64; 3]>,
    /// The coordinates of the point blocks: each point, then its free scales.
    params: Vec<f64>,
    point_col: Vec<usize>,
    bounds: Vec<(usize, f64)>,
    rays: Vec<Ray>,
    fixes: Vec<Fix>,
    huber: f64,
}

impl PositionModel {
    /// The model of the rays `rays` (sorted by point) on `points`, the centres starting at
    /// `centres` (the photo `held_photo` held), the positions `fixes` with a bias for each flight
    /// after the first of `flights`.
    fn new(
        rays: Vec<RayIn>,
        points: &[[f64; 3]],
        centres: Vec<[f64; 3]>,
        held_photo: Option<usize>,
        fixes: Vec<Fix>,
        flights: usize,
        huber: f64,
        scales: Scales,
    ) -> PositionModel {
        let n = centres.len();
        let mut in_solve = vec![false; n];
        for r in &rays {
            in_solve[r.photo] = true;
        }
        for f in &fixes {
            in_solve[f.photo] = true;
        }
        let mut block_of_photo = vec![None; n];
        let mut photo_of_block = Vec::new();
        for i in 0..n {
            if in_solve[i] && Some(i) != held_photo {
                block_of_photo[i] = Some(photo_of_block.len());
                photo_of_block.push(i);
            }
        }
        let n_bias = flights.saturating_sub(1);
        let cam_sizes = vec![3; photo_of_block.len() + n_bias];
        let mut point_sizes = vec![3; points.len()];
        for r in &rays {
            if !r.held && scales == Scales::Explicit {
                point_sizes[r.point] += 1;
            }
        }
        let mut point_col = Vec::with_capacity(points.len());
        let mut params = Vec::new();
        for (p, x) in points.iter().enumerate() {
            point_col.push(params.len());
            params.extend_from_slice(x);
            params.resize(params.len() + point_sizes[p] - 3, 0.0);
        }
        let mut next: Vec<usize> = point_col.iter().map(|c| c + 3).collect();
        let mut bounds = Vec::new();
        let rays = rays
            .into_iter()
            .map(|r| {
                let scale = if r.held {
                    Scale::Held(r.scale)
                } else if scales == Scales::Eliminated {
                    Scale::Eliminated
                } else {
                    let c = next[r.point];
                    next[r.point] += 1;
                    params[c] = r.scale;
                    bounds.push((c, SCALE_FLOOR));
                    Scale::Free(c)
                };
                Ray { photo: r.photo, point: r.point, ray: r.ray, weight: r.weight, scale }
            })
            .collect();
        PositionModel {
            cam_sizes,
            point_sizes,
            block_of_photo,
            photo_of_block,
            centres,
            biases: vec![[0.0; 3]; n_bias],
            params,
            point_col,
            bounds,
            rays,
            fixes,
            huber,
        }
    }

    fn point(&self, p: usize) -> [f64; 3] {
        let c = self.point_col[p];
        [self.params[c], self.params[c + 1], self.params[c + 2]]
    }

    /// The scale of a ray, and whether it is free above its floor (`None` for one held).
    fn scale(&self, r: &Ray) -> (f64, Option<bool>) {
        match r.scale {
            Scale::Free(c) => (self.params[c], Some(self.params[c] > SCALE_FLOOR)),
            Scale::Held(v) => (v, None),
            Scale::Eliminated => {
                let (x, c) = (self.point(r.point), self.centres[r.photo]);
                let (s, above) = best_scale(&std::array::from_fn(|a| x[a] - c[a]), &r.ray);
                (s, Some(above))
            }
        }
    }

    /// The bias of flight `f` (index among the flights).
    fn bias(&self, f: usize) -> [f64; 3] {
        if f == 0 {
            [0.0; 3]
        } else {
            self.biases[f - 1]
        }
    }

    /// The residual of a position: `C + b - g` per axis, in metres.
    fn fix_residual(&self, f: &Fix) -> [f64; 3] {
        let (c, b) = (self.centres[f.photo], self.bias(f.flight));
        std::array::from_fn(|a| c[a] + b[a] - f.xyz[a])
    }

    fn scales_on_floor(&self) -> usize {
        self.rays.iter().filter(|r| self.scale(r).1 == Some(false)).count()
    }
}

impl Model for PositionModel {
    fn camera_blocks(&self) -> &[usize] {
        &self.cam_sizes
    }

    fn point_blocks(&self) -> &[usize] {
        &self.point_sizes
    }

    fn residual_blocks(&self) -> usize {
        self.rays.len() + self.fixes.len()
    }

    fn linearize(&self, k: usize, out: &mut BlockJacobian) {
        if k < self.rays.len() {
            let r = &self.rays[k];
            let (x, c, w) = (self.point(r.point), self.centres[r.photo], r.weight);
            let d: [f64; 3] = std::array::from_fn(|a| x[a] - c[a]);
            let (s, above) = self.scale(r);
            // the derivative of the residual `s d - v` by `d`: `s I`, and for a scale eliminated
            // above its floor (`s = v . d / |d|^2`) also `d (v - 2 s d)^T / |d|^2`
            let eliminated = matches!(r.scale, Scale::Eliminated) && above == Some(true);
            let dd = d[0] * d[0] + d[1] * d[1] + d[2] * d[2];
            let m = |a: usize, q: usize| {
                let diagonal = if q == a { s } else { 0.0 };
                if eliminated { diagonal + d[a] * (r.ray[q] - 2.0 * s * d[q]) / dd } else { diagonal }
            };
            let block = self.block_of_photo[r.photo];
            if let Some(b) = block {
                out.cameras.push(b);
            }
            out.point = Some(r.point);
            let size = self.point_sizes[r.point];
            let scale_col = match r.scale {
                Scale::Free(col) => Some(col - self.point_col[r.point]),
                Scale::Held(_) | Scale::Eliminated => None,
            };
            for a in 0..3 {
                out.residuals.push(w * (s * d[a] - r.ray[a]));
                if block.is_some() {
                    for q in 0..3 {
                        out.jacobian.push(-w * m(a, q));
                    }
                }
                for q in 0..size {
                    let value = if q < 3 {
                        w * m(a, q)
                    } else if Some(q) == scale_col {
                        w * d[a]
                    } else {
                        0.0
                    };
                    out.jacobian.push(value);
                }
            }
            out.loss = Loss::Huber(self.huber * w);
        } else {
            let f = &self.fixes[k - self.rays.len()];
            let e = self.fix_residual(f);
            let block = self.block_of_photo[f.photo];
            let bias = (f.flight > 0).then(|| self.photo_of_block.len() + f.flight - 1);
            out.cameras.extend(block);
            out.cameras.extend(bias);
            for a in 0..3 {
                out.residuals.push(e[a] / f.sigma[a]);
                for _ in 0..out.cameras.len() {
                    for q in 0..3 {
                        out.jacobian.push(if q == a { 1.0 / f.sigma[a] } else { 0.0 });
                    }
                }
            }
            out.loss = Loss::Huber(GNSS_HUBER);
        }
    }

    fn apply(&mut self, delta_cameras: &[f64], delta_points: &[f64]) {
        for (b, &photo) in self.photo_of_block.iter().enumerate() {
            for a in 0..3 {
                self.centres[photo][a] += delta_cameras[3 * b + a];
            }
        }
        let first = self.photo_of_block.len();
        for (j, bias) in self.biases.iter_mut().enumerate() {
            for a in 0..3 {
                bias[a] += delta_cameras[3 * (first + j) + a];
            }
        }
        for (p, d) in self.params.iter_mut().zip(delta_points) {
            *p += d;
        }
    }

    fn save(&self) -> Vec<f64> {
        let mut out = Vec::with_capacity(3 * self.cam_sizes.len() + self.params.len());
        for &photo in &self.photo_of_block {
            out.extend_from_slice(&self.centres[photo]);
        }
        for b in &self.biases {
            out.extend_from_slice(b);
        }
        out.extend_from_slice(&self.params);
        out
    }

    fn restore(&mut self, saved: &[f64]) {
        let mut at = 0;
        for &photo in &self.photo_of_block {
            self.centres[photo].copy_from_slice(&saved[at..at + 3]);
            at += 3;
        }
        for b in self.biases.iter_mut() {
            b.copy_from_slice(&saved[at..at + 3]);
            at += 3;
        }
        self.params.copy_from_slice(&saved[at..]);
    }

    fn lower_bounds(&self) -> &[(usize, f64)] {
        &self.bounds
    }

    fn point_coordinate(&self, index: usize) -> f64 {
        self.params[index]
    }

    fn border_blocks(&self) -> usize {
        self.biases.len()
    }
}

fn solve(m: &mut PositionModel, max_iterations: usize, observe: &mut dyn FnMut() -> bool) -> Result<f64, String> {
    let opts = SolverOptions {
        max_iterations,
        loss: Loss::None,
        linear_solver: LinearSolver::Auto,
        ..SolverOptions::default()
    };
    let report = solve_model(m, &opts, &mut |_, _| observe())?;
    Ok(report.cost)
}

/// `Err` naming the first thing the positioning cannot use.
#[allow(clippy::too_many_arguments)]
fn check(
    input: &Input,
    cams: &[Camera],
    graph: &ViewGraph,
    rotations: &Rotations,
    tracks: &Tracks,
    selected: &[usize],
    gnss: Option<&Gnss>,
    o: &Options,
) -> Result<(), String> {
    if let Some(reason) = &rotations.refused {
        return Err(format!("the rotations were refused: {reason}"));
    }
    if let Some(reason) = &graph.refused {
        return Err(format!("the view graph was refused: {reason}"));
    }
    if !(o.positioning_huber > 0.0 && o.positioning_huber.is_finite()) {
        return Err(format!("positioning_huber must be a positive finite number, not {}", o.positioning_huber));
    }
    check_cameras(input, cams)?;
    tracks.check(input)?;
    let n = input.images.len();
    if rotations.world_from_cam.len() != n {
        return Err(format!("{} rotations for {n} images", rotations.world_from_cam.len()));
    }
    if let Some((k, e)) = graph.edges.iter().enumerate().find(|(_, e)| e.a >= n || e.b >= n || e.a == e.b) {
        return Err(format!("edge {k}: images {} and {} out of range or the same ({n} images)", e.a, e.b));
    }
    if let Some(w) = selected.windows(2).find(|w| w[0] >= w[1]) {
        return Err(format!("selected tracks not in increasing order: {} then {}", w[0], w[1]));
    }
    if let Some(&t) = selected.iter().find(|&&t| t >= tracks.len()) {
        return Err(format!("selected track {t} out of range ({} tracks)", tracks.len()));
    }
    if let Some(g) = gnss {
        let m = g.image.len();
        if g.xyz.len() != m || g.sigma.len() != m || g.flight.len() != m {
            return Err(format!("GNSS: {m} images, {} positions, {} sigmas, {} flights", g.xyz.len(), g.sigma.len(),
                               g.flight.len()));
        }
        let mut seen = vec![false; n];
        for k in 0..m {
            let i = g.image[k];
            if i >= n {
                return Err(format!("GNSS position {k}: image {i} out of range ({n} images)"));
            }
            if std::mem::replace(&mut seen[i], true) {
                return Err(format!("GNSS position {k}: image {i} given twice"));
            }
            if !g.xyz[k].iter().all(|v| v.is_finite()) {
                return Err(format!("GNSS position {k} (image {i}) is not finite: {:?}", g.xyz[k]));
            }
            if !g.sigma[k].iter().all(|s| *s > 0.0 && s.is_finite()) {
                return Err(format!("GNSS position {k} (image {i}): accuracy {:?} is not positive", g.sigma[k]));
            }
        }
    }
    Ok(())
}

/// The unit ray of observation `(image, k)` in the frame of `world_from_cam`.
fn ray(input: &Input, cams: &[Camera], world_from_cam: &Matrix3<f64>, image: usize, k: u32) -> Vector3<f64> {
    let px = input.keypoint(image, k);
    let (u, v) = cams[input.images[image].camera].normalised(px[0], px[1]);
    (world_from_cam * Vector3::new(u, v, 1.0)).normalize()
}

/// The weight of the observations of a photo (`UNCALIBRATED_WEIGHT` without a prior focal length),
/// as a factor of the residual.
fn weight(input: &Input, image: usize) -> f64 {
    if input.cameras[input.images[image].camera].prior_focal {
        1.0
    } else {
        UNCALIBRATED_WEIGHT.sqrt()
    }
}

/// The rotation `Q` that turns the averaged frame onto the frame of the positions, with the median
/// angle of its pairs in degrees; `None` when the pairs cannot orient the block.
fn turn_onto_positions(
    graph: &ViewGraph,
    rotations: &Rotations,
    fix_of: &[Option<usize>],
    g: &Gnss,
) -> Option<(Matrix3<f64>, f64)> {
    // the positions orient the block when they spread across it, not only along it: twice the
    // square root of the second eigenvalue of the covariance of their horizontal coordinates at
    // least `POSITION_SPREAD_SIGMAS` times the median of their recorded horizontal accuracy
    let used: Vec<usize> = fix_of.iter().flatten().copied().collect();
    if used.len() < 3 {
        return None;
    }
    let m = used.len() as f64;
    let mean = used.iter().map(|&k| Vector2::new(g.xyz[k][0], g.xyz[k][1])).sum::<Vector2<f64>>() / m;
    let cov: Matrix2<f64> = used
        .iter()
        .map(|&k| {
            let d = Vector2::new(g.xyz[k][0], g.xyz[k][1]) - mean;
            d * d.transpose()
        })
        .sum::<Matrix2<f64>>()
        / m;
    let second = cov.symmetric_eigen().eigenvalues.min().max(0.0);
    let mut sigma_h: Vec<f64> = used.iter().map(|&k| g.sigma[k][0].max(g.sigma[k][1])).collect();
    if !(2.0 * second.sqrt() >= POSITION_SPREAD_SIGMAS * median(&mut sigma_h)) {
        return None;
    }
    let mut pairs: Vec<(Vector3<f64>, Vector3<f64>)> = Vec::new();
    for (k, e) in graph.edges.iter().enumerate() {
        if !e.parallax || rotations.dropped.binary_search(&k).is_ok() {
            continue;
        }
        let (Some(Some(fa)), Some(Some(fb))) = (fix_of.get(e.a), fix_of.get(e.b)) else {
            continue;
        };
        let Some(Some(wb)) = rotations.world_from_cam.get(e.b) else {
            continue;
        };
        // X_b = R X_a + t with t = R_b (C_a - C_b): C_b - C_a along -R_b^T t
        let d = -(matrix(wb) * Vector3::from(e.translation));
        let baseline = Vector3::from(g.xyz[*fb]) - Vector3::from(g.xyz[*fa]);
        let length = baseline.norm();
        if !(d.norm() > 0.0 && length > 0.0) {
            continue;
        }
        pairs.push((d.normalize(), baseline / length));
    }
    if pairs.len() < MIN_FRAME_PAIRS {
        return None;
    }
    let scatter: Matrix3<f64> = pairs.iter().map(|(_, u)| u * u.transpose()).sum();
    let mut spread: Vec<f64> = scatter.symmetric_eigen().eigenvalues.iter().copied().collect();
    spread.sort_by(|a, b| b.total_cmp(a));
    if !(spread[1] >= MIN_SPREAD * spread[0]) {
        return None;
    }
    let limit = FRAME_HUBER_DEG.to_radians();
    let mut weights = vec![1.0; pairs.len()];
    let mut q = Matrix3::identity();
    for _ in 0..IRLS_ITERATIONS {
        // Horn: the rotation that maximises sum w u . Q d is V U^T of H = sum w d u^T = U S V^T
        let h: Matrix3<f64> = pairs.iter().zip(&weights).map(|((d, u), w)| *w * d * u.transpose()).sum();
        let svd = h.svd(true, true);
        let (u, v) = (svd.u?, svd.v_t?.transpose());
        let mut fix = Matrix3::identity();
        fix[(2, 2)] = (v * u.transpose()).determinant().signum();
        let next = v * fix * u.transpose();
        let moved = (next - q).abs().max();
        q = next;
        weights = pairs
            .iter()
            .map(|(d, u)| {
                let angle = (q * d).cross(u).norm().atan2((q * d).dot(u));
                if angle <= limit { 1.0 } else { limit / angle }
            })
            .collect();
        if moved < 1e-15 {
            break;
        }
    }
    let mut angles: Vec<f64> =
        pairs.iter().map(|(d, u)| (q * d).cross(u).norm().atan2((q * d).dot(u)).to_degrees()).collect();
    let residual = median(&mut angles);
    (residual <= MAX_FRAME_RESIDUAL_DEG).then_some((q, residual))
}

/// The similarity `x -> s R x + t` that best takes `from` onto `to` with the weights `w`
/// (Umeyama 1991).
fn similarity(from: &[Vector3<f64>], to: &[Vector3<f64>], w: &[f64]) -> Option<(f64, Matrix3<f64>, Vector3<f64>)> {
    let total: f64 = w.iter().sum();
    if !(total > 0.0) {
        return None;
    }
    let mf: Vector3<f64> = from.iter().zip(w).map(|(x, w)| *w * x).sum::<Vector3<f64>>() / total;
    let mt: Vector3<f64> = to.iter().zip(w).map(|(x, w)| *w * x).sum::<Vector3<f64>>() / total;
    let mut cov = Matrix3::zeros();
    let mut var = 0.0;
    for ((f, t), w) in from.iter().zip(to).zip(w) {
        cov += *w * (t - mt) * (f - mf).transpose();
        var += *w * (f - mf).norm_squared();
    }
    if !(var > 0.0) {
        return None;
    }
    let svd = cov.svd(true, true);
    let (u, vt) = (svd.u?, svd.v_t?);
    let mut d = Matrix3::identity();
    d[(2, 2)] = (u * vt).determinant().signum();
    let r = u * d * vt;
    let s = svd.singular_values.component_mul(&d.diagonal()).sum() / var;
    Some((s, r, mt - s * r * mf))
}

/// The photos whose fix disagrees with the fixes of more than half of its neighbours of the same
/// flight in the view graph (two such neighbours with a fix at least): the fix `g_i` of photo `i`
/// against `g_n + (C_i - C_n)` of each neighbour `n`, the displacement `C_i - C_n` that of the
/// rays-only block put on the positions (`block`), whose drift over two neighbours is negligible;
/// a disagreement is beyond `OUTLIER_SIGMAS` of the accuracies of the two fixes combined (the
/// horizontal axes together, or the vertical), or of the robust scale of all the disagreements
/// where the block's own error over an edge is the larger; a neighbour counts against a fix only
/// when it agrees with most of its other neighbours (a part of the block the rays alone got wrong
/// leaves its fixes in). A fix far off (a wrong position) disagrees with all its neighbours; a good fix next to
/// it with that one only. Ascending.
fn local_outliers(graph: &ViewGraph, fix_of: &[Option<usize>], g: &Gnss, block: &Positioned) -> Vec<usize> {
    let n = fix_of.len();
    // per edge of one flight between two photos with a fix: its photos, the disagreement of the
    // two fixes with the block and their combined accuracy
    let mut edges = Vec::new();
    for e in &graph.edges {
        let (a, b) = (e.a, e.b);
        if a >= n || b >= n {
            continue;
        }
        let (Some(ka), Some(kb), Some(ca), Some(cb)) = (fix_of[a], fix_of[b], block.centres[a], block.centres[b])
        else {
            continue;
        };
        // the translation between two flights is not known well enough yet to compare across it
        if g.flight[ka] != g.flight[kb] {
            continue;
        }
        let d: [f64; 3] = std::array::from_fn(|x| g.xyz[ka][x] - g.xyz[kb][x] - (ca[x] - cb[x]));
        edges.push((a, b, d, combined(g, ka, kb)));
    }
    vouched(n, &edges)
}

/// The photos (of `n`) that disagree with most of their neighbours, from the edges `(a, b,
/// disagreement, accuracy)` (metres per axis): a disagreement is beyond `OUTLIER_SIGMAS` of the
/// accuracy (the horizontal axes together, or the vertical), or of the robust scale of all the
/// disagreements where that is the larger; a neighbour counts against a photo only when it agrees
/// with most of its other neighbours; a photo needs two neighbours at least. Ascending.
fn vouched(n: usize, edges: &[(usize, usize, [f64; 3], [f64; 3])]) -> Vec<usize> {
    let edges: Vec<(usize, usize, f64, f64)> = edges
        .iter()
        .map(|&(a, b, d, s)| (a, b, (d[0] / s[0]).hypot(d[1] / s[1]), (d[2] / s[2]).abs()))
        .collect();
    // the block (or the directions) is only locally right: its error over an edge sets the scale
    // of a disagreement when it is larger than the accuracy of the fixes (the robust scales of the
    // horizontal norms, a Rayleigh median, and of the vertical values)
    let mut h: Vec<f64> = edges.iter().map(|e| e.2).collect();
    let mut v: Vec<f64> = edges.iter().map(|e| e.3).collect();
    let (sh, sv) = (median(&mut h) / RAYLEIGH_MEDIAN, MAD_SCALE * median(&mut v));
    let (lh, lv) = (OUTLIER_SIGMAS * sh.max(1.0), OUTLIER_SIGMAS * sv.max(1.0));
    // per photo its neighbours and whether each disagrees with it
    let mut links: Vec<Vec<(usize, bool)>> = vec![Vec::new(); n];
    for &(a, b, eh, ev) in &edges {
        let disagree = eh > lh || ev > lv;
        links[a].push((b, disagree));
        links[b].push((a, disagree));
    }
    // a neighbour vouches against `i` when it disagrees with `i` and agrees with most of its other
    // neighbours: where the rays-only block is itself broken, the neighbours disagree among them
    // too and nothing is left out
    let vouches = |nb: usize, i: usize| {
        let others: Vec<bool> = links[nb].iter().filter(|(m, _)| *m != i).map(|(_, d)| *d).collect();
        !others.is_empty() && 2 * others.iter().filter(|d| **d).count() < others.len()
    };
    (0..n)
        .filter(|&i| {
            let against = links[i].iter().filter(|&&(nb, d)| d && vouches(nb, i)).count();
            links[i].len() >= 2 && 2 * against > links[i].len()
        })
        .collect()
}

/// The median of the norm of a standard normal vector of two components, and the factor of the
/// median absolute value of a normal value to its standard deviation.
const RAYLEIGH_MEDIAN: f64 = 1.177_410_022_515_474_7;
const MAD_SCALE: f64 = 1.482_602_218_505_602;

/// Whether the residual `e` of a position is farther than `OUTLIER_SIGMAS` of its recorded accuracy
/// `sigma`, the horizontal axes together (the norm of their residuals in sigmas) or the vertical.
fn is_outlier(e: &[f64; 3], sigma: &[f64; 3]) -> bool {
    let horizontal = ((e[0] / sigma[0]).powi(2) + (e[1] / sigma[1]).powi(2)).sqrt();
    horizontal > OUTLIER_SIGMAS || (e[2] / sigma[2]).abs() > OUTLIER_SIGMAS
}

/// The positions of the photos of the selected tracks, and the points of those tracks (see the
/// module), from the rotations of `rotations` (the photos with one are the placed ones), the
/// observations of the tracks seen through `cams` (the cameras of `input`, after the calibration
/// of the focal lengths), the translations of the edges of `graph` with parallax (for the frame)
/// and the positions `gnss` when given. `observe` is called between the stages and after every
/// iteration of a solve; `false` stops with `Err("cancelled")`.
///
/// `Err` when the rotations or the view graph were refused (with their reason), when
/// `o.positioning_huber` is not a positive finite number, when the cameras, the tracks, the
/// selection or the positions do not fit the input (a position of an image out of range or given
/// twice, not finite, or with an accuracy that is not positive), or when a solve fails.
#[allow(clippy::too_many_arguments)]
pub fn position(
    input: &Input,
    cams: &[Camera],
    graph: &ViewGraph,
    rotations: &Rotations,
    tracks: &Tracks,
    selected: &[usize],
    gnss: Option<&Gnss>,
    o: &Options,
    observe: &mut dyn FnMut() -> bool,
) -> Result<Positioned, String> {
    position_with(input, cams, graph, rotations, tracks, selected, gnss, o, Scales::Eliminated, observe)
}

/// `position` with the scales of the observations solved as `scales` says (`Scales::Explicit` is
/// the reference formulation the tests compare with).
#[doc(hidden)]
#[allow(clippy::too_many_arguments)]
pub fn position_with(
    input: &Input,
    cams: &[Camera],
    graph: &ViewGraph,
    rotations: &Rotations,
    tracks: &Tracks,
    selected: &[usize],
    gnss: Option<&Gnss>,
    o: &Options,
    scales: Scales,
    observe: &mut dyn FnMut() -> bool,
) -> Result<Positioned, String> {
    check(input, cams, graph, rotations, tracks, selected, gnss, o)?;
    if !observe() {
        return Err("cancelled".into());
    }
    let ctx = Context { input, cams, graph, rotations, tracks, selected, o, scales };
    let n = input.images.len();
    // the position of each placed photo, by its index in `gnss`
    let mut fix_of: Vec<Option<usize>> = vec![None; n];
    if let Some(g) = gnss {
        for (k, &i) in g.image.iter().enumerate() {
            if rotations.world_from_cam[i].is_some() {
                fix_of[i] = Some(k);
            }
        }
    }
    let with_positions = gnss.filter(|_| fix_of.iter().filter(|f| f.is_some()).count() >= 3);
    let Some(g) = with_positions else {
        return free(&ctx, observe);
    };
    let Some(mut from_fixes) = from_positions(&ctx, g, &fix_of, observe)? else {
        // the positions alone cannot orient the block: the rays alone put on them (step 1)
        let mut p = from_rays(&ctx, g, &fix_of, observe)?;
        if p.frame == Frame::Gnss {
            p.notes.push("positioning: the pairs of one flight could not turn the frame; started from the rays \
                          alone".into());
        }
        return Ok(p);
    };
    let Err(why) = start_check(&from_fixes, g, &fix_of) else {
        return Ok(from_fixes);
    };
    if !observe() {
        return Err("cancelled".into());
    }
    // the fallback: the rays alone from the random start, put on the positions (step 1)
    let mut from_block = from_rays(&ctx, g, &fix_of, observe)?;
    if from_block.gnss_outliers.len() < from_fixes.gnss_outliers.len() {
        from_block.notes.push(format!("positioning: the start at the positions failed its check ({why}); started \
                                       from the rays alone"));
        Ok(from_block)
    } else {
        from_fixes.notes.push(format!("positioning: the start at the positions failed its check ({why}); the rays \
                                       alone did no better"));
        Ok(from_fixes)
    }
}

/// What every stage of the positioning reads.
struct Context<'a> {
    input: &'a Input,
    cams: &'a [Camera],
    graph: &'a ViewGraph,
    rotations: &'a Rotations,
    tracks: &'a Tracks,
    selected: &'a [usize],
    o: &'a Options,
    scales: Scales,
}

/// The robust check of the start at the positions, after its joint solve: `Err` naming the
/// failure when more than `START_OUTLIER_SHARE` of the positions are outliers, or when more than
/// half of the positions of a flight of `MIN_CHECKED_FLIGHT` positions or more are (a block that
/// the solve left in a wrong basin, a flight that its bias did not take).
fn start_check(p: &Positioned, g: &Gnss, fix_of: &[Option<usize>]) -> Result<(), String> {
    let total = fix_of.iter().flatten().count();
    let outliers = p.gnss_outliers.len();
    if outliers as f64 > START_OUTLIER_SHARE * total as f64 {
        return Err(format!("{outliers} of {total} positions are outliers"));
    }
    let mut flights: Vec<(i64, usize, usize)> = Vec::new();
    for (i, k) in fix_of.iter().enumerate() {
        let Some(k) = *k else { continue };
        let out = usize::from(p.gnss_outliers.binary_search(&i).is_ok());
        match flights.iter_mut().find(|(id, _, _)| *id == g.flight[k]) {
            Some(f) => {
                f.1 += 1;
                f.2 += out;
            }
            None => flights.push((g.flight[k], 1, out)),
        }
    }
    flights.sort_unstable();
    for (id, count, out) in flights {
        if count >= MIN_CHECKED_FLIGHT && 2 * out > count {
            return Err(format!("{out} of the {count} positions of flight {id} are outliers"));
        }
    }
    Ok(())
}

/// Steps 1 to 4 from the positions: the frame turned onto them, the positions that disagree with
/// their neighbours left out (`local_by_directions`), the offsets of the flights from the pairs
/// across flights (`flight_offsets`) and the frame turned again on the positions less them, every
/// centre starting at its position less its flight's offset (a photo without one placed from its
/// rays, `resect`), and the joint solve. `None` when the positions cannot orient the block.
fn from_positions(
    ctx: &Context,
    g: &Gnss,
    fix_of: &[Option<usize>],
    observe: &mut dyn FnMut() -> bool,
) -> Result<Option<Positioned>, String> {
    // a first frame from the positions as they are (the pairs across two flights carry the
    // difference of their offsets: the Huber weights of the frame bound them), the positions that
    // disagree with their neighbours and the offsets of the flights in it; then the frame again
    // from the positions less those offsets, and the offsets in that frame
    let Some((q, _)) = turn_onto_positions(ctx.graph, ctx.rotations, fix_of, g) else {
        return Ok(None);
    };
    let dirs = directions(ctx.graph, ctx.rotations, &q);
    let local = local_by_directions(&dirs, fix_of, g);
    let mut used = fix_of.to_vec();
    for &i in &local {
        used[i] = None;
    }
    let mut offsets = flight_offsets(&dirs, &used, g);
    let mut frame = None;
    for _ in 0..FRAME_PASSES {
        let Some((q, residual_deg)) = turn_onto_positions(ctx.graph, ctx.rotations, &used, &less(g, &offsets))
        else {
            return Ok(None);
        };
        let next = flight_offsets(&directions(ctx.graph, ctx.rotations, &q), &used, g);
        let moved = next
            .iter()
            .zip(&offsets)
            .map(|(a, b)| (Vector3::from(a.1) - Vector3::from(b.1)).norm())
            .fold(0.0, f64::max);
        offsets = next;
        frame = Some((q, residual_deg));
        if moved < FRAME_SETTLED {
            break;
        }
    }
    let (q, residual_deg) = frame.expect("one pass at least");
    if !observe() {
        return Err("cancelled".into());
    }
    let bias_of = |id: i64| offsets.iter().find(|(f, _)| *f == id).map_or([0.0; 3], |(_, b)| *b);
    let world_from_cam: Vec<Option<Matrix3<f64>>> =
        ctx.rotations.world_from_cam.iter().map(|w| w.as_ref().map(|w| q * matrix(w))).collect();
    let mut start: Vec<Option<Vector3<f64>>> = used
        .iter()
        .map(|k| {
            k.map(|k| {
                let b = bias_of(g.flight[k]);
                Vector3::new(g.xyz[k][0] - b[0], g.xyz[k][1] - b[1], g.xyz[k][2] - b[2])
            })
        })
        .collect();
    resect(ctx, &world_from_cam, &mut start);
    let js = JointStart { q, residual_deg, world_from_cam, start, used, local, bias_of: &bias_of };
    joint(ctx, g, fix_of, js, observe).map(Some)
}

/// Steps 1 to 4 from the rays alone (the fallback): the rays alone from GLOMAP's random start put
/// on the positions by the robust similarity with the translations of the flights, the positions
/// that disagree with their neighbours on it left out (`local_outliers`), the frame turned onto the
/// positions less those translations, the centres starting where the similarity put them, and the
/// joint solve; the block on the positions (`Frame::Similarity`) when they cannot orient it.
fn from_rays(
    ctx: &Context,
    g: &Gnss,
    fix_of: &[Option<usize>],
    observe: &mut dyn FnMut() -> bool,
) -> Result<Positioned, String> {
    let n = fix_of.len();
    let block = free(ctx, observe)?;
    if !observe() {
        return Err("cancelled".into());
    }
    let on_positions = put_on_positions(block, fix_of, g)?;
    if on_positions.frame != Frame::Similarity {
        return Ok(on_positions);
    }
    // no position is left out against the whole rays-only block (ruling R9): it drifts, and a
    // position is far from it wherever the drift is largest, which is where the positions are
    // needed; only a fix that disagrees with the fixes of most of its neighbours in the view graph
    // is left out of the joint solve (`local_outliers`), and the joint solve finds the others
    let local = local_outliers(ctx.graph, fix_of, g, &on_positions);
    let mut used = fix_of.to_vec();
    for &i in &local {
        used[i] = None;
    }
    // the directions between the positions with the translations of their flights taken out
    let bias_of = |id: i64| on_positions.bias.iter().find(|(f, _)| *f == id).map_or([0.0; 3], |(_, b)| *b);
    let corrected = less(g, &on_positions.bias);
    let Some((q, residual_deg)) = turn_onto_positions(ctx.graph, ctx.rotations, &used, &corrected) else {
        return Ok(on_positions);
    };
    if !observe() {
        return Err("cancelled".into());
    }
    let world_from_cam: Vec<Option<Matrix3<f64>>> =
        ctx.rotations.world_from_cam.iter().map(|w| w.as_ref().map(|w| q * matrix(w))).collect();
    // the centres start where the similarity put the rays-only block; a photo it did not place,
    // or placed farther than `OUTLIER_SIGMAS` from its position (the flight's translation taken
    // out), on its position: the rays alone drift and can be broken in places, and from there the
    // joint solve, whose positions pull with a bounded force, would not bring them back (the
    // positions so used have passed `local_outliers`)
    let start: Vec<Option<Vector3<f64>>> = (0..n)
        .map(|i| {
            let fix = used[i].map(|k| {
                let b = bias_of(g.flight[k]);
                (Vector3::new(g.xyz[k][0] - b[0], g.xyz[k][1] - b[1], g.xyz[k][2] - b[2]), g.sigma[k])
            });
            match (on_positions.centres[i].map(Vector3::from), fix) {
                (Some(c), Some((x, sigma))) if is_outlier(&[c.x - x.x, c.y - x.y, c.z - x.z], &sigma) => Some(x),
                (Some(c), _) => Some(c),
                (None, f) => f.map(|(x, _)| x),
            }
        })
        .collect();
    let js = JointStart { q, residual_deg, world_from_cam, start, used, local, bias_of: &bias_of };
    joint(ctx, g, fix_of, js, observe)
}

/// Where the joint solve starts: the frame and its median angle, the turned rotations, the centres,
/// the positions used and those left out before the solve, and the bias of each flight (by its
/// id, measured from any flight: the first flight of the positions used is taken as zero).
struct JointStart<'a> {
    q: Matrix3<f64>,
    residual_deg: f64,
    world_from_cam: Vec<Option<Matrix3<f64>>>,
    start: Vec<Option<Vector3<f64>>>,
    used: Vec<Option<usize>>,
    local: Vec<usize>,
    bias_of: &'a dyn Fn(i64) -> [f64; 3],
}

/// Steps 3 and 4 from `js`: the tracks triangulated from the starts, the joint solve of centres,
/// points and biases with every position used, the outliers after it (and the positions left out
/// before it that the solved block agrees with taken back), and one more solve when anything
/// changed. `all_fixes` is the position of each placed photo.
fn joint(
    ctx: &Context,
    g: &Gnss,
    all_fixes: &[Option<usize>],
    js: JointStart,
    observe: &mut dyn FnMut() -> bool,
) -> Result<Positioned, String> {
    let (input, cams, tracks, selected, o) = (ctx.input, ctx.cams, ctx.tracks, ctx.selected, ctx.o);
    let JointStart { q, residual_deg, world_from_cam, start, used: mut fix_of, local, bias_of } = js;
    let n = fix_of.len();
    // the tracks triangulated from the starts, through the filter (the reprojection left out)
    let mut triangulated: Vec<Option<[f64; 3]>> = vec![None; tracks.len()];
    for &t in selected {
        let rays: Vec<([f64; 3], [f64; 3])> = tracks
            .track(t)
            .iter()
            .filter_map(|&(image, k)| {
                let image = image as usize;
                let (w, c) = (world_from_cam[image].as_ref()?, start[image]?);
                let v = ray(input, cams, w, image, k);
                Some(([c.x, c.y, c.z], [v.x, v.y, v.z]))
            })
            .collect();
        triangulated[t] = triangulate::point(&rays);
    }
    let poses: Vec<Option<Pose>> =
        (0..n).map(|i| Some(pose_of(world_from_cam[i].as_ref()?, start[i].as_ref()?))).collect();
    let loose = Options { max_normalized_reprojection: f64::INFINITY, ..o.clone() };
    let kept = triangulate::filter(tracks, &triangulated, input, cams, &poses, &loose)?;

    let mut rays_in = Vec::new();
    let mut points = Vec::new();
    let mut point_of: Vec<Option<usize>> = vec![None; selected.len()];
    for (slot, &t) in selected.iter().enumerate() {
        let range = tracks.offsets[t]..tracks.offsets[t + 1];
        let Some(x) = triangulated[t] else {
            continue;
        };
        if range.clone().filter(|&i| kept.keep[i]).count() < MIN_VIEWS {
            continue;
        }
        let p = points.len();
        point_of[slot] = Some(p);
        points.push(x);
        let x = Vector3::from(x);
        for i in range.filter(|&i| kept.keep[i]) {
            let (image, k) = tracks.observations[i];
            let image = image as usize;
            let w = world_from_cam[image].as_ref().expect("a kept observation has a pose");
            let c = start[image].expect("a kept observation has a centre");
            let v = ray(input, cams, w, image, k);
            // GLOMAP's scale of a point already placed: the projection of the ray on X - C
            let d = x - c;
            let scale = (v.dot(&d) / d.norm_squared()).max(SCALE_FLOOR);
            rays_in.push(RayIn { photo: image, point: p, ray: [v.x, v.y, v.z], weight: weight(input, image), scale,
                                 held: false });
        }
    }
    let mut flights: Vec<i64> = fix_of.iter().flatten().map(|&k| g.flight[k]).collect();
    flights.sort_unstable();
    flights.dedup();
    let fixes: Vec<Fix> = (0..n)
        .filter_map(|i| {
            let k = fix_of[i]?;
            let flight = flights.binary_search(&g.flight[k]).expect("a known flight");
            Some(Fix { photo: i, flight, xyz: g.xyz[k], sigma: g.sigma[k] })
        })
        .collect();
    let centres: Vec<[f64; 3]> = start.iter().map(|c| c.map_or([0.0; 3], |c| [c.x, c.y, c.z])).collect();
    // the flights share the rays of one component of the rotations (the photos placed are one
    // connected part of the view graph): a flight without rays to the others would let its bias
    // and its centres trade off
    let mut m = PositionModel::new(rays_in, &points, centres, None, fixes, flights.len(), o.positioning_huber,
                                   ctx.scales);
    // the biases are measured from the first flight of all the positions; when its positions were
    // all left out, the first flight here is another one, held at zero
    let reference = flights.first().map_or([0.0; 3], |&id| bias_of(id));
    for (f, &id) in flights.iter().enumerate().skip(1) {
        let b = bias_of(id);
        m.biases[f - 1] = std::array::from_fn(|a| b[a] - reference[a]);
    }
    let mut cost = solve(&mut m, MAX_ITERATIONS, observe)?;
    // the positions farther than `OUTLIER_SIGMAS` from the solved centres and biases are the
    // outliers; a fix left out before the solve that the solved block agrees with comes back (a
    // photo the rays alone placed wrong looks like a wrong fix to its neighbours); the block is
    // solved once more when anything changed
    let mut far: Vec<usize> =
        m.fixes.iter().filter(|f| is_outlier(&m.fix_residual(f), &f.sigma)).map(|f| f.photo).collect();
    far.sort_unstable();
    let mut back = Vec::new();
    for &i in &local {
        let (Some(k), Some(_)) = (all_fixes[i], m.block_of_photo[i]) else { continue };
        let Ok(flight) = flights.binary_search(&g.flight[k]) else { continue };
        let fix = Fix { photo: i, flight, xyz: g.xyz[k], sigma: g.sigma[k] };
        if !is_outlier(&m.fix_residual(&fix), &fix.sigma) {
            back.push(fix);
        }
    }
    let mut gnss_outliers: Vec<usize> =
        local.iter().filter(|i| !back.iter().any(|f| f.photo == **i)).chain(&far).copied().collect();
    gnss_outliers.sort_unstable();
    if !far.is_empty() || !back.is_empty() {
        m.fixes.retain(|f| far.binary_search(&f.photo).is_err());
        for &i in &far {
            fix_of[i] = None;
        }
        // the fixes taken back are not checked again after the second solve: one that the block
        // then leaves far stays in, its pull bounded by the Huber loss here and by the rounds'
        // filter of the positions (`gnss_outliers` are left out of the rounds, the others weighed)
        for f in back {
            fix_of[f.photo] = all_fixes[f.photo];
            m.fixes.push(f);
        }
        if !observe() {
            return Err("cancelled".into());
        }
        cost = solve(&mut m, MAX_ITERATIONS, observe)?;
    }
    let largest = |axes: &[usize]| {
        m.fixes.iter().flat_map(|f| axes.iter().map(move |&a| f.sigma[a])).fold(0.0, f64::max)
    };
    let bias = if flights.len() >= 2 { flights.iter().enumerate().map(|(f, &id)| (id, m.bias(f))).collect() }
               else { Vec::new() };
    let placed: Vec<bool> = (0..n).map(|i| m.block_of_photo[i].is_some()).collect();
    Ok(Positioned {
        centres: (0..n).map(|i| placed[i].then(|| m.centres[i])).collect(),
        points: point_of.iter().map(|p| p.map(|p| m.point(p))).collect(),
        frame: Frame::Gnss,
        frame_rotation: rows(&q),
        frame_residual_deg: Some(residual_deg),
        sigma_h: Some(largest(&[0, 1])),
        sigma_v: Some(largest(&[2])),
        bias,
        gnss_outliers,
        scales_on_floor: m.scales_on_floor(),
        cost,
        notes: Vec::new(),
    })
}

/// Per edge of `graph` with parallax whose two photos have a rotation (the edges the rotation
/// averaging kept): its photos and the unit direction from the centre of the first to the centre
/// of the second in the frame turned by `q`.
fn directions(graph: &ViewGraph, rotations: &Rotations, q: &Matrix3<f64>) -> Vec<(usize, usize, Vector3<f64>)> {
    let n = rotations.world_from_cam.len();
    let mut out = Vec::new();
    for (k, e) in graph.edges.iter().enumerate() {
        if !e.parallax || e.a >= n || e.b >= n || rotations.dropped.binary_search(&k).is_ok() {
            continue;
        }
        let (Some(_), Some(wb)) = (rotations.world_from_cam[e.a], rotations.world_from_cam[e.b]) else {
            continue;
        };
        // X_b = R X_a + t with t = R_b (C_a - C_b): C_b - C_a along -R_b^T t
        let d = q * -(matrix(&wb) * Vector3::from(e.translation));
        if d.norm() > 0.0 && d.iter().all(|v| v.is_finite()) {
            out.push((e.a, e.b, d.normalize()));
        }
    }
    out
}

/// The photos whose fix disagrees with the fixes of most of its neighbours of the same flight (as
/// `local_outliers`), the disagreement over an edge being the part of the difference of the two
/// fixes across the direction of the edge (`directions`): the rays give the direction between two
/// photos, not their distance. A fix wrong along every edge of its photo is not seen here; the
/// joint solve, whose positions pull with a bounded force, and the test after it find it.
fn local_by_directions(dirs: &[(usize, usize, Vector3<f64>)], fix_of: &[Option<usize>], g: &Gnss) -> Vec<usize> {
    let mut edges = Vec::new();
    for &(a, b, u) in dirs {
        let (Some(ka), Some(kb)) = (fix_of[a], fix_of[b]) else { continue };
        if g.flight[ka] != g.flight[kb] {
            continue;
        }
        let diff = Vector3::from(g.xyz[kb]) - Vector3::from(g.xyz[ka]);
        let d = diff - u * u.dot(&diff);
        edges.push((a, b, [d.x, d.y, d.z], combined(g, ka, kb)));
    }
    vouched(fix_of.len(), &edges)
}

/// The positions `g` less the offset of their flight (`offsets` by flight id; zero for a flight
/// not in it).
fn less(g: &Gnss, offsets: &[(i64, [f64; 3])]) -> Gnss {
    let mut out = g.clone();
    for (xyz, &id) in out.xyz.iter_mut().zip(&g.flight) {
        let b = offsets.iter().find(|(f, _)| *f == id).map_or([0.0; 3], |(_, b)| *b);
        for a in 0..3 {
            xyz[a] -= b[a];
        }
    }
    out
}

/// The accuracy of the difference of the positions `ka` and `kb`, per axis.
fn combined(g: &Gnss, ka: usize, kb: usize) -> [f64; 3] {
    std::array::from_fn(|x| g.sigma[ka][x].hypot(g.sigma[kb][x]))
}

/// The offset of each flight of the positions used (by its id, sorted; the first at zero) from the
/// edges between two flights: the centres of an edge are the positions less the offsets of their
/// flights, and their difference lies along the direction of the edge (`directions`); in
/// iteratively reweighted least squares (Huber 1 on the part of that difference across the
/// direction, in the accuracy of the two positions). A flight without such edges, or a component
/// the directions do not hold, stays at zero (a small ridge).
fn flight_offsets(dirs: &[(usize, usize, Vector3<f64>)], fix_of: &[Option<usize>], g: &Gnss) -> Vec<(i64, [f64; 3])> {
    let mut ids: Vec<i64> = fix_of.iter().flatten().map(|&k| g.flight[k]).collect();
    ids.sort_unstable();
    ids.dedup();
    let mut offsets = vec![Vector3::zeros(); ids.len()];
    let unknowns = 3 * ids.len().saturating_sub(1);
    // per edge across two flights: the flights, the projector across its direction, the
    // difference of the positions and the weight of the accuracy of the two
    let mut edges: Vec<(usize, usize, Matrix3<f64>, Vector3<f64>, f64)> = Vec::new();
    for &(a, b, u) in dirs {
        let (Some(ka), Some(kb)) = (fix_of[a], fix_of[b]) else { continue };
        if g.flight[ka] == g.flight[kb] {
            continue;
        }
        let fa = ids.binary_search(&g.flight[ka]).expect("a known flight");
        let fb = ids.binary_search(&g.flight[kb]).expect("a known flight");
        let s = combined(g, ka, kb);
        let base = 3.0 / (s[0] * s[0] + s[1] * s[1] + s[2] * s[2]);
        edges.push((fa, fb, Matrix3::identity() - u * u.transpose(),
                    Vector3::from(g.xyz[kb]) - Vector3::from(g.xyz[ka]), base));
    }
    if unknowns > 0 && !edges.is_empty() {
        let mut weights: Vec<f64> = edges.iter().map(|e| e.4).collect();
        for _ in 0..IRLS_ITERATIONS {
            let mut a = DMatrix::<f64>::zeros(unknowns, unknowns);
            let mut rhs = DVector::<f64>::zeros(unknowns);
            for (&(fa, fb, p, y, _), w) in edges.iter().zip(&weights) {
                // the residual P (y - o_b + o_a), the first flight's offset held at zero
                let terms = [(fb, -1.0), (fa, 1.0)];
                for &(f, sf) in &terms {
                    if f == 0 {
                        continue;
                    }
                    let rf = 3 * (f - 1);
                    for &(h, sh) in &terms {
                        if h == 0 {
                            continue;
                        }
                        let rh = 3 * (h - 1);
                        let block = *w * sf * sh * p;
                        let mut view = a.view_mut((rf, rh), (3, 3));
                        view += block;
                    }
                    let mut view = rhs.rows_mut(rf, 3);
                    view -= *w * sf * (p * y);
                }
            }
            let ridge = 1e-9 * a.diagonal().max().max(f64::MIN_POSITIVE);
            for d in 0..unknowns {
                a[(d, d)] += ridge;
            }
            let Some(x) = a.cholesky().map(|c| c.solve(&rhs)) else { break };
            let next: Vec<Vector3<f64>> = (0..ids.len())
                .map(|f| match f {
                    0 => Vector3::zeros(),
                    _ => Vector3::new(x[3 * f - 3], x[3 * f - 2], x[3 * f - 1]),
                })
                .collect();
            let moved = next.iter().zip(&offsets).map(|(a, b)| (a - b).norm()).fold(0.0, f64::max);
            offsets = next;
            weights = edges
                .iter()
                .map(|&(fa, fb, p, y, base)| {
                    let norm = (p * (y - offsets[fb] + offsets[fa])).norm() * base.sqrt();
                    base * if norm <= GNSS_HUBER { 1.0 } else { GNSS_HUBER / norm }
                })
                .collect();
            if moved < 1e-9 {
                break;
            }
        }
    }
    ids.iter().zip(&offsets).map(|(&id, o)| (id, [o.x, o.y, o.z])).collect()
}

/// The placed photos without a start (no position used) started from their rays: the centre
/// nearest the lines from the points of their tracks (triangulated from the photos already
/// started) back along their rays, solved again without the lines farther than `RESECT_SPREAD`
/// times the median distance (wrong matches); in passes, as photos started this way start the
/// points of others. A photo still without a start that has observations starts at the mean of
/// the starts of its neighbours in the view graph.
fn resect(ctx: &Context, world_from_cam: &[Option<Matrix3<f64>>], start: &mut [Option<Vector3<f64>>]) {
    let (input, cams, tracks) = (ctx.input, ctx.cams, ctx.tracks);
    let n = start.len();
    let wanted = |start: &[Option<Vector3<f64>>], i: usize| world_from_cam[i].is_some() && start[i].is_none();
    for _ in 0..RESECT_PASSES {
        if !(0..n).any(|i| wanted(start, i)) {
            return;
        }
        let mut lines: Vec<Vec<([f64; 3], [f64; 3])>> = vec![Vec::new(); n];
        for &t in ctx.selected {
            let track = tracks.track(t);
            if !track.iter().any(|&(image, _)| wanted(start, image as usize)) {
                continue;
            }
            let rays: Vec<([f64; 3], [f64; 3])> = track
                .iter()
                .filter_map(|&(image, k)| {
                    let image = image as usize;
                    let (w, c) = (world_from_cam[image].as_ref()?, start[image]?);
                    let v = ray(input, cams, w, image, k);
                    Some(([c.x, c.y, c.z], [v.x, v.y, v.z]))
                })
                .collect();
            let Some(x) = triangulate::point(&rays) else { continue };
            for &(image, k) in track {
                let image = image as usize;
                if wanted(start, image) {
                    let v = ray(input, cams, world_from_cam[image].as_ref().expect("wanted"), image, k);
                    lines[image].push((x, [-v.x, -v.y, -v.z]));
                }
            }
        }
        let mut moved = false;
        for i in 0..n {
            let Some(c) = triangulate::point(&lines[i]) else { continue };
            let distance = |l: &([f64; 3], [f64; 3])| {
                let (x, v) = (Vector3::from(l.0), Vector3::from(l.1));
                let d = Vector3::from(c) - x;
                (d - v * v.dot(&d)).norm()
            };
            let mut spread: Vec<f64> = lines[i].iter().map(distance).collect();
            let limit = RESECT_SPREAD * median(&mut spread);
            let near: Vec<([f64; 3], [f64; 3])> = lines[i].iter().filter(|l| distance(l) <= limit).copied().collect();
            let c = triangulate::point(&near).unwrap_or(c);
            start[i] = Some(Vector3::from(c));
            moved = true;
        }
        if !moved {
            break;
        }
    }
    let mut seen = vec![false; n];
    for &t in ctx.selected {
        for &(image, _) in tracks.track(t) {
            seen[image as usize] = true;
        }
    }
    let fallback: Vec<Option<Vector3<f64>>> = (0..n)
        .map(|i| {
            if !(wanted(start, i) && seen[i]) {
                return None;
            }
            let near: Vec<Vector3<f64>> = ctx
                .graph
                .edges
                .iter()
                .filter_map(|e| if e.a == i { Some(e.b) } else if e.b == i { Some(e.a) } else { None })
                .filter_map(|j| start.get(j).copied().flatten())
                .collect();
            (!near.is_empty()).then(|| near.iter().sum::<Vector3<f64>>() / near.len() as f64)
        })
        .collect();
    for (s, f) in start.iter_mut().zip(fallback) {
        if f.is_some() {
            *s = f;
        }
    }
}

/// Step 5: the positioning without positions, from GLOMAP's random start.
fn free(ctx: &Context, observe: &mut dyn FnMut() -> bool) -> Result<Positioned, String> {
    let (input, cams, rotations, tracks, selected, o) =
        (ctx.input, ctx.cams, ctx.rotations, ctx.tracks, ctx.selected, ctx.o);
    let n = input.images.len();
    let mut rays_in = Vec::new();
    let mut point_of: Vec<Option<usize>> = vec![None; selected.len()];
    let mut count = 0;
    for (slot, &t) in selected.iter().enumerate() {
        let seen: Vec<(usize, u32)> = tracks
            .track(t)
            .iter()
            .map(|&(image, k)| (image as usize, k))
            .filter(|&(image, _)| rotations.world_from_cam[image].is_some())
            .collect();
        if seen.len() < MIN_VIEWS {
            continue;
        }
        point_of[slot] = Some(count);
        for (image, k) in seen {
            let w = matrix(rotations.world_from_cam[image].as_ref().expect("a placed photo"));
            let v = ray(input, cams, &w, image, k);
            let held = rays_in.is_empty();
            rays_in.push(RayIn { photo: image, point: count, ray: [v.x, v.y, v.z], weight: weight(input, image),
                                 scale: 1.0, held });
        }
        count += 1;
    }
    let mut seen_photo = vec![false; n];
    for r in &rays_in {
        seen_photo[r.photo] = true;
    }
    // GLOMAP's order: the centres, then the points
    let mut random = Lcg(SEED);
    let centres: Vec<[f64; 3]> =
        (0..n).map(|i| if seen_photo[i] { random.vector(RANDOM_EXTENT) } else { [0.0; 3] }).collect();
    let points: Vec<[f64; 3]> = (0..count).map(|_| random.vector(RANDOM_EXTENT)).collect();
    let held = seen_photo.iter().position(|&s| s);
    // GLOMAP's formulation (`Scales::Explicit`): from the random start the scales eliminated stop
    // short (a point behind its camera, its scale on the floor, hardly moves)
    let mut m =
        PositionModel::new(rays_in, &points, centres, held, Vec::new(), 0, o.positioning_huber, Scales::Explicit);
    let cost = if m.rays.is_empty() { 0.0 } else { solve(&mut m, FREE_MAX_ITERATIONS, observe)? };
    Ok(Positioned {
        centres: (0..n).map(|i| seen_photo[i].then(|| m.centres[i])).collect(),
        points: point_of.iter().map(|p| p.map(|p| m.point(p))).collect(),
        frame: Frame::Free,
        frame_rotation: rows(&Matrix3::identity()),
        frame_residual_deg: None,
        sigma_h: None,
        sigma_v: None,
        bias: Vec::new(),
        gnss_outliers: Vec::new(),
        scales_on_floor: m.scales_on_floor(),
        cost,
        notes: Vec::new(),
    })
}

/// A block of the free frame put on the positions by one similarity and, with two flights or
/// more, a translation per flight (the first held at zero: the biases of the flights, which must
/// be known before a position can be judged), robust: iteratively reweighted least squares with
/// Huber 1 on the residual in recorded sigmas, from the weighted least squares of all positions;
/// the similarity and the translations are solved in turn within each iteration. The positions
/// farther than `OUTLIER_SIGMAS` of their recorded accuracy from their placed centre are
/// reported as outliers. A flight with a single position takes that position in its translation:
/// such a position can never be judged an outlier.
///
/// The Huber weights bound the pull of a wrong position, but the start from all the positions
/// has no high breakdown point: many wrong positions that agree with one another (a whole flight
/// with a wrong datum, beyond its bias) can draw the similarity before the weights catch them.
fn put_on_positions(block: Positioned, fix_of: &[Option<usize>], g: &Gnss) -> Result<Positioned, String> {
    let photos: Vec<usize> = (0..fix_of.len()).filter(|&i| fix_of[i].is_some() && block.centres[i].is_some()).collect();
    if photos.len() < 3 {
        return Ok(block);
    }
    let from: Vec<Vector3<f64>> = photos.iter().map(|&i| Vector3::from(block.centres[i].expect("placed"))).collect();
    let to: Vec<Vector3<f64>> = photos.iter().map(|&i| Vector3::from(g.xyz[fix_of[i].expect("fix")])).collect();
    let sigma: Vec<[f64; 3]> = photos.iter().map(|&i| g.sigma[fix_of[i].expect("fix")]).collect();
    let mut flights: Vec<i64> = photos.iter().map(|&i| g.flight[fix_of[i].expect("fix")]).collect();
    flights.sort_unstable();
    flights.dedup();
    let flight: Vec<usize> = photos
        .iter()
        .map(|&i| flights.binary_search(&g.flight[fix_of[i].expect("fix")]).expect("a known flight"))
        .collect();
    let base: Vec<f64> = sigma.iter().map(|s| 3.0 / (s[0] * s[0] + s[1] * s[1] + s[2] * s[2])).collect();
    let mut w = base.clone();
    let mut bias = vec![Vector3::zeros(); flights.len()];
    let unplaced = "the positions cannot place the block by a similarity";
    let mut fit = similarity(&from, &to, &w).ok_or(unplaced)?;
    for _ in 0..PLACE_ITERATIONS {
        // the translations of the flights after the first on the similarity, then the similarity
        // on the positions less their translation
        let (s, r, t) = fit;
        let mut next_bias = bias.clone();
        for (f, b) in next_bias.iter_mut().enumerate().skip(1) {
            let (mut sum, mut total) = (Vector3::zeros(), 0.0);
            for k in (0..photos.len()).filter(|&k| flight[k] == f) {
                sum += w[k] * (to[k] - (s * r * from[k] + t));
                total += w[k];
            }
            if total > 0.0 {
                *b = sum / total;
            }
        }
        let shifted: Vec<Vector3<f64>> = (0..photos.len()).map(|k| to[k] - next_bias[flight[k]]).collect();
        let next_w: Vec<f64> = (0..photos.len())
            .map(|k| {
                let e = s * r * from[k] + t - shifted[k];
                let norm = (0..3).map(|a| (e[a] / sigma[k][a]).powi(2)).sum::<f64>().sqrt();
                base[k] * if norm <= GNSS_HUBER { 1.0 } else { GNSS_HUBER / norm }
            })
            .collect();
        let settled = next_w.iter().zip(&w).all(|(a, b)| (a - b).abs() <= 1e-12 * b.abs())
            && next_bias.iter().zip(&bias).all(|(a, b)| (a - b).norm() <= 1e-12 * (1.0 + b.norm()));
        w = next_w;
        bias = next_bias;
        fit = similarity(&from, &shifted, &w).ok_or(unplaced)?;
        if settled {
            break;
        }
    }
    let (s, r, t) = fit;
    // the translations of the final similarity (the same unless the rounds ran out)
    for (f, b) in bias.iter_mut().enumerate().skip(1) {
        let (mut sum, mut total) = (Vector3::zeros(), 0.0);
        for k in (0..photos.len()).filter(|&k| flight[k] == f) {
            sum += w[k] * (to[k] - (s * r * from[k] + t));
            total += w[k];
        }
        if total > 0.0 {
            *b = sum / total;
        }
    }
    let map = |x: [f64; 3]| {
        let y = s * r * Vector3::from(x) + t;
        [y.x, y.y, y.z]
    };
    let residuals: Vec<[f64; 3]> = (0..photos.len())
        .map(|k| {
            let e = s * r * from[k] + t + bias[flight[k]] - to[k];
            [e.x, e.y, e.z]
        })
        .collect();
    let gnss_outliers = photos
        .iter()
        .zip(&residuals)
        .zip(&sigma)
        .filter(|((_, e), recorded)| is_outlier(e, recorded))
        .map(|((&i, _), _)| i)
        .collect();
    let largest = |axes: &[usize]| sigma.iter().flat_map(|s| axes.iter().map(move |&a| s[a])).fold(0.0, f64::max);
    Ok(Positioned {
        centres: block.centres.iter().map(|c| c.map(map)).collect(),
        points: block.points.iter().map(|p| p.map(map)).collect(),
        frame: Frame::Similarity,
        frame_rotation: rows(&r),
        frame_residual_deg: None,
        sigma_h: Some(largest(&[0, 1])),
        sigma_v: Some(largest(&[2])),
        bias: if flights.len() >= 2 {
            flights.iter().zip(&bias).map(|(&id, b)| (id, [b.x, b.y, b.z])).collect()
        } else {
            Vec::new()
        },
        gnss_outliers,
        scales_on_floor: block.scales_on_floor,
        cost: block.cost,
        notes: block.notes,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::camera::transform;
    use nalgebra::{Rotation3, Unit};

    /// `pose_of` gives `cam_from_world` from `world_from_cam`: a point maps to `R^T (X - C)`, with
    /// a rotation that is not symmetric (a transposed rotation fails).
    #[test]
    fn the_pose_is_cam_from_world() {
        let w = Rotation3::from_axis_angle(&Unit::new_normalize(Vector3::new(1.0, 2.0, 3.0)), 0.7).into_inner();
        let (c, x) = (Vector3::new(3.0, -2.0, 5.0), Vector3::new(1.0, 4.0, -2.0));
        let want = w.transpose() * (x - c);
        let (a, b, d) = transform(&pose_of(&w, &c), x.x, x.y, x.z);
        assert!((Vector3::new(a, b, d) - want).norm() < 1e-12, "{:?} vs {want:?}", (a, b, d));
    }
}
