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


from pyarchinit_photogrammetry.pyarchinit.demo_db import SCHEMA  # noqa: E402



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




# ---------------------------------------------------------------- exit guard
# After the GUI tests, interpreter finalisation can crash in native destructors (Qt, VTK,
# Open3D, SpatiaLite loaded in one process: "Segmentation fault ... <no Python frame>")
# although every test passed (seen on macOS and on the Linux CI runner). Leave through
# os._exit once pytest has printed its summary.
_exit_status = {"code": 0}


def pytest_sessionfinish(session, exitstatus):
    _exit_status["code"] = int(exitstatus)


def pytest_unconfigure(config):
    import os
    import sys

    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
    except Exception:
        app = None
    if app is None:
        return
    app.processEvents()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(_exit_status["code"])
