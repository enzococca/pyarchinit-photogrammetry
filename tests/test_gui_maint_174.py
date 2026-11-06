"""Points left open by 1.7.3, closed in 1.7.4: *Pubblica* (the drawings to PyArchInit) in the menu PyArchInit, so the
search of the commands finds the action of the rail; no stray tab bar after a pane is added, shown or closed while the
program runs; the press that closes the flyout of a rail swallows its own release only."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

RIGHT = ("pane_pyarchinit", "pane_proxies", "pane_multispectral", "pane_thermal", "pane_gnss")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _settle(qapp) -> None:
    from PySide6.QtCore import QCoreApplication, QEvent

    for _ in range(4):
        qapp.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def window(qapp, tmp_path):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    w = MainWindow(ppg.Document(), autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.resize(1000, 700)
    w.show()
    _settle(qapp)
    yield w
    w.setWindowModified(False)
    w.close()


def _stray(w) -> list:
    """The visible tab bars of the window beyond one per group of two or more panes docked together."""
    from PySide6.QtWidgets import QTabBar

    docked = [d for d in w.panes.docks if not d.isHidden() and not d.isFloating()]
    groups, seen = 0, set()
    for d in docked:
        if d in seen:
            continue
        together = {d, *[t for t in w.tabifiedDockWidgets(d) if t in docked and w.dockWidgetArea(t) == w.dockWidgetArea(d)]}
        seen |= together
        groups += len(together) > 1
    bars = [b for b in w.findChildren(QTabBar) if b.parent() is w and b.metaObject().className() == "QMainWindowTabBar"
            and b.isVisible() and b.geometry().intersects(w.rect())]  # Qt moves the bars it no longer uses outside
    return [[b.tabText(i) for i in range(b.count())] for b in bars] if len(bars) != groups else []


def test_publish_drawings_is_in_the_pyarchinit_menu_and_the_search_finds_it(window, qapp):
    w = window
    menu = next(a.menu() for a in w.menuBar().actions() if a.text().replace("&", "") == "PyArchInit")
    assert w.a_publish_drawings in menu.actions()
    texts = [a.text() for a in menu.actions()]
    assert texts.count("Pubblica unità e quote disegnate") == 1  # the action of the rail, not a second one
    assert w.a_publish_drawings.text() == "Pubblica unità e quote disegnate"
    assert w.a_publish_drawings.iconText() == "Pubblica"  # the short label of the classic drawing bar
    search = w.command_search
    search.refresh()
    found = search.matches("pubblica unità e quote")
    assert found and search.commands[found[0]] is w.a_publish_drawings and found[0].endswith("PyArchInit")


def test_no_stray_tab_bar_after_panes_change_while_the_program_runs(window, qapp):
    from PySide6.QtWidgets import QLabel

    w = window
    assert not _stray(w)
    for d in w.panes.docks:  # every tool of the right closed: the bar of their group goes with them
        if d.objectName() in RIGHT:
            d.close()
    _settle(qapp)
    assert not _stray(w)
    pane = QLabel("prova")
    w.add_pane(pane, "Prova", "plugin", area="right", name="pane_plugin_prova")  # alone on the right
    _settle(qapp)
    assert not _stray(w)
    w.panes.show(w.pa_pane)  # a second pane on the right, tabbed with it
    _settle(qapp)
    assert not _stray(w)
    w.panes.dock(pane).close()
    w.panes.show(pane)
    _settle(qapp)
    assert not _stray(w)
    w.panes.remove(pane)
    _settle(qapp)
    assert not _stray(w)


def test_a_stray_tab_bar_on_the_window_is_cleaned_when_the_panes_change(window, qapp):
    """The clean-up of the start (``Panes.tidy``, run after a pane is added, shown, closed or moved): a tab bar no group
    uses, shown on the window, goes at once and the layout stays as it was. The stray is made here from the empty bar
    Qt keeps outside the window once every pane of an area is closed: no sequence of panes added, shown, closed, moved
    or removed while the program runs was found that leaves a real one (see design/1.7.4-ledger/T123-report.md)."""
    w = window
    for d in w.panes.docks:
        if d.objectName() in RIGHT:
            d.close()
    _settle(qapp)
    left = [b for b in w.panes._window_tab_bars() if b.isVisible() and not b.geometry().intersects(w.rect())]
    assert left, "Qt keeps the bar of the closed area outside the window"
    left[0].move(0, 300)  # as the second row of Foto | Sezioni of 1.7.3: on the window, used by no group
    assert _stray(w) and w.panes.stray_tab_bars()
    open_before = sorted(d.objectName() for d in w.panes.docks if not d.isHidden())
    assert w.panes.tidy() is True
    assert not _stray(w)  # at once, not at the next layout of the window
    w.panes.show(w.console)  # any pane shown (added, closed, moved): the bars are looked at again
    _settle(qapp)
    assert not _stray(w)
    assert sorted(d.objectName() for d in w.panes.docks if not d.isHidden()) == open_before
    assert w.panes.tidy() is False  # nothing left to clean


def test_the_release_of_a_press_that_closed_the_flyout_is_swallowed_once(window, qapp):
    """The press on the view that closes the flyout goes no further and nor does its release; a release that never
    comes back to the same widget (the mouse let go elsewhere) does not eat a later release, and the release that
    ends a double click whose first press was swallowed is swallowed too."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QWidget

    w = window
    w.show_view(w.ortho_view)
    rail = w.raster_rail
    rail.set_pinned(False)
    vp = w.ortho_view.viewport()
    seen: list = []
    from PySide6.QtCore import QObject

    class Recorder(QObject):
        def eventFilter(self, obj, ev):  # noqa: N802
            if ev.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                             QEvent.Type.MouseButtonDblClick):
                seen.append(ev.type())
            return False

    recorder = Recorder(vp)
    vp.installEventFilter(recorder)
    rail.watch(vp)  # the rail's filter in front of the recorder, as in the program

    def send(kind, target=vp):
        buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
        pos = QPointF(50, 50)
        QApplication.sendEvent(target, QMouseEvent(kind, pos, target.mapToGlobal(pos), Qt.MouseButton.LeftButton,
                                                   buttons, Qt.KeyboardModifier.NoModifier))

    P, R, D = QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick
    # 1. press closes the flyout: press and its release swallowed, the next click passes
    rail.open("draw")
    send(P), send(R)
    assert not rail.is_open() and seen == []
    send(P), send(R)
    assert seen == [P, R]
    # 2. the release never comes back (let go over another widget): a later click is whole
    seen.clear()
    rail.open("draw")
    send(P)
    assert not rail.is_open() and seen == []
    other = QWidget()
    send(R, other)  # the release went elsewhere
    send(P), send(R)
    assert seen == [P, R]
    # 3. a double click whose first press closed the flyout, as Qt 6 sends it (press, release, press, double click,
    # release): the second click reaches the view as a single click, never as a double click
    seen.clear()
    rail.open("draw")
    send(P), send(R), send(P), send(D), send(R)
    assert seen == [P, R]
    # 4. the same as Qt 5 sends it (press, release, double click, release): the whole gesture is swallowed
    seen.clear()
    rail.open("draw")
    send(P), send(R), send(D), send(R)
    assert seen == []
    send(P), send(R)  # and the next click passes
    assert seen == [P, R]
    vp.removeEventFilter(recorder)
