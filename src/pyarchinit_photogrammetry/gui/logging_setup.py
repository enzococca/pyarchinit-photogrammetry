"""Log file, crash file and uncaught-exception handling for the desktop interface.

``install()`` writes a rotating ``ppg-gui.log`` and a ``crash.log`` (faulthandler, for
native crashes) in the platform log folder, forwards Qt messages to :mod:`logging` and
routes uncaught Python exceptions to the log and to the error dialog.
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import platform
import sys
import threading
import traceback
from pathlib import Path

APP_NAME = "pyarchinit-photogrammetry"
_state: dict = {}


def log_dir() -> Path:
    """Platform log folder (``PPG_LOG_DIR`` overrides it)."""
    env = os.environ.get("PPG_LOG_DIR")
    if env:
        base = Path(env)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs" / APP_NAME
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME / "logs"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def log_file() -> Path:
    return log_dir() / "ppg-gui.log"


def install(level: int = logging.INFO) -> Path:
    """Set up file logging, faulthandler and the exception hooks; returns the log file."""
    if _state.get("installed"):
        return log_file()
    folder = log_dir()
    path = folder / "ppg-gui.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.WARNING:
        root.setLevel(logging.WARNING)
    logging.getLogger("pyarchinit_photogrammetry").setLevel(level)
    crash = open(folder / "crash.log", "a", encoding="utf-8")
    faulthandler.enable(file=crash, all_threads=True)
    _state.update(installed=True, handler=handler, crash_file=crash)
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    _install_qt_handler()
    log = logging.getLogger("pyarchinit_photogrammetry.gui")
    from .. import __version__

    log.info("=== %s %s | Python %s | %s %s | %s%s", APP_NAME, __version__, platform.python_version(),
             platform.system(), platform.release(), sys.executable, " (frozen)" if getattr(sys, "frozen", False) else "")
    return path


def _excepthook(exc_type, exc, tb) -> None:
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    logging.getLogger("pyarchinit_photogrammetry.gui").error("Uncaught exception:\n%s", text)
    _notify(f"{exc_type.__name__}: {exc}", text)


def _thread_excepthook(args) -> None:
    _excepthook(args.exc_type, args.exc_value, args.exc_traceback)


def _notify(summary: str, details: str) -> None:
    """Show the error dialog from the GUI thread, if a QApplication exists."""
    try:
        from PySide6.QtCore import QObject, Qt, Signal
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return
        bridge = _state.get("bridge")
        if bridge is None:
            class _Bridge(QObject):
                error = Signal(str, str)

            bridge = _Bridge()
            bridge.error.connect(_show_error, Qt.ConnectionType.QueuedConnection)
            _state["bridge"] = bridge
        bridge.error.emit(summary, details)
    except Exception:  # pragma: no cover - never let the hook itself fail
        pass


def _show_error(summary: str, details: str) -> None:
    from PySide6.QtWidgets import QApplication

    from .dialogs import ErrorDialog

    ErrorDialog("Errore imprevisto", summary, details, parent=QApplication.activeWindow()).exec()


def _install_qt_handler() -> None:
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:  # pragma: no cover
        return
    levels = {QtMsgType.QtDebugMsg: logging.DEBUG, QtMsgType.QtInfoMsg: logging.INFO,
              QtMsgType.QtWarningMsg: logging.WARNING, QtMsgType.QtCriticalMsg: logging.ERROR,
              QtMsgType.QtFatalMsg: logging.CRITICAL}
    qt_log = logging.getLogger("qt")

    def handler(msg_type, context, message):
        qt_log.log(levels.get(msg_type, logging.WARNING), "%s", message)

    qInstallMessageHandler(handler)
    _state["qt_handler"] = handler


def recent_lines(n: int = 200) -> str:
    """Last lines of the log file (for the error dialog and bug reports)."""
    try:
        lines = log_file().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


__all__ = ["install", "log_dir", "log_file", "recent_lines", "APP_NAME"]
