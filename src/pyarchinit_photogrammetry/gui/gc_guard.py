"""Python's cyclic garbage collector in the GUI thread only, while the interface runs.

PySide deletes a Qt object when the garbage collector frees its Python wrapper, in the thread
where the collection runs, and Python starts an automatic collection in whichever thread makes
the allocation that crosses the threshold. With the interface open most allocations happen in
other threads - the QThread of the TaskRunner, the thread pools of the engine, the reader of the
raster view - so a window, a dialog or a view left in a reference cycle (a frame kept by a stored
traceback, a bound method kept on the object itself) is often destroyed there. Qt cannot stop
the timers of an object from another thread ("QObject::~QObject: Timers cannot be stopped from
another thread"): they stay registered with the GUI thread, which later hands a timer event to
the deleted object - a segmentation fault in ``QTimerInfoList::activateTimers`` /
``QCoreApplication::notifyInternal2``. That was the crash of the CI on 21-23 October 2026: a
RasterView kept alive by the frame of a test (the traceback in a CliRunner result points back to
it), destroyed by a collection in a worker thread of the next tests, and the next
``processEvents`` of the GUI thread.

:func:`install` turns the automatic collection off and runs it from a timer of the GUI thread,
with Python's thresholds, so the objects of the interface are always destroyed in the GUI thread.
``gui.main`` installs it for the life of the event loop.
"""
from __future__ import annotations

import gc
import time

from PySide6.QtCore import QObject, QTimer


def due_generation(counts, threshold, full_allowed: bool = True) -> int | None:
    """The generation Python's automatic collection would collect now, given the ``counts`` and
    the ``threshold`` of the generations (``gc.get_count()``, ``gc.get_threshold()``); None when
    nothing is due. The oldest generation only when ``full_allowed``, otherwise the middle one."""
    t0, t1, t2 = tuple(threshold)[:3]
    c0, c1, c2 = tuple(counts)[:3]
    if t0 <= 0 or c0 <= t0:  # a first threshold of 0: the program turned the collection off
        return None
    if c1 <= t1:
        return 0
    if c2 <= t2 or not full_allowed:
        return 1
    return 2


class GuiThreadCollector(QObject):
    """Python's automatic collection, run by a timer of the GUI thread: every ``interval_ms``
    the generation that is due (:func:`due_generation`) is collected here. A full collection (the
    oldest generation, the expensive one) at most every ``full_every_s`` seconds: Python itself
    skips most of them when few objects have reached the oldest generation since the last one."""

    def __init__(self, interval_ms: int = 500, full_every_s: float = 60.0, parent=None):
        super().__init__(parent)
        self.threshold = gc.get_threshold()
        self.full_every_s = float(full_every_s)
        self._last_full = time.monotonic()
        self._was_enabled = gc.isenabled()
        gc.disable()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.collect)
        self.timer.start(int(interval_ms))

    def collect(self) -> int:
        """Collect the generation that is due, if any; the number of unreachable objects found."""
        if gc.isenabled():  # something turned the automatic collection on again
            gc.disable()
        generation = due_generation(gc.get_count(), self.threshold,
                                    time.monotonic() - self._last_full >= self.full_every_s)
        if generation is None:
            return 0
        found = gc.collect(generation)
        if generation == 2:
            self._last_full = time.monotonic()
        return found

    def stop(self) -> None:
        """The automatic collection back as it was before (the event loop has ended)."""
        self.timer.stop()
        if self._was_enabled:
            gc.enable()


def install(parent=None, interval_ms: int = 500, full_every_s: float = 60.0) -> GuiThreadCollector:
    """Collect garbage in this thread - the GUI thread, where the QApplication was made - and
    nowhere else, until :meth:`GuiThreadCollector.stop`."""
    return GuiThreadCollector(interval_ms, full_every_s, parent)


__all__ = ["GuiThreadCollector", "due_generation", "install"]
