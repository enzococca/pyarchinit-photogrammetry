"""Start of the desktop application (``ppg-gui`` and the packaged application): a splash with
a progress bar at once, then the heavy libraries one by one.

The module lives outside the ``pyarchinit_photogrammetry`` package on purpose: importing the
package loads Open3D, NumPy and PROJ, and the splash has to appear before that. It needs only
PySide6. :class:`Splash` also shows the progress of a project being opened.
"""
from __future__ import annotations

import importlib
import os
import sys
import time

TITLE = "PyArchInit Photogrammetry"
SUBTITLE = "Fotogrammetria open source per l'archeologia e il BIM"


def _logo_path() -> str | None:
    """``gui/resources/logo.svg`` of the package, found without importing the package."""
    import importlib.util

    try:
        spec = importlib.util.find_spec("pyarchinit_photogrammetry")
    except (ImportError, ValueError):
        return None
    for folder in (spec.submodule_search_locations or []) if spec else []:
        path = os.path.join(folder, "gui", "resources", "logo.svg")
        if os.path.exists(path):
            return path
    return None


def _splash_class():
    from PySide6.QtCore import QEventLoop, QRectF, Qt
    from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen
    from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget

    class Splash(QWidget):
        """A rounded card in the colours of the logo: logo, title, a progress bar in the middle
        with the percentage, and what is being loaded. ``parent`` shows it over a window
        (opening a project) and keeps the clicks away from that window meanwhile."""

        WIDTH, HEIGHT = 560, 330

        def __init__(self, title: str = TITLE, subtitle: str = SUBTITLE, parent=None):
            flags = Qt.WindowType.FramelessWindowHint | (Qt.WindowType.Dialog if parent is not None
                                                         else Qt.WindowType.SplashScreen)
            super().__init__(parent, flags)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            if parent is not None:
                self.setWindowModality(Qt.WindowModality.ApplicationModal)
            self.setFixedSize(self.WIDTH, self.HEIGHT)
            lay = QVBoxLayout(self)
            lay.setContentsMargins(40, 34, 40, 26)
            lay.setSpacing(10)
            head = QHBoxLayout()
            head.setSpacing(18)
            logo = QLabel()
            path = _logo_path()
            if path:
                logo.setPixmap(QIcon(path).pixmap(84, 84))
            logo.setFixedSize(84, 84)
            head.addWidget(logo)
            titles = QVBoxLayout()
            titles.setSpacing(4)
            self.title = QLabel(title)
            self.title.setStyleSheet("color: white; font-size: 23px; font-weight: 700;")
            self.subtitle = QLabel(subtitle)
            self.subtitle.setWordWrap(True)
            self.subtitle.setStyleSheet("color: #c9d8e8; font-size: 13px;")
            titles.addStretch(1)
            titles.addWidget(self.title)
            titles.addWidget(self.subtitle)
            titles.addStretch(1)
            head.addLayout(titles, 1)
            lay.addLayout(head)
            lay.addStretch(1)
            self.bar = QProgressBar()
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
            self.bar.setFormat("%p%")
            self.bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.bar.setFixedHeight(24)
            self.bar.setStyleSheet(
                "QProgressBar { background: rgba(255, 255, 255, 40); border: 1px solid rgba(255, 255, 255, 60);"
                " border-radius: 12px; color: white; font-weight: 700; font-size: 13px; }"
                "QProgressBar::chunk { border-radius: 11px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
                " stop:0 #a66a35, stop:0.6 #c98a4b, stop:1 #e8b27a); }")
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(self.bar, 10)
            row.addStretch(1)
            lay.addLayout(row)
            self.status = QLabel("")
            self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.status.setStyleSheet("color: #e8dcc8; font-size: 13px;")
            lay.addWidget(self.status)
            lay.addStretch(1)
            self.footer = QLabel("")
            self.footer.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.footer.setStyleSheet("color: #8fa9c4; font-size: 11px;")
            lay.addWidget(self.footer)
            self._qapp = QApplication

        def paintEvent(self, _ev) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            card = QRectF(self.rect()).adjusted(1, 1, -1, -1)
            path = QPainterPath()
            path.addRoundedRect(card, 22, 22)
            grad = QLinearGradient(card.topLeft(), card.bottomRight())
            grad.setColorAt(0.0, QColor("#2c5d8f"))
            grad.setColorAt(1.0, QColor("#153552"))
            p.fillPath(path, grad)
            # the layers of a trench section along the bottom, as in the logo
            p.setClipPath(path)
            w, h = card.width(), card.height()
            for k, (top, colour) in enumerate(((0.80, "#c98a4b"), (0.87, "#a66a35"), (0.94, "#7d4c27"))):
                band = QPainterPath()
                y = card.top() + h * top
                band.moveTo(card.left(), y)
                band.cubicTo(card.left() + w * 0.3, y - 10 + 3 * k, card.left() + w * 0.6, y + 12, card.right(), y - 4)
                band.lineTo(card.right(), card.bottom())
                band.lineTo(card.left(), card.bottom())
                band.closeSubpath()
                c = QColor(colour)
                c.setAlpha(150)
                p.fillPath(band, c)
            p.setClipping(False)
            p.setPen(QPen(QColor(255, 255, 255, 50), 1))
            p.drawPath(path)
            p.end()

        def set_title(self, title: str, subtitle: str | None = None) -> None:
            self.title.setText(title)
            if subtitle is not None:
                self.subtitle.setText(subtitle)

        def step(self, percent: float, text: str | None = None) -> None:
            """Show ``percent`` (0-100) and what is being done; repaints at once."""
            self.bar.setValue(int(round(max(0.0, min(100.0, percent)))))
            if text is not None:
                self.status.setText(text)
            if not self.isVisible():
                self.show()
            self.repaint()
            self._qapp.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

        def show(self) -> None:
            parent = self.parentWidget()
            if parent is not None and parent.isVisible():
                g = parent.frameGeometry()
                self.move(g.center().x() - self.WIDTH // 2, g.center().y() - self.HEIGHT // 2)
            else:
                screen = self._qapp.primaryScreen()
                if screen is not None:
                    c = screen.availableGeometry().center()
                    self.move(c.x() - self.WIDTH // 2, c.y() - self.HEIGHT // 2)
            super().show()
            self.raise_()

        def progress(self, start: float = 0.0, end: float = 100.0):
            """``callback(fraction, text)`` mapping 0..1 into ``start``..``end`` percent."""
            return lambda f, text=None: self.step(start + (end - start) * f, text)

    return Splash


_SPLASH = None


def Splash(*args, **kwargs):  # noqa: N802 - a class built on first use (PySide6 imported lazily)
    global _SPLASH
    if _SPLASH is None:
        _SPLASH = _splash_class()
    return _SPLASH(*args, **kwargs)


# the heavy imports of the start, in order, with the share of the bar they take
STEPS = [
    (6, "Librerie di calcolo (NumPy, Open3D)", "pyarchinit_photogrammetry"),
    (28, "Geodesia (PROJ, GDAL)", "rasterio"),
    (38, "Sistemi di riferimento", "pyproj"),
    (48, "Fotogrammetria (COLMAP)", "pycolmap"),
    (62, "Vista 3D (VTK)", "pyvistaqt"),
    (76, "Interfaccia", "pyarchinit_photogrammetry.gui.main_window"),
]  # the others (OpenCV, trimesh, scikit-learn...) load when first needed


def c_numbers() -> None:
    """Numbers in the C locale for the C and C++ libraries. Qt sets the locale of the system when
    the application starts (on macOS and Linux): with an Italian system the libraries reading
    numbers with strtod - IfcOpenShell's geometry - read "1.5" as 1 and the IFC preview came out
    empty. The interface keeps formatting numbers the Italian way (QLocale is not affected)."""
    import locale

    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:  # pragma: no cover
        pass


def _persistent_matplotlib_cache() -> None:
    """The 3D view (pyvistaqt) loads matplotlib. In the packaged application PyInstaller points
    matplotlib to a new temporary folder at every start, so its font cache was built again each
    time (seconds): keep it in the user's cache folder instead."""
    if not getattr(sys, "frozen", False):
        return
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    folder = os.path.join(base, "pyarchinit-photogrammetry", "matplotlib")
    try:
        os.makedirs(folder, exist_ok=True)
        os.environ["MPLCONFIGDIR"] = folder
    except OSError:
        pass


_crash_file = None  # kept open while the application runs


def crash_log() -> bool:
    """Native crashes (segfault, abort) written to ``crash.log`` in the log folder of the
    interface (as ``gui.logging_setup``, ``PPG_LOG_DIR`` overrides it) from the very start,
    before the heavy libraries load. A system that refuses it - the macOS runners of GitHub
    refused faulthandler's signal stack once the libraries were loaded - only loses the crash
    file: the application starts anyway. True when it is on."""
    global _crash_file
    import faulthandler

    if faulthandler.is_enabled():
        return True
    folder = os.environ.get("PPG_LOG_DIR")
    if not folder:
        if sys.platform == "darwin":
            folder = os.path.expanduser("~/Library/Logs/pyarchinit-photogrammetry")
        elif os.name == "nt":
            folder = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local"),
                                  "pyarchinit-photogrammetry", "logs")
        else:
            folder = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                                  "pyarchinit-photogrammetry", "logs")
    try:
        os.makedirs(folder, exist_ok=True)
        _crash_file = open(os.path.join(folder, "crash.log"), "a", encoding="utf-8")
        faulthandler.enable(file=_crash_file, all_threads=True)
        return True
    except OSError:
        if _crash_file is not None:
            _crash_file.close()
        _crash_file = None
        return False


def main(argv: list[str] | None = None) -> int:
    started = time.perf_counter()
    argv = list(sys.argv[1:] if argv is None else argv)
    _persistent_matplotlib_cache()
    if (argv and argv[0].startswith("--ppg-")) or "--doctor" in argv:  # worker processes, diagnosis: no window
        from pyarchinit_photogrammetry.gui import main as gui_main

        return gui_main(argv)
    crash_log()
    os.environ.setdefault("QT_API", "pyside6")  # qtpy (pyvistaqt) must not pick another binding
    os.environ.setdefault("GLOG_minloglevel", "2")
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
    except ImportError:  # pragma: no cover - depends on the installation
        from pyarchinit_photogrammetry.gui import main as gui_main

        return gui_main(argv)
    if QApplication.instance() is None:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
        QApplication([sys.argv[0]] + argv)
    c_numbers()
    splash = Splash()
    splash.step(2, "Avvio")
    times = {}
    for percent, text, module in STEPS:
        if module == "pyvistaqt" and os.environ.get("PPG_RENDERER") == "software":
            continue  # no VTK view
        splash.step(percent, text)
        t = time.perf_counter()
        try:
            importlib.import_module(module)
        except Exception:  # optional libraries: the interface copes without them
            pass
        times[module] = time.perf_counter() - t
        if module == "pyarchinit_photogrammetry":
            from pyarchinit_photogrammetry import __version__

            splash.footer.setText(f"versione {__version__}")
    from pyarchinit_photogrammetry.gui import main as gui_main

    return gui_main(argv, splash=splash, started=started, import_times=times)


if __name__ == "__main__":
    sys.exit(main())
