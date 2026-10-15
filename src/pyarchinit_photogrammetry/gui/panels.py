"""Docked panes: workspace, photos, reference, Python console, log, PyArchInit."""
from __future__ import annotations

import code
import contextlib
import io
import logging

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QKeyEvent, QPainter, QPixmap, QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .image_views import to_qimage

PRODUCT_LABELS = {"tie_points": "Punti di legame", "point_cloud": "Nuvola densa", "model": "Modello 3D",
                  "model_textured": "Modello con texture", "dem": "DEM", "orthomosaic": "Ortofoto",
                  "hillshade": "Ombreggiatura", "report": "Report"}


class WorkspaceTree(QTreeWidget):
    """Document > chunks > photos / markers / scale bars / products."""

    activated_item = Signal(str, object, object)  # kind, chunk, payload

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.itemDoubleClicked.connect(self._double)
        self.itemClicked.connect(self._single)
        self.document = None

    def populate(self, document) -> None:
        self.document = document
        self.clear()
        name = document.path.name if document.path else "Progetto senza nome"
        root = QTreeWidgetItem([f"Progetto: {name} ({len(document.chunks)} chunk)"])
        root.setData(0, Qt.ItemDataRole.UserRole, ("document", None, None))
        self.addTopLevelItem(root)
        for ch in document.chunks:
            n_al = len(ch.aligned_cameras)
            active = " [attivo]" if ch is document.chunk else ""
            ci = QTreeWidgetItem([f"{ch.label}{active}  ({len(ch.cameras)} foto, {n_al} allineate)"])
            ci.setData(0, Qt.ItemDataRole.UserRole, ("chunk", ch, None))
            f = ci.font(0)
            f.setBold(ch is document.chunk)
            ci.setFont(0, f)
            root.addChild(ci)
            info = QTreeWidgetItem([f"Sistema di riferimento: {ch.crs}"])
            info.setData(0, Qt.ItemDataRole.UserRole, ("crs", ch, None))
            ci.addChild(info)
            cams = QTreeWidgetItem([f"Camere ({n_al}/{len(ch.cameras)})"])
            cams.setData(0, Qt.ItemDataRole.UserRole, ("cameras", ch, None))
            ci.addChild(cams)
            for cam in ch.cameras:
                it = QTreeWidgetItem([("✔ " if cam.aligned else "✖ ") + cam.label])
                it.setData(0, Qt.ItemDataRole.UserRole, ("camera", ch, cam))
                if not cam.aligned:
                    it.setForeground(0, QColor("#c06060"))
                cams.addChild(it)
            mk = QTreeWidgetItem([f"Marker ({len(ch.markers)})"])
            mk.setData(0, Qt.ItemDataRole.UserRole, ("markers", ch, None))
            ci.addChild(mk)
            for m in ch.markers:
                kind = "check" if m.checkpoint else ("GCP" if m.reference.location else "")
                it = QTreeWidgetItem([f"{m.label}  {kind}  ({len(m.projections)} proiezioni)"])
                it.setData(0, Qt.ItemDataRole.UserRole, ("marker", ch, m))
                mk.addChild(it)
            if ch.scalebars:
                sb = QTreeWidgetItem([f"Barre di scala ({len(ch.scalebars)})"])
                ci.addChild(sb)
                for s in ch.scalebars:
                    sb.addChild(QTreeWidgetItem([f"{s.label}: {s.distance:.4f} m"]))
            for key in ("tie_points", "point_cloud", "model", "model_textured", "dem", "orthomosaic"):
                if ch.product_path(key):
                    it = QTreeWidgetItem([PRODUCT_LABELS[key]])
                    it.setData(0, Qt.ItemDataRole.UserRole, ("product", ch, key))
                    ci.addChild(it)
            ci.setExpanded(ch is document.chunk)
        root.setExpanded(True)

    def _double(self, item, _col) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            self.activated_item.emit(*data)

    def _single(self, item, _col) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data[0] == "chunk":
            self.activated_item.emit("select_chunk", data[1], None)


class PhotosPane(QListWidget):
    """Thumbnails of the active chunk's photos (double click opens the photo)."""

    open_camera = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setIconSize(QSize(150, 112))
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setSpacing(4)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.itemDoubleClicked.connect(lambda it: self.open_camera.emit(it.data(Qt.ItemDataRole.UserRole)))
        self._cache: dict[str, QPixmap] = {}
        self.chunk = None

    def _thumb(self, path: str, aligned: bool) -> QIcon:
        import cv2

        pm = self._cache.get(path)
        if pm is None:
            img = cv2.imread(path, cv2.IMREAD_REDUCED_COLOR_4 | cv2.IMREAD_IGNORE_ORIENTATION)
            if img is None:
                pm = QPixmap(150, 112)
                pm.fill(QColor("#555"))
            else:
                f = 150 / max(img.shape[1], 1)
                img = cv2.resize(img, (150, max(1, int(img.shape[0] * f))), interpolation=cv2.INTER_AREA)
                pm = QPixmap.fromImage(to_qimage(img[:, :, ::-1]))
            self._cache[path] = pm
        out = QPixmap(pm)
        p = QPainter(out)
        p.setPen(QColor("#30e050") if aligned else QColor("#ff5050"))
        f = p.font()
        f.setBold(True)
        f.setPointSize(12)
        p.setFont(f)
        p.drawText(6, 18, "✔" if aligned else "✖")
        p.end()
        return QIcon(out)

    def set_chunk(self, chunk) -> None:
        self.chunk = chunk
        self.clear()
        if chunk is None:
            return
        for cam in chunk.cameras:
            it = QListWidgetItem(self._thumb(cam.path, cam.aligned), cam.label)
            it.setData(Qt.ItemDataRole.UserRole, cam)
            it.setToolTip(f"{cam.path}\n{'allineata' if cam.aligned else 'non allineata'}")
            self.addItem(it)

    def selected_cameras(self) -> list:
        return [it.data(Qt.ItemDataRole.UserRole) for it in self.selectedItems()]


def _fmt(v, nd=4) -> str:
    return "" if v is None else f"{v:.{nd}f}"


class ReferencePane(QWidget):
    """Markers (GCP / check points) with coordinates and errors, camera references, scale bars."""

    import_requested = Signal()
    update_requested = Signal()
    optimize_requested = Signal()
    changed = Signal()

    COLS = ["Usa", "Marker", "Check", "X / Est", "Y / Nord", "Z", "Accur. (m)", "Errore (m)", "dX", "dY", "dZ",
            "Proiez.", "Errore (px)"]

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        bar = QHBoxLayout()
        self.b_import = QPushButton("Importa CSV...")
        self.b_update = QPushButton("Aggiorna")
        self.b_opt = QPushButton("Ottimizza camere")
        bar.addWidget(self.b_import)
        bar.addWidget(self.b_update)
        bar.addWidget(self.b_opt)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.b_import.clicked.connect(self.import_requested)
        self.b_update.clicked.connect(self.update_requested)
        self.b_opt.clicked.connect(self.optimize_requested)
        self.crs_label = QLabel("")
        lay.addWidget(self.crs_label)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(self._edited)
        split.addWidget(self.table)
        self.cams = QTableWidget(0, 7)
        self.cams.setHorizontalHeaderLabels(["Camera", "Allineata", "X", "Y", "Z", "Rif. X", "Rif. Y"])
        self.cams.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.cams.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        split.addWidget(self.cams)
        lay.addWidget(split, 1)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.chunk = None
        self._filling = False
        self.last_report: dict | None = None

    def set_chunk(self, chunk, report: dict | None = None) -> None:
        from ..markers import gcp

        self.chunk = chunk
        if report is not None:
            self.last_report = report
        self._filling = True
        self.table.setRowCount(0)
        self.cams.setRowCount(0)
        if chunk is None:
            self._filling = False
            return
        self.crs_label.setText(f"Sistema di riferimento: <b>{chunk.crs}</b>  -  scala {chunk.scale:.6g}")
        errs = {e["label"]: e for e in gcp.marker_errors(chunk)}
        self.table.setRowCount(len(chunk.markers))
        for r, m in enumerate(chunk.markers):
            e = errs.get(m.label, {})
            loc = m.reference.location or [None, None, None]
            acc = m.reference.accuracy[0] if m.reference.accuracy else None
            use = QTableWidgetItem()
            use.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            use.setCheckState(Qt.CheckState.Checked if m.reference.enabled else Qt.CheckState.Unchecked)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if m.checkpoint else Qt.CheckState.Unchecked)
            vals = [use, QTableWidgetItem(m.label), chk] + [QTableWidgetItem(_fmt(v)) for v in loc] + [
                QTableWidgetItem(_fmt(acc, 4)), QTableWidgetItem(_fmt(e.get("error"), 4)),
                QTableWidgetItem(_fmt(e.get("dx"), 4)), QTableWidgetItem(_fmt(e.get("dy"), 4)),
                QTableWidgetItem(_fmt(e.get("dz"), 4)), QTableWidgetItem(str(e.get("projections", ""))),
                QTableWidgetItem(_fmt(e.get("pixel_error"), 2))]
            for c, it in enumerate(vals):
                if c in (1, 7, 8, 9, 10, 11, 12):
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if c == 7 and e.get("error") is not None and e["error"] > 0.02:
                    it.setForeground(QColor("#ff6060"))
                self.table.setItem(r, c, it)
        self.cams.setRowCount(len(chunk.cameras))
        for r, cam in enumerate(chunk.cameras):
            c = chunk.to_world(cam.center) if cam.aligned else [None] * 3
            ref = cam.reference.location or [None, None, None]
            row = [cam.label, "si" if cam.aligned else "no", _fmt(c[0], 3), _fmt(c[1], 3), _fmt(c[2], 3),
                   _fmt(ref[0], 3), _fmt(ref[1], 3)]
            for k, v in enumerate(row):
                self.cams.setItem(r, k, QTableWidgetItem(v))
        rep = self.last_report or {}
        parts = []
        for key, name in (("control_rmse", "RMSE controllo"), ("check_rmse", "RMSE check")):
            if rep.get(key) is not None:
                parts.append(f"{name}: {rep[key] * 1000:.1f} mm")
        sb = gcp.scalebar_errors(chunk)
        if sb:
            parts.append("barre di scala: " + ", ".join(f"{s['label']} {_fmt(s['error'], 4)} m" for s in sb))
        self.summary.setText("  |  ".join(parts) if parts else "Importa i riferimenti e premi Aggiorna per georeferenziare.")
        self._filling = False

    def _edited(self, item) -> None:
        if self._filling or self.chunk is None:
            return
        r, c = item.row(), item.column()
        m = self.chunk.markers[r]
        if c == 0:
            m.reference.enabled = item.checkState() == Qt.CheckState.Checked
        elif c == 2:
            m.checkpoint = item.checkState() == Qt.CheckState.Checked
        elif c in (3, 4, 5):
            try:
                v = float(item.text().replace(",", "."))
            except ValueError:
                return
            loc = list(m.reference.location or [0.0, 0.0, 0.0])
            loc[c - 3] = v
            m.reference.location = loc
        elif c == 6:
            try:
                a = float(item.text().replace(",", "."))
            except ValueError:
                return
            m.reference.accuracy = [a, a, a]
        self.changed.emit()


class _Stream(io.TextIOBase):
    def __init__(self, write):
        self._write = write

    def write(self, s):
        self._write(s)
        return len(s)


class ConsolePane(QWidget):
    """Python console bound to the open project: ``doc``, ``chunk``, ``app``, ``ppg``."""

    executed = Signal()

    def __init__(self, namespace_factory, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        mono = QFont("Menlo")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.out.setFont(mono)
        self.inp = _HistoryLine()
        self.inp.setFont(mono)
        self.inp.setPlaceholderText("Python: chunk.build_dem(resolution=0.01)   (doc, chunk, app, ppg)")
        self.inp.returnPressed.connect(self._run)
        lay.addWidget(self.out, 1)
        lay.addWidget(self.inp)
        self._factory = namespace_factory
        self.console = code.InteractiveConsole(self._factory())
        self.prompt = ">>> "
        self.write("pyarchinit-photogrammetry - console Python\n"
                   "Variabili: doc (progetto), chunk (chunk attivo), app (ppg.app), ppg (modulo)\n")

    def write(self, s: str) -> None:
        self.out.moveCursor(QTextCursor.MoveOperation.End)
        self.out.insertPlainText(s)
        self.out.moveCursor(QTextCursor.MoveOperation.End)

    def run_source(self, src: str) -> bool:
        """Run one line (as typed); returns True when more input is needed."""
        self.console.locals.update({k: v for k, v in self._factory().items() if k in ("doc", "chunk", "app", "ppg")})
        stream = _Stream(self.write)
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            more = self.console.push(src)
        return more

    def _run(self) -> None:
        src = self.inp.text()
        self.inp.add_history(src)
        self.inp.clear()
        self.write(f"{self.prompt}{src}\n")
        more = self.run_source(src)
        self.prompt = "... " if more else ">>> "
        self.executed.emit()


class _HistoryLine(QLineEdit):
    def __init__(self):
        super().__init__()
        self.history: list[str] = []
        self.pos = 0

    def add_history(self, s: str) -> None:
        if s.strip():
            self.history.append(s)
        self.pos = len(self.history)

    def keyPressEvent(self, ev: QKeyEvent) -> None:
        if ev.key() == Qt.Key.Key_Up and self.history:
            self.pos = max(0, self.pos - 1)
            self.setText(self.history[self.pos])
        elif ev.key() == Qt.Key.Key_Down and self.history:
            self.pos = min(len(self.history), self.pos + 1)
            self.setText(self.history[self.pos] if self.pos < len(self.history) else "")
        else:
            super().keyPressEvent(ev)


class _LogBridge(QObject):
    message = Signal(str)


class LogHandler(logging.Handler):
    """Forwards log records (from any thread) to the log pane."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.bridge = _LogBridge()
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record) -> None:
        try:
            self.bridge.message.emit(self.format(record))
        except RuntimeError:
            pass


class LogPane(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.handler = LogHandler()
        self.handler.bridge.message.connect(self.appendPlainText)
        logging.getLogger("pyarchinit_photogrammetry").addHandler(self.handler)
        logging.getLogger("pyarchinit_photogrammetry").setLevel(logging.INFO)

    def detach(self) -> None:
        logging.getLogger("pyarchinit_photogrammetry").removeHandler(self.handler)


class PyArchInitPane(QWidget):
    """Sites, stratigraphic units and documentation of the connected PyArchInit database."""

    connect_requested = Signal()
    demo_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        row = QHBoxLayout()
        self.b_connect = QPushButton("Connetti...")
        self.b_demo = QPushButton("Database demo")
        row.addWidget(self.b_connect)
        row.addWidget(self.b_demo)
        lay.addLayout(row)
        self.b_connect.clicked.connect(self.connect_requested)
        self.b_demo.clicked.connect(self.demo_requested)
        self.status = QLabel("Non connesso")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Elemento", "Descrizione"])
        lay.addWidget(self.tree, 1)
        self.pa = None

    def set_connection(self, pa, label: str) -> None:
        self.pa = pa
        self.status.setText(f"Connesso: {label}")
        self.refresh()

    def refresh(self) -> None:
        self.tree.clear()
        if self.pa is None:
            return
        try:
            sites = self.pa.sites()
        except Exception as exc:
            self.status.setText(f"Errore: {exc}")
            return
        for s in sites:
            name = s.get("sito")
            si = QTreeWidgetItem([str(name), str(s.get("comune") or "")])
            self.tree.addTopLevelItem(si)
            us_root = QTreeWidgetItem(["Unita' stratigrafiche", ""])
            si.addChild(us_root)
            try:
                for u in self.pa.stratigraphic_units(name):
                    us_root.addChild(QTreeWidgetItem([f"{u.get('unita_tipo') or 'US'} {u.get('us')} (area {u.get('area')})",
                                                      str(u.get("d_stratigrafica") or "")]))
                doc_root = QTreeWidgetItem(["Documentazione", ""])
                si.addChild(doc_root)
                for d in self.pa.documentation(name):
                    doc_root.addChild(QTreeWidgetItem([str(d.get("nome_doc")), str(d.get("tipo_documentazione") or "")]))
            except Exception as exc:
                us_root.addChild(QTreeWidgetItem([f"errore: {exc}", ""]))
            si.setExpanded(True)
            us_root.setExpanded(True)
        self.tree.resizeColumnToContents(0)

    def site_names(self) -> list[str]:
        if self.pa is None:
            return []
        return [str(s.get("sito")) for s in self.pa.sites()]
