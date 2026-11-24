//! The problem of the bundle adjustment as `engine/bundle.py: Problem` states it, where each of
//! its unknowns sits in the vector of parameters (`Layout`, the columns of `_Layout`) and the
//! parameters themselves (`Parameters`: the start of a problem, the result of a solution).

use std::ops::Range;

use crate::camera::Model;

/// Accuracy of a measured marker projection, pixels (`bundle.MARKER_PIXEL_ACCURACY`).
pub const MARKER_PIXEL_ACCURACY: f64 = 0.5;
/// Prior of the linear velocity of the rolling shutter, units of the problem per frame read.
pub const ROLLING_SHUTTER_SIGMA_M: f64 = 1.0;
/// Prior of the angular velocity of the rolling shutter, radians per frame read.
pub const ROLLING_SHUTTER_SIGMA_RAD: f64 = 0.005;

/// A camera (a sensor) of the problem: one of the models of `camera.rs`, its parameters in
/// COLMAP's order, the size of its photos and which of its parameters are adjusted
/// (`free_intrinsics` of the Python problem).
#[derive(Clone, Debug, PartialEq)]
pub struct CameraBlock {
    pub model: Model,
    pub params: Vec<f64>,
    pub width: f64,
    pub height: f64,
    pub free: Vec<usize>,
}

/// An observation: the pixel `xy` (COLMAP's convention, the centre of the first pixel at
/// 0.5, 0.5) of the tie point `point` in the photo `image`. In `Problem::mobs`, `point` is the
/// index of a marker.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Obs {
    pub image: usize,
    pub point: usize,
    pub xy: [f64; 2],
}

/// A block to adjust, in the internal frame of the chunk: the fields of `bundle.Problem` with
/// the same meaning and units. `rotations` are world -> camera, `centres` the camera centres,
/// `image_camera[i]` the camera of the photo `i`; `gcps` are `(marker, target, sigma)`, `bars`
/// `(marker a, marker b, distance, sigma)`, `gnss` `(image, target, sigma per axis, flight)`.
/// `rs_sigma` and `rs_sigma_angular` hold one value for every photo or one per photo. With
/// `gnss_bias`, every flight of `gnss` has a bias but `reference_flight`, whose bias is held at zero
/// (the frame of the positions is that flight's: without control points the biases of all the
/// flights and a translation of the block would trade off).
#[derive(Clone, Debug, PartialEq)]
pub struct Problem {
    pub rotations: Vec<[[f64; 3]; 3]>,
    pub centres: Vec<[f64; 3]>,
    pub image_camera: Vec<usize>,
    pub cameras: Vec<CameraBlock>,
    pub points: Vec<[f64; 3]>,
    pub obs: Vec<Obs>,
    pub markers: Vec<[f64; 3]>,
    pub mobs: Vec<Obs>,
    pub gcps: Vec<(usize, [f64; 3], [f64; 3])>,
    pub bars: Vec<(usize, usize, f64, f64)>,
    pub gnss: Vec<(usize, [f64; 3], [f64; 3], i64)>,
    pub fixed_images: Vec<bool>,
    pub rolling_shutter: bool,
    pub gnss_bias: bool,
    pub reference_flight: Option<i64>,
    pub pixel_sigma: f64,
    pub marker_sigma: f64,
    pub rs_sigma: Vec<f64>,
    pub rs_sigma_angular: Vec<f64>,
}

impl Default for Problem {
    /// An empty problem with the defaults of the Python one (a pixel of accuracy for the tie
    /// points, half a pixel for the markers, the priors of the rolling shutter).
    fn default() -> Self {
        Problem {
            rotations: Vec::new(),
            centres: Vec::new(),
            image_camera: Vec::new(),
            cameras: Vec::new(),
            points: Vec::new(),
            obs: Vec::new(),
            markers: Vec::new(),
            mobs: Vec::new(),
            gcps: Vec::new(),
            bars: Vec::new(),
            gnss: Vec::new(),
            fixed_images: Vec::new(),
            rolling_shutter: false,
            gnss_bias: false,
            reference_flight: None,
            pixel_sigma: 1.0,
            marker_sigma: MARKER_PIXEL_ACCURACY,
            rs_sigma: vec![ROLLING_SHUTTER_SIGMA_M],
            rs_sigma_angular: vec![ROLLING_SHUTTER_SIGMA_RAD],
        }
    }
}

fn finite(values: impl IntoIterator<Item = f64>) -> bool {
    values.into_iter().all(f64::is_finite)
}

fn positive(values: impl IntoIterator<Item = f64>) -> bool {
    values.into_iter().all(|x| x.is_finite() && x > 0.0)
}

impl Problem {
    /// The problem is consistent: one entry per photo in every array of photos, indices inside
    /// their ranges, enough parameters for each model, finite numbers, positive accuracies.
    pub fn check(&self) -> Result<(), String> {
        let n = self.rotations.len();
        if self.centres.len() != n || self.image_camera.len() != n || self.fixed_images.len() != n {
            return Err(format!(
                "{n} rotations, {} centres, {} camera indices and {} fixed flags: one of each per photo expected",
                self.centres.len(),
                self.image_camera.len(),
                self.fixed_images.len()
            ));
        }
        let rotations = self.rotations.iter().flatten().flatten().copied();
        if !finite(rotations) || !finite(self.centres.iter().flatten().copied()) {
            return Err("a rotation or a centre is not finite".into());
        }
        for (i, &c) in self.image_camera.iter().enumerate() {
            if c >= self.cameras.len() {
                return Err(format!("photo {i} uses camera {c}, {} cameras given", self.cameras.len()));
            }
        }
        for (c, cam) in self.cameras.iter().enumerate() {
            let count = cam.model.parameter_count();
            if cam.params.len() < count {
                return Err(format!("camera {c}: {count} parameters expected, {} given", cam.params.len()));
            }
            if !finite(cam.params.iter().copied()) || !positive([cam.width, cam.height]) {
                return Err(format!("camera {c}: parameters or size not finite and positive"));
            }
            let mut seen = vec![false; count];
            for &k in &cam.free {
                if k >= count {
                    return Err(format!("camera {c}: free parameter {k} of the {count} of its model"));
                }
                if seen[k] {
                    return Err(format!("camera {c}: free parameter {k} given twice"));
                }
                seen[k] = true;
            }
        }
        if !finite(self.points.iter().flatten().copied()) || !finite(self.markers.iter().flatten().copied()) {
            return Err("a point or a marker is not finite".into());
        }
        for (what, list, count) in [("tie", &self.obs, self.points.len()), ("marker", &self.mobs, self.markers.len())] {
            for (j, o) in list.iter().enumerate() {
                if o.image >= n || o.point >= count || !finite(o.xy) {
                    return Err(format!(
                        "{what} observation {j}: photo {} of {n}, index {} of {count}, pixel {:?}",
                        o.image, o.point, o.xy
                    ));
                }
            }
        }
        for (j, (m, target, sigma)) in self.gcps.iter().enumerate() {
            if *m >= self.markers.len() || !finite(*target) || !positive(*sigma) {
                return Err(format!("control point {j}: marker {m} of {}, {target:?} +- {sigma:?}", self.markers.len()));
            }
        }
        for (j, (a, b, distance, sigma)) in self.bars.iter().enumerate() {
            let n_markers = self.markers.len();
            if *a >= n_markers || *b >= n_markers || a == b || !distance.is_finite() || !positive([*sigma]) {
                return Err(format!("scale bar {j}: markers {a} and {b} of {n_markers}, {distance} +- {sigma}"));
            }
        }
        for (j, (i, target, sigma, _)) in self.gnss.iter().enumerate() {
            if *i >= n || !finite(*target) || !positive(*sigma) {
                return Err(format!("GNSS position {j}: photo {i} of {n}, {target:?} +- {sigma:?}"));
            }
        }
        if !positive([self.pixel_sigma, self.marker_sigma]) {
            let (p, m) = (self.pixel_sigma, self.marker_sigma);
            return Err(format!("pixel_sigma {p} and marker_sigma {m} must be positive"));
        }
        if self.rolling_shutter {
            for (name, sigma) in [("rs_sigma", &self.rs_sigma), ("rs_sigma_angular", &self.rs_sigma_angular)] {
                if !(sigma.len() == 1 || sigma.len() == n) || !positive(sigma.iter().copied()) {
                    return Err(format!(
                        "{name}: one positive value, or one per photo ({n}), expected, {sigma:?} given"
                    ));
                }
            }
        }
        Ok(())
    }

    /// The prior sigmas of the rolling shutter of the photo `i`: linear and angular.
    pub(crate) fn rs_sigmas(&self, i: usize) -> (f64, f64) {
        let pick = |sigma: &[f64]| if sigma.len() == 1 { sigma[0] } else { sigma[i] };
        (pick(&self.rs_sigma), pick(&self.rs_sigma_angular))
    }
}

/// Where each unknown sits in the vector of parameters: the order of `_Layout` of bundle.py.
/// Six columns per free photo (the rotation increment, then the centre), the free intrinsics of
/// each camera, three per point, three per marker, six per photo (a fixed photo too) with the
/// rolling shutter, three per flight (sorted, but the reference flight) with the GNSS bias.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Layout {
    /// The photos that are not fixed, in their order.
    pub free_images: Vec<usize>,
    /// First column of each photo, `None` for a fixed one.
    pub image_col: Vec<Option<usize>>,
    /// First column of the free intrinsics of each camera (`cameras[c].free.len()` of them).
    pub camera_col: Vec<usize>,
    pub point_col: usize,
    pub marker_col: usize,
    /// First column of the rolling shutter, photo `i` at `rs_col + 6 i`; `None` without it.
    pub rs_col: Option<usize>,
    /// The flights with a bias, sorted; the bias of `flights[f]` at `bias_col + 3 f`.
    pub flights: Vec<i64>,
    pub bias_col: usize,
    pub size: usize,
}

impl Layout {
    pub fn new(pb: &Problem) -> Layout {
        let n = pb.centres.len();
        let mut pos = 0;
        let mut image_col = vec![None; n];
        let mut free_images = Vec::new();
        for (i, fixed) in pb.fixed_images.iter().enumerate() {
            if !fixed {
                image_col[i] = Some(pos);
                pos += 6;
                free_images.push(i);
            }
        }
        let mut camera_col = Vec::with_capacity(pb.cameras.len());
        for cam in &pb.cameras {
            camera_col.push(pos);
            pos += cam.free.len();
        }
        let point_col = pos;
        pos += 3 * pb.points.len();
        let marker_col = pos;
        pos += 3 * pb.markers.len();
        let rs_col = pb.rolling_shutter.then(|| {
            let col = pos;
            pos += 6 * n;
            col
        });
        let mut flights: Vec<i64> = if pb.gnss_bias {
            pb.gnss.iter().map(|g| g.3).filter(|f| Some(*f) != pb.reference_flight).collect()
        } else {
            Vec::new()
        };
        flights.sort_unstable();
        flights.dedup();
        let bias_col = pos;
        pos += 3 * flights.len();
        Layout { free_images, image_col, camera_col, point_col, marker_col, rs_col, flights, bias_col, size: pos }
    }

    /// The columns of the free intrinsics of the camera `c`.
    pub fn camera_cols(&self, c: usize) -> Range<usize> {
        let next = self.camera_col.get(c + 1).copied().unwrap_or(self.point_col);
        self.camera_col[c]..next
    }

    /// First of the six rolling-shutter columns of the photo `i`.
    pub fn rs_col_of(&self, i: usize) -> Option<usize> {
        self.rs_col.map(|col| col + 6 * i)
    }

    /// Position of `flight` in `flights`, if it has a bias.
    pub fn flight_index(&self, flight: i64) -> Option<usize> {
        self.flights.binary_search(&flight).ok()
    }

    /// First of the three bias columns of `flight`, if the flight has one.
    pub fn bias_col_of(&self, flight: i64) -> Option<usize> {
        self.flight_index(flight).map(|f| self.bias_col + 3 * f)
    }
}

/// The values of the unknowns of a problem (and of what is held fixed): the state the residuals
/// are evaluated at. `rolling_shutter` has one entry per photo when the problem adjusts it (the
/// linear, then the angular velocity), `bias` one per flight of `Layout::flights`.
#[derive(Clone, Debug, PartialEq)]
pub struct Parameters {
    pub rotations: Vec<[[f64; 3]; 3]>,
    pub centres: Vec<[f64; 3]>,
    pub camera_params: Vec<Vec<f64>>,
    pub points: Vec<[f64; 3]>,
    pub markers: Vec<[f64; 3]>,
    pub rolling_shutter: Vec<[f64; 6]>,
    pub bias: Vec<[f64; 3]>,
}

impl Parameters {
    /// The start of the adjustment: the problem as given, still cameras, no bias.
    pub fn initial(pb: &Problem) -> Parameters {
        let flights = if pb.gnss_bias { Layout::new(pb).flights.len() } else { 0 };
        Parameters {
            rotations: pb.rotations.clone(),
            centres: pb.centres.clone(),
            camera_params: pb.cameras.iter().map(|c| c.params.clone()).collect(),
            points: pb.points.clone(),
            markers: pb.markers.clone(),
            rolling_shutter: if pb.rolling_shutter { vec![[0.0; 6]; pb.centres.len()] } else { Vec::new() },
            bias: vec![[0.0; 3]; flights],
        }
    }

    /// The parameters are those of `pb`: one entry per photo, camera, point, marker, and the
    /// rolling shutter and biases the layout asks for.
    pub(crate) fn fits(&self, pb: &Problem, lay: &Layout) -> Result<(), String> {
        let n = pb.centres.len();
        let rs = if pb.rolling_shutter { n } else { 0 };
        if self.rotations.len() != n
            || self.centres.len() != n
            || self.camera_params.len() != pb.cameras.len()
            || self.camera_params.iter().zip(&pb.cameras).any(|(p, c)| p.len() != c.params.len())
            || self.points.len() != pb.points.len()
            || self.markers.len() != pb.markers.len()
            || self.rolling_shutter.len() != rs
            || self.bias.len() != lay.flights.len()
        {
            return Err("the parameters do not fit the problem".into());
        }
        Ok(())
    }

    /// The parameters moved by `delta`, a step in the columns of `lay`: the rotations by
    /// `exp(delta) R` on the left, everything else added.
    pub fn step(&self, pb: &Problem, lay: &Layout, delta: &[f64]) -> Parameters {
        assert_eq!(delta.len(), lay.size, "a step has one entry per column of the layout");
        let mut out = self.clone();
        for &i in &lay.free_images {
            let c0 = lay.image_col[i].expect("a free photo has columns");
            out.rotations[i] = compose(&exp_rotation([delta[c0], delta[c0 + 1], delta[c0 + 2]]), &self.rotations[i]);
            for k in 0..3 {
                out.centres[i][k] += delta[c0 + 3 + k];
            }
        }
        for (c, cam) in pb.cameras.iter().enumerate() {
            for (j, &k) in cam.free.iter().enumerate() {
                out.camera_params[c][k] += delta[lay.camera_col[c] + j];
            }
        }
        for (p, point) in out.points.iter_mut().enumerate() {
            for k in 0..3 {
                point[k] += delta[lay.point_col + 3 * p + k];
            }
        }
        for (m, marker) in out.markers.iter_mut().enumerate() {
            for k in 0..3 {
                marker[k] += delta[lay.marker_col + 3 * m + k];
            }
        }
        if let Some(r0) = lay.rs_col {
            for (i, rs) in out.rolling_shutter.iter_mut().enumerate() {
                for k in 0..6 {
                    rs[k] += delta[r0 + 6 * i + k];
                }
            }
        }
        for (f, bias) in out.bias.iter_mut().enumerate() {
            for k in 0..3 {
                bias[k] += delta[lay.bias_col + 3 * f + k];
            }
        }
        out
    }

    /// Euclidean norm of the vector of parameters in the columns of `lay` (the rotation
    /// increments are zero), the scale of the parameter tolerance.
    pub(crate) fn norm(&self, pb: &Problem, lay: &Layout) -> f64 {
        let mut sum = 0.0;
        for &i in &lay.free_images {
            sum += self.centres[i].iter().map(|x| x * x).sum::<f64>();
        }
        for (c, cam) in pb.cameras.iter().enumerate() {
            sum += cam.free.iter().map(|&k| self.camera_params[c][k].powi(2)).sum::<f64>();
        }
        for v in self.points.iter().chain(&self.markers).chain(&self.bias) {
            sum += v.iter().map(|x| x * x).sum::<f64>();
        }
        for rs in &self.rolling_shutter {
            sum += rs.iter().map(|x| x * x).sum::<f64>();
        }
        sum.sqrt()
    }
}

/// The rotation matrix of the rotation vector `w` (Rodrigues' formula).
pub fn exp_rotation(w: [f64; 3]) -> [[f64; 3]; 3] {
    let theta2 = w[0] * w[0] + w[1] * w[1] + w[2] * w[2];
    let theta = theta2.sqrt();
    // sin(theta) / theta and (1 - cos(theta)) / theta^2, by their series near zero
    let (a, b) = if theta < 1e-6 {
        (1.0 - theta2 / 6.0, 0.5 - theta2 / 24.0)
    } else {
        (theta.sin() / theta, (1.0 - theta.cos()) / theta2)
    };
    let [x, y, z] = w;
    [
        [1.0 + b * (-y * y - z * z), b * x * y - a * z, b * x * z + a * y],
        [b * x * y + a * z, 1.0 + b * (-x * x - z * z), b * y * z - a * x],
        [b * x * z - a * y, b * y * z + a * x, 1.0 + b * (-x * x - y * y)],
    ]
}

/// The product `a b` of two rotation matrices.
pub fn compose(a: &[[f64; 3]; 3], b: &[[f64; 3]; 3]) -> [[f64; 3]; 3] {
    let mut out = [[0.0; 3]; 3];
    for (i, row) in out.iter_mut().enumerate() {
        for (j, x) in row.iter_mut().enumerate() {
            *x = a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j];
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn camera(model: Model, params: &[f64], free: &[usize]) -> CameraBlock {
        CameraBlock { model, params: params.to_vec(), width: 1200.0, height: 900.0, free: free.to_vec() }
    }

    /// Three photos (the second fixed) of two cameras, four points, two markers, the rolling
    /// shutter and the biases of the flights 3 and 1.
    fn small() -> Problem {
        let eye = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];
        Problem {
            rotations: vec![eye; 3],
            centres: vec![[0.0, 0.0, 10.0], [1.0, 0.0, 10.0], [2.0, 0.0, 10.0]],
            image_camera: vec![0, 1, 0],
            cameras: vec![
                camera(Model::OpenCv, &[1000.0, 1000.0, 600.0, 450.0, 0.0, 0.0, 0.0, 0.0], &[0, 1, 4, 5]),
                camera(Model::SimpleRadial, &[900.0, 600.0, 450.0, 0.0], &[]),
            ],
            points: vec![[0.0; 3]; 4],
            obs: vec![Obs { image: 0, point: 3, xy: [100.0, 100.0] }],
            markers: vec![[0.0; 3]; 2],
            mobs: vec![Obs { image: 2, point: 1, xy: [200.0, 300.0] }],
            gcps: vec![(1, [0.0; 3], [0.01; 3])],
            bars: vec![(0, 1, 1.0, 0.001)],
            gnss: vec![(0, [0.0; 3], [0.02; 3], 3), (1, [0.0; 3], [0.02; 3], 1), (2, [0.0; 3], [0.02; 3], 3)],
            fixed_images: vec![false, true, false],
            rolling_shutter: true,
            gnss_bias: true,
            ..Problem::default()
        }
    }

    #[test]
    fn the_columns_follow_the_python_layout() {
        let pb = small();
        pb.check().unwrap();
        let lay = Layout::new(&pb);
        assert_eq!(lay.free_images, vec![0, 2]);
        assert_eq!(lay.image_col, vec![Some(0), None, Some(6)]);
        assert_eq!(lay.camera_col, vec![12, 16]);
        assert_eq!((lay.camera_cols(0), lay.camera_cols(1)), (12..16, 16..16));
        assert_eq!((lay.point_col, lay.marker_col), (16, 28));
        // six rolling-shutter columns per photo, the fixed one too
        assert_eq!(lay.rs_col, Some(34));
        assert_eq!((lay.rs_col_of(0), lay.rs_col_of(1), lay.rs_col_of(2)), (Some(34), Some(40), Some(46)));
        assert_eq!((lay.flights.clone(), lay.bias_col), (vec![1, 3], 52));
        assert_eq!((lay.bias_col_of(1), lay.bias_col_of(3), lay.bias_col_of(2)), (Some(52), Some(55), None));
        assert_eq!(lay.size, 58);

        // the reference flight has no bias: its positions are the frame
        let held = Problem { reference_flight: Some(1), ..pb.clone() };
        let lay = Layout::new(&held);
        let got = (lay.flights.clone(), lay.bias_col_of(1), lay.bias_col_of(3), lay.size);
        assert_eq!(got, (vec![3], None, Some(52), 55));
        assert_eq!(Parameters::initial(&held).bias.len(), 1);

        // without the rolling shutter and the biases those columns are not there
        let plain = Problem { rolling_shutter: false, gnss_bias: false, ..pb };
        let lay = Layout::new(&plain);
        assert_eq!((lay.rs_col, lay.rs_col_of(0), lay.flights.len(), lay.size), (None, None, 0, 34));
        assert_eq!(lay.bias_col_of(3), None);
    }

    #[test]
    fn the_initial_parameters_and_a_step() {
        let pb = small();
        let lay = Layout::new(&pb);
        let start = Parameters::initial(&pb);
        start.fits(&pb, &lay).unwrap();
        assert_eq!(start.rolling_shutter, vec![[0.0; 6]; 3]);
        assert_eq!(start.bias, vec![[0.0; 3]; 2]);
        assert!((start.norm(&pb, &lay) - (2.0f64 * 100.0 + 4.0 + 1e6 + 1e6).sqrt()).abs() < 1e-9);
        let mut delta = vec![0.0; lay.size];
        delta[2] = 0.1; // a turn of the first photo about its z axis
        delta[6 + 4] = -0.5; // the y of the centre of the third photo
        delta[13] = 2.0; // the second free intrinsic (fy) of the first camera
        delta[lay.point_col + 3 * 3 + 2] = 0.25;
        delta[lay.marker_col + 1] = -0.25;
        delta[lay.rs_col_of(1).unwrap() + 5] = 0.001;
        delta[lay.bias_col_of(3).unwrap()] = 0.3;
        let moved = start.step(&pb, &lay, &delta);
        let r = moved.rotations[0];
        assert!((r[0][0] - 0.1f64.cos()).abs() < 1e-15 && (r[0][1] + 0.1f64.sin()).abs() < 1e-15);
        assert_eq!(moved.rotations[1], start.rotations[1]);
        assert_eq!(moved.centres[2], [2.0, -0.5, 10.0]);
        assert_eq!(moved.camera_params[0][1], 1002.0);
        assert_eq!(moved.camera_params[1], start.camera_params[1]);
        assert_eq!(moved.points[3], [0.0, 0.0, 0.25]);
        assert_eq!(moved.markers[0], [0.0, -0.25, 0.0]);
        assert_eq!(moved.rolling_shutter[1][5], 0.001);
        assert_eq!(moved.bias, vec![[0.0; 3], [0.3, 0.0, 0.0]]);
    }

    #[test]
    fn exp_of_a_rotation_vector() {
        let r = exp_rotation([0.0, 0.0, std::f64::consts::FRAC_PI_2]);
        for (got, want) in r.iter().flatten().zip([0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]) {
            assert!((got - want).abs() < 1e-15, "{r:?}");
        }
        // small angles by the series, orthonormal to round-off
        let r = exp_rotation([1e-9, -2e-9, 3e-9]);
        let rrt = compose(&r, &[[r[0][0], r[1][0], r[2][0]], [r[0][1], r[1][1], r[2][1]], [r[0][2], r[1][2], r[2][2]]]);
        for i in 0..3 {
            for j in 0..3 {
                assert!((rrt[i][j] - if i == j { 1.0 } else { 0.0 }).abs() < 1e-15);
            }
        }
        // -a z + b x y and a z + b x y, the second-order term 1e-18, to the resolution of 3e-9
        assert!((r[0][1] + 3e-9 + 1e-18).abs() < 1e-23 && (r[1][0] - 3e-9 + 1e-18).abs() < 1e-23, "{r:?}");
    }

    #[test]
    fn check_refuses_what_does_not_fit() {
        let ok = small();
        ok.check().unwrap();
        let mut pb = ok.clone();
        pb.fixed_images.pop();
        assert!(pb.check().unwrap_err().contains("fixed flags"));
        let mut pb = ok.clone();
        pb.image_camera[1] = 2;
        assert!(pb.check().unwrap_err().contains("camera 2"));
        let mut pb = ok.clone();
        pb.cameras[0].params.pop();
        assert!(pb.check().unwrap_err().contains("8 parameters expected"));
        let mut pb = ok.clone();
        pb.cameras[0].free = vec![0, 0];
        assert!(pb.check().unwrap_err().contains("given twice"));
        let mut pb = ok.clone();
        pb.obs[0].point = 4;
        assert!(pb.check().unwrap_err().contains("tie observation 0"));
        let mut pb = ok.clone();
        pb.mobs[0].xy = [f64::NAN, 0.0];
        assert!(pb.check().unwrap_err().contains("marker observation 0"));
        let mut pb = ok.clone();
        pb.gcps[0].2 = [0.01, 0.0, 0.01];
        assert!(pb.check().unwrap_err().contains("control point 0"));
        let mut pb = ok.clone();
        pb.bars[0].1 = 0;
        assert!(pb.check().unwrap_err().contains("scale bar 0"));
        let mut pb = ok.clone();
        pb.gnss[2].0 = 3;
        assert!(pb.check().unwrap_err().contains("GNSS position 2"));
        let mut pb = ok.clone();
        pb.points[1][2] = f64::INFINITY;
        assert!(pb.check().unwrap_err().contains("not finite"));
        let mut pb = ok.clone();
        pb.marker_sigma = 0.0;
        assert!(pb.check().unwrap_err().contains("marker_sigma"));
        let mut pb = ok.clone();
        pb.rs_sigma = vec![1.0, 1.0];
        assert!(pb.check().unwrap_err().contains("rs_sigma"));
        pb.rolling_shutter = false; // not used, not checked
        pb.check().unwrap();
        let mut pb = ok;
        pb.rs_sigma_angular = vec![0.005, 0.005, 0.005];
        pb.check().unwrap();
        assert_eq!(pb.rs_sigmas(1), (1.0, 0.005));
    }
}
