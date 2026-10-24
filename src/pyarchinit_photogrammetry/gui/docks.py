"""The panes of the main window, each a dock of its own as in Metashape.

Every pane (Area di lavoro, Riferimenti, Layer, Foto, Console Python..., and those of the plugins)
is a :class:`PaneDock`: dragged by its tab (its group by the title bar) it goes to another side of
the window or onto another pane (they become tabs); the first button of its title bar, or a double
click on it, floats it as a window of its own, and a double click on the title of that window docks
it back. The *fixed* panes - Area di lavoro, Riferimenti, Layer - are always there; every other pane
can be closed and opened again from *Vista > Pannelli*, which also goes from pane to pane (F6,
Shift+F6) and puts back the default layout. The layout is saved with the window: :data:`STATE_VERSION`
keeps out the layout of the program up to 1.3.3, when the panes were the tabs of two docks.

The plugins released until 1.3.3 add their pane with ``window.left_tabs.addTab(pane, icon, title)``,
show it with ``window.left_tabs.setCurrentWidget(pane)`` and then ``window.d_work.show()`` and
``window.d_work.raise_()``, and take it away with ``removeTab(indexOf(pane))``: :class:`PaneGroup`
(``left_tabs``, ``bottom_tabs``) and :class:`GroupDock` (``d_work``, ``d_photos``) do the same with
the docks. Since 1.3.4 a plugin uses ``context.add_pane`` and ``context.show_pane``.
"""
from __future__ import annotations

import re

from PySide6.QtCore import QByteArray, QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (QAbstractButton, QApplication, QDockWidget, QFrame, QMainWindow, QMenu, QScrollArea,
                               QTabBar, QTabWidget, QWidget)

from .i18n import tr

STATE_VERSION = 2  # of saveState/restoreState; until 1.3.3 (0) the panes were the tabs of two docks
AREAS = {"left": Qt.DockWidgetArea.LeftDockWidgetArea, "bottom": Qt.DockWidgetArea.BottomDockWidgetArea,
         "right": Qt.DockWidgetArea.RightDockWidgetArea}
_FEATURE = QDockWidget.DockWidgetFeature
FIXED = _FEATURE.DockWidgetMovable | _FEATURE.DockWidgetFloatable  # moved and floated, never closed
CLOSABLE = FIXED | _FEATURE.DockWidgetClosable


def _pointer(obj) -> int:
    """The address of a Qt object: what the tab bars of the docks hold as the data of each tab."""
    try:
        import shiboken6

        return int(shiboken6.getCppPointer(obj)[0])
    except Exception:  # pragma: no cover - shiboken6 comes with PySide6
        return 0


def _icon(value) -> QIcon:
    if isinstance(value, QIcon):
        return value
    if isinstance(value, str) and value:
        from .icons import icon

        return icon(value)
    return QIcon()


class PaneScroll(QScrollArea):
    """The scroll area of a pane, with room above it for the pane's toolbar: the tools stay in sight
    while the pane scrolls, and the pane is still the widget of the area."""

    def __init__(self):
        super().__init__()
        self.toolbar: QWidget | None = None

    def set_toolbar(self, bar: QWidget) -> None:
        self.toolbar = bar
        bar.setParent(self)
        bar.show()
        self._place_toolbar()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt name
        super().resizeEvent(event)
        self._place_toolbar()

    def _place_toolbar(self) -> None:
        bar = self.toolbar
        if bar is None:
            return
        h = bar.sizeHint().height()
        if self.viewportMargins().top() != h:
            self.setViewportMargins(0, h, 0, 0)
        r = self.contentsRect()
        bar.setGeometry(r.left(), r.top(), r.width(), h)


class PaneDock(QDockWidget):
    """A pane: its widget in a scroll area (a pane taller than a laptop screen scrolls), the group
    of the side where it starts, fixed (always there) or not; a toolbar of the pane above it
    (:meth:`set_toolbar`), as Metashape's Workspace has."""

    def __init__(self, widget: QWidget, title: str, name: str, group: PaneGroup, parent: QMainWindow,
                 icon: QIcon | None = None, fixed: bool = False):
        super().__init__(title, parent)
        self.setObjectName(name)
        self.pane = widget
        self.group = group
        self.fixed = fixed
        self.setFeatures(FIXED if fixed else CLOSABLE)
        scroll = PaneScroll()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumSize(40, 40)  # the tabs are above the pane now: the window stays as small as before
        scroll.setWidget(widget)
        self.setWidget(scroll)
        self.set_icon(icon or QIcon())
        if fixed:
            self.toggleViewAction().setStatusTip(
                tr("Pannello sempre presente: trascina il titolo per spostarlo, doppio clic per staccarlo"))
        tips = {"qt_dockwidget_floatbutton": tr("Stacca il pannello in una finestra a sé, o riaggancialo"),
                "qt_dockwidget_closebutton": tr("Chiudi il pannello: Vista > Pannelli lo riapre")}
        for button, tip in tips.items():  # the buttons Qt puts in the title bar
            found = self.findChild(QAbstractButton, button)
            if found is not None:
                found.setToolTip(tip)

    def set_icon(self, icon: QIcon) -> None:
        """The icon of its tab (not of its entry in *Vista > Pannelli*: there the tick says open or closed,
        and a menu entry with an icon shows the icon instead of the tick)."""
        self.setWindowIcon(icon)

    def set_toolbar(self, bar: QWidget) -> None:
        """``bar`` (a QToolBar) above the pane, out of its scrolling part."""
        scroll = self.widget()
        if isinstance(scroll, PaneScroll):
            scroll.set_toolbar(bar)

    def take_pane(self) -> QWidget:
        """The widget out of the dock, without a parent: the dock can go, the widget is the caller's."""
        scroll = self.widget()
        taken = scroll.takeWidget() if isinstance(scroll, QScrollArea) else None
        return taken if taken is not None else self.pane


class PaneGroup:
    """The panes that start on one side of the window, in the order they came, with the methods of
    the QTabWidget they were in until 1.3.3 (plugins use ``addTab``, ``indexOf``, ``setCurrentWidget``,
    ``removeTab``): an index is the place in this list, a tab is a dock."""

    def __init__(self, panes: Panes, area: str):
        self._panes = panes
        self.area = area
        self.docks: list[PaneDock] = []
        self.current: PaneDock | None = None  # the pane last brought to the front

    def dock(self, widget) -> PaneDock | None:
        return next((d for d in self.docks if d.pane is widget or d is widget), None)

    def current_dock(self) -> PaneDock | None:
        """The pane of the group last in front (not closed), else the first open one."""
        if self.current in self.docks and not self.current.isHidden():
            return self.current
        return next((d for d in self.docks if not d.isHidden()), self.docks[0] if self.docks else None)

    def _at(self, index: int) -> PaneDock | None:
        return self.docks[index] if 0 <= index < len(self.docks) else None

    # the QTabWidget of the panes until 1.3.3
    def addTab(self, widget: QWidget, *args) -> int:  # noqa: N802 - Qt names, as plugins call them
        return self.insertTab(len(self.docks), widget, *args)

    def insertTab(self, index: int, widget: QWidget, *args) -> int:  # noqa: N802
        """``insertTab(index, widget, title)`` or ``insertTab(index, widget, icon, title)``."""
        icon, title = (args[0], args[1]) if len(args) >= 2 else (None, args[0] if args else "")
        # the panes plugins put among the left tabs until 1.3.3 start on the right, beside the fixed panes
        place = "right" if self.area == "left" else None
        dock = self._panes.add(widget, str(title), icon, area=self.area, index=index, place=place)
        return self.docks.index(dock)

    def removeTab(self, index: int) -> None:  # noqa: N802
        dock = self._at(index)
        if dock is not None:
            self._panes.remove(dock)

    def indexOf(self, widget) -> int:  # noqa: N802
        return next((i for i, d in enumerate(self.docks) if d.pane is widget), -1)

    def count(self) -> int:
        return len(self.docks)

    def widget(self, index: int) -> QWidget | None:
        dock = self._at(index)
        return dock.pane if dock is not None else None

    def currentWidget(self) -> QWidget | None:  # noqa: N802
        dock = self.current_dock()
        return dock.pane if dock is not None else None

    def currentIndex(self) -> int:  # noqa: N802
        dock = self.current_dock()
        return self.docks.index(dock) if dock is not None else -1

    def setCurrentWidget(self, widget: QWidget) -> None:  # noqa: N802
        """The pane in front, also when it was closed or floating."""
        self._panes.show(widget)

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        dock = self._at(index)
        if dock is not None:
            self._panes.show(dock)

    def tabText(self, index: int) -> str:  # noqa: N802
        dock = self._at(index)
        return dock.windowTitle() if dock is not None else ""

    def setTabText(self, index: int, text: str) -> None:  # noqa: N802
        dock = self._at(index)
        if dock is not None:
            dock.setWindowTitle(text)

    def tabIcon(self, index: int) -> QIcon:  # noqa: N802
        dock = self._at(index)
        return dock.windowIcon() if dock is not None else QIcon()

    def setTabIcon(self, index: int, icon: QIcon) -> None:  # noqa: N802
        dock = self._at(index)
        if dock is not None:
            dock.set_icon(_icon(icon))
            self._panes.decorate_later()

    def tabToolTip(self, index: int) -> str:  # noqa: N802
        dock = self._at(index)
        return dock.toolTip() if dock is not None else ""

    def setTabToolTip(self, index: int, tip: str) -> None:  # noqa: N802
        dock = self._at(index)
        if dock is not None:
            dock.setToolTip(tip)

    def isTabVisible(self, index: int) -> bool:  # noqa: N802
        dock = self._at(index)
        return dock is not None and not dock.isHidden()

    def setTabVisible(self, index: int, visible: bool) -> None:  # noqa: N802
        dock = self._at(index)
        if dock is not None:
            if visible:
                self._panes.show(dock)
            elif not dock.fixed:
                dock.close()


class GroupDock:
    """``window.d_work`` and ``window.d_photos``: until 1.3.3 the docks of the left and of the bottom
    panes, now the dock of the pane in front in that group - after ``left_tabs.setCurrentWidget(pane)``,
    the pane of a plugin - and what is asked of them is done to it."""

    def __init__(self, group: PaneGroup):
        self._group = group

    def dock(self) -> PaneDock | None:
        return self._group.current_dock()

    def show(self) -> None:
        dock = self.dock()
        if dock is not None:
            self._group._panes.show(dock)

    def raise_(self) -> None:
        self.show()

    def close(self) -> bool:
        dock = self.dock()
        return bool(dock is not None and not dock.fixed and dock.close())

    def hide(self) -> None:
        self.close()

    def setVisible(self, visible: bool) -> None:  # noqa: N802
        self.show() if visible else self.close()

    def isVisible(self) -> bool:  # noqa: N802
        dock = self.dock()
        return bool(dock is not None and dock.isVisible())

    def __getattr__(self, name):
        dock = self.dock()
        if dock is None:
            raise AttributeError(name)
        return getattr(dock, name)


class Panes(QObject):
    """The panes of a main window: adding and removing them, bringing one to the front, the menu
    *Vista > Pannelli*, the default layout and the one saved with the window."""

    def __init__(self, window: QMainWindow):
        super().__init__(window)
        self.window = window
        self.groups = {area: PaneGroup(self, area) for area in AREAS}
        self.menu: QMenu | None = None
        self._default: QByteArray | None = None  # the layout the window starts with
        self._default_names: set[str] = set()
        self._restored = False  # the layout of the last session was put back
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self.decorate)
        options = QMainWindow.DockOption
        window.setDockOptions(options.AnimatedDocks | options.AllowTabbedDocks | options.AllowNestedDocks
                              | options.GroupedDragging)
        window.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)
        self.next_action = QAction(tr("Pannello successivo"), window)
        self.next_action.setShortcut(QKeySequence("F6"))
        self.next_action.setStatusTip(tr("Il pannello dopo quello in uso, fra quelli raggruppati con lui"))
        self.next_action.triggered.connect(lambda _=False: self.cycle(1))
        self.previous_action = QAction(tr("Pannello precedente"), window)
        self.previous_action.setShortcut(QKeySequence("Shift+F6"))
        self.previous_action.setStatusTip(tr("Il pannello prima di quello in uso, fra quelli raggruppati con lui"))
        self.previous_action.triggered.connect(lambda _=False: self.cycle(-1))
        self.reset_action = QAction(tr("Ripristina la disposizione dei pannelli"), window)
        self.reset_action.setStatusTip(tr("Ogni pannello al suo posto iniziale, tutti aperti"))
        self.reset_action.triggered.connect(lambda _=False: self.reset())
        window.installEventFilter(self)

    # ------------------------------------------------------------------ the panes
    @property
    def docks(self) -> list[PaneDock]:
        return [d for group in self.groups.values() for d in group.docks]

    def dock(self, widget) -> PaneDock | None:
        """The dock of a pane (its widget or the dock itself)."""
        if isinstance(widget, PaneDock) and widget in self.docks:
            return widget
        return next((d for d in self.docks if d.pane is widget), None)

    def add(self, widget: QWidget, title: str, icon=None, *, name: str | None = None, area: str = "left",
            fixed: bool = False, index: int | None = None, place: str | None = None) -> PaneDock:
        """``widget`` as a pane of ``area`` ("left", "bottom", "right"), tabbed with the panes there
        or, after the window took back the layout of the last session, where the user left it."""
        if area not in self.groups or (place is not None and place not in self.groups):
            raise ValueError(f"area {area!r}, place {place!r}: one of {', '.join(AREAS)}")
        found = self.dock(widget)
        if found is not None:
            return found
        group = self.groups[area]
        dock = PaneDock(widget, title, self._unique(name or self._plugin_name(widget, title)), group, self.window,
                        _icon(icon), fixed)
        dock.place = place or area  # the side it starts on (a pane of the left group may start on the right)
        index = len(group.docks) if index is None else max(0, min(int(index), len(group.docks)))
        group.docks.insert(index, dock)
        dock.visibilityChanged.connect(lambda visible, d=dock: self._seen(d, visible))
        dock.dockLocationChanged.connect(lambda _area: self.decorate_later())
        dock.topLevelChanged.connect(lambda _floating: self.decorate_later())
        dock.toggleViewAction().triggered.connect(lambda checked, d=dock: self.show(d) if checked else None)
        if not (self._restored and self.window.restoreDockWidget(dock)):
            self._place(dock)
        self._rebuild_menu()
        self.decorate_later()
        return dock

    def remove(self, widget) -> QWidget | None:
        """The pane out of the window: its dock goes, its widget (returned, without a parent) stays
        with whoever holds it, as the page of a QTabWidget after ``removeTab``."""
        dock = self.dock(widget)
        if dock is None:
            return None
        dock.group.docks.remove(dock)
        if dock.group.current is dock:
            dock.group.current = None
        self.window.removeDockWidget(dock)
        pane = dock.take_pane()
        self._rebuild_menu()
        dock.deleteLater()
        self.decorate_later()
        return pane

    def show(self, widget) -> PaneDock | None:
        """The pane in front, also when it was closed (it comes back where it was) or floating."""
        dock = self.dock(widget)
        if dock is None:
            return None
        if dock.isHidden():
            dock.show()
            layout = dock.parentWidget().layout()
            if layout is not None:  # its tab back in the bar now, so that raise_() finds it there
                layout.activate()
        holder = dock.window()  # the main window, the dock itself when floating, a floating group of panes
        if holder is not self.window and holder.isHidden():
            holder.show()
        dock.raise_()
        if dock.parentWidget() is not self.window:  # in a floating group: raise_() does not pick the tab there
            self._select_tab(dock)
        if holder is not self.window:
            holder.raise_()
            holder.activateWindow()
        dock.group.current = dock
        return dock

    def front(self) -> None:
        """The first pane of each group in front: Area di lavoro, Foto, PyArchInit."""
        for group in self.groups.values():
            if group.docks:
                group.docks[0].raise_()
                group.current = group.docks[0]

    def _place(self, dock: PaneDock) -> None:
        """On the side it starts on: tabbed with a pane of its group docked there, else with any pane
        docked there, else alone on that side."""
        area = AREAS[getattr(dock, "place", dock.group.area)]
        there = [d for d in self.docks if d is not dock and not d.isFloating() and self.window.dockWidgetArea(d) == area]
        others = [d for d in there if d.group is dock.group] or there
        anchor = next((d for d in others if not d.isHidden()), others[0] if others else None)
        reopen = False
        if self.window.dockWidgetArea(dock) != Qt.DockWidgetArea.NoDockWidgetArea:  # in the window already
            reopen = not dock.isHidden()
            self.window.removeDockWidget(dock)  # out of where it is: Qt does not tabify a pane tabbed elsewhere
        if anchor is None:
            self.window.addDockWidget(area, dock)
        else:
            self.window.tabifyDockWidget(anchor, dock)
        if dock.isFloating():
            dock.setFloating(False)
        if reopen:
            dock.show()

    def _seen(self, dock: PaneDock, visible: bool) -> None:
        if visible:
            dock.group.current = dock
        self.decorate_later()

    def _plugin_name(self, widget: QWidget, title: str) -> str:
        """The object name of a pane added by a plugin with ``left_tabs.addTab``: from the package of
        its class (the plugin's name when the plugin is known), so the layout finds it again."""
        module = (type(widget).__module__ or "").split(".")[0]
        manager = getattr(self.window, "plugins", None)
        for info in getattr(manager, "plugins", None) or []:
            value = getattr(getattr(info, "entry_point", None), "value", "") or ""
            if module and value.split(":")[0].split(".")[0] == module:
                return f"pane_plugin_{info.name}"
        base = title if not module or module.startswith(("PySide", "shiboken", "builtins")) else module
        return "pane_plugin_" + (re.sub(r"\W+", "_", str(base)).strip("_").lower() or "pane")

    def _unique(self, name: str) -> str:
        taken = {d.objectName() for d in self.docks}
        if name not in taken:
            return name
        k = 2
        while f"{name}_{k}" in taken:
            k += 1
        return f"{name}_{k}"

    # ------------------------------------------------------------------ layout
    def keep_default(self) -> None:
        """The layout as it is now (the window just built) is the one *Ripristina* puts back."""
        self.front()
        self._default = self.window.saveState(STATE_VERSION)
        self._default_names = {d.objectName() for d in self.docks}

    def save(self) -> QByteArray:
        return self.window.saveState(STATE_VERSION)

    def restore(self, state) -> bool:
        """The layout saved at the end of the last session; False for one of another version (the
        program up to 1.3.3 had two docks with tabs) or none: the default layout stays."""
        ok = False
        if state:
            try:
                ok = bool(self.window.restoreState(state, STATE_VERSION))
            except (TypeError, ValueError):
                ok = False
        self._restored = ok
        for dock in self.docks:
            if dock.fixed and dock.isHidden():  # never closed, whatever the saved layout says
                dock.show()
        self.decorate_later()
        return ok

    def reset(self) -> None:
        """Every pane back in its group, all open, Area di lavoro and Foto in front."""
        if self._default is not None:
            self.window.restoreState(self._default, STATE_VERSION)
        for dock in self.docks:
            if self._default is None or dock.objectName() not in self._default_names:
                self._place(dock)
            if dock.isHidden():
                dock.show()
        self.front()
        self.decorate_later()

    # ------------------------------------------------------------------ menu and navigation
    def build_menu(self, parent: QMenu) -> QMenu:
        """*Pannelli* in ``parent``: a check for each pane (the fixed ones checked and greyed), the next
        and the previous pane, the default layout."""
        from .icons import icon

        self.menu = parent.addMenu(icon("panes"), tr("Pannelli"))
        self.reset_action.setIcon(icon("reset_layout"))
        self._rebuild_menu()
        return self.menu

    def _rebuild_menu(self) -> None:
        menu = self.menu
        if menu is None:
            return
        menu.clear()
        for group in self.groups.values():
            if group.docks and menu.actions():
                menu.addSeparator()
            for dock in group.docks:
                menu.addAction(dock.toggleViewAction())
        menu.addSeparator()
        menu.addAction(self.next_action)
        menu.addAction(self.previous_action)
        menu.addSeparator()
        menu.addAction(self.reset_action)

    def focused(self) -> PaneDock | None:
        """The pane holding the keyboard focus (in the window, or in a pane floating on its own)."""
        w = QApplication.focusWidget() or self.window.focusWidget()
        while w is not None and not isinstance(w, PaneDock):
            w = w.parentWidget()
        return w if w is not None and w in self.docks else None

    def tab_order(self, dock: PaneDock) -> list[PaneDock]:
        """The open panes tabbed with ``dock`` (``dock`` too) in the order of their tabs."""
        members = [dock] + [d for d in self.window.tabifiedDockWidgets(dock) if d in self.docks]
        if len(members) < 2:
            return members
        wanted = {_pointer(d): d for d in members}
        for bar in self._tab_bars():
            order = [bar.tabData(i) for i in range(bar.count())]
            if sorted(order) == sorted(wanted):
                return [wanted[p] for p in order]
        return sorted(members, key=self.docks.index)

    def cycle(self, step: int) -> PaneDock | None:
        """The next (``step`` 1) or previous (-1) pane of the group the focus is in (else of the left
        group), in front and with the focus."""
        dock = self.focused() or self.groups["left"].current_dock()
        if dock is None:
            return None
        order = self.tab_order(dock)
        if len(order) < 2:
            return None
        target = order[(order.index(dock) + step) % len(order)]
        self.show(target)
        for w in [target.pane] + target.pane.findChildren(QWidget):
            if w.focusPolicy().value & Qt.FocusPolicy.TabFocus.value and w.isEnabled() and w.isVisibleTo(target):
                w.setFocus(Qt.FocusReason.OtherFocusReason)
                break
        else:
            target.widget().setFocus(Qt.FocusReason.OtherFocusReason)
        return target

    # ------------------------------------------------------------------ tabs
    def _tab_bars(self) -> list[QTabBar]:
        """The tab bars Qt makes for the groups of panes, in the window and in its floating groups."""
        holders = [self.window] + [c for c in self.window.children() if isinstance(c, QWidget)
                                   and c.metaObject().className() == "QDockWidgetGroupWindow"]
        return [c for h in holders for c in h.children()
                if isinstance(c, QTabBar) and c.metaObject().className() == "QMainWindowTabBar"]

    def _select_tab(self, dock: PaneDock) -> None:
        pointer = _pointer(dock)
        for bar in self._tab_bars():
            for i in range(bar.count()):
                if bar.tabData(i) == pointer and bar.currentIndex() != i:
                    bar.setCurrentIndex(i)

    def decorate_later(self) -> None:
        try:
            self._timer.start()
        except RuntimeError:  # the window is going away
            pass

    def decorate(self) -> None:
        """The icon of each pane on its tab (Qt writes only the title there, on tabs it makes anew
        whenever the panes move)."""
        icons = {_pointer(d): d.windowIcon() for d in self.docks}
        for bar in self._tab_bars():
            for i in range(bar.count()):
                ic = icons.get(bar.tabData(i))
                if ic is not None and not ic.isNull() and bar.tabIcon(i).cacheKey() != ic.cacheKey():
                    bar.setTabIcon(i, ic)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt name
        if obj is self.window and event.type() in (QEvent.Type.ChildAdded, QEvent.Type.LayoutRequest):
            self.decorate_later()  # a tab bar made, the panes laid out again
        return False


__all__ = ["STATE_VERSION", "AREAS", "PaneScroll", "PaneDock", "PaneGroup", "GroupDock", "Panes"]
