"""A row of widgets that wraps onto more lines when the space is narrow."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout


class FlowLayout(QLayout):
    """Widgets in a row that continues on the next line when the row is too narrow, like
    words in a paragraph. The minimum width is that of the widest widget, so the panels
    built with it let the main window shrink to a laptop screen. Widgets with an
    expanding horizontal size policy (a search field, the cards of the start page) share
    the free space of their line; the others keep their size and stay on the left.

    The minimum height is the height of the lines at the current width (updated after each
    placement), so a docked panel makes room for the wrapped lines instead of drawing its
    next widget over them. There is no height-for-width: a scroll area around the panel
    would use it to stretch the panel to its preferred height."""

    def __init__(self, parent=None, spacing: int = 4):
        super().__init__(parent)
        self._items: list = []
        self._height = 0  # height of the lines at the width of the last placement
        self._watched = None  # the widget whose showing is watched (see eventFilter)
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    # -- QLayout interface ---------------------------------------------------
    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        need = self._arrange(rect, apply=True)
        owner = self.parentWidget()
        if owner is not None and owner is not self._watched:
            owner.installEventFilter(self)
            self._watched = owner
        if need != self._height and rect.width() > 0 and owner is not None and owner.isVisible():
            self._height = need  # more or fewer lines: the parents recompute their minimum size
            self.invalidate()

    def eventFilter(self, obj, event) -> bool:
        """The owner shown (the tab of a pane opened the first time): Qt placed the row just before,
        while it was not visible yet, so the height of its lines is taken now - otherwise the
        lines after the first were cut until the panel was resized."""
        if event.type() == QEvent.Type.Show and obj is self._watched:
            rect = self.geometry()
            if rect.width() > 0:
                need = self._arrange(rect, apply=False)
                if need != self._height:
                    self._height = need
                    self.invalidate()
        return False

    def sizeHint(self) -> QSize:
        """All the widgets on one line."""
        items = self._visible()
        m = self.contentsMargins()
        w = sum(it.sizeHint().width() for it in items) + self.spacing() * max(len(items) - 1, 0)
        h = max((it.sizeHint().height() for it in items), default=0)
        return QSize(w + m.left() + m.right(), max(h + m.top() + m.bottom(), self._height))

    def minimumSize(self) -> QSize:
        """As wide as the widest widget (on a narrower panel there is one widget per line),
        as high as the lines at the current width."""
        size = QSize(0, 0)
        for it in self._visible():
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return QSize(size.width() + m.left() + m.right(), max(size.height() + m.top() + m.bottom(), self._height))

    # -- placement -----------------------------------------------------------
    def _visible(self) -> list:
        return [it for it in self._items if not it.isEmpty()]

    @staticmethod
    def _grows(item) -> bool:
        w = item.widget()
        dirs = w.sizePolicy().expandingDirections() if w is not None else item.expandingDirections()
        return bool(dirs & Qt.Orientation.Horizontal)

    def _arrange(self, rect: QRect, apply: bool) -> int:
        """Place the widgets in ``rect`` (when ``apply``); returns the height they need."""
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        width, sp = max(area.width(), 1), self.spacing()
        lines, line, used = [], [], 0
        for it in self._visible():
            w = min(it.sizeHint().width(), width)
            if line and used + sp + w > width:
                lines.append(line)
                line, used = [], 0
            used += (sp if line else 0) + w
            line.append(it)
        if line:
            lines.append(line)
        y = area.y()
        for n, line in enumerate(lines):
            if n:
                y += sp
            widths = [min(it.sizeHint().width(), width) for it in line]
            free = width - sum(widths) - sp * (len(line) - 1)
            grow = [i for i, it in enumerate(line) if self._grows(it)]
            if free > 0 and grow:
                for k, i in enumerate(grow):
                    widths[i] += free // len(grow) + (1 if k < free % len(grow) else 0)
            height = max(it.sizeHint().height() for it in line)
            if apply:
                x = area.x()
                for it, w in zip(line, widths):
                    h = min(it.sizeHint().height(), height)
                    it.setGeometry(QRect(QPoint(x, y + (height - h) // 2), QSize(w, h)))
                    x += w + sp
            y += height
        return y - rect.y() + m.bottom()


__all__ = ["FlowLayout"]
