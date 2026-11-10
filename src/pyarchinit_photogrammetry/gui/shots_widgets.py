"""What the dialogs that give the photos the positions of their shots share: the names of the sources of the shots and
of the ways to pair them with the photos, the lever arm in the body of the drone and the latency of the camera, the
folder of the photos, how a pairing is said, and the readings done in a thread of their own."""
from __future__ import annotations

import queue
import threading
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QFormLayout, QLabel, QWidget

from ..gnss import shots as gshots
from ..gnss import sources
from .i18n import decimal_comma, engine, number, tr

_GPS_EPOCH = datetime(1980, 1, 6)
RUNNING = object()  # what a reading still in its thread gives (:meth:`Aside.get`)
_last_shots: list = []  # the shots the last dialog read of an autopilot log: [(key, ShotSet)], one at most


def kind_names() -> dict[str, str]:
    """The sources of the shots (:data:`.sources.KINDS`) as the PPK dialog lists them."""
    return {"mrk": tr("MRK di DJI"), "events": tr("Eventi del ricevitore (nel file del rover)"),
            "ubx": tr("Eventi di un log u-blox (.ubx)"), "log": tr("Log dell'autopilota (ArduPilot, PX4)"),
            "events-pos": tr("File di eventi (_events.pos)")}


def by_names() -> dict[str, str]:
    """How the photos are paired with the shots (:data:`.shots.BY`)."""
    return {"auto": tr("automatico"), "number": tr("per numero"), "time": tr("per tempo"), "order": tr("per ordine")}


def message_names() -> dict[str, str]:
    """The messages of an autopilot log the shots are read from (:data:`.sources.LOG_MESSAGES`)."""
    return {"auto": tr("automatico"), "cam": tr("CAM (scatti confermati)"), "trig": tr("TRIG (comandi di scatto)"),
            "capture": tr("camera_capture (scatti confermati)"), "trigger": tr("camera_trigger (comandi di scatto)")}


def by_combo() -> QComboBox:
    combo = QComboBox()
    names = by_names()
    for by in gshots.BY:
        combo.addItem(names[by], by)
    combo.setToolTip(tr("Per numero: il numero dello scatto è nel nome della foto; per tempo: l'ora della foto e "
                        "quella dello scatto; per ordine: tante foto quanti scatti. Automatico sceglie dal tipo degli "
                        "scatti e dalle foto."))
    return combo


def fill_messages(combo: QComboBox, ulog: bool) -> None:
    """The messages of a PX4 log (``ulog``) or of an ArduPilot one in ``combo``, the one chosen kept when it is
    still there."""
    wanted = ["auto", "capture", "trigger"] if ulog else ["auto", "cam", "trig"]
    if [combo.itemData(i) for i in range(combo.count())] == wanted:
        return
    chosen, names = combo.currentData(), message_names()
    combo.blockSignals(True)
    combo.clear()
    for message in wanted:
        combo.addItem(names[message], message)
    combo.setCurrentIndex(max(combo.findData(chosen), 0))
    combo.blockSignals(False)


def gps_when(seconds: float) -> str:
    """Seconds of GPS time since the GPS epoch as a date and time, to the second."""
    return f"{_GPS_EPOCH + timedelta(seconds=float(seconds)):%Y-%m-%d %H:%M:%S}"


def photos_folder(chunk, beside=None) -> Path | None:
    """Where the photos of some shots are: the folder of the file ``beside`` when photos of ``chunk`` are in it, else
    the folder of the photos of the chunk when they are all in one; None when nothing tells (the window asks)."""
    if chunk is None:
        return None
    folders: dict[Path, Path] = {}
    for cam in chunk.cameras:
        folder = Path(cam.path).parent
        folders.setdefault(folder.resolve(), folder)
    if beside is not None:
        here = Path(beside).resolve().parent
        if here in folders:
            return folders[here]
    return next(iter(folders.values())) if len(folders) == 1 else None


def pairing_text(pairing) -> str:
    """A :class:`.shots.Pairing` in words: how many photos find their shot and how they were paired, the clock of the
    camera, the photos and the shots left over, what the times do not confirm, the warnings of the pairing."""
    text = tr("{0} foto su {1} trovano il loro scatto, abbinate {2}.", len(pairing.kept), len(pairing.files),
              by_names().get(pairing.by, pairing.by))
    if pairing.clock_s is not None:
        text += " " + tr("L'orologio della camera è a {0} s dall'UTC.", decimal_comma(f"{pairing.clock_s:+.1f}"))
    text += " " + tr("Foto senza scatto: {0}. Scatti senza foto: {1}.", len(pairing.photos_without_shot),
                     len(pairing.extra_shots))
    if pairing.invalid:
        text += " " + tr("Scatti senza un tempo valido, mai abbinati: {0}.", len(pairing.invalid))
    if pairing.doubles:
        text += " " + tr("Scatti doppi lasciati fuori: {0}.", pairing.doubles)
    if pairing.confirmed is False:
        text += " " + tr("I tempi non confermano l'abbinamento: controlla le posizioni.")
    return "\n".join([text] + [said(w) for w in pairing.warnings])


def pairing_words(result: dict) -> list[str]:
    """How the photos were paired, from what :func:`.apply.apply_shots` and :func:`.apply.apply_ppk_shots` return: a
    line for the way, the clock of the camera and what the times do not confirm, one for the double shots left out,
    one for the shots without a valid time."""
    lines = []
    by = result.get("by")
    if by:
        text = tr("Foto abbinate {0}", by_names().get(by, by))
        if result.get("clock_s") is not None:
            text += tr(", l'orologio della camera a {0} s dall'UTC", decimal_comma(f"{result['clock_s']:+.1f}"))
        if result.get("confirmed") is False:
            text += tr(" - i tempi non lo confermano: controlla le posizioni")
        lines.append(text)
    if result.get("doubles"):
        lines.append(tr("Scatti doppi lasciati fuori: {0}", result["doubles"]))
    if result.get("invalid"):
        lines.append(tr("Scatti senza un tempo valido, mai abbinati: {0}", result["invalid"]))
    return lines


def _amount(value: float) -> str:
    return number(value, 0 if float(value) == round(float(value)) else 1)


def lever_short(lever, latency_ms: float) -> str | None:
    """The lever arm (mm forward, right, down) and the latency of the camera in a few words - "0, 0, 250 mm · 40
    ms" - for where there is little room; None when the arm is all zeros (or None) and there is no latency."""
    arm = tuple(float(v) for v in lever) if lever is not None else (0.0, 0.0, 0.0)
    if not any(arm) and not latency_ms:
        return None
    return tr("{0} mm · {1} ms", ", ".join(_amount(v) for v in arm), _amount(latency_ms))


def lever_words(lever, latency_ms: float) -> str | None:
    """The lever arm and the latency a run counts with, as a sentence for a summary or the log; None when both are
    zero."""
    arm = tuple(float(v) for v in lever) if lever is not None else (0.0, 0.0, 0.0)
    if not any(arm) and not latency_ms:
        return None
    return tr("Braccio di leva dall'antenna alla camera (avanti, destra, giù): {0} mm; latenza della camera: {1} ms",
              ", ".join(_amount(v) for v in arm), _amount(latency_ms))


def log_key(path, message: str, camera) -> tuple | None:
    """What tells one reading of an autopilot log from another: the file as it is now, the message, the camera."""
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return str(Path(path).absolute()), stat.st_mtime_ns, stat.st_size, message, camera


def remember_log(path, message: str, camera, shotset) -> None:
    """Keep the shots a dialog read of an autopilot log, for the window that applies them (:func:`log_shots`)."""
    key = log_key(path, message, camera)
    _last_shots[:] = [(key, shotset)] if key is not None else []


def log_shots(path, message: str = "auto", camera=None):
    """The shots of an autopilot log: those the dialog just read of the same file, message and camera, else read now
    (:func:`.sources.log_shots`, with its errors)."""
    key = log_key(path, message, camera)
    for known, shotset in _last_shots:
        if key is not None and known == key:
            return shotset
    return sources.log_shots(path, message, camera)


class LeverPanel(QWidget):
    """The lever arm from the antenna to the camera in the body of the drone (mm forward, right, down) and the
    latency of the camera (ms): :meth:`lever` is None while the arm is all zeros."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)

        def spin(limit: float, suffix: str, tip: str) -> QDoubleSpinBox:
            box = QDoubleSpinBox()
            box.setRange(-limit, limit)
            box.setDecimals(1)
            box.setSuffix(suffix)
            box.setToolTip(tip)
            box.valueChanged.connect(lambda _value: self.changed.emit())
            return box

        tip = tr("Dove è la camera rispetto all'antenna nel corpo del drone: girato con l'assetto di ogni scatto; "
                 "senza assetto conta solo la parte verticale.")
        self.forward = spin(100000.0, " mm", tip)
        self.right = spin(100000.0, " mm", tip)
        self.down = spin(100000.0, " mm", tip)
        self.latency = spin(10000.0, " ms", tr("Quanto dopo il segnale dello scatto la camera espone: sposta i tempi "
                                               "degli scatti, non le posizioni."))
        form.addRow(tr("Camera in avanti dall'antenna"), self.forward)
        form.addRow(tr("Camera a destra dell'antenna"), self.right)
        form.addRow(tr("Camera sotto l'antenna"), self.down)
        form.addRow(tr("Latenza della camera"), self.latency)

    def lever(self) -> tuple[float, float, float] | None:
        arm = (float(self.forward.value()), float(self.right.value()), float(self.down.value()))
        return arm if any(arm) else None

    def set_lever(self, arm) -> None:
        for box, value in zip((self.forward, self.right, self.down), arm or (0.0, 0.0, 0.0)):
            box.setValue(float(value))

    def latency_ms(self) -> float:
        return float(self.latency.value())


class Aside(QObject):
    """Readings done in threads of their own, each once: :meth:`get` gives :data:`RUNNING` while a reading is in its
    thread, then what it returned, or the exception that stopped it; ``done`` is emitted when a reading ends.
    :meth:`stop` forgets the readings still running - what they give is dropped - and nothing is started after it.
    The threads leave what they read in a queue this object empties on a timer, in the thread of the interface: they
    never touch an object of Qt, so one that ends after its dialog is deleted does no harm."""

    done = Signal()
    POLL_MS = 25

    def __init__(self, parent=None):
        super().__init__(parent)
        self._known: dict = {}
        self._stopped = False
        self._results: queue.SimpleQueue = queue.SimpleQueue()
        self._running = 0
        self._poll = QTimer(self)
        self._poll.setInterval(self.POLL_MS)
        self._poll.timeout.connect(self._collect)

    def get(self, key, work):
        """What ``work()`` gives, run once for ``key`` in a thread of its own."""
        if key in self._known:
            return self._known[key]
        if self._stopped:
            return RUNNING
        self._known[key] = RUNNING
        results = self._results

        def body() -> None:
            result = error = None
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - whatever stops the reading, the dialog says it
                error = exc
            results.put((key, result, error))

        self._running += 1
        self._poll.start()
        threading.Thread(target=body, name="ppg-shots-reading", daemon=True).start()
        return RUNNING

    def _collect(self) -> None:
        read = False
        while True:
            try:
                key, result, error = self._results.get_nowait()
            except queue.Empty:
                break
            self._running -= 1
            self._known[key] = error if error is not None else result
            read = True
        if not self._running:
            self._poll.stop()
        if read and not self._stopped:
            self.done.emit()

    def stop(self) -> None:
        self._stopped = True
        self._poll.stop()


def said(message: str) -> str:
    """A message of the engine for a summary: in the language of the interface, written as a sentence - its first
    letter a capital when it starts with a plain word (a name of a file, a number, a word of the log stay as they
    are)."""
    text = engine(message)
    first = text.split(" ", 1)[0].replace("'", "").replace("\u2019", "")
    return text[0].upper() + text[1:] if first.isalpha() and first.islower() else text


def unread(exc: Exception) -> str:
    """Why a file could not be read, for a dialog: an error of the system in Italian with the short name of the file
    (never its whole path), a message of the engine in the language of the interface."""
    if isinstance(exc, OSError) and exc.filename:
        name = Path(str(exc.filename)).name
        if isinstance(exc, IsADirectoryError):
            return tr("{0} è una cartella, non un file", name)
        if isinstance(exc, FileNotFoundError):
            return tr("{0} non esiste", name)
        if isinstance(exc, PermissionError):
            return tr("{0} non si può aprire: manca il permesso di leggerlo", name)
        return f"{name}: {exc.strerror}" if exc.strerror else name
    return engine(str(exc))


def plain_label(style: str = "color: #6c7a89") -> QLabel:
    """A label for what a dialog says of a file: plain text (file names and messages, never markup), wrapped."""
    label = QLabel("")
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setStyleSheet(style)
    return label
