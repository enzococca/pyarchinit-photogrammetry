"""Append other projects to a document, each chunk of them as a new chunk (as Metashape's *File > Append*).

The sources are projects of this program (``.ppg``), of Metashape (``.psx``) and WebODM/ODM tasks (``all.zip`` or
the task folder). A source is only read, never written. Each chunk of a ``.ppg`` comes with its data folder, copied
into the files folder of the document (one file at a time; on APFS a copy-on-write clone, instant), with its photos,
sensors, cameras, markers, scale bars, CRS, transform, products, versions, drawings and settings; its photos stay
where they are (stored relative to the document when they are inside its folder, see :class:`.chunk.Chunk`).
Metashape and WebODM go through their importers (:func:`..integrations.metashape.import_psx_into`,
:func:`..integrations.webodm.import_task_into`).

Nothing is aligned: the chunks keep their own frame and CRS (Flusso di lavoro > Allinea chunk / Unisci chunk next).
A source that fails leaves no chunk behind and does not stop the others; a cancellation takes away every chunk
appended by the call. The document is not saved.
"""
from __future__ import annotations

import copy
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .progress import Cancelled, as_progress

log = logging.getLogger("pyarchinit_photogrammetry")

KINDS = ("ppg", "psx", "webodm")
SKIPPED = {"undo"}  # folders of a chunk not copied: the copies of Modifica > Annulla of its last session


@dataclass
class Appended:
    """What one source gave: ``chunks`` the new chunks (empty when it failed), ``error`` why it failed."""

    path: Path
    kind: str | None
    chunks: list = field(default_factory=list)
    error: str | None = None
    warnings: list = field(default_factory=list)


def source_kind(path, webodm: bool = False) -> str:
    """``"ppg"``, ``"psx"`` or ``"webodm"`` (an ``all.zip`` or a task folder); ``webodm``: the source must be a
    WebODM task. A ValueError for anything else."""
    path = Path(path)
    suffix = path.suffix.lower()
    if webodm:
        if path.is_dir() or suffix == ".zip":
            return "webodm"
        raise ValueError(f"{path} is not a WebODM task (all.zip or a task folder)")
    if path.is_dir() or suffix == ".zip":
        return "webodm"
    if suffix in (".ppg", ".psx"):
        return suffix[1:]
    raise ValueError(f"{path}: not a project that can be appended (.ppg, .psx, WebODM all.zip or task folder)")


def source_chunks(path) -> list[str]:
    """The names of the chunks of a ``.ppg`` or ``.psx`` source, in their order (one for a WebODM task)."""
    path = Path(path)
    kind = source_kind(path)
    if kind == "ppg":
        from .document import Document

        return [c.label for c in Document(path).chunks]
    if kind == "psx":
        from ..integrations.metashape import read_psx

        return [mc.label for mc in read_psx(path)]
    return [path.stem if path.suffix else path.name]


def _chosen(labels: list[str], wanted) -> list[int]:
    """The positions of ``wanted`` (names or positions) among ``labels``; all when ``wanted`` is None."""
    if wanted is None:
        return list(range(len(labels)))
    out = []
    for w in wanted:
        if isinstance(w, int) and not isinstance(w, bool):
            if not 0 <= w < len(labels):
                raise ValueError(f"no chunk number {w}: the project has {len(labels)}")
            i = w
        elif w in labels:
            i = labels.index(w)
        else:
            raise ValueError(f"no chunk {w!r}: the chunks of the project are {', '.join(map(repr, labels)) or 'none'}")
        if i not in out:
            out.append(i)
    return out


def unique_label(doc, label: str, prefix: str, chunk=None) -> str:
    """``label`` when no other chunk of ``doc`` has it, else ``"<prefix> - <label>"``, numbered when that is taken too."""
    taken = {c.label for c in doc.chunks if c is not chunk}
    if label not in taken:
        return label
    base = f"{prefix} - {label}"
    name, n = base, 2
    while name in taken:
        name, n = f"{base} ({n})", n + 1
    return name


# --------------------------------------------------------------------------- .ppg
def _moved(value, old: str, new: str):
    """``value`` (JSON-like) with every string that is ``old`` or a path under it pointing under ``new``."""
    if isinstance(value, str):
        if value == old or value.startswith(old + os.sep) or value.startswith(old + "/"):
            return new + value[len(old):]
        return value
    if isinstance(value, dict):
        return {k: _moved(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_moved(v, old, new) for v in value]
    if isinstance(value, tuple):
        return tuple(_moved(v, old, new) for v in value)
    return value


def _copy_folder(src: Path, dst: Path, progress, label: str) -> None:
    """The data folder of a chunk copied one file at a time (a clone on APFS), links as links (pointing into the copy
    when they pointed into ``src``), the folders of :data:`SKIPPED` left out; progress per file."""
    from ..io.files import clone

    entries = []
    for root, dirs, files in os.walk(src):
        here = Path(root)
        keep = []
        for d in dirs:
            if here == src and d in SKIPPED:
                continue
            (entries.append(here / d) if (here / d).is_symlink() else keep.append(d))
        dirs[:] = keep
        entries.extend(here / f for f in files)
    dst.mkdir(parents=True, exist_ok=True)
    total = max(len(entries), 1)
    old = os.path.abspath(src)
    for i, path in enumerate(entries):
        target = dst / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            link = os.readlink(path)
            if os.path.isabs(link):
                link = _moved(link, old, os.path.abspath(dst))
            os.symlink(link, target)
        else:
            clone(path, target)
        if i % 20 == 0 or i == total - 1:
            progress((i + 1) / total, f"{label}: {i + 1}/{total} file copiati")


def append_ppg(doc, path, chunks=None, progress=None) -> list:
    """The chunks of the project ``path`` (all, or those named or numbered in ``chunks``) as new chunks of ``doc``,
    each with a copy of its data folder. Returns the new chunks; on an error those made are taken away again."""
    from .document import Document

    progress = as_progress(progress)
    path = Path(path).resolve()
    if doc.path is not None and Path(doc.path).resolve() == path:
        raise ValueError("è il progetto aperto: non si può aggiungere a sé stesso")
    src = Document(path)  # only read: nothing of it is written
    picked = [src.chunks[i] for i in _chosen([c.label for c in src.chunks], chunks)]
    src_files = path.with_name(path.name + ".files")
    added = []
    try:
        for n, sc in enumerate(picked):
            sub = progress.sub(n / max(len(picked), 1), 1 / max(len(picked), 1))
            ch = doc.add_chunk(unique_label(doc, sc.label, path.stem))
            added.append(ch)
            old = os.path.abspath(src_files / f"chunk_{sc.key}")
            new_dir = doc.files_dir / f"chunk_{ch.key}"
            new = os.path.abspath(new_dir)
            if Path(old).is_dir():
                if new_dir.is_dir() and not any(new_dir.iterdir()):
                    new_dir.rmdir()
                _copy_folder(Path(old), new_dir, sub, ch.label)
            for name in ("enabled", "crs", "sensors", "cameras", "markers", "scalebars"):
                setattr(ch, name, copy.deepcopy(getattr(sc, name)))
            ch.transform = sc.transform.copy()
            for cam in ch.cameras:  # photos kept in the folder of the chunk (reference copies of captures)
                cam.path = _moved(os.path.abspath(cam.path), old, new) if cam.path else cam.path
            ch.meta = _moved(copy.deepcopy(sc.meta), old, new)
            products = {}
            for name, rel in sc.products.items():
                where = os.path.normpath(os.path.join(old, rel))
                if where == old or where.startswith(old + os.sep):
                    products[name] = rel
                else:  # a file of another program the chunk points to: the same file, from the new folder
                    try:
                        products[name] = os.path.relpath(where, new)
                    except ValueError:  # another drive (Windows)
                        products[name] = where
            ch.products = products
            sub(1.0, f"{ch.label}: {len(ch.cameras)} foto")
    except BaseException:
        for ch in added:
            doc.chunks.remove(ch)
            shutil.rmtree(doc.files_dir / f"chunk_{ch.key}", ignore_errors=True)
        raise
    return added


# --------------------------------------------------------------------------- all
def append_projects(doc, sources, chunks=None, webodm: bool = False, progress=None) -> list[Appended]:
    """Every source (``.ppg``, ``.psx``, WebODM ``all.zip`` or task folder) appended to ``doc`` as new chunks, in the
    order given. ``chunks``: the chunks to take of the ``.ppg`` and ``.psx`` sources - a dict ``{source path: names or
    positions}`` (a source not in it: all its chunks), or a list of names for every one of them; ``webodm``: every
    source is a WebODM task. A source that fails leaves nothing and the others go on (its ``error``); a cancellation
    (:class:`.progress.Cancelled`) takes away everything this call appended and goes on. The active chunk is not
    changed and ``doc`` is not saved. Returns one :class:`Appended` per source."""
    progress = as_progress(progress)
    sources = [Path(s) for s in sources]
    results: list[Appended] = []
    for i, path in enumerate(sources):
        sub = progress.sub(i / max(len(sources), 1), 1 / max(len(sources), 1))
        result = Appended(path=path, kind=None)
        results.append(result)
        before = list(doc.chunks)
        try:
            result.kind = kind = source_kind(path, webodm)
            if not path.exists():
                raise FileNotFoundError(f"{path} non esiste")
            wanted = chunks.get(str(path), chunks.get(path)) if isinstance(chunks, dict) else chunks
            if kind == "ppg":
                result.chunks = append_ppg(doc, path, wanted, progress=sub)
            elif kind == "psx":
                from ..integrations.metashape import import_psx_into, read_psx

                labels = [mc.label for mc in read_psx(path)]
                result.chunks = import_psx_into(doc, path, None if wanted is None else _chosen(labels, wanted),
                                                progress=sub)
            else:
                from ..integrations.webodm import import_task_into

                result.chunks = [import_task_into(doc, path, progress=sub)]
            for ch in result.chunks:
                if kind != "ppg":
                    ch.label = unique_label(doc, ch.label, path.stem if path.suffix else path.name, ch)
                result.warnings += list((ch.meta.get("import") or {}).get("warnings") or [])
            result.warnings = list(dict.fromkeys(result.warnings))
        except Cancelled:
            _take_away(doc, [c for r in results for c in r.chunks] + [c for c in doc.chunks if c not in before])
            raise
        except Exception as exc:  # noqa: BLE001 - one source failing does not stop the others
            log.warning("Aggiunta di %s non riuscita: %s", path, exc, exc_info=True)
            _take_away(doc, [c for c in doc.chunks if not any(c is b for b in before)])
            result.chunks = []
            result.error = str(exc) or type(exc).__name__
    progress(1.0, "Progetti aggiunti")
    return results


def _take_away(doc, chunks) -> None:
    for ch in chunks:
        if any(ch is c for c in doc.chunks):
            doc.chunks.remove(ch)
            shutil.rmtree(doc.files_dir / f"chunk_{ch.key}", ignore_errors=True)


__all__ = ["Appended", "append_projects", "append_ppg", "source_kind", "source_chunks", "unique_label", "KINDS"]
