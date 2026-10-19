"""Exports for the web: Cloud Optimized GeoTIFF, COPC, GLB, Cesium 3D Tiles."""
from __future__ import annotations

import json
import shutil

import numpy as np
import pytest


def _copy(processed, tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def test_cloud_optimized_geotiff(processed, tmp_path):
    import rasterio

    _doc, ch = _copy(processed, tmp_path)
    with rasterio.open(ch.orthomosaic_path) as src:
        masked = float((src.dataset_mask() == 0).mean())
    for comp, bands in (("jpeg", 3), ("webp", 4)):
        out = ch.export_orthomosaic(tmp_path / f"orto_{comp}.tif", cog=comp)
        with rasterio.open(out) as cog:
            assert cog.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG" and cog.count == bands
            assert cog.profile["compress"] == comp and cog.overviews(1)
            assert float((cog.dataset_mask() == 0).mean()) == pytest.approx(masked, abs=0.01)  # the alpha kept
    out = ch.export_dem(tmp_path / "dem.tif", cog="zstd")
    with rasterio.open(out) as cog, rasterio.open(ch.dem_path) as dem:
        assert cog.profile["compress"] == "zstd" and cog.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
        assert np.array_equal(cog.read(1, masked=True).filled(0), dem.read(1, masked=True).filled(0))
    from shapely.geometry import box

    cut = ch.export_dem(tmp_path / "dem_cut.tif", region=box(2480001, 4500001, 2480002, 4500002), cog="deflate")
    with rasterio.open(cut) as cog:
        assert cog.width < 300 and cog.profile["compress"] == "deflate"
    with pytest.raises(ValueError):
        ch.export_dem(tmp_path / "x.tif", cog="jpeg")


def test_copc(processed, tmp_path):
    import laspy

    _doc, ch = _copy(processed, tmp_path)
    pc = ch.point_cloud
    out = ch.export_point_cloud(tmp_path / "nuvola.copc.laz")
    with laspy.CopcReader.open(str(out)) as r:
        pts = r.query()
        assert len(pts) == len(pc) and r.copc_info.halfsize > 0
        coarse = r.query(resolution=r.copc_info.spacing * 4)
        assert 0 < len(coarse) <= len(pc)
    world = ch.to_world(pc.xyz)
    assert np.allclose(np.sort(np.asarray(pts.x)), np.sort(world[:, 0]), atol=1e-3)
    assert np.array_equal(np.sort(np.asarray(pts.classification)), np.sort(np.asarray(pc.classification)))


def test_glb_and_3dtiles(processed, tmp_path):
    import trimesh
    from pyproj import Transformer

    _doc, ch = _copy(processed, tmp_path)
    out = ch.export_model(tmp_path / "modello.glb")
    scene = trimesh.load(out)
    mesh = next(iter(scene.geometry.values()))
    meta = json.loads(out.with_suffix(".json").read_text())
    assert meta["crs"] == ch.crs and np.abs(np.asarray(mesh.vertices)).max() < 10  # local, small coordinates
    world = ch.to_world(ch.model.vertices)
    centre = (world.min(0) + world.max(0)) / 2
    assert np.allclose(meta["origin"], centre, atol=1e-6)
    most = max(len(ch.model.faces) // 8, 500)  # a few levels of tiles
    tileset = ch.export_3dtiles(tmp_path / "tiles", max_faces=most)
    ts = json.loads(tileset.read_text())
    assert ts["asset"]["version"] == "1.1" and ts["root"]["children"]

    def walk(node, parent_box=None, depth=0):
        assert (tileset.parent / node["content"]["uri"]).exists()
        box = np.array(node["boundingVolume"]["box"])
        if parent_box is not None:  # a child inside its parent
            assert np.all(np.abs(box[:3] - parent_box[:3]) <= np.abs(parent_box[[3, 7, 11]]) + 1e-6)
        kids = node.get("children", [])
        assert (node["geometricError"] == 0) == (not kids)
        return 1 + sum(walk(k, box, depth + 1) for k in kids)

    tiles = walk(ts["root"])
    assert tiles == len(list((tileset.parent / "tiles").glob("*.glb"))) > 4
    leaf = trimesh.load(tileset.parent / ts["root"]["children"][0]["content"]["uri"])
    assert len(next(iter(leaf.geometry.values())).faces) <= most * 1.2
    # the root on the globe: east-north-up at the centre of the model
    t = np.array(ts["root"]["transform"]).reshape(4, 4).T
    lon, lat = Transformer.from_crs(ch.crs, "EPSG:4326", always_xy=True).transform(centre[0], centre[1])
    ecef = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True).transform(lon, lat, centre[2])
    assert np.allclose(t[:3, 3], ecef, atol=1e-3) and np.allclose(t[:3, :3].T @ t[:3, :3], np.eye(3), atol=1e-9)
    assert np.dot(t[:3, 2], np.array(ecef) / np.linalg.norm(ecef)) > 0.99  # up points away from the earth


def test_command_line(processed, tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, _ch = _copy(processed, tmp_path)
    r = CliRunner().invoke(app, ["export", str(doc.path), "--ortho", str(tmp_path / "o.tif"), "--cog", "webp",
                                 "--dem", str(tmp_path / "d.tif"), "--tiles", str(tmp_path / "t")])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert (tmp_path / "t" / "tileset.json").exists() and (tmp_path / "d.tif").exists()
