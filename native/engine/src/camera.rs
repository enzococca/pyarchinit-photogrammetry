//! COLMAP camera models: the pixel of a point given in the camera frame, computed as COLMAP's
//! `ImgFromCam` does (normalised coordinates, lens distortion, focal length and principal point).
//! `project` is the one implementation of each model, generic over the scalar (`Real`): `f64` for
//! the pixels, the dual numbers of `bundle::dual` for the Jacobians of the bundle adjustment.

use std::ops::{Add, Div, Mul, Sub};

/// The models the kernels know, in the order of `MODELS` (the others stay with the Python code).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Model {
    SimplePinhole,
    Pinhole,
    SimpleRadial,
    Radial,
    OpenCv,
    FullOpenCv,
}

impl Model {
    /// The model of a COLMAP name and the number of its parameters.
    pub fn from_name(name: &str) -> Result<(Model, usize), String> {
        let model = match name {
            "SIMPLE_PINHOLE" => Model::SimplePinhole,
            "PINHOLE" => Model::Pinhole,
            "SIMPLE_RADIAL" => Model::SimpleRadial,
            "RADIAL" => Model::Radial,
            "OPENCV" => Model::OpenCv,
            "FULL_OPENCV" => Model::FullOpenCv,
            other => return Err(format!("camera model {other} is not supported by the native kernels")),
        };
        Ok((model, model.parameter_count()))
    }

    /// The number of parameters of the model (COLMAP's).
    pub fn parameter_count(&self) -> usize {
        match self {
            Model::SimplePinhole => 3,
            Model::Pinhole | Model::SimpleRadial => 4,
            Model::Radial => 5,
            Model::OpenCv => 8,
            Model::FullOpenCv => 12,
        }
    }

    /// Indices of the principal point in the parameter vector: after the one focal length of the
    /// single-focal models, after the two of the others.
    pub fn principal_point(&self) -> [usize; 2] {
        match self {
            Model::SimplePinhole | Model::SimpleRadial | Model::Radial => [1, 2],
            Model::Pinhole | Model::OpenCv | Model::FullOpenCv => [2, 3],
        }
    }
}

/// A scalar the camera models are written for: `f64`, or a dual number that carries derivatives.
pub trait Real:
    Copy
    + Add<Output = Self>
    + Sub<Output = Self>
    + Mul<Output = Self>
    + Div<Output = Self>
    + Add<f64, Output = Self>
    + Sub<f64, Output = Self>
    + Mul<f64, Output = Self>
    + Div<f64, Output = Self>
{
    /// The scalar of a plain value (a constant, without derivatives).
    fn from_f64(x: f64) -> Self;

    /// The value, without derivatives.
    fn value(self) -> f64;
}

impl Real for f64 {
    fn from_f64(x: f64) -> Self {
        x
    }

    fn value(self) -> f64 {
        self
    }
}

/// Pixel of the normalised point `(u, v) = (x / z, y / z)` under `model`, whose parameters `p` are
/// in COLMAP's order (at least as many as the model has). The operations and their order are those
/// of milestone 0, so that `f64` gives the pixels it gave then, bit for bit.
#[inline]
pub fn project<T: Real>(model: Model, p: &[T], u: T, v: T) -> (T, T) {
    debug_assert!(
        p.len() >= model.parameter_count(),
        "{model:?} has {} parameters, {} given",
        model.parameter_count(),
        p.len()
    );
    let two = T::from_f64(2.0);
    match model {
        Model::SimplePinhole => (p[0] * u + p[1], p[0] * v + p[2]),
        Model::Pinhole => (p[0] * u + p[2], p[1] * v + p[3]),
        Model::SimpleRadial => {
            let k = p[3];
            let u2 = u * u;
            let v2 = v * v;
            let r2 = u2 + v2;
            let radial = k * r2;
            let (du, dv) = (u * radial, v * radial);
            (p[0] * (u + du) + p[1], p[0] * (v + dv) + p[2])
        }
        Model::Radial => {
            let (k1, k2) = (p[3], p[4]);
            let u2 = u * u;
            let v2 = v * v;
            let r2 = u2 + v2;
            let radial = k1 * r2 + k2 * r2 * r2;
            let (du, dv) = (u * radial, v * radial);
            (p[0] * (u + du) + p[1], p[0] * (v + dv) + p[2])
        }
        Model::OpenCv => {
            let (k1, k2, p1, p2) = (p[4], p[5], p[6], p[7]);
            let u2 = u * u;
            let uv = u * v;
            let v2 = v * v;
            let r2 = u2 + v2;
            let radial = k1 * r2 + k2 * r2 * r2;
            let du = u * radial + two * p1 * uv + p2 * (r2 + two * u2);
            let dv = v * radial + two * p2 * uv + p1 * (r2 + two * v2);
            (p[0] * (u + du) + p[2], p[1] * (v + dv) + p[3])
        }
        Model::FullOpenCv => {
            let one = T::from_f64(1.0);
            let (k1, k2, p1, p2, k3, k4, k5, k6) = (p[4], p[5], p[6], p[7], p[8], p[9], p[10], p[11]);
            let u2 = u * u;
            let uv = u * v;
            let v2 = v * v;
            let r2 = u2 + v2;
            let r4 = r2 * r2;
            let r6 = r4 * r2;
            let radial = (one + k1 * r2 + k2 * r4 + k3 * r6) / (one + k4 * r2 + k5 * r4 + k6 * r6);
            let du = u * radial + two * p1 * uv + p2 * (r2 + two * u2) - u;
            let dv = v * radial + two * p2 * uv + p1 * (r2 + two * v2) - v;
            (p[0] * (u + du) + p[2], p[1] * (v + dv) + p[3])
        }
    }
}

#[derive(Clone, Debug)]
pub struct Camera {
    model: Model,
    p: [f64; 12],
    pub fx: f64,
    pub fy: f64,
    pub cx: f64,
    pub cy: f64,
    pub width: f64,
    pub height: f64,
}

/// Names of the models the kernels know (the others stay with the Python code).
pub const MODELS: [&str; 6] = ["SIMPLE_PINHOLE", "PINHOLE", "SIMPLE_RADIAL", "RADIAL", "OPENCV", "FULL_OPENCV"];

impl Camera {
    pub fn new(model: &str, params: &[f64], width: f64, height: f64) -> Result<Self, String> {
        let (model, count) = Model::from_name(model)?;
        if params.len() < count {
            return Err(format!("{count} parameters expected, {} given", params.len()));
        }
        let mut p = [0.0; 12];
        p[..count].copy_from_slice(&params[..count]);
        let (fx, fy) = match model {
            Model::SimplePinhole | Model::SimpleRadial | Model::Radial => (p[0], p[0]),
            _ => (p[0], p[1]),
        };
        let [cx, cy] = model.principal_point().map(|i| p[i]);
        Ok(Camera { model, p, fx, fy, cx, cy, width, height })
    }

    /// Mean focal length in pixels (`Sensor.focal_px`).
    pub fn focal(&self) -> f64 {
        (self.fx + self.fy) / 2.0
    }

    /// Pixel of the normalised point `(u, v) = (x / z, y / z)`.
    #[inline]
    pub fn pixel(&self, u: f64, v: f64) -> (f64, f64) {
        project(self.model, &self.p, u, v)
    }
}

/// Rows of a `cam_from_world` matrix (3x4).
pub type Pose = [[f64; 4]; 3];

#[inline]
pub fn transform(m: &Pose, x: f64, y: f64, z: f64) -> (f64, f64, f64) {
    (
        m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3],
        m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3],
        m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3],
    )
}

/// What `engine.visibility.project_valid` computes for one point: pixel (NaN when the point is
/// behind the camera or far outside the field of view), depth and whether the pixel is inside the
/// photo by more than `margin`.
#[inline]
pub fn project_valid(cam: &Camera, m: &Pose, x: f64, y: f64, z: f64, margin: f64) -> (f64, f64, f64, bool) {
    let (pcx, pcy, depth) = transform(m, x, y, z);
    if !(depth > 1e-9) {
        return (f64::NAN, f64::NAN, depth, false);
    }
    // far outside the field of view: left out before the lens distortion (as the Python code)
    let lu = cam.fx * pcx / depth + cam.cx;
    let lv = cam.fy * pcy / depth + cam.cy;
    if !(lu > -0.25 * cam.width && lu < 1.25 * cam.width && lv > -0.25 * cam.height && lv < 1.25 * cam.height) {
        return (f64::NAN, f64::NAN, depth, false);
    }
    let (pu, pv) = cam.pixel(pcx / depth, pcy / depth);
    let ok = pu.is_finite()
        && pv.is_finite()
        && pu >= margin
        && pu < cam.width - margin
        && pv >= margin
        && pv < cam.height - margin;
    (pu, pv, depth, ok)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bundle::dual::Dual;

    /// The six models, each with a fixed parameter vector of awkward values, so that every
    /// rounding of its formula is exercised.
    const CASES: [(&str, &[f64]); 6] = [
        ("SIMPLE_PINHOLE", &[1234.5, 655.3, 471.7]),
        ("PINHOLE", &[1234.5, 1240.25, 655.3, 471.7]),
        ("SIMPLE_RADIAL", &[1234.5, 655.3, 471.7, -0.0837]),
        ("RADIAL", &[1234.5, 655.3, 471.7, -0.0837, 0.0121]),
        ("OPENCV", &[1234.5, 1240.25, 655.3, 471.7, -0.0837, 0.0121, 0.00073, -0.00042]),
        (
            "FULL_OPENCV",
            &[1234.5, 1240.25, 655.3, 471.7, -0.0837, 0.0121, 0.00073, -0.00042, 0.0019, 0.047, -0.0112, 0.0023],
        ),
    ];

    /// `Camera::pixel` as it was written before `project` existed (milestone 0), kept here as
    /// the reference the generic implementation must match bit for bit.
    fn pixel_before(model: &str, p: &[f64; 12], u: f64, v: f64) -> (f64, f64) {
        match model {
            "SIMPLE_PINHOLE" => (p[0] * u + p[1], p[0] * v + p[2]),
            "PINHOLE" => (p[0] * u + p[2], p[1] * v + p[3]),
            "SIMPLE_RADIAL" => {
                let k = p[3];
                let u2 = u * u;
                let v2 = v * v;
                let r2 = u2 + v2;
                let radial = k * r2;
                let (du, dv) = (u * radial, v * radial);
                (p[0] * (u + du) + p[1], p[0] * (v + dv) + p[2])
            }
            "RADIAL" => {
                let (k1, k2) = (p[3], p[4]);
                let u2 = u * u;
                let v2 = v * v;
                let r2 = u2 + v2;
                let radial = k1 * r2 + k2 * r2 * r2;
                let (du, dv) = (u * radial, v * radial);
                (p[0] * (u + du) + p[1], p[0] * (v + dv) + p[2])
            }
            "OPENCV" => {
                let (k1, k2, p1, p2) = (p[4], p[5], p[6], p[7]);
                let u2 = u * u;
                let uv = u * v;
                let v2 = v * v;
                let r2 = u2 + v2;
                let radial = k1 * r2 + k2 * r2 * r2;
                let du = u * radial + 2.0 * p1 * uv + p2 * (r2 + 2.0 * u2);
                let dv = v * radial + 2.0 * p2 * uv + p1 * (r2 + 2.0 * v2);
                (p[0] * (u + du) + p[2], p[1] * (v + dv) + p[3])
            }
            "FULL_OPENCV" => {
                let (k1, k2, p1, p2, k3, k4, k5, k6) = (p[4], p[5], p[6], p[7], p[8], p[9], p[10], p[11]);
                let u2 = u * u;
                let uv = u * v;
                let v2 = v * v;
                let r2 = u2 + v2;
                let r4 = r2 * r2;
                let r6 = r4 * r2;
                let radial = (1.0 + k1 * r2 + k2 * r4 + k3 * r6) / (1.0 + k4 * r2 + k5 * r4 + k6 * r6);
                let du = u * radial + 2.0 * p1 * uv + p2 * (r2 + 2.0 * u2) - u;
                let dv = v * radial + 2.0 * p2 * uv + p1 * (r2 + 2.0 * v2) - v;
                (p[0] * (u + du) + p[2], p[1] * (v + dv) + p[3])
            }
            other => panic!("no reference formula for {other}"),
        }
    }

    /// 50 normalised points of [-0.6, 0.6]^2: 10 columns by 5 rows.
    fn grid() -> Vec<(f64, f64)> {
        let mut points = Vec::with_capacity(50);
        for i in 0..10 {
            for j in 0..5 {
                points.push((-0.6 + 1.2 * i as f64 / 9.0, -0.6 + 1.2 * j as f64 / 4.0));
            }
        }
        points
    }

    #[test]
    fn every_model_gives_the_pixels_it_gave_before() {
        for (name, params) in CASES {
            let camera = Camera::new(name, params, 1300.0, 950.0).unwrap();
            let mut p = [0.0; 12];
            p[..params.len()].copy_from_slice(params);
            for (u, v) in grid() {
                let (x, y) = camera.pixel(u, v);
                let (x0, y0) = pixel_before(name, &p, u, v);
                assert_eq!(
                    (x.to_bits(), y.to_bits()),
                    (x0.to_bits(), y0.to_bits()),
                    "{name} at ({u}, {v}) gives ({x}, {y}), it gave ({x0}, {y0})"
                );
            }
        }
    }

    #[test]
    fn the_dual_jacobian_of_every_model_agrees_with_central_differences() {
        // points away from the axes, so that no derivative of the distortion vanishes by chance
        let points = [(0.31, -0.22), (-0.57, 0.44), (0.05, 0.58)];
        for (name, params) in CASES {
            let (model, count) = Model::from_name(name).unwrap();
            // the variables: u, v, then the parameters of the model
            let n = 2 + count;
            for (u, v) in points {
                let mut x = [0.0; 14];
                x[0] = u;
                x[1] = v;
                x[2..n].copy_from_slice(params);
                let f = |x: &[f64; 14]| project::<f64>(model, &x[2..n], x[0], x[1]);
                let dual: Vec<Dual<14>> = (0..n).map(|i| Dual::variable(x[i], i)).collect();
                let (px, py) = project(model, &dual[2..], dual[0], dual[1]);
                let (x0, y0) = f(&x);
                assert_eq!((px.v.to_bits(), py.v.to_bits()), (x0.to_bits(), y0.to_bits()), "{name}: value");
                for i in 0..n {
                    let h = 1e-6 * x[i].abs().max(1.0);
                    let (mut plus, mut minus) = (x, x);
                    plus[i] += h;
                    minus[i] -= h;
                    let step = plus[i] - minus[i];
                    let (fp, fm) = (f(&plus), f(&minus));
                    let central = [(fp.0 - fm.0) / step, (fp.1 - fm.1) / step];
                    for (axis, (got, want)) in [px.d[i], py.d[i]].into_iter().zip(central).enumerate() {
                        let scale = got.abs().max(want.abs());
                        assert!(
                            scale == 0.0 || (got - want).abs() / scale < 1e-6,
                            "{name} at ({u}, {v}): d pixel[{axis}] / d x[{i}] = {got}, central differences {want}"
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn model_names_give_their_counts_and_principal_points() {
        let expected = [
            ("SIMPLE_PINHOLE", Model::SimplePinhole, 3, [1, 2]),
            ("PINHOLE", Model::Pinhole, 4, [2, 3]),
            ("SIMPLE_RADIAL", Model::SimpleRadial, 4, [1, 2]),
            ("RADIAL", Model::Radial, 5, [1, 2]),
            ("OPENCV", Model::OpenCv, 8, [2, 3]),
            ("FULL_OPENCV", Model::FullOpenCv, 12, [2, 3]),
        ];
        for (name, model, count, principal_point) in expected {
            assert_eq!(Model::from_name(name), Ok((model, count)), "{name}");
            assert_eq!(model.parameter_count(), count, "{name}");
            assert_eq!(model.principal_point(), principal_point, "{name}");
        }
        assert_eq!(MODELS, expected.map(|(name, ..)| name));
        assert!(Model::from_name("FISHEYE").unwrap_err().contains("FISHEYE"));
        // the camera reads its focal lengths and principal point at the indices of its model
        let camera = Camera::new("RADIAL", &[1234.5, 655.3, 471.7, -0.0837, 0.0121], 1300.0, 950.0).unwrap();
        assert_eq!((camera.fx, camera.fy, camera.cx, camera.cy), (1234.5, 1234.5, 655.3, 471.7));
        let camera = Camera::new("OPENCV", CASES[4].1, 1300.0, 950.0).unwrap();
        assert_eq!((camera.fx, camera.fy, camera.cx, camera.cy), (1234.5, 1240.25, 655.3, 471.7));
        assert!(Camera::new("RADIAL", &[1234.5, 655.3, 471.7], 1300.0, 950.0).is_err());
    }
}
