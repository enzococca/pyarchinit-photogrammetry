//! `ppg_engine`: the kernels of pyarchinit-photogrammetry without Python - the camera models, the
//! bundle adjustment (`bundle`), the depth maps, PatchMatch, the TIN, the GPU kernels (wgpu), the
//! CPU twins of the descriptor matching (`matching`) and of SIFT (`sift_cpu`) and the photos read
//! as COLMAP reads them (`image`) - so that they are built and tested on their own
//! (`cargo test -p ppg_engine`) and timed by the binary `ppg-bench` (folder bench). The Python
//! extension `ppg_core`, the root package of the workspace, turns numpy arrays into their arguments.

pub mod bundle;
pub mod camera;
pub mod depth;
pub mod gpu;
pub mod gpu_fuse;
pub mod gpu_patchmatch;
pub mod gpu_sift;
pub mod image;
pub mod matching;
pub mod patchmatch;
pub mod sift_cpu;
pub mod tin;
