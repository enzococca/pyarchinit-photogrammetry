"""The libraries the engine is built on - COLMAP through pycolmap, Open3D - asked what they can run on a GPU here
(engine.upstream), the versions of them this install accepts (doctor) and the ceiling that keeps pip on the versions
the program was tested with (pyproject.toml). The weekly workflow that tries their newest versions is tested in
tests/test_upstream_check.py."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from pyarchinit_photogrammetry import doctor
from pyarchinit_photogrammetry.engine import dense, upstream

ROOT = Path(__file__).resolve().parents[1]


def test_colmap_gpu_reads_the_flags_of_the_build(monkeypatch):
    """The GPU back-ends of a build of COLMAP are its ``has_<name>`` flags that are true: one a later build adds shows
    without a change here."""
    fake = types.ModuleType("pycolmap")
    fake.has_cuda = False
    monkeypatch.setitem(sys.modules, "pycolmap", fake)
    assert upstream.colmap_gpu() == []
    fake.has_cuda = True
    assert upstream.colmap_gpu() == ["cuda"]
    fake.has_metal = True
    fake.has_options = "yes"  # only the flags that are True
    assert upstream.colmap_gpu() == ["cuda", "metal"]
    monkeypatch.setitem(sys.modules, "pycolmap", None)  # pycolmap does not load
    assert upstream.colmap_gpu() == []


def test_open3d_gpu_asks_each_device_of_the_build(monkeypatch):
    """The devices of Open3D besides the CPU: each module of ``open3d.core`` whose ``is_available()`` says yes; one
    that fails to answer is not available."""
    core = types.ModuleType("open3d.core")
    core.cuda = types.SimpleNamespace(is_available=lambda: False)
    core.sycl = types.SimpleNamespace(is_available=lambda: True)

    def broken():
        raise RuntimeError("no driver")

    core.metal = types.SimpleNamespace(is_available=broken)
    core.Device = type("Device", (), {})
    core.Tensor = types.SimpleNamespace(is_available=lambda device: True)  # needs an argument: not a device module
    o3d = types.ModuleType("open3d")
    o3d.core = core
    monkeypatch.setitem(sys.modules, "open3d", o3d)
    monkeypatch.setitem(sys.modules, "open3d.core", core)
    assert upstream.open3d_gpu() == ["sycl"]
    core.metal = types.SimpleNamespace(is_available=lambda: True)
    assert upstream.open3d_gpu() == ["metal", "sycl"]
    monkeypatch.setitem(sys.modules, "open3d", None)
    monkeypatch.setitem(sys.modules, "open3d.core", None)
    assert upstream.open3d_gpu() == []


def test_colmaps_patchmatch_only_with_cuda(monkeypatch):
    """The depth maps go to COLMAP's PatchMatch with its CUDA back-end only: another back-end of a later build is
    shown by the diagnosis and used once measured against the program's own kernels."""
    monkeypatch.setattr(upstream, "colmap_gpu", lambda: ["cuda"])
    assert dense.choose_backend("auto") == "colmap"
    monkeypatch.setattr(upstream, "colmap_gpu", lambda: ["metal"])
    assert dense.choose_backend("auto") != "colmap"
    monkeypatch.setattr(upstream, "colmap_gpu", lambda: [])
    assert dense.choose_backend("auto") != "colmap"


def test_doctor_names_the_gpu_back_ends_of_colmap_and_open3d(monkeypatch):
    monkeypatch.setattr(upstream, "colmap_gpu", lambda: [])
    check = doctor.colmap_gpu_check()
    assert (check.name, check.level) == ("COLMAP GPU", doctor.OK) and check.detail.startswith("none")
    monkeypatch.setattr(upstream, "colmap_gpu", lambda: ["cuda"])
    check = doctor.colmap_gpu_check()
    assert check.level == doctor.OK and check.detail.startswith("CUDA") and "depth maps" in check.detail
    monkeypatch.setattr(upstream, "colmap_gpu", lambda: ["cuda", "metal"])
    check = doctor.colmap_gpu_check()
    assert "CUDA" in check.detail and "METAL" in check.detail and "not used yet" in check.detail
    monkeypatch.setattr(upstream, "open3d_gpu", lambda: [])
    check = doctor.open3d_gpu_check()
    assert (check.name, check.level) == ("Open3D GPU", doctor.OK) and check.detail.startswith("none")
    monkeypatch.setattr(upstream, "open3d_gpu", lambda: ["cuda"])
    check = doctor.open3d_gpu_check()
    assert check.detail.startswith("CUDA") and "on the CPU" in check.detail


REQUIRES = ["numpy>=1.24", "pycolmap<4.3,>=4.2.1", 'open3d!=0.20.0,<0.21,>=0.18; extra == "mesh"',
            'open3d!=0.20.0,<0.21,>=0.18; extra == "all"']


def test_doctor_knows_the_versions_this_install_accepts(monkeypatch):
    """A version of pycolmap or Open3D outside what the program asks for (``pip install -U pycolmap`` after it) is
    named with the requirement to go back to; the packaged application, without metadata, carries the versions it was
    built and tested with."""
    monkeypatch.setattr(doctor, "_requires", lambda: REQUIRES)
    assert doctor.outside("pycolmap", "4.2.1") is None
    assert doctor.outside("pycolmap", "4.3.0") == "pycolmap<4.3,>=4.2.1"
    assert doctor.outside("pycolmap", "4.3.0.dev0") == "pycolmap<4.3,>=4.2.1"  # a pre-release of the ceiling
    assert doctor.outside("pycolmap", "3.12.0") == "pycolmap<4.3,>=4.2.1"
    assert doctor.outside("open3d", "0.19.0") is None
    assert doctor.outside("open3d", "0.20.0") == "open3d!=0.20.0,<0.21,>=0.18"
    assert doctor.outside("laspy", "9.9") is None  # not one the program bounds
    assert doctor.outside("pycolmap", None) is None and doctor.outside("pycolmap", "?") is None
    monkeypatch.setattr(doctor, "_requires", lambda: None)
    assert doctor.outside("pycolmap", "9.9") is None


def test_doctor_warns_of_a_version_the_program_was_not_tested_with(monkeypatch):
    from pyarchinit_photogrammetry.engine import isolate

    monkeypatch.setattr(isolate, "run_isolated", lambda fn, timeout=None: None)  # no SpatiaLite probe here
    monkeypatch.setattr(doctor, "_requires", lambda: REQUIRES)
    real = doctor._version
    versions = {"pycolmap": "4.3.0", "open3d": "0.21.0"}
    monkeypatch.setattr(doctor, "_version", lambda mod: versions.get(mod) or real(mod))
    checks = {c.name: c for c in doctor.run_checks(gui=False).checks}
    assert checks["pycolmap"].level == doctor.WARN and checks["pycolmap"].detail.startswith("4.3.0")
    assert 'pip install "pycolmap<4.3,>=4.2.1"' in checks["pycolmap"].hint
    assert checks["Open3D"].level == doctor.WARN and 'pip install "open3d!=0.20.0,<0.21,>=0.18"' in checks["Open3D"].hint
    versions.update(pycolmap="4.2.1", open3d="0.20.0")  # the known crash keeps its own words
    checks = {c.name: c for c in doctor.run_checks(gui=False).checks}
    assert checks["pycolmap"].level == doctor.OK
    assert checks["Open3D"].level == doctor.WARN and "Poisson" in checks["Open3D"].hint
    names = list(checks)
    assert names.index("COLMAP GPU") == names.index("Open3D") + 1
    assert names.index("Open3D GPU") == names.index("COLMAP GPU") + 1
