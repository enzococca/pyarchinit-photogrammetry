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
    foreign = tmp_path / "proj"
    foreign.mkdir()
    (foreign / "proj.db").write_bytes(b"not a database")
    monkeypatch.setenv("PROJ_DATA", str(foreign))
    monkeypatch.setenv("PROJ_LIB", str(foreign))
    from pyarchinit_photogrammetry._env import use_bundled_proj_data

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
