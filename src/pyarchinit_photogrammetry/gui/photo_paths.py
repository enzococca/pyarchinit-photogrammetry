"""*Cambia percorso...* of the photos as one step of Modifica > Annulla: the files the cameras of a chunk read (and the
raw files of developed photos, and the folder the alignment model reads them from) before and after."""
from __future__ import annotations

import copy


def state(chunk) -> dict:
    """What a change of the photo paths of ``chunk`` touches, as JSON-able values."""
    cams = {}
    for c in chunk.cameras:
        raw = c.meta.get("raw")
        cams[c.key] = (c.path, copy.deepcopy(raw) if isinstance(raw, dict) else None)
    return {"cameras": cams, "image_root": (chunk.meta.get("sfm") or {}).get("image_root")}


def put(chunk, value: dict) -> None:
    for c in chunk.cameras:
        if c.key not in value["cameras"]:
            continue
        path, raw = value["cameras"][c.key]
        c.path = path
        if raw is None:
            c.meta.pop("raw", None)
        else:
            c.meta["raw"] = copy.deepcopy(raw)
    sfm = chunk.meta.get("sfm")
    if isinstance(sfm, dict) and value["image_root"] is not None:
        sfm["image_root"] = value["image_root"]


class PhotoPathsStep:
    """The paths of the photos of a chunk changed (one photo, or several matched in a folder)."""

    def __init__(self, window, chunk, before: dict, after: dict):
        self.window, self.chunk, self.before, self.after = window, chunk, before, after

    def _apply(self, value: dict) -> None:
        put(self.chunk, value)
        self.window.setWindowModified(True)
        if self.window.chunk is self.chunk:
            self.window.photos.set_chunk(self.chunk)

    def undo(self) -> None:
        self._apply(self.before)

    def redo(self) -> None:
        self._apply(self.after)
