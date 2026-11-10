"""Stakes on a rendered dataset (slow): the synthetic trench with dark heads (one on the floor of the trench, 35 cm
down), photographed as the ``processed`` fixture does it (20 nadir photos and an oblique ring of 8), aligned,
georeferenced on its targets, dense cloud, DEM and orthophoto; every head is confirmed by the photos and triangulated
within 2 cm of the truth, and no cell of a target is proposed."""
from __future__ import annotations

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine import stakes
from pyarchinit_photogrammetry.testing import synthetic

HEADS = [(1.2, 2.3), (2.8, 2.3), (1.3, 0.75), (2.7, 0.75), (2.6, 1.7)]  # local: away from the targets; the last one
# on the floor of the trench


@pytest.mark.slow
def test_photos_agree_and_triangulate_within_2_cm(tmp_path):
    scene = synthetic.make_scene()
    truth = synthetic.add_stakes(scene, HEADS)
    synthetic.add_blotches(scene, 4, np.random.default_rng(11))
    synthetic.write_dataset(tmp_path, scene)  # as the processed fixture: 20 nadir photos and 8 oblique
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(tmp_path / "p.ppg")
    ch.add_photos([tmp_path / "photos"])
    ch.detect_markers("aruco_4x4_50")
    ch.match_photos(quality="high")
    ch.align_cameras()
    assert len(ch.aligned_cameras) == 28
    ch.import_reference(tmp_path / "gcp.csv", crs="EPSG:3004")
    ch.update_transform()
    ch.build_depth_maps(quality="high")
    ch.build_point_cloud()
    ch.build_dem()
    ch.build_orthomosaic()
    doc.save()
    known = [stakes.KnownPoint(n, np.asarray(xyz) + np.array([0.08, -0.05, 0.0]), "EPSG:3004", 0.02)
             for n, xyz in truth.items()]
    props = stakes.find(doc, ch, known, radius=0.4)
    for p in props:
        assert p.best is not None, (p.point.name, p.flags)
        assert not p.best.ortho_only and p.best.agreeing >= 3, (p.point.name, p.best.photos)
        err = np.linalg.norm(p.best.world - np.asarray(truth[p.point.name]))
        assert err < 0.02, (p.point.name, err)
    result = stakes.check(props)
    assert result["suspects"] == [] and result["outliers"] == []
    for code, xyz in scene.targets.items():  # the targets: detected in the chunk and dark, never a head
        p, = stakes.find(doc, ch, [stakes.KnownPoint(f"T{code}", np.asarray(xyz), "EPSG:3004", 0.02)], radius=0.15)
        assert p.best is None, (code, p.best)
    made = stakes.accept(doc, ch, props)
    assert len(made) == len(truth)
