"""A project opened from the file manager: on macOS it comes as a QFileOpenEvent, not in sys.argv."""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QUrl  # noqa: E402
from PySide6.QtGui import QFileOpenEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import ppg_launcher  # noqa: E402


@pytest.fixture
def qapp_open():
    app = QApplication.instance() or QApplication([])
    ppg_launcher.on_file_open(None)
    ppg_launcher.take_opened_file()
    ppg_launcher.catch_file_open(app)
    yield app
    ppg_launcher.on_file_open(None)
    ppg_launcher.take_opened_file()


def _ask(app, path):
    QCoreApplication.sendEvent(app, QFileOpenEvent(QUrl.fromLocalFile(str(path))))


def test_a_file_asked_while_starting_waits_for_the_window(qapp_open, tmp_path):
    _ask(qapp_open, tmp_path / "primo scavo.ppg")
    _ask(qapp_open, tmp_path / "note.txt")
    _ask(qapp_open, tmp_path / "secondo.ppg")
    assert ppg_launcher.take_opened_file() == str(tmp_path / "secondo.ppg")
    assert ppg_launcher.take_opened_file() is None


def test_the_handler_gets_the_waiting_file_and_the_next_ones(qapp_open, tmp_path):
    _ask(qapp_open, tmp_path / "a.ppg")
    seen = []
    ppg_launcher.on_file_open(seen.append)
    assert seen == [str(tmp_path / "a.ppg")]
    _ask(qapp_open, tmp_path / "b.ppg")
    assert seen[-1] == str(tmp_path / "b.ppg")
    ppg_launcher.on_file_open(None)
    _ask(qapp_open, tmp_path / "c.ppg")
    assert len(seen) == 2 and ppg_launcher.take_opened_file() == str(tmp_path / "c.ppg")


def test_the_filter_is_installed_once(qapp_open):
    first = ppg_launcher._OPENED["filter"]
    ppg_launcher.catch_file_open(qapp_open)
    assert ppg_launcher._OPENED["filter"] is first
