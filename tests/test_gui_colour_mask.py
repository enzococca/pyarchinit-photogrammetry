"""*Strumenti > Maschera dal colore...*: the preview on a small copy, the sample from a click, the layer written in the
worker's turn as one step of Modifica > Annulla, *Ritaglia con questa maschera* in the Layer pane. The synthetic
orthophoto of ``test_colour_mask`` (a synthetic origin, no real place)."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from test_colour_mask import RES, WATER, project  # noqa: E402

from pyarchinit_photogrammetry.core import free_drawings  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _window(qapp, doc, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mwmod
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    said: list = []
    monkeypatch.setattr(mwmod, "message", lambda *a, **k: said.append(("message", a[2] if len(a) > 2 else "")))
    monkeypatch.setattr(mwmod, "choice", lambda *a, **k: said.append(("choice", a[2] if len(a) > 2 else "")))
    w = MainWindow(doc, autoconnect=False)
    w.said = said
    monkeypatch.setattr(w, "_warn", lambda text: said.append(("warn", text)))
    monkeypatch.setattr(w, "_info", lambda title, text: said.append(("info", text)))
    monkeypatch.setattr(w, "_error", lambda summary, details="", logged=True: said.append(("error", summary)))
    w.runner.show_dialog = False
    w.show()
    qapp.processEvents()
    return w


def _close(w):
    w._close_colour_mask_dialog()
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


def _action(w, text):
    from pyarchinit_photogrammetry.gui.command_search import clean, menu_commands

    for name, a in menu_commands(w.menuBar()).items():
        if clean(a.text()) == clean(text):
            return a
    raise AssertionError(f"no command {text!r}")


def test_dialog_makes_a_layer_and_undo_removes_it(qapp, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.gui.colour_mask_dialog import PREVIEW_LAYER
    from pyarchinit_photogrammetry.gui.free_drawing_actions import LAYER_PREFIX

    doc, a = project(tmp_path)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        if w.ortho_view.tr is None:
            w._show_raster("ortho")
            qapp.processEvents()
        _action(w, "Maschera dal colore...").trigger()  # in Strumenti, so in the command search too
        qapp.processEvents()
        dlg = w._colour_mask_dialog
        assert dlg is not None and dlg.isVisible()
        assert dlg.e_layer.text() == "Maschera colore 1"
        # the sample: a click on the view lent by request_geometry
        cx, cy = (WATER[0] + WATER[2]) / 2 + 0.3, (WATER[1] + WATER[3]) / 2
        monkeypatch.setattr(w, "request_geometry", lambda kind, done, cancelled=None, hint="":
                            done(np.array([[cx, cy]])) or True)
        assert dlg.start_sample()
        rng = dlg.range()
        assert 170 < (rng.hue[0] + rng.hue[1]) / 2 < 210
        r = dlg.update_preview()
        assert r is not None and len(r.polygons) == 1
        assert np.allclose(r.polygons[0].bounds, WATER, atol=2 * r.cell)
        assert "1 regioni" in dlg.l_preview.text()
        assert PREVIEW_LAYER in w.ortho_view.layers
        n_steps = len(w.history.entries)
        assert dlg.create()
        w.runner.wait()
        qapp.processEvents()
        assert not w.said, w.said
        feats = free_drawings.features(a, "Maschera colore 1")
        assert len(feats) == 1
        assert np.allclose(np.asarray(feats[0]["xyz"])[:, :2].min(axis=0), WATER[:2], atol=2 * RES)
        assert len(w.history.entries) == n_steps + 1
        assert LAYER_PREFIX + "Maschera colore 1" in w.ortho_view.layers
        assert PREVIEW_LAYER not in w.ortho_view.layers
        assert dlg.e_layer.text() == "Maschera colore 2"
        # Ritaglia con questa maschera: in the menu of the layer, the union of its polygons to crop_products
        cropped: list = []
        monkeypatch.setattr(w, "crop_products", lambda geom: cropped.append(geom))
        menu = w.layers_pane.layer_menu(LAYER_PREFIX + "Maschera colore 1")
        act = next(x for x in menu.actions() if x.text().startswith("Ritaglia con questa maschera"))
        act.trigger()
        assert len(cropped) == 1 and cropped[0].area == pytest.approx(r.area, rel=0.05)
        menu_other = w.layers_pane.layer_menu("Marker")
        assert not any(x.text().startswith("Ritaglia con questa maschera") for x in menu_other.actions())
        # one step of Modifica > Annulla takes the layer away, Ripeti puts it back
        w.undo()
        qapp.processEvents()
        assert free_drawings.features(w.chunk, "Maschera colore 1") == []
        w.redo()
        qapp.processEvents()
        assert len(free_drawings.features(w.chunk, "Maschera colore 1")) == 1
        dlg.close()
        qapp.processEvents()
        from pyarchinit_photogrammetry.gui.ties_dialog import alive

        assert not alive(dlg)  # deleted on close
        assert PREVIEW_LAYER not in w.ortho_view.layers
    finally:
        _close(w)


def test_no_orthophoto_said(qapp, tmp_path, monkeypatch):
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    doc.add_chunk("A")
    doc.save(tmp_path / "q.ppg")
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        assert w.open_colour_mask() is None
        assert any("ortofoto" in t for _k, t in w.said)
    finally:
        _close(w)


def test_layer_menu_words_and_a_chunk_changed_during_the_run(qapp, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.engine import colour_mask
    from pyarchinit_photogrammetry.gui import i18n
    from pyarchinit_photogrammetry.gui.colour_mask_dialog import error_text
    from pyarchinit_photogrammetry.gui.free_drawing_actions import LAYER_PREFIX

    doc, a = project(tmp_path)
    free_drawings.add(a, "polygon", [[WATER[0], WATER[1], 50], [WATER[2], WATER[1], 50], [WATER[2], WATER[3], 50]],
                      layer="A mano")
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        texts = [x.text() for x in w.layers_pane.layer_menu(LAYER_PREFIX + "A mano").actions()]
        assert "Ritaglia con i poligoni di questo layer..." in texts
        assert not any(t.startswith("Ritaglia con questa maschera") for t in texts)
        dlg = w.open_colour_mask()
        result = colour_mask.mask(a, colour_mask.HsvRange((170.0, 210.0), (40.0, 100.0), (30.0, 100.0)))
        b = doc.add_chunk("B")
        w._activate_chunk(b)
        qapp.processEvents()
        assert w.chunk is b
        dlg._made(result, "Maschera colore 1")
        assert free_drawings.features(a, "Maschera colore 1") == []
        assert any("chunk attivo è cambiato" in t for _k, t in w.said)
        err = colour_mask.MaskError("the mask is empty", "la maschera è vuota")
        assert error_text(err) == "la maschera è vuota"
        i18n.set_current("en")
        try:
            assert error_text(err) == "the mask is empty"
        finally:
            i18n.set_current("it")
    finally:
        _close(w)
