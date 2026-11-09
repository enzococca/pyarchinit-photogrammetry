"""*Strumenti > Livello dell'acqua e rifrazione...*: the level of the water over a flooded trench or a wreck
(:mod:`..engine.water`) and the correction of the dense cloud for the refraction.

The level comes *Dal bordo dell'acqua* - a layer of polygons of free drawings (a colour mask of *Maschera dal colore*,
or the water drawn by hand) or a colour range of the orthophoto (three ranges and *Campiona dall'ortofoto*, as in
*Maschera dal colore*) - measured in the worker: the level, its spread (NMAD) and the 10-90 % range are shown and the
heights read on the bank drawn on the 2D view as the point layer :data:`EDGE_LAYER`. Or it is typed *A mano*. Setting
the level is one step of *Modifica > Annulla*. The refractive index: sea 1,34, fresh water 1,33 or another value.
*Correggi la nuvola* writes the corrected dense cloud as a new version in the worker (with *Ricostruisci il DEM*, a new
version of the DEM from it): undone, like every product built, by making the version before active again. The window
is not modal; it works on the chunk active when it was opened.
"""
from __future__ import annotations

import contextlib

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QPushButton, QRadioButton, QVBoxLayout, QWidget)

from ..engine import colour_mask, water
from . import i18n
from .colour_mask_dialog import RangeRow
from .i18n import number, tr
from .ties_dialog import undo_shortcuts

EDGE_LAYER = "Bordo dell'acqua"  # the layer of the 2D view with the heights of the bank the level was measured on
EDGE_COLOUR = "#00a5e0"
N_CHOICES = (("sea", water.N_SEA), ("fresh", water.N_FRESH), ("other", None))


def error_text(exc: Exception) -> str:
    """An error of the water or of its mask in the language of the interface (both carry the two texts)."""
    if isinstance(exc, (water.WaterError, colour_mask.MaskError)):
        return exc.italian if i18n.current() == "it" else exc.english
    return i18n.translate(str(exc))


def polygon_layers(chunk) -> list[str]:
    """The layers of free drawings of ``chunk`` that have polygons: where the water can be."""
    from ..core import free_drawings

    names = []
    for f in free_drawings.features(chunk):
        if f["kind"] == "polygon" and f["layer"] not in names:
            names.append(f["layer"])
    return names


class WaterDialog(QDialog):
    """The water level and the refraction correction of the active chunk (see the module)."""

    @classmethod
    def ask(cls, window) -> "WaterDialog":
        dlg = cls(window)
        dlg.show()
        return dlg

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.doc = window.doc
        self.chunk = window.chunk
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setModal(False)
        self.setWindowTitle(tr("Livello dell'acqua e rifrazione"))
        self.working = False
        self.measured: dict | None = None  # the last level measured from the edge
        self._seen = None  # the stored level the widgets were last set from: a refresh keeps what is typed otherwise
        self._edge_key = None  # the stored level whose bank heights are drawn (EDGE_LAYER)

        lay = QVBoxLayout(self)
        intro = QLabel(tr("Visto dall'alto attraverso l'acqua, il fondo sembra meno profondo di quanto è. Con il "
                          "livello dell'acqua e l'indice di rifrazione, i punti della nuvola densa sotto il livello "
                          "tornano alla loro quota: z' = L - n (L - z). È una correzione parassiale: vale per foto "
                          "quasi nadirali."))
        intro.setWordWrap(True)
        lay.addWidget(intro)

        # ---- the level
        box = QGroupBox(tr("Livello"))
        blay = QVBoxLayout(box)
        self.r_edge = QRadioButton(tr("Dal bordo dell'acqua"))
        self.r_hand = QRadioButton(tr("A mano"))
        self._how = QButtonGroup(self)
        for b in (self.r_edge, self.r_hand):
            self._how.addButton(b)
        self.r_edge.setChecked(True)
        blay.addWidget(self.r_edge)
        self.edge_box = QWidget()
        elay = QFormLayout(self.edge_box)
        elay.setContentsMargins(20, 0, 0, 0)
        self.r_layer = QRadioButton(tr("Un layer di poligoni"))
        self.r_colour = QRadioButton(tr("Un intervallo di colori dell'ortofoto"))
        self._source = QButtonGroup(self)
        for b in (self.r_layer, self.r_colour):
            self._source.addButton(b)
        self.c_layer = QComboBox()
        self.c_layer.setToolTip(tr("Il layer dei disegni liberi con il poligono dell'acqua (una maschera dal colore o "
                                   "un poligono disegnato)"))
        elay.addRow(self.r_layer, self.c_layer)
        elay.addRow(self.r_colour, QWidget())
        self.colour_box = QWidget()
        clay = QFormLayout(self.colour_box)
        clay.setContentsMargins(20, 0, 0, 0)
        self.r_hue = RangeRow(360, tr("gradi"), 170, 210)
        self.r_sat = RangeRow(100, "%", 30, 100)
        self.r_val = RangeRow(100, "%", 20, 100)
        clay.addRow(tr("Tinta"), self.r_hue)
        clay.addRow(tr("Saturazione"), self.r_sat)
        clay.addRow(tr("Valore (luminosità)"), self.r_val)
        row = QHBoxLayout()
        self.b_sample = QPushButton(tr("Campiona dall'ortofoto"))
        self.b_sample.setToolTip(tr("Un clic sull'acqua nell'ortofoto: l'intervallo attorno al suo colore"))
        self.b_mask = QPushButton(tr("Maschera dal colore..."))
        self.b_mask.setToolTip(tr("La maschera come layer di poligoni, con la sua anteprima e la pulizia: poi "
                                  "sceglila qui come layer"))
        row.addWidget(self.b_sample)
        row.addWidget(self.b_mask)
        row.addStretch(1)
        elay.addRow(self.colour_box)
        elay.addRow("", row)  # the sample chooses the colours by itself
        self.b_measure = QPushButton(tr("Misura il livello"))
        self.b_measure.setToolTip(tr("Le quote del DEM sulla riva, estrapolate al contorno dell'acqua: la mediana è il "
                                     "livello"))
        elay.addRow("", self.b_measure)
        blay.addWidget(self.edge_box)
        blay.addWidget(self.r_hand)
        hand = QHBoxLayout()
        hand.setContentsMargins(20, 0, 0, 0)
        self.s_level = QDoubleSpinBox()
        self.s_level.setRange(-12000.0, 12000.0)
        self.s_level.setDecimals(3)
        self.s_level.setSingleStep(0.01)
        self.s_level.setSuffix(" m")
        self.s_level.setToolTip(tr("La quota della superficie dell'acqua nel sistema del chunk"))
        self.b_hand = QPushButton(tr("Imposta il livello"))
        hand.addWidget(self.s_level)
        hand.addWidget(self.b_hand)
        hand.addStretch(1)
        self.hand_box = QWidget()
        self.hand_box.setLayout(hand)
        blay.addWidget(self.hand_box)
        lay.addWidget(box)

        form = QFormLayout()
        nrow = QHBoxLayout()
        self.c_n = QComboBox()
        self.c_n.addItem(tr("Mare (1,34)"), "sea")
        self.c_n.addItem(tr("Acqua dolce (1,33)"), "fresh")
        self.c_n.addItem(tr("Altro"), "other")
        self.s_n = QDoubleSpinBox()
        self.s_n.setRange(*water.N_RANGE)
        self.s_n.setDecimals(3)
        self.s_n.setSingleStep(0.01)
        self.s_n.setValue(water.N_SEA)
        nrow.addWidget(self.c_n)
        nrow.addWidget(self.s_n)
        nrow.addStretch(1)
        form.addRow(tr("Indice di rifrazione"), nrow)
        lay.addLayout(form)

        self.l_level = QLabel()
        self.l_level.setWordWrap(True)
        self.l_level.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.l_level)

        cbox = QGroupBox(tr("Correzione della nuvola densa"))
        clay2 = QVBoxLayout(cbox)
        self.c_dem = QCheckBox(tr("Ricostruisci il DEM dalla nuvola corretta"))
        self.c_dem.setToolTip(tr("Una nuova versione del DEM, sulla stessa griglia"))
        self.b_correct = QPushButton(tr("Correggi la nuvola"))
        self.b_correct.setToolTip(tr("La nuvola corretta diventa una nuova versione; quella di prima resta fra le "
                                     "versioni: rendila attiva per tornare indietro"))
        self.l_correct = QLabel()
        self.l_correct.setWordWrap(True)
        clay2.addWidget(self.c_dem)
        crow = QHBoxLayout()
        crow.addWidget(self.b_correct)
        crow.addStretch(1)
        clay2.addLayout(crow)
        clay2.addWidget(self.l_correct)
        lay.addWidget(cbox)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.b_close = QPushButton(tr("Chiudi"))
        buttons.addWidget(self.b_close)
        lay.addLayout(buttons)
        undo_shortcuts(self, window)
        for button in self.findChildren(QPushButton):  # Enter in a field does not press a button (nor makes it
            button.setAutoDefault(False)  # the default one, in bold: its text would no longer fit)

        self.r_edge.toggled.connect(self._update_buttons)
        self.r_layer.toggled.connect(self._update_buttons)
        self.r_colour.toggled.connect(self._update_buttons)
        self.c_n.currentIndexChanged.connect(self._n_changed)
        self.s_n.editingFinished.connect(self._n_changed)
        window.runner.busy_changed.connect(self._busy_changed)
        self.b_sample.clicked.connect(self.start_sample)
        self.b_mask.clicked.connect(self._open_mask)
        self.b_measure.clicked.connect(self.measure)
        self.b_hand.clicked.connect(self.set_by_hand)
        self.b_correct.clicked.connect(self.correct)
        self.b_close.clicked.connect(self.close)
        self.refresh()

    # ------------------------------------------------------------------ state
    def _gone(self) -> bool:
        try:
            self.isVisible()
        except RuntimeError:
            return True
        return self.window.doc is not self.doc or self.chunk not in self.doc.chunks

    def n(self) -> float:
        """The refractive index chosen."""
        kind = self.c_n.currentData()
        return dict((k, v) for k, v in N_CHOICES if v is not None).get(kind) or float(self.s_n.value())

    def set_n(self, n: float) -> None:
        """The combo (and the spinbox) on ``n``, without storing it."""
        index = len(N_CHOICES) - 1
        for i, (_k, v) in enumerate(N_CHOICES):
            if v is not None and abs(v - n) < 1e-9:
                index = i
                break
        for w in (self.c_n, self.s_n):
            w.blockSignals(True)
        self.c_n.setCurrentIndex(index)
        self.s_n.setValue(float(n))
        for w in (self.c_n, self.s_n):
            w.blockSignals(False)
        self._update_buttons()

    def _n_changed(self, *_a) -> None:
        """Another refractive index chosen: with a level stored, stored with it (one step of Annulla), so the
        correction always uses the n shown and stored."""
        if self._gone():
            return
        kind = self.c_n.currentData()
        v = dict(N_CHOICES).get(kind)
        if v is not None:
            self.s_n.blockSignals(True)
            self.s_n.setValue(v)
            self.s_n.blockSignals(False)
        self._update_buttons()
        rec = self.chunk.meta.get("water")
        if not isinstance(rec, dict) or rec.get("level") is None:
            return
        n = self.n()
        if abs(float(rec.get("n") or water.N_SEA) - n) < 1e-9:
            return
        if self.working or not self._active():
            self.set_n(float(rec.get("n") or water.N_SEA))  # back to the n stored
            return
        self.chunk.meta["water"] = {**rec, "n": n}  # the level as it was set: only its index changes
        self._recorded(tr("Indice di rifrazione"))
        self.window.log_pane.info(tr("Livello dell'acqua di {0}: indice di rifrazione {1}", self.chunk.label,
                                     number(n, 3)))

    def _busy_changed(self, busy: bool) -> None:
        """The worker free again (done, failed or cancelled): the buttons back."""
        if not busy and self.working:
            with contextlib.suppress(RuntimeError):
                self.working = False
                self._update_buttons()

    def refresh(self) -> None:
        """Layers, level and correction as the chunk has them now (also after Annulla and after a build)."""
        if self._gone():
            return
        current = self.c_layer.currentText()
        names = polygon_layers(self.chunk)
        self.c_layer.blockSignals(True)
        self.c_layer.clear()
        self.c_layer.addItems(names)
        if current in names:
            self.c_layer.setCurrentText(current)
        self.c_layer.blockSignals(False)
        if not self.r_layer.isChecked() and not self.r_colour.isChecked():
            (self.r_layer if names else self.r_colour).setChecked(True)
        lv = water.level_of(self.chunk)
        rec = self.chunk.meta.get("water") if isinstance(self.chunk.meta.get("water"), dict) else {}
        key = None if lv is None else (lv["level"], lv["n"], lv["source"], rec.get("when"))
        if key != self._seen:  # another level stored (set, undone, redone): the widgets from it; else what is typed stays
            self._seen = key
            if lv is not None:
                self.set_n(lv["n"])
                self.s_level.setValue(lv["level"])
        if self._edge_key is not None and (key is None or key[0] != self._edge_key[0]
                                           or key[2:] != self._edge_key[2:]):
            self.clear_edge()  # the bank heights of a level that is no longer the one stored
        if lv is None:
            self.l_level.setText(tr("Nessun livello dell'acqua per il chunk {0}.", self.chunk.label))
        else:
            if lv["source"] == "edge":
                text = tr("Livello {0} m (dal bordo dell'acqua: {1} quote, dispersione {2} m, dal 10 al 90 %: da {3} "
                          "a {4} m), n {5}.", number(lv["level"], 3), number(rec.get("edge_cells") or 0),
                          number(lv["spread"] or 0.0, 3), number(rec.get("p10") or 0.0, 3),
                          number(rec.get("p90") or 0.0, 3), number(lv["n"], 2))
                if rec.get("slope_bias") is not None:
                    text += " " + tr("Salita della riva tolta: {0} m.", number(rec["slope_bias"], 3))
            else:
                text = tr("Livello {0} m (a mano), n {1}.", number(lv["level"], 3), number(lv["n"], 2))
            self.l_level.setText(text)
        done = (self.chunk.meta.get("dense") or {}).get("refraction")
        if done:
            self.l_correct.setText(tr("La nuvola attiva è già corretta (livello {0} m, n {1}, {2} punti sotto il "
                                      "livello): rendi attiva quella di prima per correggerla di nuovo.",
                                      number(done.get("level") or 0.0, 3), number(done.get("n") or 0.0, 2),
                                      number(done.get("below") or 0)))
        else:
            self.l_correct.setText(tr("Correzione parassiale: vale per foto quasi nadirali."))
        self._update_buttons()

    def _update_buttons(self, *_a) -> None:
        with contextlib.suppress(RuntimeError):
            edge = self.r_edge.isChecked()
            self.edge_box.setEnabled(edge and not self.working)
            self.hand_box.setEnabled(not edge and not self.working)
            self.colour_box.setEnabled(self.r_colour.isChecked())
            self.c_layer.setEnabled(self.r_layer.isChecked())
            self.b_measure.setEnabled(self.r_colour.isChecked() or (self.r_layer.isChecked() and
                                                                     self.c_layer.count() > 0))
            self.c_n.setEnabled(not self.working)
            self.s_n.setEnabled(self.c_n.currentData() == "other" and not self.working)
            cloud = self.chunk.product_path("point_cloud")
            corrected = bool((self.chunk.meta.get("dense") or {}).get("refraction"))
            self.b_correct.setEnabled(not self.working and water.level_of(self.chunk) is not None
                                      and cloud is not None and cloud.exists() and not corrected)

    def range(self) -> colour_mask.HsvRange:
        return colour_mask.HsvRange(self.r_hue.values(), self.r_sat.values(), self.r_val.values())

    def source(self):
        """What the water is: the name of the layer chosen, or the colour range."""
        if self.r_layer.isChecked():
            return self.c_layer.currentText()
        return self.range()

    def _active(self) -> bool:
        """The chunk of the window still the active one (its history takes the step of Annulla)."""
        if self.window.chunk is not self.chunk:
            self.window._warn(tr("Livello dell'acqua: rendi attivo il chunk {0}.", self.chunk.label))
            return False
        return True

    # ------------------------------------------------------------------ colours
    def start_sample(self) -> bool:
        """A click on the view (:meth:`MainWindow.request_geometry`): the colour range around the colour there."""
        if self._gone() or not self._active() or self.window._plan_only():
            return False
        return self.window.request_geometry("point", self.sampled,
                                            hint=tr("Clic sull'acqua nell'ortofoto: il suo colore (Esc annulla)"))

    def sampled(self, xy) -> None:
        if self._gone():
            return
        x, y = (float(v) for v in np.asarray(xy, float).reshape(-1, 2)[0])
        try:
            rng = colour_mask.sample(self.chunk, x, y)
        except ValueError as exc:
            self.window._warn(tr("Livello dell'acqua: {0}", error_text(exc)))
            return
        for row, (lo, hi) in ((self.r_hue, rng.hue), (self.r_sat, rng.sat), (self.r_val, rng.val)):
            row.set_values(lo, hi)
        self.r_colour.setChecked(True)

    def _open_mask(self) -> None:
        self.window.open_colour_mask()

    # ------------------------------------------------------------------ the level
    def measure(self) -> bool:
        """The level from the edge of the water, measured in the worker, then set (one step of Annulla) and shown,
        the heights of the bank drawn on the view. Returns whether it started."""
        if self._gone() or self.working or not self._active():
            return False
        try:
            src = self.source()
        except ValueError as exc:
            self.window._warn(tr("Livello dell'acqua: {0}", error_text(exc)))
            return False
        if isinstance(src, str) and not src:
            self.window._warn(tr("Livello dell'acqua: nessun layer di poligoni nel chunk {0}.", self.chunk.label))
            return False
        chunk, n = self.chunk, self.n()

        def work(p):
            try:
                return water.level_from_edge(chunk, src, progress=p, points=True)
            except ValueError as exc:  # WaterError, MaskError: said in the window's words
                return {"error": exc}

        self.working = True
        self._update_buttons()
        started = self.window.runner.run("Livello dell'acqua", work, lambda r: self._measured(r, n), self._failed)
        if not started:
            self.working = False
            self._update_buttons()
        return started

    def _failed(self, summary: str, details: str = "") -> None:
        with contextlib.suppress(RuntimeError):
            self.working = False
            self._update_buttons()
        self.window._error(summary, details)

    def _measured(self, result: dict, n: float) -> None:
        with contextlib.suppress(RuntimeError):
            self.working = False
            self._update_buttons()
        if self._gone():
            return
        if "error" in result:
            self.window._warn(tr("Livello dell'acqua: {0}", error_text(result["error"])))
            return
        if self.window.chunk is not self.chunk:  # the step of Annulla would be lost with the history of the chunk
            self.window._warn(tr("Livello dell'acqua: il chunk attivo è cambiato durante la misura. Rendi attivo {0} "
                                 "e misura di nuovo.", self.chunk.label))
            return
        info = dict(result)
        points = info.pop("points", None)
        level = info.pop("level")
        self.measured = dict(result)
        self._store(level, "edge", n, **info)
        self.draw_edge(points)
        self._edge_key = self._seen
        self.window.log_pane.info(tr("Livello dell'acqua di {0}: {1} m dal bordo dell'acqua ({2} quote, dispersione "
                                     "{3} m)", self.chunk.label, number(level, 3), number(info["edge_cells"]),
                                     number(info["spread"], 3)))

    def set_by_hand(self) -> bool:
        """The level typed (one step of Annulla)."""
        if self._gone() or self.working or not self._active():
            return False
        self._store(float(self.s_level.value()), "hand", self.n())
        self.clear_edge()
        self.window.log_pane.info(tr("Livello dell'acqua di {0}: {1} m (a mano)", self.chunk.label,
                                     number(float(self.s_level.value()), 3)))
        return True

    def _store(self, level: float, source: str, n: float, **info) -> None:
        w = self.window
        try:
            water.set_level(self.chunk, level, source=source, n=n, **info)
        except ValueError as exc:
            w._warn(tr("Livello dell'acqua: {0}", error_text(exc)))
            return
        self._recorded(tr("Livello dell'acqua"))

    def _recorded(self, text: str) -> None:
        """A change of the level stored: one step of Annulla, the tree and the window shown again."""
        w = self.window
        w.setWindowModified(True)
        if w.history is not None and not w.history.applying:
            w.history.record(text)
        w.workspace.populate(self.doc)
        self.refresh()

    # ------------------------------------------------------------------ the edge on the view
    def draw_edge(self, points) -> None:
        """The heights of the bank the level was measured on, as the point layer :data:`EDGE_LAYER` (plan only)."""
        self.clear_edge()
        view = self.window.ortho_view
        if points is None or not len(points) or self.window.chunk is not self.chunk or \
                self.window.drawings_frame is not None or view.tr is None:
            return
        view.add_dots(np.asarray(points, float), color=EDGE_COLOUR, layer=EDGE_LAYER)

    def clear_edge(self) -> None:
        self._edge_key = None
        with contextlib.suppress(RuntimeError, AttributeError):
            view = self.window.ortho_view
            if EDGE_LAYER in view.layers:
                view.remove_layer(EDGE_LAYER)

    # ------------------------------------------------------------------ the correction
    def correct(self) -> bool:
        """The dense cloud corrected in the worker as a new version (with the DEM again when asked). Returns whether
        it started."""
        if self._gone() or self.working or not self._active():
            return False
        chunk, dem = self.chunk, self.c_dem.isChecked()

        def work(p):
            try:  # the n stored with the level: the one the combo shows (a change of it is stored at once)
                return water.correct_cloud(chunk, rebuild_dem=dem, progress=p)
            except ValueError as exc:
                return {"error": exc}

        self.working = True  # no new level while the correction reads this one
        self._update_buttons()
        started = self.window.run_task("Correzione della rifrazione", work, self._corrected)
        if not started:
            self.working = False
            self._update_buttons()
        return started

    def _corrected(self, result: dict) -> None:
        with contextlib.suppress(RuntimeError):
            self.working = False
        if self._gone():
            return
        if "error" in result:
            self.window._warn(tr("Correzione della rifrazione: {0}", error_text(result["error"])))
        else:
            self.window.log_pane.info(tr("Nuvola corretta per la rifrazione: {0} punti su {1} sotto il livello {2} m, "
                                         "n {3}. Correzione parassiale: vale per foto quasi nadirali.",
                                         number(result["below"]), number(result["points"]),
                                         number(result["level"], 3), number(result["n"], 2)))
        self.refresh()

    def closeEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        self.clear_edge()
        super().closeEvent(ev)

    def reject(self) -> None:  # Esc: no closeEvent
        self.clear_edge()
        super().reject()


__all__ = ["WaterDialog", "EDGE_LAYER", "error_text", "polygon_layers"]
