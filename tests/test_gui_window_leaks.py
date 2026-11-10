"""The main window of a test does not outlive it: Qt deletes it between the tests.

Closing a window only hides it. Left to themselves the windows of a whole run (some 1500, a thousand
widgets each) took the process to 13 GB and the CI to its timeout (10 November 2026)."""

import pytest

pytest.importorskip("PySide6")


_SEEN: dict = {}


def test_a_test_opens_a_window_and_closes_it(trench):
    w, app, _mw = trench
    app.processEvents()
    _SEEN["window"] = w
    _SEEN["widgets"] = len(app.allWidgets())
    assert _SEEN["widgets"] > 500


def test_the_window_of_the_test_before_is_gone():
    """Its widgets went with it. Windows and widgets other tests of the run left (a window a module
    shares, still shown) are not counted: only the one of the test before."""
    import shiboken6
    from PySide6.QtWidgets import QApplication

    if "window" not in _SEEN:
        pytest.skip("the test before did not run")
    assert not shiboken6.isValid(_SEEN.pop("window"))
    assert len(QApplication.instance().allWidgets()) < _SEEN["widgets"] - 500
