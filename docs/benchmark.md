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
| Alignment | 177 | 18 | 119 |
| Depth maps and dense cloud | 128 | 79 | 170 |
| Mesh | 42 | 32 | 51 ¹ |
| Texture | 8 ⁶ | 19 | 54 |
| DEM | 5 | 4 | 3 |
| Orthophoto | 22 | 26 | 7 |
| **Total** | **380** | **179** | **418** ² |

Results:

| | pyarchinit-photogrammetry | Metashape | ODM |
|---|---:|---:|---:|
| Photos aligned | 57 / 57 | 57 / 57 | 57 / 57 |
| Tie points | 316,000 | 65,000 | 55,000 |
| Reprojection error (RMS) | 0.97 px | 1.05 px | - |
| Dense cloud | 3.3 M points | 4.2 M | 6.1 M |
| DEM | 19 cm | 15 cm | 15 cm |
| DEM against Metashape's: NMAD / 95th percentile | 15.3 / 68 cm | - | 14.4 / 74 cm |
| Ground covered by the DEM ³ | 105 % | 100 % | 100 % |
| Orthophoto | 3.6 cm | 3.8 cm | 3.8 cm |
| Orthophoto sharpness ⁴ | 58.5 | 52.3 | 35.1 |
| Ground covered by the orthophoto ⁵ | 66 % | 75 % | 86 % |

¹ the 2.5D mesh ran out of memory once and was made again with fewer processes.
² with the georeferencing and the report.
³ cells of the common grid with a height, Metashape's DEM = 100 %.
⁴ variance of the Laplacian of the grey image at 2 cm, median of the tiles covered by all of them.
⁵ share of tiles of the common footprint; ODM fills to the edges of the flight.
⁶ without the colour correction of the photos, as Metashape without its colour calibration (with it,
as in the window: 27 s).

![Hillshades of the three DEMs, 57 photos](img/benchmark/sultan57-hillshades.png)

Notes:

* Metashape matches and aligns on the GPU: 18 s, against 119 s for ODM and 177 s for ppg on the
  CPU.
* ppg computes the depth maps of a survey from the air with PatchMatch in its native core: no
  contour-like terraces and stripes, which two-view stereo drew on this tell.
* ODM's orthophoto is the softest: it is draped on a 2.5D mesh simplified to 400,000 faces; its DSM
  fills the gaps with straight-sided patches (visible in the north).
* The line across the north-west of ppg's hillshade is a step of up to 30-40 cm between ground seen by
  different flight lines (see the notes on the 425 photos).
* ppg's orthophoto is the sharpest: for every spot it takes the photo that sees it at the highest
  resolution.

## 425 photos (the whole survey)

Times in seconds:

| Step | pyarchinit-photogrammetry 1.1 (CPU) | Metashape Pro 2.1.3 (GPU + CPU) | ODM 3.6.2 (CPU) |
|---|---:|---:|---:|
| Alignment | 1,634 | 205 | 903 |
| Depth maps and dense cloud | 1,118 | 872 | ⁷ |
| Mesh | 197 | 278 | |
| Texture | 35 | 131 | |
| DEM | 77 | 16 | |
| Orthophoto | 115 | 146 | |
| **Total** | **3,178** (53 min) | **1,648** (27 min) | did not finish ⁷ |

⁷ in its virtual machine of 8 GB ODM ran out of memory while fusing the depth maps of the 425 photos,
by tiles too, and stopped without a dense cloud.

Results:

| | pyarchinit-photogrammetry | Metashape |
|---|---:|---:|
| Photos aligned | 425 / 425 | 425 / 425 |
| Pairs of photos matched | 6,069 | - |
| Tie points | 1,985,000 | 556,000 |
| Reprojection error (RMS) | 1.21 px | 1.34 px |
| Dense cloud | 24.1 M points | 15.5 M |
| Mesh | 1.61 M faces | 1.24 M |
| DEM | 7.0 cm | 8.2 cm |
| DEM against Metashape's: NMAD / 95th percentile | 11.3 / 39 cm | - |
| Ground covered by the DEM ³ | 101 % | 100 % |
| Orthophoto | 2.0 cm | 2.0 cm |
| Orthophoto sharpness ⁴ | 658 | 339 |

![Hillshades of the DEMs, 425 photos](img/benchmark/sultan425-hillshades.png)

Notes:

* The alignment is where ppg is far behind: 6,069 pairs of photos matched on the CPU with
  40,000 points per photo, then all the cameras placed at once; Metashape matches on the GPU. After
  the alignment the two are close: 1,543 s for ppg, 1,443 s for Metashape.
* ppg's dense cloud keeps the points seen by at least three photos: at the margins of the flights it is
  sparse and the DEM is interpolated there (smoother than Metashape's), while it is far denser where
  dozens of photos overlap (the low flights over the centre).
* Once each survey is placed on the other, the two DEMs agree within 11.3 cm (NMAD), but ppg's has
  broad offsets of 10-30 cm that follow the flight lines: the depth maps of photos of different strips
  disagree by about 10 cm on average, up to 30-40 cm in places, and where the photos that see the
  ground change the DEM steps (in the hillshade of the 57 photos too, in the north-west). They come
  from ppg's depth maps, not from its alignment: computed from Metashape's alignment they disagree in
  the same way. This is being investigated; the benchmark on Tell Tuqan, which has ground control
  points, will also measure the absolute accuracy of the two programs.

## What changed in pyarchinit-photogrammetry

These tests changed the program; the numbers above are those of the version with the changes:

* alignment of many photos: the pairs that overlap found first on photos reduced to 1024 pixels, the
  20 strongest of each photo matched in full, all the cameras placed at once (global SfM): 57 photos
  472 → 177 s, 425 photos 42 → 27 min, the same cameras within 1 cm (RMS);
* depth maps of surveys from the air with PatchMatch, and a fusion that keeps the density of the depth
  maps: 57 photos 1.1 → 3.3 M points, no contour-like terraces;
* DEM: resolution from the ground the cloud covers; spikes and stray points removed; the sparse
  margins interpolated as one surface instead of scattered dots: 425 photos, 45 → 64 % of
  the common ground, 95th percentile of the differences 87 → 39 cm.
