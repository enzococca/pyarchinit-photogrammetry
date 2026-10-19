"""Docked panes: workspace, photos, reference, Python console, log, PyArchInit."""
from __future__ import annotations

import code
import contextlib
import io
import logging
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QImage, QKeyEvent, QPainter, QPixmap, QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSizePolicy,
                               QSplitter, QTableWidget, QTableWidgetItem, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from .flow_layout import FlowLayout
from .i18n import decimal_comma, number, tr, translate
from .icons import PALETTE, icon
from .image_views import to_qimage

PRODUCT_LABELS = {"tie_points": "Punti di legame", "point_cloud": "Nuvola densa", "model": "Modello 3D",
                  "model_textured": "Modello con texture", "dem": "DEM", "orthomosaic": "Ortofoto",
                  "hillshade": "Ombreggiatura", "report": "Report"}


_SUMMARIES: dict = {}


def _precision(sigma) -> str:
    """Standard deviations in metres along X, Y, Z as millimetres ("0.7 / 0.7 / 1.6")."""
    if not sigma:
        return ""
    return " / ".join(f"{v * 1000:.1f}" if v < 0.1 else f"{v * 1000:.0f}" for v in sigma)


def _it(value: float, decimals: int = 0) -> str:
    return number(value, decimals)


def product_summary(key: str, path) -> str:
    """How large a product is, as Metashape shows it in the workspace: points, faces, pixels
    (read from the headers of the files only, and remembered until the file changes)."""
    from pathlib import Path

    if path is None:
        return ""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return ""
    cache_key = (str(path), st.st_mtime_ns, st.st_size, key)
    if cache_key in _SUMMARIES:
        return _SUMMARIES[cache_key]
    text = ""
    try:
        if key in ("tie_points", "point_cloud"):
            if path.suffix.lower() in (".las", ".laz"):
                import laspy

                with laspy.open(str(path)) as f:
                    n = f.header.point_count
            else:
                from ..io.mesh_ply import read_header

                with open(path, "rb") as f:
                    n = next(e["count"] for e in read_header(f)[1] if e["name"] == "vertex")
            text = tr("{0} punti", _it(n))
        elif key == "model":
            from ..io.mesh_ply import face_count

            n = face_count(path)
            text = tr("{0} facce", _it(n)) if n is not None else ""
        elif key == "model_textured":
            from PIL import Image

            image = path.parent / "texture.jpg" if path.suffix.lower() == ".obj" else path
            with Image.open(image) as im:
                text = f"texture {_it(im.size[0])} x {_it(im.size[1])} px"
        elif key in ("dem", "orthomosaic"):
            import rasterio

            with rasterio.open(path) as src:
                res = abs(src.res[0])
                size = f"{_it(res * 100, 2)} cm/px" if res < 1 else f"{_it(res, 2)} m/px"
                text = f"{_it(src.width)} x {_it(src.height)} px, {size}"
    except Exception:
        text = ""
    _SUMMARIES[cache_key] = text
    return text


def _tool(name: str, text: str, tip: str = "") -> QToolButton:
    b = QToolButton()
    b.setIcon(icon(name))
    b.setText(text)
    b.setToolTip(tip or text)
    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    b.setAutoRaise(True)
    return b


class WorkspaceTree(QTreeWidget):
    """Document > chunks > photos / markers / scale bars / products."""

    activated_item = Signal(str, object, object)  # kind, chunk, payload
    context_requested = Signal(str, object, object, object)  # kind, chunk, payload, global position

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setIconSize(QSize(18, 18))
        self.setIndentation(16)
        self.itemDoubleClicked.connect(self._double)
        self.itemClicked.connect(self._single)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context)
        self.document = None

    def populate(self, document) -> None:
        self.document = document
        self.clear()
        name = document.path.name if document.path else tr("Progetto senza nome")
        root = QTreeWidgetItem([tr("{0}  ({1} chunk)", name, len(document.chunks))])
        root.setIcon(0, icon("project"))
        root.setData(0, Qt.ItemDataRole.UserRole, ("document", None, None))
        self.addTopLevelItem(root)
        for ch in document.chunks:
            n_al = len(ch.aligned_cameras)
            active = tr("  [attivo]") if ch is document.chunk else ""
            ci = QTreeWidgetItem([tr("{0}{1}   {2} foto, {3} allineate", ch.label, active, len(ch.cameras), n_al)])
            ci.setIcon(0, icon("chunk", "blue" if ch is document.chunk else None))
            ci.setData(0, Qt.ItemDataRole.UserRole, ("chunk", ch, None))
            f = ci.font(0)
            f.setBold(ch is document.chunk)
            ci.setFont(0, f)
            root.addChild(ci)
            info = QTreeWidgetItem([tr("Sistema di riferimento: {0}", ch.crs)])
            info.setIcon(0, icon("crs"))
            info.setData(0, Qt.ItemDataRole.UserRole, ("crs", ch, None))
            info.setToolTip(0, tr("Doppio clic per cambiare il sistema di riferimento"))
            ci.addChild(info)
            cams = QTreeWidgetItem([tr("Camere  ({0}/{1} allineate)", n_al, len(ch.cameras))])
            cams.setIcon(0, icon("cameras"))
            cams.setData(0, Qt.ItemDataRole.UserRole, ("cameras", ch, None))
            ci.addChild(cams)
            for cam in ch.cameras:
                it = QTreeWidgetItem([cam.label])
                it.setIcon(0, icon("camera_ok" if cam.aligned else "camera_bad"))
                it.setData(0, Qt.ItemDataRole.UserRole, ("camera", ch, cam))
                if not cam.aligned:
                    it.setForeground(0, QColor(PALETTE["red"]))
                    it.setToolTip(0, tr("Non allineata"))
                cams.addChild(it)
            mk = QTreeWidgetItem([f"Marker  ({len(ch.markers)})"])
            mk.setIcon(0, icon("marker"))
            mk.setData(0, Qt.ItemDataRole.UserRole, ("markers", ch, None))
            ci.addChild(mk)
            for m in ch.markers:
                kind = "check" if m.checkpoint else ("GCP" if m.reference.location else "")
                it = QTreeWidgetItem([tr("{0}   {1}   {2} proiezioni", m.label, kind,
                                         len(m.projections)).replace("    ", "  ")])
                it.setIcon(0, icon("checkpoint" if m.checkpoint else ("gcp" if m.reference.location else "marker")))
                it.setData(0, Qt.ItemDataRole.UserRole, ("marker", ch, m))
                mk.addChild(it)
            if ch.scalebars:
                sb = QTreeWidgetItem([tr("Barre di scala  ({0})", len(ch.scalebars))])
                sb.setIcon(0, icon("scalebar"))
                ci.addChild(sb)
                for s in ch.scalebars:
                    sb.addChild(QTreeWidgetItem([f"{s.label}: {s.distance:.4f} m" + ("  check" if s.checkpoint else "")
                                                 + ("" if s.enabled else tr("  (non usata)"))]))
            from ..core import versions

            for key in ("tie_points", "point_cloud", "model", "model_textured", "dem", "orthomosaic"):
                kept = versions.versions(ch, key) if key in versions.PRODUCTS else []
                if not ch.product_path(key) and not kept:
                    continue
                label = tr(PRODUCT_LABELS[key])
                size = product_summary(key, ch.product_path(key))
                if size:
                    label += f"  ({size})"
                named = key in versions.PRODUCTS and (kept or (ch.meta.get("active_versions") or {}).get(key))
                if named:
                    label += (f"  -  {versions.active_info(ch, key)['name']}" if ch.product_path(key)
                              else tr("  -  nessuna versione attiva"))
                it = QTreeWidgetItem([label])
                it.setIcon(0, icon(key))
                it.setData(0, Qt.ItemDataRole.UserRole, ("product", ch, key))
                if key in versions.PRODUCTS:
                    it.setToolTip(0, tr("Doppio clic per vederlo; tasto destro: versioni (conserva, rinomina...)"))
                ci.addChild(it)
                for e in reversed(kept):
                    when = e["created"][:16].replace("T", " ")
                    try:
                        size = product_summary(key, versions.version_file(ch, key, e["id"]))
                    except Exception:
                        size = ""
                    vi = QTreeWidgetItem([f"{e['name']}   ({size + ', ' if size else ''}{when})"])
                    vi.setIcon(0, icon("version"))
                    vi.setData(0, Qt.ItemDataRole.UserRole, ("version", ch, (key, e["id"])))
                    vi.setToolTip(0, tr("Versione conservata: doppio clic per vederla; tasto destro per renderla "
                                        "attiva, confrontarla, rinominarla o eliminarla"))
                    it.addChild(vi)
                it.setExpanded(bool(kept))
            for key, text, tip in (("slope", "Pendenza", "gradi"), ("aspect", "Esposizione", "gradi dal nord"),
                                   ("svf", "Sky-view factor", "SVF"), ("openness_pos", "Openness positiva", "gradi"),
                                   ("openness_neg", "Openness negativa", "gradi"),
                                   ("slrm", "Rilievo locale (SLRM)", "cm"), ("msrm", "Rilievo multiscala (MSRM)", "cm"),
                                   ("multi_hillshade", "Ombreggiatura da 16 direzioni", "0-1")):
                path = ch.product_path(key)
                if path is not None and path.exists():
                    it = QTreeWidgetItem([tr(text)])
                    it.setIcon(0, icon(key))
                    it.setData(0, Qt.ItemDataRole.UserRole, ("product", ch, key))
                    it.setToolTip(0, tr("Doppio clic per vederla ({0} sotto il cursore)", tr(tip)))
                    ci.addChild(it)
            cov = ch.product_path("coverage")
            if cov is not None and cov.exists():
                it = QTreeWidgetItem([tr("Copertura delle foto")])
                it.setIcon(0, icon("fieldcheck"))
                it.setData(0, Qt.ItemDataRole.UserRole, ("product", ch, "coverage"))
                it.setToolTip(0, tr("Doppio clic per vederla: quante foto inquadrano ogni punto (rosso: meno di tre)"))
                ci.addChild(it)
            prec = ch.product_path("dem_precision")
            if prec is not None and prec.exists():
                it = QTreeWidgetItem([tr("Precisione del DEM (sigma Z)")])
                it.setIcon(0, icon("precision"))
                it.setData(0, Qt.ItemDataRole.UserRole, ("product", ch, "dem_precision"))
                it.setToolTip(0, tr("Doppio clic per vederla: deviazione standard delle quote, dai punti di legame"))
                ci.addChild(it)
            elevations = (ch.meta or {}).get("elevations") or []
            if elevations:
                el = QTreeWidgetItem([tr("Prospetti  ({0})", len(elevations))])
                el.setIcon(0, icon("elevation"))
                ci.addChild(el)
                for e in elevations:
                    w, h = e.get("size", [0, 0])
                    it = QTreeWidgetItem([f"{e['name']}  ({w} x {h} px, {e.get('resolution', 0) * 100:.2f} cm/px)"])
                    it.setIcon(0, icon("elevation"))
                    it.setData(0, Qt.ItemDataRole.UserRole, ("elevation", ch, e))
                    it.setToolTip(0, tr("Doppio clic per vederlo; tasto destro: esporta, elimina"))
                    el.addChild(it)
                el.setExpanded(True)
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

    def _context(self, pos) -> None:
        item = self.itemAt(pos)
        data = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if data:
            self.context_requested.emit(*data, self.viewport().mapToGlobal(pos))


def read_thumbnail(path: str, width: int = 150) -> QImage:
    """Small image of a photo: the preview the camera stores in the EXIF (a few milliseconds,
    also for a large drone photo on a slow disk), else the photo decoded at 1/8 of its size.
    Safe in a worker thread (QImage, not QPixmap); a null image when the file cannot be read."""
    import io

    import numpy as np

    try:
        from PIL import ExifTags, Image

        with Image.open(path) as im:
            ifd1 = im.getexif().get_ifd(ExifTags.IFD.IFD1)
            raw = im.info.get("exif", b"")
        off, n = ifd1.get(0x0201), ifd1.get(0x0202)  # JPEGInterchangeFormat(Length)
        if off and n and raw[:6] == b"Exif\x00\x00":
            with Image.open(io.BytesIO(raw[6 + off:6 + off + n])) as th:
                return to_qimage(np.asarray(th.convert("RGB")))
    except Exception:
        pass
    import cv2

    img = cv2.imread(path, cv2.IMREAD_REDUCED_COLOR_8 | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        return QImage()
    f = width / max(img.shape[1], 1)
    img = cv2.resize(img, (width, max(1, int(img.shape[0] * f))), interpolation=cv2.INTER_AREA)
    return to_qimage(img[:, :, ::-1])


class _ThumbLoader(QThread):
    """Reads the thumbnails of a list of photos in the background."""

    loaded = Signal(int, str, QImage)  # generation, path, image (null: unreadable)

    def __init__(self, generation: int, paths: list[str], parent=None):
        super().__init__(parent)
        self.generation, self.paths = generation, paths

    def run(self) -> None:
        for path in self.paths:
            if self.isInterruptionRequested():
                return
            try:
                img = read_thumbnail(path)
            except Exception:
                img = QImage()
            self.loaded.emit(self.generation, path, img)


class PhotosPane(QListWidget):
    """Thumbnails of the active chunk's photos (double click opens the photo). They are read in
    the background (hundreds of drone photos took minutes, with the window frozen)."""

    open_camera = Signal(object)
    thumbs_progress = Signal(int, int)  # read, total
    filter_changed = Signal(str)  # what the photos shown are ("" = every photo)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setIconSize(QSize(150, 112))
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setSpacing(6)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.itemDoubleClicked.connect(lambda it: self.open_camera.emit(it.data(Qt.ItemDataRole.UserRole)))
        self._cache: dict[str, QPixmap] = {}
        self._items: dict[str, list[QListWidgetItem]] = {}
        self._generation = 0
        self._loader: _ThumbLoader | None = None
        self._stale: list[_ThumbLoader] = []
        self._done = self._total = 0
        self.chunk = None
        self._flags: dict[int, tuple[int, int, list]] = {}  # camera key -> (measured, estimated, labels)
        self._filter: set | None = None
        self.filter_title = ""
        self.focus = None  # the marker (or internal point) the filtered photos are about

    @staticmethod
    def _placeholder(broken: bool = False) -> QPixmap:
        pm = QPixmap(150, 112)
        pm.fill(QColor("#6b5050" if broken else "#555"))
        return pm

    @staticmethod
    def _icon(pm: QPixmap, aligned: bool, flags: tuple = (0, 0)) -> QIcon:
        """The thumbnail with the aligned badge and, at the bottom, the markers on the photo:
        green = measured, blue = estimated and to be confirmed (as Metashape's flags)."""
        out = QPixmap(pm)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        col = QColor(PALETTE["green"] if aligned else PALETTE["red"])
        p.setBrush(QBrush(col))
        p.setPen(QColor("white"))
        p.drawEllipse(5, 5, 18, 18)
        f = p.font()
        f.setBold(True)
        f.setPointSize(11)
        p.setFont(f)
        p.drawText(5, 5, 18, 18, Qt.AlignmentFlag.AlignCenter, "✓" if aligned else "✕")
        x = 5
        for count, colour in ((flags[0], "#30a050"), (flags[1], "#2f7fd8")):
            if count:
                text = f"⚑ {count}"
                w = 14 + 8 * len(str(count))
                p.setBrush(QBrush(QColor(colour)))
                p.setPen(QColor("white"))
                p.drawRoundedRect(x, out.height() - 23, w, 18, 4, 4)
                p.drawText(x, out.height() - 23, w, 18, Qt.AlignmentFlag.AlignCenter, text)
                x += w + 4
        p.end()
        return QIcon(out)

    def _marker_flags(self) -> dict:
        flags: dict[int, list] = {}
        for m in getattr(self.chunk, "markers", None) or []:
            for k, pr in m.projections.items():
                f = flags.setdefault(k, [0, 0, []])
                f[0 if pr.measured else 1] += 1
                f[2].append(m.label + ("" if pr.measured else tr(" (da confermare)")))
        return {k: tuple(v) for k, v in flags.items()}

    def _tooltip(self, cam) -> str:
        f = self._flags.get(getattr(cam, "key", None))
        text = f"{cam.path}\n{'allineata' if cam.aligned else 'non allineata'}"
        if not getattr(cam, "enabled", True):
            text += " (disattivata)"
        meta = getattr(cam, "meta", None) or {}
        if meta.get("panorama"):
            text += tr("\nFaccia «{0}» della foto 360° {1}", meta.get('face'), meta['panorama'])
        if meta.get("precision"):
            text += tr("\nPrecisione della posizione X/Y/Z: {0} mm", _precision(meta['precision']))
        q = meta.get("quality")
        if q is not None:
            text += decimal_comma(tr("\nQualita' dell'immagine {0:.2f}", q)) + (tr(" (sfocata)") if q < 0.5 else "")
        return text + (f"\nMarker: {', '.join(f[2])}" if f else "")

    def update_flags(self) -> None:
        """Draw again the marker badges (after markers were placed, moved or removed)."""
        self._flags = self._marker_flags()
        for i in range(self.count()):
            it = self.item(i)
            cam = it.data(Qt.ItemDataRole.UserRole)
            pm = self._cache.get(cam.path)
            f = self._flags.get(getattr(cam, "key", None), (0, 0, []))
            it.setIcon(self._icon(pm if pm is not None else self._placeholder(), cam.aligned, f[:2]))
            it.setToolTip(self._tooltip(cam))

    def set_filter(self, keys=None, title: str = "", focus=None) -> int:
        """Show only the photos with these camera keys (None: every photo), as Metashape's
        *Filter Photos*; ``focus`` (a marker or an internal point) is what opening them centres
        on. Returns how many photos are shown."""
        self._filter = None if keys is None else set(keys)
        self.filter_title, self.focus = (title, focus) if keys is not None else ("", None)
        shown = 0
        for i in range(self.count()):
            it = self.item(i)
            hide = self._filter is not None and getattr(it.data(Qt.ItemDataRole.UserRole), "key",
                                                        None) not in self._filter
            it.setHidden(hide)
            shown += not hide
        self.filter_changed.emit(self.filter_title)
        return shown

    def visible_cameras(self) -> list:
        """The cameras of the photos shown, in their order (the list Page Up/Down goes through)."""
        return [self.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.count()) if not self.item(i).isHidden()]

    def select_camera(self, camera) -> None:
        for i in range(self.count()):
            it = self.item(i)
            if it.data(Qt.ItemDataRole.UserRole) is camera:
                self.setCurrentItem(it)
                self.scrollToItem(it)
                return

    def set_chunk(self, chunk) -> None:
        self.stop()
        self._generation += 1
        self.chunk = chunk
        self.clear()
        self._items = {}
        if chunk is None:
            return
        todo = []
        self._flags = self._marker_flags()
        self._filter, self.filter_title, self.focus = None, "", None
        self.filter_changed.emit("")
        for cam in chunk.cameras:
            pm = self._cache.get(cam.path)
            f = self._flags.get(getattr(cam, "key", None), (0, 0, []))
            it = QListWidgetItem(self._icon(pm if pm is not None else self._placeholder(), cam.aligned, f[:2]),
                                 cam.label)
            it.setData(Qt.ItemDataRole.UserRole, cam)
            it.setToolTip(self._tooltip(cam))
            self.addItem(it)
            self._items.setdefault(cam.path, []).append(it)
            if pm is None and cam.path not in todo:
                todo.append(cam.path)
        self._done, self._total = 0, len(todo)
        if todo:
            self._loader = _ThumbLoader(self._generation, todo, self)
            self._loader.loaded.connect(self._loaded)
            self._loader.start()

    def _loaded(self, generation: int, path: str, img: QImage) -> None:
        if generation != self._generation:
            return
        if img.isNull():
            pm = self._placeholder(broken=True)
        else:
            pm = QPixmap.fromImage(img.scaledToWidth(150, Qt.TransformationMode.SmoothTransformation)
                                   if img.width() != 150 else img)
        self._cache[path] = pm
        for it in self._items.get(path, []):
            cam = it.data(Qt.ItemDataRole.UserRole)
            it.setIcon(self._icon(pm, cam.aligned, self._flags.get(getattr(cam, "key", None), (0, 0, []))[:2]))
            if img.isNull():
                it.setToolTip(tr("{0}\nfoto non leggibile (file vuoto o mancante)", cam.path))
        self._done += 1
        self.thumbs_progress.emit(self._done, self._total)

    def stop(self) -> None:
        """Stop reading thumbnails (another chunk, the window closing)."""
        if self._loader is not None:
            self._loader.requestInterruption()
            if not self._loader.wait(3000):  # a photo on a slow disk: let it finish on its own
                self._stale.append(self._loader)
            self._loader = None
        self._stale = [t for t in self._stale if t.isRunning()]

    def selected_cameras(self) -> list:
        return [it.data(Qt.ItemDataRole.UserRole) for it in self.selectedItems()]


class PhotosPanel(QWidget):
    """The thumbnails with a bar: the filter in use (by marker, point or area, as Metashape's
    *Filter Photos*), *Mostra tutte*, *Foto con marker* and the previous/next photo."""

    step = Signal(int)  # -1 previous photo of the list, +1 next
    with_markers = Signal()

    def __init__(self, pane: PhotosPane, parent=None):
        super().__init__(parent)
        self.pane = pane
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        bar = FlowLayout()
        self.label = QLabel(tr("Tutte le foto"))
        self.b_all = _tool("photos", tr("Mostra tutte"), tr("Togli il filtro: tutte le foto del chunk"))
        self.b_markers = _tool("marker", tr("Foto con marker"), tr("Solo le foto con almeno un marker (bandierina "
                                                                   "verde: misurato, blu: da confermare)"))
        self.b_prev = _tool("previous", tr("Precedente"), tr("Apri la foto precedente dell'elenco (Pagina su nella "
                                                             "foto)"))
        self.b_next = _tool("next", tr("Successiva"),
                            tr("Apri la foto successiva dell'elenco (Pagina giu' nella foto)"))
        for w in (self.label, self.b_all, self.b_markers, self.b_prev, self.b_next):
            bar.addWidget(w)
        lay.addLayout(bar)
        lay.addWidget(pane, 1)
        self.b_all.clicked.connect(lambda: pane.set_filter(None))
        self.b_markers.clicked.connect(self.with_markers)
        self.b_prev.clicked.connect(lambda: self.step.emit(-1))
        self.b_next.clicked.connect(lambda: self.step.emit(1))
        pane.filter_changed.connect(self._filter_changed)
        self._filter_changed("")

    def _filter_changed(self, title: str) -> None:
        n = len(self.pane.visible_cameras())
        self.label.setText(tr("<b>{0}</b> ({1} foto)", title, n) if title else tr("Tutte le foto ({0})", n))
        self.b_all.setEnabled(bool(title))


def _fmt(v, nd=4) -> str:
    return "" if v is None else f"{v:.{nd}f}"


class ReferencePane(QWidget):
    """Markers (GCP / check points) with coordinates and errors, scale bars, camera references.

    Scale bars as in Metashape: select two markers, right click *Crea barra di scala*, type the
    distance; *Aggiorna* scales the survey (in local coordinates without control points) and
    shows the error of every bar. A check bar only measures."""

    import_requested = Signal()
    update_requested = Signal()
    optimize_requested = Signal()
    changed = Signal()
    scalebar_requested = Signal(object)  # keys of the markers chosen (0, 1 or 2)
    filter_photos_requested = Signal(object)  # keys of the markers whose photos are to be shown
    refine_requested = Signal(object)  # keys of the markers whose blue flags are to be looked for (none: all)
    precision_requested = Signal()
    merge_requested = Signal(object)  # keys of the markers to make one
    move3d_requested = Signal(int)  # key of the marker to place again in the 3D view
    remove_requested = Signal(object)  # keys of the markers to delete
    scalebar_import_requested = Signal()

    BAR_COLS = ["Usa", "Barra", "Marker A", "Marker B", "Distanza (m)", "Accur. (m)", "Check", "Stimata (m)",
                "Errore (m)"]

    COLS = ["Usa", "Marker", "Check", "X / Est", "Y / Nord", "Z", "Accur. (m)", "Errore (m)", "dX", "dY", "dZ",
            "Proiez.", "Errore (px)", "Precisione X/Y/Z (mm)"]

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        bar = FlowLayout()
        self.b_import = _tool("reference", tr("Importa CSV..."), tr("Importa le coordinate dei marker"))
        self.b_update = _tool("georef", tr("Aggiorna"), tr("Ricalcola la georeferenziazione"))
        self.b_opt = _tool("optimize", tr("Ottimizza camere"), tr("Bundle adjustment con i riferimenti"))
        self.b_bar = _tool("scalebar", tr("Barra di scala..."), tr("Distanza nota tra due marker: scala il rilievo "
                                                                   "(anche in coordinate locali, come per gli "
                                                                   "oggetti)"))
        self.b_bar_import = _tool("scalebar", tr("Importa barre..."), tr("Barre di scala da un CSV (marker A, marker "
                                                                         "B, distanza in metri)"))
        self.b_est = _tool("markers", tr("Coordinate stimate"), tr("X, Y, Z dei marker stimate dal rilievo "
                                                                   "(triangolate dalle foto o, con una foto sola, "
                                                                   "sulla superficie) al posto di quelle di "
                                                                   "riferimento, come 'View Estimated' di Metashape"))
        self.b_est.setCheckable(True)
        self._est_chosen = False  # chosen by hand: no longer follows the project
        self.b_est.toggled.connect(self._estimated_toggled)
        self.b_prec = _tool("precision", tr("Precisione"), tr("Precisione di camere, marker e punti di legame dalla "
                                                              "covarianza del bundle adjustment, e mappa della "
                                                              "precisione del DEM"))
        bar.addWidget(self.b_import)
        bar.addWidget(self.b_update)
        bar.addWidget(self.b_opt)
        bar.addWidget(self.b_prec)
        bar.addWidget(self.b_bar)
        bar.addWidget(self.b_bar_import)
        bar.addWidget(self.b_est)
        lay.addLayout(bar)
        self.b_import.clicked.connect(self.import_requested)
        self.b_update.clicked.connect(self.update_requested)
        self.b_opt.clicked.connect(self.optimize_requested)
        self.b_prec.clicked.connect(self.precision_requested)
        self.b_bar.clicked.connect(lambda: self.scalebar_requested.emit(self.selected_markers()))
        self.b_bar_import.clicked.connect(self.scalebar_import_requested)
        self.crs_label = QLabel("")
        self.crs_label.setWordWrap(True)
        lay.addWidget(self.crs_label)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.itemChanged.connect(self._edited)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._marker_menu)
        split.addWidget(self.table)
        self.bars = QTableWidget(0, len(self.BAR_COLS))
        self.bars.setHorizontalHeaderLabels(self.BAR_COLS)
        self.bars.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.bars.setAlternatingRowColors(True)
        self.bars.verticalHeader().setVisible(False)
        self.bars.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.bars.itemChanged.connect(self._bar_edited)
        self.bars.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.bars.customContextMenuRequested.connect(self._bar_menu)
        split.addWidget(self.bars)
        self.cams = QTableWidget(0, 7)
        self.cams.setHorizontalHeaderLabels(["Camera", tr("Allineata"), "X", "Y", "Z", tr("Rif. X"), tr("Rif. Y"),
                                             tr("Precisione X/Y/Z (mm)")])
        self.cams.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.cams.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cams.setAlternatingRowColors(True)
        self.cams.verticalHeader().setVisible(False)
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
        self.bars.setRowCount(0)
        if chunk is None:
            self._filling = False
            return
        self.crs_label.setText(tr("Sistema di riferimento: <b>{0}</b>  -  scala {1:.6g}", chunk.crs, chunk.scale))
        errs = {e["label"]: e for e in gcp.marker_errors(chunk)}
        if not self._est_chosen:  # no reference coordinates: the estimated ones are what there is to see
            self.b_est.blockSignals(True)
            self.b_est.setChecked(not any(m.reference.location for m in chunk.markers)
                                  and any(m.position is not None for m in chunk.markers))
            self.b_est.blockSignals(False)
        estimated = self.b_est.isChecked()
        precision = (chunk.meta.get("precision") or {}).get("markers", {})
        names = ["X stimata", "Y stimata", "Z stimata"] if estimated else ["X / Est", "Y / Nord", "Z"]
        for c, name in enumerate(names, 3):
            self.table.setHorizontalHeaderItem(c, QTableWidgetItem(tr(name)))
        self.table.setRowCount(len(chunk.markers))
        one_photo = 0
        for r, m in enumerate(chunk.markers):
            e = errs.get(m.label, {})
            loc = m.reference.location or [None, None, None]
            if estimated:
                loc = chunk.to_world(m.position[None])[0] if m.position is not None else [None, None, None]
            views = gcp.views_of(chunk, m)
            one_photo += views == 1
            todo = gcp.views_of(chunk, m, estimated=True)
            how = (tr("triangolato da {0} foto", views) if views >= 2 else
                   tr("su una foto sola: posizione dove il suo raggio incontra la superficie (nuvola densa o "
                      "punti di legame); mettilo su una seconda foto per triangolarlo") if views == 1 else
                   tr("messo sul modello, sulla nuvola o sull'ortofoto") if m.position is not None else
                   tr("non ancora posizionato: tasto destro su una foto, oppure strumento Marker sul modello o "
                      "sull'ortofoto"))
            if todo:
                how += (tr("; +{0}: bandierine blu da confermare in altrettante foto (tasto destro > Filtra le foto, "
                           "poi trascinale sul punto esatto)", todo))
            acc = m.reference.accuracy[0] if m.reference.accuracy else None
            use = QTableWidgetItem()
            use.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            use.setCheckState(Qt.CheckState.Checked if m.reference.enabled else Qt.CheckState.Unchecked)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if m.checkpoint else Qt.CheckState.Unchecked)
            label = QTableWidgetItem(m.label)
            label.setIcon(icon("checkpoint" if m.checkpoint else ("gcp" if m.reference.location else "marker")))
            vals = [use, label, chk] + [QTableWidgetItem(_fmt(v)) for v in loc] + [
                QTableWidgetItem(_fmt(acc, 4)), QTableWidgetItem(_fmt(e.get("error"), 4)),
                QTableWidgetItem(_fmt(e.get("dx"), 4)), QTableWidgetItem(_fmt(e.get("dy"), 4)),
                QTableWidgetItem(_fmt(e.get("dz"), 4)),
                QTableWidgetItem(f"{e.get('projections', '')}" + (f" (+{e['estimated']})" if e.get("estimated") else "")),
                QTableWidgetItem(_fmt(e.get("pixel_error"), 2)),
                QTableWidgetItem(_precision(precision.get(str(m.key))))]
            for c, it in enumerate(vals):
                if c == 13 and precision.get(str(m.key)):
                    it.setToolTip(tr("Precisione stimata della posizione dalle foto (deviazione standard): misura "
                                     "nelle "
                                     "foto, incertezza delle camere e dei riferimenti"))
                if c in (1, 7, 8, 9, 10, 11, 12, 13) or (estimated and c in (3, 4, 5)):
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if estimated and c in (3, 4, 5):
                    it.setForeground(QColor(PALETTE["grey"]))
                    font = it.font()
                    font.setItalic(True)
                    it.setFont(font)
                    it.setToolTip(tr("Coordinata stimata: {0}", how))
                if c in (1, 11):
                    it.setToolTip(how)
                if c == 7 and e.get("error") is not None and e["error"] > 0.02:
                    it.setForeground(QColor(PALETTE["red"]))
                self.table.setItem(r, c, it)
        bar_errors = {e["label"]: e for e in gcp.scalebar_errors(chunk)}
        self.bars.setRowCount(len(chunk.scalebars))
        for r, sb in enumerate(chunk.scalebars):
            e = bar_errors.get(sb.label, {})
            use = QTableWidgetItem()
            use.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            use.setCheckState(Qt.CheckState.Checked if sb.enabled else Qt.CheckState.Unchecked)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked if sb.checkpoint else Qt.CheckState.Unchecked)
            label = QTableWidgetItem(sb.label)
            label.setIcon(icon("scalebar"))
            vals = [use, label, QTableWidgetItem(e.get("marker_a", "")), QTableWidgetItem(e.get("marker_b", "")),
                    QTableWidgetItem(_fmt(sb.distance, 4)), QTableWidgetItem(_fmt(sb.accuracy, 4)), chk,
                    QTableWidgetItem(_fmt(e.get("estimated"), 4)), QTableWidgetItem(_fmt(e.get("error"), 4))]
            for c, it in enumerate(vals):
                if c in (1, 2, 3, 7, 8):
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if c == 8 and e.get("error") is not None and abs(e["error"]) > max(3 * sb.accuracy, 0.005):
                    it.setForeground(QColor(PALETTE["red"]))
                self.bars.setItem(r, c, it)
        self.cams.setRowCount(len(chunk.cameras))
        for r, cam in enumerate(chunk.cameras):
            c = chunk.to_world(cam.center) if cam.aligned else [None] * 3
            ref = cam.reference.location or [None, None, None]
            row = [cam.label, "si" if cam.aligned else "no", _fmt(c[0], 3), _fmt(c[1], 3), _fmt(c[2], 3),
                   _fmt(ref[0], 3), _fmt(ref[1], 3), _precision(cam.meta.get("precision"))]
            for k, v in enumerate(row):
                it = QTableWidgetItem(v)
                if k == 0:
                    it.setIcon(icon("camera_ok" if cam.aligned else "camera_bad"))
                self.cams.setItem(r, k, it)
        rep = self.last_report or {}
        parts = []
        for key, name in (("control_rmse", "RMSE controllo"), ("check_rmse", "RMSE check")):
            if rep.get(key) is not None:
                parts.append(f"{tr(name)}: <b>{rep[key] * 1000:.1f} mm</b>")
        for key, name in (("scalebar_control_rmse", "RMSE barre"), ("scalebar_check_rmse", "RMSE barre check")):
            if rep.get(key) is not None:
                parts.append(f"{tr(name)}: <b>{rep[key] * 1000:.1f} mm</b>")
        if rep.get("method") == "scalebars":
            parts.append(tr("scala dalle barre, coordinate locali"))
        elif str(rep.get("method", "")).endswith("+scalebars"):
            parts.append(tr("scala da riferimenti e barre insieme, pesati con l'accuratezza"))
        if one_photo:
            parts.append(tr("{0} marker su una foto sola: posizione sulla superficie, su una seconda foto vengono "
                            "triangolati", one_photo))
        prec = chunk.meta.get("precision") or {}
        if (prec.get("tie_points") or {}).get("median"):
            parts.append(tr("precisione dei punti di legame (mediana) {0} mm",
                            _precision(prec['tie_points']['median'])))
        self.summary.setText("  |  ".join(parts) if parts else tr("Importa i riferimenti o crea una barra di scala, "
                                                                  "poi premi Aggiorna."))
        self._filling = False

    def _estimated_toggled(self, _on: bool) -> None:
        self._est_chosen = True
        self.set_chunk(self.chunk)

    def selected_markers(self) -> list:
        """Keys of the markers whose rows are selected."""
        if self.chunk is None:
            return []
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.chunk.markers[r].key for r in rows if r < len(self.chunk.markers)]

    def _marker_menu(self, pos) -> None:
        from PySide6.QtWidgets import QMenu

        keys = self.selected_markers()
        menu = QMenu(self)
        names = [self.chunk.marker_by_key(k).label for k in keys] if self.chunk is not None else []
        a_bar = menu.addAction(tr("Crea barra di scala tra {0} e {1}...", names[0], names[1]) if len(keys) == 2
                               else tr("Crea barra di scala... (seleziona due marker)"))
        a_bar.setIcon(icon("scalebar"))
        a_photos = menu.addAction(icon("filter"), tr("Filtra le foto di {0}", ', '.join(names)) if names
                                  else tr("Filtra le foto... (seleziona uno o piu' marker)"))
        a_photos.setEnabled(bool(keys))
        a_refine = menu.addAction(icon("refine"), tr("Affina le bandierine blu di {0}", ', '.join(names)) if names
                                  else tr("Affina le bandierine blu di tutti i marker"))
        a_refine.setToolTip(tr("Cerca per correlazione, nelle foto, il punto di ogni posizione stimata"))
        menu.addSeparator()
        a_merge = menu.addAction(icon("merge"), tr("Unisci {0} in un marker", ', '.join(names)) if len(keys) >= 2
                                 else tr("Unisci i marker... (selezionane due o piu')"))
        a_merge.setEnabled(len(keys) >= 2)
        a_remove = menu.addAction(icon("delete"), tr("Elimina {0}", ', '.join(names)) if names
                                  else tr("Elimina marker... (seleziona uno o piu' marker)"))
        a_remove.setEnabled(bool(keys))
        a_move = menu.addAction(icon("marker"), tr("Sposta {0} nella vista 3D", names[0]) if len(keys) == 1
                                else tr("Sposta nella vista 3D... (seleziona un marker)"))
        a_move.setEnabled(len(keys) == 1)
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is a_move:
            self.move3d_requested.emit(keys[0])
        elif chosen is a_merge:
            self.merge_requested.emit(keys)
        elif chosen is a_remove:
            self.remove_requested.emit(keys)
        if chosen is a_bar:
            self.scalebar_requested.emit(keys)
        elif chosen is a_photos:
            self.filter_photos_requested.emit(keys)
        elif chosen is a_refine:
            self.refine_requested.emit(keys)

    def _bar_menu(self, pos) -> None:
        from PySide6.QtWidgets import QMenu

        rows = sorted({i.row() for i in self.bars.selectedIndexes()})
        menu = QMenu(self)
        a_del = menu.addAction(icon("delete"), tr("Elimina {0} barre di scala", len(rows)) if len(rows) > 1
                               else tr("Elimina la barra di scala"))
        a_del.setEnabled(bool(rows))
        if menu.exec(self.bars.viewport().mapToGlobal(pos)) is a_del:
            self.delete_bars(rows)

    def delete_bars(self, rows) -> None:
        if self.chunk is None:
            return
        gone = [self.chunk.scalebars[r].key for r in rows if r < len(self.chunk.scalebars)]
        self.chunk.scalebars = [sb for sb in self.chunk.scalebars if sb.key not in gone]
        self.set_chunk(self.chunk)
        self.changed.emit()

    def _bar_edited(self, item) -> None:
        from PySide6.QtCore import QTimer

        if self._filling or self.chunk is None:
            return
        r, c = item.row(), item.column()
        if r >= len(self.chunk.scalebars):
            return
        sb = self.chunk.scalebars[r]
        refill = lambda: QTimer.singleShot(0, lambda: self.set_chunk(self.chunk))  # noqa: E731
        if c == 0:
            sb.enabled = item.checkState() == Qt.CheckState.Checked
        elif c == 6:
            sb.checkpoint = item.checkState() == Qt.CheckState.Checked
        elif c in (4, 5):
            try:
                v = float(item.text().replace(",", "."))
            except ValueError:
                v = 0.0
            if v <= 0:  # not a length: the value of the bar comes back
                refill()
                return
            if c == 4:
                sb.distance = v
            else:
                sb.accuracy = v
        else:
            return
        refill()  # the error of the bar with its new distance
        self.changed.emit()

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
        lay.setContentsMargins(4, 4, 4, 4)
        self.out = QPlainTextEdit()
        self.out.setObjectName("consoleOut")
        self.out.setReadOnly(True)
        mono = QFont("Menlo")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.out.setFont(mono)
        self.inp = _HistoryLine()
        self.inp.setFont(mono)
        self.inp.setPlaceholderText(">>>  chunk.build_dem(resolution=0.01)      (doc, chunk, app, ppg, np, window)")
        self.inp.returnPressed.connect(self._run)
        lay.addWidget(self.out, 1)
        lay.addWidget(self.inp)
        self._factory = namespace_factory
        self.console = code.InteractiveConsole(self._factory())
        self.prompt = ">>> "
        self.write("pyarchinit-photogrammetry - console Python\n"
                   "Variabili: doc (progetto), chunk (chunk attivo), app (ppg.app), ppg (modulo), np, window\n")

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


# ---------------------------------------------------------------- log
class _LogBridge(QObject):
    message = Signal(int, str, str)  # level, text, details


class LogHandler(logging.Handler):
    """Forwards log records (from any thread) to the log pane."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.bridge = _LogBridge()

    def emit(self, record) -> None:
        try:
            text, _, details = record.getMessage().partition("\n")
            if record.exc_info:
                details = (details + "\n" if details else "") + logging.Formatter().formatException(record.exc_info)
            self.bridge.message.emit(record.levelno, text, details.strip())
        except RuntimeError:
            pass


LEVELS = {logging.DEBUG: ("debug", "info"), logging.INFO: ("info", "info"), logging.WARNING: ("avviso", "warning"),
          logging.ERROR: ("errore", "error"), logging.CRITICAL: ("errore", "error")}


class LogPane(QWidget):
    """Messages of the session: time, level and text, with filter, search, copy and save.

    Errors keep their traceback as the row tooltip and in the saved file."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        bar = FlowLayout()
        self.level = QComboBox()
        self.level.addItem(tr("Tutto"), logging.DEBUG)
        self.level.addItem(tr("Avvisi ed errori"), logging.WARNING)
        self.level.addItem(tr("Solo errori"), logging.ERROR)
        self.level.currentIndexChanged.connect(lambda _: self._refilter())
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Cerca nel log"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self._refilter())
        self.b_copy = _tool("copy", tr("Copia"), tr("Copia le righe visibili"))
        self.b_save = _tool("save", tr("Salva..."), tr("Salva il log in un file di testo"))
        self.b_folder = _tool("folder", tr("Cartella log"), tr("Apri la cartella dei file di log"))
        self.b_clear = _tool("clear", tr("Pulisci"))
        self.b_copy.clicked.connect(self.copy_visible)
        self.b_save.clicked.connect(self.save_as)
        self.b_folder.clicked.connect(self.open_folder)
        self.b_clear.clicked.connect(self.clear)
        bar.addWidget(QLabel(tr("Mostra:")))
        bar.addWidget(self.level)
        bar.addWidget(self.search)  # expanding: takes the free space of its line
        for b in (self.b_copy, self.b_save, self.b_folder, self.b_clear):
            bar.addWidget(b)
        lay.addLayout(bar)
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("logTable")
        self.table.setHorizontalHeaderLabels([tr("Ora"), tr("Livello"), tr("Messaggio")])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        lay.addWidget(self.table, 1)
        self.entries: list[tuple[float, int, str, str]] = []
        self.counts = {"error": 0, "warning": 0}
        self.handler = LogHandler()
        self.handler.bridge.message.connect(self.log)
        logging.getLogger("pyarchinit_photogrammetry").addHandler(self.handler)
        logging.getLogger("pyarchinit_photogrammetry").setLevel(logging.INFO)

    # ------------------------------------------------------------ API
    def log(self, level: int, text: str, details: str = "") -> None:
        text = translate(text)  # also the messages of the engine and of the logger
        self.entries.append((time.time(), int(level), text, details))
        if len(self.entries) > 5000:
            del self.entries[:1000]
            self._refilter()
            return
        if level >= logging.ERROR:
            self.counts["error"] += 1
        elif level >= logging.WARNING:
            self.counts["warning"] += 1
        if self._visible(level, text):
            self._add_row(self.entries[-1])

    def info(self, text: str) -> None:
        self.log(logging.INFO, text)

    def warning(self, text: str) -> None:
        self.log(logging.WARNING, text)

    def error(self, text: str, details: str = "") -> None:
        self.log(logging.ERROR, text, details)

    def appendPlainText(self, text: str) -> None:  # compatibility with the previous pane
        self.info(text)

    def clear(self) -> None:
        self.entries = []
        self.counts = {"error": 0, "warning": 0}
        self.table.setRowCount(0)

    def text(self, visible_only: bool = True) -> str:
        rows = []
        for ts, level, text, details in self.entries:
            if visible_only and not self._visible(level, text):
                continue
            level_name = tr(LEVELS.get(level, ("?", ""))[0])
            rows.append(f"{time.strftime('%H:%M:%S', time.localtime(ts))}  {level_name:7s} {text}")
            if details:
                rows.append(details.rstrip())
        return "\n".join(rows)

    def detach(self) -> None:
        logging.getLogger("pyarchinit_photogrammetry").removeHandler(self.handler)

    # ------------------------------------------------------------ actions
    def copy_visible(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.text())

    def save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr("Salva log"), str(Path.home() / "ppg-log.txt"),
                                              tr("Testo (*.txt)"))
        if path:
            Path(path).write_text(self.text(visible_only=False), encoding="utf-8")

    def open_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from .logging_setup import log_dir

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir())))

    # ------------------------------------------------------------ internals
    def _visible(self, level: int, text: str) -> bool:
        if level < (self.level.currentData() or logging.DEBUG):
            return False
        q = self.search.text().strip().lower()
        return not q or q in text.lower()

    def _refilter(self) -> None:
        self.table.setRowCount(0)
        for e in self.entries:
            if self._visible(e[1], e[2]):
                self._add_row(e)

    def _add_row(self, entry) -> None:
        ts, level, text, details = entry
        name, iname = LEVELS.get(level, ("info", "info"))
        r = self.table.rowCount()
        self.table.insertRow(r)
        items = [QTableWidgetItem(time.strftime("%H:%M:%S", time.localtime(ts))), QTableWidgetItem(tr(name)),
                 QTableWidgetItem(text.splitlines()[0] if text else "")]
        items[1].setIcon(icon(iname))
        tip = text + ("\n\n" + details if details else "")
        for it in items:
            it.setToolTip(tip)
            if level >= logging.ERROR:
                it.setForeground(QColor(PALETTE["red"]))
            elif level >= logging.WARNING:
                it.setForeground(QColor(PALETTE["orange"]))
        for c, it in enumerate(items):
            self.table.setItem(r, c, it)
        self.table.scrollToBottom()


class PyArchInitPane(QWidget):
    """Sites, stratigraphic units and documentation of the connected PyArchInit database."""

    connect_requested = Signal()
    demo_requested = Signal()
    new_site_requested = Signal()
    us_requested = Signal(str, str, str, str)  # sito, area, us, unita_tipo ("" us = new record)
    site_changed = Signal(str)  # the site of the project chosen in the combo

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        site_row = QHBoxLayout()
        site_row.addWidget(QLabel(tr("Sito del progetto:")))
        self.site_combo = QComboBox()
        self.site_combo.setEditable(True)
        self.site_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.site_combo.setToolTip(tr("Lo scavo a cui appartengono le US e le quote disegnate in questo progetto "
                                      "(un database PyArchInit puo' contenere piu' siti)"))
        self.site_combo.activated.connect(lambda _i: self._site_edited())
        self.site_combo.lineEdit().editingFinished.connect(self._site_edited)
        site_row.addWidget(self.site_combo, 1)
        lay.addLayout(site_row)
        self._project_site = ""
        row = FlowLayout()
        self.b_connect = _tool("database", tr("Connetti..."), tr("Connetti al database di PyArchInit (config.cfg, "
                                                                 "SQLite, PostgreSQL)"))
        self.b_demo = _tool("demo_db", tr("Database demo"), tr("Usa il database dimostrativo"))
        self.b_new_site = _tool("site", tr("Nuovo sito..."), tr("Crea la scheda Sito in PyArchInit"))
        self.b_new_us = _tool("us", tr("Nuova US..."), tr("Crea una scheda US nel sito selezionato"))
        row.addWidget(self.b_connect)
        row.addWidget(self.b_demo)
        row.addWidget(self.b_new_site)
        row.addWidget(self.b_new_us)
        lay.addLayout(row)
        self.b_connect.clicked.connect(self.connect_requested)
        self.b_demo.clicked.connect(self.demo_requested)
        self.b_new_site.clicked.connect(self.new_site_requested)
        self.b_new_us.clicked.connect(lambda: self.us_requested.emit(self.current_site() or "", "1", "", "US"))
        self.status = QLabel(tr("Non connesso"))
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Elemento"), tr("Descrizione")])
        self.tree.setIconSize(QSize(16, 16))
        self.tree.setAlternatingRowColors(True)
        self.tree.itemDoubleClicked.connect(self._open_us)
        self.tree.currentItemChanged.connect(lambda cur, _prev: self._show_media(cur))
        lay.addWidget(self.tree, 1)
        self.media_label = QLabel(tr("Media: seleziona una US o un sito"))
        self.media_label.setObjectName("pageSubtitle")
        lay.addWidget(self.media_label)
        self.media = QListWidget()
        self.media.setViewMode(QListWidget.ViewMode.IconMode)
        self.media.setIconSize(QSize(96, 96))
        self.media.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.media.setMovement(QListWidget.Movement.Static)
        self.media.setWordWrap(True)
        self.media.setMaximumHeight(150)
        self.media.setToolTip(tr("Media di PyArchInit collegati: doppio clic per aprirli"))
        self.media.itemDoubleClicked.connect(self._open_media)
        lay.addWidget(self.media)
        hint = QLabel(tr("Doppio clic su una US per aprire la scheda"))
        hint.setObjectName("pageSubtitle")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.pa = None

    def set_connection(self, pa, label: str) -> None:
        self.pa = pa
        kind = ("PostgreSQL" if pa.db.is_postgres else "SpatiaLite" if pa.db.spatialite
                else tr("SQLite (senza mod_spatialite)"))
        self.status.setText(tr("Connesso ({0}): {1}", kind, label))
        self.refresh()

    def set_disconnected(self, text: str = "Non connesso") -> None:
        self.pa = None
        self.status.setText(tr(text))
        self.tree.clear()

    def set_project_site(self, sito: str) -> None:
        """Show the site of the project (no signal); the tree then lists only that site."""
        self._project_site = sito or ""
        self.site_combo.blockSignals(True)
        self.site_combo.setCurrentText(self._project_site)
        self.site_combo.blockSignals(False)
        self.refresh()

    def _site_edited(self) -> None:
        text = self.site_combo.currentText().strip()
        if text != self._project_site:
            self._project_site = text
            self.site_changed.emit(text)
            self.refresh()

    def refresh(self) -> None:
        self.tree.clear()
        names = self.site_names() if self.pa is not None else []
        self.site_combo.blockSignals(True)
        self.site_combo.clear()
        self.site_combo.addItems(names if not self._project_site or self._project_site in names
                                 else [self._project_site, *names])
        self.site_combo.setCurrentText(self._project_site)
        self.site_combo.blockSignals(False)
        if self.pa is None:
            return
        try:
            sites = self.pa.sites()
        except Exception as exc:
            self.status.setText(tr("Errore: {0}", exc))
            return
        if self._project_site and any(str(s.get("sito")) == self._project_site for s in sites):
            sites = [s for s in sites if str(s.get("sito")) == self._project_site]
        for s in sites:
            name = s.get("sito")
            si = QTreeWidgetItem([str(name), str(s.get("comune") or "")])
            si.setIcon(0, icon("site"))
            self.tree.addTopLevelItem(si)
            us_root = QTreeWidgetItem([tr("Unita' stratigrafiche"), ""])
            us_root.setIcon(0, icon("us"))
            si.addChild(us_root)
            try:
                for u in self.pa.stratigraphic_units(name):
                    it = QTreeWidgetItem([f"{u.get('unita_tipo') or 'US'} {u.get('us')} (area {u.get('area')})",
                                          str(u.get("d_stratigrafica") or "")])
                    it.setData(0, Qt.ItemDataRole.UserRole, (str(name), str(u.get("area") or ""), str(u.get("us")),
                                                             str(u.get("unita_tipo") or "US")))
                    us_root.addChild(it)
                doc_root = QTreeWidgetItem([tr("Documentazione"), ""])
                doc_root.setIcon(0, icon("doc"))
                si.addChild(doc_root)
                for d in self.pa.documentation(name):
                    doc_root.addChild(QTreeWidgetItem([str(d.get("nome_doc")),
                                                       str(d.get("tipo_documentazione") or "")]))
            except Exception as exc:
                us_root.addChild(QTreeWidgetItem([tr("errore: {0}", exc), ""]))
            si.setExpanded(True)
            us_root.setExpanded(True)
        self.tree.resizeColumnToContents(0)

    def site_names(self) -> list[str]:
        if self.pa is None:
            return []
        try:
            return [str(s.get("sito")) for s in self.pa.sites() if s.get("sito")]
        except Exception:
            return []

    def current_site(self) -> str | None:
        if self._project_site:
            return self._project_site
        it = self.tree.currentItem()
        while it is not None and it.parent() is not None:
            it = it.parent()
        if it is not None:
            return it.text(0)
        names = self.site_names()
        return names[0] if names else None

    def _show_media(self, item) -> int:
        """The media linked to the US (or the site) of ``item`` as thumbnails; returns how many."""
        self.media.clear()
        if item is None or self.pa is None:
            return 0
        data = item.data(0, Qt.ItemDataRole.UserRole)
        try:
            if data:
                media = self.pa.us_media(data[0], data[1], data[2], data[3])
                what = f"{data[3]} {data[2]} (area {data[1]})"
            elif item.parent() is None:  # a site
                media = self.pa.site_media(item.text(0))
                what = tr("sito {0}", item.text(0))
            else:
                self.media_label.setText(tr("Media: seleziona una US o un sito"))
                return 0
        except Exception as exc:
            self.media_label.setText(tr("Media non letti: {0}", exc))
            return 0
        for m in media:
            it = QListWidgetItem(str(m["filename"]))
            preview = m["thumb"] or m["resize"]
            it.setIcon(QIcon(str(preview)) if preview is not None else icon("doc"))
            it.setToolTip(f"{m['filename']}.{m['filetype']} - {m['mediatype']}"
                          + (f"\n{m['descrizione']}" if m.get("descrizione") else "")
                          + ("" if m["path"] or m["resize"] else tr("\n(file non presente su questo computer)")))
            it.setData(Qt.ItemDataRole.UserRole, m)
            self.media.addItem(it)
        self.media_label.setText(tr("Media di {0}: {1}", what, len(media)) if media
                                 else tr("Nessun media collegato a {0}", what))
        return len(media)

    def _open_media(self, item) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        m = item.data(Qt.ItemDataRole.UserRole) or {}
        path = m.get("path") or m.get("resize") or m.get("thumb")
        if path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_us(self, item, _col) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            self.us_requested.emit(*data)


# ---------------------------------------------------------------- layers
LAYER_ICONS = {"polygon": "polygon", "line": "line", "point": "point"}


class LayersPane(QWidget):
    """Layers drawn on the orthomosaic/DEM view: show/hide, zoom, remove, attributes of the
    clicked feature (editable: double click a value, Enter to confirm)."""

    attribute_edited = Signal(str, int, str, object)  # layer, feature index, field, new value
    us_form_requested = Signal(str, int)  # layer, feature index
    READ_ONLY = {"layer", "x", "y", "area_m2", "lunghezza_m", "z", "svg", "dxf"}
    MAX_LISTED = 500  # features listed under a layer

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        bar = FlowLayout()
        self.b_all = _tool("ok", tr("Tutti"), tr("Mostra tutti i layer"))
        self.b_none = _tool("eraser", tr("Nessuno"), tr("Nascondi tutti i layer"))
        self.b_zoom = _tool("search", "Zoom", tr("Zoom sul layer o sull'elemento selezionato"))
        self.b_remove = _tool("clear", tr("Rimuovi"), tr("Rimuovi il layer selezionato dalla vista"))
        self.b_us = _tool("us", tr("Scheda US"), tr("Apri (o crea) la scheda PyArchInit dell'unita' selezionata"))
        self.b_us.clicked.connect(lambda: self.us_form_requested.emit(*self._current) if self._current else None)
        for b in (self.b_all, self.b_none, self.b_zoom, self.b_remove, self.b_us):
            bar.addWidget(b)
        lay.addLayout(bar)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIconSize(QSize(16, 16))
        self.tree.setAlternatingRowColors(True)
        lay.addWidget(self.tree, 1)
        title = QLabel(tr("ATTRIBUTI"))
        title.setObjectName("sectionTitle")
        lay.addWidget(title)
        self.attrs = QTableWidget(0, 2)
        self.attrs.setHorizontalHeaderLabels([tr("Campo"), tr("Valore")])
        self.attrs.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.attrs.verticalHeader().setVisible(False)
        self.attrs.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed
                                   | QAbstractItemView.EditTrigger.AnyKeyPressed)
        self.attrs.setMaximumHeight(200)
        self.attrs.itemChanged.connect(self._attr_changed)
        lay.addWidget(self.attrs)
        hint = QLabel(tr("Doppio clic su un valore per modificarlo"))
        hint.setObjectName("pageSubtitle")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.view = None
        self._filling = False
        self._current: tuple[str, int] | None = None  # layer, feature index shown in the table
        self.tree.itemChanged.connect(self._changed)
        self.tree.itemDoubleClicked.connect(lambda it, _c: self._zoom_item(it))
        self.tree.itemClicked.connect(self._clicked)
        self.b_all.clicked.connect(lambda: self._set_all(True))
        self.b_none.clicked.connect(lambda: self._set_all(False))
        self.b_zoom.clicked.connect(lambda: self._zoom_item(self.tree.currentItem()))
        self.b_remove.clicked.connect(self._remove_current)

    def set_view(self, view) -> None:
        self.view = view
        view.layers_changed.connect(self.rebuild)
        view.feature_clicked.connect(self.show_attributes)
        self.rebuild()

    def rebuild(self) -> None:
        if self.view is None:
            return
        self._filling = True
        expanded = {self.tree.topLevelItem(i).text(0).split("  (")[0] for i in range(self.tree.topLevelItemCount())
                    if self.tree.topLevelItem(i).isExpanded()}
        self.tree.clear()
        for name in self.view.layer_order:
            lay = self.view.layers[name]
            top = QTreeWidgetItem([f"{name}  ({len(lay['features'])})"])
            top.setIcon(0, icon(LAYER_ICONS.get(lay["kind"], "line")))
            top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            top.setCheckState(0, Qt.CheckState.Checked if lay["visible"] else Qt.CheckState.Unchecked)
            top.setData(0, Qt.ItemDataRole.UserRole, (name, -1))
            self.tree.addTopLevelItem(top)
            feats = lay["features"]
            for i, (_item, _label, attrs) in enumerate(feats[:self.MAX_LISTED]):
                shown = [(k, v) for k, v in attrs.items() if not str(k).startswith("_")]
                text = str(attrs.get("etichetta") or "") or ", ".join(f"{k} {v}" for k, v in shown[:2])
                child = QTreeWidgetItem([text or tr("elemento {0}", i + 1)])
                child.setData(0, Qt.ItemDataRole.UserRole, (name, i))
                top.addChild(child)
            if len(feats) > self.MAX_LISTED:  # thousands of contour lines: listing them would only slow down
                more = QTreeWidgetItem([tr("... altri {0} elementi (clic sulla mappa per interrogarli)",
                                           len(feats) - self.MAX_LISTED)])
                more.setData(0, Qt.ItemDataRole.UserRole, (name, -2))  # not a feature, not the layer
                top.addChild(more)
            top.setExpanded(name in expanded or len(feats) <= 12)
        self._filling = False

    def _changed(self, item, _col) -> None:
        if self._filling or self.view is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data[1] == -1:
            self.view.set_layer_visible(data[0], item.checkState(0) == Qt.CheckState.Checked)

    def _set_all(self, visible: bool) -> None:
        if self.view is None:
            return
        for name in self.view.layer_order:
            self.view.set_layer_visible(name, visible)
        self.rebuild()

    def _zoom_item(self, item) -> None:
        if item is None or self.view is None:
            return
        name, idx = item.data(0, Qt.ItemDataRole.UserRole)
        if idx < 0:
            self.view.zoom_to_layer(name)
        else:
            self.view.zoom_to_feature(name, idx)

    def _clicked(self, item, _col) -> None:
        if self.view is None:
            return
        name, idx = item.data(0, Qt.ItemDataRole.UserRole)
        if idx >= 0:
            self.show_attributes(name, {**self.view.layers[name]["features"][idx][2], "_index": idx})

    def _remove_current(self) -> None:
        item = self.tree.currentItem()
        if item is None or self.view is None:
            return
        name, _ = item.data(0, Qt.ItemDataRole.UserRole)
        self.view.remove_layer(name)

    def show_attributes(self, layer: str, attrs: dict) -> None:
        idx = int(attrs.get("_index", -1))
        self._current = (layer, idx) if idx >= 0 else None
        rows = [("layer", layer)] + [(k, v) for k, v in attrs.items() if not str(k).startswith("_")]
        self._filling = True
        self.attrs.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            key = QTableWidgetItem(str(k))
            key.setFlags(key.flags() & ~Qt.ItemFlag.ItemIsEditable)
            val = QTableWidgetItem("" if v is None else str(v))
            if k in self.READ_ONLY or self._current is None:
                val.setFlags(val.flags() & ~Qt.ItemFlag.ItemIsEditable)
                val.setForeground(QColor(PALETTE["grey"]))
            self.attrs.setItem(r, 0, key)
            self.attrs.setItem(r, 1, val)
        self._filling = False

    def _attr_changed(self, item) -> None:
        if self._filling or item.column() != 1 or self._current is None or self.view is None:
            return
        key = self.attrs.item(item.row(), 0).text()
        layer, idx = self._current
        old = self.view.features(layer)[idx].get(key) if idx < len(self.view.features(layer)) else None
        value = _typed(item.text(), old)
        self.view.set_feature_attribute(layer, idx, key, value)
        self.attribute_edited.emit(layer, idx, key, value)


def _typed(text: str, old):
    """Keep numbers numeric when the field was numeric."""
    text = text.strip()
    if isinstance(old, bool):
        return text.lower() in ("1", "true", "si", "sì", "vero", "yes")
    if isinstance(old, (int, float)) and not isinstance(old, bool):
        try:
            return type(old)(float(text.replace(",", "."))) if isinstance(old, float) else int(float(text.replace(",", ".")))
        except ValueError:
            return old
    return text


# ---------------------------------------------------------------- sections
class SectionsPane(QWidget):
    """Sections drawn on the orthomosaic: list, preview of the drawing, files, actions."""

    zoom_requested = Signal(dict)
    publish_requested = Signal(dict)
    delete_requested = Signal(dict)
    folder_requested = Signal()
    elevation_requested = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QScrollArea
        from PySide6.QtSvgWidgets import QSvgWidget

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        bar = FlowLayout()
        self.b_zoom = _tool("search", tr("Zoom sulla traccia"))
        self.b_folder = _tool("folder", tr("Apri cartella"))
        self.b_publish = _tool("publish", tr("Pubblica in PyArchInit"), tr("Scrive la traccia in pyarchinit_sezioni"))
        self.b_delete = _tool("clear", tr("Elimina"))
        self.b_elevation = _tool("elevation", tr("Prospetto"),
                                 tr("Ortofoto e rilievo della parete lungo questa traccia"))
        for b in (self.b_zoom, self.b_folder, self.b_publish, self.b_elevation, self.b_delete):
            bar.addWidget(b)
        outer.addLayout(bar)
        lay = QHBoxLayout()
        outer.addLayout(lay, 1)
        self.list = QListWidget()
        self.list.setIconSize(QSize(16, 16))
        self.list.currentItemChanged.connect(lambda cur, _p: self._show(cur))
        lay.addWidget(self.list)
        right = QVBoxLayout()
        self.info = QLabel(tr("Traccia una sezione: Disegno > Traccia sezione, poi due clic sull'ortofoto."))
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right.addWidget(self.info)
        self.svg = QSvgWidget()
        self.svg.setStyleSheet("background: white;")
        scroll = self._scroll = QScrollArea()
        scroll.setWidget(self.svg)
        scroll.setWidgetResizable(False)
        scroll.setStyleSheet("background: white;")
        right.addWidget(scroll, 1)
        lay.addLayout(right, 1)
        self.sections: list[dict] = []
        self.b_zoom.clicked.connect(lambda: self._emit(self.zoom_requested))
        self.b_publish.clicked.connect(lambda: self._emit(self.publish_requested))
        self.b_delete.clicked.connect(lambda: self._emit(self.delete_requested))
        self.b_elevation.clicked.connect(lambda: self._emit(self.elevation_requested))
        self.b_folder.clicked.connect(self.folder_requested)

    def current(self) -> dict | None:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it is not None else None

    def _emit(self, signal) -> None:
        s = self.current()
        if s is not None:
            signal.emit(s)

    def set_sections(self, sections: list[dict]) -> None:
        self.sections = list(sections)
        self.list.clear()
        for s in self.sections:
            it = QListWidgetItem(icon("line" if s.get("kind", "vertical") == "vertical" else "slice"),
                                 f"{s['label']}  ({s.get('length', 0):.2f} m)")
            it.setData(Qt.ItemDataRole.UserRole, s)
            self.list.addItem(it)
        if self.sections:
            self.list.setCurrentRow(len(self.sections) - 1)

    def add(self, section: dict) -> None:
        self.set_sections(self.sections + [section])

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt name
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self._show(self.list.currentItem())  # the preview follows the width of the pane

    def _show(self, item) -> None:
        if item is None:
            self.svg.load(b"")
            return
        s = item.data(Qt.ItemDataRole.UserRole)
        svg = s.get("svg")
        if svg and Path(svg).exists():
            data, size = svg_preview(Path(svg), max_width=max(400, self._scroll.viewport().width() - 4))
            self.svg.load(data)
            if size is not None:
                self.svg.setFixedSize(size)
        p1, p2 = s.get("p1", [0, 0]), s.get("p2", [0, 0])
        self.info.setText(tr("<b>{0}</b>  lunghezza {1:.3f} m  -  da ({2:.2f}, {3:.2f}) a ({4:.2f}, {5:.2f})<br>SVG: "
                             "{6}<br>DXF: {7}  -  3D: {8}", s['label'], s.get('length', 0), p1[0], p1[1], p2[0], p2[1],
                             s.get('svg', ''), s.get('dxf', ''), s.get('dxf3d', '')))



def svg_preview(path: Path, max_width: int = 1400, min_line_px: float = 1.6, min_text_px: float = 11.0):
    """An SVG drawing for an on-screen preview: reduced to ``max_width`` pixels, with its
    lines and texts made thick enough to be seen at that size (a section drawn at scale is
    often metres long on paper: reduced 20 times, a 0.5 mm line is a twentieth of a pixel).
    Returns ``(bytes, QSize or None)``."""
    import re

    from PySide6.QtCore import QByteArray
    from PySide6.QtSvg import QSvgRenderer

    text = path.read_text(encoding="utf-8", errors="replace")
    size = QSvgRenderer(QByteArray(text.encode("utf-8"))).defaultSize()
    box = re.search(r'viewBox="\s*[-\d.eE+]+[\s,]+[-\d.eE+]+[\s,]+([\d.eE+]+)[\s,]+([\d.eE+]+)', text)
    if size.width() <= 0 or box is None or float(box.group(1)) <= 0:
        return text.encode("utf-8"), None
    f = min(1.0, max_width / size.width())
    shown = QSize(max(1, int(size.width() * f)), max(1, int(size.height() * f)))
    px_per_unit = shown.width() / float(box.group(1))

    def line(m):
        return f'stroke-width="{max(float(m.group(1)), min_line_px / px_per_unit):.4g}"'

    def label(m):  # larger, but not above the top of the drawing
        attrs = m.group(1)
        y = re.search(r'\by="([\d.eE+-]+)"', attrs)
        room = float(y.group(1)) * 0.95 if y else float("inf")

        def size(fm):
            return f'font-size="{max(float(fm.group(1)), min(min_text_px / px_per_unit, room)):.4g}"'

        return "<text " + re.sub(r'font-size="([\d.eE+-]+)"', size, attrs) + ">"

    text = re.sub(r'stroke-width="([\d.eE+-]+)"', line, text)
    text = re.sub(r"<text ([^>]*)>", label, text)
    return text.encode("utf-8"), shown


class ProxiesPane(QWidget):
    """3D proxies of the stratigraphic units: list, 3D view, export, link to PyArchInit."""

    create_requested = Signal()
    show_requested = Signal()
    export_requested = Signal()
    publish_requested = Signal(list)  # indices
    quote_requested = Signal(list)
    us_requested = Signal(int)
    delete_requested = Signal(list)

    COLS = ["Proxy", "US", "Area", "Volume m3", "Pianta m2", "Quota sup.", "Quota inf.", "PyArchInit"]

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        bar = FlowLayout()
        self.b_new = _tool("model", tr("Crea proxy..."), tr("Solidi delle US tra la superficie e una base"))
        self.b_show = _tool("view3d", tr("Vedi in 3D"))
        self.b_export = _tool("export", tr("Esporta (Extended Matrix)"), tr("Un OBJ con un oggetto per US (US12, "
                                                                            "USM3...)"))
        for b in (self.b_new, self.b_show, self.b_export):
            bar.addWidget(b)
        lay.addLayout(bar)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.itemDoubleClicked.connect(lambda it: self.us_requested.emit(it.row()))
        lay.addWidget(self.table, 1)
        bar2 = FlowLayout()
        self.b_pub = _tool("publish", tr("Pubblica in PyArchInit"), tr("Modello 3D collegato alla scheda US (media)"))
        self.b_quote = _tool("dem", tr("Quote nella scheda US"), tr("Scrive quota minima e massima assolute nella "
                                                                    "scheda"))
        self.b_us = _tool("us", tr("Scheda US"))
        self.b_del = _tool("clear", tr("Elimina"))
        for b in (self.b_pub, self.b_quote, self.b_us, self.b_del):
            bar2.addWidget(b)
        lay.addLayout(bar2)
        self.total = QLabel("")
        self.total.setWordWrap(True)
        lay.addWidget(self.total)
        self.b_new.clicked.connect(self.create_requested)
        self.b_show.clicked.connect(self.show_requested)
        self.b_export.clicked.connect(self.export_requested)
        self.b_pub.clicked.connect(lambda: self.publish_requested.emit(self.selected()))
        self.b_quote.clicked.connect(lambda: self.quote_requested.emit(self.selected()))
        self.b_us.clicked.connect(lambda: self.us_requested.emit(self.selected()[0]) if self.selected() else None)
        self.b_del.clicked.connect(lambda: self.delete_requested.emit(self.selected()))

    def selected(self) -> list[int]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return rows or list(range(self.table.rowCount()))

    def set_proxies(self, entries: list[dict]) -> None:
        from .scene import PROXY_COLORS

        self.table.setRowCount(len(entries))
        for r, e in enumerate(entries):
            vals = [e["name"], e.get("us") or "", e.get("area") or "", f"{e['volume_m3']:.3f}", f"{e['area_m2']:.3f}",
                    f"{e['z_top'][1]:.3f}", f"{e['z_bottom'][0]:.3f}",
                    (tr("collegato") if e.get("id_media") else "")]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 0:
                    col = PROXY_COLORS[r % len(PROXY_COLORS)]
                    it.setForeground(QColor(int(col[0]), int(col[1]), int(col[2])))
                self.table.setItem(r, c, it)
        vol = sum(e["volume_m3"] for e in entries)
        self.total.setText(tr("{0} proxy, volume totale {1:.3f} m3", len(entries), vol) if entries else
                           tr("Nessun proxy: crea i solidi delle US con 'Crea proxy'"))
