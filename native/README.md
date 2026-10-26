# pyarchinit-photogrammetry-core

Native kernels of [pyarchinit-photogrammetry](https://github.com/enzococca/pyarchinit-photogrammetry),
written in Rust (PyO3 + rayon) and imported as `ppg_core`. The package is optional:
pyarchinit-photogrammetry keeps the numpy version of every kernel, uses it when `ppg_core` is
missing or when `PPG_NATIVE=0`, and its tests compare the two.

Kernels: fusion of the depth maps of one photo, depth z-buffer and normals, projection of points
with the COLMAP camera models, scoring of the photos of an orthomosaic tile, agreement of points
with a depth map (confidence), PatchMatch multi-view stereo of one photo (`patchmatch_depth`: a
plane per pixel, normalised cross correlation with the best of its neighbouring photos,
red-black propagation and random refinement, from random planes or from a depth map), the
GPU kernels (wgpu: SIFT, descriptor matching, PatchMatch, fusion), and RTKLIB's post-processing
of GNSS observations (`ppk_solve`, from 0.5: what `rnx2rtkp -k options -o solution files`
does, with RTKLIB built into the extension).

Build and install in the environment of pyarchinit-photogrammetry (a Rust toolchain is needed):

```bash
pip install ./native        # or: uv pip install ./native
```

Building it compiles RTKLIB's C sources too (`vendor/rtklib`, with the `cc` crate): a C
compiler is needed (Xcode's command line tools, gcc or clang, MSVC on Windows). RTKLIB is built
as C99 and without fused multiply-add (`-ffp-contract=off`, `/fp:precise` with MSVC): the
integer ambiguities of a kinematic solution hang on the last bit of the arithmetic, and the arm64
compilers, which fuse by default, would otherwise fix other epochs than the x86 builds of the
same project (on RTKLIB's test data 115 of 120 instead of about 40).

License: GPL-2.0-or-later. The extension includes RTKLIB-EX v2.5.1
(https://github.com/rtklibexplorer/RTKLIB, commit 62d4677ed8425a4e2748c6d390b500d1afb493fc),
Copyright (c) 2007-2020, T. Takasu, distributed under the BSD 2-clause license: its text is in
`vendor/rtklib/license.txt` and in `NOTICE`, which come with the package. RTKLIB's optional routines
of the IAU SOFA library (`sofa.c`, a licence of their own) are not included.
