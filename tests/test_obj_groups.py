"""OBJ exports with groups (one per source chunk, face label or connected piece) and their import back; the face
groups of a model kept in its PLY (io/mesh_ply ``facegroup``)."""
from __future__ import annotations

import cv2
import numpy as np
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.engine import model_merge
from pyarchinit_photogrammetry.engine.texturing import texture_pages
from pyarchinit_photogrammetry.io import mesh_import
from pyarchinit_photogrammetry.io.mesh_ply import read_mesh, write_mesh

from engine_gaps_helpers import checker, grid_mesh, new_doc, textured_chunk


def _merged(tmp_path):
    doc = new_doc(tmp_path)
    xyz, f, uv = grid_mesh(0.0, 0.0, 1.0, 1.0, 10, 10)
    a = textured_chunk(doc, "Anno 2024", xyz, f, uv, [checker(256, 256, (200, 40, 40))])
    xyz, f, uv = grid_mesh(2.0, 0.0, 3.0, 1.0, 10, 10)
    b = textured_chunk(doc, "Anno 2025", xyz, f, uv, [checker(256, 256, (40, 200, 40)), checker(512, 512, (40, 40, 200))],
                       texnumber=np.r_[np.zeros(100, np.int32), np.ones(100, np.int32)])
    xyz, f, uv = grid_mesh(4.0, 0.0, 4.5, 0.5, 4, 4)
    c = textured_chunk(doc, "C", xyz, f, uv, [checker(64, 64, (90, 90, 90))])
    merged, _ = model_merge.merge_models(doc, [a, b, c], label="Unito")
    doc.save()
    return doc, merged


def _blocks(obj_text):
    """(group, material, faces) of an OBJ in the order of the file."""
    out, g, m = [], None, None
    for line in obj_text.splitlines():
        if line.startswith("g "):
            g = line[2:]
        elif line.startswith("usemtl "):
            m = line[7:]
            out.append([g, m, 0])
        elif line.startswith("f "):
            out[-1][2] += 1
    return out


def test_face_groups_survive_the_ply_and_a_subset(tmp_path):
    _doc, merged = _merged(tmp_path)
    m = read_mesh(merged.product_path("model"))
    assert m.group_names == {0: "Anno 2024", 1: "Anno 2025", 2: "C"}
    sub = m.subset_faces(m.groups != 1)
    assert sorted(set(sub.groups.tolist())) == [0, 2] and len(sub.groups) == len(sub.faces)
    write_mesh(tmp_path / "sub.ply", sub)
    back = read_mesh(tmp_path / "sub.ply")
    assert np.array_equal(back.groups, sub.groups) and back.group_names == m.group_names
    sub.faces, sub.texcoords, sub.texnumber = sub.faces[:5], None, None  # faces changed by hand without their groups: the groups are dropped, not wrong
    write_mesh(tmp_path / "bad.ply", sub)
    assert read_mesh(tmp_path / "bad.ply").groups is None


def test_obj_by_source_chunk_with_a_material_per_page(tmp_path):
    _doc, merged = _merged(tmp_path)
    out = tmp_path / "out" / "unito.obj"
    merged.export_model(out, groups="source")
    blocks = _blocks(out.read_text())
    assert [b[0] for b in blocks] == ["Anno_2024", "Anno_2025", "Anno_2025", "C"]
    assert [b[1] for b in blocks] == ["material0", "material1", "material2", "material3"]
    assert [b[2] for b in blocks] == [200, 100, 100, 32]
    mtl = (tmp_path / "out" / "unito.mtl").read_text()
    assert mtl.count("newmtl") == 4
    for k in range(4):
        name = "unito_texture" + ("" if k == 0 else f"_{k}") + ".jpg"
        assert f"newmtl material{k}" in mtl and f"map_Kd {name}" in mtl and (tmp_path / "out" / name).exists()
    lines = out.read_text().splitlines()
    assert sum(ln.startswith("o ") for ln in lines) == 3


def test_obj_groups_and_pages_come_back_on_import(tmp_path):
    doc, merged = _merged(tmp_path)
    out = tmp_path / "out" / "unito.obj"
    merged.export_model(out, groups="source")
    ch, r = mesh_import.import_model_chunk(doc, out)
    assert r["groups"] == 3 and r["pages"] == 4
    m = read_mesh(ch.product_path("model"))
    assert m.group_names == {0: "Anno_2024", 1: "Anno_2025", 2: "C"}
    assert np.bincount(m.groups).tolist() == [200, 200, 32]
    # the pages in the order of the MTL: page k of the import is page k of the merged model
    for a, b in zip(texture_pages(merged), texture_pages(ch)):
        ia, ib = cv2.imread(str(a)), cv2.imread(str(b))
        assert ia.shape == ib.shape and np.abs(ia.astype(int) - ib.astype(int)).mean() < 3
    src = read_mesh(merged.product_path("model"))
    assert np.array_equal(np.bincount(m.texnumber), np.bincount(src.texnumber))


def test_obj_by_connected_piece_and_by_labels(tmp_path):
    _doc, merged = _merged(tmp_path)
    out = tmp_path / "pieces.obj"
    merged.export_model(out, groups="component")
    groups = [ln[2:] for ln in out.read_text().splitlines() if ln.startswith("g ")]
    assert groups == ["part_001", "part_002", "part_003"]
    counts = [b[2] for b in _blocks(out.read_text())]
    assert counts[0] == 200  # the largest first (the two pages of the second chunk are one piece)
    labels = np.array(["muro" if k % 2 else "pavimento" for k in range(432)])
    out = tmp_path / "labels.obj"
    merged.export_model(out, labels=labels)
    assert sorted(ln[2:] for ln in out.read_text().splitlines() if ln.startswith("g ")) == ["muro", "pavimento"]


def test_texture_capped_and_cli(tmp_path):
    doc, merged = _merged(tmp_path)
    out = tmp_path / "cap" / "m.obj"
    merged.export_model(out, groups="source", max_texture=128)
    for p in sorted((tmp_path / "cap").glob("*.jpg")):
        assert max(cv2.imread(str(p)).shape[:2]) <= 128
    res = CliRunner().invoke(app, ["export", str(doc.path), "--chunk", "Unito", "--model", str(tmp_path / "c.obj"),
                                   "--groups", "component"])
    assert res.exit_code == 0, res.output
    assert "g part_003" in (tmp_path / "c.obj").read_text()


def test_untextured_obj_groups(tmp_path):
    doc = new_doc(tmp_path)
    xyz, f, _uv = grid_mesh(0.0, 0.0, 1.0, 1.0, 2, 2)
    lines = [f"v {x} {y} {z}" for x, y, z in xyz] + ["o primo"] + [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in f[:4]]
    lines += ["o secondo"] + [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in f[4:]]
    (tmp_path / "plain.obj").write_text("\n".join(lines) + "\n")
    ch, r = mesh_import.import_model_chunk(doc, tmp_path / "plain.obj")
    m = read_mesh(ch.product_path("model"))
    assert m.group_names == {0: "primo", 1: "secondo"} and np.bincount(m.groups).tolist() == [4, 4]
    ch.export_model(tmp_path / "back.obj", groups="source")
    text = (tmp_path / "back.obj").read_text()
    assert "g primo" in text and "g secondo" in text and "mtllib" not in text
    assert isinstance(ppg.Document, type)
