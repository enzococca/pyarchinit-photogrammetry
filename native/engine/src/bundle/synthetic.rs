//! Synthetic blocks for the tests and for `ppg-bench adjust`: a nadir grid flight 30 m above
//! gently rolling ground, seen by an OPENCV camera of 1200 x 900 pixels, with the GNSS position
//! of every photo, exact observations of its points (each seen by two photos at least, as a
//! pipeline hands them over) and of five markers held by control points; then the block
//! perturbed as the Python tests do (`tests/test_bundle.py::_perturb`): the poses, the points and
//! the markers moved by normal noise, the calibration 1 % off and without distortion. The truth
//! is returned with the perturbed problem, so that a solution can be checked against it.

use super::problem::{compose, exp_rotation, CameraBlock, Obs, Problem};
use crate::camera::{project, Model};

const WIDTH: f64 = 1200.0;
const HEIGHT: f64 = 900.0;
const FOCAL: f64 = 1000.0;
const PARAMS: [f64; 8] = [FOCAL, FOCAL, WIDTH / 2.0, HEIGHT / 2.0, -0.02, 0.01, 0.0, 0.0];
/// The intrinsics adjusted: both focal lengths, k1 and k2.
const FREE_INTRINSICS: [usize; 4] = [0, 1, 4, 5];
/// Flying height above the mean ground, metres.
const FLYING_HEIGHT: f64 = 30.0;
/// The overlap of the photos along both axes of the grid, as a part of the footprint (36 x 27 m
/// from 30 m): 60 %, so that a point inside the block is seen by about four photos.
const OVERLAP: f64 = 0.6;
/// A pixel this close to the edge of the photo is not observed.
const MARGIN_PX: f64 = 5.0;
/// Accuracy of the control points, metres.
const CONTROL_SIGMA: f64 = 0.002;
/// Accuracy of the GNSS position of each photo, metres per axis: a drone's receiver. Without
/// the positions a nadir block over gentle relief hardly tells its focal length from its flying
/// height, and the adjustment creeps along that valley.
const GNSS_SIGMA: f64 = 0.05;
/// The perturbation of `_perturb`: the centres, the angles, the points and markers.
const PERTURB_CENTRE: f64 = 0.3;
const PERTURB_ANGLE: f64 = 0.01;
const PERTURB_POINT: f64 = 0.2;

/// Where the block really is: what `grid_block` perturbs.
#[derive(Clone, Debug, PartialEq)]
pub struct Truth {
    pub rotations: Vec<[[f64; 3]; 3]>,
    pub centres: Vec<[f64; 3]>,
    pub points: Vec<[f64; 3]>,
    pub markers: Vec<[f64; 3]>,
}

/// SplitMix64: the same block for the same seed, on every computer, without a dependency.
struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// Uniform in [0, 1).
    fn uniform(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }

    fn uniform_in(&mut self, low: f64, high: f64) -> f64 {
        low + (high - low) * self.uniform()
    }

    /// Standard normal (Box-Muller).
    fn normal(&mut self) -> f64 {
        let u1 = 1.0 - self.uniform();
        let u2 = self.uniform();
        (-2.0 * u1.ln()).sqrt() * (std::f64::consts::TAU * u2).cos()
    }

    fn normal_v3(&mut self, sigma: f64) -> [f64; 3] {
        [sigma * self.normal(), sigma * self.normal(), sigma * self.normal()]
    }
}

/// world -> camera of a camera looking down, turned by `yaw` about its axis: x east, y south
/// in the image, z down.
fn look_down(yaw: f64) -> [[f64; 3]; 3] {
    let (s, c) = yaw.sin_cos();
    [[c, -s, 0.0], [-s, -c, 0.0], [0.0, 0.0, -1.0]]
}

/// The height of the ground at `(x, y)`: a gentle relief.
fn ground(x: f64, y: f64) -> f64 {
    0.6 * (x / 7.0).sin() + 0.4 * (y / 5.0).cos()
}

/// The pixel of `world` in the photo `(r, c)`, `None` behind the camera or within `MARGIN_PX` of
/// the edge.
fn pixel(r: &[[f64; 3]; 3], c: [f64; 3], world: [f64; 3]) -> Option<[f64; 2]> {
    let rel = [world[0] - c[0], world[1] - c[1], world[2] - c[2]];
    let x = [
        r[0][0] * rel[0] + r[0][1] * rel[1] + r[0][2] * rel[2],
        r[1][0] * rel[0] + r[1][1] * rel[1] + r[1][2] * rel[2],
        r[2][0] * rel[0] + r[2][1] * rel[1] + r[2][2] * rel[2],
    ];
    if x[2] <= 0.0 {
        return None;
    }
    let (u, v) = project(Model::OpenCv, &PARAMS, x[0] / x[2], x[1] / x[2]);
    let inside = u > MARGIN_PX && u < WIDTH - MARGIN_PX && v > MARGIN_PX && v < HEIGHT - MARGIN_PX;
    inside.then_some([u, v])
}

/// The grid of a flight of `photos` photos: its columns, rows and the spacing along each axis.
struct Grid {
    cols: usize,
    rows: usize,
    dx: f64,
    dy: f64,
}

impl Grid {
    fn new(photos: usize) -> Grid {
        let cols = (photos as f64).sqrt().ceil().max(1.0) as usize;
        let rows = photos.div_ceil(cols).max(1);
        // the half-footprint on the ground: 18 x 13.5 m from 30 m
        let (hx, hy) = (FLYING_HEIGHT * WIDTH / 2.0 / FOCAL, FLYING_HEIGHT * HEIGHT / 2.0 / FOCAL);
        Grid { cols, rows, dx: 2.0 * hx * (1.0 - OVERLAP), dy: 2.0 * hy * (1.0 - OVERLAP) }
    }

    /// The photos whose footprint may hold the ground point `(x, y)`: those whose centre is within
    /// the half-footprint plus a metre (the strips are turned by a hundredth of a radian).
    fn candidates(&self, photos: usize, x: f64, y: f64) -> impl Iterator<Item = usize> + '_ {
        let (hx, hy) = (FLYING_HEIGHT * WIDTH / 2.0 / FOCAL + 1.0, FLYING_HEIGHT * HEIGHT / 2.0 / FOCAL + 1.0);
        let range = |v: f64, half: f64, step: f64, count: usize| {
            let lo = ((v - half) / step).ceil().max(0.0) as usize;
            let hi = (((v + half) / step).floor().max(-1.0) as isize + 1).max(0) as usize;
            lo..hi.min(count)
        };
        let cols = range(x, hx, self.dx, self.cols);
        range(y, hy, self.dy, self.rows)
            .flat_map(move |row| cols.clone().map(move |col| row * self.cols + col))
            .filter(move |&k| k < photos)
    }
}

/// A nadir grid flight of `photos` photos with their GNSS positions, `photos * points_per_photo`
/// ground points, each seen by two photos at least (one, with a single photo), and five markers
/// held by control points, the observations exact; then the block perturbed. `seed` chooses the
/// points and the perturbation.
pub fn grid_block(photos: usize, points_per_photo: usize, seed: u64) -> (Problem, Truth) {
    let mut rng = Rng(seed);
    let grid = Grid::new(photos);
    let mut rotations = Vec::with_capacity(photos);
    let mut centres = Vec::with_capacity(photos);
    for k in 0..photos {
        let (row, col) = (k / grid.cols, k % grid.cols);
        let yaw = if row % 2 == 0 { 0.01 } else { -0.01 };
        rotations.push(look_down(yaw));
        centres.push([col as f64 * grid.dx, row as f64 * grid.dy, FLYING_HEIGHT]);
    }
    // the ground under the block: the footprints of the photos, inset by a tenth for the markers
    let (hx, hy) = (FLYING_HEIGHT * WIDTH / 2.0 / FOCAL, FLYING_HEIGHT * HEIGHT / 2.0 / FOCAL);
    let (x0, x1) = (-hx, (grid.cols - 1) as f64 * grid.dx + hx);
    let (y0, y1) = (-hy, (grid.rows - 1) as f64 * grid.dy + hy);
    let views_of = |w: [f64; 3]| -> Vec<(usize, [f64; 2])> {
        let seen = grid.candidates(photos, w[0], w[1]).filter_map(|i| Some((i, pixel(&rotations[i], centres[i], w)?)));
        seen.collect()
    };
    // a tie point is seen by two photos at least (a pipeline never hands over one seen once):
    // ground points are drawn until the block has its count of them
    let target = photos * points_per_photo;
    let (mut points, mut obs) = (Vec::with_capacity(target), Vec::new());
    while points.len() < target {
        let (x, y) = (rng.uniform_in(x0, x1), rng.uniform_in(y0, y1));
        let w = [x, y, ground(x, y)];
        let views = views_of(w);
        if views.len() < 2.min(photos) {
            continue;
        }
        let p = points.len();
        points.push(w);
        obs.extend(views.into_iter().map(|(i, xy)| Obs { image: i, point: p, xy }));
    }
    let (ix, iy) = (0.1 * (x1 - x0), 0.1 * (y1 - y0));
    let corners = [(x0 + ix, y0 + iy), (x1 - ix, y0 + iy), (x1 - ix, y1 - iy), (x0 + ix, y1 - iy)];
    let centre = (0.5 * (x0 + x1), 0.5 * (y0 + y1));
    let markers: Vec<[f64; 3]> =
        corners.into_iter().chain(std::iter::once(centre)).map(|(x, y)| [x, y, ground(x, y)]).collect();
    let mut mobs = Vec::new();
    for (m, w) in markers.iter().enumerate() {
        mobs.extend(views_of(*w).into_iter().map(|(i, xy)| Obs { image: i, point: m, xy }));
    }
    let truth = Truth { rotations: rotations.clone(), centres: centres.clone(), points: points.clone(), markers };
    let mut pb = Problem {
        rotations,
        centres,
        image_camera: vec![0; photos],
        cameras: vec![CameraBlock {
            model: Model::OpenCv,
            params: PARAMS.to_vec(),
            width: WIDTH,
            height: HEIGHT,
            free: FREE_INTRINSICS.to_vec(),
        }],
        points,
        obs,
        markers: truth.markers.clone(),
        mobs,
        gcps: truth.markers.iter().enumerate().map(|(m, x)| (m, *x, [CONTROL_SIGMA; 3])).collect(),
        gnss: truth.centres.iter().enumerate().map(|(i, c)| (i, *c, [GNSS_SIGMA; 3], 0)).collect(),
        fixed_images: vec![false; photos],
        ..Problem::default()
    };
    for c in pb.centres.iter_mut() {
        let d = rng.normal_v3(PERTURB_CENTRE);
        *c = [c[0] + d[0], c[1] + d[1], c[2] + d[2]];
    }
    for r in pb.rotations.iter_mut() {
        *r = compose(&exp_rotation(rng.normal_v3(PERTURB_ANGLE)), r);
    }
    for p in pb.points.iter_mut().chain(pb.markers.iter_mut()) {
        let d = rng.normal_v3(PERTURB_POINT);
        *p = [p[0] + d[0], p[1] + d[1], p[2] + d[2]];
    }
    pb.cameras[0].params = vec![FOCAL * 1.01, FOCAL * 1.01, WIDTH / 2.0, HEIGHT / 2.0, 0.0, 0.0, 0.0, 0.0];
    (pb, truth)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bundle::problem::Parameters;
    use crate::bundle::residuals::{evaluate_at, Loss};

    #[test]
    fn the_grid_block_is_exact_at_its_truth_and_perturbed_as_given() {
        let (pb, truth) = grid_block(12, 50, 7);
        pb.check().unwrap();
        assert_eq!((pb.centres.len(), pb.points.len(), pb.markers.len()), (12, 600, 5));
        assert_eq!((pb.gcps.len(), pb.cameras[0].free.clone()), (5, vec![0, 1, 4, 5]));
        assert!(pb.fixed_images.iter().all(|f| !f));
        // the GNSS position of every photo, exact, without a bias
        assert_eq!(pb.gnss.len(), 12);
        assert!(pb.gnss.iter().enumerate().all(|(i, g)| g.0 == i && g.1 == truth.centres[i] && g.3 == 0));
        assert!(!pb.gnss_bias);
        // a 4 x 3 grid 30 m up, every photo of the one camera
        let grid = Grid::new(12);
        assert_eq!((grid.cols, grid.rows), (4, 3));
        assert!(truth.centres.iter().all(|c| c[2] == FLYING_HEIGHT));
        assert!((truth.centres[1][0] - truth.centres[0][0] - 14.4).abs() < 1e-12);
        assert!((truth.centres[4][1] - truth.centres[0][1] - 10.8).abs() < 1e-12);
        // every observation is inside its photo, every point is seen by two photos at least and
        // by about four on average
        assert!(pb.obs.iter().all(|o| o.xy[0] > 0.0 && o.xy[0] < WIDTH && o.xy[1] > 0.0 && o.xy[1] < HEIGHT));
        let mut seen = vec![0usize; pb.points.len()];
        for o in &pb.obs {
            seen[o.point] += 1;
        }
        assert!(seen.iter().all(|s| *s >= 2), "a point seen once");
        let per_point = pb.obs.len() as f64 / pb.points.len() as f64;
        assert!((3.0..6.0).contains(&per_point), "{per_point} observations per point");
        assert!(pb.mobs.len() >= 5, "{} marker observations", pb.mobs.len());
        // exact at the truth, not at the perturbed start
        let at_truth = Parameters {
            rotations: truth.rotations.clone(),
            centres: truth.centres.clone(),
            camera_params: vec![PARAMS.to_vec()],
            points: truth.points.clone(),
            markers: truth.markers.clone(),
            rolling_shutter: Vec::new(),
            bias: Vec::new(),
        };
        let exact = evaluate_at(&pb, &at_truth, Loss::None);
        assert!(exact.cost < 1e-18, "cost {} at the truth", exact.cost);
        let start = evaluate_at(&pb, &Parameters::initial(&pb), Loss::None).cost;
        assert!(start > 1e3, "cost {start} at the start");
        let moved = pb.centres.iter().zip(&truth.centres).map(|(a, b)| (a[0] - b[0]).abs()).fold(0.0, f64::max);
        assert!(moved > 0.05 && moved < 2.0, "the centres moved {moved} m");
        assert_eq!(pb.cameras[0].params[0], FOCAL * 1.01);
        assert_eq!(pb.cameras[0].params[4], 0.0);
        // the seed chooses the block; the same seed gives the same block
        assert_eq!(grid_block(12, 50, 7).0, pb);
        assert_ne!(grid_block(12, 50, 8).0.points[0], pb.points[0]);
        // one photo is still a block
        let (one, _) = grid_block(1, 20, 1);
        one.check().unwrap();
        assert_eq!((one.centres.len(), one.points.len()), (1, 20));
        assert!(!one.obs.is_empty());
    }

    #[test]
    fn the_candidates_of_a_point_are_the_photos_around_it() {
        let grid = Grid::new(60);
        assert_eq!((grid.cols, grid.rows), (8, 8));
        // the first photo's centre: the photos of the first two strips and columns, no more
        let around: Vec<usize> = grid.candidates(60, 0.0, 0.0).collect();
        assert_eq!(around, vec![0, 1, 8, 9]);
        // a point far outside the block has no candidate; one past the last photo of a partial
        // last strip only the photos that exist
        assert_eq!(grid.candidates(60, 500.0, 500.0).count(), 0);
        let last: Vec<usize> = grid.candidates(60, 7.0 * grid.dx, 7.0 * grid.dy).collect();
        assert!(last.iter().all(|&k| k < 60) && last.contains(&55), "{last:?}");
    }
}
