"""Turntable pottery demo: synthetic photos of a bowl on a target mat, aligned in the local
metric frame of the mat, meshed, and measured through the profile tool."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from pyarchinit_photogrammetry.markers.targets import detect
from pyarchinit_photogrammetry.testing.synthetic import make_turntable_scene, render_mesh, turntable_poses


def test_turntable_render_shows_targets(tmp_path):
    scene = make_turntable_scene()
    K = np.array([[1100.0, 0, 600], [0, 1100.0, 450], [0, 0, 1]])
    poses = turntable_poses()
    seen = set()
    for i in (0, 10, 20, 26):  # both rings, several azimuths
        img = render_mesh(scene, poses[i], K, (1200, 900))
        assert img.shape == (900, 1200, 3) and img.std() > 30
        codes = {int(d["code"]) for d in detect(img, "aruco_4x4_50")}
        assert codes and codes <= set(scene.targets)
        seen |= codes
        cv2.imwrite(str(tmp_path / f"{i}.jpg"), img)
    assert len(seen) >= 8  # most of the ring is seen from a few viewpoints


@pytest.mark.slow
def test_pottery_demo_pipeline_measures_the_bowl(tmp_path):
    """Rim 16 cm, height 12 cm: the whole chain photos -> mesh -> profile must find them
    within a few millimetres (the turntable targets give scale and the vertical axis)."""
    from pyarchinit_photogrammetry.core.document import Document
    from pyarchinit_photogrammetry.demo import process_pottery_demo
    from pyarchinit_photogrammetry.testing.synthetic import write_pottery_dataset

    write_pottery_dataset(tmp_path / "turntable")
    doc = Document()
    ch = doc.add_chunk("Ciotola")
    doc.save(tmp_path / "ceramica.ppg")
    ch.add_photos([tmp_path / "turntable" / "photos"])
    assert len(ch.detect_markers("aruco_4x4_50")) == 12
    assert ch.import_reference(tmp_path / "turntable" / "targets.csv", crs="LOCAL") == 12
    svg = process_pottery_demo(doc, tmp_path / "profilo.svg")
    assert svg.exists() and len(ch.aligned_cameras) >= 28
    meas = ch.meta["pottery"]
    assert abs(meas["rim_diameter_m"] - 0.16) < 0.006, meas
    assert abs(meas["max_diameter_m"] - 0.168) < 0.01, meas
    # the base stands on the mat and is never photographed: the profile covers the visible
    # body (from about 3 cm above the mat to the rim), so the height is a lower bound
    assert 0.075 < meas["height_m"] <= 0.125, meas
    assert meas.get("wall_thickness_m") is None or 0.003 < meas["wall_thickness_m"] < 0.009, meas
