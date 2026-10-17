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

## Start and opening a project

`ppg-gui` (and the packaged application) shows at once a splash in the colours of the logo
with a progress bar in the middle: the percentage and what is loading (NumPy and Open3D, PROJ
and GDAL, COLMAP, the VTK 3D view, the interface). The window appears in about a second
(the packaged application took eleven); the log records how long the start took. Opening a
project shows the same card over the window (project, workspace, 3D view, orthophoto and
DEM); the photo thumbnails come afterwards in the background, from the small previews the
cameras store in the EXIF (a few milliseconds per photo; the status bar counts them), so
hundreds of drone photos no longer freeze the window. Photos that cannot be read (empty
files kept online only by Dropbox or iCloud) get a dark thumbnail and a tooltip.

## Start page

Without a project the central area shows the start page: *Nuovo progetto*, *Apri
progetto...*, *Progetto demo*, the recent projects, and links to the guide, the
environment diagnosis and the log folder. *File > Pagina iniziale* brings it back.

## Layout

| Area | Content |
| --- | --- |
| Toolbar | the workflow with coloured icons: open/save, photos, markers, alignment, references, dense cloud, model, texture, DEM, orthophoto, *Elabora tutto*, publish to PyArchInit. Processing actions are disabled while a task runs |
| Drawing toolbar | always visible under the workflow: the drawing modes (*Sposta / interroga*, *Disegna US*, *Traccia sezione*, *Misura*, *Quota*, *SAM* with its menu), *Cancella disegni*, contour lines, volumes, plan slices, and shortcuts to the *Layer* and *Sezioni* panes and to *Pubblica* (units and spot heights) |
| Left: **Area di lavoro** | project, chunks (click to make one active), CRS (double click to change), cameras (green camera = aligned, red = not aligned), markers (GCP, check points), products; double click opens a photo or a product |
| Left: **Riferimenti** | markers with use/check flags, editable coordinates and accuracy, errors (m and px), camera positions; *Importa CSV*, *Aggiorna* (georeferencing), *Ottimizza camere* (with 3 or more control points they take part in the bundle adjustment and bend the block; check points stay out; control/check errors before and after in the log); *Barra di scala...* and *Importa barre...*; *Coordinate stimate* (as Metashape's *View Estimated*: the X, Y, Z columns show the estimated coordinates of the markers instead of the reference ones; on by itself when no marker has reference coordinates); the scale bars below the markers (see *Scale bars*); control and check RMSE of points and bars. The tooltip of a marker tells how it is placed: triangulated from N photos, on the surface from one photo, or added on the model or the orthophoto |
| Left: **Layer** (editing) | double click a value in *Attributi* to change it (US, area, type, label, any free field; computed fields such as m2 and length are read-only). Units loaded from PyArchInit keep their `gid`: *Salva modifiche in PyArchInit* writes the changes back to `pyunitastratigrafiche`; drawn units and spot heights are written with *Pubblica*; written features keep their `gid`, so nothing is written twice |
| Left: **Layer** | everything drawn on the orthomosaic/DEM is a layer: stratigraphic units, US polygons loaded from PyArchInit, section traces, contour lines, plan slices, spot heights. Tick to show/hide, double click to zoom (layer or single feature), *Rimuovi*; in *Sposta / interroga* mode a click on a feature shows its attributes (US, area, m2, elevation of a contour...) in the pane and in the status bar |
| Centre: **Modello** | tie points, dense cloud, shaded or textured mesh, camera frusta, markers. Left drag orbits, right/middle drag pans, wheel zooms |
| Centre: **Ortofoto / DEM** | orthomosaic or DEM (hypsometric tint and hillshade) in world coordinates; cursor X, Y, Z in the status bar |
| Centre: **Foto** | a photo with its marker projections (green = placed by hand, blue = detected) and reprojected marker positions (red crosses); right click to place or remove a marker |
| Bottom: **Foto** | thumbnails with an aligned/not aligned badge |
| Bottom: **Sezioni** | the sections of the project: list, preview of the SVG drawing (fitted to the pane, with lines and labels thickened so that a drawing metres long on paper stays readable), *Zoom sulla traccia*, *Apri cartella* (DXF profile and 3D DXF in `sezioni/`), *Pubblica in PyArchInit* (`pyarchinit_sezioni`), *Elimina*. Sections are saved in `sezioni/sezioni.json` next to the project and come back when it is reopened |
| Bottom: **Console Python** | `doc`, `chunk`, `app`, `ppg`, `np`, `window` |
| Bottom: **Log** | time, level and message of everything that happened; filter (all / warnings and errors / errors), search, *Copia*, *Salva...*, *Cartella log*, *Pulisci*; the traceback of an error is in the row tooltip and in the saved file |
| Right: **PyArchInit** | connection, sites, stratigraphic units and documentation |
| Status bar | task progress with a stop button while something runs; cursor coordinates |

The window fits a laptop screen, down to about 1000 x 650 pixels: the buttons of the panes
wrap onto more lines when a pane is narrow, a pane that is too short for its content
scrolls, and a toolbar that does not fit drops its texts (the workflow bar all at once,
the drawing bar from its last buttons, so the drawing tools keep their names longest; the
tooltips keep the names).

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
  references, and all of them cut with a box or a polygon), HTML report, *Crea e apri progetto
  demo*, *Pagina iniziale*.
* **Flusso di lavoro**: add photos/folder, *Ricollega foto* (the photos with the same names in
  another folder and its subfolders: a copy on an external disk of a Dropbox folder kept online
  only, a moved folder), add chunk, *Allinea chunk* (bring other chunks into the frame of a reference chunk by markers with the same name, photos in common or overlapping clouds), *Unisci chunk* (a new chunk with the photos, markers, tie points, clouds and optionally meshes of aligned chunks), detect coded markers, align (quality,
  pair selection, limits), import references, update georeferencing, *Nuova barra di scala (distanza tra due marker)*, *Importa barre di scala (CSV)*, optimise cameras, dense
  cloud (quality, filter, engine), *Ritaglia nuvola densa* (height range and/or a cylinder
  around the turntable axis: removes the mat and the targets before meshing an object), model
  (source, surface, faces), texture, DEM, orthomosaic, *Elabora tutto* (Ctrl+R); *Pulizia e
  confidenza* (confidence of the dense cloud and of the model, cleaning of the dense cloud, of
  the model and of the tie points, the lasso, the crop tool). The project is saved after each
  step once it has a file. Steps that read the photos (depth maps, texture, orthomosaic,
  *Elabora tutto*) first list the photos that cannot be read (empty files kept online only by
  Dropbox, iCloud or OneDrive) and offer *Ricollega a un'altra cartella*, *Continua senza
  queste foto* or *Annulla*.
* **Strumenti**: printable target sheets, turntable mats (with the CSV of the target
  coordinates) and scale bars; drawing on the orthomosaic: stratigraphic units (click the vertices, double
  click to close), section traces (two clicks: SVG drawing, DXF profile and 3D DXF in
  `sezioni/`), distance and height difference, spot heights / SAM prompts; contour lines;
  cut/fill volumes (drawn unit, plane or second DEM); plan slice at a given elevation;
  pottery profile from a mesh (vessel or sherd); point-cloud classification; Segment Anything
  2.1 (automatic candidate units, units from clicked points, prompts on photos); IFC export
  (one element per drawn unit, PyArchInit attributes when connected); run a Python script;
  *Patch dell'ortofoto (assegna immagini)*: draw an area and choose its photos, assign photos to
  the selected polygon, update the orthomosaic with the patches, remove every patch (see below).
* **PyArchInit**: connect through `config.cfg` (or a SQLite path / PostgreSQL URL), use the
  demo database, publish orthomosaic/DEM/model with links to US, publish drawn units to
  `pyunitastratigrafiche`, load existing unit polygons onto the orthomosaic, publish spot
  heights to `pyarchinit_quote`.
* **Vista**: panes, views (F2 model, F3 orthomosaic/DEM, F4 photo), console (Ctrl+Shift+P), theme.
* **Aiuto**: online documentation, environment diagnosis, log folder, about.

## Orthophoto at full resolution and patches

* **Zoom**: the orthomosaic/DEM view opens a large raster from a reduced copy (at most 6,000 px a
  side, one of the overviews of the file) and, when the zoom pauses beyond that resolution, reads
  the visible area again (plus a margin) at the resolution of the screen, up to the full one, in a
  thread. The DEM detail keeps the colour limits of the whole DEM. Tell Abu adh-Dhuhur (44,952 px):
  0.7 s to open, 10.8 cm/px before the detail, 1.38 cm/px after 0.37 s.
* **Patch** (drawing bar, or *Strumenti > Patch dell'ortofoto*): drag a box or click the vertices
  of a polygon and double click, or select a polygon (a unit) and choose *Assegna immagini a
  quest'area* from its menu. The dialog shows the area now and, for the photo chosen in the list,
  the area as it would come out (the rest as it is); the list has the photos that frame the area,
  best first (framed share x closeness, verticality and centrality), with the share framed, the
  angle from the vertical, the ground pixel, the share of the area the orthomosaic takes from it
  now, and in the tooltip how much of it the model hides (tree crowns, walls). Choose one or more
  photos (Shift/Cmd), *Usa solo* or *Escludi*, *Sul DEM* (a true orthophoto) or *Sul suolo attorno
  all'area* (heights along the polygon interpolated inside: trees, vehicles and people come out
  as in the photo, and holes of the DEM are filled), and optionally the texture of the model in
  the area. *Applica* saves the patch and redraws only its cells, then the overviews of that
  window; the view stays where it was.
* The patches are a layer, *Patch ortofoto*: select one and press Delete (or *Elimina selezionati*)
  and the orthomosaic is redrawn there without it. A later build of the orthomosaic applies every
  patch again; *Aggiorna l'ortofoto con le patch* applies the ones not applied yet (after an
  interrupted update). An orthomosaic of another program (exported by Metashape) is copied into
  the chunk folder at the first patch, and its pixels come back where a patch is removed.
* **Texture**: in the 3D view, the lasso on the model offers *Correggi la texture di queste facce
  (assegna le immagini)*: the photos that see those faces, each with the part of the photo where
  they are; every face takes the best of the chosen photos that sees it.

## Scale bars

**Markers.** A marker measured in one photo only lies where its ray meets the surface (the dense
cloud, else the tie points: a local plane fitted around the ray, about 20 pixels wide), so it has
coordinates, shows on the model and on the orthophoto and can end a scale bar at once; measured
in a second photo it is triangulated (on the synthetic trench, one-photo markers are within
0.6 mm of the surveyed targets). Markers can also be added without a photo, as Metashape's *Add
Marker*: *Marker* in the 3D view (the view freezes; click a point of the model or the cloud: the
nearest shown point under the cursor, refined along the line of sight on the full dense cloud)
and *Marker* in the drawing bar (click the orthophoto or the DEM: the height from the DEM at full
resolution, else from the cloud along the vertical). The new marker (`point 1`, `point 2`, ...) is
projected on the photos that see it, not hidden by the surface (blue flags: drag one in a photo to
pin it); the markers are drawn on the orthophoto as the *Marker* layer.

A scale bar is the known distance between two markers, as in Metashape. In *Riferimenti* select two
markers (Cmd/Shift click) and choose *Crea barra di scala tra A e B...* from the right-click menu, or
press *Barra di scala...* (the selected markers come first in the dialog). The dialog asks for the
distance in metres (between the centres of the targets), its accuracy (it weighs the bar when there
are several), whether it is a check bar, and whether to update the scale at once.

* **Control bars** scale the chunk: the scale is the least-squares solution of *scale x length in the
  model = distance*, each bar weighted by `1 / accuracy²` (of two bars measured equally well, the
  longer one counts more). Without control points the chunk is scaled in local coordinates (CRS
  `LOCAL`): an object, a find, a wall, a trench.
  The distance of a bar is the 3D one: with a height difference it is more than the distance in
  plan measured on the orthophoto.
* **Georeferenced chunks**: a new bar is proposed as a check (it measures the scale without
  changing it). A control bar on a chunk georeferenced elsewhere (an imported project) with no
  reference to fit it again scales it about the bar, in its CRS; when the scale changes, the log
  says that the DEM and the orthophoto are still in the old one and are to be built again.
* **Check bars** (*Check* ticked) do not scale: their error tells how good the scale is.
* The table under the markers has a row per bar: *Usa* (untick to leave it out), name, markers,
  *Distanza* and *Accur.* (double click to change them: the error follows at once), *Check*, the
  distance measured on the model (*Stimata*) and the error, in red when it exceeds three times the
  accuracy (at least 5 mm). Right click on rows deletes the bars. The summary shows *RMSE barre*
  (control) and *RMSE barre check*, and how the chunk was placed: *scala dalle barre, coordinate
  locali* or *scala da riferimenti e barre insieme*.
* **Photos with GPS**: when their positions cover less than ten times their accuracy (a phone or a
  drone over an object or a trench) they are left out and the bars scale the chunk in local
  coordinates. Over a larger area (a flight over a site), or with three or more control points, the
  photo positions, the control points and the bars are fitted together, each weighted by
  `1 / accuracy²`: positions and points place and turn the survey, the scale comes from all of them.
* The 3D view draws the bars between their markers, control bars in cyan and check bars dashed in
  orange, with their length and error; the workspace lists them under the chunk.
* *Importa barre...* (or *Flusso di lavoro > Importa barre di scala (CSV)*): a text file with
  `marker_a, marker_b, distance[, accuracy[, check]]` per line, separated by commas, semicolons,
  tabs or spaces; markers by name (`target 12`) or target code (`12`); a header line is skipped and
  pairs whose markers are not in the chunk are listed in the log.
* *Ottimizza camere* uses the bars too, as Metashape does: with two control bars or more, or with
  control points, the markers at the ends of the bars are fixed where the bars get their lengths
  (each moved as little as its precision in the photos allows, `MARKER_PIXEL_ACCURACY` 0.5 px) and
  the block bends towards them as far as the tie points allow; a single bar only gives the scale.
  The log reports the bar errors before and after.
* Projects imported from Metashape bring their scale bars (distance, accuracy, check bars); WebODM
  tasks bring the camera poses and their GCPs as markers, so bars can be drawn between them.
* *Strumenti > Target > Barra di scala stampabile...*: one page per bar with two coded targets at a
  known distance (the last codes of the dictionary, 48-49 for `aruco_4x4_50`, so they never clash
  with target sheets and turntable mats), a 10 cm ruler to check that it was printed at 100 %, and
  the CSV of the bars next to the PDF: glue the page on something rigid, photograph it with the
  object, detect the markers and import the CSV.

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

* Photos kept "online only" by Dropbox, iCloud or OneDrive are empty placeholder files: the dense cloud step lists them
  before it starts and offers to go on without them (`build_depth_maps(skip_unreadable=True)`).
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
| Site of the project | every unit and spot height belongs to one excavation: the site is chosen once (*PyArchInit > Sito del progetto*, the combo at the top of the PyArchInit pane, or the question asked the first time something is drawn or published) and saved in the project. A PyArchInit database may hold several sites: the pane then lists the US and documents of the project's site only; the US form has the site among the database's sites | `doc.meta["sito"]` |
| Stratigraphic units | *Disegna US* in the drawing bar (click the vertices, double click to close; with *Mano libera* press, trace and release), then the fields of PyArchInit's `pyunitastratigrafiche`: area, US (or an existing US of the site), unit type (`unita_tipo_s`), stratigraphic index (`stratigraph_index_us`: 1 characterization, 2 US outline), type (`tipo_us_s`), draughtsman, date, original survey; the layer shows the same columns (`scavo_s`, `area_s`, `us_s`...). Drawn units are saved in the project and come back when it is reopened. *Snap* (with its menu: vertices, segments, tolerance 6/12/20 px) puts the points of every drawing tool exactly on the vertices or edges of the visible layers, shown by a magenta square (vertex) or circle (segment): units traced along an existing boundary share it, without gaps or overlaps; *PyArchInit > Pubblica unita' disegnate* writes `pyunitastratigrafiche`; *Carica poligoni US* shows the existing ones | `pa.publish_units(units, sito)`, `pa.us_polygons(sito)`, `archaeo.stratigraphy.export_units` |
| Select, delete, merge | *Seleziona* in the drawing bar: click a feature, Shift/Cmd+click to add or remove, drag a box to select everything it touches (hidden layers are skipped); the selection is outlined in cyan and, for a single feature, its attributes appear in the Layer pane. *Elimina* (or the Delete key) removes units, single parts of multi-part units, section traces with their drawings, contour/plan-slice lines and points; *Unisci* merges units into one polygon (you choose whose US number, area and type to keep; hairline gaps between units drawn side by side are closed) or contiguous lines of the same layer. Right click opens the same commands, Esc clears the selection. Polygons loaded from PyArchInit change in `pyunitastratigrafiche` only with *Salva modifiche in PyArchInit* (Layer pane), which asks before deleting | `pa.update_unit(gid, geometry=..., crs=...)`, `pa.delete_unit(gid)` |
| Spot heights | *Quota* in the drawing bar, separate from SAM: a click reads the elevation on the DEM at full resolution, then the fields of `pyarchinit_quote` (area, US, unit type, elevation (editable), unit of measure, draughtsman, date, original survey); *Non chiedere piu'* keeps them for the next clicks (choose the tool again to change them). The points go in the *Quote* layer, are saved in the project and are written with *Pubblica* | `pa.publish_quote(points, sito, crs)`, `pa.publish_elevations(chunk, sito, area, us, points)` |
| Segment Anything 2.1 | installed on request in its own environment, with the PyTorch build for the graphics card (CUDA on NVIDIA cards, picked by the installer on Windows and Linux; Apple's GPU through MPS on the Mac, about 3.5 times faster than the CPU on an M4 Pro); the window at the end of the installation says which one is used. *SAM* in the drawing bar, three ways chosen from its menu: **Tocco** (click an object: SAM looks at the part of the orthomosaic on screen, so zoom in for details and out for large units), **Riquadro** (drag a box around one object), **Area** (drag a box: every unit inside). The mask appears dashed with the unit form: *Salva*, *Annulla*, or *Correggi* (Shift+click adds a part, right click removes one, Enter saves, Esc discards). *Tutta l'ortofoto* scans the whole orthomosaic in overlapping 1024-pixel tiles at a chosen resolution and thoroughness (normale / alta / massima: more points and crops per tile): objects cut by the tiles are joined, duplicates dropped, and the units do not overlap (smaller ones are cut out of larger ones). Automatic units go to the layer *Unita' da SAM* without a US number. The menu also chooses the model (tiny to large). *Prompt sulle foto* segments an object on a photo | `ai.sam.segment_orthomosaic(chunk, segmenter, points=, labels=, box=, bounds=)`, `auto_units(chunk, segmenter, bounds=, resolution=, thoroughness=)`, `read_orthomosaic`, `segment_photo_and_classify(...)` |
| Classification | *Strumenti > Classificazione della nuvola*: automatic rules (ground, vegetation, masonry/structures, noise; ASPRS classes plus archaeological classes 64-71), written in the dense cloud (LAS export keeps the classes). The 3D view colours the cloud by *Classi* (or *Quota*, or the photos' colours) with a legend: untick a class to hide it. *Lazo* selects points on the screen (click the vertices or drag, double click or Enter to close) and gives them a class (a new code can be given a name, e.g. 120 *Crollo*, shown in the legend); only the points visible at the front are taken unless asked, and hidden classes are never taken. Points corrected by hand are remembered: the automatic rules and the classifier leave them as they are. *Addestra il classificatore* learns the classes from those points (or from every classified point): the geometry around each point at three scales, the height above the ground and the colour, with a random forest (or gradient boosting for a smaller file); it reports the accuracy per class and classifies the rest of the cloud. The model is kept in the project; *Salva / Carica un classificatore* (`.ppgclf`) reuses it on other excavations with a similar point spacing. *Classifica con il classificatore* takes a minimum confidence (below it points stay unclassified); *Dimentica le correzioni a mano* clears the flags, classes unchanged. *Classi dalle US (poligoni)* gives the points inside each drawn or loaded unit (plan view) the class of that unit (`US 12 (area 1)`, codes from 100, kept when run again; hand corrections kept unless unticked) and shows points, plan area and elevations per US in a table (*Salva CSV...*). **The classes drive the products**: the DEM dialog lists the classes of the dense cloud with their points (only *Terreno* gives a terrain model, DTM, with the gaps under what is left out interpolated; contours and volumes then use it); the orthophoto dialog makes chosen classes transparent where they are the visible surface (tools, people, vegetation); the model dialog meshes only the ticked classes; *File > Esporta > Nuvola di punti* exports only some classes; the menu of the *Sezione* tool (*Opzioni delle sezioni*) cuts the model or the dense cloud and keeps only the ticked classes (from the model, the stretches lying on points of those classes); *Pianta a quota* has the same choice. Noise (classes 7 and 18) never makes a DEM or a mesh | `ppg classify scavo.ppg`, `ppg classify-train scavo.ppg modello.ppgclf`, `ppg classify-apply altro.ppg modello.ppgclf`, `ppg classify-units scavo.ppg --csv us.csv`, `ppg dem scavo.ppg --classes 2`, `ppg ortho --hide-classes 3,71`, `ppg mesh --classes 2,64`, `ppg section ... --classes 2`, `ppg export --cloud muri.las --classes 64`; `ai.classify.classify_chunk(chunk)`, `set_class(chunk, mask, code)`, `classify_by_units(chunk, units)`, `class_statistics`; `ai.learn.train_on_chunk(chunk)`, `classify_chunk_with_model(chunk, model)` |
| Sections | *Traccia sezione* in the drawing toolbar, then two clicks on the orthomosaic: the model is cut along the trace, the drawing appears in the *Sezioni* pane (SVG, DXF profile and 3D DXF in `sezioni/`), the trace becomes a feature of the *Sezioni* layer. The SVG is at 1:20, or at the most detailed standard scale (1:50, 1:100, 1:200...) at which it fits about an A0 sheet (a 174 m section across a tell: 1:200); the profile line is 0.5 mm and the DXF grid spacing suits the height of the section (10 cm in a trench, 1-2 m on a tell). *Pianta a quota* cuts the model horizontally into its own layer | `ppg section scavo.ppg x1 y1 x2 y2 --out sezione.svg`; `archaeo.sections.section(chunk, p1, p2)`, `sections_along(chunk, baseline, interval, half_width)`, `plan_slice(chunk, elevation)`; `pa.publish_section_trace` |
| Contours and volumes | *Curve di livello* (DXF/GeoJSON/GeoPackage) draws the lines as a queryable layer (elevation, index line) that survives switching between DEM and orthomosaic. The dialog shows the elevation range of the DEM and proposes a round interval giving at most about 50 levels (5 cm on a trench, 50 cm on a tell 20 m high), asks before an interval that would give more than 300 levels, and offers a *Levigatura* (Gaussian smoothing of the DEM, in metres) for drone DSMs; lines keep their vertices within half a pixel and closed rings smaller than 25 pixels (noise) are left out, so a tell gives thousands of lines instead of 158,000, added to the view in one go; *Volume scavo / riporto*: the reference is the surface before the work (a plane at the original ground level or the DEM before excavation): *scavo* = volume removed (now below the reference), *riporto* = volume added; with "Quota minima" the deposit above the lowest point. Partial cells along the polygon edge are weighted by their covered fraction | `ppg contours`; `archaeo.terrain.contours`, `volume`, `hillshade`, `slope_degrees`; `pa.publish_volume` |
| Pottery profiles | *Strumenti > Profilo ceramico da mesh*: choose the mesh of a vessel or sherd (OBJ/PLY/STL), the scale and the units; the rotation axis is estimated from the surface, the profile with rim/base diameters, height and wall thickness is drawn as SVG/DXF | `ppg pottery vaso.ply --out profilo.svg --scale 1`; `archaeo.pottery.estimate_axis`, `best_profile`, `draw` |
| US forms | double click a US in the PyArchInit pane (or *Scheda US* in the Layer and Proxy panes) opens the PyArchInit US form (`us_table`): description, chronology, excavation data, characters, elevations and measures, and the stratigraphic relationships as an editable table (*Copre*, *Taglia*, *Si appoggia a*...), stored in PyArchInit's own format so its Harris matrix reads them. *Nuova US* / *PyArchInit > Nuova scheda US* creates a record (the site record too when missing); an existing site/area/US/type is refused. A database with a reduced `us_table` (e.g. the demo database of version 0.2, or a file created by other tools) shows a warning in the form with *Aggiorna struttura del database* (also in the PyArchInit menu): the missing PyArchInit 5 columns are added after a backup copy of the file, records untouched | `pa.missing_us_columns()`, `pa.upgrade_us_table()`, `pa.us_record`, `pa.create_us(sito, area, us, tipo, **fields)`, `pa.update_us(id_us, **fields)`, `parse_relationships`, `format_relationships` |
| US proxies | *Strumenti > Proxy 3D delle US*: each unit polygon (drawn, or from a PyArchInit site) becomes a closed solid between the project DEM (top) and a base: a plane at a given elevation, the DEM recorded after the unit was removed, a constant thickness or the lowest point. Its volume equals the cut/fill volume on the same cells. The *Proxy US* pane lists volume, plan area and elevations; *Vedi in 3D* shows them coloured over the model; *Esporta (Extended Matrix)* writes one OBJ with an object per unit (`US12`, `USM3`) plus the coordinate offset; *Pubblica in PyArchInit* registers each proxy as a 3D media linked to its US; *Quote nella scheda US* writes the min/max elevations into the form | `archaeo.proxies.unit_proxy`, `proxies_for_units`, `export_extended_matrix`, `save_proxy` |
| BIM / IFC | *Strumenti > Esporta IFC (BIM)*: choose the units (drawn here, the US polygons of a PyArchInit site, or the *proxy solids*) and whether to include the whole survey; the export shows its progress and, at the end, a window with the elements, their attributes (`Pset_PyArchInit_US`) and a 3D view of what was written. IFC4 in metres with `IfcMapConversion`. With proxies the file carries quantities, materials, groups and relationships: see [bim.md](bim.md) | `ppg ifc scavo.ppg rilievo.ifc`; `bim.ifc.export_chunk_ifc(...)`, `export_proxies_ifc(...)`, `read_ifc_elements(path)` |
| Versions | the dialogs of *Nuvola densa*, *Modello*, *DEM* and *Ortofoto* ask, when the product exists, whether to keep the current version (to compare it) or overwrite it, and the name of the new one; a failed build puts the kept one back. The workspace shows the name of the active version next to each product and lists the kept ones under it: double click shows a kept version without making it active (rasters in the orthomosaic view, clouds and models in the 3D view with a *Versione ... (non attiva)* label; choosing a view in the list goes back to the active products). Right click on a kept version: *Rendi attiva* (the one every tool uses; the active one takes its place among the kept ones), *Differenza con il DEM attivo* (active minus kept, red where the active one is higher, blue where lower, dZ under the cursor; a warning when the two DEMs have different resolutions), *Esporta*, *Rinomina*, *Elimina*. Right click on a product: *Conserva una copia come versione* (e.g. before classifying the cloud again), *Rinomina*. Kept versions live in `versions/` inside the chunk folder | `core.versions.keep_active`, `activate`, `versions`, `version_file`, `dem_difference`, `rename`, `delete` |
| Confidence | *Flusso di lavoro > Pulizia e confidenza > Calcola la confidenza*: for the dense cloud and/or the vertices of the model, the number of depth maps that confirm each point (within 1 % of its distance from the camera, the tolerance of the fusion; half an edge for a vertex); the fused cloud already has it. The 3D view (*Colori > Confidenza*, also for the model) colours the ranges 0 (grey: no map, e.g. a hole the mesh closed), 1 (red), 2, 3-4, 5-9, 10-19, 20 or more (blue); unticking a row of the legend hides those points or faces, a preview of a cleaning. Needs the depth maps of the chunk (*Nuvola densa*, also *Solo le mappe di profondita'*) | `ppg confidence scavo.ppg --model`; `chunk.compute_confidence("point_cloud" or "model")`, `engine.confidence.depth_map_support` |
| Cleaning | *Pulisci la nuvola densa*: points below a confidence, isolated points (statistical filter: neighbours, standard deviations), points without neighbours within a radius, detached groups smaller than a number of points, classes, a colour (with a tolerance); the points found are counted and shown in red in the 3D view, then removed when confirmed. *Pulisci il modello*: detached pieces below a share of the largest one, faces with a low vertex confidence, stretched triangles, topology, holes closed up to a radius, Taubin smoothing (borders fixed), decimation; colours and confidence kept, the texture to build again. *Pulisci i punti di legame (selezione graduale)*: thresholds on reprojection error, number of photos, reconstruction uncertainty and projection accuracy with live counts, then the camera optimisation (for models written from imported poses only the uncertainty matters). The *Lazo* deletes or keeps points and faces. Every cleaning can keep the current version first | `ppg clean-cloud`, `ppg clean-model`, `ppg clean-tie-points`; `chunk.clean_point_cloud(...)`, `clean_model(...)`, `clean_tie_points(...)`, `engine.cleaning.point_filters`, `face_filters`, `delete_points`, `delete_faces` |
| Texture | *Costruisci texture*: *Mappatura* automatic, adaptive orthophoto (terrain from the air: the texture is the orthophoto of the model, made by the tiled orthophoto engine; walls and sections get their own cells in a band below) or generic (each face its own cell: objects and small models). The texture is an image beside `model.ply`, which carries the texture coordinates of each face; the view *Modello con texture* shows the whole model. *File > Importa > Modello e texture dal progetto Metashape* reads the model of the `.psx` again with its colours, confidence and texture (the model in use is kept as a version) | `chunk.build_texture(size, mapping="auto")`; `integrations.metashape.reimport_model(chunk)`; `io.mesh_ply.read_mesh`, `write_mesh` |
| Crop | *Ritaglio* in the drawing tools (or *File > Esporta > Ritagliati...*, *Pulizia e confidenza > Ritaglia*): drag a box or click a polygon (double click to close) on the orthophoto or the DEM; or select a polygon (a unit) and *Ritaglia o esporta con il poligono selezionato*. Then choose the products: exported cut to a folder (DEM and orthophoto with no data or transparent outside the polygon, model PLY with colours, confidence and double-precision coordinates or OBJ with the texture, cloud LAS with classes and confidence, the polygon as GeoJSON) or cut in the project with the originals kept as versions; model and cloud can also be limited in height | `ppg crop scavo.ppg --polygon "x1,y1 x2,y2 x3,y3" --out ritagli` (or `--bbox`, `--wkt`, `--geojson`, `--in-project`); `chunk.export_dem(path, region=)`, `export_orthomosaic`, `export_model`, `export_point_cloud`, `crop_dem`, `crop_orthomosaic`, `crop_model`, `crop_point_cloud(region=)` |
| QGIS | *QGIS > Apri in QGIS (collegamento dal vivo)*: see [integrations.md](integrations.md) | `io.qgis_link.export_layers`, `build_project` |
| Import | *File > Importa*: Metashape `.psx`, WebODM/ODM, Photo3D sessions: see [integrations.md](integrations.md) | `ppg import-metashape`, `ppg import-webodm`, `ppg import-photo3d`, `ppg photo3d-job` |

Everything the interface does is also available in the console (*Console Python*: `doc`, `chunk`,
`ppg`) and in scripts (`ppg run script.py --project scavo.ppg`). See [api.md](api.md) and
[pyarchinit.md](pyarchinit.md).
