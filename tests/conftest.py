"""Shared fixtures: one synthetic trench processed once per test session."""
from __future__ import annotations

import os
from pathlib import Path

import sqlite3

import numpy as np
import pytest

os.environ.setdefault("GLOG_minloglevel", "2")


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """A modal dialog in an offscreen test would wait forever (the CI job hangs without a
    log). Once Qt widgets are loaded, ``exec()`` returns "rejected" at once and the test
    fails with the dialog's text."""
    import sys

    qw = sys.modules.get("PySide6.QtWidgets")
    opened = []
    if qw is not None:
        def fake_exec(self, *args):
            texts = [self.windowTitle()]
            for cls in (qw.QLabel, qw.QPlainTextEdit):
                for child in self.findChildren(cls):
                    t = child.text() if cls is qw.QLabel else child.toPlainText()
                    if t:
                        texts.append(t[-1500:])
            if isinstance(self, qw.QMessageBox):
                texts += [self.text(), self.detailedText()[-1500:]]
            opened.append(" | ".join(t for t in texts if t))
            return 0

        monkeypatch.setattr(qw.QDialog, "exec", fake_exec)
        monkeypatch.setattr(qw.QMessageBox, "exec", fake_exec)
    yield
    if opened:
        pytest.fail("modal dialog opened during the test: " + "\n".join(opened), pytrace=False)


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


def load_spatialite(con) -> bool:
    """Load mod_spatialite into a sqlite3 connection from the known locations; False if none works."""
    from pyarchinit_photogrammetry.pyarchinit.database import _spatialite_paths, pin_spatialite

    con.enable_load_extension(True)
    for cand in _spatialite_paths():
        try:
            con.load_extension(cand)
            pin_spatialite(cand)
            return True
        except Exception:
            continue
    return False


def _spatialite_available():
    """mod_spatialite loadable (bare name or the known locations, after the PROJ warm-up)."""
    try:
        from pyarchinit_photogrammetry.pyarchinit.database import spatialite_path

        return spatialite_path() is not None  # keeps the library loaded (see pin_spatialite)
    except Exception:
        return False


@pytest.fixture
def pa_db(tmp_path, monkeypatch):
    """A PyArchInit 5 database (full schema, SpatiaLite) with the site 'Saggio' and US 1-3."""
    if not _spatialite_available():
        pytest.skip("mod_spatialite not available")
    from pyarchinit_photogrammetry.pyarchinit.demo_db import create_demo_database

    home = tmp_path / "pyarchinit_5"
    us_list = tuple(("1", us, "US", f"Strato {us}", "", "I", "1", "") for us in ("1", "2", "3"))
    cfg = create_demo_database(home, site="Saggio", us_list=us_list, name="scavo.sqlite", demo_content=False)
    cfg.write_text(
        "{'DATABASE': 'scavo.sqlite', 'SERVER': 'sqlite', 'HOST': '', 'USER': '', 'PASSWORD': '', 'PORT': '', "
        f"'THUMB_PATH': '{tmp_path}/media/thumb/', 'THUMB_RESIZE': '{tmp_path}/media/resize/', 'SITE_SET': ''}}")
    monkeypatch.setenv("PYARCHINIT_HOME", str(home))
    return home / "pyarchinit_DB_folder" / "scavo.sqlite"




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
