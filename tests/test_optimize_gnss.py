"""*Ottimizza camere* held by the GNSS positions of the photos, and the safety net that undoes an optimisation that
bends the block.

A nadir block over nearly flat ground has a soft mode: the block curves into a dome while the radial distortion and
the focal length make up for it in the photos, at almost no cost in reprojection error. The incremental alignment
holds it to the positions of the photos (pose priors); COLMAP's free adjustment, without them, slides along the mode
(82 RTK-fix captures of a DJI P4 Multispectral: cameras from 3 cm to metres off their positions, reprojection error
unchanged). The blocks here are synthetic COLMAP models, the observations with half a pixel of noise: no photos are
read, every adjustment takes a couple of seconds."""
from __future__ import annotations

import logging

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core.camera import Camera, Sensor
from pyarchinit_photogrammetry.engine import sfm
from pyarchinit_photogrammetry.gnss.apply import set_camera_position
from pyarchinit_photogrammetry.markers import gcp

pycolmap = pytest.importorskip("pycolmap")

W, H, F = 1000, 750, 1000.0
K1, K2 = -0.05, 0.01
ORIGIN = np.array([500000.0, 4500000.0, 60.0])  # the internal frame is the projected system less this


def _look_down(yaw: float) -> np.ndarray:
    """world -> camera of a photo looking straight down, turned by ``yaw``."""
    down = np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]])
    c, s = np.cos(yaw), np.sin(yaw)
    return np.array([[c, s, 0], [-s, c, 0], [0, 0, 1.0]]) @ down


def _rtk_block(folder, seed: int = 0, accuracy=(0.02, 0.02, 0.02), status: str | None = "fix"):
    """A drone block as an alignment leaves it: 5 strips of 8 nadir photos 40 m above ground with 3 dm of relief
    (EPSG:32632 less :data:`ORIGIN`), its COLMAP model (6000 points, observations with 0.5 px of noise, the radial
    distortion of the lens) and every photo with a position 1 cm (per axis) off its camera, of the given accuracy
    and RTK ``status``. Returns the document and the chunk."""
    rng = np.random.default_rng(seed)
    rotations, centres = [], []
    for row in range(5):
        xs = (np.arange(8) - 3.5) * 8.0
        for x in (xs if row % 2 == 0 else xs[::-1]):  # flown one way and back
            rotations.append(_look_down(np.pi * (row % 2)))
            centres.append([x, (row - 2) * 12.0, 40.0])
    xy = rng.uniform([-52.0, -45.0], [52.0, 45.0], (6000, 2))
    points = np.column_stack([xy, 0.3 * np.sin(xy[:, 0] / 15.0) + 0.2 * np.cos(xy[:, 1] / 11.0)])
    lens = pycolmap.Camera(model="OPENCV", width=W, height=H, params=[F, F, W / 2, H / 2, K1, K2, 0.0, 0.0])
    seen, tracks = [], {}
    for i, (r, c) in enumerate(zip(rotations, centres)):
        pc = (points - c) @ r.T
        px = np.asarray(lens.img_from_cam(pc))[:, :2]
        ids = np.flatnonzero((pc[:, 2] > 0) & (px[:, 0] > 2) & (px[:, 0] < W - 2) & (px[:, 1] > 2) & (px[:, 1] < H - 2))
        seen.append((ids, px[ids] + rng.normal(0.0, 0.5, (len(ids), 2))))
        for n, pid in enumerate(ids):
            tracks.setdefault(int(pid), []).append((i + 1, n))
    doc = ppg.Document()
    ch = doc.add_chunk("rtk")
    doc.save(folder / "rtk.ppg")
    txt = ch.data_dir / "sfm" / "model_txt"
    txt.mkdir(parents=True)
    (txt / "cameras.txt").write_text(f"1 OPENCV {W} {H} {F} {F} {W / 2} {H / 2} {K1} {K2} 0 0\n")
    lines = []
    for i, (r, c, (ids, xy_obs)) in enumerate(zip(rotations, centres, seen)):
        q, t = pycolmap.Rotation3d(r).quat, -r @ np.asarray(c)  # quat: x, y, z, w
        pose = " ".join(format(float(v), ".17g") for v in (q[3], q[0], q[1], q[2], *t))
        obs = " ".join(f"{x:.17g} {y:.17g} {pid + 1 if len(tracks[int(pid)]) >= 2 else -1}"
                       for (x, y), pid in zip(xy_obs, ids))
        lines.append(f"{i + 1} {pose} 1 p{i:03d}.jpg\n{obs}\n")
    (txt / "images.txt").write_text("".join(lines))
    (txt / "points3D.txt").write_text("".join(
        f"{pid + 1} {points[pid][0]:.17g} {points[pid][1]:.17g} {points[pid][2]:.17g} 128 128 128 0.5 "
        + " ".join(f"{a} {b}" for a, b in tr) + "\n" for pid, tr in tracks.items() if len(tr) >= 2))
    rec = pycolmap.Reconstruction()
    rec.read_text(str(txt))
    model = ch.data_dir / "sfm" / "model"
    model.mkdir()
    rec.write(str(model))
    ch.sensors[0] = Sensor(key=0, label="drone", width=W, height=H, params=[F, F, W / 2, H / 2, K1, K2, 0.0, 0.0])
    names = {}
    for i, c in enumerate(centres):
        cam = Camera(key=i, label=f"p{i:03d}", path=f"p{i:03d}.jpg", sensor_key=0)
        ch.cameras.append(cam)
        names[str(i)] = f"p{i:03d}.jpg"
        position = np.asarray(c) + ORIGIN + rng.normal(0.0, 0.01, 3)
        set_camera_position(cam, [float(v) for v in position], "EPSG:32632", list(accuracy), "positions_file",
                            status=status, height="ellipsoidal")
    ch.meta["sfm"] = {"names": names, "image_root": str(folder)}
    ch._set_product("reconstruction", model)
    sfm.import_reconstruction(ch, rec)
    ch.crs = "EPSG:32632"
    ch.transform = np.eye(4)
    ch.transform[:3, 3] = ORIGIN
    doc.save()
    return doc, ch


def _offsets(ch) -> np.ndarray:
    """``estimated - reference`` (m) of every camera, in the order of the cameras."""
    errors = gcp.camera_reference_errors(ch)
    return np.array([errors[c.key]["delta"] for c in ch.cameras], float)


def _rms(d: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.sum(d * d, axis=1))))


def _model_centres(ch) -> np.ndarray:
    rec = sfm.load_reconstruction(ch)
    return np.array([rec.images[i].projection_center() for i in sorted(rec.images)], float)


def _mean_reprojection_error(ch) -> float:
    rec = sfm.load_reconstruction(ch)
    rec.update_point_3d_errors()
    return float(rec.compute_mean_reprojection_error())


def _dome(rec, depth: float = 0.002) -> None:
    """Bend ``rec`` the way a free adjustment does: every camera raised by ``depth`` times the square of its
    distance from the middle of the block (1.8 m at 30 m)."""
    poses = {fid: f.rig_from_world for fid, f in rec.frames.items() if f.has_pose}
    centres = {fid: -p.rotation.matrix().T @ p.translation for fid, p in poses.items()}
    middle = np.mean(list(centres.values()), axis=0)
    for fid, pose in poses.items():
        lift = np.array([0.0, 0.0, depth * float(np.sum((centres[fid][:2] - middle[:2]) ** 2))])
        rec.frames[fid].rig_from_world = pycolmap.Rigid3d(pose.rotation,
                                                          pose.translation - pose.rotation.matrix() @ lift)


class _BendingAdjuster:
    """Stands for COLMAP's pose-prior adjuster: its solve bends the block."""

    def __init__(self, rec):
        self.rec = rec

    def solve(self):
        _dome(self.rec)


def test_rtk_block_stays_on_its_positions(tmp_path):
    """The default optimisation of a block of RTK fixes (positions at 2 cm) keeps the cameras on their positions:
    the offsets no more than twice those the alignment left, every camera height within 5 cm of its reference. The
    ground stays where it was too: with the cameras held, photos taken looking down from one height cannot tell
    the focal length from the distance to the ground, and the focal length of the alignment is kept."""
    doc, ch = _rtk_block(tmp_path)
    aligned = _offsets(ch)
    ground, focal = float(np.median(ch.tie_points.xyz[:, 2])), ch.sensors[0].params[0]
    err = ch.optimize_cameras()
    optimised = _offsets(ch)
    assert _rms(optimised) <= 2.0 * _rms(aligned), (_rms(aligned), _rms(optimised))
    assert np.abs(optimised[:, 2]).max() <= 0.05, np.abs(optimised[:, 2]).max()
    assert abs(float(np.median(ch.tie_points.xyz[:, 2])) - ground) < 0.02
    assert ch.sensors[0].params[0] == pytest.approx(focal, rel=1e-9)
    assert ch.sensors[0].params[4] == pytest.approx(K1, abs=0.002)  # the distortion is refined, near the true one
    rep = ch.meta["sfm"]["optimize"]
    assert rep["gnss"] == {"mode": "auto", "photos": 40, "states": {"fix": 40}}
    assert rep["reverted"] is False
    assert rep["camera_offsets"] == {"cameras": 40, "before": pytest.approx(_rms(aligned)),
                                     "after": pytest.approx(_rms(optimised)), "placed": False}
    assert err < 0.8  # half a pixel of noise on the observations
    doc.save()
    again = ppg.Document.open(doc.path).chunk  # the adjusted block is the one saved
    assert np.allclose(_offsets(again), optimised, atol=1e-6)


@pytest.mark.parametrize("gnss", ["off", "auto"])
def test_unconstrained_bend_is_reverted(tmp_path, monkeypatch, caplog, gnss):
    """An adjustment that takes the cameras far from their accurate positions (here one that only bends the
    block) is undone: the reconstruction, the cameras and the georeference of before are put back, the report
    says so and the log warns."""
    doc, ch = _rtk_block(tmp_path)
    centres = {c.key: c.center for c in ch.cameras}
    model = _model_centres(ch)
    transform = ch.transform.copy()
    error = _mean_reprojection_error(ch)
    built = []

    def adjuster(options, prior_options, config, priors, rec):
        built.append(len(priors))
        return _BendingAdjuster(rec)

    if gnss == "off":
        monkeypatch.setattr(pycolmap, "bundle_adjustment", lambda rec, *args, **kwargs: _dome(rec))
    else:
        monkeypatch.setattr(pycolmap, "create_pose_prior_bundle_adjuster", adjuster)
    with caplog.at_level(logging.WARNING):
        err = ch.optimize_cameras(gnss=gnss)
    rep = ch.meta["sfm"]["optimize"]
    assert rep["reverted"] is True
    offsets = rep["camera_offsets"]
    assert offsets["cameras"] == 40 and offsets["after"] > offsets["before"] + 0.3
    assert built == ([] if gnss == "off" else [40])
    for cam in ch.cameras:
        assert np.allclose(cam.center, centres[cam.key], atol=1e-9)
    assert np.allclose(_model_centres(ch), model, atol=1e-9)  # the model on disk too
    assert np.allclose(ch.transform, transform)
    assert err == pytest.approx(error)
    assert ch.meta["sfm"]["reprojection_error"] == pytest.approx(error)
    assert any("bent the block" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert not list((ch.data_dir / "sfm").glob("*before*"))  # the copy of the model goes away


def test_gnss_off_keeps_todays_behaviour(tmp_path, monkeypatch):
    """``gnss="off"`` is the adjustment of before: COLMAP's free bundle adjustment, never the pose-prior one. On
    photos whose positions are not accurate (a drone without RTK: 5/5/10 m) the default is the same, with the same
    result, and there is nothing for the safety net to judge."""
    doc, ch = _rtk_block(tmp_path / "rtk")
    built, free = [], []
    real_adjuster, real_free = pycolmap.create_pose_prior_bundle_adjuster, pycolmap.bundle_adjustment
    monkeypatch.setattr(pycolmap, "create_pose_prior_bundle_adjuster",
                        lambda *args: built.append(1) or real_adjuster(*args))
    monkeypatch.setattr(pycolmap, "bundle_adjustment", lambda *args, **kwargs: free.append(1) or real_free(*args, **kwargs))
    ch.optimize_cameras(gnss="off")
    assert built == [] and free == [1]
    assert ch.meta["sfm"]["optimize"]["gnss"] == {"mode": "off", "photos": 0, "states": {}}

    doc, ch = _rtk_block(tmp_path / "gps", accuracy=(5.0, 5.0, 10.0), status=None)
    rec = sfm.load_reconstruction(ch)  # what the optimisation did before: the free adjustment of COLMAP
    sfm._square_pixels(ch, rec)
    real_free(rec, pycolmap.BundleAdjustmentOptions())
    expected = {name: np.asarray(rec.images[i].projection_center()) for name, i in
                ((im.name, i) for i, im in rec.images.items())}
    names = ch.meta["sfm"]["names"]
    built.clear()
    free.clear()
    ch.optimize_cameras()
    assert built == [] and free == [1]
    rep = ch.meta["sfm"]["optimize"]
    assert rep["gnss"] == {"mode": "auto", "photos": 0, "states": {}}
    assert "reverted" not in rep and "camera_offsets" not in rep
    for cam in ch.cameras:
        assert np.allclose(cam.center, expected[names[str(cam.key)]], atol=1e-4)


def test_cli_optimize_gnss_option(tmp_path, monkeypatch):
    """``ppg optimize --gnss auto|off|plain|bias``: auto by default, which says how many photos held the block;
    plain and bias are options of our own engine."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.core.chunk import Chunk

    doc, ch = _rtk_block(tmp_path)
    run = CliRunner().invoke
    result = run(app, ["optimize", str(doc.path)])
    assert result.exit_code == 0, result.output
    assert "held by the GNSS positions of 40 photos (fix 40)" in result.output
    assert "camera offsets from their GNSS positions" in result.output
    saved = ppg.Document.open(doc.path).chunk
    assert saved.meta["sfm"]["optimize"]["gnss"]["photos"] == 40
    assert np.abs(_offsets(saved)[:, 2]).max() <= 0.05

    seen = []

    def optimise(self, **kwargs):
        seen.append(kwargs)
        return 0.5

    monkeypatch.setattr(Chunk, "optimize_cameras", optimise)
    for args, engine, gnss in ((["--gnss", "off"], "colmap", "off"), ([], "colmap", "auto"),
                               (["--engine", "ppg"], "ppg", "auto"),
                               (["--engine", "ppg", "--gnss", "bias"], "ppg", "bias"),
                               (["--engine", "ppg", "--gnss", "plain"], "ppg", "plain")):
        result = run(app, ["optimize", str(doc.path), *args])
        assert result.exit_code == 0, result.output
        assert (seen[-1]["engine"], seen[-1]["gnss"]) == (engine, gnss)
    count = len(seen)
    for args in (["--gnss", "plain"], ["--gnss", "bias"], ["--gnss", "always"], ["--engine", "ppg", "--gnss", "x"]):
        result = run(app, ["optimize", str(doc.path), *args])
        assert result.exit_code != 0, args
    assert "--engine ppg" in run(app, ["optimize", str(doc.path), "--gnss", "plain"]).output
    assert len(seen) == count  # refused before the adjustment


def test_dialog_says_how_many_photos_hold_the_block(tmp_path, monkeypatch):
    """*Ottimizza camere* says before the run how many photos hold the block and with which RTK state, the log
    says it after; an optimisation that bends the block is undone and the window warns."""
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
    doc, ch = _rtk_block(tmp_path)
    w = mw.MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    seen, warned = [], []
    answer = {"markers": False, "weight": 4, "pp": False, "engine": "colmap", "gnss": "auto", "rolling": False}

    def ask(cls, parent, title, fields, intro=""):
        seen.append(({f.name: f for f in fields}, intro))
        return dict(answer)

    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(ask))
    monkeypatch.setattr(w, "_warn", warned.append)
    try:
        w.optimize_cameras()
        w.runner.wait()
        app.processEvents()
        fields, intro = seen[-1]
        assert fields["gnss"].default == "auto" and fields["markers"].default is False
        assert "Posizioni GNSS usate come vincolo: 40 foto, fix." in intro
        log = " ".join(e[2] for e in w.log_pane.entries)
        assert "blocco tenuto dalle posizioni GNSS di 40 foto (fix)" in log
        assert "scarto delle camere dalle posizioni GNSS" in log and not warned
        # a free adjustment that bends the block: the alignment of before comes back, the window says so
        monkeypatch.setattr(pycolmap, "bundle_adjustment", lambda rec, *args, **kwargs: _dome(rec))
        answer["gnss"] = "off"
        w.optimize_cameras()
        w.runner.wait()
        app.processEvents()
        assert w.chunk.meta["sfm"]["optimize"]["reverted"] is True
        assert len(warned) == 1 and "L'ottimizzazione piegava il blocco" in warned[0]
    finally:
        w.setWindowModified(False)
        w.close()
