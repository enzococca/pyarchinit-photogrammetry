"""Batch processing over several chunks: saved after every step, resumed, retried."""
from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from pyarchinit_photogrammetry import batch
from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

STEPS = [batch.step("detect_markers"),
         batch.step("align_photos", quality="medium"),
         batch.step("import_reference", path="{project}/gcp.csv", crs="EPSG:3004"),
         batch.step("build_depth_maps", quality="low"),
         batch.step("build_point_cloud"),
         batch.step("build_dem"),
         batch.step("export_dem", path="{project}/export/{chunk}_dem.tif")]


def _project(scene_dir, tmp_path):
    import shutil

    import pyarchinit_photogrammetry as ppg

    d = tmp_path / "scavo"
    shutil.copytree(scene_dir / "photos", d / "photos")
    shutil.copy2(scene_dir / "gcp.csv", d / "gcp.csv")
    (d / "vuote").mkdir()
    for i in range(3):  # photos with nothing to match: the alignment of this chunk fails
        cv2.imwrite(str(d / "vuote" / f"V{i}.jpg"), np.full((300, 400, 3), 128, np.uint8))
    doc = ppg.Document()
    doc.add_chunk("Saggio 1").add_photos([d / "photos"])
    doc.add_chunk("Prova vuota").add_photos([d / "vuote"])
    doc.save(d / "scavo.ppg")
    return doc


def test_batch_saves_resumes_and_skips(scene_dir, tmp_path, monkeypatch):
    import pyarchinit_photogrammetry as ppg

    doc = _project(scene_dir, tmp_path)
    calls = []
    original = batch.run_step

    def counting(doc_, chunk, op, params, progress=None):
        calls.append((op, chunk.label))
        return original(doc_, chunk, op, params, progress)

    monkeypatch.setattr(batch, "run_step", counting)

    def power_cut(fraction, message):  # the batch stops while building the depth maps
        if message.startswith("Batch 7/"):
            raise Cancelled("power cut")

    with pytest.raises(Cancelled):
        batch.run_batch(doc, STEPS, progress=Progress(power_cut))
    state = json.loads((doc.files_dir / batch.STATE).read_text())
    assert state["finished"] is None and len(state["done"]) == 4  # markers x2, alignment of Saggio 1, reference
    assert set(state["failed"]) == {"1:1"}  # the alignment of the empty chunk, tried twice
    assert calls.count(("align_photos", "Prova vuota")) == 2
    assert batch.pending(ppg.Document.open(doc.path), STEPS) is not None
    # the project was saved after every step: opened again, the alignment is there
    doc = ppg.Document.open(doc.path)
    assert doc.chunks[0].aligned_cameras and doc.chunks[0].markers[0].reference.location
    calls.clear()
    report = batch.run_batch(doc, STEPS)
    assert ("align_photos", "Saggio 1") not in calls and ("build_depth_maps", "Saggio 1") in calls
    # the failed step is tried again, the next ones of that chunk are skipped
    assert [op for op, label in calls if label == "Prova vuota"] == ["align_photos", "align_photos"]
    assert len(report["done"]) == 4 + 4 and set(report["failed"]) == {"1:1"}
    assert set(report["skipped"]) == {f"1:{i}" for i in range(2, 7)}
    assert (doc.path.parent / "export" / "Saggio_1_dem.tif").exists()
    assert batch.pending(doc) is None  # finished


def test_retry_and_stop(scene_dir, tmp_path, monkeypatch):
    doc = _project(scene_dir, tmp_path)
    tries = {"n": 0}

    def flaky(doc_, chunk, op, params, progress=None):
        tries["n"] += 1
        if tries["n"] == 1:
            raise RuntimeError("disk not ready")

    monkeypatch.setattr(batch, "run_step", flaky)
    report = batch.run_batch(doc, [batch.step("build_point_cloud", chunks=["Saggio 1"])], retries=1)
    assert report["done"] == ["0:0"] and not report["failed"] and tries["n"] == 2

    def broken(doc_, chunk, op, params, progress=None):
        raise RuntimeError("no photos")

    monkeypatch.setattr(batch, "run_step", broken)
    with pytest.raises(batch.BatchError, match="no photos"):
        batch.run_batch(doc, [batch.step("build_dem")], retries=0, on_error="stop", resume=False)
    with pytest.raises(ValueError):
        batch.step("build_dem", colour="red")
    with pytest.raises(ValueError):
        batch.targets(doc, ["Saggio 9"])


def test_command_line(scene_dir, tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc = _project(scene_dir, tmp_path)
    steps = batch.save_steps([batch.step("detect_markers", chunks=["Saggio 1"])], tmp_path / "passi.json")
    r = CliRunner().invoke(app, ["batch", str(doc.path), str(steps)])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert "1 steps done, 0 failed, 0 skipped" in r.output
    assert batch.load_steps(steps)[0]["params"] == {"target_type": "aruco_4x4_50"}


def test_interface(scene_dir, tmp_path, monkeypatch):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QDialog

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import batch_dialog
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc = _project(scene_dir, tmp_path)
    # the dialog: every operation has its form and its line
    dlg = batch_dialog.BatchDialog(doc, [batch.step("detect_markers"), batch.step("build_dem", chunks=["Saggio 1"])])
    for op in batch.OPERATIONS:
        assert isinstance(batch_dialog.fields(op, {}), list) and batch_dialog.describe(batch.step(op))
    assert dlg.list.count() == 2 and "Saggio 1" in dlg.list.item(1).text()
    dlg.list.setCurrentRow(1)
    dlg.move(-1)
    assert [s["op"] for s in dlg.steps()] == ["build_dem", "detect_markers"]
    dlg.remove_step()
    assert len(dlg.steps()) == 1 and dlg.options() == {"retries": 1, "on_error": "continue"}
    dlg.deleteLater()

    class Fake:
        DialogCode = QDialog.DialogCode

        def __init__(self, doc_, steps, parent=None):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def steps(self):
            return [batch.step("detect_markers", chunks=["Saggio 1"]), batch.step("build_point_cloud")]

        def options(self):
            return {"retries": 0, "on_error": "continue"}

    monkeypatch.setattr(batch_dialog, "BatchDialog", Fake)
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.batch_process()
    w.runner.wait()
    app.processEvents()
    assert doc.chunks[0].markers  # detected, then the project saved
    state = json.loads((doc.files_dir / batch.STATE).read_text())
    assert state["finished"] and set(state["failed"]) == {"0:1", "1:1"}  # no alignment: no dense cloud
    # an unfinished batch is offered to be resumed
    state["finished"], state["done"] = None, ["0:0"]
    (doc.files_dir / batch.STATE).write_text(json.dumps(state))
    asked = []
    monkeypatch.setattr(mw, "choice", lambda *a, **k: asked.append(a) or 0)
    w.batch_process()
    w.runner.wait()
    app.processEvents()
    assert asked and "dopo 1 passo su 3" in asked[0][3]
    w.setWindowModified(False)
    w.close()
    app.processEvents()
