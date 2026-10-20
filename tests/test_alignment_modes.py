"""Faster alignment of many photos: the pairs that overlap preselected on small images (as the
generic preselection of Metashape) and all the cameras placed at once (global SfM), with the same
accuracy on the control points of the synthetic trench, and the way back to the incremental
alignment when the global one leaves photos out."""
from __future__ import annotations

import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine import sfm


def _matched(scene_dir, tmp_path, pairs):
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(tmp_path / "project.ppg")
    ch.add_photos([scene_dir / "photos"])
    ch.detect_markers("aruco_4x4_50")
    ch.match_photos(quality="high", pair_selection=pairs)
    return doc, ch


def test_generic_preselection_matches_only_the_pairs_that_overlap(scene_dir, tmp_path):
    _doc, ch = _matched(scene_dir, tmp_path, "generic")
    n = len(ch.cameras)
    info = ch.meta["sfm"]
    assert info["pair_selection"] == "generic"
    assert 2 * n <= info["pairs"] < n * (n - 1) // 2  # fewer than every pair, enough to hold the block
    assert ch.align_cameras(mapper="incremental") == n
    assert not list((ch.data_dir / "sfm").glob("preselection.db*"))  # the small database is gone


def test_global_alignment_keeps_the_accuracy(scene_dir, tmp_path):
    _doc, ch = _matched(scene_dir, tmp_path, "exhaustive")
    assert ch.align_cameras(mapper="global") == len(ch.cameras)
    assert ch.meta["sfm"]["mapper"] == "global"
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    report = ch.update_transform()
    assert report["control_rmse"] < 0.002  # as the incremental alignment (test_pipeline)


def test_a_failing_global_alignment_goes_back_to_one_photo_at_a_time(scene_dir, tmp_path, monkeypatch):
    _doc, ch = _matched(scene_dir, tmp_path, "exhaustive")

    def broken(*_args, **_kwargs):
        raise RuntimeError("no rotations")

    monkeypatch.setattr(sfm, "_global_mapping", broken)
    assert ch.align_cameras(mapper="global") == len(ch.cameras)
    assert ch.meta["sfm"]["mapper"] == "incremental"


def test_auto_choices(scene_dir, tmp_path, monkeypatch):
    """Up to ``EXHAUSTIVE_UP_TO`` photos every pair and, below ``GLOBAL_FROM``, one photo at a time."""
    _doc, ch = _matched(scene_dir, tmp_path, "auto")
    assert ch.meta["sfm"]["pair_selection"] == "exhaustive"
    ch.align_cameras()
    assert ch.meta["sfm"]["mapper"] == "incremental"
    monkeypatch.setattr(sfm, "GLOBAL_FROM", 10)
    ch.align_cameras()
    assert ch.meta["sfm"]["mapper"] == "global"
    with pytest.raises(ValueError):
        ch.align_cameras(mapper="somehow")


def test_strongest_pairs_keep_every_photo_and_the_links_between_groups():
    # two flights of ten photos, every pair within a flight overlapping a lot, a few weak pairs across
    low = [f"L{i}" for i in range(10)]
    high = [f"H{i}" for i in range(10)]
    weights = {}
    for group, base in ((low, 2000), (high, 1500)):
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                weights[(a, b)] = base - 50 * abs(group.index(b) - i)  # neighbours in the strip overlap most
    for i in range(10):
        weights[(f"H{i}", f"L{(3 * i) % 10}")] = 20 + i  # few matches across the heights
    kept = sfm.strongest_pairs(weights, per_photo=3, links=4)
    assert kept < set(weights)
    for photo in low + high:  # every photo keeps its strongest pairs
        assert sum(photo in p for p in kept) >= 3
    across = [p for p in kept if p[0][0] != p[1][0]]
    assert len(across) == 4 and min(weights[p] for p in across) == 26  # the strongest links came back
