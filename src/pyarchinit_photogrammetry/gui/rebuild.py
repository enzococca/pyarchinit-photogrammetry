"""*Ricostruisci i prodotti da rifare* as ONE step of *Modifica > Annulla*.

The rebuild writes again the DEM, the orthomosaic, the elevations, the reliefs, the TIN... of a chunk, and may keep the
products it replaces as versions. :class:`RebuildStep` takes, before the task, a copy of the files it is going to write
(clones: on APFS instant and taking no space) and of ``chunk.meta`` and ``chunk.products``; after it, what changed.
Undo puts the files and the parts of the meta that changed back (the stamps too: the products read as *da rifare*
again) and sets aside what the rebuild added (new files, the versions it kept); redo swaps them back.

As :class:`.undo.ScriptAction`, undo and redo first check that the chunk is as the step left it: when another step
changed those parts meanwhile (a product built again by hand) they raise :class:`RebuildChanged` and the history drops
the entry: never a DEM of before put back over one built since.
"""
from __future__ import annotations

import copy
import json
import logging
import shutil
import uuid
from pathlib import Path

from ..core import versions as versions_mod
from ..io.files import clone
from .undo import ScriptChanged, _move, _remove

log = logging.getLogger("pyarchinit_photogrammetry")

SKIPPED_KEYS = ("drawings",)  # chunk.meta the rebuild never changes (the window stores its drawings there meanwhile)


class RebuildChanged(ScriptChanged):
    """The chunk changed after the rebuild outside its own entry: it cannot be undone or redone any more."""


def _text(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def chunk_files(chunk, items) -> list[str]:
    """The files and folders (relative to the chunk's folder) the rebuild of ``items`` (as
    :func:`..core.stamps.stale_products` gives them) writes again: the products named, the files beside the TIN, the
    rasters of the reliefs, the folders of the elevations. Files outside the chunk's folder (an orthomosaic of another
    program) are never written: left out."""
    root = Path(chunk.data_dir)
    names: list[str] = []
    rels: list[str] = []
    for item in items or []:
        kind, product = item.get("kind"), item.get("product")
        if kind == "product":
            names.append(product)
            if product == "dem" and (chunk.meta.get("dem") or {}).get("source") == "tin":
                names.append("tin")
        elif kind == "relief":
            names.append(product)
        elif kind == "elevation" and item.get("id"):
            rels.append(str(Path("elevations") / str(item["id"])))
    for name in dict.fromkeys(names):
        path = chunk.product_path(name)
        if path is None:
            continue
        paths = [Path(path)] + [Path(path).parent / side for side in versions_mod.SIDECARS.get(name, ())]
        for p in paths:
            try:
                rels.append(str(p.resolve().relative_to(root.resolve())))
            except ValueError:  # another program's file: the rebuild writes its own
                continue
    return list(dict.fromkeys(rels))


class RebuildStep:
    """The rebuild of ``chunk`` as one entry of *Modifica > Annulla* (``history.push(label, step, document=False)``):
    made before the task (copies of ``files``, relative to the chunk's folder), completed after it (:meth:`finish`)."""

    def __init__(self, window, chunk, files):
        self.window, self.chunk = window, chunk
        self.root = Path(chunk.data_dir)
        self.hold = self.root / "undo" / f"rebuild-{uuid.uuid4().hex[:12]}"
        self.before_meta = copy.deepcopy(chunk.meta)
        self.before_products = dict(chunk.products)
        self.files: list[str] = []
        for rel in files:
            src = self.root / rel
            if src.exists():
                clone(src, self.hold / "before" / rel)
            self.files.append(rel)
        self.existed = {rel: (self.root / rel).exists() for rel in self.files}
        self.after_meta: dict = {}
        self.after_products: dict = {}
        self.after_existed: dict = {}
        self.keys: list[str] = []
        self.new: list[str] = []
        self.applied = True
        self.signature: dict = self._signature()  # of the files before: what the task wrote shows in finish

    # ------------------------------------------------------------------ after the task
    def finish(self) -> bool:
        """What the rebuild changed: the parts of ``chunk.meta``, the files it added (new products, the versions it
        kept). False when nothing changed (no entry)."""
        ch = self.chunk
        self.after_meta = copy.deepcopy(ch.meta)
        self.after_products = dict(ch.products)
        keys = set(self.before_meta) | set(self.after_meta)
        self.keys = sorted(k for k in keys if k not in SKIPPED_KEYS
                           and _text(self.before_meta.get(k)) != _text(self.after_meta.get(k)))
        new = []
        known = set(self.files)
        for name, rel in self.after_products.items():
            path = self.root / rel
            if self.before_products.get(name) == rel or rel in known or not path.exists():
                continue
            try:
                new.append(str(path.resolve().relative_to(self.root.resolve())))
            except ValueError:  # a file of another program: never moved
                continue
        for product in versions_mod.PRODUCTS:
            ids = {e.get("id") for e in ((self.before_meta.get("versions") or {}).get(product) or [])}
            for e in (self.after_meta.get("versions") or {}).get(product) or []:
                folder = Path("versions") / product / str(e.get("id"))
                if e.get("id") not in ids and (self.root / folder).exists():
                    new.append(str(folder))
        self.new = list(dict.fromkeys(new))
        written = self._signature()
        changed = any(written.get(rel) != self.signature.get(rel) for rel in self.files)
        self.signature = written
        return bool(self.keys or self.new or changed or self.before_products != self.after_products)

    # ------------------------------------------------------------------ helpers
    def _signature(self) -> dict:
        """Size and modification time of every file the step swaps: a product written again meanwhile shows."""
        out = {}
        for rel in self.files + self.new:
            path = self.root / rel
            found = [path] if path.is_file() else sorted(q for q in path.rglob("*") if q.is_file()) \
                if path.is_dir() else []
            out[rel] = [[str(q.relative_to(self.root)), q.stat().st_size, q.stat().st_mtime_ns] for q in found]
        return out

    def _state(self, meta: dict, products: dict) -> str:
        return _text({"meta": {k: meta.get(k) for k in self.keys}, "products": products})

    def _check(self, meta: dict, products: dict, signature: dict) -> None:
        if not any(c is self.chunk for c in getattr(self.window.doc, "chunks", [])):
            raise RebuildChanged("the chunk is no longer in the project")
        if self._state(self.chunk.meta, self.chunk.products) != self._state(meta, products) or \
                self._signature() != signature:
            log.info("Annulla della ricostruzione: il chunk è cambiato dopo")
            raise RebuildChanged("the chunk changed after the rebuild")

    def _set(self, meta: dict, products: dict) -> None:
        ch = self.chunk
        for k in self.keys:
            if k in meta:
                ch.meta[k] = copy.deepcopy(meta[k])
            else:
                ch.meta.pop(k, None)
        ch.products.clear()
        ch.products.update(products)

    def _release(self) -> None:
        if self.window.chunk is self.chunk:
            self.window._release_views()

    def _reload(self) -> None:
        if self.window.chunk is self.chunk:
            self.window.refresh_all()
        self.window._dirty(True)

    # ------------------------------------------------------------------ undo / redo
    def undo(self) -> None:
        self._check(self.after_meta, self.after_products, self.signature)
        self._release()
        after = self.hold / "after"
        shutil.rmtree(after, ignore_errors=True)
        for rel in self.new:  # what the rebuild added, aside
            path = self.root / rel
            if path.exists():
                _move(path, after / "new" / rel)
        self.after_existed = {}
        for rel in self.files:
            path = self.root / rel
            self.after_existed[rel] = path.exists()
            if path.exists():
                _move(path, after / "files" / rel)
            if self.existed[rel]:
                clone(self.hold / "before" / rel, path)
        self._set(self.before_meta, self.before_products)
        self.applied = False
        self.signature = self._signature()
        self._reload()

    def redo(self) -> None:
        self._check(self.before_meta, self.before_products, self.signature)
        self._release()
        after = self.hold / "after"
        for rel in self.files:
            path = self.root / rel
            if path.exists() or path.is_symlink():
                _remove(path)  # the clone of before (its copy stays in the hold)
            if self.after_existed.get(rel):
                _move(after / "files" / rel, path)
        for rel in self.new:
            src = after / "new" / rel
            if src.exists():
                _move(src, self.root / rel)
        self._set(self.after_meta, self.after_products)
        self.applied = True
        self.signature = self._signature()
        self._reload()

    def drop(self) -> None:
        shutil.rmtree(self.hold, ignore_errors=True)


__all__ = ["RebuildStep", "RebuildChanged", "chunk_files"]
