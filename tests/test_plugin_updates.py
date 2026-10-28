"""Updates of the plugins installed by the program: a newer version that this program can run is downloaded and
checked at once, kept in ``<plugins>/.updates/<name>/`` and put in place at the next start, before any plugin is
imported - a running session never mixes the modules of two versions of a plugin."""
from __future__ import annotations

import dataclasses
import logging
import os
import shutil
import time
from pathlib import Path

import pytest

from pyarchinit_photogrammetry.plugins import Manager, PluginInfo, updates
from pyarchinit_photogrammetry.plugins import index as plugin_index
from test_plugins import _entry, _index, _install_folder, _wheel


def _newer(tmp_path: Path, name: str, version: str, **extra) -> plugin_index.Entry:
    """The entry of the index for ``version`` of the plugin ``name`` (its wheel is built in ``tmp_path``)."""
    wheel = _wheel(tmp_path / f"{name}-{version}-py3-none-any.whl", name, version=version)
    return plugin_index.Entry.from_dict(_entry(wheel, name, version=version, **extra))


def _installed(folder: Path) -> list[PluginInfo]:
    """The plugins of the folder as a fresh manager finds them (nothing loaded)."""
    return Manager(folder).discover()


def _versions(folder: Path) -> dict[str, str]:
    return {i.name: i.version for i in _installed(folder)}


def _never(*args, **kwargs):
    raise AssertionError("a download was not expected")


def _refuse_renames_onto(monkeypatch, target: Path, *, error=OSError, times: int | None = None) -> list:
    """From now on ``Path.rename`` raises ``error`` for a rename onto ``target`` (``times`` of them at most;
    the others go through). The list it returns holds what was refused."""
    real, refused = Path.rename, []

    def rename(self, dst):
        if Path(dst) == target and (times is None or len(refused) < times):
            refused.append(self)
            raise error(f"refused: {dst}")
        return real(self, dst)

    monkeypatch.setattr(Path, "rename", rename)
    return refused


def _work_folder(tmp_path: Path, folder: Path, name: str, *, marker: bool) -> Path:
    """A work folder as a swap leaves it after it moved the plugin ``name`` aside into "previous": with the file
    that says where the plugin belongs (``marker``), as a swap that stopped, or without it, as one that finished."""
    _install_folder(tmp_path / "aside", name)
    work = folder / f".install-{name}-x1y2z3w4"
    work.mkdir(parents=True)
    (tmp_path / "aside" / name).rename(work / "previous")
    if marker:
        (work / plugin_index.SWAPPING).write_text(name)
    return work


@pytest.fixture
def folder(tmp_path) -> Path:
    """A plugin folder of its own with the plugin ``a`` 0.1.0 installed."""
    path = tmp_path / "plugins"
    _install_folder(path, "a")
    return path


def test_newer_compatible_versions_are_staged_and_applied_at_the_next_start(tmp_path, folder, caplog):
    entry = _newer(tmp_path, "a", "0.2.0")
    result = updates.check([entry], _installed(folder), None, folder)
    assert result.updated == [("a", "0.1.0", "0.2.0")]
    assert not (result.available or result.failed or result.waiting or result.new)
    assert _versions(folder) == {"a": "0.1.0"}  # the plugin in use is not touched
    assert updates.staged(folder) == {"a": "0.2.0"} and not list(folder.glob(".install-*"))
    # the same version is not downloaded again; it is told as pending, not as news
    again = updates.check([entry], _installed(folder), None, folder, install=_never)
    assert not again and again.pending == [("a", "0.1.0", "0.2.0")]
    # the next start puts it in place, before any plugin is imported
    m = Manager(folder)
    assert m.applied == []
    with caplog.at_level(logging.DEBUG, logger=updates.__name__):
        m.load()
    assert m.applied == [("a", "0.1.0", "0.2.0")]
    assert "plugin a updated from 0.1.0 to 0.2.0" in caplog.text
    assert m.get("a").active and m.get("a").version == "0.2.0"
    assert _versions(folder) == {"a": "0.2.0"} and not list((folder / ".updates").iterdir())
    # a folder is applied once per process: what is staged afterwards waits for the next start
    updates.check([_newer(tmp_path, "a", "0.3.0")], _installed(folder), None, folder)
    later = Manager(folder)
    later.load()
    assert later.applied == [] and later.get("a").version == "0.2.0"
    assert updates.staged(folder) == {"a": "0.3.0"}


def test_an_applied_update_is_not_logged_above_the_debug_level(tmp_path, folder, caplog):
    updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)
    with caplog.at_level(logging.INFO, logger=updates.__name__):
        assert updates.apply_staged(folder) == [("a", "0.1.0", "0.2.0")]
    # the window and `ppg plugin` tell it in their own words, from Manager.applied: a second row says the same
    assert "updated from" not in caplog.text
    updates.check([_newer(tmp_path, "a", "0.3.0")], _installed(folder), None, folder)
    with caplog.at_level(logging.DEBUG, logger=updates.__name__):
        assert updates.apply_staged(folder) == [("a", "0.2.0", "0.3.0")]
    assert "plugin a updated from 0.2.0 to 0.3.0" in caplog.text  # kept for whoever looks


def test_an_update_that_needs_a_newer_program_waits(tmp_path, folder):
    entry = _newer(tmp_path, "a", "0.3.0", min_app="99.0")
    result = updates.check([entry], _installed(folder), None, folder, install=_never)
    assert result.waiting == [("a", "0.3.0", entry.problem())] and "99.0" in entry.problem()
    assert not (result.updated or result.available or result.failed)
    assert result and updates.staged(folder) == {}
    # a program of that version would take it
    taken = []
    ok = updates.check([entry], _installed(folder), None, folder, app_version="99.1",
                       install=lambda e, *args, **kwargs: taken.append(e.name))
    assert ok.updated == [("a", "0.1.0", "0.3.0")] and not ok.waiting and taken == ["a"]


def test_pip_plugins_are_never_touched(tmp_path):
    folder = tmp_path / "plugins"
    pip = PluginInfo(name="a", title="a", version="0.1.0", description="", source="environment",
                     distribution="a", location=None)
    entries = [_newer(tmp_path, "a", "0.2.0"), _newer(tmp_path, "a", "0.3.0", min_app="99.0")]
    for entry in entries:
        for update in (True, False):
            result = updates.check([entry], [pip], ["a"], folder, update=update, install=_never)
            assert not result  # nothing updated, announced, waiting or failed
    assert updates.staged(folder) == {} and not folder.exists()


def test_a_failed_download_leaves_the_plugin_as_it_was(tmp_path, folder):
    bad = _newer(tmp_path, "a", "0.2.0", sha256="0" * 64)
    result = updates.check([bad], _installed(folder), None, folder)
    assert [name for name, _message in result.failed] == ["a"] and "SHA-256" in result.failed[0][1]
    assert not result.updated and result
    # a wheel that cannot be fetched, and a disk that refuses
    gone = dataclasses.replace(bad, wheel=(tmp_path / "gone-0.2.0-py3-none-any.whl").as_uri())
    result = updates.check([gone], _installed(folder), None, folder)
    assert [name for name, _message in result.failed] == ["a"] and "downloaded" in result.failed[0][1]

    def full(*args, **kwargs):
        raise OSError("no space left on the device")

    result = updates.check([bad], _installed(folder), None, folder, install=full)
    assert result.failed == [("a", "no space left on the device")]
    assert _versions(folder) == {"a": "0.1.0"} and updates.staged(folder) == {}
    assert not (folder / ".updates" / "a").exists() and not list(folder.glob(".install-*"))
    m = Manager(folder)
    m.load()
    assert m.get("a").active and m.get("a").version == "0.1.0" and m.applied == []


def test_a_broken_download_keeps_the_update_staged_earlier(tmp_path, folder):
    updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)
    broken = _newer(tmp_path, "a", "0.3.0", sha256="0" * 64)
    assert updates.check([broken], _installed(folder), None, folder).failed
    assert updates.staged(folder) == {"a": "0.2.0"} and not list(folder.glob(".install-*"))
    m = Manager(folder)
    m.load()
    assert m.applied == [("a", "0.1.0", "0.2.0")]


def test_a_disabled_plugin_is_updated_and_stays_disabled(tmp_path, folder):
    Manager(folder).set_enabled("a", False)
    result = updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)
    assert result.updated == [("a", "0.1.0", "0.2.0")]
    m = Manager(folder)
    m.load()
    info = m.get("a")
    assert m.applied == [("a", "0.1.0", "0.2.0")]
    assert info.version == "0.2.0" and not info.enabled and not info.active


def test_a_staged_update_never_brings_back_a_removed_plugin(tmp_path):
    # removed after the update was staged
    removed = tmp_path / "removed"
    _install_folder(removed, "a")
    updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(removed), None, removed)
    assert updates.staged(removed) == {"a": "0.2.0"}
    m = Manager(removed)
    m.discover()
    m.remove("a")
    assert not (removed / "a").exists() and not list((removed / ".updates").iterdir())
    after = Manager(removed)
    after.load()
    assert after.applied == [] and after.plugins == [] and not (removed / "a").exists()
    # another version installed by hand after it was staged: that one wins
    by_hand = tmp_path / "by-hand"
    _install_folder(by_hand, "a")
    updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(by_hand), None, by_hand)
    plugin_index.install(_newer(tmp_path, "a", "0.1.5"), folder=by_hand)
    assert not list((by_hand / ".updates").iterdir()) and not list(by_hand.glob(".install-*"))
    kept = Manager(by_hand)
    kept.load()
    assert kept.applied == [] and kept.get("a").version == "0.1.5"
    # a folder copied into .updates for a plugin that is not installed (any more) is dropped
    orphan = tmp_path / "orphan"
    _install_folder(orphan / ".updates", "a", version="0.2.0")
    assert updates.staged(orphan) == {"a": "0.2.0"}
    assert updates.apply_staged(orphan) == []
    assert not list((orphan / ".updates").iterdir()) and not (orphan / "a").exists()
    # ... and one that is not newer than the plugin installed
    stale = tmp_path / "stale"
    _install_folder(stale, "a", version="0.2.0")
    _install_folder(stale / ".updates", "a", version="0.2.0")
    assert updates.apply_staged(stale) == []
    assert not list((stale / ".updates").iterdir()) and _versions(stale) == {"a": "0.2.0"}


def test_new_plugins_since_the_last_look(tmp_path, folder):
    a, b, c = (_newer(tmp_path, name, "0.1.0") for name in "abc")
    first = updates.check([a, c], _installed(folder), None, folder)
    assert first.new == [] and first.seen == ["a", "c"] and not first  # the first look only takes note
    second = updates.check([a, b, c], _installed(folder), first.seen, folder)
    assert second.new == [b] and second.seen == ["a", "b", "c"] and second
    # installed meanwhile - by the program or with pip - it is not new
    _install_folder(folder, "b")
    assert updates.check([a, b, c], _installed(folder), first.seen, folder).new == []
    pip = PluginInfo(name="b", title="b", version="0.1.0", description="", source="environment",
                     distribution="b", location=None)
    assert updates.check([a, b, c], [pip], first.seen, folder).new == []
    # with the switch off a newer version is announced, nothing is downloaded
    newer = _newer(tmp_path, "a", "0.2.0")
    off = updates.check([newer, b, c], _installed(folder), second.seen, folder, update=False, install=_never)
    assert off.available == [("a", "0.1.0", "0.2.0")] and not off.updated and off
    assert updates.staged(folder) == {}


def test_run_reads_the_index_and_stages_what_is_newer(tmp_path, folder):
    wheel_a = _wheel(tmp_path / "a-0.2.0-py3-none-any.whl", "a", version="0.2.0")
    wheel_b = _wheel(tmp_path / "b-0.1.0-py3-none-any.whl", "b")
    url = _index(tmp_path, [_entry(wheel_a, "a", version="0.2.0"), _entry(wheel_b, "b")])
    result = updates.run(["a"], folder=folder, url=url)
    assert result.updated == [("a", "0.1.0", "0.2.0")] and [e.name for e in result.new] == ["b"]
    assert result.seen == ["a", "b"] and updates.staged(folder) == {"a": "0.2.0"}
    with pytest.raises(plugin_index.PluginError, match="could not be read"):
        updates.run(None, folder=folder, url=(tmp_path / "missing.json").as_uri())


def test_staging_leaves_the_plugin_installed_alone(tmp_path, folder):
    path = plugin_index.install(_newer(tmp_path, "a", "0.2.0"), folder=folder, staging=True)
    assert path == folder / ".updates" / "a" and _versions(folder) == {"a": "0.1.0"}
    assert updates.staged(folder) == {"a": "0.2.0"}
    # a newer one replaces the staged one
    plugin_index.install(_newer(tmp_path, "a", "0.3.0"), folder=folder, staging=True)
    assert updates.staged(folder) == {"a": "0.3.0"} and not list(folder.glob(".install-*"))
    assert _versions(folder) == {"a": "0.1.0"}


def test_a_swap_that_fails_puts_the_old_folder_back(tmp_path, folder):
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(OSError):
        plugin_index.swap_folder(tmp_path / "not-there", folder / "a", work)  # nothing to put in place
    assert _versions(folder) == {"a": "0.1.0"} and not list(work.iterdir())
    new = tmp_path / "new"
    _install_folder(new, "a", version="0.2.0")
    plugin_index.swap_folder(new / "a", folder / "a", work)
    assert _versions(folder) == {"a": "0.2.0"} and not (new / "a").exists()


def test_an_update_another_process_is_applying_is_left_to_it(tmp_path, folder, monkeypatch):
    updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)
    real_rename = Path.rename

    def lost(self, target):
        if Path(target).name.startswith(".applying-"):
            raise FileNotFoundError(str(self))  # the other process renamed it first
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", lost)
    assert updates.apply_staged(folder) == []
    monkeypatch.undo()
    assert _versions(folder) == {"a": "0.1.0"} and [p.name for p in (folder / ".updates").iterdir()] == ["a"]
    assert updates.apply_staged(folder) == [("a", "0.1.0", "0.2.0")]


def test_an_update_that_cannot_be_put_in_place_stays_staged(tmp_path, folder, monkeypatch):
    updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)

    def refuse(*args, **kwargs):
        raise PermissionError("the folder is in use")

    monkeypatch.setattr(plugin_index, "swap_folder", refuse)
    assert updates.apply_staged(folder) == []
    assert _versions(folder) == {"a": "0.1.0"} and updates.staged(folder) == {"a": "0.2.0"}
    assert not list(folder.glob(".install-*"))
    monkeypatch.undo()  # the next start tries again
    assert updates.apply_staged(folder) == [("a", "0.1.0", "0.2.0")] and _versions(folder) == {"a": "0.2.0"}


def test_safe_mode_applies_the_update_but_runs_nothing(tmp_path, folder, monkeypatch):
    updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)
    monkeypatch.setenv("PPG_NO_PLUGINS", "1")
    m = Manager(folder)
    m.load()
    assert m.applied == [("a", "0.1.0", "0.2.0")] and m.get("a").version == "0.2.0" and not m.get("a").active


def test_a_plugin_that_cannot_be_put_back_is_found_again_at_the_next_start(tmp_path, folder, monkeypatch, caplog):
    """A plugin is never left missing: the new folder cannot be put in place and the old one cannot be put back (a
    rename onto its folder refused twice). The old one stays in its work folder - not deleted with it - and the next
    start puts it back."""
    refused = _refuse_renames_onto(monkeypatch, folder / "a")
    with pytest.raises(plugin_index.PluginError, match="could not be put in place"):
        plugin_index.install(_newer(tmp_path, "a", "0.2.0"), folder=folder)
    monkeypatch.undo()
    assert len(refused) == 2 and not (folder / "a").exists()
    (work,) = folder.glob(".install-a-*")
    assert (work / "previous").is_dir()  # the only copy is on the disk still
    m = Manager(folder)
    with caplog.at_level(logging.WARNING, logger=updates.__name__):
        m.load()
    assert m.get("a").active and m.get("a").version == "0.1.0" and m.applied == []
    assert "put back" in caplog.text and not list(folder.glob(".install-*"))


def test_an_update_applied_at_the_start_never_loses_the_plugin(tmp_path, folder, monkeypatch):
    updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)
    refused = _refuse_renames_onto(monkeypatch, folder / "a")
    assert updates.apply_staged(folder) == []
    monkeypatch.undo()
    assert len(refused) == 2 and not (folder / "a").exists()
    assert updates.staged(folder) == {"a": "0.2.0"}  # given back to the updates: tried again at the next start
    (work,) = folder.glob(".install-a-*")
    assert (work / "previous").is_dir()
    m = Manager(folder)
    m.load()  # puts 0.1.0 back, then applies the update
    assert m.applied == [("a", "0.1.0", "0.2.0")] and m.get("a").active and m.get("a").version == "0.2.0"
    assert not list(folder.glob(".install-*"))


def test_ctrl_c_between_the_two_renames_puts_the_plugin_back(tmp_path, folder, monkeypatch):
    refused = _refuse_renames_onto(monkeypatch, folder / "a", error=KeyboardInterrupt, times=1)
    with pytest.raises(KeyboardInterrupt):
        plugin_index.install(_newer(tmp_path, "a", "0.2.0"), folder=folder)
    monkeypatch.undo()
    assert len(refused) == 1 and _versions(folder) == {"a": "0.1.0"} and not list(folder.glob(".install-*"))


def test_only_a_swap_that_stopped_brings_a_plugin_back(tmp_path):
    folder = tmp_path / "plugins"
    # one that stopped (its file says where the plugin belongs) is put back ...
    _work_folder(tmp_path, folder, "b", marker=True)
    # ... one that finished (the old plugin is garbage; the plugin may have been removed since) is not
    finished = _work_folder(tmp_path, folder, "a", marker=False)
    # ... nor is one when the plugin is there
    _work_folder(tmp_path, folder, "c", marker=True)
    _install_folder(folder, "c", version="0.3.0")
    # ... nor the old copy of a staged update (it is downloaded again), whatever else it says
    staged_copy = _work_folder(tmp_path, folder, "d", marker=True)
    (staged_copy / plugin_index.SWAPPING).write_text(".updates/d")
    (folder / ".updates").mkdir()
    assert updates.apply_staged(folder) == []
    assert _versions(folder) == {"b": "0.1.0", "c": "0.3.0"} and (finished / "previous").is_dir()
    assert not (folder / "d").exists() and (staged_copy / "previous").is_dir()
    assert sorted(p.name for p in folder.glob(".install-*")) == [
        finished.name, ".install-c-x1y2z3w4", staged_copy.name]


def test_a_work_folder_that_could_not_be_deleted_never_brings_back_a_removed_plugin(tmp_path, folder, monkeypatch):
    # a swap that finishes, and a work folder that cannot be deleted (a file in use): the old plugin stays in it
    monkeypatch.setattr(shutil, "rmtree", lambda *args, **kwargs: None)
    plugin_index.install(_newer(tmp_path, "a", "0.2.0"), folder=folder)
    monkeypatch.undo()
    (work,) = folder.glob(".install-a-*")
    assert (work / "previous").is_dir() and _versions(folder) == {"a": "0.2.0"}
    m = Manager(folder)
    m.discover()
    m.remove("a")  # the user removes the plugin ...
    assert updates.apply_staged(folder) == []
    assert not (folder / "a").exists()  # ... and it stays removed


def test_a_folder_that_does_not_exist_has_nothing_to_apply(tmp_path):
    assert updates.apply_staged(tmp_path / "none") == [] and updates.staged(tmp_path / "none") == {}
    m = Manager(tmp_path / "none")
    m.load()
    assert m.applied == [] and m.plugins == []


@pytest.mark.parametrize("broken", ["swap_folder", "_safe_extract"])
def test_a_disk_error_while_installing_is_a_plugin_error(tmp_path, folder, monkeypatch, broken):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    def disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    entry = _newer(tmp_path, "a", "0.2.0")
    monkeypatch.setattr(plugin_index, broken, disk)
    for staging in (False, True):
        with pytest.raises(plugin_index.PluginError, match="a: could not be put in place: .*No space left"):
            plugin_index.install(entry, folder=folder, staging=staging)
    assert _versions(folder) == {"a": "0.1.0"} and not list(folder.glob(".install-*"))
    result = updates.check([entry], _installed(folder), None, folder)
    assert [name for name, _why in result.failed] == ["a"] and "No space left" in result.failed[0][1]
    # the command says it, with no traceback
    monkeypatch.setenv("PPG_PLUGINS_DIR", str(folder))
    monkeypatch.setenv("PPG_PLUGIN_INDEX", _index(tmp_path, [dataclasses.asdict(entry)]))
    r = CliRunner().invoke(app, ["plugins", "install", "a"])
    assert r.exit_code == 1 and "could not be put in place" in r.output and isinstance(r.exception, SystemExit)


def test_removing_a_plugin_drops_its_staged_update_first(tmp_path, folder, monkeypatch):
    updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)
    m = Manager(folder)
    m.discover()
    real = shutil.rmtree

    def stuck(path, *args, **kwargs):  # a file of the plugin is in use: its folder cannot be deleted
        if Path(path) == folder / "a":
            raise PermissionError("in use")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", stuck)
    with pytest.raises(PermissionError):
        m.remove("a")
    monkeypatch.undo()
    assert updates.staged(folder) == {}  # nothing is left that could bring the plugin back
    assert (folder / "a").is_dir()


def test_a_staged_update_is_dropped_all_at_once(tmp_path, folder, monkeypatch, caplog):
    plugin_index.install(_newer(tmp_path, "a", "0.2.0"), folder=folder, staging=True)
    real = shutil.rmtree

    def half(path, *args, **kwargs):  # deletes the code and stops: what is left reads as a version
        for child in Path(path).iterdir():
            if not child.name.endswith(".dist-info"):
                real(child)

    monkeypatch.setattr(shutil, "rmtree", half)
    plugin_index.drop_staged(folder, "a")
    monkeypatch.undo()
    assert not (folder / ".updates" / "a").exists() and updates.staged(folder) == {}
    (trash,) = (folder / ".updates").glob(".trash-a-*")
    assert [p.name for p in trash.iterdir()] == ["a-0.2.0.dist-info"]  # the remains, where nothing applies them
    assert updates.apply_staged(folder) == [] and _versions(folder) == {"a": "0.1.0"}
    # a folder that cannot be renamed is a warning, and stays whole
    plugin_index.install(_newer(tmp_path, "a", "0.3.0"), folder=folder, staging=True)
    real_rename = Path.rename

    def locked(self, dst):
        if Path(dst).name.startswith(".trash-"):
            raise PermissionError("in use")
        return real_rename(self, dst)

    monkeypatch.setattr(Path, "rename", locked)
    with caplog.at_level(logging.WARNING, logger=plugin_index.__name__):
        plugin_index.drop_staged(folder, "a")
    monkeypatch.undo()
    assert "could not be dropped" in caplog.text and updates.staged(folder) == {"a": "0.3.0"}


def test_what_a_crashed_run_left_is_deleted_after_a_day(tmp_path, folder, monkeypatch):
    root = folder / ".updates"
    day_old = time.time() - 2 * 24 * 3600
    old = [folder / ".install-a-old00000", root / ".applying-a-4242", root / ".trash-a-old00000"]
    new = [folder / ".install-a-new00000", root / ".applying-a-4343", root / ".trash-a-new00000"]
    for path in [*old, *new]:
        (path / "x").mkdir(parents=True)
    for path in old:
        os.utime(path, (day_old, day_old))
    kept = _work_folder(tmp_path, folder, "b", marker=True)  # old, and the only copy of plugin b ...
    os.utime(kept, (day_old, day_old))
    _refuse_renames_onto(monkeypatch, folder / "b")  # ... that cannot be put back just now
    assert updates.apply_staged(folder) == []
    monkeypatch.undo()
    assert [p.exists() for p in old] == [False] * 3 and [p.exists() for p in new] == [True] * 3
    assert (kept / "previous").is_dir()
    assert updates.apply_staged(folder) == [] and _versions(folder) == {"a": "0.1.0", "b": "0.1.0"}  # put back now
    assert not kept.exists()


def test_a_staged_update_claimed_after_days_is_fresh_for_the_sweep(tmp_path, folder, monkeypatch):
    updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)
    days_ago = time.time() - 5 * 24 * 3600
    os.utime(folder / ".updates" / "a", (days_ago, days_ago))  # it was unpacked days ago, a rename keeps that

    def refuse(*args, **kwargs):
        raise PermissionError("the folder is in use")

    monkeypatch.setattr(plugin_index, "swap_folder", refuse)
    assert updates.apply_staged(folder) == []
    assert time.time() - (folder / ".updates" / "a").stat().st_mtime < 3600  # claimed now, given back


def test_the_claim_is_never_a_day_old_for_a_sweep_that_comes_right_after_it(tmp_path, folder, monkeypatch):
    """A second copy of the program that starts just as the first claims an update runs the sweep: a claim still dated
    from the unpacking, days ago, would be deleted as one a crashed run left behind."""
    updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)
    days_ago = time.time() - 5 * 24 * 3600
    os.utime(folder / ".updates" / "a", (days_ago, days_ago))  # unpacked days ago, a rename keeps that
    real, swept = Path.rename, []

    def rename(self, destination):
        result = real(self, destination)
        if Path(destination).name.startswith(".applying-"):  # the claim is made: the other copy sweeps now
            updates._sweep(folder)
            swept.append(Path(destination).exists())
        return result

    monkeypatch.setattr(Path, "rename", rename)
    assert updates.apply_staged(folder) == [("a", "0.1.0", "0.2.0")]
    assert swept == [True]  # the claim was still there after the sweep


def _folders_to_swap(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``new`` (just unpacked), ``target`` (the plugin it replaces) and the work folder of a swap."""
    new, target, work = tmp_path / "unpacked", tmp_path / "plugins" / "a", tmp_path / "work"
    for path, text in ((new, "new"), (target, "old")):
        path.mkdir(parents=True)
        (path / "version").write_text(text)
    work.mkdir()
    return new, target, work


def _refuse_rename_of(monkeypatch, path: Path, times: int) -> list:
    """From now on ``Path.rename`` raises a PermissionError for ``path`` (``times`` of them at most); the list holds
    what was refused."""
    real, refused = Path.rename, []

    def rename(self, destination):
        if self == path and len(refused) < times:
            refused.append(destination)
            raise PermissionError(13, "Access is denied")  # what Windows says while an antivirus holds the files
        return real(self, destination)

    monkeypatch.setattr(Path, "rename", rename)
    return refused


def test_a_rename_refused_for_a_moment_on_windows_is_tried_again(tmp_path, monkeypatch):
    new, target, work = _folders_to_swap(tmp_path)
    refused, pauses = _refuse_rename_of(monkeypatch, new, 2), []
    monkeypatch.setattr(plugin_index.time, "sleep", pauses.append)
    with monkeypatch.context() as on_windows:
        on_windows.setattr(os, "name", "nt")
        plugin_index.swap_folder(new, target, work)
    assert len(refused) == 2 and pauses == [0.2, 0.2]  # refused twice, a short pause each time, then it went through
    assert (target / "version").read_text() == "new" and not new.exists()


def test_a_rename_that_windows_keeps_refusing_fails_and_the_old_plugin_stays(tmp_path, monkeypatch):
    new, target, work = _folders_to_swap(tmp_path)
    refused, pauses = _refuse_rename_of(monkeypatch, new, 99), []
    monkeypatch.setattr(plugin_index.time, "sleep", pauses.append)
    with monkeypatch.context() as on_windows:
        on_windows.setattr(os, "name", "nt")
        with pytest.raises(PermissionError):
            plugin_index.swap_folder(new, target, work)
    assert len(refused) == 6 and pauses == [0.2] * 5  # the first try and five more, with a pause before each of them
    assert (target / "version").read_text() == "old" and (new / "version").read_text() == "new"  # nothing is lost


def test_a_refused_rename_is_not_tried_again_on_other_systems(tmp_path, monkeypatch):
    new, target, work = _folders_to_swap(tmp_path)
    refused, pauses = _refuse_rename_of(monkeypatch, new, 99), []
    monkeypatch.setattr(plugin_index.time, "sleep", pauses.append)
    with monkeypatch.context() as elsewhere:
        elsewhere.setattr(os, "name", "posix")
        with pytest.raises(PermissionError):
            plugin_index.swap_folder(new, target, work)
    assert len(refused) == 1 and pauses == []  # nothing to wait for there
    assert (target / "version").read_text() == "old"


def test_staged_versions_are_compared_as_versions(tmp_path, folder):
    entry = dataclasses.replace(_newer(tmp_path, "a", "0.2.0"), version="0.2")  # the index says 0.2, the wheel 0.2.0
    assert updates.check([entry], _installed(folder), None, folder).updated == [("a", "0.1.0", "0.2")]
    again = updates.check([entry], _installed(folder), None, folder, install=_never)
    assert not again and again.pending == [("a", "0.1.0", "0.2")]
    assert updates._same_version("0.2", "0.2.0") and not updates._same_version("0.3", "0.2.0")
    assert updates._same_version("1.0-custom", "1.0-custom") and not updates._same_version("1.0-custom", "1.1-custom")
    assert not updates._same_version(None, "0.2")


def test_a_plugin_placed_under_another_folder_name_is_not_staged(tmp_path, folder):
    (folder / "a").rename(folder / "my-a")  # by hand: an update is put in <folder>/a
    (info,) = _installed(folder)
    assert info.name == "a" and info.location == folder / "my-a"
    entry = _newer(tmp_path, "a", "0.2.0")
    result = updates.check([entry], [info], None, folder, install=_never)
    ((name, version, why),) = result.waiting
    assert (name, version) == ("a", "0.2.0") and "update it by hand" in why
    assert str(folder / "my-a") in why and str(folder / "a") in why
    assert not (result.updated or result.available or result.failed) and result
    assert updates.staged(folder) == {}


def test_ppg_plugin_tells_the_updates_the_start_applied(tmp_path, folder, monkeypatch):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    updates.check([_newer(tmp_path, "a", "0.2.0")], _installed(folder), None, folder)  # downloaded by an earlier run
    monkeypatch.setenv("PPG_PLUGINS_DIR", str(folder))
    runner = CliRunner()
    r = runner.invoke(app, ["plugin"])
    assert r.exit_code == 0 and "plugin a updated from 0.1.0 to 0.2.0" in r.output
    assert _versions(folder) == {"a": "0.2.0"}
    r = runner.invoke(app, ["plugin"])  # the next run has nothing to put in place: nothing to say
    assert r.exit_code == 0 and "updated from" not in r.output


def test_command_line_update(tmp_path, folder, monkeypatch):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    monkeypatch.setenv("PPG_PLUGINS_DIR", str(folder))
    newer = _entry(_wheel(tmp_path / "a-0.2.0-py3-none-any.whl", "a", version="0.2.0"), "a", version="0.2.0")
    monkeypatch.setenv("PPG_PLUGIN_INDEX", _index(tmp_path, [newer]))
    runner = CliRunner()
    r = runner.invoke(app, ["plugins", "available"])
    assert r.exit_code == 0 and "(installed 0.1.0: can be updated)" in r.output
    r = runner.invoke(app, ["plugins", "update", "--dry-run"])
    assert r.exit_code == 0 and "a 0.1.0 -> 0.2.0 can be updated" in r.output
    assert updates.staged(folder) == {}
    r = runner.invoke(app, ["plugins", "update"])
    assert r.exit_code == 0 and "a 0.1.0 -> 0.2.0 (active from the next start)" in r.output
    assert updates.staged(folder) == {"a": "0.2.0"} and _versions(folder) == {"a": "0.1.0"}
    r = runner.invoke(app, ["plugins", "available"])
    assert r.exit_code == 0 and "(update to 0.2.0 ready: active from the next start)" in r.output
    r = runner.invoke(app, ["plugins", "update"])  # staged already: said so, not "up to date"
    assert r.exit_code == 0 and "a 0.1.0 -> 0.2.0 ready: active from the next start" in r.output
    assert "up to date" not in r.output and updates.staged(folder) == {"a": "0.2.0"}
    r = runner.invoke(app, ["plugins", "update", "--dry-run"])
    assert r.exit_code == 0 and "a 0.1.0 -> 0.2.0 ready: active from the next start" in r.output
    assert updates.apply_staged(folder) == [("a", "0.1.0", "0.2.0")]  # the next start
    r = runner.invoke(app, ["plugins", "update"])
    assert r.exit_code == 0 and "The plugins are up to date." in r.output
    # a version that needs a newer program waits, one that cannot be downloaded fails
    waits = _entry(_wheel(tmp_path / "a-0.3.0-py3-none-any.whl", "a", version="0.3.0"), "a", version="0.3.0",
                   min_app="99.0")
    monkeypatch.setenv("PPG_PLUGIN_INDEX", _index(tmp_path, [waits]))
    r = runner.invoke(app, ["plugins", "update"])
    assert r.exit_code == 0 and "a 0.3.0 waits: needs pyarchinit-photogrammetry 99.0 or later" in r.output
    r = runner.invoke(app, ["plugins", "available"])
    assert "[needs pyarchinit-photogrammetry 99.0 or later]" in r.output and "can be updated" not in r.output
    monkeypatch.setenv("PPG_PLUGIN_INDEX", _index(tmp_path, [{**newer, "version": "0.4.0", "sha256": "0" * 64}]))
    r = runner.invoke(app, ["plugins", "update"])
    assert r.exit_code == 1 and "a failed: " in r.output and "SHA-256" in r.output
    # an index that cannot be read
    monkeypatch.setenv("PPG_PLUGIN_INDEX", (tmp_path / "missing.json").as_uri())
    r = runner.invoke(app, ["plugins", "update"])
    assert r.exit_code == 1 and "could not be read" in r.output
