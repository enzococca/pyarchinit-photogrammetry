"""Work shared with other computers through the project folder: a node (a thread here, another
computer there) takes units of key points and of depth maps and the main program takes the rest;
the results are the same as alone. Paths mapped between computers, stale units given back."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.engine import nodes


def _project(scene_dir, tmp_path):
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(tmp_path / "scavo.ppg")
    ch.add_photos([scene_dir / "photos"])
    ch.detect_markers("aruco_4x4_50")
    return doc, ch


def _node(files_dir, stop):
    """A node on the same folder, as another computer would run it (the paths need no mapping)."""
    return threading.Thread(target=nodes.serve, args=(files_dir,), kwargs={"stop_event": stop, "idle": 0.2, "log": lambda *a: None},
                            daemon=True)


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
        node.join(10)
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
    monkeypatch.delenv("PPG_NODES", raising=False)
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
