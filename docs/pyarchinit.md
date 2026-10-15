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
