"""*Muro tracciato in pianta* as a pane that stays open: the elevation of a wall whose line is clicked on the plan
(:mod:`..engine.wall_trace`, :class:`..engine.wall_path.WallPath`) in four steps, each with its state - the line
traced on the orthophoto or the DEM, the line snapped onto the face of the model (or kept as clicked), the side the
wall is seen from with the heights and the depths, the build, and the same elevation on the other campaigns of the
wall (other chunks of the project, already placed together) as its group.

The pane holds the widgets and the line of the active chunk; the work - the drawing lent by the 2D view, the snap
and the build in the worker, the lines on the plan - is the window's (:mod:`.wall_path_actions`)."""
from __future__ import annotations

import contextlib

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QRadioButton, QSizePolicy, QVBoxLayout, QWidget)

from ..engine.wall_trace import SNAP_BAND
from .i18n import number, tr
from .icons import icon

STEPS = ("trace", "snap", "side", "build", "campaigns")
STATE_ICONS = {"todo": "step_todo", "done": "step_done", "running": "step_running"}
SIDES = (("auto", "Dove sono le foto"), ("left", "A sinistra della traccia"), ("right", "A destra della traccia"))


def state_names() -> dict[str, str]:
    return {"todo": tr("da fare"), "done": tr("fatto"), "running": tr("in corso")}


def _spin(lo: float, hi: float, value: float, decimals: int = 2, step: float = 0.1, suffix: str = " m") -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    _narrow(s)
    return s


def _narrow(widget, width: int = 80) -> None:
    """A field that takes the width the dock gives it, down to ``width`` pixels: the pane fits the right dock."""
    widget.setMinimumWidth(width)
    if isinstance(widget, QComboBox):
        widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        widget.setMinimumContentsLength(8)
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, widget.sizePolicy().verticalPolicy())


def line_length(points) -> float:
    xy = np.asarray(points, float).reshape(-1, 2)
    return float(np.hypot(*np.diff(xy, axis=0).T).sum()) if len(xy) > 1 else 0.0


class WallPathPane(QWidget):
    """The four steps of a wall traced on the plan of the active chunk (see the module); ``window`` the main
    window (its chunk, its worker, its 2D view)."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.chunk = None  # the chunk the line was traced on (the object: keys repeat across projects)
        self.clicked: np.ndarray | None = None
        self.snapped: np.ndarray | None = None
        self.report: dict | None = None  # of the snap: shift, median_shift, max_shift, snapped
        self.votes: dict | None = None  # the weights of the photos on either side
        self.running: str | None = None  # the step being worked on
        self.built: str | None = None  # the id of the elevation built from the line shown
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        intro = QLabel(tr("Il prospetto di un muro che segue una linea spezzata o curva: traccia la linea del muro "
                          "sulla pianta (ortofoto o DEM), il programma la aggancia alla faccia del modello, scegli il "
                          "lato e le quote, poi costruisci. La parete viene distesa lungo la linea."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.titles: dict[str, tuple[QLabel, QLabel, str]] = {}
        self.states: dict[str, QLabel] = {}

        def step(row: int, key: str, title: str, body: QWidget) -> None:
            head = QHBoxLayout()
            mark = QLabel()
            mark.setFixedWidth(18)
            t = QLabel()
            t.setWordWrap(True)
            head.addWidget(mark)
            head.addWidget(t, 1)
            lay.addLayout(head)
            lay.addWidget(body)
            s = QLabel("")
            s.setWordWrap(True)
            s.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            s.setContentsMargins(0, 0, 0, 8)
            lay.addWidget(s)
            self.titles[key] = (mark, t, f"{row + 1}. {title}")
            self.states[key] = s

        # 1. the line on the plan
        body = QWidget()
        b = QHBoxLayout(body)
        b.setContentsMargins(0, 0, 0, 0)
        self.b_trace = QPushButton(icon("elevation"), tr("Traccia sulla pianta"))
        self.b_trace.setToolTip(tr("Clic sui vertici della linea del muro nella vista 2D, doppio clic o Invio per "
                                   "finire; Backspace toglie l'ultimo, Esc annulla. Per una stanza chiudi sul primo "
                                   "punto."))
        b.addWidget(self.b_trace)
        b.addStretch(1)
        step(0, "trace", tr("Linea del muro sulla pianta"), body)

        # 2. the snap onto the face
        body = QWidget()
        sl = QVBoxLayout(body)
        sl.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.c_snap = QCheckBox(tr("Aggancia al muro"))
        self.c_snap.setChecked(True)
        self.c_snap.setToolTip(tr("La linea cliccata viene spostata sulla faccia del muro del modello"))
        row.addWidget(self.c_snap)
        row.addStretch(1)
        sl.addLayout(row)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.s_band = _spin(0.1, 20.0, SNAP_BAND)
        self.s_band.setToolTip(tr("Quanto lontano dalla linea cliccata, davanti e dietro, si cerca la faccia del muro. "
                                  "In una trincea o in un corridoio stretto usa una fascia più piccola della loro "
                                  "larghezza."))
        form.addRow(tr("Fascia di ricerca"), self.s_band)
        self.s_smooth = _spin(0.0, 50.0, 0.0)
        self.s_smooth.setToolTip(tr("Per un muro curvo cliccato con molti punti: la linea viene smussata su questa "
                                    "lunghezza prima dell'aggancio (0: nessuna)"))
        form.addRow(tr("Smussa la linea"), self.s_smooth)
        sl.addLayout(form)
        row = QHBoxLayout()
        self.r_snapped = QRadioButton(tr("Linea agganciata"))
        self.r_clicked = QRadioButton(tr("Linea cliccata"))
        self.r_clicked.setChecked(True)
        self.g_line = QButtonGroup(self)
        self.g_line.addButton(self.r_snapped)
        self.g_line.addButton(self.r_clicked)
        self.b_snap = QPushButton(tr("Aggancia di nuovo"))
        self.b_snap.setToolTip(tr("La linea cliccata agganciata di nuovo con la fascia, la smussatura e il lato scelti"))
        sl.addWidget(self.r_snapped)
        sl.addWidget(self.r_clicked)
        row.addWidget(self.b_snap)
        row.addStretch(1)
        sl.addLayout(row)
        self.shifts = QLabel("")
        self.shifts.setWordWrap(True)
        self.shifts.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sl.addWidget(self.shifts)
        step(1, "snap", tr("Aggancio alla faccia del muro"), body)

        # 3. side, heights and depths
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)  # a narrow dock: the label over its field
        self.side = QComboBox()
        _narrow(self.side)
        for key, text in SIDES:
            self.side.addItem(tr(text), key)
        form.addRow(tr("Lato da cui guardare"), self.side)
        self.s_v0 = _spin(-12000.0, 12000.0, 0.0, 2, 0.05)
        self.s_v1 = _spin(-12000.0, 12000.0, 0.0, 2, 0.05)
        self.s_v0.setToolTip(tr("Le quote del prospetto: lette sul modello lungo la linea, si possono cambiare"))
        form.addRow(tr("Quota più bassa"), self.s_v0)
        form.addRow(tr("Quota più alta"), self.s_v1)
        self.s_front = _spin(0.01, 1000.0, 0.5, 3, 0.05)
        self.s_front.setToolTip(tr("Ciò che sta più avanti (impalcature, un muro davanti) non copre la parete"))
        self.s_back = _spin(0.01, 1000.0, 0.5, 3, 0.05)
        form.addRow(tr("Profondità davanti"), self.s_front)
        form.addRow(tr("Profondità dietro"), self.s_back)
        self.s_before = _spin(0.0, 1000.0, 0.0)
        self.s_after = _spin(0.0, 1000.0, 0.0)
        self.s_before.setToolTip(tr("Il prospetto prolungato oltre il primo e l'ultimo punto, lungo la linea"))
        self.s_after.setToolTip(self.s_before.toolTip())
        form.addRow(tr("Prolunga prima"), self.s_before)
        form.addRow(tr("Prolunga dopo"), self.s_after)
        step(2, "side", tr("Lato, quote e profondità"), body)

        # 4. the build
        body = QWidget()
        form = QFormLayout(body)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)  # a narrow dock: the label over its field
        self.s_resolution = _spin(0.0, 10.0, 0.0, 4, 0.001, " m/pixel")
        self.s_resolution.setSpecialValueText(tr("automatica"))
        form.addRow(tr("Risoluzione"), self.s_resolution)
        self.c_blending = QComboBox()
        _narrow(self.c_blending)
        self.c_blending.addItem(tr("Mosaico senza cuciture"), "mosaic")
        self.c_blending.addItem(tr("Nessuna (una foto per cella)"), "disabled")
        form.addRow(tr("Fusione"), self.c_blending)
        self.c_colour = QCheckBox(tr("Correzione colore"))
        self.c_colour.setChecked(True)
        form.addRow("", self.c_colour)
        self.e_name = QLineEdit()
        _narrow(self.e_name)
        self.e_name.setPlaceholderText(tr("Prospetto del muro"))
        form.addRow(tr("Nome"), self.e_name)
        self.b_build = QPushButton(icon("elevation"), tr("Costruisci il prospetto"))
        form.addRow("", self.b_build)
        step(3, "build", tr("Prospetto"), body)

        # 5. the other campaigns
        body = QWidget()
        form = QVBoxLayout(body)
        form.setContentsMargins(0, 0, 0, 0)
        note = QLabel(tr("Lo stesso prospetto, sulla stessa griglia, sugli altri chunk del muro (altre campagne "
                         "già allineate insieme): le ortofoto si confrontano pixel per pixel."))
        note.setWordWrap(True)
        form.addWidget(note)
        self.b_campaigns = QPushButton(icon("elevation"), tr("Stesso prospetto su altri chunk..."))
        form.addWidget(self.b_campaigns)
        step(4, "campaigns", tr("Altre campagne"), body)
        lay.addStretch(1)

        self.b_trace.clicked.connect(lambda: self.window.wall_trace_start())
        self.b_build.clicked.connect(lambda: self.window.wall_build())
        self.b_campaigns.clicked.connect(lambda: self.window.wall_campaigns())
        self.b_snap.clicked.connect(lambda: self.window.wall_snap())
        self.c_snap.toggled.connect(self._snap_toggled)
        self.r_snapped.toggled.connect(lambda _on: self._line_changed())
        self.side.currentIndexChanged.connect(lambda _i: self._side_changed())
        with contextlib.suppress(AttributeError):
            window.runner.busy_changed.connect(self._busy_changed)
        self.update_states()

    # ------------------------------------------------------------------ state
    def _busy_changed(self, busy: bool) -> None:
        if not busy and self.running in ("snap", "build", "campaigns"):
            with contextlib.suppress(RuntimeError):
                self.running = None
                self.update_states()

    def _snap_toggled(self, on: bool) -> None:
        if not on:
            self.r_clicked.setChecked(True)
        elif self.snapped is not None:
            self.r_snapped.setChecked(True)
        self.update_states()
        if on and self.clicked is not None and self.snapped is None and self.running is None:
            self.window.wall_snap()

    def _line_changed(self) -> None:
        with contextlib.suppress(AttributeError, RuntimeError):
            self.window._draw_trace_preview()
        self.update_states()

    def _side_changed(self) -> None:
        """Another side: the face looked for is another, the line is snapped again."""
        if self.clicked is not None and self.c_snap.isChecked() and self.running is None:
            self.window.wall_snap()

    def set_trace(self, clicked, snapped=None, report=None, heights=None) -> None:
        """The line traced (``clicked``, plan points in the order clicked), the line snapped onto the face and the
        report of the snap (:func:`..engine.wall_trace.snap_trace`), the default heights ``(v0, v1)``; None for
        the line: the pane emptied."""
        self.clicked = None if clicked is None else np.asarray(clicked, float).reshape(-1, 2)
        self.snapped = None if snapped is None else np.asarray(snapped, float).reshape(-1, 2)
        self.report = report
        self.built = None
        if self.clicked is None:
            self.chunk = None
            self.votes = None
        else:
            self.chunk = getattr(self.window, "chunk", None)
        if heights is not None:
            self.s_v0.setValue(float(heights[0]))
            self.s_v1.setValue(float(heights[1]))
        usable = self.snapped is not None and bool((report or {}).get("snapped", True))
        self.r_snapped.setEnabled(usable)
        if usable and self.c_snap.isChecked():
            self.r_snapped.setChecked(True)
        else:
            self.r_clicked.setChecked(True)
        self.update_states()

    def refresh(self) -> None:
        """The pane as the active chunk has it: emptied when the line was traced on another chunk."""
        ch = getattr(self.window, "chunk", None)
        if self.clicked is not None and (ch is None or ch is not self.chunk):
            self.set_trace(None)
            with contextlib.suppress(AttributeError, RuntimeError):
                self.window._draw_trace_preview()
        self.update_states()

    def points(self) -> np.ndarray | None:
        """The line the elevation is built on: the snapped one or the clicked one, as chosen."""
        if self.clicked is None:
            return None
        if self.r_snapped.isChecked() and self.snapped is not None:
            return self.snapped
        return self.clicked

    def values(self) -> dict:
        res = self.s_resolution.value()
        return {"points": self.points(), "clicked": self.clicked, "side": self.side.currentData(),
                "v0": self.s_v0.value(), "v1": self.s_v1.value(), "front": self.s_front.value(),
                "back": self.s_back.value(), "extend": (self.s_before.value(), self.s_after.value()),
                "smooth": self.s_smooth.value(), "band": self.s_band.value(),
                "resolution": res if res > 0 else None, "blending": self.c_blending.currentData(),
                "colour": self.c_colour.isChecked(), "name": self.e_name.text().strip(),
                "snap": self.c_snap.isChecked()}

    def set_values(self, v: dict) -> None:
        """The answers of the elevation dialog: depths, resolution, blending, colour, name (and side)."""
        if v.get("front") is not None:
            self.s_front.setValue(float(v["front"]))
        if v.get("back") is not None:
            self.s_back.setValue(float(v["back"]))
        self.s_resolution.setValue(float(v.get("resolution") or 0.0))
        k = self.c_blending.findData(v.get("blending", "mosaic"))
        if k >= 0:
            self.c_blending.setCurrentIndex(k)
        self.c_colour.setChecked(bool(v.get("colour", True)))
        self.e_name.setText(str(v.get("name") or ""))
        k = self.side.findData(v.get("side"))
        if k >= 0:
            self.side.blockSignals(True)
            self.side.setCurrentIndex(k)
            self.side.blockSignals(False)

    def state(self, step: str) -> str:
        if self.running == step:
            return "running"
        if step == "trace":
            return "done" if self.clicked is not None else "todo"
        if step == "snap":
            if self.clicked is None:
                return "todo"
            return "done" if (self.snapped is not None or not self.c_snap.isChecked()) else "todo"
        if step == "side":
            return "done" if self.clicked is not None and self.s_v1.value() > self.s_v0.value() else "todo"
        if step == "campaigns":
            return "done" if len(self.campaigns()) > 1 else "todo"
        return "done" if self.built is not None else "todo"

    def built_entry(self) -> dict | None:
        """The elevation built from the line shown, while it is in its chunk."""
        if self.built is None or self.chunk is None:
            return None
        return next((e for e in (self.chunk.meta or {}).get("elevations") or [] if e.get("id") == self.built), None)

    def campaigns(self) -> list[dict]:
        """The member records of the group of the elevation built (:mod:`..engine.elevation_groups`), empty when
        it is in none."""
        from ..engine import elevation_groups as G

        doc = getattr(self.window, "doc", None)
        if self.built_entry() is None or doc is None:
            return []
        found = G.group_of(doc, self.chunk, self.built)
        return list(found[1]["members"]) if found is not None else []

    def state_text(self, step: str) -> str:
        if step == "trace":
            if self.running == "trace":
                return tr("Clic sui vertici nella vista 2D, doppio clic o Invio per finire")
            if self.clicked is None:
                return ""
            return tr("{0} vertici, {1} m", len(self.clicked), number(line_length(self.clicked), 2))
        if step == "snap":
            if self.running == "snap":
                return tr("Ricerca della faccia del muro...")
            if self.clicked is None:
                return ""
            if not self.c_snap.isChecked():
                return tr("La linea resta come è stata cliccata")
            r = self.report or {}
            if self.snapped is None:
                return ""
            if not r.get("snapped", True):
                return tr("La faccia del muro non è stata trovata: si usa la linea cliccata. Clicca più vicino alla "
                          "faccia o cambia la fascia di ricerca.") + (
                    "\n" + str(r["refused"]) if r.get("refused") else "")
            return tr("Vertici spostati in media di {0} cm, al massimo di {1} cm", number(100 * r.get("median_shift",
                                                                                                0.0), 1),
                      number(100 * r.get("max_shift", 0.0), 1))
        if step == "side":
            if self.clicked is None:
                return ""
            text = tr("Quote da {0} a {1} m", number(self.s_v0.value(), 2), number(self.s_v1.value(), 2))
            if self.votes:
                text += "\n" + tr("Peso delle foto: a sinistra {0}, a destra {1}", number(self.votes["left"], 2),
                                  number(self.votes["right"], 2))
            return text
        if step == "campaigns":
            if self.running == "campaigns":
                return tr("Prospetto sugli altri chunk...")
            members = self.campaigns()
            if len(members) < 2:
                return ""
            return tr("{0} campagne nel gruppo: {1}", len(members), "; ".join(
                f"{m.get('label')} ({m.get('date') or tr('data non nota')})" for m in members))
        if self.running == "build":
            return tr("Costruzione del prospetto...")
        return ""

    def update_states(self) -> None:
        names = state_names()
        for key in STEPS:
            mark, title, text = self.titles[key]
            st = self.state(key)
            mark.setPixmap(icon(STATE_ICONS[st]).pixmap(16, 16))
            title.setText(f"<b>{text}</b> — {names[st]}")
            self.states[key].setText(self.state_text(key))
        shift = (self.report or {}).get("shift")
        if shift is not None and self.snapped is not None and self.c_snap.isChecked():
            self.shifts.setText(tr("Spostamento di ogni vertice: {0}", "; ".join(
                f"{i + 1}: {number(100 * float(s), 1)} cm" for i, s in enumerate(shift))))
        else:
            self.shifts.setText("")
        has = self.clicked is not None and self.running is None
        self.b_snap.setEnabled(has and self.c_snap.isChecked())
        self.b_build.setEnabled(has)
        self.b_campaigns.setEnabled(self.running is None and self.built_entry() is not None)
        self.b_trace.setEnabled(self.running is None)
