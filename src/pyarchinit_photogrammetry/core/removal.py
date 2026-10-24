"""Removing parts of a project in a way that can be undone while the project is open.

What a removal takes away - whole chunks (:class:`ChunkRemoval`), or things inside one chunk
(:class:`ItemsRemoval`): the tie points with the alignment, the depth maps, the dense cloud, the
model, its texture, the DEM, the orthomosaic, the relief rasters, the products of the
multispectral and thermal captures, a kept version, an elevation, photos, markers, scale bars -
leaves the project at once, and its files are moved aside inside the project's files folder,
``<project>.ppg.files/.removed/<id>/`` (on the same disk: a rename, instant whatever the size).
``undo`` puts everything back, ``redo`` takes it away again.

A removal never moves or deletes a file outside the chunk's own folder: a DEM or an orthomosaic
exported by Metashape that an imported project points to only stops being part of the project.

A product built again after its removal (a new DEM where the removed one was) is not lost by an
undo: it is moved aside in its turn (the *absent* side of the removal) and comes back with a redo,
as Modifica > Annulla swaps the files of an edit.

The files are made final when the session ends (:meth:`Document.settle_removals`, called by the
window when it closes or opens another project): what the project as last saved lists is put back
in place, the rest is deleted, and the folder ``.removed`` is emptied. A session that ended
without that (a crash) is settled when the project is opened again (:func:`recover`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path

log = logging.getLogger("pyarchinit_photogrammetry")

TRASH = ".removed"  # inside the files folder of the project
MANIFEST = "removed.json"
PRESENT, ABSENT = "present", "absent"  # the item as it was / what is in its place after the removal
REMOVED, RESTORED = "removed", "restored"
RELIEF = ("svf", "openness_pos", "openness_neg", "slrm", "msrm", "multi_hillshade")
FAMILY = {"model": ("model", "model_textured"), "tie_points": ("tie_points", "reconstruction")}


class RemovalConflict(RuntimeError):
    """Something taken away cannot come back: its place is taken (photos with the same keys added since)."""


# ------------------------------------------------------------------------------ files
def _rel(path: Path, root: Path) -> str | None:
    """``path`` relative to ``root`` (POSIX), or None when it is outside it."""
    try:
        return Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root))).as_posix()
    except ValueError:
        return None


def _move(src: Path, dst: Path) -> None:
    """A file or a folder moved; inside the files folder of a project a rename, instant."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:  # another disk (a project saved elsewhere meanwhile): copy and delete
        shutil.move(str(src), str(dst))


def _free(place: Path) -> bool:
    """Nothing at ``place``, or an empty folder (``Chunk.data_dir`` makes its folder when asked) that goes."""
    if not place.exists() and not place.is_symlink():
        return True
    if place.is_dir() and not place.is_symlink() and not any(place.iterdir()):
        place.rmdir()
        return True
    return False


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _with_sidecars(path: Path) -> list[Path]:
    """A file and what GDAL and the program keep beside it (``dem.tif.aux.xml``, ``dem.tif.ovr``)."""
    out = [path] if path.exists() else []
    if path.is_file():
        out += sorted(q for q in path.parent.iterdir() if q.name.startswith(path.name + ".") and q != path)
    return out


def _outermost(rels: list[str]) -> list[str]:
    """Paths without repetitions and without those inside another one of the list."""
    out: list[str] = []
    for r in sorted(set(rels), key=lambda s: (s.count("/"), s)):
        if not any(r == o or r.startswith(o + "/") for o in out):
            out.append(r)
    return out


# ------------------------------------------------------------------------------ chunk.meta
_MISSING = object()


def _get(meta: dict, path: tuple):
    d = meta
    for k in path[:-1]:
        d = d.get(k) if isinstance(d, dict) else None
        if not isinstance(d, dict):
            return _MISSING
    return d.get(path[-1], _MISSING) if isinstance(d, dict) else _MISSING


def _pop(meta: dict, path: tuple) -> None:
    d = meta
    for k in path[:-1]:
        d = d.get(k) if isinstance(d, dict) else None
        if not isinstance(d, dict):
            return
    d.pop(path[-1], None)


def _put(meta: dict, path: tuple, value) -> None:
    d = meta
    for k in path[:-1]:
        if not isinstance(d.get(k), dict):
            d[k] = {}
        d = d[k]
    d[path[-1]] = value


# ------------------------------------------------------------------------------ the parts of a chunk
class _Part:
    """One thing inside a chunk: ``capture`` says what of it is in the chunk now (None: nothing), ``take``
    and ``give`` take that away from the chunk and put it back (the files are moved by the removal)."""

    def capture(self, chunk) -> dict | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def take(self, chunk, state: dict) -> None:
        for path in state.get("meta", {}):
            _pop(chunk.meta, path)

    def give(self, chunk, state: dict) -> None:
        for path, value in state.get("meta", {}).items():
            _put(chunk.meta, path, value)

    def check(self, chunk, state: dict) -> None:
        """Raise :class:`RemovalConflict` when :meth:`give` could not put ``state`` back."""


class _Product(_Part):
    """A product of ``chunk.products`` - the model with its texture, the tie points with the alignment
    model - with the name of its active version and, for a relief raster, its parameters. Removing the
    tie points makes the photos not aligned (their poses and the positions of the markers measured in
    them go with them); the georeference of the chunk and the other products stay."""

    def __init__(self, key: str):
        self.key = key

    def capture(self, chunk) -> dict | None:
        from . import versions

        keys = FAMILY.get(self.key, (self.key,))
        products = {k: chunk.products[k] for k in keys if k in chunk.products}
        if not products:
            return None
        files: list[Path] = []
        for k, rel in products.items():
            files += _with_sidecars(chunk.data_dir / rel)
        if "model_textured" in products:
            files += self._texture_files(chunk)
        if self.key == "model":
            files += _with_sidecars(chunk.data_dir / "model_preview.ply")
        meta = {}
        for k in keys:
            if k in versions.PRODUCTS and _get(chunk.meta, ("active_versions", k)) is not _MISSING:
                meta[("active_versions", k)] = _get(chunk.meta, ("active_versions", k))
        if self.key in RELIEF and _get(chunk.meta, ("relief", self.key)) is not _MISSING:
            meta[("relief", self.key)] = _get(chunk.meta, ("relief", self.key))
        state = {"products": products, "meta": meta, "files": files}
        if self.key == "tie_points":
            state["poses"] = {c.key: c.transform for c in chunk.cameras if c.transform is not None}
            state["markers"] = {m.key: m.position for m in chunk.markers if m.projections and m.position is not None}
            if _get(chunk.meta, ("precision",)) is not _MISSING:
                meta[("precision",)] = chunk.meta["precision"]
        return state

    @staticmethod
    def _texture_files(chunk) -> list[Path]:
        textured = chunk.product_path("model_textured")
        if textured is None:
            return []
        if textured.suffix.lower() == ".obj":  # a texture of version 0.5 and earlier: its folder
            return [textured.parent] if textured.parent != chunk.data_dir else _with_sidecars(textured)
        from ..engine.texturing import texture_pages

        pages = []
        try:
            pages = texture_pages(chunk)
        except Exception:  # noqa: BLE001 - a model that cannot be read: the first page only
            pages = [textured]
        out = [q for p in pages for q in _with_sidecars(p)]
        return out + _with_sidecars(chunk.data_dir / "model_texture_display.jpg")

    def take(self, chunk, state: dict) -> None:
        super().take(chunk, state)
        for k in state["products"]:
            chunk.products.pop(k, None)
        if self.key == "tie_points":
            poses, markers = state.get("poses", {}), state.get("markers", {})
            for c in chunk.cameras:
                if c.key in poses:
                    c.transform = None
            for m in chunk.markers:
                if m.key in markers:
                    m.position = None
            chunk._tie_points = None
            chunk._surface_cache.clear()

    def give(self, chunk, state: dict) -> None:
        super().give(chunk, state)
        chunk.products.update(state["products"])
        if self.key == "tie_points":
            poses, markers = state.get("poses", {}), state.get("markers", {})
            for c in chunk.cameras:
                if c.key in poses:
                    c.transform = poses[c.key]
            for m in chunk.markers:
                if m.key in markers:
                    m.position = markers[m.key]
            chunk._tie_points = None
            chunk._surface_cache.clear()


class _DepthMaps(_Part):
    """The depth maps (``<data>/dense``: the undistorted photos, the maps, the normals) and their settings."""

    def capture(self, chunk) -> dict | None:
        folder = chunk.data_dir / "dense"
        settings = _get(chunk.meta, ("dense",))
        if not folder.exists() and settings is _MISSING:
            return None
        return {"meta": {} if settings is _MISSING else {("dense",): settings},
                "files": [folder] if folder.exists() else []}


class _Meta(_Part):
    """A product listed in ``chunk.meta`` (those of the multispectral and thermal captures): the entries
    at ``paths``; ``files`` names, for each entry, the keys of its files (``None``: the entry is the
    name of a file itself; ``()``: no file)."""

    def __init__(self, paths: dict[tuple, tuple | None]):
        self.paths = paths

    def capture(self, chunk) -> dict | None:
        values = {p: _get(chunk.meta, p) for p in self.paths}
        values = {p: v for p, v in values.items() if v is not _MISSING}
        if not values:
            return None
        names = []
        for p, v in values.items():
            keys = self.paths[p]
            if keys is None and isinstance(v, str) and v:
                names.append(v)
            elif keys and isinstance(v, dict):
                names += [str(v[k]) for k in keys if v.get(k)]
        # a name may be absolute (a GeoPackage of zones saved beside the project): it stays where it is
        return {"meta": values, "files": [q for n in names for q in _with_sidecars(chunk.data_dir / n)]}


def _meta_part(key: str) -> _Part:
    """The part of a product of the multispectral or thermal captures (``ms:reflectance``,
    ``ms:index:<name>``, ``ms:zones:<name>``, ``ms:thermal``, ``ms:thermal_anomaly``)."""
    kind, _, name = key[3:].partition(":")
    if kind == "reflectance":
        return _Meta({("multispectral", "reflectance"): None, ("multispectral", "reflectance_bands"): ()})
    if kind == "index" and name:
        return _Meta({("multispectral", "indices", name): ("file",)})
    if kind == "zones" and name:
        return _Meta({("multispectral", "zoning", name): ("raster", "file")})
    if kind == "thermal":
        return _Meta({("thermal", "file"): None, ("thermal", "resolution"): (), ("thermal", "stats"): ()})
    if kind == "thermal_anomaly":
        return _Meta({("thermal", "anomaly"): ("file",)})
    raise ValueError(f"not a product of the captures: {key!r}")


class _ListEntry(_Part):
    """An entry with an ``id`` in a list of ``chunk.meta`` (a kept version, an elevation) and its folder."""

    def __init__(self, path: tuple, eid: str, folder: str):
        self.path, self.eid, self.folder = path, eid, folder

    def capture(self, chunk) -> dict | None:
        entries = _get(chunk.meta, self.path)
        if not isinstance(entries, list):
            return None
        for i, e in enumerate(entries):
            if isinstance(e, dict) and e.get("id") == self.eid:
                return {"index": i, "entry": e, "files": [chunk.data_dir / self.folder]}
        return None

    def take(self, chunk, state: dict) -> None:
        entries = _get(chunk.meta, self.path)
        if isinstance(entries, list):
            entries[:] = [e for e in entries if not (isinstance(e, dict) and e.get("id") == self.eid)]

    def give(self, chunk, state: dict) -> None:
        entries = _get(chunk.meta, self.path)
        if not isinstance(entries, list):
            entries = []
            _put(chunk.meta, self.path, entries)
        if not any(isinstance(e, dict) and e.get("id") == self.eid for e in entries):
            entries.insert(min(state["index"], len(entries)), state["entry"])


class _Objects(_Part):
    """Objects of a list of the chunk (photos, markers, scale bars), by identity: they go back where
    they were, unless an object with the same key took their place meanwhile."""

    attribute = ""
    noun = ""

    def __init__(self, objects):
        self.objects = list(objects)

    def _present(self, chunk) -> list:
        items = getattr(chunk, self.attribute)
        return [(i, o) for i, o in enumerate(items) if any(o is x for x in self.objects)]

    def capture(self, chunk) -> dict | None:
        present = self._present(chunk)
        return {"objects": present, "files": []} if present else None

    def take(self, chunk, state: dict) -> None:
        gone = [o for _, o in state["objects"]]
        setattr(chunk, self.attribute, [o for o in getattr(chunk, self.attribute) if not any(o is g for g in gone)])

    def check(self, chunk, state: dict) -> None:
        items = getattr(chunk, self.attribute)
        keys = {o.key for o in items}
        clash = [o.label for _, o in state["objects"] if o.key in keys and not any(o is x for x in items)]
        if clash:
            raise RemovalConflict(f"{self.noun} added since took the place of {', '.join(map(str, clash))}")

    @staticmethod
    def _insert(items: list, entries) -> None:
        for i, o in sorted(entries, key=lambda t: t[0]):
            if not any(o is x for x in items):
                items.insert(min(i, len(items)), o)

    def give(self, chunk, state: dict) -> None:
        self._insert(getattr(chunk, self.attribute), state["objects"])


class _Cameras(_Objects):
    """Photos: with them their measurements of the markers and their names in the alignment model (the
    model keeps their images, which nothing maps to a photo of the chunk any more: a new alignment
    leaves them out). The photo files are never touched."""

    attribute, noun = "cameras", "photos"

    def capture(self, chunk) -> dict | None:
        state = super().capture(chunk)
        if state is None:
            return None
        keys = {o.key for _, o in state["objects"]}
        state["projections"] = {m.key: {k: p for k, p in m.projections.items() if k in keys} for m in chunk.markers}
        state["projections"] = {k: v for k, v in state["projections"].items() if v}
        names = _get(chunk.meta, ("sfm", "names"))
        state["names"] = {str(k): names[str(k)] for k in keys if isinstance(names, dict) and str(k) in names}
        return state

    def take(self, chunk, state: dict) -> None:
        super().take(chunk, state)
        keys = {o.key for _, o in state["objects"]}
        for m in chunk.markers:
            for k in keys:
                m.projections.pop(k, None)
        names = _get(chunk.meta, ("sfm", "names"))
        if isinstance(names, dict):
            for k in state["names"]:
                names.pop(k, None)
        chunk._surface_cache.clear()

    def give(self, chunk, state: dict) -> None:
        super().give(chunk, state)
        markers = {m.key: m for m in chunk.markers}
        for mk, projections in state["projections"].items():
            if mk in markers:
                for k, p in projections.items():
                    markers[mk].projections.setdefault(k, p)
        if state["names"]:
            names = _get(chunk.meta, ("sfm", "names"))
            if isinstance(names, dict):
                for k, v in state["names"].items():
                    names.setdefault(k, v)
        chunk._surface_cache.clear()


class _Markers(_Objects):
    """Markers, with the scale bars that end at them and the photos where they were not to be estimated."""

    attribute, noun = "markers", "markers"

    def capture(self, chunk) -> dict | None:
        state = super().capture(chunk)
        if state is None:
            return None
        keys = {o.key for _, o in state["objects"]}
        state["bars"] = [(i, sb) for i, sb in enumerate(chunk.scalebars) if sb.marker_a in keys or sb.marker_b in keys]
        table = chunk.meta.get("marker_excluded") or {}
        state["excluded"] = {k: v for k, v in table.items() if str(k).lstrip("-").isdigit() and int(k) in keys}
        return state

    def take(self, chunk, state: dict) -> None:
        super().take(chunk, state)
        gone = [sb for _, sb in state["bars"]]
        chunk.scalebars = [sb for sb in chunk.scalebars if not any(sb is g for g in gone)]
        table = chunk.meta.get("marker_excluded")
        if isinstance(table, dict):
            for k in state["excluded"]:
                table.pop(k, None)
            if not table:
                chunk.meta.pop("marker_excluded", None)
        chunk._surface_cache.clear()

    def check(self, chunk, state: dict) -> None:
        super().check(chunk, state)
        keys = {sb.key for sb in chunk.scalebars}
        clash = [sb.label for _, sb in state["bars"] if sb.key in keys and not any(sb is x for x in chunk.scalebars)]
        if clash:
            raise RemovalConflict(f"scale bars added since took the place of {', '.join(map(str, clash))}")

    def give(self, chunk, state: dict) -> None:
        super().give(chunk, state)
        self._insert(chunk.scalebars, state["bars"])
        if state["excluded"]:
            table = chunk.meta.setdefault("marker_excluded", {})
            for k, v in state["excluded"].items():
                table.setdefault(k, v)


class _ScaleBars(_Objects):
    attribute, noun = "scalebars", "scale bars"


def make_part(chunk, kind: str, payload) -> _Part:
    """The part for an item of the workspace: ``("product", key)`` (a key of ``chunk.products``,
    ``depth_maps`` or ``ms:...``), ``("version", (product, id))``, ``("elevation", id)``,
    ``("cameras", [Camera])``, ``("markers", [Marker])``, ``("scalebars", [ScaleBar])``."""
    if kind == "product":
        if payload == "depth_maps":
            return _DepthMaps()
        if isinstance(payload, str) and payload.startswith("ms:"):
            return _meta_part(payload)
        return _Product(str(payload))
    if kind == "version":
        product, vid = payload
        return _ListEntry(("versions", product), vid, f"versions/{product}/{vid}")
    if kind == "elevation":
        eid = payload.get("id") if isinstance(payload, dict) else payload
        return _ListEntry(("elevations",), eid, f"elevations/{eid}")
    if kind == "cameras":
        return _Cameras(payload)
    if kind == "markers":
        return _Markers(payload)
    if kind == "scalebars":
        return _ScaleBars(payload)
    raise ValueError(f"cannot remove {kind!r}")


# ------------------------------------------------------------------------------ removals
class Removal:
    """Files of the project moved to ``<files>/.removed/<id>/<side>/`` (each at its path inside the files
    folder) and what of the project went with them. ``state``: ``removed`` or ``restored`` (undone);
    ``saved``: the state the last save of the project wrote (None: not saved since the removal)."""

    kind = ""

    def __init__(self, document):
        self.document = document
        self.id = uuid.uuid4().hex[:12]
        self.state: str | None = None
        self.saved: str | None = None
        self.files: dict[str, list[str]] = {PRESENT: [], ABSENT: []}  # paths inside the files folder
        document.removals.append(self)

    @property
    def folder(self) -> Path:
        return self.document.files_dir / TRASH / self.id

    def _manifest(self) -> dict:
        return {"version": 1, "kind": self.kind, "state": self.state, "files": self.files}

    def _write_manifest(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / MANIFEST).write_text(json.dumps(self._manifest(), indent=1), encoding="utf-8")

    def _stash_files(self, side: str, rels: list[str]) -> None:
        """The files at ``rels`` (inside the files folder) moved to the trash, side ``side``; when one
        cannot be moved, those moved go back and the error goes on."""
        root = self.document.files_dir
        self.files[side] = list(rels)
        self._write_manifest()  # first: a crash while moving leaves the list behind
        moved = []
        try:
            for rel in rels:
                src = root / rel
                if src.exists() or src.is_symlink():
                    dst = self.folder / side / rel
                    if dst.exists() or dst.is_symlink():
                        _delete(dst)
                    _move(src, dst)
                    moved.append((src, dst))
        except Exception:
            for src, dst in reversed(moved):
                try:
                    _move(dst, src)
                except OSError as exc:  # pragma: no cover - the disk refused twice
                    log.error("%s left in %s (%s)", src, dst, exc)
            raise

    def _unstash_files(self, side: str, occupant_side: str) -> None:
        """The files of ``side`` back in their place; whatever is there (nothing, as a rule) goes to the
        trash with ``occupant_side``. When one cannot be moved, those moved go back and the error goes on."""
        root = self.document.files_dir
        moved = []
        try:
            for rel in self.files[side]:
                src = self.folder / side / rel
                if not (src.exists() or src.is_symlink()):
                    continue
                place = root / rel
                if not _free(place):
                    _move(place, self.folder / occupant_side / rel)
                    if rel not in self.files[occupant_side]:
                        self.files[occupant_side].append(rel)
                _move(src, place)
                moved.append((src, place))
        except Exception:
            for src, place in reversed(moved):
                try:
                    _move(place, src)
                except OSError as exc:  # pragma: no cover
                    log.error("%s left in %s (%s)", src, place, exc)
            raise

    def mark_saved(self) -> None:
        self.saved = self.state

    def settle(self) -> None:
        """End of the session: the files as the project last saved wants them (the item back in place
        when that project still has it, else deleted) and the trash of this removal emptied."""
        try:
            if self.document.path is not None and self.state is not None:
                want = self.saved or RESTORED  # never saved since: the project on disk is from before
                if want != self.state:
                    self._swap_on_disk(want)
        except Exception as exc:  # noqa: BLE001 - the session ends anyway: the trash is left as it is
            log.warning("removed files not settled (%s): they stay in %s", exc, self.folder)
            return
        finally:
            self.document.removals[:] = [r for r in self.document.removals if r is not self]
        _delete(self.folder)
        trash = self.document.files_dir / TRASH
        if trash.is_dir() and not any(trash.iterdir()):
            trash.rmdir()

    def _swap_on_disk(self, want: str) -> None:
        if want == RESTORED:  # the absent side in place goes, the present side comes back
            self._stash_files(ABSENT, [r for r in self.files[ABSENT]
                                       if (self.document.files_dir / r).exists()])
            self._unstash_files(PRESENT, ABSENT)
        else:
            self._stash_files(PRESENT, [r for r in self.files[PRESENT] if (self.document.files_dir / r).exists()])
            self._unstash_files(ABSENT, PRESENT)
        self.state = want


class ChunkRemoval(Removal):
    """Chunks taken out of the project, each with its folder; ``was_active`` is the one of them that was
    the active chunk (None when it was another): the window decides which chunk is active after."""

    kind = "chunks"

    def __init__(self, document, chunks):
        super().__init__(document)
        self.chunks = [c for c in document.chunks if any(c is x for x in chunks)]  # in the order of the project
        if len(self.chunks) != len(list(chunks)):
            raise ValueError("a chunk to remove is not in the project")
        self.places: dict[int, int] = {}
        self.was_active = None

    def _manifest(self) -> dict:
        return {**super()._manifest(), "chunks": [c.key for c in self.chunks]}

    def apply(self) -> None:
        self.redo()

    def redo(self) -> None:
        doc = self.document
        self.places = {c.key: doc.chunks.index(c) for c in self.chunks}
        active = doc.chunk
        self.was_active = next((c for c in self.chunks if c is active), None)
        root = doc.files_dir
        rels = [f"chunk_{c.key}" for c in self.chunks if (root / f"chunk_{c.key}").exists()]
        before, self.state = self.state, REMOVED
        try:
            self._stash_files(PRESENT, rels)
        except Exception:
            self.state = before
            raise
        doc.chunks[:] = [c for c in doc.chunks if not any(c is x for x in self.chunks)]
        if doc.meta.get("active_chunk") in {c.key for c in self.chunks}:
            doc.meta.pop("active_chunk", None)

    def undo(self) -> None:
        doc = self.document
        root = doc.files_dir
        for rel in self.files[PRESENT]:
            place = root / rel
            if not _free(place) and (self.folder / PRESENT / rel).exists():
                raise RemovalConflict(f"{place} is in the way of the chunk that comes back")
        self._unstash_files(PRESENT, ABSENT)
        self.state = RESTORED
        self._write_manifest()
        for c in sorted(self.chunks, key=lambda c: self.places.get(c.key, len(doc.chunks))):
            if not any(c is x for x in doc.chunks):
                doc.chunks.insert(min(self.places.get(c.key, len(doc.chunks)), len(doc.chunks)), c)


class ItemsRemoval(Removal):
    """Things inside one chunk (see :func:`make_part`), taken away and put back together."""

    kind = "items"

    def __init__(self, chunk, items):
        super().__init__(chunk.document)
        self.chunk = chunk
        self.items = list(items)  # (kind, payload)
        self.parts = [make_part(chunk, kind, payload) for kind, payload in self.items]
        self.states: dict[str, list] = {PRESENT: [], ABSENT: []}

    def _manifest(self) -> dict:
        return {**super()._manifest(), "chunk": self.chunk.key}

    def _stash(self, side: str) -> None:
        """What the parts are in the chunk now goes away (files to the trash, side ``side``)."""
        ch = self.chunk
        states = [p.capture(ch) for p in self.parts]
        rels = []
        for st in states:
            for path in (st or {}).get("files", []):
                rel = _rel(path, ch.data_dir)
                if rel is not None and rel != ".":  # only the chunk's own files are ever moved
                    rels.append(_rel(path, self.document.files_dir))
        self.states[side] = states
        self._stash_files(side, _outermost([r for r in rels if r]))
        for p, st in zip(self.parts, states):
            if st is not None:
                p.take(ch, st)

    def _unstash(self, side: str, occupant_side: str) -> None:
        ch = self.chunk
        for p, st in zip(self.parts, self.states[side]):
            if st is not None:
                p.check(ch, st)
        self._unstash_files(side, occupant_side)
        for p, st in reversed(list(zip(self.parts, self.states[side]))):  # in the reverse order of take:
            if st is not None:  # a marker back before the photos give it their measurements again
                p.give(ch, st)

    def apply(self) -> None:
        if not any(p.capture(self.chunk) is not None for p in self.parts):
            raise ValueError("nothing to remove")
        self.state = REMOVED
        try:
            self._stash(PRESENT)
        except Exception:
            self.state = None
            raise

    def _swap(self, out: str, back: str, state: str) -> None:
        """What is in place now goes to the trash with ``out``, the side ``back`` comes back; when it
        cannot, what went out comes back and the error goes on."""
        for p, st in zip(self.parts, self.states[back]):  # nothing moves when something cannot come back
            if st is not None:
                p.check(self.chunk, st)
        self._stash(out)
        try:
            self._unstash(back, out)
        except Exception:
            self._unstash(out, back)
            raise
        self.state = state
        self._write_manifest()

    def undo(self) -> None:
        self._swap(ABSENT, PRESENT, RESTORED)

    def redo(self) -> None:
        self._swap(PRESENT, ABSENT, REMOVED)


# ------------------------------------------------------------------------------ after a crash
_CHUNK_DIR = re.compile(r"^chunk_(-?\d+)$")


def recover(document) -> dict:
    """The removals a session left in ``.removed`` without settling them (the program crashed or was
    killed): what the project, as saved, still has goes back in place - a chunk it lists, the files of a
    chunk it lists where nothing took their place - and the rest is deleted. The removals of this
    session (``document.removals``) are left alone. Returns ``{"restored": n, "deleted": n}``."""
    out = {"restored": 0, "deleted": 0}
    if document.path is None:
        return out
    root = document.files_dir
    trash = root / TRASH
    if not trash.is_dir():
        return out
    live = {r.id for r in document.removals}
    keys = {c.key for c in document.chunks}
    entries = []
    for entry in sorted(trash.iterdir()):
        if entry.name in live or not entry.is_dir():
            continue
        try:
            manifest = json.loads((entry / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("%s: no list of what it holds, left as it is", entry)
            continue
        entries.append((0 if manifest.get("kind") == "chunks" else 1, entry, manifest))
    for _order, entry, manifest in sorted(entries, key=lambda t: t[0]):  # the chunks first, then what goes in them
        for rel in manifest.get("files", {}).get(PRESENT, []):
            src = entry / PRESENT / rel
            if not (src.exists() or src.is_symlink()):
                continue
            top = rel.split("/")[0]
            m = _CHUNK_DIR.match(top)
            if manifest.get("kind") == "chunks":
                wanted = m is not None and int(m.group(1)) in keys and rel == top
            else:  # inside a chunk the project still has, where nothing took its place
                wanted = m is not None and int(m.group(1)) in keys and (root / top).is_dir()
            if wanted and _free(root / rel):
                _move(src, root / rel)
                out["restored"] += 1
            else:
                out["deleted"] += 1
        _delete(entry)
    if trash.is_dir() and not any(trash.iterdir()):
        trash.rmdir()
    return out


__all__ = ["TRASH", "RemovalConflict", "Removal", "ChunkRemoval", "ItemsRemoval", "make_part", "recover"]
