"""The inventory of the open project the assistant gets with every question: what the project holds and what the
window shows, short, filtered for the external models, never a path nor a coordinate; and the rules that ground the
scripts on this project."""
from __future__ import annotations

import os
import shutil

import numpy as np
import pytest

from pyarchinit_photogrammetry.assistant import inventory as I
from pyarchinit_photogrammetry.assistant import session as S
from pyarchinit_photogrammetry.assistant.knowledge import default_index
from pyarchinit_photogrammetry.assistant.privacy import Filter
from pyarchinit_photogrammetry.assistant.providers import Message, Reply
from pyarchinit_photogrammetry.core.camera import Camera
from pyarchinit_photogrammetry.core.document import Document


def _geotiff(path, bands=1):
    import rasterio
    from rasterio.transform import from_origin

    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", width=8, height=6, count=bands, dtype="float32",
                       crs="EPSG:32633", transform=from_origin(500000.0, 4500000.0, 0.05, 0.05)) as ds:
        ds.write(np.ones((bands, 6, 8), np.float32))


@pytest.fixture
def demo_doc(tmp_path):
    """A project of two chunks: the first with photos, a DEM in two versions, an orthophoto, a model, drawings, stones
    and an elevation; the second with photos only."""
    doc = Document(tmp_path / "Scavi" / "Rilievo Nord.ppg")
    doc.meta["sito"] = "Tell Sultan"
    a = doc.add_chunk("Volo 28/08")
    b = doc.add_chunk("Volo 30/09")
    for k in range(5):
        cam = Camera(k, f"DJI_{k:04d}.JPG", str(tmp_path / f"DJI_{k:04d}.JPG"), 0)
        cam.transform = np.eye(4) if k < 4 else None
        a.cameras.append(cam)
    b.cameras.append(Camera(0, "IMG_1.JPG", str(tmp_path / "IMG_1.JPG"), 0))
    _geotiff(a.data_dir / "dem.tif")
    _geotiff(a.data_dir / "orthomosaic.tif", 3)
    _geotiff(a.data_dir / "versions" / "dem" / "v1" / "dem.tif")
    (a.data_dir / "model.ply").write_bytes(b"ply\nformat binary_little_endian 1.0\nend_header\n")
    a.products.update({"dem": "dem.tif", "orthomosaic": "orthomosaic.tif", "model": "model.ply",
                       "point_cloud": "point_cloud.ply"})  # the dense cloud's file is not there: not listed
    a.meta["versions"] = {"dem": [{"id": "v1", "name": "DEM di Metashape", "files": {"dem": "versions/dem/v1/dem.tif"}}]}
    a.meta["active_versions"] = {"dem": {"name": "DEM dalla nuvola", "created": "2026-10-30T10:00:00"}}
    a.meta["drawings"] = {"units": [{"id": 1}, {"id": 2}], "quote": [{"id": 1}], "lines": [{"id": 1}]}
    a.meta["stones"] = {"orthomosaic.tif": ["POLYGON ((0 0, 1 0, 1 1, 0 0))"] * 3}
    a.meta["elevations"] = [{"id": "e1", "name": "Prospetto nord", "kind": "plane", "crs": "EPSG:32633",
                             "bounds": [1.0, 2.0, 3.0, 4.0]}]
    doc.chunk = a
    return doc


def _walk(value):
    """Every key and every leaf of a nested structure."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _walk(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk(v)
    else:
        yield value


def _a_path(text: str) -> bool:
    return text.startswith(("/", "~")) or ":\\" in text or "\\" in text


def test_snapshot_of_the_demo_project(demo_doc, monkeypatch):
    import rasterio.io

    from pyarchinit_photogrammetry.io import mesh_ply

    def never(*args, **kwargs):
        raise AssertionError("the inventory read the pixels or the model")

    monkeypatch.setattr(rasterio.io.DatasetReader, "read", never)
    monkeypatch.setattr(mesh_ply, "read_header", never)
    monkeypatch.setattr(mesh_ply, "read_mesh", never)
    snap = I.snapshot(demo_doc)
    assert snap["project"] == "Rilievo Nord" and snap["site"] == "Tell Sultan" and snap["active"] == "Volo 28/08"
    assert snap["active_key"] == 0
    first, second = snap["chunks"]
    assert first["label"] == "Volo 28/08" and first["key"] == 0 and first["enabled"] is True
    assert first["photos"] == 5 and first["aligned"] == 4
    assert set(first["products"]) == {"dem", "orthomosaic", "model"}
    assert first["products"]["dem"] == {"versions": ["DEM di Metashape"], "active": "DEM dalla nuvola"}
    assert "dem" in first["rasters"] and "orthomosaic" in first["rasters"]
    assert any(r.startswith("dem_") for r in first["rasters"])  # the kept version, by its calculator name
    assert first["drawings"] == {"units": 2, "quote": 1, "points": 0, "lines": 1, "stones": 3}
    assert first["elevations"] == [{"id": "e1", "name": "Prospetto nord", "kind": "plane"}]
    assert second["photos"] == 1 and second["products"] == {} and second["rasters"] == []
    assert snap["view"] == {"kind": None, "raster": None, "version": None, "elevation": None, "model": None}
    assert snap["selection"] == {"polygon_m2": None, "photos_selected": 0, "photos_filtered": None}
    leaves = [str(x) for x in _walk(snap)]
    assert not any(_a_path(x) or x.endswith((".tif", ".ply", ".JPG")) for x in leaves), leaves  # no path
    assert "bounds" not in leaves and "crs" not in leaves and "4500000.0" not in leaves  # no coordinates


def test_snapshot_without_a_project_and_without_chunks(tmp_path):
    assert I.snapshot(None) is None
    snap = I.snapshot(Document(tmp_path / "vuoto.ppg"))
    assert snap["chunks"] == [] and snap["active"] is None
    assert "nessun chunk" in I.text(snap, "it")


def test_snapshot_with_the_window(processed, tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    w = mw.MainWindow(ppg.Document.open(d / doc.path.name), autoconnect=False, check_updates=False)
    try:
        w._show_raster("ortho")
        cams = w.chunk.cameras
        w.photos.set_filter({c.key for c in cams[:3]}, "tre foto")
        # the real readers: nothing selected, then two photos selected in the Photos pane
        snap = I.snapshot(w.doc, w)
        assert snap["selection"] == {"polygon_m2": None, "photos_selected": 0, "photos_filtered": 3}
        shown = [w.photos.item(i) for i in range(w.photos.count()) if not w.photos.item(i).isHidden()]
        for item in shown[:2]:
            item.setSelected(True)
        assert len(w.selected_cameras()) == 2 and w.selected_polygon() is None
        snap = I.snapshot(w.doc, w)
        assert snap["active"] == "Saggio 1"
        assert snap["view"]["kind"] == "ortho" and snap["view"]["raster"] == "orthomosaic"
        assert snap["view"]["version"] is None and snap["view"]["elevation"] is None  # never named: no version
        assert snap["selection"] == {"polygon_m2": None, "photos_selected": 2, "photos_filtered": 3}
        # an elevation shown: its name in the view
        w.chunk.meta["elevations"] = [{"id": "e9", "name": "Sezione A", "kind": "plane"}]
        w.drawings_frame = "e9"
        w._raster_version = ("elevation", "e9")
        snap = I.snapshot(w.doc, w)
        assert snap["view"]["elevation"] == "Sezione A"
        block = I.text(snap, "it")
        assert "Sezione A" in block and "3 foto" in block and "2 foto selezionate" in block and "Saggio 1" in block
        leaves = [str(x) for x in _walk(snap)]
        assert not any(_a_path(x) for x in leaves), leaves
        # the interface thread only
        import threading

        failed = []
        t = threading.Thread(target=lambda: failed.append(_raises(lambda: I.snapshot(w.doc, w))))
        t.start()
        t.join()
        assert failed == [True]
        # the pane takes the snapshot in the interface thread and the question carries it
        received = []

        class Fake:
            kind, model, label = "local", "finto", "Finto"

            def complete(self, messages, tools=None, on_text=None, cancel=None):
                received.append(messages[0].content)
                return Reply("ok", [], "end", {})

        pane = w.assistant
        pane.set_provider(Fake())
        pane.ask("quante pietre?")
        import time

        t0 = time.monotonic()
        while pane.busy and time.monotonic() - t0 < 20:
            app.processEvents()
            time.sleep(0.01)
        assert received and "\n## Il progetto adesso\n" in received[0] and "«Saggio 1»" in received[0]
        assert "Sezione A" in received[0]
    finally:
        w.drawings_frame = None
        w.chunk.meta.pop("elevations", None)
        w.setWindowModified(False)
        w.close()
        app.processEvents()


def _raises(fn) -> bool:
    try:
        fn()
    except RuntimeError:
        return True
    return False


def _synthetic(n=40):
    chunks = [{"label": f"Settore {i:02d} - campagna 2026", "key": i, "photos": 120 + i, "enabled": i % 7 != 3,
               "aligned": 118, "products": {"dem": {"versions": ["prima", "dopo la pulizia"], "active": "finale"},
                                            "orthomosaic": {"versions": [], "active": None},
                                            "model": {"versions": [], "active": None}},
               "rasters": ["dem", "orthomosaic", "dem_prima", "dem_dopo_la_pulizia", "ndvi"],
               "drawings": {"units": 12, "quote": 30, "points": 0, "lines": 2, "stones": 140},
               "elevations": [{"id": f"e{i}", "name": f"Prospetto {i}", "kind": "plane"}]} for i in range(n)]
    return {"project": "Grande scavo", "site": "Sito", "active": "Settore 17 - campagna 2026", "chunks": chunks,
            "view": {"kind": "ortho", "raster": "orthomosaic", "version": "finale", "elevation": None,
                     "model": "model"},
            "selection": {"polygon_m2": 12.5, "photos_selected": 3, "photos_filtered": 40}}


@pytest.mark.parametrize("lang, more", [("it", "altri chunk"), ("en", "more chunks")])
def test_text_stays_short_and_cuts(lang, more):
    snap = _synthetic(40)
    block = I.text(snap, lang)
    assert len(block) <= 1500
    assert block.startswith("## Il progetto adesso" if lang == "it" else "## The project now")
    assert more in block
    lines = block.splitlines()
    active = next(i for i, line in enumerate(lines) if "Settore 17" in line)
    others = [i for i, line in enumerate(lines) if "Settore" in line and "Settore 17" not in line]
    assert others and active < min(others)  # the active chunk first
    full = lines[active]
    for word in ("dem_dopo_la_pulizia", "ndvi", "finale", "Prospetto 17", "140"):  # in full
        assert word in full or word in block.split("\n", active + 2)[active + 1], word
    assert len(I.text(snap, lang, limit=600)) <= 600
    small = _synthetic(2)
    assert more not in I.text(small, lang) and "Settore 01" in I.text(small, lang)


def test_the_snapshot_of_forty_chunks_is_cheap(tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.engine import rastercalc

    doc = Document(tmp_path / "grande.ppg")
    for i in range(40):
        ch = doc.add_chunk(f"Settore {i}")
        ch.meta["drawings"] = {"units": [{"id": 1}]}
        ch.meta["elevations"] = ["not an entry", {"id": "e", "name": "Nord", "kind": "plane"}]
    doc.chunk = doc.chunks[5]
    calls = []
    real = rastercalc.rasters
    monkeypatch.setattr(rastercalc, "rasters", lambda chunk: calls.append(chunk) or real(chunk))
    snap = I.snapshot(doc)
    assert calls == [doc.chunks[5]]  # the rasters of the active chunk only, read once
    assert all(c["rasters"] == [] for c in snap["chunks"]) and snap["active_key"] == 5
    assert snap["chunks"][0]["elevations"] == [{"id": "e", "name": "Nord", "kind": "plane"}]
    block = I.text(snap, "en")
    assert len(block) <= 1500 and "Settore 0" in block
    assert block.splitlines()[2].startswith("- «Settore 5» (key 5)")  # the active chunk, by its key


def test_the_active_chunk_is_found_by_its_key_and_an_unknown_raster_is_neutral(tmp_path):
    from pathlib import Path

    snap = _synthetic(3)
    for c in snap["chunks"]:
        c["label"] = "Settore"  # three chunks with the same label
    snap["active"], snap["active_key"] = "Settore", 2
    lines = I.text(snap, "it").splitlines()
    assert lines[2].startswith("- «Settore» (key 2)")
    assert I._raster_name([], Path(tmp_path / "Tell Sultan scavo 2026.tif")) == "raster"


def test_the_prompt_carries_the_block_and_the_filter_neutralises_names(demo_doc):
    def make(kind):
        replies = [Reply("ok", [], "end", {})]
        calls = []

        class Fake:
            label = "fake"

            def __init__(self):
                self.kind, self.model = kind, "fake"

            def complete(self, messages, tools=None, on_text=None, cancel=None):
                calls.append([Message(m.role, m.content) for m in messages])
                return replies[0]

        return S.Assistant(Fake(), lambda: demo_doc, default_index("it"), language="it"), calls

    snap = I.snapshot(demo_doc)
    snap["view"].update(kind="ortho", raster="orthomosaic", elevation="Prospetto nord")
    external, calls = make("claude")
    turn = external.ask("disegna le pietre", inventory=snap)
    system = calls[0][0].content
    assert "\n## Il progetto adesso\n" in system
    assert "chunk 1" in system and "Volo 28/08" not in system and "Tell Sultan" not in system
    assert "Rilievo Nord" not in system and str(demo_doc.path.parent) not in system
    assert "orthomosaic" in system and "Prospetto nord" in system  # the view kept its elevation, filtered
    assert any(s.startswith("## Il progetto adesso") for s in turn.sent)  # said in *Cosa viene inviato*
    local, calls = make("local")
    local.ask("disegna le pietre", inventory=snap)
    system = calls[0][0].content
    assert "Volo 28/08" in system and "Tell Sultan" in system
    # no inventory: the prompt of 1.7.0
    plain, calls = make("local")
    plain.ask("ciao")
    assert "\n## Il progetto adesso\n" not in calls[0][0].content
    assert calls[0][0].content == plain.system_prompt()


def test_filtered_keeps_the_elevation_shown_through_the_filter(demo_doc):
    snap = I.snapshot(demo_doc)
    snap["view"]["elevation"] = "Volo 28/08"  # a name the filter knows
    out = I.filtered(snap, Filter(demo_doc))
    assert out["view"]["elevation"] == "chunk 1" and out["active"] == "chunk 1"
    assert I.filtered(snap, Filter.passthrough()) == snap


def test_the_rules_say_this_project():
    it, en = S._INSTRUCTIONS["it"], S._INSTRUCTIONS["en"]
    assert "Il progetto adesso" in it and "doc.chunk_by_label" in it and "UNA domanda" in it
    assert "mostrato" in it and "strument" in it
    assert "The project now" in en and "doc.chunk_by_label" in en and "ONE short question" in en
    assert "shown" in en


def test_chunk_by_label(demo_doc):
    assert demo_doc.chunk_by_label("Volo 30/09").key == 1
    with pytest.raises(KeyError, match="Volo 28/08"):
        demo_doc.chunk_by_label("nessuno")


@pytest.mark.parametrize("lang", ["it", "en"])
def test_the_overview_and_the_examples_cover_the_window(lang):
    assistant = S.Assistant(type("P", (), {"kind": "local", "label": "x"})(), lambda: None, None, language=lang)
    overview = assistant._scripting_overview()
    for name in ("show_raster", "show_model", "photos.set_filter", "selected_cameras", "enable_photos",
                 "window.log"):
        assert name in overview, name
    assert len(overview) <= S.OVERVIEW_CHARS
    if lang == "it":  # who has the helpers: the editor of the Assistant and Run Python script, not the console
        assert "con *Esegui* nell'editor del pannello *Assistente*" in overview
        assert "Nella\n**console Python** `window` è la finestra principale stessa" in overview
        assert "`window.refresh` non ci sono" in overview and "window.refresh_all()" in overview
    else:
        assert "with *Run* in the editor of the *Assistant* pane" in overview
        assert "In the **Python console** `window` is the\nmain window itself" in overview
        assert "`window.refresh` are not there" in overview and "window.refresh_all()" in overview
    from pyarchinit_photogrammetry.assistant import facts as F

    index = default_index(lang)
    for topic, wanted in (("pietre ortofoto stones" if lang == "it" else "stones on a raster", "ppg.actions.stones"),
                          ("foto marker filtro nome" if lang == "it" else "photos by markers by name filter",
                           "filter_photos_by_markers"),
                          ("ricostruire un prodotto e mostrarlo" if lang == "it" else "product built again and shown",
                           "show_raster")):
        found = F.run_tool("scripting_api", {"topic": topic, "lang": lang}, None, index)
        assert any(wanted in p.get("text", "") for p in found["passages"]), (topic, found)


def test_inventory_says_orientation_and_stale(processed, tmp_path):
    """A chunk oriented by hand: its line says so with the date, and the products to build again by name."""
    from orient_helpers import centre, local_copy

    from pyarchinit_photogrammetry.engine import orient

    doc, ch, _ = local_copy(processed, tmp_path)
    a = centre(ch)
    orient.orient(ch, points=[a, a + np.array([1.5, 0.2, 0.1]), a + np.array([0.1, 1.0, -0.05])])
    made = ch.meta["orientation"]["made"][:10]
    snap = I.snapshot(doc)
    line = next(c for c in snap["chunks"] if c["key"] == ch.key)
    assert line["oriented"] == made
    assert "DEM" in line["stale"] and "Ortofoto" in line["stale"]
    it = I.text(snap, "it")
    en = I.text(snap, "en")
    assert f"orientato a mano il {made}" in it and "da rifare: DEM, Ortofoto" in it
    assert f"oriented by hand on {made}" in en and "to rebuild: DEM, Ortofoto" in en
    # at most six names, then the count
    many = dict(line, stale=[f"p{i}" for i in range(9)])
    assert "p5, +3" in I._chunk_line(many, "en", full=True)
    # a chunk never oriented says nothing of it
    plain = I._chunk_line(dict(line, oriented=None, stale=[]), "it", full=True)
    assert "orientato" not in plain and "da rifare" not in plain
