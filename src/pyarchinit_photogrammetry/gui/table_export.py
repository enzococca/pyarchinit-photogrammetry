"""Tables of the interface into Excel workbooks: *Esporta tabella in Excel...* in the context
menu of the tables of the panes and of the dialogs (markers, scale bars, cameras of the
Riferimenti pane, the attributes of the layers, the US proxies, the results of a step).

:func:`table_data` reads what a :class:`QTableWidget` (or any :class:`QTableView`) shows - its
visible columns and rows, the headers, the cells - with the numbers back as numbers: the value kept
under :data:`RAW_ROLE` when a cell has one, else its text read as a number when it is one (``1.25``,
``1,25`` in Italian, ``12.345.678``), a tick as true or false. :func:`add_excel_action` adds the
entry to a context menu a table already has, :func:`install_excel_menu` gives one to a table without.
"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QFileDialog, QMenu

from .dialogs import message
from .i18n import current, tr
from .icons import icon

RAW_ROLE = int(Qt.ItemDataRole.UserRole) + 77  # the value behind the text of a cell (a number stays a number)
_PLAIN = re.compile(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$")
_DOTS = re.compile(r"^[+-]?\d{1,3}(\.\d{3}){2,}$")  # 12.345.678: thousands in Italian
_COMMA_DECIMAL = re.compile(r"^[+-]?(\d{1,3}(\.\d{3})+|\d+),\d+$")  # 1.234,56 or 12,5
_COMMA_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")  # 1,234 or 1,234.5 in English


def parse_cell(text):
    """A cell's text as a number when it is one (the decimal point of Python's formatting, the
    separators of the interface's language), else the text; empty is None."""
    if text is None:
        return None
    t = str(text).strip().replace("−", "-").replace(" ", "")
    if not t:
        return None
    if t.lstrip("+-").startswith("0") and len(t.lstrip("+-")) > 1 and t.lstrip("+-")[1].isdigit():
        return str(text)  # a code with leading zeros (007) stays a text
    try:
        if _PLAIN.match(t):
            return int(t) if re.match(r"^[+-]?\d+$", t) else float(t)
        if _DOTS.match(t):
            return int(t.replace(".", ""))
        if current() == "it" and _COMMA_DECIMAL.match(t):
            return float(t.replace(".", "").replace(",", "."))
        if current() != "it" and _COMMA_THOUSANDS.match(t):
            v = t.replace(",", "")
            return float(v) if "." in v else int(v)
    except ValueError:
        pass
    return str(text)


def table_data(widget: QAbstractItemView) -> tuple[list[str], list[list]]:
    """Headers and rows of what ``widget`` shows (hidden columns and rows left out)."""
    model = widget.model()
    hidden_col = getattr(widget, "isColumnHidden", lambda _c: False)
    hidden_row = getattr(widget, "isRowHidden", lambda _r: False)
    cols = [c for c in range(model.columnCount()) if not hidden_col(c)]
    headers = []
    for c in cols:
        h = model.headerData(c, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        headers.append("" if h is None else str(h))
    rows = []
    for r in range(model.rowCount()):
        if hidden_row(r):
            continue
        row = []
        for c in cols:
            idx = model.index(r, c)
            raw = model.data(idx, RAW_ROLE)
            if raw is not None:
                row.append(raw)
                continue
            text = model.data(idx, Qt.ItemDataRole.DisplayRole)
            check = model.data(idx, Qt.ItemDataRole.CheckStateRole)
            if (text is None or str(text) == "") and check is not None:
                row.append(Qt.CheckState(check) == Qt.CheckState.Checked)
            else:
                row.append(parse_cell(text))
        rows.append(row)
    return headers, rows


def _save_path(parent, name: str) -> str | None:
    window = parent.window() if parent is not None else None
    if hasattr(window, "_last_dir"):  # the main window: the folder of the project
        folder = window._last_dir()
    else:
        from . import app_settings

        folder = str(app_settings().value("last_dir", str(Path.home())) or Path.home())
    safe = re.sub(r"[^\w.-]+", "_", name).strip("_") or "tabella"
    path, _ = QFileDialog.getSaveFileName(parent, tr("Esporta tabella in Excel"), str(Path(folder) / f"{safe}.xlsx"),
                                          "Excel (*.xlsx)")
    if not path:
        return None
    if not path.lower().endswith(".xlsx"):
        path += ".xlsx"
    return path


def export_rows(parent, name: str, headers: list[str], rows: list[list], path: str | None = None) -> Path | None:
    """``headers`` and ``rows`` into a workbook with one sheet ``name`` (asks where, unless
    ``path``); the status bar of the window says where it went. Returns the file, or None."""
    from ..io.tables import write_xlsx

    path = path or _save_path(parent, name)
    if not path:
        return None
    try:
        out = write_xlsx(path, {name: (headers, rows)})
    except Exception as exc:  # a file open in Excel, a folder not writable
        message(parent, "warning", tr("Esporta tabella in Excel"), tr("Esportazione non riuscita: {0}", exc))
        return None
    window = parent.window() if parent is not None else None
    bar = getattr(window, "statusBar", None)
    if callable(bar):
        bar().showMessage(tr("Tabella esportata in {0}", out), 10000)
    return out


def export_table(widget: QAbstractItemView, name: str, path: str | None = None) -> Path | None:
    """What ``widget`` shows into a workbook (see :func:`export_rows`)."""
    headers, rows = table_data(widget)
    return export_rows(widget, name, headers, rows, path)


def add_excel_action(menu: QMenu, widget: QAbstractItemView, name: str):
    """*Esporta tabella in Excel...* at the end of ``menu`` (a context menu of ``widget``)."""
    if menu.actions():
        menu.addSeparator()
    action = menu.addAction(icon("excel"), tr("Esporta tabella in Excel..."))
    action.triggered.connect(lambda _=False: export_table(widget, name))
    action.setEnabled(widget.model() is not None and widget.model().rowCount() > 0)
    return action


def excel_menu(widget: QAbstractItemView, name: str) -> QMenu:
    """The context menu :func:`install_excel_menu` shows."""
    menu = QMenu(widget)
    add_excel_action(menu, widget, name)
    return menu


def install_excel_menu(widget: QAbstractItemView, name: str) -> None:
    """A context menu with *Esporta tabella in Excel...* for a table that has none."""
    widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    widget.customContextMenuRequested.connect(
        lambda pos: excel_menu(widget, name).exec(widget.viewport().mapToGlobal(pos)))


__all__ = ["RAW_ROLE", "parse_cell", "table_data", "export_rows", "export_table", "add_excel_action", "excel_menu",
           "install_excel_menu"]
