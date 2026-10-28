"""New versions from the interface: the background check and the dialog that offers them (the
work is done by :mod:`pyarchinit_photogrammetry.updates`), and the same for the plugins: the background
look at the plugin index and the summary of what it found (the work is done by
:mod:`pyarchinit_photogrammetry.plugins.updates`)."""
from __future__ import annotations

import html
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser,
                               QVBoxLayout)

from .. import __version__, updates
from ..plugins import updates as plugin_updates

from .i18n import tr, translate

ERROR_TEXTS = {
    "offline": "GitHub non e' raggiungibile: controlla la connessione a Internet.",
    "limit": "GitHub ha ricevuto troppe richieste da questa rete: riprova tra un'ora.",
    "missing": "Non c'e' ancora nessuna versione pubblicata.",
    "incomplete": "Il download si e' interrotto: riprova.",
    "install": "Non e' stato possibile installare la nuova versione.",
}

SOURCE_COMMAND = "git pull"


def error_text(exc: Exception) -> str:
    return tr(ERROR_TEXTS.get(getattr(exc, "kind", ""), "Controllo degli aggiornamenti non riuscito."))


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
        self.setWindowTitle(tr("Aggiornamento disponibile"))
        lay = QVBoxLayout(self)
        date = release.published[:10]
        head = QLabel(tr("<b>E' disponibile PyArchInit Photogrammetry {0}</b>", release.version)
                      + (tr(" (pubblicata il {0}/{1}/{2})", date[8:10], date[5:7], date[:4]) if len(date) == 10 else "")
                      + tr("<br>Questa copia e' la versione {0}.", __version__))
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
            "pip": tr("Installazione Python: aggiorna con <code>{0}</code> (o con uv).", updates.PIP_COMMAND),
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


class PluginUpdateChecker(QObject):
    """Looks at the plugin index in a background thread (:func:`..plugins.updates.run`: the newer versions
    of the plugins installed by the program are downloaded and kept for the next start, unless ``update``
    is off). ``finished(result, error, manual)`` is delivered in the GUI thread, as with
    :class:`UpdateChecker`; ``result`` is a :class:`..plugins.updates.PluginUpdates`. Built as
    :class:`UpdateChecker` is: ``run`` stands in for ``plugins.updates.run`` (the tests give their own)."""

    finished = Signal(object, object, bool)

    def __init__(self, parent=None, run=None):
        super().__init__(parent)
        self.running = False
        self._run = run
        self.finished.connect(self._done)

    def start(self, manual: bool, seen: list[str] | None, update: bool, folder: Path) -> bool:
        """``seen``: the names of the index told so far (None: the first look, which tells no plugin as new);
        ``update``: download the newer versions (else only say they exist); ``folder``: the plugin folder.
        False when a look is running already."""
        if self.running:
            return False
        self.running = True

        def work():
            result = error = None
            try:
                result = (self._run or plugin_updates.run)(seen, folder=folder, update=update)
            except Exception as exc:  # a failed look must never break the interface
                error = exc
            try:
                self.finished.emit(result, error, manual)
            except RuntimeError:  # the window was closed while the look went on
                pass

        threading.Thread(target=work, name="ppg-plugin-update-check", daemon=True).start()
        return True

    @Slot(object, object, bool)
    def _done(self, *_):
        self.running = False


def plugin_summary_html(result: plugin_updates.PluginUpdates) -> str:
    """What a look at the plugin index found, for the summary window: a block for each kind that has something,
    each under its heading - the plugins updated (active from the next start), those that can be updated (the
    switch is off), those whose update failed and why, those that wait and why, the new ones of the index with
    their title and description. The names, versions and texts come from the index and from the plugins: escaped.
    The reasons are the engine's own messages (English): they go through :func:`.i18n.translate` as the others do."""
    esc = html.escape
    blocks: list[str] = []

    def block(heading: str, rows: list[str], note: str = "") -> None:
        if rows:
            blocks.append(f"<p><b>{esc(heading)}</b></p>" + (f"<p>{esc(note)}</p>" if note else "")
                          + "<ul>" + "".join(f"<li>{row}</li>" for row in rows) + "</ul>")

    def change(name: str, old: str, new: str) -> str:
        return f"<b>{esc(name)}</b> {esc(old)} &rarr; {esc(new)}"

    def reason(name: str, why: str) -> str:
        """The message of the engine, in the language of the interface, without the name of the plugin that most of
        them begin with: it stands in front of the line already."""
        return esc(translate(why.removeprefix(f"{name}: ")))

    block(tr("Aggiornati: attivi dal prossimo avvio"), [change(*u) for u in result.updated])
    block(tr("Aggiornamenti disponibili"), [change(*u) for u in result.available],
          tr("L'aggiornamento automatico dei plugin è spento (Aiuto > Aggiorna i plugin da soli): installa le nuove "
             "versioni dal gestore dei plugin."))
    block(tr("Aggiornamenti non riusciti"), [f"<b>{esc(name)}</b>: {reason(name, why)}" for name, why in result.failed])
    block(tr("In attesa"), [f"<b>{esc(name)}</b> {esc(version)}: {reason(name, why)}"
                            for name, version, why in result.waiting])
    news = []
    for entry in result.new:
        row = f"<b>{esc(entry.localized('title') or entry.name)}</b> {esc(entry.version)}"
        if entry.localized("description"):
            row += f"<br>{esc(entry.localized('description'))}"
        problem = entry.problem()  # one this program cannot install says why
        if problem:
            row += f"<br><i>{esc(translate(problem))}</i>"
        news.append(row)
    block(tr("Nuovi nel repository dei plugin"), news,
          tr("Non vengono installati da soli: scegli quali dal gestore dei plugin."))
    return "".join(blocks)


class PluginUpdatesDialog(QDialog):
    """The summary of a look at the plugin index (:func:`plugin_summary_html`). *Riavvia ora* is offered only
    when an update was downloaded: it is put in place by the restart. After ``exec()``, ``action`` is
    ``restart``, ``manager`` (open *Strumenti > Plugin...*) or None."""

    def __init__(self, result: plugin_updates.PluginUpdates, parent=None):
        super().__init__(parent)
        self.result = result
        self.action: str | None = None
        self.setWindowTitle(tr("Aggiornamenti dei plugin"))
        lay = QVBoxLayout(self)
        self.summary = QTextBrowser()
        self.summary.setHtml(plugin_summary_html(result))
        self.summary.setMinimumSize(520, 240)
        lay.addWidget(self.summary)
        self.resize(620, 420)
        row = QHBoxLayout()
        self.buttons: dict[str, QPushButton] = {}
        if result.updated:
            self._button(row, "restart", tr("Riavvia ora"), default=True)
        self._button(row, "manager", tr("Apri il gestore dei plugin"), default=not result.updated)
        row.addStretch(1)
        self._button(row, "close", tr("Chiudi"))
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
        if action == "close":
            self.reject()
            return
        self.action = action
        self.accept()
