"""The files of a PPK run and the position of its base, asked before RTKLIB runs: the drone's RINEX found beside its
Timestamp.MRK, the base's RINEX with what its header says, the navigation files beside them, and the base at the
position of its RINEX header - with the warning that it can be metres off - or at a position typed in."""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QRadioButton,
                               QVBoxLayout, QWidget)

from ..gnss import rinex
from .i18n import number, tr

_DJI_ROVER = "_rinex.obs"  # what a DJI drone writes beside its <flight>_Timestamp.MRK: <flight>_Rinex.obs
_DJI_MRK = "_timestamp.mrk"
_OBSERVATIONS = re.compile(r"\.(?:obs|\d\do)$", re.IGNORECASE)  # *.obs, *.24o


def rover_beside(mrk) -> Path | None:
    """The drone's RINEX observations beside its Timestamp.MRK: the ``*_Rinex.obs`` a DJI drone writes (of the
    flight of the MRK when the folder holds several), else the only ``*.obs`` or ``*.##o`` file there; None when there
    is none, or nothing tells which. Hidden files (the AppleDouble ones of an external drive) are left out."""
    mrk = Path(mrk)
    try:
        files = sorted(p for p in mrk.parent.iterdir() if p.is_file() and not p.name.startswith("."))
    except OSError:
        return None
    dji = [p for p in files if p.name.lower().endswith(_DJI_ROVER)]
    if dji:
        name = mrk.name.lower()
        flight = name[:-len(_DJI_MRK)] if name.endswith(_DJI_MRK) else None
        same = [p for p in dji if p.name.lower()[:-len(_DJI_ROVER)] == flight]
        if len(same) == 1:
            return same[0]
        return dji[0] if len(dji) == 1 else None
    others = [p for p in files if _OBSERVATIONS.search(p.name)]
    return others[0] if len(others) == 1 else None


def _is_navigation(path: Path) -> bool:
    try:
        return rinex.header(path).kind == "nav"
    except (OSError, ValueError):
        return False


def navigation_beside(*observations) -> list[Path]:
    """The navigation files beside each of the observation files ``observations`` in turn (None or a missing file is
    left out): :func:`rinex.find_companions` of each, only those that are RINEX navigation files, each once."""
    found: list[Path] = []
    for obs in observations:
        if obs is None or not Path(obs).is_file():
            continue
        for path in rinex.find_companions(obs):
            if path not in found and _is_navigation(path):
                found.append(path)
    return found


def _number(text: str) -> float | None:
    """A number written with a point or a comma for the decimals, None for anything else."""
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _when(value) -> str:
    return f"{value:%Y-%m-%d %H:%M:%S}" if value is not None else "?"


class _FileField(QWidget):
    """A file name typed or chosen with *Sfoglia*, which starts from ``folder`` while the name is empty."""

    def __init__(self, text: str, title: str, filters: str, folder: Path, parent=None):
        super().__init__(parent)
        self.title, self.filters, self.folder = title, filters, folder
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(text)
        browse = QPushButton(tr("Sfoglia"))
        browse.setFixedWidth(72)
        lay.addWidget(self.edit, 1)
        lay.addWidget(browse)
        browse.clicked.connect(self._browse)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.title, self.edit.text().strip() or str(self.folder),
                                              self.filters)
        if path:
            self.edit.setText(path)

    def path(self) -> Path | None:
        text = self.edit.text().strip()
        return Path(text).expanduser() if text else None


class PpkDialog(QDialog):
    """The files and the base of a PPK run: the drone's observations (found beside the ``mrk``), the base's
    observations with what their header says, the navigation files (found beside both, taken away or added by hand)
    and the position of the base: the one of the header of its file, with the warning that it can be metres off, or
    the surveyed one of its mark, typed. *OK* checks them and says in the dialog what is missing."""

    def __init__(self, mrk, parent=None):
        super().__init__(parent)
        self.mrk = Path(mrk)
        self.setWindowTitle(tr("Correzioni PPK"))
        self.setMinimumWidth(600)
        self._headers: dict[str, tuple[int, object]] = {}  # path -> (time of the file, its header or what is wrong)
        self._added: list[Path] = []  # navigation files added by hand
        self._removed: set[Path] = set()  # navigation files found beside the observations and taken away
        self._sources: tuple | None = None  # the rover and base the navigation files were looked for beside
        lay = QVBoxLayout(self)
        intro = QLabel(tr("RTKLIB calcola dove era l'antenna del drone dalle sue osservazioni e da quelle di una base "
                          "che registrava durante il volo; ogni foto prende poi la posizione della camera al suo "
                          "scatto nel Timestamp.MRK, con il suo stato (fix, float, singola) e la sua accuratezza."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        form = QFormLayout()
        lay.addLayout(form)
        mrk_label = QLabel(self.mrk.name)
        mrk_label.setTextFormat(Qt.TextFormat.PlainText)
        mrk_label.setToolTip(str(self.mrk))
        form.addRow(tr("Timestamp.MRK"), mrk_label)
        observations = tr("Osservazioni RINEX (*.obs *.??o *.rnx *.gz);;Tutti i file (*)")
        rover = rover_beside(self.mrk)
        self.rover = _FileField(str(rover) if rover else "", tr("Osservazioni del drone"), observations,
                                self.mrk.parent)
        self.rover_info = self._info_label()
        form.addRow(tr("Osservazioni del drone"), self._stack(self.rover, self.rover_info))
        self.base = _FileField("", tr("Osservazioni della base"), observations, self.mrk.parent)
        self.base_info = self._info_label()
        form.addRow(tr("Osservazioni della base"), self._stack(self.base, self.base_info))

        navs = QWidget()
        nav_lay = QVBoxLayout(navs)
        nav_lay.setContentsMargins(0, 0, 0, 0)
        self.navs = QListWidget()
        self.navs.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.navs.setFixedHeight(96)
        nav_lay.addWidget(self.navs)
        nav_buttons = QHBoxLayout()
        self.b_add_nav = QPushButton(tr("Aggiungi..."))
        self.b_remove_nav = QPushButton(tr("Togli"))
        nav_buttons.addWidget(self.b_add_nav)
        nav_buttons.addWidget(self.b_remove_nav)
        nav_buttons.addStretch(1)
        nav_lay.addLayout(nav_buttons)
        form.addRow(tr("File di navigazione"), navs)

        position = QWidget()
        pos_lay = QVBoxLayout(position)
        pos_lay.setContentsMargins(0, 0, 0, 0)
        self.from_header = QRadioButton(tr("Dal file della base (intestazione RINEX)"))
        self.typed = QRadioButton(tr("Scritta: la posizione misurata del punto su cui sta la base"))
        group = QButtonGroup(self)
        group.addButton(self.from_header)
        group.addButton(self.typed)
        self.from_header.setChecked(True)
        pos_lay.addWidget(self.from_header)
        pos_lay.addWidget(self.typed)
        coordinates = QHBoxLayout()
        self.lat, self.lon, self.height = QLineEdit(), QLineEdit(), QLineEdit()
        for edit, hint in ((self.lat, tr("Latitudine (gradi)")), (self.lon, tr("Longitudine (gradi)")),
                           (self.height, tr("Quota ellissoidica (m)"))):
            edit.setPlaceholderText(hint)
            edit.setToolTip(hint)
            coordinates.addWidget(edit)
        pos_lay.addLayout(coordinates)
        self.header_warning = QLabel(tr("La posizione dell'intestazione RINEX può essere sbagliata di metri, e tutto "
                                        "il rilievo si sposta con lei: per una posizione assoluta accurata scrivi la "
                                        "posizione misurata della base."))
        self.header_warning.setWordWrap(True)
        self.header_warning.setStyleSheet("color: #b45309")
        pos_lay.addWidget(self.header_warning)
        pos_lay.addStretch(1)
        form.addRow(tr("Posizione della base"), position)

        lay.addStretch(1)  # the room the warning leaves when it goes, under the form
        self.error = QLabel("")
        self.error.setTextFormat(Qt.TextFormat.PlainText)  # file names and messages of the engine, never markup
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #b91c1c; font-weight: bold")
        lay.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.rover.edit.textChanged.connect(lambda _text: self._update())
        self.base.edit.textChanged.connect(lambda _text: self._update())
        self.typed.toggled.connect(lambda _on: self._update())
        self.b_add_nav.clicked.connect(self._add_navs)
        self.b_remove_nav.clicked.connect(self._remove_navs)
        self._update()

    @staticmethod
    def _stack(*widgets: QWidget) -> QWidget:
        """The widgets one under the other in one cell of the form."""
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        for widget in widgets:
            lay.addWidget(widget)
        return holder

    @staticmethod
    def _info_label() -> QLabel:
        label = QLabel("")
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet("color: #6c7a89")
        return label

    # ------------------------------------------------------------ the files
    def _header(self, path: Path | None):
        """The header of ``path`` (read once while the file stays the same), or the text that says why there is none;
        None without a file."""
        if path is None:
            return None
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return tr("Il file non esiste.")
        known = self._headers.get(str(path))
        if known is not None and known[0] == stamp:
            return known[1]
        try:
            value = rinex.header(path)
        except (OSError, ValueError) as exc:
            value = tr("Il file non si legge: {0}", exc)
        self._headers[str(path)] = (stamp, value)
        return value

    def _observations(self, path: Path | None):
        """The header of the observations in ``path``, or the text that says why they cannot be used."""
        info = self._header(path)
        if isinstance(info, rinex.RinexInfo) and info.kind != "obs":
            return tr("Questo file ha messaggi di navigazione, non osservazioni.")
        return info

    def _describe_rover(self, info) -> str:
        if info is None:
            return tr("Il RINEX del drone: per un drone DJI il file *_Rinex.obs accanto al Timestamp.MRK.")
        if isinstance(info, str):
            return info
        return tr("Ricevitore {0}; osservazioni dal {1} al {2} (ora GPS).", info.receiver or "-", _when(info.first),
                  _when(info.last))

    def _describe_base(self, info) -> str:
        if info is None:
            return tr("Il RINEX di una base che registrava durante il volo: la tua o una stazione permanente vicina.")
        if isinstance(info, str):
            return info
        file_name = self.base.path().name
        name = info.marker or Path(file_name[:-3] if file_name.lower().endswith(".gz") else file_name).stem
        lines = [tr("Base {0}: ricevitore {1}, antenna {2}; osservazioni dal {3} al {4} (ora GPS).", name,
                    info.receiver or "-", info.antenna or "-", _when(info.first), _when(info.last))]
        if info.approx_xyz is not None:
            lat, lon, h = rinex.ecef_to_llh(info.approx_xyz)
            lines.append(tr("Posizione nell'intestazione: {0}, {1}, {2} m sull'ellissoide.", number(lat, 7),
                            number(lon, 7), number(h, 2)))
        else:
            lines.append(tr("Nessuna posizione nell'intestazione: scrivi quella del punto su cui sta la base."))
        delta = info.antenna_delta_hen
        if delta is None:
            lines.append(tr("Il file della base non dà l'altezza dell'antenna: la posizione della base dev'essere "
                            "quella dell'antenna."))
        elif not any(delta):
            lines.append(tr("Il file della base dà un'altezza dell'antenna nulla: la posizione della base dev'essere "
                            "quella dell'antenna, non del punto a terra."))
        elif delta[1] or delta[2]:
            lines.append(tr("La posizione della base è quella del punto a terra: il programma aggiunge lo scostamento "
                            "dell'antenna del file della base, {0} m in alto, {1} m a est e {2} m a nord.",
                            number(delta[0], 3), number(delta[1], 3), number(delta[2], 3)))
        else:
            lines.append(tr("La posizione della base è quella del punto a terra: il programma aggiunge l'altezza "
                            "dell'antenna del file della base, {0} m.", number(delta[0], 3)))
        return "\n".join(lines)

    def _update(self) -> None:
        rover, base = self.rover.path(), self.base.path()
        self.rover_info.setText(self._describe_rover(self._observations(rover)))
        self.base_info.setText(self._describe_base(self._observations(base)))
        if (rover, base) != self._sources:
            self._sources = (rover, base)
            self._fill_navs()
        typed = self.typed.isChecked()
        for edit in (self.lat, self.lon, self.height):
            edit.setEnabled(typed)
        self.header_warning.setVisible(not typed)
        self.error.setText("")

    # ------------------------------------------------------------ navigation files
    def _fill_navs(self) -> None:
        """The navigation files beside the rover and then beside the base, without those taken away, and those added."""
        found = navigation_beside(*(self._sources or ()))
        paths = [p for p in found if p not in self._removed] + [p for p in self._added if p not in found]
        self.navs.clear()
        for path in paths:
            item = QListWidgetItem(path.name)
            item.setToolTip(str(path))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.navs.addItem(item)

    def nav_paths(self) -> list[Path]:
        return [Path(self.navs.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.navs.count())]

    def _add_navs(self) -> None:
        start = self.base.path() or self.rover.path() or self.mrk
        paths, _ = QFileDialog.getOpenFileNames(self, tr("File di navigazione"), str(Path(start).parent), tr(
            "Navigazione RINEX (*.nav *.??n *.??g *.??p *.??l *.??c *.rnx *.gz);;Tutti i file (*)"))
        for text in paths:
            path = Path(text)
            self._removed.discard(path)
            if path not in self._added:
                self._added.append(path)
        self._fill_navs()

    def _remove_navs(self) -> None:
        for item in self.navs.selectedItems():
            path = Path(item.data(Qt.ItemDataRole.UserRole))
            if path in self._added:
                self._added.remove(path)
            else:
                self._removed.add(path)
        self._fill_navs()

    # ------------------------------------------------------------ the position of the base
    def _typed_position(self) -> tuple[float, float, float] | str:
        """The position typed, or the text that says what is wrong with it."""
        lat, lon, h = (_number(edit.text()) for edit in (self.lat, self.lon, self.height))
        if lat is None or lon is None or h is None:
            return tr("Scrivi la posizione della base: latitudine e longitudine in gradi, quota ellissoidica in metri.")
        if not -90.0 <= lat <= 90.0:
            return tr("La latitudine della base è fra -90 e 90 gradi.")
        if not -180.0 <= lon <= 180.0:
            return tr("La longitudine della base è fra -180 e 180 gradi.")
        return lat, lon, h

    # ------------------------------------------------------------ the answer
    def problem(self) -> str | None:
        """What keeps the run from starting, in words; None when everything is there."""
        rover, base = self.rover.path(), self.base.path()
        if rover is None:
            return tr("Scegli le osservazioni del drone.")
        rover_info = self._observations(rover)
        if isinstance(rover_info, str):
            return tr("Le osservazioni del drone non si possono usare: {0}", rover_info)
        if base is None:
            return tr("Scegli le osservazioni della base.")
        base_info = self._observations(base)
        if isinstance(base_info, str):
            return tr("Le osservazioni della base non si possono usare: {0}", base_info)
        if os.path.samefile(rover, base):
            return tr("Il drone e la base sono lo stesso file: scegli le osservazioni della base.")
        navs = self.nav_paths()
        if not navs:
            return tr("Nessun file di navigazione: aggiungi quello della base o del drone (.nav, .##n, .##p, "
                      "*_MN.rnx).")
        for path in navs:
            info = self._header(path)
            if not isinstance(info, rinex.RinexInfo) or info.kind != "nav":
                return tr("{0} non è un file di navigazione RINEX.", path.name)
        if self.typed.isChecked():
            typed = self._typed_position()
            return typed if isinstance(typed, str) else None
        if base_info.approx_xyz is None:
            return tr("Il file della base non ha una posizione nell'intestazione: scrivi la posizione misurata della "
                      "base.")
        return None

    def values(self) -> dict:
        """``{"rover", "base", "navs", "base_position"}``: the files, and the position typed (latitude, longitude,
        height above the ellipsoid) or None for the one of the header of the base's file; a ``ValueError`` for a typed
        position that is not one (see :meth:`problem`)."""
        typed = self._typed_position() if self.typed.isChecked() else None
        if isinstance(typed, str):
            raise ValueError(typed)
        return {"rover": self.rover.path(), "base": self.base.path(), "navs": self.nav_paths(), "base_position": typed}

    def accept(self) -> None:
        problem = self.problem()
        self.error.setText(problem or "")
        if problem is None:
            super().accept()

    @classmethod
    def ask(cls, parent, mrk) -> dict | None:
        """The answer of the dialog for the Timestamp.MRK ``mrk`` (:meth:`values`), None when it is cancelled."""
        dialog = cls(mrk, parent)
        return dialog.values() if dialog.exec() == QDialog.DialogCode.Accepted else None
