"""Start of the interface: splash with progress, thumbnails in the background, progress of a
project being opened."""
from __future__ import annotations

import io
import os
import struct
import subprocess
import sys
import threading
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


def test_the_launcher_runs_a_script_without_the_window(tmp_path):
    """``ppg-gui --run script.py args`` (the packaged application scripted as Metashape with
    ``-r``): the script with its arguments, the program's modules, its exit code, no splash."""
    script = tmp_path / "script.py"
    script.write_text("import sys, pyarchinit_photogrammetry as ppg\n"
                      "open(sys.argv[1], 'w').write(ppg.__version__ + ' ' + ' '.join(sys.argv[2:]))\n"
                      "sys.exit(3)\n")
    out = tmp_path / "out.txt"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PPG_LOG_DIR=str(tmp_path))
    code = "import sys, ppg_launcher; sys.exit(ppg_launcher.main(sys.argv[1:]))"
    r = subprocess.run([sys.executable, "-c", code, "--run", str(script), str(out), "a", "b"], env=env,
                       capture_output=True, text=True, timeout=300, close_fds=False)
    import pyarchinit_photogrammetry as ppg

    assert r.returncode == 3 and out.read_text() == f"{ppg.__version__} a b", r.stdout[-2000:] + r.stderr[-2000:]
    bad = tmp_path / "bad.py"
    bad.write_text("raise ValueError('no')\n")
    r = subprocess.run([sys.executable, "-c", code, "--run", str(bad)], env=env, capture_output=True, text=True,
                       timeout=300, close_fds=False)
    assert r.returncode == 1 and "ValueError: no" in r.stderr


def test_the_crash_log_never_stops_the_start(tmp_path, monkeypatch):
    """faulthandler refused by the system (the macOS runners of GitHub refused its signal
    stack): the interface starts all the same, without the crash file."""
    import faulthandler
    import logging

    from pyarchinit_photogrammetry.gui import logging_setup

    def refuse(*_a, **_k):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setenv("PPG_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logging_setup, "_state", {})
    monkeypatch.setattr(faulthandler, "is_enabled", lambda: False)
    monkeypatch.setattr(faulthandler, "enable", refuse)
    # install() takes the exception hooks and Qt's messages for the application: given back
    # afterwards, or for the rest of the session Qt's warnings went to the logging captured by
    # pytest and never reached the CI log ("Timers cannot be stopped from another thread" among
    # them, the warning that came before the segmentation faults of 21-23 October 2026)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        path = logging_setup.install()
        assert path == tmp_path / "ppg-gui.log" and logging_setup._state["crash_file"] is None
        assert "crash.log not available" in path.read_text()
    finally:
        from PySide6.QtCore import qInstallMessageHandler

        qInstallMessageHandler(None)  # Qt's own handler: the messages on stderr again
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
    import ppg_launcher

    assert ppg_launcher.crash_log() is False and ppg_launcher._crash_file is None


def test_the_launcher_turns_the_crash_log_on_first(tmp_path):
    """The crash file is on before the heavy libraries load (native crashes while loading)."""
    env = dict(os.environ, PPG_LOG_DIR=str(tmp_path), PYTHONFAULTHANDLER="")
    env.pop("PYTHONFAULTHANDLER")
    code = ("import faulthandler, sys, ppg_launcher; ok = ppg_launcher.crash_log(); "
            "print(ok, faulthandler.is_enabled(), 'pyarchinit_photogrammetry' in sys.modules)")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120,
                       close_fds=False)
    assert r.stdout.split() == ["True", "True", "False"], r.stdout + r.stderr
    assert (tmp_path / "crash.log").exists()


def test_a_stalled_check_says_where_it_stopped(tmp_path):
    """The smoke test of a build (--check) that stalls writes where every thread stood to
    check-hang.txt in the log folder and ends (the Windows ARM64 build of 1.3.0 waited 600 s for a
    check that never finished, and said nothing)."""
    env = dict(os.environ, PPG_LOG_DIR=str(tmp_path), PPG_CHECK_TIMEOUT="1")
    code = ("import time, ppg_launcher\n"
            "assert ppg_launcher.check_watchdog()\n"
            "def stuck_here():\n"
            "    time.sleep(30)\n"
            "stuck_here()\n")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60,
                       close_fds=False)
    assert r.returncode != 0
    text = (tmp_path / "check-hang.txt").read_text()
    assert "stuck_here" in text and "Thread" in text


def test_numbers_stay_in_the_c_locale(qapp):
    """Qt takes the locale of the system: with an Italian one IfcOpenShell read "1.5" as 1 and
    the IFC preview was empty. The start puts the numbers back in the C locale."""
    import locale

    import ppg_launcher

    try:
        locale.setlocale(locale.LC_NUMERIC, "it_IT.UTF-8")
    except locale.Error:
        pytest.skip("no Italian locale on this system")
    assert locale.localeconv()["decimal_point"] == ","
    ppg_launcher.c_numbers()
    assert locale.localeconv()["decimal_point"] == "."
