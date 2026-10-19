"""New versions of the application, from the GitHub releases of the project (the same versions
that are published on PyPI).

:func:`check` asks GitHub for the latest release: one HTTPS request that carries the version in
its User-Agent and nothing else (the interface makes it at most once a day, and it can be
switched off). :func:`download` fetches the installer of this platform (the dmg on macOS, the
zip on Windows). On macOS :func:`prepare_macos_update` copies the application out of the dmg
and :func:`finish_macos_update` swaps it in once the running copy has quit, then starts it.
Python installations are updated with pip (:data:`PIP_COMMAND`), source checkouts with git.
"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .core.progress import Cancelled, ProgressCallback, Progress, as_progress

REPO = "enzococca/pyarchinit-photogrammetry"
# PPG_UPDATE_URL: another answer in the format of the GitHub API (tests of the packaged application, mirrors)
LATEST_URL = os.environ.get("PPG_UPDATE_URL") or f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
PIP_COMMAND = "pip install --upgrade pyarchinit-photogrammetry"
TIMEOUT = 10.0
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
              "/Support/lsregister")

_PRE = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "pre": 2, "preview": 2, "c": 3, "rc": 3}


class UpdateError(RuntimeError):
    """A check, download or installation that did not succeed. ``kind``: ``offline`` (GitHub not
    reachable), ``limit`` (too many requests from this network), ``missing`` (nothing published),
    ``http``, ``incomplete`` (download cut short), ``install``."""

    def __init__(self, message: str, kind: str = "http"):
        super().__init__(message)
        self.kind = kind


@dataclass
class Release:
    version: str
    tag: str
    url: str                 # page of the release
    notes: str = ""          # Markdown
    published: str = ""      # ISO date and time
    assets: list[dict] = field(default_factory=list)  # name, url, size

    @classmethod
    def from_github(cls, data: dict) -> "Release":
        tag = str(data.get("tag_name") or "")
        assets = [{"name": str(a.get("name") or ""), "url": str(a.get("browser_download_url") or ""),
                   "size": int(a.get("size") or 0)} for a in data.get("assets") or []]
        return cls(version=tag.lstrip("vV"), tag=tag, url=str(data.get("html_url") or RELEASES_PAGE),
                   notes=str(data.get("body") or ""), published=str(data.get("published_at") or ""),
                   assets=assets)


def version_key(text: str) -> tuple:
    """Sortable key of a version: 0.6.1 < 0.7.0.dev1 < 0.7.0a1 < 0.7.0b1 < 0.7.0rc1 < 0.7.0 (a
    leading ``v`` and a ``+local`` part are ignored). ``ValueError`` if ``text`` is not a version."""
    m = re.fullmatch(r"\s*[vV]?(\d+(?:\.\d+)*)(?:[-_.]?([A-Za-z]+)[-_.]?(\d*))?(?:\+[\w.]*)?\s*", text or "")
    if not m or (m.group(2) and m.group(2).lower() not in _PRE):
        raise ValueError(f"not a version: {text!r}")
    numbers = ([int(x) for x in m.group(1).split(".")] + [0, 0, 0, 0])[:4]
    pre = (_PRE[m.group(2).lower()], int(m.group(3) or 0)) if m.group(2) else (9, 0)
    return (*numbers, *pre)


def is_newer(candidate: str, current: str = __version__) -> bool:
    """``candidate`` is a later version than ``current`` (False when either is not a version)."""
    try:
        return version_key(candidate) > version_key(current)
    except ValueError:
        return False


def _ssl_context():
    """Certificates of certifi when present (a packaged application may not find those of the
    Python it was built with), else the system ones."""
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _open(url: str, timeout: float, accept: str = "application/vnd.github+json"):
    request = urllib.request.Request(url, headers={"User-Agent": f"pyarchinit-photogrammetry/{__version__}",
                                                   "Accept": accept})
    return urllib.request.urlopen(request, timeout=timeout, context=_ssl_context())


def _network_error(exc: Exception) -> UpdateError:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429):
            return UpdateError("GitHub refused the request: too many requests from this network, try again later",
                               "limit")
        if exc.code == 404:
            return UpdateError("no release found", "missing")
        return UpdateError(f"GitHub answered HTTP {exc.code}", "http")
    return UpdateError(f"GitHub could not be reached: {getattr(exc, 'reason', exc)}", "offline")


def latest_release(url: str = LATEST_URL, timeout: float = TIMEOUT) -> Release:
    """The latest published release (GitHub leaves drafts and pre-releases out)."""
    try:
        with _open(url, timeout) as r:
            data = json.load(r)
    except (urllib.error.URLError, OSError) as exc:  # HTTPError is an URLError; timeouts are OSError
        raise _network_error(exc) from exc
    except ValueError as exc:
        raise UpdateError(f"unexpected answer from GitHub: {exc}", "http") from exc
    release = Release.from_github(data if isinstance(data, dict) else {})
    try:
        version_key(release.version)
    except ValueError as exc:
        raise UpdateError(f"unexpected release tag {release.tag!r}", "http") from exc
    return release


def check(current: str = __version__, skip: str | None = None, url: str = LATEST_URL,
          timeout: float = TIMEOUT) -> Release | None:
    """The latest release when it is newer than ``current`` and is not ``skip`` (a version the
    user chose to skip); ``None`` otherwise. Raises :class:`UpdateError`."""
    release = latest_release(url, timeout)
    if not is_newer(release.version, current):
        return None
    if skip:
        try:
            if version_key(skip) == version_key(release.version):
                return None
        except ValueError:
            pass
    return release


def app_bundle(executable: str | os.PathLike | None = None) -> Path | None:
    """The ``.app`` folder of the running application on macOS, ``None`` outside one."""
    exe = Path(executable or sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def install_kind() -> str:
    """How this copy is installed: ``macos-app`` and ``windows-app`` (the packaged applications),
    ``source`` (a git checkout) or ``pip``."""
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin" and app_bundle() is not None:
            return "macos-app"
        if os.name == "nt":
            return "windows-app"
    if (Path(__file__).resolve().parents[2] / ".git").exists():
        return "source"
    return "pip"


def platform_tag(system: str | None = None, machine: str | None = None) -> str | None:
    """The part of the installer names that identifies a platform (``macos-arm64``,
    ``windows-x64``), ``None`` where no installer is published (Linux: pip)."""
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine, machine)
    if system == "Darwin":
        return f"macos-{arch}"
    if system == "Windows":
        return f"windows-{arch}"
    return None


def asset_for_platform(release: Release, system: str | None = None, machine: str | None = None) -> dict | None:
    """The installer of ``release`` for this platform (or the one given), ``None`` if missing."""
    tag = platform_tag(system, machine)
    if tag is None:
        return None
    ext = ".dmg" if tag.startswith("macos") else ".zip"
    for asset in release.assets:
        name = asset["name"].lower()
        if tag in name and name.endswith(ext):
            return asset
    return None


def default_download_folder() -> Path:
    folder = Path.home() / "Downloads"
    return folder if folder.is_dir() else Path(tempfile.gettempdir())


def download(asset: dict, folder: str | os.PathLike | None = None,
             progress: ProgressCallback | Progress | None = None, timeout: float = 60.0) -> Path:
    """Download ``asset`` (one of :attr:`Release.assets`) into ``folder`` (the Downloads folder by
    default) and return the file. It is written as ``<name>.part`` and renamed only once complete
    (size checked), so a cancelled or broken download never looks finished."""
    prog = as_progress(progress)
    folder = Path(folder) if folder is not None else default_download_folder()
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(asset["name"]).name  # a bare file name, whatever the server sent
    out = folder / name
    part = out.with_name(name + ".part")
    expected = int(asset.get("size") or 0)
    done = 0
    complete = False
    try:
        with _open(asset["url"], timeout, accept="application/octet-stream") as r, open(part, "wb") as f:
            total = expected or int(r.headers.get("Content-Length") or 0)
            while True:
                prog.check()
                block = r.read(1 << 20)
                if not block:
                    break
                f.write(block)
                done += len(block)
                if total:
                    prog(done / total, f"Downloading {name}: {done / 1e6:.0f} of {total / 1e6:.0f} MB")
        if expected and done != expected:
            raise UpdateError(f"download incomplete: {done} of {expected} bytes", "incomplete")
        os.replace(part, out)
        complete = True
    except (urllib.error.URLError, OSError) as exc:
        raise _network_error(exc) from exc
    finally:
        if not complete:
            part.unlink(missing_ok=True)
    prog(1.0, f"Downloaded {out}")
    return out


def _run(cmd: list[str], timeout: float = 900.0) -> subprocess.CompletedProcess:
    # absolute program, close_fds=False and no cwd: subprocess uses posix_spawn (no fork, see _env)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, close_fds=False)


def bundle_version(app: str | os.PathLike) -> str | None:
    """``CFBundleShortVersionString`` of a macOS application bundle."""
    try:
        with open(Path(app) / "Contents" / "Info.plist", "rb") as f:
            info = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return None
    value = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
    return str(value) if value else None


def staging_folder() -> Path:
    return Path.home() / "Library" / "Caches" / "pyarchinit-photogrammetry" / "update"


def prepare_macos_update(dmg: str | os.PathLike, target: str | os.PathLike | None = None,
                         expected_version: str | None = None,
                         progress: ProgressCallback | Progress | None = None) -> Path:
    """Copy the application of ``dmg`` next to the installation (in :func:`staging_folder`), ready
    for :func:`finish_macos_update`; ``target`` (the running ``.app`` by default) is not touched.
    ``UpdateError(kind="install")`` when ``target`` cannot be replaced (a folder the user cannot
    write) or the dmg does not hold the expected version."""
    prog = as_progress(progress)
    target = Path(target) if target is not None else app_bundle()
    if target is None:
        raise UpdateError("not running from an application bundle", "install")
    if not os.access(target.parent, os.W_OK) or (target.exists() and not os.access(target, os.W_OK)):
        raise UpdateError(f"{target.parent} cannot be written by this user", "install")
    staging = staging_folder()
    staging.mkdir(parents=True, exist_ok=True)
    new = staging / target.name
    mount = Path(tempfile.mkdtemp(prefix="ppg-update-"))
    prog(0.05, "Opening the disk image")
    r = _run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount),
              str(dmg)])
    if r.returncode != 0:
        shutil.rmtree(mount, ignore_errors=True)
        raise UpdateError(f"the disk image could not be opened: {(r.stderr or r.stdout).strip()}", "install")
    try:
        apps = sorted(p for p in mount.glob("*.app") if p.is_dir())
        if not apps:
            raise UpdateError("the disk image holds no application", "install")
        found = bundle_version(apps[0])
        if expected_version and found:
            try:
                same = version_key(found) == version_key(expected_version)
            except ValueError:
                same = False
            if not same:
                raise UpdateError(f"the disk image holds version {found}, not {expected_version}", "install")
        prog(0.2, "Copying the new version")
        shutil.rmtree(new, ignore_errors=True)
        r = _run(["/usr/bin/ditto", str(apps[0]), str(new)])
        if r.returncode != 0:
            shutil.rmtree(new, ignore_errors=True)
            raise UpdateError(f"the application could not be copied: {(r.stderr or r.stdout).strip()}", "install")
    finally:
        if _run(["/usr/bin/hdiutil", "detach", "-quiet", str(mount)], timeout=120).returncode != 0:
            _run(["/usr/bin/hdiutil", "detach", "-force", "-quiet", str(mount)], timeout=120)
        shutil.rmtree(mount, ignore_errors=True)
    prog(1.0, "New version ready")
    return new


_SWAP_SCRIPT = r"""#!/bin/bash
# Installs a downloaded update of PyArchInit Photogrammetry once the running copy has quit.
trap '' HUP
pid="$1"; new="$2"; target="$3"; old="$4"; relaunch="$5"
for _ in $(seq 1 3000); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
rm -rf "$old"
if [ -e "$target" ]; then
  if mv "$target" "$old"; then
    if mv "$new" "$target"; then rm -rf "$old"; else mv "$old" "$target"; fi
  fi
else
  mv "$new" "$target"
fi
if [ -x "{lsregister}" ]; then "{lsregister}" -u "$new" "$old" >/dev/null 2>&1; fi
if [ "$relaunch" = "1" ]; then /usr/bin/open "$target"; fi
rm -f "$0"
"""


def finish_macos_update(new: str | os.PathLike, target: str | os.PathLike | None = None,
                        pid: int | None = None, relaunch: bool = True) -> subprocess.Popen:
    """Start the helper that waits for process ``pid`` (this one) to end, puts ``new`` in place of
    ``target`` (keeping the old copy until the new one is in) and opens it when ``relaunch``.
    Called just before quitting: a running application must not be replaced under itself (it still
    loads its modules from the bundle)."""
    target = Path(target) if target is not None else app_bundle()
    if target is None:
        raise UpdateError("not running from an application bundle", "install")
    new = Path(new)
    if not new.is_dir():
        raise UpdateError(f"{new} is missing", "install")
    old = target.with_name(f".{target.stem}.old.app")
    script = Path(tempfile.mkstemp(prefix="ppg-update-", suffix=".sh")[1])
    script.write_text(_SWAP_SCRIPT.replace("{lsregister}", LSREGISTER))
    script.chmod(0o700)
    return subprocess.Popen(["/bin/bash", str(script), str(pid or os.getpid()), str(new), str(target), str(old),
                             "1" if relaunch else "0"], close_fds=False, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def discard_prepared_update() -> None:
    """Remove a new version copied by :func:`prepare_macos_update` and never installed."""
    shutil.rmtree(staging_folder(), ignore_errors=True)


__all__ = ["Release", "UpdateError", "Cancelled", "check", "latest_release", "is_newer", "version_key",
           "asset_for_platform", "download", "install_kind", "app_bundle", "prepare_macos_update",
           "finish_macos_update", "PIP_COMMAND", "RELEASES_PAGE"]
