"""Undo and redo of the edits of a project (Modifica > Annulla / Ripeti).

Two kinds of changes are kept:

* what lives in the project - drawings (units, spot heights, sections, edits waiting for
  PyArchInit), markers, scale bars, photos where a marker is not to be estimated, patches of the
  orthomosaic, the reconstruction region: the window gives a snapshot of them (``get_state``, a
  dict of JSON-able parts) and :meth:`UndoHistory.record` is called whenever the project is
  modified; the parts that changed since the last snapshot become an entry, and undo hands their
  previous value back to the window (``set_state``);
* products changed in place - the dense cloud (classes by hand, deleted or kept points, cleaning),
  the model (deleted faces, cleaning), the tie points: their files are copied aside before the
  edit (:meth:`backup`; on APFS a clone, instant and taking no space) and swapped back on undo,
  the edited ones kept for redo.

The history belongs to the open chunk: it starts again when another project or chunk is opened,
and its copies (``<chunk data>/undo``) are deleted then. A third kind of entry undoes and redoes
itself (:meth:`UndoHistory.push`): the removals from the workspace (``core.removal``), which act
on any chunk and stay in the history when another chunk becomes the active one.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..io.files import clone  # noqa: F401 - also the name the window and tests know
from .i18n import tr

log = logging.getLogger("pyarchinit_photogrammetry")

PART_NAMES = {"units": "Disegni", "labels": "Etichette", "quote": "Quote", "sections": "Sezioni", "pa": "Modifiche per PyArchInit",
              "markers": "Marker", "excluded": "Marker", "scalebars": "Barre di scala",
              "patches": "Patch dell'ortofoto", "region": "Regione di ricostruzione"}
PRODUCT_NAMES = {"point_cloud": "Nuvola densa", "model": "Modello", "tie_points": "Punti di legame",
                 "reconstruction": "Punti di legame", "model_textured": "Texture"}


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


@dataclass
class Entry:
    text: str
    before: dict  # part -> JSON text
    after: dict
    files: dict | None = None  # {"folder", "before": {product: {"rel", "copy"}}, "after": ... or None}
    extra: dict = field(default_factory=dict)
    action: object | None = None  # an entry that undoes itself: action.undo(), action.redo() (push)
    document: bool = False  # kept when another chunk becomes the active one


class UndoHistory:
    """The entries of a chunk; ``index`` entries are applied (the next undo is ``index - 1``)."""

    def __init__(self, get_state, set_state, get_chunk, limit: int = 50, file_limit: int = 10, on_change=None):
        self.get_state, self.set_state, self.get_chunk = get_state, set_state, get_chunk
        self.limit, self.file_limit, self.on_change = limit, file_limit, on_change
        self.entries: list[Entry] = []
        self.index = 0
        self.applying = False
        self._base: dict = {}
        self._pending: dict | None = None
        self.reset()

    # ------------------------------------------------------------------ state
    def _snapshot(self) -> dict:
        state = self.get_state() or {}
        return {k: json.dumps(v, sort_keys=True, default=str) for k, v in state.items()}

    def reset(self, keep_document: bool = False, keep_entry=None) -> None:
        """Forget everything (another project or chunk is open) and delete the copies;
        ``keep_document``: the entries of the project that can still be undone (removals from the
        workspace) stay, as when another chunk becomes the active one - those ``keep_entry(entry)``
        says yes to, when it is given."""
        keep = [e for e in self.entries[:self.index] if e.document and (keep_entry is None or keep_entry(e))] \
            if keep_document else []
        for e in self.entries:
            if not any(e is k for k in keep):
                self._drop_files(e)
        if self._pending:
            shutil.rmtree(self._pending["folder"], ignore_errors=True)
        self.entries, self.index, self._pending = keep, len(keep), None
        self._base = self._snapshot()
        ch = self.get_chunk()
        if ch is not None:
            try:
                shutil.rmtree(ch.data_dir / "undo", ignore_errors=True)
            except Exception:  # pragma: no cover - a chunk without a folder
                pass
        self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def can_undo(self) -> bool:
        return self.index > 0

    def can_redo(self) -> bool:
        return self.index < len(self.entries)

    def undo_text(self) -> str:
        return self.entries[self.index - 1].text if self.can_undo() else ""

    def redo_text(self) -> str:
        return self.entries[self.index].text if self.can_redo() else ""

    # ------------------------------------------------------------------ recording
    def backup(self, products) -> None:
        """Before an edit that changes the files of ``products`` in place: copies of them."""
        ch = self.get_chunk()
        if ch is None:
            return
        if self._pending:
            shutil.rmtree(self._pending["folder"], ignore_errors=True)
        folder = ch.data_dir / "undo" / uuid.uuid4().hex[:12]
        before = {}
        for name in products:
            path = ch.product_path(name)
            if path is None or not path.exists():
                continue
            copy = folder / "before" / name / path.name
            try:
                clone(path, copy)
            except OSError as exc:
                log.warning("Annulla: copia di %s non riuscita (%s)", path, exc)
                continue
            before[name] = {"rel": ch.products[name], "copy": str(copy), "pages": self._pages(ch, name, folder / "before")}
        self._pending = {"folder": str(folder), "before": before, "after": None} if before else None

    @staticmethod
    def _pages(ch, name: str, folder: Path) -> list:
        """The other pages of a texture of several pages, copied beside the first one."""
        if name != "model_textured":
            return []
        from ..engine.texturing import texture_pages

        out = []
        for page in texture_pages(ch)[1:]:
            if page.exists():
                copy = folder / name / page.name
                clone(page, copy)
                out.append({"rel": str(page.relative_to(ch.data_dir)) if page.is_relative_to(ch.data_dir)
                            else str(page), "copy": str(copy)})
        return out

    @property
    def pending(self) -> bool:
        """Files were copied for an edit not recorded yet."""
        return self._pending is not None

    def rebase(self) -> None:
        """The project changed by processing, not by an edit (an alignment moves the markers):
        the next entry starts from here."""
        if not self.applying:
            self._base = self._snapshot()

    def discard_pending(self) -> None:
        """The edit announced by :meth:`backup` did not happen (failed or cancelled)."""
        if self._pending:
            shutil.rmtree(self._pending["folder"], ignore_errors=True)
        self._pending = None

    def record(self, text: str | None = None) -> bool:
        """After a change: what differs from the last snapshot (and the files copied by
        :meth:`backup`) becomes an entry. Returns whether one was added."""
        if self.applying:
            return False
        new = self._snapshot()
        if new.get("chunk") != self._base.get("chunk"):  # another chunk: its own history
            self.discard_pending()
            self.reset(keep_document=True)
            return False
        parts = [k for k in new if k != "chunk" and new.get(k) != self._base.get(k)]
        files = self._pending
        self._pending = None
        if not parts and not files:
            return False
        if not text:
            names = []
            for k in parts:
                if tr(PART_NAMES.get(k, k)) not in names:
                    names.append(tr(PART_NAMES.get(k, k)))
            for k in (files or {}).get("before", {}):
                if tr(PRODUCT_NAMES.get(k, k)) not in names:
                    names.append(tr(PRODUCT_NAMES.get(k, k)))
            text = ", ".join(names)
        for e in self.entries[self.index:]:  # a new change: what was undone cannot be redone
            self._drop_files(e)
        del self.entries[self.index:]
        self.entries.append(Entry(text, {k: self._base.get(k) for k in parts}, {k: new[k] for k in parts}, files))
        self._base = new
        self._trim()
        self._changed()
        return True

    def push(self, text: str, action, document: bool = True) -> None:
        """A step already done that undoes and redoes itself: ``action.undo()``, ``action.redo()`` and,
        if it has one, ``action.drop()`` when the history forgets it (a removal from the workspace).
        ``document``: it stays when another chunk becomes the active one. What was undone cannot be
        redone after it."""
        for e in self.entries[self.index:]:
            self._drop_files(e)
        del self.entries[self.index:]
        self.entries.append(Entry(text, {}, {}, None, action=action, document=document))
        self._base = self._snapshot()
        self._trim()
        self._changed()

    def _trim(self) -> None:
        """At most ``limit`` entries, the copies of files of the last ``file_limit`` only."""
        while len(self.entries) > self.limit:
            self._drop_files(self.entries.pop(0))
        with_files = [e for e in self.entries if e.files]
        for e in with_files[:-self.file_limit] if len(with_files) > self.file_limit else []:
            self._drop_files(e)
            e.files = None
            e.extra["files_dropped"] = True
        self.index = len(self.entries)

    # ------------------------------------------------------------------ undo / redo
    def undo(self) -> str | None:
        if not self.can_undo():
            return None
        e = self.entries[self.index - 1]
        if e.action is not None:
            self._run(e, self.index - 1, "undo")
        else:
            self._apply(e.before, e.files, "before")
        self.index -= 1
        self._changed()
        return e.text

    def redo(self) -> str | None:
        if not self.can_redo():
            return None
        e = self.entries[self.index]
        if e.action is not None:
            self._run(e, self.index, "redo")
        else:
            self._apply(e.after, e.files, "after")
        self.index += 1
        self._changed()
        return e.text

    def _run(self, e: Entry, i: int, how: str) -> None:
        """``action.undo()`` or ``redo()`` of the entry at ``i``. When it fails the entry goes, with what
        was undone after it (it cannot be redone), and the error goes on to the caller."""
        self.applying = True
        try:
            getattr(e.action, how)()
        except Exception:
            self.applying = False
            for gone in self.entries[i:]:
                self._drop_files(gone)
            del self.entries[i:]
            self.index = i
            self._base = self._snapshot()
            self._changed()
            raise
        finally:
            self.applying = False
        self._base = self._snapshot()

    def _apply(self, parts: dict, files: dict | None, side: str) -> None:
        self.applying = True
        try:
            if files:
                self._swap_files(files, side)
            state = {k: (None if v is None else json.loads(v)) for k, v in parts.items()}
            self.set_state(state, set(state), sorted((files or {}).get("before", {})))
        finally:
            self.applying = False
        self._base = self._snapshot()

    def _swap_files(self, files: dict, side: str) -> None:
        ch = self.get_chunk()
        if ch is None:
            return
        if side == "before" and files.get("after") is None:  # first undo: the edited files kept for redo
            after = {}
            for name in files["before"]:
                path = ch.product_path(name)
                if path is None or not path.exists():
                    continue
                copy = Path(files["folder"]) / "after" / name / path.name
                clone(path, copy)
                after[name] = {"rel": ch.products[name], "copy": str(copy),
                               "pages": self._pages(ch, name, Path(files["folder"]) / "after")}
            files["after"] = after
        for name, saved in (files.get(side) or {}).items():
            dst = ch.data_dir / saved["rel"]
            _remove(dst)
            clone(Path(saved["copy"]), dst)
            ch.products[name] = saved["rel"]
            for page in saved.get("pages") or []:
                target = ch.data_dir / page["rel"]
                _remove(target)
                clone(Path(page["copy"]), target)

    def _drop_files(self, e: Entry) -> None:
        if e.files:
            shutil.rmtree(e.files["folder"], ignore_errors=True)
        drop = getattr(e.action, "drop", None)
        if drop is not None:
            try:
                drop()
            except Exception as exc:  # noqa: BLE001 - forgetting an entry never stops the window
                log.warning("Annulla: %s non dimenticato (%s)", e.text, exc)


# ---------------------------------------------------------------- a script, undone as one step
SKIPPED_FOLDERS = {".script_undo", ".removed", "undo", "versions"}  # never "new files" of a script


class ScriptChanged(RuntimeError):
    """The project changed after the script outside its own entry (a step that is not in the history): undoing
    it now would take back half of it. The entry leaves the history instead."""


def snapshot_document(doc) -> dict:
    """The project as JSON-able data: ``{chunk key: chunk.to_dict()}`` (deep copies), ``"meta"`` the project's
    and ``"order"`` the keys of the chunks in their order."""
    import copy

    out: dict = {c.key: copy.deepcopy(c.to_dict()) for c in doc.chunks}
    out["meta"] = copy.deepcopy(doc.meta)
    out["order"] = [c.key for c in doc.chunks]
    return out


def snapshot_text(snapshot: dict) -> str:
    """A snapshot as one text, to compare two of them (NaN equal to NaN, keys of any type)."""
    return json.dumps({str(k): v for k, v in snapshot.items()}, sort_keys=True, default=str)


TOLERANCE = 1e-6  # numbers this close are the same when undo checks the project (a value computed again)


def first_difference(a, b, path: str = "") -> str | None:
    """Where two JSON-like values differ (None: they are the same): dicts by their keys whatever the order, lists
    item by item, numbers within :data:`TOLERANCE` (NaN equal to NaN), strings and everything else exactly."""
    import math

    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return f"{path}: keys {sorted(map(str, set(a) ^ set(b)))[:5]}"
        for k in a:
            found = first_difference(a[k], b[k], f"{path}/{k}")
            if found:
                return found
        return None
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return f"{path}: {len(a)} items, {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            found = first_difference(x, y, f"{path}/{i}")
            if found:
                return found
        return None
    numbers = (int, float)
    if isinstance(a, numbers) and isinstance(b, numbers) and not isinstance(a, bool) and not isinstance(b, bool):
        if (isinstance(a, float) and math.isnan(a)) or (isinstance(b, float) and math.isnan(b)):
            return None if (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b)) \
                else f"{path}: {a!r} != {b!r}"
        return None if abs(a - b) <= TOLERANCE else f"{path}: {a!r} != {b!r}"
    return None if a == b and type(a) is type(b) else f"{path}: {a!r} != {b!r}"


def chunk_keys(snapshot: dict) -> list:
    return list(snapshot.get("order") or [k for k in snapshot if k not in ("meta", "order")])


def files_under(folder) -> set[Path]:
    """The files under ``folder`` (paths), the folders of versions, of the copies of Modifica > Annulla and of
    the removals left out."""
    folder = Path(folder)
    out: set[Path] = set()
    if not folder.exists():
        return out
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIPPED_FOLDERS]
        out.update(Path(root) / f for f in files)
    return out


def product_stamps(doc) -> dict:
    """Size and modification time of the file of every product of every chunk: a product written again shows."""
    out = {}
    for c in doc.chunks:
        for name, rel in sorted(c.products.items()):
            try:
                st = (doc.files_dir / f"chunk_{c.key}" / rel).stat()
            except OSError:
                out[f"{c.key}/{name}"] = None
            else:
                out[f"{c.key}/{name}"] = [st.st_size, st.st_mtime_ns]
    return out


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    _remove(dst)
    shutil.move(str(src), str(dst))


class ScriptAction:
    """An entry of Modifica > Annulla for a script (``history.push(label, action, document=True)``): everything
    it changed in the project goes back in one step, and comes again with Ripeti.

    ``before``/``after``: :func:`snapshot_document` before and after the script - every chunk (cameras, markers,
    scale bars, products, meta) and the project's meta; ``versions``: ``(chunk key, product, version id)`` of
    the products the script wrote again, kept first (``core.versions.keep_during``): undo makes them active again
    and puts the script's products aside, redo swaps them back; ``new_files``: files the script wrote under the
    project's folder (not products, not versions): moved aside and back; ``new_chunks``: the keys of the chunks it
    added (out of the project, their folder aside); ``removed_chunks``: ``{key: removal}`` of the chunks it
    removed, each a ``core.removal.ChunkRemoval`` that set its folder aside (undone and redone with it).
    ``kept_folders``: ``(chunk key, product, id)`` of versions the script activated or deleted, whose files stay
    for the undo. What is put aside lives in ``<project files>/.script_undo`` while the entry can be redone.

    Undo and redo first check that the project is as the script (or its undo) left it - ``state`` (a text of the
    snapshot and the stamps of the products' files, :meth:`state`); when another step changed it meanwhile they
    raise :class:`ScriptChanged` and the history drops the entry: never half an undo.

    :meth:`drop` (the entry falls out of the history): the versions made are deleted when the script's products are
    the active ones - unless the project was saved since (its file lists them) -, what was put aside when they are
    not."""

    def __init__(self, window, before: dict, after: dict, versions: list[tuple[int, str, str]], new_files: list[Path],
                 new_chunks: list[int], removed_chunks: dict, kept_folders: list | None = None):
        self.window, self.before, self.after = window, before, after
        self.versions = list(versions)
        self.new_files, self.new_chunks, self.removed_chunks = list(new_files), list(new_chunks), dict(removed_chunks)
        self.kept_folders = list(kept_folders or [])
        self.doc = window.doc
        self.root = self.doc.files_dir
        self.hold = self.root / ".script_undo" / uuid.uuid4().hex[:12]
        self.held: dict = {}  # (key, product, vid) -> entry of the script's product put aside
        self.applied = True
        self.saves = self.doc.saves
        self.expected = self.state()

    # ------------------------------------------------------------------ helpers
    def state(self) -> dict:
        """The project now, as JSON data (the snapshot and the stamps of the products' files): what undo and redo
        expect to find."""
        self.window._store_drawings()
        return {"project": json.loads(snapshot_text(snapshot_document(self.doc))), "files": product_stamps(self.doc)}

    def _check(self) -> None:
        if self.window.doc is not self.doc:
            raise ScriptChanged("another project is open")
        found = first_difference(self.expected, self.state())
        if found:
            log.info("Annulla dello script: il progetto è cambiato dopo (%s)", found)
            raise ScriptChanged(f"the project changed after the script: {found}")

    def _chunk(self, key):
        return next((c for c in self.doc.chunks if c.key == key), None)

    def _set_chunk(self, chunk, data: dict) -> None:
        """``chunk`` as ``data`` says, the same object (the window holds it)."""
        import copy

        from ..core.chunk import Chunk

        new = Chunk.from_dict(copy.deepcopy(data), self.doc)
        for name in ("label", "enabled", "crs", "transform", "sensors", "cameras", "markers", "scalebars",
                     "products", "meta"):
            setattr(chunk, name, getattr(new, name))
        chunk._tie_points = None
        chunk._surface_cache = {}

    def _set_document(self, snap: dict) -> None:
        import copy

        from ..core.chunk import Chunk

        doc = self.doc
        chunks = []
        for key in chunk_keys(snap):
            ch = self._chunk(key)
            if ch is None:
                ch = Chunk.from_dict(copy.deepcopy(snap[key]), doc)
            else:
                self._set_chunk(ch, snap[key])
            chunks.append(ch)
        doc.chunks[:] = chunks
        doc.meta.clear()
        doc.meta.update(copy.deepcopy(snap["meta"]))

    def _version_entry(self, key, product: str, vid: str) -> dict:
        from ..core import versions as v

        ch = self._chunk(key)
        if product in v.PRODUCTS:
            for e in ((self.after.get(key) or {}).get("meta", {}).get("versions") or {}).get(product, []):
                if e["id"] == vid:
                    return e
            raise KeyError(f"{product}: no version {vid}")
        return v.copied_entry(ch, product, vid, (self.before.get(key) or {}).get("products", {}).get(product))

    def _rel(self, path: Path) -> Path:
        return Path(path).relative_to(self.root)

    def _removals(self) -> list:
        seen, out = set(), []
        for r in self.removed_chunks.values():
            if id(r) not in seen:
                seen.add(id(r))
                out.append(r)
        return out

    # ------------------------------------------------------------------ undo / redo
    def undo(self) -> None:
        from ..core import versions as v

        self._check()
        self.window._before_script_step()
        for path in self.new_files:
            path = Path(path)
            if path.exists() or path.is_symlink():
                _move(path, self.hold / "files" / self._rel(path))
        for key in self.new_chunks:
            folder = self.root / f"chunk_{key}"
            if folder.exists():
                _move(folder, self.hold / "chunks" / folder.name)
        for r in reversed(self._removals()):  # the chunks it removed, back with their folder
            r.undo()
        for key, product, vid in reversed(self.versions):
            ch = self._chunk(key)
            if ch is None:
                continue
            entry = self._version_entry(key, product, vid)
            origin = (self.after.get(key) or {}).get("products", {}).get(product)
            held = v.set_aside(ch, product, f"{vid}-script", origin=origin)
            if held is not None:
                self.held[(key, product, vid)] = held
            v.bring_back(ch, product, entry)
        self._set_document(self.before)
        self.applied = False
        self.window._after_script_step()
        self.expected = self.state()

    def redo(self) -> None:
        from ..core import versions as v

        self._check()
        self.window._before_script_step()
        for key in self.new_chunks:
            folder = self.hold / "chunks" / f"chunk_{key}"
            if folder.exists():
                _move(folder, self.root / folder.name)
        for key, product, vid in self.versions:
            ch = self._chunk(key)
            if ch is None:
                continue
            origin = (self.before.get(key) or {}).get("products", {}).get(product)
            v.set_aside(ch, product, vid, origin=origin)  # the product as it was before: its version again
            held = self.held.pop((key, product, vid), None)
            if held is not None:
                v.bring_back(ch, product, held)
        for path in self.new_files:
            src = self.hold / "files" / self._rel(Path(path))
            if src.exists():
                _move(src, Path(path))
        for r in self._removals():
            r.redo()
        self._set_document(self.after)
        self.applied = True
        self.window._after_script_step()
        self.expected = self.state()

    def drop(self) -> None:
        from ..core import versions as v

        same = self.window.doc is self.doc
        if self.applied and same and self.doc.saves == self.saves:  # the versions kept for the undo go
            for key, product, vid in self.versions:
                ch = self._chunk(key)
                if ch is None:
                    continue
                if product in v.PRODUCTS:
                    if any(e["id"] == vid for e in v.versions(ch, product)):
                        v.delete(ch, product, vid)
                else:
                    shutil.rmtree(ch.data_dir / "versions" / product / vid, ignore_errors=True)
            if self.versions:
                self.window.setWindowModified(True)
        elif not self.applied:  # undone: the script's products put aside go
            for (key, product, _vid), held in self.held.items():
                ch = self._chunk(key)
                if ch is not None:
                    shutil.rmtree(ch.data_dir / "versions" / product / held["id"], ignore_errors=True)
            self.held.clear()
        if self.applied and same and self.doc.saves == self.saves:
            # versions the script activated or deleted: their files go now, when the list no longer names them
            for key, product, vid in self.kept_folders:
                ch = self._chunk(key)
                if ch is not None and not any(e["id"] == vid for e in v.versions(ch, product)):
                    shutil.rmtree(ch.data_dir / "versions" / product / vid, ignore_errors=True)
        shutil.rmtree(self.hold, ignore_errors=True)
