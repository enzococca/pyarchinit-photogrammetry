"""Desktop interface (PySide6). Start it with ``ppg-gui [project.ppg]`` or ``ppg gui``.

Everything the interface does goes through the Python API on
``pyarchinit_photogrammetry.app.document``, which the built-in console exposes
as ``doc`` / ``chunk``.

Options: ``--check`` opens the window, renders it and exits (smoke test for the
packaged application); ``--doctor`` prints the environment diagnosis; ``--ppg-worker``
is used internally by the packaged application to run isolated worker processes.
"""
from __future__ import annotations

import os
import sys

APP_TITLE = "PyArchInit Photogrammetry"


def _hard_exit(code: int) -> None:
    """Leave the packaged application on macOS/Linux without interpreter finalisation
    (segmentation faults with VTK/Open3D/SpatiaLite loaded). On Windows the normal exit is
    the safe one: skipping it ends in an access violation in the DLL detach callbacks."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(int(code))


def _frozen_posix() -> bool:
    return bool(getattr(sys, "frozen", False)) and os.name != "nt"


def app_settings():
    """The preferences of the interface. In the application they live in the native store
    (``~/Library/Preferences/com.pyarchinit.pyarchinit-photogrammetry.plist``, the Windows
    registry); ``QSettings.setDefaultFormat``/``setPath`` redirect them (tests do), which the
    two-argument ``QSettings(organization, application)`` constructor would ignore."""
    from PySide6.QtCore import QSettings

    return QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, "pyarchinit", "pyarchinit-photogrammetry")


def install_translations(qapp) -> bool:
    """Italian texts for Qt's own widgets (OK/Annulla/Salva buttons, file dialogs)."""
    from PySide6.QtCore import QLibraryInfo, QTranslator

    path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    ok = False
    for name in ("qtbase_it", "qt_it"):
        tr = QTranslator(qapp)
        if tr.load(name, path):
            qapp.installTranslator(tr)
            ok = True
            break
    return ok


def _splash():
    """The start splash of :mod:`ppg_launcher` (None when it cannot be shown)."""
    try:
        from ppg_launcher import Splash

        return Splash()
    except Exception:  # pragma: no cover - launcher module missing: no splash
        return None


def main(argv: list[str] | None = None, splash=None, started: float | None = None,
         import_times: dict | None = None) -> int:
    """Start the interface. ``splash`` (from :mod:`ppg_launcher`, which shows it before the
    heavy imports) goes on showing the progress until the window appears."""
    import time

    started = time.perf_counter() if started is None else started
    argv = list(sys.argv[1:] if argv is None else argv)
    from ..engine.isolate import WORKER_FLAG, freeze_support, worker_main

    if argv[:1] == [WORKER_FLAG]:  # child process of run_isolated in the packaged application
        return worker_main(argv)
    freeze_support()
    os.environ.setdefault("GLOG_minloglevel", "2")
    os.environ.setdefault("QT_API", "pyside6")  # qtpy (pyvistaqt) must not pick another binding
    if "--doctor" in argv:
        from ..doctor import main as doctor_main

        code = doctor_main()
        if _frozen_posix():  # skip interpreter finalisation: it can crash the packaged app
            _hard_exit(code)
        return code
    try:
        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtWidgets import QApplication
    except ImportError:  # pragma: no cover - depends on the installation
        print("The interface needs PySide6: pip install \"pyarchinit-photogrammetry[gui]\"", file=sys.stderr)
        return 1
    from . import theme
    from .logging_setup import install

    log_path = install()
    from ..app import app as application
    from ..core.document import Document
    from .icons import app_icon

    if QApplication.instance() is None:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    qapp = QApplication.instance() or QApplication([sys.argv[0]] + argv)
    qapp.setApplicationName("pyarchinit-photogrammetry")
    qapp.setApplicationDisplayName(APP_TITLE)
    qapp.setOrganizationName("pyarchinit")
    qapp.setWindowIcon(app_icon())
    install_translations(qapp)
    settings = app_settings()
    theme.apply(qapp, str(settings.value("theme", "light")))
    splash = splash if splash is not None else _splash()
    step = splash.step if splash is not None else (lambda *_a, **_k: None)
    step(84, "Finestra principale")
    from .main_window import MainWindow

    project = next((a for a in argv if a.endswith(".ppg")), None)
    open_error = None
    if project:
        step(88, f"Apertura di {os.path.basename(project)}")
        try:
            application.document = Document.open(project)
        except Exception as exc:
            open_error = f"Impossibile aprire {project}: {exc}"
    win = MainWindow(application.document if project and not open_error else None, autoconnect="--check" not in argv,
                     progress=splash.progress(88, 99) if splash is not None else None)
    geo = win.settings.value("geometry")
    if geo is not None:
        win.restoreGeometry(geo)
        state = win.settings.value("state")
        if state is not None:
            win.restoreState(state)
    win.show()
    if splash is not None:
        step(100, "Pronto")
        splash.close()
    import logging

    times = ", ".join(f"{k} {v:.1f} s" for k, v in (import_times or {}).items() if v >= 0.1)
    logging.getLogger("pyarchinit_photogrammetry.gui").info(
        "Avvio in %.1f s%s", time.perf_counter() - started, f" ({times})" if times else "")
    if open_error:
        from .dialogs import ErrorDialog

        QTimer.singleShot(0, lambda: ErrorDialog("Apertura progetto", open_error, log_path=log_path, parent=win).exec())
    if "--check" in argv:
        def check() -> None:
            ok = win.isVisible() and win.width() > 100
            try:
                import pyvistaqt  # noqa: F401
                import vtkmodules.vtkRenderingOpenGL2  # noqa: F401

                vtk = "ok"
            except Exception as exc:
                vtk = f"missing ({exc})"
            try:  # libraries the smoke test of the packaged application must find
                import laspy  # noqa: F401
                import pyproj  # noqa: F401
                from rasterio.crs import CRS

                from ..core import crs as _crs  # noqa: F401

                CRS.from_epsg(4326).to_wkt()
                pyproj.CRS.from_epsg(4326).to_wkt()
                from sklearn.ensemble import RandomForestClassifier  # trainable point classifier

                RandomForestClassifier(n_estimators=2).fit([[0.0], [1.0]], [0, 1])
                libs = "ok"
            except Exception as exc:
                libs = f"missing ({str(exc).splitlines()[0][:120]})"
            print(f"GUI check: {'ok' if ok else 'FAILED'} | window: {win.windowTitle()} | 3D backend: "
                  f"{win.model_view.backend} | vtk: {vtk} | libs: {libs} | log: {log_path}", flush=True)
            if _frozen_posix():  # see --doctor
                _hard_exit(0 if ok else 2)
            qapp.exit(0 if ok else 2)

        QTimer.singleShot(1500, check)
    return qapp.exec()


__all__ = ["main", "APP_TITLE"]
