"""Imports (Metashape, WebODM, Photo3D), the QGIS live link, the SAM worker and the IFC reader."""
from __future__ import annotations

import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

PSX = Path("/Volumes/Extreme Pro/Dropbox/missione siria/drone/tell dlamah/tell_dlamah.psx")


# ---------------------------------------------------------------- Photo3D
def test_gcp_list_and_crs():
    from pyarchinit_photogrammetry.integrations.photo3d import odm_crs, parse_gcp_list

    assert odm_crs("EPSG:3004") == "EPSG:3004"
    assert odm_crs("+proj=utm +zone=37 +datum=WGS84 +units=m +no_defs") == "EPSG:32637"
    assert odm_crs("+proj=utm +zone=19 +south +datum=WGS84") == "EPSG:32719"
    g = parse_gcp_list("EPSG:32633\n500000 4500000 100 10 20 a.jpg\n500000 4500000 100 30 40 b.jpg\n"
                       "500010 4500000 101 5 6 a.jpg T2\n")
    assert g["crs"] == "EPSG:32633"
    assert [p[0] for p in g["points"]] == ["GCP 1", "GCP 1", "T2"]


def test_point_on_ray_plane():
    from pyarchinit_photogrammetry.integrations.photo3d import _point_on_ray

    rng = np.random.default_rng(0)
    pts = np.column_stack([rng.uniform(-1, 1, 30000), rng.uniform(-1, 1, 30000), rng.normal(0, 0.002, 30000)])
    o = np.array([0.3, -0.2, 2.0])
    d = np.array([0.05, 0.02, -1.0])
    d /= np.linalg.norm(d)
    p = _point_on_ray(pts, o, d, 0.05)
    assert abs(p[2]) < 0.003 and np.linalg.norm(np.cross(p - o, d)) < 1e-9


@pytest.mark.slow
def test_photo3d_job_with_tap_scale(tmp_path):
    """A Photo3D session of the turntable bowl with a scale bar tapped between two targets:
    the engine contract (PROGRESS, result.json, products) and the metric scale."""
    import io
    import itertools
    import re

    import cv2

    from pyarchinit_photogrammetry.core.document import Document
    from pyarchinit_photogrammetry.integrations.photo3d import run_session
    from pyarchinit_photogrammetry.markers.targets import detect, turntable_layout
    from pyarchinit_photogrammetry.testing.synthetic import write_pottery_dataset

    write_pottery_dataset(tmp_path / "t")
    s = tmp_path / "session"
    shutil.copytree(tmp_path / "t" / "photos", s / "photos")
    img = cv2.imread(str(s / "photos" / "VASO_0000.jpg"))
    h, w = img.shape[:2]
    det = {int(d["code"]): np.asarray(d["center"], float) + 0.5 for d in detect(img, "aruco_4x4_50")}  # COLMAP pixels
    a, b = sorted(det)[:2]
    lay = turntable_layout()
    mm = float(np.linalg.norm(np.array(lay[a][:2]) - np.array(lay[b][:2]))) * 1000
    (s / "session.json").write_text(json.dumps({"name": "ciotola", "useMasks": False}))
    (s / "scale.json").write_text(json.dumps({"method": "tap", "photo": "VASO_0000", "lengthMM": mm,
                                              "a": {"x": det[a][0] / w, "y": det[a][1] / h},
                                              "b": {"x": det[b][0] / w, "y": det[b][1] / h}}))
    out = io.StringIO()
    res = run_session(s, "medium", out=out)
    log = out.getvalue()
    assert "PROGRESS alignCameras 100.0" in log and log.strip().splitlines()[-1].startswith("RESULT ")
    for key in ("model", "pointCloud", "dem", "orthophoto"):
        assert Path(res["outputs"][key]).exists(), key
    assert json.loads((s / "metashape" / "result.json").read_text())["stats"]["aligned"] >= 28
    ch = Document.open(res["outputs"]["project"]).chunk
    ch.detect_markers("aruco_4x4_50")
    ch.triangulate_markers()
    pos = {int(re.findall(r"\d+", m.label)[-1]): ch.to_world(m.position[None])[0] for m in ch.markers
           if m.position is not None}
    err = [abs(np.linalg.norm(pos[i] - pos[j]) - np.linalg.norm(np.array(lay[i]) - np.array(lay[j])))
           for i, j in itertools.combinations(sorted(pos), 2)]
    assert np.median(err) < 0.0015, np.median(err)  # 1.5 mm on distances of 50-180 mm


# ---------------------------------------------------------------- WebODM
def test_webodm_all_zip(processed, tmp_path):
    import laspy
    import trimesh

    from pyarchinit_photogrammetry.integrations.webodm import import_task

    _doc, src, _ = processed
    t = tmp_path / "task"
    for d in ("odm_orthophoto", "odm_dem", "odm_georeferencing", "odm_texturing"):
        (t / d).mkdir(parents=True)
    shutil.copy(src.orthomosaic_path, t / "odm_orthophoto" / "odm_orthophoto.tif")
    shutil.copy(src.dem_path, t / "odm_dem" / "dsm.tif")
    off = np.array([2480000.0, 4500000.0, 0.0])
    (t / "odm_georeferencing" / "coords.txt").write_text("WGS84 UTM 32N\n2480000 4500000\n")
    w = src.to_world(src.point_cloud.xyz)
    hdr = laspy.LasHeader(point_format=3, version="1.2")
    hdr.offsets, hdr.scales = w.min(0), [0.001] * 3
    las = laspy.LasData(hdr)
    las.x, las.y, las.z = w[:, 0], w[:, 1], w[:, 2]
    las.write(str(t / "odm_georeferencing" / "odm_georeferenced_model.laz"))
    mesh = trimesh.load(str(src.product_path("model")), process=False)
    mesh.vertices = src.to_world(np.asarray(mesh.vertices)) - off
    mesh.export(str(t / "odm_texturing" / "odm_textured_model_geo.obj"))
    with zipfile.ZipFile(tmp_path / "all.zip", "w") as z:
        for p in t.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(t))
    ch = import_task(tmp_path / "all.zip", tmp_path / "odm" / "odm.ppg").chunk
    assert ch.crs == "EPSG:3004"  # the CRS of the rasters
    assert {"dem", "orthomosaic", "model", "point_cloud"} <= set(ch.products)
    assert np.abs(ch.to_world(ch.point_cloud.xyz[:500]) - w[:500]).max() < 0.002


# ---------------------------------------------------------------- Metashape
def test_metashape_calibration_mapping():
    from pyarchinit_photogrammetry.integrations.metashape import MsSensor, sensor_params

    s = MsSensor(0, "cam", 4000, 3000, {"f": 3000.0, "cx": -5.0, "cy": 7.0, "b1": 2.0, "k1": 0.1, "k2": -0.2,
                                         "k3": 0.05, "p1": 0.001, "p2": -0.002})
    model, p, warn = sensor_params(s)
    assert model == "FULL_OPENCV" and not warn
    assert p[:4] == [3002.0, 3000.0, 1995.0, 1507.0]
    assert p[4:9] == [0.1, -0.2, -0.002, 0.001, 0.05]  # Metashape p1/p2 are OpenCV's p2/p1


def test_metashape_ply_reader():
    from pyarchinit_photogrammetry.integrations.metashape import _ply_vertices

    pts = np.array([(1.0, 2.0, 3.0, 7), (4.0, 5.0, 6.0, 8)], dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("id", "<u4")])
    head = (b"ply\nformat binary_little_endian 1.0\nelement vertex 2\nproperty float x\nproperty float y\n"
            b"property float z\nproperty uint id\nend_header\n")
    v = _ply_vertices(head + pts.tobytes())
    assert v["x"].tolist() == [1.0, 4.0] and v["id"].tolist() == [7, 8]


@pytest.mark.skipif(not PSX.exists(), reason="real Metashape project not available")
def test_metashape_real_project(tmp_path):
    import rasterio

    from pyarchinit_photogrammetry.integrations.metashape import import_psx

    ch = import_psx(PSX, tmp_path / "p.ppg").chunk
    assert len(ch.aligned_cameras) > 100 and ch.crs.startswith("EPSG:326")
    tp = ch.to_world(ch.tie_points.xyz[::500])
    with rasterio.open(ch.dem_path) as src:
        z = np.array([v[0] for v in src.sample(tp[:, :2])])
    ok = z > -1000
    assert abs(np.median(tp[ok, 2] - z[ok])) < 0.05


# ---------------------------------------------------------------- QGIS
def test_qgis_export_layers(tmp_path):
    from pyarchinit_photogrammetry.io.qgis_link import LIVE_FILES, export_layers

    layers = {"Unita' stratigrafiche": {"kind": "polygon", "features": [
                  {"etichetta": "US 1", "us": "1", "_unit": 0, "_geom": ("Polygon", [[0, 0], [1, 0], [1, 1]])}]},
              "Curve di livello (0.1 m)": {"kind": "line", "features": [{"quota": 99.5, "_geom": ("LineString", [[0, 0], [2, 2]])}]},
              "Punti": {"kind": "point", "features": [{"z": 1.0, "_geom": ("Point", [0.5, 0.5])}]}}
    files = export_layers(tmp_path, layers, "EPSG:3004")
    assert set(files) == set(LIVE_FILES)
    unita = json.loads(files["unita"].read_text())
    f = unita["features"][0]
    assert f["geometry"]["coordinates"][0][0] == f["geometry"]["coordinates"][0][-1]  # ring closed
    assert f["properties"] == {"layer": "Unita' stratigrafiche", "etichetta": "US 1", "us": "1"}  # no hidden keys
    assert unita["crs"]["properties"]["name"].endswith("EPSG::3004")
    assert json.loads(files["linee"].read_text())["features"] == []
    assert json.loads(files["curve"].read_text())["features"][0]["properties"]["quota"] == 99.5


def test_qgis_project(processed, tmp_path):
    from pyarchinit_photogrammetry.io.qgis_link import build_project, export_layers, find_qgis

    q = find_qgis()
    if q is None or not q.python or not Path(q.python).exists():
        pytest.skip("QGIS not installed")
    _doc, ch, _ = processed
    files = export_layers(tmp_path, {}, ch.crs)
    rep = build_project(tmp_path / "p.qgs", ch.crs, files, [{"name": "Ortofoto", "path": str(ch.orthomosaic_path)}], qgis=q)
    assert rep["written"] and all(v["valid"] for v in rep["vectors"] + rep["rasters"])
    text = (tmp_path / "p.qgs").read_text(encoding="utf-8")
    assert text.count('autoRefreshMode="ReloadData"') == len(files)


# ---------------------------------------------------------------- SAM worker (protocol, fake model)
def test_sam_worker_protocol(monkeypatch):
    from pyarchinit_photogrammetry.ai.sam_env import ExternalSam2Segmenter

    monkeypatch.setenv("PPG_SAM_FAKE", "1")
    s = ExternalSam2Segmenter(python=sys.executable)
    try:
        img = np.zeros((120, 160, 3), np.uint8)
        assert s.device == "fake"
        m = s.segment_points(img, [[80, 60]], [1])
        assert m.shape == (120, 160) and m[60, 80] and not m[0, 0]
        assert s.segment_box(img, [10, 10, 30, 20]).sum() == 200
        auto = s.segment_auto(img)
        assert len(auto) == 2 and auto[0].dtype == bool
    finally:
        s.close()


def test_sam_env_paths(monkeypatch, tmp_path):
    from pyarchinit_photogrammetry.ai import sam_env

    monkeypatch.setenv("PPG_SAM_ENV", str(tmp_path / "env"))
    assert sam_env.env_dir() == tmp_path / "env" and not sam_env.is_installed()
    target, ext = sam_env._uv_target()
    assert target and ext in ("zip", "tar.gz")


# ---------------------------------------------------------------- IFC
def test_ifc_reader_round_trip(processed, tmp_path):
    pytest.importorskip("ifcopenshell")
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.bim.ifc import export_chunk_ifc, read_ifc_elements

    _doc, ch, _ = processed
    steps = []
    u = UnitPolygon(box(2480001.1, 4500001.0, 2480002.9, 4500002.0), us="7", area="1")
    out = export_chunk_ifc(ch, tmp_path / "r.ifc", units=[u], us_records={"7": {"d_stratigrafica": "strato"}},
                           site="Saggio", progress=lambda f, m: steps.append(f))
    els = read_ifc_elements(out)
    assert [e["name"] for e in els] == [f"Rilievo fotogrammetrico - {ch.label}", "US 7"]
    assert els[1]["psets"]["Pset_PyArchInit_US"]["d_stratigrafica"] == "strato"
    v = els[1]["vertices"]
    assert 2480001.0 < v[:, 0].min() and v[:, 0].max() < 2480003.0  # metres, in the CRS
    assert steps and max(steps) > 0.5
