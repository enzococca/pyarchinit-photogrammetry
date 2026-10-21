"""Main window: menus, workspace, views and docked panes, all on the Python API
(every action is a call that a script could make through ``ppg.app.document``)."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path

import numpy as np
from PySide6.QtCore import QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QDockWidget, QFileDialog, QFrame,
                               QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QStackedWidget, QStatusBar, QTabWidget, QToolBar, QToolButton, QVBoxLayout,
                               QWidget)

from .. import __version__, _native, updates
from ..app import app as application
from ..core.document import Document
from . import i18n, theme
from .dialogs import (QUALITY, ConnectDialog, ErrorDialog, Field, GradualSelectionDialog, IfcPreviewDialog,
                      ImageDialog, ParamDialog, PatchDialog,
                      PublishDialog, QuotaDialog, SiteChoiceDialog, SiteDialog, TableDialog, UnitDialog, UsFormDialog,
                      choice, message, short_label)
from .i18n import decimal_comma, number, thousands_points, tr, translate
from .icons import app_icon, icon
from .image_views import RELIEF_KINDS, US_COLORS, PhotoView, RasterView
from .model_view import ModelView
from .panels import (ConsolePane, LayersPane, LogPane, PhotosPane, PhotosPanel, ProxiesPane, PyArchInitPane, ReferencePane,
                     SectionsPane, WorkspaceTree)
from .start_page import StartPage
from .undo import UndoHistory
from .update_dialog import UpdateChecker, UpdateDialog, error_text
from .worker import StatusProgress, TaskRunner

log = logging.getLogger("pyarchinit_photogrammetry.gui")
APP_TITLE = "PyArchInit Photogrammetry"
DOCS_URL = "https://github.com/enzococca/pyarchinit-photogrammetry"
DONATE_URL = "https://paypal.me/enzococca"  # donations to the project (README, site and *Aiuto* say the same)
TARGET_TYPES = [("aruco_4x4_50", "ArUco 4x4 (50 codici)"), ("aruco_4x4_250", "ArUco 4x4 (250 codici)"),
                ("aruco_5x5_100", "ArUco 5x5 (100 codici)"), ("aruco_6x6_250", "ArUco 6x6 (250 codici)"),
                ("apriltag_36h11", "AprilTag 36h11")]
QUOTE_LAYER = "Quote"  # spot heights (pyarchinit_quote)
CROP_LAYER = "Ritaglio"  # the polygon drawn with the crop tool
PATCH_LAYER = "Patch ortofoto"  # areas of the orthomosaic coloured from photos chosen by hand
STONES_LAYER = "Pietre"  # stone-by-stone drawing (archaeo.stones), per raster: orthomosaic or elevation


def _outlined(rgba: np.ndarray, polygon, bounds) -> np.ndarray:
    """A preview of an area with the outline of its polygon (world coordinates) drawn on it."""
    import cv2

    h, w = rgba.shape[:2]
    x0, y0, x1, y1 = bounds
    xy = np.asarray(polygon, float)[:, :2]
    px = np.column_stack([(xy[:, 0] - x0) / (x1 - x0) * w, (y1 - xy[:, 1]) / (y1 - y0) * h])
    out = np.ascontiguousarray(rgba.copy())
    cv2.polylines(out, [np.round(px * 4).astype(np.int32)], True, (232, 67, 147, 255), 2, cv2.LINE_AA, shift=2)
    return out


def _compass(deg: float) -> str:
    """The point of the compass of a direction in degrees from north (initials of the interface language)."""
    points = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"] if i18n.current() == "it" else \
        ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return points[int((deg % 360) / 45 + 0.5) % 8]


def _it(value: float, decimals: int = 0) -> str:
    """A number as the interface writes it: 1.234,56 in Italian, 1,234.56 in English."""
    return number(value, decimals)


class _StatusBar(QStatusBar):
    """The status bar: its messages in the language of the interface (also those of the engine)."""

    def showMessage(self, text: str, timeout: int = 0) -> None:  # noqa: N802 - Qt name
        super().showMessage(translate(text), timeout)
CROP_PRODUCTS = [("dem", "DEM"), ("orthomosaic", "Ortofoto"), ("model", "Modello"), ("point_cloud", "Nuvola densa")]
SAM_TOOLS = {"tap": "Tocco: clic sull'oggetto", "box": "Riquadro: un oggetto nel riquadro",
             "area": "Area: tutte le unita' nel riquadro",
             "stones": "Pietre: ogni pietra nel riquadro (disegno pietra per pietra)"}
SAM_MODELS = [("tiny", "Molto veloce (tiny)"), ("small", "Veloce (small)"), ("base_plus", "Accurato (base+)"),
              ("large", "Molto accurato (large)")]


CLASSIFIER_FILE = "classificatore.ppgclf"  # classifier trained in a chunk, kept in its data folder


class MainWindow(QMainWindow):
    def __init__(self, document: Document | None = None, autoconnect: bool = True, progress=None,
                 check_updates: bool | None = None):
        """``check_updates``: look for a new version a few seconds after start-up (once a day,
        when enabled in *Aiuto*); ``None`` = unless ``PPG_NO_UPDATE_CHECK`` is set."""
        super().__init__()
        self.setStatusBar(_StatusBar(self))
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
        self.stones: dict[str, list] = {}  # name of the raster -> outlines of its stones (polygons)
        self._sam_obj: dict | None = None  # SAM mask being edited (prompts, bounds, preview)
        self.last_report: dict | None = None
        self._started = document is not None
        self._task_done = False  # a processing step just ended: its changes are not an edit
        self.history: UndoHistory | None = None
        self._build_views()
        self._build_docks()
        self._build_actions()
        self._build_statusbar()
        self._load_plugins()
        self.set_document(document or application.document, progress=progress)
        QTimer.singleShot(0, self._autoconnect_pyarchinit)
        self.update_checker = UpdateChecker(self)
        self.update_checker.finished.connect(self._updates_checked)
        self._pending_update: Path | None = None  # new application swapped in when the window closes
        self._relaunch_on_close = False  # a new copy starts when this one closes (Vista > Lingua)
        self._timeline = None  # Strumenti > Fasi dello scavo, a window beside this one
        self._pottery_win = None  # Strumenti > Profilo ceramico: drawing, similar forms, save in PyArchInit
        self._pottery: dict | None = None  # the last profile measured (profile, measures, id_rep once saved)
        self._proposal_win = None  # Strumenti > Proponi US e rapporti
        self._proposal: dict | None = None
        if i18n.current() != "it":  # the menu bar of macOS is native: its menus are never shown as widgets
            i18n.translate_menus(self)
        if check_updates if check_updates is not None else not os.environ.get("PPG_NO_UPDATE_CHECK"):
            QTimer.singleShot(5000, self._auto_check_updates)

    # ================================================================ layout
    def _build_views(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setIconSize(QSize(18, 18))
        self.model_view = ModelView()
        self.model_view.points_selected.connect(self._points_selected)
        self.model_view.faces_selected.connect(self._faces_selected)
        self.model_view.marker_requested.connect(self._marker_picked_in_3d)
        self.model_view.section_requested.connect(self._section_in_3d)
        self.model_view.slice_requested.connect(self._slice_in_3d)
        self.model_view.move_marker_requested.connect(self._marker_moved_in_3d)
        self.model_view.photos_point_requested.connect(self.filter_photos_by_point)
        self.ortho_view = RasterView()
        self.photo_view = PhotoView()
        self.tabs.addTab(self.model_view, icon("view3d"), tr("Modello"))
        self.tabs.addTab(self.ortho_view, icon("view_ortho"), tr("Ortofoto / DEM"))
        self.tabs.addTab(self.photo_view, icon("view_photo"), tr("Foto"))
        self.start_page = StartPage()
        self.start_page.new_requested.connect(self.new_document)
        self.start_page.open_requested.connect(lambda: self.open_document())
        self.start_page.demo_requested.connect(self.open_demo)
        self.start_page.recent_requested.connect(self.open_document)
        self.start_page.doctor_requested.connect(self.show_doctor)
        self.start_page.log_requested.connect(self.open_log_folder)
        self.start_page.help_requested.connect(self.open_docs)
        self.start_page.donate_requested.connect(self.donate)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.start_page)
        self.stack.addWidget(self.tabs)
        self.setCentralWidget(self.stack)
        self.ortho_view.mouse_world.connect(self._show_coords)
        self.ortho_view.polygon_finished.connect(self._unit_drawn)
        self.ortho_view.crop_finished.connect(self._crop_drawn)
        self.ortho_view.crop_requested.connect(self.crop_with_selection)
        self.ortho_view.patch_finished.connect(self._patch_drawn)
        self.ortho_view.dem_edit_finished.connect(self._dem_edit_drawn)
        self.ortho_view.dem_edit_requested.connect(self._dem_edit_selected)
        self.ortho_view.area_finished.connect(self._area_drawn)
        self.ortho_view.chart_finished.connect(self._chart_drawn)
        self.ortho_view.area_requested.connect(self._area_selected)
        self.ortho_view.patch_requested.connect(self.patch_with_selection)
        self.ortho_view.line_finished.connect(self._section_drawn)
        self.ortho_view.measured.connect(lambda t: (self.statusBar().showMessage(t, 30000),
                                                    self.log_pane.info(t)))
        self.ortho_view.quota_clicked.connect(self._quota_clicked)
        self.ortho_view.marker_clicked.connect(self._marker_clicked_on_raster)
        self.ortho_view.photos_at_point.connect(lambda x, y: self.filter_photos_by_point(self._raster_point(x, y)))
        self.ortho_view.photos_in_area_requested.connect(self._photos_in_selected_area)
        self.ortho_view.sam_click.connect(self._sam_click)
        self.ortho_view.sam_rect.connect(self._sam_rect)
        self.ortho_view.sam_key.connect(self._sam_key)
        self.ortho_view.selection_changed.connect(self._selection_changed)
        self.ortho_view.delete_requested.connect(lambda: self.delete_selected())
        self.ortho_view.merge_requested.connect(lambda: self.merge_selected())
        self.photo_view.markers_changed.connect(self._markers_edited)
        self.photo_view.navigate.connect(self.photo_step)
        self.photo_view.mask_changed.connect(lambda cam: (self._dirty(True), self.statusBar().showMessage(
            tr("Maschera di {0} aggiornata", cam.label), 5000)))
        self.photo_view.marker_created.connect(lambda m: self.filter_photos_by_markers([m.key]))
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
        self.reference.scalebar_requested.connect(self.new_scalebar)
        self.reference.scalebar_import_requested.connect(self.import_scalebars)
        self.reference.filter_photos_requested.connect(self.filter_photos_by_markers)
        self.reference.refine_requested.connect(self.refine_markers)
        self.reference.merge_requested.connect(self.merge_markers)
        self.reference.move3d_requested.connect(self.move_marker_in_3d)
        self.reference.remove_requested.connect(self.remove_markers)
        self.reference.precision_requested.connect(self.estimate_precision)
        self.photos = PhotosPane()
        self.photos.thumbs_progress.connect(self._thumbs_progress)
        self.photos.open_camera.connect(self.open_photo)
        self.photos.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.photos.customContextMenuRequested.connect(self._photos_menu)
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
        self.left_tabs.addTab(self.workspace, icon("workspace"), tr("Area di lavoro"))
        self.left_tabs.addTab(self.reference, icon("reference_pane"), tr("Riferimenti"))
        self.layers_pane = LayersPane()
        self.layers_pane.set_view(self.ortho_view)
        self.layers_pane.attribute_edited.connect(self._attribute_edited)
        self.pa_pending: dict[int, object] = {}  # gid -> UnitPolygon edited, to write back to PyArchInit
        self.pa_deleted: set[int] = set()  # gid of polygons deleted or merged away here
        self.pa_geom_changed: set[int] = set()  # gid whose geometry changed (merge, part removed)
        self.b_pa_save = QPushButton(icon("publish"), tr("Salva modifiche in PyArchInit"))
        self.b_pa_save.clicked.connect(self.save_pending_units)
        self.b_pa_save.hide()
        self.layers_pane.layout().addWidget(self.b_pa_save)
        self.left_tabs.addTab(self.layers_pane, icon("slice"), "Layer")
        self.layers_pane.us_form_requested.connect(self._us_form_from_feature)
        self.proxies_pane = ProxiesPane()
        self.left_tabs.addTab(self.proxies_pane, icon("us"), tr("Proxy US"))
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
        self.sections_pane.elevation_requested.connect(lambda sdict: self.build_elevation(sdict))
        self.sections_pane.delete_requested.connect(self._delete_section)
        self.sections_pane.folder_requested.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._sections_dir()))))
        self.ortho_view.feature_clicked.connect(self._feature_clicked)
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.setIconSize(QSize(16, 16))
        self.photos_panel = PhotosPanel(self.photos)
        self.photos_panel.step.connect(self.photo_step)
        self.photos_panel.with_markers.connect(self.filter_photos_with_markers)
        self.bottom_tabs.addTab(self.photos_panel, icon("photos"), tr("Foto"))
        self.bottom_tabs.addTab(self.sections_pane, icon("line"), tr("Sezioni"))
        self.bottom_tabs.addTab(self.console, icon("console"), tr("Console Python"))
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
        self._act(m, tr("Nuovo progetto"), self.new_document, QKeySequence.StandardKey.New, icon_name="new")
        self._act(m, tr("Apri..."), self.open_document, QKeySequence.StandardKey.Open, icon_name="open")
        self.recent_menu = m.addMenu(icon("recent"), tr("Progetti recenti"))
        self._act(m, tr("Salva"), self.save_document, QKeySequence.StandardKey.Save, icon_name="save")
        self._act(m, tr("Salva con nome..."), lambda: self.save_document(ask=True), QKeySequence.StandardKey.SaveAs,
                  icon_name="save_as")
        m.addSeparator()
        exp = self.export_menu = m.addMenu(icon("export"), tr("Esporta"))
        self._act(exp, tr("Nuvola di punti (LAS/COPC/PLY)..."), lambda: self.export("cloud"), icon_name="point_cloud")
        self._act(exp, tr("Modello (OBJ/PLY/GLB)..."), lambda: self.export("model"), icon_name="model")
        self._act(exp, tr("Modello in Cesium 3D Tiles..."), self.export_3dtiles, processing=True, icon_name="globe",
                  tip=tr("Cartella con tileset.json e i tasselli GLB: il modello sul globo di CesiumJS"))
        self._act(exp, tr("Modello per ArcGIS e Cesium (I3S, .slpk)..."), self.export_i3s, processing=True,
                  icon_name="globe", tip=tr("Pacchetto I3S (Integrated Mesh 1.7): livelli di dettaglio con una texture "
                                            "per nodo, per ArcGIS Pro e Online, CesiumJS e deck.gl"))
        self._act(exp, tr("Modello leggero con normal map (GLB/OBJ)..."), self.export_light_model, processing=True,
                  icon_name="model", tip=tr("Il modello con poche facce e due texture (colore e normal map) cotte dal "
                                            "modello completo: per il web, Sketchfab e i motori di gioco"))
        self._act(exp, tr("Video di sorvolo (MP4)..."), self.export_video, processing=True, icon_name="video",
                  tip=tr("Una camera che gira attorno al modello o alla nuvola, per presentazioni e musei"))
        self._act(exp, "DEM (GeoTIFF/COG)...", lambda: self.export("dem"), icon_name="dem")
        self._act(exp, tr("Ortofoto (GeoTIFF/COG)..."), lambda: self.export("ortho"), icon_name="ortho")
        self._act(exp, tr("Camere (CSV / COLMAP)..."), lambda: self.export("cameras"), icon_name="cameras")
        self._act(exp, tr("Riferimenti (CSV)..."), lambda: self.export("reference"), icon_name="reference")
        self._act(exp, tr("Pacchetto QField (tablet in trincea)..."), self.export_qfield, processing=True,
                  icon_name="export", tip=tr("Ortofoto, DEM, unita' e quote del chunk in una cartella per QField: si "
                                             "disegna sul tablet, poi File > Importa > Modifiche da QField"))
        exp.addSeparator()
        self._act(exp, tr("Ritagliati con un poligono o un riquadro (DEM, ortofoto, modello, nuvola)..."),
                  self.start_crop, icon_name="crop")
        imp = m.addMenu(icon("open"), tr("Importa"))
        self._act(imp, tr("Scansioni laser (E57, PTX)..."), self.import_scans, processing=True, icon_name="scan",
                  tip=tr("Una o piu' scansioni come nuvola densa di un nuovo chunk; poi Allinea chunk (punti) e Colori "
                         "dalle foto"))
        self._act(imp, tr("Progetto Metashape (.psx)..."), lambda: self.import_external("metashape"), processing=True,
                  icon_name="open")
        self._act(imp, tr("Modello e texture dal progetto Metashape (reimporta)..."), self.reimport_metashape_model,
                  processing=True, icon_name="model_textured",
                  tip=tr("Rilegge dal .psx il modello con colori, confidenza e texture (progetti importati prima)"))
        self._act(imp, tr("Progetto WebODM / ODM (cartella o all.zip)..."), lambda: self.import_external("webodm"),
                  processing=True, icon_name="open")
        self._act(imp, tr("Modifiche da QField..."), self.import_qfield, icon_name="open",
                  tip=tr("Le unita' e le quote disegnate o corrette sul tablet tornano nel chunk"))
        self._act(imp, tr("Sessione Photo3D..."), lambda: self.import_external("photo3d"), processing=True,
                  icon_name="photos")
        self._act(m, tr("Genera report HTML..."), self.generate_report, processing=True, icon_name="report")
        m.addSeparator()
        self._act(m, tr("Crea e apri progetto demo..."), self.open_demo, processing=True, icon_name="demo")
        self._act(m, tr("Pagina iniziale"), self.show_start_page, icon_name="home_view")
        m.addSeparator()
        self._act(m, tr("Esci"), self.close, QKeySequence.StandardKey.Quit, icon_name="quit")
        # ------------------------------------------------------------ Edit
        m = mb.addMenu(tr("&Modifica"))
        self.a_undo = self._act(m, tr("Annulla", context="undo"), self.undo, QKeySequence.StandardKey.Undo,
                                icon_name="undo",
                                tip=tr("Annulla l'ultima modifica: disegni, quote, sezioni, marker, barre di scala, "
                                       "patch, regione, punti della nuvola e facce del modello"))
        self.a_redo = self._act(m, tr("Ripeti"), self.redo, QKeySequence.StandardKey.Redo, icon_name="redo")
        self.a_undo.setEnabled(False)
        self.a_redo.setEnabled(False)
        # ------------------------------------------------------------ Workflow
        m = mb.addMenu(tr("Flusso di &lavoro"))
        self._act(m, tr("Aggiungi foto..."), self.add_photos, processing=True, icon_name="photos")
        self._act(m, tr("Aggiungi cartella..."), lambda: self.add_photos(folder=True), processing=True,
                  icon_name="folder_photos")
        self._act(m, tr("Aggiungi video..."), self.add_video, processing=True, icon_name="video",
                  tip=tr("Fotogrammi scelti dal video (nitidi, con la sovrapposizione giusta), con il GPS dei droni "
                         "DJI"))
        self._act(m, tr("Aggiungi foto 360°..."), self.add_panoramas, processing=True, icon_name="panorama",
                  tip=tr("Foto sferiche (equirettangolari) divise nelle sei facce di un cubo, allineate come un rig"))
        self._act(m, tr("Ricollega foto (altra cartella)..."), lambda: self.relink_photos(), icon_name="relink",
                  tip=tr("Collega le foto ai file con lo stesso nome in un'altra cartella (copia su un altro disco, "
                         "cartella spostata o tenuta «solo online» da Dropbox)"))
        self._act(m, tr("Aggiungi chunk"), self.add_chunk, icon_name="chunk_add")
        self._act(m, tr("Allinea chunk..."), self.align_chunks, processing=True, icon_name="chunk")
        self._act(m, tr("Unisci chunk..."), self.merge_chunks, processing=True, icon_name="merge")
        self._act(m, tr("Colori dalle foto (scansioni)..."), self.colour_scan, processing=True, icon_name="colour",
                  tip=tr("Colora la nuvola del chunk attivo (una scansione laser) con le foto allineate di un altro "
                         "chunk"))
        m.addSeparator()
        self._act(m, tr("Rileva marker codificati..."), self.detect_markers, processing=True, icon_name="markers")
        self._act(m, tr("Affina le bandierine blu dei marker"), lambda: self.refine_markers(), processing=True,
                  icon_name="refine", tip=tr("Cerca nelle foto, per correlazione, il punto di ogni posizione stimata "
                                             "(bandierina blu) dei marker misurati in almeno una foto"))
        self._act(m, tr("Allinea foto..."), self.align_photos, processing=True, icon_name="align")
        self._act(m, tr("Importa riferimenti..."), self.import_reference, icon_name="reference")
        self._act(m, tr("Aggiorna georeferenziazione"), self.update_transform, processing=True, icon_name="georef")
        self._act(m, tr("Nuova barra di scala (distanza tra due marker)..."), lambda: self.new_scalebar(),
                  icon_name="scalebar")
        self._act(m, tr("Importa barre di scala (CSV)..."), self.import_scalebars, icon_name="scalebar")
        self._act(m, tr("Ottimizza camere"), self.optimize_cameras, processing=True, icon_name="optimize")
        self._act(m, tr("Elaborazione batch..."), self.batch_process, processing=True, icon_name="batch",
                  tip=tr("Passi in sequenza su piu' chunk, con salvataggio dopo ogni passo e ripresa se si interrompe"))
        self._act(m, tr("Controllo prima di lasciare il sito"), self.field_check, processing=True,
                  icon_name="fieldcheck", tip=tr("Subito dopo l'allineamento: aree viste da meno di tre foto, foto non "
                                                 "allineate o sfocate, marker in troppo poche foto"))
        self._act(m, tr("Stima la precisione"), self.estimate_precision, processing=True, icon_name="precision",
                  tip=tr("Precisione di camere, marker e punti di legame dalla covarianza del bundle adjustment; "
                         "mappa della precisione del DEM"))
        m.addSeparator()
        self._act(m, tr("Costruisci nuvola densa..."), self.build_dense, processing=True, icon_name="dense")
        self._act(m, tr("Ritaglia nuvola densa..."), self.crop_cloud, processing=True, icon_name="eraser")
        cl = m.addMenu(icon("clean"), tr("Pulizia e confidenza"))
        self._act(cl, tr("Calcola la confidenza (nuvola densa, modello)..."), self.compute_confidence, processing=True,
                  icon_name="confidence")
        self._act(cl, tr("Mostra la confidenza nella vista 3D"), self.show_confidence, icon_name="confidence")
        cl.addSeparator()
        self._act(cl, tr("Pulisci la nuvola densa..."), self.clean_cloud, processing=True, icon_name="clean")
        self._act(cl, tr("Pulisci il modello..."), self.clean_model, processing=True, icon_name="clean")
        self._act(cl, tr("Pulisci i punti di legame (selezione graduale)..."), self.clean_tie_points, processing=True,
                  icon_name="tie_clean")
        cl.addSeparator()
        self._act(cl, tr("Seleziona con il lazo nella vista 3D (elimina, tieni)"), self.lasso_edit, icon_name="select")
        self._act(cl, tr("Ritaglia con un poligono o un riquadro..."), self.start_crop, icon_name="crop")
        self._act(m, tr("Costruisci modello..."), self.build_model, processing=True, icon_name="model")
        self._act(m, tr("Costruisci texture..."), self.build_texture, processing=True, icon_name="texture")
        self._act(m, tr("Costruisci DEM..."), self.build_dem, processing=True, icon_name="dem")
        self._act(m, tr("Costruisci ortofoto..."), self.build_ortho, processing=True, icon_name="ortho")
        rm = m.addMenu(icon("crop"), tr("Regione di ricostruzione"))
        self._act(rm, tr("Attorno ai punti di legame"), self.region_reset, processing=True,
                  tip=tr("Depth map, nuvola densa e modello limitati al riquadro giallo della vista 3D"))
        self._act(rm, tr("Ruotata come la vista 3D"), self.region_rotate_to_view, processing=True)
        self._act(rm, tr("Sul poligono selezionato (pianta)"), self.region_from_selection, processing=True)
        self._act(rm, tr("Modifica i valori..."), self.region_edit, processing=True)
        self._act(rm, tr("Togli la regione"), self.region_clear, processing=True)
        self._act(m, tr("Prospetto o sviluppo cilindrico..."), self.build_elevation, processing=True,
                  icon_name="elevation",
                  tip=tr("Ortofoto e rilievo su un piano (muri) o su un cilindro (pozzi, torri)"))
        m.addSeparator()
        self._act(m, tr("Elabora tutto..."), self.process_all, "Ctrl+R", processing=True, icon_name="process_all")
        # ------------------------------------------------------------ Tools
        m = mb.addMenu(tr("&Strumenti"))
        tm = m.addMenu(icon("print"), "Target")
        self._act(tm, tr("Stampa fogli di target..."), self.print_targets, icon_name="print")
        self._act(tm, tr("Tappetino per turntable..."), self.print_turntable, icon_name="turntable")
        self._act(tm, tr("Barra di scala stampabile..."), self.print_scalebar, icon_name="scalebar")
        self._act(m, tr("Calibra i colori delle foto..."), self.calibrate_colours, processing=True, icon_name="colour",
                  tip=tr("Esposizione di ogni foto e vignettatura degli obiettivi, corrette in ortofoto e texture"))
        dm = m.addMenu(icon("diagnostics"), tr("Diagnostica dell'allineamento"))
        self._act(dm, tr("Grafo delle corrispondenze..."), self.match_graph, icon_name="graph",
                  tip=tr("Quali foto si collegano a quali: gruppi separati e foto isolate"))
        self._act(dm, tr("Corrispondenze tra due foto..."), lambda: self.pair_matches(), icon_name="matches",
                  tip=tr("Le due foto selezionate nel pannello Foto, con i punti che le collegano"))
        self.a_residuals = QAction(icon("residuals"), tr("Residui dei punti di legame nella foto"), self,
                                   checkable=True)
        self.a_residuals.setStatusTip(tr("Nella vista Foto: dove ogni punto di legame e' stato misurato e dove lo "
                                         "rimette il modello (frecce ingrandite 20 volte)"))
        self.a_residuals.toggled.connect(self._residuals_toggled)
        dm.addAction(self.a_residuals)
        self._act(dm, tr("Calibrazione della camera..."), self.camera_calibration, icon_name="lens",
                  tip=tr("Parametri dei sensori, calibrazione fissa e grafico della distorsione"))
        km = m.addMenu(icon("mask"), tr("Maschere delle foto"))
        self.mask_actions: dict[str, QAction] = {}
        mgroup = QActionGroup(self)
        for mode, label in ((None, "Nessun disegno (marker)"),
                            ("mask", "Disegna la maschera sulla foto (clic sui vertici, doppio clic chiude)"),
                            ("unmask", "Togli la maschera (poligono; anche Maiusc + doppio clic)")):
            a = QAction(label, self, checkable=True)
            a.setChecked(mode is None)
            a.triggered.connect(lambda _=False, md=mode: self.set_mask_mode(md))
            mgroup.addAction(a)
            km.addAction(a)
            self.mask_actions[str(mode)] = a
        self.a_show_masks = QAction(tr("Mostra le maschere"), self, checkable=True)
        self.a_show_masks.setChecked(True)
        self.a_show_masks.toggled.connect(self._show_masks)
        km.addAction(self.a_show_masks)
        km.addSeparator()
        self._act(km, tr("Importa maschere (immagini)..."), self.masks_import, processing=True)
        self._act(km, tr("Maschere dal canale alfa delle foto"), lambda: self.masks_import(alpha=True), processing=True)
        self._act(km, tr("Maschere dallo sfondo (foto senza l'oggetto)..."), self.masks_from_background,
                  processing=True)
        self._act(km, tr("Maschere da un colore (fondale)..."), self.masks_from_colour, processing=True)
        self._act(km, tr("Maschere dal modello (resta solo l'oggetto)"), self.masks_from_model, processing=True)
        self._act(km, tr("Maschere con SAM 2.1 (l'oggetto al centro della foto)"), self.masks_with_sam, processing=True)
        km.addSeparator()
        self._act(km, tr("Esporta maschere..."), self.masks_export)
        self._act(km, tr("Rimuovi le maschere delle foto selezionate"), lambda: self.masks_remove(selected=True))
        self._act(km, tr("Rimuovi tutte le maschere"), lambda: self.masks_remove(selected=False))
        dm = m.addMenu(icon("polygon"), tr("Disegno su ortofoto"))
        self.draw_group = QActionGroup(self)
        self.draw_actions: dict[str, QAction] = {}
        for mode, label, ic in (("pan", "Sposta / interroga", "pan"), ("select", "Seleziona", "select"),
                                ("polygon", "Disegna unita' stratigrafica", "polygon"),
                                ("line", "Traccia sezione", "line"), ("measure", "Misura distanza", "measure"),
                                ("area", "Misura area e superficie 3D", "area"),
                                ("quota", "Quota puntuale (pyarchinit_quote)", "point"),
                                ("marker", "Marker: clic su un punto, proiettato sulle foto che lo vedono", "marker"),
                                ("crop", "Ritaglio: riquadro o poligono (esporta o ritaglia i prodotti)", "crop"),
                                ("patch", "Patch dell'ortofoto: assegna le immagini a un'area", "patch"),
                                ("dem_edit", "Modifica DEM: area da riempire dal terreno attorno", "dem"),
                                ("sam", "Segment Anything (SAM)", "sam")):
            a = QAction(icon(ic), label, self, checkable=True)
            a.setChecked(mode == "pan")
            a.setStatusTip(label)
            a.triggered.connect(lambda _=False, md=mode: self.set_draw_mode(md))
            self.draw_group.addAction(a)
            dm.addAction(a)
            self.draw_actions[mode] = a
        # drawing options: freehand units, snapping to vertices / segments of the visible layers
        self.a_freehand = QAction(icon("freehand"), tr("Mano libera"), self, checkable=True)
        self.a_freehand.setToolTip(tr("Disegno a mano libera delle unita': tieni premuto, traccia e rilascia per "
                                      "chiudere"))
        self.a_freehand.setChecked(self.settings.value("draw/freehand", "false") in ("true", True))
        self.a_freehand.toggled.connect(lambda _on: self._apply_draw_options())
        dm.addAction(self.a_freehand)
        self.snap_menu = QMenu("Snap", self)
        self.a_snap = QAction(icon("snap"), "Snap", self, checkable=True)
        self.a_snap.setToolTip(tr("Aggancia i punti ai vertici e/o ai segmenti dei layer visibili (scegli dal menu)"))
        self.a_snap.setChecked(self.settings.value("draw/snap", "false") in ("true", True))
        self.a_snap.setMenu(self.snap_menu)
        self.a_snap_vertex = self.snap_menu.addAction(tr("Vertici"))
        self.a_snap_segment = self.snap_menu.addAction(tr("Segmenti"))
        for a, key in ((self.a_snap_vertex, "draw/snap_vertex"), (self.a_snap_segment, "draw/snap_segment")):
            a.setCheckable(True)
            a.setChecked(self.settings.value(key, "true") in ("true", True))
        self.snap_menu.addSeparator()
        tol_group = QActionGroup(self)
        self.snap_tol_actions = {}
        for px in (6, 12, 20):
            a = self.snap_menu.addAction(tr("Tolleranza {0} px", px))
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
            a = self.sam_menu.addAction(tr(label))
            a.setCheckable(True)
            a.setChecked(key == current_tool)
            a.triggered.connect(lambda _=False, k=key: self._set_sam_tool(k))
            sam_tools.addAction(a)
            self.sam_tool_actions[key] = a
        self.sam_menu.addSeparator()
        self._act(self.sam_menu, tr("Tutta l'ortofoto (scansione a tasselli)..."), lambda: self.sam_whole_orthomosaic(),
                  processing=True, icon_name="sam")
        self._act(self.sam_menu, tr("Proponi US e rapporti (Extended Matrix)..."), self.propose_units,
                  processing=True, icon_name="timeline")
        model_menu = self.sam_menu.addMenu(tr("Modello"))
        model_group = QActionGroup(self)
        self.sam_model_actions = {}
        for key, label in SAM_MODELS:
            a = model_menu.addAction(tr(label))
            a.setCheckable(True)
            a.setChecked(key == self.settings.value("sam/model", "small"))
            a.triggered.connect(lambda _=False, k=key: self._set_sam_model(k))
            model_group.addAction(a)
            self.sam_model_actions[key] = a
        self.draw_actions["sam"].setMenu(self.sam_menu)
        # sections: what is cut (model or dense cloud) and which point classes
        self.section_menu = QMenu(tr("Sezione"), self)
        self._act(self.section_menu, tr("Opzioni delle sezioni (modello o nuvola, classi)..."), self.section_options,
                  icon_name="line")
        self.draw_actions["line"].setMenu(self.section_menu)
        self.draw_actions["sam"].setToolTip(tr("Segment Anything 2.1: clic su un oggetto (tocco), un riquadro per un "
                                               "oggetto o per tutte le unita' di un'area; dal menu anche tutta "
                                               "l'ortofoto"))
        self.ortho_view.sam_tool = current_tool if current_tool in SAM_TOOLS else "tap"
        dm.addSeparator()
        self.a_sel_delete = self._act(dm, tr("Elimina selezionati"), lambda: self.delete_selected(), icon_name="delete")
        self.a_sel_delete.setToolTip(tr("Elimina gli elementi selezionati (tasto Canc nella modalita' Seleziona)"))
        self.a_sel_merge = self._act(dm, tr("Unisci selezionati"), lambda: self.merge_selected(), icon_name="merge")
        self.a_sel_merge.setToolTip(tr("Unisce le unita' selezionate in un solo poligono, o linee contigue dello "
                                       "stesso layer"))
        for a in (self.a_sel_delete, self.a_sel_merge):
            a.setEnabled(False)
        self.a_clear_drawings = self._act(dm, tr("Cancella disegni"), self.clear_drawings, icon_name="eraser")
        self._act(dm, tr("Ritaglia o esporta con il poligono selezionato..."), self.crop_with_selection,
                  icon_name="crop")
        pm = m.addMenu(icon("patch"), tr("Patch dell'ortofoto (assegna immagini)"))
        self._act(pm, tr("Disegna un'area e scegli le sue foto"), self.start_patch, icon_name="patch")
        self._act(pm, tr("Assegna immagini al poligono selezionato..."), self.patch_with_selection, icon_name="patch")
        self.a_patch_update = self._act(pm, tr("Aggiorna l'ortofoto con le patch"), self.update_ortho_patches,
                                        processing=True, icon_name="patch_update")
        self._act(pm, tr("Rimuovi tutte le patch..."), self.clear_ortho_patches, processing=True,
                  icon_name="patch_remove")
        m.addSeparator()
        self.a_contours = self._act(m, tr("Curve di livello..."), self.contours, processing=True, icon_name="contours")
        self.a_volume = self._act(m, tr("Volume scavo / riporto..."), self.compute_volume, processing=True,
                                  icon_name="volume")
        self._act(m, tr("Proponi US e rapporti (Extended Matrix)..."), self.propose_units, processing=True,
                  icon_name="timeline",
                  tip=tr("Numero di US, tipo e misure per le unita' senza numero (SAM o disegnate) e i rapporti "
                         "stratigrafici dei loro contatti, da controllare e scrivere nelle schede US"))
        self._act(m, tr("Fasi dello scavo (volumi e rapporti)..."), self.excavation_phases, processing=True,
                  icon_name="timeline",
                  tip=tr("Un chunk con il DEM per ogni rilievo dello scavo: volume asportato da una fase all'altra e "
                         "per US, rapporti stratigrafici suggeriti dall'ordine in cui le US sono state scavate"))
        self._act(m, tr("Pendenza del DEM"), lambda: self.terrain_raster("slope"), processing=True, icon_name="slope",
                  tip=tr("Raster della pendenza in gradi, mostrato subito (gradi sotto il cursore)"))
        self._act(m, tr("Esposizione del DEM"), lambda: self.terrain_raster("aspect"), processing=True,
                  icon_name="aspect",
                  tip=tr("Raster dell'esposizione (verso dove scende il terreno), gradi dal nord"))
        self._act(m, tr("Calibra il colore con la tabella colori..."), self.calibrate_colour_chart, processing=True,
                  icon_name="colour",
                  tip=tr("Un riquadro attorno a una ColorChecker fotografata nello scavo: i colori Munsell misurati "
                         "con Misura area diventano confrontabili tra giorni e camere diversi"))
        self._act(m, tr("Visualizzazioni del rilievo..."), lambda: self.relief_visualisation(), processing=True,
                  icon_name="relief",
                  tip=tr("Sky-view factor, openness, rilievo locale e multiscala, ombreggiatura da piu' direzioni: "
                         "muri, fossati e terrazzamenti appena accennati nel DEM"))
        self._act(m, tr("Opzioni delle sezioni..."), self.section_options, icon_name="line")
        self.a_slice = self._act(m, tr("Pianta a quota (slice orizzontale)..."), self.plan_slice, processing=True,
                                 icon_name="slice")
        self._act(m, tr("Profilo ceramico da mesh..."), self.pottery_profile, processing=True, icon_name="pottery")
        self._act(m, tr("Proxy 3D delle US..."), self.create_proxies, processing=True, icon_name="us")
        m.addSeparator()
        cm = m.addMenu(icon("classify"), tr("Classificazione della nuvola"))
        self._act(cm, tr("Classificazione automatica (regole)"), self.classify, processing=True, icon_name="classify")
        self._act(cm, tr("Mostra la nuvola per classi"), self.show_classes, icon_name="classify")
        self._act(cm, tr("Correggi a mano con il lazo (vista 3D)"), self.classify_with_lasso, icon_name="select")
        self._act(cm, tr("Classi dalle US (poligoni)..."), self.units_to_classes, processing=True, icon_name="us")
        cm.addSeparator()
        self._act(cm, tr("Addestra il classificatore sui punti corretti..."), self.train_classifier, processing=True,
                  icon_name="classify")
        self._act(cm, tr("Classifica con il classificatore addestrato..."), self.apply_classifier, processing=True,
                  icon_name="classify")
        self._act(cm, tr("Carica un classificatore (.ppgclf)..."), self.load_classifier, icon_name="open")
        self._act(cm, tr("Salva il classificatore..."), self.save_classifier, icon_name="save")
        self._act(cm, tr("Dimentica le correzioni a mano"), self.clear_manual_labels, icon_name="clear")
        sm = m.addMenu(icon("sam"), "Segment Anything 2.1")
        for key, label in SAM_TOOLS.items():
            self._act(sm, tr("Ortofoto - {0}", tr(label)), lambda _=False, k=key: (self._set_sam_tool(k),
                                                                               self.set_draw_mode("sam")),
                      icon_name="sam")
        self._act(sm, tr("Ortofoto - tutta, scansione a tasselli..."), lambda: self.sam_whole_orthomosaic(),
                  processing=True,
                  icon_name="sam")
        self._act(sm, tr("Prompt sulle foto (attiva/disattiva)"), self.toggle_photo_sam, icon_name="view_photo")
        self._act(sm, tr("Esporta il disegno delle pietre..."), self.export_stones_drawing, icon_name="export",
                  tip=tr("Le pietre disegnate con lo strumento SAM «Pietre» sull'immagine mostrata: DXF (layer "
                         "PIETRE), SVG in scala o GeoJSON"))
        sm.addSeparator()
        self._act(sm, tr("Installa / aggiorna SAM 2.1..."), lambda: self.install_sam(), processing=True,
                  icon_name="export")
        self._act(sm, tr("Rimuovi SAM 2.1"), self.remove_sam, icon_name="clear")
        m.addSeparator()
        self._act(m, tr("Esporta IFC (BIM)..."), self.export_ifc, processing=True, icon_name="ifc")
        self._act(m, tr("Esegui script Python..."), self.run_script, icon_name="script")
        self._act(m, tr("Nodi di rete..."), self.show_nodes, icon_name="network",
                  tip=tr("Altri computer che lavorano a questo progetto dalla cartella condivisa"))
        self._act(m, tr("Plugin..."), self.show_plugins, icon_name="plugin",
                  tip=tr("I plugin installati e quelli del repository dei plugin: installa, attiva, disattiva"))
        # ------------------------------------------------------------ PyArchInit
        m = mb.addMenu("&PyArchInit")
        self._act(m, tr("Connetti (config.cfg, SQLite o PostgreSQL)..."), lambda: self.pyarchinit_connect(ask=True),
                  icon_name="database")
        self._act(m, tr("Usa il database demo"), self.pyarchinit_demo, icon_name="demo_db")
        self._act(m, tr("Sito del progetto..."), lambda: self.choose_project_site(), icon_name="site")
        self._act(m, tr("Nuovo sito..."), self.pyarchinit_new_site, icon_name="site")
        self._act(m, tr("Nuova scheda US..."), lambda: self.open_us_form(self.project_site(), "1", "", "US"),
                  icon_name="us")
        self._act(m, tr("Aggiorna struttura del database..."), self.pyarchinit_upgrade, icon_name="database")
        self.a_autoconnect = QAction(tr("Connetti all'avvio al database di PyArchInit"), self, checkable=True)
        self.a_autoconnect.setChecked(self.settings.value("pa_autoconnect", "true") in ("true", True, 1, "1"))
        self.a_autoconnect.toggled.connect(lambda on: self.settings.setValue("pa_autoconnect", "true" if on else "false"))
        m.addAction(self.a_autoconnect)
        m.addSeparator()
        self._act(m, tr("Pubblica ortofoto / DEM / modello..."), self.pyarchinit_publish, processing=True,
                  icon_name="publish")
        self._act(m, tr("Pubblica unita' e quote disegnate"), self.pyarchinit_publish_drawings, processing=True,
                  icon_name="publish")
        self._act(m, tr("Pubblica solo le unita' (pyunitastratigrafiche)"), self.pyarchinit_publish_units,
                  processing=True, icon_name="publish")
        self._act(m, tr("Pubblica solo le quote (pyarchinit_quote)"), self.pyarchinit_publish_points, processing=True,
                  icon_name="publish")
        self._act(m, tr("Carica poligoni US sull'ortofoto"), self.pyarchinit_load_units, icon_name="load")
        self._act(m, tr("Aggiorna pannello"), lambda: self.pa_pane.refresh(), icon_name="refresh")
        # ------------------------------------------------------------ QGIS
        m = mb.addMenu("&QGIS")
        self.a_qgis_open = self._act(m, tr("Apri in QGIS (collegamento dal vivo)"), self.open_in_qgis,
                                     icon_name="view_ortho")
        self.a_qgis_live = QAction(tr("Aggiorna QGIS mentre disegno"), self, checkable=True)
        self.a_qgis_live.setChecked(False)
        self.a_qgis_live.toggled.connect(lambda on: self._schedule_qgis() if on else None)
        m.addAction(self.a_qgis_live)
        self._act(m, tr("Apri la cartella dei layer per QGIS"), lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self._qgis_dir()))), icon_name="folder")
        # ------------------------------------------------------------ View
        m = mb.addMenu(tr("&Vista"))
        for d in (self.d_work, self.d_photos, self.d_pa):
            m.addAction(d.toggleViewAction())
        m.addSeparator()
        self._act(m, tr("Area di lavoro"), lambda: self._show_pane(self.left_tabs, self.workspace),
                  icon_name="workspace")
        self._act(m, tr("Riferimenti"), lambda: self._show_pane(self.left_tabs, self.reference),
                  icon_name="reference_pane")
        self._act(m, "Layer", lambda: self._show_pane(self.left_tabs, self.layers_pane), icon_name="slice")
        self._act(m, tr("Foto (miniature)"), lambda: self._show_pane(self.bottom_tabs, self.photos_panel),
                  icon_name="photos")
        self._act(m, tr("Sezioni"), lambda: self._show_pane(self.bottom_tabs, self.sections_pane), icon_name="line")
        self._act(m, tr("Console Python"), lambda: self._show_pane(self.bottom_tabs, self.console), "Ctrl+Shift+P",
                  icon_name="console")
        self._act(m, "Log", lambda: self._show_pane(self.bottom_tabs, self.log_pane), icon_name="log")
        m.addSeparator()
        self._act(m, tr("Modello"), lambda: self.show_view(self.model_view), "F2", icon_name="view3d")
        self._act(m, tr("Ortofoto / DEM"), lambda: self.show_view(self.ortho_view), "F3", icon_name="view_ortho")
        self._act(m, tr("Foto"), lambda: self.show_view(self.photo_view), "F4", icon_name="view_photo")
        m.addSeparator()
        tmenu = m.addMenu(icon("theme"), tr("Tema"))
        tgroup = QActionGroup(self)
        self.theme_actions = {}
        for mode, label in (("light", "Chiaro"), ("dark", "Scuro")):
            a = QAction(label, self, checkable=True)
            a.setChecked(theme.current() == mode)
            a.triggered.connect(lambda _=False, md=mode: self.set_theme(md))
            tgroup.addAction(a)
            tmenu.addAction(a)
            self.theme_actions[mode] = a
        lmenu = m.addMenu(icon("language"), "Lingua / Language")
        lgroup = QActionGroup(self)
        self.language_actions = {}
        for code, label in i18n.LANGUAGES.items():
            a = QAction(label, self, checkable=True)
            a.setChecked(i18n.current() == code)
            a.triggered.connect(lambda _=False, c=code: self.set_language(c))
            lgroup.addAction(a)
            lmenu.addAction(a)
            self.language_actions[code] = a
        # ------------------------------------------------------------ Help
        m = self.help_menu = mb.addMenu(tr("&Aiuto"))
        self._act(m, tr("Documentazione online"), self.open_docs, icon_name="help")
        self._act(m, tr("Diagnostica ambiente..."), self.show_doctor, icon_name="doctor")
        self._act(m, tr("Apri cartella dei log"), self.open_log_folder, icon_name="folder")
        m.addSeparator()
        self._act(m, tr("Controlla aggiornamenti..."), self.check_updates, icon_name="update")
        self._act(m, tr("Sostieni il progetto (PayPal)..."), self.donate, icon_name="donate")
        self.a_update_auto = QAction(tr("Controlla gli aggiornamenti all'avvio"), self, checkable=True)
        self.a_update_auto.setChecked(self.settings.value("updates_auto", "true") in ("true", True, 1, "1"))
        self.a_update_auto.toggled.connect(lambda on: self.settings.setValue("updates_auto", "true" if on else "false"))
        m.addAction(self.a_update_auto)
        m.addSeparator()
        self._act(m, tr("Informazioni"), self.about, icon_name="about")
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
        lab = QLabel(tr(" Disegno: "))
        lab.setObjectName("pageSubtitle")
        db.addWidget(lab)
        # short texts in the bar (menus keep the full names, tooltips show them)
        for a, short in ((self.draw_actions["pan"], "Sposta"), (self.draw_actions["polygon"], "Disegna US"),
                         (self.draw_actions["line"], "Sezione"), (self.draw_actions["measure"], "Misura"),
                         (self.draw_actions["area"], "Area"),
                         (self.draw_actions["quota"], "Quota"), (self.draw_actions["marker"], "Marker"),
                         (self.draw_actions["sam"], "SAM"),
                         (self.draw_actions["patch"], "Patch"),
                         (self.a_sel_delete, "Elimina"),
                         (self.a_sel_merge, "Unisci"), (self.a_clear_drawings, "Cancella tutto"),
                         (self.a_contours, "Curve"), (self.a_volume, "Volume"), (self.a_slice, "Pianta a quota"),
                         (self.a_qgis_open, "QGIS")):
            if a.toolTip() == a.text().replace("...", "").replace("&", ""):
                a.setToolTip(a.text().replace("...", ""))
            a.setIconText(tr(short))
        for mode in ("pan", "select", "polygon"):
            db.addAction(self.draw_actions[mode])
        db.addAction(self.a_freehand)
        db.addAction(self.a_snap)
        snap_button = db.widgetForAction(self.a_snap)
        if snap_button is not None:
            snap_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        for mode in ("line", "measure", "quota", "marker", "sam", "patch"):
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
        a.setToolTip(tr("Pannello dei layer: mostra, nascondi, zoom, attributi"))
        a.triggered.connect(lambda _=False: self._show_pane(self.left_tabs, self.layers_pane))
        a = db.addAction(icon("line"), tr("Sezioni"))
        a.setToolTip(tr("Pannello delle sezioni"))
        a.triggered.connect(lambda _=False: self._show_pane(self.bottom_tabs, self.sections_pane))
        db.addAction(self.a_qgis_open)
        a = db.addAction(icon("publish"), tr("Pubblica"))
        a.setToolTip(tr("Scrive in PyArchInit le unita' disegnate (pyunitastratigrafiche) e le quote "
                        "(pyarchinit_quote)"))
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
        self.stones = {}
        self._sam_obj = None
        self.classifier = None
        self.sections = []
        self.line_layers = {}
        self._restore_drawings()
        self.pa_pane.set_project_site(self.project_site())
        self.refresh_all(reset_views=True, progress=step)
        step(0.95, tr("Sezioni e disegni"))
        self._load_sections()
        self.proxies_pane.set_proxies(self.chunk.meta.get("proxies", []) if self.chunk else [])
        if self.history is None:
            self.history = UndoHistory(self._undo_state, self._undo_apply, lambda: self.chunk,
                                       on_change=self._undo_actions)
        else:
            self.history.reset()
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
        step(0.05, tr("Area di lavoro"))
        self.workspace.populate(self.doc)
        self.reference.set_chunk(self.chunk, self.last_report)
        step(0.15, tr("Foto (le miniature arrivano mentre lavori)"))
        self.photos.set_chunk(self.chunk)
        step(0.25, tr("Vista 3D"))
        self.model_view.set_chunk(self.chunk) if reset_views else self.model_view.set_chunk(self.chunk,
                                                                                             self.model_view.kind)
        step(0.6, tr("Ortofoto e DEM"))
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
            self.ortho_view.scene().addText(tr("Costruisci il DEM o l'ortofoto per vederli qui"))
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
            self._draw_patches()
            self._draw_markers_on_raster()
            self._draw_stones()

    def _dirty(self, flag: bool = True) -> None:
        self.setWindowModified(flag)
        if flag and self.history is not None and not self._task_done:
            self.history.record()

    def _update_title(self) -> None:
        name = self.doc.path.name if self.doc.path else tr("senza nome")
        site = self.project_site()
        self.setWindowTitle(f"{APP_TITLE} {__version__} - {name}" + (tr(" - sito {0}", site) if site else "") + "[*]")

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
        self.log_pane.info(tr("Sito del progetto: {0}", sito or tr('(nessuno)')))

    def _project_open(self) -> bool:
        return self.doc.path is not None or any(ch.cameras for ch in self.doc.chunks)

    def choose_project_site(self, reason: str = "") -> str:
        """Ask which site the project belongs to: the sites of the PyArchInit database, or a new name."""
        sites = self.pa_pane.site_names() if self.pa is not None else []
        text = (reason + "\n\n" if reason else "") + (tr("Sito dello scavo: tutte le US e le quote di questo progetto "
                                                         "appartengono a questo sito."))
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
            self.choose_project_site(tr("Il database contiene {0} siti (scavi).", len(sites)))

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
            self._warn(tr("Costruisci prima {0}.", tr(names.get(product, product))))
            return False
        return True

    # ---------------------------------------------------------------- messages
    # ================================================================ plugins
    def _load_plugins(self) -> None:
        """The plugins (Strumenti > Plugin): their actions in the menu Plugin, their export formats
        in File > Esporta. One that fails is written in the log; the window starts anyway."""
        from ..plugins import Manager

        self.plugins = Manager(window=self)
        self.plugin_menu: QMenu | None = None
        try:
            self.plugins.load()
        except Exception as exc:
            log.warning("Plugin non caricati: %s", exc)
            return
        for info in self.plugins.plugins:
            if info.error:
                log.warning(tr("Plugin {0} non caricato: {1}", info.name, info.error))
        actions = self.plugins.actions
        if actions:
            menu = self.plugin_menu = QMenu(tr("&Plugin"), self)
            self.menuBar().insertMenu(self.help_menu.menuAction(), menu)
            titles = {i.name: i.title for i in self.plugins.plugins}
            submenus: dict = {}
            for a in actions:
                key = a.menu or titles.get(a.plugin, a.plugin)
                if key not in submenus:
                    submenus[key] = menu.addMenu(icon("plugin"), key)
                self._act(submenus[key], a.title, lambda a=a: self._run_plugin_action(a), tip=a.tip,
                          processing=a.processing)
        if self.plugins.exporters:
            self.export_menu.addSeparator()
            for e in self.plugins.exporters:
                self._act(self.export_menu, e.title + "...", lambda e=e: self._plugin_export(e), processing=True,
                          icon_name="plugin")

    def _run_plugin_action(self, action) -> None:
        try:
            action.callback()
        except Exception as exc:  # a plugin never takes the window down
            self._exc(tr("Plugin {0}", action.plugin), exc)

    def _plugin_export(self, exporter) -> None:
        if not self._need_chunk():
            return
        ch = self.chunk
        path, _ = QFileDialog.getSaveFileName(self, exporter.title,
                                              str(Path(self._last_dir()) / f"{ch.label.replace(' ', '_')}{exporter.suffix}"),
                                              f"{exporter.title} (*{exporter.suffix})")
        if not path:
            return
        out = Path(path) if path.lower().endswith(exporter.suffix.lower()) else Path(path + exporter.suffix)
        self._store_drawings()  # the exporter reads chunk.meta in the background: the drawings go there first
        self._save_sections()
        self.run_task(exporter.title, lambda p: exporter.function(ch, out, p),
                      lambda _r: self.statusBar().showMessage(tr("Scritto {0}", out), 10000), refresh=False,
                      save=False)

    def show_nodes(self) -> None:
        """The computers working on this project through the shared folder."""
        from .nodes_dialog import NodesDialog

        files_dir = self.doc.files_dir if self.doc is not None and self.doc.path else None
        if files_dir is None:
            self._warn("Salva prima il progetto: i nodi lavorano dalla sua cartella.")
            return
        NodesDialog(files_dir, self.settings, self).exec()

    def show_plugins(self) -> None:
        """Strumenti > Plugin: those found and those of the plugin repository."""
        from .plugins_dialog import PluginsDialog

        PluginsDialog(self).exec()

    def restart(self) -> None:
        """Close and start again with the same project (after plugins or the language changed)."""
        self._relaunch_on_close = True
        if not self.close():  # the project was not saved and the user stayed
            self._relaunch_on_close = False

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

    def run_task(self, label: str, fn, on_done=None, refresh: bool = True, save: bool = True,
                 undo: bool = False) -> bool:
        """``fn(progress)`` in the background, then the project saved and the views refreshed.
        ``undo``: the step is an edit (refined markers, a patch) that Modifica > Annulla takes back;
        so is a step announced with ``history.backup`` (points, faces); a processing step is not."""
        label = tr(label)  # also the name of the step in Modifica > Annulla

        def done(result):
            self._task_done = True
            try:
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
            finally:
                self._task_done = False
            if self.history is not None:
                if undo or self.history.pending:
                    self.history.record(label)
                else:
                    self.history.rebase()
            if on_done is not None:
                on_done(result)

        def failed(*args):
            if self.history is not None:
                self.history.discard_pending()
            self._error(*args)

        return self.runner.run(label, fn, done, failed)

    # ---------------------------------------------------------------- file
    def new_document(self) -> None:
        if not self._confirm_discard():
            return
        self._started = True
        self.set_document(Document())
        self.statusBar().showMessage(tr("Nuovo progetto: aggiungi le foto (Flusso di lavoro > Aggiungi foto)"), 10000)

    def open_document(self, path: str | None = None) -> None:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, tr("Apri progetto"), self._last_dir(), tr("Progetti (*.ppg)"))
            if not path:
                return
        card = self._progress_card("Apertura del progetto", Path(path).name)
        step = card.progress(0, 100) if card is not None else (lambda *_a, **_k: None)
        try:
            step(0.02, tr("Lettura del progetto"))
            try:
                doc = Document.open(path)
            except Exception as exc:
                if card is not None:
                    card.close()
                    card = None
                self._exc(tr("Impossibile aprire {0}", path), exc)
                return
            self.last_report = None
            self._started = True
            self.set_document(doc, progress=lambda f, text=None: step(0.08 + 0.9 * f, text))
            step(1.0, tr("Pronto"))
        finally:
            if card is not None:
                card.close()
        self.statusBar().showMessage(tr("Aperto {0}", path), 5000)

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
            path, _ = QFileDialog.getSaveFileName(self, tr("Salva progetto"), self._last_dir(), tr("Progetti (*.ppg)"))
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
        self.statusBar().showMessage(tr("Salvato {0}", self.doc.path), 5000)
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
        if self._pending_update is not None:
            self._install_pending_update()
        if self._relaunch_on_close:
            self._relaunch()

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
        filters = {"cloud": "LAS (*.las);;COPC - Cloud Optimized Point Cloud (*.copc.laz);;PLY (*.ply)",
                   "model": "OBJ con texture (*.obj);;PLY (*.ply);;glTF binario con texture (*.glb)",
                   "dem": "GeoTIFF (*.tif);;COG - Cloud Optimized GeoTIFF, ZSTD (*.tif)",
                   "ortho": "GeoTIFF (*.tif);;COG - Cloud Optimized GeoTIFF, JPEG (*.tif);;"
                            "COG - Cloud Optimized GeoTIFF, WebP con trasparenza (*.tif)",
                   "cameras": "CSV (*.csv);;COLMAP (*.txt)", "reference": "CSV (*.csv)"}
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
        path, flt = QFileDialog.getSaveFileName(self, tr("Esporta"), self._last_dir(), tr(filters[what]))
        if not path:
            return
        if "copc" in flt.lower() and not path.lower().endswith(".copc.laz"):
            path = str(Path(path).with_suffix("")) + ".copc.laz"
        cog = next((c for c in ("jpeg", "webp", "zstd") if c in flt.lower()), None) if "COG" in flt else None
        product = {"cloud": "point_cloud", "model": "model", "dem": "dem", "ortho": "orthomosaic"}.get(what)
        operator = str(self.settings.value("pa/disegnatore", "") or "") or None  # read here, not in the worker
        if cog or path.lower().endswith((".copc.laz", ".glb")):  # can take a while: in the background
            fn = {"cloud": lambda p: ch.export_point_cloud(path, classes=classes),
                  "model": lambda p: ch.export_model(path),
                  "dem": lambda p: ch.export_dem(path, cog=cog),
                  "ortho": lambda p: ch.export_orthomosaic(path, cog=cog)}[what]

            def job(p):
                out = fn(p)
                self._write_paradata(ch, product, out, operator)
                return out

            self.run_task("Esportazione", job,
                          lambda out: (self.log_pane.info(tr("Esportato {0}", out)),
                                       self.statusBar().showMessage(tr("Esportato {0}", out), 8000)),
                          refresh=False, save=False)
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
        if product is not None:
            self._write_paradata(ch, product, path, operator)
        self.log_pane.info(tr("Esportato {0}", path))
        self.statusBar().showMessage(tr("Esportato {0}", path), 8000)

    @staticmethod
    def _write_paradata(chunk, product: str, path, operator: str | None = None) -> None:
        """How an exported product was made, beside it (``<file>.prov.jsonld``, :mod:`core.paradata`)."""
        from ..core.paradata import write_provenance

        try:
            write_provenance(chunk, product, path, operator=operator)
        except Exception as exc:  # the export itself went well
            log.warning("Paradati non scritti per %s: %s", path, exc)

    def import_scans(self) -> None:
        """File > Importa > Scansioni laser: E57 or PTX files as the dense cloud of a new chunk."""
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Scansioni laser"), self._last_dir(),
                                                tr("Scansioni (*.e57 *.E57 *.ptx *.PTX)"))
        if not paths:
            return
        v = ParamDialog.ask(self, "Scansioni laser", [
            Field("label", "Nome del chunk", tr("Scansioni laser ({0} file)", len(paths)), kind="text"),
            Field("crs", "Sistema di riferimento delle scansioni (es. EPSG:32633; vuoto = locale)", "", kind="text"),
            Field("max_points", "Punti al massimo (0 = tutti)", 0, minimum=0, maximum=2_000_000_000)],
            intro="Le scansioni diventano la nuvola densa di un nuovo chunk, ognuna nella sua posizione registrata. "
                  "Se sono nello stesso sistema di riferimento del rilievo fotogrammetrico sono gia' al loro posto; "
                  "altrimenti Flusso di lavoro > Allinea chunk (metodo punti, senza scala) le porta sul chunk delle "
                  "foto. Poi Colori dalle foto le colora con le foto.")
        if not v:
            return
        from ..io.scans import import_scans

        doc = self.doc

        def done(chunk):
            self._activate_chunk(chunk)
            self._show_model("point_cloud")
            self.log_pane.info(tr("{0}: {1} punti da {2} scansioni",
                                  chunk.label, _it(len(chunk.point_cloud)), len(chunk.meta['scans'])))

        self.run_task("Scansioni laser", lambda p: import_scans(doc, paths, label=v["label"] or None,
                                                                crs=(v["crs"] or "").strip() or None,
                                                                max_points=int(v["max_points"]) or None, progress=p),
                      done)

    def colour_scan(self) -> None:
        """The dense cloud of the active chunk coloured from the aligned photos of another chunk."""
        if not self._need_chunk(product="point_cloud"):
            return
        others = [c for c in self.doc.chunks if c is not self.chunk and c.aligned_cameras]
        if not others:
            self._warn("Serve un altro chunk con le foto allineate, nello stesso sistema (Allinea chunk).")
            return
        v = ParamDialog.ask(self, "Colori dalle foto", [
            Field("photos", "Foto del chunk", others[0].key, choices=[(c.key, c.label) for c in others])],
            intro=tr("I punti di «{0}» prendono il colore della foto che li vede piu' di fronte e da piu' vicino, "
                     "tra quelle in cui non sono nascosti dal modello.", self.chunk.label))
        if not v:
            return
        from ..io.scans import colour_from_photos

        target = self.chunk
        photos = next(c for c in others if c.key == v["photos"])
        self.run_task("Colori dalle foto", lambda p: colour_from_photos(target, photos, progress=p),
                      lambda r: (self.log_pane.info(tr("Colori dalle foto per {0} punti su {1}",
                                                       _it(r['coloured']), _it(r['points']))),
                                 self._show_model("point_cloud")))

    def export_light_model(self) -> None:
        if not self._need_chunk(product="model"):
            return
        v = ParamDialog.ask(self, "Modello leggero con normal map", [
            Field("folder", "Cartella", str(Path(self._last_dir()) / f"{self.chunk.label.replace(' ', '_')}_leggero"),
                  kind="dir"),
            Field("faces", "Facce del modello leggero", 50_000, minimum=500, maximum=2_000_000),
            Field("size", "Lato delle texture (px)", 4096, choices=[(2048, "2048"), (4096, "4096"), (8192, "8192")])],
            intro="Il modello viene ridotto e i dettagli della superficie del modello completo finiscono nella normal "
                  "map: la luce li mostra anche con poche facce. Si scrivono light.glb (colore e normal map), "
                  "light.obj con il suo .mtl e le due immagini.")
        if not v or not v["folder"]:
            return
        from ..engine.normalmap import light_model

        ch = self.chunk
        self.run_task("Modello leggero", lambda p: light_model(ch, int(v["faces"]), int(v["size"]), v["folder"], p),
                      lambda out: self._info("Modello leggero", tr("{0} facce, scritto in {1}",
                                                                   _it(out['faces']), Path(out['glb']).parent)),
                      refresh=False, save=False)

    def export_video(self) -> None:
        if not self._need_chunk():
            return
        from ..gui.scene import available_views, best_view

        views = [k for k in ("model_textured", "model", "point_cloud", "tie_points") if k in available_views(self.chunk)]
        if not views:
            self._warn("Costruisci prima la nuvola densa o il modello.")
            return
        names = {"model_textured": "Modello con texture", "model": "Modello (colori dei vertici)",
                 "point_cloud": "Nuvola densa", "tie_points": "Punti di legame"}
        v = ParamDialog.ask(self, "Video di sorvolo", [
            Field("path", "File MP4", str(Path(self._last_dir()) / f"{self.chunk.label.replace(' ', '_')}_sorvolo.mp4"),
                  kind="file_save", filter="Video MP4 (*.mp4)"),
            Field("kind", "Cosa mostrare", best_view(self.chunk) if best_view(self.chunk) in views else views[0],
                  choices=[(k, names[k]) for k in views]),
            Field("size", "Risoluzione", "1280x720", choices=[("1280x720", "HD 1280 x 720"),
                                                              ("1920x1080", "Full HD 1920 x 1080"),
                                                              ("3840x2160", "4K 3840 x 2160")]),
            Field("seconds", "Durata (secondi)", 12.0, minimum=2.0, maximum=600.0, decimals=0),
            Field("turns", "Giri attorno al rilievo", 1.0, minimum=0.25, maximum=10.0, decimals=2),
            Field("pitch", "Inclinazione della camera (gradi sopra l'orizzonte)", 35.0, minimum=5.0, maximum=89.0,
                  decimals=0),
            Field("zoom", "Avvicinamento (1 = tutto il rilievo nell'inquadratura)", 1.0, minimum=0.3, maximum=5.0,
                  decimals=2)])
        if not v or not v["path"]:
            return
        from ..io.flythrough import orbit_video

        w, h = (int(x) for x in v["size"].split("x"))
        ch = self.chunk
        self.run_task("Video di sorvolo", lambda p: orbit_video(ch, v["path"], kind=v["kind"], seconds=v["seconds"],
                                                                size=(w, h), pitch=v["pitch"], zoom=v["zoom"],
                                                                turns=v["turns"], progress=p),
                      lambda out: (self.log_pane.info(tr("Video salvato in {0}", out)),
                                   self.statusBar().showMessage(tr("Video salvato in {0}", out), 10000)),
                      refresh=False, save=False)

    def export_3dtiles(self) -> None:
        if not self._need_chunk(product="model"):
            return
        folder = QFileDialog.getExistingDirectory(self, tr("Cartella per i 3D Tiles"), self._last_dir())
        if not folder:
            return
        ch = self.chunk
        out = Path(folder) / f"{ch.label.replace(' ', '_')}_3dtiles"
        self.run_task("Cesium 3D Tiles", lambda p: ch.export_3dtiles(out, progress=p),
                      lambda path: self._info("Cesium 3D Tiles", tr("Scritto {0}: aprilo in CesiumJS con "
                                                                    "Cesium3DTileset.fromUrl (servito da un server "
                                                                    "web).", path)),
                      refresh=False, save=False)

    def export_i3s(self) -> None:
        """The model as an I3S scene layer package (:func:`io.i3s.export_slpk`)."""
        if not self._need_chunk(product="model"):
            return
        from ..io.i3s import projected_epsg

        ch = self.chunk
        try:
            projected_epsg(ch.crs)
        except ValueError as exc:
            self._warn(str(exc))
            return
        v = ParamDialog.ask(self, "Modello I3S (.slpk)", [
            Field("out", "File", str(Path(self._last_dir()) / f"{ch.label.replace(' ', '_')}.slpk"), kind="file_save",
                  filter="Scene Layer Package (*.slpk)"),
            Field("mode", "Coordinate", "global", choices=[
                ("global", "WGS84 (ArcGIS Online, scene globali, CesiumJS, deck.gl)"),
                ("local", tr("Il sistema del progetto {0} (scene locali di ArcGIS)", ch.crs))]),
            Field("faces", "Facce per nodo", 20000, minimum=2000, maximum=200000),
            Field("texture", "Texture massima di un nodo (pixel)", 2048, choices=[(1024, "1024"), (2048, "2048"), (4096, "4096")])],
            intro="Il modello diviso in nodi con livelli di dettaglio, ogni nodo con la sua texture presa dalla texture "
                  "del modello (o dai colori dei vertici), come un Integrated Mesh di ArcGIS.")
        if not v or not v["out"]:
            return
        out = Path(v["out"]).with_suffix(".slpk")
        self.run_task("I3S", lambda p: ch.export_i3s(out, mode=v["mode"], max_faces=int(v["faces"]),
                                                    texture_size=int(v["texture"]), progress=p),
                      lambda path: self._info("I3S", tr("Scritto {0}: aggiungilo in ArcGIS Pro o caricalo in ArcGIS "
                                                        "Online come pacchetto di scena.", path)),
                      refresh=False, save=False)

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
            text = (tr("Saggio di scavo: {0}\nCiotola su turntable: {1}\n", d.project, d.pottery_project or '-')
                    + (tr("Profilo della ciotola: {0}\n", d.pottery_profile) if d.pottery_profile else "")
                    + tr("Leggi {0} per cosa provare.", d.folder / 'LEGGIMI.txt'))
            self._info("Demo pronta", text)

        self.run_task("Creazione dati demo", lambda p: create_demo(v["folder"], process=v["process"],
                                                                   with_pottery=v["pottery"], progress=p),
                      done, refresh=False, save=False)

    def import_external(self, kind: str) -> None:
        """Open a project made by Metashape, WebODM/ODM or a Photo3D capture session."""
        if not self._confirm_discard():
            return
        if kind == "metashape":
            src, _ = QFileDialog.getOpenFileName(self, tr("Progetto Metashape"), self._last_dir(), "Metashape (*.psx)")
        elif kind == "webodm":
            r = message(self, "question", "WebODM", "Importi l'archivio scaricato da WebODM (all.zip)?\n"
                        "Si = scegli all.zip, No = scegli la cartella del task ODM.",
                        buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                        | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel:
                return
            if r == QMessageBox.StandardButton.Yes:
                src, _ = QFileDialog.getOpenFileName(self, tr("all.zip di WebODM"), self._last_dir(), "Zip (*.zip)")
            else:
                src = QFileDialog.getExistingDirectory(self, tr("Cartella del task ODM"), self._last_dir())
        else:
            src = QFileDialog.getExistingDirectory(self, tr("Cartella della sessione Photo3D"), self._last_dir())
        if not src:
            return
        stem = Path(src).stem if kind != "photo3d" else Path(src).name
        default = Path(src).parent / f"{stem}_ppg.ppg" if kind != "photo3d" else Path(src) / f"{stem}.ppg"
        out, _ = QFileDialog.getSaveFileName(self, tr("Salva il progetto importato"), str(default),
                                             tr("Progetti (*.ppg)"))
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
            products = ", ".join(sorted(k for k in ch.products if k != "reconstruction")) or tr("nessuno")
            text = tr("{0} chunk, {1} foto ({2} allineate), prodotti: {3}, CRS {4}",
                      len(doc.chunks), len(ch.cameras), len(ch.aligned_cameras), products, ch.crs)
            if ch.meta.get("point_cloud_source"):
                text += tr("\nNuvola densa: {0} ({1} punti)", ch.meta['point_cloud_source'], len(ch.point_cloud))
            if ch.meta.get("import", {}).get("dem"):
                text += f"\nDEM: {ch.meta['import']['dem']}"
            if ch.markers:
                text += f"\nMarker: {len(ch.markers)}" + (tr(", barre di scala: {0}",
                                                             len(ch.scalebars)) if ch.scalebars else "")
            odm = ch.meta.get("import", {}).get("gcp", {}).get("odm_errors")
            if odm:
                rmse = float(np.sqrt(np.mean(np.sum(np.square(list(odm.values())), axis=1))))
                text += tr(" (GCP di ODM: errore medio {0:.1f} mm secondo ODM)", rmse * 1000)
            self.log_pane.info(tr("Importato {0}: {1}", src, text))
            for w in warn:
                self.log_pane.warning(w)
            self._info("Importazione completata", text + ("\n\n" + "\n".join(warn[:6]) if warn else ""))

        self.run_task("Importazione", job, done, refresh=False, save=False)

    # ---------------------------------------------------------------- workflow
    def add_chunk(self) -> None:
        label, ok = QInputDialog.getText(self, tr("Nuovo chunk"), tr("Nome:"), text=f"Chunk {len(self.doc.chunks) + 1}")
        if ok:
            self._activate_chunk(self.doc.add_chunk(label))
            self._dirty(True)

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
        choices = [(ch.key, tr("{0} ({1} foto allineate)", ch.label, len(ch.aligned_cameras))) for ch in doc.chunks]
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
                err = "" if rmse is None else (tr(", errore medio {0:.1f} mm",
                                                  rmse * 1000) if metric else f", RMSE {rmse:.4g}")
                lines.append(tr("{0}: {1} corrispondenze{2}, scala {3:.4f}",
                                e['label'], e.get('correspondences', 0), err, e['scale'])
                             + (tr("; da ricostruire: {0}",
                                   ', '.join(e['stale_products'])) if e.get("stale_products") else ""))
            if report.get("levelled_reference"):
                lines.append(tr("Il chunk {0} e' stato prima messo in bolla (come per il DEM).", ref.label))
            text = tr("Allineati a {0} ({1}):\n",
                      ref.label, tr(dict(markers='marker', cameras='foto', points='punti')[v['method']])) \
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
                self._activate_chunk(merged)
            text = (tr("Creato «{0}»: {1} foto, {2} marker", merged.label, len(merged.cameras), len(merged.markers))
                    + (tr(", nuvola densa di {0} punti", _it(len(merged.point_cloud)))
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
            d = QFileDialog.getExistingDirectory(self, tr("Cartella di foto"), self._last_dir())
            paths = [d] if d else []
        else:
            paths, _ = QFileDialog.getOpenFileNames(self, tr("Foto"), self._last_dir(),
                                                    tr("Immagini (*.jpg *.jpeg *.png *.tif *.tiff *.JPG *.JPEG *.PNG "
                                                       "*.TIF)"))
        if paths:
            self._started = True
            self.run_task("Aggiunta foto", lambda p: self.chunk.add_photos(paths, recursive=folder),
                          lambda cams: self.statusBar().showMessage(tr("{0} foto aggiunte", len(cams)), 8000))

    def add_video(self) -> None:
        """Workflow > Aggiungi video: frames chosen for the reconstruction, GPS from DJI subtitles."""
        if not self._need_chunk():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Video", self._last_dir(),
                                              "Video (*.mp4 *.MP4 *.mov *.MOV *.m4v *.avi *.mkv *.mts *.MTS)")
        if not path:
            return
        v = ParamDialog.ask(self, "Fotogrammi dal video", [
            Field("mode", "Scelta dei fotogrammi", "auto",
                  choices=[("auto", "Automatica: quando la vista si e' spostata (il piu' nitido)"),
                           ("every", "Uno ogni tot secondi")]),
            Field("overlap", "Sovrapposizione tra fotogrammi (%)", 70.0, minimum=30.0, maximum=95.0, decimals=0),
            Field("every", "Secondi tra due fotogrammi", 1.0, minimum=0.1, maximum=60.0, decimals=1),
            Field("max_frames", "Fotogrammi al massimo (0 = nessun limite)", 0, minimum=0, maximum=100000)],
            intro=tr("{0}: i fotogrammi vanno nella cartella «{1}_fotogrammi» accanto al video. Se accanto c'e' il "
                     "file .SRT del drone DJI, ogni fotogramma prende la sua posizione GPS.",
                     Path(path).name, Path(path).stem))
        if not v:
            return
        from ..engine.video import add_video

        self._started = True
        chunk = self.chunk
        self.run_task("Fotogrammi dal video", lambda p: add_video(
            chunk, path, overlap=v["overlap"] / 100.0, every=v["every"] if v["mode"] == "every" else None,
            max_frames=int(v["max_frames"]) or None, progress=p),
            lambda cams: self.statusBar().showMessage(
                tr("{0} fotogrammi aggiunti",
                   len(cams)) + (tr(" con il GPS") if cams and cams[0].reference.location else ""),
                10000))

    PANORAMA_FACES = [("front", "Davanti"), ("right", "Destra"), ("back", "Dietro"), ("left", "Sinistra"),
                      ("up", "Sopra (volta, cielo)"), ("down", "Sotto (pavimento, treppiede)")]

    def add_panoramas(self) -> None:
        """Workflow > Aggiungi foto 360°: equirectangular photos as the faces of a cube (a rig)."""
        if not self._need_chunk():
            return
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Foto 360° (equirettangolari)"), self._last_dir(),
                                                tr("Immagini (*.jpg *.jpeg *.png *.tif *.tiff *.JPG *.JPEG *.PNG "
                                                   "*.TIF)"))
        if not paths:
            return
        v = ParamDialog.ask(self, "Foto 360°", [
            Field("faces", "Facce", [k for k, _ in self.PANORAMA_FACES], choices=self.PANORAMA_FACES, kind="checklist",
                  tip="Togli la faccia «Sotto» se mostra solo il treppiede o chi tiene la camera"),
            Field("fov", "Ampiezza di ogni faccia (gradi)", 100.0, minimum=90.0, maximum=130.0, decimals=0,
                  tip="Piu' di 90 gradi: le facce vicine si sovrappongono e i punti si collegano"),
            Field("size", "Lato delle facce in pixel (0 = la risoluzione della foto)", 0, minimum=0, maximum=20000)],
            intro="Ogni foto 360° diventa sei foto prospettiche, le facce di un cubo, nella cartella «facce_360» "
                  "accanto alle foto. Nell'allineamento le facce di una foto restano unite (stesso centro, rotazioni "
                  "note); le foto che non sono equirettangolari (larghezza doppia dell'altezza) sono lasciate fuori.")
        if not v:
            return
        if not v["faces"]:
            self._warn("Scegli almeno una faccia.")
            return
        from ..engine.panorama import add_panoramas

        self._started = True
        chunk = self.chunk
        n = len(paths)
        self.run_task("Foto 360°", lambda p: add_panoramas(chunk, paths, size=int(v["size"]) or None, fov=v["fov"],
                                                         faces=v["faces"], progress=p),
                      lambda cams: self.statusBar().showMessage(
                          tr("{0} foto 360° di {1} aggiunte ({2} facce)",
                             len({c.meta['panorama'] for c in cams}), n, len(cams)),
                          10000))

    LENS_MODELS = [("OPENCV", "Normale (Brown: k1, k2, p1, p2)"),
                   ("OPENCV_FISHEYE", "Fisheye (Kannala-Brandt: k1-k4)"),
                   ("FULL_OPENCV", "Normale, distorsione forte (k1-k6, p1, p2)"),
                   ("PINHOLE", "Senza distorsione (camera virtuale, foto gia' corrette)")]

    def lens_model(self, cams=None) -> None:
        """Photos menu > Modello dell'obiettivo: the camera model of the sensors of ``cams``."""
        if not self._need_chunk():
            return
        cams = cams if cams is not None else self.photos.selected_cameras()
        sensors = sorted({c.sensor_key for c in cams}) or sorted(self.chunk.sensors)
        if not sensors:
            return
        current = self.chunk.sensors[sensors[0]].model
        choices = list(self.LENS_MODELS)
        if current not in [k for k, _ in choices]:
            choices.append((current, current))
        labels = ", ".join(self.chunk.sensors[k].label for k in sensors)
        v = ParamDialog.ask(self, "Modello dell'obiettivo", [
            Field("model", "Modello", current, choices=choices)],
            intro=tr("Sensori: {0}. Il fisheye e' riconosciuto dai dati EXIF dell'obiettivo (o da una focale "
                     "equivalente fino a 15 mm); se non lo e', sceglilo qui. Focale e punto principale restano, la "
                     "distorsione riparte da zero: allinea di nuovo le foto.", labels))
        if not v or all(self.chunk.sensors[k].model == v["model"] for k in sensors):
            return
        for k in sensors:
            self.chunk.set_sensor_model(k, v["model"])
        self._dirty(True)
        self.log_pane.info(tr("Modello dell'obiettivo {0} per: {1}. Allinea di nuovo le foto.", v['model'], labels))
        self.statusBar().showMessage(tr("Modello dell'obiettivo cambiato: allinea di nuovo le foto"), 10000)

    def _photos_menu(self, pos) -> None:
        """Right click in the Photos pane: quality, enable/disable, masks of the selected photos."""
        if self.chunk is None:
            return
        cams = self.photos.selected_cameras()
        menu = QMenu(self)
        if len(cams) == 1:
            menu.addAction(icon("photos"), tr("Apri"), lambda: self.open_photo(cams[0]))
        menu.addAction(icon("quality"), tr("Stima la qualita' delle immagini"), self.estimate_quality)
        menu.addAction(tr("Disattiva le foto sfocate (qualita' sotto 0,5)"), self.disable_blurred)
        menu.addAction(icon("lens"), tr("Modello dell'obiettivo..."), lambda: self.lens_model(cams))
        if len(cams) == 2:
            menu.addAction(icon("matches"), tr("Corrispondenze tra queste due foto"),
                           lambda: self.pair_matches(cams[0], cams[1]))
        if len(cams) == 1 and cams[0].aligned:
            menu.addAction(icon("residuals"), tr("Residui dei punti di legame"),
                           lambda: (self.a_residuals.setChecked(True), self.open_photo(cams[0])))
        if cams:
            menu.addSeparator()
            menu.addAction(tr("Disattiva le {0} foto selezionate", len(cams)), lambda: self.enable_photos(cams, False))
            menu.addAction(tr("Attiva le {0} foto selezionate", len(cams)), lambda: self.enable_photos(cams, True))
            menu.addSeparator()
            menu.addAction(icon("mask"), tr("Maschere dal modello (foto selezionate)"), self.masks_from_model)
            menu.addAction(icon("mask"), tr("Maschere con SAM 2.1 (foto selezionate)"), self.masks_with_sam)
            menu.addAction(tr("Rimuovi le maschere delle foto selezionate"), lambda: self.masks_remove(selected=True))
        menu.exec(self.photos.mapToGlobal(pos))

    def estimate_quality(self) -> None:
        if not self._need_chunk():
            return
        from ..engine.quality import THRESHOLD, estimate_quality

        chunk = self.chunk

        def done(q):
            low = sorted((v, k) for k, v in q.items() if v < THRESHOLD)
            self.photos.update_flags()
            self._dirty(True)
            text = tr("Qualita' stimata per {0} foto.", len(q))
            if low:
                names = ", ".join(chunk.camera(k).label for _v, k in low[:12]) + (" ..." if len(low) > 12 else "")
                text += tr(" {0} sfocate rispetto alle altre (sotto 0,5): {1}", len(low), names)
            self.log_pane.info(text)
            self.statusBar().showMessage(text[:200], 15000)

        self.run_task("Qualita' delle immagini", lambda p: estimate_quality(chunk, progress=p), done, refresh=False)

    def disable_blurred(self) -> None:
        if not self._need_chunk():
            return
        from ..engine.quality import low_quality

        if not any(c.meta.get("quality") is not None for c in self.chunk.cameras):
            self._warn("Stima prima la qualita' delle immagini (tasto destro nel pannello Foto).")
            return
        self.enable_photos(low_quality(self.chunk), False)

    def enable_photos(self, cams, enabled: bool) -> None:
        for c in cams:
            c.enabled = bool(enabled)
        self.photos.update_flags()
        self._dirty(True)
        self.statusBar().showMessage(tr("{0} foto {1}: valgono dal prossimo allineamento o dalla prossima costruzione",
                                        len(cams), tr('attivate') if enabled else tr('disattivate')), 10000)

    def detect_markers(self) -> None:
        if not self._need_chunk():
            return
        v = ParamDialog.ask(self, "Rileva marker", [Field("target_type", "Tipo di target", "aruco_4x4_50",
                                                         choices=TARGET_TYPES)])
        if v:
            self.run_task("Rilevamento marker", lambda p: self.chunk.detect_markers(v["target_type"], progress=p),
                          lambda ms: self.statusBar().showMessage(tr("{0} marker rilevati", len(ms)), 8000))

    # ---------------------------------------------------------------- undo and redo
    def _undo_state(self) -> dict:
        """What Modifica > Annulla takes back, as JSON-able parts (see ``gui.undo``)."""
        ch = self.chunk
        state = {"chunk": None if ch is None else ch.key,
                 "units": [{**u.to_dict(), "attributes": dict(u.attributes or {})} for u in self.units],
                 "quote": [dict(q) for q in self.quote], "sections": [dict(x) for x in self.sections],
                 "pa": {"deleted": sorted(self.pa_deleted), "geom": sorted(self.pa_geom_changed),
                        "pending": sorted(int(k) for k in self.pa_pending)}}
        if ch is not None:
            state.update(markers=[m.to_dict() for m in ch.markers], scalebars=[sb.to_dict() for sb in ch.scalebars],
                         excluded=ch.meta.get("marker_excluded"),
                         patches=(ch.meta.get("orthomosaic") or {}).get("patches"), region=ch.meta.get("region"))
        return state

    def _undo_apply(self, state: dict, parts: set, products: list) -> None:
        """Put back the ``parts`` of a snapshot (and show the ``products`` whose files came back)."""
        ch = self.chunk
        if parts & {"units", "quote", "sections", "pa"}:
            from ..archaeo.stratigraphy import UnitPolygon

            if "units" in state:
                self.units = [UnitPolygon.from_dict(d) for d in state["units"] or []]
            if "quote" in state:
                self.quote = [dict(q) for q in state["quote"] or []]
            if "sections" in state:
                self.sections = [dict(x) for x in state["sections"] or []]
                self._save_sections()
                self.sections_pane.set_sections(self.sections)
            if "pa" in state:
                pa = state["pa"] or {}
                self.pa_deleted, self.pa_geom_changed = set(pa.get("deleted", [])), set(pa.get("geom", []))
                pending = set(pa.get("pending", []))
                self.pa_pending = {int(u.attributes["gid"]): u for u in self.units
                                   if (u.attributes or {}).get("gid") is not None
                                   and int(u.attributes["gid"]) in pending}
            self._redraw_drawings()
        if ch is not None and parts & {"markers", "scalebars", "excluded"}:
            from ..core.marker import Marker, ScaleBar

            if "markers" in state:
                ch.markers = [Marker.from_dict(d) for d in state["markers"] or []]
            if "scalebars" in state:
                ch.scalebars = [ScaleBar.from_dict(d) for d in state["scalebars"] or []]
            if "excluded" in state:
                if state["excluded"]:
                    ch.meta["marker_excluded"] = state["excluded"]
                else:
                    ch.meta.pop("marker_excluded", None)
            self._markers_edited()
        if ch is not None and "region" in state:
            if state["region"]:
                ch.meta["region"] = state["region"]
            else:
                ch.meta.pop("region", None)
            self._region_changed("Regione di ricostruzione come prima")
        if ch is not None and "patches" in state:
            ch.meta.setdefault("orthomosaic", {})["patches"] = state["patches"] or []
            if ch.orthomosaic_path is not None:
                self.run_task("Patch dell'ortofoto", lambda p: ch.update_orthomosaic(progress=p),
                              lambda _r: self._draw_patches(), refresh=False)
            else:
                self._draw_patches()
        if products:
            self.workspace.populate(self.doc)
            if self.model_view.kind:
                self.model_view.refresh(reset=False)
        self._dirty(True)

    def _undo_actions(self) -> None:
        h = self.history
        if h is None:
            return
        self.a_undo.setEnabled(h.can_undo())
        self.a_redo.setEnabled(h.can_redo())
        self.a_undo.setText(tr("Annulla: {0}", h.undo_text()) if h.can_undo() else tr("Annulla", context="undo"))
        self.a_redo.setText(tr("Ripeti: {0}", h.redo_text()) if h.can_redo() else tr("Ripeti"))

    def undo(self) -> None:
        if self.runner.busy:
            self.statusBar().showMessage(tr("Aspetta che finisca l'elaborazione in corso"), 6000)
            return
        text = self.history.undo() if self.history is not None else None
        if text:
            self.statusBar().showMessage(tr("Annullato: {0}", text), 6000)

    def redo(self) -> None:
        if self.runner.busy:
            self.statusBar().showMessage(tr("Aspetta che finisca l'elaborazione in corso"), 6000)
            return
        text = self.history.redo() if self.history is not None else None
        if text:
            self.statusBar().showMessage(tr("Ripetuto: {0}", text), 6000)

    def batch_process(self) -> None:
        """Flusso di lavoro > Elaborazione batch: steps over several chunks, the project saved after
        each one; an unfinished batch is offered to be resumed (``pyarchinit_photogrammetry.batch``)."""
        import json

        from .. import batch
        from .batch_dialog import BatchDialog

        if self.doc.path is None:
            self._warn("Salva prima il progetto: il batch lo salva dopo ogni passo.")
            return
        state = batch.pending(self.doc)
        resume, steps, opts = False, None, {"retries": 1, "on_error": "continue"}
        if state is not None:
            total = sum(len(batch.targets(self.doc, s["chunks"])) for s in state["steps"])
            r = choice(self, "question", "Elaborazione batch",
                       tr("Un batch si e' interrotto dopo {0} {1} su {2} (iniziato il {3}). Riprenderlo da dove si "
                          "era fermato?",
                          len(state['done']), tr('passo') if len(state['done']) == 1 else tr('passi'), total,
                          state['started'][:16].replace('T', ' ')),
                       ["Riprendi", "Modifica e ricomincia"])
            if r is None:
                return
            steps, resume = state["steps"], r == 0
        if not resume:
            if steps is None:
                try:
                    steps = json.loads(self.settings.value("batch/steps", "[]") or "[]")
                except ValueError:
                    steps = []
            dlg = BatchDialog(self.doc, steps, self)
            if dlg.exec() != dlg.DialogCode.Accepted:
                return
            steps, opts = dlg.steps(), dlg.options()
            self.settings.setValue("batch/steps", json.dumps(steps))
        self._store_drawings()  # the batch saves the project: the drawings go with it
        doc = self.doc

        def done(report):
            self.setWindowModified(False)
            text = (tr("Batch finito in {0:.1f} min: {1} passi fatti", report['seconds'] / 60, len(report['done']))
                    + (tr(", {0} falliti", len(report['failed'])) if report["failed"] else "")
                    + (tr(", {0} saltati", len(report['skipped'])) if report["skipped"] else ""))
            for key, err in report["failed"].items():
                ck, i = key.split(":")
                label = next((c.label for c in doc.chunks if str(c.key) == ck), ck)
                self.log_pane.warning(tr("Batch: {0} su {1} fallito: {2}", steps[int(i)]['op'], label, err))
            self.log_pane.info(text)
            self.statusBar().showMessage(text, 20000)

        self.run_task("Elaborazione batch", lambda p: batch.run_batch(doc, steps, resume=resume, progress=p, **opts),
                      done)

    def field_check(self) -> None:
        """Before leaving the site: gaps in the coverage, photos not aligned or blurred, markers in
        too few photos (``engine.fieldcheck``); the coverage map opens in the raster view."""
        if not self._need_chunk():
            return
        ch = self.chunk
        if not ch.aligned_cameras:
            self._warn("Allinea prima le foto: il controllo usa le loro posizioni.")
            return
        from ..engine.fieldcheck import field_check

        def done(rep):
            self.photos.update_flags()
            self.workspace.populate(self.doc)
            self._dirty(True)
            lines = []
            if rep.get("cells"):
                pct = rep["weak_fraction"] * 100
                lines.append(tr("Copertura: {0:.1f}% dell'area vista da meno di {1} foto ({2:.2f} m2); in media ogni "
                                "punto e' in {3:.0f} foto.",
                                pct, rep['min_photos'], rep['weak_area'], rep['median_photos'])
                             if pct else tr("Copertura: tutta l'area e' vista da almeno {0} foto (in media {1:.0f}).",
                                            rep['min_photos'], rep['median_photos']))
            if rep["unaligned"]:
                lines.append(tr("Foto non allineate ({0}): ", len(rep['unaligned'])) + ", ".join(rep["unaligned"][:15])
                             + (" ..." if len(rep["unaligned"]) > 15 else ""))
            if rep.get("blurred"):
                lines.append(tr("Foto sfocate ({0}): ", len(rep['blurred'])) + ", ".join(rep["blurred"][:15])
                             + (" ..." if len(rep["blurred"]) > 15 else ""))
            words = {"not found": "in nessuna foto", "one photo": "in una foto sola", "two photos": "in due foto sole"}
            for m in rep["markers"]:
                lines.append(f"Marker {m['label']}: {tr(words[m['status']])}")
            if not lines[1:] and not rep.get("weak_cells"):
                lines.append("Nessun problema: si puo' lasciare il sito.")
            if rep.get("cells"):
                self.show_coverage_map()
            self._info("Controllo prima di lasciare il sito", "\n".join(lines))

        self.run_task("Controllo del rilievo", lambda p: field_check(ch, progress=p), done, refresh=False)

    def show_coverage_map(self) -> None:
        """How many photos frame every cell of the area (red: fewer than three)."""
        ch = self.chunk
        path = ch.product_path("coverage") if ch is not None else None
        if path is None or not path.exists():
            self._warn("Fai prima il controllo prima di lasciare il sito (Flusso di lavoro).")
            return
        self._load_raster("coverage", path)
        self.show_view(self.ortho_view)
        self.statusBar().showMessage(tr("Copertura: foto che inquadrano ogni punto - rosso scuro nessuna, rosso una, "
                                        "arancio due, giallo 3-4, verde chiaro 5-8, verde 9 o piu'"), 20000)

    # ---------------------------------------------------------------- diagnostics of the alignment
    def match_graph(self) -> None:
        if not self._need_chunk():
            return
        from ..engine import diagnostics
        from .matches_dialog import MatchGraphDialog

        try:
            graph = diagnostics.match_graph(self.chunk)
        except RuntimeError as exc:
            self._warn(str(exc))
            return
        MatchGraphDialog(self.chunk, graph, self).exec()

    def pair_matches(self, a=None, b=None) -> None:
        """The points that link two photos (the two selected in the Photos pane)."""
        if not self._need_chunk():
            return
        if a is None or b is None:
            cams = self.photos.selected_cameras()
            if len(cams) != 2:
                self._warn("Seleziona due foto nel pannello Foto (Cmd/Maiusc + clic).")
                return
            a, b = cams
        from .matches_dialog import PairMatchesDialog

        PairMatchesDialog(self.chunk, a.key, b.key, self).exec()

    def _residuals_toggled(self, on: bool) -> None:
        if on and self.photo_view.camera is not None:
            self._draw_residuals()
        elif not on:
            self.photo_view.clear_residuals()

    def _draw_residuals(self) -> None:
        cam = self.photo_view.camera
        if cam is None or not cam.aligned:
            self.photo_view.clear_residuals()
            return
        from ..engine.diagnostics import residuals

        try:
            measured, projected = residuals(self.chunk, cam.key)
        except Exception as exc:  # no model (an imported project without tie points)
            self.statusBar().showMessage(tr("Residui non disponibili: {0}", exc), 8000)
            return
        info = self.photo_view.show_residuals(measured, projected)
        self.statusBar().showMessage(
            decimal_comma(tr("{0}: {1} punti di legame, residuo medio {2:.2f} px, massimo {3:.2f} px (frecce x20: "
                             "verde sotto 1 px, arancio sotto 3, rosso "
                             "oltre)", cam.label, info['count'], info['mean'], info['max'])),
            20000)

    def camera_calibration(self) -> None:
        if not self._need_chunk():
            return
        if not self.chunk.sensors:
            self._warn("Aggiungi prima delle foto.")
            return
        from .calibration_dialog import CalibrationDialog

        dlg = CalibrationDialog(self.chunk, self)
        dlg.exec()
        if dlg.changed:
            self._dirty(True)
            self.log_pane.info("Calibrazione della camera cambiata: allinea od ottimizza di nuovo le foto")

    def merge_markers(self, keys) -> None:
        if not self._need_chunk() or len(keys or []) < 2:
            self._warn("Seleziona due marker o piu' in Riferimenti per unirli.")
            return
        ch = self.chunk
        names = [ch.marker_by_key(k).label for k in keys]
        keep = ch.merge_markers(keys)
        self._markers_edited(keep)
        self.log_pane.info(tr("Marker {0} uniti in {1}", ', '.join(names), keep.label))

    def remove_markers(self, keys) -> None:
        if not self._need_chunk() or not keys:
            return
        ch = self.chunk
        names = [ch.marker_by_key(k).label for k in keys]
        bars = sum(1 for sb in ch.scalebars if {sb.marker_a, sb.marker_b} & set(keys))
        if choice(self, "question", "Elimina marker", tr("Eliminare {0}", ', '.join(names))
                  + (tr(" e {0} barre di scala che ci finiscono", bars) if bars else "") + "?", ["Elimina"]) != 0:
            return
        ch.remove_markers(keys)
        self._markers_edited()
        self.log_pane.info(tr("Marker eliminati: {0}", ', '.join(names)))

    def estimate_precision(self) -> None:
        """Precision of cameras, markers and tie points from the covariance of the adjustment, and
        the map of the precision of the DEM (``engine.precision``)."""
        if not self._need_chunk():
            return
        ch = self.chunk
        if len(ch.aligned_cameras) < 2:
            self._warn("Allinea prima le foto.")
            return

        def done(report):
            self.reference.set_chunk(ch, self.last_report)
            self.photos.update_flags()
            self.workspace.populate(self.doc)
            self._dirty(True)
            tp, cams = report["tie_points"].get("median"), report["cameras"].get("median")
            datum = tr({"control points": "rispetto ai punti di controllo e alla loro accuratezza",
                        "camera positions": "rispetto alle posizioni GPS delle foto",
                        "internal": "interna al blocco (nessun riferimento)"}[report["datum"]])
            fmt = lambda v: " / ".join(f"{x * 1000:.1f}" for x in v) if v else "-"  # noqa: E731
            text = (tr("Precisione {0}: punti di legame X/Y/Z {1} mm, camere {2} mm (mediane); misura nelle foto "
                       "{3:.2f} px", datum, fmt(tp), fmt(cams), report['sigma0_px']))
            self.log_pane.info(text)
            self.statusBar().showMessage(text, 20000)
            if ch.product_path("dem_precision") is not None:
                self.show_precision_map()

        self.run_task("Stima della precisione", lambda p: ch.estimate_precision(progress=p), done, refresh=False)

    def show_precision_map(self) -> None:
        """The vertical precision of the DEM in the raster view (green small, red large)."""
        ch = self.chunk
        path = ch.product_path("dem_precision") if ch is not None else None
        if path is None or not path.exists():
            self._warn("Stima prima la precisione (con il DEM costruito).")
            return
        self._load_raster("sigma", path)
        self.show_view(self.ortho_view)
        lim = getattr(self.ortho_view, "diff_limit", 0.0)
        self.statusBar().showMessage(
            tr("Precisione del DEM (deviazione standard delle quote): verde piccola, rosso {0:.1f} mm o piu'; sigma "
               "Z sotto il cursore", lim * 1000), 20000)

    def refine_markers(self, keys=None) -> None:
        """The blue flags of the markers (all, or those of ``keys``) looked for in the photos by
        image correlation: those found become measurements (``markers.refine``)."""
        if not self._need_chunk():
            return
        from ..markers.refine import refine_markers

        ch = self.chunk
        markers = [ch.marker_by_key(k) for k in keys] if keys else list(ch.markers)
        if not any(p.measured for m in markers for p in m.projections.values()):
            self._warn("Misura prima il marker in almeno una foto (o rileva i marker codificati): le bandierine blu "
                       "nelle altre foto partono da li'.")
            return

        def done(report):
            found = sum(len(r["refined"]) for r in report.values())
            left = sum(len(r["failed"]) for r in report.values())
            self._markers_edited()
            text = (tr("Bandierine blu trovate nelle foto: "
                       "{0}", found) if found else tr("Nessuna bandierina blu "
                                                                                         "trovata"))
            text += tr("; {0} da confermare a mano", left) if left else ""
            self.log_pane.info(text)
            self.statusBar().showMessage(text, 15000)

        self.run_task("Affinamento dei marker", lambda p: refine_markers(ch, markers, progress=p), done, refresh=False,
                      undo=True)

    def align_photos(self) -> None:
        if not self._need_chunk():
            return
        if not self.chunk.cameras:
            self._warn("Aggiungi prima delle foto.")
            return
        has_gps = sum(1 for c in self.chunk.cameras if c.reference.location and c.reference.enabled) >= 3
        aligned = bool(self.chunk.aligned_cameras)
        v = ParamDialog.ask(self, "Allinea foto", [
            Field("general", "Generale", kind="heading"),
            Field("quality", "Precisione", "high", choices=QUALITY[1:]),
            Field("generic", "Preselezione generica", True,
                  tip="Le coppie che si sovrappongono trovate prima su foto rimpicciolite; fino a 40 foto si "
                      "abbinano comunque tutte le coppie"),
            Field("reference", "Preselezione di riferimento", "source" if has_gps else "none",
                  choices=[("none", "Nessuna"), ("source", "Posizioni di riferimento (GPS delle foto)"),
                           ("estimated", "Posizioni stimate (allineamento precedente)"),
                           ("sequential", "Sequenziale (ordine di scatto)")],
                  tip="Ogni foto abbinata alle 20 più vicine per posizione, o a quelle scattate prima e dopo; "
                      "insieme alla generica si abbinano le coppie di entrambe"),
            Field("reset", "Azzera l'allineamento attuale", True,
                  tip="Spento: le foto aggiunte dopo l'ultimo allineamento entrano nel blocco esistente, che "
                      "resta com'è"),
            Field("advanced", "Avanzate", kind="heading"),
            Field("keypoint_limit", "Limite punti chiave", 40000, minimum=1000, maximum=200000),
            Field("tiepoint_limit", "Limite punti di legame", 8000, minimum=500, maximum=100000),
            Field("masks", "Applica le maschere a", "keypoints",
                  choices=[("none", "Niente"), ("keypoints", "Punti chiave"), ("tiepoints", "Punti di legame")],
                  tip="Punti chiave: nelle parti mascherate non si cercano punti; punti di legame: un punto "
                      "mascherato anche in una sola foto viene tolto ovunque (bastano poche foto mascherate)"),
            Field("stationary", "Escludi i punti di legame fermi", False,
                  tip="I punti che restano nello stesso pixel in due foto sono lo sfondo fermo (turntable con "
                      "fotocamera fissa): non contano"),
            Field("guided", "Abbinamento guidato delle immagini", False,
                  tip="Le corrispondenze cercate di nuovo con la geometria della coppia: più punti di legame, "
                      "abbinamento più lento"),
            Field("adaptive", "Adattamento adattivo del modello della camera", False,
                  tip="Punto principale, distorsione tangenziale e due focali stimati su una copia del blocco e "
                      "tenuti solo se affidabili: errore che scende e valori entro i limiti"),
            Field("gps", "Posizioni GPS delle foto nell'allineamento", has_gps,
                  tip="Le posizioni delle foto, con la loro accuratezza, tengono il blocco durante l'allineamento "
                      "incrementale: un anello o una lunga catena di foto non deriva"),
            Field("mapper", "Metodo", "auto",
                  choices=[("auto", "Automatico"), ("global", "Globale (tutte le camere insieme)"),
                           ("incremental", "Incrementale (una foto alla volta)")],
                  tip="Globale: molto piu' veloce con molte foto; automatico lo sceglie da 40 foto in su e torna "
                      "all'incrementale se lascia fuori delle foto"),
            Field("gpu", "GPU per l'abbinamento", str(self.settings.value("sfm/gpu", "auto")),
                  choices=[("auto", "Automatica (la GPU migliore insieme alla CPU)"), ("off", "Spenta (solo CPU)")]
                          + [(str(a["index"]), f"{a['name']} ({a['backend']})") for a in _native.gpu_adapters()],
                  tip="Le coppie di foto si dividono tra la GPU (kernel wgpu: Metal, Vulkan o DirectX 12) e la CPU, "
                      "ognuna alla sua velocità; l'abbinamento guidato resta sulla CPU")],
            intro="Le scelte di Allinea foto di Metashape: precisione, preselezioni, limiti, maschere, punti fermi, "
                  "abbinamento guidato, modello della camera."
                  + ("" if aligned else " Le opzioni che usano un allineamento precedente valgono dal secondo in poi."))
        if not v:
            return
        self.settings.setValue("sfm/gpu", v.get("gpu", "auto"))

        def job(p):
            self.chunk.match_photos(v["quality"], v["keypoint_limit"], v["tiepoint_limit"], progress=p.sub(0, 0.5),
                                    exclude_stationary=bool(v.get("stationary")), generic=bool(v.get("generic")),
                                    reference=v.get("reference") or None, apply_masks=v.get("masks", "keypoints"),
                                    guided_matching=bool(v.get("guided")), reset=bool(v.get("reset", True)),
                                    gpu=v.get("gpu", "auto"))
            n = self.chunk.align_cameras(progress=p.sub(0.5, 0.5), mapper=v.get("mapper", "auto"),
                                         reset=bool(v.get("reset", True)), use_gps=bool(v.get("gps")),
                                         adaptive_fitting=bool(v.get("adaptive")))
            if any(m.reference.location for m in self.chunk.markers):
                self.last_report = self.chunk.update_transform()
            return n

        self.run_task("Allineamento", job, lambda n: self.statusBar().showMessage(
            tr("{0}/{1} foto allineate", n, len(self.chunk.cameras)), 10000))

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
        self.statusBar().showMessage(tr("{0} riferimenti importati", n), 8000)
        self.log_pane.info(tr("{0} riferimenti importati da {1}", n, v['path']))
        self._dirty(True)
        self.refresh_all()
        if self.chunk.aligned_cameras:
            self.update_transform()

    # ---------------------------------------------------------------- scale bars (as in Metashape)
    def new_scalebar(self, keys=None) -> None:
        """The known distance between two markers: it scales the survey (in local coordinates
        without control points, as for objects); several bars are averaged, a check bar only
        measures the error."""
        if not self._need_chunk():
            return
        ch = self.chunk
        if len(ch.markers) < 2:
            self._warn("Servono almeno due marker: rileva i target (Flusso di lavoro > Rileva marker) o mettili "
                       "sulle foto.")
            return
        keys = list(keys or [])
        choices = [(m.key, m.label) for m in ch.markers]
        first = keys[0] if keys else ch.markers[0].key
        second = keys[1] if len(keys) > 1 else next(k for k, _ in choices if k != first)
        try:
            ch.triangulate_markers()  # markers on one photo get their place on the surface
        except Exception as exc:
            self.log_pane.warning(tr("Triangolazione dei marker non riuscita: {0}", exc))
        a, b = ch.marker_by_key(first), ch.marker_by_key(second)
        now = ""
        if a.position is not None and b.position is not None:
            from ..engine.chunks import _is_scaled

            d = _it(ch.scale * float(np.linalg.norm(a.position - b.position)), 4)
            now = (tr(" Ora {0} e {1} distano {2} m (in 3D: con un dislivello e' piu' della distanza in pianta "
                      "misurata sull'ortofoto).", a.label, b.label, d) if _is_scaled(ch) else
                   tr(" Ora {0} e {1} distano {2} unita' arbitrarie (il rilievo non e' ancora in scala).",
                      a.label, b.label, d))
        else:
            missing = [m.label for m in (a, b) if m.position is None]
            now = (tr(" {0} non ha ancora una posizione: mettilo su una foto (tasto destro nella vista Foto) o sul "
                      "modello, sulla nuvola o sull'ortofoto (strumento Marker).", ' e '.join(missing)))
        from ..core import crs as crs_mod

        georef = not crs_mod.is_local(ch.crs)
        geo_text = (tr("\n\nIl chunk e' georeferenziato ({0}): la barra e' proposta come verifica, misura l'errore "
                       "della scala senza cambiarla. Togli la spunta per scalare con la barra: con punti di "
                       "controllo o posizioni delle camere si stima insieme a loro; senza riferimenti il rilievo "
                       "viene scalato attorno alla barra, nel suo sistema (DEM e ortofoto vanno poi ricostruiti).",
                       ch.crs)
                    if georef else "")
        v = ParamDialog.ask(self, "Barra di scala", [
            Field("a", "Marker A", first, choices=choices),
            Field("b", "Marker B", second, choices=choices),
            Field("distance", "Distanza (m)", 1.0, minimum=0.0001, maximum=100000, decimals=4,
                  tip="La distanza misurata tra i centri dei due target"),
            Field("accuracy", "Accuratezza (m)", 0.001, minimum=0.00001, maximum=10, decimals=5,
                  tip="Pesa la barra quando ce ne sono piu' d'una"),
            Field("check", "Solo verifica (check): misura l'errore, non scala", georef),
            Field("update", "Aggiorna subito la scala", True)],
            intro=tr("Come in Metashape: la distanza nota tra due marker mette in scala il rilievo. Senza punti di "
                     "controllo resta in coordinate locali (oggetti, saggi senza stazione totale); piu' barre si "
                     "mediano e quelle di verifica misurano l'errore.") + now + geo_text)
        if not v:
            return
        if v["a"] == v["b"]:
            self._warn("Scegli due marker diversi.")
            return
        try:
            sb = ch.add_scalebar(ch.marker_by_key(v["a"]), ch.marker_by_key(v["b"]), v["distance"], v["accuracy"],
                                 checkpoint=v["check"])
        except (KeyError, ValueError) as exc:
            self._warn(str(exc))
            return
        self.log_pane.info(tr("Barra di scala {0}: {1} m", sb.label, _it(sb.distance,
                                                                         4)) + (" (check)" if sb.checkpoint else ""))
        self._dirty(True)
        self.workspace.populate(self.doc)
        if v["update"] and any(c.aligned for c in ch.cameras):
            self.update_transform()
        else:
            self.reference.set_chunk(ch)
            self._show_pane(self.left_tabs, self.reference)

    def import_scalebars(self) -> None:
        if not self._need_chunk():
            return
        path, _ = QFileDialog.getOpenFileName(self, tr("Barre di scala"), self._last_dir(),
                                              tr("Testo (*.csv *.txt);;Tutti i file (*)"))
        if not path:
            return
        try:
            out = self.chunk.import_scalebars(path)
        except (OSError, ValueError) as exc:
            self._exc("Lettura delle barre di scala non riuscita", exc)
            return
        text = tr("{0} barre di scala importate", len(out['bars']))
        if out["missing"]:
            text += (tr("; {0} senza i loro marker nel chunk ({1}): rileva prima i target",
                        len(out['missing']), ', '.join(out['missing'][:6])))
        self.log_pane.info(text)
        self.statusBar().showMessage(text, 15000)
        self._dirty(True)
        self.workspace.populate(self.doc)
        if out["bars"] and any(c.aligned for c in self.chunk.cameras):
            self.update_transform()
        else:
            self.reference.set_chunk(self.chunk)
            self._show_pane(self.left_tabs, self.reference)

    def print_scalebar(self) -> None:
        from ..markers.targets import generate_scale_bar, scale_bar_codes

        a, b = scale_bar_codes()[0]
        v = ParamDialog.ask(self, "Barra di scala stampabile", [
            Field("path", "File (PDF o PNG)", str(Path(self._last_dir()) / "barra_di_scala.pdf"), kind="file_save",
                  filter="PDF (*.pdf);;PNG (*.png)"),
            Field("target_type", "Tipo", "aruco_4x4_50", choices=TARGET_TYPES),
            Field("distance", "Distanza tra i centri (mm)", 200.0, minimum=50, maximum=380, decimals=1),
            Field("size", "Lato del target (mm)", 40.0, minimum=10, maximum=100, decimals=1),
            Field("page", "Formato", "A4", choices=[("A4", "A4 orizzontale"), ("A3", "A3 orizzontale")]),
            Field("count", "Numero di barre (una per pagina)", 1, minimum=1, maximum=10)],
            intro=tr("Due target codificati a distanza nota (codici {0}-{1} e seguenti a scendere): stampa al 100%, "
                     "controlla il righello di 10 cm e incolla il foglio su un supporto rigido. Insieme al file "
                     "viene scritto il CSV delle barre: dopo aver rilevato i target, importalo con Importa barre.",
                     a, b))
        if not v:
            return
        try:
            files, csv = generate_scale_bar(v["path"], v["target_type"], distance_mm=v["distance"],
                                            marker_size_mm=v["size"], page=v["page"], count=int(v["count"]))
        except Exception as exc:
            self._exc("Generazione della barra di scala non riuscita", exc)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(files[0])))
        self.statusBar().showMessage(tr("Barre di scala: {0}", csv), 15000)
        self.log_pane.info(tr("Barra di scala stampabile: {0} (CSV delle barre: {1})", files[0], csv))

    def update_transform(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        ch = self.chunk
        before = float(ch.scale)

        def done(rep):
            self.last_report = rep
            self.reference.set_chunk(ch, rep)
            self._show_pane(self.left_tabs, self.reference)
            self._draw_markers_on_raster()
            change = float(ch.scale) / before - 1 if before else 0.0
            stale = [n for k, n in (("dem", "DEM"), ("orthomosaic", "ortofoto")) if k in ch.products]
            if abs(change) > 1e-6 and stale:
                text = (tr("La scala del rilievo e' cambiata del {0:+.3f} %: {1} sono ancora nella scala di prima, "
                           "ricostruiscili (Flusso di lavoro).", change * 100, ' e '.join(stale)))
                self.log_pane.warning(text)
                self.statusBar().showMessage(text, 20000)

        self.run_task("Georeferenziazione", lambda p: ch.update_transform(), done)

    def add_marker_at(self, point, where: str = "sul modello") -> None:
        """A marker at a point of the model, the cloud or the orthophoto (internal frame), as
        Metashape's *Add Marker*: it is projected on the photos that see it (not pinned) and is
        then triangulated like the others."""
        from ..markers import gcp

        ch = self.chunk
        if ch is None:
            return
        names = {m.label for m in ch.markers}
        n = 1
        while f"point {n}" in names:
            n += 1
        m = ch.add_marker(f"point {n}", position=np.asarray(point, float))
        seen = gcp.project_marker(ch, m, m.position)
        world = ch.to_world(m.position[None])[0]
        self.log_pane.info(f"Marker {m.label} {tr(where)}: X {world[0]:.3f} Y {world[1]:.3f} Z {world[2]:.3f}; "
                           + (tr("stimato su {0} foto: nel pannello Foto (filtrato su {1}) doppio clic su una foto, "
                                 "poi trascina la bandierina blu sul punto esatto o doppio clic se e' giusta",
                                 seen, m.label)
                              if seen else tr("nessuna foto lo vede: resta dove l'hai messo")))
        self._markers_edited()
        self.model_view.refresh(reset=False)
        if seen:  # its photos, to confirm the flags as in Metashape
            self.filter_photos_by_markers([m.key])
        else:
            self.statusBar().showMessage(tr("Marker {0}: nessuna foto lo vede", m.label), 10000)

    def _draw_markers_on_raster(self) -> None:
        """The markers with a position as a layer of the orthophoto/DEM view."""
        view = self.ortho_view
        view.remove_layer("Marker")
        ch = self.chunk
        if ch is None or view.tr is None:
            return
        for m in ch.markers:
            if m.position is None:
                continue
            x, y, z = ch.to_world(m.position[None])[0]
            view.add_point(float(x), float(y), color="#00e5ff" if not m.checkpoint else "#ff9f1c", label=m.label,
                           layer="Marker", attrs={"marker": m.label, "z": round(float(z), 3)})

    def optimize_cameras(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        ch = self.chunk
        from ..markers import gcp

        controls = [m for m in ch.markers if not m.checkpoint and m.reference.enabled and m.reference.location]
        checks = [m for m in ch.markers if m.checkpoint and m.reference.location]
        bars = gcp.control_scalebars(ch)
        v = ParamDialog.ask(self, "Ottimizza camere", [
            Field("markers", tr("Usa punti di controllo e barre di scala nell'ottimizzazione ({0} GCP, {1} barre)",
                                len(controls), len(bars)), len(controls) >= 3 or len(bars) >= 2),
            Field("weight", "Peso di una misura di marker (in osservazioni di punti di legame)", 4, minimum=1, maximum=50),
            Field("pp", "Ottimizza anche il punto principale (cx, cy)", False),
            Field("engine", "Motore", "colmap", choices=[
                ("colmap", "COLMAP"),
                ("ppg", "Proprio: GCP secondo la loro accuratezza, barre come distanze, GNSS, rolling shutter")]),
            Field("gnss", "Posizioni GNSS delle foto (motore proprio)", "off", choices=[
                ("off", "non usate"), ("plain", "usate con la loro accuratezza"), ("bias", "usate, con un bias per volo")]),
            Field("rolling", "Rolling shutter: la camera si muove mentre legge l'immagine (motore proprio)", False)],
            intro=tr("Bundle adjustment globale di posizioni, orientamenti e calibrazione. Con 3 o piu' punti di "
                     "controllo i GCP entrano nel calcolo come punti fissi alle loro coordinate: il blocco si piega "
                     "per passarci (corregge anche deformazioni come la cupola di una distorsione sbagliata, non "
                     "solo la rototraslazione). I check point ({0}) restano fuori e misurano il risultato.\n\nLe "
                     "barre di scala (due o piu', oppure insieme ai GCP) entrano come in Metashape: i marker alle "
                     "loro estremita' vengono fissati dove le barre hanno la loro lunghezza, spostati quanto "
                     "permette la loro precisione nelle foto, e il blocco si piega verso di loro quanto le foto lo "
                     "consentono. Una sola barra da' soltanto la scala. Le barre di check misurano il risultato.\n\n"
                     "Il motore proprio non fissa i GCP: li avvicina alle coordinate secondo la loro accuratezza, "
                     "tiene le barre come distanze tra i marker, puo' usare le posizioni GNSS delle foto stimando "
                     "un bias per volo (base RTK su un punto sbagliato, offset dell'antenna) e la velocita' di ogni "
                     "foto durante la lettura del sensore (rolling shutter dei droni). Su terreno piatto con sole "
                     "foto nadirali il bias verticale si confonde con la focale: servono GCP o foto oblique.",
                     len(checks)))
        if not v:
            return

        def job(p):
            own = v.get("engine", "colmap") == "ppg"
            err = ch.optimize_cameras(fit_principal_point=v["pp"], use_markers=v["markers"],
                                      marker_weight=int(v["weight"]), progress=p, engine="ppg" if own else "colmap",
                                      gnss=v.get("gnss", "off") if own else "off",
                                      rolling_shutter=bool(v.get("rolling")) and own)
            if any(m.reference.location for m in ch.markers) or ch.scalebars:
                self.last_report = ch.update_transform()
            return err

        def done(err):
            rep = ch.meta.get("sfm", {}).get("optimize", {})
            text = tr("Errore di riproiezione {0:.3f} px", err)
            b, a = rep.get("before", {}), rep.get("after", {})

            def mm(x):
                return "-" if x is None else f"{x * 1000:.1f} mm"

            if rep.get("control_points"):
                text += (tr("; {0} punti di controllo: errore {1} -> {2}",
                            rep['control_points'], mm(b.get('control')), mm(a.get('control')))
                         + (f", check point {mm(b.get('check'))} -> {mm(a.get('check'))}"
                            if a.get("check") is not None else ""))
            if rep.get("scale_bars"):
                text += (tr("; {0} barre di scala: errore {1} -> {2}",
                            rep['scale_bars'], mm(b.get('scalebar_control')), mm(a.get('scalebar_control'))) + (
                             tr(", barre check {0} -> {1}", mm(b.get('scalebar_check')), mm(a.get('scalebar_check')))
                             if a.get("scalebar_check") is not None else ""))
            for flight, bias in (rep.get("gnss_bias") or {}).items():
                text += tr("; bias GNSS del volo {0}: {1}", flight, " ".join(mm(x) for x in bias))
            if rep.get("rolling_shutter_m"):
                text += tr("; rolling shutter: {0} per immagine (mediana)", mm(rep["rolling_shutter_m"]["median"]))
            self.log_pane.info(tr("Ottimizzazione camere: ") + text)
            self.statusBar().showMessage(text, 15000)

        self.run_task("Ottimizzazione camere", job, done)

    def build_dense(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        from ..engine import dense

        v = ParamDialog.ask(self, "Nuvola densa", [
            Field("quality", "Qualita'", "medium", choices=QUALITY),
            Field("filter_mode", "Filtro profondita'", "moderate",
                  choices=[("mild", "Lieve"), ("moderate", "Moderato"), ("aggressive", "Aggressivo")]),
            Field("backend", "Motore", "auto", choices=[("auto", "Automatico"), ("cpu", "CPU (integrato)")]
                  + ([("patchmatch", "PatchMatch multi-vista (piu' preciso, piu' lento)")]
                     if dense.patchmatch_available() else [])
                  + [("colmap", "COLMAP (GPU NVIDIA)"), ("openmvs", "OpenMVS")],
                  tip=tr("Automatico: PatchMatch per i rilievi aerei (foto con il GPS sparse sul sito), lo stereo a "
                         "coppie (CPU) per oggetti e scavi. PatchMatch: un piano per pixel confrontato con piu' foto "
                         "insieme; da drone niente gradini a forma di curve di livello e bordi piu' coperti, circa 8 "
                         "volte piu' lento")),
            Field("only_depth", "Solo le mappe di profondita' (per il modello fuso, senza nuvola densa)", False)]
            + self._version_fields("point_cloud"))
        if not v:
            return
        skip = self._photos_to_skip("Nuvola densa")
        if skip is None:
            return

        def job(p):
            self.chunk.build_depth_maps(v["quality"], v["filter_mode"], v["backend"],
                                        progress=p.sub(0, 1.0 if v["only_depth"] else 0.8), skip_unreadable=skip,
                                        gpu=str(self.settings.value("sfm/gpu", "auto")))
            if v["only_depth"]:
                return None
            return self.chunk.build_point_cloud(progress=p.sub(0.8, 0.2))

        if not v["only_depth"]:
            job = self._versioned("point_cloud", v, job)

        def done(pc):
            if pc is None:
                self.statusBar().showMessage(tr("Mappe di profondita' pronte: Modello > Dati sorgente «Mappe di "
                                                "profondita'»"), 15000)
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
            text = (tr("{0} foto su {1} non si possono leggere:\n{2}\n\nUn file vuoto (0 byte) di solito e' un file "
                       "di Dropbox, iCloud o OneDrive tenuto «solo online»: rendi la cartella disponibile offline, "
                       "oppure collega le foto a un'altra copia (per esempio sul disco esterno).",
                       len(bad), len(cams), names))
            every = len(bad) == len(cams)
            options = ["Ricollega a un'altra cartella..."] + ([] if every else ["Continua senza queste foto"])
            r = choice(self, "warning" if every else "question", tr("{0}: foto non leggibili", step), text, options)
            if r is None:
                return None
            if r == 1:
                self.log_pane.warning(tr("{0} senza {1} foto non leggibili", step, len(bad)))
                return True
            if not self.relink_photos():  # every photo found there: a complete copy replaces the folder
                return None

    def relink_photos(self, only_unreadable: bool = False) -> bool:
        """Point the photos to the files with the same names in another folder (and its
        subfolders): a copy on another disk, a folder moved or renamed."""
        if not self._need_chunk():
            return False
        folder = QFileDialog.getExistingDirectory(self, tr("Cartella con le foto (anche nelle sottocartelle)"),
                                                  self._last_dir())
        if not folder:
            return False
        try:
            r = self.chunk.relink_photos(folder, only_unreadable=only_unreadable)
        except Exception as exc:
            self._exc("Ricollegamento delle foto non riuscito", exc)
            return False
        text = tr("{0} foto collegate a {1}", r['relinked'], folder)
        if r["not_found"]:
            text += tr("; {0} non trovate ({1}", len(r['not_found']), ', '.join(r['not_found'][:3])) + \
                ("...)" if len(r["not_found"]) > 3 else ")")
        if r["unreadable"]:
            text += tr("; ancora {0} non leggibili", r['unreadable'])
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

        self.history.backup(["point_cloud"])

        def job(p):
            p(0.2, "Ritaglio")
            return self.chunk.crop_point_cloud(zmin=v["zmin"], zmax=v["zmax"] if v["use_zmax"] else None,
                                               radius=v["radius"] if v["use_radius"] else None,
                                               center=(v["cx"], v["cy"]))

        self.run_task("Ritaglio nuvola", job,
                      lambda pc: (self.statusBar().showMessage(tr("{0} punti", _it(len(pc))), 8000),
                                  self._show_model("point_cloud")))

    def reimport_metashape_model(self) -> None:
        """The model of the Metashape project again, with its colours and texture (the model in
        use is kept among the versions)."""
        if not self._need_chunk():
            return
        ch = self.chunk
        info = ch.meta.get("import") or {}
        psx = info.get("psx") if info.get("source") == "metashape" else None
        if not psx or not Path(psx).exists():
            path, _ = QFileDialog.getOpenFileName(self, tr("Progetto Metashape del chunk"), self._last_dir(),
                                                  "Metashape (*.psx)")
            if not path:
                return
            psx = path

        def job(p):
            from ..integrations.metashape import reimport_model

            return reimport_model(ch, psx, progress=p)

        def done(r):
            text = tr("Modello di Metashape reimportato: {0} facce", _it(r['faces'] or 0)) + \
                (tr(", con la texture") if r["texture"] else tr(", senza texture")) + \
                tr(" (il modello di prima e' tra le versioni)")
            for w in r["warnings"]:
                self.log_pane.warning(w)
            self.log_pane.info(text)
            self.statusBar().showMessage(text, 15000)
            self._show_model("model_textured" if r["texture"] else "model")

        self.run_task("Modello di Metashape", job, done)

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
                self.log_pane.info(tr("Confidenza {0} (media {1}): ", names[k].lower(), _it(info['mean'], 1))
                                   + self._confidence_text(info["histogram"]))
            first = "point_cloud" if "point_cloud" in res else "model"
            self.show_confidence(first)
            self.statusBar().showMessage(tr("Confidenza calcolata: nella legenda togli la spunta a una fascia per "
                                            "nasconderla; Pulisci toglie i punti o le facce sotto una soglia"), 15000)

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

            hist_text = tr("\n\nConfidenza attuale - ") + self._confidence_text(histogram(conf))
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
                            intro=tr("{0} punti. Prima vengono contati i punti da togliere e mostrati in rosso nella "
                                     "vista 3D; poi confermi.", _it(self._cloud_size())) + hist_text)
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
            parts = ", ".join(f"{tr(names.get(k, k))} {_it(c)}" for k, c in info["by_filter"].items() if c)
            self._show_model("point_cloud")
            self.model_view.highlight = remove  # shown in red while the question is open
            self.model_view.refresh(reset=False)
            r = choice(self, "question", "Pulisci la nuvola densa",
                       tr("Da togliere: {0} punti su {1} ({2}%) - {3}.\n\nSono mostrati in rosso nella vista 3D.",
                          _it(info['removed']), _it(info['total']), _it(100 * info['removed'] / n, 2), parts),
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
                          lambda out: (self.log_pane.info(tr("Nuvola pulita: tolti {0} punti, restano {1} ({2})",
                                                             _it(out['removed']), _it(out['kept']), parts)),
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
            intro=tr("Modello di {0} facce", _it(n_faces))
                  + (tr("; confidenza dei vertici calcolata.") if conf is not None else
                     tr("; la confidenza dei vertici verra' calcolata dalle mappe di profondita' se usi quel filtro."))
                  + tr(" La texture va ricostruita dopo la pulizia."))
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
            parts = ", ".join(f"{tr(names.get(k, k))} {_it(c)}" for k, c in info["by_filter"].items() if c)
            text = (tr("Modello pulito: {0} facce (prima {1})", _it(info['faces']), _it(info['faces_before']))
                    + (tr("; tolte {0}", parts) if parts else "")
                    + (tr("; {0} facce per chiudere i buchi",
                          _it(info['filled_faces'])) if info.get("filled_faces") else ""))
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

            self.history.backup(["tie_points", "reconstruction"])

            def done(out):
                text = tr("Punti di legame: tolti {0}, restano {1}; errore di riproiezione {2} -> {3} px",
                          _it(out['removed']), _it(out['remaining']), _it(out['reprojection_error_before'], 3),
                          _it(out['reprojection_error_after'], 3))
                if "optimize_error" in out:
                    text += tr("; dopo l'ottimizzazione {0} px", _it(out['optimize_error'], 3))
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
        if self.history is not None:  # Modifica > Annulla puts the files back
            self.history.backup([product] + (["reconstruction"] if product == "tie_points" else []))

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
            intro=(tr("Poligono di {0} m2 ({1} x {2} m). DEM e ortofoto tengono la finestra del poligono, vuota "
                      "(trasparente) fuori; il modello le facce con il centro dentro; la nuvola i punti dentro.",
                      _it(geom.area, 2), _it(x1 - x0, 2), _it(y1 - y0, 2))))
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
                                   tr("Nella cartella ci sono gia': {0}. Sovrascriverli?", ', '.join(existing)),
                                   ["Sovrascrivi"]) != 0:
                return

            def job(p):
                import json

                folder.mkdir(parents=True, exist_ok=True)
                out = []
                for i, k in enumerate(v["what"]):
                    p(i / len(v["what"]), tr("Ritaglio: {0}", names[k]))
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
                    self.log_pane.info(tr("Esportato {0}", path))
                self.statusBar().showMessage(tr("Ritagli esportati in {0}", folder), 15000)
                self._info("Ritaglia con un poligono", tr("File scritti in {0}:\n", folder) +
                           "\n".join(Path(x).name for x in paths))

            self.run_task("Esportazione dei ritagli", job, done, refresh=False, save=False)
            return

        self.history.backup([k for k in v["what"] if k in ("point_cloud", "model")])

        def job(p):
            from ..core import versions

            done = []
            for i, k in enumerate(v["what"]):
                p(i / len(v["what"]), tr("Ritaglio: {0}", names[k]))
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
            text = tr("Ritagliati nel progetto: ") + ", ".join(tr(names[k]) for k in products)
            if v["keep"]:
                text += tr(" (gli originali sono tra le versioni: tasto destro nell'area di lavoro)")
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
            intro=(tr("Con le mappe di profondita' la superficie nasce dalla fusione volumetrica delle mappe (TSDF), "
                      "senza passare dalla nuvola densa: piu' fedele e con i colori delle foto.") if depth_maps else
                   tr("Costruisci le mappe di profondita' (Nuvola densa) per avere anche la fusione delle mappe."))
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
        from ..io.mesh_ply import face_count

        faces = face_count(self.chunk.product_path("model")) or 0
        v = ParamDialog.ask(self, "Texture", [
            Field("mapping", "Mappatura", "auto",
                  choices=[("auto", "Automatica"), ("orthophoto", "Ortofoto adattiva (terreno, rilievi da drone)"),
                           ("generic", "Generica (oggetti, modelli piccoli)")]),
            Field("size", "Dimensione texture", 8192 if faces > 1_000_000 else 4096,
                  choices=[(2048, "2048"), (4096, "4096"), (8192, "8192"), (16384, tr("16384 (4 pagine da 8192)")),
                           (32768, tr("32768 (16 pagine da 8192)"))],
                  tip=tr("Oltre 8192 pixel la texture e' divisa in pagine, come in Metashape: ogni pagina resta "
                         "leggibile dalle schede grafiche e dai visualizzatori")),
            Field("colour", "Correzione colore (esposizione delle foto, vignettatura)", True,
                  tip="Misurata la prima volta sui punti visti da piu' foto (Strumenti > Calibra i colori)")],
            intro=thousands_points(tr("Modello di {0:,} facce. ", faces))
                  + tr("Ortofoto adattiva: la texture e' l'ortofoto del modello e le pareti ripide hanno spazi propri "
                       "(come in Metashape, per i rilievi dall'alto); generica: ogni faccia ha il suo spazio "
                       "nell'immagine (per oggetti 3D; su milioni di facce ognuna resta di pochi pixel). "
                       "Automatica sceglie l'ortofoto per i modelli grandi rivolti verso l'alto."))
        if not v:
            return
        skip = self._photos_to_skip("Texture")
        if skip is None:
            return
        self.run_task("Texture", lambda p: self.chunk.build_texture(v["size"], progress=p, skip_unreadable=skip,
                                                                    mapping=v.get("mapping", "auto"),
                                                                    colour_correction=bool(v.get("colour", True))),
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
            Field("blending", "Fusione", "mosaic",
                  choices=[("mosaic", "Mosaico senza cuciture"), ("disabled", "Nessuna (una foto per cella)"),
                           ("average", "Media delle foto")],
                  tip="Mosaico: i dettagli dalla foto migliore, le differenze di luce sfumate lungo le cuciture"),
            Field("colour", "Correzione colore (esposizione delle foto, vignettatura)", True,
                  tip="Misurata la prima volta sui punti visti da piu' foto (Strumenti > Calibra i colori)"),
            Field("ghosting", "Togli persone e mezzi visti da poche foto", False,
                  tip="Dove almeno tre foto vedono la cella si prende la migliore che concorda con le altre (piu' lento)"),
            Field("fill_holes", "Riempi i piccoli buchi (fino a 100 pixel)", True,
                  tip="Celle che nessuna foto vede, riempite dai bordi; i buchi grandi restano trasparenti"),
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
            max_angle=v.get("max_angle", 70.0), colour_correction=bool(v.get("colour", True)),
            ghosting=bool(v.get("ghosting")), fill_holes=bool(v.get("fill_holes", True)))),
            lambda _r: self._show_raster("ortho"))

    # ---------------------------------------------------------------- reconstruction region
    def _region_changed(self, text: str) -> None:
        text = tr(text)
        self._dirty(True)
        self.model_view.refresh(reset=False)
        r = self.chunk.region
        if r is not None:
            text += "  ({} x {} x {} m)".format(*(_it(v, 2) for v in r.size))
        self.statusBar().showMessage(text, 10000)
        self.log_pane.info(text + tr(": depth map, nuvola densa e modello costruiti da ora in poi ne restano dentro"))

    def region_reset(self, angle_deg: float = 0.0) -> None:
        if not self._need_chunk(aligned=True):
            return
        try:
            self.chunk.reset_region(angle_deg=angle_deg)
        except Exception as exc:
            self._exc("Regione non impostata", exc)
            return
        self._region_changed("Regione attorno ai punti di legame")

    def region_rotate_to_view(self) -> None:
        """The region around the tie points, turned so that its sides face the 3D view."""
        if not self._need_chunk(aligned=True):
            return
        canvas = self.model_view.canvas
        _o, d, _s = canvas.view_ray(canvas.width() / 2, canvas.height() / 2)
        world = self.chunk.transform[:3, :3] @ np.asarray(d, float)
        if np.hypot(world[0], world[1]) < 1e-6:  # looking straight down: as it is
            angle = 0.0
        else:
            angle = float(np.degrees(np.arctan2(world[1], world[0])))
        self.region_reset(angle)

    def region_from_selection(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        geom = self._selected_polygon()
        if geom is None:
            self._warn("Seleziona un poligono sull'ortofoto o sul DEM (strumento Seleziona).")
            return
        from ..engine.region import region_from_box

        region_from_box(self.chunk, geom.bounds)
        self._region_changed("Regione sul poligono selezionato")

    def region_edit(self) -> None:
        if not self._need_chunk(aligned=True):
            return
        from ..engine.region import Region, rotation_z

        r = self.chunk.region
        if r is None:
            try:
                r = self.chunk.reset_region()
            except Exception as exc:
                self._exc("Regione non impostata", exc)
                return
        angle = float(np.degrees(np.arctan2(r.rotation[1, 0], r.rotation[0, 0])))
        fields = [Field(k, label, float(val), minimum=-1e9, maximum=1e9, decimals=3)
                  for k, label, val in (("cx", "Centro X", r.center[0]), ("cy", "Centro Y", r.center[1]),
                                        ("cz", "Centro Z", r.center[2]), ("sx", "Larghezza (m)", r.size[0]),
                                        ("sy", "Profondita' (m)", r.size[1]), ("sz", "Altezza (m)", r.size[2]))]
        fields.append(Field("angle", "Rotazione attorno alla verticale (gradi)", angle, minimum=-360.0,
                            maximum=360.0, decimals=2))
        v = ParamDialog.ask(self, "Regione di ricostruzione", fields, intro="Coordinate del sistema del chunk.")
        if not v:
            return
        self.chunk.region = Region([v["cx"], v["cy"], v["cz"]], [v["sx"], v["sy"], v["sz"]], rotation_z(v["angle"]))
        self._region_changed("Regione modificata")

    def region_clear(self) -> None:
        if not self._need_chunk():
            return
        self.chunk.region = None
        self._dirty(True)
        self.model_view.refresh(reset=False)
        self.statusBar().showMessage(tr("Nessuna regione: si ricostruisce tutto cio' che le foto vedono"), 8000)

    # ---------------------------------------------------------------- DEM editing
    def _dem_edit_drawn(self, xy) -> None:
        from shapely.geometry import Polygon

        geom = Polygon(np.asarray(xy, float)[:, :2])
        if not geom.is_valid or geom.area <= 0:
            self._warn("L'area non e' valida (i lati si incrociano o non ha area): ridisegnala.")
            return
        self.edit_dem_area(geom)

    def _terrain_at(self, x: float, y: float, z: float) -> str:
        """Slope and aspect of the DEM under the cursor and, with a DTM kept as a version, the
        height above the ground."""
        from ..archaeo import measures

        ch = self.chunk
        out = ""
        try:
            sa = measures.slope_aspect_at(ch.dem_path, x, y)
        except Exception:
            sa = None
        if sa is not None:
            s, a = sa
            out += tr("  pend. {0:.0f}°", s) + (tr(" esp. {0}", _compass(a)) if a >= 0 else "")
        key = (ch.key, tuple(sorted(ch.products.items())), len((ch.meta.get("versions") or {}).get("dem", [])))
        if getattr(self, "_ground_key", None) != key:
            self._ground_key, self._ground_path = key, measures.ground_dem(ch)
        if self._ground_path is not None and Path(self._ground_path) != Path(ch.dem_path):
            g = measures.ground_dem_at(self._ground_path, x, y)
            if g is not None:
                out += decimal_comma(tr("  dal suolo {0:.2f} m", z - g))
        return out

    def _area_drawn(self, xy) -> None:
        from shapely.geometry import Polygon

        self.measure_area(Polygon(xy).buffer(0))

    def _area_selected(self) -> None:
        geom = self._selected_polygon()
        if geom is None:
            self._warn("Seleziona un poligono, oppure disegna l'area con lo strumento Misura area.")
            return
        self.measure_area(geom)

    def measure_area(self, geom) -> None:
        """Area in plan and in 3D of a plan polygon: from the DEM and from the faces of the model."""
        ch = self.chunk
        if ch is None or ch.dem_path is None:
            self._warn("Costruisci prima il DEM.")
            return
        from ..archaeo.measures import surface_area

        def job(p):
            p(0.1, "Misura dell'area")
            model = None
            if ch.product_path("model") is not None:
                model = ch.model
                if model is not None:
                    model.apply_transform(ch.transform)
            r = surface_area(ch.dem_path, geom, model=model)
            if ch.orthomosaic_path is not None:  # and its colour, when colour-science is there
                from ..archaeo.munsell import area_colour

                try:
                    r["colour"] = area_colour(ch.orthomosaic_path, geom, (ch.meta.get("colour_chart") or {}).get("ccm"))
                except (ImportError, ValueError) as exc:
                    log.info("Colore Munsell non calcolato: %s", exc)
            return r

        def done(r):
            if "plan" not in r:
                self._warn("L'area e' fuori dal DEM.")
                return
            lines = [tr("Area in pianta {0:.3f} m2, superficie {1:.3f} m2 (+{2:.1f}%)",
                        r['plan'], r['surface'], (r['surface'] / max(r['plan'], 1e-12) - 1) * 100),
                     tr("Pendenza media {0:.1f}°, quote da {1:.3f} a {2:.3f} m",
                        r['mean_slope'], r['z_min'], r['z_max'])]
            if r.get("model_faces"):
                lines.append(tr("Dal modello ({0} facce): pianta {1:.3f} m2, superficie {2:.3f} m2",
                                r['model_faces'], r['model_plan'], r['model_surface']))
            text = "\n".join(decimal_comma(line) for line in lines)
            if r.get("colour"):  # Munsell notation keeps its points
                c = r["colour"]
                text += "\n" + tr("Colore Munsell {0} ({1}){2}", c["chip"], c["notation"],
                                  "" if ch.meta.get("colour_chart") else tr(", senza calibrazione con la tabella colori"))
            coords = list(geom.exterior.coords) if geom.geom_type == "Polygon" else \
                list(max(geom.geoms, key=lambda g: g.area).exterior.coords)
            self._add_line_feature("Misure", coords, "#ff006e", decimal_comma(f"{r['surface']:.2f} m2"),
                                   {"area_pianta_m2": round(r["plan"], 4), "superficie_m2": round(r["surface"], 4),
                                    "pendenza_media": round(r["mean_slope"], 2)})
            self._info("Misura dell'area", text)

        self.run_task("Misura dell'area", job, done, refresh=False, save=False)

    def export_qfield(self) -> None:
        """File > Esporta > Pacchetto QField (:mod:`io.qfield`)."""
        if not self._need_chunk():
            return
        from ..io import qfield
        from ..io.qgis_link import find_qgis

        folder = QFileDialog.getExistingDirectory(self, tr("Cartella in cui creare il pacchetto QField"),
                                                  self._last_dir())
        if not folder:
            return
        qgis = find_qgis()
        v = ParamDialog.ask(self, "Pacchetto QField", [
            Field("resolution", "Pixel dell'ortofoto sul tablet (m)", 0.02, minimum=0.001, maximum=1.0, decimals=3),
            Field("project", "Progetto per QField (serve QGIS)", qgis is not None and bool(qgis.python))],
            intro=tr("Una cartella con l'ortofoto (ridotta per il tablet), il DEM e un GeoPackage con le unita' e le "
                     "quote di questo chunk, da copiare sul tablet o su QFieldCloud. Al ritorno, File > Importa > "
                     "Modifiche da QField riporta qui cio' che e' stato disegnato o corretto."))
        if not v:
            return
        import uuid

        for u in self.units:  # the identity of every feature in the package, kept with the drawings
            if not str((u.attributes or {}).get("_layer") or "").startswith("US da PyArchInit - "):
                u.attributes.setdefault("ppg_id", uuid.uuid4().hex)
        for q in self.quote:
            q.setdefault("ppg_id", uuid.uuid4().hex)
        self._store_drawings()
        ch = self.chunk
        target = Path(folder) / ("".join(c if c.isalnum() or c in "-_" else "_" for c in ch.label) + "_qfield")

        def job(p):
            return qfield.export_package(ch, target, resolution=v["resolution"], project=v["project"], progress=p)

        def done(out):
            self._dirty(True)
            name = out["project"].name if out["project"] else qfield.GPKG
            self._info("QField", tr("Pacchetto QField in {0}: {1} unita', {2} quote. Copia la cartella sul tablet (o "
                                    "su QFieldCloud) e apri {3} con QField; al ritorno, File > Importa > Modifiche "
                                    "da QField.", out["folder"], out["units"], out["quote"], name))

        self.run_task("Pacchetto QField", job, done, refresh=False)

    def import_qfield(self, folder: str | None = None) -> None:
        """File > Importa > Modifiche da QField: the edits made on the tablet back in the chunk."""
        if not self._need_chunk():
            return
        from ..io import qfield

        if folder is None:
            folder = QFileDialog.getExistingDirectory(self, tr("Cartella del pacchetto QField tornato dal tablet"),
                                                      self._last_dir())
            if not folder:
                return
        if not (Path(folder) / qfield.RECORD).exists():
            self._warn(tr("{0} non e' un pacchetto QField di questo programma (manca {1}).", folder, qfield.RECORD))
            return
        self._store_drawings()
        try:
            n = qfield.import_edits(self.chunk, folder)
        except (ValueError, OSError) as exc:
            self._exc(tr("Modifiche da QField non importate"), exc)
            return
        self._reload_drawings()
        self._dirty(True)
        u, q = n["units"], n["quote"]
        self._info("QField", tr("Dal tablet: unita' {0} nuove, {1} modificate, {2} eliminate; quote {3} nuove, {4} "
                                "modificate, {5} eliminate. Verso PyArchInit: Pubblica in PyArchInit per le nuove, "
                                "Salva modifiche in PyArchInit per le altre.", u["added"], u["changed"], u["deleted"],
                                q["added"], q["changed"], q["deleted"]))

    def calibrate_colour_chart(self) -> None:
        """Strumenti > Calibra il colore con la tabella colori: a box around a ColorChecker
        photographed in the trench, on the orthomosaic (:mod:`archaeo.munsell`)."""
        if not self._need_chunk(product="orthomosaic"):
            return
        self._show_raster("ortho")
        self.show_view(self.ortho_view)
        self.ortho_view.set_mode("chart")
        self.statusBar().showMessage(tr("Trascina un riquadro attorno alla tabella colori (ColorChecker da 24 colori), "
                                        "oppure clic per i vertici e doppio clic per chiudere"), 30000)

    def _chart_drawn(self, xy) -> None:
        from datetime import datetime

        from ..ai.sam import read_orthomosaic
        from ..archaeo.munsell import calibrate_chart

        self.set_draw_mode("pan")
        ch = self.chunk
        (x0, y0), (x1, y1) = np.min(xy, axis=0), np.max(xy, axis=0)

        def job(p):
            p(0.2, "Ricerca della tabella colori")
            img, _valid, _tr = read_orthomosaic(ch.orthomosaic_path, (x0, y0, x1, y1), max_side=2400)
            return calibrate_chart(img)

        def done(cal):
            ch.meta["colour_chart"] = {"ccm": cal["ccm"], "loss": cal["loss"], "bounds": [float(x0), float(y0),
                                                                                          float(x1), float(y1)],
                                       "date": datetime.now().isoformat(timespec="seconds")}
            self._dirty(True)
            self._info("Colore Munsell", tr("Tabella colori trovata: corretti, i 24 colori distano in media {0} "
                                            "(CIEDE2000) dai valori di riferimento. Da ora i colori Munsell misurati "
                                            "(Misura area) sono calibrati.", number(cal["loss"], 1)))

        self.run_task("Calibrazione del colore", job, done, refresh=False)

    def terrain_raster(self, kind: str) -> None:
        """Strumenti > Pendenza / Esposizione del DEM: a raster of degrees, shown at once."""
        ch = self.chunk
        if ch is None or ch.dem_path is None:
            self._warn("Costruisci prima il DEM.")
            return
        from ..archaeo import measures, terrain

        out = ch.data_dir / "terrain" / f"{kind}.tif"
        out.parent.mkdir(parents=True, exist_ok=True)
        from ..core import crs as crs_mod

        crs = None if crs_mod.is_local(ch.crs) else ch.crs

        def job(p):
            p(0.2, "Pendenza del DEM" if kind == "slope" else "Esposizione del DEM")
            if kind == "slope":
                terrain.slope_degrees(ch.dem_path, out, crs=crs)
            else:
                measures.aspect_raster(ch.dem_path, out, crs=crs)
            ch._set_product(kind, out)
            return out

        def done(path):
            self.workspace.populate(self.doc)
            self.show_terrain_raster(kind)

        self.run_task("Pendenza del DEM" if kind == "slope" else "Esposizione del DEM", job, done, refresh=False)

    RELIEF_CHOICES = [("svf", "Sky-view factor (quanto cielo si vede)"),
                      ("openness_pos", "Openness positiva (creste, muri)"),
                      ("openness_neg", "Openness negativa (fossati, buche)"),
                      ("slrm", "Rilievo locale (DEM meno la sua media)"),
                      ("msrm", "Rilievo multiscala (MSRM)"),
                      ("multi_hillshade", "Ombreggiatura da 16 direzioni")]
    RELIEF_HINTS = {"svf": "Sky-view factor: bianco cielo aperto, scuro nei fossati e al piede dei muri",
                    "openness_pos": "Openness positiva: chiari creste e muri, scuri i fondi (gradi sotto il cursore)",
                    "openness_neg": "Openness negativa: scuri fossati e buche (gradi sotto il cursore)",
                    "slrm": "Rilievo locale: rosso sopra i dintorni, blu sotto (cm sotto il cursore)",
                    "msrm": "Rilievo multiscala: rosso sopra i dintorni, blu sotto (cm sotto il cursore)",
                    "multi_hillshade": "Ombreggiatura da 16 direzioni: nessun elemento nascosto dalla luce"}

    def relief_visualisation(self, method: str | None = None) -> None:
        """Strumenti > Visualizzazioni del rilievo (:mod:`archaeo.relief`), shown at once."""
        if not self._need_chunk(product="dem"):
            return
        import rasterio

        from ..archaeo import relief

        ch = self.chunk
        with rasterio.open(ch.dem_path) as src:
            res = abs(src.transform.a)
        radius = relief.default_radius(res)
        v = ParamDialog.ask(self, "Visualizzazioni del rilievo", [
            Field("method", "Visualizzazione", method or "svf", choices=self.RELIEF_CHOICES),
            Field("radius", "Raggio di ricerca (m)", radius, minimum=res, maximum=relief.MAX_RADIUS_CELLS * res,
                  decimals=3, tip="Fin dove cercare l'orizzonte e la media dei dintorni: un po' piu' della meta' "
                                  "degli elementi da vedere"),
            Field("directions", "Direzioni", 16, minimum=4, maximum=64),
            Field("min_size", "Rilievo multiscala: elementi da (m)", round(4 * res, 3), minimum=res, maximum=1e4,
                  decimals=3),
            Field("max_size", "Rilievo multiscala: fino a (m)", 2 * radius, minimum=res, maximum=1e4, decimals=3)],
            intro=tr("DEM di {0} cm per cella. Muri, fossati, terrazzamenti e tracciati appena accennati, che i "
                     "colori delle quote e un'ombreggiatura sola nascondono.", _it(res * 100, 1)))
        if not v:
            return

        def done(_path):
            self.workspace.populate(self.doc)
            self.show_relief(v["method"])

        self.run_task("Visualizzazione del rilievo", lambda p: ch.build_relief(
            v["method"], radius=v["radius"], directions=int(v["directions"]), min_size=v["min_size"],
            max_size=v["max_size"], progress=p), done, refresh=False)

    def show_relief(self, kind: str) -> None:
        path = self.chunk.product_path(kind) if self.chunk is not None else None
        if path is None or not path.exists():
            self.relief_visualisation(kind)
            return
        self._load_raster(kind, path)
        self.show_view(self.ortho_view)
        self.statusBar().showMessage(tr(self.RELIEF_HINTS[kind]), 20000)

    def show_terrain_raster(self, kind: str) -> None:
        path = self.chunk.product_path(kind) if self.chunk is not None else None
        if path is None or not path.exists():
            self.terrain_raster(kind)
            return
        self._load_raster(kind, path)
        self.show_view(self.ortho_view)
        self.statusBar().showMessage(
            tr("Pendenza: bianco piano, rosso scuro ripido (gradi sotto il cursore)") if kind == "slope" else
            tr("Esposizione: verso dove scende il terreno - rosso nord, giallo est, azzurro sud, viola ovest; grigio "
               "piano"),
            20000)

    def _dem_edit_selected(self) -> None:
        geom = self._selected_polygon()
        if geom is None:
            self._warn("Seleziona un poligono, oppure disegna l'area con lo strumento Modifica DEM.")
            return
        self.edit_dem_area(geom)

    def _selected_lines(self) -> list:
        """The lines selected in the raster view (world XY), e.g. as breaklines."""
        out = []
        for layer, _i, attrs in self.ortho_view.selected_features():
            g = attrs.get("_geom")
            if self.ortho_view.layers.get(layer, {}).get("kind") == "line" and g and len(g[1]) >= 2:
                out.append(np.asarray(g[1], float)[:, :2])
        return out

    def edit_dem_area(self, geom) -> None:
        """Fill the DEM inside ``geom`` from the ground around it (the DEM as it was is kept)."""
        if not self._need_chunk(product="dem"):
            return
        lines = self._selected_lines()
        v = ParamDialog.ask(self, "Modifica DEM", [f for f in [
            Field("method", "Nuove quote", "plane",
                  choices=[("plane", "Piano che si adatta al terreno attorno"),
                           ("linear", "Interpolazione lineare dal bordo"),
                           ("idw", "Media pesata con la distanza (IDW)"),
                           ("constant", "Quota costante")]),
            Field("value", "Quota costante (vuota: la mediana del bordo)", None, kind="optional_float"),
            Field("breaklines", tr("Usa le {0} linee selezionate come linee di discontinuita'", len(lines)),
                  bool(lines))
            if lines else None,
            Field("name", "Nome del DEM modificato (facoltativo)", "", kind="text")] if f is not None],
            intro=tr("{0} m2. Le quote dentro l'area vengono ricavate da quelle appena fuori (alberi, tende, "
                     "mezzi, cumuli e buchi spariscono da curve di livello e volumi). Il DEM com'era resta tra le "
                     "versioni per confrontarlo o tornare indietro.", _it(geom.area, 2)))
        if not v:
            return
        breaklines = lines if v.get("breaklines") else None
        chunk = self.chunk
        self.run_task("Modifica del DEM", lambda p: chunk.edit_dem(
            geom, v["method"], v.get("value"), breaklines, True, (v.get("name") or "").strip() or None, progress=p),
            lambda info: (self._show_raster("dem"), self.log_pane.info(
                tr("DEM modificato: {0} celle, variazione media {1} cm, massima {2} cm", _it(info['cells']),
                   ("+" if info['dz_mean'] >= 0 else "") + _it(info['dz_mean'] * 100, 1),
                   _it(info['dz_max'] * 100, 1)))))

    # ---------------------------------------------------------------- elevations
    def _elevation_fields(self, kind: str) -> list:
        fields = []
        if kind == "plane":
            fields.append(Field("side", "Lato da cui guardare", "auto",
                                choices=[("auto", "Dove sono le foto"), ("left", "A sinistra della traccia"),
                                         ("right", "A destra della traccia")]))
        if kind == "cylinder":
            fields.append(Field("inside", "Visto", True, choices=[(True, "Dall'interno (pozzo, abside)"),
                                                                   (False, "Dall'esterno (torre, colonna)")]))
        return fields + [
            Field("front", "Profondita' davanti (m)", 0.5, minimum=0.0, maximum=1000.0, decimals=3,
                  tip="Cio' che sta piu' avanti (impalcature, una sezione davanti al muro) non copre la parete"),
            Field("back", "Profondita' dietro (m)", 0.5, minimum=0.0, maximum=1000.0, decimals=3),
            Field("resolution", "Risoluzione (m/pixel)", None, kind="optional_float"),
            Field("blending", "Fusione", "mosaic",
                  choices=[("mosaic", "Mosaico senza cuciture"), ("disabled", "Nessuna (una foto per cella)")]),
            Field("colour", "Correzione colore", True),
            Field("name", "Nome", "", kind="text")]

    def build_elevation(self, section: dict | None = None) -> None:
        """Workflow > Prospetto: on the trace of a section, on the plane of the selected markers,
        or on the cylinder of the selected polygon (a well, a tower)."""
        if not self._need_chunk(aligned=True, product="model"):
            return
        from ..engine import elevation as E

        if section is None:
            markers = self.reference.selected_markers()
            poly = self._selected_polygon()
            choices = []
            if self.sections:
                choices += [(("section", i), tr("Traccia della sezione {0}", s['label'])) for i,
                            s in enumerate(self.sections)]
            if len(markers) >= 3:
                choices.append((("markers", None), tr("Piano dei {0} marker selezionati", len(markers))))
            if poly is not None:
                choices.append((("cylinder", None), "Cilindro dal poligono selezionato (pozzo, torre)"))
            if not choices:
                self._warn("Traccia una sezione lungo il muro (Disegno > Traccia sezione), oppure seleziona tre o piu' "
                           "marker sulla parete nel pannello Riferimenti, oppure seleziona un poligono attorno a un "
                           "pozzo o a una torre.")
                return
            v = ParamDialog.ask(self, "Prospetto", [Field("source", "Superficie", choices[0][0], choices=choices)]
                                + self._elevation_fields("cylinder" if choices[0][0][0] == "cylinder" else "plane"),
                                intro="Ortofoto della parete e rilievo (distanza dal piano o dal cilindro) costruiti "
                                      "sul modello.")
            if not v:
                return
            kind, idx = v["source"]
            if kind == "section":
                section = self.sections[idx]
        else:
            v = ParamDialog.ask(self, f"Prospetto lungo {section['label']}", self._elevation_fields("plane"))
            if not v:
                return
            kind = "section"
        try:
            if kind == "section":
                a, b = np.asarray(section["p1"], float), np.asarray(section["p2"], float)
                side = None
                if v.get("side") in ("left", "right"):
                    d = b - a
                    left = np.array([-d[1], d[0]])
                    side = (a + b) / 2 + (left if v["side"] == "left" else -left)
                frame = E.plane_from_line(self.chunk, a, b, side)
                name = v.get("name") or f"Prospetto {section['label']}"
            elif kind == "markers":
                ms = [self.chunk.marker_by_key(k) for k in self.reference.selected_markers()]
                pts = [m.position for m in ms if m.position is not None]
                if len(pts) < 3:
                    self._warn("I marker selezionati non hanno ancora una posizione (allinea le foto).")
                    return
                world = self.chunk.to_world(np.array(pts))
                cams = self.chunk.to_world(np.array([c.center for c in self.chunk.aligned_cameras]))
                frame = E.plane_from_points(world, toward=cams.mean(axis=0))
                name = v.get("name") or "Prospetto sui marker"
            else:
                poly = self._selected_polygon()
                ring = np.asarray(poly.exterior.coords if hasattr(poly, "exterior") else poly.geoms[0].exterior.coords)
                frame = E.cylinder_from_circle(self.chunk, ring, inside=bool(v.get("inside", True)))
                name = v.get("name") or ("Sviluppo interno" if frame.inside else "Sviluppo esterno")
        except Exception as exc:
            self._exc("Superficie del prospetto non valida", exc)
            return
        depth = (-float(v.get("back", 0.5)), float(v.get("front", 0.5)))
        chunk = self.chunk
        self.run_task("Prospetto", lambda p: E.build_elevation(
            chunk, frame, v.get("resolution"), name, depth, v.get("blending", "mosaic"),
            bool(v.get("colour", True)), progress=p), self.show_elevation, refresh=False)

    def show_elevation(self, entry: dict) -> None:
        from ..engine.elevation import elevation_paths

        ortho, relief = elevation_paths(self.chunk, entry)
        if not ortho.exists():
            self._warn(tr("Il prospetto «{0}» non ha piu' i suoi file.", entry['name']))
            return
        self.ortho_view.load(ortho, "ortho")
        if relief.exists():
            self.ortho_view.set_dem(relief)
        self._draw_stones()
        self._raster_version = ("elevation", entry["id"])
        self.show_view(self.ortho_view)
        self.workspace.populate(self.doc)
        self.statusBar().showMessage(tr("{0}: coordinate del prospetto in metri (u lungo la parete, v in alto); Z = "
                                        "rilievo verso chi guarda", entry['name']), 15000)
        self._dirty(True)

    def export_elevation(self, entry: dict) -> None:
        import shutil

        from ..engine.elevation import elevation_paths

        folder = QFileDialog.getExistingDirectory(self, tr("Cartella per il prospetto"), self._last_dir())
        if not folder:
            return
        ortho, relief = elevation_paths(self.chunk, entry)
        stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in entry["name"])
        for src, suffix in ((ortho, "ortofoto"), (relief, "rilievo")):
            if src.exists():
                shutil.copy2(src, Path(folder) / f"{stem}_{suffix}.tif")
        frame = ortho.parent / "frame.json"
        if frame.exists():
            shutil.copy2(frame, Path(folder) / f"{stem}_piano.json")
        self._info("Prospetto", tr("Ortofoto, rilievo e piano di «{0}» in {1}.", entry['name'], folder))

    def delete_elevation(self, entry: dict) -> None:
        from ..engine.elevation import remove_elevation

        if choice(self, "question", "Elimina prospetto", tr("Eliminare «{0}» e i suoi file?", entry['name']),
                  ["Elimina"]) != 0:
            return
        remove_elevation(self.chunk, entry["id"])
        self.workspace.populate(self.doc)
        self._dirty(True)

    # ---------------------------------------------------------------- masks
    def set_mask_mode(self, mode: str | None) -> None:
        """Draw masks on the photo view (``mask`` / ``unmask``) or go back to the markers."""
        self.photo_view.set_mask_mode(mode)
        self.mask_actions[str(mode)].setChecked(True)
        if mode is not None:
            self.show_view(self.photo_view)
            self.statusBar().showMessage(tr("Clic sui vertici dell'area, doppio clic (o Invio) chiude, Esc annulla"),
                                         10000)

    def _show_masks(self, on: bool) -> None:
        self.photo_view.show_masks = bool(on)
        self.photo_view.draw_mask()

    def _mask_targets(self):
        cams = self.photos.selected_cameras()
        return cams or list(self.chunk.cameras)

    def _masks_done(self, what: str):
        def done(n):
            self.photo_view.draw_mask()
            self._info("Maschere", tr("{0} maschere {1}.", n, what))
            self._dirty(True)
        return done

    def masks_import(self, alpha: bool = False) -> None:
        if not self._need_chunk():
            return
        from ..engine import masks

        folder = None
        if not alpha:
            folder = QFileDialog.getExistingDirectory(self, tr("Cartella delle maschere"), self._last_dir())
            if not folder:
                return
        self.run_task("Importazione delle maschere", lambda p: masks.import_masks(self.chunk, folder, progress=p),
                      self._masks_done(tr("importate") if folder else tr("dal canale alfa")), refresh=False)

    def masks_export(self) -> None:
        if not self._need_chunk():
            return
        from ..engine import masks

        folder = QFileDialog.getExistingDirectory(self, tr("Cartella per le maschere"), self._last_dir())
        if folder:
            n = masks.export_masks(self.chunk, folder)
            self._info("Maschere", tr("{0} maschere scritte in {1} (nome della foto + _mask.png).", n, folder))

    def masks_from_background(self) -> None:
        if not self._need_chunk():
            return
        from ..engine import masks

        v = ParamDialog.ask(self, "Maschere dallo sfondo", [
            Field("folder", "Cartella delle foto dello sfondo", "", kind="dir"),
            Field("tolerance", "Tolleranza (livelli di colore)", 12.0, minimum=1.0, maximum=120.0, decimals=0)],
            intro="Foto dello sfondo senza l'oggetto, dalle stesse posizioni (con lo stesso nome delle foto, o una "
                  "sola foto per tutte se la fotocamera e' fissa): l'oggetto e' cio' che cambia.")
        if not v or not v["folder"]:
            return
        cams = self._mask_targets()
        self.run_task("Maschere dallo sfondo", lambda p: masks.masks_from_background(
            self.chunk, v["folder"], v["tolerance"], cams=cams, progress=p), self._masks_done(tr("dallo sfondo")),
            refresh=False)

    def masks_from_colour(self) -> None:
        if not self._need_chunk():
            return
        from ..engine import masks

        v = ParamDialog.ask(self, "Maschere da un colore", [
            Field("colour", "Colore del fondale", (40, 160, 60), kind="color"),
            Field("tolerance", "Tolleranza (distanza RGB)", 40.0, minimum=5.0, maximum=200.0, decimals=0)],
            intro="Ogni pixel vicino al colore del fondale (un telo verde o blu) viene mascherato.")
        if not v:
            return
        cams = self._mask_targets()
        self.run_task("Maschere da un colore", lambda p: masks.masks_from_colour(
            self.chunk, v["colour"], v["tolerance"], cams=cams, progress=p), self._masks_done(tr("dal colore")),
            refresh=False)

    def masks_from_model(self) -> None:
        if not self._need_chunk(aligned=True, product="model"):
            return
        from ..engine import masks

        cams = [c for c in self._mask_targets() if c.aligned]
        self.run_task("Maschere dal modello", lambda p: masks.masks_from_model(self.chunk, cams=cams, progress=p),
                      self._masks_done(tr("dal modello")), refresh=False)

    def masks_with_sam(self) -> None:
        if not self._need_chunk():
            return
        if not self._sam_available(then=self.masks_with_sam):
            return
        from ..engine import masks

        cams = self._mask_targets()
        segmenter = self._segmenter()
        self.run_task("Maschere con SAM 2.1", lambda p: masks.masks_with_sam(self.chunk, segmenter, cams=cams,
                                                                            progress=p),
                      self._masks_done(tr("con SAM 2.1")), refresh=False)

    def masks_remove(self, selected: bool = True) -> None:
        if not self._need_chunk():
            return
        from ..engine import masks

        n = masks.remove_masks(self.chunk, self.photos.selected_cameras() if selected else None)
        self.photo_view.draw_mask()
        self.statusBar().showMessage(tr("{0} maschere rimosse", n), 8000)
        if n:
            self._dirty(True)

    def calibrate_colours(self) -> None:
        """*Strumenti > Calibra i colori delle foto*: gains and vignetting, then a summary."""
        if not self._need_chunk(aligned=True):
            return
        if not (self.chunk.product_path("point_cloud") or self.chunk.tie_points is not None):
            self._warn("Serve la nuvola densa (o almeno i punti di legame).")
            return

        def done(info):
            gains = [sum(g) / 3 for g in info["gains"].values()]
            text = tr("Correzione misurata su {0} osservazioni di punti visti da piu' foto.\n\nDifferenza di colore "
                      "tra le foto: {1}% prima, {2}% dopo.\nEsposizione delle foto: da {3} a {4} volte la media.",
                      _it(info['observations']), _it(info['rms_before'] * 100, 1), _it(info['rms_after'] * 100, 1),
                      _it(min(gains), 2), _it(max(gains), 2))
            import math

            corner = [100 * (1 - math.exp(sum(b))) for b in info["vignetting"].values()]  # at the corners (r = 1)
            if any(abs(c) >= 1 for c in corner):
                text += tr("\nVignettatura agli angoli: ") + ", ".join(
                    tr("{0:.0f}% piu' {1}", abs(c), tr('scuri') if c > 0 else tr('chiari')) for c in corner)
            self._info("Colori delle foto", text + tr("\n\nOrtofoto e texture costruite da ora in poi la usano."))

        self.run_task("Calibrazione dei colori", lambda p: self.chunk.calibrate_colours(progress=p), done,
                      refresh=False)

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
            self._activate_chunk(chunk)
            return
        if kind == "camera":
            self.open_photo(payload)
        elif kind == "product":
            if payload in ("slope", "aspect"):
                self.show_terrain_raster(payload)
            elif payload in RELIEF_KINDS:
                self.show_relief(payload)
            elif payload == "dem_precision":
                self.show_precision_map()
            elif payload == "coverage":
                self.show_coverage_map()
            elif payload in ("dem", "orthomosaic"):
                self._show_raster("dem" if payload == "dem" else "ortho")
            else:
                self._show_model(payload)
        elif kind == "version":
            self.show_version(*payload)
        elif kind == "elevation":
            self.show_elevation(payload)
        elif kind == "marker":
            cams = [chunk.camera(k) for k in payload.projections]
            if cams:
                self.open_photo(cams[0])
        elif kind == "crs":
            self.set_crs()

    def set_crs(self) -> None:
        crs, ok = QInputDialog.getText(self, tr("Sistema di riferimento"),
                                       tr("EPSG del chunk (es. EPSG:3004) o LOCAL:"),
                                       text=self.chunk.crs)
        if ok and crs:
            self.chunk.crs = crs.strip()
            self._dirty(True)
            self.refresh_all()

    def open_photo(self, camera, focus=None) -> None:
        """Open a photo; with a filter by marker or point, centred on it at full resolution."""
        focus = self.photos.focus if focus is None else focus
        self.photo_view.show_camera(self.chunk, camera, focus=focus)
        self.show_view(self.photo_view)
        self.photo_view.setFocus()
        self.photos.select_camera(camera)
        flags = sum(1 for m in self.chunk.markers if camera.key in m.projections)
        self.statusBar().showMessage(
            f"{camera.label}: " + (tr("bandierina blu = posizione stimata da confermare: trascinala sul punto esatto "
                                      "(o doppio "
                                      "clic se e' gia' giusta), diventa verde; ") if flags else "")
            + tr("tasto destro per posizionare un marker; Pagina giu'/su: foto successiva/precedente"), 20000)
        if self.a_residuals.isChecked():
            self._draw_residuals()

    def photo_step(self, step: int) -> None:
        """The next (+1) or previous (-1) photo of the list shown (the filtered one), as Metashape's
        Page Up/Down in the photo view."""
        cams = self.photos.visible_cameras()
        if not cams:
            return
        cur = self.photo_view.camera
        i = next((j for j, c in enumerate(cams) if c is cur), None)
        self.open_photo(cams[(i + step) % len(cams)] if i is not None else cams[0])

    def filter_photos_by_markers(self, keys) -> None:
        """Only the photos where these markers are (measured, estimated or in the frame where
        the survey puts them), as Metashape's *Filter Photos by Markers*; opening one centres it
        on the first marker."""
        from ..markers import gcp

        ch = self.chunk
        if ch is None or not keys:
            return
        markers = [ch.marker_by_key(k) for k in keys]
        cams = set()
        for m in markers:
            cams |= set(m.projections)
            if m.position is not None:
                cams |= {c.key for c, *_ in gcp.photos_seeing(ch, m.position)}
        names = ", ".join(m.label for m in markers)
        n = self.photos.set_filter(cams, tr("Foto di {0}", names), focus=markers[0])
        self._show_pane(self.bottom_tabs, self.photos_panel)
        self.statusBar().showMessage(
            tr("{0} foto con {1}: doppio clic per aprirle centrate sul marker; bandierina blu = da confermare "
               "(trascinala sul punto esatto o doppio clic se e' giusta); Pagina giu'/su per passare alle altre",
               n, names), 20000)

    def filter_photos_with_markers(self) -> None:
        ch = self.chunk
        if ch is None:
            return
        n = self.photos.set_filter({k for m in ch.markers for k in m.projections}, "Foto con marker")
        self.statusBar().showMessage(tr("{0} foto con almeno un marker", n), 8000)

    def filter_photos_by_point(self, point, title: str | None = None) -> None:
        """Only the photos that see a point (internal frame), as Metashape's *Filter Photos by
        Point*; opening one centres it on the point."""
        from ..markers import gcp

        ch = self.chunk
        if ch is None or point is None:
            self.statusBar().showMessage(tr("Nessun punto del modello, della nuvola o del DEM qui"), 8000)
            return
        point = np.asarray(point, float)
        cams = {c.key for c, *_ in gcp.photos_seeing(ch, point)}
        x, y, z = ch.to_world(point[None])[0]
        n = self.photos.set_filter(cams, title or tr("Foto del punto X {0:.2f} Y {1:.2f} Z {2:.2f}", x, y, z),
                                   focus=point)
        self._show_pane(self.bottom_tabs, self.photos_panel)
        self.statusBar().showMessage(tr("{0} foto vedono il punto: doppio clic per aprirle centrate su di esso "
                                        "(croce rossa)", n), 15000)

    def _photos_in_selected_area(self) -> None:
        from ..engine.patches import photos_for_area

        geom = self._selected_polygon()
        ch = self.chunk
        if geom is None or ch is None:
            self._warn("Seleziona un poligono (strumento Seleziona) per filtrare le foto che lo inquadrano.")
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            rows = photos_for_area(ch, geom, limit=100_000)
        finally:
            QApplication.restoreOverrideCursor()
        keys = {r["key"] for r in rows if r.get("coverage", 0) > 0}
        n = self.photos.set_filter(keys, "Foto dell'area selezionata")
        self._show_pane(self.bottom_tabs, self.photos_panel)
        self.statusBar().showMessage(tr("{0} foto inquadrano l'area", n), 8000)

    def _markers_edited(self, marker=None) -> None:
        """A marker was placed, moved, confirmed or removed in a photo: positions again and, for that
        marker, its blue flags on the other photos from where it is now (Metashape's guided
        placement)."""
        from ..markers import gcp

        self._dirty(True)
        ch = self.chunk
        try:
            ch.triangulate_markers()
            if marker is not None and any(m is marker for m in ch.markers) and \
                    any(p.measured for p in marker.projections.values()):
                gcp.project_marker(ch, marker)
        except Exception as exc:
            self.log_pane.warning(tr("Triangolazione dei marker non riuscita: {0}", exc))
        self.reference.set_chunk(ch)
        self.workspace.populate(self.doc)
        self._draw_markers_on_raster()
        self.photos.update_flags()
        if self.photo_view.camera is not None:
            self.photo_view.draw_markers()

    def _thumbs_progress(self, done: int, total: int) -> None:
        if total >= 30:  # a few photos load at once: no message
            self.statusBar().showMessage(tr("Miniature delle foto: {0}/{1}", done, total) if done < total
                                         else tr("Miniature delle foto pronte ({0})", total), 3000)

    def _show_coords(self, x: float, y: float, z) -> None:
        kind = getattr(self.ortho_view, "kind", None)
        if kind in RELIEF_KINDS:
            zt = ""
            if z is not None and np.isfinite(z):
                zt = decimal_comma(f"  SVF {z:.3f}" if kind == "svf" else f"  {z:.1f}°" if kind.startswith("openness")
                                   else f"  {z * 100:+.1f} cm" if kind in ("slrm", "msrm") else f"  {z:.2f}")
            self.coord_label.setText(f"X {x:.3f}  Y {y:.3f}{zt}   [{self.chunk.crs if self.chunk else ''}]")
            return
        if kind in ("slope", "aspect"):
            zt = ""
            if z is not None and np.isfinite(z):
                zt = decimal_comma(tr("  pendenza {0:.1f}°", z) if kind == "slope" else
                                   tr("  terreno piano") if z < 0 else tr("  esposizione {0:.0f}° ({1})", z, _compass(z)))
            self.coord_label.setText(f"X {x:.3f}  Y {y:.3f}{zt}   [{self.chunk.crs if self.chunk else ''}]")
            return
        if kind == "dem" and z is not None and self.chunk is not None and self.chunk.dem_path is not None:
            self.coord_label.setText(f"X {x:.3f}  Y {y:.3f}  Z {z:.3f}{self._terrain_at(x, y, z)}   [{self.chunk.crs}]")
            return
        if getattr(self.ortho_view, "kind", None) == "coverage":
            zt = tr("  {0} foto", int(z)) if z is not None and np.isfinite(z) else ""
            self.coord_label.setText(f"X {x:.3f}  Y {y:.3f}{zt}   [{self.chunk.crs if self.chunk else ''}]")
            return
        if getattr(self.ortho_view, "kind", None) == "sigma":
            zt = f"  sigma Z {z * 1000:.1f} mm" if z is not None else ""
            self.coord_label.setText(f"X {x:.3f}  Y {y:.3f}{zt}   [{self.chunk.crs if self.chunk else ''}]")
            return
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
    def propose_units(self) -> None:
        """Strumenti > Proponi US e rapporti: a US number, a kind and the measurements for every unit
        without a number (SAM, drawn) and the relations their contacts suggest
        (:mod:`archaeo.us_proposal`), in a window where they are checked before being applied."""
        from ..archaeo import us_proposal
        from ..archaeo.timeline import units_of
        from .us_proposal_dialog import UsProposalDialog

        if not self._need_chunk():
            return
        todo = [i for i, u in enumerate(self.units) if not u.us and (u.attributes or {}).get("gid") is None]
        if not todo:
            self._warn("Nessuna unita' senza numero di US: segmenta l'ortofoto con SAM (Tutta l'ortofoto) o disegna "
                       "le unita' prima.")
            return
        site = self.project_site()
        used = [u.us for u in self.units] + [u.us for ch in self.doc.chunks if ch is not self.chunk
                                             for u in units_of(ch)]
        first = max([int(n) for n in used if n and str(n).isdigit()], default=0) + 1
        if self.pa is not None and site:
            try:
                first = max(first, self.pa.next_us(site))
            except Exception as exc:  # the database does not answer: the project's numbers
                log.warning("Numeri di US di PyArchInit non letti: %s", exc)
        areas = [u.area for u in self.units if u.area]
        v = ParamDialog.ask(self, "Proponi US e rapporti", [
            Field("first", "Primo numero di US", int(first), minimum=1, maximum=10 ** 6),
            Field("area", "Area", max(set(areas), key=areas.count) if areas else "1"),
            Field("step", "Dislivello minimo per dire chi copre (cm)", 2.0, minimum=0.1, maximum=100.0, decimals=1),
            Field("cuts", "Proponi il taglio dei riempimenti", True)],
            intro=tr("{0} unita' senza numero di US nel chunk {1}. I numeri partono dopo l'ultima US del sito {2} e "
                     "del progetto.", len(todo), self.chunk.label, site or tr("(nessun sito scelto)")))
        if not v:
            return
        units, chunk, lang = [self.units[i] for i in todo], self.chunk, i18n.current()

        def job(p):
            return us_proposal.propose_for_chunk(chunk, units, v["first"], area=v["area"].strip() or None,
                                                 sito=site or None, step=v["step"] / 100.0, cuts=v["cuts"], lang=lang,
                                                 progress=p)

        def done(prop):
            for it in prop["units"]:
                it["index"] = todo[it["source"]] if it["source"] is not None else None
            self._proposal = prop
            if self._proposal_win is not None:
                self._proposal_win.close()
            self._proposal_win = UsProposalDialog(prop, can_write=self.pa is not None and bool(site), parent=self)
            self._proposal_win.apply_requested.connect(self._apply_proposal)
            self._proposal_win.unit_chosen.connect(self._show_proposed_unit)
            self._proposal_win.show()
            self._proposal_win.raise_()

        self.run_task("Proposta di US", job, done, refresh=False, save=False)

    def _show_proposed_unit(self, key: int) -> None:
        it = self._proposal["units"][key]
        if it["kind"] == "cut":
            it = self._proposal["units"][it["fill"]]
        rect = QRectF()
        for lay in self.ortho_view.layers.values():
            for item, _label, attrs in lay["features"]:
                if attrs.get("_unit") == it["index"]:
                    rect = rect.united(item.sceneBoundingRect())
        self.show_view(self.ortho_view)
        self.ortho_view.zoom_to(rect)

    def _apply_proposal(self, keys, rels, write: bool) -> None:
        """The units and relations ticked in the proposal: numbers and cuts in the project, and with
        ``write`` the US forms, the relations and the polygons in PyArchInit."""
        from ..archaeo import us_proposal

        prop, chosen = self._proposal, set(keys)
        for it in prop["units"]:
            it["use"] = it["key"] in chosen
            if not it["use"]:
                continue
            new = it["unit"]
            if it["kind"] == "unit":
                u = self.units[it["index"]]
                u.us, u.tipo, u.area, u.sito = new.us, new.tipo, new.area, new.sito
            else:  # a cut: a new unit with the outline of its fill, in the same layer
                fill = self.units[prop["units"][it["fill"]]["index"]]
                new.attributes = {"_layer": (fill.attributes or {}).get("_layer"), "stratigraph_index_us": 2}
                self.units.append(new)
                it["index"] = len(self.units) - 1
        for i, rel in enumerate(prop["relations"]):
            rel["use"] = i in set(rels)
        self._redraw_overlays()
        self._dirty(True)
        if self._proposal_win is not None:
            self._proposal_win.close()
        n = len(chosen)
        if not write:
            self.statusBar().showMessage(tr("{0} unita' numerate nel progetto", n), 10000)
            return
        pa, site, crs = self.pa, self.project_site(), self.chunk.crs
        units = [self.units[it["index"]] for it in prop["units"] if it["use"]]
        units = [u for u in units if (u.attributes or {}).get("gid") is None]

        def job(p):
            p(0.1, "Schede US")
            out = us_proposal.write_proposal(pa, prop, site)
            p(0.7, "Unita' stratigrafiche")
            return out, pa.publish_units(units, site, crs=crs) if units else []

        def done(r):
            out, gids = r
            for u, g in zip(units, gids):
                u.attributes["gid"] = g
            self._redraw_overlays()
            self._dirty(True)
            self.pa_pane.refresh()
            text = tr("Sito {0}: {1} schede US create, {2} rapporti aggiunti, {3} poligoni in pyunitastratigrafiche.",
                      site, len(out["created"]), out["relations"], len(gids))
            if out["existing"]:
                text += "\n" + tr("Schede gia' presenti, lasciate come erano: {0}", ", ".join(out["existing"]))
            self._info("PyArchInit", text)

        self.run_task("Proposta di US in PyArchInit", job, done, refresh=False, save=False)

    def excavation_phases(self) -> None:
        """Strumenti > Fasi dello scavo (:mod:`archaeo.timeline`), in a window beside this one."""
        from ..archaeo import timeline
        from .timeline_dialog import TimelineDialog

        if self._timeline is None:
            self._timeline = TimelineDialog(self)
            self._timeline.phase_chosen.connect(self._show_phase)
            self._timeline.date_changed.connect(self._phase_date_changed)
            self._timeline.recompute_requested.connect(self.excavation_phases)
            self._timeline.write_requested.connect(self._write_phase_relations)
        self._store_drawings()  # the units of the active phase, also those not saved yet
        doc = self.doc

        def done(report):
            self._timeline.set_report(report, can_write=self.pa is not None)
            self._timeline.show()
            self._timeline.raise_()

        self.run_task("Fasi dello scavo", lambda p: timeline.timeline(doc, progress=p), done, refresh=False,
                      save=False)

    def _show_phase(self, key: int) -> None:
        chunk = next((c for c in self.doc.chunks if c.key == key), None)
        if chunk is not None:
            self._activate_chunk(chunk)
            self.show_view(self.ortho_view)

    def _phase_date_changed(self, key: int, text: str) -> None:
        from ..archaeo import timeline

        chunk = next((c for c in self.doc.chunks if c.key == key), None)
        if chunk is None:
            return
        timeline.set_phase_date(chunk, text or None)
        self._dirty(True)
        self.statusBar().showMessage(tr("Data di {0}: {1}. Ricalcola per riordinare le fasi", chunk.label,
                                        text or tr("dalle foto")), 10000)

    def _write_phase_relations(self, relations) -> None:
        from ..archaeo import timeline

        if self.pa is None:
            self._warn("Connetti prima PyArchInit (menu PyArchInit > Connetti).")
            return
        if choice(self, "question", "Rapporti stratigrafici",
                  tr("Aggiungere {0} rapporti alle schede US di PyArchInit, in entrambe le direzioni? Quelli gia' "
                     "presenti non si ripetono.", len(relations)), ["Aggiungi"]) != 0:
            return
        pa, site = self.pa, self.project_site()

        def done(out):
            text = tr("Rapporti aggiunti alle schede US: {0}", out["added"])
            if out["missing"]:
                text += tr("; senza scheda US (crea la scheda e ripubblica): {0}", ", ".join(out["missing"]))
            self._info("PyArchInit", text)

        self.run_task("Rapporti nelle schede US", lambda p: timeline.write_relations(pa, relations, site=site), done,
                      refresh=False, save=False)

    def set_language(self, code: str) -> None:
        """The language of the interface from the next start; a restart now applies it."""
        i18n.save_language(code)
        if code == i18n.current():
            return
        pick = choice(self, "question", "Lingua / Language", tr("La lingua cambia al prossimo avvio del programma."),
                      [tr("Riavvia ora")], cancel=tr("Piu' tardi"))
        if pick == 0:
            self._relaunch_on_close = True
            if not self.close():  # the project was not saved and the user stayed
                self._relaunch_on_close = False

    def _relaunch(self) -> None:
        """A new copy of the program, with the project that was open (the language just chosen)."""
        env = {k: v for k, v in os.environ.items() if k != "PPG_LANG"}
        project = [str(self.doc.path)] if self.doc.path else []
        bundle = updates.app_bundle() if sys.platform == "darwin" and getattr(sys, "frozen", False) else None
        if bundle is not None:
            cmd = ["/usr/bin/open", "-n", str(bundle)] + (["--args"] + project if project else [])
        elif getattr(sys, "frozen", False):
            cmd = [sys.executable] + project
        else:
            cmd = [sys.executable, "-m", "pyarchinit_photogrammetry.gui"] + project
        try:  # close_fds=False: posix_spawn, never a fork of this process (see engine.isolate)
            subprocess.Popen(cmd, close_fds=False, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            log.error("Riavvio non riuscito: %s", exc)

    def set_theme(self, mode: str) -> None:
        theme.apply(QApplication.instance(), mode)
        self.settings.setValue("theme", mode)
        for k, a in self.theme_actions.items():
            a.setChecked(k == mode)

    def open_docs(self) -> None:
        QDesktopServices.openUrl(QUrl(DOCS_URL))


    def donate(self) -> None:
        """*Aiuto > Sostieni il progetto*: the PayPal page of the project."""
        QDesktopServices.openUrl(QUrl(DONATE_URL))
    def open_log_folder(self) -> None:
        from .logging_setup import log_dir

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir())))

    def show_doctor(self) -> DoctorDialog:
        dlg = DoctorDialog(self)
        dlg.exec()
        return dlg

    # ---------------------------------------------------------------- updates
    def _auto_check_updates(self) -> None:
        """The automatic check: at most once a day, when enabled in *Aiuto*."""
        import datetime

        if not self.a_update_auto.isChecked():
            return
        today = datetime.date.today().isoformat()
        if self.settings.value("updates_last_check", "") == today:
            return
        self.settings.setValue("updates_last_check", today)
        self.update_checker.start(manual=False, skip=self.settings.value("updates_skip", "") or None)

    def check_updates(self) -> None:
        """*Aiuto > Controlla aggiornamenti*: also the versions skipped before."""
        started = self.update_checker.start(manual=True)
        self.statusBar().showMessage(tr("Controllo degli aggiornamenti...") if started
                                     else tr("Controllo degli aggiornamenti gia' in corso"), 5000)

    def _updates_checked(self, release, error, manual: bool) -> None:
        if error is not None:
            log.info("Controllo degli aggiornamenti: %s", error)
            if manual:
                self._warn(f"{error_text(error)}\n\n({error})")
            return
        if release is None:
            if manual:
                self._info("Aggiornamenti", tr("Questa e' l'ultima versione ({0}).", __version__))
            return
        self.log_pane.info(tr("E' disponibile la versione {0} (questa e' la {1})", release.version, __version__))
        dlg = UpdateDialog(release, parent=self)
        dlg.exec()
        if dlg.action == "skip":
            self.settings.setValue("updates_skip", release.version)
            self.log_pane.info(tr("La versione {0} non verra' piu' proposta all'avvio", release.version))
        elif dlg.action == "page":
            QDesktopServices.openUrl(QUrl(release.url))
        elif dlg.action == "copy" and dlg.command:
            self.statusBar().showMessage(tr("Comando copiato: {0}", dlg.command), 10000)
        elif dlg.action in ("install", "download"):
            self.download_update(release, install=dlg.action == "install")

    def download_update(self, release, install: bool = False) -> bool:
        """Download the installer of ``release`` for this system; with ``install`` (macOS
        application) also copy the new application, swapped in when the window closes."""
        asset = updates.asset_for_platform(release)
        if asset is None:
            self._warn(tr("La versione {0} non ha un pacchetto per questo sistema: scaricalo dalla pagina della "
                          "versione.", release.version))
            QDesktopServices.openUrl(QUrl(release.url))
            return False

        def work(p):
            try:
                path = updates.download(asset, progress=p.sub(0.0, 0.85 if install else 1.0))
            except updates.UpdateError as exc:
                raise RuntimeError(f"{error_text(exc)} ({exc})") from exc
            if not install:
                return path, None, None
            try:
                new = updates.prepare_macos_update(path, expected_version=release.version, progress=p.sub(0.85, 0.15))
            except updates.UpdateError as exc:
                return path, None, exc
            return path, new, None

        def done(result):
            path, new, problem = result
            if new is None:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if problem is not None else path.parent)))
                if problem is not None:  # e.g. an application folder this user cannot write
                    self._warn(tr("La versione {0} e' stata scaricata ma non installata: {1}.\n\nSi apre il disco: "
                                  "trascina l'applicazione nella cartella Applicazioni.", release.version, problem))
                else:
                    self._info("Aggiornamento scaricato",
                               tr("{0} e' nella cartella {1}.\n\nChiudi il programma, estrai lo zip e sostituisci la "
                                  "cartella del programma con quella nuova.", path.name, path.parent))
                return
            self._pending_update = new
            pick = choice(self, "question", "Aggiornamento pronto",
                          tr("La versione {0} e' pronta. Il programma si chiude (chiedendo di salvare il progetto se "
                             "e' cambiato) e si riapre aggiornato.", release.version),
                          ["Riavvia ora", "Alla chiusura del programma"], cancel=None)
            if pick == 0:
                self.close()
            else:
                self.statusBar().showMessage(tr("La versione {0} verra' installata alla chiusura", release.version),
                                             10000)

        return self.runner.run("Download dell'aggiornamento", work, done, self._error)

    def _install_pending_update(self) -> None:
        try:
            updates.finish_macos_update(self._pending_update)
            log.info("Aggiornamento: installazione di %s alla chiusura", self._pending_update)
        except Exception as exc:  # never block the exit
            log.error("Aggiornamento non installato: %s", exc)
        self._pending_update = None

    def about(self) -> None:
        message(self, "info", "Informazioni",
                tr("<b>{0} {1}</b><br>Fotogrammetria open source per l'archeologia e il BIM, integrata con "
                   "PyArchInit.<br><br>Visualizzatore 3D: {2}<br>Licenza GNU GPL v2 o successiva<br>Autore: Enzo "
                   "Cocca<br><a href='{3}'>{4}</a>",
                   APP_TITLE, __version__, self.model_view.backend, DOCS_URL, DOCS_URL))

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
        if mode == "patch" and self.ortho_view.kind != "ortho":
            if self.chunk is None or self.chunk.orthomosaic_path is None:
                self.draw_actions["pan"].setChecked(True)
                self._warn("Le patch si disegnano sull'ortofoto: costruiscila prima.")
                return
            self._show_raster("ortho")
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
                 "area": "Trascina un riquadro, oppure clic per i vertici e doppio clic per chiudere: area in pianta "
                         "e superficie 3D (dal DEM e dal modello), pendenza media, quote",
                 "quota": "Clic su un punto: la quota viene letta dal DEM, poi area, US e gli altri campi di "
                          "pyarchinit_quote",
                 "marker": "Clic su un punto: un marker con la quota del DEM, proiettato sulle foto che lo vedono "
                           "(come Aggiungi marker in Metashape); con due marker si fa una barra di scala",
                 "crop": "Trascina un riquadro, oppure clic per i vertici e doppio clic per chiudere il poligono: poi "
                         "scegli i prodotti da esportare ritagliati o da ritagliare nel progetto",
                 "patch": "Trascina un riquadro, oppure clic per i vertici e doppio clic per chiudere: poi scegli da "
                          "quali foto prendere i colori dell'area (alberi, veicoli, persone, cuciture)",
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
        stones = {k: [g.wkt for g in v] for k, v in self.stones.items() if v}
        if stones:
            ch.meta["stones"] = stones
        else:
            ch.meta.pop("stones", None)

    def _activate_chunk(self, chunk) -> None:
        """Another chunk becomes the active one: the drawings of the one left stay with it (the
        units exposed in that phase of the excavation), those of the new one come back."""
        if chunk is self.chunk:
            return
        self._store_drawings()
        self._sam_cancel()
        self.doc.chunk = chunk
        self._reload_drawings(redraw=False)
        self.refresh_all(reset_views=True)
        if self.history is not None:
            self.history.reset()  # the history belongs to a chunk

    def _reload_drawings(self, redraw: bool = True) -> None:
        """The drawings of the active chunk read again from the project; the units loaded from
        PyArchInit (of the site, not of a chunk) stay."""
        from_pa = [u for u in self.units
                   if str((u.attributes or {}).get("_layer") or "").startswith("US da PyArchInit - ")]
        self._restore_drawings()
        self.units += from_pa
        if redraw:
            self._redraw_overlays()

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
        from shapely import wkt

        self.stones = {}
        for key, rows in ((ch.meta.get("stones") or {}) if ch is not None else {}).items():
            try:
                self.stones[key] = [wkt.loads(r) for r in rows]
            except Exception as exc:  # a broken record must not stop the project from opening
                log.warning("Pietre non lette dal progetto: %s", exc)

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
                           + (tr(" (sito {0})", u.sito) if u.sito else ""))

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
                self.statusBar().showMessage(tr("Fuori dal DEM: quota non letta"), 8000)
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
                self.statusBar().showMessage(tr("Quote: valori fissati per i prossimi punti (Esc o un altro "
                                                "strumento per tornare a chiederli)"), 15000)
            if v.get("disegnatore"):
                self.settings.setValue("pa/disegnatore", v["disegnatore"])
        q = {"x": float(x), "y": float(y), "sito_q": self.project_site(), **v}
        from ..engine.precision import precision_at

        sigma = precision_at(self.chunk, x, y) if self.chunk is not None else None
        if sigma is not None:  # PyArchInit has no column for it: in the note of the survey too
            q["precisione_q"] = round(sigma, 4)
            if str(q.get("rilievo_originale") or "Fotogrammetria").strip() == "Fotogrammetria":
                q["rilievo_originale"] = f"Fotogrammetria (±{sigma * 1000:.1f} mm)".replace(".", ",")
        self._add_quota(q)

    def _section_in_3d(self, a, b) -> None:
        """A section traced by two points picked in the 3D view (its plan trace)."""
        w = self.chunk.to_world(np.array([a, b], float))
        if np.hypot(*(w[1, :2] - w[0, :2])) < 1e-6:
            self.statusBar().showMessage(tr("I due punti sono uno sopra l'altro: nessuna traccia in pianta"), 8000)
            return
        self._section_drawn((float(w[0, 0]), float(w[0, 1])), (float(w[1, 0]), float(w[1, 1])))

    def _slice_in_3d(self, point) -> None:
        self.plan_slice(z_default=float(self.chunk.to_world(np.asarray(point, float)[None])[0, 2]))

    def move_marker_in_3d(self, key: int) -> None:
        """Riferimenti > Sposta nella vista 3D: the next click on the model or the cloud is the
        new place of the marker (one placed in 3D or on the orthophoto, not measured in photos)."""
        ch = self.chunk
        if ch is None:
            return
        m = ch.marker_by_key(key)
        if any(p.measured for p in m.projections.values()):
            self._warn(tr("{0} e' misurato nelle foto: la sua posizione viene da li'. Spostalo trascinando le "
                          "bandierine verdi nelle foto, oppure togli le misure.", m.label))
            return
        kind = self.model_view.kind if self.model_view.kind in self.model_view.PICK_KINDS else None
        self._show_model(kind or ("model" if ch.product_path("model") else "point_cloud"))
        self.model_view.start_pick(f"move:{key}")

    def _marker_moved_in_3d(self, key: int, point) -> None:
        from ..markers import gcp

        if point is None:
            self.statusBar().showMessage(tr("Nessun punto del modello o della nuvola sotto il cursore"), 8000)
            return
        m = self.chunk.marker_by_key(key)
        m.position = np.asarray(point, float)
        gcp.project_marker(self.chunk, m)
        self._markers_edited(m)
        x, y, z = self.chunk.to_world(m.position[None])[0]
        self.log_pane.info(tr("{0} spostato in X {1:.3f} Y {2:.3f} Z {3:.3f}", m.label, x, y, z))

    def _marker_picked_in_3d(self, point) -> None:
        if point is None:
            self.statusBar().showMessage(tr("Nessun punto del modello o della nuvola sotto il cursore"), 8000)
            return
        if self.chunk is None or not self.chunk.aligned_cameras:
            self._warn("Servono le foto allineate: il marker si proietta sulle foto che lo vedono.")
            return
        kind = self.model_view.kind or ""
        self.add_marker_at(point, "sul modello" if kind.startswith("model") else
                           "sulla nuvola densa" if kind == "point_cloud" else "sui punti di legame")

    def _raster_point(self, x: float, y: float):
        """The point of the survey (internal frame) under world x, y of the orthophoto or the DEM:
        the height from the DEM at full resolution, else from the surface (dense cloud or tie
        points) along the vertical; None where there is neither."""
        from ..core import geometry
        from ..markers import gcp

        ch = self.chunk
        if ch is None:
            return None
        z = self._dem_value(x, y)
        if z is not None:
            return ch.to_internal(np.array([[x, y, z]], float))[0]
        surface = gcp._SurfacePoints(ch)
        xyz = surface.xyz()
        if xyz is None:
            return None
        top = ch.to_internal(np.array([[x, y, 1e4], [x, y, 0.0]], float))
        d = (top[1] - top[0]) / np.linalg.norm(top[1] - top[0])
        return geometry.point_on_ray(xyz, top[0], d, 0.005 * surface.diag())

    def _marker_clicked_on_raster(self, x: float, y: float) -> None:
        """A marker where the orthophoto or the DEM was clicked (see :meth:`_raster_point`)."""
        ch = self.chunk
        if ch is None or not ch.aligned_cameras:
            self._warn("Servono le foto allineate: il marker si proietta sulle foto che lo vedono.")
            return
        point = self._raster_point(x, y)
        if point is None:
            self.statusBar().showMessage(tr("Qui non c'e' superficie (fuori dal DEM e dalla nuvola)"), 8000)
            return
        self.add_marker_at(point, "sull'ortofoto" if self.ortho_view.kind == "ortho" else "sul DEM")

    def _add_quota(self, q: dict) -> None:
        q.pop("keep", None)
        self.quote.append(q)
        self._draw_quota(q, len(self.quote) - 1)
        self._dirty(True)
        self.log_pane.info(tr("Quota {0} {1}", q.get('quota_q'), q.get('unita_misu_q', ''))
                           + (f" ±{q['precisione_q'] * 1000:.1f} mm" if q.get("precisione_q") else "")
                           + tr(" - US {0} (area {1}) in X {2:.3f} Y {3:.3f}",
                                q.get('us_q') or '?', q.get('area_q') or '?', q['x'], q['y']))

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
        self.statusBar().showMessage(tr("Opzioni delle sezioni salvate: traccia la sezione sull'ortofoto"), 8000)

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
            self.log_pane.info(tr("{0}: lunghezza {1:.3f} m, disegni in {2}", label, sec.length, out_dir))

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
        site, ok = QInputDialog.getItem(self, tr("Sito"), tr("Sito PyArchInit:"), sites, 0, True)
        if not ok or not site:
            return
        sec = Section(np.array(sdict["p1"] + [0.0]), np.array(sdict["p2"] + [0.0]), label=sdict["label"])
        self.run_task("Pubblicazione sezione",
                      lambda p: self.pa.publish_section_trace(sec, site, sdict["label"], crs=self.chunk.crs),
                      lambda gid: self._info("PyArchInit", tr("Traccia '{0}' scritta in pyarchinit_sezioni (gid {1})",
                                                              sdict['label'], gid)),
                      refresh=False, save=False)

    def _delete_section(self, sdict: dict, confirm: bool = True, redraw: bool = True) -> None:
        if confirm:
            r = message(self, "question", "Elimina sezione", tr("Eliminare {0} e i suoi file?", sdict['label']),
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
        self.b_pa_save.setText(tr("Salva modifiche in PyArchInit ({0})", n))
        self.b_pa_save.setVisible(n > 0)

    def save_pending_units(self, confirm: bool = True) -> None:
        """Write to ``pyunitastratigrafiche`` the changes made here to polygons loaded from
        PyArchInit: attributes, geometries (merges) and deletions."""
        if not (self.pa_pending or self.pa_deleted) or not self._need_pa():
            return
        pending, deleted, geom = dict(self.pa_pending), set(self.pa_deleted), set(self.pa_geom_changed)
        if deleted and confirm:
            r = message(self, "question", "Salva modifiche in PyArchInit",
                        tr("Eliminare {0} poligoni da pyunitastratigrafiche e aggiornarne {1}? Le schede US non "
                           "vengono toccate.", len(deleted), len(set(pending) - deleted)),
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
            self._info("PyArchInit", tr("pyunitastratigrafiche: {0} poligoni aggiornati", n_upd)
                       + (tr(", {0} eliminati", n_del) if n_del else ""))

        self.run_task("Aggiornamento unita' in PyArchInit", job, done, refresh=False, save=False)

    # ---------------------------------------------------------------- selection: delete and merge
    def _selection_changed(self, n: int) -> None:
        self.a_sel_delete.setEnabled(n > 0)
        self.a_sel_merge.setEnabled(n > 1)
        if self.ortho_view.mode == "select":
            self.statusBar().showMessage(tr("{0} elementi selezionati: Canc per eliminare, Unisci per fonderli", n)
                                         if n else tr("Nessun elemento selezionato"), 8000)

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
        sections, lines, skip, quote_gone, patch_ids, stones_gone = [], {}, set(), set(), set(), set()
        for layer, idx, attrs in sel:
            if layer == PATCH_LAYER:
                patch_ids.add(attrs.get("patch"))
            elif layer == STONES_LAYER:
                stones_gone.add(int(attrs["_stone"]))
            elif "_unit" in attrs:
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
            text = tr("Eliminare {0} elementi selezionati?", len(sel))
            if sections:
                text += tr("\n{0} tracce di sezione: anche i loro disegni (SVG/DXF).", len(sections))
            if from_pa:
                text += (tr("\n{0} poligoni vengono da PyArchInit: escono dal database solo con «Salva modifiche in "
                            "PyArchInit».", from_pa))
            if patch_ids:
                text += tr("\n{0} patch dell'ortofoto: l'ortofoto viene aggiornata senza di esse.", len(patch_ids))
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
        if stones_gone and self.ortho_view.path is not None:
            key = self.ortho_view.path.name
            self.stones[key] = [s for i, s in enumerate(self.stones.get(key, [])) if i not in stones_gone]
        for sdict in sections:
            self._delete_section(sdict, confirm=False, redraw=False)
        for layer, idxs in lines.items():
            kept = [ln for k, ln in enumerate(self.line_layers[layer]) if k not in idxs]
            if kept:
                self.line_layers[layer] = kept
            else:
                del self.line_layers[layer]
        if patch_ids and self.chunk is not None:
            from ..engine import patches as P

            for pid in patch_ids:
                try:
                    P.remove_patch(self.chunk, pid)
                except KeyError:
                    pass
        self._update_pa_save_button()
        self._redraw_overlays(skip=skip)
        self._dirty(True)
        self.log_pane.info(tr("Eliminati {0} elementi", len(sel)))
        if patch_ids:
            self.update_ortho_patches()
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
                        + (tr(" - da PyArchInit") if (self.units[i].attributes or {}).get("gid") is not None else ""))
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
        self.log_pane.info(tr("Unite {0} unita' in {1} {2}: area {3:.3f} m2",
                              len(order), target.tipo, target.us or '?', target.geometry.area)
                           + (tr(", {0} parti separate", len(polys)) if len(polys) > 1 else ""))
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
        self.log_pane.info(tr("{0}: unite {1} linee, lunghezza {2:.3f} m", layer, len(idxs), merged.length))
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
        modelled = set(self.line_layers) | {"Sezioni", QUOTE_LAYER, PATCH_LAYER, "Marker", STONES_LAYER}
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
        self._draw_patches()
        self._draw_markers_on_raster()
        self._draw_stones()
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
        self.log_pane.info(tr("Scheda {0} {1} salvata in PyArchInit ({2})",
                              dlg.tipo.currentText(), dlg.us.text(), sito))
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
        choices = ([("drawn", tr("Unita' disegnate ({0})", len(drawn)))] if drawn else []) + \
            [("pa:" + s, tr("Poligoni US da PyArchInit - {0}", s)) for s in sites]
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
            self.log_pane.info(tr("{0} proxy creati, volume {1:.3f} m3",
                                  len(entries), sum(e['volume_m3'] for e in entries)))

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
        path, _ = QFileDialog.getSaveFileName(self, tr("Proxy per Extended Matrix"), self._last_dir() + "/proxy_us.obj",
                                              "OBJ (*.obj)")
        if not path:
            return
        from ..archaeo.proxies import export_extended_matrix

        objs = [o[2] for o in self._proxy_objects()]
        allv = np.vstack([np.asarray(o.mesh.vertices) for o in objs])
        out = export_extended_matrix(objs, path, offset=np.floor(allv.min(0)))
        self._info("Proxy esportati", tr("{0} oggetti in {1}\nOffset delle coordinate in {2}.offset.txt",
                                         len(objs), out, out.name))

    def publish_proxies(self, rows: list) -> None:
        if not self._need_pa():
            return
        from ..archaeo.proxies import save_proxy
        from ..pyarchinit.media import register_media

        items = self._proxy_objects(set(rows))

        def job(p):
            done_ = []
            for k, (i, e, px) in enumerate(items):
                p(k / max(len(items), 1), tr("Pubblicazione {0}", px.name))
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
            self._info("PyArchInit", tr("{0} proxy pubblicati come modelli 3D", len(res))
                       + (tr("; senza scheda US (crea la scheda e ripubblica): {0}",
                             ', '.join(missing)) if missing else ""))

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
        self._info("PyArchInit", tr("Quote scritte in {0} schede US", n)
                   + (tr("; schede mancanti: {0}", ', '.join(missing)) if missing else ""))

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
        layers = {name: {"kind": lay["kind"], "features": view.features(name)} for name, lay in view.layers.items()
                  if name != "Marker"}  # the markers are the survey's control, not excavation drawings
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
            self._warn(tr("Non riesco a scrivere i disegni per QGIS in {0}: vedi il log.", self._qgis_dir()))
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
            self.log_pane.info(tr("Progetto QGIS: {0} ({1} layer vettoriali aggiornati dal vivo, {2} raster)",
                                  project, len(rep['vectors']), len(rep['rasters']))
                               + (tr("; non validi: {0}", ', '.join(bad)) if bad else ""))
            self.statusBar().showMessage(tr("QGIS aperto: i disegni si aggiornano in QGIS da soli, ogni secondo"),
                                         15000)

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
        self.statusBar().showMessage(tr("Coordinate dei target: {0}", csv), 15000)

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
            intro=tr("Quote del DEM da {0:.2f} a {1:.2f} m (dislivello {2:.2f} m): con {3:g} m sono circa {4} "
                     "livelli. Su un DSM da drone (vegetazione, zolle) una levigatura di 0,2-0,5 m toglie i piccoli "
                     "anelli e da' curve piu' pulite.", lo, hi, hi - lo, suggested, int((hi - lo) / suggested)))
        if not v:
            return
        levels = (hi - lo) / v["interval"]
        if levels > 300:
            r = message(self, "question", "Curve di livello",
                        tr("Con equidistanza {0:g} m il DEM (dislivello {1:.1f} m) da' {2} livelli di curve: decine "
                           "di migliaia di linee, lente da disegnare e da usare. Un'equidistanza di {3:g} m ne da' "
                           "circa {4}.\n\nContinuare con {5:g} m?",
                           v['interval'], hi - lo, int(levels), suggested, int((hi - lo) / suggested), v['interval']),
                        buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        default=QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return

        def job(p):
            p(0.1, "Curve di livello")
            lines = terrain.contours(dem, v["interval"], index_every=v["index_every"], smooth=v["smooth"])
            p(0.7, tr("Salvataggio di {0} curve", len(lines)))
            terrain.export_contours(lines, v["path"], crs=self.chunk.crs)
            return lines

        def done(lines):
            self._show_raster("dem")
            name = f"Curve di livello ({v['interval']:g} m)"
            self._draw_contours(name, lines)
            self.log_pane.info(tr("{0}: {1} linee, {2} vertici, salvate in {3}",
                                  name, len(lines), sum(len(ln['coords']) for ln in lines), v['path']))
            self.statusBar().showMessage(tr("{0} curve salvate in {1}", len(lines), v['path']), 10000)
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
            if v["reference"] == "min":
                what = tr("deposito sopra il fondo")
            elif v["reference"] == "plane":
                what = tr("rispetto al ") + tr("piano a ") + f"{_it(v['elevation'], 3)} m"
            else:
                what = tr("rispetto al ") + tr("DEM precedente")
            txt = (tr("Area {0:.3f} m2 ({1}): scavo (volume asportato) {2:.3f} m3, riporto {3:.3f} m3, netto {4:.3f} "
                      "m3; quote attuali {5:.3f} / {6:.3f} m",
                      r['area_m2'], what, r['cut_m3'], r['fill_m3'], r['net_m3'], r['quota_min'], r['quota_max']))
            self._info("Volume", txt)

        self.run_task("Volume", lambda p: terrain.volume(self.chunk.dem_path, ref, poly), done, refresh=False,
                      save=False)

    def plan_slice(self, z_default: float | None = None) -> None:
        if not self._need_chunk(product="model"):
            return
        if z_default is not None:
            z_default = round(float(z_default), 3)
        elif self.chunk.dem_path:
            from ..archaeo.terrain import dem_range

            try:
                lo, hi = dem_range(self.chunk.dem_path)[:2]
                z_default = round((lo + hi) / 2, 3)
            except Exception:
                z_default = 0.0
        else:
            z_default = 0.0
        choices, codes = self._cloud_class_choices()
        v = ParamDialog.ask(self, "Pianta a quota", [
            Field("z", "Quota (m)", float(z_default), minimum=-10000.0, maximum=100000.0, decimals=3)]
            + ([Field("classes", "Classi", codes, choices=choices, kind="checklist")] if len(codes) > 1 else []),
            intro=tr("Il modello viene tagliato con un piano orizzontale alla quota scelta (per esempio la pianta di "
                     "un "
                     "muro).") + (tr(" Togliendo delle classi restano solo i tratti che poggiano sulle classi scelte.")
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
            if s.polylines:
                s.to_dxf(out, world=True)
            return s

        def done(sec):
            if not sec.polylines:  # above or below the model: nothing to draw, no empty drawing written
                self.statusBar().showMessage(tr("Il piano a quota {0} m non taglia il modello: nessuna pianta",
                                                f"{z0:.2f}"), 15000)
                return
            for pl in sec.polylines:
                self._add_line_feature(f"Pianta a quota {z0:.2f} m", pl[:, :2], "#ff00ff",
                                       attrs={"quota": z0, "dxf": str(out)})
            self.show_view(self.ortho_view)
            self.statusBar().showMessage(tr("Pianta salvata in {0}", out), 10000)

        self.run_task("Pianta a quota", job, done, refresh=False, save=False)

    def pottery_profile(self) -> None:
        """Strumenti > Profilo ceramico da mesh: drawing and measurements of a vessel or a sherd, in a
        window that searches the similar forms of PyArchInit and saves them in a pottery form."""
        start = self._last_dir()
        v = ParamDialog.ask(self, "Profilo ceramico", [
            Field("mesh", "Mesh del vaso o frammento", "", kind="file_open",
                  filter="Mesh (*.obj *.ply *.stl *.glb *.off)"),
            Field("out", "Disegno (SVG o DXF)", str(Path(start) / "profilo.svg"), kind="file_save",
                  filter="SVG (*.svg);;DXF (*.dxf)"),
            Field("scale", "Scala 1:", 1.0, minimum=0.1, maximum=100, decimals=2),
            Field("units", "Unita' della mesh", "m", choices=[("m", "metri"), ("mm", "millimetri"),
                                                             ("cm", "centimetri")]),
            Field("axis", "Asse di rotazione", "auto", choices=[
                ("auto", "stimato dalle normali"), ("up", "asse Z della mesh, orlo in alto (tornio)"),
                ("down", "asse Z della mesh, orlo in basso (frammento appoggiato sull'orlo)")]),
            Field("rim", "Il pezzo conserva l'orlo", True,
                  tip="Diametro dell'orlo, percentuale conservata e confronto dei profili partono dall'orlo"),
            Field("title", "Titolo", "")],
            intro="L'asse di rotazione viene stimato dalle normali della superficie (funziona anche sui frammenti). "
                  "Un frammento piccolo si misura meglio con l'orlo appoggiato in piano e l'asse Z. Il disegno "
                  "viene salvato anche in PNG.")
        if not v or not v["mesh"]:
            return
        from ..archaeo import pottery

        title = v["title"] or Path(v["mesh"]).stem

        def job(p):
            import trimesh

            p(0.1, "Lettura mesh")
            mesh = trimesh.load(v["mesh"], force="mesh", process=True)
            f = {"m": 1.0, "cm": 0.01, "mm": 0.001}[v["units"]]
            if f != 1.0:
                mesh.apply_scale(f)
            p(0.3, "Asse di rotazione")
            if v["axis"] == "auto":
                axis = pottery.estimate_axis(mesh)
            else:
                axis = pottery.Axis(mesh.vertices.mean(axis=0), (0, 0, 1 if v["axis"] == "up" else -1))
            p(0.6, "Orlo e profilo")
            prof, meas = pottery.measure(mesh, axis, has_rim=v["rim"])
            out = pottery.draw(prof, v["out"], scale=v["scale"], title=title, measures=meas)
            png = pottery.draw(prof, Path(v["out"]).with_suffix(".png"), scale=v["scale"], title=title, measures=meas)
            return out, png, prof, meas

        def done(r):
            from .pottery_dialog import PotteryDialog, measure_rows

            out, png, prof, meas = r
            self._pottery = {"profile": prof, "measures": meas, "title": title, "scale": v["scale"], "id_rep": None}
            self.log_pane.info(tr("Profilo ceramico: {0}", "  -  ".join(f"{a} {b}" for a, b in measure_rows(meas))))
            if self._pottery_win is None:
                self._pottery_win = PotteryDialog(self)
                self._pottery_win.search_requested.connect(self.search_similar_pottery)
                self._pottery_win.save_requested.connect(self.save_pottery)
            self._pottery_win.set_result(png, meas, [out, png], connected=self.pa is not None)
            self._pottery_win.show()
            self._pottery_win.raise_()

        self.run_task("Profilo ceramico", job, done, refresh=False, save=False)

    def _pottery_query(self) -> dict:
        """The measurements of the last profile as a search of :meth:`PyArchInit.search_pottery` (cm)."""
        meas = self._pottery["measures"]

        def cm(key):
            return None if meas.get(key) is None else meas[key] * 100.0

        return {"rim_cm": cm("rim_diameter_m"), "max_cm": cm("max_diameter_m"), "base_cm": cm("base_diameter_m"),
                "height_cm": cm("height_m") if meas.get("full_profile") else None, "signature": meas.get("signature"),
                "exclude": self._pottery.get("id_rep")}

    def search_similar_pottery(self) -> None:
        """The forms of PyArchInit's pottery_table most like the last profile (all the sites)."""
        if not self._pottery or not self._need_pa():
            return
        pa, query = self.pa, self._pottery_query()

        def done(rows):
            if self._pottery_win is not None:
                self._pottery_win.set_matches(rows, searched=True)

        self.run_task("Ricerca di forme simili", lambda p: pa.search_pottery(**query), done, refresh=False,
                      save=False)

    def save_pottery(self, chosen=None) -> None:
        """Measurements and drawing of the last profile in a pottery form of PyArchInit: a new one,
        or the form chosen among the similar ones (or the one just saved)."""
        if not self._pottery or not self._need_pa():
            return
        site = self._ensure_site("La scheda ceramica appartiene al sito del progetto.")
        if not site:
            return
        pa, meas = self.pa, self._pottery["measures"]
        forms = [(None, tr("Nuova scheda ceramica"))]
        for rid in dict.fromkeys(x for x in (self._pottery.get("id_rep"), chosen) if x is not None):
            rec = (pa.db.query("pottery_table", id_rep=int(rid)) or [{}])[0]
            forms.append((int(rid), tr("Aggiorna la scheda n. {0} ({1}, US {2})", rec.get("id_number") or rid,
                                       rec.get("sito") or "", rec.get("us") or "")))
        lang = "EN" if i18n.current() == "en" else "IT"

        def values(code):
            return [(x, x) for _, x in pa.thesaurus(code, lingua=lang)]

        part = next((x for s, x in pa.thesaurus("11.5", lingua=lang) if s == "RIM"), "") if meas.get("rim_diameter_m") \
            and not meas.get("full_profile") else ""
        v = ParamDialog.ask(self, "Salva in PyArchInit", [
            Field("id_rep", "Scheda", chosen if chosen is not None else None, choices=forms),
            Field("area", "Area", ""), Field("us", "US", ""),
            Field("form", "Forma", "", kind="combo_text", choices=values("11.4")),
            Field("specific_form", "Parte", part, kind="combo_text", choices=values("11.5")),
            Field("specific_shape", "Forma specifica", "", kind="combo_text", choices=values("11.12")),
            Field("ware", "Classe", "", kind="combo_text", choices=values("11.6")),
            Field("overwrite", "Sostituisci i valori gia' presenti nella scheda", False)],
            intro=tr("Sito {0}. Le misure vanno nella scheda in centimetri (diametri, altezza totale o conservata, "
                     "percentuale dell'orlo); il disegno nei media della scheda (PNG, con SVG e DXF accanto) insieme "
                     "al profilo per le ricerche. Area, US e i campi vuoti non cambiano una scheda esistente.", site))
        if not v:
            return
        fields = {k: v[k] for k in ("form", "specific_form", "specific_shape", "ware") if v[k]}
        pot = self._pottery
        kw = {"area": v["area"], "us": v["us"]} if v["id_rep"] is None else \
            {k: v[k] for k in ("area", "us") if v[k].strip()}

        def job(p):
            p(0.2, "Scheda ceramica")
            return pa.save_pottery(site, meas, id_rep=v["id_rep"], profile=pot["profile"], title=pot["title"],
                                   scale=pot["scale"], overwrite=v["overwrite"], **kw, **fields)

        def done(out):
            pot["id_rep"] = out["id_rep"]
            text = tr("Scheda ceramica n. {0} salvata; disegno: {1}", out["id_rep"], out.get("image", ""))
            if out["uncertain"]:
                text += "\n\n" + tr("Diametro dell'orlo e percentuale conservata sono incerti (arco breve o "
                                     "irregolare): non scritti nella scheda, restano nel profilo salvato.")
            if out["kept"]:
                text += "\n\n" + tr("Valori gia' presenti, lasciati come erano (misurati tra parentesi): {0}",
                                     ", ".join(f"{k} {old} ({new})" for k, (old, new) in out["kept"].items()))
            self.log_pane.info(text.replace("\n\n", " "))
            self._info("PyArchInit", text)

        self.run_task("Salva in PyArchInit", job, done, refresh=False, save=False)

    def classify(self) -> None:
        if not self._need_chunk(product="point_cloud"):
            return
        from ..ai.classify import class_statistics, classify_chunk

        def done(pc):
            st = class_statistics(pc)
            self.log_pane.info(tr("Classi: ") + ", ".join(f"{k}: {n}" for k, n in sorted(st.items())))
            self.show_classes()

        self.run_task("Classificazione", lambda p: classify_chunk(self.chunk), done)

    def show_classes(self) -> None:
        """The dense cloud coloured by class, with the legend (untick a class to hide it)."""
        if not self._need_chunk(product="point_cloud"):
            return
        self.model_view.set_color_mode("class")
        self._show_model("point_cloud")
        self.statusBar().showMessage(tr("Nuvola per classi: togli la spunta nella legenda per nascondere una classe; "
                                        "«Lazo» per correggere la classe di un gruppo di punti"), 15000)

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
            self.statusBar().showMessage(tr("Nessun punto nel lazo"), 8000)
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
            Field("through", tr("Anche i punti nascosti dietro la superficie ({0} invece di {1})",
                                _it(n_all), _it(n_vis)),
                  False),
            Field("keep_copy", "Elimina / tieni: conserva prima una copia della nuvola (versione)", False)],
            intro=tr("Nel lazo ci sono {0} punti visibili ({1} contando quelli nascosti). Le classi date a mano "
                     "vengono ricordate: le classificazioni automatiche non le cambiano e il classificatore impara "
                     "da loro (Classificazione della nuvola > Addestra il classificatore). Eliminare o tenere cambia "
                     "la nuvola attuale: senza la copia non si torna indietro.", _it(n_vis), _it(n_all)))
        if not v:
            return
        mask = inside if v["through"] else visible
        action = v.get("action", "class")  # the class unless asked otherwise
        if action in ("delete", "keep"):
            from ..engine.cleaning import delete_points

            keep_selected = action == "keep"
            self.run_task("Lazo sulla nuvola", self._versioned_edit(
                "point_cloud", v, lambda p: delete_points(self.chunk, mask, keep_selected=keep_selected),
                "Nuvola modificata con il lazo"),
                lambda out: (self.log_pane.info(tr("Lazo: tolti {0} punti, restano {1}",
                                                   _it(out['removed']), _it(out['kept']))),
                             self.model_view.refresh(reset=False)), refresh=False)
            return
        code = int(v["other"] or v["code"])
        name = str(v.get("name") or "").strip() if v["other"] else ""
        self.settings.setValue("classes/last", code)
        self._set_point_class(mask, code, name or None)
        self.log_pane.info(tr("Classe {0} ({1}) assegnata a mano a {2} punti",
                              code, class_name(code, self.chunk.meta.get('classes')), int(np.count_nonzero(mask))))
        self.model_view.refresh(reset=False)
        self._dirty(True)

    def _faces_selected(self, visible, inside) -> None:
        """Faces of the model chosen with the lasso: delete them or keep only them."""
        n_vis, n_all = int(np.count_nonzero(visible)), int(np.count_nonzero(inside))
        if not n_all:
            self.statusBar().showMessage(tr("Nessuna faccia nel lazo"), 8000)
            return
        textured = self._texture_image() is not None
        v = ParamDialog.ask(self, "Facce selezionate con il lazo", [
            Field("action", "Cosa fare", "delete",
                  choices=[("delete", "Elimina queste facce"), ("keep", "Tieni solo queste facce (ritaglia)")]
                  + ([("texture", "Correggi la texture di queste facce (assegna le immagini)")] if textured else [])),
            Field("through", tr("Anche le facce nascoste dietro la superficie ({0} invece di {1})",
                                _it(n_all), _it(n_vis)),
                  False),
            Field("keep_copy", "Conserva prima una copia del modello (versione)", False)],
            intro=tr("Nel lazo ci sono {0} facce visibili ({1} contando quelle nascoste). Colori e confidenza "
                     "restano; la texture va ricostruita.", _it(n_vis), _it(n_all)))
        if not v:
            return
        from ..engine.cleaning import delete_faces

        mask = inside if v["through"] else visible
        if v.get("action") == "texture":
            self._texture_patch(np.nonzero(mask)[0])
            return
        keep_selected = v.get("action", "delete") == "keep"
        self.run_task("Lazo sul modello", self._versioned_edit(
            "model", v, lambda p: delete_faces(self.chunk, mask, keep_selected=keep_selected),
            "Modello modificato con il lazo"),
            lambda out: (self.log_pane.info(tr("Lazo: tolte {0} facce, restano {1}",
                                               _it(out['removed_faces']), _it(out['faces']))),
                         self._show_model("model")))

    def _set_point_class(self, mask, code: int, label: str | None = None) -> None:
        """Assign a class by hand: the points are flagged as labelled by hand (kept by the
        automatic classifiers, used to train the classifier)."""
        from ..ai.classify import set_class

        if self.history is not None:
            self.history.backup(["point_cloud"])
        set_class(self.chunk, mask, code, label=label)

    # ---------------------------------------------------------------- patches of the orthomosaic and texture
    def _texture_image(self):
        """The texture image of the model (None without one, or with an old OBJ texture)."""
        tex = self.chunk.product_path("model_textured") if self.chunk is not None else None
        if tex is None or not tex.exists() or tex.suffix.lower() not in (".jpg", ".jpeg", ".png", ".tif", ".tiff"):
            return None
        return tex

    def start_patch(self) -> None:
        if not self._need_chunk():
            return
        if self.chunk.orthomosaic_path is None:
            self._warn("Le patch si disegnano sull'ortofoto: costruiscila prima.")
            return
        self.set_draw_mode("patch")

    def patch_with_selection(self) -> None:
        geom = self._selected_polygon()
        if geom is None:
            self._warn("Seleziona un poligono (strumento Seleziona) oppure disegna l'area con lo strumento Patch.")
            return
        self._patch_area(geom)

    def _patch_drawn(self, xy) -> None:
        from shapely.geometry import Polygon

        geom = Polygon(np.asarray(xy, float)[:, :2])
        if not geom.is_valid or geom.area <= 0:
            self._warn("L'area non e' valida (i lati si incrociano o non ha area): ridisegnala.")
            return
        self._patch_area(geom)

    def _patch_area(self, geom) -> None:
        """The photos that see an area of the orthomosaic, each with the area seen from it: the
        user chooses, then the patch is saved and the orthomosaic updated there."""
        if not self._need_chunk():
            return
        ch = self.chunk
        if ch.orthomosaic_path is None:
            self._warn("Costruisci prima l'ortofoto.")
            return
        if geom.geom_type != "Polygon":  # several polygons selected: the largest one
            geom = max(getattr(geom, "geoms", [geom]), key=lambda g: g.area)
        polygon = np.asarray(geom.exterior.coords)[:-1, :2]

        def job(p):
            from ..engine import patches as P

            p(0.02, "Foto che vedono l'area")
            cands = P.photos_for_area(ch, polygon, limit=12)
            imgs, now, bounds = P.previews(ch, polygon, [c["label"] for c in cands], 320, progress=p.sub(0.15, 0.85))
            for c in cands:
                c["previews"] = {s: _outlined(imgs[(c["label"], s)], polygon, bounds) for s in P.SURFACES}
                c["preview"] = c["previews"]["dem"]
            return cands, None if now is None else _outlined(now, polygon, bounds)

        def done(result):
            cands, now = result
            if not cands:
                self._warn("Nessuna foto vede quest'area dell'ortofoto.")
                return
            intro = (tr("Area di {0} m2. Scegli da quali foto prendere i colori: per esempio una sola foto per "
                        "alberi, veicoli o persone che l'ortofoto mostra a vortici o tagliati, oppure escludi la "
                        "foto con un'ombra o un oggetto che si muove. La patch resta nel progetto (layer «Patch "
                        "ortofoto») e vale anche per le prossime costruzioni dell'ortofoto.", _it(geom.area, 2)))
            dlg = PatchDialog("Assegna immagini (patch dell'ortofoto)", now, cands, intro, "ortho",
                              texture=False if self._texture_image() is not None else None, parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._apply_patch(polygon, dlg.values())

        self.run_task("Foto dell'area", job, done, refresh=False, save=False)

    def _apply_patch(self, polygon, v: dict) -> None:
        skip = self._photos_to_skip("Patch dell'ortofoto")
        if skip is None:
            return
        ch = self.chunk
        view = self.ortho_view.visible_world_bounds()
        if v["texture"]:
            self.history.backup(["model_textured"])  # Modifica > Annulla puts the texture back too

        def job(p):
            from ..engine import patches as P

            patch = P.add_patch(ch, polygon, v["photos"], v["mode"], v.get("surface", "dem"))
            ch.update_orthomosaic(progress=p.sub(0.0, 0.8 if v["texture"] else 1.0), skip_unreadable=skip)
            tex = ch.patch_texture(v["photos"], polygon=polygon, mode=v["mode"], progress=p.sub(0.8, 0.2)) \
                if v["texture"] else None
            return patch, tex

        def done(result):
            patch, tex = result
            how = tr("solo da ") if patch["mode"] == "assign" else tr("senza ")
            where = tr(" sul suolo attorno all'area") if patch.get("surface") == "ground" else ""
            self.log_pane.info(tr("Patch {0}: colori {1}{2}{3}", patch['id'], how, ', '.join(patch['photos']), where))
            if tex:
                self.log_pane.info(tr("Texture corretta su {0} facce", _it(tex['faces'])))
            self._reload_ortho(view)

        self.run_task("Patch dell'ortofoto", job, done, refresh=False, undo=True)

    def update_ortho_patches(self) -> None:
        """Apply the patches added, changed or removed since the orthomosaic was written."""
        if not self._need_chunk():
            return
        from ..engine import patches as P

        ch = self.chunk
        if ch.orthomosaic_path is None:
            self._warn("Costruisci prima l'ortofoto.")
            return
        if not P.changed_patches(ch):
            self.statusBar().showMessage(tr("L'ortofoto e' gia' aggiornata con le patch"), 8000)
            return
        skip = self._photos_to_skip("Aggiorna l'ortofoto")
        if skip is None:
            return
        view = self.ortho_view.visible_world_bounds() if self.ortho_view.kind == "ortho" else None
        self.run_task("Aggiorna l'ortofoto (patch)", lambda p: ch.update_orthomosaic(progress=p, skip_unreadable=skip),
                      lambda _r: self._reload_ortho(view), refresh=False)

    def clear_ortho_patches(self) -> None:
        if not self._need_chunk():
            return
        from ..engine import patches as P

        n = len(P.patches(self.chunk))
        if not n:
            self.statusBar().showMessage(tr("Nessuna patch nell'ortofoto"), 8000)
            return
        if choice(self, "question", "Rimuovi le patch", tr("Togliere le {0} patch dell'ortofoto? Le loro aree "
                                                           "tornano come senza patch (per un'ortofoto di un altro "
                                                           "programma, i suoi pixel originali).", n),
                  ["Rimuovi"]) != 0:
            return
        P.clear_patches(self.chunk)
        self._dirty(True)
        self.update_ortho_patches()

    def _draw_patches(self) -> None:
        """The patches of the orthomosaic as a layer over it (selectable: Delete takes them away)."""
        v = self.ortho_view
        if PATCH_LAYER in v.layers:
            v.remove_layer(PATCH_LAYER)
        if self.chunk is None or v.kind != "ortho" or v.tr is None:
            return
        from ..engine import patches as P

        for patch in P.patches(self.chunk):
            names = ", ".join(short_label(n) for n in patch["photos"][:2]) + (" ..." if len(patch["photos"]) > 2 else "")
            label = f"{patch['id']}: " + (names if patch["mode"] == "assign" else tr("senza {0}", names))
            ground = patch.get("surface") == "ground"
            v.add_polygon(patch["polygon"], label=label + (tr(" (suolo)") if ground else ""), color="#e84393",
                          fill_alpha=18, layer=PATCH_LAYER,
                          attrs={"patch": patch["id"], "foto": ", ".join(patch["photos"]),
                                 "modalita": "usa solo" if patch["mode"] == "assign" else "escludi",
                                 "superficie": "suolo attorno all'area" if ground else "DEM"})

    def _reload_ortho(self, view_bounds=None) -> None:
        """The orthomosaic again after an update, on the same area as before."""
        self._load_raster("ortho")
        self.show_view(self.ortho_view)
        if view_bounds is not None and self.ortho_view.tr is not None:
            x0, y0, x1, y1 = view_bounds
            rect = QRectF(self.ortho_view.scene_pt(x0, y1), self.ortho_view.scene_pt(x1, y0)).normalized()
            self.ortho_view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        self.workspace.populate(self.doc)

    def _texture_patch(self, faces) -> None:
        """Faces of the model chosen in the 3D view: the photos that see them, then the texture
        of those faces again from the chosen ones."""
        ch = self.chunk
        faces = np.asarray(faces, int)
        if self._texture_image() is None:
            self._warn("Costruisci prima la texture del modello.")
            return
        try:
            now = np.ascontiguousarray(self.model_view.canvas.grab_array())
            now = np.dstack([now, np.full(now.shape[:2], 255, np.uint8)])
        except Exception:  # pragma: no cover - a view that cannot be grabbed
            now = None

        def job(p):
            from ..engine import patches as P

            p(0.02, "Foto che vedono le facce")
            cands = P.photos_for_faces(ch, faces, limit=12)
            for i, c in enumerate(cands):
                p(0.1 + 0.85 * i / max(len(cands), 1), tr("Facce viste da {0}", c['label']))
                c["preview"] = P.preview_faces(ch, faces, c["label"], max_side=320)
            return cands

        def done(cands):
            if not cands:
                self._warn("Nessuna foto vede queste facce.")
                return
            dlg = PatchDialog("Assegna immagini alla texture", now, cands,
                              tr("{0} facce del modello. Scegli le foto da cui prendere i loro colori: ogni faccia "
                                 "prende la migliore di quelle che la vedono.", _it(len(faces))), "faces", parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            v = dlg.values()
            self.history.backup(["model_textured"])
            self.run_task("Texture delle facce", lambda p: ch.patch_texture(v["photos"], faces=faces, mode=v["mode"],
                                                                            progress=p),
                          lambda r: (self.log_pane.info(tr("Texture corretta su {0} facce", _it(r['faces']))),
                                     self._show_model("model_textured")), refresh=False)

        self.run_task("Foto delle facce", job, done, refresh=False, save=False)

    # ---------------------------------------------------------------- versions of the products
    def _version_fields(self, product: str) -> list:
        """Keep the active version of ``product`` (when there is one) or overwrite it, and the
        name of the version built now."""
        from ..core import versions

        fields = []
        if self.chunk is not None and versions.active_files(self.chunk, product):
            name = versions.active_info(self.chunk, product)["name"]
            fields.append(Field("keep", "Versione attuale", "keep",
                                choices=[("keep", tr("Conservala per confrontarla ({0})", name)),
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

        if chunk is self.chunk and kind == "elevation":
            menu = QMenu(self)
            menu.addAction(icon("view"), tr("Mostra"), lambda: self.show_elevation(payload))
            menu.addAction(icon("export"), tr("Esporta..."), lambda: self.export_elevation(payload))
            menu.addSeparator()
            menu.addAction(icon("delete"), tr("Elimina"), lambda: self.delete_elevation(payload))
            menu.exec(pos)
            return
        if chunk is not self.chunk or kind not in ("product", "version"):
            return
        product, vid = (payload, None) if kind == "product" else payload
        if product not in versions.PRODUCTS:
            return
        menu = QMenu(self)
        if vid is None:
            menu.addAction(icon("view"), tr("Mostra"), lambda: self._workspace_item("product", chunk, product))
            menu.addAction(icon("recent"), tr("Conserva una copia come versione"),
                           lambda: self.keep_version_copy(product))
            menu.addAction(icon("edit"), tr("Rinomina questa versione..."), lambda: self.rename_version(product, None))
        else:
            menu.addAction(icon("view"), tr("Mostra"), lambda: self.show_version(product, vid))
            menu.addAction(icon("ok"), tr("Rendi attiva (la usano gli altri strumenti)"),
                           lambda: self.activate_version(product, vid))
            if product == "dem" and chunk.dem_path:
                menu.addAction(icon("dem"), tr("Differenza con il DEM attivo"), lambda: self.dem_difference(vid))
            menu.addAction(icon("export"), tr("Esporta..."), lambda: self.export_version(product, vid))
            menu.addAction(icon("edit"), tr("Rinomina..."), lambda: self.rename_version(product, vid))
            menu.addSeparator()
            menu.addAction(icon("delete"), tr("Elimina"), lambda: self.delete_version(product, vid))
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
        self.statusBar().showMessage(tr("Versione «{0}» (non attiva): doppio clic sul prodotto nell'area di lavoro "
                                        "per tornare a quella attiva", title), 15000)

    def keep_version_copy(self, product: str) -> None:
        """Keep a copy of the active version (e.g. before classifying the cloud again)."""
        from ..core import versions

        chunk = self.chunk
        self.run_task("Copia della versione", lambda p: versions.keep_active(chunk, product),
                      lambda e: self.log_pane.info(tr("Conservata una copia: «{0}»", e['name'])) if e else None)

    def activate_version(self, product: str, vid: str) -> None:
        from ..core import versions

        chunk = self.chunk
        title = self._version_title(product, vid)

        def done(_previous):
            self.log_pane.info(tr("Versione attiva: «{0}»", title))
            if product in ("dem", "orthomosaic"):
                self._show_raster("dem" if product == "dem" else "ortho")
            else:
                self.model_view.set_chunk(chunk, product)
                self.show_view(self.model_view)

        self.run_task("Cambio di versione", lambda p: versions.activate(chunk, product, vid), done)

    def rename_version(self, product: str, vid: str | None) -> None:
        from ..core import versions

        old = versions.active_info(self.chunk, product)["name"] if vid is None else self._version_title(product, vid)
        name, ok = QInputDialog.getText(self, tr("Rinomina versione"), tr("Nome:"), text=old)
        if ok and name.strip():
            versions.rename(self.chunk, product, vid, name.strip())
            self._dirty(True)
            self.workspace.populate(self.doc)

    def delete_version(self, product: str, vid: str) -> None:
        from ..core import versions

        title = self._version_title(product, vid)
        r = message(self, "question", "Elimina versione", tr("Eliminare la versione «{0}» e i suoi file?", title),
                    buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        if getattr(self, "_raster_version", None) == (product, vid) or self.model_view.paths:
            self.model_view.set_chunk(self.chunk, self.model_view.kind)
            self._load_raster()
        versions.delete(self.chunk, product, vid)
        self._dirty(True)
        self.workspace.populate(self.doc)
        self.log_pane.info(tr("Versione «{0}» eliminata", title))

    def export_version(self, product: str, vid: str) -> None:
        import shutil

        from ..core import versions

        src = versions.version_file(self.chunk, product, vid)
        path, _ = QFileDialog.getSaveFileName(self, tr("Esporta versione"), str(Path(self._last_dir()) / src.name),
                                              f"*{src.suffix}")
        if path:
            shutil.copy2(src, path)
            self.log_pane.info(tr("Versione «{0}» esportata in {1}", self._version_title(product, vid), path))

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
            text = (tr("Differenza «{0}» - «{1}»: rosso dove l'attivo e' piu' alto, blu dove e' piu' basso (colori "
                       "pieni a +/-{2:.2f} m); dZ sotto il cursore", active, title, lim))
            if r1 and r2 and abs(r1 - r2) > 1e-6 * max(r1, r2):
                text += (tr(". Attenzione: risoluzioni diverse ({0:.3g} e {1:.3g} m), i bordi ripidi risultano "
                            "diversi; per un confronto pulito usa la stessa risoluzione", r1, r2))
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
        return [(c, thousands_points(tr("{0} {1} ({2:,} "
                                        "punti)", c, class_name(c, labels), counts[c]))) for c in codes], codes

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
            self.log_pane.info(tr("Classi dalle US: ") + ", ".join(tr("{0} = {1} ({2} punti)",
                                                                  r['name'], r['code'], r['points'])
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
        """What the classifier learnt: method, radii, accuracy, figures per class."""
        from .scene import class_name

        st = model.stats or {}
        labels = self.chunk.meta.get("classes", {}) if self.chunk is not None else {}
        num = lambda n: thousands_points(f"{int(n):,}")  # noqa: E731
        method = "Random forest" if model.method == "random_forest" else "Gradient boosting"
        lines = [tr("{0}: {1} caratteristiche per punto, raggi ", method, len(model.feature_names))
                 + " / ".join(f"{r:.3g}" for r in model.scales) + " m."]
        acc = st.get("validation_accuracy")
        if st:
            check = tr("sui campioni esclusi da ogni albero") if st.get("validation") == "out-of-bag" else \
                tr("su un quinto dei campioni tenuto da parte")
            lines.append(tr("Addestrato su {0} campioni ({1} punti con la classe nota)",
                            num(st.get('n_samples', 0)), num(st.get('n_labelled_points', 0)))
                         + ("" if acc is None else tr("; accuratezza {0}: {1:.1%}.", check, acc)))
        for c in model.classes:
            fig = st.get("per_class", {}).get(str(c), {})
            name = class_name(c, labels)
            if name == tr("Classe {0}", c) and model.class_names.get(c, f"Class {c}") != f"Class {c}":
                name = model.class_names[c]  # named in the project where the classifier was trained
            pr, rc = fig.get("precision"), fig.get("recall")
            lines.append(tr("  {0} {1}: {2} campioni", c, name, num(fig.get('samples', 0)))
                         + ("" if pr is None else tr(", precisione {0:.1%}", pr))
                         + ("" if rc is None else tr(", richiamo {0:.1%}", rc)))
        return "\n".join(lines)

    def _log_classes(self, pc) -> None:
        from ..ai.classify import class_statistics

        st = class_statistics(pc)
        self.log_pane.info(tr("Classi: ") + ", ".join(f"{k}: {n}" for k, n in sorted(st.items())))

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
        names = lambda codes: ", ".join(f"{c} {class_name(c, labels)}" for c in codes) or tr("nessuna")  # noqa: E731
        v = ParamDialog.ask(self, "Addestra il classificatore", [
            Field("labels", "Impara da", "manual" if len(by_hand) >= 2 else "all",
                  choices=[("manual", thousands_points(tr("Punti corretti a mano ({0:,} punti)", int(manual.sum())))),
                           ("all", "Tutti i punti con una classe (sopra 1)")]),
            Field("method", "Metodo", "random_forest",
                  choices=[("random_forest", "Random forest (consigliato)"),
                           ("gradient_boosting", "Gradient boosting (file piu' piccolo)")]),
            Field("trees", "Alberi (o iterazioni)", 100, minimum=10, maximum=1000),
            Field("apply", "Poi classifica il resto della nuvola", True)],
            intro=tr("Il classificatore impara dai punti di cui conosci la classe: con il lazo (vista 3D) assegna la "
                     "classe giusta ad alcuni gruppi di punti di ogni tipo (almeno due classi), poi addestralo. Usa "
                     "la geometria attorno a ogni punto a tre scale, l'altezza sul terreno e il colore.\n\nClassi "
                     "corrette a mano: {0}.\nClassi presenti: {1}.", names(by_hand), names(classified)))
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
            self._info("Classificatore addestrato", self._classifier_report(model) + tr("\n\nSalvato nel progetto; "
                                                                                        "con «Salva il "
                                                                                        "classificatore» si usa anche "
                                                                                        "in altri scavi."))

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
            intro=tr("Classi del classificatore: ") + ", ".join(f"{c} {model.class_names.get(c) or class_name(c)}"
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
        path, _ = QFileDialog.getOpenFileName(self, tr("Carica classificatore"), self._last_dir(),
                                              tr("Classificatori (*.ppgclf)"))
        if not path or not self._need_sklearn():
            return False
        from ..ai.learn import ClassifierModel

        try:
            self.classifier = ClassifierModel.load(path)
        except Exception as exc:
            self._exc("Classificatore non leggibile", exc)
            return False
        self.log_pane.info(tr("Classificatore caricato da {0}\n{1}", path, self._classifier_report(self.classifier)))
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
        path, _ = QFileDialog.getSaveFileName(self, tr("Salva classificatore"),
                                              self._last_dir() + "/classificatore.ppgclf",
                                              tr("Classificatori (*.ppgclf)"))
        if path:
            self.log_pane.info(tr("Classificatore salvato in {0}", model.save(path)))

    def clear_manual_labels(self) -> None:
        if not self._need_chunk(product="point_cloud"):
            return
        r = message(self, "question", "Correzioni a mano", "Dimenticare quali punti sono stati corretti a mano? "
                    "Le classi restano, ma le prossime classificazioni automatiche potranno cambiarle.",
                    buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        from ..ai.classify import clear_manual

        if self.history is not None:
            self.history.backup(["point_cloud"])
        n = clear_manual(self.chunk)
        self.log_pane.info(tr("Correzioni a mano dimenticate su {0} punti (le classi restano)", n))
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
            self._info("Segment Anything 2.1", tr("SAM 2.1 installato in {0}.\n\nCalcolo su: {1}.", env, device))
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
        edit = tr(" Maiusc+clic aggiunge una parte, clic destro la toglie; Invio salva, Esc annulla.")
        return {"tap": tr("SAM, tocco: clic sull'oggetto. SAM guarda l'area visibile: avvicinati per i dettagli, "
                          "allontanati per gli oggetti grandi.") + edit,
                "box": tr("SAM, riquadro: trascina un riquadro attorno all'oggetto.") + edit,
                "area": tr("SAM, area: trascina un riquadro; SAM cerca tutte le unita' al suo interno.")}.get(
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
        if self.ortho_view.sam_tool == "stones":
            self._sam_cancel()
            self.draw_stones((x0, y0, x1, y1))
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
                self.statusBar().showMessage(tr("SAM non ha trovato un oggetto qui: prova un altro punto o cambia lo "
                                                "zoom"), 10000)
                if ask:
                    self._sam_cancel()
                return
            self._sam_obj = {**obj, "geom": unit.geometry}
            self.ortho_view.show_sam_preview(unit.geometry, pts, obj["box"])
            if ask:
                self._sam_accept()
            else:
                self.statusBar().showMessage(tr("Maschera aggiornata ({0:.3f} m2): Invio per salvare, Esc per "
                                                "annullare", unit.geometry.area), 15000)

        if not self.runner.run("SAM 2.1", job, done, self._error, quiet=True):
            self.statusBar().showMessage(tr("SAM sta ancora lavorando: attendi il risultato"), 5000)

    def _sam_key(self, key: str) -> None:
        if key == "accept" and self._sam_obj is not None and self._sam_obj.get("geom") is not None:
            self._sam_accept()
        elif key == "cancel":
            self._sam_cancel()
            self.statusBar().showMessage(tr("Maschera di SAM annullata"), 5000)

    def _sam_accept(self) -> None:
        obj = self._sam_obj
        if obj is None or obj.get("geom") is None:
            return
        r, v = self._ask_unit(refine=True, title="Unita' da SAM")
        if r == 1:
            self._sam_cancel()
            self._add_unit(self._make_unit(obj["geom"], v))
        elif r == UnitDialog.REFINE:
            self.statusBar().showMessage(tr("Correggi la maschera: Maiusc+clic aggiunge, clic destro toglie; "
                                            "Invio per salvare, Esc per annullare"), 30000)
        else:
            self._sam_cancel()

    # ---------------------------------------------------------------- stone-by-stone drawing
    def draw_stones(self, bounds=None) -> None:
        """The stones SAM finds in ``bounds`` of the orthophoto or elevation shown (layer *Pietre*,
        kept with that raster; the stones already there inside the box are found again)."""
        view = self.ortho_view
        if view.path is None or view.kind != "ortho" or view.tr is None:
            self._warn("Mostra l'ortofoto o il prospetto di un muro per disegnarne le pietre.")
            return
        if not self._sam_available(then=lambda: self.draw_stones(bounds)):
            return
        from ..ai.sam import THOROUGHNESS
        from ..archaeo.stones import find_stones

        v = ParamDialog.ask(self, "Pietre (SAM)", [
            Field("min_size", "Pietra piu' piccola (m)", float(self.settings.value("stones/min", 0.05)), minimum=0.005,
                  maximum=5.0, decimals=3),
            Field("max_size", "Pietra piu' grande (m)", float(self.settings.value("stones/max", 0.8)), minimum=0.01,
                  maximum=20.0, decimals=3),
            Field("thoroughness", "Scansione", "alta", choices=[(k, k.capitalize()) for k in THOROUGHNESS])],
            intro=tr("SAM cerca ogni oggetto nel riquadro; restano quelli della misura di una pietra e di forma "
                     "compatta. Le pietre vanno nel layer «Pietre» di questa immagine; le sbagliate si tolgono con "
                     "Seleziona e Canc, Esporta il disegno delle pietre le scrive in DXF, SVG in scala o GeoJSON."))
        if not v:
            return
        self.settings.setValue("stones/min", v["min_size"])
        self.settings.setValue("stones/max", v["max_size"])
        path, key = view.path, view.path.name

        def job(p):
            return find_stones(path, self._segmenter(), bounds=bounds, min_size=v["min_size"], max_size=v["max_size"],
                               thoroughness=v["thoroughness"], progress=p)

        def done(found):
            from shapely.geometry import box

            area = box(*bounds) if bounds is not None else None
            kept = [s for s in self.stones.get(key, []) if area is not None and not s.intersects(area)]
            self.stones[key] = kept + found
            self._draw_stones()
            self._dirty(True)
            self.statusBar().showMessage(tr("{0} pietre nel layer «Pietre»", len(found)), 15000)

        self.run_task("SAM - pietre", job, done, refresh=False, save=False)

    def _draw_stones(self) -> None:
        from ..archaeo.stones import size_of

        view = self.ortho_view
        view.remove_layer(STONES_LAYER)
        if view.path is None or view.tr is None:
            return
        with view.batch():
            for i, g in enumerate(self.stones.get(view.path.name, [])):
                for part in getattr(g, "geoms", [g]):
                    view.add_polygon(np.asarray(part.exterior.coords)[:, :2], "", "#5b3a1e", fill_alpha=0,
                                     layer=STONES_LAYER, attrs={"n": i + 1, "area_m2": round(g.area, 4),
                                                                "size_m": round(size_of(g), 3), "_stone": i})

    def export_stones_drawing(self) -> None:
        """The stones of the orthophoto or elevation shown as a drawing (DXF, SVG at scale, GeoJSON)."""
        from ..archaeo.stones import export_stones

        view = self.ortho_view
        stones = self.stones.get(view.path.name, []) if view.path is not None else []
        if not stones:
            self._warn("Nessuna pietra su questa immagine: usa lo strumento SAM «Pietre» (trascina un riquadro).")
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Disegno delle pietre"), str(Path(self._last_dir()) / "pietre.dxf"),
                                              "DXF (*.dxf);;SVG (*.svg);;GeoJSON (*.geojson)")
        if not path:
            return
        scale = 20.0
        if path.lower().endswith(".svg"):
            v = ParamDialog.ask(self, "Disegno delle pietre", [Field("scale", "Scala 1:", 20.0, minimum=1.0,
                                                                      maximum=1000.0, decimals=0)])
            if not v:
                return
            scale = v["scale"]
        try:
            out = export_stones(stones, path, scale=scale, title=view.path.stem)
        except Exception as exc:
            self._exc(tr("Salvataggio non riuscito: {0}", path), exc)
            return
        self.log_pane.info(tr("{0} pietre scritte in {1}", len(stones), out))
        self.statusBar().showMessage(tr("{0} pietre scritte in {1}", len(stones), out), 10000)

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
            self.statusBar().showMessage(tr("{0} unita' candidate nel layer «Unita' da SAM»: assegna le US (pannello "
                                            "Layer) prima di pubblicarle", len(units)), 20000)
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
            intro=tr("L'ortofoto ({0:.0f} m di lato, pixel di {1:.1f} cm) viene esaminata a tasselli di 1024 pixel "
                     "che si sovrappongono: alla risoluzione proposta sono circa {2} tasselli. Con la GPU del Mac "
                     "(MPS) un tassello richiede circa 10 s in scansione normale, 40-90 s in alta o massima. Le "
                     "unita' trovate vanno nel layer «Unita' da SAM», senza numero di US.",
                     side, native * 100, tiles(suggested)))
        if not v:
            return
        self._ensure_site("Le US trovate da SAM appartengono a un sito: sceglilo una volta per il progetto.")
        self._sam_auto(None, title="SAM - tutta l'ortofoto", resolution=v["resolution"], thoroughness=v["thoroughness"],
                       min_area_m2=v["min_area"], exclusive=v["exclusive"])

    def toggle_photo_sam(self) -> None:
        if not self.photo_view.sam_mode and not self._sam_available(then=self.toggle_photo_sam):
            return
        self.photo_view.sam_mode = not self.photo_view.sam_mode
        self.statusBar().showMessage(tr("Prompt SAM sulle foto ") + (tr("attivi: clic sinistro sull'oggetto")
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
            choices.append(("proxies", tr("Proxy solidi delle US ({0}): volumi e quantita' per il computo",
                                          len(self.chunk.meta['proxies']))))
        if drawn:
            choices.append(("drawn", tr("Unita' disegnate in questo progetto ({0})", len(drawn))))
        choices += [("pa:" + s, tr("Poligoni US da PyArchInit - {0}", s)) for s in sites]
        choices.append(("none", "Solo il modello del rilievo (un elemento)"))
        v = ParamDialog.ask(self, "Esporta IFC (BIM)", [
            Field("path", "File IFC", str(Path(self._last_dir()) / "rilievo.ifc"), kind="file_save",
                  filter="IFC (*.ifc)"),
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
                p(0.02, tr("Lettura dei poligoni US di {0}", site))
                units = self.pa.us_polygons(site, crs=self.chunk.crs if self.chunk else None)
            if self.pa is not None and site:
                try:
                    records = self.pa.us_records(site)
                except Exception:
                    records = None
            out = export_chunk_ifc(self.chunk, path, units=units, us_records=records, site=site,
                                   include_survey=v["survey"], progress=p.sub(0.05, 0.85), quote=self.quote)
            p(0.92, "Verifica del file IFC")
            return out, read_ifc_elements(out), len(units or [])

        def done(r):
            out, elements, n_units = r
            self.log_pane.info(tr("IFC salvato in {0}: {1} elementi", out, len(elements)))
            if n_units and len(elements) <= (1 if v["survey"] else 0):
                self._warn("Nessuna US interseca il modello: controlla che i poligoni stiano sull'area rilevata "
                           "e nello stesso sistema di riferimento.")
            IfcPreviewDialog(out, elements, self).exec()

        self.run_task("Esportazione IFC", job, done, refresh=False, save=False)

    def run_script(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("Script Python"), self._last_dir(), "Python (*.py)")
        if not path:
            return
        src = Path(path).read_text(encoding="utf-8")
        self._show_pane(self.bottom_tabs, self.console)
        self.console.write(tr("# esecuzione di {0}\n", path))
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
            if self._pottery_win is not None:
                self._pottery_win.set_connected(True)
            log.info("PyArchInit connesso: %s", pa.label)
            self.d_pa.show()
            self._sync_site_with_db(ask=self._project_open() and not quiet)
            try:
                missing = pa.missing_us_columns()
            except Exception:
                missing = []
            if missing:
                self.log_pane.warning(tr("La scheda US di questo database e' ridotta: mancano {0} campi di "
                                         "PyArchInit 5",
                                         len(missing)) + (tr(", tra cui i rapporti") if "rapporti" in missing else "")
                                      + tr(". PyArchInit > Aggiorna struttura del database li aggiunge."))

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
                    tr("Alla tabella us_table mancano {0} campi della scheda US di PyArchInit 5", len(missing))
                    + (tr(", tra cui i rapporti stratigrafici") if "rapporti" in missing else "") + ".\n\n"
                    + tr("Aggiungerli? I record esistenti non vengono modificati.")
                    + (tr("\nPrima viene fatta una copia di sicurezza del file.") if sqlite
                       else tr("\nSu PostgreSQL fai prima un backup del database.")),
                    buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    default=QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return

        def done(res):
            self.pa_pane.refresh()
            text = tr("Aggiunti {0} campi alla scheda US.", len(res['added']))
            if res.get("backup"):
                text += tr("\nCopia di sicurezza: {0}", res['backup'])
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
            self.log_pane.info(tr("Database demo in {0}: per vederlo in QGIS seleziona demo.sqlite nelle "
                                  "impostazioni di PyArchInit", db))
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
        self.log_pane.info(tr("Sito creato in PyArchInit: {0}", dlg.values()['sito']))

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
                out["modello"] = self.pa.publish_model(self.chunk, v["sito"], v["nome_doc"] + tr(" modello"), **kw)
            return out

        def done(out):
            self.pa_pane.refresh()
            self._info("PyArchInit", tr("Pubblicato:\n") + json.dumps(out, indent=1, default=str))

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
            self._warn(tr("Niente da pubblicare: disegna (o segmenta con SAM) delle unita' con il numero di US, o "
                          "delle "
                          "quote") + (tr(" ({0} unita' sono ancora senza US)", no_us) if no_us else "") + ".")
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
            self._info("PyArchInit", tr("Sito {0}: {1} poligoni scritti in pyunitastratigrafiche, {2} quote in "
                                        "pyarchinit_quote.", site, len(gu), len(gq)) + (tr("\n{0} unita' senza numero "
                                                                                           "di US non sono state "
                                                                                           "pubblicate.",
                                                    no_us)
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
        self.statusBar().showMessage(tr("{0} poligoni US caricati dal sito {1}", len(polys), site), 10000)


class DoctorDialog(QDialog):
    """Environment diagnosis (``ppg doctor``) with copy button."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from ..doctor import run_checks

        self.setWindowTitle(tr("Diagnostica ambiente"))
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        self.report = run_checks(gui=True)
        head = QLabel((tr("Nessun problema bloccante.") if not self.report.failures else
                       tr("{0} problemi impediscono il funzionamento.", len(self.report.failures)))
                      + (tr("  {0} avvisi.", len(self.report.warnings)) if self.report.warnings else ""))
        head.setObjectName("errorSummary")
        lay.addWidget(head)
        self.box = QPlainTextEdit(self.report.text())
        self.box.setObjectName("tracebackBox")
        self.box.setReadOnly(True)
        lay.addWidget(self.box, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        b_copy = QPushButton(icon("copy"), tr("Copia"))
        b_copy.clicked.connect(lambda: QApplication.clipboard().setText(self.report.text()))
        bb.addButton(b_copy, QDialogButtonBox.ButtonRole.ActionRole)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
