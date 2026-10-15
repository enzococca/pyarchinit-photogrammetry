# Integrations: QGIS, Metashape, WebODM, Photo3D

## QGIS, live

*QGIS > Apri in QGIS (collegamento dal vivo)* (or the QGIS button in the drawing toolbar)
writes a QGIS project next to the `.ppg` (`<name>_qgis/<name>_ppg.qgs`) and opens it:

* rasters: the orthomosaic, the DEM (hillshade, hidden at first) and the DEM values;
* vectors, in the group *Disegni (aggiornati dal vivo)*: `unita.geojson` (unit polygons,
  label = `etichetta`), `sezioni.geojson`, `curve.geojson` (contour lines, label = elevation),
  `linee.geojson` (other lines), `punti.geojson` (spot heights, label = `z`).

The vector files are rewritten 0.4 s after every change in the interface (new drawing,
removed layer, edited attribute) and the QGIS layers are set to *reload data every second*,
so what you draw here appears in QGIS while you draw. The project is created by QGIS's own
Python (`/Applications/QGIS*.app`, `C:\Program Files\QGIS*`, `/usr/bin/qgis`, or `PPG_QGIS`),
so styles and labels are native QGIS ones. Units published to PyArchInit
(`pyunitastratigrafiche`) are of course visible in the PyArchInit layers of QGIS too.

## Metashape (`.psx`)

*File > Importa > Progetto Metashape* or `ppg import-metashape progetto.psx nuovo.ppg`.
The project is read directly (no Metashape needed): sensors with their calibration
(converted to COLMAP `FULL_OPENCV`: `f`, `cx/cy` from the centre, `b1`, `k1..k3`, `p1/p2`
swapped to OpenCV order), aligned cameras, markers with references and projections, tie
points with colours, the model (`model.zip`), and the DEM/orthomosaic GeoTIFFs exported next
to the project (folders or names containing *dem/dsm* and *orto/ortho*). The chunk is placed
in the CRS of those GeoTIFFs, otherwise in the projected CRS of the project, or in the UTM
zone for geographic projects (similarity fitted through Metashape's geocentric frame).

A COLMAP model is written from the imported poses, so *Nuvola densa*, *Modello*, *DEM* and
*Ortofoto* run here without aligning again (photos must be where the project says).

Details of the import:

* cameras and markers inside groups (folders of Metashape's Workspace) are read; empty
  chunks are skipped and the chunk with most aligned cameras becomes the active one;
* tie points are read from every alignment component, each through its own transform;
  points left by components that no longer exist in the project (a stale frame, hundreds
  of metres away) are left out with a warning;
* cameras, tie points and model vertices are converted to the CRS point by point (camera
  rotations follow the local rotation of the conversion), not through one similarity;
* the model (`mesh.ply` in `model.zip`, up to tens of millions of faces) is extracted as a
  stream and a coarse copy made by vertex clustering (`model_preview.ply`, about 600,000
  faces) is what the 3D view shows; the texture is not imported (vertex colours are);
* Metashape 2.x builds the mesh from depth maps, so most projects have no dense cloud: the
  coloured mesh vertices become the dense cloud (at most 8 million points). For a real dense
  cloud run *Nuvola densa* here (it uses Metashape's poses), or build it in Metashape;
* exported DEM/orthomosaic GeoTIFFs are searched next to the project, not inside other
  projects' folders, and linked only if they cover the cameras.

Checked on the drone projects of a survey season (29-3265 photos): Tell Dlamah imported in
6 s (tie points +3 mm and mesh vertices +4 mm from Metashape's DEM, median), a 425-photo
project with a camera group, a 3265-photo project with a deleted component (+13 mm). Not
read: Metashape's own tiled DEM/orthomosaic storage (export them as GeoTIFF), dense clouds
in Metashape's octree format, masks, skew `b2`, `k4`.

## WebODM / ODM

*File > Importa > Progetto WebODM* or `ppg import-webodm all.zip nuovo.ppg` (or the task
folder). Orthophoto, DSM (or DTM), georeferenced point cloud (`.laz`/`.las`/`.ply`) and the
textured mesh (`odm_textured_model_geo.obj`, relative to the offset in `coords.txt`) become
the products of a chunk in the CRS of the rasters. Camera poses are not imported.

## Photo3D

Two ways:

* **as a Photo3D engine** — the Photo3D server runs engines as subprocesses with
  `PHOTO3D_SESSION_DIR` and `PHOTO3D_PRESET`; `ppg photo3d-job` follows that contract:
  `PROGRESS <stage> <pct>` lines, products in `<session>/metashape/` (`model.obj`,
  `pointcloud.ply`, `dem.tif`, `orthophoto.tif`, plus `ppg_project.ppg`) and
  `metashape/result.json`. It reads `gcp_list.txt` (ODM format, raw pixels), `target_list.json`
  (coded targets with surveyed coordinates: ArUco/AprilTag are recognised, Metashape's
  circular 12-bit ones are not) and `scale.json` (two taps and a length in mm: the taps are
  intersected with a local plane fitted to the dense cloud; 0.5 mm median error on 50-180 mm
  distances in the turntable test). Presets: preview / medium / high.
* **as an import** — *File > Importa > Sessione Photo3D* or `ppg import-photo3d <session>`:
  a `.ppg` with photos, targets and GCPs, to process in the interface.

To add the engine to Photo3D (repository `photo3d`, server in `mac/photo3d_server/`):
`config.py` (`ENGINE_NAMES` += `"ppg"`), `queue.py` (`build_command()`: run
`ppg photo3d-job` with the session environment), `engines/registry.py` (availability:
`ppg` on PATH or the packaged app), `api.py` (`health()` → `ppgAvailable`), and the engine
choice in the iOS (`NewSessionSheet.swift`) and Android (`MainActivity.kt`) apps. The
existing endpoints already serve the products from `metashape/` by their fixed names.

## Segment Anything 2.1 in the packaged application

PyTorch is not inside the application. The first time a SAM tool is used, the interface
offers to install it (*Strumenti > Segment Anything 2.1 > Installa*): `uv` (from PATH or
downloaded from its official GitHub release) creates a Python 3.12 environment in
`~/Library/Application Support/pyarchinit-photogrammetry/sam-env` (macOS),
`%LOCALAPPDATA%\pyarchinit-photogrammetry\sam-env` (Windows) or `~/.local/share/...` (Linux),
and installs PyTorch and `sam2` (1-2 GB). SAM then runs in a worker process of that
environment (`ai/sam_worker.py`), on the Apple GPU (MPS), CUDA or CPU; the model weights are
downloaded from Hugging Face on first use. A pip installation with the `[sam]` extra keeps
running SAM in process.
