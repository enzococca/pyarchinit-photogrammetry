"""2D views: orthomosaic/DEM with drawing tools, and photos with marker placement."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsPixmapItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView, QInputDialog, QMenu)

US_COLORS = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4", "#46f0f0", "#f032e6", "#bcf60c",
             "#fabebe", "#008080", "#e6beff", "#9a6324", "#800000", "#aaffc3", "#808000"]


def _apply(t, x: float, y: float) -> tuple[float, float]:
    """Apply an affine (rasterio) transform to one point."""
    return t.a * x + t.b * y + t.c, t.d * x + t.e * y + t.f


def to_qimage(rgb: np.ndarray) -> QImage:
    rgb = np.ascontiguousarray(rgb)
    h, w = rgb.shape[:2]
    if rgb.ndim == 3 and rgb.shape[2] == 4:
        return QImage(rgb.data, w, h, 4 * w, QImage.Format.Format_RGBA8888).copy()
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def dem_colors(z: np.ndarray, res: float) -> np.ndarray:
    """Hypsometric tint x hillshade, RGBA (transparent where no data)."""
    valid = np.isfinite(z)
    rgba = np.zeros(z.shape + (4,), np.uint8)
    if not valid.any():
        return rgba
    lo, hi = np.percentile(z[valid], [1, 99])
    t = np.clip((np.where(valid, z, lo) - lo) / max(hi - lo, 1e-9), 0, 1)
    stops = np.array([[0.0, 40, 90, 160], [0.25, 60, 160, 120], [0.5, 230, 220, 120], [0.75, 200, 130, 60],
                      [1.0, 250, 250, 250]])
    col = np.stack([np.interp(t, stops[:, 0], stops[:, k]) for k in (1, 2, 3)], -1)
    zf = np.where(valid, z, np.nanmean(z[valid]))
    gy, gx = np.gradient(zf, res)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(gy, -gx)
    az, alt = np.radians(315.0), np.radians(45.0)
    shade = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    shade = np.clip(shade, 0, 1) * 0.7 + 0.3
    rgba[..., :3] = np.clip(col * shade[..., None], 0, 255).astype(np.uint8)
    rgba[..., 3] = np.where(valid, 255, 0)
    return rgba


class _ZoomView(QGraphicsView):
    """Wheel zoom around the cursor, middle/space drag to pan."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QColor(52, 56, 64))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.pixmap_item: QGraphicsPixmapItem | None = None

    def wheelEvent(self, ev) -> None:
        f = 1.2 ** (ev.angleDelta().y() / 120.0)
        self.scale(f, f)

    def fit(self) -> None:
        if self.pixmap_item is not None:
            self.fitInView(self.pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

    def _pen(self, color, width=2.0) -> QPen:
        pen = QPen(QColor(color), width)
        pen.setCosmetic(True)
        return pen


class RasterView(_ZoomView):
    """Orthomosaic or DEM in world coordinates, with drawing tools.

    Modes: ``pan``; ``polygon`` (click vertices, double click to close: a unit);
    ``line`` (two clicks: a section trace); ``measure`` (two clicks: distance and
    height difference); ``point`` (one click: elevation / SAM prompt)."""

    mouse_world = Signal(float, float, object)  # x, y, z or None
    polygon_finished = Signal(object)  # (N,2) world XY
    line_finished = Signal(object, object)
    point_clicked = Signal(float, float, int)  # x, y, label (1 left / 0 right)
    measured = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.mode = "pan"
        self.kind = None
        self.path: Path | None = None
        self.tr = None  # rasterio Affine of the displayed (decimated) array
        self.z = None  # DEM values at display resolution
        self._clicks: list[QPointF] = []
        self._rubber: QGraphicsPathItem | None = None
        self.overlays: list = []

    # ----------------------------------------------------------- data
    def load(self, path: str | Path, kind: str, max_side: int = 6000) -> None:
        import rasterio
        from rasterio.enums import Resampling

        self.scene().clear()
        self.overlays, self._clicks, self._rubber = [], [], None
        self.path, self.kind = Path(path), kind
        with rasterio.open(path) as src:
            f = max(1.0, max(src.width, src.height) / max_side)
            h, w = int(round(src.height / f)), int(round(src.width / f))
            self.tr = src.transform @ src.transform.scale(src.width / w, src.height / h)
            if kind == "dem":
                z = src.read(1, out_shape=(h, w), resampling=Resampling.average, masked=True).astype(np.float64)
                self.z = z.filled(np.nan)
                rgba = dem_colors(self.z, abs(self.tr.a))
            else:
                self.z = None
                n = min(src.count, 4)
                arr = src.read(list(range(1, n + 1)), out_shape=(n, h, w), resampling=Resampling.average)
                arr = np.moveaxis(arr, 0, -1)
                if n == 3:
                    arr = np.dstack([arr, np.full((h, w), 255, np.uint8)])
                rgba = arr.astype(np.uint8)
        self.pixmap_item = self.scene().addPixmap(QPixmap.fromImage(to_qimage(rgba)))
        self.setSceneRect(QRectF(0, 0, w, h))
        self.fit()

    def set_dem(self, path: str | Path) -> None:
        """Elevations for the cursor readout while showing the orthomosaic."""
        import rasterio
        from rasterio.enums import Resampling

        if self.tr is None:
            return
        with rasterio.open(path) as src:
            w = int(self.sceneRect().width())
            h = int(self.sceneRect().height())
            z = src.read(1, out_shape=(max(1, h // 4), max(1, w // 4)), resampling=Resampling.average, masked=True)
            self._dem_lookup = (z.filled(np.nan).astype(float), src.transform @ src.transform.scale(
                src.width / max(1, w // 4), src.height / max(1, h // 4)))

    def world(self, p: QPointF) -> tuple[float, float]:
        x, y = _apply(self.tr, p.x(), p.y())
        return float(x), float(y)

    def scene_pt(self, x: float, y: float) -> QPointF:
        c, r = _apply(~self.tr, x, y)
        return QPointF(c, r)

    def elevation(self, x: float, y: float) -> float | None:
        if self.z is not None:
            c, r = _apply(~self.tr, x, y)
            arr = self.z
        elif getattr(self, "_dem_lookup", None) is not None:
            arr, t = self._dem_lookup
            c, r = _apply(~t, x, y)
        else:
            return None
        r, c = int(r), int(c)
        if 0 <= r < arr.shape[0] and 0 <= c < arr.shape[1] and np.isfinite(arr[r, c]):
            return float(arr[r, c])
        return None

    # ----------------------------------------------------------- overlays
    def add_polygon(self, xy, label: str = "", color: str = "#ff3030", fill_alpha: int = 50):
        path = QPainterPath()
        poly = QPolygonF([self.scene_pt(x, y) for x, y in xy])
        path.addPolygon(poly)
        path.closeSubpath()
        item = self.scene().addPath(path, self._pen(color, 2))
        c = QColor(color)
        c.setAlpha(fill_alpha)
        item.setBrush(QBrush(c))
        self.overlays.append(item)
        if label:
            t = QGraphicsSimpleTextItem(label)
            t.setBrush(QColor("white"))
            t.setFont(QFont(self.font().family(), 11, QFont.Weight.Bold))
            t.setFlag(t.GraphicsItemFlag.ItemIgnoresTransformations)
            t.setPos(poly.boundingRect().center())
            self.scene().addItem(t)
            self.overlays.append(t)
        return item

    def add_line(self, xy, color: str = "#ffd000", label: str = ""):
        path = QPainterPath()
        pts = [self.scene_pt(x, y) for x, y in xy]
        path.moveTo(pts[0])
        for p in pts[1:]:
            path.lineTo(p)
        item = self.scene().addPath(path, self._pen(color, 2.5))
        self.overlays.append(item)
        if label:
            t = QGraphicsSimpleTextItem(label)
            t.setBrush(QColor(color))
            t.setFlag(t.GraphicsItemFlag.ItemIgnoresTransformations)
            t.setPos(pts[0])
            self.scene().addItem(t)
            self.overlays.append(t)
        return item

    def clear_overlays(self) -> None:
        for it in self.overlays:
            self.scene().removeItem(it)
        self.overlays = []

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._clicks = []
        if self._rubber is not None:
            self.scene().removeItem(self._rubber)
            self._rubber = None
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag if mode == "pan" else QGraphicsView.DragMode.NoDrag)
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if mode == "pan" else Qt.CursorShape.CrossCursor)

    # ----------------------------------------------------------- events
    def _update_rubber(self, cursor: QPointF | None = None) -> None:
        pts = list(self._clicks) + ([cursor] if cursor is not None else [])
        if not pts:
            return
        path = QPainterPath(pts[0])
        for p in pts[1:]:
            path.lineTo(p)
        if self._rubber is None:
            self._rubber = self.scene().addPath(path, self._pen("#00e0ff", 2))
        else:
            self._rubber.setPath(path)

    def mouseMoveEvent(self, ev) -> None:
        super().mouseMoveEvent(ev)
        if self.tr is None:
            return
        sp = self.mapToScene(ev.position().toPoint())
        x, y = self.world(sp)
        self.mouse_world.emit(x, y, self.elevation(x, y))
        if self._clicks:
            self._update_rubber(sp)

    def mousePressEvent(self, ev) -> None:
        if self.mode == "pan" or self.tr is None or ev.button() == Qt.MouseButton.MiddleButton:
            return super().mousePressEvent(ev)
        sp = self.mapToScene(ev.position().toPoint())
        if self.mode == "point":
            x, y = self.world(sp)
            self.point_clicked.emit(x, y, 0 if ev.button() == Qt.MouseButton.RightButton else 1)
            mark = QGraphicsEllipseItem(-5, -5, 10, 10)
            mark.setPos(sp)
            mark.setFlag(mark.GraphicsItemFlag.ItemIgnoresTransformations)
            ok = ev.button() != Qt.MouseButton.RightButton
            mark.setBrush(QColor("#30ff60" if ok else "#ff3030"))
            self.scene().addItem(mark)
            self.overlays.append(mark)
            return
        if ev.button() == Qt.MouseButton.RightButton:  # cancel the current drawing
            self.set_mode(self.mode)
            return
        self._clicks.append(sp)
        self._update_rubber()
        if self.mode in ("line", "measure") and len(self._clicks) == 2:
            a, b = (np.array(self.world(p)) for p in self._clicks)
            if self.mode == "line":
                self.add_line([a, b])
                self.line_finished.emit(a, b)
            else:
                za, zb = self.elevation(*a), self.elevation(*b)
                txt = f"Distanza {np.linalg.norm(b - a):.3f} m"
                if za is not None and zb is not None:
                    txt += f", dislivello {zb - za:+.3f} m, distanza inclinata {np.sqrt(np.sum((b - a) ** 2) + (zb - za) ** 2):.3f} m"
                self.measured.emit(txt)
            self.set_mode(self.mode)

    def mouseDoubleClickEvent(self, ev) -> None:
        if self.mode == "polygon" and len(self._clicks) >= 3:
            xy = np.array([self.world(p) for p in self._clicks])
            self.set_mode("polygon")
            self.polygon_finished.emit(xy)
        else:
            super().mouseDoubleClickEvent(ev)


class PhotoView(_ZoomView):
    """A photo with its marker projections; right click to place or remove markers."""

    markers_changed = Signal()
    sam_prompt = Signal(object, float, float, int)  # camera, x, y, label

    def __init__(self, parent=None):
        super().__init__(parent)
        self.chunk = None
        self.camera = None
        self.items: list = []
        self.sam_mode = False

    def show_camera(self, chunk, camera) -> None:
        import cv2

        self.chunk, self.camera = chunk, camera
        self.scene().clear()
        self.items = []
        img = cv2.imread(camera.path, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
        if img is None:
            self.pixmap_item = None
            self.scene().addText(f"Impossibile leggere {camera.path}")
            return
        self.pixmap_item = self.scene().addPixmap(QPixmap.fromImage(to_qimage(img[:, :, ::-1])))
        self.setSceneRect(QRectF(0, 0, img.shape[1], img.shape[0]))
        self.draw_markers()
        self.fit()

    def draw_markers(self) -> None:
        for it in self.items:
            self.scene().removeItem(it)
        self.items = []
        if self.chunk is None or self.camera is None:
            return
        cam = self.camera
        for m in self.chunk.markers:
            pr = m.projections.get(cam.key)
            if pr is not None:
                col = "#30e050" if pr.pinned else "#40a0ff"
                self._flag(pr.x, pr.y, m.label, col)
            if m.position is not None and cam.aligned:
                sensor = self.chunk.sensor(cam)
                px, depth = cam.project(sensor, m.position[None])
                u, v = px[0]
                if depth[0] > 0 and 0 <= u <= sensor.width and 0 <= v <= sensor.height:
                    self._cross(u, v, "#ff4040")

    def _flag(self, x, y, label, color) -> None:
        pen = self._pen(color, 2)
        path = QPainterPath(QPointF(0, 0))
        path.lineTo(0, -22)
        path.lineTo(12, -17)
        path.lineTo(0, -12)
        item = QGraphicsPathItem(path)
        item.setPen(pen)
        item.setBrush(QColor(color))
        item.setPos(QPointF(x, y))
        item.setFlag(item.GraphicsItemFlag.ItemIgnoresTransformations)
        self.scene().addItem(item)
        t = QGraphicsSimpleTextItem(label)
        t.setBrush(QColor(color))
        t.setFont(QFont(self.font().family(), 10, QFont.Weight.Bold))
        t.setFlag(t.GraphicsItemFlag.ItemIgnoresTransformations)
        t.setPos(QPointF(x, y))
        t.setTransform(t.transform().translate(14, -24))
        self.scene().addItem(t)
        self.items += [item, t]

    def _cross(self, x, y, color) -> None:
        path = QPainterPath()
        path.moveTo(-7, 0)
        path.lineTo(7, 0)
        path.moveTo(0, -7)
        path.lineTo(0, 7)
        item = QGraphicsPathItem(path)
        item.setPen(self._pen(color, 1.5))
        item.setPos(QPointF(x, y))
        item.setFlag(item.GraphicsItemFlag.ItemIgnoresTransformations)
        self.scene().addItem(item)
        self.items.append(item)

    def _nearest_projection(self, x, y, radius_px: float):
        best, dmin = None, radius_px
        for m in self.chunk.markers:
            pr = m.projections.get(self.camera.key)
            if pr is not None:
                d = np.hypot(pr.x - x, pr.y - y)
                if d < dmin:
                    best, dmin = m, d
        return best

    def mousePressEvent(self, ev) -> None:
        if self.sam_mode and self.camera is not None and ev.button() in (Qt.MouseButton.LeftButton,
                                                                         Qt.MouseButton.RightButton):
            sp = self.mapToScene(ev.position().toPoint())
            lab = 0 if ev.button() == Qt.MouseButton.RightButton else 1
            self.sam_prompt.emit(self.camera, sp.x(), sp.y(), lab)
            return
        super().mousePressEvent(ev)

    def contextMenuEvent(self, ev) -> None:
        if self.chunk is None or self.camera is None or self.sam_mode:
            return
        sp = self.mapToScene(ev.pos())
        x, y = sp.x(), sp.y()  # scene units = pixels, first pixel centre at (0.5, 0.5) as in COLMAP
        menu = QMenu(self)
        place = menu.addMenu("Posiziona marker qui")
        for m in self.chunk.markers:
            place.addAction(m.label).triggered.connect(lambda _=False, mm=m: self._place(mm, x, y))
        place.addSeparator()
        place.addAction("Nuovo marker...").triggered.connect(lambda: self._new_marker(x, y))
        radius = 15 / max(self.transform().m11(), 1e-9)
        near = self._nearest_projection(x, y, radius)
        if near is not None:
            menu.addAction(f"Rimuovi proiezione di {near.label}").triggered.connect(lambda: self._remove(near))
        menu.exec(ev.globalPos())

    def _place(self, marker, x, y) -> None:
        self.chunk.add_marker_projection(marker, self.camera, x, y, pinned=True)
        self.draw_markers()
        self.markers_changed.emit()

    def _new_marker(self, x, y) -> None:
        label, ok = QInputDialog.getText(self, "Nuovo marker", "Etichetta:", text=f"point {len(self.chunk.markers) + 1}")
        if ok and label:
            m = self.chunk.add_marker(label)
            self._place(m, x, y)

    def _remove(self, marker) -> None:
        marker.projections.pop(self.camera.key, None)
        self.draw_markers()
        self.markers_changed.emit()

    def show_mask(self, mask: np.ndarray, color=(255, 60, 200)) -> None:
        rgba = np.zeros(mask.shape + (4,), np.uint8)
        rgba[mask] = (*color, 110)
        item = self.scene().addPixmap(QPixmap.fromImage(to_qimage(rgba)))
        self.items.append(item)
