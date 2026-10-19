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
and its copies (``<chunk data>/undo``) are deleted then.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("pyarchinit_photogrammetry")

PART_NAMES = {"units": "Disegni", "quote": "Quote", "sections": "Sezioni", "pa": "Modifiche per PyArchInit",
              "markers": "Marker", "excluded": "Marker", "scalebars": "Barre di scala",
              "patches": "Patch dell'ortofoto", "region": "Regione di ricostruzione"}
PRODUCT_NAMES = {"point_cloud": "Nuvola densa", "model": "Modello", "tie_points": "Punti di legame",
                 "reconstruction": "Punti di legame", "model_textured": "Texture"}


def clone(src: Path, dst: Path) -> None:
    """Copy a file or a folder; on macOS a copy-on-write clone (instant, no space taken)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        try:
            import ctypes

            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            if libc.clonefile(os.fsencode(str(src)), os.fsencode(str(dst)), 0) == 0:
                return
        except Exception:  # pragma: no cover - another file system: a plain copy
            pass
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


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

    def reset(self) -> None:
        """Forget everything (another project or chunk is open) and delete the copies."""
        for e in self.entries:
            self._drop_files(e)
        if self._pending:
            shutil.rmtree(self._pending["folder"], ignore_errors=True)
        self.entries, self.index, self._pending = [], 0, None
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
            before[name] = {"rel": ch.products[name], "copy": str(copy)}
        self._pending = {"folder": str(folder), "before": before, "after": None} if before else None

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
            self.reset()
            return False
        parts = [k for k in new if k != "chunk" and new.get(k) != self._base.get(k)]
        files = self._pending
        self._pending = None
        if not parts and not files:
            return False
        if not text:
            names = []
            for k in parts:
                if PART_NAMES.get(k, k) not in names:
                    names.append(PART_NAMES.get(k, k))
            for k in (files or {}).get("before", {}):
                if PRODUCT_NAMES.get(k, k) not in names:
                    names.append(PRODUCT_NAMES.get(k, k))
            text = ", ".join(names)
        for e in self.entries[self.index:]:  # a new change: what was undone cannot be redone
            self._drop_files(e)
        del self.entries[self.index:]
        self.entries.append(Entry(text, {k: self._base.get(k) for k in parts}, {k: new[k] for k in parts}, files))
        self._base = new
        while len(self.entries) > self.limit:
            self._drop_files(self.entries.pop(0))
        with_files = [e for e in self.entries if e.files]
        for e in with_files[:-self.file_limit] if len(with_files) > self.file_limit else []:
            self._drop_files(e)
            e.files = None
            e.extra["files_dropped"] = True
        self.index = len(self.entries)
        self._changed()
        return True

    # ------------------------------------------------------------------ undo / redo
    def undo(self) -> str | None:
        if not self.can_undo():
            return None
        e = self.entries[self.index - 1]
        self._apply(e.before, e.files, "before")
        self.index -= 1
        self._changed()
        return e.text

    def redo(self) -> str | None:
        if not self.can_redo():
            return None
        e = self.entries[self.index]
        self._apply(e.after, e.files, "after")
        self.index += 1
        self._changed()
        return e.text

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
                after[name] = {"rel": ch.products[name], "copy": str(copy)}
            files["after"] = after
        for name, saved in (files.get(side) or {}).items():
            dst = ch.data_dir / saved["rel"]
            _remove(dst)
            clone(Path(saved["copy"]), dst)
            ch.products[name] = saved["rel"]

    def _drop_files(self, e: Entry) -> None:
        if e.files:
            shutil.rmtree(e.files["folder"], ignore_errors=True)
