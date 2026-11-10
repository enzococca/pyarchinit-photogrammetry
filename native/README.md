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
GPU kernels (wgpu: SIFT, descriptor matching, PatchMatch, fusion), RTKLIB's post-processing
of GNSS observations (`ppk_solve`, from 0.5: what `rnx2rtkp -k options -o solution files`
does, with RTKLIB built into the extension), and the decoding of Hatanaka-compressed RINEX
(`crx2rnx`, from 0.6: Compact RINEX 1.0, 3.0 and 3.1 into the RINEX that RNXCMP's CRX2RNX
writes, by a decoder of our own written from the description of the format in Hatanaka (2008),
Bulletin of the Geographical Survey Institute 55, 21-30).
From 0.7 (API 10) the kernels of the TIN (`engine.tin`): the constrained Delaunay triangulation of
the vertices with the break lines as constraints (`tin_triangulate`, with the crate
[spade](https://github.com/Stoeoef/spade), robust predicates in double precision; constraints that
cross are split at a new vertex), the TIN rasterised on a grid (`tin_rasterize`), its contour
segments (`tin_contours`) and its exact cut and fill against a plane (`tin_volume`).
From 0.8 (API 11) RTKLIB's conversion of raw receiver logs into RINEX 3 (`raw_to_rinex`, what
RTKLIB's `convbin` does, with `convert_cancel` to stop it): u-blox UBX (time marks TIM-TM2 as event
records), RTCM 3 and NovAtel; Septentrio SBF, Trimble RT-17 and Unicore are known by name but their
decoders are not built (`vendor/rtklib/VERSION` says why). RTKLIB is built with `NFREQ=3` (and
`NEXOBS=3`), as the post-processing needs, where `convbin`'s makefile has `NFREQ=4`: at most 6
signals of a satellite in an epoch are kept instead of 7, so on logs of many signals (Galileo E6,
BeiDou B3I, B1C and B2a beside the others) the core may write fewer observation types than
`convbin`; on RTKLIB's samples of u-blox, RTCM 3 and NovAtel (3 signals at most) the RINEX is the
same.

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
`vendor/rtklib/license.txt` and in `NOTICE`, which come with the package. Of RTKLIB's receiver
decoders only `ublox.c` (T. Takasu, T. Suzuki) and `novatel.c` (T. Takasu) are included. Their headers name no licence: BSD 2-clause for them is inferred from the
distribution (T. Takasu holds the copyright of its `license.txt`), see `vendor/rtklib/VERSION`. RTKLIB's optional routines
of the IAU SOFA library (`sofa.c`, a licence of their own) are not included.

The Rust crates built into the extension are under licences compatible with the GPL (MIT or
Apache-2.0: PyO3, numpy, rayon, wgpu, spade and their dependencies).
