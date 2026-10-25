# Photos and markers

Everything that goes into the alignment: the photos and their masks, the markers measured in them, the scale
bars between markers.

## Adding photos

| Command | What it adds |
| --- | --- |
| *Workflow > Add photos...* / *Add folder...* | photos into the active chunk; a flight split over several folders (the rolls of a camera, `100MEDIA` and `101MEDIA`, whose numbering starts again) is added folder after folder or as the parent folder in one go: names that repeat take their folder as a prefix, files already in the chunk are left out |
| *Workflow > Add video...* | frames chosen when the view has moved by a share of its width (the sharpest around that moment) or one every few seconds, written in a folder beside the video; with the `.SRT` of a DJI drone beside the video every frame gets its GPS position |
| *Workflow > Add 360° photos...* | every equirectangular photo becomes the six faces of a cube (the faces to keep - *Down* often shows only the tripod - their width in degrees and size), written in `facce_360` beside the photos; in the alignment the faces of one photo stay together as a rig |
| *Workflow > Relink photos (another folder)...* | the photos with the same names in another folder and its subfolders: a copy on an external disk of a Dropbox folder kept online only, a moved folder |

The files of a **multispectral camera** are recognised when you add photos: the window offers to add them as
captures and opens the *Multispectral* pane, so the bands are not aligned as if they were different views
(see [Multispectral and thermal](multispectral.md)).

A **fisheye lens** is recognised from the EXIF; otherwise *Lens model...* (right click in the *Photos* pane)
sets it for the sensors of the selected photos: normal, fisheye, strong distortion, none. The tooltip of a
face of a 360° photo names its photo.

!!! tip "Image quality"
    Right click in the *Photos* pane: *Image quality* gives the sharpness of every photo compared with the
    sharpest ones (in the tooltip; *blurred* under 0.5), and *Disable the blurred photos* leaves them out of
    the next alignment.

## Masks

The masked parts of a photo (red on it) are not used to align, in the depth maps, in the orthophoto or in
the texture. *Tools > Masks of the photos*:

* **draw** a mask on the photo shown: click the vertices, double click or ++enter++ closes, ++esc++ drops;
  *Remove the mask* or ++shift++ + double click gives the area back; *Show the masks* hides the red;
* **import** them from images, or from the alpha channel of the photos;
* **make** them from photos of the background without the object, from the colour of a backdrop, from the model
  (only the object stays) or with SAM 2.1 (the object at the centre of each photo);
* **export** them, or remove them (from the selected photos or all of them).

*Align photos* can also leave out the points that do not move between photos: the turntable with a fixed
camera.

## Markers

![A photo with its markers](../img/gui/view-photo.png)

The markers are the targets and the points measured in the photos; *Workflow > Detect coded markers...* finds
the printed coded targets by itself.

**A marker measured in one photo only** lies where its ray meets the surface (the dense cloud, else the tie
points: a local plane fitted around the ray, about 20 pixels wide). So it has coordinates at once, shows on the
model and on the orthophoto and can end a scale bar; measured in a second photo it is triangulated. On the
synthetic trench, one-photo markers are within 0.6 mm of the surveyed targets.

**Markers without a photo**, as Metashape's *Add Marker*:

* *Marker* in the 3D view: the view freezes; click a point of the model or the cloud (the nearest point shown
  under the cursor, refined along the line of sight on the full dense cloud);
* *Marker* in the drawing bar: click the orthophoto or the DEM (the height from the DEM at full resolution,
  else from the cloud along the vertical).

The new marker (`point 1`, `point 2`...) is projected on the photos that see it, not hidden by the surface: blue
flags, to drag in a photo to pin it. The markers are drawn on the orthophoto as the *Marker* layer.

### Guided placement

As in Metashape, a marker placed on one photo (or in 3D) gets **blue flags** on the other photos that see it (at
most 60, those where it falls nearest the centre first), and the *Photos* pane is filtered on it.

1. Open a photo with a double click: it is centred on the marker at full resolution.
2. Drag the blue flag onto the point, or double click it when it is right: it turns green.
3. ++page-down++ opens the next photo.

Every confirmation places the marker again (two green flags: triangulated) and moves the blue flags of the
other photos to where it is now. Blue flags are estimates, not measurements: they never move a marker that has
a green one, nor enter the camera optimisation.

*Refine the blue flags* (right click in *Reference*, for the selected markers or all of them, or *Workflow >
Refine the blue flags of the markers*) looks for the point of every blue flag in its photo by image
correlation, on the surface around the marker: the flags found become measurements (automatic, as detected
targets), the others stay blue to confirm by hand.

In *Reference* the right click also **merges** the selected markers (a coded target and the GCP placed by hand
on it) or **deletes** them, and *Move in the 3D view* places a marker again by a click on the model or the cloud
(for markers placed in 3D or on the orthophoto; those measured in the photos are moved in the photos).

## Scale bars

A scale bar is the known distance between two markers, as in Metashape.

1. In *Reference* select two markers (++ctrl++ or ++shift++ + click) and choose *Create scale bar between A and
   B...* from the right click, or press *Scale bar...* (the selected markers come first in the dialog).
2. The dialog asks for the distance in metres (between the centres of the targets), its accuracy (it weighs the
   bar when there are several), whether it is a check bar, and whether to update the scale at once.

| Kind of bar | What it does |
| --- | --- |
| **Control bar** | scales the chunk: the scale is the least-squares solution of *scale x length in the model = distance*, each bar weighted by `1 / accuracy²` (of two bars measured equally well, the longer one counts more). Without control points the chunk is scaled in local coordinates (CRS `LOCAL`): an object, a find, a wall, a trench |
| **Check bar** (*Check* ticked) | does not scale: its error tells how good the scale is |

!!! note "The distance of a bar is in 3D"
    With a height difference it is more than the distance in plan measured on the orthophoto.

**Georeferenced chunks**: a new bar is proposed as a check (it measures the scale without changing it). A
control bar on a chunk georeferenced elsewhere (an imported project) with no reference to fit it again scales it
about the bar, in its CRS; when the scale changes, the log says that the DEM and the orthophoto are still in the
old one and are to be built again.

**Photos with GPS**: when their positions cover less than ten times their accuracy (a phone or a drone over an
object or a trench) they are left out and the bars scale the chunk in local coordinates. Over a larger area (a
flight over a site), or with three or more control points, the photo positions, the control points and the bars
are fitted together, each weighted by `1 / accuracy²`: positions and points place and turn the survey, the scale
comes from all of them.

**The table of the bars**, under the markers in *Reference*, has a row per bar: *Use* (untick to leave it out),
name, markers, *Distance* and *Accur.* (double click to change them: the error follows at once), *Check*, the
distance measured on the model (*Estimated*) and the error, in red when it exceeds three times the accuracy (at
least 5 mm). The right click deletes the bars. The summary shows *Bars RMSE* (control) and *Check bars RMSE*, and
how the chunk was placed: scale from the bars, local coordinates, or scale from references and bars together.

The 3D view draws the bars between their markers, control bars in cyan and check bars dashed in orange, with
their length and error; the workspace lists them under the chunk.

### Importing and printing bars

* *Import bars...* (or *Workflow > Import scale bars (CSV)*): a text file with
  `marker_a, marker_b, distance[, accuracy[, check]]` per line, separated by commas, semicolons, tabs or spaces;
  markers by name (`target 12`) or target code (`12`); a header line is skipped and pairs whose markers are not
  in the chunk are listed in the log.
* *Tools > Target > Printable scale bar...*: one page per bar with two coded targets at a known distance (the
  last codes of the dictionary, 48-49 for `aruco_4x4_50`, so they never clash with target sheets and turntable
  mats), a 10 cm ruler to check that it was printed at 100 %, and the CSV of the bars next to the PDF. Glue the
  page on something rigid, photograph it with the object, detect the markers and import the CSV.
* Projects imported from Metashape bring their scale bars (distance, accuracy, check bars); WebODM tasks bring
  the camera poses and their GCPs as markers, so bars can be drawn between them.

### Bars in the optimisation

*Optimise cameras* uses the bars too, as Metashape does: with two control bars or more, or with control points,
the markers at the ends of the bars are fixed where the bars get their lengths (each moved as little as its
precision in the photos allows, `MARKER_PIXEL_ACCURACY` 0.5 px) and the block bends towards them as far as the
tie points allow; a single bar only gives the scale. The log reports the bar errors before and after.

??? info "With the engine *Own*"
    The bars are distances between their markers inside the adjustment itself, and the control points are
    pulled to their coordinates by their accuracy instead of being fixed. The GNSS positions of the photos can
    take part (with a bias per flight estimated with the block: an RTK base on a wrong point, an antenna offset)
    and so can the rolling shutter of drones (a velocity of every photo while its sensor is read). Over flat
    ground with nadir photos only, a vertical bias trades off with the focal length: GCPs or oblique photos are
    needed. The log reports the bias of every flight and the median velocity.
