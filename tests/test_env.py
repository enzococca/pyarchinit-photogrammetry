"""PROJ data: EPSG lookups work in worker threads even when Anaconda/QGIS export PROJ_DATA."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest


def test_epsg_lookup_in_worker_thread_with_foreign_proj_data(monkeypatch, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    bundled = Path(rasterio.__file__).parent / "proj_data"
    if not (bundled / "proj.db").exists():
        pytest.skip("rasterio without bundled PROJ data")
    # imported first: importing the package already redirects a foreign PROJ_DATA
    from pyarchinit_photogrammetry._env import use_bundled_proj_data

    foreign = tmp_path / "proj"
    foreign.mkdir()
    (foreign / "proj.db").write_bytes(b"not a database")
    monkeypatch.setenv("PROJ_DATA", str(foreign))
    monkeypatch.setenv("PROJ_LIB", str(foreign))
    assert use_bundled_proj_data() == str(bundled)
    assert os.environ["PROJ_DATA"] == str(bundled) and os.environ["PROJ_LIB"] == str(bundled)
    from rasterio.crs import CRS

    results = {}

    def worker():
        try:
            results["wkt"] = CRS.from_epsg(3004).to_wkt()
        except Exception as exc:  # pragma: no cover - the failure we guard against
            results["error"] = str(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert "error" not in results, results
    assert "Monte Mario" in results["wkt"]


_FORK_SCRIPT = r'''
import sqlite3, subprocess, sys, threading
from pyarchinit_photogrammetry.pyarchinit import database

path = database.spatialite_path()
if path is None:
    print("NO_SPATIALITE")
    sys.exit(0)
for _ in range(3):  # connections opened and closed, as the connection pool of a database does
    con = sqlite3.connect(":memory:")
    con.enable_load_extension(True)
    con.load_extension(path)
    database.pin_spatialite(path)
    con.execute("SELECT InitSpatialMetadata(1)")
    con.close()
ok = subprocess.run([sys.executable, "-c", "print('child ok')"], capture_output=True, text=True, close_fds=False)
assert ok.stdout.strip() == "child ok", ok
# a plain fork (third-party code): on macOS the child may die in PROJ's atfork handler, but the
# parent must neither crash nor hang
th = threading.Thread(target=lambda: subprocess.run(["true"], capture_output=True))
th.start()
th.join(60)
assert not th.is_alive(), "fork hangs"
try:
    import sklearn  # noqa: F401
except ImportError:
    pass
else:  # joblib's thread pools need multiprocessing's resource tracker, started before SpatiaLite
    import numpy as np
    from pyarchinit_photogrammetry.ai import learn

    xyz = np.random.default_rng(0).random((3000, 3))
    model = learn.train(xyz, np.where(xyz[:, 2] > 0.5, 2, 6), n_estimators=8, ground=False, n_jobs=2)
    assert list(model.classes) == [2, 6]
print("FORK_OK")
'''


def test_fork_after_spatialite(tmp_path):
    """SQLite unloads mod_spatialite when a connection closes, leaving PROJ's atfork handlers
    pointing into the unloaded library: the next fork() crashed the application or hung a
    task (training the classifier after connecting to PyArchInit). Run in a child process."""
    import subprocess
    import sys

    script = tmp_path / "fork_check.py"
    script.write_text(_FORK_SCRIPT)
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=300, close_fds=False)
    if "NO_SPATIALITE" in r.stdout:
        pytest.skip("mod_spatialite not found")
    assert r.returncode == 0 and "FORK_OK" in r.stdout, (r.returncode, r.stdout[-2000:], r.stderr[-3000:])
    assert "resource_tracker: process died" not in r.stderr
