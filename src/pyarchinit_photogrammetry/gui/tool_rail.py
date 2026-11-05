"""A narrow column of tool groups beside a view, each opening a flyout with its tools.

The 2D and the 3D views keep their tools here instead of rows of buttons above them: the rail
shows one icon per group (Naviga, Disegno, Misure...); a click opens the flyout of the group
beside the rail, inside the view area, with the tools as buttons with icon and text. The
tools are the program's own :class:`QAction` objects (checked state, menus, enabled state and
shortcuts as they are) or existing widgets moved in (the buttons of the 3D view keep their
names and signals).

A click on the same group, Esc or a click on the view closes the flyout, unless it is pinned
(the pin of its header, setting ``rail/pinned``). A right click on the rail chooses icons
only or icons and text (setting ``rail/text``). Two ways of placing it:

* :meth:`ToolRail.attach_to_view` - beside the viewport of a scroll area (the raster view):
  the rail in the left viewport margin, the flyout floating over the viewport;
* :meth:`ToolRail.place_in` - in a horizontal layout (the 3D view): rail, flyout and the view
  side by side. The 3D canvas may be a native OpenGL window, which nothing can float over.
"""
from __future__ import annotations

import weakref

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractScrollArea, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea,
                               QSizePolicy, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from .i18n import tr
from .icons import icon

_RAILS: "weakref.WeakSet[ToolRail]" = weakref.WeakSet()  # a choice of the context menu or the pin goes to all


def _settings():
    from . import app_settings

    return app_settings()


def _flag(key: str, default: bool) -> bool:
    try:
        return _settings().value(key, "true" if default else "false") in ("true", True, 1, "1")
    except Exception:  # noqa: BLE001 - preferences that cannot be read: the defaults
        return default


class ActionButton(QToolButton):
    """A button of the flyout for a :class:`QAction`, with its own (longer) text: it follows the
    action's icon, tooltip, enabled, visible and checked state, a click triggers the action, a
    menu of the action (or the one given) opens from the arrow beside it. ``setDefaultAction``
    would put back the action's short icon text at every change of its state."""

    def __init__(self, action: QAction, text: str | None = None, menu: QMenu | None = None, parent=None):
        super().__init__(parent)
        self.action = action
        self._text = text
        self.setObjectName("railItem")
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setAutoRaise(True)
        self.setIconSize(QSize(20, 20))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        menu = menu or action.menu()
        if menu is not None:
            self.setMenu(menu)
            self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.clicked.connect(lambda _=False: self.action.trigger())
        action.changed.connect(self._sync)
        self._sync()

    def nextCheckState(self) -> None:  # the action decides: a click triggers it, its state comes back
        pass

    def _sync(self) -> None:
        a = self.action
        self.setIcon(a.icon())
        self.setText(self._text or a.iconText())
        self.setToolTip(a.toolTip() or a.text().replace("&", ""))
        self.setStatusTip(a.statusTip())
        self.setEnabled(a.isEnabled())
        if self.parentWidget() is not None:  # a widget without a parent shown would be a window of its own
            self.setVisible(a.isVisible())
        elif not a.isVisible():
            self.hide()
        self.setCheckable(a.isCheckable())
        self.setChecked(a.isCheckable() and a.isChecked())


class RailGroup:
    """A group of the rail: its button on the rail and the page of its tools in the flyout."""

    def __init__(self, key: str, title: str, button: QToolButton, page: QWidget):
        self.key, self.title, self.button, self.page = key, title, button, page
        self.actions: list[QAction] = []  # the actions of its buttons (not those of their menus)
        self.widgets: list[QWidget] = []  # the widgets of its page, buttons for actions included


class RailFlyout(QFrame):
    """The panel of the tools of the group open: a header (name of the group, pin) and the tools."""

    def __init__(self, rail: "ToolRail"):
        super().__init__(None)
        self.setObjectName("railFlyout")
        self.setFrameShape(QFrame.Shape.NoFrame)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 6, 8)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(4)
        self.title = QLabel("")
        self.title.setObjectName("railTitle")
        head.addWidget(self.title, 1)
        self.pin = QToolButton()
        self.pin.setObjectName("railPin")
        self.pin.setAutoRaise(True)
        self.pin.setCheckable(True)
        self.pin.setIconSize(QSize(16, 16))
        head.addWidget(self.pin)
        lay.addLayout(head)
        self.pages = QStackedWidget()
        self.scroll = QScrollArea()
        self.scroll.setObjectName("railScroll")
        self.scroll.setWidget(self.pages)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)  # a narrow view: the arrows too
        lay.addWidget(self.scroll, 1)
        self._esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._esc.activated.connect(lambda: rail.close(force=False))

    def wanted_size(self) -> QSize:
        """The size of the header plus the current page with no scrolling."""
        page = self.pages.currentWidget()
        inner = page.sizeHint() if page is not None else QSize(160, 40)
        m = self.layout().contentsMargins()
        head = self.layout().itemAt(0).sizeHint()
        bar = self.scroll.verticalScrollBar().sizeHint().width()
        # the stack of the pages is as wide as its widest page: the flyout too, the same for every group
        widest = max(inner.width(), self.pages.minimumSizeHint().width(), self.pages.sizeHint().width())
        width = max(widest + bar, head.width(), 170) + m.left() + m.right() + 4  # + the border of the frame
        height = inner.height() + head.height() + self.layout().spacing() + m.top() + m.bottom() + 2
        return QSize(width, height)


class ToolRail(QWidget):
    """The rail: a column of group buttons and the flyout of the group open (see the module)."""

    opened = Signal(str)  # the key of the group shown in the flyout
    closed = Signal()
    WIDTH_ICONS = 40

    def __init__(self, name: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("toolRail")
        self.setProperty("railName", name)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.groups: dict[str, RailGroup] = {}
        self._current: str | None = None
        self._view: QAbstractScrollArea | None = None  # attach_to_view: the scroll area whose margin holds the rail
        self._watched: list = []
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(3, 4, 3, 4)
        self._lay.setSpacing(2)
        self._lay.addStretch(1)
        self.flyout = RailFlyout(self)
        self.flyout.hide()
        self.flyout.pin.toggled.connect(self.set_pinned)
        self._pinned = _flag("rail/pinned", False)
        self._text = _flag("rail/text", False)
        self._apply_pin()
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        _RAILS.add(self)
        self._apply_text()

    # ----------------------------------------------------------------- building
    def add_group(self, key: str, title: str, icon_name: str, items: list, tip: str = "") -> RailGroup:
        """A group: ``items`` are :class:`QAction` (a button with the action's icon text), ``(action,
        text)`` or ``(action, text, menu)`` (the button's own text and a menu beside it), widgets
        (moved into the flyout as they are) or ``None`` (a separator line)."""
        button = QToolButton()
        button.setObjectName("railGroup")
        button.setProperty("groupKey", key)
        button.setIcon(icon(icon_name))
        button.setIconSize(QSize(22, 22))
        button.setText(title)
        button.setToolTip(tip or title)
        button.setCheckable(True)
        button.setAutoRaise(True)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setProperty("active", False)
        button.clicked.connect(lambda _=False, k=key: self.toggle(k))
        self._lay.insertWidget(self._lay.count() - 1, button)
        page = QWidget()
        page.setObjectName("railPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        group = RailGroup(key, title, button, page)
        for item in items:
            self._add_item(group, lay, item)
        lay.addStretch(1)
        self.flyout.pages.addWidget(page)
        self.groups[key] = group
        self._apply_text()
        return group

    def _add_item(self, group: RailGroup, lay, item) -> None:
        if item is None:
            line = QFrame()
            line.setObjectName("railSeparator")
            line.setFrameShape(QFrame.Shape.HLine)
            line.setFixedHeight(1)
            lay.addWidget(line)
            return
        if isinstance(item, QAction) or (isinstance(item, tuple) and isinstance(item[0], QAction)):
            action, text, menu = (item, None, None) if isinstance(item, QAction) else (tuple(item) + (None, None))[:3]
            widget = ActionButton(action, text, menu)
            group_ = action.actionGroup()
            if not action.isCheckable() or (group_ is not None and group_.isExclusive()):
                widget.clicked.connect(self._used)  # a tool or a command; an option (Snap) leaves the flyout open
            group.actions.append(action)
            if widget.menu() is not None:
                widget.menu().triggered.connect(self._used)
        else:
            widget = item
            if isinstance(widget, QPushButton):
                widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                widget.setObjectName(widget.objectName() or "railItem")
                widget.clicked.connect(self._used)
                if widget.menu() is not None:
                    widget.menu().triggered.connect(self._used)
        group.widgets.append(widget)
        lay.addWidget(widget)

    # ----------------------------------------------------------------- open / close
    def current(self) -> str | None:
        """The key of the group open in the flyout, None when it is closed."""
        return self._current if self.is_open() else None

    def is_open(self) -> bool:
        return self._current is not None and not self.flyout.isHidden()

    def toggle(self, key: str) -> None:
        if self.is_open() and self._current == key:
            self.close(force=True)
        else:
            self.open(key)

    def open(self, key: str) -> None:
        group = self.groups[key]
        self._current = key
        self.flyout.pages.setCurrentWidget(group.page)
        self.flyout.title.setText(group.title)
        for k, g in self.groups.items():
            g.button.setChecked(k == key)
        self.flyout.show()
        self._place()
        self.flyout.raise_()
        self.opened.emit(key)

    def close(self, force: bool = True) -> bool:
        """Close the flyout. ``force`` False (Esc, a click on the view, a tool chosen): only when
        it is not pinned. Returns whether it was open and is closed now."""
        if not self.is_open() or (self._pinned and not force):
            return False
        self.flyout.hide()
        self._current = None
        for g in self.groups.values():
            g.button.setChecked(False)
        self._place()
        self.closed.emit()
        return True

    def _used(self, *_args) -> None:
        """A tool chosen in the flyout: the view is free again (unless pinned)."""
        self.close(force=False)

    # ----------------------------------------------------------------- pin and texts
    @property
    def pinned(self) -> bool:
        return self._pinned

    def set_pinned(self, on: bool) -> None:
        on = bool(on)
        try:
            _settings().setValue("rail/pinned", "true" if on else "false")
        except Exception:  # noqa: BLE001
            pass
        for rail in list(_RAILS):
            rail._pinned = on
            rail._apply_pin()

    def _apply_pin(self) -> None:
        pin = self.flyout.pin
        pin.blockSignals(True)
        pin.setChecked(self._pinned)
        pin.blockSignals(False)
        pin.setIcon(icon("pin" if self._pinned else "pin_off"))
        pin.setToolTip(tr("Fissato: resta aperto finché non lo chiudi") if self._pinned else
                       tr("Fissa il pannello: resta aperto mentre lavori sulla vista"))

    @property
    def with_text(self) -> bool:
        return self._text

    def set_text(self, on: bool) -> None:
        """Icons only (False) or icons with the names of the groups under them (True), on every rail."""
        on = bool(on)
        try:
            _settings().setValue("rail/text", "true" if on else "false")
        except Exception:  # noqa: BLE001
            pass
        for rail in list(_RAILS):
            rail._text = on
            rail._apply_text()

    def _apply_text(self) -> None:
        style = (Qt.ToolButtonStyle.ToolButtonTextUnderIcon if self._text else Qt.ToolButtonStyle.ToolButtonIconOnly)
        width = self.WIDTH_ICONS
        for g in self.groups.values():
            g.button.setToolButtonStyle(style)
            width = max(width, g.button.sizeHint().width() + 6)
        self.setFixedWidth(width)
        self._place()

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        icons_only = menu.addAction(tr("Solo icone"))
        with_text = menu.addAction(tr("Icone e testo"))
        for a, on in ((icons_only, not self._text), (with_text, self._text)):
            a.setCheckable(True)
            a.setChecked(on)
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is icons_only:
            self.set_text(False)
        elif chosen is with_text:
            self.set_text(True)

    # ----------------------------------------------------------------- active tool
    def mark_active(self, action: QAction | None) -> None:
        """Highlight the group whose tools (or the menus of its tools) hold ``action``: the tool in use."""
        for g in self.groups.values():
            on = action is not None and action in self.actions_of(g.key)
            if g.button.property("active") != on:
                g.button.setProperty("active", on)
                g.button.style().unpolish(g.button)
                g.button.style().polish(g.button)

    def active_group(self) -> str | None:
        return next((k for k, g in self.groups.items() if g.button.property("active")), None)

    def actions_of(self, key: str) -> list[QAction]:
        """Every action reachable in the group: its buttons' actions, those of their menus (and
        submenus), of the menus of its widgets."""
        group = self.groups[key]
        seen: list[QAction] = []

        def walk_menu(menu: QMenu | None) -> None:
            if menu is None:
                return
            for a in menu.actions():
                if a.isSeparator():
                    continue
                if a.menu() is not None and a.menu() is not menu:
                    walk_menu(a.menu())
                if a not in seen:
                    seen.append(a)

        for a in group.actions:
            if a not in seen:
                seen.append(a)
            walk_menu(a.menu())
        for w in group.widgets:
            if hasattr(w, "menu") and callable(w.menu):
                walk_menu(w.menu())
        return seen

    def all_actions(self) -> list[QAction]:
        out: list[QAction] = []
        for k in self.groups:
            out += [a for a in self.actions_of(k) if a not in out]
        return out

    # ----------------------------------------------------------------- placing
    def attach_to_view(self, view: QAbstractScrollArea) -> None:
        """The rail in the left viewport margin of ``view``, the flyout floating over its viewport."""
        self._view = view
        self.setParent(view)
        self.flyout.setParent(view)
        self.flyout.hide()
        view.installEventFilter(self._filter)
        self.watch(view.viewport())
        self.show()
        self._place()

    def place_in(self, layout, view: QWidget) -> None:
        """Rail and flyout at the start of the horizontal ``layout`` that holds ``view``."""
        layout.insertWidget(0, self)
        layout.insertWidget(1, self.flyout)
        self.flyout.hide()
        self.watch(view)

    def watch(self, widget: QWidget) -> None:
        """A press of the mouse or Esc on ``widget`` closes the flyout (unless pinned) and goes no further, as
        with a popup. Called again for a widget, the rail's filter runs before the ones installed since."""
        widget.installEventFilter(self._filter)  # Qt puts a filter installed again in front
        if widget not in self._watched:
            self._watched.append(widget)

    @property
    def _filter(self) -> "_RailFilter":
        f = getattr(self, "_filter_obj", None)
        if f is None:
            f = self._filter_obj = _RailFilter(self)
        return f

    def set_shown(self, on: bool) -> None:
        """Show or hide the rail (with its flyout) - the classic drawing bar replaces the 2D rail."""
        if not on:
            self.close(force=True)
        self.setVisible(on)
        self._place()

    def _place(self) -> None:
        view = self._view
        if view is None:
            if not self.flyout.isHidden():
                self.flyout.setFixedWidth(self.flyout.wanted_size().width())
            return
        shown = not self.isHidden()
        view.setViewportMargins(self.width() if shown else 0, 0, 0, 0)
        vp = view.viewport().geometry()
        if shown:
            self.setGeometry(vp.left() - self.width(), vp.top(), self.width(), vp.height())
        if not self.flyout.isHidden():
            want = self.flyout.wanted_size()
            height = max(80, min(want.height(), vp.height() - 8))
            self.flyout.setGeometry(vp.left() + 2, vp.top() + 4, min(want.width(), max(120, vp.width() - 8)), height)
            self.flyout.raise_()


class _RailFilter(QObject):
    def __init__(self, rail: ToolRail):
        super().__init__(rail)
        self.rail = rail
        self._swallow = None  # the widget whose press closed the flyout: its release goes with it

    def eventFilter(self, obj, ev) -> bool:
        rail = self.rail
        t = ev.type()
        if obj is rail._view and t in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            rail._place()
        elif t == QEvent.Type.MouseButtonPress and obj in rail._watched:
            if rail.close(force=False):  # as a popup: the press that closes the flyout does nothing else
                self._swallow = obj
                return True
        elif t in (QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick) and obj is self._swallow:
            if t == QEvent.Type.MouseButtonRelease:
                self._swallow = None
            return True
        elif (t == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Escape and rail.is_open() and not rail.pinned
              and obj in (rail._view, *rail._watched)):
            rail.close(force=False)
            return True  # Esc closed the flyout: nothing else is cancelled by the same key
        return False


__all__ = ["ToolRail", "RailGroup", "RailFlyout", "ActionButton"]
