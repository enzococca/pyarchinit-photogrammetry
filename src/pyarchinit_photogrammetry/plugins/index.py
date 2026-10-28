"""The plugin index: the list of plugins kept in the repository
https://github.com/enzococca/pyarchinit-photogrammetry-plugins (``index.json``), and the
installation of one of them into the plugin folder of the program.

An entry gives a pure-Python wheel (``-py3-none-any.whl``) by URL with its SHA-256, the plugin API
it was written for, the oldest version of the program it works with and the modules it needs
beyond those of the program. Installing downloads the wheel, checks the hash, unpacks it into
``<plugins>/<name>/`` (in place of an older version) and leaves the activation for the next start.
A newer version can also be *staged* in ``<plugins>/.updates/<name>/`` (:mod:`.updates` puts it in
place at the next start). ``PPG_PLUGIN_INDEX`` points to another index (a file URL in the tests).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import tempfile
import urllib.error
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .api import API_VERSION
from .manager import GROUP, plugins_dir

log = logging.getLogger(__name__)
INDEX_URL = "https://raw.githubusercontent.com/enzococca/pyarchinit-photogrammetry-plugins/main/index.json"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
STAGING = ".updates"  # in the plugin folder: the updates downloaded, put in place at the next start
SWAPPING = "swapping"  # in a work folder: the name of the plugin moved aside into "previous", until the swap is done


class PluginError(Exception):
    """A plugin that cannot be listed or installed; the message says why."""


@dataclass
class Entry:
    """A plugin of the index."""

    name: str
    version: str
    wheel: str
    sha256: str
    title: str = ""
    description: str = ""
    author: str = ""
    homepage: str = ""
    license: str = ""
    api: int = API_VERSION
    min_app: str = "0"
    requires: list = field(default_factory=list)
    i18n: dict = field(default_factory=dict)  # {"it": {"title": ..., "description": ...}}

    @classmethod
    def from_dict(cls, d: dict) -> Entry:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        entry = cls(**known)
        entry.requires = list(entry.requires or [])
        entry.api = int(entry.api)
        entry.i18n = dict(entry.i18n) if isinstance(entry.i18n, dict) else {}
        return entry

    def localized(self, name: str, language: str | None = None) -> str:
        """The ``title`` or the ``description`` in ``language`` (that of the interface by default)
        when the index gives it, else the plain one."""
        if language is None:
            try:
                from ..gui.i18n import current

                language = current()
            except Exception:  # the program without its interface
                language = ""
        texts = self.i18n.get(language)
        text = texts.get(name) if isinstance(texts, dict) else None
        return text if isinstance(text, str) and text else getattr(self, name)

    def problem(self, app_version: str | None = None) -> str:
        """Why this program cannot install the plugin ("" when it can)."""
        from .. import __version__
        from ..updates import version_key

        if not NAME.match(self.name):
            return f"invalid plugin name {self.name!r}"
        if not self.wheel.endswith("-py3-none-any.whl"):
            return "not a pure-Python wheel (-py3-none-any.whl): it could not run in the packaged application"
        if self.api > API_VERSION:
            return f"written for the plugin API {self.api}, this program has {API_VERSION}: update the program"
        try:
            if version_key(app_version or __version__) < version_key(self.min_app):
                return f"needs pyarchinit-photogrammetry {self.min_app} or later"
        except ValueError:
            pass
        missing = [m for m in self.requires if importlib.util.find_spec(m) is None]
        if missing:
            return "needs modules this program does not have: " + ", ".join(missing)
        return ""


def _open(url: str, timeout: float):
    from ..updates import _open as open_url

    return open_url(url, timeout, accept="application/json, application/octet-stream, */*")


def fetch(url: str | None = None, timeout: float = 15.0) -> list[Entry]:
    """The plugins of the index (``PPG_PLUGIN_INDEX`` or the repository of the plugins)."""
    url = url or os.environ.get("PPG_PLUGIN_INDEX") or INDEX_URL
    try:
        with _open(url, timeout) as r:
            data = json.load(r)
    except (urllib.error.URLError, OSError) as exc:
        raise PluginError(f"the plugin index could not be read: {getattr(exc, 'reason', exc)}") from exc
    except ValueError as exc:
        raise PluginError(f"the plugin index is not valid JSON: {exc}") from exc
    entries = []
    for d in (data.get("plugins", []) if isinstance(data, dict) else []):
        try:
            entries.append(Entry.from_dict(d))
        except (TypeError, ValueError):
            continue  # an entry this version does not understand
    return entries


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    for member in archive.namelist():
        p = PurePosixPath(member)
        if p.is_absolute() or ".." in p.parts:
            raise PluginError(f"the wheel holds an unsafe path: {member}")
    archive.extractall(target)


def _forget(marker: Path) -> None:
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass


def swap_folder(new: Path, target: Path, work: Path) -> None:
    """Put the complete folder ``new`` in place of ``target`` by renames only: the old ``target`` goes into
    ``work`` first and comes back if ``new`` cannot be put in place (for whatever reason, Ctrl+C included),
    so a plugin is never missing or half written. If it cannot come back either, ``work`` keeps it - see
    :func:`interrupted_swap` - and the next start puts it back. The caller deletes ``work`` with
    :func:`discard_work`."""
    old = work / "previous"
    marker = work / SWAPPING  # says where the plugin in "previous" belongs, for as long as it is there
    try:
        if target.exists():
            marker.write_text(os.path.relpath(target, work.parent))
            target.rename(old)
        new.rename(target)
    except BaseException:
        back = True
        if old.exists() and not target.exists():  # moved aside, and nothing has taken its place
            try:
                old.rename(target)
            except OSError as exc:
                back = False
                log.error("%s could not be put back from %s: %s", target, old, exc)
        if back:
            _forget(marker)
        raise
    _forget(marker)
    importlib.invalidate_caches()  # the import system and importlib.metadata listed the folder that was there


def interrupted_swap(work: Path) -> tuple[Path, Path] | None:
    """``(old folder, plugin folder)`` when ``work`` holds the only copy of an installed plugin: a swap moved
    it aside and could not put it back (it was cut short, or the rename back failed) and the plugin folder is
    missing. None for any other folder: a swap that finished, or one of a staged update (that is downloaded
    again)."""
    try:
        name = (work / SWAPPING).read_text().strip()
    except (OSError, ValueError):
        return None
    previous = work / "previous"
    if not name or name in (".", "..") or Path(name).name != name or not previous.is_dir():
        return None
    target = work.parent / name
    return None if target.exists() else (previous, target)


def discard_work(work: Path) -> None:
    """Delete a work folder of :func:`install` or :func:`.updates.apply_staged` - unless it holds the only copy
    of a plugin (:func:`interrupted_swap`): then it stays, with a warning, and the next start puts the plugin back."""
    held = interrupted_swap(work)
    if held is not None:
        log.warning("plugin %s is kept in %s: it is put back at the next start", held[1].name, work)
        return
    shutil.rmtree(work, ignore_errors=True)


def drop_staged(folder: Path, name: str) -> None:
    """Delete the update staged for ``name`` all at once: it is renamed out of ``.updates`` first, so what is
    left of it, should the deletion stop half way, is never found as an update to apply."""
    staged = folder / STAGING / name
    if not staged.exists():
        return
    trash = staged.parent / f".trash-{name}-{os.urandom(4).hex()}"
    try:
        staged.rename(trash)
    except OSError as exc:
        log.warning("the update staged for %s could not be dropped: %s", name, exc)
        return
    shutil.rmtree(trash, ignore_errors=True)


def install(entry: Entry, folder: str | os.PathLike | None = None, progress=None, timeout: float = 60.0, *,
            staging: bool = False) -> Path:
    """Download, check and unpack ``entry`` into the plugin folder; returns its folder. Active
    from the next start of the program. An older version is replaced by renames (:func:`swap_folder`),
    so a failure leaves it as it was; an update staged for it is dropped: a plugin installed by hand
    wins over it.

    With ``staging`` the plugin is unpacked into ``<folder>/.updates/<name>/`` instead (replacing an
    older staged one) and the one installed is not touched: :func:`.updates.apply_staged` puts it in
    place at the next start. Whatever goes wrong - the download, the archive, the disk - is a
    :class:`PluginError`."""
    problem = entry.problem()
    if problem:
        raise PluginError(f"{entry.name}: {problem}")
    folder = Path(folder) if folder is not None else plugins_dir()
    try:
        return _install(entry, folder, progress, timeout, staging)
    except OSError as exc:  # unpacking, putting in place: the dialog and the command show a PluginError
        raise PluginError(f"{entry.name}: could not be put in place: {exc}") from exc


def _install(entry: Entry, folder: Path, progress, timeout: float, staging: bool) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".install-{entry.name}-", dir=folder))
    try:
        wheel = work / PurePosixPath(entry.wheel).name
        digest = hashlib.sha256()
        try:
            with _open(entry.wheel, timeout) as r, open(wheel, "wb") as out:
                total = int(r.headers.get("Content-Length") or 0) if hasattr(r, "headers") and r.headers else 0
                done = 0
                while chunk := r.read(1 << 16):
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress is not None and total:
                        progress(min(0.9, 0.9 * done / total), f"{entry.name}: download")
        except (urllib.error.URLError, OSError) as exc:
            raise PluginError(f"{entry.name}: the wheel could not be downloaded: {getattr(exc, 'reason', exc)}") from exc
        if digest.hexdigest().lower() != entry.sha256.lower():
            raise PluginError(f"{entry.name}: the wheel does not match its SHA-256 in the index")
        unpacked = work / "unpacked"
        try:
            with zipfile.ZipFile(wheel) as z:
                _safe_extract(z, unpacked)
        except zipfile.BadZipFile as exc:
            raise PluginError(f"{entry.name}: the wheel is not a valid archive") from exc
        import importlib.metadata

        names = {ep.name for dist in importlib.metadata.distributions(path=[str(unpacked)])
                 for ep in dist.entry_points.select(group=GROUP)}
        if entry.name not in names:
            raise PluginError(f"{entry.name}: the wheel declares no plugin {entry.name!r} "
                              f"(entry point group {GROUP})")
        target = folder / STAGING / entry.name if staging else folder / entry.name
        target.parent.mkdir(parents=True, exist_ok=True)
        swap_folder(unpacked, target, work)
        if not staging:
            drop_staged(folder, entry.name)
        if progress is not None:
            progress(1.0, f"{entry.name} {entry.version} {'staged' if staging else 'installed'}")
        return target
    finally:
        discard_work(work)
