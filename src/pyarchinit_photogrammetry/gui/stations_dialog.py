"""The permanent GNSS stations near a survey, to choose the one that is the base of a PPK or that places the user's own
base by a static baseline: the stations of the EPN, the IGS and RING within the distance a base is any good at, the
nearest first, with their networks and how far they are. The list is read in a thread of its own (it is downloaded when
the program has none from the last week: the lists of M3G and of the IGS); no file of any station is looked for here -
that is done when the station chosen is used, and what is missing is said then."""
from __future__ import annotations

import threading
from datetime import datetime

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..gnss import stations
from .i18n import current, number, tr

WITHIN_KM = 200.0  # beyond this a base hardly fixes the ambiguities of a flight
LIMIT = 30  # the stations listed at most


class _Relay(QObject):
    """Hands the list from the thread that reads it to the dialog, in the thread of the interface (the relay lives
    there: the connection is queued)."""

    loaded = Signal(int, object, object)  # the number of the request, the stations with their distance, the error


def _when(when: datetime) -> str:
    """A day as the interface writes it."""
    return f"{when:%d/%m/%Y}" if current() == "it" else f"{when:%Y-%m-%d}"


class StationsDialog(QDialog):
    """The permanent stations within ``within_km`` of the point ``lat``, ``lon`` (degrees), at most ``limit`` of them,
    the nearest first (:func:`.stations.nearby`), to choose one; ``when`` is the time of the flight, which the dialog
    names. The list is read in a thread: offline and without a saved list the dialog says what went wrong and
    *Riprova* reads it again. *OK* only with a station chosen (a double click chooses it too)."""

    def __init__(self, lat: float, lon: float, when: datetime | None = None, parent=None,
                 within_km: float = WITHIN_KM, limit: int = LIMIT):
        super().__init__(parent)
        self.lat, self.lon, self.when = float(lat), float(lon), when
        self.within_km, self.limit = float(within_km), int(limit)
        self.loading = False
        self._request = 0
        self.setWindowTitle(tr("Stazioni permanenti vicine"))
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        text = tr("Le stazioni permanenti dell'EPN, dell'IGS e di RING entro {0} km dal rilievo, la più vicina per "
                  "prima.", number(self.within_km, 0))
        if when is not None:
            text += " " + tr("I loro file per le ore del volo del {0} si cercano quando la stazione viene usata: se "
                             "mancano, il programma lo dice.", _when(when))
        else:
            text += " " + tr("I loro file si cercano quando la stazione viene usata: se mancano, il programma lo dice.")
        self.intro = QLabel(text)
        self.intro.setWordWrap(True)
        lay.addWidget(self.intro)
        heads = [tr("Stazione"), tr("Reti"), tr("Distanza (km)")]
        self.table = QTableWidget(0, len(heads))
        self.table.setHorizontalHeaderLabels(heads)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(240)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)  # the messages of the network, never markup
        self.b_retry = QPushButton(tr("Riprova"))
        self.b_retry.setVisible(False)
        row.addWidget(self.status, 1)
        row.addWidget(self.b_retry)
        lay.addLayout(row)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._relay = _Relay(self)
        self._relay.loaded.connect(self._loaded)
        self.table.itemSelectionChanged.connect(self._chosen)
        self.table.cellDoubleClicked.connect(lambda _row, _column: self.accept() if self.selected() else None)
        self.b_retry.clicked.connect(lambda: self.load())
        self._chosen()
        self.load()

    def load(self, refresh: bool = False) -> None:
        """Read the list in a thread (``refresh``: downloaded again even when the program has a recent one)."""
        self._request += 1
        request, relay = self._request, self._relay
        lat, lon, within, limit = self.lat, self.lon, self.within_km, self.limit
        self.loading = True
        self.b_retry.setVisible(False)
        self.status.setText(tr("Caricamento dell'elenco delle stazioni..."))

        def work() -> None:
            found = error = None
            try:
                found = stations.nearby(lat, lon, limit, within, stations=stations.station_list(refresh=refresh))
            except Exception as exc:  # noqa: BLE001 - offline, a list that cannot be read: the dialog says it
                error = exc
            try:
                relay.loaded.emit(request, found, error)
            except RuntimeError:  # the dialog was closed meanwhile
                pass

        threading.Thread(target=work, name="ppg-stations", daemon=True).start()

    @Slot(int, object, object)
    def _loaded(self, request: int, found, error) -> None:
        if request != self._request:  # an older request: a newer one is coming
            return
        self.loading = False
        if error is not None:
            self.table.setRowCount(0)
            self.status.setText(tr("L'elenco delle stazioni non si scarica: {0}. Controlla la connessione e premi "
                                   "Riprova.", str(error).rstrip(".")))
            self.b_retry.setVisible(True)
            self._chosen()
            return
        self.table.setRowCount(len(found))
        for i, (station, km) in enumerate(found):
            name = f"{station.id} - {station.name}" if station.name and station.name != station.id else station.id
            cells = [name, ", ".join(station.networks), number(km, 1)]
            for j, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if j == 0:
                    item.setData(Qt.ItemDataRole.UserRole, station)
                if j == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(i, j, item)
        within = number(self.within_km, 0)
        self.status.setText(tr("Nessuna stazione entro {0} km dal rilievo.", within) if not found else
                            tr("Una stazione entro {0} km.", within) if len(found) == 1 else
                            tr("{0} stazioni entro {1} km.", len(found), within))
        self._chosen()

    def selected(self) -> stations.Station | None:
        """The station of the row chosen, None without one."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _chosen(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.selected() is not None)

    @classmethod
    def ask(cls, parent, lat: float, lon: float, when: datetime | None = None) -> stations.Station | None:
        """The station chosen near ``lat``, ``lon`` for a flight at ``when``, None when the dialog is cancelled."""
        dialog = cls(lat, lon, when, parent)
        return dialog.selected() if dialog.exec() == QDialog.DialogCode.Accepted else None
