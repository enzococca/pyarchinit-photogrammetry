"""Shared fixtures: one synthetic trench processed once per test session."""
from __future__ import annotations

import os
from pathlib import Path

import sqlite3

import numpy as np
import pytest

os.environ.setdefault("GLOG_minloglevel", "2")


@pytest.fixture(scope="session")
def scene_dir(tmp_path_factory) -> Path:
    from pyarchinit_photogrammetry.testing.synthetic import write_dataset

    d = tmp_path_factory.mktemp("trench")
    write_dataset(d)
    return d


@pytest.fixture(scope="session")
def scene():
    from pyarchinit_photogrammetry.testing.synthetic import make_scene

    return make_scene()


@pytest.fixture(scope="session")
def processed(scene_dir):
    """Project with alignment, georeferencing, dense cloud, mesh, texture, DEM and orthomosaic."""
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    ch = doc.add_chunk("Saggio 1")
    doc.save(scene_dir / "project.ppg")
    ch.add_photos([scene_dir / "photos"])
    ch.detect_markers("aruco_4x4_50")
    ch.match_photos(quality="high")
    ch.align_cameras()
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    report = ch.update_transform()
    ch.build_depth_maps(quality="high")
    ch.build_point_cloud()
    ch.build_model()
    ch.build_texture(texture_size=2048)
    ch.build_dem()
    ch.build_orthomosaic()
    doc.save()
    return doc, ch, report


@pytest.fixture
def rng():
    return np.random.default_rng(0)


def _spatialite_available():
    try:
        import rasterio.crs  # PROJ first (see database._warm_up_proj)

        rasterio.crs.CRS.from_epsg(4326).to_wkt()
        con = sqlite3.connect(":memory:")
        con.enable_load_extension(True)
        con.load_extension("mod_spatialite")
        return True
    except Exception:
        return False


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


@pytest.fixture
def pa_db(tmp_path, monkeypatch):
    if not _spatialite_available():
        pytest.skip("mod_spatialite not available")
    home = tmp_path / "pyarchinit_5"
    (home / "pyarchinit_DB_folder").mkdir(parents=True)
    db = home / "pyarchinit_DB_folder" / "scavo.sqlite"
    con = sqlite3.connect(db)
    con.enable_load_extension(True)
    con.load_extension("mod_spatialite")
    con.execute("SELECT InitSpatialMetadata(1)")
    con.executescript(SCHEMA)
    for table, gtype in (("pyunitastratigrafiche", "MULTIPOLYGON"), ("pyarchinit_documentazione", "LINESTRING"),
                         ("pyarchinit_quote", "POINT"), ("pyarchinit_sezioni", "LINESTRING")):
        con.execute(f"SELECT AddGeometryColumn('{table}', 'the_geom', 3004, '{gtype}', 'XY')")
    con.execute("INSERT INTO site_table (id_sito, sito, comune) VALUES (1, 'Saggio', 'Baselice')")
    for i, us in enumerate(("1", "2", "3"), 1):
        con.execute("INSERT INTO us_table (id_us, sito, area, us, unita_tipo, d_stratigrafica) VALUES (?,?,?,?,?,?)",
                    (i, "Saggio", "1", us, "US", f"Strato {us}"))
    con.commit()
    con.close()
    (home / "pyarchinit_DB_folder" / "config.cfg").write_text(
        "{'DATABASE': 'scavo.sqlite', 'SERVER': 'sqlite', 'HOST': '', 'USER': '', 'PASSWORD': '', 'PORT': '', "
        f"'THUMB_PATH': '{tmp_path}/media/thumb/', 'THUMB_RESIZE': '{tmp_path}/media/resize/', 'SITE_SET': ''}}")
    monkeypatch.setenv("PYARCHINIT_HOME", str(home))
    return db


