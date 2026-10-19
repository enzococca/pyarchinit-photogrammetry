# pyarchinit-photogrammetry-core

Native kernels of [pyarchinit-photogrammetry](https://github.com/enzococca/pyarchinit-photogrammetry),
written in Rust (PyO3 + rayon) and imported as `ppg_core`. The package is optional:
pyarchinit-photogrammetry keeps the numpy version of every kernel, uses it when `ppg_core` is
missing or when `PPG_NATIVE=0`, and its tests compare the two.

Kernels: fusion of the depth maps of one photo, depth z-buffer and normals, projection of points
with the COLMAP camera models, scoring of the photos of an orthomosaic tile, agreement of points
with a depth map (confidence), PatchMatch multi-view stereo of one photo (`patchmatch_depth`: a
plane per pixel, normalised cross correlation with the best of its neighbouring photos,
red-black propagation and random refinement, from random planes or from a depth map).

Build and install in the environment of pyarchinit-photogrammetry (a Rust toolchain is needed):

```bash
pip install ./native        # or: uv pip install ./native
```

License: GPL-2.0-or-later.
