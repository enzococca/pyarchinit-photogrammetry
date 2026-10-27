"""The GNSS positions of the photos as a pane that stays open: where the positions come from, their PPK correction
against a base, the reference frames and epochs of the positions and the coordinate system of the chunk with the kind of
its heights, how good they are flight by flight, and the choice to trust only the RTK fixes."""
from __future__ import annotations

from collections import Counter

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHeaderView, QLabel, QPushButton, QScrollArea,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import _native
from ..core import crs as crs_mod
from ..gnss import dji, frames, heights, ppk
from ..gnss.apply import ONLY_FIX
from ..gnss.quality import mixed_heights, summary
from .flow_layout import FlowLayout
from .i18n import decimal_comma, number, tr
from .icons import icon

GEOID_MB = round(heights.GEOID_BYTES / 1e6)  # the size of the grid of EGM2008 the window says (81 MB)


def source_names() -> dict[str, str]:
    """Where a position can come from (``meta["gnss"]["source"]``), as the pane says it."""
    return {"exif": tr("EXIF"), dji.SOURCE_XMP: tr("XMP di DJI"), dji.SOURCE_MRK: tr("MRK di DJI"), "ppk": tr("PPK"),
            "positions_file": tr("file di posizioni"), "video": tr("video"), "panorama": tr("foto a 360°"),
            "unknown": tr("sconosciuta")}


def status_names() -> dict[str, str]:
    """The states of a position (``meta["gnss"]["status"]``) in running text."""
    return {"fix": tr("fix"), "float": tr("float"), "single": tr("singola"), "none": tr("senza RTK"),
            "unknown": tr("stato sconosciuto")}


def height_names() -> dict[str, str]:
    """The kinds of height of a position (``meta["gnss"]["height"]``) as the pane says them."""
    return {"ellipsoidal": tr("ellissoidica"), "exif": tr("EXIF"), "unknown": tr("sconosciuta")}


def lengths(values) -> str:
    """Lengths in metres (one per axis) as ``1.2 / 1.2 / 2.4 cm``: in centimetres when all are under a metre, else
    in metres; ``-`` for none."""
    if not values:
        return "-"
    centimetres = max(values) < 1.0
    k = 100.0 if centimetres else 1.0
    return " / ".join(number(v * k, 1 if centimetres else 2) for v in values) + (" cm" if centimetres else " m")


def length(metres: float) -> str:
    """One distance in metres as a sentence says it: ``12 cm`` under a metre, else ``3,45 m``."""
    return f"{number(metres * 100.0, 0)} cm" if metres < 1.0 else f"{number(metres, 2)} m"


def year(epoch: float, decimals: int = 2) -> str:
    """An epoch (a decimal year) as the interface writes it: ``2026,82``."""
    return decimal_comma(f"{float(epoch):.{decimals}f}")


def frame_text(frame: str | None, epoch: float | None = None) -> str:
    """The reference frame of a position, and the epoch of its coordinates in one that moves with time, as a sentence
    says them: ``sistema sconosciuto (WGS 84)``, ``ITRF2020 all'epoca 2026,82``, ``ETRF2000 (RDN2008)``."""
    if frame in (None, frames.UNKNOWN, frames.WGS84):
        return tr("sistema sconosciuto (WGS 84)")
    name = tr("ETRF2000 (RDN2008)") if frame == "ETRF2000" else str(frame)
    return tr("{0} all'epoca {1}", name, year(epoch)) if epoch is not None and frame in frames.DYNAMIC else name


def base_source_text(base: dict) -> str:
    """Where the position of a base comes from (``base`` is ``Solution.base``, kept in every photo it gave a
    position): the header of its RINEX file, typed, a permanent station and the solution of its coordinates, a static
    baseline from a station, a static PPP and its products."""
    source, detail = base.get("position_source"), base.get("detail") or {}
    if source == "header":
        return tr("posizione dell'intestazione RINEX")
    if source == "typed":
        return tr("posizione scritta")
    if source == "station":
        coordinates = detail.get("coordinates") or {}
        return tr("coordinate della stazione permanente {0} ({1})", detail.get("station") or base.get("name") or "?",
                  coordinates.get("source") or "-")
    if source == "baseline":
        return tr("calcolata con una linea di base dalla stazione {0}", detail.get("station") or "?")
    if source == "ppp":
        products = " ".join(str(v) for v in (detail.get("centre"), detail.get("kind")) if v)
        return tr("calcolata con il PPP statico ({0})", products or "-")
    return tr("posizione di origine sconosciuta")


def ppk_tip() -> str:
    """What *Calcola PPK* (and *Strumenti > GNSS > Correzioni PPK*) does, as its tooltip says it."""
    return tr("Il RINEX del drone e il Timestamp.MRK del volo, il RINEX di una base che registrava durante il volo (la "
              "tua o una stazione permanente) e i file di navigazione: RTKLIB calcola la traiettoria dell'antenna e "
              "ogni foto prende la posizione della camera al suo scatto")


def ppk_state(chunk) -> str:
    """What the PPK step says of ``chunk``: how many photos have a PPK position and of which status, the base each
    position was computed against and how its position was set - the one of its RINEX header with the warning that it
    can be metres off; else where it comes from (typed, a permanent station, a static baseline, a static PPP), its
    frame and the epoch of its coordinates; typed at positions more than a few centimetres apart, with the warning
    that one is likely wrong -; without PPK positions what the step needs, and whether RTKLIB is there to compute
    them."""
    cameras = list(chunk.cameras) if chunk is not None else []
    records = [c.meta["gnss"] for c in cameras if (c.meta.get("gnss") or {}).get("source") == "ppk"]
    if not records:
        text = tr("Nessuna posizione PPK. Servono il RINEX del drone e il Timestamp.MRK del volo, il RINEX di una base "
                  "che registrava durante il volo e i file di navigazione.")
        if not _native.ppk_available():
            text += " " + tr("Per calcolarle serve il nucleo nativo 0.5 o più recente, che contiene RTKLIB.")
        return text
    names = status_names()
    states = Counter(r.get("status") for r in records)
    detail = ", ".join(f"{names.get(k, k)} {n}" for k, n in sorted(states.items(), key=lambda p: -p[1]))
    text = tr("PPK: {0} foto su {1} ({2}).", len(records), len(cameras), detail)
    bases: dict[str, dict] = {}  # the bases of the positions, in the order of the photos: how each position was set
    typed: dict[str, list] = {}  # the typed positions of each base [lon, lat, h], each once
    for r in records:
        base = r.get("base") or {}
        name = str(base.get("name") or "?")
        bases.setdefault(name, {}).setdefault(base.get("position_source"), base)
        if base.get("position_source") == "typed" and base.get("position"):
            position = [float(v) for v in base["position"]]
            if position not in typed.setdefault(name, []):
                typed[name].append(position)
    for name, sources in bases.items():
        if "header" in sources:  # also when some of its positions came from a typed one: the warning wins
            text += " " + tr("La base {0} è nella posizione della sua intestazione RINEX, che può essere sbagliata di "
                             "metri e sposta tutto il rilievo: calcola di nuovo con la posizione misurata della base.",
                             name)
        else:  # where its position comes from, in which frame and at which epoch
            for source, base in sources.items():
                if source is not None:
                    text += " " + tr("Base {0}: {1}, {2}.", name, base_source_text(base),
                                     frame_text(base.get("frame"), base.get("epoch")))
        positions = typed.get(name) or []
        apart = max((ppk.distance_m(a, b) for i, a in enumerate(positions) for b in positions[i + 1:]), default=0.0)
        if apart > ppk.SAME_BASE_M:
            text += " " + tr("La base {0} è stata scritta in posizioni diverse in questo chunk, fino a {1} l'una "
                             "dall'altra: se è lo stesso punto, una è sbagliata; calcola di nuovo i voli con quella "
                             "giusta.", name, length(apart))
    return text


def _frames_text(chunk) -> str:
    """The reference frames of the positions of the photos of ``chunk``, how many each, with the epochs of those in a
    frame that moves with time."""
    placed = [cam for cam in chunk.cameras if cam.reference.location]
    if not placed:
        return tr("Nessuna posizione delle foto.")
    counts = Counter(frames.position_frame(cam.meta.get("gnss")) for cam in placed)
    parts = []
    for frame, n in sorted(counts.items(), key=lambda pair: -pair[1]):
        text = f"{frame_text(frame)} {n}"
        epochs = sorted(float(epoch) for cam in placed if frames.position_frame(cam.meta.get("gnss")) == frame
                        and (epoch := (cam.meta.get("gnss") or {}).get("epoch")) is not None)
        if frame in frames.DYNAMIC and epochs:
            first, last = year(epochs[0]), year(epochs[-1])
            text += " (" + (tr("epoca {0}", first) if first == last else tr("epoche dal {0} al {1}", first, last)) + ")"
        parts.append(text)
    return tr("Posizioni delle foto: {0}.", ", ".join(parts))


def _system_text(crs: str) -> str:
    """The coordinate system of a chunk and the kind of its heights."""
    if crs_mod.is_local(crs):
        return tr("Il chunk è in coordinate locali: «Sistema del rilievo...» lo mette in un sistema di coordinate.")
    try:
        name, kind = crs_mod.to_pyproj(crs).name, heights.height_kind(crs)
    except Exception:  # noqa: BLE001 - a system PROJ cannot read: its code says it
        return tr("Sistema del chunk: {0}.", crs)
    words = {heights.ELLIPSOIDAL: tr("quote ellissoidiche"), heights.ORTHOMETRIC: tr("quote sul geoide"),
             heights.NONE: tr("senza quote proprie: le quote delle posizioni passano come sono")}[kind]
    return tr("Sistema del chunk: {0} ({1}), {2}.", crs, name, words)


def _check_lines(chunk) -> list[str]:
    """What does not fit between the heights of the photos of ``chunk`` and those of its system
    (:func:`.heights.check`): its two common cases - the grid of EGM2008 missing, a system without heights of its own -
    in the words of the window, any other as the engine says it."""
    lines = []
    for text in heights.check(chunk):
        if heights.GEOID_GRID in text:
            lines.append(tr("Le quote ellissoidiche delle posizioni delle foto non diventano quote sul geoide del "
                            "sistema del chunk senza la griglia del geoide EGM2008 ({0}): scaricala con «Scarica il "
                            "geoide EGM2008» qui sopra, o con «{1}».", heights.GEOID_GRID, heights.DOWNLOAD_COMMAND))
        elif "has no heights of its own" in text:
            lines.append(tr("Le foto hanno quote ellissoidiche (GNSS) e il sistema del chunk, {0}, non ha quote "
                            "proprie: le quote del rilievo restano sull'ellissoide, 40-50 m da quelle sul livello del "
                            "mare in Italia. Con marker a quote sul livello del mare scegli un sistema con le quote sul "
                            "geoide EGM2008 (Sistema del rilievo...).", chunk.crs))
        else:
            lines.append(text)
    return lines


def heights_state(chunk, geoid: bool | None = None) -> str:
    """What the step Sistema e quote says of ``chunk``: the frames of the positions of its photos and their epochs, its
    coordinate system and the kind of its heights, whether the grid of the geoid EGM2008 is there (``geoid``: None to
    ask PROJ), and what does not fit between the heights of the photos and those of the chunk
    (:func:`.heights.check`, a sentence each)."""
    if geoid is None:
        geoid = heights.geoid_available()
    grid = tr("Griglia del geoide EGM2008 installata.") if geoid else tr("Griglia del geoide EGM2008 non installata.")
    lines = [_frames_text(chunk), _system_text(chunk.crs), grid]
    try:
        lines += _check_lines(chunk)
    except Exception as exc:  # noqa: BLE001 - a system PROJ cannot read: the pane says it, it does not stop
        lines.append(str(exc))
    return "\n".join(lines)


class GnssPane(QWidget):
    """The positions of the photos: where they come from (the RTK of the photos, a DJI ``Timestamp.MRK``, a file of
    positions), their PPK correction against a base, their reference frames and the coordinate system of the chunk
    with the kind of its heights (and the grid of the geoid), a table of the quality of each flight and the switch that
    trusts only the fixes. Each step shows its state under its buttons; the window does the work and tells the pane to
    read the chunk again."""

    MAX_ROWS = 8

    mrk_requested = Signal()
    positions_requested = Signal()
    ppk_requested = Signal()
    survey_crs_requested = Signal()
    geoid_requested = Signal()
    only_fix_toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        intro = QLabel(tr("Posizioni GNSS delle foto: l'RTK dei droni DJI, il file MRK, il PPK con una base, un file "
                          "di posizioni. Ogni posizione porta il suo stato (fix, float, singola) e la sua "
                          "accuratezza."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.states: dict[str, QLabel] = {}

        def step(row: int, key: str, title: str, widgets: list[QWidget], table: QWidget | None = None) -> None:
            # the title, the commands (on more lines when the dock is narrow), what the step has: one under the other
            t = QLabel(f"<b>{row + 1}. {title}</b>")
            t.setWordWrap(True)
            lay.addWidget(t)
            if widgets:
                holder = QWidget()
                bar = FlowLayout(holder)
                for w in widgets:
                    bar.addWidget(w)
                lay.addWidget(holder)
            if table is not None:
                lay.addWidget(table)
            s = QLabel("")
            s.setWordWrap(True)
            s.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            s.setContentsMargins(0, 0, 0, 8)
            lay.addWidget(s)
            self.states[key] = s

        self.b_mrk = QPushButton(icon("load"), tr("File MRK di DJI..."))
        self.b_mrk.setToolTip(tr("Il Timestamp.MRK della scheda del drone: la posizione di ogni scatto, con il braccio "
                                 "di leva dell'antenna, il suo stato RTK e le deviazioni standard"))
        self.b_positions = QPushButton(icon("load"), tr("File di posizioni..."))
        self.b_positions.setToolTip(tr("Un file di testo con una riga per foto: nome, x, y, quota e, se ci sono, le "
                                       "deviazioni standard o l'accuratezza e lo stato (drone costruito in casa, "
                                       "soluzione PPK di un altro programma)"))
        step(0, "positions", tr("Posizioni delle foto"), [self.b_mrk, self.b_positions])

        self.b_ppk = QPushButton(icon("run"), tr("Calcola PPK..."))
        self.b_ppk.setToolTip(ppk_tip())
        step(1, "ppk", tr("Correzioni (PPK)"), [self.b_ppk])

        self.b_survey_crs = QPushButton(icon("georef"), tr("Sistema del rilievo..."))
        self.b_survey_crs.setToolTip(tr("Il sistema di coordinate del rilievo dove si trova, per il sistema delle sue "
                                        "posizioni (UTM su WGS 84, RDN2008 / UTM in Italia), con le quote sul geoide "
                                        "EGM2008 o no: marker e disegni vengono convertiti"))
        self.b_geoid = QPushButton(icon("load"), tr("Scarica il geoide EGM2008 ({0} MB)...", GEOID_MB))
        self.b_geoid.setToolTip(tr("La griglia del geoide EGM2008 di PROJ (dominio pubblico), che serve a convertire "
                                   "le quote ellissoidiche del GNSS in quote sul livello del mare: scaricata una volta "
                                   "nella cartella del programma"))
        step(2, "heights", tr("Sistema e quote"), [self.b_survey_crs, self.b_geoid])

        heads = [(tr("Volo"), tr("Un volo finisce con una pausa di oltre 20 minuti; le foto senza data sono nel volo 0")),
                 (tr("Foto"), tr("Le foto del volo")),
                 (tr("Fix"), tr("Foto con una posizione RTK fissata")),
                 (tr("Float"), tr("Foto con una posizione RTK float")),
                 (tr("Singola"), tr("Foto con una posizione GNSS singola, senza correzioni")),
                 (tr("Senza RTK"), tr("Foto con una posizione e nessun dato RTK (il GPS dell'EXIF)")),
                 (tr("Senza posizione"), tr("Foto senza alcuna posizione")),
                 (tr("Disattivate"), tr("Foto con una posizione il cui riferimento è disattivato")),
                 (tr("Accuratezza"), tr("Mediana dell'accuratezza dei riferimenti, per asse (x / y / z)")),
                 (tr("Tipo di quota"), tr("Il tipo di quota delle posizioni: ellissoidica (GNSS, MRK), dell'EXIF "
                                          "(sul livello del mare) o sconosciuta")),
                 (tr("Scarto RMS"), tr("Distanza fra le camere allineate e le loro posizioni, per asse, quadratica media")),
                 (tr("Scarto max"), tr("La distanza maggiore fra una camera allineata e la sua posizione"))]
        self.table = QTableWidget(0, len(heads))
        self.table.setHorizontalHeaderLabels([name for name, _tip in heads])
        for j, (_name, tip) in enumerate(heads):
            self.table.horizontalHeaderItem(j).setToolTip(tip)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        step(3, "quality", tr("Qualità per volo"), [], table=self.table)

        self.c_only_fix = QCheckBox(tr("Usa solo le foto con posizione fix"))
        self.c_only_fix.setToolTip(tr("Disattiva i riferimenti delle foto la cui posizione non è un RTK fissato: non "
                                      "contano più nell'allineamento e nell'ottimizzazione. Togli la spunta per "
                                      "riattivarli (quelli che avevi disattivato a mano restano disattivati)"))
        step(4, "only_fix", tr("Solo le posizioni fix"), [self.c_only_fix])
        lay.addStretch(1)
        self.b_mrk.clicked.connect(self.mrk_requested)
        self.b_positions.clicked.connect(self.positions_requested)
        self.b_ppk.clicked.connect(self.ppk_requested)
        self.b_survey_crs.clicked.connect(self.survey_crs_requested)
        self.b_geoid.clicked.connect(self.geoid_requested)
        self.c_only_fix.toggled.connect(self.only_fix_toggled)
        self._busy = False  # another step is running: the PPK cannot start
        self._photos = 0
        self._chunk = None
        self._heights: tuple | None = None  # (what the step was read from, what it says): read again when that changes
        self._geoid: tuple | None = None  # (the grid file of the program as it is, whether PROJ has EGM2008)
        self.set_state(None)

    # --------------------------------------------------------------- state
    def set_state(self, chunk) -> None:
        """What every step has, read from the chunk."""
        rows = summary(chunk) if chunk is not None else []
        photos = sum(r["photos"] for r in rows)
        positioned = photos - sum(r["no_position"] for r in rows)
        self.b_mrk.setEnabled(photos > 0)
        self.b_positions.setEnabled(photos > 0)
        self._photos = photos
        self._chunk = chunk
        self.b_ppk.setEnabled(photos > 0 and not self._busy)
        self.states["ppk"].setText(ppk_state(chunk) if photos else "")
        self._set_heights(chunk)
        if not rows:
            self.states["positions"].setText(tr("Nessuna foto nel chunk: aggiungi le foto, poi applica l'MRK o un file "
                                                "di posizioni."))
        else:
            names, sources = source_names(), Counter()
            for r in rows:
                sources.update(r["sources"])
            text = tr("{0} foto su {1} con una posizione", positioned, photos)
            if sources:
                text += ": " + ", ".join(f"{names.get(s, s)} {n}" for s, n in sorted(sources.items(), key=lambda p: -p[1]))
            text += "."
            mixed = mixed_heights(chunk)
            if mixed:
                heights = height_names()
                text += " " + tr("Le quote di queste posizioni sono di tipo diverso ({0}): controllale prima di usarle "
                                 "insieme.", " / ".join(heights.get(k, k) for k in mixed))
            self.states["positions"].setText(text)
        self._fill(rows)
        aligned = any(r["residual_rms"] is not None for r in rows)
        self.states["quality"].setText(
            "" if not rows else
            tr("Gli scarti sono la distanza fra le camere allineate e le loro posizioni.") if aligned else
            tr("Gli scarti compaiono dopo l'allineamento di un chunk georeferenziato."))
        kept = (chunk.meta.get(ONLY_FIX) if chunk is not None else None)
        self.c_only_fix.setEnabled(positioned > 0)
        self.c_only_fix.blockSignals(True)
        self.c_only_fix.setChecked(kept is not None)
        self.c_only_fix.blockSignals(False)
        self.states["only_fix"].setText(
            tr("Disattivati i riferimenti di {0} foto senza posizione fix: non contano nell'allineamento.",
               len(kept.get("disabled") or [])) if kept is not None else
            tr("Tutte le posizioni contano, ognuna con la sua accuratezza.") if rows else "")

    def set_busy(self, busy: bool) -> None:
        """Another step is running (``busy``) or has ended: *Calcola PPK*, *Sistema del rilievo* and the download of the
        geoid wait for it, as the menu does."""
        self._busy = bool(busy)
        self.b_ppk.setEnabled(self._photos > 0 and not self._busy)
        self.b_survey_crs.setEnabled(self._chunk is not None and not self._busy)
        self.b_geoid.setEnabled(not self._busy and not self._geoid_available())

    def _geoid_available(self) -> bool:
        """Whether PROJ has the grid of EGM2008, asked again when the grid file of the program changes."""
        grid = heights.grid_folder() / heights.GEOID_GRID
        try:
            stamp = (str(grid), grid.stat().st_mtime_ns)
        except OSError:
            stamp = (str(grid), None)
        if self._geoid is None or self._geoid[0] != stamp:
            self._geoid = (stamp, heights.geoid_available())
        return self._geoid[1]

    def _set_heights(self, chunk) -> None:
        """The step Sistema e quote, read again only when what it is read from changed (heights.check asks PROJ, and the
        pane is read again often); not while a step runs, which may be changing the chunk."""
        geoid = self._geoid_available()
        self.b_survey_crs.setEnabled(chunk is not None and not self._busy)
        self.b_geoid.setEnabled(not self._busy and not geoid)
        if chunk is None:
            self._heights = None
            self.states["heights"].setText("")
            return
        if self._busy and self._heights is not None:
            return
        cams = tuple((cam.key, cam.meta.get("reference_crs"), tuple(cam.reference.location or ()),
                      bool(cam.reference.enabled), (cam.meta.get("gnss") or {}).get("frame"),
                      (cam.meta.get("gnss") or {}).get("epoch"), (cam.meta.get("gnss") or {}).get("height"))
                     for cam in chunk.cameras)
        key = (id(chunk), chunk.crs, geoid, cams)
        if self._heights is None or self._heights[0] != key:
            self._heights = (key, heights_state(chunk, geoid))
        self.states["heights"].setText(self._heights[1])

    def _fill(self, rows: list[dict]) -> None:
        self.table.setRowCount(len(rows))
        heights = height_names()
        for i, r in enumerate(rows):
            status = r["status"]
            kinds = ", ".join(f"{heights.get(k, k)} {n}" for k, n in sorted(r["heights"].items(), key=lambda p: -p[1]))
            cells = [str(r["flight"]) if r["flight"] else tr("senza data"), str(r["photos"]), str(status["fix"]),
                     str(status["float"]), str(status["single"]), str(status["none"]), str(r["no_position"]),
                     str(r["disabled"]), lengths(r["accuracy_median"]), kinds or "-",
                     lengths(r["residual_rms"]) if r["residual_rms"] else "-",
                     lengths([r["residual_max"]]) if r["residual_max"] is not None else "-"]
            for j, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(i, j, item)
        self.table.resizeColumnsToContents()
        # as tall as its rows (the ones past the eighth scroll), with the bar of the columns that do not fit
        shown = sum(self.table.rowHeight(i) for i in range(min(len(rows), self.MAX_ROWS)))
        self.table.setFixedHeight(self.table.horizontalHeader().height() + shown + 2 * self.table.frameWidth()
                                  + self.table.horizontalScrollBar().sizeHint().height() + 2)
