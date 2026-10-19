"""Elevations: orthophoto and relief of the model on a plane (a wall) or a cylinder (a well)."""
from __future__ import annotations

import shutil

import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.engine import elevation as E

ORIGIN = np.array([2480000.0, 4500000.0, 100.0])  # the synthetic trench (testing.synthetic.make_scene)


def _copy(processed, tmp_path, name="copia"):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / name
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def test_frames_map_cells_to_the_world_and_back(rng):
    fr = E.Plane([10.0, 5.0, 100.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0])
    assert np.allclose(fr.normal, [0, -1, 0])
    uvd = rng.uniform(-3, 3, (50, 3))
    assert np.allclose(fr.coords(fr.point(uvd[:, 0], uvd[:, 1], uvd[:, 2])), uvd)
    for inside in (True, False):
        cy = E.Cylinder([1.0, 2.0, 0.0], [0, 0, 1], 2.5, inside=inside, start=[1, 1, 0])
        uvd = np.column_stack([rng.uniform(-7, 7, 50), rng.uniform(-1, 1, 50), rng.uniform(-0.5, 0.5, 50)])
        assert np.allclose(cy.coords(cy.point(uvd[:, 0], uvd[:, 1], uvd[:, 2])), uvd)
        p0 = cy.point(np.zeros(1), np.zeros(1), np.zeros(1))[0]
        p1 = cy.point(np.zeros(1), np.zeros(1), np.full(1, 0.1))[0]
        radial = np.linalg.norm(p1[:2] - [1, 2]) - np.linalg.norm(p0[:2] - [1, 2])
        assert np.isclose(radial, -0.1 if inside else 0.1)  # d is towards the viewer
        # u runs to the right of the viewer: up x towards the viewer
        right = np.cross([0, 0, 1], cy.view(np.zeros(1), np.zeros(1))[0])
        step = cy.point(np.full(1, 0.01), np.zeros(1), np.zeros(1))[0] - p0
        assert step @ right > 0
    circle = [[1 + 2 * np.cos(a), 2 + 2 * np.sin(a)] for a in np.linspace(0, 5, 7)]
    cy = E.cylinder_from_circle(None, circle)
    assert np.allclose(cy.centre[:2], [1, 2]) and np.isclose(cy.radius, 2.0)
    assert E.frame_from_dict(cy.to_dict()).to_dict() == cy.to_dict()
    pts = [[0, 0, 0], [2, 0, 0], [0, 0, 1.5], [2, 0.01, 1.4]]
    pl = E.plane_from_points(pts, toward=[1, 5, 0])
    assert pl.normal[1] > 0.99 and abs(pl.v_axis[2]) > 0.99


def test_elevation_of_a_trench_wall(processed, tmp_path):
    """The south face of the trench (y = 0.9 m, from 0.35 m below the ground to the ground) seen
    from inside the trench: the relief is about zero on the face, the orthophoto covers it."""
    doc, ch = _copy(processed, tmp_path)
    a = ORIGIN[:2] + [1.05, 0.9]
    b = ORIGIN[:2] + [2.95, 0.9]
    frame = E.plane_from_line(ch, a, b, side=ORIGIN[:2] + [2.0, 1.5])
    assert frame.normal[1] > 0.99  # looks south from inside the trench
    entry = E.build_elevation(ch, frame, resolution=0.004, depth=(-0.08, 0.08), max_angle=89.0)
    assert entry in ch.meta["elevations"] and entry["name"] == "Prospetto"
    ortho, relief = E.elevation_paths(ch, entry)
    with rasterio.open(relief) as src:
        d = src.read(1)
        t = src.transform
        assert src.tags()["ppg_frame"]
    rows, cols = np.nonzero(np.isfinite(d))
    v = t.f - (rows + 0.5) * abs(t.e)  # height of the cells
    face = (v > ORIGIN[2] - 0.30) & (v < ORIGIN[2] - 0.05)
    assert face.sum() > 2000
    assert np.median(np.abs(d[rows[face], cols[face]])) < 0.01
    with rasterio.open(ortho) as src:
        alpha = src.read(4)
    assert (alpha[rows[face], cols[face]] > 0).mean() > 0.5
    # a well: the trench seen from a vertical cylinder at its centre, from inside
    ring = [ORIGIN[:2] + [2 + 0.5 * np.cos(t), 1.5 + 0.5 * np.sin(t)] for t in np.linspace(0, 6, 8)]
    well = E.cylinder_from_circle(ch, ring, inside=True)
    e2 = E.build_elevation(ch, well, resolution=0.01, depth=(-2.0, 0.4), max_angle=89.0)
    with rasterio.open(E.elevation_paths(ch, e2)[1]) as src:
        assert np.isfinite(src.read(1)).mean() > 0.05
    assert len(E.elevations(ch)) == 2 and E.remove_elevation(ch, e2["id"]) and len(E.elevations(ch)) == 1


def test_interface_elevation_from_a_section(processed, tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, ch = _copy(processed, tmp_path)
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    section = {"label": "S1", "p1": list(ORIGIN[:2] + [1.05, 0.9]), "p2": list(ORIGIN[:2] + [2.95, 0.9])}
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {
        "side": "left", "front": 0.08, "back": 0.08, "resolution": 0.008, "blending": "mosaic", "colour": False,
        "name": "Parete sud"}))
    w.build_elevation(section)
    w.runner.wait()
    (entry,) = w.chunk.meta["elevations"]
    assert entry["name"] == "Parete sud" and w._raster_version == ("elevation", entry["id"])
    assert w.ortho_view.kind == "ortho"
    items = w.workspace.findItems("Prospetti", mw.Qt.MatchFlag.MatchStartsWith | mw.Qt.MatchFlag.MatchRecursive)
    assert items and items[0].childCount() == 1
    w.setWindowModified(False)
    w.close()
    app.processEvents()
