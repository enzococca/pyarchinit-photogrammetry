"""Crash isolation: native crashes in a child process become Python exceptions."""
from __future__ import annotations

import os
import time

import numpy as np
import pytest

from pyarchinit_photogrammetry.engine.isolate import IsolatedCrash, run_isolated


def _double(a: np.ndarray, k: float = 2.0) -> np.ndarray:
    return a * k


def _abort() -> None:
    os.abort()


def _exit_zero() -> None:
    os._exit(0)  # like PoissonRecon calling exit() from C++


def _raise() -> None:
    raise ValueError("bad input")


def _sleep(s: float) -> None:
    time.sleep(s)


def test_result_comes_back():
    out = run_isolated(_double, np.arange(4.0), k=3.0)
    assert np.allclose(out, [0, 3, 6, 9])


def test_abort_becomes_exception():
    with pytest.raises(IsolatedCrash) as info:
        run_isolated(_abort)
    assert info.value.exit_code not in (None, 0)


def test_silent_exit_becomes_exception():
    with pytest.raises(IsolatedCrash):
        run_isolated(_exit_zero)


def test_python_error_is_reraised_with_traceback():
    with pytest.raises(RuntimeError, match="bad input"):
        run_isolated(_raise)


def test_timeout_kills_child():
    with pytest.raises(TimeoutError):
        run_isolated(_sleep, 30.0, timeout=1.0)


def test_cancel_kills_child():
    flag = {"stop": False}
    t0 = time.monotonic()

    def cancelled() -> bool:
        return time.monotonic() - t0 > 0.5

    with pytest.raises(InterruptedError):
        run_isolated(_sleep, 30.0, cancel_check=cancelled)
    assert time.monotonic() - t0 < 10
