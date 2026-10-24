"""Masks of the photos: files, background, colour, model, hand drawing; masked areas left out of
the matching, the depth maps, the orthomosaic and the texture; stationary matches dropped."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import cv2
import numpy as np
import pytest

from pyarchinit_photogrammetry.engine import masks as M


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


def test_files_alpha_and_hand_drawing(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    cams = ch.cameras[:3]
    h, w = int(ch.sensor(cams[0]).height), int(ch.sensor(cams[0]).width)
    folder = tmp_path / "in"
    folder.mkdir()
    for i, cam in enumerate(cams):
        m = np.full((h, w), 255, np.uint8)
        m[:, : w // (2 + i)] = 0
        cv2.imwrite(str(folder / f"{Path(cam.path).stem}_mask.png"), m)
    assert M.import_masks(ch, folder) == 3 and len(M.masked_cameras(ch)) == 3
    keep = M.read_mask(ch, cams[0])
    assert keep.shape == (h, w) and not keep[:, 0].any() and keep[:, -1].all()
    assert M.import_masks(ch, folder, invert=True, cams=cams[:1]) == 1
    assert M.read_mask(ch, cams[0])[:, 0].all()
    out = tmp_path / "out"
    assert M.export_masks(ch, out, "{stem}.png") == 3 and len(list(out.glob("*.png"))) == 3
    # the alpha channel of a photo
    rgba = np.dstack([cv2.imread(cams[1].path), np.zeros((h, w), np.uint8)])
    rgba[h // 4: h // 2, w // 4: w // 2, 3] = 255
    png = tmp_path / "alpha.png"
    cv2.imwrite(str(png), rgba)
    cams[1].path = str(png)
    assert M.import_masks(ch, None, cams=[cams[1]]) == 1
    keep = M.read_mask(ch, cams[1])
    assert keep[h // 3, w // 3] and not keep[0, 0]
    # by hand: a polygon masked, then part of it given back
    M.paint_polygon(ch, cams[2], [[10, 10], [110, 10], [110, 90], [10, 90]], masked=True)
    M.paint_polygon(ch, cams[2], [[10, 10], [50, 10], [50, 90], [10, 90]], masked=False)
    keep = M.read_mask(ch, cams[2])
    assert keep[50, 30] and not keep[50, 80]
    assert M.remove_masks(ch) == 3 and not M.masked_cameras(ch)


def test_masks_from_background_and_colour(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    cams = ch.cameras[:2]
    bg = tmp_path / "background"
    bg.mkdir()
    centres = []
    for cam in cams:
        img = cv2.imread(cam.path)
        shutil.copy2(cam.path, bg / Path(cam.path).name)
        h, w = img.shape[:2]
        c = (w // 2, h // 2)
        cv2.circle(img, c, min(h, w) // 5, (20, 200, 240), -1)  # the "object" on the turntable
        cv2.imwrite(cam.path, img, [cv2.IMWRITE_JPEG_QUALITY, 98])
        centres.append(c)
    assert M.masks_from_background(ch, bg, cams=cams) == 2
    for cam, (cx, cy) in zip(cams, centres):
        keep = M.read_mask(ch, cam)
        assert keep[cy, cx] and not keep[5, 5]
        assert 0.05 < keep.mean() < 0.3
    assert M.masks_from_colour(ch, (240, 200, 20), tolerance=40, cams=cams[:1]) == 1  # RGB of the disc
    keep = M.read_mask(ch, cams[0])
    assert not keep[centres[0][1], centres[0][0]] and keep[5, 5]


def test_masks_from_the_model(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    cam = ch.aligned_cameras[0]
    assert M.masks_from_model(ch, cams=[cam]) == 1
    keep = M.read_mask(ch, cam)
    assert keep is not None and 0.2 < keep.mean() <= 1.0


def test_masked_areas_are_left_out(processed, tmp_path):
    """Left half of a photo masked: no keypoints there, no depth there, and a fully masked photo
    is never used by the orthomosaic."""
    import pyarchinit_photogrammetry as ppg  # noqa: F401
    from pyarchinit_photogrammetry.engine import dense, ortho

    doc, ch = _copy(processed, tmp_path)
    cam = ch.aligned_cameras[0]
    h, w = int(ch.sensor(cam).height), int(ch.sensor(cam).width)
    left = np.ones((h, w), bool)
    left[:, : w // 2] = False
    M.write_mask(ch, cam, left)
    ch.match_photos(quality="high")
    db = sqlite3.connect(str(ch.data_dir / "sfm" / "database.db"))
    name = ch.meta["sfm"]["names"][str(cam.key)]
    (image_id,) = db.execute("SELECT image_id FROM images WHERE name = ?", (name,)).fetchone()
    rows, cols, data = db.execute("SELECT rows, cols, data FROM keypoints WHERE image_id = ?", (image_id,)).fetchone()
    db.close()
    kp = np.frombuffer(data, np.float32).reshape(rows, cols)
    assert len(kp) > 50 and kp[:, 0].min() >= w // 2 - 2
    ch.align_cameras()
    ch.update_transform()
    ch.build_depth_maps(quality="medium", backend="cpu")
    ws = ch.data_dir / "dense"
    depth = dense.read_colmap_array(ws / "stereo" / "depth_maps" / f"{name}.geometric.bin")
    dh, dw = depth.shape
    assert (depth[:, : dw // 2 - 4] > 0).sum() == 0 and (depth[:, dw // 2 + 4:] > 0).sum() > 100
    # a photo masked everywhere is never taken
    other = ch.aligned_cameras[1]
    M.write_mask(ch, other, np.zeros((h, w), bool))
    ch.build_orthomosaic(resolution=0.01, blending="disabled", colour_correction=False)
    import rasterio

    from pyarchinit_photogrammetry.engine.patches import photos_for_area

    with rasterio.open(ch.orthomosaic_path) as src:
        b = src.bounds
    area = [[b.left, b.bottom], [b.right, b.bottom], [b.right, b.top], [b.left, b.top]]
    used = {d["label"]: d["used"] for d in photos_for_area(ch, area, limit=100)}
    assert used.get(other.label, 0.0) == 0.0
    assert ortho  # the module that leaves the masked photo out


def test_stationary_matches_dropped(processed, tmp_path):
    """The same textured patch at the same place in every photo (a fixed background) gives
    matches that do not move: they are dropped, the photos still align."""
    doc, ch = _copy(processed, tmp_path)
    rng = np.random.default_rng(1)
    patch = cv2.GaussianBlur((rng.random((140, 220, 3)) * 255).astype(np.uint8), (0, 0), 1.2)
    for cam in ch.cameras:
        img = cv2.imread(cam.path)
        img[10:150, 10:230] = patch
        cv2.imwrite(cam.path, img, [cv2.IMWRITE_JPEG_QUALITY, 98])
    ch.match_photos(quality="high", exclude_stationary=True)
    assert ch.meta["sfm"]["stationary_removed"] > 100
    assert ch.align_cameras() >= len(ch.cameras) - 1


def test_interface_masks_menu(processed, tmp_path, monkeypatch):
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
    shown = []
    monkeypatch.setattr(mw, "message", lambda parent, kind, title, text, *a, **k: shown.append((title, text)))
    cam = w.chunk.aligned_cameras[0]
    w.open_photo(cam)
    v = w.photo_view
    v.set_mask_mode("mask")
    poly = [(20.5, 20.5), (220.5, 20.5), (220.5, 160.5), (20.5, 160.5)]
    for x, y in poly:
        v._mask_click(x, y)
    v._mask_close()
    keep = M.read_mask(w.chunk, cam)
    assert keep is not None and not keep[90, 120] and keep[300, 400]
    assert v.mask_item is not None  # the masked area is shown over the photo
    w.photos.select_camera(cam)
    w.masks_remove(selected=True)
    assert M.read_mask(w.chunk, cam) is None
    w.setWindowModified(False)
    w.close()
    app.processEvents()
