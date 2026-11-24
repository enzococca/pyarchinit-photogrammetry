"""The window's side of a wall traced on the plan (:mod:`..engine.wall_trace`, :class:`.wall_path_pane.WallPathPane`):
the line clicked on the 2D view, snapped onto the face of the model and given its default heights in the worker, the
elevation built on it in the worker; the line as clicked and as snapped drawn on the plan while it waits
(:data:`TRACE_PREVIEW_LAYER`), and the traces of the elevations of the chunk always (``TRACES_LAYER``) - a double
click on one shows its elevation. The line can also be followed between two clicks along the crest of the wall
(:mod:`..engine.wall_auto`), with SAM 2.1 where the DEM is weak, or picked among the walls proposed on the whole plan
(:data:`PROPOSALS_LAYER`)."""
from __future__ import annotations

import logging

import numpy as np

from .i18n import tr
from .wall_path_pane import weak_text

log = logging.getLogger("pyarchinit_photogrammetry")

TRACE_PREVIEW_LAYER = "Traccia del muro (anteprima)"
PROPOSALS_LAYER = "Muri proposti"
MERGE_CLICKS = 0.001  # m: clicks closer than this are one point (a double click, a slip of the hand)
CLOSE_CLICK = 0.05  # m: a last click this near the first closes the line on it (a room traced all round)
THIN_SIDE = 0.6  # the photos on the weaker side weigh this share of the stronger at least: the side is asked
COLOUR_CLICKED = "#ffd000"
COLOUR_SNAPPED = "#00e5ff"
COLOUR_TRACES = "#ff8c00"
COLOUR_PROPOSED = "#ff4fd8"


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


def _along(line, points) -> np.ndarray:
    """How far along the polyline ``line`` (N, 2) the foot of each of ``points`` (M, 2) is, in metres."""
    line = np.asarray(line, float).reshape(-1, 2)
    p = np.asarray(points, float).reshape(-1, 2)
    if len(line) < 2:
        return np.zeros(len(p))
    run = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(line, axis=0).T))])
    best, at = np.full(len(p), np.inf), np.zeros(len(p))
    for i, (a, b) in enumerate(zip(line[:-1], line[1:])):
        step = b - a
        tau = np.clip((p - a) @ step / max(float(step @ step), 1e-300), 0.0, 1.0)
        d = np.hypot(*(p - a - tau[:, None] * step).T)
        nearer = d < best
        best[nearer], at[nearer] = d[nearer], run[i] + tau[nearer] * np.sqrt(float(step @ step))
    return at


def follow_error_text(out: dict) -> str:
    """The words of the window for a line along the wall that could not be followed (the ``error`` of the task)."""
    code = out.get("error")
    if code == "outside":
        return tr("Il muro non si può seguire: il punto {0} è fuori dal DEM. Clicca dentro la pianta.",
                  out.get("point") or "?")
    if code == "points":
        return tr("Il muro si segue fra due clic almeno: clicca sui suoi due capi.")
    if code == "no_path":
        return tr("Nessun percorso fra i punti cliccati sul DEM: clicca sullo stesso muro.")
    if code == "no_ortho":
        return tr("SAM guarda l'ortofoto: costruiscila prima, o togli «Usa SAM dove il muro è basso».")
    if code == "sam_outside":
        return tr("I punti cliccati sono fuori dall'ortofoto: SAM non può ritagliare il muro.")
    return tr("SAM 2.1 non ha potuto ritagliare il muro: {0}", out.get("detail") or "")


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


def reason_text(refused: dict) -> str:
    """The words of the window for a chunk :func:`..engine.elevation_groups.build_on_chunks` refused (its record:
    ``code``, ``reason`` and for ``crs`` the two systems)."""
    code = refused.get("code")
    if code == "already":
        return tr("ha già un prospetto del gruppo")
    if code == "no_model":
        return tr("non ha un modello")
    if code == "local":
        return tr("è in un sistema locale: non è stato detto che è nello stesso sistema")
    if code == "crs":
        return tr("è in {0}, il prospetto in {1}: la stessa griglia vuole lo stesso sistema di riferimento",
                  refused.get("mine") or tr("locale"), refused.get("theirs") or tr("locale"))
    return str(refused.get("reason", ""))


def campaigns_text(out: dict) -> str:
    """The message at the end of *Stesso prospetto su altri chunk...*: the chunks built, refused (with their reasons)
    and empty."""
    lines = []
    if out["built"]:
        names = ", ".join(c.label for c, _e in out["built"])
        lines.append(tr("Costruito su {0} chunk: {1}.", len(out["built"]), names))
    else:
        lines.append(tr("Nessun prospetto costruito."))
    for r in out["refused"]:
        lines.append(tr("«{0}» lasciato fuori: {1}.", r["chunk"], reason_text(r)))
    for label in out["empty"]:
        lines.append(tr("«{0}»: il muro non è nel suo modello (nessuna cella), lasciato fuori.", label))
    return "\n".join(lines)


def _left_out_text(code: str) -> str:
    """Why a campaign is left out of the comparison sheet, in Italian, by the engine's code."""
    if code == "grid":
        return tr("è su un'altra griglia, con un'altra risoluzione")
    if code == "shift":
        return tr("i suoi pixel sono spostati di una frazione di cella")
    return tr("il prospetto o i suoi file non ci sono più")


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
        if ch is None or ch is not pane.chunk:
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

    # ------------------------------------------------------------ the line found by the program
    def _wall_sam_ready(self) -> bool:
        """SAM 2.1 can run (in this Python or in its own environment); nothing is offered here."""
        from ..ai import sam_env

        return sam_env.available() is not None

    def wall_follow(self) -> bool:
        """*Segui il muro fra due clic*: the two ends of the wall (and any via points) clicked on the plan, the line
        found along its crest in the worker (:func:`_wall_follow_run`)."""
        if not self._need_chunk(product="dem"):
            return False
        pane = self.wall_pane
        pane.running = "trace"
        pane.update_states()
        started = self.request_geometry("line", self._wall_follow_drawn, self._wall_trace_cancelled,
                                        tr("Clic sui due capi del muro e, se serve, su punti di passaggio; doppio "
                                           "clic o Invio per finire, Esc annulla"))
        if not started:
            pane.running = None
            pane.update_states()
        return bool(started)

    def _wall_follow_drawn(self, xy) -> bool:
        pane = self.wall_pane
        pane.running = None
        points = clean_clicks(xy)
        if len(points) < 2:
            pane.update_states()
            self._warn(tr("Il muro si segue fra due clic almeno: clicca sui suoi due capi."))
            return False
        return self._wall_follow_run(points)

    def _wall_follow_run(self, clicks) -> bool:
        """The line along the wall between ``clicks`` in one task of the worker - the mask of SAM when ticked, the
        path along the crest (:func:`..engine.wall_auto.follow`), its snap onto the face and its heights - shown
        on the plan as a proposal; a weak one said."""
        from ..core.progress import Cancelled
        from ..engine import wall_auto, wall_trace
        from ..engine.wall_path import TraceRefused, WallPath, corner_radii, ordered

        pane, ch = self.wall_pane, self.chunk
        v = pane.values()
        clicks = np.asarray(clicks, float).reshape(-1, 2)
        use_sam = pane.c_sam.isChecked() and pane.c_sam.isEnabled()
        snap, band, front, back, asked = v["snap"], v["band"], v["front"], v["back"], v["side"]

        def work(p):
            mask = None
            if use_sam:
                p(0.0, "SAM 2.1 on the orthophoto")
                try:
                    mask = wall_auto.sam_mask(ch, self._segmenter(), clicks)
                except Cancelled:
                    raise
                except RuntimeError as exc:
                    if ch.orthomosaic_path is None:
                        return {"error": "no_ortho"}
                    return {"error": "sam", "detail": str(exc)}
                except ValueError:  # the clicks off the orthophoto
                    return {"error": "sam_outside"}
                except Exception as exc:  # noqa: BLE001 - the worker of SAM failed: said, the DEM alone not used
                    log.warning("SAM for the wall failed: %s", exc)
                    return {"error": "sam", "detail": str(exc)}
            p(0.1, "The line along the wall")
            try:
                found = wall_auto.follow(ch, clicks, mask=mask, progress=p.sub(0.1, 0.6))
            except wall_auto.FollowRefused as exc:
                return {"error": exc.reason, "point": exc.point}
            out = {"points": found["points"], "on_ridge": found["on_ridge"], "cell": found["cell"],
                   "clicks": clicks, "snap": None, "heights": None, "refused": None, "which": None}
            out["weak"] = found["on_ridge"] < wall_auto.RIDGE
            line = found["points"]
            try:
                which = wall_trace.side_of(ch, line, asked)
                WallPath(ordered(line, which), radii=corner_radii(ordered(line, which), front, back))
            except TraceRefused as exc:
                out["refused"] = refusal_text(exc)
                return out
            out["which"] = which
            used = line
            if snap and ch.product_path("model"):
                p(0.6, "Snap of the line onto the face of the wall")
                result = wall_trace.snap_trace(ch, line, which, band=band, progress=p.sub(0.6, 0.9))
                if result["snapped"]:
                    try:
                        kept = ordered(result["points"], which)
                        WallPath(kept, radii=corner_radii(kept, front, back))
                        used = result["points"]
                    except TraceRefused as exc:
                        result = dict(result, snapped=False, refused=refusal_text(exc))
                out["snap"] = result
            try:
                out["heights"] = default_heights(ch, used, which, front, back)
            except Exception as exc:  # noqa: BLE001 - the heights are typed by hand then
                log.warning("No default heights for the wall: %s", exc)
            return out

        def done(out):
            pane.running = None
            if self.chunk is not ch:
                pane.update_states()
                return
            if out.get("error"):
                pane.update_states()
                self._warn(follow_error_text(out))
                return
            pane.set_proposal(out)
            self._draw_trace_preview()
            if out["weak"]:
                self._warn(weak_text())

        pane.running = "follow"
        pane.update_states()
        started = self.run_task("Il muro seguito fra i clic", work, done, refresh=False, save=False)
        if not started:
            pane.running = None
            pane.update_states()
        return bool(started)

    def wall_follow_via(self) -> bool:
        """*Aggiungi punti di passaggio*: a point clicked where the wall passes, put among the clicks in its place
        along the line proposed, and the line found again."""
        pane = self.wall_pane
        if pane.proposal is None:
            self._warn(tr("Segui prima il muro fra due clic."))
            return False
        return bool(self.request_geometry("point", self._wall_via_drawn, None,
                                          tr("Clic dove passa il muro; Esc annulla")))

    def _wall_via_drawn(self, xy) -> bool:
        pane = self.wall_pane
        if pane.proposal is None:
            return False
        via = np.asarray(xy, float).reshape(-1, 2)
        clicks = np.asarray(pane.proposal["clicks"], float)
        line = np.asarray(pane.proposal["points"], float)
        along = _along(line, np.vstack([clicks, via]))
        ends = along[[0, len(clicks) - 1]]
        order = np.argsort(np.concatenate([along[:len(clicks)], np.clip(along[len(clicks):], ends[0] + 1e-9,
                                                                         ends[1] - 1e-9)]), kind="stable")
        merged = np.vstack([clicks, via])[order]
        return self._wall_follow_run(clean_clicks(merged))

    def wall_follow_keep(self) -> bool:
        """*Tieni*: the line proposed becomes the line of the wall, with its snap and heights."""
        pane = self.wall_pane
        p = pane.proposal
        if p is None:
            self._warn(tr("Segui prima il muro fra due clic."))
            return False
        if p.get("refused"):
            self._warn(p["refused"])
            return False
        from ..engine.wall_path import TraceRefused

        points = np.asarray(p["points"], float)
        r = p.get("snap")
        try:  # the side asked as for a line clicked by hand: the owner chooses when the photos weigh nearly the same
            which = self._wall_side(points, pane.side.currentData())
        except TraceRefused as exc:
            self._warn(refusal_text(exc))
            return False
        if which is None:
            return False
        if (r is None and pane.c_snap.isChecked()) or which != p.get("which"):  # snapped now, on the side chosen
            if which != p.get("which") and pane.side.currentData() == "auto":
                k = pane.side.findData(which)
                pane.side.blockSignals(True)
                pane.side.setCurrentIndex(k)
                pane.side.blockSignals(False)
            pane.set_trace(points, heights=p.get("heights"))
            self._draw_trace_preview()
            return self.wall_snap()
        pane.set_trace(points, None if r is None else r["points"], r, p.get("heights"))
        self._draw_trace_preview()
        return True

    def wall_proposals(self) -> bool:
        """*Proponi i muri*: the walls of the whole plan (:func:`..engine.wall_auto.proposals`) in the worker, drawn
        on the layer *Muri proposti*; a click on one makes it the line of the wall."""
        from ..engine import wall_auto

        if not self._need_chunk(product="dem"):
            return False
        pane, ch = self.wall_pane, self.chunk

        def work(p):
            return wall_auto.proposals(ch, progress=p)

        def done(found):
            pane.running = None
            if self.chunk is not ch:
                pane.update_states()
                return
            pane.candidates, pane.candidates_chunk = list(found), ch
            pane.update_states()
            self._draw_proposals()
            if not found:
                self._warn(tr("Nessun muro trovato sulla pianta: nessuna cresta più alta di 30 cm e lunga almeno "
                              "3 m. Segui il muro fra due clic o traccialo a mano."))

        pane.running = "proposals"
        pane.update_states()
        started = self.run_task("Muri proposti sulla pianta", work, done, refresh=False, save=False)
        if not started:
            pane.running = None
            pane.update_states()
        return bool(started)

    def _draw_proposals(self) -> None:
        """The walls proposed on the plan of the active chunk (never on an elevation)."""
        from .i18n import number

        view = self.ortho_view
        view.remove_layer(PROPOSALS_LAYER)
        pane = getattr(self, "wall_pane", None)
        if pane is None or not pane.candidates or pane.candidates_chunk is not self.chunk or not self._on_plan():
            return
        from .wall_path_pane import line_length

        with view.batch():
            for i, line in enumerate(pane.candidates):
                view.add_line(line, COLOUR_PROPOSED, tr("muro {0}", i + 1), layer=PROPOSALS_LAYER,
                              attrs={"muro": i + 1, "lunghezza": number(line_length(line), 2)}, width=2.0,
                              dash=True)

    def _proposal_clicked(self, layer: str, attrs: dict) -> None:
        """A click on a wall proposed: it becomes the line of the wall (checked, its side, snapped)."""
        pane = self.wall_pane
        if layer != PROPOSALS_LAYER or pane.candidates_chunk is not self.chunk or pane.running is not None:
            return
        try:
            line = pane.candidates[int(attrs.get("muro")) - 1]
        except (TypeError, ValueError, IndexError):
            return
        self._wall_trace_drawn(np.asarray(line, float))

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
                                          heights=(v["v0"], v["v1"]), skip_backlit=bool(v.get("skip_backlit")))
            except E.NoSurface as exc:
                return {"no_surface": exc}
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

    # ------------------------------------------------------------ the other campaigns
    def wall_campaigns(self, entry=None, chunk=None) -> bool:
        """*Stesso prospetto su altri chunk...*: the elevation ``entry`` of ``chunk`` (default: the one the pane
        built, in its chunk) built again on the other chunks ticked, each with its date, as the group of the wall
        (:func:`..engine.elevation_groups.build_on_chunks`) in one task; at its end the built, the refused with
        their reasons and the empty ones in one message."""
        from ..core import crs as crs_mod
        from ..engine import elevation as E
        from ..engine import elevation_groups as G
        from . import dialogs
        from .dialogs import Field

        if entry is None:
            pane = getattr(self, "wall_pane", None)
            entry = pane.built_entry() if pane is not None else None
            chunk = pane.chunk if entry is not None else None
        chunk = chunk if chunk is not None else self.chunk
        if entry is None or chunk is None:
            self._warn(tr("Costruisci prima il prospetto del muro: poi lo stesso prospetto va sugli altri chunk."))
            return False
        others = [c for c in self.doc.chunks if c is not chunk and c.product_path("model")
                  and c.product_path("model").exists()]
        if not others:
            self._warn(tr("Nessun altro chunk del progetto ha un modello: lo stesso prospetto non ha dove andare."))
            return False
        mine = entry.get("crs") or chunk.crs
        found = G.group_of(self.doc, chunk, entry["id"])
        live = {}  # chunk key -> the record of a member whose elevation is still there
        if found is not None:
            for m in G.members(self.doc, found[0], found[1]):
                if m["chunk"] is not None and m["entry"] is not None:
                    live[m["chunk"].key] = m["record"]
        ticked = [c.key for c in others if E.same_crs(mine, c.crs) and c.key not in live]
        fields = [Field("chunks", tr("Chunk (altre campagne)"), ticked,
                        choices=[(c.key, f"{c.label}  ({c.crs or tr('locale')})") for c in others],
                        kind="checklist")]
        dated = [chunk, *others]
        fields += [Field(f"date_{c.key}", tr("Data di «{0}» (AAAA-MM-GG)", c.label),
                         (live[c.key].get("date") or "") if c.key in live else (G.campaign_date(c) or ""),
                         kind="text") for c in dated]
        v = dialogs.ParamDialog.ask(self, tr("Stesso prospetto su altri chunk"), fields, intro=tr(
            "«{0}» costruito di nuovo, sulla stessa griglia, sui chunk spuntati: una campagna ciascuno, tenute "
            "insieme come gruppo del muro. I chunk devono essere già allineati insieme e nello stesso sistema di "
            "riferimento.", entry.get("name")))
        if v is None:
            return False
        targets = [c for c in others if c.key in (v.get("chunks") or [])]
        dates = {}
        for c in dated:
            text = str(v.get(f"date_{c.key}") or "").strip()
            try:
                dates[c.key] = G.parse_date(text)
            except ValueError:
                self._warn(tr("La data di «{0}» si scrive AAAA-MM-GG, non «{1}».", c.label, text))
                return False
        changed = False
        for key, record in live.items():  # the dates of the members edited in place
            if key in dates and dates[key] != record.get("date"):
                G.set_date(found[1], key, record["entry"], dates[key])
                changed = True
        if changed:
            self._dirty(True)
            self.workspace.populate(self.doc)
            if getattr(self, "wall_pane", None) is not None:
                self.wall_pane.update_states()
        if not targets:
            return False
        dates = {k: d for k, d in dates.items() if d is not None and k not in live}
        same_frame = False
        if crs_mod.is_local(mine) and any(crs_mod.is_local(c.crs) for c in targets):
            k = dialogs.choice(self, "question", tr("Stesso prospetto su altri chunk"), tr(
                "I chunk sono in un sistema locale: le loro coordinate coincidono solo se sono stati allineati, uniti "
                "o orientati sugli stessi marker. Lo sono?"), [tr("Sì, sono nello stesso sistema")])
            if k != 0:
                return False
            same_frame = True
        doc = self.doc
        pane = getattr(self, "wall_pane", None)

        def work(p):
            return G.build_on_chunks(doc, chunk, entry, targets, same_frame=same_frame, dates=dates, progress=p)

        def done(out):
            if pane is not None:
                pane.running = None
                pane.update_states()
            self._info(tr("Stesso prospetto su altri chunk"), campaigns_text(out))

        if pane is not None and pane.built == entry.get("id"):
            pane.running = "campaigns"
            pane.update_states()
        started = self.run_task("Stesso prospetto su altri chunk", work, done)
        if not started and pane is not None:
            pane.running = None
            pane.update_states()
        return bool(started)

    def wall_sheet(self, entry=None, chunk=None) -> bool:
        """*Scheda di confronto...*: the comparison sheet of the group of the elevation ``entry`` of ``chunk``
        (default: the one the pane built) - the campaigns stacked on one ``u`` axis
        (:func:`..engine.elevation_sheet.comparison_sheet`) - written where the save dialog says, a PDF beside it,
        in one task; the two paths said at its end."""
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        from ..engine import elevation_groups as G
        from ..engine.elevation_sheet import comparison_sheet
        from . import dialogs

        if entry is None:
            pane = getattr(self, "wall_pane", None)
            entry = pane.built_entry() if pane is not None else None
            chunk = pane.chunk if entry is not None else None
        chunk = chunk if chunk is not None else self.chunk
        if entry is None or chunk is None:
            self._warn(tr("Costruisci prima il prospetto del muro e le altre campagne: poi la scheda le confronta."))
            return False
        found = G.group_of(self.doc, chunk, entry["id"])
        if found is None:
            self._warn(tr("«{0}» non è in un gruppo di campagne: costruisci prima lo stesso prospetto su altri chunk.",
                          entry.get("name")))
            return False
        ref, group = found
        folder = Path(self.doc.path).parent if getattr(self.doc, "path", None) else Path.home()
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(group.get("name") or "muro"))
        path, _f = QFileDialog.getSaveFileName(self, tr("Scheda di confronto"), str(folder / f"confronto_{safe}.png"),
                                               "PNG (*.png);;JPEG (*.jpg *.jpeg)")
        if not path:
            return False
        out = Path(path)
        if out.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            out = out.with_name(out.name + ".png")
        pdf = out.with_suffix(".pdf")  # the dialog confirmed the image; the PDF beside it is asked about here
        if pdf.exists() and dialogs.choice(self, "question", tr("Scheda di confronto"), tr(
                "Accanto all'immagine c'è già {0}: sovrascriverlo?", pdf.name), [tr("Sovrascrivi")]) != 0:
            return False
        doc = self.doc

        def work(p):
            return comparison_sheet(doc, ref, group, out, progress=p)

        def done(r):
            text = tr("Scheda di confronto di {0} campagne scritta:\n{1}\n{2}", len(r["bands"]), r["image"], r["pdf"])
            if r["skipped"]:
                text += "\n" + tr("Lasciate fuori: {0}", "; ".join(
                    f"{label} ({_left_out_text(r['codes'].get(label, ''))})" for label in r["skipped"]))
            self._info(tr("Scheda di confronto"), text)

        return bool(self.run_task("Scheda di confronto", work, done, refresh=False, save=False))

    def wall_finish(self, entry=None, chunk=None) -> bool:
        """*Esporta per la pubblicazione...*: the elevation ``entry`` of ``chunk`` (default: the one the pane built)
        finished for a publication - holes filled, outside the wall masked, colour levelled, survey targets painted
        over, as the dialog says (:func:`..engine.elevation_finish.finish`) - and written where the save dialog
        says, a JPEG or a TIFF, in one task. An export: the elevation stays as measured, nothing is saved in the
        project."""
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        from ..engine import elevation_finish as F
        from . import dialogs

        if entry is None:
            pane = getattr(self, "wall_pane", None)
            entry = pane.built_entry() if pane is not None else None
            chunk = pane.chunk if entry is not None else None
        chunk = chunk if chunk is not None else self.chunk
        if entry is None or chunk is None:
            self._warn(tr("Costruisci prima il prospetto: poi si può esportare per la pubblicazione."))
            return False
        v = dialogs.ParamDialog.ask(self, tr("Esporta per la pubblicazione"), [
            dialogs.Field("fill", tr("Riempi i buchi (colore e trama dai bordi)"), True),
            dialogs.Field("mask", tr("Fuori dal muro bianco (trasparente in un TIFF)"), True),
            dialogs.Field("level", tr("Uniforma il colore (luce che cambia lungo il muro)"), True,
                          tip=tr("Toglie i cambi lenti di luce lungo il muro, e con loro anche la differenza vera di "
                                 "ombra fra parti del muro esposte diversamente")),
            dialogs.Field("max_hole", tr("Buco più grande da riempire (m)"), F.MAX_HOLE, minimum=0.0, maximum=100.0,
                          decimals=2, tip=tr("I buchi fino a questa misura vengono sempre riempiti; uno più grande "
                                             "solo se il rilievo vi trova superficie, altrimenti è un'apertura "
                                             "(una porta, una finestra): resta aperto, mai muro finto")),
            dialogs.Field("targets", tr("Copri le mire (i marker sul prospetto, 18 cm intorno)"), True),
            dialogs.Field("reduce", tr("Un pixel ogni"), 1, minimum=1, maximum=64,
                          tip=tr("1: la piena risoluzione; 2: metà dei pixel per lato, e così via"))],
            intro=tr("Un'immagine del prospetto «{0}» da pubblicare. Il prospetto misurato (ortofoto e rilievo) "
                     "resta com'è.", entry.get("name")))
        if not v:
            return False
        reduce = max(1, int(v.get("reduce") or 1))
        w, h = (int(n) for n in entry.get("size") or (0, 0))
        if -(-w // reduce) * -(-h // reduce) > F.MAX_PIXELS:
            need = reduce
            while -(-w // need) * -(-h // need) > F.MAX_PIXELS:
                need += 1
            self._warn(tr("Il prospetto è troppo grande per un'immagine sola ({0} x {1} pixel): scegli un pixel ogni "
                          "{2} o più.", w, h, need))
            return False
        folder = Path(self.doc.path).parent if getattr(self.doc, "path", None) else Path.home()
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(entry.get("name") or "prospetto"))
        path, chosen = QFileDialog.getSaveFileName(self, tr("Esporta per la pubblicazione"),
                                                   str(folder / f"{safe}_pubblicazione.jpg"),
                                                   "JPEG (*.jpg *.jpeg);;GeoTIFF (*.tif *.tiff)")
        if not path:
            return False
        out = Path(path)
        if out.suffix.lower() not in (".jpg", ".jpeg", ".tif", ".tiff"):
            out = out.with_name(out.name + (".tif" if "tif" in (chosen or "").lower() else ".jpg"))
        if F.inside_folder(out, chunk.data_dir):
            self._warn(tr("Scegli una cartella fuori dai dati del progetto: lì resta il prospetto misurato."))
            return False

        def work(p):
            return F.finish(chunk, entry, out, fill=bool(v.get("fill", True)), mask=bool(v.get("mask", True)),
                            level=bool(v.get("level", True)), targets=bool(v.get("targets", True)), reduce=reduce,
                            max_hole=float(v.get("max_hole", F.MAX_HOLE)), progress=p)

        def done(r):
            self._info(tr("Esporta per la pubblicazione"), tr(
                "Prospetto per la pubblicazione scritto:\n{0}\n{1} x {2} pixel; {3} celle riempite, {4} mire coperte",
                r["path"], r["size"][0], r["size"][1], r["filled"], r["targets"])
                + "\n" + tr("{0} aperture lasciate aperte", r.get("openings", 0)))

        return bool(self.run_task("Esporta per la pubblicazione", work, done, refresh=False, save=False))

    def _composite_group(self, entry, chunk):
        """``(reference chunk, group)`` of the elevation ``entry`` of ``chunk``: the group it is a campaign of, or the
        group a composite was made of; None for none."""
        from ..engine import elevation_groups as G

        if entry.get("kind") == "composite":
            group = next((g for g in G.groups(chunk) if g.get("id") == entry.get("group")), None)
            return (chunk, group) if group is not None else None
        return G.group_of(self.doc, chunk, entry["id"])

    def wall_composite(self, entry=None, chunk=None, ask: bool = True) -> bool:
        """*Composito delle campagne...* (``ask``: the threshold, the name and the colours asked) and *Rifai il
        composito* (not asked: as it was made): the composite of the campaigns of the group of the elevation
        ``entry`` of ``chunk`` (a campaign, or the composite itself; default: the one the pane built) -
        :func:`..engine.elevation_composite.composite` - in one task, written in the chunk the wall was traced on
        and shown at its end."""
        from ..engine import elevation as E
        from ..engine import elevation_composite as C
        from . import dialogs
        from .dialogs import Field

        if entry is None:
            pane = getattr(self, "wall_pane", None)
            entry = pane.built_entry() if pane is not None else None
            chunk = pane.chunk if entry is not None else None
        chunk = chunk if chunk is not None else self.chunk
        if entry is None or chunk is None:
            self._warn(tr("Costruisci prima il prospetto del muro e le altre campagne: poi il composito le unisce."))
            return False
        found = self._composite_group(entry, chunk)
        if found is None:
            self._warn(tr("«{0}» non è in un gruppo di campagne: costruisci prima lo stesso prospetto su altri chunk.",
                          entry.get("name")))
            return False
        ref, group = found
        missing = C.undated(self.doc, ref, group)
        if missing:  # the order of the campaigns decides the surface and the colours: never guessed
            self._warn(tr(C.UNDATED_IT, ", ".join(missing)))
            return False
        record = group.get("composite") or {}
        old = next((e for e in E.elevations(ref) if e.get("id") == record.get("entry")), None)
        threshold = float(record.get("threshold", C.DEEPER))
        match = bool(old.get("colours_matched", True)) if old is not None else True
        name = None
        if ask:
            v = dialogs.ParamDialog.ask(self, tr("Composito delle campagne"), [
                Field("threshold", tr("Una campagna più vecchia prende la cella se è più profonda di (m)"), threshold,
                      minimum=0.0, maximum=10.0, decimals=3),
                Field("name", tr("Nome"), old.get("name") if old is not None else "", kind="text"),
                Field("match", tr("Accorda i colori delle campagne a quelli della più recente"), match)],
                intro=tr("Per ogni cella la superficie della campagna più recente, sostituita da quella di una più "
                         "vecchia dove è più profonda della soglia o dove la recente non ha nulla: il muro com'era "
                         "quando era più scoperto. Il composito va nel chunk «{0}»{1}.", ref.label,
                         tr(" al posto di quello fatto prima") if old is not None else ""))
            if v is None:
                return False
            threshold, match = float(v.get("threshold", threshold)), bool(v.get("match", match))
            name = str(v.get("name") or "").strip() or None
        elif old is None:
            self._warn(tr("Il composito di questo gruppo non c'è ancora: fallo con «Composito delle campagne...»."))
            return False
        doc = self.doc
        pane = getattr(self, "wall_pane", None)

        def work(p):
            return C.composite(doc, ref, group, threshold=threshold, name=name, match=match, progress=p)

        def done(made):
            if pane is not None:
                pane.running = None
                pane.update_states()
            if self._activate_chunk(ref):
                self.show_elevation(made)

        if pane is not None:
            pane.running = "composite"
            pane.update_states()
        started = self.run_task("Composito delle campagne", work, done)
        if not started and pane is not None:
            pane.running = None
            pane.update_states()
        return bool(started)

    def composite_stale(self, chunk, entry) -> bool:
        """The composite ``entry`` of ``chunk`` is to be made again (:func:`..engine.elevation_composite.is_stale`);
        False for any other elevation."""
        from ..engine import elevation_composite as C

        if entry is None or entry.get("kind") != "composite" or chunk is None:
            return False
        try:
            return C.is_stale(self.doc, chunk, entry)
        except Exception as exc:  # noqa: BLE001 - a notice: never in the way of showing it
            log.warning("Stato del composito non letto: %s", exc)
            return False

    # ------------------------------------------------------------ the lines on the plan
    def _on_plan(self) -> bool:
        view = self.ortho_view
        return self.chunk is not None and view.tr is not None and self.drawings_frame is None and view.kind != "frame"

    def _draw_trace_preview(self) -> None:
        """The line of the pane on the plan while it waits to be built: as clicked (dashed) and as snapped."""
        view = self.ortho_view
        view.remove_layer(TRACE_PREVIEW_LAYER)
        pane = getattr(self, "wall_pane", None)
        if pane is not None and pane.proposal is not None and self._on_plan():
            p = pane.proposal
            if p.get("chunk") is self.chunk:
                with view.batch():
                    view.add_line(p["points"], COLOUR_PROPOSED, tr("proposta"), layer=TRACE_PREVIEW_LAYER,
                                  attrs={"linea": "proposta"}, width=2.0)
                    r = p.get("snap")
                    if r is not None and r.get("snapped"):
                        view.add_line(np.asarray(r["points"])[::-1], COLOUR_SNAPPED, tr("agganciata"),
                                      layer=TRACE_PREVIEW_LAYER, attrs={"linea": "agganciata"}, width=2.0)
                return
        if pane is None or pane.clicked is None or pane.built is not None or not self._on_plan():
            return
        if self.chunk is not pane.chunk:
            return
        with view.batch():
            view.add_line(pane.clicked, COLOUR_CLICKED, tr("cliccata"), layer=TRACE_PREVIEW_LAYER,
                          attrs={"linea": "cliccata"}, width=2.0, dash=True)
            if pane.snapped is not None and pane.r_snapped.isEnabled():  # its name at the other end
                view.add_line(pane.snapped[::-1], COLOUR_SNAPPED, tr("agganciata"), layer=TRACE_PREVIEW_LAYER,
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
        self._draw_proposals()
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
