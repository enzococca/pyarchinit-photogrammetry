"""Measuring on the 3D view from the window: *Misura distanza* and *Misura area e superficie 3D* of the drawing bar
measure on the model (or the cloud) when the 3D view is the one in front, or when there is no orthophoto or DEM to
measure on; the button *Misura* of the 3D view does the same and also gives the coordinates of a point. The vertices
are picked as those of the free drawings in 3D (:meth:`.model_view.ModelView.start_measure`); the measures come from
:mod:`.model_measure` and, for an area, the surface of the model inside it from the same worker as the free polygons
(:func:`.free_drawing_dialog.measure_later`). The results go where the measures of the 2D view go (status bar and log;
an area also in a message box) and are not saved with the project. A mixin of :class:`.main_window.MainWindow`."""
from __future__ import annotations

import logging

import numpy as np

from .i18n import tr

log = logging.getLogger("pyarchinit_photogrammetry.gui")

MODEL_MEASURE_KINDS = {"measure": "line", "area": "polygon"}  # the tools of the drawing bar -> what is picked in 3D


class ModelMeasureActions:
    """The measures on the 3D view (see the module)."""

    _model_measure: dict | None = None  # the last measure on the model: kind, xyz (chunk), values, metric, text
    _measuring_on_model = False  # a measuring tool of the drawing bar is measuring in the 3D view

    def _connect_model_measure(self) -> None:
        mv = self.model_view
        mv.measure_drawn.connect(self._model_measured)
        mv.measure_live.connect(lambda text: self.statusBar().showMessage(text, 15000))
        mv.measure_stopped.connect(self._model_measure_stopped)

    def _model_in_front(self) -> bool:
        return self.stack.currentWidget() is self.tabs and self.tabs.currentWidget() is self.model_view

    def _measure_on_model(self, mode: str) -> bool:
        """*Misura distanza* or *Misura area* on the model: when the 3D view is in front, or when there is no raster
        to measure on in 2D and the 3D view shows something to measure on. True when the tool was handled here (on the
        model, or refused with a word); False: the 2D view measures as before."""
        if mode not in MODEL_MEASURE_KINDS:
            return False
        mv = self.model_view
        if not self._model_in_front():
            if self.ortho_view.tr is not None or not mv.can_draw:
                return False
            self.show_view(mv)  # nothing to measure on in 2D: the model is
        if not mv.can_draw:
            self.draw_actions["pan"].setChecked(True)
            self._warn(tr("Per misurare nella vista 3D mostra il modello, la nuvola densa o i punti di legame"))
            return True
        self.ortho_view.set_mode("pan")  # the 2D view idle meanwhile
        mv.start_measure(MODEL_MEASURE_KINDS[mode])
        self._measuring_on_model = True
        self.draw_actions[mode].setChecked(True)
        hint = (tr("Misura sul modello: clic per i vertici, doppio clic o Invio per finire, Backspace toglie l'ultimo "
                   "vertice, Esc annulla; trascina per ruotare la vista") if mode == "measure" else
                tr("Area sul modello: clic per i vertici, doppio clic o Invio per chiudere, Esc annulla; area in "
                   "pianta, nel piano medio e superficie del modello"))
        self.statusBar().showMessage(hint, 20000)
        return True

    def _leave_model_measure(self) -> None:
        """Another tool of the drawing bar chosen: the measure in progress on the model given up."""
        if self.model_view.measuring:
            self._measuring_on_model = False
            self.model_view.stop_drawing()

    def _model_measure_stopped(self) -> None:
        """The measuring tool of the 3D view left (Esc, another tool or chunk): the drawing bar back on *Sposta* when
        one of its tools was measuring there."""
        if self._measuring_on_model:
            self._measuring_on_model = False
            self.draw_actions["pan"].setChecked(True)

    def _model_measured(self, kind: str, internal, draped) -> None:
        """A measure ended in the 3D view: its values in the chunk's coordinates; an area also with the surface of
        the model inside it, taken in the background when the chunk has a model."""
        from . import model_measure as mm

        ch = self.chunk
        if ch is None:
            return
        world = ch.to_world(np.asarray(internal, np.float64).reshape(-1, 3))
        draped_w = None if draped is None else ch.to_world(np.asarray(draped, np.float64).reshape(-1, 3))
        try:
            v = mm.values(kind, world, draped_w)
        except ValueError:
            self._warn(tr("Misura non valida: i vertici sono troppo pochi o tutti su una linea"))
            return
        entry = {"kind": kind, "xyz": world, "values": v, "metric": mm.is_metric(ch), "text": None}
        self._model_measure = entry
        model = ch.product_path("model") if kind == "polygon" else None
        if model is None or not model.exists():
            self._model_measure_done(entry)
            return
        from . import free_drawing_dialog as fdd
        from ..engine.orient import toward_cameras

        doc = self.doc

        def done(m) -> None:
            if self.doc is not doc or self.chunk is not ch or self._model_measure is not entry:
                return  # another project, chunk or measure meanwhile
            entry["values"]["model_surface"] = None if m is None else m.get("model_surface")
            self._model_measure_done(entry)

        rec = {"kind": "polygon", "xyz": world.tolist(), "name": tr("Misura")}
        if fdd.measure_later(self, ch, rec, toward_cameras(ch, world), done, words=lambda _rec, m: m):
            self.statusBar().showMessage(tr("Superficie del modello in calcolo..."), 10000)

    def _model_measure_done(self, entry: dict) -> None:
        from . import model_measure as mm

        ch = self.chunk
        text = mm.text(entry["kind"], entry["values"], entry["metric"], getattr(ch, "crs", None))
        entry["text"] = text
        self.statusBar().showMessage(text, 30000)
        self.model_view.canvas.set_hint(text)
        if entry["kind"] == "polygon":  # as Misura area on the 2D view: in a box (and in the log)
            self._info(tr("Misura sul modello"), text)
        else:  # as Misura distanza on the 2D view: in the status bar and the log
            self.log_pane.info(text)
