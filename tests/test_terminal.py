"""*Strumenti > Apri un terminale* (``terminal.build``): the command per system built by parameter, the PATH with the
program's tools first and the folders of Node after, the script for the packaged application. Nothing is opened:
no terminal window, no browser."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from pyarchinit_photogrammetry import terminal

TOOLS = "/venv/bin"
NODE = "/opt/homebrew/bin"


def _which(found: dict[str, str]):
    return lambda name: found.get(name)


def test_macos_terminal_app_runs_a_script_that_sets_the_path(tmp_path):
    folder = tmp_path / "scavo con spazio"
    launch = terminal.build("darwin", folder, env={"PATH": "/usr/bin:/bin", "SHELL": "/bin/zsh"}, tools=[TOOLS],
                            extra=[NODE], where=tmp_path / "w", which=_which({"open": "/usr/bin/open"}))
    script = str(tmp_path / "w" / "ppg-terminal.command")
    assert launch.argv == ["/usr/bin/open", "-a", "Terminal", script] and not launch.problem
    text = launch.files[script]
    assert text.startswith("#!/bin/sh\n")
    assert f"export PATH={TOOLS}:\"$PATH\":{NODE}" in text  # tools first, Node after the user's PATH
    assert f"cd {shlex.quote(str(folder))} || exit 1" in text
    assert 'exec "${SHELL:-/bin/sh}" -i' in text and "PPG_APP" not in text
    assert launch.env["PATH"] == f"{TOOLS}:/usr/bin:/bin:{NODE}"
    assert not (tmp_path / "w").exists()  # nothing written by build


def test_windows_terminal_or_a_new_console(tmp_path):
    env = {"PATH": r"C:\Windows\system32", "COMSPEC": r"C:\Windows\system32\cmd.exe"}
    folder = r"C:\Users\enzo\scavo"
    tools, node = r"C:\venv\Scripts", r"C:\Users\enzo\AppData\Roaming\npm"
    wt = terminal.build("win32", folder, env=env, tools=[tools], extra=[node], where=tmp_path,
                        which=_which({"wt.exe": r"C:\Users\enzo\AppData\Local\Microsoft\WindowsApps\wt.exe"}))
    script = str(tmp_path / "ppg-terminal.cmd")
    assert wt.argv[:3] == [r"C:\Users\enzo\AppData\Local\Microsoft\WindowsApps\wt.exe", "-d", folder]
    assert wt.argv[3:] == ["cmd.exe", "/k", script] and wt.creationflags == 0
    text = wt.files[script]
    assert f'set "PATH={tools};%PATH%;{node}"' in text and f'cd /d "{folder}"' in text and "\r\n" in text
    assert wt.env["PATH"] == f"{tools};{env['PATH']};{node}"
    console = terminal.build("win32", folder, env=env, tools=[tools], extra=[node], where=tmp_path,
                             which=_which({}))
    assert console.argv == [env["COMSPEC"], "/k", script]
    assert console.creationflags == terminal.CREATE_NEW_CONSOLE and console.cwd == folder


@pytest.mark.parametrize("found, first", [
    ({"x-terminal-emulator": "/usr/bin/x-terminal-emulator", "xterm": "/usr/bin/xterm"},
     ["/usr/bin/x-terminal-emulator", "-e"]),
    ({"gnome-terminal": "/usr/bin/gnome-terminal"}, ["/usr/bin/gnome-terminal", "--working-directory=/scavo", "--"]),
    ({"konsole": "/usr/bin/konsole"}, ["/usr/bin/konsole", "--workdir", "/scavo", "-e"]),
    ({"xterm": "/usr/bin/xterm"}, ["/usr/bin/xterm", "-e"]),
])
def test_linux_the_first_terminal_found(tmp_path, found, first):
    launch = terminal.build("linux", "/scavo", env={"PATH": "/usr/bin"}, tools=[TOOLS], extra=[], where=tmp_path,
                            which=_which(found))
    script = str(tmp_path / "ppg-terminal.sh")
    assert launch.argv == first + [script] and script in launch.files


def test_linux_terminal_variable_and_none_found(tmp_path):
    launch = terminal.build("linux", "/scavo", env={"PATH": "/usr/bin", "TERMINAL": "foot"}, tools=[], extra=[],
                            where=tmp_path, which=_which({"foot": "/usr/bin/foot", "xterm": "/usr/bin/xterm"}))
    assert launch.argv[:2] == ["/usr/bin/foot", "-e"]
    none = terminal.build("linux", "/scavo", env={"PATH": "/usr/bin"}, tools=[], extra=[], where=tmp_path,
                          which=_which({}))
    assert none.problem == "no_terminal" and none.argv == [] and none.files == {}
    with pytest.raises(FileNotFoundError):
        terminal.open_terminal(none)


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_the_packaged_application_offers_ppg_run_and_ppg_doctor(tmp_path, platform):
    app = "/Applications/PyArchInit Photogrammetry.app/Contents/MacOS/PyArchInit Photogrammetry"
    launch = terminal.build(platform, "/scavo", env={"PATH": "/usr/bin"}, tools=[], extra=[], app=app,
                            where=tmp_path, which=_which({"open": "/usr/bin/open", "xterm": "/usr/bin/xterm"}))
    assert launch.env["PPG_APP"] == app
    assert launch.env["PATH"].startswith(str(tmp_path / "bin"))
    names = {Path(p).name for p in launch.files}
    ext = ".cmd" if platform == "win32" else ""
    assert {f"ppg-run{ext}", f"ppg-doctor{ext}"} <= names
    run = next(t for p, t in launch.files.items() if Path(p).name == f"ppg-run{ext}")
    assert app in run and "--run" in run
    if platform == "win32":
        assert "type" in run  # the application has no console: its output shown from a file


def test_tool_dirs(tmp_path):
    assert terminal.tool_dirs(frozen=True) == []
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ppg = bin_dir / ("ppg.exe" if os.name == "nt" else "ppg")
    ppg.write_text("")
    ppg.chmod(0o755)
    assert str(bin_dir) in terminal.tool_dirs(frozen=False, scripts=str(bin_dir))


def test_project_folder(tmp_path):
    class Doc:
        path = tmp_path / "scavo.ppg"

    assert terminal.project_folder(Doc()) == tmp_path.resolve()
    assert terminal.project_folder(None) == Path.home()


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell script")
def test_the_generated_script_sets_path_and_folder(tmp_path):
    """The script run by sh with a fake shell that prints what it got (no terminal opened)."""
    folder = tmp_path / "scavo"
    folder.mkdir()
    shell = tmp_path / "fakeshell"
    shell.write_text('#!/bin/sh\necho "PWD=$(pwd)"\necho "PATH=$PATH"\n')
    shell.chmod(0o755)
    launch = terminal.build("linux", folder, env={"PATH": "/usr/bin:/bin"}, tools=["/tools dir"],
                            extra=["/node"], where=tmp_path / "w", which=_which({"xterm": "/usr/bin/xterm"}))
    script = str(tmp_path / "w" / "ppg-terminal.sh")
    Path(script).parent.mkdir()
    Path(script).write_text(launch.files[script])
    out = subprocess.run(["/bin/sh", script], env={"PATH": "/usr/bin:/bin", "SHELL": str(shell)},
                         capture_output=True, text=True, timeout=30, close_fds=False).stdout
    pwd = next(line[4:] for line in out.splitlines() if line.startswith("PWD="))
    assert Path(pwd).resolve() == folder.resolve()
    assert "PATH=/tools dir:/usr/bin:/bin:/node" in out


def test_the_menu_action(tmp_path, monkeypatch):
    """*Strumenti > Apri un terminale*: the command of this system started (recorded here, not run); a clear
    message when no terminal is found."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window
    from pyarchinit_photogrammetry.gui.i18n import tr

    doc = ppg.Document()
    doc.save(tmp_path / "scavo.ppg")
    w = main_window.MainWindow(ppg.Document.open(tmp_path / "scavo.ppg"), autoconnect=False)
    # Linux: the terminal named by TERMINAL, as a machine without a terminal emulator (a CI runner) has none to
    # find; macOS and Windows have theirs and do not read the variable
    chosen = tmp_path / "terminale"
    chosen.write_text("#!/bin/sh\n")
    chosen.chmod(0o755)
    monkeypatch.setenv("TERMINAL", str(chosen))
    started, said = [], []
    monkeypatch.setattr(terminal, "open_terminal", lambda launch: started.append(launch))
    monkeypatch.setattr(main_window, "message", lambda *a, **k: said.append(a))
    actions = [a for a in w.findChildren(main_window.QAction) if a.text() == tr("Apri un terminale")]
    assert actions and not actions[0].icon().isNull()
    actions[0].trigger()
    assert len(started) == 1 and said == []
    assert started[0].cwd == str(tmp_path.resolve())
    if sys.platform.startswith("linux"):
        assert started[0].argv[:2] == [str(chosen), "-e"]
    monkeypatch.setattr(terminal, "for_this_program",
                        lambda folder: terminal.Launch([], str(folder), {}, problem="no_terminal"))
    assert w.open_terminal() is False and len(started) == 1
    assert said and str(tmp_path.resolve()) in said[0][3]
    w.setWindowModified(False)
    w.close()


def test_terminal_gives_wrangler_a_cache_folder_outside_the_home(tmp_path):
    """wrangler run in the home folder makes ~/.wrangler/cache, which then hides its login: the terminal sets
    WRANGLER_CACHE_DIR (a value of the user is kept)."""
    from pyarchinit_photogrammetry import terminal
    from pyarchinit_photogrammetry.io.site_publish import wrangler_cache_dir

    for platform in ("darwin", "linux", "win32"):
        launch = terminal.build(platform, tmp_path, env={"PATH": ""}, tools=[], extra=[], where=tmp_path / "w",
                                which=lambda name: "/usr/bin/" + name)
        text = "".join(launch.files.values())
        assert "WRANGLER_CACHE_DIR" in text and str(wrangler_cache_dir()) in text, platform
        assert launch.env["WRANGLER_CACHE_DIR"] == str(wrangler_cache_dir())
    mine = terminal.build("darwin", tmp_path, env={"PATH": "", "WRANGLER_CACHE_DIR": "/mine"}, tools=[], extra=[],
                          where=tmp_path / "w")
    assert mine.env["WRANGLER_CACHE_DIR"] == "/mine"
