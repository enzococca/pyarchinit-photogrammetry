# Scripts

Everything the interface does is a call of the Python API ([Python API](api.md)), so what you do in the window can be
repeated, in a loop or on many chunks, in a script. A script runs in three places:

* the **Python console** of the window, a pane at the bottom brought to the front from the *View* menu or with
  ++ctrl+shift+p++ ([Panes](gui/panes.md#python-console)): one line at a time; a `for` or a `def` goes on over the
  following lines, after the `...` prompt, and ends with an empty line;
* **Tools > Run Python script...** ([Menus](gui/menus.md#tools)): the file is read and run in the background, the
  window stays alive and *Edit > Undo* takes the whole script back in one step; the console shows what it prints;
* the command line, `ppg run script.py [--project project.ppg] [arguments]`, with no window.

## The names a script finds

In the console and in **Run Python script...** these names are ready, bound to the project that is open:

| Name | What it is |
| --- | --- |
| `doc` | the project (`ppg.Document`): `doc.chunks`, `doc.chunk`, `doc.save()` |
| `chunk` | the active chunk, `doc.chunk` |
| `app` | the application state (`ppg.app`): `app.document` is the open project, the same as `doc` |
| `ppg` | the package, `import pyarchinit_photogrammetry as ppg` already done |
| `np` | NumPy |
| `window` | the main window: `window.mark_modified()`, `window.refresh_all()`, `window.selected_polygon()`. In **Run Python script...** the script runs in the background and `window` hands every attribute and method of the window to the window's own thread, waiting for the answer, so scripts written for the window run unchanged; the supported names are `window.show_raster("dem")`, `window.show_model("model")`, `window.log("text")` and `window.refresh()` (done without waiting), `window.selected_polygon()` (the selection when the script started) and `window.mark_modified()` (nothing to do: the script's entry of *Edit > Undo* marks the project) |

**Run Python script...** (from 1.7.0) adds these, since the script runs in the background, away from the window:

| Name | What it is |
| --- | --- |
| `polygon` | the polygons selected in the 2D view when the script started, merged (a shapely geometry), or `None` |
| `cameras` | the photos selected in the *Photos* pane when the script started |
| `progress` | the reporter of the step: `progress(0.5, "half way")` moves the bar of the progress window |
| `cancel` | `cancel()` stops the script when *Cancel* was pressed in the progress window (else it returns `False`): call it in long loops |

What the script prints goes to the console and, line by line, to the details of the progress window. An error stops
the script and its traceback goes to the console, after what it printed; `exit()` or `raise SystemExit(...)` ends the
script, never the program, and so does it in the console. A `KeyboardInterrupt` raised by the script stops it as
*Cancel* does. While a script runs, another step does not start (the status bar says so). Only what the script itself
prints is captured: what the window prints meanwhile goes where it always goes.

With `ppg run` there is no window: only the project, through `ppg.app.document`.

```python
import pyarchinit_photogrammetry as ppg

doc = ppg.app.document
chunk = doc.chunk
print(chunk.label, len(chunk.cameras), "photos")
```

## What *Edit > Undo* takes back of a script

A script run by **Run Python script...** is one entry of *Edit > Undo*, named after its file, and *Edit > Redo* does
it again. It is one entry also when the script failed halfway: what it had done before the error goes back as well.
It takes back:

* every chunk as it was: cameras and their poses, sensors, markers, scale bars, the region, the settings and the
  drawings kept in the chunk, and the project's own settings (the active chunk among them);
* the products the script built again - dense cloud, model and its texture, DEM, orthomosaic, tie points, normal
  map and the alignment's COLMAP model: before the script writes one, the product as it was is kept (as a version for
  the first four, as a copy for the others), and undo makes it the active one again, the very same files. The version
  made this way is listed among the versions while the entry can be undone, and deleted when the entry leaves the
  history - unless the project was saved after the script: then it stays, as the saved project lists it;
* the versions the script made active or deleted: their files stay until the entry leaves the history;
* the chunks the script added (out of the project, with their folder) and those it removed (back in their place, with
  their folder);
* the files the script wrote inside the project's folder (`<project>.ppg.files`), which did not exist before.

It does not take back what the script did outside the project: records written to PyArchInit, the drawings sent to
QGIS's live GeoPackage, files exported elsewhere. When the script made such calls, the window lists them after it
("Not undoable: ..."). Files that were there before and that the script wrote over, other than the products listed
above (relief rasters, precision and coverage maps, multispectral and thermal rasters, files of the processing
folders), stay as the script left them; so do files outside the project's folder. A script that opens another
project cannot be undone: the window shows the project it opened.

Undo takes the script back only from the state it left: when a step that is not in *Edit > Undo* - a product built
from the *Workflow* menu, say - changed the project after the script, undoing it would take back half of it. The
entry is then removed from the history instead, and the status bar says so.

## The active chunk and its products

```python
chunk = doc.chunk                 # the active chunk; doc.chunk = doc.chunks[1] changes it
chunk.crs                         # the coordinate system of the chunk
chunk.product_path("dem")         # a Path, or None if the product does not exist
chunk.dem_path                    # the same as chunk.product_path("dem")
chunk.orthomosaic_path            # the same as chunk.product_path("orthomosaic")
chunk.data_dir                    # the folder of the chunk inside <project>.ppg.files/
```

The names of `product_path` are those of the work area: `point_cloud`, `model`, `dem`, `orthomosaic`, `tie_points`, and
so on (`chunk.products` lists the ones the chunk has). A product is a file of the chunk folder; read it with the library
you like (rasterio for the GeoTIFF of the DEM and the orthomosaic):

```python
import rasterio

with rasterio.open(chunk.dem_path) as dem:
    z = dem.read(1, masked=True)
    print("DEM %d x %d cells, %.3f m per cell" % (dem.width, dem.height, dem.res[0]))
    print("lowest %.2f m, highest %.2f m" % (z.min(), z.max()))
```

## Markers, photos and the region

```python
for m in chunk.markers:                       # markers, with the label shown in the interface
    print(m.label, m.position)                # position: the point in the internal frame, None if not placed

m = chunk.marker("target 1")                  # by label, key or target code
world = chunk.to_world(np.array([m.position]))   # internal frame -> coordinates of the CRS of the chunk

for c in chunk.cameras:                       # all the photos of the chunk
    print(c.label, c.aligned, c.enabled)
len(chunk.aligned_cameras)                    # the photos that are enabled and aligned: the ones the steps use

chunk.region                                  # the reconstruction region (engine.region.Region) or None
chunk.reset_region()                          # around the tie points
```

A script that changes what is in the project (markers, the region, `chunk.meta`) tells the window with
`window.mark_modified()`: the title shows the unsaved changes, *Save* saves them and *Edit > Undo* can take back what the
history follows (markers, scale bars, the region, drawings). Files written by a script, and the *enabled* flag of the
photos, are not followed by the history.

## Building a product again

The steps are methods of the chunk, with the parameters of the dialogs ([Python API](api.md) has the whole list):

```python
chunk.build_dem(resolution=0.02, source="point_cloud", interpolate=True)
chunk.build_orthomosaic(resolution=0.01, surface="dem", blending="mosaic")
```

`build_dem(resolution, source, interpolate, classes)` writes the DEM, with `classes=[2]` to use only the ground points.
`build_orthomosaic(resolution, surface, blending, hide_classes, workers, max_angle, colour_correction, ghosting,
fill_holes)` writes the orthomosaic on the DEM (`surface="dem"`) or on the model. Only the photos that are enabled and
aligned are used. In the window the same calls are in the *Workflow* menu.

## Examples

The three scripts below are written for the Python console, where `window` is the window itself. They use the
functions the program has from 1.7.0 on: `chunk.region_from_markers(...)` and `chunk.build_normal_map(...)` (the
first two examples, one call each; `ppg region-markers` and `ppg normal-map` on the command line, *Tools > Region on
markers...* and *Tools > Normal map...* in the window), then `doc.duplicate_chunk(...)`, `chunk.set_cameras_enabled(...)`
and `window.selected_polygon()`, the public name of the selection of the 2D view (in a script run with *Run Python
script...* the same area is the name `polygon`) in the third one (`ppg duplicate-chunk` and `ppg photos disable` on the
command line; in the window *Duplicate chunk...* on the right click of a chunk in the Workspace and *Disable the N
selected photos* on the right click in the Photos pane).

### The region on the plane of three markers

For an object or a wall, the region of the reconstruction is a thin box lying on the plane through three markers: depth
maps, dense cloud and model are made only inside it. The plane of the markers is the plane of the box; its size in the
plane stays the one of the previous region (or the extent of the tie points), and you choose the thickness.

```python
a, b, c = "target 1", "target 2", "target 3"          # the labels of three markers with a position

region = chunk.region_from_markers(a, b, c, thickness=0.10)   # 5 cm of region on each side of the plane
window.mark_modified()
print("Region %.3f x %.3f x %.3f m" % tuple(region.size))
```

The box axes are the two directions of the plane and its normal, pointing to the photos; `thickness` is the size along
the normal, centred on the plane. With `extent="tie_points"` the size in the plane comes from the tie points even if a
region exists.

### The normal map on the grid of the orthophoto

A normal map on the same pixels and coordinates as the orthophoto: red is the east component of the surface normal, green
the north one, blue the up one, as in the normal maps of Metashape. It is useful to light a drawing from another side, or
to put the relief under a drawing in QGIS.

```python
path = chunk.build_normal_map(source="auto")          # "model", "dem" or "auto" (the model if there is one)
print("Normal map:", path)

import rasterio
with rasterio.open(chunk.orthomosaic_path) as ortho, rasterio.open(path) as normals:
    assert (ortho.width, ortho.height, ortho.transform, ortho.crs) == (
        normals.width, normals.height, normals.transform, normals.crs)
```

The file is a GeoTIFF with four bands (RGB and the alpha: transparent where there is no surface), saved in the folder of
the chunk as the product `normal_map`.

### Only the photos of an area

To remake the orthophoto of a part of the site you do not need every photo. Select the area on the 2D view with the
*Select* tool, then keep only the photos that see it: either a new chunk with those photos (the original stays as it is), or
the others switched off in the active chunk.

```python
from pyarchinit_photogrammetry.engine.patches import photos_for_area

area = window.selected_polygon()                      # the polygon selected on the 2D view, or None
if area is None:
    print("Select a polygon on the 2D view first.")
else:
    if area.geom_type == "MultiPolygon":              # several polygons selected: the largest
        area = max(area.geoms, key=lambda g: g.area)
    ring = np.asarray(area.exterior.coords)           # the outline (holes are not considered)
    rows = photos_for_area(chunk, ring, limit=100_000)   # the photos, with how much of the area each covers
    keys = {r["key"] for r in rows if r.get("coverage", 0) > 0}
    useful = [c for c in chunk.cameras if c.key in keys]
    print(len(useful), "photos out of", len(chunk.cameras), "see the area")

    NEW_CHUNK = True
    if NEW_CHUNK:
        new = doc.duplicate_chunk(chunk, cameras=useful, label=chunk.label + " - area")
        print("New chunk:", new.label, "with", len(new.cameras), "photos")
    else:
        others = [c for c in chunk.cameras if c.key not in keys]
        n = chunk.set_cameras_enabled(others, False)   # the photos are kept, the steps skip them
        print("Switched off:", n)
    window.mark_modified()
```

With the photos switched off, `chunk.build_orthomosaic(...)` uses only the others; to use them again,
`chunk.set_cameras_enabled(chunk.cameras, True)`. The new chunk carries the photos with their poses and the markers they
see, so depth maps and the following steps can run on it.
