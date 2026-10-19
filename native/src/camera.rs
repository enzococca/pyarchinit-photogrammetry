//! COLMAP camera models: the pixel of a point given in the camera frame, computed as COLMAP's
//! `ImgFromCam` does (normalised coordinates, lens distortion, focal length and principal point).

#[derive(Clone, Copy, Debug, PartialEq)]
enum Model {
    SimplePinhole,
    Pinhole,
    SimpleRadial,
    Radial,
    OpenCv,
    FullOpenCv,
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
        let (model, count) = match model {
            "SIMPLE_PINHOLE" => (Model::SimplePinhole, 3),
            "PINHOLE" => (Model::Pinhole, 4),
            "SIMPLE_RADIAL" => (Model::SimpleRadial, 4),
            "RADIAL" => (Model::Radial, 5),
            "OPENCV" => (Model::OpenCv, 8),
            "FULL_OPENCV" => (Model::FullOpenCv, 12),
            other => return Err(format!("camera model {other} is not supported by the native kernels")),
        };
        if params.len() < count {
            return Err(format!("{count} parameters expected, {} given", params.len()));
        }
        let mut p = [0.0; 12];
        p[..count].copy_from_slice(&params[..count]);
        let (fx, fy, cx, cy) = match model {
            Model::SimplePinhole | Model::SimpleRadial | Model::Radial => (p[0], p[0], p[1], p[2]),
            _ => (p[0], p[1], p[2], p[3]),
        };
        Ok(Camera { model, p, fx, fy, cx, cy, width, height })
    }

    /// Mean focal length in pixels (`Sensor.focal_px`).
    pub fn focal(&self) -> f64 {
        (self.fx + self.fy) / 2.0
    }

    /// Pixel of the normalised point `(u, v) = (x / z, y / z)`.
    #[inline]
    pub fn pixel(&self, u: f64, v: f64) -> (f64, f64) {
        let p = &self.p;
        match self.model {
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
                let du = u * radial + 2.0 * p1 * uv + p2 * (r2 + 2.0 * u2);
                let dv = v * radial + 2.0 * p2 * uv + p1 * (r2 + 2.0 * v2);
                (p[0] * (u + du) + p[2], p[1] * (v + dv) + p[3])
            }
            Model::FullOpenCv => {
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
        }
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
