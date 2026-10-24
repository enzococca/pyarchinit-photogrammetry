"""The workspace as Metashape's: chunks added, made active, renamed, enabled and disabled; anything
of the tree removed (with the right click, the toolbar or Delete) and brought back by Modifica >
Annulla while the project is open, the files of what is removed waiting in ``.removed`` until the
session ends (``core.removal``)."""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from pyarchinit_photogrammetry.core import removal, versions
from pyarchinit_photogrammetry.core.camera import Camera, Sensor
from pyarchinit_photogrammetry.core.document import Document
from pyarchinit_photogrammetry.core.marker import Projection
from pyarchinit_photogrammetry.core.pointcloud import PointCloud


def _raster(path, value: float, size=(40, 60), bands: int = 1, origin=(1000.0, 2000.0), res: float = 0.1,
            dtype: str = "float32"):
    import rasterio
    from rasterio.transform import from_origin

    path.parent.mkdir(parents=True, exist_ok=True)
    rows, cols = size
    data = np.full((bands, rows, cols), value, np.float32)
    data[:, :, : cols // 2] += 1.0
    if dtype == "uint8":  # an orthophoto: RGB and the alpha band
        data[-1] = 255
    with rasterio.open(path, "w", driver="GTiff", height=rows, width=cols, count=bands, dtype=dtype,
                       transform=from_origin(origin[0], origin[1], res, res), BIGTIFF="IF_SAFER") as dst:
        dst.write(data.astype(dtype))
    return path


def _cloud(n: int = 200, seed: int = 0) -> PointCloud:
    rng = np.random.default_rng(seed)
    xyz = rng.uniform(-1, 1, (n, 3))
    return PointCloud(xyz, (rng.uniform(0, 255, (n, 3))).astype(np.uint8))


def _mesh(path, pages=()):
    """A small sphere with vertex colours; ``pages``: the images of its texture (``TextureFile`` comments)."""
    import trimesh

    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh, write_mesh, write_mesh_ply

    m = trimesh.creation.icosphere(subdivisions=1)
    write_mesh_ply(path, np.asarray(m.vertices), np.asarray(m.faces),
                   np.tile(np.array([[200, 120, 60]], np.uint8), (len(m.vertices), 1)))
    if pages:
        mesh = read_mesh(path)
        mesh.texcoords = np.zeros((len(mesh.faces), 6), np.float32)
        mesh.texnumber = (np.arange(len(mesh.faces)) % len(pages)).astype(np.int32)
        mesh.set_texture_files(list(pages))
        write_mesh(path, mesh)
    return path


def _project(tmp_path, name: str = "progetto") -> Document:
    """A small project, saved: chunk «Scavo» with photos (two aligned), markers measured in them, a scale
    bar, tie points and the alignment model, depth maps, dense cloud, model with a texture of two pages,
    a DEM with a sidecar and a kept version, an orthomosaic of another program (outside the project),
    a relief raster, an elevation, the multispectral and thermal products; chunk «Rilievo 2» empty."""
    folder = tmp_path / name
    folder.mkdir()
    doc = Document()
    ch = doc.add_chunk("Scavo")
    rilievo = doc.add_chunk("Rilievo 2")
    doc.save(folder / f"{name}.ppg")
    (rilievo.data_dir / "nota.txt").write_text("rilievo")
    d = ch.data_dir
    ch.sensors[0] = Sensor(key=0, label="camera", width=60, height=40, model="OPENCV",
                           params=[50.0, 50.0, 30.0, 20.0, 0, 0, 0, 0])
    for k in range(4):
        pose = None
        if k < 2:  # two photos aligned, a metre apart, looking along +Z
            pose = np.eye(4)
            pose[0, 3] = float(k)
        ch.cameras.append(Camera(key=k, label=f"IMG_{k:04d}", path=str(folder / f"IMG_{k:04d}.jpg"), sensor_key=0,
                                 transform=pose))
    ch.meta["sfm"] = {"names": {str(k): f"IMG_{k:04d}.jpg" for k in range(4)}}
    m1, m2 = ch.add_marker("target 1"), ch.add_marker("target 2")
    target = np.array([[0.2, 0.1, 5.0]])
    for m, keys in ((m1, (0, 1)), (m2, (1,))):  # measured where the photos see the target
        for k in keys:
            px, _depth = ch.cameras[k].project(ch.sensors[0], target)
            m.projections[k] = Projection(float(px[0, 0]), float(px[0, 1]), True)
    ch.add_marker("sul modello", position=np.array([0.5, 0.5, 0.5]))  # placed in 3D: not measured in photos
    ch.add_scalebar(m1, m2, 1.0)
    ch.tie_points = _cloud(50, 1)
    (d / "sfm" / "0").mkdir(parents=True)
    (d / "sfm" / "0" / "cameras.bin").write_bytes(b"colmap")
    ch.products["reconstruction"] = "sfm/0"
    ch.meta["precision"] = {"cameras": 1}
    (d / "dense" / "stereo" / "depth_maps").mkdir(parents=True)
    for k in range(2):
        (d / "dense" / "stereo" / "depth_maps" / f"IMG_{k:04d}.jpg.geometric.bin").write_bytes(b"map")
    ch.meta["dense"] = {"backend": "cpu", "quality": "medium", "filter": "moderate"}
    ch.point_cloud = _cloud(300, 2)
    _mesh(d / "model.ply", pages=("model_texture.jpg", "model_texture_1.jpg"))
    ch.products["model"] = "model.ply"
    (d / "model_preview.ply").write_bytes((d / "model.ply").read_bytes())
    for page in ("model_texture.jpg", "model_texture_1.jpg"):
        import cv2

        cv2.imwrite(str(d / page), np.full((16, 16, 3), 128, np.uint8))
    ch.products["model_textured"] = "model_texture.jpg"
    _raster(d / "dem.tif", 100.0)
    (d / "dem.tif.aux.xml").write_text("<PAMDataset/>")
    ch.products["dem"] = "dem.tif"
    versions.set_active_name(ch, "dem", "Superficie")
    kept = versions.keep_active(ch, "dem")  # a kept copy: it stays when the DEM is removed
    versions.set_active_name(ch, "dem", "Terreno")
    ch.meta["dem_kept"] = kept["id"]
    other = _raster(tmp_path / "metashape" / "ortofoto_metashape.tif", 120.0, bands=4, dtype="uint8")
    ch._set_product("orthomosaic", other)  # a file of another program: never moved
    _raster(d / "terrain" / "svf.tif", 0.5)
    ch.products["svf"] = "terrain/svf.tif"
    ch.meta["relief"] = {"svf": {"radius": 5}}
    (d / "elevations" / "e1").mkdir(parents=True)
    _raster(d / "elevations" / "e1" / "ortho.tif", 1.0)
    ch.meta["elevations"] = [{"id": "e1", "name": "Parete sud", "size": [60, 40], "resolution": 0.1,
                              "orthophoto": "elevations/e1/ortho.tif", "relief": "elevations/e1/ortho.tif"}]
    ms = d / "multispectral"
    _raster(ms / "reflectance.tif", 0.2, bands=3)
    _raster(ms / "NDVI.tif", 0.6)
    _raster(ms / "NDVI_zone.tif", 1.0)
    gpkg = folder / "zone_NDVI.gpkg"  # beside the project: the user's file
    gpkg.write_bytes(b"gpkg")
    _raster(ms / "thermal.tif", 20.0)
    _raster(ms / "thermal_anomaly.tif", 0.4)
    ch.meta["multispectral"] = {"captures": 4, "bands": ["Green", "Red", "NIR"],
                                "reflectance": "multispectral/reflectance.tif",
                                "reflectance_bands": ["Green", "Red", "NIR"],
                                "indices": {"NDVI": {"file": "multispectral/NDVI.tif", "formula": "(NIR-Red)/(NIR+Red)"}},
                                "zoning": {"NDVI": {"raster": "multispectral/NDVI_zone.tif", "file": str(gpkg),
                                                    "classes": 2, "polygons": 3}}}
    ch.meta["thermal"] = {"file": "multispectral/thermal.tif", "resolution": 0.03,
                          "stats": {"p2": 18.0, "p98": 24.0},
                          "anomaly": {"file": "multispectral/thermal_anomaly.tif", "radius_m": 2.0}}
    ch.triangulate_markers()  # positions from the measurements, as the window computes them again
    assert m1.position is not None and np.allclose(m1.position, target[0], atol=1e-6)
    doc.save()
    return doc


def _tree(folder):
    """Every file under ``folder`` (relative) with its bytes: the state of the disk to compare."""
    out = {}
    for root, _dirs, files in os.walk(folder):
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, folder)
            if rel.startswith(removal.TRASH):
                continue
            with open(p, "rb") as fh:
                out[rel] = fh.read()
    return out


def _state(doc):
    """What the project says (its file as it would be written), to compare before and after."""
    return json.dumps({"meta": doc.meta, "chunks": [c.to_dict() for c in doc.chunks]}, sort_keys=True, default=str)


ALL_PRODUCTS = [("product", k) for k in ("tie_points", "depth_maps", "point_cloud", "model", "dem", "orthomosaic",
                                         "svf", "ms:reflectance", "ms:index:NDVI", "ms:zones:NDVI", "ms:thermal",
                                         "ms:thermal_anomaly")]


def test_products_removed_and_back_with_undo(tmp_path):
    doc = _project(tmp_path)
    ch = doc.chunks[0]
    files, state = _tree(doc.files_dir), _state(doc)
    outside = sorted(p.name for p in (tmp_path / "metashape").iterdir())
    gpkg = (tmp_path / "progetto" / "zone_NDVI.gpkg").read_bytes()
    r = removal.ItemsRemoval(ch, ALL_PRODUCTS + [("elevation", "e1")])
    r.apply()
    # out of the project...
    assert set(ch.products) == set()
    assert "dense" not in ch.meta and "svf" not in ch.meta.get("relief", {}) and ch.meta["elevations"] == []
    ms = ch.meta["multispectral"]
    assert "reflectance" not in ms and ms["indices"] == {} and ms["zoning"] == {} and ms["captures"] == 4
    assert ch.meta["thermal"] == {} and "dem" not in ch.meta.get("active_versions", {})
    assert [c.aligned for c in ch.cameras] == [False] * 4 and "precision" not in ch.meta
    assert ch.markers[0].position is None and list(ch.markers[2].position) == [0.5, 0.5, 0.5]  # placed in 3D: kept
    # ...the files aside, in the files folder of the project, the kept version where it was
    d = ch.data_dir
    for name in ("dem.tif", "dem.tif.aux.xml", "model.ply", "model_preview.ply", "model_texture.jpg",
                 "model_texture_1.jpg", "point_cloud.ply", "tie_points.ply", "sfm/0", "dense", "terrain/svf.tif",
                 "elevations/e1", "multispectral/NDVI.tif", "multispectral/thermal.tif"):
        assert not (d / name).exists(), name
        assert (r.folder / "present" / "chunk_0" / name).exists(), name
    assert versions.versions(ch, "dem")[0]["id"] == ch.meta["dem_kept"]
    assert versions.version_file(ch, "dem", ch.meta["dem_kept"]).exists()
    # what is not the chunk's own stays where it is: only the project stops pointing to it
    assert sorted(p.name for p in (tmp_path / "metashape").iterdir()) == outside
    assert (tmp_path / "progetto" / "zone_NDVI.gpkg").read_bytes() == gpkg
    r.undo()
    assert _state(doc) == state and _tree(doc.files_dir) == files
    assert [c.aligned for c in ch.cameras] == [True, True, False, False] and ch.tie_points is not None
    r.redo()
    assert not ch.products and not (d / "dem.tif").exists()
    r.undo()
    assert _state(doc) == state and _tree(doc.files_dir) == files


def test_a_product_built_again_after_its_removal_is_kept_by_undo_and_redo(tmp_path):
    """As Modifica > Annulla swaps the files of an edit: the DEM built after the removal goes aside when the
    removal is undone, and comes back with the redo."""
    doc = _project(tmp_path)
    ch = doc.chunks[0]
    old = (ch.data_dir / "dem.tif").read_bytes()
    r = removal.ItemsRemoval(ch, [("product", "dem")])
    r.apply()
    _raster(ch.data_dir / "dem.tif", 7.0)  # built again
    ch.products["dem"] = "dem.tif"
    versions.set_active_name(ch, "dem", "Nuovo")
    new = (ch.data_dir / "dem.tif").read_bytes()
    assert new != old
    r.undo()
    assert (ch.data_dir / "dem.tif").read_bytes() == old and versions.active_info(ch, "dem")["name"] == "Terreno"
    assert (ch.data_dir / "dem.tif.aux.xml").exists()
    r.redo()
    assert (ch.data_dir / "dem.tif").read_bytes() == new and versions.active_info(ch, "dem")["name"] == "Nuovo"
    assert not (ch.data_dir / "dem.tif.aux.xml").exists()


def test_photos_markers_and_scale_bars(tmp_path):
    doc = _project(tmp_path)
    ch = doc.chunks[0]
    state = _state(doc)
    cams = [ch.cameras[1], ch.cameras[3]]
    r = removal.ItemsRemoval(ch, [("cameras", cams), ("markers", [ch.markers[1]])])
    r.apply()
    assert [c.label for c in ch.cameras] == ["IMG_0000", "IMG_0002"]
    assert set(ch.meta["sfm"]["names"]) == {"0", "2"}  # an optimisation leaves the photos out
    assert set(ch.markers[0].projections) == {0} and [m.label for m in ch.markers] == ["target 1", "sul modello"]
    assert ch.scalebars == []  # it ended at the marker removed
    r.undo()
    assert _state(doc) == state
    # a scale bar alone; photos added meanwhile with the key of one removed: it cannot come back
    r2 = removal.ItemsRemoval(ch, [("scalebars", list(ch.scalebars))])
    r2.apply()
    assert ch.scalebars == []
    r2.undo()
    assert len(ch.scalebars) == 1
    r3 = removal.ItemsRemoval(ch, [("cameras", [ch.cameras[3]])])
    r3.apply()
    ch.cameras.append(Camera(key=3, label="nuova", path=str(tmp_path / "nuova.jpg"), sensor_key=0))
    with pytest.raises(removal.RemovalConflict):
        r3.undo()
    assert [c.label for c in ch.cameras][-1] == "nuova" and len(ch.cameras) == 4  # nothing moved
    # a chunk-level helper of the engine does the same for scripts: names of the alignment model gone
    ch.remove_cameras([ch.cameras[0]])
    assert "0" not in ch.meta["sfm"]["names"]


def test_chunks_removed_kept_aside_until_the_session_ends(tmp_path):
    doc = _project(tmp_path)
    scavo, rilievo = doc.chunks
    terzo = doc.add_chunk("Terzo")
    (terzo.data_dir / "dati.txt").write_text("3")
    doc.save()
    doc.chunk = scavo
    r = removal.ChunkRemoval(doc, [scavo, terzo])
    r.apply()
    assert doc.chunks == [rilievo] and r.was_active is scavo and "active_chunk" not in doc.meta
    assert not (doc.files_dir / "chunk_0").exists() and (r.folder / "present" / "chunk_0" / "dem.tif").exists()
    new = doc.add_chunk("Nuovo")
    assert new.key not in (scavo.key, terzo.key)  # the keys of the removed ones wait for an undo
    doc.chunks.remove(new)
    r.undo()
    assert [c.label for c in doc.chunks] == ["Scavo", "Rilievo 2", "Terzo"]
    assert (doc.files_dir / "chunk_0" / "dem.tif").exists() and (doc.files_dir / "chunk_2" / "dati.txt").exists()
    r.redo()
    # never saved since the removal: the end of the session puts the folders back (the project on disk has them)
    doc.settle_removals()
    assert (doc.files_dir / "chunk_0" / "dem.tif").exists() and not (doc.files_dir / removal.TRASH).exists()
    # saved without them: deleted for good
    doc2 = Document.open(doc.path)
    r = removal.ChunkRemoval(doc2, [doc2.chunks[0]])
    r.apply()
    doc2.save()
    doc2.settle_removals()
    assert not (doc2.files_dir / "chunk_0").exists() and [c.label for c in Document.open(doc.path).chunks] == \
        ["Rilievo 2", "Terzo"]
    # saved while removed, then undone and not saved again: the folder that came back is nobody's, it goes
    doc3 = Document.open(doc.path)
    r = removal.ChunkRemoval(doc3, [doc3.chunks[1]])
    r.apply()
    doc3.save()
    r.undo()
    doc3.settle_removals()
    assert not (doc3.files_dir / "chunk_2").exists()
    assert [c.label for c in Document.open(doc.path).chunks] == ["Rilievo 2"]


def test_items_settled_with_the_saved_project_and_recovered_after_a_crash(tmp_path):
    doc = _project(tmp_path)
    ch = doc.chunks[0]
    dem = ch.data_dir / "dem.tif"
    # removed and the project not saved: the DEM back at the end of the session
    removal.ItemsRemoval(ch, [("product", "dem")]).apply()
    doc.settle_removals()
    assert dem.exists() and not (doc.files_dir / removal.TRASH).exists()
    # removed and saved: deleted
    doc = Document.open(doc.path)
    ch = doc.chunks[0]
    removal.ItemsRemoval(ch, [("product", "dem")]).apply()
    doc.save()
    doc.settle_removals()
    assert not dem.exists() and "dem" not in Document.open(doc.path).chunks[0].products
    # a crash: the session never settled; opened again, what the saved project lists comes back
    doc = Document.open(doc.path)
    ch = doc.chunks[0]
    r1 = removal.ItemsRemoval(ch, [("product", "point_cloud")])
    r1.apply()
    r2 = removal.ChunkRemoval(doc, [doc.chunks[1]])
    r2.apply()
    saved_keys = [c.key for c in Document.open(doc.path).chunks]
    assert saved_keys == [0, 1]
    again = Document.open(doc.path)
    assert again.recover_removed() == {"restored": 2, "deleted": 0}
    assert (again.files_dir / "chunk_0" / "point_cloud.ply").exists() and (again.files_dir / "chunk_1").is_dir()
    assert not (again.files_dir / removal.TRASH).exists()
    # what the saved project no longer lists is deleted
    doc = Document.open(doc.path)
    removal.ChunkRemoval(doc, [doc.chunks[1]]).apply()
    doc.save()
    again = Document.open(doc.path)
    assert again.recover_removed() == {"restored": 0, "deleted": 1} and not (again.files_dir / "chunk_1").exists()


def test_a_file_that_cannot_be_moved_leaves_everything_as_it_was(tmp_path, monkeypatch):
    """A file in use (Windows does not move it): the files moved before it go back, the project is unchanged;
    the same when an undo cannot put a file back."""
    doc = _project(tmp_path)
    ch = doc.chunks[0]
    state, files = _state(doc), _tree(doc.files_dir)
    real = removal._move
    blocked = {"point_cloud.ply"}

    def move(src, dst):
        if src.name in blocked:
            raise OSError(f"{src.name} in use")
        real(src, dst)

    monkeypatch.setattr(removal, "_move", move)
    with pytest.raises(OSError):
        removal.ItemsRemoval(ch, [("product", "dem"), ("product", "model"), ("product", "point_cloud")]).apply()
    assert _state(doc) == state and _tree(doc.files_dir) == files
    blocked.add("chunk_0")  # the folder of a chunk in use
    with pytest.raises(OSError):
        removal.ChunkRemoval(doc, [ch]).apply()
    assert doc.chunks[0] is ch and _tree(doc.files_dir) == files
    blocked.clear()
    r = removal.ItemsRemoval(ch, [("product", "dem"), ("product", "point_cloud")])
    r.apply()
    blocked.add("point_cloud.ply")  # coming back from the trash: refused
    with pytest.raises(OSError):
        r.undo()
    assert r.state == "removed" and "dem" not in ch.products and not (ch.data_dir / "dem.tif").exists()
    blocked.clear()
    r.undo()
    assert _state(doc) == state and _tree(doc.files_dir) == files


def test_history_keeps_the_removals_when_another_chunk_becomes_active():
    """Entries that undo themselves (the removals): pushed among the edits of a chunk, kept when another
    chunk becomes the active one while the edits of the chunk are forgotten; one that fails goes away
    with what was undone after it."""
    from pyarchinit_photogrammetry.gui.undo import UndoHistory

    state = {"chunk": 0, "units": []}
    calls = []

    class Step:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        def undo(self):
            if self.fail:
                raise RuntimeError("non torna")
            calls.append(("undo", self.name))

        def redo(self):
            calls.append(("redo", self.name))

    h = UndoHistory(lambda: dict(state), lambda parts, names, products: state.update(parts), lambda: None)
    state["units"] = ["u1"]
    h.record()
    h.push("Rimuovi DEM", Step("dem"))
    state["units"] = ["u1", "u2"]
    h.record()
    assert [e.text for e in h.entries] == ["Disegni", "Rimuovi DEM", "Disegni"]
    assert h.undo() == "Disegni" and h.undo() == "Rimuovi DEM" and calls == [("undo", "dem")]
    assert h.redo() == "Rimuovi DEM" and calls[-1] == ("redo", "dem")
    h.reset(keep_document=True)  # another chunk active: the drawings of the first one are not its own
    assert [e.text for e in h.entries] == ["Rimuovi DEM"] and h.can_undo() and not h.can_redo()
    state["chunk"] = 1  # the same when the snapshot sees another chunk
    assert not h.record() and [e.text for e in h.entries] == ["Rimuovi DEM"]
    h.push("Rimuovi chunk «Scavo»", Step("chunk", fail=True))
    with pytest.raises(RuntimeError):
        h.undo()
    assert [e.text for e in h.entries] == ["Rimuovi DEM"] and h.index == 1 and h.undo() == "Rimuovi DEM"
    h.reset()  # another project: nothing stays
    assert not h.entries and not h.can_undo()


# ------------------------------------------------------------------------------ the window
@pytest.fixture
def qapp(tmp_path_factory):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    """The window on the project of :func:`_project`, with the menus of the workspace recorded (``menus``: the
    texts of each one opened; ``choose``: the text of the entry to trigger) and the questions answered by
    ``answers`` (the index of the button, None = Annulla) and recorded in ``asked``."""
    from PySide6.QtCore import QSettings

    import pyarchinit_photogrammetry.gui as gui
    from pyarchinit_photogrammetry.gui import main_window as mw

    monkeypatch.setattr(gui, "app_settings", lambda: QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat))
    doc = _project(tmp_path)
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.resize(1200, 800)
    w.show()
    qapp.processEvents()
    w.menus, w.choose, w.asked, w.answers = [], [], [], []

    class Menu(mw.QMenu):
        def exec(self, *args):
            texts = [a.text() for a in self.actions() if a.text()]
            w.menus.append(texts)
            want = w.choose.pop(0) if w.choose else None
            for a in self.actions():
                if want is not None and a.text() == want:
                    a.trigger()
                    break

    def answer(parent, kind, title, text, options, **kw):
        w.asked.append(text)
        return w.answers.pop(0) if w.answers else 0

    monkeypatch.setattr(mw, "QMenu", Menu)
    monkeypatch.setattr(mw, "choice", answer)
    yield w
    if w.isVisible():  # a test may close it itself
        w.setWindowModified(False)
        w.close()
    qapp.processEvents()


def _node(tree, kind, label=None, payload=None):
    """The node of the tree of ``kind`` (and of the chunk ``label``, of the ``payload``)."""
    from PySide6.QtCore import Qt

    for item in tree._walk():
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != kind:
            continue
        if label is not None and getattr(data[1], "label", None) != label:
            continue
        if payload is not None and not (data[2] == payload or getattr(data[2], "label", None) == payload):
            continue
        return item
    raise AssertionError(f"no {kind} {label} {payload} in the tree")


def _right_click(w, item) -> None:
    tree = w.workspace
    tree.scrollToItem(item)
    tree.customContextMenuRequested.emit(tree.visualItemRect(item).center())


def test_the_bar_of_the_workspace_adds_activates_renames_and_disables(window, monkeypatch, qapp, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QToolBar

    from pyarchinit_photogrammetry.gui import main_window as mw

    w = window
    dock = w.panes.dock(w.workspace)
    bar = dock.findChild(QToolBar, "paneToolbar")
    assert bar is w.workspace_toolbar and dock.widget().widget() is w.workspace  # the pane is still the tree
    assert bar.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    assert [a.text() for a in bar.actions() if a.text()] == [
        "Aggiungi chunk", "Aggiungi foto...", "Aggiungi cartella...", "Rendi attivo", "Attiva", "Disattiva",
        "Rinomina...", "Rimuovi"]
    assert all(a.toolTip() for a in bar.actions() if a.text())
    assert bar.geometry().bottom() < dock.widget().viewport().geometry().top() + 1  # above the tree, not over it
    doc = w.doc
    scavo, rilievo = doc.chunks
    assert w.chunk is scavo and not w.a_ws_remove.isEnabled() and not w.a_ws_activate.isEnabled()
    # a single click selects, it no longer makes the chunk active; Rendi attivo (or a double click) does
    item = _node(w.workspace, "chunk", "Rilievo 2")
    w.workspace.setCurrentItem(item)
    w.workspace.itemClicked.emit(item, 0)
    assert w.chunk is scavo and w.a_ws_activate.isEnabled() and w.a_ws_remove.isEnabled()
    w.a_ws_activate.trigger()
    assert w.chunk is rilievo and "[attivo]" in _node(w.workspace, "chunk", "Rilievo 2").text(0)
    w.workspace.itemDoubleClicked.emit(_node(w.workspace, "chunk", "Scavo"), 0)
    assert w.chunk is scavo
    # Aggiungi chunk: the name asked (non-native dialog), the new chunk the active one
    asked = []
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(
        lambda cls, parent, title, fields, intro="": asked.append((title, fields[0].default)) or {"label": "Fase 2"}))
    w.a_ws_add_chunk.trigger()
    assert asked == [("Nuovo chunk", "Chunk 3")] and w.chunk.label == "Fase 2" and len(doc.chunks) == 3
    assert w.isWindowModified()
    # F2 renames the chunk selected; Disattiva and Attiva
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {"label": "Rilievo finale"}))
    item = _node(w.workspace, "chunk", "Rilievo 2")
    w.workspace.setCurrentItem(item)
    from PySide6.QtTest import QTest

    QTest.keyClick(w.workspace, Qt.Key.Key_F2)
    assert rilievo.label == "Rilievo finale" and w.chunk.label == "Fase 2"  # renamed, not made active
    w.workspace.setCurrentItem(_node(w.workspace, "chunk", "Rilievo finale"))
    w.a_ws_disable.trigger()
    assert not rilievo.enabled and "(disattivato)" in _node(w.workspace, "chunk", "Rilievo finale").text(0)
    w.workspace.setCurrentItem(_node(w.workspace, "chunk", "Rilievo finale"))
    assert w.a_ws_enable.isEnabled() and not w.a_ws_disable.isEnabled()
    w.a_ws_enable.trigger()
    assert rilievo.enabled
    # Aggiungi foto: into the active chunk
    import cv2

    photo = tmp_path / "nuova.jpg"
    cv2.imwrite(str(photo), np.full((48, 64, 3), 90, np.uint8))
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(photo)], ""))
    w.a_ws_add_photos.trigger()
    w.runner.wait()
    qapp.processEvents()
    assert [c.label for c in w.chunk.cameras] == ["nuova"] and w.chunk.label == "Fase 2"
    # Rinomina... and Rimuovi of the bar, on the chunk selected
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {"label": "Fase 2 (scavo)"}))
    w.workspace.setCurrentItem(_node(w.workspace, "chunk", "Fase 2"))
    w.a_ws_rename.trigger()
    assert w.chunk.label == "Fase 2 (scavo)"
    fase = w.chunk
    w.workspace.setCurrentItem(_node(w.workspace, "chunk", "Fase 2 (scavo)"))
    w.a_ws_remove.trigger()
    assert "«Fase 2 (scavo)» è il chunk attivo: diventa attivo «Rilievo finale»." in w.asked[-1]
    assert doc.chunks == [scavo, rilievo] and w.chunk is rilievo
    w.undo()
    assert doc.chunks == [scavo, rilievo, fase] and w.chunk is fase and fase.cameras[0].label == "nuova"


def test_products_removed_from_the_menu_and_back_with_undo(window, qapp):
    w = window
    ch = w.chunk
    d = ch.data_dir
    state, files = _state(w.doc), _tree(w.doc.files_dir)
    dem = _node(w.workspace, "product", payload="dem")
    w.choose = ["Rimuovi"]
    _right_click(w, dem)
    assert w.menus[-1] == ["Mostra", "Conserva una copia come versione", "Rinomina questa versione...", "Rimuovi"]
    assert "DEM" in w.asked[-1] and "Le versioni conservate di DEM restano" in w.asked[-1]
    assert "dem" not in ch.products and not (d / "dem.tif").exists()
    assert w.a_undo.text() == "Annulla: Rimuovi DEM"
    labels = [it.text(0) for it in w.workspace._walk()]
    assert not any(t.startswith("DEM  (") for t in labels) and any(t.startswith("DEM  -  nessuna versione attiva")
                                                                   for t in labels)  # the kept version stays
    n = len(w.menus)
    _right_click(w, _node(w.workspace, "product", payload="dem"))  # its kept version only: nothing to show or remove
    assert len(w.menus) == n
    w.choose = ["Rimuovi"]
    _right_click(w, _node(w.workspace, "version", "Scavo"))
    assert "la versione «Superficie»" in w.asked[-1] and versions.versions(ch, "dem") == []
    assert w.a_undo.text() == "Annulla: Rimuovi la versione «Superficie»"
    w.undo()
    assert len(versions.versions(ch, "dem")) == 1 and versions.version_file(ch, "dem", ch.meta["dem_kept"]).exists()
    w.undo()
    assert ch.products["dem"] == "dem.tif" and (d / "dem.tif").exists() and w.a_redo.text() == "Ripeti: Rimuovi DEM"
    w.redo()
    assert "dem" not in ch.products
    w.undo()
    assert _state(w.doc) == state and _tree(w.doc.files_dir) == files
    # the tie points go with the alignment; the orthophoto of another program only leaves the project
    w.choose = ["Rimuovi (con l'allineamento)"]
    _right_click(w, _node(w.workspace, "product", payload="tie_points"))
    assert "l'allineamento" in w.asked[-1] and not any(c.aligned for c in ch.cameras)
    w.choose = ["Rimuovi"]
    _right_click(w, _node(w.workspace, "product", payload="orthomosaic"))
    assert "ortofoto_metashape.tif" in w.asked[-1] and "orthomosaic" not in ch.products
    assert (w.doc.path.parent.parent / "metashape" / "ortofoto_metashape.tif").exists()
    w.undo()
    w.undo()
    assert _state(w.doc) == state and _tree(w.doc.files_dir) == files
    # the depth maps, listed as Metashape lists them, and the products of the captures
    maps = _node(w.workspace, "product", payload="depth_maps")
    assert maps.text(0).startswith("Mappe di profondità  (2, qualità Media)")
    w.choose = ["Rimuovi"]
    _right_click(w, maps)
    assert "dense" not in ch.meta and not (d / "dense").exists()
    w.choose = ["Rimuovi"]
    _right_click(w, _node(w.workspace, "product", payload="ms:index:NDVI"))
    assert ch.meta["multispectral"]["indices"] == {}
    w.undo()
    w.undo()
    assert _state(w.doc) == state and _tree(w.doc.files_dir) == files


def test_the_confirmation_refused_changes_nothing(window):
    w = window
    state, files = _state(w.doc), _tree(w.doc.files_dir)
    w.answers = [None]  # Annulla
    w.choose = ["Rimuovi chunk"]
    _right_click(w, _node(w.workspace, "chunk", "Scavo"))
    assert len(w.asked) == 1 and _state(w.doc) == state and _tree(w.doc.files_dir) == files
    assert not w.a_undo.isEnabled() and not (w.doc.files_dir / removal.TRASH).exists()


def test_delete_with_several_items_selected(window, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    w = window
    ch = w.chunk
    state = _state(w.doc)
    tree = w.workspace
    _node(tree, "cameras", "Scavo").setExpanded(True)
    _node(tree, "markers", "Scavo").setExpanded(True)
    for kind, payload in (("camera", "IMG_0001"), ("camera", "IMG_0003"), ("marker", "target 2")):
        _node(tree, kind, "Scavo", payload).setSelected(True)
    tree.setFocus()
    QTest.keyClick(tree, Qt.Key.Key_Delete)
    assert "2 foto" in w.asked[-1] and "il marker «target 2»" in w.asked[-1]
    assert "Le barre di scala che finiscono sui marker rimossi vanno con loro." in w.asked[-1]
    assert [c.label for c in ch.cameras] == ["IMG_0000", "IMG_0002"] and [m.label for m in ch.markers] == \
        ["target 1", "sul modello"] and ch.scalebars == []
    assert w.a_undo.text() == "Annulla: Rimuovi 2 elementi"
    w.undo()
    assert _state(w.doc) == state
    # the right click on one of several items selected: the menu of the selection
    for kind, payload in (("camera", "IMG_0000"), ("camera", "IMG_0002")):
        _node(tree, kind, "Scavo", payload).setSelected(True)
    w.choose = ["Disattiva"]
    _right_click(w, _node(tree, "camera", "Scavo", "IMG_0002"))
    assert w.menus[-1] == ["Attiva", "Disattiva", "Rimuovi 2 elementi"]
    assert [c.enabled for c in ch.cameras] == [False, True, False, True]


def test_the_menu_of_a_chunk_that_is_not_the_active_one(window, monkeypatch, qapp):
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = window
    scavo, rilievo = w.doc.chunks
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {"label": "Rilievo 2b"}))
    w.choose = ["Rinomina..."]
    _right_click(w, _node(w.workspace, "chunk", "Rilievo 2"))
    assert w.menus[-1] == ["Rendi attivo", "Rinomina...", "Disattiva", "Rimuovi chunk"]
    assert rilievo.label == "Rilievo 2b" and w.chunk is scavo  # acts on it, the active chunk stays
    w.choose = ["Rimuovi chunk"]
    _right_click(w, _node(w.workspace, "chunk", "Rilievo 2b"))
    assert w.doc.chunks == [scavo] and w.chunk is scavo and not (w.doc.files_dir / "chunk_1").exists()
    w.undo()
    assert w.doc.chunks == [scavo, rilievo] and (w.doc.files_dir / "chunk_1" / "nota.txt").exists()
    # removing acts on the chunk of the item; showing what belongs to another chunk makes it the active one first
    w.workspace.itemDoubleClicked.emit(_node(w.workspace, "chunk", "Rilievo 2b"), 0)
    assert w.chunk is rilievo
    w.choose = ["Rimuovi"]
    _right_click(w, _node(w.workspace, "product", "Scavo", "svf"))
    assert "svf" not in scavo.products and w.chunk is rilievo
    w.choose = ["Mostra"]
    _right_click(w, _node(w.workspace, "product", "Scavo", "dem"))
    assert w.chunk is scavo and w.ortho_view.kind == "dem"
    w.undo()  # the history of «Scavo» starts again, the removal made from «Rilievo 2b» is still there
    assert scavo.products["svf"] == "terrain/svf.tif" and (scavo.data_dir / "terrain" / "svf.tif").exists()


def test_the_active_chunk_removed_kept_aside_until_saved_and_closed(window, qapp):
    w = window
    doc = w.doc
    scavo, rilievo = doc.chunks
    w.choose = ["Rimuovi chunk"]
    _right_click(w, _node(w.workspace, "chunk", "Scavo"))
    assert "«Scavo» è il chunk attivo: diventa attivo «Rilievo 2»." in w.asked[-1]
    assert doc.chunks == [rilievo] and w.chunk is rilievo and w.a_undo.text() == "Annulla: Rimuovi il chunk «Scavo»"
    trash = doc.files_dir / removal.TRASH
    assert not (doc.files_dir / "chunk_0").exists() and any(trash.rglob("dem.tif"))
    w.undo()  # back, the active chunk again, with its folder
    assert doc.chunks == [scavo, rilievo] and w.chunk is scavo and (doc.files_dir / "chunk_0" / "dem.tif").exists()
    assert w.model_view.chunk is scavo
    w.redo()
    assert doc.chunks == [rilievo] and w.chunk is rilievo
    # the last chunk too: the project gets an empty one, as a new project has
    w.choose = ["Rimuovi chunk"]
    _right_click(w, _node(w.workspace, "chunk", "Rilievo 2"))
    assert [c.label for c in doc.chunks] == ["Chunk 1"] and w.chunk.label == "Chunk 1" and not w.chunk.cameras
    assert "Non resta nessun chunk" in w.asked[-1]
    w.undo()
    assert doc.chunks == [rilievo] and w.chunk is rilievo  # the empty chunk went away again
    # saved without «Scavo», then closed: its files are deleted for good
    w.save_document()
    assert any(trash.rglob("dem.tif"))  # until the session ends
    w.close()
    qapp.processEvents()
    assert not (doc.files_dir / "chunk_0").exists() and not trash.exists()
    assert [c.label for c in Document.open(doc.path).chunks] == ["Rilievo 2"]


def test_a_removal_not_saved_comes_back_when_the_window_closes(window, qapp):
    w = window
    doc = w.doc
    w.choose = ["Rimuovi chunk"]
    _right_click(w, _node(w.workspace, "chunk", "Rilievo 2"))
    w.choose = ["Rimuovi"]
    _right_click(w, _node(w.workspace, "product", "Scavo", "point_cloud"))
    assert not (doc.files_dir / "chunk_1").exists() and not (doc.files_dir / "chunk_0" / "point_cloud.ply").exists()
    w.setWindowModified(False)  # closed without saving
    w.close()
    qapp.processEvents()
    assert (doc.files_dir / "chunk_1" / "nota.txt").exists() and (doc.files_dir / "chunk_0" / "point_cloud.ply").exists()
    assert not (doc.files_dir / removal.TRASH).exists()
    again = Document.open(doc.path)
    assert [c.label for c in again.chunks] == ["Scavo", "Rilievo 2"] and again.chunks[0].point_cloud is not None
