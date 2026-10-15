"""A small PyArchInit-compatible SpatiaLite database for demos and tests.

It contains the subset of the PyArchInit 5 schema that the bridge writes to
(same table and column names), one site and a few stratigraphic units.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE site_table (id_sito INTEGER PRIMARY KEY, sito TEXT UNIQUE, nazione TEXT, regione TEXT, comune TEXT,
  descrizione TEXT, provincia TEXT, definizione_sito TEXT, find_check INTEGER, sito_path TEXT, entity_uuid TEXT);
CREATE TABLE us_table (id_us INTEGER PRIMARY KEY, sito TEXT, area TEXT, us TEXT, d_stratigrafica TEXT,
  d_interpretativa TEXT, descrizione TEXT, interpretazione TEXT, periodo_iniziale TEXT, fase_iniziale TEXT,
  periodo_finale TEXT, fase_finale TEXT, unita_tipo TEXT, entity_uuid TEXT);
CREATE TABLE media_table (id_media INTEGER PRIMARY KEY, mediatype TEXT, filename TEXT, filetype VARCHAR(10),
  filepath TEXT UNIQUE, descrizione TEXT, tags TEXT, entity_uuid TEXT);
CREATE TABLE media_thumb_table (id_media_thumb INTEGER PRIMARY KEY, id_media INTEGER, mediatype TEXT,
  media_filename TEXT, media_thumb_filename TEXT UNIQUE, filetype VARCHAR(10), filepath TEXT, path_resize TEXT,
  entity_uuid TEXT);
CREATE TABLE media_to_entity_table (id_mediaToEntity INTEGER PRIMARY KEY, id_entity INTEGER, entity_type TEXT,
  table_name TEXT, id_media INTEGER, filepath TEXT, media_name TEXT, entity_uuid TEXT);
CREATE TABLE documentazione_table (id_documentazione INTEGER PRIMARY KEY, sito TEXT, nome_doc TEXT, data TEXT,
  tipo_documentazione TEXT, sorgente TEXT, scala TEXT, disegnatore TEXT, note TEXT, entity_uuid TEXT);
CREATE TABLE computo_metrico_table (id_computo INTEGER PRIMARY KEY, sito TEXT, nome_calcolo TEXT, tipo_calcolo TEXT,
  dem_pre TEXT, dem_post TEXT, layer_poligono TEXT, area_mq REAL, volume_mc REAL, quota_min REAL, quota_max REAL,
  data_calcolo TEXT, fase_scavo TEXT, note TEXT, entity_uuid TEXT);
CREATE TABLE pyunitastratigrafiche (gid INTEGER PRIMARY KEY, area_s TEXT, scavo_s TEXT, us_s TEXT,
  stratigraph_index_us INTEGER, tipo_us_s TEXT, rilievo_originale TEXT, disegnatore TEXT, data DATE, tipo_doc TEXT,
  nome_doc TEXT, coord TEXT, unita_tipo_s TEXT);
CREATE TABLE pyarchinit_documentazione (gid INTEGER PRIMARY KEY, sito TEXT, nome_doc TEXT, tipo_doc TEXT,
  path_qgis_pj TEXT);
CREATE TABLE pyarchinit_quote (gid INTEGER PRIMARY KEY, sito_q TEXT, area_q TEXT, us_q TEXT, unita_misu_q TEXT,
  quota_q REAL, data TEXT, disegnatore TEXT, rilievo_originale TEXT, unita_tipo_q TEXT);
CREATE TABLE pyarchinit_sezioni (gid INTEGER PRIMARY KEY, id_sezione TEXT, sito TEXT, area INTEGER, descr TEXT,
  tipo_doc TEXT, nome_doc TEXT);
"""

SPATIAL_LAYERS = (("pyunitastratigrafiche", "MULTIPOLYGON"), ("pyarchinit_documentazione", "LINESTRING"),
                  ("pyarchinit_quote", "POINT"), ("pyarchinit_sezioni", "LINESTRING"))


def create_demo_database(home: str | Path, srid: int = 3004, site: str = "Saggio dimostrativo",
                         us_list=(("1", "1", "Strato di crollo"), ("1", "2", "Muro in pietre e malta"),
                                  ("1", "3", "Piano di calpestio"))) -> Path:
    """Create ``<home>/pyarchinit_DB_folder/{demo.sqlite, config.cfg}`` and the media folders.

    Set ``PYARCHINIT_HOME=<home>`` (or pass the config path) to use it with :class:`PyArchInit`.
    """
    from .database import _warm_up_proj

    home = Path(home)
    folder = home / "pyarchinit_DB_folder"
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "demo.sqlite"
    if db.exists():
        db.unlink()
    _warm_up_proj()
    con = sqlite3.connect(db)
    spatial = True
    try:
        con.enable_load_extension(True)
        from .database import _spatialite_paths

        for p in _spatialite_paths():
            try:
                con.load_extension(p)
                break
            except Exception:
                continue
        else:
            spatial = False
        if spatial:
            con.execute("SELECT InitSpatialMetadata(1)")
    except Exception:
        spatial = False
    con.executescript(SCHEMA)
    if spatial:
        for table, gtype in SPATIAL_LAYERS:
            con.execute(f"SELECT AddGeometryColumn('{table}', 'the_geom', {int(srid)}, '{gtype}', 'XY')")
    con.execute("INSERT INTO site_table (id_sito, sito, nazione, regione, comune) VALUES (1, ?, 'Italia', 'Campania', 'Baselice')",
                (site,))
    for i, (area, us, desc) in enumerate(us_list, 1):
        con.execute("INSERT INTO us_table (id_us, sito, area, us, unita_tipo, d_stratigrafica) VALUES (?,?,?,?,?,?)",
                    (i, site, area, us, "USM" if "Muro" in desc else "US", desc))
    con.commit()
    con.close()
    media = home / "media"
    (folder / "config.cfg").write_text(
        "{'DATABASE': 'demo.sqlite', 'SERVER': 'sqlite', 'HOST': '', 'USER': '', 'PASSWORD': '', 'PORT': '', "
        f"'THUMB_PATH': '{(media / 'thumb').as_posix()}/', 'THUMB_RESIZE': '{(media / 'resize').as_posix()}/', "
        "'EXPERIMENTAL': 'No', 'SITE_SET': '', 'LOGO': ''}")
    return folder / "config.cfg"
