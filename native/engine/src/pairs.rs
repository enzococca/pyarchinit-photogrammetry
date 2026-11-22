//! The matching of the selected pairs of photos in two stages, and its run over the queue of
//! `sched`. For a pair the first stage matches the `stage1_points` points of the largest scale
//! of each photo (COLMAP's three tests on their bests, from a GPU or the CPU twin) and verifies
//! them (`geometry::two_view`). Trusted - `trust_inliers` inlier matches or more - the second
//! stage matches every point of the first photo against the points of the second that lie within
//! `max_error` of its epipolar line under the model of the first stage, in the frame of that
//! model: the essential matrix of a calibrated pair on the key points undistorted once to ideal
//! pixels, the fundamental matrix on the raw key points it was estimated on (the points of the
//! second photo in a grid of cells in either frame), with the ratio and the distance test among
//! those candidates and the mutual check from the second photo; the matches are verified again
//! and the pair is `Route::Guided`. The ratio test of that stage (`guided_max_ratio`) and the width
//! of its band (`guided_band`) are options of their own, and `guided` false sends the trusted pairs
//! to brute force instead. Not trusted, a pair that still has
//! `drop_inliers` inliers, or that the preselection chose, is matched point against point
//! (`Route::BruteForce`); any other pair is `Route::Dropped` with nothing matched. In every route
//! the matches that go to the verification are capped to `max_num_matches`, the smallest
//! distances kept.
//!
//! `match_all` does every pair of a list through the queue: the GPUs given (one worker each) and
//! the CPU threads share it, the pairs sorted by name so that a device keeps the descriptors of
//! the photo of the current pairs (`Matcher` caches them by key: `photo_key` of the name for the
//! full set, `stage1_key` for the subset of the first stage); every result goes into the match
//! store as it is made. The second stage runs on the CPU of whichever worker did the first, so a
//! pair's result is the same whichever device did it: the kernels are exact and the RANSAC is
//! seeded by the pair.

use std::collections::{HashMap, HashSet};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Mutex, PoisonError};
use std::thread;

use nalgebra::{Matrix3, Vector3};

use crate::camera::Camera;
use crate::geometry::two_view::{self, pair_seed, verify, Config, TwoView};
use crate::gpu::{match_quality, select_matches, Matcher, DIM};
use crate::matching::bests_cpu;
use crate::sched::{self, Worker};
use crate::store::{FeatureStore, FeatureView, MatchStore};
pub use crate::store::{PairResult, Route};

/// The options of the matching of a pair: the size of the first stage, the thresholds of the
/// routes (the program passes them from `native_match.py`), COLMAP's three tests, the cap on the
/// matches, and the verification.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Options {
    /// The points of the largest scale of each photo the first stage matches; a photo with fewer
    /// points goes whole. The first stage ignores `max_num_matches` (it gives at most this many
    /// matches). Zero is no first stage: nothing is asked of the device for it, a preselected pair
    /// goes straight to brute force and any other pair is dropped.
    pub stage1_points: usize,
    /// The inlier matches of the first stage from which it is trusted and the second stage
    /// follows the epipolar lines.
    pub trust_inliers: usize,
    /// Below `trust_inliers`, the inlier matches of the first stage from which a pair that was
    /// not preselected still goes to brute force; with fewer it is dropped.
    pub drop_inliers: usize,
    pub max_ratio: f32,
    pub max_distance: f32,
    pub cross_check: bool,
    /// The most matches kept for the verification, those of the smallest distances (COLMAP's
    /// `max_num_matches`); `usize::MAX` keeps every match.
    pub max_num_matches: usize,
    pub two_view: two_view::Options,
    /// The second stage along the epipolar lines; false sends every trusted pair to brute force
    /// instead (the first stage still decides which pairs are trusted, kept or dropped).
    pub guided: bool,
    /// The ratio test of the second stage, in place of `max_ratio`: a point is compared there only
    /// with the candidates of its band, so a ratio as loose as the first stage's lets through the
    /// ambiguous matches along the line. `max_ratio` keeps the behaviour of the first stage.
    pub guided_max_ratio: f32,
    /// The half-width of the band of the second stage, as a multiple of the verification's
    /// `max_error` (1.0: the band is the threshold of an inlier).
    pub guided_band: f64,
}

/// What the matching of a list of pairs gave: how many pairs were done, how many took each
/// route (guided, brute force, dropped), how many pairs each device did the first stage of (the
/// GPUs by their worker name, then `"CPU"`), and the log (GPUs not opened, workers retired).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct PairsReport {
    pub done: usize,
    pub by_route: (usize, usize, usize),
    pub stage1_by_device: Vec<(String, usize)>,
    pub log: Vec<String>,
}

/// The bests of two sets of descriptors (n x 128 bytes each) as [`Matcher::bests`] gives them,
/// from whatever device: `(best, index, second)` for every descriptor of A over B, then of B
/// over A. The keys name the sets for a cache on the device ([`photo_key`], [`stage1_key`]); the
/// CPU twin ignores them.
pub type Bests<'a> = dyn FnMut(u64, &[u8], u64, &[u8]) -> Result<Vec<[u32; 3]>, String> + 'a;

/// A device that does the first stage (and brute force) of the pairs of a worker: a GPU
/// ([`Matcher`]), or a fake in a test. An error retires the worker; its pair goes back to the
/// queue for the others.
pub trait Device: Send {
    /// What the log and the report call the device.
    fn name(&self) -> String;
    /// As [`Bests`].
    fn bests(&mut self, key_a: u64, a: &[u8], key_b: u64, b: &[u8]) -> Result<Vec<[u32; 3]>, String>;
}

impl Device for Matcher {
    fn name(&self) -> String {
        format!("GPU {} ({})", self.info.index, self.info.name)
    }

    fn bests(&mut self, key_a: u64, a: &[u8], key_b: u64, b: &[u8]) -> Result<Vec<[u32; 3]>, String> {
        Matcher::bests(self, key_a, a, key_b, b)
    }
}

/// The photos a worker keeps prepared: the pairs come sorted by name, so one photo of a pair is
/// one of the pair before, and the next ones on the other side repeat soon after.
const CACHED_PHOTOS: usize = 4;

/// FNV-1a 64 of `bytes`.
fn fnv1a(bytes: impl IntoIterator<Item = u8>) -> u64 {
    let mut hash: u64 = 0xcbf29ce484222325;
    for byte in bytes {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    hash
}

/// The key of the descriptors of the photo `name` (every point) on a device: FNV-1a 64 of the
/// name.
pub fn photo_key(name: &str) -> u64 {
    fnv1a(name.bytes())
}

/// The key of the descriptors of the first stage of the photo `name` (its points of the largest
/// scale): FNV-1a 64 of the name followed by a NUL byte and `stage1`.
pub fn stage1_key(name: &str) -> u64 {
    fnv1a(name.bytes().chain(b"\0stage1".iter().copied()))
}

/// The integer dot product of two descriptors.
#[inline]
fn dot(x: &[u8; DIM], y: &[u8; DIM]) -> u32 {
    x.iter().zip(y).map(|(&p, &q)| u32::from(p) * u32::from(q)).sum()
}

fn descriptor(set: &[u8], i: usize) -> &[u8; DIM] {
    set[i * DIM..(i + 1) * DIM].try_into().expect("a descriptor of DIM bytes")
}

/// The side of a cell of the grid of the second stage, in ideal pixels: four times the band's
/// half-width, at least eight pixels (16 px with COLMAP's 4 px). A line crosses a few hundred
/// cells of a large photo and the points tested exactly are those of a band a few cells wide.
fn cell_side(max_error: f64) -> f64 {
    (4.0 * max_error).max(8.0)
}

/// The points of a photo in a grid of square cells over their bounding box, so that the points
/// along a line are found by the cells the band around it crosses. The points are listed twice,
/// cell by cell along the rows of cells and along the columns, so that the cells the band crosses
/// in one column (or one row) of cells are one run of the list.
struct Grid {
    x0: f64,
    y0: f64,
    cell: f64,
    cols: usize,
    rows: usize,
    /// The first entry of each cell in `items`, the cells row after row (`row * cols + col`), and
    /// one past the last cell.
    starts: Vec<u32>,
    items: Vec<u32>,
    /// The same with the cells column after column (`col * rows + row`).
    starts_by_column: Vec<u32>,
    items_by_column: Vec<u32>,
}

impl Grid {
    /// The grid of `points` with cells of `cell` pixels, widened when the box would take more
    /// than a few million cells (a point far away from the others); a point that is not finite
    /// is in no cell.
    fn new(points: &[[f64; 2]], cell: f64) -> Grid {
        let finite: Vec<usize> = (0..points.len()).filter(|&i| points[i].iter().all(|v| v.is_finite())).collect();
        if finite.is_empty() {
            return Grid {
                x0: 0.0,
                y0: 0.0,
                cell,
                cols: 0,
                rows: 0,
                starts: vec![0],
                items: Vec::new(),
                starts_by_column: vec![0],
                items_by_column: Vec::new(),
            };
        }
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for &i in &finite {
            let [x, y] = points[i];
            x0 = x0.min(x);
            y0 = y0.min(y);
            x1 = x1.max(x);
            y1 = y1.max(y);
        }
        const MOST_CELLS: f64 = 4.0e6;
        let cell = cell.max(((x1 - x0) * (y1 - y0) / MOST_CELLS).sqrt());
        let cols = ((x1 - x0) / cell).floor() as usize + 1;
        let rows = ((y1 - y0) / cell).floor() as usize + 1;
        let place = |p: [f64; 2]| {
            let col = (((p[0] - x0) / cell).floor() as usize).min(cols - 1);
            let row = (((p[1] - y0) / cell).floor() as usize).min(rows - 1);
            (row, col)
        };
        // a counting sort of the points by cell, the cells numbered by `index`
        let sorted = |index: &dyn Fn(usize, usize) -> usize| {
            let mut starts = vec![0u32; cols * rows + 1];
            for &i in &finite {
                let (row, col) = place(points[i]);
                starts[index(row, col) + 1] += 1;
            }
            for k in 0..cols * rows {
                starts[k + 1] += starts[k];
            }
            let mut next = starts.clone();
            let mut items = vec![0u32; finite.len()];
            for &i in &finite {
                let (row, col) = place(points[i]);
                let k = index(row, col);
                items[next[k] as usize] = i as u32;
                next[k] += 1;
            }
            (starts, items)
        };
        let (starts, items) = sorted(&|row, col| row * cols + col);
        let (starts_by_column, items_by_column) = sorted(&|row, col| col * rows + row);
        Grid { x0, y0, cell, cols, rows, starts, items, starts_by_column, items_by_column }
    }

    /// Every point within `e` of the line `l` (`a x + b y + c = 0` with `a^2 + b^2 = 1`), each
    /// once: `visit` gets its index. The line is walked along the axis it is nearer to, over the
    /// columns (or rows) of cells where its band meets the box of the grid (`walk`); in each, the
    /// cells the band crosses are one run of the list sorted that way, whose points are tested
    /// exactly.
    fn along(&self, l: [f64; 3], e: f64, points: &[[f64; 2]], mut visit: impl FnMut(u32)) {
        if self.items.is_empty() {
            return;
        }
        let [a, b, c] = l;
        let mut scan = |run: &[u32]| {
            for &i in run {
                let p = points[i as usize];
                if (a * p[0] + b * p[1] + c).abs() <= e {
                    visit(i);
                }
            }
        };
        let inverse = 1.0 / self.cell;
        if a.abs() <= b.abs() {
            // nearer to horizontal: y = -(a x + c) / b, the rows the band crosses in each column
            let (slope, offset) = (-a / b, -c / b);
            let q = (slope * self.x0 + offset - self.y0) * inverse;
            let slack = e / b.abs() * inverse + CELL_MARGIN;
            let (starts, items) = (&self.starts_by_column, &self.items_by_column);
            walk(slope, q, slack, self.cols, self.rows, |col, first, last| {
                let base = col * self.rows;
                scan(&items[starts[base + first] as usize..starts[base + last + 1] as usize]);
            });
        } else {
            let (slope, offset) = (-b / a, -c / a);
            let q = (slope * self.y0 + offset - self.x0) * inverse;
            let slack = e / a.abs() * inverse + CELL_MARGIN;
            walk(slope, q, slack, self.rows, self.cols, |row, first, last| {
                let base = row * self.cols;
                scan(&self.items[self.starts[base + first] as usize..self.starts[base + last + 1] as usize]);
            });
        }
    }
}

/// The margin, in cells, added to the half-width of a band when its cells are chosen: the cells
/// it adds are tested point by point like the others, and it keeps a point at the very edge of
/// the band from being lost to the roundoff of the walk.
const CELL_MARGIN: f64 = 1e-9;

/// The walk of a band over a grid, in units of cells: along the major axis (`major` cells), the
/// line is `v = slope u + q` on the minor axis (`minor` cells), and its band `slack` wide on
/// either side. The cells of the major axis where the band meets `[0, minor]` (the line clipped
/// to the box, one cell of margin on either side) are visited in order: `run(j, first, last)` for
/// the cells `first..=last` of the minor axis the band crosses in the cell `j` of the major one.
fn walk(slope: f64, q: f64, slack: f64, major: usize, minor: usize, mut run: impl FnMut(usize, usize, usize)) {
    let height = minor as f64;
    // a slope this small is a line along the major axis: dividing by it would overflow
    let (first, last) = if slope.abs() < 1e-12 {
        if q + slack < 0.0 || q - slack >= height {
            return;
        }
        (0, major - 1)
    } else {
        let (u, w) = ((-slack - q) / slope, (height + slack - q) / slope);
        let (low, high) = (u.min(w).floor() - 1.0, u.max(w).floor() + 1.0);
        if !(low.is_finite() && high.is_finite()) || high < 0.0 || low >= major as f64 {
            return;
        }
        (low.max(0.0) as usize, (high as usize).min(major - 1))
    };
    let mut v0 = slope * first as f64 + q;
    for j in first..=last {
        let v1 = slope * (j + 1) as f64 + q;
        let (low, high) = (v0.min(v1) - slack, v0.max(v1) + slack);
        v0 = v1;
        if high < 0.0 || low >= height {
            continue;
        }
        run(j, low.max(0.0).floor() as usize, high.min(height - 1.0).floor() as usize);
    }
}

/// A photo prepared for its pairs: the points of the first stage, their descriptors gathered
/// when they are a subset, every key point undistorted once to ideal pixels, and the grid of
/// those.
struct Prepared {
    /// The indices of the points of the first stage, ascending: the `stage1_points` of the
    /// largest scale (the lowest index first among equal scales), or every point.
    stage1: Vec<u32>,
    /// Their descriptors, in that order; empty when every point is in the first stage, whose
    /// set is then the photo's own.
    subset: Vec<u8>,
    all_in_stage1: bool,
    /// The key points as they are, the frame of a fundamental matrix, and their grid.
    raw: Vec<[f64; 2]>,
    grid_raw: Grid,
    /// The key points as ideal pixels (`Camera::normalised`, then the pinhole of the camera), the
    /// frame of an essential matrix, and their grid.
    ideal: Vec<[f64; 2]>,
    grid_ideal: Grid,
}

impl Prepared {
    fn new(view: &FeatureView, cam: &Camera, stage1_points: usize, cell: f64) -> Prepared {
        let kp = view.keypoints();
        let n = kp.len();
        let all_in_stage1 = n <= stage1_points;
        let stage1: Vec<u32> = if all_in_stage1 {
            (0..n as u32).collect()
        } else if stage1_points == 0 {
            Vec::new()
        } else {
            let mut order: Vec<u32> = (0..n as u32).collect();
            let larger_first = |&i: &u32, &j: &u32| kp[j as usize][2].total_cmp(&kp[i as usize][2]).then(i.cmp(&j));
            order.select_nth_unstable_by(stage1_points - 1, larger_first);
            order.truncate(stage1_points);
            order.sort_unstable();
            order
        };
        let subset = if all_in_stage1 {
            Vec::new()
        } else {
            let d = view.descriptors();
            let mut subset = Vec::with_capacity(stage1.len() * DIM);
            for &i in &stage1 {
                subset.extend_from_slice(descriptor(d, i as usize));
            }
            subset
        };
        let raw: Vec<[f64; 2]> = kp.iter().map(|k| [f64::from(k[0]), f64::from(k[1])]).collect();
        let ideal: Vec<[f64; 2]> = raw
            .iter()
            .map(|&[x, y]| {
                let (u, v) = cam.normalised(x, y);
                [cam.fx * u + cam.cx, cam.fy * v + cam.cy]
            })
            .collect();
        let grid_raw = Grid::new(&raw, cell);
        let grid_ideal = Grid::new(&ideal, cell);
        Prepared { stage1, subset, all_in_stage1, raw, grid_raw, ideal, grid_ideal }
    }

    /// The descriptors of the first stage of the photo `name` and their key on a device.
    fn stage1_set<'v>(&'v self, view: &'v FeatureView, name: &str) -> (u64, &'v [u8]) {
        if self.all_in_stage1 {
            (photo_key(name), view.descriptors())
        } else {
            (stage1_key(name), &self.subset)
        }
    }
}

/// `(best, index, second)` of a descriptor over the candidates seen so far, with the rule of the
/// kernels whatever the order the candidates come in: the largest score is the best, the lowest
/// index among equal largest scores, and the second best equals the best on such a tie.
#[derive(Clone, Copy)]
struct Best {
    best: i64,
    index: u32,
    second: i64,
}

impl Best {
    const NONE: Best = Best { best: -1, index: 0, second: -1 };

    fn see(&mut self, score: u32, j: u32) {
        let score = i64::from(score);
        if score > self.best {
            self.second = self.best;
            self.best = score;
            self.index = j;
        } else if score == self.best {
            self.second = score;
            if j < self.index {
                self.index = j;
            }
        } else if score > self.second {
            self.second = score;
        }
    }

    /// As the kernels write it out: nothing seen is 0, which is also what a score of 0 gives.
    fn out(self) -> [u32; 3] {
        [self.best.max(0) as u32, self.index, self.second.max(0) as u32]
    }
}

/// The frame a guide is written in, which is the frame its band is walked in: ideal pixels (the
/// key points undistorted) for an essential matrix, the raw key points for a fundamental matrix,
/// which `verify` estimates on them. With distortion the two differ by pixels to tens of pixels
/// towards the borders, more than the band is wide.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Frame {
    Ideal,
    Raw,
}

/// The bests of the second stage, in the layout of the kernels: for every point of A over the
/// points of B within `e` of its epipolar line under `f` (`x_b^T F x_a = 0`, in the pixels of
/// `frame`), and for every point of B over the points of A whose band reached it (the mutual
/// check).
fn guided_bests(da: &[u8], pa: &Prepared, db: &[u8], pb: &Prepared, f: &Matrix3<f64>, frame: Frame, e: f64)
                -> Vec<[u32; 3]> {
    let (points_a, points_b, grid_b) = match frame {
        Frame::Ideal => (&pa.ideal, &pb.ideal, &pb.grid_ideal),
        Frame::Raw => (&pa.raw, &pb.raw, &pb.grid_raw),
    };
    bests_along(da, points_a, db, points_b, grid_b, f, e)
}

/// [`guided_bests`] on the points of A and B in one frame, those of B in their grid.
fn bests_along(da: &[u8], points_a: &[[f64; 2]], db: &[u8], points_b: &[[f64; 2]], grid_b: &Grid,
               f: &Matrix3<f64>, e: f64) -> Vec<[u32; 3]> {
    let (n_a, n_b) = (points_a.len(), points_b.len());
    let mut out = Vec::with_capacity(n_a + n_b);
    let mut cols = vec![Best::NONE; n_b];
    for (i, p) in points_a.iter().enumerate() {
        let mut row = Best::NONE;
        let l = f * Vector3::new(p[0], p[1], 1.0);
        let norm = (l.x * l.x + l.y * l.y).sqrt();
        if norm > 0.0 && norm.is_finite() && p.iter().all(|v| v.is_finite()) {
            let line = [l.x / norm, l.y / norm, l.z / norm];
            let qa = descriptor(da, i);
            grid_b.along(line, e, points_b, |j| {
                let score = dot(qa, descriptor(db, j as usize));
                row.see(score, j);
                cols[j as usize].see(score, i as u32);
            });
        }
        out.push(row.out());
    }
    out.extend(cols.into_iter().map(Best::out));
    out
}

/// The inverse calibration of `cam`: ideal pixels to normalised coordinates.
fn inverse_calibration(cam: &Camera) -> Matrix3<f64> {
    Matrix3::new(1.0 / cam.fx, 0.0, -cam.cx / cam.fx, 0.0, 1.0 / cam.fy, -cam.cy / cam.fy, 0.0, 0.0, 1.0)
}

/// The fundamental matrix that guides the second stage, and its frame: from the essential matrix
/// of a calibrated pair (`K_b^-T E K_a^-1`, ideal pixels), the fundamental matrix of the first
/// stage otherwise, in the raw pixels it was estimated on (a homography configuration keeps it:
/// the true correspondences of a plane or a panorama satisfy the fundamental matrix that
/// explained them too); none for a watermark or a degenerate pair, which is not trusted.
fn guide(stage1: &TwoView, cam_a: &Camera, cam_b: &Camera) -> Option<(Matrix3<f64>, Frame)> {
    let raw = stage1.f.map(|f| (f, Frame::Raw));
    match stage1.config {
        Config::Calibrated => stage1
            .e
            .map(|e| (inverse_calibration(cam_b).transpose() * e * inverse_calibration(cam_a), Frame::Ideal))
            .or(raw),
        Config::Uncalibrated | Config::Planar | Config::Panoramic | Config::PlanarOrPanoramic => raw,
        Config::Undefined | Config::Degenerate | Config::Watermark => None,
    }
}

/// The matches out of the bests (COLMAP's three tests, the ratio test at `max_ratio`), capped to
/// `cap` with the smallest distances kept (the earlier match of two equal ones), in the order of
/// the points of A, and their quality.
fn selected(bests: &[[u32; 3]], n_a: usize, n_b: usize, o: &Options, max_ratio: f32, cap: usize)
            -> (Vec<[u32; 2]>, Vec<f32>) {
    let mut matches = select_matches(bests, n_a, n_b, max_ratio, o.max_distance, o.cross_check);
    if matches.len() > cap {
        // the distance of a match falls as the dot product of its descriptors rises
        let mut order: Vec<usize> = (0..matches.len()).collect();
        order.sort_by_key(|&k| (std::cmp::Reverse(bests[matches[k][0] as usize][0]), k));
        let mut kept = order[..cap].to_vec();
        kept.sort_unstable();
        matches = kept.into_iter().map(|k| matches[k]).collect();
    }
    let quality = match_quality(bests, &matches);
    (matches, quality)
}

/// A pair with nothing matched.
fn dropped() -> PairResult {
    let two_view =
        TwoView { config: Config::Undefined, inliers: Vec::new(), e: None, f: None, h: None, cam2_from_cam1: None };
    PairResult { matches: Vec::new(), two_view, route: Route::Dropped }
}

/// The two stages on prepared photos (see [`match_pair`]).
#[allow(clippy::too_many_arguments)]
fn match_prepared(a: &FeatureView, pa: &Prepared, b: &FeatureView, pb: &Prepared, name_a: &str, name_b: &str,
                  cam_a: &Camera, cam_b: &Camera, calibrated: bool, preselected: bool, o: &Options,
                  bests: &mut Bests<'_>) -> Result<PairResult, String> {
    let (kp_a, kp_b) = (a.keypoints(), b.keypoints());
    let (n_a, n_b) = (kp_a.len(), kp_b.len());
    if n_a == 0 || n_b == 0 {
        return Ok(dropped());
    }
    let seed = pair_seed(name_a, name_b);
    let (da, db) = (a.descriptors(), b.descriptors());

    // the first stage: the largest scales (none with `stage1_points` 0: nothing asked of the
    // device, no match, no inlier)
    let (key_a, set_a) = pa.stage1_set(a, name_a);
    let (key_b, set_b) = pb.stage1_set(b, name_b);
    let bests1 = if set_a.is_empty() || set_b.is_empty() { Vec::new() } else { bests(key_a, set_a, key_b, set_b)? };
    let (m1, q1) = selected(&bests1, set_a.len() / DIM, set_b.len() / DIM, o, o.max_ratio, usize::MAX);
    let m1: Vec<[u32; 2]> = m1.iter().map(|m| [pa.stage1[m[0] as usize], pb.stage1[m[1] as usize]]).collect();
    let stage1 = verify(cam_a, kp_a, cam_b, kp_b, &m1, &q1, calibrated, &o.two_view, seed);
    let inliers = stage1.inliers.len();

    // the second stage: along the epipolar lines of the model of the first
    let trusted = inliers >= o.trust_inliers;
    if trusted && o.guided {
        if let Some((f, frame)) = guide(&stage1, cam_a, cam_b) {
            let band = o.two_view.ransac.max_error * o.guided_band;
            let bests2 = guided_bests(da, pa, db, pb, &f, frame, band);
            let (matches, quality) = selected(&bests2, n_a, n_b, o, o.guided_max_ratio, o.max_num_matches);
            let two_view = verify(cam_a, kp_a, cam_b, kp_b, &matches, &quality, calibrated, &o.two_view, seed);
            return Ok(PairResult { matches, two_view, route: Route::Guided });
        }
    }

    // brute force: every point against every point (the first stage already, when it took them all);
    // a trusted pair too when the second stage is off
    if (trusted && !o.guided) || inliers >= o.drop_inliers || preselected {
        let whole = pa.all_in_stage1 && pb.all_in_stage1;
        let bests_all = if whole { bests1 } else { bests(photo_key(name_a), da, photo_key(name_b), db)? };
        let (matches, quality) = selected(&bests_all, n_a, n_b, o, o.max_ratio, o.max_num_matches);
        let two_view = if whole && matches == m1 {
            stage1
        } else {
            verify(cam_a, kp_a, cam_b, kp_b, &matches, &quality, calibrated, &o.two_view, seed)
        };
        return Ok(PairResult { matches, two_view, route: Route::BruteForce });
    }
    Ok(dropped())
}

/// The matching of the pair of the photos `a` and `b` (their features, named `name_a` and
/// `name_b` as the store names them) with the cameras `cam_a` and `cam_b`: the two stages of the
/// module, the route decided by the first and by `preselected` (whether the preselection chose
/// the pair). `calibrated` is whether both cameras have a prior focal length (then the essential
/// matrix is estimated and guides the second stage). The seed of the RANSAC is `pair_seed` of
/// the two names and the keys of the descriptors on a device follow from them too, so the result
/// is the same in every run and on every device. `bests` does the first stage and brute force:
/// its error is this call's (a lost GPU). A photo without points is dropped at once.
#[allow(clippy::too_many_arguments)]
pub fn match_pair(a: &FeatureView, b: &FeatureView, name_a: &str, name_b: &str, cam_a: &Camera, cam_b: &Camera,
                  calibrated: bool, preselected: bool, o: &Options, bests: &mut Bests<'_>)
                  -> Result<PairResult, String> {
    let cell = cell_side(o.two_view.ransac.max_error);
    let pa = Prepared::new(a, cam_a, o.stage1_points, cell);
    let pb = Prepared::new(b, cam_b, o.stage1_points, cell);
    match_prepared(a, &pa, b, &pb, name_a, name_b, cam_a, cam_b, calibrated, preselected, o, bests)
}

/// A device of a run shared by every worker: its own GPU worker and the CPU workers, which take
/// their first stage (and brute force) from it and keep the second stage and the verification on
/// their threads, so that the GPU serves the pairs of every core instead of the one thread of its
/// worker. The first error (or panic) of the device marks it lost: its own worker is retired as
/// before, the CPU workers do the bests on the CPU from then on.
struct SharedDevice<'a> {
    /// Its name in the report and the log (two devices of one name told apart, `#2`).
    name: String,
    device: Mutex<Box<dyn Device + 'a>>,
    lost: AtomicBool,
    /// The pairs whose first stage it was asked for, for the test hook.
    asked: AtomicUsize,
}

/// What the workers of a run share.
struct Context<'a> {
    /// The pairs, sorted by name.
    pairs: Vec<(String, String, bool)>,
    features: &'a FeatureStore,
    cams: &'a HashMap<String, Camera>,
    calibrated: &'a HashSet<String>,
    out: &'a MatchStore,
    o: &'a Options,
    cell: f64,
    devices: Vec<SharedDevice<'a>>,
    /// The CPU workers made so far: each starts its search for a free device at its own.
    cpu_workers: AtomicUsize,
    /// `sched::fail_after`: a device is lost at its N-th pair, whichever worker asked.
    fail_after: Option<usize>,
    /// The devices lost under a CPU worker (the retired GPU workers are in the log of `sched`).
    log: Mutex<Vec<String>>,
    /// Set when a result could not be put into the match store: a pair failing then is the
    /// store's fault (a full disk), never given up as dropped.
    store_failed: AtomicBool,
}

impl Context<'_> {
    /// The bests of `device` for the first stage (`first`, which the test hook counts) or brute
    /// force of a pair; an error, or a panic, marks the device lost.
    /// `Err(None)` when it was lost before the call.
    fn on_device(&self, device: &SharedDevice<'_>, first: bool, key_a: u64, a: &[u8], key_b: u64, b: &[u8])
                 -> Result<Vec<[u32; 3]>, Option<String>> {
        let mut guard = device.device.lock().unwrap_or_else(PoisonError::into_inner);
        if device.lost.load(Ordering::SeqCst) {
            return Err(None);
        }
        let asked = if first { device.asked.fetch_add(1, Ordering::SeqCst) + 1 } else { 0 };
        let result = if first && self.fail_after == Some(asked) {
            Err(sched::FAIL_AFTER_VAR.to_owned())
        } else {
            match catch_unwind(AssertUnwindSafe(|| guard.bests(key_a, a, key_b, b))) {
                Ok(result) => result,
                Err(_) => Err("the device panicked".to_owned()),
            }
        };
        if result.is_err() {
            device.lost.store(true, Ordering::SeqCst);
        }
        result.map_err(Some)
    }

    /// The bests of a pair for a worker, and the slot of the device that gave them: a GPU worker
    /// (`own`) asks its device, whose error is the worker's; a CPU worker (`own` None) asks the
    /// first free device from its `start`, or waits for one, and does them on the CPU when every
    /// device is lost (a device lost under it is logged, and the CPU does that call).
    #[allow(clippy::too_many_arguments)]
    fn bests(&self, own: Option<usize>, start: usize, first: bool, key_a: u64, a: &[u8], key_b: u64, b: &[u8])
             -> Result<(usize, Vec<[u32; 3]>), String> {
        if let Some(k) = own {
            let device = &self.devices[k];
            return self.on_device(device, first, key_a, a, key_b, b)
                .map(|bests| (k, bests))
                .map_err(|problem| problem.unwrap_or_else(|| format!("{} was lost on another worker", device.name)));
        }
        let n = self.devices.len();
        loop {
            let alive: Vec<usize> =
                (0..n).map(|k| (start + k) % n).filter(|&k| !self.devices[k].lost.load(Ordering::SeqCst)).collect();
            let Some(&preferred) = alive.first() else { break };
            // a free device first, else the worker's own turn waits for its device
            let k = alive.iter().copied().find(|&k| !self.devices[k].lost.load(Ordering::SeqCst)
                                                      && self.devices[k].device.try_lock().is_ok())
                .unwrap_or(preferred);
            match self.on_device(&self.devices[k], first, key_a, a, key_b, b) {
                Ok(bests) => return Ok((k, bests)),
                Err(None) => {}
                Err(Some(problem)) => {
                    let line = format!("{}: {problem}; the CPU workers do their first stage on the CPU or on the \
                                        other GPUs", self.devices[k].name);
                    self.log.lock().unwrap_or_else(PoisonError::into_inner).push(line);
                }
            }
        }
        Ok((n, bests_cpu(a, b)))
    }
}

/// A worker doing whole pairs: the first stage on its device (a GPU worker), or on the free GPU of
/// the run (a CPU worker; the CPU twin without one), the second on its thread, the result into
/// the store. The photos it prepared last stay with it.
struct PairWorker<'c, 'a> {
    ctx: &'c Context<'a>,
    /// The index of its device among the devices of the run, `None` for a CPU worker.
    own: Option<usize>,
    /// Where a CPU worker starts looking for a free device.
    start: usize,
    /// The photos prepared, the one used last at the end.
    cache: Vec<(String, FeatureView, Prepared)>,
}

impl PairWorker<'_, '_> {
    /// The photo `name` prepared (mapped and prepared now when it is not in the cache, the
    /// photo used least recently forgotten when the cache is full) and moved to the end.
    fn prepare(&mut self, name: &str) -> Result<(), String> {
        if let Some(k) = self.cache.iter().position(|(n, _, _)| n == name) {
            let entry = self.cache.remove(k);
            self.cache.push(entry);
            return Ok(());
        }
        let view = self.ctx.features.get(name).ok_or_else(|| format!("{name}: its features cannot be mapped"))?;
        let prepared = Prepared::new(&view, &self.ctx.cams[name], self.ctx.o.stage1_points, self.ctx.cell);
        if self.cache.len() >= CACHED_PHOTOS {
            self.cache.remove(0);
        }
        self.cache.push((name.to_owned(), view, prepared));
        Ok(())
    }

    /// Its place in the report when no device was asked: its device's, or one past the devices
    /// for the CPU.
    fn slot(&self) -> usize {
        self.own.unwrap_or(self.ctx.devices.len())
    }
}

impl Worker<usize, (Route, usize)> for PairWorker<'_, '_> {
    fn name(&self) -> String {
        self.own.map_or_else(|| "CPU".to_owned(), |k| self.ctx.devices[k].name.clone())
    }

    fn run(&mut self, unit: &usize) -> Result<(Route, usize), String> {
        let ctx = self.ctx;
        let (name_a, name_b, preselected) = &ctx.pairs[*unit];
        self.prepare(name_a)?;
        self.prepare(name_b)?;
        let at = |cache: &[(String, FeatureView, Prepared)], name: &str| {
            cache.iter().position(|(n, _, _)| n == name).expect("prepared above")
        };
        let (ia, ib) = (at(&self.cache, name_a), at(&self.cache, name_b));
        let (cam_a, cam_b) = (&ctx.cams[name_a], &ctx.cams[name_b]);
        let calibrated = ctx.calibrated.contains(name_a) && ctx.calibrated.contains(name_b);
        // the device of the first stage, the pair's place in the report
        let mut stage1_slot = None;
        let result = {
            let (_, va, pa) = &self.cache[ia];
            let (_, vb, pb) = &self.cache[ib];
            let (own, start) = (self.own, self.start);
            let mut bests = |key_a: u64, a: &[u8], key_b: u64, b: &[u8]| {
                let first = stage1_slot.is_none();
                let (slot, bests) = ctx.bests(own, start, first, key_a, a, key_b, b)?;
                stage1_slot.get_or_insert(slot);
                Ok(bests)
            };
            match_prepared(va, pa, vb, pb, name_a, name_b, cam_a, cam_b, calibrated, *preselected, ctx.o, &mut bests)?
        };
        if let Err(e) = ctx.out.put(name_a, name_b, &result) {
            ctx.store_failed.store(true, Ordering::SeqCst);
            return Err(format!("the match store: {e}"));
        }
        Ok((result.route, stage1_slot.unwrap_or_else(|| self.slot())))
    }

    /// A pair that failed on another worker and then on the CPU (a photo whose features cannot be
    /// mapped any more, or a defect on that pair): dropped, unless the match store failed - the
    /// step's error, not the pair's.
    fn give_up(&mut self, _unit: &usize, _why: &str) -> Option<(Route, usize)> {
        (!self.ctx.store_failed.load(Ordering::SeqCst)).then_some((Route::Dropped, self.slot()))
    }
}

/// The error of a run, with the lines of its log after it.
fn failed(reason: String, lines: Vec<String>) -> String {
    if lines.is_empty() {
        reason
    } else {
        format!("{reason}\n{}", lines.join("\n"))
    }
}

/// Every pair of `pairs` (two photo names and whether the preselection chose the pair) matched
/// and put into `out`, through the queue of `sched`: one worker per device of `devices` and
/// `cpu_threads` CPU workers (none with no device is an error); with no CPU worker beside the
/// devices, one per core stands by for the pairs left when every device is lost. A pair that
/// fails on another worker and then on the CPU is dropped, with a line in the log; a device keeps
/// its own count in `stage1_by_device`, two devices of one name told apart by their place (`#2`).
/// `features` holds the photos,
/// `cams` their cameras (one per name; a pair of a photo without one, or not in the store, is an
/// error before any work), `calibrated` names the photos with a prior focal length (a pair is
/// calibrated when both are in it). `progress(done, total)` counts the pairs. The store is
/// flushed when the run ends, however it ends: on a cancel (`"cancelled"`) or with no worker
/// left, the pairs done so far are on disk and the error carries the log.
#[allow(clippy::too_many_arguments)]
pub fn match_on<'a>(pairs: &[(String, String, bool)], features: &FeatureStore, cams: &HashMap<String, Camera>,
                    calibrated: &HashSet<String>, out: &MatchStore, devices: Vec<Box<dyn Device + 'a>>,
                    cpu_threads: usize, o: &Options, cancel: &AtomicBool,
                    progress: &(dyn Fn(usize, usize) + Sync)) -> Result<PairsReport, String> {
    for (a, b, _) in pairs {
        for name in [a, b] {
            if !cams.contains_key(name) {
                return Err(format!("{name}: no camera for the photo"));
            }
            if features.points(name).is_none() {
                return Err(format!("{name}: not in the feature store"));
            }
        }
    }
    if cancel.load(Ordering::SeqCst) {
        return Err("cancelled".to_owned());
    }
    let mut sorted = pairs.to_vec();
    sorted.sort();
    let cell = cell_side(o.two_view.ransac.max_error);
    let mut names: Vec<String> = Vec::with_capacity(devices.len());
    for device in &devices {
        let name = device.name();
        let twins = names.iter().filter(|n| **n == name || n.starts_with(&format!("{name} #"))).count();
        names.push(if twins == 0 { name } else { format!("{name} #{}", twins + 1) });
    }
    let shared: Vec<SharedDevice<'_>> = devices
        .into_iter()
        .zip(&names)
        .map(|(device, name)| SharedDevice { name: name.clone(), device: Mutex::new(device),
                                             lost: AtomicBool::new(false), asked: AtomicUsize::new(0) })
        .collect();
    let ctx = Context { pairs: sorted, features, cams, calibrated, out, o, cell, devices: shared,
                        cpu_workers: AtomicUsize::new(0), fail_after: sched::fail_after(), log: Mutex::new(Vec::new()),
                        store_failed: AtomicBool::new(false) };
    let cpu_slot = names.len();
    let gpus: Vec<Box<dyn Worker<usize, (Route, usize)> + '_>> = (0..names.len())
        .map(|k| {
            Box::new(PairWorker { ctx: &ctx, own: Some(k), start: k, cache: Vec::new() })
                as Box<dyn Worker<usize, (Route, usize)> + '_>
        })
        .collect();
    let cpu: Option<sched::Factory<'_, usize, (Route, usize)>> = {
        let ctx = &ctx;
        Some(Box::new(move || {
            let start = ctx.cpu_workers.fetch_add(1, Ordering::SeqCst);
            Box::new(PairWorker { ctx, own: None, start, cache: Vec::new() })
                as Box<dyn Worker<usize, (Route, usize)> + '_>
        }))
    };
    let units: Vec<usize> = (0..ctx.pairs.len()).collect();
    let standby = thread::available_parallelism().map_or(1, |n| n.get());
    let run = sched::run_with_standby(units, gpus, cpu, cpu_threads, standby, cancel, progress);
    let flushed = out.flush().map_err(|e| format!("the match store: {e}"));
    let lost = std::mem::take(&mut *ctx.log.lock().unwrap_or_else(PoisonError::into_inner));
    let run = match run {
        Ok(mut run) => {
            run.log.extend(lost);
            run
        }
        Err((reason, mut log)) => {
            log.extend(lost);
            log.extend(flushed.err());
            return Err(failed(reason, log));
        }
    };
    flushed?;
    let mut by_route = (0, 0, 0);
    let mut counts = vec![0usize; names.len() + 1];
    for (_, (route, slot)) in &run.results {
        match route {
            Route::Guided => by_route.0 += 1,
            Route::BruteForce => by_route.1 += 1,
            Route::Dropped => by_route.2 += 1,
            Route::Imported => {}
        }
        counts[*slot] += 1;
    }
    let mut stage1_by_device: Vec<(String, usize)> = names.into_iter().zip(counts.iter().copied()).collect();
    if cpu_threads > 0 || counts[cpu_slot] > 0 {
        stage1_by_device.push(("CPU".to_owned(), counts[cpu_slot]));
    }
    Ok(PairsReport { done: run.results.len(), by_route, stage1_by_device, log: run.log })
}

/// [`match_on`] with the GPUs `devices` (adapter indices; one that opens no matcher is logged
/// and left to the others) and `cpu_threads` CPU workers beside them: `None` for one per core
/// (the CPU takes the pairs the GPUs do not reach, and all of them without a GPU), `Some(n)` for
/// `n`, `Some(0)` for the GPUs alone - an error (no worker) when none of them opens. The share
/// changes who does which pair, never a pair's result.
#[allow(clippy::too_many_arguments)]
pub fn match_all(pairs: &[(String, String, bool)], features: &FeatureStore, cams: &HashMap<String, Camera>,
                 calibrated: &HashSet<String>, out: &MatchStore, devices: &[usize], cpu_threads: Option<usize>,
                 o: &Options, cancel: &AtomicBool, progress: &(dyn Fn(usize, usize) + Sync))
                 -> Result<PairsReport, String> {
    let mut log = Vec::new();
    let mut opened: Vec<Box<dyn Device>> = Vec::new();
    for &index in devices {
        match Matcher::new(Some(index)) {
            Ok(matcher) => opened.push(Box::new(matcher)),
            Err(problem) => log.push(format!("GPU {index} not opened ({problem}): the other workers do its share")),
        }
    }
    let threads = cpu_threads.unwrap_or_else(|| thread::available_parallelism().map_or(1, |n| n.get()));
    match match_on(pairs, features, cams, calibrated, out, opened, threads, o, cancel, progress) {
        Ok(mut report) => {
            log.append(&mut report.log);
            report.log = log;
            Ok(report)
        }
        Err(problem) => Err(failed(problem, log)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_keys_follow_the_name() {
        assert_eq!(photo_key("a.jpg"), fnv1a("a.jpg".bytes()));
        assert_eq!(stage1_key("a.jpg"), fnv1a("a.jpg\0stage1".bytes()));
        assert_ne!(photo_key("a.jpg"), stage1_key("a.jpg"));
        assert_ne!(photo_key("a.jpg"), photo_key("b.jpg"));
        // the empty name is FNV's offset basis, never 0 (the key the matcher does not cache)
        assert_ne!(photo_key(""), 0);
    }

    #[test]
    fn the_bests_keep_the_rule_of_the_kernels_in_any_order() {
        let mut ordered = Best::NONE;
        for (j, s) in [5u32, 9, 9, 3, 7].into_iter().enumerate() {
            ordered.see(s, j as u32);
        }
        let mut reversed = Best::NONE;
        for (j, s) in [5u32, 9, 9, 3, 7].into_iter().enumerate().rev() {
            reversed.see(s, j as u32);
        }
        // the lowest index of the two nines, the second best equal to the best
        assert_eq!(ordered.out(), [9, 1, 9]);
        assert_eq!(reversed.out(), [9, 1, 9]);
        let mut plain = Best::NONE;
        for (j, s) in [2u32, 8, 4].into_iter().enumerate() {
            plain.see(s, j as u32);
        }
        assert_eq!(plain.out(), [8, 1, 4]);
        assert_eq!(Best::NONE.out(), [0, 0, 0]);
    }

    #[test]
    fn the_grid_finds_the_points_along_a_line_once() {
        // a lattice of points; the band around a few lines holds exactly the points within e
        let points: Vec<[f64; 2]> =
            (0..40).flat_map(|r| (0..60).map(move |c| [c as f64 * 7.5 + 3.0, r as f64 * 7.5 + 2.0])).collect();
        let grid = Grid::new(&points, 16.0);
        let lines = [[0.0, 1.0, -100.0], [1.0, 0.0, -200.3], [0.6, 0.8, -150.0], [-0.8, 0.6, 20.0],
                     [0.70710678, -0.70710678, 0.0]];
        for l in lines {
            let mut got = Vec::new();
            grid.along(l, 4.0, &points, |i| got.push(i));
            let mut want: Vec<u32> = (0..points.len() as u32)
                .filter(|&i| (l[0] * points[i as usize][0] + l[1] * points[i as usize][1] + l[2]).abs() <= 4.0)
                .collect();
            assert!(!want.is_empty(), "line {l:?} crosses no point");
            let count = got.len();
            got.sort_unstable();
            got.dedup();
            assert_eq!(got.len(), count, "line {l:?}: a point twice");
            want.sort_unstable();
            assert_eq!(got, want, "line {l:?}");
        }
        // a line that misses the box, an empty grid, a point that is not finite
        let mut got = Vec::new();
        grid.along([0.0, 1.0, 5000.0], 4.0, &points, |i| got.push(i));
        assert!(got.is_empty());
        Grid::new(&[], 16.0).along([0.0, 1.0, -1.0], 4.0, &[], |_| panic!("no point"));
        let odd = [[1.0, 1.0], [f64::NAN, 2.0], [3.0, 1.5]];
        let grid = Grid::new(&odd, 16.0);
        let mut got = Vec::new();
        grid.along([0.0, 1.0, -1.0], 4.0, &odd, |i| got.push(i));
        assert_eq!(got, [0, 2]);
    }

    #[test]
    fn the_cap_keeps_the_largest_dot_products_in_the_order_of_a() {
        let o = Options { stage1_points: 10, trust_inliers: 40, drop_inliers: 8, max_ratio: 0.8, max_distance: 0.7,
                          cross_check: false, max_num_matches: 2, two_view: two_view::Options::default(),
                          guided: true, guided_max_ratio: 0.8, guided_band: 1.0 };
        let s = |cos: f32| (cos * 512.0 * 512.0) as u32;
        // four descriptors of A matched to B: scores 0.95, 0.99, 0.95, 0.97; the cap of two
        // keeps the second and the fourth, in that order
        let bests = vec![[s(0.95), 0, s(0.5)], [s(0.99), 1, s(0.5)], [s(0.95), 2, s(0.5)], [s(0.97), 3, s(0.5)],
                         [0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]];
        let (matches, quality) = selected(&bests, 4, 4, &o, o.max_ratio, 2);
        assert_eq!(matches, vec![[1, 1], [3, 3]]);
        assert_eq!(quality.len(), 2);
        // a tie at the cap: the earlier match stays
        let (matches, _) = selected(&bests, 4, 4, &o, o.max_ratio, 3);
        assert_eq!(matches, vec![[0, 0], [1, 1], [3, 3]]);
        let (matches, _) = selected(&bests, 4, 4, &o, o.max_ratio, usize::MAX);
        assert_eq!(matches.len(), 4);
        // the ratio given, not the options': at 0.1 none passes (their ratios of distances, the
        // angles of the best and the second best, are 0.14 to 0.31)
        let (matches, _) = selected(&bests, 4, 4, &o, 0.1, usize::MAX);
        assert!(matches.is_empty(), "{matches:?}");
    }

    /// Points spread over a photo of 6000 x 4000 pixels, a few far outside it, and lines at every
    /// angle, through it, along its edges and past it.
    fn random_points_and_lines(seed: u64, n_points: usize, n_lines: usize) -> (Vec<[f64; 2]>, Vec<[f64; 3]>) {
        let mut state = seed;
        let mut next = move || {
            state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (state >> 11) as f64 / (1u64 << 53) as f64
        };
        let mut points: Vec<[f64; 2]> = (0..n_points).map(|_| [6000.0 * next(), 4000.0 * next()]).collect();
        for k in 0..n_points / 500 {
            points[k * 499] = [-3000.0 + 12000.0 * next(), -2000.0 + 8000.0 * next()];
        }
        let lines = (0..n_lines)
            .map(|_| {
                let angle = std::f64::consts::PI * next();
                let (a, b) = (angle.cos(), angle.sin());
                // through a point of a box larger than the photo, so that some lines miss it
                let (x, y) = (-2000.0 + 10000.0 * next(), -2000.0 + 8000.0 * next());
                [a, b, -(a * x + b * y)]
            })
            .collect();
        (points, lines)
    }

    #[test]
    fn the_band_walk_finds_what_a_scan_of_every_point_finds() {
        for (seed, cell, e) in [(1u64, 16.0, 4.0), (2, 8.0, 1.0), (3, 40.0, 10.0)] {
            let (points, mut lines) = random_points_and_lines(seed, 5000, 300);
            // and the axes, exactly horizontal and vertical, on and between the cells
            lines.extend([[0.0, 1.0, -1000.0], [1.0, 0.0, -2048.0], [0.0, -1.0, 16.0], [-1.0, 0.0, 5999.5]]);
            let grid = Grid::new(&points, cell);
            for l in lines {
                let mut got = Vec::new();
                grid.along(l, e, &points, |i| got.push(i));
                let count = got.len();
                got.sort_unstable();
                got.dedup();
                assert_eq!(got.len(), count, "line {l:?}: a point twice");
                let want: Vec<u32> = (0..points.len() as u32)
                    .filter(|&i| (l[0] * points[i as usize][0] + l[1] * points[i as usize][1] + l[2]).abs() <= e)
                    .collect();
                assert_eq!(got, want, "seed {seed}, line {l:?}");
            }
        }
    }

    /// A timing probe, not a check (`cargo test --release -p ppg_engine --lib band_walk -- --ignored
    /// --nocapture`): the band walk of 25,000 lines over 25,000 points of a 6000 x 4000 photo with
    /// the cells and band of COLMAP's 4 px.
    #[test]
    #[ignore]
    fn timing_of_the_band_walk() {
        let (points, lines) = random_points_and_lines(9, 25000, 25000);
        let grid = Grid::new(&points, cell_side(4.0));
        let mut best = f64::INFINITY;
        let mut found = 0;
        for _ in 0..5 {
            let start = std::time::Instant::now();
            found = 0;
            for &l in &lines {
                grid.along(l, 4.0, &points, |_| found += 1);
            }
            best = best.min(start.elapsed().as_secs_f64());
        }
        println!("band walk: {:.1} ms for {} lines, {found} points found", best * 1e3, lines.len());
    }

    /// `n` descriptors as SIFT's bytes (the squares of uniform values scaled to a norm of 512),
    /// out of the generator of `random_points_and_lines`.
    fn random_descriptors(seed: u64, n: usize) -> Vec<u8> {
        let mut state = seed;
        let mut next = move || {
            state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (state >> 11) as f64 / (1u64 << 53) as f64
        };
        let mut out = Vec::with_capacity(n * DIM);
        for _ in 0..n {
            let values: Vec<f64> = (0..DIM).map(|_| next().powi(2)).collect();
            let norm = values.iter().map(|v| v * v).sum::<f64>().sqrt();
            out.extend(values.iter().map(|v| (v / norm * 512.0).round().clamp(0.0, 255.0) as u8));
        }
        out
    }

    /// The fundamental matrix of two photos taken side by side: `[e]x` with the epipole `e` far
    /// to the left of a 6000 x 4000 photo, so that every line crosses the photo nearly level.
    fn side_by_side() -> Matrix3<f64> {
        let e = Vector3::new(-40000.0, 1500.0, 1.0);
        Matrix3::new(0.0, -e.z, e.y, e.z, 0.0, -e.x, -e.y, e.x, 0.0)
    }

    /// A timing probe, not a check (`cargo test --release -p ppg_engine --lib guided_stage -- --ignored
    /// --nocapture`): the bests of the second stage of a pair of 30,000 points each over a 6000 x 4000
    /// photo, the band of COLMAP's 4 px, on one thread.
    #[test]
    #[ignore]
    fn timing_of_the_guided_stage() {
        const N: usize = 30000;
        let (points_a, _) = random_points_and_lines(11, N, 0);
        let (points_b, _) = random_points_and_lines(12, N, 0);
        let (da, db) = (random_descriptors(13, N), random_descriptors(14, N));
        let grid = Grid::new(&points_b, cell_side(4.0));
        let f = side_by_side();
        let mut candidates = 0usize;
        for p in &points_a {
            let l = f * Vector3::new(p[0], p[1], 1.0);
            let norm = (l.x * l.x + l.y * l.y).sqrt();
            grid.along([l.x / norm, l.y / norm, l.z / norm], 4.0, &points_b, |_| candidates += 1);
        }
        let mut best = f64::INFINITY;
        let mut sum = 0u64;
        for _ in 0..3 {
            let start = std::time::Instant::now();
            let bests = bests_along(&da, &points_a, &db, &points_b, &grid, &f, 4.0);
            best = best.min(start.elapsed().as_secs_f64());
            sum = bests.iter().map(|b| u64::from(b[0]) + u64::from(b[1]) + u64::from(b[2])).sum();
        }
        println!("guided stage: {:.1} ms for {N} x {N} points, {candidates} candidates ({:.1} ns each), checksum {sum}",
                 best * 1e3, best * 1e9 / candidates as f64);
    }
}
