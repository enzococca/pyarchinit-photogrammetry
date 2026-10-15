# Desktop interface

```bash
pip install "pyarchinit-photogrammetry[gui]"   # included in [all]
ppg-gui [project.ppg]                          # or: ppg gui, python -m pyarchinit_photogrammetry.gui
ppg-gui --doctor                               # environment diagnosis (same as ppg doctor)
ppg-gui --check                                # opens, renders and exits (smoke test)
```

Or the packaged application for macOS and Windows: [packaging.md](packaging.md).

The interface is in Italian, like PyArchInit. Every action calls the same Python API that
scripts use, so anything done with the mouse can be repeated in the console or in a script.

## Start page

Without a project the central area shows the start page: *Nuovo progetto*, *Apri
progetto...*, *Progetto demo*, the recent projects, and links to the guide, the
environment diagnosis and the log folder. *File > Pagina iniziale* brings it back.

## Layout

| Area | Content |
| --- | --- |
| Toolbar | the workflow with coloured icons: open/save, photos, markers, alignment, references, dense cloud, model, texture, DEM, orthophoto, *Elabora tutto*, publish to PyArchInit. Processing actions are disabled while a task runs |
| Left: **Area di lavoro** | project, chunks (click to make one active), CRS (double click to change), cameras (green camera = aligned, red = not aligned), markers (GCP, check points), products; double click opens a photo or a product |
| Left: **Riferimenti** | markers with use/check flags, editable coordinates and accuracy, errors (m and px), camera positions; *Importa CSV*, *Aggiorna* (georeferencing), *Ottimizza camere*; control and check RMSE |
| Centre: **Modello** | tie points, dense cloud, shaded or textured mesh, camera frusta, markers. Left drag orbits, right/middle drag pans, wheel zooms |
| Centre: **Ortofoto / DEM** | orthomosaic or DEM (hypsometric tint and hillshade) in world coordinates; cursor X, Y, Z in the status bar |
| Centre: **Foto** | a photo with its marker projections (green = placed by hand, blue = detected) and reprojected marker positions (red crosses); right click to place or remove a marker |
| Bottom: **Foto** | thumbnails with an aligned/not aligned badge |
| Bottom: **Console Python** | `doc`, `chunk`, `app`, `ppg`, `np`, `window` |
| Bottom: **Log** | time, level and message of everything that happened; filter (all / warnings and errors / errors), search, *Copia*, *Salva...*, *Cartella log*, *Pulisci*; the traceback of an error is in the row tooltip and in the saved file |
| Right: **PyArchInit** | connection, sites, stratigraphic units and documentation |
| Status bar | task progress with a stop button while something runs; cursor coordinates |

The 3D view uses VTK (pyvista/pyvistaqt) when OpenGL is available, otherwise a software
renderer (force it with `PPG_RENDERER=software`, or VTK with `PPG_RENDERER=vtk`).
*Vista > Tema* switches between the light and the dark theme (remembered).

## Errors and logs

Long steps run in the background with a progress window and *Annulla*. When a step fails,
an error window shows a one-line summary, a hint for known problems (missing SpatiaLite or
PyTorch, a crashed reconstruction, a stale Anaconda install...), the traceback under
*Dettagli*, and the buttons *Copia* (summary + traceback, ready for a bug report) and
*Apri cartella log*. The same text goes to the Log pane and to the log file
(`ppg-gui.log`, rotated at 2 MB) in the platform log folder; native crashes are written to
`crash.log` in the same folder by Python's faulthandler.

*Aiuto > Diagnostica ambiente* lists Python, every library with its version, the known
pitfalls (another `ppg-gui` earlier on `PATH`, the Anaconda base environment, Open3D 0.20.0,
old pyvistaqt, missing SpatiaLite) and what to do about them; *Copia* puts the report on the
clipboard.

## Menus

* **File**: new/open/save, recent projects, export (cloud, model, DEM, orthomosaic, cameras,
  references), HTML report, *Crea e apri progetto demo*, *Pagina iniziale*.
* **Flusso di lavoro**: add photos/folder, add chunk, detect coded markers, align (quality,
  pair selection, limits), import references, update georeferencing, optimise cameras, dense
  cloud (quality, filter, engine), model (source, surface, faces), texture, DEM, orthomosaic,
  *Elabora tutto* (Ctrl+R). The project is saved after each step once it has a file.
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
* **Vista**: panes, views (F2 model, F3 orthomosaic/DEM, F4 photo), console (Ctrl+Shift+P), theme.
* **Aiuto**: online documentation, environment diagnosis, log folder, about.

## Demo

**File > Crea e apri progetto demo** (or `ppg demo <folder> --process`) writes a synthetic
4 x 3 m trench with a wall stub, 28 photos and 8 ArUco targets at EPSG:3004 coordinates, a
bowl and a sherd as meshes, printable targets and a PyArchInit database with site
*Saggio dimostrativo* and US 1-3, then opens the project and connects the database.

## Robustness notes

* Message boxes are Qt's own (non-native): the native `NSAlert` of macOS 26/27 aborts the
  process with Qt 6.
* Poisson reconstruction runs in a child process (`engine/isolate.py`): a crash of the
  native code becomes an error in the log, the step retries at a shallower depth and finally
  builds a 2.5D height field.
* Open3D is imported before rasterio/SciPy (`_env.load_open3d_first`) and the PROJ data
  exported by Anaconda or QGIS is replaced by rasterio's own, also for worker threads.

## Where the archaeology, AI and BIM tools are

| Task | Interface | Command line / API |
| --- | --- | --- |
| Connect PyArchInit | *PyArchInit > Connetti (config.cfg)* reads the database configured in PyArchInit (`~/pyarchinit/pyarchinit_DB_folder/config.cfg`); *Connetti a un database* takes a SQLite path or a `postgresql://` URL; *Usa il database demo*. The right pane lists sites, US and documentation | `PyArchInit.connect()`, `.sites()`, `.stratigraphic_units(sito)`, `.documentation(sito)` |
| Publish products | *PyArchInit > Pubblica ortofoto / DEM / modello*: choose the site, the document name and the US to link; rasters are copied next to PyArchInit's media folder, with thumbnails and a `documentazione_table` record | `ppg pyarchinit publish scavo.ppg "Sito" "Nome" --dem --model --link-us 1:101`; `pa.publish_orthomosaic/dem/model` |
| Stratigraphic units | *Strumenti > Disegno su ortofoto > Disegna unita'* (click the vertices, double click to close), then assign area/US/type; *PyArchInit > Pubblica unita' disegnate* writes `pyunitastratigrafiche`; *Carica poligoni US* shows the existing ones | `pa.publish_units(units, sito)`, `pa.us_polygons(sito)`, `archaeo.stratigraphy.export_units` |
| Spot heights | *Disegno > Quota puntuale* (left click) then *PyArchInit > Pubblica quote dei punti cliccati* (`pyarchinit_quote`) | `pa.publish_elevations(chunk, sito, area, us, points)` |
| Segment Anything 2.1 | needs `pip install "pyarchinit-photogrammetry[sam]"` (PyTorch; the first run downloads the model). *Strumenti > Segment Anything 2.1 > Unita' automatiche sull'ortofoto* proposes candidate units; *Segmenta dai punti cliccati* uses the points placed with *Quota puntuale / prompt SAM* (left = inside, right = outside); *Prompt sulle foto* segments an object on a photo and shows the mask | `ai.sam.Sam2Segmenter("small")`, `auto_units(chunk, segmenter)`, `segment_orthomosaic(chunk, segmenter, points=..., labels=...)`, `segment_photo_and_classify(...)` |
| Classification | *Strumenti > Classifica nuvola di punti*: ground, vegetation, masonry/structures and noise with rule-based criteria (ASPRS classes plus archaeological classes 64-71); the result is written in the dense cloud (LAS export keeps the classes) and shown in the 3D view | `ppg classify scavo.ppg`; `ai.classify.classify_chunk(chunk)`, `assign_class_by_polygons(chunk, units)`, `class_statistics` |
| Sections | *Disegno > Traccia sezione* (two clicks on the orthomosaic): the model is cut along the trace, the drawing opens (SVG) and the DXF files go to `sezioni/` next to the project; *Pianta a quota* cuts the model horizontally | `ppg section scavo.ppg x1 y1 x2 y2 --out sezione.svg`; `archaeo.sections.section(chunk, p1, p2)`, `sections_along(chunk, baseline, interval, half_width)`, `plan_slice(chunk, elevation)`; `pa.publish_section_trace` |
| Contours and volumes | *Strumenti > Curve di livello* (DXF/GeoJSON/GeoPackage) and *Volume scavo / riporto* (drawn unit, plane at a given elevation or a second DEM) | `ppg contours`; `archaeo.terrain.contours`, `volume`, `hillshade`, `slope_degrees`; `pa.publish_volume` |
| Pottery profiles | *Strumenti > Profilo ceramico da mesh*: choose the mesh of a vessel or sherd (OBJ/PLY/STL), the scale and the units; the rotation axis is estimated from the surface, the profile with rim/base diameters, height and wall thickness is drawn as SVG/DXF | `ppg pottery vaso.ply --out profilo.svg --scale 1`; `archaeo.pottery.estimate_axis`, `best_profile`, `draw` |
| BIM / IFC | *Strumenti > Esporta IFC (BIM)*: IFC4 with georeferencing (`IfcMapConversion`), one element per drawn unit with the PyArchInit attributes (`Pset_PyArchInit_US`) when a database is connected | `ppg ifc scavo.ppg rilievo.ifc`; `bim.ifc.export_chunk_ifc(chunk, path, units=..., us_records=...)` |

Everything the interface does is also available in the console (*Console Python*: `doc`, `chunk`,
`ppg`) and in scripts (`ppg run script.py --project scavo.ppg`). See [api.md](api.md) and
[pyarchinit.md](pyarchinit.md).
