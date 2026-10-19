"""New versions from the interface: the background check and the dialog that offers them (the
work is done by :mod:`pyarchinit_photogrammetry.updates`)."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser,
                               QVBoxLayout)

from .. import __version__, updates

ERROR_TEXTS = {
    "offline": "GitHub non e' raggiungibile: controlla la connessione a Internet.",
    "limit": "GitHub ha ricevuto troppe richieste da questa rete: riprova tra un'ora.",
    "missing": "Non c'e' ancora nessuna versione pubblicata.",
    "incomplete": "Il download si e' interrotto: riprova.",
    "install": "Non e' stato possibile installare la nuova versione.",
}

SOURCE_COMMAND = "git pull"


def error_text(exc: Exception) -> str:
    return ERROR_TEXTS.get(getattr(exc, "kind", ""), "Controllo degli aggiornamenti non riuscito.")


class UpdateChecker(QObject):
    """Asks GitHub for the latest release in a background thread. ``finished(release, error,
    manual)`` is delivered in the GUI thread: the checker lives there, so the connection to its
    receivers is queued."""

    finished = Signal(object, object, bool)

    def __init__(self, parent=None, check=None):
        super().__init__(parent)
        self.running = False
        self._check = check or updates.check
        self.finished.connect(self._done)

    def start(self, manual: bool = False, skip: str | None = None) -> bool:
        """``skip``: a version the user chose to skip (ignored when the check is ``manual``)."""
        if self.running:
            return False
        self.running = True

        def work():
            release = error = None
            try:
                release = self._check(skip=None if manual else skip)
            except Exception as exc:  # a failed check must never break the interface
                error = exc
            self.finished.emit(release, error, manual)

        threading.Thread(target=work, name="ppg-update-check", daemon=True).start()
        return True

    @Slot(object, object, bool)
    def _done(self, *_):
        self.running = False


class UpdateDialog(QDialog):
    """A newer release: what changed and what to do with it. After ``exec()``, ``action`` is
    ``install`` (macOS application), ``download`` (Windows application), ``copy`` (the command
    of a Python installation was copied), ``page``, ``skip`` or ``later``."""

    def __init__(self, release: updates.Release, kind: str | None = None, parent=None):
        super().__init__(parent)
        self.release = release
        self.kind = kind or updates.install_kind()
        self.action = "later"
        self.setWindowTitle("Aggiornamento disponibile")
        lay = QVBoxLayout(self)
        date = release.published[:10]
        head = QLabel(f"<b>E' disponibile PyArchInit Photogrammetry {release.version}</b>"
                      + (f" (pubblicata il {date[8:10]}/{date[5:7]}/{date[:4]})" if len(date) == 10 else "")
                      + f"<br>Questa copia e' la versione {__version__}.")
        lay.addWidget(head)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(release.notes.strip() or "Nessuna nota per questa versione.")
        notes.setMinimumSize(560, 260)
        lay.addWidget(notes)
        self.command = {"pip": updates.PIP_COMMAND, "source": SOURCE_COMMAND}.get(self.kind)
        hint = {
            "macos-app": "L'applicazione viene scaricata, sostituita e riaperta: il progetto aperto viene chiuso "
                         "come quando esci (con la richiesta di salvarlo).",
            "windows-app": "Il file zip viene scaricato nella cartella Download: chiudi il programma, estrai lo "
                           "zip e sostituisci la cartella del programma.",
            "pip": f"Installazione Python: aggiorna con <code>{updates.PIP_COMMAND}</code> (o con uv).",
            "source": "Copia dai sorgenti: aggiorna con <code>git pull</code> e reinstalla le dipendenze.",
        }.get(self.kind, "")
        if hint:
            label = QLabel(hint)
            label.setWordWrap(True)
            lay.addWidget(label)
        row = QHBoxLayout()
        self.buttons: dict[str, QPushButton] = {}
        main = {"macos-app": ("install", "Scarica e installa"), "windows-app": ("download", "Scarica"),
                "pip": ("copy", "Copia il comando"), "source": ("copy", "Copia il comando")}.get(self.kind)
        if main and (main[0] not in ("install", "download") or updates.asset_for_platform(release)):
            self._button(row, *main, default=True)
        self._button(row, "page", "Pagina della versione")
        row.addStretch(1)
        self._button(row, "skip", "Salta questa versione")
        self._button(row, "later", "Piu' tardi")
        lay.addLayout(row)

    def _button(self, row, action: str, text: str, default: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setMinimumWidth(b.sizeHint().width() + 24)  # room for the padding of the theme
        b.setDefault(default)
        b.setAutoDefault(default)
        b.clicked.connect(lambda _=False, a=action: self.choose(a))
        row.addWidget(b)
        self.buttons[action] = b
        return b

    def choose(self, action: str) -> None:
        self.action = action
        if action == "copy" and self.command:
            QApplication.clipboard().setText(self.command)
        if action in ("later", "skip"):
            self.reject()
        else:
            self.accept()
