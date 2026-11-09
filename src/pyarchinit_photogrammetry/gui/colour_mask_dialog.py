"""*Strumenti > Maschera dal colore...*: the cells of the orthophoto within a colour range (:mod:`..engine.colour_mask`)
as polygons of a layer of free drawings - the water of a flooded trench, a tarpaulin, the sand around a wreck - for the
crop of the products (*Ritaglia con questa maschera* in the Layer pane), the stable height of the ties and every
export of the free drawings.

Three ranges (hue in degrees, saturation and value in %: a hue whose low end is above its high end goes through 0, the
reds), *Campiona dall'ortofoto* (a click on the view: the range around the colour there), the cleaning (opening,
holes, the largest region or all, smallest area, margin). The preview is computed at once on a small copy of the
orthophoto (:data:`PREVIEW_SIDE` pixels a side) and drawn on the view; *Crea il layer* computes the mask on the copy of
:data:`..engine.colour_mask.MASK_SIDE` pixels in the worker and writes the layer: one step of *Modifica > Annulla*.
The window is not modal; it works on the chunk active when it was opened.
"""
from __future__ import annotations

import contextlib

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QSlider, QVBoxLayout, QWidget)

from ..engine import colour_mask
from . import i18n
from .i18n import tr
from .ties_dialog import undo_shortcuts

PREVIEW_SIDE = 1000  # pixels a side of the copy of the orthophoto the preview is computed on
PREVIEW_LAYER = "Anteprima della maschera"  # the layer of the 2D view that shows the preview
PREVIEW_MAX = 300  # polygons of the preview drawn, at most (the largest)
PREVIEW_COLOUR = "#00a5e0"


def error_text(exc: Exception) -> str:
    """An error of the mask in the language of the interface: a :class:`..engine.colour_mask.MaskError` has both."""
    if isinstance(exc, colour_mask.MaskError):
        return exc.italian if i18n.current() == "it" else exc.english
    return i18n.translate(str(exc))


class RangeRow(QWidget):
    """Low and high of one channel: two sliders with their values."""

    def __init__(self, top: int, unit: str, low: float, high: float, parent=None):
        super().__init__(parent)
        lay = QGridLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.unit = unit
        self.low, self.high = QSlider(Qt.Orientation.Horizontal), QSlider(Qt.Orientation.Horizontal)
        self.l_low, self.l_high = QLabel(), QLabel()
        for r, (s, lab, v, what) in enumerate(((self.low, self.l_low, low, tr("minimo")),
                                               (self.high, self.l_high, high, tr("massimo")))):
            s.setRange(0, top)
            s.setValue(int(round(v)))
            lab.setMinimumWidth(lab.fontMetrics().horizontalAdvance("360 gradi") + 6)
            lay.addWidget(QLabel(what), r, 0)
            lay.addWidget(s, r, 1)
            lay.addWidget(lab, r, 2)
            s.valueChanged.connect(self._show)
        self._show()

    def _show(self) -> None:
        self.l_low.setText(f"{self.low.value()} {self.unit}")
        self.l_high.setText(f"{self.high.value()} {self.unit}")

    def values(self) -> tuple[float, float]:
        return float(self.low.value()), float(self.high.value())

    def set_values(self, lo: float, hi: float) -> None:
        self.low.setValue(int(round(lo)))
        self.high.setValue(int(round(hi)))


class ColourMaskDialog(QDialog):
    """The mask from a colour range of the active chunk (see the module)."""

    @classmethod
    def ask(cls, window) -> "ColourMaskDialog":
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
        self.setWindowTitle(tr("Maschera dal colore"))
        self.image = None  # the small copy of the orthophoto of the preview (engine.colour_mask.Ortho)
        self.working = False
        self.preview: colour_mask.MaskResult | None = None

        lay = QVBoxLayout(self)
        intro = QLabel(tr("Le celle dell'ortofoto in un intervallo di colori diventano i poligoni di un layer di "
                          "disegni liberi, con le quote del DEM: l'acqua di uno scavo allagato, un telo, la sabbia "
                          "attorno a un relitto. Il layer serve a ritagliare i prodotti (Ritaglia con questa maschera "
                          "nel pannello Layer), come area della quota stabile dei legami e in ogni esportazione dei "
                          "disegni. Una tinta con il primo valore più alto del secondo passa per lo 0 (i rossi)."))
        intro.setWordWrap(True)
        lay.addWidget(intro)

        form = QFormLayout()
        self.r_hue = RangeRow(360, tr("gradi"), 170, 210)
        self.r_sat = RangeRow(100, "%", 30, 100)
        self.r_val = RangeRow(100, "%", 20, 100)
        form.addRow(tr("Tinta"), self.r_hue)
        form.addRow(tr("Saturazione"), self.r_sat)
        form.addRow(tr("Valore (luminosità)"), self.r_val)
        sample_row = QHBoxLayout()
        self.b_sample = QPushButton(tr("Campiona dall'ortofoto"))
        self.b_sample.setToolTip(tr("Un clic sull'ortofoto: l'intervallo attorno al colore di un piccolo cerchio lì"))
        self.s_radius = QDoubleSpinBox()
        self.s_radius.setRange(0.005, 5.0)
        self.s_radius.setDecimals(3)
        self.s_radius.setValue(0.1)
        self.s_radius.setSuffix(" m")
        self.s_radius.setToolTip(tr("Raggio del cerchio campionato"))
        sample_row.addWidget(self.b_sample)
        sample_row.addWidget(QLabel(tr("Raggio")))
        sample_row.addWidget(self.s_radius)
        sample_row.addStretch(1)
        form.addRow("", sample_row)
        self.s_opening = self._metres(0.0, 10.0, 0.05, tr("Le macchie più piccole di questa misura sono tolte"))
        form.addRow(tr("Togli le macchie fino a"), self.s_opening)
        self.c_holes = QCheckBox(tr("Riempi i buchi"))
        self.c_holes.setChecked(True)
        form.addRow("", self.c_holes)
        self.c_keep = QComboBox()
        self.c_keep.addItem(tr("Solo la regione più grande"), "largest")
        self.c_keep.addItem(tr("Tutte le regioni"), "all")
        form.addRow(tr("Regioni"), self.c_keep)
        self.s_min_area = QDoubleSpinBox()
        self.s_min_area.setRange(0.0, 1e6)
        self.s_min_area.setDecimals(3)
        self.s_min_area.setValue(0.05)
        self.s_min_area.setSuffix(" m2")
        form.addRow(tr("Area minima"), self.s_min_area)
        self.s_margin = self._metres(-10.0, 10.0, 0.0, tr("Metri aggiunti attorno ai poligoni (negativo: tolti)"))
        form.addRow(tr("Margine"), self.s_margin)
        self.e_layer = QLineEdit(colour_mask.next_layer_name(self.chunk))
        self.e_layer.setToolTip(tr("Il layer dei disegni liberi: una maschera già in quel layer viene sostituita, i "
                                   "disegni fatti a mano restano"))
        form.addRow(tr("Layer"), self.e_layer)
        lay.addLayout(form)
        self.l_preview = QLabel()
        self.l_preview.setWordWrap(True)
        lay.addWidget(self.l_preview)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.b_create = QPushButton(tr("Crea il layer"))
        self.b_close = QPushButton(tr("Chiudi"))
        buttons.addWidget(self.b_create)
        buttons.addWidget(self.b_close)
        lay.addLayout(buttons)
        undo_shortcuts(self, window)
        for button in self.findChildren(QPushButton):  # Enter in a field does not press a button (nor makes it
            button.setAutoDefault(False)  # the default one, in bold: its text would no longer fit)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.update_preview)
        for row in (self.r_hue, self.r_sat, self.r_val):
            row.low.valueChanged.connect(self._changed)
            row.high.valueChanged.connect(self._changed)
        for w in (self.s_opening, self.s_min_area, self.s_margin):
            w.valueChanged.connect(self._changed)
        self.c_holes.toggled.connect(self._changed)
        self.c_keep.currentIndexChanged.connect(self._changed)
        self.b_sample.clicked.connect(self.start_sample)
        self.b_create.clicked.connect(self.create)
        self.b_close.clicked.connect(self.close)
        self.update_preview()

    def _metres(self, lo: float, hi: float, value: float, tip: str) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(3)
        s.setSingleStep(0.01)
        s.setValue(value)
        s.setSuffix(" m")
        s.setToolTip(tip)
        return s

    # ------------------------------------------------------------------ state
    def _gone(self) -> bool:
        try:
            self.isVisible()
        except RuntimeError:
            return True
        return self.window.doc is not self.doc or self.chunk not in self.doc.chunks

    def range(self) -> colour_mask.HsvRange:
        """The range of the sliders (ValueError when a low end of saturation or value is above its high end)."""
        return colour_mask.HsvRange(self.r_hue.values(), self.r_sat.values(), self.r_val.values())

    def set_range(self, rng: colour_mask.HsvRange) -> None:
        for row, (lo, hi) in ((self.r_hue, rng.hue), (self.r_sat, rng.sat), (self.r_val, rng.val)):
            row.set_values(lo, hi)  # each move starts the timer of the preview again

    def options(self) -> dict:
        return {"opening": self.s_opening.value(), "fill_holes": self.c_holes.isChecked(),
                "keep": self.c_keep.currentData(), "min_area": self.s_min_area.value(),
                "margin": self.s_margin.value()}

    def _changed(self, *_a) -> None:
        self._timer.start()

    # ------------------------------------------------------------------ preview
    def update_preview(self) -> colour_mask.MaskResult | None:
        """The mask on the small copy: its regions and area said, its polygons drawn on the view (on the plan)."""
        self._timer.stop()
        if self._gone():
            return None
        try:
            if self.image is None:
                self.image = colour_mask.read_orthophoto(self.chunk, side=PREVIEW_SIDE)
            rng = self.range()
            self.preview = colour_mask.mask(self.chunk, rng, image=self.image, **self.options())
        except ValueError as exc:
            self.preview = None
            self.l_preview.setText(tr("Anteprima: {0}", error_text(exc)))
            self._clear_preview()
            self._update_buttons()
            return None
        r = self.preview
        self.l_preview.setText(tr("Anteprima ridotta (celle di {0} m): {1} regioni, {2} m2, {3} % dell'ortofoto. "
                                  "Il layer si calcola su celle più fini: macchie e aree minime possono cambiare.",
                                  f"{r.cell:.3f}", len(r.polygons), f"{r.area:.2f}", f"{100 * r.share:.1f}"))
        self._draw_preview(r)
        self._update_buttons()
        return r

    def _draw_preview(self, r: colour_mask.MaskResult) -> None:
        view = self.window.ortho_view
        self._clear_preview()
        if self.window.chunk is not self.chunk or self.window.drawings_frame is not None or view.tr is None:
            return  # another chunk or an elevation shown: its coordinates are not these
        with view.batch():
            for pg in r.polygons[:PREVIEW_MAX]:
                view.add_polygon(np.asarray(pg.exterior.coords, float)[:-1], color=PREVIEW_COLOUR, fill_alpha=60,
                                 layer=PREVIEW_LAYER)

    def _clear_preview(self) -> None:
        with contextlib.suppress(RuntimeError, AttributeError):
            view = self.window.ortho_view
            if PREVIEW_LAYER in view.layers:
                view.remove_layer(PREVIEW_LAYER)

    def _update_buttons(self) -> None:
        with contextlib.suppress(RuntimeError):
            self.b_create.setEnabled(not self.working and self.preview is not None and bool(self.preview.polygons))
            self.b_sample.setEnabled(not self.working)

    # ------------------------------------------------------------------ sample
    def start_sample(self) -> bool:
        """A click on the view asked for (:meth:`MainWindow.request_geometry`): the range around the colour there."""
        if self._gone():
            return False
        if self.window.chunk is not self.chunk:
            self.window._warn(tr("Maschera dal colore: rendi attivo il chunk {0} per campionare.", self.chunk.label))
            return False
        if self.window._plan_only():
            return False
        return self.window.request_geometry("point", self.sampled,
                                            hint=tr("Clic sull'ortofoto: il colore da cercare (Esc annulla)"))

    def sampled(self, xy) -> None:
        if self._gone():
            return
        x, y = (float(v) for v in np.asarray(xy, float).reshape(-1, 2)[0])
        try:
            rng = colour_mask.sample(self.chunk, x, y, radius=self.s_radius.value())
        except ValueError as exc:
            self.window._warn(tr("Maschera dal colore: {0}", error_text(exc)))
            return
        self.set_range(rng)
        self.update_preview()

    # ------------------------------------------------------------------ create
    def create(self) -> bool:
        """The mask computed in the worker on the copy of ``MASK_SIDE`` pixels, then written as the layer (one step of
        *Modifica > Annulla*). Returns whether it started."""
        if self._gone() or self.working:
            return False
        try:
            rng = self.range()
        except ValueError as exc:
            self.window._warn(tr("Maschera dal colore: {0}", error_text(exc)))
            return False
        layer = self.e_layer.text().strip() or colour_mask.next_layer_name(self.chunk)
        chunk, opts = self.chunk, self.options()

        def work(p):
            try:
                return colour_mask.mask(chunk, rng, progress=p, **opts)
            except ValueError as exc:
                return {"error": exc}

        self.working = True
        self._update_buttons()
        started = self.window.runner.run("Maschera dal colore", work, lambda result: self._made(result, layer),
                                         self._failed)
        if not started:
            self.working = False
            self._update_buttons()
        return started

    def _failed(self, summary: str, details: str = "") -> None:
        with contextlib.suppress(RuntimeError):
            self.working = False
            self._update_buttons()
        self.window._error(summary, details)

    def _made(self, result, layer: str) -> None:
        with contextlib.suppress(RuntimeError):
            self.working = False
            self._update_buttons()
        if self._gone():
            return
        if isinstance(result, dict):
            self.window._warn(tr("Maschera dal colore: {0}", error_text(result["error"])))
            return
        w = self.window
        if w.chunk is not self.chunk:  # another chunk active: the step of Annulla would be lost with its history
            w._warn(tr("Maschera dal colore: il chunk attivo è cambiato durante il calcolo. Rendi attivo {0} e crea "
                       "di nuovo il layer.", self.chunk.label))
            return
        try:
            ids = colour_mask.to_layer(self.chunk, result, layer=layer)
        except ValueError as exc:
            w._warn(tr("Maschera dal colore: {0}", error_text(exc)))
            return
        w.setWindowModified(True)
        if w.history is not None and not w.history.applying:
            w.history.record(tr("Maschera dal colore"))
        self._clear_preview()
        w._draw_free()
        w.log_pane.info(tr("Maschera dal colore: layer {0}, {1} poligoni, {2} m2", layer, len(ids),
                           f"{result.area:.2f}"))
        if result.holes:
            w.log_pane.info(tr("Maschera dal colore: {0} buchi lasciati fuori (un disegno libero ha un solo "
                               "contorno)", result.holes))
        self.e_layer.setText(colour_mask.next_layer_name(self.chunk))

    def closeEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        self._timer.stop()
        self._clear_preview()
        super().closeEvent(ev)

    def reject(self) -> None:  # Esc: no closeEvent
        self._timer.stop()
        self._clear_preview()
        super().reject()


__all__ = ["ColourMaskDialog", "RangeRow", "PREVIEW_LAYER"]
