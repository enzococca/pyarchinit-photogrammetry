"""3D view: VTK (pyvista) when an OpenGL context is available, otherwise a
software point-splatting renderer (works everywhere, also headless)."""
from __future__ import annotations

import os

import numpy as np
from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QSizePolicy, QSpinBox, QStackedLayout, QVBoxLayout,
                               QWidget)

from .flow_layout import FlowLayout
from .scene import OrbitCamera, SceneData, available_views, build_scene, sample_mesh_colors, splat

VIEW_LABELS = {"cameras": "Camere", "tie_points": "Punti di legame", "point_cloud": "Nuvola densa",
               "model": "Modello (ombreggiato)", "model_textured": "Modello con texture", "proxies": "Proxy delle US"}


class SoftwareCanvas(QWidget):
    """Orbit view rendered with numpy (left drag: orbit, right/middle drag: pan, wheel: zoom)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 150)
        self.setMouseTracking(True)
        self.sd = SceneData(np.zeros(3))
        self.cam = OrbitCamera(np.zeros(3), 5.0)
        self.pts = self.cols = None
        self.preview = None
        self.show_cameras = True
        self.show_markers = True
        self.show_region = True
        self.point_size = 2  # screen pixels of a point
        self._image: QImage | None = None
        self._dirty = True
        self._dragging = False
        self._last = None
        self._full_timer = QTimer(self)
        self._full_timer.setSingleShot(True)
        self._full_timer.timeout.connect(self._render_full)

    # ----------------------------------------------------------- data
    def set_scene(self, sd: SceneData, reset: bool = True) -> None:
        self.sd = sd
        if sd.mesh_vertices is not None:
            self.pts, self.cols = sample_mesh_colors(sd)
        elif sd.points is not None:
            self.pts, self.cols = sd.points, sd.colors
        else:
            self.pts = self.cols = None
        if self.pts is not None and len(self.pts) > 80_000:
            idx = np.random.default_rng(2).choice(len(self.pts), 80_000, replace=False)
            self.preview = (self.pts[idx], self.cols[idx])
        else:
            self.preview = None
        if reset:
            self.reset_view()
        self._dirty = True
        self.update()

    def set_point_size(self, px: float) -> None:
        self.point_size = max(1, int(round(px)))
        self._dirty = True
        self.update()

    def reset_view(self) -> None:
        self.cam = OrbitCamera(self.sd.center.astype(float), self.sd.extent * 1.1, up=self.sd.up)
        self._dirty = True
        self.update()

    def top_view(self) -> None:
        self.cam.pitch = 89.5
        self.cam.yaw = -90.0
        self._dirty = True
        self.update()

    # ----------------------------------------------------------- render
    def _render(self, preview: bool) -> None:
        w, h = max(self.width(), 1), max(self.height(), 1)
        ratio = self.devicePixelRatioF()
        W, H = int(w * ratio), int(h * ratio)
        pts, cols = (self.preview if (preview and self.preview is not None) else (self.pts, self.cols))
        base = max(1, int(round(self.point_size * ratio)))
        size = base + 1 if (preview and self.preview is not None) else base
        img = splat(pts, cols, self.cam, W, H, size=size)
        self._image = QImage(img.data, W, H, 3 * W, QImage.Format.Format_RGB888).copy()
        self._image.setDevicePixelRatio(ratio)
        self._dirty = False

    def _render_full(self) -> None:
        self._dirty = True
        self._dragging = False
        self.update()

    def paintEvent(self, _ev) -> None:
        if self._dirty or self._image is None or self._image.size() != self.size() * self.devicePixelRatioF():
            self._render(self._dragging)
        p = QPainter(self)
        p.drawImage(0, 0, self._image)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if self.show_cameras and self.sd.cameras:
            p.setPen(QPen(QColor(90, 170, 255), 1))
            for fr in self.sd.cameras:
                x, y, _, ok = self.cam.project(fr, w, h)
                if not ok.all():
                    continue
                q = [QPointF(float(a), float(b)) for a, b in zip(x, y)]
                for i in range(1, 5):
                    p.drawLine(q[0], q[i])
                for i in range(1, 5):
                    p.drawLine(q[i], q[1 + i % 4])
        if self.show_markers and self.sd.markers:
            p.setFont(QFont(self.font().family(), 9))
            for label, xyz, check in self.sd.markers:
                x, y, _, ok = self.cam.project(xyz[None], w, h)
                if not ok[0]:
                    continue
                col = QColor(255, 160, 40) if check else QColor(80, 220, 90)
                p.setPen(QPen(col, 2))
                pt = QPointF(float(x[0]), float(y[0]))
                p.drawLine(pt, pt + QPointF(0, -16))
                p.drawLine(pt + QPointF(0, -16), pt + QPointF(9, -12))
                p.drawLine(pt + QPointF(9, -12), pt + QPointF(0, -8))
                p.drawText(pt + QPointF(11, -10), label)
            for text, a, b, check in self.sd.scalebars:  # scale bars between their markers
                x, y, _, ok = self.cam.project(np.array([a, b]), w, h)
                if not ok.all():
                    continue
                col = QColor(255, 160, 40) if check else QColor(0, 229, 255)
                pen = QPen(col, 2)
                pen.setStyle(Qt.PenStyle.DashLine if check else Qt.PenStyle.SolidLine)
                p.setPen(pen)
                pa, pb = QPointF(float(x[0]), float(y[0])), QPointF(float(x[1]), float(y[1]))
                p.drawLine(pa, pb)
                p.drawText(pa + (pb - pa) * 0.4 + QPointF(6, -6), text)  # off the middle: crossing bars
        if self.show_region and self.sd.region is not None:  # the reconstruction region
            x, y, _, ok = self.cam.project(self.sd.region, w, h)
            pen = QPen(QColor(255, 210, 0), 1.5)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            from ..engine.region import Region

            for a, b in Region.EDGES:
                if ok[a] and ok[b]:
                    p.drawLine(QPointF(float(x[a]), float(y[a])), QPointF(float(x[b]), float(y[b])))
        p.setPen(QColor(200, 200, 200))
        p.drawText(8, h - 8, f"{VIEW_LABELS.get(self.sd.kind, self.sd.kind)} - renderer software")
        p.end()

    # ----------------------------------------------------------- interaction
    def mousePressEvent(self, ev) -> None:
        self._last = ev.position()
        self._dragging = True

    def mouseMoveEvent(self, ev) -> None:
        if self._last is None or not ev.buttons():
            return
        d = ev.position() - self._last
        self._last = ev.position()
        if ev.buttons() & Qt.MouseButton.LeftButton and not (ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.cam.yaw -= d.x() * 0.4
            self.cam.pitch = float(np.clip(self.cam.pitch + d.y() * 0.4, -89.5, 89.5))
        else:
            R = self.cam.basis()
            k = self.cam.distance * 2 * np.tan(np.radians(self.cam.fov) / 2) / max(self.height(), 1)
            self.cam.target = self.cam.target - R[0] * d.x() * k - R[1] * d.y() * k
        self._dirty = True
        self.update()

    def mouseReleaseEvent(self, _ev) -> None:
        self._last = None
        self._full_timer.start(120)

    def wheelEvent(self, ev) -> None:
        steps = ev.angleDelta().y() / 120.0
        self.cam.distance *= 0.88 ** steps
        self._dragging = True
        self._dirty = True
        self.update()
        self._full_timer.start(200)

    def snapshot(self):
        """The current frame and the projection of scene points to widget pixels at this moment:
        ``(QImage, project(pts) -> (x, y, depth, ok))``."""
        import copy

        cam = copy.deepcopy(self.cam)
        w, h = max(self.width(), 1), max(self.height(), 1)
        return self.grab().toImage(), lambda pts: cam.project(pts, w, h)

    def view_ray(self, x: float, y: float):
        """Line of sight through widget pixel (x, y): origin and unit direction in scene
        coordinates, and the size of a pixel at a distance (a function of the distance)."""
        w, h = max(self.width(), 1), max(self.height(), 1)
        f = 0.5 * h / np.tan(np.radians(self.cam.fov) / 2)
        d = self.cam.basis().T @ np.array([(x - w / 2) / f, (y - h / 2) / f, 1.0])
        return self.cam.eye.copy(), d / np.linalg.norm(d), (lambda dist: dist / f)

    def grab_array(self) -> np.ndarray:
        img = self.grab().toImage().convertToFormat(QImage.Format.Format_RGB888)
        ptr = img.constBits()
        arr = np.frombuffer(ptr, np.uint8, img.sizeInBytes()).reshape(img.height(), img.bytesPerLine())
        return arr[:, : img.width() * 3].reshape(img.height(), img.width(), 3).copy()


class VtkCanvas(QWidget):
    """pyvista/pyvistaqt view (hardware OpenGL)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from pyvistaqt import QtInteractor

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plotter = QtInteractor(self)
        self.plotter.set_background("#343840", top="#1f2228")
        lay.addWidget(self.plotter.interactor)
        self.sd = SceneData(np.zeros(3))
        self.show_cameras = True
        self.show_markers = True
        self.show_region = True
        self.point_size = 2.0  # screen pixels of a point
        self._cloud_actor = None

    def set_point_size(self, px: float) -> None:
        self.point_size = float(px)
        if self._cloud_actor is not None:
            self._cloud_actor.GetProperty().SetPointSize(self.point_size)
            self.plotter.render()

    def set_scene(self, sd: SceneData, reset: bool = True) -> None:
        import pyvista as pv

        self.sd = sd
        pl = self.plotter
        pl.clear()
        self._cloud_actor = None
        if sd.points is not None and len(sd.points):
            cloud = pv.PolyData(sd.points)
            cloud["rgb"] = sd.colors
            self._cloud_actor = pl.add_mesh(cloud, scalars="rgb", rgb=True, point_size=self.point_size,
                                            render_points_as_spheres=False, show_scalar_bar=False)
        if sd.mesh_vertices is not None:
            faces = np.hstack([np.full((len(sd.mesh_faces), 1), 3), sd.mesh_faces]).astype(np.int64).ravel()
            mesh = pv.PolyData(sd.mesh_vertices, faces)
            if sd.mesh_uv is not None and sd.texture is not None:
                mesh.active_texture_coordinates = sd.mesh_uv
                pl.add_mesh(mesh, texture=pv.read_texture(str(sd.texture)), lighting=False)
            elif sd.mesh_colors is not None:
                mesh["rgb"] = sd.mesh_colors
                pl.add_mesh(mesh, scalars="rgb", rgb=True, smooth_shading=True)
            else:
                pl.add_mesh(mesh, color="#d6d0c4", smooth_shading=True, specular=0.2)
        if self.show_cameras and sd.cameras:
            pts, lines = [], []
            for fr in sd.cameras:
                b = len(pts)
                pts.extend(fr)
                for i in range(1, 5):
                    lines += [2, b, b + i, 2, b + i, b + 1 + i % 4]
            pl.add_mesh(pv.PolyData(np.asarray(pts), lines=np.asarray(lines)), color="#5aaaff", line_width=1)
        if self.show_markers and sd.markers:
            xyz = np.array([m[1] for m in sd.markers])
            pl.add_point_labels(xyz, [m[0] for m in sd.markers], point_color="#50dc5a", point_size=10,
                                font_size=12, text_color="white", shape_opacity=0.3, render_points_as_spheres=True)
        if self.show_markers and sd.scalebars:  # scale bars between their markers
            for check in (False, True):
                bars = [s for s in sd.scalebars if s[3] == check]
                if not bars:
                    continue
                ends = np.array([e for s in bars for e in (s[1], s[2])])
                lines = np.hstack([[2, 2 * i, 2 * i + 1] for i in range(len(bars))])
                pl.add_mesh(pv.PolyData(ends, lines=lines), color="#ffa028" if check else "#00e5ff", line_width=3)
                pl.add_point_labels(np.array([s[1] + 0.4 * (s[2] - s[1]) for s in bars]), [s[0] for s in bars],
                                    point_size=1, font_size=11, text_color="#ffa028" if check else "#00e5ff",
                                    shape_opacity=0.25, always_visible=True)
        if self.show_region and sd.region is not None:  # the reconstruction region
            from ..engine.region import Region

            lines = np.hstack([[2, a, b] for a, b in Region.EDGES])
            pl.add_mesh(pv.PolyData(np.asarray(sd.region, np.float64), lines=lines), color="#ffd200", line_width=2)
        if reset:
            self.reset_view()
        pl.render()

    def reset_view(self) -> None:
        pl = self.plotter
        c = self.sd.center
        up = self.sd.up
        d = self.sd.extent * 1.1
        side = np.cross(up, [1.0, 0, 0]) if abs(up[0]) < 0.9 else np.cross(up, [0, 1.0, 0])
        eye = c + (0.55 * up - 0.8 * side / np.linalg.norm(side)) * d
        pl.camera_position = [tuple(eye), tuple(c), tuple(up)]
        pl.reset_camera()  # keep the direction, fit everything
        pl.render()

    def top_view(self) -> None:
        c, up = self.sd.center, self.sd.up
        self.plotter.camera_position = [tuple(c + up * self.sd.extent * 1.2), tuple(c), (0, 1, 0)]
        self.plotter.reset_camera()
        self.plotter.render()

    def grab_array(self) -> np.ndarray:
        return np.asarray(self.plotter.screenshot(return_img=True))[:, :, :3]

    def snapshot(self):
        """The current frame and the projection of scene points to widget pixels (VTK camera)."""
        ren = self.plotter.renderer
        cam = ren.GetActiveCamera()
        w, h = max(self.width(), 1), max(self.height(), 1)
        m = cam.GetCompositeProjectionTransformMatrix(ren.GetTiledAspectRatio(), -1, 1)
        M = np.array([[m.GetElement(i, j) for j in range(4)] for i in range(4)])
        arr = np.ascontiguousarray(self.grab_array())
        img = QImage(arr.data, arr.shape[1], arr.shape[0], 3 * arr.shape[1], QImage.Format.Format_RGB888).copy()
        img = img.scaled(w, h)

        def project(pts):
            P = np.c_[np.asarray(pts, float), np.ones(len(pts))] @ M.T
            wv = P[:, 3]
            with np.errstate(divide="ignore", invalid="ignore"):
                ndc = P[:, :3] / wv[:, None]
            ok = (wv > 0) & (np.abs(ndc[:, 0]) <= 1) & (np.abs(ndc[:, 1]) <= 1) & (np.abs(ndc[:, 2]) <= 1)
            return (ndc[:, 0] + 1) / 2 * w, (1 - ndc[:, 1]) / 2 * h, wv, ok

        return img, project

    def view_ray(self, x: float, y: float):
        """Line of sight through widget pixel (x, y) (scene coordinates) and the size of a pixel
        at a distance, from the same projection as :meth:`snapshot`."""
        ren = self.plotter.renderer
        cam = ren.GetActiveCamera()
        w, h = max(self.width(), 1), max(self.height(), 1)
        m = cam.GetCompositeProjectionTransformMatrix(ren.GetTiledAspectRatio(), -1, 1)
        inv = np.linalg.inv(np.array([[m.GetElement(i, j) for j in range(4)] for i in range(4)]))
        nx, ny = 2 * x / w - 1, 1 - 2 * y / h
        a, b = inv @ np.array([nx, ny, -1.0, 1.0]), inv @ np.array([nx, ny, 1.0, 1.0])
        a, b = a[:3] / a[3], b[:3] / b[3]
        d = (b - a) / np.linalg.norm(b - a)
        if cam.GetParallelProjection():
            size = 2 * cam.GetParallelScale() / h
            return a, d, (lambda dist: size)
        k = 2 * np.tan(np.radians(cam.GetViewAngle()) / 2) / h
        return a, d, (lambda dist: dist * k)

    def close(self):
        try:
            self.plotter.close()
        finally:
            return super().close()


class LassoOverlay(QWidget):
    """A frozen frame of the 3D view on which a lasso is drawn: click for vertices (or drag for
    a freehand line), double click or Enter to close, right click removes the last vertex,
    Esc cancels."""

    finished = Signal(object)  # list of (x, y) in widget pixels, or None when cancelled

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.image: QImage | None = None
        self.pts: list[QPointF] = []
        self.cursor_pos: QPointF | None = None
        self._down = False

    def start(self, image: QImage) -> None:
        self.image, self.pts, self.cursor_pos = image, [], None
        self.setFocus()
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        if self.image is not None:
            p.drawImage(self.rect(), self.image)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pts = self.pts + ([self.cursor_pos] if self.cursor_pos is not None and self.pts else [])
        if len(pts) >= 2:
            path = QPainterPath()
            path.addPolygon(QPolygonF(pts))
            path.closeSubpath()
            p.fillPath(path, QBrush(QColor(0, 229, 255, 50)))
            pen = QPen(QColor("#00e5ff"), 2)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawPath(path)
        p.setPen(QColor("white"))
        p.drawText(10, 20, "Lazo: clic per i vertici (o trascina), doppio clic o Invio per chiudere, "
                           "clic destro toglie l'ultimo, Esc annulla")
        p.end()

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.RightButton:
            if self.pts:
                self.pts.pop()
        elif ev.button() == Qt.MouseButton.LeftButton:
            self.pts.append(ev.position())
            self._down = True
        self.update()

    def mouseMoveEvent(self, ev) -> None:
        self.cursor_pos = ev.position()
        if self._down and self.pts and (ev.position() - self.pts[-1]).manhattanLength() > 4:
            self.pts.append(ev.position())  # freehand
        self.update()

    def mouseReleaseEvent(self, ev) -> None:
        self._down = False

    def mouseDoubleClickEvent(self, ev) -> None:
        self._close()

    def keyPressEvent(self, ev) -> None:
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._close()
        elif ev.key() == Qt.Key.Key_Escape:
            self.pts = []
            self.finished.emit(None)

    def _close(self) -> None:
        pts = [(p.x(), p.y()) for p in self.pts]
        self.pts = []
        self.finished.emit(pts if len(pts) >= 3 else None)


class PickOverlay(QWidget):
    """A frozen frame of the 3D view on which one point is clicked (a new marker); right click or
    Esc cancels."""

    finished = Signal(object)  # (x, y) in widget pixels, or None when cancelled

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.image: QImage | None = None
        self.text = ""

    def start(self, image: QImage, text: str) -> None:
        self.image, self.text = image, text
        self.setFocus()
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        if self.image is not None:
            p.drawImage(self.rect(), self.image)
        p.setPen(QColor("white"))
        p.drawText(10, 20, self.text)
        p.end()

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self.finished.emit((ev.position().x(), ev.position().y()))
        else:
            self.finished.emit(None)

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key.Key_Escape:
            self.finished.emit(None)


class ClassLegend(QListWidget):
    """The classes of the cloud with their colour and number of points; unticked = hidden."""

    visibility_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMaximumWidth(240)
        self.itemChanged.connect(lambda _it: None if self._filling else self.visibility_changed.emit())
        self._filling = False

    mode = "class"  # what the rows are: "class" codes or "confidence" ranges

    def set_counts(self, counts: dict, hidden: set, labels: dict | None = None) -> None:
        from .scene import class_name, class_palette

        pal = class_palette()
        self._fill("class", [(code, f"{code}  {class_name(code, labels)}", pal[code], counts[code])
                             for code in sorted(counts)], hidden)

    def set_bins(self, counts: dict, hidden: set) -> None:
        """Rows for the confidence ranges (depth maps agreeing on a point or vertex)."""
        from .scene import CONFIDENCE_COLORS, confidence_legend

        self._fill("confidence", [(b, label, CONFIDENCE_COLORS[b], counts.get(b, 0))
                                  for b, label in confidence_legend() if counts.get(b, 0)], hidden)

    def _fill(self, mode: str, rows, hidden: set) -> None:
        self.mode = mode
        self._filling = True
        self.clear()
        for key, text, rgb, n in rows:
            pm = QPixmap(14, 14)
            pm.fill(QColor(*[int(v) for v in rgb]))
            it = QListWidgetItem(QIcon(pm), f"{text}  ({n:,})".replace(",", "."))
            it.setData(Qt.ItemDataRole.UserRole, int(key))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked if key in hidden else Qt.CheckState.Checked)
            self.addItem(it)
        self._filling = False

    def hidden(self) -> set[int]:
        return {self.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.count())
                if self.item(i).checkState() != Qt.CheckState.Checked}


def _vtk_usable() -> bool:
    choice = os.environ.get("PPG_RENDERER", "").lower()
    if choice == "software":
        return False
    if QApplication.platformName() in ("offscreen", "minimal") and choice != "vtk":
        return False
    try:
        import pyvistaqt  # noqa: F401
        import vtkmodules.vtkRenderingOpenGL2  # noqa: F401  (not "import vtk": the full package loads every module)
    except Exception:
        return False
    return True


class ModelView(QWidget):
    """The Model tab: view selector, display toggles and the canvas; point clouds can be
    coloured by class (with a legend to hide classes) and points selected with a lasso."""

    view_changed = Signal(str)
    points_selected = Signal(object, object)  # boolean masks over the whole cloud: visible only, all inside
    faces_selected = Signal(object, object)  # boolean masks over the faces of the model: visible only, all inside
    marker_requested = Signal(object)  # a point of the model or the cloud (internal frame), None: nothing there
    photos_point_requested = Signal(object)  # filter the photos that see this point (internal frame)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        bar = FlowLayout()
        self.combo = QComboBox()
        self.combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.combo.currentIndexChanged.connect(self._combo_changed)
        self.cb_cams = QCheckBox("Camere")
        self.cb_cams.setChecked(True)
        self.cb_marks = QCheckBox("Marker")
        self.cb_marks.setChecked(True)
        self.cb_region = QCheckBox("Regione")
        self.cb_region.setChecked(True)
        self.cb_region.setToolTip("Il riquadro giallo della regione di ricostruzione (Flusso di lavoro > Regione)")
        self.cb_cams.toggled.connect(lambda _: self.refresh(reset=False))
        self.cb_marks.toggled.connect(lambda _: self.refresh(reset=False))
        self.cb_region.toggled.connect(lambda _: self.refresh(reset=False))
        b_reset = QPushButton("Vista iniziale")
        b_top = QPushButton("Dall'alto")
        self.color_combo = QComboBox()
        self._color_modes: tuple = ()
        self._wanted_mode = "rgb"  # the colours asked for: kept while a view without them is shown
        self._filling = False
        self._fill_color_combo("point_cloud")
        self.color_combo.setToolTip("Colori: quelli delle foto, le classi (con la legenda per nasconderle), la quota "
                                    "o la confidenza (quante mappe di profondita' confermano ogni punto o vertice)")
        self.color_combo.currentIndexChanged.connect(self._color_chosen)
        self.point_spin = QSpinBox()
        self.point_spin.setRange(1, 8)
        self.point_spin.setValue(2)
        self.point_spin.setSuffix(" px")
        self.point_spin.setToolTip("Dimensione dei punti sullo schermo")
        self.point_label = QLabel("Punti:")
        self.b_lasso = QPushButton("Lazo")
        self.b_lasso.setToolTip("Seleziona con un lazo punti della nuvola (classe, elimina, tieni) o facce del "
                                "modello (elimina, tieni)")
        self.b_lasso.clicked.connect(self.start_lasso)
        self.b_marker = QPushButton("Marker")
        self.b_marker.setToolTip("Aggiungi un marker: clic su un punto del modello o della nuvola, come in Metashape. "
                                 "Si proietta sulle foto che lo vedono; con due marker si fa una barra di scala")
        self.b_marker.clicked.connect(lambda: self.start_pick("marker"))
        self.b_photos = QPushButton("Foto del punto")
        self.b_photos.setToolTip("Clic su un punto del modello o della nuvola: le foto che lo vedono, come Filter "
                                 "Photos by Point di Metashape")
        self.b_photos.clicked.connect(lambda: self.start_pick("photos"))
        self.version_label = QLabel("")
        self.version_label.setObjectName("versionLabel")
        self.version_label.setStyleSheet("color: #b45309; font-weight: bold")
        self.version_label.setVisible(False)
        self.info_label = QLabel("Senza confidenza: Flusso di lavoro > Calcola la confidenza")
        self.info_label.setStyleSheet("color: #b45309")
        self.info_label.setVisible(False)
        bar.addWidget(QLabel("Mostra:"))
        bar.addWidget(self.combo)
        bar.addWidget(self.version_label)
        bar.addWidget(self.color_combo)
        bar.addWidget(self.point_label)
        bar.addWidget(self.point_spin)
        bar.addWidget(self.info_label)
        bar.addWidget(self.b_lasso)
        bar.addWidget(self.b_marker)
        bar.addWidget(self.b_photos)
        bar.addWidget(self.cb_cams)
        bar.addWidget(self.cb_marks)
        bar.addWidget(self.cb_region)
        bar.addWidget(b_top)
        bar.addWidget(b_reset)
        lay.addLayout(bar)
        self.canvas = None
        if _vtk_usable():
            try:
                self.canvas = VtkCanvas(self)
            except Exception:
                self.canvas = None
        if self.canvas is None:
            self.canvas = SoftwareCanvas(self)
        b_reset.clicked.connect(lambda: self.canvas.reset_view())
        self.point_spin.valueChanged.connect(lambda v: self.canvas.set_point_size(v))
        b_top.clicked.connect(lambda: self.canvas.top_view())
        self.lasso = LassoOverlay(self)
        self.lasso.finished.connect(self._lasso_finished)
        self.picker = PickOverlay(self)
        self.picker.finished.connect(self._pick_finished)
        holder = QWidget()
        self.stack = QStackedLayout(holder)
        self.stack.addWidget(self.canvas)
        self.stack.addWidget(self.lasso)
        self.stack.addWidget(self.picker)
        self.legend = ClassLegend()
        self.legend.visibility_changed.connect(lambda: self.refresh(reset=False))
        row = QHBoxLayout()
        row.addWidget(holder, 1)
        row.addWidget(self.legend)
        lay.addLayout(row, 1)
        self.chunk = None
        self.kind = None
        self.sd = None
        self.paths: dict = {}  # files shown instead of the active products (a kept version)
        self.highlight = None  # mask over the dense cloud shown in red (what a cleaning would remove)
        self._project = None
        self._filling = False
        self._update_tools()

    @property
    def backend(self) -> str:
        return "vtk" if isinstance(self.canvas, VtkCanvas) else "software"

    def show_version(self, kind: str, path, title: str) -> None:
        """Show a kept version (``path``) of the dense cloud or the model; choosing a view in
        the list goes back to the active products."""
        self.paths = {kind: str(path)}
        self.version_label.setText(f"Versione: {title} (non attiva)")
        self.version_label.setVisible(True)
        if self.kind == kind:
            self.refresh(reset=False)
        else:
            self._filling = True
            i = self.combo.findData(kind)
            if i >= 0:
                self.combo.setCurrentIndex(i)
            self._filling = False
            self.kind = kind
            self.refresh(reset=True)

    def _clear_version(self) -> None:
        self.paths = {}
        self.version_label.setVisible(False)

    def set_chunk(self, chunk, kind: str | None = None) -> None:
        self._clear_version()
        self.highlight = None
        self.chunk = chunk
        views = ["cameras"] + available_views(chunk) if chunk is not None else []
        self._filling = True
        self.combo.clear()
        for v in views:
            self.combo.addItem(VIEW_LABELS[v], v)
        from .scene import best_view

        kind = kind or (best_view(chunk) if chunk is not None else None)
        if kind in views:
            self.combo.setCurrentIndex(views.index(kind))
        self._filling = False
        self.kind = kind
        self.refresh(reset=True)

    def show_kind(self, kind: str) -> None:
        i = self.combo.findData(kind)
        if i >= 0:
            self.combo.setCurrentIndex(i)

    def _combo_changed(self, _i) -> None:
        if self._filling:
            return
        self._clear_version()
        self.kind = self.combo.currentData()
        self.refresh(reset=True)
        self.view_changed.emit(self.kind or "")

    COLOR_MODES = {"point_cloud": ("rgb", "class", "elevation", "confidence"),
                   "tie_points": ("rgb", "class", "elevation"), "model": ("rgb", "confidence")}
    COLOR_LABELS = {"rgb": "Colori RGB", "class": "Classi", "elevation": "Quota", "confidence": "Confidenza"}

    def _color_chosen(self, _i) -> None:
        if self._filling:
            return
        self._wanted_mode = self.color_combo.currentData() or "rgb"
        self.refresh(reset=False)

    def _fill_color_combo(self, kind) -> None:
        modes = self.COLOR_MODES.get(kind, ())
        was = self._filling
        self._filling = True
        if modes != self._color_modes:
            self.color_combo.clear()
            for key in modes:
                self.color_combo.addItem(self.COLOR_LABELS[key], key)
            self._color_modes = modes
        i = self.color_combo.findData(self._wanted_mode)  # e.g. classes asked while the model was shown
        if i >= 0:
            self.color_combo.setCurrentIndex(i)
        elif self.color_combo.currentIndex() < 0 and self.color_combo.count():
            self.color_combo.setCurrentIndex(0)
        self._filling = was

    def color_mode(self) -> str:
        return self.color_combo.currentData() or "rgb" if self.kind in self.COLOR_MODES else "rgb"

    def _update_tools(self) -> None:
        self._fill_color_combo(self.kind)
        self.color_combo.setVisible(self.kind in self.COLOR_MODES)
        points = self.kind in ("point_cloud", "tie_points")
        self.point_label.setVisible(points)
        self.point_spin.setVisible(points)
        # edits go to the active cloud or model, not to a kept version
        self.b_lasso.setVisible(self.kind in ("point_cloud", "model") and not self.paths)
        self.b_marker.setVisible(self.kind in self.PICK_KINDS)
        self.b_photos.setVisible(self.kind in self.PICK_KINDS)
        mode = self.color_mode()
        self.legend.setVisible((self.kind in ("point_cloud", "tie_points") and mode == "class")
                               or (self.kind in ("point_cloud", "model") and mode == "confidence"))

    def set_color_mode(self, mode: str) -> None:
        """Colours by ``mode``; when the current view has no such colours they are used as soon
        as a view that has them is shown."""
        self._wanted_mode = mode
        self._fill_color_combo(self.kind)
        i = self.color_combo.findData(mode)
        if i >= 0 and self.color_combo.currentIndex() != i:
            self.color_combo.setCurrentIndex(i)
        elif i >= 0:
            self.refresh(reset=False)

    def _hidden(self, mode: str) -> set:
        """Rows unticked in the legend, when the legend shows ``mode`` (classes or ranges)."""
        return self.legend.hidden() if self.legend.mode == mode and self.legend.count() else set()

    # ----------------------------------------------------------- lasso selection
    PICK_KINDS = ("point_cloud", "tie_points", "model", "model_textured")

    def start_pick(self, purpose: str = "marker") -> None:
        """Freeze the view and wait for a click on the model or the cloud: a new marker, or the
        photos that see the point (``purpose`` "photos")."""
        if self.kind not in self.PICK_KINDS or self.chunk is None or self.sd is None:
            return
        image, self._project = self.canvas.snapshot()
        self._ray = self.canvas.view_ray
        self._purpose = purpose
        what = "le foto che lo vedono" if purpose == "photos" else "un nuovo marker"
        self.picker.start(image, f"Clic su un punto del modello o della nuvola: {what} - Esc o tasto destro annulla")
        self.stack.setCurrentWidget(self.picker)

    def _pick_finished(self, pos) -> None:
        self.stack.setCurrentWidget(self.canvas)
        if pos is None or self._project is None:
            return
        point = self.pick_point(pos[0], pos[1], self._project, self._ray)
        (self.photos_point_requested if getattr(self, "_purpose", "marker") == "photos"
         else self.marker_requested).emit(point)

    def pick_point(self, x: float, y: float, project, ray=None, reach: float = 8.0):
        """The point of the surface under widget pixel (x, y), internal frame: of the points and
        vertices shown within ``reach`` pixels, the nearest to the viewer; then, along the line of
        sight, the plane of the chunk's dense cloud (or tie points) at full resolution there.
        None when nothing is shown under the cursor."""
        from ..core import geometry
        from ..markers import gcp

        sd = self.sd
        arrs = [np.asarray(a, np.float64) for a in (sd.points, sd.mesh_vertices) if a is not None and len(a)]
        if not arrs or self.chunk is None:
            return None
        pts = np.vstack(arrs)
        px, py, depth, ok = project(pts)
        d2 = (px - x) ** 2 + (py - y) ** 2
        idx = np.nonzero(ok & (d2 <= reach ** 2))[0]
        if not len(idx):
            return None
        front = idx[depth[idx] <= depth[idx].min() * 1.02 + 1e-12]  # the surface in front, not what is behind it
        picked = self.chunk.to_internal((pts[front[np.argmin(d2[front])]] + sd.offset)[None])[0]
        if ray is None:
            return picked
        origin, direction, pixel = ray(x, y)
        o, e = self.chunk.to_internal(np.array([origin + sd.offset, origin + direction + sd.offset]))
        d = (e - o) / np.linalg.norm(e - o)
        dist = float(np.linalg.norm(picked - o))
        scale = float(np.linalg.norm(e - o))  # internal units per scene unit
        radius = 6 * pixel(dist / scale) * scale  # about six pixels of the screen there
        xyz = gcp._SurfacePoints(self.chunk).xyz()
        if xyz is None or radius <= 0:
            return picked
        near = xyz[np.einsum("ij,ij->i", xyz - picked, xyz - picked) < (40 * radius) ** 2]
        for k in (1, 2, 4):
            hit = geometry.point_on_ray(near, o, d, k * radius) if len(near) >= 15 else None
            if hit is not None and np.linalg.norm(hit - picked) < 20 * radius:
                return hit
        return picked

    def start_lasso(self) -> None:
        if self.kind not in ("point_cloud", "model") or self.chunk is None:
            return
        image, self._project = self.canvas.snapshot()
        self.lasso.start(image)
        self.stack.setCurrentWidget(self.lasso)

    def _lasso_finished(self, pts) -> None:
        self.stack.setCurrentWidget(self.canvas)
        if pts is None or self._project is None or self.sd is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if self.kind == "model":
                visible, inside = self.select_faces(pts, self._project)
            else:
                visible, inside = self.select_points(pts, self._project)
        finally:
            QApplication.restoreOverrideCursor()
        (self.faces_selected if self.kind == "model" else self.points_selected).emit(visible, inside)

    @staticmethod
    def _inside_and_visible(polygon, x, y, z, ok, depth_tol: float = 0.02, cell: int = 3):
        """(visible, inside) masks of projected points: inside the screen ``polygon`` and, for
        "visible", not behind the nearest of them at that pixel (``depth_tol`` of the distance)."""
        import shapely

        poly = shapely.Polygon(polygon)
        inside = np.zeros(len(x), bool)
        idx = np.nonzero(ok)[0]
        visible = np.zeros(len(x), bool)
        if not len(idx):
            return visible, inside
        inside[idx] = shapely.contains_xy(poly, x[idx], y[idx])
        cx, cy = (x[idx] // cell).astype(np.int64), (y[idx] // cell).astype(np.int64)
        key = cx * 1_000_003 + cy
        order = np.argsort(key, kind="stable")
        ks, zs = key[order], z[idx][order]
        start = np.r_[0, np.nonzero(np.diff(ks))[0] + 1]
        zmin = np.minimum.reduceat(zs, start)
        cell_of = np.searchsorted(ks[start], key)
        visible[idx] = inside[idx] & (z[idx] <= zmin[cell_of] * (1 + depth_tol))
        return visible, inside

    def select_faces(self, polygon, project, depth_tol: float = 0.02):
        """Faces of the whole model whose centre is inside a screen ``polygon``: (visible ones,
        all of them). Faces hidden by the confidence legend are never taken."""
        mesh = self.chunk.model
        if mesh is None:
            return np.zeros(0, bool), np.zeros(0, bool)
        v, f = np.asarray(mesh.vertices, np.float64), np.asarray(mesh.faces, np.int64)
        centres = self.chunk.to_world((v[f[:, 0]] + v[f[:, 1]] + v[f[:, 2]]) / 3.0) - self.sd.offset
        x, y, z, ok = project(centres)
        hidden = self._hidden("confidence") if self.color_mode() == "confidence" else set()
        if hidden:
            from ..engine.confidence import read_model_confidence
            from .scene import confidence_bins

            conf = read_model_confidence(self.chunk.product_path("model"))
            if conf is not None and len(conf) == len(v):
                ok &= ~np.isin(confidence_bins(conf)[f], list(hidden)).any(axis=1)
        return self._inside_and_visible(polygon, x, y, z, ok, depth_tol)

    def select_points(self, polygon, project, depth_tol: float = 0.02, cell: int = 3):
        """Points of the whole dense cloud inside a screen ``polygon`` (widget pixels) for the
        projection ``project``: (visible ones, all of them). Hidden classes are never taken;
        "visible" = not behind the nearest surface at that pixel (2 % of the distance)."""
        pc = self.chunk.point_cloud
        if pc is None:
            n = 0
            return np.zeros(n, bool), np.zeros(n, bool)
        world = self.chunk.to_world(pc.xyz) - self.sd.offset
        x, y, z, ok = project(world)
        mode = self.color_mode()
        hidden = self._hidden(mode) if mode in ("class", "confidence") else set()
        codes = getattr(pc, "classification", None)
        if hidden and mode == "class" and codes is not None:
            ok &= ~np.isin(np.asarray(codes), list(hidden))
        if hidden and mode == "confidence" and pc.confidence is not None:
            from .scene import confidence_bins

            ok &= ~np.isin(confidence_bins(pc.confidence), list(hidden))
        return self._inside_and_visible(polygon, x, y, z, ok, depth_tol, cell)

    def refresh(self, reset: bool = False) -> None:
        self.canvas.show_cameras = self.cb_cams.isChecked()
        self.canvas.show_markers = self.cb_marks.isChecked()
        self.canvas.show_region = self.cb_region.isChecked()
        self._update_tools()
        mode = self.color_mode()
        hidden = self._hidden("class") if mode == "class" else set()
        bins = self._hidden("confidence") if mode == "confidence" else set()
        highlight = self.highlight if self.kind == "point_cloud" and not self.paths else None
        vtk = self.backend == "vtk"  # the graphics card draws millions of points; numpy a few hundred thousand
        sd = (build_scene(self.chunk, self.kind, color_mode=mode, hidden_classes=hidden, paths=self.paths,
                          hidden_bins=bins, highlight=highlight, max_points=3_000_000 if vtk else 400_000,
                          max_textured_faces=8_000_000 if vtk else 3_000_000)
              if self.chunk is not None else SceneData(np.zeros(3)))
        self.sd = sd
        if mode == "class":
            self.legend.set_counts(sd.class_counts, hidden, sd.class_labels)
        elif mode == "confidence":
            self.legend.set_bins(sd.bin_counts, bins)
            self.legend.setVisible(bool(sd.bin_counts))
        self.info_label.setVisible(bool(sd.confidence_missing))
        self.canvas.set_scene(sd, reset=reset)
