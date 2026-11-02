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


def test_poisson_points_thinned_to_the_memory(terrain_chunk, monkeypatch):
    """A cloud above the memory budget is thinned before the Poisson (13.6 M points at depth 12
    filled 24.8 GB on a 24 GB Mac): the report says so and the mesh is still a surface."""
    pytest.importorskip("open3d")
    monkeypatch.delenv("PPG_POISSON_MAX_POINTS", raising=False)
    monkeypatch.setattr(meshing, "poisson_point_budget", lambda: 1000)
    mesh = meshing.build_model(terrain_chunk, face_count=2000)
    report = terrain_chunk.meta["model"]
    assert report["thinned_points"] == [len(terrain_chunk.point_cloud), 1000]
    assert report["poisson_points"] == 1000
    assert len(mesh.faces) > 0 and np.isfinite(mesh.vertices).all()
    lo, hi = mesh.bounds
    assert lo[0] < 0.2 and hi[0] > 1.8 and lo[1] < 0.2 and hi[1] > 1.8
    monkeypatch.undo()
    monkeypatch.setenv("PPG_POISSON_MAX_POINTS", "0")  # no thinning at all
    meshing.build_model(terrain_chunk, face_count=2000)
    report = terrain_chunk.meta["model"]
    assert "thinned_points" not in report and report["poisson_points"] == len(terrain_chunk.point_cloud)


def test_poisson_budget_from_the_memory_and_its_override(monkeypatch):
    monkeypatch.delenv("PPG_POISSON_MAX_POINTS", raising=False)
    monkeypatch.setattr(meshing, "physical_memory", lambda: 24 * 1000 ** 3)
    assert meshing.poisson_point_budget() == 4_800_000
    monkeypatch.setattr(meshing, "physical_memory", lambda: None)
    assert meshing.poisson_point_budget() is None
    monkeypatch.setenv("PPG_POISSON_MAX_POINTS", "0")
    xyz = np.zeros((5000, 3))
    assert meshing._thin_for_poisson(xyz, None)[2] is None  # 0: no thinning
    monkeypatch.setenv("PPG_POISSON_MAX_POINTS", "1200")
    normals = np.arange(15000, dtype=float).reshape(5000, 3)
    xyz = normals * 2
    thin_xyz, thin_n, thinned = meshing._thin_for_poisson(xyz, normals)
    assert thinned == (5000, 1200) and len(thin_xyz) == 1200 and np.array_equal(thin_xyz, thin_n * 2)


def test_physical_memory_is_known_here():
    ram = meshing.physical_memory()
    assert ram is None or ram > 1 << 28


def test_physical_memory_on_windows_from_kernel32(monkeypatch):
    """Windows has no ``sysconf`` and psutil is optional: ``GlobalMemoryStatusEx`` of kernel32
    (here a stand-in that fills the structure as Windows does)."""
    import ctypes
    import sys
    import types

    calls = []

    def status_ex(ref):
        status = ref._obj
        calls.append(status.dwLength)
        status.ullTotalPhys = 32 * 1024 ** 3
        return 1

    if sys.platform != "win32":
        fake = types.SimpleNamespace(kernel32=types.SimpleNamespace(GlobalMemoryStatusEx=status_ex))
        monkeypatch.setattr(ctypes, "windll", fake, raising=False)
        monkeypatch.setattr(meshing, "sys", types.SimpleNamespace(platform="win32"))
        monkeypatch.setitem(sys.modules, "psutil", None)  # not installed
        assert meshing.physical_memory() == 32 * 1024 ** 3
        assert calls == [64]  # dwLength = sizeof(MEMORYSTATUSEX)
    else:  # the real call
        assert meshing._windows_physical_memory() > 1 << 28
