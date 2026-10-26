"""The GNSS positions of the photos as a pane that stays open: where the positions come from, their PPK correction
against a base, how good they are flight by flight, and the choice to trust only the RTK fixes."""
from __future__ import annotations

from collections import Counter

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHeaderView, QLabel, QPushButton, QScrollArea,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import _native
from ..gnss.apply import ONLY_FIX
from ..gnss.quality import mixed_heights, summary
from .flow_layout import FlowLayout
from .i18n import number, tr
from .icons import icon


def source_names() -> dict[str, str]:
    """Where a position can come from (``meta["gnss"]["source"]``), as the pane says it."""
    return {"exif": tr("EXIF"), "dji_xmp": tr("XMP di DJI"), "dji_mrk": tr("MRK di DJI"), "ppk": tr("PPK"),
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


def ppk_tip() -> str:
    """What *Calcola PPK* (and *Strumenti > GNSS > Correzioni PPK*) does, as its tooltip says it."""
    return tr("Il RINEX del drone e il Timestamp.MRK del volo, il RINEX di una base che registrava durante il volo (la "
              "tua o una stazione permanente) e i file di navigazione: RTKLIB calcola la traiettoria dell'antenna e "
              "ogni foto prende la posizione della camera al suo scatto")


def ppk_state(chunk) -> str:
    """What the PPK step says of ``chunk``: how many photos have a PPK position and of which status, the base each
    position was computed against and how its position was set (the one of its RINEX header with the warning that it
    can be metres off); without PPK positions what the step needs, and whether RTKLIB is there to compute them."""
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
    bases: dict[str, set] = {}  # the bases of the positions, in the order of the photos: how their position was set
    for r in records:
        base = r.get("base") or {}
        bases.setdefault(str(base.get("name") or "?"), set()).add(base.get("position_source"))
    for name, sources in bases.items():
        if "header" in sources:  # also when some of its positions came from a typed one: the warning wins
            text += " " + tr("La base {0} è nella posizione della sua intestazione RINEX, che può essere sbagliata di "
                             "metri e sposta tutto il rilievo: calcola di nuovo con la posizione misurata della base.",
                             name)
        elif "typed" in sources:
            text += " " + tr("Base {0} nella posizione scritta.", name)
    return text


class GnssPane(QWidget):
    """The positions of the photos: where they come from (the RTK of the photos, a DJI ``Timestamp.MRK``, a file of
    positions), their PPK correction against a base, a table of the quality of each flight and the switch that trusts
    only the fixes. Each step shows its state under its buttons; the window does the work and tells the pane to read
    the chunk again."""

    MAX_ROWS = 8

    mrk_requested = Signal()
    positions_requested = Signal()
    ppk_requested = Signal()
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
        step(2, "quality", tr("Qualità per volo"), [], table=self.table)

        self.c_only_fix = QCheckBox(tr("Usa solo le foto con posizione fix"))
        self.c_only_fix.setToolTip(tr("Disattiva i riferimenti delle foto la cui posizione non è un RTK fissato: non "
                                      "contano più nell'allineamento e nell'ottimizzazione. Togli la spunta per "
                                      "riattivarli (quelli che avevi disattivato a mano restano disattivati)"))
        step(3, "only_fix", tr("Solo le posizioni fix"), [self.c_only_fix])
        lay.addStretch(1)
        self.b_mrk.clicked.connect(self.mrk_requested)
        self.b_positions.clicked.connect(self.positions_requested)
        self.b_ppk.clicked.connect(self.ppk_requested)
        self.c_only_fix.toggled.connect(self.only_fix_toggled)
        self._busy = False  # another step is running: the PPK cannot start
        self._photos = 0
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
        self.b_ppk.setEnabled(photos > 0 and not self._busy)
        self.states["ppk"].setText(ppk_state(chunk) if photos else "")
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
        """Another step is running (``busy``) or has ended: *Calcola PPK* waits for it, as the menu does."""
        self._busy = bool(busy)
        self.b_ppk.setEnabled(self._photos > 0 and not self._busy)

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
