"""Image quality (sharpness of the photos) and photos from a video with DJI subtitles."""
from __future__ import annotations

import shutil

import cv2
import numpy as np
import pytest

from pyarchinit_photogrammetry.engine import quality, video


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


def test_blurred_photos_have_a_low_quality(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    blurred = ch.cameras[:3]
    for cam in blurred:
        img = cv2.imread(cam.path)
        cv2.imwrite(cam.path, cv2.GaussianBlur(img, (0, 0), 3.0), [cv2.IMWRITE_JPEG_QUALITY, 95])
    q = quality.estimate_quality(ch)
    assert len(q) == len(ch.cameras)
    assert all(q[c.key] < quality.THRESHOLD for c in blurred)
    others = [q[c.key] for c in ch.cameras[3:]]
    assert np.median(others) > 0.8 and min(others) > quality.THRESHOLD
    assert {c.key for c in quality.low_quality(ch)} == {c.key for c in blurred}
    assert ch.cameras[0].meta["quality"] == q[ch.cameras[0].key]


SRT_NEW = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2024-05-12 10:11:12.123
[iso: 100] [shutter: 1/1000.0] [fnum: 1.7] [focal_len: 24.00] [latitude: 43.700000] [longitude: 11.200000] [rel_alt: 30.000 abs_alt: 250.000] </font>

2
00:00:01,000 --> 00:00:01,033
<font size="28">FrameCnt: 31, DiffTime: 33ms
[latitude: 43.700100] [longitude: 11.200200] [rel_alt: 31.000 abs_alt: 251.000] </font>
"""
SRT_OLD = """1
00:00:00,000 --> 00:00:01,000
HOME(11.2000,43.7000) 2018.05.12 10:11:12
GPS(11.2001,43.7001,15) BAROMETER:30.5

2
00:00:01,000 --> 00:00:02,000
GPS(11.2003,43.7002,16) BAROMETER:31.0
"""
SRT_P4 = """1
00:00:00,000 --> 00:00:01,000
F/2.8, SS 1000.00, ISO 100, EV 0, GPS (11.2001, 43.7001, 19), D 23.45m, H 30.10m, H.S 0.00m/s
"""


def test_dji_subtitles(tmp_path):
    for text, first in ((SRT_NEW, (0.0, 11.2, 43.7, 250.0)), (SRT_OLD, (0.0, 11.2001, 43.7001, 15.0)),
                        (SRT_P4, (0.0, 11.2001, 43.7001, 19.0))):
        p = tmp_path / "v.SRT"
        p.write_text(text)
        track = video.read_srt(p)
        assert track and np.allclose(track[0], first)
    p.write_text(SRT_NEW)
    lon, lat, alt = video.position_at(video.read_srt(p), 0.5)
    assert np.isclose(lon, 11.2001) and np.isclose(lat, 43.70005) and np.isclose(alt, 250.5)


def _pan_video(path, frames=150, fps=30, step=6, size=(480, 360), blur_every=4):
    rng = np.random.default_rng(0)
    w, h = size
    world = cv2.GaussianBlur((rng.random((h, w + frames * step + 10, 3)) * 255).astype(np.uint8), (0, 0), 1.5)
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not out.isOpened():
        return False
    for i in range(frames):
        f = world[:, i * step:i * step + w].copy()
        if i % blur_every == 1:  # motion blur on some frames
            f = cv2.blur(f, (15, 1))
        out.write(f)
    out.release()
    return True


def test_frames_follow_the_motion_and_skip_blur(tmp_path):
    path = tmp_path / "volo.mp4"
    if not _pan_video(path):
        pytest.skip("no MP4 writer in this OpenCV")
    kept = video.extract_frames(path, tmp_path / "frames", overlap=0.7)
    # 150 frames x 6 px = 900 px of motion; a new frame every 30 % of 480 px (144 px = 24 frames)
    assert 5 <= len(kept) <= 9
    frames = [k["frame"] for k in kept]
    assert frames == sorted(frames) and all(f % 4 != 1 for f in frames)  # never a blurred one
    assert all(k["path"].exists() for k in kept)
    every = video.extract_frames(path, tmp_path / "every", every=1.0)
    assert len(every) == 5  # one a second for five seconds


def test_add_video_with_positions(tmp_path):
    import pyarchinit_photogrammetry as ppg

    path = tmp_path / "DJI_0001.MP4"
    if not _pan_video(path, frames=60):
        pytest.skip("no MP4 writer in this OpenCV")
    path.with_suffix(".SRT").write_text(SRT_NEW)
    doc = ppg.Document()
    ch = doc.add_chunk("video")
    doc.save(tmp_path / "v.ppg")
    cams = video.add_video(ch, path, overlap=0.6)
    assert cams and all(c.meta["video"] == "DJI_0001.MP4" for c in cams)
    refs = [c.reference.location for c in cams]
    assert all(r is not None for r in refs) and np.isclose(refs[0][0], 11.2, atol=1e-3)
    assert cams[0].meta["reference_crs"] == "EPSG:4326"
    assert cams[0].meta["gnss"]["source"] == "video" and cams[0].meta["gnss"]["height"] == "exif"
    assert cams[0].reference.accuracy == [3.0, 3.0, 5.0]


def test_interface_quality_and_video(processed, tmp_path, monkeypatch):
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
    blurred = ch.cameras[0]
    img = cv2.imread(blurred.path)
    cv2.imwrite(blurred.path, cv2.GaussianBlur(img, (0, 0), 3.0), [cv2.IMWRITE_JPEG_QUALITY, 95])
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.estimate_quality()
    w.runner.wait()
    assert all(c.meta.get("quality") is not None for c in w.chunk.cameras)
    item = next(w.photos.item(i) for i in range(w.photos.count())
                if w.photos.item(i).data(mw.Qt.ItemDataRole.UserRole) is blurred)
    assert "sfocata" in item.toolTip()
    w.disable_blurred()
    assert not blurred.enabled and sum(not c.enabled for c in w.chunk.cameras) == 1
    w.enable_photos([blurred], True)
    assert blurred.enabled
    # a video: the frames join the chunk
    path = tmp_path / "volo.mp4"
    if _pan_video(path, frames=60):
        n = len(w.chunk.cameras)
        monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), "")))
        monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {
            "mode": "auto", "overlap": 60.0, "every": 1.0, "max_frames": 0}))
        w.add_video()
        w.runner.wait()
        assert len(w.chunk.cameras) > n and (tmp_path / "volo_fotogrammi").is_dir()
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def test_command_line(tmp_path):
    """The commands call the engine with a plain progress function (print_progress)."""
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    rng = np.random.default_rng(3)
    (tmp_path / "foto").mkdir()
    for i in range(3):
        img = cv2.GaussianBlur((rng.random((300, 400, 3)) * 255).astype(np.uint8), (0, 0), 1.0 + 2.0 * (i == 2))
        cv2.imwrite(str(tmp_path / "foto" / f"a{i}.jpg"), img)
    doc = ppg.Document()
    ch = doc.add_chunk("c")
    doc.save(tmp_path / "p.ppg")
    ch.add_photos([tmp_path / "foto"])
    doc.save()
    run = CliRunner()
    r = run.invoke(app, ["quality", str(doc.path)])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert "a2" in r.output
    path = tmp_path / "volo.mp4"
    if _pan_video(path, frames=60):
        r = run.invoke(app, ["add-video", str(doc.path), str(path), "--every", "1"])
        assert r.exit_code == 0, r.output + repr(r.exception)
        assert len(ppg.Document.open(doc.path).chunk.cameras) == 5
