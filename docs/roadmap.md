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
orthomosaics built tile by tile.

## 0.6 - Interface

* Batch processing over several chunks.
* Section and plan-slice tools drawn directly in the 3D view; editable markers in 3D.
* PyArchInit panel: browsing media linked to US.
* English/Italian interface translation.

## Engine

* Plane-sweep MVS backend for strongly converging close-range photos (a first prototype on the
  Apple GPU found the same depths as the CPU path but ran 30 times slower than OpenCV SGBM).
* Seamless texture blending and multi-page atlases.
* Rig and multi-camera support, rolling-shutter compensation for drones.
* E57 import/export, Cloud Optimized GeoTIFF output, tiled 3D (3D Tiles/I3S).

## Archaeology and BIM

* Profile-based pottery typology search against `pottery_table`.
* Automatic US proposal from SAM masks + stratigraphic relations for the Extended Matrix.
* IFC with stratigraphic relations and s3dgraphy/StratiGraph identifiers (`node_uuid`).
