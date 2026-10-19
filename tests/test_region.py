"""The reconstruction region: depth maps, dense cloud, mesh (and so DEM and orthomosaic) inside it."""
from __future__ import annotations

import shutil

import numpy as np
import pytest

from pyarchinit_photogrammetry.engine import region as R

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


def test_box(rng):
    r = R.Region([1.0, 2.0, 3.0], [4.0, 2.0, 1.0], R.rotation_z(30))
    pts = rng.uniform(-4, 6, (2000, 3))
    loc = (pts - [1, 2, 3]) @ R.rotation_z(30)
    truth = (np.abs(loc) <= [2, 1, 0.5]).all(axis=1)
    assert np.array_equal(r.contains(pts), truth) and truth.any()
    c = r.corners()
    assert c.shape == (8, 3) and np.allclose(c.mean(axis=0), [1, 2, 3])
    size = {1: 4.0, 2: 2.0, 4: 1.0}  # an edge changes one bit of the corner index: one axis
    assert all(np.isclose(np.linalg.norm(c[a] - c[b]), size[a ^ b]) for a, b in R.Region.EDGES)
    back = R.Region.from_dict(r.to_dict())
    assert np.allclose(back.corners(), r.corners())
    x0, y0, x1, y1 = r.footprint()
    assert x0 < 1 < x1 and y0 < 2 < y1


def test_trench_only(processed, tmp_path):
    """A region on the excavated area: depth maps, dense cloud and mesh stay inside it."""
    from pyarchinit_photogrammetry.engine import dense

    doc, ch = _copy(processed, tmp_path)
    full = ch.point_cloud
    r = ch.reset_region()
    assert ch.region is not None and r.contains(ch.to_world(ch.tie_points.xyz)).mean() > 0.95
    lo, hi = ORIGIN[:2] + [1.0, 0.9], ORIGIN[:2] + [3.0, 2.1]
    R.region_from_box(ch, (*lo, *hi))
    region = ch.region
    assert region.size[2] > 0.3  # from the trench floor to the ground
    ch.build_depth_maps(quality="medium", backend="cpu")
    ws = ch.data_dir / "dense"
    rec = dense._read_workspace_model(ws)
    maps = ws / "stereo" / "depth_maps"
    image = next(i for i in rec.images.values() if (maps / f"{i.name}.geometric.bin").exists())
    depth = dense.read_colmap_array(maps / f"{image.name}.geometric.bin")
    inside = R.depth_mask(ch, depth, dense._K(rec.cameras[image.camera_id]), dense._pose(image), region)
    assert (depth > 0).sum() > 100 and not (depth[~inside] > 0).any()
    pc = ch.build_point_cloud()
    w = ch.to_world(pc.xyz)
    assert region.contains(w).all() and len(pc) < len(full)
    assert w[:, 0].min() > lo[0] - 1e-6 and w[:, 0].max() < hi[0] + 1e-6
    mesh = ch.build_model()
    centres = ch.to_world(np.asarray(mesh.triangles_center))
    assert region.contains(centres).all()
    ch.region = None
    assert ch.region is None and "region" not in ch.meta


def test_interface_region(processed, tmp_path, monkeypatch):
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
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    w.region_reset()
    assert w.chunk.region is not None and w.model_view.canvas.sd.region is not None
    before = w.model_view.canvas.grab_array()
    w.model_view.cb_region.setChecked(False)
    app.processEvents()
    assert not np.array_equal(before, w.model_view.canvas.grab_array())  # the yellow box is drawn
    w.model_view.cb_region.setChecked(True)
    w.region_rotate_to_view()
    assert w.chunk.region is not None
    c = w.chunk.region.center
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {
        "cx": c[0], "cy": c[1], "cz": c[2], "sx": 1.0, "sy": 2.0, "sz": 3.0, "angle": 45.0}))
    w.region_edit()
    assert np.allclose(w.chunk.region.size, [1, 2, 3])
    assert np.isclose(np.degrees(np.arctan2(w.chunk.region.rotation[1, 0], w.chunk.region.rotation[0, 0])), 45)
    w.region_clear()
    assert w.chunk.region is None
    w.setWindowModified(False)
    w.close()
    app.processEvents()
