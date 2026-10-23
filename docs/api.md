# Python API

```python
import pyarchinit_photogrammetry as ppg
```

## Document and chunks

| Call | Description |
| --- | --- |
| `ppg.Document(path=None)`, `Document.open(path)`, `doc.save(path=None)` | project file `.ppg` + folder `.ppg.files/` |
| `doc.add_chunk(label)`, `doc.chunks`, `doc.chunk` | chunks; `doc.chunk` is the active one (assign to change it) |
| `ppg.app.document` | the project shared by scripts, CLI (`ppg run`) and GUI |

## Chunk workflow

One row per method of `Chunk`, in the order of the workflow; the parameters that matter most. The
longer explanations follow the table.

| Method | What it does |
| --- | --- |
| `add_photos(paths, recursive=False)` | files or folders; sensors grouped by body, lens and resolution; the EXIF GPS becomes the camera reference |
| `detect_markers(target_type="aruco_4x4_50")` | markers `target <code>` with their projections |
| `match_photos(quality, keypoint_limit, tiepoint_limit, generic=None, reference=None, apply_masks="keypoints", guided_matching=False, exclude_stationary=False, reset=True, gpu=None, matcher="sift")` | keypoints and matches, with the choices of Metashape's *Align Photos* (*Pairs*, below); SIFT or learned points (*Learned points*, below) |
| `align_cameras(mapper="auto", use_gps=None, adaptive_fitting=False, reset=True, tiepoint_limit=None, ba_global_ratio=None, ba_global_refinements=None)` | the photos placed, by the global, the incremental or the hierarchical mapper (*Mappers*, below), held to the GPS of the photos; the transform is reset and the georeference made again when references exist |
| `optimize_cameras(fit_principal_point=False, use_markers=True, marker_weight=4)` | bundle adjustment of poses, calibration and tie points, on the control points and scale bars when there are any (*Camera optimisation*, below); returns the reprojection error (px) |
| `import_reference(csv, crs, columns="nxyz")` | references of markers and cameras from a text file: `n` label, `x y z`, `a` accuracy, `-` skip |
| `export_reference(csv)` | the references to a text file |
| `add_marker(...)`, `add_marker_projection(...)` | markers by hand (how they are placed: *Scale bars*) |
| `add_scalebar(marker_a, marker_b, distance)` | a scale bar between two markers (*Scale bars*) |
| `triangulate_markers()` | the markers placed from their projections (two photos or more), or on the surface from one |
| `update_transform(use_cameras=None)` | the similarity onto the references (weighted, 7 parameters); returns the control and check RMSE and the error of every marker and bar |
| `build_depth_maps(quality, filter_mode, backend)` | depth maps; backends `auto`, `cpu`, `patchmatch`, `colmap`, `openmvs` |
| `build_point_cloud()` | the dense cloud from the depth maps |
| `build_model(source, surface, face_count, voxel_size, vertex_colors)` | the mesh from `point_cloud`, `tie_points` or `depth_maps` (*Mesh from depth maps*, below) |
| `build_texture(texture_size, page_size=8192)` | the texture of the model, in pages above `page_size` (*Texture pages*, below) |
| `build_dem(resolution, source)` | the DEM, a GeoTIFF in the chunk CRS |
| `build_orthomosaic(resolution, surface, blending, colour_correction, ghosting, fill_holes)` | the orthomosaic on the DEM or on the model (*Seamless orthomosaic and colour correction*) |
| `calibrate_colours()` | exposure and vignetting of the photos (*Seamless orthomosaic and colour correction*) |
| `export_point_cloud(path)`, `export_model(path)`, `export_dem(path)`, `export_orthomosaic(path)` | the products in world coordinates (formats and options: *Exports for the web*, *Confidence, cleaning and cropping*) |
| `export_cameras(fmt="csv")` | the cameras, `csv` or `colmap` |
| `generate_report(path)` | the HTML report of the chunk |

Every processing method accepts `progress=callback(fraction, message)`.

### Pairs

The choices are those of Metashape's *Align Photos*. `quality` is the accuracy: `ultra`, `high`,
`medium`, `low` or `lowest`, the photos at full size or reduced to 1/2, 1/4, 1/8, 1/16 of their
side. `generic` is the generic preselection: the pairs that overlap are found first on the photos
reduced to 1024 pixels and the 20 strongest pairs of each photo are matched in full (`None`: from 40
photos up; below, every pair is cheap enough). `reference` is the reference preselection:
`source` (every photo with its `neighbours` nearest by the reference positions, the GPS of the
drone), `estimated` (by the positions a previous alignment estimated; photos not aligned yet with
every other), `sequential` (the photos before and after it in the order of the capture times: a
video, a walk around an object). With both preselections on, the pairs of both are matched; with
neither, every pair. The older `pair_selection` word (`auto`, `exhaustive`, `generic`,
`sequential`, `reference`) stands in when `generic` and `reference` are not given.

`apply_masks` applies the masks of the photos to the `keypoints` (no point is detected in the masked
parts), to the `tiepoints` (a point masked in one photo is dropped everywhere after the alignment:
masking a few photos is enough) or to `none`. `guided_matching` searches the matches again with the
geometry of each pair (more tie points, slower); `exclude_stationary` drops the matches that do not
move between two photos (the still background of a turntable). `reset=False` keeps the matches and
the alignment there are and only matches the photos added since; `align_cameras(reset=False)` then
registers them into the block, one photo after the other (when nothing can be kept, everything is
done again). `meta["sfm"]` records what ran: `pair_selection` (`exhaustive`, `generic`,
`generic+source`, ...), `pairs`, `apply_masks`, `masked_tie_points`, `added`, `matcher`.

### Learned points

`matcher` picks the points: `sift` (COLMAP's SIFT, on the GPU when there is one) or key points found
by a small neural network and matched by LightGlue, `disk` or `aliked` (`engine.learned`, kornia on
PyTorch: the `[learned]` extra, or the separate environment of SAM 2.1 that the application installs
on request). They find matches where SIFT finds few - thermal frames, smooth plaster, sand - and are
slower. The networks are not invariant to rotation, SIFT is: every photo is first turned by quarter
turns to the heading the photos share (`quarter_turns`: the yaw the drone writes into the photo,
DJI's gimbal or aircraft yaw in the XMP or the EXIF `GPSImgDirection`; the course of the strips
modulo 90 degrees, so that strips flown one way and back, and cross strips, all end up the same way
up), and its points are brought back to the photo as it is. Without that, the frames of a thermal
flight matched across strips flown in opposite directions failed nearly always. The networks always
run in a process of their own (`learned_worker.py`): PyTorch and pycolmap each bring an OpenMP
runtime, and the second to start aborts the process (`OMP: Error #15`); SAM 2.1 does the same.
Photos are reduced to 1600 pixels at most for the networks; the descriptors stay in
`sfm/learned/<kind>/` beside COLMAP's database, the matches go into it and COLMAP's geometric
verification decides the inliers. Not for the rigs of 360-degree photos, nor with the guided
matching (both COLMAP's). `ppg align PROJECT --matcher disk`.

On the 407 thermal frames of a Mavic 2 Enterprise Advanced flight at 20 m (photos at 320 pixels,
`quality="high"`), SIFT aligned 405 one photo at a time (its global solution missed the GPS by 78
m) after 108 s of matching and 229 s of alignment; DISK aligned all 407 at once (513 s and 22 s),
ALIKED 404 of them (1229 s and 29 s); the temperature orthomosaics came out equally sharp and the cameras as
close to the GPS (1.7-1.9 m median). Without turning the photos DISK aligned 267. SIFT stays the
default: the learned points are for the photos SIFT cannot hold together.

### The GPU

`gpu` (default: the `PPG_GPU` environment variable, else `auto`) puts the matching on the GPU as
well: `auto` takes the best GPU of the computer, `all` every GPU it has (each with its own stream
of pairs), `off` leaves everything to the CPU, an adapter index or part of its name picks one
(`ppg doctor` lists them). The kernels are written once in
WGSL and run through wgpu - Metal on macOS, Vulkan on Linux and Windows (ARM too), DirectX 12 on
Windows - in the native core (`pyarchinit-photogrammetry-core` 0.3, `ppg_core.gpu_match`): one
pass over a pair of photos gives the best and second-best dot products in both directions, so
COLMAP's ratio test, distance test and cross check come out of it exactly (the descriptors are
COLMAP's uint8 ones, the dot products exact integers; an Apple M4 Pro does a pair of 40,000
descriptors in 0.2 s). The pairs form one queue: the GPU takes them one by one from the front, the
CPU (COLMAP's matcher) in chunks from the back, each as fast as it goes, so the split follows the
speed of the two; the matches of the GPU go into the database and through COLMAP's geometric
verification, and the alignment sees no difference. The guided matching and the rigs of 360-degree
photos stay on the CPU. `meta["sfm"]["gpu"]`, `gpu_pairs` and `cpu_pairs` say what did what.

The key points too: with the native core 0.4 COLMAP's SIFT (VLFeat's) is a set of WGSL kernels
as well (`ppg_core.gpu_sift`) - the Gaussian pyramid, its differences, the extrema with their
refinement, the orientations and the descriptors of a photo, an octave per submission. The photos
are read and reduced on the CPU, in threads, exactly as COLMAP reads them (its Rec. 709 grey, the
raster of the file, OpenImageIO's triangle filter in single precision, the longer side at the size
of the quality), so the GPU finds the points COLMAP's extractor finds, with the same descriptors
(99 % of them on the synthetic trench). An Apple M4 Pro does a 2016-pixel photo in 0.19 s where
one core of COLMAP's extractor takes 3.2 s; against all its cores the gain is smaller (57 photos of
Tell Sultan: 11 s against 19 s), and larger on a discrete GPU. The masks apply as before; the faces
of 360-degree photos stay with COLMAP's extractor, and so does a photo the GPU cannot do.
`meta["sfm"]["features"]` says `gpu` or `cpu`.

The fusion of the depth maps too (`build_point_cloud(gpu=...)`, `ppg_core.gpu_fuse`): one thread
per pixel of every depth map instead of COLMAP's traversal, the maps covisible with it (up to 64,
kept on the GPU in a cache) checked with COLMAP's own tests - the pixel of the other map
back-projected within 2 px, the depth within 1 %, the normals within 20 degrees - the consistent
pixels averaged into the point, which the earliest map that holds it emits (no map repeats a
point another one made), kept with `min_num_pixels` pixels of the filter mode. No pixel is
absorbed by a neighbouring point, so the cloud is as dense as the depth maps (on the synthetic
trench 2.7 times COLMAP's, with the 90th percentile of the height errors 2.4 mm against 3.0),
and the confidence of every point - the maps that agree on its depth within 1 % - comes out of
the same pass, so `depth_map_support` is not run again (57 photos of Tell Sultan, maps of 1008
pixels: 1.8 s against 4.5 s of COLMAP's fusion and the confidence; the larger the survey, the
larger the gain, as COLMAP's traversal slows down with the number of maps). `meta["dense"]["fusion"]`
says `gpu` or `colmap`; a GPU that fails leaves the work to COLMAP's fusion.

The PatchMatch depth maps take the same choice (`build_depth_maps(gpu=...)`): the kernel of the
native core runs on the GPU, one thread per pixel, with the same cost (the normalised cross
correlation of the window through the plane of the pixel in up to six photos, the best three
counting), the same propagation and the same start, the whole run of a photo in one submission.
The photos form a queue served by a worker per GPU and one for the CPU kernel (which uses every
core), so GPUs and CPU share them by their speed; a photo a GPU cannot do goes to the CPU kernel.
`meta["dense"]["gpu"]` names the GPUs used.

### Multispectral cameras

`engine.multispectral`: a multispectral camera (MicaSense RedEdge / Altum, DJI Mavic 3
Multispectral, Parrot Sequoia, DJI P4 Multispectral) writes one file per band of every capture, side
by side; the bands are found by their suffixes (`_1`..`_6`, `_MS_G` ..., `_GRE` ...) or by the
`BandName` of their XMP, the captures of a P4 Multispectral (six files of their own numbers) by
their `CaptureUUID`, a numbering that starts again in a second folder by the folder's name
(`detect_bands(paths, recursive=False)`; `looks_multispectral(paths)` says from the first files
whether a folder holds captures at all, what the window asks on). `add_captures(chunk, paths,
reference=None, recursive=False)` makes the reference band (the red one by default) the photo of
each capture - an 8-bit stretched copy with the tags of the original, aligned and reconstructed as
any photo - and keeps the other bands in `camera.meta["bands"]`; called again with another folder
of the flight it adds to the captures there (names that repeat take their folder, the reference
band stays, files already in the chunk are left out). The radiometry (`radiance_image`, `reflectance_image`) follows the tags:
MicaSense's model (black level, gain and exposure, the `RadiometricCalibration` coefficients, the
vignetting polynomial), else digital numbers normalised by exposure and gain; reflectance comes from
a calibration panel in the flight (`calibrate_panel(chunk, camera, region, reflectance)`: the
known reflectance per band over the panel's radiance) or from the irradiance of the sun sensor in
the tags. The bands of a capture are brought onto the reference band with a homography found on
the images (`register_bands`: the shift by phase correlation on the gradient magnitudes, an affine
at a reduced size, the homography at full size), in a chain by wavelength: each band is matched
against the registered bands nearest to it in wavelength (a leaf is bright and textured in the NIR,
dark and smooth in the red: NIR on red seldom converges, NIR on red-edge always does; on a P4
Multispectral over scrub the correlations go from 0.1-0.3 to 0.9). DJI's radiometric model (the
vignetting polynomial, the black level of the EXIF, gain, exposure, the gain adjustment, the
irradiance of the sun sensor) is followed as in their guide. `build_reflectance(chunk, bands=None,
resolution=None, balance=True)` runs the orthophoto engine on the reference band's geometry with
the registered bands as float values: `<data>/multispectral/reflectance.tif`, a float32 band per
spectral band, NaN where nothing. `balance` first brings the photos to the same reflectance where
they overlap (`balance_gains`: surface points, 80 per photo, projected into every capture and read
in its bands; the log values split into a value of the point and, per photo, a gain and a plane
across the frame, plus a radial term per band; least squares by alternation with the gains of mean
zero, every reading weighed by Tukey's biweight of what is left once its photo is corrected, so
that the shadow of a tree that moved between two flights does not tilt the plane of a photo; the
plane of a photo holds on the part of its frame its points cover): the strips between flight lines
go away, and so does the light changing inside a frame - a cloud's shadow, vegetation seen from one
side, edges brighter than the middle - while the mean level stays. On a DJI P4 Multispectral survey of 547 captures under passing clouds, the readings of a
point disagreed by 27 % between the photos, 18 % with a gain per photo and 14 % with the planes.
Gains, planes, radial terms, their spread and what is left per band are kept in
`meta["multispectral"]["balance"]`. `index_raster(chunk, "NDVI")` (also NDRE, GNDVI, SAVI, NDWI, OSAVI, CIRE,
Thermal, or any formula over the band names) makes the index on the same grid; `export_zoning(chunk,
"NDVI", "zone.gpkg", classes=3, method="quantiles" | "equal" | "kmeans")` cuts it into classes
(small patches given to their surroundings) and writes the polygons with class, range, mean and
area into a GeoPackage - the prescription map of agriculture; in archaeology the crop marks over
buried walls and ditches. `ppg multispectral PROJECT add|panel|reflectance|index|zoning|bands`.

### Thermal cameras

A drone thermal camera shoots a visible photo and a thermal frame at the same moment (DJI Mavic 2
Enterprise Advanced, M30T, Mavic 3 Thermal, H20T: `DJI_0517_W.JPG` and `DJI_0518_T.JPG`; MicaSense
Altum: the sixth band, LWIR). `add_captures` recognises them: the pairs are found by their number
when it is the same, else by the time and the place of the shot (these cameras number every file,
and the two kinds may sit in different folders), the visible photo is the photo of the capture as
it is, the thermal frame its `LWIR` band. Thermal frames without visible photos are captures of
their own: an 8-bit copy of each, all with one stretch of the raw counts, is aligned as the photo.

`engine.thermal` turns the frames into degrees Celsius (`temperature(chunk, cam)`, `decode_file`):
DJI R-JPEGs through DJI's Thermal SDK (`dji_irp -a measure --measurefmt float32`, all the frames in
one run; Windows and Linux natively, on macOS in Docker with an image the program prepares the first
time: Docker Desktop must be running), FLIR radiometric JPEGs (`decode_flir`), MicaSense LWIR TIFFs in
centikelvin, TIFFs already in degrees or kelvin, and TIFFs of integers of other cameras (WIRIS...),
which become degrees with `scale` and `offset` of the parameters (degrees = value x scale + offset;
`RAW_PRESETS`: centikelvin 0.01 and -273.15, hundredths of a degree from -100 0.01 and -100), frame by
frame before the mosaic where Metashape transforms the finished orthomosaic. FLIR JPEGs and TIFFs of
temperatures are thermal frames by themselves; `add_captures(chunk, paths, thermal=True)` (`ppg
multispectral PROJECT add FOLDER --thermal`) takes every radiometric image without a band, the
TIFFs of integers too. The SDK goes in the folder `dji_tsdk` of the program's data (or `PPG_DJI_TSDK`); it is
free from dji.com and not shipped with the program: `install_sdk(progress=None)` (`ppg thermal-sdk --install
--accept-license`) downloads it from DJI for whoever accepted its licence (MIT for the samples and utilities
such as dji_irp, DJI's SDK EULA for the rest), checks its SHA-256 against the archive DJI published, unpacks
it without letting any file out of its folder and puts it in place in one move. The raw counts of an R-JPEG (`raw_counts`, its APP3 segments) grow with the
temperature but are not degrees: they serve the geometry only. The parameters of the measure
(`params_of`, `set_params`) hold for every frame: emissivity 0.95, reflected and ambient
temperature 22 °C (the factory values of the DJI cameras), humidity 70 %, the distance the flight
height clipped to the SDK's 1-25 m; changing them decodes the frames again.

`build_thermal(chunk, resolution=None, balance=True)` makes `<data>/multispectral/thermal.tif`, one
float32 band in degrees Celsius with the parameters in its tags, NaN where nothing, at the ground
size of a thermal pixel unless asked otherwise. With visible photos the thermal frame of every
capture is put on its photo by a homography (`registrations`): the first guess from the focal
lengths, or a search of the part of the photo the frame covers, refined by ECC on the gradient
magnitudes; one homography for the flight (the median of the captures that agree best: the camera
is rigid), refined capture by capture only where that agrees clearly better. The engine samples
the temperatures on a reduced grid of the visible photo (`thermal_stack`), and a cell takes the best
photo whose frame reaches it, however many better photos frame it only at their edge (on a flight at
two heights the low visible photos win on resolution where their thermal frames do not reach).
`balance_offsets` takes away an offset
per frame found where the frames overlap (uncooled cameras drift between their flat-field
corrections, and the ground warms during a flight: on a Mavic 2 Enterprise Advanced flight of 28
minutes the offsets spread by about a degree), the mean level kept. `anomalies(chunk, radius_m)` is
the temperature minus its surroundings (`thermal_anomaly.tif`, degrees around zero); `difference`
subtracts another temperature orthomosaic of the same ground (night against day).
`export(chunk, path, "tif" | "tif16" | "csv" | "flir")` keeps the temperatures: float32 GeoTIFF in
degrees, 16-bit GeoTIFF in centikelvin (degrees = value / 100 - 273.15, with scale and offset set),
CSV of x, y and temperature, the parameters in the tags or the header, or FLIR radiometric JPEGs
(`export_flir`, `write_flir_jpeg`): tiles of 2048 cells a side (`tile`, at most 2800: a FLIR file
fits in 256 APP1 segments) named `<name>_r<row>_c<column>.jpg`, each with its world file (`.jgw`)
and reference system (`.aux.xml`), and an index `<name>_tiles.csv`. The picture is the palette
rendering; the raw counts are made with the inverse of FLIR's model (Thermimage's raw2temp) for the
parameters of the measure as ExifTool prints them, so ExifTool, FLIR's tools and the programs built
on them (Thermal-Image-Analysis, flirimageextractor, flyr) read the same degrees back, within 0.01
°C. The format has no empty value: cells without a temperature get the coldest of the tile (black in
the picture). FLIR radiometric JPEGs are read too (`read_flir`, `decode_flir`: the raw data as
16-bit counts or as a PNG, the camera's Planck constants, the measure's parameters of the chunk).
`ppg thermal PROJECT status|params|build|anomalies|export [--format flir --tile 2048]`.

`engine.probe` reads what the products hold at a point, at full resolution: `Probe(chunk)` keeps
the DEM, the thermal orthomosaic and its anomalies, the reflectance and the indices open;
`probe.at(x, y, fields=None)` gives `{"z", "temp_c", "anomaly_c", "refl_<band>", "<index>"}` (only
the values the products have there), `probe.record(x, y, label=...)` a measuring point rounded to
what the values mean, `write_points(points, path, crs)` a GeoPackage (table `punti_misura`), a CSV
or a GeoJSON. The window keeps its measuring points in `chunk.meta["drawings"]["points"]`.

### Satellite imagery

`engine.satellite`: a satellite scene comes with its *rational polynomial coefficients* (RPC),
two rational cubics giving the column and row of any ground point (longitude, latitude,
ellipsoidal height). `read_rpc(scene, rpc=None)` takes them from the GeoTIFF, from a `_RPC.TXT`
/ `.RPB` / `.rpc` sidecar or from the `RPC_*.XML` of a DIMAP product (Pleiades, SPOT: their
1-based pixel numbering is brought to 0-based); `project` and `ground` are the model and its
inverse (centre of the first pixel at (0, 0)). `scene_info` gives size, bands, ground sampling
distance and footprint in the CRS of the chunk. `ortho_scene(chunk, scene, name=None, rpc=None,
resolution=None, height_offset=0, rgb=None)` orthorectifies the scene on the DEM of the chunk
(`orthorectify`: every cell lifted to the DEM height, projected into the scene, the scene
resampled there, tile by tile in threads): the full result, all the bands in the type of the
scene, in `<data>/satellite/<name>_ortho.tif`, and an 8-bit RGBA rendition kept as a version of
the orthomosaic, so the drone orthomosaic and the satellite image of the same ground are compared
cell by cell. RPC heights are ellipsoidal: with a DEM in orthometric heights give the geoid
undulation as `height_offset`. `dsm_from_pair(chunk, scene_a, scene_b, name=None, resolution=None,
height_range=None, use_dem=True)` builds a surface model from a stereo pair (`stereo_dsm`): tile
by tile both scenes are resampled on the ground plane at a reference height (the median of the
DEM there, else the RPC's) along the direction of their baseline; a point above or below the
plane is displaced along the line of sight of each scene, so the disparity found by semi-global
matching (OpenCV SGBM) gives its height and its true position is recovered by taking the
displacement back; the result, a float32 GeoTIFF with the holes filled, is kept as a version of the
DEM and in `<data>/satellite/`. On the synthetic trench seen by two parallel-projection
"satellites" 25 degrees off nadir (base to height 0.93) the DSM is within 0.6 mm (median) of the
DEM. `ppg satellite PROJECT info|ortho|dsm SCENE [SCENE_B] [--rpc] [--resolution] [--height-offset]
[--height-range zmin,zmax] [--rgb 3,2,1]`.

### Other computers

The work that splits by photo goes to the other computers of the network through the project
folder (`engine.nodes`): on each of them, with the project folder mounted, `ppg node
<project>.ppg.files` takes units of work from `nodes/todo` (a few photos each, taken by an atomic
rename, given back when a node dies) and leaves the results in `nodes/done`. Key points: the node
runs COLMAP's extractor into a database of its own and sends keypoints and descriptors back, the
main program writes them into its database, so the alignment sees no difference. PatchMatch depth
maps: the node opens the project read-only on the shared folder and computes its units in the
workspace there, with its GPUs and CPU. The main program is a node too (it takes what nobody
took), so with no node present nothing changes; `PPG_NODES=0` keeps everything here. A node maps
the main program's paths to its own (the project folder by itself, `--map SRC=DST` for the
photos). Over SSH, `ppg remote user@host:/folder project.ppg [--photos ...]` copies the project
folder (and the photos) there with rsync, starts the node and copies units and results back and
forth while a step runs here. `meta["sfm"]["nodes"]` and `meta["dense"]["nodes"]` name the
computers that took part. The alignment itself stays on one machine.

### Mappers

`mapper` is `incremental` (one photo after the other, COLMAP), `global` (all the cameras at once,
GLOMAP: rotations, positions, then the adjustment on `tiepoint_limit / 4` tracks per photo,
retriangulated; on the 57 photos of the Tell Sultan tutorial 58 s against 293 s, the same cameras
within 2 cm) or `auto`: global from 40 photos, incremental for fewer, for 360-degree rigs and
calibrations held fixed, when the global one places less than 90 % of the photos, and when its
cameras do not fit the GPS of the photos. `meta["sfm"]["mapper"]` says which ran. Photos are aligned
with one focal length (square pixels): fitting fx and fy separately can drift into a degenerate
solution. The result stays in the sensor model (`OPENCV` with fx = fy, no tangential distortion); a
sensor calibrated with fx != fy or tangential distortion is aligned as it is, and `optimize_cameras()`
refines every parameter.

`use_gps` (`None`: when the photos have them) gives the incremental mapper the reference positions
of the photos, with their accuracy, as pose priors: the block is held to them while it grows, as
Metashape's alignment holds it to the camera references, and a long chain or a ring of photos no
longer drifts (a wrong GPS fix is damped by a robust loss); the reconstruction then lives on the
metric axes of the references less a round offset, `meta["sfm"]["gps_priors"]`. `adaptive_fitting`
is Metashape's adaptive camera model fitting, done our way: the parameters only a sound block can
bear - the principal point, the tangential distortion, two focal lengths - are refined on a copy of
the reconstruction and kept only when the mean reprojection error drops by 1 % at least and every
camera stays within physical bounds (`meta["sfm"]["adaptive_fitting"]`).

`tiepoint_limit` (default: the limit given to `match_photos`) is Metashape's tie point limit the
mapper's way: the incremental and hierarchical mappers read a copy of the database that keeps, for
every photo, its most matched key points only (`meta["sfm"]["tie_point_limit"]` counts what was
kept; 0 keeps everything). `ba_global_ratio` and `ba_global_refinements` space out the global
adjustments of the incremental mapper, which by default adjusts the whole block every time it
grows by 10 % with up to 5 refinements each (`meta["sfm"]["ba_schedule"]`): on a block of a thousand
photos that is most of the alignment time. `mapper="hierarchical"` is COLMAP's hierarchical
pipeline: clusters of photos aligned on the cores, then merged.

### Camera optimisation

With 3 or more control points (markers with an enabled reference, not check points) a free
adjustment is followed by one where the markers are fixed points at their reference coordinates,
observed in the photos (each measurement weighs `marker_weight` tie point observations): the block
bends to fit them, which a similarity alone cannot do. The fixed points are reached by continuation:
moved in steps from where the block sees them to their references, with an adjustment at each step,
so that no step starts far from its solution (a single adjustment with the points fixed metres away
diverges, the calibration running away; on the 997 photos of Tell Tuqan the targets start 1.8 m off
and are reached in 20 steps). On big blocks (more than 2.5 million observations) the steps run on a
copy with the strongest tracks, every photo keeping its share; then the poses go back to the whole
block, every tie point is triangulated again, the points that no longer fit are dropped and one
adjustment of everything closes. Control scale bars (2 or more, or with control points) enter the
same way: their markers are fixed at `engine.sfm.bar_anchors` (see *Scale bars*).
`meta["sfm"]["optimize"]` reports `control_points`, `scale_bars`, `steps` and the control/check point
and bar RMSE before and after (`ppg optimize`).

`engine="ppg"` uses our own adjustment instead (below), with `gnss="plain"` or `"bias"` and
`rolling_shutter=True`.

### Our own bundle adjustment

`engine.bundle.problem_from_chunk(chunk, rec, use_markers, gnss, rolling_shutter)`,
`bundle.adjust(problem)` and `bundle.write_back(...)`: sparse trust-region least squares (scipy; the
Jacobian by finite differences grouped by the structure: a residual depends on one photo, one point,
one camera), projection with the COLMAP camera models. Control points pulled to their reference by
their accuracy (not fixed), control scale bars as distances between their markers, GNSS positions of
the photos with a bias per flight (`bundle.flights`: per sensor, a new flight after 20 minutes
without photos), rolling shutter (a linear and an angular velocity per photo, the time of an
observation from its row; a weak prior towards a still camera, as large as 2 pixels of shift at the
depth of the photo: on photos with a global shutter the velocities stay close to zero). Report:
`gnss_bias` (per flight, in the CRS), `rolling_shutter_m`. Rigs (faces of 360-degree photos) are
refused: COLMAP's adjustment keeps them.

### Texture pages

A texture larger than `page_size` is split into pages as in Metashape (`model_texture.jpg`,
`model_texture_1.jpg`...; `TextureFile` comments and the `texnumber` of every face in `model.ply`,
as MeshLab reads them): the generic mapping puts groups of nearby faces on each page, the orthophoto
mapping a tile of the plan (the steep faces on pages of their own). `engine.texturing.texture_pages(chunk)`
lists the pages; exports write a material per page (OBJ), the pages and `texnumber` (PLY), a mesh
per page (GLB); the 3D view shows them side by side in one reduced image (`display_texture`);
versions, undo, texture patches and the Metashape import (pages and `texnumber` of `mesh.ply`) carry
every page.

### Mesh from depth maps

`chunk.build_model(source="depth_maps", surface="arbitrary", face_count="medium", voxel_size=None,
vertex_colors=True)` builds the mesh straight from the depth maps of `build_depth_maps()`, without the
dense cloud (like *source data: depth maps*). The depth maps are fused into a truncated signed distance
field (TSDF in an Open3D `VoxelBlockGrid`: only the blocks near the surface are stored) and the surface
is extracted with marching cubes, with vertex colours averaged from the photos. A voxel counts once as
many depth maps see it as the dense-cloud fusion wants for the filtering mode (3 for `moderate`, 2 for
`mild`/`none`); pieces smaller than 1 % of the largest one are dropped. The mesh is decimated to
`face_count` (`high`/`medium`/`low` = 1/5, 1/15, 1/45 of the points the depth maps give at their own
resolution, or a number) and saved as `model.ply` like the Poisson mesh, so texture, DEM, orthomosaic
and sections work as before; `surface="height_field"` re-triangulates the fused surface in 2.5D.
`voxel_size` is in chunk units (metres once georeferenced): by default twice the size of a depth-map
pixel on the surface, raised (with a warning) when the surface would need more than a quarter of the
memory. The fusion runs in a child process (a crash does not close the program; progress and
cancellation work as usual) and needs depth maps of the `cpu`, `patchmatch` or `colmap` backend (OpenMVS keeps its
own format). `engine.tsdf.fuse_depth_maps(chunk, voxel_size=, min_views=, colors=, memory_gb=)` returns
the fused surface (`FusedMesh`, internal frame) without saving it. On the synthetic trench of the tests
(depth maps at quality `high`) it takes about 4 s and is closer to the true surface than the Poisson
mesh of the dense cloud: median distance 0.9 mm, 90th percentile 2.2 mm (Poisson 1.0 and 2.8 mm).
CLI: `ppg mesh project.ppg --source depth_maps [--voxel-size 0.005]`.

## Align and merge chunks

Chunks are aligned separately (each in its own arbitrary frame), brought into one frame,
then merged, as with *Workflow > Align Chunks* and *Merge Chunks*.

| Call | Description |
| --- | --- |
| `doc.align_chunks(reference, chunks=None, method="markers", fix_scale=False, initial="auto", max_points=200_000, progress=None)` | moves `chunks` (Chunk objects, keys or labels; default: the other enabled chunks) into the world frame and CRS of `reference`; returns a report |
| `doc.merge_chunks(chunks, label=None, merge_markers=True, merge_tie_points=True, merge_point_clouds=True, merge_models=False, progress=None)` | new chunk (default label `Merged chunk`) in the frame of the first chunk; the inputs are kept |
| `ppg align-chunks project.ppg [CHUNKS...] -r REF --method markers\|points\|cameras [--fix-scale] [--initial ...] [--json]` | command line (default reference: the active chunk) |
| `ppg merge-chunks project.ppg [CHUNKS...] [--label L] [--no-markers] [--no-tie-points] [--no-point-clouds] [--models] [--activate]` | command line (default: all enabled chunks) |

Methods of `align_chunks`:

* `markers`: markers with the same label in both chunks (at least 3, with a position),
  least squares (Umeyama).
* `cameras`: photos present in both chunks (the same file, or the same label when it is
  unique in each chunk). Least squares on the camera centres; with 1-2 common photos, or
  photos along a line, the rotation comes from the camera orientations.
* `points`: registration of the dense clouds (tie points for a chunk without one). Initial
  position from the common markers, else the georeference (both chunks in a projected CRS,
  or referenced in the same local grid), else the common photos, else a global registration
  (FPFH descriptors from Open3D, matched with a seeded RANSAC: needs the `[mesh]` extra);
  `initial="markers"|"reference"|"cameras"|"global"` forces one. A scaled point-to-plane
  ICP refines it; the result is accepted when most overlapping points fit and, with colours,
  the brightness of matched points agrees (else `ValueError`).
* `fix_scale=True`: rigid alignment that keeps the scale of each chunk; only for chunks already
  in metres (CRS, references or scale bars), else `ValueError`.

Only `chunk.transform` and `chunk.crs` change; photos, tie points, dense cloud and mesh stay
in the chunk's internal frame. Estimates use local frames (world coordinates minus a rounded
offset), so national grid/UTM coordinates lose no precision. An unlevelled local reference is
levelled first (as `build_dem` would do later). If one chunk cannot be aligned, nothing changes.
Marker references of a chunk in another projected CRS are converted to the new one. The
DEM and orthomosaic of an aligned chunk are in its old frame: build them again
(`stale_products` in the report).

The report: `{"reference": {"key", "label"}, "method", "fix_scale", "crs", "levelled_reference",
"chunks": [...]}`, one entry per chunk with `key`, `label`, `method`, `correspondences`, `rmse`
(reference world units, metres once georeferenced or scaled), `scale` (scale applied),
`transform` (the new `chunk.transform`), `delta` (old world -> reference world), `stale_products`;
`residuals` (per marker or photo: `error`, `dx`, `dy`, `dz`; `angle_deg` for photos) and
`max_error` for markers/cameras; `initial`, `source`/`target` (`point_cloud` or `tie_points`),
`fitness`, `overlap`, `inlier_ratio`, `color_corr`, `attempts` and `check` (residuals of the
common markers; when the ICP started from them they also anchor it - point-to-point terms
weighing together as much as the cloud - so a flat or noisy cloud cannot slide away from them,
`anchored`) for points. `chunk.meta["chunk_alignment"]` keeps a summary.

`merge_chunks` needs chunks in one frame (aligned, or georeferenced in projected CRSs; another
CRS is converted through a fitted similarity, with a warning above 5 mm). The merged chunk has
the CRS, transform and internal frame of the first chunk, and contains:

* sensors and cameras with new keys; a photo present in several chunks is kept once (the
  first aligned copy). Identical calibrations stay separate sensors.
* markers merged by label (projections combined, positions triangulated again), or all
  kept with `merge_markers=False`; scale bars remapped.
* tie points, dense clouds (colours, normals, classes) and, with `merge_models=True`,
  meshes (vertex colours) concatenated in the common frame.
* a COLMAP model of the merged cameras (`engine.sfm.write_reconstruction_from_chunk`,
  with the merged tie points as 3D points), so depth maps, dense cloud, mesh, DEM and
  orthomosaic can be built again on the merged chunk.

DEMs, orthomosaics and textures are not merged. `merged.meta["merge"]` lists the source
chunks, the photos found in more than one chunk and warnings.

## Products from chosen point classes

The classes of the dense cloud (rules, lasso, trained classifier or stratigraphic units)
choose what goes into the products. Noise (`NOISE_CLASSES` = 7, 18) never makes a DEM or a mesh.

| Call | Description |
| --- | --- |
| `chunk.point_classes()` | points per class of the dense cloud, reading only its class column (`PointCloud.read_class_counts(path)`) |
| `chunk.build_dem(..., classes=[2])` | DEM from these classes only: `[2]` gives a terrain model (DTM) without vegetation, people or tools; `meta["dem"]["classes"]` |
| `chunk.build_orthomosaic(..., hide_classes=[3, 71])` | transparent where the visible surface is of these classes; `meta["orthomosaic"]["hidden_classes"]` |
| `chunk.build_model(source="point_cloud", ..., classes=[2, 64])` | mesh from these classes (the surface closes over what is left out); only with the dense cloud as source; `meta["model"]["classes"]` |
| `sections.section(chunk, p1, p2, source=, classes=)` | a section through the dense cloud or the model, limited to these classes |
| `sections.sections_along(..., classes=)` | cross sections perpendicular to a baseline, with the same class filter |
| `sections.plan_slice(chunk, z, classes=)` | a horizontal slice of the model at height `z`, with the same class filter |
| `chunk.export_point_cloud(path, classes=[64])` | only these classes (LAS keeps the class codes) |
| `PointCloud.class_mask(classes=None, exclude=())` | boolean mask of the points to use |
| `classify.classify_by_units(chunk, units, base_code=100, keep_manual=True)` | one class per stratigraphic unit (`unit_class_name`: `US 12 (area 1)`), points on the edge included; returns a row per unit |
| `classify.units_table_csv(rows, path)` | writes the rows of `classify_by_units` to a CSV (semicolons) |

**`chunk.build_dem(..., classes=[2])`** interpolates the gaps under what is left out.

**`chunk.build_orthomosaic(..., hide_classes=[3, 71])`** takes as visible surface the highest dense
point, gridded at the point spacing and widened by one cell;
`engine.ortho.hidden_cells(chunk, origin, shape, resolution, classes)` gives those cells.

**`sections.section(...)`**, `sections_along` and `plan_slice` with `classes=` take, from the dense
cloud, only the points of these classes; from the model, only the stretches of the cut whose middle
is nearest to a point of these classes and whose ends lie on such points (not the parts the mesh
interpolates over what is left out).

**`classify.classify_by_units(...)`** takes free codes from 100 and gives the same code when run
again (the old extent goes back to 1); it returns per unit `name`, `code`, `sito`, `area_s`, `us`,
`points`, `area_m2` (plan, of the polygons), `z_min`, `z_mean`, `z_max`.

Command line: `ppg dem scavo.ppg --classes 2`, `ppg ortho scavo.ppg --hide-classes 3,71`,
`ppg mesh scavo.ppg --classes 2,64`, `ppg section scavo.ppg x1 y1 x2 y2 --classes 2`,
`ppg export scavo.ppg --cloud muri.las --classes 64`,
`ppg classify-units scavo.ppg [--pyarchinit-site SITO] [--csv us.csv] [--no-keep-manual]`
(the units drawn in the project, or the US polygons of a PyArchInit site).

## Versions of the products

`core.versions` keeps earlier results of the dense cloud, the model, the DEM and the
orthomosaic to compare them. The *active* version is the product every tool uses (the usual
files of the chunk); kept versions are in `versions/<product>/<id>/` inside the chunk folder,
listed in `chunk.meta["versions"]` with name, date and the parameters they were built with.

| Call | Description |
| --- | --- |
| `versions.keep_active(chunk, product, move=False)` | puts the active version among the kept ones; returns the entry |
| `versions.restore(chunk, product, entry)` | undoes a `keep_active(move=True)` (a build that failed) |
| `versions.set_active_name(chunk, product, name)` | names the active version (by default `DEM del 16/10 11:20 (classi 2)`) |
| `versions.active_info(chunk, product)` | name and date of the active version |
| `versions.versions(chunk, product)` | the kept versions |
| `versions.version_file(chunk, product, id)` | the main file of a kept version |
| `versions.activate(chunk, product, id)` | makes a kept version active; the active one is kept in its place |
| `versions.rename(chunk, product, id_or_None, name)` | renames a kept version (`None`: the active one) |
| `versions.delete(chunk, product, id)` | deletes a kept version and its files |
| `versions.dem_difference(chunk, id, out=None)` | active DEM minus a kept one, on the grid of the active one (GeoTIFF float32) |

**`versions.keep_active(...)`** copies the active version, or moves it with `move` when its files
are inside the chunk folder: a file of another program is always copied.

**`versions.dem_difference(...)`**: surface minus terrain = height of walls and vegetation, after
minus before = excavated depth.

`chunk.build_depth_maps(..., skip_unreadable=True)` leaves out photos that cannot be read
(`chunk.unreadable_photos()`: missing, empty placeholders of files kept online only, not
images); without it they raise `engine.visibility.UnreadablePhotos` (also importable from
`engine.dense`). `build_texture` and `build_orthomosaic` take the same option;
`chunk.relink_photos(folder, only_unreadable=False)` points the photos to the files with the same
names in another folder (COLMAP then reads them from their common folder, or from a folder of
links when they are split, `engine.sfm.photo_folder`). The COLMAP model written
from imported poses refers only to points with two observations; `engine.sfm.repair_reconstruction(chunk)`
fixes models written by earlier versions (done before the depth maps and the camera optimisation).

## Confidence, cleaning and cropping

| Call | Description |
| --- | --- |
| `chunk.compute_confidence("point_cloud" or "model", tol=0.01)` | depth maps agreeing on each point or vertex, stored as the `confidence` property of the PLY; returns the statistics |
| `engine.confidence.depth_map_support(chunk, points, tol, abs_tol=None)` | the same count for any points of the internal frame |
| `chunk.clean_point_cloud(min_confidence=, remove_classes=, statistical=(k, std), sparse=(radius, n), groups=(distance, n), color=((r, g, b), tol))` | removes what `engine.cleaning.point_filters` selects (distances 0 = automatic from the point spacing); returns the counts per filter |
| `chunk.clean_model(min_confidence=, min_component=, max_edge=, repair=True, fill_holes=, smooth=, target_faces=)` | model cleaning (`engine.cleaning.face_filters` for the counts) |
| `chunk.clean_tie_points(max_reprojection_error=, min_image_count=, max_reconstruction_uncertainty=, max_projection_accuracy=, optimize=False)` | gradual selection (`engine.tiepoints.tie_point_metrics`, `select_tie_points`, `remove_tie_points`) |
| `engine.cleaning.delete_points(chunk, mask, keep_selected=False)` | Metashape's delete / crop selection on the dense cloud |
| `engine.cleaning.delete_faces(...)` | the same on the faces of the model |
| `chunk.export_dem(path, region=)` | the DEM cut by a polygon in the chunk CRS |
| `chunk.export_orthomosaic(path, region=)` | the orthomosaic cut by a polygon |
| `chunk.export_model(path, region=, zmin=, zmax=)` | the model cut by a polygon and between two heights |
| `chunk.export_point_cloud(path, region=, zmin=, zmax=)` | the dense cloud cut by a polygon and between two heights |
| `chunk.crop_dem(region)` | the same cut, written in the project |
| `chunk.crop_orthomosaic(region)` | the same for the orthomosaic |
| `chunk.crop_model(region, zmin, zmax)` | the same for the model |
| `chunk.crop_point_cloud(region=)` | the same for the dense cloud |
| `chunk.build_orthomosaic(..., workers=None, max_angle=70.0)` | the orthomosaic tile by tile, the tiles in parallel |

**`chunk.compute_confidence(...)`** counts the depth maps that agree within `tol` of the distance
(half an edge for a vertex); the count is also an extra bytes dimension of LAS exports.

**`chunk.export_dem(path, region=)`** and the other exports take the region as an `(N, 2)` ring, a
box, WKT, GeoJSON or a shapely geometry (`engine.crop.as_polygon`).

**`chunk.crop_dem(region)`** and the other crops never overwrite a product that is a file of another
program (`chunk.own_product_path`).

**`chunk.build_orthomosaic(..., workers=None, max_angle=70.0)`** builds the orthomosaic tile by tile
(the surface read one window at a time, every tile written at once, overviews at the end): any
size, the tiles in parallel; every cell takes the best of the photos that frame it (the four best
are tried in turn against occlusions), so the choice never depends on the tile around it; cells
that every photo sees from further than `max_angle` degrees from the vertical stay transparent.

## Seamless orthomosaic and colour correction

| Call | Description |
| --- | --- |
| `chunk.build_orthomosaic(..., blending="mosaic", blend_sigma=8.0)` | how the photos are blended: `mosaic`, `disabled` (one photo per cell) or `average` |
| `chunk.calibrate_colours(vignetting=True, samples=20000)` | exposure gain of every photo (per channel) and vignetting of every lens, stored in `chunk.meta["colour"]` |
| `engine.colour.estimate(chunk)` | the same measurement as a module function |
| `build_orthomosaic(colour_correction=None)` | None: the calibration of the chunk if there is one; True: measured first when missing; False: none |
| `build_texture(colour_correction=None)` | the same option for the texture |
| `build_orthomosaic(ghosting=False)` | with `True`, people and vehicles seen by a few photos are left out |
| `build_orthomosaic(fill_holes=False)` | holes of at most 100 cells that no photo sees filled from their edges (`cv2.inpaint`, the cells of the same group only); larger holes stay transparent |

**`chunk.build_orthomosaic(..., blending="mosaic", blend_sigma=8.0)`**: `mosaic` takes the details
of every cell from its best photo, the low frequencies blended across the seams over a few
`blend_sigma` cells (Gaussian), so different exposures leave no edge and nothing is blurred;
`disabled`: one photo per cell; `average`: every photo weighted by how well it sees the cell. A
patch never changes the pixels around it: outside the patches the weights come from the photos the
cells would take without any patch, inside a patch from its own cells (`engine.ortho._blend`).

**`chunk.calibrate_colours(...)`** fits the vignetting as `v(r) = b1 r^2 + b2 r^4 + b3 r^6` on the
radius over the half diagonal, from surface points seen by several photos, least squares on all
the observations at once (`scipy.sparse.linalg.lsqr`, outliers left out between rounds, mean log
gain zero); `chunk.meta["colour"]` keeps the result with the colour scatter before/after.

**`build_orthomosaic(colour_correction=None)`** and `build_texture` apply the correction
(`engine.colour.Correction.correct(cam, image)`) as the photos are read.

**`build_orthomosaic(ghosting=False)`**: where three or more of the candidate photos see a cell, the
best one that agrees with their median colour (within 30 levels) is taken.

## Image quality and video

| Call | Description |
| --- | --- |
| `engine.quality.estimate_quality(chunk, cams=None)` | sharpness of every photo relative to the sharpest of the chunk (0-1, `camera.meta["quality"]`) |
| `engine.quality.low_quality(chunk, threshold=0.5)` | the blurred photos |
| `engine.quality.sharpness(image)` | the measure alone |
| `engine.video.add_video(chunk, video, folder=None, srt=None, overlap=0.7, every=None, max_frames=None)` | frames of a video added as photos, a new one as the view moves or one every `every` seconds |
| `engine.video.extract_frames(video, folder, ...)` | the frames alone, written as files |
| `engine.video.read_srt(path)` | the positions of a DJI `.SRT` track |
| `engine.video.position_at(track, t)` | the position of the track at a time, interpolated |

**`engine.quality.estimate_quality(...)`** measures, per cell of a 6 x 6 grid with texture, the
finest detail over the detail a few pixels wide, along x and y, the lower of the two (motion blur
in one direction counts), 80th percentile of the cells; independent of how much texture the scene
has.

**`engine.video.add_video(...)`** adds a new frame when the view has moved by `1 - overlap` of its
width (median shift of ORB matches, a frame with too few matches only counts when sharp), the
sharpest in the next 0.4 s; or one every `every` seconds. The DJI `.SRT` beside the video
(`[latitude] [longitude] [abs_alt]`, `GPS(lon,lat,alt)` or `GPS (lon, lat, alt)`) gives every frame
its position (EPSG:4326, interpolated in time).

## Fisheye lenses and 360-degree photos

| Call | Description |
| --- | --- |
| `chunk.add_photos(...)` | a fisheye lens gets an `OPENCV_FISHEYE` sensor (Kannala-Brandt: `fx, fy, cx, cy, k1-k4`) |
| `chunk.set_sensor_model(sensor_key, model)` | another camera model for a sensor (`OPENCV`, `OPENCV_FISHEYE`, `FULL_OPENCV`, `PINHOLE`, any COLMAP model): focal length and principal point kept, distortion reset; align again after it |
| `engine.panorama.add_panoramas(chunk, paths, size=None, fov=100, faces=None)` | equirectangular photos become the faces of a cube: pinhole photos with one fixed `PINHOLE` sensor |
| alignment | the faces of every panorama are one frame of a COLMAP rig (`engine.panorama.rig_configs`) |
| `engine.panorama.split_panorama(path, folder=None, size=None, fov=100, faces=None)` | the faces of one panorama alone, written as files |
| `engine.panorama.face_maps(...)` | the `cv2.remap` maps of a face |
| `engine.panorama.face_rotation(yaw, pitch)` | the rotation from a face camera to the panorama frame |

**`chunk.add_photos(...)`** recognises a lens whose EXIF name says fisheye (`LensModel`, or the
camera model), or a 35 mm equivalent focal length up to 15 mm; the sensor is aligned with one focal
length (`RADIAL_FISHEYE`) unless a calibration says otherwise. Projections far from the axis are no
longer cut at the pinhole frame.

**`engine.panorama.add_panoramas(...)`** takes equirectangular photos (width twice the height; others
left out) and writes the faces of a cube (`front`, `right`, `back`, `left`, `up`, `down`, or the ones
in `faces`): pinhole photos of `fov` degrees (more than 90: neighbouring faces overlap), `size` px a
side (default: the resolution of the panorama in their centre), in `facce_360/<face>/<panorama>.jpg`
beside the panoramas, one fixed `PINHOLE` sensor; `camera.meta["panorama"]`, `meta["face"]`; the GPS
of the panorama is the reference of its faces.

**alignment**: a rig per `facce_360` folder, faces at their known rotation, no translation: they
keep one centre and their relative rotation, their calibration is not refined (also in
`optimize_cameras`), pairs of faces of the same panorama are not matched. Depth maps pair a face
with faces of other panoramas looking up to 50 degrees away (the panoramas are turned anyhow).

## PyArchInit media

`PyArchInit.us_media(sito, area, us, unita_tipo=None)` and `site_media(sito)`: the media linked to a
stratigraphic unit or a site (`media_to_entity_table`), each with `id_media`, `filename`, `filetype`,
`mediatype`, `descrizione` and the full paths of the original (`path`), the thumbnail (`thumb`) and the
resized copy (`resize`) - None where the file is not on this computer or on a remote store.

## Meshes of other programs and cutting the model

| Call | Description |
| --- | --- |
| `chunk.import_model(path, frame="world", offset=None, scale=1.0, up="auto", crs=None, keep=True, name=None)` | a mesh (OBJ, PLY, GLB/glTF, STL, OFF...) as the model of the chunk, with its colours and texture; the current model kept as a version |
| `io.mesh_import.import_model_chunk(doc, path, label=None, **kw)` | the same in a new chunk, removed again if the import fails; returns `(chunk, report)` |
| `io.mesh_import.read_mesh_file(path)` | the mesh of a file in its own coordinates: positions, faces, colours, texture coordinates per face corner, pages |
| `chunk.cut_model(point, normal, keep="above", thickness=None, step=None, count=None, out=None, as_version=True)` | cut the model with a world plane: one side or a slice as the model or a file, both sides or a series of slices as files |
| `engine.mesh_cut.horizontal(z)`, `vertical(a, b)`, `through_points(p1, p2, p3)`, `dipping(point, dip_direction, dip)` | planes as `(point, normal)` in world coordinates |
| `engine.mesh_cut.clip(mesh, point, normal, keep)`, `slab(...)`, `series(...)` | the cuts on a `MeshData` in the frame of the plane |

**`chunk.import_model(...)`**: `frame="world"` reads the file in the coordinate system of the chunk
(scaled by `scale`, then shifted by `offset`); `frame="internal"` in the internal frame of the chunk
(its `model.ply` edited elsewhere). `up="auto"` turns glTF files from Y-up to Z-up; the GLB written by
this program gets its origin from the JSON file beside it. A chunk without photos, products or
reference takes the coordinates of the mesh: its internal frame starts at a round origin under it,
and `crs` names its coordinate system. Every object of the file joins one mesh, every texture image
becomes a page (`model_texture.jpg`, `model_texture_1.jpg`, ...), vertices at the same position are
joined, the vertex colours of a textured mesh are read from the texture. The report gives vertices,
faces, pages, `distance` from the data of the chunk in metres (0 when they overlap) and `kept` (the
version kept). CLI: `ppg import-model`.

**`chunk.cut_model(...)`**: `keep` "above" is the side the normal points to (for `vertical(a, b)` the
left of the walker from a to b), "below" the other, "slab" the slice of `thickness` around the plane;
without `out` the part becomes the model (the previous one kept as a version), with `out` (.ply or
.obj) it is written there in world coordinates. "both" writes `<out>_sopra` and `<out>_sotto`,
"series" slices of `thickness` every `step` in the folder `out` (`fetta_+0.500.ply`, the offset along
the normal). Faces crossed by the plane are cut on it: new vertices on the crossed edges, shared by
the two faces of the edge, with every vertex property and the texture coordinates interpolated.
CLI: `ppg cut-model`.

## Laser scans

| Call | Description |
| --- | --- |
| `io.scans.read_ptx(path)` | the scans of a PTX file: the grid of each scan with its 4 x 4 registration (empty returns left out) |
| `io.scans.read_e57(path)` | the `data3D` scans of an E57 file with their poses, cartesian or spherical (extra `scans`: pye57) |
| `io.scans.read_scans(path)` | either reader, by the extension of the file |
| `io.scans.import_scans(doc, paths, label=None, crs=None, max_points=None)` | a new chunk whose dense cloud is all the scans in their registered frame; `meta["scans"]`, `meta["metric"]` |
| `doc.align_chunks(photo_chunk, [scan_chunk], method="points", fix_scale=True)` | the scan on the photogrammetric chunk (global registration, then ICP); not needed when both are in the same CRS |
| `io.scans.colour_from_photos(target, photos)` | the points of `target` take the colour of the aligned photo of `photos` that sees them closest |

**`io.scans.read_ptx(path)`**, `read_e57` and `read_scans` return the scans of a file as `Scan`
objects: registered `xyz`, `rgb`, `intensity` 0-1, scanner `position`.

**`io.scans.import_scans(...)`** keeps the colours, else the intensity in grey; `meta["metric"]`
means metres even in local coordinates (*Allinea chunk* without scale accepts it).

**`doc.align_chunks(photo_chunk, [scan_chunk], method="points", fix_scale=True)`**: on the synthetic
trench the heights agree to 0.2 mm; in plan the walls of the dense cloud move the fit by a few
millimetres.

**`io.scans.colour_from_photos(...)`** chooses among the photos where the model does not hide the
point (2 % tolerance).

## Exports for the web

| Call | Description |
| --- | --- |
| `chunk.export_orthomosaic(path, region=None, cog=None, quality=85)` | the orthomosaic as a Cloud Optimized GeoTIFF with the compression `cog` |
| `chunk.export_dem(path, region=None, cog=None)` | the DEM as a Cloud Optimized GeoTIFF |
| `io.web.export_cog(src, dst, compression="auto")` | any GeoTIFF as a Cloud Optimized GeoTIFF |
| `chunk.export_point_cloud("x.copc.laz")` | the dense cloud as a Cloud Optimized Point Cloud 1.0 |
| `io.copc.write_copc(path, xyz, rgb, classification, confidence, crs_wkt)` | the COPC writer for any points |
| `chunk.export_model("x.glb")` | binary glTF with the texture (or the vertex colours) |
| `io.web.export_glb(chunk, path)` | the same as a module function |
| `engine.normalmap.light_model(chunk, target_faces=50000, texture_size=4096, folder=None)` | a light model for the web and game engines: `light.glb`, `light.obj` + `.mtl`, `light_color.jpg`, `light_normal.png` |
| `engine.normalmap.bake_maps(full_verts, full_faces, light_verts, light_faces, ...)` | the baking alone |
| `io.flythrough.orbit_video(chunk, path, kind=None, seconds=12, fps=25, size=(1280, 720), pitch=35, zoom=1, turns=1, backend="auto")` | an MP4 of a camera orbiting the model or the cloud |
| `chunk.export_3dtiles(folder, max_faces=150000)` | Cesium 3D Tiles 1.1: `tileset.json` and GLB tiles in a quadtree in plan |
| `io.web.export_3dtiles` | the same as a module function |
| `chunk.export_i3s(path, mode="global", max_faces=20000, texture_size=2048)` | I3S 1.7 Integrated Mesh scene layer package (`.slpk`: ArcGIS Pro, ArcGIS Online and Earth, CesiumJS, loaders.gl) |
| `io.i3s.export_slpk(chunk, path, ...)` | the same as a module function |
| `io.i3s.read_slpk(path)` | reads a package back (layer, nodes, geometries) |

**`chunk.export_orthomosaic(..., cog=)`**, `export_dem` and `export_cog` write a Cloud Optimized
GeoTIFF (tiled, overviews, read by parts over the network): `jpeg` (the alpha of the orthomosaic
becomes a mask), `webp` (keeps the alpha), `zstd`/`deflate`/`lzw` lossless with the predictor
(floating point for DEMs).

**`chunk.export_point_cloud("x.copc.laz")`** writes LAZ 1.4 (point format 7, confidence as an extra
byte) arranged in an octree (each node one point per cell of a 128^3 grid, the rest to its
children, down to 100 000 points), one LAZ chunk per node, the `copc info` VLR and the hierarchy
EVLR; QGIS, Potree and laspy's `CopcReader` read only what they show.

**`chunk.export_model("x.glb")`** keeps the coordinates around the centre of the model (single
precision keeps millimetres), the centre and the CRS in the scene `extras` and in `x.json`.

**`engine.normalmap.light_model(...)`**: quadric decimation (Open3D), faces in cells of an atlas,
and for every texel the light surface moved along its normal onto the full model - its colour
(texture or vertex colours) into `light_color.jpg`, its normal in the tangent space of the light
face (x along U, y along V, OpenGL green up as glTF wants) into `light_normal.png`; `light.glb` (PBR
material with both, local coordinates), `light.obj` + `.mtl` (`map_Kd`, `norm`).

**`engine.normalmap.bake_maps(...)`**: on a rippled surface baked onto two triangles the decoded
normals are within 3 degrees (median).

**`io.flythrough.orbit_video(...)`** renders with VTK off screen (with the texture) or the software
renderer of the 3D view (points sampled on the surface, z-buffered), OpenCV writer (H.264 or
MPEG-4).

**`chunk.export_3dtiles(...)`**: each level the model with fewer faces (vertex colours, clustering),
the leaves at full detail; the root transform from east-north-up at the centre of the model to
earth-centred coordinates (pyproj; heights as they are).

**`chunk.export_i3s(...)`** builds a quadtree in plan with levels of detail (leaves with at most
`max_faces` faces, the other nodes clustered to that many, a root without mesh), every node with a
texture of its own, a power of two sized by what it shows up to `texture_size` (a leaf whose faces
lie together in the model's texture, as with an orthophoto mapping, takes that part as it is and
bakes only its few other faces beside it; other leaves bake the texture pages or vertex colours
face by face; other nodes cast onto the model), node pages, gzip geometry buffers, the MD5 hash
table of the paths last; `mode="global"` WGS84 with boxes oriented east-north-up and earth-centred
normals, `mode="local"` the projected CRS of the chunk (it needs an EPSG code in metres).

## Measures

| Call | Description |
| --- | --- |
| `archaeo.measures.surface_area(dem_path, polygon, model=None)` | inside a plan polygon: `plan` and `surface` areas from the DEM, `mean_slope`, `z_min`, `z_max`; with a mesh also `model_surface`, `model_plan`, `model_faces` |
| `measures.slope_aspect_at(dem_path, x, y)` | slope and aspect (degrees; aspect clockwise from north, where the ground faces downhill, -1 flat) from the 3 x 3 cells (Horn) |
| `measures.aspect_raster(dem_path, out)` | raster of aspect |
| `archaeo.terrain.slope_degrees(dem_path, out)` | raster of slope |
| `measures.ground_surface(xyz, classes, cell=None)` | the ground on a grid from the points of class 2 (median of each cell, holes interpolated) |
| `measures.height_above_ground(xyz, classes)` | the height of every point above that ground |
| `archaeo.timeline.phases(doc)` | the chunks with a DEM as phases of the excavation in date order |
| `timeline.phase_date(chunk)` | the date of a phase (`meta["date"]`, else the earliest EXIF date of the photos) |
| `timeline.set_phase_date(chunk, date)` | the date of a phase set by hand |
| `timeline.removed(doc, polygon=None)` | volume removed and added from each phase to the next, and inside each unit drawn on the earlier phase |
| `timeline.suggested_relations(doc)` | units exposed earlier that overlap in plan units exposed later, as `Copre` / `Coperto da` |
| `timeline.timeline(doc)` | the whole report |
| `timeline.timeline_csv(report)` | the report as CSV |
| `timeline.write_relations(pa, relations)` | the suggested relations added to the `rapporti` of the US forms (both ways; those already there left out) |
| `pa.add_relationships(sito, area, us, rels)` | relations added to the `rapporti` of one US form |
| `archaeo.us_proposal.propose_for_chunk(chunk, units, first_us, area=None, sito=None, step=0.02, cuts=True, lang="it")` | US proposed for units without a number: measures, contacts, numbers and relations, each relation with `why` |
| `us_proposal.write_proposal(pa, proposal, sito)` | the chosen units as US forms (`create_us`: `node_uuid`, `entity_uuid`; existing forms left) and their relations in both directions |
| `pa.next_us(sito)` | the number after the highest US of a site |
| `chunk.provenance(product, path=None, operator=None)` | W3C PROV-O in JSON-LD: the product, the activity that made it, what it derives from, the agents |
| `core.paradata.provenance(...)` | the same as a module function |
| `paradata.write_provenance(chunk, product, path)` | `<file>.prov.jsonld` beside an export or a published copy |
| `paradata.summary(chunk, product)` | a one-line summary (PyArchInit's note) |
| `io.qfield.export_package(chunk, folder, resolution=0.02, project=True)` | QField package: `ortofoto.tif`, `dem.tif`, `rilievo.gpkg`, `<chunk>.qgs` and `ppg_qfield.json` |
| `qfield.read_edits(folder)` | the units and spot heights added, changed and deleted on the tablet |
| `qfield.import_edits(chunk, folder)` | those edits applied to `chunk.meta["drawings"]` |
| `io.gpkg.read_table(path, table)` | features of a GeoPackage table read with `sqlite3` |
| `gpkg.geometry(blob)` | a geometry from the GeoPackage binary format |
| `archaeo.munsell.calibrate_chart(rgb)` | a ColorChecker Classic found in an image (OpenCV `mcc`): the 3 x 3 correction matrix in linear light (OpenCV `ccm`, loss as CIEDE2000) |
| `munsell.correct(rgb, ccm)` | the matrix applied to colours |
| `munsell.munsell(rgb)` | Munsell notation (illuminant C, colour-science) and nearest soil chart chip |
| `munsell.area_colour(raster, polygon, ccm=None)` | the median colour of a raster inside a polygon, shadows and glare left out |
| `archaeo.stones.find_stones(raster, segmenter, bounds=None, min_size=0.05, max_size=1.0)` | stone-by-stone drawing: SAM's automatic masks over any RGB raster by tiles, a partition, then `keep_stones` |
| `stones.export_stones(stones, path, scale=20)` | the outlines as DXF (layer `PIETRE`), SVG at 1:`scale` (millimetres on paper) or GeoJSON |
| `ai.sam.auto_units(..., path=raster)` | segments any raster |
| `chunk.build_relief(method="svf", radius=None, directions=16, min_size=None, max_size=None, scaling=2.0)` | relief visualisation of the DEM, product `method` in `<data>/terrain/`: `svf`, `openness_pos`, `openness_neg`, `slrm`, `msrm`, `multi_hillshade` |
| `archaeo.relief.build_relief(dem_path, out, method, ...)` | the same on any DEM file (tiles with the margin each method needs, several threads; the result is that of the whole DEM) |
| `relief.compute(z, resolution, method, ...)` | the same on an array |
| `measures.ground_dem(chunk)` | a DTM kept as a version of the DEM (built from class 2 only) |
| `measures.ground_dem_at(path, x, y)` | its height at a point |

**`archaeo.measures.surface_area(...)`** counts each cell `sqrt(1 + gx^2 + gy^2)` times, border
cells by the share inside; the mesh must be in the same coordinates and `model_faces` are the faces
whose centre is inside.

**`archaeo.us_proposal.propose_for_chunk(...)`** runs `measure_units` (area, perimeter, DEM
elevations, Munsell colour, majority class of the classified cloud), `contacts` (pairs that touch:
length, share of each outline, enclosure, DEM step across the contact from a strip on each side)
and `propose` (numbers from the highest unit, USM from the masonry class, fills of cuts with their
cut, *Copre* from the step, *Si appoggia a* against masonry; each relation with `why`).

**`chunk.provenance(...)`** describes the product (file, SHA-256, size, CRS, resolution, active
version) as `prov:Entity`, the `prov:Activity` that made it with its settings, the entities it
derives from back to the photos and the georeference (control/check RMSE), the program and the
operator as agents.

**`paradata.write_provenance(...)`** and `summary`: `pa.publish_orthomosaic/dem/model` write both.

**`io.qfield.export_package(...)`**: `ortofoto.tif` is a COG JPEG at `resolution`, `rilievo.gpkg`
has the layers `unita` and `quote` with `ppg_id` and the PyArchInit columns, `<chunk>.qgs` is
written by QGIS's Python (relative paths, value maps) and `ppg_qfield.json` says what was exported.

**`munsell.munsell(rgb)`** retries and, if it does not converge, takes the nearest chip in CIELAB.

**`archaeo.stones.find_stones(...)`** reads the orthomosaic or the orthophoto of an elevation;
`keep_stones` keeps an equivalent diameter between the sizes and a solidity >= 0.75.

**`chunk.build_relief(...)`** keeps the parameters and the 2nd/98th percentiles in `meta["relief"]`:
`svf` sky-view factor (1 - mean sin of the horizon angle), `openness_pos` / `openness_neg` (mean
zenith angle of the horizon, degrees), `slrm` (DEM minus its mean over the radius, metres), `msrm`
(multi-scale relief: mean difference of low-pass levels of radius `k ** scaling` from `min_size` to
`max_size`), `multi_hillshade` (0-1); radius default about 20 cells.

## Diagnostics of the alignment, calibration, markers

| Call | Description |
| --- | --- |
| `engine.diagnostics.match_graph(chunk, verified=True)` | `{(camera key a, b): matches}` of every matched pair, from the COLMAP database; also for photos that did not align |
| `diagnostics.components(keys, graph, min_matches=1)` | groups of photos linked by pairs with enough matches, largest first (separate groups, isolated photos) |
| `diagnostics.pair_matches(chunk, a, b)` | keypoints of the two photos and the candidate and verified matches (index pairs) |
| `diagnostics.shared_tie_points(chunk, a, b)` | the tie points seen by both photos |
| `diagnostics.residuals(chunk, key)` | measured and reprojected pixels of the tie points of a photo |
| `Sensor.converted(model)` | the parameters in another camera model (focal length and principal point kept); `Chunk.set_sensor_model` uses it |
| `Sensor.fixed` | the calibration is held: alignment (`constant_cameras`) and camera optimisation do not refine it, as for the faces of 360-degree photos |
| `chunk.merge_markers(markers, label=None)` | markers that are the same point made one: the first keeps its key |
| `chunk.remove_markers(markers)` | markers (objects, keys or labels) removed with the scale bars that end at them |

**`engine.diagnostics.match_graph(...)`** counts the matches that fit the geometry of the pair,
else (`verified=False`) all candidates.

**`Sensor.fixed`**: imports no longer set it (their calibration is where the alignment starts).

**`chunk.merge_markers(...)`**: the first marker takes the measurements of all (in a photo measured
in several, the one placed by hand), the first reference, the excluded photos; scale bars that
ended at the others end at it.

## Undo and redo (interface)

`gui.undo.UndoHistory(get_state, set_state, get_chunk, limit=50, file_limit=10)`: `record(text=None)`
after a change keeps the parts of `get_state()` (JSON-able) that differ from the last snapshot;
`backup(products)` before an edit copies the files of those products of the chunk (`gui.undo.clone`:
`clonefile` on macOS, else a copy) into `<chunk data>/undo/`, and the next `record` keeps them;
`undo()`/`redo()` hand the parts back to `set_state(parts, names, products)` and swap the files;
`rebase()` after processing (not an edit); `reset()` for another project or chunk (copies deleted).
The window records on every change of the project, `run_task(..., undo=True)` for edits made in
the background.

## Batch processing

| Call | Description |
| --- | --- |
| `batch.step(op, chunks="all", **params)` | a step: one of `batch.OPERATIONS` with its parameters (the defaults completed, unknown names refused); `chunks`: `"all"`, `"enabled"` or labels/keys |
| `batch.run_batch(doc, steps, resume=True, retries=1, on_error="continue")` | every step on all its chunks, then the next; returns `done`, `failed` (`"chunk:step"` -> error), `skipped`, `seconds` |
| `batch.load_steps(path)` | the steps of a batch file (JSON) |
| `batch.save_steps(steps, path)` | writes a batch file |
| `batch.pending(doc, steps=None)` | the state of an unfinished batch |
| `batch.targets(doc, chunks)` | the chunks a step runs on |
| `batch.run_step(doc, chunk, op, params)` | one operation on one chunk |

**`batch.step(...)`**: `batch.OPERATIONS` are `detect_markers`, `import_reference`, `align_photos`,
`match_photos`, `align_cameras`, `update_transform`, `optimize_cameras`, `field_check`,
`build_depth_maps`, `build_point_cloud`, `build_model`, `build_texture`, `build_dem`,
`build_orthomosaic`, `calibrate_colours`, `estimate_precision`, `export_point_cloud`,
`export_model`, `export_dem`, `export_orthomosaic`, `generate_report`. Paths may use `{project}`,
`{name}`, `{chunk}`.

**`batch.run_batch(...)`** saves the project after each step of each chunk with the state in
`<project>.ppg.files/batch.json`; `resume`: an unfinished batch with the same steps goes on (done
steps are not run again, failed ones are tried again); a failing step is retried `retries` times,
then the next steps of its chunk are skipped (or `BatchError` with `on_error="stop"`).

## Field check before leaving the site

| Call | Description |
| --- | --- |
| `engine.fieldcheck.field_check(chunk, min_photos=3, resolution=None, quality=True)` | right after the alignment: the photos not aligned or blurred, the markers measured in too few photos, the coverage of every cell; report in `chunk.meta["field_check"]` |
| `engine.fieldcheck.coverage(chunk, resolution=None)` | the counts alone: `(counts, transform, heights)` |

**`engine.fieldcheck.field_check(...)`** lists the photos not aligned, the blurred ones (image
quality estimated where missing), the markers measured in no photo (with a reference), one photo or
two only, and the coverage: on a grid over the middle 98 % of the tie points (400 cells along the
longer side by default), at the height of the DEM or of the tie points of the cell (the empty cells
interpolated), how many aligned photos frame each cell (no occlusion: fast); a GeoTIFF of counts
(product `coverage`, -1 outside the area). The report in `chunk.meta["field_check"]` has
`weak_fraction`, `weak_area` m2, `median_photos`, `unaligned`, `blurred`, `markers`,
`tie_points_weak`.

## Precision of the alignment

| Call | Description |
| --- | --- |
| `chunk.estimate_precision(marker_px=0.5)` | standard deviations in metres along X, Y, Z of the CRS of cameras, tie points and markers, from COLMAP's covariance of the adjustment |
| `engine.precision.estimate_precision(chunk, ...)` | the same as a module function |
| `engine.precision.dem_precision(chunk, max_side=1024)` | sigma Z of the tie points interpolated over the DEM, GeoTIFF product `dem_precision` |
| `engine.precision.precision_at(chunk, x, y)` | sigma Z of the DEM at a point |
| `engine.precision.tie_point_precision(chunk)` | the tie points with theirs |
| spot heights | the precision of the DEM where a spot height is taken is kept with it (`precisione_q`) |
| `bim.ifc.export_chunk_ifc(..., quote=...)` | the spot heights in the IFC as `IfcAnnotation` (`SpotHeight`) with `Pset_PyArchInit_Quota` (Quota, Precisione, US, ...) |
| `bim.ifc.spot_heights(quote)` | the spot heights of the project as points for the IFC export |

**`chunk.estimate_precision(...)`** sets the adjustment up as the camera optimisation does it
(control points held at their reference, else three tie points; calibration held) and takes
COLMAP's covariance of the camera poses (`pycolmap.estimate_ba_covariance`; tangent space of
cam_from_world, rotation perturbed as `Exp(2d) R`, translation added). Cameras: covariance of the
centre. Tie points and markers (two photos or more): the full covariance of the triangulated point,
`s^2 H^-1 + H^-1 H_pc S_cc H_cp H^-1` - its measurements (`s` = the reprojection error of the
adjustment, `marker_px` for markers) and the uncertainty of the cameras that see it. Datum: the
accuracy of the control points (or of the camera positions that place the chunk),
`a^2 (1 + d^2 / r^2)` per axis. Standard deviations in metres along X, Y, Z of the CRS:
`camera.meta["precision"]`, `chunk.meta["precision"]` (`datum`, `sigma0_px`, medians, `markers`),
tie points in `<data>/precision/tie_points.npz`. With a DEM, also the raster below. A new
alignment, optimisation or georeference drops it (`chunk.forget_precision()`). Synthetic trench:
tie point height errors / estimated sigma = 1.07 (RMS), cameras 0.5-0.7.

**`engine.precision.dem_precision(...)`** interpolates linearly (the nearest outside), in the CRS
of the DEM at most 1024 cells a side (as the precision maps of James et al., 2017, from the
covariance).

**spot heights**: the precision is written in `rilievo_originale` ("Fotogrammetria (±1,2 mm)",
PyArchInit has no column for it).

## Reconstruction region

| Call | Description |
| --- | --- |
| `chunk.region = engine.region.Region(center, size, rotation)` | an oriented box in world coordinates (`chunk.meta["region"]`) that bounds every product |
| `chunk.region = None` | no region |
| `chunk.reset_region(source="tie_points", margin=0.05, angle_deg=0)` | around the middle 98 % of the tie points (or the dense cloud), turned about the vertical |
| `engine.region.region_from_box(chunk, (xmin, ymin, xmax, ymax), zmin=None, zmax=None)` | over a plan rectangle, the heights from the points inside |
| `Region.contains(world)` | which world points lie inside the box |
| `Region.corners()` | the 8 corners of the box |
| `Region.footprint()` | the extent of the box in plan |
| `engine.region.inside_internal(chunk, pts)` | which points of the internal frame lie inside the region |
| `engine.region.depth_mask(...)` | the pixels of a depth map whose surface lies inside the region |
| `engine.region.crop_mesh(...)` | a mesh without the faces whose centre lies outside the region |

**`chunk.region = engine.region.Region(...)`**: depth maps (CPU and, afterwards, COLMAP's) have no
depth outside it, the dense cloud and the mesh are cut to it (a Poisson surface no longer balloons
beyond the object), so DEM and orthomosaic cover it only.

## Editing the DEM

| Call | Description |
| --- | --- |
| `chunk.edit_dem(polygon, method="plane", value=None, breaklines=None, keep_version=True, version_name=None)` | new heights for the cells inside `polygon` (chunk CRS) from those in a ring of three cells around it; returns `cells`, `dz_mean`, `dz_max`, `version` |
| `engine.dem_edit.fill_area(...)` | the same as a module function |
| `engine.dem_edit.new_heights(known, target, method, value=None)` | the interpolation alone |

**`chunk.edit_dem(...)`**: `plane` (the best-fit plane), `linear` (triangulation of the ring, IDW
outside it), `idw`, `constant` (`value`, default the median of the ring); `breaklines` (lines
(N, 2) or (N, 3); without z the DEM at their vertices) keep their heights. The DEM as it was is
kept as a version first; a DEM of another program is edited as a copy in the chunk folder;
overviews refreshed in the window; `chunk.meta["dem_edits"]` lists the edits.

## Elevations: orthophoto and relief on a plane or a cylinder

A frame maps a raster cell `(u, v)` (u along the surface, left to right as the viewer sees it, v up,
metres) and a depth `d` (towards the viewer) to the world: `engine.elevation.Plane(origin, u_axis,
v_axis)` (`normal = u x v`) or `Cylinder(centre, axis, radius, inside, start)` (inside: seen from the
axis, a well or an apse; outside: a tower or a column). The surface of a cell is the first hit of a
ray from the viewer side on the model, kept within `depth=(dmin, dmax)` (what stands in front of
`dmax` - scaffolding, a section left in front of the wall - is passed through).

| Call | Description |
| --- | --- |
| `engine.elevation.plane_from_line(chunk, p1, p2, side=None)` | the vertical plane through a wall trace (plan points), seen from `side` (default: where the photos are) |
| `plane_from_points(points, toward=None)` | the plane of three or more world points (markers on a wall) |
| `cylinder_from_circle(chunk, points, inside=True)` | the vertical cylinder through a circle fitted to plan points |
| `build_elevation(chunk, frame, resolution=None, name=None, depth=None, blending="mosaic", colour_correction=None, ghosting=False, fill_holes=True, max_angle=80)` | orthophoto and relief (`d`, float32) of the frame as GeoTIFF in frame metres, listed in `chunk.meta["elevations"]` |
| `elevations(chunk)` | the elevations of a chunk |
| `elevation_paths(chunk, entry)` | the orthophoto and relief files of one |
| `remove_elevation(chunk, id)` | removes one |

**`build_elevation(...)`** runs the orthomosaic engine on the frame (same photo choice, blending,
colour correction) and writes the files in `<chunk data>/elevations/<id>/` (frame in the tags and
in `frame.json`).

## Masks of the photos

A mask is a single-channel PNG as large as its photo, white where the photo counts and black where it
is masked (Metashape's and COLMAP's convention), kept in `<chunk data>/masks/<camera key>.png`. Masked
areas are left out when matching (COLMAP ignores their keypoints; the photos without a mask get a
white one, since COLMAP skips a photo whose mask is missing), in the depth maps (no depth there, also
after COLMAP's PatchMatch: `engine.dense.mask_depth_maps`), in the orthomosaic and in the texture (a
photo does not see a masked cell or face).

| Call | Description |
| --- | --- |
| `engine.masks.import_masks(chunk, folder, template=None, invert=False)` | from image files named after the photos (`{stem}_mask.png`, `{stem}.png`, `{name}.png`... or `template` with `{name}`, `{stem}`, `{label}`); `folder=None`: the alpha channel of the photos |
| `export_masks(chunk, folder, template="{stem}_mask.png")` | the masks written as files named after the photos |
| `read_mask(chunk, cam)` | the mask of a photo as an array (`keep`: True where the photo counts) |
| `write_mask(chunk, cam, keep)` | stores an array as the mask of a photo |
| `remove_masks(chunk, cams=None)` | removes the masks (all by default) |
| `masked_cameras(chunk)` | the photos that have a mask |
| `masks_from_background(chunk, folder, tolerance=12)` | the object is what differs from photos of the background taken from the same positions |
| `masks_from_colour(chunk, colour, tolerance=40)` | a backdrop of one colour (RGB) masked |
| `masks_from_model(chunk, cams=None)` | only what the model covers is kept (after a first reconstruction) |
| `masks_with_sam(chunk, segmenter, cams=None)` | SAM 2.1: the object at the centre of each photo |
| `paint_polygon(chunk, cam, polygon, masked=True)` | mask (or give back) a polygon of pixels by hand |
| `chunk.match_photos(..., exclude_stationary=True)` | verified matches that do not move between two photos dropped: the background of a turntable photographed from a fixed camera |
| `engine.sfm.remove_stationary_matches(db)` | the same on a COLMAP database |

**`masks_from_background(...)`** takes one background photo per photo (same names), or a single one
for a fixed camera.

**`chunk.match_photos(..., exclude_stationary=True)`** drops the matches that move less than 2 px
or 0.15 % of the photo.

## Patches of the orthomosaic and of the texture

A patch takes the colours of an area of the orthomosaic from photos chosen by hand: a dict in
`chunk.meta["orthomosaic"]["patches"]`, `{"id", "polygon" [[x, y], ...] (chunk CRS), "photos"
[labels], "mode" "assign"|"exclude", "surface" "dem"|"ground"}` (a later patch covers an earlier one).

| Call | Description |
| --- | --- |
| `chunk.add_ortho_patch(polygon, photos, mode="assign")` | a new patch |
| `engine.patches.add_patch(chunk, polygon, photos, mode, surface="dem")` | the same; `surface="ground"` projects the area on the ground around it instead of the DEM |
| `chunk.update_orthomosaic(progress=None, skip_unreadable=False)` | redraws only the cells of the patches added, changed or removed since the file was written, then the overviews of those windows |
| `chunk.ortho_patches` | the patches of the chunk |
| `chunk.remove_ortho_patch(id)` | removes one |
| `engine.patches.clear_patches(chunk)` | removes them all |
| `engine.patches.changed_patches(chunk)` | the patches added, changed or removed since the orthomosaic file was written |
| `engine.patches.photos_for_area(chunk, polygon, limit=12)` | the photos that frame the area, best first: `label`, `coverage`, `visible`, `angle`, `gsd`, `score`, `used` |
| `engine.patches.previews(chunk, polygon, photos, max_side=320)` | `({(label, surface): RGBA}, now, bounds)`: the area from each photo on each surface, the rest as it is |
| `chunk.patch_texture(photos, polygon=None, faces=None, mode="assign")` | colours again the faces of the textured model inside a polygon (seen from above) or chosen by index, from the chosen photos |
| `engine.patches.photos_for_faces(chunk, faces)` | the photos that see those faces, best first |
| `engine.patches.preview_faces(chunk, faces, photo)` | the part of a photo where the faces are seen |

**`engine.patches.add_patch(..., surface="ground")`** takes the heights along the polygon,
inverse-distance weighted inside: trees and vehicles as the photo shows them, holes of the DEM
filled.

**`chunk.update_orthomosaic(...)`** refreshes the overviews with `engine.overviews.refresh` (through
rasterio's GDAL; every overview again when it cannot); an orthomosaic of another program is copied
into the chunk first and its pixels come back where a patch is removed.

**`engine.patches.photos_for_area(...)`**: `visible` is the share not hidden by the model, `used`
the share the orthomosaic takes from it now.

**`chunk.patch_texture(...)`**: texels no chosen photo sees keep their colour.

`chunk.build_orthomosaic(..., patches=None)` applies the patches of the chunk (pass `[]` to leave
them out). The photos of every tile are ranked on all its cells, so an update gives the same
pixels as a build.

## Scale bars

A scale bar is the known distance between two markers (`core.marker.ScaleBar`: `label`, `marker_a`,
`marker_b` (marker keys), `distance` in metres, `accuracy`, `enabled`, `checkpoint`).

| Call | Description |
| --- | --- |
| `chunk.add_scalebar(marker_a, marker_b, distance, accuracy=0.001, checkpoint=False, label=None)` | markers as objects, labels, keys or target codes; the same pair again changes the bar |
| `chunk.scalebar(label_or_key)` | one bar |
| `chunk.remove_scalebar(label_or_key)` | removes one |
| `chunk.scalebars` | all the bars |
| `chunk.import_scalebars(path, accuracy=0.001, delimiter=None)` | `marker_a, marker_b, distance[, accuracy[, check]]` per line; returns `{"bars": [labels], "missing": ["A-B"]}` |
| `markers.gcp.scalebar_errors(chunk)` | every bar: `label`, `marker_a`, `marker_b`, `distance`, `accuracy`, `type` (control/check), `enabled`, `estimated`, `error` (metres) |
| `markers.targets.generate_scale_bar(path, target_type="aruco_4x4_50", codes=None, distance_mm=200, marker_size_mm=40, page="A4", count=1)` | printable bars and their CSV, `(pages, csv)` |

`chunk.update_transform()` uses the control bars (enabled, not check) as follows:

* no control point and no camera reference (or camera references that cover less than ten times
  their accuracy, `cameras_span_their_accuracy`): the scale is the least-squares solution of
  `scale * |a - b| = distance` weighted by `1 / accuracy²` (of two bars measured equally well, the
  longer one counts more); the orientation stays, the origin is the internal one and the CRS
  becomes `LOCAL` (`method == "scalebars"`);
* the same on a chunk georeferenced elsewhere (an imported project, CRS not `LOCAL`) with no
  enabled reference: the bars scale it about their centre, keeping its position, orientation
  and CRS;
* three or more control points, or camera references: the similarity is the joint weighted
  least-squares fit of the points (`1 / accuracy²` on their 3D residuals) and of the bars
  (`1 / accuracy²` on `scale * length - distance`), `markers.gcp.similarity_with_bars`: the
  rotation and the position come from the points, the scale from points and bars together
  (`"markers+scalebars"`, `"cameras+scalebars"`, `"markers+cameras+scalebars"`);
  `use_cameras=True` or `False` forces the use of the camera references.

The report adds `scale` (world units per internal unit), `scalebars` (as `scalebar_errors`),
`scalebar_control_rmse` and `scalebar_check_rmse` (metres, None without bars of that type).

Markers are placed by `chunk.triangulate_markers()` (`markers.gcp.triangulate_markers`): from two
photos or more they are triangulated; measured in one photo only they lie where its ray meets the
surface, `gcp.surface_point(chunk, camera, x, y)` (the dense cloud, else the tie points, through
`core.geometry.point_on_ray`: a plane fitted to the points around the ray, first in a wide cylinder,
then in one of about 20 pixels at the depth found; kept per measurement); a marker with no
projection keeps its position. `gcp.views_of(chunk, marker)` counts the aligned photos that measure
it, `gcp.camera_ray(chunk, camera, x, y)` is the ray of a pixel. A marker placed in 3D (internal
frame) is projected on the photos that see it with `gcp.project_marker(chunk, marker, point,
max_photos=60)`: inside the frame, in front of the camera, not hidden by the surface near the
marker, as estimated projections.

A projection (`core.marker.Projection`) is `pinned` True when placed or confirmed by hand, False
when detected (coded targets), None when only estimated from the model (Metashape's blue flags;
older versions read None as a detection). `Projection.measured` is True for the first two: a
marker with measured projections is placed from them only, estimates are used only for a marker
placed in 3D, and they never enter the camera optimisation. `gcp.photos_seeing(chunk, point,
occlusion=True)` lists `(camera, x, y, depth)` of the photos that see a point, nearest the centre
first (*Filter Photos by Point*); `gcp.project_marker(chunk, marker, point=None, max_photos=60)`
replaces the estimated projections of a marker from its position; `gcp.views_of(chunk, marker,
estimated=False)` counts the photos that measure it (or, with `estimated`, that only estimate it).

`markers.refine.refine_markers(chunk, markers=None, min_score=0.8)` looks for every blue flag in its
photo by image correlation: a patch of the photo where the marker is measured (placed by hand
first, else the sharpest view) and the photo of the flag are both sampled on the same grid of
points on the surface at the marker (the plane of the dense cloud, else of the tie points, around
it; one sample per pixel of the coarser photo, smoothed against aliasing), so perspective,
rotation, scale and lens distortion do not matter; a clear peak of the normalised correlation
(at least `min_score`, 0.1 above any other peak, inside +-10 samples) moves the flag there to a
fraction of a pixel and makes it an automatic measurement (`pinned` False); the marker is
triangulated again and the flags left are estimated from its new place. Returns `{marker key:
{"refined": {camera key: (dx, dy, score)}, "failed": [camera keys], "reference": key}}`;
`refine_marker(chunk, marker, ...)` does one marker. On the synthetic trench, flags 2-6 px off
the target come back within 0.3 px of the detected centre.

In the camera optimisation (`chunk.optimize_cameras`) the control bars are fixed points as the
control points are: `engine.sfm.bar_anchors(chunk, bars, fixed=None)` moves the markers at their
ends so that the bars get their lengths, least squares of `scale * length - distance` (weighted
by `1 / accuracy²`) and of the shifts, weighted by `engine.sfm.marker_precision(chunk, marker)`
(the pixel accuracy of its measurements, 0.5 px, carried to its distance from each camera and
combined over the photos); the adjustment then holds those positions, observed in the photos.
It needs three fixed points or more, not on a line: two bars, or bars and control points. On
the synthetic trench, markers measured to 0.5 mm, a bar 6 mm too long with an accuracy of
0.1 mm bends the block by 0.2 mm: the photos hold it, as in a joint adjustment.

Imports: `integrations.metashape.import_psx` brings the scale bars of the project
(`MsScaleBar`: two `endpoint marker_id`, `reference d`, accuracy `sd` or the chunk's
`accuracy_scalebars`, unticked = check bar); `integrations.webodm.import_task` brings the camera
poses (`read_cameras`: `cameras.json`, `odm_report/shots.geojson`) and the GCPs as markers
(`read_gcps`), triangulated at once.

## Modules

| Module | Main functions |
| --- | --- |
| `markers.targets` | `generate_sheet`, `generate_turntable_mat`, `turntable_layout`, `generate_scale_bar`, `scale_bar_codes`, `detect` |
| `markers.gcp` | `import_reference`, `export_reference`, `triangulate_markers`, `update_transform`, `similarity_with_bars`, `marker_errors`, `scalebar_errors`, `import_scalebars`, `resolve_marker`, `cameras_span_their_accuracy` |
| `engine.chunks` | `align_chunks`, `merge_chunks`, `common_markers`, `common_cameras`, `pose_similarity`, `icp`, `global_guesses`, `ransac_rigid`, `registration_quality` |
| `archaeo.sections` | `section`, `sections_along`, `plan_slice`, `Section.to_dxf/to_svg`, `drawing_scale`, `grid_step` |
| `archaeo.pottery` | `estimate_axis`, `profile_at`, `best_profile`, `Profile.measurements`, `rim` (circle of the lip: diameter, arc kept, reliability), `measure` (profile and measurements of a vessel or sherd, axis moved to the centre of the rim), `percent_class`, `signature`, `signature_distance`, `draw` (SVG, DXF, PNG) |
| `archaeo.terrain` | `contours` (simplified, optional smoothing), `suggest_interval`, `dem_range`, `export_contours`, `volume`, `hillshade`, `slope_degrees` |
| `archaeo.stratigraphy` | `UnitPolygon`, `mask_to_polygons`, `export_units` |
| `bim.ifc` | `export_ifc`, `export_chunk_ifc`, `export_proxies_ifc`, `split_mesh_by_units`, `global_id` (IFC GlobalId from the `node_uuid` of a US form), `read_ifc_elements` |
| `archaeo.stratigraphy` (relations) | `RELATION_EDGES` (the relations of PyArchInit's forms in Italian, English and German as Extended Matrix edges), `stratigraphic_edges` (each declared relation once, from the start of its edge) |
| `ai.classify` | `classify_chunk`, `classify_by_units`, `unit_class_name`, `units_table_csv`, `assign_class_by_polygons`, `class_statistics`, `set_class`, `clear_manual`, `class_names` |
| `ai.learn` | `point_features`, `train`, `predict`, `ClassifierModel`, `train_on_chunk`, `classify_chunk_with_model` (see below) |
| `ai.sam` | `Sam2Segmenter`, `segment_orthomosaic`, `auto_units`, `classify_from_masks`, `segment_photo_and_classify` |
| `pyarchinit` | `PyArchInit`, `register_media`, `connect` |

## Coordinate conventions

* Internal frame = frame of the SfM solution; `chunk.transform` (similarity) maps it to the
  chunk CRS. Dense cloud and mesh are stored internally; rasters and exports are in the CRS.
* Image coordinates follow COLMAP: the centre of the first pixel is (0.5, 0.5).
* Cameras: OpenCV axes (+X right, +Y down, +Z forward); `camera.transform` is
  `world_from_cam` in the internal frame.

## Demo data and interface

| Call | Description |
| --- | --- |
| `ppg.demo.create_demo(folder, process=False)` | synthetic trench photos + GCPs, project, pottery meshes, PyArchInit DB (`DemoData`) |
| `pyarchinit_photogrammetry.gui.main(argv)` | starts the desktop interface (`ppg-gui`) |
| `ppg.core.progress.Progress` | progress callback with `sub()` ranges; `cancel()` stops every sub-step |


## Added in 0.3

* `chunk.crop_point_cloud(zmin=, zmax=, radius=, center=(x, y), bounds=(xmin, ymin, xmax, ymax))`
  keeps the dense points inside the limits (world coordinates) and drops the model products.
* `PyArchInit.connect(url=None, config_path=None, media_root=None)`, `pa.ensure_site(sito, ...)`,
  `pa.site_exists(sito)`, `pa.label`.
* `pyarchinit.demo_db.create_demo_database(home, size="small"|"large", overwrite=False, demo_content=True)`
  with the PyArchInit 5 schema (`pyarchinit5_schema.sql`).
* `demo.create_demo(..., with_pottery=True)` and `demo.process_pottery_demo(doc, profile_svg)`;
  `testing.synthetic.make_turntable_scene()`, `render_mesh()`, `write_pottery_dataset()`.
* `engine.isolate.run_isolated(fn, *args, timeout=, cancel_check=)` runs a module-level
  function in a fresh interpreter (used by the Poisson reconstruction). With `progress=callback`
  it relays what `fn` passes to `engine.isolate.report(fraction, message)` (used by the fusion of
  the depth maps).


## Trainable point classification

Label some points by hand, train a classifier on them, classify the rest of the cloud and,
with the saved model, the clouds of other projects (like CloudCompare's 3DMASC/CANUPO).
Needs scikit-learn (`pip install "pyarchinit-photogrammetry[learn]"`, included in `[gui]` and `[all]`).

```python
from pyarchinit_photogrammetry.ai import classify, learn

classify.set_class(chunk, lasso_mask, 64, label="Muro")   # class + "labelled by hand" flag
model = learn.train_on_chunk(chunk)                        # learns from the hand-labelled points
print(model.summary())                                     # out-of-bag accuracy, precision/recall per class
pc, confidence = learn.classify_chunk_with_model(chunk, model, min_confidence=0.6)
model.save("scavo.ppgclf")
other = learn.ClassifierModel.load("scavo.ppgclf")          # apply it to another project
```

| Call | Description |
| --- | --- |
| `classify.set_class(chunk, mask, code, manual=True, label=None)` | gives the selected dense points (boolean mask or indices) a class and flags them as labelled by hand; `manual=False` removes the flag; returns the count |
| `classify.label_points(pc, mask, code, manual=True)` | the same on a `PointCloud` in memory: label several selections, then store the cloud once (`chunk.point_cloud = pc`) |
| `classify.clear_manual(chunk, mask=None)` | forgets the hand-labelled flag (classes unchanged) |
| `learn.point_features(xyz, rgb=None, normals=None, scales=None, ground=None, progress=None)` | `(N, F)` float32 feature matrix and feature names; world coordinates, z up |
| `learn.suggest_scales(xyz)` | default core voxel and radii: 5, 14 and 40 times the point spacing |
| `learn.estimate_spacing(xyz)` | the point spacing of a cloud |
| `learn.train(xyz, codes, rgb=None, ..., method="random_forest", n_estimators=None, max_per_class=20000, random_state=0)` | `ClassifierModel`; `codes` 1-255, 0 or negative = unlabelled; `method="gradient_boosting"` for smaller files |
| `learn.predict(model, xyz, rgb=None, ..., min_confidence=0.0)` | `(codes uint8, confidence float32)`; points below `min_confidence` become 1 (unclassified) |
| `learn.train_on_chunk(chunk, labels="manual")` | trains on the hand-labelled points (`labels="all"`: every point with a class above 1) |
| `learn.classify_chunk_with_model(chunk, model, overwrite_manual=False, min_confidence=0.0)` | classifies and stores the dense cloud; returns `(point_cloud, confidence)` |
| `model.save(path)` | writes the model file |
| `ClassifierModel.load(path)` | reads a model file |
| `learn.read_model_info(path)` | reads the metadata only |
| `model.summary()` | a readable description: out-of-bag accuracy, precision/recall per class |
| `model.classes` | the class codes of the model |
| `model.class_names` | their names |
| `model.feature_names` | the names of the features |
| `model.scales` | the neighbourhood radii (metres) |
| `model.oob_score` | the out-of-bag accuracy (random forest) |
| `model.training_accuracy` | the accuracy on the training samples |
| `model.confusion_matrix` | the confusion matrix (rows true class, columns predicted) |
| `model.stats` | the training statistics |

* **Features** (per radius): linearity, planarity, sphericity, omnivariance, eigenentropy,
  change of curvature, verticality of the normal and of the main axis, density, height above
  the lowest and below the highest neighbour, vertical spread, roughness; then height above
  ground (progressive morphological filter, or `ground=` a DTM GeoTIFF / ground elevations),
  colour (normalised RGB, HSV, excess green) and colour contrast with the neighbours.
  They are computed on a *core* cloud (one point per voxel; voxel = point spacing, coarser
  above `max_core_points`, default 1 M) and every point takes the values of its voxel; each
  radius uses its own voxel level and at most 64 neighbours. About 15 s per million core
  points on 4 threads (`n_jobs`).
* **Hand labels**: `PointCloud.manual` (boolean per point, `None` when nothing was labelled)
  is stored as the PLY property `manual`; files without it load as before. The rule-based
  `classify_chunk` and `classify_chunk_with_model` keep the class of these points unless
  `overwrite_manual=True` (then the flag stays where the new class agrees).
* **Training**: one sample per core voxel (the class most of its labelled points carry), at most
  `max_per_class` per class, classes weighted inversely to their size; the random forest
  (100 trees, each on at most 10,000 samples) is validated out of bag, gradient boosting on
  20 % held out. Deterministic for a given `random_state`.
* **Model file** (`.ppgclf`): a ZIP with `model.json` (format version, classes and names,
  feature names, radii and settings, core voxel, statistics, versions of the package,
  scikit-learn and NumPy, training chunk and CRS) and `estimator.pkl` (the scikit-learn
  estimator). The estimator is unpickled with an allow-list of scikit-learn and NumPy types,
  so a file that tries to run other code is refused. The radii are in metres: apply a model to
  clouds with a similar point spacing and a metric CRS (a local chunk without scale has
  arbitrary units).
* **CLI**: `ppg classify-train scavo.ppg modello.ppgclf [--labels manual|all] [--method
  random_forest|gradient_boosting] [--scales 0.05,0.15,0.4] [--trees 100]` and
  `ppg classify-apply scavo.ppg modello.ppgclf [--min-confidence 0.6] [--overwrite-manual]`.
