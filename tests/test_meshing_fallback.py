"""Meshing survives a Poisson crash: the isolated worker dies, the height field takes over."""
from __future__ import annotations

import logging

import numpy as np
import pytest

from pyarchinit_photogrammetry.core.document import Document
from pyarchinit_photogrammetry.core.pointcloud import PointCloud
from pyarchinit_photogrammetry.engine import meshing


@pytest.fixture
def terrain_chunk(tmp_path):
    """A chunk whose dense cloud is a bumpy 2 x 2 m patch of ground (no cameras needed)."""
    doc = Document()
    ch = doc.add_chunk("terreno")
    doc.save(tmp_path / "t.ppg")
    rng = np.random.default_rng(0)
    g = np.linspace(0, 2, 70)
    x, y = np.meshgrid(g, g)
    z = 0.1 * np.sin(2 * x) * np.cos(3 * y) + rng.normal(0, 0.001, x.shape)
    xyz = np.column_stack([x.ravel(), y.ravel(), z.ravel()])
    n = np.column_stack([-0.2 * np.cos(2 * x).ravel(), 0.3 * np.sin(3 * y).ravel(), np.ones(xyz.shape[0])])
    n /= np.linalg.norm(n, axis=1)[:, None]
    rgb = np.full((len(xyz), 3), 120, np.uint8)
    ch.point_cloud = PointCloud(xyz, rgb, n.astype(np.float32))
    return ch


def test_poisson_in_isolated_process(terrain_chunk):
    pytest.importorskip("open3d")
    mesh = meshing.build_model(terrain_chunk, face_count=5000)
    assert len(mesh.faces) > 1000
    assert terrain_chunk.product_path("model") is not None
    # the mesh covers the patch (bounding box within a few cm of the cloud)
    lo, hi = mesh.bounds
    assert lo[0] < 0.1 and hi[0] > 1.9 and lo[1] < 0.1 and hi[1] > 1.9


def test_poisson_crash_falls_back_to_height_field(terrain_chunk, monkeypatch, caplog):
    pytest.importorskip("open3d")
    monkeypatch.setenv("PPG_POISSON_SIMULATE_CRASH", "1")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        mesh = meshing.build_model(terrain_chunk, face_count=5000)
    assert len(mesh.faces) > 1000
    assert any("height field" in r.getMessage() for r in caplog.records)
    assert any("crashed" in r.getMessage() for r in caplog.records)
    assert terrain_chunk.product_path("model").exists()
