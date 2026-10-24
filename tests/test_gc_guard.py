"""Python's garbage collector runs in the GUI thread only while the interface is open
(gui/gc_guard.py): the crash of the CI on 21-23 October 2026, reproduced in a child process."""
from __future__ import annotations

import gc
import os
import subprocess
import sys
import threading

import pytest

pytest.importorskip("PySide6")


def _allocate_in_a_thread(n: int = 300_000) -> None:
    """What a worker thread does while it computes: containers, kept until the end."""
    def work():
        keep = [[i] for i in range(n)]
        del keep

    t = threading.Thread(target=work, name="worker")
    t.start()
    t.join()


def test_without_the_guard_a_worker_thread_runs_the_collections():
    """Why the guard is needed: Python collects in the thread whose allocation crosses the
    threshold, the worker thread here."""
    where = []

    def seen(phase, info):
        if phase == "start":
            where.append(threading.current_thread().name)

    assert gc.isenabled()
    gc.callbacks.append(seen)
    try:
        _allocate_in_a_thread()
    finally:
        gc.callbacks.remove(seen)
    assert "worker" in where


_SCENARIO = r'''
import gc, os, sys, tempfile, threading, time, weakref
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])
from typer.testing import CliRunner

from pyarchinit_photogrammetry.cli import app as cli_app
from pyarchinit_photogrammetry.gui.gc_guard import install
from pyarchinit_photogrammetry.gui.image_views import RasterView

path = Path(sys.argv[1]) / "orto.tif"
rgb = (np.random.default_rng(0).random((3, 200, 300)) * 255).astype(np.uint8)
with rasterio.open(path, "w", driver="GTiff", width=300, height=200, count=3, dtype="uint8",
                   transform=from_origin(0, 10, 0.05, 0.05), BIGTIFF="IF_SAFER") as dst:
    dst.write(rgb)

collector = install(app, interval_ms=10, full_every_s=0)
assert not gc.isenabled()
where = []
gc.callbacks.append(lambda phase, info: where.append(threading.current_thread().name) if phase == "start" else None)


def the_test():
    # as tests/test_relief.py::test_chunk_cli_and_view: the traceback of click's SystemExit in the
    # result leads back to this frame, a reference cycle that keeps the view after the return
    r = CliRunner().invoke(cli_app, ["--help"])
    assert r.exit_code == 0
    view = RasterView()
    view.load(path, "ortho")  # its detail timer and the index timer of its scene start
    return weakref.ref(view)


ref = the_test()
assert ref() is not None


def worker():  # a worker thread of the engine computing: without the guard it ran the collections
    keep = [[i] for i in range(600_000)]
    time.sleep(0.05)
    del keep


t = threading.Thread(target=worker, name="worker")
t.start()
t.join()
print("collections in the worker:", where.count("worker"), flush=True)
time.sleep(0.3)  # the timers of the view are due
deadline = time.monotonic() + 30
while ref() is not None and time.monotonic() < deadline:
    app.processEvents()  # without the guard: segmentation fault in QTimerInfoList::activateTimers
    time.sleep(0.01)
app.processEvents()
print("view freed:", ref() is None, "| collections in the GUI thread:", where.count("MainThread"), flush=True)
collector.stop()
assert gc.isenabled()
print("SCENARIO_OK", flush=True)
os._exit(0)
'''


def test_qt_objects_left_to_the_collector_die_in_the_gui_thread(tmp_path):
    """The crash of the CI: a RasterView left in a reference cycle (by the frame of a test that
    keeps a CliRunner result) was freed by a collection that a worker thread started; Qt could
    not stop its timers from there and the next ``processEvents`` of the GUI thread handed a
    timer event to the deleted view (segmentation fault in QTimerInfoList::activateTimers /
    QCoreApplication::notifyInternal2). Without the guard this script dies with SIGSEGV after
    "QObject::~QObject: Timers cannot be stopped from another thread"; with it no collection runs
    in the worker and the view is freed by the GUI thread."""
    script = tmp_path / "scenario.py"
    script.write_text(_SCENARIO)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PPG_RENDERER="software")
    r = subprocess.run([sys.executable, str(script), str(tmp_path)], env=env, capture_output=True, text=True,
                       timeout=300, close_fds=False)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and "SCENARIO_OK" in r.stdout, (r.returncode, out[-3000:])
    assert "collections in the worker: 0" in r.stdout
    assert "view freed: True" in r.stdout
    assert "Timers cannot be stopped from another thread" not in out


def test_the_collector_follows_the_thresholds():
    """The generation a tick collects: none up to the first threshold, the youngest above it,
    the older ones when their counts are due (the oldest only when a full collection is
    allowed); the automatic collection stays off while installed and comes back with ``stop``."""
    from PySide6.QtWidgets import QApplication

    from pyarchinit_photogrammetry.gui.gc_guard import due_generation, install

    t = (10, 2, 2)
    assert due_generation((5, 9, 9), t) is None
    assert due_generation((11, 0, 0), t) == 0
    assert due_generation((11, 3, 0), t) == 1
    assert due_generation((11, 3, 3), t, full_allowed=False) == 1
    assert due_generation((11, 3, 3), t) == 2
    assert due_generation((10**6, 3, 3), (0, 2, 2)) is None  # threshold 0: collection turned off
    app = QApplication.instance() or QApplication([])
    collector = install(app, interval_ms=60_000)
    try:
        assert not gc.isenabled()
        gc.enable()  # something turned it on again: the next tick turns it off
        collector.collect()
        assert not gc.isenabled()
    finally:
        collector.stop()
        collector.deleteLater()
    assert gc.isenabled()
