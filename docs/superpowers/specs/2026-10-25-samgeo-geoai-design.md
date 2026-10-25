# Plugins samgeo and geoai: finding what is written, learning from the drawings

Date: 2026-10-25. Status: design approved by the author; samgeo first (milestones "1 - samgeo" and
"2 - geoai" of the plugin repository).

## What was asked

Two plugins of the plugin repository, from the roadmap ("ideas proposed but not decided yet"):

* **samgeo**: segmenting the orthomosaic by writing what to find (stones, walls), into units and
  layers of the project;
* **geoai**: training on one's own drawings, and comparing surveys of different dates.

Decisions of the author (2026-10-25):

1. What samgeo finds goes into a layer of **proposals**, checked on the view, and only then becomes
   units (US), point classes or a GeoPackage.
2. The **open engine** (Grounding DINO + SAM 2.1, open weights) is the default; **SAM 3** is used
   when the user has access to its weights on Hugging Face.
3. The classes geoai learns come from a **table**: the user picks the field of the units (the kind
   of unit, the stratigraphic definition, any attribute) and groups its values into classes.

## What the program has already

* SAM 2.1 (tap, box, area, stones, the whole orthomosaic in tiles) in an environment of its own
  (`ai/sam_env.py`: `uv`, Python 3.12, PyTorch chosen with `--torch-backend auto`), always in a
  worker process: PyTorch never runs beside pycolmap (two OpenMP runtimes abort the process).
* Units drawn (`archaeo.stratigraphy.UnitPolygon`) kept in `chunk.meta["drawings"]["units"]`, undone
  by snapshot (`UndoHistory.record` when the window is marked modified), in the live QGIS link, numbered
  by *Proponi US e rapporti* (`archaeo.us_proposal`, the units without a number), and turned into point
  classes by *Classi dalle US (poligoni)* (`ai.classify.classify_by_units`).
* The plugin API: panes, items of the workspace, rasters of the calculator, drawing lent
  (`request_polygon`), work in the background with the progress bar; plugins draw their own layers
  with `ortho_view.add_polygon`.

Missing: a plugin cannot make real units - with *Modifica > Annulla*, the live QGIS link, the US forms
of PyArchInit and the US proposal. Hence the first step, in the program.

## Program: `context.add_units` (since 1.5.0)

```python
context.add_units(polygons, tipo="US", attributes=None, chunk=None) -> int
```

* `polygons`: shapely (Multi)Polygons or `(N, 2)` vertex lists in the coordinates of the chunk; an
  invalid outline is made valid, an empty one or one that is not an area is left out.
* `attributes`: one dict for all, or a list with one dict per polygon: `us`, `area`, `tipo`, `sito`
  and the columns of `pyunitastratigrafiche` (`UNIT_FIELDS`); any other key stays an attribute of
  the unit (kept with the project, as a column in the live QGIS layer). The site is the project's
  when not given.
* The window, on the active chunk: the units are added to those drawn, drawn on the view, one entry
  of *Modifica > Annulla* for all of them, one line of the log, the hint about the US forms not yet
  in PyArchInit. On another chunk, and in the command line: into `chunk.meta["drawings"]`, where the
  window finds them when that chunk becomes the active one; the project is marked modified (the
  command line saves it). Only in the interface thread, as `store_drawings`.
* Returns how many units were added. A plugin that uses it asks for `min_app` 1.5.0.

## samgeo

### What it does

The *Samgeo* pane: a text - with ready entries in Italian (*pietre*, *muri*, *mattoni*, *tegole e
coppi*, *frammenti ceramici*, *ossa*, *alberi*, *cespugli*, *persone*...) given to the models in
English - a confidence threshold, the smallest and the largest area in square metres, and where to
look: the whole raster, a polygon drawn, or the part shown. The raster is the orthomosaic, or any
raster of the chunk (`context.rasters()`): a relief visualisation of the DEM for mounds and ditches,
experimentally. Every run is a layer of proposals on the 2D view (`samgeo: pietre`) with the score and
the area of each polygon, listed in the pane and in the workspace. From there:

* **US** (all, or those selected): `context.add_units`, kind US, the text and the score as
  attributes; *Proponi US e rapporti* then numbers them;
* **classes of the dense cloud**: the points inside the polygons get a class (`classify_by_units`);
* **GeoPackage**; **show**, **hide**, **remove**.

The mask of a run is a GeoTIFF in the chunk folder, which the raster calculator finds by itself.

### Engines

* **Open** (default): the recipe of samgeo's `LangSAM` - Grounding DINO finds the boxes of what is
  written, SAM 2.1 draws the mask in each box - with Grounding DINO from Hugging Face `transformers`
  (`IDEA-Research/grounding-dino-base`) instead of the `groundingdino-py` package, which compiles C++
  at installation (a compiler is rarely there on Windows and macOS). Apache-2.0 weights, no account.
* **SAM 3**: samgeo's `SamGeo3`, which finds every instance of a concept at once. The weights on
  Hugging Face (`facebook/sam3`) are subject to Meta's separate SAM License, not the plugin's GPL.
  Each user requests and accepts model access on Hugging Face, then uses their own Hugging Face read
  token. A token alone does not grant access; the pane explains the two steps and never stores the
  token in a project, settings or logs.

### Environment and worker

* One environment for both plugins, `<app data>/pyarchinit-photogrammetry/geoai-env` (one copy of
  PyTorch, about 2 GB): made with the program's `uv` (`ai.sam_env.find_uv`, `torch_backend_args`),
  Python 3.12; each plugin adds its packages when the user asks (samgeo: `torch`, `transformers`,
  `sam2`; SAM 3: `segment-geospatial[samgeo3]`; geoai: `geoai-py`, `torchange`). `ppg-env.json`
  records the packages and the device found (CUDA, Apple GPU, CPU).
* `worker.py` runs with the environment's Python, as `ai/sam_worker.py`: framed pickle requests on
  stdin/stdout - `init` (engine, models, device), `segment` (an RGB tile, the text, the thresholds:
  the instance masks with their scores and phrases), `quit`. `PPG_SAMGEO_FAKE=1` makes it a fake
  engine without PyTorch (bright blobs are the objects), for the tests.

### Pipeline (in the program's Python)

1. The window of the raster: the area asked, read at the resolution chosen (the raster's, or coarser:
   1 cm/px is plenty for stones); 3 or 4 bands give RGB, one band is stretched to grey (2-98 %).
2. Tiles of 1024 px with 128 px of overlap; each tile to the worker.
3. The masks into polygons in the coordinates of the chunk (`mask_to_polygons`), the polygons of
   neighbouring tiles that are the same object (overlapping by half of the smaller) merged, the score
   of the best kept; then the area and score filters.
4. `<chunk data>/samgeo/<run>.gpkg` (prompt, score, area_m2), `<run>_mask.tif` (the objects numbered,
   with its `ppg_name`), `chunk.meta["samgeo"]["runs"]` (text, engine, raster, area, parameters,
   count, files, time).

### Command line

`ppg plugin samgeo install [--sam3]`, `ppg plugin samgeo segment PROJECT TEXT [--chunk LABEL]
[--raster NAME] [--area X0,Y0,X1,Y1] [--resolution M] [--threshold T] [--min-area M2] [--max-area M2]
[--engine open|sam3] [--units]`, `ppg plugin samgeo list|export|remove ...`.

### Tests (without PyTorch, on the CI of the plugin repository)

The fake worker on a synthetic orthophoto with "stones" (bright discs): the polygons found, their
areas, the objects across the seams of the tiles merged once; the files and the record of a run; the
pane in the offscreen window; `add_units` when the program has it (skipped on older ones).

### Limits

* English texts work best; the Italian entries are translated by the pane.
* The open engine finds objects (stones, bricks, tiles, trees, people) far better than the contrast
  between two soils: that is for geoai, trained on the drawings.
* Without a GPU it runs on the processor, slowly (tens of seconds per tile).

## geoai (after samgeo)

### Learning from the drawings

* Labels: the units of the chunks chosen, with the class table; what counts as "nothing" is only
  inside a polygon of the trained area (by default the hull of the units drawn), so that ground left
  undrawn is not taught as background.
* Images: the orthomosaic, and, if asked, the DEM made local (minus its smoothed self), the slope and
  the sky-view factor as more bands, on the orthomosaic's grid.
* Tiles of 512 px with geoai's export of tiles from a raster and vector labels, split for training and
  checking; a U-Net (segmentation-models-pytorch, ImageNet ResNet-34 encoder, one input channel per
  band) trained with geoai; IoU per class at the end. The model is kept in the plugin's folder
  (`.ppgseg`: the weights and a JSON of classes, bands, resolution, normalisation) and used again in
  other projects, as the classifier of the dense cloud.

### Applying a model

geoai's segmentation of a GeoTIFF on a new orthomosaic (with the same bands): the raster of the
classes (found by the calculator), its polygons per class (minimum area, simplified), a layer of
proposals; *US* gives each the kind of its class (USM for walls), and the US proposal numbers them.

### Change between two dates

Two chunks of the excavation phases, coregistered: their orthomosaics on one grid, geoai's
`ChangeDetection` (AnyChange, SAM, no training) gives the objects that changed with a score; each
polygon gets the mean height difference of the DEMs (removed, added) from the raster calculator; a
layer, a table, a GeoPackage.

### Limits

Training wants a GPU (NVIDIA or Apple); on the processor only small trials. AnyChange reads RGB, and
the two orthomosaics must lie on each other (the DEM coregistration of the phases).

## Phases

1. `context.add_units` in the program (1.5.0).
2. samgeo 0.1.0: environment, worker, open engine, pipeline, pane, command line, tests.
3. samgeo 0.2.0: SAM 3.
4. geoai 0.1.0: learning from the drawings and applying a model.
5. geoai 0.2.0: optional DEM, multispectral and thermal raster inputs, alone or combined with RGB;
   windowed alignment to a common reference grid, channel/unit schema and train-only normalisation.
6. geoai 0.3.0: change between two dates, after the optional-input workflow has been evaluated on a
   real annotated survey. Requires a registered pair of dates; DEM differences accompany proposed changes.

## Optional raster inputs (2026-10-25)

Approved by the author after the first release. RGB remains the default. The user may select an
ordered set of named raster sources and band numbers (DEM, multispectral, thermal, numeric derived
rasters), with resampling and physical units. The first source defines the reference grid in chunk
coordinates. Additional sources are read on that grid with WarpedVRT; unknown CRS is not guessed,
nodata/nonfinite pixels in any input are ignored, and GeoTIFF scales/offsets are applied. This aligns
grids, not misregistered surveys; sensor calibration and unit conversion remain explicit user work.

New numeric datasets/models use format 2 with per-channel mean/std calculated only from valid labelled
training pixels; saved statistics and the ordered semantic schema are required at application time.
Format 1 RGB models remain usable. Custom numeric channels use one U-Net input channel per band and
start without ImageNet weights. No complete resampled raster stack is written, to limit disk usage.
