"""Layer labels can be hidden or edited without changing archaeological identifiers."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(qapp):
    from PySide6.QtCore import QCoreApplication, QEvent

    from pyarchinit_photogrammetry.gui.image_views import RasterView

    v = RasterView()
    v.detail = False
    v.show_frame((0, 0, 10, 10), 0.02)
    yield v
    v.close()
    v.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_hidden_labels_leave_geometry_selectable_and_survive_layer_toggle(view):
    v = view
    polygon = v.add_polygon([(1, 1), (2, 1), (2, 2), (1, 2)], "UT 1", layer="UT")
    v.add_line([(3, 1), (4, 2)], label="Confine", layer="Confini")
    v.add_point(5, 5, label="Punto 1", layer="Punti")
    for name in ("UT", "Confini", "Punti"):
        v.set_layer_labels_visible(name, False)
        item, label, _ = v.layers[name]["features"][0]
        assert item.isVisible() and not label.isVisible()
        v.set_layer_visible(name, False)
        v.set_layer_visible(name, True)
        assert item.isVisible() and not label.isVisible()
    v.select_in_polygon([v.scene_pt(x, y) for x, y in [(0, 0), (2.5, 0), (2.5, 2.5), (0, 2.5)]])
    assert v.selection == [polygon]
    # New features in an existing layer obey the label setting too.
    v.add_point(6, 6, label="Punto 2", layer="Punti")
    assert not v.layers["Punti"]["features"][1][1].isVisible()
    v.set_layer_labels_visible("Punti", True)
    assert all(label.isVisible() for _, label, _ in v.layers["Punti"]["features"])
    assert not v.layers["UT"]["features"][0][1].isVisible()


@pytest.mark.parametrize("kind", ["point", "line", "polygon"])
def test_editing_previously_empty_labels_creates_text_and_respects_visibility(view, kind):
    v = view
    if kind == "point":
        v.add_point(1, 1, layer="Prova")
    elif kind == "line":
        v.add_line([(1, 1), (2, 2)], layer="Prova")
    else:
        v.add_polygon([(1, 1), (2, 1), (2, 2), (1, 2)], layer="Prova")
    v.set_layer_labels_visible("Prova", False)
    v.set_feature_attribute("Prova", 0, "etichetta", "Recinto A")
    item, label, attrs = v.layers["Prova"]["features"][0]
    assert label is not None and label.text() == "Recinto A"
    assert attrs["etichetta"] == "Recinto A"
    assert item.isVisible() and not label.isVisible()
    v.set_layer_labels_visible("Prova", True)
    assert label.isVisible()
    v.set_feature_attribute("Prova", 0, "etichetta", "")
    assert label.text() == "" and attrs["etichetta"] == ""


def test_layer_settings_restore_generic_feature_label_overrides(view):
    from copy import deepcopy

    v = view
    v.add_point(1, 2, label="Originale", layer="Punti")
    v.set_feature_attribute("Punti", 0, "etichetta", "Ingresso")
    v.set_layer_labels_visible("Punti", False)
    saved = deepcopy(v.layer_settings)
    v.clear_overlays()
    v.restore_layer_settings({})
    v.add_point(1, 2, label="Originale", layer="Punti")
    assert v.layers["Punti"]["features"][0][1].isVisible()
    v.restore_layer_settings(saved)
    _, label, attrs = v.layers["Punti"]["features"][0]
    assert label.text() == "Ingresso" and attrs["etichetta"] == "Ingresso"
    assert not label.isVisible()


def test_labels_menu_edits_canvas_feature_beyond_the_tree_listing(view, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from pyarchinit_photogrammetry.gui.panels import LayersPane

    v = view
    with v.batch():
        for i in range(501):
            v.add_point(1 + i * 0.01, 1, label=f"Punto {i}", layer="Punti")
    pane = LayersPane()
    pane.set_view(v)
    first = pane.tree.topLevelItem(0).child(0)
    pane.tree.setCurrentItem(first)
    pane._clicked(first, 0)
    # A map click can reach all features, including those omitted from the capped tree.
    v.feature_clicked.emit("Punti", {**v.features("Punti")[500], "_index": 500})
    pane._fill_labels_menu()
    edit = next(action for action in pane._labels_menu.actions() if action.text() == "Modifica etichetta...")
    assert edit.isEnabled()
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Ingresso", True))
    edited = []
    pane.attribute_edited.connect(lambda *args: edited.append(args))
    edit.trigger()
    assert edited == [("Punti", 500, "etichetta", "Ingresso")]
    assert v.features("Punti")[500]["etichetta"] == "Ingresso"
    assert v.features("Punti")[0]["etichetta"] == "Punto 0"
    pane.deleteLater()


def test_custom_unit_label_and_layer_visibility_survive_redraw_and_project_reopen(trench, tmp_path):
    from shapely.geometry import box

    from pyarchinit_photogrammetry import Document
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    w, app, _ = trench
    w.add_units([UnitPolygon(box(2480001, 4500001, 2480002, 4500002), us="7", sito="Saggio")])
    v = w.ortho_view
    layer = next(name for name, data in v.layers.items()
                 if any(attrs.get("_unit") == 0 for _, _, attrs in data["features"]))
    v.set_feature_attribute(layer, 0, "etichetta", "Muro orientale")
    w._attribute_edited(layer, 0, "etichetta", "Muro orientale")
    v.set_layer_labels_visible(layer, False)
    w._redraw_overlays()
    assert w.units[0].us == "7"
    assert v.features(layer)[0]["etichetta"] == "Muro orientale"
    assert not v.layers[layer]["features"][0][1].isVisible()
    w._store_drawings()
    path = tmp_path / "etichette.ppg"
    w.doc.save(path)
    w.set_document(Document.open(path))
    app.processEvents()
    assert w.units[0].us == "7"
    assert v.features(layer)[0]["etichetta"] == "Muro orientale"
    item, label, _ = v.layers[layer]["features"][0]
    assert item.isVisible() and not label.isVisible()
    # An explicitly blank label also persists; it must not become the default "US 7".
    v.set_feature_attribute(layer, 0, "etichetta", "")
    w._attribute_edited(layer, 0, "etichetta", "")
    w._redraw_overlays()
    assert v.features(layer)[0]["etichetta"] == "" and w.units[0].us == "7"


def test_same_layer_name_has_independent_label_visibility_in_each_chunk(trench):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    w, app, _ = trench
    first = w.chunk
    geometry = box(2480001, 4500001, 2480002, 4500002)
    w.add_units([UnitPolygon(geometry, us="1", sito="Saggio")])
    layer = next(name for name, data in w.ortho_view.layers.items()
                 if any("_unit" in attrs for _, _, attrs in data["features"]))
    w.ortho_view.set_layer_labels_visible(layer, False)
    second = w.doc.add_chunk("Saggio 2")
    second.crs = first.crs
    second._set_product("orthomosaic", first.orthomosaic_path)
    second._set_product("dem", first.dem_path)
    w._activate_chunk(second)
    w.add_units([UnitPolygon(geometry, us="2", sito="Saggio")])
    assert w.ortho_view.layers[layer]["features"][0][1].isVisible()
    w._activate_chunk(first)
    app.processEvents()
    assert w.units[0].us == "1"
    assert not w.ortho_view.layers[layer]["features"][0][1].isVisible()
    w._activate_chunk(second)
    assert w.units[0].us == "2"
    assert w.ortho_view.layers[layer]["features"][0][1].isVisible()


def test_layer_labels_visibility_undo_redo_survives_overlay_redraw(trench):
    w, _, _ = trench
    v = w.ortho_view
    v.add_point(2480001, 4500001, label="Ingresso", layer="Punti prova")
    w.history.reset()
    v.set_layer_labels_visible("Punti prova", False)
    assert len(w.history.entries) == 1
    w._redraw_overlays()
    assert not v.layers["Punti prova"]["features"][0][1].isVisible()
    w.history.undo()
    w._redraw_overlays()
    item, label, _ = v.layers["Punti prova"]["features"][0]
    assert item.isVisible() and label.isVisible()
    w.history.redo()
    w._redraw_overlays()
    item, label, _ = v.layers["Punti prova"]["features"][0]
    assert item.isVisible() and not label.isVisible()


@pytest.mark.parametrize("kind", ["unit", "generic"])
def test_label_text_edit_is_one_undoable_action_even_after_redraw(trench, monkeypatch, kind):
    from PySide6.QtWidgets import QInputDialog
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    w, _, _ = trench
    v = w.ortho_view
    if kind == "unit":
        w.add_units([UnitPolygon(box(2480001, 4500001, 2480002, 4500002), us="7", sito="Saggio")])
        layer = next(name for name, data in v.layers.items()
                     if any("_unit" in attrs for _, _, attrs in data["features"]))
        original = "US 7"
    else:
        layer, original = "Punti prova", "Ingresso"
        v.add_point(2480001, 4500001, label=original, layer=layer)
    w.history.reset()
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Porta orientale", True))
    w.layers_pane.edit_feature_label(layer, 0)
    assert len(w.history.entries) == 1
    w._redraw_overlays()
    assert v.features(layer)[0]["etichetta"] == "Porta orientale"
    w.history.undo()
    w._redraw_overlays()
    assert v.features(layer)[0]["etichetta"] == original
    assert v.layers[layer]["features"][0][1].text() == original
    w.history.redo()
    w._redraw_overlays()
    assert v.features(layer)[0]["etichetta"] == "Porta orientale"
    assert v.layers[layer]["features"][0][1].text() == "Porta orientale"
    if kind == "unit":
        assert w.units[0].us == "7"



def test_label_style_of_a_layer_and_of_all(view):
    """Font, size, bold, italic, colour and halo of the labels of a layer; the default comes back."""
    from PySide6.QtCore import Qt

    v = view
    v.add_polygon([(1, 1), (2, 1), (2, 2), (1, 2)], "US 1", layer="US")
    v.add_point(5, 5, color="#30ff60", label="Q 1", layer="Quote")
    v.set_layer_label_style("US", {"size": 20, "bold": False, "italic": True, "color": "#ff0000",
                                   "halo": True, "halo_color": "#000000", "halo_width": 2})
    label = v.layers["US"]["features"][0][1]
    assert label.font().pointSizeF() == 20 and not label.font().bold() and label.font().italic()
    assert label.brush().color().name() == "#ff0000"
    assert label.pen().style() != Qt.PenStyle.NoPen and label.pen().widthF() == 2
    # a feature added later takes the style of its layer
    v.add_polygon([(3, 3), (4, 3), (4, 4), (3, 4)], "US 2", layer="US")
    assert v.layers["US"]["features"][1][1].font().pointSizeF() == 20
    # the other layer keeps its own colour; "all" styles every layer
    assert v.layers["Quote"]["features"][0][1].brush().color().name() == "#30ff60"
    v.set_layer_label_style(None, {"size": 14})
    assert all(f[1].font().pointSizeF() == 14 for n in ("US", "Quote") for f in v.layers[n]["features"])
    assert v.layers["Quote"]["features"][0][1].brush().color().name() == "#30ff60"  # its colour, not set
    # saved with the layer settings, and put back from them (project opened, undo)
    saved = {k: dict(s) for k, s in v.layer_settings.items()}
    v.set_layer_label_style("US", None)
    label = v.layers["US"]["features"][0][1]
    assert label.font().pointSizeF() == 11 and label.font().bold() and label.brush().color().name() == "#ffffff"
    assert "label_style" not in v.layer_settings["US"]
    v.restore_layer_settings(saved)
    assert v.layers["US"]["features"][0][1].font().pointSizeF() == 14


def test_label_style_dialog_from_the_layers_pane(view, monkeypatch):
    from pyarchinit_photogrammetry.gui.panels import LabelStyleDialog, LayersPane

    v = view
    v.add_line([(3, 1), (4, 2)], label="Confine", layer="Confini")
    pane = LayersPane()
    pane.set_view(v)
    dlg = LabelStyleDialog(v.label_style("Confini"), "Confini", "Helvetica")
    dlg.size.setValue(16)
    dlg.halo.setChecked(True)
    style = dlg.style()
    assert style["size"] == 16 and style["halo"] and style["color"] is None and style["bold"] is None

    class Fake:
        def __init__(self, *args):
            pass

        def exec(self):
            return True

        def style(self):
            return style

    monkeypatch.setattr(LayersPane, "LabelStyleDialog", Fake)
    pane.edit_label_style("Confini")
    assert v.layers["Confini"]["features"][0][1].font().pointSizeF() == 16
    assert v.layer_settings["Confini"]["label_style"]["halo"] is True


def test_label_style_is_undone(trench):
    w, _, _ = trench
    v = w.ortho_view
    v.add_point(2480001, 4500001, label="Ingresso", layer="Punti prova")
    w.history.reset()
    v.set_layer_label_style("Punti prova", {"size": 18})
    assert len(w.history.entries) == 1
    w._redraw_overlays()
    assert v.layers["Punti prova"]["features"][0][1].font().pointSizeF() == 18
    w.history.undo()
    w._redraw_overlays()
    assert v.layers["Punti prova"]["features"][0][1].font().pointSizeF() != 18
    w.history.redo()
    w._redraw_overlays()
    assert v.layers["Punti prova"]["features"][0][1].font().pointSizeF() == 18
