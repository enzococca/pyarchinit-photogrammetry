"""The system terminal opened in a folder with the program's own tools reachable (since 1.10.1).

Not a terminal inside the program: the one of the system (Terminal.app on macOS; Windows Terminal or a new console
on Windows; the first terminal emulator found on Linux), started in the folder of the project with, first on the
PATH, the folder of the ``ppg`` command (an installation with pip or in a virtual environment) and, after the PATH,
the usual folders of Node (:func:`..io.site_publish.extra_dirs`) so that ``npx`` and ``wrangler`` are found.

The packaged application (PyInstaller) has no ``ppg`` command: its executable runs scripts with ``--run`` and the
diagnostics with ``--doctor``. There the terminal gets two small commands, ``ppg-run`` (a Python script run by the
application, with the program importable) and ``ppg-doctor``, and ``PPG_APP``, the path of the executable.

:func:`build` is pure (argv, environment, folder and the files to write, per platform): it opens nothing and is
tested on every system. :func:`open_terminal` writes the files and starts the command. The generated script holds
only folders (no secrets); it is written in a folder of the user in the temporary directory, never in the project.
On macOS Terminal.app does not inherit an environment from ``open``: the script exports PATH, goes to the folder and
runs the user's shell; the shell's own start-up files may still put other folders before them.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

# Linux terminal emulators, in order: argv for (terminal, folder, script)
LINUX_TERMINALS: list[tuple[str, Callable[[str, str, str], list[str]]]] = [
    ("x-terminal-emulator", lambda t, d, s: [t, "-e", s]),
    ("gnome-terminal", lambda t, d, s: [t, f"--working-directory={d}", "--", s]),
    ("konsole", lambda t, d, s: [t, "--workdir", d, "-e", s]),
    ("xfce4-terminal", lambda t, d, s: [t, f"--working-directory={d}", "-x", s]),
    ("mate-terminal", lambda t, d, s: [t, f"--working-directory={d}", "-x", s]),
    ("tilix", lambda t, d, s: [t, "-w", d, "-e", s]),
    ("kitty", lambda t, d, s: [t, "--directory", d, s]),
    ("alacritty", lambda t, d, s: [t, "--working-directory", d, "-e", s]),
    ("wezterm", lambda t, d, s: [t, "start", "--cwd", d, "--", s]),
    ("lxterminal", lambda t, d, s: [t, f"--working-directory={d}", "-e", s]),
    ("xterm", lambda t, d, s: [t, "-e", s]),
]
CREATE_NEW_CONSOLE = 0x00000010  # subprocess.CREATE_NEW_CONSOLE, defined on Windows only


@dataclass
class Launch:
    argv: list[str]
    cwd: str
    env: dict[str, str]
    files: dict[str, str] = field(default_factory=dict)  # path -> text, written (executable) before starting
    creationflags: int = 0
    problem: str = ""  # "no_terminal": nothing can be started


def tool_dirs(frozen: bool | None = None, scripts: str | None = None) -> list[str]:
    """Folders of the program's own commands: the folder of ``ppg`` of the running Python (its ``Scripts`` or
    ``bin``); none in the packaged application (see :func:`build`)."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if frozen:
        return []
    scripts = scripts or sysconfig.get_path("scripts")
    out = []
    for folder in (scripts, str(Path(sys.executable).parent)):
        if folder and Path(folder).is_dir() and folder not in out and shutil.which("ppg", path=folder):
            out.append(folder)
    return out


def script_folder() -> Path:
    """Where the generated scripts go: a folder of this user in the temporary directory (not the project)."""
    who = str(os.getuid()) if hasattr(os, "getuid") else (os.environ.get("USERNAME") or "user")
    return Path(tempfile.gettempdir()) / f"ppg-terminal-{who}"


def _posix_script(folder: str, first: list[str], last: list[str], app: str | None) -> str:
    q = shlex.quote
    path = ":".join([q(d) for d in first] + ['"$PATH"'] + [q(d) for d in last])
    lines = ["#!/bin/sh", "# opened by PyArchInit Photogrammetry: its tools on the PATH, then the user's shell",
             f"export PATH={path}"]
    if app:
        lines.append(f"export PPG_APP={q(app)}")
    lines += [f"cd {q(folder)} || exit 1",
              "echo " + q("PyArchInit Photogrammetry: " + ("ppg-run script.py, ppg-doctor" if app else
                                                           "ppg --help") + "; npx and wrangler if installed"),
              'exec "${SHELL:-/bin/sh}" -i', ""]
    return "\n".join(lines)


def _posix_wrappers(where: Path, app: str) -> dict[str, str]:
    q = shlex.quote
    return {str(where / "bin" / "ppg-run"): f"#!/bin/sh\nexec {q(app)} --run \"$@\"\n",
            str(where / "bin" / "ppg-doctor"): f"#!/bin/sh\nexec {q(app)} --doctor \"$@\"\n"}


def _windows_files(where: Path, folder: str, first: list[str], last: list[str], app: str | None) -> dict[str, str]:
    def esc(text: str) -> str:
        return text.replace("%", "%%")

    path = ";".join([esc(d) for d in first] + ["%PATH%"] + [esc(d) for d in last])
    lines = ["@echo off", f'set "PATH={path}"']
    if app:
        lines.append(f'set "PPG_APP={esc(app)}"')
    lines += [f'cd /d "{esc(folder)}"',
              "echo PyArchInit Photogrammetry: " + ("ppg-run script.py, ppg-doctor" if app else "ppg --help") +
              "; npx and wrangler if installed"]
    files = {str(where / "ppg-terminal.cmd"): "\r\n".join(lines) + "\r\n"}
    if app:  # the application has no console: its output goes to a file, shown when it ends
        out = r"%TEMP%\ppg-run-output.txt"
        for name, flag in (("ppg-run", "--run"), ("ppg-doctor", "--doctor")):
            files[str(where / "bin" / f"{name}.cmd")] = "\r\n".join(
                ["@echo off", f'"{esc(app)}" {flag} %* > "{out}" 2>&1', "set CODE=%ERRORLEVEL%", f'type "{out}"',
                 "exit /b %CODE%", ""])
    return files


def build(platform: str, folder: str | os.PathLike, env: Mapping[str, str] | None = None,
          tools: list[str] | None = None, extra: list[str] | None = None, app: str | None = None,
          where: str | os.PathLike | None = None, which: Callable[[str], str | None] = shutil.which) -> Launch:
    """The command that opens the system terminal of ``platform`` (``sys.platform``: darwin, win32, linux...) in
    ``folder``: ``tools`` first on the PATH (default :func:`tool_dirs`), ``extra`` after it (default
    :func:`..io.site_publish.extra_dirs`), ``app`` the executable of the packaged application (``ppg-run`` and
    ``ppg-doctor`` for it), ``where`` the folder of the generated files (default :func:`script_folder`), ``which``
    finds the terminals. Nothing is written or started."""
    folder = str(folder)
    env = dict(os.environ if env is None else env)
    if tools is None:
        tools = tool_dirs()
    if extra is None:
        from .io.site_publish import extra_dirs

        extra = extra_dirs()
    where = Path(where) if where is not None else script_folder()
    first = list(tools) + ([str(where / "bin")] if app else [])
    sep = ";" if platform == "win32" else ":"
    parts = [p for p in env.get("PATH", "").split(sep) if p]
    last = [d for d in extra if d not in parts and d not in first]
    env["PATH"] = sep.join(first + [p for p in parts if p not in first] + last)
    if app:
        env["PPG_APP"] = app
    if platform == "win32":
        files = _windows_files(where, folder, first, last, app)
        script = str(where / "ppg-terminal.cmd")
        wt = which("wt.exe") or which("wt")
        if wt:
            return Launch([wt, "-d", folder, "cmd.exe", "/k", script], folder, env, files)
        cmd = env.get("COMSPEC") or which("cmd.exe") or "cmd.exe"
        return Launch([cmd, "/k", script], folder, env, files, creationflags=CREATE_NEW_CONSOLE)
    files = _posix_wrappers(where, app) if app else {}
    if platform == "darwin":
        script = str(where / "ppg-terminal.command")
        files[script] = _posix_script(folder, first, last, app)
        return Launch([which("open") or "/usr/bin/open", "-a", "Terminal", script], folder, env, files)
    script = str(where / "ppg-terminal.sh")
    files[script] = _posix_script(folder, first, last, app)
    chosen = env.get("TERMINAL")
    candidates = ([(chosen, lambda t, d, s: [t, "-e", s])] if chosen else []) + LINUX_TERMINALS
    for name, make in candidates:
        found = which(name)
        if found:
            return Launch(make(found, folder, script), folder, env, files)
    return Launch([], folder, env, {}, problem="no_terminal")


def open_terminal(launch: Launch) -> subprocess.Popen:
    """Write the files of ``launch`` (executable) and start its command; raise :class:`FileNotFoundError` when it
    has none (``launch.problem``)."""
    if not launch.argv:
        raise FileNotFoundError(launch.problem or "no terminal")
    for path, text in launch.files.items():
        p = Path(path)
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="")
        p.chmod(0o755)
    if sys.platform == "win32":
        return subprocess.Popen(launch.argv, cwd=launch.cwd, env=launch.env, creationflags=launch.creationflags,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # posix_spawn: absolute executable, close_fds=False, no cwd (see the notes of the project on fork); the
    # script goes to the folder by itself
    return subprocess.Popen(launch.argv, env=launch.env, close_fds=False, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def project_folder(document=None) -> Path:
    """The folder of the project open, the home folder without one."""
    path = getattr(document, "path", None)
    if path:
        return Path(path).resolve().parent
    return Path.home()


def for_this_program(folder: str | os.PathLike) -> Launch:
    """:func:`build` for this system and this installation (the packaged application or a Python one)."""
    frozen = bool(getattr(sys, "frozen", False))
    return build(sys.platform, folder, app=sys.executable if frozen else None,
                 tools=tool_dirs(frozen))


__all__ = ["Launch", "build", "open_terminal", "tool_dirs", "script_folder", "project_folder", "for_this_program",
           "LINUX_TERMINALS"]
