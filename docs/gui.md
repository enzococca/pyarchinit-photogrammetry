# Desktop interface

```bash
pip install "pyarchinit-photogrammetry[gui]"   # included in [all]
ppg-gui [project.ppg]                          # or: ppg gui, python -m pyarchinit_photogrammetry.gui
```

The interface is in Italian, like PyArchInit. Every action calls the same Python API that
scripts use, so anything done with the mouse can be repeated in the console or in a script.

## Layout

| Area | Content |
| --- | --- |
| Left: **Area di lavoro** | project, chunks (click to make one active), CRS (double click to change), cameras (aligned / not aligned), markers, products; double click opens a photo or a product |
| Left: **Riferimenti** | markers with use/check flags, editable coordinates and accuracy, errors (m and px), camera positions; *Importa CSV*, *Aggiorna* (georeferencing), *Ottimizza camere*; control and check RMSE |
| Centre: **Modello** | tie points, dense cloud, shaded or textured mesh, camera frusta, markers. Left drag orbits, right/middle drag pans, wheel zooms |
| Centre: **Ortofoto / DEM** | orthomosaic or DEM (hypsometric tint and hillshade) in world coordinates; cursor X, Y, Z in the status bar |
| Centre: **Foto** | a photo with its marker projections (green = placed by hand, blue = detected) and reprojected marker positions (red crosses); right click to place or remove a marker |
| Bottom | photo thumbnails, **Console Python** (`doc`, `chunk`, `app`, `ppg`, `np`, `window`), log |
| Right: **PyArchInit** | connection, sites, stratigraphic units and documentation |

The 3D view uses VTK (pyvista/pyvistaqt) when OpenGL is available, otherwise a software
renderer (force it with `PPG_RENDERER=software`, or VTK with `PPG_RENDERER=vtk`).

## Menus

* **File**: new/open/save, recent projects, export (cloud, model, DEM, orthomosaic, cameras,
  references), HTML report, *Crea e apri progetto demo*.
* **Flusso di lavoro**: add photos/folder, add chunk, detect coded markers, align (quality,
  pair selection, limits), import references, update georeferencing, optimise cameras, dense
  cloud (quality, filter, engine), model (source, surface, faces), texture, DEM, orthomosaic,
  *Elabora tutto* (Ctrl+R). Long steps run in the background with progress and *Annulla*;
  the project is saved after each step once it has a file.
* **Strumenti**: printable target sheets and turntable mats (with the CSV of the target
  coordinates); drawing on the orthomosaic: stratigraphic units (click the vertices, double
  click to close), section traces (two clicks: SVG drawing, DXF profile and 3D DXF in
  `sezioni/`), distance and height difference, spot heights / SAM prompts; contour lines;
  cut/fill volumes (drawn unit, plane or second DEM); plan slice at a given elevation;
  pottery profile from a mesh (vessel or sherd); point-cloud classification; Segment Anything
  2.1 (automatic candidate units, units from clicked points, prompts on photos); IFC export
  (one element per drawn unit, PyArchInit attributes when connected); run a Python script.
* **PyArchInit**: connect through `config.cfg` (or a SQLite path / PostgreSQL URL), use the
  demo database, publish orthomosaic/DEM/model with links to US, publish drawn units to
  `pyunitastratigrafiche`, load existing unit polygons onto the orthomosaic, publish spot
  heights to `pyarchinit_quote`.

## Demo

**File > Crea e apri progetto demo** (or `ppg demo <folder> --process`) writes a synthetic
4 x 3 m trench with a wall stub, 28 photos and 8 ArUco targets at EPSG:3004 coordinates, a
bowl and a sherd as meshes, printable targets and a PyArchInit database with site
*Saggio dimostrativo* and US 1-3, then opens the project and connects the database.
