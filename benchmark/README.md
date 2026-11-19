# Benchmark scripts

The same photos through pyarchinit-photogrammetry, Agisoft Metashape Pro and ODM (the engine of
WebODM), each step timed, with the same resolutions: features on the photos at half size, depth maps
at a quarter, moderate filtering, DEM from the dense cloud, orthophoto on the DEM. The results on Tell
Sultan are in the documentation (*Benchmark*).

```bash
python benchmark/bench_ppg.py PHOTOS OUT/ppg
/Applications/MetashapePro.app/Contents/MacOS/MetashapePro -r benchmark/bench_metashape.py PHOTOS OUT/metashape
benchmark/bench_odm.sh PHOTOS OUT/odm sultan --dem-resolution 15 --orthophoto-resolution 3.8   # Docker, opendronemap/odm
python benchmark/compare.py OUT/compare \
    metashape=OUT/metashape/dem.tif,OUT/metashape/orthomosaic.tif \
    ppg=OUT/ppg/dem.tif,OUT/ppg/orthomosaic.tif \
    odm=OUT/odm/sultan/odm_dem/dsm.tif,OUT/odm/sultan/odm_orthophoto/odm_orthophoto.tif
```

With ground control (Tell Tuqan: the targets measured in the field, the marker projections of the
surveyor's Metashape project read by both programs, `BENCH_CRS` the projected CRS of the targets):

```bash
BENCH_CRS=EPSG:xxxxx python benchmark/bench_ppg_tuqan.py PROJECT.psx PHOTOS targets.csv rtk.csv OUT/ppg
BENCH_CRS=EPSG:xxxxx python benchmark/bench_ppg_tuqan_gpu.py PROJECT.psx PHOTOS targets.csv rtk.csv OUT/ppg_gpu   # GPU + GPS priors
BENCH_CRS=EPSG:xxxxx MetashapePro -r benchmark/bench_metashape_tuqan.py PROJECT.psx PHOTOS targets.csv OUT/metashape
BENCH_CRS=EPSG:xxxxx MetashapePro -r benchmark/bench_metashape_rebuild_ortho.py PHOTOS OUT/metashape   # DEM and orthomosaic as BigTIFF
```

`targets.csv`: `label,east,north,height,role` (role `control` or `check`); `rtk.csv`: `name,east,north,height`
points on the ground checked against the DEM.

* `bench_ppg.py`, `bench_metashape.py`: one process, one JSON of times and statistics
  (`ppg_times.json`, `metashape_times.json`), with the reprojection error measured the same way (RMS
  over all the observations of the tie points). Metashape scripts run inside its window (the
  application has no headless mode on macOS) and need a Pro licence; the script uses only its public
  Python API. The products are written in the UTM zone of the photos, from their GPS.
* `bench_odm.sh`: ODM times its stages itself (`benchmark.txt` in the project folder); the total of
  the run is printed at the end. Give it the DEM and orthophoto resolutions of the reference (those
  Metashape chose, in cm), as above for the 57 photos of the tutorial.
* `compare.py`: the first product is the reference. Surveys placed only by the GPS of the drone differ
  by a shift, a tilt and a scale of the heights: they are estimated and removed (phase correlation of
  the hillshades on a 1 m grid, then least squares surface matching at 10 cm) before the DEMs are
  compared (NMAD, 95th percentile, ground covered); the orthophotos are compared by the ground they
  cover and their sharpness (variance of the Laplacian at 2 cm) in the tiles that all of them cover.

Satellite stereo against lidar (the JHU/APL multi-view stereo benchmark, WorldView-3 chips with their
RPCs and the lidar of each site; public release):

```bash
python benchmark/satellite_wv3.py WV3_MVS_FOLDER OUT [--site Explorer] [--scenes 12JUN15,18JUN15,...] [--max-pairs 6]
```

* `satellite_wv3.py`: for every site the surface from the chosen scenes and from each of its pairs
  alone, with and without the relative correction of the RPCs, measured against the lidar inside the
  site polygon (median absolute error, NMAD, RMSE, completeness = share of lidar cells within 1 m)
  as they come out and after the evaluation alignment (whole-cell horizontal shift within 3 m and the
  median vertical offset); `report.json` in OUT.

## Measuring a version with `ppg bench`

`ppg bench` is a command of the program and measures the whole of it, from the photos to the products;
it is not `ppg-bench`, the Rust binary of `native/bench`, which times the kernels of the engine alone,
without Python. A run does the steps of a set of photos in a child process of its own, times each step,
samples the memory of the child and of its children every half second, keeps the stages the engine
records and the results of the run (photos aligned, reprojection error, the errors on the references),
and writes one record, a JSON file. A folder of records gives the table of a version, or of two side
by side.

```bash
ppg bench run PHOTOS OUT --name NAME [--steps align|all] [--mapper auto|incremental|global] [--repeat N] [--keep] [--records DIR]
ppg bench synthetic trench|ring OUT [--repeat 3] [--keep] [--records DIR]
ppg bench adjust PROJECT --name NAME [--solvers native,colmap,scipy] [--repeat 3] [--records DIR]
ppg bench table DIR
```

* `run`: `--name` (required) names the run and its records (letters, digits, `.`, `_`, `-`): say the
  set and what is measured, e.g. `trench-28-align`. `--steps align` (the default) matches, aligns
  and, when the photos carry a GPS position, georeferences by those positions; `all` detects the
  default targets (`aruco_4x4_50`) first and goes on with the depth maps, the dense cloud, the model,
  the texture, the DEM and the orthophoto: the chain of `ppg process` with its defaults, which the
  record writes out. A reference file or other targets are not options of `run` (the synthetic sets
  pass them; a real set is measured as its photos come). Each of the `--repeat` runs works in
  `OUT/NAME-<i>`: `ppg bench` prints nothing while a run lasts; the step that is running and those
  done are in `result.json` there, rewritten as each step starts and ends, and what the child prints
  (the warnings of the program and of COLMAP) in `child.log` - both readable meanwhile. The folder
  is removed after unless `--keep`, or the run failed: then it stays with its log, its result (the
  error of the step as the child wrote it, before the scrub of the record) and its project, and
  `ppg bench` says so by the folder's name; the records go to `OUT`, or to `--records DIR`. The run
  needs the `bench` extra (`pip install "pyarchinit-photogrammetry[bench]"`: psutil, for the
  memory of the child). `--mapper incremental|global` asks the alignment for that mapper (`auto`,
  the default, picks as `ppg process` does); the record keeps it as `mapper_requested` beside the
  `mapper` the run used, and the table compares it. `--seed N` (0 or more) fixes the seed of the
  RANSAC of the geometric verification of the matches (`PPG_RANSAC_SEED` in the child): runs with
  the same seed verify the same pairs, so repeats compare like with like; the record keeps it as
  `ransac_seed`, `-1` without it (a seed from the clock). The seed fixes the verification only: on a
  computer that shares the pairs between the GPU and the CPU, and since COLMAP's threaded extractor
  numbers the photos in the order it finishes them, repeated runs can still differ in their pairs.
* `synthetic trench|ring`: writes the set in a folder of its own, `OUT/synthetic-trench-set/` or
  `OUT/synthetic-ring-set/` (its photos and `gcp.csv`), which must not exist yet, and runs it as
  `synthetic-trench` (28 photos, all the steps of `ppg process` with its defaults - the depth maps at
  `medium`, where the metric tests build them at `high` -, the targets detected and `gcp.csv`
  imported: the set and the georeferencing of the metric tests) or `synthetic-ring` (72 photos around
  a mound, a GPS position in the EXIF of each, the steps of `align` with the targets detected,
  georeferenced by those positions). The set stays after the runs, beside its records; to run the
  same set again in the same `OUT`, delete its folder (`rm -r OUT/synthetic-trench-set`) or give
  another `OUT`. The results of these runs add the errors against the truth of the set, the keys that
  start with `truth_`: for the trench `truth_control_rmse` and `truth_check_rmse` (targets 4 and 5
  left out of the fit and measured, as the metric tests do); for the ring `truth_target_rmse` over
  the `truth_targets` targets found, `truth_camera_rmse`, `truth_camera_height_mean` (estimated minus
  true: heights that landed too high or too low keep their sign) and `truth_camera_height_rmse`,
  against the true targets and poses of the scene. The two sets are synthetic: their coordinates are
  made up and name no place. `--keep` keeps the work folder of each run with its project
  (`OUT/synthetic-trench-<i>/project.ppg`): what `adjust` takes.
* `adjust PROJECT --name NAME`: the native bundle adjustment, COLMAP's and scipy's on the
  reconstruction of a kept project (one of `run` or `synthetic` with `--keep`, or any aligned
  project), on the same plain problem - the tie points, every calibration free but the principal
  point, the first photo and the farthest from it held -, each solver in a child process of its own:
  the steps `bundle_native`, `bundle_colmap`, `bundle_scipy` with the seconds of the solve alone
  (COLMAP's with Ceres' set-up of its problem, as the native solver's hold the core's) and the peak
  memory of the child. The project is opened and never written: each solver reads the
  reconstruction from disk for itself, COLMAP adjusts its copy in memory. The results: `photos`,
  `points`, `observations`, `bundle_cost_initial`; per solver `bundle_cost_<s>`, by one evaluator
  for the three (the native evaluation of the problem at the solver's end point), `bundle_iterations_<s>`,
  `bundle_gradient_max_<s>` and `bundle_converged_<s>` (1 or 0); `bundle_gap_colmap` and
  `bundle_gap_scipy`, the cost of the native solver against the other's, relative (the gate of the
  native engine asks at most `1e-4` against COLMAP on the same problem and not above scipy's);
  `bundle_sigma_ratio_scipy`, the largest gap between the native and the scipy parameters over the
  photo and intrinsic columns, in sigmas of the reduced camera system at scipy's end point (a figure,
  to read with `bundle_converged_scipy`: where scipy stopped at its budget the gap is not a gate).
  The settings keep the solvers asked, the linear solver the native solver and COLMAP used,
  `max_iterations` (200) and the version of pycolmap. `--solvers` names some of the three
  (`native,colmap`); scipy, the reference of the tests, is left out above 100 photos (hours on a
  large block), with a warning in the record. Tie points seen by fewer than two photos, which no
  solver can adjust, are left out of the problem of all three, said in the warnings. The records,
  and the work folder of each run (its job, the child's log, its result; removed after unless the
  run failed), go to `--records DIR`, by default the current folder; the set of the record is the
  project's photos and the disk the project's.
* `table DIR`: per run, program and step the median, the least and the most of the seconds of the
  valid runs, the highest peak memory, the median of each stage and the engine; then the results as
  medians per run and program (a result present in some of the runs only shows its count beside its
  median: `0.002 (2 of 3)`); last, the records left out, counted by reason. The `engine` column is
  the engine the step ran with (`classic`, `native`; `classic/native` when the runs of the row
  differ): it is what the table of 2.0.0 compares. A row whose valid runs differ in their settings
  (other than what records what a run did rather than what it was asked: the version of pycolmap,
  the count of pairs - above 40 photos the preselection's RANSAC has a random seed - and their split
  between the GPU and the CPU, the mapper and its fallback), in their machine (system, architecture,
  processor, memory) or in their set (how many photos, their bytes) says `mixed settings`, `mixed
  machines` or `mixed sets` and is no baseline.

**What a record holds**: the name of the run, its start, the version of the program; the machine
(system, processor, cores, memory, GPU adapters, load, battery, whether Dropbox runs); the disk of the
photos (bus, link speed, read rate, solid state); the set (how many photos, their bytes, how many
online-only, how many with GPS); the settings of the engine (matching and its parameters, mapper, GPU,
version of pycolmap, `photo_cache_mb`, georeferencing method, targets); the steps (seconds, peak
memory in MB, stages, engine; for the orthophoto the counts of its photo cache: reads, hits, rereads);
the results; the warnings; whether it is valid and why not; the error of a run that failed. **What a record never holds**: paths, coordinates, CRS codes, photo names, the
name of the host or of the user. The text of an error is scrubbed of them before it is written, and
a record that would still hold a path is refused. Records can be shared as they are.

**When a run is not valid** (the record says so, with its reasons, and the table leaves it out): the
disk of the photos is on a slow link (below 5000 Mb/s: USB 2), it reads slowly (below 100 MB/s),
photos were online-only (a cloud folder not made available offline), the run failed (a step raised,
the child died, the run was cancelled) or the errors against the truth could not be measured. A
running Dropbox, a load above 2.0 and a laptop on its battery only warn: the record says so, and the
reader decides.

**Why a figure needs three runs**: the machine disturbs a run - a background job, thermal throttling,
a cache that was cold the first time - and one run cannot tell; two that differ cannot say which one
was disturbed. The median of three stands. `table` writes `not a baseline (n < 3)` on a row with
fewer; a figure given as a baseline is the median of three valid runs of the same set with the same
settings, on the reference machine (Mac mini M4 Pro, 12 cores, 24 GB, memory budget 16 GB).

**The photo cache of the orthophoto**: the photos it reads again are kept in a cache whose size is
`PPG_PHOTO_CACHE_MB`, a whole number of megabytes, kept in the record as `settings.photo_cache_mb`
(`null` when unset: a share of the memory of the machine). Whatever the size, the cache always holds
a floor of photos, `max(12, 3 × workers)`, so that the workers never wait for a photo another one
has just read. `PPG_PHOTO_CACHE_MB=0` leaves that floor alone and nothing more, which is the cache of
version 1.10: that is how the 1.10 baseline is measured.
