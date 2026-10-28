"""Updates of the plugins installed by the program: a newer version that this program can run is downloaded and
checked at once, kept in ``<plugins>/.updates/<name>/`` and put in place at the next start, before any plugin is
imported - a running session never mixes the modules of two versions of a plugin."""
from __future__ import annotations

import dataclasses
import logging
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
    # the same version is not downloaded again
    assert not updates.check([entry], _installed(folder), None, folder, install=_never)
    # the next start puts it in place, before any plugin is imported
    m = Manager(folder)
    assert m.applied == []
    with caplog.at_level(logging.INFO, logger=updates.__name__):
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
