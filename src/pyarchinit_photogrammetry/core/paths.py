"""Paths of the photos as a project file stores them.

In memory a photo path is absolute. In the ``.ppg`` file it is written relative to the folder of the project file
whenever the photo is on the same volume as the project, inside that folder or not (``./foto/IMG_1.JPG``,
``./../foto/IMG_1.JPG``, POSIX separators on every system), so a project moved together with its photos finds
them; a photo on another volume (another disk, another Windows drive or share) stays absolute.

Relative paths always start with ``./``: versions before 2.0 read ``./<rest>`` as ``<project folder>/<rest>``, so
they find ``./../foto/IMG_1.JPG`` too. ``../foto/IMG_1.JPG`` (as other programs write it) is read as well. Beside a
relative path the file keeps the last absolute one (``path_abs`` of a camera, ``raw_path_abs``, ``image_root_abs``
of a chunk: keys older versions ignore and drop), taken when the relative one does not lead to the photo - a project
moved without its photos.

The disk is looked at once per folder of photos (:class:`Folders`), never once per photo: a project of thousands of
photos on a slow disk saves, opens and fills the Photos pane without a call per file.
"""
from __future__ import annotations

import ntpath
import os
import stat
from pathlib import Path


class Folders:
    """What the disk says about the folders of the photos, each folder looked at once: its real path (links followed),
    its device and the names of its files (one listing). A folder that is not there answers None."""

    def __init__(self):
        self._real: dict[str, str] = {}
        self._dev: dict[str, int | None] = {}
        self._names: dict[str, frozenset | None] = {}

    def real(self, folder: str) -> str:
        folder = str(folder)
        if folder not in self._real:
            self._real[folder] = os.path.realpath(folder)
        return self._real[folder]

    def device(self, folder: str) -> int | None:
        folder = str(folder)
        if folder not in self._dev:
            try:
                st = os.stat(folder)
                self._dev[folder] = st.st_dev if stat.S_ISDIR(st.st_mode) else None
            except (OSError, ValueError):
                self._dev[folder] = None
        return self._dev[folder]

    def names(self, folder: str) -> frozenset | None:
        folder = str(folder)
        if folder not in self._names:
            try:
                with os.scandir(folder) as entries:
                    self._names[folder] = frozenset(e.name for e in entries)
            except (OSError, ValueError):
                self._names[folder] = None
        return self._names[folder]

    def exists(self, path: str | os.PathLike) -> bool:
        """Whether the file ``path`` is in its folder (the listing of the folder, not the file, is looked at)."""
        folder, name = os.path.split(os.fspath(path))
        names = self.names(folder)
        return names is not None and name in names


def windows_same_volume(path: str, base: str) -> bool:
    """Whether two absolute Windows paths are on the same drive (``C:``) or the same share (``\\\\server\\share``),
    ignoring case. A path without a drive is never on the same volume."""
    a, b = ntpath.splitdrive(path)[0], ntpath.splitdrive(base)[0]
    return bool(a) and ntpath.normcase(a) == ntpath.normcase(b)


def same_volume(path: str | os.PathLike, base: str | os.PathLike, cache: Folders | None = None) -> bool | None:
    """Whether the photo ``path`` and the folder ``base`` of the project are on the same volume: same drive or share
    on Windows, same device (``st_dev``) of their folders elsewhere. None when it cannot be told: the folder of the
    photo is not there (a disk not plugged in), and the nearest folder that is would answer for the wrong volume.
    ``cache``: the folders already looked at."""
    if os.name == "nt":
        return windows_same_volume(os.path.abspath(path), os.path.abspath(base))
    cache = cache if cache is not None else Folders()
    dev = cache.device(os.path.dirname(os.fspath(path)))
    if dev is None:
        return None
    for q in (Path(base), *Path(base).parents):
        root = cache.device(str(q))
        if root is not None:
            return dev == root
    return None


def relative_text(path: str, base: str, mod=os.path) -> str | None:
    """``path`` relative to the folder ``base`` as the file stores it (``./...``, POSIX separators), None when there
    is no relative path (another Windows drive). ``mod``: :mod:`os.path`, or :mod:`ntpath` / :mod:`posixpath`."""
    try:
        rel = mod.relpath(path, base)
    except ValueError:
        return None
    return "./" + rel.replace(mod.sep, "/") if mod.sep != "/" else "./" + rel


def _real(path: str, cache: Folders) -> str:
    """The absolute path with the links of its folders followed, the file name kept as given."""
    folder, name = os.path.split(os.fspath(path))
    return os.path.join(cache.real(folder), name)


def portable(path: str, base: Path | None, was_relative: bool = False, inside_only: bool = False,
             cache: Folders | None = None) -> str:
    """What the project file stores for the photo ``path`` (absolute in memory) of a project in the folder ``base``:
    relative when on the same volume, else absolute. When the volume cannot be told (:func:`same_volume` None), the
    form it had when the project was read: ``was_relative``. ``inside_only``: relative only inside ``base`` (the
    rule before 2.0, kept for the chunks handed to processing nodes, whose other paths are mapped by prefix).
    ``cache``: the folders already looked at (one :class:`Folders` for a whole save)."""
    if base is None or not path or not Path(path).is_absolute():
        return path
    cache = cache if cache is not None else Folders()
    if inside_only:
        rel = relative_text(_real(path, cache), cache.real(str(base)))
        return rel if rel is not None and not rel.startswith("./../") else path
    same = same_volume(path, base, cache)
    if same is None:
        same = was_relative
    if not same:
        return path
    rel = relative_text(_real(path, cache), cache.real(str(base)))
    return path if rel is None else rel


def is_relative(path: str) -> bool:
    """A path as stored relative to the project file."""
    return isinstance(path, str) and (path.startswith("./") or path.startswith("../")
                                      or path.startswith(".\\") or path.startswith("..\\"))


def resolve(path: str, base: Path | None) -> str:
    """The absolute path of a stored ``path``: relative ones (:func:`is_relative`) from the folder ``base`` of the
    project (its real path: the caller follows its links), whether the file is there or not; absolute ones as they
    are. Nothing is looked at on the disk."""
    if base is not None and is_relative(path):
        return os.path.normpath(os.path.join(str(base), path.replace("\\", "/")))
    return path


def choose(path: str, path_abs: str | None, base: Path | None, cache: Folders | None = None) -> str:
    """The absolute path a photo is read from, of its stored ``path`` and the absolute ``path_abs`` kept beside it:
    the relative one when it leads to the file, else the absolute one when that does, else the relative one (the
    photo is missing)."""
    out = resolve(path, base)
    if out == path or not path_abs:
        return out
    cache = cache if cache is not None else Folders()
    if cache.exists(out) or not cache.exists(path_abs):
        return out
    return path_abs
