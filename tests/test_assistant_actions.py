"""The assistant's actions on the open project (``ppg.actions``): headless, from a script and in the window."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import cv2
import numpy as np
import pytest

from pyarchinit_photogrammetry.engine.dem import write_geotiff

RES = 0.005  # a 2 x 1.5 m wall face, 5 mm pixels


class WallSegmenter:
    """SAM-like masks of a wall face: every stone and the mortar around them (as tests/test_stones.py)."""

    def segment_auto(self, image, **options):
        dark = (image[..., 0] < 100).astype(np.uint8)
        n, lab = cv2.connectedComponents(dark)
        return [lab == i for i in range(1, n)] + [dark == 0]


def _wall_image():
    rng = np.random.default_rng(3)
    img = np.full((300, 400, 3), 190, np.uint8)
    n = 0
    for r in range(40, 300, 70):
        for c in range(45, 400, 85):
            a, b = rng.integers(22, 34), rng.integers(16, 26)
            cv2.ellipse(img, (int(c), int(r)), (int(a), int(b)), float(rng.integers(0, 30)), 0, 360, (70, 60, 55), -1)
            n += 1
    return img, n


@pytest.fixture
def wall(tmp_path, monkeypatch):
    """A project whose orthomosaic is a stone wall, with a DEM beside it; SAM is the wall segmenter."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.ai import sam_env

    monkeypatch.setattr(sam_env, "make_segmenter", lambda model="small": WallSegmenter())
    img, n = _wall_image()
    doc = ppg.Document()
    ch = doc.add_chunk("Muro")
    doc.save(tmp_path / "muro.ppg")
    ch.data_dir.mkdir(parents=True, exist_ok=True)
    ortho = write_geotiff(ch.data_dir / "orthomosaic.tif", np.moveaxis(img, -1, 0), (0.0, 1.5), RES, None)
    dem = write_geotiff(ch.data_dir / "dem.tif", np.full((300, 400), 10.0, np.float32), (0.0, 1.5), RES, None,
                        nodata=-9999.0)
    ch._set_product("orthomosaic", ortho)
    ch._set_product("dem", dem)
    doc.save()
    return doc, ch, n


@pytest.fixture
def project(processed, tmp_path):
    """A copy of the processed demo project (photos, cloud, model, DEM, orthomosaic)."""
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    copy = ppg.Document.open(d / doc.path.name)
    return copy, copy.chunks[0]


def _qt(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    return app, mw


def _window(doc, tmp_path):
    app, mw = _qt(tmp_path)
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w._script_message = lambda text: None
    w.show()
    app.processEvents()
    return w, app, mw


def _close(w, app):
    w.runner.wait()
    w.setWindowModified(False)
    w.close()
    app.processEvents()


# ---------------------------------------------------------------- the module
def test_ppg_actions_is_lazy():
    code = ("import sys, pyarchinit_photogrammetry as ppg\n"
            "assert 'pyarchinit_photogrammetry.assistant.actions' not in sys.modules\n"
            "assert callable(ppg.actions.stones) and 'stones' in ppg.actions.ACTIONS\n"
            "assert 'pyarchinit_photogrammetry.assistant.actions' in sys.modules\n"
            "assert 'PySide6' not in sys.modules\n")
    env = {**os.environ, "PPG_NO_PLUGINS": "1"}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stderr


def test_registry_describes_every_action():
    from pyarchinit_photogrammetry.assistant import actions

    assert set(actions.ACTIONS) == {"show", "select_photos", "stones", "build", "segment", "export", "run_script"}
    for name, (fn, quick, it, en, schema) in actions.ACTIONS.items():
        assert callable(fn) and fn.__name__ == name and isinstance(quick, bool)
        assert it and en and schema["type"] == "object"
        for key, prop in schema["properties"].items():
            assert prop.get("description_it") and prop.get("description_en"), (name, key)
        assert set(schema.get("required", [])) <= set(schema["properties"])
    assert actions.ACTIONS["show"][1] and not actions.ACTIONS["stones"][1] and not actions.ACTIONS["build"][1]
    assert actions.is_quick("select_photos", {"by": "names", "then": "filter"})
    assert not actions.is_quick("select_photos", {"by": "names", "then": "disable"})
    assert actions.ACTIONS["build"][4]["properties"]["product"]["enum"] == list(actions.PRODUCTS)


# ---------------------------------------------------------------- headless
def test_show_needs_the_window(wall):
    from pyarchinit_photogrammetry.assistant import actions

    _doc, ch, _n = wall
    with pytest.raises(ValueError, match="finestra"):
        actions.show(ch, None, "ortho")


def test_stones_headless_stored_where_the_tool_stores_them(wall):
    from shapely import wkt

    from pyarchinit_photogrammetry.archaeo import stones as st
    from pyarchinit_photogrammetry.assistant import actions

    _doc, ch, n = wall
    r = actions.stones(ch, None, min_size=0.1, max_size=0.6, thoroughness="normale")
    assert r["stones"] == n and r["layer"] == "Pietre" and r["raster"] == "orthomosaic"
    assert "/" not in str(r) and "\\" not in str(r)  # no paths for the model
    kept = [wkt.loads(s) for s in ch.meta["stones"]["orthomosaic.tif"]]
    assert len(kept) == n and all(0.1 <= st.size_of(s) <= 0.6 for s in kept)
    # a box: the stones touching it found again, those outside kept (as the tool does)
    from shapely.geometry import box

    outside = sum(1 for s in kept if not s.intersects(box(0.0, 0.75, 1.0, 1.5)))
    r = actions.stones(ch, None, raster="orthomosaic", bounds=(0.0, 0.75, 1.0, 1.5), min_size=0.1, max_size=0.6)
    assert 0 < r["stones"] < n and 0 < outside < n
    assert r["total"] == outside + r["stones"] == len(ch.meta["stones"]["orthomosaic.tif"])


def test_stones_refuse_a_dem_and_unknown_rasters(wall):
    from pyarchinit_photogrammetry.assistant import actions

    _doc, ch, _n = wall
    with pytest.raises(ValueError, match="ortofoto o un prospetto"):
        actions.stones(ch, None, raster="dem")
    with pytest.raises(ValueError, match="orthomosaic"):  # the names there are
        actions.stones(ch, None, raster="nessuno")
    with pytest.raises(ValueError, match="thoroughness"):
        actions.stones(ch, None, thoroughness="lenta")
    assert "stones" not in ch.meta


def test_segment_headless_with_the_fake_sam(wall, monkeypatch):
    from pyarchinit_photogrammetry.ai import sam_env
    from pyarchinit_photogrammetry.assistant import actions

    monkeypatch.setenv("PPG_SAM_FAKE", "1")
    monkeypatch.setattr(sam_env, "make_segmenter",
                        lambda model="small": sam_env.ExternalSam2Segmenter(model, python=sys.executable))
    _doc, ch, _n = wall
    r = actions.segment(ch, None, prompt="USM")
    assert r["units"] >= 1 and r["layer"] == "Unita' da SAM"
    rows = ch.meta["drawings"]["units"]
    assert len(rows) == r["units"] and all(u["layer"] == "Unita' da SAM" and u["tipo"] == "USM" for u in rows)


def test_select_photos_headless(project):
    from pyarchinit_photogrammetry.assistant import actions

    _doc, ch = project
    cams = ch.cameras
    first = cams[0].label
    r = actions.select_photos(ch, None, "names", then="disable", pattern=first)
    assert r["photos"] == 1 and r["changed"] == 1 and r["labels"] == [first] and not cams[0].enabled
    r = actions.select_photos(ch, None, "all", then="enable")
    assert r["photos"] == len(cams) and r["changed"] == 1 and all(c.enabled for c in cams)
    x0, y0, x1, y1 = _ortho_bounds(ch)
    r = actions.select_photos(ch, None, "area", then="disable", area=(x0, y0, x0 + (x1 - x0) / 4, y0 + (y1 - y0) / 4))
    assert 0 < r["photos"] <= len(cams) and r["changed"] == r["photos"]
    actions.select_photos(ch, None, "all", then="enable")
    with pytest.raises(ValueError, match="finestra"):
        actions.select_photos(ch, None, "names", then="filter", pattern="*")
    with pytest.raises(ValueError, match="by"):
        actions.select_photos(ch, None, "colore", then="disable")
    with pytest.raises(ValueError, match="then"):
        actions.select_photos(ch, None, "all", then="cancella")
    with pytest.raises(ValueError, match="pattern"):
        actions.select_photos(ch, None, "names", then="disable")


def _ortho_bounds(ch):
    import rasterio

    with rasterio.open(ch.orthomosaic_path) as src:
        b = src.bounds
    return b.left, b.bottom, b.right, b.top


def test_build_keeps_the_product_before_as_a_version(project):
    from pyarchinit_photogrammetry.assistant import actions
    from pyarchinit_photogrammetry.core import versions

    _doc, ch = project
    kept = len(versions.versions(ch, "dem"))
    r = actions.build(ch, None, "dem", resolution=0.02)
    made = versions.versions(ch, "dem")
    assert len(made) == kept + 1 and r["version_kept"] == made[-1]["name"]
    assert r["product"] == "dem" and r["resolution"] == pytest.approx(0.02, rel=0.01) and r["width"] > 0
    assert not ch.keep_versions
    with pytest.raises(ValueError, match="resolution"):  # the known keywords listed
        actions.build(ch, None, "dem", risoluzione=0.02)
    with pytest.raises(ValueError, match="orthomosaic"):
        actions.build(ch, None, "pianta")
    with pytest.raises(ValueError, match="interval"):
        actions.build(ch, None, "contours")
    r = actions.build(ch, None, "contours", interval=0.05)
    assert r["lines"] > 0


def test_export_and_run_script_headless(project, tmp_path):
    from pyarchinit_photogrammetry.assistant import actions

    _doc, ch = project
    out = tmp_path / "esportati" / "dem_export"
    out.parent.mkdir()
    r = actions.export(ch, None, "dem", "tif", path=out)
    assert r["file"] == "dem_export.tif" and r["bytes"] == (out.parent / "dem_export.tif").stat().st_size > 0
    with pytest.raises(ValueError, match="percorso"):
        actions.export(ch, None, "dem", "tif")
    with pytest.raises(ValueError, match="tif"):
        actions.export(ch, None, "dem", "obj", path=tmp_path / "x.obj")
    with pytest.raises(ValueError, match="orthomosaic"):
        actions.export(ch, None, "foto", "jpg", path=tmp_path / "x.jpg")
    r = actions.run_script(ch, None, "print(chunk.label, len(chunk.cameras))")
    assert r["error"] is None and r["output"].strip() == f"{ch.label} {len(ch.cameras)}"
    r = actions.run_script(ch, None, "1 / 0")
    assert r["error"].startswith("ZeroDivisionError")


# ---------------------------------------------------------------- in the window and from a script
def test_stones_in_the_window_and_from_a_script(wall, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.assistant import actions

    doc, ch, n = wall
    w, app, mw = _window(doc, tmp_path)
    try:
        monkeypatch.setattr(w, "_segmenter", lambda: WallSegmenter())
        w._show_raster("ortho")
        r = actions.stones(w.chunk, w, min_size=0.1, max_size=0.6)
        assert r["stones"] == n and len(w.stones["orthomosaic.tif"]) == n
        assert len(w.ortho_view.layers[mw.STONES_LAYER]["features"]) == n
        assert len(w.chunk.meta["stones"]["orthomosaic.tif"]) == n  # in the chunk too: saved, undone
        # from a script (the window proxy in the worker): one entry of Modifica > Annulla
        w.stones = {}
        w._store_drawings()
        w._draw_stones()
        entries = len(w.history.entries)
        results = []
        src = "print(ppg.actions.stones(chunk, window, min_size=0.1, max_size=0.6)['stones'])"
        assert w.run_script_source(src, "Pietre", on_done=results.append)
        w.runner.wait()
        app.processEvents()
        assert results and results[0].error is None, results[0].traceback
        assert results[0].output.strip() == str(n) and len(w.stones["orthomosaic.tif"]) == n
        assert len(w.history.entries) == entries + 1
        w.history.undo()
        assert not w.stones.get("orthomosaic.tif") and "stones" not in w.chunk.meta
        w._show_raster("dem")
        with pytest.raises(ValueError, match="ortofoto o un prospetto"):
            actions.stones(w.chunk, w)
    finally:
        _close(w, app)


def test_show_select_and_segment_in_the_window(project, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.ai import sam_env
    from pyarchinit_photogrammetry.assistant import actions

    doc, _ch = project
    w, app, mw = _window(doc, tmp_path)
    try:
        ch = w.chunk
        r = actions.show(ch, w, "dem")
        assert w.ortho_view.kind == "dem" and r["shown"] == "dem"
        actions.show(ch, w, "orthomosaic")
        assert w.ortho_view.kind == "ortho" and w.ortho_view.path.name == "orthomosaic.tif"
        actions.show(ch, w, "model")
        assert w.tabs.currentWidget() is w.model_view
        with pytest.raises(ValueError, match="versione"):
            actions.show(ch, w, "dem", version="nessuna")
        names = [c.label for c in ch.cameras[:3]]
        r = actions.select_photos(ch, w, "names", then="filter", pattern=names[0])
        assert r["photos"] == 1 and len(w.photos.visible_cameras()) == 1
        actions.select_photos(ch, w, "all", then="filter")
        assert len(w.photos.visible_cameras()) == len(ch.cameras)
        r = actions.select_photos(ch, w, "names", then="select", pattern=names[1])
        assert [c.label for c in w.selected_cameras()] == [names[1]]
        r = actions.select_photos(ch, w, "names", then="disable", pattern=names[2])
        assert not ch.cameras[2].enabled
        # SAM units on the raster shown, in the layer of the SAM tools
        monkeypatch.setenv("PPG_SAM_FAKE", "1")
        monkeypatch.setattr(w, "_segmenter", lambda: sam_env.ExternalSam2Segmenter(python=sys.executable))
        actions.show(ch, w, "ortho")
        before = len(w.units)
        r = actions.segment(ch, w)
        assert r["units"] >= 1 and len(w.units) == before + r["units"]
        assert all(u.attributes["_layer"] == "Unita' da SAM" for u in w.units[before:])
    finally:
        _close(w, app)
