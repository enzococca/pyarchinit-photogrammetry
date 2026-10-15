"""Run a function in a separate process so that a native crash (abort, segfault,
``exit()`` called from C++) becomes a Python exception instead of killing the caller.

Third-party reconstruction code (PoissonRecon inside Open3D and COLMAP) terminates the
whole process on some inputs. The desktop interface and long pipelines must survive
that, so the risky call runs in a fresh interpreter started with :mod:`subprocess`.

Why not :mod:`multiprocessing`: its ``spawn`` start method still ``fork()``s the parent
before ``exec``, and on macOS a process that has loaded rasterio/PROJ, pycolmap and
SpatiaLite crashes in that fork (the ``atfork`` handlers of the mixed native
libraries). ``subprocess`` uses ``posix_spawn`` when ``close_fds`` is False, which does
not run them. The request (function, arguments, ``sys.path``) travels as a pickle file;
the result comes back the same way; stdout/stderr of the child go to a log file whose
tail is reported when the child dies.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import pickle
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable

WORKER_FLAG = "--ppg-worker"


class IsolatedCrash(RuntimeError):
    """The child process ended without returning a result."""

    def __init__(self, message: str, exit_code: int | None = None):
        super().__init__(message)
        self.exit_code = exit_code


def _command(request: Path, result: Path) -> list[str]:
    exe = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False):  # packaged application: the launcher dispatches the flag
        return [exe, WORKER_FLAG, str(request), str(result)]
    return [exe, "-c", "from pyarchinit_photogrammetry.engine.isolate import worker_main; worker_main()",
            str(request), str(result)]


def run_isolated(fn: Callable[..., Any], *args, timeout: float | None = None,
                 cancel_check: Callable[[], bool] | None = None, **kwargs) -> Any:
    """Call ``fn(*args, **kwargs)`` in a child process and return its result.

    ``fn`` must be a module-level function; arguments and result must be picklable
    (numpy arrays, paths...). Raises :class:`IsolatedCrash` when the child dies without
    a result, re-raises a ``RuntimeError`` with the child's traceback when ``fn``
    raised, ``TimeoutError`` when ``timeout`` seconds pass and ``InterruptedError``
    when ``cancel_check()`` returns True (polled four times a second).
    """
    with tempfile.TemporaryDirectory(prefix="ppg-worker-") as tmp:
        request, result, log_path = Path(tmp) / "request.pkl", Path(tmp) / "result.pkl", Path(tmp) / "worker.log"
        with open(request, "wb") as f:
            pickle.dump({"module": fn.__module__, "name": fn.__qualname__, "args": args, "kwargs": kwargs,
                         "sys_path": list(sys.path), "cwd": os.getcwd()}, f, protocol=pickle.HIGHEST_PROTOCOL)
        env = dict(os.environ)
        env["PPG_WORKER"] = "1"
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(_command(request, result), stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, env=env, close_fds=False)
        t0 = time.monotonic()
        try:
            while proc.poll() is None:
                if cancel_check is not None and cancel_check():
                    _kill(proc)
                    raise InterruptedError(f"{_name(fn)} cancelled")
                if timeout is not None and time.monotonic() - t0 > timeout:
                    _kill(proc)
                    raise TimeoutError(f"{_name(fn)} did not finish within {timeout} s")
                time.sleep(0.25)
        finally:
            if proc.poll() is None:  # pragma: no cover
                _kill(proc)
        if result.exists():
            with open(result, "rb") as f:
                payload = pickle.load(f)
            if payload["status"] == "ok":
                return payload["value"]
            raise RuntimeError(f"{_name(fn)} failed in the worker process:\n{payload['traceback']}")
        tail = _tail(log_path)
        raise IsolatedCrash(f"{_name(fn)} crashed in the worker process (exit code {proc.returncode})"
                            + (f"\n{tail}" if tail else ""), proc.returncode)


def worker_main(argv: list[str] | None = None) -> int:
    """Entry point of the child: run the pickled request, write the pickled result."""
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = [a for a in argv if a != WORKER_FLAG]
    request, result = Path(argv[0]), Path(argv[1])
    with open(request, "rb") as f:
        req = pickle.load(f)
    for p in reversed(req.get("sys_path", [])):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        import importlib

        fn = importlib.import_module(req["module"])
        for part in req["name"].split("."):
            fn = getattr(fn, part)
        value = fn(*req["args"], **req["kwargs"])
        payload = {"status": "ok", "value": value}
    except BaseException:
        payload = {"status": "error", "traceback": traceback.format_exc()}
    tmp = result.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, result)  # the parent only trusts a complete file
    return 0


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:  # pragma: no cover
        pass
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pass


def _tail(path: Path, n: int = 12) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    lines = [ln for ln in lines if ln.strip()]
    return "\n".join(lines[-n:])


def _name(fn) -> str:
    return getattr(fn, "__qualname__", repr(fn))


def freeze_support() -> None:
    """For frozen executables: keeps multiprocessing usable if a library needs it."""
    mp.freeze_support()


__all__ = ["IsolatedCrash", "run_isolated", "worker_main", "freeze_support", "WORKER_FLAG"]
