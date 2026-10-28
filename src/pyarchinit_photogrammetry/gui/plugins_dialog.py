"""Strumenti > Plugin: the plugins found - installed by the program or with pip - with their
state (active, disabled, failed with the reason), and those of the index of the plugin repository
(https://github.com/enzococca/pyarchinit-photogrammetry-plugins) to install or update. Enabling,
disabling, installing and removing apply from the next start: *Riavvia ora* restarts at once
with the open project. Plugins are Python code with the same rights as the program."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .i18n import tr, translate

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
        self.new_button = QPushButton(tr("Nuovo plugin..."))
        self.new_button.setToolTip(tr("Scrive il progetto di un plugin da cui partire (per chi programma in Python)"))
        self.new_button.clicked.connect(self.new_plugin)
        row.addWidget(self.toggle_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        row.addWidget(self.new_button)
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
    def _installed_note(self, entry, info, ready: dict) -> str:
        """What the list says of an entry whose plugin is installed: the version in use, and what the index has
        beyond it - an update downloaded already, one that waits (the plugin is not where an update would go), or one
        that can be installed (:func:`..plugins.updates.status`)."""
        from ..plugins import updates
        from ..updates import is_newer

        state, why = updates.status(entry, info, ready, self.manager.folder)
        if state == "staged":
            return tr("pronta la {0}: attiva dal prossimo avvio", entry.version)
        if state == "waiting":
            return translate(why)
        if is_newer(entry.version, info.version):  # one installed with pip: the index has a newer one too
            return tr("installata la {0}: aggiornabile", info.version)
        return tr("installato ({0})", info.version)  # 0.2 is 0.2.0, and a version newer than the index's is no update

    def fetch(self) -> None:
        from ..plugins import index, updates
        from .dialogs import message

        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.entries = index.fetch()
        except index.PluginError as exc:
            self.entries = []
            message(self, "warning", tr("Plugin"), str(exc))
        finally:
            QGuiApplication.restoreOverrideCursor()
        installed = {i.name: i for i in self.manager.plugins}
        ready = updates.staged(self.manager.folder)
        t = self.available
        t.setRowCount(0)
        for e in self.entries:
            r = t.rowCount()
            t.insertRow(r)
            note = e.problem()
            if not note and e.name in installed:
                note = self._installed_note(e, installed[e.name], ready)
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

    def new_plugin(self) -> None:
        NewPluginDialog(self).exec()


PARTS_LABELS = [("action", "Azione nel menu Plugin"), ("background", "Lavoro in background con la barra di avanzamento"),
                ("command", "Comando della riga di comando (ppg plugin NOME)"),
                ("exporter", "Formato di esportazione (File > Esporta)")]


class NewPluginDialog(QDialog):
    """*Nuovo plugin...*: the project of a plugin to start from, written by
    :func:`pyarchinit_photogrammetry.plugins.scaffold.create` with the name, the texts, the author
    and the parts chosen; the folder opens in the file manager when it is done."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.created: Path | None = None
        self.setWindowTitle(tr("Nuovo plugin"))
        self.resize(560, 420)
        lay = QVBoxLayout(self)
        intro = QLabel(tr("Scrive una cartella con un plugin funzionante da cui partire: pacchetto Python con "
                          "l'entry point, le parti scelte con un esempio ciascuna, i test, i README, la licenza "
                          "e il workflow di rilascio. Poi si installa con pip install -e . e si modifica."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("conta-foto")
        form.addRow(tr("Nome (lettere, cifre e trattini)"), self.name)
        self.title_edit = QLineEdit()
        form.addRow(tr("Titolo"), self.title_edit)
        self.description = QLineEdit()
        form.addRow(tr("Descrizione"), self.description)
        self.author = QLineEdit()
        form.addRow(tr("Autore"), self.author)
        self.email = QLineEdit()
        form.addRow(tr("E-mail"), self.email)
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
                                or str(Path.home()))
        browse = QPushButton(tr("Sfoglia..."))
        browse.clicked.connect(self.browse)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        form.addRow(tr("Cartella"), folder_row)
        lay.addLayout(form)
        lay.addWidget(QLabel(tr("Il plugin parte con:")))
        self.parts = {}
        for key, label in PARTS_LABELS:
            box = QCheckBox(tr(label))
            box.setChecked(True)
            self.parts[key] = box
            lay.addWidget(box)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.create_button = QPushButton(tr("Crea"))
        self.create_button.setDefault(True)
        self.create_button.clicked.connect(self.create)
        cancel = QPushButton(tr("Annulla"))
        cancel.clicked.connect(self.reject)
        row.addWidget(self.create_button)
        row.addWidget(cancel)
        lay.addLayout(row)

    def browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, tr("Cartella del progetto"), self.folder.text())
        if folder:
            self.folder.setText(folder)

    def create(self) -> None:
        from ..plugins import scaffold
        from .dialogs import message

        parts = [key for key, box in self.parts.items() if box.isChecked()]
        try:
            target = scaffold.create(self.folder.text().strip() or str(Path.home()), self.name.text(),
                                     title=self.title_edit.text(), description=self.description.text(),
                                     author=self.author.text(), email=self.email.text(), parts=parts)
        except (ValueError, FileExistsError, OSError) as exc:
            message(self, "warning", tr("Nuovo plugin"), str(exc))
            return
        self.created = target
        message(self, "info", tr("Nuovo plugin"), tr("Progetto del plugin scritto in {0}.\n\nProssimi passi:\n{1}",
                                                      target, scaffold.next_steps(target)))
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        self.accept()
