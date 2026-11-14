# Several surveys: masks, water, merged products

Application **1.7.7**. Three tools for a site surveyed more than once, or partly under water: a **mask from a colour**
of the orthophoto (the water of a flooded trench, a tarpaulin, the sand around a wreck), the **water level** with the
**refraction correction** of the dense cloud below it, and the **merged orthophoto and DEM** of several chunks tied
together ([ties between chunks](ties.md)). All three are in the *Tools* menu, run in the background and are undone
with *Edit > Undo*.

## Mask from a colour

*Tools > Mask from colour...* turns the cells of the orthophoto of the active chunk within a range of colours into
the polygons of a layer of [free drawings](drawing.md), with their heights from the DEM.

![Mask from colour: the range sampled on a pond, the preview on the copy of the orthophoto](../img/gui/colour-mask-dialog.png)

* **Hue** in degrees (0-360), **saturation** and **value** (brightness) in %: a low and a high end each. A hue whose
  low end is above its high end goes through 0: 350 to 10 are the reds.
* **Sample from the orthophoto**: a click on the 2D view sets the three ranges around the colour of a small disc there
  (*Radius*). Start from it, then widen or narrow the ranges.
* The cleaning: **Remove specks up to** (spots smaller than this go), **Fill the holes**, **Regions** (*Only the
  largest region*, or *All the regions* of at least the **Smallest area**), **Margin** (metres added around the
  polygons; negative: taken away).
* **Layer**: the layer of free drawings (default *Maschera colore N*). A colour mask already in that layer is
  replaced; polygons drawn by hand in it stay.

The **preview** is computed at once on a reduced copy of the orthophoto (about 1000 pixels a side) and drawn on the
view; *Create the layer* computes the mask on a finer copy (at most 4000 pixels a side) in the background, so specks
and the smallest areas may change a little. Creating the layer is one step of *Edit > Undo*. A polygon of the mask has
one outline: its holes are left out and counted in the log.

What the layer is for:

* **cropping the products**: right click on the layer in the [Layer pane](panes.md#layer), *Crop with this mask...*
  (*Crop with the polygons of this layer...* for a layer drawn by hand) opens the crop of DEM, orthophoto, cloud and
  model with the union of its polygons;
* the **area** (or what to **leave out**) of a [stable height](ties.md#stable-height);
* the water of the **water level** (below);
* every export of the free drawings (DXF, GeoPackage, GeoJSON, CSV, QGIS).

The orthophoto must be up to date: one built before the chunk was moved by a tie solve is refused (build it again).

## Water level and refraction

Seen from above through still water, the bottom looks shallower than it is: the photos see each point of the bottom
higher, by the refraction at the surface. With the **level** of the water L and its **refractive index** n, the points
of the dense cloud below the level go back to their height: z' = L - n (L - z).

*Tools > Water level and refraction...* (or a double click on *Water: level ...* under the chunk in the Workspace)
works on the active chunk, which needs a DEM or a dense cloud.

![Water level and refraction: the level measured on the edge of a pond](../img/gui/water-dialog.png)

**The level**, one of:

* **From the edge of the water**: the water is *A layer of polygons* (a colour mask, a polygon drawn around it) or
  *A colour range of the orthophoto* (with *Sample from the orthophoto*; *Mask from colour...* opens the mask window to
  make a layer with its cleaning). *Measure the level* follows the outline of the water and reads the DEM on the land
  side, 2 and 4 cells from the edge, so the slope of the bank is taken away; the median of these heights is the level.
  The window says how many heights, their spread (NMAD), the 10-90 % range and the rise of the bank removed, and draws
  the heights used on the view (layer *Bordo dell'acqua*, while the window is open).
* **By hand**: a height typed in metres, *Set the level*.

**Refractive index**: *Sea (1.34)*, *Fresh water (1.33)* or *Other* (1 to 2). It is stored with the level.

Setting the level, changing n and measuring again are each one step of *Edit > Undo*. The level is kept in the project
and moves with the chunk when a tie solve moves it.

**Correct the cloud** writes a new **version** of the dense cloud with the points below the level corrected (those
above it stay as they were, bit for bit); the cloud of before stays among the versions, and making it active again
undoes the correction. *Rebuild the DEM from the corrected cloud* also builds a new version of the DEM, at the cell of
the existing one. A cloud already corrected is not corrected twice.

The [Shipwreck plugin](../plugins/shipwreck.md#water) can correct the heights of its sections under a level as well.
With a model built from the corrected cloud leave that correction off, or the bottom is corrected twice and ends below
its true depth (from version 0.5.0 the plugin asks when the active cloud of the chunk is a corrected version).

!!! warning "Limits"
    * The correction is **paraxial**: it holds for photos close to the vertical. Oblique photos through the water need
      a correction per ray, which this is not.
    * The water must be still and its surface flat: waves, a current, a turbid bottom spoil the cloud before any
      correction.
    * The level is read on the DEM: a DEM coarse or noisy on the bank gives a level as good as it is. The spread and
      the 10-90 % range say how much to trust it.
    * The cloud is read whole in memory: a very large cloud needs as much memory as it takes on disk.

### Several levels, shoreline planes, the factor from control points

A survey of several years or of several basins does not have one level and one index.

* **One level and one factor per chunk** (a chunk per year): each chunk keeps its own level and *effective factor*
  n. `ppg water table` sets them for several chunks at once from a CSV, by chunk or by survey date (the date of the
  phase, as in the [timeline](archaeology.md)): columns `chunk` or `date`, `level`, `n` (`;` with decimal commas).
* **Basins** within a chunk: each basin has its level, its factor and its polygons (a WKT polygon or a layer of free
  drawings); the basin without a polygon takes every point no other basin takes, the points outside every basin stay as
  they are. A level can be a plane fitted to **points picked on the shoreline** (three or more: a water surface seen
  slightly tilted because the frame is tilted); the command says the tilt and the residual of the fit.
* **The effective factor from control points**: points of known true height under the water (a staff, GNSS on the
  bottom) whose apparent height is read on the DEM or on the dense cloud before the correction, or markers whose
  reference lies under the water and whose position was triangulated through it. The least squares on
  L - z_true = n (L - z_apparent) give n, with the residual of every point; `--fit-level` fits the level too (three
  points at least). `--apply` stores the factor on the chunk, or on the basin of `--basin`.

Without basins nothing changes: one level and one n, as before. Basins and factors move with the chunk like the
level. These are on the command line and in the Python API; the window above still sets one level per chunk.

## Merged model

The **models** of several chunks - the blocks of a wreck surveyed in different years, the pieces of a building - become
**one textured model** of a new chunk: *Workflow > Merge models into one...* or `ppg merge-models`.

* The chunks must share the system (aligned or georeferenced); the first gives the system of the new chunk. The source
  chunks are not changed.
* **Texture pages**: kept as they are, or *packed* together in pages of a largest side (8192 px by default); a page
  that does not fit is reduced. Texture coordinates are fractions of a page, so a reduced page needs no other change.
* **Colours** (optional): every model takes the colours of the first where their surfaces overlap - an RGB gain and
  offset per channel, or a *Lab transfer* (mean and spread of each channel in CIE Lab). A model that overlaps only
  another already matched follows the chain; one that overlaps none is left as it is and said in the warnings.
* **Overlapped faces** (optional): where two models cover the same surface, the faces of the later one in the list
  are removed within a distance (automatic: twice the median edge), only where they face the same way.
* **Openings stay open**: nothing is bridged between the models nor closed after the removal.
* The source chunk of every face is kept with the model (its group): an OBJ export can write one group per source.

The new chunk is a chunk like the others: DEM from the model, orthophoto from its texture (below), elevations,
measures and exports work on it.

## Orthophoto of a chunk without photos

An imported textured model or a merged model has no aligned photos: *Workflow > Build orthophoto...* then makes the
orthophoto **from the texture of the model** (and says so). Every cell sends a vertical ray to the model and takes the
texture where it meets it; where the texture is finer than the cell, several rays per cell are averaged. The cell
defaults to the size of a texel. The product is the orthophoto of the chunk like the others (GeoTIFF RGBA in the CRS of
the chunk, versions, exports, measures). *Elevations* on such a chunk are painted from the texture too.
`ppg ortho --texture` asks for it on a chunk that has photos as well (`--photos` the opposite).

The orthophoto is made and written tile by tile (BigTIFF when needed): its size is limited by the disk, not by the
memory nor by the 32,767 pixels of a side that stop OpenCV's `remap`.

## Merged orthophoto and DEM

Several chunks of the same site - campaigns of different years, pieces of a building, a dry part and a flooded one -
once tied in one system can give one orthophoto or one DEM. *Tools > Merged orthophoto and DEM...*:

![Merged orthophoto and DEM: two campaigns, an area each](../img/gui/mosaic-dialog.png)

* the **list** of the chunks: tick those to merge (only the chunks with the product can be ticked). The order is the
  **priority** (*Up*, *Down*): where two chunks have data, the first one wins. The **system** of the merged product is
  that of the first chunk; a chunk in a local frame with one in a system of the world is refused, as for the ties, and
  so is a pair of systems that only an approximate transformation of PROJ would join (ITRF2020 with another system,
  ETRF2014/2020 with ETRF2000): move the chunk into the system of the first, or make it first. The ties and the stable
  heights accept such a pair, with a note in the log, because they move a few points by the program's own route
  ([Ties](ties.md)); here every pixel would be warped by PROJ's approximation, centimetres to a metre
  off, with nothing to show it;
* an **area** for a chunk (optional): select its row, then *Draw the area* (a polygon on the 2D view) or *Area from a
  layer...* (the polygons of one of its layers of free drawings: a colour mask, a drawing). Inside its area the chunk
  wins over the list; where areas overlap, the first area in the order of the list wins. *Remove the area* takes it
  away. An area keeps the system it was drawn in (that of the 2D view) or taken in (that of its chunk): when another
  chunk, in another system of the world, is put first, the area is carried into its system at the build ("reprojected"
  in its row); a local frame with a system of the world cannot be joined, and the build is refused until the area is
  drawn again or the order changed;
* **Product** (*Orthophoto* or *DEM*), **Resolution** (default the finest of the sources), **Background** with areas:
  *Faded* (outside every area the cells come from the list, blended halfway toward white in the orthophoto; the DEM
  keeps its heights) or *None* (only the areas: the rest stays empty), **Name** (default *Ortofoto unita N* /
  *DEM unito N*).

*Build* runs in the background, in blocks of 2048 cells: rasters of any size merge in little memory. Each source
raster is placed where its chunk is **now**: one built before the last tie solve is moved with its chunk, in plan and
in height, as the 2D view shows it (the log names it; a source tilted by more than half a degree is placed in plan
only - build it again).

The merged product goes into the node **Merged products** at the bottom of the Workspace and is shown in the 2D view
with its areas (layer *aree*). Under the cursor, the status bar says **which chunk each cell comes from** ("from
2025").

![The merged orthophoto: the areas of 2024 and 2025, the background faded around them, the chunk under the cursor](../img/gui/mosaic-view.png)

* A double click on it shows it again; the right click has *Show*, *Open the folder* and *Remove*.
* A merged product whose chunks moved (a new tie solve), whose source rasters were built again or whose chunks left
  the project is marked **to merge again**, in red: build it again.
* Building it is one step of *Edit > Undo*: the undo puts its folder aside (as a [removal](panes.md#removing) does),
  the redo brings it back. *Remove* is undone the same way. When the project is closed, the files follow the project
  as last saved.
* Shown while the active chunk is in another system, the status bar says so: the drawings of the chunk do not fall on
  it.
* Over a merged orthophoto the status bar gives no height: the DEM of the active chunk may not cover it. Over a merged
  DEM only the status bar shows the merged heights. Drawing and measuring tools always take their heights from the
  active chunk's DEM, also over a merged DEM: draw and measure on the products of a chunk.

The files are in `<project>.ppg.files/mosaics/<id>/`: `mosaic.tif` (RGBA for the orthophoto, float32 with NaN for the
DEM), `sources.tif` (the chunk of each cell: 0 none, 1 the first of the list...), `areas.gpkg` (layer `aree`, attributes
`chunk` and `priority`) and `mosaic.json`. The paths of the sources are kept relative to the project: a project moved
or renamed stays as it was.

## Command line

```bash
ppg mask colour scavo.ppg 2025 --sample 2480003.2,4500000.6 [--radius 0.1] [--opening 0.05] [--layer Acqua]
ppg mask colour scavo.ppg 2025 --hue 190,230 --sat 30,100 --val 20,100 [--all] [--min-area 0.05] [--margin 0.1]
ppg water level scavo.ppg 2025 --layer Acqua [--sea | --fresh | --n 1.335]   # from the edge of the water
ppg water level scavo.ppg 2025 --set 0.80                                     # by hand
ppg water correct scavo.ppg 2025 [--dem]                                      # a new version of the dense cloud
ppg water show scavo.ppg 2025
ppg water basin scavo.ppg 2025 --name Est --shore X,Y,Z;X,Y,Z;X,Y,Z --polygon "POLYGON ((...))" --n 1.33
ppg water basin scavo.ppg 2025 --name Resto --level 0.78 --n 1.34          # no polygon: everywhere else
ppg water basin scavo.ppg 2025 --clear                                      # back to the single level
ppg water factor scavo.ppg 2025 --points controlli.csv [--source dem|cloud] [--markers T1,T2] [--fit-level] [--apply]
ppg water table scavo.ppg livelli.csv                                       # chunk or date, level, n
ppg merge-models scavo.ppg 2024 2025 2026 [--colour gain|lab] [--remove-overlaps --priority 2026,2025,2024]
    [--overlap-distance 0.02] [--pages keep|repack] [--page-size 8192] [--max-page 8192] [--label "Modello unito"]
ppg ortho scavo.ppg --chunk "Modello unito" [--resolution 0.005] [--texture] [--supersample 2]
ppg mosaic build scavo.ppg --chunks 2024,2025 [--dem] [--areas aree.gpkg] [--resolution 0.01] [--background none]
ppg mosaic list scavo.ppg [--json]
ppg mosaic remove scavo.ppg m1a2b3c4d
```

`--areas` takes a GeoPackage of polygons with the attribute `chunk` (label or key) and, optionally, `priority`.
`ppg mosaic remove` deletes the files at once (the window puts them aside for *Undo*). All the options are in the
[Python API](../api.md#several-surveys-masks-water-merged-products).
