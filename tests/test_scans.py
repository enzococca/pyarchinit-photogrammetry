"""Laser scans (PTX, E57): read, imported as a chunk, registered on the photogrammetric chunk,
coloured from its photos."""
from __future__ import annotations

import shutil

import numpy as np
import pytest

from pyarchinit_photogrammetry.io import scans


def _surface(scene, n=120_000, seed=0):
    """Points of the true trench surface (world coordinates) with a millimetre of noise."""
    rng = np.random.default_rng(seed)
    x, y = rng.uniform(0.3, scene.size[0] - 0.3, n), rng.uniform(0.3, scene.size[1] - 0.3, n)
    z = scene.height_at(x, y) + rng.normal(0, 0.001, n)
    return np.column_stack([x, y, z]) + np.asarray(scene.origin)


def _pose(yaw_deg: float, position):
    a = np.radians(yaw_deg)
    R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1.0]])
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = R, position
    return T


def _write_ptx(path, world, T, intensity, registered=True):
    """One scan: points in scanner coordinates and the matrix that registers them (identity
    when ``registered`` is False: the scan stays in its own frame)."""
    R, t = T[:3, :3], T[:3, 3]
    local = (world - t) @ R
    M = T if registered else np.eye(4)
    pos = M[:3, 3]
    lines = [str(len(local)), "1", " ".join(map(str, pos)), "1 0 0", "0 1 0", "0 0 1"]
    lines += [" ".join(f"{v:.10f}" for v in row) for row in M.T]
    data = np.column_stack([local, intensity])
    lines += [" ".join(f"{v:.6f}" for v in row) for row in data]
    lines.append("0 0 0 0.5")  # an empty return: left out
    path.write_text("\n".join(lines[:9 + 1]) + "\n" + "\n".join(lines[10:]) + "\n")
    # fix the count: the zero point is part of the grid
    text = path.read_text().splitlines()
    text[0] = str(len(local) + 1)
    path.write_text("\n".join(text) + "\n")
    return path


def test_ptx_and_e57(scene, tmp_path):
    pye57 = pytest.importorskip("pye57")
    world = _surface(scene, 20_000)
    T = _pose(30, scene.origin + np.array([2.0, 1.5, 1.6]))
    inten = np.linspace(0, 1, len(world))
    s = scans.read_ptx(_write_ptx(tmp_path / "a.ptx", world, T, inten))
    assert len(s) == 1 and np.allclose(s[0].xyz, world, atol=1e-5) and np.allclose(s[0].position, T[:3, 3])
    assert s[0].rgb is None and np.allclose(s[0].intensity, inten, atol=1e-6)
    # the same scan in E57 with its pose as a quaternion
    e57 = pye57.E57(str(tmp_path / "a.e57"), mode="w")
    local = (world - T[:3, 3]) @ T[:3, :3]
    a = np.radians(30) / 2
    rgb = (np.column_stack([inten, 1 - inten, inten]) * 255).astype(np.uint8)
    e57.write_scan_raw({"cartesianX": local[:, 0], "cartesianY": local[:, 1], "cartesianZ": local[:, 2],
                        "colorRed": rgb[:, 0], "colorGreen": rgb[:, 1], "colorBlue": rgb[:, 2]},
                       rotation=np.array([np.cos(a), 0, 0, np.sin(a)]), translation=T[:3, 3], name="Stazione 1")
    e57.close()
    s = scans.read_e57(tmp_path / "a.e57")
    assert len(s) == 1 and s[0].name == "Stazione 1" and np.allclose(s[0].xyz, world, atol=1e-4)
    assert np.array_equal(s[0].rgb, rgb)


def test_scan_registered_and_coloured(processed, scene, tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    doc = ppg.Document.open(d / doc.path.name)
    photo = doc.chunk
    world = _surface(scene)
    T = _pose(35, scene.origin + np.array([1.2, 0.8, 1.5]))
    ptx = _write_ptx(tmp_path / "s.ptx", world, T, np.full(len(world), 0.6), registered=False)
    chunk = scans.import_scans(doc, [ptx], label="Scansione")
    assert len(chunk.point_cloud) == len(world) and chunk.meta["scans"][0]["points"] == len(world)
    report = doc.align_chunks(photo, [chunk], method="points", fix_scale=True)
    assert report
    got = chunk.to_world(chunk.point_cloud.xyz)
    d = got - world
    # the scan is where the trench is: in height to the millimetre; in plan the walls of the dense
    # cloud (a little fattened by the stereo matching) move the best fit by some millimetres
    assert abs(np.median(d[:, 2])) < 0.0015 and np.median(np.hypot(d[:, 0], d[:, 1])) < 0.015, d.mean(0)
    out = scans.colour_from_photos(chunk, photo)
    assert out["coloured"] > 0.95 * len(world)
    # the colours are those of the trench where the points now are (grey level against its texture)
    rgb = np.asarray(chunk.point_cloud.rgb, float)
    local = got - np.asarray(scene.origin)
    col = (local[:, 0] / scene.texel).astype(int).clip(0, scene.texture.shape[1] - 1)
    row = ((scene.size[1] - local[:, 1]) / scene.texel).astype(int).clip(0, scene.texture.shape[0] - 1)
    truth = scene.texture[row, col].astype(float).mean(1)
    assert np.corrcoef(rgb.mean(1), truth)[0, 1] > 0.8


def test_command_line(processed, scene, tmp_path):
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    project = d / doc.path.name
    world = _surface(scene, 20_000)
    ptx = _write_ptx(tmp_path / "r.ptx", world, _pose(10, scene.origin + np.array([1.0, 1.0, 1.5])),
                     np.full(len(world), 0.5))  # registered: already in the CRS of the survey
    run = CliRunner()
    r = run.invoke(app, ["import-scans", str(project), str(ptx), "--label", "Laser", "--crs", "EPSG:3004"])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert "20000 points from 1 scans" in r.output
    r = run.invoke(app, ["colour-scan", str(project), "--scan", "Laser", "--photos", "Saggio 1"])
    assert r.exit_code == 0, r.output + repr(r.exception)
    laser = next(c for c in ppg.Document.open(project).chunks if c.label == "Laser")
    assert np.asarray(laser.point_cloud.rgb).std() > 5  # no longer the grey of the intensity
