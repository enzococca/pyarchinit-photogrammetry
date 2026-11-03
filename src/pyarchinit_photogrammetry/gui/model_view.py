"""3D view: VTK (pyvista) when an OpenGL context is available, otherwise a
software point-splatting renderer (works everywhere, also headless)."""
from __future__ import annotations

import os
import sys

import numpy as np
from PySide6.QtCore import QLineF, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QListWidget,
                               QMenu, QListWidgetItem, QPushButton, QSizePolicy, QSpinBox, QStackedLayout, QVBoxLayout,
                               QWidget)

from .flow_layout import FlowLayout
from .i18n import decimal_comma, thousands_points, tr
from .scene import OrbitCamera, SceneData, available_views, build_scene, exaggerated, sample_mesh_colors, splat
from .trackball import MODES as TRACKBALL_MODES
from .trackball import OUTLINE_COLOUR, RING_COLOURS, SETTING as TRACKBALL_SETTING, Trackball

VIEW_LABELS = {"cameras": "Camere", "tie_points": "Punti di legame", "point_cloud": "Nuvola densa",
               "model": "Modello (ombreggiato)", "model_textured": "Modello con texture", "proxies": "Proxy delle US",
               "tin": "TIN"}


def ring_runs(xy, ok, front, half: bool) -> list:
    """The runs of consecutive segments of a closed ring (``xy`` (n, 2)) whose both ends are ``ok`` and whose first
    end is on the ``front`` side ``half``, as lists of points: each run one polyline (one painter call)."""
    n = len(xy)
    use = [bool(ok[i] and ok[(i + 1) % n] and bool(front[i]) == half) for i in range(n)]
    if not any(use):
        return []
    if all(use):
        return [[xy[i] for i in range(n)] + [xy[0]]]
    start = next(i for i in range(n) if not use[i])  # begin after a gap, so no run is split at the seam
    runs, cur = [], []
    for k in range(1, n + 1):
        i = (start + k) % n
        if use[i]:
            if not cur:
                cur = [xy[i]]
            cur.append(xy[(i + 1) % n])
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return runs


def exaggeration_text(factor: float) -> str:
    """The caption of an exaggerated view: "Esagerazione verticale x2,5"."""
    return tr("Esagerazione verticale x{0}", decimal_comma(f"{factor:g}"))


KEY_NAMES = {Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right", Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down",
             Qt.Key.Key_Plus: "plus", Qt.Key.Key_Equal: "plus", Qt.Key.Key_Minus: "minus",
             Qt.Key.Key_F: "front", Qt.Key.Key_L: "left_side", Qt.Key.Key_Home: "home", Qt.Key.Key_T: "top"}


def _icon(name: str) -> QIcon:
    from .icons import icon

    return icon(name)


class _TrackballCanvas:
    """What both canvases share: the trackball, its fading, the keys of the view."""

    def _init_trackball(self) -> None:
        self.trackball = Trackball(self)
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(50)
        self._fade_timer.timeout.connect(self._fade_tick)

    def view_size(self) -> tuple[int, int]:
        return max(self.width(), 1), max(self.height(), 1)

    def scene_up(self) -> np.ndarray:
        return np.asarray(self.sd.up, np.float64)

    def set_trackball_mode(self, mode: str) -> None:
        self.trackball.mode = mode if mode in TRACKBALL_MODES else "auto"
        self.redraw()

    def set_pivot(self, point, recentre: bool = False) -> None:
        """The centre of rotation (coordinates shown); the view stays where it is unless ``recentre``."""
        self.trackball.set_pivot(point, recentre)

    def pivot(self) -> np.ndarray:
        return self.trackball._pivot(self.get_cam()).copy()

    def centre_on_pivot(self) -> None:
        self.trackball.centre_on_pivot()

    def _mouse_seen(self, x=None, y=None) -> None:
        self.trackball.touched(x, y)
        if self.trackball.mode == "auto" and not self._fade_timer.isActive():
            self._fade_timer.start()
        self._redraw_if_changed()

    def _mouse_gone(self) -> None:
        self.trackball.left()
        self._redraw_if_changed()

    def _fade_tick(self) -> None:
        if not self.trackball.fading():
            self._fade_timer.stop()
        self._redraw_if_changed()

    def _redraw_if_changed(self) -> None:
        """Draw again only when the trackball looks different: a VTK scene of millions of points is
        not drawn again at every movement of the mouse."""
        if self._trackball_state() != getattr(self, "_tb_drawn", None):
            self.redraw()

    def _trackball_state(self) -> tuple:
        return round(self.trackball.opacity(), 2), self.trackball.hover

    def view_key(self, key, shift: bool = False) -> bool:
        """A key pressed on the view (``Qt.Key``): True when the view used it."""
        name = KEY_NAMES.get(key)
        if name == "home":
            self.reset_view()
        elif name == "top":
            self.top_view()
        elif name is None or not self.trackball.key(name, shift):
            return False
        self._mouse_seen()
        return True


class SoftwareCanvas(_TrackballCanvas, QWidget):
    """Orbit view rendered with numpy, with the trackball (left drag: rotate, right/middle drag: pan,
    wheel: zoom toward the cursor, Shift+wheel: dolly, double click: centre of rotation)."""

    pivot_requested = Signal(float, float)  # a double click: the centre of rotation at this pixel

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 150)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
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
        self._init_trackball()

    # ----------------------------------------------------------- trackball
    def get_cam(self) -> OrbitCamera:
        return self.cam

    def set_cam(self, cam: OrbitCamera) -> None:
        self.cam = cam
        self._dragging = True  # the light preview while it moves, the whole scene a moment after
        self._dirty = True
        self.update()
        self._full_timer.start(150 if self.trackball.dragging else 200)

    def redraw(self) -> None:
        self.update()

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
        self.trackball.pivot = None  # back to the middle of the scene
        self._dirty = True
        self.update()

    def top_view(self) -> None:
        self.cam.pitch = 89.5
        self.cam.yaw = -90.0
        self.cam.target = self.sd.center.astype(float)
        self.trackball.pivot = None  # back to the middle of the scene
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
        # few painter calls per frame: lines gathered and drawn with one drawLines (see image_views.polyline_path)
        if self.show_cameras and self.sd.cameras:
            p.setPen(QPen(QColor(90, 170, 255), 1))
            lines = []
            for fr in self.sd.cameras:
                x, y, _, ok = self.cam.project(fr, w, h)
                if not ok.all():
                    continue
                q = [QPointF(float(a), float(b)) for a, b in zip(x, y)]
                lines += [QLineF(q[0], q[i]) for i in range(1, 5)] + [QLineF(q[i], q[1 + i % 4]) for i in range(1, 5)]
            if lines:
                p.drawLines(lines)
        if self.show_markers and self.sd.markers:
            p.setFont(QFont(self.font().family(), 9))
            for label, xyz, check in self.sd.markers:
                x, y, _, ok = self.cam.project(xyz[None], w, h)
                if not ok[0]:
                    continue
                col = QColor(255, 160, 40) if check else QColor(80, 220, 90)
                p.setPen(QPen(col, 2))
                pt = QPointF(float(x[0]), float(y[0]))
                p.drawPolyline(QPolygonF([pt, pt + QPointF(0, -16), pt + QPointF(9, -12), pt + QPointF(0, -8)]))
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

            edges = [QLineF(float(x[a]), float(y[a]), float(x[b]), float(y[b])) for a, b in Region.EDGES
                     if ok[a] and ok[b]]
            if edges:
                p.drawLines(edges)
        p.setPen(QColor(200, 200, 200))
        p.drawText(8, h - 8, tr("{0} - renderer software", tr(VIEW_LABELS.get(self.sd.kind, self.sd.kind))))
        if self.sd.exaggeration != 1.0:
            p.drawText(8, h - 26, exaggeration_text(self.sd.exaggeration))
        self._paint_trackball(p)
        p.end()

    def _paint_trackball(self, p: QPainter) -> None:
        self._tb_drawn = self._trackball_state()
        op = self.trackball.opacity()
        if op <= 0 or (self.pts is None and not self.sd.cameras):
            return
        geo = self.trackball.geometry(self.cam)
        cx, cy = geo["centre"]
        r = geo["radius"]

        def pen(rgb, alpha, width=1.5):
            return QPen(QColor(*rgb, int(alpha * op)), width)

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(pen(OUTLINE_COLOUR, 150))
        p.drawEllipse(QPointF(cx, cy), r, r)
        for name, (_a, _pts, xy, ok, front) in geo["rings"].items():
            width = 2.6 if name == self.trackball.hover else 1.5
            for half, alpha in ((True, 220), (False, 90)):  # the front of the ring, then the back: one call each run
                p.setPen(pen(RING_COLOURS[name], alpha, width))
                for run in ring_runs(xy, ok, front, half):
                    p.drawPolyline(QPolygonF([QPointF(float(a), float(b)) for a, b in run]))
        p.setPen(pen((255, 255, 255), 230))
        p.drawLine(QPointF(cx - 6, cy), QPointF(cx + 6, cy))
        p.drawLine(QPointF(cx, cy - 6), QPointF(cx, cy + 6))

    # ----------------------------------------------------------- interaction
    def mousePressEvent(self, ev) -> None:
        pos = ev.position()
        self._last = pos
        rotate = ev.button() == Qt.MouseButton.LeftButton and not (
            ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.trackball.press(pos.x(), pos.y(), "rotate" if rotate else "pan")
        self._dragging = True

    def mouseMoveEvent(self, ev) -> None:
        pos = ev.position()
        if self._last is None or not ev.buttons():
            self._mouse_seen(pos.x(), pos.y())
            return
        self._last = pos
        self.trackball.move(pos.x(), pos.y())

    def mouseReleaseEvent(self, _ev) -> None:
        self._last = None
        self.trackball.release()
        self._full_timer.start(120)

    def mouseDoubleClickEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self.trackball.release()
            self._last = None
            self.pivot_requested.emit(ev.position().x(), ev.position().y())

    def wheelEvent(self, ev) -> None:
        delta = ev.angleDelta()
        steps = (delta.y() or delta.x()) / 120.0  # Shift turns the wheel sideways on macOS
        pos = ev.position()
        shift = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.trackball.zoom(steps, pos.x(), pos.y(), dolly=shift)
        self._mouse_seen()

    def keyPressEvent(self, ev) -> None:
        if not self.view_key(ev.key(), bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)):
            super().keyPressEvent(ev)

    def enterEvent(self, ev) -> None:
        self._mouse_seen()
        super().enterEvent(ev)

    def leaveEvent(self, ev) -> None:
        self._mouse_gone()
        super().leaveEvent(ev)

    def snapshot(self):
        """The current frame and the projection of scene points to widget pixels at this moment:
        ``(QImage, project(pts) -> (x, y, depth, ok))``."""
        import copy

        cam = copy.deepcopy(self.cam)
        w, h = max(self.width(), 1), max(self.height(), 1)
        return self._grab(), lambda pts: cam.project(pts, w, h)

    def _grab(self) -> QImage:
        """The frame without the trackball."""
        self.trackball.hidden = True
        try:
            return self.grab().toImage()
        finally:
            self.trackball.hidden = False

    def view_ray(self, x: float, y: float):
        """Line of sight through widget pixel (x, y): origin and unit direction in scene
        coordinates, and the size of a pixel at a distance (a function of the distance)."""
        w, h = max(self.width(), 1), max(self.height(), 1)
        f = 0.5 * h / np.tan(np.radians(self.cam.fov) / 2)
        d = self.cam.basis().T @ np.array([(x - w / 2) / f, (y - h / 2) / f, 1.0])
        return self.cam.eye.copy(), d / np.linalg.norm(d), (lambda dist: dist / f)

    def grab_array(self) -> np.ndarray:
        img = self._grab().convertToFormat(QImage.Format.Format_RGB888)
        ptr = img.constBits()
        arr = np.frombuffer(ptr, np.uint8, img.sizeInBytes()).reshape(img.height(), img.bytesPerLine())
        return arr[:, : img.width() * 3].reshape(img.height(), img.width(), 3).copy()


def _trackball_style(canvas):
    """A VTK interactor style that hands the mouse and the keys to the canvas's trackball (the
    default handlers of the trackball camera style are replaced by these observers)."""
    from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera

    class TrackballStyle(vtkInteractorStyleTrackballCamera):
        def __init__(self):
            super().__init__()
            for event, slot in (("LeftButtonPressEvent", self._left), ("RightButtonPressEvent", self._pan),
                                ("MiddleButtonPressEvent", self._pan), ("LeftButtonReleaseEvent", self._up),
                                ("RightButtonReleaseEvent", self._up), ("MiddleButtonReleaseEvent", self._up),
                                ("MouseMoveEvent", self._move), ("MouseWheelForwardEvent", self._wheel),
                                ("MouseWheelBackwardEvent", self._wheel), ("KeyPressEvent", self._key),
                                ("CharEvent", self._nothing), ("EnterEvent", self._enter),
                                ("LeaveEvent", self._leave)):
                self.AddObserver(event, slot)

        def _xy(self):
            iren = self.GetInteractor()
            x, y = iren.GetEventPosition()
            w, h = canvas.view_size()
            size = iren.GetRenderWindow().GetSize()
            ratio = (size[1] / h) if size[1] else 1.0
            return x / ratio, h - y / ratio

        def _left(self, _obj, _ev):
            x, y = self._xy()
            iren = self.GetInteractor()
            if iren.GetRepeatCount():  # a double click
                canvas.trackball.release()
                canvas.pivot_requested.emit(x, y)
                return
            canvas.trackball.press(x, y, "pan" if iren.GetShiftKey() else "rotate")

        def _pan(self, _obj, _ev):
            canvas.trackball.press(*self._xy(), "pan")

        def _up(self, _obj, _ev):
            canvas.trackball.release()
            canvas.redraw()

        def _move(self, _obj, _ev):
            x, y = self._xy()
            if not canvas.trackball.move(x, y):
                canvas._mouse_seen(x, y)

        def _wheel(self, _obj, ev):
            x, y = self._xy()
            steps = 1.0 if ev == "MouseWheelForwardEvent" else -1.0
            canvas.trackball.zoom(steps, x, y, dolly=bool(self.GetInteractor().GetShiftKey()))
            canvas._mouse_seen()

        def _key(self, _obj, _ev):
            iren = self.GetInteractor()
            key = {"Left": Qt.Key.Key_Left, "Right": Qt.Key.Key_Right, "Up": Qt.Key.Key_Up, "Down": Qt.Key.Key_Down,
                   "plus": Qt.Key.Key_Plus, "equal": Qt.Key.Key_Equal, "minus": Qt.Key.Key_Minus,
                   "KP_Add": Qt.Key.Key_Plus, "KP_Subtract": Qt.Key.Key_Minus, "Home": Qt.Key.Key_Home,
                   "f": Qt.Key.Key_F, "l": Qt.Key.Key_L, "t": Qt.Key.Key_T}.get((iren.GetKeySym() or "").lower()
                                                                              if len(iren.GetKeySym() or "") == 1
                                                                              else iren.GetKeySym())
            if key is not None:
                canvas.view_key(key, bool(iren.GetShiftKey()))

        def _nothing(self, _obj, _ev):  # no VTK single-letter commands (wireframe, stereo, quit...)
            pass

        def _enter(self, _obj, _ev):
            canvas._mouse_seen()

        def _leave(self, _obj, _ev):
            canvas._mouse_gone()

    return TrackballStyle()


class VtkCanvas(_TrackballCanvas, QWidget):
    """pyvista/pyvistaqt view (hardware OpenGL) with the trackball: its rings are line actors kept at
    a constant size on the screen before every render."""

    pivot_requested = Signal(float, float)
    TB_ALPHA = {"back": 90, "outline": 150, "cross": 230}  # of 255, as the software canvas draws them

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
        self._init_trackball()
        self._tb_actors = self._make_trackball_actors()
        from vtkmodules.vtkRenderingCore import vtkRenderer

        self._tb_renderer = vtkRenderer()  # a layer above the scene: the rings are never hidden by it
        self._tb_renderer.SetLayer(1)
        self._tb_renderer.SetInteractive(False)
        self._tb_renderer.SetActiveCamera(self.plotter.renderer.GetActiveCamera())
        rw = self.plotter.ren_win
        rw.SetNumberOfLayers(max(rw.GetNumberOfLayers(), 2))
        rw.AddRenderer(self._tb_renderer)
        for actor, _poly in self._tb_actors.values():
            self._tb_renderer.AddActor(actor)
        self._style = _trackball_style(self)
        self.plotter.iren.interactor.SetInteractorStyle(self._style)
        # pyvista's own keys (Up/Down zoom, +/- point size, q closes, v, b, C...) would act beside the trackball's
        self.plotter.iren.clear_key_event_callbacks()
        # Shift+wheel: macOS turns the wheel sideways and QVTKRenderWindowInteractor reads only the vertical delta
        self.plotter.interactor.installEventFilter(self)
        self.plotter.renderer.AddObserver("StartEvent", lambda *_a: self._update_trackball_actors())

    def eventFilter(self, obj, ev) -> bool:
        from PySide6.QtCore import QEvent

        if (obj is self.plotter.interactor and ev.type() == QEvent.Type.Wheel
                and ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            delta = ev.angleDelta()
            steps = (delta.y() or delta.x()) / 120.0
            if steps:
                pos = ev.position()
                self.trackball.zoom(steps, pos.x(), pos.y(), dolly=True)
                self._mouse_seen()
            return True
        return super().eventFilter(obj, ev)

    # ----------------------------------------------------------- trackball
    def get_cam(self) -> OrbitCamera:
        c = self.plotter.renderer.GetActiveCamera()
        pos, focal = np.array(c.GetPosition()), np.array(c.GetFocalPoint())
        d = float(np.linalg.norm(focal - pos)) or 1.0
        cam = OrbitCamera(focal, d, fov=c.GetViewAngle(), up=self.sd.up)
        cam.R = OrbitCamera.looking((focal - pos) / d, np.array(c.GetViewUp()))
        return cam

    def set_cam(self, cam: OrbitCamera) -> None:
        c = self.plotter.renderer.GetActiveCamera()
        c.SetPosition(*cam.eye)
        c.SetFocalPoint(*cam.target)
        c.SetViewUp(*(-cam.basis()[1]))
        self.plotter.renderer.ResetCameraClippingRange()
        self.plotter.render()

    def redraw(self) -> None:
        self.plotter.render()

    def _make_trackball_actors(self) -> dict:
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        actors = {}
        colours = {**RING_COLOURS, **{f"{k}_back": v for k, v in RING_COLOURS.items()}, "outline": OUTLINE_COLOUR,
                   "cross": (255, 255, 255)}
        for name, rgb in colours.items():
            poly = vtkPolyData()
            poly.SetPoints(vtkPoints())
            poly.SetLines(vtkCellArray())
            mapper = vtkPolyDataMapper()
            mapper.SetInputData(poly)
            actor = vtkActor()
            actor.SetMapper(mapper)
            prop = actor.GetProperty()
            prop.SetColor(*[v / 255 for v in rgb])
            prop.SetLighting(False)
            prop.SetLineWidth(1.5)
            actor.SetUseBounds(False)  # never part of the scene's extent (reset_camera)
            actor.SetPickable(False)
            actor.VisibilityOff()
            actors[name] = (actor, poly)
        return actors

    def _update_trackball_actors(self) -> None:
        from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray

        self._tb_renderer.SetActiveCamera(self.plotter.renderer.GetActiveCamera())  # a camera set anew
        self._tb_drawn = self._trackball_state()
        op = self.trackball.opacity()
        show = op > 0 and (self.sd.points is not None or self.sd.mesh_vertices is not None or bool(self.sd.cameras))
        for name, (actor, _poly) in self._tb_actors.items():
            actor.SetVisibility(show)
            actor.GetProperty().SetOpacity(op * self.TB_ALPHA.get(name.split("_")[-1], 220) / 255)
        if not show:
            return
        cam = self.get_cam()
        geo = self.trackball.geometry(cam)
        R, p, rw = cam.basis(), self.trackball.pivot, geo["world_radius"]
        t = np.linspace(0, 2 * np.pi, 96, endpoint=False)
        n = len(t)
        seg = np.arange(n)
        shapes = {}  # actor -> (points, first vertex of each segment)
        for name, (_a, flat, _xy, _ok, front) in geo["rings"].items():
            shapes[name] = (flat, seg[front])  # the half on the viewer's side, then the dimmer one behind
            shapes[f"{name}_back"] = (flat, seg[~front])
        shapes["outline"] = (p + rw * (np.outer(np.cos(t), R[0]) + np.outer(np.sin(t), R[1])), seg)
        k = rw / geo["radius"] * 6  # six pixels
        cross = np.array([p - R[0] * k, p + R[0] * k, p - R[1] * k, p + R[1] * k])
        for name, (actor, poly) in self._tb_actors.items():
            if name == "cross":
                pts, cells = cross, np.array([2, 0, 1, 2, 2, 3])
            else:
                pts, first = shapes[name]
                cells = np.column_stack([np.full(len(first), 2), first, (first + 1) % len(pts)]).ravel()
            poly.GetPoints().SetData(numpy_to_vtk(np.ascontiguousarray(pts, np.float64), deep=True))
            lines = poly.GetLines()
            lines.SetCells(len(cells) // 3, numpy_to_vtkIdTypeArray(cells.astype(np.int64), deep=True))
            poly.Modified()
            actor.GetProperty().SetLineWidth(2.6 if name.split("_")[0] == self.trackball.hover else 1.5)

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
        pl.enable_lightkit()  # clear() also takes the lights away: without them a model has no shading
        self._cloud_actor = None
        if sd.points is not None and len(sd.points):
            cloud = pv.PolyData(sd.points)
            cloud["rgb"] = sd.colors
            self._cloud_actor = pl.add_mesh(cloud, scalars="rgb", rgb=True, point_size=self.point_size,
                                            render_points_as_spheres=False, show_scalar_bar=False,
                                            lighting=False)  # points keep their own colours
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
        if sd.exaggeration != 1.0:
            pl.add_text(exaggeration_text(sd.exaggeration), position="lower_left", font_size=10, color="#c8c8c8")
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
        self.trackball.pivot = None  # back to the middle of the scene (the focal point)
        pl.render()

    def top_view(self) -> None:
        c, up = self.sd.center, self.sd.up
        self.plotter.camera_position = [tuple(c + up * self.sd.extent * 1.2), tuple(c), (0, 1, 0)]
        self.plotter.reset_camera()
        self.trackball.pivot = None  # back to the middle of the scene (the focal point)
        self.plotter.render()

    def grab_array(self) -> np.ndarray:
        self.trackball.hidden = True  # the frame without the trackball
        try:
            return np.asarray(self.plotter.screenshot(return_img=True))[:, :, :3]
        finally:
            self.trackball.hidden = False

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
            pen = QPen(QColor("#00e5ff"), 2)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(QBrush(QColor(0, 229, 255, 50)))
            p.drawPolygon(QPolygonF(pts))  # filled and outlined in one call (see image_views.polyline_path)
            p.setBrush(Qt.BrushStyle.NoBrush)
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
            it = QListWidgetItem(QIcon(pm), thousands_points(f"{text}  ({n:,})"))
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
    if choice != "vtk" and _windows_arm64():
        # Windows on ARM: OpenGL 3.2 comes from the graphics driver or from Microsoft's OpenCL, OpenGL
        # and Vulkan Compatibility Pack; without them (virtual machines) VTK cannot draw
        from .wgl import VTK_MINIMUM, opengl_version

        found = opengl_version()
        if found is not None and found[0] < VTK_MINIMUM:
            import logging

            logging.getLogger(__name__).warning(
                "OpenGL %s (%s): VTK needs 3.2, the 3D view uses the software renderer", found[1], found[2])
            return False
    return True


def _windows_arm64() -> bool:
    import platform

    return sys.platform == "win32" and platform.machine().upper() == "ARM64"


class ModelView(QWidget):
    """The Model tab: view selector, display toggles and the canvas; point clouds can be
    coloured by class (with a legend to hide classes) and points selected with a lasso."""

    view_changed = Signal(str)
    points_selected = Signal(object, object)  # boolean masks over the whole cloud: visible only, all inside
    faces_selected = Signal(object, object)  # boolean masks over the faces of the model: visible only, all inside
    marker_requested = Signal(object)  # a point of the model or the cloud (internal frame), None: nothing there
    photos_point_requested = Signal(object)  # filter the photos that see this point (internal frame)
    section_requested = Signal(object, object)  # the two ends of a section trace (internal frame)
    slice_requested = Signal(object)  # a point at the height of a plan slice (internal frame)
    move_marker_requested = Signal(int, object)  # a marker (key) to move to this point (internal frame)
    cut_requested = Signal(str, object)  # cut the model: "horizontal" (1 point), "vertical" (2), "points" (3), "values"
    elevation_requested = Signal(str, object)  # an orthophoto on a plane: "view" (view_frame()), "points" (3, internal)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        bar = FlowLayout()
        self.combo = QComboBox()
        self.combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.combo.currentIndexChanged.connect(self._combo_changed)
        self.cb_cams = QCheckBox(tr("Camere"))
        self.cb_cams.setChecked(True)
        self.cb_marks = QCheckBox("Marker")
        self.cb_marks.setChecked(True)
        self.cb_region = QCheckBox(tr("Regione"))
        self.cb_region.setChecked(True)
        self.cb_region.setToolTip(tr("Il riquadro giallo della regione di ricostruzione (Flusso di lavoro > Regione)"))
        self.cb_cams.toggled.connect(lambda _: self.refresh(reset=False))
        self.cb_marks.toggled.connect(lambda _: self.refresh(reset=False))
        self.cb_region.toggled.connect(lambda _: self.refresh(reset=False))
        b_reset = QPushButton(tr("Vista iniziale"))
        b_reset.setToolTip(tr("La vista iniziale di tutta la scena (tasto Home)"))
        b_top = QPushButton(tr("Dall'alto"))
        b_top.setToolTip(tr("La vista dall'alto (tasto T); F di fronte (verso +y), L da sinistra (verso +x)"))
        self.b_center = QPushButton(tr("Centra la vista sul punto"))
        self.b_center.setToolTip(tr("Porta al centro della vista il centro di rotazione (doppio clic su un punto "
                                    "del modello o della nuvola per sceglierlo)"))
        self.b_center.setIcon(_icon("pivot"))
        self.b_trackball = QPushButton(_icon("trackball"), "Trackball")
        self.b_trackball.setToolTip(tr("La sfera di rotazione: trascina dentro per ruotare liberamente, su un anello "
                                       "per ruotare intorno al suo asse, fuori per girare intorno alla linea di "
                                       "vista; tasto destro o centrale (o Maiusc+trascina) sposta, la rotella avvicina verso il "
                                       "cursore (Maiusc+rotella senza spostare il centro), doppio clic sceglie il "
                                       "centro di rotazione. Tasti: frecce ruotano di 15 gradi, Maiusc+frecce "
                                       "spostano, + e - avvicinano, Home vista iniziale, T dall'alto, F di fronte, "
                                       "L da sinistra"))
        from PySide6.QtGui import QAction, QActionGroup

        self.trackball_group = QActionGroup(self)
        self.trackball_actions: dict[str, QAction] = {}
        tb_menu = QMenu(self.b_trackball)
        for mode, label in (("auto", "Quando il mouse è sulla vista"), ("always", "Sempre"), ("never", "Mai")):
            a = QAction(tr(label), self, checkable=True)
            a.setData(mode)
            a.triggered.connect(lambda _=False, md=mode: self.set_trackball_mode(md))
            self.trackball_group.addAction(a)
            tb_menu.addAction(a)
            self.trackball_actions[mode] = a
        self.b_trackball.setMenu(tb_menu)
        self.exaggeration = 1.0
        self.exag_spin = QDoubleSpinBox()
        self.exag_spin.setRange(0.1, 50.0)
        self.exag_spin.setDecimals(1)
        self.exag_spin.setSingleStep(0.5)
        self.exag_spin.setValue(1.0)
        self.exag_spin.setPrefix("x")
        self.exag_spin.setKeyboardTracking(False)
        self.exag_spin.setToolTip(tr("Esagerazione verticale: le quote di ciò che si vede moltiplicate intorno al "
                                     "centro della scena, per leggere il rilievo di un terreno quasi piatto. Cambia "
                                     "solo la vista: marker, sezioni, tagli e lazo usano le coordinate vere"))
        self._exag_timer = QTimer(self)  # one rebuild of the view when the arrows are held down
        self._exag_timer.setSingleShot(True)
        self._exag_timer.timeout.connect(lambda: self.set_exaggeration(self.exag_spin.value()))
        self.exag_spin.valueChanged.connect(lambda _v: self._exag_timer.start(150))
        self.color_combo = QComboBox()
        self._color_modes: tuple = ()
        self._wanted_mode = "rgb"  # the colours asked for: kept while a view without them is shown
        self._filling = False
        self._fill_color_combo("point_cloud")
        self.color_combo.setToolTip(tr("Colori: quelli delle foto, le classi (con la legenda per nasconderle), la "
                                       "quota "
                                       "o la confidenza (quante mappe di profondità confermano ogni punto o vertice)"))
        self.color_combo.currentIndexChanged.connect(self._color_chosen)
        self.point_spin = QSpinBox()
        self.point_spin.setRange(1, 8)
        self.point_spin.setValue(2)
        self.point_spin.setSuffix(" px")
        self.point_spin.setToolTip(tr("Dimensione dei punti sullo schermo"))
        self.point_label = QLabel(tr("Punti:"))
        self.b_lasso = QPushButton(tr("Lazo"))
        self.b_lasso.setToolTip(tr("Seleziona con un lazo punti della nuvola (classe, elimina, tieni) o facce del "
                                   "modello (elimina, tieni)"))
        self.b_lasso.clicked.connect(self.start_lasso)
        self.b_marker = QPushButton("Marker")
        self.b_marker.setToolTip(tr("Aggiungi un marker: clic su un punto del modello o della nuvola, come in "
                                    "Metashape. "
                                    "Si proietta sulle foto che lo vedono; con due marker si fa una barra di scala"))
        self.b_marker.clicked.connect(lambda: self.start_pick("marker"))
        self.b_photos = QPushButton(tr("Foto del punto"))
        self.b_photos.setToolTip(tr("Clic su un punto del modello o della nuvola: le foto che lo vedono, come Filter "
                                    "Photos by Point di Metashape"))
        self.b_photos.clicked.connect(lambda: self.start_pick("photos"))
        self.b_section = QPushButton(tr("Sezione"))
        self.b_section.setToolTip(tr("Traccia di una sezione: clic su due punti del modello o della nuvola"))
        self.b_section.clicked.connect(lambda: self.start_pick("section"))
        self.b_slice = QPushButton(tr("Pianta a quota"))
        self.b_slice.setToolTip(tr("Clic su un punto: la pianta del modello tagliato a quella quota"))
        self.b_slice.clicked.connect(lambda: self.start_pick("slice"))
        self.b_cut = QPushButton(tr("Taglia"))
        self.b_cut.setToolTip(tr("Taglia il modello con un piano: tieni una parte, una fetta, o salva le due parti "
                                 "o una serie di fette"))
        cut_menu = QMenu(self.b_cut)
        cut_menu.addAction(tr("Piano orizzontale: clic sulla quota"), lambda: self.start_pick("cut_h"))
        cut_menu.addAction(tr("Piano verticale: clic su due punti"), lambda: self.start_pick("cut_v"))
        cut_menu.addAction(tr("Piano per tre punti: tre clic"), lambda: self.start_pick("cut_p"))
        cut_menu.addAction(tr("Con i valori..."), lambda: self.cut_requested.emit("values", []))
        self.b_cut.setMenu(cut_menu)
        self.b_elevation = QPushButton(tr("Prospetto"))
        self.b_elevation.setToolTip(tr("Ortofoto e rilievo del modello su un piano: quello della vista com'è ora, o "
                                       "quello di tre punti cliccati; un prospetto o una sezione"))
        elev_menu = QMenu(self.b_elevation)
        elev_menu.addAction(tr("Ortofoto da questa vista"), self._elevation_from_view)
        elev_menu.addAction(tr("Piano per tre punti: tre clic"), lambda: self.start_pick("elev_p"))
        self.b_elevation.setMenu(elev_menu)
        self.version_label = QLabel("")
        self.version_label.setObjectName("versionLabel")
        self.version_label.setStyleSheet("color: #b45309; font-weight: bold")
        self.version_label.setVisible(False)
        self.info_label = QLabel(tr("Senza confidenza: Flusso di lavoro > Calcola la confidenza"))
        self.info_label.setStyleSheet("color: #b45309")
        self.info_label.setVisible(False)
        bar.addWidget(QLabel(tr("Mostra:")))
        bar.addWidget(self.combo)
        bar.addWidget(self.version_label)
        bar.addWidget(self.color_combo)
        bar.addWidget(self.point_label)
        bar.addWidget(self.point_spin)
        bar.addWidget(self.info_label)
        bar.addWidget(self.b_lasso)
        bar.addWidget(self.b_marker)
        bar.addWidget(self.b_photos)
        bar.addWidget(self.b_section)
        bar.addWidget(self.b_slice)
        bar.addWidget(self.b_cut)
        bar.addWidget(self.b_elevation)
        bar.addWidget(self.cb_cams)
        bar.addWidget(self.cb_marks)
        bar.addWidget(self.cb_region)
        exag = QWidget()  # the label never on another row than its value
        exag_row = QHBoxLayout(exag)
        exag_row.setContentsMargins(0, 0, 0, 0)
        exag_row.addWidget(QLabel(tr("Esagerazione verticale:")))
        exag_row.addWidget(self.exag_spin)
        bar.addWidget(exag)
        bar.addWidget(b_top)
        bar.addWidget(b_reset)
        bar.addWidget(self.b_center)
        bar.addWidget(self.b_trackball)
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
        self.b_center.clicked.connect(lambda: self.canvas.centre_on_pivot())
        self.canvas.pivot_requested.connect(self._pivot_at)
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
        self.sd = None  # what is shown (heights exaggerated)
        self.base_sd = None  # the same in true scene coordinates
        self.paths: dict = {}  # files shown instead of the active products (a kept version)
        self._own_entry: str | None = None  # the kind of an entry of the list only for the file shown
        self.highlight = None  # mask over the dense cloud shown in red (what a cleaning would remove)
        self._project = None
        self._filling = False
        self._update_tools()
        self.set_trackball_mode(self._saved_trackball_mode(), save=False)

    # ----------------------------------------------------------- trackball
    @staticmethod
    def _saved_trackball_mode() -> str:
        from . import app_settings

        mode = str(app_settings().value(TRACKBALL_SETTING, "auto"))
        return mode if mode in TRACKBALL_MODES else "auto"

    def set_trackball_mode(self, mode: str, save: bool = True) -> None:
        """When the trackball is shown: "auto" (while the mouse is over the view), "always", "never"."""
        mode = mode if mode in TRACKBALL_MODES else "auto"
        self.trackball_actions[mode].setChecked(True)
        self.canvas.set_trackball_mode(mode)
        if save:
            from . import app_settings

            app_settings().setValue(TRACKBALL_SETTING, mode)

    def trackball_mode(self) -> str:
        return self.canvas.trackball.mode

    def pivot(self) -> np.ndarray | None:
        """The centre of rotation in world coordinates (the chunk CRS)."""
        if self.sd is None:
            return None
        return self.sd.from_display(self.canvas.pivot()[None])[0] + self.sd.offset

    def set_pivot(self, point, recentre: bool = False) -> None:
        """The centre of rotation at ``point`` (world coordinates); the view does not move unless
        ``recentre``."""
        if self.sd is None:
            return
        self.canvas.set_pivot(self.sd.to_display(np.asarray(point, np.float64)[None] - self.sd.offset)[0], recentre)

    def _pivot_at(self, x: float, y: float) -> None:
        """A double click: the centre of rotation on the surface under it (as a marker is placed)."""
        if self.kind not in self.PICK_KINDS or self.chunk is None or self.sd is None:
            return
        _image, project = self.canvas.snapshot()
        point = self.pick_point(x, y, project, self.canvas.view_ray)
        if point is not None:
            self.set_pivot(self.chunk.to_world(np.asarray(point)[None])[0])

    @property
    def backend(self) -> str:
        return "vtk" if isinstance(self.canvas, VtkCanvas) else "software"

    def show_version(self, kind: str, path, title: str, version: bool = True) -> None:
        """Show a kept version (``path``) of the dense cloud or the model - or, ``version`` False, a
        file of a plugin named ``title`` (the units of a model of the deposits) - instead of the
        active product; choosing a view in the list goes back to the active products. The list
        names the kind shown: when the chunk has nothing of that kind, with an entry of its own that
        goes away with the file."""
        self._clear_version()
        self.paths = {kind: str(path)}
        self.version_label.setText(tr("Versione: {0} (non attiva)", title) if version else title)
        self.version_label.setToolTip(tr("Scegli un'altra vista nell'elenco per tornare ai prodotti del chunk"))
        self.version_label.setVisible(True)
        self._filling = True
        i = self.combo.findData(kind)
        if i < 0:
            self.combo.addItem(tr(VIEW_LABELS.get(kind, kind)), kind)
            i = self.combo.count() - 1
            self._own_entry = kind
        self.combo.setCurrentIndex(i)
        self._filling = False
        reset = self.kind != kind
        self.kind = kind
        self.refresh(reset=reset)

    def _clear_version(self) -> None:
        self.paths = {}
        self.version_label.setVisible(False)
        kind, self._own_entry = self._own_entry, None
        i = self.combo.findData(kind) if kind is not None else -1
        if i >= 0:  # the entry of the file shown goes with it (the last one: the choice stays)
            filling, self._filling = self._filling, True
            self.combo.removeItem(i)
            self._filling = filling

    def set_chunk(self, chunk, kind: str | None = None) -> None:
        self._clear_version()
        self.highlight = None
        self.chunk = chunk
        views = ["cameras"] + available_views(chunk) if chunk is not None else []
        self._filling = True
        self.combo.clear()
        for v in views:
            self.combo.addItem(tr(VIEW_LABELS[v]), v)
        from .scene import best_view

        if kind not in views:  # none asked, or the chunk has nothing of that kind: the best it has
            kind = best_view(chunk) if chunk is not None else None
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

    COLOR_MODES = {"point_cloud": ("rgb", "class", "elevation", "hag", "confidence"),
                   "tie_points": ("rgb", "class", "elevation"), "model": ("rgb", "confidence")}
    COLOR_LABELS = {"rgb": "Colori RGB", "class": "Classi", "elevation": "Quota", "hag": "Altezza dal suolo",
                    "confidence": "Confidenza"}

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
        self.b_section.setVisible(self.kind in self.PICK_KINDS)
        self.b_slice.setVisible(self.kind in self.PICK_KINDS)
        self.b_cut.setVisible(self.kind in ("model", "model_textured") and not self.paths)
        self.b_elevation.setVisible(self.kind in self.PICK_KINDS and not self.paths)
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
        if purpose != "section_end":
            self._section_start = None
        if purpose in ("cut_h", "cut_v", "cut_p", "elev_p"):
            self._cut_points = []
        what = {"photos": "le foto che lo vedono", "section": "il primo estremo della sezione",
                "section_end": "il secondo estremo della sezione", "slice": "la quota della pianta",
                "cut_h": "la quota del piano di taglio", "cut_v": "il primo punto della linea di taglio",
                "cut_v2": "il secondo punto della linea di taglio", "cut_p": "il primo dei tre punti del piano",
                "cut_p2": "il secondo dei tre punti del piano", "cut_p3": "il terzo dei tre punti del piano",
                "elev_p": "il primo punto del prospetto (l'asse orizzontale va dal primo al secondo)",
                "elev_p2": "il secondo punto del prospetto", "elev_p3": "il terzo punto del prospetto"}.get(
            purpose, "il nuovo posto del marker" if purpose.startswith("move:") else "un nuovo marker")
        self.picker.start(image, tr("Clic su un punto del modello o della nuvola: {0} - Esc o tasto destro annulla",
                                    tr(what)))
        self.stack.setCurrentWidget(self.picker)

    def _pick_finished(self, pos) -> None:
        self.stack.setCurrentWidget(self.canvas)
        if pos is None or self._project is None:
            return
        point = self.pick_point(pos[0], pos[1], self._project, self._ray)
        purpose = getattr(self, "_purpose", "marker")
        if purpose == "photos":
            self.photos_point_requested.emit(point)
        elif purpose == "section":
            if point is not None:
                self._section_start = point
                self.start_pick("section_end")
        elif purpose == "section_end":
            if point is not None and getattr(self, "_section_start", None) is not None:
                self.section_requested.emit(self._section_start, point)
            self._section_start = None
        elif purpose == "slice":
            if point is not None:
                self.slice_requested.emit(point)
        elif purpose.startswith("elev_p"):
            if point is None:
                return
            points = getattr(self, "_cut_points", []) + [point]
            self._cut_points = points
            follow = {"elev_p": "elev_p2", "elev_p2": "elev_p3"}.get(purpose)
            if follow is not None:
                self.start_pick(follow)
            else:
                self._cut_points = []
                self.elevation_requested.emit("points", points)
        elif purpose.startswith("cut_"):
            if point is None:
                return
            points = getattr(self, "_cut_points", []) + [point]
            self._cut_points = points
            follow = {"cut_v": "cut_v2", "cut_p": "cut_p2", "cut_p2": "cut_p3"}.get(purpose)
            if follow is not None:
                self.start_pick(follow)
            else:
                kind = {"cut_h": "horizontal", "cut_v2": "vertical", "cut_p3": "points"}[purpose]
                self._cut_points = []
                self.cut_requested.emit(kind, points)
        elif purpose.startswith("move:"):
            self.move_marker_requested.emit(int(purpose[5:]), point)
        else:
            self.marker_requested.emit(point)

    def _elevation_from_view(self) -> None:
        frame = self.view_frame()
        if frame is not None:
            self.elevation_requested.emit("view", frame)

    def view_frame(self) -> dict | None:
        """What the view shows now, for an orthophoto from this view, in world coordinates (the chunk
        CRS): ``centre`` (the surface under the middle of the view, else the middle of the scene on
        the line of sight), ``toward`` (from the scene to the viewer), ``up`` (of the screen) and
        ``corners`` (the four corners of the view on the plane through the centre)."""
        if self.chunk is None or self.sd is None or self.kind not in self.PICK_KINDS:
            return None
        sd, canvas = self.sd, self.canvas
        w, h = max(canvas.width(), 1), max(canvas.height(), 1)
        _image, project = canvas.snapshot()
        ray = canvas.view_ray
        o, d, _pixel = ray(w / 2, h / 2)
        picked = self.pick_point(w / 2, h / 2, project, ray)
        if picked is not None:
            shown = sd.to_display(self.chunk.to_world(np.asarray(picked)[None]) - sd.offset)[0]
        else:
            shown = np.asarray(sd.center, np.float64)
        pc = o + float((shown - o) @ d) * d  # on the line of sight, at the depth of the surface

        def on_plane(x, y):
            oc, dc, _ = ray(x, y)
            return oc + ((pc - oc) @ d) / (dc @ d) * dc

        shown_pts = np.vstack([pc, pc - d, on_plane(w / 2, 0)] + [on_plane(x, y) for x, y in
                                                                   ((0, 0), (w, 0), (w, h), (0, h))])
        world = sd.from_display(shown_pts) + sd.offset
        return {"centre": world[0], "toward": world[1] - world[0], "up": world[2] - world[0], "corners": world[3:]}

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
        picked = self.chunk.to_internal(sd.from_display(pts[front[np.argmin(d2[front])]][None]) + sd.offset)[0]
        if ray is None:
            return picked
        origin, direction, pixel = ray(x, y)  # a line of the view is a line of the true scene too
        o, e = self.chunk.to_internal(sd.from_display(np.array([origin, origin + direction])) + sd.offset)
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
        x, y, z, ok = project(self.sd.to_display(centres))
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
        x, y, z, ok = project(self.sd.to_display(world))
        mode = self.color_mode()
        hidden = self._hidden(mode) if mode in ("class", "confidence") else set()
        codes = getattr(pc, "classification", None)
        if hidden and mode == "class" and codes is not None:
            ok &= ~np.isin(np.asarray(codes), list(hidden))
        if hidden and mode == "confidence" and pc.confidence is not None:
            from .scene import confidence_bins

            ok &= ~np.isin(confidence_bins(pc.confidence), list(hidden))
        return self._inside_and_visible(polygon, x, y, z, ok, depth_tol, cell)

    def set_exaggeration(self, factor: float) -> None:
        """Heights of what is shown multiplied by ``factor`` around the centre of the scene (1: as
        they are). Only the view changes: picks and selections give the true coordinates."""
        factor = float(min(max(factor, 0.1), 50.0))
        self._exag_timer.stop()
        if abs(self.exag_spin.value() - factor) > 1e-9:
            self.exag_spin.blockSignals(True)
            self.exag_spin.setValue(factor)
            self.exag_spin.blockSignals(False)
        if factor == self.exaggeration and (self.sd is None or self.sd.exaggeration == factor):
            return
        self.exaggeration = factor
        if self.base_sd is not None:
            pivot = self.pivot()  # the centre of rotation stays on the same point of the scene
            self.sd = exaggerated(self.base_sd, factor)
            self.canvas.set_scene(self.sd, reset=False)
            if pivot is not None:
                self.set_pivot(pivot)

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
        self.base_sd = sd
        self.sd = sd = exaggerated(sd, self.exaggeration)
        if mode == "class":
            self.legend.set_counts(sd.class_counts, hidden, sd.class_labels)
        elif mode == "confidence":
            self.legend.set_bins(sd.bin_counts, bins)
            self.legend.setVisible(bool(sd.bin_counts))
        self.info_label.setText(tr("Senza punti del suolo (classe 2): classifica prima la nuvola") if sd.hag_missing
                                else tr("Senza confidenza: Flusso di lavoro > Calcola la confidenza"))
        self.info_label.setVisible(bool(sd.confidence_missing or sd.hag_missing))
        self.canvas.set_scene(sd, reset=reset)
