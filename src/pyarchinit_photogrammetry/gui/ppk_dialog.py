"""The files of a PPK run and the position of its base, asked before RTKLIB runs: the Timestamp.MRK and its shots, the
drone's RINEX found beside it, the base's RINEX with what its header says, the navigation files beside them, and the
base at the position of its RINEX header - with the warning that it can be metres off - or at a position typed in;
whether the photos with an RTK fix of the drone itself take the PPK position too. The dialog starts from the last run
of the chunk. What can be checked before the run is checked here: the files, their times, the work folder of the
PPK."""
from __future__ import annotations

import math
import os
import re
from datetime import timedelta
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget)

from ..gnss import dji, ppk, rinex
from ..gnss.apply import onboard_fix
from .gnss_pane import length
from .i18n import number, tr

_DJI_ROVER = "_rinex.obs"  # what a DJI drone writes beside its <flight>_Timestamp.MRK: <flight>_Rinex.obs
_DJI_MRK = "_timestamp.mrk"
_OBSERVATIONS = re.compile(r"\.(?:obs|\d\do)$", re.IGNORECASE)  # *.obs, *.24o
_GPS_EPOCH = dji.GPS_EPOCH.replace(tzinfo=None)
TYPING_PAUSE_MS = 300  # the files named in a field are read this long after the last key pressed in it
_SLACK_S = 1.0  # a shot this close to the first or last epoch of the drone counts as within its observations
_WARNING = "color: #b45309"
_NOTE = "color: #6c7a89"


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
    """The files and the base of a PPK run: the shots of the ``mrk`` (read at once), the drone's observations (found
    beside it), the base's observations with what their header says - and that the phase-centre offset of its antenna
    is not applied - the navigation files (found beside both, taken away or added by hand), the position of the base:
    the one of the header of its file, with the warning that it can be metres off, or the surveyed one of its mark,
    typed (a warning when it is far from the header's, and when the same base was typed elsewhere for other photos of
    ``chunk``), and whether the photos with an RTK fix of the drone itself take the PPK position too. ``work`` is the
    work folder of the PPK, which no input may be in. *OK* checks everything that can be checked before the run and
    says in the dialog what is missing or wrong.

    With ``chunk`` the dialog starts from its last PPK run (:func:`.ppk.last_run`): the base, its position typed or
    from its header, the navigation files still there, the choice about the onboard fixes, and the drone's
    observations too when the MRK is the one of that run. The files named in the fields are read after a pause in
    typing, once while they stay the same; the dialog is as tall as what it shows (the form scrolls only on a screen
    too short for it)."""

    def __init__(self, mrk, parent=None, work=None, chunk=None):
        super().__init__(parent)
        self.mrk = Path(mrk)
        self.work = Path(work) if work is not None else None
        self.chunk = chunk
        self.setWindowTitle(tr("Correzioni PPK"))
        self.setMinimumWidth(600)
        self._headers: dict[str, tuple] = {}  # path -> (its time and size, its header, the error reading it)
        self._beside: dict[str, tuple] = {}  # observation file -> (time of its folder, navigation files beside it)
        self._added: list[Path] = []  # navigation files added by hand
        self._removed: set[Path] = set()  # navigation files taken away: not listed again when found beside a file
        self._sources: tuple | None = None  # the rover and base the navigation files were listed for
        self._flight: set | None = None  # the keys of the cameras in the folder of the MRK (this run replaces them)
        try:
            self.shots = dji.read_mrk(self.mrk)
            self._mrk_problem = None if self.shots else tr("Il file non ha scatti.")
        except (OSError, ValueError) as exc:
            self.shots, self._mrk_problem = [], tr("Il file non si legge: {0}", exc)
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
        observations = tr("Osservazioni RINEX (*.obs *.??o *.rnx *.gz);;Tutti i file (*)")
        rover = rover_beside(self.mrk)
        self.rover = _FileField(str(rover) if rover else "", tr("Osservazioni del drone"), observations,
                                self.mrk.parent)
        self.rover_info = _label()
        form.addRow(tr("Osservazioni del drone"), _stack(self.rover, self.rover_info))
        self.base = _FileField("", tr("Osservazioni della base"), observations, self.mrk.parent)
        self.base_info = _label()
        self.antenna_warning = _label(_WARNING)  # the phase-centre offset of the base's antenna is not applied
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
        self.lat, self.lon, self.ellh = QLineEdit(), QLineEdit(), QLineEdit()
        for edit, hint in ((self.lat, tr("Latitudine (gradi)")), (self.lon, tr("Longitudine (gradi)")),
                           (self.ellh, tr("Quota ellissoidica (m)"))):
            edit.setPlaceholderText(hint)
            edit.setToolTip(hint)
            coordinates.addWidget(edit)
        pos_lay.addLayout(coordinates)
        note = _label()
        note.setText(tr("Latitudine e longitudine in gradi; la quota in metri sull'ellissoide (quella che dà il "
                        "GNSS), non sul livello del mare."))
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
        pos_lay.addStretch(1)
        form.addRow(tr("Posizione della base"), position)

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
        self.typed.toggled.connect(lambda _on: self._update_position())
        for edit in (self.lat, self.lon, self.ellh):
            edit.textChanged.connect(lambda _text: self._update_position())
        self.b_add_nav.clicked.connect(self._add_navs)
        self.b_remove_nav.clicked.connect(self._remove_navs)
        self._update()

    # ------------------------------------------------------------ the last run of the chunk
    def _prefill(self, last: dict | None) -> None:
        """The answers of the last PPK run of the chunk: the base file, its position typed or from its header, the
        navigation files still there (but the ones beside the drone's observations of another flight), the choice
        about the onboard fixes; the drone's observations too when the MRK is the one of that run."""
        if not last:
            return
        same = _same_file(last.get("mrk"), self.mrk)
        rover = last.get("rover")
        if same and rover and Path(rover).is_file():
            self.rover.edit.setText(rover)
        if last.get("base"):
            self.base.edit.setText(last["base"])
        drone = Path(rover).parent if rover else None
        for text in last.get("navs") or []:
            path = Path(text)
            if path.is_file() and path not in self._added and (same or drone is None or path.parent != drone):
                self._added.append(path)
        position = last.get("position")
        if last.get("position_source") == "typed" and position and len(position) == 3:
            lon, lat, h = (float(v) for v in position)
            self.typed.setChecked(True)
            for edit, value in ((self.lat, lat), (self.lon, lon), (self.ellh, h)):
                edit.setText(repr(value))
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
        is a ``FileNotFoundError``."""
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        known = self._headers.get(str(path))
        if known is None or known[0] != stamp:
            try:
                known = (stamp, rinex.header(path), None)
            except (OSError, ValueError) as exc:
                known = (stamp, None, exc)
            self._headers[str(path)] = known
        if known[2] is not None:
            raise known[2].with_traceback(None)
        return known[1]

    def _observations(self, path: Path | None):
        """The header of the observations in ``path``, or the text that says why they cannot be used; None without
        a file."""
        if path is None:
            return None
        try:
            info = self._read(path)
        except FileNotFoundError:
            return tr("Il file non esiste.")
        except (OSError, ValueError) as exc:
            return tr("Il file non si legge: {0}", exc)
        if info.kind != "obs":
            return tr("Questo file ha messaggi di navigazione, non osservazioni.")
        return info

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
        if isinstance(info, str):
            return info
        return tr("Ricevitore {0}; osservazioni dal {1} al {2} (ora GPS).", info.receiver or "-", _when(info.first),
                  _when(info.last))

    def _describe_base(self, path: Path | None, info) -> str:
        if info is None:
            return tr("Il RINEX di una base che registrava durante tutto il volo, in un solo file: la tua o una "
                      "stazione permanente vicina.")
        if isinstance(info, str):
            return info
        lines = [tr("Base {0}: ricevitore {1}, antenna {2}; osservazioni dal {3} al {4} (ora GPS).",
                    ppk.base_name(path, info), info.receiver or "-", info.antenna or "-", _when(info.first),
                    _when(info.last))]
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

    @staticmethod
    def _antenna_text(info) -> str:
        """That the offset of the phase centre of the base's antenna is not applied, naming the antenna of its header;
        nothing without the header of a base."""
        if not isinstance(info, rinex.RinexInfo):
            return ""
        kind = ppk.antenna_type(info)
        if kind:
            return tr("Lo scostamento del centro di fase dell'antenna {0} della base non viene applicato (le "
                      "calibrazioni delle antenne arriveranno in una versione successiva): le quote possono risultare "
                      "più basse di qualche centimetro, oltre 10 cm con alcune antenne choke ring.", kind)
        return tr("L'intestazione della base non dice il tipo della sua antenna: lo scostamento del suo centro di fase "
                  "non viene applicato (le calibrazioni delle antenne arriveranno in una versione successiva), e le "
                  "quote possono risultare più basse di qualche centimetro, oltre 10 cm con alcune antenne choke ring.")

    def _typing(self) -> None:
        """A file name is being typed: the files are read after a pause."""
        self.error.setText("")
        self._timer.start()

    def refresh(self) -> None:
        """Read what the files named so far hold now, without waiting for the pause in typing."""
        self._timer.stop()
        self._update()

    def _update(self) -> None:
        rover, base = self.rover.path(), self.base.path()
        rover_info, base_info = self._observations(rover), self._observations(base)
        self.mrk_info.setText(self._describe_mrk(rover_info))
        self.rover_info.setText(self._describe_rover(rover_info))
        self.base_info.setText(self._describe_base(base, base_info))
        antenna = self._antenna_text(base_info)
        self.antenna_warning.setText(antenna)
        self.antenna_warning.setVisible(bool(antenna))
        if (rover, base) != self._sources:
            self._sources = (rover, base)
            self._fill_navs()
        self._update_position()

    # ------------------------------------------------------------ navigation files
    def _companions(self, obs: Path | None) -> list[Path]:
        """The RINEX navigation files beside the observation file ``obs``, looked for again only when its folder
        changes."""
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
            known = (stamp, [p for p in rinex.find_companions(obs) if ppk.navigation_info(p, self._read)])
            self._beside[str(obs)] = known
        return known[1]

    def _fill_navs(self) -> None:
        """The navigation files beside the rover and then beside the base, without those taken away, and those added."""
        found: list[Path] = []
        for obs in self._sources or ():
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
            self._removed.add(path)  # also when it is found beside a file too: away at the first click
        self._fill_navs()

    # ------------------------------------------------------------ the position of the base
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

    def _update_position(self) -> None:
        typed = self.typed.isChecked()
        for edit in (self.lat, self.lon, self.ellh):
            edit.setEnabled(typed)
        self.header_warning.setVisible(not typed)
        far = self._far_text() if typed else ""
        self.far_warning.setText(far)
        self.far_warning.setVisible(bool(far))
        reuse = self._reuse_text() if typed else ""
        self.reuse_warning.setText(reuse)
        self.reuse_warning.setVisible(bool(reuse))
        self.error.setText("")
        self._fit()

    # ------------------------------------------------------------ the answer
    def problem(self) -> str | None:
        """What keeps the run from starting, in words; None when everything is there. The files typed so far are
        read first."""
        if self._timer.isActive():
            self.refresh()
        if self._mrk_problem:
            return tr("Il Timestamp.MRK non si può usare: {0}", self._mrk_problem)
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
        rover_span, base_span = _span(rover_info), _span(base_info)
        if rover_span and base_span and max(rover_span[0], base_span[0]) > min(rover_span[1], base_span[1]):
            return tr("Il drone (dal {0} al {1}) e la base (dal {2} al {3}) non hanno ore in comune: la base deve "
                      "registrare mentre il drone vola.", _when(rover_info.first), _when(rover_info.last),
                      _when(base_info.first), _when(base_info.last))
        times = [shot.gps_time for shot in self.shots]
        if rover_span and not any(rover_span[0] - _SLACK_S <= t <= rover_span[1] + _SLACK_S for t in times):
            return tr("Gli scatti del Timestamp.MRK (dal {0} al {1}) sono fuori dalle osservazioni del drone (dal {2} "
                      "al {3}): non sono dello stesso volo.", _when(min(times)), _when(max(times)),
                      _when(rover_info.first), _when(rover_info.last))
        navs = self.nav_paths()
        if not navs:
            return tr("Nessun file di navigazione: aggiungi quello della base o del drone (.nav, .##n, .##p, "
                      "*_MN.rnx).")
        for path in navs:
            if not path.exists():
                return tr("{0} non esiste più.", path.name)
            if not ppk.navigation_info(path, self._read):
                return tr("{0} non è un file di navigazione RINEX.", path.name)
        if self.work is not None:
            for path in (rover, base, *navs):
                if ppk.inside(path, self.work):
                    return tr("{0} è nella cartella di lavoro del PPK ({1}), i cui file si sostituiscono a ogni "
                              "calcolo: spostalo fuori da lì.", path.name, str(self.work))
        if self.typed.isChecked():
            typed = self._typed_position()
            return typed if isinstance(typed, str) else None
        if base_info.approx_xyz is None:
            return tr("Il file della base non ha una posizione nell'intestazione: scrivi la posizione misurata della "
                      "base.")
        return None

    def values(self) -> dict:
        """``{"rover", "base", "navs", "base_position", "replace_fix"}``: the files, the position typed (latitude,
        longitude, height above the ellipsoid, as typed) or None for the one of the header of the base's file, and
        whether the photos with an RTK fix of the drone itself take the PPK position too; a ``ValueError`` for a typed
        position that is not one (see :meth:`problem`)."""
        typed = self._typed_position() if self.typed.isChecked() else None
        if isinstance(typed, str):
            raise ValueError(typed)
        return {"rover": self.rover.path(), "base": self.base.path(), "navs": self.nav_paths(), "base_position": typed,
                "replace_fix": self.replace_fix.isChecked()}

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
