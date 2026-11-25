//! The native global structure-from-motion mapper (milestone 3; Pan et al. 2024, "Global
//! Structure-from-Motion Revisited", as GLOMAP): the arrays it starts from (`input`: the key
//! points and the inlier matches of the verified pairs, as COLMAP's database holds them) and the
//! view graph of the pairs with their relative poses and the focal lengths found from the
//! fundamental matrices (`graph`), the rotations of the photos averaged from the relative
//! rotations of its edges (`rotation`), the tracks joined from the matches and the longest of them
//! chosen over every photo (`tracks`), the points triangulated from them with the filter of
//! their observations (`triangulate`), the global positioning of the photos and the points with
//! the positions of the photos inside it (`positioning`), the rounds of adjustment (`rounds`), the
//! model in COLMAP's binary format (`model_io`), and the `Mapper` that runs every stage in its
//! order (`heartbeat` runs one of them on a thread of its own while the caller gives progress).
//! `synthetic` makes the ring of `ppg-bench map`.

pub mod graph;
pub mod heartbeat;
pub mod input;
pub mod model_io;
pub mod positioning;
pub mod rotation;
pub mod rounds;
pub mod synthetic;
pub mod tracks;
pub mod triangulate;

mod run;

pub use run::{Mapper, MapperReport};

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
    /// The scale of the Huber loss on the residuals of the rays in the global positioning
    /// (`positioning`), in the units of the residual (a difference of unit directions, about an
    /// angle in radians): GLOMAP's `GlobalPositionerOptions::thres_loss_function`, 0.1.
    pub positioning_huber: f64,
    /// The rounds of adjustment (`rounds`): COLMAP's global mapper runs its bundle adjustment
    /// `ba_num_iterations` times, 3 in pycolmap 4.2.1.
    pub rounds: usize,
    /// The scale of the robust loss on the reprojection errors of the rounds, in pixels:
    /// pycolmap's `GlobalPipelineOptions().mapper.bundle_adjustment.ceres.loss_function_scale`,
    /// 1.0 in pycolmap 4.2.1.
    pub ba_loss_scale_px: f64,
    /// The loss of the rounds is Cauchy's when true, Huber's when false: pycolmap's
    /// `GlobalPipelineOptions().mapper.bundle_adjustment.ceres.loss_function_type`, `HUBER` in
    /// pycolmap 4.2.1.
    pub ba_loss_cauchy: bool,
    /// After the rounds, every track is triangulated and one last adjustment runs with all of them
    /// (COLMAP's retriangulation in its global mapper); false only for the trials of the plan
    /// (`PPG_NATIVE_FINAL_ALL_TRACKS=0`).
    pub final_all_tracks: bool,
    /// The filter of the last adjustment with every track, before and after it, as COLMAP's
    /// retriangulation filters the global mapper's block: an observation whose reprojection error
    /// exceeds this many pixels goes (beside the normalised threshold), and a point goes whose rays
    /// meet at less than `final_min_triangulation_deg` (pycolmap's
    /// `IncrementalPipelineOptions().mapper.filter_max_reproj_error` and `.filter_min_tri_angle`,
    /// 4.0 and 1.5 in pycolmap 4.2.1, the options of the retriangulation of its global mapper).
    /// The normalised threshold alone keeps errors up to `max_normalized_reprojection` times the
    /// focal length (36 px on a 20-megapixel drone camera): the wrong observations of long tracks.
    pub final_max_reprojection_px: f64,
    pub final_min_triangulation_deg: f64,
    /// One flag per camera of the input: its sensor is OPENCV (the camera of the mapper being
    /// RADIAL for a square sensor), so that the last round frees its principal point and decentring
    /// terms on a block held by the positions. Empty: none but the cameras whose model is OPENCV.
    pub opencv_sensor: Vec<bool>,
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
            positioning_huber: 0.1,
            rounds: 3,
            ba_loss_scale_px: 1.0,
            ba_loss_cauchy: false,
            final_all_tracks: true,
            final_max_reprojection_px: 4.0,
            final_min_triangulation_deg: 1.5,
            opencv_sensor: Vec::new(),
        }
    }
}
