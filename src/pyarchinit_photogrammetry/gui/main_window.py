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
from .dialogs import (QUALITY, ConnectDialog, ErrorDialog, Field, GradualSelectionDialog, IfcPreviewDialog,
                      ImageDialog, ParamDialog,
                      PublishDialog, QuotaDialog, SiteChoiceDialog, SiteDialog, TableDialog, UnitDialog, UsFormDialog,
                      choice, message)
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
QUOTE_LAYER = "Quote"  # spot heights (pyarchinit_quote)
CROP_LAYER = "Ritaglio"  # the polygon drawn with the crop tool


def _it(value: float, decimals: int = 0) -> str:
    """A number written the Italian way: 1.234.567 or 1.234,56."""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
CROP_PRODUCTS = [("dem", "DEM"), ("orthomosaic", "Ortofoto"), ("model", "Modello"), ("point_cloud", "Nuvola densa")]
SAM_TOOLS = {"tap": "Tocco: clic sull'oggetto", "box": "Riquadro: un oggetto nel riquadro",
             "area": "Area: tutte le unita' nel riquadro"}
SAM_MODELS = [("tiny", "Molto veloce (tiny)"), ("small", "Veloce (small)"), ("base_plus", "Accurato (base+)"),
              ("large", "Molto accurato (large)")]


CLASSIFIER_FILE = "classificatore.ppgclf"  # classifier trained in a chunk, kept in its data folder


class MainWindow(QMainWindow):
    def __init__(self, document: Document | None = None, autoconnect: bool = True, progress=None):
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
        self.classifier = None  # trained point classifier (ai.learn.ClassifierModel) in use
        self.quote: list[dict] = []  # spot heights: x, y and the columns of pyarchinit_quote
        self._sam_obj: dict | None = None  # SAM mask being edited (prompts, bounds, preview)
        self.last_report: dict | None = None
        self._started = document is not None
        self._build_views()
        self._build_docks()
        self._build_actions()
        self._build_statusbar()
        self.set_document(document or application.document, progress=progress)
        QTimer.singleShot(0, self._autoconnect_pyarchinit)

    # ================================================================ layout
    def _build_views(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setIconSize(QSize(18, 18))
        self.model_view = ModelView()
        self.model_view.points_selected.connect(self._points_selected)
        self.model_view.faces_selected.connect(self._faces_selected)
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
        self.ortho_view.crop_finished.connect(self._crop_drawn)
        self.ortho_view.crop_requested.connect(self.crop_with_selection)
        self.ortho_view.line_finished.connect(self._section_drawn)
        self.ortho_view.measured.connect(lambda t: (self.statusBar().showMessage(t, 30000),
                                                    self.log_pane.info(t)))
        self.ortho_view.quota_clicked.connect(self._quota_clicked)
        self.ortho_view.sam_click.connect(self._sam_click)
        self.ortho_view.sam_rect.connect(self._sam_rect)
        self.ortho_view.sam_key.connect(self._sam_key)
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
        self.workspace.context_requested.connect(self._workspace_menu)
        self.reference = ReferencePane()
        self.reference.import_requested.connect(self.import_reference)
        self.reference.update_requested.connect(self.update_transform)
        self.reference.optimize_requested.connect(self.optimize_cameras)
        self.reference.changed.connect(lambda: self._dirty(True))
        self.photos = PhotosPane()
        self.photos.thumbs_progress.connect(self._thumbs_progress)
        self.photos.open_camera.connect(self.open_photo)
        self.log_pane = LogPane()
        self.console = ConsolePane(self._namespace)
        self.console.executed.connect(self._after_console)
        self.pa_pane = PyArchInitPane()
        self.pa_pane.connect_requested.connect(lambda: self.pyarchinit_connect(ask=True))
        self.pa_pane.demo_requested.connect(self.pyarchinit_demo)
        self.pa_pane.new_site_requested.connect(self.pyarchinit_new_site)
        self.pa_pane.us_requested.connect(lambda sito, area, us, tipo: self.open_us_form(sito, area, us, tipo))
        self.pa_pane.site_changed.connect(self.set_project_site)
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
        exp.addSeparator()
        self._act(exp, "Ritagliati con un poligono o un riquadro (DEM, ortofoto, modello, nuvola)...",
                  self.start_crop, icon_name="crop")
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
        self._act(m, "Ricollega foto (altra cartella)...", lambda: self.relink_photos(), icon_name="relink",
                  tip="Collega le foto ai file con lo stesso nome in un'altra cartella (copia su un altro disco, "
                      "cartella spostata o tenuta «solo online» da Dropbox)")
        self._act(m, "Aggiungi chunk", self.add_chunk, icon_name="chunk_add")
        self._act(m, "Allinea chunk...", self.align_chunks, processing=True, icon_name="chunk")
        self._act(m, "Unisci chunk...", self.merge_chunks, processing=True, icon_name="merge")
        m.addSeparator()
        self._act(m, "Rileva marker codificati...", self.detect_markers, processing=True, icon_name="markers")
        self._act(m, "Allinea foto...", self.align_photos, processing=True, icon_name="align")
        self._act(m, "Importa riferimenti...", self.import_reference, icon_name="reference")
        self._act(m, "Aggiorna georeferenziazione", self.update_transform, processing=True, icon_name="georef")
        self._act(m, "Ottimizza camere", self.optimize_cameras, processing=True, icon_name="optimize")
        m.addSeparator()
        self._act(m, "Costruisci nuvola densa...", self.build_dense, processing=True, icon_name="dense")
        self._act(m, "Ritaglia nuvola densa...", self.crop_cloud, processing=True, icon_name="eraser")
        cl = m.addMenu(icon("clean"), "Pulizia e confidenza")
        self._act(cl, "Calcola la confidenza (nuvola densa, modello)...", self.compute_confidence, processing=True,
                  icon_name="confidence")
        self._act(cl, "Mostra la confidenza nella vista 3D", self.show_confidence, icon_name="confidence")
        cl.addSeparator()
        self._act(cl, "Pulisci la nuvola densa...", self.clean_cloud, processing=True, icon_name="clean")
        self._act(cl, "Pulisci il modello...", self.clean_model, processing=True, icon_name="clean")
        self._act(cl, "Pulisci i punti di legame (selezione graduale)...", self.clean_tie_points, processing=True,
                  icon_name="tie_clean")
        cl.addSeparator()
        self._act(cl, "Seleziona con il lazo nella vista 3D (elimina, tieni)", self.lasso_edit, icon_name="select")
        self._act(cl, "Ritaglia con un poligono o un riquadro...", self.start_crop, icon_name="crop")
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
                                ("quota", "Quota puntuale (pyarchinit_quote)", "point"),
                                ("crop", "Ritaglio: riquadro o poligono (esporta o ritaglia i prodotti)", "crop"),
                                ("sam", "Segment Anything (SAM)", "sam")):
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
        # SAM: how the clicks are used, model and thoroughness of the automatic scan
        self.sam_menu = QMenu("SAM", self)
        sam_tools = QActionGroup(self)
        self.sam_tool_actions = {}
        current_tool = self.settings.value("sam/tool", "tap")
        for key, label in SAM_TOOLS.items():
            a = self.sam_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(key == current_tool)
            a.triggered.connect(lambda _=False, k=key: self._set_sam_tool(k))
            sam_tools.addAction(a)
            self.sam_tool_actions[key] = a
        self.sam_menu.addSeparator()
        self._act(self.sam_menu, "Tutta l'ortofoto (scansione a tasselli)...", lambda: self.sam_whole_orthomosaic(),
                  processing=True, icon_name="sam")
        model_menu = self.sam_menu.addMenu("Modello")
        model_group = QActionGroup(self)
        self.sam_model_actions = {}
        for key, label in SAM_MODELS:
            a = model_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(key == self.settings.value("sam/model", "small"))
            a.triggered.connect(lambda _=False, k=key: self._set_sam_model(k))
            model_group.addAction(a)
            self.sam_model_actions[key] = a
        self.draw_actions["sam"].setMenu(self.sam_menu)
        # sections: what is cut (model or dense cloud) and which point classes
        self.section_menu = QMenu("Sezione", self)
        self._act(self.section_menu, "Opzioni delle sezioni (modello o nuvola, classi)...", self.section_options,
                  icon_name="line")
        self.draw_actions["line"].setMenu(self.section_menu)
        self.draw_actions["sam"].setToolTip("Segment Anything 2.1: clic su un oggetto (tocco), un riquadro per un "
                                            "oggetto o per tutte le unita' di un'area; dal menu anche tutta l'ortofoto")
        self.ortho_view.sam_tool = current_tool if current_tool in SAM_TOOLS else "tap"
        dm.addSeparator()
        self.a_sel_delete = self._act(dm, "Elimina selezionati", lambda: self.delete_selected(), icon_name="delete")
        self.a_sel_delete.setToolTip("Elimina gli elementi selezionati (tasto Canc nella modalita' Seleziona)")
        self.a_sel_merge = self._act(dm, "Unisci selezionati", lambda: self.merge_selected(), icon_name="merge")
        self.a_sel_merge.setToolTip("Unisce le unita' selezionate in un solo poligono, o linee contigue dello stesso layer")
        for a in (self.a_sel_delete, self.a_sel_merge):
            a.setEnabled(False)
        self.a_clear_drawings = self._act(dm, "Cancella disegni", self.clear_drawings, icon_name="eraser")
        self._act(dm, "Ritaglia o esporta con il poligono selezionato...", self.crop_with_selection, icon_name="crop")
        m.addSeparator()
        self.a_contours = self._act(m, "Curve di livello...", self.contours, processing=True, icon_name="contours")
        self.a_volume = self._act(m, "Volume scavo / riporto...", self.compute_volume, processing=True, icon_name="volume")
        self._act(m, "Opzioni delle sezioni...", self.section_options, icon_name="line")
        self.a_slice = self._act(m, "Pianta a quota (slice orizzontale)...", self.plan_slice, processing=True,
                                 icon_name="slice")
        self._act(m, "Profilo ceramico da mesh...", self.pottery_profile, processing=True, icon_name="pottery")
        self._act(m, "Proxy 3D delle US...", self.create_proxies, processing=True, icon_name="us")
        m.addSeparator()
        cm = m.addMenu(icon("classify"), "Classificazione della nuvola")
        self._act(cm, "Classificazione automatica (regole)", self.classify, processing=True, icon_name="classify")
        self._act(cm, "Mostra la nuvola per classi", self.show_classes, icon_name="classify")
        self._act(cm, "Correggi a mano con il lazo (vista 3D)", self.classify_with_lasso, icon_name="select")
        self._act(cm, "Classi dalle US (poligoni)...", self.units_to_classes, processing=True, icon_name="us")
        cm.addSeparator()
        self._act(cm, "Addestra il classificatore sui punti corretti...", self.train_classifier, processing=True,
                  icon_name="classify")
        self._act(cm, "Classifica con il classificatore addestrato...", self.apply_classifier, processing=True,
                  icon_name="classify")
        self._act(cm, "Carica un classificatore (.ppgclf)...", self.load_classifier, icon_name="open")
        self._act(cm, "Salva il classificatore...", self.save_classifier, icon_name="save")
        self._act(cm, "Dimentica le correzioni a mano", self.clear_manual_labels, icon_name="clear")
        sm = m.addMenu(icon("sam"), "Segment Anything 2.1")
        for key, label in SAM_TOOLS.items():
            self._act(sm, f"Ortofoto - {label}", lambda _=False, k=key: (self._set_sam_tool(k), self.set_draw_mode("sam")),
                      icon_name="sam")
        self._act(sm, "Ortofoto - tutta, scansione a tasselli...", lambda: self.sam_whole_orthomosaic(), processing=True,
                  icon_name="sam")
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
        self._act(m, "Sito del progetto...", lambda: self.choose_project_site(), icon_name="site")
        self._act(m, "Nuovo sito...", self.pyarchinit_new_site, icon_name="site")
        self._act(m, "Nuova scheda US...", lambda: self.open_us_form(self.project_site(), "1", "", "US"),
                  icon_name="us")
        self._act(m, "Aggiorna struttura del database...", self.pyarchinit_upgrade, icon_name="database")
        self.a_autoconnect = QAction("Connetti all'avvio al database di PyArchInit", self, checkable=True)
        self.a_autoconnect.setChecked(self.settings.value("pa_autoconnect", "true") in ("true", True, 1, "1"))
        self.a_autoconnect.toggled.connect(lambda on: self.settings.setValue("pa_autoconnect", "true" if on else "false"))
        m.addAction(self.a_autoconnect)
        m.addSeparator()
        self._act(m, "Pubblica ortofoto / DEM / modello...", self.pyarchinit_publish, processing=True,
                  icon_name="publish")
        self._act(m, "Pubblica unita' e quote disegnate", self.pyarchinit_publish_drawings, processing=True,
                  icon_name="publish")
        self._act(m, "Pubblica solo le unita' (pyunitastratigrafiche)", self.pyarchinit_publish_units,
                  processing=True, icon_name="publish")
        self._act(m, "Pubblica solo le quote (pyarchinit_quote)", self.pyarchinit_publish_points, processing=True,
                  icon_name="publish")
        self._act(m, "Carica poligoni US sull'ortofoto", self.pyarchinit_load_units, icon_name="load")
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
                         (self.draw_actions["quota"], "Quota"), (self.draw_actions["sam"], "SAM"),
                         (self.a_sel_delete, "Elimina"),
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
        for mode in ("line", "measure", "quota", "sam"):
            db.addAction(self.draw_actions[mode])
        for mode in ("sam", "line"):
            button = db.widgetForAction(self.draw_actions[mode])
            if button is not None:
                button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
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
        a = db.addAction(icon("publish"), "Pubblica")
        a.setToolTip("Scrive in PyArchInit le unita' disegnate (pyunitastratigrafiche) e le quote (pyarchinit_quote)")
        a.triggered.connect(lambda _=False: self.pyarchinit_publish_drawings())
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

    def set_document(self, doc: Document, progress=None) -> None:
        """Show a project. ``progress(fraction, text)`` follows the loading of the views."""
        step = progress or (lambda *_a, **_k: None)
        application.document = doc
        if not doc.chunks:
            doc.add_chunk("Chunk 1")
        self.units = []
        self.quote = []
        self._sam_obj = None
        self.classifier = None
        self.sections = []
        self.line_layers = {}
        self._restore_drawings()
        self.pa_pane.set_project_site(self.project_site())
        self.refresh_all(reset_views=True, progress=step)
        step(0.95, "Sezioni e disegni")
        self._load_sections()
        self.proxies_pane.set_proxies(self.chunk.meta.get("proxies", []) if self.chunk else [])
        if doc.path:
            self._add_recent(doc.path)
        # a project already opened in QGIS stays linked: its drawings keep updating there
        from ..io.qgis_link import LIVE_DB

        linked = doc.path is not None and (self._qgis_dir() / LIVE_DB).exists()
        if linked != self.a_qgis_live.isChecked():
            self.a_qgis_live.setChecked(linked)
        elif linked:
            self._schedule_qgis()

    def refresh_all(self, reset_views: bool = False, progress=None) -> None:
        step = progress or (lambda *_a, **_k: None)
        self._update_title()
        step(0.05, "Area di lavoro")
        self.workspace.populate(self.doc)
        self.reference.set_chunk(self.chunk, self.last_report)
        step(0.15, "Foto (le miniature arrivano mentre lavori)")
        self.photos.set_chunk(self.chunk)
        step(0.25, "Vista 3D")
        self.model_view.set_chunk(self.chunk) if reset_views else self.model_view.set_chunk(self.chunk,
                                                                                             self.model_view.kind)
        step(0.6, "Ortofoto e DEM")
        self._load_raster()
        self._update_stack()

    def _load_raster(self, kind: str | None = None, path=None) -> None:
        """The orthomosaic or the DEM of the chunk in the raster view (``path``: another file of
        that kind, e.g. a kept version or a difference of DEMs) with the drawings on top."""
        ch = self.chunk
        kind = kind or ("ortho" if ch is not None and ch.orthomosaic_path else "dem")
        if path is None:
            path = (ch.orthomosaic_path if kind == "ortho" else ch.dem_path) if ch is not None else None
        if path is None or not Path(path).exists():
            self.ortho_view.scene().clear()
            self.ortho_view.tr = None
            self.ortho_view.scene().addText("Costruisci il DEM o l'ortofoto per vederli qui")
            return
        self.ortho_view.load(path, kind)
        if kind == "ortho" and ch.dem_path:
            self.ortho_view.set_dem(ch.dem_path)
        self._raster_version = None
        with self.ortho_view.batch():  # thousands of contour lines: one update of panes and QGIS
            for i, u in enumerate(self.units):
                self._draw_unit(u, i)
            for i, q in enumerate(self.quote):
                self._draw_quota(q, i)
            self._draw_sections()
            for name, lines in self.line_layers.items():
                for coords, color, label, attrs, width in lines:
                    self.ortho_view.add_line(coords, color, label, layer=name, attrs=attrs, width=width)

    def _dirty(self, flag: bool = True) -> None:
        self.setWindowModified(flag)

    def _update_title(self) -> None:
        name = self.doc.path.name if self.doc.path else "senza nome"
        site = self.project_site()
        self.setWindowTitle(f"{APP_TITLE} {__version__} - {name}" + (f" - sito {site}" if site else "") + "[*]")

    # ---------------------------------------------------------------- site of the project
    def project_site(self) -> str:
        """The excavation (PyArchInit site) every unit and spot height of the project belongs to."""
        return str(self.doc.meta.get("sito") or "")

    def set_project_site(self, sito: str) -> None:
        sito = (sito or "").strip()
        if sito == self.project_site():
            return
        if sito:
            self.doc.meta["sito"] = sito
        else:
            self.doc.meta.pop("sito", None)
        for u in self.units:  # the drawings of the project follow it (units loaded from a site keep theirs)
            if (u.attributes or {}).get("gid") is None:
                u.sito = sito or None
        for q in self.quote:
            if q.get("gid") is None:
                q["sito_q"] = sito
        self.pa_pane.set_project_site(sito)
        self._update_title()
        self._dirty(True)
        self._redraw_drawings()
        self.log_pane.info(f"Sito del progetto: {sito or '(nessuno)'}")

    def _project_open(self) -> bool:
        return self.doc.path is not None or any(ch.cameras for ch in self.doc.chunks)

    def choose_project_site(self, reason: str = "") -> str:
        """Ask which site the project belongs to: the sites of the PyArchInit database, or a new name."""
        sites = self.pa_pane.site_names() if self.pa is not None else []
        text = (reason + "\n\n" if reason else "") + ("Sito dello scavo: tutte le US e le quote di questo progetto "
                                                       "appartengono a questo sito.")
        dlg = SiteChoiceDialog(self, sites, self.project_site(), text)
        if dlg.exec() == dlg.DialogCode.Accepted and dlg.value():
            self.set_project_site(dlg.value())
        return self.project_site()

    def _ensure_site(self, reason: str = "") -> str:
        """The site of the project, asked once when it is not set yet ("" when cancelled)."""
        return self.project_site() or self.choose_project_site(reason or "Scegli prima il sito del progetto.")

    def _sync_site_with_db(self, ask: bool) -> None:
        """After connecting: a database with one site gives the project its site; with more
        sites the user chooses (when a project is open)."""
        if self.pa is None or self.project_site():
            return
        sites = self.pa_pane.site_names()
        if len(sites) == 1:
            self.set_project_site(sites[0])
        elif sites and ask:
            self.choose_project_site(f"Il database contiene {len(sites)} siti (scavi).")

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
                    self._store_drawings()
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
        card = self._progress_card("Apertura del progetto", Path(path).name)
        step = card.progress(0, 100) if card is not None else (lambda *_a, **_k: None)
        try:
            step(0.02, "Lettura del progetto")
            try:
                doc = Document.open(path)
            except Exception as exc:
                if card is not None:
                    card.close()
                    card = None
                self._exc(f"Impossibile aprire {path}", exc)
                return
            self.last_report = None
            self._started = True
            self.set_document(doc, progress=lambda f, text=None: step(0.08 + 0.9 * f, text))
            step(1.0, "Pronto")
        finally:
            if card is not None:
                card.close()
        self.statusBar().showMessage(f"Aperto {path}", 5000)

    def _progress_card(self, title: str, subtitle: str = ""):
        """The splash card over the window, for a long step that is not a background task."""
        try:
            from ppg_launcher import Splash
        except Exception:  # pragma: no cover - launcher module missing
            return None
        card = Splash(title, subtitle, parent=self)
        card.step(0, "")
        return card

    def save_document(self, ask: bool = False) -> bool:
        if ask or self.doc.path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Salva progetto", self._last_dir(), "Progetti (*.ppg)")
            if not path:
                return False
            self._store_drawings()
            self.doc.save(path)
            self._add_recent(self.doc.path)
        else:
            self._store_drawings()
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
        self.photos.stop()
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
        classes = None
        if what == "cloud":
            choices, codes = self._cloud_class_choices(noise=True)
            if len(codes) > 1:
                v = ParamDialog.ask(self, "Esporta nuvola", [
                    Field("classes", "Classi da esportare", codes, choices=choices, kind="checklist")],
                    intro="Il file LAS conserva le classi; puoi esportarne solo alcune (per esempio le murature).")
                if not v:
                    return
                if not v["classes"]:
                    self._warn("Scegli almeno una classe.")
                    return
                classes = None if set(v["classes"]) >= set(codes) else sorted(v["classes"])
        path, flt = QFileDialog.getSaveFileName(self, "Esporta", self._last_dir(), filters[what])
        if not path:
            return
        try:
            if what == "cloud":
                ch.export_point_cloud(path, classes=classes)
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
            if ch.meta.get("import", {}).get("dem"):
                text += f"\nDEM: {ch.meta['import']['dem']}"
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

    def _two_chunks(self) -> bool:
        if len(self.doc.chunks) < 2:
            self._warn("Servono almeno due chunk (Flusso di lavoro > Aggiungi chunk), ognuno allineato per conto suo.")
            return False
        return True

    def align_chunks(self) -> None:
        """Bring the other chunks into the frame of a reference chunk (markers, photos or points)."""
        if not self._two_chunks():
            return
        doc = self.doc
        ref0 = self.chunk if self.chunk is not None else doc.chunks[0]
        choices = [(ch.key, f"{ch.label} ({len(ch.aligned_cameras)} foto allineate)") for ch in doc.chunks]
        v = ParamDialog.ask(self, "Allinea chunk", [
            Field("reference", "Chunk di riferimento", ref0.key, choices=choices),
            Field("chunks", "Chunk da allineare", [ch.key for ch in doc.chunks if ch is not ref0 and ch.enabled],
                  choices=choices, kind="checklist"),
            Field("method", "Metodo", "markers",
                  choices=[("markers", "Marker con lo stesso nome (almeno 3)"),
                           ("cameras", "Foto in comune tra i chunk"),
                           ("points", "Punti (nuvole che si sovrappongono)")]),
            Field("fix_scale", "Scala fissa (solo rototraslazione: chunk gia' in metri)", False)],
            intro="Ogni chunk resta com'e' (foto, nuvole, mesh): cambia solo la sua trasformazione, che lo porta nel "
                  "sistema del chunk di riferimento. Con i punti la posizione iniziale viene da marker, "
                  "georeferenziazione o foto in comune, altrimenti dalla forma delle nuvole. DEM e ortofoto dei "
                  "chunk allineati vanno ricostruiti.")
        if not v:
            return
        ref = next(ch for ch in doc.chunks if ch.key == v["reference"])
        targets = [ch for ch in doc.chunks if ch.key in v["chunks"] and ch is not ref]
        if not targets:
            self._warn("Scegli almeno un chunk da allineare diverso dal riferimento.")
            return
        from ..engine.chunks import _is_scaled

        metric = _is_scaled(ref)

        def done(report):
            lines = []
            for e in report["chunks"]:
                rmse = e.get("rmse")
                err = "" if rmse is None else (f", errore medio {rmse * 1000:.1f} mm" if metric else f", RMSE {rmse:.4g}")
                lines.append(f"{e['label']}: {e.get('correspondences', 0)} corrispondenze{err}, scala {e['scale']:.4f}"
                             + (f"; da ricostruire: {', '.join(e['stale_products'])}" if e.get("stale_products") else ""))
            if report.get("levelled_reference"):
                lines.append(f"Il chunk {ref.label} e' stato prima messo in bolla (come per il DEM).")
            text = f"Allineati a {ref.label} ({dict(markers='marker', cameras='foto', points='punti')[v['method']]}):\n" \
                + "\n".join(lines)
            self._info("Allinea chunk", text)

        self.run_task("Allineamento dei chunk",
                      lambda p: doc.align_chunks(ref, targets, method=v["method"], fix_scale=v["fix_scale"], progress=p),
                      done)

    def merge_chunks(self) -> None:
        """A new chunk with the photos, markers and clouds of the chosen chunks (already aligned)."""
        if not self._two_chunks():
            return
        doc = self.doc
        choices = [(ch.key, ch.label) for ch in doc.chunks]
        v = ParamDialog.ask(self, "Unisci chunk", [
            Field("chunks", "Chunk da unire (il primo da' il sistema di riferimento)",
                  [ch.key for ch in doc.chunks if ch.enabled], choices=choices, kind="checklist"),
            Field("label", "Nome del nuovo chunk", "Chunk unito", kind="text"),
            Field("markers", "Unisci i marker con lo stesso nome", True),
            Field("tie_points", "Unisci i punti di legame", True),
            Field("clouds", "Unisci le nuvole dense", True),
            Field("models", "Unisci le mesh", False),
            Field("activate", "Rendi attivo il nuovo chunk", True)],
            intro="I chunk devono essere nello stesso sistema: allineali prima (Allinea chunk) o georeferenziali. "
                  "Il nuovo chunk ha tutte le foto (quelle presenti in piu' chunk una volta sola), i marker e le "
                  "nuvole; i chunk di partenza restano. DEM e ortofoto si ricostruiscono sul chunk unito.")
        if not v:
            return
        sources = [ch for ch in doc.chunks if ch.key in v["chunks"]]
        if len(sources) < 2:
            self._warn("Scegli almeno due chunk da unire.")
            return

        def done(merged):
            info = merged.meta.get("merge", {})
            if v["activate"]:
                doc.chunk = merged
                self.refresh_all(reset_views=True)
            text = (f"Creato «{merged.label}»: {len(merged.cameras)} foto, {len(merged.markers)} marker"
                    + (f", nuvola densa di {len(merged.point_cloud):,} punti".replace(",", ".")
                       if merged.point_cloud is not None else ""))
            for w in info.get("warnings", []):
                self.log_pane.warning(w)
            self._info("Unisci chunk", text)

        self.run_task("Unione dei chunk",
                      lambda p: doc.merge_chunks(sources, label=v["label"].strip() or None, merge_markers=v["markers"],
                                                 merge_tie_points=v["tie_points"], merge_point_clouds=v["clouds"],
                                                 merge_models=v["models"], progress=p), done)

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
        ch = self.chunk
        controls = [m for m in ch.markers if not m.checkpoint and m.reference.enabled and m.reference.location]
        checks = [m for m in ch.markers if m.checkpoint and m.reference.location]
        v = ParamDialog.ask(self, "Ottimizza camere", [
            Field("markers", f"Usa i punti di controllo nell'ottimizzazione ({len(controls)} GCP)", len(controls) >= 3),
            Field("weight", "Peso di una misura di marker (in osservazioni di punti di legame)", 4, minimum=1, maximum=50),
            Field("pp", "Ottimizza anche il punto principale (cx, cy)", False)],
            intro="Bundle adjustment globale di posizioni, orientamenti e calibrazione. Con 3 o piu' punti di controllo "
                  "i GCP entrano nel calcolo come punti fissi alle loro coordinate: il blocco si piega per passarci "
                  "(corregge anche deformazioni come la cupola di una distorsione sbagliata, non solo la "
                  f"rototraslazione). I check point ({len(checks)}) restano fuori e misurano il risultato.")
        if not v:
            return

        def job(p):
            err = ch.optimize_cameras(fit_principal_point=v["pp"], use_markers=v["markers"],
                                      marker_weight=int(v["weight"]), progress=p)
            if any(m.reference.location for m in ch.markers):
                self.last_report = ch.update_transform()
            return err

        def done(err):
            rep = ch.meta.get("sfm", {}).get("optimize", {})
            text = f"Errore di riproiezione {err:.3f} px"
            if rep.get("control_points"):
                b, a = rep.get("before", {}), rep.get("after", {})

                def mm(x):
                    return "-" if x is None else f"{x * 1000:.1f} mm"

                text += (f"; {rep['control_points']} punti di controllo: errore {mm(b.get('control'))} -> "
                         f"{mm(a.get('control'))}" + (f", check point {mm(b.get('check'))} -> {mm(a.get('check'))}"
                                                     if a.get("check") is not None else ""))
            self.log_pane.info("Ottimizzazione camere: " + text)
            self.statusBar().showMessage(text, 15000)

        self.run_task("Ottimizzazione camere", job, done)

    def build_dense(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        v = ParamDialog.ask(self, "Nuvola densa", [
            Field("quality", "Qualita'", "medium", choices=QUALITY),
            Field("filter_mode", "Filtro profondita'", "moderate",
                  choices=[("mild", "Lieve"), ("moderate", "Moderato"), ("aggressive", "Aggressivo")]),
            Field("backend", "Motore", "auto", choices=[("auto", "Automatico"), ("cpu", "CPU (integrato)"),
                                                        ("colmap", "COLMAP (GPU NVIDIA)"), ("openmvs", "OpenMVS")]),
            Field("only_depth", "Solo le mappe di profondita' (per il modello fuso, senza nuvola densa)", False)]
            + self._version_fields("point_cloud"))
        if not v:
            return
        skip = self._photos_to_skip("Nuvola densa")
        if skip is None:
            return

        def job(p):
            self.chunk.build_depth_maps(v["quality"], v["filter_mode"], v["backend"],
                                        progress=p.sub(0, 1.0 if v["only_depth"] else 0.8), skip_unreadable=skip)
            if v["only_depth"]:
                return None
            return self.chunk.build_point_cloud(progress=p.sub(0.8, 0.2))

        if not v["only_depth"]:
            job = self._versioned("point_cloud", v, job)

        def done(pc):
            if pc is None:
                self.statusBar().showMessage("Mappe di profondita' pronte: Modello > Dati sorgente «Mappe di "
                                             "profondita'»", 15000)
            else:
                self._show_model("point_cloud")

        self.run_task("Nuvola densa" if not v["only_depth"] else "Mappe di profondita'", job, done)

    def _photos_to_skip(self, step: str, all_photos: bool = False) -> bool | None:
        """Before a step that reads the photos (the aligned ones, or every enabled one with
        ``all_photos``): when some cannot be read (a Dropbox folder kept online only), link
        them to another copy, go on without them or give up. Returns the ``skip_unreadable``
        flag for the step, ``None`` to give up."""
        while True:
            cams = [c for c in self.chunk.cameras if c.enabled] if all_photos else self.chunk.aligned_cameras
            bad = self.chunk.unreadable_photos(cams)
            if not bad:
                return False
            names = "\n".join(Path(c.path).name for c in bad[:5]) + ("\n..." if len(bad) > 5 else "")
            text = (f"{len(bad)} foto su {len(cams)} non si possono leggere:\n{names}\n\nUn file vuoto (0 byte) di "
                    "solito e' un file di Dropbox, iCloud o OneDrive tenuto «solo online»: rendi la cartella disponibile "
                    "offline, oppure collega le foto a un'altra copia (per esempio sul disco esterno).")
            every = len(bad) == len(cams)
            options = ["Ricollega a un'altra cartella..."] + ([] if every else ["Continua senza queste foto"])
            r = choice(self, "warning" if every else "question", f"{step}: foto non leggibili", text, options)
            if r is None:
                return None
            if r == 1:
                self.log_pane.warning(f"{step} senza {len(bad)} foto non leggibili")
                return True
            if not self.relink_photos():  # every photo found there: a complete copy replaces the folder
                return None

    def relink_photos(self, only_unreadable: bool = False) -> bool:
        """Point the photos to the files with the same names in another folder (and its
        subfolders): a copy on another disk, a folder moved or renamed."""
        if not self._need_chunk():
            return False
        folder = QFileDialog.getExistingDirectory(self, "Cartella con le foto (anche nelle sottocartelle)",
                                                  self._last_dir())
        if not folder:
            return False
        try:
            r = self.chunk.relink_photos(folder, only_unreadable=only_unreadable)
        except Exception as exc:
            self._exc("Ricollegamento delle foto non riuscito", exc)
            return False
        text = f"{r['relinked']} foto collegate a {folder}"
        if r["not_found"]:
            text += f"; {len(r['not_found'])} non trovate ({', '.join(r['not_found'][:3])}" + \
                ("...)" if len(r["not_found"]) > 3 else ")")
        if r["unreadable"]:
            text += f"; ancora {r['unreadable']} non leggibili"
        self.log_pane.info(text)
        self.statusBar().showMessage(text, 15000)
        self._dirty(True)
        if self.doc.path:
            try:
                self.doc.save()
            except Exception as exc:
                log.error("Salvataggio non riuscito: %s", exc)
        self.photos.set_chunk(self.chunk)
        return True

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

    # ---------------------------------------------------------------- confidence and cleaning
    def _has_depth_maps(self) -> bool:
        ch = self.chunk
        if ch is None or (ch.meta.get("dense") or {}).get("backend") == "openmvs":
            return False
        folder = ch.data_dir / "dense" / "stereo" / "depth_maps"
        return folder.exists() and any(folder.iterdir())

    def _no_depth_maps(self) -> None:
        self._warn("La confidenza si calcola dalle mappe di profondita': costruiscile con Flusso di lavoro > "
                   "Costruisci nuvola densa (con «Solo le mappe di profondita'» la nuvola attuale non cambia).")

    def _model_confidence(self):
        from ..engine.confidence import read_model_confidence

        path = self.chunk.product_path("model")
        return read_model_confidence(path) if path is not None and path.exists() else None

    def _cloud_confidence(self):
        from ..core.pointcloud import PointCloud

        path = self.chunk.product_path("point_cloud")
        try:
            return PointCloud.read_column(path, "confidence") if path is not None and path.exists() else None
        except Exception:
            return None

    @staticmethod
    def _confidence_text(hist: dict) -> str:
        from ..engine.confidence import BINS, bin_label

        total = max(sum(hist.values()), 1)
        parts = []
        for lo, hi in BINS:
            n = sum(v for c, v in hist.items() if lo <= c <= hi)
            if n:
                parts.append(f"{bin_label(lo, hi)}: {_it(n)} ({_it(100 * n / total, 1)}%)")
        return "; ".join(parts)

    def compute_confidence(self) -> None:
        """Confidence of the dense points and / or of the model vertices: how many depth maps
        confirm each of them (as Metashape)."""
        if not self._need_chunk(aligned=True):
            return
        ch = self.chunk
        targets = [(k, lab) for k, lab in (("point_cloud", "Nuvola densa"), ("model", "Modello (vertici)"))
                   if ch.product_path(k) and ch.product_path(k).exists()]
        if not targets:
            self._warn("Costruisci prima la nuvola densa o il modello.")
            return
        if not self._has_depth_maps():
            self._no_depth_maps()
            return
        v = ParamDialog.ask(self, "Calcola la confidenza", [
            Field("what", "Calcola per", [k for k, _ in targets], choices=targets, kind="checklist"),
            Field("tol", "Tolleranza di profondita' (% della distanza)", 1.0, minimum=0.1, maximum=10.0, decimals=1,
                  tip="Una mappa conferma un punto se la sua superficie e' a meno di questa frazione della distanza "
                      "dalla camera (1% come nella fusione delle mappe)")],
            intro="La confidenza di un punto (o di un vertice del modello) e' il numero di mappe di profondita' che "
                  "lo confermano, come in Metashape: valori bassi indicano rumore, riflessi, bordi del rilievo o "
                  "superfici inventate per chiudere un buco. Si vede nella vista 3D (Colori > Confidenza) e si usa "
                  "per pulire la nuvola e il modello.")
        if not v or not v["what"]:
            return

        def job(p):
            out = {}
            for i, k in enumerate(v["what"]):
                out[k] = ch.compute_confidence(k, tol=v["tol"] / 100.0,
                                               progress=p.sub(i / len(v["what"]), 1 / len(v["what"])))
            return out

        def done(res):
            names = dict(targets)
            for k, info in res.items():
                self.log_pane.info(f"Confidenza {names[k].lower()} (media {_it(info['mean'], 1)}): "
                                   + self._confidence_text(info["histogram"]))
            first = "point_cloud" if "point_cloud" in res else "model"
            self.show_confidence(first)
            self.statusBar().showMessage("Confidenza calcolata: nella legenda togli la spunta a una fascia per "
                                         "nasconderla; Pulisci toglie i punti o le facce sotto una soglia", 15000)

        self.run_task("Confidenza", job, done, refresh=False)

    def show_confidence(self, kind: str | None = None) -> None:
        """The dense cloud (or the model) coloured by confidence, with the legend of the ranges."""
        if not self._need_chunk():
            return
        ch = self.chunk
        kind = kind or ("point_cloud" if ch.product_path("point_cloud") else "model")
        if not ch.product_path(kind):
            self._warn("Costruisci prima la nuvola densa o il modello.")
            return
        self._show_model(kind)
        self.model_view.set_color_mode("confidence")

    def clean_cloud(self) -> None:
        """Remove noise from the dense cloud (Metashape's filters): low confidence, isolated points,
        small detached groups, classes, a colour. The points found are shown in red before."""
        if not self._need_chunk(product="point_cloud"):
            return
        ch = self.chunk
        conf = self._cloud_confidence()
        hist_text = ""
        if conf is not None:
            from ..engine.confidence import histogram

            hist_text = "\n\nConfidenza attuale - " + self._confidence_text(histogram(conf))
        choices, codes = self._cloud_class_choices(noise=True)
        noise = [c for c in codes if c in (7, 18)]
        fields = [
            Field("min_confidence", "Togli i punti con confidenza minore di (0 = no)", 3 if conf is not None else 0,
                  minimum=0, maximum=255,
                  tip="Numero di mappe di profondita' che confermano il punto: 2-3 toglie il rumore piu' evidente"),
            Field("statistical", "Togli i punti isolati (filtro statistico)", True),
            Field("neighbors", "    vicini considerati", 16, minimum=4, maximum=200),
            Field("std_ratio", "    soglia (deviazioni standard)", 2.0, minimum=0.5, maximum=10.0, decimals=1),
            Field("groups", "Togli i gruppi staccati dalla superficie", True),
            Field("group_points", "    punti minimi di un gruppo", 1000, minimum=2, maximum=100_000_000),
            Field("group_distance", "    distanza che separa i gruppi (m, 0 = automatica)", 0.0, minimum=0.0,
                  maximum=1000.0, decimals=3),
            Field("sparse", "Togli i punti senza vicini", False),
            Field("radius", "    raggio (m, 0 = automatico)", 0.0, minimum=0.0, maximum=1000.0, decimals=3),
            Field("min_neighbors", "    vicini minimi nel raggio", 4, minimum=1, maximum=1000)]
        if len(codes) > 1:
            fields.append(Field("classes", "Togli le classi", noise, choices=choices, kind="checklist"))
        fields += [Field("use_color", "Togli i punti di un colore (cielo, bordi neri)", False),
                   Field("color", "    colore", (135, 190, 235), kind="color"),
                   Field("color_tol", "    tolleranza (0-255)", 40, minimum=1, maximum=255)]
        fields += self._version_fields("point_cloud")
        v = ParamDialog.ask(self, "Pulisci la nuvola densa", fields,
                            intro=f"{_it(self._cloud_size())} punti. Prima vengono contati i punti da togliere e "
                                  "mostrati in rosso nella vista 3D; poi confermi." + hist_text)
        if not v:
            return
        filters = {}
        if v["min_confidence"]:
            filters["min_confidence"] = int(v["min_confidence"])
        if v["statistical"]:
            filters["statistical"] = (int(v["neighbors"]), float(v["std_ratio"]))
        if v["groups"]:
            filters["groups"] = (float(v["group_distance"]), int(v["group_points"]))
        if v["sparse"]:
            filters["sparse"] = (float(v["radius"]), int(v["min_neighbors"]))
        if v.get("classes"):
            filters["remove_classes"] = list(v["classes"])
        if v["use_color"]:
            filters["color"] = (tuple(v["color"]), float(v["color_tol"]))
        if not filters:
            self._warn("Scegli almeno un filtro.")
            return
        need_conf = "min_confidence" in filters and conf is None
        if need_conf and not self._has_depth_maps():
            self._no_depth_maps()
            return

        def analyse(p):
            from ..engine.cleaning import point_filters

            if need_conf:
                ch.compute_confidence("point_cloud", progress=p.sub(0.0, 0.4))
                p = p.sub(0.4, 0.6)
            pc = ch.point_cloud
            return len(pc), point_filters(ch, pc, progress=p, **filters)

        def confirm(result):
            from ..engine.cleaning import summarize

            n, masks = result
            info = summarize(masks, n)
            if not info["removed"]:
                self._info("Pulisci la nuvola densa", "Nessun punto da togliere con questi filtri.")
                return
            remove = np.zeros(n, bool)
            for m in masks.values():
                remove |= m
            names = {"confidence": "confidenza bassa", "statistical": "isolati", "groups": "gruppi staccati",
                     "sparse": "senza vicini", "classes": "classi", "color": "colore"}
            parts = ", ".join(f"{names.get(k, k)} {_it(c)}" for k, c in info["by_filter"].items() if c)
            self._show_model("point_cloud")
            self.model_view.highlight = remove  # shown in red while the question is open
            self.model_view.refresh(reset=False)
            r = choice(self, "question", "Pulisci la nuvola densa",
                       f"Da togliere: {_it(info['removed'])} punti su {_it(info['total'])} "
                       f"({_it(100 * info['removed'] / n, 2)}%) - {parts}.\n\nSono mostrati in rosso nella vista 3D.",
                       ["Togli i punti"])
            self.model_view.highlight = None
            if r != 0:
                self.model_view.refresh(reset=False)
                return

            def apply(p):
                from ..engine.cleaning import delete_points

                p(0.3, "Salvataggio della nuvola pulita")
                out = delete_points(ch, remove)
                source = ch.meta.get("point_cloud_source") or ""
                if "pulita" not in source:
                    ch.meta["point_cloud_source"] = (source + ", pulita").lstrip(", ")
                ch.meta["point_cloud_cleaning"] = info
                return out

            self.run_task("Pulizia della nuvola", self._versioned_edit("point_cloud", v, apply, "Nuvola pulita"),
                          lambda out: (self.log_pane.info(f"Nuvola pulita: tolti {_it(out['removed'])} punti, restano "
                                                          f"{_it(out['kept'])} ({parts})"),
                                       self._show_model("point_cloud")))

        self.run_task("Analisi della nuvola", analyse, confirm, refresh=False)

    def _cloud_size(self) -> int:
        from ..io.mesh_ply import read_header

        path = self.chunk.product_path("point_cloud")
        try:
            with open(path, "rb") as f:
                return next(e["count"] for e in read_header(f)[1] if e["name"] == "vertex")
        except Exception:
            return 0

    def clean_model(self) -> None:
        """Metashape's model cleaning: detached pieces, low-confidence faces, stretched triangles,
        topology, holes, smoothing, decimation."""
        if not self._need_chunk(product="model"):
            return
        ch = self.chunk
        conf = self._model_confidence()
        from ..io.mesh_ply import face_count

        n_faces = face_count(ch.product_path("model")) or 0
        v = ParamDialog.ask(self, "Pulisci il modello", [
            Field("min_component", "Togli le parti staccate sotto il ...% della parte piu' grande (0 = no)", 1.0,
                  minimum=0.0, maximum=100.0, decimals=2),
            Field("min_confidence", "Togli le facce con vertici di confidenza minore di (0 = no)",
                  1 if conf is not None else 0, minimum=0, maximum=255,
                  tip="1 toglie le superfici che nessuna mappa di profondita' ha visto (buchi chiusi dal modello)"),
            Field("max_edge", "Togli i triangoli stirati: lato oltre N volte la mediana (0 = no)", 0.0, minimum=0.0,
                  maximum=1000.0, decimals=1),
            Field("repair", "Ripara la topologia (facce degeneri e doppie)", True),
            Field("fill_holes", "Chiudi i buchi fino a un raggio di (m, 0 = no)", 0.0, minimum=0.0, maximum=1000.0,
                  decimals=3),
            Field("smooth", "Levigatura (passi, 0 = no; i bordi restano fermi)", 0, minimum=0, maximum=100),
            Field("target_faces", "Semplifica a un numero di facce (0 = no)", 0, minimum=0, maximum=2_000_000_000)]
            + self._version_fields("model"),
            intro=f"Modello di {_it(n_faces)} facce"
                  + ("; confidenza dei vertici calcolata." if conf is not None else
                     "; la confidenza dei vertici verra' calcolata dalle mappe di profondita' se usi quel filtro.")
                  + " La texture va ricostruita dopo la pulizia.")
        if not v:
            return
        need_conf = v["min_confidence"] > 0 and conf is None
        if need_conf and not self._has_depth_maps():
            self._no_depth_maps()
            return
        kw = {"min_component": v["min_component"] / 100.0 or None, "min_confidence": int(v["min_confidence"]) or None,
              "max_edge": v["max_edge"] or None, "repair": v["repair"], "fill_holes": v["fill_holes"] or None,
              "smooth": int(v["smooth"]), "target_faces": int(v["target_faces"]) or None}

        def job(p):
            if need_conf:
                ch.compute_confidence("model", progress=p.sub(0.0, 0.5))
                p = p.sub(0.5, 0.5)
            return ch.clean_model(progress=p, **kw)

        def done(info):
            names = {"confidence": "confidenza bassa", "components": "parti staccate", "long_edges": "triangoli stirati",
                     "topology": "topologia"}
            parts = ", ".join(f"{names.get(k, k)} {_it(c)}" for k, c in info["by_filter"].items() if c)
            text = (f"Modello pulito: {_it(info['faces'])} facce (prima {_it(info['faces_before'])})"
                    + (f"; tolte {parts}" if parts else "")
                    + (f"; {_it(info['filled_faces'])} facce per chiudere i buchi" if info.get("filled_faces") else ""))
            self.log_pane.info(text)
            self.statusBar().showMessage(text, 15000)
            self._show_model("model")

        self.run_task("Pulizia del modello", self._versioned_edit("model", v, job, "Modello pulito"), done)

    def clean_tie_points(self) -> None:
        """Gradual selection of the tie points: thresholds with live counts, removal, optimization."""
        if not self._need_chunk(aligned=True):
            return
        ch = self.chunk
        if not ch.product_path("reconstruction"):
            self._warn("Il chunk non ha un modello di allineamento (punti di legame).")
            return

        def measure(p):
            from ..engine.tiepoints import tie_point_metrics

            p(0.1, "Misura dei punti di legame")
            return tie_point_metrics(ch)

        def choose(metrics):
            from PySide6.QtWidgets import QDialog

            from ..engine.tiepoints import remove_tie_points, select_tie_points

            d = GradualSelectionDialog(metrics, lambda **kw: select_tie_points(ch, metrics=metrics, **kw), self)
            if d.exec() != QDialog.DialogCode.Accepted or d.selection is None:
                return
            vals, sel = d.values(), d.selection

            def job(p):
                return remove_tie_points(ch, sel["ids"], optimize=vals["optimize"], progress=p)

            def done(out):
                text = (f"Punti di legame: tolti {_it(out['removed'])}, restano {_it(out['remaining'])}; errore di "
                        f"riproiezione {_it(out['reprojection_error_before'], 3)} -> "
                        f"{_it(out['reprojection_error_after'], 3)} px")
                if "optimize_error" in out:
                    text += f"; dopo l'ottimizzazione {_it(out['optimize_error'], 3)} px"
                self.log_pane.info(text)
                self.statusBar().showMessage(text, 15000)
                self._show_model("tie_points")

            self.run_task("Pulizia dei punti di legame", job, done)

        self.run_task("Misura dei punti di legame", measure, choose, refresh=False, save=False)

    def lasso_edit(self) -> None:
        """The lasso in the 3D view on the dense cloud (or the model when it is shown)."""
        if not self._need_chunk():
            return
        kind = self.model_view.kind if self.model_view.kind in ("point_cloud", "model") else None
        kind = kind or ("point_cloud" if self.chunk.product_path("point_cloud") else
                        "model" if self.chunk.product_path("model") else None)
        if kind is None:
            self._warn("Costruisci prima la nuvola densa o il modello.")
            return
        if self.model_view.kind != kind or self.model_view.paths:
            self._show_model(kind)
        self.show_view(self.model_view)
        self.model_view.start_lasso()

    def _versioned_edit(self, product: str, v: dict, job, name: str):
        """``job(p)`` changing the active ``product`` in place, after keeping a copy of it as a
        version when asked (dropped again if the job fails: nothing was changed)."""
        from ..core import versions

        chunk = self.chunk
        keep = v.get("keep") == "keep" or v.get("keep_copy") is True
        label = (v.get("version_name") or "").strip() or name

        def wrapped(p):
            kept = versions.keep_active(chunk, product, move=False) if keep else None
            try:
                result = job(p)
            except BaseException:
                if kept is not None:
                    versions.delete(chunk, product, kept["id"])
                raise
            versions.set_active_name(chunk, product, label)
            return result

        return wrapped

    # ---------------------------------------------------------------- crop with a polygon
    def start_crop(self) -> None:
        """The crop tool on the orthophoto or the DEM: drag a box or click a polygon."""
        if not self._need_chunk():
            return
        ch = self.chunk
        if self.ortho_view.tr is None:
            if ch.orthomosaic_path is not None:
                self._show_raster("ortho")
            elif ch.dem_path is not None:
                self._show_raster("dem")
            else:
                self._warn("Il ritaglio si disegna sull'ortofoto o sul DEM: costruiscine uno prima.")
                return
        self.set_draw_mode("crop")

    def _selected_polygon(self):
        """The polygons selected in the orthophoto view, merged (a unit, a section of a layer)."""
        from shapely.geometry import Polygon
        from shapely.ops import unary_union

        polys = []
        for layer, _i, attrs in self.ortho_view.selected_features():
            if self.ortho_view.layers.get(layer, {}).get("kind") != "polygon":
                continue
            u = attrs.get("_unit")
            if isinstance(u, int) and 0 <= u < len(self.units) and getattr(self.units[u], "geometry", None) is not None:
                polys.append(self.units[u].geometry)
            elif attrs.get("_geom") and attrs["_geom"][0] == "Polygon" and len(attrs["_geom"][1]) >= 3:
                polys.append(Polygon(attrs["_geom"][1]))
        polys = [g for g in polys if g is not None and not g.is_empty]
        return unary_union(polys) if polys else None

    def crop_with_selection(self) -> None:
        geom = self._selected_polygon()
        if geom is None:
            self._warn("Seleziona un poligono (strumento Seleziona: per esempio un'unita' stratigrafica) oppure "
                       "disegnalo con lo strumento Ritaglio.")
            return
        self.crop_products(geom)

    def _crop_drawn(self, xy) -> None:
        from shapely.geometry import Polygon

        geom = Polygon(np.asarray(xy, float)[:, :2])
        if not geom.is_valid or geom.area <= 0:
            self._warn("Il poligono di ritaglio non e' valido (i lati si incrociano o non ha area): ridisegnalo.")
            return
        self.ortho_view.remove_layer(CROP_LAYER)
        self.ortho_view.add_polygon(np.asarray(geom.exterior.coords)[:-1], color="#00b0ff", fill_alpha=25,
                                    layer=CROP_LAYER)
        self.crop_products(geom)

    def _dem_range(self) -> tuple[float, float]:
        """Lowest and highest DEM value (from an overview: quick), or 0, 0."""
        path = self.chunk.dem_path if self.chunk is not None else None
        if path is None or not path.exists():
            return 0.0, 0.0
        try:
            import rasterio

            with rasterio.open(path) as src:
                f = max(1, max(src.width, src.height) // 1024)
                z = src.read(1, out_shape=(max(1, src.height // f), max(1, src.width // f)), masked=True)
            z = z.compressed() if np.ma.isMaskedArray(z) else np.ravel(z)
            z = z[np.isfinite(z)]
            return (float(z.min()), float(z.max())) if len(z) else (0.0, 0.0)
        except Exception:
            return 0.0, 0.0

    def crop_products(self, geom) -> None:
        """Export the products cut by a polygon, or cut them in the project (the originals kept as
        versions). Files of other programs are never changed."""
        if not self._need_chunk():
            return
        ch = self.chunk
        avail = [(k, lab) for k, lab in CROP_PRODUCTS if ch.product_path(k) and ch.product_path(k).exists()]
        if not avail:
            self._warn("Non ci sono prodotti da ritagliare (DEM, ortofoto, modello o nuvola densa).")
            return
        x0, y0, x1, y1 = geom.bounds
        zlo, zhi = self._dem_range()
        v = ParamDialog.ask(self, "Ritaglia con un poligono", [
            Field("what", "Prodotti", [k for k, _ in avail], choices=avail, kind="checklist"),
            Field("action", "Cosa fare", "export",
                  choices=[("export", "Esporta in una cartella"), ("project", "Ritaglia nel progetto")],
                  tip="Esportando, il progetto non cambia; ritagliando nel progetto gli originali possono restare "
                      "come versioni"),
            Field("folder", "Cartella per l'esportazione", str(Path(self._last_dir()) / "ritagli"), kind="dir"),
            Field("name", "Inizio del nome dei file", "ritaglio", kind="text"),
            Field("model_format", "Formato del modello", "ply",
                  choices=[("ply", "PLY (colori e confidenza)"), ("obj", "OBJ (con la texture)")],
                  tip="PLY con coordinate in doppia precisione; OBJ con la texture se e' stata costruita"),
            Field("cloud_format", "Formato della nuvola", "las",
                  choices=[("las", "LAS (classi e confidenza)"), ("ply", "PLY")]),
            Field("use_z", "Limita anche le quote (modello e nuvola)", False),
            Field("zmin", "    quota minima", round(zlo, 3), minimum=-1e6, maximum=1e6, decimals=3),
            Field("zmax", "    quota massima", round(zhi, 3), minimum=-1e6, maximum=1e6, decimals=3),
            Field("keep", "Nel progetto: conserva gli originali come versioni", True)],
            intro=(f"Poligono di {_it(geom.area, 2)} m2 ({_it(x1 - x0, 2)} x {_it(y1 - y0, 2)} m). DEM e ortofoto "
                   "tengono la finestra del poligono, vuota (trasparente) fuori; il modello le facce con il centro "
                   "dentro; la nuvola i punti dentro."))
        if not v:
            return
        if not v["what"]:
            self._warn("Scegli almeno un prodotto.")
            return
        zmin, zmax = (v["zmin"], v["zmax"]) if v["use_z"] else (None, None)
        if zmin is not None and zmax is not None and zmin >= zmax:
            self._warn("La quota minima deve essere minore della massima.")
            return
        names = dict(CROP_PRODUCTS)
        if v["action"] == "export":
            import re

            folder = Path(v["folder"] or Path(self._last_dir()) / "ritagli")
            stem = re.sub(r"[^\w.-]+", "_", (v["name"] or "ritaglio").strip()) or "ritaglio"
            ext = {"dem": "_dem.tif", "orthomosaic": "_ortofoto.tif", "model": f"_modello.{v['model_format']}",
                   "point_cloud": f"_nuvola.{v['cloud_format']}"}
            targets = {k: folder / f"{stem}{ext[k]}" for k in v["what"]}
            existing = [t.name for t in targets.values() if t.exists()]
            if existing and choice(self, "question", "Ritaglia con un poligono",
                                   f"Nella cartella ci sono gia': {', '.join(existing)}. Sovrascriverli?",
                                   ["Sovrascrivi"]) != 0:
                return

            def job(p):
                import json

                folder.mkdir(parents=True, exist_ok=True)
                out = []
                for i, k in enumerate(v["what"]):
                    p(i / len(v["what"]), f"Ritaglio: {names[k]}")
                    if k == "dem":
                        out.append(ch.export_dem(targets[k], region=geom))
                    elif k == "orthomosaic":
                        out.append(ch.export_orthomosaic(targets[k], region=geom))
                    elif k == "model":
                        out.append(ch.export_model(targets[k], region=geom, zmin=zmin, zmax=zmax))
                    else:
                        out.append(ch.export_point_cloud(targets[k], region=geom, zmin=zmin, zmax=zmax))
                from shapely.geometry import mapping

                poly = folder / f"{stem}_poligono.geojson"
                poly.write_text(json.dumps({"type": "FeatureCollection", "features": [
                    {"type": "Feature", "properties": {"nome": stem, "crs": ch.crs}, "geometry": mapping(geom)}]}))
                out.append(poly)
                return out

            def done(paths):
                for path in paths:
                    self.log_pane.info(f"Esportato {path}")
                self.statusBar().showMessage(f"Ritagli esportati in {folder}", 15000)
                self._info("Ritaglia con un poligono", f"File scritti in {folder}:\n" +
                           "\n".join(Path(x).name for x in paths))

            self.run_task("Esportazione dei ritagli", job, done, refresh=False, save=False)
            return

        def job(p):
            from ..core import versions

            done = []
            for i, k in enumerate(v["what"]):
                p(i / len(v["what"]), f"Ritaglio: {names[k]}")
                kept = versions.keep_active(ch, k, move=False) if v["keep"] else None
                try:
                    if k == "dem":
                        ch.crop_dem(geom)
                    elif k == "orthomosaic":
                        ch.crop_orthomosaic(geom)
                    elif k == "model":
                        ch.crop_model(geom, zmin=zmin, zmax=zmax)
                    else:
                        ch.crop_point_cloud(region=geom, zmin=zmin, zmax=zmax)
                except BaseException:
                    if kept is not None:
                        versions.delete(ch, k, kept["id"])
                    raise
                versions.set_active_name(ch, k, "Ritagliato")
                done.append(k)
            return done

        def finished(products):
            text = "Ritagliati nel progetto: " + ", ".join(names[k] for k in products)
            if v["keep"]:
                text += " (gli originali sono tra le versioni: tasto destro nell'area di lavoro)"
            self.log_pane.info(text)
            self.statusBar().showMessage(text, 15000)

        self.run_task("Ritaglio dei prodotti", job, finished)

    def build_model(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        dense = self.chunk.meta.get("dense") or {}
        depth_maps = bool(dense) and dense.get("backend") != "openmvs" and (self.chunk.data_dir / "dense").exists()
        sources = ([("depth_maps", "Mappe di profondita' (fusione, come Metashape)")] if depth_maps else []) + \
            [("point_cloud", "Nuvola densa"), ("tie_points", "Punti di legame")]
        src = "depth_maps" if depth_maps else ("point_cloud" if self.chunk.product_path("point_cloud") else "tie_points")
        v = ParamDialog.ask(self, "Modello 3D", [
            Field("source", "Dati sorgente", src, choices=sources),
            Field("surface", "Tipo di superficie", "arbitrary",
                  choices=[("arbitrary", "Arbitraria (3D)"), ("height_field", "Campo di altezze (2.5D, terreno)")]),
            Field("face_count", "Numero di facce", "medium",
                  choices=[("high", "Alto"), ("medium", "Medio"), ("low", "Basso")]),
            Field("voxel", "Voxel per le mappe di profondita' (m, 0 = automatico)", 0.0, minimum=0.0, maximum=100.0,
                  decimals=4, tip="Lato del voxel della fusione: 0 = due volte il pixel delle mappe sulla superficie")]
            + self._class_fields("Classi (solo dalla nuvola densa)") + self._version_fields("model"),
            intro=("Con le mappe di profondita' la superficie nasce dalla fusione volumetrica delle mappe (TSDF), "
                   "senza passare dalla nuvola densa: piu' fedele e con i colori delle foto." if depth_maps else
                   "Costruisci le mappe di profondita' (Nuvola densa) per avere anche la fusione delle mappe.")
            + self._class_hint("Dalla nuvola densa puoi togliere delle classi (vegetazione, attrezzi): la superficie "
                               "si richiude sopra quello che manca."))
        if v:
            kw = {"voxel_size": v["voxel"] or None} if v["source"] == "depth_maps" else {}
            classes = self._chosen_classes(v)
            if classes is not None and v["source"] != "point_cloud":
                self.log_pane.warning("Le classi valgono solo per il modello dalla nuvola densa: uso tutti i dati.")
                classes = None
            if classes == []:
                self._warn("Scegli almeno una classe.")
                return
            self.run_task("Modello", self._versioned("model", v, lambda p: self.chunk.build_model(
                v["source"], v["surface"], v["face_count"], classes=classes, progress=p, **kw)),
                lambda _m: self._show_model("model"))

    def build_texture(self) -> None:
        if not self._need_chunk(aligned=True, product="model"):
            return
        v = ParamDialog.ask(self, "Texture", [Field("size", "Dimensione texture", 4096,
                                                    choices=[(2048, "2048"), (4096, "4096"), (8192, "8192")])])
        if not v:
            return
        skip = self._photos_to_skip("Texture")
        if skip is None:
            return
        self.run_task("Texture", lambda p: self.chunk.build_texture(v["size"], progress=p, skip_unreadable=skip),
                      lambda _r: self._show_model("model_textured"))

    def build_dem(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        v = ParamDialog.ask(self, "DEM", [
            Field("source", "Dati sorgente", "point_cloud" if self.chunk.product_path("point_cloud") else "model",
                  choices=[("point_cloud", "Nuvola densa"), ("model", "Modello"), ("tie_points", "Punti di legame")]),
            Field("resolution", "Risoluzione (m/pixel)", None, kind="optional_float"),
            Field("interpolate", "Interpola i vuoti", True)] + self._class_fields("Classi (solo dalla nuvola densa)")
            + self._version_fields("dem"),
            intro=self._class_hint("Con la nuvola densa scegli le classi: solo Terreno da' il modello del terreno (DTM), "
                                   "senza vegetazione, persone o attrezzi; i vuoti che lasciano vengono interpolati. "
                                   "Curve di livello e volumi useranno questo DEM."))
        if v:
            classes = self._chosen_classes(v) if v["source"] == "point_cloud" else None
            if classes == []:
                self._warn("Scegli almeno una classe.")
                return
            self.run_task("DEM", self._versioned("dem", v, lambda p: self.chunk.build_dem(
                v["resolution"], v["source"], v["interpolate"], classes=classes, progress=p)),
                lambda _r: self._show_raster("dem"))

    def build_ortho(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        if not (self.chunk.dem_path or self.chunk.product_path("model")):
            self._warn("Costruisci prima il DEM o il modello.")
            return
        choices, _codes = self._cloud_class_choices()
        v = ParamDialog.ask(self, "Ortofoto", [
            Field("surface", "Superficie", "dem" if self.chunk.dem_path else "model",
                  choices=[("dem", "DEM"), ("model", "Modello (true ortho)")]),
            Field("blending", "Fusione", "mosaic", choices=[("mosaic", "Mosaico"), ("average", "Media")]),
            Field("resolution", "Risoluzione (m/pixel)", None, kind="optional_float"),
            Field("max_angle", "Angolo massimo di vista dalla verticale (gradi)", 70.0, minimum=10.0, maximum=90.0,
                  decimals=0, tip="Dove tutte le foto guardano piu' di traverso la cella resta trasparente: viste "
                                  "radenti (foto oblique ai bordi del rilievo) stirano la foto sulla superficie")]
            + ([Field("hide", "Classi da rendere trasparenti", [], choices=choices, kind="checklist")]
               if len(choices) > 1 else []) + self._version_fields("orthomosaic"),
            intro=self._class_hint("Le classi scelte (attrezzi, persone, vegetazione) diventano trasparenti dove sono "
                                   "la superficie visibile. Su un DEM del solo terreno l'ortofoto e' quella classica."))
        if not v:
            return
        skip = self._photos_to_skip("Ortofoto")
        if skip is None:
            return
        hide = sorted(v.get("hide") or []) or None
        self.run_task("Ortofoto", self._versioned("orthomosaic", v, lambda p: self.chunk.build_orthomosaic(
            v["resolution"], v["surface"], v["blending"], hide_classes=hide, progress=p, skip_unreadable=skip,
            max_angle=v.get("max_angle", 70.0))),
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
        skip = self._photos_to_skip("Elabora tutto", all_photos=True)
        if skip is None:
            return
        if skip:  # the photos that cannot be read are not aligned at all
            bad = {c.key for c in self.chunk.unreadable_photos([c for c in self.chunk.cameras if c.enabled])}
            for c in self.chunk.cameras:
                if c.key in bad:
                    c.enabled = False

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
            ch.build_depth_maps(v["dense_quality"], progress=q.sub(0, 0.8), skip_unreadable=skip)
            ch.build_point_cloud(progress=q.sub(0.8, 0.2))
            ch.build_model(surface=v["surface"], progress=sp())
            if v["texture"]:
                ch.build_texture(progress=sp(), skip_unreadable=skip)
            if v["dem"]:
                ch.build_dem(progress=sp())
            if v["ortho"]:
                ch.build_orthomosaic(progress=sp(), skip_unreadable=skip)
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
        elif kind == "version":
            self.show_version(*payload)
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

    def _thumbs_progress(self, done: int, total: int) -> None:
        if total >= 30:  # a few photos load at once: no message
            self.statusBar().showMessage(f"Miniature delle foto: {done}/{total}" if done < total
                                         else f"Miniature delle foto pronte ({total})", 3000)

    def _show_coords(self, x: float, y: float, z) -> None:
        label = "dZ" if getattr(self.ortho_view, "kind", None) == "diff" else "Z"
        zt = f"  {label} {z:+.3f}" if z is not None and label == "dZ" else (f"  Z {z:.3f}" if z is not None else "")
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
        self._quota_keep = None
        if mode != "sam":
            self._sam_cancel()
        if mode == "sam" and not self._sam_available(then=lambda: self.set_draw_mode("sam")):
            self.draw_actions["pan"].setChecked(True)
            self.ortho_view.set_mode("pan")
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
                 "quota": "Clic su un punto: la quota viene letta dal DEM, poi area, US e gli altri campi di "
                          "pyarchinit_quote",
                 "crop": "Trascina un riquadro, oppure clic per i vertici e doppio clic per chiudere il poligono: poi "
                         "scegli i prodotti da esportare ritagliati o da ritagliare nel progetto",
                 "sam": self._sam_hint()}
        self.statusBar().showMessage(hints.get(mode, ""), 20000)

    def clear_drawings(self) -> None:
        self.units = []
        self.quote = []
        self._sam_cancel()
        self.line_layers = {}
        self.ortho_view.clear_overlays()
        self._draw_sections()
        self._dirty(True)

    # ---------------------------------------------------------------- drawings saved with the project
    def _store_drawings(self) -> None:
        """Units drawn here (not those loaded from PyArchInit) and spot heights go into the
        active chunk, so they come back when the project is reopened."""
        ch = self.chunk
        if ch is None:
            return
        units = [{**u.to_dict(), "layer": (u.attributes or {}).get("_layer")} for u in self.units
                 if not str((u.attributes or {}).get("_layer") or "").startswith("US da PyArchInit - ")]
        quote = [{k: v for k, v in q.items() if not str(k).startswith("_")} for q in self.quote]
        if units or quote:
            ch.meta["drawings"] = {"units": units, "quote": quote}
        else:
            ch.meta.pop("drawings", None)

    def _restore_drawings(self) -> None:
        from ..archaeo.stratigraphy import UnitPolygon

        ch = self.chunk
        data = (ch.meta.get("drawings") or {}) if ch is not None else {}
        self.units, self.quote = [], []
        for d in data.get("units", []):
            try:
                u = UnitPolygon.from_dict(d)
                if d.get("layer"):
                    u.attributes["_layer"] = d["layer"]
                self.units.append(u)
            except Exception as exc:  # a broken record must not stop the project from opening
                log.warning("Unita' non letta dal progetto: %s", exc)
        self.quote = [dict(q) for q in data.get("quote", []) if "x" in q and "y" in q]

    def _redraw_drawings(self) -> None:
        self._redraw_overlays()

    def _add_line_feature(self, layer: str, coords, color: str, label: str = "", attrs: dict | None = None,
                          width: float = 2.0) -> None:
        """A line on the raster view that is kept across DEM/orthomosaic reloads."""
        coords = np.asarray(coords, float)[:, :2]
        self.line_layers.setdefault(layer, []).append((coords, color, label, dict(attrs or {}), width))
        if self.ortho_view.tr is not None:
            self.ortho_view.add_line(coords, color, label, layer=layer, attrs=attrs, width=width)

    def _us_choices(self) -> list:
        if self.pa is None or not self.project_site():
            return []
        try:
            return [(str(u.get("area")), str(u.get("us")), str(u.get("d_stratigrafica") or ""))
                    for u in self.pa.stratigraphic_units(self.project_site())]
        except Exception:
            return []

    def _unit_defaults(self) -> dict:
        d = dict(getattr(self, "_last_unit", {}) or {})
        d.setdefault("disegnatore", self.settings.value("pa/disegnatore", "") or "")
        d.pop("us", None)  # a new unit has a new number
        return d

    def _ask_unit(self, refine: bool = False, title: str = "Unita' stratigrafica"):
        """The unit dialog with the last values; returns (result, values)."""
        dlg = UnitDialog(self, self._us_choices(), sito=self.project_site(), defaults=self._unit_defaults(),
                         refine=refine, title=title)
        r = dlg.exec()
        v = dlg.values() if r else {}
        if r == 1 or (refine and r == UnitDialog.REFINE):
            keep = {k: v.get(k) for k in ("area", "tipo", "stratigraph_index_us", "tipo_us_s", "disegnatore", "data",
                                          "rilievo_originale") if v.get(k) is not None}
            self._last_unit = keep
            if v.get("disegnatore"):
                self.settings.setValue("pa/disegnatore", v["disegnatore"])
        return r, v

    def _make_unit(self, geometry, v: dict):
        from ..archaeo.stratigraphy import UNIT_FIELDS, UnitPolygon

        extra = {k: v[k] for k in UNIT_FIELDS if v.get(k) not in (None, "")}
        return UnitPolygon(geometry, us=v.get("us") or None, area=v.get("area") or None, tipo=v.get("tipo") or "US",
                           attributes=extra, sito=self.project_site() or None)

    def _add_unit(self, u) -> None:
        self.units.append(u)
        self._draw_unit(u, len(self.units) - 1)
        self._dirty(True)
        self.log_pane.info(f"{u.tipo} {u.us or '?'}: area {u.geometry.area:.3f} m2"
                           + (f" (sito {u.sito})" if u.sito else ""))

    def _unit_drawn(self, xy) -> None:
        from shapely.geometry import Polygon

        self._ensure_site("Le US disegnate appartengono a un sito: sceglilo una volta per il progetto.")
        r, v = self._ask_unit()
        if r != 1:
            return
        self._add_unit(self._make_unit(Polygon(xy).buffer(0), v))

    def _draw_unit(self, u, i: int, layer: str | None = None) -> None:
        if u.attributes is None:
            u.attributes = {}
        layer = layer or u.attributes.get("_layer") or "Unita' stratigrafiche"
        u.attributes["_layer"] = layer  # survives raster reloads (DEM <-> orthomosaic)
        geoms = getattr(u.geometry, "geoms", [u.geometry])
        # the columns of pyunitastratigrafiche, as in PyArchInit's layer
        attrs = {**u.layer_attributes(), "area_m2": round(u.geometry.area, 3),
                 **{k: v for k, v in (u.attributes or {}).items()
                    if k not in ("gid",) and not k.startswith("_") and k not in u.layer_attributes()},
                 "_unit": i}
        if (u.attributes or {}).get("gid") is not None:
            attrs["_gid"] = u.attributes["gid"]
        for g in geoms:
            self.ortho_view.add_polygon(np.asarray(g.exterior.coords)[:, :2], f"{u.tipo} {u.us or '?'}",
                                        US_COLORS[i % len(US_COLORS)], layer=layer, attrs=attrs)

    # ---------------------------------------------------------------- spot heights (pyarchinit_quote)
    def _draw_quota(self, q: dict, i: int) -> None:
        from ..archaeo.stratigraphy import QUOTE_FIELDS

        attrs = {k: q.get(k, "") for k in QUOTE_FIELDS}
        attrs["_quota"] = i
        if q.get("gid") is not None:
            attrs["_gid"] = q["gid"]
        z = q.get("quota_q")
        label = f"{float(z):.2f}" if z not in (None, "") else "?"
        self.ortho_view.add_point(float(q["x"]), float(q["y"]), "#ffd166", label, layer=QUOTE_LAYER, attrs=attrs)

    def _quota_clicked(self, x: float, y: float) -> None:
        """A spot height at the clicked point: elevation from the DEM, then the columns of
        pyarchinit_quote (asked once; "non chiedere piu'" keeps the values for the next points)."""
        z = self._dem_value(x, y)
        keep = getattr(self, "_quota_keep", None)
        if keep is not None:
            v = dict(keep)
            if z is None:
                self.statusBar().showMessage("Fuori dal DEM: quota non letta", 8000)
                return
            v["quota_q"] = round(z, 3)
        else:
            self._ensure_site("Le quote appartengono a un sito: sceglilo una volta per il progetto.")
            defaults = dict(getattr(self, "_last_quota", {}) or {})
            defaults.setdefault("disegnatore", self.settings.value("pa/disegnatore", "") or "")
            dlg = QuotaDialog(self, self._us_choices(), sito=self.project_site(), quota=z, defaults=defaults)
            if dlg.exec() != dlg.DialogCode.Accepted:
                return
            v = dlg.values()
            self._last_quota = {k: val for k, val in v.items() if k not in ("quota_q", "keep")}
            if v.pop("keep", False):
                self._quota_keep = dict(self._last_quota)
                self.statusBar().showMessage("Quote: valori fissati per i prossimi punti (Esc o un altro "
                                             "strumento per tornare a chiederli)", 15000)
            if v.get("disegnatore"):
                self.settings.setValue("pa/disegnatore", v["disegnatore"])
        self._add_quota({"x": float(x), "y": float(y), "sito_q": self.project_site(), **v})

    def _add_quota(self, q: dict) -> None:
        q.pop("keep", None)
        self.quote.append(q)
        self._draw_quota(q, len(self.quote) - 1)
        self._dirty(True)
        self.log_pane.info(f"Quota {q.get('quota_q')} {q.get('unita_misu_q', '')} - US {q.get('us_q') or '?'} "
                           f"(area {q.get('area_q') or '?'}) in X {q['x']:.3f} Y {q['y']:.3f}")

    def _dem_value(self, x: float, y: float) -> float | None:
        """Elevation of the DEM at full resolution (the view keeps a reduced copy)."""
        ch = self.chunk
        if ch is None or not ch.dem_path:
            return self.ortho_view.elevation(x, y)
        try:
            import rasterio

            with rasterio.open(ch.dem_path) as src:
                val = float(next(src.sample([(x, y)]))[0])
                if (src.nodata is not None and val == src.nodata) or not np.isfinite(val):
                    return None
                return val
        except Exception:
            return self.ortho_view.elevation(x, y)

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
        with self.ortho_view.batch():
            for sdict in self.sections:
                self.ortho_view.add_line([sdict["p1"], sdict["p2"]], "#ffd000", sdict["label"], layer="Sezioni",
                                         attrs={"lunghezza_m": round(sdict.get("length", 0), 3),
                                                "svg": sdict.get("svg", "")})

    def _section_options(self) -> dict:
        return dict((self.chunk.meta.get("section_options") or {}) if self.chunk is not None else {})

    def section_options(self) -> None:
        """How the next sections are cut: model or dense cloud, band of points, classes."""
        if not self._need_chunk():
            return
        cur = self._section_options()
        choices, codes = self._cloud_class_choices()
        v = ParamDialog.ask(self, "Opzioni delle sezioni", [
            Field("source", "Taglia", cur.get("source", "model"),
                  choices=[("model", "Il modello 3D (profilo continuo)"), ("point_cloud", "La nuvola densa (punti)")]),
            Field("thickness", "Fascia di punti attorno alla traccia (m)", float(cur.get("thickness", 0.02)),
                  minimum=0.001, maximum=10.0, decimals=3)]
            + ([Field("classes", "Classi", cur.get("classes") or codes, choices=choices, kind="checklist")]
               if len(codes) > 1 else []),
            intro="Valgono per le prossime sezioni. Togliendo delle classi (vegetazione, attrezzi) la sezione mostra "
                  "solo cio' che resta: dal modello restano i tratti che poggiano su punti delle classi scelte.")
        if not v:
            return
        classes = self._chosen_classes(v)
        if classes == []:
            self._warn("Scegli almeno una classe.")
            return
        self.chunk.meta["section_options"] = {"source": v["source"], "thickness": float(v["thickness"]),
                                              "classes": classes}
        self._dirty(True)
        self.statusBar().showMessage("Opzioni delle sezioni salvate: traccia la sezione sull'ortofoto", 8000)

    def _section_drawn(self, a, b) -> None:
        opts = self._section_options()
        source = opts.get("source", "model")
        if not self._need_chunk(product="model" if source == "model" else "point_cloud"):
            return
        from ..archaeo import sections

        out_dir = self._sections_dir()
        n = len(self.sections) + 1
        while (out_dir / f"sezione_{n}.svg").exists():
            n += 1
        label = f"Sezione {n}"

        def job(p):
            p(0.1, "Taglio del modello" if source == "model" else "Punti della nuvola lungo la traccia")
            sec = sections.section(self.chunk, a, b, label=label, source=source,
                                   thickness=float(opts.get("thickness", 0.02)), classes=opts.get("classes"))
            if not sec.polylines:
                raise RuntimeError("la sezione e' vuota: nessun dato delle classi scelte lungo la traccia")
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
        if "_quota" in attrs and 0 <= attrs["_quota"] < len(self.quote):
            q = self.quote[attrs["_quota"]]
            if key == "quota_q":
                try:
                    value = round(float(value), 3)
                except (TypeError, ValueError):
                    value = None
                self.ortho_view.set_feature_attribute(layer, idx, "etichetta", "?" if value is None else f"{value:.2f}")
            if key != "etichetta":
                q[key] = value
        elif "_unit" in attrs and 0 <= attrs["_unit"] < len(self.units):
            u = self.units[attrs["_unit"]]
            fields = {"us_s": "us", "area_s": "area", "unita_tipo_s": "tipo", "scavo_s": "sito",
                      "us": "us", "area": "area", "tipo": "tipo"}  # pyunitastratigrafiche -> UnitPolygon
            if key in fields:
                setattr(u, fields[key], None if value in ("", None) else str(value))
                label = f"{u.tipo} {u.us or '?'}"
                for j, other in enumerate(feats):  # every part of a multipolygon
                    if other.get("_unit") == attrs["_unit"]:
                        self.ortho_view.set_feature_attribute(layer, j, key, value)
                        self.ortho_view.set_feature_attribute(layer, j, "etichetta", label)
            elif key != "etichetta":
                if key == "stratigraph_index_us":
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        value = 1
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
        sections, lines, skip, quote_gone = [], {}, set(), set()
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
            elif "_quota" in attrs:
                quote_gone.add(int(attrs["_quota"]))
            else:  # points and anything else drawn only in the view
                skip.add(id(attrs))
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
        self.quote = [q for i, q in enumerate(self.quote) if i not in quote_gone]
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
        with v.batch():
            self._redraw_overlays_now(skip)

    def _redraw_overlays_now(self, skip: set | None) -> None:
        v = self.ortho_view
        skip = skip or set()
        visible = {n: lay["visible"] for n, lay in v.layers.items()}
        order = list(v.layer_order)
        modelled = set(self.line_layers) | {"Sezioni", QUOTE_LAYER}
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
        for i, q in enumerate(self.quote):
            self._draw_quota(q, i)
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
        sito = sito or self._ensure_site("La scheda US appartiene a un sito.")
        if not sito:
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
        return self.project_site() or self.pa_pane.current_site() or ""

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
        us = str(attrs.get("us_s") or attrs.get("us") or "").replace("?", "")
        area = str(attrs.get("area_s") or attrs.get("area") or "1")
        tipo = str(attrs.get("unita_tipo_s") or attrs.get("tipo") or "US")
        site = (u.sito if u is not None and u.sito else "") or self._unit_site(layer)
        self.open_us_form(site, area, us, tipo, defaults=self._quote_defaults(u.geometry if u is not None else None))

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

    def _qgis_live_link(self):
        """The GeoPackage of the drawings read by QGIS (one per project folder and CRS; it
        remembers what it wrote, so only the changed tables are rewritten)."""
        from ..io.qgis_link import LiveLink

        folder, crs = self._qgis_dir(), self.chunk.crs if self.chunk else ""
        link = getattr(self, "_live_link", None)
        if link is None or link.path.parent != folder or link.crs != (crs or ""):
            link = self._live_link = LiveLink(folder, crs)
        return link

    def _export_qgis_layers(self, force: bool = False) -> dict:
        view = self.ortho_view
        layers = {name: {"kind": lay["kind"], "features": view.features(name)} for name, lay in view.layers.items()}
        try:
            return self._qgis_live_link().write(layers, force=force)
        except Exception as exc:  # a live refresh must never interrupt drawing with an error window
            log.warning("Aggiornamento dei layer per QGIS non riuscito: %s", exc, exc_info=True)
            if "locked" in str(exc) or "busy" in str(exc):
                self._qgis_timer.start(3000)  # QGIS or a sync client holds the file a moment: try again
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
        files = self._export_qgis_layers(force=True)
        if not files:
            self._warn(f"Non riesco a scrivere i disegni per QGIS in {self._qgis_dir()}: vedi il log.")
            return
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
            self.statusBar().showMessage("QGIS aperto: i disegni si aggiornano in QGIS da soli, ogni secondo", 15000)

        self.run_task("Apertura in QGIS", job, done, refresh=False, save=False)

    def _feature_clicked(self, layer: str, attrs: dict) -> None:
        text = "  ".join(f"{k}: {v}" for k, v in attrs.items() if v not in (None, "") and not str(k).startswith("_"))
        self.statusBar().showMessage(f"{layer}: {text}", 15000)
        self.layers_pane.show_attributes(layer, attrs)

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
        from ..archaeo import terrain

        dem = self.chunk.dem_path
        try:
            lo, hi = terrain.dem_range(dem)
            suggested = terrain.suggest_interval(dem)
        except Exception as exc:
            self._exc("DEM non leggibile", exc)
            return
        v = ParamDialog.ask(self, "Curve di livello", [
            Field("interval", "Equidistanza (m)", suggested, minimum=0.001, maximum=100, decimals=3),
            Field("index_every", "Curva direttrice ogni", 5, minimum=1, maximum=50),
            Field("smooth", "Levigatura del DEM (m, 0 = nessuna)", 0.0, minimum=0.0, maximum=50, decimals=2),
            Field("path", "Salva (DXF, GeoPackage, GeoJSON)", str(Path(self._last_dir()) / "curve.dxf"),
                  kind="file_save", filter="DXF (*.dxf);;GeoPackage (*.gpkg);;GeoJSON (*.geojson)")],
            intro=f"Quote del DEM da {lo:.2f} a {hi:.2f} m (dislivello {hi - lo:.2f} m): con {suggested:g} m "
                  f"sono circa {int((hi - lo) / suggested)} livelli. Su un DSM da drone (vegetazione, zolle) "
                  "una levigatura di 0,2-0,5 m toglie i piccoli anelli e da' curve piu' pulite.")
        if not v:
            return
        levels = (hi - lo) / v["interval"]
        if levels > 300:
            r = message(self, "question", "Curve di livello",
                        f"Con equidistanza {v['interval']:g} m il DEM (dislivello {hi - lo:.1f} m) da' {int(levels)} "
                        f"livelli di curve: decine di migliaia di linee, lente da disegnare e da usare. "
                        f"Un'equidistanza di {suggested:g} m ne da' circa {int((hi - lo) / suggested)}.\n\nContinuare "
                        f"con {v['interval']:g} m?",
                        buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        default=QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return

        def job(p):
            p(0.1, "Curve di livello")
            lines = terrain.contours(dem, v["interval"], index_every=v["index_every"], smooth=v["smooth"])
            p(0.7, f"Salvataggio di {len(lines)} curve")
            terrain.export_contours(lines, v["path"], crs=self.chunk.crs)
            return lines

        def done(lines):
            self._show_raster("dem")
            name = f"Curve di livello ({v['interval']:g} m)"
            self._draw_contours(name, lines)
            self.log_pane.info(f"{name}: {len(lines)} linee, {sum(len(ln['coords']) for ln in lines)} vertici, "
                               f"salvate in {v['path']}")
            self.statusBar().showMessage(f"{len(lines)} curve salvate in {v['path']}", 10000)
            self._show_pane(self.left_tabs, self.layers_pane)

        self.run_task("Curve di livello", job, done, refresh=False, save=False)

    def _draw_contours(self, name: str, lines: list[dict]) -> None:
        """Contour lines as a layer of the raster view (replacing a layer with the same name),
        added in one batch: tens of thousands of lines update the panes and QGIS once."""
        self.line_layers.pop(name, None)
        self.ortho_view.remove_layer(name)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with self.ortho_view.batch():
                for ln in lines:
                    self._add_line_feature(name, ln["coords"], "#ffffff" if ln.get("index") else "#202020",
                                           attrs={"quota": round(float(ln.get("elevation", 0)), 3),
                                                  "direttrice": bool(ln.get("index"))},
                                           width=1.6 if ln.get("index") else 1.0)
        finally:
            QApplication.restoreOverrideCursor()

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
        z_default = 0.0
        if self.chunk.dem_path:
            from ..archaeo.terrain import dem_range

            try:
                lo, hi = dem_range(self.chunk.dem_path)[:2]
                z_default = round((lo + hi) / 2, 3)
            except Exception:
                pass
        choices, codes = self._cloud_class_choices()
        v = ParamDialog.ask(self, "Pianta a quota", [
            Field("z", "Quota (m)", float(z_default), minimum=-10000.0, maximum=100000.0, decimals=3)]
            + ([Field("classes", "Classi", codes, choices=choices, kind="checklist")] if len(codes) > 1 else []),
            intro="Il modello viene tagliato con un piano orizzontale alla quota scelta (per esempio la pianta di un "
                  "muro)." + (" Togliendo delle classi restano solo i tratti che poggiano sulle classi scelte."
                              if len(codes) > 1 else ""))
        if not v:
            return
        z0 = float(v["z"])
        classes = self._chosen_classes(v)
        if classes == []:
            self._warn("Scegli almeno una classe.")
            return
        from ..archaeo import sections

        out = (self.doc.path.parent if self.doc.path else Path.home()) / f"pianta_{z0:.3f}.dxf"

        def job(p):
            s = sections.plan_slice(self.chunk, z0, classes=classes)
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
            self.show_classes()

        self.run_task("Classificazione", lambda p: classify_chunk(self.chunk), done)

    def show_classes(self) -> None:
        """The dense cloud coloured by class, with the legend (untick a class to hide it)."""
        if not self._need_chunk(product="point_cloud"):
            return
        self.model_view.set_color_mode("class")
        self._show_model("point_cloud")
        self.statusBar().showMessage("Nuvola per classi: togli la spunta nella legenda per nascondere una classe; "
                                     "«Lazo» per correggere la classe di un gruppo di punti", 15000)

    def classify_with_lasso(self) -> None:
        if not self._need_chunk(product="point_cloud"):
            return
        self.show_classes()
        self.model_view.start_lasso()

    def _points_selected(self, visible, inside) -> None:
        """Points chosen with the lasso in the 3D view: give them a class, delete them or keep
        only them (Metashape's delete / crop selection)."""
        from ..core.pointcloud import ARCHAEO_CLASSES, ASPRS_CLASSES
        from .scene import class_name

        n_vis, n_all = int(np.count_nonzero(visible)), int(np.count_nonzero(inside))
        if not n_all:
            self.statusBar().showMessage("Nessun punto nel lazo", 8000)
            return
        labels = self.chunk.meta.get("classes", {})
        codes = sorted(set(ASPRS_CLASSES) | set(ARCHAEO_CLASSES) | {int(k) for k in labels}
                       | set(self.model_view.sd.class_counts if self.model_view.sd else {}))
        last = int(self.settings.value("classes/last", 64) or 64)
        v = ParamDialog.ask(self, "Punti selezionati con il lazo", [
            Field("action", "Cosa fare", "class",
                  choices=[("class", "Assegna una classe"), ("delete", "Elimina questi punti"),
                           ("keep", "Tieni solo questi punti (ritaglia)")]),
            Field("code", "Classe", last if last in codes else codes[0],
                  choices=[(c, f"{c} - {class_name(c, labels)}") for c in codes]),
            Field("other", "...oppure un nuovo codice (0 = usa la classe sopra)", 0, minimum=0, maximum=255),
            Field("name", "Nome del nuovo codice (es. Crollo)", "", kind="text"),
            Field("through", f"Anche i punti nascosti dietro la superficie ({_it(n_all)} invece di {_it(n_vis)})",
                  False),
            Field("keep_copy", "Elimina / tieni: conserva prima una copia della nuvola (versione)", False)],
            intro=f"Nel lazo ci sono {_it(n_vis)} punti visibili ({_it(n_all)} contando quelli nascosti). Le classi "
                  "date a mano vengono ricordate: le classificazioni automatiche non le cambiano e il classificatore "
                  "impara da loro (Classificazione della nuvola > Addestra il classificatore). Eliminare o tenere "
                  "cambia la nuvola attuale: senza la copia non si torna indietro.")
        if not v:
            return
        mask = inside if v["through"] else visible
        if v["action"] in ("delete", "keep"):
            from ..engine.cleaning import delete_points

            keep_selected = v["action"] == "keep"
            self.run_task("Lazo sulla nuvola", self._versioned_edit(
                "point_cloud", v, lambda p: delete_points(self.chunk, mask, keep_selected=keep_selected),
                "Nuvola modificata con il lazo"),
                lambda out: (self.log_pane.info(f"Lazo: tolti {_it(out['removed'])} punti, restano {_it(out['kept'])}"),
                             self.model_view.refresh(reset=False)), refresh=False)
            return
        code = int(v["other"] or v["code"])
        name = str(v.get("name") or "").strip() if v["other"] else ""
        self.settings.setValue("classes/last", code)
        self._set_point_class(mask, code, name or None)
        self.log_pane.info(f"Classe {code} ({class_name(code, self.chunk.meta.get('classes'))}) assegnata a mano a "
                           f"{int(np.count_nonzero(mask))} punti")
        self.model_view.refresh(reset=False)
        self._dirty(True)

    def _faces_selected(self, visible, inside) -> None:
        """Faces of the model chosen with the lasso: delete them or keep only them."""
        n_vis, n_all = int(np.count_nonzero(visible)), int(np.count_nonzero(inside))
        if not n_all:
            self.statusBar().showMessage("Nessuna faccia nel lazo", 8000)
            return
        v = ParamDialog.ask(self, "Facce selezionate con il lazo", [
            Field("action", "Cosa fare", "delete",
                  choices=[("delete", "Elimina queste facce"), ("keep", "Tieni solo queste facce (ritaglia)")]),
            Field("through", f"Anche le facce nascoste dietro la superficie ({_it(n_all)} invece di {_it(n_vis)})",
                  False),
            Field("keep_copy", "Conserva prima una copia del modello (versione)", False)],
            intro=f"Nel lazo ci sono {_it(n_vis)} facce visibili ({_it(n_all)} contando quelle nascoste). Colori e "
                  "confidenza restano; la texture va ricostruita.")
        if not v:
            return
        from ..engine.cleaning import delete_faces

        mask = inside if v["through"] else visible
        keep_selected = v["action"] == "keep"
        self.run_task("Lazo sul modello", self._versioned_edit(
            "model", v, lambda p: delete_faces(self.chunk, mask, keep_selected=keep_selected),
            "Modello modificato con il lazo"),
            lambda out: (self.log_pane.info(f"Lazo: tolte {_it(out['removed_faces'])} facce, restano {_it(out['faces'])}"),
                         self._show_model("model")))

    def _set_point_class(self, mask, code: int, label: str | None = None) -> None:
        """Assign a class by hand: the points are flagged as labelled by hand (kept by the
        automatic classifiers, used to train the classifier)."""
        from ..ai.classify import set_class

        set_class(self.chunk, mask, code, label=label)

    # ---------------------------------------------------------------- versions of the products
    def _version_fields(self, product: str) -> list:
        """Keep the active version of ``product`` (when there is one) or overwrite it, and the
        name of the version built now."""
        from ..core import versions

        fields = []
        if self.chunk is not None and versions.active_files(self.chunk, product):
            name = versions.active_info(self.chunk, product)["name"]
            fields.append(Field("keep", "Versione attuale", "keep",
                                choices=[("keep", f"Conservala per confrontarla ({name})"),
                                         ("overwrite", "Sovrascrivila")]))
        fields.append(Field("version_name", "Nome di questa versione (facoltativo)", "", kind="text"))
        return fields

    def _versioned(self, product: str, v: dict, job):
        """``job(p)`` keeping first the active version of ``product`` when asked (it comes back if
        the job fails) and naming the one it builds."""
        from ..core import versions

        chunk = self.chunk
        keep = v.get("keep") == "keep"
        name = (v.get("version_name") or "").strip() or None

        def wrapped(p):
            kept = versions.keep_active(chunk, product, move=True) if keep else None
            try:
                result = job(p)
            except BaseException:
                if kept is not None:
                    versions.restore(chunk, product, kept)
                raise
            versions.set_active_name(chunk, product, name)
            return result

        return wrapped

    def _workspace_menu(self, kind: str, chunk, payload, pos) -> None:
        """Right click in the workspace: the versions of the dense cloud, model, DEM, orthomosaic."""
        from ..core import versions

        if chunk is not self.chunk or kind not in ("product", "version"):
            return
        product, vid = (payload, None) if kind == "product" else payload
        if product not in versions.PRODUCTS:
            return
        menu = QMenu(self)
        if vid is None:
            menu.addAction(icon("view"), "Mostra", lambda: self._workspace_item("product", chunk, product))
            menu.addAction(icon("recent"), "Conserva una copia come versione",
                           lambda: self.keep_version_copy(product))
            menu.addAction(icon("edit"), "Rinomina questa versione...", lambda: self.rename_version(product, None))
        else:
            menu.addAction(icon("view"), "Mostra", lambda: self.show_version(product, vid))
            menu.addAction(icon("ok"), "Rendi attiva (la usano gli altri strumenti)",
                           lambda: self.activate_version(product, vid))
            if product == "dem" and chunk.dem_path:
                menu.addAction(icon("dem"), "Differenza con il DEM attivo", lambda: self.dem_difference(vid))
            menu.addAction(icon("export"), "Esporta...", lambda: self.export_version(product, vid))
            menu.addAction(icon("edit"), "Rinomina...", lambda: self.rename_version(product, vid))
            menu.addSeparator()
            menu.addAction(icon("delete"), "Elimina", lambda: self.delete_version(product, vid))
        menu.exec(pos)

    def _version_title(self, product: str, vid: str) -> str:
        from ..core import versions

        e = next(e for e in versions.versions(self.chunk, product) if e["id"] == vid)
        return e["name"]

    def show_version(self, product: str, vid: str) -> None:
        """Show a kept version without making it active."""
        from ..core import versions

        path = versions.version_file(self.chunk, product, vid)
        title = self._version_title(product, vid)
        if product in ("dem", "orthomosaic"):
            self._load_raster("dem" if product == "dem" else "ortho", path)
            self._raster_version = (product, vid)
            self.show_view(self.ortho_view)
        else:
            self.model_view.show_version(product, path, title)
            self.show_view(self.model_view)
        self.statusBar().showMessage(f"Versione «{title}» (non attiva): doppio clic sul prodotto nell'area di lavoro "
                                     "per tornare a quella attiva", 15000)

    def keep_version_copy(self, product: str) -> None:
        """Keep a copy of the active version (e.g. before classifying the cloud again)."""
        from ..core import versions

        chunk = self.chunk
        self.run_task("Copia della versione", lambda p: versions.keep_active(chunk, product),
                      lambda e: self.log_pane.info(f"Conservata una copia: «{e['name']}»") if e else None)

    def activate_version(self, product: str, vid: str) -> None:
        from ..core import versions

        chunk = self.chunk
        title = self._version_title(product, vid)

        def done(_previous):
            self.log_pane.info(f"Versione attiva: «{title}»")
            if product in ("dem", "orthomosaic"):
                self._show_raster("dem" if product == "dem" else "ortho")
            else:
                self.model_view.set_chunk(chunk, product)
                self.show_view(self.model_view)

        self.run_task("Cambio di versione", lambda p: versions.activate(chunk, product, vid), done)

    def rename_version(self, product: str, vid: str | None) -> None:
        from ..core import versions

        old = versions.active_info(self.chunk, product)["name"] if vid is None else self._version_title(product, vid)
        name, ok = QInputDialog.getText(self, "Rinomina versione", "Nome:", text=old)
        if ok and name.strip():
            versions.rename(self.chunk, product, vid, name.strip())
            self._dirty(True)
            self.workspace.populate(self.doc)

    def delete_version(self, product: str, vid: str) -> None:
        from ..core import versions

        title = self._version_title(product, vid)
        r = message(self, "question", "Elimina versione", f"Eliminare la versione «{title}» e i suoi file?",
                    buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        if getattr(self, "_raster_version", None) == (product, vid) or self.model_view.paths:
            self.model_view.set_chunk(self.chunk, self.model_view.kind)
            self._load_raster()
        versions.delete(self.chunk, product, vid)
        self._dirty(True)
        self.workspace.populate(self.doc)
        self.log_pane.info(f"Versione «{title}» eliminata")

    def export_version(self, product: str, vid: str) -> None:
        import shutil

        from ..core import versions

        src = versions.version_file(self.chunk, product, vid)
        path, _ = QFileDialog.getSaveFileName(self, "Esporta versione", str(Path(self._last_dir()) / src.name),
                                              f"*{src.suffix}")
        if path:
            shutil.copy2(src, path)
            self.log_pane.info(f"Versione «{self._version_title(product, vid)}» esportata in {path}")

    def dem_difference(self, vid: str) -> None:
        """Active DEM minus a kept one, shown with diverging colours (e.g. surface minus terrain =
        height of walls and vegetation; after minus before an excavation = removed depth)."""
        from ..core import versions

        chunk = self.chunk
        title = self._version_title("dem", vid)
        active = versions.active_info(chunk, "dem")["name"]
        kept = next(e for e in versions.versions(chunk, "dem") if e["id"] == vid)
        r1, r2 = (chunk.meta.get("dem") or {}).get("resolution"), (kept.get("params") or {}).get("resolution")

        def done(path):
            self._load_raster("diff", path)
            lim = getattr(self.ortho_view, "diff_limit", 0.0)
            self.show_view(self.ortho_view)
            text = (f"Differenza «{active}» - «{title}»: rosso dove l'attivo e' piu' alto, blu dove e' piu' basso "
                    f"(colori pieni a +/-{lim:.2f} m); dZ sotto il cursore")
            if r1 and r2 and abs(r1 - r2) > 1e-6 * max(r1, r2):
                text += (f". Attenzione: risoluzioni diverse ({r1:.3g} e {r2:.3g} m), i bordi ripidi risultano diversi; "
                         "per un confronto pulito usa la stessa risoluzione")
            self.log_pane.info(text)
            self.statusBar().showMessage(text, 30000)

        self.run_task("Differenza tra DEM", lambda p: versions.dem_difference(chunk, vid), done, refresh=False,
                      save=False)

    # ---------------------------------------------------------------- products from chosen classes
    def _cloud_class_choices(self, noise: bool = False) -> tuple[list, list[int]]:
        """Classes of the dense cloud as checklist choices (``"2 Terreno (1.234 punti)"``) and
        their codes; noise left out unless ``noise``."""
        from ..core.pointcloud import NOISE_CLASSES
        from .scene import class_name

        path = self.chunk.product_path("point_cloud") if self.chunk is not None else None
        if path is None or not path.exists():
            return [], []
        key = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        if getattr(self, "_class_counts_key", None) != key:  # read the class column once per version of the file
            try:
                self._class_counts = self.chunk.point_classes()
            except Exception as exc:  # unreadable cloud: products without class choice
                log.warning("Classi della nuvola non lette: %s", exc)
                return [], []
            self._class_counts_key = key
        counts = self._class_counts
        labels = self.chunk.meta.get("classes", {})
        codes = [c for c in sorted(counts) if noise or c not in NOISE_CLASSES]
        return [(c, f"{c} {class_name(c, labels)} ({counts[c]:,} punti)".replace(",", ".")) for c in codes], codes

    def _class_fields(self, label: str) -> list:
        """A checklist of the classes of the dense cloud (all ticked), when it has more than one."""
        choices, codes = self._cloud_class_choices()
        return [Field("classes", label, codes, choices=choices, kind="checklist")] if len(codes) > 1 else []

    def _class_hint(self, text: str) -> str:
        return text if len(self._cloud_class_choices()[1]) > 1 else ""

    def _chosen_classes(self, values: dict):
        """None when every class is ticked (the product as always), else the ticked codes."""
        if "classes" not in values:
            return None
        codes = self._cloud_class_choices()[1]
        chosen = sorted(int(c) for c in values["classes"])
        return None if set(chosen) >= set(codes) else chosen

    def units_to_classes(self) -> None:
        """One class per stratigraphic unit (points inside its polygon), with points, area and
        elevations per unit."""
        if not self._need_chunk(product="point_cloud"):
            return
        units = [u for u in self.units if u.geometry is not None and not u.geometry.is_empty]
        if not units:
            self._warn("Non ci sono US sull'ortofoto: disegnale (Disegna US) o caricale da PyArchInit "
                       "(PyArchInit > Carica poligoni US).")
            return
        from ..ai.classify import classify_by_units, unit_class_name, units_table_csv

        names = [unit_class_name(u, i) for i, u in enumerate(units)]
        unique = list(dict.fromkeys(names))
        v = ParamDialog.ask(self, "Classi dalle US", [
            Field("units", "US", unique, choices=[(n, n) for n in unique], kind="checklist"),
            Field("keep_manual", "Mantieni le correzioni fatte a mano", True)],
            intro="I punti della nuvola densa dentro il poligono di ogni US (in pianta) prendono la classe di quella "
                  "US, con codici da 100. Poi puoi vedere la nuvola per US, nasconderle, esportarle o farne DEM e "
                  "mesh; per ogni US ottieni punti, area e quote.")
        if not v or not v["units"]:
            return
        chosen = [u for u, n in zip(units, names) if n in v["units"]]
        chunk = self.chunk

        def done(rows):
            self.show_classes()
            f = lambda x: "" if x is None else round(x, 3)  # noqa: E731
            table = [[r["name"], r["code"], r["points"], round(r["area_m2"], 3), f(r["z_min"]), f(r["z_mean"]),
                      f(r["z_max"])] for r in rows]
            self.log_pane.info("Classi dalle US: " + ", ".join(f"{r['name']} = {r['code']} ({r['points']} punti)"
                                                               for r in rows))
            TableDialog("Classi dalle US", ["US", "Classe", "Punti", "Area (m²)", "Quota min", "Quota media",
                                            "Quota max"], table, self,
                        intro="La nuvola e' colorata per classi: ogni US ha la sua. Area = superficie del poligono in "
                              "pianta; quote dei punti della US.",
                        save=lambda path: units_table_csv(rows, path),
                        csv_name=str(Path(self._last_dir()) / "us_statistiche.csv")).exec()

        self.run_task("Classi dalle US", lambda p: classify_by_units(chunk, chosen, keep_manual=v["keep_manual"]),
                      done)

    # ---------------------------------------------------------------- trainable classifier
    def _classifier_path(self) -> Path:
        """Where the classifier trained in this chunk is kept (next to its data)."""
        return self.chunk.data_dir / CLASSIFIER_FILE

    def _need_sklearn(self) -> bool:
        try:
            import sklearn  # noqa: F401

            return True
        except ImportError:
            self._warn("Il classificatore addestrabile richiede scikit-learn:\n"
                       "pip install \"pyarchinit-photogrammetry[learn]\"")
            return False

    def _current_classifier(self):
        """The classifier in use, or the one trained in this chunk (read from its file)."""
        if self.classifier is None and self.chunk is not None and self._classifier_path().exists():
            from ..ai.learn import ClassifierModel

            self.classifier = ClassifierModel.load(self._classifier_path())
        return self.classifier

    def _classifier_report(self, model) -> str:
        """What the classifier learnt, in Italian: method, radii, accuracy, figures per class."""
        from .scene import class_name

        st = model.stats or {}
        labels = self.chunk.meta.get("classes", {}) if self.chunk is not None else {}
        num = lambda n: f"{int(n):,}".replace(",", ".")  # noqa: E731
        method = "Random forest" if model.method == "random_forest" else "Gradient boosting"
        lines = [f"{method}: {len(model.feature_names)} caratteristiche per punto, raggi "
                 + " / ".join(f"{r:.3g}" for r in model.scales) + " m."]
        acc = st.get("validation_accuracy")
        if st:
            check = "sui campioni esclusi da ogni albero" if st.get("validation") == "out-of-bag" else \
                "su un quinto dei campioni tenuto da parte"
            lines.append(f"Addestrato su {num(st.get('n_samples', 0))} campioni ({num(st.get('n_labelled_points', 0))} "
                         "punti con la classe nota)" + ("" if acc is None else f"; accuratezza {check}: {acc:.1%}."))
        for c in model.classes:
            fig = st.get("per_class", {}).get(str(c), {})
            name = class_name(c, labels)
            if name == f"Classe {c}" and model.class_names.get(c, f"Class {c}") != f"Class {c}":
                name = model.class_names[c]  # named in the project where the classifier was trained
            pr, rc = fig.get("precision"), fig.get("recall")
            lines.append(f"  {c} {name}: {num(fig.get('samples', 0))} campioni"
                         + ("" if pr is None else f", precisione {pr:.1%}") + ("" if rc is None else f", richiamo {rc:.1%}"))
        return "\n".join(lines)

    def _log_classes(self, pc) -> None:
        from ..ai.classify import class_statistics

        st = class_statistics(pc)
        self.log_pane.info("Classi: " + ", ".join(f"{k}: {n}" for k, n in sorted(st.items())))

    def train_classifier(self) -> None:
        """Learn the classes from the points corrected by hand, then classify the rest."""
        if not self._need_chunk(product="point_cloud") or not self._need_sklearn():
            return
        from .scene import class_name

        pc = self.chunk.point_cloud
        labels = self.chunk.meta.get("classes", {})
        manual = pc.manual if pc.manual is not None else np.zeros(len(pc), bool)
        by_hand = sorted(int(c) for c in np.unique(pc.classification[manual])) if manual.any() else []
        classified = sorted(int(c) for c in np.unique(pc.classification) if c > 1)
        names = lambda codes: ", ".join(f"{c} {class_name(c, labels)}" for c in codes) or "nessuna"  # noqa: E731
        v = ParamDialog.ask(self, "Addestra il classificatore", [
            Field("labels", "Impara da", "manual" if len(by_hand) >= 2 else "all",
                  choices=[("manual", f"Punti corretti a mano ({int(manual.sum()):,} punti)".replace(",", ".")),
                           ("all", "Tutti i punti con una classe (sopra 1)")]),
            Field("method", "Metodo", "random_forest",
                  choices=[("random_forest", "Random forest (consigliato)"),
                           ("gradient_boosting", "Gradient boosting (file piu' piccolo)")]),
            Field("trees", "Alberi (o iterazioni)", 100, minimum=10, maximum=1000),
            Field("apply", "Poi classifica il resto della nuvola", True)],
            intro="Il classificatore impara dai punti di cui conosci la classe: con il lazo (vista 3D) assegna la "
                  "classe giusta ad alcuni gruppi di punti di ogni tipo (almeno due classi), poi addestralo. Usa la "
                  "geometria attorno a ogni punto a tre scale, l'altezza sul terreno e il colore.\n\n"
                  f"Classi corrette a mano: {names(by_hand)}.\nClassi presenti: {names(classified)}.")
        if not v:
            return
        if len(by_hand if v["labels"] == "manual" else classified) < 2:
            self._warn("Servono punti di almeno due classi diverse: assegnale con il lazo nella vista 3D "
                       "(Classificazione della nuvola > Correggi a mano con il lazo).")
            return
        from ..ai import learn

        path = self._classifier_path()
        chunk = self.chunk

        def job(p):
            model = learn.train_on_chunk(chunk, labels=v["labels"], method=v["method"], n_estimators=int(v["trees"]),
                                         progress=p.sub(0.0, 0.6 if v["apply"] else 1.0))
            model.save(path)
            result = None
            if v["apply"]:
                result = learn.classify_chunk_with_model(chunk, model, progress=p.sub(0.6, 0.4))
            return model, result

        def done(res):
            model, result = res
            self.classifier = model
            if result is not None:
                self._log_classes(result[0])
                self.show_classes()
            self._info("Classificatore addestrato", self._classifier_report(model) + "\n\nSalvato nel progetto; "
                       "con «Salva il classificatore» si usa anche in altri scavi.")

        self.run_task("Addestramento del classificatore", job, done)

    def apply_classifier(self) -> None:
        """Classify the dense cloud with the trained (or loaded) classifier."""
        if not self._need_chunk(product="point_cloud") or not self._need_sklearn():
            return
        try:
            model = self._current_classifier()
        except Exception as exc:
            self._exc("Classificatore non leggibile", exc)
            return
        if model is None:
            r = message(self, "question", "Classificatore", "Questo progetto non ha ancora un classificatore "
                        "addestrato.\n\nCaricarne uno da file (.ppgclf)?",
                        buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes or not self.load_classifier():
                return
            model = self.classifier
        from .scene import class_name

        v = ParamDialog.ask(self, "Classifica con il classificatore", [
            Field("min_confidence", "Confidenza minima (sotto resta 1, non classificato)", 0.0, minimum=0.0,
                  maximum=0.99, decimals=2),
            Field("overwrite", "Riclassifica anche i punti corretti a mano", False)],
            intro="Classi del classificatore: " + ", ".join(f"{c} {model.class_names.get(c) or class_name(c)}"
                                                             for c in model.classes) + ".")
        if not v:
            return
        from ..ai import learn

        chunk = self.chunk

        def done(res):
            self._log_classes(res[0])
            self.show_classes()

        self.run_task("Classificazione con il classificatore",
                      lambda p: learn.classify_chunk_with_model(chunk, model, overwrite_manual=v["overwrite"],
                                                                min_confidence=float(v["min_confidence"]), progress=p),
                      done)

    def load_classifier(self) -> bool:
        path, _ = QFileDialog.getOpenFileName(self, "Carica classificatore", self._last_dir(),
                                              "Classificatori (*.ppgclf)")
        if not path or not self._need_sklearn():
            return False
        from ..ai.learn import ClassifierModel

        try:
            self.classifier = ClassifierModel.load(path)
        except Exception as exc:
            self._exc("Classificatore non leggibile", exc)
            return False
        self.log_pane.info(f"Classificatore caricato da {path}\n{self._classifier_report(self.classifier)}")
        return True

    def save_classifier(self) -> None:
        try:
            model = self._current_classifier() if self._need_sklearn() else None
        except Exception as exc:
            self._exc("Classificatore non leggibile", exc)
            return
        if model is None:
            self._warn("Nessun classificatore: addestralo prima (Classificazione della nuvola > Addestra).")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Salva classificatore", self._last_dir() + "/classificatore.ppgclf",
                                              "Classificatori (*.ppgclf)")
        if path:
            self.log_pane.info(f"Classificatore salvato in {model.save(path)}")

    def clear_manual_labels(self) -> None:
        if not self._need_chunk(product="point_cloud"):
            return
        r = message(self, "question", "Correzioni a mano", "Dimenticare quali punti sono stati corretti a mano? "
                    "Le classi restano, ma le prossime classificazioni automatiche potranno cambiarle.",
                    buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        from ..ai.classify import clear_manual

        n = clear_manual(self.chunk)
        self.log_pane.info(f"Correzioni a mano dimenticate su {n} punti (le classi restano)")
        self._dirty(True)

    def _segmenter(self):
        if self.sam is None:
            from ..ai.sam_env import make_segmenter

            self.sam = make_segmenter(str(self.settings.value("sam/model", "small") or "small"))
            device = getattr(self.sam, "device", None)
            if device:
                log.info("SAM 2.1 (%s) su %s", self.settings.value("sam/model", "small"),
                         {"mps": "GPU del Mac (MPS)", "cuda": "GPU NVIDIA (CUDA)"}.get(device, device))
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
            device = sam_env.DEVICE_NAMES.get(sam_env.installed_device(env) or "cpu")
            self._info("Segment Anything 2.1", f"SAM 2.1 installato in {env}.\n\nCalcolo su: {device}.")
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

    # ---------------------------------------------------------------- SAM on the orthomosaic
    def _set_sam_tool(self, key: str) -> None:
        self.settings.setValue("sam/tool", key)
        self.ortho_view.sam_tool = key
        if key in self.sam_tool_actions:
            self.sam_tool_actions[key].setChecked(True)
        self._sam_cancel()
        if self.ortho_view.mode == "sam":
            self.statusBar().showMessage(self._sam_hint(), 20000)

    def _set_sam_model(self, key: str) -> None:
        self.settings.setValue("sam/model", key)
        if self.sam is not None and hasattr(self.sam, "close"):
            self.sam.close()
        self.sam = None

    def _sam_hint(self) -> str:
        edit = " Maiusc+clic aggiunge una parte, clic destro la toglie; Invio salva, Esc annulla."
        return {"tap": "SAM, tocco: clic sull'oggetto. SAM guarda l'area visibile: avvicinati per i dettagli, "
                       "allontanati per gli oggetti grandi." + edit,
                "box": "SAM, riquadro: trascina un riquadro attorno all'oggetto." + edit,
                "area": "SAM, area: trascina un riquadro; SAM cerca tutte le unita' al suo interno."}.get(
            self.ortho_view.sam_tool, "")

    def _sam_cancel(self) -> None:
        self._sam_obj = None
        if hasattr(self, "ortho_view"):
            self.ortho_view.clear_sam_preview()

    @staticmethod
    def _grow(bounds, x: float, y: float, margin: float = 0.25):
        """``bounds`` grown to contain (x, y) with a margin (a click outside the area SAM saw)."""
        x0, y0, x1, y1 = bounds
        if x0 <= x <= x1 and y0 <= y <= y1:
            return bounds
        w, h = x1 - x0, y1 - y0
        return (min(x0, x - margin * w), min(y0, y - margin * h), max(x1, x + margin * w), max(y1, y + margin * h))

    def _sam_click(self, x: float, y: float, label: int, new: bool) -> None:
        if not self._need_chunk(product="orthomosaic"):
            return
        obj = self._sam_obj
        if new or obj is None:
            if not label:
                return  # nothing to remove from yet
            self._ensure_site("Le US trovate da SAM appartengono a un sito: sceglilo una volta per il progetto.")
            obj = {"points": [(x, y, 1)], "box": None, "bounds": self.ortho_view.visible_world_bounds()}
            self._sam_run(obj, ask=True)
        else:
            obj = {**obj, "points": obj["points"] + [(x, y, int(label))], "bounds": self._grow(obj["bounds"], x, y)}
            self._sam_run(obj, ask=False)

    def _sam_rect(self, x0: float, y0: float, x1: float, y1: float) -> None:
        if not self._need_chunk(product="orthomosaic"):
            return
        self._ensure_site("Le US trovate da SAM appartengono a un sito: sceglilo una volta per il progetto.")
        if self.ortho_view.sam_tool == "area":
            self._sam_cancel()
            self._sam_auto((x0, y0, x1, y1), title="SAM - unita' nell'area")
            return
        view = self.ortho_view.visible_world_bounds()
        w, h = x1 - x0, y1 - y0
        bounds = (x0 - 0.3 * w, y0 - 0.3 * h, x1 + 0.3 * w, y1 + 0.3 * h)  # the object and some context
        if view is not None and view[0] <= bounds[0] and view[1] <= bounds[1] and view[2] >= bounds[2] \
                and view[3] >= bounds[3]:
            bounds = view
        self._sam_run({"points": [], "box": (x0, y0, x1, y1), "bounds": bounds}, ask=True)

    def _sam_run(self, obj: dict, ask: bool) -> None:
        """Segment the current object (prompts in ``obj``) in the background, show the mask,
        then (``ask``) open the unit form: Salva / Correggi / Annulla."""
        from ..ai.sam import segment_orthomosaic

        pts = obj["points"]

        def job(p):
            p(0.2, "SAM 2.1")
            seg = self._segmenter()
            return segment_orthomosaic(self.chunk, seg, points=[(x, y) for x, y, _ in pts] if pts else None,
                                       labels=[lab for _, _, lab in pts] if pts else None, box=obj["box"],
                                       bounds=obj["bounds"])

        def done(unit):
            if unit.geometry.is_empty:
                self.statusBar().showMessage("SAM non ha trovato un oggetto qui: prova un altro punto o cambia lo "
                                             "zoom", 10000)
                if ask:
                    self._sam_cancel()
                return
            self._sam_obj = {**obj, "geom": unit.geometry}
            self.ortho_view.show_sam_preview(unit.geometry, pts, obj["box"])
            if ask:
                self._sam_accept()
            else:
                self.statusBar().showMessage(f"Maschera aggiornata ({unit.geometry.area:.3f} m2): Invio per salvare, "
                                             "Esc per annullare", 15000)

        if not self.runner.run("SAM 2.1", job, done, self._error, quiet=True):
            self.statusBar().showMessage("SAM sta ancora lavorando: attendi il risultato", 5000)

    def _sam_key(self, key: str) -> None:
        if key == "accept" and self._sam_obj is not None and self._sam_obj.get("geom") is not None:
            self._sam_accept()
        elif key == "cancel":
            self._sam_cancel()
            self.statusBar().showMessage("Maschera di SAM annullata", 5000)

    def _sam_accept(self) -> None:
        obj = self._sam_obj
        if obj is None or obj.get("geom") is None:
            return
        r, v = self._ask_unit(refine=True, title="Unita' da SAM")
        if r == 1:
            self._sam_cancel()
            self._add_unit(self._make_unit(obj["geom"], v))
        elif r == UnitDialog.REFINE:
            self.statusBar().showMessage("Correggi la maschera: Maiusc+clic aggiunge, clic destro toglie; "
                                         "Invio per salvare, Esc per annullare", 30000)
        else:
            self._sam_cancel()

    def _sam_auto(self, bounds=None, title: str = "SAM - unita' automatiche", **kw) -> None:
        """Every unit SAM finds in ``bounds`` (None: the whole orthomosaic), added without US
        to the layer *Unita' da SAM* of the project's site."""
        from ..ai.sam import auto_units

        def job(p):
            seg = self._segmenter()
            return auto_units(self.chunk, seg, bounds=bounds, progress=p, **kw)

        def done(units):
            site = self.project_site() or None
            with self.ortho_view.batch():
                for u in units:
                    u.sito = site
                    u.attributes = {"_layer": "Unita' da SAM", "stratigraph_index_us": 1}
                    self.units.append(u)
                    self._draw_unit(u, len(self.units) - 1)
            self._dirty(True)
            self.statusBar().showMessage(f"{len(units)} unita' candidate nel layer «Unita' da SAM»: assegna le US "
                                         "(pannello Layer) prima di pubblicarle", 20000)
            self.log_pane.info(f"SAM: {len(units)} unita' candidate")
            self._show_pane(self.left_tabs, self.layers_pane)

        self.run_task(title, job, done, refresh=False, save=False)

    def sam_whole_orthomosaic(self) -> None:
        """Automatic SAM on the whole orthomosaic, scanned in tiles at a resolution the user picks."""
        if not self._need_chunk(product="orthomosaic") or not self._sam_available(then=self.sam_whole_orthomosaic):
            return
        import rasterio

        from ..ai.sam import THOROUGHNESS

        with rasterio.open(self.chunk.orthomosaic_path) as src:
            native = abs(src.res[0])
            side = max(src.width, src.height) * native
        per_axis = 8
        suggested = max(native, side / (per_axis * 896 + 128))

        def tiles(res):
            n = max(1, int(np.ceil(max(side / res - 1024, 0) / 896)) + 1)
            return n * n

        v = ParamDialog.ask(self, "SAM - tutta l'ortofoto", [
            Field("resolution", "Risoluzione di lavoro (m/pixel)", round(suggested, 3), minimum=native, maximum=10.0,
                  decimals=3, tip="Piu' fine = piu' dettagli e piu' tasselli (piu' tempo)"),
            Field("thoroughness", "Scansione", "normale", choices=[(k, k.capitalize()) for k in THOROUGHNESS]),
            Field("min_area", "Area minima di un'unita' (m2)", round(max(0.01, (12 * suggested) ** 2), 3),
                  minimum=0.0001, maximum=10000.0, decimals=4),
            Field("exclusive", "Unita' non sovrapposte (le piccole ritagliano le grandi)", True)],
            intro=f"L'ortofoto ({side:.0f} m di lato, pixel di {native * 100:.1f} cm) viene esaminata a tasselli di 1024 "
                  f"pixel che si sovrappongono: alla risoluzione proposta sono circa {tiles(suggested)} tasselli. "
                  "Con la GPU del Mac (MPS) un tassello richiede circa 10 s in scansione normale, 40-90 s in alta o "
                  "massima. Le unita' trovate vanno nel layer «Unita' da SAM», senza numero di US.")
        if not v:
            return
        self._ensure_site("Le US trovate da SAM appartengono a un sito: sceglilo una volta per il progetto.")
        self._sam_auto(None, title="SAM - tutta l'ortofoto", resolution=v["resolution"], thoroughness=v["thoroughness"],
                       min_area_m2=v["min_area"], exclusive=v["exclusive"])

    def toggle_photo_sam(self) -> None:
        if not self.photo_view.sam_mode and not self._sam_available(then=self.toggle_photo_sam):
            return
        self.photo_view.sam_mode = not self.photo_view.sam_mode
        self.statusBar().showMessage("Prompt SAM sulle foto " + ("attivi: clic sinistro sull'oggetto"
                                                                if self.photo_view.sam_mode else "disattivati"), 10000)

    def _photo_sam_prompt(self, camera, x, y, label) -> None:
        def job(p):
            from ..engine.visibility import read_photo

            img = read_photo(camera)[:, :, ::-1]
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
            self._sync_site_with_db(ask=self._project_open() and not quiet)
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

    def _publishable(self) -> tuple[list, list, int]:
        """Units drawn here with a US and not yet in PyArchInit, spot heights not yet written,
        and the number of units still without a US."""
        units = [u for u in self.units if u.us and (u.attributes or {}).get("gid") is None]
        no_us = sum(1 for u in self.units if not u.us and (u.attributes or {}).get("gid") is None)
        quote = [q for q in self.quote if q.get("gid") is None]
        return units, quote, no_us

    def pyarchinit_publish_drawings(self, what: str = "both") -> None:
        """Write the units drawn here into ``pyunitastratigrafiche`` and the spot heights into
        ``pyarchinit_quote``, in the site of the project. Written features keep their ``gid``:
        later changes go through *Salva modifiche in PyArchInit*, nothing is written twice."""
        if not self._need_pa() or not self._need_chunk():
            return
        site = self._ensure_site("Unita' e quote vengono scritte nel sito del progetto.")
        if not site:
            return
        units, quote, no_us = self._publishable()
        if what == "units":
            quote = []
        elif what == "quote":
            units = []
        if not units and not quote:
            self._warn("Niente da pubblicare: disegna (o segmenta con SAM) delle unita' con il numero di US, o delle "
                       "quote" + (f" ({no_us} unita' sono ancora senza US)" if no_us else "") + ".")
            return
        crs = self.chunk.crs

        def job(p):
            p(0.1, "Unita' stratigrafiche")
            gu = self.pa.publish_units(units, site, crs=crs) if units else []
            p(0.6, "Quote")
            gq = self.pa.publish_quote(quote, site, crs=crs) if quote else []
            return gu, gq

        def done(r):
            gu, gq = r
            for u, g in zip(units, gu):
                u.attributes["gid"] = g
            for q, g in zip(quote, gq):
                q["gid"] = g
            self._redraw_overlays()
            self._dirty(True)
            self.pa_pane.refresh()
            self._info("PyArchInit", f"Sito {site}: {len(gu)} poligoni scritti in pyunitastratigrafiche, {len(gq)} quote "
                       "in pyarchinit_quote." + (f"\n{no_us} unita' senza numero di US non sono state pubblicate."
                                                 if no_us and units else ""))

        self.run_task("Pubblicazione in PyArchInit", job, done, refresh=False, save=False)

    def pyarchinit_publish_units(self) -> None:
        self.pyarchinit_publish_drawings("units")

    def pyarchinit_publish_points(self) -> None:
        self.pyarchinit_publish_drawings("quote")

    def pyarchinit_load_units(self) -> None:
        if not self._need_pa():
            return
        site = self._ensure_site("Carica i poligoni US del sito del progetto.")
        if not site:
            return
        try:
            polys = self.pa.us_polygons(site, crs=self.chunk.crs if self.chunk else None)
        except Exception as exc:
            self._exc("Lettura delle unita' non riuscita", exc)
            return
        have = {int(u.attributes["gid"]) for u in self.units if (u.attributes or {}).get("gid") is not None}
        polys = [u for u in polys if int(u.attributes.get("gid", -1)) not in have]  # already here
        with self.ortho_view.batch():
            for u in polys:
                self.units.append(u)
                self._draw_unit(u, len(self.units) - 1, layer=f"US da PyArchInit - {site}")
        self.show_view(self.ortho_view)
        self._show_pane(self.left_tabs, self.layers_pane)
        self.statusBar().showMessage(f"{len(polys)} poligoni US caricati dal sito {site}", 10000)


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
