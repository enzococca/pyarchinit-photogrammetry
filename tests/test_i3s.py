"""I3S scene layer package of the model: the tree of nodes, the geometry of every node in metres
from its box, a texture a power of two on each side, the hash table of the paths."""
from __future__ import annotations

import gzip
import struct
import zipfile

import cv2
import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.io import i3s


def test_package_of_the_trench(processed, tmp_path):
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    _, ch, _ = processed
    path = i3s.export_slpk(ch, tmp_path / "scavo.slpk", max_faces=2000, texture_size=512, mode="local")
    pkg = i3s.read_slpk(path)
    layer, nodes = pkg["layer"], pkg["nodes"]
    assert layer["layerType"] == "IntegratedMesh" and layer["spatialReference"]["wkid"] == 3004
    assert layer["store"]["version"] == "1.7" and layer["nodePages"]["nodesPerPage"] == i3s.NODES_PER_PAGE
    assert [n["index"] for n in nodes] == list(range(len(nodes))) and "mesh" not in nodes[0]
    assert len(nodes) > 5 and all("mesh" in n for n in nodes[1:])
    mesh = read_mesh(ch.product_path("model"))
    world = mesh.xyz @ ch.transform[:3, :3].T + ch.transform[:3, 3]
    lo, hi = world.min(axis=0), world.max(axis=0)
    leaf_faces = 0
    with zipfile.ZipFile(path) as z:
        assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())  # the archive itself not compressed
        assert z.infolist()[-1].filename == i3s.HASH_TABLE
        for n in nodes[1:]:
            k = n["mesh"]["geometry"]["resource"]
            g = i3s.read_geometry_buffer(gzip.decompress(z.read(f"nodes/{k}/geometries/0.bin.gz")))
            count = n["mesh"]["geometry"]["vertexCount"]
            assert len(g["position"]) == count and count % 3 == 0 and g["faceRange"].tolist() == [[0, count // 3 - 1]]
            pts = g["position"] + np.array(n["obb"]["center"])
            assert (pts >= lo - 0.01).all() and (pts <= hi + 0.01).all()  # metres from the centre of the box
            assert np.abs(g["position"]).max(axis=0).tolist() <= [h + 1e-3 for h in n["obb"]["halfSize"]]
            assert np.allclose(np.linalg.norm(g["normal"], axis=1), 1, atol=1e-3)
            assert (g["uv0"] >= 0).all() and (g["uv0"] <= 1).all()
            img = cv2.imdecode(np.frombuffer(z.read(f"nodes/{k}/textures/0.jpg"), np.uint8), cv2.IMREAD_COLOR)
            h, w = img.shape[:2]
            assert h == w and w & (w - 1) == 0 and n["mesh"]["material"]["texelCountHint"] == w * h
            if "children" not in n:
                leaf_faces += count // 3
            else:
                assert n["lodThreshold"] == pytest.approx(i3s.LOD_PIXELS_PER_FACE * count / 3)
                assert all(nodes[c]["parentIndex"] == n["index"] for c in n["children"])
        # the colour of the leaves where they face the sky: the orthophoto of the same place
        diffs = []
        with rasterio.open(ch.orthomosaic_path) as src:
            for n in nodes[1:]:
                if "children" in n:
                    continue
                k = n["mesh"]["geometry"]["resource"]
                g = i3s.read_geometry_buffer(gzip.decompress(z.read(f"nodes/{k}/geometries/0.bin.gz")))
                img = cv2.imdecode(np.frombuffer(z.read(f"nodes/{k}/textures/0.jpg"), np.uint8), cv2.IMREAD_COLOR)
                tri = (g["position"] + np.array(n["obb"]["center"])).reshape(-1, 3, 3)
                up = g["normal"].reshape(-1, 3, 3).mean(axis=1)[:, 2] > 0.95
                c = tri[up].mean(axis=1)[::7]
                uv = g["uv0"].reshape(-1, 3, 2)[up].mean(axis=1)[::7]
                size = img.shape[0]
                tex = img[np.clip((uv[:, 1] * size).astype(int), 0, size - 1),
                          np.clip((uv[:, 0] * size).astype(int), 0, size - 1)][:, ::-1].astype(float)
                ortho = np.array([v for v in src.sample([tuple(p) for p in c[:, :2]])], float)
                ok = ortho[:, 3] > 0
                diffs += list(np.abs(tex[ok] - ortho[ok, :3]).mean(axis=1))
        assert len(diffs) > 100 and np.median(diffs) < 20  # texture coordinates from the top-left corner
    assert leaf_faces == len(mesh.faces)
    # every path of the hash table points at the local header of that file
    assert set(pkg["hash"]) == set(pkg["files"]) - {i3s.HASH_TABLE}
    raw = path.read_bytes()
    for name, offset in pkg["hash"].items():
        assert raw[offset:offset + 4] == b"PK\x03\x04"
        n_len = struct.unpack("<H", raw[offset + 26:offset + 28])[0]
        assert raw[offset + 30:offset + 30 + n_len].decode() == name


def test_global_mode(processed, tmp_path):
    """WGS84: the boxes east, north, up at their centre, the vertices in degrees and metres from it,
    normals earth-centred."""
    from pyproj import Transformer
    from scipy.spatial.transform import Rotation

    _, ch, _ = processed
    path = i3s.export_slpk(ch, tmp_path / "scavo.slpk", max_faces=4000, texture_size=256)
    pkg = i3s.read_slpk(path)
    layer, nodes = pkg["layer"], pkg["nodes"]
    assert layer["spatialReference"]["wkid"] == 4326 and layer["store"]["normalReferenceFrame"] == "earth-centered"
    lon0, lat0, lon1, lat1 = layer["store"]["extent"]
    assert -180 < lon0 < lon1 < 180 and -90 < lat0 < lat1 < 90
    back = Transformer.from_crs("EPSG:4326", ch.crs, always_xy=True)
    to_ecef = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    with zipfile.ZipFile(path) as z:
        for n in nodes[1:]:
            g = i3s.read_geometry_buffer(gzip.decompress(z.read(f"nodes/{n['index']}/geometries/0.bin.gz")))
            c = np.array(n["obb"]["center"])
            lonlat = g["position"].astype(np.float64) + c
            x, y = back.transform(lonlat[:, 0], lonlat[:, 1])
            assert np.ptp(x) < 10 and np.ptp(y) < 10  # the trench, metres again
            rot = Rotation.from_quat(n["obb"]["quaternion"]).as_matrix()
            up = rot[:, 2]  # the box axes: east, north, up at the centre
            centre = np.array(to_ecef.transform(*c))
            assert np.dot(up, centre / np.linalg.norm(centre)) > 0.99
            ecef = np.column_stack(to_ecef.transform(lonlat[:, 0], lonlat[:, 1], lonlat[:, 2]))
            local = (ecef - centre) @ rot
            assert (np.abs(local) <= np.array(n["obb"]["halfSize"]) + 1e-3).all()
            # the earth-centred normals agree with the faces they belong to
            tri = local.reshape(-1, 3, 3)
            face = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
            face /= np.maximum(np.linalg.norm(face, axis=1, keepdims=True), 1e-12)
            stored = g["normal"].reshape(-1, 3, 3).mean(axis=1) @ rot
            stored /= np.maximum(np.linalg.norm(stored, axis=1, keepdims=True), 1e-12)
            assert np.median((face * stored).sum(axis=1)) > 0.9


def test_hash_table_order():
    rows = i3s.hash_table([("nodes/1/textures/0.jpg", 10), ("3dSceneLayer.json.gz", 0), ("NodePages/0.json.gz", 99)])
    keys = [struct.unpack("<QQ", rows[k:k + 16]) for k in range(0, len(rows), 24)]
    assert len(rows) == 72 and keys == sorted(keys)


def test_local_coordinates_are_refused(processed, tmp_path):
    with pytest.raises(ValueError):
        i3s.projected_epsg("LOCAL")
    with pytest.raises(ValueError):
        i3s.projected_epsg("EPSG:4326")  # geographic: degrees
    assert i3s.projected_epsg("EPSG:32633") == 32633


def test_cli_and_window(processed, tmp_path, monkeypatch):
    import os

    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, ch, _ = processed
    res = CliRunner().invoke(app, ["export", str(doc.path), "--i3s", str(tmp_path / "a.slpk"), "--i3s-local"])
    assert res.exit_code == 0, res.output
    assert i3s.read_slpk(tmp_path / "a.slpk")["layer"]["spatialReference"]["wkid"] == 3004
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app_ = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {
        "out": str(tmp_path / "b.slpk"), "mode": "global", "faces": 4000, "texture": 1024}))
    monkeypatch.setattr(w, "_info", lambda *a, **k: None)
    w.export_i3s()
    w.runner.wait()
    app_.processEvents()
    assert i3s.read_slpk(tmp_path / "b.slpk")["layer"]["spatialReference"]["wkid"] == 4326
    w.setWindowModified(False)
    w.close()
