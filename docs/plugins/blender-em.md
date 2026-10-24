# Blender and EMtools

The *Blender and EMtools* plugin takes the 3D proxies of the stratigraphic units and the
photogrammetric survey into [Blender](https://www.blender.org) and into the Extended Matrix of
[EMtools](https://github.com/zalmoxes-laran/EM-blender-tools), the Blender add-on of the Extended
Matrix (E. Demetrescu, CNR-ISPC), and keeps them up to date while the work goes on - as the QGIS link
keeps the drawings up to date in QGIS. The matrix stays where it is written: the em.json of the site,
exported by PyArchInit or written by EMStudio and EMtools. The plugin reads it, gives every proxy the
name of its unit in the matrix - the name EMtools pairs a proxy with - and writes the files where an
EM project keeps them.

This page describes version 0.1.0 of the plugin, which needs pyarchinit-photogrammetry 1.3.4 or later.
Its README and source code are in the
[plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/blender-em).

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
   units.
5. Go on working in the program - build a proxy again, take another one away, build the model again:
   Blender follows within a second or two.
6. In Blender, start the sync of EMtools (*Sync*) and tick *Sync with EMtools* in the pane: a proxy
   picked in Blender selects its unit in the pane, a unit picked in the pane selects and frames its
   proxy in Blender.

## What it does

| Step | What it gives | Useful for |
|---|---|---|
| Link the em.json | the units of the matrix with their epoch, and the proxy of the chunk paired with each; the proxies the matrix lacks, and the ambiguous ones, said | checking that every proxy has its unit before going into Blender |
| Export into the EM project | `EM/proxies/<unit>.glb` per proxy, named as its node; `RB/<chunk>/<chunk>.glb` the survey with its colours or texture; `ppg_scene.json` | EMtools, Heriverse, any glTF viewer; an EM project that holds its geometry outside Blender |
| Open in Blender | Blender on the EM project with the live script of the plugin; the em.json loaded into EMtools 1.6 | modelling the reconstruction (the virtual units) on the real proxies and on the survey; renders; the EM workflow |
| Live update | every proxy built again, every model changed written again, and reloaded by Blender | working in both programs at once without exporting by hand |
| Sync with EMtools | the selection shared with EMtools 1.6 | reading the form of a unit picked in 3D; finding in 3D the unit of the matrix |

What stays elsewhere: the matrix is edited in EMStudio, in EMtools or in PyArchInit's forms; the
reconstructions are modelled in Blender; the proxies are built by the program (*Tools > 3D proxies of
the units...*), as solids between two surfaces with their volume.

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
  works; with **1.5** (GraphML only) the files and the live script work the same, and the matrix is
  loaded in EMtools by hand.
* **A PyArchInit database connected** in the program - optional: it gives each proxy the `node_uuid`
  of its US form, the id PyArchInit gives the unit in the em.json, so the pairing does not depend on
  names.

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
<project>/ppg_scene.json            the manifest the live script follows
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
are not set yet).

**Files of other programs.** A file is written again only when what it comes from changed (the proxy,
the model, the shift, the name, the colour of the epoch), and **never over a file another program
wrote or changed** in the same place: EMtools writes the proxies modelled in Blender into
`EM/proxies` too. Such a file is left as it is and the report says so. A proxy taken out of the chunk
has its file removed only when it is still the one written by the plugin.

## Step by step in the program { #program }

The *Blender and EMtools* pane (*Plugins > Blender and EMtools > Blender and EMtools pane*, on the
right) shows the em.json linked, the EM project, Blender and the state of the sync, the buttons, and
the table of the units: unit, type, epoch, proxy (its volume). The *EM project (Blender)* item under
the chunk in the workspace opens the pane and has the same actions in its menu.

### 1. Link the em.json { #link }

*Link em.json...*: the `.em.json` of the site. The pane lists the units of the matrix with the proxy
paired with each, then the proxies the matrix does not have; a message names those. The link is kept
with the chunk (also relative to the project, so a project moved with its EM project finds it again),
and the em.json is read again whenever it changes - exported again from PyArchInit, saved by EMStudio.

### 2. Export into the EM project { #export }

*Export into the EM project* writes the proxies and the survey and reports what it wrote, what was
unchanged, what it left alone (files of another program), the proxies not in the matrix and the shift.
The survey is the active model of the chunk, with its texture when it has one.

### 3. Open in Blender { #blender }

*Open in Blender (kept up to date)* exports, then starts Blender on the project: the newest `.blend`
in the folder of the project when there is one, else a new scene (save it there, and it opens next
time). In Blender:

* the proxies are in the collection **PPG Proxy**, the survey in **PPG Rilievo**; each object keeps the
  unit, area, type, epoch, node id and volume as custom properties (*Object properties > Custom
  properties*);
* the **PPG** tab of the sidebar of the 3D view (key N) shows the project, the revision of the
  manifest, *Live: yes/no*, *Reload*, *Load the em.json into EMtools*, and the objects changed in
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
old one.

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

The program sends only the selection: it never sends edits of the graph or commands, which are
EMtools' and EMStudio's. When Blender closes and opens again, the program connects again by itself.

## Real time: how it works { #realtime }

The link with Blender works like the [QGIS link](../integrations.md#qgis-live): files the other program reads
again, plus a channel for what must be immediate.

| | QGIS link | Blender and EMtools |
|---|---|---|
| What travels | the drawings, in a GeoPackage | the proxies and the survey, in glTF files of the EM project |
| Who looks | QGIS redraws the layers every second | the script of the plugin in Blender reads the manifest every second |
| When the program writes | at every change of the drawings | when proxies, model or em.json change (every two seconds at most) |
| Immediate channel | - | the WebSocket of EMtools 1.6: the selection, both ways |
| What the other side changes | - | kept: detached objects, materials of EMtools, files written by EMtools |

## From the command line { #cli }

```bash
# the units of the em.json and the proxies paired with them
ppg plugin blender-em status scavo.ppg --emjson Scavo/EM/Scavo.em.json

# the EM project of the em.json (EM/proxies, RB, ppg_scene.json)
ppg plugin blender-em export scavo.ppg --emjson Scavo/EM/Scavo.em.json

# without an em.json, in a folder; the proxies only
ppg plugin blender-em export scavo.ppg --out Scavo --no-survey

# export and open Blender on it (the em.json linked to the chunk when none is given)
ppg plugin blender-em open scavo.ppg --blender /Applications/Blender.app/Contents/MacOS/Blender

# the Blender programs found
ppg plugin blender-em blender
```

`--chunk LABEL` works on another chunk than the active one. The script that runs in Blender can also
be started by hand: `blender --python blender_live.py -- ppg_scene.json` (the file is in the folder of
the plugin), or from Blender's panel, *Choose the manifest...*.

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
  To have it again, delete the object in Blender and click *Reload* in the PPG panel: the object is
  imported again.

### Limits { #limits }

* **The program does not read back what is modelled in Blender**: the reconstructions and the proxies
  modelled there stay in Blender and in EMtools' project.
* **One chunk**: the active one, with its proxies and its model.
* **The survey as one file**: a large model makes a large glTF; decimate it first (the *Mesh (MeshLab)*
  plugin) when Blender must stay light.
* **The sync needs EMtools 1.6** (a development version as of October 2026); EMtools 1.5 reads GraphML
  and has no sync: there, load the matrix by hand.

## Files written { #files }

* `EM/proxies/<unit>.glb` (or `proxies/` beside the em.json): one glTF binary per proxy, a node and a
  mesh named as the unit, a material in the colour of its epoch; in its extras the unit, area, type,
  node id, volume, CRS and shift.
* `RB/<chunk>/<chunk>.glb`: the survey, with its colours or its texture.
* `ppg_scene.json` in the folder of the project: the manifest (revision, CRS, shift, em.json, the
  objects with their files, sources and checksums), read by the script in Blender.
* The project keeps the em.json linked and the folder of the EM project with the chunk.
* The settings of the plugin (Blender, live update, sync, port) in its folder of the program (see
  [Plugins](../plugins.md)).
