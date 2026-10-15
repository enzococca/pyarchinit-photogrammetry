# Roadmap

## 0.2 - Desktop interface

A Qt (PySide6) application on top of the same Python API, so everything done in the
GUI can be scripted and vice versa:

* workspace tree (document, chunks, photos, markers, scale bars, products);
* photo pane with marker placement and SAM 2.1 point/box prompts;
* 3D view of tie points, dense cloud, mesh and texture (VTK/pyvista), with section and
  plan-slice tools drawn directly on the model;
* ortho/DEM view with stratigraphic-unit drawing and PyArchInit labels;
* reference pane: GCP/check points, errors, scale bars, CRS;
* processing dialogs with progress and cancel, batch processing;
* Python console bound to `ppg.app.document`;
* PyArchInit panel: site/area/US selection, publishing, browsing media.

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
