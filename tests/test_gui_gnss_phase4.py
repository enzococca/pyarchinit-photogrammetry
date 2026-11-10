"""The interface of the shots of self-built drones: the messages of the engine in Italian."""
from __future__ import annotations

import os
import re

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from pyarchinit_photogrammetry.gui import i18n  # noqa: E402

FIELD = re.compile(r"\{\d+[^{}]*\}")


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


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
