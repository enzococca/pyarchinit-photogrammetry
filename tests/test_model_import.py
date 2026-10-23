"""Meshes of other programs as the model of a chunk (File > Importa > Modello 3D, ppg import-model)."""
from __future__ import annotations

import shutil

import cv2
import numpy as np
import pytest
from scipy.spatial import cKDTree

from pyarchinit_photogrammetry.core import versions
from pyarchinit_photogrammetry.engine.texturing import display_texture, texture_pages
from pyarchinit_photogrammetry.io import mesh_import
from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

RED, BLUE = (220, 30, 30), (30, 30, 220)


def _copy(processed, tmp_path, name="copia"):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / name
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def _two_textures_obj(folder, origin=(500000.0, 4500000.0, 100.0)):
    """Two squares side by side sharing an edge, one with a red texture and one with a blue one,
    in map coordinates."""
    from PIL import Image

    folder.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), RED).save(folder / "rosso.png")
    Image.new("RGB", (16, 16), BLUE).save(folder / "blu.png")
    (folder / "due.mtl").write_text("newmtl rosso\nKd 1 1 1\nmap_Kd rosso.png\n"
                                    "newmtl blu\nKd 1 1 1\nmap_Kd blu.png\n")
    x0, y0, z0 = origin
    v = np.array([(x0, y0, z0), (x0 + 1, y0, z0), (x0 + 1, y0 + 1, z0 + 0.5), (x0, y0 + 1, z0 + 0.5),
                  (x0 + 2, y0, z0), (x0 + 2, y0 + 1, z0 + 0.5)])
    lines = ["mtllib due.mtl"] + [f"v {a:.4f} {b:.4f} {c:.4f}" for a, b, c in v]
    lines += ["vt 0.1 0.1", "vt 0.9 0.1", "vt 0.9 0.9", "vt 0.1 0.9"]
    lines += ["usemtl rosso", "f 1/1 2/2 3/3", "f 1/1 3/3 4/4", "usemtl blu", "f 2/1 5/2 6/3", "f 2/1 6/3 3/4"]
    (folder / "due.obj").write_text("\n".join(lines) + "\n")
    return folder / "due.obj", v


def test_an_obj_with_two_textures_in_a_new_chunk(tmp_path):
    import pyarchinit_photogrammetry as ppg

    obj, v = _two_textures_obj(tmp_path / "file")
    doc = ppg.Document()
    doc.save(tmp_path / "progetto.ppg")
    ch, r = mesh_import.import_model_chunk(doc, obj, crs="EPSG:32633")
    assert ch.label == "due" and ch.crs == "EPSG:32633"
    assert np.allclose(ch.transform[:3, 3], np.floor(v.min(0)))  # metres from a round origin
    m = read_mesh(ch.product_path("model"))
    assert len(m.faces) == 4 and len(m.xyz) == 6  # the vertices of the shared edge joined
    world = ch.to_world(m.xyz)
    assert cKDTree(v).query(world)[0].max() < 1e-4
    assert r["pages"] == 2 and sorted(set(m.texnumber.tolist())) == [0, 1] and r["distance"] is None
    pages = texture_pages(ch)
    assert [p.name for p in pages] == ["model_texture.jpg", "model_texture_1.jpg"]
    for k, face in enumerate(m.faces):  # the left faces on the red page, the right ones on the blue page
        rgb = cv2.imread(str(pages[m.texnumber[k]]))[:, :, ::-1].reshape(-1, 3).mean(0)
        left = world[face, 0].mean() < v[0, 0] + 1
        assert (rgb[0] > 150) == left and (rgb[2] > 150) != left
    col = m.colors  # vertex colours read from the textures
    assert (col[world[:, 0] < v[0, 0] + 0.5, 0] > 150).all() and (col[world[:, 0] > v[0, 0] + 1.5, 2] > 150).all()
    assert versions.active_info(ch, "model")["name"] == "Importato da due.obj"
    doc.save()


@pytest.mark.parametrize("suffix", [".obj", ".ply", ".glb"])
def test_an_exported_model_comes_back_in_its_place(processed, tmp_path, suffix):
    doc, ch = _copy(processed, tmp_path)
    before = read_mesh(ch.product_path("model"))
    world0 = ch.to_world(before.xyz)
    out = ch.export_model(tmp_path / f"esportato{suffix}")
    r = ch.import_model(out)
    after = read_mesh(ch.product_path("model"))
    assert len(after.faces) == len(before.faces) and len(after.xyz) == len(before.xyz)
    assert cKDTree(world0).query(ch.to_world(after.xyz))[0].max() < (1e-3 if suffix == ".glb" else 1e-4)
    assert r["distance"] == 0.0 and r["pages"] == len(texture_pages(ch)) == 1
    assert ch.product_path("model_textured").exists() and after.texcoords is not None
    kept = versions.versions(ch, "model")
    assert len(kept) == 1 and r["kept"] == kept[0]["id"]
    assert versions.version_file(ch, "model", kept[0]["id"]).exists()
    assert versions.active_info(ch, "model")["name"] == f"Importato da esportato{suffix}"
    if suffix == ".ply" and "confidence" in before.vertices.dtype.names:
        assert np.array_equal(np.sort(after.vertices["confidence"]), np.sort(before.vertices["confidence"]))
    # the colours of the texture where the model had them
    near = cKDTree(ch.to_world(after.xyz)).query(world0)[1]
    diff = np.abs(after.colors[near].astype(int) - before.colors.astype(int)).mean()
    assert diff < 40


def test_the_chunks_own_model_edited_elsewhere_in_millimetres(processed, tmp_path):
    import trimesh

    doc, ch = _copy(processed, tmp_path)
    m = read_mesh(ch.product_path("model"))
    trimesh.Trimesh(m.xyz * 1000, m.faces, process=False).export(tmp_path / "mm.stl")  # no colours, mm
    r = ch.import_model(tmp_path / "mm.stl", frame="internal", scale=0.001, keep=False)
    after = read_mesh(ch.product_path("model"))
    assert len(after.faces) == len(m.faces) and len(after.xyz) == len(m.xyz)  # STL corners joined again
    assert cKDTree(m.xyz).query(after.xyz)[0].max() < 1e-5
    assert r["kept"] is None and versions.versions(ch, "model") == []
    assert after.colors is None and ch.product_path("model_textured") is None
    assert not list(ch.data_dir.glob("model_texture*.jpg"))
    assert ch.meta["model"]["source"] == "import" and "texture" not in ch.meta


def test_a_model_far_from_the_chunk_is_reported(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    out = ch.export_model(tmp_path / "modello.ply")
    r = ch.import_model(out, offset=(5000.0, 0.0, 0.0))
    assert r["distance"] > 4000


def test_a_failed_import_leaves_the_model_as_it_was(processed, tmp_path, monkeypatch):
    doc, ch = _copy(processed, tmp_path)
    model = ch.product_path("model")
    data, products = model.read_bytes(), dict(ch.products)
    bad = tmp_path / "rotto.obj"
    bad.write_text("questo non e' un modello\n")
    with pytest.raises(ValueError):
        ch.import_model(bad)
    out = ch.export_model(tmp_path / "buono.ply")

    def broken(*a, **k):
        raise OSError("disco pieno")

    monkeypatch.setattr(mesh_import, "_write_model", broken)
    with pytest.raises(OSError):
        ch.import_model(out)
    assert ch.products == products and ch.product_path("model").read_bytes() == data
    assert versions.versions(ch, "model") == [] and texture_pages(ch)
    with pytest.raises(ValueError):  # a CRS only for a chunk without data
        ch.import_model(out, crs="EPSG:32633")


def test_glb_is_y_up_and_brings_its_origin(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    out = ch.export_model(tmp_path / "web.glb")
    assert mesh_import.default_up(out) == "y" and mesh_import.default_up(tmp_path / "x.obj") == "z"
    origin = mesh_import.default_offset(out)
    assert np.abs(origin).max() > 0  # the centre of the model, from web.json
    m = mesh_import.read_mesh_file(out)
    local = mesh_import.to_file_frame(m.xyz, "y")
    world0 = ch.to_world(read_mesh(ch.product_path("model")).xyz)
    assert cKDTree(world0 - origin).query(local)[0].max() < 1e-3


def test_one_big_page_is_reduced_for_the_view(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    m = read_mesh(ch.product_path("model"))
    path, uv = display_texture(ch, m.texcoords, None, max_side=512)
    assert path.name == "model_texture_display.jpg" and max(cv2.imread(str(path)).shape[:2]) == 512
    assert uv is m.texcoords
    path, _uv = display_texture(ch, m.texcoords, None)  # 2048 px: the page itself
    assert path == texture_pages(ch)[0]


def test_import_model_from_the_command_line(processed, tmp_path):
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    doc, ch = _copy(processed, tmp_path)
    out = ch.export_model(tmp_path / "modello.obj")
    r = CliRunner().invoke(app, ["import-model", str(doc.path), str(out), "--new-chunk", "Dal file",
                                 "--crs", "EPSG:3004"])
    assert r.exit_code == 0, r.output
    again = ppg.Document.open(doc.path)
    new = next(c for c in again.chunks if c.label == "Dal file")
    assert new.crs == "EPSG:3004" and new.product_path("model").exists()
    w = new.to_world(read_mesh(new.product_path("model")).xyz)
    assert cKDTree(ch.to_world(read_mesh(ch.product_path("model")).xyz)).query(w)[0].max() < 1e-4
    r = CliRunner().invoke(app, ["import-model", str(doc.path), str(out), "--chunk", ch.label, "--name", "Da MeshLab"])
    assert r.exit_code == 0, r.output
    assert "kept as version" in r.output
    again = ppg.Document.open(doc.path)
    assert versions.active_info(again.chunk, "model")["name"] == "Da MeshLab"


def test_file_import_model_in_the_window(processed, tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from pyarchinit_photogrammetry.gui import main_window as mw

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    doc, ch = _copy(processed, tmp_path)
    obj, _v = _two_textures_obj(tmp_path / "file")
    seen = []

    def ask(cls, parent, title, fields, intro=""):
        seen.append({f.name: f for f in fields})
        answers = {f.name: f.default for f in fields}
        answers.update(target="new", label="Dal file", crs="EPSG:32633")
        return answers

    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(ask))
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(obj), "")))
    w = mw.MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    try:
        w.import_model()
        w.runner.wait()
        app.processEvents()
        assert {"target", "frame", "up", "scale", "dx", "dy", "dz", "crs", "keep"} <= set(seen[0])
        new = w.chunk
        assert new.label == "Dal file" and new.crs == "EPSG:32633" and len(doc.chunks) == 2
        assert read_mesh(new.product_path("model")).texnumber is not None
        before = len(versions.versions(ch, "model"))
        w._activate_chunk(ch)

        def ask_active(cls, parent, title, fields, intro=""):
            answers = {f.name: f.default for f in fields}
            answers.update(target="active", version_name="Da un altro programma")
            return answers

        monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(ask_active))
        warned = []
        monkeypatch.setattr(w, "_warn", lambda text, *a, **k: warned.append(text))
        w.import_model()  # map coordinates far from the trench: imported, with a warning
        w.runner.wait()
        app.processEvents()
        assert len(versions.versions(ch, "model")) == before + 1
        assert versions.active_info(ch, "model")["name"] == "Da un altro programma"
        assert warned and "m dai dati del chunk" in warned[0]
    finally:
        w.photos.stop()
        w.setWindowModified(False)
        w.close()
