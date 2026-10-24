# terrio

The *terrio* plugin brings into the project what other instruments and other programs make, and
sends the project's products out in their formats. It is built on [terrio](https://github.com/opengeos/terrio),
a library of geospatial input and output written in Rust: nothing else - GDAL, PROJ, PDAL - has to be
installed. It imports point clouds of any common format, from a file or from the web; it separates the
ground from the rest of the dense cloud with the filters used for airborne lidar; it cleans the cloud of
isolated points and detached groups; it gives the points the colours of an orthophoto, or a value of a
raster - a temperature, an index, a height; it moves a DEM or a cloud between ellipsoidal (GNSS) heights
and heights above a geoid; it fetches a reference raster - a regional DEM, an older orthophoto, the
Copernicus DEM of Microsoft's Planetary Computer - over the survey; it exports the cloud for
laser-scanning software and the orthophoto as tiles for web maps.

This page describes version 0.2.0 of the plugin, which needs pyarchinit-photogrammetry 1.1.0 or later
(1.3.4 or later to see the points found and the values in the 3D view) and terrio 0.5. An older terrio
(0.3.2 and the 0.4 releases) still runs the import, the ground classification, the reference rasters
from a URL and the exports; the other steps say that they need terrio 0.5 and offer to download it. Its README and source code are in the
[plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/terrio).

<figure markdown="span">
  ![The surface of all the points and of the ground points only](../img/plugins/terrio/dsm-dtm.jpg)
  <figcaption>The ground of a dense cloud of 3.1 million points from 57 drone photos, classified with
  SMRF (cell 0.5 m, slope 0.2, threshold 0.3 m, low noise first). (a) The surface of all the points,
  with the 74,536 points left out of the ground in red; (b) the surface of the ground points only: the
  houses and most of the scrub on the slopes are gone. Hillshades of the points gridded at 30 cm, in a local
  frame; Tell Sultan, the site of the tutorial.</figcaption>
</figure>

## In short

1. Install the plugin; the first time a step needs terrio, the program offers to download it (about
   20 MB), or *Plugins > terrio > Install terrio...*.
2. *Import point cloud...*: a LAS, LAZ, COPC, EPT, PLY or E57 file, or a web address, into a new chunk
   or in place of the dense cloud of the active one.
3. *Classify the ground (SMRF, CSF, PMF)...*: the points without another class become ground (class 2) or
   the rest (class 1); then build a DEM
   of the *Ground* class: a terrain model.
4. *Clean the cloud (LOF, DBSCAN)...*: isolated points to class 7, detached groups (birds, people,
   floating fragments) removed or classed - counted and shown before anything changes.
5. *Colours and values from a raster...*: the colours of the orthophoto for the points, or a value
   (temperature, index, height) as a dimension of them; *Values of the points in the 3D view...* shows it.
6. *Ellipsoidal and geoid heights...*: the DEM or the cloud from GNSS heights to heights above EGM2008,
   EGM96 or a national geoid, or back, as a new version.
7. *Reference raster from the web...*: a regional DEM or an orthophoto over the survey - from a URL or
   from the Planetary Computer - kept as a version of the DEM or of the orthophoto, its heights moved
   into those of the survey when asked, to compare with yours.
8. *File > Export*: the dense cloud as E57 or EPT, the orthophoto as PMTiles or MBTiles.

## What it does

| Step | What it gives | Useful for |
|---|---|---|
| Import point cloud | a cloud of LAS, LAZ, COPC, EPT, PLY or E57 - also zipped, also on the web - as the dense cloud of a new chunk or of the active one, with its colours (or its intensity as greys) and its classes | a laser scan, the lidar of a regional survey, the cloud of another program, beside the photogrammetry |
| Classify the ground | ground (class 2) and other points (class 1), low noise (7), vegetation by height (3, 4, 5); the points of other classes stay as they are | a DTM under trees and buildings, the ground for the [Terrain](terrain.md) and [Powerlines](powerlines.md) plugins |
| Clean the cloud | isolated points in class 7, detached groups removed or in a class (18 by default); the cloud of before kept as a version | DEMs and meshes without spikes, birds and floating fragments of wrong depths |
| Colours and values | the colours of an orthophoto (or of any raster) for the points; or a band as a dimension of the points, exported with them | a lidar without colours; the temperature or an index on the 3D cloud, in other programs too |
| Heights | a DEM or a cloud between ellipsoidal heights and heights above a geoid, as a new version | putting a drone survey and a national DEM, or two surveys, on the same heights |
| Reference raster | a raster on the web, on disk or in the Planetary Computer, read over the survey only, reprojected into the system of the chunk, its heights moved when asked, kept as a version | comparing the survey with a regional DEM or an older orthophoto, the landscape around the site |
| Exports | E57 and EPT of the cloud (LAS, LAZ, COPC and PLY from the command line), PMTiles and MBTiles of the orthophoto | laser-scanning software, web viewers of point clouds, web maps, maps on a tablet in the field |

## What you need first { #before }

* **The plugin**, installed from the program: see [Plugin guides](guides.md#install).
* **terrio itself.** Installed with pip, the plugin brings it along. The desktop application has no pip:
  the first time a step needs terrio, the program asks whether to download it from PyPI (about 20 MB,
  its SHA-256 checked) into the plugin's own folder; after the download, start the step again.
  *Plugins > terrio > Install terrio...* does the same at any time, `ppg plugin terrio install` from the
  command line. terrio exists for macOS (Apple Silicon and Intel), Windows (x64 and ARM64) and Linux
  (x86-64 and ARM64). When the terrio there is older than 0.5, the steps that need 0.5 say so and offer
  the download: the new one goes beside it and is used from then on.
* **A project.** An import into a new chunk needs nothing else; the classification and the export of
  the cloud need a chunk with a dense cloud; the tiles need an orthophoto; the reference raster needs a
  chunk with a reference system (*the chunk needs a reference system (align it with GPS or markers
  first)* otherwise).
* **Memory.** The ground filters work on the whole cloud at once: tens of millions of points need a few
  gigabytes.
* **Internet**, for the sources on the web, the Planetary Computer and the geoid grids that are not on
  the computer yet.

## The data { #data }

### Point cloud files { #clouds }

| Format | Files | Notes |
|---|---|---|
| LAS, LAZ | `.las`, `.laz` | the formats of airborne lidar and of most programs; compressed in LAZ |
| COPC | `.copc.laz` | a LAZ arranged for reading parts of it: on the web only the parts needed are read |
| EPT | the `ept.json` of an EPT folder | the format of Entwine and of many open lidar archives; read in place on the web |
| PLY | `.ply` | the clouds of photogrammetry and of many programs |
| E57 | `.e57` | laser scanners: every scan placed with its pose, all of them in one cloud |
| ZIP | `.zip` | one of these inside |

* **On the web**: an address beginning with `https://`, `s3://`, `gs://` (Google Cloud) or `az://`
  (Azure) in place of a file.
* **What comes in**: the coordinates, the colours (16-bit colours are brought to 8 bits), or the
  intensity as greys when the file has no colours, and the classification codes. Other attributes
  (returns, GPS time) are not kept.
* **Reference system**: LAS, LAZ, COPC and EPT usually carry theirs; E57 and PLY often do not. Give
  it in the import window (for example `EPSG:32633`) when the file has none; without one the new chunk
  is local.
* **Points at most**: a cloud larger than the survey needs - a regional lidar tile, a dense scan - can be
  thinned at the import: from COPC and EPT fewer levels are read, then the points are chosen at random.
* **Only these classes**: the ASPRS codes of the classes to keep, for example `2` for the ground of a
  classified lidar, `2,6` ground and buildings.

### New chunk or in place { #destination }

* **A new chunk**, called after the file (or the name given), in the reference system of the file or
  the one given. In the window it becomes the active chunk; from the command line the active chunk of
  the project stays as it was (`--activate` makes the new one active). Its points are kept from a round
  origin, the floor of their smallest coordinates, as the program does for laser scans.
* **In place of the dense cloud of the active chunk**: the cloud there is kept as a version (in the
  *Workspace*, under the dense cloud; right click, *Make active (the other tools use it)*, to go back).
  When the file has another reference system the points are reprojected into the chunk's; a chunk
  without a reference system refuses a file with one (*... has no reference system and the file has one
  (EPSG:...): import it into a new chunk*).

### Before classifying { #before-ground }

* The classification changes only the points of classes **0** (never classified), **1** (unclassified)
  and **2** (ground) - and **3, 4, 5** when the vegetation classes are asked for: the ground takes 2, the
  rest 1 (7 the low noise, 3, 4 and 5 the vegetation when asked). Every other class stays as it is:
  buildings (6), water (9), the walls and the other archaeological classes of the program (64 and up),
  the noise found before (7 and 18, which are also left out of the filters: a wrong point far below the
  ground cannot pull the ground down), the classes of an imported lidar; and so do the points labelled by
  hand. A cloud that came in classified keeps what it has: to classify it all again, give its points
  class 1 first.
* With the vegetation classes, every point above the ground that has no other class is classed by its
  height above the nearest ground point - under 0.5 m low vegetation (3), under 2 m medium (4), higher
  high (5): standing walls, sections, spoil heaps and buildings that are not classified otherwise become
  "vegetation" too (the tooltip of the option says so). Classify them first - the walls in class 64,
  the buildings in 6: those classes stay - or correct by hand afterwards. Use it for the plants of a
  field, and look at the result.
* The cloud must be in metres with z upwards: a chunk with a reference system, or levelled.

### Reference rasters { #reference-data }

* **A Cloud Optimized GeoTIFF on the web**, by its `https://` address: only the part over the survey is
  read. Many national DEMs and global ones (Copernicus, for example) are published as COGs.
* **A STAC search**, written `STACIT:...` as terrio expects it (see the terrio documentation).
* **A file** on disk, any GeoTIFF.
* The area read is the extent of the survey - of the DEM, else of the orthophoto, of the dense cloud or
  of the aligned photos - plus the margin; the raster is reprojected into the system of the chunk.
* **Microsoft Planetary Computer**: a collection by name - *Copernicus DEM GLO-30* and *GLO-90*
  (EGM2008 heights), *ALOS World 3D* and *NASADEM* (EGM96), *USGS 3DEP* (United States only, NAVD88),
  *Sentinel-2 L2A* in natural colours (the newest image under 10 % of clouds in the dates given) and
  *NAIP* (United States only) - or any other collection and asset of its catalogue. The catalogue is
  searched over the survey and its files read by window, their addresses signed by terrio. No account
  is needed; the anonymous access is rate limited (a subscription key in the environment variable
  `PC_SDK_SUBSCRIPTION_KEY` is sent when set). A Sentinel-2 image is slow: terrio fetches about 200 MB
  of each for a window, so only one is read.
* **Heights.** A national or global DEM may give heights above the geoid (sea level) while a drone
  survey with GNSS gives heights above the ellipsoid, or the other way round: the difference is the
  undulation of the geoid, tens of metres in many places (in a test a 30 m global DEM lay 37.6 m below
  the drone DEM). The window moves a DEM's heights into those of the survey on the way: the catalogue
  knows the heights of its DEMs, for a URL say them (see [Heights](#heights-data)).

### Heights { #heights-data }

A height system is one of:

* **the ellipsoid** (GNSS heights): of the datum of the chunk's system - WGS 84 for UTM, ETRS89 for
  ETRS89 systems - or WGS 84 when the system is on an older datum (Monte Mario, OSGB36), on which GNSS
  heights never are;
* **a geoid**: EGM2008 (`EPSG:3855`), EGM96 (`EPSG:5773`), or any vertical system of the EPSG dataset
  (`EPSG:5701` ODN, `EPSG:5703` NAVD88...): terrio uses the geoid model and the transformations that
  PROJ would use;
* **a geoid grid file** (`.gtx`, or a PROJ GeoTIFF grid `.tif`): a national geoid that the PROJ CDN does
  not carry (the Italian ITALGEO, for instance, is distributed by the IGM): heights above it.

The grids come from the PROJ CDN (`https://cdn.proj.org`) the first time they are needed - EGM2008 is
80 MB, EGM96 3 MB - and are kept in `plugins/_data/terrio/grids/` inside the plugin folder of the program
(`ppg plugin terrio version` prints it), where they serve again without the network; grids already on
the computer (a folder given in the window, PROJ's own folders) are used first. A height that no grid
moved - the grid missing and no network, or a survey outside the model - is an error (*No model moved
the heights from ... to ...*), never a copy of the heights as they were.

## Step by step in the program { #program }

The *Plugins > terrio* menu has *Import point cloud...*, *Classify the ground (SMRF, CSF, PMF)...*,
*Clean the cloud (LOF, DBSCAN)...*, *Colours and values from a raster...*, *Values of the points in the 3D
view...*, *Ellipsoidal and geoid heights...*, *Reference raster from the web...* and *Install terrio...*; *File > Export* has *Point cloud E57
(terrio)...*, *Point cloud EPT (terrio)...*, *Orthomosaic PMTiles (terrio)...* and *Orthomosaic MBTiles
(terrio)...*. The steps that change the project run in the background with the progress bar, then the
project is saved and the views refreshed. Most fields keep the value used the last time.

### 1. Import a point cloud { #import }

*Import point cloud...*

| Field | What it does |
|---|---|
| *File* | LAS, LAZ, COPC, PLY, E57, a zip of them or the `ept.json` of an EPT |
| *Or URL (https, s3, gs, az)* | a cloud on the web, in place of the file |
| *Destination* | *a new chunk*, or *the dense cloud of the active chunk (the current one is kept as a version)* |
| *Name of the new chunk (empty: file name)* | |
| *Points at most (0: all)* | |
| *Only these classes (e.g. 2,6; empty: all)* | the ASPRS codes of the classes to keep |
| *Reference system when the file has none (e.g. EPSG:32633)* | |

At the end a window gives the points imported and the chunk (*7,400 points in laser.*); a new chunk
becomes the active one. To see the cloud: the *Model* view, *Dense cloud*.

### 2. Classify the ground { #ground }

*Classify the ground (SMRF, CSF, PMF)...* works on the dense cloud of the active chunk.

| Field | Default | What it does |
|---|---|---|
| *Filter* | SMRF | *SMRF (Simple Morphological Filter)*, *CSF (Cloth Simulation Filter)* or *PMF (Progressive Morphological Filter)* |
| *Cell (m) - SMRF and PMF* | 1.0 | the grid on which the lowest surface is built |
| *Slope - SMRF* | 0.15 | the steepest ground expected, as rise over run (0.15 = 15 %) |
| *Height threshold (m) - SMRF and CSF* | 0.5 | how far above the estimated ground a point may be and still be ground (SMRF), its distance from the cloth (CSF) |
| *Slope - PMF* | 1.0 | the slope of PMF, which sets how fast its height threshold grows with the windows |
| *Initial distance (m) - PMF* | 0.15 | the first height step of PMF |
| *Largest window (m; empty: the filter's)* | empty | SMRF and PMF: the size of the largest object to take away, such as the largest building |
| *Cloth resolution (m) - CSF* | 1.0 | the size of the cells of the cloth |
| *Cloth rigidness (1-3) - CSF* | 3 | 3 for flat ground, 2 for relief, 1 for steep slopes |
| *Low noise first (class 7, ELM)* | off | the isolated points below the ground (wrong depths, reflections) found first and left out of the ground |
| *Other points as classes 3/4/5 by height above the ground (0.5 and 2 m)* | off | the vegetation classes, [with care](#before-ground): walls, sections and spoil heaps without another class become vegetation too |

The three filters are those of airborne lidar processing (PDAL), run by terrio:

* **SMRF** builds the lowest surface on a grid and grows its windows step by step, keeping as ground
  the points within the threshold of it. A good start for a photogrammetric cloud: a cell of half a
  metre to a metre, the slope of the steepest real ground (0.2-0.3 for the slopes of a tell), a
  threshold of a few decimetres.
* **CSF** turns the cloud upside down and drops a cloth on it: the points close to the cloth are ground.
  Little to set: the resolution of the cloth and its rigidness.
* **PMF** opens the surface with growing windows and a height threshold growing with them; it has its
  own fields, with its own defaults - slope 1.0, initial distance 0.15 m - apart from those of SMRF.

At the end a window counts the classes, named as the program names them, in its language:

```text
Filter SMRF: 7,400 points, 7,350 changed
  50 points of other classes or labelled by hand left as they were
  2 Terrain: 6,105
  3 Low vegetation: 140
  4 Medium vegetation: 553
  5 High vegetation: 550
  7 Noise (low): 2
  64 Masonry structure: 50
```

### 3. Clean the cloud { #clean }

*Clean the cloud (LOF, DBSCAN)...* works on the dense cloud of the active chunk, in two steps: first the
points are found and counted, then you decide.

| Field | Default | What it does |
|---|---|---|
| *Isolated points to class 7 (LOF)* | on | the local outlier factor of every point: the density of its neighbours over its own, about 1 on a surface |
| *Neighbours for LOF* | 20 | |
| *LOF threshold* | 3 | above it a point is isolated; lower: more points |
| *Detached groups (DBSCAN)* | on | |
| *DBSCAN radius (m; empty: 4 times the spacing of the points)* | empty | two points closer than this are in the same group; the spacing is the median distance of each point from the nearest one |
| *Fewest points of a DBSCAN core* | 6 | |
| *Detached when under (% of the cloud)* | 1 | the largest group is never detached |
| *The detached groups* | in a class | *in a class (below)* or *removed from the cloud* |
| *Class of the detached groups* | 18 | 18 high noise, 7 low noise: the program leaves both out of DEMs and meshes |

A group is detached when DBSCAN finds it small and it is also more than 4 radii from any other point (a
second grouping, by distance, says so): the sparse margins of a drone survey, which DBSCAN breaks into
small groups at the radius of the dense centre, stay attached to the rest. The analysis runs in the
background; then, with the program 1.3.4 or later, the 3D view shows the cloud with the isolated points
red and the detached groups orange, and a window gives the counts:

```text
7,352,132 points, spacing between the points 0.00799 m
  Isolated points (LOF > 3, 20 neighbours): 910
  Detached groups (DBSCAN, radius 0.032 m, under 1 % of the cloud, over 0.128 m from the rest): 824 with 10,301 points
    the largest: 586, 426, 186, 167, 156
```

*Apply* changes the cloud: the cloud of before goes among the versions (*Workspace*, under the dense
cloud: *Make active* to undo), the new one is named *Cloud cleaned with terrio, <date>*. The points
labelled by hand never change and are never removed; the points already in classes 7 and 18 are left out
of the analysis. The values the plugin gave the points (below) go with them.

### 4. Colours and values from a raster { #colours }

*Colours and values from a raster...* works on the dense cloud of the active chunk.

| Field | What it does |
|---|---|
| *Raster* | the rasters of the chunk: the active orthophoto and its versions, the DEM and its versions, the temperature of the thermal orthophoto, the indices and the bands of the reflectance (multispectral); or *another raster (file or URL, below)* |
| *File*, *Or URL (COG, STACIT:...)* | another raster, in any reference system |
| *What to take* | *by the raster* (colours from an orthophoto, a value from the others), *the colours (bands 1, 2, 3)*, *a value, as a dimension of the points* |
| *Band of the value (file)* | for another raster |
| *Name of the dimension (empty: from the raster)* | lower case letters, digits and `_`: `temperature`, `ndvi`, `height` |
| *Then show the value in the 3D view with* | a palette, or not |

* **Colours.** The points the raster covers take its colours (bands of 16 bits or of floats stretched
  between their 2nd and 98th percentiles; transparent and nodata pixels give none); the others keep
  theirs, grey when they had none - a lidar imported without colours. The cloud of before is kept as a
  version.
* **A value.** A band becomes a dimension of the points: a property of the vertices of the dense cloud's
  PLY (a `float`, NaN where the raster has no value), as the program keeps its confidence; CloudCompare
  shows it as a scalar field. The colours do not change. The plugin's exports (E57, EPT, and LAS, LAZ,
  COPC, PLY from the command line) carry it as an extra dimension, and its own steps keep it.
* The raster is read tile by tile over the points - a large orthophoto is never read whole - and, when it
  is in another system, warped into the chunk's (bilinear).

*Values of the points in the 3D view...* picks one of these dimensions and shows the cloud coloured by
it (*turbo*, *viridis*, *inferno*, *grey*, *rdylgn*, *coolwarm*, *terrain*; between the 2nd and 98th
percentiles, or the limits given) beside the list *Show* of the 3D view; it can also take the dimension
away.

### 5. Ellipsoidal and geoid heights { #heights }

*Ellipsoidal and geoid heights...*

| Field | Default | What it does |
|---|---|---|
| *What* | DEM | *DEM* or *Dense cloud (active)* |
| *Which DEM* | the active DEM | or a kept version (a reference raster, for instance) |
| *Heights now* | Ellipsoid (GNSS heights) | *EGM2008 geoid (EPSG:3855)*, *EGM96 geoid (EPSG:5773)* or *other (written below)* |
| *Other system now* | | a vertical CRS (`EPSG:5703`) or a geoid file (`.gtx`, `.tif`) |
| *Heights wanted* | EGM2008 geoid | as above |
| *Make it the active version* | off | |
| *Download missing grids from the PROJ CDN* | on | |
| *Keep the grids downloaded in the plugin folder (then also offline)* | on | off: the grids are read over the network, in place, each time |
| *Folder with other grids (optional)* | | grids of your own, searched first |

The result is a new version of the DEM or of the cloud, named after the one converted (*DEM of 24/10
17:27 (EGM2008 heights)*); the one converted does not change. A window gives the shift:

```text
"DEM of 24/10 17:27 (EGM2008 heights)" (among the versions)
Heights from Ellipsoid (GNSS heights) to EGM2008 geoid (EPSG:3855): moved by -47.896 m (-47.903 to -47.890)
Grids downloaded into: .../plugins/_data/terrio/grids
```

### 6. Reference raster { #reference }

*Reference raster from the web...*

| Field | Default | What it does |
|---|---|---|
| *URL of the COG, STAC search (STACIT:...) or file* | | the source |
| *Or Microsoft Planetary Computer* | none | a collection of the catalogue (see [Reference rasters](#reference-data)) or *another collection (below)* |
| *Other collection and asset* | | e.g. `esa-worldcover map` |
| *Dates (imagery)* | | `2024-06-01/2024-09-30`: Sentinel-2 and NAIP |
| *Keep it as a version of* | DEM | *DEM* or *Orthomosaic*; for the collections of the catalogue the catalogue decides |
| *Margin around the survey (m)* | 20 | how far beyond the survey the raster is read |
| *Resolution (m; empty: the source's)* | empty | the cell of the result |
| *Heights of the raster (DEM)* | as the catalogue says | the heights of the source |
| *Convert the heights to* | no conversion | the heights of the survey: the ellipsoid for a drone with GNSS |
| *Name of the version (empty: from the file)* | empty | `Reference: <file name>` when empty (`Riferimento: ...` when the program speaks Italian) |

The raster is read over the survey and the margin, reprojected into the system of the chunk (bilinear
for a DEM, cubic for an orthophoto) and kept as a version: the active DEM or orthophoto does not change.
A window gives the size (*Version "Reference: dem_regionale.tif": 60 x 60 pixels at 1 m.*). The
version is in the *Workspace* under the DEM (or the orthophoto): right click, *Show*; for a DEM also
*Difference with the active DEM*. With the heights converted, the name says so (*Reference:
cop-dem-glo-30 (ellipsoidal heights)*) and the window gives the shift.

### 7. Exports { #exports }

* *File > Export > Point cloud E57 (terrio)...*: the dense cloud in the coordinates of the survey, with
  colours and classes, as one scan.
* *File > Export > Point cloud EPT (terrio)...*: an EPT dataset - a folder with `ept.json`, `ept-data/`
  and `ept-hierarchy/` - for web viewers and for QGIS.
* *File > Export > Orthomosaic PMTiles (terrio)...* and *... MBTiles (terrio)...*: the active orthophoto
  as tiles of web maps, in Web Mercator. PMTiles is one file for web maps; MBTiles is read by QGIS,
  QField and many mapping applications.
* LAS, LAZ, COPC and PLY: from the command line (`export`).

## From the command line { #cli }

`ppg plugin terrio WHAT PROJECT ...`. The commands work on the active chunk (`--chunk NAME` for
another one); those that change the project save it.

```bash
ppg plugin terrio version
ppg plugin terrio install
ppg plugin terrio import scavo.ppg scansione.e57 --label laser --crs EPSG:32633
ppg plugin terrio import scavo.ppg https://host/lidar.copc.laz --chunk rilievo --max-points 5000000 --classes 2
ppg plugin terrio ground scavo.ppg --method smrf --cell 0.5 --slope 0.2 --threshold 0.3 --noise
ppg plugin terrio ground scavo.ppg --method pmf
ppg plugin terrio ground scavo.ppg --method csf --resolution 0.5 --rigidness 2
ppg plugin terrio reference scavo.ppg https://host/dem_regionale.tif --product dem --margin 50 --resolution 1
ppg plugin terrio reference scavo.ppg ortofoto_2010.tif --product orthomosaic --name "Ortofoto 2010"
ppg plugin terrio reference scavo.ppg --collection cop-dem-glo-30 --margin 100 --heights-to ellipsoid
ppg plugin terrio reference scavo.ppg --collection sentinel-2-l2a --datetime 2024-06-01/2024-09-30
ppg plugin terrio catalogue
ppg plugin terrio clean scavo.ppg --dry-run
ppg plugin terrio clean scavo.ppg --remove
ppg plugin terrio colour scavo.ppg --list
ppg plugin terrio colour scavo.ppg orthomosaic
ppg plugin terrio colour scavo.ppg thermal
ppg plugin terrio colour scavo.ppg index:NDVI
ppg plugin terrio values scavo.ppg
ppg plugin terrio heights scavo.ppg --from ellipsoid --to EPSG:3855
ppg plugin terrio heights scavo.ppg --product point_cloud --from ellipsoid --to italgeo.gtx --activate
ppg plugin terrio export scavo.ppg nuvola.e57
ppg plugin terrio export scavo.ppg nuvola.ept
ppg plugin terrio export scavo.ppg nuvola.copc.laz
ppg plugin terrio export scavo.ppg ortofoto.pmtiles
```

| Command | Options |
|---|---|
| `version` | the terrio in use (exit code 1 when there is none) and the folder of the grids |
| `install` | downloads terrio into the plugin's folder |
| `import PROJECT SOURCE` | a file or a web address; without `--chunk` a new chunk (`--label` its name) - the active chunk of the project stays, `--activate` makes the new one active; `--chunk NAME`: in place of the dense cloud of that chunk; `--max-points`; `--classes 2,6`; `--crs EPSG:...` |
| `ground PROJECT` | `--method` smrf, csf or pmf; `--cell` (1.0); `--slope` (the filter's: SMRF 0.15, PMF 1.0); `--threshold` (the filter's: SMRF and CSF 0.5, PMF 0.15 - its initial distance); `--window`; `--resolution` (1.0, CSF); `--rigidness` (3, CSF); `--noise`; `--vegetation` |
| `clean PROJECT` | `--no-lof`; `--lof-neighbours` (20); `--lof-threshold` (3); `--no-dbscan`; `--eps` (4 times the spacing); `--min-points` (6); `--max-share` (1 %); `--remove`, else `--detached-class` (18); `--dry-run`: only the counts |
| `colour PROJECT SOURCE` | `orthomosaic`, `dem`, `thermal`, `index:NDVI`, `reflectance:N`, `version:dem:ID`... (`--list` prints them) or a file or URL; `--rgb` or `--value`; `--band`; `--name` |
| `values PROJECT` | the dimensions of the points; `--remove NAME` |
| `heights PROJECT` | `--from`, `--to`: `ellipsoid`, `EPSG:3855`, `EPSG:5773`, any vertical CRS or a geoid file; `--product` dem or point_cloud; `--version ID`; `--activate`; `--name`; `--no-network`; `--no-keep-grids`; `--grids FOLDER` |
| `reference PROJECT [URL]` | `--collection` (Planetary Computer), `--asset`, `--datetime`, `--max-items`, `--cloud`; `--product` dem or orthomosaic; `--margin` (20); `--resolution`; `--name`; `--heights-from`, `--heights-to`; `--no-network` |
| `catalogue` | the collections of the Planetary Computer offered by name |
| `export PROJECT OUT` | `.e57`, `.ept` (a folder), `.las`, `.laz`, `.copc.laz`, `.ply`: the dense cloud; `.pmtiles`, `.mbtiles`: the orthophoto |

## Reading the results { #reading }

### The classes { #reading-classes }

* The window - or the command - counts the points of every class, how many changed and how many of
  other classes (or labelled by hand) were left as they were.
* Look at them: the *Model* view, *Dense cloud*, colours *Classes*.
* Then the terrain model: *Workflow > Build DEM...* with the *Ground* class only; make it the active
  version for the tools that need the bare ground.
* Read the DTM as a hillshade or with the *Terrain* plugin. Bumps where buildings or trees stood mean
  that some of their points stayed ground: lower the threshold, give a larger window (SMRF, PMF) or a
  finer cloth (CSF). Holes and steps on steep real ground - the slopes of a tell, the walls of a trench -
  mean that ground was taken away: raise the slope or the threshold, or use CSF with rigidness 1 or 2.
* Thin structures on bare ground - a low wall, a bank - are often classed as ground: for the ground
  model that may be right, for a plan of the walls it is not.

### Cleaning { #reading-clean }

* LOF is a ratio of densities: on a photogrammetric cloud whose density changes a lot - photos taken
  from two heights, a dense centre and sparse margins - it also marks points along the edge between a
  dense part and a sparse one. A threshold of 3 leaves most of them out (2 marked them all along such an
  edge in a test on a real cloud); look at the red points before applying.
* Tree canopies whose trunks the photos did not see are detached groups too, as are birds and people:
  that is why the default is a class, not removal. A class can be changed again; removed points come
  back only with the version.
* On a full-resolution crop of 7.4 million points of a drone survey (40 x 40 m) the analysis took about
  a minute: 910 isolated points and 824 detached groups with 10,301 points.

### Values and heights { #reading-values }

* The program does not know the dimensions of the plugin: when it writes the cloud again - its own
  cleaning and classification, a crop - they go, and *Values of the points...* no longer lists them:
  take them again from the raster.
* A conversion of heights adds the same shift to every height of a place: the shape of the DEM does not
  change. Over a site the shift is almost constant (a few millimetres over a few hundred metres); compare
  it with the difference you see between two DEMs: what is left is the error of one of them or a real
  change.
* On a survey in southern Italy the shift from the ellipsoid to EGM2008 was -47.896 m, PROJ's own value
  to the millimetre; the Copernicus DEM of the Planetary Computer, moved into ellipsoidal heights, came
  15 m from that survey's DEM instead of 63 m - the rest being the drone's own heights, not GNSS ones.

### Reference rasters { #reading-reference }

The version shows the source over the survey on the grid you asked for. *Difference with the active
DEM* shows where the two DEMs differ: a uniform difference is a different height system or an offset of
the survey; a pattern is a real change (excavation, erosion, a building) or the error of one of the two.
A regional DEM of tens of metres smooths a site into a few cells: it describes the landscape around it,
not the site.

### Limits { #limits }

* **All in memory.** Import and classification hold the whole cloud.
* **One classification for the whole cloud.** Different parts of a site may want different parameters:
  classify chunks separately, or correct by hand (the program keeps the hand labels).
* **E57 in one piece.** The scans of an E57 are merged into one cloud at the import and written as one
  scan at the export; the program's own E57 import keeps them apart.
* **Values are not program data.** See [Values and heights](#reading-values).
* **terrio is young.** Its functions may still change before its version 1.0; the plugin accepts the
  versions it was tested with.

## Files written { #files }

* An imported cloud is the dense cloud of its chunk, in the folder of the chunk
  (`<project>.ppg.files/chunk_<n>/point_cloud.ply`); a cloud replaced is kept in `versions/point_cloud/`.
* A reference raster: `versions/dem/<id>/dem.tif` or `versions/orthomosaic/<id>/orthomosaic.tif` in the
  folder of the chunk, a tiled and compressed GeoTIFF with overviews.
* The classes are written into the dense cloud; a cleaned or recoloured cloud keeps the one of before in
  `versions/point_cloud/`, a converted DEM or cloud is a new folder of `versions/dem/` or
  `versions/point_cloud/`.
* The values of the points are properties of the vertices of `point_cloud.ply`.
* The geoid and datum grids downloaded: `plugins/_data/terrio/grids/`; the clouds shown in the 3D view
  (the points found, the values in colours): `plugins/_data/terrio/preview/`.
* The project records the source of an import, the parameters and counts of the last classification
  and cleaning, the sources of the values of the points, and the source and heights of every version made
  by the plugin.
* terrio, when the plugin downloads it, goes into `plugins/_data/terrio/lib/` inside the plugin folder of
  the program (see [Plugins](../plugins.md)).
* The exports go where you choose; an EPT export is a folder.
