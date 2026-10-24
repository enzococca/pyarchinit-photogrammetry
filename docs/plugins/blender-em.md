# Blender and EMtools

The *Blender and EMtools* plugin takes the 3D proxies of the stratigraphic units, the
photogrammetric survey and the aligned cameras with their photos into [Blender](https://www.blender.org)
and into the Extended Matrix of [EMtools](https://github.com/zalmoxes-laran/EM-blender-tools), the
Blender add-on of the Extended Matrix (E. Demetrescu, CNR-ISPC), and keeps them up to date while the
work goes on - as the QGIS link keeps the drawings up to date in QGIS. What is modelled in Blender
comes back into the program as the proxy of its unit, with its volume. The matrix stays where it is
written: the em.json of the site, exported by PyArchInit or written by EMStudio and EMtools. The
plugin reads it, gives every proxy the name of its unit in the matrix - the name EMtools pairs a proxy
with - and writes the files where an EM project keeps them.

This page describes version 0.1.2 of the plugin, which needs pyarchinit-photogrammetry 1.3.4 or later.
Its README and source code are in the
[plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/blender-em).

<figure markdown="span">
  ![The EM project in Blender: survey, proxies by epoch, cameras, a virtual unit modelled in Blender](../img/plugins/blender-em/em-project.jpg)
  <figcaption>The EM project written by the plugin, in Blender. (a) The survey of the trench with the
  proxies of the units and the 28 aligned cameras. (b) The proxies coloured by the epoch of their unit
  in the em.json: the fill dug away (US 2, seen through), the wall (USM 3), the two layers under the
  floor (US 4, US 5). (c) The elevation of the wall as it stood (USVs 101, a virtual unit), modelled in
  Blender on the stub of the wall, sent to the program and taken back as the proxy of its unit, with its
  volume. The volumes are those the program computed. A synthetic excavation, rendered in Blender 5.2
  (Cycles).</figcaption>
</figure>

!!! note "The example on this page"
    The figures come from the **synthetic** excavation of the program's tests: a textured surface with
    targets, a trench 0.35 m deep with the stub of a wall in it, 28 photos rendered with a radial
    distortion and aligned, and an em.json written as PyArchInit writes it, with invented units and
    epochs. They show what the plugin does and say nothing of a real site. The scenes were imported
    into Blender by the live script of the plugin; the figures were made with the Italian interface.

!!! note "Extended Matrix, EMtools, em.json"
    The [Extended Matrix](https://www.extendedmatrix.org) is the graph of the stratigraphy - units,
    relations, epochs, sources and reconstructions - of a site. Its working file is the **em.json**
    (from s3dgraphy 1.6): one file holding one or more graphs, read by EMStudio, EMtools 1.6 and
    Heriverse. GraphML (yEd) remains the way in for the matrices drawn by hand. In Blender a *proxy*
    is the solid of a unit, an object named as its node: EMtools colours it by epoch, shows and hides
    it with the matrix, and exports it for the web.

## In short

1. Build the proxies of the units: *Tools > 3D proxies of the units...* (the units drawn, or those of a
   PyArchInit site, between the DEM and a base).
2. In PyArchInit (QGIS), export the matrix of the site: *Extended Matrix → Esporta progetto EM per
   EMStudio…* writes `<site>.em.json` into the `EM` folder of an EM project, the tree the plugin
   writes into (*Extended Matrix → Esporta sito in em.json…* writes the file alone).
3. In the program, *Plugins > Blender and EMtools > Link the em.json of the site...*: the pane lists the
   units of the matrix and the proxy paired with each.
4. *Open in Blender (kept up to date)*: the proxies go into `EM/proxies`, the survey into `RB`, and
   Blender opens with them; with EMtools 1.6 the em.json is loaded and the proxies are paired with the
   units. Tick *Cameras and photos* first to have the aligned cameras too, each with its photo.
5. Go on working in the program - build a proxy again, take another one away, build the model again:
   Blender follows within a second or two.
6. In Blender, start the sync of EMtools (*Sync*) and tick *Sync with EMtools* in the pane: a proxy
   picked in Blender selects its unit in the pane, a unit picked in the pane selects and frames its
   proxy in Blender.
7. Model in Blender what the program cannot build from the DEMs - a reconstruction, a unit that is no
   longer there - and send it back with *Invia al programma* (the *PPG* panel in Blender): it comes into
   the chunk as the proxy of its unit, with its volume, area and elevations.

## What it does

| Step | What it gives | Useful for |
|---|---|---|
| Link the em.json | the units of the matrix with their epoch, and the proxy of the chunk paired with each; the proxies the matrix lacks, and the ambiguous ones, said | checking that every proxy has its unit before going into Blender |
| Export into the EM project | `EM/proxies/<unit>.glb` per proxy, named as its node; `RB/<chunk>/<chunk>.glb` the survey with its colours or texture; `ppg_scene.json` | EMtools, Heriverse, any glTF viewer; an EM project that holds its geometry outside Blender |
| Open in Blender | Blender on the EM project with the live script of the plugin; the em.json loaded into EMtools 1.6 | modelling the reconstruction (the virtual units) on the real proxies and on the survey; renders; the EM workflow |
| Cameras and photos | the aligned cameras as Blender cameras, each with its photo undistorted behind it | modelling where the photos show it; checking a proxy against the photo; renders from the point of view of a photo |
| Live update | every proxy built again, every model changed written again, and reloaded by Blender | working in both programs at once without exporting by hand |
| Sync with EMtools | the selection shared with EMtools 1.6 | reading the form of a unit picked in 3D; finding in 3D the unit of the matrix |
| Proxy in Blender | EMtools makes the proxy of a unit without one, a box bound to its node, where the unit is drawn | starting the model of a unit that has no proxy in the program |
| Import from Blender | the objects sent from Blender, into the chunk as the proxies of their units, with volume, area and elevations | the measures of what was modelled in Blender, in the program's tables, sections and in PyArchInit |

What stays elsewhere: the matrix is edited in EMStudio, in EMtools or in PyArchInit's forms; the
reconstructions are modelled in Blender; the proxies of the units dug are built by the program
(*Tools > 3D proxies of the units...*), as solids between two surfaces with their volume.

## What you need first { #before }

* **The plugin**, installed from the program: see [Plugin guides](guides.md#install).
* **Proxies of the units in the chunk**, made with *Tools > 3D proxies of the units...* (see
  [Desktop interface](../gui.md)), and/or **a model**. Without either there is nothing to send.
* **The em.json of the site** - recommended. PyArchInit writes it (*Extended Matrix → Esporta sito in em.json…*, s3dgraphy 1.6),
  as EMStudio and EMtools 1.6 do. Without it the proxies keep the program's names (`US12`, `USM3`) and
  EMtools pairs them only if its matrix uses those names.
* **Blender** 4.2 or later (tested with 5.2), found where it is installed, or chosen with *Choose the
  Blender program...* (or the variable `PPG_BLENDER`).
* **EMtools** in Blender - optional. With **1.6** the em.json is loaded by itself and the live sync
  works; with **1.5** (GraphML only) the files, the cameras, the live script and the way back work the
  same, and the matrix is loaded in EMtools by hand.
* **A PyArchInit database connected** in the program - optional: it gives each proxy the `node_uuid`
  of its US form, the id PyArchInit gives the unit in the em.json, so the pairing does not depend on
  names.
* **For the cameras**, the aligned cameras of the chunk with their photos where the project says they
  are.

## The data { #data }

### The em.json { #emjson }

The plugin reads the em.json, it never writes it. Of the file it uses:

* the **units** (the stratigraphic nodes: US, USVs, USVn, SF, VSF, USD, TSU, series...): their id,
  their name - PyArchInit writes `area.typeNUMBER`, `1.US12` - and the US, area and type of the form
  PyArchInit keeps with them;
* their **epoch** (the first one), whose colour becomes the colour of the proxy;
* the **shift** of the project (its georeferencing node), when it has one.

A file with several graphs - PyArchInit writes one per *saggio* when asked - is read whole; the
shelf of assets and the documentation corpus are left out. A file in the shape before the container
(one graph) is read as well.

### Pairing the proxies with the units { #pairing }

Each proxy of the chunk is paired with one unit, in this order:

1. by the **`node_uuid` of its US form**, when a PyArchInit database is connected: the id of the unit
   in PyArchInit's em.json;
2. by the **form**: the same US number and type (`USM` is not `US`), and the same area when both have
   one;
3. by the **name**: `US12`, `1.US12`, or a node name ending in `.US12`.

A unit goes to one proxy only. Two possible units for a proxy are not a guess: the pane lists the
candidates, and the proxy keeps the program's name until the area of the proxy or of the forms says
which one. Proxies without a unit are written with the program's name and marked *not in the matrix*.

### The EM project { #project }

```
<project>/EM/<site>.em.json         the matrix (PyArchInit's, EMStudio's)
<project>/EM/proxies/<unit>.glb     a proxy per unit, named as its node
<project>/RB/<chunk>/<chunk>.glb    the survey (reality-based)
<project>/RB/<chunk>/foto/          the photos undistorted for the cameras in Blender
<project>/ppg_scene.json            the manifest the live script follows
<project>/ppg_inbox/                what Blender sends back, until the program takes it
```

This is the standard tree of an EM project (s3dgraphy, EMtools 1.6). When the em.json sits in the
`EM` folder of a project, the files go into that project; an em.json elsewhere has them beside it
(`proxies/`, `RB/`), as EMtools does. Without an em.json the plugin asks for the folder of the project
once and keeps it with the chunk.

**Coordinates.** glTF has Y up; Blender turns it Z up on import. The coordinates are **shifted by whole
metres**, because a float32 keeps a quarter of a metre at four thousand kilometres: by the shift of the
em.json when it has one, else by the lowest corner of what is written, rounded down (for example
`300000, 4500000, 99`). Once chosen, the shift stays for the project, so an update never moves what
Blender shows; the scene keeps it (`ppg_shift`, and the `BL_x_shift`... that EMtools reads, when they
are not set yet). What comes back from Blender is shifted back into the coordinates of the chunk.

**Files of other programs.** A file is written again only when what it comes from changed (the proxy,
the model, the shift, the name, the colour of the epoch), and **never over a file another program
wrote or changed** in the same place: EMtools writes the proxies modelled in Blender into
`EM/proxies` too. Such a file is left as it is and the report says so. A proxy taken out of the chunk
has its file removed only when it is still the one written by the plugin.

## Step by step in the program { #program }

The *Blender and EMtools* pane (*Plugins > Blender and EMtools > Blender and EMtools pane*, on the
right) shows the em.json linked, the EM project, Blender and the state of the sync, the buttons, the
three switches (*Live update*, *Sync with EMtools*, *Cameras and photos*) and the table of the units:
unit, type, epoch, proxy (its volume). The *EM project (Blender)* item under the chunk in the
workspace opens the pane and has the same actions in its menu.

<figure markdown="span">
  ![The Blender and EMtools pane with the sync on and a unit picked in Blender](../img/plugins/blender-em/pane.png)
  <figcaption>The pane with the em.json of the site linked (5 units, 3 epochs), the live update, the sync
  and the cameras on. The wall, picked in Blender, is selected in the table; under it, its description in
  the matrix and the measures of its proxy. USVs 101 has the proxy modelled in Blender. Here the sync
  talks to a test host that speaks the wire of EMtools 1.6.</figcaption>
</figure>

### 1. Link the em.json { #link }

*Link em.json...*: the `.em.json` of the site. The pane lists the units of the matrix with the proxy
paired with each, then the proxies the matrix does not have; a message names those. The link is kept
with the chunk (also relative to the project, so a project moved with its EM project finds it again),
and the em.json is read again whenever it changes - exported again from PyArchInit, saved by EMStudio.

### 2. Export into the EM project { #export }

*Export into the EM project* writes the proxies and the survey and reports what it wrote, what was
unchanged, what it left alone (files of another program), the proxies not in the matrix, the photos
undistorted and the shift. The survey is the active model of the chunk, with its texture when it has
one.

### 3. Open in Blender { #blender }

*Open in Blender (kept up to date)* exports, then starts Blender on the project: the newest `.blend`
in the folder of the project when there is one, else a new scene (save it there, and it opens next
time). In Blender:

* the proxies are in the collection **PPG Proxy**, the survey in **PPG Rilievo**, the cameras in **PPG
  Camere**; each object keeps the unit, area, type, epoch, node id and volume as custom properties
  (*Object properties > Custom properties*);
* the **PPG** tab of the sidebar of the 3D view (key N) shows the project, the revision of the
  manifest, *Dal vivo: sì/no* (live: yes/no), the reload button, *Invia al programma* (send to the
  program), *Carica l'em.json in EMtools* (load the em.json into EMtools), and the objects changed in
  Blender;
* with EMtools 1.6, on a new scene, the em.json is loaded into EMtools by itself; the proxies are
  paired with the units by name.

**What you change in Blender is never overwritten.** An object whose vertices are no longer those the
plugin imported - a proxy modelled by hand - is *detached*: the program's new version does not replace
it, and the panel lists it. A material given by EMtools (the colour of the epoch) stays when the mesh
of a proxy is replaced.

### 4. Live update { #live }

*Live update* (also turned on by *Open in Blender*): every two seconds the program looks at the
proxies, the model and the em.json of the active chunk; when something changed it writes the EM
project again in the background - without a progress window, and not while a processing step runs -
and Blender, which reads the manifest every second, reloads what changed. A proxy built again replaces
the mesh of its object, a proxy taken away disappears, the survey of a model built again replaces the
old one. What Blender sent back is taken into the chunk at the same time (see [below](#take)).

### 5. Sync with EMtools { #sync }

EMtools 1.6 *hosts* the live sync of the Extended Matrix, the one EMStudio uses: in Blender, start it
from the panel of EMtools (*Sync*). Then tick **Sync with EMtools** in the pane. The program connects
to Blender (`127.0.0.1`, port 8788; another port with the setting `port` of the plugin), says which
matrix it has open - EMtools warns when the two are not on the same study - and asks for EMtools'
graph: without an em.json linked, the pane and the names of the proxies follow that graph.

* **A proxy picked in Blender** selects its unit in the table of the pane, with its type, epoch and the
  measures of its proxy (volume, area, elevations) under the table.
* **A unit picked in the pane** selects and frames its proxy in Blender. EMtools must accept the
  selection (its default: *Accept: everything* or *selection*).

The program sends the selection and, when asked with *Proxy in Blender* (below), EMtools' command to
make a proxy; it never sends edits of the graph, which are EMtools' and EMStudio's. When Blender
closes and opens again, the program connects again by itself.

### 6. Cameras and photos { #cameras }

With **Cameras and photos** ticked, the next export (and *Open in Blender*) also writes the aligned
cameras of the chunk:

* each **photo undistorted** with the calibration of its sensor - every camera model of the program,
  the fisheye ones too - and made smaller, 2048 pixels on the long side, into `RB/<chunk>/foto/`;
  Blender's camera has no lens distortion, so the photo must lose it first;
* each **camera** in Blender, in the collection *PPG Camere*: its pose in the coordinates of the
  project, its focal length and principal point on the frame of the photo, and the undistorted photo
  as its background image. The size of the render is set to the frame of the photos, once (a size set
  by hand afterwards is kept).

Look through a camera in Blender (select it, *View > Cameras > Set Active Object as Camera*, Ctrl
Numpad 0): the survey and the proxies lie on the photo, and a reconstruction can be modelled where the
photo shows it. A photo is written again only when the photo, its calibration or the size change;
unticking the switch takes the cameras and their photos away at the next export. Photos the project
cannot find are named in the report, and their cameras left out.

<figure markdown="span">
  ![A photo as taken, then the proxies through its camera in Blender, on the undistorted photo](../img/plugins/blender-em/camera-photo.jpg)
  <figcaption>(a) A photo of the trench as it was taken, with the distortion of its lens. (b) The same
  photo undistorted by the plugin, behind its camera in Blender: the proxies seen through the camera lie
  where the photo shows the units - the fill dug away (pink) on the empty trench, the wall on its stub.
  (c) Another camera, a nearly vertical photo. The survey hides what lies under the ground, as in the
  photo. Synthetic photos, rendered in Blender 5.2.</figcaption>
</figure>

### 7. From Blender back into the program { #take }

What is modelled in Blender comes back into the chunk as the proxy of its unit:

1. In Blender, model the solid of the unit - a reconstruction, a virtual unit (USVs, USVn), a unit
   dug away and never surveyed - and give the object the name of its unit (`1.USVs101`, `USVs101`), or
   start from an object with its node (a proxy of the program, or one EMtools made, below).
2. Select it and click **Invia al programma** (send to the program) in the *PPG* panel; without a
   selection, the objects changed in Blender are sent. Each goes into `ppg_inbox` beside the manifest,
   as glTF, with the id of its node.
3. In the program, **Import from Blender** (the pane, or *Plugins > Blender and EMtools > Import the
   proxies from Blender*); with *Live update* on, this happens by itself within two seconds.

Each object becomes a proxy of the chunk, as *Tools > 3D proxies of the units...* makes them: a PLY in
the coordinates of the chunk, beside the others, with

* the **unit**: by the id of its node, else by its name (also with the code of the graph EMtools puts
  before it, `SCAVO.1.US12`);
* the **volume**, when the solid is closed: small holes are closed first; an open mesh - a surface, a
  sheet - has no volume, and the report says so;
* the **area in plan** and the **elevations** of its top and of its bottom (the faces looking up and
  down).

A proxy of the chunk with the same name is replaced: the proxy built from the DEMs gives way to the
one modelled. The proxies pane of the window, the table of this pane and the EM project follow; the
object stays in Blender, now as the program's proxy. A proxy already published in PyArchInit is named
in the report, *to publish again in PyArchInit*: its geometry changed.

### 8. Proxy in Blender { #emtools-proxy }

For a unit of the matrix the program has no proxy of, EMtools can start one. With the sync on, pick the
units in the table and click **Proxy in Blender**: the program sends EMtools its command
`create_proxy_for_unit`, and EMtools makes, for each, a box bound to the node of the unit. When the
unit is drawn in the chunk, the box is as large as the unit drawn, in its middle, with its top on the
DEM; else EMtools puts it at the 3D cursor. Model it, then send it back with *Invia al programma*.

The button is active while the sync is connected, unless EMtools says it refuses commands: in Blender,
tick **Accept commands from EMStudio** in the sync panel of EMtools (it is off by default). A unit that already has a proxy in
Blender is said, and left alone.

## Real time: how it works { #realtime }

The link with Blender works like the [QGIS link](../integrations.md#qgis-live): files the other program reads
again, plus a channel for what must be immediate.

| | QGIS link | Blender and EMtools |
|---|---|---|
| What travels | the drawings, in a GeoPackage | the proxies, the survey and the cameras, in glTF files and photos of the EM project |
| Who looks | QGIS redraws the layers every second | the script of the plugin in Blender reads the manifest every second |
| When the program writes | at every change of the drawings | when proxies, model or em.json change (every two seconds at most) |
| What comes back | the drawings made in QGIS | the objects sent with *Invia al programma*, taken as proxies with their volume |
| Immediate channel | - | the WebSocket of EMtools 1.6: the selection, both ways; the command that makes a proxy |
| What the other side changes | - | kept: detached objects, materials of EMtools, files written by EMtools |

## From the command line { #cli }

```bash
# the units of the em.json and the proxies paired with them
ppg plugin blender-em status scavo.ppg --emjson Scavo/EM/Scavo.em.json

# the EM project of the em.json (EM/proxies, RB, ppg_scene.json)
ppg plugin blender-em export scavo.ppg --emjson Scavo/EM/Scavo.em.json

# with the cameras and their photos undistorted, 3000 pixels on the long side
ppg plugin blender-em export scavo.ppg --cameras --photo-side 3000

# without an em.json, in a folder; the proxies only
ppg plugin blender-em export scavo.ppg --out Scavo --no-survey

# export and open Blender on it (the em.json linked to the chunk when none is given)
ppg plugin blender-em open scavo.ppg --blender /Applications/Blender.app/Contents/MacOS/Blender

# the proxies sent from Blender into the chunk, and the project saved
ppg plugin blender-em take scavo.ppg

# the Blender programs found
ppg plugin blender-em blender
```

`--chunk LABEL` works on another chunk than the active one. Without `--cameras`, an export keeps the
cameras the EM project already has. The script that runs in Blender can also be started by hand:
`blender --python blender_live.py -- ppg_scene.json` (the file is in the folder of the plugin), or from
Blender's panel, *Scegli il manifest...* (choose the manifest).

## Reading the results { #reading }

* **Not in the matrix.** A proxy without a unit keeps the program's name: either the unit is missing
  from the em.json (export the em.json again from PyArchInit after creating its form), or its number,
  type or area differ.
* **Several possible units.** The same US number in two areas, and a proxy without an area: give the
  area to the unit drawn (or build the proxies from the PyArchInit site), or connect the database for
  the `node_uuid`.
* **Left as they were.** A file of the same name that EMtools wrote: delete it in the EM project if
  the program's proxy must replace it.
* **Detached in Blender.** The proxy was modelled in Blender: the program's version is not applied.
  To have it again, delete the object in Blender and click the reload button in the PPG panel: the
  object is imported again. To have the modelled one in the program instead, send it with *Invia al
  programma*.
* **Not closed, no volume.** An object sent from Blender with holes the program cannot close, or a
  surface: it comes in with its area and elevations, and a volume of 0. Close it in Blender (*Mesh >
  Clean Up > Fill Holes*, or a solid modelled closed) and send it again.
* **To publish again in PyArchInit.** A proxy published in PyArchInit was replaced by the one from
  Blender: publish it again for the media of the US form to show the new geometry.
* **Photos not found.** The project does not find those photos where it says they are: their cameras
  are left out.

### Limits { #limits }

* **What comes back is the solid**: a proxy with its measures. Materials, textures, modifiers not
  applied and the relations of the matrix stay in Blender and in EMtools' project; the matrix is
  edited in EMStudio, EMtools or PyArchInit.
* **One chunk**: the active one, with its proxies, its model and its cameras.
* **The survey as one file**: a large model makes a large glTF; decimate it first (the *Mesh (MeshLab)*
  plugin) when Blender must stay light.
* **The photos of the cameras** are copies, undistorted and smaller (2048 pixels by default, `--photo-side`
  from the command line): the originals stay where they are.
* **The sync needs EMtools 1.6** (a development version as of October 2026); EMtools 1.5 reads GraphML
  and has no sync, nor commands: there, load the matrix by hand. *Proxy in Blender* also needs
  EMtools to accept commands.
* **Proxies in map coordinates built with the program up to 1.3.4** were saved in single precision:
  with millions of metres in the northing, their vertices stand on a grid of a quarter to half a metre,
  in Blender as in the program. Build them again with a later version (which saves them in double
  precision, as this plugin saves what comes back from Blender).

## Files written { #files }

* `EM/proxies/<unit>.glb` (or `proxies/` beside the em.json): one glTF binary per proxy, a node and a
  mesh named as the unit, a material in the colour of its epoch; in its extras the unit, area, type,
  node id, volume, CRS and shift.
* `RB/<chunk>/<chunk>.glb`: the survey, with its colours or its texture.
* `RB/<chunk>/foto/<photo>.jpg`: the photos of the cameras, undistorted (with *Cameras and photos*).
* `ppg_scene.json` in the folder of the project: the manifest (revision, CRS, shift, em.json, the
  objects with their files, sources and checksums, the cameras with their pose and calibration), read
  by the script in Blender.
* `ppg_inbox/<object>.glb` and `.json`: what Blender sent, until the program takes it (then removed).
* In the chunk, `proxies/<unit>.ply` for each proxy taken from Blender, in double precision, and its
  entry among the proxies (base *blender*, the name of the object in Blender).
* The project keeps the em.json linked and the folder of the EM project with the chunk.
* The settings of the plugin (Blender, live update, sync, cameras, port) in its folder of the program
  (see [Plugins](../plugins.md)).
