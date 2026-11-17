"""One record per run of the bench: the facts the measurement depends on, its steps, its results, and whether it is
valid and why.

A record (:func:`new`) holds exactly the keys of :data:`KEYS`. :func:`judge` says whether the run is a valid measurement
- not when the disk is on a slow link, reads slowly, when photos were online-only or when the run failed - and warns of
what does not invalidate it (a running Dropbox, a busy machine, a laptop on its battery). :func:`write` puts the record
in a folder, named by the run, its start and its index, and refuses a record that holds a path. Records may be shared:
they hold no path, no photo name, no coordinate and no CRS; the text a record keeps of an exception is
:func:`describe` (an ``OSError`` gives its type and ``strerror``, never its file name) passed through :func:`scrub`.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .. import __version__

SCHEMA = 1
KEYS = ("schema", "name", "started", "program", "machine", "disk", "set", "settings", "steps", "results", "warnings",
        "valid", "invalid_reasons", "error")
# a link below 5000 Mb/s is USB 2 (480 Mb/s) or slower: USB 3 starts at 5000 Mb/s
SLOW_LINK_MBPS = 5000
# 100 MB/s lies between what USB 2 gives (about 30-40 MB/s) and what an SSD on USB 3 gives (several hundred): an
# estimate of where a measurement stops being one of the engine and becomes one of the disk
SLOW_DISK_MB_S = 100
# a load average above 2.0 over the last minute means other work on the cores: an estimate, not measured
HIGH_LOAD = 2.0
# the name of a run: it becomes part of the name of a file, never a path
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
# a quoted span that holds a separator: a path as Python quotes it in the text of an OSError or the repr of a Path.
# A single quote opens or closes a span only at a word boundary: an apostrophe inside a word (can't, d'Alto,
# Sant'Elmo) is part of the span, of a path or of a photo name, never a quote
_IN_WORD = r"(?<=\w)'(?=\w)"
_QUOTED = re.compile(r"""(?<!\w)'(?:[^'\n]|""" + _IN_WORD + r""")*[/\\](?:[^'\n]|""" + _IN_WORD + r""")*'(?!\w)"""
                     r"""|"[^"\n]*[/\\][^"\n]*\"""")
# the tail of an unquoted path: to the end of the line, a quote (not an apostrophe inside a word), or the next " - "
# or ": " (a path with spaces is cut there rather than at its first space: a piece of a path left in the text is the
# defect, a lost word is not)
_TAIL = r"""(?:(?! - |: )(?:[^'"\n]|""" + _IN_WORD + r"""))*"""
# the last pass: a token (between spaces) that still holds a separator, a URL excepted
_TOKEN = re.compile(r"\S+")
_URL = re.compile(r"https?://", re.IGNORECASE)
# an unquoted absolute path: a token starting with / that has a second /, a token starting with ~, or one starting
# with a drive letter, never starting inside a word nor right after the scheme of a web URL (its // is no path; a
# path right after another word and a colon, or a file:// or sqlite:// URI, is a path), with its tail
_ABSOLUTE = re.compile(r"""(?<![\w.~-])(?<!https:)(?<!http:)(?<!https:/)(?<!http:/)"""
                       r"""(?:/[^\s'"]*/|~[^\s'"]+|[A-Za-z]:[\\/])""" + _TAIL)
# what may follow a private path: its end (the end of the text, a space, a quote) or a separator with the tail of
# the path under it; a letter means a longer name (/Users/x is not /Users/xy)
_PRIVATE_END = r"""(?:(?=$|[\s'"])|[/\\]""" + _TAIL + ")"
_EPSG = re.compile(r"EPSG:\d+", re.IGNORECASE)
# a coordinate: four or more digits before the point (metres), or five or more after it (degrees)
_NUMBER = re.compile(r"(?<![\d.])(?:\d{4,}(?:\.\d+)?|\d*\.\d{5,})")


def check_name(name: str) -> str:
    """``name`` when it is a name a file can take (letters, digits, ``.``, ``_``, ``-``, not starting with a point);
    else a ValueError, before anything is run under it."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("the name of a run is letters, digits, '.', '_' and '-', starting with a letter or a digit")
    return name


def new(name: str, machine: dict, disk: dict, set_: dict, settings: dict) -> dict:
    """A record of the run ``name``, started now (UTC, ISO 8601, to the second), of this version of the program, with
    the facts of the machine, the disk and the set of photos and the ``settings`` of the engine; its steps, results
    and warnings empty, ``valid`` None until :func:`judge`, no error."""
    return {"schema": SCHEMA, "name": check_name(name),
            "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "program": __version__,
            "machine": machine, "disk": disk, "set": set_, "settings": settings, "steps": [], "results": {},
            "warnings": [], "valid": None, "invalid_reasons": [], "error": None}


def judge(rec: dict) -> dict:
    """``valid``, ``invalid_reasons`` and ``warnings`` of ``rec``, which is returned. Not valid: the link of the disk is
    known and below :data:`SLOW_LINK_MBPS` (``slow disk link: 480 Mb/s``), the disk reads below :data:`SLOW_DISK_MB_S`
    (``slow disk: 32 MB/s``), photos were online-only (``online-only photos: N``), the run failed (``failed: ...``,
    from ``error``) or the extra results failed (``failed: ...``, from ``results["error"]``). Warnings, which do not
    invalidate: Dropbox running, a load above :data:`HIGH_LOAD`, the battery. A fact that is None is not known and
    neither invalidates nor warns."""
    disk, machine, set_ = rec.get("disk") or {}, rec.get("machine") or {}, rec.get("set") or {}
    reasons, warnings = [], []
    link = disk.get("link_mbps")
    if link is not None and link < SLOW_LINK_MBPS:
        reasons.append(f"slow disk link: {link:g} Mb/s")
    rate = disk.get("read_mb_s")
    if rate is not None and rate < SLOW_DISK_MB_S:
        reasons.append(f"slow disk: {rate:.0f} MB/s")
    online_only = set_.get("online_only") or 0
    if online_only > 0:
        reasons.append(f"online-only photos: {online_only}")
    if rec.get("error"):
        reasons.append(f"failed: {rec['error']}")
    extra = (rec.get("results") or {}).get("error")
    if extra:
        reasons.append(f"failed: {extra}")
    if machine.get("dropbox_running") is True:
        warnings.append("Dropbox running")
    load = machine.get("load")
    if load is not None and load > HIGH_LOAD:
        warnings.append(f"load {load:.1f} above {HIGH_LOAD}")
    if machine.get("on_battery") is True:
        warnings.append("on battery")
    rec["warnings"], rec["valid"], rec["invalid_reasons"] = warnings, not reasons, reasons
    return rec


def describe(exc: BaseException) -> str:
    """The text a record keeps of an exception, to be passed through :func:`scrub`: its type and its message; for an
    ``OSError`` (and its subclasses) its type and ``strerror`` alone, never ``filename``, ``filename2`` or
    ``str(exc)``, which name the file (the type alone when there is no ``strerror``: a ``FileNotFoundError`` raised
    with the path as its message loses the path)."""
    name = type(exc).__name__
    if isinstance(exc, OSError):
        return f"{name}: {exc.strerror}" if exc.strerror else name
    text = str(exc)
    return f"{name}: {text}" if text else name


def _forms(private) -> list[str]:
    """The spellings of the private paths to look for, longest first: each as given when it is absolute (by a
    slash, a drive letter or ``~``) and, for a path of this system, as resolved (``/var`` and ``/private/var`` are one
    folder on macOS); each of them also with every backslash doubled (as the text of an ``OSError`` on Windows
    writes it), with slashes for backslashes (as the repr of a ``Path`` does) and as ``os.path.normcase`` folds it.
    Never a relative spelling, which would match words."""
    forms = set()
    for path in private:
        given = str(path)
        own = os.path.isabs(given) or given.startswith("~")
        if own or _DRIVE.match(given):
            forms.add(given)
        if own:
            forms.add(str(Path(path).expanduser().resolve()))
    spellings = set()
    for form in forms:
        spellings.update((form, form.replace("\\", "\\\\"), form.replace("\\", "/"), os.path.normcase(form)))
    return sorted((f for f in spellings if f and f != os.sep), key=len, reverse=True)


def scrub(text: str, private=(), names=()) -> str:
    """``text`` (of an exception) with what a record must not hold replaced, in this order: every quoted span that
    holds a separator by ``'<path>'`` (an apostrophe inside a word is no quote); every path of ``private`` (in every
    spelling of :func:`_forms`, case ignored, longest first, whole: at its end or with the rest of the path under it,
    never into a longer name) by ``<path>``; every remaining unquoted absolute path, to the end of the line or the
    next `` - `` or ``: ``, by ``<path>``; every photo file name of ``names`` and every stem of them, longest first
    and whole, by ``<photo>``; every ``EPSG:<digits>``, case ignored, by ``<crs>``; every number with four or more
    digits before the point or five or more after it (a coordinate in metres or in degrees) by ``<number>``; last,
    every token between spaces that still holds a separator (what a stop left of a path: a folder named with
    `` - ``, a private folder whose name is another's up to a space; a relative path; a unit such as ``m/s``), a
    URL excepted, by ``<path>``.

    What remains, since a pass over tokens cannot close it: the words of a folder name after a `` - `` inside it
    (``<path> - Saggio Est <path>``); the inner words of a folder name of three or more words that a stop split at
    a space - a private folder whose name is another folder's up to a space, a private home that is the start of
    another user's home (``<path> Nord <path>``, ``<path> Maria <path>``); the words of a relative path with spaces
    (``Scavo Nord <path>``). A folder of one or two words leaves nothing."""
    text = _QUOTED.sub("'<path>'", text)
    forms = _forms(private)
    if forms:
        text = re.sub("(?:" + "|".join(map(re.escape, forms)) + ")" + _PRIVATE_END, "<path>", text, flags=re.IGNORECASE)
    text = _ABSOLUTE.sub("<path>", text)
    words = {str(n) for n in names} | {Path(str(n)).stem for n in names}
    for word in sorted((w for w in words if w), key=len, reverse=True):
        text = re.sub(rf"(?<![\w-]){re.escape(word)}(?![\w-])", "<photo>", text, flags=re.IGNORECASE)
    text = _EPSG.sub("<crs>", text)
    text = _NUMBER.sub("<number>", text)
    return _TOKEN.sub(_token_scrubbed, text)


def _token_scrubbed(match: re.Match) -> str:
    token = match.group(0)
    if ("/" in token or "\\" in token) and not _URL.match(token):
        return "<path>"
    return token


def _strings(value, where: str = "record"):
    """Every string of ``value``, keys included, with where it sits."""
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key, f"{where}.<key>")
            yield from _strings(item, f"{where}.{key}" if isinstance(key, str) else where)
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            yield from _strings(item, f"{where}[{i}]")


def write(rec: dict, folder: Path, private=(), index: int = 1) -> Path:
    """``rec`` written as ``<name>-<digits of started>-<index>.json`` in ``folder`` (made when missing), never over an
    existing file (a FileExistsError goes to the caller). A ValueError, and no file, when any string of the record - a
    key or a value, the error among them - starts with ``/``, ``~`` or a drive letter, or holds one of the ``private``
    paths in any spelling of :func:`_forms`, case ignored: the ValueError names where the string sits, never the
    string."""
    forms = [form.casefold() for form in _forms(private)]
    for where, text in _strings(rec):
        low = text.casefold()
        if text.startswith(("/", "~")) or _DRIVE.match(text) or any(form in low for form in forms):
            raise ValueError(f"the record holds a path ({where}): not written")
    name = check_name(rec["name"])
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{re.sub(r'[^0-9]', '', str(rec['started']))}-{int(index)}.json"
    with open(path, "x", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)
    return path
