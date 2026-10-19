"""The GPS positions of the photos in the Reference pane: read in longitude and latitude, shown
in the CRS of the chunk with the difference from the aligned camera (as Metashape shows the
camera errors)."""
from __future__ import annotations

import os

import numpy as np
import pytest

from pyarchinit_photogrammetry.core import crs as crs_mod
from pyarchinit_photogrammetry.markers.gcp import camera_reference_errors


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def gps_cameras(processed):
    """The cameras of the processed trench with GPS positions in EPSG:4326, 1 m east, 2 m south
    and 0.5 m above where the alignment puts them; restored afterwards (the project is shared)."""
    _doc, ch, _report = processed
    saved = [(cam.reference.location, cam.reference.enabled, cam.meta.get("reference_crs")) for cam in ch.cameras]
    offset = np.array([1.0, -2.0, 0.5])
    world = np.array([ch.to_world(cam.center) for cam in ch.aligned_cameras])
    lonlat = crs_mod.transform_points(world + offset, ch.crs, "EPSG:4326")
    for cam, loc in zip(ch.aligned_cameras, lonlat):
        cam.reference.location, cam.reference.enabled = [float(v) for v in loc], True
        cam.meta["reference_crs"] = "EPSG:4326"
    try:
        yield ch, offset
    finally:
        for cam, (loc, enabled, ref_crs) in zip(ch.cameras, saved):
            cam.reference.location, cam.reference.enabled = loc, enabled
            if ref_crs is None:
                cam.meta.pop("reference_crs", None)
            else:
                cam.meta["reference_crs"] = ref_crs


def test_camera_references_in_the_chunk_crs(gps_cameras):
    ch, offset = gps_cameras
    crs_before = ch.crs
    errors = camera_reference_errors(ch)
    assert len(errors) == len(ch.aligned_cameras) and ch.crs == crs_before  # nothing of the chunk changes
    for cam in ch.aligned_cameras:
        e = errors[cam.key]
        assert np.allclose(e["reference"], ch.to_world(cam.center) + offset, atol=1e-3)
        assert np.allclose(e["delta"], -offset, atol=1e-3)  # estimated - reference
        assert e["error"] == pytest.approx(float(np.linalg.norm(offset)), abs=1e-3)


def test_camera_errors_in_the_reference_pane(gps_cameras, qapp):
    from pyarchinit_photogrammetry.gui.panels import ReferencePane

    ch, offset = gps_cameras
    pane = ReferencePane()
    pane.set_chunk(ch)
    headers = [pane.cams.horizontalHeaderItem(c).text() for c in range(pane.cams.columnCount())]
    assert headers[5:] == ["Rif. X", "Rif. Y", "Rif. Z", "Errore (m)", "Precisione X/Y/Z (mm)"]
    row = next(r for r in range(pane.cams.rowCount()) if pane.cams.item(r, 1).text() == "si")
    assert float(pane.cams.item(row, 8).text()) == pytest.approx(float(np.linalg.norm(offset)), abs=2e-3)
    world_x = float(pane.cams.item(row, 2).text())
    assert float(pane.cams.item(row, 5).text()) == pytest.approx(world_x + offset[0], abs=2e-3)  # metres, not degrees
    assert "Camere (GPS): errore 2.29 m" in pane.summary.text()
