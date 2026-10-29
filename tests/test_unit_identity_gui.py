"""Actual canvas/TOC grouping, numbering, undo and persistence in the desktop window."""
from __future__ import annotations

import pytest
from shapely.geometry import box

pytest.importorskip("PySide6")

from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
from pyarchinit_photogrammetry.archaeo.unit_identity import plan_units


@pytest.fixture(scope="module", autouse=True)
def application():
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    for widget in app.topLevelWidgets():
        if widget.parentWidget() is None:
            # a window left by an earlier module may be marked as modified: closing it would ask
            # whether to save, a modal question nobody answers here (the CI waited for it 20 minutes)
            widget.setWindowModified(False)
            widget.close()
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_selected_units_receive_shared_name_ut_layer_and_one_undo(trench, monkeypatch, tmp_path):
    from pyarchinit_photogrammetry.gui.unit_identity_dialog import UnitIdentityDialog
    from pyarchinit_photogrammetry import Document

    w, app, _ = trench
    x, y = 2480001, 4500001
    w.add_units([UnitPolygon(box(x + i, y, x + i + .5, y + .5), sito="Saggio") for i in range(4)])
    w.history.reset()
    features = [item for layer in w.ortho_view.layers.values() for item, _, a in layer["features"]
                if a.get("_unit") in (1, 2)]
    w.ortho_view.select_items(features)
    captured = []

    def ask(units, selected, **kwargs):
        captured.append(selected)
        return plan_units(units, selected, tipo="UT", progetto="Necropoli", name_mode="same",
                          name="Tombe settore A", group="Tombe settore A")

    monkeypatch.setattr(UnitIdentityDialog, "ask", ask)
    w.identify_units()
    assert captured == [{1, 2}]
    assert [u.tipo for u in w.units] == ["US", "UT", "UT", "US"]
    assert [u.us for u in w.units] == [None, "1", "2", None]
    group = "Unità topografiche - Tombe settore A"
    assert len(w.ortho_view.features(group)) == 2
    assert len(w.ortho_view.features("Unita' stratigrafiche")) == 2
    assert {a["nome"] for a in w.ortho_view.features(group)} == {"Tombe settore A"}
    assert len(w.history.entries) == 1
    assert {a["_unit"] for _, _, a in w.ortho_view.selected_features()} == {1, 2}
    assert not w._publishable()[0]  # UT are excluded from excavation geometry publishing.
    w.history.undo()
    assert all(u.tipo == "US" and u.us is None for u in w.units)
    assert group not in w.ortho_view.layers
    w.history.redo()
    assert len(w.ortho_view.features(group)) == 2
    w._store_drawings()
    target = tmp_path / "identified.ppg"
    w.doc.save(target)
    reopened = Document.open(target)
    drawings = reopened.chunk.meta["drawings"]["units"]
    assert drawings[1]["tipo"] == "UT" and drawings[1]["attributes"]["nome"] == "Tombe settore A"
    assert drawings[1]["layer"] == group
    app.processEvents()


def test_lasso_menu_and_same_name_dialog_preserve_unique_numbers(trench):
    from pyarchinit_photogrammetry.gui.unit_identity_dialog import UnitIdentityDialog

    w, app, _ = trench
    w.a_select_freehand.setChecked(True)
    assert w.ortho_view.mode == "select" and w.ortho_view.selection_shape == "freehand"
    w.a_select_freehand.setChecked(False)
    assert w.ortho_view.selection_shape == "rectangle"
    units = [UnitPolygon(box(i, 0, i + .5, 1), sito="Temple") for i in range(3)]
    dialog = UnitIdentityDialog(units, selected={0, 2}, parent=w, defaults={"progetto": "Survey"})
    dialog.kind.setCurrentIndex(dialog.kind.findData("UT"))
    dialog.naming.setCurrentIndex(dialog.naming.findData("same"))
    dialog.name.setText("Recinto A")
    changes = dialog.changes()
    assert {c.index for c in changes} == {0, 2}
    assert len({c.after.us for c in changes}) == 2
    assert {c.after.attributes["_layer"] for c in changes} == {"Unità topografiche - Recinto A"}
    assert all(u.us is None for u in units)
    dialog.close()
    dialog.deleteLater()
    app.processEvents()
