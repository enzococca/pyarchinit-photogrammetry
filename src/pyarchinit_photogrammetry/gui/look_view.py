"""Looking through a photo in the 3D view, as Metashape's *Look Through*: the view takes the pose and the field of
view of an aligned photo and draws the photo, undistorted, over the model with an opacity of its own. The photo is
read and undistorted in a worker thread (the last few kept); moving the view, Home, T or Esc leave the photo,
Page Down/Up (or the arrows) go to the next/previous photo in the order they were taken."""
from __future__ import annotations

import threading
from collections import OrderedDict

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QWidget

from ..engine import look_through as lt
from .i18n import tr
from .scene import OrbitCamera

PHOTOS_KEPT = 6  # undistorted photos kept (RGBA at MAX_SIDE: 1600 x 1200 x 4 = ~7.7 MB each)
MAPS_KEPT = 3  # undistortion maps kept, one per sensor and size read (two float32 maps: ~15 MB each)
LABEL_PX = 170  # the name of the photo elided beyond this (whole in its tooltip)
SETTING = "view3d/look_opacity"


class _LookRelay(QObject):
    """Hands the photo undistorted in a worker thread to the view (queued to the interface thread)."""

    ready = Signal(object, object)  # (generation, camera key), (RGBA array or None, maps, error text)


def _load(path, sensor, ideal, maps):
    try:
        rgba, maps = lt.photo_and_maps(path, sensor, ideal, maps)
        return rgba, maps, ""
    except Exception as exc:  # noqa: BLE001 - told on the view: the camera is placed all the same
        return None, maps, f"{type(exc).__name__}: {exc}"


class LookThrough:
    """The 3D view's part that looks through photos (mixed into :class:`.model_view.ModelView`, which declares
    ``look_changed``). Call :meth:`_init_look` once the canvas exists."""

    def _init_look(self) -> None:
        from . import app_settings

        self._look = None  # {"camera", "ideal", "corners", "generation"} while looking through a photo
        self._look_generation = 0
        self._look_photos: OrderedDict = OrderedDict()  # (path, sensor signature, size) -> RGBA
        self._look_maps: OrderedDict = OrderedDict()  # (sensor signature, size, read size) -> maps
        self._look_running = False
        self._look_pending = None  # a photo asked while another one is read
        self._look_relay = _LookRelay(self)
        self._look_relay.ready.connect(self._look_loaded, Qt.ConnectionType.QueuedConnection)
        # the row of the photo looked through, in parts the bar of the view wraps (none wider than the display
        # choices: the 3D view is never widened by it on a narrow screen); shown only while looking
        def part(*widgets, left: int = 0):
            w = QWidget()
            lay = QHBoxLayout(w)
            lay.setContentsMargins(left, 0, 0, 0)
            lay.setSpacing(4)
            for x in widgets:
                lay.addWidget(x)
            w.setVisible(False)
            return w

        self.look_label = QLabel("")
        self.look_label.setStyleSheet("font-weight: bold")
        self.look_label.setMaximumWidth(LABEL_PX)
        self.b_look_prev = QPushButton("<")
        self.b_look_prev.setToolTip(tr("Foto precedente (Pagina su)"))
        self.b_look_prev.clicked.connect(lambda: self.look_step(-1))
        self.b_look_next = QPushButton(">")
        self.b_look_next.setToolTip(tr("Foto successiva (Pagina giù)"))
        self.b_look_next.clicked.connect(lambda: self.look_step(1))
        for b in (self.b_look_prev, self.b_look_next):
            b.setFixedWidth(b.fontMetrics().horizontalAdvance("<") + 24)
        self.look_slider = QSlider(Qt.Orientation.Horizontal)
        self.look_slider.setRange(0, 100)
        try:
            value = int(float(app_settings().value(SETTING, 50)))
        except (TypeError, ValueError):
            value = 50
        self.look_slider.setValue(min(max(value, 0), 100))
        self.look_slider.setMinimumWidth(60)
        self.look_slider.setMaximumWidth(110)
        self.look_slider.setToolTip(tr("Opacità della foto") + ": " + tr("0 %: solo il modello, 100 %: solo la foto"))
        self.look_slider.valueChanged.connect(self._look_opacity_moved)
        self.look_value = QLabel(f"{self.look_slider.value()} %")
        self.look_value.setMinimumWidth(self.look_value.fontMetrics().horizontalAdvance("100 %") + 4)
        self.b_look_exit = QPushButton(tr("Esci dalla foto"))
        self.b_look_exit.setToolTip(tr("Torna alla vista libera (Esc); anche ruotare, spostare o avvicinare la vista "
                                       "esce dalla foto"))
        self.b_look_exit.clicked.connect(self.leave_look)
        opacity = QLabel(tr("Opacità"))
        opacity.setToolTip(tr("Opacità della foto"))
        self.look_parts = [part(self.look_label, left=12), part(self.b_look_prev, self.b_look_next),
                           part(opacity, self.look_slider, self.look_value), part(self.b_look_exit)]
        # Esc and Page Down/Up while looking, also with the slider or a button of the row focused (on this view only)
        self._look_shortcuts = []
        for key, slot in ((Qt.Key.Key_Escape, self._look_escape), (Qt.Key.Key_PageDown, lambda: self.look_step(1)),
                          (Qt.Key.Key_PageUp, lambda: self.look_step(-1))):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.setEnabled(False)
            sc.activated.connect(slot)
            self._look_shortcuts.append(sc)
        self.canvas.look_left.connect(self._look_left)

    def _show_look_row(self, shown: bool) -> None:
        for w in self.look_parts:
            w.setVisible(shown)
        for sc in self._look_shortcuts:
            sc.setEnabled(shown)

    def look_shown(self) -> bool:
        """True when the row of the photo looked through is shown."""
        return self.look_parts[0].isVisibleTo(self)

    def _look_escape(self) -> None:
        """Esc while looking: a drawing, a pick or a lasso in progress first, else out of the photo."""
        if getattr(self, "_draw_kind", None) is not None:
            self.stop_drawing()
        elif self.stack.currentWidget() is not self.canvas:
            self._end_overlays()
            self.stack.setCurrentWidget(self.canvas)
        else:
            self.leave_look()

    # ----------------------------------------------------------- state
    @property
    def look_camera(self):
        """The photo looked through (a Camera of the chunk shown), None: the free view."""
        return None if self._look is None else self._look["camera"]

    def look_opacity(self) -> float:
        return self.look_slider.value() / 100.0

    def look_cameras(self) -> list:
        """The photos that can be looked through, in the order of Page Down/Up."""
        return lt.ordered_cameras(self.chunk) if self.chunk is not None else []

    # ----------------------------------------------------------- entering and leaving
    def look_through(self, camera) -> bool:
        """Look through ``camera`` (an aligned photo of the chunk shown): the view takes its pose and field of view
        and the photo is drawn over the model when it has been read. False: not possible (not aligned, nothing
        shown)."""
        if camera is None or self.chunk is None or self.sd is None or not camera.aligned:
            return False
        if not any(c is camera for c in self.chunk.cameras):
            return False
        self.stop_drawing()
        self.stop_free_select()
        self._end_overlays()
        if self.exaggeration != 1.0:  # the photo lies on the true heights only
            self.set_exaggeration(1.0)
        sensor = self.chunk.sensor(camera)
        ideal = lt.ideal_camera(sensor)
        self._look_generation += 1
        self._look = {"camera": camera, "ideal": ideal, "corners": None, "generation": self._look_generation}
        self._look_place()
        text = tr("Foto: {0}", camera.label)
        fm = self.look_label.fontMetrics()
        self.look_label.setText(fm.elidedText(text, Qt.TextElideMode.ElideMiddle, LABEL_PX - 4))
        self.look_label.setToolTip(text)
        self._show_look_row(True)
        self.canvas.set_hint(tr("{0} - Esc: esci, Pagina giù/su: foto successiva/precedente", camera.label))
        key = self._look_photo_key(camera, sensor, ideal)
        if key in self._look_photos:
            self._look_photos.move_to_end(key)
            self._look_show(self._look_photos[key])
        else:
            self.canvas.set_photo(None, None)
            self._look_request(camera)
        self._draw_target().setFocus()
        self.look_changed.emit(camera)
        return True

    def _look_place(self) -> None:
        """The view's camera at the photo's pose with the field of view that fits the photo in the view (bands at
        the sides or above and below), and the corners of the photo's frame in the scene."""
        look = self._look
        if look is None or self.sd is None:
            return
        centre, basis = lt.photo_pose(self.chunk, look["camera"])
        eye = centre - np.asarray(self.sd.offset, np.float64)
        fov, _rect = lt.fit_in_view(look["ideal"], *self.canvas.view_size())
        ahead = float(np.dot(np.asarray(self.sd.center, np.float64) - eye, basis[2]))
        depth = max(ahead, 1e-3 * max(self.sd.extent, 1e-9), 1e-6)
        cam = OrbitCamera(eye + basis[2] * depth, depth, fov=fov, up=self.sd.up)
        cam.R = basis.copy()
        look["corners"] = eye + depth * (look["ideal"].corners() @ basis)
        self.canvas.look(cam)
        if look.get("image") is not None:  # fitted again (the view changed size): the photo on its new frame
            self.canvas.set_photo(look["image"], look["corners"], self.look_opacity())

    def leave_look(self) -> None:
        """Back to the free view (Esc); the camera stays where the photo was."""
        if self._look is not None:
            self.canvas.end_look()  # look_left -> _look_left

    def _look_left(self) -> None:
        if self._look is None:
            return
        self._look = None
        self._look_pending = None
        self._show_look_row(False)
        if getattr(self, "_draw_kind", None) is None:
            self.canvas.set_hint("")
        self.look_changed.emit(None)

    def _look_resized(self) -> None:
        """The view changed size: the photo fitted again."""
        if self._look is not None:
            self._look_place()

    # ----------------------------------------------------------- choosing the photo
    def look_best(self):
        """Look through the aligned photo whose direction and position best match the view now: the photo, or None
        when no photo looks the same way."""
        if self.chunk is None or self.sd is None:
            return None
        cam = self.canvas.get_cam()
        off = np.asarray(self.sd.offset, np.float64)
        eye = self.sd.from_display(cam.eye[None])[0] + off
        best = lt.best_photo(self.chunk, eye, cam.basis()[2], float(self.sd.extent), self.look_cameras())
        if best is not None:
            self.look_through(best)
        return best

    def look_step(self, step: int):
        """The next (+1) or previous (-1) photo in the order they were taken; the first one when not looking."""
        cams = self.look_cameras()
        if not cams:
            return None
        cur = self.look_camera
        i = next((j for j, c in enumerate(cams) if c is cur), None)
        cam = cams[(i + step) % len(cams)] if i is not None else cams[0 if step >= 0 else -1]
        self.look_through(cam)
        return cam

    def _look_key(self, ev) -> bool:
        """A key on the view while looking through a photo: True when it was used."""
        key = ev.key()
        if key == Qt.Key.Key_Escape:
            self.leave_look()
        elif key in (Qt.Key.Key_PageDown, Qt.Key.Key_Right):
            self.look_step(1)
        elif key in (Qt.Key.Key_PageUp, Qt.Key.Key_Left):
            self.look_step(-1)
        else:
            return False
        return True

    # ----------------------------------------------------------- the photo
    @staticmethod
    def _sensor_signature(sensor) -> tuple:
        return sensor.model, int(sensor.width), int(sensor.height), tuple(round(float(v), 9) for v in sensor.params)

    def _look_photo_key(self, camera, sensor, ideal) -> tuple:
        import os

        try:
            stamp = os.stat(camera.path).st_mtime_ns
        except OSError:
            stamp = 0
        return str(camera.path), stamp, self._sensor_signature(sensor), (ideal.width, ideal.height)

    def _look_request(self, camera) -> None:
        if self._look_running:  # one photo read at a time: the last one asked comes next
            self._look_pending = camera
            return
        sensor = self.chunk.sensor(camera)
        ideal = lt.ideal_camera(sensor)
        sig = (self._sensor_signature(sensor), (ideal.width, ideal.height))
        maps = next((m for k, m in self._look_maps.items() if k[:2] == sig), None)
        key = self._look_photo_key(camera, sensor, ideal)
        tag = (self._look_generation, camera.key, key, sig)
        self._look_running = True
        relay = self._look_relay

        def work():
            result = _load(camera.path, sensor, ideal, maps)
            try:
                relay.ready.emit(tag, result)
            except RuntimeError:  # the view was closed and deleted meanwhile: nobody wants the photo
                pass

        threading.Thread(target=work, name="ppg-look-through", daemon=True).start()

    def _look_loaded(self, tag, result) -> None:
        self._look_running = False
        generation, cam_key, key, sig = tag
        rgba, maps, error = result
        if maps is not None:
            self._look_maps[sig + (maps[1],)] = maps
            while len(self._look_maps) > MAPS_KEPT:
                self._look_maps.popitem(last=False)
        if rgba is not None:
            self._look_photos[key] = rgba
            while len(self._look_photos) > PHOTOS_KEPT:
                self._look_photos.popitem(last=False)
        look = self._look
        if look is not None and look["camera"].key == cam_key and generation == look["generation"]:
            if rgba is not None:
                self._look_show(rgba)
            else:
                self.busy_message.emit(tr("Impossibile leggere la foto {0}: {1}", look["camera"].label, error))
        pending, self._look_pending = self._look_pending, None
        if pending is not None and self._look is not None and pending is self._look["camera"]:
            sensor = self.chunk.sensor(pending)
            got = self._look_photos.get(self._look_photo_key(pending, sensor, lt.ideal_camera(sensor)))
            if got is not None:
                self._look_show(got)
            else:
                self._look_request(pending)

    def _look_show(self, rgba: np.ndarray) -> None:
        look = self._look
        if look is None:
            return
        h, w = rgba.shape[:2]
        image = QImage(rgba.data, w, h, 4 * w, QImage.Format.Format_RGBA8888).copy()
        look["image"] = image
        self.canvas.set_photo(image, look["corners"], self.look_opacity())

    def look_ready(self) -> bool:
        """True when the photo looked through is drawn."""
        return self._look is not None and self._look.get("image") is not None

    def _look_opacity_moved(self, value: int) -> None:
        from . import app_settings

        self.look_value.setText(f"{value} %")
        self.canvas.set_photo_opacity(value / 100.0)
        app_settings().setValue(SETTING, int(value))
