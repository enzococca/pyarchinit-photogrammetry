# Benchmark

pyarchinit-photogrammetry, Agisoft Metashape Pro and ODM (the engine of WebODM) on the same photos of
a tell, on the same computer, with the same resolutions: the time of every step and the quality of the
results.

## How

* **Computer**: Apple M4 Pro (8 performance and 4 efficiency cores), 24 GB, macOS 27.
  **Metashape uses the GPU (Metal) as well as the CPU; pyarchinit-photogrammetry and ODM use the CPU
  only**: pyarchinit-photogrammetry natively, with its native core, ODM in Docker (a Linux virtual
  machine with 11 cores and 8 GB).
* **Data**: Tell Sultan (Syria), a DJI drone with an FC8482 camera (4032 × 2268 pixels):
  the 57 photos of the [tutorial](tutorials/tell.md) (about 110 m above the ground, 3.6 cm per pixel)
  and the whole survey of 425 photos (nadir flights at about 40, 65 and 110 m, and oblique photos).
* **Settings**: features found on the photos at half size (Metashape *Medium* accuracy, ppg *High*,
  ODM `--feature-quality high`), depth maps at a quarter (Metashape *Medium*, ppg *Medium*, ODM
  `--pc-quality medium`), moderate filtering, DEM from the dense cloud, orthophoto on the DEM, mosaic
  blending; ODM's DEM and orthophoto at the resolutions Metashape chose. Georeferencing by the GPS of
  the drone: no ground control on Tell Sultan.
* **Quality**: the products of each program are brought onto those of Metashape - a shift found by
  phase correlation of the hillshades, then refined together with an offset, a tilt and a height scale
  by least squares surface matching: each program places a survey known only from the drone GPS a
  little differently - and compared: NMAD and 95th percentile of the height differences, the ground
  covered, the sharpness of the orthophotos where all of them cover it. The reprojection error is the
  RMS over all the observations of the tie points, in pixels of the photos.

The scripts are in the repository (`benchmark/`): run them on your data and computer.

!!! note "GPU and CPU"
    On a Mac, Metashape runs its heaviest steps - the matching of the photos and the depth maps - on
    the GPU through Metal, besides the CPU; pyarchinit-photogrammetry and ODM run everything on the CPU.
    The times compare the programs as they run on this computer, not the algorithms on the same
    hardware.

## 57 photos (the tutorial)

Times in seconds:

| Step | pyarchinit-photogrammetry 1.1 (CPU) | Metashape Pro 2.1.3 (GPU + CPU) | ODM 3.6.2 (CPU) |
|---|---:|---:|---:|
| Alignment | 190 | 18 | 119 |
| Depth maps and dense cloud | 158 | 79 | 170 |
| Mesh | 32 | 32 | 51 ¹ |
| Texture | 8 ⁶ | 19 | 54 |
| DEM | 8 | 4 | 3 |
| Orthophoto | 20 | 26 | 7 |
| **Total** | **416** | **179** | **418** ² |

Results:

| | pyarchinit-photogrammetry | Metashape | ODM |
|---|---:|---:|---:|
| Photos aligned | 57 / 57 | 57 / 57 | 57 / 57 |
| Tie points | 316,000 | 65,000 | 55,000 |
| Reprojection error (RMS) | 0.97 px | 1.05 px | - |
| Dense cloud | 3.1 M points | 4.2 M | 6.1 M |
| DEM | 19 cm | 15 cm | 15 cm |
| DEM against Metashape's: NMAD / 95th percentile | 14.4 / 52 cm | - | 14.2 / 75 cm |
| Ground covered by the DEM ³ | 95 % | 100 % | 99 % |
| Orthophoto | 3.6 cm | 3.8 cm | 3.8 cm |
| Orthophoto sharpness ⁴ | 58.3 | 52.8 | 35.0 |
| Ground covered by the orthophoto ⁵ | 64 % | 79 % | 90 % |

¹ the 2.5D mesh ran out of memory once and was made again with fewer processes.
² with the georeferencing and the report.
³ cells of the common grid with a height, Metashape's DEM = 100 %.
⁴ variance of the Laplacian of the grey image at 2 cm, median of the tiles covered by all of them.
⁵ share of tiles of the common footprint; ODM fills to the edges of the flight.
⁶ without the colour correction of the photos, as Metashape without its colour calibration (with it,
as in the window: 27 s).

![Hillshades of the three DEMs, 57 photos](img/benchmark/sultan57-hillshades.png)

Notes:

* Metashape matches and aligns on the GPU: 18 s, against 119 s for ODM and 190 s for ppg on the
  CPU.
* ppg computes the depth maps of a survey from the air with PatchMatch in its native core: no
  contour-like terraces and stripes, which two-view stereo drew on this tell.
* ODM's orthophoto is the softest: it is draped on a 2.5D mesh simplified to 400,000 faces; its DSM
  fills the gaps with straight-sided patches (visible in the north).
* The line across the north-west of ppg's hillshade is the edge of the ground seen by three or more
  photos: beyond it, where two photos see the ground, the surface of ppg sits about 20 cm higher. The
  dense cloud of ppg keeps the points seen by at least three photos, and every plane of its depth maps
  needs two photos seeing it (before this benchmark that margin was pitted by a few wrong points).
* A block flown at one height and placed by the GPS of the drone alone does not fix the scale of its own
  heights: against Metashape's DEM of the whole survey (425 photos, three heights), the heights of the
  57-photo surveys of *both* programs are stretched by about 3 % (a height scale of 0.97 brings them
  back, with a residual dome of 23 cm NMAD for Metashape and 34 cm for ppg). The comparison above,
  between the products of the same 57 photos, does not see it.
* ppg's orthophoto is the sharpest: for every spot it takes the photo that sees it at the highest
  resolution.

## 425 photos (the whole survey)

Times in seconds:

| Step | pyarchinit-photogrammetry 1.1 (CPU) | Metashape Pro 2.1.3 (GPU + CPU) | ODM 3.6.2 (CPU) |
|---|---:|---:|---:|
| Alignment | 1,775 | 205 | 903 |
| Depth maps and dense cloud | 1,111 | 872 | ⁷ |
| Mesh | 216 | 278 | |
| Texture | 122 | 131 | |
| DEM | 71 | 16 | |
| Orthophoto | 112 | 146 | |
| **Total** | **3,407** (57 min) | **1,648** (27 min) | did not finish ⁷ |

⁷ in its virtual machine of 8 GB ODM ran out of memory while fusing the depth maps of the 425 photos,
by tiles too, and stopped without a dense cloud.

Results:

| | pyarchinit-photogrammetry | Metashape |
|---|---:|---:|
| Photos aligned | 425 / 425 | 425 / 425 |
| Pairs of photos matched | 6,060 | - |
| Tie points | 1,985,000 | 556,000 |
| Reprojection error (RMS) | 1.22 px | 1.34 px |
| Dense cloud | 22.2 M points | 15.5 M |
| Mesh | 1.48 M faces | 1.24 M |
| DEM | 7.1 cm | 8.2 cm |
| DEM against Metashape's: NMAD / 95th percentile | 12.0 / 39 cm | - |
| Ground covered by the DEM ³ | 98 % | 100 % |
| Orthophoto | 2.0 cm | 2.0 cm |
| Orthophoto sharpness ⁴ | 636 | 347 |

![Hillshades of the DEMs, 425 photos](img/benchmark/sultan425-hillshades.png)

Notes:

* The alignment is where ppg is far behind: 6,060 pairs of photos matched on the CPU with
  40,000 points per photo, then all the cameras placed at once; Metashape matches on the GPU. After
  the alignment the two are close: 1,632 s for ppg, 1,443 s for Metashape.
* ppg's dense cloud keeps the points seen by at least three photos: at the margins of the flights it is
  sparse and the DEM is interpolated there (smoother than Metashape's), while it is far denser where
  dozens of photos overlap (the low flights over the centre).
* Once each survey is placed on the other, the two DEMs agree within 12 cm (NMAD). The broader
  differences - a ring 15-25 cm lower than Metashape around the centre of the tell, the borders higher -
  are a difference of self-calibration: ppg refines two radial coefficients with the principal point
  fixed, Metashape three radial and two tangential coefficients and the principal point; placed by the
  GPS of the drone alone, neither survey can be called right. The depth maps are not the cause:
  computed from Metashape's alignment, ppg's agree with Metashape's tie points within 2 cm per photo.
  The benchmark on Tell Tuqan below, with ground control points, measures the absolute accuracy of
  both programs.

## 997 photos with ground control (Tell Tuqan)

Tell Tuqan (Syria), the flight of the targets: 997 photos of 8064 × 4536 pixels (48 Mpx, DJI FC8482)
and 11 coded targets measured on the ground - 7 control points, 4 check points, 2 cm horizontal and 3 cm
vertical accuracy - with the same marker projections (measured in Metashape by the surveyor) given to
both programs. Settings as above scaled to the 48 Mpx photos: features on the photos at a quarter
(2016 px: Metashape *Low*, ppg *Medium*), depth maps at an eighth (1008 px), moderate filtering, mesh
with a medium face count, texture 4096, DEM from the dense cloud, orthophoto on the DEM. ODM was not
run (see the 425 photos).

Times in seconds:

| Step | pyarchinit-photogrammetry 1.1 (CPU) | Metashape Pro 2.1.3 (GPU + CPU) |
|---|---:|---:|
| Matching | 3,391 | 972 |
| Alignment | 23,110 ⁸ | 455 |
| Optimization on the control points | 2,231 | 25 |
| Depth maps and dense cloud | 2,884 | 4,218 |
| Mesh | 244 | 2,579 |
| Texture | 3,076 | 2,621 |
| DEM | 166 | 85 |
| Orthophoto | 10,903 ⁹ | 2,401 |
| **Total** | **46,007** (12 h 47 min) | **13,355** (3 h 43 min) |

⁸ incremental alignment: the global one, used for the 425 photos, bent this ring (see the notes).
⁹ three hours of which the disk, not the program, set the pace (see the notes).

Results:

| | pyarchinit-photogrammetry | Metashape |
|---|---:|---:|
| Photos aligned | 988 / 997 | 995 / 997 |
| Tie points | 4,681,000 | 1,244,000 |
| Control points: RMSE (horizontal / vertical) | 2.7 cm (0.3 / 2.7) | 2.7 cm (2.3 / 1.6) |
| Check points: RMSE (horizontal / vertical) | 66.3 cm (51.4 / 42.0) | 28.6 cm (13.4 / 25.2) |
| Dense cloud | 37.6 M points | 78.7 M points |
| Mesh | 2.51 M faces | 4.61 M faces |
| DEM | 5.5 cm | 4.1 cm |
| DEM against Metashape's: NMAD / 95th percentile ¹⁰ | 50 cm / 1.36 m | - |
| Ground covered by the DEM ³ | 91 % | 100 % |
| Orthophoto | 8.0 mm | 5.1 mm |
| Orthophoto sharpness ⁴ | 915 | 780 |

¹⁰ compared as they are, without the registration used on Tell Sultan: both surveys stand on the same
control points.

![Hillshades of the DEMs, 997 photos](img/benchmark/tuqan-hillshades.png)

![Height differences, ppg minus Metashape, in metres](img/benchmark/tuqan-dem-differences.png)

Notes:

* The flight of the targets is a closed ring around the tell, some 650 m across, flown once with the
  camera on the targets and without RTK (the GPS of the photos is accurate to 5 m horizontally and 10 m
  vertically). What both programs reconstruct is the strip of ground under the ring: the tell in the
  middle is not covered, as the hillshades show.
* The alignment is the whole story of this survey. ppg's global SfM, which did well on the 425 photos,
  placed every photo of the ring but bent it: consistent locally (the targets re-projected within 1-3
  pixels) while the scale changed from one side of the ring to the other, and the targets ended up
  some 90 m from their coordinates. The incremental mapper got the ring right at the cost of 6.4
  hours, and its block was still bent by a few metres before the control points (2.6 m RMSE on the
  control targets, a group of 25 photos 14 m off): a free ring of a thousand oblique photos, without
  strips across it, drifts. Metashape uses the GPS of the photos within its alignment and does not.
  Adding the GPS of the photos to ppg's bundle adjustment afterwards only brought the control error
  from 2.6 to 1.8 m: the tie points hold the bend.
* The optimization on the control points had to reach targets metres away from the block: the targets
  are taken as fixed points in steps, on a subset of the strongest tracks, then the whole block is
  re-triangulated and adjusted (37 min). It brings the control points to 2.7 cm, as Metashape, but the
  check points to 66 cm against Metashape's 29 cm: three of the four check targets stay 25 cm to 1.3 m
  off (the fourth 2 cm), the worst in the group of 25 photos that remains a metre from Metashape's
  cameras.
* The two DEMs, compared as they are, differ by 50 cm (NMAD): not noise, but waves of ±0.3-1 m along
  the ring in the difference map - the residual bend of ppg's block - with agreement where the ring
  passes the control targets. The ten RTK points measured on the ground fall outside the strip: ppg's
  DEM has no cell at any of them (3 to 80 m from the nearest), Metashape's reaches three, two within
  0.6 m and one 2 m off in a patch it interpolated across a gap (the spikes in its hillshade).
* After the alignment, ppg's depth maps, dense cloud, mesh, texture and DEM take 6,370 s on the CPU
  against Metashape's 9,503 s with the GPU, and its orthophoto is the sharper one. The orthophoto
  time is not the program's: the 28 GB of 48-megapixel photos do not fit in the 24 GB of the Mac, ppg
  re-reads them for every tile, and the external SSD they sit on was shared with other work for most
  of those three hours (alone, the last stretch ran five times faster). A photo cache sized on the
  memory rather than on the workers is the fix to make.
* ODM was not run (see the 425 photos).

## What changed in pyarchinit-photogrammetry

These tests changed the program; the numbers above are those of the version with the changes:

* alignment of many photos: the pairs that overlap found first on photos reduced to 1024 pixels, the
  20 strongest of each photo matched in full, all the cameras placed at once (global SfM): 57 photos
  472 → 177 s, 425 photos 42 → 27 min, the same cameras within 1 cm (RMS);
* depth maps of surveys from the air with PatchMatch, every plane confirmed by two photos, and a fusion
  that keeps the density of the depth maps: 57 photos 1.1 → 3.1 M points, no contour-like terraces;
* DEM: resolution from the ground the cloud covers; spikes, stray points and the pits of a few wrong
  points removed; the sparse margins interpolated as one surface instead of scattered dots: 425
  photos, 45 → 64 % of the common ground, 95th percentile of the differences 87 → 39 cm;
* optimization on control points that lie metres from the block: the targets are reached in steps
  (continuation) on the strongest tracks, then the whole block is re-triangulated and adjusted; a single
  adjustment from that far diverged (Tell Tuqan: control targets from 2.6 m to 2.7 cm).
