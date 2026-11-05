"""A folder put in the place of another (the files of an elevation built again): crash-safe at every step - a failure
leaves either the old folder or the new one in place, never neither, and the next swap mends what a crash left."""
from __future__ import annotations

import os

import pytest

from pyarchinit_photogrammetry.io import files


def _folders(tmp_path):
    place, new = tmp_path / "el01", tmp_path / "nuovo"
    place.mkdir()
    (place / "elevation.tif").write_text("old")
    new.mkdir()
    (new / "elevation.tif").write_text("new")
    return place, new


def _content(folder):
    return (folder / "elevation.tif").read_text()


def test_swap_puts_the_new_folder_in_place(tmp_path):
    place, new = _folders(tmp_path)
    files.swap_folder(new, place)
    assert _content(place) == "new"
    assert not new.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["el01"]  # no backup left


def test_swap_into_an_empty_place(tmp_path):
    place, new = _folders(tmp_path)
    import shutil

    shutil.rmtree(place)
    files.swap_folder(new, place)
    assert _content(place) == "new" and not new.exists()


def _failing_rename(monkeypatch, at: int):
    calls = []
    real = os.replace

    def rename(src, dst):
        calls.append((src, dst))
        if len(calls) == at:
            raise OSError("disco pieno")
        return real(src, dst)

    monkeypatch.setattr(files, "_rename_folder", rename)
    return calls


def test_failure_moving_the_old_folder_aside_changes_nothing(tmp_path, monkeypatch):
    place, new = _folders(tmp_path)
    _failing_rename(monkeypatch, 1)
    with pytest.raises(OSError, match="disco pieno"):
        files.swap_folder(new, place)
    assert _content(place) == "old" and _content(new) == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["el01", "nuovo"]


def test_failure_moving_the_new_folder_puts_the_old_one_back(tmp_path, monkeypatch):
    place, new = _folders(tmp_path)
    _failing_rename(monkeypatch, 2)
    with pytest.raises(OSError, match="disco pieno"):
        files.swap_folder(new, place)
    assert _content(place) == "old" and _content(new) == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["el01", "nuovo"]


def test_failure_deleting_the_backup_keeps_the_new_folder(tmp_path, monkeypatch):
    place, new = _folders(tmp_path)

    def broken(path, *a, **k):
        raise OSError("file in uso")

    monkeypatch.setattr(files, "remove_tree", broken)
    files.swap_folder(new, place)  # the swap is made: a backup left behind is no failure
    assert _content(place) == "new" and not new.exists()
    backup = files.backup_name(place)
    assert backup.exists() and _content(backup) == "old"
    monkeypatch.undo()
    other = tmp_path / "ancora"
    other.mkdir()
    (other / "elevation.tif").write_text("again")
    files.swap_folder(other, place)  # the backup of the swap before goes first
    assert _content(place) == "again"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["el01"]


def test_a_crash_after_moving_the_old_folder_aside_is_mended(tmp_path):
    place, new = _folders(tmp_path)
    os.replace(place, files.backup_name(place))  # a crash between the first and the second rename
    files.mend_swap(place)
    assert _content(place) == "old" and not files.backup_name(place).exists()
    files.swap_folder(new, place)
    assert _content(place) == "new"


def test_a_crash_before_deleting_the_backup_is_mended(tmp_path):
    place, new = _folders(tmp_path)
    os.replace(place, files.backup_name(place))
    os.replace(new, place)  # a crash between the second rename and the deletion
    files.mend_swap(place)
    assert _content(place) == "new" and not files.backup_name(place).exists()


def test_the_first_error_is_said_when_putting_back_fails_too(tmp_path, monkeypatch, caplog):
    place, new = _folders(tmp_path)
    calls = []

    def rename(src, dst):
        calls.append(1)
        if len(calls) == 1:
            return os.replace(src, dst)
        raise OSError("disco pieno" if len(calls) == 2 else "ancora pieno")

    monkeypatch.setattr(files, "_rename_folder", rename)
    with pytest.raises(OSError, match="disco pieno"):
        files.swap_folder(new, place)
    assert "ancora pieno" in caplog.text  # the failure of the way back logged
    monkeypatch.undo()
    files.mend_swap(place)  # the backup becomes the folder again
    assert _content(place) == "old"
