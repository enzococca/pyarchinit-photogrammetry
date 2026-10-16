"""Start of the interface: splash with progress, thumbnails in the background, progress of a
project being opened."""
from __future__ import annotations

import io
import os
import struct
import subprocess
import sys
import time

import numpy as np
import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _jpeg_with_exif_thumbnail(path, colour=(200, 30, 30)):
    """A photo whose EXIF holds a 160x90 thumbnail (IFD1), as drone cameras write."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (160, 90), colour).save(buf, "JPEG")
    thumb = buf.getvalue()
    ifd1 = 8 + 2 + 4  # empty IFD0, then IFD1
    data = 8 + 2 + 4 + 2 + 2 * 12 + 4
    tiff = (b"II*\x00" + struct.pack("<I", 8) + struct.pack("<HI", 0, ifd1)
            + struct.pack("<H", 2) + struct.pack("<HHII", 0x0201, 4, 1, data) + struct.pack("<HHII", 0x0202, 4, 1,
                                                                                            len(thumb))
            + struct.pack("<I", 0) + thumb)
    Image.new("RGB", (1200, 900), (20, 120, 200)).save(path, "JPEG", exif=b"Exif\x00\x00" + tiff)
    return path


def test_thumbnails_from_the_exif_or_decoded(tmp_path, qapp):
    from PIL import Image

    from pyarchinit_photogrammetry.gui.panels import read_thumbnail

    img = read_thumbnail(str(_jpeg_with_exif_thumbnail(tmp_path / "drone.jpg")))
    assert (img.width(), img.height()) == (160, 90) and img.pixelColor(80, 45).red() > 150  # the EXIF one, red
    Image.new("RGB", (1200, 900), (20, 120, 200)).save(tmp_path / "plain.jpg")
    img = read_thumbnail(str(tmp_path / "plain.jpg"))
    assert img.width() == 150 and img.pixelColor(75, 50).blue() > 150  # decoded at 1/8
    (tmp_path / "online_only.jpg").write_bytes(b"")
    assert read_thumbnail(str(tmp_path / "online_only.jpg")).isNull()


def test_photos_pane_loads_thumbnails_in_the_background(tmp_path, qapp):
    from types import SimpleNamespace

    from pyarchinit_photogrammetry.gui.panels import PhotosPane

    paths = [str(_jpeg_with_exif_thumbnail(tmp_path / f"p{i}.jpg")) for i in range(6)]
    (tmp_path / "empty.jpg").write_bytes(b"")
    cams = [SimpleNamespace(path=p, label=os.path.basename(p), aligned=i % 2 == 0)
            for i, p in enumerate(paths + [str(tmp_path / "empty.jpg")])]
    pane = PhotosPane()
    progress = []
    pane.thumbs_progress.connect(lambda done, total: progress.append((done, total)))
    pane.set_chunk(SimpleNamespace(cameras=cams))
    assert pane.count() == 7  # the items are there at once, the images come later
    deadline = time.time() + 20
    while (pane._loader is not None and pane._loader.isRunning() or len(progress) < 7) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert progress[-1] == (7, 7) and len(pane._cache) == 7
    assert "non leggibile" in pane.item(6).toolTip()
    progress.clear()
    pane.set_chunk(SimpleNamespace(cameras=cams))  # cached: nothing to read again
    assert pane._loader is None and not progress
    pane.stop()


def test_splash_steps(qapp):
    import ppg_launcher

    s = ppg_launcher.Splash()
    s.step(42, "Fotogrammetria (COLMAP)")
    assert s.bar.value() == 42 and s.bar.text() == "42%" and s.status.text() == "Fotogrammetria (COLMAP)"
    cb = s.progress(80, 100)
    cb(0.5, "Ortofoto")
    assert s.bar.value() == 90
    s.step(150)
    assert s.bar.value() == 100
    s.close()


def test_open_project_shows_its_progress(processed, qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    import ppg_launcher
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    doc, ch, _ = processed
    steps = []
    real = ppg_launcher.Splash

    def recording(*args, **kwargs):
        card = real(*args, **kwargs)
        original = card.step
        card.step = lambda percent, text=None: (steps.append((percent, text)), original(percent, text))
        return card

    monkeypatch.setattr(ppg_launcher, "Splash", recording)
    w = MainWindow(None, autoconnect=False)
    try:
        w.open_document(str(doc.path))
        values = [p for p, _ in steps]
        texts = [t for _, t in steps if t]
        assert values == sorted(values) and values[-1] == 100
        assert {"Lettura del progetto", "Vista 3D", "Ortofoto e DEM"} <= set(texts)
        assert w.doc.path == doc.path
    finally:
        w.photos.stop()
        w.setWindowModified(False)
        w.close()


def test_launcher_starts_the_window(tmp_path):
    """``ppg-gui``: the splash, the heavy imports one by one, the window (smoke test)."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PPG_RENDERER="software", PPG_LOG_DIR=str(tmp_path))
    code = "import sys, ppg_launcher; sys.exit(ppg_launcher.main(['--check']))"
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300,
                       close_fds=False)
    assert "GUI check: ok" in r.stdout, r.stdout[-2000:] + r.stderr[-2000:]
    log = (tmp_path / "ppg-gui.log").read_text()
    assert "Avvio in" in log
    assert np.isfinite(float(log.split("Avvio in ")[1].split(" s")[0]))
