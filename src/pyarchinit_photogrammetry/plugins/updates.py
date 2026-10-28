"""Updates of the plugins installed by the program (those of the plugin folder; one installed with pip
is never touched).

:func:`check` compares them with the index of the plugin repository and *stages* each newer version this
program can run: :func:`.index.install` downloads, checks and unpacks it into ``<plugins>/.updates/<name>/``
and leaves the plugin in use as it is. :func:`apply_staged` puts it in place at the next start
(:meth:`.Manager.load`, before any plugin is imported), so a running session never meets new modules of a
plugin among its old ones. :func:`run` is the whole check, for the window and for ``ppg plugins update``.
"""
from __future__ import annotations

import importlib.metadata
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..updates import is_newer  # the program's own updates module
from . import index
from .manager import GROUP, Manager, PluginInfo, plugins_dir

log = logging.getLogger(__name__)


@dataclass
class PluginUpdates:
    """What a look at the index found."""

    updated: list[tuple[str, str, str]] = field(default_factory=list)  # name, installed version, staged version
    available: list[tuple[str, str, str]] = field(default_factory=list)  # as updated: not staged (update=False)
    failed: list[tuple[str, str]] = field(default_factory=list)  # name, why
    waiting: list[tuple[str, str, str]] = field(default_factory=list)  # name, version of the index, what it needs
    new: list[index.Entry] = field(default_factory=list)  # in the index, not installed, not seen before
    seen: list[str] = field(default_factory=list)  # the names seen so far, sorted

    def __bool__(self) -> bool:
        """Something to tell (the names seen alone are not)."""
        return bool(self.updated or self.available or self.failed or self.waiting or self.new)


def _version_of(path: Path, name: str) -> str | None:
    """The version of the plugin ``name`` unpacked in ``path``, from its dist-info (None when it is not there)."""
    try:
        for dist in importlib.metadata.distributions(path=[str(path)]):
            if any(ep.name == name for ep in dist.entry_points.select(group=GROUP)):
                return dist.version or None
    except (KeyError, ValueError, OSError):
        pass
    return None


def _staged_folders(folder: Path) -> list[Path]:
    """``<folder>/.updates/<name>`` of each update staged (a dot in front: one being applied, not these)."""
    root = folder / index.STAGING
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))


def staged(folder: str | os.PathLike) -> dict[str, str]:
    """The updates waiting for the next start: plugin name -> staged version."""
    found: dict[str, str] = {}
    for sub in _staged_folders(Path(folder)):
        version = _version_of(sub, sub.name)
        if version:
            found[sub.name] = version
    return found


def status(entry: index.Entry | None, info: PluginInfo, ready: dict[str, str], app_version: str | None = None) -> str:
    """What the index says of the installed plugin ``info``: ``""`` (nothing to do: not installed by the
    program, not in the index, or not newer), ``"waiting"`` (the newer version needs a newer program),
    ``"staged"`` (downloaded already, in ``ready``) or ``"available"``."""
    if info.source != "folder" or entry is None or not is_newer(entry.version, info.version):
        return ""
    if entry.problem(app_version):
        return "waiting"
    return "staged" if ready.get(info.name) == entry.version else "available"


def check(entries: list[index.Entry], installed: list[PluginInfo], seen: list[str] | None, folder: str | os.PathLike, *,
          app_version: str | None = None, update: bool = True, install=index.install, progress=None) -> PluginUpdates:
    """Compare the plugins installed by the program with the ``entries`` of the index. A newer version
    this program can run is staged (``update``; else it is only listed in ``available``), one that needs
    a newer program is listed in ``waiting``, one that cannot be downloaded in ``failed``. ``new`` holds
    the entries neither installed nor in ``seen`` (nothing at the first look, ``seen`` None); the
    result's ``seen`` adds the names of the index."""
    folder = Path(folder)
    result = PluginUpdates()
    by_name = {e.name: e for e in entries}
    ready = staged(folder)
    for info in installed:
        entry = by_name.get(info.name)
        what = status(entry, info, ready, app_version)
        if what == "waiting":
            result.waiting.append((info.name, entry.version, entry.problem(app_version)))
        elif what == "available" and not update:
            result.available.append((info.name, info.version, entry.version))
        elif what == "available":
            try:
                install(entry, folder, progress=progress, staging=True)
            except (index.PluginError, OSError) as exc:
                result.failed.append((info.name, str(exc)))
            else:
                result.updated.append((info.name, info.version, entry.version))
    before = set(seen or ())
    if seen is not None:
        known = {i.name for i in installed}
        result.new = [e for e in entries if e.name not in known and e.name not in before]
    result.seen = sorted(before | set(by_name))
    return result


def apply_staged(folder: str | os.PathLike) -> list[tuple[str, str, str]]:
    """Put in place the updates staged in ``<folder>/.updates/``; returns (name, old version, new version)
    of those applied. Each is claimed first by renaming it, so of two programs starting together only one
    gets it. One whose plugin is no longer installed, or is not older than the staged version, is dropped:
    an update never brings back a plugin that was removed nor replaces one installed by hand later. One
    that cannot be put in place stays staged for the next start."""
    folder = Path(folder)
    applied: list[tuple[str, str, str]] = []
    for sub in _staged_folders(folder):
        name = sub.name
        claim = sub.parent / f".applying-{name}-{os.getpid()}"
        try:
            sub.rename(claim)
        except OSError:
            continue  # another program starting now has it
        installed = folder / name
        old = _version_of(installed, name) if installed.is_dir() else None
        new = _version_of(claim, name)
        if old is None or new is None or not is_newer(new, old):
            log.info("plugin %s: the staged update to %s is dropped (installed: %s)", name, new, old or "none")
            shutil.rmtree(claim, ignore_errors=True)
            continue
        try:
            with tempfile.TemporaryDirectory(prefix=f".install-{name}-", dir=folder,
                                             ignore_cleanup_errors=True) as work:
                index.swap_folder(claim, installed, Path(work))
        except OSError as exc:
            log.warning("plugin %s: the update to %s could not be put in place: %s", name, new, exc)
            try:
                claim.rename(sub)  # staged again: the next start tries once more
            except OSError:
                pass
            continue
        log.info("plugin %s updated from %s to %s", name, old, new)
        applied.append((name, old, new))
    return applied


def run(seen: list[str] | None = None, *, folder: str | os.PathLike | None = None, update: bool = True,
        url: str | None = None, progress=None) -> PluginUpdates:
    """Read the index (``url``, else the usual one) and :func:`check` the plugins of ``folder`` against it.
    A :class:`.index.PluginError` (the index cannot be read) goes to the caller."""
    entries = index.fetch(url)
    folder = Path(folder) if folder is not None else plugins_dir()
    return check(entries, Manager(folder).discover(), seen, folder, update=update, progress=progress)
