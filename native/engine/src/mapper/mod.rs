//! The native global structure-from-motion mapper (milestone 3; Pan et al. 2024, "Global
//! Structure-from-Motion Revisited", as GLOMAP): the arrays it starts from (`input`: the key
//! points and the inlier matches of the verified pairs, as COLMAP's database holds them) and the
//! view graph of the pairs with their relative poses and the focal lengths found from the
//! fundamental matrices (`graph`).

pub mod graph;
pub mod input;

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
}

impl Default for Options {
    fn default() -> Self {
        Options { min_inliers: 15, max_epipolar_error_px: 1.0, min_pair_parallax_deg: 1.0, max_relpose_trials: 10000 }
    }
}
