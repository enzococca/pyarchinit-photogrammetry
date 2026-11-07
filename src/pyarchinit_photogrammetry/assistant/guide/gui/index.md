# Desktop interface

The desktop interface does with the mouse everything the [Python API](../api.md) does in scripts: every
command calls the same functions, so anything done in the window can be repeated in the console or in a
script. This page shows the window and how it is organised; the other pages of this section go into each
part.

<div class="grid cards" markdown>

- :material-menu: **[Menus](menus.md)** - every command, menu by menu
- :material-dock-left: **[Panes](panes.md)** - workspace, references, layers, photos, sections, log
- :material-rotate-3d-variant: **[3D view and trackball](view3d.md)** - turning, panning, zooming, the centre of rotation, keys
- :material-camera-marker: **[Photos and markers](photos-markers.md)** - photos, masks, markers, scale bars
- :material-cog-play: **[Processing](processing.md)** - from the alignment to the orthophoto, versions, cleaning
- :material-draw: **[Drawing and measures](drawing.md)** - units, sections, spot heights, measures, SAM
- :material-shovel: **[Tools for archaeology](archaeology.md)** - PyArchInit, QGIS, proxies, matrix, phases, pottery
- :material-leaf: **[Multispectral and thermal](multispectral.md)** - reflectance, indices, temperatures
- :material-export: **[Exports and reports](exports.md)** - PDF report, Excel tables, web formats, imports
- :material-triangle-outline: **[TIN and break lines](../tin.md)** - triangle network, break lines, contours, volumes
- :material-lifebuoy: **[Errors and updates](help.md)** - log, diagnosis, updates

</div>

## Starting the program

```bash
pip install "pyarchinit-photogrammetry[gui]"   # included in [all]
ppg-gui [project.ppg]                          # or: ppg gui, python -m pyarchinit_photogrammetry.gui
ppg-gui --doctor                               # environment diagnosis (same as ppg doctor)
ppg-gui --check                                # opens, renders and exits (smoke test)
ppg-gui --run script.py [arguments]            # a Python script with the program's modules, no window
```

Or install the packaged application for macOS and Windows: see [Packaging](../packaging.md).

At start a splash in the colours of the logo shows a progress bar: the percentage and what is loading (NumPy
and Open3D, PROJ and GDAL, COLMAP, the 3D view, the interface). The window appears in about a second; the log
records how long the start took.

Opening a project shows the same card over the window (project, workspace, 3D view, orthophoto and DEM). The
thumbnails of the photos come afterwards, in the background, from the small previews the cameras store in
the EXIF: hundreds of drone photos do not freeze the window, and the status bar counts them. A photo that
cannot be read (an empty file kept online only by Dropbox or iCloud) gets a dark thumbnail and a tooltip.

## The start page

![The start page](../img/gui/start-page.png)

Without a project the centre of the window shows the start page:

* **New project**, **Open project...** and **Demo project** (the [demo](#the-demo-project));
* the **recent projects**, a click to open one;
* *Guide* (this site), *Environment diagnosis*, *Log folder* and *Support the project*.

*File > Start page* brings it back.

## The window

![The areas of the window](../img/gui/window-areas.png)

| | Area | What it holds |
| --- | --- | --- |
| **1** | Menu bar | every command: see [Menus](menus.md) |
| **2** | Top bar | open, save, the *Workflow* menu of the steps, *Process everything*, PyArchInit and the search of the commands: see [below](#the-toolbars) |
| **3** | Tool rail | beside the orthophoto and the DEM, the tools that draw and measure, in groups that open a panel: see [below](#the-tool-rail-of-the-2d-view) |
| **4** | Panes on the left | *Workspace*, *Reference*, *Layer*: the project, its markers, what is drawn ([Panes](panes.md)) |
| **5** | Central views | *Model* (3D), *Orthophoto / DEM*, *Photos*: see [below](#the-central-views) |
| **6** | Panes at the bottom | *Photos* (thumbnails), *Sections*, *Python console*, *Log* |
| **7** | Panes on the right | *PyArchInit*, *US proxies*, *Multispectral*, *Thermal* and the panes of the plugins |
| **8** | Status bar | the progress of a step with its stop button; under the cursor coordinates, elevation, slope and the values of the rasters |

Every pane can be moved, floated or closed, and *View > Panes* brings it back: see
[Moving the panes](panes.md#moving-the-panes). The window fits a laptop screen, down to about 1000 x 650
pixels: buttons wrap onto more lines, a pane too short for its content scrolls, the top bar drops its texts first
when it does not fit (the tooltips keep the names).

## The toolbars

### The top bar

![The top bar](../img/gui/toolbar-workflow.png)

One slim bar above the views. While a step runs, the processing buttons are disabled.

| | Button | What it does |
| --- | --- | --- |
| ![](../img/gui/icons/apri.png){ width=20 } | Open | as *File > Open...* |
| ![](../img/gui/icons/salva.png){ width=20 } | Save | as *File > Save* |
| ![](../img/gui/icons/flusso-di-lavoro.png){ width=20 } | Workflow | a menu with the steps in order (below), each with the state of the active chunk |
| ![](../img/gui/icons/elabora-tutto.png){ width=20 } | Process everything | all the steps in sequence (++ctrl+r++) |
| ![](../img/gui/icons/pyarchinit.png){ width=20 } | PyArchInit | orthophoto, DEM and model in the media of PyArchInit |
| ![](../img/gui/icons/search.png){ width=20 } | Search a command | type a part of the name of any command of the menus and press ++enter++; ++ctrl+k++ (++cmd+k++ on macOS) puts the cursor there |

The **Workflow** menu lists the steps from the photos to the products, each with the state of the active chunk
in place of its icon: ![](../img/gui/icons/passo-fatto.png){ width=20 } done,
![](../img/gui/icons/passo-da-fare.png){ width=20 } still to do. A click runs the step, as the commands of the
menu *Workflow*:

| Step | What it does |
| --- | --- |
| 1. Add photos | photos into the active chunk |
| 2. Marker | finds the printed coded targets in the photos |
| 3. Align | positions and orientations of the cameras (SfM) |
| 4. Reference | the coordinates of the markers from a CSV (done when a marker has surveyed coordinates or the chunk is aligned in a coordinate system) |
| 5. Dense cloud | depth maps and dense cloud |
| 6. Model | the 3D mesh |
| 7. Texture | the texture of the model |
| 8. DEM | the digital elevation model |
| 9. Orthophoto | the orthophoto (true ortho) |

*Contour lines*, *Cut / fill volume* and *Plan slice* are in the group *Sections and analyses* of the rail of
the 2D view and in the menu *Tools*.

### The tool rail of the 2D view

![The tool rail with the group Drawing open](../img/gui/toolbar-drawing.png)

On the left of the orthophoto and the DEM a narrow column holds the drawing tools in groups. A click on a
group opens its panel beside the rail, over the view, with the tools as buttons with icon and name; a small
arrow beside a button opens its menu. The panel closes when you choose a tool (an option such as *Snap* leaves it open), click the same group again,
press ++esc++ or click the view; the pin in its header keeps it open while you work. The group of the tool in
use is highlighted. A right click on the rail chooses *Icons only* or *Icons and text* (the names of the groups
under their icons).

| | Group | Tools |
| --- | --- | --- |
| ![](../img/gui/icons/rail-navigate.png){ width=20 } | Navigate | *Pan / query*, *Select* (*Freehand selection* and *Identify and group units* in its menu) |
| ![](../img/gui/icons/rail-draw.png){ width=20 } | Drawing | *Draw US* (*Freehand* in its menu), *Vertices* (*Reshape* in its menu), *Reshape*, *Free drawing*, *Break lines* (the kind in its menu), *Snap* |
| ![](../img/gui/icons/rail-measure.png){ width=20 } | Measurements | *Measure distance*, *Area and 3D surface*, *Height* (*Measuring point* in its menu), *Marker* |
| ![](../img/gui/icons/rail-analysis.png){ width=20 } | Sections and analyses | *Section* (the options and the *Sections* pane in its menu), *Contour lines*, *Cut / fill volume*, *Plan slice* |
| ![](../img/gui/icons/rail-auto.png){ width=20 } | Automatic | *Segment Anything (SAM)* (its ways and its model in its menu), *Orthophoto patches* |
| ![](../img/gui/icons/rail-edit.png){ width=20 } | Edit | *Delete selected*, *Merge selected*, *Clear drawings* |
| ![](../img/gui/icons/rail-layers.png){ width=20 } | Layers and outputs | *Layer* (the *Sections* pane in its menu), *QGIS*, *Publish in PyArchInit* |
| ![](../img/gui/icons/rail-plugin.png){ width=20 } | Plugin | since 1.7.4, only with plugins installed: each plugin with its actions in a menu (the same as the menu *Plugin*) |

Each tool is described in [Drawing and measures](drawing.md#the-drawing-tools). *View > Bars > Classic
drawing bar* shows the old horizontal bar with the same tools under the top bar instead of the rail.

A plugin can add a group of its own at the end of the rail of the 2D or of the 3D view (with its own icon);
the plugins that do not are in the last group, *Plugin*, so every plugin is reachable from the rail too. With the
classic drawing bar the rail of the 2D view is hidden: the plugins are then in the menu *Plugin* (an action a plugin
gave only to its group of the 2D rail is not there).

## The central views

The three tabs in the centre show the same chunk in three ways; ++f2++, ++f3++ and ++f4++ switch between
them (in the workspace ++f2++ renames instead).

=== "Model"

    <div class="grid" markdown>

    ![The textured model in the 3D view](../img/gui/view-model.png)

    ![The dense cloud in the 3D view](../img/gui/view-cloud.png)

    </div>

    Tie points, dense cloud, shaded or textured model, the TIN, cameras and markers. *Show* chooses what; the
    left drag turns the scene with the trackball, right or middle drag pans, the wheel zooms toward the cursor, a
    double click sets the centre of rotation: see [3D view and trackball](view3d.md).

    Above the view one row chooses what is shown and how (*Show*, the colours, the size of the points, *Cameras*,
    *Marker*, *Region*); the tools that work in 3D are in the rail on its left, in four groups that open a panel
    as in the 2D view: *View* (*From above*, *Home view*, *Centre the view on the point*, *Trackball*,
    [*Look through a photo*](view3d.md#looking-through-a-photo), *Vertical exaggeration*), *Points* (*Marker*, *Photos of the point*, *Lasso*), *Cuts and elevations* (*Section*, *Plan
    slice*, *Cut*, *Elevation*) and *Draw*:

    * **Marker**: click a point of the model or the cloud to add a marker there;
    * **Photos of the point**: the photos that see the point clicked;
    * **Section** (two points of the model or the cloud) and **Plan slice** (a point: the plan at its
      height);
    * **Cut**: the model cut with a plane ([cutting the model](processing.md#cutting-the-model));
    * **Elevation**: an orthophoto on the plane of the view as it is now, or on the plane of three points
      clicked; an elevation or a section ([elevations](processing.md#elevations));
    * **Vertical exaggeration** (x0.1 to x50): the heights multiplied around the centre of the scene, so that
      the relief of almost flat ground can be read. Only the view changes: markers, sections and cuts still
      take the true coordinates;
    * **From above** and **Home view**.

    The view uses VTK (pyvista) when OpenGL is available, otherwise a software renderer: `PPG_RENDERER=software`
    forces it, `PPG_RENDERER=vtk` forces VTK.

=== "Orthophoto / DEM"

    ![The orthophoto with the units](../img/gui/view-ortho-units.png)

    The orthophoto or the DEM (hypsometric tints over a hillshade) in world coordinates, with every layer drawn
    over them: units, sections, spot heights, markers. Under the cursor the status bar reads the products at
    full resolution: X, Y, Z, the slope and the aspect, the height above the ground when a DTM is kept, the
    temperature, the bands and the indices.

    A large raster opens from a reduced copy (at most 6,000 pixels a side) and, when the zoom stops beyond
    that resolution, the visible area (with a margin) is read again at the resolution of the screen, up to
    the full one, in the background. The detail of the DEM keeps the colours of the whole DEM. Tell Abu
    adh-Dhuhur (44,952 px): 0.7 s to open, 10.8 cm/px before the detail, 1.38 cm/px after 0.37 s.

=== "Photos"

    ![A photo with its markers](../img/gui/view-photo.png)

    One photo with its markers, as in Metashape: green flags are measured, blue flags are estimated and to be
    confirmed, red crosses are where the survey puts a marker not placed in that photo. Drag a flag onto the
    point, double click a blue flag to confirm it; ++page-down++ and ++page-up++ (or `]` and `[`) open the next
    and the previous photo of the list shown. See [Markers](photos-markers.md#markers).

## Undo and redo

*Edit > Undo* (++ctrl+z++) and *Redo* (++ctrl+y++, or ++ctrl+shift+z++) work on the drawings, the markers,
the scale bars, the patches of the orthophoto, the reconstruction region, the edits of the products in place
(classes given by hand, points deleted with the lasso, cleanings, faces of the model, texture patches) and
what the workspace removes. The menu names what will be undone.

* Before a product is edited in place its files are copied aside (on macOS a clone: instant and without
  space), so undo puts them back exactly.
* What comes back from QGIS through the live link is one entry per save made there (*From QGIS: ...*), and
  the proxies of the units follow their polygons when it is undone.
* Processing steps (alignment, dense cloud...) are not undone: use the [versions](processing.md#versions).
* The data a plugin keeps in the chunk (when the plugin says the project changed) is undone too, as "Plugin data".
* *All* / *None* in the Layer pane are one step for all the layers they show or hide.
* The history starts again when another project is opened; when another chunk becomes the active one it
  keeps only the removals (the other edits belong to their chunk). The copies kept for undo (in the `undo` folder
  of each chunk) are deleted when the program closes.

## Language and theme

The interface speaks Italian, like PyArchInit, or English: *View > Lingua / Language* chooses; the change
takes effect at the next start, and *Restart now* restarts at once with the open project. Without a choice
it follows the language of the system; `PPG_LANG=en` or `PPG_LANG=it` forces one for a session.

Menus, dialogs, panes, messages, the log and the progress of the engine are translated, and numbers are
written the English way. What is data keeps its Italian words: the values PyArchInit reads back (US types,
relationships), the names of the layers and of the versions of the products, the drawings and their files.

*View > Theme* switches between the light and the dark theme (remembered).

## The demo project

**File > Create and open demo project** (or `ppg demo <folder> --process`) writes:

* a synthetic **trench** of 4 x 3 m with a wall stub, 28 photos and 8 ArUco targets at EPSG:3004
  coordinates;
* a **bowl** and a **sherd** as meshes, and printable targets;
* a **PyArchInit database** with the full PyArchInit 5 schema and the site *Saggio dimostrativo* (US 1-3,
  polygons, spot heights, a section trace, pottery and finds), plus a Roman villa and a necropolis with tens
  of units;

then opens the project and connects the database. The screenshots of this guide come from it.

The **pottery demo** (`ceramica/ceramica.ppg`, 32 photos of the bowl on a turntable mat with 12 ArUco
targets in a local metric frame) shows the close-range workflow: *Process everything* without DEM and
orthophoto, then *Tools > Pottery profile from a mesh* on the exported model; with `--process` the demo
already contains `ceramica/profilo.svg` (expected rim 16 cm, height 12 cm, wall 6 mm). The demo window lets
you choose which project to open at the end.

## Console and scripts

Everything the interface does is also available in the *Python console* pane (`doc`, `chunk`, `app`, `ppg`,
`np`, `window`) and in scripts (`ppg run script.py --project scavo.ppg`, *Tools > Run Python script...*, or the
console's *Run .py file...*, see [Python console](panes.md#python-console)).
See the [Python API](../api.md) and [PyArchInit](../pyarchinit.md).
