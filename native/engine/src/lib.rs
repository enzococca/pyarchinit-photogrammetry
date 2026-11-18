//! `ppg_engine`: the kernels of pyarchinit-photogrammetry without Python - the camera models, the
//! bundle adjustment (`bundle`), the depth maps, PatchMatch, the TIN and the GPU kernels (wgpu) -
//! so that they are built and tested on their own (`cargo test -p ppg_engine`) and timed by the
//! binary `ppg-bench` (folder bench).
//! The Python extension `ppg_core`, the root package of the workspace, turns numpy arrays into
//! their arguments.

pub mod bundle;
pub mod camera;
pub mod depth;
pub mod gpu;
pub mod gpu_fuse;
pub mod gpu_patchmatch;
pub mod gpu_sift;
pub mod patchmatch;
pub mod tin;
