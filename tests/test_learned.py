"""Learned key points matched by LightGlue (DISK or ALIKED, kornia): the synthetic trench aligned to a
few millimetres, photos added to such an alignment, and PyTorch never in the program's process -
PyTorch and pycolmap each bring an OpenMP runtime, and the second to start aborts the process. The
network tests need PyTorch and kornia (the [learned] extra) and are skipped without.

The trench is a hard case for the networks: a self-similar fractal texture and 8 oblique photos
turned far from the 20 nadir ones with no heading in their files to turn them back. With COLMAP's
verification thresholds wrong pairs bent the block by decimetres; with those of ``learned`` DISK
places the nadir photos and some oblique ones (1.8 mm on the control points), ALIKED all of them
(2.9 mm); SIFT 0.4 mm."""
from __future__ import annotations

import subprocess
import sys

import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine import learned

needs_torch = pytest.mark.skipif(learned.available() is None, reason="PyTorch and kornia are not installed")


def _project(scene_dir, tmp_path, photos=None):
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(tmp_path / "project.ppg")
    ch.add_photos(photos if photos is not None else [scene_dir / "photos"])
    ch.detect_markers("aruco_4x4_50")
    return doc, ch


@needs_torch
@pytest.mark.parametrize("kind", ["disk", "aliked"])
def test_learned_points_align_the_trench(scene_dir, tmp_path, kind):
    _doc, ch = _project(scene_dir, tmp_path)
    ch.match_photos(quality="high", matcher=kind)
    info = ch.meta["sfm"]
    assert info["matcher"] == kind and info["features"] == kind
    assert len(list((ch.data_dir / "sfm" / "learned" / kind).glob("*.npz"))) == len(ch.cameras)
    ch.align_cameras(mapper="incremental", use_gps=False)
    nadir = {f"IMG_{i:04d}" for i in range(20)}
    assert nadir <= {c.label for c in ch.aligned_cameras}
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    assert ch.update_transform()["control_rmse"] < 0.004
    assert "torch" not in sys.modules  # the network ran in its worker process


@needs_torch
def test_photos_added_to_a_learned_alignment(scene_dir, tmp_path):
    photos = sorted((scene_dir / "photos").glob("*.jpg"))[:20]  # the nadir photos
    doc, ch = _project(scene_dir, tmp_path, photos[:12])
    ch.match_photos(quality="high", matcher="disk")
    assert ch.align_cameras(mapper="incremental", use_gps=False) == 12
    ch.add_photos(photos[12:])
    ch.detect_markers("aruco_4x4_50")
    ch.match_photos(quality="high", matcher="disk", reset=False)
    assert ch.meta["sfm"]["continued"] and len(ch.meta["sfm"]["added"]) == len(photos) - 12
    assert ch.align_cameras(mapper="incremental", use_gps=False, reset=False) == len(photos)
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    assert ch.update_transform()["control_rmse"] < 0.004
    # the points of another matcher cannot join: SIFT starts again from every photo
    ch.match_photos(quality="high", matcher="sift", reset=False)
    assert not ch.meta["sfm"]["continued"] and ch.meta["sfm"]["matcher"] == "sift"
    assert not (ch.data_dir / "sfm" / "learned").exists()


def test_photos_turned_to_one_heading_and_their_points_back(monkeypatch):
    """The networks are not invariant to rotation: the photos are turned by quarter turns to the
    heading they share (strips flown one way and back, cross strips) and the points come back."""
    import numpy as np

    img = np.zeros((48, 64), np.uint8)
    for k in range(4):
        img[:] = 0
        img[7, 50] = 255
        turned = learned.turn(img, k)
        y1, x1 = np.argwhere(turned == 255)[0]
        assert learned.unturn_points([[x1, y1]], k, 64, 48)[0].tolist() == [50, 7]
    # clockwise: a photo taken heading east (top of the photo to the east) turned once has north up
    east = np.zeros((48, 64), np.uint8)
    east[:, :8] = 255  # the left side of that photo is north
    assert learned.turn(east, 1)[:8].min() == 255

    class Cam:
        def __init__(self, key, yaw):
            self.key, self.enabled, self.yaw = key, True, yaw

    def run(yaws):
        cams = [Cam(k, y) for k, y in enumerate(yaws)]
        monkeypatch.setattr(learned, "_camera_yaw", lambda cam: cam.yaw)
        chunk = type("Chunk", (), {"cameras": cams})()
        turns = learned.quarter_turns(chunk, {c.key: f"{c.key}.jpg" for c in cams})
        return [((y - 90 * turns[f"{k}.jpg"] + 180) % 360) - 180 for k, y in enumerate(yaws)]

    headings = run([88, -92, 179, 2, 91, -178, -1])  # strips flown east and west, north and south
    assert max(headings) - min(headings) < 6
    headings = run([44, 46, 224, 226, 134, 136, -44, -46])  # strips at 45 degrees: never a quarter turn apart
    assert max(headings) - min(headings) < 6


def test_choices_without_pytorch(scene_dir, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.ai import sam_env

    monkeypatch.setattr(learned, "local", lambda: False)
    monkeypatch.setattr(sam_env, "is_installed", lambda env=None: False)
    assert learned.available() is None
    with pytest.raises(ImportError, match="kornia"):
        learned.open_features("disk")
    _doc, ch = _project(scene_dir, tmp_path)
    with pytest.raises(ValueError, match="matcher"):
        ch.match_photos(matcher="orb")
    with pytest.raises(ImportError):
        ch.match_photos(matcher="aliked")


def test_pytorch_is_never_imported_by_the_program():
    """Looking for SAM 2.1 and the learned points, and the environment diagnosis, import no PyTorch."""
    code = ("import sys, pyarchinit_photogrammetry, pycolmap\n"
            "from pyarchinit_photogrammetry.ai import sam_env\n"
            "from pyarchinit_photogrammetry.engine import learned\n"
            "from pyarchinit_photogrammetry import doctor\n"
            "sam_env.available(); learned.available(); doctor.run_checks(gui=False)\n"
            "print('torch' in sys.modules, 'kornia' in sys.modules)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=600, close_fds=False)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "False False"


def test_the_alignment_dialog_offers_the_learned_points(scene_dir, tmp_path, monkeypatch):
    """*Allinea foto* > *Punti*: a learned matcher that cannot run yet is offered for installation,
    the alignment follows the installation, the choice is remembered."""
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    from pyarchinit_photogrammetry.core.progress import as_progress
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, ch = _project(scene_dir, tmp_path, sorted((scene_dir / "photos").glob("*.jpg"))[:3])
    w = mw.MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    try:
        choices = {"quality": "high", "keypoint_limit": 40000, "tiepoint_limit": 8000, "matcher": "disk",
                   "generic": True, "reference": "none", "reset": True, "masks": "keypoints", "mapper": "auto",
                   "gpu": "off"}
        monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: dict(choices)))
        monkeypatch.setattr(learned, "available", lambda: None)
        monkeypatch.setattr(w, "_info", lambda *a, **k: None)
        ran, matched = [], {}

        def run_task(label, fn, on_done=None, **k):
            ran.append(label)
            result = "/env" if "punti appresi" in label else fn(as_progress(None))
            if on_done is not None:
                on_done(result)
            return True

        monkeypatch.setattr(w, "run_task", run_task)
        monkeypatch.setattr(learned, "install", lambda progress=None: "/env")
        monkeypatch.setattr(ch, "match_photos", lambda *a, **k: matched.update(k))
        monkeypatch.setattr(ch, "align_cameras", lambda *a, **k: 3)
        monkeypatch.setattr(mw, "message", lambda *a, **k: QMessageBox.StandardButton.No)
        w.align_photos()
        assert ran == []  # not installed: nothing runs
        monkeypatch.setattr(mw, "message", lambda *a, **k: QMessageBox.StandardButton.Yes)
        w.align_photos()
        assert ran == ["Installazione dei punti appresi", "Allineamento"] and matched["matcher"] == "disk"
        assert w.settings.value("sfm/matcher") == "disk"
        ran.clear()
        choices["matcher"] = "sift"  # SIFT needs nothing
        w.align_photos()
        assert ran == ["Allineamento"] and matched["matcher"] == "sift"
    finally:
        w.setWindowModified(False)
        w.close()
        app.processEvents()
