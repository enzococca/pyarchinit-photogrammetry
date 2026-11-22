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


def _project(folders, folder):
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    folder.mkdir(parents=True, exist_ok=True)
    doc.save(folder / "project.ppg")
    ch.add_photos(folders)
    return doc, ch


def _verified(db_path) -> tuple[set[tuple[str, str]], set[str]]:
    """The pairs of image names with a two-view geometry in the database (COLMAP writes one for
    every pair it took, also when the verification fails) and the names of its images."""
    import pycolmap

    con = sqlite3.connect(sfm.read_only_uri(db_path), uri=True)  # a raw file: URI would stop at a "#" of the path
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
    _doc, ch = _project(_photos_with_blanks(scene_dir, tmp_path), tmp_path / "Saggio #3")  # the reader of the GPU streams too
    seen = []
    ch.match_photos(quality="high", generic=False, reference="sequential", neighbours=NEIGHBOURS, gpu="auto",
                    progress=lambda f, m: seen.append(m), engine="classic")  # COLMAP's pair lists
    names, asked = _asked(ch)
    info = ch.meta["sfm"]
    assert info["gpu"] and info["gpu_pairs"] > 0 and info["gpu_pairs"] + info["cpu_pairs"] == len(asked)
    assert any(m.startswith("Verifying") for m in seen)
    assert info["pairs_unmatched"] == 0
    verified, in_db = _verified(ch.data_dir / "sfm" / "database.db")
    assert asked <= verified
    assert in_db == set(names.values())
    assert ch.align_cameras(mapper="incremental", use_gps=False) == len(ch.cameras)


def test_a_project_in_a_folder_named_with_a_hash_matches_on_the_cpu(scene_dir, tmp_path):
    """The database is opened read-only by its URI: a ``#`` (or ``?``) in a folder of the project
    ends a raw ``file:`` URI early and another file is opened ("no such table: images"). The pair
    list route, the tie point limit and the key points of the tie point viewer all read it so."""
    from pyarchinit_photogrammetry.engine import tiepoints

    _doc, ch = _project([scene_dir / "photos"], tmp_path / "Saggio #3")
    ch.match_photos(quality="high", generic=False, reference="sequential", neighbours=NEIGHBOURS, gpu="off")
    _root, names = sfm.image_root_and_names(ch)
    asked = set(sfm._sequential_pairs(ch, names, NEIGHBOURS))
    db = ch.data_dir / "sfm" / "database.db"
    assert "#" in str(db)
    verified, _in_db = _verified(db)
    assert asked <= verified and ch.meta["sfm"]["pairs_unmatched"] == 0
    assert sfm.limit_tie_points(db, tmp_path / "limited.db", per_image=50)["pruned"] is True
    keypoints = tiepoints._Keypoints(ch)
    try:
        got = keypoints.get(next(iter(names.values())))
        assert got is not None and len(got[0]) > 0
    finally:
        keypoints.close()
    assert ch.align_cameras(mapper="incremental", use_gps=False) == len(ch.cameras)


def test_the_read_only_uri_has_no_authority_and_escapes_the_path(tmp_path):
    """SQLite's URI parser takes no authority but an empty one or ``localhost``: a Windows share
    (and a mapped drive, which ``resolve`` rewrites to its share) must not come out as
    ``file://NAS/...``; a ``#`` or ``?`` in the path would end it early and a ``%`` be decoded; a
    drive path keeps the form SQLite documents."""
    uri = sfm.sqlite_file_uri
    assert uri("/Users/enzo/Saggio #3/che cosa?/100% fatto/database.db", windows=False) == \
        "file:///Users/enzo/Saggio%20%233/che%20cosa%3F/100%25%20fatto/database.db"
    assert uri(r"C:\Users\enzo\Saggio #3\database.db", windows=True) == "file:///C:/Users/enzo/Saggio%20%233/database.db"
    assert uri(r"\\NAS\scavi\Saggio #3\database.db", windows=True) == "file:////NAS/scavi/Saggio%20%233/database.db"
    if os.name != "nt":  # the parser takes the share form: the path is missing, the authority not refused
        with pytest.raises(sqlite3.OperationalError, match="unable to open"):  # (Windows would look for the share)
            sqlite3.connect(uri(r"\\NAS\scavi\database.db", windows=True) + "?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError, match="authority"):  # what as_uri gives for a share
        sqlite3.connect("file://NAS/scavi/database.db?mode=ro", uri=True)
    # a database under folders with those characters, through read_only_uri (the path resolved, the flags)
    folder = tmp_path / "Saggio #3 100% fatto"
    if os.name != "nt":
        folder = folder / "che cosa?"  # not a file name Windows allows
    folder.mkdir(parents=True)
    db = folder / "database.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()
    ro = sfm.read_only_uri(db, "immutable=1")
    assert ro.endswith("/database.db?mode=ro&immutable=1") and "#" not in ro and "%23" in ro and "%25" in ro
    con = sqlite3.connect(ro, uri=True)
    try:
        assert con.execute("SELECT name FROM sqlite_master").fetchone() == ("t",)
        with pytest.raises(sqlite3.OperationalError):  # read-only holds
            con.execute("INSERT INTO t VALUES (1)")
    finally:
        con.close()


def test_the_aliases_of_a_pair_list_go_back_whatever_happens(tmp_path, monkeypatch, caplog):
    """Inside the block the photos COLMAP's reader could not find go by an alias it can read; the
    others keep their name; on the way out, also after an error, the names are back. The originals
    sit in a record beside the database meanwhile: a restore that fails does not hide the error
    that ended the block, and the next call puts the names back from the record."""
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
    record = db_path.parent / sfm.LIST_NAMES_FILE
    with sfm.colmap_list_names(db_path) as alias:
        assert set(alias) == set(names)
        assert alias["plain.jpg"] == "plain.jpg" and alias["ppg_list_7.jpg"] == "ppg_list_7.jpg"
        for name in ("camera A/foto 1.jpg", "#2.jpg", "tab\there.jpg"):
            assert alias[name] != name and not alias[name].startswith("#")
            assert alias[name] and not any(c.isspace() for c in alias[name])
        assert len(set(alias.values())) == len(names)
        assert sfm._db_image_names(db_path) == set(alias.values())  # what COLMAP reads meanwhile
        assert record.exists()
    assert sfm._db_image_names(db_path) == set(names) and not record.exists()
    assert not [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]  # a completed block says nothing
    with pytest.raises(RuntimeError, match="stopped"):
        with sfm.colmap_list_names(db_path):
            assert sfm._db_image_names(db_path) != set(names)
            raise RuntimeError("COLMAP stopped")
    assert sfm._db_image_names(db_path) == set(names) and not record.exists()
    # the restore itself fails (the database locked): the error of the block is the one raised, the record stays
    real = sfm._rename_images
    calls = []

    def locked(path, by_id):
        calls.append(by_id)
        if len(calls) == 2:
            raise sqlite3.OperationalError("database is locked")
        return real(path, by_id)

    monkeypatch.setattr(sfm, "_rename_images", locked)
    with caplog.at_level(logging.ERROR, logger="pyarchinit_photogrammetry"):
        with pytest.raises(RuntimeError, match="stopped"):
            with sfm.colmap_list_names(db_path):
                raise RuntimeError("COLMAP stopped")
    assert any("put back" in r.getMessage() for r in caplog.records)
    assert sfm._db_image_names(db_path) != set(names) and record.exists()
    monkeypatch.undo()
    assert sfm.restore_list_names(db_path) == 3  # the next call: the names back from the record
    assert sfm._db_image_names(db_path) == set(names) and not record.exists()
    # a record left behind by a database rebuilt since (other photos under those ids) renames nothing
    record.write_text('{"3": ["ppg_list_3", "elsewhere.jpg"]}')
    assert sfm.restore_list_names(db_path) == 0 and not record.exists()
    assert sfm._db_image_names(db_path) == set(names)


def test_an_unreadable_record_of_the_names_is_dropped(tmp_path, caplog):
    """The record is written whole (a temporary file put in its place); one left empty or cut by a
    machine that died while it was written is dropped with a warning, instead of stopping every
    later matching and alignment of the chunk at their first line."""
    import pycolmap

    db_path = tmp_path / "database.db"
    names = ["camera A/foto 1.jpg", "plain.jpg"]
    db = pycolmap.Database.open(str(db_path))
    try:
        cid = db.write_camera(pycolmap.Camera(model="SIMPLE_RADIAL", width=100, height=80, params=[90.0, 50.0, 40.0, 0.0]))
        for name in names:
            db.write_image(pycolmap.Image(name=name, camera_id=cid))
    finally:
        db.close()
    record = db_path.parent / sfm.LIST_NAMES_FILE
    sfm._alias_list_names(db_path)
    assert record.exists() and not record.with_name(record.name + ".tmp").exists()
    assert sfm.restore_list_names(db_path) == 1
    for broken in ("", '{"1": ["ppg_list_1", "camera A/f', "[1, 2]", '{"1": ["ppg_list_1"]}'):
        record.write_text(broken, encoding="utf-8")
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
            assert sfm.restore_list_names(db_path) == 0
        assert not record.exists() and sfm._db_image_names(db_path) == set(names)
        assert any(sfm.LIST_NAMES_FILE in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    with sfm.colmap_list_names(db_path) as alias:  # the block works as before
        assert alias["camera A/foto 1.jpg"] != "camera A/foto 1.jpg"
    assert sfm._db_image_names(db_path) == set(names)


def test_the_names_aliased_by_an_interrupted_matching_come_back(scene_dir, tmp_path, caplog):
    """A process that dies inside the block (COLMAP aborts the process on a failed commit) leaves
    the aliases in the database and the record beside it: the next alignment puts the names back
    first and aligns every photo, the next matching continues from them."""
    _doc, ch = _project(_photos_with_blanks(scene_dir, tmp_path), tmp_path)
    ch.match_photos(quality="high", generic=False, reference="sequential", neighbours=NEIGHBOURS, gpu="off")
    names, _pairs = _asked(ch)
    db = ch.data_dir / "sfm" / "database.db"
    alias = sfm._alias_list_names(db)  # as an interrupted call leaves them
    assert (db.parent / sfm.LIST_NAMES_FILE).exists() and sfm._db_image_names(db) == set(alias.values())
    assert set(alias.values()) != set(names.values())
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        assert ch.align_cameras(mapper="incremental", use_gps=False) == len(ch.cameras)
    assert any("interrupted" in r.getMessage() for r in caplog.records)
    assert sfm._db_image_names(db) == set(names.values()) and not (db.parent / sfm.LIST_NAMES_FILE).exists()
    sfm._alias_list_names(db)
    seen = []
    ch.match_photos(quality="high", generic=False, reference="sequential", neighbours=NEIGHBOURS, gpu="off",
                    reset=False, progress=lambda f, m: seen.append(m))
    assert ch.meta["sfm"]["continued"] and not any("every photo again" in m for m in seen)
    assert sfm._db_image_names(db) == set(names.values())


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
        ch.match_photos(quality="high", generic=False, reference="sequential", neighbours=3, gpu="off",
                        engine="classic")  # COLMAP's pair list
    asked = ch.meta["sfm"]["pairs"]
    lost = asked - asked // 2
    assert lost > 0 and ch.meta["sfm"]["pairs_unmatched"] == lost
    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and "pairs" in r.getMessage()]
    assert said and f"{lost:,}" in said[-1] and f"{asked:,}" in said[-1]


def test_a_model_written_from_the_cameras_keeps_names_with_blanks(processed, tmp_path):
    """COLMAP's text reader stops an image name at the first blank: a model written from the
    cameras of the chunk (an import from Metashape, merged chunks) comes back with the names of
    the photos all the same."""
    doc0, _, _ = processed
    src = Path(doc0.path)
    shutil.copytree(src.parent, tmp_path / "copia", ignore=shutil.ignore_patterns("dense"))
    ch = ppg.Document.open(tmp_path / "copia" / src.name).chunks[0]
    for i, cam in enumerate(ch.cameras):
        cam.path = str(tmp_path / "rilievo 2024" / ("camera A" if i % 2 else "camera B") / f"foto {i:04d}.jpg")
    sfm.write_reconstruction_from_chunk(ch)
    _root, names = sfm.image_root_and_names(ch)
    rec = sfm.load_reconstruction(ch)
    assert {im.name for im in rec.images.values()} == set(names.values()) and len(rec.images) == len(ch.cameras)
    assert ch.meta["sfm"]["names"] == {str(k): v for k, v in names.items()}
