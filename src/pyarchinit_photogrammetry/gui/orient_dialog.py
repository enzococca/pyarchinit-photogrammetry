"""*Flusso di lavoro > Orienta il sistema locale*: the frame of a chunk in a local system set from three points (A
origin, B on the x axis, C on the horizontal plane) or from a plane fitted to an area of the model, the points picked
in the 3D view, clicked on the orthophoto (the height from the DEM) or taken from the markers; the new axes previewed in
the 3D view and the motion said in words at every change; *Applica* hands the arguments of
:func:`..engine.orient.orient` to the window (:meth:`MainWindow._orient_apply`). Not modal: the views stay usable while
it is open. :class:`OrientStep` is the orientation as one step of *Modifica > Annulla*."""
from __future__ import annotations

import copy
import logging

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QRadioButton, QVBoxLayout)

from ..engine import orient
from .i18n import decimal_comma, number, tr

log = logging.getLogger(__name__)

ROWS = ("A", "B", "C")
PURPOSES = {"A": "orient_p", "B": "orient_p2", "C": "orient_p3"}  # the picks of ModelView, one per row
MAX_NAMES = 8  # names listed in a refusal, then "e altri N"
STEEP = 60.0  # a tilt beyond this is said: the points are worth a look


def refusal_texts() -> dict[str, str]:
    """The reasons of :class:`..engine.orient.OrientRefused` in the window's words."""
    return {
        "crs": tr("Il chunk è in un sistema di coordinate: l'orientamento lo danno i marker con coordinate"),
        "markers": tr("L'orientamento lo danno i marker di controllo con coordinate: cambia le loro coordinate"),
        "aligned_to": tr("Orienta il chunk di riferimento: quelli allineati lo seguono"),
        "cameras": tr("Le posizioni delle foto danno l'orientamento: togli i loro riferimenti o disattivali"),
        "scalebars": tr("Le barre di scala di controllo danno già la scala: cambia le loro distanze"),
        "points": tr("Mancano i punti del modello per l'orientamento"),
        "nan": tr("Un punto non ha coordinate valide"),
        "collinear": tr("I punti sono allineati: non danno un piano"),
        "plane_points": tr("Troppo pochi punti, o un'area troppo stretta, per un piano"),
        "plane_rough": tr("I punti dell'area non stanno su un piano"),
    }


def refusal_text(exc: orient.OrientRefused) -> str:
    """The refusal ``exc`` said in Italian (the engine's English message when its reason has no text of the window),
    with the names it lists (at most :data:`MAX_NAMES`, then how many more)."""
    text = refusal_texts().get(exc.reason) or tr("Orientamento non possibile: {0}", str(exc))
    names = [str(n) for n in exc.names]
    if names:
        shown = ", ".join(names[:MAX_NAMES])
        if len(names) > MAX_NAMES:
            shown += " " + tr("e altri {0}", len(names) - MAX_NAMES)
        text += "\n" + shown
    return text


def _coords(p) -> str:
    return "-" if p is None else decimal_comma(" ".join(f"{v:.3f}" for v in p))


class OrientDialog(QDialog):
    """The dialog of the orientation of ``chunk``: :meth:`ask` shows it (patchable in tests); ``apply_requested``
    gives the keyword arguments of :func:`..engine.orient.orient`."""

    apply_requested = Signal(dict)

    @classmethod
    def ask(cls, window, chunk) -> OrientDialog:
        dlg = cls(window, chunk)
        dlg.show()
        dlg.raise_()
        return dlg

    def __init__(self, window, chunk):
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setModal(False)
        self.setWindowTitle(tr("Orienta il sistema locale"))
        self.window_, self.chunk = window, chunk
        self.model_view, self.ortho_view = window.model_view, window.ortho_view
        self.plane_xyz: np.ndarray | None = None
        self.plane_nmad: float | None = None
        self._pending_row: str | None = None
        self._lasso_waiting = False
        self.rows: dict[str, dict] = {}
        lay = QVBoxLayout(self)
        intro = QLabel(tr("Il chunk è in coordinate locali: scegli dove vanno l'origine, l'asse x e il piano "
                          "orizzontale. Tutto quello che è nelle sue coordinate lo segue; DEM e ortofoto vanno "
                          "ricostruiti."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        # the source
        src = QHBoxLayout()
        self.points_radio = QRadioButton(tr("Tre punti"))
        self.plane_radio = QRadioButton(tr("Piano adattato"))
        self.points_radio.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.points_radio)
        group.addButton(self.plane_radio)
        src.addWidget(self.points_radio)
        src.addWidget(self.plane_radio)
        src.addStretch(1)
        lay.addLayout(src)
        # the rows A, B, C
        grid = QGridLayout()
        names = {"A": tr("A (origine)"), "B": tr("B (asse x)"), "C": tr("C (piano orizzontale)")}
        markers = [m for m in chunk.markers if m.position is not None]
        for r, label in enumerate(ROWS):
            coords = QLabel("-")
            coords.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            b3d = QPushButton(tr("Nel 3D"))
            b2d = QPushButton(tr("Sull'ortofoto"))
            combo = QComboBox()
            combo.addItem("-", None)
            for m in markers:
                combo.addItem(m.label, m.key)
            combo.setToolTip(tr("Un marker del chunk come punto"))
            b3d.clicked.connect(lambda _=False, lab=label: self._pick_3d(lab))
            b2d.clicked.connect(lambda _=False, lab=label: self._pick_raster(lab))
            combo.currentIndexChanged.connect(lambda _i, lab=label: self._marker_chosen(lab))
            grid.addWidget(QLabel(names[label]), r, 0)
            grid.addWidget(coords, r, 1)
            grid.addWidget(b3d, r, 2)
            grid.addWidget(b2d, r, 3)
            grid.addWidget(combo, r, 4)
            self.rows[label] = {"coords": coords, "b3d": b3d, "b2d": b2d, "combo": combo, "xyz": None, "marker": None}
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        # the fitted plane
        plane = QHBoxLayout()
        self.b_lasso = QPushButton(tr("Seleziona l'area nel 3D (lazo)"))
        self.b_area = QPushButton(tr("Disegna l'area sull'ortofoto"))
        self.plane_label = QLabel(tr("Nessuna area"))
        self.b_lasso.clicked.connect(self._lasso)
        self.b_area.clicked.connect(self._area)
        plane.addWidget(self.b_lasso)
        plane.addWidget(self.b_area)
        plane.addWidget(self.plane_label, 1)
        lay.addLayout(plane)
        # the options
        opts = QGridLayout()
        self.x_along = QCheckBox(tr("x lungo A→B"))
        self.x_along.setChecked(True)
        self.x_along.setToolTip(tr("Tolta la spunta, l'asse x resta nella direzione di quello di adesso"))
        self.flip = QCheckBox(tr("Capovolgi"))
        self.flip.setToolTip(tr("L'asse z dall'altra parte del piano"))
        self.quota = self._spin(-1e6, 1e6, 3)
        self.origin_x = self._spin(-1e9, 1e9, 3)
        self.origin_y = self._spin(-1e9, 1e9, 3)
        self.distance_check = QCheckBox(tr("Distanza vera A-B"))
        self.distance = self._spin(0.0, 1e6, 4)
        self.distance.setValue(1.0)
        self.distance.setEnabled(False)
        from ..markers import gcp

        if gcp.control_scalebars(chunk):
            tip = tr("Le barre di scala di controllo danno già la scala")
            for w in (self.distance_check, self.distance):
                w.setEnabled(False)
                w.setToolTip(tip)
        opts.addWidget(self.x_along, 0, 0)
        opts.addWidget(self.flip, 0, 1)
        opts.addWidget(QLabel(tr("Quota di A (m)")), 1, 0)
        opts.addWidget(self.quota, 1, 1)
        opts.addWidget(QLabel(tr("X, Y di A (m)")), 2, 0)
        xy = QHBoxLayout()
        xy.addWidget(self.origin_x)
        xy.addWidget(self.origin_y)
        opts.addLayout(xy, 2, 1)
        opts.addWidget(self.distance_check, 3, 0)
        opts.addWidget(self.distance, 3, 1)
        lay.addLayout(opts)
        self.aligned = orient.aligned_to(chunk)
        self.move_aligned = QCheckBox(tr("Sposta anche i chunk allineati"))
        self.move_aligned.setChecked(True)
        lay.addWidget(self.move_aligned)
        self.aligned_label = QLabel(", ".join(c.label for c in self.aligned) if self.aligned
                                    else tr("Nessun chunk allineato a questo"))
        self.aligned_label.setWordWrap(True)
        lay.addWidget(self.aligned_label)
        self.move_aligned.setEnabled(bool(self.aligned))
        # what the orientation does
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.summary)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.b_apply = QPushButton(tr("Applica"))
        self.b_apply.setDefault(True)
        self.b_close = QPushButton(tr("Chiudi"))
        self.b_apply.clicked.connect(self._apply)
        self.b_close.clicked.connect(self.reject)
        buttons.addWidget(self.b_apply)
        buttons.addWidget(self.b_close)
        lay.addLayout(buttons)
        for w in (self.points_radio, self.plane_radio, self.x_along, self.flip, self.distance_check):
            w.toggled.connect(lambda _v: self._update())
        for w in (self.quota, self.origin_x, self.origin_y, self.distance):
            w.valueChanged.connect(lambda _v: self._update())
        self.move_aligned.toggled.connect(lambda _v: self._update())
        self.model_view.orient_requested.connect(self._picked)
        self.ortho_view.orient_clicked.connect(self._raster_clicked)
        self._connected = True
        self.resize(760, 520)
        self._update()

    @staticmethod
    def _spin(lo: float, hi: float, decimals: int) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(decimals)
        s.setKeyboardTracking(False)
        return s

    # ------------------------------------------------------------------ the rows
    def point(self, label: str) -> np.ndarray | None:
        """The point of row ``label`` in the coordinates of the chunk, None when empty."""
        xyz = self.rows[label]["xyz"]
        return None if xyz is None else np.array(xyz, float)

    def set_point(self, label: str, xyz, marker=None) -> None:
        row = self.rows[label]
        row["xyz"] = None if xyz is None else np.asarray(xyz, float).reshape(3).copy()
        row["marker"] = marker
        row["coords"].setText(_coords(row["xyz"]))
        if marker is None and row["combo"].currentIndex() != 0:
            row["combo"].blockSignals(True)
            row["combo"].setCurrentIndex(0)
            row["combo"].blockSignals(False)
        self._update()

    def _marker_chosen(self, label: str) -> None:
        key = self.rows[label]["combo"].currentData()
        if key is None:
            self.set_point(label, None)
            return
        try:
            m = self.chunk.marker_by_key(key)
        except KeyError:  # removed meanwhile
            m = None
        if m is None or m.position is None:
            self.status.setText(tr("Il marker non è più collocato"))
            return
        self.set_point(label, self.chunk.to_world(np.asarray(m.position, float)[None])[0], marker=key)

    def _pick_3d(self, label: str) -> None:
        self.window_.show_view(self.model_view)
        self.status.setText(tr("Clic sul punto {0} nella vista 3D", label))
        self.model_view.start_pick(PURPOSES[label])

    def _picked(self, kind: str, payload) -> None:
        if payload is None:
            return
        if kind == "points":
            self._points_selected(*payload)
            return
        if kind == "faces":
            self._faces_selected(*payload)
            return
        if kind != "point":
            return
        label, internal = payload
        if label not in self.rows or internal is None:
            return
        self.status.setText("")
        self.set_point(label, self.chunk.to_world(np.asarray(internal, float).reshape(1, 3))[0])

    def _pick_raster(self, label: str) -> None:
        view = self.ortho_view
        if view.tr is None:
            return
        self._pending_row = label
        for action in self.window_.draw_group.actions():  # no tool of the bar is the one drawing now
            action.setChecked(False)
        view.set_mode("orient_pick")
        self.window_.show_view(view)
        self.status.setText(tr("Clic sul punto {0} sull'ortofoto: la quota dal DEM", label))

    def _end_raster_pick(self) -> None:
        self._pending_row = None
        if self.ortho_view.mode == "orient_pick":
            self.ortho_view.set_mode("pan")
            pan = getattr(self.window_, "draw_actions", {}).get("pan")
            if pan is not None:
                pan.setChecked(True)

    def _raster_clicked(self, x: float, y: float) -> None:
        label = self._pending_row
        if label is None:
            return
        self._end_raster_pick()
        from ..archaeo.measures import ground_dem_at

        z = None
        path = self.chunk.dem_path
        if path is not None and path.exists():
            try:
                z = ground_dem_at(path, x, y)
            except Exception as exc:  # noqa: BLE001 - an unreadable DEM: said, the row stays
                log.warning("DEM non leggibile: %s", exc)
        if z is None or not np.isfinite(z):
            self.status.setText(tr("Nessun DEM in quel punto"))
            return
        self.status.setText("")
        self.set_point(label, [x, y, z])

    # ------------------------------------------------------------------ the fitted plane
    def _lasso(self) -> None:
        self.plane_radio.setChecked(True)
        self.window_.show_view(self.model_view)
        self._lasso_waiting = True
        self.status.setText(tr("Disegna l'area del piano nella vista 3D"))
        self.model_view.start_lasso("orient")

    def _points_selected(self, visible, inside) -> None:
        if not self._lasso_waiting:
            return
        self._lasso_waiting = False
        pc = self.chunk.point_cloud
        if pc is None:
            return
        mask = visible if int(np.count_nonzero(visible)) >= orient.MIN_PLANE_POINTS else inside
        self._set_plane(self.chunk.to_world(np.asarray(pc.xyz, float)[np.asarray(mask, bool)]))

    def _faces_selected(self, visible, inside) -> None:
        if not self._lasso_waiting:
            return
        self._lasso_waiting = False
        mesh = self.chunk.model
        if mesh is None:
            return
        mask = visible if int(np.count_nonzero(visible)) else inside
        faces = np.asarray(mesh.faces, np.int64)[np.asarray(mask, bool)]
        vertices = np.asarray(mesh.vertices, float)[np.unique(faces)]
        self._set_plane(self.chunk.to_world(vertices))

    def _area(self) -> None:
        self.plane_radio.setChecked(True)

        def drawn(xy) -> None:
            import shapely

            try:
                xyz = orient.points_in_plan(self.chunk, shapely.Polygon(np.asarray(xy, float)))
            except orient.OrientRefused as exc:
                self.status.setText(refusal_text(exc))
                return
            self._set_plane(xyz)

        self.window_.request_geometry("polygon", drawn, hint=tr("Disegna l'area del piano: doppio clic o Invio "
                                                                "chiude, Esc annulla"))

    def _set_plane(self, xyz) -> None:
        self.plane_xyz = np.asarray(xyz, float).reshape(-1, 3)
        self.plane_nmad = orient.fit_plane(self.plane_xyz)[2] if len(self.plane_xyz) >= 3 else None
        self.status.setText("")
        self._update()

    # ------------------------------------------------------------------ the motion
    def _toward_cameras(self) -> np.ndarray:
        """The side the photos see the model from (above the points when no photo is aligned)."""
        xyz = self.plane_xyz
        if xyz is None:
            given = [p for p in (self.point(k) for k in ROWS) if p is not None]
            xyz = np.array(given, float) if given else np.zeros((1, 3))
        return np.asarray(orient.toward_cameras(self.chunk, xyz), float)

    def _frame(self):
        """``(frame, missing text or None)`` of what the dialog holds; raises :class:`OrientRefused`."""
        a, b, c = (self.point(k) for k in ROWS)
        if self.plane_radio.isChecked():
            if self.plane_xyz is None:
                return None, tr("Scegli l'area del piano: lazo nel 3D o area sull'ortofoto")
            ends = (a, b) if a is not None and b is not None else (None, None)
            frame = orient.frame_from_plane(self.plane_xyz, self._toward_cameras(), a=ends[0], b=ends[1],
                                            flip=self.flip.isChecked())
            return frame, None
        if a is None or b is None:
            return None, tr("Mancano i punti: A e B servono, C è facoltativo (senza C il piano resta orizzontale)")
        return orient.frame_from_points(a, b, c, x_along_ab=self.x_along.isChecked(), flip=self.flip.isChecked()), None

    def _distance(self) -> float | None:
        if self.distance_check.isEnabled() and self.distance_check.isChecked() and self.distance.value() > 0:
            return float(self.distance.value())
        return None

    def _update(self) -> None:
        self.distance.setEnabled(self.distance_check.isEnabled() and self.distance_check.isChecked())
        plane = self.plane_radio.isChecked()
        self.b_lasso.setEnabled(plane and self.model_view.chunk is not None
                                and self.model_view.kind in ("point_cloud", "model"))
        self.b_area.setEnabled(plane and self.ortho_view.tr is not None)
        self.x_along.setEnabled(not plane)
        pickable = self.model_view.chunk is not None and self.model_view.kind in self.model_view.PICK_KINDS
        for row in self.rows.values():
            row["b3d"].setEnabled(pickable)
            row["b2d"].setEnabled(self.ortho_view.tr is not None)
        if self.plane_xyz is None:
            self.plane_label.setText(tr("Nessuna area"))
        elif self.plane_nmad is not None:
            self.plane_label.setText(tr("{0} punti, scarto dal piano (NMAD) {1} mm", len(self.plane_xyz),
                                        number(self.plane_nmad * 1000, 1)))
        lines, ok = [], False
        try:
            frame, missing = self._frame()
            if frame is None:
                lines.append(missing)
            else:
                distance = self._distance()
                a, b = self.point("A"), self.point("B")
                scale = 1.0
                if distance is not None:
                    if a is None or b is None:
                        raise orient.OrientRefused("points", "the distance is the one between A and B")
                    scale = orient.scale_for(a, b, distance)
                m = orient.motion(frame, quota=self.quota.value(), origin_xy=(self.origin_x.value(),
                                                                               self.origin_y.value()), scale=scale)
                d = orient.describe(m)
                t = d["translation"]
                lines.append(tr("Inclinazione {0}° verso {1}°, direzione {2}°; traslazione {3}, {4}, {5} m; scala {6}",
                                number(d["tilt_deg"], 1), number(d["tilt_direction_deg"], 1),
                                number(d["heading_deg"], 1), number(t[0], 3), number(t[1], 3), number(t[2], 3),
                                number(d["scale"], 6)))
                if d["tilt_deg"] > STEEP:
                    lines.append(tr("Inclinazione oltre 60°: controlla i punti"))
                toward = self._toward_cameras()
                if (np.asarray(toward, float) - frame.origin) @ frame.axes[2] < 0:
                    lines.append(tr("L'asse z guarda dalla parte opposta alle foto: valuta Capovolgi"))
                if frame.note == "B ignored without A: x kept":
                    lines.append(tr("B senza A: l'asse x resta com'è"))
                elif frame.note:
                    lines.append(tr("A→B è perpendicolare al piano: l'asse x resta com'è"))
                self.model_view.show_axes(frame.origin, frame.axes, self.model_view.axes_size())
                ok = True
        except orient.OrientRefused as exc:
            lines.append(refusal_text(exc))
        except ValueError as exc:
            lines.append(tr("Orientamento non possibile: {0}", str(exc)))
        if not ok:
            self.model_view.hide_axes()
        if self.aligned and self.move_aligned.isChecked():
            pulled = [c.label for c in self.aligned if self._pulled_back(c)]
            if pulled:
                lines.append(tr("Questi chunk allineati hanno marker o posizioni delle foto propri che al prossimo "
                                "calcolo della georeferenziazione li riporterebbero indietro: {0}", ", ".join(pulled)))
        self.summary.setText("\n".join(lines))
        self.b_apply.setEnabled(ok)

    @staticmethod
    def _pulled_back(chunk) -> bool:
        """An aligned chunk that would go back where it was at its next fit: 3+ typed control markers, or 3+ photo
        positions of its own."""
        controls = orient._control_markers(chunk)
        cams = [c for c in chunk.aligned_cameras if c.reference.enabled and c.reference.location]
        return len(controls) >= 3 or len(cams) >= 3

    def kwargs(self) -> dict:
        """The arguments of :func:`..engine.orient.orient` for what the dialog holds."""
        rows = [k for k in ROWS if self.point(k) is not None]
        plane = self.plane_radio.isChecked()
        if plane:
            rows = [k for k in ("A", "B") if k in rows] if {"A", "B"} <= set(rows) else []
        out: dict = {"flip": self.flip.isChecked(), "quota": float(self.quota.value()),
                     "origin_xy": (float(self.origin_x.value()), float(self.origin_y.value())),
                     "move_aligned": self.move_aligned.isChecked()}
        if not plane:
            out["x_along_ab"] = self.x_along.isChecked()
        if rows:
            keys = [self.rows[k]["marker"] for k in rows]
            if all(k is not None for k in keys):
                out["markers"] = keys
            else:
                out["points"] = [self.point(k).tolist() for k in rows]
        if plane:
            out["plane_xyz"] = self.plane_xyz
            out["toward"] = self._toward_cameras()
        distance = self._distance()
        if distance is not None:
            out["distance"] = distance
        return out

    def _apply(self) -> None:
        if not self.b_apply.isEnabled():
            return
        kwargs = self.kwargs()
        self.accept()
        self.apply_requested.emit(kwargs)

    # ------------------------------------------------------------------ closing
    def done(self, r: int) -> None:
        if self._connected:
            self._connected = False
            for signal, slot in ((self.model_view.orient_requested, self._picked),
                                 (self.ortho_view.orient_clicked, self._raster_clicked)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            self.model_view.hide_axes()
            self._end_raster_pick()
            self._lasso_waiting = False
        super().done(r)


class OrientStep:
    """An orientation (the ``result`` of :func:`..engine.orient.orient`) as one step of *Modifica > Annulla*: undone,
    the state it took before puts everything back and the listeners hear of the inverse motion; done again, the state
    taken at the first undo comes back and the listeners hear of the motion; the window reads its drawings, sections
    and views again after each."""

    KEYS = ("orientation", "marker_excluded")  # chunk.meta kept beside the state (orient's ``previous``)

    def __init__(self, window, chunk, result: dict):
        self.window, self.chunk = window, chunk
        self.before = result["state"]
        self.before_meta = copy.deepcopy(result.get("previous") or {})
        self.after = None
        self.after_meta: dict = {}
        self.m = np.asarray(result["M"], float)

    def _chunks(self) -> list:
        return [saved["chunk"] for saved in self.before.chunks]

    def _meta(self, saved: dict) -> None:
        for k in self.KEYS:
            if k in saved:
                self.chunk.meta[k] = copy.deepcopy(saved[k])
            else:
                self.chunk.meta.pop(k, None)

    def undo(self) -> None:
        chunks = self._chunks()
        if self.after is None:  # the oriented state, to do it again
            self.after = orient.capture(chunks, self.before.sections)
            self.after_meta = {k: copy.deepcopy(self.chunk.meta[k]) for k in self.KEYS if k in self.chunk.meta}
        orient.put_back(self.before)
        self._meta(self.before_meta)
        inverse = np.linalg.inv(self.m)
        for ch in chunks:
            orient.fire(ch, inverse)
        self.window._orient_reload(self.chunk)

    def redo(self) -> None:
        if self.after is None:
            return
        orient.put_back(self.after)
        self._meta(self.after_meta)
        for ch in self._chunks():
            orient.fire(ch, self.m)
        self.window._orient_reload(self.chunk)

    def drop(self) -> None:
        orient.drop(self.before)
        if self.after is not None:
            orient.drop(self.after)
