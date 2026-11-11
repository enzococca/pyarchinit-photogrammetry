"""The files of a PPK run and the position of its base, asked before RTKLIB runs: the shots - a DJI Timestamp.MRK,
the events of the rover's receiver, the time marks of a u-blox log, an autopilot log, an ``_events.pos`` - with the
photos they pair with, the lever arm and the latency of a self-built drone, the drone's observations (the RINEX found
beside the MRK, or the raw log of its receiver, converted in a thread), how the base is placed - at the position of
its RINEX header (with the warning that it
can be metres off), at a position typed in the frame of its coordinates, a permanent station near the flight as the
base, the user's own base placed by a static baseline from a station or by a static PPP - the base's RINEX (one file or
several) with what its header says, the navigation files beside them, the calibration of the antenna of the base
(igs20.atx, downloaded once), what will be downloaded, and whether the photos with an RTK fix of the drone itself take
the PPK position too. The dialog starts from the last run of the chunk. What can be checked before the run is checked
here: the files, their times, the work folder of the PPK; files packed in gzip, the Unix compress, zip or Hatanaka are
read in a thread of their own, never in the thread of the interface."""
from __future__ import annotations

import errno
import math
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget)

from ..core.progress import Progress
from ..gnss import atx as antex
from ..gnss import autopilot, convert, dji, events, frames, ppk, rinex, sources
from ..gnss import base as gbase
from ..gnss import shots as gshots
from ..gnss.apply import onboard_fix
from ..gnss.stations import Station
from . import shots_widgets as sw
from .gnss_pane import length
from .i18n import decimal_comma, engine, number, tr
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
_RAW_PATTERNS = "*.ubx *.rtcm *.rtcm3 *.sbf *.dat *.DAT"  # the raw logs of a receiver the program converts
_LOG_SUFFIXES = (".bin", ".log", ".ulg")  # the logs of an autopilot
_WITH_FILE = ("mrk", "log", "events-pos")  # the sources of the shots that are a file of their own
_RAW_NAMES = {"ubx": "u-blox", "rtcm3": "RTCM 3", "sbf": "Septentrio SBF", "unicore": "Unicore",
              "rt17": "Trimble RT-17"}
_DAY_IT = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})([ T].+)?")  # 04/05/2026, as it is written in Italy
_GPS_START = datetime(1980, 1, 6)
_WAIT = object()  # the approximate time of a log is not known yet: what tells it is still being read
MODES = gbase.BASE_MODES  # how the position of the base is set (values()["base_mode"])
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
    """What :meth:`PpkDialog._observations` gives for a file whose header is being read in the background, or -
    ``converting`` - for a raw log being converted to RINEX there."""

    def __init__(self, path: Path, converting: bool = False):
        self.path = path
        self.converting = converting


def opening_kind(path: Path) -> str:
    """The source of the shots (:data:`.sources.KINDS`) the file the dialog is opened with tells: a ``.MRK`` the
    shots of a DJI drone, a ``.pos`` a file of events, a PX4 log (by its first bytes) and a ``.bin`` ``.log`` ``.ulg``
    an autopilot log, a RINEX file or the raw log of a receiver the rover with its events; anything else an MRK."""
    suffix = path.suffix.lower()
    if suffix == ".mrk":
        return "mrk"
    if suffix == ".pos":
        return "events-pos"
    if suffix == ".ulg" or sources.is_ulog(path):
        return "log"
    try:
        raw = convert.raw_kind(path)
    except OSError:
        raw = None
    if raw is not None:
        return "events"
    return "log" if suffix in _LOG_SUFFIXES else "mrk"


def _stamp(path) -> tuple | None:
    """The time and size of the file ``path``, None when it is not there."""
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


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
    hour, the two days of a flight across midnight -: the field then says how many, lists them in its tooltip and
    cannot be typed in (read-only); only *Sfoglia* goes back to one file."""

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


def _receiver(info) -> str:
    """The receiver the RINEX header ``info`` names, or the words that say it names none."""
    return " ".join((info.receiver or "").split()) or tr("non indicato")


def _antenna(info) -> str:
    """The antenna the RINEX header ``info`` names, or the words that say it names none."""
    return " ".join((info.antenna or "").split()) or tr("non indicata")


class PpkDialog(QDialog):
    """The files and the base of a PPK run: the shots, the drone's observations, how the base is placed
    (:data:`MODES`), the base's observations - one file, or several that make it,
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
    scrolls only on a screen too short for it).

    ``mrk`` is the file the dialog is opened with, which tells the source of the shots (:func:`opening_kind`,
    changed in :attr:`kind_combo`): a DJI ``Timestamp.MRK`` (read at once, the drone's RINEX found beside it), the
    rover itself - a RINEX with the events of its receiver, or its raw log - a u-blox log with time marks, an
    autopilot log, an ``_events.pos``. :attr:`shots_file` is the file of the shots where the source has one,
    :attr:`shots_info` what it holds and the photos of ``chunk`` its shots pair with (:func:`.shots.pair`),
    :attr:`edge`, :attr:`message`, :attr:`camera` and :attr:`by` the options of the source; the lever arm from the
    antenna to the camera and the latency of the camera (:attr:`lever_panel`) are behind :attr:`b_lever`, and count
    whenever they are not zero, the panel open or closed (closed, the button and the summary of the shots say
    them); a DJI MRK takes none of them. A raw log named as the rover or the base (u-blox, RTCM 3,
    Septentrio SBF, Unicore, Trimble RT-17, a D-RTK ``.DAT``) is converted to RINEX in a thread
    (:func:`.convert.to_rinex`, kept in the cache of the program) while the dialog says so; closing the dialog
    cancels the conversion and waits for nothing."""

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
        self._flight: tuple | None = None  # the folder of the photos of this run, the keys of the cameras in it
        self._atx_choice = True  # the user's choice about igs20.atx, kept while a computed base forces it
        self._raw: dict[str, tuple] = {}  # path -> (its time and size, whether it is a raw log to convert)
        self._timeless: dict[str, tuple] = {}  # path -> (its time and size, whether it has the time of the week only)
        self._converted: dict[str, tuple] = {}  # raw log -> (its time and size, its :class:`.convert.Converted`)
        self._mrk_read: tuple | None = None  # the MRK read: (its path, time and size), its shots, its problem
        self.shots: list = []  # the shots of the MRK (:func:`.dji.read_mrk`), none for another source
        self._mrk_problem: str | None = None
        self._progress = Progress()  # of the conversions: cancelled when the dialog closes
        self._aside = sw.Aside(self)  # conversions, shots and pairings, each in a thread of its own
        self._aside.done.connect(self._aside_done)
        opened = opening_kind(self.mrk)
        self._relay = _ReadRelay(self)
        self._relay.read.connect(self._read_done)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(TYPING_PAUSE_MS)
        self._timer.timeout.connect(self._update)

        lay = QVBoxLayout(self)
        intro = QLabel(tr("RTKLIB calcola dove era l'antenna del drone dalle sue osservazioni e da quelle di una base "
                          "che registrava durante il volo; ogni foto prende poi la posizione della camera al suo "
                          "scatto - nel Timestamp.MRK di DJI, negli eventi del ricevitore o nel log dell'autopilota - "
                          "con il suo stato (fix, float, singola) e la sua accuratezza."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.body = QWidget()
        form = QFormLayout(self.body)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow(tr("Scatti"), self._shots_section(opened))
        observations = tr("Osservazioni RINEX o log del ricevitore ({0});;Tutti i file (*)",
                          _OBSERVATION_PATTERNS + " " + _RAW_PATTERNS)
        rover = self.mrk if opened == "events" else rover_beside(self.mrk) if opened == "mrk" else None
        self._rover_beside = str(rover) if rover and opened == "mrk" else ""  # found beside the MRK, not named
        self.rover = _FileField(self._rover_beside or (str(rover) if rover else ""), tr("Osservazioni del drone"),
                                observations, self.mrk.parent)
        self.rover_info = _label()
        self.day = QLineEdit()
        self.day.setPlaceholderText(tr("AAAA-MM-GG"))
        self.day.setMaximumWidth(220)
        self.day.setToolTip(tr("Un log RTCM 3 o Trimble RT-17 porta solo il tempo della settimana: il giorno del volo "
                               "(tempo GPS; anche con l'ora, 2026-05-04 09:30) dice in quale settimana cade. Serve "
                               "quando gli scatti sono gli eventi del log stesso: un file degli scatti lo dice da "
                               "sé."))
        self.day_row = QWidget()
        day_lay = QHBoxLayout(self.day_row)
        day_lay.setContentsMargins(0, 0, 0, 0)
        day_lay.addWidget(QLabel(tr("Giorno del volo")))
        day_lay.addWidget(self.day)
        day_lay.addStretch(1)
        form.addRow(tr("Osservazioni del drone"), _stack(self.rover, self.day_row, self.rover_info))
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

        self._show_kind(self.kind())  # before the form is measured: what the source does not ask for is not there
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
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Avvia"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Annulla"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._prefill(ppk.last_run(chunk))  # before the fields are watched: read once below
        self.kind_combo.currentIndexChanged.connect(lambda _index: self._update())
        self.shots_file.edit.textChanged.connect(self._typing)
        for combo in (self.edge, self.message, self.camera, self.by):
            combo.currentIndexChanged.connect(lambda _index: self._update())
        self.b_lever.toggled.connect(lambda _on: self._update())
        self.lever_panel.changed.connect(self._typing)
        self.rover.edit.textChanged.connect(self._typing)
        self.day.textChanged.connect(self._typing)
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

    def _shots_section(self, opened: str) -> QWidget:
        """Where the shots come from: the source, its file, its options (the edge of the time marks, the message and
        the camera of an autopilot log, how the photos are paired), what it holds, the lever arm and the latency."""
        self.kind_combo = QComboBox()
        names = sw.kind_names()
        for kind in sources.KINDS:
            self.kind_combo.addItem(names[kind], kind)
        self.kind_combo.setCurrentIndex(self.kind_combo.findData(opened))
        self.shots_file = _FileField("" if opened == "events" else str(self.mrk), tr("File degli scatti"),
                                     tr("Tutti i file (*)"), self.mrk.parent)

        def option(title: str, combo: QComboBox) -> QWidget:
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(QLabel(title))
            lay.addWidget(combo)
            lay.addStretch(1)
            return row

        self.edge = QComboBox()
        for edge, name in zip(events.EDGES, (tr("discesa"), tr("salita"))):
            self.edge.addItem(name, edge)
        self.edge.setToolTip(tr("Il fronte del segnale dello scatto che il ricevitore ha marcato."))
        self.message = QComboBox()
        sw.fill_messages(self.message, sources.is_ulog(self.mrk))
        self.message.setToolTip(tr("Gli scatti confermati dalla camera sono più precisi dei comandi di scatto; "
                                   "automatico prende i confermati quando il log li ha."))
        self.camera = QComboBox()
        self.camera.addItem(tr("una sola"), None)
        self.by = sw.by_combo()
        self.edge_row, self.message_row = option(tr("Fronte"), self.edge), option(tr("Messaggi"), self.message)
        self.camera_row, self.by_row = option(tr("Camera"), self.camera), option(tr("Abbinamento"), self.by)
        self.options_row = QWidget()
        options = QVBoxLayout(self.options_row)  # one under the other: side by side they would widen the dialog
        options.setContentsMargins(0, 0, 0, 0)
        options.setSpacing(2)
        for row in (self.edge_row, self.message_row, self.camera_row, self.by_row):
            options.addWidget(row)
        self.shots_info = _label()
        self.mrk_info = self.shots_info  # the name it had while the shots were those of an MRK only
        self.b_lever = QPushButton(tr("Braccio di leva e latenza..."))
        self.b_lever.setCheckable(True)
        self.b_lever.setToolTip(tr("Per un drone autocostruito: dove è la camera rispetto all'antenna e quanto dopo "
                                   "il segnale dello scatto espone. I valori diversi da zero contano anche a "
                                   "pannello chiuso: restano scritti sul pulsante."))
        lever_row = QWidget()
        lever_lay = QHBoxLayout(lever_row)
        lever_lay.setContentsMargins(0, 0, 0, 0)
        lever_lay.addWidget(self.b_lever)
        lever_lay.addStretch(1)
        self.lever_row = lever_row
        self.lever_panel = sw.LeverPanel()
        self.lever_forward, self.lever_right = self.lever_panel.forward, self.lever_panel.right
        self.lever_down, self.latency = self.lever_panel.down, self.lever_panel.latency
        return _stack(self.kind_combo, self.shots_file, self.options_row, self.shots_info, lever_row,
                      self.lever_panel)

    def kind(self) -> str:
        """The source of the shots: one of :data:`.sources.KINDS`."""
        return self.kind_combo.currentData()

    def set_kind(self, kind: str) -> None:
        self.kind_combo.setCurrentIndex(self.kind_combo.findData(kind))

    def _show_kind(self, kind: str) -> None:
        """What the source of the shots shows and asks for."""
        path = self.shots_file.path()
        self.shots_file.setEnabled(kind != "events")
        self.shots_file.edit.setPlaceholderText(
            tr("gli eventi sono nel file del rover") if kind == "events" else
            tr("vuoto: il log è il file del rover") if kind == "ubx" else "")
        self.shots_file.filters = {
            "mrk": tr("Timestamp.MRK (*.MRK *.mrk);;Tutti i file (*)"),
            "ubx": tr("Log u-blox (*.ubx);;Tutti i file (*)"),
            "log": tr("Log dell'autopilota (*.bin *.BIN *.log *.ulg);;Tutti i file (*)"),
            "events-pos": tr("File di eventi (*.pos);;Tutti i file (*)")}.get(kind, tr("Tutti i file (*)"))
        self.edge_row.setVisible(kind == "ubx")
        self.message_row.setVisible(kind == "log")
        if kind == "log":
            sw.fill_messages(self.message, path is not None and sources.is_ulog(path))
        self.camera_row.setVisible(kind == "log" and self.camera.count() > 1)
        self.by_row.setVisible(kind != "mrk")
        self.options_row.setVisible(kind != "mrk")
        self.lever_row.setVisible(kind != "mrk")
        self.lever_panel.setVisible(kind != "mrk" and self.b_lever.isChecked())
        self._show_lever()

    def _camera(self) -> int | None:
        """The camera of an autopilot log the shots are read of: None while the log has one."""
        return self.camera.currentData() if self.kind() == "log" and self.camera.count() > 1 else None

    def _lever(self) -> tuple[float, float, float] | None:
        """The lever arm that counts: the one typed, whatever the state of its panel, for a source that takes it;
        None when it is all zeros."""
        return self.lever_panel.lever() if self.kind() != "mrk" else None

    def _latency_ms(self) -> float:
        """The latency that counts, as :meth:`_lever`."""
        return self.lever_panel.latency_ms() if self.kind() != "mrk" else 0.0

    def _latency_s(self) -> float:
        return self._latency_ms() / 1000.0

    def _show_lever(self) -> None:
        """The button of the lever arm: with its panel closed it says the values that count."""
        short = None if self.b_lever.isChecked() else sw.lever_short(self._lever(), self._latency_ms())
        text = tr("Braccio di leva e latenza...") if short is None else tr("Braccio di leva e latenza: {0}", short)
        if self.b_lever.text() != text:
            self.b_lever.setText(text)

    def _photos(self) -> Path | None:
        """The folder of the photos of the shots (:func:`.shots_widgets.photos_folder`): None for an MRK (its own
        folder, as it always was) and when nothing tells (the window asks)."""
        if self.kind() == "mrk":
            return None
        return sw.photos_folder(self.chunk, self.shots_file.path() if self.kind() != "events" else self.rover.path())

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
        self.epoch.setPlaceholderText(tr("anno decimale (2026,82) o data (2026-10-27); vuota: la metà delle "
                                         "osservazioni del drone"))
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
        shots = last.get("shots") if isinstance(last.get("shots"), dict) else None
        same = _same_file(shots.get("file") if shots else last.get("mrk"), self.mrk)
        if shots and self.kind() != "mrk":  # the drone is the same: its lever arm and the latency of its camera
            lever, latency = shots.get("body_lever_mm"), float(shots.get("latency_s") or 0.0)
            if (lever and any(lever)) or latency:
                self.lever_panel.set_lever(lever)
                self.lever_panel.latency.setValue(latency * 1000.0)
                self.b_lever.setChecked(True)
            if same and shots.get("by") in gshots.BY:
                self.by.setCurrentIndex(self.by.findData(shots["by"]))
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
        """The keys of the cameras whose photos are in the folder of the photos of the shots (for an MRK its own
        folder): this run gives them their positions."""
        photos = self._photos()
        folder = (photos if photos is not None else (self.shots_file.path() or self.mrk).parent).resolve()
        if self._flight is None or self._flight[0] != folder:
            self._flight = (folder, {cam.key for cam in (self.chunk.cameras if self.chunk is not None else [])
                                     if Path(cam.path).resolve().parent == folder})
        return self._flight[1]

    def done(self, result: int) -> None:
        """The dialog closes: the conversions still running are cancelled and nothing waits for them."""
        self._timer.stop()
        self._progress.cancel()
        self._aside.stop()
        super().done(result)

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

    def _aside_done(self) -> None:
        """A conversion, some shots or a pairing are there: everything the dialog says looked at again."""
        self._sources = None  # the navigation files listed again, with those of a conversion
        self._update()

    def _is_raw(self, path: Path) -> tuple | None:
        """The time and size of ``path`` when it is the raw log of a receiver, to convert (:func:`.sources.is_raw`,
        as the run takes it); None for anything else."""
        stamp = _stamp(path)
        if stamp is None:
            return None
        known = self._raw.get(str(path))
        if known is None or known[0] != stamp:
            try:
                raw = sources.is_raw(path)
            except OSError:
                raw = False
            known = self._raw[str(path)] = (stamp, raw)
        return stamp if known[1] else None

    def _file_shots(self):
        """The shots of the file of the shots (never those read from the rover): a :class:`.shots.ShotSet`,
        :data:`.shots_widgets.RUNNING` while they are read, the exception that stopped the reading; None when the
        source has no file of its own, or none is named."""
        kind, path = self.kind(), self.shots_file.path()
        if kind in ("mrk", "events") or path is None:
            return None
        stamp = _stamp(path)
        if stamp is None:
            return FileNotFoundError(tr("Il file non esiste."))
        if path.is_dir():  # said by its name: the readers would give the error of the system, or their own
            return IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(path))
        edge, message, camera = self.edge.currentData(), self.message.currentData(), self._camera()
        if kind != "log":
            message, camera = "auto", None
        return self._aside.get(("shots", kind, str(path), stamp, edge if kind == "ubx" else None, message, camera),
                               lambda: sources.read(kind, path, edge, message, camera))

    def _shotset(self, rover_info):
        """The shots of a source other than an MRK, as :meth:`_file_shots` gives them; those of the rover - its
        events, the time marks of a u-blox rover - once its header ``rover_info`` is there (None until then)."""
        kind, path, rover = self.kind(), self.shots_file.path(), self.rover.path()
        if kind == "mrk":
            return None
        if kind == "events" or (kind == "ubx" and path is None):
            if rover is None or not isinstance(rover_info, rinex.RinexInfo):
                return None
            source = rover
            if kind == "events" and self._is_raw(rover) is not None:  # the events of a raw rover: in its conversion
                source = self._converted[str(rover)][1].obs
            stamp, edge = _stamp(source), self.edge.currentData()
            return self._aside.get(("shots", kind, str(source), stamp, edge if kind == "ubx" else None, "auto", None),
                                   lambda: sources.read(kind, source, edge))
        return self._file_shots()

    def _asks_day(self) -> bool:
        """Whether the day of the flight is asked: the rover is a raw log that carries the time of the week only
        (RTCM 3, Trimble RT-17) and the shots are its own events, so that no file of shots tells its day."""
        rover = self.rover.path()
        if self.kind() != "events" or rover is None:
            return False
        stamp = self._is_raw(rover)
        if stamp is None:
            return False
        known = self._timeless.get(str(rover))
        if known is None or known[0] != stamp:
            known = self._timeless[str(rover)] = (stamp, ppk._needs_time(rover))
        return known[1]

    def _day(self):
        """The day of the flight typed (a datetime in GPS time: ``2026-05-04``, ``04/05/2026``, with the time too),
        None when the field is empty or not asked, the text that says it is not a day."""
        text = self.day.text().strip()
        if not text or not self._asks_day():
            return None
        found = _DAY_IT.fullmatch(text)
        if found:
            text = f"{found.group(3)}-{int(found.group(2)):02d}-{int(found.group(1)):02d}{found.group(4) or ''}"
        try:
            when = datetime.fromisoformat(text)
        except ValueError:
            return tr("Il giorno del volo si scrive AAAA-MM-GG (anche con l'ora: 2026-05-04 09:30), non «{0}».",
                      self.day.text().strip())
        if when.tzinfo is not None or when < _GPS_START:
            return tr("Il giorno del volo è una data in tempo GPS, dal 1980 in poi, senza fuso orario.")
        return when

    def _approx_time(self, path: Path):
        """The approximate time (GPS seconds) for the conversion of the raw log ``path``, the one
        :func:`.sources.ppk_inputs` takes (so that the run finds the conversion of the dialog): for the rover the
        first shot of a file of shots, or the day of the flight typed when the shots are its own events
        (:meth:`_asks_day`); for a base :func:`.sources.base_time`, the first epoch of the rover, else that shot.
        None when nothing tells, :data:`_WAIT` while what tells is being read."""
        rover = self.rover.path()
        if rover is not None and path == rover and self._asks_day():
            when = self._day()
            return (when - _GPS_START).total_seconds() if isinstance(when, datetime) else None
        if rover is not None and path != rover:
            info = self._observations(rover)
            if isinstance(info, _Pending):
                return _WAIT
            if isinstance(info, rinex.RinexInfo):
                when = sources.first_epoch(info)
                if when is not None:
                    return when
        if self.kind() == "mrk":
            return self.shots[0].gps_time if self.shots else None
        shotset = self._file_shots()
        if shotset is sw.RUNNING:
            return _WAIT
        return sources.first_shot(shotset) if isinstance(shotset, gshots.ShotSet) else None

    def _convert(self, path: Path, stamp: tuple):
        """The header of the conversion to RINEX of the raw log ``path`` (done once, in a thread), the text that says
        why it cannot be converted, or a :class:`_Pending` while it is."""
        when = self._approx_time(path)
        if when is _WAIT:
            return _Pending(path, converting=True)
        if when is None and path == self.rover.path() and self._asks_day():  # nothing to convert with: said here
            typed = self._day()
            return typed if isinstance(typed, str) else tr(
                "Indica il giorno del volo: un log {0} porta solo il tempo della settimana.",
                _RAW_NAMES.get(convert.raw_kind(path), "RTCM 3"))
        progress = self._progress

        def work():
            done = convert.to_rinex(path, approx_time=when, progress=progress)
            return done, rinex.header(done.obs)

        day = None if when is None else int(when // 86400.0)
        got = self._aside.get(("convert", str(path), stamp, day), work)
        if got is sw.RUNNING:
            return _Pending(path, converting=True)
        if isinstance(got, Exception):
            return engine(str(got))
        self._converted[str(path)] = (stamp, got[0])
        return got[1]

    def _conversion(self, path: Path | None):
        """The :class:`.convert.Converted` of the raw log ``path`` as it is now, None when it is not converted."""
        known = self._converted.get(str(path)) if path is not None else None
        return known[1] if known is not None and known[0] == _stamp(path) else None

    def _conversion_lines(self, paths: list) -> list[str]:
        """What the conversion of each raw log among ``paths`` gave: its kind, epochs and events, the position of the
        station an RTCM 3 log carries, its warnings."""
        lines = []
        for path in paths:
            done = self._conversion(path)
            if done is None:
                continue
            text = tr("Log {0} convertito: {1} epoche, {2} eventi", _RAW_NAMES.get(done.kind, done.kind), done.epochs,
                      done.events)
            if done.station_position is not None:
                text += tr(", posizione della stazione dall'RTCM")
            lines.append((text if len(paths) == 1 else tr("{0}: {1}", path.name, text)) + ".")
            lines += [sw.said(w) for w in done.warnings]
        return lines

    def _observations(self, path: Path | None):
        """The header of the observations in ``path`` - of its conversion to RINEX, for a raw log -, the text that
        says why they cannot be used, or a :class:`_Pending` while it is read or converted in the background; None
        without a file."""
        if path is None:
            return None
        raw = self._is_raw(path)
        if raw is not None:
            return self._convert(path, raw)
        try:
            info = self._read(path)
        except _Reading:
            return _Pending(path)
        except FileNotFoundError:
            return tr("Il file non esiste.")
        except (OSError, ValueError) as exc:
            return tr("Il file non si legge: {0}", sw.unread(exc))
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

    def _read_mrk(self) -> None:
        """The shots of the MRK named in the field of the shots, read once while the file stays the same; none, and
        no problem, for another source."""
        path = self.shots_file.path() if self.kind() == "mrk" else None
        if path is None:
            self.shots, self._mrk_problem, self._mrk_read = [], None, None
            return
        key = (str(path), _stamp(path))
        if self._mrk_read is None or self._mrk_read[0] != key:
            try:
                shots = dji.read_mrk(path)
                problem = None if shots else tr("Il file non ha scatti.")
            except (OSError, ValueError) as exc:
                shots, problem = [], tr("Il file non si legge: {0}", sw.unread(exc))
            self._mrk_read = (key, shots, problem)
        self.shots, self._mrk_problem = self._mrk_read[1], self._mrk_read[2]

    def _fill_cameras(self) -> None:
        """The cameras of an ArduPilot log with several (:func:`.autopilot.cameras`, read in a thread) in their
        combo; one entry for any other log."""
        path = self.shots_file.path()
        stamp = _stamp(path) if path is not None and self.kind() == "log" else None
        cameras = []
        if stamp is not None and not sources.is_ulog(path):
            cameras = self._aside.get(("cameras", str(path), stamp), lambda: autopilot.cameras(path))
        cameras = cameras if isinstance(cameras, list) and len(cameras) > 1 else []
        wanted = [int(entry["instance"]) for entry in cameras] or [None]
        if [self.camera.itemData(i) for i in range(self.camera.count())] != wanted:
            self.camera.blockSignals(True)
            self.camera.clear()
            for entry in cameras:
                self.camera.addItem(tr("Camera {0}: {1} CAM, {2} TRIG", entry["instance"], entry["cam"],
                                       entry["trig"]), int(entry["instance"]))
            if not cameras:
                self.camera.addItem(tr("una sola"), None)
            self.camera.blockSignals(False)

    def _pairing(self, shotset):
        """The photos of the chunk paired with ``shotset`` (:func:`.shots.pair`, in a thread): a
        :class:`.shots.Pairing`, :data:`.shots_widgets.RUNNING`, the exception that says why they cannot be paired;
        None without a chunk or without the folder of the photos."""
        chunk, folder = self.chunk, self._photos()
        if chunk is None or folder is None:
            return None
        by, latency = self.by.currentData(), self._latency_s()
        return self._aside.get(("pair", id(shotset), str(folder), by, latency),
                               lambda: gshots.pair(chunk, shotset, folder, by=by, latency_s=latency))

    def _describe_shots(self, rover_info) -> str:
        """What the source of the shots holds: for an MRK as it always was; for the others the events (or shots) and
        their span, those outside the observations of the drone, the photos they pair with, the warnings."""
        kind = self.kind()
        if kind == "mrk":
            return self._describe_mrk(rover_info) if self.shots_file.path() is not None else ""
        shotset = self._shotset(rover_info)
        if shotset is None:
            if kind == "events" or (kind == "ubx" and self.shots_file.path() is None):
                return (tr("Gli eventi vengono letti dal file del rover: le osservazioni del drone.")
                        if kind == "events" else
                        tr("Gli eventi vengono letti dal log del rover; oppure scegli un altro log u-blox."))
            return (tr("Il log dell'autopilota del volo: ArduPilot (.bin, .log) o PX4 (.ulg).") if kind == "log" else
                    tr("Il file di eventi che RTKLIB scrive accanto alla soluzione (*_events.pos)."))
        source = self.shots_file.path() or self.rover.path()
        if shotset is sw.RUNNING:
            return tr("Lettura di {0} in corso...", source.name)
        if isinstance(shotset, Exception):
            return tr("Il file non si legge: {0}", sw.unread(shotset))
        times = [s.gps_time for s in shotset.shots if s.valid]
        text = (tr("{0} scatti ({1} con un tempo valido)", len(shotset.shots), len(times)) if kind == "log" else
                tr("{0} eventi ({1} con un tempo valido)", len(shotset.shots), len(times)))
        if times:
            text += tr(", dal {0} al {1} (ora GPS)", _when(min(times)), _when(max(times)))
        text += "."
        span = _span(rover_info)
        if span is not None:
            outside = sum(1 for t in times if not span[0] - _SLACK_S <= t <= span[1] + _SLACK_S)
            if 0 < outside < len(times):
                text += " " + tr("Scatti fuori dalle osservazioni del drone: {0}; le loro foto terranno la posizione "
                                 "che hanno.", outside)
        lines = [text] + [sw.said(w) for w in shotset.warnings]
        lever = sw.lever_words(self._lever(), self._latency_ms())
        if lever is not None:
            lines.append(lever + ".")
        if times and self.chunk is not None:
            pairing = self._pairing(shotset)
            if pairing is None:
                lines.append(tr("La cartella delle foto di questi scatti viene chiesta all'avvio del calcolo."))
            elif pairing is sw.RUNNING:
                lines.append(tr("Abbinamento delle foto in corso..."))
            elif isinstance(pairing, Exception):
                lines.append(sw.said(str(pairing)))
            else:
                lines.append(sw.pairing_text(pairing))
        return "\n".join(lines)

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
            if self.kind() != "mrk":
                return tr("Il RINEX del drone, o il log del suo ricevitore (u-blox, RTCM 3, Septentrio SBF, Unicore, "
                          "Trimble RT-17), che il programma converte in RINEX.")
            return tr("Il RINEX del drone: per un drone DJI il file *_Rinex.obs accanto al Timestamp.MRK.")
        if isinstance(info, _Pending):
            if info.converting:
                return tr("Conversione di {0} in RINEX in corso...", info.path.name)
            return tr("Lettura di {0} in corso...", info.path.name)
        if isinstance(info, str):
            return info
        return "\n".join(self._conversion_lines([self.rover.path()]) + [
            tr("Ricevitore {0}; osservazioni dal {1} al {2} (ora GPS).", _receiver(info), _when(info.first),
               _when(info.last))])

    def _describe_base(self, paths: list[Path], infos: list) -> str:
        if self.mode() == "station":
            return tr("La stazione permanente scelta fa da base: le sue osservazioni per le ore del volo vengono "
                      "scaricate, non serve un file della base.")
        if not paths:
            return tr("Il RINEX di una base che registrava durante tutto il volo: un file, o più file (Aggiungi...) "
                      "che insieme lo coprono; la tua base, o una stazione permanente vicina.")
        pending = next((info for info in infos if isinstance(info, _Pending)), None)
        if pending is not None:
            if pending.converting:
                return tr("Conversione di {0} in RINEX in corso...", pending.path.name)
            return tr("Lettura di {0} in corso...", pending.path.name)
        wrong = next(((path, info) for path, info in zip(paths, infos) if isinstance(info, str)), None)
        if wrong is not None:
            return wrong[1] if len(paths) == 1 else tr("{0}: {1}", wrong[0].name, wrong[1])
        info = infos[0]
        span = self._joined_span(infos)
        first, last = ((info.first, info.last) if span is None else
                       (_GPS_EPOCH + timedelta(seconds=span[0]), _GPS_EPOCH + timedelta(seconds=span[1])))
        name = ppk.base_name(paths[0], info)
        lines = self._conversion_lines(paths)
        if len(paths) == 1:
            lines.append(tr("Base {0}: ricevitore {1}, antenna {2}; osservazioni dal {3} al {4} (ora GPS).", name,
                            _receiver(info), _antenna(info), _when(first), _when(last)))
        else:
            lines.append(tr("Base {0}: {1} file, ricevitore {2}, antenna {3}; osservazioni dal {4} al {5} (ora GPS).",
                            name, len(paths), _receiver(info), _antenna(info), _when(first), _when(last)))
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
                      "calibrazioni dell'IGS (igs20.atx), se l'antenna vi è: il registro del calcolo avvisa quando "
                      "non vi è o quando RTKLIB usa la voce di un altro radome, e il rapporto PDF dice la voce usata.",
                      kind)
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

    def _follow_mrk(self) -> None:
        """The observations of the drone beside the MRK named now (:func:`rover_beside`), while the field holds the
        ones found beside another MRK, or nothing: what the user named stays."""
        path = self.shots_file.path() if self.kind() == "mrk" else None
        text = self.rover.edit.text().strip()
        if path is None or not path.is_file() or text not in ("", self._rover_beside):
            return
        found = rover_beside(path)
        self._rover_beside = str(found) if found else ""
        if self._rover_beside != text:
            self.rover.edit.setText(self._rover_beside)

    def _update(self) -> None:
        self._follow_mrk()
        self._read_mrk()
        self._fill_cameras()
        self._show_kind(self.kind())
        rover, bases = self.rover.path(), self.base.paths()
        station = self.mode() == "station"
        rover_info, base_infos = self._observations(rover), ([] if station else self._base_headers())
        self.day_row.setVisible(self._asks_day())
        self.shots_info.setText(self._describe_shots(rover_info))
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
            done = self._conversion(obs)
            if done is not None:  # a raw log: the navigation messages it carried
                found += [p for p in ([Path(done.nav)] if done.nav else []) if p.is_file() and p not in found]
                continue
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
        wrong with it; None in another frame, and when none is written: then the run takes the middle of the drone's
        observations (:func:`.base.flight_epoch`, as ``ppg gnss ppk`` does)."""
        if self.frame.currentData() not in frames.DYNAMIC or not self.epoch.text().strip():
            return None
        try:
            return frames.parse_epoch(self.epoch.text())
        except ValueError:
            return tr("Scrivi l'epoca della posizione in ITRF2020: un anno decimale fra 1980 e 2100 (2026,82) o una "
                      "data (2026-10-27), o lasciala vuota per la metà delle osservazioni del drone.")

    def _timed_shots(self) -> list:
        """The shots of a source other than an MRK that have a valid time, as far as they are read."""
        shotset = self._shotset(self._observations(self.rover.path())) if self.kind() != "mrk" else None
        return [s for s in shotset.shots if s.valid] if isinstance(shotset, gshots.ShotSet) else []

    def _flight_place(self) -> tuple[float, float] | None:
        """Where the drone flew: the median latitude and longitude of the shots of the MRK; for another source those
        of the antenna at its shots (an autopilot log), else the approximate position in the header of the drone's
        observations; None when nothing tells."""
        places = [(shot.lat, shot.lon) for shot in self.shots]
        if self.kind() != "mrk":
            places = [(s.antenna[0], s.antenna[1]) for s in self._timed_shots() if s.antenna is not None]
            info = self._observations(self.rover.path())
            if not places and isinstance(info, rinex.RinexInfo) and info.approx_xyz is not None:
                lat, lon, _h = rinex.ecef_to_llh(info.approx_xyz)
                return lat, lon
        if not places:
            return None
        lats, lons = sorted(lat for lat, _ in places), sorted(lon for _, lon in places)
        middle = len(lats) // 2
        if len(lats) % 2:
            return lats[middle], lons[middle]
        return (lats[middle - 1] + lats[middle]) / 2.0, (lons[middle - 1] + lons[middle]) / 2.0

    def _flight_epoch(self) -> float | None:
        """The epoch a typed position in ITRF2020 takes when none is written: the middle of the drone's observations
        as the header of its file says them (:func:`.base.middle_epoch`); None while it is not known."""
        info = self._observations(self.rover.path())
        if not isinstance(info, rinex.RinexInfo) or info.first is None or info.last is None:
            return None
        return gbase.middle_epoch(info.first, info.last)

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
            self.error.setText(tr("Il programma non sa dove è il rilievo: il Timestamp.MRK non ha scatti.")
                               if self.kind() == "mrk" else
                               tr("Il programma non sa dove è il rilievo: scegli prima le osservazioni del drone."))
            return
        when = min(self.shots, key=lambda shot: shot.gps_time).utc if self.shots else None
        timed = self._timed_shots()
        if timed:
            when = gshots.utc(min(timed, key=lambda shot: shot.gps_time))
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
        """The frame of the typed position: in ITRF2020 its epoch is asked (:meth:`_update_mode`)."""
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
        if self.epoch_row.isVisibleTo(self):  # left empty: the middle of the drone's observations, said when known
            epoch = self._flight_epoch()
            self.epoch.setPlaceholderText(
                tr("vuota: {0}, la metà delle osservazioni del drone", decimal_comma(f"{epoch:.4f}"))
                if epoch is not None else
                tr("anno decimale (2026,82) o data (2026-10-27); vuota: la metà delle osservazioni del drone"))
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
        kind = self.kind()
        if kind in _WITH_FILE and self.shots_file.path() is None:
            return tr("Scegli il file degli scatti.")
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
        if kind != "mrk":
            shotset = self._shotset(rover_info)
            source = self.shots_file.path() or rover
            if shotset is sw.RUNNING or shotset is None:
                return tr("Il programma sta ancora leggendo {0}: attendi un momento.", source.name)
            if isinstance(shotset, Exception):
                return tr("Gli scatti non si possono usare: {0}", sw.unread(shotset))
            times = [s.gps_time for s in shotset.shots if s.valid]
            if not times:
                return tr("Nessuno scatto con un tempo valido in {0}.", source.name)
            if rover_span and not any(rover_span[0] - _SLACK_S <= t <= rover_span[1] + _SLACK_S for t in times):
                return tr("Gli scatti (dal {0} al {1}) sono fuori dalle osservazioni del drone (dal {2} al {3}): non "
                          "sono dello stesso volo.", _when(min(times)), _when(max(times)), _when(rover_info.first),
                          _when(rover_info.last))
            pairing = self._pairing(shotset) if self.chunk is not None else None
            if pairing is sw.RUNNING:
                return tr("Il programma sta ancora abbinando le foto agli scatti: attendi un momento.")
            if isinstance(pairing, Exception):
                return engine(str(pairing))
            if pairing is not None and not pairing.kept:
                return tr("Nessuna foto del chunk trova il suo scatto.")
        elif rover_span and not any(rover_span[0] - _SLACK_S <= t <= rover_span[1] + _SLACK_S for t in times):
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
        ``ValueError`` for a typed position or epoch that is not one (see :meth:`problem`).

        This is the whole answer for the Timestamp.MRK the dialog was opened with; for any other source of the shots
        (and for another MRK named in the field) the keys of :meth:`shots_values` are there too. A raw log is
        answered as it was named: the run converts it (at once: the conversion is kept); ``"approx_time"`` (a datetime
        in GPS time) is there when the day of the flight was asked and typed (:meth:`_asks_day`): the approximate time
        of that conversion."""
        mode = self.mode()
        typed = frame = epoch = None
        if mode == "typed":
            typed, frame, epoch = self._typed_position(), self.frame.currentData(), self._typed_epoch()
            for wrong in (typed, epoch):
                if isinstance(wrong, str):
                    raise ValueError(wrong)
        bases = self.base.paths()
        base = None if mode == "station" else (bases[0] if len(bases) == 1 else (bases or None))
        out = {"rover": self.rover.path(), "base": base, "navs": self.nav_paths(), "base_position": typed,
               "replace_fix": self.replace_fix.isChecked(), "base_mode": mode, "base_frame": frame,
               "base_epoch": epoch, "station": self.station if mode in _WITH_STATION else None,
               "atx": "download" if self.atx_download.isChecked() else "auto"}
        if self.kind() != "mrk" or self.shots_file.path() != self.mrk:
            out.update(self.shots_values())
        day = self._day()
        if isinstance(day, datetime):
            out["approx_time"] = day
        return out

    def shots_values(self) -> dict:
        """``{"shots", "lever_body_mm", "latency_ms", "by"}``: the source of the shots - ``{"kind"`` (one of
        :data:`.sources.KINDS`), ``"file"`` (None for the events of the rover, and for a u-blox rover with its own time
        marks), ``"edge"``, ``"message"``, ``"camera"``, ``"photos"}`` (the folder of the photos, None for an MRK and
        when the window is to ask) -, the lever arm from the antenna to the camera in the body of the drone (mm
        forward, right, down; None when all zero), the latency of the camera in milliseconds - both as typed, with
        their panel open or closed -, how the photos are paired (:data:`.shots.BY`)."""
        kind = self.kind()
        return {"shots": {"kind": kind, "file": None if kind == "events" else self.shots_file.path(),
                          "edge": self.edge.currentData() if kind == "ubx" else events.EDGES[0],
                          "message": self.message.currentData() if kind == "log" else "auto",
                          "camera": self._camera(), "photos": self._photos()},
                "lever_body_mm": self._lever(), "latency_ms": self._latency_ms(),
                "by": self.by.currentData() if kind != "mrk" else "auto"}

    def accept(self) -> None:
        problem = self.problem()
        self.error.setText(problem or "")
        if problem is None:
            super().accept()
        else:
            self._fit()

    @classmethod
    def ask(cls, parent, mrk, work=None, chunk=None) -> dict | None:
        """The answer of the dialog for the file ``mrk`` (a Timestamp.MRK, or what else tells the shots:
        :func:`opening_kind`), the work folder ``work`` and the chunk ``chunk`` (its last run, its photos:
        :class:`PpkDialog`) (:meth:`values`), None when it is cancelled."""
        dialog = cls(mrk, parent, work, chunk)
        try:
            return dialog.values() if dialog.exec() == QDialog.DialogCode.Accepted else None
        finally:
            dialog.deleteLater()
