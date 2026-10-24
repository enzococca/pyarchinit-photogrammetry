# Integrations: QGIS, Metashape, WebODM, Photo3D

## QGIS, live

*QGIS > Apri in QGIS (collegamento dal vivo)* (or the QGIS button in the drawing toolbar)
writes a QGIS project next to the `.ppg` (`<name>_qgis/<name>_ppg.qgs`) and opens it:

* rasters: the orthomosaic, the DEM (hillshade, hidden at first) and the DEM values;
* vectors, in the group *Disegni (aggiornati dal vivo)*: the tables of one GeoPackage,
  `disegni_ppg.gpkg`: `unita` (unit polygons with the columns of `pyunitastratigrafiche`,
  label = `etichetta`), `poligoni` (zones, stones, patches, the polygons of plugins), `sezioni`
  (section traces), `curve` (contour lines, label = elevation), `linee` (other lines), `punti`
  (measuring points and other points) and `quote` (spot heights with the columns of
  `pyarchinit_quote`, label = `quota_q`).

The GeoPackage is updated 0.4 s after every change in the interface (new drawing, removed
layer, edited attribute), in place and in one transaction, and the project asks QGIS to redraw
those layers every second: QGIS reads a GeoPackage again at every redraw, so what you draw here
appears in QGIS by itself, without *Refresh*.

### Both ways

With *QGIS > Collegamento dal vivo con QGIS (nei due sensi)* on (it is turned on by *Apri in
QGIS*, and stays on for a project already linked), what is saved in QGIS comes back too. Edit
the layers *Unita' stratigrafiche (ppg)*, *Quote (ppg)* or *Sezioni (ppg)* in QGIS as any layer
(*Toggle Editing*, draw, move vertices, change attributes in the attribute table, delete) and save
the edits: within a second the window has them.

* **Units**: the outline and the columns of `pyunitastratigrafiche` - `us_s`, `area_s`,
  `unita_tipo_s`, `scavo_s`, `stratigraph_index_us`, `tipo_us_s`, `disegnatore`, `data`... - and
  the other columns (also those added in QGIS) as attributes of the unit. A unit of several
  polygons is one row per part (`ppg_parte`). A unit drawn in QGIS becomes a unit here, in the
  layer *Unita' stratigrafiche*, with the site of the project when `scavo_s` is empty. A polygon
  loaded from PyArchInit and changed in QGIS waits, as when it is changed here, for *Salva
  modifiche in PyArchInit*.
* **Spot heights**: the point and the columns of `pyarchinit_quote` (`quota_q`, `us_q`...).
* **Sections**: the name (`etichetta`) and the trace (its first and last vertex). A section whose
  trace moved, or drawn in QGIS, is cut again in the background with the options of the chunk,
  when it has the model (or the dense cloud); the drawings of a section deleted in QGIS stay in
  `sezioni/`.

Each save made in QGIS is one entry of *Modifica > Annulla* (*Annulla: Da QGIS: US 6
modificata*; several features: *Da QGIS: 3 modifiche*) and a line of the log for each feature.
The drawings are stored in the chunk, the layers drawn again, and the GeoPackage brought to the
program's version of those rows (its label, its area...). When the outline of a unit that has a
3D proxy changes, the proxy is built again by itself, with the base it was built with (the plane,
the thickness or the DEM after the removal, kept with the proxy since this version; for a proxy
of an earlier version the missing value is asked once); when only its number changes the proxy
takes the new name. Undoing the entry brings the proxies back with the polygons, and the
sections are cut again along the traces they had.

How the program tells the edits of QGIS from its own: every feature has an identity, `ppg_id` (a
UUID kept with the unit, the spot height or the section, and also used by the QField package),
and the rows are written one by one by that identity, so a row keeps its `fid` while its feature
lives and an edit session of QGIS never loses its rows. Each row also holds `ppg_sig`, the
signature of what the program last wrote in it: a row whose geometry or attributes no longer
match was edited by another program. Rows without an identity were drawn there (they get one at
once); rows that disappeared were deleted there. The window looks once a second whether another
program committed to the file (`PRAGMA data_version` of a connection kept open: its own writes do
not count), and never writes over an edit or a deletion it has not read yet. Since the signatures live in the
file, the edits made in QGIS while the program was closed come back when the project is opened
again (deletions made meanwhile excepted: the program writes those features again).

* **Invalid outlines** (a polygon crossing itself) are made valid; one with no area left, or a
  feature without geometry, is skipped, with a line in the log.
* **Reference system**: a layer whose reference system is not the project's is refused (a line
  in the log), as is a single feature written in another one.
* **QGIS holding the file** while it saves: the window reads a moment later.
* **One way only**: contour lines, zones, stones, patches, measuring points and the drawings of
  plugins are computed by the program; an edit made to them in QGIS is not read back.

Up to 0.4.1 the drawings were GeoJSON files set to *reload data every second*, and new
drawings appeared only after *Refresh*: QGIS keeps GeoJSON in memory, and QGIS 3.34-3.42
does not start the refresh timer of a layer read from a project saved with
`autoRefreshMode` (the style, read first, sets the mode while the interval is still 0;
the mode is then set again unchanged), so the map kept showing its cached image. The
project is therefore written with the older `autoRefreshEnabled` attribute, which QGIS
reads in the right order. A project saved again by those QGIS versions gets the new
attribute and loses the automatic redraw on the next opening: open it from here (*Apri in
QGIS* rewrites it). Use *Apri in QGIS* once to move a project linked with 0.4.1 to the
GeoPackage. A project that has been opened in QGIS stays linked when it is reopened here;
rasters made after the QGIS project (a new DEM, a new orthomosaic) appear after *Apri in
QGIS* again. The project is created by QGIS's own
Python (`/Applications/QGIS*.app`, `C:\Program Files\QGIS*`, `/usr/bin/qgis`, or `PPG_QGIS`),
so styles and labels are native QGIS ones. Units published to PyArchInit
(`pyunitastratigrafiche`) are of course visible in the PyArchInit layers of QGIS too.

## Metashape (`.psx`)

*File > Importa > Progetto Metashape* or `ppg import-metashape progetto.psx nuovo.ppg`.
The project is read directly (no Metashape needed): sensors with their calibration
(converted to COLMAP `FULL_OPENCV`: `f`, `cx/cy` from the centre, `b1`, `k1..k3`, `p1/p2`
swapped to OpenCV order), aligned cameras, markers with references, accuracies and
projections, scale bars, tie points with colours, the model (`model.zip`), and the DEM/orthomosaic GeoTIFFs exported next
to the project (folders or names containing *dem/dsm* and *orto/ortho*). Without a usable
exported DEM (none, or an empty file left by a failed export, which is reported), the DEM
kept inside the project is used: Metashape stores it as GeoTIFF tiles listed in
`elevation.zip`, which are mosaicked and warped to the CRS of the chunk at Metashape's
resolution (`dem.tif` in the project; Tell Abu adh-Dhuhur: 8 tiles, 10,241 x 10,545 px at
5.9 cm in 30 s, 2 cm from the tie points, median). The chunk is placed
in the CRS of those GeoTIFFs, otherwise in the projected CRS of the project, or in the UTM
zone for geographic projects (similarity fitted through Metashape's geocentric frame).

A COLMAP model is written from the imported poses, so *Nuvola densa*, *Modello*, *DEM* and
*Ortofoto* run here without aligning again (photos must be where the project says).

Details of the import:

* scale bars between markers come with their distance, their accuracy (the bar's, else the
  chunk's *Scale bar accuracy*) and their type: a bar unticked in Metashape's Reference pane is a
  check bar. Metashape already scaled the chunk with them, so the transform is not fitted again:
  the *Riferimenti* pane shows every bar with its error (on four object projects of a season,
  0.5 m bars came back at 0.5000 m). Bars between cameras, or without a distance, are reported
  and left out. Marker accuracies come from the markers or from the chunk's *Marker accuracy*;
* a chunk in local coordinates scaled by scale bars has a transform with the scale alone (no
  rotation, no translation): it is applied (up to 0.5.1 the scale was dropped and such projects
  came in 3-14 times too large);
* cameras and markers inside groups (folders of Metashape's Workspace) are read; empty
  chunks are skipped and the chunk with most aligned cameras becomes the active one;
* tie points are read from every alignment component, each through its own transform;
  points left by components that no longer exist in the project (a stale frame, hundreds
  of metres away) are left out with a warning;
* cameras, tie points and model vertices are converted to the CRS point by point (camera
  rotations follow the local rotation of the conversion), not through one similarity;
* the model (`mesh.ply` in `model.zip`, up to tens of millions of faces) is extracted as a
  stream and a coarse copy made by vertex clustering (`model_preview.ply`, about 600,000
  faces) is what the 3D view shows; the vertex colours and the texture (a single-page
  `texture.tif`, written as `model_texture.jpg`) come with it, and *File > Importa > Modello
  e texture dal progetto Metashape* adds them to projects imported before;
* Metashape 2.x builds the mesh from depth maps, so most projects have no dense cloud: the
  coloured mesh vertices become the dense cloud (at most 8 million points). For a real dense
  cloud run *Nuvola densa* here (it uses Metashape's poses), or build it in Metashape;
* exported DEM/orthomosaic GeoTIFFs are searched next to the project, not inside other
  projects' folders, and linked only if they cover the cameras.

Checked on the drone projects of a survey season (29-3265 photos): Tell Dlamah imported in
6 s (tie points +3 mm and mesh vertices +4 mm from Metashape's DEM, median), a 425-photo
project with a camera group, a 3265-photo project with a deleted component (+13 mm). Not
read: Metashape's own orthomosaic storage (image blocks and seamlines: export it as
GeoTIFF), dense clouds in Metashape's octree format, masks, skew `b2`, `k4`.

## WebODM / ODM

*File > Importa > Progetto WebODM* or `ppg import-webodm all.zip nuovo.ppg` (or the task
folder). Orthophoto, DSM (or DTM), georeferenced point cloud (`.laz`/`.las`/`.ply`) and the
textured mesh (`odm_textured_model_geo.obj`, relative to the offset in `coords.txt`) become
the products of a chunk in the CRS of the rasters.

The cameras come with their calibration and pose (`cameras.json` and
`odm_report/shots.geojson`, both in `all.zip`), and the GCPs ODM used become markers
(`odm_georeferencing/ground_control_points.geojson`, or `gcp_list_utm.txt`), with their
surveyed coordinates and where they were measured in the photos. They are triangulated at
once: the *Riferimenti* pane shows their errors (the same ODM computed; its own figure is in
the import report), and scale bars can be drawn between them to check the scale. ODM has no
scale bars of its own: the scale of a task comes from the GPS of the photos or from its GCPs.
With the photos (the task folder has them, `all.zip` does not) the dense cloud, model and
texture can be built again here from ODM's poses. OpenSfM camera models: `perspective`,
`brown`, `radial`, `simple_radial`; fisheye and spherical cameras are left out. Poses of a
task that is not georeferenced are not imported.

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
