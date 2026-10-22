"""COLMAP's log relayed as progress (engine/colmap_log.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from pyarchinit_photogrammetry.core.progress import Progress
from pyarchinit_photogrammetry.engine import colmap_log

PREFIX = "I20261022 14:42:11.123456 19286 "


def test_parse_the_lines_that_measure_progress():
    line = colmap_log.parse(PREFIX + "feature_extraction.cc:270] Processed file [3/14]")
    assert line.kind == "features" and line.a == 3 and line.b == 14
    assert line.fraction == pytest.approx(3 / 14) and line.message == "Key points of photo 3 of 14"
    # exhaustive matching: block (2, 3) of 4 x 4
    line = colmap_log.parse(PREFIX + "pairing.cc:100] Processing block [2/4, 3/4]")
    assert line.kind == "blocks2" and line.a == 7 and line.b == 16 and line.fraction == pytest.approx(7 / 16)
    line = colmap_log.parse(PREFIX + "pairing.cc:200] Processing block [5/9]")
    assert line.kind == "blocks" and line.fraction == pytest.approx(5 / 9)
    # the mapper: the photos placed so far, the fraction waits for the total
    line = colmap_log.parse(PREFIX + "incremental_pipeline.cc:300] Registering image #7 (num_reg_frames=120)")
    assert line.kind == "register" and line.a == 7 and line.b == 120 and line.fraction is None
    assert line.message == "Placing photo #7: 120 in the block"
    assert colmap_log.parse("=> Image sees 812 / 1500 points").kind == "sees"
    assert colmap_log.parse("Retriangulation and Global bundle adjustment").kind == "note"
    # the global mapper: its stages
    line = colmap_log.parse(PREFIX + "global_mapper.cc:50] === Running rotation averaging ===")
    assert line.kind == "stage" and line.fraction == colmap_log.STAGES["rotation averaging"]
    assert line.message == "Global alignment: rotation averaging"
    line = colmap_log.parse("=== Reconstructing component 1 / 2 with 57 images ===")
    assert line.kind == "note" and line.message == "Component 1 of 2: 57 photos"
    # the dense steps
    assert colmap_log.parse("Fusing image [5/9] with index 3").fraction == pytest.approx(5 / 9)
    assert colmap_log.parse("Undistorting image [2/9]").kind == "undistort"
    # lines that say nothing of the progress
    assert colmap_log.parse(PREFIX + "timer.cc:90] Elapsed time: 0.001 [minutes]") is None
    assert colmap_log.parse("") is None


def test_the_tail_maps_the_lines_into_the_phases(tmp_path):
    events = []
    tail = colmap_log._Tail(tmp_path / "colmap_log.", Progress(lambda f, m: events.append((round(f, 3), m))),
                            total=10, phases={"register": (0.2, 0.8), "features": (0.0, 0.2)})
    tail.line("Processed file [5/10]")
    tail.line("Finding good initial image pair")
    tail.line("Registering image #3 (num_reg_frames=4)")
    tail.line("=> Image sees 100 / 200 points")
    tail.line("=> Image sees 101 / 200 points")  # within a second of the previous: not shown
    tail.line("Registering image #9 (num_reg_frames=10)")
    tail.line("Elapsed time: 0.1 [minutes]")
    assert events == [(0.1, "Key points of photo 5 of 10"),
                      (0.1, "Looking for the first pair of photos"),  # a message keeps the fraction
                      (0.52, "Placing photo #3: 4 in the block"),
                      (0.52, "   it sees 100 of 200 points"),
                      (1.0, "Placing photo #9: 10 in the block")]
    assert tail.lines == 6


def test_relay_reports_colmaps_feature_extraction(scene_dir: Path, tmp_path):
    pycolmap = pytest.importorskip("pycolmap")
    photos = scene_dir / "photos"
    names = sorted(p.name for p in photos.glob("*.jpg"))[:6]
    events = []
    progress = Progress(lambda f, m: events.append((f, m)))
    lg = pycolmap.logging
    before = (lg.minloglevel, lg.stderrthreshold, lg.alsologtostderr)
    ext = pycolmap.FeatureExtractionOptions()
    ext.max_image_size = 300
    with colmap_log.relay(progress.sub(0.0, 0.5), tmp_path / "work", phases={"features": (0.0, 1.0)}):
        assert list((tmp_path / "work").glob("colmap_log.*")) == []  # the file comes with the first line
        pycolmap.extract_features(str(tmp_path / "db.db"), str(photos), image_names=names,
                                  camera_mode=pycolmap.CameraMode.SINGLE, extraction_options=ext)
    counted = [(f, m) for f, m in events if m.startswith("Key points of photo")]
    assert len(counted) == len(names)
    assert [m for _, m in counted][-1] == f"Key points of photo {len(names)} of {len(names)}"
    fractions = [f for f, _ in counted]
    assert fractions == sorted(fractions) and fractions[-1] == pytest.approx(0.5)  # within the sub-range
    assert list((tmp_path / "work").glob("colmap_log.*")) == []  # removed afterwards
    assert (lg.minloglevel, lg.stderrthreshold, lg.alsologtostderr) == before
    with colmap_log.relay(progress, tmp_path / "again"):
        with colmap_log.relay(progress, tmp_path / "nested"):  # the inner one does nothing
            assert not (tmp_path / "nested").exists()
