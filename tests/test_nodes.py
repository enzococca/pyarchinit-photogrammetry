"""Work shared with other computers through the project folder: a node (a thread here, another
computer there) takes units of key points, depth maps, orthomosaic tiles and texture cells and the
main program takes the rest; the results are the same as alone. Paths mapped between computers,
stale units given back, units failed on a node done again here, a cancelled step taken back."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.core.camera import Camera
from pyarchinit_photogrammetry.core.progress import Cancelled, Progress
from pyarchinit_photogrammetry.engine import nodes


def _project(scene_dir, tmp_path):
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(tmp_path / "scavo.ppg")
    ch.add_photos([scene_dir / "photos"])
    ch.detect_markers("aruco_4x4_50")
    return doc, ch


def _node(files_dir, stop, host=None):
    """A node on the same folder, as another computer would run it (the paths need no mapping);
    ``host``: its own name, so that its units count as done by another computer."""
    return threading.Thread(target=nodes.serve, args=(files_dir,), daemon=True,
                            kwargs={"stop_event": stop, "idle": 0.2, "log": lambda *a: None, "host": host})


def _copy(processed, tmp_path, name="copia"):
    doc, _ch, _ = processed
    d = tmp_path / name
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def _left(files_dir):
    root = files_dir / "nodes"
    return sorted(p.name for state in ("todo", "taken", "done", "failed", "state") for p in (root / state).iterdir())


def test_units_taken_once_and_stale_ones_given_back(tmp_path):
    root = nodes.folder(tmp_path)
    names = nodes.put(tmp_path, [{"kind": "features", "names": ["a"]}, {"kind": "depth_maps", "image_ids": [1]}])
    assert len(names) == 2 and all((root / "todo" / f"{n}.json").exists() for n in names)
    got = nodes.take(root, "one")
    assert got is not None and got[0] == names[0] and got[1]["kind"] == "features"
    assert nodes.take(root, "two")[0] == names[1] and nodes.take(root, "three") is None  # nothing left
    nodes.finish(root, names[0], "one", {"ok": 1})
    assert nodes.outcome(root, names[0])["result"] == {"ok": 1} and nodes.outcome(root, names[1]) is None
    taken = root / "taken" / f"{names[1]}@two.json"
    old = time.time() - 2 * nodes.STALE
    os.utime(taken, (old, old))
    assert nodes.requeue_stale(root) == 1 and (root / "todo" / f"{names[1]}.json").exists()
    nodes.finish(root, names[1], "two", error="boom")
    assert nodes.outcome(root, names[1])["error"] == "boom"
    nodes.forget(tmp_path, names)
    assert not list((root / "done").iterdir()) and not list((root / "failed").iterdir())
    # the paths of the main program as a node sees them
    nodes.write_master(tmp_path, tmp_path / "scavo.ppg")
    mapping = nodes.mapping_for(nodes.folder(tmp_path), ["/Users/x/foto=/mnt/foto"])
    assert nodes.map_path("/Users/x/foto/IMG_1.JPG", mapping) == "/mnt/foto/IMG_1.JPG"
    assert nodes.map_path(str(tmp_path / "a.txt"), mapping) == str(tmp_path / "a.txt")  # the same folder here
    assert nodes.map_path("/elsewhere/b", mapping) == "/elsewhere/b" and nodes.map_path(None, mapping) is None
    assert nodes.present(tmp_path) == []


def test_key_points_and_depth_maps_shared_with_a_node(scene_dir, scene, tmp_path, monkeypatch):
    monkeypatch.setenv("PPG_NODES", "1")
    doc, ch = _project(scene_dir, tmp_path)
    files_dir = doc.files_dir
    stop = threading.Event()
    node = _node(files_dir, stop)
    node.start()
    try:
        for _ in range(50):  # the hello of the node
            if nodes.present(files_dir):
                break
            time.sleep(0.1)
        assert nodes.present(files_dir) and nodes.wanted(files_dir)
        seen = []
        ch.match_photos(quality="high", gpu="off", progress=lambda f, m: seen.append(m))
        info = ch.meta["sfm"]
        assert info["nodes"] and any("units of work shared" in m for m in seen)
        assert ch.align_cameras(mapper="incremental", use_gps=False) == len(ch.cameras)
        ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
        assert ch.update_transform()["control_rmse"] < 0.002  # the key points of the nodes are COLMAP's
        doc.save()
        if _native.kernel("patchmatch_depth") is None:  # the nodes share PatchMatch depth maps: native core only
            return
        ch.build_depth_maps(quality="high", backend="patchmatch", gpu="off")
        assert ch.meta["dense"].get("nodes")
        ws = ch.data_dir / "dense"
        maps = list((ws / "stereo" / "depth_maps").glob("*.bin"))
        assert len(maps) >= 0.8 * len(ch.cameras)
        assert not list((files_dir / "nodes" / "todo").iterdir()) and not list((files_dir / "nodes" / "done").iterdir())
    finally:
        stop.set()
        # the node leaves (and takes its hello away) once the unit it is working on is done: on a slow
        # computer that is more than the 10 s this waited, and the node was still there below
        node.join(120)
    assert not node.is_alive() and not nodes.present(files_dir)
    # the depth maps are as good as alone
    ch.build_point_cloud()
    ch.build_dem()
    import rasterio

    with rasterio.open(ch.dem_path) as src:
        dem = src.read(1)
        rows, cols = np.nonzero(dem != src.nodata)
        sel = np.random.default_rng(1).choice(len(rows), 3000, replace=False)
        xs, ys = rasterio.transform.xy(src.transform, rows[sel], cols[sel])
    err = dem[rows[sel], cols[sel]] - scene.world_height(np.array(xs), np.array(ys))
    assert abs(np.median(err)) < 0.001 and np.percentile(np.abs(err), 90) < 0.004
    # without a node present nothing is shared, and the switch turns it off
    ch.match_photos(quality="high", gpu="off")
    assert ch.meta["sfm"]["nodes"] == []
    monkeypatch.setenv("PPG_NODES", "0")
    assert not nodes.enabled() and nodes.wanted(files_dir) == []


def test_a_failing_unit_stops_the_step(tmp_path, monkeypatch):
    def broken(payload, mapping, gpu=None, out_dir=None):
        raise RuntimeError("no photos here")

    monkeypatch.setattr(nodes, "process", broken)
    with pytest.raises(RuntimeError, match="no photos here"):
        nodes.run(tmp_path, [{"kind": "features", "names": ["x"]}], project_path=None)


def test_a_unit_taken_late_is_not_stale_and_a_unit_failed_elsewhere_is_done_here(tmp_path):
    """A unit that waited in todo longer than STALE is not given back as soon as it is taken; a unit
    another computer could not do is done by the main program, which reports it."""
    root = nodes.folder(tmp_path)
    (name,) = nodes.put(tmp_path, [{"kind": "x"}])
    old = time.time() - 2 * nodes.STALE
    os.utime(root / "todo" / f"{name}.json", (old, old))
    assert nodes.take(root, "uno")[0] == name and nodes.requeue_stale(root) == 0
    # another computer that fails every unit it takes
    stop = threading.Event()

    def failing():
        while not stop.is_set():
            got = nodes.take(root, "altro-1")
            if got is None:
                stop.wait(0.02)
                continue
            nodes.finish(root, got[0], "altro-1", error="RuntimeError: no photos here")

    other = threading.Thread(target=failing, daemon=True)
    other.start()
    seen = []

    def here(payload, quiet):
        time.sleep(0.2)  # the other computer takes some units meanwhile
        return {"n": payload["n"]}

    try:
        out = nodes.run(tmp_path, [{"kind": "x", "n": i} for i in range(6)], lambda f, m: seen.append(m), local=here)
    finally:
        stop.set()
        other.join(10)
    assert [o["result"]["n"] for o in out] == list(range(6)) and {o["by"] for o in out} == {nodes.host_name()}
    assert any(m.startswith("A unit failed on altro-1 (RuntimeError: no photos here): done here") for m in seen)
    assert _left(tmp_path) == sorted(p.name for p in (root / "done").iterdir())  # only the results, for the caller


def test_a_cancelled_step_takes_its_units_back(tmp_path):
    """Cancelled: the unit being done here stops at once and nobody takes the others; what an earlier
    step left behind is cleared when a step starts."""
    root = nodes.folder(tmp_path)
    nodes.put(tmp_path, [{"kind": "x"}])  # of a step the program never finished
    (root / "done" / "vecchio.json").write_text("{}")
    progress = Progress()
    started = threading.Event()

    def here(payload, quiet):
        started.set()
        while True:  # a long unit: only the end of the step stops it
            quiet.check()
            time.sleep(0.02)

    threading.Thread(target=lambda: started.wait(10) and progress.cancel(), daemon=True).start()
    t0 = time.time()
    with pytest.raises(Cancelled):
        nodes.run(tmp_path, [{"kind": "x"} for _ in range(4)], progress, local=here)
    assert time.time() - t0 < 10 and _left(tmp_path) == []


def test_the_chunk_of_a_step_as_a_node_sees_it(tmp_path):
    """The chunk as the main program has it in memory (not saved) reaches a node that sees the
    project folder elsewhere: the photos inside it follow the folder, the others take the prefixes
    given to the node; the state goes away with the step, and old ones with the next step."""
    main = tmp_path / "principale"
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(main / "scavo.ppg")
    ch.cameras = [Camera(1, "a", str(main / "foto" / "a.jpg"), 0), Camera(2, "b", "/Users/x/foto/b.jpg", 0)]
    ch.transform = np.diag([2.0, 2.0, 2.0, 1.0])
    ch.meta["colour"] = {"gains": {"1": [1.0, 1.1, 0.9]}}  # measured by the step, never saved
    old = nodes.share_chunk(doc.files_dir, ch)
    past = time.time() - 2 * nodes.STATE_KEPT
    os.utime(nodes.folder(doc.files_dir) / "state" / f"{old}.json", (past, past))
    state = nodes.share_chunk(doc.files_dir, ch, ortho={"tile": 512})
    np.save(nodes.state_file(nodes.folder(doc.files_dir), state, "faces.npy"), np.arange(3))
    assert not (nodes.folder(doc.files_dir) / "state" / f"{old}.json").exists()
    nodes.write_master(doc.files_dir, doc.path)
    there = tmp_path / "altro" / "progetti"
    shutil.copytree(main, there)  # the shared disk as the node sees it
    root = nodes.folder(there / "scavo.ppg.files")
    got, data = nodes.node_chunk(root, state, nodes.mapping_for(root, ["/Users/x/foto=/mnt/foto"]))
    assert [c.path for c in got.cameras] == [str((there / "foto" / "a.jpg").resolve()), "/mnt/foto/b.jpg"]
    assert np.allclose(got.transform, ch.transform) and got.meta["colour"] == ch.meta["colour"]
    assert got.data_dir == (there / "scavo.ppg.files").resolve() / f"chunk_{ch.key}" and data["ortho"] == {"tile": 512}
    nodes.forget_state(doc.files_dir, state)
    assert not list((nodes.folder(doc.files_dir) / "state").iterdir())


def test_a_node_reads_where_the_main_program_is_at_every_unit(tmp_path, monkeypatch):
    """A node started before the main program wrote ``master.json`` maps the paths of the units
    that come after it."""
    seen = []
    monkeypatch.setattr(nodes, "process", lambda payload, mapping, gpu=None, out_dir=None: seen.append(mapping) or {})
    stop = threading.Event()
    node = _node(tmp_path, stop, host="altro-1")
    node.start()
    try:
        nodes.write_master(tmp_path, tmp_path / "scavo.ppg")
        nodes.put(tmp_path, [{"kind": "x"}])
        for _ in range(100):
            if seen:
                break
            time.sleep(0.05)
    finally:
        stop.set()
        node.join(10)
    assert seen and (str(tmp_path.resolve()), str(tmp_path.resolve())) in seen[0]


def test_orthomosaic_and_texture_shared_with_a_node(processed, tmp_path, monkeypatch):
    """The tiles of the orthomosaic and of the orthophoto of a terrain texture, and the cells of the
    texture: a node takes units, the main program the rest, and the files are the same as alone -
    also when every unit fails on the node, the main program doing them again."""
    import cv2
    import rasterio
    from threadpoolctl import threadpool_limits

    from pyarchinit_photogrammetry.engine import ortho, texturing
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    monkeypatch.setenv("PPG_NODES", "1")
    monkeypatch.setattr(nodes, "ORTHO_UNIT_TILES", 2)  # a small scene: many small units
    monkeypatch.setattr(nodes, "TEXTURE_UNIT_TEXELS", 100_000)
    doc, ch = _copy(processed, tmp_path)
    files_dir = doc.files_dir
    options = {"resolution": 0.004, "tile": 128, "colour_correction": True, "fill_holes": True}

    def orthomosaic(name, progress=None):
        path = ortho.build_orthomosaic(ch, out_path=tmp_path / name, progress=progress, **options)
        with rasterio.open(path) as src:
            return src.read()

    def texture(mapping, progress=None):
        texturing.build_texture(ch, texture_size=1024, mapping=mapping, progress=progress)
        pages = [cv2.imread(str(p)) for p in texturing.texture_pages(ch)]
        return pages, read_mesh(ch.product_path("model")).texcoords

    def same_texture(a, b):
        return len(a[0]) == len(b[0]) and all(np.array_equal(x, y) for x, y in zip(a[0], b[0])) and \
            np.array_equal(a[1], b[1])

    # one BLAS thread for the whole test: the node is a thread of this process, and the first of two
    # steps leaving its one-thread section would give the other more threads (other rounding)
    with threadpool_limits(limits=1, user_api="blas"):
        alone = orthomosaic("alone.tif")  # measures the colour correction: the node gets it with the chunk
        generic_alone, plan_alone = texture("generic"), texture("orthophoto")
        stop = threading.Event()
        node = _node(files_dir, stop, host="altro-1")
        node.start()
        try:
            for _ in range(50):
                if nodes.present(files_dir):
                    break
                time.sleep(0.1)
            seen = []
            assert np.array_equal(orthomosaic("shared.tif", lambda f, m: seen.append(m)), alone)
            assert any("units of work shared" in m for m in seen) and any("by other computers" in m for m in seen)
            seen.clear()
            assert same_texture(texture("generic", lambda f, m: seen.append(m)), generic_alone)
            assert any(m.startswith("Texture: ") and "units of work shared" in m for m in seen)
            assert any("units of texture cells (" in m and "by other computers" in m for m in seen)
            seen.clear()
            assert same_texture(texture("orthophoto", lambda f, m: seen.append(m)), plan_alone)
            assert any("units of tiles (" in m and "by other computers" in m for m in seen)

            def broken(payload, mapping, out_dir):
                raise RuntimeError("no photos here")

            monkeypatch.setattr(ortho, "node_tiles", broken)
            seen.clear()
            assert np.array_equal(orthomosaic("again.tif", lambda f, m: seen.append(m)), alone)
            assert any("A unit failed on altro-1" in m and "done here" in m for m in seen)
            assert _left(files_dir) == []
        finally:
            stop.set()
            node.join(120)
    assert not node.is_alive()


def test_node_command_leaves_when_nothing_is_left(tmp_path):
    env = dict(os.environ, PPG_NO_PLUGINS="1")
    out = subprocess.run([sys.executable, "-m", "pyarchinit_photogrammetry.cli", "node", str(tmp_path), "--once"],
                         capture_output=True, text=True, env=env, timeout=120, close_fds=False)
    assert out.returncode == 0 and "0 units done" in out.stdout, out.stdout + out.stderr
    assert (tmp_path / "nodes" / "todo").is_dir()
    dry = subprocess.run([sys.executable, "-m", "pyarchinit_photogrammetry.cli", "remote", "user@host:/data",
                          str(tmp_path / "missing.ppg"), "--dry-run"], capture_output=True, text=True, env=env,
                         timeout=120, close_fds=False)
    assert dry.returncode != 0 and "does not exist" in (dry.stdout + dry.stderr)
    (tmp_path / "p.ppg").write_text("{}")
    (tmp_path / "p.ppg.files").mkdir()
    dry = subprocess.run([sys.executable, "-m", "pyarchinit_photogrammetry.cli", "remote", "user@host:/data",
                          str(tmp_path / "p.ppg"), "--photos", str(tmp_path), "--dry-run"], capture_output=True,
                         text=True, env=env, timeout=120, close_fds=False)
    assert dry.returncode == 0 and "ppg node /data/p.ppg.files --once" in dry.stdout and "--map" in dry.stdout, dry.stdout + dry.stderr
    assert json.loads((tmp_path / "p.ppg").read_text()) == {}


def test_dialog_switch_drives_the_engine(tmp_path, monkeypatch):
    """The switch of the dialog sets ``PPG_NODES`` (the engine reads only that); at start the
    stored choice applies unless the environment was set by hand."""
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from pyarchinit_photogrammetry.gui import nodes_dialog

    QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / "test.ini"), QSettings.Format.IniFormat)
    monkeypatch.setenv("PPG_NODES", "1")  # recorded: the variable comes back as it was after the test
    monkeypatch.delenv("PPG_NODES")
    settings.setValue("nodes/enabled", "false")
    assert nodes_dialog.apply_setting(settings) is False and os.environ["PPG_NODES"] == "0" and not nodes.enabled()
    monkeypatch.setenv("PPG_NODES", "1")
    assert nodes_dialog.apply_setting(settings) is True  # set by hand: kept
    d = nodes_dialog.NodesDialog(tmp_path, settings)
    assert not d.use.isChecked()
    d.use.setChecked(True)
    assert os.environ["PPG_NODES"] == "1" and settings.value("nodes/enabled") == "true" and nodes.enabled()
    d.use.setChecked(False)
    assert os.environ["PPG_NODES"] == "0" and not nodes.enabled()
    d.close()


def test_processing_started_from_the_window_follows_its_switch(tmp_path, monkeypatch):
    """The choice of *Strumenti > Nodi di rete* decides for what the window runs: a step in the
    background asks the engine which nodes to share the work with (as the key points and the depth
    maps do) and gets the node present only while the switch is on - the stored choice when the
    window opens, the dialog's as it changes; ``PPG_NODES`` set by hand before the start is kept."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import pyarchinit_photogrammetry.gui as gui
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.gui import nodes_dialog

    app = QApplication.instance() or QApplication([])
    path = tmp_path / "preferenze.ini"
    monkeypatch.setattr(gui, "app_settings", lambda: QSettings(str(path), QSettings.Format.IniFormat))
    stored = gui.app_settings()
    stored.setValue("nodes/enabled", "false")
    stored.sync()
    monkeypatch.setenv("PPG_NODES", "1")  # recorded: the variable comes back as it was after the test
    monkeypatch.delenv("PPG_NODES")
    doc = ppg.Document()
    doc.add_chunk("Saggio")
    doc.save(tmp_path / "scavo.ppg")
    files_dir = doc.files_dir
    (nodes.folder(files_dir) / "hello" / "altro-1.json").write_text(json.dumps(
        {"host": "altro-1", "machine": "altro", "cores": 8, "gpus": [], "time": time.time()}))
    seen = []

    def step(w):
        w.run_task("Nodi", lambda p: [n["host"] for n in nodes.wanted(files_dir)], seen.append, refresh=False,
                   save=False)
        w.runner.wait()
        app.processEvents()
        return seen.pop()

    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    try:
        assert step(w) == []  # the stored choice: the work stays here
        d = nodes_dialog.NodesDialog(files_dir, w.settings, w)
        assert not d.use.isChecked()
        d.use.setChecked(True)
        assert step(w) == ["altro-1"]
        d.use.setChecked(False)
        assert step(w) == []
        d.close()
    finally:
        w.setWindowModified(False)
        w.close()
    monkeypatch.setenv("PPG_NODES", "1")  # by hand, before the window opens
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    try:
        assert step(w) == ["altro-1"] and w.settings.value("nodes/enabled") == "false"
    finally:
        w.setWindowModified(False)
        w.close()
