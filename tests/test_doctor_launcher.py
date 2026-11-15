"""The diagnosis tells whose ``ppg-gui`` a terminal runs.

The launchers of an install are in the scripts folder of its environment, which is not the folder of the
interpreter: the ``python`` of a virtual environment is a link to an interpreter elsewhere, and a base install on
Windows keeps ``python.exe`` beside ``Scripts``, not inside it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from pyarchinit_photogrammetry import doctor

NAME = "ppg-gui.exe" if sys.platform == "win32" else "ppg-gui"


def _launcher(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / NAME
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An environment whose interpreter is in one folder and whose launchers are in another."""
    scripts, user_scripts = tmp_path / "env" / "bin", tmp_path / "user" / "bin"
    monkeypatch.setattr(sys, "executable", str(tmp_path / "base" / "bin" / "python"))
    monkeypatch.setattr(doctor, "_scripts_dir", lambda user=False: user_scripts if user else scripts)
    monkeypatch.setattr(doctor, "_installed_for_the_user", lambda: False)
    return scripts, user_scripts


def _on_path(monkeypatch, folder: Path) -> None:
    monkeypatch.setenv("PATH", str(folder))


def test_the_launcher_in_the_scripts_folder_of_the_environment_is_this_install(env, monkeypatch):
    scripts, _ = env
    launcher = _launcher(scripts)
    _on_path(monkeypatch, scripts)
    check = doctor.launcher_check()
    assert (check.name, check.level, check.detail, check.hint) == ("ppg-gui on PATH", doctor.OK, str(launcher), "")


@pytest.mark.skipif(sys.platform == "win32", reason="links need a privilege on Windows")
def test_a_link_to_the_launcher_of_this_install_is_this_install(env, tmp_path, monkeypatch):
    scripts, _ = env
    launcher = _launcher(scripts)
    link = tmp_path / "links" / NAME
    link.parent.mkdir()
    link.symlink_to(launcher)
    _on_path(monkeypatch, link.parent)
    check = doctor.launcher_check()
    assert (check.level, check.detail) == (doctor.OK, str(link))


def test_the_launcher_of_another_install_is_reported_with_its_own_python(env, tmp_path, monkeypatch):
    scripts, _ = env
    _launcher(scripts)
    other = _launcher(tmp_path / "other" / "bin")
    _on_path(monkeypatch, other.parent)
    check = doctor.launcher_check()
    assert (check.level, check.detail) == (doctor.WARN, str(other))
    assert f"not the one in {scripts}" in check.hint
    assert f"'{other.parent.resolve() / 'python'} -m pip uninstall pyarchinit-photogrammetry'" in check.hint


def test_the_user_scripts_folder_counts_only_for_an_install_made_for_the_user(env, monkeypatch):
    _, user_scripts = env
    launcher = _launcher(user_scripts)
    _on_path(monkeypatch, user_scripts)
    assert doctor.launcher_check().level == doctor.WARN  # another Python may have put it there
    monkeypatch.setattr(doctor, "_installed_for_the_user", lambda: True)
    check = doctor.launcher_check()
    assert (check.level, check.detail) == (doctor.OK, str(launcher))


def test_no_launcher_on_the_path(env, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    _on_path(monkeypatch, empty)
    check = doctor.launcher_check()
    assert (check.level, check.detail) == (doctor.WARN, "not found")
    assert "python -m pyarchinit_photogrammetry.gui" in check.hint


def test_the_launcher_of_this_environment_is_this_install(monkeypatch):
    """No stand-in: the scripts folder that this interpreter reports, first on the path."""
    scripts = doctor._scripts_dir()
    if not (scripts / NAME).exists():
        pytest.skip("the program is not installed with its launchers in this environment")
    monkeypatch.setenv("PATH", str(scripts) + os.pathsep + os.environ.get("PATH", ""))
    check = doctor.launcher_check()
    assert (check.level, check.hint) == (doctor.OK, ""), check.hint


def test_an_install_for_the_user_is_one_whose_distribution_is_in_the_user_site(tmp_path, monkeypatch):
    import site
    from importlib import metadata

    try:
        where = metadata.distribution("pyarchinit-photogrammetry").locate_file("")
    except metadata.PackageNotFoundError:
        pytest.skip("the program is run from a source tree, without its distribution")
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(tmp_path))
    assert doctor._installed_for_the_user() is False
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(where))
    assert doctor._installed_for_the_user() is True
