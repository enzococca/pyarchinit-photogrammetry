"""Application state shared by scripts, the command line and the GUI.

Scripts can work on the open project through ``app.document`` exactly as the
GUI does::

    import pyarchinit_photogrammetry as ppg
    chunk = ppg.app.document.chunk
    chunk.build_dem(resolution=0.01)
"""
from __future__ import annotations

from .core.document import Document
from .core.progress import ProgressCallback, print_progress


class Application:
    def __init__(self):
        self.document = Document()
        self.progress: ProgressCallback | None = print_progress
        self.settings: dict = {}

    def open_document(self, path) -> Document:
        self.document = Document.open(path)
        return self.document

    def new_document(self) -> Document:
        self.document = Document()
        return self.document


app = Application()
