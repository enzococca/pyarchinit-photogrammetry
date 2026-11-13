"""Measuring on the 3D view from the window: *Misura distanza* and *Misura area e superficie 3D* of the drawing bar
measure on the model (or the cloud) when the 3D view is the one in front, or when there is no orthophoto or DEM to
measure on; the button *Misura* of the 3D view does the same and also gives the coordinates of a point. The vertices
are picked as those of the free drawings in 3D (:meth:`.model_view.ModelView.start_measure`); the measures come from
:mod:`.model_measure` and, for an area, the surface of the model inside it from the same worker as the free polygons
(:func:`.free_drawing_dialog.measure_later`). The results go where the measures of the 2D view go (status bar and log;
an area also in a message box) and are not saved with the project.

The same button sets the units of a chunk only aligned (local frame, no references), measured in model units until
then: *Il modello è in metri* declares its coordinates metres (``chunk.meta["metric"]``, the flag an imported model or
a laser scan carries), *Scala da una distanza nota* scales it so that two points picked on the model are as far apart
as the distance typed. The scale is a motion of the chunk as *Orienta il sistema locale* makes one
(:func:`..engine.orient.apply`): its transform and everything kept in its coordinates carried, the rasters on a grid
stale, one step of *Modifica > Annulla* (:class:`ScaleStep`). A mixin of :class:`.main_window.MainWindow`."""
from __future__ import annotations

import logging

import numpy as np

from .i18n import decimal_comma, tr
from .orient_dialog import OrientStep

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
        mv.scale_picked.connect(self._scale_picked)
        mv.a_model_metric.triggered.connect(self._set_model_metric)
        mv.a_scale_distance.triggered.connect(self.scale_from_distance)
        mv.measure_menu.aboutToShow.connect(self._update_units_actions)

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

    def _model_measure_follow(self, chunk, m=None) -> None:
        """The last measure on the model said again after the units of ``chunk`` changed: in metres or in model units
        as the chunk is now and, with the motion ``m`` (a scale, its undo or redo), its vertices and values carried by
        it. Nothing when another chunk is the active one or no measure was taken."""
        from ..core import geometry as geo
        from . import model_measure as mm

        entry = self._model_measure
        if entry is None or chunk is not self.chunk or entry.get("text") is None:
            return
        if m is not None:
            s = geo.decompose_similarity(np.asarray(m, float))[0]
            old = entry["values"]
            entry["xyz"] = geo.apply(np.asarray(m, float), np.asarray(entry["xyz"], np.float64).reshape(-1, 3))
            v = mm.values(entry["kind"], entry["xyz"])
            if old.get("length_surface") is not None:  # a uniform scale: the length along the surface with it
                v["length_surface"] = old["length_surface"] * s
            if "model_surface" in old:
                v["model_surface"] = None if old["model_surface"] is None else old["model_surface"] * s * s
            entry["values"] = v
        entry["metric"] = mm.is_metric(chunk)
        entry["text"] = mm.text(entry["kind"], entry["values"], entry["metric"], getattr(chunk, "crs", None))
        self.statusBar().showMessage(entry["text"], 30000)
        self.model_view.canvas.set_hint(entry["text"])

    # ---------------------------------------------------------------- the units of the chunk
    def _update_units_actions(self) -> None:
        """*Il modello è in metri* checked as the active chunk is: checked and disabled, with the reason in its
        tooltip, when something else than the flag makes it metres (its system, references, scale bars, a scale);
        *Scala da una distanza nota* disabled when that something gives the scale."""
        from . import model_measure as mm

        mv, ch = self.model_view, self.chunk
        a, b = mv.a_model_metric, mv.a_scale_distance
        reason = mm.metric_reason(ch)
        if ch is None:
            a.setChecked(False)
            a.setEnabled(False)
            a.setToolTip("")
        elif reason is not None:
            a.setChecked(True)
            a.setEnabled(False)
            a.setToolTip(tr("Già in metri: {0}", reason))
        else:
            a.setChecked(bool(ch.meta.get("metric")))
            a.setEnabled(True)
            a.setToolTip(tr("Le coordinate del chunk sono metri (un modello misurato in metri): le misure in metri "
                            "invece che in unità del modello"))
        refusal = None if ch is None else self._scale_refusal(ch)
        b.setEnabled(ch is not None and refusal is None)
        b.setToolTip(refusal or tr("Due punti sul modello e la loro distanza vera in metri: il chunk scalato di "
                                   "conseguenza"))

    def _set_model_metric(self, checked: bool) -> None:
        """*Il modello è in metri* switched by the user: ``meta["metric"]`` set or removed on the active chunk, one
        step of *Modifica > Annulla*, the project modified and the last measure said in the new units."""
        from . import model_measure as mm

        ch = self.chunk
        if ch is None:
            return
        reason = mm.metric_reason(ch)
        if reason is not None:  # metres anyway: nothing to set
            self._update_units_actions()
            return
        before = bool(ch.meta.get("metric"))
        if before == bool(checked):
            return
        step = MetricStep(self, ch, bool(checked))
        step.apply(bool(checked))
        if self.history is not None:
            self.history.push(tr("Il modello è in metri") if checked else tr("Il modello non è in metri"), step,
                              document=False)
        self._dirty(True)
        self.log_pane.info(tr("Chunk {0}: coordinate in metri", ch.label) if checked else
                           tr("Chunk {0}: coordinate in unità del modello", ch.label))

    def _scale_refusal(self, ch) -> str | None:
        """Why *Scala da una distanza nota* cannot scale ``ch``, in words; None when it can: the scale is given by its
        system, by markers with references, by scale bars, by the positions of the photos or by the reference chunk it
        is aligned to."""
        from ..core import crs as crs_mod
        from ..engine import orient

        if not crs_mod.is_local(ch.crs):
            return tr("Il chunk è nel sistema {0}: la scala la danno i riferimenti, non una distanza", ch.crs)
        if any(m.reference.location is not None and m.reference.enabled for m in ch.markers):
            return tr("La scala la danno i marker con coordinate: cambia le loro coordinate o disattivali")
        if any(getattr(sb, "enabled", True) for sb in ch.scalebars):
            return tr("La scala la danno le barre di scala: cambia le loro distanze")
        try:
            orient.check(ch)
        except orient.OrientRefused as exc:
            if exc.reason == "cameras":
                return tr("La scala la danno le posizioni delle foto: togli i loro riferimenti o disattivali")
            if exc.reason == "aligned_to":
                return tr("Il chunk è allineato a {0}: scala il chunk di riferimento, quelli allineati lo seguono",
                          ", ".join(exc.names))
            from .orient_dialog import refusal_text

            return refusal_text(exc)
        return None

    def scale_from_distance(self) -> bool:
        """*Scala da una distanza nota*: refused in words when the chunk's scale comes from elsewhere
        (:meth:`_scale_refusal`) or nothing in the 3D view can be measured on; otherwise two points picked on the
        model (:meth:`.model_view.ModelView.start_scale_pick`), then :meth:`_scale_picked`. True when the pick
        started."""
        ch, mv = self.chunk, self.model_view
        if ch is None:
            return False
        refusal = self._scale_refusal(ch)
        if refusal is not None:
            self._warn(refusal)
            return False
        if not self._model_in_front():
            self.show_view(mv)
        if not mv.can_draw or not mv.start_scale_pick():
            self._warn(tr("Per scalare il modello mostra il modello, la nuvola densa o i punti di legame nella vista "
                          "3D"))
            return False
        self.draw_actions["pan"].setChecked(True)
        self.ortho_view.set_mode("pan")
        self.statusBar().showMessage(tr("Scala: clic sui due estremi della distanza nota sul modello; Esc annulla"),
                                     20000)
        return True

    def _scale_picked(self, internal) -> None:
        """The two points of *Scala da una distanza nota* picked (internal): their distance shown in a dialog
        (:class:`.dialogs.KnownDistanceDialog`), the true one asked in metres, then :meth:`scale_chunk`."""
        from . import model_measure as mm
        from .dialogs import KnownDistanceDialog

        ch = self.chunk
        if ch is None:
            return
        world = ch.to_world(np.asarray(internal, np.float64).reshape(-1, 3))
        measured = float(np.linalg.norm(world[1] - world[0])) if len(world) >= 2 else 0.0
        if not measured > 1e-9:
            self._warn(tr("I due punti coincidono: scegli due punti distinti"))
            return
        true = KnownDistanceDialog.ask(self, measured, mm.is_metric(ch))
        if true is None:
            return
        self.scale_chunk(world[0], world[1], true)

    def scale_chunk(self, a, b, distance: float) -> dict | None:
        """``self.chunk`` scaled so that the points ``a`` and ``b`` (chunk coordinates) are ``distance`` metres apart:
        the similarity of factor ``distance / |ab|`` about ``a`` (the first point stays where it is, the model does
        not jump away), applied as an orientation is (:func:`..engine.orient.apply`: the transform pre-multiplied,
        the drawings, markers' references, sections, elevations and proxies carried, the chunks aligned to it moved
        too, the rasters on a grid stale) but leaving ``meta["levelled"]`` as it was (a scale does not level the
        chunk). ``meta["metric"]`` set, so that the chunk counts as metres also when the factor is exactly 1. One step
        of *Modifica > Annulla* (:class:`ScaleStep`), the project saved, the factor in the log, the views shown again
        and the picked distance as the last measure. Refused in words (:meth:`_scale_refusal`, two points that
        coincide): None; else the result of :func:`..engine.orient.apply` with ``M``."""
        from ..core import geometry as geo
        from ..engine import orient
        from . import model_measure as mm

        ch = self.chunk
        if ch is None:
            return None
        refusal = self._scale_refusal(ch)
        if refusal is not None:
            self._warn(refusal)
            return None
        a, b = (np.asarray(p, np.float64).reshape(3) for p in (a, b))
        try:
            s = orient.scale_for(a, b, float(distance))
        except ValueError:
            self._warn(tr("I due punti coincidono: scegli due punti distinti"))
            return None
        m = geo.compose_similarity(s, np.eye(3), a - s * a)
        self._rustgis_leave()
        self._store_drawings()
        self._save_sections()
        moving = [ch] + orient.aligned_to(ch)
        levelled = [(c, "levelled" in c.meta, c.meta.get("levelled")) for c in moving]
        previous = {"metric": ch.meta["metric"]} if "metric" in ch.meta else {}
        try:
            result = orient.apply(ch, m)
        except orient.OrientRefused as exc:
            from .orient_dialog import refusal_text

            self._warn(refusal_text(exc))
            return None
        for c, had, value in levelled:  # a scale about a point does not level the chunk
            if had:
                c.meta["levelled"] = value
            else:
                c.meta.pop("levelled", None)
        ch.meta["metric"] = True
        result.update(M=m.tolist(), previous=previous, scale=s)
        dropped = self._orient_read_again()
        if self.history is not None and result.get("state") is not None:
            self.history.push(tr("Scala da una distanza nota"), ScaleStep(self, ch, result), document=False)
        saved = False
        if self.doc.path:
            try:
                self.doc.save()
                saved = True
            except Exception as exc:  # noqa: BLE001 - the scale is applied: the project stays to be saved
                log.error("Salvataggio non riuscito: %s", exc)
        self._dirty(not saved)
        self._orient_show()
        if self.history is not None:
            self.history.rebase()
        head = tr("Chunk {0} scalato di un fattore {1} attorno al primo punto: {2} m tra i due punti", ch.label,
                  decimal_comma(f"{s:.6f}"), decimal_comma(f"{float(distance):.4f}"))
        lines = [head] + self._orient_text(result).splitlines()[1:]
        if dropped:
            lines.append(tr("Da ridisegnare nel nuovo sistema: {0}.", ", ".join(dropped)))
        stale = result.get("stale") or []
        if stale:
            self._said_with_rebuild(ch, tr("Scala da una distanza nota"), "\n".join(lines), stale)
        else:
            for line in lines:
                self.log_pane.info(line)
        # the picked distance as the last measure, in the new units: it reads the distance typed
        world = np.array([geo.apply(m, a[None])[0], geo.apply(m, b[None])[0]])
        entry = {"kind": "line", "xyz": world, "values": mm.values("line", world), "metric": mm.is_metric(ch),
                 "text": None}
        self._model_measure = entry
        self._model_measure_done(entry)
        self.statusBar().showMessage(head, 30000)
        self._update_units_actions()
        return result


class MetricStep:
    """*Il modello è in metri* switched on ``chunk`` (``meta["metric"]`` set or removed) as one step of *Modifica >
    Annulla*: undone and done again by setting the flag back, the window told (:meth:`ModelMeasureActions.
    _model_measure_follow`)."""

    def __init__(self, window, chunk, metric: bool):
        self.window, self.chunk, self.metric = window, chunk, metric

    def apply(self, metric: bool) -> None:
        if metric:
            self.chunk.meta["metric"] = True
        else:
            self.chunk.meta.pop("metric", None)
        self.window._model_measure_follow(self.chunk)
        self.window._update_units_actions()

    def undo(self) -> None:
        self.apply(not self.metric)
        self.window._dirty(True)

    def redo(self) -> None:
        self.apply(self.metric)
        self.window._dirty(True)


class ScaleStep(OrientStep):
    """*Scala da una distanza nota* as one step of *Modifica > Annulla*: the orientation's step
    (:class:`.orient_dialog.OrientStep`: the state taken by :func:`..engine.orient.apply` put back, the listeners told
    of the inverse motion) with ``meta["metric"]`` as it was, and the last measure carried."""

    KEYS = ("metric",)

    def undo(self) -> None:
        super().undo()
        self.window._model_measure_follow(self.chunk, np.linalg.inv(self.m))
        self.window._update_units_actions()

    def redo(self) -> None:
        if self.after is None:
            return
        super().redo()
        self.window._model_measure_follow(self.chunk, self.m)
        self.window._update_units_actions()
