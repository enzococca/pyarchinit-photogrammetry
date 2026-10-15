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
| Drawing toolbar | always visible under the workflow: the drawing modes (*Sposta / interroga*, *Disegna unita'*, *Traccia sezione*, *Misura*, *Quota puntuale / prompt SAM*), *Cancella disegni*, contour lines, volumes, plan slices, and shortcuts to the *Layer* and *Sezioni* panes and to *Pubblica unita'* |
| Left: **Area di lavoro** | project, chunks (click to make one active), CRS (double click to change), cameras (green camera = aligned, red = not aligned), markers (GCP, check points), products; double click opens a photo or a product |
| Left: **Riferimenti** | markers with use/check flags, editable coordinates and accuracy, errors (m and px), camera positions; *Importa CSV*, *Aggiorna* (georeferencing), *Ottimizza camere*; control and check RMSE |
| Left: **Layer** (editing) | double click a value in *Attributi* to change it (US, area, type, label, any free field; computed fields such as m2 and length are read-only). Units loaded from PyArchInit keep their `gid`: *Salva modifiche in PyArchInit* writes the changes back to `pyunitastratigrafiche`; drawn units are written with *Pubblica unita'* |
| Left: **Layer** | everything drawn on the orthomosaic/DEM is a layer: stratigraphic units, US polygons loaded from PyArchInit, section traces, contour lines, plan slices, spot heights. Tick to show/hide, double click to zoom (layer or single feature), *Rimuovi*; in *Sposta / interroga* mode a click on a feature shows its attributes (US, area, m2, elevation of a contour...) in the pane and in the status bar |
| Centre: **Modello** | tie points, dense cloud, shaded or textured mesh, camera frusta, markers. Left drag orbits, right/middle drag pans, wheel zooms |
| Centre: **Ortofoto / DEM** | orthomosaic or DEM (hypsometric tint and hillshade) in world coordinates; cursor X, Y, Z in the status bar |
| Centre: **Foto** | a photo with its marker projections (green = placed by hand, blue = detected) and reprojected marker positions (red crosses); right click to place or remove a marker |
| Bottom: **Foto** | thumbnails with an aligned/not aligned badge |
| Bottom: **Sezioni** | the sections of the project: list, preview of the SVG drawing, *Zoom sulla traccia*, *Apri cartella* (DXF profile and 3D DXF in `sezioni/`), *Pubblica in PyArchInit* (`pyarchinit_sezioni`), *Elimina*. Sections are saved in `sezioni/sezioni.json` next to the project and come back when it is reopened |
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
  cloud (quality, filter, engine), *Ritaglia nuvola densa* (height range and/or a cylinder
  around the turntable axis: removes the mat and the targets before meshing an object), model
  (source, surface, faces), texture, DEM, orthomosaic, *Elabora tutto* (Ctrl+R). The project
  is saved after each step once it has a file.
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
bowl and a sherd as meshes, printable targets and a PyArchInit database (full PyArchInit 5
schema) with the site *Saggio dimostrativo* (US 1-3, polygons, spot heights, a section
trace, pottery and finds) plus a Roman villa and a necropolis with tens of units, then opens
the project and connects the database.

The **pottery demo** (`ceramica/ceramica.ppg`, 32 photos of the bowl on a turntable mat with
12 ArUco targets in a local metric frame) shows the close-range workflow: *Elabora tutto*
without DEM and orthophoto, then *Strumenti > Profilo ceramico da mesh* on the exported
model; with `--process` the demo already contains `ceramica/profilo.svg` (expected rim 16 cm,
height 12 cm, wall 6 mm). The demo window lets you choose which project to open at the end.

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
| Connect PyArchInit | at start the interface connects by itself to the database configured in PyArchInit 5 (`~/pyarchinit_5/pyarchinit_DB_folder/config.cfg`, option *Connetti all'avvio*). *PyArchInit > Connetti...* opens the connection window: the PyArchInit configuration, a SQLite/SpatiaLite file, or a PostgreSQL/PostGIS server (host, port, database, user, password, *Prova connessione*). *Usa il database demo* creates `demo.sqlite` with the full PyArchInit 5 schema (three sites) inside `~/pyarchinit_5/pyarchinit_DB_folder` when that installation exists, so QGIS can open the same file; `config.cfg` is never touched. The right pane lists sites, US and documentation | `PyArchInit.connect(url=..., config_path=...)`, `.sites()`, `.stratigraphic_units(sito)`, `.documentation(sito)` |
| Sites | a site must have its *scheda Sito* before anything is published: *PyArchInit > Nuovo sito...* (or *Nuovo sito* in the pane and in the publish window) creates the record; publishing to a site that does not exist creates it automatically | `pa.ensure_site(sito, comune=...)`, `pa.site_exists(sito)` |
| Publish products | *PyArchInit > Pubblica ortofoto / DEM / modello*: choose the site, the document name and the US to link; rasters are copied next to PyArchInit's media folder, with thumbnails and a `documentazione_table` record | `ppg pyarchinit publish scavo.ppg "Sito" "Nome" --dem --model --link-us 1:101`; `pa.publish_orthomosaic/dem/model` |
| Stratigraphic units | *Strumenti > Disegno su ortofoto > Disegna unita'* (click the vertices, double click to close), then assign area/US/type; *PyArchInit > Pubblica unita' disegnate* writes `pyunitastratigrafiche`; *Carica poligoni US* shows the existing ones | `pa.publish_units(units, sito)`, `pa.us_polygons(sito)`, `archaeo.stratigraphy.export_units` |
| Spot heights | *Disegno > Quota puntuale* (left click) then *PyArchInit > Pubblica quote dei punti cliccati* (`pyarchinit_quote`) | `pa.publish_elevations(chunk, sito, area, us, points)` |
| Segment Anything 2.1 | needs `pip install "pyarchinit-photogrammetry[sam]"` (PyTorch; the first run downloads the model). *Strumenti > Segment Anything 2.1 > Unita' automatiche sull'ortofoto* proposes candidate units; *Segmenta dai punti cliccati* uses the points placed with *Quota puntuale / prompt SAM* (left = inside, right = outside); *Prompt sulle foto* segments an object on a photo and shows the mask | `ai.sam.Sam2Segmenter("small")`, `auto_units(chunk, segmenter)`, `segment_orthomosaic(chunk, segmenter, points=..., labels=...)`, `segment_photo_and_classify(...)` |
| Classification | *Strumenti > Classifica nuvola di punti*: ground, vegetation, masonry/structures and noise with rule-based criteria (ASPRS classes plus archaeological classes 64-71); the result is written in the dense cloud (LAS export keeps the classes) and shown in the 3D view | `ppg classify scavo.ppg`; `ai.classify.classify_chunk(chunk)`, `assign_class_by_polygons(chunk, units)`, `class_statistics` |
| Sections | *Traccia sezione* in the drawing toolbar, then two clicks on the orthomosaic: the model is cut along the trace, the drawing appears in the *Sezioni* pane (SVG, DXF profile and 3D DXF in `sezioni/`), the trace becomes a feature of the *Sezioni* layer; *Pianta a quota* cuts the model horizontally into its own layer | `ppg section scavo.ppg x1 y1 x2 y2 --out sezione.svg`; `archaeo.sections.section(chunk, p1, p2)`, `sections_along(chunk, baseline, interval, half_width)`, `plan_slice(chunk, elevation)`; `pa.publish_section_trace` |
| Contours and volumes | *Curve di livello* (DXF/GeoJSON/GeoPackage) draws the lines as a queryable layer (elevation, index line) that survives switching between DEM and orthomosaic; *Volume scavo / riporto*: the reference is the surface before the work (a plane at the original ground level or the DEM before excavation): *scavo* = volume removed (now below the reference), *riporto* = volume added; with "Quota minima" the deposit above the lowest point. Partial cells along the polygon edge are weighted by their covered fraction | `ppg contours`; `archaeo.terrain.contours`, `volume`, `hillshade`, `slope_degrees`; `pa.publish_volume` |
| Pottery profiles | *Strumenti > Profilo ceramico da mesh*: choose the mesh of a vessel or sherd (OBJ/PLY/STL), the scale and the units; the rotation axis is estimated from the surface, the profile with rim/base diameters, height and wall thickness is drawn as SVG/DXF | `ppg pottery vaso.ply --out profilo.svg --scale 1`; `archaeo.pottery.estimate_axis`, `best_profile`, `draw` |
| US forms | double click a US in the PyArchInit pane (or *Scheda US* in the Layer and Proxy panes) opens the PyArchInit US form (`us_table`): description, chronology, excavation data, characters, elevations and measures, and the stratigraphic relationships as an editable table (*Copre*, *Taglia*, *Si appoggia a*...), stored in PyArchInit's own format so its Harris matrix reads them. *Nuova US* / *PyArchInit > Nuova scheda US* creates a record (the site record too when missing); an existing site/area/US/type is refused | `pa.us_record`, `pa.create_us(sito, area, us, tipo, **fields)`, `pa.update_us(id_us, **fields)`, `parse_relationships`, `format_relationships` |
| US proxies | *Strumenti > Proxy 3D delle US*: each unit polygon (drawn, or from a PyArchInit site) becomes a closed solid between the project DEM (top) and a base: a plane at a given elevation, the DEM recorded after the unit was removed, a constant thickness or the lowest point. Its volume equals the cut/fill volume on the same cells. The *Proxy US* pane lists volume, plan area and elevations; *Vedi in 3D* shows them coloured over the model; *Esporta (Extended Matrix)* writes one OBJ with an object per unit (`US12`, `USM3`) plus the coordinate offset; *Pubblica in PyArchInit* registers each proxy as a 3D media linked to its US; *Quote nella scheda US* writes the min/max elevations into the form | `archaeo.proxies.unit_proxy`, `proxies_for_units`, `export_extended_matrix`, `save_proxy` |
| BIM / IFC | *Strumenti > Esporta IFC (BIM)*: choose the units (drawn here, the US polygons of a PyArchInit site, or the *proxy solids*) and whether to include the whole survey; the export shows its progress and, at the end, a window with the elements, their attributes (`Pset_PyArchInit_US`) and a 3D view of what was written. IFC4 in metres with `IfcMapConversion`. With proxies the file carries quantities, materials, groups and relationships: see [bim.md](bim.md) | `ppg ifc scavo.ppg rilievo.ifc`; `bim.ifc.export_chunk_ifc(...)`, `export_proxies_ifc(...)`, `read_ifc_elements(path)` |
| QGIS | *QGIS > Apri in QGIS (collegamento dal vivo)*: see [integrations.md](integrations.md) | `io.qgis_link.export_layers`, `build_project` |
| Import | *File > Importa*: Metashape `.psx`, WebODM/ODM, Photo3D sessions: see [integrations.md](integrations.md) | `ppg import-metashape`, `ppg import-webodm`, `ppg import-photo3d`, `ppg photo3d-job` |

Everything the interface does is also available in the console (*Console Python*: `doc`, `chunk`,
`ppg`) and in scripts (`ppg run script.py --project scavo.ppg`). See [api.md](api.md) and
[pyarchinit.md](pyarchinit.md).
