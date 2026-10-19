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

## 0.9 - Everyday work

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

## 1.0 - Archaeology first

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
* QField package: orthomosaic and layers on the tablet in the trench, edits back to PyArchInit.
* Machine-readable paradata (provenance of every product) linked to the PyArchInit media.
* Pottery: rim diameter from a sherd, profile-based typology search against `pottery_table`.
* Automatic US proposal from SAM masks + stratigraphic relations for the Extended Matrix.
* IFC with stratigraphic relations and s3dgraphy/StratiGraph identifiers (`node_uuid`).

## Research

* CPU PatchMatch multi-view stereo in the native core (COLMAP's needs CUDA, which the Mac does not
  have; today the CPU path is two-view SGBM). A plane-sweep prototype on the Apple GPU found the
  same depths as the CPU path but ran 30 times slower than OpenCV SGBM.
* Own bundle adjustment in the native core: true distance constraints for scale bars,
  rolling-shutter compensation for drones, GNSS bias (pycolmap does not accept custom costs).
* Multi-page texture atlases, I3S.

## Not planned

Network and cloud processing, satellite imagery, planetary coordinate systems, aerial lidar
calibration, powerline detection, agricultural prescription maps.
