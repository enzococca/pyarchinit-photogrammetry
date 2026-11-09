# Flight plans

The *Flight plans* plugin designs a drone survey **before going to the field**: the area, the camera, the ground
resolution (GSD) and the height, the overlaps, the strips and the shots, the time and the batteries, the flight that
follows the terrain on the project DEM, the **predicted coverage** (how many photos will see every cell of the
ground), the GNSS targets as points where the drone goes down to photograph them. It then writes the files the drones
and their apps read - DJI WPML KMZ, DJI Fly, Litchi, QGroundControl, Mission Planner, Parrot - with KML, GeoJSON,
GeoPackage and a flight sheet in Excel or HTML. **After the flight** it compares the plan with the photos taken and
the coverage obtained.

!!! warning "Airspace, permits and safety are the pilot's"
    The plugin computes a route: it does not know the airspace, the no-fly zones, the obstacles (trees, poles,
    buildings, cranes), the weather or the rules of your country. Checking that the flight is allowed and safe, and
    asking for the permits, is the pilot's job. Look at the route on the map and in the flight app before taking off.

This page describes version 0.1.0 of the plugin, which **requires pyarchinit-photogrammetry 1.7.4**. The planner -
serpentine grids, rings, routes given point by point, legs that fit a battery, the camera profiles - is ported from the
author's Photo3D planner. The README and the code are in the [plugin
repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/flightplan).

## Install

```bash
ppg plugins install flightplan
```

or *Tools > Plugins...* in the program ([Installing a plugin](guides.md#install)). The plugin needs nothing beyond the
program. The network is used only for the public terrain tiles: when you choose them, or with the project DEM where it
ends (see [Terrain](#terrain)). The tiles fetched are kept on disk in the plugin's folder.

## What it needs

- **A project with a chunk.** The plans are kept in the chunk (in `chunk.meta`) and its files in
  `<chunk data>/flightplan/`. A chunk with a projected coordinate system (UTM, a national grid) is the best case: you
  can draw the area on the 2D view and compute the coverage.
- **Optional: a DEM and an orthophoto** of the chunk, from an earlier survey or a quick reconnaissance flight. The DEM
  gives the terrain following and the coverage on the real ground; the orthophoto is the background of the maps of the
  legs. Without them the plan is made on flat ground at the takeoff and the maps have a grey background.
- **A chunk in a local system** (no CRS) can be planned only with an area from a file or from coordinates in degrees;
  nothing is drawn on the view and the coverage is not computed (it needs a projected system).
- **Without a project** the command line makes a plan from coordinates into a JSON file (see
  [Command line](#command-line)).

## The pane

*Plugins > Flight plans > Flight plans pane* opens the **Flight plans** pane, docked on the right. The same actions are
in a group of their own in the tool rail of the 2D view (the quadcopter icon): *Draw the flight area*, *Design the
flight...*, *Predicted coverage*, *Export the flight plan...*, *Copy into DJI Fly...*, *Compare with the flight*.

At the top, **Plan:** chooses the active plan of the chunk; **Remove** takes it out (*Edit > Undo* puts it back with
its files; the files are deleted for good the next time the project is opened). The steps follow in order; under each
step a line says its state - what is there, what is missing, what must be done again.

### 1. Area

- **Draw on the view**: click the vertices of the polygon on the 2D view (orthophoto, DEM), double click or Enter to
  close, Esc to cancel.
- **From a layer...**: a polygon already drawn on the 2D view.
- **From a file (KML, GeoJSON, GeoPackage)...**.
- **Coordinates...**: the vertices in degrees, or in the system of the chunk.
- **Margin**: metres added around the area in the blocks of the flight, so that the edges get the same coverage as the
  middle. The polygon you drew is kept as it is; the margin goes only into the blocks.

*Design* again on the same area **replaces the active plan**; a new area makes a **new plan**.

### 2. Camera

The list holds the cameras of the planner, grouped by how they fly: **DJI (Pilot, apps with the SDK)**, **DJI Fly
(from the remote controller only)**, **Other drones** (Autel, Parrot, Skydio...). Their sensor values come from the
spec sheets. A camera that is not in the list is never invented; take it:

- **From a photo (EXIF)...**: a photo of that camera; it needs both `FocalLength` and `FocalLengthIn35mmFilm` in the
  EXIF, otherwise it is refused with a message;
- **From the chunk**: the calibrated sensor of the chunk (it needs the focal length in millimetres);
- **By hand...**: sensor width and height in mm, focal length in mm, pixels on the long side. The fields start from
  the camera chosen in the list.

**High resolution** uses the high-resolution mode of the cameras that have one (48, 50 or 100 MP, as the camera list says); **Battery** is the
minutes of flight you count on for one battery: a plan longer than that is split into legs.

### 3. Parameters

- **Goal**: *Orthophoto* (the GSD in cm/px), *Terrain model (DEM)* (the cell in cm: the GSD is finer), *Full 3D model*
  (nadir plus two crossed obliques), *Close inspection*, *Quick survey*, *GSD by hand*; the value of the goal.
- **Height (m)**: a height above the takeoff instead of the goal.
- **Direction**: *Along the longest side* of the area, or *Degrees*.
- **Double grid**: a second nadir grid crossed at 90 degrees.
- **Obliques**: as the goal says, yes, or no.
- **Forward** and **Side** overlap (%), **Speed** (m/s).
- **A shot per point** (a waypoint for each photo) or **Shots by interval** (the camera shoots every few seconds).
- **Design** makes the plan.

The overlaps are those of the strips over flat ground at the takeoff. What the plan really gives on the ground - where
the terrain rises, at the edges, on short strips - is in the [Coverage](#coverage) step: look at it there.

### 4. Plan

The table lists the **blocks** (grid, obliques, ring, route, targets) and their **legs**, with shots, minutes,
batteries, GSD and heights. A tick on a block makes it fly and go into the files; untick it to leave it out. The
warnings of the planner are listed under the table - speed reduced because the camera cannot shoot often enough,
heights above 120 m over the takeoff (the limit of a stock drone), strips with fewer than three shots, shots less
than 10 m from the ground, side overlap falling under 60 % where the terrain rises, a GSD a quarter coarser where the
terrain falls below the takeoff.

- **Takeoff on the view**: click where the drone takes off. Heights in every file are **above the takeoff point**.
- **Takeoff of this leg**: a takeoff of its own for the leg chosen in the table (each leg of a long flight may start
  from another place); its heights are corrected by the difference of the ground.

On the 2D view the layers *Piano di volo: area*, *strisciate*, *scatti*, *decollo*, *target* show the plan.

### 5. Terrain { #terrain }

**Follow the terrain** gives every point its own height above the takeoff, so that the height above the ground - and
the GSD - stays the same uphill and downhill: height = height above the ground + (ground at the point - ground at the
takeoff), kept between 10 m and 500 m above the takeoff.

- **Project DEM**: the DEM of the chunk. A DEM under the whole flight sends nothing to the network. Only where the DEM
  ends does the plugin ask the public tiles, shifted by the median difference measured where both exist; if the two
  never overlap under the flight the project DEM is left out (heights of different references are not mixed) and a
  warning says so.
- **Public tiles (network)**: the free terrain tiles of AWS (Terrain Tiles, Mapzen terrarium), with a detail of
  *Normal detail*, *Fine detail* or *Finest detail*. They need the network; they are kept in a cache.
- **Remove** goes back to one height per block.

The takeoff is measured with the same source as the waypoints; a takeoff farther than 2000 m from the flight is
ignored. When the area, the takeoff or the DEM change, the heights become old: every export **recomputes them first**
with the source the plan was computed with (tiles stay tiles, the project DEM stays the DEM). The heights of each block
remember the takeoff they were counted from: when the takeoff moved, every block that follows the terrain is
recomputed, a block switched off included. A plan of the planner without a height source is recomputed on the project
DEM only, never on the tiles unless you choose them. If the ground under a takeoff cannot be measured again (no network
for the tiles, the takeoff off the DEM, the DEM gone) the export is **refused** and says why: a file never carries
heights, or descents of the special points, counted from the old takeoff.

The warnings of *Design* (GSD, overlap, distance from the ground) read the ground from the project DEM; they ask the
public tiles only when the plan's heights come from them.

### 6. Coverage { #coverage }

**Compute** puts every shot back in the chunk's coordinate system, as a camera with its heading and pitch over the
ground of the DEM (or a flat ground at the takeoff without a DEM), and counts on a grid of cells **how many photos see
each cell** and the finest GSD a photo gives there. The state line gives the median photos per cell, the 10th
percentile, the share of the area under the **Threshold** (5 photos by default), the median GSD and the cell.

- **Show the coverage** replaces the orthophoto on the 2D view with the coverage map: red no photo, orange under the
  threshold, yellow-green up to twice the threshold, green above. **Show the orthophoto** goes back. The layer *Piano di
  volo: copertura bassa* outlines the cells under the threshold.
- The coverage is the truth to look at before flying: where the terrain rises or the strips are short it shows the
  overlap the plan really gives.
- **Limits**: there is **no occlusion** - walls and trees do not hide the ground behind them. Cells outside the DEM are
  taken **flat at the takeoff**. A plan changed after the computation makes the coverage *To do again*.

### 7. Targets

GNSS targets on the ground become **special points**: the drone goes over each target, descends to **Metres above the
ground** (10 m by default: an A4 target can be read from there) and takes a ladder of photos with the camera straight
down.

- **From a CSV...**: name, coordinates and height, in degrees, in UTM, or in metres of another system (give its EPSG).
  Without a header the order of the columns is guessed from the numbers; when it is only a guess a **red line** under
  the step says so: check latitude and longitude.
- **From the chunk's markers**: the markers with measured coordinates. In a chunk that is not georeferenced the
  markers' positions are **estimated** by the survey: they are good to fly over, **never as GCPs**.
- **Remove** takes the targets out of the plan.

The descents need the ground under the targets. It is read from the project DEM; in a chunk without a DEM the plugin
asks whether to use the **public tiles (network)** (on the command line, `--tiles`). Without either, the export is
refused and says to choose the tiles or add a DEM: the network is never asked for it unasked.

### 8. Export

Choose the format and press **Export...**; **Excel sheet** and **HTML sheet** write the flight sheet; **Open in QGIS**
opens the plan in QGIS. *File > Export* of the program has five of them - the DJI KMZ, the QGroundControl plan, the
Litchi CSV, the GeoPackage and the Excel sheet; the other formats are here and on the command line. Formats that hold one leg write **one
file per leg** next to the name you chose (or a zip); files of an earlier export with the same names are replaced, and
the export says how many. See [Exports per drone family](#exports).

**Copy into DJI Fly...** puts the legs into the missions of DJI Fly on the remote controller (see [DJI Fly](#djifly)).

### 9. After the flight

Add the photos of the flight to the chunk (*File > Add photos*), then **Compare**:

- **Maximum distance**: how far from a planned shot the photo that makes it can be; *automatic* = half the step of the
  leg.
- The summary line says how many shots were **matched** by a photo, how many are **missing** (no photo near), the
  photos **far from every shot** (extra) and those **repeated** (a second photo at a shot already taken), the median
  offset in plan and, for DJI drones, the median height difference (from the `RelativeAltitude` of DJI's XMP; other
  drones give no height difference).
- The red layer *Piano di volo: scatti mancanti* shows the missing shots; in the workspace tree the plan gets a child
  *Comparison with the flight* (double click: the plan active, the obtained coverage shown, the pane opened).
- The positions are the program's camera references: EXIF GPS, a positions file, PPK. With RTK/PPK the offsets are
  real; with a plain GPS (± 5 m) the median offset is mostly the GPS error.
- **Coverage obtained**: with the photos **aligned** and a projected system, the plugin counts how many aligned photos
  see each cell, on the grid of the predicted coverage; **Show the coverage obtained** shows it. *Cells worse than
  predicted* = predicted at or above the threshold and obtained under it. Cells outside the
  reconstruction are **not measured**: their number is given, and they are left without a value on the map.
- **Limits**: the obtained coverage has no occlusion either, and it has been tested only on synthetic data, never on a
  real aligned flight yet. The comparison becomes *To do again* when the plan or the photos change (added, moved, aligned
  again, switched off), after a new alignment, a new DEM or new positions.

The flight sheet (Excel/HTML) gets a *Comparison* page while the comparison is current.

## Exports per drone family { #exports }

**Heights.** All the drone files carry heights **above the takeoff point**, and every file says so in its own way. A
plan that follows the terrain keeps **one height per point** in every drone format.

| Format | For | Height reference written in the file |
|---|---|---|
| DJI WPML KMZ (one per leg, or all legs in a zip) | DJI Pilot 2, FlightHub 2, DJI Fly (Mini 4 Pro) | `heightMode` / `executeHeightMode` `relativeToStartPoint`, `coordinateMode` `WGS84`, one `executeHeight` per waypoint |
| DJI Fly: ready folders to drag (zip) | DJI Fly on the RC 2 / RC Pro or an Android phone | the KMZ above, renamed as the mission it replaces |
| QGroundControl `.plan` | ArduPilot and PX4 drones, QGroundControl | items frame 3 (`MAV_FRAME_GLOBAL_RELATIVE_ALT`), `AltitudeMode` 1, `globalPlanAltitudeMode` 1 |
| Mission Planner `.waypoints` | ArduPilot, Mission Planner | the `.plan` commands, frame 3; home row at the takeoff with altitude 0 |
| Parrot `.mavlink` | Anafi, Anafi USA, Anafi Ai (FreeFlight 7, GroundSDK) | frame 3 |
| Litchi CSV | DJI drones without waypoints in DJI Fly (Mini 2, Air 2S, Mavic 2, Phantom 4...) | `altitudemode` 0 (above the takeoff) |
| KML of the plan | Google Earth, QGIS | `altitudeMode` `absolute` = ground of the takeoff + height above the takeoff; the takeoff height and its source written in the file (0, with a note, when unknown) |
| GeoJSON of the plan | GIS | z = height above the takeoff, `riferimento_quote` = `decollo` |
| GeoPackage of the plan | GIS, in the chunk's system | `quota_decollo_m` = height above the takeoff; Z = ground of the takeoff + it in the height system of the DEM used; a `riferimento_quote` column says which |
| Areas in KML for the DJI apps (experimental) | apps that make their own strips | only the outline of a grid; the values to set again are in its description |
| Flight sheet (Excel, HTML), maps of the legs (zip) | whoever flies | every waypoint with its height above the takeoff; one map per leg over the chunk's orthophoto (grey without one) |

The drone never reads KML, GeoJSON or GeoPackage: they are for looking at the plan and keeping it.

**Proven and not proven.** The DJI Fly KMZ of the Mini 4 Pro is the file the planner was tried with on the real drone.
The other files follow each format's specification and are checked by the tests, but they **have not been imported in
the real apps yet**: WPML in DJI Pilot 2 and FlightHub 2, Litchi, QGroundControl, Mission Planner, Parrot and the area
KML. In particular the **one height per point** that QGroundControl, Mission Planner, Parrot and Litchi get from a plan
that follows the terrain has not been tried in those apps: the export warns about it. The same holds for DJI Pilot 2 and
FlightHub 2: the WPML KMZ of the Enterprise drones carries the height of every point both in the route that is flown
(`waylines.wpml`) and in the template that Pilot 2 edits (`template.kml`), but **the heights per point in Pilot 2 are
untried**. **Try every file in the real app, and look at the route there, before flying.**

How to load them (the steps of the apps may change with their versions):

- **DJI Pilot 2** (Mavic 3E/3M/3T, Matrice 30, 4E/4T, 300/350): copy the KMZ of the leg to the remote controller (USB or
  microSD), *Flight Route > Import Route (KMZ/KML)*; check height mode and speed before flying. **FlightHub 2**: import
  the KMZ in the routes library and send it to the dock or the controller.
- **DJI Fly** (Mini 4 Pro and the drones with waypoints in DJI Fly): DJI Fly does not import files - see
  [DJI Fly](#djifly).
- **Litchi** (iOS, Android): import the CSV in the Litchi Mission Hub (on the web), save the mission, open it in the
  app.
- **QGroundControl**: in the *Plan* view, *File > Open*, choose the `.plan`, then upload it to the vehicle.
- **Mission Planner**: *Plan > Load WP File*, choose the `.waypoints`, then *Write WPs*.
- **Parrot**: FreeFlight 7 imports the `.mavlink` from the share sheet of the phone or tablet; GroundSDK loads it
  directly.

## DJI Fly { #djifly }

DJI Fly does not import files: it keeps each mission as a KMZ in a folder named with a code, in
`Android/data/dji.go.v5/files/waypoint/` on the RC 2, the RC Pro and Android phones (on an iPhone it cannot be reached
from the computer). The plugin replaces those files with the legs:

1. In DJI Fly, create **one placeholder mission per leg** (two waypoints are enough) and save them.
2. Connect the remote controller to the computer. **Copy into DJI Fly...** finds the waypoint folder by itself when the
   device is visible as a path (Linux, an SD card, a path you paste); on Windows it reaches the MTP device through the
   system shell (PowerShell).
3. The plugin fills the safe slots first (placeholder missions, the earlier legs of this plan), says what each slot
   holds, asks before overwriting a mission of yours or a leg of another plan, and keeps a copy of every file it replaces in
   `<chunk data>/flightplan/djifly_backup/`.
4. Where there is no path (a Mac with the RC 2), export **DJI Fly: ready folders to drag (zip)** and drag the folders
   into the waypoint folder with a file manager for Android devices.

In DJI Fly the missions keep their placeholder names (DJI Fly names them by the time they were made): the plugin says
which name holds which leg. The copy on Windows through MTP and PowerShell has **not been tried yet on a real remote
controller**.

## Command line { #command-line }

```bash
ppg plugin flightplan cameras                                   # the cameras of the list
ppg plugin flightplan design scavo.ppg --area area.kml --goal orto --value 2 --terrain
ppg plugin flightplan design scavo.ppg --xy "x,y;x,y;x,y" --camera mini4pro --margin 10 --double
ppg plugin flightplan design scavo.ppg --bbox W,S,E,N --exif foto.jpg --takeoff LAT,LON
ppg plugin flightplan show scavo.ppg                            # the numbers of the plan (JSON)
ppg plugin flightplan terrain scavo.ppg --detail fine           # --tiles: public tiles; --off: one height per block again
ppg plugin flightplan targets scavo.ppg --csv target.csv --epsg 3003 --metres 10
ppg plugin flightplan targets scavo.ppg --markers               # --estimated, --remove, --tiles (ground on the tiles)
ppg plugin flightplan coverage scavo.ppg --cell 0.5 --low 5
ppg plugin flightplan export scavo.ppg --format kmz-zip --out volo.zip
ppg plugin flightplan djifly scavo.ppg --zip pronte.zip --missions UUID,UUID
ppg plugin flightplan compare scavo.ppg --max-distance 3
ppg plugin flightplan list scavo.ppg                            # use ID, remove ID, add --plan piano.json
ppg plugin flightplan design --coords "lat,lon;lat,lon;lat,lon" --plan piano.json   # without a project
```

`--goal` is one of `orto`, `dem`, `modello3d`, `ispezione`, `ricognizione`, `gsd`; `--camera` an id of `cameras`, or
`--exif PHOTO`, or `--manual NAME,WIDTH_MM,HEIGHT_MM,FOCAL_MM,PIXELS`; `--mode per_punto|intervallo`; `--obliques` /
`--no-obliques`. `--format` is one of `kmz`, `kmz-zip`, `djifly-zip`, `plan`, `waypoints`, `mavlink`, `litchi`, `kml`,
`geojson`, `gpkg`, `area-kml`, `sheet-xlsx`, `sheet-html`, `maps-zip`. `--id` chooses a plan of the project (default
the active one); `--plan FILE` works on a plan JSON file instead, for `design`, `show`, `terrain`, `targets`, `export`, `djifly` (and
`add` puts that file into the project); a plan JSON of the Photo3D planner loads as it is. `coverage` refuses `--plan`
(the coverage is kept in the project), and `compare`, `list`, `use` and `remove` work on the project only.

## Files

In `<chunk data>/flightplan/<plan id>/`: the coverage (`copertura_count.tif`, `copertura_gsd.tif`,
`copertura_rgba.tif`), the comparison (`confronto_confronto.json`, `confronto_mancanti.gpkg` with the table
`scatti_mancanti`, `confronto_copertura_ottenuta.tif` and `_rgba.tif`). Derived files carry the stamp of the plan and
are made again when the plan changes. The exports go where you choose; the copies of DJI Fly missions in
`<chunk data>/flightplan/djifly_backup/`.

## Limits

- No **occlusion** in the coverage, predicted or obtained; cells outside the DEM are flat at the takeoff.
- The heights per point of QGroundControl, Mission Planner, Parrot and Litchi, the WPML in DJI Pilot 2 / FlightHub 2,
  the area KML and the DJI Fly copy through MTP on Windows are **not yet proven** in the real apps and controllers: try
  them before flying.
- The obtained coverage has not yet been tried on a real aligned flight.
- Without an orthophoto in the project the maps of the legs have a **grey background** (no third-party map tiles).
- **Close-up shooting** (a trench, a wall face) is not in 0.1.0.
- Airspace, permits, obstacles and weather are the pilot's.
