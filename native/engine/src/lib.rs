//! `ppg_engine`: the kernels of pyarchinit-photogrammetry without Python - the camera models, the
//! bundle adjustment (`bundle`), the depth maps, PatchMatch, the TIN, the GPU kernels (wgpu), the
//! CPU twins of the descriptor matching (`matching`) and of SIFT (`sift_cpu`), the photos read
//! as COLMAP reads them (`image`), the feature store (`store`), the queue of units shared by the
//! GPUs and the CPU (`sched`), the features step built on them (`features`), the minimal solvers
//! of two views and the geometric verification of a pair (`geometry`) - so that they are built
//! and tested on their own
//! (`cargo test -p ppg_engine`) and timed by the binary `ppg-bench` (folder bench). The Python
//! extension `ppg_core`, the root package of the workspace, turns numpy arrays into their
//! arguments.

pub mod bundle;
pub mod camera;
pub mod depth;
pub mod features;
pub mod geometry;
pub mod gpu;
pub mod gpu_fuse;
pub mod gpu_patchmatch;
pub mod gpu_sift;
pub mod image;
pub mod matching;
pub mod patchmatch;
pub mod sched;
pub mod sift_cpu;
pub mod store;
pub mod tin;
