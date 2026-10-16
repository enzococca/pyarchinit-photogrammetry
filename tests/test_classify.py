"""Points classified by hand: stored in the PLY, kept by the automatic classification."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.ai import classify
from pyarchinit_photogrammetry.core import geometry
from pyarchinit_photogrammetry.core.pointcloud import PointCloud
from pyarchinit_photogrammetry.testing.synthetic import make_classified_cloud


def test_manual_flag_in_ply(tmp_path):
    rng = np.random.default_rng(0)
    xyz = rng.random((50, 3))
    old = PointCloud(xyz, classification=np.full(50, 2))
    old.save_ply(tmp_path / "old.ply")
    assert b"manual" not in (tmp_path / "old.ply").read_bytes()[:400]  # same file as before
    back = PointCloud.load(tmp_path / "old.ply")
    assert back.manual is None and (back.classification == 2).all()
    manual = rng.random(50) < 0.3
    pc = PointCloud(xyz, rgb=rng.integers(0, 255, (50, 3)), classification=np.full(50, 2), manual=manual)
    pc.save_ply(tmp_path / "new.ply")
    back = PointCloud.load(tmp_path / "new.ply")
    assert np.array_equal(back.manual, manual) and back.manual.dtype == bool
    assert np.array_equal(back.subset(manual).manual, manual[manual])
    assert np.array_equal(pc.transformed(np.diag([2.0, 2.0, 2.0, 1.0])).manual, manual)


def test_rule_based_classification_keeps_hand_labels(tmp_path):
    import pyarchinit_photogrammetry as ppg

    xyz, rgb, codes = make_classified_cloud(spacing=0.04)
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio 1")
    ch.crs = "EPSG:3004"
    ch.transform = geometry.compose_similarity(0.5, np.eye(3), xyz.mean(0))
    doc.save(tmp_path / "scavo.ppg")
    ch.point_cloud = PointCloud(ch.to_internal(xyz), rgb)
    wall = np.nonzero(codes == 64)[0][:30]
    ground = np.nonzero(codes == 2)[0][:20]
    assert classify.set_class(ch, wall, 70, label="Roccia") == 30  # indices
    mask = np.zeros(len(xyz), bool)
    mask[ground] = True
    assert classify.set_class(ch, mask, 2) == 20  # boolean mask
    with pytest.raises(ValueError):
        classify.set_class(ch, mask[:-1], 2)
    pc = ch.point_cloud
    assert pc.manual.sum() == 50 and (pc.classification[wall] == 70).all()
    assert ch.meta["classes"]["70"] == "Roccia" and classify.class_names(ch)[70] == "Roccia"
    ruled = classify.classify_chunk(ch)
    assert (ruled.classification[wall] == 70).all() and ruled.manual.sum() == 50
    assert (ruled.classification[codes == 64] == classify.MASONRY).mean() > 0.5  # the others are classified
    ruled = classify.classify_chunk(ch, overwrite_manual=True)
    assert (ruled.classification[wall] != 70).all() and not ruled.manual[wall].any()
    assert ruled.manual[ground].sum() >= 15  # still flagged where the rules agree
    classify.set_class(ch, ground[:5], 3, manual=False)
    pc = ch.point_cloud
    assert (pc.classification[ground[:5]] == 3).all() and not pc.manual[ground[:5]].any()
    assert classify.label_points(pc, wall[:4], 64) == 4 and pc.manual[wall[:4]].all()  # in memory only
    assert not ch.point_cloud.manual[wall[:4]].any()
    ch.point_cloud = pc
    remaining = int(pc.manual.sum())
    assert classify.clear_manual(ch) == remaining and not ch.point_cloud.manual.any()
