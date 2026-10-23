# GIS archive

The *GIS archive* plugin copies the survey of a project into a GIS database outside PyArchInit: a
GeoPackage file, a PostgreSQL/PostGIS database or a SpatiaLite file. Every kind of data goes into a
table of its own - the spot heights, the contour lines of the DEM, the stratigraphic units drawn, the
sections, the stones, the markers, the DEM and the orthophoto - and every row carries the project, the
chunk and the site it comes from. So one database can collect the surveys of a whole excavation,
campaign after campaign, and be opened in QGIS or in any GIS.

This page describes version 0.1.1 of the plugin, which needs pyarchinit-photogrammetry 1.1.0 or later.
Its README and source code are in the
[plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/gis-archive).

<figure markdown="span">
  ![The window of the GIS archive, set for a PostGIS database](../img/plugins/gis-archive/dialog.png)
  <figcaption>The window of <em>Plugins > GIS archive > Save into a GIS database...</em>, set for a
  PostgreSQL/PostGIS database: the server, what to save and the options.</figcaption>
</figure>

## In short

1. Draw in the program what you want to keep - spot heights, US, sections, stones - build the DEM and
   the orthophoto, and save the project.
2. *Plugins > GIS archive > Save into a GIS database...*
3. Choose the database: a GeoPackage file, a PostgreSQL/PostGIS server or a SpatiaLite file.
4. Tick what to save, set the contour interval and how the rasters are kept, then *Save*.
5. Open the database in QGIS.

## What it does

| Table | What it holds | Geometry |
|---|---|---|
| `ppg_quote` | the spot heights drawn: height, area, US, kind, unit, precision, survey, date, drawer | point |
| `ppg_curve` | the contour lines of the DEM at the interval chosen, with the index lines | line |
| `ppg_us` | the stratigraphic units drawn: site, area, US, type, the fields of `pyunitastratigrafiche`, area in m² | multipolygon |
| `ppg_sezioni` | the traces of the sections of the project: name, length, drawing | line |
| `ppg_pietre` | the stones drawn with SAM on the orthophoto: the image they were drawn on, area | polygon |
| `ppg_marker` | markers and control points: height, reference coordinates, error, control or check | point |
| `ppg_raster` | the DEM and the orthophoto: footprint, file, resolution and size of what is archived, reference system, table of the copy | polygon |
| `ppg_dem_<project>_<chunk>`, `ppg_ortofoto_<project>_<chunk>` | the copies of the DEM and of the orthophoto, when the database can hold rasters | raster |

It helps when the survey has to live in a GIS of its own: the archive of an excavation in a
GeoPackage to hand over or to open in the field, the PostGIS database of a team, the drawings of
several campaigns side by side. It does not write into the tables of PyArchInit - for those, the
program publishes US, spot heights and documentation itself ([PyArchInit](../pyarchinit.md)) - and it
is not a backup of the project: the project file stays the place where the survey is processed.

## What you need first { #before }

* **The plugin**, installed from the program: see [Plugin guides](guides.md#install). It needs nothing
  else for GeoPackage; SpatiaLite and PostgreSQL need what is listed below.
* **A project saved with a file name.** The name of the file (without `.ppg`) is the `progetto` of
  every row. Which rows belong to the project is said by an id of its own, `progetto_id`, made at its
  first archive and kept in the project: archiving the same project again replaces its rows, and
  another project never touches them - not even one with the same file name in another folder. A copy
  of the project made with *Save as* (or by copying its folder) is another project, with an id of its
  own. After the first archive the window marks the project as modified: save it, so that it keeps its
  id (if it is not saved, the plugin finds the id again from the rows written from the same project
  file).
* **A projected reference system** for the chunk (an EPSG code, such as the UTM zone of the site): the
  tables take it. A local system works too, but the tables then have no reference system and the
  summary warns about it.
* **The site of the project**, if you want it in the `sito` column: choose it in the *PyArchInit*
  pane (*Project site*). The US and spot heights drawn keep their own site, when they have one.
* **The database**:
    * *GeoPackage*: a file, created if it does not exist. Nothing to install.
    * *SpatiaLite*: a file, created if it does not exist. The program needs the SpatiaLite library
      (`mod_spatialite`), found by itself in QGIS, Homebrew or MacPorts, or named by the variable
      `SPATIALITE_LIBRARY_PATH`. SpatiaLite keeps rasters as references only.
    * *PostgreSQL / PostGIS*: an existing database on a server you can reach, a user that can create
      tables in it, and the PostGIS extension. The plugin creates the extension and the schema when they
      are missing, if the user is allowed to; otherwise ask the administrator for `CREATE EXTENSION
      postgis;` and, for raster copies, `CREATE EXTENSION postgis_raster;`. The plugin connects through
      the PostgreSQL driver the program has: psycopg2, or psycopg 3 where psycopg2 is missing (the
      Windows ARM64 application).

## The data { #data }

The plugin takes what the chunk holds: it draws nothing itself. Before archiving, draw and build
what the archive should contain, and save.

### What goes into each table { #tables }

| Table | Where it comes from in the program | Columns |
|---|---|---|
| `ppg_quote` | the spot heights placed with the *Height* tool on the orthophoto or the DEM, with the fields of `pyarchinit_quote` | `area`, `us`, `tipo`, `quota`, `unita_misura`, `precisione`, `rilievo`, `data`, `disegnatore` |
| `ppg_curve` | computed from the active DEM at the archive | `quota`, `principale` (1 for the index lines, every fifth), `equidistanza` |
| `ppg_us` | the polygons drawn with *Draw US* (not the US loaded from PyArchInit) | `area`, `us`, `tipo`, `stratigraph_index_us`, `tipo_us_s`, `rilievo_originale`, `disegnatore`, `data`, `tipo_doc`, `nome_doc`, `area_m2`, `layer` |
| `ppg_sezioni` | the traces drawn with the *Section* tool, kept with the project | `nome`, `lunghezza`, `disegno` (the file of the drawing) |
| `ppg_pietre` | the stones drawn with the SAM tool *Stones* on the orthophoto (not those drawn on the elevation of a wall) | `gruppo` (the image they were drawn on), `area_m2` |
| `ppg_marker` | the markers of the chunk with a position or reference coordinates | `nome`, `z`, `ref_x`, `ref_y`, `ref_z`, `errore_m`, `controllo` |
| `ppg_raster` | the active DEM and orthophoto | `tipo` (`dem` or `ortofoto`), `file`, `risoluzione`, `larghezza`, `altezza`, `crs`, `tabella` |

Every row also has `progetto` (the file name of the project), `chunk` (its name), `sito` and
`archiviato` (the date and time of the archive), and the columns that say whose it is: `progetto_id`
(the id of the project), `progetto_file` (the path of the project file) and `chunk_id` (the number of
the chunk in the project, which does not change when the chunk is renamed).

* **Spot heights** keep the values typed in their window; one without a position is skipped.
* **Contour lines** are drawn at the *Contour interval*; left at *suggested from the DEM*, the plugin
  takes a round interval giving at most about fifty levels over the relief of the DEM - 5 cm on a
  trench with a metre of relief, 50 cm on a tell 20 m high. The lines are simplified to half a cell of
  the DEM, and closed rings smaller than 25 cells are left out.
* **US**: only the units drawn in the program; those shown from PyArchInit's layer are not copied.
* **Sections** belong to the project (they are kept in the `sezioni` folder beside it), not to a
  chunk: they are written once per project, with `chunk` and `chunk_id` empty, and archiving any chunk
  of the project replaces them. Each is the straight line between its two ends.
* **Stones**: `gruppo` is the file of the image they were drawn on - `orthomosaic.tif` for the
  orthophoto. Stones drawn on the elevation of a wall are in the coordinates of the elevation (metres
  along the wall and height), not of the map: they are left out of the table, and the summary says how
  many and on which image (`elevation.tif`).
* **Markers** are placed at their position estimated by the survey (or at their reference coordinates
  when they have no position); `errore_m` is the distance between the two, `controllo` is 1 for a
  control point (its reference used by the georeferencing), 0 for a check point, for a marker whose
  reference is not used and for one without reference.
* **Rasters**: the active versions of the DEM and of the orthophoto. `file` is the path of the file
  on this computer; `risoluzione`, `larghezza` and `altezza` are those of what is archived: of the copy
  in the database when it is reduced, of the file otherwise.

### Archiving again { #again }

* With *Replace the rows already archived for this project and chunk* (the default), the rows of this
  project and chunk in the tables written are deleted and written again; the rows of other projects and
  chunks stay. Unticked (`--append` on the command line), the new rows are added beside the old ones.
  The rows are recognised by `progetto_id` and `chunk_id`: they stay this project's when the chunk is
  renamed or the project file is moved.
* The tables not chosen are left as they are.
* Each product replaces only its own rows: archiving only the orthophoto again replaces the row of the
  orthophoto in `ppg_raster` and leaves that of the DEM, and its copy, as they are.
* Rows written by version 0.1.0 of the plugin have no `progetto_id`: at the next archive they are taken
  as the rows of the project with the same file name (and, but for the sections, the same chunk name)
  and replaced, unless the rasters they record lie in the folder of another project file - then they
  belong to that project, stay, and the summary says so.
* The window saves the drawings of the view into the chunk before archiving. The command line reads the
  project from its file: save the project in the program first.

### Rasters { #rasters }

*Rasters*: *copied into the database*, or *reference only (file and footprint)*.

* **GeoPackage**: the orthophoto becomes a tile table of images, the DEM a tiled coverage of floating
  point values; the table names are written in capitals, as GDAL writes them
  (`PPG_DEM_SCAVO_CHUNK_1`). QGIS opens them as raster layers of the GeoPackage.
* **PostGIS**: a table `ppg_dem_<project>_<chunk>` with tiles of 256 x 256 pixels (`rid`, `progetto`,
  `chunk`, `rast`), a spatial index and the raster constraints QGIS reads. It needs the
  `postgis_raster` extension; without it the rasters stay references and the summary says so. The
  tiles are written by the plugin, so the GDAL of the server can stay disabled.
* **SpatiaLite**: references only.
* A copy is made again at every archive, from the active version, into the same table. When another
  project with the same file name already has the table `ppg_dem_<project>_<chunk>`, the copy of this
  one takes a part of its id after the name (`ppg_dem_scavo_chunk_1_3f2a9c1e`). A table of that name
  that is not a raster of the plugin is never replaced: the raster stays a reference and the summary
  says why.
* *Largest side of the copied rasters (pixels)*: 0 copies at full resolution; a value (8192, 4096...)
  reduces the copy to that longer side, averaging the pixels. An orthophoto of a few centimetres over a
  large site can weigh gigabytes: a reduced copy is enough to see it in a GIS, and the full file stays
  where `file` says.

## Step by step in the program { #program }

### 1. Save into a GIS database { #dialog }

*Plugins > GIS archive > Save into a GIS database...* archives the active chunk.

| Field | What it does |
|---|---|
| *Type* | *GeoPackage (.gpkg)*, *PostgreSQL / PostGIS* or *SpatiaLite (.sqlite)* |
| file and *Browse...* | for GeoPackage and SpatiaLite: the file, new or existing (the plugin adds its tables to it) |
| *Host*, *Port*, *Database*, *User*, *Password*, *Schema* | for PostgreSQL: the server (5432 is the usual port), the database, the user and its password, the schema of the tables (`public` by default; created if missing). The password is never saved |
| *What to save* | *Spot heights*, *Contour lines*, *Units drawn*, *Sections*, *Stones*, *Markers*, *DEM*, *Orthomosaic* |
| *Contour interval (m)* | *suggested from the DEM*, or the interval |
| *Rasters* | *copied into the database* or *reference only (file and footprint)*; SpatiaLite: references only |
| *Largest side of the copied rasters (pixels)* | *full resolution*, or the longer side of the copies |
| *Prefix of the tables* | `ppg_` by default: another prefix makes another set of tables in the same database |
| *Replace the rows already archived for this project and chunk* | ticked by default ([Archiving again](#again)) |

Everything but the password is remembered for the next time. The archive runs in the background with
the progress bar; at the end a window lists what was written:

```text
GeoPackage archive: /Users/Shared/archivio/scavi.gpkg
  ppg_quote: 2 rows
  ppg_us: 1 rows
  ppg_pietre: 2 rows
  ppg_sezioni: 1 rows
  ppg_marker: 1 rows
  ppg_curve: 30 rows
  ppg_raster: 2 rows
  dem: table PPG_DEM_SCAVO_CHUNK_1
  ortofoto: table PPG_ORTOFOTO_SCAVO_CHUNK_1
```

Notes follow the list when something went otherwise - a local reference system, a raster kept as a
reference and why, stones of a wall elevation left out, rows of version 0.1.0 belonging to another
project kept.

### 2. Everything into a GeoPackage { #export }

*File > Export > GIS archive (GeoPackage)...* writes everything of the active chunk into a GeoPackage
in one step: all the tables, the contour interval suggested from the DEM, the rasters copied at full
resolution, the rows of the project replaced.

### 3. In QGIS { #qgis }

* **GeoPackage**: drag the file into QGIS, or *Layer > Add Layer > Add Vector Layer...*; the tables and
  the raster copies appear in the list of its layers.
* **PostGIS**: create a connection in the *Browser* panel (*PostgreSQL > New Connection...*) with the
  same server, database and user; the tables are in the schema chosen.
* **SpatiaLite**: *SpatiaLite > New Connection...* in the *Browser* panel.
* To show one project or one chunk, filter the layer (right click, *Filter...*):
  `"progetto" = 'scavo' AND "chunk" = 'Chunk 1'`; two projects with the same file name are told apart by
  `"progetto_file"` (or `"progetto_id"`).
* The rasters kept as references: open the file named in the `file` column of `ppg_raster`.

## From the command line { #cli }

`ppg plugin gis-archive PROJECT DATABASE [options]`: the project first, then the database - a file
ending in `.gpkg`, a SpatiaLite file (`.sqlite`, `.db`, or any name after `spatialite:`) or a
PostgreSQL address. The project is read from its file; the first time, the command writes the archive
id of the project into it (`gis_archive` in its metadata), and nothing else.

```bash
ppg plugin gis-archive --help
ppg plugin gis-archive scavo.ppg archivio.gpkg
ppg plugin gis-archive scavo.ppg archivio.gpkg --what quote,us,sezioni --chunk "Chunk 2"
ppg plugin gis-archive scavo.ppg spatialite:archivio.sqlite --what quote,curve,us --interval 0.5
PGPASSWORD=... ppg plugin gis-archive scavo.ppg "postgresql://postgres@localhost:5432/scavi?schema=tell" \
    --interval 0.5 --rasters copy --max-side 8192
ppg plugin gis-archive scavo.ppg archivio.gpkg --what dem,ortofoto --rasters reference
```

| Option | What it does |
|---|---|
| `--what` | what to save, separated by commas, among `quote`, `curve`, `us`, `sezioni`, `pietre`, `marker`, `dem`, `ortofoto` (all by default) |
| `--chunk` | the name of the chunk (the active one by default) |
| `--interval` | the contour interval in metres (suggested from the DEM by default) |
| `--prefix` | the prefix of the tables (`ppg_`) |
| `--append` | add the rows instead of replacing those of this project and chunk |
| `--rasters` | `copy` (the default) or `reference` |
| `--max-side` | the largest side of the raster copies in pixels (0: full resolution) |

A PostgreSQL address is written `postgresql://USER@HOST:PORT/DATABASE?schema=SCHEMA`; the driver is the
one the program has (psycopg2, or psycopg 3), and `postgresql+psycopg://...` or
`postgresql+psycopg2://...` chooses one. The password goes
in the variable `PGPASSWORD` (or in the address, `USER:PASSWORD@HOST`, which stays in the history of
the terminal). The command prints the same summary as the window, in the language of the program, and
`error: ...` when the database refuses.

## Reading the results { #reading }

* **Which rows are which.** `progetto`, `chunk` and `sito` tell where every row comes from;
  `archiviato` when it was written; `progetto_id` and `progetto_file` which project exactly, also when
  two have the same file name. All the rows of one archive have the same `archiviato`.
* **Heights.** The geometries are two-dimensional: the heights are columns - `quota` of the spot heights
  and of the contour lines, `z` of the markers.
* **Markers.** `errore_m` is the three-dimensional distance between where the survey puts the marker and
  its reference coordinates: the error of a control point after the georeferencing, or of a check
  point.
* **Contour lines.** `principale` = 1 marks the index lines (every fifth); `equidistanza` repeats the
  interval used, so archives made with different intervals can be told apart.
* **Rasters.** `tabella` names the copy in the database, empty for a reference; the polygon is the
  footprint of the raster.

### Limits { #limits }

* **One chunk at a time.** The window archives the active chunk; on the command line, `--chunk`.
* **Not PyArchInit.** No US forms, relations or documentation: the tables are a GIS copy of the drawings.
* **Two dimensions.** Polygons, lines and points have no height in their geometry.
* **Sections** are straight lines between their two ends.
* **Stones on an elevation** are not archived: they are in the coordinates of the elevation, not of
  the map.
* **SpatiaLite** holds no rasters.

## Files written { #files }

The plugin writes into the database chosen, and into the project only its archive id: nothing in the
folder of the project. A GeoPackage or
SpatiaLite file is created when it does not exist; a file that exists and is not a database of the kind
chosen is left alone and the archive stops (*... exists and is not a GeoPackage*, *... is a GeoPackage:
choose the GeoPackage type*). The choices of the window are kept in the settings of the plugin, the
password never.
