"""Background processing: every long step runs in a QThread with progress and cancel."""
from __future__ import annotations

import time
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

from ..core.progress import Cancelled, Progress


class Task(QObject):
    progress = Signal(float, str)
    finished = Signal(object)
    failed = Signal(str)
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
            self.failed.emit(f"{exc}\n\n{traceback.format_exc()}")
        else:
            self.finished.emit(result)

    def cancel(self) -> None:
        self.reporter.cancel()


class ProgressDialog(QDialog):
    """Modeless progress window with a cancel button (one per running task)."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        lay = QVBoxLayout(self)
        self.message = QLabel(title)
        self.message.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.elapsed = QLabel("")
        self.cancel_button = QPushButton("Annulla")
        row = QHBoxLayout()
        row.addWidget(self.elapsed)
        row.addStretch(1)
        row.addWidget(self.cancel_button)
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
            txt += f" - restante circa {el * (1 - fraction) / fraction:5.0f} s"
        self.elapsed.setText(txt)


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

    @property
    def busy(self) -> bool:
        return self.thread is not None

    def run(self, label: str, fn: Callable[[Progress], Any], on_done: Callable[[Any], None] | None = None,
            on_error: Callable[[str], None] | None = None) -> bool:
        if self.busy:
            self.status.emit("Un'elaborazione e' gia' in corso")
            return False
        self._label, self._on_done, self._on_error = label, on_done, on_error
        self.task = Task(fn, label)
        self.thread = QThread()
        self.task.moveToThread(self.thread)
        self.thread.started.connect(self.task.run)
        if self.show_dialog:
            self.dialog = ProgressDialog(label, self.parent_widget)
            self.dialog.cancel_button.clicked.connect(self.cancel)
            self.dialog.show()
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
        self.status.emit(f"{self._label}: {fraction * 100:.0f}% {message}")

    @Slot(object)
    def _on_finished(self, result) -> None:
        self._done(self._label, self._on_done, result)

    @Slot(str)
    def _on_failed(self, err: str) -> None:
        self._fail(self._label, self._on_error, err)

    @Slot()
    def _on_cancelled(self) -> None:
        self._cancelled(self._label)

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()
            if self.dialog is not None:
                self.dialog.message.setText("Annullamento in corso...")
                self.dialog.cancel_button.setEnabled(False)

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
        self.thread = self.task = self.dialog = None
        self.busy_changed.emit(False)

    def _done(self, label, on_done, result) -> None:
        self._cleanup()
        self.status.emit(f"{label}: completato")
        if on_done is not None:
            on_done(result)

    def _fail(self, label, on_error, err: str) -> None:
        self._cleanup()
        self.status.emit(f"{label}: errore")
        if on_error is not None:
            on_error(err)

    def _cancelled(self, label) -> None:
        self._cleanup()
        self.status.emit(f"{label}: annullato")
