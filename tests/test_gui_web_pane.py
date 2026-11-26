"""The pane *Pubblica sul web*: the survey as a web site built, looked at and kept with the project from the window.
A copy of the processed synthetic trench (a synthetic place in Gauss-Boaga, no real site)."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def project(processed, tmp_path_factory):
    """A copy of the processed trench (orthophoto, DEM, model; no drawings): the path of its project file."""
    doc = processed[0]
    base = tmp_path_factory.mktemp("web") / "progetto"
    base.mkdir()
    shutil.copy2(doc.path, base / doc.path.name)
    shutil.copytree(doc.files_dir, base / doc.files_dir.name)
    return base / doc.path.name


@pytest.fixture
def win(qapp, project, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import web_pane
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    doc = ppg.Document.open(project)
    doc.meta.pop("web_site", None)
    doc.meta.pop("sito", None)
    for ch in doc.chunks:
        ch.meta.pop("drawings", None)
    doc.save()
    opened: list = []
    monkeypatch.setattr(web_pane.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    w = MainWindow(doc, autoconnect=False)
    said: list = []
    w.said, w.opened = said, opened
    monkeypatch.setattr(w, "_warn", lambda text: said.append(("warn", text)))
    monkeypatch.setattr(w, "_error", lambda summary, details="", logged=True: said.append(("error", summary)))
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qapp.processEvents()
    yield w
    w.web.close()
    w.setWindowModified(False)
    w.close()


def _settle(qapp):
    for _ in range(3):
        qapp.processEvents()


def _front(w, qapp):
    w.show_web_pane()
    _settle(qapp)
    return w.web_pane


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers=headers or {})
        r = conn.getresponse()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read()
    finally:
        conn.close()


def _port_free(port) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _preview_threads():
    return [t for t in threading.enumerate() if t.name == "ppg-site-preview"]


def _build(w, qapp, pane, folder, products=("orthomosaic", "dem")):
    for key, c in pane.products.items():
        c.setChecked(key in products)
    pane.texture.setCurrentIndex(pane.texture.findData(1024))
    pane.folder.setText(str(folder))
    assert pane.b_build.isEnabled(), pane.states["contents"].text()
    assert w.web.build()
    assert pane.step_states["build"] == "running"
    w.runner.wait()
    _settle(qapp)
    assert not w.said, w.said


def test_pane_from_the_menu_and_from_vista_pannelli(win, qapp):
    from pyarchinit_photogrammetry.gui.command_search import clean, menu_commands

    w = win
    dock = w.panes.dock(w.web_pane)
    assert dock.objectName() == "pane_web" and dock.isFloating() is False
    wanted = clean("Sito web (Pubblica sul web)...")
    act = next(a for a in menu_commands(w.menuBar()).values() if clean(a.text()) == wanted)
    assert act in w.export_menu.actions()
    dock.close()
    _settle(qapp)
    assert dock.isHidden()
    act.trigger()  # File > Esporta > Sito web
    _settle(qapp)
    assert not dock.isHidden() and w.panes.groups["left"].currentWidget() is w.web_pane
    menu = {a.text(): a for a in w.panes.menu.actions() if a.text()}
    assert dock.close()
    _settle(qapp)
    assert not menu["Pubblica sul web"].isChecked()
    menu["Pubblica sul web"].trigger()  # Vista > Pannelli > Pubblica sul web
    _settle(qapp)
    assert not dock.isHidden() and menu["Pubblica sul web"].isChecked()


def test_only_the_products_a_chunk_has(win, qapp):
    w = win
    pane = _front(w, qapp)
    assert [ch.label for ch in pane.chosen_chunks()] == [w.chunk.label]  # every chunk with something: the one
    assert pane.products["orthomosaic"].isEnabled() and pane.products["dem"].isEnabled()
    assert pane.products["model"].isEnabled()
    assert not pane.products["units"].isEnabled() and not pane.products["drawings"].isEnabled()
    assert pane.options()["products"] == ["orthomosaic", "dem", "model"]
    assert pane.frame_local.isChecked() and pane.options()["frame"] == "local"
    assert pane.options()["languages"] == ["it", "en"] and pane.options()["quality"] == 85
    assert pane.options()["model_texture"] == 4096
    assert Path(pane.folder.text()) == Path(w.doc.path).parent / "project_sito_web"  # beside the project
    assert pane.step_states == {"contents": "done", "build": "todo", "preview": "todo", "publish": "todo"}
    assert not pane.b_preview.isEnabled() and not pane.b_stop.isEnabled() and not pane.b_open_folder.isEnabled()
    assert "Costruisci prima il sito" in pane.states["publish"].text() and not pane.b_publish.isEnabled()
    from PySide6.QtCore import Qt

    pane.chunks.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert pane.step_states["contents"] == "todo" and not pane.b_build.isEnabled()
    assert not pane.products["dem"].isEnabled()


def test_true_coordinates_say_so(win, qapp):
    pane = _front(win, qapp)
    assert not pane.crs_warning.isVisibleTo(pane)
    pane.frame_crs.setChecked(True)
    assert pane.crs_warning.isVisibleTo(pane) and "posizione reale" in pane.crs_warning.text()
    assert "coordinate vere" in pane.states["contents"].text()
    pane.frame_local.setChecked(True)
    assert not pane.crs_warning.isVisibleTo(pane)


def test_a_foreign_folder_is_refused(win, qapp, tmp_path):
    w = win
    pane = _front(w, qapp)
    other = tmp_path / "documenti"
    other.mkdir()
    (other / "tesi.txt").write_text("mia", encoding="utf-8")
    pane.folder.setText(str(other))
    assert pane.step_states["contents"] == "failed" and not pane.b_build.isEnabled()
    assert "non è un sito" in pane.states["contents"].text()
    assert w.web.build() is False and w.said and "non è un sito" in w.said[-1][1]
    assert [p.name for p in other.iterdir()] == ["tesi.txt"]
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    pane.folder.setText(str(tmp_path / "file.txt"))
    assert pane.step_states["contents"] == "failed" and "file" in pane.states["contents"].text()


def test_build_preview_record_and_stale(win, qapp, tmp_path):
    import pyarchinit_photogrammetry as ppg

    w = win
    pane = _front(w, qapp)
    pane.title.setText("Saggio di prova")
    pane.languages["it"].setChecked(False)
    folder = Path(w.doc.path).parent / "sito"
    _build(w, qapp, pane, folder)
    manifest = json.loads((folder / "site.json").read_text(encoding="utf-8"))
    assert manifest["title"] == "Saggio di prova" and manifest["languages"] == ["en"]
    assert manifest["frame"] == "local" and manifest["crs"] is None
    layers = {layer["id"] for layer in manifest["chunks"][0]["layers"]}
    assert layers == {"ortho", "hillshade", "dem"}  # no model: unticked
    text = "".join(f.read_text(encoding="utf-8", errors="ignore") for f in folder.rglob("*")
                   if f.is_file() and f.suffix in (".json", ".html", ".geojson"))
    for p in (str(Path.home()), str(Path(w.doc.path).parent), tempfile.gettempdir()):
        assert p not in text  # no path of this computer
    for word in ("EPSG", "3004"):
        assert word not in json.dumps(manifest)
    # the state: done, the record kept with the project (folder relative to it), the site in the tree
    assert pane.step_states["build"] == "done" and "MB" in pane.states["build"].text()
    record = w.doc.meta["web_site"]
    assert record["folder"] == "sito" and record["options"]["products"] == ["orthomosaic", "dem"]
    assert record["options"]["languages"] == ["en"] and record["size"] > 0 and record["frame"] == "local"
    saved = ppg.Document.open(w.doc.path)
    assert saved.meta["web_site"]["folder"] == "sito"  # saved with the project
    items = [it for it in w.workspace._walk() if (it.data(0, 0x0100) or (None,))[0] == "web_site"]
    assert len(items) == 1 and "da rifare" not in items[0].text(0)
    assert pane.b_preview.isEnabled() and pane.b_open_folder.isEnabled()

    # the preview: 127.0.0.1, a free port, the page and a range request; Ferma frees port and thread
    url = w.web.start_preview()
    assert url and url.startswith("http://127.0.0.1:") and w.opened == [url]
    port = w.web.preview.port
    assert w.web.preview.httpd.server_address[0] == "127.0.0.1"
    assert pane.step_states["preview"] == "running" and pane.address.text() == url
    status, head, body = _get(port, "/index.html")
    assert status == 200 and b"app/main.js" in body
    data = (folder / "data/dem.tif").read_bytes()
    status, head, body = _get(port, "/data/dem.tif", {"Range": "bytes=10-109"})
    assert status == 206 and body == data[10:110] and head["content-range"] == f"bytes 10-109/{len(data)}"
    pane.b_copy.click()
    from PySide6.QtGui import QGuiApplication

    assert QGuiApplication.clipboard().text() == url
    pane.b_stop.click()
    assert w.web.preview is None and not _preview_threads() and _port_free(port)
    assert pane.step_states["preview"] == "todo" and pane.address.text() == ""

    # a product built again after the site: da rifare, in the pane and in the tree
    dem = w.chunk.dem_path
    st = dem.stat()
    try:
        os.utime(dem, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        w.refresh_all()
        _settle(qapp)
        assert pane.step_states["build"] == "stale" and "dem" in pane.states["build"].text()
        items = [it for it in w.workspace._walk() if (it.data(0, 0x0100) or (None,))[0] == "web_site"]
        assert "da rifare" in items[0].text(0)
    finally:
        os.utime(dem, ns=(st.st_atime_ns, st.st_mtime_ns))
    w.refresh_all()
    assert pane.step_states["build"] == "done"


def test_closing_the_window_stops_the_preview(qapp, project, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import web_pane
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    doc = ppg.Document.open(project)
    doc.meta.pop("web_site", None)
    site = doc.export_site(tmp_path / "sito", products="dem", model_texture=None)
    from pyarchinit_photogrammetry.io import site as site_mod

    site_mod.record_site(doc, site, {"products": ["dem"], "frame": "local"}, site_mod.site_sources(doc, None, ["dem"]))
    monkeypatch.setattr(web_pane.QDesktopServices, "openUrl", lambda url: True)
    w = MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    w.show()
    _settle(qapp)
    assert w.web_pane.step_states["build"] == "done"
    assert w.web_pane.folder.text() == str(tmp_path / "sito")  # outside the project: absolute
    assert w.web.start_preview()
    port = w.web.preview.port
    assert _get(port, "/site.json")[0] == 200 and _preview_threads()
    w.setWindowModified(False)
    w.close()
    _settle(qapp)
    assert w.web.preview is None and not _preview_threads() and _port_free(port)


def test_cloud_budget_and_dates_among_the_contents(win, qapp, tmp_path):
    """The point cloud offered but not ticked by default (a site stays light), the size budget, the points of the
    cloud and one date at a time among the advanced choices, given to the build and kept with the site."""
    import json

    w = win
    pane = _front(w, qapp)
    assert pane.products["cloud"].isEnabled() and not pane.products["cloud"].isChecked()
    assert not pane.products["sections"].isEnabled()  # the trench has no section
    o = pane.options()
    assert (o["budget_mb"], o["cloud_points"], o["timeline"]) == (500, 3_000_000, False)
    pane.budget.setValue(0)
    assert pane.budget.text() == "Nessun limite" and pane.options()["budget_mb"] == 0
    pane.budget.setValue(300)
    pane.cloud_points.setValue(20_000)
    _build(w, qapp, pane, tmp_path / "sito", products=("dem", "cloud"))
    manifest = json.loads((tmp_path / "sito" / "site.json").read_text(encoding="utf-8"))
    layer = next(x for x in manifest["chunks"][0]["layers"] if x["id"] == "cloud")
    assert layer["points"] == 20_000 and manifest["build"]["budget"] == 300_000_000
    assert w.doc.meta["web_site"]["options"]["cloud_points"] == 20_000
    pane.set_document(w.doc)  # the choices of the last site come back
    assert pane.products["cloud"].isChecked() and pane.cloud_points.value() == 20_000 and pane.budget.value() == 300


# --- the unit records of PyArchInit, the names of people, the heights of the masonry units ---

SITE = "Sito di prova"


class FakePyArchInit:
    """What the pane reads of the bridge: ``us_rows`` of the sites asked (US 1 of the trench, and US 2 recorded as a
    USM - found ignoring the type; US 3 has none), or the error of a database that cannot be read."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.asked: list = []

    def us_rows(self, sites):
        self.asked.append(list(sites))
        if self.error is not None:
            raise self.error
        return [{"sito": SITE, "area": "1", "us": n, "unita_tipo": kind, "d_stratigrafica": f"Strato {n}",
                 "descrizione": f"Descrizione {n}", "schedatore": "Mario Schedatore"}
                for n, kind in (("1", "US"), ("2", "USM")) if SITE in sites]


def _units(w, qapp):
    """Three unit polygons on the trench (US 1, 2, 3 of area 1) and the site of the project."""
    import rasterio
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    with rasterio.open(w.chunk.dem_path) as src:
        b = src.bounds
    x, y = (b.left + b.right) / 2, (b.bottom + b.top) / 2
    w.doc.meta["sito"] = SITE
    w.chunk.meta["drawings"] = {"units": [
        UnitPolygon(box(x + 0.3 * n, y, x + 0.3 * n + 0.2, y + 0.2), us=str(n + 1), area="1").to_dict()
        for n in range(3)]}
    w._reload_drawings(redraw=False)
    pane = _front(w, qapp)
    pane.set_document(w.doc)
    return pane


def test_records_box_off_by_default_and_needs_pyarchinit(win, qapp):
    pane = _units(win, qapp)
    assert not pane.records.isChecked() and not pane.records.isEnabled()  # no PyArchInit connected
    assert not pane.people.isChecked() and pane.people.isEnabled()
    tip = pane.people.toolTip()
    assert all(c in tip for c in ("schedatore", "direttore_us", "responsabile_us", "disegnatore"))
    o = pane.options()
    assert (o["records"], o["people"], o["usm_heights"]) == (False, False, "none")
    assert [pane.usm_heights.itemData(i) for i in range(pane.usm_heights.count())] == ["none", "relative",
                                                                                       "absolute"]
    pane.set_pa(FakePyArchInit())
    assert pane.records.isEnabled() and not pane.records.isChecked()
    pane.records.setChecked(True)
    pane.set_pa(None)  # the bridge went: the box cannot be used, the choice is kept
    assert not pane.records.isEnabled() and pane.options()["records"]


def test_records_note_counts_the_units(win, qapp):
    pane = _units(win, qapp)
    pane.products["units"].setChecked(True)
    fake = FakePyArchInit()
    pane.set_pa(fake)
    assert not pane.records_note.isVisibleTo(pane)
    pane.records.setChecked(True)
    assert fake.asked[-1] == [SITE]
    assert pane.records_note.isVisibleTo(pane)
    assert pane.records_note.text() == "2 unità su 3 con la scheda"
    tip = pane.records_note.toolTip()
    assert "Senza scheda" in tip and "US 3" in tip
    assert "US 2 (ignorando il tipo)" in tip  # matched at a looser step: what it ignored is said (spec 3.2)
    pane.products["units"].setChecked(False)  # the build matches the polygons only with the units
    assert pane.records_note.text() == "0 unità su 0 con la scheda"
    pane.products["units"].setChecked(True)
    assert pane.records_note.text() == "2 unità su 3 con la scheda"
    for i in range(pane.chunks.count()):  # no chunk: no unit
        pane.chunks.item(i).setCheckState(pane.chunks.item(i).checkState().Unchecked)
    assert pane.records_note.text() == "0 unità su 0 con la scheda"


def test_records_note_says_pyarchinit_cannot_be_read(win, qapp):
    pane = _units(win, qapp)
    pane.set_pa(FakePyArchInit(RuntimeError("database is locked")))
    pane.records.setChecked(True)
    assert pane.records.isChecked()  # the box stays ticked
    assert "PyArchInit non leggibile" in pane.records_note.text()
    assert "database is locked" in pane.records_note.toolTip()


def test_usm_heights_hidden_in_true_coordinates(win, qapp):
    pane = _units(win, qapp)
    assert pane.usm_heights.isVisibleTo(pane)
    pane.usm_heights.setCurrentIndex(pane.usm_heights.findData("absolute"))
    pane.frame_crs.setChecked(True)
    assert not pane.usm_heights.isVisibleTo(pane)
    assert pane.options()["usm_heights"] == "absolute"  # kept all the same
    pane.frame_local.setChecked(True)
    assert pane.usm_heights.isVisibleTo(pane)


def test_build_with_records_writes_them(win, qapp, tmp_path):
    w = win
    pane = _units(w, qapp)
    pane.set_pa(FakePyArchInit())
    pane.records.setChecked(True)
    assert "schede US" in pane.states["contents"].text()
    _build(w, qapp, pane, tmp_path / "sito", products=("dem", "units"))
    site_dir = tmp_path / "sito"
    records = json.loads((site_dir / "data" / "records.json").read_text(encoding="utf-8"))
    assert sorted(r["title"] for r in records.values()) == ["US 1 - Strato 1", "USM 2 - Strato 2"]
    manifest = json.loads((site_dir / "site.json").read_text(encoding="utf-8"))
    assert manifest["people"] is False
    assert "Mario Schedatore" not in (site_dir / "data" / "records.json").read_text(encoding="utf-8")


def test_build_without_pyarchinit_says_so(win, qapp, tmp_path, caplog):
    import logging

    w = win
    pane = _units(w, qapp)
    pane.set_pa(FakePyArchInit())
    pane.records.setChecked(True)
    pane.set_pa(FakePyArchInit(RuntimeError("database is locked")))
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry.gui"):
        _build(w, qapp, pane, tmp_path / "sito", products=("dem", "units"))
    assert not (tmp_path / "sito" / "data" / "records.json").exists()
    assert any("database is locked" in r.getMessage() for r in caplog.records)
    assert w.doc.meta["web_site"]["options"]["records"] is False  # recorded as not there
    pane.set_pa(None)  # no bridge: the box kept but the contents do not promise the records
    assert pane.records.isChecked() and not pane.records.isEnabled()
    assert "schede US" not in pane.states["contents"].text()
    pane.set_pa(FakePyArchInit())  # PyArchInit back: the site lacks the records the box asks
    assert "Le scelte del passo 1 sono cambiate" in pane.states["build"].text()


def test_options_kept_with_the_project(win, qapp, tmp_path):
    w = win
    pane = _units(w, qapp)
    pane.set_pa(FakePyArchInit())
    pane.records.setChecked(True)
    pane.people.setChecked(True)
    pane.usm_heights.setCurrentIndex(pane.usm_heights.findData("relative"))
    _build(w, qapp, pane, tmp_path / "sito", products=("dem", "units"))
    o = w.doc.meta["web_site"]["options"]
    assert (o["records"], o["people"], o["usm_heights"]) == (True, True, "relative")
    manifest = json.loads((tmp_path / "sito" / "site.json").read_text(encoding="utf-8"))
    assert manifest["people"] is True
    pane.records.setChecked(False)
    pane.people.setChecked(False)
    pane.usm_heights.setCurrentIndex(0)
    assert "Le scelte del passo 1 sono cambiate" in pane.states["build"].text()
    pane.set_document(w.doc)  # the pane opened again on the project: the three choices restored
    assert pane.records.isChecked() and pane.people.isChecked()
    assert pane.usm_heights.currentData() == "relative"
    assert "Le scelte del passo 1 sono cambiate" not in pane.states["build"].text()


def test_a_site_built_before_the_records_keeps_its_choices(win, qapp, tmp_path):
    """A site recorded before the three choices existed: its options lack them, they count as the defaults."""
    w = win
    pane = _front(w, qapp)
    _build(w, qapp, pane, tmp_path / "sito", products=("dem",))
    for k in ("records", "people", "usm_heights"):
        w.doc.meta["web_site"]["options"].pop(k)
    pane.set_document(w.doc)
    assert pane.step_states["build"] == "done"
    assert "Le scelte del passo 1 sono cambiate" not in pane.states["build"].text()
