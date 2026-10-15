# Roadmap

## 0.2 - Desktop interface (done)

Workspace tree, 3D view (VTK or software), orthomosaic/DEM view with drawing tools, photo
view with marker placement and SAM prompts, reference pane, Python console, background
processing with cancel, PyArchInit menu, demo data (`ppg demo`).

## 0.3 - Interface

* Batch processing over several chunks; chunk alignment and merging.
* Section and plan-slice tools drawn directly in the 3D view; editable markers in 3D.
* Point-cloud class editing (lasso) and filtering by class or confidence.
* PyArchInit panel: browsing media and opening linked US forms; attribute editing of units.
* English/Italian interface translation.

## Engine

* GCP-constrained bundle adjustment (markers as observations in the adjustment).
* Plane-sweep MVS backend for strongly converging close-range photos.
* Seamless texture blending and multi-page atlases.
* Rig and multi-camera support, rolling-shutter compensation for drones.
* E57 import/export, Cloud Optimized GeoTIFF output, tiled 3D (3D Tiles/I3S).

## Archaeology and BIM

* Profile-based pottery typology search against `pottery_table`.
* Automatic US proposal from SAM masks + stratigraphic relations for the Extended Matrix.
* IFC with stratigraphic relations and s3dgraphy/StratiGraph identifiers (`node_uuid`).
