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
    buttons = [mv.b_top, mv.b_reset, mv.b_center, mv.b_trackball, mv.b_look, mv.b_marker, mv.b_photos, mv.b_lasso,
               mv.b_section, mv.b_slice, mv.b_cut, mv.b_elevation, mv.b_draw3d]
    inside = [b for g in rail.groups.values() for b in g.widgets]
    assert all(b in inside for b in buttons)
    assert mv.exag_spin.window() is window and rail.flyout.isAncestorOf(mv.exag_spin)
    # only the display choices stay above the canvas (and the row of the photo looked through, shown only then)
    assert not mv.look_shown()
    above = [b for b in mv.findChildren(QPushButton) if not rail.flyout.isAncestorOf(b)
             and not any(p.isAncestorOf(b) for p in mv.look_parts)]
    assert not above, [b.text() for b in above]
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
    assert w.a_analysis in bar.actions()  # the analyses of the old top bar, in one button's menu
    assert w.analysis_menu.actions() == [w.a_contours, w.a_volume, w.a_slice]
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


def _click(widget, x, y):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    QTest.mousePress(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x, y))
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x, y))


def test_the_click_that_closes_the_flyout_does_not_draw(window, qapp):
    """2D: in Disegna US with one vertex placed, a click on the view with a flyout open closes it and adds nothing."""
    from PySide6.QtTest import QTest

    w = window
    w.show_view(w.ortho_view)
    rail = w.raster_rail
    rail.set_pinned(False)
    w.set_draw_mode("polygon")
    _pump(qapp)
    vp = w.ortho_view.viewport()
    _click(vp, 200, 200)
    QTest.qWait(500)  # never a double click with the next one
    assert len(w.ortho_view._clicks) == 1
    rail.open("measure")
    _click(vp, 260, 240)
    _pump(qapp)
    assert not rail.is_open()
    assert len(w.ortho_view._clicks) == 1  # the press closed the flyout only
    QTest.qWait(500)
    _click(vp, 260, 240)  # the next click draws again
    assert len(w.ortho_view._clicks) == 2
    w.set_draw_mode("pan")


def test_3d_flyout_closes_first_on_press_and_esc(window, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    mv = window.model_view
    window.show_view(mv)
    _pump(qapp)
    rail = mv.rail
    rail.set_pinned(False)
    mv.start_drawing("line")
    _pump(qapp)
    assert mv._draw_kind == "line"
    target = mv._draw_target()
    rail.open("view")
    _pump(qapp)
    QTest.mousePress(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, target.rect().center())
    assert not rail.is_open() and mv._press is None  # the press closed the flyout and never reached the drawing
    QTest.mouseRelease(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, target.rect().center())
    assert mv._draw_pts == []
    rail.open("view")
    QTest.keyClick(target, Qt.Key.Key_Escape)  # first Esc: the flyout, as in the 2D view
    assert not rail.is_open() and mv._draw_kind == "line"
    QTest.keyClick(target, Qt.Key.Key_Escape)  # second Esc: the drawing
    assert mv._draw_kind is None


def test_a_toggle_keeps_the_flyout_open(window, qapp):
    w = window
    rail = w.raster_rail
    rail.set_pinned(False)
    w.show_view(w.ortho_view)
    rail.open("draw")
    snap = next(b for b in rail.groups["draw"].widgets if getattr(b, "action", None) is w.a_snap)
    was = w.a_snap.isChecked()
    snap.click()
    assert w.a_snap.isChecked() is not was and rail.current() == "draw"  # an option: the flyout stays
    snap.click()
    rail.close()


def test_ripristina_keeps_the_choice_of_the_drawing_bar(window, qapp):
    w = window
    w.show_view(w.ortho_view)
    for classic in (True, False):
        w.a_classic_bar.setChecked(classic)
        _pump(qapp)
        w.panes.reset()  # Vista > Pannelli > Ripristina la disposizione dei pannelli
        _pump(qapp)
        assert w.a_classic_bar.isChecked() is classic
        assert w.drawing_toolbar.isHidden() is not classic
        assert w.raster_rail.isHidden() is classic


def _stray_tab_bars(w):
    from PySide6.QtWidgets import QTabBar

    docked = [d for d in w.panes.docks if not d.isHidden() and not d.isFloating()]
    areas = {w.dockWidgetArea(d) for d in docked}
    groups = 0
    for area in areas:  # a tab bar for each group of more than one pane in an area
        here = [d for d in docked if w.dockWidgetArea(d) == area]
        seen = set()
        for d in here:
            if d in seen:
                continue
            together = {d, *[t for t in w.tabifiedDockWidgets(d) if t in here]}
            seen |= together
            groups += len(together) > 1
    bars = [b for b in w.findChildren(QTabBar) if b.parent() is w
            and b.metaObject().className() == "QMainWindowTabBar" and b.isVisible()]
    return bars, groups


def test_no_stray_tab_bars_of_the_panes(window, qapp):
    """Qt 6.12 leaves a second, unused tab bar when panes are tabbed in a second area: it showed through as a
    second row of Foto | Sezioni at laptop sizes. Only the tab bars of the groups of panes are visible."""
    w = window
    w.resize(1000, 700)
    _pump(qapp, 5)
    bars, groups = _stray_tab_bars(w)
    assert len(bars) == groups, [[b.tabText(i) for i in range(b.count())] for b in bars]
    w.panes.reset()
    _pump(qapp, 5)
    bars, groups = _stray_tab_bars(w)
    assert len(bars) == groups, [[b.tabText(i) for i in range(b.count())] for b in bars]


def test_command_search_shortcut_is_of_the_window(window):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QShortcut

    keys = [s for s in window.findChildren(QShortcut) if s.key().toString() == "Ctrl+K"]
    assert len(keys) == 1 and keys[0].context() == Qt.ShortcutContext.WindowShortcut


def test_flyout_scrolls_sideways_when_the_view_is_narrow(window, qapp):
    from PySide6.QtCore import Qt

    w = window
    w.show_view(w.ortho_view)
    w.resize(560, 600)
    _pump(qapp, 5)
    rail = w.raster_rail
    rail.open("draw")
    _pump(qapp)
    fly = rail.flyout
    assert fly.scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    if fly.width() < fly.wanted_size().width():  # cut by the view: the rest reachable by scrolling
        assert fly.scroll.horizontalScrollBar().maximum() > 0
    rail.close()


def test_command_search_shortcut_wins_over_a_text_field(window, qapp):
    """Ctrl+K in a focused text field (a pane's filter, the console): on Linux a line edit takes it to delete to
    the end of the line; in the window it opens the search of the commands."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication, QLineEdit

    w = window
    field = QLineEdit(w.centralWidget())
    field.show()
    w.activateWindow()
    field.setFocus()
    _pump(qapp)
    w._keep_search_shortcut(None, field)  # what focusChanged does (offscreen windows may not get the focus)
    ev = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier, "k")
    ev.setAccepted(False)
    QApplication.sendEvent(field, ev)
    assert not ev.isAccepted()  # the field does not keep the key: the shortcut of the window runs
    other = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier, "a")
    other.setAccepted(False)
    QApplication.sendEvent(field, other)
    assert other.isAccepted()  # the other keys of the field are untouched (Ctrl+A selects all)
    w._keep_search_shortcut(field, None)  # the cursor leaves: the filter goes with it
    import gc
    import weakref

    ref = weakref.ref(w._shortcut_keeper)
    assert not any(isinstance(r, type(w)) for r in gc.get_referents(ref()))  # no cycle with the window
    field.deleteLater()
