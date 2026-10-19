"""Precision of cameras, tie points, markers and DEM from the covariance of the bundle adjustment,
checked against the true errors of the synthetic trench."""
from __future__ import annotations

import shutil

import numpy as np
import pytest

from pyarchinit_photogrammetry.engine import precision


def _copy(processed, tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    for m in out.chunk.markers:  # the synthetic control points are exact: millimetric survey
        m.reference.accuracy = [0.0005] * 3
    return out, out.chunk


def test_precision_matches_the_true_errors(processed, scene, tmp_path):
    import rasterio

    from pyarchinit_photogrammetry.testing.synthetic import camera_poses

    doc, ch = _copy(processed, tmp_path)
    report = ch.estimate_precision()
    assert report["datum"] == "control points" and report["held"] == "control points"
    assert 0.05 < report["sigma0_px"] < 1.0
    # cameras: their errors are of the size of their precision
    truth = {f"IMG_{i:04d}": pose[:3, 3] + np.asarray(scene.origin) for i, pose in enumerate(camera_poses(scene))}
    z = np.array([(ch.to_world(c.center[None])[0] - truth[c.label]) / np.array(c.meta["precision"])
                  for c in ch.aligned_cameras])
    assert np.all((0.3 < np.sqrt((z ** 2).mean(0))) & (np.sqrt((z ** 2).mean(0)) < 2.0)), z
    # tie points: heights against the true surface, where it is level (walls and plates aside)
    xyz, sigma = precision.tie_point_precision(ch)
    h = scene.world_height(xyz[:, 0], xyz[:, 1])
    gx = (scene.world_height(xyz[:, 0] + 0.01, xyz[:, 1]) - scene.world_height(xyz[:, 0] - 0.01, xyz[:, 1])) / 0.02
    gy = (scene.world_height(xyz[:, 0], xyz[:, 1] + 0.01) - scene.world_height(xyz[:, 0], xyz[:, 1] - 0.01)) / 0.02
    e = xyz[:, 2] - h
    level = (np.hypot(gx, gy) < 0.15) & (np.abs(e) < 0.01)
    assert level.sum() > 500
    ratio = np.sqrt(((e[level] / sigma[level, 2]) ** 2).mean())
    assert 0.6 < ratio < 1.6, ratio
    # markers measured in two photos or more
    assert set(report["markers"]) == {str(m.key) for m in ch.markers if sum(p.measured for p in m.projections.values()) >= 2}
    assert all(0.0001 < v < 0.005 for s in report["markers"].values() for v in s)
    # the map of the DEM
    path = ch.product_path("dem_precision")
    with rasterio.open(path) as src, rasterio.open(ch.dem_path) as dem:
        a = src.read(1, masked=True)
        assert src.crs == dem.crs and not a.mask.all()
        med = float(np.ma.median(a))
    assert 0.0003 < med < 0.003
    assert precision.precision_at(ch, *xyz[level][0, :2]) == pytest.approx(med, rel=1.5)
    # a new georeference: the precision belongs to the old one
    ch.update_transform()
    assert "precision" not in ch.meta and ch.product_path("dem_precision") is None
    assert all("precision" not in c.meta for c in ch.cameras)


def test_interface_and_command_line(processed, tmp_path):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app as cli

    doc, ch = _copy(processed, tmp_path)
    doc.save()
    r = CliRunner().invoke(cli, ["precision", str(doc.path)])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert "datum: control points" in r.output and "precision of the DEM" in r.output
    doc = ppg.Document.open(doc.path)
    assert doc.chunk.meta["precision"]["tie_points"]["count"] > 1000
    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.reference.precision_requested.emit()
    w.runner.wait()
    app.processEvents()
    assert w.ortho_view.kind == "sigma" and w.ortho_view.diff_limit > 0
    col = w.reference.COLS.index("Precisione X/Y/Z (mm)")
    texts = [w.reference.table.item(r, col).text() for r in range(w.reference.table.rowCount())]
    assert any(t.count("/") == 2 for t in texts)
    w._show_coords(0.0, 0.0, 0.0012)
    assert "sigma Z 1.2 mm" in w.coord_label.text()
    # a spot height takes the precision of the DEM where it is
    x, y, _z = doc.chunk.to_world(doc.chunk.tie_points.xyz[:1])[0]
    w._quota_keep = {"area_q": "1", "us_q": "2", "unita_misu_q": "m", "rilievo_originale": "Fotogrammetria"}
    w._quota_clicked(float(x), float(y))
    q = w.quote[-1]
    assert 0.0001 < q["precisione_q"] < 0.005 and q["rilievo_originale"].startswith("Fotogrammetria (±")
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def test_spot_heights_in_ifc(tmp_path):
    pytest.importorskip("ifcopenshell")
    import ifcopenshell
    import ifcopenshell.util.element as ue

    from pyarchinit_photogrammetry.bim.ifc import export_ifc, spot_heights

    quote = [{"x": 2480001.0, "y": 4500001.5, "quota_q": 99.734, "sito_q": "Saggio", "area_q": "1", "us_q": "2",
              "unita_misu_q": "m", "precisione_q": 0.0012, "rilievo_originale": "Fotogrammetria (±1,2 mm)"},
             {"x": 2480002.0, "y": 4500002.0, "quota_q": None}]  # no height: left out
    path = export_ifc(tmp_path / "quote.ifc", [], crs="EPSG:3004", spot_heights=spot_heights(quote))
    model = ifcopenshell.open(str(path))
    (a,) = model.by_type("IfcAnnotation")
    assert a.ObjectType == "SpotHeight" and a.Name == "Quota 99.734 US 2"
    props = ue.get_psets(a)["Pset_PyArchInit_Quota"]
    assert props["Quota"] == pytest.approx(99.734) and props["Precisione"] == pytest.approx(0.0012)
    assert props["US"] == "2" and props["RilievoOriginale"] == "Fotogrammetria (±1,2 mm)"
    place = a.ObjectPlacement.RelativePlacement.Location.Coordinates
    (conv,) = model.by_type("IfcMapConversion")
    assert conv.Eastings + place[0] == pytest.approx(2480001.0) and conv.OrthogonalHeight + place[2] == pytest.approx(99.734)
