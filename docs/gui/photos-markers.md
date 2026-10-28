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

## GNSS positions of the photos

Every photo has a position, where it comes from and how good it is. The *GNSS* pane (*Tools > GNSS > GNSS pane*,
on the right with *Multispectral* and *Thermal*) shows them and holds the commands that give photos a better
position than the GPS of their EXIF.

| Source | What it gives |
| --- | --- |
| The photos themselves | The GPS of the EXIF, with an accuracy of 5, 5, 10 m (no RTK data). The photos of a DJI drone with RTK also carry its state in the XMP (`RtkFlag`: 50 fix, 34 float, 16 single, 0 none) and its standard deviations, read when the photos are added; those of a multispectral camera take them from their bands |
| *Tools > GNSS > DJI MRK file...* | The `Timestamp.MRK` the drone writes on its card: the position of every shot, of the camera and not of the antenna (the lever arm of the shot is applied), with its ellipsoidal height, its RTK state and its standard deviations |
| *Tools > GNSS > PPK corrections...* (*Compute PPK...* in the pane) | The position of every shot of the `Timestamp.MRK` computed again from the RINEX of the drone and of a base: see [PPK](#ppk-corrections-from-a-base) below |
| *Tools > GNSS > Positions file...* | A text file with a line per photo, for a drone built by hand or positions computed by another program (a PPK solution, a total station): name, x, y, height and, when there are any, `sx`, `sy`, `sz` (standard deviations) or `accuracy`, `status`, `yaw`, `pitch`, `roll`; a header names the columns in any order (`name`, `lon`/`lat`/`ellh`, `easting`/`northing`, `std`, `q`...); comma, semicolon, tab or spaces |

**The MRK is paired with the photos by the shot number in their names** (`DJI_0001.JPG` is line 1 of the file),
so a photo deleted from the card never moves the positions of the others; by order only when the MRK has as many
lines as the folder has photos, otherwise nothing is done and the window says why. Every pair is then checked
against the time the camera wrote in the photo (its local time, a time zone away from the UTC of the MRK): a photo
taken more than 3 seconds away from its shot keeps the position it had, so the photos of another flight copied
into the same folder, with the same shot numbers, are left alone; when the times agree on no time zone, or most
photos are not at the time of their shots, nothing is done. When the photos of another flight fit a time zone too
and are as many as half of the photos of the MRK or more, the times cannot say which flight the MRK is of (its
own flight may have lost photos): nothing is done and the window asks to keep each flight in a folder of its own.
The shots that found no photo are listed in the log.
The photos are those of the folder of the MRK; when the MRK was copied somewhere else the window asks for the folder (`--photos` on the
command line). **The positions file** is asked its coordinate system (`EPSG:4979` is longitude, latitude and
ellipsoidal height, as in the MRK; a projected system such as `EPSG:32633`; `LOCAL`) and the kind of height
(*ellipsoidal*, as the GNSS gives it, or *EXIF*, above the sea level); a row is paired with its photo by the file
name, with or without the extension (`100MEDIA/DJI_0001` when the same name is in two folders). Applying
positions replaces the ones the photos had, and doing it twice changes nothing.

**The accuracy** of a position is twice its declared standard deviations (the ones of an RTK solution are
optimistic), never below the floor of its state: 1 cm for a fix, 20 cm for a float, 1 m for a single position.
Without RTK data it stays 5, 5, 10 m. In a positions file `accuracy` is taken as it is; a line with no accuracy
and no deviations gets 5, 5, 10 cm, raised to the floor of its `status` when it has one. Two steps weigh every
photo by its accuracy, so a block of fixes and a few singles is not treated as one of equals: *Align photos...*
holds the block to the positions during the incremental alignment (the option *GPS positions of the photos in the
alignment*, on when the photos have positions; the global method does not use them, and the automatic choice
goes back to the incremental one when the global block misses them), and *Optimise cameras*: with its default
engine, COLMAP, the accurate positions (fix, float, PPK: 0.5 m or better) hold the block when 3 control points or
2 scale bars do not (*GNSS positions of the photos*: *automatic*, see
[Optimising the cameras](processing.md#optimising-the-cameras)), and so do they with the engine *Own* on
*automatic*; with the engine *Own* every position takes part by its accuracy when that option is *used with their
accuracy (own engine)* or *used, with a bias per flight (own engine)*.

**The table** has a row per flight (a pause of more than 20 minutes starts another; the photos with no date are
the *no date* row): the photos with a fix, a float, a single position, with a position and no RTK data, with no
position at all, and those whose reference is switched off; the median accuracy per axis; the kinds of height of
the positions (*ellipsoidal* from the GNSS and the MRK, *EXIF*, *unknown* for positions read from another
program); and, once the chunk is aligned and georeferenced, how far the cameras ended from their positions (root
mean square per axis and the largest distance). A flight whose offsets are far larger than its accuracy has a
bias (a base on a wrong point) or positions that do not belong to it. When the positions in use, accurate in
height (under a metre), have heights of different kinds - the ellipsoidal ones of an MRK and those of the EXIF of
other photos, which can be tens of metres apart - the pane says so under the positions (and `ppg gnss status`
prints a warning): check them before using them together.

**Only the fix positions** switches off the references of the photos whose position is not an RTK fix: they stay
in the chunk and are aligned, but no longer pull the cameras. A position with no RTK state (the GPS of the EXIF, a
positions file with no `status` column) counts as not fix. The photos switched off are remembered: unticking
switches back on exactly those, one you had switched off by hand stays off, and positions applied again keep the
choice.

From the command line:

```bash
ppg gnss status survey.ppg                                    # the table, per flight
ppg gnss mrk survey.ppg /card/DCIM/100MEDIA/Timestamp.MRK     # --photos FOLDER if the photos are elsewhere
ppg gnss positions survey.ppg cameras.csv --crs EPSG:4979     # --height ellipsoidal|exif
```

All three take `--chunk` and say what they did; `mrk` and `positions` save the project, and stop with an error,
changing nothing, when the file cannot be used.

### PPK: corrections from a base

**What it gives.** A drone without an RTK link (or one that lost it) places its photos to a few metres. With PPK
(post-processed kinematic) RTKLIB computes again where the drone's antenna was, epoch by epoch, from the drone's own
satellite observations and from those of a base that recorded at the same time; every photo then takes the
position of the camera at its shot, as with an MRK, with the status (fix, float, single) and the accuracy of the
solution. With fixes the photos are placed to a few centimetres against the base, and a survey needs few or no
ground control points. The base decides where the whole survey ends up: your own receiver, a permanent station near
the survey, or your receiver placed by a static baseline from a station or by a static PPP
([The position of the base](#the-position-of-the-base)); its antenna is calibrated with the file of the IGS,
igs20.atx ([The antenna of the base](#the-antenna-of-the-base)); and every position keeps the reference frame and
the epoch of its coordinates ([Reference frames and epochs](#reference-frames-and-epochs)).

**The files.**

| File | Where it comes from |
| --- | --- |
| The drone's RINEX and its `Timestamp.MRK` | The folder of the flight on the drone's card: a DJI drone writes `<flight>_Rinex.obs` beside `<flight>_Timestamp.MRK` (often with `<flight>_PPKNAV.nav`); another receiver its observation file (`.obs`, `.##o`) |
| The base's RINEX | Your own base (any receiver that records RINEX) on a mark, or a permanent station near the survey: the program downloads the observations of a station of the EPN, the IGS or RING for the hours of the flight (*A permanent station nearby as the base*, below), or you give the file of a station you downloaded yourself. The base must record during the whole flight, in one file or in **several**: the hourly files of a station, the two daily files of a flight across midnight (UTC), the pieces a receiver writes - *Add...* takes them all, and the program joins them into one (they must be of one receiver: the same marker, antenna and observation types) and says where the holes of the base fall within the flight. The nearer the better: beyond 20 km a fix gets hard (the program warns) |
| Navigation files | The broadcast ephemerides: `.nav`, `.##n`, `.##g`, `.##p` or the RINEX 3 `*_MN.rnx` - the drone's, the base's or the daily file of a station; the daily broadcast file of the IGS in RINEX 2 (`brdcDDD0.YYn`, `.YYg`) is found too beside the observations, for their day and the days around it. For a station, a baseline or a PPP, when none is beside the drone's file, the program downloads the broadcast messages of the hours of the flight (`BRDC00WRD`, from BKG) |

**Compressed files** are read as they are, as the archives of the stations give them: gzip (`.gz`), the Unix compress
(`.Z`), a zip with one file in it (`.zip`) and Hatanaka (`.crx`, `.##d`: Compact RINEX 1.0, 3.0 and 3.1), one inside
another too (`.crx.gz`, `.##d.Z`). Hatanaka files are decoded by the native core 0.6 or later, which the applications
include (their smoke test, `--check`, fails if it is missing); without it by the `CRX2RNX` program of RNXCMP found in
the `PATH` (or named by `PPG_CRX2RNX`), else the program says what to install. `ppg doctor` (*Help > Environment
diagnosis...*) says where they are decoded. The decompressed copy is kept in the GNSS folder of the program and used again until
the file changes; RTKLIB is never given a compressed file.

**The steps.** *Compute PPK...* in the pane (*Tools > GNSS > PPK corrections...*) asks for the `Timestamp.MRK`, then
opens a dialog:

- *Timestamp.MRK*: its shots and the hours they were taken, and how many fall outside the drone's observations
  (their photos will keep the position they have);
- *Observations of the drone*: found by itself beside the MRK (the `*_Rinex.obs` of a DJI drone, of the flight of the
  MRK when the folder holds several; else the only `*.obs` or `*.##o` file there), with the hours it covers;
- *Position of the base*: one of the five ways below - with the coordinates, their frame and their epoch when they
  are typed, with *Choose the station...* for a permanent station;
- *Observations of the base*: one file or more (*Add...*; the field then says how many), with what the header says -
  the mark, the receiver, the antenna, the hours of the observations of all the files, the position in the header
  and the antenna height - and, in sight, whether the phase-centre offset of that antenna will be applied; a
  permanent station as the base needs none;
- *Navigation files*: those beside the drone's observations, then those beside the base's; *Remove* takes one away,
  *Add...* adds another;
- *Antenna of the base*: *Calibration of the antenna of the base: download igs20.atx (62 MB), once*
  ([below](#the-antenna-of-the-base));
- *Downloads*: what will be downloaded for the run - the coordinates and the observations of a station, the precise
  products of a PPP, igs20.atx, the navigation messages of the flight - and the frame and the epoch the positions of
  the photos will have;
- *Drone's RTK fixes*: whether the photos with an RTK fix of the drone itself take the PPK position too (below).

The dialog starts from the last run of the chunk: the files of the base, how the base was placed (its typed position
with its frame and epoch, or its station), the navigation files still there (not those beside the drone's
observations of another flight), the choice about the onboard fixes; for the same MRK the drone's observations too.
With several flights against one base the base and its position are given once.

*OK* checks, before anything runs, what can be checked and says in the dialog what is missing or wrong: the files
(observations that are observations, navigation files still there), their times (the drone and the base with hours
in common, the shots of the MRK within the drone's observations), no file in the work folder of the PPK, the
position of the base, the station chosen. The files are read once, after a pause in typing their names; the dialog
is as tall as what it says, and on a short screen its form scrolls. When the MRK was copied away from the photos the
window asks for their folder, as for an MRK. The work runs in the background: the station, the baseline or the PPP
first, with what they download (given to RTKLIB from `gnss/base`, `gnss/baseline` and `gnss/ppp` of the folder of
the chunk; the files of the stations and the precise products stay in the GNSS folder of the program, where a second
run finds them), then RTKLIB in `gnss/ppk`, where every run writes its options and its solution under a name of its
own, `solution-<date>-<time>.conf` and `.pos`: the solutions of the earlier runs (the other flights) stay, and every
photo records the solution of its run. The photos are paired with the shots as for an MRK.

#### The position of the base

Every PPK position is relative to the base: where the base is, the whole survey goes. Five ways to place it, in the
dialog and in `ppg gnss ppk`:

| Way | What it is | What to expect |
| --- | --- | --- |
| *From the file of the base (RINEX header)* | the position written in the header of its RINEX file | for a permanent station usually its published position; for your own base often the position the receiver found by itself, which **can be metres off, and the whole survey moves with it** |
| *Typed* | the surveyed position of the mark the base stands on, in the frame of its coordinates | as good as the survey of the mark |
| *A permanent station nearby as the base* | a station of the EPN, the IGS or RING: its observations for the hours of the flight and its coordinates in ITRF2020 at the middle of the flight are downloaded | coordinates good to a centimetre or better; the photos as good as a PPK at that distance - within 20 km, the nearer the better |
| *Computed by a baseline from a station nearby* | your own base on a mark nobody surveyed, placed by a static baseline from a station | fixed and good to the centimetre with an hour or more of observations together and a station within 20 km; a further station wants a longer session |
| *Computed by static PPP* | your own base placed on its own, on the precise orbits and clocks of the IGS and of its analysis centres | 10-20 cm with less than 2 hours of observations, a few centimetres with 4 hours or more, 1-2 cm across and 2-4 cm in height after a day: under 4 hours a baseline from a station nearby is better |

- **From the file of the base**: the program says that the position can be metres off in the dialog, in a message
  and in the log after the computation, in the pane and in `ppg gnss ppk`: type the surveyed position, or place the
  base with a station or a PPP, and compute again.
- **Typed**: latitude and longitude in degrees, height above the ellipsoid in metres (not above the sea level), in
  the frame of the coordinates (*Frame of the typed coordinates*: *Unknown (WGS 84)*, ITRF2020, ETRF2000 (RDN2008),
  ETRF2014, ETRF2020) and, in ITRF2020, at their epoch (*Epoch of the coordinates*: a decimal year such as 2026.82,
  or a date; empty, the middle of the drone's observations). This is the way to an accurate absolute position with
  a mark you surveyed. Typed more than 1 km away from the position of the base's header, or more than 30 m higher or
  lower, it gives a warning (in the dialog, in the log and in `ppg gnss ppk`): latitude and longitude swapped or
  without their sign, a height above the sea level. The same base (by the name of its mark, else of its file) typed
  at a position more than 5 cm away from the one used for other photos of the chunk gives a warning too - a typo in
  one flight moves that flight - and the pane says when one base has positions further apart.
- **A permanent station as the base**: *Choose the station...* opens *Permanent stations nearby*: the stations of the
  EPN, the IGS and RING within 200 km of the survey, the nearest first, with their networks and their distance (the
  list is downloaded again once a week, and the copy serves offline; *Try again* when it cannot be had). Whether the
  archives have the files of a station for the hours of the flight is found out when it is used: its observations
  at 1 s when the archive has them for the whole flight, else at 30 s, with ten minutes before and after (BKG's data
  centre for the stations of the EPN and of the IGS, RING's own archive, on the port 44324 that some firewalls close,
  for those of RING); the program says when they are not online yet. Its coordinates come from the multi-year
  solution of the EPN, from IGc20 of the IGS or from a straight line through two years of the daily positions of
  RING, brought to the epoch of the flight: the positions of the photos are in ITRF2020 at that epoch. A station
  known only by its approximate position (good to metres) is refused. The coordinates are those of the marker of the
  station: the program adds the antenna height of the header of its RINEX file, as for any base.
- **A baseline from a station**: your base records for a session (an hour or more) on a mark nobody surveyed;
  *Computed by a baseline from a station nearby* downloads the station's observations for that session and its
  coordinates at the middle of it, and RTKLIB solves your base as a receiver standing still for the whole session
  against the station, into one position. It takes off the antenna height of your base's header: the position is
  the one of the mark on the ground, in ITRF2020 at the epoch of the station's coordinates. A solution that is float,
  a station beyond 20 km, a session shorter than an hour, holes of more than a minute in the observations of the base
  give warnings; a solution of the code alone (no fix, no float), metres off, is refused. `ppg gnss baseline` finds
  the mark alone, to type it for several flights.
- **A static PPP**: your base alone, on the precise products of one centre and one kind: the final orbits and clocks
  of ESA (online about 4 days after the day), of CODE (about 9 days) or of the IGS (two weeks or more), else the
  rapid ones of the IGS (about 17 hours); never the ultra-rapid ones, which are half predicted. igs20.atx is always
  used, for the antennas of the satellites and of your base. RTKLIB-EX solves a PPP as float only (it fixes no
  ambiguities): the durations in the table are what to expect. The mark is in ITRF2020 at the epoch of the middle of
  the session. A session under 2 hours, under 4 hours (a station nearby is better), rapid products (compute again
  when the final ones are out) and a session before 27 November 2022 (GPS week 2238: the products were made for
  igs14.atx) give warnings. `ppg gnss ppp` finds the mark alone.

#### The antenna of the base

**The antenna height.** The position of the base, typed or from the header, is that of the mark on the ground: the
program adds the antenna height written in the base's RINEX header (`ANTENNA: DELTA H/E/N`), as RINEX means it, and
the dialog says how much ("the program adds the antenna height of the file of the base, 1.800 m"). When the header
gives no antenna height, or zero, the position must be that of the antenna itself.

**The phase centre.** The antenna height brings the base to the reference point of its antenna (usually the bottom of
its mount); the satellites are measured at its phase centre, which sits higher by an offset of its own for every
antenna model, radome and frequency: a few centimetres for survey antennas, more than 10 cm for some choke rings.
RTKLIB corrects it with the antenna calibrations of the IGS, **igs20.atx**, where the program looks for the antenna
named in the base's RINEX header (its type and radome) as RTKLIB does. *Calibration of the antenna of the base:
download igs20.atx (62 MB), once* in the dialog (ticked; `--atx download` on the command line, or
`ppg gnss atx --download`) downloads it into the GNSS folder of the program when it has none; once there every run applies it,
unticked too. A station, a baseline and a PPP download it anyway, so that every antenna is calibrated, and they renew
a copy older than 30 days, as `ppg gnss atx --download` does; `--atx off` gives no calibrations. In the dialog, under
the base, the program says whether the offset will be applied; after the run it warns when it was not - no
calibrations given (`--atx off`), the antenna not in igs20.atx, a header that names no antenna - and when RTKLIB took
the same antenna with another radome, or the nearest name it found (a few millimetres, more if it is another antenna).
Without the calibration every PPK height comes out lower by the offset - the same for all the photos of a run, but
different from one base antenna to another, so surveys computed against different bases differ by it too (DEM
differences, volumes): check the heights against a ground control point, or type the height of the base with the
vertical offset of its antenna added. The [PDF report](exports.md#pdf-report) names, for every run, the entry used
("calibrated with TRM57971.00 NONE of igs20.atx") or why there was none.

**Photos with an RTK fix of the drone itself.** A photo whose position is an RTK fix of the drone itself - from the
XMP of a DJI photo or from a DJI MRK - keeps it: the PPK is for the photos that are not fix. *Replace the drone's own
RTK fixes too* in the dialog (`--replace-fix` on the command line) gives them the PPK position as well, so that
every photo is in the frame of the base; the program warns when an onboard fix becomes float or single. When the
fixes kept are more than 10 cm (median) from where the PPK puts the same shots (in height too when both heights are
ellipsoidal, as in an MRK; across only for the altitude of an EXIF), a warning says what it likely means: the base
in another reference frame than the drone's RTK corrections (a network in ETRF and a base in ITRF, or the other way
round) or at a wrong position, and the flight now mixes the two - check the position of the base, or compute again
replacing the onboard fixes. A photo that is not fix is not always better off with the PPK: when it gets a worse state
than the drone itself gave it (an onboard float that is single in the PPK), a warning counts those photos - the PPK
position is used all the same - and the base, its distance from the flight and its observations are what to check.

**The result.** A photo takes the antenna of the solution at the time of its shot (an epoch, or the straight line
between two epochs at most a second apart) plus the lever arm of its shot in the MRK; its status comes from RTKLIB's
quality (fix, float - also dgps - or single) and its accuracy is twice the standard deviations of the solution,
with the floors of its status. A photo shot where the solution has no position (a gap of more than a second,
before it starts, after it ends) keeps the position it had: nothing is guessed across a gap, and such photos and
shots are listed in the log. *Only the fix positions* is decided again. The log also says how many photos kept
their onboard fix, the base with its antenna, how far it is from the flight (from the median position of the drone
in the solution: beyond 20 km a warning, the ambiguities are hard to fix and the positions mostly float) and the
solution file. Every photo keeps, with its position, the base it was computed against - its name, its position as
longitude, latitude and height (the order of every position of the program), how it was placed and what the source
of its position says (the station and the solution of its coordinates, the baseline, the PPP and its products), its
frame and epoch, the antenna of its header and its calibration - the drone's observation file and the solution file
of its run. The step of the pane then says how many photos have a PPK position and of which status, and for each
base where its position comes from, in which frame and at which epoch, with the warning when its position is the
one of its header. With accurate positions, *Optimise cameras* holds the block to them when no control points do
([Optimising the cameras](processing.md#optimising-the-cameras)).

RTKLIB (RTKLIB-EX 2.5.1, BSD licence) is inside the native core 0.5 or later, which the applications include (their
smoke test, `--check`, fails if it is missing); without it the program says what to install. `PPG_RNX2RTKP` can name the
`rnx2rtkp` program of RTKLIB-EX 2.5 instead. `ppg doctor` (*Help > Environment diagnosis...*) says where the PPK runs:
RTKLIB inside the native core and its version, the program named by `PPG_RNX2RTKP`, or what to install.

**Credits.** What the program downloads is public and free, with its credits: the observations and coordinates of
the stations of the EPN (CC BY 4.0; the multi-year solution is cited with the DOI of its weekly folder,
`10.24414/ROB-EUREF-Cwwww`) and of RING of INGV (CC BY 4.0), IGc20 and igs20.atx of the IGS, the precise products of
ESA, CODE and the IGS by their names, the geoid EGM2008 of the NGA (public domain). The PDF report credits those that
each survey used: the station and the folder of the solution of its coordinates, the centre of the products of a
PPP.

#### From the command line

```bash
ppg gnss ppk survey.ppg --mrk /card/FLIGHT_Timestamp.MRK --rover /card/FLIGHT_Rinex.obs --base base.obs
#   --base FILE (repeat it for a base in several files: hourly files, two days)
#   --nav FILE (repeat it; default: those beside the drone's observations, else beside the base's)
#   --base-position LAT,LON,H (degrees, degrees, metres above the ellipsoid, a point for the decimals;
#                              default: the position of the base's RINEX header, with its warning)
#   --base-frame unknown|ITRF2020|ETRF2000|ETRF2014|ETRF2020, --base-epoch 2026.82 (or 2026-10-27):
#                              the frame and the epoch of --base-position (in ITRF2020 the epoch defaults to
#                              the middle of the drone's observations)
#   --base-station ID (a permanent station as the base: its files are downloaded, no --base)
#   --base-baseline ID (your --base placed by a static baseline from the station ID)
#   --base-ppp (your --base placed by a static PPP)
#   --atx auto|download|off (the antenna calibrations: igs20.atx when the program has it, downloaded once, none)
#   --replace-fix (the photos with an RTK fix of the drone itself take the PPK position too)
#   --photos FOLDER, --chunk NAME
ppg gnss stations survey.ppg                     # the permanent stations within 200 km of the survey
#   --near LAT,LON instead of a project, --limit N, --refresh (the lists downloaded again)
ppg gnss baseline base.obs --station AQUI00ITA   # the mark of your base by a static baseline (--folder DIR)
ppg gnss ppp base_1.obs base_2.obs               # the mark of your base by a static PPP (--folder DIR)
ppg gnss atx --download                          # igs20.atx (62 MB): where it is, or downloaded
```

`ppg gnss ppk` prints the photos per status, how many kept their onboard fix, the base used (latitude, longitude and
height, how it was placed, its frame and epoch), its antenna height and how far it is from the flight, the navigation
files downloaded for the flight, the solution file of the run, the warnings (the phase-centre offset of the base
antenna when it is not applied; a base at the position of its header, a typed position far from it or from the one of
other photos; those of a baseline or a PPP) and the photos and shots left without a PPK position; it saves the
project, and stops with an error, changing nothing, when the files cannot be used (a file named that is not there
stops it before anything is read). A flight whose photos all kept their onboard fix changes nothing and is no error.
`ppg gnss baseline` and `ppg gnss ppp` print the mark found with its frame, epoch and standard deviations, the
warnings, and the options that give it to `ppg gnss ppk`
(`--base-position ... --base-frame ITRF2020 --base-epoch ...`).

### Reference frames and epochs

Every position of a photo keeps the reference frame and the epoch of its coordinates, and the program converts it
at that epoch.

- **ITRF2020** (and IGS20, IGb20, IGc20, the same for the program): the global frame of the products of the IGS, of
  the coordinates of the permanent stations and of a PPP. In it the ground moves with its plate - Europe by about
  2.5 cm a year - so a position in ITRF2020 needs its epoch. A PPK against a station, or against a base placed by a
  baseline or a PPP, gives positions in ITRF2020 at the epoch of the coordinates of the base.
- **ETRF2000**, and the other European frames ETRF2014 and ETRF2020, move with the European plate: a point on the
  ground keeps its coordinates. **RDN2008**, the Italian reference network (and ETRS89 in Italy), is ETRF2000 at the
  epoch 2008.0: the program takes ETRF2000 as RDN2008.
- **Unknown (WGS 84)**: the drone's own RTK (a network in ETRF, the drone's own base), the GPS of the photos, the
  positions written before the frames: taken as WGS 84, as always.

**Why ITRF2020 and ETRF2000 differ by about a metre in Italy.** The two frames coincided in 1989, when ETRS89 was
defined on the European plate; since then Europe has moved in the global frame by about 2.5 cm a year towards the
north-east, so that in 2026 the same point has coordinates about a metre apart in the two (0.99 m at Milan, 1.03 m at
Rome, 1.06 m at Palermo; the heights within a centimetre). A survey placed by a station in ITRF2020 and drawn on maps
in RDN2008 is a metre off when the frames are not converted, and still 30 cm off when they are converted without the
epoch (PROJ alone takes 2015.0). The program converts from ITRF2020 to ETRF2000 - and so to RDN2008 and to ETRS89 /
UTM - with the time-dependent Helmert transformation of the EPSG (*ITRF2020 to ETRF2000 (1)*, EPSG:10586) at the
epoch of each position; from ITRF2020 to a system on WGS 84 the positions pass as they are (the two agree within
centimetres). Between two European frames - a base typed in ETRF2014 or ETRF2020, a chunk in RDN2008 or ETRS89 /
UTM - the positions go through ITRF2020 at their epoch, by the two Helmerts of the EPSG (PROJ alone takes a null
through ETRS89, 8-9 cm off at Rome in 2026): the photos at the epoch of their shot, markers read from a file at the
epoch of the survey, which the log says. The motion of Italy within its plate since 2008 - a few millimetres a year,
more in the south and around the volcanoes - is not in it: RDN2008 is ETRF2000 at 2008.0.

The frame and the epoch come with the position: a typed base its own (*Frame of the typed coordinates*, *Epoch of the
coordinates*; `--base-frame`, `--base-epoch`); a positions file in a system of ITRF2020 (`EPSG:9989` and the systems
on it) the *Epoch of the positions* of its dialog (`ppg gnss positions --epoch`; empty, the EXIF date of each photo);
the MRK and the photos are *unknown (WGS 84)*. Points that carry no frame and epoch in a system of ITRF2020 - the
markers, the positions of a project made before - are converted as before, by PROJ's own transformation where it has
one, else as positions on WGS 84: an approximation, about a metre off in Europe, which the log says once. Give them
again with their epoch, or in ETRF2000 (RDN2008).

The GNSS pane has five steps: *Positions of the photos*, *Corrections (PPK)*, **System and heights**, *Quality per
flight* and *Only the fix positions*. *System and heights* lists the frames of the positions with their epochs, the
system of the chunk and the kind of its heights, whether the grid of the geoid EGM2008 is installed and what does not
fit between the heights of the photos and those of the chunk; its buttons are *Survey system...* and *Download the
EGM2008 geoid (81 MB)...* (below). A chunk in a system on WGS 84 holding positions in ITRF2020 (a station, a baseline,
PPP) together with references in a European frame (photos of a base typed in RDN2008, markers read from a file in
RDN2008 such as those of an Italian network RTK) is said there, in the PDF report and by `ppg gnss crs`: in Europe the
two are about a metre apart in such a system, and both arrive exactly in a system of ETRF2000.

### Heights above the geoid and the system of the survey

GNSS gives heights above the ellipsoid; the heights of a survey are above the sea level, the geoid, which in Italy is
40-50 m away (48 m at Rome). The program converts between the two with the grid of the geoid **EGM2008**
(`us_nga_egm08_25.tif`, 81 MB, public domain), which it does not ship and **downloads only when asked**: *Download the
EGM2008 geoid (81 MB)...* in the step *System and heights* of the GNSS pane, or `ppg gnss geoid --download`, into the
GNSS folder of the program, checked against its published size and SHA-256. PROJ never goes to the network by itself.
Without the grid every conversion between the two kinds of height is refused, naming the grid and how to get it:
heights above the ellipsoid are never passed off as heights above the sea level. A grid of your own - such as
**ITALGEO2005** of the IGM (`geo_igm_mar06.grd`, the heights of Genova 1942, EPSG:5214) - is copied into the folder
of the grids of the program, which `ppg gnss geoid` prints (`proj` in the GNSS folder); systems with those heights,
such as `EPSG:7792+5214`, then use it.

**The system of the survey.** *Survey system...* proposes the coordinate system of the survey where it is. In Europe
(where ETRS89 is the datum) a system of ETRF2000: RDN2008 / UTM in Italy (EPSG:7791, 7792, 7793, the national datum)
and ETRS89 / UTM elsewhere, where positions in ITRF2020 (a permanent station, a baseline, PPP) arrive exactly at their
epoch, as those in the European frames do (a base typed in RDN2008, markers of a network RTK), and nothing drifts with
the plate between flights of different years; beyond Europe the UTM zone on WGS 84. The other one is offered too (the
UTM zone on WGS 84 in Europe, ETRS89 / UTM for positions in a European frame beyond it), or another system (*Other:*,
an EPSG code or a WKT). *Heights above the EGM2008 geoid* gives it heights above the sea level (`+3855`);
when the grid is not installed the window offers to download it first (*Download and apply*) or to apply the same
system without heights of its own (*Apply without heights above the geoid*). Before anything changes the window works
out, in the background, what the change does, shows it and asks *Apply?*:

- the georeference follows the chunk into the new system, with the model, the clouds and the tie points;
- the references of the markers are converted from one system to the other, as coordinates measured on the ground;
  what was drawn on the products - units, spot heights, measuring points, stones, the sections of the project, the
  patches of the orthomosaic and of the texture, the region of the reconstruction, the colour chart - goes with the
  georeference, so that it stays on the model (on a chunk with nothing to follow it is converted as the markers are);
  the polygons loaded from PyArchInit go as coordinates too;
- what is to be built again in the new system: the DEM, the orthomosaic, the precision of the DEM, the coverage;
- what stays in the old system and is to be made again if needed: the inactive versions of the DEM and of the
  orthomosaic, the elevations, the multispectral, thermal and satellite rasters, the relief and the raster
  calculator, the US proxies, the stones drawn on other rasters, the drawings (SVG, DXF) of the sections; the lines
  that are only in the window (contour lines, plan slices, measures) are taken away, to draw again.

A chunk in local coordinates only takes the system: its markers and drawings stay where they are, and its
georeference is fitted afterwards. *Edit > Undo* does not cross the change: what was edited before it stays as it is
in the new system.

A site too large to be converted by one similarity between the two systems (it would miss the plan by more than
5 mm) is refused, nothing changed. The [PDF report](exports.md#pdf-report) names the system of the chunk, the grid of
its geoid with its SHA-256 and how the positions in ITRF2020 were converted.

```bash
ppg gnss crs survey.ppg                     # the system and its heights, the frames of the positions, what does not fit
ppg gnss crs survey.ppg --survey            # into the system of a survey where it is (--frame WGS84|ETRF2000 instead, --no-geoid)
ppg gnss crs survey.ppg --set EPSG:7792+3855
ppg gnss geoid                              # the grid of EGM2008 installed or not, the folder of the grids
ppg gnss geoid --download                   # EGM2008 (81 MB, public domain)
ppg gnss positions survey.ppg cameras.csv --crs EPSG:9989 --epoch 2026.82   # positions in ITRF2020 at their epoch
```

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
