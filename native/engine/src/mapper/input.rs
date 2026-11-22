//! The arrays the mapper starts from, as Python reads them from COLMAP's database: the photos
//! with their camera and their key points, the cameras, every key point of every photo (COLMAP
//! pixels, x and y), the verified pairs with their inlier matches, their configuration and their
//! fundamental matrix. `Input::check` refuses an input whose indices, ranges or numbers the
//! mapper could not use, with a message that names the item.

use std::ops::Range;

use crate::camera::{Model, MODELS};

/// A photo: its name (the seed of the RANSAC of its pairs), its id in the database, the index of
/// its camera in `Input::cameras` and the range of its key points in `Input::keypoints`.
#[derive(Clone, Debug, PartialEq)]
pub struct ImageIn {
    pub name: String,
    pub image_id: u32,
    pub camera: usize,
    pub keypoints: Range<usize>,
}

/// A camera: its id in the database, its COLMAP model and parameters, the size of its photos and
/// whether its focal length is known (`has_prior_focal_length`) or guessed.
#[derive(Clone, Debug, PartialEq)]
pub struct CameraIn {
    pub camera_id: u32,
    pub model: String,
    pub params: Vec<f64>,
    pub width: f64,
    pub height: f64,
    pub prior_focal: bool,
}

/// A verified pair: the indices of its photos in `Input::images`, the range of its inlier matches
/// in `Input::matches`, its configuration (COLMAP's number) and its fundamental matrix, row-major,
/// when the database holds one.
#[derive(Clone, Debug, PartialEq)]
pub struct PairIn {
    pub a: usize,
    pub b: usize,
    pub matches: Range<usize>,
    pub config: i32,
    pub f: Option<[f64; 9]>,
}

/// The input of the mapper. A match is `[key point of a, key point of b]`, each an index among the
/// key points of its own photo (COLMAP's `point2D_idx`), not into `keypoints`.
#[derive(Clone, Debug, PartialEq)]
pub struct Input {
    pub images: Vec<ImageIn>,
    pub cameras: Vec<CameraIn>,
    pub keypoints: Vec<[f32; 2]>,
    pub pairs: Vec<PairIn>,
    pub matches: Vec<[u32; 2]>,
}

fn range_within(r: &Range<usize>, len: usize) -> bool {
    r.start <= r.end && r.end <= len
}

impl Input {
    /// The key point `k` of photo `image`, in pixels.
    pub fn keypoint(&self, image: usize, k: u32) -> [f64; 2] {
        let p = self.keypoints[self.images[image].keypoints.start + k as usize];
        [p[0] as f64, p[1] as f64]
    }

    /// `Err` naming the first item the mapper could not use: a camera of another model than the six
    /// of `camera::MODELS`, with too few or not finite parameters or a size that is not positive; a
    /// photo whose camera or key point range is out of range; a key point that is not finite; a
    /// pair whose photos are out of range or the same, whose matches are out of range, whose match
    /// names a key point beyond those of its photo, or whose fundamental matrix is not finite.
    pub fn check(&self) -> Result<(), String> {
        for (c, cam) in self.cameras.iter().enumerate() {
            if !MODELS.contains(&cam.model.as_str()) {
                return Err(format!("camera {c}: model {} is not one of {}", cam.model, MODELS.join(", ")));
            }
            let (_, count) = Model::from_name(&cam.model)?;
            if cam.params.len() != count {
                return Err(format!("camera {c}: {} parameters for {}, {count} expected", cam.params.len(), cam.model));
            }
            if !cam.params.iter().all(|p| p.is_finite()) {
                return Err(format!("camera {c}: a parameter is not finite ({:?})", cam.params));
            }
            if !(cam.width > 0.0 && cam.width.is_finite() && cam.height > 0.0 && cam.height.is_finite()) {
                return Err(format!("camera {c}: size {} x {} is not positive", cam.width, cam.height));
            }
        }
        for (i, image) in self.images.iter().enumerate() {
            if image.camera >= self.cameras.len() {
                return Err(format!("image {i} ({}): camera {} out of range ({} cameras)", image.name, image.camera,
                                   self.cameras.len()));
            }
            if !range_within(&image.keypoints, self.keypoints.len()) {
                return Err(format!("image {i} ({}): key points {:?} out of range ({} key points)", image.name,
                                   image.keypoints, self.keypoints.len()));
            }
            for k in image.keypoints.clone() {
                if !self.keypoints[k].iter().all(|v| v.is_finite()) {
                    return Err(format!("key point {k} (image {i}, {}) is not finite: {:?}", image.name,
                                       self.keypoints[k]));
                }
            }
        }
        let n = self.images.len();
        for (p, pair) in self.pairs.iter().enumerate() {
            for image in [pair.a, pair.b] {
                if image >= n {
                    return Err(format!("pair {p}: image {image} out of range ({n} images)"));
                }
            }
            if pair.a == pair.b {
                return Err(format!("pair {p}: image {} paired with itself", pair.a));
            }
            if !range_within(&pair.matches, self.matches.len()) {
                return Err(format!("pair {p}: matches {:?} out of range ({} matches)", pair.matches,
                                   self.matches.len()));
            }
            let (na, nb) = (self.images[pair.a].keypoints.len(), self.images[pair.b].keypoints.len());
            for m in pair.matches.clone() {
                let [ka, kb] = self.matches[m];
                for (k, image, count) in [(ka, pair.a, na), (kb, pair.b, nb)] {
                    if k as usize >= count {
                        return Err(format!("pair {p}, match {m}: key point {k} beyond the {count} of image {image} \
                                            ({})", self.images[image].name));
                    }
                }
            }
            if let Some(f) = &pair.f {
                if !f.iter().all(|v| v.is_finite()) {
                    return Err(format!("pair {p}: the fundamental matrix is not finite"));
                }
            }
        }
        Ok(())
    }
}
