"""Running a Python script on the open project, as *Esegui script Python...* and the assistant's *Esegui* do.

:func:`run_script` compiles and executes the source in a namespace the caller gives (the window builds it in the
interface thread: ``doc``, ``chunk``, ``app``, ``ppg``, ``np``, the selection), with ``progress`` - the reporter of
the step - and ``cancel()`` added: ``cancel()`` raises :class:`~..core.progress.Cancelled` when the user pressed
*Annulla* in the progress window and returns False otherwise, for long loops. What the script prints is captured
and every line also goes to the progress (the details of the progress window). An error of the script does not
propagate: it comes back in :class:`ScriptResult` with its traceback and the output printed before it; a script
calling ``exit()`` / ``sys.exit()`` ends there and never quits the program. Only a cancellation propagates (a
``KeyboardInterrupt`` raised by the script is one). What the script's thread prints is captured; the other threads
print where they did (:func:`capture`).

Some calls change things outside the project that *Annulla lo script* cannot take back - the PyArchInit database,
the live GeoPackage of QGIS, files exported elsewhere. :class:`ExternalCalls` wraps those callables (``EXTERNAL``)
while the script runs: the calls go through as usual and are listed, for the window to say them afterwards.
"""
from __future__ import annotations

import contextlib
import functools
import importlib
import io
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field

from ..core.progress import Cancelled, Progress

# (module, class or None for the module's functions, names: a name ending in "_" is a prefix)
EXTERNAL = [("pyarchinit_photogrammetry.pyarchinit.bridge", "PyArchInit",
             ("publish_", "create_us", "update_us", "delete_unit", "register_media", "save_pottery")),
            ("pyarchinit_photogrammetry.io.qgis_link", "LiveLink", ("write",)),
            ("pyarchinit_photogrammetry.io.exports", None, ("export_",))]


@dataclass
class ScriptResult:
    output: str
    error: str | None = None
    traceback: str | None = None
    external: list[str] = field(default_factory=list)
    elapsed: float = 0.0


def _matches(name: str, names) -> bool:
    return any(name.startswith(n) if n.endswith("_") else name == n for n in names)


class ExternalCalls:
    """While inside: the callables of :data:`EXTERNAL` wrapped, each call recorded in ``calls`` as
    ``"PyArchInit.publish_units(...)"`` (once per call, in order) and then made as usual. On exit everything is
    put back as it was. One at a time: a second one inside the first records nothing more."""

    _active = threading.Lock()

    def __init__(self):
        self.calls: list[str] = []
        self._patched: list[tuple[object, str, object]] = []
        self._owner = False
        self._thread = threading.get_ident()

    def __enter__(self) -> "ExternalCalls":
        self._thread = threading.get_ident()
        if not ExternalCalls._active.acquire(blocking=False):
            return self
        self._owner = True
        for module_name, class_name, names in EXTERNAL:
            try:
                module = importlib.import_module(module_name)
            except Exception:  # noqa: BLE001 - an optional part (a dependency missing): nothing to wrap
                continue
            target = getattr(module, class_name, None) if class_name else module
            if target is None:
                continue
            owner = class_name or module_name.rsplit(".", 1)[-1]
            attrs = vars(target)
            for name in list(attrs):
                original = attrs[name]
                if name.startswith("__") or not _matches(name, names) or not callable(original):
                    continue
                setattr(target, name, self._wrap(original, f"{owner}.{name}(...)"))
                self._patched.append((target, name, original))
        return self

    def _wrap(self, function, text: str):
        @functools.wraps(function)
        def recorded(*args, **kwargs):
            if threading.get_ident() == self._thread:  # the script's calls, not those of the window meanwhile
                self.calls.append(text)
            return function(*args, **kwargs)

        return recorded

    def __exit__(self, *exc) -> None:
        for target, name, original in reversed(self._patched):
            setattr(target, name, original)
        self._patched.clear()
        if self._owner:
            self._owner = False
            ExternalCalls._active.release()


class _Lines(io.TextIOBase):
    """What the script prints: kept whole, and each complete line to the progress."""

    def __init__(self, progress: Progress):
        self.progress = progress
        self.parts: list[str] = []
        self._line = ""
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        with self._lock:
            self.parts.append(s)
            self._line += s
            lines = []
            while "\n" in self._line:
                line, self._line = self._line.split("\n", 1)
                if line.strip():
                    lines.append(line.rstrip())
        for line in lines:
            self.progress.message(line)
        return len(s)

    def flush_line(self) -> None:
        with self._lock:
            line, self._line = self._line, ""
        if line.strip():
            self.progress.message(line.rstrip())

    @property
    def text(self) -> str:
        with self._lock:
            return "".join(self.parts)


class _Router:
    """``sys.stdout`` or ``sys.stderr`` that sends what a thread writes to the stream that thread captures
    (:func:`capture`), and what any other thread writes to the stream there was before (``original``)."""

    def __init__(self, original):
        self.original = original
        self.targets: dict[int, object] = {}

    def _stream(self):
        return self.targets.get(threading.get_ident(), self.original)

    def write(self, s: str) -> int:
        stream = self._stream()
        if stream is None:  # no console (a windowed app): nowhere to write
            return len(s)
        return stream.write(s)

    def flush(self) -> None:
        stream = self._stream()
        if stream is not None and hasattr(stream, "flush"):
            stream.flush()

    def __getattr__(self, name):
        if self.original is None:  # a windowed app has no console: what a stream says when there is none
            defaults = {"encoding": "utf-8", "errors": "strict", "closed": False, "isatty": lambda: False,
                        "writable": lambda: True, "readable": lambda: False, "seekable": lambda: False}
            if name in defaults:
                return defaults[name]
            if name == "fileno":
                def fileno():
                    raise io.UnsupportedOperation("no console")
                return fileno
            raise AttributeError(name)
        return getattr(self.original, name)


_routers_lock = threading.Lock()


def _router(name: str) -> _Router:
    with _routers_lock:
        current = getattr(sys, name)
        if not isinstance(current, _Router):
            current = _Router(current)
            setattr(sys, name, current)
        return current


@contextlib.contextmanager
def capture(stream):
    """While inside, what THIS thread prints (``sys.stdout`` and ``sys.stderr``) goes to ``stream``; the other
    threads print where they did. ``sys.stdout`` itself is never swapped back and forth: a router stays in its place
    and the capture of the thread is taken away in the end."""
    ident = threading.get_ident()
    routers = [_router("stdout"), _router("stderr")]
    before = [r.targets.get(ident) for r in routers]
    for r in routers:
        r.targets[ident] = stream
    try:
        yield stream
    finally:
        for r, b in zip(routers, before):
            if b is None:
                r.targets.pop(ident, None)
            else:
                r.targets[ident] = b


def _exit_error(exc: SystemExit) -> str | None:
    code = exc.code
    if code is None or code == 0:
        return None
    return f"exit({code!r})"


def run_script(source: str, namespace: dict, progress: Progress, name: str = "script") -> ScriptResult:
    """Run ``source`` in ``namespace`` (``progress`` and ``cancel`` added); see the module. Returns what it
    printed, its error and traceback (None when it ended well) and the external calls it made."""
    namespace.setdefault("__name__", "__main__")
    namespace["progress"] = progress

    def cancel() -> bool:
        if progress.cancel_requested:
            raise Cancelled("cancelled")
        return False

    namespace["cancel"] = cancel

    def stop(code=None):  # the builtin exit() of the site module would also close sys.stdin
        raise SystemExit(code)

    namespace.setdefault("exit", stop)
    namespace.setdefault("quit", stop)
    out = _Lines(progress)
    error = trace = None
    t0 = time.monotonic()
    external = ExternalCalls()
    with external, capture(out):
        try:
            code = compile(source, name, "exec")
            exec(code, namespace)  # noqa: S102 - the user's own script, run on the user's click
        except Cancelled:
            raise
        except KeyboardInterrupt as exc:  # a script's Ctrl+C: stopped as with the Annulla button
            raise Cancelled("cancelled") from exc
        except SystemExit as exc:  # exit() ends the script, never the program
            error = _exit_error(exc)
        except BaseException as exc:  # noqa: BLE001 - the script's error is its result
            text = str(exc).strip()
            error = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
            trace = traceback.format_exc()
        out.flush_line()
    return ScriptResult(out.text, error, trace, list(external.calls), time.monotonic() - t0)


__all__ = ["EXTERNAL", "ExternalCalls", "ScriptResult", "capture", "run_script"]
