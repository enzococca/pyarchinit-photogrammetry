"""Paths of the photos as a project file stores them.

In memory a photo path is absolute. In the ``.ppg`` file it is written relative to the folder of the project file
whenever the photo is on the same volume as the project, inside that folder or not (``./foto/IMG_1.JPG``,
``./../foto/IMG_1.JPG``, POSIX separators on every system), so a project moved together with its photos finds
them; a photo on another volume (another disk, another Windows drive or share) stays absolute.

Relative paths always start with ``./``: versions before 2.0 read ``./<rest>`` as ``<project folder>/<rest>``, so
they find ``./../foto/IMG_1.JPG`` too. ``../foto/IMG_1.JPG`` (as other programs write it) is read as well.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path


def windows_same_volume(path: str, base: str) -> bool:
    """Whether two absolute Windows paths are on the same drive (``C:``) or the same share (``\\\\server\\share``),
    ignoring case. A path without a drive is never on the same volume."""
    a, b = ntpath.splitdrive(path)[0], ntpath.splitdrive(base)[0]
    return bool(a) and ntpath.normcase(a) == ntpath.normcase(b)


def _existing(p: Path) -> Path | None:
    for q in (p, *p.parents):
        try:
            if q.is_dir():
                return q
        except OSError:
            continue
    return None


def same_volume(path: str | os.PathLike, base: str | os.PathLike) -> bool | None:
    """Whether the photo ``path`` and the folder ``base`` of the project are on the same volume: same drive or share
    on Windows, same device (``st_dev``) of their folders elsewhere. None when it cannot be told: the folder of the
    photo is not there (a disk not plugged in), and the nearest folder that is would answer for the wrong volume."""
    if os.name == "nt":
        return windows_same_volume(os.path.abspath(path), os.path.abspath(base))
    folder, root = Path(path).parent, _existing(Path(base))
    try:
        if root is None or not folder.is_dir():
            return None
        return folder.stat().st_dev == root.stat().st_dev
    except OSError:
        return None


def relative_text(path: str, base: str, mod=os.path) -> str | None:
    """``path`` relative to the folder ``base`` as the file stores it (``./...``, POSIX separators), None when there
    is no relative path (another Windows drive). ``mod``: :mod:`os.path`, or :mod:`ntpath` / :mod:`posixpath`."""
    try:
        rel = mod.relpath(path, base)
    except ValueError:
        return None
    return "./" + rel.replace(mod.sep, "/") if mod.sep != "/" else "./" + rel


def _real(path: str | os.PathLike) -> str:
    """The absolute path with the links of its folders followed, the file name kept as given."""
    p = Path(path)
    return os.path.join(os.path.realpath(p.parent), p.name)


def portable(path: str, base: Path | None, was_relative: bool = False) -> str:
    """What the project file stores for the photo ``path`` (absolute in memory) of a project in the folder ``base``:
    relative when on the same volume, else absolute. When the volume cannot be told (:func:`same_volume` None), the
    form it had when the project was read: ``was_relative``."""
    if base is None or not path or not Path(path).is_absolute():
        return path
    same = same_volume(path, base)
    if same is None:
        same = was_relative
    if not same:
        return path
    rel = relative_text(_real(path), os.path.realpath(base))
    return path if rel is None else rel


def is_relative(path: str) -> bool:
    """A path as stored relative to the project file."""
    return isinstance(path, str) and (path.startswith("./") or path.startswith("../")
                                      or path.startswith(".\\") or path.startswith("..\\"))


def resolve(path: str, base: Path | None) -> str:
    """The absolute path of a stored ``path``: relative ones (:func:`is_relative`) from the folder ``base`` of the
    project, whether the file is there or not; absolute ones as they are."""
    if base is not None and is_relative(path):
        return str((base / path.replace("\\", "/")).resolve())
    return path
