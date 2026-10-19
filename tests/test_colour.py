"""Seamless orthomosaic: colour correction between photos (exposure, vignetting), two-band
blending across the seams, ghosting filter and small holes filled."""
from __future__ import annotations

import shutil

import cv2
import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.engine import colour, ortho

FALLOFF = -0.35  # v(r) = FALLOFF r^2 put on every photo


def _copy(processed, tmp_path, name):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / name
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def _darken(ch) -> dict[int, float]:
    """Every other photo 30 % darker, all of them with a lens falloff: the true gains."""
    gains = {}
    for i, cam in enumerate(ch.aligned_cameras):
        img = cv2.imread(cam.path).astype(np.float32)
        s, h, w = ch.sensor(cam), img.shape[0], img.shape[1]
        k = s.K
        yy, xx = np.mgrid[0:h, 0:w]
        r = np.hypot(xx + 0.5 - k[0, 2], yy + 0.5 - k[1, 2]) / (0.5 * np.hypot(s.width, s.height))
        g = 0.7 if i % 2 else 1.0
        gains[cam.key] = g
        cv2.imwrite(cam.path, np.clip(img * g * np.exp(FALLOFF * r ** 2)[..., None], 0, 255).astype(np.uint8),
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
    return gains


def _read(path):
    with rasterio.open(path) as src:
        return src.read()


@pytest.fixture(scope="module")
def darkened(processed, tmp_path_factory):
    """A copy of the processed trench whose photos differ in exposure and vignetting, and the
    orthomosaic of the untouched photos."""
    tmp = tmp_path_factory.mktemp("colour")
    _doc, ref_ch = _copy(processed, tmp, "reference")
    reference = _read(ref_ch.build_orthomosaic(resolution=0.004, blending="disabled", colour_correction=False))
    doc, ch = _copy(processed, tmp, "darkened")
    return doc, ch, _darken(ch), reference


def test_calibration_finds_gains_and_vignetting(darkened):
    doc, ch, gains, _ = darkened
    info = ch.calibrate_colours()
    assert info is ch.meta["colour"] and info["rms_after"] < 0.3 * info["rms_before"]
    est = np.array([np.mean(info["gains"][str(c.key)]) for c in ch.aligned_cameras])
    true = np.array([gains[c.key] for c in ch.aligned_cameras])
    true /= np.exp(np.mean(np.log(true)))  # the mean log gain is kept at zero
    assert np.max(np.abs(est / true - 1)) < 0.02
    (b,) = info["vignetting"].values()
    for r in (0.5, 0.8):
        assert abs(b[0] * r ** 2 + b[1] * r ** 4 + b[2] * r ** 6 - FALLOFF * r ** 2) < 0.02
    corr = colour.Correction(ch)
    cam = ch.aligned_cameras[1]
    fixed = corr.correct(cam, cv2.imread(cam.path)).astype(float)
    assert fixed.mean() > cv2.imread(cam.path).mean() * 1.2  # the darker photo brightened


def _low_error(img, reference):
    """Mean difference of the low frequencies (after the overall exposure) on common cells."""
    m = (img[3] == 255) & (reference[3] == 255)
    a, r = img[:3].astype(np.float32).mean(axis=0), reference[:3].astype(np.float32).mean(axis=0)
    a *= np.median(r[m]) / np.median(a[m])
    return float(np.abs(cv2.GaussianBlur(a, (0, 0), 6) - cv2.GaussianBlur(r, (0, 0), 6))[m].mean())


def test_corrected_and_blended_orthomosaic(darkened):
    doc, ch, _gains, reference = darkened
    ch.calibrate_colours()
    hard = _read(ch.build_orthomosaic(resolution=0.004, blending="disabled", colour_correction=False))
    soft = _read(ch.build_orthomosaic(resolution=0.004, blending="mosaic", colour_correction=False))
    fixed = _read(ch.build_orthomosaic(resolution=0.004, blending="mosaic", colour_correction=True))
    assert ch.meta["orthomosaic"]["colour_correction"] and ch.meta["orthomosaic"]["blend_sigma"] > 0
    assert _low_error(fixed, reference) < 2.0 < _low_error(hard, reference)
    # blending: the steps at the seams are gone (high frequencies of the grey image lower than
    # with one photo per cell, and not lower than in the reference: details are kept)

    def steps(img):
        g = img[:3].astype(np.float32).mean(axis=0)
        m = cv2.erode((img[3] == 255).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
        return float(np.abs(g - cv2.GaussianBlur(g, (0, 0), 3))[m].mean())

    assert steps(soft) < steps(hard)
    assert steps(fixed) > 0.8 * steps(reference)


def test_ghosting_filter_leaves_out_what_one_photo_shows(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path, "ghost")
    plain = _read(ch.build_orthomosaic(resolution=0.004, blending="disabled", colour_correction=False))
    # a red "person" in the middle of the photo the orthomosaic uses most at the centre
    h, w = plain.shape[1:]
    from rasterio.transform import xy

    with rasterio.open(ch.orthomosaic_path) as src:
        x, y = xy(src.transform, h // 2, w // 2)
    z = ch.dem_path and rasterio.open(ch.dem_path)
    zc = float(next(z.sample([(x, y)]))[0])
    z.close()
    p = ch.to_internal(np.array([[x, y, zc]]))
    # the photo the orthomosaic takes there: the best score
    inv = np.linalg.inv(ch.transform)
    up = inv[:3, :3] @ np.array([0.0, 0.0, 1.0])
    centres = {c.key: c.center for c in ch.aligned_cameras}
    best = max(ch.aligned_cameras, key=lambda c: ortho._scores(ch, c, p, centres, up / np.linalg.norm(up), 0.34)[1][0])
    px = ortho._scores(ch, best, p, centres, up / np.linalg.norm(up), 0.34)[0]
    img = cv2.imread(best.path)
    cv2.circle(img, (int(px[0, 0]), int(px[0, 1])), 25, (0, 0, 255), -1)
    cv2.imwrite(best.path, img, [cv2.IMWRITE_JPEG_QUALITY, 95])

    def red(img):
        return int(((img[0] > 180) & (img[1] < 60) & (img[2] < 60) & (img[3] == 255)).sum())

    with_ghost = red(_read(ch.build_orthomosaic(resolution=0.004, blending="disabled", colour_correction=False)))
    without = red(_read(ch.build_orthomosaic(resolution=0.004, blending="disabled", colour_correction=False,
                                             ghosting=True)))
    assert ch.meta["orthomosaic"]["ghosting"]
    assert red(plain) == 0 and with_ghost > 200  # the photo is used there...
    assert without < 0.1 * with_ghost  # ...and the filter takes another one


def test_small_holes_filled_from_their_own_group():
    yy, xx = np.mgrid[0:60, 0:80]
    rgb = np.dstack([xx * 2.0, yy * 3.0, np.full_like(xx, 50.0)]).astype(np.float64)
    painted = np.ones((60, 80), bool)
    painted[20:26, 20:26] = False      # 36 cells: filled
    painted[5:20, 50:72] = False       # 330 cells: left empty
    surface = np.ones((60, 80), bool)
    group = np.full((60, 80), -1)
    group[:, 40:] = 1                  # a patch on the right: its colours do not leak out
    rgb[group == 1] = (255.0, 0.0, 255.0)
    before = rgb.copy()
    filled = ortho._fill_holes(rgb, painted, surface, group)
    assert filled[20:26, 20:26].all() and not filled[5:20, 50:72].any()
    hole = rgb[20:26, 20:26]
    assert np.abs(hole - before[20:26, 20:26]).max() < 25  # smooth continuation of the gradient
    assert not (hole[..., 0] > 200).any()  # nothing from the patch


def test_interface_calibrates_and_builds_with_the_options(processed, tmp_path, monkeypatch):
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

    doc, ch = _copy(processed, tmp_path, "gui")
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    shown = []
    monkeypatch.setattr(mw, "message", lambda parent, kind, title, text, *a, **k: shown.append((title, text)))
    w.calibrate_colours()
    w.runner.wait()
    assert w.chunk.meta.get("colour") and shown and shown[-1][0] == "Colori delle foto"
    assert "osservazioni" in shown[-1][1]
    seen = []

    def fake(cls, parent, title, fields, intro=""):
        seen.append({f.name: f for f in fields})
        return {"surface": "dem", "blending": "mosaic", "resolution": 0.008, "max_angle": 70.0, "colour": True,
                "ghosting": True, "fill_holes": True, "keep": "overwrite", "version_name": ""}

    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(fake))
    w.build_ortho()
    w.runner.wait()
    fields = seen[-1]
    assert [c[0] for c in fields["blending"].choices] == ["mosaic", "disabled", "average"]
    assert {"colour", "ghosting", "fill_holes"} <= set(fields)
    meta = w.chunk.meta["orthomosaic"]
    assert meta["blending"] == "mosaic" and meta["colour_correction"] and meta["ghosting"] and meta["fill_holes"]
    w.setWindowModified(False)
    w.close()
    app.processEvents()
