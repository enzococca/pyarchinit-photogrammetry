"""The window's side of a wall traced on the plan (:mod:`..engine.wall_trace`, :class:`.wall_path_pane.WallPathPane`):
the line clicked on the 2D view, snapped onto the face of the model and given its default heights in the worker, the
elevation built on it in the worker; the line as clicked and as snapped drawn on the plan while it waits
(:data:`TRACE_PREVIEW_LAYER`), and the traces of the elevations of the chunk always (``TRACES_LAYER``) - a double
click on one shows its elevation."""
from __future__ import annotations

import logging

import numpy as np

from .i18n import tr

log = logging.getLogger("pyarchinit_photogrammetry")

TRACE_PREVIEW_LAYER = "Traccia del muro (anteprima)"
MERGE_CLICKS = 0.001  # m: clicks closer than this are one point (a double click, a slip of the hand)
CLOSE_CLICK = 0.05  # m: a last click this near the first closes the line on it (a room traced all round)
THIN_SIDE = 0.6  # the photos on the weaker side weigh this share of the stronger at least: the side is asked
COLOUR_CLICKED = "#ffd000"
COLOUR_SNAPPED = "#00e5ff"
COLOUR_TRACES = "#ff8c00"


def clean_clicks(xy) -> np.ndarray:
    """The clicks of a line: those closer than :data:`MERGE_CLICKS` to the one before taken once; a last click within
    :data:`CLOSE_CLICK` of the first (on a line of four points at least) put on it exactly - the line of a room."""
    xy = np.asarray(xy, float).reshape(-1, 2)
    keep = [xy[0]] if len(xy) else []
    for p in xy[1:]:
        if np.hypot(*(p - keep[-1])) >= MERGE_CLICKS:
            keep.append(p)
    out = np.array(keep, float).reshape(-1, 2)
    if len(out) >= 4 and np.hypot(*(out[-1] - out[0])) < CLOSE_CLICK:
        out[-1] = out[0]
    return out


def refusal_text(exc) -> str:
    """The words of the window for a line the frame of a wall refuses (:class:`..engine.wall_path.TraceRefused`)."""
    reason = getattr(exc, "reason", "")
    if reason == "points":
        return tr("La linea del muro ha bisogno di almeno due punti diversi: tracciala di nuovo.")
    if reason == "crosses":
        return tr("La linea del muro incrocia o tocca se stessa: tracciala di nuovo senza incroci (due muri che si "
                  "incrociano sono due prospetti; una stanza si chiude cliccando di nuovo sul primo punto).")
    if reason == "turns_back":
        return tr("La linea del muro torna indietro su se stessa (una svolta di più di 170°): le due facce di un muro "
                  "sono due prospetti. Tracciala di nuovo.")
    if reason == "side":
        return tr("Non si sa da che lato guardare il muro: nessuna foto allineata lo vede. In «Lato da cui guardare» "
                  "scegli «A sinistra della traccia» o «A destra della traccia» (rispetto al verso in cui è stata "
                  "cliccata) e traccia di nuovo.")
    return tr("La linea non può essere la traccia di un muro: tracciala di nuovo.")


def no_surface_text() -> str:
    from ..engine.elevation import FACE_ROWS

    return tr("Nessuna superficie del modello lungo la linea, nella fascia di profondità: una colonna è della faccia "
              "del muro quando il modello c'è in almeno il {0} % delle sue quote. Aggancia la linea al muro, allarga "
              "la profondità davanti e dietro o controlla le quote.", round(100 * FACE_ROWS))


def default_heights(chunk, points, side: str, front: float, back: float) -> tuple[float, float] | None:
    """The heights ``(v0, v1)`` of the wall along the line ``points`` seen from ``side``: the lowest and the highest
    of :func:`..engine.elevation.height_range` (the vertices of the model in the depth band, by count) and of the
    surface along the line weighed by area (:func:`..engine.wall_trace._heights_near`) - a ground meshed far finer
    than the wall does not cut its top - rounded outward to ``HEIGHT_STEP``. None when the model has nothing there."""
    from ..engine import elevation as E
    from ..engine import wall_trace
    from ..engine.wall_path import CLEARANCE, WallPath, corner_radii, ordered

    vertices = ordered(points, side)
    path = WallPath(vertices, radius=front + CLEARANCE, radii=corner_radii(vertices, front, back))
    found = []
    try:
        found.append(E.height_range(chunk, path, (-float(back), float(front))))
    except ValueError:
        pass
    near = wall_trace._heights_near(chunk, np.asarray(path.vertices, float), max(float(front), float(back)))
    if near is not None:
        found.append(near)
    if not found:
        return None
    lo, hi = min(f[0] for f in found), max(f[1] for f in found)
    step = E.HEIGHT_STEP
    return (round(float(np.floor(lo / step + 1e-6)) * step, 6), round(float(np.ceil(hi / step - 1e-6)) * step, 6))


class WallPathActions:
    """Methods of the main window (it provides ``chunk``, ``wall_pane``, ``ortho_view``, ``drawings_frame``,
    ``request_geometry``, ``run_task``, ``show_elevation``, ``_show_raster``, ``_need_chunk``, ``_warn``, ``_act``,
    ``_show_pane``)."""

    # ------------------------------------------------------------ menu and pane
    def _build_wall_path_actions(self, menu) -> None:
        """*Flusso di lavoro > Muro tracciato in pianta...*, after *Prospetto o sviluppo cilindrico...*."""
        self._act(menu, tr("Muro tracciato in pianta..."), lambda: self.show_wall_path_pane(start=True),
                  processing=True, icon_name="elevation",
                  tip=tr("Il prospetto di un muro che segue una linea tracciata sulla pianta (angoli, curve), "
                         "agganciata alla faccia del modello"))

    def show_wall_path_pane(self, start: bool = False) -> None:
        """The pane *Muro tracciato in pianta* in front, read again from the active chunk; ``start``: the tracing of
        a new line begins when none is there."""
        pane = self.wall_pane
        pane.refresh()
        self._show_pane(pane)
        if start and pane.clicked is None:
            self.wall_trace_start()

    def _wall_pane_ready(self) -> bool:
        """The line of the pane is of the active chunk (said when it is not)."""
        pane, ch = self.wall_pane, self.chunk
        if pane.clicked is None:
            self._warn(tr("Traccia prima la linea del muro sulla pianta."))
            return False
        if ch is None or ch.key != pane.chunk_key:
            self._warn(tr("La linea del muro è stata tracciata su un altro chunk: tracciala di nuovo su questo."))
            pane.refresh()
            return False
        return True

    # ------------------------------------------------------------ the line
    def wall_trace_start(self) -> bool:
        """The 2D view lent for the line of the wall: the plan shown first when an elevation is."""
        if not self._need_chunk(product="model"):
            return False
        ch, view = self.chunk, self.ortho_view
        on_plan = view.tr is not None and self.drawings_frame is None and view.kind != "frame"
        if not on_plan and (ch.orthomosaic_path or ch.dem_path):
            self._show_raster("ortho" if ch.orthomosaic_path else "dem")
        pane = self.wall_pane
        pane.running = "trace"
        pane.update_states()
        started = self.request_geometry("line", self._wall_trace_drawn, self._wall_trace_cancelled,
                                        tr("Clic sui vertici della linea del muro, doppio clic o Invio per finire; "
                                           "Backspace toglie l'ultimo, Esc annulla"))
        if not started:
            pane.running = None
            pane.update_states()
        return bool(started)

    def _wall_trace_cancelled(self) -> None:
        pane = self.wall_pane
        if pane.running == "trace":
            pane.running = None
        pane.update_states()

    def _wall_side(self, points, asked: str):
        """The side of the line ``points`` as the pane asks it (a word): for "auto", from the photos, the owner asked
        when the two sides weigh nearly the same. None when the owner gave up; :class:`TraceRefused` as
        :func:`..engine.wall_trace.side_of`."""
        from ..engine import wall_trace
        from .dialogs import choice

        pane = self.wall_pane
        if asked != "auto":
            return wall_trace.side_of(self.chunk, points, asked)
        which = wall_trace.side_of(self.chunk, points, "auto")
        votes = wall_trace.side_votes(self.chunk, points)
        pane.votes = votes
        strong, weak = max(votes.values()), min(votes.values())
        if strong > 0.0 and weak >= THIN_SIDE * strong:
            from .i18n import number

            k = choice(self, "question", tr("Lato del muro"),
                       tr("Le foto vedono il muro quasi allo stesso modo dai due lati della linea (peso a sinistra "
                          "{0}, a destra {1}, rispetto al verso in cui è stata cliccata): da che lato si guarda la "
                          "faccia del muro?", number(votes["left"], 2), number(votes["right"], 2)),
                       [tr("A sinistra della traccia"), tr("A destra della traccia")],
                       default=0 if which == "left" else 1)
            if k is None:
                return None
            which = ("left", "right")[k]
            i = pane.side.findData(which)
            pane.side.blockSignals(True)
            pane.side.setCurrentIndex(i)
            pane.side.blockSignals(False)
        return which

    def _wall_trace_drawn(self, xy) -> bool:
        """The line clicked: checked at once (a line that crosses itself, no side), then snapped in the worker."""
        from ..engine import wall_trace
        from ..engine.wall_path import TraceRefused, WallPath, corner_radii

        pane = self.wall_pane
        pane.running = None
        points = clean_clicks(xy)
        v = pane.values()
        try:
            WallPath(points, radii=corner_radii(points, v["front"], v["back"]))  # the line first, then its side
            which = self._wall_side(points, v["side"])
            if which is None:
                pane.update_states()
                return False
            wall_trace.path_from_trace(self.chunk, points, which, v["front"], v["back"], snap=False)
        except TraceRefused as exc:
            pane.set_trace(None)
            self._draw_trace_preview()
            self._warn(refusal_text(exc))
            return False
        pane.set_trace(points)
        self._draw_trace_preview()
        return self.wall_snap()

    def wall_snap(self) -> bool:
        """The line of the pane snapped onto the face (when *Aggancia al muro* is ticked) and its default heights, in
        one task of the worker; the pane and the preview shown again with them."""
        from ..engine import wall_trace
        from ..engine.wall_path import TraceRefused, WallPath, corner_radii, ordered, smooth_trace

        if not self._wall_pane_ready():
            return False
        pane, ch = self.wall_pane, self.chunk
        v = pane.values()
        clicked = pane.clicked
        try:
            which = wall_trace.side_of(ch, clicked, v["side"])
        except TraceRefused as exc:
            self._warn(refusal_text(exc))
            return False
        snap, band, smooth, front, back = v["snap"], v["band"], v["smooth"], v["front"], v["back"]

        def work(p):
            line = smooth_trace(clicked, smooth) if smooth > 0 else clicked
            result = None
            if snap:
                p(0.0, "Snap of the line onto the face of the wall")
                result = wall_trace.snap_trace(ch, line, which, band=band, progress=p.sub(0.0, 0.9))
                if result["snapped"]:
                    try:
                        kept = ordered(result["points"], which)
                        WallPath(kept, radii=corner_radii(kept, front, back))
                    except TraceRefused as exc:  # the line snapped is no trace: the clicked one is used
                        result = dict(result, snapped=False, refused=refusal_text(exc))
            used = result["points"] if result is not None and result["snapped"] else line
            p(0.9, "Heights of the wall")
            try:
                heights = default_heights(ch, used, which, front, back)
            except Exception as exc:  # noqa: BLE001 - the heights are typed by hand then
                log.warning("No default heights for the wall: %s", exc)
                heights = None
            return {"result": result, "heights": heights}

        def done(out):
            if pane.clicked is not clicked:  # traced again meanwhile, or the pane emptied
                return
            r = out["result"]
            pane.running = None
            pane.set_trace(clicked, None if r is None else r["points"], r, out["heights"])
            self._draw_trace_preview()

        pane.running = "snap" if snap else None
        pane.update_states()
        started = self.run_task("Aggancio della linea al muro" if snap else "Quote del muro", work, done,
                                refresh=False, save=False)
        if not started:
            pane.running = None
            pane.update_states()
        return bool(started)

    # ------------------------------------------------------------ the build
    def wall_build(self) -> bool:
        """The elevation of the line of the pane built in the worker, then shown."""
        from ..engine import elevation as E
        from ..engine import wall_trace
        from ..engine.wall_path import TraceRefused

        if not self._wall_pane_ready():
            return False
        pane, ch = self.wall_pane, self.chunk
        v = pane.values()
        if not v["v1"] > v["v0"]:
            self._warn(tr("Le quote del prospetto vanno da una più bassa a una più alta."))
            return False
        try:
            which = wall_trace.side_of(ch, v["clicked"], v["side"])
        except TraceRefused as exc:
            self._warn(refusal_text(exc))
            return False
        points, clicked = v["points"], v["clicked"]
        smooth = v["smooth"] if points is clicked else 0.0
        extend = tuple(v["extend"]) if max(v["extend"]) > 0 else None

        def work(p):
            try:
                frame, _report = wall_trace.path_from_trace(ch, points, which, v["front"], v["back"], snap=False,
                                                            smooth=smooth, clicked=clicked)
            except TraceRefused as exc:
                return {"refused": exc}
            try:
                entry = E.build_elevation(ch, frame, v["resolution"], v["name"] or None, (-v["back"], v["front"]),
                                          v["blending"], bool(v["colour"]), progress=p, extend=extend,
                                          heights=(v["v0"], v["v1"]))
            except ValueError as exc:
                if str(exc).startswith("no surface"):
                    return {"no_surface": exc}
                raise
            return {"entry": entry}

        def done(out):
            pane.running = None
            if "refused" in out:
                pane.update_states()
                self._warn(refusal_text(out["refused"]))
                return
            if "no_surface" in out:
                pane.update_states()
                self._warn(no_surface_text())
                return
            entry = out["entry"]
            pane.built = entry["id"]
            pane.update_states()
            self.ortho_view.remove_layer(TRACE_PREVIEW_LAYER)
            self.show_elevation(entry)

        pane.running = "build"
        pane.update_states()
        started = self.run_task("Prospetto del muro tracciato", work, done, refresh=False)
        if not started:
            pane.running = None
            pane.update_states()
        return bool(started)

    # ------------------------------------------------------------ the lines on the plan
    def _on_plan(self) -> bool:
        view = self.ortho_view
        return self.chunk is not None and view.tr is not None and self.drawings_frame is None and view.kind != "frame"

    def _draw_trace_preview(self) -> None:
        """The line of the pane on the plan while it waits to be built: as clicked (dashed) and as snapped."""
        view = self.ortho_view
        view.remove_layer(TRACE_PREVIEW_LAYER)
        pane = getattr(self, "wall_pane", None)
        if pane is None or pane.clicked is None or pane.built is not None or not self._on_plan():
            return
        if self.chunk.key != pane.chunk_key:
            return
        with view.batch():
            view.add_line(pane.clicked, COLOUR_CLICKED, tr("cliccata"), layer=TRACE_PREVIEW_LAYER,
                          attrs={"linea": "cliccata"}, width=2.0, dash=True)
            if pane.snapped is not None and pane.r_snapped.isEnabled():
                view.add_line(pane.snapped, COLOUR_SNAPPED, tr("agganciata"), layer=TRACE_PREVIEW_LAYER,
                              attrs={"linea": "agganciata"}, width=2.0)

    def _draw_traces(self) -> None:
        """The traces of the elevations of the active chunk on the plan (never on an elevation), and the line of the
        pane."""
        from ..engine import elevation as E
        from ..io.qgis_link import ID_FIELD, stable_id

        view = self.ortho_view
        view.remove_layer(E.TRACES_LAYER)
        if self._on_plan():
            ch = self.chunk
            try:
                features = E.trace_layer(ch)["features"]
            except Exception as exc:  # noqa: BLE001 - the plan is shown without them
                log.warning("The traces of the elevations are not drawn: %s", exc)
                features = []
            with view.batch():
                for f in features:
                    attrs = {k: val for k, val in f.items() if k != "_geom"}
                    attrs[ID_FIELD] = stable_id("trace", ch.key, f["prospetto_id"])
                    view.add_line(f["_geom"][1], COLOUR_TRACES, f["etichetta"], layer=E.TRACES_LAYER, attrs=attrs,
                                  width=2.0)
        self._draw_trace_preview()

    def _trace_double_clicked(self, layer: str, attrs: dict) -> None:
        """A double click on the trace of an elevation on the plan: that elevation shown."""
        from ..engine import elevation as E

        if layer != E.TRACES_LAYER or self.chunk is None:
            return
        eid = attrs.get("prospetto_id")
        entry = next((e for e in self.chunk.meta.get("elevations") or [] if e.get("id") == eid), None)
        if entry is not None:
            self.show_elevation(entry)

    def refusal_text(self, exc) -> str:
        return refusal_text(exc)
