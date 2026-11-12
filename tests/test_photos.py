"""Photos that cannot be read (kept online only by Dropbox) and photos linked to another folder."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import rasterio


def _copy(processed, tmp_path, name="copia"):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / name
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")  # the photos are stored relative to the project
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def test_orthomosaic_and_texture_without_unreadable_photos(processed, tmp_path):
    """Photos kept online only by Dropbox (empty files) stopped the orthomosaic with a
    FileNotFoundError; now they are reported and, if asked, left out."""
    from pyarchinit_photogrammetry.engine.visibility import UnreadablePhotos

    doc, ch = _copy(processed, tmp_path)
    online_only = []
    for cam in ch.aligned_cameras[:2]:
        empty = tmp_path / "dropbox" / f"{cam.label}.JPG"
        empty.parent.mkdir(exist_ok=True)
        empty.write_bytes(b"")
        cam.path = str(empty)
        online_only.append(cam.label)
    assert [c.label for c in ch.unreadable_photos()] == online_only
    with pytest.raises(UnreadablePhotos, match="2 photos cannot be read"):
        ch.build_orthomosaic(resolution=0.005)
    path = ch.build_orthomosaic(resolution=0.005, skip_unreadable=True)
    assert ch.meta["orthomosaic"]["skipped_photos"] == online_only
    with rasterio.open(path) as src:
        alpha = src.read(4)
    assert (alpha > 0).mean() > 0.5
    with pytest.raises(UnreadablePhotos):
        ch.build_texture(texture_size=1024)
    assert ch.build_texture(texture_size=1024, skip_unreadable=True).exists()


def test_relink_photos_to_another_folder(processed, tmp_path):
    """The photos found again by name in another folder (a copy outside Dropbox), with the
    folder COLMAP reads them from; photos split between folders are reached through links."""
    from pyarchinit_photogrammetry.engine.sfm import photo_folder

    doc, ch = _copy(processed, tmp_path)
    old_root = ch.meta["sfm"]["image_root"]
    copy = tmp_path / "ssd" / "missione" / "foto"
    copy.mkdir(parents=True)
    for cam in ch.cameras:
        shutil.copy2(cam.path, copy / Path(cam.path).name)
        cam.path = str(tmp_path / "dropbox" / Path(cam.path).name)  # gone
    assert len(ch.unreadable_photos()) == len(ch.aligned_cameras)
    report = ch.relink_photos(tmp_path / "ssd")
    assert report["relinked"] == len(ch.cameras) and not report["not_found"] and report["unreadable"] == 0
    assert report["image_root"] == str(copy.resolve()) != old_root
    assert photo_folder(ch) == str(copy.resolve())
    doc.save()
    # half of the photos elsewhere: COLMAP gets a folder of links named as in its model
    other = tmp_path / "altro"
    other.mkdir()
    moved = ch.cameras[::2]
    for cam in moved:
        shutil.move(cam.path, other / Path(cam.path).name)
    assert len(ch.unreadable_photos(ch.cameras)) == len(moved)
    report = ch.relink_photos(other, only_unreadable=True)
    assert report["relinked"] == len(moved) and report["unreadable"] == 0
    folder = photo_folder(ch)
    assert folder.endswith("photo_links")
    names = ch.meta["sfm"]["names"]
    for cam in ch.cameras:
        linked = (tmp_path / folder / names[str(cam.key)]).resolve()
        assert linked == (other / Path(cam.path).name).resolve() or linked.parent == copy.resolve()
    assert ch.unreadable_photos() == []


def test_add_photos_skips_the_hidden_files_of_a_folder(tmp_path):
    """A folder copied from a Mac to an exFAT disk holds an AppleDouble file `._name.JPG` beside each photo:
    it has the extension of a photo and is not one, and adding the folder stopped with UnidentifiedImageError."""
    import numpy as np
    from PIL import Image

    import pyarchinit_photogrammetry as ppg

    folder = tmp_path / "photos"
    (folder / "sub").mkdir(parents=True)
    for name in ("a.jpg", "sub/b.jpg"):
        Image.fromarray(np.full((40, 60, 3), 120, np.uint8)).save(folder / name)
        hidden = (folder / name).with_name("._" + Path(name).name)
        hidden.write_bytes(b"\x00\x05\x16\x07" + b"\x00" * 60)
    doc = ppg.Document()
    ch = doc.add_chunk("c")
    assert [c.label for c in ch.add_photos([folder])] == ["a"]
    assert [c.label for c in ch.add_photos([folder], recursive=True)] == ["b"]
