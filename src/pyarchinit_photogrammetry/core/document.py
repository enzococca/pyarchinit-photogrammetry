"""Document: the project file.

A project is ``name.ppg`` (JSON) plus the folder ``name.ppg.files/`` with the
heavy data of each chunk (feature database, depth maps, clouds, meshes, rasters).
What the workspace removes in a session waits in ``name.ppg.files/.removed/`` until
the session ends (see :mod:`.removal`).
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import weakref
from pathlib import Path

from .chunk import Chunk

log = logging.getLogger("pyarchinit_photogrammetry")

FORMAT_VERSION = 1
# the temporary folders of the documents never saved that are still alive: each is removed with its document or when
# the interpreter ends, but not when the process leaves through os._exit (remove_temporary_folders is for that)
_TEMPORARY: weakref.WeakSet = weakref.WeakSet()


def remove_temporary_folders() -> None:
    """Remove the temporary folders of the documents never saved (``ppg_*`` in the temporary folder of the system).
    For a process about to leave without the finalisation of the interpreter (``os._exit``), which would leave them
    behind; the documents are not to be used after it."""
    for folder in list(_TEMPORARY):
        try:
            folder.cleanup()
        except OSError:  # a file still open on Windows: what can be removed is
            shutil.rmtree(folder.name, ignore_errors=True)
    _TEMPORARY.clear()


_CHUNK_LOCK = threading.Lock()  # a key and a place in the list for one new chunk at a time (module-level: a Document
#                                stays copyable with copy.deepcopy, which cannot copy a lock)


class Document:
    def __init__(self, path: str | Path | None = None):
        self.chunks: list[Chunk] = []
        self.meta: dict = {}
        self.removals: list = []  # removals of this session that can still be undone (core.removal)
        self.saves = 0  # how many times it was saved in this session
        self.script_removals: list | None = None  # while a script runs: its chunk removals, to be undone
        self._path: Path | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None
        if path is not None:
            self._path = Path(path).resolve()
            if self._path.exists():
                self._read()

    # -------------------------------------------------------------- paths
    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def files_dir(self) -> Path:
        if self._path is not None:
            d = self._path.with_name(self._path.name + ".files")
        else:
            if self._tmp is None:
                self._tmp = tempfile.TemporaryDirectory(prefix="ppg_")
                _TEMPORARY.add(self._tmp)
            d = Path(self._tmp.name)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------- chunks
    @property
    def chunk(self) -> Chunk | None:
        """The active chunk (set by assignment, as in the GUI; default: the first enabled one)."""
        key = self.meta.get("active_chunk")
        for c in self.chunks:
            if c.key == key:
                return c
        return next((c for c in self.chunks if c.enabled), None)

    @chunk.setter
    def chunk(self, chunk: Chunk | None) -> None:
        if chunk is None:
            self.meta.pop("active_chunk", None)
        elif chunk not in self.chunks:
            raise ValueError("the chunk does not belong to this document")
        else:
            self.meta["active_chunk"] = chunk.key

    def chunk_by_label(self, label: str) -> Chunk:
        """The chunk named ``label`` (the first one when two share it); a KeyError naming the labels when none is."""
        for c in self.chunks:
            if c.label == label:
                return c
        labels = ", ".join(repr(c.label) for c in self.chunks) or "none"
        raise KeyError(f"no chunk {label!r}: the chunks of the project are {labels}")

    def add_chunk(self, label: str | None = None) -> Chunk:
        """A new chunk at the end. Its key is never that of a chunk removed in this session (it can
        come back with Modifica > Annulla) nor that of a folder already full in the files folder."""
        with _CHUNK_LOCK:  # the program and a task in the background (an append) may add one at once
            removed = [c.key for r in self.removals if getattr(r, "kind", "") == "chunks" and r.state == "removed"
                       for c in r.chunks]
            key = max([c.key for c in self.chunks] + removed, default=-1) + 1
            if self._path is not None or self._tmp is not None:  # the files folder exists already
                while (folder := self.files_dir / f"chunk_{key}").is_dir() and any(folder.iterdir()):
                    key += 1
            chunk = Chunk(key=key, label=label or f"Chunk {key + 1}", document=self)
            self.chunks.append(chunk)
        return chunk

    def remove_chunk(self, chunk: Chunk) -> None:
        """The chunk out of the project and its folder deleted at once (a chunk just made by a step that
        failed). The workspace removes chunks with :class:`removal.ChunkRemoval`, which can be undone."""
        if self.script_removals is not None:  # a script runs: its folder set aside, Annulla brings it back
            from .removal import ChunkRemoval

            removal = ChunkRemoval(self, [chunk])
            removal.apply()
            self.script_removals.append(removal)
            return
        self.chunks.remove(chunk)
        shutil.rmtree(self.files_dir / f"chunk_{chunk.key}", ignore_errors=True)

    def settle_removals(self) -> None:
        """End of the session: the files of what was removed in it as the project last saved wants them
        (back in place when it still lists them, else deleted) - the chunks first, then what goes inside
        them - and the folder ``.removed`` emptied (see :mod:`.removal`)."""
        for r in sorted(self.removals, key=lambda r: 0 if getattr(r, "kind", "") == "chunks" else 1):
            r.settle()
        self.removals = []

    def mend_swaps(self) -> int:
        """The folders of elevations a crash left halfway through their swap (``<id>.old`` or ``<id>.old<n>`` beside them, see
        :func:`..io.files.swap_folder`) mended: the old one back when the new one never came, the backup deleted
        when it did. Returns how many."""
        import re

        from ..io.files import BACKUPS, mend_swap

        n = 0
        for c in self.chunks:
            folder = self.files_dir / f"chunk_{c.key}" / "elevations"
            if not folder.is_dir():
                continue
            places = set()
            for old in folder.glob("*.old*"):
                m = re.fullmatch(r"(.+)\.old(\d?)", old.name)
                if old.is_dir() and m and (not m.group(2) or 0 < int(m.group(2)) < BACKUPS):
                    places.add(old.with_name(m.group(1)))
            for place in sorted(places):
                mend_swap(place)
                n += 1
        return n

    def recover_removed(self) -> dict:
        """What a session that crashed left in ``.removed``, settled now (see :func:`removal.recover`)."""
        from .removal import recover

        return recover(self)

    def align_chunks(self, reference, chunks=None, method: str = "markers", fix_scale: bool = False,
                     initial: str = "auto", max_points: int = 200_000, progress=None, check: bool = False) -> dict:
        """Bring ``chunks`` (Chunk objects, keys or labels; default: the other enabled ones) into the
        world frame of ``reference``: their ``transform`` and CRS change, their products stay.
        ``method``: ``"markers"`` (same labels), ``"points"`` (dense clouds or tie points:
        initial position from markers, georeference or common photos, else FPFH + RANSAC;
        ``initial`` forces one of ``"markers"``, ``"reference"``, ``"cameras"``, ``"global"``),
        ``"cameras"`` (photos in both chunks). ``fix_scale``: rigid, without scale.
        Returns a report with residuals, correspondences and transforms (see
        :func:`engine.chunks.align_chunks`); ``check``: each chunk measured against the reference once aligned
        (overlap and distances, :func:`engine.chunks.cloud_distances`)."""
        from ..engine.chunks import align_chunks

        return align_chunks(self, reference, chunks, method=method, fix_scale=fix_scale, initial=initial,
                            max_points=max_points, progress=progress, check=check)

    def merge_models(self, chunks, label: str | None = None, **options):
        """The models of ``chunks`` as one textured model of a new chunk (colours matched, overlaps removed, pages
        kept or repacked): see :func:`engine.model_merge.merge_models`. Returns ``(chunk, report)``."""
        from ..engine.model_merge import merge_models

        return merge_models(self, chunks, label=label, **options)

    def merge_chunks(self, chunks, label: str | None = None, merge_markers: bool = True,
                     merge_tie_points: bool = True, merge_point_clouds: bool = True, merge_models: bool = False,
                     progress=None) -> Chunk:
        """A new chunk with the photos, markers, tie points, dense clouds (and models) of
        ``chunks``, in the frame and CRS of the first one; the inputs are kept. A COLMAP model
        is written so that dense cloud, mesh, DEM and orthomosaic can be built again (see
        :func:`engine.chunks.merge_chunks`)."""
        from ..engine.chunks import merge_chunks

        return merge_chunks(self, chunks, label=label, merge_markers=merge_markers, merge_tie_points=merge_tie_points,
                            merge_point_clouds=merge_point_clouds, merge_models=merge_models, progress=progress)

    def duplicate_chunk(self, chunk: Chunk, cameras=None, label: str | None = None,
                        keep_products: bool = False) -> Chunk:
        """A new chunk (at the end) with the photos ``cameras`` of ``chunk`` (all when None): their
        sensors, poses and enabled flags, the markers with the projections on those photos only, the
        scale bars, transform, CRS and the settings of the chunk (its ``meta`` without what belongs to
        products, versions, drawings and proxies). A COLMAP model is written from the poses, so depth
        maps, dense cloud and the rest can be built again without aligning; the tie points are copied
        when every photo is kept. Products are never copied (``keep_products`` is reserved and must be
        False). The masks of the photos kept are copied, and so are the photos that live in the folder of
        ``chunk`` (the reference copies of multispectral and thermal captures), the cameras pointed at the copies;
        the settings of the captures stay, their products (reflectance, indices, zones, thermal orthomosaic,
        anomalies) do not. Some of the photos need the tie points of ``chunk`` (ValueError without them).
        ``label``: default ``"<label> (copia)"``, numbered (``"(copia 2)"``...) when taken
        (:func:`engine.chunks.duplicate_label`). ``chunk`` is not changed."""
        from ..engine.chunks import duplicate_chunk

        return duplicate_chunk(self, chunk, cameras, label=label, keep_products=keep_products)

    # ---------------------------------------------------------------- I/O
    def export_site(self, folder, chunks=None, products=None, frame: str = "local", title: str | None = None,
                    description: str | None = None, language=("it", "en"), quality: int = 85,
                    model_texture: int | None = 4096, model_faces: int = 150_000, progress=None) -> Path:
        """The survey as a web site in ``folder``: the orthophoto, the DEM with its shaded relief, the stratigraphic
        units, the drawings and the 3D model of ``chunks`` (objects, keys or labels; default: every chunk that has
        some), with a page that shows them on a plan and in 3D - a folder any static server can publish (``ppg web
        serve`` to look at it). ``frame``: ``"local"`` (default) shifts the coordinates to the centre of the survey
        and writes no reference system: the place of the site is not in the folder; ``"crs"`` keeps the real
        coordinates. ``model_texture``: the largest side of a texture page of the model (None: vertex colours).
        Returns the path of ``site.json`` (see :func:`io.site.export_site`)."""
        from ..io.site import export_site

        return export_site(self, folder, chunks=chunks, products=products, frame=frame, title=title,
                           description=description, language=language, quality=quality,
                           model_texture=model_texture, model_faces=model_faces, progress=progress)

    def save(self, path: str | Path | None = None) -> Path:
        if path is not None:
            new = Path(path).resolve()
            if new.suffix != ".ppg":
                new = new.with_suffix(".ppg")
            if new != self._path:
                old_files = self.files_dir
                self._path = new
                new_files = self.files_dir
                if old_files.exists() and old_files.resolve() != new_files.resolve():
                    shutil.copytree(old_files, new_files, dirs_exist_ok=True)
                    self._repoint(old_files.resolve(), new_files)
        if self._path is None:
            raise ValueError("save() needs a path for a new document")
        self._settle_ties()
        payload = {"format": "pyarchinit-photogrammetry", "version": FORMAT_VERSION, "meta": self.meta,
                   "chunks": [c.to_dict() for c in self.chunks]}
        tmp = self._path.with_suffix(".ppg.tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        tmp.replace(self._path)
        for r in self.removals:  # what the file now says: the end of the session settles the files on it
            r.mark_saved()
        self.saves += 1
        return self._path

    def _repoint(self, old: Path, new: Path) -> None:
        """After *save as*: the photos kept inside the project folder (developed raw files, reference bands) are
        read from the new folder, not from the one the project had before."""
        def moved(value):
            try:
                return str(new / Path(value).resolve().relative_to(old))
            except ValueError:
                return value

        for chunk in self.chunks:
            for cam in chunk.cameras:
                cam.path = moved(cam.path)
            root = (chunk.meta.get("sfm") or {}).get("image_root")
            if root:
                chunk.meta["sfm"]["image_root"] = moved(root)

    def _read(self) -> None:
        data = json.loads(self._path.read_text())
        if data.get("format") != "pyarchinit-photogrammetry":
            raise ValueError(f"{self._path} is not a pyarchinit-photogrammetry project")
        self.meta = data.get("meta", {})
        self.chunks = [Chunk.from_dict(c, document=self) for c in data.get("chunks", [])]
        self._drop_missing_versions()
        self._settle_ties()

    def _settle_ties(self) -> None:
        """The ties between chunks as they are read now (labels of their markers, alignments of their ends), written
        back in ``meta`` (:func:`..engine.ties.settle_stored`): on open and on every save."""
        if self.meta.get("ties"):
            from ..engine.ties import settle_stored

            settle_stored(self)

    def _drop_missing_versions(self) -> None:
        """A kept version listed without its main file (an interrupted session) is not listed any more."""
        files = self._path.with_name(self._path.name + ".files")
        for c in self.chunks:
            listed = (c.meta.get("versions") or {}) if isinstance(c.meta.get("versions"), dict) else {}
            for product, entries in listed.items():
                kept = []
                for e in entries or []:
                    main = (e.get("files") or {}).get(product)
                    if main and not (files / f"chunk_{c.key}" / main).exists():
                        log.warning("Versione %s di %s (%s) senza file: tolta dall'elenco", e.get("id"), product,
                                    c.label)
                        continue
                    kept.append(e)
                listed[product] = kept

    @classmethod
    def open(cls, path: str | Path) -> "Document":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(p)
        return cls(p)

    def __repr__(self) -> str:
        return f"<Document {self._path or '(unsaved)'} chunks={len(self.chunks)}>"
