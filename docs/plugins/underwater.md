# Underwater

The *Underwater* plugin helps with surveys made under water - a wreck, a submerged structure, finds on the sea
bed - from the photos of a diver or the images of an ROV. It does three things the rest of the program does not:
it gives back the colours that the water takes away, it says which photos and which frames of a video are worth
using, and it uses the depth of the camera, read by a pressure sensor or a dive computer, where a survey on land
would use the GPS.

This page describes version 0.1.1 of the plugin, which needs pyarchinit-photogrammetry 1.3.4 or later. Its README
and source code are in the
[plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/underwater).

<figure markdown="span">
  ![A photo of the Sea-thru dataset as developed and with its colours restored](../img/plugins/underwater/restored.jpg)
  <figcaption>A coral head at 1-3 m from the camera in the Red Sea (Sea-thru dataset D3, Akkaynak and Treibitz):
  the photo as the camera developed it, and with its colours restored by the plugin with the range of every
  pixel. Photo: D. Akkaynak, T. Treibitz, [Sea-thru dataset](https://www.kaggle.com/datasets/colorlabeilat/seathru-dataset), [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/); developed and restored by us, the figure is under the
  same licence.</figcaption>
</figure>

## In short

1. Bring in the photos: *Plugins > Underwater > Frames from a video...* or *Frames from an image sequence...* for
   video and ROV logs, *Add photos* of the program for still photos. An ROV camera calibrated with Kalibr: its
   YAML with *Calibration (Kalibr)...*.
2. *Score the photos* in the *Underwater* pane, look at the photos flagged, *Exclude the flagged*.
3. If the photos are dark, milky or very blue, make the *quick correction* (with *CLAHE* for the alignment) and
   *Use the corrected photos* before aligning them.
4. Align the photos (*Workflow > Align photos*). For a sequence, turning off the *Generic preselection* in the
   alignment dialog (all the pairs) helps on sand and other surfaces with little texture.
5. Import the depth log, *Match to the photos*, *Estimate the offset* if the clocks differ, *Level and scale*.
6. Build the depth maps (*Workflow > Build dense cloud...*, engine *PatchMatch multi-view*, *Depth maps only*),
   then *Physics-based restoration*: *Correct the photos* and *Use the corrected photos*.
7. Build the dense cloud, the DEM, the orthophoto and the textured model as usual: they use the restored photos,
   in metres, with heights below the surface of the water.

## What it does

| Part | What it gives | Useful for |
|---|---|---|
| Photo scores | three numbers per photo - quality (sharpness), contrast, veil - flags with the reason, the flagged photos excluded or included again with one click | leaving out the photos blurred by the swell, those taken in a cloud of sediment, those too dark |
| Frames from videos and sequences | the frames worth keeping, chosen by how far the view moved and by how sharp they are, with the time of each | GoPro videos of a diver, the 10-30 images a second of an ROV |
| Quick correction | red rebuilt from the green, white balance, contrast, optionally local contrast (CLAHE) | photos to align, a look at the site before the processing, photos without depth maps |
| Physics-based restoration | the colours the surface would have without the water between it and the camera, from the range of every pixel | the orthophoto and the textured model of a site with its true colours, the same colour for the same object near and far |
| Depth logs | the depth of every photo from a pressure sensor or a dive computer, matched by time | a survey without GPS |
| Levelling | the chunk level (the vertical of gravity), in metres, with heights equal to minus the depth | DEMs, sections and measures in metres, comparable from one dive to the next |
| Known positions | X and Y from an acoustic positioning system (USBL) or a few photos placed by hand | placing the survey on a map |

## What you need first { #before }

* **The plugin**, installed from the program: see [Plugin guides](guides.md#install). It needs nothing else.
* **Photos or videos** of the survey. The plugin reads JPEG, PNG and TIFF (8 or 16 bit); RAW files must be
  developed first, with as little processing as possible (no strong colour matrix: a camera that clips the red
  channel to zero leaves nothing to restore).
* For the restoration, **an aligned chunk with its depth maps** (*Workflow > Build dense cloud...* with *Depth maps
  only*, or the dense cloud itself; see [step 5](#program)). The quick correction needs nothing.
* For the levelling, **a depth log** and **the time of the photos**: the frames the plugin extracted carry it;
  still photos have it in their EXIF or in their names.

## 1. Photos and frames { #photos }

### The scores

*Score the photos* measures every photo on a copy at most 1000 pixels wide:

* **Quality** - the sharpness of the program (the finest detail over the detail a few pixels wide, in the cells
  of a grid that have some texture) over the sharpness of the sharpest photos of the chunk. Under 0.5 the photo is
  blurred compared with the others: motion, focus, the swell.
* **Contrast** - how far apart the dark and light parts of the photo are (between the 2nd and 98th percentile of
  the lightness, 0-1). Low in a milky water and in photos too dark.
* **Veil** - how much light the water adds between the camera and the scene. In a clear photo every patch has
  some dark pixels (shadows, crevices); in a hazy one even the darkest pixels are lifted by the backscatter. It is
  the median of the darkest of green and blue in windows of 15 pixels, over the brightest light of the photo,
  0-1.

The three thresholds stay in the pane (0.5, 0.12, 0.6 at first); changing them flags the photos again at once,
without measuring them again. The table lists the photos flagged with the reason; *Exclude the flagged* disables
them, as *Disable photos* does - the alignment and every product leave them out - and *Include again* enables
the photos the plugin disabled. *CSV table...* writes scores, times and depths of every photo. The thresholds are
a starting point: on a sandy bottom without texture the sharpness is low everywhere, and a photo that holds the
alignment together is worth keeping even if it is not the sharpest.

### Frames from a sequence

An ROV logs images many times a second: hundreds of frames for every one a reconstruction needs, many blurred by
the motion. *Frames from a sequence...* asks for:

* the **folder** of the images (in the order of their names, frame2 before frame10);
* the **table of the times** (optional): a CSV with the time of every frame - the file name or the frame number,
  and the time in Unix seconds (s, ms, us or ns) or as a date. Without it the time comes from the file names
  (`1542883311752336025.png`, `20190312_142355_123.jpg`) or from the EXIF;
* the **overlap** between frames kept (0.8: the next frame is kept when the view has moved by a fifth of its
  width since the last one); the **least sharpness** against the neighbours (0.6); the **step** (analyse one frame
  in N); a time limit (a frame at least every so many seconds, also when the view barely moves).

Every frame is matched with the one before (ORB points and a similarity found by RANSAC on a copy 480 pixels
wide), so the frames of a stretch are chained and the plugin knows how far the view has moved since the last
frame kept, also when the ROV turns or moves closer. The next frame kept is the sharpest of those that moved by
half to all of the step, leaving out the frames much less sharp than their neighbours. An ROV hovering gives no
new frames; where the frames cannot be matched (a jump, a cloud of sediment, the lights off) a new stretch starts.
The frames are not copied: they become photos of the active chunk where they are, with their time.

### Frames from a video

*Frames from a video...* uses the extraction of the program (a frame each time the view has moved, the sharpest of
the moments after) and gives every frame its time: the creation time written in the MP4 or MOV file - the clock of
the camera, often local time written as UTC - or a start time typed in, plus the time in the video. The frames are
written in a folder beside the video.

### The calibration of an ROV camera

ROV cameras are often calibrated with [Kalibr](https://github.com/ethz-asl/kalibr). *Calibration (Kalibr)...* reads
its YAML (pinhole with radtan distortion, or equidistant for fisheye lenses) and gives it to the photos of the same
size, held fixed in the alignment. Kalibr, like OpenCV, puts the centre of the first pixel at 0, the program at
0.5: the plugin moves the principal point by half a pixel.

## 2. Colours { #colours }

Water absorbs the red light within a few metres, then the green; and the particles in it scatter the light of the
sun or of the lamps back towards the camera, a bluish veil that grows with the distance. A photo under water is
therefore not only bluer than the same scene in air: it is bluer and hazier the farther each surface is from the
camera. The same amphora is reddish when close and blue-grey a few metres away, and an orthophoto made of photos
taken at different distances is a patchwork.

### The physics-based restoration

With the **range of every pixel** - how far, along its ray, the surface it shows is - the effect of the water can
be estimated and taken away. The plugin follows the image formation model of Sea-thru (D. Akkaynak and
T. Treibitz, CVPR 2019), written from the paper:

* the ranges come from the depth maps of the program. They are maps of the undistorted photos: the plugin takes
  every pixel of the photo to its ray with the camera model, then to the depth map. They are in metres when the
  chunk is (georeferenced, scaled with scale bars, or levelled with the depths); otherwise in the units of the
  alignment, which is enough for the restoration;
* in every band of ranges the darkest pixels are almost only the light of the water: the **backscatter** of every
  colour channel is fitted to them, as a curve that grows with the range, and subtracted from the photo;
* what is left is the light coming from the surfaces. Its local average among pixels at a similar range is the
  light reaching that range; how it falls with the range gives the **attenuation** of every channel and the
  exposure of every photo;
* the local average mixes the water with the colours of the scene when they change with the distance - a yellow
  coral near the camera, pale sand far away. So, where photos see the same scene, the plugin matches their points
  and compares the colour of every point seen from two distances: the ratio between the channels does not depend
  on the colour of the point nor on the exposure, only on the water. These **points seen twice** give the
  differences of attenuation between red, green and blue; the local average gives only the attenuation of green;
* the attenuation is undone pixel by pixel (at most twelve times: far pixels are mostly noise); the colour of the
  light is taken from the edges of the restored photos (the *grey-edge* hypothesis of van de Weijer and colleagues:
  the average edge is grey - much less fooled than the average colour by a large surface of one colour), and the
  result is brought to a middle brightness.

The coefficients can be fitted **once for the survey** - on a sample of up to 30 photos together: the water is the
same, so every photo of a dive gets the same correction, and the orthophoto and the texture have no seams of
colour, while the exposure and the strength of the backscatter of each photo stay its own - or **for every photo**,
which follows a water that changes (another dive, a cloud of sediment) at the price of photos that match less; the
differences between the channels still come from the points the photos of the survey share. The photos of the
sample are paired with their nearest ones when the chunk is aligned, otherwise with those next to them in the
list (a sequence, a video).
The pane shows the coefficients per metre of the survey (attenuation βD and backscatter βB of red, green and blue).

Pixels the depth maps do not reach (the open water above the horizon, the margins) take the range of the nearest
surface, smoothed: there the restoration is only plausible.

<figure markdown="span">
  ![The grey scale of a colour chart under water](../img/plugins/underwater/chart.jpg)
  <figcaption>The colour chart of a Sea-thru D3 photo at 0.9 m: as developed, with the quick correction, restored.
  The right column is a grey scale: restored, its patches are grey. Photo: D. Akkaynak, T. Treibitz, [Sea-thru dataset](https://www.kaggle.com/datasets/colorlabeilat/seathru-dataset),
  [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/); cropped, developed and corrected by us, the figure is under the same licence.</figcaption>
</figure>

### The quick correction

Without ranges - before the alignment, or on photos without depth maps - the quick correction rebuilds the red from
the green where the red is missing (C. O. Ancuti and colleagues, IEEE TIP 2018), optionally the blue in green water,
balances the white so that the average is grey, stretches the lightness, and optionally raises the local contrast
(CLAHE). The means and gains come from a sample of the survey together, so the photos stay alike. It cannot tell
near from far: a surface far away stays bluer than the same near by.

CLAHE changes the colours, but it can make the alignment possible: on the dark, low-contrast photos of a deep site
the keypoints of the program are found again on the sand. On the AQUALOC sequence below, the alignment registered
63 of 139 frames with the original images and 136 of 139 with the quick correction with CLAHE.

### Preview, copies and the photos in use

*Preview* shows one photo before and after, with the method and options chosen. *Correct the photos* writes a copy
of every photo - same name, size and format, the EXIF kept - in the folder `underwater/colour` of the chunk; the
originals are never touched. *Use the corrected photos* points the photos of the chunk to the copies: the
orthophoto, the texture, the depth maps and a new alignment read them from then on. *Use the originals* points them
back. Correcting again writes over the copies.

## 3. Depth instead of GPS { #depth }

Under water there is no GPS. The depth of the camera, measured by the pressure sensor of an ROV or by the dive
computer of the diver, is the measure that stays.

### The depth log

*Import the log...* reads:

* CSV and text tables with a time column - Unix time in s, ms, us or ns, ISO dates, clock times with a date column,
  seconds from the start - and a depth column in metres or feet, or a pressure column (bar, mbar, dbar, kPa, Pa,
  psi), turned into depth with the density of sea water (1025 kg/m³) or fresh water (1000 kg/m³) chosen in the
  pane;
* the exports of dive computers, as Subsurface writes them (date, time, sample time, sample depth);
* UDDF files, the exchange format of dive logs;
* the depth files of the AQUALOC dataset.

The depths are matched to the photos by time: the time of the frames the plugin extracted, the time in the file
names, or the EXIF time with fractions of a second and time zone. The **time offset** is what to add to the clock of
the camera to read the clock of the log (a camera an hour ahead: -3600). When the clocks were not synchronised,
*Estimate the offset* tries every offset over a quarter of an hour on each side and keeps the one with which the
heights of the aligned cameras explain best the changes of depth - not the smallest difference in metres: an
offset that puts the photos on a flat stretch of the log (the diver hovering, the bottom) gives nearly equal
depths that anything fits. Only the offsets that give a depth to (nearly) every photo are compared.

### Level and scale

The depths of three or more aligned cameras say how the chunk is turned (the vertical is the direction along which
the depth of the cameras changes), how large it is (how many metres of depth a unit of the alignment is) and how
deep (the height of every camera is minus its depth). The plugin finds the three at once, with weights that leave
out a few wrong matches, and puts the chunk in a local system in metres: **Z = -depth**, the surface of the water at
0. X, Y and the direction of north are arbitrary - the depth says nothing about them - and are kept as they were.
The pane shows the root mean square difference between the heights of the cameras and their depths, the largest
one, the cameras out of line, and the uncertainty of the vertical.

The program's own references of the cameras are positions in three coordinates with one accuracy for the three: a
reference with the height alone cannot be held there. So the levelling is done by the plugin, which keeps it in the
project; a new alignment resets it, and the pane and the workspace say *Levelling to redo*.

When the depth of the cameras changes too little - a survey flown at a constant depth - the depths cannot give the
scale: then it comes from the control scale bars of the chunk (*Scale: from the scale bars*), and the vertical from
the plane of the cameras.

### Known positions

An acoustic positioning system (USBL) or a few photos placed by hand give X and Y: *Known positions (USBL)...* reads
a table by photo name or by time (interpolated), in a reference system given as an EPSG code. After the levelling a
horizontal similarity puts the chunk on them: north and the position on the map.

## Step by step in the program { #program }

The pictures of this section come from 20 photos of the Sea-thru D3 dataset (a coral head in the Red Sea,
photographed from 1-3 m; D. Akkaynak, T. Treibitz, [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/))
and from a depth log **simulated for the example**: the dataset has none, so the log was made from the heights of
the aligned cameras, with 1.5 cm of noise, one sample a second and its clock 7 seconds ahead of the camera's.

### The pane

*Plugins > Underwater > Underwater pane* opens it; it stays on the right of the window, among the panes of the
tools (*US proxies*, *Multispectral*, *Thermal*). It has three boxes, in the order of the work - **1. Photos and
frames**, **2. Colours**, **3. Depth instead of GPS** - and under the buttons of each box a line says where the
active chunk stands.

<figure markdown="span">
  ![The window with the Underwater pane on the right](../img/plugins/underwater/pane-window.png)
  <figcaption>The window with the <em>Underwater</em> pane open on the right, before anything is done: 20 aligned photos of the Sea-thru D3 dataset.</figcaption>
</figure>


### 1. Score the photos

1. Click *Score the photos*. Every photo gets three numbers: *quality* (sharpness against the sharpest photos of
   the chunk), *contrast* (the spread of its lightness), *veil* (the haze of the suspended sediment).
2. The three thresholds above the buttons decide which photos are flagged. Changing one flags the photos again at
   once, without scoring them again.
3. The table lists the flagged photos with the reason. *Exclude the flagged* turns them off: they stay in the
   chunk, greyed, and *Include again* turns them back on. *CSV table...* writes the scores of every photo.

<figure markdown="span">
  ![The box Photos and frames after the scoring](../img/plugins/underwater/pane-photos.png)
  <figcaption>The photos scored, with <em>Least quality</em> raised to 0.85: five photos flagged as blurred. With the default 0.50 none of these photos is flagged - D3 is a good set.</figcaption>
</figure>


### 2. A first look at the colours

1. Set *Method* to *Quick correction*, choose a photo in the list under the options, click *Preview*: the photo
   as it is and corrected.
2. When the photos are too blue or too dark to align, tick *CLAHE*, then *Correct the photos* and *Use the
   corrected photos* **before** the alignment. The photos of the example align as they are, so the step stops at
   the preview.

<figure markdown="span">
  ![The preview of the quick correction](../img/plugins/underwater/pane-quick.png)
  <figcaption>The preview of the quick correction on a photo with the colour chart: red rebuilt from the green, white balance.</figcaption>
</figure>


### 3. Align the photos

*Workflow > Align photos...* as for any survey (see [In short](#in-short), step 4). The 20 photos of the example
align in a few seconds.

### 4. The depth log, the time offset, the levelling

1. In the box *3. Depth instead of GPS* choose the *Water* (sea or fresh, for the logs of pressure) and leave
   *Time offset* at 0 if you do not know it.
2. *Import the log...* and choose the file. The depths are matched to the photos at once, with the offset in the
   box. Every photo gets a depth even with a wrong offset - the depth of another moment - so go on to the next
   step.

<figure markdown="span">
  ![The depth log imported, with no time offset](../img/plugins/underwater/pane-depth-log.png)
  <figcaption>The log imported with the offset at 0: 114 samples between 7.61 and 8.38 m, a depth for each of the 20 photos, the chunk not levelled yet.</figcaption>
</figure>


3. *Estimate the offset* tries the offsets within a quarter of an hour of the one in the box and keeps the one
   with which the heights of the cameras explain the depths best; it writes it in the box and matches the depths
   again. In the example it finds 7.08 seconds for a clock 7 seconds ahead.
4. *Level and scale*: the chunk becomes level, in metres, with Z = -depth. The line under the buttons gives the
   root mean square difference between the heights of the cameras and their depths (1.1 cm here), the largest
   (5.2 cm) and the uncertainty of the vertical (0.49°).

<figure markdown="span">
  ![The offset estimated and the chunk levelled](../img/plugins/underwater/pane-levelled.png)
  <figcaption>The offset estimated (7.08 s) and the chunk levelled and scaled on 20 cameras: 1.1 cm RMS, uncertainty of the vertical 0.49°.</figcaption>
</figure>


In the example the cameras stay within 0.8 m of depth, and the scale found differs by 7 % from the one of the metric
depth maps of the dataset: the less the depth changes, the less it says about the scale. With small depth changes,
a scale bar on the site and *Scale: from the scale bars* do better.

### 5. Depth maps for the restoration

The restoration takes the range of every pixel from the depth maps of the program. Build them with *Workflow >
Build dense cloud...*, **Engine: PatchMatch multi-view** and **Depth maps only** ticked.

<figure markdown="span">
  ![The dialog of the dense cloud set to build the depth maps only](../img/plugins/underwater/dialog-depth-maps.png)
  <figcaption><em>Workflow > Build dense cloud...</em> set for the restoration: PatchMatch, depth maps only.</figcaption>
</figure>


The automatic engine uses the stereo of photo pairs for photos taken close to the subject: fast, but on the photos
of the example it covers about a fifth of each photo, PatchMatch three quarters (in 9 seconds on 20 photos). Where
the maps do not reach, the range is taken from the nearest surface and the colours are only plausible: after the
restoration the plugin warns when the maps cover less than 40 % of the photos.

### 6. Restore the colours

1. Set *Method* to *Physics-based restoration* and *Coefficients* to *one for the survey* (the same correction for
   every photo: no seams of colour in the orthophoto).
2. *Correct the photos*: the model of the water is fitted on the survey, then every photo is restored. *Preview*
   shows a photo before and after with that model.
3. *Use the corrected photos*: the orthophoto, the texture and the depth maps read the copies from then on; *Use
   the originals* goes back.

The line under the buttons says how many photos are corrected, which are in use, and the coefficients of the
water per metre - attenuation βD and backscatter βB of red, green and blue - when the chunk is levelled in metres.

<figure markdown="span">
  ![The box Colours after the restoration](../img/plugins/underwater/pane-restored.png)
  <figcaption>The restoration: the preview of a photo, 20 corrected photos in use, the attenuation from the points seen twice and the coefficients of the water per metre.</figcaption>
</figure>


### 7. The products

Under the chunk, in the *Workspace*, the *Underwater* node sums up what was done: the corrected photos and whether
they are in use, the flagged photos, the photos with a depth, the levelling. Its items open the pane; their right
click menus repeat the main actions. The dense cloud, the DEM, the orthophoto and the textured model are then built
as usual: they use the restored photos, in metres.

<figure markdown="span">
  ![The Underwater node in the workspace](../img/plugins/underwater/pane-tree.png)
  <figcaption>The <em>Underwater</em> node under the chunk in the workspace.</figcaption>
</figure>

## From the command line

```bash
ppg plugin underwater frames-sequence scavo.ppg raw_data/images_sequence_6 --times raw_data/img_sequence_6.csv --overlap 0.9
ppg plugin underwater calibration scavo.ppg archaeo_camera_calib.yaml
ppg plugin underwater score scavo.ppg --quality 0.5 --contrast 0.12 --veil 0.6 --exclude
ppg plugin underwater quick scavo.ppg --clahe --alpha 0 --no-balance --use
ppg plugin underwater restore scavo.ppg --per survey --use
ppg plugin underwater use scavo.ppg original
ppg plugin underwater depth scavo.ppg depth_sequence_6.csv --offset 0
ppg plugin underwater offset scavo.ppg --span 900
ppg plugin underwater level scavo.ppg --positions usbl.csv --crs 32633
ppg plugin underwater status scavo.ppg
```

## Tried on real data { #tests }

**AQUALOC, archaeological site, sequence 6** (M. Ferrera, V. Creuze, J. Moras, P. Trouvé-Peloux, *AQUALOC: An
Underwater Dataset for Visual-Inertial-Pressure Localization*, The International Journal of Robotics Research,
2019): an ROV over an amphora wreck at 387-391 m, 3385 monochrome frames of 968 x 608 pixels at 20 a second, the
depth of its pressure sensor, and a trajectory computed with COLMAP as reference.

* 139 frames chosen out of 3385 in 8.5 seconds, with an overlap of 0.9; the Kalibr calibration held fixed.
* Aligned: 63 of 139 with the original frames, 136 of 139 after the quick correction with CLAHE.
* Levelled and scaled with the depths: the heights of the cameras differ from their depths by 2.3 cm (root mean
  square; 6.1 cm at most) over 3.9 m of depth; the scale is known to 0.5 %; the vertical lies within 0.18° of the
  vertical that the same depths give to the reference trajectory, and the positions of the cameras lie within
  5.8 cm of the reference over 38 m of path.
* A camera clock shifted on purpose by 7.3 seconds: the offset found is 7.33 seconds.
* The DEM and the orthophoto, in metres, lie between 389.7 and 394.2 m below the surface.


**Sea-thru D3** (D. Akkaynak, T. Treibitz, CVPR 2019): 68 RAW photos of a coral head in the Red Sea, with a colour
chart, and depth maps in metres made with Metashape. Developed with the white balance of
the camera and no colour matrix, 23 photos were restored with their depth maps, the coefficients fitted on the 23
together (2.8 minutes).

* The grey scale of the chart, 0.9 m from the camera, has a mean chroma (CIELAB a*b*, 0 = perfectly grey) of 3.7
  restored, against 16.9 in the photos as developed and 6.1 with the quick correction; with one model per photo,
  4.0.
* With the local average alone (Sea-thru's illuminant, before the points seen twice and the grey edges) blue came
  out as the most attenuated colour and the chart at 19: the yellow coral near the camera and the pale sand far
  away mix the colours of the scene with the water.
* At 1-3 m from the scene the colours hardly change with the distance: the same points seen from distances at least
  25 % apart differ by a median ΔE a*b* of 1.56 as developed and 1.67 restored. Most of the gain is in the colour of
  the light.
* The whole chain in the program on 20 of the photos: aligned 20 of 20 in 26 seconds, depth maps in 3 seconds. Our
  depth maps and those of Metashape agree up to the scale, with a spread of 0.4 %. Restored with our ranges the grey
  scale has a mean chroma of 7.4; with Metashape's maps on the same photos 4.2 (the differences between the
  channels are found on more points there).
* D3 was taken from a camera moving round a coral head at 1-3 m: an orthophoto of it is not meaningful, but the
  orthophoto of the restored photos has the same colours from photo to photo.

## Limits

* The restoration assumes the light of the sun from above, the same at every place of the photo. The lamps of an
  ROV light the near things more than the far ones and the centre more than the edges: the model takes some of it
  as attenuation, but a deep site lit by lamps is not what it was made for.
* Red absorbed completely - a camera that wrote zero in the red channel - cannot be given back.
* The colour of the light comes from the edges of the scene: a scene almost all of one colour and without detail
  stays hard.
* The depth gives the vertical and the scale, not north nor the position: without known positions the survey stays
  in a local system.

## References

* D. Akkaynak, T. Treibitz, *Sea-thru: A Method for Removing Water From Underwater Images*, CVPR 2019.
* C. O. Ancuti, C. Ancuti, C. De Vleeschouwer, P. Bekaert, *Color Balance and Fusion for Underwater Image and Video
  Enhancement*, IEEE Transactions on Image Processing 27(1), 2018.
* J. van de Weijer, T. Gevers, A. Gijsenij, *Edge-Based Color Constancy*, IEEE Transactions on Image Processing
  16(9), 2007.
* P. Drews Jr., E. do Nascimento, F. Moraes, S. Botelho, M. Campos, *Transmission Estimation in Underwater Single
  Images*, ICCV Workshops 2013.
* M. Ferrera, V. Creuze, J. Moras, P. Trouvé-Peloux, *AQUALOC: An Underwater Dataset for Visual-Inertial-Pressure
  Localization*, The International Journal of Robotics Research, 2019.
