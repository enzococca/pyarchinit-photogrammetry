"""Scripts run in the worker thread and undone as one step (*Annulla lo script*)."""
from __future__ import annotations

import hashlib
import shutil
import time

import pytest

from pyarchinit_photogrammetry.core.progress import Cancelled, Progress


def test_run_script_captures_output_and_errors():
    from pyarchinit_photogrammetry.assistant.scripts import run_script

    messages = []
    p = Progress(lambda f, m: messages.append(m))
    r = run_script("print('primo')\nprint('secondo')\nraise ValueError('sbagliato')", {}, p, name="prova.py")
    assert "primo\nsecondo" in r.output  # what was printed before the error is kept
    assert r.error and "sbagliato" in r.error and "ValueError" in r.error
    assert "Traceback" in r.traceback and "prova.py" in r.traceback
    assert "primo" in messages and "secondo" in messages  # every line also to the progress
    assert r.elapsed >= 0 and r.external == []
    ok = run_script("x = 2 + 2\nprint(x)", {}, p)
    assert ok.error is None and ok.traceback is None and ok.output.strip() == "4"
    # a script calling exit() never quits the program
    gone = run_script("import sys\nprint('prima')\nsys.exit(3)", {}, p)
    assert gone.error and "3" in gone.error and "prima" in gone.output
    assert run_script("exit()", {}, p).error is None  # exit() without a code: a script that ended
    # the reporter and the check of the cancel button are in the namespace
    ns: dict = {}
    run_script("seen = (progress, cancel())", ns, p)
    assert ns["seen"][0] is p and ns["seen"][1] is False
    p.cancel()
    with pytest.raises(Cancelled):
        run_script("cancel()", {}, p)


def test_external_calls_are_recorded_not_blocked(monkeypatch):
    from pyarchinit_photogrammetry.assistant.scripts import ExternalCalls, run_script
    from pyarchinit_photogrammetry.pyarchinit.bridge import PyArchInit

    done = []

    def publish_drawings(self, *args, **kwargs):
        done.append(args)
        return 7

    monkeypatch.setattr(PyArchInit, "publish_drawings", publish_drawings, raising=False)
    pa = PyArchInit.__new__(PyArchInit)
    with ExternalCalls() as calls:
        assert pa.publish_drawings("US 1") == 7
    assert done == [("US 1",)] and calls.calls == ["PyArchInit.publish_drawings(...)"]
    assert PyArchInit.publish_drawings is publish_drawings  # put back as it was
    r = run_script("n = pa.publish_drawings('US 2')\nprint(n)", {"pa": pa}, Progress())
    assert r.error is None and r.output.strip() == "7" and done[-1] == ("US 2",)
    assert r.external == ["PyArchInit.publish_drawings(...)"]


# ---------------------------------------------------------------- in the window


@pytest.fixture
def window(processed, tmp_path):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import pyarchinit_photogrammetry as ppg

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    w = mw.MainWindow(ppg.Document.open(d / doc.path.name), autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    shown = []
    w._script_message = lambda text: shown.append(text)  # the non-native message of the external calls
    w.shown_messages = shown
    yield w, app
    w.runner.wait()
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def _run(w, app, source, label="Script"):
    results = []
    assert w.run_script_source(source, label, on_done=results.append)
    w.runner.wait()
    app.processEvents()
    assert results, "the script did not end"
    return results[0]


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_script_meta_change_is_undone_as_one_step(window):
    w, app = window
    ch = w.chunk
    n_markers = len(ch.markers)
    entries = len(w.history.entries)
    r = _run(w, app, "chunk.meta['note'] = 1\nchunk.add_marker('nuovo')\nprint('fatto')")
    assert r.error is None and r.output.strip() == "fatto"
    assert len(w.history.entries) == entries + 1 and w.history.undo_text() == "Script"
    assert w.chunk.meta.get("note") == 1 and len(w.chunk.markers) == n_markers + 1
    assert w.history.undo() == "Script"
    assert "note" not in w.chunk.meta and len(w.chunk.markers) == n_markers
    assert w.history.redo() == "Script"
    assert w.chunk.meta.get("note") == 1 and len(w.chunk.markers) == n_markers + 1
    assert w.chunk.markers[-1].label == "nuovo"


def test_script_that_rebuilds_the_dem_keeps_the_previous_one(window):
    from pyarchinit_photogrammetry.core import versions

    w, app = window
    ch = w.chunk
    before = _digest(ch.dem_path)
    kept = len(versions.versions(ch, "dem"))
    r = _run(w, app, "chunk.build_dem(resolution=0.02)")
    assert r.error is None
    ch = w.chunk
    assert len(versions.versions(ch, "dem")) == kept + 1  # the previous DEM among the versions
    after = _digest(ch.dem_path)
    assert after != before and not ch.keep_versions
    w.history.undo()
    ch = w.chunk
    assert _digest(ch.dem_path) == before and len(versions.versions(ch, "dem")) == kept
    w.history.redo()
    ch = w.chunk
    assert _digest(ch.dem_path) == after and len(versions.versions(ch, "dem")) == kept + 1
    w.history.undo()
    assert _digest(w.chunk.dem_path) == before


def test_undo_after_a_script_that_failed_halfway(window):
    w, app = window
    ch = w.chunk
    before = _digest(ch.dem_path)
    r = _run(w, app, "chunk.build_dem(resolution=0.02)\nchunk.meta['note'] = 2\nraise RuntimeError('a metà')")
    assert r.error and "a metà" in r.error
    assert _digest(w.chunk.dem_path) != before and w.history.undo_text() == "Script"
    w.history.undo()
    assert _digest(w.chunk.dem_path) == before and "note" not in w.chunk.meta
    assert not w.chunk.keep_versions


def test_new_chunk_and_new_files_are_removed_by_undo(window):
    w, app = window
    doc = w.doc
    n = len(doc.chunks)
    r = _run(w, app, "dup = doc.duplicate_chunk(chunk, label='Copia')\n"
                     "(chunk.data_dir / 'appunti.txt').write_text('ciao')\n")
    assert r.error is None
    assert len(doc.chunks) == n + 1
    new = doc.chunks[-1]
    folder = new.data_dir
    note = w.chunk.data_dir / "appunti.txt"
    assert folder.exists() and note.exists()
    w.history.undo()
    assert len(doc.chunks) == n and not note.exists() and not folder.exists()
    w.history.redo()
    assert len(doc.chunks) == n + 1 and doc.chunks[-1].label == "Copia" and note.read_text() == "ciao"
    assert (doc.files_dir / folder.name).exists()


def test_run_script_menu_uses_the_worker(window, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.gui import main_window as mw

    w, app = window
    script = tmp_path / "lento.py"
    script.write_text("import time\ntime.sleep(0.5)\nchunk.meta['lento'] = True\n", encoding="utf-8")
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(script), "")))
    w.run_script()
    assert w.runner.busy  # the script runs in the worker: the window is back at once
    t0 = time.monotonic()
    seen = False
    while w.runner.busy and time.monotonic() - t0 < 30:
        app.processEvents()
        seen = seen or w.isEnabled()
        time.sleep(0.01)
    assert seen and not w.runner.busy
    app.processEvents()
    assert w.chunk.meta.get("lento") is True and w.history.undo_text() == "lento.py"


def test_busy_runner_refuses_a_script(window):
    w, app = window
    assert w.run_script_source("import time\ntime.sleep(0.3)")
    assert w.runner.busy
    assert not w.run_script_source("chunk.meta['mai'] = 1")
    w.runner.wait()
    app.processEvents()
    assert "mai" not in w.chunk.meta


def test_external_calls_are_said_after_the_script(window, monkeypatch):
    from pyarchinit_photogrammetry.pyarchinit.bridge import PyArchInit

    w, app = window
    monkeypatch.setattr(PyArchInit, "publish_drawings", lambda self, *a, **k: 1, raising=False)
    r = _run(w, app, "window.log('ciao')\nfrom pyarchinit_photogrammetry.pyarchinit.bridge import PyArchInit\n"
                     "PyArchInit.__new__(PyArchInit).publish_drawings()", label="Pubblica")
    assert r.error is None and r.external == ["PyArchInit.publish_drawings(...)"]
    assert w.shown_messages and "Non annullabili" in w.shown_messages[-1]
    assert "PyArchInit.publish_drawings" in w.shown_messages[-1]
