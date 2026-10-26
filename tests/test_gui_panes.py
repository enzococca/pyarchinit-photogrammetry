"""The panes of the window, each a dock of its own as in Metashape: Area di lavoro, Riferimenti and
Layer always there, the others closed and opened again from *Vista > Pannelli*; moved, floated and
tabbed; F6 / Shift+F6 from pane to pane; the layout saved with the window (that of the program up to
1.3.3 left out) and the default one put back; the methods of the tab widgets of 1.3.3 for the plugins
released, and ``context.add_pane`` / ``context.show_pane`` since 1.3.4."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from PySide6.QtWidgets import QLabel  # noqa: E402

LEFT = ["pane_workspace", "pane_reference", "pane_layers"]  # the fixed panes
RIGHT = ["pane_pyarchinit", "pane_proxies", "pane_multispectral", "pane_thermal", "pane_gnss"]  # beside them: the tools
BOTTOM = ["pane_photos", "pane_sections", "pane_console", "pane_log"]


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _settle(qapp) -> None:
    """Events handled and the objects waiting for deleteLater gone (tab bars Qt retired, docks removed)."""
    from PySide6.QtCore import QCoreApplication, QEvent

    for _ in range(3):
        qapp.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _front(dock) -> bool:
    """Open and in front: the panes behind another tab are moved out of sight by Qt."""
    g = dock.geometry()
    return not dock.isHidden() and g.right() >= 0 and g.bottom() >= 0


@pytest.fixture
def make_window(qapp, tmp_path):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    made = []

    def make():
        w = MainWindow(ppg.Document(), autoconnect=False, check_updates=False)
        w.runner.show_dialog = False
        w.resize(1400, 900)
        w.show()
        _settle(qapp)
        made.append(w)
        return w

    yield make
    for w in made:
        w.setWindowModified(False)
        w.close()


def test_every_pane_is_a_dock_of_its_own(make_window, qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDockWidget, QMainWindow, QScrollArea, QTabWidget

    from pyarchinit_photogrammetry.gui import icons

    w = make_window()
    docks = {d.objectName(): d for d in w.findChildren(QDockWidget)}
    panes = dict(zip(LEFT + RIGHT + BOTTOM,
                     [w.workspace, w.reference, w.layers_pane, w.pa_pane, w.proxies_pane, w.ms_pane, w.thermal_pane, w.gnss_pane,
                      w.photos_panel, w.sections_pane, w.console, w.log_pane]))
    assert set(panes) <= set(docks)
    for name, pane in panes.items():  # in a scroll area: a pane taller than a laptop screen scrolls
        scroll = docks[name].widget()
        assert isinstance(scroll, QScrollArea) and scroll.widget() is pane, name
    area = Qt.DockWidgetArea
    assert all(w.dockWidgetArea(docks[n]) == area.LeftDockWidgetArea for n in LEFT)
    assert all(w.dockWidgetArea(docks[n]) == area.RightDockWidgetArea for n in RIGHT)
    assert all(w.dockWidgetArea(docks[n]) == area.BottomDockWidgetArea for n in BOTTOM)
    assert w.d_pa is docks["pane_pyarchinit"]
    # the fixed panes tabbed on the left, the tools on the right (seen beside them), the bottom ones together
    assert {d.objectName() for d in w.tabifiedDockWidgets(docks["pane_workspace"])} == set(LEFT[1:])
    assert {d.objectName() for d in w.tabifiedDockWidgets(docks["pane_pyarchinit"])} == set(RIGHT[1:])
    assert {d.objectName() for d in w.tabifiedDockWidgets(docks["pane_photos"])} == set(BOTTOM[1:])
    assert _front(docks["pane_workspace"]) and _front(docks["pane_photos"]) and _front(docks["pane_pyarchinit"])
    assert not any(_front(docks[n]) for n in LEFT[1:] + RIGHT[1:] + BOTTOM[1:])
    assert w.tabPosition(area.LeftDockWidgetArea) == QTabWidget.TabPosition.North
    assert w.tabPosition(area.BottomDockWidgetArea) == QTabWidget.TabPosition.North
    options = QMainWindow.DockOption
    for option in (options.AllowTabbedDocks, options.AllowNestedDocks, options.GroupedDragging):
        assert w.dockOptions() & option
    bars = w.panes._tab_bars()
    assert sorted(b.count() for b in bars) == sorted([len(LEFT), len(RIGHT), len(BOTTOM)])
    if icons.available():  # the icon of each pane on its tab, as on the tabs of 1.3.3
        assert all(not b.tabIcon(i).isNull() for b in bars for i in range(b.count()))
    for page in (w.layers_pane, w.proxies_pane, w.reference, w.ms_pane):  # still fits a laptop screen
        w._show_pane(w.left_tabs, page)
        _settle(qapp)
        hint = w.minimumSizeHint()
        assert hint.width() <= 900 and hint.height() <= 640, (page, hint)


def test_fixed_panes_stay_the_others_close_and_come_back_from_the_menu(make_window, qapp):
    from PySide6.QtWidgets import QAbstractButton, QDockWidget

    w = make_window()
    feature = QDockWidget.DockWidgetFeature
    fixed = [w.panes.dock(p) for p in (w.workspace, w.reference, w.layers_pane)]

    def button(dock, name):
        return dock.findChild(QAbstractButton, f"qt_dockwidget_{name}button")

    for dock in fixed:
        assert dock.features() & feature.DockWidgetMovable and dock.features() & feature.DockWidgetFloatable
        assert not dock.features() & feature.DockWidgetClosable
        assert not dock.close() and not dock.isHidden()  # refused
        assert dock.toggleViewAction().isChecked() and not dock.toggleViewAction().isEnabled()
        assert button(dock, "float").isVisibleTo(dock) and not button(dock, "close").isVisibleTo(dock)
    others = [d for d in w.panes.docks if d not in fixed]
    assert len(others) == 9 and all(d.features() & feature.DockWidgetClosable for d in others)
    assert all(button(d, "close").isVisibleTo(d) for d in others)
    assert button(others[0], "close").toolTip() == "Chiudi il pannello: Vista > Pannelli lo riapre"
    view = next(a.menu() for a in w.menuBar().actions() if a.text() == "&Vista")
    assert w.panes.menu.menuAction() in view.actions()
    menu = {a.text(): a for a in w.panes.menu.actions() if a.text()}
    assert {"Area di lavoro", "Riferimenti", "Layer", "Proxy US", "Multispettrale", "Termico", "GNSS", "Foto", "Sezioni",
            "Console Python", "Log", "PyArchInit", "Pannello successivo", "Pannello precedente",
            "Ripristina la disposizione dei pannelli"} <= set(menu)
    ms = w.panes.dock(w.ms_pane)
    assert ms.close()
    _settle(qapp)
    assert ms.isHidden() and not menu["Multispettrale"].isChecked()
    assert ms not in w.tabifiedDockWidgets(w.d_pa)  # its tab went with it
    menu["Multispettrale"].trigger()  # Vista > Pannelli > Multispettrale
    _settle(qapp)
    assert menu["Multispettrale"].isChecked() and _front(ms) and w.left_tabs.currentWidget() is w.ms_pane
    assert ms in w.tabifiedDockWidgets(w.d_pa)  # back among the panes it was tabbed with (on the right)
    menu["Multispettrale"].trigger()  # unchecked: closed again
    _settle(qapp)
    assert ms.isHidden()


def test_show_pane_brings_back_a_closed_or_floating_pane(make_window, qapp):
    w = make_window()
    console, log = w.panes.dock(w.console), w.panes.dock(w.log_pane)
    console.close()
    _settle(qapp)
    w._show_pane(w.bottom_tabs, w.console)  # as the program calls it (Ctrl+Shift+P, a script that writes...)
    _settle(qapp)
    assert _front(console) and w.bottom_tabs.currentWidget() is w.console
    log.setFloating(True)
    _settle(qapp)
    log.close()
    _settle(qapp)
    assert log.isHidden()
    w._show_pane(w.log_pane)  # the pane alone is enough
    _settle(qapp)
    assert log.isFloating() and log.isVisible() and w.bottom_tabs.currentWidget() is w.log_pane
    view = next(a.menu() for a in w.menuBar().actions() if a.text() == "&Vista")
    next(a for a in view.actions() if a.text() == "Riferimenti").trigger()
    _settle(qapp)
    assert _front(w.panes.dock(w.reference)) and w.left_tabs.currentWidget() is w.reference


def test_layout_saved_restored_and_reset(make_window, qapp):
    from PySide6.QtCore import Qt

    area = Qt.DockWidgetArea
    w = make_window()
    sections, log, thermal = (w.panes.dock(p) for p in (w.sections_pane, w.log_pane, w.thermal_pane))
    w.addDockWidget(area.RightDockWidgetArea, sections)  # as dragged onto the right side
    log.close()
    thermal.setFloating(True)
    _settle(qapp)
    state = w.panes.save()
    old = w.saveState()  # version 0: the layout of the program up to 1.3.3 (two docks with tabs)
    w2 = make_window()
    assert w2.restore_layout(state)
    _settle(qapp)
    s2, l2, t2 = (w2.panes.dock(p) for p in (w2.sections_pane, w2.log_pane, w2.thermal_pane))
    assert w2.dockWidgetArea(s2) == area.RightDockWidgetArea and l2.isHidden() and t2.isFloating() and t2.isVisible()
    w3 = make_window()
    assert not w3.restore_layout(old) and not w3.restore_layout(None)
    _settle(qapp)
    assert all(not d.isHidden() and not d.isFloating() for d in w3.panes.docks)  # the default layout stays
    assert w3.dockWidgetArea(w3.panes.dock(w3.sections_pane)) == area.BottomDockWidgetArea
    # Vista > Pannelli > Ripristina: every pane back in its group, all open, the first ones in front
    w2._show_pane(w2.reference)
    w2.panes.reset_action.trigger()
    _settle(qapp)
    assert w2.dockWidgetArea(s2) == area.BottomDockWidgetArea and not l2.isHidden() and not t2.isFloating()
    assert w2.dockWidgetArea(t2) == area.RightDockWidgetArea  # the tools start on the right
    assert t2 in w2.tabifiedDockWidgets(w2.d_pa)
    assert _front(w2.panes.dock(w2.workspace)) and _front(w2.panes.dock(w2.photos_panel))
    # the window keeps its layout when it closes; the next one takes it back
    w.setWindowModified(False)
    w.close()
    w4 = make_window()
    assert w4.restore_layout(w.settings.value("state"))
    _settle(qapp)
    assert w4.dockWidgetArea(w4.panes.dock(w4.sections_pane)) == area.RightDockWidgetArea


def test_next_and_previous_pane(make_window, qapp):
    w = make_window()
    assert w.panes.next_action.shortcut().toString() == "F6"
    assert w.panes.previous_action.shortcut().toString() == "Shift+F6"
    w.activateWindow()
    w.workspace.setFocus()
    w.panes.next_action.trigger()  # the focus in Area di lavoro: Riferimenti
    _settle(qapp)
    assert _front(w.panes.dock(w.reference)) and w.panes.focused() is w.panes.dock(w.reference)
    w.panes.previous_action.trigger()
    w.panes.previous_action.trigger()  # round to the last pane tabbed with them
    _settle(qapp)
    assert _front(w.panes.dock(w.layers_pane))
    w._show_pane(w.console)  # the focus in the console: the bottom group
    w.console.inp.setFocus()
    w.panes.next_action.trigger()
    _settle(qapp)
    assert _front(w.panes.dock(w.log_pane)) and _front(w.panes.dock(w.layers_pane))


class _TerrainPane(QLabel):
    """A pane class of a plugin: its package names the dock, the same in every session."""


def test_plugins_written_for_the_tabs_of_1_3_3(make_window, qapp):
    """As the plugins released until 1.3.3 do (terrain, geophysics, geoarchaeology, sentinel2...)."""
    import shiboken6

    from pyarchinit_photogrammetry.gui.icons import icon

    w = make_window()
    pane = _TerrainPane("Terreno")
    tabs = w.left_tabs
    k = tabs.addTab(pane, icon("relief"), "Terreno")
    _settle(qapp)
    assert k == tabs.indexOf(pane) == tabs.count() - 1 and tabs.widget(k) is pane and tabs.tabText(k) == "Terreno"
    dock = w.panes.dock(pane)
    assert dock.objectName() == "pane_plugin_test_gui_panes"  # from the package of the pane: the same every time
    assert dock in w.tabifiedDockWidgets(w.d_pa)  # a plugin's pane starts on the right, beside the fixed ones
    assert "Terreno" in [a.text() for a in w.panes.menu.actions()]
    # how they show their pane: setCurrentWidget, then d_work.show() and d_work.raise_()
    tabs.setCurrentWidget(pane)
    w.d_work.show()
    w.d_work.raise_()
    _settle(qapp)
    # on the right in front, the Area di lavoro still in front on the left: both are seen
    assert _front(dock) and _front(w.panes.dock(w.workspace)) and tabs.currentWidget() is pane
    dock.close()  # closed by the user: shown again the same way
    _settle(qapp)
    tabs.setCurrentWidget(pane)
    w.d_work.show()
    w.d_work.raise_()
    _settle(qapp)
    assert _front(dock) and w.d_work.isVisible()
    plain = QLabel("Senza icona")
    assert tabs.addTab(plain, "Senza icona") == tabs.count() - 1 and tabs.tabIcon(tabs.indexOf(plain)).isNull()
    bottom = QLabel("In basso")
    assert w.bottom_tabs.indexOf(bottom) < 0
    w.bottom_tabs.addTab(bottom, "In basso")
    assert w.bottom_tabs.indexOf(bottom) >= 0 and w.bottom_tabs.tabText(w.bottom_tabs.indexOf(bottom)) == "In basso"
    # how they take it away in deactivate
    tabs.removeTab(tabs.indexOf(pane))
    _settle(qapp)
    assert tabs.indexOf(pane) < 0 and not shiboken6.isValid(dock) and pane.parent() is None
    assert "Terreno" not in [a.text() for a in w.panes.menu.actions()]


def test_a_plugin_pane_goes_back_where_it_was(make_window, qapp):
    """Moved by the user, saved with the window: a pane that comes after the window took its layout
    back (restoreDockWidget) goes there too."""
    from PySide6.QtCore import Qt

    w = make_window()
    pane = _TerrainPane("Terreno")
    w.left_tabs.addTab(pane, "Terreno")
    _settle(qapp)
    w.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, w.panes.dock(pane))
    _settle(qapp)
    state = w.panes.save()
    w2 = make_window()
    assert w2.restore_layout(state)
    again = _TerrainPane("Terreno")
    w2.left_tabs.addTab(again, "Terreno")
    _settle(qapp)
    assert w2.dockWidgetArea(w2.panes.dock(again)) == Qt.DockWidgetArea.BottomDockWidgetArea
    assert w2.left_tabs.indexOf(again) >= 0  # still a pane of its group for the plugin


def _move_to(w, anchor, dock) -> None:
    """As dragged by the user onto the tab group of ``anchor``."""
    w.removeDockWidget(dock)
    w.tabifyDockWidget(anchor, dock)
    dock.show()


def test_reset_puts_back_panes_that_came_later(make_window, qapp):
    w = make_window()
    floating, moved = _TerrainPane("Terreno"), _TerrainPane("Pianta")
    w.left_tabs.addTab(floating, "Terreno")  # after the window was built: not in its default layout
    w.left_tabs.addTab(moved, "Pianta")
    dock, other, console = w.panes.dock(floating), w.panes.dock(moved), w.panes.dock(w.console)
    photos = w.panes.dock(w.photos_panel)
    dock.setFloating(True)
    _move_to(w, photos, other)
    console.close()
    _settle(qapp)
    assert other in w.tabifiedDockWidgets(photos)
    w.panes.reset()
    _settle(qapp)
    workspace = w.panes.dock(w.workspace)
    assert not dock.isFloating() and dock in w.tabifiedDockWidgets(w.d_pa)
    assert other in w.tabifiedDockWidgets(w.d_pa) and other not in w.tabifiedDockWidgets(photos)
    assert not console.isHidden() and console in w.tabifiedDockWidgets(photos)
    assert _front(workspace) and _front(photos)


def test_context_add_pane_show_pane_and_release(make_window, qapp, tmp_path):
    import shiboken6
    from PySide6.QtWidgets import QDockWidget

    from pyarchinit_photogrammetry.gui import icons
    from pyarchinit_photogrammetry.plugins.api import Context

    w = make_window()
    context = Context("prova-pannelli", tmp_path / "ctx", w)
    first, second = QLabel("uno"), QLabel("due")
    dock = context.add_pane(first, "Pannello di prova", "plugin")
    dock2 = context.add_pane(second, "Secondo", area="bottom")
    _settle(qapp)
    assert isinstance(dock, QDockWidget) and dock.objectName() == "pane_plugin_prova-pannelli"
    assert dock2.objectName() == "pane_plugin_prova-pannelli_2"
    assert dock.features() & QDockWidget.DockWidgetFeature.DockWidgetClosable
    assert dock in w.tabifiedDockWidgets(w.d_pa)  # on the right by default
    assert dock2 in w.tabifiedDockWidgets(w.panes.dock(w.photos_panel))
    assert dock.windowIcon().isNull() != icons.available()
    assert {"Pannello di prova", "Secondo"} <= {a.text() for a in w.panes.menu.actions()}
    dock.close()
    _settle(qapp)
    context.show_pane(first)
    _settle(qapp)
    assert _front(dock) and w.panes.groups["right"].currentWidget() is first
    context.release()  # the plugin goes: its panes too
    _settle(qapp)
    assert not shiboken6.isValid(dock) and not shiboken6.isValid(dock2)
    assert w.left_tabs.indexOf(first) < 0 and w.bottom_tabs.indexOf(second) < 0
    command_line = Context("riga", tmp_path / "cli")  # no window: nothing to add
    assert command_line.add_pane(QLabel("x"), "x") is None
    command_line.show_pane(first)


def test_a_plugin_of_the_folder_with_both_kinds_of_pane(make_window, qapp, tmp_path, monkeypatch):
    """A plugin installed in the plugin folder, loaded by the window: a pane added the 1.3.3 way and
    one with ``add_pane``; disabled, both go."""
    folder = tmp_path / "plugins"
    module = "ppg_test_panes_plugin"
    dist = folder / "pannelli" / "pannelli-0.1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: pannelli\nVersion: 0.1.0\nSummary: panes\n")
    (dist / "entry_points.txt").write_text(f"[pyarchinit_photogrammetry.plugins]\npannelli = {module}:PanesPlugin\n")
    (dist / "WHEEL").write_text("Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
    (folder / "pannelli" / module).mkdir()
    (folder / "pannelli" / module / "__init__.py").write_text('''
from PySide6.QtWidgets import QLabel

from pyarchinit_photogrammetry.plugins import Plugin


class OldPane(QLabel):
    pass


class PanesPlugin(Plugin):
    title = "Pannelli di prova"

    def activate(self, context):
        self.context = context
        self.old, self.new = OldPane("vecchio"), QLabel("nuovo")
        context.window.left_tabs.addTab(self.old, "Vecchio modo")
        context.add_pane(self.new, "Nuovo modo", "plugin", area="right")

    def deactivate(self):
        tabs = self.context.window.left_tabs
        k = tabs.indexOf(self.old)
        if k >= 0:
            tabs.removeTab(k)
''')
    monkeypatch.setenv("PPG_PLUGINS_DIR", str(folder))
    w = make_window()
    plugin = w.plugins.get("pannelli").plugin
    assert plugin is not None
    old, new = w.panes.dock(plugin.old), w.panes.dock(plugin.new)
    assert old.objectName() == "pane_plugin_pannelli"  # the plugin's name, from the package of its pane
    assert new.objectName() == "pane_plugin_pannelli_2"
    assert new in w.tabifiedDockWidgets(w.d_pa)
    w.plugins.unload()
    _settle(qapp)
    assert w.left_tabs.indexOf(plugin.old) < 0 and w.panes.dock(plugin.new) is None
    assert {d.objectName() for d in w.panes.docks} == set(LEFT + RIGHT + BOTTOM)


def test_panes_in_english(make_window, qapp):
    from pyarchinit_photogrammetry.gui import i18n

    i18n.set_current("en")
    try:
        w = make_window()
        titles = {d.objectName(): d.windowTitle() for d in w.panes.docks}
        assert (titles["pane_workspace"], titles["pane_reference"], titles["pane_photos"]) == \
            ("Workspace", "Reference", "Photos")
        assert w.panes.menu.title() == "Panes" and w.panes.reset_action.text() == "Restore the layout of the panes"
        entries = {a.text() for a in w.panes.menu.actions()}
        assert {"Workspace", "Python console", "Next pane", "Previous pane"} <= entries
        bars = w.panes._tab_bars()
        assert "Workspace" in [b.tabText(i) for b in bars for i in range(b.count())]
    finally:
        i18n.set_current("it")
