"""The files of a PPK run and the position of its base, asked before RTKLIB runs: the Timestamp.MRK and its shots, the
drone's RINEX found beside it, how the base is placed - at the position of its RINEX header (with the warning that it
can be metres off), at a position typed in the frame of its coordinates, a permanent station near the flight as the
base, the user's own base placed by a static baseline from a station or by a static PPP - the base's RINEX (one file or
several) with what its header says, the navigation files beside them, the calibration of the antenna of the base
(igs20.atx, downloaded once), what will be downloaded, and whether the photos with an RTK fix of the drone itself take
the PPK position too. The dialog starts from the last run of the chunk. What can be checked before the run is checked
here: the files, their times, the work folder of the PPK; files packed in gzip, the Unix compress, zip or Hatanaka are
read in a thread of their own, never in the thread of the interface."""
from __future__ import annotations

import math
import os
import re
import threading
from datetime import timedelta
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget)

from ..gnss import atx as antex
from ..gnss import dji, frames, ppk, rinex
from ..gnss.apply import onboard_fix
from ..gnss.stations import Station
from .gnss_pane import length
from .i18n import decimal_comma, number, tr
from .stations_dialog import StationsDialog

_DJI_ROVER = "_rinex.obs"  # what a DJI drone writes beside its <flight>_Timestamp.MRK: <flight>_Rinex.obs
_DJI_MRK = "_timestamp.mrk"
_OBSERVATIONS = re.compile(r"\.(?:obs|\d\do)$", re.IGNORECASE)  # *.obs, *.24o
_GPS_EPOCH = dji.GPS_EPOCH.replace(tzinfo=None)
TYPING_PAUSE_MS = 300  # the files named in a field are read this long after the last key pressed in it
_SLACK_S = 1.0  # a shot this close to the first or last epoch of the drone counts as within its observations
_WARNING = "color: #b45309"
_NOTE = "color: #6c7a89"
# every way a RINEX file comes: plain, Hatanaka (.crx, .##d), and packed in gzip, the Unix compress or zip
_OBSERVATION_PATTERNS = "*.obs *.??o *.??d *.rnx *.crx *.gz *.Z *.zip"
_NAVIGATION_PATTERNS = "*.nav *.??n *.??g *.??p *.??l *.??c *.rnx *.gz *.Z *.zip"
MODES = ("header", "typed", "station", "baseline", "ppp")  # how the position of the base is set (values()["base_mode"])
_WITH_STATION = ("station", "baseline")  # the modes that need a permanent station
_ATX_MB = 62  # the size of igs20.atx the dialog says


def rover_beside(mrk) -> Path | None:
    """The drone's RINEX observations beside its Timestamp.MRK: the ``*_Rinex.obs`` a DJI drone writes (of the
    flight of the MRK when the folder holds several), else the only ``*.obs`` or ``*.##o`` file there; None when there
    is none, or nothing tells which. Hidden files (the AppleDouble ones of an external drive) are left out."""
    mrk = Path(mrk)
    try:
        files = sorted(p for p in mrk.parent.iterdir() if p.is_file() and not p.name.startswith("."))
    except OSError:
        return None
    dji_files = [p for p in files if p.name.lower().endswith(_DJI_ROVER)]
    if dji_files:
        name = mrk.name.lower()
        flight = name[:-len(_DJI_MRK)] if name.endswith(_DJI_MRK) else None
        same = [p for p in dji_files if p.name.lower()[:-len(_DJI_ROVER)] == flight]
        if len(same) == 1:
            return same[0]
        return dji_files[0] if len(dji_files) == 1 else None
    others = [p for p in files if _OBSERVATIONS.search(p.name)]
    return others[0] if len(others) == 1 else None


def _number(text: str) -> float | None:
    """A number written with a point or a comma for the decimals, None for anything else."""
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _same_file(a, b) -> bool:
    """``a`` and ``b`` name the same file (or the same path, when either is missing)."""
    if not a or not b:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def _when(value) -> str:
    """A datetime, or seconds of GPS time since the GPS epoch, to the second."""
    if value is None:
        return "?"
    if not hasattr(value, "strftime"):
        value = _GPS_EPOCH + timedelta(seconds=float(value))
    return f"{value:%Y-%m-%d %H:%M:%S}"


def _span(info) -> tuple[float, float] | None:
    """The first and last epoch of the observations ``info`` in seconds of GPS time, None when not known."""
    if not isinstance(info, rinex.RinexInfo) or info.first is None or info.last is None:
        return None
    return (info.first - _GPS_EPOCH).total_seconds(), (info.last - _GPS_EPOCH).total_seconds()


def _label(style: str = _NOTE) -> QLabel:
    """A label for what the dialog says of a file: plain text (file names and messages, never markup), wrapped."""
    label = QLabel("")
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setStyleSheet(style)
    return label


def _stack(*widgets: QWidget) -> QWidget:
    """The widgets one under the other in one cell of the form."""
    holder = QWidget()
    lay = QVBoxLayout(holder)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    for widget in widgets:
        lay.addWidget(widget)
    return holder


def station_of(record) -> Station | None:
    """A permanent station from what the last run recorded of it (:func:`.ppk.last_run` ``["station"]``), None when the
    record is not one."""
    try:
        return Station(str(record["id"]), str(record.get("name") or record["id"]), tuple(record.get("networks") or ()),
                       float(record["lat"]), float(record["lon"]), float(record["h"]),
                       tuple(float(v) for v in record["xyz"]))
    except (KeyError, TypeError, ValueError):
        return None


class _Pending:
    """What :meth:`PpkDialog._observations` gives for a file whose header is being read in the background."""

    def __init__(self, path: Path):
        self.path = path


class _Reading(Exception):
    """The header of ``path`` is being read in the background: asked again when it is there."""

    def __init__(self, path: Path):
        super().__init__(str(path))
        self.path = path


class _ReadRelay(QObject):
    """Hands a header read in a thread to the dialog, in the thread of the interface (the relay lives there)."""

    read = Signal(str, object, object, object)  # the path, its time and size, its header, the error reading it


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


class _FilesField(_FileField):
    """A file typed or chosen with *Sfoglia*, or several added with *Aggiungi...* - the pieces of a base: files of an
    hour, the two days of a flight across midnight -: the field then says how many and lists them in its tooltip.
    *Sfoglia* chooses one file again; typing in the field too."""

    def __init__(self, text: str, title: str, filters: str, folder: Path, parent=None):
        super().__init__(text, title, filters, folder, parent)
        self._many: list[Path] = []
        self._many_text = ""
        self.b_add = QPushButton(tr("Aggiungi..."))
        self.layout().addWidget(self.b_add)
        self.b_add.clicked.connect(self._add)

    def _browse(self) -> None:
        start = self.path()
        path, _ = QFileDialog.getOpenFileName(self, self.title, str(start) if start else str(self.folder),
                                              self.filters)
        if path:
            self.set_paths([Path(path)])

    def _add(self) -> None:
        current = self.paths()
        start = current[-1].parent if current else self.folder
        names, _ = QFileDialog.getOpenFileNames(self, self.title, str(start), self.filters)
        if names:
            self.set_paths(current + [path for path in map(Path, names) if path not in current])

    def set_paths(self, paths) -> None:
        """The files of the field: one is written in it, several are said by their number."""
        paths = [Path(p) for p in paths]
        self._many = paths if len(paths) > 1 else []
        self._many_text = tr("{0} file", len(paths)) if self._many else ""
        self.edit.setReadOnly(bool(self._many))
        self.edit.setToolTip("\n".join(str(p) for p in self._many))
        self.edit.setText(self._many_text or (str(paths[0]) if paths else ""))

    def paths(self) -> list[Path]:
        """The files of the field, in the order they were given; none when it is empty."""
        if self._many and self.edit.text() == self._many_text:
            return list(self._many)
        path = super().path()
        return [path] if path is not None else []

    def path(self) -> Path | None:
        """The first file of the field."""
        paths = self.paths()
        return paths[0] if paths else None


class PpkDialog(QDialog):
    """The files and the base of a PPK run: the shots of the ``mrk`` (read at once), the drone's observations (found
    beside it), how the base is placed (:data:`MODES`), the base's observations - one file, or several that make it,
    with what their header says - the navigation files (found beside both, taken away or added by hand), the
    calibration of the antenna of the base and whether the photos with an RTK fix of the drone itself take the PPK
    position too. ``work`` is the work folder of the PPK, which no input may be in. *OK* checks everything that can be
    checked before the run and says in the dialog what is missing or wrong.

    The base is placed at the position of the header of its file (with the warning that it can be metres off), at a
    typed position - the surveyed one of its mark - in the frame of its coordinates (an epoch for ITRF2020, the
    flight's by default; a warning when it is far from the header's, and when the same base was typed elsewhere for
    other photos of ``chunk``), or computed: a permanent station near the flight is the base (no file of the base:
    its observations and coordinates are downloaded), the user's base is placed by a static baseline from a station,
    or by a static PPP. The station is chosen in :class:`.StationsDialog`, near the flight; the dialog says what will
    be downloaded.

    With ``chunk`` the dialog starts from its last PPK run (:func:`.ppk.last_run`): how the base was placed, its files,
    its typed position, frame and epoch or its station, the navigation files still there, the choice about the onboard
    fixes, and the drone's observations too when the MRK is the one of that run. The files named in the fields are
    read after a pause in typing, once while they stay the same - those packed in gzip, compress, zip or Hatanaka in a
    thread of their own, while the dialog says it is reading them; the dialog is as tall as what it shows (the form
    scrolls only on a screen too short for it)."""

    def __init__(self, mrk, parent=None, work=None, chunk=None):
        super().__init__(parent)
        self.mrk = Path(mrk)
        self.work = Path(work) if work is not None else None
        self.chunk = chunk
        self.station: Station | None = None
        self.setWindowTitle(tr("Correzioni PPK"))
        self.setMinimumWidth(600)
        self._headers: dict[str, tuple] = {}  # path -> (its time and size, its header, the error reading it)
        self._pending: dict[str, tuple] = {}  # path -> the time and size of the file being read in the background
        self._beside: dict[str, tuple] = {}  # observation file -> (time of its folder, navigation files beside it)
        self._added: list[Path] = []  # navigation files added by hand
        self._removed: set[Path] = set()  # navigation files taken away: not listed again when found beside a file
        self._sources: tuple | None = None  # the rover and base the navigation files were listed for
        self._flight: set | None = None  # the keys of the cameras in the folder of the MRK (this run replaces them)
        self._atx_choice = True  # the user's choice about igs20.atx, kept while a computed base forces it
        try:
            self.shots = dji.read_mrk(self.mrk)
            self._mrk_problem = None if self.shots else tr("Il file non ha scatti.")
        except (OSError, ValueError) as exc:
            self.shots, self._mrk_problem = [], tr("Il file non si legge: {0}", exc)
        self._relay = _ReadRelay(self)
        self._relay.read.connect(self._read_done)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(TYPING_PAUSE_MS)
        self._timer.timeout.connect(self._update)

        lay = QVBoxLayout(self)
        intro = QLabel(tr("RTKLIB calcola dove era l'antenna del drone dalle sue osservazioni e da quelle di una base "
                          "che registrava durante il volo; ogni foto prende poi la posizione della camera al suo "
                          "scatto nel Timestamp.MRK, con il suo stato (fix, float, singola) e la sua accuratezza."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.body = QWidget()
        form = QFormLayout(self.body)
        form.setContentsMargins(0, 0, 0, 0)
        mrk_name = QLabel(self.mrk.name)
        mrk_name.setTextFormat(Qt.TextFormat.PlainText)
        mrk_name.setToolTip(str(self.mrk))
        self.mrk_info = _label()
        form.addRow(tr("Timestamp.MRK"), _stack(mrk_name, self.mrk_info))
        observations = tr("Osservazioni RINEX ({0});;Tutti i file (*)", _OBSERVATION_PATTERNS)
        rover = rover_beside(self.mrk)
        self.rover = _FileField(str(rover) if rover else "", tr("Osservazioni del drone"), observations,
                                self.mrk.parent)
        self.rover_info = _label()
        form.addRow(tr("Osservazioni del drone"), _stack(self.rover, self.rover_info))
        form.addRow(tr("Posizione della base"), self._position_section())
        self.base = _FilesField("", tr("Osservazioni della base"), observations, self.mrk.parent)
        self.base_info = _label()
        self.antenna_warning = _label(_WARNING)  # the phase-centre offset of the base's antenna: applied or not
        form.addRow(tr("Osservazioni della base"), _stack(self.base, self.base_info, self.antenna_warning))

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

        self.atx_download = QCheckBox(tr("Calibrazione dell'antenna della base: scarica igs20.atx (62 MB), una volta"))
        self.atx_download.setChecked(True)
        self.atx_note = _label()
        form.addRow(tr("Antenna della base"), _stack(self.atx_download, self.atx_note))
        self.downloads = _label()
        form.addRow(tr("Download"), self.downloads)

        self.replace_fix = QCheckBox(tr("Sostituisci anche i fix RTK del drone"))
        self.fix_note = _label()
        fixes = self._onboard_fixes()
        self.fix_note.setText(
            tr("{0} foto del chunk hanno un fix RTK del drone stesso (XMP o MRK di DJI): lo tengono e il PPK corregge "
               "le altre; con la spunta prendono anche loro la posizione PPK, così tutte stanno nel sistema della "
               "base.", fixes) if fixes else
            tr("Le foto con un fix RTK del drone stesso (XMP o MRK di DJI) lo tengono e il PPK corregge le altre; con "
               "la spunta prendono anche loro la posizione PPK, così tutte stanno nel sistema della base."))
        form.addRow(tr("Fix RTK del drone"), _stack(self.replace_fix, self.fix_note))

        self.scroll = QScrollArea()
        self.scroll.setWidget(self.body)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # never narrower than the form: what does not wrap (the choices of the position) stays whole
        self.scroll.setMinimumWidth(self.body.minimumSizeHint().width() + self.scroll.verticalScrollBar().sizeHint()
                                    .width() + 2 * self.scroll.frameWidth())
        lay.addWidget(self.scroll, 1)
        self.error = QLabel("")
        self.error.setTextFormat(Qt.TextFormat.PlainText)  # file names and messages of the engine, never markup
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #b91c1c; font-weight: bold")
        lay.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._prefill(ppk.last_run(chunk))  # before the fields are watched: read once below
        self.rover.edit.textChanged.connect(self._typing)
        self.base.edit.textChanged.connect(self._typing)
        self._modes.buttonToggled.connect(lambda _button, on: self._update() if on else None)
        for edit in (self.lat, self.lon, self.ellh, self.epoch):
            edit.textChanged.connect(lambda _text: self._update_mode())
        self.frame.currentIndexChanged.connect(lambda _index: self._frame_changed())
        self.atx_download.toggled.connect(self._atx_toggled)
        self.b_station.clicked.connect(self._choose_station)
        self.b_add_nav.clicked.connect(self._add_navs)
        self.b_remove_nav.clicked.connect(self._remove_navs)
        self._update()

    def _position_section(self) -> QWidget:
        """The five ways to place the base, the typed position with its frame and epoch, the permanent station, and
        the warnings of each."""
        position = QWidget()
        pos_lay = QVBoxLayout(position)
        pos_lay.setContentsMargins(0, 0, 0, 0)
        self.from_header = QRadioButton(tr("Dal file della base (intestazione RINEX)"))
        self.typed = QRadioButton(tr("Scritta: la posizione misurata del punto su cui sta la base"))
        self.from_station = QRadioButton(tr("Una stazione permanente vicina come base"))
        self.by_baseline = QRadioButton(tr("Calcolata con una linea di base da una stazione vicina"))
        self.by_ppp = QRadioButton(tr("Calcolata con il PPP statico"))
        self._modes = QButtonGroup(self)
        for mode, button in zip(MODES, (self.from_header, self.typed, self.from_station, self.by_baseline,
                                        self.by_ppp)):
            button.setProperty("ppg_mode", mode)
            self._modes.addButton(button)
        self.from_header.setChecked(True)
        pos_lay.addWidget(self.from_header)
        pos_lay.addWidget(self.typed)
        coordinates = QHBoxLayout()
        self.lat, self.lon, self.ellh = QLineEdit(), QLineEdit(), QLineEdit()
        for edit, hint in ((self.lat, tr("Latitudine (gradi)")), (self.lon, tr("Longitudine (gradi)")),
                           (self.ellh, tr("Quota ellissoidica (m)"))):
            edit.setPlaceholderText(hint)
            edit.setToolTip(hint)
            coordinates.addWidget(edit)
        pos_lay.addLayout(coordinates)
        frame_row = QHBoxLayout()
        frame_row.addWidget(QLabel(tr("Sistema delle coordinate scritte")))
        self.frame = QComboBox()
        for frame, text in (("unknown", tr("Sconosciuto (WGS 84)")), ("ITRF2020", "ITRF2020"),
                            ("ETRF2000", tr("ETRF2000 (RDN2008)")), ("ETRF2014", "ETRF2014"),
                            ("ETRF2020", "ETRF2020")):
            self.frame.addItem(text, frame)
        frame_row.addWidget(self.frame)
        frame_row.addStretch(1)
        pos_lay.addLayout(frame_row)
        self.epoch_row = QWidget()
        epoch_lay = QHBoxLayout(self.epoch_row)
        epoch_lay.setContentsMargins(0, 0, 0, 0)
        epoch_lay.addWidget(QLabel(tr("Epoca delle coordinate")))
        self.epoch = QLineEdit()
        self.epoch.setPlaceholderText(tr("anno decimale (2026,82) o data (2026-10-27)"))
        epoch_lay.addWidget(self.epoch)
        epoch_lay.addStretch(1)
        pos_lay.addWidget(self.epoch_row)
        note = _label()
        note.setText(tr("Latitudine e longitudine in gradi; la quota in metri sull'ellissoide (quella che dà il "
                        "GNSS), non sul livello del mare. Una posizione in ITRF2020 si muove con la placca, 2-3 cm "
                        "l'anno in Europa: serve l'epoca delle sue coordinate."))
        pos_lay.addWidget(note)
        self.header_warning = _label(_WARNING)
        self.header_warning.setText(tr("La posizione dell'intestazione RINEX può essere sbagliata di metri, e tutto "
                                       "il rilievo si sposta con lei: per una posizione assoluta accurata scrivi la "
                                       "posizione misurata della base."))
        pos_lay.addWidget(self.header_warning)
        self.far_warning = _label(_WARNING)
        pos_lay.addWidget(self.far_warning)
        self.reuse_warning = _label(_WARNING)  # the same base typed elsewhere for other photos of the chunk
        pos_lay.addWidget(self.reuse_warning)
        for button in (self.from_station, self.by_baseline, self.by_ppp):
            pos_lay.addWidget(button)
        self.station_row = QWidget()
        station_lay = QHBoxLayout(self.station_row)
        station_lay.setContentsMargins(0, 0, 0, 0)
        self.station_info = _label()
        self.b_station = QPushButton(tr("Scegli la stazione..."))
        station_lay.addWidget(self.station_info, 1)
        station_lay.addWidget(self.b_station)
        pos_lay.addWidget(self.station_row)
        pos_lay.addStretch(1)
        return position

    # ------------------------------------------------------------ the last run of the chunk
    def _prefill(self, last: dict | None) -> None:
        """The answers of the last PPK run of the chunk: how the base was placed (its typed position, frame and epoch,
        or its permanent station), its files (several when it came in several, all still there), the navigation files
        still there (but the ones beside the drone's observations of another flight), the choice about the onboard
        fixes; the drone's observations too when the MRK is the one of that run and the drone came in one file (this
        dialog takes one)."""
        if not last:
            return
        same = _same_file(last.get("mrk"), self.mrk)
        rover = last.get("rover") if isinstance(last.get("rover"), str) else None  # a list: the drone in pieces
        if same and rover and Path(rover).is_file():
            self.rover.edit.setText(rover)
        base = last.get("base")
        if isinstance(base, str) and base:
            self.base.set_paths([Path(base)])
        elif isinstance(base, list) and base and all(isinstance(p, str) and Path(p).is_file() for p in base):
            self.base.set_paths([Path(p) for p in base])
        drone = Path(rover).parent if rover else None
        for text in last.get("navs") or []:
            path = Path(text)
            if path.is_file() and path not in self._added and (same or drone is None or path.parent != drone):
                self._added.append(path)
        source, position = last.get("position_source"), last.get("position")
        if source == "typed" and position and len(position) == 3:
            lon, lat, h = (float(v) for v in position)
            self.typed.setChecked(True)
            for edit, value in ((self.lat, lat), (self.lon, lon), (self.ellh, h)):
                edit.setText(repr(value))
            index = self.frame.findData(last.get("frame") or frames.UNKNOWN)
            self.frame.setCurrentIndex(max(index, 0))
            if last.get("epoch") is not None:
                self.epoch.setText(decimal_comma(f"{float(last['epoch']):.4f}"))
        elif source in _WITH_STATION and station_of(last.get("station")) is not None:
            self.station = station_of(last["station"])
            (self.from_station if source == "station" else self.by_baseline).setChecked(True)
        elif source == "ppp":
            self.by_ppp.setChecked(True)
        self.replace_fix.setChecked(bool(last.get("replace_fix")))

    def _onboard_fixes(self) -> int:
        """How many photos of the chunk have an RTK fix of the drone itself (:func:`.apply.onboard_fix`)."""
        return sum(1 for cam in self.chunk.cameras if onboard_fix(cam)) if self.chunk is not None else 0

    def _flight_keys(self) -> set:
        """The keys of the cameras whose photos are in the folder of the MRK: this run gives them their positions."""
        if self._flight is None:
            folder = self.mrk.resolve().parent
            self._flight = {cam.key for cam in (self.chunk.cameras if self.chunk is not None else [])
                            if Path(cam.path).resolve().parent == folder}
        return self._flight

    # ------------------------------------------------------------ the size
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit()

    def _room(self) -> int:
        """The height the dialog can have: what the screen has available, less the frame of the window."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        frame = self.frameGeometry().height() - self.geometry().height()
        return max(screen.availableGeometry().height() - frame, self.minimumSizeHint().height())

    def _fit(self) -> None:
        """The dialog as tall as what it shows at its width, as far as the screen has room: the form scrolls only
        beyond that, so that no text is cut; the window moved up when it would go past the bottom of the screen."""
        if not self.isVisible():
            return
        # the texts changed: the heights for a width the layouts keep for the widgets and for themselves are found again
        for widget in self.body.findChildren(QWidget):
            widget.updateGeometry()
        for layout in self.findChildren(QLayout):
            layout.invalidate()
        lay = self.layout()
        margins = lay.contentsMargins()
        frame = 2 * self.scroll.frameWidth()
        # the form at the width it has beside a scroll bar: as tall as it is there, it fits with the bar or without it
        # (measured at the whole width it would not fit beside the bar shown while the dialog was shorter)
        bar = self.scroll.verticalScrollBar().sizeHint().width()
        form = self.body.heightForWidth(self.width() - margins.left() - margins.right() - frame - bar)
        if form < 0:  # nothing in the form wraps
            form = self.body.sizeHint().height()
        total = lay.totalHeightForWidth(self.width())
        if total < 0:
            total = lay.totalSizeHint().height()
        wanted = total - lay.itemAt(lay.indexOf(self.scroll)).sizeHint().height() + form + frame
        height = min(wanted, self._room())
        if height != self.height():
            self.resize(self.width(), height)
        # the form laid out again at its new height also when the dialog keeps its size (the screen is full): the
        # scroll area does it on a resize of its own, or when asked
        QCoreApplication.sendEvent(self.scroll, QEvent(QEvent.Type.LayoutRequest))
        available = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        geometry = self.frameGeometry()
        shift = max(min(0, available.bottom() - geometry.bottom()), available.top() - geometry.top())
        if shift:
            self.move(geometry.x(), geometry.y() + shift)

    # ------------------------------------------------------------ the files
    def _read(self, path: Path) -> rinex.RinexInfo:
        """:func:`rinex.header` of ``path``, read once while the file stays the same, its error too; a missing file
        is a ``FileNotFoundError``. A file packed in gzip, the Unix compress, zip or Hatanaka
        (:func:`rinex.compression`) is read in a thread of its own: :class:`_Reading` until it is there, when the
        dialog looks again."""
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        key = str(path)
        known = self._headers.get(key)
        if known is None or known[0] != stamp:
            if self._pending.get(key) == stamp:
                raise _Reading(path)
            if self._packed(path):
                self._pending[key] = stamp
                self._read_aside(path, key, stamp)
                raise _Reading(path)
            try:
                known = (stamp, rinex.header(path), None)
            except (OSError, ValueError) as exc:
                known = (stamp, None, exc)
            self._headers[key] = known
        if known[2] is not None:
            raise known[2].with_traceback(None)
        return known[1]

    @staticmethod
    def _packed(path: Path) -> bool:
        """``path`` is packed: what its first bytes say (:func:`rinex.compression`)."""
        try:
            return bool(rinex.compression(path))
        except OSError:
            return False

    def _read_aside(self, path: Path, key: str, stamp: tuple) -> None:
        """The header of ``path`` read in a thread, handed to :meth:`_read_done` in the thread of the interface."""
        relay = self._relay

        def work() -> None:
            info = error = None
            try:
                info = rinex.header(path)
            except (OSError, ValueError) as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001 - whatever stops the reading, the dialog says it
                error = ValueError(f"{path.name}: {exc}")
            try:
                relay.read.emit(key, stamp, info, error)
            except RuntimeError:  # the dialog was closed meanwhile
                pass

        threading.Thread(target=work, name="ppg-rinex-header", daemon=True).start()

    @Slot(str, object, object, object)
    def _read_done(self, key: str, stamp, info, error) -> None:
        """A header read in the background: kept, and everything the dialog says looked at again."""
        if self._pending.get(key) != stamp:  # the file changed meanwhile: another reading is coming
            return
        del self._pending[key]
        self._headers[key] = (stamp, info, error)
        self._sources = None  # the navigation files listed again, with this one read
        self._update()

    def _observations(self, path: Path | None):
        """The header of the observations in ``path``, the text that says why they cannot be used, or a
        :class:`_Pending` while it is read in the background; None without a file."""
        if path is None:
            return None
        try:
            info = self._read(path)
        except _Reading:
            return _Pending(path)
        except FileNotFoundError:
            return tr("Il file non esiste.")
        except (OSError, ValueError) as exc:
            return tr("Il file non si legge: {0}", exc)
        if info.kind != "obs":
            return tr("Questo file ha messaggi di navigazione, non osservazioni.")
        return info

    def _base_headers(self) -> list:
        """The header (or what :meth:`_observations` gives instead) of every file of the base."""
        return [self._observations(path) for path in self.base.paths()]

    @staticmethod
    def _joined_span(infos: list) -> tuple[float, float] | None:
        """The time all the headers ``infos`` cover together, in seconds of GPS time; None when one is not known."""
        spans = [_span(info) for info in infos]
        if not spans or any(span is None for span in spans):
            return None
        return min(a for a, _ in spans), max(b for _, b in spans)

    def _describe_mrk(self, rover_info) -> str:
        if self._mrk_problem:
            return self._mrk_problem
        times = [shot.gps_time for shot in self.shots]
        text = tr("Scatti: {0}, dal {1} al {2} (ora GPS).", len(times), _when(min(times)), _when(max(times)))
        span = _span(rover_info)
        if span is not None:
            outside = sum(1 for t in times if not span[0] - _SLACK_S <= t <= span[1] + _SLACK_S)
            if 0 < outside < len(times):
                text += " " + tr("Scatti fuori dalle osservazioni del drone: {0}; le loro foto terranno la posizione "
                                 "che hanno.", outside)
        return text

    def _describe_rover(self, info) -> str:
        if info is None:
            return tr("Il RINEX del drone: per un drone DJI il file *_Rinex.obs accanto al Timestamp.MRK.")
        if isinstance(info, _Pending):
            return tr("Lettura di {0} in corso...", info.path.name)
        if isinstance(info, str):
            return info
        return tr("Ricevitore {0}; osservazioni dal {1} al {2} (ora GPS).", info.receiver or "-", _when(info.first),
                  _when(info.last))

    def _describe_base(self, paths: list[Path], infos: list) -> str:
        if self.mode() == "station":
            return tr("La stazione permanente scelta fa da base: le sue osservazioni per le ore del volo vengono "
                      "scaricate, non serve un file della base.")
        if not paths:
            return tr("Il RINEX di una base che registrava durante tutto il volo: un file, o più file (Aggiungi...) "
                      "che insieme lo coprono; la tua base, o una stazione permanente vicina.")
        pending = next((info for info in infos if isinstance(info, _Pending)), None)
        if pending is not None:
            return tr("Lettura di {0} in corso...", pending.path.name)
        wrong = next(((path, info) for path, info in zip(paths, infos) if isinstance(info, str)), None)
        if wrong is not None:
            return wrong[1] if len(paths) == 1 else tr("{0}: {1}", wrong[0].name, wrong[1])
        info = infos[0]
        span = self._joined_span(infos)
        first, last = ((info.first, info.last) if span is None else
                       (_GPS_EPOCH + timedelta(seconds=span[0]), _GPS_EPOCH + timedelta(seconds=span[1])))
        name = ppk.base_name(paths[0], info)
        if len(paths) == 1:
            lines = [tr("Base {0}: ricevitore {1}, antenna {2}; osservazioni dal {3} al {4} (ora GPS).", name,
                        info.receiver or "-", info.antenna or "-", _when(first), _when(last))]
        else:
            lines = [tr("Base {0}: {1} file, ricevitore {2}, antenna {3}; osservazioni dal {4} al {5} (ora GPS).",
                        name, len(paths), info.receiver or "-", info.antenna or "-", _when(first), _when(last))]
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

    def _calibrated(self) -> bool:
        """The run will be given igs20.atx: it is asked for, or the program has it."""
        return self.atx_download.isChecked() or antex.cached_atx() is not None

    def _antenna_text(self, info) -> str:
        """Whether the offset of the phase centre of the base's antenna is applied (igs20.atx asked for, or the program
        has it) or not, naming the antenna of its header; nothing without the header of a base."""
        if not isinstance(info, rinex.RinexInfo):
            return ""
        kind = ppk.antenna_type(info)
        if not kind:
            return tr("L'intestazione della base non dice il tipo della sua antenna: lo scostamento del suo centro di "
                      "fase non viene applicato, e le quote possono risultare più basse di qualche centimetro, oltre "
                      "10 cm con alcune antenne choke ring.")
        if self._calibrated():
            return tr("Lo scostamento del centro di fase dell'antenna {0} della base viene applicato con le "
                      "calibrazioni dell'IGS (igs20.atx), se l'antenna vi è: il registro del calcolo dice quale "
                      "calibrazione RTKLIB ha usato.", kind)
        return tr("Lo scostamento del centro di fase dell'antenna {0} della base non viene applicato: le quote possono "
                  "risultare più basse di qualche centimetro, oltre 10 cm con alcune antenne choke ring. Spunta la "
                  "calibrazione dell'antenna della base per applicarlo.", kind)

    def _typing(self) -> None:
        """A file name is being typed: the files are read after a pause."""
        self.error.setText("")
        self._timer.start()

    def refresh(self) -> None:
        """Read what the files named so far hold now, without waiting for the pause in typing."""
        self._timer.stop()
        self._update()

    def _update(self) -> None:
        rover, bases = self.rover.path(), self.base.paths()
        station = self.mode() == "station"
        rover_info, base_infos = self._observations(rover), ([] if station else self._base_headers())
        self.mrk_info.setText(self._describe_mrk(rover_info))
        self.rover_info.setText(self._describe_rover(rover_info))
        self.base_info.setText(self._describe_base(bases, base_infos))
        sources = (rover, None if station else (tuple(bases) or None))
        if sources != self._sources:
            self._sources = sources
            self._fill_navs()
        self._update_mode()

    # ------------------------------------------------------------ navigation files
    def _companions(self, obs: Path | None) -> list[Path]:
        """The RINEX navigation files beside the observation file ``obs``, looked for again only when its folder
        changes (and while one of them is still being read)."""
        if obs is None:
            return []
        try:
            if not obs.is_file():
                return []
            stamp = obs.parent.stat().st_mtime_ns
        except OSError:
            return []
        known = self._beside.get(str(obs))
        if known is None or known[0] != stamp:
            found, pending = [], False
            for path in rinex.find_companions(obs):
                try:
                    if ppk.navigation_info(path, self._read):
                        found.append(path)
                except _Reading:
                    pending = True  # listed when it is read
            known = (stamp, found)
            if not pending:
                self._beside[str(obs)] = known
        return known[1]

    def _fill_navs(self) -> None:
        """The navigation files beside the rover and then beside the files of the base, without those taken away, and
        those added."""
        rover, bases = self._sources or (None, None)
        found: list[Path] = []
        for obs in (rover, *(bases or ())):
            found += [p for p in self._companions(obs) if p not in found]
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
            "Navigazione RINEX ({0});;Tutti i file (*)", _NAVIGATION_PATTERNS))
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
            self._removed.add(path)  # also when it is found beside a file too: away at the first click
        self._fill_navs()

    # ------------------------------------------------------------ the position of the base
    def mode(self) -> str:
        """How the position of the base is set: one of :data:`MODES`."""
        button = self._modes.checkedButton()
        return button.property("ppg_mode") if button is not None else "header"

    def _typed_position(self) -> tuple[float, float, float] | str:
        """The position typed, or the text that says what is wrong with it."""
        lat, lon, h = (_number(edit.text()) for edit in (self.lat, self.lon, self.ellh))
        if lat is None or lon is None or h is None:
            return tr("Scrivi la posizione della base: latitudine e longitudine in gradi, quota ellissoidica in metri.")
        if not -90.0 <= lat <= 90.0:
            return tr("La latitudine della base deve essere fra -90 e 90 gradi.")
        if not -180.0 <= lon <= 180.0:
            return tr("La longitudine della base deve essere fra -180 e 180 gradi.")
        return lat, lon, h

    def _typed_epoch(self) -> float | str | None:
        """The epoch of the typed coordinates in a frame that moves with time (ITRF2020), or the text that says what is
        wrong with it; None in another frame."""
        if self.frame.currentData() not in frames.DYNAMIC:
            return None
        try:
            return frames.parse_epoch(self.epoch.text())
        except ValueError:
            return tr("Scrivi l'epoca della posizione in ITRF2020: un anno decimale fra 1980 e 2100 (2026,82) o una "
                      "data (2026-10-27).")

    def _flight_place(self) -> tuple[float, float] | None:
        """Where the drone flew: the median latitude and longitude of the shots of the MRK; None without shots."""
        if not self.shots:
            return None
        lats, lons = sorted(shot.lat for shot in self.shots), sorted(shot.lon for shot in self.shots)
        middle = len(lats) // 2
        if len(lats) % 2:
            return lats[middle], lons[middle]
        return (lats[middle - 1] + lats[middle]) / 2.0, (lons[middle - 1] + lons[middle]) / 2.0

    def _flight_epoch(self) -> float | None:
        """The middle of the shots of the MRK as a decimal year (UTC); None without shots."""
        if not self.shots:
            return None
        first = min(self.shots, key=lambda shot: shot.gps_time).utc
        last = max(self.shots, key=lambda shot: shot.gps_time).utc
        return frames.decimal_year(first + (last - first) / 2)

    def _station_place(self) -> tuple[float, float] | None:
        """Where the stations are looked for: for a baseline the position in the header of the user's base, else (and
        without one) where the drone flew."""
        if self.mode() == "baseline":
            info = self._observations(self.base.path())
            if isinstance(info, rinex.RinexInfo) and info.approx_xyz is not None:
                lat, lon, _h = rinex.ecef_to_llh(info.approx_xyz)
                return lat, lon
        return self._flight_place()

    def _choose_station(self) -> None:
        """The permanent station chosen among those near the flight (:class:`.StationsDialog`)."""
        place = self._station_place()
        if place is None:
            self.error.setText(tr("Il programma non sa dove è il rilievo: il Timestamp.MRK non ha scatti."))
            return
        when = min(self.shots, key=lambda shot: shot.gps_time).utc if self.shots else None
        chosen = StationsDialog.ask(self, place[0], place[1], when)
        if chosen is not None:
            self.station = chosen
            self._update_mode()

    def _station_text(self) -> str:
        """The station chosen and how far it is from the flight, or that none is."""
        station = self.station
        if station is None:
            return tr("Nessuna stazione scelta.")
        networks = ", ".join(station.networks) or "-"
        named = (tr("{0} - {1} ({2})", station.id, station.name, networks) if station.name and
                 station.name != station.id else tr("{0} ({1})", station.id, networks))
        place = self._flight_place()
        if place is None:
            return named
        km = ppk.horizontal_m(place[0], place[1], station.lat, station.lon) / 1000.0
        return tr("{0}, a {1} km dal volo", named, number(km, 1))

    def _downloads_text(self, mode: str) -> str:
        """What will be downloaded for the base of the run, and the frame and epoch the positions will have."""
        sid = self.station.id if self.station is not None else None
        if mode == "station":
            if sid is None:
                return tr("Scegli la stazione: le sue coordinate e le sue osservazioni per le ore del volo vengono "
                          "scaricate.")
            return tr("Da scaricare: le coordinate della stazione {0} (EPN, IGS o RING) e le sue osservazioni per le "
                      "ore del volo, a 1 s o a 30 s; i file di navigazione del volo, se mancano; igs20.atx ({1} MB), "
                      "se il programma non lo ha.", sid, _ATX_MB) + " " + tr(
                "Le posizioni delle foto saranno in ITRF2020, all'epoca del volo.")
        if mode == "baseline":
            return tr("Da scaricare: le coordinate della stazione {0} e le sue osservazioni per le ore della sessione "
                      "della tua base, a 1 s o a 30 s; i file di navigazione, se mancano; igs20.atx ({1} MB), se il "
                      "programma non lo ha: una linea di base statica vuole un'ora o più di osservazioni comuni, di "
                      "più se la stazione è lontana.", sid or "?", _ATX_MB) + " " + tr(
                "La base, e le posizioni delle foto, saranno in ITRF2020, all'epoca della sessione della tua base.")
        if mode == "ppp":
            return tr("Da scaricare: i prodotti precisi (orbite, orologi, rotazione terrestre) dei giorni della "
                      "sessione della tua base - i finali di ESA circa 4 giorni dopo, i rapidi dell'IGS dopo 17 ore - "
                      "e igs20.atx ({0} MB), se il programma non lo ha. Con meno di 4 ore di osservazioni una linea di "
                      "base da una stazione vicina dà una posizione migliore.", _ATX_MB) + " " + tr(
                "La base, e le posizioni delle foto, saranno in ITRF2020, all'epoca della sessione della tua base.")
        if self.atx_download.isChecked() and antex.cached_atx() is None:
            return tr("Da scaricare: igs20.atx, le calibrazioni delle antenne dell'IGS ({0} MB), una volta sola.",
                      _ATX_MB)
        return tr("Niente da scaricare.")

    def _far_text(self) -> str:
        """The warning for a typed position far from the one of the header of the base's file, or nothing."""
        typed, info = self._typed_position(), self._observations(self.base.path())
        if isinstance(typed, str) or not isinstance(info, rinex.RinexInfo):
            return ""
        offset = ppk.offset_from_header(typed, info)
        if not ppk.far_from_header(offset):
            return ""
        apart = f"{number(offset[0] / 1000.0, 1)} km" if offset[0] >= 1000.0 else f"{number(offset[0], 0)} m"
        return tr("La posizione scritta è a {0} in pianta e a {1} m in quota da quella dell'intestazione del file "
                  "della base: controlla che latitudine e longitudine non siano scambiate o senza il segno, e che la "
                  "quota sia sull'ellissoide, non sul livello del mare.", apart, number(offset[1], 1))

    def _reuse_text(self) -> str:
        """The warning for a typed position of a base that was typed at another one, more than a few centimetres
        away, for photos of the chunk (but those in the folder of the MRK, which this run gives their positions):
        :func:`.ppk.other_typed_position`; nothing otherwise."""
        typed, base = self._typed_position(), self.base.path()
        info = self._observations(base)
        if self.chunk is None or isinstance(typed, str) or not isinstance(info, rinex.RinexInfo):
            return ""
        lat, lon, h = typed
        name = ppk.base_name(base, info)
        other = ppk.other_typed_position(self.chunk, name, [lon, lat, h], exclude=self._flight_keys())
        if other is None:
            return ""
        (lon0, lat0, h0), apart = other
        return tr("La base {0} è già stata usata in questo chunk nella posizione scritta {1}, {2}, {3} m, a {4} da "
                  "questa: se è lo stesso punto, una delle due è sbagliata. Correggi questa, o calcola di nuovo i voli "
                  "fatti con l'altra.", name, number(lat0, 8), number(lon0, 8), number(h0, 3), length(apart))

    def _frame_changed(self) -> None:
        """The frame of the typed position: in ITRF2020 its epoch, the flight's when none is written."""
        if self.frame.currentData() in frames.DYNAMIC and not self.epoch.text().strip():
            epoch = self._flight_epoch()
            if epoch is not None:
                self.epoch.setText(decimal_comma(f"{epoch:.4f}"))
        self._update_mode()

    def _atx_toggled(self, on: bool) -> None:
        if self.atx_download.isEnabled():  # the user's choice, not the one a computed base forces
            self._atx_choice = bool(on)
        self._update_mode()

    def _update_mode(self) -> None:
        """What the way the base is placed shows, asks for and warns of."""
        mode = self.mode()
        typed = mode == "typed"
        for widget in (self.lat, self.lon, self.ellh, self.frame):
            widget.setEnabled(typed)
        self.epoch_row.setVisible(typed and self.frame.currentData() in frames.DYNAMIC)
        self.header_warning.setVisible(mode == "header")
        far = self._far_text() if typed else ""
        self.far_warning.setText(far)
        self.far_warning.setVisible(bool(far))
        reuse = self._reuse_text() if typed else ""
        self.reuse_warning.setText(reuse)
        self.reuse_warning.setVisible(bool(reuse))
        self.station_row.setVisible(mode in _WITH_STATION)
        self.station_info.setText(self._station_text())
        self.base.setEnabled(mode != "station")
        computed = mode in ("station", "baseline", "ppp")  # the program downloads igs20.atx for them anyway
        self.atx_download.blockSignals(True)
        self.atx_download.setEnabled(not computed)
        self.atx_download.setChecked(True if computed else self._atx_choice)
        self.atx_download.blockSignals(False)
        kept = antex.cached_atx()
        self.atx_note.setText(
            tr("Il programma ha già igs20.atx: le calibrazioni vengono applicate.") if kept is not None else
            tr("Per la stazione, la linea di base e il PPP il programma scarica comunque igs20.atx, per correggere "
               "tutte le antenne.") if computed else
            tr("Senza, lo scostamento del centro di fase dell'antenna della base non viene applicato."))
        infos = [] if mode == "station" else self._base_headers()
        antenna = self._antenna_text(infos[0]) if infos else ""
        self.antenna_warning.setText(antenna)
        self.antenna_warning.setStyleSheet(_NOTE if self._calibrated() else _WARNING)
        self.antenna_warning.setVisible(bool(antenna))
        self.downloads.setText(self._downloads_text(mode))
        self.error.setText("")
        self._fit()

    # ------------------------------------------------------------ the answer
    def problem(self) -> str | None:
        """What keeps the run from starting, in words; None when everything is there. The files typed so far are
        read first; one still being read in the background is waited for."""
        if self._timer.isActive():
            self.refresh()
        if self._mrk_problem:
            return tr("Il Timestamp.MRK non si può usare: {0}", self._mrk_problem)
        mode = self.mode()
        rover, bases = self.rover.path(), self.base.paths()
        if rover is None:
            return tr("Scegli le osservazioni del drone.")
        rover_info = self._observations(rover)
        if isinstance(rover_info, _Pending):
            return tr("Il programma sta ancora leggendo {0}: attendi un momento.", rover.name)
        if isinstance(rover_info, str):
            return tr("Le osservazioni del drone non si possono usare: {0}", rover_info)
        times = [shot.gps_time for shot in self.shots]
        rover_span = _span(rover_info)
        if mode in _WITH_STATION and self.station is None:
            return tr("Scegli la stazione permanente (Scegli la stazione...).")
        base_span = None
        if mode != "station":
            if not bases:
                return tr("Scegli le osservazioni della base.")
            infos = self._base_headers()
            for path, info in zip(bases, infos):
                if isinstance(info, _Pending):
                    return tr("Il programma sta ancora leggendo {0}: attendi un momento.", path.name)
                if isinstance(info, str):
                    return tr("Le osservazioni della base non si possono usare: {0}",
                              info if len(bases) == 1 else tr("{0}: {1}", path.name, info))
                if os.path.samefile(rover, path):
                    return tr("Il drone e la base sono lo stesso file: scegli le osservazioni della base.")
            base_span = self._joined_span(infos)
            if rover_span and base_span and max(rover_span[0], base_span[0]) > min(rover_span[1], base_span[1]):
                return tr("Il drone (dal {0} al {1}) e la base (dal {2} al {3}) non hanno ore in comune: la base deve "
                          "registrare mentre il drone vola.", _when(rover_info.first), _when(rover_info.last),
                          _when(base_span[0]), _when(base_span[1]))
        if rover_span and not any(rover_span[0] - _SLACK_S <= t <= rover_span[1] + _SLACK_S for t in times):
            return tr("Gli scatti del Timestamp.MRK (dal {0} al {1}) sono fuori dalle osservazioni del drone (dal {2} "
                      "al {3}): non sono dello stesso volo.", _when(min(times)), _when(max(times)),
                      _when(rover_info.first), _when(rover_info.last))
        navs = self.nav_paths()
        if not navs and mode in ("header", "typed"):  # a computed base downloads the navigation when there is none
            return tr("Nessun file di navigazione: aggiungi quello della base o del drone (.nav, .##n, .##p, "
                      "*_MN.rnx).")
        for path in navs:
            if not path.exists():
                return tr("{0} non esiste più.", path.name)
            try:
                navigation = ppk.navigation_info(path, self._read)
            except _Reading:
                return tr("Il programma sta ancora leggendo {0}: attendi un momento.", path.name)
            if not navigation:
                return tr("{0} non è un file di navigazione RINEX.", path.name)
        if self.work is not None:
            for path in (rover, *([] if mode == "station" else bases), *navs):
                if ppk.inside(path, self.work):
                    return tr("{0} è nella cartella di lavoro del PPK ({1}), dove il programma scrive e cancella "
                              "file: spostalo fuori da lì.", path.name, str(self.work))
        if mode == "typed":
            typed = self._typed_position()
            if isinstance(typed, str):
                return typed
            epoch = self._typed_epoch()
            return epoch if isinstance(epoch, str) else None
        if mode == "header" and self._observations(bases[0]).approx_xyz is None:
            return tr("Il file della base non ha una posizione nell'intestazione: scrivi la posizione misurata della "
                      "base.")
        return None

    def values(self) -> dict:
        """``{"rover", "base", "navs", "base_position", "replace_fix", "base_mode", "base_frame", "base_epoch",
        "station", "atx"}``: the files (``base`` one path, a list of paths for a base in several files, None for a
        permanent station as the base), the position typed (latitude, longitude, height above the ellipsoid, as typed)
        or None, whether the photos with an RTK fix of the drone itself take the PPK position too, how the base is
        placed (:data:`MODES`), the frame of the typed position and the epoch of its coordinates (in ITRF2020; None
        otherwise), the permanent station (:class:`.stations.Station`) for a station as the base or a baseline,
        ``"download"`` when igs20.atx is to be downloaded when the program has none, else ``"auto"``. A
        ``ValueError`` for a typed position or epoch that is not one (see :meth:`problem`)."""
        mode = self.mode()
        typed = frame = epoch = None
        if mode == "typed":
            typed, frame, epoch = self._typed_position(), self.frame.currentData(), self._typed_epoch()
            for wrong in (typed, epoch):
                if isinstance(wrong, str):
                    raise ValueError(wrong)
        bases = self.base.paths()
        base = None if mode == "station" else (bases[0] if len(bases) == 1 else (bases or None))
        return {"rover": self.rover.path(), "base": base, "navs": self.nav_paths(), "base_position": typed,
                "replace_fix": self.replace_fix.isChecked(), "base_mode": mode, "base_frame": frame,
                "base_epoch": epoch, "station": self.station if mode in _WITH_STATION else None,
                "atx": "download" if self.atx_download.isChecked() else "auto"}

    def accept(self) -> None:
        problem = self.problem()
        self.error.setText(problem or "")
        if problem is None:
            super().accept()
        else:
            self._fit()

    @classmethod
    def ask(cls, parent, mrk, work=None, chunk=None) -> dict | None:
        """The answer of the dialog for the Timestamp.MRK ``mrk``, the work folder ``work`` and the chunk ``chunk``
        (its last run, its photos: :class:`PpkDialog`) (:meth:`values`), None when it is cancelled."""
        dialog = cls(mrk, parent, work, chunk)
        return dialog.values() if dialog.exec() == QDialog.DialogCode.Accepted else None
