//! The geometry of two views: the minimal solvers of the essential and fundamental matrices and
//! of the homography, the errors that measure them on a correspondence and the pose of an
//! essential matrix (`solvers`); the robust estimation of a model on correspondences, LO-RANSAC
//! with PROSAC sampling (`ransac`); the configuration of a pair and its inliers as COLMAP decides
//! them (`two_view`), with the refinement of each model on its inliers (`refine`); the point
//! normalisation and the singular value decomposition they share (`linalg`).

pub(crate) mod linalg;
pub mod ransac;
pub(crate) mod refine;
pub mod solvers;
pub mod two_view;
