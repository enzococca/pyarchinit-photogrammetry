//! The native bundle adjustment on the block of `tests/test_bundle.py::_block`: 3 strips of 6
//! photos 30 m above rolling ground, 600 points and 5 markers seen through an OPENCV camera of
//! 1200 x 900 pixels, every observation exact. The residuals and their Jacobian are checked
//! against central differences and the structure of `_Model.sparsity`; the solver against the
//! closed-form triangulation, the recovered block, a point behind the cameras, a block without
//! a gauge, the robust losses, the same bits twice and the cancellation.

use std::collections::HashSet;

use ppg_engine::bundle::{
    evaluate, evaluate_at, solve, CameraBlock, Layout, Loss, Obs, Options, Parameters, Problem, Solution,
    Termination,
};
use ppg_engine::camera::{project, Model};

const W: f64 = 1200.0;
const H: f64 = 900.0;
const F: f64 = 1000.0;
const PARAMS: [f64; 8] = [F, F, W / 2.0, H / 2.0, -0.02, 0.01, 0.0, 0.0];
const N_PHOTOS: usize = 18;

type M3 = [[f64; 3]; 3];
type V3 = [f64; 3];

/// SplitMix64 with a fixed seed: the same block at every run, without a dependency.
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

    fn normal_v3(&mut self, sigma: f64) -> V3 {
        [sigma * self.normal(), sigma * self.normal(), sigma * self.normal()]
    }
}

fn mat_vec(m: &M3, v: V3) -> V3 {
    [
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    ]
}

fn mat_mul(a: &M3, b: &M3) -> M3 {
    let mut out = [[0.0; 3]; 3];
    for (i, row) in out.iter_mut().enumerate() {
        for (j, x) in row.iter_mut().enumerate() {
            *x = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j];
        }
    }
    out
}

fn sub(a: V3, b: V3) -> V3 {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}

fn norm(v: V3) -> f64 {
    (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt()
}

fn distance(a: V3, b: V3) -> f64 {
    norm(sub(a, b))
}

/// Rodrigues' formula, written here on its own so that the rotations of the tests do not come
/// from the code under test.
fn exp_so3(w: V3) -> M3 {
    let theta = norm(w);
    let k = [[0.0, -w[2], w[1]], [w[2], 0.0, -w[0]], [-w[1], w[0], 0.0]];
    let k2 = mat_mul(&k, &k);
    let (a, b) = if theta < 1e-12 { (1.0, 0.5) } else { (theta.sin() / theta, (1.0 - theta.cos()) / (theta * theta)) };
    let mut out = [[0.0; 3]; 3];
    for i in 0..3 {
        for j in 0..3 {
            out[i][j] = if i == j { 1.0 } else { 0.0 } + a * k[i][j] + b * k2[i][j];
        }
    }
    out
}

/// world -> camera of a camera looking down (nadir), turned by `yaw`: x east, y south in the
/// image, z down.
fn look_down(yaw: f64) -> M3 {
    let (s, c) = yaw.sin_cos();
    [[c, -s, 0.0], [-s, -c, 0.0], [0.0, 0.0, -1.0]]
}

/// Pixel of `world` from the photo `(r, c)` moving at `vel` while the frame is read; the row read
/// depends on where the point falls, so a few rounds (as the Python block). `None` behind the
/// camera or outside the photo by less than 5 pixels.
fn pixel(r: &M3, c: V3, vel: V3, world: V3) -> Option<[f64; 2]> {
    let mut tau = 0.0;
    let mut px = [0.0; 2];
    for _ in 0..4 {
        let rel = [world[0] - c[0] - vel[0] * tau, world[1] - c[1] - vel[1] * tau, world[2] - c[2] - vel[2] * tau];
        let x = mat_vec(r, rel);
        if x[2] <= 0.0 {
            return None;
        }
        let (u, v) = project(Model::OpenCv, &PARAMS, x[0] / x[2], x[1] / x[2]);
        px = [u, v];
        tau = v / H - 0.5;
    }
    let inside = px[0] > 5.0 && px[0] < W - 5.0 && px[1] > 5.0 && px[1] < H - 5.0;
    inside.then_some(px)
}

struct Truth {
    centres: Vec<V3>,
    points: Vec<V3>,
    markers: Vec<V3>,
}

/// The OPENCV camera of the block, with the intrinsics `free` adjusted.
fn camera(free: &[usize]) -> CameraBlock {
    CameraBlock { model: Model::OpenCv, params: PARAMS.to_vec(), width: W, height: H, free: free.to_vec() }
}

/// The drone block of `tests/test_bundle.py::_block` (`rolling`: velocity per photo, metres per
/// frame read), with exact observations and the intrinsics `[0, 1, 4, 5]` free.
fn block(rng: &mut Rng, n_points: usize, rolling: Option<&[V3]>) -> (Problem, Truth) {
    let mut rotations = Vec::new();
    let mut centres = Vec::new();
    for (row, y) in [-12.0, 0.0, 12.0].into_iter().enumerate() {
        for k in 0..6 {
            rotations.push(look_down(0.02 * row as f64));
            centres.push([-20.0 + 40.0 * k as f64 / 5.0, y, 30.0]);
        }
    }
    let points: Vec<V3> = (0..n_points)
        .map(|_| {
            let x = rng.uniform_in(-25.0, 25.0);
            let y = rng.uniform_in(-18.0, 18.0);
            [x, y, 0.6 * (x / 7.0).sin() + 0.4 * (y / 5.0).cos()]
        })
        .collect();
    let markers =
        vec![[-15.0, -10.0, 0.1], [15.0, -10.0, 0.2], [15.0, 10.0, -0.1], [-15.0, 10.0, 0.3], [0.0, 0.0, 0.0]];
    let still = vec![[0.0; 3]; N_PHOTOS];
    let vel = rolling.unwrap_or(&still);
    let observe = |world: &[V3]| -> Vec<Obs> {
        let mut out = Vec::new();
        for i in 0..N_PHOTOS {
            for (p, w) in world.iter().enumerate() {
                if let Some(xy) = pixel(&rotations[i], centres[i], vel[i], *w) {
                    out.push(Obs { image: i, point: p, xy });
                }
            }
        }
        out
    };
    let obs = observe(&points);
    let mobs = observe(&markers);
    let pb = Problem {
        rotations,
        centres: centres.clone(),
        image_camera: vec![0; N_PHOTOS],
        cameras: vec![camera(&[0, 1, 4, 5])],
        points: points.clone(),
        obs,
        markers: markers.clone(),
        mobs,
        fixed_images: vec![false; N_PHOTOS],
        ..Problem::default()
    };
    (pb, Truth { centres, points, markers })
}

fn gcps(truth: &Truth, sigma: f64, which: &[usize]) -> Vec<(usize, V3, V3)> {
    which.iter().map(|&m| (m, truth.markers[m], [sigma; 3])).collect()
}

/// `tests/test_bundle.py::_perturb`: the poses, points and markers moved by normal noise.
fn perturb(pb: &mut Problem, rng: &mut Rng, centre: f64, angle: f64, point: f64) {
    for c in pb.centres.iter_mut() {
        let d = rng.normal_v3(centre);
        *c = [c[0] + d[0], c[1] + d[1], c[2] + d[2]];
    }
    for r in pb.rotations.iter_mut() {
        *r = mat_mul(&exp_so3(rng.normal_v3(angle)), r);
    }
    for p in pb.points.iter_mut().chain(pb.markers.iter_mut()) {
        let d = rng.normal_v3(point);
        *p = [p[0] + d[0], p[1] + d[1], p[2] + d[2]];
    }
}

/// The block recovered from wrong poses towards four control points, as
/// `test_a_block_comes_back_to_its_control_points`: the calibration 1 % off and without distortion.
fn control_point_block(seed: u64) -> (Problem, Truth) {
    let mut rng = Rng(seed);
    let (mut pb, truth) = block(&mut rng, 600, None);
    pb.gcps = gcps(&truth, 0.002, &[0, 1, 2, 3]);
    perturb(&mut pb, &mut rng, 0.3, 0.01, 0.2);
    pb.cameras[0].params = vec![F * 1.01, F * 1.01, W / 2.0, H / 2.0, 0.0, 0.0, 0.0, 0.0];
    (pb, truth)
}

/// Which columns each residual row may touch: `_Model.sparsity` of bundle.py, row by row.
fn sparsity(pb: &Problem, lay: &Layout) -> Vec<Vec<usize>> {
    let image_cols = |i: usize| {
        let mut cs = Vec::new();
        if let Some(c0) = lay.image_col[i] {
            cs.extend(c0..c0 + 6);
        }
        cs.extend(lay.camera_cols(pb.image_camera[i]));
        if let Some(r0) = lay.rs_col_of(i) {
            cs.extend(r0..r0 + 6);
        }
        cs
    };
    let mut rows = Vec::new();
    for o in &pb.obs {
        let mut cs = image_cols(o.image);
        cs.extend(lay.point_col + 3 * o.point..lay.point_col + 3 * o.point + 3);
        rows.push(cs.clone());
        rows.push(cs);
    }
    for o in &pb.mobs {
        let mut cs = image_cols(o.image);
        cs.extend(lay.marker_col + 3 * o.point..lay.marker_col + 3 * o.point + 3);
        rows.push(cs.clone());
        rows.push(cs);
    }
    for (m, _, _) in &pb.gcps {
        for k in 0..3 {
            rows.push(vec![lay.marker_col + 3 * m + k]);
        }
    }
    for (a, b, _, _) in &pb.bars {
        let mut cs: Vec<usize> = (lay.marker_col + 3 * a..lay.marker_col + 3 * a + 3).collect();
        cs.extend(lay.marker_col + 3 * b..lay.marker_col + 3 * b + 3);
        rows.push(cs);
    }
    for (i, _, _, flight) in &pb.gnss {
        for k in 0..3 {
            let mut cs = Vec::new();
            if let Some(c0) = lay.image_col[*i] {
                cs.push(c0 + 3 + k);
            }
            if let Some(b0) = lay.bias_col_of(*flight) {
                cs.push(b0 + k);
            }
            rows.push(cs);
        }
    }
    if pb.rolling_shutter {
        for i in 0..pb.centres.len() {
            for k in 0..6 {
                rows.push(vec![lay.rs_col_of(i).unwrap() + k]);
            }
        }
    }
    rows
}

/// The parameter of the column `col` of `lay` moved by `h` (a rotation by `exp(h e_k)` on the
/// left), and its value before the move.
fn nudged(pb: &Problem, params: &Parameters, lay: &Layout, col: usize, h: f64) -> (Parameters, f64) {
    let mut p = params.clone();
    for (i, c0) in lay.image_col.iter().enumerate() {
        if let Some(c0) = c0 {
            if (*c0..c0 + 6).contains(&col) {
                let k = col - c0;
                if k < 3 {
                    let mut w = [0.0; 3];
                    w[k] = h;
                    p.rotations[i] = mat_mul(&exp_so3(w), &p.rotations[i]);
                    return (p, 0.0);
                }
                let before = p.centres[i][k - 3];
                p.centres[i][k - 3] += h;
                return (p, before);
            }
        }
    }
    for (c, cam) in pb.cameras.iter().enumerate() {
        let cols = lay.camera_cols(c);
        if cols.contains(&col) {
            let k = cam.free[col - cols.start];
            let before = p.camera_params[c][k];
            p.camera_params[c][k] += h;
            return (p, before);
        }
    }
    if col >= lay.point_col && col < lay.point_col + 3 * pb.points.len() {
        let (i, k) = ((col - lay.point_col) / 3, (col - lay.point_col) % 3);
        let before = p.points[i][k];
        p.points[i][k] += h;
        return (p, before);
    }
    if col >= lay.marker_col && col < lay.marker_col + 3 * pb.markers.len() {
        let (i, k) = ((col - lay.marker_col) / 3, (col - lay.marker_col) % 3);
        let before = p.markers[i][k];
        p.markers[i][k] += h;
        return (p, before);
    }
    if let Some(r0) = lay.rs_col {
        if col >= r0 && col < r0 + 6 * pb.centres.len() {
            let (i, k) = ((col - r0) / 6, (col - r0) % 6);
            let before = p.rolling_shutter[i][k];
            p.rolling_shutter[i][k] += h;
            return (p, before);
        }
    }
    let (f, k) = ((col - lay.bias_col) / 3, (col - lay.bias_col) % 3);
    let before = p.bias[f][k];
    p.bias[f][k] += h;
    (p, before)
}

/// Root mean square of the reprojection error of the tie points, in pixels.
fn reprojection_rms(pb: &Problem, params: &Parameters) -> f64 {
    let r = evaluate_at(pb, params, Loss::None).residuals;
    let n = pb.obs.len();
    let sum: f64 = r[..2 * n].iter().map(|x| x * x * pb.pixel_sigma * pb.pixel_sigma).sum();
    (sum / n as f64).sqrt()
}

fn max_distance(got: &[V3], want: &[V3]) -> f64 {
    got.iter().zip(want).map(|(g, w)| distance(*g, *w)).fold(0.0, f64::max)
}

fn every_number(s: &Solution) -> Vec<u64> {
    let mut out = Vec::new();
    for r in &s.rotations {
        out.extend(r.iter().flatten().map(|x| x.to_bits()));
    }
    for v in s.centres.iter().chain(&s.points).chain(&s.markers) {
        out.extend(v.iter().map(|x| x.to_bits()));
    }
    for p in &s.camera_params {
        out.extend(p.iter().map(|x| x.to_bits()));
    }
    for rs in s.rolling_shutter.iter().flatten() {
        out.extend(rs.iter().map(|x| x.to_bits()));
    }
    for (_, b) in &s.bias {
        out.extend(b.iter().map(|x| x.to_bits()));
    }
    out.extend([s.cost_initial.to_bits(), s.cost.to_bits(), s.gradient_max.to_bits(), s.iterations as u64]);
    out
}

#[test]
fn evaluate_is_zero_on_exact_observations_and_its_jacobian_agrees_with_central_differences() {
    let mut rng = Rng(1);
    let (mut pb, truth) = block(&mut rng, 600, None);
    // every kind of residual: markers (in the block), 4 control points, 2 bars, GNSS with a bias
    // per strip, rolling shutter, one fixed photo
    pb.gcps = gcps(&truth, 0.002, &[0, 1, 2, 3]);
    let m = &truth.markers;
    pb.bars = vec![(0, 2, distance(m[0], m[2]), 0.001), (1, 3, distance(m[1], m[3]), 0.001)];
    pb.gnss = (0..N_PHOTOS).map(|i| (i, truth.centres[i], [0.02; 3], (i / 6) as i64)).collect();
    pb.gnss_bias = true;
    pb.rolling_shutter = true;
    pb.fixed_images[3] = true;
    pb.check().unwrap();
    let lay = Layout::new(&pb);
    let n_rows = 2 * pb.obs.len() + 2 * pb.mobs.len() + 12 + 2 + 3 * N_PHOTOS + 6 * N_PHOTOS;
    assert_eq!(lay.size, 17 * 6 + 4 + 3 * 600 + 15 + 6 * N_PHOTOS + 9);

    // exact observations: nothing to correct
    let exact = evaluate(&pb, Loss::None);
    assert_eq!(exact.residuals.len(), n_rows);
    assert_eq!(exact.gradient.len(), lay.size);
    let worst = exact.residuals.iter().fold(0.0, |a: f64, r| a.max(r.abs()));
    assert!(worst < 1e-9, "the largest residual of the exact block is {worst}");
    assert!(exact.cost < 1e-18, "cost {}", exact.cost);

    // perturbed, with velocities and biases that are not zero
    perturb(&mut pb, &mut rng, 0.3, 0.01, 0.2);
    pb.cameras[0].params = vec![F * 1.01, F * 1.01, W / 2.0, H / 2.0, 0.0, 0.0, 0.0, 0.0];
    let mut params = Parameters::initial(&pb);
    for (i, rs) in params.rolling_shutter.iter_mut().enumerate() {
        let t = i as f64;
        *rs = [0.3 * t.sin(), 0.1 * t.cos(), 0.05 * t.sin(), 0.002 * t.cos(), 0.003 * t.sin(), 0.001 * t.cos()];
    }
    params.bias = vec![[0.35, -0.2, 0.6], [-0.1, 0.4, 0.2], [0.05, 0.05, -0.3]];
    let ev = evaluate_at(&pb, &params, Loss::None);
    assert_eq!(ev.residuals.len(), n_rows);
    let expected_cost = 0.5 * ev.residuals.iter().map(|r| r * r).sum::<f64>();
    assert!((ev.cost - expected_cost).abs() <= 1e-12 * expected_cost, "cost {} vs {expected_cost}", ev.cost);

    // the structure of the Jacobian is that of _Model.sparsity
    let structure = sparsity(&pb, &lay);
    assert_eq!(structure.len(), n_rows);
    let allowed: HashSet<(usize, usize)> =
        structure.iter().enumerate().flat_map(|(r, cs)| cs.iter().map(move |&c| (r, c))).collect();
    let mut by_column: Vec<Vec<(usize, f64)>> = vec![Vec::new(); lay.size];
    for &(row, col, value) in &ev.jacobian {
        assert!(row < n_rows && col < lay.size, "entry ({row}, {col}) outside the Jacobian");
        if value != 0.0 {
            assert!(allowed.contains(&(row, col)), "entry ({row}, {col}) = {value} outside the structure");
        }
        by_column[col].push((row, value));
    }

    // the gradient is J^T r
    let mut gradient = vec![0.0; lay.size];
    for &(row, col, value) in &ev.jacobian {
        gradient[col] += value * ev.residuals[row];
    }
    for (c, (got, want)) in ev.gradient.iter().zip(&gradient).enumerate() {
        assert!((got - want).abs() <= 1e-9 * want.abs().max(1.0), "gradient[{c}] = {got}, J^T r gives {want}");
    }

    // every column against central differences, step 1e-6 max(1, |x|): entries at least 1 in size to
    // 1e-6 relative; below that, the noise of the differences themselves (about 1e-8) makes a relative
    // check meaningless, so they are checked to 1e-6 absolute. Outside the structure the differences
    // must vanish.
    let mut checked = 0usize;
    for col in 0..lay.size {
        let (_, x) = nudged(&pb, &params, &lay, col, 0.0);
        let h = 1e-6 * x.abs().max(1.0);
        let (plus, _) = nudged(&pb, &params, &lay, col, h);
        let (minus, _) = nudged(&pb, &params, &lay, col, -h);
        let rp = evaluate_at(&pb, &plus, Loss::None).residuals;
        let rm = evaluate_at(&pb, &minus, Loss::None).residuals;
        let mut analytic = vec![0.0; n_rows];
        for &(row, value) in &by_column[col] {
            analytic[row] += value;
        }
        for row in 0..n_rows {
            let want = (rp[row] - rm[row]) / (2.0 * h);
            let got = analytic[row];
            let tolerance = 1e-6 * got.abs().max(want.abs()).max(1.0);
            assert!(
                (got - want).abs() <= tolerance,
                "d residual[{row}] / d column[{col}] = {got}, central differences {want}"
            );
            if got != 0.0 {
                checked += 1;
            }
        }
    }
    assert!(checked > 10 * n_rows, "only {checked} non-zero entries checked");
}

#[test]
fn a_point_seen_by_two_fixed_cameras_is_triangulated() {
    let truth = [1.0, 2.0, 0.3];
    let rotations = vec![look_down(0.0), look_down(0.1)];
    let centres = vec![[-5.0, 0.0, 30.0], [5.0, 1.0, 30.0]];
    let obs: Vec<Obs> = (0..2)
        .map(|i| Obs { image: i, point: 0, xy: pixel(&rotations[i], centres[i], [0.0; 3], truth).unwrap() })
        .collect();
    let pb = Problem {
        rotations,
        centres,
        image_camera: vec![0, 0],
        cameras: vec![camera(&[])],
        points: vec![[1.3, 1.6, 0.3]],
        obs,
        fixed_images: vec![true, true],
        ..Problem::default()
    };
    assert!((distance(pb.points[0], truth) - 0.5).abs() < 1e-12);
    let sol = solve(&pb, &Options::default(), &mut |_, _| true).unwrap();
    assert!(distance(sol.points[0], truth) < 1e-9, "point {:?}", sol.points[0]);
    assert!(sol.cost < 1e-20, "cost {}", sol.cost);
    assert!(sol.iterations <= 10, "{} iterations", sol.iterations);
    assert_ne!(sol.termination, Termination::MaxIterations);
    assert!(sol.cost_initial > 1.0);
    assert_eq!((sol.rotations, sol.centres), (pb.rotations.clone(), pb.centres.clone()));
}

#[test]
fn the_block_comes_back_to_its_control_points() {
    let (pb, truth) = control_point_block(1);
    let before = reprojection_rms(&pb, &Parameters::initial(&pb));
    assert!(before > 10.0, "the perturbed block starts at {before} px");
    let sol = solve(&pb, &Options::default(), &mut |_, _| true).unwrap();
    assert!(matches!(sol.termination, Termination::Function | Termination::Gradient | Termination::Parameters));
    let after = reprojection_rms(&pb, &sol.parameters());
    assert!(after < 0.01, "reprojection RMS {after} px after {} iterations", sol.iterations);
    assert!(max_distance(&sol.centres, &truth.centres) < 0.005);
    assert!(max_distance(&sol.points, &truth.points) < 0.01);
    let p = &sol.camera_params[0];
    assert!((p[0] - F).abs() < 0.5 && (p[1] - F).abs() < 0.5, "focal lengths {} {}", p[0], p[1]);
    assert!((p[4] + 0.02).abs() < 1e-3, "k1 {}", p[4]);
    // a marker that is not a control point comes back too
    assert!(distance(sol.markers[4], truth.markers[4]) < 0.005);
    assert!(sol.cost < sol.cost_initial * 1e-6);
}

#[test]
fn a_point_behind_every_camera_stays_and_the_rest_converges() {
    let (mut pb, truth) = control_point_block(2);
    // the point with the most observations, moved 30 m above the cameras: behind every photo
    let mut seen = vec![0usize; pb.points.len()];
    for o in &pb.obs {
        seen[o.point] += 1;
    }
    let moved = (0..pb.points.len()).max_by_key(|&p| seen[p]).unwrap();
    assert!(seen[moved] >= 3);
    pb.points[moved][2] = 60.0;
    let start = pb.points[moved];
    let sol = solve(&pb, &Options::default(), &mut |_, _| true).unwrap();
    for v in sol.centres.iter().chain(&sol.points).chain(&sol.markers) {
        assert!(v.iter().all(|x| x.is_finite()), "{v:?}");
    }
    assert!(sol.rotations.iter().flatten().flatten().all(|x| x.is_finite()));
    assert!(sol.camera_params[0].iter().all(|x| x.is_finite()));
    assert!(sol.cost.is_finite() && sol.gradient_max.is_finite());
    assert_eq!(sol.points[moved], start, "the point behind the cameras moved");
    let rest = (0..pb.points.len())
        .filter(|&p| p != moved)
        .map(|p| distance(sol.points[p], truth.points[p]))
        .fold(0.0, f64::max);
    assert!(rest < 0.01, "the other points are {rest} m from the truth");
    let centres = max_distance(&sol.centres, &truth.centres);
    assert!(
        centres < 0.005,
        "centres {centres} m off after {} iterations ({:?}), cost {} from {}, gradient {}",
        sol.iterations,
        sol.termination,
        sol.cost,
        sol.cost_initial,
        sol.gradient_max
    );
}

#[test]
fn a_problem_without_a_gauge_is_still_solved() {
    let mut rng = Rng(3);
    let (mut pb, _) = block(&mut rng, 600, None);
    perturb(&mut pb, &mut rng, 0.3, 0.01, 0.2);
    assert!(pb.gcps.is_empty() && pb.gnss.is_empty() && pb.fixed_images.iter().all(|f| !f));
    let sol = solve(&pb, &Options::default(), &mut |_, _| true).unwrap();
    assert!(sol.cost < 1e-12 * sol.cost_initial, "cost {} from {}", sol.cost, sol.cost_initial);
    assert!(sol.cost.is_finite() && sol.gradient_max.is_finite());
}

#[test]
fn huber_and_cauchy_cost_as_scipy_does_and_resist_the_moved_observations() {
    let mut rng = Rng(4);
    let (mut pb, truth) = block(&mut rng, 600, None);
    // tie points only, the camera calibrated, two photos holding the place and the size
    pb.markers.clear();
    pb.mobs.clear();
    pb.cameras[0].free.clear();
    pb.fixed_images[0] = true;
    pb.fixed_images[17] = true;
    // 0.3 px of noise everywhere, every fiftieth observation moved 40 px
    let mut moved_points = HashSet::new();
    for (j, o) in pb.obs.iter_mut().enumerate() {
        o.xy = [o.xy[0] + 0.3 * rng.normal(), o.xy[1] + 0.3 * rng.normal()];
        if j % 50 == 0 {
            let angle = rng.uniform_in(0.0, std::f64::consts::TAU);
            o.xy = [o.xy[0] + 40.0 * angle.cos(), o.xy[1] + 40.0 * angle.sin()];
            moved_points.insert(o.point);
        }
    }
    assert!(moved_points.len() > 50);

    let rho = |loss: Loss, s: f64| match loss {
        Loss::None => s,
        Loss::Huber(a) => {
            if s <= a * a {
                s
            } else {
                2.0 * a * s.sqrt() - a * a
            }
        }
        Loss::Cauchy(a) => a * a * (1.0 + s / (a * a)).ln(),
    };
    let mut error = Vec::new();
    for loss in [Loss::None, Loss::Huber(1.0), Loss::Cauchy(1.0)] {
        let ev = evaluate(&pb, loss);
        let want = 0.5 * ev.residuals.iter().map(|r| rho(loss, r * r)).sum::<f64>();
        assert!((ev.cost - want).abs() <= 1e-12 * want, "{loss:?}: cost {} vs {want}", ev.cost);
        let sol = solve(&pb, &Options { loss, ..Options::default() }, &mut |_, _| true).unwrap();
        assert_ne!(sol.termination, Termination::MaxIterations, "{loss:?} did not converge");
        assert_eq!(sol.cost_initial, ev.cost);
        assert!(sol.cost < ev.cost);
        let at_solution = evaluate_at(&pb, &sol.parameters(), loss);
        let want = 0.5 * at_solution.residuals.iter().map(|r| rho(loss, r * r)).sum::<f64>();
        assert!((sol.cost - want).abs() <= 1e-9 * want, "{loss:?}: final cost {} vs {want}", sol.cost);
        let mean = moved_points.iter().map(|&p| distance(sol.points[p], truth.points[p])).sum::<f64>()
            / moved_points.len() as f64;
        error.push(mean);
    }
    let (plain, huber, cauchy) = (error[0], error[1], error[2]);
    assert!(huber < 0.5 * plain, "Huber leaves the moved points at {huber} m, the squared loss at {plain}");
    assert!(cauchy < 0.5 * plain, "Cauchy leaves the moved points at {cauchy} m, the squared loss at {plain}");
}

#[test]
fn two_runs_give_the_same_bits() {
    let (pb, _) = control_point_block(5);
    let first = solve(&pb, &Options::default(), &mut |_, _| true).unwrap();
    let second = solve(&pb, &Options::default(), &mut |_, _| true).unwrap();
    assert_eq!(every_number(&first), every_number(&second));
    assert_eq!((first.termination, first.linear_solver), (second.termination, second.linear_solver));
}

#[test]
fn observe_false_cancels() {
    let (pb, _) = control_point_block(6);
    let mut seen = Vec::new();
    let result = solve(&pb, &Options::default(), &mut |iteration, cost| {
        seen.push((iteration, cost));
        iteration < 2
    });
    assert_eq!(result, Err("cancelled".to_string()));
    assert_eq!(seen.len(), 2);
    assert_eq!((seen[0].0, seen[1].0), (1, 2));
    assert!(seen.iter().all(|(_, cost)| cost.is_finite()));
}

/// The control-point block with 0.3 px of noise on every tie observation and `wrong` wrong tracks:
/// points whose every observation is moved 40 px in a random direction.
fn wrong_track_block(seed: u64, wrong: usize) -> (Problem, Truth, Vec<usize>) {
    let mut rng = Rng(seed);
    let (mut pb, truth) = block(&mut rng, 600, None);
    pb.gcps = gcps(&truth, 0.002, &[0, 1, 2, 3]);
    for o in pb.obs.iter_mut() {
        o.xy = [o.xy[0] + 0.3 * rng.normal(), o.xy[1] + 0.3 * rng.normal()];
    }
    let mut seen = vec![0usize; pb.points.len()];
    for o in &pb.obs {
        seen[o.point] += 1;
    }
    let wrong: Vec<usize> = (0..pb.points.len()).filter(|&p| seen[p] >= 4).step_by(23).take(wrong).collect();
    for o in pb.obs.iter_mut().filter(|o| wrong.contains(&o.point)) {
        let angle = rng.uniform_in(0.0, std::f64::consts::TAU);
        o.xy = [o.xy[0] + 40.0 * angle.cos(), o.xy[1] + 40.0 * angle.sin()];
    }
    (pb, truth, wrong)
}

#[test]
fn wrong_tracks_do_not_stall_the_robust_losses() {
    // twelve points whose observations are all wrong: the robust losses must still converge in a
    // bounded number of iterations and rejected steps, and leave the other points where they belong
    let (pb, truth, wrong) = wrong_track_block(7, 12);
    assert_eq!(wrong.len(), 12);
    for loss in [Loss::Huber(1.0), Loss::Cauchy(1.0)] {
        let mut costs = Vec::new();
        let sol = solve(&pb, &Options { loss, ..Options::default() }, &mut |_, cost| {
            costs.push(cost);
            true
        })
        .unwrap();
        // with the first-order weights and without the inner iterations, Huber ran to the limit
        // (100 iterations, 29 rejected) and Cauchy took 73 (20 rejected); now 3 / 0 and 25 / 5
        let rejected = costs.windows(2).filter(|w| w[0] == w[1]).count();
        assert!(
            sol.iterations <= 40 && rejected <= 8,
            "{loss:?}: {} iterations, {rejected} rejected steps, {:?}",
            sol.iterations,
            sol.termination
        );
        assert_ne!(sol.termination, Termination::MaxIterations);
        let rest = (0..pb.points.len())
            .filter(|p| !wrong.contains(p))
            .map(|p| distance(sol.points[p], truth.points[p]))
            .fold(0.0, f64::max);
        assert!(rest < 0.3, "{loss:?}: the right points are {rest} m from the truth");
        assert!(max_distance(&sol.centres, &truth.centres) < 0.3);
        assert!((sol.camera_params[0][0] - F).abs() < 5.0, "{loss:?}: f {}", sol.camera_params[0][0]);
    }
}

#[test]
fn a_point_seen_once_stays_on_its_ray_under_a_robust_loss() {
    // the exact block with control points and a calibrated camera, plus six points each seen by one
    // photo only, with 0.3 px on their observation: nothing in the problem can place such a point
    // along its ray, so the adjustment leaves it where it is, under every loss, and the rest of the
    // block stays exact
    let mut rng = Rng(8);
    let (mut pb, truth) = block(&mut rng, 600, None);
    pb.gcps = gcps(&truth, 0.002, &[0, 1, 2, 3]);
    pb.cameras[0].free.clear();
    let mut once = Vec::new();
    for (k, i) in [1usize, 4, 7, 10, 13, 16].into_iter().enumerate() {
        let c = pb.centres[i];
        let world = [c[0] + 0.8 - 0.3 * k as f64, c[1] - 0.6 + 0.2 * k as f64, c[2] - 30.0 + 0.4 * (k % 3) as f64];
        let xy = pixel(&pb.rotations[i], c, [0.0; 3], world).unwrap();
        once.push((pb.points.len(), i));
        pb.points.push(world);
        pb.obs.push(Obs { image: i, point: pb.points.len() - 1, xy: [xy[0] + 0.3, xy[1] - 0.2] });
    }
    pb.check().unwrap();
    for loss in [Loss::None, Loss::Huber(1.0), Loss::Cauchy(1.0)] {
        let sol = solve(&pb, &Options { loss, ..Options::default() }, &mut |_, _| true).unwrap();
        for &(p, i) in &once {
            let (before, after) = (distance(pb.points[p], pb.centres[i]), distance(sol.points[p], sol.centres[i]));
            let moved = format!("{loss:?}: point {p} moved from {before} m to {after} m from its camera");
            assert_eq!(sol.points[p], pb.points[p], "{moved}");
        }
        // the six residuals that remain bend the block by millimetres: the least-squares answer
        assert!(max_distance(&sol.centres, &truth.centres) < 1e-2, "{loss:?}");
        assert!(max_distance(&sol.points[..600], &truth.points) < 1e-2, "{loss:?}");
    }
}
