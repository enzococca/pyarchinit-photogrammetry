# terrio

The *terrio* plugin brings into the project what other instruments and other programs make, and
sends the project's products out in their formats. It is built on [terrio](https://github.com/opengeos/terrio),
a library of geospatial input and output written in Rust: nothing else - GDAL, PROJ, PDAL - has to be
installed. It imports point clouds of any common format, from a file or from the web; it separates the
ground from the rest of the dense cloud with the filters used for airborne lidar; it fetches a
reference raster - a regional DEM, an older orthophoto - over the survey; it exports the cloud for
laser-scanning software and the orthophoto as tiles for web maps.

This page describes version 0.1.2 of the plugin, which needs pyarchinit-photogrammetry 1.1.0 or later
and terrio 0.4 (0.3.2 and the 0.4 releases are accepted). Its README and source code are in the
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
   17 MB), or *Plugins > terrio > Install terrio...*.
2. *Import point cloud...*: a LAS, LAZ, COPC, EPT, PLY or E57 file, or a web address, into a new chunk
   or in place of the dense cloud of the active one.
3. *Classify the ground (SMRF, CSF, PMF)...*: the points without another class become ground (class 2) or
   the rest (class 1); then build a DEM
   of the *Ground* class: a terrain model.
4. *Reference raster from the web...*: a regional DEM or an orthophoto over the survey, kept as a
   version of the DEM or of the orthophoto, to compare with yours.
5. *File > Export*: the dense cloud as E57 or EPT, the orthophoto as PMTiles or MBTiles.

## What it does

| Step | What it gives | Useful for |
|---|---|---|
| Import point cloud | a cloud of LAS, LAZ, COPC, EPT, PLY or E57 - also zipped, also on the web - as the dense cloud of a new chunk or of the active one, with its colours (or its intensity as greys) and its classes | a laser scan, the lidar of a regional survey, the cloud of another program, beside the photogrammetry |
| Classify the ground | ground (class 2) and other points (class 1), low noise (7), vegetation by height (3, 4, 5); the points of other classes stay as they are | a DTM under trees and buildings, the ground for the [Terrain](terrain.md) and [Powerlines](powerlines.md) plugins |
| Reference raster | a raster on the web or on disk, read over the survey only, reprojected into the system of the chunk, kept as a version | comparing the survey with a regional DEM or an older orthophoto, the landscape around the site |
| Exports | E57 and EPT of the cloud (LAS, LAZ, COPC and PLY from the command line), PMTiles and MBTiles of the orthophoto | laser-scanning software, web viewers of point clouds, web maps, maps on a tablet in the field |

## What you need first { #before }

* **The plugin**, installed from the program: see [Plugin guides](guides.md#install).
* **terrio itself.** Installed with pip, the plugin brings it along. The desktop application has no pip:
  the first time a step needs terrio, the program asks whether to download it from PyPI (about 17 MB,
  its SHA-256 checked) into the plugin's own folder; after the download, start the step again.
  *Plugins > terrio > Install terrio...* does the same at any time, `ppg plugin terrio install` from the
  command line. terrio exists for macOS (Apple Silicon and Intel), Windows (x64 and ARM64) and Linux
  (x86-64 and ARM64).
* **A project.** An import into a new chunk needs nothing else; the classification and the export of
  the cloud need a chunk with a dense cloud; the tiles need an orthophoto; the reference raster needs a
  chunk with a reference system (*the chunk needs a reference system (align it with GPS or markers
  first)* otherwise).
* **Memory.** The ground filters work on the whole cloud at once: tens of millions of points need a few
  gigabytes.
* **Internet**, for the sources on the web.

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
* **Heights.** A national or global DEM may give heights above the geoid (sea level) while a drone
  survey with GNSS gives heights above the ellipsoid, or the other way round: the difference is the
  undulation of the geoid, tens of metres in many places (in a test a 30 m global DEM lay 37.6 m below
  the drone DEM). Compare the shapes, or correct the offset before comparing heights.

## Step by step in the program { #program }

The *Plugins > terrio* menu has *Import point cloud...*, *Classify the ground (SMRF, CSF, PMF)...*,
*Reference raster from the web...* and *Install terrio...*; *File > Export* has *Point cloud E57
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

### 3. Reference raster { #reference }

*Reference raster from the web...*

| Field | Default | What it does |
|---|---|---|
| *URL of the COG, STAC search (STACIT:...) or file* | | the source |
| *Keep it as a version of* | DEM | *DEM* or *Orthomosaic* |
| *Margin around the survey (m)* | 20 | how far beyond the survey the raster is read |
| *Resolution (m; empty: the source's)* | empty | the cell of the result |
| *Name of the version (empty: from the file)* | empty | `Reference: <file name>` when empty (`Riferimento: ...` when the program speaks Italian) |

The raster is read over the survey and the margin, reprojected into the system of the chunk (bilinear
for a DEM, cubic for an orthophoto) and kept as a version: the active DEM or orthophoto does not change.
A window gives the size (*Version "Reference: dem_regionale.tif": 60 x 60 pixels at 1 m.*). The
version is in the *Workspace* under the DEM (or the orthophoto): right click, *Show*; for a DEM also
*Difference with the active DEM*.

### 4. Exports { #exports }

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
ppg plugin terrio export scavo.ppg nuvola.e57
ppg plugin terrio export scavo.ppg nuvola.ept
ppg plugin terrio export scavo.ppg nuvola.copc.laz
ppg plugin terrio export scavo.ppg ortofoto.pmtiles
```

| Command | Options |
|---|---|
| `version` | the terrio in use (exit code 1 when there is none) |
| `install` | downloads terrio into the plugin's folder |
| `import PROJECT SOURCE` | a file or a web address; without `--chunk` a new chunk (`--label` its name) - the active chunk of the project stays, `--activate` makes the new one active; `--chunk NAME`: in place of the dense cloud of that chunk; `--max-points`; `--classes 2,6`; `--crs EPSG:...` |
| `ground PROJECT` | `--method` smrf, csf or pmf; `--cell` (1.0); `--slope` (the filter's: SMRF 0.15, PMF 1.0); `--threshold` (the filter's: SMRF and CSF 0.5, PMF 0.15 - its initial distance); `--window`; `--resolution` (1.0, CSF); `--rigidness` (3, CSF); `--noise`; `--vegetation` |
| `reference PROJECT URL` | `--product` dem or orthomosaic; `--margin` (20); `--resolution`; `--name` |
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
* **terrio is young.** Its functions may still change before its version 1.0; the plugin accepts the
  versions it was tested with.

## Files written { #files }

* An imported cloud is the dense cloud of its chunk, in the folder of the chunk
  (`<project>.ppg.files/chunk_<n>/point_cloud.ply`); a cloud replaced is kept in `versions/point_cloud/`.
* A reference raster: `versions/dem/<id>/dem.tif` or `versions/orthomosaic/<id>/orthomosaic.tif` in the
  folder of the chunk, a tiled and compressed GeoTIFF with overviews.
* The classes are written into the dense cloud.
* The project records the source of an import, the parameters and counts of the last classification
  and the source of every reference version.
* terrio, when the plugin downloads it, goes into `plugins/_data/terrio/lib/` inside the plugin folder of
  the program (see [Plugins](../plugins.md)).
* The exports go where you choose; an EPT export is a folder.
