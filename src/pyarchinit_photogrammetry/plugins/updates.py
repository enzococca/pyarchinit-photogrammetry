"""Updates of the plugins installed by the program (those of the plugin folder; one installed with pip
is never touched).

:func:`check` compares them with the index of the plugin repository and *stages* each newer version this
program can run: :func:`.index.install` downloads, checks and unpacks it into ``<plugins>/.updates/<name>/``
and leaves the plugin in use as it is. :func:`apply_staged` puts it in place at the next start
(:meth:`.Manager.load`, before any plugin is imported), so the session that starts never meets new modules of a
plugin among its old ones; any copy of the program that starts does it, also one started while a window is open
(the window keeps the modules it loaded and should be restarted). It also puts back a plugin an earlier swap left
in a work folder and deletes what a crashed run left behind. :func:`run` is the whole check, for the window and
for ``ppg plugins update``.
"""
from __future__ import annotations

import importlib.metadata
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..updates import is_newer, version_key  # the program's own updates module
from . import index
from .manager import GROUP, Manager, PluginInfo, plugins_dir

log = logging.getLogger(__name__)
ABANDONED = 24 * 3600  # seconds: what a crashed run left in the plugin folder is deleted after this long


@dataclass
class PluginUpdates:
    """What a look at the index found."""

    updated: list[tuple[str, str, str]] = field(default_factory=list)  # name, installed version, staged version
    available: list[tuple[str, str, str]] = field(default_factory=list)  # as updated: not staged (update=False)
    failed: list[tuple[str, str]] = field(default_factory=list)  # name, why
    waiting: list[tuple[str, str, str]] = field(default_factory=list)  # name, version of the index, what it needs
    new: list[index.Entry] = field(default_factory=list)  # in the index, not installed, not seen before
    seen: list[str] = field(default_factory=list)  # the names seen so far, sorted
    pending: list[tuple[str, str, str]] = field(default_factory=list)  # as updated: staged by an earlier look

    def __bool__(self) -> bool:
        """Something to tell. Not the names seen, nor ``pending``: those were told when they were staged."""
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


def _same_version(a: str | None, b: str) -> bool:
    """``a`` and ``b`` are the same version ("0.2" and "0.2.0" are); strings that are not versions, if equal."""
    if a is None:
        return False
    try:
        return version_key(a) == version_key(b)
    except ValueError:
        return a == b


def _same_path(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


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


def status(entry: index.Entry | None, info: PluginInfo, ready: dict[str, str], folder: str | os.PathLike,
           app_version: str | None = None) -> tuple[str, str]:
    """What the index says of the installed plugin ``info``, as ``(state, why)``: ``("", "")`` (nothing to do:
    not installed by the program, not in the index, or not newer), ``("waiting", reason)`` (the newer version
    needs a newer program, or the plugin is not in ``<folder>/<name>``, where an update would be put),
    ``("staged", "")`` (downloaded already: it is in ``ready``) or ``("available", "")``."""
    if info.source != "folder" or entry is None or not is_newer(entry.version, info.version):
        return "", ""
    problem = entry.problem(app_version)
    if problem:
        return "waiting", problem
    where = Path(folder) / info.name
    if info.location is not None and not _same_path(info.location, where):
        return "waiting", f"installed in {info.location}, not in {where}: update it by hand"
    return ("staged" if _same_version(ready.get(info.name), entry.version) else "available"), ""


def check(entries: list[index.Entry], installed: list[PluginInfo], seen: list[str] | None, folder: str | os.PathLike, *,
          app_version: str | None = None, update: bool = True, install=index.install, progress=None) -> PluginUpdates:
    """Compare the plugins installed by the program with the ``entries`` of the index. A newer version
    this program can run is staged (``update``; else it is only listed in ``available``), one that needs
    a newer program is listed in ``waiting``, one that cannot be downloaded in ``failed``, one staged
    earlier in ``pending``. ``new`` holds the entries neither installed nor in ``seen`` (nothing at the
    first look, ``seen`` None); the result's ``seen`` adds the names of the index."""
    folder = Path(folder)
    result = PluginUpdates()
    by_name = {e.name: e for e in entries}
    ready = staged(folder)
    for info in installed:
        entry = by_name.get(info.name)
        what, why = status(entry, info, ready, folder, app_version)
        if what == "waiting":
            result.waiting.append((info.name, entry.version, why))
        elif what == "staged":
            result.pending.append((info.name, info.version, entry.version))
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


def _recover(folder: Path) -> None:
    """Put back the plugins a swap left in a work folder: moved aside, and neither the new version nor the old
    one could be put in place (the program was killed between the renames, or the rename back failed)."""
    for work in sorted(folder.glob(".install-*")):
        held = index.interrupted_swap(work)
        if held is None:
            continue
        previous, target = held
        try:
            previous.rename(target)
        except OSError as exc:
            log.warning("plugin %s could not be put back from %s: %s", target.name, work, exc)
            continue
        log.warning("plugin %s put back from %s: an update was cut short", target.name, work.name)
        index.discard_work(work)


def _sweep(folder: Path) -> None:
    """Delete what a crashed run left behind - claims and trash of ``.updates/``, work folders of the plugin
    folder - when it is older than a day, by modification time (process ids are reused, and telling whether one
    is running takes more than the standard library on Windows). A work folder that still keeps the only copy
    of a plugin stays."""
    root = folder / index.STAGING
    limit = time.time() - ABANDONED
    for path in [*folder.glob(".install-*"), *root.glob(".applying-*"), *root.glob(".trash-*")]:
        try:
            abandoned = path.is_dir() and path.stat().st_mtime < limit
        except OSError:
            continue
        if abandoned and index.interrupted_swap(path) is None:
            log.info("deleted %s, left by an earlier run", path.name)
            shutil.rmtree(path, ignore_errors=True)


def apply_staged(folder: str | os.PathLike) -> list[tuple[str, str, str]]:
    """Put in place the updates staged in ``<folder>/.updates/``; returns (name, old version, new version)
    of those applied. First a plugin an interrupted swap left in a work folder is put back, and what a crashed
    run left behind is deleted. Each update is claimed by renaming it, so of two programs starting together
    only one gets it. One whose plugin is no longer installed, or is not older than the staged version, is
    dropped: an update never brings back a plugin that was removed nor replaces one installed by hand later.
    One that cannot be put in place stays staged for the next start."""
    folder = Path(folder)
    _recover(folder)
    _sweep(folder)
    applied: list[tuple[str, str, str]] = []
    for sub in _staged_folders(folder):
        name = sub.name
        claim = sub.parent / f".applying-{name}-{os.getpid()}"
        try:
            os.utime(sub)  # a rename keeps the time of the unpacking: touched first, no sweep sees the claim as old
        except OSError:
            pass
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
        work = None
        try:
            work = Path(tempfile.mkdtemp(prefix=f".install-{name}-", dir=folder))
            index.swap_folder(claim, installed, work)
        except OSError as exc:
            log.warning("plugin %s: the update to %s could not be put in place: %s", name, new, exc)
            try:
                claim.rename(sub)  # staged again: the next start tries once more
            except OSError:
                pass
            continue
        finally:
            if work is not None:
                index.discard_work(work)
        log.debug("plugin %s updated from %s to %s", name, old, new)  # told by the window and `ppg plugin`: `applied`
        applied.append((name, old, new))
    return applied


def run(seen: list[str] | None = None, *, folder: str | os.PathLike | None = None, update: bool = True,
        url: str | None = None, progress=None) -> PluginUpdates:
    """Read the index (``url``, else the usual one) and :func:`check` the plugins of ``folder`` against it.
    A :class:`.index.PluginError` (the index cannot be read) goes to the caller."""
    entries = index.fetch(url)
    folder = Path(folder) if folder is not None else plugins_dir()
    return check(entries, Manager(folder).discover(), seen, folder, update=update, progress=progress)
