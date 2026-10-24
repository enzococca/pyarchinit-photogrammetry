"""Streaming access to large PLY meshes and the display copy of the 3D view."""
from __future__ import annotations

import os
import time

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")


def _sphere(tmp_path):
    from pyarchinit_photogrammetry.io.mesh_ply import write_mesh_ply

    m = trimesh.creation.icosphere(subdivisions=6, radius=5.0)  # 81920 faces
    colors = (np.abs(m.vertices) / 5.0 * 255).astype(np.uint8)
    return m, write_mesh_ply(tmp_path / "sphere.ply", m.vertices, m.faces, colors)


def test_stream_decimate_and_transform(tmp_path):
    from pyarchinit_photogrammetry.io.mesh_ply import PlyMesh, face_count, make_preview, transform_vertices

    m, path = _sphere(tmp_path)
    mesh = PlyMesh(path)
    assert mesh.n_faces == len(m.faces) == face_count(path) and mesh.has_colors
    chunks = list(mesh.face_chunks(size=10000))
    assert len(chunks) == 9 and np.array_equal(np.vstack(chunks), m.faces)
    prev = trimesh.load(make_preview(path, tmp_path / "prev.ply", 8000), process=False)
    assert 0.7 * 8000 <= len(prev.faces) <= 1.15 * 8000
    assert abs(prev.area - m.area) / m.area < 0.05 and np.abs(prev.bounds - m.bounds).max() < 0.2
    assert prev.visual.vertex_colors[:, :3].max() > 200  # colours averaged, not lost
    transform_vertices(path, lambda p: p + np.array([10.0, -2.0, 0.5]))
    moved = PlyMesh(path)
    assert np.allclose(moved.xyz().mean(axis=0), m.vertices.mean(axis=0) + [10.0, -2.0, 0.5], atol=1e-4)
    assert np.array_equal(np.vstack(list(moved.face_chunks())), m.faces)  # faces untouched


def test_a_read_mesh_does_not_keep_its_file_mapped(tmp_path):
    """Every array of read_mesh is a copy: a view of the memory map keeps the file mapped, and Windows
    refuses to replace, move or delete it (writing the cleaned model over it failed there)."""
    from pyarchinit_photogrammetry.io.mesh_ply import MeshData, read_mesh, write_mesh

    v = np.zeros(4, [("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1")])
    mesh = MeshData(v, np.array([[0, 1, 2], [1, 2, 3]]), np.zeros((2, 6), np.float32), ["TextureFile a.jpg",
                    "TextureFile b.jpg"], np.array([0, 1], np.int32))
    back = read_mesh(write_mesh(tmp_path / "m.ply", mesh))

    def mapped(a):
        while a is not None:
            if isinstance(a, np.memmap):
                return True
            a = getattr(a, "base", None)
        return False

    assert not any(mapped(a) for a in (back.vertices, back.faces, back.texcoords, back.texnumber))
    assert back.texture_files == ["a.jpg", "b.jpg"] and back.texnumber.tolist() == [0, 1]


def test_variable_face_lists_are_refused(tmp_path):
    from pyarchinit_photogrammetry.io.mesh_ply import PlyMesh

    head = ("ply\nformat binary_little_endian 1.0\nelement vertex 4\nproperty float x\nproperty float y\n"
            "property float z\nelement face 2\nproperty list uchar int vertex_indices\nend_header\n").encode()
    verts = np.zeros((4, 3), "<f4").tobytes()
    tri = np.array([3], "u1").tobytes() + np.array([0, 1, 2], "<i4").tobytes()
    quad = np.array([4], "u1").tobytes() + np.array([0, 1, 2, 3], "<i4").tobytes()
    (tmp_path / "mixed.ply").write_bytes(head + verts + tri + quad)
    with pytest.raises(ValueError):
        PlyMesh(tmp_path / "mixed.ply")


def test_display_model_uses_a_cached_preview(tmp_path):
    from pyarchinit_photogrammetry.core.document import Document
    from pyarchinit_photogrammetry.gui.scene import display_model

    doc = Document()
    doc.save(tmp_path / "p.ppg")
    ch = doc.add_chunk("A")
    m, path = _sphere(tmp_path)
    dst = ch.data_dir / "model.ply"
    dst.write_bytes(path.read_bytes())
    ch._set_product("model", dst)
    past = time.time() - 100
    os.utime(dst, (past, past))  # the model was built before its display copy
    shown = display_model(ch, max_faces=8000)
    preview = ch.data_dir / "model_preview.ply"
    assert preview.exists() and len(shown.faces) < 10000
    stamp = preview.stat().st_mtime
    assert len(display_model(ch, max_faces=8000).faces) == len(shown.faces) and preview.stat().st_mtime == stamp
    os.utime(preview, (past - 50, past - 50))  # older than the model: the model was rebuilt
    display_model(ch, max_faces=8000)
    assert preview.stat().st_mtime > past  # made again
    small = display_model(ch, max_faces=10 ** 6)  # a fresh copy is used whatever the limit
    assert len(small.faces) < 10000
