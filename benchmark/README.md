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
