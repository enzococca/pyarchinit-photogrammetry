# Mesh (MeshLab)

The *Mesh (MeshLab)* plugin applies the filters of [MeshLab](https://www.meshlab.net) to the 3D model
of the chunk: it reduces the number of triangles and keeps the texture, rebuilds the surface with
regular triangles, smooths it without shrinking it, closes its holes and leaves its outline open,
repairs its topology and measures it - area, volume of a closed model, separate parts, borders,
defects. Every result is a new version of the model: the one before stays among the versions of the
chunk, to compare the two or to go back. MeshLab works through
[pymeshlab](https://github.com/cnr-isti-vclab/PyMeshLab), by the Visual Computing Lab of ISTI-CNR, in
a process of its own: a filter that fails, or makes MeshLab crash, leaves the model as it was.

This page describes version 0.1.0 of the plugin, which needs pyarchinit-photogrammetry 1.3.3 or later.
Its README and source code are in the
[plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/mesh-tools).

<figure markdown="span">
  ![A decimation that keeps the texture](../img/plugins/mesh-tools/decimation.jpg)
  <figcaption>(a) The model of the tutorial as the program built it: 458,089 faces, with the wireframe
  over the texture. (b) The same model decimated by MeshLab to 100,000 faces with the texture kept:
  the triangles stay where the surface bends - the trench, the pits, the edge of the model - and
  become large on the flat ground; the texture is the same image as before. A trench on Tell Sultan,
  rendered from the two versions of the model.</figcaption>
</figure>

!!! note "The examples on this page"
    The examples come from the model of Tell Sultan - the site of the [tutorial](../tutorials/tell.md):
    57 drone photos, 458,089 faces, a texture of 4,096 x 3,378 pixels, in a local reference system. The
    times were measured on a recent computer (an Apple M4 Pro). They show what the plugin does and are
    not a study of the site.

## In short

1. Build the model of the chunk (*Workflow > Build model...*) and, if you want it, its texture
   (*Workflow > Build texture...*); or bring in a model made elsewhere (*File > Import > 3D model
   (OBJ, PLY, GLB)...*).
2. Open the *Mesh (MeshLab)* pane (*Plugins > Mesh (MeshLab) > Mesh (MeshLab)
   pane*). The first time an operation runs, the plugin offers to install MeshLab (about 300 MB),
   unless pymeshlab is already in the Python of the program, and tries it on a triangle.
3. *Measure*: closed or open, how many separate parts, how many defects.
4. Choose an operation - *Decimate...*, *Remesh...*, *Smooth...*, *Close holes...*, *Repair the
   topology...* - and set it in its window. The new version shows in the 3D view, its measures in the
   pane.
5. Compare it with the version before in the *Workspace* (double click), keep the one you want active,
   export it as usual from *File > Export*.

## What it does

| Operation | What it gives | Texture | Useful for |
|---|---|---|---|
| Decimate | fewer triangles, down to a number or a share of them; the shape stays, the borders stay where they are | kept | a light model for the web, a viewer, a 3D PDF or a presentation; faster views and exports |
| Remesh | a new surface of regular triangles of one size (isotropic remeshing), sharp edges kept | to build again | a clean mesh for 3D modelling, printing or analysis; triangles of even size over the whole model |
| Smooth | the surface smoothed by Taubin (it does not shrink) or HC Laplacian smoothing, the borders fixed | kept | the noise of photogrammetry on smooth surfaces: plaster, pottery, worked stone |
| Close holes | the holes up to a number of edges closed with new faces; the outline of each part left open | to build again, when holes were closed | small gaps left by shadows, vegetation or reflections; on request, the bottom of an object |
| Repair the topology | duplicate vertices and faces, faces without area, non-manifold edges and vertices, unused vertices removed or split | kept | before remeshing or closing holes; for programs that refuse a defective mesh (3D printing, some viewers) |
| Measure | surface area, volume of a closed model, closed or not, 2-manifold or not, separate parts, borders, defects, size, mean edge | - | the area of a wall or of a section, the volume of a closed find, a check before printing or exporting |

What the program already does stays the program's: the mesh from the dense cloud or from the depth
maps (*Workflow > Build model...*), the cleaning of the model by confidence, detached pieces and
stretched triangles (*Workflow > Cleaning and confidence > Clean the model...*), the cut with a plane
(*Workflow > Cleaning and confidence > Cut the model with a plane (parts, slices)...*), the texture
(*Workflow > Build texture...*) and the volumes of an excavation from the DEM (*Tools > Cut / fill
volume...*). The plugin adds what MeshLab does better.

## What you need first { #before }

* **The plugin**, installed from the program: see [Plugin guides](guides.md#install).
* **MeshLab, through pymeshlab.** The program never loads it - pymeshlab brings its own libraries,
  which would clash with those of the window, and some of its filters crash on a model they do not
  accept - but runs it as a separate program, with:
    * the Python of the program, when pymeshlab is installed there (`pip install pymeshlab` in the
      Python environment of the program);
    * otherwise an environment of the plugin: Python 3.12 and pymeshlab from PyPI, installed with
      [uv](https://github.com/astral-sh/uv) - about 300 MB and a few minutes, with the internet that
      one time. This is how the desktop application works, since it has no pip. The plugin offers it
      the first time an operation runs; *Install MeshLab...* in the pane, *Plugins > Mesh (MeshLab) >
      Install MeshLab (pymeshlab)...* or `ppg plugin mesh-tools install` do it beforehand. The
      environment goes into `plugins/_data/mesh-tools/env/` inside the plugin folder of the program
      (see [Plugins](../plugins.md)); the variable `PPG_MESH_TOOLS_ENV` puts it elsewhere.

    pymeshlab has builds for macOS (Apple Silicon and Intel), Linux (x86-64 and ARM) and Windows on
    x86-64; there is none for Windows on ARM, where the plugin says that the MeshLab tools are not
    available. The line under the description of the pane says which MeshLab the plugin uses.

* **On Linux, a few system libraries.** pymeshlab needs libraries its packages do not carry,
  `libOpenGL.so.0` above all: without them it starts but reads no model, and every operation would
  fail. Install them once - on Debian and Ubuntu `sudo apt install libopengl0 libgl1`, on Fedora
  `sudo dnf install libglvnd-opengl libglvnd-glx`.

* **A chunk with a model**: built by the program, or imported. The plugin works on the model the chunk
  shows, its active version.
* **A chunk in metres** - a reference system, scale bars, or a scan - for lengths and measures in
  metres. In a chunk without a scale they are in the units of the model, and the pane says so.

## The model { #model }

* **Seams.** A textured model repeats its vertices along the seams of the texture. Before MeshLab sees
  the model, the copies along a seam are counted once: otherwise MeshLab would take every seam for a
  border, smoothing would open it and the measures would count it as a hole. Copies that touch only at
  a point - a non-manifold vertex the repair split, two pieces meeting at a corner - stay apart, so
  the next operation does not undo a repair.
* **What MeshLab cannot carry.** MeshLab gets the positions, the colours of the vertices and, when the
  operation keeps it, the texture. The other properties of the vertices - the confidence, how many
  depth maps confirm the vertex - come back from the vertex of the model nearest to each new vertex;
  the colours of new vertices are MeshLab's, interpolated.
* **The texture.** It stays valid after a decimation (MeshLab's variant that carries the texture
  coordinates of the corners of the faces, also with several texture pages), after smoothing and after
  the repair. Remeshing and closing holes make new faces that have none: the result has the colours of
  its vertices, and the texture is built again with *Workflow > Build texture...*.
* **Nothing changed, no version.** When MeshLab finds nothing to do - no hole that small, no defect -
  the model stays as it is, no version is added and a message says why.
* **Versions and names.** The new version is named after what MeshLab did, for example *MeshLab:
  decimazione a 100.000 facce (texture conservata)*, or with the name you give it. Like the names the
  program gives to its own versions, these are kept in Italian in the project, whatever the language
  of the interface. The project is saved with the new version, as the program does after its own
  processing steps, and records every operation with its parameters.

## Step by step in the program { #program }

### The pane

Once the plugin is installed and the program restarted, the *Mesh (MeshLab)* pane opens on the right of the window, tabbed with *PyArchInit*; it can be moved, floated or closed like the other panes and opened again from *Vista > Pannelli*, or with
*Plugins > Mesh (MeshLab) > Mesh (MeshLab) pane*.

<figure markdown="span">
  ![The Mesh (MeshLab) pane after a decimation](../img/plugins/mesh-tools/pane.png)
  <figcaption>The pane after the decimation of the tutorial model to 100,000 faces, named <em>For the
  web</em> in its window: MeshLab runs with the pymeshlab of the Python of the program, the measures
  of the new version are in the table, the last line says what MeshLab did and the defects of the
  model it left. On the right, the new version with its texture in the 3D view.</figcaption>
</figure>

From the top:

* a short description;
* which MeshLab the plugin uses - *pymeshlab ... of this Python* or *in the environment of the
  plugin* - or that it is not installed, with the button *Install MeshLab...*, or what it lacks when
  the check below found it not working;
* the active model: its name, faces, vertices and texture pages;
* the buttons *Decimate...*, *Remesh...*, *Smooth...*, *Close holes...*, *Repair the topology...*,
  *Measure* and *Show in the 3D view*;
* the table of the measures, of the active model after *Measure*, or of the new version after an
  operation (*Measures after 'Decimation'*);
* at the bottom, what the last operation did.

The *Plugins > Mesh (MeshLab)* menu has the same operations - *Decimate the model...*, *Remesh the model
(isotropic)...*, *Smooth the model...*, *Close the holes of the model...*, *Repair the topology of the
model...*, *Measure the model* - with *Mesh (MeshLab) pane* and *Install MeshLab (pymeshlab)...*. The
buttons wait while the program is processing something else: one step at a time. The pane follows
the active chunk and its active model: another project, another chunk, a version made active, an
undo.

Every operation opens its window, with the parameters described below and two more fields at the
end:

| Field | What it does |
|---|---|
| *Current model* | *Keep it among the versions (...)* (the default) or *Overwrite it* |
| *Name of the new version (optional)* | empty: the name says what MeshLab did |

The first operation of a session starts with a check, in the background (*Trying MeshLab*): MeshLab
reads a triangle and lists the filters the plugin uses. When it works the operation follows, and the
check is not repeated in that session. When it does not, the operation stops there and the message,
and the line of the pane, say what is missing: on Linux the libraries and the packages that have
them, elsewhere to install MeshLab again. A check that failed is done again at the next operation:
after installing what was missing, just try again.

The work runs in the background with the progress bar - reading the model, the filter, the measures,
writing the result. *Cancel* stops MeshLab at once and the model stays as it was. At the end the new
version is the active model, the 3D view shows it, the pane gives its measures and the status bar
the summary, for example *Model decimated: 458,089 → 99,999 faces; 2,636 non-manifold vertices and 0
edges left ('Repair the topology'); texture kept*. The middle part appears when MeshLab leaves
non-manifold vertices or edges: here they are the defects of the model as built, which the decimation
does not touch.

### 1. Measure { #measure }

*Measure* asks MeshLab for the measures of the active model and changes nothing. Every operation
measures its result too.

| Measure | What it is |
|---|---|
| *Faces*, *Vertices* | how many; the vertices at the seams of the texture counted once |
| *Surface area* | in m², the sum of the areas of the triangles |
| *Volume* | in m³, only for a closed model; *(normals pointing inwards)* when the faces face inwards |
| *Closed (watertight)* | yes when the model is 2-manifold and has no border |
| *2-manifold* | yes when every edge belongs to at most two faces and the faces around every vertex form a single fan |
| *Separate parts* | the pieces not joined to one another |
| *Borders (loops, the outline included)* | the closed lines of open edges: the outline of each part and its holes; *–* when the model is not 2-manifold, as MeshLab cannot count them then |
| *Border edges* | the edges that belong to one face only |
| *Non-manifold edges*, *Non-manifold vertices* | the defects: an edge shared by three or more faces, a vertex where two fans of faces touch |
| *Unused vertices* | vertices no face uses |
| *Size (x × y × z)* | the box around the model, in the coordinates of the chunk |
| *Mean edge of the triangles* | the mean length of the sides of the triangles |

The tutorial model, in under two seconds: 458,089 faces, 250,084 vertices, 87,402 m², not closed and not
2-manifold, 925 separate parts - the main surface and many small pieces in the fields around the
mound - 57,981 border edges, 2,636 non-manifold vertices, 434.64 × 324.68 × 24.42 m, a mean edge of
0.70 m. A model of open ground built from photos is rarely closed: the volume is for finds and
objects photographed all round. For the volume of an excavation, use *Tools > Cut / fill volume...*
on the DEM.

### 2. Decimate { #decimate }

*Decimate...* reduces the faces by collapsing edges where the shape changes least (MeshLab's quadric
edge collapse): flat ground loses its triangles first, edges and bends keep theirs.

<figure markdown="span">
  ![The decimation window](../img/plugins/mesh-tools/dialog-decimate.png)
  <figcaption>The window of the decimation, set for 100,000 faces and a new version named <em>For the
  web</em>.</figcaption>
</figure>

| Field | Default | What it does |
|---|---|---|
| *Faces to reach (0 = the fraction below)* | 0 | the number of faces of the result |
| *Fraction of the faces to keep* | 0.5 | used when the number is 0: 0.25 keeps a quarter of the faces |
| *Keep the texture (MeshLab's variant with the texture coordinates)* | ticked (textured models only) | MeshLab moves the texture coordinates with the vertices, so the texture stays valid |
| *Keep the borders* | ticked | MeshLab leaves the open edges of the surface - the outline of a survey, the edge of a section - as they are |
| *Keep the topology (without texture: no hole closed or opened)* | ticked | for the variant without texture: the decimation neither closes nor opens holes |
| *Quality of the triangles (0-1: higher, more regular triangles)* | 0.3 | higher values avoid long thin triangles, at the price of a little shape |
| *Planar simplification (flat areas with better triangles)* | not ticked | better triangles on flat areas: floors, walls, the faces of a block |
| *Do not flip faces* | not ticked | refuses collapses that would turn a face over |

The tutorial model went from 458,089 to 99,999 faces in about 5 s, with its texture: MeshLab can stop
a face or two short of the number asked. The area went from 87,402 to 86,585 m² (-0.9 %, the small
bumps flattened), the border edges stayed the same and the mean edge grew from 0.70 to 1.05 m. A
model meant for measuring should stay the full one: keep the version before, and decimate the copy
you give away.

### 3. Remesh { #remesh }

*Remesh...* rebuilds the surface with triangles of one size (MeshLab's isotropic explicit remeshing):
it splits the long edges, collapses the short ones and moves the vertices over the surface, again and
again.

| Field | Default | What it does |
|---|---|---|
| *Edge of the triangles (m)* | the mean edge of the model | the length of the sides of the new triangles |
| *Iterations* | 10 | how many rounds of splitting, collapsing and moving |
| *Adaptive (smaller triangles where the surface curves)* | not ticked | ticked: the edge shrinks where the surface bends |
| *Sharp edges to keep: angle (degrees)* | 30 | edges between faces at a sharper angle stay where they are |
| *Largest distance from the surface before (m, 0 = no check)* | 0 | when above 0, a vertex is not moved farther than this from the model before |
| *Repair the topology first (on a non-manifold model MeshLab crashes)* | ticked | the repair of section 6 before the remeshing, and again after it: the remeshing joins again the vertices the repair split |

The texture has to be built again afterwards. On the tutorial model the edge proposed, 0.70 m, gave
526,706 faces in 24 s, with a mean edge of 0.63 m - a little shorter than the one asked - and 12
non-manifold vertices left, as after the repair alone. Remeshing moves every vertex: it is the right
step before modelling or printing, not before measuring.

### 4. Smooth { #smooth }

| Field | Default | What it does |
|---|---|---|
| *Method* | *Taubin (no shrinking; 10 steps)* | Taubin alternates a step that smooths and one that swells back, so the model does not shrink; *HC Laplacian (1 step)* moves each vertex towards its neighbours and then partly back towards where it was: one step shrinks the model very little, many steps more |
| *Steps (0 = the method's)* | 0 | 0: 10 steps for Taubin, 1 for HC; more steps, smoother surface |
| *Taubin: lambda* | 0.5 | the strength of the smoothing step |
| *Taubin: mu* | -0.53 | the strength of the step that swells back (negative, a little larger than lambda) |
| *The borders stay where they are* | ticked | the vertices of the open edges do not move: the outline of the survey keeps its place |

The faces stay the same and the texture stays. Ten Taubin steps took 2.2 s on the tutorial model.
Smoothing removes the noise of photogrammetry from surfaces that are smooth in reality - plaster,
pottery, a worked block; on rough ground, rubble or carved surfaces it removes detail too.

### 5. Close holes { #holes }

*Close holes...* fills the holes up to a size with new faces.

| Field | Default | What it does |
|---|---|---|
| *Holes up to (edges of the border)* | 30 | the size of the largest hole closed, as the number of edges around it |
| *Avoid faces that intersect* | ticked | the new faces must not cross the model: a hole that cannot be filled so stays open |
| *Refine the new faces to an edge of (m, 0 = no)* | 0 | when above 0, the faces of the filling are split down to this edge |
| *Leave the outline of each part open (its longest border)* | ticked | the outline is the longest border of each separate part |
| *Repair the topology first (MeshLab closes holes only on 2-manifold models)* | ticked | the repair of section 6 before closing |

An excavated surface, a section, the survey of a field are open surfaces: their outline is a border
like the edge of a hole, and MeshLab alone would cap the whole surface with a lid. So the plugin gives
MeshLab only the holes - every border loop except the longest one of each separate part - and the
outline stays open. Untick *Leave the outline...* to close the bottom of an object photographed from
above, with a number of edges larger than its outline.

<figure markdown="span">
  ![Holes closed, outline left open](../img/plugins/mesh-tools/closing-holes.jpg)
  <figcaption>The fields south-west of the tell, seen from above with the border edges in red. (a) The
  model after <em>Repair the topology</em>: the small holes left where the photos saw too little of the
  ground. (b) After <em>Close holes</em> with the defaults: the holes up to 30 edges are closed, the
  larger ones and the outline of each part stay open. The colours are those of the vertices: the
  texture has to be built again.</figcaption>
</figure>

On the tutorial model, after the repair, 3,302 holes were closed with 13,071 new faces in under 4 s;
the border edges went from 57,984 to 39,141. When there is no hole that small, the message *No hole up
to 30 edges to close: the model has not changed.* says so and no version is added.

### 6. Repair the topology { #repair }

Every step can be left out by unticking it; they run in this order.

| Field | What it does |
|---|---|
| *Duplicate vertices* | vertices at the same place joined |
| *Duplicate faces* | faces repeated on the same three vertices removed |
| *Faces without area* | faces with no area (their vertices on a line) removed |
| *Non-manifold edges* | edges shared by three or more faces made regular, as chosen in the next field |
| *Non-manifold edges: how* | *remove the faces in excess* (the default) or *split the vertices* |
| *Non-manifold vertices (split)* | a vertex where two fans of faces touch split into two |
| *Unused vertices* | vertices no face uses removed |

The texture stays. The message lists what each step changed and what is left; on the tutorial model,
in 2.4 s: *Topology repaired: duplicate faces removed 1, non-manifold vertices split 2,636; 12
non-manifold vertices and 0 edges left ('Repair the topology'); texture kept*. The vertices split
stay split in the operations that follow. While a few defects remain the model is not 2-manifold, and
MeshLab cannot count its borders.

### 7. Versions { #versions }

Every operation leaves the model before among the versions of the chunk, unless you chose
*Overwrite it*. In the *Workspace*, under *3D model*: a double click on a kept version shows it in the
3D view without making it active; right click, *Make active (the other tools use it)* puts it back in
use. Each version is a whole model, with its texture when it has one: several versions of a large
model take room on the disk (*Delete* in the same menu).

## From the command line { #cli }

The same operations with `ppg plugin mesh-tools WHAT PROJECT ...`. The project comes right after the
operation; `--chunk LABEL` works on a chunk other than the active one. When the model changes, the
project is saved.

```bash
ppg plugin mesh-tools status
ppg plugin mesh-tools install
ppg plugin mesh-tools measure scavo.ppg
ppg plugin mesh-tools decimate scavo.ppg --faces 100000 --name "For the web"
ppg plugin mesh-tools decimate scavo.ppg --fraction 0.25 --planar
ppg plugin mesh-tools remesh scavo.ppg --edge 0.01 --max-distance 0.002
ppg plugin mesh-tools smooth scavo.ppg --method hc --steps 2
ppg plugin mesh-tools close-holes scavo.ppg --max-edges 100
ppg plugin mesh-tools close-holes vaso.ppg --max-edges 5000 --close-outline
ppg plugin mesh-tools repair scavo.ppg --split
ppg plugin mesh-tools measure scavo.ppg --json
```

| Command | Options |
|---|---|
| `status` | prints which MeshLab the plugin uses and tries it on a triangle; exit code 1 when there is none or it does not work, and then it says what is missing |
| `install` | installs the environment of the plugin and tries it; `--force` also when the Python of the program has pymeshlab, or to install it again. When the computer lacks a library, the environment stays installed and the message names what to add |
| `measure PROJECT` | prints the measures; `--json` all of them as JSON, with the corners of the box |
| `decimate PROJECT` | `--faces` faces to reach (0: `--fraction` of them, 0.5); `--no-texture` the variant without texture; `--quality` (0.3); `--free-borders`; `--free-topology`; `--planar`; `--keep-normals` (no face flipped) |
| `remesh PROJECT` | `--edge` m (0: the mean edge of the model); `--iterations` (10); `--adaptive`; `--feature-angle` degrees (30); `--max-distance` m (0); `--no-repair` |
| `smooth PROJECT` | `--method taubin` or `hc`; `--steps` (0: Taubin 10, HC 1); `--lambda` (0.5); `--mu` (-0.53); `--move-borders` |
| `close-holes PROJECT` | `--max-edges` (30); `--refine` m (0); `--allow-self-intersections`; `--close-outline`; `--no-repair` |
| `repair PROJECT` | `--split` (non-manifold edges by splitting vertices); `--skip STEP`, repeatable: `duplicate_vertices`, `duplicate_faces`, `null_faces`, `non_manifold_edges`, `non_manifold_vertices`, `unreferenced_vertices` |

Every operation also takes `--name` (the name of the new version), `--overwrite` (no version kept of
the model before) and `--json` (the whole report, with the measures). Without `--json` it prints the
progress, what MeshLab did, the name of the new version and the measures. Every command that runs
MeshLab tries it first, as the window does, and stops with the same message when it does not work.

## Limits { #limits }

* **The geometry changes.** Decimation, remeshing and smoothing move or remove vertices: measure,
  draw sections and compute volumes on the model as built, and keep the edited one for showing and
  giving away.
* **The texture after remeshing and closing holes** is gone: build it again on the new model.
* **Defective models.** A model that is not 2-manifold cannot have its borders counted, and some
  MeshLab filters crash on it: the plugin repairs first, and if MeshLab closes unexpectedly the model is
  left as it was and the message suggests *Repair the topology*. MeshLab's remeshing joins again the
  vertices a repair split: with *Repair the topology first* the plugin repairs once more after it.
  Every summary names the non-manifold vertices and edges left.
* **Memory.** The program and MeshLab both hold the whole model: a model of tens of millions of faces
  needs several gigabytes. Decimate a copy before remeshing it.
* **Volume** only for closed models.
* **Linux** needs the system libraries of [What you need first](#before); **Windows on ARM** has no
  pymeshlab.

## Files written { #files }

* The new model replaces `model.ply` in the folder of the chunk, `<project>.ppg.files/chunk_<n>/`,
  with its texture pages (`model_texture.jpg`...) when the texture was kept.
* The version before goes into `versions/model/<id>/` in the same folder.
* While MeshLab works, a folder `.mesh-tools-...` beside the model holds the file it reads and the one
  it writes; it is deleted at the end (one left by a run that died is deleted after six hours).
* The project keeps the list of the operations with their parameters, the faces before and after and
  the version of pymeshlab.
* The environment of MeshLab, when the plugin installs it: `plugins/_data/mesh-tools/env/` in the
  plugin folder of the program, about 300 MB.

## Licences { #licences }

MeshLab and pymeshlab are made by the Visual Computing Lab of ISTI-CNR and released under the GNU GPL
version 3. Since the plugin works with pymeshlab, the plugin itself is released under the
GPL-3.0-or-later; the program and the other plugins are GPL-2.0-or-later, and the "or later" lets them
work together. The program never loads pymeshlab: it runs it as a separate program and exchanges files
with it.
