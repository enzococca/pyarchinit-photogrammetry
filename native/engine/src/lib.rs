//! `ppg_engine`: the kernels of pyarchinit-photogrammetry without Python - the camera models, the
//! depth maps, PatchMatch, the TIN and the GPU kernels (wgpu) - so that they are built, tested
//! and timed on their own (`cargo test -p ppg_engine`). The Python extension `ppg_core`, the root
//! package of the workspace, turns numpy arrays into their arguments.

pub mod camera;
pub mod depth;
pub mod gpu;
pub mod gpu_fuse;
pub mod gpu_patchmatch;
pub mod gpu_sift;
pub mod patchmatch;
pub mod tin;
