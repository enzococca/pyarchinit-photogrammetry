# Spirula

The *Spirula* plugin trains a 3D Gaussian splat of the aligned photos with
[Spirula Studio](https://github.com/harry7557558/spirula-studio), a free program for Gaussian splatting
(GPL-3) that runs on the graphics card of Apple, NVIDIA, AMD and Intel computers. A splat is a cloud of
millions of small coloured, half-transparent blobs that, seen from any point of view near the photos,
looks like the photos: grass, shrubs, fences, the crumbling edge of a section, the soft shapes a mesh
breaks into triangles, come out as they look. The plugin prepares the data of the chunk - the aligned
photos with their cameras, the masks, the dense cloud as a starting point - runs the training with its
progress, keeps the splat as a product of the chunk, extracts a mesh from it, opens it in the viewer of
Spirula Studio and, when Spirula Studio is missing, installs it.

This page describes version 0.1.2 of the plugin, which needs pyarchinit-photogrammetry 1.1.0 or later
and Spirula Studio (it was tried with release 2026.10.16). Its README and source code are in the
[plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/spirula).

<figure markdown="span">
  ![A mesh extracted from a splat of a tell](../img/plugins/spirula/mesh-from-splat.jpg)
  <figcaption>The mesh extracted by Spirula from the splat of 57 drone photos of Tell Sultan, the site of
  the tutorial: 336,000 faces with a colour per vertex, seen from the point of
  view of one of the photos. The splat was trained at quality <em>low</em> on the photos halved
  (15,000 steps, 200,000 splats, 13 minutes on an Apple M4 Pro); the mesh took 14 seconds. Away
  from the area the photos cover, the surface breaks into large triangles.</figcaption>
</figure>

## In short

1. Align the photos, and better build the dense cloud: the splats start from it.
2. *Plugins > Spirula > Train Gaussian splatting...*: preset, quality, reduction of the photos. The
   first time, the plugin offers to download and install Spirula Studio.
3. Wait: minutes to hours on the graphics card; the progress bar counts the steps; if you like, follow
   the training in the browser.
4. *Plugins > Spirula > Open the splat in Spirula Studio* to look at it, *File > Export > Splat (PLY 3D
   Gaussian Splatting)...* to give it, in the coordinates of the survey, to another viewer.
5. *Plugins > Spirula > Mesh from the splat...*: PLY, OBJ, glTF, GLB or STL; the PLY also among the
   versions of the model.

## What it does

| Step | What it gives | Useful for |
|---|---|---|
| Training | `splat.ply`, the standard PLY of 3D Gaussian splatting that every splat viewer reads, as the product *splat* of the chunk | showing a site or a find as it looks, from any point of view: visits, presentations, teaching, the public |
| Mesh from the splat | a mesh in PLY, OBJ, glTF, GLB or STL beside the splat, with colours per vertex or a texture; the PLY as a version of the model | a mesh of what the splat shows, for 3D programs, the web, printing |
| Export | the splat in the coordinates of the survey (or as Spirula trained it) | other splat viewers and editors, the splat beside the other products of the survey |
| Viewer | the splat in the window of Spirula Studio | looking at the result |
| Installation | the latest release of Spirula Studio for this computer | getting the program without searching for it |

A splat is made to look right from the photos' points of view; it is not a survey. Measurements,
sections, plans and volumes come from the dense cloud, the model, the DEM and the orthophoto of the
program.

## What you need first { #before }

* **The plugin**, installed from the program: see [Plugin guides](guides.md#install).
* **Spirula Studio.** Its releases cover macOS on Apple Silicon (an application, `Spirula Studio.app`),
  Windows on x86-64 and Linux on x86-64 (a zip with one executable, `spirula`); Intel Macs and ARM
  computers with Windows or Linux have no build. The plugin finds it by itself - in
  `/Applications/Spirula Studio.app`, in the usual folders of Windows and Linux, on the PATH, or where the
  variable `PPG_SPIRULA` says - and when it does not, offers to download it (about 50-60 MB from GitHub,
  checked with the SHA-256 GitHub publishes) and install it: into `/Applications` on macOS (or
  `~/Applications` when `/Applications` cannot be written), into `%LOCALAPPDATA%\Programs\Spirula Studio`
  on Windows, into `~/.local/share/spirula-studio` on Linux. You can also install it yourself and tell
  the plugin where it is (*Plugins > Spirula > Where Spirula Studio is...*).
* **A graphics card with Vulkan**: the Apple chips (M1 and later), NVIDIA, AMD and Intel cards with
  recent drivers. The training runs on it; a computer without one cannot train.
* **A chunk with aligned photos**, *Workflow > Align photos...*: without them the window, the command
  line and the training itself stop with *The photos of the chunk ... are not aligned: align them first
  (Workflow > Align photos, or ppg align).*
* **The photos where the project expects them**: Spirula reads the original files (it removes their lens
  distortion by itself). If they moved, link them again (*Workflow > Relink photos (another folder)...*).
* **Better, a dense cloud** (*Workflow > Build dense cloud...*): the splats start from its points.
* **Disk space**: a run takes from a hundred megabytes to a few gigabytes, by the number of splats.

## The data { #data }

### The photos { #photos }

* **Size.** Spirula trains at the resolution of the photos it is given, and memory and time grow with
  it. *Photo reduction* divides their sides: the plugin suggests the divisor that brings them to about 4
  megapixels - 2 for photos of 9 to 25 megapixels, 3 for 48 megapixels, 1 for small photos.
* **Sharp, well exposed photos** make a sharp splat; blurred photos make blurred splats where they are
  the only ones.
* **Light.** Light that changes between the photos - clouds, hours of the day, flights on different
  days - gives patches and floating colours: the *in-the-wild* preset is made for it.
* **Moving things.** People, vehicles, shadows that move between the photos leave ghosts: mask them in
  the photos (*Tools > Masks of the photos*), or pass Spirula's own option
  `--distraction-robustness strong` from the [command line](#cli).
* **Points of view.** A splat looks right near the places the photos were taken from: a nadir flight
  gives a good view from above and poor ones from the side; walk around what you want to see from the
  side.

### Starting points and masks { #seed }

* **Seed points**: the dense cloud of the chunk - up to 2 million of its points, chosen at random - or
  the tie points of the alignment. The dense cloud gives a faster, cleaner start.
* **Masks**: the masks of the photos of the chunk go to Spirula with them, unless *Use the photo masks*
  is unticked; masked parts are left out of the training.

## Step by step in the program { #program }

The *Plugins > Spirula* menu has *Train Gaussian splatting...*, *Mesh from the splat...*, *Open the
splat in Spirula Studio*, *Install Spirula Studio...* and *Where Spirula Studio is...*; *File > Export*
has *Splat (PLY 3D Gaussian Splatting)...* and *Splat (PLY, frame of the training)...*. The plugin has no
pane.

### 1. Spirula Studio { #spirula-studio }

* *Install Spirula Studio...*: the folder (the one of the system by default); the latest release is
  downloaded, checked, installed, and the plugin remembers where it is. The same window comes up by
  itself - with *Download and install*, *Say where it is* and *Cancel* - the first time a step needs
  Spirula and does not find it.
* *Where Spirula Studio is...*: the `spirula` executable, the application (`Spirula Studio.app`) or the
  folder holding it.

### 2. Train { #train }

*Train Gaussian splatting...*

| Field | Default | What it does |
|---|---|---|
| *Preset* | *3dgs (standard)* | the kind of training, from the table below |
| *Quality (iterations)* | *medium (30.000)* | *low*, *medium*, *high*, *ultra*: steps of training and largest number of splats |
| *Iterations (empty: the quality's)* | empty | a number of steps of your own |
| *Photo reduction (1: full resolution)* | the suggested one | the sides of the photos divided by this |
| *Spherical harmonics degree (0-3)* | 3 | how much the colour of a splat may change with the point of view: 0 one colour (a smaller file), 3 reflections and shine |
| *Seed points* | *dense cloud of the chunk* | or *tie points* |
| *Use the photo masks* | ticked | |
| *Spirula web viewer while training* | unticked | the training shown live in the browser |
| *Viewer port* | 7007 | the port of the web viewer |

| Preset | For |
|---|---|
| *3dgs* | most photo sets: drone surveys, excavations, buildings |
| *in-the-wild* | light changing between the photos, things not masked moving in them, long focal lengths |
| *centered-object* | an object photographed all around, with masks, whose background is not wanted: a find on a turntable |
| *360-camera* | the original fisheye images of a 360 camera, with the black circle around them |
| *hdr* | keeping the full range of light of the photos |
| *synthetic* | images rendered with constant exposure |
| *meshing* | a splat made for its mesh rather than for its looks: train with it before *Mesh from the splat* |

| Quality | Steps | Largest number of splats |
|---|---|---|
| *low* | 15,000 | 200,000 |
| *medium* | 30,000 | 1,000,000 |
| *high* | 50,000 | 3,000,000 |
| *ultra* | 80,000 | 10,000,000 |

The training runs in the background. The bar moves every hundred steps with *Spirula: step 1,001 of
15,000, 200,000 splats*, in the language of the program; *Cancel* stops Spirula. With the web viewer,
the browser opens at `http://localhost:7007/` (or the port chosen) as soon as Spirula starts it: the
splat grows while it trains, and the viewer closes with the training. The viewer answers on every network interface of the
computer, so other computers of the same network can open it at this computer's address.

At the end a window sums up:

```text
Splat: .../spirula/runs/20261020-215810/step-000015000.ckpt/splat.ply
  preset 3dgs, quality low, 15,000 steps, 200,000 splats, 57 photos
  seed: dense (2,000,000), masks: no, time 767.3 s
```

The splat becomes the product *splat* of the chunk, and the project is saved. A new training replaces
the product; every training keeps its own folder.

### 3. Look at the splat { #view }

* *Open the splat in Spirula Studio*: the native window of Spirula Studio, with its own controls.
* *File > Export > Splat (PLY 3D Gaussian Splatting)...*: the splat in the coordinates of the survey -
  the reference system of the chunk, or its local frame - for other splat viewers and editors, beside
  the other products of the chunk.
* *File > Export > Splat (PLY, frame of the training)...*: `splat.ply` as Spirula wrote it.

Spirula moves, turns and scales the scene before training it: `splat.ply` is in that frame, centred on
the scene. The project keeps the matrix (`world_from_train`, from the `scene_transform.json` of the run)
that brings it back to the frame of the alignment, and the transform of the chunk brings that into the
coordinates of the survey. The export applies both to every splat: its position, its rotation, its size
(by the scale of the transform) and its colours that change with the point of view - the spherical
harmonics, turned so that every splat shows from every direction the colour it showed before. Opacity
and base colour do not change. Coordinates of a projected system (hundreds of kilometres) do not fit the
32-bit numbers of a splat file without losing centimetres: beyond 10 km the positions are written as
64-bit numbers, which a few viewers do not read - export in the frame of the training for those. A run
without `scene_transform.json` can only be exported in the frame of the training.

### 4. Mesh from the splat { #mesh }

*Mesh from the splat...*, once a splat has been trained.

| Field | Default | What it does |
|---|---|---|
| *Formats* | PLY | one or more of PLY, OBJ, GLTF, GLB, STL |
| *Colour* | *per vertex* | *per vertex*, *texture (OBJ, glTF, GLB)* or *none* |
| *Texture side (0: Spirula's)* | 0 | the side of the texture image in pixels |

The files are written beside the splat of the last training; a PLY is also kept among the versions of
the model, as *Mesh dallo splat (Spirula, ...)*: in the *Workspace*, under the 3D model, right click,
*Show*, or *Make active (the other tools use it)*. The mesh is in the frame of the alignment, like the
program's own model: as the active model it sits with the other products, and exporting the model
writes it in the coordinates of the survey.

## From the command line { #cli }

`ppg plugin spirula WHAT ...`. The commands that work on a project take the active chunk (`--chunk
NAME` for another one) and save the project.

```bash
ppg plugin spirula install
ppg plugin spirula where
ppg plugin spirula where --set "/Applications/Spirula Studio.app"
ppg plugin spirula train scavo.ppg --preset 3dgs --quality medium
ppg plugin spirula train scavo.ppg --preset in-the-wild --quality high --divisor 2 --viewer
ppg plugin spirula train scavo.ppg --quality low --no-masks --name prova -- --cap-max 500000
ppg plugin spirula train scavo.ppg --distraction-robustness strong
ppg plugin spirula mesh scavo.ppg --format ply,glb --color texture
ppg plugin spirula open scavo.ppg
ppg plugin spirula export scavo.ppg splat.ply
ppg plugin spirula export scavo.ppg splat_spirula.ply --frame training
```

| Command | Options |
|---|---|
| `install` | `--dir` the folder (the system's by default) |
| `where` | prints the executable in use; `--set PATH` remembers another one |
| `train PROJECT` | stops with an error when the photos of the chunk are not aligned; `--preset` (3dgs); `--quality` low, medium, high, ultra (medium); `--iterations`; `--divisor` (0: the suggested one, 1: full resolution); `--sh-degree` (3); `--seed` dense or sparse; `--no-masks`; `--viewer`, `--port` (7007), `--no-browser`; `--name` of the run (the date and time by default); `--spirula` the executable |
| `mesh PROJECT` | `--format` among ply, obj, gltf, glb, stl, separated by commas (ply); `--color` none, vertex, texture (vertex); `--texture-size`; `--run` the folder of another run (the last one by default) |
| `open PROJECT` | opens the splat in Spirula Studio |
| `export PROJECT OUT` | the splat as a PLY: `--frame survey` (the default: the coordinates of the survey) or `--frame training` (as Spirula wrote it) |

The options `train` does not know go to `spirula train` as they are - after `--` or not - so every
option of Spirula can be used: `--cap-max` (the largest number of splats),
`--distraction-robustness off|mild|strong` (people and vehicles that move between the photos), and the
others listed by `spirula train --help`.

## Reading the results { #reading }

* **Steps and splats.** The number of splats grows during the training up to the largest the quality
  allows; more splats, more detail, a larger file. A splat of 200,000 is enough for a quick look, millions
  for close views.
* **Where it is good.** Near the points of view of the photos, the splat looks like them. Moving away
  - lower than a drone flew, behind what the photos did not see - it fills with blurred or floating
  blobs: they are the edge of what the photos say.
* **Floating blobs** over the scene often come from the sky, from moving things or from light changing
  between the photos: masks, the *in-the-wild* preset or `--distraction-robustness` help.
* **The mesh** follows the surfaces well where many photos see them and breaks into large flat
  triangles at the borders, as in the figure. For a mesh, train with the *meshing* preset.
* **Time.** On an Apple M4 Pro, 57 drone photos of 9 megapixels, halved, at quality *low* took 13
  minutes; *high* and *ultra* take hours. Large photos at full resolution need much more memory on the
  graphics card.

### Limits { #limits }

* **Not a survey.** The splat reproduces how the scene looks; it has no scale bars, contours or
  classes. Exported, it lies in the coordinates of the survey, but it is still a picture of the scene,
  not a measurement of it.
* **The graphics card decides** how many splats and how large photos fit.
* **Spirula Studio changes.** The plugin runs Spirula as a separate program; a later release may rename
  an option or a file. The plugin looks for the splat and the mesh files wherever Spirula writes them in
  the folder of the run.

## Files written { #files }

Everything goes into the folder of the chunk, next to the project file:
`<project>.ppg.files/chunk_<n>/spirula/`.

* `dataset/`: what Spirula reads, made again at every training - `sparse/0/` (the cameras and points of
  the alignment as a COLMAP model in text), `images` (a link to the folder of the photos: a symbolic link,
  a junction on Windows, or a copy when neither can be made), `masks/`, `seed.ply` (the starting points).
* `runs/<name>/`: one folder per training, named with its date and time unless you name it: Spirula's
  `config.json`, `scene_transform.json`, `splat.ply` (Spirula 2026.10 writes it into a `step-...ckpt/`
  folder) and, after *Mesh from the splat*, the mesh files.
* `versions/model/<id>/model.ply`, in the folder of the chunk: the PLY mesh kept as a version of the
  model.
* The project records the splat, the parameters, steps, splats and time of the training, the command
  given to Spirula, the matrix of the scene and the mesh files.
