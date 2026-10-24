"""Document: the project file.

A project is ``name.ppg`` (JSON) plus the folder ``name.ppg.files/`` with the
heavy data of each chunk (feature database, depth maps, clouds, meshes, rasters).
What the workspace removes in a session waits in ``name.ppg.files/.removed/`` until
the session ends (see :mod:`.removal`).
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from .chunk import Chunk

FORMAT_VERSION = 1


class Document:
    def __init__(self, path: str | Path | None = None):
        self.chunks: list[Chunk] = []
        self.meta: dict = {}
        self.removals: list = []  # removals of this session that can still be undone (core.removal)
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

    def add_chunk(self, label: str | None = None) -> Chunk:
        """A new chunk at the end. Its key is never that of a chunk removed in this session (it can
        come back with Modifica > Annulla) nor that of a folder already full in the files folder."""
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
        self.chunks.remove(chunk)
        shutil.rmtree(self.files_dir / f"chunk_{chunk.key}", ignore_errors=True)

    def settle_removals(self) -> None:
        """End of the session: the files of what was removed in it as the project last saved wants them
        (back in place when it still lists them, else deleted) - the chunks first, then what goes inside
        them - and the folder ``.removed`` emptied (see :mod:`.removal`)."""
        for r in sorted(self.removals, key=lambda r: 0 if getattr(r, "kind", "") == "chunks" else 1):
            r.settle()
        self.removals = []

    def recover_removed(self) -> dict:
        """What a session that crashed left in ``.removed``, settled now (see :func:`removal.recover`)."""
        from .removal import recover

        return recover(self)

    def align_chunks(self, reference, chunks=None, method: str = "markers", fix_scale: bool = False,
                     initial: str = "auto", max_points: int = 200_000, progress=None) -> dict:
        """Bring ``chunks`` (Chunk objects, keys or labels; default: the other enabled ones) into the
        world frame of ``reference``: their ``transform`` and CRS change, their products stay.
        ``method``: ``"markers"`` (same labels), ``"points"`` (dense clouds or tie points:
        initial position from markers, georeference or common photos, else FPFH + RANSAC;
        ``initial`` forces one of ``"markers"``, ``"reference"``, ``"cameras"``, ``"global"``),
        ``"cameras"`` (photos in both chunks). ``fix_scale``: rigid, without scale.
        Returns a report with residuals, correspondences and transforms (see
        :func:`engine.chunks.align_chunks`)."""
        from ..engine.chunks import align_chunks

        return align_chunks(self, reference, chunks, method=method, fix_scale=fix_scale, initial=initial,
                            max_points=max_points, progress=progress)

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

    # ---------------------------------------------------------------- I/O
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
        if self._path is None:
            raise ValueError("save() needs a path for a new document")
        payload = {"format": "pyarchinit-photogrammetry", "version": FORMAT_VERSION, "meta": self.meta,
                   "chunks": [c.to_dict() for c in self.chunks]}
        tmp = self._path.with_suffix(".ppg.tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        tmp.replace(self._path)
        for r in self.removals:  # what the file now says: the end of the session settles the files on it
            r.mark_saved()
        return self._path

    def _read(self) -> None:
        data = json.loads(self._path.read_text())
        if data.get("format") != "pyarchinit-photogrammetry":
            raise ValueError(f"{self._path} is not a pyarchinit-photogrammetry project")
        self.meta = data.get("meta", {})
        self.chunks = [Chunk.from_dict(c, document=self) for c in data.get("chunks", [])]

    @classmethod
    def open(cls, path: str | Path) -> "Document":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(p)
        return cls(p)

    def __repr__(self) -> str:
        return f"<Document {self._path or '(unsaved)'} chunks={len(self.chunks)}>"
