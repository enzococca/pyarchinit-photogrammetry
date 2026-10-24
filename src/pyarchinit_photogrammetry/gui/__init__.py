"""Desktop interface (PySide6). Start it with ``ppg-gui [project.ppg]`` or ``ppg gui``.

Everything the interface does goes through the Python API on
``pyarchinit_photogrammetry.app.document``, which the built-in console exposes
as ``doc`` / ``chunk``.

Options: ``--check`` opens the window, renders it and exits (smoke test for the
packaged application); ``--doctor`` prints the environment diagnosis; ``--run script.py
[arguments]`` runs a Python script with the program's Python and modules, without the window
(the packaged application can be scripted as Metashape with ``-r``); ``--ppg-worker`` is used
internally by the packaged application to run isolated worker processes.
"""
from __future__ import annotations

import os
import sys

from .i18n import tr

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


def run_script(args: list[str]) -> int:
    """``--run script.py [arguments]``: the script runs as ``__main__`` with ``sys.argv`` set to
    the script and its arguments (``pyarchinit_photogrammetry.app`` holds the open project, as
    in ``ppg run``). Returns its exit code: that of ``sys.exit``, 1 for an error."""
    import runpy
    import traceback

    if not args:
        print("--run: the script to run is missing", file=sys.stderr)
        return 2
    sys.argv = list(args)
    try:
        runpy.run_path(args[0], run_name="__main__")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except Exception:
        traceback.print_exc()
        return 1
    return 0


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
        translator = QTranslator(qapp)
        if translator.load(name, path):
            qapp.installTranslator(translator)
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
    if "--doctor" in argv or argv[:1] == ["--run"]:
        if argv[:1] == ["--run"]:
            code = run_script(argv[1:])
        else:
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
    import locale

    try:  # Qt took the locale of the system: numbers for the C/C++ libraries in the C locale (see ppg_launcher)
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:  # pragma: no cover
        pass
    qapp.setApplicationName("pyarchinit-photogrammetry")
    qapp.setApplicationDisplayName(APP_TITLE)
    qapp.setOrganizationName("pyarchinit")
    qapp.setWindowIcon(app_icon())
    from . import i18n

    if not i18n.install(qapp):  # Italian: Qt's own buttons and dialogs in Italian too
        install_translations(qapp)
    settings = app_settings()
    theme.apply(qapp, str(settings.value("theme", "light")))
    splash = splash if splash is not None else _splash()
    step = splash.step if splash is not None else (lambda *_a, **_k: None)
    step(84, tr("Finestra principale"))
    from .main_window import MainWindow

    project = next((a for a in argv if a.endswith(".ppg")), None)
    open_error = None
    if project:
        step(88, tr("Apertura di {0}", os.path.basename(project)))
        try:
            application.document = Document.open(project)
        except Exception as exc:
            open_error = tr("Impossibile aprire {0}: {1}", project, exc)
    win = MainWindow(application.document if project and not open_error else None, autoconnect="--check" not in argv,
                     check_updates=False if "--check" in argv else None,
                     progress=splash.progress(88, 99) if splash is not None else None)
    geo = win.settings.value("geometry")
    if geo is not None:
        win.restoreGeometry(geo)
        win.restore_layout(win.settings.value("state"))  # the panes as left (a layout before 1.3.4 is ignored)
    win.show()
    if splash is not None:
        step(100, tr("Pronto"))
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
                import numpy as np  # cleaning and cropping: scipy graphs and trees, shapely, Open3D holes

                from ..engine.cleaning import connected_groups, statistical_outliers
                from ..engine.crop import as_polygon, inside_xy

                pts = np.random.default_rng(0).random((200, 3))
                connected_groups(pts, 0.2)
                statistical_outliers(pts, 8, 2.0)
                inside_xy(as_polygon([[0, 0], [1, 0], [0, 1]]), pts[:, 0], pts[:, 1])
                import open3d as o3d

                box = o3d.t.geometry.TriangleMesh.create_box()
                box.fill_holes(hole_size=1.0)
                import tempfile  # a figure in every format matplotlib loads by extension (plugins write SVG, PDF)

                from matplotlib.figure import Figure

                fig = Figure()
                fig.add_subplot().plot([0, 1], [0, 1])
                with tempfile.TemporaryDirectory() as tmp:
                    for ext in ("png", "svg", "pdf", "eps"):
                        fig.savefig(os.path.join(tmp, f"check.{ext}"))
                    # the PDF report (fpdf2, fontTools, the fonts) and the Excel tables (openpyxl)
                    from ..io.pdf_report import sample_pdf
                    from ..io.tables import read_xlsx, write_xlsx

                    if not sample_pdf(os.path.join(tmp, "report.pdf")).read_bytes().startswith(b"%PDF"):
                        raise RuntimeError("the PDF report is not a PDF")
                    book = write_xlsx(os.path.join(tmp, "tables.xlsx"), {"check": (["a", "b"], [[1, 2.5]])})
                    if read_xlsx(book)["check"][1] != [1, 2.5]:
                        raise RuntimeError("the Excel workbook does not read back")
                libs = "ok"
            except Exception as exc:
                libs = f"missing ({str(exc).splitlines()[0][:120]})"
            from .. import _native

            print(f"GUI check: {'ok' if ok else 'FAILED'} | window: {win.windowTitle()} | 3D backend: "
                  f"{win.model_view.backend} | vtk: {vtk} | libs: {libs} | native: {_native.version() or 'missing'} | "
                  f"log: {log_path}", flush=True)
            if _frozen_posix():  # see --doctor
                _hard_exit(0 if ok else 2)
            qapp.exit(0 if ok else 2)

        QTimer.singleShot(1500, check)
    return qapp.exec()


__all__ = ["main", "APP_TITLE"]
