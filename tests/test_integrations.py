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
def _gpkg_rows(path, table):
    import sqlite3

    import shapely

    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(f'SELECT * FROM "{table}" ORDER BY fid')]
    for r in rows:
        blob = r.pop("geom")
        assert blob[:2] == b"GP" and blob[3] == 0b011  # little endian, xy envelope
        r["geom"] = shapely.from_wkb(blob[8 + 32:])
    con.close()
    return rows


def test_qgis_live_geopackage(tmp_path):
    """The drawings for QGIS: one GeoPackage, a table per kind, rewritten in place and only
    where something changed."""
    import sqlite3

    from pyarchinit_photogrammetry.io.qgis_link import LIVE_DB, LIVE_TABLES, LiveLink

    unit = {"etichetta": "US 1", "us": "1", "_unit": 0, "_geom": ("Polygon", [[0, 0], [1, 0], [1, 1]])}
    layers = {"Unita' stratigrafiche": {"kind": "polygon", "features": [unit]},
              "Curve di livello (0.1 m)": {"kind": "line", "features": [
                  {"quota": 99.5, "direttrice": True, "_geom": ("LineString", [[0, 0], [2, 2]])}]},
              "Punti": {"kind": "point", "features": [{"z": 1.0, "_geom": ("Point", [0.5, 0.5])}]}}
    link = LiveLink(tmp_path, "EPSG:3004")
    files = link.write(layers)
    assert set(files) == set(LIVE_TABLES) and files["unita"] == tmp_path / LIVE_DB
    con = sqlite3.connect(tmp_path / LIVE_DB)
    assert con.execute("PRAGMA application_id").fetchone()[0] == 0x47504B47  # 'GPKG'
    assert con.execute("SELECT srs_id FROM gpkg_contents WHERE table_name='unita'").fetchone()[0] == 3004
    assert con.execute("SELECT COUNT(*) FROM gpkg_spatial_ref_sys WHERE srs_id=3004").fetchone()[0] == 1
    con.close()
    (u,) = _gpkg_rows(tmp_path / LIVE_DB, "unita")
    assert u["layer"] == "Unita' stratigrafiche" and u["etichetta"] == "US 1" and u["us"] == "1"
    assert "_unit" not in u and u["geom"].geom_type == "Polygon" and u["geom"].area == 0.5  # ring closed
    (c,) = _gpkg_rows(tmp_path / LIVE_DB, "curve")
    assert c["quota"] == 99.5 and c["direttrice"] == 1
    assert link.counts() == {"unita": 1, "sezioni": 0, "curve": 1, "linee": 0, "punti": 1, "quote": 0}

    def changes():
        con = sqlite3.connect(tmp_path / LIVE_DB)
        n = con.total_changes, {t: con.execute("SELECT last_change FROM gpkg_contents WHERE table_name=?", (t,)).fetchone()[0]
                                for t in LIVE_TABLES}
        con.close()
        return n[1]

    before = changes()
    link.write(layers)  # nothing changed: nothing written
    assert changes() == before
    unit["us"] = "2"  # an attribute edited in place
    layers["Sezioni"] = {"kind": "line", "features": [{"etichetta": "Sezione 1", "lunghezza_m": 2.0,
                                                       "_geom": ("LineString", [[0, 0], [0, 2]])}]}
    link.write(layers)
    after = changes()
    assert after["unita"] != before["unita"] and after["sezioni"] != before["sezioni"]
    assert after["curve"] == before["curve"] and after["punti"] == before["punti"]
    assert _gpkg_rows(tmp_path / LIVE_DB, "unita")[0]["us"] == "2"
    assert _gpkg_rows(tmp_path / LIVE_DB, "sezioni")[0]["lunghezza_m"] == 2.0


_QGIS_LIVE = r"""
import json, sys
from qgis.core import QgsApplication, QgsPointXY, QgsProject, QgsRectangle
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QEventLoop, QTimer
from qgis.PyQt.QtGui import QColor
app = QgsApplication([], True)
app.initQgis()
proj = QgsProject.instance()
proj.read(sys.argv[1])
layers = {l.name(): l for l in proj.mapLayers().values()}
x, y, x2, y2 = (float(v) for v in sys.argv[2:6])
canvas = QgsMapCanvas()
canvas.setCachingEnabled(True)  # as in QGIS desktop: unchanged layers are drawn from the cache
canvas.resize(300, 300)
canvas.show()
canvas.setCanvasColor(QColor("white"))
canvas.setDestinationCrs(proj.crs())
canvas.setLayers([l for n, l in layers.items() if n.endswith("(ppg)")])
canvas.setExtent(QgsRectangle(x - 1, y - 1, x2 + 1, y2 + 1))
def spin(sec):
    loop = QEventLoop()
    QTimer.singleShot(int(sec * 1000), loop.quit)
    loop.exec()
def colour(px, py):
    pt = canvas.getCoordinateTransform().transform(QgsPointXY(px, py))
    return QColor(canvas.grab().toImage().pixel(int(pt.x()), int(pt.y()))).name()
def state():
    return json.dumps({"counts": {n: sum(1 for _ in l.getFeatures()) for n, l in layers.items() if n.endswith("(ppg)")},
                       "first": colour(x + 0.25, y + 0.25), "second": colour(x2 - 0.25, y2 - 0.25)})
spin(1.0)
print("STATE " + state(), flush=True)
sys.stdin.readline()
spin(3.0)
print("STATE " + state(), flush=True)
app.exitQgis()
"""


def test_qgis_project_shows_new_drawings_by_itself(processed, tmp_path):
    """The project made with QGIS's Python: valid layers, and QGIS sees what is drawn after
    it opened the project, without any reload (0.4.1 needed QGIS's Refresh button)."""
    import subprocess

    from pyarchinit_photogrammetry.io.qgis_link import LiveLink, build_project, find_qgis

    q = find_qgis()
    if q is None or not q.python or not Path(q.python).exists():
        pytest.skip("QGIS not installed")
    _doc, ch, _ = processed
    x0, y0, x1, y1 = 2480001.0, 4500001.0, 2480003.0, 4500003.0

    def square(x, y):
        return ("Polygon", [[x, y], [x + 0.5, y], [x + 0.5, y + 0.5], [x, y + 0.5]])

    layers = {"Unita' stratigrafiche": {"kind": "polygon", "features": [{"etichetta": "", "_geom": square(x0, y0)}]}}
    link = LiveLink(tmp_path, ch.crs)
    files = link.write(layers)
    rep = build_project(tmp_path / "p.qgs", ch.crs, files, [{"name": "Ortofoto", "path": str(ch.orthomosaic_path)}], qgis=q)
    assert rep["written"] and all(v["valid"] for v in rep["vectors"] + rep["rasters"])
    text = (tmp_path / "p.qgs").read_text(encoding="utf-8")
    assert text.count('autoRefreshEnabled="1"') == len(files) and "layername=sezioni" in text
    assert 'autoRefreshMode="ReloadData"' not in text and 'autoRefreshMode="RedrawOnly"' not in text
    assert "<authid>EPSG:3004</authid>" in text  # the project has its CRS (QGIS's Python found its database)
    script = tmp_path / "live.py"
    script.write_text(_QGIS_LIVE, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PROJ_", "GDAL_", "QT_"))}
    env.update(q.env)
    proc = subprocess.Popen([q.python, str(script), str(tmp_path / "p.qgs"), str(x0), str(y0), str(x1 + 0.5),
                             str(y1 + 0.5)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, env=env, close_fds=False)
    try:
        first = json.loads(next(ln for ln in proc.stdout if ln.startswith("STATE ")).split(" ", 1)[1])
        assert first["counts"]["Unita' stratigrafiche (ppg)"] == 1
        assert first["first"] != "#ffffff" and first["second"] == "#ffffff"  # one unit drawn, the other not yet
        layers["Unita' stratigrafiche"]["features"].append({"etichetta": "", "_geom": square(x1, y1)})
        layers["Sezioni"] = {"kind": "line", "features": [{"etichetta": "Sezione 1",
                                                           "_geom": ("LineString", [[x0, y1], [x1, y0]])}]}
        link.write(layers)  # QGIS has the project open
        proc.stdin.write("next\n")
        proc.stdin.flush()
        second = json.loads(next(ln for ln in proc.stdout if ln.startswith("STATE ")).split(" ", 1)[1])
        assert second["counts"]["Unita' stratigrafiche (ppg)"] == 2 and second["counts"]["Sezioni (ppg)"] == 1
        assert second["second"] == first["first"]  # the new unit is on the map, nobody pressed Refresh
    finally:
        proc.kill()
        proc.wait(timeout=30)


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
    assert sam_env.installed_device() is None
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "ppg-sam.json").write_text('{"torch": "2.9", "device": "mps"}')
    assert sam_env.installed_device() == "mps"


def test_sam_in_process_needs_huggingface_hub(monkeypatch):
    """sam2 does not require huggingface_hub, which downloads the weights: without it SAM runs
    in its own environment (or is offered for installation) instead of failing at first use."""
    import sys

    from pyarchinit_photogrammetry.ai import sam_env

    monkeypatch.setitem(sys.modules, "huggingface_hub", None)  # the import fails
    assert not sam_env.in_process_available()


def test_sam_env_torch_backend(tmp_path):
    """uv picks the PyTorch build for the graphics card when it knows --torch-backend."""
    import sys

    from pyarchinit_photogrammetry.ai import sam_env

    fake = tmp_path / "uv.py"
    fake.write_text("import sys; print('--torch-backend <TORCH_BACKEND>' if 'new' in sys.argv[0] else '--python')")
    new = tmp_path / "new_uv.py"
    new.write_text(fake.read_text())
    if sys.platform == "win32":
        pytest.skip("scripts are not executable on Windows")
    for f in (fake, new):
        f.write_text("#!" + sys.executable + "\n" + f.read_text())
        f.chmod(0o755)
    assert sam_env.torch_backend_args(new) == ["--torch-backend", "auto"]
    assert sam_env.torch_backend_args(fake) == []
    assert sam_env.torch_backend_args(tmp_path / "missing") == []


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


# ---------------------------------------------------------------- Metashape: synthetic project
def _ply_bytes(fields, records, faces=None):
    head = ["ply", "format binary_little_endian 1.0", f"element vertex {len(records)}"]
    names = {"<f4": "float", "u1": "uchar", "<u4": "uint"}
    head += [f"property {names[t]} {n}" for n, t in fields]
    body = records.tobytes()
    if faces is not None:
        head += [f"element face {len(faces)}", "property list uchar int vertex_indices",
                 "property list uchar float texcoord"]
        body += faces.tobytes()
    return ("\n".join(head + ["end_header"]) + "\n").encode() + body


def _fl(values) -> str:
    return " ".join(repr(float(v)) for v in np.ravel(values))


def _zip(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)


def make_psx(root: Path, n_grid: int = 41, texture: bool = False):
    """A Metashape 2.x-like project near Tell Tuqan: chunk transform internal -> geocentric
    (ENU axes, 0.5 m per internal unit), 4 nadir cameras inside a camera group, tie points of
    the live component 0 plus stale points of a deleted component 1, and a model with
    texture coordinates. Returns (psx path, site geocentric origin, east/north/up axes)."""
    from pyproj import CRS, Transformer

    lon, lat, h = 36.8, 35.8, 300.0
    origin = np.array(Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True).transform(lon, lat, h))
    lo, la = np.radians(lon), np.radians(lat)
    east = np.array([-np.sin(lo), np.cos(lo), 0.0])
    north = np.array([-np.sin(la) * np.cos(lo), -np.sin(la) * np.sin(lo), np.cos(la)])
    up = np.array([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])
    R = np.column_stack([east, north, up])
    scale = 0.5
    tr_xml = (f"<transform><rotation>{_fl(R)}</rotation>"
              f"<translation>{_fl(origin)}</translation><scale>{scale}</scale></transform>")
    files = root / "scavo.files"
    cams = ""
    frame_cams = ""
    for i, (x, y) in enumerate([(-20, -20), (20, -20), (-20, 20), (20, 20)]):
        T = np.eye(4)
        T[:3, :3] = np.diag([1.0, -1.0, -1.0])  # looking down: camera z = -up
        T[:3, 3] = [x, y, 100.0]  # 50 m above the ground (internal units of 0.5 m)
        cams += (f'<camera id="{i}" sensor_id="0" component_id="0" label="IMG_{i}">'
                 f'<transform>{_fl(T)}</transform></camera>')
        frame_cams += f'<camera camera_id="{i}"><photo path="../../../photos/IMG_{i}.jpg"/></camera>'
    wkt = CRS.from_epsg(4326).to_wkt("WKT1_GDAL")
    chunk = (f'<chunk version="1.2.0" label="Chunk 1"><sensors><sensor id="0" label="cam" type="frame">'
             f'<resolution width="4000" height="3000"/><calibration type="frame" class="adjusted">'
             f'<resolution width="4000" height="3000"/><f>3000</f><cx>0</cx><cy>0</cy></calibration></sensor></sensors>'
             f'<components next_id="2" active_id="0"><component id="0" label="Component 1">{tr_xml}</component></components>'
             f'<cameras><group label="DJI_001">{cams}</group></cameras>'
             f'<frames><frame id="0" path="0/frame.zip"/></frames>{tr_xml}<reference>{wkt}</reference></chunk>')
    _zip(files / "project.zip", {"doc.xml": '<document version="1.2.0"><chunks><chunk id="0" path="0/chunk.zip"/></chunks></document>'})
    _zip(files / "0" / "chunk.zip", {"doc.xml": chunk})
    _zip(files / "0" / "0" / "frame.zip", {"doc.xml": f'<frame version="1.2.0"><cameras>{frame_cams}</cameras>'
                                                        '<point_cloud path="point_cloud/point_cloud.zip"/>'
                                                        '<model id="0" path="model/model.zip"/></frame>'})
    rng = np.random.default_rng(0)
    good = np.zeros(1000, dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("id", "<u4")])
    good["x"], good["y"] = rng.uniform(-50, 50, 1000), rng.uniform(-50, 50, 1000)
    good["id"] = np.arange(1000)
    stale = np.zeros(500, dtype=good.dtype)
    stale["x"], stale["y"], stale["z"] = rng.uniform(-5000, 5000, (3, 500))
    tracks = np.zeros(1500, dtype=[("red", "u1"), ("green", "u1"), ("blue", "u1")])
    tracks["red"] = 200
    fields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("id", "<u4")]
    _zip(files / "0" / "0" / "point_cloud" / "point_cloud.zip", {
        "doc.xml": '<point_cloud version="1.7.0"><tracks path="tracks.ply" count="1500"/>'
                   '<points component_id="0" path="points0.ply" count="1000"/>'
                   '<points component_id="1" path="points1.ply" count="500"/></point_cloud>',
        "tracks.ply": _ply_bytes([("red", "u1"), ("green", "u1"), ("blue", "u1")], tracks),
        "points0.ply": _ply_bytes(fields, good), "points1.ply": _ply_bytes(fields, stale)})
    g = np.linspace(-50, 50, n_grid)
    gx, gy = np.meshgrid(g, g)
    vfields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1"),
               ("confidence", "<f4")]
    verts = np.zeros(gx.size, dtype=vfields)
    verts["x"], verts["y"], verts["z"] = gx.ravel(), gy.ravel(), 0.02 * gx.ravel()
    verts["green"] = 150
    idx = np.arange(gx.size).reshape(gx.shape)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()
    tri = np.vstack([np.column_stack([a, b, c]), np.column_stack([a, c, d])])
    faces = np.zeros(len(tri), dtype=[("n", "u1"), ("v", "<i4", (3,)), ("nt", "u1"), ("t", "<f4", (6,))])
    faces["n"], faces["v"], faces["nt"] = 3, tri, 6
    tif = b"II*\x00"  # not an image: the import warns and goes on
    if texture:  # planar texture coordinates and a real image, red on the left, blue on the right
        import cv2

        uv = (np.column_stack([verts["x"], verts["y"]]) + 50.0) / 100.0
        faces["t"] = uv[tri].reshape(-1, 6)
        img = np.zeros((64, 64, 3), np.uint8)
        img[:, :32] = (0, 0, 255)
        img[:, 32:] = (255, 0, 0)
        tif = cv2.imencode(".tif", img)[1].tobytes()
    _zip(files / "0" / "0" / "model" / "model.zip", {
        "doc.xml": f'<model version="1.2.0"><mesh path="mesh.ply"><faceCount>{len(tri)}</faceCount></mesh>'
                   '<texture path="texture.tif"/></model>',
        "mesh.ply": _ply_bytes(vfields, verts, faces), "texture.tif": tif})
    psx = root / "scavo.psx"
    psx.write_text('<document version="1.2.0" path="{projectname}.files/project.zip"/>')
    return psx, origin, (east, north, up)


def test_metashape_groups_components_mesh_and_exact_frame(tmp_path, monkeypatch):
    from pyproj import Transformer

    from pyarchinit_photogrammetry.integrations import metashape as ms
    from pyarchinit_photogrammetry.io.mesh_ply import PlyMesh, face_count

    psx, origin, _ = make_psx(tmp_path / "src")
    monkeypatch.setattr(ms, "PREVIEW_FACES", 1000)  # the 3200-face model gets its display copy
    doc = ms.import_psx(psx, tmp_path / "out" / "p.ppg")
    ch = doc.chunk
    warnings = " | ".join(ch.meta["import"]["warnings"])
    assert len(ch.aligned_cameras) == 4  # inside a camera group
    assert len(ch.tie_points) == 1000 and "500 punti di legame" in warnings  # stale component left out
    assert ch.crs == "EPSG:32637" and ch.meta["import"]["frame"] == "exact"
    # the ground plane of the internal frame lies at 300 m (ellipsoidal) in UTM
    e0, n0 = Transformer.from_crs("EPSG:4326", "EPSG:32637", always_xy=True).transform(36.8, 35.8)
    tw = ch.to_world(ch.tie_points.xyz)
    assert np.abs(tw[:, 2] - 300.0).max() < 0.005
    assert abs(np.median(tw[:, 0]) - e0) < 2.0 and abs(np.median(tw[:, 1]) - n0) < 2.0
    centres = ch.to_world(np.array([c.center for c in ch.aligned_cameras]))
    assert np.allclose(centres[:, 2], 350.0, atol=0.005)  # 100 internal units of 0.5 m above
    # the converted poses see the converted points where the original poses saw the originals
    mc = ms.read_psx(psx)[0]
    parts, _, _ = ms._read_clouds(mc)
    P_int = parts[0][1][:200]
    P_new = ms.crs_mapper(mc, ch.crs)(P_int) - ch.transform[:3, 3]
    for cam, orig in zip(ch.aligned_cameras, mc.cameras):
        s = ch.sensor(cam)
        old = type(cam)(key=cam.key, label=cam.label, path=cam.path, sensor_key=cam.sensor_key)
        old.transform = orig.transform
        a, da = old.project(s, P_int)
        b, _ = cam.project(s, P_new)
        assert np.abs(a - b)[da > 0].max() < 0.15  # a wrong rotation would give hundreds of pixels
    # model: extracted, converted in place, display copy, coloured vertices as dense cloud
    model = ch.product_path("model")
    mesh = PlyMesh(model)
    assert mesh.n_faces == 3200 and abs(ch.to_world(mesh.xyz()[:1])[0, 2] - (300.0 - 0.5)) < 0.01
    assert face_count(ch.data_dir / "model_preview.ply") < 3200
    assert len(ch.point_cloud) == mesh.n_vertices and ch.meta["point_cloud_source"].startswith("vertici")
    assert "texture" in warnings


def test_metashape_model_keeps_its_texture_and_colours(tmp_path):
    """The model of Metashape comes with its colours, its confidence and its texture; computing
    the confidence, cleaning and cropping keep them (trimesh used to drop the colours of a mesh
    with texture coordinates and to split its vertices along the seams)."""
    from pyarchinit_photogrammetry.gui.scene import available_views, build_scene
    from pyarchinit_photogrammetry.integrations import metashape as ms
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    psx, _origin, _ = make_psx(tmp_path / "src", texture=True)
    doc = ms.import_psx(psx, tmp_path / "out" / "p.ppg")
    ch = doc.chunk
    tex = ch.product_path("model_textured")
    assert tex is not None and tex.suffix == ".jpg" and "model_textured" in available_views(ch)
    mesh = read_mesh(ch.product_path("model"))
    assert mesh.texcoords is not None and mesh.colors is not None and (mesh.colors[:, 1] == 150).all()
    assert ch.model.visual.kind == "vertex"  # chunk.model keeps the colours too
    sd = build_scene(ch, "model_textured")
    assert sd.mesh_uv is not None and sd.texture == tex and len(sd.mesh_faces) == len(mesh.faces)
    # cleaning and cropping keep colours, texture coordinates and the texture
    ch.clean_model(min_component=0.01)
    after = read_mesh(ch.product_path("model"))
    assert after.texcoords is not None and after.colors is not None and ch.product_path("model_textured") == tex
    w = ch.to_world(after.xyz)
    lo, hi = w.min(axis=0), w.max(axis=0)
    ch.crop_model((lo[0], lo[1], (lo[0] + hi[0]) / 2, hi[1]))  # the left half: red in the texture
    half = read_mesh(ch.product_path("model"))
    assert len(half.faces) < len(after.faces) and half.texcoords.reshape(-1, 2)[:, 0].max() <= 0.5 + 0.026  # one cell
    out = ch.export_model(tmp_path / "metà.obj")
    assert (tmp_path / "metà_texture.jpg").exists() and "vt " in out.read_text()[:10_000_000]
    ply = ch.export_model(tmp_path / "metà.ply")
    assert read_mesh(ply).texture_file == "metà_texture.jpg" and (tmp_path / "metà_texture.jpg").exists()
    # smoothing keeps the texture, closing holes or decimating leave it out
    ch.clean_model(target_faces=len(half.faces) // 2)
    assert ch.product_path("model_textured") is None and read_mesh(ch.product_path("model")).colors is not None


def test_metashape_local_frame_with_components(tmp_path):
    """No reference system: the chunk keeps Metashape's frame; a second live component with
    its own transform is brought into it."""
    from pyarchinit_photogrammetry.integrations import metashape as ms

    psx, _, _ = make_psx(tmp_path / "src", n_grid=5)
    chunk_zip = psx.with_suffix(".files") / "0" / "chunk.zip"
    with zipfile.ZipFile(chunk_zip) as z:
        xml = z.read("doc.xml").decode()
    xml = xml.split("<reference>")[0] + "</chunk>"  # no CRS
    # component 1 is alive again, shifted by 10 internal units along x
    shifted = xml[xml.index("<transform>", xml.index("<component ")):xml.index("</transform>", xml.index("<component ")) + 12]
    m = ms._transform(__import__("xml.etree.ElementTree", fromlist=["fromstring"]).fromstring(shifted))
    m2 = m.copy()
    m2[:3, 3] = m[:3, 3] + m[:3, :3] @ np.array([10.0, 0.0, 0.0])
    t2 = (f"<transform><rotation>{_fl(m2[:3, :3] / 0.5)}</rotation><translation>"
          f"{_fl(m2[:3, 3])}</translation><scale>0.5</scale></transform>")
    xml = xml.replace("</component></components>", f'</component><component id="1">{t2}</component></components>')
    xml = xml.replace('component_id="0" label="IMG_3"', 'component_id="1" label="IMG_3"')
    _zip(chunk_zip, {"doc.xml": xml})
    ch = ms.import_psx(psx, tmp_path / "out" / "p.ppg", with_model=False).chunk
    assert ch.crs == "LOCAL" and len(ch.tie_points) == 1500  # both components are live now
    moved = next(c for c in ch.aligned_cameras if c.label == "IMG_3")
    assert np.allclose(moved.transform[:3, 3], [30.0, 20.0, 100.0], atol=1e-6)  # 20 + 10 along x
    assert np.allclose(moved.transform[:3, :3] @ moved.transform[:3, :3].T, np.eye(3), atol=1e-9)


def _add_internal_dem(psx: Path, lon: float = 36.8, lat: float = 35.8) -> None:
    """Metashape's own DEM: 2 x 2 GeoTIFF tiles in WGS 84 listed in ``elevation.zip`` (height
    300 m rising 1 m per 0.001 degrees eastwards), a coarser level that must be ignored, and
    an empty exported DEM next to the project (a failed export)."""
    import rasterio
    from rasterio.transform import from_origin

    frame_dir = psx.with_suffix(".files") / "0" / "0"
    elev = frame_dir / "elevation"
    elev.mkdir(parents=True, exist_ok=True)
    n, step = 64, 0.0005 / 64  # 2 x 2 tiles of 64 px over 0.001 x 0.001 degrees
    left, top = lon - 0.0005, lat + 0.0005
    tiles = ""
    for ty in range(2):
        for tx in range(2):
            x0, y0 = left + tx * n * step, top - ty * n * step
            cols = np.arange(n)[None, :] + 0.5
            z = (300.0 + (x0 + cols * step - lon) * 1000.0).repeat(n, axis=0).astype("float32")
            if (tx, ty) == (0, 0):
                z[:8, :8] = -32767  # no data
            name = f"tile-{ty}-{tx}.tif"
            with rasterio.open(elev / name, "w", driver="GTiff", width=n, height=n, count=1, dtype="float32",
                               crs="EPSG:4326", transform=from_origin(x0, y0, step, step), nodata=-32767) as dst:
                dst.write(z, 1)
            tiles += f'<tile x="{tx}" y="{ty}"><path>{name}</path></tile>'
    with rasterio.open(elev / "tile-l1-0-0.tif", "w", driver="GTiff", width=n, height=n, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(left, top, 2 * step, 2 * step)) as dst:
        dst.write(np.full((1, n, n), 999.0, "float32"))
    tiles += '<tile x="0" y="0" level="1"><path>tile-l1-0-0.tif</path></tile>'
    _zip(elev / "elevation.zip", {"doc.xml": (
        '<elevation version="1.2.0"><params><crs>GEOGCS["WGS 84"]</crs><nodataValue>-32767</nodataValue></params>'
        f'<tiles>{tiles}</tiles><meta><property name="BuildDem/resolution" value="0.5"/></meta></elevation>')})
    with zipfile.ZipFile(frame_dir / "frame.zip") as z:
        xml = z.read("doc.xml").decode()
    _zip(frame_dir / "frame.zip", {"doc.xml": xml.replace("</frame>", '<elevation id="0" path="elevation/elevation.zip"/></frame>')})
    (psx.parent / "dem").mkdir(exist_ok=True)
    (psx.parent / "dem" / "scavo_dem.tif").write_bytes(b"")


def test_metashape_internal_dem(tmp_path):
    """No usable exported DEM: the tiles of Metashape's own DEM become the DEM of the chunk,
    in its CRS, without writing anything into the Metashape project."""
    import rasterio
    from pyproj import Transformer

    from pyarchinit_photogrammetry.integrations import metashape as ms

    psx, _, _ = make_psx(tmp_path / "src", n_grid=5)
    _add_internal_dem(psx)
    before = sorted(p.name for p in (psx.with_suffix(".files") / "0" / "0" / "elevation").iterdir())
    ch = ms.import_psx(psx, tmp_path / "out" / "p.ppg", with_model=False).chunk
    warnings = " | ".join(ch.meta["import"]["warnings"])
    assert "scavo_dem.tif e' vuoto" in warnings and "quote del DEM" not in warnings
    assert ch.meta["import"]["dem"] == "DEM interno del progetto Metashape"
    assert ch.dem_path is not None and ch.dem_path.parent == ch.data_dir
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32637", always_xy=True)
    with rasterio.open(ch.dem_path) as src:
        assert src.crs.to_epsg() == 32637 and abs(src.res[0] - 0.5) < 1e-9  # Metashape's resolution kept
        pts = [to_utm.transform(36.8 + dx, 35.8 + dy) for dx, dy in ((0.0, 0.0), (0.0003, 0.0002), (-0.0002, -0.0003))]
        vals = [v[0] for v in src.sample(pts)]
        nw = next(src.sample([to_utm.transform(36.8 - 0.00049, 35.8 + 0.00049)]))[0]
    assert np.allclose(vals, [300.0, 300.3, 299.8], atol=0.02)  # not the 999 of the coarse level
    assert nw == src.nodata  # the no-data corner stays empty
    after = sorted(p.name for p in (psx.with_suffix(".files") / "0" / "0" / "elevation").iterdir())
    assert after == before  # no .aux.xml or other file left in the Metashape project
