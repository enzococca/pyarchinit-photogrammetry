"""The search of the commands in the top bar: every enabled command of the menu bar by its name.

Typing shows the commands whose name contains the text, each with the path of its menu
(``Allinea foto  —  Flusso di lavoro``); Enter or a click runs it. Ctrl+K (Cmd+K on macOS)
puts the cursor in the field from anywhere in the window.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QObject, QStringListModel, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QCompleter, QLineEdit, QMenu, QSizePolicy, QWidget

from .i18n import tr
from .icons import icon

SEPARATOR = "  —  "


def clean(text: str) -> str:
    """The name of a command as it reads: no mnemonic &, no trailing dots."""
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&").replace("…", "").rstrip(".").strip()


def menu_commands(menubar) -> dict[str, QAction]:
    """``"name  —  Menu > Submenu"`` -> action, for every enabled, visible command of ``menubar``."""
    found: dict[str, QAction] = {}

    def walk(menu: QMenu, path: list[str], seen: set) -> None:
        if id(menu) in seen:
            return
        seen.add(id(menu))
        for a in menu.actions():
            if a.isSeparator() or not a.isVisible():
                continue
            sub = a.menu()
            if sub is not None:
                walk(sub, path + [clean(a.text())], seen)
                continue
            name = clean(a.text())
            if not name or not a.isEnabled():
                continue
            found.setdefault(name + SEPARATOR + " > ".join(p for p in path if p), a)

    seen: set = set()
    for a in menubar.actions():
        if a.menu() is not None and a.isVisible():
            walk(a.menu(), [clean(a.text())], seen)
    return found


class CommandSearch(QLineEdit):
    """The field of the search (see the module)."""

    def __init__(self, window):
        super().__init__()
        self._window = window
        self.setObjectName("commandSearch")
        self.setClearButtonEnabled(True)
        key = QKeySequence("Ctrl+K").toString(QKeySequence.SequenceFormat.NativeText)
        self.setPlaceholderText(tr("Cerca un comando... ({0})", key))
        self.setToolTip(tr("Scrivi una parte del nome di un comando dei menu e premi Invio ({0})", key))
        self.addAction(icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(110)
        self.setMaximumWidth(300)
        self.commands: dict[str, QAction] = {}
        self._model = QStringListModel(self)
        comp = QCompleter(self._model, self)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        comp.setMaxVisibleItems(14)
        comp.activated[str].connect(self.run)
        self.setCompleter(comp)
        self.returnPressed.connect(self._enter)
        self._last = (None, 0.0)  # the action run last and when: Enter and the popup may both report it

    def refresh(self) -> None:
        """The commands as the menus are now (enabled ones only)."""
        self.commands = menu_commands(self._window.menuBar())
        self._model.setStringList(sorted(self.commands, key=str.casefold))

    def matches(self, text: str) -> list[str]:
        """The commands whose name or menu contains ``text`` (case not counted), names first."""
        if not self.commands:
            self.refresh()
        t = text.strip().casefold()
        if not t:
            return []
        names = [k for k in self.commands if t in k.split(SEPARATOR)[0].casefold()]
        rest = [k for k in self.commands if k not in names and t in k.casefold()]
        return sorted(names, key=lambda k: (not k.casefold().startswith(t), len(k))) + sorted(rest, key=len)

    def start(self) -> None:
        """Ctrl+K: the cursor in the field, ready to type."""
        self.refresh()
        self.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.selectAll()

    def focusInEvent(self, ev) -> None:
        self.refresh()
        super().focusInEvent(ev)

    def _enter(self) -> None:
        text = self.text()
        if text in self.commands:
            self.run(text)
            return
        found = self.matches(text)
        if found:
            self.run(found[0])

    def run(self, entry: str) -> bool:
        """Run the command ``entry`` (a key of :attr:`commands`); False when there is none."""
        action = self.commands.get(entry)
        if action is None or not action.isEnabled():
            return False
        last, when = self._last
        if last is action and time.monotonic() - when < 0.4:
            return True  # the same Enter seen by the popup and by the field
        self._last = (action, time.monotonic())
        QTimer.singleShot(0, self.clear)
        QTimer.singleShot(0, action.trigger)  # once the popup is closed: a dialog of the command opens in front
        return True


__all__ = ["CommandSearch", "menu_commands", "clean"]


class ShortcutKeeper(QObject):
    """Ctrl+K kept for the search of the commands: on Linux and Windows a focused line edit or text field takes
    Ctrl+K (delete to the end of the line) before the window's shortcut sees it. Installed on the application,
    it leaves the key to the shortcut in ``window`` and in its floating panes; every other key is untouched."""

    def __init__(self, window):
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt's name
        if (ev.type() == QEvent.Type.ShortcutOverride and ev.key() == Qt.Key.Key_K
                and ev.modifiers() == Qt.KeyboardModifier.ControlModifier and isinstance(obj, QWidget)
                and self._inside(obj)):
            ev.ignore()
            return True  # not accepted: the shortcut of the window runs
        return False

    def _inside(self, widget) -> bool:
        top = widget.window()
        while top is not None:
            if top is self._window:
                return True
            parent = top.parentWidget()
            top = parent.window() if parent is not None else None
        return False
