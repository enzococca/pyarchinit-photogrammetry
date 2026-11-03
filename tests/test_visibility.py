"""The ray casting of the occlusion tests without Open3D: trimesh (with rtree, a dependency of the program) hits the
same surface, and the reason Open3D was not used is logged once."""
from __future__ import annotations

import builtins
import logging

import numpy as np


def test_raycaster_without_open3d(monkeypatch, caplog):
    from pyarchinit_photogrammetry.engine import visibility as V

    real_import = builtins.__import__

    def without_open3d(name, *args, **kwargs):
        if name == "open3d" or name.startswith("open3d."):
            raise ImportError("No module named 'open3d'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_open3d)
    monkeypatch.setattr(V, "_FALLBACK_SAID", False)
    verts = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], float)
    faces = np.array([[0, 1, 2], [0, 2, 3]])
    with caplog.at_level(logging.INFO, logger=V.log.name):
        rc = V.Raycaster(verts, faces)
        V.Raycaster(verts, faces)
    assert rc._o3d is None
    said = [r.getMessage() for r in caplog.records if "RaycastingScene" in r.getMessage()]
    assert len(said) == 1 and "No module named 'open3d'" in said[0]
    d = rc.hit_distance(np.array([[0.5, 0.5, 2.0], [5.0, 5.0, 2.0]]), np.array([[0, 0, -1.0], [0, 0, -1.0]]))
    assert d[0] == 2.0 and np.isinf(d[1])
    visible = rc.visible_from(np.array([0.5, 0.5, 2.0]), np.array([[0.5, 0.5, 1.0], [0.5, 0.5, -1.0]]))
    assert visible.tolist() == [True, False]
