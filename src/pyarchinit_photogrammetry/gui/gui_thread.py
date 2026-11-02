"""Calls made in the interface thread from any thread, waiting for their result.

A script run in the worker (*Esegui script Python...*) gets the window as :class:`GuiProxy`: every attribute it reads
and every method it calls goes to the interface thread through :func:`call` (a queued call; the worker waits, the
interface never does), so a script written for the window - ``window.photos.set_filter(...)``,
``window._show_pane(...)``, ``window.refresh_all()`` - runs unchanged and no widget is touched from the worker.
Attributes that are Qt objects come back as proxies in their turn; other values come back as they are.
:func:`install` is called once by the window, in the interface thread.
"""
from __future__ import annotations

import functools
import threading

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, Signal, Slot


class _Invoker(QObject):
    job = Signal(object)

    def __init__(self):
        super().__init__()
        self.job.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _run(self, item) -> None:
        function, args, kwargs, box, done = item
        try:
            box["result"] = function(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - handed to the waiting thread
            box["error"] = exc
        finally:
            done.set()


_invoker: _Invoker | None = None


def install() -> None:
    """The receiver of the calls, in the interface thread (the window calls it once)."""
    global _invoker
    if _invoker is None and in_gui_thread():
        _invoker = _Invoker()


def in_gui_thread() -> bool:
    app = QCoreApplication.instance()
    return app is None or QThread.currentThread() == app.thread()


def call(function, *args, **kwargs):
    """``function(*args, **kwargs)`` in the interface thread, its result (or its exception) returned here."""
    if _invoker is None or in_gui_thread():
        return function(*args, **kwargs)
    box: dict = {}
    done = threading.Event()
    _invoker.job.emit((function, args, kwargs, box, done))
    done.wait()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def unwrap(value):
    """The object behind a :class:`GuiProxy` (anything else as it is)."""
    if isinstance(value, GuiProxy):
        return object.__getattribute__(value, "_target")
    return value


def _unwrap_all(args, kwargs):
    return tuple(unwrap(a) for a in args), {k: unwrap(v) for k, v in kwargs.items()}


def wrap(value):
    """What a proxy hands back: Qt objects and callables as proxies, the rest as it is."""
    if isinstance(value, GuiProxy):
        return value
    if isinstance(value, QObject):
        return GuiProxy(value)
    if callable(value):
        @functools.wraps(value)
        def in_gui(*args, **kwargs):
            a, k = _unwrap_all(args, kwargs)
            return wrap(call(value, *a, **k))

        return in_gui
    return value


class GuiProxy:
    """An object of the interface seen from another thread: attributes read, methods called and attributes set
    in the interface thread (:func:`call`)."""

    def __init__(self, target):
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name):
        return wrap(call(getattr, object.__getattribute__(self, "_target"), name))

    def __setattr__(self, name, value) -> None:
        call(setattr, object.__getattribute__(self, "_target"), name, unwrap(value))

    def __repr__(self) -> str:
        return f"<in the interface thread: {object.__getattribute__(self, '_target')!r}>"


__all__ = ["GuiProxy", "call", "in_gui_thread", "install", "unwrap", "wrap"]
