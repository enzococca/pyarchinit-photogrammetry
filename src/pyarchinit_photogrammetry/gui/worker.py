"""Background processing: every long step runs in a QThread with progress and cancel."""
from __future__ import annotations

import logging
import time
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QSize, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QToolButton,
                               QVBoxLayout, QWidget)

from ..core.progress import Cancelled, Progress
from .icons import icon

log = logging.getLogger("pyarchinit_photogrammetry.gui")


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
    """Modal progress window with a cancel button (one per running task)."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        head = QLabel(title)
        head.setObjectName("errorSummary")
        self.message = QLabel("Avvio...")
        self.message.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(True)
        self.elapsed = QLabel("")
        self.elapsed.setObjectName("pageSubtitle")
        self.cancel_button = QPushButton(icon("cancel"), "Annulla")
        row = QHBoxLayout()
        row.addWidget(self.elapsed)
        row.addStretch(1)
        row.addWidget(self.cancel_button)
        lay.addWidget(head)
        lay.addWidget(self.message)
        lay.addWidget(self.bar)
        lay.addLayout(row)
        self._t0 = time.monotonic()

    def update_progress(self, fraction: float, message: str) -> None:
        self.bar.setValue(int(fraction * 1000))
        if message:
            self.message.setText(message)
        el = time.monotonic() - self._t0
        txt = f"Trascorso {el:5.0f} s"
        if 0.02 < fraction < 1:
            txt += f"  -  restante circa {el * (1 - fraction) / fraction:5.0f} s"
        self.elapsed.setText(txt)


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
        self.stop.setToolTip("Annulla l'elaborazione")
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
            self.status.emit("Un'elaborazione e' gia' in corso")
            return False
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
                self.dialog.message.setText("Annullamento in corso...")
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
        self.status.emit(f"{label}: completato ({self._elapsed()})")
        if on_done is not None:
            on_done(result)

    def _fail(self, label, on_error, summary: str, details: str) -> None:
        self._cleanup()
        log.error("%s: errore dopo %s\n%s", label, self._elapsed(), details.rstrip())
        self.status.emit(f"{label}: errore")
        if on_error is not None:
            on_error(summary, details)

    def _cancelled(self, label) -> None:
        self._cleanup()
        log.info("%s: annullato", label)
        self.status.emit(f"{label}: annullato")
