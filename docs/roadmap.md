# Roadmap

## 0.2 - Desktop interface (done)

Workspace tree, 3D view (VTK or software), orthomosaic/DEM view with drawing tools, photo
view with marker placement and SAM prompts, reference pane, Python console, background
processing with cancel, PyArchInit menu, demo data (`ppg demo`).

## 0.3 - Robust desktop release (done)

Coloured icons (qtawesome), light/dark theme, start page, log pane with filters and
copy/save, error windows with hints and traceback, `ppg doctor` / *Aiuto > Diagnostica*,
log and crash files, non-native message boxes (macOS 26/27), Poisson reconstruction in an
isolated process with retries and height-field fallback, Open3D 0.20.0 excluded, PROJ data
redirected to rasterio's own in worker threads, packaged applications for macOS and Windows
(PyInstaller, `desktop` workflow).

## 0.4 - Integrations (done)

Editable layer attributes (write-back to PyArchInit), live QGIS project, imports from
Metashape `.psx`, WebODM/ODM and Photo3D, Photo3D engine entry point (`ppg photo3d-job`),
SAM 2.1 installed on demand in the packaged app, IFC export with progress/preview and units
in metres, corrected cut/fill volumes, signing and notarization in the build. US forms (create/edit `us_table` records with
relationships), 3D proxies of stratigraphic units (Extended Matrix OBJ, 3D media in PyArchInit),
IFC proxies with quantities, materials, phase groups and relationships.

## 0.5 - Excavation records, point classes, engine (done)

One site per project; drawn units and spot heights with the fields of `pyunitastratigrafiche`
and `pyarchinit_quote`, saved in the project and published once; spot-height tool separate
from SAM; SAM 2.1 by tap, box, area or the whole orthomosaic in tiles, with corrections, on
CUDA or the Apple GPU; point classes shown, hidden and corrected with a lasso in the 3D view;
trainable point classifier with reusable models; mesh from the depth maps (TSDF fusion);
camera optimisation constrained by the control points; chunk alignment and merging; confidence
of the dense cloud and of the model; cleaning of the dense cloud, the model and the tie points
(gradual selection); cropping and cropped exports; photos relinked to another folder; large
orthomosaics built tile by tile and shown at full resolution when zooming in; patches that
assign photos to areas of the orthomosaic and of the texture (on the DEM or on the ground
around the area), updated in place with their overviews.

## 0.6 - Scale bars and markers as in Metashape (done)

Scale bars between markers (control and check, several averaged by their accuracy), also in
local coordinates for objects and trenches, fitted together with control points and camera
positions, inside the camera optimisation, from files and printable; scale bars and marker
accuracies imported from Metashape (and the scale of local Metashape chunks applied); camera
poses and GCPs imported from WebODM; markers placed on one photo (on the surface), on the model,
the cloud or the orthophoto; guided placement with blue flags to confirm in the photos, photo
filters by marker, point and area, estimated coordinates in the Reference pane.

## 0.7 - Updates, speed and image quality (done)

Automatic update check (at start-up once a day and from *Aiuto*; on macOS the new application is
downloaded, copied aside and swapped in after quitting, then reopened). Native core in Rust
(`native/`, `pyarchinit-photogrammetry-core`, imported as `ppg_core`, optional, numpy kept as fallback
and reference): fusion of the depth estimates, disparity back to depth, normals, projection with the
COLMAP camera models, scoring and best photos of every orthomosaic cell, confidence; CPU depth maps of
several photos at once (synthetic trench 5.7 s -> 0.9 s, 40 photos of the Tell 10.8 s -> 3.2 s).
Seamless orthomosaic: every cell picks among all the photos that frame it (no seam follows the
tiles), two-band blending across the seams, exposure gain and vignetting measured on the photos and
corrected (orthomosaic and texture), ghosting filter, small holes filled. Masks of the photos (drawn,
imported, from background photos, a colour, the model or SAM 2.1) left out of matching, depth maps,
orthomosaic and texture; stationary matches dropped for turntables. Elevations: orthophoto and relief
on the plane of a wall or a cylinder (wells, towers). DEM editing (plane, linear, IDW, constant,
breaklines, the DEM as it was kept as a version). Reconstruction region (a box around the subject).
The generic texture of objects gets the colour correction; blending inside its atlas is left for later.

## 0.8 - Acquisition and precision (done)

Fisheye lenses recognised from the EXIF and aligned with their own model; 360-degree photos split
into the faces of a cube and aligned as a COLMAP rig (one centre, known rotations, calibration held).
Photos from a video (sharp frames with the right overlap, GPS from the subtitles of DJI drones) and
image quality. Blue flags of the markers found by image correlation on the surface around the
marker. Precision of cameras, markers and tie points from the covariance of the bundle adjustment
(with the accuracy of the control points), map of the vertical precision of the DEM, spot heights
with their precision in PyArchInit and in the IFC (IfcAnnotation). Field check right after the
alignment: coverage map (areas seen by fewer than three photos), photos not aligned or blurred,
markers in too few photos.

## 0.9 - Everyday work (done)

* Batch processing over several chunks, saving after each step, resuming after a power cut,
  retrying a failed step. *Done.*
* Undo and redo for drawings, markers, patches, scale bars and point edits. *Done.*
* Diagnostics: matching graph, matches between two photos, residuals in the photo view, camera
  calibration dialog with distortion plot, merging markers. *Done.*
* Measures: 3D area on the model, slope, aspect and height above ground under the cursor,
  height-above-ground view of the cloud, aspect raster. *Done.*
* Exports: Cloud Optimized GeoTIFF (JPEG, WebP, ZSTD), COPC, Cesium 3D Tiles, GLB, normal maps
  for light models, fly-through video. *Done.*
* Laser scans: E57 and PTX import, registration on the photogrammetric chunk, colours from the
  photos. *Done.*
* Section and plan-slice tools drawn directly in the 3D view; markers moved in 3D. *Done.*
* PyArchInit panel: browsing media linked to US. *Done.*
* English/Italian interface translation: catalog of the texts, messages with values, the
  messages of the engine and of the log recognised when shown, *Vista > Lingua / Language*. *Done.*

## 1.0 - Archaeology first (done, released as 1.0.0)

* Relief visualisation of DEMs: sky-view factor, positive and negative openness, local relief
  model, multi-directional hillshade, multi-scale relief (walls and ditches barely visible on a
  tell). *Done* (in NumPy by tiles and threads: 2000 x 2400 cells in under a second per method, so
  no native kernel was needed).
* Excavation over time: phases (chunks by date) aligned together, volume removed for each US,
  timeline in the views, stratigraphic relations suggested from the order of removal. *Done*
  (*Strumenti > Fasi dello scavo*, `ppg timeline`; the drawings now stay with the chunk they were
  drawn on).
* Stone-by-stone drawing with SAM on orthomosaics and elevations. *Done* (SAM tool *Pietre*,
  `ppg stones`).
* Munsell colour of the US from orthomosaics calibrated with a colour chart. *Done* (*Misura area*,
  *Calibra il colore con la tabella colori*, `ppg munsell`).
* QField package: orthomosaic and layers on the tablet in the trench, edits back to PyArchInit. *Done*
  (*File > Esporta > Pacchetto QField*, *File > Importa > Modifiche da QField*, `ppg qfield`).
* Machine-readable paradata (provenance of every product) linked to the PyArchInit media. *Done*
  (W3C PROV in JSON-LD beside exports and published media, `ppg paradata`).
* Pottery: rim diameter from a sherd, profile-based typology search against `pottery_table`. *Done*
  (*Strumenti > Profilo ceramico da mesh*: rim diameter and arc kept from the circle of the lip,
  *Cerca forme simili*, *Salva in PyArchInit*; `ppg pyarchinit pottery-search`, `pottery-save`).
* Automatic US proposal from SAM masks + stratigraphic relations for the Extended Matrix. *Done*
  (*Strumenti > Proponi US e rapporti*: numbers, kinds and measurements, relations from the contacts
  and the DEM, cuts of the fills; written into the US forms from which PyArchInit and EMStudio build
  the matrix; `ppg us-proposal`).
* IFC with stratigraphic relations and s3dgraphy/StratiGraph identifiers (`node_uuid`). *Done*
  (the `node_uuid` of the US form is the `GlobalId` of its element; one `IfcRelConnectsElements` per
  relation with its Extended Matrix edge; see [bim.md](bim.md)).

## Research

* CPU PatchMatch multi-view stereo in the native core (COLMAP's needs CUDA, which the Mac does not
  have; the CPU path was two-view SGBM). A plane-sweep prototype on the Apple GPU found the
  same depths as the CPU path but ran 30 times slower than OpenCV SGBM. *Done* (backend
  `patchmatch`, core 0.2.0: on the synthetic trench DEM median 0.27 mm, NMAD 0.70 mm, 90th
  percentile 1.5 mm against 0.76 / 1.69 / 3.7 mm with SGBM, 0.5 s per photo of 600 x 450 pixels;
  the two-view maps also put points beyond the edges of the surface).
* Own bundle adjustment in the native core: true distance constraints for scale bars,
  rolling-shutter compensation for drones, GNSS bias (pycolmap does not accept custom costs).
  *Done* in Python (`engine.bundle`: scipy sparse least squares with the Jacobian grouped by the
  structure of the problem; `optimize_cameras(engine="ppg")`, `ppg optimize --engine ppg`): on
  synthetic blocks it recovers poses and calibration, the scale from two bars, a GNSS bias within
  2 cm, and brings a rolling-shutter block from 1.75 px to 0.002 px; the trench keeps its
  thresholds. Moving it to the native core is left for when blocks get too large for it.
* Multi-page texture atlases, I3S. Pages *done*: a texture larger than 8192 px is split into pages
  (groups of nearby faces, or tiles of the plan for the orthophoto mapping), carried by exports,
  the 3D view, versions, undo, patches and the Metashape import. I3S *done*: the textured model
  as an Integrated Mesh scene layer package 1.7 (`chunk.export_i3s`, `ppg export --i3s`, *File >
  Esporta*), global (WGS84) or local (projected CRS), read back node by node by loaders.gl.
* ARM builds. Linux aarch64 *done*: wheels of pycolmap 4.2.1 and Open3D 0.19.0, which their
  projects do not publish, built in Docker with their own recipes (`packaging/arm`, workflow
  `arm-wheels`) and tested with this project. Windows on ARM: the x64 application runs under the
  emulation of Windows 11 (checked); native ARM64 builds (pycolmap, Open3D, OpenCV, lazrs) next.

## After 1.0

* Official documentation site in Italian and English, with tutorials worked on a real tell.
* Plugins: a plugin interface, a guide to writing plugins and a repository that collects them.
* Benchmark against Metashape and WebODM on Tell Tuqan (with control points): alignment, depth
  maps, dense cloud, mesh, DEM and orthomosaic - times, results and errors.
* Native Windows ARM64 builds.

## After 1.1 (done, released as 1.2.0)

Done in 1.2.0: the GPU through wgpu for the matching of the descriptors and the PatchMatch depth
maps, several GPUs and the CPU together (after 1.2.2 also the SIFT key points and the fusion of
the depth maps; the bundle adjustment stays on the CPU: Ceres, no portable GPU solver); the
network nodes and `ppg remote`; the multispectral
captures with the panel, the sun sensor, reflectance orthomosaics, the indices and the zoning;
the satellite scenes orthorectified on the DEM and the surface model from a stereo pair. Still
open from this list: the texture and orthomosaic tiles on the nodes, stereo triplets and the check
on the public WorldView-3 datasets.

* GPU for the slowest steps, portable: kernels written in WGSL and run through wgpu (Metal on
  the Mac, Vulkan on Linux and Windows, no CUDA), first for the matching of the descriptors and
  the NCC of PatchMatch; several GPUs and the CPU share the work in proportion to their speed.
* Network and remote processing: the program started as a node on other computers of the
  network takes from the shared project folder the work that splits by photo (features, depth
  maps, texture and orthomosaic tiles); the same over SSH on a rented machine. The alignment
  stays on one machine.
* Multispectral cameras and vegetation indices: rigs of four or five bands as the 360 rigs,
  radiometric calibration with the panel and the sun sensor, reflectance orthomosaics at 16 bits
  per band, NDVI, NDRE and thermal, zoning exported as polygons with an attribute (the
  prescription map of agriculture; in archaeology the crop marks over buried walls and ditches).
* Satellite imagery, in two steps: the orthorectification of a scene with its RPC model on the
  DEM of the survey, kept as a version of the orthomosaic; then surface models from stereo pairs
  and triplets (local affine approximation of the RPC per tile, rectification, dense matching,
  triangulation), checked on the public WorldView-3 datasets with lidar truth.

## Done in 1.3.5 - 1.5

* **Windows like the other systems** (1.3.5): the test suite runs on Windows x64 in CI too, and the bugs it
  found are fixed (the model file left mapped, DJI thermal decoding, products held open, SpatiaLite of QGIS
  not found, progress reports of child processes); the Windows ARM64 application has the VTK 3D view and
  E57 (VTK 9.7.1 and pye57 built for ARM64, the view checked on a Windows 11 ARM64 machine).
* **Satellite** (1.3.5): surface models from three scenes or more (pairs chosen by intersection angle and
  date, a relative correction of the RPCs from tie points, the pairs fused per cell), checked on two sites
  of the public JHU/APL WorldView-3 benchmark against lidar: median error 0.59 m and 1.03 m, 61 % and 38 %
  of the cells within 1 m (see the benchmark page).
* **MicaSense** (1.3.5): checked on the sample captures of five cameras against MicaSense's own numbers -
  radiance, thermal band, panel found from the tags, sun sensor, band registration through the lenses; the
  thermal band within 0.3 thermal pixels; panel + sun sensor by default.
* **Network nodes** (1.4.0): the tiles of the orthomosaic and of the orthophoto of a terrain texture, and the
  rows of cells of the texture, on the nodes too; the pixels are the same as alone.
* **Raster calculator** (1.4.0): *Strumenti > Calcolatore raster*, `ppg calc`: formulas as in QGIS on every
  raster of a chunk, those of the plugins too.
* **QGIS both ways** (1.4.0): what is saved in QGIS in the layers of the units, the spot heights and the
  sections comes back into the window within a second (changed, drawn there, deleted), one *Annulla* per
  save.
* **Editing the units drawn** (1.4.0): *Vertici* (move, add, remove vertices with snapping) and *Rimodella*
  (QGIS's Reshape Features); the 3D proxy of the unit is built again by itself with its base.
* **New units into the Extended Matrix** (1.4.0): a unit drawn with its number and without a PyArchInit
  form is taken by *Proponi US e rapporti*, and reaches EMtools and EMStudio with the next em.json of the
  site exported by PyArchInit.
* **Units made by plugins** (1.5.0): `context.add_units`; the plugin **samgeo** finds what is written
  ("stones", "walls") in the orthophoto (Grounding DINO and SAM 2.1; SAM 3 optional from samgeo 0.2.0) and
  the plugin **geoai** trains on the drawings checked by hand (DEM, multispectral and thermal inputs too).
* **Units in groups** (1.5.1): bulk identifiers, named UT groups and freehand selection.
* **Layer labels** (1.5.2 - 1.5.3): labels shown or hidden per layer and edited without changing the US/UT
  number, saved per chunk, kept by *Annulla* and *Ripeti*.
* **Elevations and sections like Metashape's planar orthomosaics** (1.5.5): on the plane of the 3D view as it
  looks now, of markers with the axes chosen (u from one marker towards another, v perpendicular or between two
  more), of three points clicked in the 3D view; a section takes away what stands in front of the plane and
  saves the profile of the cut (drawn on the orthophoto, exported as DXF); v is the height on upright planes,
  the markers are drawn on the elevation; `ppg elevation`.
* **GNSS positions of the photos, first step**: the RTK state and standard deviations of DJI photos (and of the
  bands of multispectral captures), the shots of a `Timestamp.MRK` with the lever arm of the antenna, positions
  files of any drone or program, the accuracy of each photo (twice the declared deviations, with the floors of
  its state) and the *GNSS* pane with a table per flight, the offsets of the aligned cameras and *Only the fix
  positions*; `ppg gnss`.
* **GNSS positions of the photos, PPK**: the positions of the shots of a `Timestamp.MRK` computed again
  from the RINEX of the drone and of a base - one's own or a permanent station - by RTKLIB-EX 2.5.1 inside the
  native core 0.5 (the `rnx2rtkp` of `PPG_RNX2RTKP` without it): the base at its surveyed position or at the one of
  its RINEX header, with the warning that it can be metres off, its antenna height added; the camera below the
  antenna by the lever arm of the shot, the status and accuracy of the solution, nothing guessed across its gaps;
  *Compute PPK...* in the *GNSS* pane, `ppg gnss ppk`.

## Next

Open points, in no fixed order:

* **GNSS, the next steps**: the position of the base from a nearby permanent station or from a static PPP, the
  list and the download of the public stations near a survey, Hatanaka RINEX read as they are, the reference frame
  and epoch of the positions and heights above the geoid, the quality per flight in the PDF report; the events of a
  receiver and the logs of ArduPilot and PX4 for drones built by hand.

* Parts never tried on real data: the import from WebODM, a satellite scene of one's own, `ppg remote`
  on a remote machine, a whole MicaSense flight to the reflectance orthomosaic.
* SpatiaLite on Windows ARM64: it waits for an ARM64 build of QGIS (its library is x64).
* geoai: the comparison of two coregistered dates and the height differences of their DEMs, after the
  evaluation on a real annotated survey.
* **Drawings on elevations and sections**: the units, lines, spot heights and points drawn on an elevation or a
  section kept with it, in its coordinates (u, v; v the height on upright planes), not among the drawings of the
  plan; shown again when the elevation is opened, exported with it (DXF, GeoPackage) and, for a section, sent to
  PyArchInit as a section drawing. The stone-by-stone drawings of elevations kept per elevation too (today they
  go by the file name, the same for every elevation).
* Ideas not decided yet: a **topology** plugin on topologicpy - stratigraphic relations read from the faces
  the 3D proxies of the units share (*covers* from horizontal contacts, *abuts* from vertical ones) into the
  Extended Matrix, overlapping proxies split into clean cells for the volumes, the rooms of standing
  buildings as cells. A plugin only: its geometric core is AGPL-3.0 and has no Windows ARM64 build.

## Not planned

Planetary coordinate systems; the boresight calibration of aerial lidar (no trajectory data to
validate it with; lidar clouds are imported and co-registered through the terrio plugin and the
chunk alignment). Powerline detection is not part of the program but the plugin `powerlines` of
the [plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins)
(conductors as catenaries, poles, clearance from the ground, distance from obstacles).
