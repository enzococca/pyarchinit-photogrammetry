"""Field check before leaving the site: coverage, photos and markers."""
from __future__ import annotations

import shutil

import cv2
import numpy as np
import pytest

from pyarchinit_photogrammetry.core.marker import Projection
from pyarchinit_photogrammetry.engine import fieldcheck


def _copy(processed, tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def test_field_check_finds_gaps_photos_and_markers(processed, tmp_path):
    import rasterio

    doc, ch = _copy(processed, tmp_path)
    rep = fieldcheck.field_check(ch)
    assert rep["unaligned"] == [] and rep["blurred"] == [] and rep["markers"] == []
    assert rep["weak_fraction"] < 0.01 and rep["median_photos"] >= 5
    with rasterio.open(ch.product_path("coverage")) as src:
        counts = src.read(1)
        assert src.crs is not None and counts.max() >= 9
    # a blurred photo, a marker placed in one photo only, the west half photographed by two photos
    blurred = ch.cameras[3]
    cv2.imwrite(blurred.path, cv2.GaussianBlur(cv2.imread(blurred.path), (0, 0), 3.0))
    for c in ch.cameras:
        c.meta.pop("quality", None)
    centres = {c.key: ch.to_world(c.center[None])[0] for c in ch.aligned_cameras}
    mid = float(np.median([v[0] for v in centres.values()]))
    west = sorted((c for c in ch.cameras if centres[c.key][0] < mid), key=lambda c: centres[c.key][0])
    for c in west[2:]:
        c.enabled = False
    m = next(mk for mk in ch.markers if any(ch.camera(k).enabled for k in mk.projections))
    k, p = next((k, p) for k, p in m.projections.items() if ch.camera(k).enabled)
    m.projections = {k: Projection(p.x, p.y, True)}
    rep = fieldcheck.field_check(ch)
    assert blurred.label in rep["blurred"]
    assert {"label": m.label, "views": 1, "status": "one photo", "reference": True} in rep["markers"]
    assert rep["weak_fraction"] > 0.02 and rep["weak_area"] > 0.2
    with rasterio.open(ch.product_path("coverage")) as src:
        counts = src.read(1)
        rows, cols = np.nonzero((counts >= 0) & (counts < 3))
        xs, _ys = rasterio.transform.xy(src.transform, rows, cols)
    assert np.mean(np.asarray(xs) < mid) > 0.8  # the gaps are where the photos were left out


def test_interface_and_command_line(processed, tmp_path, monkeypatch):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app as cli

    doc, ch = _copy(processed, tmp_path)
    r = CliRunner().invoke(cli, ["field-check", str(doc.path)])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert "coverage: 0.0% of the area" in r.output and "not aligned 0" in r.output
    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    shown = []
    monkeypatch.setattr(w, "_info", lambda title, text: shown.append(text))
    w.field_check()
    w.runner.wait()
    app.processEvents()
    assert shown and "Copertura: tutta l'area" in shown[0] and "si può lasciare il sito" in shown[0]
    assert w.ortho_view.kind == "coverage"
    w._show_coords(0.0, 0.0, 7.0)
    assert "7 foto" in w.coord_label.text()
    w.setWindowModified(False)
    w.close()
    app.processEvents()
