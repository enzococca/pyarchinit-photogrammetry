"""Photos whose folders or file names hold blanks. COLMAP reads a list of pairs as lines of
``name1 name2`` split at the blanks, with no quoting: a pair whose names hold one is logged as
"Image ... does not exist" and dropped, with no error (a survey of 2026-11-19 with its photos under
"2024 camera/...": 7,901 of 10,553 pairs lost, 34 of 1,356 photos aligned). Every pair asked for
must be matched, on the CPU alone and shared with the GPU, and the photos keep their names; the
pairs COLMAP leaves out, if any, are counted and said; a model written from the cameras keeps such
names too (COLMAP's text reader stops an image name at the first blank)."""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine import sfm

NEIGHBOURS = 5  # the photos before and after each one in the sequence: the next row of the grid too


def _photos_with_blanks(scene_dir, tmp_path) -> list[Path]:
    """The photos of the trench under two folders and file names with blanks, in the order of the
    capture (hard links where the disk allows them: the paths must stay as given, a symbolic link
    would resolve to the original)."""
    root = tmp_path / "rilievo 2024"
    photos = sorted((scene_dir / "photos").glob("*.jpg"))
    for i, src in enumerate(photos):
        dst = root / ("camera A" if i < len(photos) // 2 else "camera B") / f"foto {i:04d}.jpg"
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy(src, dst)
    return [root / "camera A", root / "camera B"]


def _project(folders, tmp_path):
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(tmp_path / "project.ppg")
    ch.add_photos(folders)
    return doc, ch


def _verified(db_path) -> tuple[set[tuple[str, str]], set[str]]:
    """The pairs of image names with a two-view geometry in the database (COLMAP writes one for
    every pair it took, also when the verification fails) and the names of its images."""
    import pycolmap

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = dict(con.execute("SELECT image_id, name FROM images"))
        pairs = set()
        for (pid,) in con.execute("SELECT pair_id FROM two_view_geometries"):
            a, b = pycolmap.pair_id_to_image_pair(int(pid))
            pairs.add(tuple(sorted((names[a], names[b]))))
        return pairs, set(names.values())
    finally:
        con.close()


def _asked(ch) -> tuple[dict[int, str], set[tuple[str, str]]]:
    _root, names = sfm.image_root_and_names(ch)
    assert all(" " in n for n in names.values())  # "camera A/foto 0003.jpg"
    return names, set(sfm._sequential_pairs(ch, names, NEIGHBOURS))


def test_every_pair_of_photos_named_with_blanks_is_matched_on_the_cpu(scene_dir, tmp_path):
    _doc, ch = _project(_photos_with_blanks(scene_dir, tmp_path), tmp_path)
    ch.match_photos(quality="high", generic=False, reference="sequential", neighbours=NEIGHBOURS, gpu="off")
    names, asked = _asked(ch)
    info = ch.meta["sfm"]
    assert info["pairs"] == len(asked) and info["cpu_pairs"] == len(asked) and info["pairs_unmatched"] == 0
    verified, in_db = _verified(ch.data_dir / "sfm" / "database.db")
    assert asked <= verified
    assert in_db == set(names.values())  # the names of the photos, no alias left behind
    assert ch.align_cameras(mapper="incremental", use_gps=False) == len(ch.cameras)


def test_every_pair_of_photos_named_with_blanks_is_matched_with_the_gpu(scene_dir, tmp_path):
    """The pairs the CPU takes of the shared queue and the verification of those the GPU matched
    both go through a pair list."""
    from pyarchinit_photogrammetry import _native

    if _native.gpu_device("auto") is None:
        pytest.skip("no GPU (wgpu adapter) on this computer")
    _doc, ch = _project(_photos_with_blanks(scene_dir, tmp_path), tmp_path)
    seen = []
    ch.match_photos(quality="high", generic=False, reference="sequential", neighbours=NEIGHBOURS, gpu="auto",
                    progress=lambda f, m: seen.append(m))
    names, asked = _asked(ch)
    info = ch.meta["sfm"]
    assert info["gpu"] and info["gpu_pairs"] > 0 and info["gpu_pairs"] + info["cpu_pairs"] == len(asked)
    assert any(m.startswith("Verifying") for m in seen)
    assert info["pairs_unmatched"] == 0
    verified, in_db = _verified(ch.data_dir / "sfm" / "database.db")
    assert asked <= verified
    assert in_db == set(names.values())
    assert ch.align_cameras(mapper="incremental", use_gps=False) == len(ch.cameras)


def test_the_aliases_of_a_pair_list_go_back_whatever_happens(tmp_path):
    """Inside the block the photos COLMAP's reader could not find go by an alias it can read; the
    others keep their name; on the way out, also after an error, the names are back."""
    import pycolmap

    db_path = tmp_path / "database.db"
    names = ["camera A/foto 1.jpg", "#2.jpg", "plain.jpg", "ppg_list_7.jpg", "tab\there.jpg"]
    db = pycolmap.Database.open(str(db_path))
    try:
        cid = db.write_camera(pycolmap.Camera(model="SIMPLE_RADIAL", width=100, height=80, params=[90.0, 50.0, 40.0, 0.0]))
        for name in names:
            db.write_image(pycolmap.Image(name=name, camera_id=cid))
    finally:
        db.close()
    with sfm.colmap_list_names(db_path) as alias:
        assert set(alias) == set(names)
        assert alias["plain.jpg"] == "plain.jpg" and alias["ppg_list_7.jpg"] == "ppg_list_7.jpg"
        for name in ("camera A/foto 1.jpg", "#2.jpg", "tab\there.jpg"):
            assert alias[name] != name and not alias[name].startswith("#")
            assert alias[name] and not any(c.isspace() for c in alias[name])
        assert len(set(alias.values())) == len(names)
        assert sfm._db_image_names(db_path) == set(alias.values())  # what COLMAP reads meanwhile
    assert sfm._db_image_names(db_path) == set(names)
    with pytest.raises(RuntimeError, match="stopped"):
        with sfm.colmap_list_names(db_path):
            assert sfm._db_image_names(db_path) != set(names)
            raise RuntimeError("COLMAP stopped")
    assert sfm._db_image_names(db_path) == set(names)


def test_the_pairs_colmap_leaves_out_are_counted_and_said(scene_dir, tmp_path, monkeypatch, caplog):
    """A pair list COLMAP cannot take in full (here half its lines taken away before the matcher
    reads it): the pairs matched are counted against those asked for, the loss is in the log and
    in the record of the step."""
    import pycolmap

    original = pycolmap.match_image_pairs

    def losing(database_path, *, pairing_options, **kwargs):
        lst = Path(pairing_options.match_list_path)
        lines = lst.read_text().splitlines()
        lst.write_text("\n".join(lines[:len(lines) // 2]))
        return original(database_path, pairing_options=pairing_options, **kwargs)

    monkeypatch.setattr(pycolmap, "match_image_pairs", losing)
    _doc, ch = _project([scene_dir / "photos"], tmp_path)
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        ch.match_photos(quality="high", generic=False, reference="sequential", neighbours=3, gpu="off")
    asked = ch.meta["sfm"]["pairs"]
    lost = asked - asked // 2
    assert lost > 0 and ch.meta["sfm"]["pairs_unmatched"] == lost
    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and "pairs" in r.getMessage()]
    assert said and f"{lost:,}" in said[-1] and f"{asked:,}" in said[-1]
