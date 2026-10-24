"""Media of PyArchInit linked to a US or a site, browsed from the PyArchInit pane."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest


def _image(path, value):
    from PIL import Image

    Image.fromarray(np.full((120, 160, 3), value, np.uint8)).save(path)
    return path


def test_media_of_a_us(pa_db, tmp_path):
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit
    from pyarchinit_photogrammetry.pyarchinit.media import register_media

    pa = PyArchInit.connect()
    a = register_media(pa.db, _image(tmp_path / "foto_us1.jpg", 90), descrizione="Taglio della fossa",
                       links=[("US", pa.us_id("Saggio", "1", "1"))])
    register_media(pa.db, _image(tmp_path / "foto_sito.jpg", 160), links=[("SITO", pa.ensure_site("Saggio"))])
    media = pa.us_media("Saggio", "1", "1")
    assert [m["id_media"] for m in media] == [a] and media[0]["descrizione"] == "Taglio della fossa"
    assert media[0]["path"] == tmp_path / "foto_us1.jpg" and media[0]["thumb"].exists() and media[0]["resize"].exists()
    assert pa.us_media("Saggio", "1", "2") == []
    assert [m["filename"] for m in pa.site_media("Saggio")] == ["foto_sito"]


def test_pane_shows_the_media(pa_db, tmp_path, monkeypatch):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from pyarchinit_photogrammetry.gui import panels
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit
    from pyarchinit_photogrammetry.pyarchinit.media import register_media

    app = QApplication.instance() or QApplication([])
    pa = PyArchInit.connect()
    register_media(pa.db, _image(tmp_path / "pianta.jpg", 120), links=[("US", pa.us_id("Saggio", "1", "3"))])
    pane = panels.PyArchInitPane()
    pane.set_connection(pa, "prova")
    us3 = None
    for i in range(pane.tree.topLevelItemCount()):
        site = pane.tree.topLevelItem(i)
        for j in range(site.childCount()):
            group = site.child(j)
            for k in range(group.childCount()):
                it = group.child(k)
                data = it.data(0, Qt.ItemDataRole.UserRole)
                if data and data[2] == "3":
                    us3 = it
    assert us3 is not None
    pane.tree.setCurrentItem(us3)
    app.processEvents()
    assert pane.media.count() == 1 and "Media di US 3" in pane.media_label.text()
    opened = []
    from PySide6.QtGui import QDesktopServices

    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toLocalFile())))
    pane._open_media(pane.media.item(0))
    assert [Path(p) for p in opened] == [tmp_path / "pianta.jpg"]
    pane.deleteLater()
