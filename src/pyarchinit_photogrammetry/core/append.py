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
import json
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
SECTIONS = ("sezioni", "sezioni.json")  # beside a project: its sections (shared by the projects of the folder)
SECTION_FILES = ("svg", "dxf", "dxf3d")
KEY_FIELDS = ("reference_key", "reference_chunk")  # a key of another chunk of the project: chunk_alignment, coregistration


def _norm(path: str) -> str:
    """``path`` compared as the system does: one separator, case folded where the file system folds it (Windows)."""
    return os.path.normcase(os.path.normpath(path)) if path else path


def _inside(path: str, folder: str) -> bool:
    """``path`` is ``folder`` or a path under it, whatever separators either uses (a path stored with ``/`` on
    Windows, a folder from ``str(Path)`` with ``\\``)."""
    if not path or not folder:
        return False
    p, f = _norm(path), _norm(folder)
    return p == f or p.startswith(f.rstrip(os.sep) + os.sep)


def _moved(value, moves):
    """``value`` (JSON-like) with every string that is a folder of ``moves`` (``[(old, new)]``) or a path under it
    pointing under the new folder."""
    if isinstance(value, str):
        for old, new in moves:
            if _inside(value, old):
                rest = os.path.normpath(value)[len(os.path.normpath(old).rstrip(os.sep)):]  # normcase keeps lengths
                return new + rest.replace(os.sep, "/") if "/" in new and os.sep != "/" else new + rest
        return value
    if isinstance(value, dict):
        return {k: _moved(v, moves) for k, v in value.items()}
    if isinstance(value, list):
        return [_moved(v, moves) for v in value]
    if isinstance(value, tuple):
        return tuple(_moved(v, moves) for v in value)
    return value


def _strings_under(value, folder: str) -> list[str]:
    """The strings of ``value`` (JSON-like) that are paths under ``folder``."""
    if isinstance(value, str):
        return [value] if _inside(value, folder) else []
    if isinstance(value, dict):
        return [x for v in value.values() for x in _strings_under(v, folder)]
    if isinstance(value, (list, tuple)):
        return [x for v in value for x in _strings_under(v, folder)]
    return []


def _remap_keys(value, mapping: dict, lost: list):
    """``value`` (JSON-like) with the chunk keys of :data:`KEY_FIELDS` turned into those of the chunks appended
    (``mapping``: old key -> new key); a key of a chunk not appended becomes None (added to ``lost``): never a key
    that names another chunk of this project."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in KEY_FIELDS and isinstance(v, int) and not isinstance(v, bool):
                if v in mapping:
                    out[k] = mapping[v]
                else:
                    out[k] = None
                    lost.append(v)
            else:
                out[k] = _remap_keys(v, mapping, lost)
        return out
    if isinstance(value, list):
        return [_remap_keys(v, mapping, lost) for v in value]
    return value


def _free_name(folder: Path, name: str) -> Path:
    target = folder / name
    stem, suffix, n = Path(name).stem, Path(name).suffix, 2
    while target.exists() or target.is_symlink():
        target, n = folder / f"{stem}_{n}{suffix}", n + 1
    return target


def _copy_folder(src: Path, dst: Path, progress, label: str, moves) -> None:
    """The data folder of a chunk copied one file at a time (a clone on APFS), links as links (pointing into the copies
    when they pointed into a folder of ``moves``), the folders of :data:`SKIPPED` left out; progress per file."""
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
    for i, path in enumerate(entries):
        target = dst / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            link = os.readlink(path)
            if os.path.isabs(link):
                link = _moved(link, moves)
            os.symlink(link, target)
        else:
            clone(path, target)
        if i % 20 == 0 or i == total - 1:
            progress((i + 1) / total, f"{label}: {i + 1}/{total} file copiati")


def _copy_zoning(ch, new: str) -> None:
    """The GeoPackages of the zonings of the multispectral captures (written by default beside the source project)
    copied into the folder of ``ch`` and pointed at: QGIS edits the copy, never the source project's file."""
    from ..io.files import clone

    zoning = (ch.meta.get("multispectral") or {}).get("zoning") or {}
    for entry in zoning.values():
        file = entry.get("file") if isinstance(entry, dict) else None
        if not file or _inside(os.path.abspath(file), new) or not Path(file).is_file():
            continue
        folder = Path(new) / "multispectral" / "zoning"
        folder.mkdir(parents=True, exist_ok=True)
        target = _free_name(folder, Path(file).name)
        clone(file, target)
        entry["file"] = str(target)


def source_sections(doc, path) -> list[dict]:
    """The sections of the project ``path`` (``sezioni/sezioni.json`` beside it), their drawings (SVG, DXF) copied into
    the folder of the sections of ``doc`` and pointed at; none when the two projects share that folder or ``doc`` has
    no file yet (it has no folder for them)."""
    from ..io.files import clone

    path = Path(path).resolve()
    source = path.parent.joinpath(*SECTIONS)
    if doc.path is None or not source.is_file():
        return []
    folder = Path(doc.path).resolve().parent / SECTIONS[0]
    if folder.resolve() == source.parent.resolve():
        return []
    try:
        sections = [dict(x) for x in json.loads(source.read_text(encoding="utf-8"))]
    except (OSError, ValueError) as exc:
        log.warning("%s non leggibile: %s", source, exc)
        return []
    for sec in sections:
        for key in SECTION_FILES:
            file = sec.get(key)
            if file and Path(file).is_file():
                folder.mkdir(parents=True, exist_ok=True)
                target = _free_name(folder, Path(file).name)
                clone(file, target)
                sec[key] = str(target)
    return sections


def merge_sections(existing: list[dict], incoming: list[dict], prefix: str) -> list[dict]:
    """``incoming`` sections made fit to join ``existing``: a name taken gets ``prefix`` before it (numbered when
    that is taken too), an identity (``ppg_id``) taken is dropped (the window gives a new one). Returns the new ones."""
    labels = {s.get("label") for s in existing}
    ids = {s.get("ppg_id") for s in existing if s.get("ppg_id")}
    out = []
    for sec in incoming:
        sec = dict(sec)
        label = str(sec.get("label") or "Sezione")
        if label in labels:
            base = f"{prefix} - {label}"
            label, n = base, 2
            while label in labels:
                label, n = f"{base} ({n})", n + 1
        sec["label"] = label
        labels.add(label)
        if sec.get("ppg_id") in ids:
            sec.pop("ppg_id")
        elif sec.get("ppg_id"):
            ids.add(sec["ppg_id"])
        out.append(sec)
    return out


def write_sections(doc, sections: list[dict], prefix: str) -> list[dict]:
    """``sections`` (of :func:`source_sections`) added to ``sezioni/sezioni.json`` beside ``doc`` (the command line;
    the window merges them into the sections it shows). Returns those added."""
    if not sections or doc.path is None:
        return []
    file = Path(doc.path).resolve().parent.joinpath(*SECTIONS)
    existing = json.loads(file.read_text(encoding="utf-8")) if file.is_file() else []
    new = merge_sections(existing, sections, prefix)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(existing + new, indent=1, ensure_ascii=False), encoding="utf-8")
    return new


def append_ppg(doc, path, chunks=None, progress=None, on_chunk=None, report: dict | None = None) -> list:
    """The chunks of the project ``path`` (all, or those named or numbered in ``chunks``) as new chunks of ``doc``,
    each with a copy of its data folder. ``on_chunk(chunk)``: called for each new chunk as soon as it is in ``doc``,
    before its folder is filled (all the chunks are made first, then filled). Paths into the folders of the chunks
    appended point at the copies, the zoning GeoPackages are copied, the keys of chunks kept in the settings
    (*Allinea chunk*, coregistration) turned into the new ones - or dropped when that chunk is not appended, said in
    ``report["warnings"]``, with the files of the source project the chunks still point to and a site of the source
    other than this project's. ``report["sections"]``: the sections of the source (:func:`source_sections`).
    Returns the new chunks; on an error those it made are taken away again."""
    from .document import Document

    progress = as_progress(progress)
    report = report if report is not None else {}
    warnings = report.setdefault("warnings", [])
    path = Path(path).resolve()
    if doc.path is not None and Path(doc.path).resolve() == path:
        raise ValueError("è il progetto aperto: non si può aggiungere a sé stesso")
    src = Document(path)  # only read: nothing of it is written
    picked = [src.chunks[i] for i in _chosen([c.label for c in src.chunks], chunks)]
    src_files = os.path.abspath(path.with_name(path.name + ".files"))
    added, pairs = [], []
    try:
        for sc in picked:
            ch = doc.add_chunk(unique_label(doc, sc.label, path.stem))
            added.append(ch)
            if on_chunk is not None:
                on_chunk(ch)
            pairs.append((sc, ch))
        mapping = {sc.key: ch.key for sc, ch in pairs}
        moves = [(os.path.join(src_files, f"chunk_{sc.key}"), os.path.abspath(doc.files_dir / f"chunk_{ch.key}"))
                 for sc, ch in pairs]
        for n, (sc, ch) in enumerate(pairs):
            sub = progress.sub(n / max(len(pairs), 1), 1 / max(len(pairs), 1))
            old, new = moves[n]
            new_dir = Path(new)
            if Path(old).is_dir():
                if new_dir.is_dir() and not any(new_dir.iterdir()):
                    new_dir.rmdir()
                _copy_folder(Path(old), new_dir, sub, ch.label, moves)
            for name in ("enabled", "crs", "sensors", "cameras", "markers", "scalebars"):
                setattr(ch, name, copy.deepcopy(getattr(sc, name)))
            ch.transform = sc.transform.copy()
            for cam in ch.cameras:  # photos kept in the folder of the chunk (reference copies of captures)
                if cam.path:
                    cam.path = _moved(os.path.abspath(cam.path), moves)
                if isinstance(getattr(cam, "meta", None), dict):
                    cam.meta = _moved(cam.meta, moves)
            lost: list = []
            meta = _remap_keys(_moved(copy.deepcopy(sc.meta), moves), mapping, lost)
            for e in (meta.get("merge") or {}).get("chunks") or []:  # the chunks a merged chunk came from
                if isinstance(e, dict) and "key" in e:
                    e["key"] = mapping.get(e["key"])
            alignment = meta.get("chunk_alignment")
            dropped = isinstance(alignment, dict) and "reference_key" in alignment and alignment["reference_key"] is None
            if dropped:
                meta.pop("chunk_alignment")
                warnings.append(f"«{ch.label}»: era allineato a «{alignment.get('reference')}», che non è stato "
                                "aggiunto: il collegamento è tolto (la sua posizione resta)")
            if len(lost) > int(dropped):
                warnings.append(f"«{ch.label}»: rimanda a un chunk del progetto di origine che non è stato aggiunto "
                                "(riferimento della coregistrazione): il collegamento è tolto")
            ch.meta = meta
            _copy_zoning(ch, new)
            products = {}
            for name, rel in sc.products.items():
                where = os.path.normpath(os.path.join(old, rel))
                if _inside(where, old):
                    products[name] = rel
                    continue
                where = _moved(where, moves)  # a file of a sibling chunk appended too: its copy
                try:  # else a file of another program the chunk points to: the same file, from the new folder
                    products[name] = os.path.relpath(where, new)
                except ValueError:  # another drive (Windows)
                    products[name] = where
            ch.products = products
            foreign = _strings_under([ch.meta, [os.path.normpath(os.path.join(new, r)) for r in products.values()]],
                                     src_files)
            if foreign:
                warnings.append(f"«{ch.label}»: usa {len(foreign)} file di chunk del progetto di origine non aggiunti "
                                f"(per esempio {foreign[0]}): restano lì, letti e mai modificati")
            sub(1.0, f"{ch.label}: {len(ch.cameras)} foto")
        site, mine = src.meta.get("sito"), doc.meta.get("sito")
        if site and site != mine:
            warnings.append(f"{path.name}: è del sito «{site}», questo progetto " + (f"del sito «{mine}»" if mine else
                            "non ha un sito") + ": il sito di questo progetto non cambia")
        if pairs:
            report["sections"] = source_sections(doc, path)
    except BaseException:
        _take_away(doc, added)
        raise
    return added


# --------------------------------------------------------------------------- all
class Results(list):
    """The :class:`Appended` of every source; ``journal``: the :meth:`.removal.ChunkRemoval.added` that lists the
    chunks appended (None when there is none); ``sections``: ``[(prefix, sections)]`` of the ``.ppg`` sources."""

    journal = None
    sections: list = []


def append_projects(doc, sources, chunks=None, webodm: bool = False, progress=None, journal=None) -> Results:
    """Every source (``.ppg``, ``.psx``, WebODM ``all.zip`` or task folder) appended to ``doc`` as new chunks, in the
    order given. ``chunks``: the chunks to take of the ``.ppg`` and ``.psx`` sources - a dict ``{source path: names or
    positions}`` (a source not in it: all its chunks), or a list of names for every one of them; ``webodm``: every
    source is a WebODM task. Each chunk made is written at once in ``journal`` (a :meth:`.removal.ChunkRemoval.added`;
    made here when None), before its folder is filled: the end of the session, or :func:`.removal.recover` after a
    crash, deletes the folders of those the project as saved does not list. A source that fails takes away the chunks
    it made - only those - and the others go on (its ``error``); a cancellation (:class:`.progress.Cancelled`) takes
    away every chunk this call made and goes on. A journal left with no chunk is discarded. The active chunk is not
    changed and ``doc`` is not saved. Returns :class:`Results`."""
    from .removal import ChunkRemoval

    progress = as_progress(progress)
    journal = journal if journal is not None else ChunkRemoval.added(doc)
    sources = [Path(s) for s in sources]
    results = Results()
    results.sections = []
    made_all: list = []
    try:
        for i, path in enumerate(sources):
            sub = progress.sub(i / max(len(sources), 1), 1 / max(len(sources), 1))
            result = Appended(path=path, kind=None)
            results.append(result)
            made: list = []

            def on_chunk(ch, made=made):
                made.append(ch)
                made_all.append(ch)
                journal.add(ch)

            try:
                result.kind = kind = source_kind(path, webodm)
                if not path.exists():
                    raise FileNotFoundError(f"{path} non esiste")
                wanted = chunks.get(str(path), chunks.get(path)) if isinstance(chunks, dict) else chunks
                if kind == "ppg":
                    report: dict = {}
                    result.chunks = append_ppg(doc, path, wanted, progress=sub, on_chunk=on_chunk, report=report)
                    result.warnings += report.get("warnings", [])
                    if report.get("sections"):
                        results.sections.append((path.stem, report["sections"]))
                elif kind == "psx":
                    from ..integrations.metashape import import_psx_into

                    names = [w for w in wanted if isinstance(w, str)] if wanted is not None else None
                    numbers = [w for w in wanted if not isinstance(w, str)] if wanted is not None else None
                    result.chunks = import_psx_into(doc, path, numbers if numbers else None, progress=sub,
                                                    chunk_labels=names if names else None, on_chunk=on_chunk)
                else:
                    from ..integrations.webodm import import_task_into

                    result.chunks = [import_task_into(doc, path, progress=sub, on_chunk=on_chunk)]
                for ch in result.chunks:
                    if kind != "ppg":
                        ch.label = unique_label(doc, ch.label, path.stem if path.suffix else path.name, ch)
                    result.warnings += list((ch.meta.get("import") or {}).get("warnings") or [])
                result.warnings = list(dict.fromkeys(result.warnings))
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - one source failing does not stop the others
                log.warning("Aggiunta di %s non riuscita: %s", path, exc, exc_info=True)
                _take_away(doc, made)
                journal.forget(made)
                result.chunks = []
                result.error = str(exc) or type(exc).__name__
    except BaseException:
        _take_away(doc, made_all)
        journal.forget(made_all)
        journal.discard()
        raise
    if journal.chunks:
        results.journal = journal
    else:
        journal.discard()
    progress(1.0, "Progetti aggiunti")
    return results


def _take_away(doc, chunks) -> None:
    """``chunks`` (made by the append) out of ``doc`` with their folders: never another chunk."""
    for ch in chunks:
        if any(ch is c for c in doc.chunks):
            doc.chunks.remove(ch)
        shutil.rmtree(doc.files_dir / f"chunk_{ch.key}", ignore_errors=True)


__all__ = ["Appended", "Results", "append_projects", "append_ppg", "source_kind", "source_chunks", "unique_label",
           "source_sections", "merge_sections", "write_sections", "KINDS"]
