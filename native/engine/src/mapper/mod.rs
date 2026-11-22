//! The native global structure-from-motion mapper (milestone 3; Pan et al. 2024, "Global
//! Structure-from-Motion Revisited", as GLOMAP): the arrays it starts from (`input`: the key
//! points and the inlier matches of the verified pairs, as COLMAP's database holds them) and the
//! view graph of the pairs with their relative poses and the focal lengths found from the
//! fundamental matrices (`graph`), the rotations of the photos averaged from the relative
//! rotations of its edges (`rotation`), the tracks joined from the matches and the longest of them
//! chosen over every photo (`tracks`), and the points triangulated from them with the filter of
//! their observations (`triangulate`).

pub mod graph;
pub mod input;
pub mod rotation;
pub mod tracks;
pub mod triangulate;

/// The options of the whole mapper. The defaults are the figures of the plan; the ones that come
/// from pycolmap are passed by Python in place of them.
#[derive(Clone, Debug, PartialEq)]
pub struct Options {
    /// The fewest inlier matches of a pair that enters the view graph (pycolmap's
    /// `TwoViewGeometryOptions().min_num_inliers`, passed from Python).
    pub min_inliers: usize,
    /// The largest epipolar (Sampson) error of an inlier of the relative pose, in pixels; divided by
    /// the focal lengths of the pair for the normalised coordinates (PoseLib's default, which GLOMAP
    /// uses for its relative poses).
    pub max_epipolar_error_px: f64,
    /// A pair has parallax when the median triangulation angle of its inliers is at least this, in
    /// degrees.
    pub min_pair_parallax_deg: f64,
    /// The largest number of trials of the LO-RANSAC of a relative pose.
    pub max_relpose_trials: usize,
    /// An edge whose relative rotation differs from the one of the averaged rotations of its photos
    /// by more than this, in degrees, is dropped by the rotation averaging (GLOMAP's
    /// `max_rotation_error`).
    pub max_rotation_error_deg: f64,
    /// An observation whose reprojection error exceeds this goes (`triangulate::filter`). The error
    /// is measured in normalised coordinates (the distance between the point seen by the camera,
    /// divided by its depth, and the normalised key point; about the error in pixels over the focal
    /// length), as COLMAP's global mapper filters (`ReprojectionErrorType::NORMALIZED`): pycolmap's
    /// `GlobalPipelineOptions().mapper.max_normalized_reproj_error`, 0.01 in pycolmap 4.2.1, passed
    /// from Python.
    pub max_normalized_reprojection: f64,
    /// The observations of a point go when no two of its rays meet at this angle, in degrees
    /// (pycolmap's `GlobalPipelineOptions().mapper.min_tri_angle_deg`, 1.0 in pycolmap 4.2.1,
    /// passed from Python).
    pub min_triangulation_deg: f64,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            min_inliers: 15,
            max_epipolar_error_px: 1.0,
            min_pair_parallax_deg: 1.0,
            max_relpose_trials: 10000,
            max_rotation_error_deg: 10.0,
            max_normalized_reprojection: 0.01,
            min_triangulation_deg: 1.0,
        }
    }
}
