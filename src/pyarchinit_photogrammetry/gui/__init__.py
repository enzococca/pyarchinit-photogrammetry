"""Desktop interface (PySide6). Start it with ``ppg-gui [project.ppg]`` or ``ppg gui``.

Everything the interface does goes through the Python API on
``pyarchinit_photogrammetry.app.document``, which the built-in console exposes
as ``doc`` / ``chunk``.
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:  # pragma: no cover - depends on the installation
        print("The interface needs PySide6: pip install \"pyarchinit-photogrammetry[gui]\"", file=sys.stderr)
        return 1
    import os

    os.environ.setdefault("GLOG_minloglevel", "2")
    from ..app import app as application
    from ..core.document import Document
    from .main_window import MainWindow

    qapp = QApplication.instance() or QApplication([sys.argv[0]] + argv)
    qapp.setApplicationName("pyarchinit-photogrammetry")
    qapp.setOrganizationName("pyarchinit")
    project = next((a for a in argv if a.endswith(".ppg")), None)
    if project:
        application.document = Document.open(project)
    win = MainWindow(application.document)
    geo = win.settings.value("geometry")
    if geo is not None:
        win.restoreGeometry(geo)
        state = win.settings.value("state")
        if state is not None:
            win.restoreState(state)
    win.show()
    return qapp.exec()


__all__ = ["main"]
