# Powerlines

The *Powerlines* plugin finds the overhead lines - power, telephone - in the dense cloud of a survey.
Every conductor becomes a hanging curve, a catenary, with its length, its sag, its lowest clearance
above the ground and its distance from the nearest obstacle - a tree, a roof; the poles where the
conductors end are placed and measured; the points of the wires and of the poles take classes of their
own in the cloud. The lines go onto the orthophoto and the DEM of the program and out as 3D GeoPackage,
DXF and CSV.

On an archaeological survey a line is part of the setting to record, an obstacle for the machines, the
cranes and the drones of the excavation, and a thin object that spoils a mesh or a DEM: once its points
are classed, the DEM and the model can be built without them, by unticking their classes
([DEM and model without the wires](#dem)).

This page describes version 0.1.1 of the plugin, which needs pyarchinit-photogrammetry 1.1.0 or later.
Its README and source code are in the
[plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/powerlines).

<figure markdown="span">
  ![The wires and poles found in a survey, and the profile of one span](../img/plugins/powerlines/detection.png)
  <figcaption>(a) A line of three parallel conductors over three spans and a lower line crossing it,
  among trees and a building: the ten conductors found and the six poles. (b) The profile of one
  conductor of the first span: the points of the wire, the catenary fitted to them, its sag below the
  chord and its clearance above the ground. Synthetic data: the survey the plugin is tested with, in
  local metres.</figcaption>
</figure>

## In short

1. Fly close to the lines and build the dense cloud at high quality: the wires must be in the cloud.
2. Better, classify the ground of the cloud (for example with the [terrio](terrio.md) plugin).
3. *Plugins > Powerlines > Detect powerlines...*
4. Read the summary; the wires and the poles are on the orthophoto and the DEM; *List of the
   powerlines...* gives every conductor.
5. *File > Export*: GeoPackage, DXF or CSV.

## What it does

| Result | What it is | Useful for |
|---|---|---|
| Conductors | every wire as a catenary: length, sag, lowest clearance above the ground, distance from the nearest obstacle, direction, heights, a vertex every metre | recording a line, the safety distances of works under it, vegetation too close to it |
| Lines | the conductors side by side over the same span, grouped | telling the lines apart |
| Poles | where the conductors end together: position, ground height, height, conductors | the supports of the line in the plan |
| Classes | the points of the wires in class 14, of the poles in class 15 | the wires shown apart in the 3D view; a DEM or a model built without those classes (untick them: they are not left out by themselves) |
| Exports | GeoPackage with 3D lines and points, DXF with 3D polylines, CSV | GIS, CAD, the report |

## What you need first { #before }

* **The plugin**, installed from the program: see [Plugin guides](guides.md#install). Nothing else:
  what it uses comes with the program.
* **A dense cloud in metres with z upwards**: a chunk with a reference system, or levelled. The
  detection measures heights above the ground and the slope of the wires: a cloud tilted in a local frame
  gives nothing sensible.
* **The wires in the cloud.** The detection works on the points: a cable that the reconstruction did not
  produce cannot be found. Photogrammetry often loses thin cables seen from far away against a busy
  ground: see [the next section](#data). A lidar cloud imported with *terrio* works as well.

## The data { #data }

### The flight { #flight }

* Fly close: inspection flights at 20-30 m from the lines, or lower, rather than mapping flights at 80-120
  m, where a cable is thinner than a pixel.
* Photograph the lines from several heights and directions, also obliquely, so that every stretch of
  cable is seen by many photos.
* Build the depth maps at high quality (*Workflow > Build dense cloud...*): low qualities smooth the
  cables away.
* Avoid wind: a cable that swings between the photos is not reconstructed.
* For true clearances, the survey needs its ground control points: the heights are those of the cloud.

### What a wire is for the plugin { #wire }

The plugin keeps as candidates the points that are:

* at least the *Minimum height above the ground* (4 m) above the ground;
* in line with their neighbours within the *Neighbourhood radius* (1 m), as straight as the *Minimum
  linearity* (0.85) asks, and not steeper than 37 degrees;
* alone beside the line: no more than two other points within half a metre of it (a roof has points
  beside every point, a wire has none nearer than the next conductor);
* free underneath: no more than two points between 0.3 and 3 m below (a roof edge has its wall under it,
  a branch its trunk, a wire hangs in the air).

Then the candidates are linked into wires when each lies within the *Largest distance between points of a
cable* (2 m) of the other and within 25 cm of its line, so that parallel conductors are never mixed; a run
of points that passes over a pole is cut where the next span begins; wires shorter than the *Shortest
cable* (5 m) or with fewer points than the *Fewest points of a cable* (10) are dropped. A catenary is fitted
to each wire; pieces of the same wire separated by a gap of the reconstruction, up to the *Largest gap
bridged* (15 m), are joined when they lie on the same line and one continues the curve of the other.

The **poles** are placed where the ends of several wires meet within 3 m, away from the edge of the
survey (where the cloud simply ends), and confirmed by a thin vertical structure of points when the cloud
holds one. Thin poles often leave no points in photogrammetry: a pole without points is kept only where at
least two conductors end together, and is placed from their ends.

### The ground { #ground }

The heights above the ground come from the points of class 2 when the cloud has enough of them (2 % of
the points, at least 1,000) - for example after the ground classification of *terrio*; otherwise the plugin
estimates the ground itself with a morphological filter (cells of 1 m, windows up to 40 m). The list of
the powerlines says which was used. On a site full of trenches and spoil heaps, a classified ground gives
better heights.

## Step by step in the program { #program }

The *Plugins > Powerlines* menu has *Detect powerlines...*, *Show the powerlines*, *List of the
powerlines...* and *Forget the powerlines*; *File > Export* has *Powerlines (GeoPackage)...*, *Powerlines
(DXF)...* and *Powerlines (CSV)...*. The plugin has no pane.

### 1. Detect { #detect }

*Detect powerlines...* works on the dense cloud of the active chunk.

| Field | Default | What it does |
|---|---|---|
| *Minimum height above the ground (m)* | 4 | no cable is looked for below it: lower it for low lines, raise it to leave out fences, walls and vehicles |
| *Neighbourhood radius (m)* | 1 | the neighbours that give the direction of a point: keep it below the distance between parallel conductors |
| *Minimum linearity (0-1)* | 0.85 | how straight the neighbours must be: 1 is a perfect line; lower it for noisy clouds |
| *Largest distance between points of a cable (m)* | 2 | raise it for sparse reconstructions |
| *Shortest cable (m)* | 5 | shorter groups are not wires |
| *Fewest points of a cable* | 10 | thinner groups are not wires |
| *Largest gap bridged along a cable (m)* | 15 | pieces of one cable separated by a gap of the reconstruction are joined up to this gap |
| *Classes 14 (wires) and 15 (poles) in the cloud* | ticked | the points found take these classes; the points classified by a previous detection take back the class they had first. No other point changes: the classes 14 and 15 of an imported lidar or of another tool, and the labels given by hand, stay |

The detection runs in the background; at the end the conductors are drawn on the orthophoto and DEM
views as the layer *Linee elettriche* (magenta lines, labelled `L1`, `L2`...) and the poles as the layer
*Pali* (orange crosses, `P1`, `P2`...), the project is saved and a window sums up:

```text
10 wires in 4 lines, 602 m in all, 6 poles.
Lowest clearance 5.00 m (L1).
Nearest obstacle at 1.68 m (L5).
Wire points have class 14, pole points class 15 (3D view: colours by class). The DEM and the model do not leave them out by themselves: to build them without wires and poles, untick classes 14 and 15 in their window.
```

With the *Move* tool, a click on a line or a pole shows its attributes. When nothing is found, the window
says so: the cloud does not hold the cables, or the minimum height or the linearity are too strict.

### 2. Show, list, forget { #list }

* *Show the powerlines*: the two layers again - after the project is opened again, for example.
* *List of the powerlines...*: a table of the conductors and the poles:

    ```text
    10 wires in 4 lines, 6 poles (6 with points); ground from the morphological filter; 2,759 wire points
      id  line  length  sag  clearance  obstacle  azimuth  points
      L1      1      98.0 m   1.92 m   5.00 m    7.24 m  180.0     454
      L2      2      60.6 m   1.52 m   8.51 m   23.94 m   90.0     277
      ...
      P1: height 10.5 m, wires L4, L5, L6
    ```

* *Forget the powerlines*: after a confirmation, the result goes, and the points the plugin classified
  take back the class they had. The classes 14 and 15 of an imported lidar or of another tool, and those
  set by hand, stay. When the dense cloud changed after the detection (built again, cleaned, cropped),
  or the detection was made by version 0.1.0, the plugin can no longer tell its points from the others:
  their classes stay as they are, and a window says so (correct them by hand in the 3D view if needed).

To see the classes: the *Model* view, *Dense cloud*, colours *Classes*.

### DEM and model without the wires { #dem }

The classes 14 and 15 are not left out of the DEM or of the model by themselves: the program leaves out
only the noise (7 and 18). To build a DEM or a model without wires and poles, build them from the dense
cloud and untick 14 and 15 in the list of classes of *Workflow > Build DEM...* or *Workflow > Build
model...*.

### 3. Exports { #export }

* *File > Export > Powerlines (GeoPackage)...*: the table `linee_elettriche` (3D lines with a vertex every
  metre) and the table `pali` (3D points at the foot of the poles), in the reference system of the chunk.
  An existing GeoPackage keeps its other tables. A file that exists and is not a GeoPackage is never
  deleted or written into: the export stops with *... exists and is not a GeoPackage: the plugin does not
  overwrite it*.
* *File > Export > Powerlines (DXF)...*: the conductors as 3D polylines on the layer `LINEE_ELETTRICHE`, the
  poles as vertical lines on `PALI`, the labels `L1`, `P1`... on `ETICHETTE`; metres, in the coordinates of
  the survey.
* *File > Export > Powerlines (CSV)...*: a row per conductor with its attributes and its two ends; the
  poles in `<name>_pali.csv` beside it.

## From the command line { #cli }

`ppg plugin powerlines WHAT PROJECT ...`, on the active chunk (`--chunk NAME` for another one).

```bash
ppg plugin powerlines detect scavo.ppg
ppg plugin powerlines detect scavo.ppg --min-height 3 --linearity 0.8 --gap 3 --export linee.gpkg
ppg plugin powerlines list scavo.ppg
ppg plugin powerlines export scavo.ppg linee.dxf
ppg plugin powerlines export scavo.ppg linee.csv
ppg plugin powerlines clear scavo.ppg
```

| Command | Options |
|---|---|
| `detect PROJECT` | `--min-height` (4); `--radius` (1); `--linearity` (0.85); `--gap` (2); `--min-length` (5); `--min-points` (10); `--merge-gap` (15); `--no-classify` leaves the classes of the cloud as they are; `--export` also writes a `.gpkg`, `.dxf` or `.csv` |
| `list PROJECT` | the table of the conductors and the poles |
| `export PROJECT OUT` | `.gpkg`, `.dxf` or `.csv` |
| `clear PROJECT` | forgets the result; the points the plugin classified take back the class they had |

`detect` and `clear` save the project; `detect` prints the table.

## Reading the results { #reading }

### Conductors { #reading-wires }

| Column (exports) | What it is |
|---|---|
| `id` | `L1`, `L2`... in the window: the longest first |
| `linea` | the line: conductors side by side over the same span share the number |
| `lunghezza_m` | the length along the curve |
| `freccia_m` | the sag: the largest distance of the wire below the straight line between its ends |
| `franco_m` | the clearance: the lowest height of the wire above the ground, checked every metre |
| `ostacolo_m` | the distance from the nearest point that is neither a wire nor a pole and stands at least 0.5 m above the ground: a tree, a roof, a wall |
| `z_min_m`, `z_max_m` | the lowest and highest points of the wire |
| `azimut_gradi` | its direction in plan, 0 to 180 degrees from north |
| `punti` | the points of the cloud on the wire |
| `rms_m` | how far the points lie from the fitted curve |

* A **line** is a span, not a whole power line: the three conductors between two poles are one line, the
  three of the next span another one.
* The **sag** grows with the length of the span and with the temperature: compare spans measured on the
  same day.
* The **clearance** is measured on the ground model of the plugin: over a hedge or a parked truck it is
  measured to the ground, not to the top of the obstacle - that is what `ostacolo_m` is for.
* A **small `rms_m`** (a few centimetres) and many points mean a well reconstructed cable. Long gaps in
  the points, a high rms or a wire much shorter than its span call for a look at the cloud.

### Poles { #reading-poles }

The table `pali` has `altezza_m` (from the ground to the top of the points of the pole, or to the highest
attachment of its conductors), `z_terreno_m`, `conduttori` and `linee` (the wires and lines that end on
it), `rilevato` (1 when the cloud holds the structure of the pole, 0 when the pole is placed from the ends
of the conductors) and `punti`. A pole with `rilevato` 0 is where the conductors end, not necessarily the
axis of the pole: its height is that of the highest conductor.

### Limits { #limits }

* **The points decide.** Cables missing from the cloud are missing from the result; a detection on the
  photos themselves would be another tool.
* **Bundles** of conductors closer than half a metre are not separated.
* **Free air** of 0.3-3 m is needed under a wire: cables lying on a roof or along a wall are not found.
* **The heights** are those of the cloud: without ground control points the clearance carries the error
  of the survey.

## Files written { #files }

The result - every conductor with its catenary, its vertices every metre and its attributes, every
pole - is kept in the project. In the folder of the chunk the plugin writes only
`powerlines/classified.npz`: the points it classified, with the class they had and a fingerprint of the
dense cloud, so that a new detection and *Forget* give back exactly their classes. The classes 14 and 15
are written into the dense cloud, and their names (*Conduttore (linea elettrica) / Wire conductor*, *Palo o
traliccio / Pole or tower*) are added to the legend of the classes. The exports go where you choose.
