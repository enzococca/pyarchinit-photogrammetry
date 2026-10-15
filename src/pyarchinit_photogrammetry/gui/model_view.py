"""3D view: VTK (pyvista) when an OpenGL context is available, otherwise a
software point-splatting renderer (works everywhere, also headless)."""
from __future__ import annotations

import os

import numpy as np
from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
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
        size = 3 if (preview and self.preview is not None) else 2
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

    def set_scene(self, sd: SceneData, reset: bool = True) -> None:
        import pyvista as pv

        self.sd = sd
        pl = self.plotter
        pl.clear()
        if sd.points is not None and len(sd.points):
            cloud = pv.PolyData(sd.points)
            cloud["rgb"] = sd.colors
            pl.add_mesh(cloud, scalars="rgb", rgb=True, point_size=2.5, render_points_as_spheres=False)
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

    def close(self):
        try:
            self.plotter.close()
        finally:
            return super().close()


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
    """The Model tab: view selector, display toggles and the canvas."""

    view_changed = Signal(str)

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
        self.cb_cams.toggled.connect(lambda _: self.refresh(reset=False))
        self.cb_marks.toggled.connect(lambda _: self.refresh(reset=False))
        b_reset = QPushButton("Vista iniziale")
        b_top = QPushButton("Dall'alto")
        bar.addWidget(QLabel("Mostra:"))
        bar.addWidget(self.combo)
        bar.addWidget(self.cb_cams)
        bar.addWidget(self.cb_marks)
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
        b_top.clicked.connect(lambda: self.canvas.top_view())
        lay.addWidget(self.canvas, 1)
        self.chunk = None
        self.kind = None
        self._filling = False

    @property
    def backend(self) -> str:
        return "vtk" if isinstance(self.canvas, VtkCanvas) else "software"

    def set_chunk(self, chunk, kind: str | None = None) -> None:
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
        self.kind = self.combo.currentData()
        self.refresh(reset=True)
        self.view_changed.emit(self.kind or "")

    def refresh(self, reset: bool = False) -> None:
        self.canvas.show_cameras = self.cb_cams.isChecked()
        self.canvas.show_markers = self.cb_marks.isChecked()
        sd = build_scene(self.chunk, self.kind) if self.chunk is not None else SceneData(np.zeros(3))
        self.canvas.set_scene(sd, reset=reset)
