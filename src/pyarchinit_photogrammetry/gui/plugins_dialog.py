"""Strumenti > Plugin: the plugins found - installed by the program or with pip - with their
state (active, disabled, failed with the reason), and those of the index of the plugin repository
(https://github.com/enzococca/pyarchinit-photogrammetry-plugins) to install or update. Enabling,
disabling, installing and removing apply from the next start: *Riavvia ora* restarts at once
with the open project. Plugins are Python code with the same rights as the program."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from .i18n import tr

INSTALLED_HEADERS = ["Plugin", "Versione", "Stato", "Origine", "Descrizione"]
AVAILABLE_HEADERS = ["Plugin", "Versione", "Autore", "Descrizione", "Note"]


def _table(headers) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels([tr(h) for h in headers])
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def _item(text, value=None) -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    if value is not None:
        it.setData(Qt.ItemDataRole.UserRole, value)
    return it


def state(info) -> str:
    """The state of a plugin, in words."""
    if info.error:
        return tr("errore: {0}", info.error)
    if info.active:
        return tr("attivo") if info.enabled else tr("attivo fino al riavvio")
    if info.enabled:
        return tr("attivo dal prossimo avvio")
    return tr("disattivato")


class PluginsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.manager = window.plugins
        self.entries: list = []
        self.changed = False
        self.setWindowTitle(tr("Plugin"))
        self.resize(900, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Plugin installati")))
        self.installed = _table(INSTALLED_HEADERS)
        lay.addWidget(self.installed, 1)
        row = QHBoxLayout()
        self.toggle_button = QPushButton(tr("Attiva / disattiva"))
        self.toggle_button.clicked.connect(self.toggle)
        self.remove_button = QPushButton(tr("Rimuovi"))
        self.remove_button.clicked.connect(self.remove)
        row.addWidget(self.toggle_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(QLabel(tr("Disponibili nel repository dei plugin")))
        self.available = _table(AVAILABLE_HEADERS)
        lay.addWidget(self.available, 1)
        row = QHBoxLayout()
        self.refresh_button = QPushButton(tr("Aggiorna l'elenco"))
        self.refresh_button.clicked.connect(self.fetch)
        self.install_button = QPushButton(tr("Installa / aggiorna"))
        self.install_button.clicked.connect(self.install)
        row.addWidget(self.refresh_button)
        row.addWidget(self.install_button)
        row.addStretch(1)
        lay.addLayout(row)
        self.note = QLabel(tr("I plugin sono codice Python con gli stessi permessi del programma: installa solo quelli "
                              "di cui ti fidi. Le modifiche valgono dal prossimo avvio."))
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        row = QHBoxLayout()
        self.restart_button = QPushButton(tr("Riavvia ora"))
        self.restart_button.clicked.connect(self.restart)
        close = QPushButton(tr("Chiudi"))
        close.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(self.restart_button)
        row.addWidget(close)
        lay.addLayout(row)
        self.fill_installed()

    # ------------------------------------------------------------------ installed
    def fill_installed(self) -> None:
        t = self.installed
        t.setRowCount(0)
        for info in self.manager.plugins:
            r = t.rowCount()
            t.insertRow(r)
            source = tr("programma") if info.source == "folder" else "pip"
            for c, text in enumerate([info.title or info.name, info.version, state(info), source, info.description]):
                t.setItem(r, c, _item(text, info.name if c == 0 else None))

    def _selected_installed(self):
        rows = self.installed.selectionModel().selectedRows()
        if not rows:
            return None
        name = self.installed.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return self.manager.get(name)

    def toggle(self) -> None:
        info = self._selected_installed()
        if info is None:
            return
        self.manager.set_enabled(info.name, not info.enabled)
        self.changed = True
        self.fill_installed()

    def remove(self) -> None:
        from .dialogs import choice, message

        info = self._selected_installed()
        if info is None:
            return
        if info.source != "folder":
            message(self, "info", tr("Plugin"), tr("{0} e' installato con pip: si toglie con pip uninstall {1}.",
                                                    info.title or info.name, info.distribution))
            return
        if choice(self, "question", tr("Plugin"), tr("Rimuovere il plugin {0}?", info.title or info.name),
                  [tr("Rimuovi")]) != 0:
            return
        try:
            self.manager.remove(info.name)
        except (OSError, ValueError) as exc:
            message(self, "warning", tr("Plugin"), str(exc))
            return
        self.changed = True
        self.fill_installed()

    # ------------------------------------------------------------------ the index
    def fetch(self) -> None:
        from ..plugins import index
        from .dialogs import message

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.entries = index.fetch()
        except index.PluginError as exc:
            self.entries = []
            message(self, "warning", tr("Plugin"), str(exc))
        finally:
            QGuiApplication.restoreOverrideCursor()
        installed = {i.name: i.version for i in self.manager.plugins}
        t = self.available
        t.setRowCount(0)
        for e in self.entries:
            r = t.rowCount()
            t.insertRow(r)
            note = e.problem()
            if not note and e.name in installed:
                note = tr("installato ({0})", installed[e.name]) if installed[e.name] == e.version else \
                    tr("installata la {0}: aggiornabile", installed[e.name])
            for c, text in enumerate([e.localized("title") or e.name, e.version, e.author, e.localized("description"),
                                      note]):
                t.setItem(r, c, _item(text, e.name if c == 0 else None))

    def install(self) -> None:
        from ..plugins import index
        from .dialogs import message

        rows = self.available.selectionModel().selectedRows()
        if not rows:
            return
        name = self.available.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        entry = next((e for e in self.entries if e.name == name), None)
        if entry is None:
            return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            index.install(entry, folder=self.manager.folder)
        except index.PluginError as exc:
            QGuiApplication.restoreOverrideCursor()
            message(self, "warning", tr("Plugin"), str(exc))
            return
        QGuiApplication.restoreOverrideCursor()
        self.changed = True
        self.manager.discover()
        self.fill_installed()
        self.fetch()
        message(self, "info", tr("Plugin"), tr("{0} {1} installato: si attiva al prossimo avvio.",
                                               entry.localized("title") or entry.name, entry.version))

    def restart(self) -> None:
        self.accept()
        self.window.restart()
