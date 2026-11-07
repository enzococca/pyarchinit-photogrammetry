"""Several chunks seen together: the points of a chunk placed in the system of another, their distances to its surface
(the overlap after *Allinea chunk*), and the 3D and 2D views that show them before and after the alignment."""
from __future__ import annotations

import os

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core import geometry
from pyarchinit_photogrammetry.core.pointcloud import PointCloud
from pyarchinit_photogrammetry.engine import chunks as chunks_mod

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

MARKS = np.array([[0.2, 0.2], [3.8, 0.4], [3.6, 2.7], [0.5, 2.8]])  # four markers on the trench


def surface(x, y):
    """A trench floor with a bank: shape enough for a normal and a distance, in metres."""
    return 0.15 * np.sin(x * 1.3) + 0.1 * np.cos(y * 1.7) - 0.3 * (x > 2.0)


def _cloud(n, seed, x0=0.0, x1=4.0):
    rng = np.random.default_rng(seed)
    x, y = rng.uniform(x0, x1, n), rng.uniform(0.0, 3.0, n)
    xyz = np.column_stack([x, y, surface(x, y)])
    rgb = np.column_stack([(x * 60) % 256, (y * 80) % 256, np.full(n, 120)]).astype(np.uint8)
    return xyz, rgb


def make_project(path, shift=(0.0, 0.0, 0.03), n=30_000):
    """Three chunks of the same trench: A the reference; B an aligned copy (the same surface, other points); C the
    same as B but shifted by ``shift`` through its transform (a chunk not aligned yet). Markers M1-M4 in each, at the
    true positions of their internal frame."""
    doc = ppg.Document()
    doc.save(path)
    for label, seed, move in (("A", 1, None), ("B", 2, None), ("C", 3, shift)):
        ch = doc.add_chunk(label)
        xyz, rgb = _cloud(n, seed)
        ch.point_cloud = PointCloud(xyz, rgb)
        for i, (x, y) in enumerate(MARKS):
            ch.add_marker(f"M{i + 1}", position=np.array([x, y, surface(x, y)]))
        if move is not None:
            T = geometry.identity()
            T[:3, 3] = move
            ch.transform = T
    doc.chunk = doc.chunks[0]
    doc.save()
    return doc


@pytest.fixture
def three(tmp_path):
    return make_project(tmp_path / "tre.ppg")


def test_preview_points_are_a_stride_of_the_file(three):
    a = three.chunks[0]
    xyz, rgb, what = chunks_mod.preview_points(a, 1000)
    assert what == "point_cloud" and 1000 >= len(xyz) >= 900 and rgb.shape == (len(xyz), 3)
    whole = a.point_cloud.xyz
    np.testing.assert_allclose(xyz[1], whole[-(-len(whole) // 1000)])  # every so many points of the file
    empty = three.add_chunk("vuoto")
    with pytest.raises(ValueError):
        chunks_mod.preview_points(empty)


def test_distances_near_zero_for_an_aligned_copy_and_the_shift_for_a_shifted_one(three):
    a, b, c = three.chunks
    surf = chunks_mod.DistanceSurface(a)
    rb = chunks_mod.cloud_distances(a, b, surface=surf)
    rc = chunks_mod.cloud_distances(a, c, surface=surf)
    assert rb["placed"] and rb["what"] == "point_cloud"
    assert abs(rb["median"]) < 0.002 and rb["nmad"] < 0.004 and rb["overlap"] > 0.9
    assert rc["median"] == pytest.approx(0.03, abs=0.003)  # up: the normals turn upwards
    assert rc["overlap"] < 0.3  # 3 cm is more than three spacings of 30 000 points on 12 m2
    # by key or label through the project, the default threshold three spacings
    r = chunks_mod.cloud_distances("A", "C", doc=three, threshold=0.05)
    assert r["threshold"] == 0.05 and r["overlap"] > 0.9


def test_a_point_beyond_the_edge_is_far(tmp_path):
    doc = ppg.Document()
    doc.save(tmp_path / "bordo.ppg")
    a, b = doc.add_chunk("A"), doc.add_chunk("B")
    a.point_cloud = PointCloud(_cloud(20_000, 1, 0.0, 2.0)[0])
    b.point_cloud = PointCloud(_cloud(20_000, 2, 3.0, 4.0)[0])  # the same surface, a metre beyond A
    r = chunks_mod.cloud_distances(a, b)
    assert r["covered"] == 0.0 and r["overlap"] == 0.0 and r["median"] is None


def test_chunks_in_other_systems(tmp_path):
    doc = ppg.Document()
    doc.save(tmp_path / "sistemi.ppg")
    a, b, c = doc.add_chunk("A"), doc.add_chunk("B"), doc.add_chunk("C")
    for ch in (a, b, c):
        ch.point_cloud = PointCloud(_cloud(5000, 1)[0])
    T = geometry.identity()
    T[:3, 3] = (300000.0, 4600000.0, 50.0)
    a.transform, a.crs = T.copy(), "EPSG:32633"
    # B: the same place in the zone next door (EPSG:32634): converted with PROJ
    from pyarchinit_photogrammetry.core import crs as crs_mod

    there = crs_mod.transform_points(np.array([[300000.0, 4600000.0, 50.0]]), "EPSG:32633", "EPSG:32634")[0]
    T2 = geometry.identity()
    T2[:3, 3] = there
    b.transform, b.crs = T2, "EPSG:32634"
    world, placed = chunks_mod.shown_in(b, np.zeros((1, 3)), a.crs)
    assert placed and np.allclose(world[0], T[:3, 3], atol=0.01)
    r = chunks_mod.cloud_distances(a, b)
    assert r["placed"] and abs(r["median"]) < 0.02  # a few mm of the conversion over 4 m
    world, placed = chunks_mod.shown_in(c, np.zeros((1, 3)), a.crs)  # local frame: in its own
    assert not placed and np.allclose(world, 0.0)


def test_align_chunks_check_reports_overlap_and_distance(three):
    report = three.align_chunks("A", ["C"], method="markers", check=True)
    d = report["chunks"][0]["distances"]
    assert d["label"] == "C" and abs(d["median"]) < 0.002 and d["overlap"] > 0.9
    report = three.align_chunks("A", ["B"], method="markers")  # without check: nothing measured
    assert "distances" not in report["chunks"][0]


def test_cli_chunk_distances_and_align_check(three):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    r = CliRunner().invoke(app, ["chunk-distances", str(three.path), "B", "C", "--reference", "A"])
    assert r.exit_code == 0, r.output
    assert "B -> A" in r.output and "C -> A" in r.output and "median" in r.output
    r = CliRunner().invoke(app, ["chunk-distances", str(three.path), "C", "-r", "A", "--json"])
    import json

    assert json.loads(r.output)[0]["median"] == pytest.approx(0.03, abs=0.003)
    r = CliRunner().invoke(app, ["align-chunks", str(three.path), "C", "-r", "A", "--check"])
    assert r.exit_code == 0, r.output
    assert "overlap" in r.output and "median distance" in r.output
