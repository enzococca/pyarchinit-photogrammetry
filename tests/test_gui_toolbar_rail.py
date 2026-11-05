"""The slim top bar (workflow menu, search of the commands) and the tool rails of the 2D and 3D views."""
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


def _settings(tmp_path):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))


@pytest.fixture
def window(qapp, processed, tmp_path):
    _settings(tmp_path)
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    doc, ch, _ = processed
    ch.meta.pop("drawings", None)
    w = MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    w.resize(1400, 860)
    w.show()
    qapp.processEvents()
    yield w
    w.setWindowModified(False)
    w.close()


def _pump(qapp, n=3):
    for _ in range(n):
        qapp.processEvents()


def _main_toolbars(w):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QToolBar

    return [t for t in w.findChildren(QToolBar)
            if w.toolBarArea(t) != Qt.ToolBarArea.NoToolBarArea and not t.isHidden()]


def test_one_top_bar_and_every_drawing_tool_in_the_rail(window, qapp):
    w = window
    assert [t.objectName() for t in _main_toolbars(w)] == ["workflow_toolbar"]
    top = [a.text() for a in w.workflow_toolbar.actions() if not a.isSeparator() and a.text()]
    assert top == ["Apri", "Salva", "Flusso di lavoro", "Elabora tutto", "PyArchInit"]
    assert w.command_search.parent() is w.workflow_toolbar
    # every tool of the old drawing bar, with the actions of its menus, is in the rail of the 2D view
    d = w.draw_actions
    old_bar = [d[m] for m in ("pan", "select", "polygon", "vertices", "reshape", "line", "measure", "area", "quota",
                              "probe", "breakline", "free", "marker", "sam", "patch")]
    old_bar += [w.a_snap, w.a_snap_vertex, w.a_snap_segment, w.a_freehand, w.a_select_freehand, w.a_identify_units,
                w.a_sel_delete, w.a_sel_merge, w.a_clear_drawings, w.a_contours, w.a_volume, w.a_slice,
                w.a_qgis_open, w.a_layers_pane, w.a_sections_pane, w.a_publish_drawings,
                *w.breakline_kind_actions.values(), *w.sam_tool_actions.values()]
    reachable = w.raster_rail.all_actions()
    missing = [a.text() for a in old_bar if a not in reachable]
    assert not missing, missing
    assert list(w.raster_rail.groups) == ["navigate", "draw", "measure", "analysis", "auto", "edit", "layers"]
    assert w.a_sections_pane in w.raster_rail.actions_of("analysis")  # Sezioni in the menu of Sezione


def test_flyout_opens_and_closes(window, qapp):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    w = window
    w.show_view(w.ortho_view)
    rail = w.raster_rail
    rail.set_pinned(False)
    _pump(qapp)
    assert rail.isVisible() and not rail.is_open()
    vp = w.ortho_view.viewport()
    assert rail.geometry().right() < vp.geometry().left() + 2  # beside the drawing, not over it
    group = rail.groups["draw"].button
    QTest.mouseClick(group, Qt.MouseButton.LeftButton)
    _pump(qapp)
    assert rail.current() == "draw" and rail.flyout.isVisible() and group.isChecked()
    assert rail.flyout.parent() is w.ortho_view and rail.flyout.geometry().left() >= vp.geometry().left()
    QTest.mouseClick(group, Qt.MouseButton.LeftButton)  # the same group again: closed
    _pump(qapp)
    assert not rail.is_open() and not group.isChecked()
    rail.open("measure")
    QTest.keyClick(w.ortho_view, Qt.Key.Key_Escape)
    assert not rail.is_open()
    rail.open("measure")
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 300))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 300))
    assert not rail.is_open()
    # pinned: Esc and the view leave it open, a tool chosen too; the group button closes it
    rail.open("measure")
    rail.flyout.pin.click()
    assert rail.pinned and w.model_view.rail.pinned  # one choice for both rails
    assert w.settings.value("rail/pinned") == "true"
    QTest.keyClick(w.ortho_view, Qt.Key.Key_Escape)
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 300))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(300, 300))
    assert rail.current() == "measure"
    rail.flyout.pin.click()
    assert not rail.pinned
    # a tool chosen in the flyout: the tool is on, the group stands out, the flyout goes
    measure = next(b for b in rail.groups["measure"].widgets if getattr(b, "action", None) is w.draw_actions["measure"])
    measure.click()
    _pump(qapp)
    assert w.draw_actions["measure"].isChecked() and measure.isChecked()
    assert not rail.is_open() and rail.active_group() == "measure"
    w.draw_actions["pan"].trigger()
    assert rail.active_group() == "navigate"
    rail.set_text(True)  # the names of the groups under their icons: a wider rail, the view margin follows
    _pump(qapp)
    assert rail.width() > rail.WIDTH_ICONS and w.settings.value("rail/text") == "true"
    assert rail.geometry().right() < vp.geometry().left() + 2
    rail.set_text(False)


def test_workflow_menu_shows_the_steps_done(window, qapp):
    w = window
    w.refresh_all()
    states = w.workflow_states()
    assert all(states.values()), states  # the processed project has every product
    assert all(a.data() is True for a in w.workflow_actions.values())
    steps = [a.text() for a in w.workflow_menu.actions() if not a.isSeparator()]
    assert steps[0].startswith("1.") and "Aggiungi foto" in steps[0] and "Ortofoto" in steps[-1]
    import pyarchinit_photogrammetry as ppg

    new = ppg.Document()
    new.add_chunk("vuoto")
    w.set_document(new)
    w._update_workflow_states()
    assert not any(a.data() for a in w.workflow_actions.values())


def test_command_search_finds_and_runs_a_command(window, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    w = window
    search = w.command_search
    w.command_search.start()
    found = search.matches("console python")
    assert found and found[0].startswith("Console Python") and "Vista" in found[0]
    ran = []
    search.commands[found[0]].triggered.connect(lambda *_: ran.append(1))
    search.setText("console pyt")
    QTest.keyClick(search, Qt.Key.Key_Return)
    _pump(qapp)
    assert ran == [1] and search.text() == ""
    assert not search.run("Nessun comando con questo nome")
    assert all("..." not in k.split("  —  ")[0] and "&" not in k for k in search.commands)


def test_3d_view_one_row_and_the_tools_in_its_rail(window, qapp):
    from PySide6.QtWidgets import QPushButton

    mv = window.model_view
    window.show_view(mv)
    _pump(qapp)
    rail = mv.rail
    assert list(rail.groups) == ["view", "points", "cut", "draw"]
    buttons = [mv.b_top, mv.b_reset, mv.b_center, mv.b_trackball, mv.b_marker, mv.b_photos, mv.b_lasso,
               mv.b_section, mv.b_slice, mv.b_cut, mv.b_elevation, mv.b_draw3d]
    inside = [b for g in rail.groups.values() for b in g.widgets]
    assert all(b in inside for b in buttons)
    assert mv.exag_spin.window() is window and rail.flyout.isAncestorOf(mv.exag_spin)
    above = [b for b in mv.findChildren(QPushButton) if not rail.flyout.isAncestorOf(b)]
    assert not above, [b.text() for b in above]  # only the display choices stay above the canvas
    for b in (mv.combo, mv.cb_cams, mv.cb_marks, mv.cb_region):
        assert not rail.flyout.isAncestorOf(b)
    rows = {b.mapTo(mv, b.rect().topLeft()).y() for b in (mv.combo, mv.cb_cams, mv.cb_marks, mv.cb_region)}
    assert max(rows) - min(rows) < mv.combo.height()  # one row
    rail.open("cut")
    _pump(qapp)
    assert rail.flyout.isVisible() and mv.b_section.isVisible()
    assert rail.flyout.geometry().right() <= mv.canvas.mapTo(mv, mv.canvas.rect().topLeft()).x() + 4  # beside it
    mv.b_section.click()  # a tool chosen: the canvas free again
    _pump(qapp)
    assert not rail.is_open()


def test_classic_drawing_bar_comes_back(window, qapp, tmp_path):
    w = window
    w.a_classic_bar.setChecked(True)  # Vista > Barre > Barra di disegno classica
    _pump(qapp)
    names = [t.objectName() for t in _main_toolbars(w)]
    assert names == ["workflow_toolbar", "drawing_toolbar"]
    assert not w.raster_rail.isVisible() and w.ortho_view.viewportMargins().left() == 0
    assert w.settings.value("toolbar/classic") == "true"
    bar = w.drawing_toolbar
    assert w.draw_actions["sam"] in bar.actions() and w.a_publish_drawings in bar.actions()
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    other = MainWindow(w.doc, autoconnect=False)  # the choice is kept
    try:
        assert other.a_classic_bar.isChecked() and not other.drawing_toolbar.isHidden()
        assert other.raster_rail.isHidden()
    finally:
        other.setWindowModified(False)
        other.close()
    w.a_classic_bar.setChecked(False)
    _pump(qapp)
    assert [t.objectName() for t in _main_toolbars(w)] == ["workflow_toolbar"]
    assert not w.raster_rail.isHidden()


def test_top_bar_fits_a_narrow_window(window, qapp):
    from PySide6.QtWidgets import QToolButton

    w = window
    for width in (2000, 1000, 360):
        w.resize(width, 570)
        _pump(qapp, 4)
        tb = w.workflow_toolbar
        buttons = [b for b in (tb.widgetForAction(a) for a in tb.actions()) if isinstance(b, QToolButton)]
        assert all(b.isVisible() for b in buttons), width  # none behind >>
        assert w.command_search.isVisible()
        assert tb.sizeHint().width() <= max(width, w.minimumSizeHint().width())
    assert w.command_search.width() >= 150
