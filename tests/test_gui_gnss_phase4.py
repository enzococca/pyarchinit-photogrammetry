"""The interface of the shots of self-built drones: the sources of the shots, raw logs and the lever arm in the PPK
dialog, the dialog of the autopilot log, its button in the GNSS pane, the messages of the engine in Italian."""
from __future__ import annotations

import os
import re
import shutil
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
import gnss_helpers  # noqa: E402
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


REFUSALS = (  # as the engine writes them, with the values of a real case
    "flight.MRK: the times of the photos and of the shots do not agree: by the clock of the camera the photos were "
    "taken +0:17:42 from the UTC of their shots, which is no time zone. These are not the photos of this MRK, or the "
    "clock of the camera was wrong",
    "rover.ubx: the times of the photos and of the shots do not agree: by the clock of the camera the photos were "
    "taken -3:12:05 from the UTC of their shots, which is no time zone. These are not the photos of this source, or "
    "the clock of the camera was wrong",
    "flight.MRK: the times of the photos fit the shots of more than one flight (at +2:00:00, +1:00:00 from UTC): keep "
    "the photos of each flight in a folder of their own",
    "flight.MRK: the times of the photos fit more than one way of pairing them with the shots, on the clocks of the "
    "camera +2:00:00 (57 photos), +2:00:02 (56 photos) from UTC: the photos cannot be paired by time, and paired by "
    "number or by order they are not checked by the times either",
    "flight.MRK: the times of the photos fit the shots on more than one clock of the camera, +1:00:00, +2:00:00 from "
    "UTC, as well as each other: the photos cannot be paired by time, and paired by number or by order they are not "
    "checked by the times either",
    "flight.MRK: paired by number, 3 photos disagree with the times, which pair them with other shots: the first, "
    "DJI_0004.JPG, has shot 4 by number and shot 5 by time. Pair them by time",
    "flight.MRK: paired by order, 1 photo disagree with the times, which pair it with other shots: the first, "
    "DJI_0004.JPG, has shot 4 by order and shot 5 by time. Pair them by time",
    "flight.MRK: paired by order, the photos were taken +0:17:42 (most of them) from the UTC of their shots, +0:17:42 "
    "from the nearest time zone: the times disagree with this pairing",
    "flight.MRK: paired by number, 12 of 40 photos disagree with the times of their shots by more than 2 s (the "
    "first, DJI_0004.JPG, by +0:00:07): the number pairs photos with the shots of others. Pair them by time",
    "flight.MRK: only 12 of 40 photos were taken at the time of their shot (clock of the camera +2:00:00 from UTC): "
    "the MRK is of another flight, or the folder holds the photos of other flights too",
    "pairing by order not confirmed by the times: the times alone cannot pair the photos (the times of the photos "
    "fit more than one way of pairing them with the shots, on the clocks of the camera +2:00:00 (57 photos), "
    "+2:00:02 (56 photos) from UTC: the photos cannot be paired by time, and paired by number or by order they are "
    "not checked by the times either); a shift by whole shots cannot be excluded, check the positions on a control "
    "point",
    "the shots of kind mrk need their file",
    "the counter of TIM-TM2 started again 1 time (41 -> 0): the receiver restarted - the marks keep their order, and "
    "pulses lost while it restarted are not counted",
    "the counter of TIM-TM2 started again 4 times (41 -> 0, 12 -> 0 (channel 1), 7 -> 1, ...): the receiver "
    "restarted - the marks keep their order, and pulses lost while it restarted are not counted",
    "events.pos: its times are in GLONASST, which is not GPS time (GPST), UTC or JST",
    "3 time marks RTKLIB could not place (no solution at the epochs around it): not valid, without a position",
    "1 time mark RTKLIB could not place (no solution at the epochs around it): not valid, without a position",
    "5 time marks without a valid time: the receiver had no valid GNSS fix when they were taken (3 before its first "
    "fix, 2 after losing it in flight, e.g. signals blocked). Kept, marked as not valid: check those photos",
    "1 time mark without a valid time: the receiver had no valid GNSS fix when it was taken (1 before its first "
    "fix). Kept, marked as not valid: check that photo",
    "2 time marks without a valid time: the receiver had no valid GNSS fix when they were taken (2 after losing it "
    "in flight, e.g. signals blocked). Kept, marked as not valid: check those photos",
    "base.rtcm3: the observations and the GPS ephemerides of the log are in different weeks: the approximate time of "
    "the log is in the wrong week - give the day of the flight",
    "base.24o: a RINEX file with no observation epochs",
    "notes.txt: not a raw receiver log (u-blox, RTCM 3, Septentrio SBF, Unicore, Trimble RT-17) nor a RINEX file - a "
    'NovAtel log is converted with kind="novatel"',
    "base.rtcm3: an rtcm3 log carries the time of the week only: an approximate time of the log (the day of the "
    "flight) is needed",
    "base.rtcm3: its MSM say they end their epoch (multiple message bit 0) before others of the same time: RTKLIB "
    "writes one RINEX epoch for each, the same time repeated with some of the systems each (the PPK reads them as one "
    "epoch)",
    "base.rtcm3: the BeiDou MSM of the log carry their epoch in GPS time, not in BeiDou time as RTCM 3 says (a "
    "receiver of before the standard): RTKLIB would put the BeiDou observations 14 s late and lose the others; "
    "converted from a copy with the BeiDou epochs moved 14 s back",
    "base.rtcm3: the observations of the log are MSM1-3 only (messages 1071, 1081), which RTKLIB does not decode "
    "(they lack the full pseudoranges): set the receiver to send MSM4 or MSM7 (or 1004/1012)",
    "base.ubx: not a ubx log, or damaged",
    "base.ubx: conversion failed (-3): no memory",
    "base.rtcm3: the conversion has no epochs",
    "base.rtcm3: the conversion has no epochs: is the approximate time of the log in the right week?",
    "base.rtcm3: the epochs are more than 3 days from the approximate time of the log: is it the log of that day, in "
    "the right week?",
    "old.bin: the log has no camera messages (CAM or TRIG), and is too old for the times of shots anyway: its formats "
    "of CAM, GPS have no TimeUS (an ArduPilot of about 2015 or older)",
    "flight.bin: the CAM messages have no GPSWeek, GPSTime: not a format this reader knows",
    "flight.bin: the log has 2 cameras (CAM instances 0, 1): choose one",
    "flight.bin: no camera 3 in the log (cameras 0, 1)",
    "flight.ulg: the log has no camera_capture messages: no camera was set up to log its shots",
    "flight.ulg: the log has no camera_trigger messages (the log's info lists them among the topics it left out): no "
    "camera was set up to log its shots",
    "/Users/Shared/flights/flight.bin: no such file",
    "flight.bin: the file is empty",
    "flight.bin: the log is closed",
    "notes.txt: not an ArduPilot DataFlash log (binary or text)",
    "flight.bin: not an ArduPilot DataFlash log - no message formats in it",
    "notes.txt is not a ULog file (no ULog header)",
    "flight.ulg: ULog version 2, this reader knows version 1",
    "flight.ulg: incompatible ULog flags this reader does not know",
    "flight.ulg: the data of topic 'camera_capture' cannot be read: format 'camera_capture' unknown or nested too "
    "deep",
    "flight.ulg: the data of topic 'camera_capture' cannot be read: format 'esc_report' has no fields",
    "flight.ulg: the data of topic 'camera_capture' cannot be read: format 'camera_capture' larger than a data "
    "message (65535 bytes)",
    "flight.ulg: the data of topic 'camera_capture' cannot be read: field 'uint8_t[' of format 'camera_capture'",
    "flight.ulg: the data of topic 'camera_capture' cannot be read: field 'seq' twice in format 'camera_capture'",
    "flight.ulg: 120 damaged bytes skipped (to the next sync message)",
    "flight.ulg: an offset of appended data before the data it follows or past the end of the file: no appended data "
    "read from it on",
    "flight.ulg: 2 formats that cannot be read (esc_report, mission): their topics not read",
    "flight.ulg: 1 format that cannot be read (esc_report): their topics not read",
    "flight.ulg: 3 data messages shorter than their format (more than its trailing padding missing): damaged, left "
    "out",
    "flight.ulg: 1 data message shorter than their format (more than its trailing padding missing): damaged, left out",
    "flight.ulg: 4 dropouts of the log (310 ms in all): data lost while the log could not write",
    "flight.ulg: 1 dropout of the log (25 ms in all): data lost while the log could not write",
)
ENGLISH = re.compile(r"\b(the|of|is|are|not|and|or|by|with|from|to|its|their|them|it|one|this|an|on|as|was|were|"
                     r"photos?|shots?|times?)\b")


#: what the stations and the downloads say, as the window of the GNSS shows it: built as gnss/stations.py and
#: gnss/download.py build them
STATION_NOTES = (
    "AQUI00ITA: no observation files can be had for 2026-05-04 09:10 to 09:42 (GPS time).\n"
    "gnss.bkg.bund.de could not be reached: https://igs.bkg.bund.de/root_ftp/EUREF/obs/2026/124/: could not connect "
    "(timed out) (after 3 attempts)\n"
    "1 s files:\n"
    "  BKG EPN, 15 minute 1 s files: AQUI00ITA_[RSU]_20261240900_15M_01S_MO.crx.gz is not in "
    "https://igs.bkg.bund.de/root_ftp/EUREF/highrate/2026/124/j/ - not published yet: BKG 15 minute files are online "
    "after each quarter of an hour\n"
    "  RING, hourly 1 s files: AQUI00ITA_[RSU]_20261240900_01H_01S_MO.crx.gz is not in "
    "https://webring.gm.ingv.it:44324/rinex/RING/2026/124/\n"
    "  and 3 more\n"
    "The 30 s files for these hours are online: ask for the rate \"30s\" (or \"auto\").",
    "AQUI00ITA: no observation files can be had for 2026-05-04 23:50 to 2026-05-05 00:20 (GPS time).\n"
    "30 s files:\n"
    "  BKG IGS, daily 30 s files: AQUI00ITA_[RSU]_20261240000_01D_30S_MO.crx.gz is not in "
    "https://igs.bkg.bund.de/root_ftp/IGS/obs/2026/124/\n"
    "The 1 s files for these hours are not there either.",
    "AQUI00ITA: the observations have no epoch from 2026-05-04 09:20:00 to 09:31:30 (GPS time) during the flight, "
    "2026-05-04 09:10:00 to 09:42:00 (and 2 more): a hole of more than 300 s is not accepted\n"
    "The 30 s files, tried next, do not do either: AQUI00ITA: the observations run from 2026-05-04 09:15:00 to "
    "2026-05-04 09:30:00 (GPS time) and do not cover the flight, 2026-05-04 09:10:00 to 09:42:00",
    "AQUI00ITA: the observations have no epoch (GPS time) and do not cover the flight, 2026-05-04 09:10:00 to 09:42:00",
    "AQUI00ITA: the base has 3 holes during the flight, the longest, from 2026-05-04 09:20:00 to 09:20:40 (GPS time), "
    "40 s: accepted, RTKLIB takes base data up to 300 s old",
    "AQUI00ITA: the base has a hole during the flight, from 2026-05-04 09:20:00 to 09:20:40 (GPS time), 40 s: "
    "accepted, RTKLIB takes base data up to 300 s old",
    "AQUI00ITA: the coordinates from RING were not used: https://webring.gm.ingv.it:44324/x.pos: could not connect "
    "(timed out) (RING is on port 44324; a firewall may block it)",
    "AQUI00ITA: only the approximate position of the list is known, good to metres",
    "AQUI00ITA: no mixed file (_MO) for 2026-05-04 09:00 in https://igs.bkg.bund.de/root_ftp/EUREF/obs/2026/124/: "
    "AQUI00ITA_R_20261240900_01H_30S_GO.crx.gz is used, which holds the observations of GPS alone - the satellites of "
    "the other constellations of the drone are left out of the solution",
    "https://example.org/list.json: the server answered HTTP 503; trying again in 2 s (attempt 2 of 3)",
    "http://example.org/list.json: not HTTPS - only https and file addresses are downloaded, what comes over plain "
    "http can be changed on the way",
    "https://example.org/a.gz: the connection closed after only 1200 of 88000 bytes (after 3 attempts)",
    "https://example.org/list.json: not the list of stations that was expected (an empty list, without a single "
    "station); using the list of stations saved on 2026-05-01",
    "AQUI00ITA_R_20261240900_01H_30S_MO.crx.gz (https://example.org/a.gz) is no good, not even when downloaded "
    "again: AQUI00ITA_R_20261240900_01H_30S_MO.crx.gz is cut short or damaged (not gzip)",
    "No broadcast navigation file for 2026-05-04: BRDC00WRD_R_20261240000_01D_MN.rnx.gz is not in "
    "https://igs.bkg.bund.de/root_ftp/IGS/BRDC/2026/124/ - not published yet: BKG daily files are online about 30 "
    "minutes after midnight UTC",
    "AQUI00ITA is in none of the networks (EPN, IGS, RING): there is no archive that this program reads to ask for "
    "its observations",
    "Joining the 4 files of AQUI00ITA",
    "Contacting igs.bkg.bund.de (attempt 2 of 3)",
)


def test_station_and_download_notes_in_italian():
    """What the stations and the downloads say is shown in Italian in the window of the GNSS, each line of a message
    of several lines too; the names, the addresses and the times stay as they are."""
    i18n.set_current("it")
    for message in STATION_NOTES:
        shown = i18n.engine(message)
        assert shown.count("\n") == message.count("\n"), shown
        words = ENGLISH.findall(re.sub(r"https?://\S+|\S+\.gz", "", shown))
        assert not words, (words, shown)
    shown = i18n.engine(STATION_NOTES[0]).split("\n")
    assert shown[0] == ("AQUI00ITA: non si possono avere file di osservazioni dalle 2026-05-04 09:10 alle 09:42 "
                        "(tempo GPS).")
    assert shown[2] == "File a 1 s:" and shown[5] == "  e altri 3"
    assert shown[3].startswith("  BKG EPN, file di 15 minuti a 1 s: AQUI00ITA_[RSU]_20261240900_15M_01S_MO.crx.gz ")
    assert "44324" in i18n.engine(STATION_NOTES[6]) and "firewall" in i18n.engine(STATION_NOTES[6])
    i18n.set_current("en")
    try:
        assert all(i18n.engine(message) == message for message in STATION_NOTES)
    finally:
        i18n.set_current("it")


def _converter_notes() -> tuple:
    """What the conversion of a raw log says when it cannot be done, or warns of, as the engine writes it."""
    from pyarchinit_photogrammetry import _native

    return (
        _native.CONVBIN_MISSING,
        "PPG_CONVBIN names /opt/rtklib/convbin, which is not there. " + _native.CONVBIN_MISSING,
        "PPG_CONVBIN names /opt/rtklib/convbin, which is not a program that can run. " + _native.CONVBIN_MISSING,
        "rover.rtcm3: rtcm3 carries the time of the week only, an approximate time of the log (its day) is needed",
        "convbin failed on rover.ubx (exit status 3)",
        "convbin failed on rover.ubx (exit status 3): 2026/05/04 09:10:00-05/04 09:42:00: O=0",
        "no observations decoded from rover.ubx: not a log of the ubx format, or damaged (convbin: O=0 N=0)",
        "rover.rtcm3: no GPS ephemerides (RTCM 1019) in the log: the week of its observations comes from the "
        "approximate time alone and is not checked",
        "rover.rtcm3: the GPS ephemerides of the log are all at the turn of a week: the week of its observations "
        "comes from the approximate time alone and is not checked",
        "rover.obs: system G has 6 signals, as many as the native core keeps of a satellite (NFREQ=3): others in "
        "the log may have been left out",
    )


def test_converter_notes_in_italian():
    """Why a raw log cannot be converted - no converter among them - and what its conversion warns of are shown in
    Italian; the names of the programs, of the packages and of the files stay as they are; in English as written."""
    notes = _converter_notes()
    i18n.set_current("it")
    names = re.compile(r"pip install -U \S+|PPG_CONVBIN|/opt/\S+|convbin: [^)]*|\(exit status|: 2026.*$")
    for message in notes:
        shown = i18n.engine(message)
        words = ENGLISH.findall(names.sub("", shown))
        assert not words, (words, shown)
    assert i18n.engine(notes[0]).startswith("I log grezzi dei ricevitori (u-blox, RTCM 3, NovAtel, ...) sono convertiti "
                                            "in RINEX dal core nativo pyarchinit-photogrammetry-core 0.8 o successivo")
    for name in ('pip install -U "pyarchinit-photogrammetry-core>=0.8"', "pyarchinit-photogrammetry[fast]",
                 "RTKLIB-EX 2.5 (rtklibexplorer)", "PPG_CONVBIN"):
        assert name in i18n.engine(notes[0]), name
    assert i18n.engine(notes[1]).startswith("PPG_CONVBIN indica /opt/rtklib/convbin, che non c'è. I log grezzi")
    i18n.set_current("en")
    try:
        assert all(i18n.engine(message) == message for message in notes)
    finally:
        i18n.set_current("it")


def test_ppk_dialog_says_in_italian_that_no_converter_is_there(qt_app, tmp_path, monkeypatch):
    """A raw log as the rover and no converter (a native core older than 0.8, no convbin): the reason is written in
    Italian under the field and in what stops OK."""
    from pyarchinit_photogrammetry import _native
    from pyarchinit_photogrammetry.gnss import convert

    def no_converter(path, **_options):
        raise RuntimeError(_native.CONVBIN_MISSING)

    monkeypatch.setattr(convert, "to_rinex", no_converter)
    rover = events_test._ubx_rover(tmp_path / "rover")
    d = _dialog(qt_app, rover)
    assert d.kind() == "events" and d.rover.path() == rover
    assert _wait_until(lambda: "in corso" not in d.rover_info.text(), 30), d.rover_info.text()
    assert d.rover_info.text().startswith("I log grezzi dei ricevitori (u-blox, RTCM 3, NovAtel, ...) sono convertiti")
    assert d.problem().startswith("Le osservazioni del drone non si possono usare: I log grezzi dei ricevitori")
    assert "Raw receiver logs" not in d.rover_info.text() + d.problem()
    _close(qt_app, d)


def test_the_clock_of_the_camera_is_written_as_hours_minutes_seconds(qt_app):
    """The offset of the clock of the camera from UTC: hours:minutes:seconds from a minute on (as the messages on the
    time zones write it), with the tenths when there are some; seconds under a minute. The same in the PPK and
    autopilot windows, in the log of the window and in the commands."""
    from types import SimpleNamespace

    from pyarchinit_photogrammetry import cli
    from pyarchinit_photogrammetry.gnss import shots as gshots
    from pyarchinit_photogrammetry.gui import shots_widgets as sw

    for seconds, said in ((32399.5, "+8:59:59.5"), (7199.5, "+1:59:59.5"), (7200.0, "+2:00:00"), (-3600.04, "-1:00:00"),
                          (-12600.26, "-3:30:00.3"), (60.0, "+0:01:00"), (59.94, "+59.9 s"), (-0.5, "-0.5 s"),
                          (0.0, "+0.0 s"), (-0.04, "+0.0 s"), (59.96, "+0:01:00"), (86400.0 + 3661.2, "+25:01:01.2")):
        assert gshots.clock_text(seconds) == said, seconds
    i18n.set_current("it")
    (line,) = sw.pairing_words({"by": "time", "clock_s": 32399.5})
    assert line.endswith(", l'orologio della camera a +8:59:59,5 dall'UTC"), line
    (line,) = sw.pairing_words({"by": "time", "clock_s": -0.5, "confirmed": False})
    assert ", l'orologio della camera a -0,5 s dall'UTC - i tempi non lo confermano" in line, line
    pairing = SimpleNamespace(kept=[1, 2], files=[1, 2], by="time", clock_s=7199.5, photos_without_shot=[],
                              extra_shots=[], invalid=[], doubles=0, confirmed=True, warnings=[])
    assert " L'orologio della camera è a +1:59:59,5 dall'UTC. " in sw.pairing_text(pairing)
    pairing.clock_s = 0.5
    assert " L'orologio della camera è a +0,5 s dall'UTC. " in sw.pairing_text(pairing)
    assert cli._pairing_words({"by": "time", "clock_s": 32399.5}) == [
        "paired by time, the clock of the camera +8:59:59.5 from UTC"]
    assert cli._pairing_words({"by": "time", "clock_s": 0.5}) == [
        "paired by time, the clock of the camera +0.5 s from UTC"]
    i18n.set_current("en")
    try:
        (line,) = sw.pairing_words({"by": "time", "clock_s": 32399.5})
        assert line.endswith(", the clock of the camera +8:59:59.5 from UTC"), line
    finally:
        i18n.set_current("it")


def test_engine_refusals_in_italian():
    """What the engine refuses, and what it warns of, reaches the dialogs in Italian whole: the name of the file and
    the values kept, a reason inside another message and a list of values in Italian too; nothing of the English is
    left."""
    i18n.set_current("it")
    left = {}
    for message in REFUSALS:
        shown = i18n.engine(message)
        words = ENGLISH.findall(shown.replace("no memory", ""))  # what RTKLIB says stays as it says it
        if shown == message or words:
            left[message[:70]] = sorted(set(words))
    assert not left, left
    shown = i18n.engine(REFUSALS[0])
    assert shown.startswith("flight.MRK: ") and "+0:17:42" in shown and "MRK" in shown[12:]
    assert "DJI_0004.JPG" in i18n.engine(REFUSALS[5]) and "per numero" in i18n.engine(REFUSALS[5])
    assert "57 foto" in i18n.engine(REFUSALS[3]) and "57 foto" in i18n.engine(REFUSALS[10])
    assert i18n.engine(REFUSALS[10]).startswith("abbinamento per ordine non confermato dai tempi: ")
    assert "12 -> 0 (canale 1), 7 -> 1, ...)" in i18n.engine(REFUSALS[13])
    assert i18n.engine("flight.MRK: something the catalog does not know") == (
        "flight.MRK: something the catalog does not know")


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


def test_ppk_dialog_folder_as_shots_and_another_mrk(qt_app, tmp_path):
    """A folder named where the file of the shots goes is said in Italian with its short name, whatever the source
    (never the error of the system with the whole path); and another MRK typed brings the observations of the drone
    beside it, unless the user named some."""
    first, second = tmp_path / "card one" / "flight1", tmp_path / "card two" / "flight2"
    for folder, flight in ((first, "DJI_0001"), (second, "DJI_0002")):
        folder.mkdir(parents=True)
        (folder / f"{flight}_Timestamp.MRK").write_text(gnss_helpers.mrk_line(1, 1_300_000_000.0, (0, 0, 190)))
        (folder / f"{flight}_Rinex.obs").write_text("")
    d = _dialog(qt_app, first / "DJI_0001_Timestamp.MRK")
    assert d.rover.path() == first / "DJI_0001_Rinex.obs"
    for kind in sources.KINDS:
        if kind == "events":
            continue
        d.set_kind(kind)
        d.shots_file.edit.setText(str(second))
        d.refresh()
        said = lambda: d.shots_info.text()  # noqa: E731
        assert _wait_until(lambda: "flight2 è una cartella, non un file" in said()), (kind, said())
        assert said().startswith("Il file non si legge: ") and str(tmp_path) not in said() and "Errno" not in said()
    d.set_kind("mrk")
    d.shots_file.edit.setText(str(second / "DJI_0002_Timestamp.MRK"))
    d.refresh()
    assert d.rover.path() == second / "DJI_0002_Rinex.obs"  # the drone of the MRK typed
    mine = tmp_path / "mine.obs"
    mine.write_text("")
    d.rover.edit.setText(str(mine))
    d.shots_file.edit.setText(str(first / "DJI_0001_Timestamp.MRK"))
    d.refresh()
    assert d.rover.path() == mine  # the one the user named stays
    d.rover.edit.setText("")
    d.refresh()
    assert d.rover.path() == first / "DJI_0001_Rinex.obs"  # an empty field is filled again
    _close(qt_app, d)


def test_a_folder_refused_as_on_windows_is_said_a_folder(qt_app, tmp_path):
    """Windows refuses a folder opened as a file with a PermissionError, not an IsADirectoryError: it is said a
    folder all the same; a file that cannot be read stays a matter of permission."""
    import errno

    from pyarchinit_photogrammetry.gui import shots_widgets as sw

    folder, file = tmp_path / "flight2", tmp_path / "locked.MRK"
    folder.mkdir()
    file.write_text("")
    assert sw.unread(PermissionError(errno.EACCES, "Permission denied", str(folder))) == (
        "flight2 è una cartella, non un file")
    assert sw.unread(PermissionError(errno.EACCES, "Permission denied", str(file))) == (
        "locked.MRK non si può aprire: manca il permesso di leggerlo")


def test_summaries_start_their_sentences_with_a_capital(qt_app, tmp_path):
    """What the engine notes is written in the summaries of the dialogs as a sentence, its first letter a capital;
    a note that starts with the name of a file, a number or a word of the log is left as it is. The buttons of the
    PPK dialog are in Italian as those of the others."""
    from PySide6.QtWidgets import QDialogButtonBox

    from pyarchinit_photogrammetry.gui import shots_widgets as sw

    assert sw.said("pairing by number not confirmed by the times: the photos have no date; a shift by whole shots "
                   "cannot be excluded, check the positions on a control point").startswith(
        "Abbinamento per numero non confermato dai tempi: le foto non hanno una data; ")
    assert sw.said("the log has a gimbal: the attitude of a capture is the gimbal's when it reported one, not the "
                   "vehicle's - a lever arm in the body of the drone turned with it is wrong").startswith("Il log ha ")
    assert sw.said("the last message of the log is cut (the logger stopped while writing it): left out").startswith(
        "L'ultimo messaggio del log")
    for kept in ("2 photos without a date left without a shot: the times cannot check them",
                 "flight.ulg: the log is closed", "volo: these shots carry no numbers: they cannot be paired by number",
                 "camera_trigger 12", "TRIG 12", ""):
        assert sw.said(kept) == i18n.engine(kept), kept
    doc, _folder, _project = _events_chunk(tmp_path)
    d = _dialog(qt_app, events_test.TM2_OBS, chunk=doc.chunk)
    d.by.setCurrentIndex(d.by.findData("order"))
    d.refresh()
    assert _wait_until(lambda: "non confermato dai tempi" in d.shots_info.text()), d.shots_info.text()
    lines = d.shots_info.text().split("\n")
    assert any(line.startswith("Abbinamento per ordine non confermato dai tempi") for line in lines), lines
    assert not [line for line in lines if line[:1].islower()], lines
    box = d.findChild(QDialogButtonBox)
    assert box.button(QDialogButtonBox.StandardButton.Ok).text() == "Avvia"
    assert box.button(QDialogButtonBox.StandardButton.Cancel).text() == "Annulla"
    _close(qt_app, d)


def _readers() -> list:
    import threading

    return [t for t in threading.enumerate() if t.name == "ppg-shots-reading"]


def test_aside_reads_once_and_starts_nothing_once_stopped(qt_app):
    """A reading runs once in its thread and tells the interface when it ends; once the dialog is closed nothing is
    started, and what a reading still running gives then is dropped - also when its dialog is gone."""
    import threading

    from PySide6.QtCore import QCoreApplication, QEvent

    from pyarchinit_photogrammetry.gui import shots_widgets as sw

    assert _wait_until(lambda: not _readers())
    aside, told, calls = sw.Aside(), [], []
    aside.done.connect(lambda: told.append(1))
    assert aside.get("a", lambda: calls.append("a") or 7) is sw.RUNNING
    assert _wait_until(lambda: aside.get("a", lambda: calls.append("again")) == 7)
    assert calls == ["a"] and told == [1]
    failed = aside.get("b", lambda: 1 / 0)
    assert failed is sw.RUNNING and _wait_until(lambda: isinstance(aside.get("b", None), ZeroDivisionError))
    gate, problems = threading.Event(), []
    hook, threading.excepthook = threading.excepthook, lambda args: problems.append(args.exc_value)
    try:
        assert aside.get("slow", lambda: gate.wait(10) and calls.append("slow")) is sw.RUNNING
        slow = _readers()
        aside.stop()
        assert aside.get("late", lambda: calls.append("late")) is sw.RUNNING  # asked after the end: never started
        assert _readers() == slow and "late" not in calls
        assert aside.get("a", None) == 7  # what was read stays
        told.clear()
        aside.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        gate.set()
        assert _wait_until(lambda: not _readers())
        qt_app.processEvents()
    finally:
        gate.set()
        threading.excepthook = hook
    assert not problems and not told and "late" not in calls


def test_ppk_dialog_asked_is_deleted_when_done(qt_app, tmp_path, monkeypatch):
    """The dialog asked for its answer is deleted when it is done, as the one of the autopilot log, with its readings
    stopped."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QDialog

    from pyarchinit_photogrammetry.gui.ppk_dialog import PpkDialog

    doc, _folder, _project = _events_chunk(tmp_path)
    seen = []

    def driver(dialog):
        seen.append(dialog)
        dialog.destroyed.connect(lambda: seen.append("deleted"))
        dialog.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(PpkDialog, "exec", driver)
    assert PpkDialog.ask(None, events_test.TM2_OBS, chunk=doc.chunk) is None
    assert seen[0]._aside._stopped
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert seen[-1] == "deleted"
    assert _wait_until(lambda: not _readers())


def test_ppk_dialog_lever_counts_with_its_panel_closed(qt_app, tmp_path):
    """A lever arm and a latency typed and their panel closed again: they count as they are, and the dialog says so
    on the button and in the summary of the shots - never silently dropped."""
    doc, folder, _project = _events_chunk(tmp_path)
    d = _dialog(qt_app, events_test.TM2_OBS, chunk=doc.chunk)
    assert d.b_lever.text() == "Braccio di leva e latenza..."
    d.b_lever.click()
    d.lever_down.setValue(250)
    d.latency.setValue(40)
    d.b_lever.click()
    qt_app.processEvents()
    assert not d.lever_panel.isVisible() and not d.b_lever.isChecked()
    v = d.values()
    assert v["lever_body_mm"] == (0.0, 0.0, 250.0) and v["latency_ms"] == 40.0
    assert d._latency_s() == pytest.approx(0.040)
    assert d.b_lever.text() == "Braccio di leva e latenza: 0, 0, 250 mm · 40 ms"
    assert _wait_until(lambda: "3 eventi" in d.shots_info.text()), d.shots_info.text()
    assert ("Braccio di leva dall'antenna alla camera (avanti, destra, giù): 0, 0, 250 mm; latenza della camera: "
            "40 ms.") in d.shots_info.text()
    d.b_lever.click()  # open: the values are in sight, the button as it was
    assert d.b_lever.text() == "Braccio di leva e latenza..."
    d.lever_down.setValue(0)
    d.latency.setValue(0)
    d.b_lever.click()
    qt_app.processEvents()
    assert d.b_lever.text() == "Braccio di leva e latenza..." and "Braccio di leva" not in d.shots_info.text()
    assert d.values()["lever_body_mm"] is None and d.values()["latency_ms"] == 0.0
    d.latency.setValue(12.5)
    assert _wait_until(lambda: d.b_lever.text() == "Braccio di leva e latenza: 0, 0, 0 mm · 12,5 ms"), d.b_lever.text()
    d.set_kind("mrk")  # a DJI MRK takes none of them
    assert d.shots_values()["lever_body_mm"] is None and d.shots_values()["latency_ms"] == 0.0
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
def test_ppk_dialog_asks_the_day_of_an_rtcm3_rover_with_its_own_events(qt_app, tmp_path):
    """An RTCM 3 log as the rover, the shots its own events: no file of shots tells its day, so the dialog asks the
    day of the flight, says so while it is missing or is not a day, converts the log with it and answers it for the
    run, which finds that conversion; a rover in RINEX is asked nothing."""
    from datetime import datetime

    from pyarchinit_photogrammetry.gnss import sources

    rover = tmp_path / "rover.rtcm3"
    shutil.copyfile(PHASE4 / "GMSD7_20121014_64k.rtcm3", rover)
    d = _dialog(qt_app, rover)
    assert d.kind() == "events" and d.rover.path() == rover and d.day_row.isVisibleTo(d)
    assert d.rover_info.text() == "Indica il giorno del volo: un log RTCM 3 porta solo il tempo della settimana."
    assert "Indica il giorno del volo" in d.problem() and "approx_time" not in d.values()
    d.day.setText("14 ottobre")
    d.refresh()
    assert d.rover_info.text().startswith("Il giorno del volo si scrive AAAA-MM-GG") and "14 ottobre" in d.problem()
    d.day.setText("14/10/2012")  # as it is written in Italy; 2012-10-14 is the same day
    d.refresh()
    assert _wait_until(lambda: "convertito" in d.rover_info.text(), 60), d.rover_info.text()
    assert re.search(r"convertito: 64 epoche, 0 eventi", d.rover_info.text()), d.rover_info.text()
    assert d.values()["approx_time"] == datetime(2012, 10, 14) and d.values()["rover"] == rover
    done = d._conversion(rover)
    inputs = sources.ppk_inputs("events", None, rover, approx_time=d.values()["approx_time"])
    assert inputs.rover == done.obs  # the conversion of the dialog, not another one
    d.day.setText("2012-10-14 09:30")
    d.refresh()
    assert d.values()["approx_time"] == datetime(2012, 10, 14, 9, 30) and "convertito" in d.rover_info.text()
    d.rover.edit.setText(str(events_test.RTCM_OBS))
    d.refresh()
    assert not d.day_row.isVisibleTo(d) and "approx_time" not in d.values()
    _close(qt_app, d)


def test_ppk_dialog_with_the_lever_open_fits_a_laptop_screen(qt_app, tmp_path, monkeypatch):
    """The dialog with everything it can show (the events of the rover, the lever arm open, an error under the
    form) is taller than the screen of a 13" laptop: it is never taller than the room the screen has, the form
    scrolls, no text is cut, the buttons and the error stay in sight and every field is reached by scrolling."""
    from PySide6.QtWidgets import QDialogButtonBox, QLabel

    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))
    d = _dialog(qt_app, events_test.RTCM_OBS)
    assert d.kind() == "events"
    d.b_lever.setChecked(True)
    d.refresh()
    qt_app.processEvents()
    d.accept()  # no base yet: said under the form
    assert d.lever_panel.isVisibleTo(d) and d.error.isVisible() and d.error.text()
    buttons = d.findChild(QDialogButtonBox)

    def fits(room: int) -> None:
        assert d.height() <= room and d.scroll.verticalScrollBar().isVisible(), (d.height(), room)
        assert d.body.height() > d.scroll.viewport().height()  # the whole form is there, scrolled
        for widget in (buttons, d.error):  # outside the form, whole inside the dialog
            assert widget.isVisible() and d.rect().contains(widget.geometry()), widget
        assert buttons.geometry().top() >= d.scroll.geometry().bottom()
        cut = [label.text()[:40] for label in d.findChildren(QLabel) if label.isVisible() and label.wordWrap()
               and label.text() and label.heightForWidth(label.width()) > label.height()]
        assert cut == []
        for field in (d.rover.edit, d.lever_panel.down, d.replace_fix):  # the top, the lever, the bottom of the form
            d.scroll.ensureWidgetVisible(field)
            qt_app.processEvents()
            assert not field.visibleRegion().isEmpty(), field

    available = d.screen().availableGeometry()
    assert available.contains(d.frameGeometry())
    fits(available.height())
    for room in (800, 720, 600):  # a 13" laptop (about 800 points of usable height), and less
        monkeypatch.setattr(d, "_room", lambda room=room: room)
        d.refresh()
        d.accept()  # the error again: a refresh takes it away
        qt_app.processEvents()
        assert d.error.text() == "Scegli le osservazioni della base."
        fits(min(room, available.height()))
    _close(qt_app, d)


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
def test_autopilot_dialog_with_room_shows_all_without_a_scrollbar(qt_app, tmp_path):
    """On a screen with room the dialog holds all it says, with no scrollbar: a scrollbar that came out while the
    dialog was still short takes some width, the texts wrap on more lines and it would stay for ever. On a short
    screen, and when the user makes the dialog short, the scrollbar is there."""
    from pyarchinit_photogrammetry.gui.autopilot_dialog import AutopilotDialog

    class Roomy(AutopilotDialog):
        tall = 4000

        def max_height(self) -> int:
            return self.tall

    log, _expected, project, _folder = _px4_flight(tmp_path, separation=None, layout="1.7")
    doc = ppg.Document.open(project)
    d = Roomy(doc.chunk, log)
    d.show()
    assert _wait_until(lambda: d.b_apply.isEnabled()), d.summary.text() + d.error.text()
    _wait_until(lambda: False, 0.05)
    view, bar = d.scroll.viewport(), d.scroll.verticalScrollBar()
    assert not bar.isVisible()
    assert view.height() >= d.scroll.widget().layout().totalHeightForWidth(view.width())
    d.resize(d.width(), 300)  # made short by the user: scrolled, never cut
    assert _wait_until(bar.isVisible)
    _close(qt_app, d)
    Roomy.tall = 300  # a short screen
    d = Roomy(doc.chunk, log)
    d.show()
    assert _wait_until(lambda: d.b_apply.isEnabled()), d.summary.text() + d.error.text()
    assert _wait_until(d.scroll.verticalScrollBar().isVisible)
    _close(qt_app, d)


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
        assert "Braccio di leva dall'antenna alla camera (avanti, destra, giù): 0, 0, 250 mm" in text
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
        # the run is remembered with the files the user named, not with those of the cache of conversions
        from pyarchinit_photogrammetry.gnss import download, ppk

        last = ppk.last_run(w.chunk)
        cache = str(download.cache_dir("converted"))
        assert last["rover"] == str(rover.absolute()) and last["base"] == str(dat.absolute()), last
        assert not [nav for nav in last["navs"] if nav.startswith(cache)], last["navs"]
        again = mw.PpkDialog(rover, w, ppk.work_folder(w.chunk), w.chunk)  # the dialog opened again: the user's files
        assert again.base.paths() == [dat.absolute()] and again.rover.path() == rover
        _close(qt_app, again)
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
