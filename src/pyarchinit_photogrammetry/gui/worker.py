"""Background processing: every long step runs in a QThread with progress and cancel."""
from __future__ import annotations

import logging
import time
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QSize, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton,
                               QToolButton, QVBoxLayout, QWidget)

from ..core.progress import Cancelled, Progress
from . import app_settings
from .i18n import tr, translate
from .icons import icon

log = logging.getLogger("pyarchinit_photogrammetry.gui")

LOG_LINES = 2000  # lines kept in the details pane of the progress window


def hms(seconds: float) -> str:
    """A duration as ``h:mm:ss`` (``0:00:07``, ``1:02:03``), as Metashape's progress window shows it."""
    s = max(0, int(round(seconds)))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


class Eta:
    """The time a step still needs, from its progress so far: the rate of the recent samples
    (the last third of the elapsed time, at least ``window`` seconds), the whole run when they say
    nothing, smoothed so the estimate does not jump; None before 2 % is done. The rate is measured
    up to now, not up to the last sample: a step that stalls sees its estimate grow, not count down."""

    def __init__(self, window: float = 20.0, share: float = 0.34):
        self.samples: list[tuple[float, float]] = []
        self.window, self.share = window, share
        self.estimate: float | None = None

    def add(self, t: float, fraction: float) -> None:
        if self.samples and t - self.samples[-1][0] < 0.5 and fraction < 1:
            return
        self.samples.append((t, fraction))

    def remaining(self, t: float) -> float | None:
        if not self.samples:
            return None
        t0, f0 = self.samples[0]
        t1, f1 = self.samples[-1]
        if f1 >= 1:
            return 0.0
        if f1 < 0.02:
            return None
        back = max(self.window, self.share * (t - t0))
        older = [(ts, fs) for ts, fs in self.samples if t1 - ts >= back]
        ts, fs = older[-1] if older else (t0, f0)
        rate = (f1 - fs) / max(t - ts, 1e-6)
        if rate <= 0:
            rate = (f1 - f0) / max(t - t0, 1e-6)
        if rate <= 0:
            return None
        left = (1.0 - f1) / rate
        self.estimate = left if self.estimate is None else 0.7 * self.estimate + 0.3 * left
        return self.estimate


class Task(QObject):
    progress = Signal(float, str)
    finished = Signal(object)
    failed = Signal(str, str)  # summary, traceback
    cancelled = Signal()

    def __init__(self, fn: Callable[[Progress], Any], label: str):
        super().__init__()
        self.fn = fn
        self.label = label
        self.reporter = Progress(self._emit)
        self._last = 0.0

    def _emit(self, fraction: float, message: str) -> None:
        now = time.monotonic()
        if now - self._last > 0.05 or fraction >= 1.0:  # do not flood the event loop
            self._last = now
            self.progress.emit(float(fraction), message)

    @Slot()
    def run(self) -> None:
        try:
            result = self.fn(self.reporter)
        except Cancelled:
            self.cancelled.emit()
        except Exception as exc:  # reported in the GUI
            text = str(exc).strip() or type(exc).__name__
            self.failed.emit(f"{self.label}: {text}", traceback.format_exc())
        else:
            self.finished.emit(result)

    def cancel(self) -> None:
        self.reporter.cancel()


class ProgressDialog(QDialog):
    """Modal progress window with a cancel button (one per running task): the bar, the current
    message, the elapsed time and the estimate of the time left (ticking every second, also while
    the step says nothing), and a details pane with every message of the step and its time - the
    photos matched, the photos placed in the block, the adjustments - as Metashape's console."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        head = QLabel(title)
        head.setObjectName("errorSummary")
        self.message = QLabel(tr("Avvio..."))
        self.message.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(True)
        self.elapsed = QLabel("")
        self.elapsed.setObjectName("pageSubtitle")
        self.details = QToolButton()
        self.details.setText(tr("Dettagli"))
        self.details.setCheckable(True)
        self.details.setAutoRaise(True)
        self.details.setArrowType(Qt.ArrowType.RightArrow)
        self.details.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.log = QPlainTextEdit()
        self.log.setObjectName("consoleOut")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(LOG_LINES)
        self.log.setMinimumHeight(180)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log.hide()
        self.cancel_button = QPushButton(icon("cancel"), tr("Annulla"))
        row = QHBoxLayout()
        row.addWidget(self.elapsed)
        row.addStretch(1)
        row.addWidget(self.details)
        row.addWidget(self.cancel_button)
        lay.addWidget(head)
        lay.addWidget(self.message)
        lay.addWidget(self.bar)
        lay.addLayout(row)
        lay.addWidget(self.log, 1)
        self._t0 = time.monotonic()
        self._fraction = 0.0
        self._last_message = ""
        self.eta = Eta()
        self.details.toggled.connect(self._show_details)
        shown = False
        try:
            shown = bool(app_settings().value("progress/details", False, type=bool))
        except Exception:  # noqa: BLE001 - no settings store (scripts)
            pass
        if shown:
            self.details.setChecked(True)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.tick()

    def update_progress(self, fraction: float, message: str) -> None:
        self._fraction = fraction
        self.bar.setValue(int(fraction * 1000))
        self.eta.add(time.monotonic(), fraction)
        message = (message or "").strip()
        if message and message != self._last_message:
            self._last_message = message
            self.message.setText(message)
            self.log.appendPlainText(f"{hms(time.monotonic() - self._t0)}  {message}")
            bar = self.log.verticalScrollBar()
            bar.setValue(bar.maximum())
        self.tick()

    def tick(self) -> None:
        """The elapsed time and the estimate of the time left, once a second."""
        now = time.monotonic()
        txt = tr("Trascorso {0}", hms(now - self._t0))
        left = self.eta.remaining(now)
        if left is not None and self._fraction < 1:
            txt += tr("  -  restante circa {0}", hms(left))
        self.elapsed.setText(txt)

    def _show_details(self, on: bool) -> None:
        self.log.setVisible(on)
        self.details.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        try:
            app_settings().setValue("progress/details", bool(on))
        except Exception:  # noqa: BLE001
            pass
        if not on:
            self.adjustSize()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt
        self.timer.stop()
        super().closeEvent(event)


class StatusProgress(QWidget):
    """Compact progress indicator for the status bar: label, bar and a stop button."""

    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.label = QLabel("")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedWidth(180)
        self.bar.setTextVisible(False)
        self.stop = QToolButton()
        self.stop.setIcon(icon("cancel"))
        self.stop.setIconSize(QSize(16, 16))
        self.stop.setToolTip(tr("Annulla l'elaborazione"))
        self.stop.setAutoRaise(True)
        self.stop.clicked.connect(self.cancel_requested)
        lay.addWidget(self.label)
        lay.addWidget(self.bar)
        lay.addWidget(self.stop)
        self.hide()

    def start(self, label: str) -> None:
        self.label.setText(label)
        self.bar.setValue(0)
        self.stop.setEnabled(True)
        self.show()

    def update_progress(self, fraction: float, message: str) -> None:
        self.bar.setValue(int(fraction * 1000))

    def finish(self) -> None:
        self.hide()


class TaskRunner(QObject):
    """Runs one task at a time; the main window disables processing while busy."""

    busy_changed = Signal(bool)
    status = Signal(str)

    def __init__(self, parent_widget=None):
        super().__init__(parent_widget)
        self.parent_widget = parent_widget
        self.thread: QThread | None = None
        self.task: Task | None = None
        self.dialog: ProgressDialog | None = None
        self.show_dialog = True
        self.status_widget: StatusProgress | None = None

    @property
    def busy(self) -> bool:
        return self.thread is not None

    def run(self, label: str, fn: Callable[[Progress], Any], on_done: Callable[[Any], None] | None = None,
            on_error: Callable[[str, str], None] | None = None, quiet: bool = False) -> bool:
        """Run ``fn(progress)`` in the worker thread. ``quiet``: no modal progress window, only
        the status-bar indicator (for short background jobs such as database connections)."""
        if self.busy:
            self.status.emit(tr("Un'elaborazione è già in corso"))
            return False
        label = tr(label)
        self._label, self._on_done, self._on_error = label, on_done, on_error
        self._t0 = time.monotonic()
        log.info("%s: avvio", label)
        self.task = Task(fn, label)
        self.thread = QThread()
        self.task.moveToThread(self.thread)
        self.thread.started.connect(self.task.run)
        if self.show_dialog and not quiet:
            self.dialog = ProgressDialog(label, self.parent_widget)
            self.dialog.cancel_button.clicked.connect(self.cancel)
            self.dialog.show()
        if self.status_widget is not None:
            self.status_widget.start(label)
        # bound slots of this object (main thread): the connections are queued, so the
        # callbacks run in the GUI thread, never in the worker
        self.task.progress.connect(self._on_progress)
        self.task.finished.connect(self._on_finished)
        self.task.failed.connect(self._on_failed)
        self.task.cancelled.connect(self._on_cancelled)
        self.busy_changed.emit(True)
        self.thread.start()
        return True

    @Slot(float, str)
    def _on_progress(self, fraction: float, message: str) -> None:
        message = translate(message)  # the engine writes in English or Italian
        if self.dialog is not None:
            self.dialog.update_progress(fraction, message)
        if self.status_widget is not None:
            self.status_widget.update_progress(fraction, message)
        self.status.emit(f"{self._label}: {fraction * 100:.0f}%  {message}")

    @Slot(object)
    def _on_finished(self, result) -> None:
        self._done(self._label, self._on_done, result)

    @Slot(str, str)
    def _on_failed(self, summary: str, details: str) -> None:
        self._fail(self._label, self._on_error, summary, details)

    @Slot()
    def _on_cancelled(self) -> None:
        self._cancelled(self._label)

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()
            if self.dialog is not None:
                self.dialog.message.setText(tr("Annullamento in corso..."))
                self.dialog.cancel_button.setEnabled(False)
            if self.status_widget is not None:
                self.status_widget.stop.setEnabled(False)

    def wait(self, timeout_s: float = 600.0) -> None:
        """Block (processing events) until the current task ends: for scripts and tests."""
        t0 = time.monotonic()
        while self.busy and time.monotonic() - t0 < timeout_s:
            QApplication.processEvents()
            time.sleep(0.01)

    def _cleanup(self) -> None:
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait()
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
        if self.status_widget is not None:
            self.status_widget.finish()
        self.thread = self.task = self.dialog = None
        self.busy_changed.emit(False)

    def _elapsed(self) -> str:
        return f"{time.monotonic() - self._t0:.0f} s"

    def _done(self, label, on_done, result) -> None:
        self._cleanup()
        log.info("%s: completato in %s", label, self._elapsed())
        self.status.emit(tr("{0}: completato ({1})", label, self._elapsed()))
        if on_done is not None:
            on_done(result)

    def _fail(self, label, on_error, summary: str, details: str) -> None:
        self._cleanup()
        log.error("%s: errore dopo %s\n%s", label, self._elapsed(), details.rstrip())
        self.status.emit(tr("{0}: errore", label))
        if on_error is not None:
            on_error(summary, details)

    def _cancelled(self, label) -> None:
        self._cleanup()
        log.info("%s: annullato", label)
        self.status.emit(tr("{0}: annullato", label))
