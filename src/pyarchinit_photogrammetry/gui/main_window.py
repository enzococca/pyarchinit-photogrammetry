"""Main window: menus, workspace, views and docked panes, all on the Python API
(every action is a call that a script could make through ``ppg.app.document``)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QDockWidget, QFileDialog, QInputDialog, QLabel, QMainWindow,
                               QMessageBox, QTabWidget, QToolBar, QWidget)

from .. import __version__
from ..app import app as application
from ..core.document import Document
from .dialogs import QUALITY, Field, ImageDialog, ParamDialog, PublishDialog, UnitDialog
from .image_views import US_COLORS, PhotoView, RasterView
from .model_view import ModelView
from .panels import ConsolePane, LogPane, PhotosPane, PyArchInitPane, ReferencePane, WorkspaceTree
from .worker import TaskRunner

TARGET_TYPES = [("aruco_4x4_50", "ArUco 4x4 (50 codici)"), ("aruco_4x4_250", "ArUco 4x4 (250 codici)"),
                ("aruco_5x5_100", "ArUco 5x5 (100 codici)"), ("aruco_6x6_250", "ArUco 6x6 (250 codici)"),
                ("apriltag_36h11", "AprilTag 36h11")]


class MainWindow(QMainWindow):
    def __init__(self, document: Document | None = None):
        super().__init__()
        self.setWindowTitle("pyarchinit-photogrammetry")
        self.resize(1500, 950)
        self.settings = QSettings("pyarchinit", "pyarchinit-photogrammetry")
        self.runner = TaskRunner(self)
        self.runner.status.connect(lambda s: self.statusBar().showMessage(s, 8000))
        self.runner.busy_changed.connect(self._busy)
        self.pa = None  # PyArchInit bridge
        self.units: list = []  # stratigraphic units drawn on the orthomosaic
        self.sam = None
        self.sam_points: list = []
        self.last_report: dict | None = None
        self._build_views()
        self._build_docks()
        self._build_actions()
        self.coord_label = QLabel("")
        self.statusBar().addPermanentWidget(self.coord_label)
        self.set_document(document or application.document)

    # ================================================================ layout
    def _build_views(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.model_view = ModelView()
        self.ortho_view = RasterView()
        self.photo_view = PhotoView()
        self.tabs.addTab(self.model_view, "Modello")
        self.tabs.addTab(self.ortho_view, "Ortofoto / DEM")
        self.tabs.addTab(self.photo_view, "Foto")
        self.setCentralWidget(self.tabs)
        self.ortho_view.mouse_world.connect(self._show_coords)
        self.ortho_view.polygon_finished.connect(self._unit_drawn)
        self.ortho_view.line_finished.connect(self._section_drawn)
        self.ortho_view.measured.connect(lambda t: (self.statusBar().showMessage(t, 30000),
                                                    self.log_pane.appendPlainText(t)))
        self.ortho_view.point_clicked.connect(self._ortho_point)
        self.photo_view.markers_changed.connect(self._markers_edited)
        self.photo_view.sam_prompt.connect(self._photo_sam_prompt)

    def _dock(self, title: str, widget: QWidget, area, name: str, tab_with: QDockWidget | None = None) -> QDockWidget:
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setWidget(widget)
        if tab_with is None:
            self.addDockWidget(area, d)
        else:
            self.tabifyDockWidget(tab_with, d)
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
        self.pa_pane.connect_requested.connect(self.pyarchinit_connect)
        self.pa_pane.demo_requested.connect(self.pyarchinit_demo)
        # tab widgets inside two docks (more robust than tabified docks across Qt platforms)
        self.left_tabs = QTabWidget()
        self.left_tabs.addTab(self.workspace, "Area di lavoro")
        self.left_tabs.addTab(self.reference, "Riferimenti")
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.addTab(self.photos, "Foto")
        self.bottom_tabs.addTab(self.console, "Console Python")
        self.bottom_tabs.addTab(self.log_pane, "Log")
        self.d_work = self._dock("Progetto", self.left_tabs, L, "workspace")
        self.d_pa = self._dock("PyArchInit", self.pa_pane, R, "pyarchinit")
        self.d_photos = self._dock("Foto, console e log", self.bottom_tabs, B, "bottom")
        self.resizeDocks([self.d_work, self.d_pa], [360, 300], Qt.Orientation.Horizontal)
        self.resizeDocks([self.d_photos], [230], Qt.Orientation.Vertical)

    def _act(self, menu, text: str, slot, shortcut=None, tip: str = "", processing: bool = False) -> QAction:
        a = QAction(text, self)
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
        self._act(m, "Nuovo progetto", self.new_document, QKeySequence.StandardKey.New)
        self._act(m, "Apri...", self.open_document, QKeySequence.StandardKey.Open)
        self.recent_menu = m.addMenu("Progetti recenti")
        self._act(m, "Salva", self.save_document, QKeySequence.StandardKey.Save)
        self._act(m, "Salva con nome...", lambda: self.save_document(ask=True), QKeySequence.StandardKey.SaveAs)
        m.addSeparator()
        exp = m.addMenu("Esporta")
        self._act(exp, "Nuvola di punti (LAS/PLY)...", lambda: self.export("cloud"))
        self._act(exp, "Modello (OBJ/PLY)...", lambda: self.export("model"))
        self._act(exp, "DEM (GeoTIFF)...", lambda: self.export("dem"))
        self._act(exp, "Ortofoto (GeoTIFF)...", lambda: self.export("ortho"))
        self._act(exp, "Camere (CSV / COLMAP)...", lambda: self.export("cameras"))
        self._act(exp, "Riferimenti (CSV)...", lambda: self.export("reference"))
        self._act(m, "Genera report HTML...", self.generate_report, processing=True)
        m.addSeparator()
        self._act(m, "Crea e apri progetto demo...", self.open_demo, processing=True)
        m.addSeparator()
        self._act(m, "Esci", self.close, QKeySequence.StandardKey.Quit)
        # ------------------------------------------------------------ Workflow
        m = mb.addMenu("Flusso di &lavoro")
        self._act(m, "Aggiungi foto...", self.add_photos, processing=True)
        self._act(m, "Aggiungi cartella...", lambda: self.add_photos(folder=True), processing=True)
        self._act(m, "Aggiungi chunk", self.add_chunk)
        m.addSeparator()
        self._act(m, "Rileva marker codificati...", self.detect_markers, processing=True)
        self._act(m, "Allinea foto...", self.align_photos, processing=True)
        self._act(m, "Importa riferimenti...", self.import_reference)
        self._act(m, "Aggiorna georeferenziazione", self.update_transform, processing=True)
        self._act(m, "Ottimizza camere", self.optimize_cameras, processing=True)
        m.addSeparator()
        self._act(m, "Costruisci nuvola densa...", self.build_dense, processing=True)
        self._act(m, "Costruisci modello...", self.build_model, processing=True)
        self._act(m, "Costruisci texture...", self.build_texture, processing=True)
        self._act(m, "Costruisci DEM...", self.build_dem, processing=True)
        self._act(m, "Costruisci ortofoto...", self.build_ortho, processing=True)
        m.addSeparator()
        self._act(m, "Elabora tutto...", self.process_all, "Ctrl+R", processing=True)
        # ------------------------------------------------------------ Tools
        m = mb.addMenu("&Strumenti")
        tm = m.addMenu("Target")
        self._act(tm, "Stampa fogli di target...", self.print_targets)
        self._act(tm, "Tappetino per turntable...", self.print_turntable)
        dm = m.addMenu("Disegno su ortofoto")
        group = QActionGroup(self)
        for mode, label in (("pan", "Sposta (navigazione)"), ("polygon", "Disegna unita' stratigrafica"),
                            ("line", "Traccia sezione"), ("measure", "Misura distanza"),
                            ("point", "Quota puntuale / prompt SAM")):
            a = QAction(label, self, checkable=True)
            a.setChecked(mode == "pan")
            a.triggered.connect(lambda _=False, md=mode: self.set_draw_mode(md))
            group.addAction(a)
            dm.addAction(a)
        self._act(dm, "Cancella disegni", self.clear_drawings)
        m.addSeparator()
        self._act(m, "Curve di livello...", self.contours, processing=True)
        self._act(m, "Volume scavo / riporto...", self.compute_volume, processing=True)
        self._act(m, "Pianta a quota (slice orizzontale)...", self.plan_slice, processing=True)
        self._act(m, "Profilo ceramico da mesh...", self.pottery_profile, processing=True)
        m.addSeparator()
        self._act(m, "Classifica nuvola di punti", self.classify, processing=True)
        sm = m.addMenu("Segment Anything 2.1")
        self._act(sm, "Unita' automatiche sull'ortofoto", self.sam_auto_units, processing=True)
        self._act(sm, "Segmenta dai punti cliccati sull'ortofoto", self.sam_from_points, processing=True)
        self._act(sm, "Prompt sulle foto (attiva/disattiva)", self.toggle_photo_sam)
        m.addSeparator()
        self._act(m, "Esporta IFC (BIM)...", self.export_ifc, processing=True)
        self._act(m, "Esegui script Python...", self.run_script)
        # ------------------------------------------------------------ PyArchInit
        m = mb.addMenu("&PyArchInit")
        self._act(m, "Connetti (config.cfg)...", self.pyarchinit_connect)
        self._act(m, "Connetti a un database (SQLite / PostgreSQL)...", lambda: self.pyarchinit_connect(ask_url=True))
        self._act(m, "Usa il database demo", self.pyarchinit_demo)
        m.addSeparator()
        self._act(m, "Pubblica ortofoto / DEM / modello...", self.pyarchinit_publish, processing=True)
        self._act(m, "Pubblica unita' disegnate (pyunitastratigrafiche)", self.pyarchinit_publish_units,
                  processing=True)
        self._act(m, "Carica poligoni US sull'ortofoto", self.pyarchinit_load_units)
        self._act(m, "Pubblica quote dei punti cliccati", self.pyarchinit_publish_points, processing=True)
        self._act(m, "Aggiorna pannello", lambda: self.pa_pane.refresh())
        # ------------------------------------------------------------ View
        m = mb.addMenu("&Vista")
        for d in (self.d_work, self.d_photos, self.d_pa):
            m.addAction(d.toggleViewAction())
        m.addSeparator()
        self._act(m, "Area di lavoro", lambda: self._show_pane(self.left_tabs, self.workspace))
        self._act(m, "Riferimenti", lambda: self._show_pane(self.left_tabs, self.reference))
        self._act(m, "Foto (miniature)", lambda: self._show_pane(self.bottom_tabs, self.photos))
        self._act(m, "Console Python", lambda: self._show_pane(self.bottom_tabs, self.console), "Ctrl+Shift+P")
        self._act(m, "Log", lambda: self._show_pane(self.bottom_tabs, self.log_pane))
        m.addSeparator()
        self._act(m, "Modello", lambda: self.tabs.setCurrentWidget(self.model_view), "F2")
        self._act(m, "Ortofoto / DEM", lambda: self.tabs.setCurrentWidget(self.ortho_view), "F3")
        self._act(m, "Foto", lambda: self.tabs.setCurrentWidget(self.photo_view), "F4")
        # ------------------------------------------------------------ Help
        m = mb.addMenu("&Aiuto")
        self._act(m, "Documentazione online", lambda: QDesktopServices.openUrl(
            QUrl("https://github.com/enzococca/pyarchinit-photogrammetry")))
        self._act(m, "Informazioni", self.about)
        # ------------------------------------------------------------ toolbar
        tb = QToolBar("Flusso di lavoro")
        tb.setObjectName("workflow_toolbar")
        self.addToolBar(tb)
        for text, slot in (("Aggiungi foto", self.add_photos), ("Marker", self.detect_markers),
                           ("Allinea", self.align_photos), ("Riferimenti", self.import_reference),
                           ("Nuvola densa", self.build_dense), ("Modello", self.build_model),
                           ("Texture", self.build_texture), ("DEM", self.build_dem), ("Ortofoto", self.build_ortho),
                           ("Elabora tutto", self.process_all)):
            a = tb.addAction(text)
            a.triggered.connect(lambda _=False, s=slot: s())
            self._processing_actions.append(a)
        self._refresh_recent()

    def _show_pane(self, tabs: QTabWidget, widget: QWidget) -> None:
        dock = self.d_work if tabs is self.left_tabs else self.d_photos
        dock.show()
        tabs.setCurrentWidget(widget)

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
        self.refresh_all(reset_views=True)
        if doc.path:
            self._add_recent(doc.path)

    def refresh_all(self, reset_views: bool = False) -> None:
        name = self.doc.path.name if self.doc.path else "senza nome"
        self.setWindowTitle(f"pyarchinit-photogrammetry {__version__} - {name}[*]")
        self.workspace.populate(self.doc)
        self.reference.set_chunk(self.chunk, self.last_report)
        self.photos.set_chunk(self.chunk)
        self.model_view.set_chunk(self.chunk) if reset_views else self.model_view.set_chunk(self.chunk,
                                                                                             self.model_view.kind)
        self._load_raster()

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

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self, "pyarchinit-photogrammetry", text)

    def _error(self, err: str) -> None:
        first = err.strip().splitlines()[0] if err.strip() else "Errore"
        box = QMessageBox(QMessageBox.Icon.Critical, "Errore", first, parent=self)
        box.setDetailedText(err)
        box.exec()
        self.log_pane.appendPlainText(err)

    def run_task(self, label: str, fn, on_done=None, refresh: bool = True, save: bool = True) -> bool:
        def done(result):
            if save and self.doc.path:
                try:
                    self.doc.save()
                except Exception as exc:
                    self.log_pane.appendPlainText(f"Salvataggio non riuscito: {exc}")
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
        self.set_document(Document())

    def open_document(self, path: str | None = None) -> None:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Apri progetto", self._last_dir(), "Progetti (*.ppg)")
            if not path:
                return
        try:
            doc = Document.open(path)
        except Exception as exc:
            self._error(f"Impossibile aprire {path}: {exc}")
            return
        self.last_report = None
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
        r = QMessageBox.question(self, "Modifiche non salvate", "Salvare il progetto prima di continuare?",
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                 | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            return self.save_document()
        return r == QMessageBox.StandardButton.Discard

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
        for p in self.settings.value("recent", []) or []:
            if Path(p).exists():
                a = self.recent_menu.addAction(p)
                a.triggered.connect(lambda _=False, pp=p: self.open_document(pp))

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
            self._error(str(exc))
            return
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
            Field("process", "Elabora subito tutto (circa 1-2 minuti)", True)],
            intro="Crea foto sintetiche di un saggio di scavo con 8 target georeferenziati (EPSG:3004), "
                  "mesh di ceramica e un database PyArchInit di prova.")
        if not v:
            return
        from ..demo import create_demo

        def done(d):
            self.open_document(str(d.project))
            self.pyarchinit_connect(config=d.pyarchinit_config)
            QMessageBox.information(self, "Demo pronta",
                                    f"Progetto: {d.project}\nCeramica: {d.vessel.parent}\n"
                                    f"Leggi {d.folder / 'LEGGIMI.txt'} per cosa provare.")

        self.run_task("Creazione dati demo", lambda p: create_demo(v["folder"], process=v["process"], progress=p),
                      done, refresh=False, save=False)

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
            self.run_task("Aggiunta foto", lambda p: self.chunk.add_photos(paths, recursive=folder))

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
            self._error(str(exc))
            return
        self.statusBar().showMessage(f"{n} riferimenti importati", 8000)
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
        self.tabs.setCurrentWidget(self.model_view)

    def _show_raster(self, kind: str) -> None:
        self._load_raster(kind)
        self.tabs.setCurrentWidget(self.ortho_view)

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
        self.tabs.setCurrentWidget(self.photo_view)
        self.statusBar().showMessage(f"{camera.label}: tasto destro per posizionare un marker", 8000)

    def _markers_edited(self) -> None:
        self._dirty(True)
        try:
            self.chunk.triangulate_markers()
        except Exception:
            pass
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

    # ---------------------------------------------------------------- drawing tools
    def set_draw_mode(self, mode: str) -> None:
        if mode != "pan" and self.ortho_view.tr is None:
            self._warn("Costruisci prima l'ortofoto o il DEM.")
            return
        self.ortho_view.set_mode(mode)
        self.tabs.setCurrentWidget(self.ortho_view)
        hints = {"polygon": "Clic per i vertici, doppio clic per chiudere, tasto destro per annullare",
                 "line": "Due clic: inizio e fine della sezione", "measure": "Due clic per misurare",
                 "point": "Clic sinistro: punto positivo, destro: negativo (prompt SAM); quote nel log"}
        self.statusBar().showMessage(hints.get(mode, ""), 15000)

    def clear_drawings(self) -> None:
        self.units = []
        self.sam_points = []
        self.ortho_view.clear_overlays()

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
        self.log_pane.appendPlainText(f"{u.tipo} {u.us}: area {poly.area:.3f} m2")

    def _draw_unit(self, u, i: int) -> None:
        geoms = getattr(u.geometry, "geoms", [u.geometry])
        for g in geoms:
            self.ortho_view.add_polygon(np.asarray(g.exterior.coords)[:, :2], f"{u.tipo} {u.us or '?'}",
                                        US_COLORS[i % len(US_COLORS)])

    def _section_drawn(self, a, b) -> None:
        if not self._need_chunk(product="model"):
            return
        from ..archaeo import sections

        out_dir = (self.doc.path.parent if self.doc.path else Path.home()) / "sezioni"
        n = len(list(out_dir.glob("sezione_*.svg"))) + 1 if out_dir.exists() else 1
        label = f"Sezione {n}"

        def job(p):
            p(0.1, "Taglio del modello")
            s = sections.section(self.chunk, a, b, label=label)
            out_dir.mkdir(parents=True, exist_ok=True)
            svg = s.to_svg(out_dir / f"sezione_{n}.svg")
            s.to_dxf(out_dir / f"sezione_{n}.dxf", world=False)
            s.to_dxf(out_dir / f"sezione_{n}_3d.dxf", world=True)
            return s, svg

        def done(r):
            s, svg = r
            self.ortho_view.add_line([a, b], label=label)
            self._last_section = s
            ImageDialog(label, svg, f"Lunghezza {s.length:.3f} m - DXF in {out_dir}", self).exec()

        self.run_task("Sezione", job, done, refresh=False, save=False)

    def _ortho_point(self, x: float, y: float, label: int) -> None:
        z = self.ortho_view.elevation(x, y)
        self.sam_points.append((x, y, label))
        self.log_pane.appendPlainText(f"Punto {len(self.sam_points)}: X {x:.3f} Y {y:.3f}"
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
            self._error(str(exc))
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
            self._error(str(exc))
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
            for ln in lines:
                self.ortho_view.add_line(ln["coords"], "#ffffff" if ln.get("index") else "#202020")
            self.statusBar().showMessage(f"{len(lines)} curve salvate in {v['path']}", 10000)

        self.run_task("Curve di livello", job, done, refresh=False, save=False)

    def compute_volume(self) -> None:
        if not self._need_chunk(product="dem"):
            return
        polys = [u.geometry for u in self.units]
        v = ParamDialog.ask(self, "Volume", [
            Field("polygon", "Area di calcolo", "last" if polys else "all",
                  choices=([("last", "Ultima unita' disegnata"), ("union", "Unione delle unita' disegnate")]
                           if polys else []) + [("all", "Tutto il DEM")]),
            Field("reference", "Riferimento", "min", choices=[("min", "Quota minima dell'area"),
                                                             ("plane", "Piano a quota data"),
                                                             ("dem", "Altro DEM (prima dello scavo)")]),
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
            txt = (f"Area {r['area_m2']:.3f} m2 - scavo {r['cut_m3']:.3f} m3 - riporto {r['fill_m3']:.3f} m3 - "
                   f"netto {r['net_m3']:.3f} m3 - quote {r['quota_min']:.3f} / {r['quota_max']:.3f}")
            self.log_pane.appendPlainText(txt)
            QMessageBox.information(self, "Volume", txt)

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

        def done(s):
            for pl in s.polylines:
                self.ortho_view.add_line(pl[:, :2], "#ff00ff")
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
            self.log_pane.appendPlainText(f"Profilo ceramico: {txt}")
            if str(out).lower().endswith(".svg"):
                ImageDialog("Profilo ceramico", out, txt, self).exec()
            else:
                QMessageBox.information(self, "Profilo ceramico", f"{txt}\n\nSalvato in {out}")

        self.run_task("Profilo ceramico", job, done, refresh=False, save=False)

    def classify(self) -> None:
        if not self._need_chunk(product="point_cloud"):
            return
        from ..ai.classify import class_statistics, classify_chunk

        def done(pc):
            st = class_statistics(pc)
            self.log_pane.appendPlainText("Classi: " + ", ".join(f"{k}: {n}" for k, n in sorted(st.items())))
            self._show_model("point_cloud")

        self.run_task("Classificazione", lambda p: classify_chunk(self.chunk), done)

    def _segmenter(self):
        if self.sam is None:
            from ..ai.sam import Sam2Segmenter

            self.sam = Sam2Segmenter("small")
        return self.sam

    def _sam_available(self) -> bool:
        try:
            import torch  # noqa: F401

            return True
        except Exception:
            self._warn("Segment Anything 2.1 richiede PyTorch:\npip install \"pyarchinit-photogrammetry[sam]\"")
            return False

    def sam_auto_units(self) -> None:
        if not self._need_chunk(product="orthomosaic") or not self._sam_available():
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
        if not self._need_chunk(product="orthomosaic") or not self._sam_available():
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
        if not self.photo_view.sam_mode and not self._sam_available():
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
        if not self._need_chunk(product="model"):
            return
        path, _ = QFileDialog.getSaveFileName(self, "Esporta IFC", self._last_dir() + "/rilievo.ifc", "IFC (*.ifc)")
        if not path:
            return
        from ..bim.ifc import export_chunk_ifc

        units = [u for u in self.units if u.us] or None
        records = None
        site = None
        if self.pa is not None and units:
            try:
                site = self.pa_pane.site_names()[0]
                records = {str(r.get("us")): r for r in self.pa.stratigraphic_units(site)}
            except Exception:
                records = None
        self.run_task("IFC", lambda p: export_chunk_ifc(self.chunk, path, units=units, us_records=records, site=site),
                      lambda r: self.statusBar().showMessage(f"IFC salvato in {r}", 10000), refresh=False, save=False)

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
            import traceback

            self.console.write(traceback.format_exc())
        self._after_console()
        self.refresh_all()

    # ---------------------------------------------------------------- PyArchInit
    def pyarchinit_connect(self, ask_url: bool = False, config: str | Path | None = None) -> None:
        from ..pyarchinit import PyArchInit

        url = None
        if ask_url:
            url, ok = QInputDialog.getText(self, "Database PyArchInit",
                                           "Percorso SQLite o URL (postgresql://utente@host:5432/db):")
            if not ok or not url:
                return
        elif config is None and not os.environ.get("PYARCHINIT_HOME"):
            from ..pyarchinit.config import pyarchinit_home

            default = pyarchinit_home() / "pyarchinit_DB_folder" / "config.cfg"
            if not default.exists():
                path, _ = QFileDialog.getOpenFileName(self, "config.cfg di PyArchInit", str(Path.home()),
                                                      "config.cfg (config.cfg);;Tutti (*)")
                if not path:
                    return
                config = path
        try:
            self.pa = PyArchInit.connect(url=url, config_path=config)
            label = url or str(config or "config.cfg di PyArchInit")
            self.pa_pane.set_connection(self.pa, label)
            self.d_pa.show()
        except Exception as exc:
            self._error(f"Connessione a PyArchInit non riuscita: {exc}")

    def pyarchinit_demo(self) -> None:
        from ..pyarchinit.demo_db import create_demo_database

        home = Path.home() / "ppg_demo" / "pyarchinit"
        cfg = home / "pyarchinit_DB_folder" / "config.cfg"
        if not cfg.exists():
            try:
                cfg = create_demo_database(home)
            except Exception as exc:
                self._error(str(exc))
                return
        self.pyarchinit_connect(config=cfg)

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
            QMessageBox.information(self, "PyArchInit", "Pubblicato:\n" + json.dumps(out, indent=1, default=str))

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
                      lambda g: QMessageBox.information(self, "PyArchInit",
                                                        f"{len(g)} poligoni scritti in pyunitastratigrafiche"),
                      refresh=False, save=False)

    def pyarchinit_load_units(self) -> None:
        if not self._need_pa():
            return
        sites = self.pa_pane.site_names()
        site, ok = QInputDialog.getItem(self, "Sito", "Sito PyArchInit:", sites, 0, False)
        if not ok:
            return
        try:
            polys = self.pa.us_polygons(site)
        except Exception as exc:
            self._error(str(exc))
            return
        for u in polys:
            self.units.append(u)
            self._draw_unit(u, len(self.units) - 1)
        self.tabs.setCurrentWidget(self.ortho_view)

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
                      lambda g: QMessageBox.information(self, "PyArchInit", f"{len(g)} quote scritte"),
                      refresh=False, save=False)

    # ---------------------------------------------------------------- help
    def about(self) -> None:
        QMessageBox.about(self, "pyarchinit-photogrammetry",
                          f"<b>pyarchinit-photogrammetry {__version__}</b><br>"
                          "Fotogrammetria open source per l'archeologia e il BIM, integrata con PyArchInit.<br><br>"
                          f"Visualizzatore 3D: {self.model_view.backend}<br>"
                          "Licenza GNU GPL v2 o successiva<br>Autore: Enzo Cocca")
