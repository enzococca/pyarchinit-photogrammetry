"""The progress window: elapsed and remaining time, the details pane (gui/worker.py)."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from pyarchinit_photogrammetry.gui.worker import Eta, hms  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_hms():
    assert hms(0) == "0:00:00"
    assert hms(7.4) == "0:00:07"
    assert hms(3723) == "1:02:03"
    assert hms(-5) == "0:00:00"


def test_eta_from_the_recent_rate():
    eta = Eta()
    assert eta.remaining(0.0) is None
    eta.add(0.0, 0.0)
    eta.add(1.0, 0.01)
    assert eta.remaining(1.0) is None  # too early to say
    for t in range(2, 51):  # one percent a second
        eta.add(float(t), t / 100)
    assert eta.remaining(50.0) == pytest.approx(50.0, abs=1.0)
    # a stall makes the estimate grow instead of counting down to zero
    later = eta.remaining(90.0)
    assert later > 50.0
    # the recent rate wins over the whole run: twice as fast for the last 30 s
    fast = Eta()
    for t in range(0, 61):
        fast.add(float(t), 0.005 * t)
    for t in range(61, 91):
        fast.add(float(t), 0.3 + 0.01 * (t - 60))
    assert 0.3 + 0.01 * 30 == pytest.approx(0.6)
    assert fast.remaining(90.0) == pytest.approx(40.0, abs=3.0)  # (1 - 0.6) / 0.01, not / 0.0067
    fast.add(91.0, 1.0)
    assert fast.remaining(91.0) == 0.0


def test_the_window_lists_the_messages_with_their_time(qapp, tmp_path):
    from PySide6.QtCore import QSettings

    from pyarchinit_photogrammetry.gui.worker import ProgressDialog

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    dlg = ProgressDialog("Allinea foto")
    dlg.show()
    assert "Trascorso" in dlg.elapsed.text() and "restante" not in dlg.elapsed.text()
    dlg.update_progress(0.1, "Matching photos (exhaustive)")
    dlg.update_progress(0.12, "Matching photos (exhaustive)")  # the same message is not repeated
    dlg.update_progress(0.2, "Placing photo #3: 4 in the block")
    dlg.update_progress(0.2, "   it sees 100 of 200 points")
    lines = dlg.log.toPlainText().splitlines()
    assert len(lines) == 3 and lines[0].startswith("0:00:0") and lines[0].endswith("Matching photos (exhaustive)")
    assert lines[-1].endswith("it sees 100 of 200 points")
    assert dlg.message.text() == "it sees 100 of 200 points"
    assert dlg.bar.value() == 200
    assert not dlg.log.isVisible()
    dlg.details.setChecked(True)
    assert dlg.log.isVisible()
    assert QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, "pyarchinit",
                     "pyarchinit-photogrammetry").value("progress/details", False, type=bool) is True
    dlg._t0 -= 100.0  # a hundred seconds ago, a fifth done: the estimate appears
    dlg.eta = Eta()
    dlg.eta.add(dlg._t0, 0.0)
    dlg.update_progress(0.2, "Placing photo #4: 5 in the block")
    assert "Trascorso 0:01:40" in dlg.elapsed.text() and "restante circa 0:06:" in dlg.elapsed.text()
    dlg.close()
    again = ProgressDialog("Costruisci nuvola densa")  # remembers that the details were open
    assert again.details.isChecked()
    again.close()
