"""COLMAP's log relayed as progress (engine/colmap_log.py)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pyarchinit_photogrammetry.core.progress import Progress
from pyarchinit_photogrammetry.engine import colmap_log, timing

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


def test_the_tail_marks_the_stages_colmap_announces(tmp_path):
    """With ``stages`` every ``=== Running ... ===`` line closes the stage before it and opens its own, by its name
    in small letters with underscores; the progress is the one of a tail without them."""
    from pyarchinit_photogrammetry.engine.timing import Stages

    now = [0.0]
    stages = Stages(clock=lambda: now[0])
    events, plain = [], []
    tail = colmap_log._Tail(tmp_path / "colmap_log.", Progress(lambda f, m: events.append((round(f, 3), m))),
                            total=10, phases={"stage": (0.0, 1.0)}, stages=stages)
    old = colmap_log._Tail(tmp_path / "colmap_log.", Progress(lambda f, m: plain.append((round(f, 3), m))),
                           total=10, phases={"stage": (0.0, 1.0)})
    for t in (tail, old):
        t.line(PREFIX + "global_mapper.cc:553] === Running rotation averaging ===")
    now[0] = 4.0  # four seconds later
    for t in (tail, old):
        t.line(PREFIX + "global_mapper.cc:575] === Running global positioning ===")
    now[0] = 6.5
    stages.close()
    assert stages.as_dict() == {"rotation_averaging": 4.0, "global_positioning": 2.5}
    assert events == plain == [(0.1, "Global alignment: rotation averaging"),
                               (0.4, "Global alignment: global positioning")]


def test_a_relay_marks_the_stages_of_its_log_and_closes_the_last_one(tmp_path):
    """The stages the log of the block announces are marked as they come - names outside ``STAGES`` too - and the
    last one ends with the block: a later close adds nothing to it."""
    pytest.importorskip("pycolmap")
    from pyarchinit_photogrammetry.engine.timing import Stages

    now = [0.0]
    stages = Stages(clock=lambda: now[0])
    work = tmp_path / "work"
    lines = [f"{PREFIX}global_mapper.cc:553] === Running {name} ===\n"
             for name in ("rotation averaging", "view graph calibration")]
    with colmap_log.relay(Progress(), work, stages=stages):
        (work / "colmap_log.INFO").write_text("".join(lines))  # as COLMAP would write it
    now[0] = 5.0
    stages.close()
    assert stages.as_dict() == {"rotation_averaging": 0.0, "view_graph_calibration": 0.0}
    assert list(work.glob("colmap_log.*")) == []


# two stages logged through glog, as COLMAP logs them, one second apart; the relay goes on for another second
_LOGGED_STAGES = """
import json, sys, time
import pycolmap
from pyarchinit_photogrammetry.engine import colmap_log
from pyarchinit_photogrammetry.engine.timing import Stages

stages = Stages()
with colmap_log.relay(None, sys.argv[1], stages=stages):
    pycolmap.logging.info("=== Running a ===")
    time.sleep(1.0)
    pycolmap.logging.info("=== Running b ===")
    time.sleep(1.0)
stages.close()
print(json.dumps(stages.as_dict()))
"""


def test_with_the_live_log_a_stage_is_marked_when_colmap_logs_it(tmp_path):
    """In a process started with ``LIVE_LOG_ENV`` glog writes out every line at once, and a stage is marked within
    the polling interval of the tail of the moment COLMAP logs it: ``a``, one second long, is measured as one
    second. Without it glog keeps ``b`` in its buffer until the relay closes the file, a second later, and ``a``
    takes that second too. The child loads pycolmap before the package (which would ask glog for the same), and
    the ``GLOG_logbufsecs`` the package put in the environment of the tests is left out: only ``LIVE_LOG_ENV``
    can give it."""
    pytest.importorskip("pycolmap")
    env = {k: v for k, v in os.environ.items() if k != "GLOG_logbufsecs"}
    env.update(timing.LIVE_LOG_ENV)
    done = subprocess.run([sys.executable, "-c", _LOGGED_STAGES, str(tmp_path / "work")], capture_output=True,
                          text=True, env=env, timeout=300, close_fds=False)
    assert done.returncode == 0, done.stderr[-3000:]
    stages = json.loads(done.stdout.strip().splitlines()[-1])
    assert 0.7 <= stages["a"] <= 1.6, stages


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
