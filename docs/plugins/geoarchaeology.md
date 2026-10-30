# Geoarchaeology

The *Geoarchaeology* plugin turns the logs of boreholes - hand augers, mechanical cores, test pits,
columns measured on the wall of a trench - into the stratigraphy under the photogrammetric survey:
the column of every borehole, the surface where each unit begins, the thickness and the volume of
each unit, sections along any line and a 3D model with one solid per unit. Everything lies on the DEM
and the orthophoto of the project, in its reference system.

This page describes version 0.2.0 of the plugin, which needs pyarchinit-photogrammetry 1.3.3 or later.
Its README and source code are in the
[plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/geoarchaeology).

<figure markdown="span">
  ![Hillshade of the DEM with the boreholes and the traces of two sections](../img/plugins/geoarchaeology/boreholes-and-traces.jpg)
  <figcaption>Simulated boreholes (invented stratigraphy) on the real DEM of Tell Sultan, in a local
  reference system. Circles: boreholes that reach the natural; triangles: boreholes that stop before
  it. The dashed line bounds the surfaces, the orange lines are the traces of two sections.</figcaption>
</figure>

!!! note "The example on this page"
    The figures and the numbers come from a demonstration with 18 **simulated** boreholes: an invented
    stratigraphy of topsoil (US 1), collapse and colluvium (US 2), Iron Age (US 3) and Bronze Age (US 4)
    deposits over a sterile natural (US 5), placed on the real DEM of Tell Sultan - the site of the
    [tutorial](../tutorials/tell.md) - moved to a local reference system. They show what the plugin
    does and say nothing about the site. The [example files](#example-files) hold the same data. The
    figures were made with the Italian interface.

## In short

1. Survey the site and build the DEM (and the orthophoto) in a chunk with a projected reference
   system, in metres.
2. Measure the position of every borehole in the same reference system; describe each log interval
   by interval.
3. Write a CSV table of the collars and one of the intervals, or a single table.
4. In the *Geoarchaeology* pane: *Import boreholes...*, check the *Order of the units...*, then
   *Surfaces...*.
5. Look at the surfaces over the orthophoto, draw sections and stratigraphic columns, build the 3D
   model and look at it in the 3D view.
6. Export to GeoPackage, GeoTIFF, DXF, PLY/OBJ and CSV, or open everything in QGIS.
7. For pits, ditches and their fills, or a contact seen in few boreholes, build the [implicit model](#implicit).

## What it does

| Product | What it is | Useful for |
|---|---|---|
| Stratigraphic columns | the log of one borehole with its descriptions, or a panel of several side by side on a common elevation axis, with the tops of the same unit joined | recording and correlating the logs |
| Contact surfaces | the top of every unit between the boreholes - its contact with the unit above - interpolated by kriging, with its uncertainty | where the natural begins under a mound, a buried floor, the base of the topsoil |
| Thicknesses and volumes | the thickness of every unit (isopach) and its volume | how deep the deposits are, volumes to excavate, how a mound grew |
| Sections | the ground, the contacts and the units along a line, with the nearby boreholes drawn on it, to scale | plates for the report, checking a correlation |
| 3D model | a closed solid per unit, with its volume | QGIS 3D, Blender, MeshLab, teaching |
| Implicit model | on request, with LoopStructural: cuts and fills, undercuts, sparse contacts, dips | a pit or a ditch and the volume of its fill, as a range |

It helps wherever a few holes have to tell what lies under a surface you have surveyed: a coring
campaign on a tell, a grid of augers across a field before an excavation, the sections of separate
trenches to be linked. The plugin draws what the logs say and fills the space between them as
smoothly as the data allow. The correlation of the logs - which layer of one borehole is the same as
which layer of another - stays the archaeologist's job: it is written in the unit codes.

## What you need first { #before }

* **The plugin**, installed from the program: see [Plugin guides](guides.md#install).
* **A project with a chunk processed up to the DEM** (*Workflow > Build DEM...*), better also with the
  orthophoto (*Workflow > Build orthophoto...*). The plugin takes the ground from the DEM: the top of
  the first unit, the elevation of the collars that have none, the surface that depths hang from. It
  uses the DEM the chunk shows, its active version: if trees or buildings are in it, classify the dense
  cloud (*Tools > Classification of the cloud*), build a DEM from the *Terrain* class only and make that
  version active (right click on it in the *Workspace*, *Make active (the other tools use it)*).
  Without a DEM the plugin still works - the ground is interpolated between the elevations of the
  collars, which should then all have one - but without a DEM or an orthophoto there is nothing to draw
  a section on: only sections through the boreholes can be made.
* **A projected reference system, in metres**, for the chunk - for example the UTM zone of the site -
  and the boreholes **measured in the same system**. The plugin puts every borehole where its x and y
  say, on the grid of the DEM, and measures distances, areas and volumes in the units of the
  coordinates: boreholes measured in another system (latitude and longitude, another zone, the local
  grid of the excavation) fall elsewhere or nowhere. Convert them first, for example in QGIS. A local
  system works too when the survey and the boreholes are both in it, as in the demonstration; the
  GeoTIFFs and the layers then have no reference system, and QGIS shows them in a project without one.
* **The logs** of the boreholes as CSV tables: the next section.

## The data

### Two tables, or one

The plugin reads CSV files: plain text tables, which every spreadsheet can write. Two layouts:

* **two tables**: the *collars*, one row per borehole (where it is), and the *intervals*, one row per
  interval of a log (what it found), linked by the code of the borehole;
* **one table**: one row per interval, with the position of its borehole on the same row - at least on
  the first row of each borehole.

The columns can be in any order, with Italian or English names, written as they are in the field:

* capitals, accents, spaces and punctuation do not count: `Profondità (m)`, `profondita_m` and
  `PROFONDITA` are the same column;
* a unit of length after the name converts the values: `Da (cm)`, `Prof. (m)`, `quota_mm`;
* the words around a name that do not change it are passed over: `Sigla sondaggio`, `N. sondaggio`,
  `Codice US`, `N. US`, `US n.` (the borehole, the unit), `Quota s.l.m.`, `Elevation (m a.s.l.)` (the
  elevation), `Quota sondaggio`, `Descrizione US` (a field of the borehole, of the unit).

Columns the plugin does not know are left out and named at the end of the import (*columns not used:
'Data', 'Operatore'*), so the table can keep columns of your own (date, operator, samples). When two
columns could hold the same thing, the clearer name wins - `sondaggio` over `id`, `descrizione` over
`note` - and the one further left among equals. A few names only guess their field: `codice`,
`sigla` and `nome` (the borehole, or the unit when the borehole has a column of its own), `h` (the
elevation), `e` or `n` alone (the coordinates). They are read when nothing clearer is there, and the
import says so: *column 'codice' taken as the unit*.

### The collar table { #collars }

| Name: Italian<br>English | Required | Unit | Example | Notes |
|---|---|---|---|---|
| **sondaggio**<br>**hole_id** | yes | | `S1` | the code of the borehole, the same in the interval table |
| **x**<br>**x**, **easting** | yes | m | `611,00` | in the reference system of the chunk |
| **y**<br>**y**, **northing** | yes | m | `799,00` | in the reference system of the chunk |
| **quota**<br>**elevation**, **z** | no | m | `307,130` | the ground at the borehole, where the log starts; empty: read from the DEM |
| **profondita**<br>**depth** | no | m | `12,00` | the depth reached; empty: the base of the last interval |
| **tipo**<br>**kind** | no | | `core` | free text, such as `auger`, `core`, `trench`, `test_pit`; shown with the boreholes, used in no computation |

### The interval table { #intervals }

| Name: Italian<br>English | Required | Unit | Example | Notes |
|---|---|---|---|---|
| **sondaggio**<br>**hole_id** | yes | | `S1` | as in the collar table |
| **da**<br>**from** | yes, or `quota_tetto` | m | `0,34` | depth of the top of the interval below the collar |
| **a**<br>**to** | yes, or `quota_letto` | m | `1,31` | depth of its base |
| **quota_tetto**<br>**top_z** | instead of `da` | m | `306,790` | elevation of the top of the interval |
| **quota_letto**<br>**base_z** | instead of `a` | m | `305,820` | elevation of its base |
| **us**<br>**unit** | yes | | `US 2` | the unit: a US number or a lithological unit |
| **descrizione**<br>**description** | no | | `crollo e colluvio` | free text |
| **colore**<br>**colour** | no | | `7.5YR 6/4` | Munsell, `#rrggbb`, `r,g,b` or an English colour name |
| **litologia**<br>**lithology** | no | | `argilla` | the main lithology; it helps choose the hatch of the drawings |

A single table has the columns of both: one row per interval, with `sondaggio`, `x` and `y` (and
`quota`, `profondita`, `tipo`) on at least the first row of each borehole; the following rows of the
same borehole may leave them empty.

??? info "Every column name the plugin recognises"
    The names are written here without capitals, accents and punctuation, with `_` between the words.
    Under *less sure* are the names that are read only when no clearer one is there.

    | Field | Names | Less sure |
    |---|---|---|
    | borehole | `sondaggio`, `sondaggi`, `id_sondaggio`, `nome_sondaggio`, `carota`, `carotaggio`, `trincea`, `saggio`, `pozzetto`, `trivellata`, `foro`, `hole`, `hole_id`, `holeid`, `bhid`, `bh`, `borehole`, `borehole_id`, `boring`, `core`, `core_id`, `trench`, `test_pit`, `pit`, `location_id`, `log_id` | `id`; guesses: `codice`, `code`, `sigla`, `nome`, `name`, `punto`, `point`, `station`, `location` |
    | x | `x`, `est`, `x_coord`, `coord_x`, `xcoord`, `coordinata_x`, `x_utm`, `utm_e`, `utm_x`, `east`, `easting` | `e` (a guess without `n`) |
    | y | `y`, `nord`, `y_coord`, `coord_y`, `ycoord`, `coordinata_y`, `y_utm`, `utm_n`, `utm_y`, `north`, `northing` | `n` (a guess without `e`) |
    | elevation of the collar | `quota`, `z`, `quota_bocca`, `quota_testa`, `quota_piano`, `quota_pc`, `quota_p_c`, `quota_piano_campagna`, `quota_terreno`, `quota_suolo`, `elevation`, `elev`, `collar_z`, `z_collar`, `collar_elevation`, `rl`, `collar_rl`, `altitude`, `alt`, `ground_level`, `ground_elevation`, `surface_elevation` | guesses: `h`, `height` |
    | depth reached | `profondita`, `profondita_totale`, `profondita_raggiunta`, `profondita_max`, `profondita_foro`, `prof`, `prof_max`, `prof_totale`, `prof_raggiunta`, `fondo_foro`, `depth`, `total_depth`, `max_depth`, `final_depth`, `hole_depth`, `eoh`, `td` | |
    | kind | `tipo`, `tipologia`, `metodo`, `strumento`, `tecnica`, `kind`, `type`, `method`, `tool` | |
    | top of the interval (depth) | `da`, `dal`, `prof_da`, `profondita_da`, `da_prof`, `inizio`, `profondita_tetto`, `prof_tetto`, `from`, `depth_from`, `from_depth`, `top_depth`, `start` | `tetto`, `top` (below) |
    | base of the interval (depth) | `a`, `al`, `prof_a`, `profondita_a`, `a_prof`, `fine`, `profondita_letto`, `prof_letto`, `profondita_base`, `prof_base`, `to`, `depth_to`, `to_depth`, `bottom_depth`, `end` | `letto`, `base`, `bottom` (below) |
    | top of the interval (elevation) | `quota_tetto`, `tetto_quota`, `quota_da`, `da_quota`, `quota_sup`, `quota_superiore`, `z_tetto`, `q_tetto`, `top_z`, `z_top`, `z_from`, `from_z`, `top_elevation`, `top_rl`, `rl_top`, `rl_from` | `tetto`, `top` (below) |
    | base of the interval (elevation) | `quota_letto`, `letto_quota`, `quota_base`, `base_quota`, `quota_a`, `a_quota`, `quota_inf`, `quota_inferiore`, `quota_fondo`, `z_letto`, `q_letto`, `base_z`, `bottom_z`, `z_base`, `z_bottom`, `z_to`, `to_z`, `bottom_elevation`, `base_elevation`, `bottom_rl`, `base_rl`, `rl_bottom`, `rl_to` | `letto`, `base`, `bottom` (below) |
    | unit | `us`, `usm`, `us_usm`, `unita`, `unita_stratigrafica`, `unita_litologica`, `codice_unita`, `strato`, `orizzonte`, `su`, `unit`, `unit_code`, `unit_id`, `us_code`, `context`, `layer`, `stratum`, `horizon`, `facies`, `formation`, `litho_unit` | `livello`; guesses: `codice`, `code`, `sigla` |
    | description | `descrizione`, `desc`, `descr`, `description` | `note`, `osservazioni`, `commento`, `commenti`, `notes`, `remarks`, `comment`, `comments` |
    | colour | `colore`, `colore_munsell`, `munsell`, `colour`, `color`, `munsell_colour`, `munsell_color`, `colour_munsell`, `color_munsell` | |
    | lithology | `litologia`, `lito`, `materiale`, `tessitura`, `granulometria`, `lithology`, `lith`, `material`, `texture` | |

    Around these names:

    * **a unit of length** at the end - `m`, `mt`, `metro`, `metri`, `metre`, `metres`, `meter`,
      `meters`, `cm`, `mm` - converts the values to metres, in any column of numbers: `Da (cm)`,
      `quota_cm`, `profondita_mm`;
    * **what an elevation is measured from**, at the end, is passed over: `s.l.m.`, `slm`, `m s.l.m.`,
      `a.s.l.`, `asl`, `m a.s.l.`, `amsl`, `msl`, `assoluta`, `assoluto`, `ass`, `abs`, `absolute`
      (`Quota s.l.m. (m)` is `quota`, `Elevation (m a.s.l.)` is `elevation`);
    * **the words that do not change the borehole or the unit**, before or after it, are passed over:
      `n`, `nr`, `no`, `num`, `numero`, `number`, `cod`, `codice`, `code`, `sigla`, `id`, `nome`, `name`
      (`Sigla sondaggio`, `N. sondaggio`, `Codice US`, `US n.`, `Context No.`);
    * **the borehole or the unit a field belongs to**, after the field, is passed over: `sondaggio`,
      `sondaggi`, `foro`, `carota`, `carotaggio`, `hole`, `borehole`, `us`, `unita`, `strato`, `layer`,
      `unit`, `intervallo`, `interval` (`Quota sondaggio`, `Descrizione US`, `Tipo sondaggio`).

    `tetto` and `letto`, `top`, `bottom` and `base` alone can be depths or elevations. The plugin
    decides from the other columns - beside `da` and `a` they are elevations, beside `quota_tetto` and
    `quota_letto` they are depths, and with only one of these the other end of the interval tells -,
    otherwise from the values: elevations decrease downwards, depths increase. The import says which:
    *'tetto', 'letto' read as elevations (the base is below the top in 2 rows of 2)*.

### Depths { #depths }

* Depths are in metres below the collar - the ground at the top of the borehole - and grow downwards:
  an interval from `0,34` to `1,31` starts 34 cm under the surface and is 97 cm thick.
* Every log starts at `0`: the first interval begins at the collar. The top of the first unit is then
  the ground itself, the DEM.
* The intervals follow one another: the `a` of one is the `da` of the next. Differences under 5 mm
  count as rounding.
* Depths in centimetres or millimetres need a column name that says so: `Da (cm)`, `da_cm`,
  `profondita_mm`. The same holds for elevations and coordinates.
* `profondita`, the depth reached, may stay empty. When it is deeper than the base of the last interval,
  the plugin takes it that the hole was still in the last unit down there: the units below are deeper.

### Elevations { #elevations }

* `quota` is the elevation of the ground at the borehole, in metres, in the same height system as the
  DEM. Leave it empty and the plugin reads it from the DEM at that point; in the *Boreholes* tab of the
  pane those elevations have a `*`.
* Where you give it, the plugin compares it with the DEM and writes the result at the end of the
  import: *Collar elevation minus DEM: median -0.008 m, largest +0.071 m (S17)*. A few centimetres are
  normal. A median far from zero means the elevations and the DEM are in different height systems -
  for instance ellipsoidal heights from a GNSS receiver and a DEM on the geoid - or the survey is
  shifted in height: bring them together, or leave the column empty. A single large value points to a
  typing error or to a borehole in the wrong place.
* The contacts of a borehole are placed at its elevation minus their depth.

### Intervals by elevation { #by-elevation }

Logs recorded with absolute elevations (with a level or a total station) use `quota_tetto` and
`quota_letto` in place of `da` and `a`. The plugin turns them into depths below the collar, so the
collar needs an elevation - in the table, or from the DEM; without one the interval is left out with a
warning. Give the collar the elevation of the top of its first interval. A table may mix the two ways:
a row with `da` and `a` uses them, a row without them uses the elevations.

`tetto` and `letto` alone, like `top`, `bottom` and `base`, can be either: the plugin decides from the
other columns or from the values - elevations decrease downwards, depths increase - and says which at the
end of the import (*'tetto', 'letto' read as elevations (the base is below the top in 2 rows of 2)*).
`quota_tetto` and `quota_letto` leave no doubt.

### Units { #units }

The `us` column says which unit each interval belongs to. It is the heart of the data: the boreholes
are linked to one another only through these codes.

* Any code works: a US number (`US 12`, `12`), a lithological or stratigraphic unit (`A`, `B`,
  `Unit 3`).
* The codes are compared in one form: `US 3`, `US3`, `us 3`, `US 03`, `US n. 3` and `SU 3` are one
  unit, shown as it is written most often, and the import lists the spellings it read as one (*units
  written in different ways, read as one: US 3 (US3, us 3)*). A plain number (`3`) is the unit of the
  one kind that has that number (`US 3`); when two kinds share it (`US 4` and `USM 4`) it stays a unit
  of its own and the import says so (*unit '4': it could be US 4 or USM 4, kept apart*): write the
  kind. Other codes are compared without capitals, spaces and punctuation: `Sabbia gialla` and
  `sabbia-gialla` are one unit. The column of a single borehole shows every code as its log writes it.
* One code is one unit, in one place of the sequence. Two layers of the same kind at different depths -
  two levels of ash - need two codes.
* Several intervals in a row may belong to the same unit, with different descriptions; a unit does not
  come back further down the same log.
* The units come in the same order in every log. The plugin finds the order in the logs - a unit above
  another in a borehole lies above it - and where the logs disagree the majority wins and the
  disagreement is reported. The order can also be written by hand ([step 2](#order)).
* A unit absent from a borehole is simply not written: a log that goes from `US 2` straight to `US 4`
  says that US 3 is missing there.
* When the codes are the numbers of US drawn in the chunk with the *Draw US* tool, the 3D model cuts
  each unit to its drawn polygons: `US 12`, `12` and `US12` match a drawn US 12, `USM 3` only a drawn
  USM 3.

The plugin builds a stack: every unit lies above the next one everywhere, with zero thickness where it
is missing. Units side by side - a wall next to its fill, a pit cut through the layers - are not
modelled as such: at best they become units missing from some logs.

### Descriptions, colours and lithology { #colours }

* `descrizione` is free text. The most frequent description of a unit names it in the legends of the
  sections and panels; each interval keeps its own in the column of a single borehole.
* `colore`: a Munsell notation (`10YR 4/3`, `7.5YR 6/4`, `2.5Y 5/2` or `2,5Y 5/2`, `N 5/`), also
  inside a longer text (`10YR 4/3 bruno scuro`); `#rrggbb`; three numbers from 0 to 255 (`160,82,45`
  or `160 82 45`); or the colour in words, Italian or English (`bruno rossastro`, `grigio scuro`,
  `giallo chiaro`, `light yellowish brown`, `dark grey`), which gives an approximate soil tone. A unit
  is drawn in the colour written most often for it (the first one that can be read); a colour that
  cannot be read is named at the end of the import (*colour 'xyzzy' not recognised (line 3): not
  used*), and a unit without a readable colour gets one from a palette of earth tones. Munsell
  notations are converted by the colour-science library, which is in the desktop application and in
  the `[gui]` and `[all]` installations; without it the import says so, and the units take the colour
  of the words written beside the notation, or a palette colour.
* `litologia` is optional: it chooses the hatch of the unit in the columns and in the sections; when it
  names no lithology, the description does.

| Hatch | Nouns | Adjectives, when no noun is there |
|---|---|---|
| horizontal lines (clay) | `argilla`, `argille`, `clay` | `argilloso` (`-a`, `-i`, `-e`), `clayey` |
| lines and dots (silt) | `limo`, `limi`, `silt` | `limoso`..., `silty` |
| dots (sand) | `sabbia`, `sabbie`, `sand` | `sabbioso`..., `sandy` |
| small circles (gravel) | `ghiaia`, `ghiaie`, `ghiaietto`, `ghiaino`, `ciottolo`, `ciottoli`, `gravel`, `pebble`, `cobble` | `ghiaioso`..., `ciottoloso`..., `gravelly`, `pebbly` |
| stars (stones, collapse, bricks) | `pietra`, `pietre`, `pietrame`, `pietrisco`, `sasso`, `sassi`, `crollo`, `crolli`, `maceria`, `macerie`, `laterizio`, `laterizi`, `mattone`, `mattoni`, `tegola`, `tegole`, `coppo`, `coppi`, `stone`, `rubble`, `brick`, `tile`, `collapse`, `debris` | `pietroso`..., `stony` |
| grid (mudbrick) | `mattone crudo`, `mattoni crudi`, `mattone di fango`, `mattoni di fango`, `terra cruda`, `mudbrick`, `mud brick`, `adobe` | |
| crosses (ash, charcoal) | `cenere`, `ceneri`, `carbone`, `carboni`, `carbonella`, `ash`, `charcoal` | `cineritico`..., `carbonioso`..., `ashy` |
| vertical lines (peat) | `torba`, `torbe`, `peat` | `torboso`..., `peaty` |
| diagonal lines (organic) | `humus`, `topsoil`, `terreno vegetale`, `terra vegetale`, `suolo vegetale` | `organico`..., `humico`..., `organic` |

Whole words count, not parts of words; capitals and accents do not, and the English plurals (`clays`,
`stones`, `bricks`, `ashes`...) are read too. The lithology column decides when it names a lithology,
the description otherwise. Inside a text the noun decides: in Italian the first one (`ghiaia sabbiosa`
is gravel, `limo argilloso` silt, `crollo di mattoni crudi` stones), in English the last one of a
compound (`sandy gravel` is gravel, `silty clay` clay, `mudbrick collapse` stones); an adjective counts
only when there is no noun (`argilloso`: clay). A text that names no lithology gives no hatch:
`concrezioni carbonatiche` has none, `argilla con concrezioni carbonatiche` is clay. In the sections and
in the legends a unit takes the hatch of its most frequent lithology and description; in the columns
every interval takes its own.

### Gaps and lost core { #gaps }

* Do not write a row for a stretch that was not recovered: leave the gap between two intervals.
* A gap inside one unit (the same unit above and below it) has no contact: the plugin reports it -
  *gap between 8.9 and 9.1 m inside US 4 (no contact)* - and the column shows it blank.
* A gap between two different units puts their contact in the middle of the gap: *gap between 2 and
  2.5 m (contact in the middle)*. If you know where the contact is, close the gap.
* Do not make a unit of the lost core (`perdita`, `lacuna`): it would become a unit of the sequence.

### Boreholes that stop early { #early }

A borehole that stops before the natural - in hard deposits, on a stone, at the water table - is
recorded down to where it stopped, with the depth reached. It says nothing about the units below
except that they are deeper than its bottom: where a surface interpolated from the other boreholes
would pass above that bottom, the plugin keeps it below, and the report counts the *borehole bottoms
used as a bound*. In the drawings the bottom of every borehole is marked by a short line.

### Example files { #example-files }

| File | Layout | What it shows |
|---|---|---|
| [sondaggi_collari.csv](geoarchaeology/sondaggi_collari.csv) | collars; Italian names, semicolons, decimal commas | 18 boreholes, 5 of them without elevation (read from the DEM) |
| [sondaggi_intervalli.csv](geoarchaeology/sondaggi_intervalli.csv) | intervals; same format | 117 intervals with Munsell colours, a lost core in S1 between 8.90 and 9.10 m, boreholes that stop before the natural, units missing from some logs |
| [boreholes_single_table.csv](geoarchaeology/boreholes_single_table.csv) | single table; English names, commas, decimal points | four of the same boreholes with kind and lithology, the position on the first row of each borehole, S9 without elevation |
| [intervalli_per_quota.csv](geoarchaeology/intervalli_per_quota.csv) | single table, intervals by elevation (`quota_tetto`, `quota_letto`) | S4 and S9 written with absolute elevations |

They are the simulated data of the demonstration, in its local reference system: use them as templates
and replace codes, coordinates and logs with yours (their coordinates do not fall on your survey). The
first lines of the two tables:

```text
sondaggio;x;y;quota;profondita
S1;611,00;799,00;307,130;12,00
S2;607,50;836,00;298,617;12,00
S3;612,50;866,00;;7,90
```

```text
sondaggio;da;a;us;descrizione;colore
S1;0,00;0,34;US 1;terreno vegetale (humus);10YR 3/2
S1;0,34;1,31;US 2;crollo e colluvio di mattoni;7.5YR 6/4
S1;1,31;2,43;US 3;argilla limosa grigia, carboni;10YR 5/2
```

of the single table in English (S9 has no elevation: it is read from the DEM):

```text
hole_id,kind,easting,northing,elevation,depth,from,to,unit,description,colour,lithology
S9,auger,607.00,680.00,,3.00,0.00,0.35,US 1,topsoil (humus),10YR 3/2,organic
S9,,,,,,0.35,1.85,US 5,sterile yellowish-brown clay,10YR 6/6,clay
S9,,,,,,1.85,3.00,US 5,"silty clay with carbonate concretions, sterile",10YR 7/4,clay
```

and of the table by elevations:

```text
sondaggio;x;y;quota;quota_tetto;quota_letto;us;descrizione;colore
S9;607,00;680,00;285,553;285,553;285,203;US 1;terreno vegetale (humus);10YR 3/2
S9;607,00;680,00;285,553;285,203;283,703;US 5;argilla bruno-gialla sterile;10YR 6/6
```

### Preparing the tables in a spreadsheet { #spreadsheet }

Any spreadsheet will do: one sheet per table, the column names in the first row, then one row per
borehole (collars) or per interval, and nothing else on the sheet - no titles, merged cells or totals.

* **Format** the code columns (`sondaggio`, `us`) as *Text* before typing, so that codes such as `1-2`
  or `01` do not turn into dates or numbers. The number columns can keep their format: decimals and
  thousands separators are read in the context of the file (below).
* **LibreOffice Calc**: *File > Save As...*, file type *Text CSV (.csv)*, tick *Edit filter settings*,
  then character set *Unicode (UTF-8)* and field delimiter `;`. Decimals are written as the cells show
  them (a comma with Italian settings): the plugin reads both.
* **Excel**: *File > Save As*, type *CSV UTF-8* (or *CSV*). Depending on the settings of the system,
  Excel separates the fields with commas or with semicolons and puts quotes around the cells that
  contain the separator: the plugin reads both.

The plugin finds the separator by itself (comma, semicolon, tab or vertical bar; spaces when there is
none of these), reads files in UTF-8 or in the Windows encoding of Excel, and skips empty lines and
lines that start with `#`, handy for notes at the top of a table.

* **Numbers** are read with a decimal point or a decimal comma, with or without thousands separators
  (`4.500.810,5`, `4,500,810.5`, `4 500 810`, `4'500'810`), each in the context of its file: `1.234` is
  1234 in a file whose numbers have a decimal comma, 1.234 otherwise.
* Empty cells, `-`, `na`, `n/a`, `n.d.`, `nd`, `null` and `?` mean no value; any other text in a column
  of numbers is named with its line (*S2: x 'cinquecento' is not a number (line 3)*).
* Text in quotes may contain the separator (`"argilla limosa, carboni"`): spreadsheets add the quotes
  by themselves. A row typed by hand without them - a description with the separator in it, a decimal
  comma in a file separated by commas - has more fields than the header: the plugin puts it back
  together when that can be done in one way only and names the lines (*decimal comma in a file
  separated by commas: 1 row put back together (line 2)*); otherwise it leaves the row out, with its
  line.

### In the field { #field }

* Decide the unit codes on the first day, as a short list with a description of each, and use the same
  codes in every log: correlating the boreholes means giving the same code to the same unit.
* One row per interval: from, to, unit, description, colour. Measure the depths from the ground at the
  top of the borehole, always the same way.
* Start every log at 0, write down the depth reached, leave lost core as a gap.
* Measure the position of every borehole with the GNSS receiver or the total station, in the reference
  system of the photogrammetric survey, and write the system on the sheet. Take the elevation in the
  same height system as the DEM, or leave it to the DEM.
* Write Munsell colours as notations (`2.5Y 5/2`, or `2,5Y 5/2`) and the main lithology with its noun
  (`argilla`, `limo sabbioso`, `ghiaia`...): the noun chooses the hatch.
* Give the boreholes short, unique codes (`S1`, `S2`... or `T1`, `T2`...); a borehole drilled again
  next to a failed one gets a new code.
* A trench section becomes one or more logs: a column every metre or two along the wall, its collar at
  the top of the wall, kind `trench`.

### Common mistakes and the warnings they give { #mistakes }

At the end of the import the plugin lists what it guessed, put back together, left out or found odd:
the first 15 warnings in the window, all of them on the command line. Line numbers are those of the
file as a text editor shows them, empty lines and notes included.

| What happened | What the plugin does or says | What to do |
|---|---|---|
| no column for the borehole, or a name it does not know | *no column of the borehole (id, sondaggio, hole...)*: nothing is imported | rename it `sondaggio` |
| a table with neither x and y nor from and to | *neither coordinates (x, y) nor intervals (from, to)*: nothing is imported | check the column names |
| no collar with coordinates | *No borehole with coordinates in the files read* | add the collar table, or x and y |
| a column with a name it does not know (`Strato archeologico`, `Quota relativa`) | *column not used: 'Strato archeologico'*; without the unit every interval is also *without a unit (line 2): left out*, without the elevation the elevations are read from the DEM | rename it (`us`, `quota`...), or leave it if it is a column of your own |
| a name that only guesses its field (`codice`, `sigla`, `h`) | read, and said so: *column 'codice' taken as the unit* | nothing if right, otherwise rename it |
| elevations in `tetto`/`letto` or `top`/`bottom` | read as elevations, and said so: *'tetto', 'letto' read as elevations (the base is below the top in 2 rows of 2)* | nothing; `quota_tetto` and `quota_letto` leave no doubt |
| decimal commas in a file separated by commas, without quotes (typed by hand) | the row is put back together when that can be done in one way only (*decimal comma in a file separated by commas: 1 row put back together (line 2)*), otherwise left out (*line 3: 7 fields instead of 5 and more than one way to put them back together: left out ...*) | separate the columns with semicolons, or use decimal points |
| the separator inside a text without quotes (`humus; radici` in a table separated by semicolons), or between the three numbers of a colour (`139;90;43`) | put back together when that can be done in one way only: *the separator inside a field without quotes: 2 rows put back together (lines 2, 3)*; otherwise left out, with its line | quote the text, or write `139,90,43` |
| text in a column of numbers (`cinquecento`) | *S2: x 'cinquecento' is not a number (line 3)*; a collar without its x or y is left out with its intervals | write the number |
| a row without the code of the borehole | *line 3 without the code of the borehole: left out* | write the code |
| a collar without x and y | *S2 without x and y: left out*, and its intervals are *without the position of the borehole* | add the position |
| the same unit written in different ways (`US 1`, `US1`, `us 1`) | read as one unit, and said so: *units written in different ways, read as one: US 1 (US1, us 1)* | nothing, if they are one unit |
| a plain number shared by two kinds of unit (`4` beside `US 4` and `USM 4`) | *unit '4': it could be US 4 or USM 4, kept apart* | write the kind |
| the same code for two different layers, or a log with the units in another order | *US 2 above US 3 in 2 boreholes, below in 1* in the order; *S7: US 2 below US 3, against the order of the units* with the surfaces | check the logs, give different layers different codes |
| elevations in another height system | *Collar elevation minus DEM* with a median of metres | bring them to the system of the DEM, or leave them empty |
| coordinates in another system, or x and y swapped | the boreholes fall outside the DEM: *Without elevation (neither in the file nor from the DEM)* when the table has none, and in the view they are not where they should be | convert the coordinates |
| a log that does not start at 0 | no message: the top of the first unit is interpolated instead of being the DEM | start at 0 |
| a row without `a` (or without `da`) | *S1: line 5 without 'a': left out* | fill it in |
| the base above the top | *interval 2-1.8 m with its base above its top (line 6): left out* | correct the depths |
| two intervals that overlap | *overlapping intervals at 0.9 m: the second starts at the base of the first*, or *interval ... inside the one before: left out* | correct the depths |
| a gap between two intervals | *gap between 2 and 2.5 m (contact in the middle)*, or *gap ... inside US 2 (no contact)* | nothing, if it is lost core |
| an interval without a unit | *interval 2-2.1 m without a unit (line 9): left out* | write the unit |
| the depth reached less than the last base | *depth 1.5 m less than the base of the last interval (2 m)*: the base is kept | correct it |
| intervals of a borehole missing from the collars | *X9: intervals without the position of the borehole: left out* | add the borehole to the collars, with the same code |
| a borehole with two different positions | *B has two positions (line 4): the first kept* | one position per borehole |
| intervals by elevation, the collar without elevation and outside the DEM | *interval by elevations (line 2) without the elevation of the borehole: left out* | give the collar its elevation |
| a colour it cannot read | *colour 'xyzzy' not recognised (line 3): not used*: the unit takes another colour written for it, or a palette colour | a Munsell notation, `#rrggbb`, `r,g,b` or words |
| Munsell notations without the colour-science library | *Munsell colours not converted: the colour-science library is missing (the units take the colours of the palette)* | use the desktop application, or install the program with `[gui]` or `[all]` |

## Step by step in the program { #program }

### The pane

Once the plugin is installed and the program restarted, the *Geoarchaeology* pane opens on the right of the window, tabbed with *PyArchInit*; it can be moved, floated or closed like the other panes and opened again from *Vista > Pannelli*, or with
*Plugins > Geoarchaeology > Geoarchaeology...*. The *Plugins > Geoarchaeology* menu has the main steps
too: *Import boreholes...*, *Contact surfaces (kriging)...*, *Geoarchaeological section...*,
*Stratigraphic columns...*, *3D model of the units...*, *Hide the overlay*.

<figure markdown="span">
  ![The Geoarcheologia pane with the Surfaces tab open](../img/plugins/geoarchaeology/pane.png)
  <figcaption>The pane (Italian interface) after the surfaces of the demonstration: the <em>Surfaces</em> tab
  with method and range of every product; at the bottom, the range of the overlay shown (the thickness
  of US 4). Simulated boreholes.</figcaption>
</figure>

From the top: a short description; the state of the chunk (boreholes, units from the top down, when the
surfaces were built and on which grid, or *The boreholes or the order changed: build them again*); the
buttons of the steps; three tabs, *Boreholes*, *Surfaces* and *Sections*; the controls of the overlay
(*Band*, *Palette*, *Opacity*); *Overlay on the view*, *Hide*, *Open the drawing*, *Export...*,
*Open in QGIS* and *Remove*; at the bottom, the values at the two ends of the colours shown. The longer
steps run in the background with the progress bar, and the project is saved when they end.

### 1. Import the boreholes { #import }

*Import boreholes...*

| Field | What it does |
|---|---|
| *Collars, or a single table* | the collar table, or the single table |
| *Intervals (when in a separate file)* | the interval table; empty with a single table |
| *Kind* | *from the table*, or one kind for all the boreholes of these files: hand auger, core, trench section, test pit, other |
| *Replace the boreholes of the chunk (otherwise they are added)* | ticked: the boreholes imported before are forgotten; unticked: those with the same code are replaced and the others kept |

At the end a window sums up the import; with the example files (lines wrapped here):

```text
18 boreholes read (18 in the chunk), 117 intervals; units from the top:
  US 1, US 2, US 3, US 4, US 5
5 elevations taken from the DEM
Collar elevation minus DEM: median -0.008 m, largest +0.071 m (S17)
Warnings (1):
- S1: gap between 8.9 and 9.1 m inside US 4 (no contact)
```

The *Boreholes* tab lists the code, the elevation (`*`: read from the DEM), the depth and the units of
every borehole. *Boreholes in the view* draws them on the orthophoto or the DEM as a layer of red
points, *Geoarcheologia: sondaggi*, drawn again each time the view shows another raster: with the *Move*
tool, a click on a point shows its code, elevation, depth and units. The tables read are copied into the
project. New tables make the surfaces out of date: build them again.

### 2. The order of the units { #order }

*Order of the units...* shows the units from the top down, as found in the logs, and the disagreements
between the boreholes, if any (*US 2 above US 3 in 2 boreholes, below in 1*). To change the order, write
the codes from the top down separated by commas, in any spelling; the codes not written go after, in the
order of the logs. An order given by hand is kept until the boreholes bring new units. Build the
surfaces and the model again afterwards.

### 3. Contact surfaces { #surfaces }

*Surfaces...* interpolates, between the boreholes, the top of every unit: the surface where it begins.

| Field | Default | What it does |
|---|---|---|
| *Variogram model* | *chosen by the data* | spherical, exponential or Gaussian: how the differences of a contact grow with the distance. Chosen by the data: the steadiest model among those whose leave-one-out error is within 5 % of the best |
| *Trend* | *chosen by the data* | *none* (ordinary kriging) or *tilted plane* (universal kriging, for a contact that dips across the site); the plane is tried with six boreholes or more, not all in a line |
| *Interpolate* | per contact | the elevations of every contact, the depths of every contact below the ground, or each contact in its own way (below) |
| *By unit* | | the way of some units, for example `US 2=depth, US 5=elevation`, the codes in any spelling; `profondità` and `quota` work too |
| *Cell* | the DEM's | the size of the cells of the surfaces, in metres; over a large area the cell of the DEM is doubled, tripled... to stay within a million cells; without a DEM, a round size giving about 300 cells across the area |
| *Nugget* | fitted | the variance of a contact between two boreholes very close to each other, in m²: measurement errors and changes over short distances |
| *Margin around the boreholes* | their usual spacing | how far beyond the outer boreholes the surfaces go; by default the median distance between a borehole and its nearest one |
| *Least boreholes for kriging* | 5 | with fewer boreholes crossing a contact, inverse distance weighting (IDW) instead of kriging |

**By elevation or by depth.** A contact with a shape of its own - the natural under a tell, a buried
floor, a channel - is interpolated from its elevations. A contact that follows the ground - the base of
the topsoil, layers draped over a mound - is interpolated from its depths and hung from the DEM, so that
between the boreholes it keeps following the ground. *Per contact* chooses by itself: by depth when the
depths of the contact vary from borehole to borehole less than half as much as its elevations (both
measured around a plane), by elevation otherwise, and by elevation when fewer than three boreholes
cross it. When every log starts at 0, the top of the first unit is the DEM itself.

**Then:**

* a borehole that stops above a contact keeps the contact below its bottom;
* where two contacts cross, the one with the larger kriging variance - the less certain one - gives way,
  and no contact rises above the ground;
* a unit missing from some logs is missing around them too: the boreholes vote for its presence, the
  nearer ones counting more, and where those without the unit win and its two contacts do not show a
  real thickness (more than twice their uncertainty), its top and its base become one surface; no thin
  units appear between the boreholes by chance;
* the surfaces cover the area of the boreholes plus the margin, and have no value outside it.

At the end a window reports, contact by contact, how it was made. In the demonstration (lines wrapped
here):

```text
945 x 945 cells of 0.38 m
Top of US 1: the ground
Top of US 2: kriging Gaussian, leave-one-out error 0.026 m, by depth (it
  follows the ground: depths varying by 0.03 m, elevations by 6.22 m);
  moved by the stack on 9,736 m² (lowered up to 0.01 m, raised up to 0.02 m)
Top of US 3: kriging spherical, leave-one-out error 0.943 m (the Gaussian
  model gave 0.908 m: less than 5 % apart), by depth (...)
Top of US 5: kriging spherical, leave-one-out error 0.274 m (the
  exponential model gave 0.271 m: less than 5 % apart), by elevation (it
  has a shape of its own: depths varying by 2.84 m, elevations by 0.22 m);
  moved by the stack on 39,412 m² (lowered up to 3.44 m, raised up to
  1.60 m)
Thickness of US 2: mean 1.08 m, largest 5.20 m, 78,462 m³ (+ 2,787 m³
  where there is no DEM); missing on 4,956 m² (boreholes without it: S9,
  S10)
2,580 m² without a DEM: the ground there is the surface of the collars,
  the volumes apart
```

[Reading the results](#reading) explains these numbers. The *Surfaces* tab lists *Top of ...* for every
unit and *Thickness of ...* for every unit but the last, with the method (the model, `+ plane`, by
elevation or by depth) and the lowest and highest values; the tooltip of a row repeats its report.

### 4. Surfaces on the view { #overlay }

Choose a row of the *Surfaces* tab, then:

* *Band*, for the tops only: *Elevation*, *Kriging variance* (the uncertainty, m²) or *Correction by the
  stack* (how much the contact was moved, m);
* *Palette*: the product's own (hypsometric for elevations, heat for variances, diverging for
  corrections, a brown ramp for thicknesses) or another one (greys, earth...);
* *Opacity*.

*Overlay on the view*, or a double click on the row, lays it over the orthophoto or the DEM; the line at
the bottom of the pane says what is shown and the values at the two ends of the colours (the 2nd and
98th percentiles). Thicknesses and variances start from zero. The corrections are centred on zero:
the cells where a contact was lowered are red, those where it was raised blue, those it kept white.
*Hide* takes the overlay away; one is shown at a time.

### 5. Sections { #sections }

* *Draw a section*: click the vertices of the trace on the orthophoto or the DEM - two or more - and
  double click or press Enter to end it (the drawing of the program, lent to the plugin); Backspace takes
  the last vertex back, Esc or another tool of the *Drawing* bar cancels. The program's own line tool
  keeps drawing the program's sections.
* *Section through the boreholes*: select two or more boreholes in the *Boreholes* tab (with Ctrl, or
  Cmd on the Mac); the trace joins them in the order you picked them (a range picked with Shift comes
  in the order of the table).

| Field | What it does |
|---|---|
| *Name* | the title of the drawing and the name of its files; a section with the same name is replaced |
| *Boreholes within* | how far from the trace a borehole is drawn on it, in metres; empty: half the usual spacing of the boreholes |
| *Scale 1:* | empty: as for the program's own sections, the most detailed standard scale (1:20, 1:50, 1:100...) that fits about an A0 sheet |
| *Vertical exaggeration* | empty: chosen so that the drawing is not a thin strip (1, 2, 5, 10, 20, 50 or 100) |

The drawing has the ground (black), the top of every unit (grey), the units filled with their colour and
hatch - the last one down to the bottom of the drawing -, the boreholes near the trace as columns with
their code and their distance from the trace (positive on the left, looking along the trace), the
legend of the units. The files are an SVG and a PNG; a DXF in section coordinates (distance along the
trace and true elevation, in metres, without exaggeration; layers `GEO_TERRENO`, `GEO_CONTATTI`,
`GEO_UNITA_<unit>`, `GEO_SONDAGGI`, `GEO_TESTI`, `GEO_GRIGLIA`); a CSV of the profile (distance, x, y,
ground, top of every unit). *Open the drawing*, or a double click in the *Sections* tab, opens the PNG;
the traces are drawn on the view as the layer *Geoarcheologia: sezioni*, again on every raster the view
shows. A section is a drawing made at that moment: after building the surfaces again, draw it again.

<figure markdown="span">
  ![A north-south section across the mound with the units and the boreholes projected on it](../img/plugins/geoarchaeology/section-ns.png)
  <figcaption>A section of the plugin through nine boreholes, 1:1000 with vertical exaggeration x4:
  ground from the DEM, contacts by kriging, the boreholes within 15 m with their distance from the
  trace. Simulated boreholes on the real DEM of Tell Sultan.</figcaption>
</figure>

### 6. Stratigraphic columns { #columns }

*Stratigraphic columns*: tick the boreholes; those selected in the table come first, in the order you
picked them.

* One borehole: its column with the depths, the elevations when the collar has one, and the
  description and colour of every interval.
* Several: a panel of columns side by side, with the tops of the same unit joined by dashed lines.

*Vertical axis*: elevations when every borehole has one (the default), elevations, or depths.
*Scale 1:*: empty to fit an A4 sheet (from 1:10 to 1:1000). The plugin writes an SVG and a PNG and opens
the PNG. Every panel has files of its own, named after its boreholes (`colonne_S5-S4-S3-S2-S1.svg`,
`.png`), so a new panel replaces only the panel of the same boreholes; from the command line it can be
written to a file of your choice.

<figure markdown="span">
  ![A panel of nine stratigraphic columns on a common elevation axis](../img/plugins/geoarchaeology/columns.png)
  <figcaption>The north-south transect as a panel of columns: Munsell colours of the logs, hatches from
  the descriptions, dashed lines joining the tops of the same unit, the lost core of S1 left blank.
  Simulated boreholes.</figcaption>
</figure>

### 7. 3D model of the units { #model }

*3D model...*, available once the surfaces exist.

| Field | What it does |
|---|---|
| *Cell* | empty: automatic, the grid of the surfaces made coarser up to 60,000 cells |
| *Clip the units to the US drawn with the same number* | a unit whose code matches US drawn in the chunk is cut to their polygons |
| *The last unit too, down to the floor* | the last unit has nothing below it: its solid goes down to a floor |
| *Elevation of the floor* | empty: the deepest bottom of the boreholes, kept below the lowest top of the last unit by a margin of 5 % of the height the boreholes span (at least 10 cm), so that the solid has no holes |
| *In the 3D view, units set apart vertically by* | empty: as they lie; a distance in metres raises every unit by that much above the one below it, so that units sharing their contacts can be told apart in the view (the files of the model are not moved) |

At the end the units appear in the program's 3D view - the *Model* tab of the central area - each in its
colour, under the title *Geoarchaeological units (5 units)*; to go back to the products of the chunk,
choose another view in the *Show* list of the 3D view. *Show in the 3D view*, in the pane, brings the
units back; *Open the folder* opens the folder of the model in the Finder or in the Explorer. A window
lists the volumes. The files are a PLY per unit (true coordinates in double precision, the colour of the
unit), one OBJ with an object and a material per unit (`unita.obj`, `unita.mtl`) and `volumi.csv`; open
them in MeshLab, Blender, CloudCompare or QGIS 3D. The volume of the last unit depends on the floor
chosen: it is not the volume of a deposit.

*Open as the model of a new chunk* (also in *Plugins > Geoarchaeology*) puts the units into a new chunk of
the project, *Geoarchaeological units - <chunk>*, as its model, in the reference system of the chunk of the
boreholes (the program imports `unita.obj`, the offset of its header added back). The new chunk becomes
the active one and its model appears in the 3D view, where the *Cut* button - or *Workflow > Cleaning and
confidence > Cut the model with a plane* - cuts it horizontally, vertically along a section or through
three points: every unit, a closed solid, is closed on the cut in its colour, so the faces of the cut show
the stratigraphy. The project shows as modified: save it to keep the new chunk. To go back to the
boreholes, make their chunk the active one again in the workspace: the pane follows the active chunk.

<figure markdown="span">
  ![The solids of five units and a cut through the model](../img/plugins/geoarchaeology/model-3d.jpg)
  <figcaption>The 3D model: one closed solid per unit with its volume, and a cut along the east-west
  section. Simulated boreholes on the real DEM of Tell Sultan.</figcaption>
</figure>

### 8. Export, QGIS, removing { #export }

* *Export...*: on the *Surfaces* tab with a row selected, that surface or thickness as a GeoTIFF;
  otherwise everything as a GeoPackage - boreholes (points), intervals (table), section traces (lines) -
  with a GeoTIFF for every surface and thickness and the CSV of the volumes beside it. The same
  GeoPackage comes from *File > Export > Boreholes and geoarchaeological surfaces (GeoPackage)...*.
* *Open in QGIS*: the program's QGIS project ([QGIS](../integrations.md)) with the surfaces - hidden at
  first, elevations in greys and thicknesses in a warm ramp - and the layers of the boreholes and of
  the sections; for a chunk in a local system too, in a project without a reference system.
* *Remove*: on the *Surfaces* tab all the surfaces and thicknesses (they form one stack); on the
  *Sections* tab the selected section and its drawings; on the *Boreholes* tab the selected boreholes.

## The implicit model { #implicit }

The surfaces of the steps above are one height per point, z(x, y), between the boreholes. That cannot draw what you dig:
a pit that cuts the layers and is then filled, a ditch with an undercut side, a contact seen in three boreholes that should take
its shape from the layers around it, a dip measured on a section. The implicit model describes the ground as scalar fields in 3D
with [LoopStructural](https://github.com/Loop3D/LoopStructural), so these things exist in it. It is built on request, beside
the kriging, which stays the default. It is new in version 0.2.0.

!!! warning "An interpretation of the data, not a measure"
    Away from the boreholes the model says what the data suggest. Pane and figures say so, and a number from
    the model should be reported as a number from a model.

### When to use it, and when kriging { #implicit-when }

| Your data | Use |
|---|---|
| Layers seen in most boreholes, one on the other | **kriging**: as good or better, in seconds |
| A pit, a ditch, a robber trench, with its fills | **implicit** |
| A contact seen in a few boreholes, whose shape should come from the layers around it | **implicit** |
| A dip measured on a section, contacts seen on a wall | **implicit**: kriging cannot use them |
| A volume of a plain deposit to report | **kriging** |

The leave-one-out errors of both are shown side by side after each build. These are from the demonstration below: 21 simulated boreholes on
Tell Sultan's real DEM, 18 of them without the invented pit. Kriging leaves out one borehole at a time and counts the boreholes where the
unit is missing; the implicit model leaves out 12 groups of boreholes and counts those that see the contact.

| Contact | Boreholes that see it | Without the pit: kriging / implicit | With the pit: kriging / implicit |
|---|---:|---:|---:|
| US 2, follows the ground | 16 | **0.03** / 0.10 m | **0.03** / 0.09 m |
| US 3, rare | 5 | 0.94 / **0.31** m | 0.87 / **0.31** m |
| US 4 | 12 | **1.33** / 1.61 m | **1.28** / 1.62 m |
| US 5, natural | 13 | **0.27** / 0.31 m | 0.35 / **0.31** m |

Kriging stays better on the plain layers; the implicit model wins on the rare contact. With the pit, kriging gets worse on the natural
because three boreholes find it lower down and it does not know why. The vote that stops a rare unit, below under
[Rare units and volumes](#implicit-volumes), acts on the solids and not on these errors, which are the same with and without it.

### Installing it { #implicit-install }

LoopStructural does not come with the plugin. *Install LoopStructural* in the *Implicit* tab (or
`ppg plugin geoarchaeology implicit-install`) puts it in an environment of its own, about 300 MB, in the folder of the plugin's data. It
needs the internet once and takes a few minutes; `uv` is fetched if missing. The program never loads LoopStructural: every
model runs in a separate process, and a failure or a cancel leaves the present model as it was. If LoopStructural is already installed
in the Python of the program, the plugin uses that; `PPG_GEOARCH_LOOP_ENV` puts the environment elsewhere.

### The steps { #implicit-steps }

1. Import the boreholes and build the surfaces as usual: the model takes its grid and the series from them.
2. In the program, draw the US of the cut (or of its fill) with its number. Do this first: the outline counts more than
   anything else.
3. In the *Implicit* tab, *Mark a fill...*: the fill, as in the logs; the cut, its US number or a name; the shape of the floor.
   Repeat for every fill of the cut.
4. *Contacts and orientations from a file...*, if you have them.
5. *Build...*: 2 minutes on the demonstration, with 12 groups for the leave-one-out. Set the groups to 0 to try things out.
6. The model becomes the model of the chunk, with its surfaces listed as *Top of US n (implicit)* in the *Surfaces* tab. A
   kriging *3D model...* afterwards replaces it; the implicit surfaces stay. Sections use the implicit surfaces when the
   implicit model is the active one.

<figure markdown="span">
  ![The Implicit tab with the table of the cuts and the last result](../img/plugins/geoarchaeology/pane-implicit.png)
  <figcaption>The <em>Implicit</em> tab: where LoopStructural runs, the buttons, the cuts with the shape of
  their floor, the text of the last build.</figcaption>
</figure>

| Field of *Build...* | What it does |
|---|---|
| *Cell* | empty: that of the surfaces, larger when more than 120,000 cells would be needed; it can be finer than the surfaces |
| *Groups of boreholes for the leave-one-out check* | at most 12; 0 for none |
| *By unit* | `US 2=depth, US 5=elevation`; empty: as the surfaces |
| *In the 3D view, units set apart vertically by* | as in the 3D model |

### Series { #implicit-series }

A *series* is one scalar field. As for the kriging [modes](#surfaces), the contacts that follow the ground go together in one series in
depth under the DEM, and each contact by elevation is a series of its own. In the demonstration: US 1 to US 4 in depth, the natural US 5 by
elevation. *By unit* changes it.

### Cuts and fills { #implicit-cuts }

A cut has no material: the logs show its fill. So the cut is not a layer, nor a field: it is an excavation record.

* **The plan** is the US drawn for the cut, or for its fills. Without a drawing it is estimated, the cells nearer to the boreholes
  inside than to those outside, with a warning; in the demonstration this gave 53-92 m³ for a fill of 76 m³.
* **The floor** hangs from where the cut opens: `opening - s(x, y) P(t)`. The opening is the ground or, under a cover, the top of the first fill each borehole meets,
  whichever fill it is, and the same surface is the top of the youngest fill: so a sealed pit keeps its whole plan and the cover takes none of its space. `t` is the distance from the edge over the local half width, `P` the shape chosen, `s` the
  measured depths divided by P and spread over the plan, a borehole weighing `P²`. No floor goes deeper than a borehole away from the edge implies: its depth over the shape at its place. Between the boreholes it is an assumption.
* **Near the edge** (`t` under 0.25) a measure tells how steep the wall is, not how deep the middle goes: it is left out of `s` when another one stands
  further in. If it is deeper than the shape, the floor comes down towards it only as far as the boreholes that count allow, and the measure is met at its own cell: deeper than all of those it is a local sump in the model, and `rim_deeper` says so. If it is shallower it is not used. So the floor
  passes through every base measured, except those shallower than the shape next to the edge (`base_near_edge`).
* **The fills** have tops as depths under the ground, spread from the boreholes that saw them: they follow the ground unless
  the boreholes say otherwise. A deeper fill stays under the one above it. A layer can cover the pit, as the topsoil does
  in the demonstration. **Fills are a stack, one over the other**: fills side by side, tip lines, a lens seen in one borehole are not modelled. The volume of each such fill is wrong and that of the cut may be too; `fill_thinned` says so when a borehole logged twice what the model has.
* **Pits that cut pits** are read from the logs: each fill stays with its cut, and the pit seen higher in a borehole through both is the younger.
* **An undercut** (a bottle pit, a ditch with an overhanging side) needs measures on the wall: rows of the kind `wall` in the contacts file.
  Only such a cut uses a field of LoopStructural, and few points smooth it away: only a bottle pit with wall points all round has been tried.

<figure markdown="span">
  ![Section through the pit at true scale, with the three boreholes](../img/plugins/geoarchaeology/implicit-section.jpg)
  <figcaption>Section through the invented pit at true scale: it cuts US 2 and goes into the natural, its two fills under the topsoil, the
  boreholes P1, P2, P3 projected onto the line. The floor passes through each of their bases.</figcaption>
</figure>

### The volume of a cut is a range { #implicit-range }

A few boreholes cannot tell the shape of a floor. So the plugin computes the fill for all three shapes, and the row of the cut in
`volumi.csv`, the pane and the text gives the range; the chosen shape sets the solid and which value of the range the model has, which can be one of its ends. **The range is of
the fill**: from the floor up to the top of the youngest fill, without what covers it. In `volumi.csv` the `volume_m3` of the row of the cut is empty, the volume being in the rows of its fills: a sum of the column counts each pit once.

| Invented pit | Flat floor | Bowl (chosen) | V | True |
|---|---:|---:|---:|---:|
| Fill | 118.2 m³ | **97.2 m³** | 79.7 m³ | 76.4 m³ |

The true volume is known here because the pit is invented, with a floor between the V and the bowl of the plugin. It lies 4 % *below*
the range, and the bowl is 27 % too much: three boreholes nearly in a line say little. The range tells how much the
shape matters; it does not always hold the truth. More boreholes across the pit, and at least one near the middle, are what narrows it.

### What a fill volume means { #implicit-fill }

It is the space between the assumed floor and the top of the fill, within the drawn plan, in the cells of the grid. Here the plan has 58 cells of
1.14 m, 75.5 m² against 76.2 m² drawn. It is not a measured amount of material: give it as a range, with the number of boreholes. The plan
holds the fill under a cover as well: a pit opening 1.5 m down keeps all of its plan. A floor
measured only beside the edge says nothing of the middle (`floor_unmeasured`), and then the volume is a guess whatever the range says.

<figure markdown="span">
  ![Thickness of the fills in plan, implicit model and kriging](../img/plugins/geoarchaeology/implicit-fill-thickness.jpg)
  <figcaption>The thickness of the fills. Left, the implicit model stays in the drawn outline. Right, kriging takes the three boreholes for a
  layer and spreads it over 1,800 m² with 2,004 m³, twenty times too much.</figcaption>
</figure>

### Rare units and volumes { #implicit-volumes }

A field keeps a thickness everywhere, so a unit seen in a few boreholes would spread over the whole site. The model applies the rule of the
kriging: the boreholes vote for the unit by inverse distance, those that pass it by against, and where those without it win it is not
there, with a vertical edge; a thickness the kriging can tell from none stays. A borehole inside a cut does not vote against what the cut took away.
Where a unit is not, its voxels go to the nearest layer above it in its series that is there, so the top of the layer under it stays where its own boreholes put it; with no layer above in the series they go to the layer under it. Fills and the inside of cuts neither give nor take. The summary says from how many cells a unit was taken. There is no warning: it is the normal
working of the model. In the demonstration US 3, seen in 5 boreholes of 21, covers 10,368 m² against the kriging's 9,647; without this rule it covered 59,600 m².

`volumi.csv` has `kriging_m3` and `same_cells_m3`: the volume of each method on the cells both have. The kriging stops at the margin around the
boreholes, the implicit model covers every cell with a DEM on its grid, so `volume_m3` covers more; compare `same_cells_m3` with `kriging_m3`, as the summary does. On the
73,834 m² of the demonstration (simulated boreholes, an invented pit):

| Unit | Kriging | Implicit, same cells | Implicit, all of it (`volume_m3`) |
|---|---:|---:|---:|
| US 1 | 22,974 m³ | 21,981 m³ | 28,321 m³ |
| US 2 | 75,705 m³ | 93,068 m³ | 117,294 m³ |
| US 3, rare | 25,341 m³ | 20,466 m³ | 20,466 m³ |
| US 4 | 181,703 m³ | 167,112 m³ | 167,133 m³ |
| US 5, down to the base of the model | 263,288 m³ | 268,208 m³ | 371,845 m³ |

The whole deposit above the natural differs by 1 % (305,723 against 302,627 m³), single units by up to 23 %: where US 3 is not, US 2 takes its space, and it is 23 % over the
kriging, US 3 19 % under, US 4 8 % under. Trust the volume of the deposit before those of its units.

**Trust the two figures together.** After each build the summary gives the leave-one-out of a contact and, for every unit, the model against the logs at its own boreholes
(`rmse`, maximum). They answer two questions: how well the data tell a contact where a borehole is missing, and whether the model keeps the boreholes it has. A low leave-one-out alone
hides a model that left its logs: in a test with a lens 1.2 m thick in 8 boreholes of 25 the model had it at 0.09 m on average, and the
leave-one-out of that unit was 0.00 m. In the demonstration:

| Contact | Boreholes | Model against logs: rmse / maximum |
|---|---:|---:|
| US 1 | 21 | 0.08 / 0.19 m |
| US 7, US 8 | 3 | 0.01 / 0.02 m |
| US 2 | 16 | 0.07 / 0.18 m |
| US 3 | 5 | 0.17 / 0.34 m |
| US 4 | 12 | **1.35 / 4.36 m** |
| US 5 | 16 | 0.02 / 0.07 m |

<figure markdown="span">
  ![The solids around the pit, cut along the section](../img/plugins/geoarchaeology/implicit-solids.jpg)
  <figcaption>The solids around the invented pit, trimmed and cut along the section, each unit closed on the cut. Vertical exaggeration x3.</figcaption>
</figure>

### More data { #implicit-data }

*Contacts and orientations from a file...* reads CSV tables as the logs are read: comma, semicolon or tab, decimal point or comma, names
in Italian or English. A new file of a kind replaces the old one.

**Contacts**: a point where a unit begins, seen on a section or on the surface.

```text
unita;x;y;quota;tipo
US 3;588,20;741,50;286,15;
US 9;591,00;713,00;285,10;parete
```

The kind is empty, or `wall` (`parete`, `muro`, `side`) for a point of the wall of a cut. A contact of a cut is a point of its floor, and so is a spot height with its US. A height taken on the edge of a cut
is not a point of its floor: it is left out, and `base_near_edge` says how many.

**Orientations**: a dip measured.

```text
unita;x;y;z;inclinazione;immersione
US 5;560,00;700,00;285,50;4,0;220
```

Dip in degrees from the horizontal (`dip`, `inclinazione`, `angolo`, `pendenza`), dip direction in degrees clockwise from north
(`dip_direction`, `immersione`, `azimut`). A dip against what the boreholes say gives way to them, or is dropped with a warning; on a fill, or on a cut
without walls, it is of no use and is left out.

**Spot heights** of the program with the number of a US are contacts of that unit, without any file.

A contact is not honoured exactly: the field is smooth, and it leans on contacts and spot heights. In a test two points 0.8 m under a contact seen by 16 boreholes moved it by
a fraction of that. To change the shape of a contact, give it several points or a dip.

### Warnings, and what to do { #implicit-warnings }

| The text says | Meaning | What to do |
|---|---|---|
| *nobody drew the outline* | the plan is estimated, the volume may be over or under | draw the US of the cut |
| *all the boreholes are near the edge* (`floor_unmeasured`) | the depth of the floor was not measured | a borehole near the middle; until then the volume is a guess |
| *measures of the floor next to the edge were not used* (`base_near_edge`) | the edge does not tell how deep the middle goes; a height on the edge is left out | nothing for a height on the edge; for a borehole, check it and the outline |
| *a measure next to the edge reaches ... m, more than any measure away from the edge* (`rim_deeper`) | that depth stands at one place only, a local sump; the rest follows the other boreholes | if it is the wall, a borehole further in; check the log |
| *the floor reaches ... m, the deepest measure is ... m* (`floor_past_measure`) | with all measures by the edge nothing stops the floor | a borehole near the middle |
| *separate parts of the outline have no measure of the floor* (`plan_part_unmeasured`) | their depth comes from boreholes far away | a borehole in each part, or a cut for each |
| *in borehole ... the logs give ... m, the model ... m* (`fill_thinned`) | fills are a stack, and this one has lost its place | read the fill volumes as one; give the fills side by side as one unit |
| *the model passes far from the logs of its own boreholes* (`logs_missed`) | rmse over 0.25 m or two voxels; leave-one-out alone does not show it | a series by depth or by elevation for that unit, and do not quote its volume alone |
| *a measure of the floor ... is outside the outline* | it counts as if on the edge | check the position of the borehole, or the drawing |
| *the outline is only ... m wide* | the cell is too large for that shape | build with the cell it gives |
| *the outline measures ..., ... are left on the grid* | it leaves the grid, or the cell is large for it | a smaller cell, or surfaces with a larger margin |
| *the boreholes ... end inside the fill* | the floor is deeper and the volume understated | deepen a borehole, or say so |
| *fill ... with no measured top* | how far the fill reaches is not known | a borehole through it, a contact |
| *Voxels of ... m instead of ...* | at most 200 levels; thin units are poorly drawn | a `--floor` nearer to the surface, a smaller range of heights |
| *the contact leaves the range of heights* | it has no top in part of the grid | none needed if that part is far from your interest |
| *no borehole has this contact with the others* | its distance from them is estimated | a borehole that sees it with the others |
| *the boreholes do not make a plane* | across the line the contact is level | a borehole off the line, or a dip |
| *boreholes have two units in the order opposite to the column* | the order or the logs are wrong | *Order of the units...*, the logs |
| *the orientation ... contradicts the contact* | it would have turned the model over; left out | check its direction |

### Limits { #implicit-limits }

* **Narrow necks are not warned.** A neck under 5 % of the plan gives no warning (a 1.2 m neck in a 6 m ditch with a 1 m cell): look at the plan.
* **A contact is not honoured exactly**, as above: a smooth field leans on it. In the demonstration the top of US 4 stays 1.35 m from its logs (rmse), 4.36 m at worst.
* **Fills are a stack**: fills side by side and tip lines are not modelled.
* **An estimated edge is rough**, either way: +125 % and -54 % in tests.
* **A floor measured at the edge only** (`floor_unmeasured`) is not a measure of the middle: a borehole at the rim of a bowl gave less than a third of the volume.
* **A rare unit ends at a vertical edge** halfway between the boreholes, as in the kriging, and the layers around it share out its space unevenly, as above.
* **The shape of a floor is assumed.** A flat floor comes out rounded at the first quarter of its half width: a truly vertical wall is outside the range, 16 % over the
  flat floor on a pit of radius 8. One borehole in the middle of such a pit, with the bowl chosen, gives 40 % less than the truth: nothing says that the wall is steep.
* **The grid**: at most 120,000 cells and 200 levels by default. A pit should be at least several cells wide; here 9 cells, voxels of 0.13 m.
* **Not done**: faults, folds, intrusions, editing the model by hand, ensembles.

## From the command line { #cli }

The same steps with `ppg plugin geoarchaeology WHAT PROJECT ...`. Every command works on the active
chunk (`--chunk LABEL` for another one) and saves the project.

```bash
ppg plugin geoarchaeology help
ppg plugin geoarchaeology import scavo.ppg sondaggi_collari.csv sondaggi_intervalli.csv
ppg plugin geoarchaeology import scavo.ppg campagna2.csv
ppg plugin geoarchaeology import scavo.ppg tutti.csv --replace --kind core
ppg plugin geoarchaeology list scavo.ppg
ppg plugin geoarchaeology order scavo.ppg "US 1,US 2,US 3,US 4,US 5"
ppg plugin geoarchaeology surfaces scavo.ppg
ppg plugin geoarchaeology surfaces scavo.ppg --mode "auto,US 2=depth,US 5=elevation" --min-points 6
ppg plugin geoarchaeology section scavo.ppg --through S9,S8,S7,S6,S1,S2,S3,S4,S5 --name "Sezione N-S"
ppg plugin geoarchaeology section scavo.ppg --coords "455,800;760,800" --distance 15 --scale 1000 --exaggeration 4
ppg plugin geoarchaeology logs scavo.ppg S1
ppg plugin geoarchaeology logs scavo.ppg S5 S4 S3 S2 S1 --out colonne_ns.pdf
ppg plugin geoarchaeology model scavo.ppg --offset 600,800,0
ppg plugin geoarchaeology model-chunk scavo.ppg --label "Units to cut"
ppg cut-model scavo.ppg --chunk "Units to cut" --vertical 455,800,760,800 --keep both --out cut.ply
ppg plugin geoarchaeology implicit-install
ppg plugin geoarchaeology implicit-data scavo.ppg --contacts contatti.csv --orientations giaciture.csv
ppg plugin geoarchaeology implicit scavo.ppg --fill "US 7=US 9" --fill "US 8=US 9" --cut "US 9=bowl"
ppg plugin geoarchaeology export scavo.ppg geoarcheologia.gpkg
ppg plugin geoarchaeology export scavo.ppg naturale.tif --id top_4_US_5
ppg plugin geoarchaeology remove scavo.ppg surfaces
```

| Command | Options |
|---|---|
| `help` | every command with its options |
| `import PROJECT FILES...` | one or more tables (collars, intervals, single tables, in any order); the boreholes are added to those of the chunk, replacing those with the same code; `--replace` forgets those there were; `--kind` auger, core, trench, test_pit or other for all of them |
| `list PROJECT` | boreholes, units, surfaces, sections, model; `--json`: the whole record, with every variogram model tried and its error |
| `order PROJECT UNITS` | the codes from the top down, separated by commas, in any spelling (`us3` is `US 3`) |
| `surfaces PROJECT` | `--model` auto, spherical, exponential, gaussian; `--trend` auto, none, linear; `--mode` auto, elevation or depth, and the units named (`"auto,US 2=depth"`); `--cell` m; `--nugget` m²; `--buffer` m, the margin; `--min-points` (5); `--order` the units from the top down |
| `section PROJECT` | `--coords "x1,y1;x2,y2;..."`, a trace of two or more points in the reference system of the chunk (`--coords=...` when the first number is negative), or `--through S1,S4,S7`, in that order; `--name`; `--distance` m; `--scale` (the N of 1:N); `--exaggeration` |
| `logs PROJECT [CODES...]` | the boreholes (all when none is given); `--out` an `.svg`, `.png` or `.pdf` file, otherwise `logs/<borehole>` for one borehole and `logs/colonne_<boreholes>` for a panel; `--vertical` auto, elevation, depth; `--scale` |
| `model PROJECT` | `--cell` m; `--no-clip`; `--floor` elevation; `--no-last` leaves the last unit out; `--offset X,Y,Z` subtracted from the coordinates of the OBJ (written in its header and in `unita.obj.offset.txt`); `--explode` m, the units set apart vertically in `unita_vista.ply`, the file of the 3D view |
| `model-chunk PROJECT` | the units of the model as the model of a new chunk of the project, in the reference system of the chunk (the offset of the OBJ added back); `--label` the label of the new chunk (default *Geoarchaeological units - <chunk>*, in the language of the interface, numbered when taken); the active chunk stays the same. `ppg cut-model PROJECT --chunk LABEL ...` cuts it, every unit closed on the cut in its colour |
| `implicit PROJECT` | the implicit model; `--cut UNIT=PROFILE` (flat, bowl, v), `--fill FILL=CUT`, both repeated; `--cell` m; `--folds` (12, at most; 0 none); `--series "US 2=depth,US 5=elevation"`; `--floor` elevation; `--explode` m |
| `implicit-data PROJECT` | `--contacts` and `--orientations`, CSV files |
| `implicit-status [PROJECT]` | where LoopStructural runs; with a project, the last implicit model |
| `implicit-install` | LoopStructural in an environment of the plugin; `--force` also when this Python has it, or again |
| `export PROJECT OUT` | a GeoPackage with the GeoTIFFs and the volumes beside it; with `--id` (an id shown by `list`, such as `top_4_US_5` or `thickness_US_2`) one surface as GeoTIFF |
| `remove PROJECT [WHAT]` | `all` (the default) or `boreholes`: everything of the plugin in the chunk; `surfaces`, `sections`, `model`, `implicit`; the code of a borehole or the name of a section |

Large coordinates (UTM) lose precision in some 3D programs: `--offset` with round numbers near the
site keeps those of the OBJ small; the PLY files keep the true coordinates.

## Reading the results { #reading }

### The surfaces

Each *Top of US n* is the surface where unit n begins - its contact with the unit above - as an
elevation in metres over the area of the boreholes. The first unit's top is the ground; the last unit
has a top but no base.

<figure markdown="span">
  ![The top of the natural, its uncertainty and the corrections of the stack](../img/plugins/geoarchaeology/natural-top.jpg)
  <figcaption>The top of the natural (US 5): (a) band 1, the elevation; (b) the square root of band 2,
  the kriging uncertainty, smallest at the boreholes; (c) band 3, the correction made by the stack.
  Simulated boreholes on the real DEM of Tell Sultan.</figcaption>
</figure>

### Kriging variance (band 2)

The kriging variance says how uncertain the surface is at every point, in m²; its square root is a
standard deviation in metres (0.09 m² is ±0.30 m). It is smallest at the boreholes and grows between
them and beyond them. It depends on where the boreholes are and on the variogram, not on the values
found: it tells where more boreholes would help, not whether the logs are right. Contacts interpolated
by inverse distance (few boreholes) and the top of the first unit (the DEM) have none.

### Leave-one-out error

For every contact made by kriging, each borehole is left out in turn and the contact at its place is
estimated from the others; the report gives the root mean square of these differences (`list --json`
also the mean and the largest). It is the error to expect on the surface between neighbouring
boreholes. In the demonstration: 0.03 m for the base of the topsoil, which follows the ground; 0.27 m
for the top of the natural, a gentle surface; 0.9 and 1.3 m for the tops of the Iron Age and Bronze Age
deposits inside the mound, with boreholes 30-60 m apart.

The report names the model used and, when another one had a slightly smaller error, says so: *(the
Gaussian model gave 0.908 m: less than 5 % apart)*. Models within 5 % of the best are taken as equally
good and the steadiest of them is used (spherical, then exponential, without a tilted plane before with
one, Gaussian last): with few boreholes the Gaussian model, which often wins such close races,
overshoots between the boreholes. The *surfaces* folder also has a figure per contact made by kriging
(`variogram_<id>.png`): the semivariance of the pairs of boreholes against their distance, and the model
fitted. With a dozen boreholes it has few points and the model is a rough guide: judge the surface by
its leave-one-out error.

### Corrections of the stack (band 3)

Interpolated one by one, two contacts can cross or rise above the ground. The stack puts them back in
order: where two contacts cross they meet where the better known one is, and none rises above the DEM.
Band 3 keeps the correction made at every cell (positive: lowered, red on the view; negative: raised,
blue), the report the area and the largest moves (*moved by the stack on 39,412 m² (lowered up to 3.44
m, raised up to 1.60 m)*). Corrections of a few centimetres are normal. Corrections of metres over large
areas mean that the boreholes are too far apart for how quickly the units change, or that codes or order
are not consistent: look at band 3 and draw a section through that area.

### Missing units

*missing on 4,956 m² (boreholes without it: S9, S10)*: the area where the unit is absent - its top and
its base are one surface - and the boreholes that do not have it. Its thickness there is zero.

### Thicknesses and volumes

Each *Thickness of US n* is the top of unit n minus the top of the unit below, zero where n is missing.
Its volume is summed over the cells with a DEM; where the DEM has holes the ground is the surface
interpolated between the collars, and the volume there is given apart (*+ 2,787 m³ where there is no
DEM*). The last unit has no thickness. The 3D model gives the same volumes on its coarser grid (within
0.1 % in the demonstration).

<figure markdown="span">
  ![Thickness of the deposit above the natural, with contours](../img/plugins/geoarchaeology/deposit-thickness.jpg)
  <figcaption>The thickness of the deposit above the natural (DEM minus the top of US 5), with the
  volumes of the units computed by the plugin. Simulated boreholes on the real DEM of Tell Sultan.</figcaption>
</figure>

### How much to trust them

* Everything depends on how far apart the boreholes are compared with how quickly the units change. A
  contact that follows the ground, or changes slowly, is well known even with sparse boreholes; the tops
  of deposits that come and go inside a mound are not.
* Take the leave-one-out error as the error to expect between the boreholes, the kriging variance to see
  where it grows, band 3 to see where the logs disagree.
* In the demonstration the true stratigraphy is known, because it was invented: the volume of the whole
  deposit came within 1 % of the true one, the volumes of single units within about 15 % (partly
  compensating one another), and 95 % of the top of the natural within 0.28 m.
* The volume of a whole deposit is more reliable than the volumes of its units, and both more than the
  elevation of a contact at a single point.

### Limits

* **Sharp edges.** Where a unit is taken away because the nearby boreholes do not have it, it ends with a
  vertical edge halfway between a borehole that has it and one that does not: a unit that thins out
  gradually is not reconstructed.
* **An isolated borehole without a unit** takes the unit away around it, up to the edge of the surfaces,
  even where the unit may still be there: no other borehole says otherwise.
* **Wide spacing.** With boreholes 30-60 m apart, the tops of the units inside the mound of the
  demonstration were 1-2 m off (95 % of the errors).
* **A stack of layers.** Units side by side, pits and walls are not modelled as such: pits and ditches are the [implicit model](#implicit).
* **No extrapolation.** The surfaces stop at the margin around the boreholes.
* **Few boreholes.** Under five boreholes (or the number set) a contact is interpolated by inverse
  distance, without an uncertainty.

## Files written { #files }

Everything goes into the folder of the chunk, next to the project file:
`<project>.ppg.files/chunk_<n>/geoarchaeology/`. The records of the plugin are kept in the project
itself and open with it.

* `sources/`: the CSV tables imported, unchanged.
* `sondaggi.gpkg`: a GeoPackage kept up to date, with three tables: `sondaggi` (points: code, x, y,
  elevation and where it came from, elevation of the DEM, depth, elevation of the bottom, kind, units,
  number of intervals, source table), `intervalli` (no geometry: borehole, from, to, elevations of top
  and base, unit as shown and as written in the log - `unita`, `unita_scritta` -, description, colour,
  lithology) and `sezioni` (lines: name, length, distance, scale, boreholes, drawing).
* `surfaces/top_<k>_<unit>.tif`: the top of every unit, a float32 GeoTIFF with three bands: 1 the
  elevation (m), 2 the kriging variance (m²), 3 the correction by the stack (m, positive where lowered,
  negative where raised); no value outside the area of the boreholes.
* `surfaces/thickness_<unit>.tif`: the thickness of every unit but the last (m).
* `surfaces/variogram_<id>.png`: the variogram of every contact made by kriging.
* `sections/<name>.svg`, `.png`, `.dxf`, `.csv`: every section - drawing, DXF in section coordinates,
  profile.
* `logs/<borehole>.svg`, `.png`: the column of one borehole; `logs/colonne_<boreholes>.svg`, `.png`:
  every panel, named after its boreholes. The SVGs (and a PDF asked from the command line) need
  matplotlib's vector backends: where they are missing, as in the desktop application up to 1.3.0,
  sections and columns are written as PNG only, and the plugin says so (*SVG not available in this
  installation: only the PNG written*).
* `model/<unit>.ply`, `model/unita.obj` with `unita.mtl`, `model/volumi.csv`: the solids and their
  volumes; `volumi.csv` has unit, description, volume, area, mean and largest thickness, lowest and
  highest top and base, cells, clipped, down to the floor, volume and area where there is no DEM.
* `implicit/top_<k>_<unit>.tif`: the surfaces of the implicit model, float32, band 1 the elevation (m), band 2 the jackknife variance (m²), band 3 implicit minus kriging (m); `implicit/data/` the files of contacts and orientations. `model/` is then that of the implicit model, and `volumi.csv` has also `absent_cells`, `same_cells_m3`, `method`, `kriging_m3`, `range_min`, `range_max`.
* `model/unita_vista.ply`: all the units in one PLY for the program's 3D view, in the internal frame
  of the chunk (for a large model the program writes a lighter copy beside it, `model_preview.ply`).

The GeoPackage exported with *Export...* (or `export`) has the same three tables, with `<name>_<id>.tif`
for every surface and thickness and `<name>_volumi.csv` beside it.
