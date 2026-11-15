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


def _file(folder: Path, name: str, text: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def _activation(folder: Path, windows: bool) -> Path:
    """Make ``folder`` the scripts folder of a virtual environment; what is typed to activate it.

    On Windows ``...\\Scripts\\activate`` runs ``activate.bat``; elsewhere ``activate`` is read with ``source``.
    """
    _file(folder, "activate.bat" if windows else "activate")
    return folder / "activate"


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
    python = _file(other.parent, "python.exe" if sys.platform == "win32" else "python")
    _on_path(monkeypatch, other.parent)
    check = doctor.launcher_check()
    assert (check.level, check.detail) == (doctor.WARN, str(other))
    assert f"not the one in {scripts}" in check.hint
    assert f"('{doctor._typed(python.resolve())} -m pip uninstall pyarchinit-photogrammetry')" in check.hint


def test_no_command_is_made_up_for_a_launcher_whose_python_is_not_found(env, tmp_path, monkeypatch):
    """A launcher in a folder of the user or of a tool has its interpreter elsewhere."""
    scripts, _ = env
    _launcher(scripts)
    other = _launcher(tmp_path / "tools" / "bin")
    _on_path(monkeypatch, other.parent)
    check = doctor.launcher_check()
    assert check.level == doctor.WARN
    assert "with the Python it was installed with ('python -m pip uninstall pyarchinit-photogrammetry'" in check.hint
    assert str(other.parent.resolve() / "python") not in check.hint


def test_the_hint_names_the_way_this_install_comes_first(env, tmp_path, monkeypatch):
    scripts, _ = env
    _launcher(scripts)
    _on_path(monkeypatch, _launcher(tmp_path / "other" / "bin").parent)
    assert "Put that folder first on the PATH" in doctor.launcher_check().hint  # not a virtual environment
    activate = _activation(scripts, sys.platform == "win32")
    hint = doctor.launcher_check().hint
    expected = f"('{doctor._typed(activate)}')" if sys.platform == "win32" else f"('source {doctor._typed(activate)}')"
    assert "Activate the environment of this install " + expected in hint
    assert ".venv/bin/activate" not in hint and "first on the PATH" not in hint


@pytest.mark.parametrize("windows", [False, True])
def test_the_hint_is_worded_for_the_system(tmp_path, windows):
    """Both wordings on every system: the folders are stand-ins, the names are the ones of each system."""
    mine = tmp_path / "env" / ("Scripts" if windows else "bin")
    activate = _activation(mine, windows)
    other = tmp_path / "other" / ("Scripts" if windows else "bin")
    target = _file(other, "ppg-gui.exe" if windows else "ppg-gui")
    python = _file(other, "python.exe" if windows else "python")
    assert doctor._other_launcher_hint(mine, target, windows) == (
        f"Typing 'ppg-gui' in a terminal runs this file, not the one in {mine}. Activate the environment of this "
        f"install ('{'' if windows else 'source '}{doctor._typed(activate)}') or remove the other one "
        f"('{doctor._typed(python)} -m pip uninstall pyarchinit-photogrammetry').")
    half = tmp_path / "half" / mine.name
    _file(half, "activate" if windows else "activate.bat")  # only the file of the other system: nothing to type here
    assert "Put that folder first on the PATH" in doctor._other_launcher_hint(half, target, windows)


def test_a_path_typed_in_a_command_is_quoted_only_with_a_space():
    assert doctor._typed(Path("/opt/env/bin/python")) == str(Path("/opt/env/bin/python"))
    assert doctor._typed(Path("/Users/a b/env/bin/python")) == f'"{Path("/Users/a b/env/bin/python")}"'


#: the second line of a launcher whose interpreter has a long path or a space in it, as pip and uv write it
SECOND_LINES = ["'''exec' {} \"$0\" \"$@\"", "'''exec' \"{}\" \"$0\" \"$@\"", "'''exec' '{}' \"$0\" \"$@\""]


@pytest.mark.skipif(sys.platform == "win32", reason="the first line of a launcher is read on macOS and Linux only")
@pytest.mark.parametrize("second_line, folder", [(None, "elsewhere"), (SECOND_LINES[0], "elsewhere"),
                                                 (SECOND_LINES[1], "else where"), (SECOND_LINES[2], "else where")])
def test_the_python_of_a_launcher_is_the_one_the_launcher_names(tmp_path, second_line, folder):
    """``#!/path/python``, or ``#!/bin/sh`` and the interpreter in the second line (a long path, a space in it)."""
    python = _file(tmp_path / folder, "python3.11")
    if folder == "elsewhere" and " " in str(python):
        pytest.skip("the temporary folder has a space in its path: such a line is never written without quotes")
    beside = _file(tmp_path / "other" / "bin", "python")  # not this one: the launcher says which
    head = f"#!{python}\n" if second_line is None else "#!/bin/sh\n" + second_line.format(python) + "\n' '''\n"
    launcher = _file(beside.parent, "ppg-gui", head + "import sys\n")
    assert doctor._python_of(launcher, windows=False) == python


@pytest.mark.skipif(sys.platform == "win32", reason="the first line of a launcher is read on macOS and Linux only")
def test_a_launcher_that_names_no_python_has_it_beside_itself(tmp_path, monkeypatch):
    folder = tmp_path / "other" / "bin"
    gone = tmp_path / "removed" / "python3"
    there = _file(tmp_path / "elsewhere", "python3")
    monkeypatch.chdir(there.parent)  # a name without its folder is not looked for where the diagnosis is run
    for head in ("#!/bin/sh\n", "#!/usr/bin/env python3\n", f"#!{gone}\n", "#!/bin/sh\n'''exec' \"unbalanced\n", "",
                 "#!python3\n", f"#!/bin/sh\necho {there}\n", "#!/bin/sh\n'''exec' python3 \"$0\" \"$@\"\n"):
        launcher = _file(folder, "ppg-gui", head)
        assert doctor._python_of(launcher, windows=False) is None, head
    python3 = _file(folder, "python3")
    assert doctor._python_of(launcher, windows=False) == python3
    python = _file(folder, "python")
    assert doctor._python_of(launcher, windows=False) == python


def test_on_windows_the_python_is_beside_the_launcher_or_beside_its_scripts_folder(tmp_path):
    """A virtual environment has ``python.exe`` in ``Scripts``; a base install beside ``Scripts``."""
    named = _file(tmp_path / "elsewhere", "python.exe")
    venv = _file(tmp_path / "venv" / "Scripts", "ppg-gui.exe", f"#!{named}\n")  # an executable: no line is read
    assert doctor._python_of(venv, windows=True) is None
    base = _file(tmp_path / "base" / "Scripts", "ppg-gui.exe")
    above = _file(tmp_path / "base", "python.exe")
    assert doctor._python_of(base, windows=True) == above
    inside = _file(venv.parent, "python.exe")
    assert doctor._python_of(venv, windows=True) == inside
    tool = _file(tmp_path / "tools" / "bin", "ppg-gui.exe")
    _file(tmp_path / "tools", "python.exe")  # beside a folder that is not Scripts: not its interpreter
    assert doctor._python_of(tool, windows=True) is None


def test_the_launcher_is_shown_with_the_name_its_folder_gives_it(tmp_path):
    """On Windows ``shutil.which`` answers ``ppg-gui.EXE`` for the file ``ppg-gui.exe``."""
    launcher = _file(tmp_path / "Scripts", "ppg-gui.exe")
    answered = str(launcher.parent / "ppg-gui.EXE")
    assert doctor._as_on_disk(answered, windows=True) == str(launcher)
    assert doctor._as_on_disk(answered, windows=False) == answered
    missing = str(tmp_path / "nowhere" / "ppg-gui.EXE")
    assert doctor._as_on_disk(missing, windows=True) == missing


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
