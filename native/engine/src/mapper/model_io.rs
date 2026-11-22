//! The model of the mapper on disk in COLMAP's binary format, as COLMAP 4.2 writes a reconstruction
//! (`src/colmap/scene/reconstruction_io_binary.cc`, `Reconstruction::WriteBinary`): `rigs.bin`,
//! `cameras.bin`, `frames.bin`, `images.bin` and `points3D.bin`, little endian, every list sorted by
//! its ids. Each camera is the one sensor of a rig of its own id and each photo the one image of a
//! frame of its own id (what COLMAP makes of a model without rigs and frames, and what pycolmap
//! 4.2.1 writes for a camera and an image added with their trivial rig and frame): a model read by
//! pycolmap and written again by it is the same bytes.

use std::fs::File;
use std::io::{self, BufWriter, Write};
use std::path::Path;

use nalgebra::{Matrix3, Rotation3, UnitQuaternion};

use crate::camera::Model;

/// COLMAP's `kInvalidPoint3DId`: the point 3D of a point 2D that has none.
pub const NO_POINT: u64 = u64::MAX;
/// COLMAP's `SensorType::CAMERA`.
const SENSOR_CAMERA: i32 = 0;

/// A camera: its id, model, size in pixels and parameters in COLMAP's order.
#[derive(Clone, Debug, PartialEq)]
pub struct ModelCamera {
    pub camera_id: u32,
    pub model: Model,
    pub width: u64,
    pub height: u64,
    pub params: Vec<f64>,
}

/// A registered photo: its id, its camera, its name, its pose `cam_from_world` (the quaternion
/// `w, x, y, z` of the rotation and the translation) and every key point of the photo as a point
/// 2D, COLMAP pixels, with the id of its point 3D (`NO_POINT` for none).
#[derive(Clone, Debug, PartialEq)]
pub struct ModelImage {
    pub image_id: u32,
    pub camera_id: u32,
    pub name: String,
    pub rotation: [f64; 4],
    pub translation: [f64; 3],
    pub points2d: Vec<([f64; 2], u64)>,
}

/// A point 3D: its id, position, colour, mean reprojection error in pixels and track (image id,
/// index of the point 2D in that image).
#[derive(Clone, Debug, PartialEq)]
pub struct ModelPoint {
    pub point_id: u64,
    pub xyz: [f64; 3],
    pub color: [u8; 3],
    pub error: f64,
    pub track: Vec<(u32, u32)>,
}

/// COLMAP's number of a camera model (`CameraModelId`).
pub fn model_id(model: Model) -> i32 {
    match model {
        Model::SimplePinhole => 0,
        Model::Pinhole => 1,
        Model::SimpleRadial => 2,
        Model::Radial => 3,
        Model::OpenCv => 4,
        Model::FullOpenCv => 6,
    }
}

/// The unit quaternion `w, x, y, z` of a rotation matrix, `w` not negative.
pub fn quaternion(r: &[[f64; 3]; 3]) -> [f64; 4] {
    let m = Matrix3::from_fn(|i, j| r[i][j]);
    let q = UnitQuaternion::from_rotation_matrix(&Rotation3::from_matrix_unchecked(m));
    let q = q.into_inner();
    let sign = if q.w < 0.0 { -1.0 } else { 1.0 };
    [sign * q.w, sign * q.i, sign * q.j, sign * q.k]
}

/// Little-endian writes of the scalars of the format.
struct Out<W: Write>(W);

impl<W: Write> Out<W> {
    fn u8(&mut self, v: u8) -> io::Result<()> {
        self.0.write_all(&[v])
    }
    fn i32(&mut self, v: i32) -> io::Result<()> {
        self.0.write_all(&v.to_le_bytes())
    }
    fn u32(&mut self, v: u32) -> io::Result<()> {
        self.0.write_all(&v.to_le_bytes())
    }
    fn u64(&mut self, v: u64) -> io::Result<()> {
        self.0.write_all(&v.to_le_bytes())
    }
    fn f64(&mut self, v: f64) -> io::Result<()> {
        self.0.write_all(&v.to_le_bytes())
    }
    fn pose(&mut self, rotation: &[f64; 4], translation: &[f64; 3]) -> io::Result<()> {
        for &v in rotation.iter().chain(translation) {
            self.f64(v)?;
        }
        Ok(())
    }
}

fn create(dir: &Path, name: &str) -> io::Result<Out<BufWriter<File>>> {
    Ok(Out(BufWriter::new(File::create(dir.join(name))?)))
}

fn finish(out: Out<BufWriter<File>>) -> io::Result<()> {
    out.0.into_inner().map_err(|e| e.into_error())?.sync_all()
}

fn sorted<T, K: Ord>(items: &[T], key: impl Fn(&T) -> K) -> Vec<&T> {
    let mut v: Vec<&T> = items.iter().collect();
    v.sort_by_key(|x| key(x));
    v
}

fn invalid(message: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

/// Writes the model into the folder `dir` (which exists): a rig per camera and a frame per photo,
/// the five files of COLMAP 4.2. `Err` when an id is repeated, a photo names a camera that is not
/// given, a point names a photo that is not given or a point 2D beyond its key points, or a file
/// cannot be written.
pub fn write_colmap_binary(
    dir: &Path,
    cameras: &[ModelCamera],
    images: &[ModelImage],
    points: &[ModelPoint],
) -> io::Result<()> {
    let cameras = sorted(cameras, |c| c.camera_id);
    let images = sorted(images, |i| i.image_id);
    let points = sorted(points, |p| p.point_id);
    if cameras.windows(2).any(|w| w[0].camera_id == w[1].camera_id) {
        return Err(invalid("a camera id is given twice".into()));
    }
    if images.windows(2).any(|w| w[0].image_id == w[1].image_id) {
        return Err(invalid("an image id is given twice".into()));
    }
    if points.windows(2).any(|w| w[0].point_id == w[1].point_id) {
        return Err(invalid("a point id is given twice".into()));
    }
    for image in &images {
        if cameras.binary_search_by_key(&image.camera_id, |c| c.camera_id).is_err() {
            return Err(invalid(format!("image {} names camera {}, not given", image.image_id, image.camera_id)));
        }
    }
    for point in &points {
        for &(image_id, k) in &point.track {
            let Ok(at) = images.binary_search_by_key(&image_id, |i| i.image_id) else {
                return Err(invalid(format!("point {} names image {image_id}, not given", point.point_id)));
            };
            if k as usize >= images[at].points2d.len() {
                return Err(invalid(format!("point {} names point 2D {k} of image {image_id}", point.point_id)));
            }
        }
    }

    let mut out = create(dir, "rigs.bin")?;
    out.u64(cameras.len() as u64)?;
    for c in &cameras {
        out.u32(c.camera_id)?; // the rig, of the camera's id
        out.u32(1)?; // one sensor, the reference
        out.i32(SENSOR_CAMERA)?;
        out.u32(c.camera_id)?;
    }
    finish(out)?;

    let mut out = create(dir, "cameras.bin")?;
    out.u64(cameras.len() as u64)?;
    for c in &cameras {
        out.u32(c.camera_id)?;
        out.i32(model_id(c.model))?;
        out.u64(c.width)?;
        out.u64(c.height)?;
        for &p in &c.params {
            out.f64(p)?;
        }
    }
    finish(out)?;

    let mut out = create(dir, "frames.bin")?;
    out.u64(images.len() as u64)?;
    for i in &images {
        out.u32(i.image_id)?; // the frame, of the image's id
        out.u32(i.camera_id)?; // its rig
        out.pose(&i.rotation, &i.translation)?;
        out.u32(1)?;
        out.i32(SENSOR_CAMERA)?;
        out.u32(i.camera_id)?;
        out.u64(i.image_id as u64)?;
    }
    finish(out)?;

    let mut out = create(dir, "images.bin")?;
    out.u64(images.len() as u64)?;
    for i in &images {
        out.u32(i.image_id)?;
        out.pose(&i.rotation, &i.translation)?;
        out.u32(i.camera_id)?;
        out.0.write_all(i.name.as_bytes())?;
        out.u8(0)?;
        out.u64(i.points2d.len() as u64)?;
        for &(xy, point) in &i.points2d {
            out.f64(xy[0])?;
            out.f64(xy[1])?;
            out.u64(point)?;
        }
    }
    finish(out)?;

    let mut out = create(dir, "points3D.bin")?;
    out.u64(points.len() as u64)?;
    for p in &points {
        out.u64(p.point_id)?;
        for &v in &p.xyz {
            out.f64(v)?;
        }
        for &c in &p.color {
            out.u8(c)?;
        }
        out.f64(p.error)?;
        out.u64(p.track.len() as u64)?;
        for &(image_id, k) in &p.track {
            out.u32(image_id)?;
            out.u32(k)?;
        }
    }
    finish(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_quaternion_of_a_rotation() {
        // 90 degrees about z: w and z are cos 45 and sin 45 degrees
        let q = quaternion(&[[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]);
        let h = std::f64::consts::FRAC_1_SQRT_2;
        assert!((q[0] - h).abs() < 1e-15 && q[1].abs() < 1e-15 && q[2].abs() < 1e-15 && (q[3] - h).abs() < 1e-15);
    }
}
