"""Photo paths stored relative to the project file (also outside its folder), the photos missing when a project is
opened and the path of chosen photos changed (issue #30)."""
from __future__ import annotations

import json
import ntpath
import shutil
from pathlib import Path

import numpy as np
import pytest


def _photo(path: Path, value: int, size=(48, 64)) -> Path:
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    img = np.full((*size, 3), value, np.uint8)
    img[10:20, 10:30] = 255 - value
    cv2.imwrite(str(path), img)
    return path


def _project(root: Path, n: int = 3):
    """``root/progetto/scavo.ppg`` with its photos in the sibling folder ``root/foto``."""
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(root / "progetto" / "scavo.ppg")
    ch.add_photos([_photo(root / "foto" / f"IMG_{i}.jpg", 40 + 30 * i) for i in range(n)])
    doc.save()
    return doc, ch


def _stored(ppg_file: Path, chunk: int = 0) -> list[str]:
    return [c["path"] for c in json.loads(Path(ppg_file).read_text())["chunks"][chunk]["cameras"]]


# ------------------------------------------------------------------ item 1: relative outside the project folder
def test_photos_in_a_sibling_folder_are_stored_relative_and_move_with_the_project(tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc, _ch = _project(tmp_path / "lavoro")
    stored = _stored(doc.path)
    assert stored == [f"./../foto/IMG_{i}.jpg" for i in range(3)]  # POSIX separators, read by older versions too
    shutil.move(str(tmp_path / "lavoro"), str(tmp_path / "altrove"))  # project and photos moved together
    again = ppg.Document.open(tmp_path / "altrove" / "progetto" / "scavo.ppg")
    cams = again.chunk.cameras
    assert all(Path(c.path).is_file() for c in cams)
    assert Path(cams[0].path) == (tmp_path / "altrove" / "foto" / "IMG_0.jpg").resolve()
    assert again.chunk.missing_photos() == []


def test_a_photo_on_another_volume_stays_absolute(tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.core import paths

    monkeypatch.setattr(paths, "same_volume", lambda path, base: False)
    doc, ch = _project(tmp_path)
    assert _stored(doc.path) == [str(Path(c.path)) for c in ch.cameras]
    assert all(Path(p).is_absolute() for p in _stored(doc.path))


def test_an_old_project_with_absolute_paths_opens_and_is_saved_relative(tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc, ch = _project(tmp_path)
    data = json.loads(doc.path.read_text())
    for c, cam in zip(data["chunks"][0]["cameras"], ch.cameras):  # as 1.10 wrote a photo outside the folder
        c["path"] = str(Path(cam.path).resolve())
    doc.path.write_text(json.dumps(data))
    old = ppg.Document.open(doc.path)
    assert [Path(c.path) for c in old.chunk.cameras] == [Path(c.path).resolve() for c in ch.cameras]
    old.save()
    assert _stored(doc.path) == [f"./../foto/IMG_{i}.jpg" for i in range(3)]


def test_relative_paths_without_the_dot_prefix_are_read_too(tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc, ch = _project(tmp_path)
    data = json.loads(doc.path.read_text())
    for c in data["chunks"][0]["cameras"]:
        c["path"] = c["path"].removeprefix("./")  # "../foto/IMG_0.jpg", as Metashape writes them
    doc.path.write_text(json.dumps(data))
    again = ppg.Document.open(doc.path)
    assert [Path(c.path) for c in again.chunk.cameras] == [Path(c.path) for c in ch.cameras]


def test_save_as_into_another_folder_keeps_every_photo(tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc, ch = _project(tmp_path)
    doc.save(tmp_path / "copie" / "2026" / "scavo_bis.ppg")
    assert _stored(doc.path) == [f"./../../foto/IMG_{i}.jpg" for i in range(3)]
    again = ppg.Document.open(tmp_path / "copie" / "2026" / "scavo_bis.ppg")
    assert all(Path(c.path).is_file() for c in again.chunk.cameras)


def test_a_relative_path_that_does_not_resolve_opens_and_keeps_its_form(tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc, _ch = _project(tmp_path)
    shutil.rmtree(tmp_path / "foto")  # the photos not there (a disk not plugged in, a folder not copied yet)
    again = ppg.Document.open(doc.path)
    assert len(again.chunk.missing_photos()) == 3
    again.save()
    assert _stored(doc.path) == [f"./../foto/IMG_{i}.jpg" for i in range(3)]  # not made absolute
    again.save(tmp_path / "altro" / "scavo.ppg")  # rebased with the project
    assert _stored(tmp_path / "altro" / "scavo.ppg") == [f"./../foto/IMG_{i}.jpg" for i in range(3)]
    assert Path(ppg.Document.open(tmp_path / "altro" / "scavo.ppg").chunk.cameras[0].path) == \
        (tmp_path / "foto" / "IMG_0.jpg").resolve()


def test_a_missing_photo_stored_absolute_stays_absolute(tmp_path):
    """A photo on a disk not plugged in (its folder gone): the volume cannot be told, the path stays as it was."""
    import pyarchinit_photogrammetry as ppg

    doc, ch = _project(tmp_path)
    gone = tmp_path / "disco_esterno" / "foto" / "IMG_0.jpg"
    ch.cameras[0].path = str(gone)
    doc.save()
    assert _stored(doc.path)[0] == str(gone)
    ppg.Document.open(doc.path).save()
    assert _stored(doc.path)[0] == str(gone)


def test_windows_volume_rules():
    from pyarchinit_photogrammetry.core.paths import relative_text, windows_same_volume

    assert windows_same_volume(r"C:\scavi\foto\IMG_1.JPG", r"C:\scavi\progetto")
    assert windows_same_volume(r"c:\scavi\foto\IMG_1.JPG", r"C:\Scavi")  # case does not count
    assert not windows_same_volume(r"D:\foto\IMG_1.JPG", r"C:\scavi\progetto")
    assert windows_same_volume(r"\\server\scavi\foto\IMG_1.JPG", r"\\SERVER\scavi\progetto")
    assert not windows_same_volume(r"\\server\altro\foto\IMG_1.JPG", r"\\server\scavi\progetto")
    assert not windows_same_volume(r"\\server\scavi\foto\IMG_1.JPG", r"C:\scavi")
    assert not windows_same_volume("foto/IMG_1.JPG", r"C:\scavi")  # not absolute: never relative
    assert relative_text(r"C:\scavi\foto\IMG_1.JPG", r"C:\scavi\progetto", ntpath) == "./../foto/IMG_1.JPG"
    assert relative_text(r"C:\scavi\progetto\foto\IMG_1.JPG", r"C:\scavi\progetto", ntpath) == "./foto/IMG_1.JPG"
    assert relative_text(r"D:\foto\IMG_1.JPG", r"C:\scavi\progetto", ntpath) is None


def test_the_raw_file_and_the_photo_folder_of_the_model_are_relative_too(tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc, ch = _project(tmp_path)
    ch.meta["sfm"] = {"image_root": str((tmp_path / "foto").resolve()), "names": {}}
    ch.cameras[0].meta["raw"] = {"path": str((tmp_path / "raw" / "IMG_0.dng").resolve()), "size": 1}
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "IMG_0.dng").write_bytes(b"x")
    doc.save()
    data = json.loads(doc.path.read_text())["chunks"][0]
    assert data["meta"]["sfm"]["image_root"] == "./../foto"
    assert data["cameras"][0]["meta"]["raw"]["path"] == "./../raw/IMG_0.dng"
    again = ppg.Document.open(doc.path).chunk
    assert Path(again.meta["sfm"]["image_root"]) == (tmp_path / "foto").resolve()
    assert Path(again.cameras[0].meta["raw"]["path"]) == (tmp_path / "raw" / "IMG_0.dng").resolve()


# ------------------------------------------------------------------ item 2: missing photos counted
def test_missing_photos_are_the_enabled_ones_missing_or_empty(tmp_path):
    doc, ch = _project(tmp_path, n=4)
    Path(ch.cameras[0].path).unlink()
    Path(ch.cameras[1].path).write_bytes(b"")  # kept online only by Dropbox
    ch.cameras[2].enabled = False
    Path(ch.cameras[2].path).unlink()  # disabled: not counted
    assert [c.label for c in ch.missing_photos()] == ["IMG_0", "IMG_1"]


def test_info_says_the_photos_missing(tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, ch = _project(tmp_path)
    Path(ch.cameras[1].path).unlink()
    r = CliRunner().invoke(app, ["info", str(doc.path)])
    assert r.exit_code == 0, r.output
    assert "1 photo missing" in r.output


# ------------------------------------------------------------------ item 3: relink of chosen photos
def test_relink_only_the_cameras_chosen(tmp_path):
    doc, ch = _project(tmp_path)
    copy = tmp_path / "ssd" / "foto"
    copy.mkdir(parents=True)
    for cam in ch.cameras:
        shutil.copy2(cam.path, copy / Path(cam.path).name)
    before = [c.path for c in ch.cameras]
    r = ch.relink_photos(tmp_path / "ssd", cameras=[ch.cameras[1]])
    assert r["relinked"] == 1 and not r["not_found"]
    assert ch.cameras[1].path == str((copy / "IMG_1.jpg").resolve())
    assert [ch.cameras[0].path, ch.cameras[2].path] == [before[0], before[2]]


def test_cli_relink_only(tmp_path):
    import pyarchinit_photogrammetry as ppg
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, ch = _project(tmp_path)
    copy = tmp_path / "ssd"
    copy.mkdir()
    for cam in ch.cameras:
        shutil.copy2(cam.path, copy / Path(cam.path).name)
    r = CliRunner().invoke(app, ["relink", str(doc.path), str(copy), "--only", "IMG_0", "--only", "IMG_2"])
    assert r.exit_code == 0, r.output
    again = ppg.Document.open(doc.path).chunk
    assert [Path(c.path).parent.name for c in again.cameras] == ["ssd", "foto", "ssd"]
    r = CliRunner().invoke(app, ["relink", str(doc.path), str(copy), "--only", "NESSUNA"])
    assert r.exit_code != 0 and "NESSUNA" in r.output


def test_set_photo_path_of_one_camera(tmp_path):
    doc, ch = _project(tmp_path)
    other = _photo(tmp_path / "rinominata" / "scatto_bis.jpg", 90)
    cam = ch.cameras[0]
    ch.set_photo_path(cam, other)
    assert cam.path == str(other.resolve())
    with pytest.raises(FileNotFoundError):
        ch.set_photo_path(cam, tmp_path / "nessuna.jpg")
    with pytest.raises(ValueError, match="32 x 16"):
        ch.set_photo_path(cam, _photo(tmp_path / "piccola.jpg", 90, size=(16, 32)))
    assert cam.path == str(other.resolve())
