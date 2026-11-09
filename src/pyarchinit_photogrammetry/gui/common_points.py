"""*Flusso di lavoro > Punti in comune*: two chunks side by side to place the ties between them (:mod:`..engine.ties`).

Each side shows a raster of a chunk - orthophoto, DEM or a relief of its DEM - in a :class:`TieRasterView`. The two
views are linked through the current transforms of the chunks: the world point under the centre of one is under the
centre of the other, at the same metres per screen pixel, and the cursor of one is echoed as a cross in the other.
Two chunks in different systems of the world are converted with PROJ (as :func:`..engine.chunks.shown_in`); a local
frame beside a system of the world cannot be placed and is superimposed, middle on middle (said in the window), as the
2D overlays of :mod:`.chunk_views` do. A raster built in another transform of its chunk (to be rebuilt) is read in the
transform it was built in (its stamp, :mod:`..core.stamps`), so a click on it is still the right internal point.

*Legame* (on by default): a click in one view and a click in the other make a tie (:func:`..engine.ties.add_tie`: a
marker in each chunk, the height read from the chunk's DEM, precision two cells of each raster shown, combined);
after the first click the other view jumps to where the current transforms put that point; Esc cancels the half tie.
Dragging the empty raster pans, dragging an end of a tie moves it (its marker's position; its residual is no longer
drawn; an end placed without photos before a new alignment of its chunk - drawn grey, to be measured again - is
placed again by dragging it). The ties are drawn as points with the residual vector of the last check (x :data:`RESIDUAL_SCALE`), the
suspect ones in red; a click selects one.

Ends on photos: the right-click menu of the photo view has *Estremo del legame su questa foto* while this window is
open (:meth:`CommonPointsWindow.end_on_photo`): the pending end, or the second end of the pending tie, measured there
(a pinned projection of the end's marker: triangulated when it has two photos or more, else placed on the surface);
*Foto del punto* in the menu of a view lists the photos of that chunk that see the point. Every tie made, moved or
measured here is one step of *Modifica > Annulla* with its markers (:func:`.ties_dialog.record_edit`). *Controlla*
and *Applica* open the report of *Aggancia chunk con legami*."""
from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QTransform
from PySide6.QtWidgets import (QComboBox, QDialog, QGraphicsItem, QGraphicsPathItem, QGraphicsView, QHBoxLayout,
                               QLabel, QMenu, QPushButton, QSplitter, QVBoxLayout, QWidget)

from ..core import crs as crs_mod
from ..core import geometry
from ..engine import ties as ties_mod
from . import i18n
from .i18n import tr
from .image_views import RASTER_TITLES, RELIEF_KINDS, RasterView, polyline_path
from .ties_dialog import alive, edit_state, record_edit, undo_shortcuts

TIE_LAYER = "Legami"  # names of layers: data, as the other layers of the views
PENDING_LAYER = "Legame a metà"
RESIDUAL_LAYER = "Residui dei legami"
RESIDUAL_SCALE = 20.0  # the residual vectors drawn this many times longer than they are
DRAG_PX = 4  # a press that moves farther than this (screen pixels) is a drag, not a click
COLOURS = {"tie": "#00e5ff", "selected": "#ffe600", "suspect": "#ff3b30", "off": "#9aa0a6", "pending": "#ff9f1c"}


# ---------------------------------------------------------------- the rasters of a chunk and their frames
def raster_choices(chunk) -> list[tuple[str, Path, str]]:
    """The rasters of ``chunk`` a side can show: ``(kind, path, title)`` - the orthophoto, the DEM, the reliefs of its
    DEM (:data:`.image_views.RELIEF_KINDS`) - those whose file exists."""
    out = []
    for kind, path in (("ortho", chunk.orthomosaic_path), ("dem", chunk.dem_path)):
        if path is not None and Path(path).exists():
            out.append((kind, Path(path), tr(RASTER_TITLES[kind])))
    for kind in RELIEF_KINDS:
        path = chunk.product_path(kind)
        if path is not None and Path(path).exists():
            out.append((kind, Path(path), tr(RASTER_TITLES.get(kind, kind))))
    return out


def file_frame(chunk, kind: str) -> np.ndarray:
    """The transform from the internal frame of ``chunk`` to the coordinates of its raster ``kind`` ("ortho", "dem" or
    a relief): the transform the raster was built in (its stamp), else the chunk's transform now."""
    from ..core import stamps

    if kind == "ortho":
        source = "orthomosaic"
    elif kind == "dem":
        source = "dem"
    else:
        source = (chunk.meta.get("relief") or {}).get(kind)
    built = stamps.stamp_of(chunk, source)
    return np.asarray(chunk.transform if built is None else built, float)


def raster_cell(path) -> float:
    """The side of a cell of a raster in metres (degrees of a geographic system turned into metres as
    :func:`..engine.ties.raster_precision` does)."""
    import rasterio

    with rasterio.open(path) as src:
        cell = float(max(abs(src.res[0]), abs(src.res[1])))
        geographic = src.crs is not None and src.crs.is_geographic
    return cell * 111_320.0 if geographic else cell


def _point(frame: np.ndarray, xyz) -> np.ndarray:
    return geometry.apply(frame, np.asarray(xyz, float).reshape(-1, 3))


@dataclass
class End:
    """An end of a tie being made: a point of ``chunk`` clicked on a raster (``internal``, ``precision``) and/or
    measured on a photo (``camera``, ``xy`` in its pixels)."""

    chunk: object
    internal: np.ndarray | None = None
    precision: float | None = None
    camera: object = None
    xy: tuple[float, float] | None = None


# ---------------------------------------------------------------- one view
class TieRasterView(RasterView):
    """A :class:`.image_views.RasterView` for the ties: in mode ``"tie"`` a click is a point (:attr:`tie_clicked`), a
    drag of the empty raster pans, a drag of an end of a tie moves it (:attr:`tie_dragged`), a click on it selects it
    (:attr:`tie_selected`); Esc :attr:`escape`; the right button :attr:`point_menu`. The view may scroll beyond its
    raster (the other chunk's point can lie outside it) and says when its view moved (:attr:`view_moved`)."""

    tie_clicked = Signal(float, float)  # world x, y of the raster shown
    tie_selected = Signal(str)  # tie id
    tie_dragged = Signal(str, str, float, float)  # tie id, "a" or "b", the new world x, y
    point_menu = Signal(float, float, object)  # world x, y, global position
    escape = Signal()
    view_moved = Signal()
    MARGIN = 3.0  # the view scrolls this many raster sizes beyond each side of the raster

    def __init__(self, parent=None):
        super().__init__(parent)
        self.detail = True
        self._press = None  # {"pos", "last", "hit", "moved"} of the left button
        self.raster_loaded.connect(self._update_scroll_rect)

    def _update_scroll_rect(self) -> None:
        base = self.raster_rect()
        m = self.MARGIN
        QGraphicsView.setSceneRect(self, base.adjusted(-m * base.width(), -m * base.height(), m * base.width(),
                                                       m * base.height()))

    def _view_changed(self, *args) -> None:
        super()._view_changed(*args)
        self.view_moved.emit()

    def _point_menu(self, global_pos, x: float, y: float) -> None:
        self.point_menu.emit(x, y, global_pos)

    def tie_at(self, scene_pos: QPointF):
        """``(tie id, "a"/"b", graphics item, label item)`` of the end of a tie under a scene position, or None."""
        hit = self.feature_at(scene_pos)
        if hit is None or hit[0] != TIE_LAYER:
            return None
        attrs = hit[1]
        features = self.layers.get(TIE_LAYER, {}).get("features", [])
        if not 0 <= attrs.get("_index", -1) < len(features):
            return None
        item, label, _attrs = features[attrs["_index"]]
        return attrs["tie"], attrs["end"], item, label

    def mousePressEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        if self.mode != "tie" or self.tr is None:
            return super().mousePressEvent(ev)
        self.setFocus()
        sp = self.mapToScene(ev.position().toPoint())
        if ev.button() == Qt.MouseButton.RightButton:
            self.point_menu.emit(*self.world(sp), ev.globalPosition().toPoint())
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(ev)
        pos = ev.position().toPoint()
        self._press = {"pos": pos, "last": pos, "hit": self.tie_at(sp), "moved": False}

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        super().mouseMoveEvent(ev)
        press = self._press
        if press is None or self.mode != "tie":
            return
        pos = ev.position().toPoint()
        if not press["moved"] and (pos - press["pos"]).manhattanLength() <= DRAG_PX:
            return
        press["moved"] = True
        if press["hit"] is not None:  # an end of a tie follows the mouse
            for it in press["hit"][2:]:  # the point and its label
                if it is not None:
                    it.setPos(self.mapToScene(pos))
        else:  # the raster follows the mouse
            d = pos - press["last"]
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
        press["last"] = pos

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        press, self._press = self._press, None
        if press is None or self.mode != "tie" or ev.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(ev)
        x, y = self.world(self.mapToScene(ev.position().toPoint()))
        if press["hit"] is not None:
            tie_id, end = press["hit"][:2]
            if press["moved"]:
                self.tie_dragged.emit(tie_id, end, x, y)
            else:
                self.tie_selected.emit(tie_id)
        elif not press["moved"]:
            self.tie_clicked.emit(x, y)

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        if self.mode == "tie":
            return  # a second press is not a second point
        super().mouseDoubleClickEvent(ev)

    def keyPressEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        if ev.key() == Qt.Key.Key_Escape:
            self.escape.emit()
            ev.accept()
            return
        super().keyPressEvent(ev)


class _Side:
    """One half of the window: the chunk, the raster shown, its frame (:func:`file_frame`), the height used to link
    the views and the cross that echoes the cursor of the other view."""

    def __init__(self, owner, index: int):
        self.owner, self.index = owner, index
        self.view = TieRasterView()
        self.view.setMinimumSize(200, 200)
        self.chunks = QComboBox()
        self.rasters = QComboBox()
        self.chunk = None
        self.kind: str | None = None
        self.path: Path | None = None
        self.stamp = None  # (path, mtime, frame) of what is loaded
        self.frame = np.eye(4)
        self.inverse = np.eye(4)
        self.z0 = 0.0  # height (raster coordinates) used to link the views: the plan hardly depends on it
        self.cell = 0.0  # metres of a cell of the raster shown (full resolution)
        self.cross = None
        self.widget = QWidget()
        lay = QVBoxLayout(self.widget)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Chunk")))
        row.addWidget(self.chunks, 1)
        row.addWidget(QLabel(tr("Raster")))
        row.addWidget(self.rasters, 1)
        lay.addLayout(row)
        lay.addWidget(self.view, 1)
        self.view.raster_loaded.connect(self._forget_items)

    def _forget_items(self) -> None:
        self.cross = None  # deleted with the scene

    def set_frame(self) -> None:
        self.frame = file_frame(self.chunk, self.kind) if self.chunk is not None and self.kind else np.eye(4)
        self.inverse = np.linalg.inv(self.frame)

    def to_internal(self, x: float, y: float, z: float | None = None) -> np.ndarray:
        return _point(self.inverse, [x, y, self.z0 if z is None else z])[0]

    def to_raster(self, internal) -> np.ndarray:
        return _point(self.frame, internal)

    def placed(self) -> bool:
        return self.chunk is not None and self.view.tr is not None


# ---------------------------------------------------------------- the window
class CommonPointsWindow(QDialog):
    """Two linked views of two chunks to place the ties between them (see the module)."""

    @classmethod
    def ask(cls, window) -> "CommonPointsWindow":
        dlg = cls(window)
        dlg.show()
        return dlg

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setModal(False)
        self.setWindowTitle(tr("Punti in comune"))
        self.resize(1400, 820)
        self.pending: End | None = None
        self.selected: str | None = None
        # ties with an end moved since the last check -> the internal places of their ends when the residual held: no
        # residual drawn until a new check, or until the ends are back there (an undo)
        self._moved: dict[str, list] = {}
        # ties made here: every precision they were given by the program, as {chunk key: precision of that end
        # (metres)} - the latest last; an undo brings one of them back, so it is still the program's, not one by hand
        self._made: dict[str, list[dict]] = {}
        self._signature = None  # what the views show (chunks, transforms, rasters): refresh() re-links when it changes
        self._result_seen = None
        self._syncing = False
        self._filling = False
        self.superimposed = False

        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.b_tie = QPushButton(tr("Legame"))
        self.b_tie.setCheckable(True)
        self.b_tie.setChecked(True)
        self.b_tie.setToolTip(tr("Clic su un punto in una vista e sullo stesso punto nell'altra: un legame. Esc annulla "
                                 "il legame a metà; trascinare sposta la vista o un estremo"))
        self.b_ties = QPushButton(tr("Legami..."))
        self.b_check = QPushButton(tr("Controlla"))
        self.b_apply = QPushButton(tr("Applica"))
        self.b_close = QPushButton(tr("Chiudi"))
        bar.addWidget(self.b_tie)
        bar.addStretch(1)
        for b in (self.b_ties, self.b_check, self.b_apply, self.b_close):
            bar.addWidget(b)
        lay.addLayout(bar)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #b9770e")
        self.note.hide()
        lay.addWidget(self.note)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.sides = [_Side(self, 0), _Side(self, 1)]
        for side in self.sides:
            split.addWidget(side.widget)
        lay.addWidget(split, 1)
        self.status = QLabel(tr("Clic sullo stesso punto nelle due viste: un legame. Tasto destro: le foto del punto."))
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        for side in self.sides:
            v = side.view
            v.set_mode("tie")
            side.chunks.currentIndexChanged.connect(lambda _i, s=side: self._chunk_chosen(s))
            side.rasters.currentIndexChanged.connect(lambda _i, s=side: self._raster_chosen(s))
            v.view_moved.connect(lambda s=side: self._sync(s))
            v.mouse_world.connect(lambda x, y, _z, s=side: self._echo(s, x, y))
            v.tie_clicked.connect(lambda x, y, s=side: self.clicked(s, x, y))
            v.tie_selected.connect(self.select)
            v.tie_dragged.connect(lambda tid, end, x, y, s=side: self.move_end(s, tid, end, x, y))
            v.point_menu.connect(lambda x, y, pos, s=side: self._menu(s, x, y, pos))
            v.escape.connect(self.cancel)
        self.b_tie.toggled.connect(lambda _on: self._say_mode())
        self.b_ties.clicked.connect(lambda: self.window.tie_chunks())
        self.b_check.clicked.connect(self.check)
        self.b_apply.clicked.connect(self.apply)
        self.b_close.clicked.connect(self.close)
        window.refreshed.connect(self.refresh)
        self.finished.connect(self._forget)  # Esc / reject: Qt closes the dialog without a closeEvent
        undo_shortcuts(self, window)

        doc = window.doc
        first = window.chunk if window.chunk in doc.chunks else (doc.chunks[0] if doc.chunks else None)
        second = next((c for c in doc.chunks if c is not first), first)
        self._fill_chunks(first, second)

    # ------------------------------------------------------------------ chunks and rasters
    @property
    def doc(self):
        return self.window.doc

    def _fill_chunks(self, first=None, second=None) -> None:
        wanted = [first, second]
        self._filling = True
        try:
            for side, chunk in zip(self.sides, wanted):
                side.chunks.clear()
                for ch in self.doc.chunks:
                    side.chunks.addItem(ch.label, ch.key)
                side.chunks.setCurrentIndex(max(0, side.chunks.findData(chunk.key if chunk is not None else None)))
        finally:
            self._filling = False
        for side in self.sides:
            self._chunk_chosen(side, sync=False)
        self._sync(self.sides[0])
        self._signature = self.signature()

    def set_chunks(self, first, second) -> None:
        """The chunks of the two sides (objects or keys)."""
        for side, chunk in zip(self.sides, (first, second)):
            key = getattr(chunk, "key", chunk)
            side.chunks.setCurrentIndex(side.chunks.findData(key))

    def set_raster(self, index: int, kind: str) -> None:
        """The raster ``kind`` ("ortho", "dem" or a relief) of side ``index``."""
        side = self.sides[index]
        i = side.rasters.findData(kind)
        if i >= 0:
            side.rasters.setCurrentIndex(i)

    def _chunk_of(self, side):
        key = side.chunks.currentData()
        return next((c for c in self.doc.chunks if c.key == key), None)

    def _chunk_chosen(self, side, sync: bool = True) -> None:
        if self._filling:
            return
        side.chunk = self._chunk_of(side)
        kind = side.kind
        self._filling = True
        try:
            side.rasters.clear()
            for k, _path, title in raster_choices(side.chunk) if side.chunk is not None else []:
                side.rasters.addItem(title, k)
            i = side.rasters.findData(kind)
            side.rasters.setCurrentIndex(i if i >= 0 else 0)
        finally:
            self._filling = False
        self._load(side)
        if sync:
            self._sync(self._other(side))
            self._drop_unshown_pending()
        self._update_note()

    def _drop_unshown_pending(self) -> None:
        """A half tie whose chunk is no longer in either view is dropped (said): the next click would tie it unseen."""
        if self.pending is not None and all(s.chunk is not self.pending.chunk for s in self.sides):
            self.pending = None
            self.redraw()
            self._say(tr("Legame a metà annullato: il suo chunk non è più in una delle due viste."))

    def _raster_chosen(self, side) -> None:
        if self._filling:
            return
        self._load(side)
        self._sync(self._other(side))

    def _load(self, side) -> None:
        """The raster chosen of ``side``'s chunk shown (again only when its file or frame changed)."""
        kind = side.rasters.currentData()
        path = next((p for k, p, _t in raster_choices(side.chunk) if k == kind), None) if side.chunk else None
        side.kind = kind
        side.set_frame()
        if path is None:
            side.path, side.stamp, side.cell = None, None, 0.0
            side.view.clear_raster(tr("{0}: nessuna ortofoto, DEM o rilievo da mostrare", side.chunk.label)
                                   if side.chunk is not None else "")
            return
        stamp = (str(path), path.stat().st_mtime_ns, side.frame.tobytes())
        if stamp != side.stamp or side.view.tr is None:
            side.path, side.stamp = path, stamp
            syncing, self._syncing = self._syncing, True  # the fit of the new raster does not move the other view
            try:
                side.view.load(path, kind)
            finally:
                self._syncing = syncing
            side.cell = raster_cell(path)
            side.z0 = self._middle_height(side)
        self.redraw(side)

    def _middle_height(self, side) -> float:
        """A height of the raster's frame for the link between the views: the DEM at the middle of the raster shown,
        else the median of the values of a DEM shown, else 0."""
        v = side.view
        if v.tr is None:
            return 0.0
        x, y = v.world(v.raster_rect().center())
        z = self.height_at(side, x, y, approximate=True)
        if z is not None:
            return float(z)
        if side.kind == "dem" and v.z is not None and np.isfinite(v.z).any():
            return float(np.nanmedian(v.z))
        return 0.0

    def _other(self, side):
        return self.sides[1 - side.index]

    def refresh(self) -> None:
        """After a change of the project (a chunk removed or renamed, a product rebuilt, a solve applied or undone):
        the lists of chunks, the frames, the rasters whose file changed and the ties drawn again."""
        if not alive(self):
            return
        try:
            if not self.isVisible():
                return
        except RuntimeError:  # deleted meanwhile
            return
        signature = self.signature()
        if signature == self._signature:  # nothing the views show changed: the ties only (no re-link, no re-read)
            self.redraw()
            return
        keys = [c.key for c in self.doc.chunks]
        listed = [self.sides[0].chunks.itemData(i) for i in range(self.sides[0].chunks.count())]
        labels = [self.sides[0].chunks.itemText(i) for i in range(self.sides[0].chunks.count())]
        chosen_again = keys != listed or labels != [c.label for c in self.doc.chunks]
        if chosen_again:
            chunks = {c.key: c for c in self.doc.chunks}
            first = chunks.get(self.sides[0].chunks.currentData())
            second = chunks.get(self.sides[1].chunks.currentData())
            self._fill_chunks(first or (self.doc.chunks[0] if self.doc.chunks else None),
                              second or next((c for c in self.doc.chunks if c is not first), first))
        else:
            for side in self.sides:
                self._chunk_chosen(side, sync=False)
            self._sync(self.sides[0])
        if self.pending is not None and self.pending.chunk not in self.doc.chunks:
            self.pending = None
        self._update_note()
        self.redraw()
        # the same chunks shown: re-linking them changes nothing the signature has; else other chunks were chosen
        self._signature = signature if not chosen_again else self.signature()

    def signature(self):
        """The chunks of the project and, for each side, its chunk's transform and system and the rasters it can show
        (files and their times): :meth:`refresh` re-reads and re-links the views only when it changes."""
        sides = []
        for side in self.sides:
            ch = self._chunk_of(side)
            if ch is None:
                sides.append(None)
                continue
            rasters = []
            for k, path, _t in raster_choices(ch):
                with contextlib.suppress(OSError):
                    rasters.append((k, str(path), path.stat().st_mtime_ns))
            sides.append((ch.key, np.asarray(ch.transform, float).tobytes(), ch.crs,
                          json.dumps([ch.meta.get("built_in"), ch.meta.get("relief")], sort_keys=True, default=str),
                          tuple(rasters)))
        return tuple((c.key, c.label) for c in self.doc.chunks), tuple(sides)

    # ------------------------------------------------------------------ the link between the views
    def _offset(self, src, dst) -> np.ndarray:
        """For two chunks that cannot be placed together (a local frame and a system of the world): the move that puts
        the middle of ``src``'s raster on the middle of ``dst``'s (world coordinates now)."""
        out = []
        for side in (src, dst):
            x, y = side.view.world(side.view.raster_rect().center())
            out.append(side.chunk.to_world(side.to_internal(x, y)[None])[0])
        return out[1] - out[0]

    def transfer(self, src, dst, xy) -> np.ndarray:
        """Points ``xy`` ((n, 2) coordinates of ``src``'s raster) in the coordinates of ``dst``'s raster, through the
        current transforms of the two chunks (and PROJ between two systems of the world)."""
        from ..engine.chunks import shown_in

        xy = np.asarray(xy, float).reshape(-1, 2)
        internal = _point(src.inverse, np.column_stack([xy, np.full(len(xy), src.z0)]))
        try:
            world, placed = shown_in(src.chunk, internal, dst.chunk.crs)
        except (ValueError, RuntimeError):
            world, placed = src.chunk.to_world(internal), False
        if not placed:
            world = world + self._offset(src, dst)
        inner = geometry.apply(np.linalg.inv(np.asarray(dst.chunk.transform, float)), world)
        return dst.to_raster(inner)[:, :2]

    def _update_note(self) -> None:
        a, b = (s.chunk for s in self.sides)
        self.superimposed = bool(a is not None and b is not None and a.crs != b.crs
                                 and crs_mod.is_local(a.crs) != crs_mod.is_local(b.crs))
        if self.superimposed:
            self.note.setText(tr("{0} è in un sistema locale, {1} in un sistema del mondo: le viste sono sovrapposte "
                                 "(centro su centro), non collocate. I legami valgono lo stesso.",
                                 *(s.chunk.label for s in sorted(self.sides, key=lambda s: not crs_mod.is_local(
                                     s.chunk.crs)))))
        self.note.setVisible(self.superimposed)

    def centre(self, side) -> tuple[float, float]:
        """The point of ``side``'s raster (its coordinates) under the centre of its view."""
        v = side.view
        return v.world(v.mapToScene(v.viewport().rect().center()))

    def units_per_pixel(self, side) -> float:
        """Raster units (metres) per screen pixel of ``side``'s view."""
        return abs(side.view.tr.a) / max(side.view.transform().m11(), 1e-12)

    def _sync(self, src) -> None:
        """The other view centred on the point under the centre of ``src``'s view, at the same metres per screen
        pixel."""
        dst = self._other(src)
        if self._syncing or not (src.placed() and dst.placed()):
            return
        self._syncing = True
        try:
            x, y = self.centre(src)
            upp = self.units_per_pixel(src)
            pts = self.transfer(src, dst, [[x, y], [x + 100 * upp, y]])
            ratio = float(np.hypot(*(pts[1] - pts[0]))) / (100 * upp)
            m = abs(dst.view.tr.a) / max(upp * ratio, 1e-12)
            dst.view.setTransform(QTransform.fromScale(m, m))
            dst.view.centerOn(dst.view.scene_pt(*pts[0]))
        finally:
            self._syncing = False

    def centre_on(self, side, x: float, y: float) -> None:
        """``side``'s view centred on its raster point x, y (zoom kept); the other follows."""
        side.view.centerOn(side.view.scene_pt(x, y))
        self._sync(side)

    def _echo(self, src, x: float, y: float) -> None:
        """The cursor of ``src`` as a cross in the other view."""
        dst = self._other(src)
        if not (src.placed() and dst.placed()):
            return
        (px, py), = self.transfer(src, dst, [[x, y]])
        if dst.cross is None:
            path = polyline_path([(-9, 0), (9, 0)])
            path.addPath(polyline_path([(0, -9), (0, 9)]))
            dst.cross = QGraphicsPathItem(path)
            dst.cross.setPen(dst.view._pen("#ff2d55", 1.5))
            dst.cross.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            dst.cross.setZValue(20000)
            dst.view.scene().addItem(dst.cross)
        dst.cross.setPos(dst.view.scene_pt(px, py))

    # ------------------------------------------------------------------ heights and precision
    def height_at(self, side, x: float, y: float, approximate: bool = False) -> float | None:
        """The height (coordinates of ``side``'s raster) of its chunk's DEM at the raster point x, y; None without a
        DEM or a value there. A DEM built in another transform than the raster shown is read through the internal
        frame (a few rounds: the height moves the point a little in plan when the frames are tilted)."""
        from ..core.free_drawings import raster_values

        ch = side.chunk
        dem = ch.dem_path if ch is not None else None
        if dem is None or not Path(dem).exists():
            return None
        dem_frame = file_frame(ch, "dem")
        if np.allclose(dem_frame, side.frame, rtol=0, atol=1e-12):
            (z,) = raster_values(dem, [(x, y)])
            return z
        to_dem = dem_frame @ side.inverse
        z = side.z0
        for _ in range(1 if approximate else 4):
            q = _point(to_dem, [x, y, z])[0]
            (zz,) = raster_values(dem, [(q[0], q[1])])
            if zz is None:
                return None
            z = float(_point(side.frame @ np.linalg.inv(dem_frame), [q[0], q[1], zz])[0, 2])
        return z

    def internal_at(self, side, x: float, y: float) -> np.ndarray | None:
        """The internal point of ``side``'s chunk at its raster point x, y with the height of its DEM; None without."""
        z = self.height_at(side, x, y)
        return None if z is None else side.to_internal(x, y, z)

    # ------------------------------------------------------------------ making ties
    def _say(self, text: str) -> None:
        self.status.setText(text)
        with contextlib.suppress(AttributeError, RuntimeError):
            self.window.statusBar().showMessage(text, 15000)

    def _say_mode(self) -> None:
        self._say(tr("Clic sullo stesso punto nelle due viste: un legame.") if self.b_tie.isChecked() else
                  tr("Legame spento: un clic sceglie un legame, trascinare sposta la vista o un estremo."))

    def clicked(self, side, x: float, y: float) -> None:
        """A click on ``side``'s raster: the first end of a tie (the other view jumps there) or the second (the tie)."""
        if not self.b_tie.isChecked() or side.chunk is None:
            return
        p = self.internal_at(side, x, y)
        if p is None:
            self.window._warn(tr("Nessuna quota del DEM di {0} in questo punto: un legame ha bisogno della quota "
                                 "(costruisci il DEM del chunk).", side.chunk.label))
            return
        self.take(End(side.chunk, p, ties_mod.RASTER_PIXELS * side.cell or None), side)

    def take(self, end: End, side=None) -> None:
        """An end placed: kept as the pending one, or tied to the pending one of the other chunk."""
        if self.pending is not None and self.pending.chunk is not end.chunk:
            self._make(self.pending, end)
            return
        again = self.pending is not None
        self.pending = end
        self.redraw()
        other = self._other(side) if side is not None else next((s for s in self.sides if s.chunk is not end.chunk
                                                                and s.placed()), None)
        if other is not None and other.chunk is not end.chunk and other.placed() and end.internal is not None:
            mine = next((s for s in self.sides if s.chunk is end.chunk and s.placed()), None)
            if mine is not None:  # salta all'altro: where the current transforms put the point
                x, y = mine.to_raster(end.internal)[0, :2]
                (ox, oy), = self.transfer(mine, other, [[x, y]])
                self._syncing = True
                try:
                    other.view.centerOn(other.view.scene_pt(ox, oy))
                finally:
                    self._syncing = False
        if again:
            self._say(tr("Stesso chunk ({0}): primo estremo spostato qui. Ora lo stesso punto nell'altro chunk.",
                         end.chunk.label))
        else:
            self._say(tr("Primo estremo in {0}: ora lo stesso punto nell'altro chunk (vista o foto). Esc annulla.",
                         end.chunk.label))

    def cancel(self) -> None:
        """Esc: the half tie dropped."""
        if self.pending is None:
            return
        self.pending = None
        self.redraw()
        self._say(tr("Legame a metà annullato."))

    def _position(self, end: End) -> np.ndarray | None:
        if end.internal is not None:
            return end.internal
        from ..markers import gcp

        return gcp.surface_point(end.chunk, end.camera, *end.xy)

    @staticmethod
    def _triangulate_end(chunk, marker) -> None:
        """``marker`` triangulated from its photos (two or more), else placed on the surface (the rules of
        :func:`..markers.gcp.triangulate_markers`); the other markers of the chunk stay where they are."""
        import copy

        kept = {id(m): copy.deepcopy(m.position) for m in chunk.markers if m is not marker}
        chunk.triangulate_markers()
        for m in chunk.markers:
            if id(m) in kept:
                m.position = kept[id(m)]

    def _ends_now(self, tie) -> list:
        """The internal places of a tie's ends now (None for an end without one)."""
        out = []
        for end, uid in tie.end_uids():
            found = ties_mod.resolve_end(self.doc, end, uid)
            p = None if found is None else ties_mod.end_internal(*found)
            out.append(None if p is None else [round(float(v), 9) for v in p])
        return out

    def _blocked(self) -> bool:
        """An elaboration runs in the worker (the check of the ties reads the markers and the ties there): no tie is
        made, moved or measured until it ends, said."""
        runner = getattr(self.window, "runner", None)
        if runner is None or not runner.busy:
            return False
        self._say(tr("Un'elaborazione è in corso (per esempio il controllo dei legami): aspetta che finisca, poi "
                     "riprova."))
        return True

    def _make(self, first: End, second: End):
        """The tie between two ends of two chunks: one step of Modifica > Annulla (the tie and both markers)."""
        from ..markers import gcp

        if self._blocked():
            return None
        doc = self.doc
        points = []
        for end in (first, second):
            p = self._position(end)
            if p is None:
                self.window._warn(tr("{0}: il punto della foto non incontra la superficie (nuvola o punti di legame): "
                                     "misuralo anche su un'altra foto o cliccalo su un raster.", end.chunk.label))
                return None
            points.append(p)
        before = edit_state(doc)
        tie = ties_mod.add_tie(doc, first.chunk, points[0], second.chunk, points[1], precision=1.0)
        precisions = []
        for end, (_ck, marker_key) in ((first, tie.a), (second, tie.b)):
            marker = end.chunk.marker_by_key(marker_key)
            if end.camera is not None:
                end.chunk.add_marker_projection(marker, end.camera, end.xy[0], end.xy[1], pinned=True)
                gcp.exclude_photo(end.chunk, marker, end.camera.key, excluded=False)
                self._triangulate_end(end.chunk, marker)
                with contextlib.suppress(Exception):  # the blue flags on the other photos: a help only
                    gcp.project_marker(end.chunk, marker)
                precisions.append(ties_mod.end_precision(end.chunk, marker) or ties_mod.TABLE_PRECISION)
            else:
                precisions.append(end.precision or ties_mod.TABLE_PRECISION)
        all_ties = ties_mod.ties_of(doc)
        for t in all_ties:
            if t.id == tie.id:
                t.precision = float(np.hypot.reduce(precisions))
        ties_mod.save_ties(doc, all_ties)
        self._made[tie.id] = [{first.chunk.key: precisions[0], second.chunk.key: precisions[1]}]
        record_edit(self.window, tr("Legame da Punti in comune"), before)
        self.pending = None
        self.selected = tie.id
        self.window._ties_made()
        self.redraw()
        self._say(tr("{0}: legame fra {1} e {2}, precisione {3} m.", tie.name, first.chunk.label, second.chunk.label,
                     i18n.number(float(np.hypot.reduce(precisions)), 3)))
        return tie

    def end_on_photo(self, chunk, camera, x: float, y: float):
        """*Estremo del legame su questa foto*: a point of a photo of ``chunk`` - the second end of the pending tie
        (another chunk), the photo measure of the pending end (same chunk), or the first end of a new tie. Returns the
        tie when one was made."""
        if not getattr(camera, "aligned", False):
            self.window._warn(tr("{0} non è allineata: un estremo su una foto ha bisogno della sua posizione.",
                                 camera.label))
            return None
        end = End(chunk, camera=camera, xy=(float(x), float(y)))
        if self.pending is not None and self.pending.chunk is chunk:
            self.pending.camera, self.pending.xy = camera, (float(x), float(y))  # measured on this photo now
            self.redraw()
            self._say(tr("Primo estremo in {0} misurato su {1}: ora lo stesso punto nell'altro chunk.", chunk.label,
                         camera.label))
            return None
        if self.pending is not None:
            return self._make(self.pending, end)
        self.take(end)
        return None

    def selected_tie(self):
        if self.selected is None:
            return None
        return next((t for t in ties_mod.ties_of(self.doc) if t.id == self.selected and not t.broken), None)

    def add_photo_to_selected(self, chunk, camera, x: float, y: float) -> bool:
        """The end in ``chunk`` of the selected tie measured on one more photo (a pinned projection of its marker,
        triangulated with the others); one step of Modifica > Annulla."""
        from ..markers import gcp

        tie = self.selected_tie()
        if tie is None or not getattr(camera, "aligned", False) or self._blocked():
            return False
        end = next(((e, u) for e, u in tie.end_uids() if e[0] == chunk.key), None)
        found = None if end is None else ties_mod.resolve_end(self.doc, *end)
        if found is None:
            return False
        before = edit_state(self.doc)
        was = self._ends_now(tie)
        ch, marker = found
        ch.add_marker_projection(marker, camera, x, y, pinned=True)
        gcp.exclude_photo(ch, marker, camera.key, excluded=False)
        self._triangulate_end(ch, marker)
        with contextlib.suppress(Exception):
            gcp.project_marker(ch, marker)
        # the precision: this end from its photos now, the other end as it was - only for a tie made here whose
        # precision was not changed by hand since (else it stays, said)
        made = next((dict(m) for m in reversed(self._made.get(tie.id, []))
                     if abs(tie.precision - float(np.hypot.reduce(list(m.values())))) <= 1e-9), None)
        kept = made is None
        if not kept:
            made[ch.key] = ties_mod.end_precision(ch, marker) or ties_mod.TABLE_PRECISION
            self._made[tie.id].append(made)
            all_ties = ties_mod.ties_of(self.doc)
            for t in all_ties:
                if t.id == tie.id:
                    t.precision = float(np.hypot.reduce(list(made.values())))
            ties_mod.save_ties(self.doc, all_ties)
        self._moved.setdefault(tie.id, was)
        record_edit(self.window, tr("Estremo di un legame su una foto"), before)
        self.window._ties_made()
        self.redraw()
        text = tr("{0}: estremo in {1} misurato anche su {2}.", tie.name, ch.label, camera.label)
        if kept:
            text += " " + tr("Precisione del legame invariata ({0} m): cambiala in Aggancia chunk con legami se serve.",
                             i18n.number(float(tie.precision), 3))
        self._say(text)
        return True

    def move_end(self, side, tie_id: str, end_name: str, x: float, y: float) -> bool:
        """An end of a tie dragged on ``side``'s raster: its marker at the new point (height from the DEM); one step of
        Modifica > Annulla. An end measured on photos stays: its flags on the photos say where it is."""
        tie = next((t for t in ties_mod.ties_of(self.doc) if t.id == tie_id), None)
        if tie is None or (end_name == "b" and tie.b is None) or self._blocked():
            self.redraw()
            return False
        end, uid = (tie.a, tie.a_uid) if end_name == "a" else (tie.b, tie.b_uid)
        found = ties_mod.resolve_end(self.doc, end, uid)
        if found is None:
            self.redraw()
            return False
        chunk, marker = found
        if any(p.measured for p in marker.projections.values()):
            self.redraw()
            self._say(tr("{0} è misurato sulle foto: spostane le bandierine sulle foto.", marker.label))
            return False
        p = self.internal_at(side, x, y)
        if p is None:
            self.redraw()
            self.window._warn(tr("Nessuna quota del DEM di {0} in questo punto: un legame ha bisogno della quota "
                                 "(costruisci il DEM del chunk).", chunk.label))
            return False
        before = edit_state(self.doc)
        self._moved.setdefault(tie_id, self._ends_now(tie))
        ties_mod.place_end(self.doc, tie_id, end_name, p)  # placed again now: no longer stale after a new alignment
        self.selected = tie_id
        record_edit(self.window, tr("Sposta l'estremo di un legame"), before)
        self.window._ties_made()
        self.redraw()
        self._say(tr("{0}: estremo spostato; il residuo vale di nuovo dopo Controlla.", tie.name))
        return True

    def select(self, tie_id: str | None) -> None:
        """A tie selected (drawn in yellow, its row selected in *Aggancia chunk con legami* when open)."""
        self.selected = tie_id
        self.redraw()
        dlg = self.window._live_ties_dialog()
        if dlg is not None and tie_id is not None:
            with contextlib.suppress(RuntimeError):
                row = dlg.row_of(tie_id)
                if row >= 0:
                    dlg.table.selectRow(row)
        tie = self.selected_tie()
        if tie is not None:
            self._say(tr("{0} selezionato: trascina un estremo per spostarlo; sulle foto, tasto destro per misurarlo "
                         "anche lì.", tie.name))

    # ------------------------------------------------------------------ drawing
    def _residuals(self) -> tuple[dict, set]:
        """The residual of each tie from the last check (the dialog's, else the solve applied last) and the suspect
        ties; the ties with an end moved since are left out."""
        result = None
        dlg = self.window._live_ties_dialog()
        if dlg is not None:
            with contextlib.suppress(RuntimeError):
                result = dlg.result
        if result is not None and result is not self._result_seen:
            self._result_seen = result
            self._moved.clear()
        if self._moved:  # an end back where it was when the residual held (an undo): its residual again
            now = {t.id: t for t in ties_mod.ties_of(self.doc)}
            for tid, was in list(self._moved.items()):
                if tid not in now or self._ends_now(now[tid]) == was:
                    del self._moved[tid]
        if result is not None:
            rows, suspects = result["report"]["residuals"], set(result["report"]["suspects"])
        else:
            rows, suspects = (self.doc.meta.get("ties_report") or {}).get("residuals") or [], set()
        return {r["id"]: r for r in rows if r.get("id") and r["id"] not in self._moved}, suspects

    def redraw(self, side=None) -> None:
        """The ties (points, residual vectors) and the pending end drawn again in both views (or ``side``)."""
        with contextlib.suppress(RuntimeError):  # already deleted
            for s in ([side] if side is not None else self.sides):
                self._draw(s)

    def _draw(self, side) -> None:
        v = side.view
        for name in (TIE_LAYER, RESIDUAL_LAYER, PENDING_LAYER):
            v.remove_layer(name)
        if not side.placed():
            return
        residuals, suspects = self._residuals()
        points, segments = [], []
        for t in ties_mod.ties_of(self.doc):
            for name, end, uid in (("a", t.a, t.a_uid), ("b", t.b, t.b_uid)):
                if end is None or end[0] != side.chunk.key:
                    continue
                found = ties_mod.resolve_end(self.doc, end, uid)
                p = None if found is None else ties_mod.end_internal(*found)
                if p is None:
                    continue
                x, y = side.to_raster(p)[0, :2]
                colour = (COLOURS["selected"] if t.id == self.selected
                          else COLOURS["off"] if (t.broken or t.stale or not t.active)
                          else COLOURS["suspect"] if t.id in suspects else COLOURS["tie"])
                points.append((float(x), float(y), colour, t.name, {"tie": t.id, "end": name, "legame": t.name}))
                r = residuals.get(t.id)
                if r is not None:
                    sign = -1.0 if r["chunks"] and r["chunks"][0] == side.chunk.key else 1.0
                    d = sign * RESIDUAL_SCALE * np.array([r["dx"], r["dy"]], float)
                    segments.append([[x, y], [x + d[0], y + d[1]]])
        with v.batch():
            if segments:
                v.add_segments(np.asarray(segments, float), color="#ff4d4d", layer=RESIDUAL_LAYER, width=2.0)
            for x, y, colour, label, attrs in points:
                v.add_point(x, y, color=colour, label=label, layer=TIE_LAYER, attrs=attrs)
            pending = self.pending
            if pending is not None and pending.chunk is side.chunk:
                p = pending.internal if pending.internal is not None else (
                    None if pending.camera is None else self._position(pending))
                if p is not None:
                    x, y = side.to_raster(p)[0, :2]
                    v.add_point(float(x), float(y), color=COLOURS["pending"], label=tr("legame a metà"),
                                layer=PENDING_LAYER)

    # ------------------------------------------------------------------ menus, report
    def _menu(self, side, x: float, y: float, global_pos) -> None:
        menu = QMenu(self)
        a_photos = menu.addAction(tr("Foto del punto"))
        a_cancel = menu.addAction(tr("Annulla il legame a metà")) if self.pending is not None else None
        chosen = menu.exec(global_pos)
        if chosen is a_photos:
            self.photos_of_point(side, x, y)
        elif a_cancel is not None and chosen is a_cancel:
            self.cancel()

    def photos_of_point(self, side, x: float, y: float) -> None:
        """*Foto del punto*: the photos of ``side``'s chunk that see the point, in the Photos pane (that chunk made
        the active one first)."""
        if side.chunk is None:
            return
        p = self.internal_at(side, x, y)
        if p is None:
            p = side.to_internal(x, y)
        w = self.window
        switched = side.chunk is not w.chunk
        if switched and not w._activate_chunk(side.chunk):
            return
        w.filter_photos_by_point(p)
        text = tr("Foto di {0} che vedono il punto: aprine una, poi tasto destro sul punto esatto: Estremo del "
                  "legame su questa foto.", side.chunk.label)
        if switched:  # the main window follows that chunk now: said, not silent
            note = tr("{0} è ora il chunk attivo: le foto del pannello sono le sue.", side.chunk.label)
            with contextlib.suppress(AttributeError):
                w.log_pane.info(note)
            text = note + " " + text
        self._say(text)

    def predicted_point(self, chunk) -> np.ndarray | None:
        """Where the current transforms put the pending end in ``chunk`` (internal), for a photo of that chunk."""
        end = self.pending
        if end is None:
            return None
        p = self._position(end) if end.camera is not None else end.internal
        if p is None:
            return None
        if end.chunk is chunk:
            return np.asarray(p, float)
        from ..engine.chunks import shown_in

        try:
            world, placed = shown_in(end.chunk, np.asarray(p, float)[None], chunk.crs)
        except (ValueError, RuntimeError):
            return None
        if not placed:
            return None
        return geometry.apply(np.linalg.inv(np.asarray(chunk.transform, float)), world)[0]

    def check(self) -> None:
        """*Controlla*: the report of *Aggancia chunk con legami* (its dialog opened, the check started)."""
        self.window.tie_chunks()
        dlg = self.window._live_ties_dialog()
        if dlg is not None:
            dlg.run_check()

    def apply(self) -> None:
        """*Applica*: the last check of *Aggancia chunk con legami* applied (its dialog opened); without one, said."""
        self.window.tie_chunks()
        dlg = self.window._live_ties_dialog()
        if dlg is None:
            return
        if dlg.result is None:
            self.window._warn(tr("Premi prima Controlla: il rapporto mostra i legami sbagliati prima di applicare."))
            return
        dlg.apply()

    def keyPressEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        if ev.key() == Qt.Key.Key_Escape and self.pending is not None:
            self.cancel()
            return
        super().keyPressEvent(ev)

    def _forget(self, *_args) -> None:
        """The one cleanup, reached from a close and from Esc / reject (``finished``: Qt 6 closes a dialog from
        ``done()`` without a closeEvent): the main window no longer refreshes it nor offers its photo entries. Once."""
        if getattr(self, "_forgotten", False):
            return
        self._forgotten = True
        with contextlib.suppress(RuntimeError, TypeError):
            self.window.refreshed.disconnect(self.refresh)
        if getattr(self.window, "_common_points", None) is self:
            self.window._common_points = None

    def closeEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        self._forget()
        super().closeEvent(ev)


__all__ = ["CommonPointsWindow", "TieRasterView", "End", "raster_choices", "file_frame", "raster_cell", "TIE_LAYER",
           "PENDING_LAYER", "RESIDUAL_LAYER", "RESIDUAL_SCALE"]
