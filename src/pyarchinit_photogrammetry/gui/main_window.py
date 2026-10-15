"""Main window: menus, workspace, views and docked panes, all on the Python API
(every action is a call that a script could make through ``ppg.app.document``)."""
from __future__ import annotations

import json
import logging
import os
import traceback
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QDockWidget, QFileDialog, QFrame,
                               QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QStackedWidget, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)

from .. import __version__
from ..app import app as application
from ..core.document import Document
from . import theme
from .dialogs import (QUALITY, ConnectDialog, ErrorDialog, Field, IfcPreviewDialog, ImageDialog, ParamDialog,
                      PublishDialog, SiteDialog, UnitDialog, UsFormDialog, message)
from .icons import app_icon, icon
from .image_views import US_COLORS, PhotoView, RasterView
from .model_view import ModelView
from .panels import (ConsolePane, LayersPane, LogPane, PhotosPane, ProxiesPane, PyArchInitPane, ReferencePane,
                     SectionsPane, WorkspaceTree)
from .start_page import StartPage
from .worker import StatusProgress, TaskRunner

log = logging.getLogger("pyarchinit_photogrammetry.gui")
APP_TITLE = "PyArchInit Photogrammetry"
DOCS_URL = "https://github.com/enzococca/pyarchinit-photogrammetry"
TARGET_TYPES = [("aruco_4x4_50", "ArUco 4x4 (50 codici)"), ("aruco_4x4_250", "ArUco 4x4 (250 codici)"),
                ("aruco_5x5_100", "ArUco 5x5 (100 codici)"), ("aruco_6x6_250", "ArUco 6x6 (250 codici)"),
                ("apriltag_36h11", "AprilTag 36h11")]


class MainWindow(QMainWindow):
    def __init__(self, document: Document | None = None, autoconnect: bool = True):
        super().__init__()
        self._autoconnect = autoconnect
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(app_icon())
        avail = self.screen().availableGeometry()  # a laptop screen is smaller than the default size
        self.resize(min(1500, avail.width() - 40), min(950, avail.height() - 60))
        from . import app_settings

        self.settings = app_settings()
        self.runner = TaskRunner(self)
        self.runner.status.connect(lambda s: self.statusBar().showMessage(s, 8000))
        self.runner.busy_changed.connect(self._busy)
        self.pa = None  # PyArchInit bridge
        self.units: list = []  # stratigraphic units drawn on the orthomosaic
        self.sections: list[dict] = []  # sections of the open project (sezioni/sezioni.json)
        self.line_layers: dict[str, list] = {}  # contour lines, plan slices: redrawn after raster reloads
        self.sam = None
        self.sam_points: list = []
        self.last_report: dict | None = None
        self._started = document is not None
        self._build_views()
        self._build_docks()
        self._build_actions()
        self._build_statusbar()
        self.set_document(document or application.document)
        QTimer.singleShot(0, self._autoconnect_pyarchinit)

    # ================================================================ layout
    def _build_views(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setIconSize(QSize(18, 18))
        self.model_view = ModelView()
        self.ortho_view = RasterView()
        self.photo_view = PhotoView()
        self.tabs.addTab(self.model_view, icon("view3d"), "Modello")
        self.tabs.addTab(self.ortho_view, icon("view_ortho"), "Ortofoto / DEM")
        self.tabs.addTab(self.photo_view, icon("view_photo"), "Foto")
        self.start_page = StartPage()
        self.start_page.new_requested.connect(self.new_document)
        self.start_page.open_requested.connect(lambda: self.open_document())
        self.start_page.demo_requested.connect(self.open_demo)
        self.start_page.recent_requested.connect(self.open_document)
        self.start_page.doctor_requested.connect(self.show_doctor)
        self.start_page.log_requested.connect(self.open_log_folder)
        self.start_page.help_requested.connect(self.open_docs)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.start_page)
        self.stack.addWidget(self.tabs)
        self.setCentralWidget(self.stack)
        self.ortho_view.mouse_world.connect(self._show_coords)
        self.ortho_view.polygon_finished.connect(self._unit_drawn)
        self.ortho_view.line_finished.connect(self._section_drawn)
        self.ortho_view.measured.connect(lambda t: (self.statusBar().showMessage(t, 30000),
                                                    self.log_pane.info(t)))
        self.ortho_view.point_clicked.connect(self._ortho_point)
        self.ortho_view.selection_changed.connect(self._selection_changed)
        self.ortho_view.delete_requested.connect(lambda: self.delete_selected())
        self.ortho_view.merge_requested.connect(lambda: self.merge_selected())
        self.photo_view.markers_changed.connect(self._markers_edited)
        self.photo_view.sam_prompt.connect(self._photo_sam_prompt)

    def _dock(self, title: str, widget: QWidget, area, name: str) -> QDockWidget:
        d = QDockWidget(title, self)
        d.setObjectName(name)
        # on a short screen the pane scrolls instead of making the window taller than the screen
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        d.setWidget(scroll)
        self.addDockWidget(area, d)
        return d

    def _build_docks(self) -> None:
        L, B, R = (Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.BottomDockWidgetArea,
                   Qt.DockWidgetArea.RightDockWidgetArea)
        self.workspace = WorkspaceTree()
        self.workspace.activated_item.connect(self._workspace_item)
        self.reference = ReferencePane()
        self.reference.import_requested.connect(self.import_reference)
        self.reference.update_requested.connect(self.update_transform)
        self.reference.optimize_requested.connect(self.optimize_cameras)
        self.reference.changed.connect(lambda: self._dirty(True))
        self.photos = PhotosPane()
        self.photos.open_camera.connect(self.open_photo)
        self.log_pane = LogPane()
        self.console = ConsolePane(self._namespace)
        self.console.executed.connect(self._after_console)
        self.pa_pane = PyArchInitPane()
        self.pa_pane.connect_requested.connect(lambda: self.pyarchinit_connect(ask=True))
        self.pa_pane.demo_requested.connect(self.pyarchinit_demo)
        self.pa_pane.new_site_requested.connect(self.pyarchinit_new_site)
        self.pa_pane.us_requested.connect(lambda sito, area, us, tipo: self.open_us_form(sito, area, us, tipo))
        # tab widgets inside docks (more robust than tabified docks across Qt platforms)
        self.left_tabs = QTabWidget()
        self.left_tabs.setIconSize(QSize(16, 16))
        self.left_tabs.addTab(self.workspace, icon("workspace"), "Area di lavoro")
        self.left_tabs.addTab(self.reference, icon("reference_pane"), "Riferimenti")
        self.layers_pane = LayersPane()
        self.layers_pane.set_view(self.ortho_view)
        self.layers_pane.attribute_edited.connect(self._attribute_edited)
        self.pa_pending: dict[int, object] = {}  # gid -> UnitPolygon edited, to write back to PyArchInit
        self.pa_deleted: set[int] = set()  # gid of polygons deleted or merged away here
        self.pa_geom_changed: set[int] = set()  # gid whose geometry changed (merge, part removed)
        self.b_pa_save = QPushButton(icon("publish"), "Salva modifiche in PyArchInit")
        self.b_pa_save.clicked.connect(self.save_pending_units)
        self.b_pa_save.hide()
        self.layers_pane.layout().addWidget(self.b_pa_save)
        self.left_tabs.addTab(self.layers_pane, icon("slice"), "Layer")
        self.layers_pane.us_form_requested.connect(self._us_form_from_feature)
        self.proxies_pane = ProxiesPane()
        self.left_tabs.addTab(self.proxies_pane, icon("us"), "Proxy US")
        self.proxies_pane.create_requested.connect(self.create_proxies)
        self.proxies_pane.show_requested.connect(lambda: self._show_model("proxies"))
        self.proxies_pane.export_requested.connect(self.export_proxies)
        self.proxies_pane.publish_requested.connect(self.publish_proxies)
        self.proxies_pane.quote_requested.connect(self.quote_proxies)
        self.proxies_pane.us_requested.connect(self._us_form_from_proxy)
        self.proxies_pane.delete_requested.connect(self.delete_proxies)
        self.sections_pane = SectionsPane()
        self.sections_pane.zoom_requested.connect(self._zoom_section)
        self.sections_pane.publish_requested.connect(self._publish_section)
        self.sections_pane.delete_requested.connect(self._delete_section)
        self.sections_pane.folder_requested.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._sections_dir()))))
        self.ortho_view.feature_clicked.connect(self._feature_clicked)
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.setIconSize(QSize(16, 16))
        self.bottom_tabs.addTab(self.photos, icon("photos"), "Foto")
        self.bottom_tabs.addTab(self.sections_pane, icon("line"), "Sezioni")
        self.bottom_tabs.addTab(self.console, icon("console"), "Console Python")
        self.bottom_tabs.addTab(self.log_pane, icon("log"), "Log")
        self.d_work = self._dock("Progetto", self.left_tabs, L, "workspace")
        self.d_pa = self._dock("PyArchInit", self.pa_pane, R, "pyarchinit")
        self.d_photos = self._dock("Foto, console e log", self.bottom_tabs, B, "bottom")
        self.resizeDocks([self.d_work, self.d_pa], [360, 300], Qt.Orientation.Horizontal)
        self.resizeDocks([self.d_photos], [230], Qt.Orientation.Vertical)

    def _build_statusbar(self) -> None:
        self.progress_widget = StatusProgress()
        self.progress_widget.cancel_requested.connect(self.runner.cancel)
        self.runner.status_widget = self.progress_widget
        self.coord_label = QLabel("")
        self.statusBar().addPermanentWidget(self.progress_widget)
        self.statusBar().addPermanentWidget(self.coord_label)
        self._qgis_timer = QTimer(self)
        self._qgis_timer.setSingleShot(True)
        self._qgis_timer.timeout.connect(self._export_qgis_layers)
        self.ortho_view.layers_changed.connect(self._schedule_qgis)

    def _act(self, menu, text: str, slot, shortcut=None, tip: str = "", processing: bool = False,
             icon_name: str | None = None) -> QAction:
        a = QAction(text, self)
        if icon_name:
            a.setIcon(icon(icon_name))
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setStatusTip(tip)
        a.triggered.connect(lambda _=False: slot())
        menu.addAction(a)
        if processing:
            self._processing_actions.append(a)
        return a

    def _build_actions(self) -> None:
        self._processing_actions: list[QAction] = []
        mb = self.menuBar()
        # ------------------------------------------------------------ File
        m = mb.addMenu("&File")
        self._act(m, "Nuovo progetto", self.new_document, QKeySequence.StandardKey.New, icon_name="new")
        self._act(m, "Apri...", self.open_document, QKeySequence.StandardKey.Open, icon_name="open")
        self.recent_menu = m.addMenu(icon("recent"), "Progetti recenti")
        self._act(m, "Salva", self.save_document, QKeySequence.StandardKey.Save, icon_name="save")
        self._act(m, "Salva con nome...", lambda: self.save_document(ask=True), QKeySequence.StandardKey.SaveAs,
                  icon_name="save_as")
        m.addSeparator()
        exp = m.addMenu(icon("export"), "Esporta")
        self._act(exp, "Nuvola di punti (LAS/PLY)...", lambda: self.export("cloud"), icon_name="point_cloud")
        self._act(exp, "Modello (OBJ/PLY)...", lambda: self.export("model"), icon_name="model")
        self._act(exp, "DEM (GeoTIFF)...", lambda: self.export("dem"), icon_name="dem")
        self._act(exp, "Ortofoto (GeoTIFF)...", lambda: self.export("ortho"), icon_name="ortho")
        self._act(exp, "Camere (CSV / COLMAP)...", lambda: self.export("cameras"), icon_name="cameras")
        self._act(exp, "Riferimenti (CSV)...", lambda: self.export("reference"), icon_name="reference")
        imp = m.addMenu(icon("open"), "Importa")
        self._act(imp, "Progetto Metashape (.psx)...", lambda: self.import_external("metashape"), processing=True,
                  icon_name="open")
        self._act(imp, "Progetto WebODM / ODM (cartella o all.zip)...", lambda: self.import_external("webodm"),
                  processing=True, icon_name="open")
        self._act(imp, "Sessione Photo3D...", lambda: self.import_external("photo3d"), processing=True,
                  icon_name="photos")
        self._act(m, "Genera report HTML...", self.generate_report, processing=True, icon_name="report")
        m.addSeparator()
        self._act(m, "Crea e apri progetto demo...", self.open_demo, processing=True, icon_name="demo")
        self._act(m, "Pagina iniziale", self.show_start_page, icon_name="home_view")
        m.addSeparator()
        self._act(m, "Esci", self.close, QKeySequence.StandardKey.Quit, icon_name="quit")
        # ------------------------------------------------------------ Workflow
        m = mb.addMenu("Flusso di &lavoro")
        self._act(m, "Aggiungi foto...", self.add_photos, processing=True, icon_name="photos")
        self._act(m, "Aggiungi cartella...", lambda: self.add_photos(folder=True), processing=True,
                  icon_name="folder_photos")
        self._act(m, "Aggiungi chunk", self.add_chunk, icon_name="chunk_add")
        m.addSeparator()
        self._act(m, "Rileva marker codificati...", self.detect_markers, processing=True, icon_name="markers")
        self._act(m, "Allinea foto...", self.align_photos, processing=True, icon_name="align")
        self._act(m, "Importa riferimenti...", self.import_reference, icon_name="reference")
        self._act(m, "Aggiorna georeferenziazione", self.update_transform, processing=True, icon_name="georef")
        self._act(m, "Ottimizza camere", self.optimize_cameras, processing=True, icon_name="optimize")
        m.addSeparator()
        self._act(m, "Costruisci nuvola densa...", self.build_dense, processing=True, icon_name="dense")
        self._act(m, "Ritaglia nuvola densa...", self.crop_cloud, processing=True, icon_name="eraser")
        self._act(m, "Costruisci modello...", self.build_model, processing=True, icon_name="model")
        self._act(m, "Costruisci texture...", self.build_texture, processing=True, icon_name="texture")
        self._act(m, "Costruisci DEM...", self.build_dem, processing=True, icon_name="dem")
        self._act(m, "Costruisci ortofoto...", self.build_ortho, processing=True, icon_name="ortho")
        m.addSeparator()
        self._act(m, "Elabora tutto...", self.process_all, "Ctrl+R", processing=True, icon_name="process_all")
        # ------------------------------------------------------------ Tools
        m = mb.addMenu("&Strumenti")
        tm = m.addMenu(icon("print"), "Target")
        self._act(tm, "Stampa fogli di target...", self.print_targets, icon_name="print")
        self._act(tm, "Tappetino per turntable...", self.print_turntable, icon_name="turntable")
        dm = m.addMenu(icon("polygon"), "Disegno su ortofoto")
        self.draw_group = QActionGroup(self)
        self.draw_actions: dict[str, QAction] = {}
        for mode, label, ic in (("pan", "Sposta / interroga", "pan"), ("select", "Seleziona", "select"),
                                ("polygon", "Disegna unita' stratigrafica", "polygon"),
                                ("line", "Traccia sezione", "line"), ("measure", "Misura distanza", "measure"),
                                ("point", "Quota puntuale / prompt SAM", "point")):
            a = QAction(icon(ic), label, self, checkable=True)
            a.setChecked(mode == "pan")
            a.setStatusTip(label)
            a.triggered.connect(lambda _=False, md=mode: self.set_draw_mode(md))
            self.draw_group.addAction(a)
            dm.addAction(a)
            self.draw_actions[mode] = a
        # drawing options: freehand units, snapping to vertices / segments of the visible layers
        self.a_freehand = QAction(icon("freehand"), "Mano libera", self, checkable=True)
        self.a_freehand.setToolTip("Disegno a mano libera delle unita': tieni premuto, traccia e rilascia per chiudere")
        self.a_freehand.setChecked(self.settings.value("draw/freehand", "false") in ("true", True))
        self.a_freehand.toggled.connect(lambda _on: self._apply_draw_options())
        dm.addAction(self.a_freehand)
        self.snap_menu = QMenu("Snap", self)
        self.a_snap = QAction(icon("snap"), "Snap", self, checkable=True)
        self.a_snap.setToolTip("Aggancia i punti ai vertici e/o ai segmenti dei layer visibili (scegli dal menu)")
        self.a_snap.setChecked(self.settings.value("draw/snap", "false") in ("true", True))
        self.a_snap.setMenu(self.snap_menu)
        self.a_snap_vertex = self.snap_menu.addAction("Vertici")
        self.a_snap_segment = self.snap_menu.addAction("Segmenti")
        for a, key in ((self.a_snap_vertex, "draw/snap_vertex"), (self.a_snap_segment, "draw/snap_segment")):
            a.setCheckable(True)
            a.setChecked(self.settings.value(key, "true") in ("true", True))
        self.snap_menu.addSeparator()
        tol_group = QActionGroup(self)
        self.snap_tol_actions = {}
        for px in (6, 12, 20):
            a = self.snap_menu.addAction(f"Tolleranza {px} px")
            a.setCheckable(True)
            a.setChecked(int(self.settings.value("draw/snap_px", 12)) == px)
            tol_group.addAction(a)
            self.snap_tol_actions[px] = a
        for a in [self.a_snap, self.a_snap_vertex, self.a_snap_segment, *self.snap_tol_actions.values()]:
            a.toggled.connect(lambda _on: self._apply_draw_options())
        dm.addAction(self.a_snap)
        dm.addSeparator()
        self.a_sel_delete = self._act(dm, "Elimina selezionati", lambda: self.delete_selected(), icon_name="delete")
        self.a_sel_delete.setToolTip("Elimina gli elementi selezionati (tasto Canc nella modalita' Seleziona)")
        self.a_sel_merge = self._act(dm, "Unisci selezionati", lambda: self.merge_selected(), icon_name="merge")
        self.a_sel_merge.setToolTip("Unisce le unita' selezionate in un solo poligono, o linee contigue dello stesso layer")
        for a in (self.a_sel_delete, self.a_sel_merge):
            a.setEnabled(False)
        self.a_clear_drawings = self._act(dm, "Cancella disegni", self.clear_drawings, icon_name="eraser")
        m.addSeparator()
        self.a_contours = self._act(m, "Curve di livello...", self.contours, processing=True, icon_name="contours")
        self.a_volume = self._act(m, "Volume scavo / riporto...", self.compute_volume, processing=True, icon_name="volume")
        self.a_slice = self._act(m, "Pianta a quota (slice orizzontale)...", self.plan_slice, processing=True,
                                 icon_name="slice")
        self._act(m, "Profilo ceramico da mesh...", self.pottery_profile, processing=True, icon_name="pottery")
        self._act(m, "Proxy 3D delle US...", self.create_proxies, processing=True, icon_name="us")
        m.addSeparator()
        self._act(m, "Classifica nuvola di punti", self.classify, processing=True, icon_name="classify")
        sm = m.addMenu(icon("sam"), "Segment Anything 2.1")
        self._act(sm, "Unita' automatiche sull'ortofoto", self.sam_auto_units, processing=True, icon_name="sam")
        self._act(sm, "Segmenta dai punti cliccati sull'ortofoto", self.sam_from_points, processing=True,
                  icon_name="point")
        self._act(sm, "Prompt sulle foto (attiva/disattiva)", self.toggle_photo_sam, icon_name="view_photo")
        sm.addSeparator()
        self._act(sm, "Installa / aggiorna SAM 2.1...", lambda: self.install_sam(), processing=True, icon_name="export")
        self._act(sm, "Rimuovi SAM 2.1", self.remove_sam, icon_name="clear")
        m.addSeparator()
        self._act(m, "Esporta IFC (BIM)...", self.export_ifc, processing=True, icon_name="ifc")
        self._act(m, "Esegui script Python...", self.run_script, icon_name="script")
        # ------------------------------------------------------------ PyArchInit
        m = mb.addMenu("&PyArchInit")
        self._act(m, "Connetti (config.cfg, SQLite o PostgreSQL)...", lambda: self.pyarchinit_connect(ask=True),
                  icon_name="database")
        self._act(m, "Usa il database demo", self.pyarchinit_demo, icon_name="demo_db")
        self._act(m, "Nuovo sito...", self.pyarchinit_new_site, icon_name="site")
        self._act(m, "Nuova scheda US...", lambda: self.open_us_form(self.pa_pane.current_site() or "", "1", "", "US"),
                  icon_name="us")
        self._act(m, "Aggiorna struttura del database...", self.pyarchinit_upgrade, icon_name="database")
        self.a_autoconnect = QAction("Connetti all'avvio al database di PyArchInit", self, checkable=True)
        self.a_autoconnect.setChecked(self.settings.value("pa_autoconnect", "true") in ("true", True, 1, "1"))
        self.a_autoconnect.toggled.connect(lambda on: self.settings.setValue("pa_autoconnect", "true" if on else "false"))
        m.addAction(self.a_autoconnect)
        m.addSeparator()
        self._act(m, "Pubblica ortofoto / DEM / modello...", self.pyarchinit_publish, processing=True,
                  icon_name="publish")
        self._act(m, "Pubblica unita' disegnate (pyunitastratigrafiche)", self.pyarchinit_publish_units,
                  processing=True, icon_name="publish")
        self._act(m, "Carica poligoni US sull'ortofoto", self.pyarchinit_load_units, icon_name="load")
        self._act(m, "Pubblica quote dei punti cliccati", self.pyarchinit_publish_points, processing=True,
                  icon_name="publish")
        self._act(m, "Aggiorna pannello", lambda: self.pa_pane.refresh(), icon_name="refresh")
        # ------------------------------------------------------------ QGIS
        m = mb.addMenu("&QGIS")
        self.a_qgis_open = self._act(m, "Apri in QGIS (collegamento dal vivo)", self.open_in_qgis, icon_name="view_ortho")
        self.a_qgis_live = QAction("Aggiorna QGIS mentre disegno", self, checkable=True)
        self.a_qgis_live.setChecked(False)
        self.a_qgis_live.toggled.connect(lambda on: self._schedule_qgis() if on else None)
        m.addAction(self.a_qgis_live)
        self._act(m, "Apri la cartella dei layer per QGIS", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self._qgis_dir()))), icon_name="folder")
        # ------------------------------------------------------------ View
        m = mb.addMenu("&Vista")
        for d in (self.d_work, self.d_photos, self.d_pa):
            m.addAction(d.toggleViewAction())
        m.addSeparator()
        self._act(m, "Area di lavoro", lambda: self._show_pane(self.left_tabs, self.workspace), icon_name="workspace")
        self._act(m, "Riferimenti", lambda: self._show_pane(self.left_tabs, self.reference), icon_name="reference_pane")
        self._act(m, "Layer", lambda: self._show_pane(self.left_tabs, self.layers_pane), icon_name="slice")
        self._act(m, "Foto (miniature)", lambda: self._show_pane(self.bottom_tabs, self.photos), icon_name="photos")
        self._act(m, "Sezioni", lambda: self._show_pane(self.bottom_tabs, self.sections_pane), icon_name="line")
        self._act(m, "Console Python", lambda: self._show_pane(self.bottom_tabs, self.console), "Ctrl+Shift+P",
                  icon_name="console")
        self._act(m, "Log", lambda: self._show_pane(self.bottom_tabs, self.log_pane), icon_name="log")
        m.addSeparator()
        self._act(m, "Modello", lambda: self.show_view(self.model_view), "F2", icon_name="view3d")
        self._act(m, "Ortofoto / DEM", lambda: self.show_view(self.ortho_view), "F3", icon_name="view_ortho")
        self._act(m, "Foto", lambda: self.show_view(self.photo_view), "F4", icon_name="view_photo")
        m.addSeparator()
        tmenu = m.addMenu(icon("theme"), "Tema")
        tgroup = QActionGroup(self)
        self.theme_actions = {}
        for mode, label in (("light", "Chiaro"), ("dark", "Scuro")):
            a = QAction(label, self, checkable=True)
            a.setChecked(theme.current() == mode)
            a.triggered.connect(lambda _=False, md=mode: self.set_theme(md))
            tgroup.addAction(a)
            tmenu.addAction(a)
            self.theme_actions[mode] = a
        # ------------------------------------------------------------ Help
        m = mb.addMenu("&Aiuto")
        self._act(m, "Documentazione online", self.open_docs, icon_name="help")
        self._act(m, "Diagnostica ambiente...", self.show_doctor, icon_name="doctor")
        self._act(m, "Apri cartella dei log", self.open_log_folder, icon_name="folder")
        m.addSeparator()
        self._act(m, "Informazioni", self.about, icon_name="about")
        # ------------------------------------------------------------ toolbar
        tb = QToolBar("Flusso di lavoro")
        tb.setObjectName("workflow_toolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(26, 26))
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.addToolBar(tb)
        self._toolbar_styles = {tb: Qt.ToolButtonStyle.ToolButtonTextUnderIcon}
        self._toolbar_sizes: dict = {}  # toolbar -> (width besides the buttons, [(with text, icon only)])
        groups = [
            [("open", "Apri", lambda: self.open_document(), "Apri un progetto", False),
             ("save", "Salva", self.save_document, "Salva il progetto", False)],
            [("photos", "Foto", self.add_photos, "Aggiungi foto al chunk", True),
             ("markers", "Marker", self.detect_markers, "Rileva i target codificati", True),
             ("align", "Allinea", self.align_photos, "Allinea le foto (SfM)", True),
             ("reference", "Riferimenti", self.import_reference, "Importa le coordinate dei marker", True)],
            [("dense", "Nuvola densa", self.build_dense, "Depth map e nuvola densa", True),
             ("model", "Modello", self.build_model, "Mesh 3D", True),
             ("texture", "Texture", self.build_texture, "Texture del modello", True),
             ("dem", "DEM", self.build_dem, "Modello digitale di elevazione", True),
             ("ortho", "Ortofoto", self.build_ortho, "Ortofoto (true ortho)", True)],
            [("process_all", "Elabora tutto", self.process_all, "Tutti i passi in sequenza (Ctrl+R)", True)],
            [("publish", "PyArchInit", self.pyarchinit_publish, "Pubblica in PyArchInit", True)],
        ]
        for gi, group in enumerate(groups):
            if gi:
                tb.addSeparator()
            for name, text, slot, tip, processing in group:
                a = tb.addAction(icon(name), text)
                a.setStatusTip(tip)
                a.setToolTip(tip)
                a.triggered.connect(lambda _=False, s=slot: s())
                if processing:
                    self._processing_actions.append(a)
        tb.addSeparator()  # analyses of the DEM and of the model, next to the products they use
        for a in (self.a_contours, self.a_volume, self.a_slice):
            tb.addAction(a)
        # drawing and layer tools, always visible under the workflow
        self.addToolBarBreak()
        db = QToolBar("Disegno e layer")
        db.setObjectName("drawing_toolbar")
        db.setMovable(False)
        db.setIconSize(QSize(20, 20))
        db.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(db)
        self._toolbar_styles[db] = Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        lab = QLabel(" Disegno: ")
        lab.setObjectName("pageSubtitle")
        db.addWidget(lab)
        # short texts in the bar (menus keep the full names, tooltips show them)
        for a, short in ((self.draw_actions["pan"], "Sposta"), (self.draw_actions["polygon"], "Disegna US"),
                         (self.draw_actions["line"], "Sezione"), (self.draw_actions["measure"], "Misura"),
                         (self.draw_actions["point"], "Quota / SAM"), (self.a_sel_delete, "Elimina"),
                         (self.a_sel_merge, "Unisci"), (self.a_clear_drawings, "Cancella tutto"),
                         (self.a_contours, "Curve"), (self.a_volume, "Volume"), (self.a_slice, "Pianta a quota"),
                         (self.a_qgis_open, "QGIS")):
            if a.toolTip() == a.text().replace("...", "").replace("&", ""):
                a.setToolTip(a.text().replace("...", ""))
            a.setIconText(short)
        for mode in ("pan", "select", "polygon"):
            db.addAction(self.draw_actions[mode])
        db.addAction(self.a_freehand)
        db.addAction(self.a_snap)
        snap_button = db.widgetForAction(self.a_snap)
        if snap_button is not None:
            snap_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        for mode in ("line", "measure", "point"):
            db.addAction(self.draw_actions[mode])
        db.addAction(self.a_sel_delete)
        db.addAction(self.a_sel_merge)
        db.addAction(self.a_clear_drawings)
        db.addSeparator()
        a = db.addAction(icon("slice"), "Layer")
        a.setToolTip("Pannello dei layer: mostra, nascondi, zoom, attributi")
        a.triggered.connect(lambda _=False: self._show_pane(self.left_tabs, self.layers_pane))
        a = db.addAction(icon("line"), "Sezioni")
        a.setToolTip("Pannello delle sezioni")
        a.triggered.connect(lambda _=False: self._show_pane(self.bottom_tabs, self.sections_pane))
        db.addAction(self.a_qgis_open)
        a = db.addAction(icon("publish"), "Pubblica unita'")
        a.setToolTip("Scrive le unita' disegnate in pyunitastratigrafiche")
        a.triggered.connect(lambda _=False: self.pyarchinit_publish_units())
        self._processing_actions.append(a)
        self._refresh_recent()
        self._apply_draw_options()

    def _show_pane(self, tabs: QTabWidget, widget: QWidget) -> None:
        dock = self.d_work if tabs is self.left_tabs else self.d_photos
        dock.show()
        tabs.setCurrentWidget(widget)

    def show_view(self, widget: QWidget) -> None:
        self.stack.setCurrentWidget(self.tabs)
        self.tabs.setCurrentWidget(widget)

    def show_start_page(self) -> None:
        self.stack.setCurrentWidget(self.start_page)

    def _update_stack(self) -> None:
        has_data = self.doc.path is not None or any(ch.cameras for ch in self.doc.chunks)
        self.stack.setCurrentWidget(self.tabs if (self._started or has_data) else self.start_page)

    # ================================================================ document
    @property
    def doc(self) -> Document:
        return application.document

    @property
    def chunk(self):
        return self.doc.chunk

    def _namespace(self) -> dict:
        import pyarchinit_photogrammetry as ppg

        return {"doc": self.doc, "chunk": self.chunk, "app": application, "ppg": ppg, "np": np, "window": self}

    def set_document(self, doc: Document) -> None:
        application.document = doc
        if not doc.chunks:
            doc.add_chunk("Chunk 1")
        self.units = []
        self.sam_points = []
        self.sections = []
        self.line_layers = {}
        self.refresh_all(reset_views=True)
        self._load_sections()
        self.proxies_pane.set_proxies(self.chunk.meta.get("proxies", []) if self.chunk else [])
        if doc.path:
            self._add_recent(doc.path)

    def refresh_all(self, reset_views: bool = False) -> None:
        name = self.doc.path.name if self.doc.path else "senza nome"
        self.setWindowTitle(f"{APP_TITLE} {__version__} - {name}[*]")
        self.workspace.populate(self.doc)
        self.reference.set_chunk(self.chunk, self.last_report)
        self.photos.set_chunk(self.chunk)
        self.model_view.set_chunk(self.chunk) if reset_views else self.model_view.set_chunk(self.chunk,
                                                                                             self.model_view.kind)
        self._load_raster()
        self._update_stack()

    def _load_raster(self, kind: str | None = None) -> None:
        ch = self.chunk
        kind = kind or ("ortho" if ch is not None and ch.orthomosaic_path else "dem")
        path = (ch.orthomosaic_path if kind == "ortho" else ch.dem_path) if ch is not None else None
        if path is None or not Path(path).exists():
            self.ortho_view.scene().clear()
            self.ortho_view.tr = None
            self.ortho_view.scene().addText("Costruisci il DEM o l'ortofoto per vederli qui")
            return
        self.ortho_view.load(path, kind)
        if kind == "ortho" and ch.dem_path:
            self.ortho_view.set_dem(ch.dem_path)
        for i, u in enumerate(self.units):
            self._draw_unit(u, i)
        self._draw_sections()
        for name, lines in self.line_layers.items():
            for coords, color, label, attrs, width in lines:
                self.ortho_view.add_line(coords, color, label, layer=name, attrs=attrs, width=width)

    def _dirty(self, flag: bool = True) -> None:
        self.setWindowModified(flag)

    def _busy(self, busy: bool) -> None:
        for a in self._processing_actions:
            a.setEnabled(not busy)
        self.reference.setEnabled(not busy)

    def _need_chunk(self, what: str = "", aligned: bool = False, product: str | None = None) -> bool:
        ch = self.chunk
        if ch is None:
            self._warn("Nessun chunk attivo.")
            return False
        if aligned and not ch.aligned_cameras:
            self._warn("Allinea prima le foto (Flusso di lavoro > Allinea foto).")
            return False
        names = {"point_cloud": "la nuvola densa", "model": "il modello", "dem": "il DEM",
                 "orthomosaic": "l'ortofoto", "depth_maps": "le depth map"}
        if product and not ch.product_path(product):
            self._warn(f"Costruisci prima {names.get(product, product)}.")
            return False
        return True

    # ---------------------------------------------------------------- messages
    def _warn(self, text: str) -> None:
        self.log_pane.warning(text)
        message(self, "warning", APP_TITLE, text)

    def _info(self, title: str, text: str) -> None:
        self.log_pane.info(text)
        message(self, "info", title, text)

    def _error(self, summary: str, details: str = "", logged: bool = True) -> None:
        """Report a failure: error dialog with hint and traceback; the log pane shows the
        row written by the logger (the task runner and :meth:`_exc` log before calling)."""
        first = summary.strip().splitlines()[0] if summary.strip() else "Errore"
        if not logged:
            log.error("%s\n%s", first, details.rstrip())
        if details:
            self._show_pane(self.bottom_tabs, self.log_pane)
        ErrorDialog("Errore", first, details, parent=self).exec()

    def _exc(self, prefix: str, exc: BaseException) -> None:
        self._error(f"{prefix}: {exc}", traceback.format_exc(), logged=False)

    def run_task(self, label: str, fn, on_done=None, refresh: bool = True, save: bool = True) -> bool:
        def done(result):
            if save and self.doc.path:
                try:
                    self.doc.save()
                except Exception as exc:
                    log.error("Salvataggio non riuscito: %s\n%s", exc, traceback.format_exc())
            elif not self.doc.path:
                self._dirty(True)
            if refresh:
                self.refresh_all()
            if on_done is not None:
                on_done(result)

        return self.runner.run(label, fn, done, self._error)

    # ---------------------------------------------------------------- file
    def new_document(self) -> None:
        if not self._confirm_discard():
            return
        self._started = True
        self.set_document(Document())
        self.statusBar().showMessage("Nuovo progetto: aggiungi le foto (Flusso di lavoro > Aggiungi foto)", 10000)

    def open_document(self, path: str | None = None) -> None:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Apri progetto", self._last_dir(), "Progetti (*.ppg)")
            if not path:
                return
        try:
            doc = Document.open(path)
        except Exception as exc:
            self._exc(f"Impossibile aprire {path}", exc)
            return
        self.last_report = None
        self._started = True
        self.set_document(doc)
        self.statusBar().showMessage(f"Aperto {path}", 5000)

    def save_document(self, ask: bool = False) -> bool:
        if ask or self.doc.path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Salva progetto", self._last_dir(), "Progetti (*.ppg)")
            if not path:
                return False
            self.doc.save(path)
            self._add_recent(self.doc.path)
        else:
            self.doc.save()
        self._dirty(False)
        self.refresh_all()
        self.statusBar().showMessage(f"Salvato {self.doc.path}", 5000)
        return True

    def _confirm_discard(self) -> bool:
        if not self.isWindowModified():
            return True
        r = message(self, "question", "Modifiche non salvate", "Salvare il progetto prima di continuare?",
                    buttons=QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                    | QMessageBox.StandardButton.Cancel, default=QMessageBox.StandardButton.Save)
        if r == QMessageBox.StandardButton.Save:
            return self.save_document()
        return r == QMessageBox.StandardButton.Discard

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._fit_toolbars()

    def _fit_toolbars(self) -> None:
        """A toolbar that would not fit the window with its texts (laptop screens) loses them
        instead of hiding its last buttons behind the >> menu: the workflow bar all at once,
        the drawing bar from its last button, so that the drawing tools keep their names as
        long as possible. The tooltips keep the names."""
        icon_only = Qt.ToolButtonStyle.ToolButtonIconOnly
        for tb, style in getattr(self, "_toolbar_styles", {}).items():
            buttons = [b for b in (tb.widgetForAction(a) for a in tb.actions()) if isinstance(b, QToolButton)]
            sizes = self._toolbar_sizes.get(tb)
            if sizes is None or len(sizes[1]) != len(buttons):
                sizes = self._toolbar_sizes[tb] = self._measure_toolbar(tb, style, buttons)
            other, widths = sizes
            need, avail = other + sum(full for full, _ in widths), self.width() - 16
            short = [False] * len(buttons)
            for i in reversed(range(len(buttons))):
                if need <= avail:
                    break
                short[i] = True
                need -= widths[i][0] - widths[i][1]
            if any(short) and style == Qt.ToolButtonStyle.ToolButtonTextUnderIcon:
                short = [True] * len(buttons)  # mixed heights would look broken
            for b, sh in zip(buttons, short):
                if b.toolButtonStyle() != (icon_only if sh else style):
                    b.setToolButtonStyle(icon_only if sh else style)

    @staticmethod
    def _measure_toolbar(tb: QToolBar, style, buttons: list) -> tuple[int, list]:
        widths = []
        for b in buttons:
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            small = b.sizeHint().width()
            b.setToolButtonStyle(style)
            widths.append((b.sizeHint().width(), small))
        tb.layout().invalidate()
        return tb.sizeHint().width() - sum(full for full, _ in widths), widths

    def closeEvent(self, ev) -> None:
        if self.runner.busy:
            self._warn("Attendi la fine (o annulla) l'elaborazione in corso.")
            ev.ignore()
            return
        if not self._confirm_discard():
            ev.ignore()
            return
        self.log_pane.detach()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("state", self.saveState())
        super().closeEvent(ev)

    def _last_dir(self) -> str:
        if self.doc.path:
            return str(self.doc.path.parent)
        return str(self.settings.value("last_dir", str(Path.home())))

    def _add_recent(self, path: Path) -> None:
        rec = [p for p in (self.settings.value("recent", []) or []) if p != str(path)]
        rec.insert(0, str(path))
        self.settings.setValue("recent", rec[:10])
        self.settings.setValue("last_dir", str(Path(path).parent))
        self._refresh_recent()

    def _refresh_recent(self) -> None:
        self.recent_menu.clear()
        rec = [p for p in (self.settings.value("recent", []) or []) if Path(p).exists()]
        for p in rec:
            a = self.recent_menu.addAction(icon("project"), p)
            a.triggered.connect(lambda _=False, pp=p: self.open_document(pp))
        self.start_page.set_recent(rec)

    def export(self, what: str) -> None:
        ch = self.chunk
        if ch is None:
            return
        filters = {"cloud": "LAS (*.las);;PLY (*.ply)", "model": "OBJ con texture (*.obj);;PLY (*.ply)",
                   "dem": "GeoTIFF (*.tif)", "ortho": "GeoTIFF (*.tif)", "cameras": "CSV (*.csv);;COLMAP (*.txt)",
                   "reference": "CSV (*.csv)"}
        path, flt = QFileDialog.getSaveFileName(self, "Esporta", self._last_dir(), filters[what])
        if not path:
            return
        try:
            if what == "cloud":
                ch.export_point_cloud(path)
            elif what == "model":
                ch.export_model(path)
            elif what == "dem":
                ch.export_dem(path)
            elif what == "ortho":
                ch.export_orthomosaic(path)
            elif what == "cameras":
                ch.export_cameras(path, fmt="csv" if path.lower().endswith(".csv") else "colmap")
            elif what == "reference":
                ch.export_reference(path)
        except Exception as exc:
            self._exc("Esportazione non riuscita", exc)
            return
        self.log_pane.info(f"Esportato {path}")
        self.statusBar().showMessage(f"Esportato {path}", 8000)

    def generate_report(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        path, _ = QFileDialog.getSaveFileName(self, "Report", self._last_dir() + "/report.html", "HTML (*.html)")
        if path:
            self.run_task("Report", lambda p: self.chunk.generate_report(path),
                          lambda r: QDesktopServices.openUrl(QUrl.fromLocalFile(str(r))), refresh=False, save=False)

    def open_demo(self) -> None:
        default = str(Path.home() / "ppg_demo")
        v = ParamDialog.ask(self, "Progetto demo", [
            Field("folder", "Cartella", default, kind="dir"),
            Field("which", "Apri alla fine", "scavo", choices=[("scavo", "Saggio di scavo"), ("ceramica", "Ciotola su turntable")]),
            Field("pottery", "Crea anche la demo ceramica (turntable con target)", True),
            Field("process", "Elabora subito tutto (circa 2-3 minuti)", True)],
            intro="Crea foto sintetiche di un saggio di scavo con 8 target georeferenziati (EPSG:3004), "
                  "una ciotola fotografata su un tappetino turntable con 12 target, mesh di ceramica e un "
                  "database PyArchInit di prova.")
        if not v:
            return
        from ..demo import create_demo

        def done(d):
            which = d.pottery_project if (v["which"] == "ceramica" and d.pottery_project) else d.project
            self.open_document(str(which))
            if d.pyarchinit_config:
                self.pyarchinit_connect(config=d.pyarchinit_config, quiet=True)
            text = (f"Saggio di scavo: {d.project}\nCiotola su turntable: {d.pottery_project or '-'}\n"
                    + (f"Profilo della ciotola: {d.pottery_profile}\n" if d.pottery_profile else "")
                    + f"Leggi {d.folder / 'LEGGIMI.txt'} per cosa provare.")
            self._info("Demo pronta", text)

        self.run_task("Creazione dati demo", lambda p: create_demo(v["folder"], process=v["process"],
                                                                   with_pottery=v["pottery"], progress=p),
                      done, refresh=False, save=False)

    def import_external(self, kind: str) -> None:
        """Open a project made by Metashape, WebODM/ODM or a Photo3D capture session."""
        if not self._confirm_discard():
            return
        if kind == "metashape":
            src, _ = QFileDialog.getOpenFileName(self, "Progetto Metashape", self._last_dir(), "Metashape (*.psx)")
        elif kind == "webodm":
            r = message(self, "question", "WebODM", "Importi l'archivio scaricato da WebODM (all.zip)?\n"
                        "Si = scegli all.zip, No = scegli la cartella del task ODM.",
                        buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                        | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel:
                return
            if r == QMessageBox.StandardButton.Yes:
                src, _ = QFileDialog.getOpenFileName(self, "all.zip di WebODM", self._last_dir(), "Zip (*.zip)")
            else:
                src = QFileDialog.getExistingDirectory(self, "Cartella del task ODM", self._last_dir())
        else:
            src = QFileDialog.getExistingDirectory(self, "Cartella della sessione Photo3D", self._last_dir())
        if not src:
            return
        stem = Path(src).stem if kind != "photo3d" else Path(src).name
        default = Path(src).parent / f"{stem}_ppg.ppg" if kind != "photo3d" else Path(src) / f"{stem}.ppg"
        out, _ = QFileDialog.getSaveFileName(self, "Salva il progetto importato", str(default), "Progetti (*.ppg)")
        if not out:
            return

        def job(p):
            if kind == "metashape":
                from ..integrations.metashape import import_psx

                return import_psx(src, out, progress=p)
            if kind == "webodm":
                from ..integrations.webodm import import_task

                return import_task(src, out, progress=p)
            from ..integrations.photo3d import import_session

            p(0.3, "Lettura della sessione Photo3D")
            return import_session(src, out)

        def done(doc):
            self._started = True
            self.set_document(doc)
            warn = list(dict.fromkeys(w for ch in doc.chunks for w in ch.meta.get("import", {}).get("warnings", [])))
            ch = doc.chunk
            text = (f"{len(doc.chunks)} chunk, {len(ch.cameras)} foto ({len(ch.aligned_cameras)} allineate), "
                    f"prodotti: {', '.join(sorted(k for k in ch.products if k != 'reconstruction')) or 'nessuno'}, "
                    f"CRS {ch.crs}")
            if ch.meta.get("point_cloud_source"):
                text += f"\nNuvola densa: {ch.meta['point_cloud_source']} ({len(ch.point_cloud)} punti)"
            self.log_pane.info(f"Importato {src}: {text}")
            for w in warn:
                self.log_pane.warning(w)
            self._info("Importazione completata", text + ("\n\n" + "\n".join(warn[:6]) if warn else ""))

        self.run_task("Importazione", job, done, refresh=False, save=False)

    # ---------------------------------------------------------------- workflow
    def add_chunk(self) -> None:
        label, ok = QInputDialog.getText(self, "Nuovo chunk", "Nome:", text=f"Chunk {len(self.doc.chunks) + 1}")
        if ok:
            self.doc.chunk = self.doc.add_chunk(label)
            self._dirty(True)
            self.refresh_all(reset_views=True)

    def add_photos(self, folder: bool = False) -> None:
        if not self._need_chunk():
            return
        if folder:
            d = QFileDialog.getExistingDirectory(self, "Cartella di foto", self._last_dir())
            paths = [d] if d else []
        else:
            paths, _ = QFileDialog.getOpenFileNames(self, "Foto", self._last_dir(),
                                                    "Immagini (*.jpg *.jpeg *.png *.tif *.tiff *.JPG *.JPEG *.PNG *.TIF)")
        if paths:
            self._started = True
            self.run_task("Aggiunta foto", lambda p: self.chunk.add_photos(paths, recursive=folder),
                          lambda cams: self.statusBar().showMessage(f"{len(cams)} foto aggiunte", 8000))

    def detect_markers(self) -> None:
        if not self._need_chunk():
            return
        v = ParamDialog.ask(self, "Rileva marker", [Field("target_type", "Tipo di target", "aruco_4x4_50",
                                                         choices=TARGET_TYPES)])
        if v:
            self.run_task("Rilevamento marker", lambda p: self.chunk.detect_markers(v["target_type"], progress=p),
                          lambda ms: self.statusBar().showMessage(f"{len(ms)} marker rilevati", 8000))

    def align_photos(self) -> None:
        if not self._need_chunk():
            return
        if not self.chunk.cameras:
            self._warn("Aggiungi prima delle foto.")
            return
        v = ParamDialog.ask(self, "Allinea foto", [
            Field("quality", "Precisione", "high", choices=QUALITY[1:]),
            Field("pair_selection", "Selezione coppie", "auto",
                  choices=[("auto", "Automatica"), ("exhaustive", "Tutte le coppie"), ("sequential", "Sequenziale"),
                           ("reference", "Da posizione GNSS")]),
            Field("keypoint_limit", "Limite punti chiave", 40000, minimum=1000, maximum=200000),
            Field("tiepoint_limit", "Limite punti di legame", 8000, minimum=500, maximum=100000)])
        if not v:
            return

        def job(p):
            self.chunk.match_photos(v["quality"], v["keypoint_limit"], v["tiepoint_limit"], v["pair_selection"],
                                    progress=p.sub(0, 0.5))
            n = self.chunk.align_cameras(progress=p.sub(0.5, 0.5))
            if any(m.reference.location for m in self.chunk.markers):
                self.last_report = self.chunk.update_transform()
            return n

        self.run_task("Allineamento", job, lambda n: self.statusBar().showMessage(
            f"{n}/{len(self.chunk.cameras)} foto allineate", 10000))

    def import_reference(self) -> None:
        if not self._need_chunk():
            return
        v = ParamDialog.ask(self, "Importa riferimenti", [
            Field("path", "File CSV/TXT", "", kind="file_open", filter="Testo (*.csv *.txt);;Tutti (*)"),
            Field("crs", "Sistema di riferimento (EPSG)", self.chunk.crs if self.chunk.crs != "LOCAL" else "EPSG:3004",
                  tip="Per esempio EPSG:3004, EPSG:6708, EPSG:32633, EPSG:4326 (geografiche -> UTM) o LOCAL"),
            Field("columns", "Colonne", "nxyz", tip="n=etichetta, x/y/z coordinate, a=accuratezza, -=salta"),
            Field("skip_rows", "Righe da saltare", 0, maximum=100)],
            intro="Una riga per marker: etichetta (o codice del target), X, Y, Z.")
        if not v or not v["path"]:
            return
        try:
            n = self.chunk.import_reference(v["path"], crs=v["crs"] or None, columns=v["columns"],
                                            skip_rows=v["skip_rows"])
        except Exception as exc:
            self._exc("Importazione dei riferimenti non riuscita", exc)
            return
        self.statusBar().showMessage(f"{n} riferimenti importati", 8000)
        self.log_pane.info(f"{n} riferimenti importati da {v['path']}")
        self._dirty(True)
        self.refresh_all()
        if self.chunk.aligned_cameras:
            self.update_transform()

    def update_transform(self) -> None:
        if not self._need_chunk(aligned=True):
            return

        def done(rep):
            self.last_report = rep
            self.reference.set_chunk(self.chunk, rep)
            self._show_pane(self.left_tabs, self.reference)

        self.run_task("Georeferenziazione", lambda p: self.chunk.update_transform(), done)

    def optimize_cameras(self) -> None:
        if not self._need_chunk(aligned=True):
            return

        def job(p):
            err = self.chunk.optimize_cameras(progress=p)
            if any(m.reference.location for m in self.chunk.markers):
                self.last_report = self.chunk.update_transform()
            return err

        self.run_task("Ottimizzazione camere", job,
                      lambda e: self.statusBar().showMessage(f"Errore di riproiezione {e:.3f} px", 10000))

    def build_dense(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        v = ParamDialog.ask(self, "Nuvola densa", [
            Field("quality", "Qualita'", "medium", choices=QUALITY),
            Field("filter_mode", "Filtro profondita'", "moderate",
                  choices=[("mild", "Lieve"), ("moderate", "Moderato"), ("aggressive", "Aggressivo")]),
            Field("backend", "Motore", "auto", choices=[("auto", "Automatico"), ("cpu", "CPU (integrato)"),
                                                        ("colmap", "COLMAP (GPU NVIDIA)"), ("openmvs", "OpenMVS")])])
        if not v:
            return

        def job(p):
            self.chunk.build_depth_maps(v["quality"], v["filter_mode"], v["backend"], progress=p.sub(0, 0.8))
            return self.chunk.build_point_cloud(progress=p.sub(0.8, 0.2))

        self.run_task("Nuvola densa", job, lambda pc: self._show_model("point_cloud"))

    def crop_cloud(self) -> None:
        """Keep the dense points inside a height range and/or a cylinder (turntable scans: the
        mat and the targets are not part of the object)."""
        if not self._need_chunk(product="point_cloud"):
            return
        v = ParamDialog.ask(self, "Ritaglia nuvola densa", [
            Field("zmin", "Quota minima (m)", 0.003, minimum=-1e6, maximum=1e6, decimals=3),
            Field("use_zmax", "Limita anche la quota massima", False),
            Field("zmax", "Quota massima (m)", 1.0, minimum=-1e6, maximum=1e6, decimals=3),
            Field("use_radius", "Limita a un cilindro attorno all'asse", True),
            Field("radius", "Raggio del cilindro (m)", 0.1, minimum=0.001, maximum=1e6, decimals=3),
            Field("cx", "Centro X", 0.0, minimum=-1e9, maximum=1e9, decimals=3),
            Field("cy", "Centro Y", 0.0, minimum=-1e9, maximum=1e9, decimals=3)],
            intro="Coordinate del sistema di riferimento del chunk. Per un oggetto su turntable: quota minima "
                  "pochi millimetri sopra il tappetino e raggio appena maggiore dell'oggetto, centro (0, 0).")
        if not v:
            return

        def job(p):
            p(0.2, "Ritaglio")
            return self.chunk.crop_point_cloud(zmin=v["zmin"], zmax=v["zmax"] if v["use_zmax"] else None,
                                               radius=v["radius"] if v["use_radius"] else None,
                                               center=(v["cx"], v["cy"]))

        self.run_task("Ritaglio nuvola", job, lambda pc: (self.statusBar().showMessage(f"{len(pc):,} punti", 8000),
                                                           self._show_model("point_cloud")))

    def build_model(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        src = "point_cloud" if self.chunk.product_path("point_cloud") else "tie_points"
        v = ParamDialog.ask(self, "Modello 3D", [
            Field("source", "Dati sorgente", src, choices=[("point_cloud", "Nuvola densa"),
                                                          ("tie_points", "Punti di legame")]),
            Field("surface", "Tipo di superficie", "arbitrary",
                  choices=[("arbitrary", "Arbitraria (3D)"), ("height_field", "Campo di altezze (2.5D, terreno)")]),
            Field("face_count", "Numero di facce", "medium",
                  choices=[("high", "Alto"), ("medium", "Medio"), ("low", "Basso")])])
        if v:
            self.run_task("Modello", lambda p: self.chunk.build_model(v["source"], v["surface"], v["face_count"],
                                                                    progress=p),
                          lambda _m: self._show_model("model"))

    def build_texture(self) -> None:
        if not self._need_chunk(aligned=True, product="model"):
            return
        v = ParamDialog.ask(self, "Texture", [Field("size", "Dimensione texture", 4096,
                                                    choices=[(2048, "2048"), (4096, "4096"), (8192, "8192")])])
        if v:
            self.run_task("Texture", lambda p: self.chunk.build_texture(v["size"], progress=p),
                          lambda _r: self._show_model("model_textured"))

    def build_dem(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        v = ParamDialog.ask(self, "DEM", [
            Field("source", "Dati sorgente", "point_cloud" if self.chunk.product_path("point_cloud") else "model",
                  choices=[("point_cloud", "Nuvola densa"), ("model", "Modello"), ("tie_points", "Punti di legame")]),
            Field("resolution", "Risoluzione (m/pixel)", None, kind="optional_float"),
            Field("interpolate", "Interpola i vuoti", True)])
        if v:
            self.run_task("DEM", lambda p: self.chunk.build_dem(v["resolution"], v["source"], v["interpolate"],
                                                               progress=p),
                          lambda _r: self._show_raster("dem"))

    def build_ortho(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        if not (self.chunk.dem_path or self.chunk.product_path("model")):
            self._warn("Costruisci prima il DEM o il modello.")
            return
        v = ParamDialog.ask(self, "Ortofoto", [
            Field("surface", "Superficie", "dem" if self.chunk.dem_path else "model",
                  choices=[("dem", "DEM"), ("model", "Modello (true ortho)")]),
            Field("blending", "Fusione", "mosaic", choices=[("mosaic", "Mosaico"), ("average", "Media")]),
            Field("resolution", "Risoluzione (m/pixel)", None, kind="optional_float")])
        if v:
            self.run_task("Ortofoto", lambda p: self.chunk.build_orthomosaic(v["resolution"], v["surface"],
                                                                            v["blending"], progress=p),
                          lambda _r: self._show_raster("ortho"))

    def process_all(self) -> None:
        if not self._need_chunk():
            return
        if not self.chunk.cameras:
            self._warn("Aggiungi prima delle foto.")
            return
        v = ParamDialog.ask(self, "Elabora tutto", [
            Field("markers", "Rileva target", True),
            Field("target_type", "Tipo di target", "aruco_4x4_50", choices=TARGET_TYPES),
            Field("align_quality", "Precisione allineamento", "high", choices=QUALITY[1:]),
            Field("dense_quality", "Qualita' nuvola densa", "medium", choices=QUALITY),
            Field("surface", "Superficie modello", "arbitrary",
                  choices=[("arbitrary", "Arbitraria (3D)"), ("height_field", "Campo di altezze (2.5D)")]),
            Field("texture", "Texture", True), Field("dem", "DEM", True), Field("ortho", "Ortofoto", True),
            Field("report", "Report HTML", True)])
        if not v:
            return

        def job(p):
            ch = self.chunk
            steps = [s for s in ("markers", "align", "dense", "model", "texture", "dem", "ortho", "report")
                     if s in ("align", "dense", "model") or v.get(s)]
            k = 1.0 / len(steps)
            i = 0

            def sp():
                nonlocal i
                i += 1
                return p.sub((i - 1) * k, k)

            if v["markers"]:
                ch.detect_markers(v["target_type"], progress=sp())
            q = sp()
            ch.match_photos(v["align_quality"], progress=q.sub(0, 0.5))
            ch.align_cameras(progress=q.sub(0.5, 0.5))
            if any(m.reference.location for m in ch.markers):
                self.last_report = ch.update_transform()
            q = sp()
            ch.build_depth_maps(v["dense_quality"], progress=q.sub(0, 0.8))
            ch.build_point_cloud(progress=q.sub(0.8, 0.2))
            ch.build_model(surface=v["surface"], progress=sp())
            if v["texture"]:
                ch.build_texture(progress=sp())
            if v["dem"]:
                ch.build_dem(progress=sp())
            if v["ortho"]:
                ch.build_orthomosaic(progress=sp())
            if v["report"] and self.doc.path:
                sp()(0.5, "Report")
                ch.generate_report(self.doc.path.with_name(self.doc.path.stem + "_report.html"))
            return ch

        self.run_task("Elaborazione completa", job, lambda _c: self._show_model(None))

    def _show_model(self, kind) -> None:
        self.model_view.set_chunk(self.chunk, kind)
        self.show_view(self.model_view)

    def _show_raster(self, kind: str) -> None:
        self._load_raster(kind)
        self.show_view(self.ortho_view)

    # ---------------------------------------------------------------- views and workspace
    def _workspace_item(self, kind: str, chunk, payload) -> None:
        if chunk is not None and chunk is not self.chunk and kind in ("select_chunk", "chunk"):
            self.doc.chunk = chunk
            self.refresh_all(reset_views=True)
            return
        if kind == "camera":
            self.open_photo(payload)
        elif kind == "product":
            if payload in ("dem", "orthomosaic"):
                self._show_raster("dem" if payload == "dem" else "ortho")
            else:
                self._show_model(payload)
        elif kind == "marker":
            cams = [chunk.camera(k) for k in payload.projections]
            if cams:
                self.open_photo(cams[0])
        elif kind == "crs":
            self.set_crs()

    def set_crs(self) -> None:
        crs, ok = QInputDialog.getText(self, "Sistema di riferimento", "EPSG del chunk (es. EPSG:3004) o LOCAL:",
                                       text=self.chunk.crs)
        if ok and crs:
            self.chunk.crs = crs.strip()
            self._dirty(True)
            self.refresh_all()

    def open_photo(self, camera) -> None:
        self.photo_view.show_camera(self.chunk, camera)
        self.show_view(self.photo_view)
        self.statusBar().showMessage(f"{camera.label}: tasto destro per posizionare un marker", 8000)

    def _markers_edited(self) -> None:
        self._dirty(True)
        try:
            self.chunk.triangulate_markers()
        except Exception as exc:
            self.log_pane.warning(f"Triangolazione dei marker non riuscita: {exc}")
        self.reference.set_chunk(self.chunk)
        self.workspace.populate(self.doc)

    def _show_coords(self, x: float, y: float, z) -> None:
        zt = f"  Z {z:.3f}" if z is not None else ""
        self.coord_label.setText(f"X {x:.3f}  Y {y:.3f}{zt}   [{self.chunk.crs if self.chunk else ''}]")

    def _after_console(self) -> None:
        if self.doc is not self.workspace.document:
            self.set_document(self.doc)
        else:
            self.workspace.populate(self.doc)
            self.reference.set_chunk(self.chunk)

    # ---------------------------------------------------------------- appearance and help
    def set_theme(self, mode: str) -> None:
        theme.apply(QApplication.instance(), mode)
        self.settings.setValue("theme", mode)
        for k, a in self.theme_actions.items():
            a.setChecked(k == mode)

    def open_docs(self) -> None:
        QDesktopServices.openUrl(QUrl(DOCS_URL))

    def open_log_folder(self) -> None:
        from .logging_setup import log_dir

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir())))

    def show_doctor(self) -> DoctorDialog:
        dlg = DoctorDialog(self)
        dlg.exec()
        return dlg

    def about(self) -> None:
        message(self, "info", "Informazioni",
                f"<b>{APP_TITLE} {__version__}</b><br>"
                "Fotogrammetria open source per l'archeologia e il BIM, integrata con PyArchInit.<br><br>"
                f"Visualizzatore 3D: {self.model_view.backend}<br>"
                "Licenza GNU GPL v2 o successiva<br>Autore: Enzo Cocca<br>"
                f"<a href='{DOCS_URL}'>{DOCS_URL}</a>")

    # ---------------------------------------------------------------- drawing tools
    def _apply_draw_options(self) -> None:
        """Freehand and snapping options to the raster view, remembered between sessions."""
        v = self.ortho_view
        v.freehand = self.a_freehand.isChecked()
        on = self.a_snap.isChecked()
        v.snap_vertex = on and self.a_snap_vertex.isChecked()
        v.snap_segment = on and self.a_snap_segment.isChecked()
        v.snap_px = float(next((px for px, a in self.snap_tol_actions.items() if a.isChecked()), 12))
        for key, value in (("draw/freehand", v.freehand), ("draw/snap", on),
                           ("draw/snap_vertex", self.a_snap_vertex.isChecked()),
                           ("draw/snap_segment", self.a_snap_segment.isChecked()), ("draw/snap_px", int(v.snap_px))):
            self.settings.setValue(key, "true" if value is True else "false" if value is False else value)
        if v.mode == "polygon":
            self.set_draw_mode("polygon")  # refresh the hint

    def set_draw_mode(self, mode: str) -> None:
        if mode != "pan" and self.ortho_view.tr is None:
            self.draw_actions["pan"].setChecked(True)
            self._warn("Costruisci prima l'ortofoto o il DEM.")
            return
        self.ortho_view.set_mode(mode)
        self.draw_actions[mode].setChecked(True)
        self.show_view(self.ortho_view)
        hints = {"pan": "Trascina per spostare, rotella per lo zoom, clic su un elemento per i suoi attributi",
                 "select": "Clic per selezionare, Maiusc/Cmd+clic per aggiungere o togliere, trascina per un riquadro; "
                           "Canc elimina, Unisci fonde le unita', Esc deseleziona",
                 "polygon": ("Tieni premuto e traccia l'unita' a mano libera, rilascia per chiudere; tasto destro per annullare"
                             if self.ortho_view.freehand else
                             "Clic per i vertici, doppio clic per chiudere, tasto destro per annullare"),
                 "line": "Due clic: inizio e fine della sezione", "measure": "Due clic per misurare",
                 "point": "Clic sinistro: punto positivo, destro: negativo (prompt SAM); quote nel log"}
        self.statusBar().showMessage(hints.get(mode, ""), 15000)

    def clear_drawings(self) -> None:
        self.units = []
        self.sam_points = []
        self.line_layers = {}
        self.ortho_view.clear_overlays()
        self._draw_sections()

    def _add_line_feature(self, layer: str, coords, color: str, label: str = "", attrs: dict | None = None,
                          width: float = 2.0) -> None:
        """A line on the raster view that is kept across DEM/orthomosaic reloads."""
        coords = np.asarray(coords, float)[:, :2]
        self.line_layers.setdefault(layer, []).append((coords, color, label, dict(attrs or {}), width))
        if self.ortho_view.tr is not None:
            self.ortho_view.add_line(coords, color, label, layer=layer, attrs=attrs, width=width)

    def _us_choices(self) -> list:
        if self.pa is None:
            return []
        try:
            sito = self.pa_pane.site_names()[0] if self.pa_pane.site_names() else None
            return [(str(u.get("area")), str(u.get("us")), str(u.get("d_stratigrafica") or ""))
                    for u in self.pa.stratigraphic_units(sito)] if sito else []
        except Exception:
            return []

    def _unit_drawn(self, xy) -> None:
        from shapely.geometry import Polygon

        from ..archaeo.stratigraphy import UnitPolygon

        dlg = UnitDialog(self, self._us_choices())
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        v = dlg.values()
        poly = Polygon(xy).buffer(0)
        u = UnitPolygon(poly, us=v["us"] or None, area=v["area"] or None, tipo=v["tipo"])
        self.units.append(u)
        self._draw_unit(u, len(self.units) - 1)
        self.log_pane.info(f"{u.tipo} {u.us}: area {poly.area:.3f} m2")

    def _draw_unit(self, u, i: int, layer: str | None = None) -> None:
        if u.attributes is None:
            u.attributes = {}
        layer = layer or u.attributes.get("_layer") or "Unita' stratigrafiche"
        u.attributes["_layer"] = layer  # survives raster reloads (DEM <-> orthomosaic)
        geoms = getattr(u.geometry, "geoms", [u.geometry])
        attrs = {"tipo": u.tipo, "us": u.us or "?", "area": u.area or "", "area_m2": round(u.geometry.area, 3),
                 **{k: v for k, v in (u.attributes or {}).items() if k != "gid" and not k.startswith("_")},
                 "_unit": i}
        if (u.attributes or {}).get("gid") is not None:
            attrs["_gid"] = u.attributes["gid"]
        for g in geoms:
            self.ortho_view.add_polygon(np.asarray(g.exterior.coords)[:, :2], f"{u.tipo} {u.us or '?'}",
                                        US_COLORS[i % len(US_COLORS)], layer=layer, attrs=attrs)

    def _sections_dir(self) -> Path:
        return (self.doc.path.parent if self.doc.path else Path.home()) / "sezioni"

    def _sections_file(self) -> Path:
        return self._sections_dir() / "sezioni.json"

    def _save_sections(self) -> None:
        if not self.sections:
            return
        self._sections_dir().mkdir(parents=True, exist_ok=True)
        self._sections_file().write_text(json.dumps(self.sections, indent=1, ensure_ascii=False), encoding="utf-8")

    def _load_sections(self) -> None:
        """Sections of the open project (``sezioni/sezioni.json``) into the pane and the layer."""
        self.sections = []
        f = self._sections_file()
        if f.exists():
            try:
                self.sections = [dict(x) for x in json.loads(f.read_text(encoding="utf-8"))]
            except (OSError, ValueError) as exc:
                log.warning("sezioni.json non leggibile: %s", exc)
        self.sections_pane.set_sections(self.sections)
        self._draw_sections()

    def _draw_sections(self) -> None:
        if self.ortho_view.tr is None:
            return
        for sdict in self.sections:
            self.ortho_view.add_line([sdict["p1"], sdict["p2"]], "#ffd000", sdict["label"], layer="Sezioni",
                                     attrs={"lunghezza_m": round(sdict.get("length", 0), 3), "svg": sdict.get("svg", "")})

    def _section_drawn(self, a, b) -> None:
        if not self._need_chunk(product="model"):
            return
        from ..archaeo import sections

        out_dir = self._sections_dir()
        n = len(self.sections) + 1
        while (out_dir / f"sezione_{n}.svg").exists():
            n += 1
        label = f"Sezione {n}"

        def job(p):
            p(0.1, "Taglio del modello")
            sec = sections.section(self.chunk, a, b, label=label)
            out_dir.mkdir(parents=True, exist_ok=True)
            svg = sec.to_svg(out_dir / f"sezione_{n}.svg")
            dxf = sec.to_dxf(out_dir / f"sezione_{n}.dxf", world=False)
            dxf3d = sec.to_dxf(out_dir / f"sezione_{n}_3d.dxf", world=True)
            return sec, svg, dxf, dxf3d

        def done(r):
            sec, svg, dxf, dxf3d = r
            self._last_section = sec
            sdict = {"label": label, "p1": [float(a[0]), float(a[1])], "p2": [float(b[0]), float(b[1])],
                     "length": float(sec.length), "svg": str(svg), "dxf": str(dxf), "dxf3d": str(dxf3d),
                     "kind": "vertical"}
            self.sections.append(sdict)
            self._save_sections()
            self.ortho_view.add_line([a, b], "#ffd000", label, layer="Sezioni",
                                     attrs={"lunghezza_m": round(sec.length, 3), "svg": str(svg)})
            self.sections_pane.add(sdict)
            self._show_pane(self.bottom_tabs, self.sections_pane)
            self.log_pane.info(f"{label}: lunghezza {sec.length:.3f} m, disegni in {out_dir}")

        self.run_task("Sezione", job, done, refresh=False, save=False)

    def _zoom_section(self, sdict: dict) -> None:
        from PySide6.QtCore import QRectF

        p1, p2 = self.ortho_view.scene_pt(*sdict["p1"]), self.ortho_view.scene_pt(*sdict["p2"])
        self.show_view(self.ortho_view)
        self.ortho_view.zoom_to(QRectF(p1, p2).normalized())

    def _publish_section(self, sdict: dict) -> None:
        if not self._need_pa():
            return
        from ..archaeo.sections import Section

        sites = self.pa_pane.site_names()
        site, ok = QInputDialog.getItem(self, "Sito", "Sito PyArchInit:", sites, 0, True)
        if not ok or not site:
            return
        sec = Section(np.array(sdict["p1"] + [0.0]), np.array(sdict["p2"] + [0.0]), label=sdict["label"])
        self.run_task("Pubblicazione sezione",
                      lambda p: self.pa.publish_section_trace(sec, site, sdict["label"], crs=self.chunk.crs),
                      lambda gid: self._info("PyArchInit", f"Traccia '{sdict['label']}' scritta in pyarchinit_sezioni (gid {gid})"),
                      refresh=False, save=False)

    def _delete_section(self, sdict: dict, confirm: bool = True, redraw: bool = True) -> None:
        if confirm:
            r = message(self, "question", "Elimina sezione", f"Eliminare {sdict['label']} e i suoi file?",
                        buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        default=QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return
        for key in ("svg", "dxf", "dxf3d"):
            try:
                Path(sdict.get(key, "")).unlink(missing_ok=True)
            except OSError:
                pass
        self.sections = [x for x in self.sections if x is not sdict and x.get("label") != sdict.get("label")]
        self._save_sections() if self.sections else self._sections_file().unlink(missing_ok=True)
        self.sections_pane.set_sections(self.sections)
        if redraw:
            self.ortho_view.remove_layer("Sezioni")
            self._draw_sections()

    def _attribute_edited(self, layer: str, idx: int, key: str, value) -> None:
        """An attribute changed in the Layers pane: update the unit / section behind it."""
        feats = self.ortho_view.features(layer)
        if not 0 <= idx < len(feats):
            return
        attrs = feats[idx]
        if "_unit" in attrs and 0 <= attrs["_unit"] < len(self.units):
            u = self.units[attrs["_unit"]]
            if key in ("us", "area", "tipo"):
                setattr(u, key, None if value in ("", None) else str(value))
                label = f"{u.tipo} {u.us or '?'}"
                for j, other in enumerate(feats):  # every part of a multipolygon
                    if other.get("_unit") == attrs["_unit"]:
                        self.ortho_view.set_feature_attribute(layer, j, key, value)
                        self.ortho_view.set_feature_attribute(layer, j, "etichetta", label)
            elif key != "etichetta":
                u.attributes[key] = value
            gid = (u.attributes or {}).get("gid")
            if gid is not None:
                self.pa_pending[int(gid)] = u
                self._update_pa_save_button()
        elif layer == "Sezioni" and key == "etichetta":
            p1 = attrs.get("_geom", (None, [[None]]))[1][0]
            for sdict in self.sections:
                if sdict["p1"] == p1:
                    sdict["label"] = str(value)
            self._save_sections()
            self.sections_pane.set_sections(self.sections)
        self._dirty(True)
        self._schedule_qgis()
        self.log_pane.info(f"{layer}: {key} = {value}")

    def _update_pa_save_button(self) -> None:
        n = len(set(self.pa_pending) | self.pa_deleted)
        self.b_pa_save.setText(f"Salva modifiche in PyArchInit ({n})")
        self.b_pa_save.setVisible(n > 0)

    def save_pending_units(self, confirm: bool = True) -> None:
        """Write to ``pyunitastratigrafiche`` the changes made here to polygons loaded from
        PyArchInit: attributes, geometries (merges) and deletions."""
        if not (self.pa_pending or self.pa_deleted) or not self._need_pa():
            return
        pending, deleted, geom = dict(self.pa_pending), set(self.pa_deleted), set(self.pa_geom_changed)
        if deleted and confirm:
            r = message(self, "question", "Salva modifiche in PyArchInit",
                        f"Eliminare {len(deleted)} poligoni da pyunitastratigrafiche e aggiornarne "
                        f"{len(set(pending) - deleted)}? Le schede US non vengono toccate.",
                        buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        default=QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return
        crs = self.chunk.crs if self.chunk is not None else None

        def job(p):
            n_del = sum(self.pa.delete_unit(gid) for gid in deleted)
            n_upd = 0
            for gid, u in pending.items():
                if gid not in deleted:
                    n_upd += self.pa.update_unit(gid, geometry=u.geometry if gid in geom else None, crs=crs,
                                                 us=u.us, area=u.area, tipo=u.tipo)
            return n_upd, n_del

        def done(r):
            self.pa_pending.clear()
            self.pa_deleted.clear()
            self.pa_geom_changed.clear()
            self._update_pa_save_button()
            self.pa_pane.refresh()
            n_upd, n_del = r
            self._info("PyArchInit", f"pyunitastratigrafiche: {n_upd} poligoni aggiornati"
                       + (f", {n_del} eliminati" if n_del else ""))

        self.run_task("Aggiornamento unita' in PyArchInit", job, done, refresh=False, save=False)

    # ---------------------------------------------------------------- selection: delete and merge
    def _selection_changed(self, n: int) -> None:
        self.a_sel_delete.setEnabled(n > 0)
        self.a_sel_merge.setEnabled(n > 1)
        if self.ortho_view.mode == "select":
            self.statusBar().showMessage(f"{n} elementi selezionati: Canc per eliminare, Unisci per fonderli"
                                         if n else "Nessun elemento selezionato", 8000)

    def _unit_part(self, layer: str, attrs: dict) -> tuple[int, int] | None:
        """(unit index, part index) of a feature drawn for a unit (one feature per polygon part)."""
        i = attrs.get("_unit")
        if i is None or not 0 <= i < len(self.units):
            return None
        k = 0
        for f in self.ortho_view.features(layer):
            if f is attrs:
                return i, k
            if f.get("_unit") == i:
                k += 1
        return None

    def _forget_pa_unit(self, u, deleted: bool) -> None:
        gid = (u.attributes or {}).get("gid")
        if gid is None:
            return
        gid = int(gid)
        if deleted:
            self.pa_deleted.add(gid)
            self.pa_pending.pop(gid, None)
            self.pa_geom_changed.discard(gid)
        else:
            self.pa_pending[gid] = u
            self.pa_geom_changed.add(gid)

    def delete_selected(self, confirm: bool = True) -> int:
        """Delete the selected features: units (whole, or single parts of a multipolygon),
        section traces with their drawings, lines of the line layers, points. Polygons loaded
        from PyArchInit leave the database only with *Salva modifiche in PyArchInit*."""
        from shapely.geometry import MultiPolygon

        sel = self.ortho_view.selected_features()
        if not sel:
            return 0
        parts: dict[int, set[int]] = {}
        sections, lines, skip = [], {}, set()
        for layer, idx, attrs in sel:
            if "_unit" in attrs:
                ref = self._unit_part(layer, attrs)
                if ref is not None:
                    parts.setdefault(ref[0], set()).add(ref[1])
            elif layer == "Sezioni":
                p1 = attrs.get("_geom", (None, [[None]]))[1][0]
                sdict = next((x for x in self.sections if x["p1"] == p1), None)
                if sdict is not None:
                    sections.append(sdict)
            elif layer in self.line_layers:
                lines.setdefault(layer, set()).add(idx)
            else:  # points and anything else drawn only in the view
                skip.add(id(attrs))
                if attrs.get("_geom", ("",))[0] == "Point":
                    x, y = attrs["_geom"][1]
                    self.sam_points = [p for p in self.sam_points if abs(p[0] - x) > 1e-6 or abs(p[1] - y) > 1e-6]
        if confirm:
            from_pa = sum(1 for i in parts if (self.units[i].attributes or {}).get("gid") is not None)
            text = f"Eliminare {len(sel)} elementi selezionati?"
            if sections:
                text += f"\n{len(sections)} tracce di sezione: anche i loro disegni (SVG/DXF)."
            if from_pa:
                text += (f"\n{from_pa} poligoni vengono da PyArchInit: escono dal database solo con "
                         "«Salva modifiche in PyArchInit».")
            r = message(self, "question", "Elimina selezionati", text,
                        buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        default=QMessageBox.StandardButton.Yes)
            if r != QMessageBox.StandardButton.Yes:
                return 0
        removed = set()
        for i, ks in parts.items():
            u = self.units[i]
            geoms = list(getattr(u.geometry, "geoms", [u.geometry]))
            keep = [g for k, g in enumerate(geoms) if k not in ks]
            if keep:
                u.geometry = keep[0] if len(keep) == 1 else MultiPolygon(keep)
            else:
                removed.add(i)
            self._forget_pa_unit(u, deleted=not keep)
        self.units = [u for i, u in enumerate(self.units) if i not in removed]
        for sdict in sections:
            self._delete_section(sdict, confirm=False, redraw=False)
        for layer, idxs in lines.items():
            kept = [ln for k, ln in enumerate(self.line_layers[layer]) if k not in idxs]
            if kept:
                self.line_layers[layer] = kept
            else:
                del self.line_layers[layer]
        self._update_pa_save_button()
        self._redraw_overlays(skip=skip)
        self._dirty(True)
        self.log_pane.info(f"Eliminati {len(sel)} elementi")
        return len(sel)

    def merge_selected(self) -> bool:
        """Merge the selected units into one polygon (the attributes of one of them are kept),
        or contiguous lines of the same line layer into one line."""
        v = self.ortho_view
        sel = v.selected_features()
        if len(sel) < 2:
            self._warn("Seleziona almeno due elementi da unire (Maiusc+clic, oppure trascina un riquadro).")
            return False
        kinds = {v.layers[layer]["kind"] for layer, _, _ in sel}
        if kinds == {"polygon"} and all("_unit" in a for _, _, a in sel):
            return self._merge_units(sel)
        layers = {layer for layer, _, _ in sel}
        if kinds == {"line"} and len(layers) == 1 and next(iter(layers)) in self.line_layers:
            return self._merge_lines(next(iter(layers)), sorted({i for _, i, _ in sel}))
        self._warn("Si possono unire unita' (poligoni) tra loro, oppure linee contigue dello stesso layer "
                   "(curve, piante a quota). Tracce di sezione e punti non si uniscono.")
        return False

    def _merge_units(self, sel) -> bool:
        from shapely.geometry import MultiPolygon
        from shapely.ops import unary_union

        order = []
        for _layer, _idx, attrs in sel:
            i = attrs.get("_unit")
            if i is not None and 0 <= i < len(self.units) and i not in order:
                order.append(i)
        if not order:
            return False
        keep = order[0]
        if len({(self.units[i].tipo, self.units[i].us, self.units[i].area) for i in order}) > 1:
            choices = [(str(i), f"{self.units[i].tipo} {self.units[i].us or '?'} (area {self.units[i].area or '-'})"
                        + (" - da PyArchInit" if (self.units[i].attributes or {}).get("gid") is not None else ""))
                       for i in order]
            vals = ParamDialog.ask(self, "Unisci unita'", [
                Field("keep", "Numero, area e tipo di", choices[0][0], choices=choices)],
                intro="Le unita' selezionate diventano un solo poligono. Scegli l'unita' di cui mantenere "
                      "numero, area e tipo: le altre vengono eliminate.")
            if not vals:
                return False
            keep = int(vals["keep"])
        # close the hairline gaps left between units digitised side by side (half a display pixel)
        eps = max(0.001, 0.5 * abs(self.ortho_view.tr.a)) if self.ortho_view.tr is not None else 0.001
        merged = unary_union([self.units[i].geometry.buffer(eps, join_style=2) for i in order]).buffer(-eps, join_style=2)
        polys = [g for g in getattr(merged, "geoms", [merged]) if g.geom_type == "Polygon" and not g.is_empty]
        if not polys:
            self._warn("L'unione non ha prodotto un poligono valido.")
            return False
        target = self.units[keep]
        target.geometry = polys[0] if len(polys) == 1 else MultiPolygon(polys)
        self._forget_pa_unit(target, deleted=False)
        for i in order:
            if i != keep:
                self._forget_pa_unit(self.units[i], deleted=True)
        self.units = [u for i, u in enumerate(self.units) if i == keep or i not in order]
        self._update_pa_save_button()
        self._redraw_overlays()
        self._dirty(True)
        self.log_pane.info(f"Unite {len(order)} unita' in {target.tipo} {target.us or '?'}: area "
                           f"{target.geometry.area:.3f} m2" + (f", {len(polys)} parti separate" if len(polys) > 1 else ""))
        return True

    def _merge_lines(self, layer: str, idxs: list[int]) -> bool:
        from shapely.geometry import LineString, MultiLineString
        from shapely.ops import linemerge

        entries = self.line_layers[layer]
        lines = [np.asarray(entries[i][0], float)[:, :2].copy() for i in idxs]
        eps = max(0.001, abs(self.ortho_view.tr.a)) if self.ortho_view.tr is not None else 0.001
        ends = [ln[[0, -1]] for ln in lines]
        for a, ln in enumerate(lines):  # snap end points that almost touch
            for end in (0, -1):
                for b, other in enumerate(ends):
                    if b == a:
                        continue
                    d = np.linalg.norm(other - ln[end], axis=1)
                    if d.min() <= eps:
                        ln[end] = other[int(d.argmin())]
                        break
        merged = linemerge(MultiLineString([LineString(ln) for ln in lines]))
        if merged.geom_type != "LineString":
            self._warn("Le linee selezionate non sono contigue: non si possono unire in una sola linea.")
            return False
        first = idxs[0]
        _coords, color, label, attrs, width = entries[first]
        entries[first] = (np.asarray(merged.coords)[:, :2], color, label, attrs, width)
        self.line_layers[layer] = [e for k, e in enumerate(entries) if k == first or k not in idxs]
        self._redraw_overlays()
        self._dirty(True)
        self.log_pane.info(f"{layer}: unite {len(idxs)} linee, lunghezza {merged.length:.3f} m")
        return True

    def _redraw_overlays(self, skip: set | None = None) -> None:
        """Draw again every layer from the project data (units, sections, line layers) without
        reloading the raster; features only in the view (points) are kept unless in ``skip``.
        Layer visibility and order are preserved."""
        v = self.ortho_view
        if v.tr is None:
            return
        skip = skip or set()
        visible = {n: lay["visible"] for n, lay in v.layers.items()}
        order = list(v.layer_order)
        modelled = set(self.line_layers) | {"Sezioni"}
        others = []
        for name in order:
            lay = v.layers[name]
            if name in modelled or any("_unit" in f[2] for f in lay["features"]):
                continue
            for item, _label, attrs in lay["features"]:
                if id(attrs) in skip:
                    continue
                color = (item.brush().color() if lay["kind"] == "point" else item.pen().color()).name()
                others.append((name, lay["kind"], dict(attrs), color))
        v.clear_overlays()
        for i, u in enumerate(self.units):
            self._draw_unit(u, i)
        self._draw_sections()
        for name, lines in self.line_layers.items():
            for coords, color, label, attrs, width in lines:
                v.add_line(coords, color, label, layer=name, attrs=attrs, width=width)
        for name, kind, attrs, color in others:
            gtype, coords = attrs.get("_geom", (None, None))
            label = attrs.get("etichetta", "")
            extra = {k: val for k, val in attrs.items() if not k.startswith("_") and k not in ("etichetta", "x", "y")}
            if gtype == "Point":
                v.add_point(coords[0], coords[1], color, label, layer=name, attrs=extra)
            elif gtype == "LineString":
                v.add_line(coords, color, label, layer=name, attrs=extra)
            elif gtype == "Polygon":
                v.add_polygon(coords, label, color, layer=name, attrs=extra)
        v.layer_order = [n for n in order if n in v.layers] + [n for n in v.layer_order if n not in order]
        for name, vis in visible.items():
            if not vis and name in v.layers:
                v.set_layer_visible(name, False)
        v.layers_changed.emit()

    # ---------------------------------------------------------------- PyArchInit US form
    def open_us_form(self, sito: str, area: str = "1", us: str = "", tipo: str = "US", defaults: dict | None = None) -> bool:
        if not self._need_pa():
            return False
        if not sito:
            sites = self.pa_pane.site_names()
            sito, ok = QInputDialog.getItem(self, "Sito", "Sito PyArchInit:", sites or [""], 0, True)
            if not ok or not sito:
                return False
        dlg = UsFormDialog(self.pa, sito, area, us, tipo, self, defaults=defaults)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return False
        self.pa_pane.refresh()
        self.log_pane.info(f"Scheda {dlg.tipo.currentText()} {dlg.us.text()} salvata in PyArchInit ({sito})")
        return True

    def _unit_site(self, layer: str) -> str:
        prefix = "US da PyArchInit - "
        if layer.startswith(prefix):
            return layer[len(prefix):]
        return self.pa_pane.current_site() or ""

    def _quote_defaults(self, geometry) -> dict:
        """Lowest and highest DEM elevation inside a unit: defaults for a new US form."""
        if self.chunk is None or not self.chunk.dem_path or geometry is None:
            return {}
        try:
            from ..archaeo import terrain

            r = terrain.volume(self.chunk.dem_path, None, geometry)
            return {"quota_min_abs": f"{r['quota_min']:.3f}", "quota_max_abs": f"{r['quota_max']:.3f}"}
        except Exception:
            return {}

    def _us_form_from_feature(self, layer: str, idx: int) -> None:
        feats = self.ortho_view.features(layer)
        if not 0 <= idx < len(feats):
            return
        attrs = feats[idx]
        u = self.units[attrs["_unit"]] if "_unit" in attrs and attrs["_unit"] < len(self.units) else None
        us = str(attrs.get("us") or "").replace("?", "")
        area = str(attrs.get("area") or "1")
        tipo = str(attrs.get("tipo") or "US")
        self.open_us_form(self._unit_site(layer), area, us, tipo,
                          defaults=self._quote_defaults(u.geometry if u is not None else None))

    def _us_form_from_proxy(self, row: int) -> None:
        entries = self.chunk.meta.get("proxies", [])
        if 0 <= row < len(entries):
            e = entries[row]
            self.open_us_form(e.get("sito") or self.pa_pane.current_site() or "", e.get("area") or "1",
                              e.get("us") or "", e.get("tipo") or "US",
                              defaults={"quota_min_abs": f"{e['z_bottom'][0]:.3f}", "quota_max_abs": f"{e['z_top'][1]:.3f}"})

    # ---------------------------------------------------------------- proxies
    def create_proxies(self) -> None:
        if not self._need_chunk(product="dem"):
            return
        drawn = [u for u in self.units if u.us]
        sites = self.pa_pane.site_names() if self.pa is not None else []
        choices = ([("drawn", f"Unita' disegnate ({len(drawn)})")] if drawn else []) + \
            [("pa:" + s, f"Poligoni US da PyArchInit - {s}") for s in sites]
        if not choices:
            self._warn("Disegna delle unita' (con il numero di US) o connetti PyArchInit per usare i poligoni delle US.")
            return
        v = ParamDialog.ask(self, "Proxy 3D delle US", [
            Field("units", "Unita'", choices[0][0], choices=choices),
            Field("base", "Base del solido", "plane",
                  choices=[("plane", "Piano a quota data"), ("dem", "DEM dopo la rimozione dell'unita'"),
                           ("thickness", "Spessore costante"), ("min", "Quota minima dell'unita'")]),
            Field("z", "Quota del piano (m)", 0.0, minimum=-10000, maximum=10000, decimals=3),
            Field("thickness", "Spessore (m)", 0.1, minimum=0.001, maximum=100, decimals=3),
            Field("dem", "DEM dopo la rimozione", "", kind="file_open", filter="GeoTIFF (*.tif *.tiff)")],
            intro="Ogni US diventa un solido chiuso: la pianta e' il poligono dell'unita', la superficie superiore "
                  "il DEM del progetto, quella inferiore la base scelta. Il volume e' quello del computo.")
        if not v:
            return
        from ..archaeo.proxies import proxies_for_units, save_proxy

        base = {"plane": v["z"], "dem": v["dem"] or None, "min": None, "thickness": None}[v["base"]]
        thickness = v["thickness"] if v["base"] == "thickness" else None
        if v["base"] == "dem" and not v["dem"]:
            self._warn("Indica il DEM dopo la rimozione dell'unita'.")
            return
        site = v["units"][3:] if v["units"].startswith("pa:") else (sites[0] if sites else "")

        def job(p):
            units = drawn if v["units"] == "drawn" else self.pa.us_polygons(site, crs=self.chunk.crs)
            proxies = proxies_for_units(self.chunk.dem_path, units, base=base, thickness=thickness,
                                        progress=p.sub(0, 0.9))
            folder = self.chunk.data_dir / "proxies"
            entries = []
            for px in proxies:
                path = save_proxy(px, folder, "ply")
                entries.append({"name": px.name, "us": px.us, "area": px.area, "tipo": px.tipo, "sito": site,
                                "file": str(path.relative_to(self.chunk.data_dir)), "volume_m3": px.volume_m3,
                                "area_m2": px.area_m2, "z_top": list(px.z_top), "z_bottom": list(px.z_bottom),
                                "base": v["base"]})
            return entries

        def done(entries):
            old = {e["name"]: e for e in self.chunk.meta.get("proxies", [])}
            for e in entries:
                old[e["name"]] = e
            self.chunk.meta["proxies"] = list(old.values())
            self.proxies_pane.set_proxies(self.chunk.meta["proxies"])
            self._show_pane(self.left_tabs, self.proxies_pane)
            self._show_model("proxies")
            self.log_pane.info(f"{len(entries)} proxy creati, volume {sum(e['volume_m3'] for e in entries):.3f} m3")

        self.run_task("Proxy delle US", job, done)

    def _proxy_objects(self, rows=None):
        import trimesh

        from ..archaeo.proxies import UnitProxy

        entries = self.chunk.meta.get("proxies", [])
        out = []
        for i, e in enumerate(entries):
            if rows is not None and i not in rows:
                continue
            m = trimesh.load(str(self.chunk.data_dir / e["file"]), process=False, force="mesh")
            out.append((i, e, UnitProxy(e["name"], e.get("us"), e.get("area"), e.get("tipo") or "US", m, e["volume_m3"],
                                        e["area_m2"], tuple(e["z_top"]), tuple(e["z_bottom"]))))
        return out

    def export_proxies(self) -> None:
        if not self.chunk.meta.get("proxies"):
            self._warn("Crea prima i proxy delle US.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Proxy per Extended Matrix", self._last_dir() + "/proxy_us.obj",
                                              "OBJ (*.obj)")
        if not path:
            return
        from ..archaeo.proxies import export_extended_matrix

        objs = [o[2] for o in self._proxy_objects()]
        allv = np.vstack([np.asarray(o.mesh.vertices) for o in objs])
        out = export_extended_matrix(objs, path, offset=np.floor(allv.min(0)))
        self._info("Proxy esportati", f"{len(objs)} oggetti in {out}\nOffset delle coordinate in {out.name}.offset.txt")

    def publish_proxies(self, rows: list) -> None:
        if not self._need_pa():
            return
        from ..archaeo.proxies import save_proxy
        from ..pyarchinit.media import register_media

        items = self._proxy_objects(set(rows))

        def job(p):
            done_ = []
            for k, (i, e, px) in enumerate(items):
                p(k / max(len(items), 1), f"Pubblicazione {px.name}")
                site = e.get("sito") or self.pa_pane.current_site()
                if not site:
                    raise RuntimeError("indica il sito (pubblica dai poligoni di un sito PyArchInit)")
                folder = self.pa._media_folder(site, "proxy_us")
                obj = save_proxy(px, folder, "obj")
                links = []
                if px.us and self.pa.us_record(site, px.area or "1", px.us, px.tipo) is not None:
                    links = self.pa._links(site, [(px.area or "1", px.us, px.tipo)])
                rid = register_media(self.pa.db, obj, mediatype="3d_model", descrizione=f"Proxy 3D {px.name}",
                                     tags="['proxy', 'extended matrix']", links=links, overwrite=True,
                                     **self.pa._media_dirs())
                done_.append((i, rid, bool(links)))
            return done_

        def done(res):
            entries = self.chunk.meta["proxies"]
            for i, rid, linked in res:
                entries[i]["id_media"] = rid
                entries[i]["linked"] = linked
            self.proxies_pane.set_proxies(entries)
            missing = [entries[i]["name"] for i, _r, linked in res if not linked]
            self._info("PyArchInit", f"{len(res)} proxy pubblicati come modelli 3D"
                       + (f"; senza scheda US (crea la scheda e ripubblica): {', '.join(missing)}" if missing else ""))

        self.run_task("Pubblicazione dei proxy", job, done)

    def quote_proxies(self, rows: list) -> None:
        if not self._need_pa():
            return
        entries = self.chunk.meta.get("proxies", [])
        n, missing = 0, []
        for i in rows:
            e = entries[i]
            site = e.get("sito") or self.pa_pane.current_site()
            rec = self.pa.us_record(site, e.get("area") or "1", e.get("us") or "", e.get("tipo")) if e.get("us") else None
            if rec is None:
                missing.append(e["name"])
                continue
            n += self.pa.update_us(int(rec["id_us"]), quota_min_abs=round(e["z_bottom"][0], 3),
                                   quota_max_abs=round(e["z_top"][1], 3))
        self.pa_pane.refresh()
        self._info("PyArchInit", f"Quote scritte in {n} schede US"
                   + (f"; schede mancanti: {', '.join(missing)}" if missing else ""))

    def delete_proxies(self, rows: list) -> None:
        entries = self.chunk.meta.get("proxies", [])
        keep = [e for i, e in enumerate(entries) if i not in set(rows)]
        for i in rows:
            try:
                (self.chunk.data_dir / entries[i]["file"]).unlink()
            except (OSError, IndexError):
                pass
        self.chunk.meta["proxies"] = keep
        self.proxies_pane.set_proxies(keep)
        self._dirty(True)

    # ---------------------------------------------------------------- QGIS
    def _qgis_dir(self) -> Path:
        if self.doc.path:
            return self.doc.path.parent / f"{self.doc.path.stem}_qgis"
        return Path.home() / "ppg_qgis"

    def _schedule_qgis(self) -> None:
        if getattr(self, "a_qgis_live", None) is not None and self.a_qgis_live.isChecked():
            self._qgis_timer.start(400)

    def _export_qgis_layers(self) -> dict:
        from ..io.qgis_link import export_layers

        view = self.ortho_view
        layers = {name: {"kind": lay["kind"], "features": view.features(name)} for name, lay in view.layers.items()}
        try:
            return export_layers(self._qgis_dir(), layers, self.chunk.crs if self.chunk else "")
        except Exception as exc:  # a live refresh must never interrupt drawing with an error window
            log.warning("Aggiornamento dei layer per QGIS non riuscito: %s", exc, exc_info=True)
            return {}

    def open_in_qgis(self) -> None:
        """QGIS project with orthomosaic, DEM and the drawn layers, which QGIS reloads every
        second while the drawings change here."""
        from ..io import qgis_link

        q = qgis_link.find_qgis()
        if q is None:
            self._warn("QGIS non trovato. Installalo da qgis.org (oppure indica il percorso con la variabile PPG_QGIS).")
            return
        ch = self.chunk
        files = self._export_qgis_layers()
        name = self.doc.path.stem if self.doc.path else "progetto"
        project = self._qgis_dir() / f"{name}_ppg.qgs"
        rasters = []
        if ch is not None and ch.orthomosaic_path:
            rasters.append({"name": "Ortofoto", "path": str(ch.orthomosaic_path)})
        if ch is not None and ch.dem_path:
            rasters.append({"name": "DEM (ombreggiato)", "path": str(ch.dem_path), "hillshade": True,
                            "opacity": 0.6, "hidden": True})
            rasters.append({"name": "DEM", "path": str(ch.dem_path), "hidden": True})

        def job(p):
            p(0.2, "Creazione del progetto QGIS")
            rep = qgis_link.build_project(project, ch.crs if ch else "", files, rasters, title=name, qgis=q)
            qgis_link.open_in_qgis(project, q)
            return rep

        def done(rep):
            self.a_qgis_live.setChecked(True)
            bad = [v["name"] for v in rep["vectors"] + rep["rasters"] if not v["valid"]]
            self.log_pane.info(f"Progetto QGIS: {project} ({len(rep['vectors'])} layer vettoriali aggiornati dal vivo, "
                               f"{len(rep['rasters'])} raster)" + (f"; non validi: {', '.join(bad)}" if bad else ""))
            self.statusBar().showMessage("QGIS aperto: i disegni si aggiornano in QGIS ogni secondo", 15000)

        self.run_task("Apertura in QGIS", job, done, refresh=False, save=False)

    def _feature_clicked(self, layer: str, attrs: dict) -> None:
        text = "  ".join(f"{k}: {v}" for k, v in attrs.items() if v not in (None, "") and not str(k).startswith("_"))
        self.statusBar().showMessage(f"{layer}: {text}", 15000)
        self.layers_pane.show_attributes(layer, attrs)

    def _ortho_point(self, x: float, y: float, label: int) -> None:
        z = self.ortho_view.elevation(x, y)
        self.sam_points.append((x, y, label))
        self.log_pane.info(f"Punto {len(self.sam_points)}: X {x:.3f} Y {y:.3f}"
                           + (f" Z {z:.3f}" if z is not None else ""))

    # ---------------------------------------------------------------- tools
    def print_targets(self) -> None:
        v = ParamDialog.ask(self, "Fogli di target", [
            Field("path", "File (PDF o PNG)", str(Path(self._last_dir()) / "target.pdf"), kind="file_save",
                  filter="PDF (*.pdf);;PNG (*.png)"),
            Field("target_type", "Tipo", "aruco_4x4_50", choices=TARGET_TYPES),
            Field("codes", "Codici", "0-11", tip="Per esempio 0-11 oppure 1,5,7"),
            Field("size", "Lato del target (mm)", 40.0, minimum=5, maximum=500, decimals=1),
            Field("page", "Formato", "A4", choices=[("A4", "A4"), ("A3", "A3")])])
        if not v:
            return
        from ..markers.targets import generate_sheet

        codes = []
        for part in v["codes"].split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-")
                codes += list(range(int(a), int(b) + 1))
            elif part:
                codes.append(int(part))
        try:
            files = generate_sheet(v["path"], v["target_type"], codes, v["size"], v["page"])
        except Exception as exc:
            self._exc("Generazione dei target non riuscita", exc)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(files[0])))

    def print_turntable(self) -> None:
        v = ParamDialog.ask(self, "Tappetino turntable", [
            Field("path", "File", str(Path(self._last_dir()) / "tappetino.pdf"), kind="file_save",
                  filter="PDF (*.pdf);;PNG (*.png)"),
            Field("n", "Numero di target", 12, minimum=4, maximum=48),
            Field("diameter", "Diametro dell'anello (mm)", 180.0, minimum=50, maximum=1000, decimals=1),
            Field("size", "Lato del target (mm)", 22.0, minimum=5, maximum=100, decimals=1)],
            intro="Genera anche il CSV delle coordinate dei target (sistema locale in metri) da importare "
                  "come riferimenti.")
        if not v:
            return
        from ..markers.targets import generate_turntable_mat

        try:
            files, layout = generate_turntable_mat(v["path"], n_targets=v["n"], diameter_mm=v["diameter"],
                                                   marker_size_mm=v["size"])
            csv = Path(v["path"]).with_suffix(".csv")
            csv.write_text("".join(f"{k},{x:.5f},{y:.5f},{z:.5f}\n" for k, (x, y, z) in layout.items()))
        except Exception as exc:
            self._exc("Generazione del tappetino non riuscita", exc)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(files[0])))
        self.statusBar().showMessage(f"Coordinate dei target: {csv}", 15000)

    def contours(self) -> None:
        if not self._need_chunk(product="dem"):
            return
        v = ParamDialog.ask(self, "Curve di livello", [
            Field("interval", "Equidistanza (m)", 0.05, minimum=0.001, maximum=100, decimals=3),
            Field("index_every", "Curva direttrice ogni", 5, minimum=1, maximum=50),
            Field("path", "Salva (DXF, GeoJSON, SHP, GPKG)", str(Path(self._last_dir()) / "curve.dxf"),
                  kind="file_save", filter="DXF (*.dxf);;GeoJSON (*.geojson);;GeoPackage (*.gpkg)")])
        if not v:
            return
        from ..archaeo import terrain

        def job(p):
            lines = terrain.contours(self.chunk.dem_path, v["interval"], index_every=v["index_every"])
            terrain.export_contours(lines, v["path"], crs=self.chunk.crs)
            return lines

        def done(lines):
            self._show_raster("dem")
            name = f"Curve di livello ({v['interval']} m)"
            self.line_layers.pop(name, None)
            self.ortho_view.remove_layer(name)
            for ln in lines:
                self._add_line_feature(name, ln["coords"], "#ffffff" if ln.get("index") else "#202020",
                                       attrs={"quota": round(float(ln.get("elevation", 0)), 3),
                                              "direttrice": bool(ln.get("index"))}, width=1.6 if ln.get("index") else 1.0)
            self.statusBar().showMessage(f"{len(lines)} curve salvate in {v['path']}", 10000)
            self._show_pane(self.left_tabs, self.layers_pane)

        self.run_task("Curve di livello", job, done, refresh=False, save=False)

    def compute_volume(self) -> None:
        if not self._need_chunk(product="dem"):
            return
        polys = [u.geometry for u in self.units]
        v = ParamDialog.ask(self, "Volume", [
            Field("polygon", "Area di calcolo", "last" if polys else "all",
                  choices=([("last", "Ultima unita' disegnata"), ("union", "Unione delle unita' disegnate")]
                           if polys else []) + [("all", "Tutto il DEM")]),
            Field("reference", "Superficie di riferimento", "plane",
                  choices=[("plane", "Piano alla quota del terreno originario"),
                           ("dem", "DEM prima dello scavo"),
                           ("min", "Quota minima dell'area (deposito sopra il fondo)")]),
            Field("elevation", "Quota del piano (m)", 0.0, minimum=-10000, maximum=10000, decimals=3),
            Field("dem", "DEM di confronto", "", kind="file_open", filter="GeoTIFF (*.tif *.tiff)")])
        if not v:
            return
        from shapely.ops import unary_union

        from ..archaeo import terrain

        poly = None if v["polygon"] == "all" else (polys[-1] if v["polygon"] == "last" else unary_union(polys))
        ref = None if v["reference"] == "min" else (v["elevation"] if v["reference"] == "plane" else v["dem"])

        def done(r):
            self._last_volume = r
            what = ("deposito sopra il fondo" if v["reference"] == "min" else
                    "rispetto al " + ("piano a " + f"{v['elevation']:.3f} m" if v["reference"] == "plane" else "DEM precedente"))
            txt = (f"Area {r['area_m2']:.3f} m2 ({what}): scavo (volume asportato) {r['cut_m3']:.3f} m3, "
                   f"riporto {r['fill_m3']:.3f} m3, netto {r['net_m3']:.3f} m3; quote attuali "
                   f"{r['quota_min']:.3f} / {r['quota_max']:.3f} m")
            self._info("Volume", txt)

        self.run_task("Volume", lambda p: terrain.volume(self.chunk.dem_path, ref, poly), done, refresh=False,
                      save=False)

    def plan_slice(self) -> None:
        if not self._need_chunk(product="model"):
            return
        z0, ok = QInputDialog.getDouble(self, "Pianta a quota", "Quota (m):", 0.0, -10000, 10000, 3)
        if not ok:
            return
        from ..archaeo import sections

        out = (self.doc.path.parent if self.doc.path else Path.home()) / f"pianta_{z0:.3f}.dxf"

        def job(p):
            s = sections.plan_slice(self.chunk, z0)
            s.to_dxf(out, world=True)
            return s

        def done(sec):
            for pl in sec.polylines:
                self._add_line_feature(f"Pianta a quota {z0:.2f} m", pl[:, :2], "#ff00ff",
                                       attrs={"quota": z0, "dxf": str(out)})
            self.show_view(self.ortho_view)
            self.statusBar().showMessage(f"Pianta salvata in {out}", 10000)

        self.run_task("Pianta a quota", job, done, refresh=False, save=False)

    def pottery_profile(self) -> None:
        start = self._last_dir()
        v = ParamDialog.ask(self, "Profilo ceramico", [
            Field("mesh", "Mesh del vaso o frammento", "", kind="file_open",
                  filter="Mesh (*.obj *.ply *.stl *.glb *.off)"),
            Field("out", "Disegno (SVG o DXF)", str(Path(start) / "profilo.svg"), kind="file_save",
                  filter="SVG (*.svg);;DXF (*.dxf)"),
            Field("scale", "Scala 1:", 1.0, minimum=0.1, maximum=100, decimals=2),
            Field("units", "Unita' della mesh", "m", choices=[("m", "metri"), ("mm", "millimetri"),
                                                             ("cm", "centimetri")]),
            Field("title", "Titolo", "")],
            intro="L'asse di rotazione viene stimato dalle normali della superficie (funziona anche sui frammenti).")
        if not v or not v["mesh"]:
            return
        from ..archaeo import pottery

        def job(p):
            import trimesh

            p(0.1, "Lettura mesh")
            mesh = trimesh.load(v["mesh"], force="mesh", process=True)
            f = {"m": 1.0, "cm": 0.01, "mm": 0.001}[v["units"]]
            if f != 1.0:
                mesh.apply_scale(f)
            p(0.3, "Asse di rotazione")
            axis = pottery.estimate_axis(mesh)
            p(0.6, "Profilo")
            prof = pottery.best_profile(mesh, axis)
            out = pottery.draw(prof, v["out"], scale=v["scale"], title=v["title"] or None)
            return out, prof.measurements()

        def done(r):
            out, meas = r
            names = {"height_m": "Altezza", "max_diameter_m": "Diametro massimo", "rim_diameter_m": "Diametro orlo",
                     "base_diameter_m": "Diametro fondo", "wall_thickness_m": "Spessore parete"}
            txt = "  -  ".join(f"{names.get(k, k)} {val * 100:.1f} cm" for k, val in meas.items())
            self.log_pane.info(f"Profilo ceramico: {txt}")
            if str(out).lower().endswith(".svg"):
                ImageDialog("Profilo ceramico", out, txt, self).exec()
            else:
                self._info("Profilo ceramico", f"{txt}\n\nSalvato in {out}")

        self.run_task("Profilo ceramico", job, done, refresh=False, save=False)

    def classify(self) -> None:
        if not self._need_chunk(product="point_cloud"):
            return
        from ..ai.classify import class_statistics, classify_chunk

        def done(pc):
            st = class_statistics(pc)
            self.log_pane.info("Classi: " + ", ".join(f"{k}: {n}" for k, n in sorted(st.items())))
            self._show_model("point_cloud")

        self.run_task("Classificazione", lambda p: classify_chunk(self.chunk), done)

    def _segmenter(self):
        if self.sam is None:
            from ..ai.sam_env import make_segmenter

            self.sam = make_segmenter("small")
        return self.sam

    def _sam_available(self, then=None) -> bool:
        """True when SAM 2.1 can run; otherwise offer to install it (then call ``then``)."""
        from ..ai import sam_env

        if sam_env.available():
            return True
        r = message(self, "question", "Segment Anything 2.1",
                    "SAM 2.1 non e' ancora installato.\n\nL'applicazione puo' scaricare e installare ora PyTorch e "
                    "SAM 2.1 in un ambiente separato (circa 1-2 GB, alcuni minuti; serve la connessione). "
                    "I pesi del modello si scaricano al primo uso.\n\nInstallare adesso?",
                    buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    default=QMessageBox.StandardButton.Yes)
        if r == QMessageBox.StandardButton.Yes:
            self.install_sam(then=then)
        return False

    def install_sam(self, then=None) -> None:
        from ..ai import sam_env

        def done(env):
            self.sam = None
            self._info("Segment Anything 2.1", f"SAM 2.1 installato in {env}.")
            if then is not None:
                then()

        self.run_task("Installazione di SAM 2.1", lambda p: sam_env.install(progress=p), done, refresh=False, save=False)

    def remove_sam(self) -> None:
        from ..ai import sam_env

        if self.sam is not None and hasattr(self.sam, "close"):
            self.sam.close()
        self.sam = None
        sam_env.remove()
        self._info("Segment Anything 2.1", "Ambiente di SAM 2.1 rimosso.")

    def sam_auto_units(self) -> None:
        if not self._need_chunk(product="orthomosaic") or not self._sam_available(then=self.sam_auto_units):
            return
        from ..ai.sam import auto_units

        def done(units):
            start = len(self.units)
            self.units += units
            for i, u in enumerate(units):
                self._draw_unit(u, start + i)
            self.statusBar().showMessage(f"{len(units)} unita' candidate (assegna le US prima di pubblicarle)", 15000)

        self.run_task("SAM 2.1 - unita' automatiche", lambda p: auto_units(self.chunk, self._segmenter()), done,
                      refresh=False, save=False)

    def sam_from_points(self) -> None:
        if not self._need_chunk(product="orthomosaic") or not self._sam_available(then=self.sam_from_points):
            return
        if not self.sam_points:
            self._warn("Attiva Strumenti > Disegno su ortofoto > Quota puntuale / prompt SAM e clicca sull'unita'.")
            return
        from ..ai.sam import segment_orthomosaic

        pts = np.array([(x, y) for x, y, _ in self.sam_points])
        labels = np.array([lab for _, _, lab in self.sam_points])
        self.sam_points = []

        def done(u):
            dlg = UnitDialog(self, self._us_choices())
            if dlg.exec() == dlg.DialogCode.Accepted:
                vals = dlg.values()
                u.us, u.area, u.tipo = vals["us"] or None, vals["area"] or None, vals["tipo"]
            self.units.append(u)
            self._draw_unit(u, len(self.units) - 1)

        self.run_task("SAM 2.1", lambda p: segment_orthomosaic(self.chunk, self._segmenter(), points=pts,
                                                               labels=labels), done, refresh=False, save=False)

    def toggle_photo_sam(self) -> None:
        if not self.photo_view.sam_mode and not self._sam_available(then=self.toggle_photo_sam):
            return
        self.photo_view.sam_mode = not self.photo_view.sam_mode
        self.statusBar().showMessage("Prompt SAM sulle foto " + ("attivi: clic sinistro sull'oggetto"
                                                                if self.photo_view.sam_mode else "disattivati"), 10000)

    def _photo_sam_prompt(self, camera, x, y, label) -> None:
        def job(p):
            import cv2

            img = cv2.imread(camera.path, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)[:, :, ::-1]
            return self._segmenter().segment_points(img, np.array([[x - 0.5, y - 0.5]]), np.array([label]))

        self.run_task("SAM 2.1 sulla foto", job, lambda m: self.photo_view.show_mask(np.asarray(m, bool)),
                      refresh=False, save=False)

    def export_ifc(self) -> None:
        """IFC4 of the model, one element per stratigraphic unit (drawn here or loaded from
        PyArchInit), with progress and a preview of what was written."""
        if not self._need_chunk(product="model"):
            return
        drawn = [u for u in self.units if u.us]
        sites = self.pa_pane.site_names() if self.pa is not None else []
        choices = []
        if self.chunk.meta.get("proxies"):
            choices.append(("proxies", f"Proxy solidi delle US ({len(self.chunk.meta['proxies'])}): volumi e quantita' "
                                       "per il computo"))
        if drawn:
            choices.append(("drawn", f"Unita' disegnate in questo progetto ({len(drawn)})"))
        choices += [("pa:" + s, f"Poligoni US da PyArchInit - {s}") for s in sites]
        choices.append(("none", "Solo il modello del rilievo (un elemento)"))
        v = ParamDialog.ask(self, "Esporta IFC (BIM)", [
            Field("path", "File IFC", str(Path(self._last_dir()) / "rilievo.ifc"), kind="file_save", filter="IFC (*.ifc)"),
            Field("units", "Elementi per unita' stratigrafica", choices[0][0], choices=choices),
            Field("survey", "Includi anche il modello intero", True)],
            intro="Il modello viene tagliato in pianta con i poligoni delle US: ogni US diventa un IfcGeographicElement "
                  "con gli attributi della scheda (Pset_PyArchInit_US). Georeferenziazione con IfcMapConversion.")
        if not v or not v["path"]:
            return
        from ..bim.ifc import export_chunk_ifc, read_ifc_elements

        path, choice = v["path"], v["units"]

        def job(p):
            units, records, site = None, None, None
            if choice == "proxies":
                from ..bim.ifc import export_proxies_ifc

                objs = [o[2] for o in self._proxy_objects()]
                site = next((e.get("sito") for e in self.chunk.meta["proxies"] if e.get("sito")), None)
                if self.pa is not None and site:
                    try:
                        records = self.pa.us_records(site)
                    except Exception:
                        records = None
                survey = None
                if v["survey"] and self.chunk.model is not None:
                    survey = self.chunk.model
                    survey.apply_transform(self.chunk.transform)
                out = export_proxies_ifc(objs, path, crs=self.chunk.crs, us_records=records, site=site,
                                         survey_mesh=survey, project=self.chunk.label, progress=p.sub(0.05, 0.85))
                p(0.92, "Verifica del file IFC")
                return out, read_ifc_elements(out), len(objs)
            if choice == "drawn":
                units = drawn
                if self.pa is not None and sites:
                    site = sites[0]
            elif choice.startswith("pa:"):
                site = choice[3:]
                p(0.02, f"Lettura dei poligoni US di {site}")
                units = self.pa.us_polygons(site, crs=self.chunk.crs if self.chunk else None)
            if self.pa is not None and site:
                try:
                    records = self.pa.us_records(site)
                except Exception:
                    records = None
            out = export_chunk_ifc(self.chunk, path, units=units, us_records=records, site=site,
                                   include_survey=v["survey"], progress=p.sub(0.05, 0.85))
            p(0.92, "Verifica del file IFC")
            return out, read_ifc_elements(out), len(units or [])

        def done(r):
            out, elements, n_units = r
            self.log_pane.info(f"IFC salvato in {out}: {len(elements)} elementi")
            if n_units and len(elements) <= (1 if v["survey"] else 0):
                self._warn("Nessuna US interseca il modello: controlla che i poligoni stiano sull'area rilevata "
                           "e nello stesso sistema di riferimento.")
            IfcPreviewDialog(out, elements, self).exec()

        self.run_task("Esportazione IFC", job, done, refresh=False, save=False)

    def run_script(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Script Python", self._last_dir(), "Python (*.py)")
        if not path:
            return
        src = Path(path).read_text(encoding="utf-8")
        self._show_pane(self.bottom_tabs, self.console)
        self.console.write(f"# esecuzione di {path}\n")
        ns = self._namespace()
        ns["__name__"] = "__main__"
        ns["__file__"] = path
        import contextlib

        from .panels import _Stream

        stream = _Stream(self.console.write)
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                exec(compile(src, path, "exec"), ns)
        except Exception:
            self.console.write(traceback.format_exc())
        self._after_console()
        self.refresh_all()

    # ---------------------------------------------------------------- PyArchInit
    def pyarchinit_connect(self, ask: bool = False, config: str | Path | None = None, url: str | None = None,
                           quiet: bool = False) -> None:
        """Connect to PyArchInit: ``config`` (a config.cfg) or ``url`` (SQLite path / SQLAlchemy URL);
        ``ask`` opens the connection dialog. The connection runs in the background."""
        from ..pyarchinit import PyArchInit
        from ..pyarchinit.config import pyarchinit_home

        if ask:
            dlg = ConnectDialog(self, self.settings)
            if dlg.exec() != dlg.DialogCode.Accepted:
                return
            v = dlg.values()
            config, url = v["config"], v["url"]
        elif config is None and url is None:
            config = pyarchinit_home() / "pyarchinit_DB_folder" / "config.cfg"
            if not Path(config).exists():
                return self.pyarchinit_connect(ask=True)

        def job(p):
            p(0.2, "Connessione")
            return PyArchInit.connect(url=url, config_path=config)

        def done(pa):
            self.pa = pa
            self.pa_pane.set_connection(pa, pa.label)
            log.info("PyArchInit connesso: %s", pa.label)
            self.d_pa.show()
            try:
                missing = pa.missing_us_columns()
            except Exception:
                missing = []
            if missing:
                self.log_pane.warning(f"La scheda US di questo database e' ridotta: mancano {len(missing)} campi di "
                                      "PyArchInit 5" + (", tra cui i rapporti" if "rapporti" in missing else "")
                                      + ". PyArchInit > Aggiorna struttura del database li aggiunge.")

        def failed(summary, details):
            self.pa_pane.set_disconnected("Non connesso")
            if quiet:
                log.warning("Connessione automatica a PyArchInit non riuscita: %s", summary)
            else:
                self._error(summary, details)

        self.runner.run("Connessione a PyArchInit", job, done, failed, quiet=quiet)

    def pyarchinit_upgrade(self) -> None:
        """Add the PyArchInit 5 columns that ``us_table`` lacks (older or reduced databases)."""
        if not self._need_pa():
            return
        try:
            missing = self.pa.missing_us_columns()
        except Exception as exc:
            self._exc("Controllo della struttura del database non riuscito", exc)
            return
        if not missing:
            self._info("PyArchInit", "La scheda US del database ha gia' tutti i campi di PyArchInit 5.")
            return
        sqlite = self.pa.db.sqlite_path is not None
        r = message(self, "question", "Aggiorna struttura del database",
                    f"Alla tabella us_table mancano {len(missing)} campi della scheda US di PyArchInit 5"
                    + (", tra cui i rapporti stratigrafici" if "rapporti" in missing else "") + ".\n\n"
                    + "Aggiungerli? I record esistenti non vengono modificati."
                    + ("\nPrima viene fatta una copia di sicurezza del file." if sqlite
                       else "\nSu PostgreSQL fai prima un backup del database."),
                    buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    default=QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return

        def done(res):
            self.pa_pane.refresh()
            text = f"Aggiunti {len(res['added'])} campi alla scheda US."
            if res.get("backup"):
                text += f"\nCopia di sicurezza: {res['backup']}"
            self._info("PyArchInit", text)

        self.run_task("Aggiornamento della struttura PyArchInit", lambda p: self.pa.upgrade_us_table(), done,
                      refresh=False, save=False)

    def _autoconnect_pyarchinit(self) -> None:
        from ..pyarchinit.config import pyarchinit_home

        cfg = pyarchinit_home() / "pyarchinit_DB_folder" / "config.cfg"
        if (self._autoconnect and self.a_autoconnect.isChecked() and cfg.exists() and self.pa is None
                and not self.runner.busy):
            self.pyarchinit_connect(config=cfg, quiet=True)

    def pyarchinit_demo(self) -> None:
        """Create (once) and open the demo database. When a PyArchInit 5 installation exists
        (``~/pyarchinit_5/pyarchinit_DB_folder``) the database is written there as ``demo.sqlite``,
        so that QGIS/PyArchInit can open the same file; its ``config.cfg`` is left untouched."""
        from ..pyarchinit.config import pyarchinit_home
        from ..pyarchinit.demo_db import create_demo_database

        pa_home = pyarchinit_home()
        in_pyarchinit = (pa_home / "pyarchinit_DB_folder").exists()
        home = pa_home if in_pyarchinit else Path.home() / "ppg_demo" / "pyarchinit"
        try:
            cfg = create_demo_database(home, size="large")
        except Exception as exc:
            self._exc("Creazione del database demo non riuscita", exc)
            return
        db = home / "pyarchinit_DB_folder" / "demo.sqlite"
        if in_pyarchinit:
            self.pyarchinit_connect(url=str(db))
            self.log_pane.info(f"Database demo in {db}: per vederlo in QGIS seleziona demo.sqlite nelle "
                               "impostazioni di PyArchInit")
        else:
            self.pyarchinit_connect(config=cfg)

    def pyarchinit_new_site(self) -> None:
        if not self._need_pa():
            return
        dlg = SiteDialog(self)
        if dlg.exec() != dlg.DialogCode.Accepted or not dlg.values()["sito"]:
            return
        try:
            self.pa.ensure_site(**dlg.values())
        except Exception as exc:
            self._exc("Creazione del sito non riuscita", exc)
            return
        self.pa_pane.refresh()
        self.log_pane.info(f"Sito creato in PyArchInit: {dlg.values()['sito']}")

    def _need_pa(self) -> bool:
        if self.pa is None:
            self._warn("Connetti prima un database PyArchInit (menu PyArchInit).")
            return False
        return True

    def pyarchinit_publish(self) -> None:
        if not self._need_pa() or not self._need_chunk():
            return
        dlg = PublishDialog(self.pa, self.chunk, self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        v = dlg.values()
        if not v["nome_doc"]:
            self._warn("Indica il nome del documento.")
            return

        def job(p):
            out = {}
            kw = {"link_us": v["link_us"] or None, "disegnatore": v["disegnatore"], "overwrite": v["overwrite"]}
            if v["ortho"]:
                p(0.1, "Ortofoto")
                out["ortofoto"] = self.pa.publish_orthomosaic(self.chunk, v["sito"], v["nome_doc"], **kw)
            if v["dem"]:
                p(0.4, "DEM")
                out["dem"] = self.pa.publish_dem(self.chunk, v["sito"], v["nome_doc"] + " DEM", **kw)
            if v["model"]:
                p(0.7, "Modello 3D")
                out["modello"] = self.pa.publish_model(self.chunk, v["sito"], v["nome_doc"] + " modello", **kw)
            return out

        def done(out):
            self.pa_pane.refresh()
            self._info("PyArchInit", "Pubblicato:\n" + json.dumps(out, indent=1, default=str))

        self.run_task("Pubblicazione in PyArchInit", job, done, refresh=False, save=False)

    def pyarchinit_publish_units(self) -> None:
        if not self._need_pa():
            return
        units = [u for u in self.units if u.us]
        if not units:
            self._warn("Disegna (o segmenta con SAM) almeno un'unita' con il numero di US.")
            return
        sites = self.pa_pane.site_names()
        site, ok = QInputDialog.getItem(self, "Sito", "Sito PyArchInit:", sites, 0, False)
        if not ok:
            return
        self.run_task("Pubblicazione unita'", lambda p: self.pa.publish_units(units, site, crs=self.chunk.crs),
                      lambda g: self._info("PyArchInit", f"{len(g)} poligoni scritti in pyunitastratigrafiche"),
                      refresh=False, save=False)

    def pyarchinit_load_units(self) -> None:
        if not self._need_pa():
            return
        sites = self.pa_pane.site_names()
        site, ok = QInputDialog.getItem(self, "Sito", "Sito PyArchInit:", sites, 0, False)
        if not ok:
            return
        try:
            polys = self.pa.us_polygons(site, crs=self.chunk.crs if self.chunk else None)
        except Exception as exc:
            self._exc("Lettura delle unita' non riuscita", exc)
            return
        for u in polys:
            self.units.append(u)
            self._draw_unit(u, len(self.units) - 1, layer=f"US da PyArchInit - {site}")
        self.show_view(self.ortho_view)
        self._show_pane(self.left_tabs, self.layers_pane)

    def pyarchinit_publish_points(self) -> None:
        if not self._need_pa() or not self._need_chunk(product="dem"):
            return
        if not self.sam_points:
            self._warn("Clicca prima dei punti sull'ortofoto (Strumenti > Disegno > Quota puntuale).")
            return
        dlg = UnitDialog(self, self._us_choices())
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        v = dlg.values()
        site = self.pa_pane.site_names()[0] if self.pa_pane.site_names() else ""
        pts = [(x, y) for x, y, _ in self.sam_points]
        self.sam_points = []
        self.run_task("Pubblicazione quote", lambda p: self.pa.publish_elevations(self.chunk, site, v["area"], v["us"],
                                                                                 pts, unita_tipo=v["tipo"]),
                      lambda g: self._info("PyArchInit", f"{len(g)} quote scritte"),
                      refresh=False, save=False)


class DoctorDialog(QDialog):
    """Environment diagnosis (``ppg doctor``) with copy button."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from ..doctor import run_checks

        self.setWindowTitle("Diagnostica ambiente")
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        self.report = run_checks(gui=True)
        head = QLabel(("Nessun problema bloccante." if not self.report.failures else
                       f"{len(self.report.failures)} problemi impediscono il funzionamento.")
                      + (f"  {len(self.report.warnings)} avvisi." if self.report.warnings else ""))
        head.setObjectName("errorSummary")
        lay.addWidget(head)
        self.box = QPlainTextEdit(self.report.text())
        self.box.setObjectName("tracebackBox")
        self.box.setReadOnly(True)
        lay.addWidget(self.box, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        b_copy = QPushButton(icon("copy"), "Copia")
        b_copy.clicked.connect(lambda: QApplication.clipboard().setText(self.report.text()))
        bb.addButton(b_copy, QDialogButtonBox.ButtonRole.ActionRole)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
