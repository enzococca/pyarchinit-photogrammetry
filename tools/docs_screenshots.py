"""Screenshots of the desktop guide (``docs/gui/``): the window of the program on the demo
project, its menus, bars and panes, in English and in Italian.

    QT_QPA_PLATFORM=offscreen PPG_RENDERER=software python tools/docs_screenshots.py [DEMO] [--out docs/img/gui]

``DEMO`` is the folder of the processed demo project (``ppg demo DEMO --process``), made when it is
missing. Every picture is written twice: ``<name>.png`` in English and ``<name>.it.png`` in
Italian, the name the Italian pages of the site pick by themselves (mkdocs-static-i18n). The
program runs with its own settings, plugins and home folder, so nothing of the user's is read or
written. The GNSS pictures come from a made-up flight over the demo (``tools/docs_gnss.py``), its photos given PPK
positions in memory only; the lists of the permanent stations are kept in ``DEMO/gnss_data``."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIZE = (1400, 860)  # the size of the screenshots of the tutorial


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("demo", nargs="?", type=Path, default=Path(tempfile.gettempdir()) / "ppg-docs-demo")
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "img" / "gui")
    ap.add_argument("--lang", nargs="+", default=["en", "it"])
    ap.add_argument("--only", nargs="*", default=None, help="names of the pictures to make (default: all)")
    ap.add_argument("--worker", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker:
        return _worker(args)
    project = args.demo / "scavo.ppg"
    if not project.exists():
        print(f"Demo project in {args.demo}")
        subprocess.run([sys.executable, "-m", "pyarchinit_photogrammetry.cli", "demo", str(args.demo), "--process"],
                       check=True)
    args.out.mkdir(parents=True, exist_ok=True)
    for lang in args.lang:
        home = Path(tempfile.mkdtemp(prefix="ppg-docs-home-"))
        screen = home / "screen.json"  # a tall screen: a long menu stays in one column, as on a desktop
        screen.write_text(json.dumps({"screens": [{"name": "docs", "x": 0, "y": 0, "width": 2560, "height": 2048,
                                                   "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}))
        env = dict(os.environ, PPG_LANG=lang, HOME=str(home), USERPROFILE=str(home), PPG_NO_UPDATE_CHECK="1",
                   PPG_PLUGINS_DIR=str(home / "plugins"), PPG_PLUGINS_ENVIRONMENT="0",
                   PPG_GNSS_DIR=str(args.demo / "gnss_data"),  # the lists of the permanent stations, read once
                   QT_QPA_PLATFORM=f"offscreen:configfile={screen}",
                   PPG_RENDERER=os.environ.get("PPG_RENDERER", "software"))
        cmd = [sys.executable, __file__, str(args.demo), "--out", str(args.out), "--worker", lang]
        if args.only:
            cmd += ["--only", *args.only]
        subprocess.run(cmd, check=True, env=env)
    return 0


# ---------------------------------------------------------------- in the window, one language
def _worker(args) -> int:
    from PySide6.QtCore import QPoint, QRect, QSettings, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
    from PySide6.QtWidgets import QApplication, QDialog, QDockWidget, QToolBar

    lang, out, only = args.worker, args.out, set(args.only or [])
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                      os.path.join(os.environ["HOME"], "settings"))
    app.setApplicationName("pyarchinit-photogrammetry")
    app.setOrganizationName("pyarchinit")
    app.setFont(QFont("Inter", 10))
    from pyarchinit_photogrammetry.core.document import Document
    from pyarchinit_photogrammetry.gui import i18n, install_translations, theme

    if not i18n.install(app):  # Italian: the buttons of Qt's own windows in Italian too, as in the program
        install_translations(app)
    theme.apply(app, "light")
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    def pump(ms: int = 200) -> None:
        end = time.monotonic() + ms / 1000
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)

    def wanted(name: str) -> bool:
        return not only or name in only

    def save(image, name: str) -> None:
        img = image.toImage() if hasattr(image, "toImage") else image
        path = out / (f"{name}.png" if lang == "en" else f"{name}.{lang}.png")
        img.save(str(path), "PNG")
        _shrink(path, flat=not name.startswith("window"))
        print(f"  {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}  {img.width()}x{img.height()}")

    # dialogs opened by the actions are shown, taken and closed instead of waiting for the user
    taken: dict[str, QImage] = {}

    def fake_exec(self, *a):
        self.show()
        pump(300)
        from PySide6.QtWidgets import QLabel, QLineEdit

        for field in self.findChildren(QLineEdit) + self.findChildren(QLabel):  # the demo as in a home folder
            if str(args.demo) in field.text():
                field.setText(field.text().replace(str(args.demo), "~/ppg_demo"))
        pump(50)
        taken["last"] = self.grab().toImage()
        self.hide()
        return QDialog.DialogCode.Rejected

    QDialog.exec = fake_exec

    doc = Document.open(args.demo / "scavo.ppg")
    w = MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.resize(*SIZE)
    w.show()
    pump(1500)
    for _ in range(50):  # the thumbnails of the photos come in the background
        pump(100)
    w._show_raster("dem")  # the DEM in its colours, as in the tutorial
    pump(800)
    import rasterio

    with rasterio.open(w.chunk.dem_path) as src:  # the cursor in the middle of the DEM: its values in the bar
        b = src.bounds
        cx, cy = b.left + 0.62 * (b.right - b.left), b.bottom + 0.55 * (b.top - b.bottom)
        cz = float(next(src.sample([(cx, cy)]))[0])
    w.ortho_view.mouse_world.emit(cx, cy, cz)
    pump(200)
    print(f"{lang}: {w.windowTitle()}")

    # ---- the window with the numbers of its areas
    if wanted("window-areas"):
        save(_areas(w, QPainter, QColor, QPen, QFont, QRect, QPoint, QDockWidget, QToolBar), "window-areas")

    # ---- the menus
    for name, path in _MENUS:
        if wanted(name):
            save(_menu_picture(w, path, pump, QImage, QPainter, QColor, QPen, QPoint, QRect), name)

    # ---- the bars, and the icons of their buttons (the same in every language)
    bars = {b.objectName(): b for b in w.findChildren(QToolBar)}
    for name, key in (("toolbar-workflow", "workflow_toolbar"), ("toolbar-drawing", "drawing_toolbar")):
        if wanted(name):
            save(bars[key].grab(), name)
    if wanted("icons"):
        _icons(bars, out / "icons", lang)

    # a modal window opened by Qt itself (a question, a file dialog) is taken and closed
    from PySide6.QtCore import QTimer

    def close_modal() -> None:
        m = app.activeModalWidget()
        if m is not None:
            taken["modal"] = m.grab().toImage()
            m.close()

    guard = QTimer()
    guard.timeout.connect(close_modal)
    guard.start(400)

    # ---- the PyArchInit database of the demo: the pane, the units on the orthophoto, the layers
    w.pyarchinit_connect(config=str(args.demo / "pyarchinit" / "pyarchinit_DB_folder" / "config.cfg"))
    pump(500)
    w.set_project_site("Saggio dimostrativo")
    w.pyarchinit_load_units()
    pump(800)
    from PySide6.QtWidgets import QLabel

    for label in w.pa_pane.findChildren(QLabel):  # the database as it is in a home folder, not in this run's
        if str(args.demo) in label.text():
            label.setText(label.text().replace(str(args.demo), "~/ppg_demo"))

    def dock_of(widget):
        for d in w.findChildren(QDockWidget):
            if d.widget() is widget or (d.widget() is not None and d.widget().isAncestorOf(widget)):
                return d
        raise LookupError(widget)

    def pane_shot(widget, name: str) -> None:
        """The pane in front of its group, with the tabs of the group above it."""
        from PySide6.QtWidgets import QTabBar

        d = dock_of(widget)
        d.show()
        d.raise_()
        pump(400)
        r = d.geometry()
        for bar in w.findChildren(QTabBar):
            if bar.metaObject().className() != "QMainWindowTabBar" or not bar.isVisible():
                continue
            b = QRect(bar.mapTo(w, QPoint(0, 0)), bar.size())
            if b.left() < r.right() and b.right() > r.left() and abs(b.bottom() - r.top()) < 8:
                r = r.united(b)
        save(w.grab(r), name)

    panes = [("pane-workspace", w.workspace), ("pane-reference", w.reference), ("pane-layers", w.layers_pane),
             ("pane-photos", w.photos), ("pane-log", w.log_pane), ("pane-console", w.console),
             ("pane-pyarchinit", w.pa_pane), ("pane-proxies", w.proxies_pane), ("pane-multispectral", w.ms_pane),
             ("pane-thermal", w.thermal_pane)]

    def layers_ready() -> None:  # the units of PyArchInit open, the first one with its attributes
        tree = w.layers_pane.tree
        tree.collapseAll()
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            if item.text(0).startswith("US da PyArchInit") or "PyArchInit" in item.text(0):
                item.setExpanded(True)
                if item.childCount():
                    tree.setCurrentItem(item.child(0))
                    w.layers_pane._clicked(item.child(0), 0)
                break
        pump(300)

    w.resizeDocks([dock_of(w.photos)], [300], Qt.Orientation.Vertical)  # the names under the thumbnails
    pump(300)

    def tall_shot(widget, name: str, width: int = 400) -> None:
        """A pane of steps floated on its own and tall enough to show all of them."""
        d = dock_of(widget)
        d.setFloating(True)
        d.resize(width, 1240)
        pump(600)
        img = d.grab().toImage()
        bg = img.pixel(img.width() // 2, img.height() - 12)
        bottom = img.height() - 12  # the empty part under the last step left out
        while bottom > 200 and all(img.pixel(x, bottom) == bg for x in range(8, img.width() - 24, 2)):
            bottom -= 1
        save(img.copy(0, 0, img.width(), min(img.height(), bottom + 16)), name)
        d.setFloating(False)
        pump(400)

    for name, widget in panes:
        if wanted(name):
            if name == "pane-layers":
                layers_ready()
            if name in ("pane-multispectral", "pane-thermal"):
                tall_shot(widget, name)
            else:
                pane_shot(widget, name)
    for widget in (w.workspace, w.photos, w.pa_pane):  # the panes in front as at the start
        dock_of(widget).raise_()
    pump(300)

    # ---- the central views
    def view_shot(name: str) -> None:
        pump(1200)
        save(w.stack.grab(), name)

    if wanted("view-ortho-units"):
        w._show_raster("ortho")
        view_shot("view-ortho-units")
    if wanted("view-vertices"):  # the Vertici tool on US 1: its vertices and the middle of its sides
        w._show_raster("ortho")
        pump(400)
        view = w.ortho_view
        layer = next(n for n in view.layers if "PyArchInit" in n)
        item = view.layers[layer]["features"][0][0]
        w.set_draw_mode("vertices")
        view.begin_vertex_edit(item)
        r = item.sceneBoundingRect()
        view.fitInView(r.adjusted(-0.35 * r.width(), -0.9 * r.height(), 0.35 * r.width(), 0.9 * r.height()),
                       Qt.AspectRatioMode.KeepAspectRatio)
        view_shot("view-vertices")
        view.end_vertex_edit(commit=False)
        w.set_draw_mode("pan")
    if wanted("view-model"):
        w._show_model("model_textured")
        view_shot("view-model")
    if wanted("view-cloud"):
        w._show_model("point_cloud")
        view_shot("view-cloud")
    if wanted("view-photo"):
        w.open_photo(w.chunk.cameras[6])
        view_shot("view-photo")
    w._show_raster("dem")
    pump(300)

    # ---- windows opened by the commands
    def dialog_shot(name: str, call) -> None:
        taken.pop("last", None)
        taken.pop("modal", None)
        call()
        pump(600)
        img = taken.get("last") or taken.get("modal")
        if img is None:
            print(f"  {name}: no window")
            return
        save(img, name)

    def error_window() -> None:  # a real error: a project that is not where it was
        import traceback

        from pyarchinit_photogrammetry.gui.dialogs import ErrorDialog
        from pyarchinit_photogrammetry.gui.i18n import tr

        try:
            Document.open(args.demo / "scavo_spostato.ppg")
        except Exception as exc:  # noqa: BLE001 - shown as the program shows it
            ErrorDialog("Apertura progetto", tr("Impossibile aprire {0}: {1}", "scavo_spostato.ppg", exc),
                        traceback.format_exc(), parent=w).exec()

    dialogs = [("dialog-error", error_window),
               ("dialog-raster-calculator", lambda: w.raster_calculator("dem - dem_prima", "differenza")),
               ("dialog-contours", w.contours),
               ("dialog-us-form", lambda: w.open_us_form("Saggio dimostrativo", "1", "1", "US")),
               ("dialog-align", w.align_photos), ("dialog-dense", w.build_dense), ("dialog-model", w.build_model),
               ("dialog-texture", w.build_texture), ("dialog-dem", w.build_dem), ("dialog-ortho", w.build_ortho),
               ("dialog-clean-cloud", w.clean_cloud), ("dialog-cut-model", lambda: w.cut_model()),
               ("dialog-batch", w.batch_process), ("dialog-nodes", w.show_nodes),
               ("dialog-relief", lambda: w.relief_visualisation()), ("dialog-volume", w.compute_volume),
               ("dialog-proxies", w.create_proxies)]
    for name, call in dialogs:
        if wanted(name):
            dialog_shot(name, call)

    # ---- GNSS: a made-up flight over the demo (tools/docs_gnss.py), its photos given PPK positions in memory only
    gnss_pictures = ("pane-gnss", "dialog-ppk-station", "dialog-ppk-ppp", "dialog-stations", "dialog-survey-crs",
                     "dialog-survey-crs-preview", "report-gnss")
    if any(wanted(n) for n in gnss_pictures):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import docs_gnss
        from pyarchinit_photogrammetry.gnss import heights, ppk
        from pyarchinit_photogrammetry.gui.ppk_dialog import PpkDialog
        from pyarchinit_photogrammetry.gui.stations_dialog import StationsDialog

        scene = docs_gnss.make_scene(w.chunk, args.demo / "scavo" / "photos", args.demo / "gnss")
        print(f"  GNSS: {scene.result['status']} against {scene.station.id}, {scene.distance_km:.1f} km")
        w._gnss_changed()
        pump(600)

        def neutral(widget) -> None:  # the demo as in a home folder, as fake_exec does
            from PySide6.QtWidgets import QLabel, QLineEdit

            for field in widget.findChildren(QLineEdit) + widget.findChildren(QLabel):
                if str(args.demo) in field.text():
                    field.setText(field.text().replace(str(args.demo), "~/ppg_demo"))

        def window_shot(dlg, name: str, ready=lambda: True, timeout: float = 90.0) -> None:
            dlg.show()
            end = time.monotonic() + timeout
            while time.monotonic() < end and not ready():
                pump(100)
            pump(500)
            neutral(dlg)
            pump(50)
            save(dlg.grab(), name)
            dlg.close()

        if wanted("pane-gnss"):
            tall_shot(w.gnss_pane, "pane-gnss", width=1100)  # wide: the table of the flights whole
        if wanted("dialog-ppk-station"):  # the permanent station nearby as the base
            dlg = PpkDialog(scene.mrk, w, ppk.work_folder(w.chunk), chunk=w.chunk)
            dlg._added = []  # the navigation files of the last run's base: none of this base
            dlg.station = scene.station
            dlg.base.set_paths([])
            dlg.from_station.setChecked(True)
            dlg._sources = None  # the list made again for the drone alone
            dlg._update()
            window_shot(dlg, "dialog-ppk-station", ready=lambda: not dlg._pending)
        if wanted("dialog-ppk-ppp"):  # the user's own base, placed by a static PPP
            dlg = PpkDialog(scene.mrk, w, ppk.work_folder(w.chunk), chunk=w.chunk)
            dlg.by_ppp.setChecked(True)
            dlg.base.set_paths([scene.base])
            dlg._update()
            window_shot(dlg, "dialog-ppk-ppp", ready=lambda: not dlg._pending)
        if wanted("dialog-stations"):
            dlg = StationsDialog(scene.lat, scene.lon, scene.when, parent=w)
            window_shot(dlg, "dialog-stations", ready=lambda: not dlg.loading)
        if wanted("dialog-survey-crs"):
            dialog_shot("dialog-survey-crs", w.gnss_survey_crs)
        if wanted("dialog-survey-crs-preview"):  # what the change would do, asked before it is done (and refused)
            w._save_sections = lambda: None  # the sections of the demo left as they are on disk
            taken.pop("last", None)
            w._set_survey_crs(heights.survey_crs(scene.lon, scene.lat, geoid=False))
            end = time.monotonic() + 300
            while "last" not in taken and time.monotonic() < end:
                pump(200)
            if "last" in taken:
                save(taken["last"], "dialog-survey-crs-preview")
            else:
                print("  dialog-survey-crs-preview: no window")
        if wanted("report-gnss"):
            from PySide6.QtCore import QSize

            from pyarchinit_photogrammetry.io.pdf_report import generate_pdf_report

            pdf = Path(tempfile.mkdtemp(prefix="ppg-docs-report-")) / "report.pdf"
            generate_pdf_report(w.doc, pdf, language=lang, chunks=[w.chunk])
            page = _pdf_page(pdf, ("GNSS", "PPK"), QSize)
            if page is not None:
                save(page, "report-gnss")
            else:
                print("  report-gnss: no page with the GNSS section")

    # ---- the start page (File > Pagina iniziale): its card whole, the panes put aside meanwhile
    if wanted("start-page"):
        from PySide6.QtWidgets import QWidget

        docks = [d for d in w.findChildren(QDockWidget) if d.isVisible()]
        for d in docks:
            d.hide()
        w.show_start_page()
        pump(800)
        card = w.start_page.findChild(QWidget, "card")
        r = QRect(card.mapTo(w.stack, QPoint(0, 0)), card.size()).adjusted(-24, -24, 24, 24)
        save(w.stack.grab(r.intersected(w.stack.rect())), "start-page")
        for d in docks:
            d.show()
    guard.stop()
    w.setWindowModified(False)
    w.close()
    return 0


def _pdf_page(path: Path, words: tuple[str, ...], QSize):
    """The first page of the PDF ``path`` that has all ``words``, drawn at about 130 dpi; None when none has them."""
    from PySide6.QtPdf import QPdfDocument

    doc = QPdfDocument()
    doc.load(str(path))
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainter

    for i in range(doc.pageCount()):
        if all(word in doc.getAllText(i).text() for word in words):
            size = doc.pagePointSize(i)
            page = doc.render(i, QSize(int(size.width() * 1.8), int(size.height() * 1.8)))
            paper = page.copy()
            paper.fill(Qt.GlobalColor.white)  # the page is drawn on nothing: on white, as on paper
            painter = QPainter(paper)
            painter.drawImage(0, 0, page)
            painter.end()
            return paper
    return None


def _icons(bars, folder: Path, lang: str) -> None:
    """The icon of every button of the two bars, 20 px at twice the resolution, named by the Italian
    text of the button (written once, in the Italian run); ``icons.json`` lists names and texts."""
    import re
    import unicodedata

    from PySide6.QtCore import QSize

    folder.mkdir(parents=True, exist_ok=True)
    listed = {}
    index = folder / "icons.json"
    if index.exists():
        listed = json.loads(index.read_text(encoding="utf-8"))
    for key in ("workflow_toolbar", "drawing_toolbar"):
        for n, a in enumerate(bars[key].actions()):
            if a.isSeparator() or a.icon().isNull():
                continue
            text = (a.toolTip() or a.text()).replace("&", "").split("\n")[0]
            entry = listed.setdefault(key, {}).setdefault(str(n), {})
            entry[lang] = text
            if lang == "it":
                slug = unicodedata.normalize("NFKD", a.text().replace("&", "")).encode("ascii", "ignore").decode()
                slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-") or f"{key}-{n}"
                entry["file"] = f"{slug}.png"
                a.icon().pixmap(QSize(40, 40)).save(str(folder / f"{slug}.png"), "PNG")
    index.write_text(json.dumps(listed, ensure_ascii=False, indent=1), encoding="utf-8")


# the menus of the guide: (picture, titles from the menu bar down to the submenu shown open)
_MENUS = [
    ("menu-file", ["File"]),
    ("menu-file-export", ["File", "Esporta"]),
    ("menu-file-import", ["File", "Importa"]),
    ("menu-edit", ["Modifica"]),
    ("menu-workflow", ["Flusso di lavoro"]),
    ("menu-workflow-cleaning", ["Flusso di lavoro", "Pulizia e confidenza"]),
    ("menu-workflow-region", ["Flusso di lavoro", "Regione di ricostruzione"]),
    ("menu-tools", ["Strumenti"]),
    ("menu-tools-target", ["Strumenti", "Target"]),
    ("menu-tools-diagnostics", ["Strumenti", "Diagnostica dell'allineamento"]),
    ("menu-tools-masks", ["Strumenti", "Maschere delle foto"]),
    ("menu-tools-drawing", ["Strumenti", "Disegno su ortofoto"]),
    ("menu-tools-patch", ["Strumenti", "Patch dell'ortofoto (assegna immagini)"]),
    ("menu-tools-classification", ["Strumenti", "Classificazione della nuvola"]),
    ("menu-tools-sam", ["Strumenti", "Segment Anything 2.1"]),
    ("menu-tools-multispectral", ["Strumenti", "Multispettrale"]),
    ("menu-tools-thermal", ["Strumenti", "Termico"]),
    ("menu-tools-satellite", ["Strumenti", "Immagini satellitari"]),
    ("menu-pyarchinit", ["PyArchInit"]),
    ("menu-qgis", ["QGIS"]),
    ("menu-view", ["Vista"]),
    ("menu-view-panes", ["Vista", "Pannelli"]),
    ("menu-help", ["Aiuto"]),
]


def _shrink(path: Path, flat: bool) -> None:
    """The PNG written again smaller: a menu, a bar or a dialog in 256 colours (they have few), a
    window with photos and rasters in 256 colours with dithering."""
    from PIL import Image

    im = Image.open(path).convert("RGB")
    dither = Image.Dither.NONE if flat else Image.Dither.FLOYDSTEINBERG
    im.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=dither).save(path, "PNG", optimize=True)


def _find(actions, title: str):
    """The action of ``title`` (the Italian text of the code) among ``actions``, in any language."""
    from pyarchinit_photogrammetry.gui.i18n import tr

    names = {title, tr(title), tr("&" + title)}
    for a in actions:
        text = a.text().replace("&", "")
        if text in {n.replace("&", "") for n in names}:
            return a
    raise LookupError(f"no menu {title!r} among {[a.text() for a in actions]}")


def _menu_picture(w, path, pump, QImage, QPainter, QColor, QPen, QPoint, QRect):
    """The menu bar with the menu of ``path[0]`` open under its title and, for a longer path,
    the submenus open to its right, as they cascade in the window."""
    mb = w.menuBar()
    top = _find(mb.actions(), path[0])
    mb.setActiveAction(top)  # the title highlighted and its menu open
    pump(250)
    menu = top.menu()
    bar = mb.grab()
    title = mb.actionGeometry(top)
    layers = []  # (image, x, y) in the picture
    x, y = title.x(), mb.height()
    current = menu
    for depth, name in enumerate(path[1:], 1):
        act = _find(current.actions(), name)
        current.setActiveAction(act)  # the item of the submenu highlighted
        pump(150)
        layers.append((current.grab().toImage(), x, y))
        item = current.actionGeometry(act)
        x, y = x + current.width() - 4, y + item.y() - 4
        sub = act.menu()
        current.hide()
        sub.popup(w.mapToGlobal(QPoint(0, 0)))
        pump(250)
        current = sub
    layers.append((current.grab().toImage(), x, y))
    current.hide()
    menu.hide()
    mb.setActiveAction(None)
    pump(100)
    pad = 16
    if len(path) > 1:  # a submenu on its own: the section of the guide names where it is
        layers = [(layers[-1][0], 0, 0)]
        bar = None
    width = max(title.right() + pad if bar is not None else 0, max(lx + img.width() for img, lx, _ in layers) + pad)
    height = max(ly + img.height() for img, _lx, ly in layers) + pad
    width = min(max(width, 480), mb.width()) if bar is not None else width
    pic = QImage(width, height, QImage.Format.Format_RGB32)
    pic.fill(QColor("#ffffff"))
    p = QPainter(pic)
    if bar is not None:
        p.drawPixmap(0, 0, bar, 0, 0, width, mb.height())
        p.setPen(QPen(QColor("#d0d4dc"), 1))
        p.drawLine(0, mb.height() - 1, width, mb.height() - 1)
    for img, lx, ly in layers:
        for k, alpha in ((6, 18), (4, 26), (2, 40)):  # a soft shadow under the menu
            p.fillRect(QRect(lx + k, ly + k, img.width(), img.height()), QColor(0, 0, 0, alpha))
        p.drawImage(lx, ly, img)
        p.setPen(QPen(QColor("#b8bec9"), 1))
        p.drawRect(QRect(lx, ly, img.width() - 1, img.height() - 1))
    p.end()
    return pic


def _areas(w, QPainter, QColor, QPen, QFont, QRect, QPoint, QDockWidget, QToolBar):
    """The window with the numbers of its areas (the table of *The window* in the guide)."""
    img = w.grab().toImage()
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    def at(widget, where: str = "right"):
        r = QRect(widget.mapTo(w, QPoint(0, 0)), widget.size())
        if where == "right":
            return QPoint(r.right() - 22, r.center().y())
        if where == "title":
            return QPoint(r.right() - 70, r.top() + 14)
        return r.center()

    def last_button(bar):
        widgets = [bar.widgetForAction(a) for a in bar.actions()]
        right = max((b.geometry().right() for b in widgets if b is not None and b.isVisible()),
                    default=bar.width() - 40)
        r = QRect(bar.mapTo(w, QPoint(0, 0)), bar.size())
        return QPoint(min(r.left() + right + 26, w.width() - 20), r.center().y())

    mb = w.menuBar()
    last = mb.actionGeometry(mb.actions()[-1])
    marks = [QPoint(mb.mapTo(w, QPoint(last.right() + 28, mb.height() // 2)))]
    bars = {b.objectName(): b for b in w.findChildren(QToolBar)}
    marks += [last_button(bars["workflow_toolbar"]), last_button(bars["drawing_toolbar"])]
    from PySide6.QtWidgets import QTabBar

    central = QRect(w.centralWidget().mapTo(w, QPoint(0, 0)), w.centralWidget().size())
    groups = {}  # the tab bars of the groups of panes, by side
    for bar in w.findChildren(QTabBar):
        if bar.metaObject().className() != "QMainWindowTabBar" or not bar.isVisible() or bar.count() < 2:
            continue
        r = QRect(bar.mapTo(w, QPoint(0, 0)), bar.size())
        side = "left" if r.right() <= central.left() else "right" if r.left() >= central.right() else "bottom"
        groups[side] = (bar, r)

    def after_tabs(bar, r):
        end = bar.tabRect(bar.count() - 1).right() + 24
        return QPoint(r.left() + min(end, r.width() - 16), r.center().y())

    tabs = w.tabs.tabBar()
    r = QRect(tabs.mapTo(w, QPoint(0, 0)), tabs.size())
    marks += [after_tabs(*groups["left"]) if "left" in groups else None,
              QPoint(r.left() + tabs.tabRect(tabs.count() - 1).right() + 26, r.center().y()),
              after_tabs(*groups["bottom"]) if "bottom" in groups else None,
              QPoint(groups["right"][1].right() - 64, groups["right"][1].bottom() + 16) if "right" in groups else None]
    sb = w.statusBar()
    marks += [QPoint(sb.mapTo(w, QPoint(0, 0)).x() + 300, sb.mapTo(w, QPoint(0, 0)).y() + sb.height() // 2)]
    font = QFont("Inter", 11)
    font.setBold(True)
    p.setFont(font)
    for n, c in enumerate(marks, 1):
        if c is None:
            continue
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.setBrush(QColor("#e65100"))
        p.drawEllipse(c, 13, 13)
        p.setPen(QColor("#ffffff"))
        p.drawText(QRect(c.x() - 13, c.y() - 13, 26, 26), 0x0084, str(n))  # AlignCenter
    p.end()
    return img


if __name__ == "__main__":
    sys.exit(main())
