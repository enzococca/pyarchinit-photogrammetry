"""2D views: orthomosaic/DEM with drawing tools, and photos with marker placement."""
from __future__ import annotations

from contextlib import contextmanager
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

    Modes: ``pan``; ``select`` (click, shift/cmd-click or drag a box to select features,
    Delete to remove them); ``polygon`` (click vertices, double click to close: a unit; with
    ``freehand`` press, draw and release);
    ``line`` (two clicks: a section trace); ``measure`` (two clicks: distance and
    height difference); ``point`` (one click: elevation / SAM prompt).

    Snapping (``snap_vertex``, ``snap_segment``, tolerance ``snap_px`` in screen pixels) moves
    the points of every drawing tool onto the vertices or the segments of the visible layers."""

    DRAW_MODES = ("polygon", "line", "measure", "point")

    mouse_world = Signal(float, float, object)  # x, y, z or None
    polygon_finished = Signal(object)  # (N,2) world XY
    line_finished = Signal(object, object)
    point_clicked = Signal(float, float, int)  # x, y, label (1 left / 0 right)
    measured = Signal(str)
    layers_changed = Signal()
    feature_clicked = Signal(str, dict)  # layer name, attributes (query in pan mode)
    selection_changed = Signal(int)  # number of selected features
    delete_requested = Signal()  # Delete/Backspace or context menu in select mode
    merge_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.mode = "pan"
        self.kind = None
        self.path: Path | None = None
        self.tr = None  # rasterio Affine of the displayed (decimated) array
        self.z = None  # DEM values at display resolution
        self._clicks: list[QPointF] = []
        self._click_xy: list[tuple[float, float]] = []  # the same points in world coordinates, exact when snapped
        self._rubber: QGraphicsPathItem | None = None
        self.overlays: list = []
        self.layers: dict[str, dict] = {}  # name -> {"kind", "visible", "features": [(item, label_item, attrs)]}
        self.layer_order: list[str] = []
        self.selection: list = []  # graphics items of the selected features
        self._sel_marks: list = []  # highlight items drawn over the selection
        self._band_origin: QPointF | None = None
        self._band = None
        self._press_mods = Qt.KeyboardModifier.NoModifier
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # Delete / Esc in select mode
        self.freehand = False  # polygon tool: press, draw, release
        self._stroking = False
        self.snap_vertex = False
        self.snap_segment = False
        self.snap_px = 12.0
        self._snap_cache = None  # (vertices (n, 2), segments (m, 2, 2)) in world coordinates
        self._snap_marks: dict = {}
        self._batch = 0  # > 0 inside batch(): one layers_changed at the end
        self._batch_changed = False
        self._inv_of = self._inv = None  # self.tr and its inverse

    @contextmanager
    def batch(self):
        """Add or remove many features (contour lines, a reloaded drawing) with a single
        ``layers_changed`` at the end, instead of one per feature: the layer pane and the
        QGIS link then update once."""
        self._batch += 1
        try:
            yield self
        finally:
            self._batch -= 1
            if not self._batch and self._batch_changed:
                self._batch_changed = False
                self.layers_changed.emit()

    def _changed(self) -> None:
        if self._batch:
            self._batch_changed = True
        else:
            self.layers_changed.emit()

    # ----------------------------------------------------------- data
    def load(self, path: str | Path, kind: str, max_side: int = 6000) -> None:
        import rasterio
        from rasterio.enums import Resampling

        had_selection = bool(self.selection)
        self.selection, self._sel_marks, self._band, self._band_origin = [], [], None, None  # deleted by clear()
        self._snap_marks, self._snap_cache, self._stroking = {}, None, False
        self.scene().clear()
        self.overlays, self._clicks, self._click_xy, self._rubber = [], [], [], None
        self.layers, self.layer_order = {}, []
        self._changed()
        if had_selection:
            self.selection_changed.emit(0)
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

    def _inverse(self):
        """World -> pixel transform of the displayed raster (cached: inverting it for every
        vertex of thousands of contour lines took seconds)."""
        if self._inv_of is not self.tr:
            self._inv_of, self._inv = self.tr, ~self.tr
        return self._inv

    def scene_pt(self, x: float, y: float) -> QPointF:
        c, r = _apply(self._inverse(), x, y)
        return QPointF(c, r)

    def elevation(self, x: float, y: float) -> float | None:
        if self.z is not None:
            c, r = _apply(self._inverse(), x, y)
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

    # ----------------------------------------------------------- overlays and layers
    # Everything drawn on the raster belongs to a named layer (units, section traces, contour
    # lines, spot heights...). Layers can be hidden, zoomed to, removed, and their features
    # carry attributes shown when clicked in navigation mode.
    def _layer(self, name: str, kind: str) -> dict:
        lay = self.layers.get(name)
        if lay is None:
            lay = {"kind": kind, "visible": True, "features": []}
            self.layers[name] = lay
            self.layer_order.append(name)
        return lay

    def _register(self, layer: str, kind: str, item, label_item, attrs: dict | None) -> None:
        lay = self._layer(layer, kind)
        attrs = dict(attrs or {})  # the same dict is shared with the item: edits are visible everywhere
        item.setData(0, layer)
        item.setData(1, attrs)
        lay["features"].append((item, label_item, attrs))
        self._snap_cache = None
        for it in (item, label_item):
            if it is not None:
                it.setVisible(lay["visible"])
                self.overlays.append(it)
        self._changed()

    def add_polygon(self, xy, label: str = "", color: str = "#ff3030", fill_alpha: int = 50,
                    layer: str = "Unita' stratigrafiche", attrs: dict | None = None):
        path = QPainterPath()
        poly = QPolygonF([self.scene_pt(x, y) for x, y in xy])
        path.addPolygon(poly)
        path.closeSubpath()
        item = self.scene().addPath(path, self._pen(color, 2))
        c = QColor(color)
        c.setAlpha(fill_alpha)
        item.setBrush(QBrush(c))
        t = None
        if label:
            t = QGraphicsSimpleTextItem(label)
            t.setBrush(QColor("white"))
            t.setFont(QFont(self.font().family(), 11, QFont.Weight.Bold))
            t.setFlag(t.GraphicsItemFlag.ItemIgnoresTransformations)
            t.setPos(poly.boundingRect().center())
            self.scene().addItem(t)
        self._register(layer, "polygon", item, t, {"etichetta": label, **(attrs or {}),
                                                     "_geom": ("Polygon", [[float(x), float(y)] for x, y in xy])})
        return item

    def add_line(self, xy, color: str = "#ffd000", label: str = "", layer: str = "Linee", attrs: dict | None = None,
                 width: float = 2.5):
        xy = np.asarray(xy, float)[:, :2]
        inv = self._inverse()  # all the vertices at once: contour layers have hundreds of thousands
        cols = inv.a * xy[:, 0] + inv.b * xy[:, 1] + inv.c
        rows = inv.d * xy[:, 0] + inv.e * xy[:, 1] + inv.f
        pts = [QPointF(c, r) for c, r in zip(cols.tolist(), rows.tolist())]
        path = QPainterPath()
        path.addPolygon(QPolygonF(pts))  # an open subpath
        item = self.scene().addPath(path, self._pen(color, width))
        t = None
        if label:
            t = QGraphicsSimpleTextItem(label)
            t.setBrush(QColor(color))
            t.setFlag(t.GraphicsItemFlag.ItemIgnoresTransformations)
            t.setPos(pts[0])
            self.scene().addItem(t)
        self._register(layer, "line", item, t, {"etichetta": label, **(attrs or {}), "_geom": ("LineString", xy.tolist())})
        return item

    def add_point(self, x: float, y: float, color: str = "#30ff60", label: str = "", layer: str = "Punti",
                  attrs: dict | None = None):
        mark = QGraphicsEllipseItem(-5, -5, 10, 10)
        mark.setPos(self.scene_pt(x, y))
        mark.setFlag(mark.GraphicsItemFlag.ItemIgnoresTransformations)
        mark.setBrush(QColor(color))
        mark.setPen(self._pen("white", 1))
        self.scene().addItem(mark)
        t = None
        if label:
            t = QGraphicsSimpleTextItem(label)
            t.setBrush(QColor(color))
            t.setFlag(t.GraphicsItemFlag.ItemIgnoresTransformations)
            t.setPos(self.scene_pt(x, y))
            self.scene().addItem(t)
        self._register(layer, "point", mark, t, {"etichetta": label, "x": round(x, 3), "y": round(y, 3),
                                                   **(attrs or {}), "_geom": ("Point", [float(x), float(y)])})
        return mark

    def set_feature_attribute(self, name: str, index: int, key: str, value) -> dict | None:
        """Change one attribute of a feature; ``etichetta`` also changes the label on the map."""
        lay = self.layers.get(name)
        if lay is None or not 0 <= index < len(lay["features"]):
            return None
        item, label_item, attrs = lay["features"][index]
        attrs[key] = value
        item.setData(1, attrs)
        if key == "etichetta" and label_item is not None:
            label_item.setText(str(value))
        self._changed()
        return attrs

    def features(self, name: str) -> list[dict]:
        """Attribute dicts (with the hidden ``_geom``) of a layer, in drawing order."""
        lay = self.layers.get(name)
        return [attrs for _i, _l, attrs in lay["features"]] if lay else []

    def set_layer_visible(self, name: str, visible: bool) -> None:
        lay = self.layers.get(name)
        if lay is None:
            return
        lay["visible"] = bool(visible)
        self._snap_cache = None
        for item, label_item, _ in lay["features"]:
            item.setVisible(visible)
            if label_item is not None:
                label_item.setVisible(visible)

    def remove_layer(self, name: str) -> None:
        lay = self.layers.pop(name, None)
        if lay is None:
            return
        self.layer_order.remove(name)
        self._snap_cache = None
        gone = {id(f[0]) for f in lay["features"]}
        if any(id(it) in gone for it in self.selection):
            self.select_items([it for it in self.selection if id(it) not in gone])
        for item, label_item, _ in lay["features"]:
            for it in (item, label_item):
                if it is not None:
                    self.scene().removeItem(it)
                    if it in self.overlays:
                        self.overlays.remove(it)
        self._changed()

    def layer_rect(self, name: str) -> QRectF | None:
        lay = self.layers.get(name)
        if not lay or not lay["features"]:
            return None
        rect = QRectF()
        for item, _, _ in lay["features"]:
            rect = rect.united(item.sceneBoundingRect())
        return rect

    def zoom_to(self, rect: QRectF | None, margin: float = 0.25) -> None:
        if rect is None or rect.isNull():
            return
        m = max(rect.width(), rect.height(), 20) * margin
        self.fitInView(rect.adjusted(-m, -m, m, m), Qt.AspectRatioMode.KeepAspectRatio)

    def zoom_to_layer(self, name: str) -> None:
        self.zoom_to(self.layer_rect(name))

    def zoom_to_feature(self, name: str, index: int) -> None:
        lay = self.layers.get(name)
        if lay and 0 <= index < len(lay["features"]):
            self.zoom_to(lay["features"][index][0].sceneBoundingRect())

    def feature_at(self, scene_pos: QPointF, radius_px: float = 8.0):
        """(layer, attributes) of the topmost visible feature under a scene position."""
        r = radius_px / max(self.transform().m11(), 1e-9)
        area = QRectF(scene_pos.x() - r, scene_pos.y() - r, 2 * r, 2 * r)
        for it in self.scene().items(area):
            layer = it.data(0)
            if layer and it.isVisible():
                lay = self.layers.get(layer, {"features": []})
                idx = next((i for i, f in enumerate(lay["features"]) if f[0] is it), -1)
                attrs = dict(it.data(1) or {})
                attrs["_index"] = idx
                return layer, attrs
        return None

    def clear_overlays(self) -> None:
        self.clear_selection()
        self._snap_cache = None
        for it in self.overlays:
            self.scene().removeItem(it)
        self.overlays = []
        self.layers, self.layer_order = {}, []
        self._changed()

    def set_mode(self, mode: str) -> None:
        if mode != "select" and self.selection:
            self.clear_selection()
        self.mode = mode
        self._clicks, self._click_xy = [], []
        self._stroking = False
        self._show_snap(None, None)
        if self._rubber is not None:
            self.scene().removeItem(self._rubber)
            self._rubber = None
        self._end_band()
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag if mode == "pan" else QGraphicsView.DragMode.NoDrag)
        cursor = {"pan": Qt.CursorShape.OpenHandCursor, "select": Qt.CursorShape.ArrowCursor}
        self.viewport().setCursor(cursor.get(mode, Qt.CursorShape.CrossCursor))

    # ----------------------------------------------------------- snapping
    def _snap_data(self):
        """Vertices and segments of the visible layers, in world coordinates (cached)."""
        if self._snap_cache is None:
            verts, segs = [], []
            for lay in self.layers.values():
                if not lay["visible"]:
                    continue
                for _item, _label, attrs in lay["features"]:
                    gtype, coords = attrs.get("_geom", (None, None))
                    if gtype is None:
                        continue
                    if gtype == "Point":
                        verts.append(np.asarray([coords], float)[:, :2])
                        continue
                    c = np.asarray(coords, float)[:, :2]
                    if gtype == "Polygon" and len(c) > 2 and not np.allclose(c[0], c[-1]):
                        c = np.vstack([c, c[:1]])
                    verts.append(c)
                    if len(c) >= 2:
                        segs.append(np.stack([c[:-1], c[1:]], axis=1))
            self._snap_cache = (np.vstack(verts) if verts else np.zeros((0, 2)),
                                np.vstack(segs) if segs else np.zeros((0, 2, 2)))
        return self._snap_cache

    def snap(self, scene_pos: QPointF):
        """``(position, kind)``: the nearest vertex (preferred) or point on a segment within
        ``snap_px`` screen pixels, ``kind`` "vertex"/"segment"; the position itself otherwise."""
        pos, kind, _xy = self.snap_world(scene_pos)
        return pos, kind

    def snap_world(self, scene_pos: QPointF):
        """:meth:`snap` plus the world coordinates of the result: those of the vertex or of the
        point on the segment exactly (a round trip through the view would move them by a
        fraction of a nanometre, enough to leave a gap between units that share an edge)."""
        p = np.array(self.world(scene_pos))
        if self.tr is None or not (self.snap_vertex or self.snap_segment):
            return scene_pos, None, (float(p[0]), float(p[1]))
        tol = self.snap_px / max(self.transform().m11(), 1e-12) * abs(self.tr.a)  # screen px -> world units
        verts, segs = self._snap_data()
        if self.snap_vertex and len(verts):
            d = np.hypot(*(verts - p).T)
            i = int(np.argmin(d))
            if d[i] <= tol:
                return self.scene_pt(*verts[i]), "vertex", (float(verts[i][0]), float(verts[i][1]))
        if self.snap_segment and len(segs):
            a, b = segs[:, 0], segs[:, 1]
            near = ((np.minimum(a[:, 0], b[:, 0]) - tol <= p[0]) & (p[0] <= np.maximum(a[:, 0], b[:, 0]) + tol)
                    & (np.minimum(a[:, 1], b[:, 1]) - tol <= p[1]) & (p[1] <= np.maximum(a[:, 1], b[:, 1]) + tol))
            if near.any():
                a, b = a[near], b[near]
                ab = b - a
                t = np.clip(np.sum((p - a) * ab, axis=1) / np.maximum(np.sum(ab * ab, axis=1), 1e-24), 0.0, 1.0)
                q = a + t[:, None] * ab
                d = np.hypot(*(q - p).T)
                i = int(np.argmin(d))
                if d[i] <= tol:
                    return self.scene_pt(*q[i]), "segment", (float(q[i][0]), float(q[i][1]))
        return scene_pos, None, (float(p[0]), float(p[1]))

    def _show_snap(self, pos: QPointF | None, kind: str | None) -> None:
        """A magenta square on a vertex, a circle on a segment."""
        for k, it in list(self._snap_marks.items()):
            if it.scene() is not self.scene():
                del self._snap_marks[k]
        for k in ("vertex", "segment"):
            it = self._snap_marks.get(k)
            if k != kind or pos is None:
                if it is not None:
                    it.setVisible(False)
                continue
            if it is None:
                pen = self._pen("#ff00ff", 2.0)
                it = (self.scene().addRect(QRectF(-6, -6, 12, 12), pen) if k == "vertex"
                      else self.scene().addEllipse(QRectF(-6, -6, 12, 12), pen))
                it.setFlag(it.GraphicsItemFlag.ItemIgnoresTransformations)
                it.setZValue(10002)
                self._snap_marks[k] = it
            it.setPos(pos)
            it.setVisible(True)

    def _finish_stroke(self) -> None:
        """End of a freehand stroke: the traced polygon, simplified within half a pixel (the
        kept points are original ones, so snapped points stay on their target)."""
        self._stroking = False
        if len(self._click_xy) >= 3:
            xy = np.array(self._click_xy)
            from shapely.geometry import LineString

            tol = 0.5 / max(self.transform().m11(), 1e-12) * abs(self.tr.a)
            simple = np.asarray(LineString(xy).simplify(tol, preserve_topology=False).coords)
            if len(simple) >= 3:
                self.set_mode("polygon")
                self.polygon_finished.emit(simple)
                return
        self.set_mode(self.mode)

    # ----------------------------------------------------------- selection
    def _feature_items(self, items) -> list:
        """Feature items (not labels, highlights or the raster) among graphics items, visible only."""
        return [it for it in items if it.data(0) in self.layers and it.isVisible()]

    def item_at(self, scene_pos: QPointF, radius_px: float = 8.0):
        r = radius_px / max(self.transform().m11(), 1e-9)
        area = QRectF(scene_pos.x() - r, scene_pos.y() - r, 2 * r, 2 * r)
        found = self._feature_items(self.scene().items(area, Qt.ItemSelectionMode.IntersectsItemShape,
                                                       Qt.SortOrder.DescendingOrder, self.viewportTransform()))
        return found[0] if found else None

    def items_in_rect(self, rect: QRectF) -> list:
        return self._feature_items(self.scene().items(rect.normalized(), Qt.ItemSelectionMode.IntersectsItemShape,
                                                      Qt.SortOrder.AscendingOrder, self.viewportTransform()))

    def select_items(self, items, mode: str = "replace") -> None:
        """``mode``: replace, add or toggle."""
        items = list(dict.fromkeys(items))
        if mode == "replace":
            sel = items
        elif mode == "add":
            sel = self.selection + [it for it in items if it not in self.selection]
        else:
            sel = [it for it in self.selection if it not in items] + [it for it in items if it not in self.selection]
        self.selection = sel
        self._draw_selection()
        self.selection_changed.emit(len(self.selection))

    def select_at(self, scene_pos: QPointF, toggle: bool = False) -> None:
        it = self.item_at(scene_pos)
        if it is None:
            if not toggle:
                self.select_items([])
            return
        self.select_items([it], "toggle" if toggle else "replace")
        if len(self.selection) == 1:  # one feature: show its attributes like a click in navigation mode
            ref = self.selected_features()[0]
            self.feature_clicked.emit(ref[0], {**ref[2], "_index": ref[1]})

    def select_in_rect(self, rect: QRectF, add: bool = False) -> None:
        self.select_items(self.items_in_rect(rect), "add" if add else "replace")

    def clear_selection(self) -> None:
        if self.selection or self._sel_marks:
            self.selection = []
            self._draw_selection()
            self.selection_changed.emit(0)

    def selected_features(self) -> list[tuple[str, int, dict]]:
        """``(layer, index, attributes)`` of the selected features, in selection order."""
        out = []
        for it in self.selection:
            lay = self.layers.get(it.data(0))
            if lay is None:
                continue
            idx = next((i for i, f in enumerate(lay["features"]) if f[0] is it), -1)
            if idx >= 0:
                out.append((it.data(0), idx, lay["features"][idx][2]))
        return out

    def _draw_selection(self) -> None:
        for m in self._sel_marks:
            if m.scene() is self.scene():
                self.scene().removeItem(m)
        self._sel_marks = []
        pen = self._pen("#00e5ff", 3.0)
        pen.setStyle(Qt.PenStyle.DashLine)
        for it in self.selection:
            if isinstance(it, QGraphicsPathItem):
                polygon = self.layers.get(it.data(0), {}).get("kind") == "polygon"
                m = self.scene().addPath(it.path(), pen, QBrush(QColor(0, 229, 255, 70)) if polygon else QBrush())
            else:  # a point marker: a ring around it, constant size on screen
                m = QGraphicsEllipseItem(-10, -10, 20, 20)
                m.setPos(it.pos())
                m.setFlag(m.GraphicsItemFlag.ItemIgnoresTransformations)
                m.setPen(pen)
                self.scene().addItem(m)
            m.setZValue(10000)
            self._sel_marks.append(m)

    def _end_band(self) -> None:
        if self._band is not None and self._band.scene() is self.scene():
            self.scene().removeItem(self._band)
        self._band, self._band_origin = None, None

    def _selection_menu(self, global_pos) -> None:
        menu = QMenu(self)
        a_del = menu.addAction(f"Elimina selezionati ({len(self.selection)})")
        a_merge = menu.addAction("Unisci selezionati")
        a_merge.setEnabled(len(self.selection) > 1)
        a_clear = menu.addAction("Deseleziona tutto")
        chosen = menu.exec(global_pos)
        if chosen is a_del:
            self.delete_requested.emit()
        elif chosen is a_merge:
            self.merge_requested.emit()
        elif chosen is a_clear:
            self.clear_selection()

    def keyPressEvent(self, ev) -> None:
        if self.mode == "select":
            if ev.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selection:
                self.delete_requested.emit()
                return
            if ev.key() == Qt.Key.Key_Escape:
                self.clear_selection()
                return
        super().keyPressEvent(ev)

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
        if self.mode in self.DRAW_MODES:
            snapped, kind, xy = self.snap_world(sp)
            self._show_snap(snapped if kind else None, kind)
            if self._stroking:
                last = self.mapFromScene(self._clicks[-1])
                if (ev.position().toPoint() - last).manhattanLength() >= 3:
                    self._clicks.append(snapped)
                    self._click_xy.append(xy)
                    self._update_rubber()
            elif self._clicks:
                self._update_rubber(snapped)
        elif self._clicks:
            self._update_rubber(sp)
        if self.mode == "select" and self._band_origin is not None:
            start = self.mapFromScene(self._band_origin)
            if self._band is None and (ev.position().toPoint() - start).manhattanLength() > 4:
                pen = self._pen("#00e5ff", 1.5)
                pen.setStyle(Qt.PenStyle.DashLine)
                self._band = self.scene().addRect(QRectF(self._band_origin, sp), pen, QBrush(QColor(0, 229, 255, 40)))
                self._band.setZValue(10001)
            elif self._band is not None:
                self._band.setRect(QRectF(self._band_origin, sp).normalized())

    def mouseReleaseEvent(self, ev) -> None:
        if self._stroking and ev.button() == Qt.MouseButton.LeftButton:
            self._finish_stroke()
            return
        if self.mode == "select" and self._band_origin is not None and ev.button() == Qt.MouseButton.LeftButton:
            mods = self._press_mods
            extend = bool(mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier
                                  | Qt.KeyboardModifier.MetaModifier))
            if self._band is not None:
                rect = self._band.rect()
                self._end_band()
                self.select_in_rect(rect, add=extend)
            else:
                origin = self._band_origin
                self._end_band()
                self.select_at(origin, toggle=extend)
            return
        super().mouseReleaseEvent(ev)

    def mousePressEvent(self, ev) -> None:
        if self.mode == "pan" and self.tr is not None and ev.button() == Qt.MouseButton.LeftButton:
            hit = self.feature_at(self.mapToScene(ev.position().toPoint()))
            if hit is not None:
                self.feature_clicked.emit(*hit)
        if self.mode == "pan" or self.tr is None or ev.button() == Qt.MouseButton.MiddleButton:
            return super().mousePressEvent(ev)
        sp = self.mapToScene(ev.position().toPoint())
        if self.mode == "select":
            if ev.button() == Qt.MouseButton.LeftButton:
                self.setFocus()
                self._band_origin, self._press_mods = sp, ev.modifiers()
            elif ev.button() == Qt.MouseButton.RightButton and self.selection:
                self._selection_menu(ev.globalPosition().toPoint())
            return
        xy = self.world(sp)
        if self.mode in self.DRAW_MODES and ev.button() != Qt.MouseButton.RightButton:
            sp, _kind, xy = self.snap_world(sp)
        if self.mode == "point":
            x, y = xy
            ok = ev.button() != Qt.MouseButton.RightButton
            z = self.elevation(x, y)
            self.add_point(x, y, "#30ff60" if ok else "#ff3030", layer="Punti",
                           attrs={"tipo": "positivo" if ok else "negativo", "z": None if z is None else round(z, 3)})
            self.point_clicked.emit(x, y, 1 if ok else 0)
            return
        if ev.button() == Qt.MouseButton.RightButton:  # cancel the current drawing
            self.set_mode(self.mode)
            return
        if self.mode == "polygon" and self.freehand:  # start a stroke; release closes it
            self._clicks, self._click_xy = [sp], [xy]
            self._stroking = True
            self._update_rubber()
            return
        self._clicks.append(sp)
        self._click_xy.append(xy)
        self._update_rubber()
        if self.mode in ("line", "measure") and len(self._clicks) == 2:
            a, b = (np.array(p) for p in self._click_xy)
            if self.mode == "line":
                self.line_finished.emit(a, b)
            else:
                za, zb = self.elevation(*a), self.elevation(*b)
                txt = f"Distanza {np.linalg.norm(b - a):.3f} m"
                if za is not None and zb is not None:
                    txt += f", dislivello {zb - za:+.3f} m, distanza inclinata {np.sqrt(np.sum((b - a) ** 2) + (zb - za) ** 2):.3f} m"
                self.measured.emit(txt)
            self.set_mode(self.mode)

    def mouseDoubleClickEvent(self, ev) -> None:
        if self.mode == "polygon" and len(self._click_xy) >= 3:
            xy = np.array(self._click_xy)
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
