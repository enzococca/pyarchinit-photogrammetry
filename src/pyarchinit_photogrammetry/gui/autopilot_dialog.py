"""The log of an autopilot (ArduPilot DataFlash, PX4 ULog) whose shots give the photos their positions: what the log
holds, the photos its shots pair with, the camera and the message the shots are read from, the lever arm from the
antenna to the camera and the latency of the camera. The log is read in a thread of its own, never in the thread of
the interface."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ..gnss import autopilot, sources
from ..gnss import shots as gshots
from . import shots_widgets as sw
from .i18n import engine, tr

TYPING_PAUSE_MS = 300  # the photos are paired again this long after the last change of the latency or of the folder


class AutopilotDialog(QDialog):
    """The shots of the autopilot log ``path`` for the photos of ``chunk``: :attr:`summary` says what the log holds
    (its kind, its cameras, the shots and how many have a valid time, their span, the kind of their heights),
    :attr:`pairing` the photos the shots pair with (:func:`.shots.pair`: how many, how, the photos and shots left
    over), :attr:`error` what keeps the positions from being applied. The message the shots are read from, the camera
    (an ArduPilot log with several), how the photos are paired, the folder of the photos, the lever arm and the
    latency are chosen here; *Applica* is there once the log is read and photos find their shot."""

    def __init__(self, chunk, path, parent=None):
        super().__init__(parent)
        self.chunk, self.path = chunk, Path(path)
        self.setWindowTitle(tr("Log dell'autopilota"))
        self.setMinimumWidth(560)
        self._ulog = sources.is_ulog(self.path)
        self._shotset = None
        self._aside = sw.Aside(self)
        self._aside.done.connect(self._update)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(TYPING_PAUSE_MS)
        self._timer.timeout.connect(self._update)

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        intro = QLabel(tr("L'autopilota registra dove era l'antenna a ogni scatto della camera: ogni foto prende la "
                          "posizione del suo scatto, con il suo stato e la sua accuratezza. Per posizioni al "
                          "centimetro serve il PPK (Calcola PPK...), che corregge quelle del ricevitore."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        form = QFormLayout()
        name = QLabel(self.path.name)
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setToolTip(str(self.path))
        self.summary = sw.plain_label()
        holder = QVBoxLayout()
        holder.setSpacing(2)
        holder.addWidget(name)
        holder.addWidget(self.summary)
        form.addRow(tr("Log"), holder)
        self.message = QComboBox()
        sw.fill_messages(self.message, self._ulog)
        self.message.setToolTip(tr("Gli scatti confermati dalla camera sono più precisi dei comandi di scatto; "
                                   "automatico prende i confermati quando il log li ha."))
        form.addRow(tr("Messaggi degli scatti"), self.message)
        self.camera = QComboBox()
        self.camera.setEnabled(False)
        self.camera.addItem(tr("una sola"), None)
        form.addRow(tr("Camera"), self.camera)
        self.by = sw.by_combo()
        form.addRow(tr("Abbinamento delle foto"), self.by)
        folder = sw.photos_folder(chunk, self.path)
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        self.photos = QLineEdit(str(folder) if folder is not None else "")
        self._folder = folder  # as the chunk names it, while the field still says it
        self.photos.setPlaceholderText(tr("la cartella del log"))
        browse = QPushButton(tr("Sfoglia"))
        browse.setFixedWidth(72)
        row_lay.addWidget(self.photos, 1)
        row_lay.addWidget(browse)
        self.pairing = sw.plain_label()
        photos = QVBoxLayout()
        photos.setSpacing(2)
        photos.addWidget(row)
        photos.addWidget(self.pairing)
        form.addRow(tr("Cartella delle foto"), photos)
        self.lever_panel = sw.LeverPanel()
        self.lever_forward, self.lever_right = self.lever_panel.forward, self.lever_panel.right
        self.lever_down, self.latency = self.lever_panel.down, self.lever_panel.latency
        form.addRow(tr("Braccio di leva e latenza"), self.lever_panel)
        lay.addLayout(form)
        self.error = sw.plain_label("color: #b91c1c; font-weight: bold")
        lay.addWidget(self.error)
        lay.addStretch(1)
        self.scroll = QScrollArea()  # a log with much to say on a small screen: scrolled, never cut
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.addWidget(self.scroll, 1)
        buttons = QDialogButtonBox()
        self.b_apply = buttons.addButton(tr("Applica"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(tr("Annulla"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._buttons = buttons

        browse.clicked.connect(self._browse)
        self.message.currentIndexChanged.connect(lambda _index: self._update())
        self.camera.currentIndexChanged.connect(lambda _index: self._update())
        self.by.currentIndexChanged.connect(lambda _index: self._update())
        self.photos.textChanged.connect(lambda _text: self._timer.start())
        self.latency.valueChanged.connect(lambda _value: self._timer.start())
        self._update()

    # ------------------------------------------------------------ the choices
    def _browse(self) -> None:
        start = self.photos.text().strip() or str(self.path.parent)
        folder = QFileDialog.getExistingDirectory(self, tr("Cartella delle foto"), start)
        if folder:
            self.photos.setText(folder)

    def photos_path(self) -> Path | None:
        """The folder of the photos: the one of the chunk as it names it, the one written, None for the log's."""
        text = self.photos.text().strip()
        if not text:
            return None
        if self._folder is not None and text == str(self._folder):
            return self._folder
        return Path(text).expanduser()

    def _camera(self) -> int | None:
        """The camera the shots are read of: None while the log has one."""
        return self.camera.currentData() if self.camera.count() > 1 else None

    def _cameras(self):
        """The cameras of an ArduPilot log (:func:`.autopilot.cameras`), :data:`.shots_widgets.RUNNING` while they
        are read, an exception when they cannot be; none for a PX4 log."""
        if self._ulog:
            return []
        path = self.path
        return self._aside.get(("cameras",), lambda: autopilot.cameras(path))

    def _fill_cameras(self, cameras: list) -> None:
        if len(cameras) < 2 or self.camera.count() == len(cameras):
            return
        self.camera.blockSignals(True)
        self.camera.clear()
        for entry in cameras:
            self.camera.addItem(tr("Camera {0}: {1} CAM, {2} TRIG", entry["instance"], entry["cam"], entry["trig"]),
                                int(entry["instance"]))
        self.camera.setEnabled(True)
        self.camera.blockSignals(False)

    # ------------------------------------------------------------ what the log holds
    def _kind_text(self, cameras) -> str:
        text = tr("Log PX4 (ULog)") if self._ulog else tr("Log ArduPilot (DataFlash)")
        if isinstance(cameras, list) and len(cameras) > 1:
            text += tr(", {0} camere", len(cameras))
        return text + "."

    def _shots_text(self, shotset) -> list[str]:
        timed = [s.gps_time for s in shotset.shots if s.valid]
        text = tr("{0} scatti ({1} con un tempo valido)", len(shotset.shots), len(timed))
        if timed:
            text += tr(", dal {0} al {1} (ora GPS)", sw.gps_when(min(timed)), sw.gps_when(max(timed)))
        lines = [text + "."]
        placed = [s for s in shotset.shots if s.antenna is not None]
        if placed:
            lines.append(tr("Posizioni dell'autopilota con quote sul livello del mare (AMSL).")
                         if placed[0].height == "msl" else tr("Posizioni dell'autopilota con quote ellissoidiche."))
        return lines + [engine(w) for w in shotset.warnings]

    def _update(self) -> None:
        """What the dialog says (:meth:`_say`), the dialog grown to hold it: never smaller, never beyond the screen."""
        self._say()
        outer, margins = self.layout(), self.layout().contentsMargins()
        width = self.width() - margins.left() - margins.right()
        need = (self.scroll.widget().layout().totalHeightForWidth(width) + margins.top() + margins.bottom()
                + outer.spacing() + self._buttons.sizeHint().height())
        need = min(need, self.max_height())
        if need > self.height():
            self.resize(self.width(), need)

    def max_height(self) -> int:
        """How tall the dialog grows: the screen less its title bar and a margin."""
        screen = self.screen()
        return screen.availableGeometry().height() - 60 if screen is not None else 700

    def _say(self) -> None:
        """What the dialog says, from what was read so far; what is missing is read in a thread, and looked at again
        when it is there."""
        self._timer.stop()
        self._shotset = None
        self.b_apply.setEnabled(False)
        self.error.setText("")
        self.pairing.setText("")
        cameras = self._cameras()
        reading = tr("Lettura di {0} in corso...", self.path.name)
        if cameras is sw.RUNNING:
            self.summary.setText(self._kind_text(None) + "\n" + reading)
            return
        if isinstance(cameras, Exception):
            cameras = []  # the reading of the shots says what is wrong with the log
        self._fill_cameras(cameras)
        kind = self._kind_text(cameras)
        path, message, camera = self.path, self.message.currentData(), self._camera()
        shots_key = ("shots", message, camera)
        shotset = self._aside.get(shots_key, lambda: sources.log_shots(path, message, camera))
        if shotset is sw.RUNNING:
            self.summary.setText(kind + "\n" + reading)
            return
        if isinstance(shotset, Exception):
            self.summary.setText(kind)
            self.error.setText(tr("Il log non si legge: {0}", engine(str(shotset))))
            return
        self.summary.setText("\n".join([kind] + self._shots_text(shotset)))
        chunk, folder, by = self.chunk, self.photos_path(), self.by.currentData()
        latency = self.lever_panel.latency_ms() / 1000.0
        pairing = self._aside.get(("pair", shots_key, str(folder), by, latency),
                                  lambda: gshots.pair(chunk, shotset, folder, by=by, latency_s=latency))
        if pairing is sw.RUNNING:
            self.pairing.setText(tr("Abbinamento delle foto in corso..."))
            return
        if isinstance(pairing, Exception):
            self.error.setText(engine(str(pairing)))
            return
        self.pairing.setText(sw.pairing_text(pairing))
        if not pairing.kept:
            self.error.setText(tr("Nessuna foto del chunk trova il suo scatto in questo log."))
            return
        self._shotset = shotset
        self.b_apply.setEnabled(True)

    # ------------------------------------------------------------ the answer
    def values(self) -> dict:
        """``{"path", "message", "camera", "by", "lever_body_mm", "latency_ms", "photos"}``: the log, the message its
        shots are read from (``"auto"`` or one of the log), the camera (None while the log has one), how the photos are
        paired (:data:`.shots.BY`), the lever arm from the antenna to the camera in the body of the drone (mm forward,
        right, down; None when all zero), the latency of the camera in milliseconds, the folder of the photos (None:
        the one of the log)."""
        return {"path": self.path, "message": self.message.currentData(), "camera": self._camera(),
                "by": self.by.currentData(), "lever_body_mm": self.lever_panel.lever(),
                "latency_ms": self.lever_panel.latency_ms(), "photos": self.photos_path()}

    def accept(self) -> None:
        if self._timer.isActive():
            self._update()
        if self._shotset is None:
            return
        sw.remember_log(self.path, self.message.currentData(), self._camera(), self._shotset)
        super().accept()

    def done(self, result: int) -> None:
        self._timer.stop()
        self._aside.stop()  # the readings still in their threads are dropped: nothing waits for them
        super().done(result)

    @staticmethod
    def ask(parent, chunk, path) -> dict | None:
        """The answer of the dialog (:meth:`values`) for the log ``path`` and the photos of ``chunk``, None when it
        is cancelled."""
        dialog = AutopilotDialog(chunk, path, parent)
        try:
            return dialog.values() if dialog.exec() == QDialog.DialogCode.Accepted else None
        finally:
            dialog.deleteLater()
