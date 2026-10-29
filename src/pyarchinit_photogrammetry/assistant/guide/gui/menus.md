# Menus

Every command of the program is in the menus at the top; the [toolbars](index.md#the-toolbars) and the
[panes](panes.md) repeat the ones used most. This page describes the menus one by one, in the order they
appear.

| Menu | What it is for |
| --- | --- |
| [File](#file) | projects, exports and imports, reports |
| [Edit](#edit) | undo and redo |
| [Workflow](#workflow) | from the photos to the products, one step after the other |
| [Tools](#tools) | drawing, measures, archaeology, multispectral, thermal, satellite, plugins |
| [PyArchInit](#pyarchinit) | the database of the excavation |
| [QGIS](#qgis) | the live link with QGIS |
| [View](#view) | panes, views, theme and language |
| [Help](#help) | guide, diagnosis, updates |
| [Plugins](#plugins) | the commands of the plugins installed |

!!! tip "Shortcuts on the Mac"
    The shortcuts are written with ++ctrl++: on the Mac use ++cmd++ (++cmd+s++ saves, ++cmd+z++ undoes).
    Commands with dots (*Open...*) first open a window with the choices; the others act at once.

## File

![The File menu](../img/gui/menu-file.png)

| Item | What it is for |
| --- | --- |
| **New project** ++ctrl+n++ | an empty project with one chunk |
| **Open...** ++ctrl+o++ | opens a `.ppg` project |
| **Recent projects** | the projects opened last |
| **Save** ++ctrl+s++ · **Save as...** ++ctrl+shift+s++ | saves the project; after every processing step the program saves it by itself, once it has a file |
| **Export** | the products in other formats: [see below](#file-export) |
| **Import** | projects and data of other programs: [see below](#file-import) |
| **Generate HTML report...** | the processing report as a web page (the same content as the [PDF report](exports.md#pdf-report)) |
| **Create and open demo project...** | a synthetic trench with photos, targets and a PyArchInit database, to try the program ([Demo](index.md#the-demo-project)) |
| **Start page** | back to the [start page](index.md#the-start-page) |
| **Quit** | closes the program (asks to save when needed) |

### File > Export

![The Export submenu](../img/gui/menu-file-export.png)

| Item | What it writes |
| --- | --- |
| **Point cloud (LAS/COPC/PLY)...** | the dense cloud; LAS keeps classes and confidence, COPC (`.copc.laz`) opens on the web; some classes only can be exported |
| **Model (OBJ/PLY/GLB)...** | the model with its colours and texture |
| **Model as Cesium 3D Tiles...** | a folder with `tileset.json` to open in CesiumJS |
| **Model for ArcGIS and Cesium (I3S, .slpk)...** | an I3S package with levels of detail, for ArcGIS Pro and Online, CesiumJS and deck.gl |
| **Light model with a normal map (GLB/OBJ)...** | few faces with a colour texture and a normal map: for the web, Sketchfab and game engines |
| **Fly-through video (MP4)...** | a camera orbiting the model or the cloud, up to 4K |
| **DEM (GeoTIFF/COG)...** · **Orthophoto (GeoTIFF/COG)...** | the rasters, also as Cloud Optimized GeoTIFF for the web |
| **Cameras (CSV / COLMAP)...** · **References (CSV)...** | positions and orientations of the cameras, markers with coordinates and errors |
| **Tables (Excel)...** | every table of the chunk in one Excel file, a sheet each ([details](exports.md#excel-tables)) |
| **PDF report...** | the processing report to print or hand over ([details](exports.md#pdf-report)) |
| **QField package (tablet in the trench)...** | orthophoto, DEM, units and spot heights to draw on the tablet ([QField](archaeology.md#qfield-in-the-trench)) |
| **Cropped by a polygon or a box...** | DEM, orthophoto, model and cloud cut to an area ([Crop](processing.md#crop)) |

!!! note
    Every export of the cloud, the model, the DEM or the orthophoto writes a `<file>.prov.jsonld` beside the
    file with its history (the [paradata](exports.md#paradata)). Large exports run in the background: the
    bar at the bottom shows how far they are.

### File > Import

![The Import submenu](../img/gui/menu-file-import.png)

| Item | What it reads |
| --- | --- |
| **Laser scans (E57, PTX)...** | one or more scans as the dense cloud of a new chunk |
| **3D model (OBJ, PLY, GLB)...** | a mesh made with another program as the model of a chunk; the previous one stays among the versions |
| **Metashape project (.psx)...** | cameras, markers, scale bars and products of Metashape ([integrations](../integrations.md)) |
| **Model and texture from the Metashape project (reimport)...** | reads the model of the `.psx` again with colours, confidence and texture |
| **WebODM / ODM project (folder or all.zip)...** | cameras, GCPs and products of WebODM |
| **Edits from QField...** | the units and spot heights drawn or corrected on the tablet come back into the chunk |
| **Photo3D session...** | a session of the Photo3D app |

## Edit

![The Edit menu](../img/gui/menu-edit.png)

| Item | What it is for |
| --- | --- |
| **Undo** ++ctrl+z++ | undoes the last edit; the menu says which |
| **Redo** ++ctrl+y++ | does it again |

Undo covers the drawings (units, spot heights, sections), the markers and their measurements, the scale bars,
the patches of the orthophoto, the reconstruction region, the edits of the products (classes given by hand,
points removed with the lasso, cleanings, faces of the model) and what the workspace removes. Every save made
in QGIS through the live link is one entry.

!!! warning "What is not undone"
    Processing steps (alignment, dense cloud, model...) are not undone: to go back, use the
    [versions](processing.md#versions) of the products. The history starts again when another project is
    opened.

## Workflow

![The Workflow menu](../img/gui/menu-workflow.png)

The commands are in working order: first the photos, then the alignment and the references, last the
products. The step-by-step guide is the [tutorial](../tutorials/tell.md); the details of every step are in
[Processing](processing.md).

**Photos and chunks**

| Item | What it is for |
| --- | --- |
| **Add photos...** · **Add folder...** | photos into the active chunk; multispectral captures are recognised by themselves |
| **Add video...** | the sharpest frames of a video, with the GPS when the `.SRT` of a DJI drone is there |
| **Add 360° photos...** | every spherical photo becomes the six faces of a cube, aligned together |
| **Relink photos (another folder)...** | finds the photos with the same names in another folder (moved, on another disk, "online only" on Dropbox) |
| **Add chunk** | an empty chunk |
| **Align chunks...** | brings other chunks into the frame of a reference chunk: markers with the same name, photos in common or overlapping clouds |
| **Merge chunks...** | a new chunk with the photos, markers, points, clouds and models of chunks already aligned |
| **Colours from the photos (scans)...** | colours a laser scan with the aligned photos of another chunk |

**Alignment and references**

| Item | What it is for |
| --- | --- |
| **Detect coded markers...** | finds the printed targets in the photos |
| **Refine the blue flags of the markers** | looks in the photos for the exact point of the estimated positions of the markers ([markers](photos-markers.md#guided-placement)) |
| **Align photos...** | computes the positions and orientations of the cameras (the window of Metashape's *Align Photos*) |
| **Import references...** · **Update georeference** | the coordinates of the control points, then the survey in their system |
| **New scale bar...** · **Import scale bars (CSV)...** | known distances between two markers ([scale bars](photos-markers.md#scale-bars)) |
| **Optimise cameras** | adjusts the block again with the control points and the bars |
| **Batch processing...** | a list of steps on several chunks, resumed if it stops ([batch](processing.md#batch-processing)) |
| **Check before leaving the site** | right after the alignment: areas seen by few photos, blurred photos, markers in few photos |
| **Estimate the precision** | the precision of cameras, markers, tie points and DEM |

**Products**

| Item | What it is for |
| --- | --- |
| **Build dense cloud...** | depth maps and dense cloud |
| **Crop dense cloud...** | by height or with a cylinder around the turntable axis (takes away mat and targets) |
| **Cleaning and confidence** | [see below](#workflow-cleaning-and-confidence) |
| **Build model...** · **Build texture...** | the mesh and its texture |
| **Build DEM...** · **Build orthophoto...** | the rasters in plan |
| **Reconstruction region** | [see below](#workflow-reconstruction-region) |
| **Elevation or cylindrical development...** | orthophoto and relief of a wall, or a section, on a plane (a section trace, markers with the axes chosen), or of a well or a tower on a cylinder ([elevations](processing.md#elevations)) |
| **Process everything...** ++ctrl+r++ | every step in a row with the default choices |

!!! info "Photos that cannot be read"
    The steps that read the photos (depth maps, texture, orthophoto, *Process everything*) first list the
    photos that do not open, such as the empty files kept online only by Dropbox, iCloud or OneDrive, and
    offer *Relink to another folder*, *Go on without these photos* or *Cancel*.

### Workflow > Cleaning and confidence

![The Cleaning and confidence submenu](../img/gui/menu-workflow-cleaning.png)

| Item | What it is for |
| --- | --- |
| **Compute the confidence...** · **Show the confidence in the 3D view** | how many depth maps confirm every point of the cloud and every vertex of the model |
| **Clean the dense cloud...** | isolated points, points little confirmed, of a class or of a colour |
| **Clean the model...** | detached pieces, stretched triangles, holes, smoothing, fewer faces |
| **Clean the tie points (gradual selection)...** | thresholds on error, number of photos and uncertainty, then the optimisation |
| **Select with the lasso in the 3D view** | points and faces to delete or to keep |
| **Crop with a polygon or a box...** | the products cut to an area |
| **Cut the model with a plane (parts, slices)...** | a horizontal, vertical, three-point or dipping plane |

Every cleaning can first keep a copy of the product among the versions. Details in
[Processing](processing.md#confidence-and-cleaning).

### Workflow > Reconstruction region

![The Reconstruction region submenu](../img/gui/menu-workflow-region.png)

A box (yellow in the 3D view) that limits depth maps, dense cloud, model, DEM and orthophoto: the trench and
not the field around it, the pot and not the table.

| Item | The box |
| --- | --- |
| **Around the tie points** | around the whole survey |
| **Rotated as the 3D view** | turned as the view |
| **On the selected polygon (plan)** | over the polygon chosen in plan |
| **Edit the values...** | centre, size and rotation typed in |
| **Clear the region** | everything the photos see again |

## Tools

![The Tools menu](../img/gui/menu-tools.png)

The tools that work on the products: drawing and measures, the analyses for archaeology, multispectral,
thermal and satellite captures, plugins.

| Item | What it is for |
| --- | --- |
| **Target** | target sheets, turntable mats and scale bars to print ([see below](#tools-target)) |
| **Calibrate the colours of the photos...** | exposure of every photo and vignetting of the lenses, corrected in orthophoto and texture |
| **Diagnostics of the alignment** | why an alignment fails: [see below](#tools-diagnostics-of-the-alignment) |
| **Masks of the photos** | the parts of the photos not to use: [see below](#tools-masks-of-the-photos) |
| **Drawing on the orthophoto** | the tools of the *Drawing* bar: [see below](#tools-drawing-on-the-orthophoto) |
| **Orthophoto patch (assign images)** | which photo to use in an area of the orthophoto: [see below](#tools-orthophoto-patch) |
| **Contour lines...** | the contours of the DEM, as a layer to query and export |
| **Cut / fill volume...** | the volume removed and added against a plane or a DEM of before |
| **Propose US and relationships (Extended Matrix)...** | number, type and stratigraphic relationships for the units with no number ([US proposal](archaeology.md#us-proposal)) |
| **Excavation phases (volumes and relationships)...** | a survey per phase: volumes between phases and relationships suggested ([phases](archaeology.md#excavation-phases)) |
| **Coregister DEM...** | moves the DEM onto the DEM of another survey where the ground did not change ([coregistration](processing.md#dem-coregistration)) |
| **Slope of the DEM** · **Aspect of the DEM** | the rasters of slope and aspect, with the degrees under the cursor |
| **Calibrate the colour with the colour chart...** | a ColorChecker photographed in the trench makes the Munsell colours comparable |
| **Relief visualisations...** | sky-view factor, openness, local and multi-scale relief, hillshades ([relief](archaeology.md#relief-visualisations)) |
| **Raster calculator...** | formulas on the rasters of the project, those of the plugins too, as in QGIS ([calculator](archaeology.md#raster-calculator)) |
| **Section options...** · **Plan slice (horizontal slice)...** | what the sections cut (model or cloud, classes); a plan at a height |
| **Pottery profile from a mesh...** | diameters, height and profile of a vessel or a sherd ([pottery](archaeology.md#pottery-profiles)) |
| **3D proxies of the units...** | the solids of the units for the Extended Matrix and BIM ([proxies](archaeology.md#us-proxies)) |
| **Classification of the cloud** | ground, vegetation, masonry...: [see below](#tools-classification-of-the-cloud) |
| **Segment Anything 2.1** | units and stones drawn by SAM 2.1: [see below](#tools-segment-anything-21) |
| **Export IFC (BIM)...** | the units or their proxies in an IFC file ([BIM and IFC](../bim.md)) |
| **Run Python script...** | a script with the modules of the program ([API](../api.md)) |
| **Multispectral** · **Thermal** | multispectral and thermal captures: [see below](#tools-multispectral) |
| **Satellite imagery** | satellite scenes on the DEM of the survey: [see below](#tools-satellite-imagery) |
| **Network nodes...** | other computers working on the same project ([nodes](processing.md#network-nodes)) |
| **Plugins...** | installs, enables and disables the plugins ([plugins](../plugins.md)) |

### Tools > Target

![The Target submenu](../img/gui/menu-tools-target.png)

| Item | What it prints |
| --- | --- |
| **Print target sheets...** | sheets of coded targets to place in the trench |
| **Turntable mat...** | a mat with a ring of targets and the CSV of their coordinates |
| **Printable scale bar...** | two targets at a known distance and a 10 cm ruler to check that it printed at 100 % |

### Tools > Diagnostics of the alignment

![The Diagnostics of the alignment submenu](../img/gui/menu-tools-diagnostics.png)

| Item | What it shows |
| --- | --- |
| **Matching graph...** | which photos connect to which: separate groups and isolated photos in red |
| **Matches between two photos...** | the two photos selected in the *Photos* pane, with the points that join them |
| **Tie point residuals in the photo** | where every point was measured and where the model puts it (arrows 20 times longer) |
| **Camera calibration...** | the parameters of the sensors, fixed calibration and the plot of the distortion |

### Tools > Masks of the photos

![The Masks of the photos submenu](../img/gui/menu-tools-masks.png)

The masked parts (red on the photo) are not used to align, in the depth maps, in the orthophoto and in the
texture.

| Item | What it is for |
| --- | --- |
| **No drawing (markers)** | the photo is used for the markers again |
| **Draw the mask on the photo** | click the vertices, double click or ++enter++ closes, ++esc++ drops |
| **Remove the mask** | gives an area back (also ++shift++ + double click) |
| **Show the masks** | shows or hides the red |
| **Import masks (images)...** · **Masks from the alpha channel of the photos** | masks made with another program |
| **Masks from the background...** · **Masks from a colour (backdrop)...** | from photos without the object, or from the colour of the backdrop |
| **Masks from the model** · **Masks with SAM 2.1** | only the object stays: from the model or with SAM 2.1 (the object at the centre of the photo) |
| **Export masks...** · **Remove...** | saves the masks, or removes them from the selected photos or all of them |

### Tools > Drawing on the orthophoto

![The Drawing on the orthophoto submenu](../img/gui/menu-tools-drawing.png)

These are the tools of the *Drawing* bar, described with their windows in [Drawing and measures](drawing.md).

| Item | What it is for |
| --- | --- |
| **Pan / query** · **Select** | move over the orthophoto and read attributes; choose features to delete or merge |
| **Draw stratigraphic unit** · **Freehand** | a polygon by its vertices, or traced keeping the mouse pressed |
| **Edit the vertices of the units** · **Reshape a unit with a line** | correct an outline, as in QGIS |
| **Trace section** | two clicks: the drawing of the section in SVG and DXF |
| **Measure distance** · **Measure area and 3D surface** | distances and height differences; areas in plan and on the surface, with the Munsell colour |
| **Spot height** · **Measuring point** | a spot height for PyArchInit; a point with elevation, temperature, bands and indices |
| **Marker** | a marker where you click, projected on the photos that see it |
| **Crop** · **Orthophoto patch** · **Edit DEM** | work on an area: cut, choose the photos, fill the DEM from the ground around it |
| **Segment Anything (SAM)** | units drawn by SAM: touch, box, area, stones |
| **Snap** | snaps the points to the vertices and segments of the visible layers |
| **Delete selected** · **Merge selected** · **Clear drawings** | remove or merge features |
| **Crop or export with the selected polygon...** | the crop of the products on a unit |

### Tools > Orthophoto patch

![The Orthophoto patch submenu](../img/gui/menu-tools-patch.png)

| Item | What it is for |
| --- | --- |
| **Draw an area and choose its photos** | an area, then the photo to use there (with a preview) |
| **Assign images to the selected polygon...** | the same on a unit already drawn |
| **Update the orthophoto with the patches** | applies the patches not applied yet |
| **Remove all patches...** | the orthophoto as it was |

How to choose the right photo is in [Processing](processing.md#orthophoto-patches).

### Tools > Classification of the cloud

![The Classification of the cloud submenu](../img/gui/menu-tools-classification.png)

| Item | What it is for |
| --- | --- |
| **Automatic classification (rules)** | ground, vegetation, masonry, noise and the archaeological classes |
| **Show the cloud by classes** | the cloud coloured by class, with its legend |
| **Correct by hand with the lasso (3D view)** | give a class to the points chosen |
| **Classes from the units (polygons)...** | the points inside every unit take its class |
| **Train the classifier...** · **Classify with the trained classifier...** | learns from the corrected points and classifies the rest |
| **Load...** · **Save the classifier...** | reuse a classifier (`.ppgclf`) on other excavations |
| **Forget the corrections by hand** | the classes stay, but the rules may change them again |

The classes drive the products: a DTM from the ground only, an orthophoto without tools and people, a mesh
of the masonry only ([classification](archaeology.md#classification-of-the-cloud)).

### Tools > Segment Anything 2.1

![The Segment Anything 2.1 submenu](../img/gui/menu-tools-sam.png)

| Item | What it is for |
| --- | --- |
| **Orthophoto - Touch** · **Box** · **Area** | a unit with one click, one object in the box, every unit in the box |
| **Orthophoto - Stones** | every stone in the box: the stone-by-stone drawing of a wall |
| **Orthophoto - all of it, scanned by tiles...** | every unit of the orthophoto, in the layer *Units from SAM* |
| **Prompts on the photos (on/off)** | segment an object on a photo |
| **Export the drawing of the stones...** | the stones as DXF, SVG at a scale or GeoJSON |
| **Install / update SAM 2.1...** · **Remove SAM 2.1** | SAM is installed on request, in its own environment |

### Tools > Multispectral

![The Multispectral submenu](../img/gui/menu-tools-multispectral.png)

The same steps as the *Multispectral* pane ([Multispectral and thermal](multispectral.md)).

| Item | What it is for |
| --- | --- |
| **Add multispectral captures...** | a folder with one file per band of every capture (MicaSense, DJI, Sequoia) |
| **Calibration panel...** | the capture of the panel and its reflectance: the bands become reflectance |
| **Reflectance orthomosaic...** | one band per band on the geometry of the orthophoto |
| **Index and zoning...** | NDVI, NDRE and the other indices; the zones as polygons in a GeoPackage |
| **Measuring point** · **Export the measuring points...** | the values at a point, and their list |

### Tools > Thermal

![The Thermal submenu](../img/gui/menu-tools-thermal.png)

| Item | What it is for |
| --- | --- |
| **Add thermal captures...** | the thermal frames and, when there are any, the visible photos of the same flight |
| **Download DJI Thermal SDK...** | DJI's program that reads the R-JPEGs, after accepting its licence |
| **Thermal measurement parameters...** | emissivity, reflected and ambient temperature, humidity, distance |
| **Thermal orthomosaic...** · **Show the thermal orthomosaic** | the orthophoto in degrees Celsius |
| **Thermal anomalies** | the temperature minus that of its surroundings: buried walls and ditches |
| **Export the temperatures** | GeoTIFF in degrees or in centikelvin, CSV, FLIR radiometric JPEGs |
| **Measuring point** · **Export the measuring points...** · **Thermal pane** | the values at a point; the pane with every step |

### Tools > Satellite imagery

![The Satellite imagery submenu](../img/gui/menu-tools-satellite.png)

| Item | What it is for |
| --- | --- |
| **Orthorectify a scene...** | a scene with its RPCs on the DEM of the survey, as a version of the orthophoto |
| **DSM from two or more scenes...** | the surface from scenes taken from different directions, as a version of the DEM |
| **About the scene...** | size, bands, ground resolution and footprint |

## PyArchInit

![The PyArchInit menu](../img/gui/menu-pyarchinit.png)

The link with the database of the excavation; the forms and the relationships stay in PyArchInit
([PyArchInit](../pyarchinit.md), [tools for archaeology](archaeology.md#pyarchinit)).

| Item | What it is for |
| --- | --- |
| **Connect (config.cfg, SQLite or PostgreSQL)...** | the database configured in PyArchInit, a SQLite file or a PostgreSQL server |
| **Use the demo database** | a test database with three sites |
| **Project site...** | the excavation the units and spot heights of the project belong to |
| **New site...** · **New US form...** | creates the form of a site or of a US |
| **Update the database structure...** | adds the missing PyArchInit 5 columns, after a backup copy |
| **Connect to the PyArchInit database at start-up** | connects by itself when the program opens |
| **Publish orthophoto / DEM / model...** | the products in the media of PyArchInit, linked to the US |
| **Publish the drawn units and spot heights** · **only the units** · **only the spot heights** | writes `pyunitastratigrafiche` and `pyarchinit_quote` |
| **Load US polygons on the orthophoto** | shows the units already in the database |
| **Refresh pane** | reads sites, US and documentation again |

## QGIS

![The QGIS menu](../img/gui/menu-qgis.png)

| Item | What it is for |
| --- | --- |
| **Open in QGIS (live link)** | a QGIS project with orthophoto, DEM and drawings |
| **Live link with QGIS (both ways)** | what you draw here reaches QGIS; what you save in QGIS comes back here within a second |
| **Open the folder of the layers for QGIS** | the folder of the shared GeoPackage |

How the link works both ways is in [QGIS, live](../integrations.md#qgis-live).

## View

![The View menu](../img/gui/menu-view.png)

| Item | What it is for |
| --- | --- |
| **Panes** | every pane open or closed: [see below](#view-panes) |
| **Workspace** ... **Log** | brings a pane to the front (the console also with ++ctrl+shift+p++) |
| **Model** ++f2++ · **Orthophoto / DEM** ++f3++ · **Photos** ++f4++ | the three central views (in the workspace ++f2++ renames) |
| **Theme** | light or dark, remembered |
| **Lingua / Language** | Italian or English, at the next start (*Restart now* opens the project again at once) |

### View > Panes

![The Panes submenu](../img/gui/menu-view-panes.png)

| Item | What it is for |
| --- | --- |
| a pane with its tick | open; without the tick it is closed. *Workspace*, *Reference* and *Layer* are always there |
| **Next pane** ++f6++ · **Previous pane** ++shift+f6++ | goes round the panes of the same group |
| **Restore the layout of the panes** | every pane back in its first place, all of them open |

Moving, floating and docking the panes again is in [Panes](panes.md#moving-the-panes).

## Help

![The Help menu](../img/gui/menu-help.png)

| Item | What it is for |
| --- | --- |
| **Online documentation** | this guide |
| **Environment diagnosis...** | Python, the libraries and the known problems, with what to do; *Copy* for a bug report |
| **Open the log folder** | the log files to attach to a bug report |
| **Check for updates...** · **at start-up** | a newer version, and whether to look for it at start-up ([updates](help.md#updates)) |
| **Update the plugins by themselves** | whether the plugins installed by the program download their newer versions, put in place at the next start ([plugin updates](../plugins.md#updates)) |
| **Support the project (PayPal)...** | a donation |
| **About** | version, libraries and licence |

## Plugins

The *Plugins* menu appears between *View* and *Help* when at least one plugin is enabled: every plugin puts
its commands there, grouped under its name. The formats a plugin writes go in *File > Export* instead. They
are installed from *Tools > Plugins...* ([plugins](../plugins.md), [plugin guides](../plugins/guides.md)).
