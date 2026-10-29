# Scripts

Everything the interface does is a call of the Python API ([Python API](api.md)), so what you do in the window can be
repeated, in a loop or on many chunks, in a script. A script runs in three places:

* the **Python console** of the window, a pane at the bottom brought to the front from the *View* menu or with
  ++ctrl+shift+p++ ([Panes](gui/panes.md#python-console)): one line at a time; a `for` or a `def` goes on over the
  following lines, after the `...` prompt, and ends with an empty line;
* **Tools > Run Python script...** ([Menus](gui/menus.md#tools)): the file is read and run with the same names as the
  console, and the console shows what it prints;
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
| `window` | the main window: `window.mark_modified()`, `window.refresh_all()`, `window.selected_polygon()` (from 1.7.0) |

With `ppg run` there is no window: only the project, through `ppg.app.document`.

```python
import pyarchinit_photogrammetry as ppg

doc = ppg.app.document
chunk = doc.chunk
print(chunk.label, len(chunk.cameras), "photos")
```

!!! note "Coming in 1.7.0"
    The script run by *Run Python script...* will work in the background, so that the window stays alive, and
    *Edit > Undo* will take it back as one step. Its names will then include `polygon` (the area selected in the 2D
    view), `cameras` (the photos selected in the *Photos* pane) and `progress` (to report how far a long script has
    got). The console will run a block of several lines pasted at once. Both will catch `SystemExit` as well: a script
    written for `ppg run` that stops with `raise SystemExit(...)` will end the script, not the program.

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
functions the program has from 1.7.0 on.

!!! note "Coming in 1.7.0"
    `chunk.region_from_markers(...)` and `chunk.build_normal_map(...)` become functions of the program (the first two
    examples), `doc.duplicate_chunk(...)` and `chunk.set_cameras_enabled(...)` are the functions of the third, and
    `window.selected_polygon()` becomes the public name of the selection of the 2D view (in a script run with
    *Run Python script...* the same area is the name `polygon`). The scripts are written as they will be: they do
    not run in the versions before 1.7.0.

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
