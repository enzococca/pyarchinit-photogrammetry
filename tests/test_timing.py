"""The time of a step and of each of its stages (engine/timing.py), kept in the chunk by matching and alignment."""
from __future__ import annotations

import contextlib
import json
import logging

import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine import timing
from pyarchinit_photogrammetry.engine.timing import Stages


def test_stages_add_up_and_keep_their_order():
    now = [0.0]
    s = Stages(clock=lambda: now[0])
    with s.stage("features"):
        now[0] = 2.0
    with s.stage("pairs"):
        now[0] = 5.0
    with s.stage("features"):
        now[0] = 6.5
    assert s.as_dict() == {"features": 3.5, "pairs": 3.0}
    assert list(s.as_dict()) == ["features", "pairs"]


def test_mark_closes_the_stage_before_it():
    now = [0.0]
    s = Stages(clock=lambda: now[0])
    s.mark("rotation_averaging")
    now[0] = 4.0
    s.mark("global_positioning")
    now[0] = 9.0
    s.close()
    assert s.as_dict() == {"rotation_averaging": 4.0, "global_positioning": 5.0}
    now[0] = 20.0
    s.close()  # nothing open: nothing added
    assert s.as_dict() == {"rotation_averaging": 4.0, "global_positioning": 5.0}


def test_a_stage_that_raises_keeps_its_time():
    now = [0.0]
    s = Stages(clock=lambda: now[0])
    with pytest.raises(RuntimeError, match="cancelled"), s.stage("pairs"):
        now[0] = 1.25
        raise RuntimeError("cancelled")
    assert s.as_dict() == {"pairs": 1.25}


def test_store_keeps_each_step_beside_the_record_of_the_engine(caplog):
    ch = ppg.Document().add_chunk("c")
    engine = {"match_photos": {"requested": "auto", "used": "classic", "fallback": None}}
    ch.meta["engine"] = dict(engine)
    timing.store(ch, "match_photos", 12.34567, {"features": 3.0, "pairs": 8.5})
    timing.store(ch, "align_cameras", 2.0004, {}, mapper="global")
    assert ch.meta["timings"] == {"match_photos": {"seconds": 12.346, "stages": {"features": 3.0, "pairs": 8.5}},
                                  "align_cameras": {"seconds": 2.0, "stages": {}, "mapper": "global"}}
    assert ch.meta["engine"] == engine
    timing.store(ch, "match_photos", 1.0, {"features": 1.0})  # a step measured again replaces its record
    assert ch.meta["timings"]["match_photos"] == {"seconds": 1.0, "stages": {"features": 1.0}}
    # what a project holds under the key and is not a dict (a plugin's, a script's) is replaced, its type said
    ch.meta["timings"] = "a value of somebody else"
    with caplog.at_level(logging.WARNING):
        timing.store(ch, "align_cameras", 1.0, {})
    assert ch.meta["timings"] == {"align_cameras": {"seconds": 1.0, "stages": {}}}
    assert "str" in caplog.text and "somebody" not in caplog.text


def test_matching_and_alignment_leave_their_timings(scene_dir, tmp_path):
    """On the smallest set the alignment tests align (14 photos of the trench): the matching split into its parts,
    the alignment timed, beside the record of the engine, nothing of the photos in the record, and the record saved
    with the project. A matching with both preselections times them as one stage."""
    photos = sorted((scene_dir / "photos").glob("*.jpg"))[:14]
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(tmp_path / "project.ppg")
    ch.add_photos(photos)
    ch.match_photos()
    ch.align_cameras()
    t = ch.meta["timings"]
    assert {"features", "pairs"} <= set(t["match_photos"]["stages"])
    assert sum(t["match_photos"]["stages"].values()) <= t["match_photos"]["seconds"] + 0.01
    assert t["align_cameras"]["seconds"] > 0 and "failed" not in t["align_cameras"]
    assert set(t) == set(ch.meta["engine"]) == {"match_photos", "align_cameras"}  # two keys side by side
    text = json.dumps(t)
    assert ch.meta["sfm"]["image_root"] not in text
    assert not any(name in text for name in ch.meta["sfm"]["names"].values())
    assert not any(c.label in text for c in ch.cameras)
    doc.save()
    assert ppg.Document.open(tmp_path / "project.ppg").chunks[0].meta["timings"] == t
    ch.match_photos(generic=True, reference="sequential")
    stages = ch.meta["timings"]["match_photos"]["stages"]
    native = ch.meta["engine"]["match_photos"]["used"] == "native"  # auto: native since its gate, where the core is
    assert list(stages) == ["features", "preselection", "pairs"] + (["export"] if native else [])
    assert sum(stages.values()) <= ch.meta["timings"]["match_photos"]["seconds"] + 0.01


def test_both_mappers_hand_the_stages_of_the_step_to_their_relay(tmp_path, monkeypatch):
    """The relay of the global mapper and the one of the incremental mapper get the Stages of the step - the same
    through every attempt from another focal length - and what they mark is kept although the alignment fails
    (patched, no reconstruction: the global mapper places nothing, the incremental one finds nothing)."""
    pycolmap = pytest.importorskip("pycolmap")
    from pyarchinit_photogrammetry.core.camera import Sensor
    from pyarchinit_photogrammetry.engine import colmap_log, sfm

    doc = ppg.Document()
    ch = doc.add_chunk("c")
    doc.save(tmp_path / "p.ppg")
    (sfm._sfm_dir(ch) / "database.db").touch()
    ch.meta["sfm"] = {"image_root": str(tmp_path), "names": {"0": "a.jpg", "1": "b.jpg", "2": "c.jpg"},
                      "tiepoint_limit": 0}
    handed = []

    @contextlib.contextmanager
    def relay(progress, folder, total=None, phases=None, stages=None):
        handed.append(stages)
        if stages is not None:
            stages.mark({"stage": "global_mapper", "register": "incremental_mapper"}[next(iter(phases))])
        yield

    monkeypatch.setattr(colmap_log, "relay", relay)
    monkeypatch.setattr(sfm, "_global_mapping", lambda *args, **kwargs: {})
    monkeypatch.setattr(pycolmap, "incremental_mapping", lambda *args, **kwargs: {})
    with pytest.raises(RuntimeError, match="no reconstruction"):
        sfm.align_cameras(ch, mapper="global", use_gps=False)
    assert len(handed) == 2 and isinstance(handed[0], Stages) and handed[1] is handed[0]
    assert list(ch.meta["timings"]["align_cameras"]["stages"]) == ["global_mapper", "incremental_mapper"]
    assert ch.meta["timings"]["align_cameras"]["failed"] == "RuntimeError"
    # photos that say no focal length: aligned again from other focal lengths, every attempt in the same stages
    handed.clear()
    ch.sensors[0] = Sensor(0, "camera", 1000, 750, params=[850.0, 850.0, 500.0, 375.0, 0, 0, 0, 0],
                           focal_source="guess")
    monkeypatch.setattr(sfm, "_set_db_focals", lambda *args: None)
    with pytest.raises(RuntimeError, match="no reconstruction"):
        sfm.align_cameras(ch, mapper="global", use_gps=False)
    assert len(handed) > 2 and isinstance(handed[0], Stages) and all(s is handed[0] for s in handed)
    assert list(ch.meta["timings"]["align_cameras"]["stages"]) == ["global_mapper", "incremental_mapper"]
    assert ch.meta["timings"]["align_cameras"]["failed"] == "RuntimeError"


def test_a_failed_alignment_says_so_by_the_type_of_its_error(tmp_path):
    """The record of an alignment that stops on an error names the type of the error, never its text: one refused
    before any work (no matching yet) and one cancelled once it has started. One that succeeds has no ``failed``
    (test_matching_and_alignment_leave_their_timings)."""
    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress
    from pyarchinit_photogrammetry.engine import sfm

    doc = ppg.Document()
    ch = doc.add_chunk("c")
    doc.save(tmp_path / "p.ppg")
    with pytest.raises(RuntimeError, match="run match_photos"):
        ch.align_cameras()
    record = ch.meta["timings"]["align_cameras"]
    assert set(record) == {"seconds", "stages", "failed"} and record["failed"] == "RuntimeError"
    assert record["stages"] == {} and "match_photos" not in json.dumps(record)
    (sfm._sfm_dir(ch) / "database.db").touch()
    ch.meta["sfm"] = {"image_root": str(tmp_path), "names": {"0": "a.jpg"}, "tiepoint_limit": 0}
    progress = Progress()
    progress.cancel()
    with pytest.raises(Cancelled):
        ch.align_cameras(progress=progress, mapper="incremental", use_gps=False)
    assert ch.meta["timings"]["align_cameras"]["failed"] == "Cancelled"


def test_a_failed_matching_keeps_the_seconds_measured_so_far_and_the_type_of_its_error(tmp_path, monkeypatch):
    """The record of a matching that stops on an error keeps the stages measured up to it and names the type of the
    error, never its text: one refused before any work, and one that fails in the ``pairs`` stage once the points of
    the photos are found (``features`` has its seconds). One that succeeds has no ``failed``
    (test_matching_and_alignment_leave_their_timings)."""
    import cv2
    import numpy as np

    pycolmap = pytest.importorskip("pycolmap")
    from pyarchinit_photogrammetry.engine import sfm

    doc = ppg.Document()
    ch = doc.add_chunk("c")
    doc.save(tmp_path / "p.ppg")
    with pytest.raises(ValueError, match="apply_masks"):
        ch.match_photos(apply_masks="nonsense")
    record = ch.meta["timings"]["match_photos"]
    assert set(record) == {"seconds", "stages", "failed"} and record["failed"] == "ValueError"
    assert record["stages"] == {}
    rng = np.random.default_rng(0)
    (tmp_path / "photos").mkdir()
    for i in range(3):
        cv2.imwrite(str(tmp_path / "photos" / f"NOISE_{i}.jpg"), rng.integers(0, 256, (240, 320, 3), dtype=np.uint8))
    ch.add_photos(sorted((tmp_path / "photos").glob("*.jpg")))

    def no_pairs(*args, **kwargs):
        raise RuntimeError("the pairs stage failed in " + str(tmp_path))

    monkeypatch.setattr(sfm._native, "gpu_devices", lambda setting=None: [])  # the CPU alone: COLMAP matches
    monkeypatch.setattr(pycolmap, "match_exhaustive", no_pairs)
    with pytest.raises(RuntimeError, match="pairs stage"):
        ch.match_photos(engine="classic")  # COLMAP's pairs stage
    record = ch.meta["timings"]["match_photos"]
    assert record["failed"] == "RuntimeError" and list(record["stages"]) == ["features", "pairs"]
    assert record["stages"]["features"] > 0 and record["seconds"] >= record["stages"]["features"]
    assert str(tmp_path) not in json.dumps(record) and "NOISE" not in json.dumps(record)
