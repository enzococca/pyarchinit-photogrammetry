"""Progress reporting and cancellation shared by every processing step.

A progress callback receives ``(fraction, message)`` with ``fraction`` in 0..1.
The GUI plugs its progress bar here; scripts can pass ``print_progress``.
"""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger("pyarchinit_photogrammetry")

ProgressCallback = Callable[[float, str], None]


class Cancelled(RuntimeError):
    """Raised when the user cancels a running step."""


class Progress:
    def __init__(self, callback: ProgressCallback | None = None, start: float = 0.0, span: float = 1.0):
        self.callback = callback
        self.start = start
        self.span = span
        self.cancel_requested = False

    def __call__(self, fraction: float, message: str = "") -> None:
        if self.cancel_requested:
            raise Cancelled(message or "cancelled")
        if message:
            log.info(message)
        if self.callback is not None:
            self.callback(self.start + self.span * max(0.0, min(1.0, fraction)), message)

    def sub(self, start: float, span: float) -> "Progress":
        """Child reporter mapping its 0..1 range into ``[start, start+span]`` of this one."""
        child = Progress(self.callback, self.start + self.span * start, self.span * span)
        child.cancel_requested = self.cancel_requested
        return child

    def cancel(self) -> None:
        self.cancel_requested = True


def as_progress(cb: ProgressCallback | Progress | None) -> Progress:
    return cb if isinstance(cb, Progress) else Progress(cb)


def print_progress(fraction: float, message: str) -> None:
    print(f"[{fraction * 100:5.1f}%] {message}", flush=True)
