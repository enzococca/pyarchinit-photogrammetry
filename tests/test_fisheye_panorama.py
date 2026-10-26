"""Fisheye lenses and 360-degree photos (equirectangular, split into the faces of a cube and
aligned as a COLMAP rig), on a synthetic chamber (a tomb 4 x 3 x 2.4 m with a block inside)."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from pyarchinit_photogrammetry.core.camera import Sensor
from pyarchinit_photogrammetry.core.geometry import look_at, umeyama
from pyarchinit_photogrammetry.engine import panorama
from pyarchinit_photogrammetry.testing import synthetic

STATIONS = np.array([(1.0, 0.9, 1.2), (1.6, 0.8, 1.3), (2.2, 0.9, 1.2), (2.8, 0.85, 1.35), (3.2, 1.4, 1.25),
                     (3.1, 2.1, 1.3), (2.5, 2.3, 1.2), (1.9, 2.3, 1.3), (1.3, 2.2, 1.25), (0.8, 1.6, 1.3)])


def _wall_distance(P: np.ndarray) -> np.ndarray:
    """Distance of points (chamber frame) from the nearest wall, floor, vault or block face."""
    d = np.min(np.abs(np.stack([P[:, 0], 4 - P[:, 0], P[:, 1], 3 - P[:, 1], P[:, 2], 2.4 - P[:, 2]])), axis=0)
    block = (P[:, 0] > 1.55) & (P[:, 0] < 2.45) & (P[:, 1] > 1.15) & (P[:, 1] < 1.85) & (P[:, 2] < 0.75)
    db = np.min(np.abs(np.stack([P[:, 0] - 1.6, P[:, 0] - 2.4, P[:, 1] - 1.2, P[:, 1] - 1.8, P[:, 2] - 0.7])), axis=0)
    return np.where(block, np.minimum(d, db), d)


def _world_from_pano(yaw: float) -> np.ndarray:
    """Rotation panorama frame (x right, y down, z forward) -> chamber frame for a level
    panorama looking ``yaw`` degrees east of north."""
    a = np.radians(yaw)
    return np.column_stack([(np.cos(a), -np.sin(a), 0.0), (0.0, 0.0, -1.0), (np.sin(a), np.cos(a), 0.0)])


def test_cube_faces_are_the_pinhole_photos_of_the_same_place(tmp_path):
    scene = synthetic.make_chamber_scene()
    centre, yaw = np.array([1.5, 1.0, 1.3]), 25.0
    pano = synthetic.render_equirectangular(scene, centre, 2400, yaw=yaw)
    path = tmp_path / "P.png"
    cv2.imwrite(str(path), pano)
    faces, f, side = panorama.split_panorama(path, fov=100.0)
    assert list(faces) == list(panorama.FACES) and side == panorama.face_size(2400, 100.0)
    assert all(p == tmp_path / "facce_360" / name / "P.jpg" for name, p in faces.items())
    K = np.array([[f, 0, side / 2], [0, f, side / 2], [0, 0, 1]])
    errors = {}
    for name in ("front", "right", "up", "down"):
        pose = np.eye(4)
        pose[:3, :3] = _world_from_pano(yaw) @ panorama.face_rotation(*panorama.FACES[name])
        pose[:3, 3] = centre
        truth = synthetic.render_mesh(scene, pose, K, (side, side), k1=0.0).astype(float)
        face = cv2.imread(str(faces[name])).astype(float)
        errors[name] = np.abs(cv2.GaussianBlur(face, (0, 0), 2) - cv2.GaussianBlur(truth, (0, 0), 2)).mean()
        other = cv2.imread(str(faces["back" if name == "front" else "front"])).astype(float)
        assert np.abs(cv2.GaussianBlur(other, (0, 0), 2) - cv2.GaussianBlur(truth, (0, 0), 2)).mean() > 4 * errors[name]
    assert max(errors.values()) < 6, errors


def test_rig_of_the_faces():
    import pycolmap

    class Cam:
        def __init__(self, key, face, pano="P.jpg"):
            self.key, self.meta, self.enabled = key, {"face": face, "panorama": pano}, True

    class Chunk:
        cameras = [Cam(0, "front"), Cam(1, "right"), Cam(2, "up"), Cam(3, "front", pano="Q.jpg"), Cam(4, "left")]

    names = {0: "a/facce_360/front/P.jpg", 1: "a/facce_360/right/P.jpg", 2: "a/facce_360/up/P.jpg",
             3: "a/facce_360/front/Q.jpg", 4: "b/sinistra/P.jpg"}  # 4: not in the layout of split_panorama
    (config,) = panorama.rig_configs(Chunk(), names)
    cams = config.cameras
    assert [c.image_prefix for c in cams] == ["a/facce_360/front/", "a/facce_360/right/", "a/facce_360/up/"]
    assert cams[0].ref_sensor and not cams[1].ref_sensor
    right = cams[1].cam_from_rig.rotation.matrix()
    # a ray ahead of the front face is on the left edge of the right face (x = -1 in its frame)
    assert np.allclose(right @ [0, 0, 1], [-1, 0, 0], atol=1e-12)
    up = cams[2].cam_from_rig.rotation.matrix()
    assert np.allclose(up @ [0, -1, 0], [0, 0, 1], atol=1e-12)  # straight up is ahead of the up face
    assert isinstance(cams[1].cam_from_rig, pycolmap.Rigid3d) and np.allclose(cams[1].cam_from_rig.translation, 0)


def test_panoramas_align_as_rigs(tmp_path, monkeypatch):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.io import exif

    scene = synthetic.make_chamber_scene()
    folder = tmp_path / "pano"
    folder.mkdir()
    yaws = [0, 35, 80, 150, 200, 290, 10, 120, 240, 330]
    for i, (c, y) in enumerate(zip(STATIONS, yaws)):
        cv2.imwrite(str(folder / f"P{i:02d}.jpg"), synthetic.render_equirectangular(scene, c, 1600, yaw=y),
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
    cv2.imwrite(str(folder / "not_a_panorama.jpg"), np.zeros((300, 400, 3), np.uint8))
    read = exif.read_exif

    def with_gps(path):  # the 360-degree camera wrote the position of P00
        info = read(path)
        return {**info, "gps": (11.25, 43.77, 50.0)} if path.name == "P00.jpg" and path.parent == folder else info

    monkeypatch.setattr(exif, "read_exif", with_gps)
    doc = ppg.Document()
    ch = doc.add_chunk("tomba")
    doc.save(tmp_path / "t.ppg")
    cams = panorama.add_panoramas(ch, [folder])
    assert len(cams) == 60 and len(ch.cameras) == 60  # the photo that is not a panorama is left out
    (sensor,) = ch.sensors.values()
    assert sensor.model == "PINHOLE" and sensor.width == sensor.height == panorama.face_size(1600)
    assert {c.label for c in cams} >= {"P00_front", "P09_down"}
    p00 = [c for c in cams if c.meta["panorama"] == "P00.jpg"]
    assert all(c.reference.location == [11.25, 43.77, 50.0] for c in p00)
    assert all(c.meta["gnss"]["source"] == "panorama" and c.meta["gnss"]["height"] == "exif"
               and c.reference.accuracy == [5.0, 5.0, 10.0] and c.meta["reference_crs"] == "EPSG:4326" for c in p00)
    assert all(c.reference.location is None for c in cams if c.meta["panorama"] != "P00.jpg")
    monkeypatch.setattr(exif, "read_exif", read)
    for c in cams:  # keep the georeference out of this test: the scale comes from the chamber
        c.reference.location = None

    ch.match_photos(quality="highest")
    assert ch.meta["sfm"]["rigs"] == 1
    assert ch.align_cameras() == 60
    by_pano: dict[str, dict] = {}
    for c in ch.cameras:
        by_pano.setdefault(c.meta["panorama"], {})[c.meta["face"]] = c.transform
    for faces in by_pano.values():
        centres = np.array([t[:3, 3] for t in faces.values()])
        assert np.ptp(centres, axis=0).max() < 1e-9  # one centre per panorama
        front = faces["front"][:3, :3]
        for name, t in faces.items():
            assert np.allclose(front.T @ t[:3, :3], panorama.face_rotation(*panorama.FACES[name]), atol=1e-9)
        assert ch.sensors[sensor.key].params == pytest.approx(sensor.params)  # calibration not refined
    names = sorted(by_pano)
    E = np.array([next(iter(by_pano[n].values()))[:3, 3] for n in names])
    T = STATIONS[[int(n[1:3]) for n in names]]
    M = umeyama(E, T)
    res = (M[:3, :3] @ E.T).T + M[:3, 3] - T
    assert np.sqrt((res ** 2).sum(1).mean()) < 0.002  # metres, in a chamber of 4 m
    ch.build_depth_maps(quality="medium")
    ch.build_point_cloud()
    P = (M[:3, :3] @ np.asarray(ch.point_cloud.xyz, float).T).T + M[:3, 3]
    assert len(P) > 3000 and np.median(_wall_distance(P)) < 0.006


def test_fisheye_photos_are_recognised_and_aligned(tmp_path):
    """The fisheye model: read from the EXIF, refined by the alignment, projecting beyond 90
    degrees, depth maps. The 40 photos are taken from 10 stations, 4 directions with one centre
    each, and are aligned one by one (incremental mapper): the global one, the automatic choice
    from 40 photos, put a camera of a station 14-37 cm off in 3 runs of 200 (RMS 23-59 mm; once
    in the CI, 19 mm), the incremental one never (200 runs, RMS 1.4-1.6 mm). COLMAP's seeds do
    not make the runs repeatable: the photos get their database ids in the order the extraction
    threads finish, and the RANSAC generators are per thread."""
    import pyarchinit_photogrammetry as ppg
    from PIL import Image

    scene = synthetic.make_chamber_scene()
    focal, size, k1 = 300.0, (800, 600), 0.02  # 150 degrees across the width
    folder = tmp_path / "foto"
    folder.mkdir()
    truth = []
    for i, (x, y, z) in enumerate(STATIONS):
        for j, a in enumerate((0.0, 90.0, 180.0, 270.0)):
            a = np.radians(a + 90 + 8 * ((4 * i + j) % 3))
            eye = np.array([x, y, z])
            pose = look_at(eye, eye + np.array([np.sin(a), np.cos(a), -0.25]), up=(0, 0, 1))
            exif = Image.Exif()
            exif[0x010F], exif[0x0110] = "Prova", "FE-1"
            sub = exif.get_ifd(0x8769)
            sub[0xA405], sub[0xA434] = 17, "8mm F3.5 Fisheye"  # 35 mm equivalent, lens model
            img = synthetic.render_fisheye(scene, pose, focal, size, k1)
            Image.fromarray(img[..., ::-1]).save(folder / f"F{4 * i + j:02d}.jpg", exif=exif, quality=95)
            truth.append(eye)
    doc = ppg.Document()
    ch = doc.add_chunk("fisheye")
    doc.save(tmp_path / "f.ppg")
    ch.add_photos([folder])
    (sensor,) = ch.sensors.values()
    assert sensor.model == "OPENCV_FISHEYE"  # from the lens model in the EXIF
    ch.match_photos(quality="high")
    assert ch.align_cameras(mapper="incremental") == 40
    sensor = ch.sensors[sensor.key]
    assert sensor.model == "OPENCV_FISHEYE" and abs(sensor.params[0] - focal) < 3 and abs(sensor.params[4] - k1) < 0.005
    E = np.array([c.transform[:3, 3] for c in ch.cameras])
    M = umeyama(E, np.array(truth))
    res = (M[:3, :3] @ E.T).T + M[:3, 3] - np.array(truth)
    assert np.sqrt((res ** 2).sum(1).mean()) < 0.004
    # a point 80 degrees off the axis is in a fisheye photo (a pinhole would put it far outside)
    from pyarchinit_photogrammetry.engine.visibility import project_valid

    cam = ch.cameras[0]
    theta, phi = np.radians(80), np.radians(30)
    ray = np.array([np.sin(theta) * np.cos(phi), np.sin(theta) * np.sin(phi), np.cos(theta)])
    px, _depth, ok = project_valid(ch, cam, (cam.transform[:3, :3] @ ray + cam.transform[:3, 3])[None])
    f, _, cx, cy, *k = sensor.params
    r = f * theta * (1 + k[0] * theta ** 2 + k[1] * theta ** 4 + k[2] * theta ** 6 + k[3] * theta ** 8)
    assert ok[0] and np.allclose(px[0], (cx + r * np.cos(phi), cy + r * np.sin(phi)), atol=0.5)
    ch.build_depth_maps(quality="high")
    ch.build_point_cloud()
    P = (M[:3, :3] @ np.asarray(ch.point_cloud.xyz, float).T).T + M[:3, 3]
    assert len(P) > 2000 and np.median(_wall_distance(P)) < 0.006


def test_lens_model_change_and_square_pixels_mapping():
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.engine import sfm

    s = Sensor(key=0, label="x", width=4000, height=3000, model="OPENCV_FISHEYE",
               params=[1500.0, 1500.0, 2000.0, 1500.0, 0.01, -0.002, 0.0, 0.0])
    model, params = sfm.mapping_camera(s)
    assert model == "RADIAL_FISHEYE" and params == [1500.0, 2000.0, 1500.0, 0.01, -0.002]
    assert sfm.sensor_model_params(model, params, "OPENCV_FISHEYE") == ("OPENCV_FISHEYE", s.params)
    doc = ppg.Document()
    ch = doc.add_chunk("c")
    ch.sensors[0] = Sensor(key=0, label="x", width=4000, height=3000, model="OPENCV",
                           params=[3000.0, 3010.0, 1990.0, 1510.0, -0.1, 0.02, 0.001, 0.0])
    out = ch.set_sensor_model(0, "OPENCV_FISHEYE")
    assert out.model == "OPENCV_FISHEYE" and out.params == [3000.0, 3010.0, 1990.0, 1510.0, 0.0, 0.0, 0.0, 0.0]
    out = ch.set_sensor_model(0, "RADIAL_FISHEYE")
    assert out.params == [3005.0, 1990.0, 1510.0, 0.0, 0.0] and np.allclose(out.K[:2, 2], (1990, 1510))
    with pytest.raises(ValueError):
        ch.set_sensor_model(0, "EQUIDISTANT")
    # recognised from the EXIF: the lens name, or a 35 mm equivalent focal length up to 15 mm
    assert ch._sensor_for({"width": 4000, "height": 3000, "model": "X", "lens": "Samyang 8mm Fish-eye"}).model \
        == "OPENCV_FISHEYE"
    assert ch._sensor_for({"width": 4000, "height": 3000, "model": "Y", "focal_length_35mm": 14.0}).model \
        == "OPENCV_FISHEYE"
    assert ch._sensor_for({"width": 4000, "height": 3000, "model": "Z", "focal_length_35mm": 24.0}).model == "OPENCV"


def test_interface_panoramas_and_lens_model(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import pyarchinit_photogrammetry as ppg

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    scene = synthetic.make_chamber_scene()
    paths = []
    for i in range(2):
        paths.append(str(tmp_path / f"P{i}.jpg"))
        cv2.imwrite(paths[-1], synthetic.render_equirectangular(scene, STATIONS[i], 800))
    doc = ppg.Document()
    doc.add_chunk("tomba")
    doc.save(tmp_path / "t.ppg")
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: (paths, "")))
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {
        "faces": ["front", "right", "back", "left", "up"], "fov": 100.0, "size": 0}))
    w.add_panoramas()
    w.runner.wait()
    app.processEvents()
    assert len(w.chunk.cameras) == 10 and (tmp_path / "facce_360" / "up" / "P1.jpg").exists()
    assert not (tmp_path / "facce_360" / "down").exists()
    item = w.photos.item(0)
    assert "foto 360°" in item.toolTip()
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {"model": "OPENCV_FISHEYE"}))
    w.lens_model(w.chunk.cameras[:1])
    assert w.chunk.sensors[w.chunk.cameras[0].sensor_key].model == "OPENCV_FISHEYE"
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def test_command_line(tmp_path):
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    scene = synthetic.make_chamber_scene()
    cv2.imwrite(str(tmp_path / "P0.jpg"), synthetic.render_equirectangular(scene, STATIONS[0], 800))
    doc = ppg.Document()
    doc.add_chunk("c")
    doc.save(tmp_path / "p.ppg")
    run = CliRunner()
    r = run.invoke(app, ["add-panoramas", str(doc.path), str(tmp_path / "P0.jpg"), "--faces", "front,right,back,left"])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert "1 panoramas added (4 faces)" in r.output
    r = run.invoke(app, ["lens-model", str(doc.path), "opencv_fisheye"])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert {s.model for s in ppg.Document.open(doc.path).chunk.sensors.values()} == {"OPENCV_FISHEYE"}
