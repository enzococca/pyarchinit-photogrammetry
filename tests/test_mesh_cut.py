"""Cutting the model with planes: one side, a slice, both parts, a series (Taglia, ppg cut-model)."""
from __future__ import annotations

import shutil
from collections import Counter

import numpy as np
import pytest

from pyarchinit_photogrammetry.core import versions
from pyarchinit_photogrammetry.engine import mesh_cut
from pyarchinit_photogrammetry.io.mesh_ply import MeshData, read_mesh


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


def _box(n=6):
    """A closed box 2 x 2 x 2 made of small triangles, outward faces; red = 100 z + 50, the
    texture coordinates u = (x + 1) / 4, v = (z + 1) / 4 at every corner (linear: a cut must keep
    them exact)."""
    import trimesh

    m = trimesh.creation.box(extents=(2, 2, 2))
    for _ in range(n // 3):
        m = m.subdivide()
    v = np.asarray(m.vertices, np.float64)
    f = np.asarray(m.faces, np.int64)
    arr = np.empty(len(v), [("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("red", "u1"), ("green", "u1"),
                            ("blue", "u1"), ("confidence", "u1")])
    arr["x"], arr["y"], arr["z"] = v.T
    arr["red"] = np.rint(100 * v[:, 2] + 150)
    arr["green"], arr["blue"] = 20, 200
    arr["confidence"] = np.rint(10 * (v[:, 0] + 1))
    corners = v[f]
    tex = np.stack([(corners[..., 0] + 1) / 4, (corners[..., 2] + 1) / 4], -1).reshape(-1, 6).astype(np.float32)
    return MeshData(arr, f, tex, ["TextureFile t.jpg"], None)


def _area(xyz, faces):
    c = xyz[faces]
    return 0.5 * np.linalg.norm(np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0]), axis=1)


def test_a_cut_keeps_one_side_with_colours_texture_and_orientation():
    box = _box()
    p, n = mesh_cut.horizontal(0.3)
    up = mesh_cut.clip(box, p, n, "above")
    xyz = up.xyz
    assert xyz[:, 2].min() >= 0.3 - 1e-6 and len(up.faces)
    # the area: the top, and the four sides from 0.3 to 1
    assert np.isclose(_area(xyz, up.faces).sum(), 4 + 4 * 2 * 0.7, rtol=1e-4)
    on = np.isclose(xyz[:, 2], 0.3, atol=1e-6)
    assert on.sum() > 8 and np.allclose(up.vertices["red"][on], 180, atol=1)  # 100 * 0.3 + 150
    corners = xyz[up.faces]
    tex = up.texcoords.reshape(-1, 3, 2)
    assert np.allclose(tex[..., 0], (corners[..., 0] + 1) / 4, atol=1e-5)
    assert np.allclose(tex[..., 1], (corners[..., 2] + 1) / 4, atol=1e-5)
    # outward normals stay outward: the normals of the faces point away from the centre of the box
    c = xyz[up.faces]
    normals = np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0])
    assert (np.einsum("ij,ij->i", normals, c.mean(axis=1)) > 0).all()
    # the cut is one closed line: every edge of the boundary lies on the plane, used once
    edges = Counter(tuple(sorted(e)) for f in up.faces.tolist() for e in ((f[0], f[1]), (f[1], f[2]), (f[2], f[0])))
    border = [e for e, k in edges.items() if k == 1]
    assert border and all(on[a] and on[b] for a, b in border)
    down = mesh_cut.clip(box, p, n, "below")
    assert np.isclose(_area(down.xyz, down.faces).sum(), 4 + 4 * 2 * 1.3, rtol=1e-4)


def test_slab_series_and_planes():
    box = _box()
    p, n = mesh_cut.horizontal(0.0)
    s = mesh_cut.slab(box, p, n, 0.5)
    assert s.xyz[:, 2].min() >= -0.25 - 1e-6 and s.xyz[:, 2].max() <= 0.25 + 1e-6
    assert np.isclose(_area(s.xyz, s.faces).sum(), 4 * 2 * 0.5, rtol=1e-4)
    parts = mesh_cut.series(box, p, n, 0.2, 0.5)
    assert [round(o, 3) for o, _m in parts] == [-1.0, -0.5, 0.0, 0.5, 1.0]
    # planes: the left of the walker, counter-clockwise points give the normal up, a dip to the east
    _p, nv = mesh_cut.vertical((0, 0), (1, 0))
    assert np.allclose(nv, [0, 1, 0])
    _p, nt = mesh_cut.through_points((0, 0, 0), (1, 0, 0), (0, 1, 0))
    assert np.allclose(nt, [0, 0, 1])
    _p, nd = mesh_cut.dipping((0, 0, 0), 90, 30)
    assert nd[0] > 0.49 and abs(nd[1]) < 1e-9 and nd[2] > 0.86
    with pytest.raises(ValueError):
        mesh_cut.through_points((0, 0, 0), (1, 1, 1), (2, 2, 2))
    # a plane through vertices leaves no slivers: no face of zero area, no vertex twice
    q, m = mesh_cut.horizontal(0.0)
    half = mesh_cut.clip(box, q, m, "above")
    assert _area(half.xyz, half.faces).min() > 1e-9
    assert len(np.unique(np.round(half.xyz, 6), axis=0)) == len(half.xyz)


def test_cut_the_model_of_the_chunk(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    before = read_mesh(ch.product_path("model"))
    world = ch.to_world(before.xyz)
    z = float(np.median(world[:, 2]))
    r = ch.cut_model(*mesh_cut.horizontal(z), keep="below")
    after = read_mesh(ch.product_path("model"))
    w = ch.to_world(after.xyz)
    assert w[:, 2].max() <= z + 1e-6 and 0 < r["faces_kept"] == len(after.faces) < len(before.faces)
    assert after.texcoords is not None and ch.product_path("model_textured").exists()
    assert r["kept"] and len(versions.versions(ch, "model")) == 1
    assert versions.active_info(ch, "model")["name"] == "Tagliato: parte sotto il piano"
    versions.activate(ch, "model", r["kept"])  # back to the whole model
    assert len(read_mesh(ch.product_path("model")).faces) == len(before.faces)
    # both parts as files: together the whole model and the cut
    r = ch.cut_model(*mesh_cut.horizontal(z), keep="both", out=tmp_path / "parti" / "trincea.ply")
    names = sorted(p.split("/")[-1] for p in r["files"])
    assert names == ["trincea_sopra.ply", "trincea_sotto.ply"]
    faces = [read_mesh(f).faces.shape[0] for f in r["files"]]
    assert sum(faces) >= len(before.faces)
    up = read_mesh(next(f for f in r["files"] if f.endswith("sopra.ply")))
    assert up.xyz[:, 2].min() >= z - 1e-6  # world coordinates in the file
    assert (tmp_path / "parti" / "trincea_sopra_texture.jpg").exists()
    # a series of vertical slices along x, as OBJ-free PLY files in a folder
    x0 = float(np.median(world[:, 0]))
    r = ch.cut_model(np.array([x0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]), keep="series", thickness=0.05, step=0.5,
                     out=tmp_path / "fette")
    assert len(r["files"]) >= 2
    for f in r["files"]:
        xs = read_mesh(f).xyz[:, 0]
        off = float(f.split("fetta_")[1][:-4])
        assert xs.min() >= x0 + off - 0.025 - 1e-6 and xs.max() <= x0 + off + 0.025 + 1e-6
    # a slab to an OBJ with the texture, the model unchanged
    r = ch.cut_model(*mesh_cut.horizontal(z), keep="slab", thickness=0.1, out=tmp_path / "fetta.obj")
    assert (tmp_path / "fetta.obj").exists() and (tmp_path / "fetta.mtl").exists()
    assert len(read_mesh(ch.product_path("model")).faces) == len(before.faces)
    with pytest.raises(ValueError):
        ch.cut_model(*mesh_cut.horizontal(world[:, 2].max() + 10), keep="above")


def test_cut_model_from_the_command_line(processed, tmp_path):
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    doc, ch = _copy(processed, tmp_path)
    z = float(np.median(ch.to_world(read_mesh(ch.product_path("model")).xyz)[:, 2]))
    r = CliRunner().invoke(app, ["cut-model", str(doc.path), "--horizontal", str(z), "--keep", "above"])
    assert r.exit_code == 0, r.output
    assert "kept as version" in r.output
    again = ppg.Document.open(doc.path)
    w = again.chunk.to_world(read_mesh(again.chunk.product_path("model")).xyz)
    assert w[:, 2].min() >= z - 1e-6
    r = CliRunner().invoke(app, ["cut-model", str(doc.path), "--horizontal", str(z), "--vertical", "0,0,1,1"])
    assert r.exit_code != 0


def test_the_cut_button_of_the_3d_view(processed, tmp_path, monkeypatch):
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
    m = read_mesh(ch.product_path("model"))
    picked = m.xyz[np.argsort(m.xyz[:, 2])[len(m.xyz) // 2]]  # a vertex at the median height
    z = float(ch.to_world(picked[None])[0, 2])
    seen = []

    def ask(cls, parent, title, fields, intro=""):
        f = {x.name: x for x in fields}
        seen.append(f)
        answers = {x.name: x.default for x in fields}
        answers.update(part="above", result="version", version_name="Sopra la meta'")
        return answers

    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(ask))
    w = mw.MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    try:
        w._show_model("model")
        app.processEvents()
        assert w.model_view.b_cut.isVisibleTo(w.model_view)
        w.model_view.cut_requested.emit("horizontal", [picked])
        w.runner.wait()
        app.processEvents()
        assert seen[0]["plane"].default == "horizontal" and abs(seen[0]["z"].default - z) < 1e-6
        after = ch.to_world(read_mesh(ch.product_path("model")).xyz)
        assert after[:, 2].min() >= z - 1e-6
        assert versions.active_info(ch, "model")["name"] == "Sopra la meta'"
    finally:
        w.photos.stop()
        w.setWindowModified(False)
        w.close()
