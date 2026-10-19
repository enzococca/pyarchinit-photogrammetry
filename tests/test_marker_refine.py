"""Blue flags (estimated marker projections) found again in the photos by image correlation."""
from __future__ import annotations

import shutil

import cv2
import numpy as np

from pyarchinit_photogrammetry.core.marker import Projection
from pyarchinit_photogrammetry.markers import gcp, refine


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


def test_flags_move_onto_the_target(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    m = max(ch.markers, key=lambda mk: sum(p.measured for p in mk.projections.values()))
    truth = {k: (p.x, p.y) for k, p in m.projections.items() if p.measured}
    assert len(truth) >= 6
    # placed by hand in one photo only, as a user would; the others are blue flags off the point
    ref = next(iter(truth))
    m.projections = {ref: Projection(*truth[ref], True)}
    ch.triangulate_markers()
    gcp.project_marker(ch, m)
    rng = np.random.default_rng(1)
    for k, p in m.projections.items():
        if not p.measured:
            p.x, p.y = p.x + rng.uniform(-5, 5), p.y + rng.uniform(-5, 5)
    blank = next(k for k in m.projections if k != ref and k in truth)  # a photo with nothing to match
    path = ch.camera(blank).path
    cv2.imwrite(path, np.full_like(cv2.imread(path), 128))
    before = [np.hypot(p.x - truth[k][0], p.y - truth[k][1]) for k, p in m.projections.items()
              if not p.measured and k in truth]
    report = refine.refine_markers(ch, [m])[m.key]
    assert report["reference"] == ref and blank in report["failed"]
    assert not m.projections[blank].measured  # still a flag to confirm
    errors = [np.hypot(m.projections[k].x - truth[k][0], m.projections[k].y - truth[k][1])
              for k in report["refined"] if k in truth]
    assert len(errors) >= 0.7 * (len(truth) - 2)
    assert np.median(before) > 3 and np.median(errors) < 0.5 and max(errors) < 1.0, (before, errors)
    assert all(m.projections[k].pinned is False for k in report["refined"])
    # the marker is triangulated from the flags found
    assert gcp.views_of(ch, m) == 1 + len(report["refined"])


def test_interface_refines_the_flags(processed, tmp_path):
    import os

    import pytest

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, ch = _copy(processed, tmp_path)
    m = max(ch.markers, key=lambda mk: sum(p.measured for p in mk.projections.values()))
    ref, p = next(iter(m.projections.items()))
    m.projections = {ref: Projection(p.x, p.y, True)}
    ch.triangulate_markers()
    gcp.project_marker(ch, m)
    flags = [k for k, p in m.projections.items() if not p.measured]
    for k in flags:
        m.projections[k].x += 3.0
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.reference.refine_requested.emit([m.key])
    w.runner.wait()
    app.processEvents()
    assert sum(m.projections[k].measured for k in flags if k in m.projections) >= len(flags) // 2
    assert gcp.views_of(ch, m) > 1
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def test_command_line(processed, tmp_path):
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    doc, ch = _copy(processed, tmp_path)
    m = max(ch.markers, key=lambda mk: sum(p.measured for p in mk.projections.values()))
    ref, p = next(iter(m.projections.items()))
    m.projections = {ref: Projection(p.x, p.y, True)}
    doc.save()
    r = CliRunner().invoke(app, ["refine-markers", str(doc.path), "--marker", m.label])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert f"{m.label}: " in r.output and " found" in r.output
    again = ppg.Document.open(doc.path).chunk.marker_by_key(m.key)
    assert sum(p.measured for p in again.projections.values()) > 3
