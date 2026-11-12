"""The assistant's actions on the open project (``ppg.actions``): headless, from a script and in the window."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from pathlib import Path

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
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=300,
                       close_fds=False)  # posix_spawn, never a fork of the process of the tests (see test_env)
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
    import json

    r = actions.run_script(ch, None, "import json\njson.loads('x')")
    folder = str(Path(json.__file__).parent)
    assert r["error"].startswith("JSONDecodeError") and "decoder.py" in r["traceback"]
    assert folder not in r["traceback"] and folder not in r["error"]
    with pytest.raises(ValueError, match="un numero"):
        actions.build(ch, None, "dem", resolution="fine")
    with pytest.raises(ValueError, match="true o false"):
        actions.build(ch, None, "dem", interpolate="si")


# ---------------------------------------------------------------- in the window and from a script
def test_stones_in_the_window_and_from_a_script(wall, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.assistant import actions

    doc, ch, n = wall
    w, app, mw = _window(doc, tmp_path)
    try:
        w.sam = WallSegmenter()
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
        w._show_raster("dem")  # a DEM shown: the stones go on the chunk's orthophoto, and the result says so
        r = actions.stones(w.chunk, w)
        assert r["raster"] == "orthomosaic" and "DEM" in r["note"] and len(w.stones["orthomosaic.tif"]) == n
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
        w.sam = sam_env.ExternalSam2Segmenter(python=sys.executable)
        actions.show(ch, w, "ortho")
        before = len(w.units)
        r = actions.segment(ch, w)
        assert r["units"] >= 1 and len(w.units) == before + r["units"]
        assert all(u.attributes["_layer"] == "Unita' da SAM" for u in w.units[before:])
    finally:
        _close(w, app)


def test_sam_is_made_in_the_scripts_thread_not_in_the_window(wall, tmp_path, monkeypatch):
    """From a script the segmenter is made in the worker (the SAM worker starts and loads its model there: in the
    interface thread the window would freeze) and handed to the window, which keeps it."""
    import threading

    from pyarchinit_photogrammetry.ai import sam_env

    doc, _ch, n = wall
    w, app, _mw = _window(doc, tmp_path)
    try:
        made = []

        def make(model="small"):
            made.append((threading.current_thread() is threading.main_thread(), model))
            return WallSegmenter()

        monkeypatch.setattr(sam_env, "make_segmenter", make)
        w.sam = None
        w.settings.setValue("sam/model", "tiny")
        w._show_raster("ortho")
        results = []
        src = "print(ppg.actions.stones(chunk, window, min_size=0.1, max_size=0.6)['stones'])"
        assert w.run_script_source(src, "Pietre", on_done=results.append)
        w.runner.wait()
        app.processEvents()
        assert results and results[0].error is None, results[0].traceback
        assert results[0].output.strip() == str(n)
        assert made == [(False, "tiny")] and isinstance(w.sam, WallSegmenter)
    finally:
        _close(w, app)


# the values the tests above give to each action: the schema of the registry must accept every one of them
GIVEN = {
    "show": {"what": ["ortho", "dem", "orthomosaic", "model"], "version": ["nessuna"], "chunk_label": ["Saggio 1"],
             "elevation": ["Muro nord"]},
    "select_photos": {"by": ["names", "all", "area", "markers"], "then": ["filter", "select", "enable", "disable"],
                      "pattern": ["DJI_01*"], "area": [[0.0, 0.0, 1.0, 1.0], "selection"],
                      "markers": [["target 1", "2"], "target 1", 3], "enabled": [True, False]},
    "stones": {"raster": ["orthomosaic", "dem"], "bounds": [[0.0, 0.75, 1.0, 1.5], "selection"],
               "min_size": [0.1, 0.05], "max_size": [0.6, 1], "thoroughness": ["normale", "alta"]},
    "build": {"product": ["dem", "contours"], "chunk_label": ["Saggio 1"],
              "params": [{"resolution": 0.02}, {"interval": 0.05}]},
    "segment": {"prompt": ["USM", "muro"], "bounds": ["selection", [0, 0, 1, 1]], "thoroughness": ["normale"]},
    "export": {"what": ["dem"], "format": ["tif", "cog"], "path": ["/tmp/dem_export"]},
    "run_script": {"source": ["print(1)"]},
}
PARAMS_GIVEN = {"dem": {"resolution": [0.02, 1], "interpolate": [True], "classes": [[2]], "source": ["point_cloud"]},
                "contours": {"interval": [0.05], "index_every": [5], "smooth": [0.2]},
                "model": {"face_count": ["medium", 200000]}}


def test_the_schemas_accept_what_the_actions_take():
    jsonschema = pytest.importorskip("jsonschema")
    from pyarchinit_photogrammetry.assistant import actions

    for name, values in GIVEN.items():
        props = actions.ACTIONS[name][4]["properties"]
        for key, examples in values.items():
            for v in examples:
                jsonschema.validate(v, props[key])
    per_product = actions.ACTIONS["build"][4]["properties"]["params"]["per_product"]
    for product, values in PARAMS_GIVEN.items():
        for key, examples in values.items():
            for v in examples:
                jsonschema.validate(v, per_product[product][key])
    with pytest.raises(jsonschema.ValidationError):  # and still refuse what is wrong
        jsonschema.validate("fine", per_product["dem"]["resolution"])


def test_call_passes_the_tool_arguments_to_the_action(monkeypatch):
    """What the model sends: empty keys left out, build's params flattened, progress to who takes it, an unknown
    parameter named."""
    from pyarchinit_photogrammetry.assistant import actions

    seen = []

    def build(chunk, window, product, chunk_label=None, progress=None, **params):
        seen.append(("build", product, chunk_label, progress, params))
        return {"built": product}

    def show(chunk, window, what, chunk_label=None):
        seen.append(("show", what, chunk_label))
        return {}

    monkeypatch.setitem(actions.ACTIONS, "build", (build, *actions.ACTIONS["build"][1:]))
    monkeypatch.setitem(actions.ACTIONS, "show", (show, *actions.ACTIONS["show"][1:]))
    assert actions.call("build", "C", None, {"product": "dem", "chunk_label": None,
                                             "params": {"resolution": 0.02, "source": None}}, "P") == {"built": "dem"}
    assert seen[-1] == ("build", "dem", None, "P", {"resolution": 0.02})
    actions.call("show", "C", "W", {"what": "dem", "chunk_label": "B"}, "P")
    assert seen[-1] == ("show", "dem", "B")
    with pytest.raises(ValueError, match="colore"):
        actions.call("show", "C", "W", {"what": "dem", "colore": "rosso"})
    with pytest.raises(ValueError, match="sconosciuta"):
        actions.call("vola", "C", "W", {})


def test_stones_with_a_dem_shown_fall_back_to_the_orthomosaic(wall):
    from pyarchinit_photogrammetry.assistant import actions

    doc, ch, n = wall

    class Window:  # a window that shows the DEM
        chunk, sam = ch, None
        stored = {}

        def __init__(self):
            self.doc = doc

        def shown_raster(self):
            return {"path": ch.product_path("dem"), "kind": "dem", "frame": None, "version": None}

        def set_stones(self, key, found, bounds=None):
            self.stored[key] = found
            return len(found)

    w = Window()
    r = actions.stones(ch, w, min_size=0.1, max_size=0.6)
    assert r["raster"] == "orthomosaic" and r["stones"] == n and "DEM" in r["note"]
    assert list(w.stored) == ["orthomosaic.tif"]
    ch.products.pop("orthomosaic")  # no orthomosaic: refused as before
    with pytest.raises(ValueError, match="ortofoto o un prospetto"):
        actions.stones(ch, w)


def test_export_without_path_asks_and_a_cancel_is_declined(wall):
    from pyarchinit_photogrammetry.assistant import actions

    _doc, ch, _n = wall

    class Window:
        chunk, asked = ch, []

        def ask_save_path(self, title, name, filter_):
            self.asked.append(name)
            return ""

    w = Window()
    assert actions.export(ch, w, "orthomosaic", "tif") == {"declined": True, "what": "orthomosaic", "format": "tif",
                                                           "chunk": "Muro"}
    assert w.asked == ["orthomosaic.tif"]
    assert "path" in actions.script_only("export")


def test_paths_are_cut_to_their_names_but_urls_stay():
    from pyarchinit_photogrammetry.assistant.actions import _no_paths, _of_type

    assert _no_paths('File "/Users/enzo/My Docs/scavo.py", line 3') == 'File "scavo.py", line 3'
    assert _no_paths("see https://example.com/a/b.html") == "see https://example.com/a/b.html"
    assert _no_paths(r"C:\Users\x\f.tif failed") == "f.tif failed"
    assert _of_type(np.int64(3), "integer") and _of_type(np.float32(0.5), "number")
    assert _of_type(np.zeros(4), "array") and not _of_type(True, "integer")


def test_quick_showing_is_only_on_the_active_chunk():
    from pyarchinit_photogrammetry.assistant.actions import is_quick

    assert is_quick("show", {"what": "dem"}, "A") and is_quick("show", {"chunk_label": "A"}, "A")
    assert not is_quick("show", {"chunk_label": "B"}, "A")
