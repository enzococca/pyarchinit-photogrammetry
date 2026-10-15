"""Document: the project file.

A project is ``name.ppg`` (JSON) plus the folder ``name.ppg.files/`` with the
heavy data of each chunk (feature database, depth maps, clouds, meshes, rasters).
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
        """The active chunk (the first enabled one)."""
        return next((c for c in self.chunks if c.enabled), None)

    def add_chunk(self, label: str | None = None) -> Chunk:
        key = max((c.key for c in self.chunks), default=-1) + 1
        chunk = Chunk(key=key, label=label or f"Chunk {key + 1}", document=self)
        self.chunks.append(chunk)
        return chunk

    def remove_chunk(self, chunk: Chunk) -> None:
        self.chunks.remove(chunk)
        shutil.rmtree(self.files_dir / f"chunk_{chunk.key}", ignore_errors=True)

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
