"""The interface of the shots of self-built drones: the sources of the shots, raw logs and the lever arm in the PPK
dialog, the dialog of the autopilot log, its button in the GNSS pane, the messages of the engine in Italian."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

import pyarchinit_photogrammetry as ppg  # noqa: E402
from pyarchinit_photogrammetry.gnss import sources  # noqa: E402
from pyarchinit_photogrammetry.gui import i18n  # noqa: E402
import gnss_dataflash  # noqa: E402
import gnss_ulog  # noqa: E402
import test_gnss_cli_phase4 as cli_test  # noqa: E402
import test_gnss_convert as convert_test  # noqa: E402
import test_gnss_ppk_events as events_test  # noqa: E402
from gnss_helpers import needs_rtklib  # noqa: E402

PHASE4 = events_test.PHASE4
needs_a_converter = cli_test.needs_a_converter
FIELD = re.compile(r"\{\d+[^{}]*\}")


@pytest.fixture
def qt_app(tmp_path):
    """The application of the interface, its preferences in INI files of the test."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    return app


def _wait_until(condition, seconds: float = 10.0) -> bool:
    from PySide6.QtTest import QTest

    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(20)
    return bool(condition())


def _events_chunk(tmp_path: Path):
    """A chunk with a photo for each of the first three marks of the u-blox sample, on the clock of their camera."""
    times = [t for _m, t in events_test.MARKS[:3]]
    project, folder = cli_test._project(tmp_path, {i: t for i, t in enumerate(times, 1)}, events_test.LEAP_2008,
                                        zone=events_test.ZONE)
    return ppg.Document.open(project), folder, project


def _px4_flight(tmp_path: Path, **options):
    log, expected = cli_test._px4_log(tmp_path, **options)
    project, folder = cli_test._project(tmp_path, {e["seq"]: e["true"] for e in expected}, gnss_ulog.LEAP_2025)
    return log, expected, project, folder


def _dialog(qt_app, *args, **kwargs):
    from pyarchinit_photogrammetry.gui.ppk_dialog import PpkDialog

    dialog = PpkDialog(*args, **kwargs)
    dialog.show()
    qt_app.processEvents()
    return dialog


def _close(qt_app, dialog) -> None:
    dialog.reject()
    dialog.deleteLater()
    qt_app.processEvents()


# ------------------------------------------------------------------ messages of the engine
def test_engine_messages_in_italian():
    """The messages the engine writes in English are shown in Italian: progress, warnings of the base, of the PPP and
    of the conversion, the refusal of a D-RTK log; the values kept; several lines, each its own; what the catalog does
    not know, and every Italian text, as it is; in English nothing changes."""
    i18n.set_current("it")
    engine = i18n.engine
    assert engine("Contacting igs.bkg.bund.de") == "Contatto igs.bkg.bund.de"
    assert engine("Contacting cddis.nasa.gov (attempt 2 of 3)") == "Contatto cddis.nasa.gov (tentativo 2 di 3)"
    assert engine("Converting rover.ubx (ubx) to RINEX") == "Converto rover.ubx (ubx) in RINEX"
    assert engine("rover.ubx: 412 epochs, 3 events") == "rover.ubx: 412 epoche, 3 eventi"
    assert engine("Your base by static PPP") == "La tua base con un PPP statico"
    far = engine("your base BASE is 31.4 km from the station ROAG: beyond 20 km the ambiguities are hard to fix, and a "
                 "solution that is not fixed can be centimetres or decimetres off. A nearer station is better")
    assert far.startswith("la tua base BASE è a 31.4 km dalla stazione ROAG: oltre 20 km")
    refused = engine("DJI_202210140000_001.DAT: not an RTCM 3 log: export RINEX from DJI Terra (4.4 or later) or DJI "
                     "Assistant 2")
    assert refused.startswith("DJI_202210140000_001.DAT: non è un log RTCM 3: esporta il RINEX da DJI Terra")
    edge = engine('the log has no time marks of the falling edge, but 12 of the rising edge: read them with '
                  'edge="rising"')
    assert "fronte discesa" in edge and "12 del fronte salita" in edge
    unsure = engine("pairing by order not confirmed by the times: the times alone cannot pair the photos (the times "
                    "of the photos fit the times of the shots on no clock of the camera within 120 s of a time zone: "
                    "the photos cannot be paired by time); a shift by whole shots cannot be excluded, check the "
                    "positions on a control point")
    assert unsure.startswith("abbinamento per ordine non confermato dai tempi: i tempi da soli non possono abbinare")
    assert "entro 120 s da un fuso orario" in unsure and unsure.endswith("su un punto di controllo")
    assert engine("pairing by number not confirmed by the times: the photos have no date; a shift by whole shots "
                  "cannot be excluded, check the positions on a control point").startswith(
        "abbinamento per numero non confermato dai tempi: le foto non hanno una data; non si può escludere")
    assert engine("2 photos without a date left without a shot: the times cannot check them") == (
        "2 foto senza data restano senza scatto: i tempi non possono controllarle")
    assert engine("the time marks are in GNSS time and the log does not say of which system: read as GPS time, they "
                  "can be off by seconds if that is not GPS time").startswith("le marche di tempo sono in ora GNSS")
    two = engine("Precise products: ESA final\nsomething the catalog does not know")
    assert two == "Prodotti precisi: ESA final\nsomething the catalog does not know"
    for italian in ("Calcolo PPK con RTKLIB", "Posizioni delle foto agli scatti", "3 foto aggiunte", ""):
        assert engine(italian) == italian
    section = i18n._data()["engine"]
    assert len(section) >= 40
    for it, en in section.items():  # the values go into the same places
        assert sorted(set(FIELD.findall(it))) == sorted(set(FIELD.findall(en))), it
        assert en.strip() and it.strip(), it
    assert len(set(section.values())) == len(section)  # one Italian text for an English one
    i18n.set_current("en")
    try:
        assert engine("Contacting igs.bkg.bund.de") == "Contacting igs.bkg.bund.de"
        assert engine("Calcolo PPK con RTKLIB") == i18n.translate("Calcolo PPK con RTKLIB")
    finally:
        i18n.set_current("it")


def test_engine_messages_reach_the_log_and_the_messages(qt_app, monkeypatch):
    """The log pane and the message boxes show a message of the engine in Italian."""
    from pyarchinit_photogrammetry.gui import dialogs, panels

    i18n.set_current("it")
    pane = panels.LogPane()
    pane.info("Your base by static PPP")
    assert "La tua base con un PPP statico" in pane.text()
    pane.deleteLater()
    box = dialogs.ErrorDialog("Calcolo PPK", "conversion of rover.ubx cancelled")
    assert box.summary == "conversione di rover.ubx annullata"
    box.deleteLater()
    qt_app.processEvents()


# ------------------------------------------------------------------ the PPK dialog
def test_ppk_dialog_shots_modes(qt_app, tmp_path):
    """The section Scatti: the source told from the file the dialog is opened with, changed in the combo; the file of
    the shots where the source has one; the summary of the events with the photos they pair with; the lever arm and
    the latency closed, and there only for the sources that take them; the edge for u-blox logs only."""
    doc, folder, _project = _events_chunk(tmp_path)
    mrk = tmp_path / "card" / "Timestamp.MRK"
    mrk.parent.mkdir()
    mrk.write_text("")
    d = _dialog(qt_app, mrk, chunk=doc.chunk)
    assert [d.kind_combo.itemData(i) for i in range(d.kind_combo.count())] == list(sources.KINDS)
    assert [d.kind_combo.itemText(i) for i in range(d.kind_combo.count())] == [
        "MRK di DJI", "Eventi del ricevitore (nel file del rover)", "Eventi di un log u-blox (.ubx)",
        "Log dell'autopilota (ArduPilot, PX4)", "File di eventi (_events.pos)"]
    assert d.kind() == "mrk" and d.shots_file.path() == mrk
    assert not d.b_lever.isVisible() and not d.lever_panel.isVisible()
    assert "shots" not in d.values()  # the answer of an MRK as it always was
    assert d.shots_values() == {"shots": {"kind": "mrk", "file": mrk, "edge": "falling", "message": "auto",
                                          "camera": None, "photos": None},
                                "lever_body_mm": None, "latency_ms": 0.0, "by": "auto"}
    _close(qt_app, d)

    d = _dialog(qt_app, events_test.TM2_OBS, chunk=doc.chunk)  # a RINEX with events: the rover and its shots
    assert d.kind() == "events" and d.rover.path() == events_test.TM2_OBS
    assert not d.shots_file.isEnabled()
    assert _wait_until(lambda: "3 eventi" in d.shots_info.text()), d.shots_info.text()
    assert d.b_lever.isVisible() and not d.b_lever.isChecked() and not d.lever_panel.isVisible()
    d.by.setCurrentIndex(d.by.findData("order"))
    assert _wait_until(lambda: "3 foto" in d.shots_info.text()), d.shots_info.text()
    d.b_lever.click()
    assert d.lever_panel.isVisible() and not d.edge.isVisible()
    d.lever_down.setValue(250)
    d.latency.setValue(12.5)
    v = d.values()
    assert v["shots"]["kind"] == "events" and v["shots"]["file"] is None and v["shots"]["photos"] == folder
    assert v["lever_body_mm"] == (0.0, 0.0, 250.0) and v["latency_ms"] == 12.5 and v["by"] == "order"
    d.set_kind("ubx")
    assert d.shots_file.isEnabled() and d.edge.isVisible()
    assert [d.edge.itemData(i) for i in range(d.edge.count())] == ["falling", "rising"]
    assert [d.edge.itemText(i) for i in range(d.edge.count())] == ["discesa", "salita"]
    d.shots_file.edit.setText(str(events_test.UBX_LOG))
    d.edge.setCurrentIndex(1)
    d.refresh()
    assert d.values()["shots"] == {"kind": "ubx", "file": events_test.UBX_LOG, "edge": "rising", "message": "auto",
                                   "camera": None, "photos": folder}
    d.set_kind("log")
    assert d.message.isVisible() and not d.edge.isVisible()
    d.set_kind("events-pos")
    d.shots_file.edit.setText("")
    d.refresh()
    assert d.problem() == "Scegli il file degli scatti."
    _close(qt_app, d)


@needs_a_converter
def test_ppk_dialog_converts_a_ubx_rover_in_background(qt_app, tmp_path):
    """A raw u-blox log as the rover: converted in a thread while the dialog says so, then its epochs and events, the
    shots of its events; OK waits for the conversion; closing the dialog while it converts does not wait."""
    doc, folder, _project = _events_chunk(tmp_path)
    rover = events_test._ubx_rover(tmp_path / "rover")
    from pyarchinit_photogrammetry.gui.ppk_dialog import PpkDialog

    d = PpkDialog(rover, chunk=doc.chunk)
    assert d.kind() == "events" and d.rover.path() == rover
    assert d.rover_info.text() == f"Conversione di {rover.name} in RINEX in corso..."
    assert d.problem() == f"Il programma sta ancora leggendo {rover.name}: attendi un momento."
    assert _wait_until(lambda: "convertito" in d.rover_info.text(), 60), d.rover_info.text()
    text = d.rover_info.text()
    assert re.search(r"convertito: \d+ epoche, 3 eventi", text), text
    assert _wait_until(lambda: "3 eventi" in d.shots_info.text()), d.shots_info.text()
    assert d.values()["rover"] == rover  # the log itself: the run converts it (kept: at once)
    _close(qt_app, d)

    again = events_test._ubx_rover(tmp_path / "again")
    os.utime(again, (1.0e9, 1.0e9))  # another file for the cache: converted again
    d = _dialog(qt_app, again, chunk=doc.chunk)
    started = time.monotonic()
    _close(qt_app, d)  # while it converts
    assert time.monotonic() - started < 2.0


@needs_a_converter
def test_ppk_dialog_dji_dat_base(qt_app, tmp_path):
    """A D-RTK .DAT as the base: read as RTCM 3 on the day of the rover, with the position of the station its
    messages give; one that is not RTCM 3 refused in Italian."""
    dat = convert_test._dji_dat(tmp_path)
    d = _dialog(qt_app, events_test.RTCM_OBS)
    assert d.kind() == "events"
    d.base.set_paths([dat])
    d.refresh()
    assert d.base_info.text() == f"Conversione di {dat.name} in RINEX in corso..."
    assert _wait_until(lambda: "convertito" in d.base_info.text(), 60), d.base_info.text()
    text = d.base_info.text()
    assert re.search(r"convertito: \d+ epoche, 0 eventi, posizione della stazione dall'RTCM", text), text
    assert "Posizione nell'intestazione" in text
    other = tmp_path / "other" / "DJI_202210140000_002.DAT"
    other.parent.mkdir()
    other.write_bytes(bytes(range(256)) * 64)
    d.base.set_paths([other])
    d.refresh()
    assert _wait_until(lambda: "RTCM 3" in d.base_info.text(), 30), d.base_info.text()
    assert "non è un log RTCM 3: esporta il RINEX da DJI Terra" in d.base_info.text()
    assert "Le osservazioni della base non si possono usare" in (d.problem() or "")
    _close(qt_app, d)


# ------------------------------------------------------------------ the autopilot log
def test_autopilot_dialog_summary_and_pairing(qt_app, tmp_path):
    """The dialog of an autopilot log: what the log holds (its kind, the captures, the heights, the span), the pairing
    proposed with its counts, the camera and the message, the lever arm and the latency in its answer."""
    from pyarchinit_photogrammetry.gui.autopilot_dialog import AutopilotDialog

    log, expected, project, folder = _px4_flight(tmp_path, separation=None, layout="1.7")
    doc = ppg.Document.open(project)
    d = AutopilotDialog(doc.chunk, log)
    d.show()
    assert not d.b_apply.isEnabled() and "Lettura di flight.ulg in corso" in d.summary.text()
    assert _wait_until(lambda: d.b_apply.isEnabled()), d.summary.text() + d.error.text()
    summary = d.summary.text()
    assert "Log PX4 (ULog)" in summary and "12 scatti" in summary and "12 con un tempo valido" in summary
    assert "quote sul livello del mare (AMSL)" in summary
    pairing = d.pairing.text()
    assert "12 foto su 12" in pairing and "per numero" in pairing
    assert "Foto senza scatto: 0" in pairing and "Scatti senza foto: 0" in pairing
    _wait_until(lambda: False, 0.05)  # the dialog grew with its texts: nothing is cut
    view = d.scroll.viewport()  # all of it in sight, or the dialog as tall as the screen lets it and scrolled
    assert (view.height() >= d.scroll.widget().layout().totalHeightForWidth(view.width())
            or d.height() == d.max_height())
    assert "the GPS messages" not in summary  # the warnings of the log in Italian
    assert not d.camera.isEnabled()  # a PX4 log has one camera
    assert [d.message.itemData(i) for i in range(d.message.count())] == ["auto", "capture", "trigger"]
    d.lever_down.setValue(250)
    d.latency.setValue(8)
    assert d.values() == {"path": log, "message": "auto", "camera": None, "by": "auto",
                          "lever_body_mm": (0.0, 0.0, 250.0), "latency_ms": 8.0, "photos": folder}
    d.reject()
    d.deleteLater()

    w, _shots = gnss_dataflash.flight(6, instances=(0, 1))
    two = tmp_path / "two.bin"
    two.write_bytes(w.binary())
    d = AutopilotDialog(doc.chunk, two)
    d.show()
    assert _wait_until(lambda: d.camera.count() == 2), d.summary.text() + d.error.text()
    assert [d.camera.itemData(i) for i in range(2)] == [0, 1] and d.camera.isEnabled()
    assert "Log ArduPilot (DataFlash)" in d.summary.text() and "2 camere" in d.summary.text()
    assert [d.message.itemData(i) for i in range(d.message.count())] == ["auto", "cam", "trig"]
    assert d.values()["camera"] == 0
    d.reject()
    d.deleteLater()

    wrong = tmp_path / "wrong.bin"
    wrong.write_bytes(b"not a log" * 100)
    d = AutopilotDialog(doc.chunk, wrong)
    d.show()
    assert _wait_until(lambda: bool(d.error.text())), d.summary.text()
    assert not d.b_apply.isEnabled()
    d.reject()
    d.deleteLater()
    qt_app.processEvents()


@pytest.fixture
def log_window(tmp_path, qt_app):
    """A window with the saved project of a PX4 flight (AMSL heights) and its log."""
    from pyarchinit_photogrammetry.gui import main_window as mw

    log, expected, project, folder = _px4_flight(tmp_path, separation=None, layout="1.7")
    w = mw.MainWindow(ppg.Document.open(project), autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qt_app.processEvents()
    yield w, log, expected, folder, mw
    w.setWindowModified(False)
    w.close()
    qt_app.processEvents()


def test_gnss_pane_autopilot_button_applies(log_window, monkeypatch):
    """Log dell'autopilota... of the GNSS pane: the log and the dialog asked (replaced), the photos given the
    positions of their shots - AMSL heights marked as such, the camera under the antenna -, the counts in the log, the
    source of the shots in the pane; a log that cannot be read said, nothing changed."""
    w, log, expected, folder, mw = log_window
    pane, chunk = w.gnss_pane, w.chunk
    assert pane.b_autopilot.text() == "Log dell'autopilota..." and pane.b_autopilot.isEnabled()
    from pyarchinit_photogrammetry.gui import gnss_pane

    needs = ("le osservazioni del drone e gli scatti del volo (Timestamp.MRK, eventi del ricevitore, log "
             "dell'autopilota)")
    assert needs in gnss_pane.ppk_state(chunk) and needs.capitalize()[:30] in gnss_pane.ppk_tip()
    asked, said = [], []
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(log), ""))
    monkeypatch.setattr(mw, "message", lambda parent, kind, title, text, *a, **k: said.append((kind, text)))

    def ask(parent, ch, path):
        asked.append((ch, Path(path)))
        return {"path": Path(path), "message": "auto", "camera": None, "by": "auto",
                "lever_body_mm": (0.0, 0.0, 250.0), "latency_ms": 0.0, "photos": folder}

    monkeypatch.setattr(mw.AutopilotDialog, "ask", staticmethod(ask))
    pane.b_autopilot.click()
    assert asked == [(chunk, log)]
    by = {e["seq"]: e for e in expected}
    for cam in chunk.cameras:
        e = by[int(cam.label[-4:])]
        lon, lat, h = cam.reference.location
        assert (lat, lon) == pytest.approx((e["lat"], e["lon"]), abs=1e-6)
        assert h == pytest.approx(e["msl"] - 0.25, abs=1e-3)
        assert cam.meta["gnss"]["height"] == "msl" and cam.meta["gnss"]["source"] == "px4_capture"
    text = w.log_pane.text()
    assert "Log dell'autopilota: 12 foto su 12 hanno la posizione del loro scatto" in text
    assert "abbinate per numero" in text
    assert w.isWindowModified()
    state = pane.states["positions"].text()
    assert "log PX4 (catture)" in state and "flight.ulg" in state
    assert "livello del mare" in pane.states["heights"].text() or "msl" not in pane.states["heights"].text()

    before = [cam.reference.location for cam in chunk.cameras]
    monkeypatch.setattr(mw.AutopilotDialog, "ask", staticmethod(lambda parent, ch, path: None))
    pane.b_autopilot.click()  # cancelled
    wrong = folder / "wrong.bin"
    wrong.write_bytes(b"not a log" * 100)
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(wrong), ""))
    monkeypatch.setattr(mw.AutopilotDialog, "ask", staticmethod(
        lambda parent, ch, path: {"path": Path(path), "message": "auto", "camera": None, "by": "auto",
                                  "lever_body_mm": None, "latency_ms": 0.0, "photos": folder}))
    errors = []
    monkeypatch.setattr(w, "_exc", lambda title, exc: errors.append((title, str(exc))))
    pane.b_autopilot.click()
    assert len(errors) == 1 and errors[0][0] == "Lettura del log dell'autopilota non riuscita"
    assert [cam.reference.location for cam in chunk.cameras] == before


@needs_rtklib
def test_window_ppk_with_rover_events(tmp_path, qt_app, monkeypatch):
    """Calcola PPK... with the events of the rover's receiver: the file of the shots asked, the answer of the dialog
    (replaced), the PPK in the background, the photos the position of their events with the lever arm in the body; the
    pane says where the shots came from."""
    from pyarchinit_photogrammetry.gui import main_window as mw

    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    doc, folder, project = _events_chunk(tmp_path)
    base = events_test._alone(events_test.UBX_OBS, tmp_path / "base")
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.show()
    qt_app.processEvents()
    try:
        monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(events_test.TM2_OBS), ""))
        answer = {"rover": events_test.TM2_OBS, "base": base, "navs": [events_test.UBX_NAV], "base_position": None,
                  "replace_fix": False, "base_mode": "header", "base_frame": None, "base_epoch": None,
                  "station": None, "atx": "auto",
                  "shots": {"kind": "events", "file": None, "edge": "falling", "message": "auto", "camera": None,
                            "photos": folder},
                  "lever_body_mm": (0.0, 0.0, 250.0), "latency_ms": 0.0, "by": "order"}
        monkeypatch.setattr(mw.PpkDialog, "ask", classmethod(lambda cls, parent, path, work=None, chunk=None: answer))
        monkeypatch.setattr(mw, "message", lambda *a, **k: None)
        failed = []
        monkeypatch.setattr(w, "_error", lambda *a, **k: failed.append(a))
        w.gnss_ppk()
        assert _wait_until(lambda: not w.runner.busy, 120)
        w.runner.wait()
        qt_app.processEvents()
        assert not failed, failed
        assert all(cam.meta["gnss"]["shots"]["kind"] == "rinex_events" for cam in w.chunk.cameras)
        text = w.log_pane.text()
        assert "PPK: 3 foto su 3 hanno la posizione corretta del loro scatto" in text
        assert "abbinate per ordine" in text and "parte verticale" in text
        assert "eventi del ricevitore" in w.gnss_pane.states["positions"].text()
    finally:
        w.setWindowModified(False)
        w.close()
        qt_app.processEvents()


@needs_a_converter
def test_window_ppk_from_the_dialog_with_a_raw_base(tmp_path, qt_app, monkeypatch):
    """Calcola PPK... through the dialog itself: the events of a rover in RINEX and a D-RTK .DAT as the base, which
    carries the time of the week only. What the dialog accepts the run takes: the base converted once, with the same
    approximate time (the first epoch of the rover), and the navigation files of that conversion given to RTKLIB
    (stood in for: the sample has too few ephemerides for a solution)."""
    from pyarchinit_photogrammetry import _native
    from pyarchinit_photogrammetry.gnss import convert, rinex
    from pyarchinit_photogrammetry.gui import main_window as mw
    from gnss_helpers import pos_text

    rover = events_test._rover_2012(tmp_path / "rover")
    first = (rinex.time_span(rover)[0] - events_test.GPS_EPOCH).total_seconds()
    midnight = (first // 86400 + 1) * 86400
    project, folder = cli_test._project(tmp_path, {i: midnight + t for i, t in enumerate((10.5, 20.25, 30.0), 1)}, 16)
    doc = ppg.Document.open(project)
    dat = convert_test._dji_dat(tmp_path)
    seen, given = [], []
    to_rinex = convert.to_rinex

    def spy(path, *args, **kwargs):
        seen.append((Path(path).name, kwargs.get("approx_time")))
        return to_rinex(path, *args, **kwargs)

    def rtklib(conf, inputs, out):
        given.append([Path(path) for path in inputs if Path(path).is_file()])
        rows = [(first + t, 35.7, 139.5, 80.0, 1, 8, 0.01, 0.01, 0.02) for t in range(0, 64)]
        Path(out).write_text(pos_text(rows), newline="")
        return 0, ""

    def drive(dialog):  # the user: names the base, waits for what the dialog says of it, confirms
        dialog.show()
        dialog.base.set_paths([dat])
        dialog.by.setCurrentIndex(dialog.by.findData("order"))  # three photos: too few for their times to pair them
        dialog.refresh()
        assert _wait_until(lambda: "convertito" in dialog.base_info.text(), 60), dialog.base_info.text()
        assert _wait_until(lambda: dialog.problem() is None, 30), dialog.problem()
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(convert, "to_rinex", spy)
    monkeypatch.setattr(_native, "ppk_solve", rtklib)
    monkeypatch.setattr(_native, "ppk_available", lambda: True)
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.show()
    qt_app.processEvents()
    try:
        monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(rover), ""))
        monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))
        monkeypatch.setattr(mw.PpkDialog, "exec", drive)
        monkeypatch.setattr(mw, "message", lambda *a, **k: None)
        failed = []
        monkeypatch.setattr(w, "_error", lambda *a, **k: failed.append(a))
        w.gnss_ppk()
        assert _wait_until(lambda: not w.runner.busy, 120)
        w.runner.wait()
        qt_app.processEvents()
        assert not failed, failed
        times = [when for name, when in seen if name == dat.name]
        assert times and all(when == pytest.approx(first) for when in times), seen
        (inputs,) = given
        assert len(inputs) >= 3, inputs  # the rover, the base and the navigation of the conversion, all there
        assert "PPK: 3 foto su 3" in w.log_pane.text(), w.log_pane.text()
    finally:
        w.setWindowModified(False)
        w.close()
        qt_app.processEvents()


def test_window_ppk_refuses_what_the_source_does_not_take(tmp_path, qt_app, monkeypatch):
    """An answer with a lever arm for an MRK (an older caller, a script) is refused before anything runs."""
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, folder, project = _events_chunk(tmp_path)
    assert mw.MainWindow._ppk_shots_problem({"shots": {"kind": "mrk"}, "lever_body_mm": (0, 0, 250)}) is not None
    assert mw.MainWindow._ppk_shots_problem({"shots": {"kind": "mrk"}, "latency_ms": 0.0, "by": "auto"}) is None
    assert mw.MainWindow._ppk_shots_problem({"shots": {"kind": "events", "edge": "rising"}}) is not None
    assert mw.MainWindow._ppk_shots_problem({"shots": {"kind": "ubx", "edge": "rising"}}) is None
    assert mw.MainWindow._ppk_shots_problem({"shots": {"kind": "events", "message": "cam"}}) is not None
    assert mw.MainWindow._ppk_shots_problem({}) is None
