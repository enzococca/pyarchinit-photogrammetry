# PyArchInit integration

The bridge (`pyarchinit_photogrammetry.pyarchinit`) works without QGIS. It mirrors the
conventions of PyArchInit 5 (reference: branch `Stratigraph_00001`).

## Connection

| Source | How |
| --- | --- |
| PyArchInit settings | `PyArchInit.connect()` reads `$PYARCHINIT_HOME/pyarchinit_DB_folder/config.cfg` (`PYARCHINIT_HOME` from the environment, else `~/pyarchinit_5` or `~/pyarchinit`) |
| Explicit database | `PyArchInit.connect("postgresql://user:pwd@host:5433/pyarchinit")` or `PyArchInit.connect("/path/db.sqlite")`; thumbnail folders are still taken from `config.cfg` when present |

`config.cfg` is parsed with `ast.literal_eval` (it is a Python dict literal). SQLite paths
in `DATABASE` are relative to `pyarchinit_DB_folder`, as in PyArchInit.

For SQLite, `mod_spatialite` is needed only to write geometries. The library initialises
the PROJ library bundled with rasterio/pyproj before loading SpatiaLite: loading a system
SpatiaLite first can crash GDAL-based code in the same process.

## Reading

| Method | Table |
| --- | --- |
| `sites()` | `site_table` |
| `stratigraphic_units(sito, area=None, us=None)` | `us_table` |
| `us_id(sito, area, us)` | `us_table.id_us` |
| `us_records(sito)` | US -> attributes (used for IFC property sets) |
| `finds(sito)`, `pottery(sito)`, `documentation(sito)` | `inventario_materiali_table`, `pottery_table`, `documentazione_table` |
| `media_for(entity_type, id_entity)` | `media_to_entity_table` + `media_table` |
| `us_polygons(sito, area=None)` | `pyunitastratigrafiche` as `UnitPolygon` (shapely) |

## Writing

| Method | What is written |
| --- | --- |
| `publish_orthomosaic(chunk, sito, nome_doc, link_us=[(area, us)])` | GeoTIFF copied to `<media>/fotogrammetria/<sito>/`, `media_table` (`image`, `tif`), thumbnails, `media_to_entity_table` (`US`, `us_table`), `documentazione_table` (`Fotopiano`, `Fotogrammetria`, GSD and CRS in `note`), extent in `pyarchinit_documentazione` |
| `publish_dem(...)` | as above with `tipo_documentazione = "DEM"` |
| `publish_model(...)` | textured OBJ in world coordinates, `mediatype = "3d_model"`, `{id}_{name}_3d_thumb.png`, model copied to the resize folder, `Modello tridimensionale` |
| `publish_units(units, sito, area, crs)` | `pyunitastratigrafiche` (MULTIPOLYGON) |
| `publish_elevations(chunk, sito, area, us, points)` | `pyarchinit_quote`, heights sampled on the DEM |
| `publish_section_trace(section, sito, id_sezione)` | `pyarchinit_sezioni` |
| `publish_volume(sito, nome_calcolo, result)` | `computo_metrico_table` |
| `create_us(sito, area, us, unita_tipo, **fields)` | a new `us_table` record (`id_us` = max+1, `entity_uuid`, `node_uuid`); the site record is created when missing; `FileExistsError` if sito/area/US/type exists. Unknown fields are ignored |
| `update_us(id_us, **fields)` | the given columns of a `us_table` record |
| `upgrade_us_table()` | adds the PyArchInit 5 columns that `us_table` lacks (`missing_us_columns()`), as PyArchInit's own updater does: a copy `<file>.backup_<date>_<time>` of a SQLite file, then `ALTER TABLE ... ADD COLUMN`; records are untouched. Column names and types come from the bundled schema (`reference_us_columns()`) |
| `update_unit(gid, geometry=None, crs=None, **fields)` | attributes and/or geometry of a `pyunitastratigrafiche` polygon (geometry reprojected from `crs` to the layer SRID) |
| `delete_unit(gid)` | removes a `pyunitastratigrafiche` polygon (the US form is not touched) |

`us_polygons(sito, crs=...)` returns the polygons in the given CRS (the chunk's), reprojected
from the layer SRID when they differ.

`rapporti` is stored as PyArchInit does (`"[['Copre', '2', '1', 'Sito'], ...]"`);
`parse_relationships` reads it (also the short `[rel, us]` form and plain text such as
`copre 3; tagliato da 7`) and `format_relationships` writes it. `RELATIONS` lists the
relationship names and `INVERSE` their inverse (*Copre* / *Coperto da*...).

Conventions:

* ids are `max + 1` as in PyArchInit; on PostgreSQL, columns backed by a sequence
  (`gid` of the spatial layers) take `nextval` so QGIS editing keeps working;
* `entity_uuid` (UUID v4) is filled when the column exists;
* thumbnails: `THUMB_PATH/{id}_{name}_thumb.png` (max 150 px, 100 dpi), resized copy
  `THUMB_RESIZE/{id}_{name}.png` (max 2008 x 1417, 300 dpi); raster previews are rendered
  with rasterio, so large or float GeoTIFFs are fine;
* geometries are reprojected with pyproj from the chunk CRS to the SRID declared in
  `geometry_columns`;
* publishing the same file twice raises `FileExistsError`; `overwrite=True` refreshes the
  file and previews and keeps the same `id_media`;
* media registration is a single transaction: if a preview fails nothing is left behind.


## Connections, sites and the demo database (0.3)

* `PyArchInit.connect()` reads `~/pyarchinit_5/pyarchinit_DB_folder/config.cfg` (PyArchInit 5) or
  `~/pyarchinit/...`, or `PYARCHINIT_HOME`. `connect(url="path.sqlite")`, `connect(url="postgresql://user:pw@host:5432/db?sslmode=allow")`
  and `connect(config_path=...)` are the other ways; `media_root=` sends media, thumbnails and
  resized copies to a folder of your choice instead of the PyArchInit ones (used by the tests).
* A site must exist in `site_table` for PyArchInit to show its records: `ensure_site(sito, nazione=..., comune=...)`
  creates the *scheda Sito* when missing, and every `publish_*` method calls it.
* `pyarchinit.demo_db.create_demo_database(home, size="large")` builds `demo.sqlite` with the
  table definitions of PyArchInit 5 (`pyarchinit5_schema.sql`, 60 tables, 21 geometry layers
  registered in EPSG:3004 when `mod_spatialite` is available): three sites, 73 units, polygons,
  spot heights, a section trace, pottery and finds for the demo site. Existing files are kept
  (`overwrite=True` to rebuild) and `config.cfg` is written only when absent, so the interface
  can create the demo inside a real PyArchInit installation without changing its settings.
* The bridge was exercised against real PyArchInit 5 databases (87 tables, 11 sites, 4820 unit
  polygons, 2779 media): reading sites, units, polygons, documentation, finds and media, and
  writing sites, unit polygons and documentation records.
