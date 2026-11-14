"""*Acqua: livelli e bacini* as a pane that stays open: the water of a survey of several years or of several basins
(:mod:`..engine.water`) in four steps, each with its state - the levels per chunk or per date from a table, the basins
of the active chunk (a level, or a plane through points picked on the shoreline; a polygon drawn or taken from a layer;
the factor n), the effective factor measured on control points (markers triangulated through the water, or a CSV of
points of known true height) with the residual of every point in a table, and the correction of the dense cloud.

The single level of a chunk is still set in *Livello dell'acqua e rifrazione* (the window); this pane adds what a
survey of several levels needs. Every change of the levels, the basins or the factor is one step of *Modifica >
Annulla*; the correction is a new version of the dense cloud, made in the worker.
"""
from __future__ import annotations

import contextlib

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..engine import colour_mask, water
from .i18n import number, tr
from .icons import icon
from .water_dialog import error_text, polygon_layers

STEPS = ("table", "basins", "factor", "correct")
STATE_ICONS = {"todo": "step_todo", "done": "step_done", "optional": "step_todo", "running": "step_running"}


def state_names() -> dict[str, str]:
    return {"todo": tr("da fare"), "done": tr("fatto"), "optional": tr("facoltativo"), "running": tr("in corso")}


class WaterPane(QWidget):
    """The four steps of the water of the active chunk (see the module); ``window`` the main window (its chunk, its
    worker, its 2D view for the picking, its history for Annulla)."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.working = False
        self.fit: dict | None = None  # the last factor estimated (shown in the table, applied on request)
        self.points: list[dict] = []  # control points loaded from a CSV
        lay = QVBoxLayout(self)  # the dock puts the pane in a scroll area
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        intro = QLabel(tr("L'acqua di un rilievo di più anni o di più bacini: un livello e un fattore per chunk o per "
                          "data, i bacini con il loro livello e il loro fattore, il fattore misurato sui punti di "
                          "controllo, la correzione della nuvola. Il livello unico si imposta in Livello dell'acqua e "
                          "rifrazione."))
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

        # 1. levels per chunk or per date
        body = QWidget()
        b = QHBoxLayout(body)
        b.setContentsMargins(0, 0, 0, 0)
        self.b_table = QPushButton(tr("Carica la tabella dei livelli..."))
        self.b_table.setToolTip(tr("Un CSV con le colonne chunk o date, level, n: un livello e un fattore per ogni "
                                   "chunk (per esempio uno per anno)"))
        b.addWidget(self.b_table)
        b.addStretch(1)
        step(0, "table", tr("Livelli per chunk o per data"), body)

        # 2. the basins of the active chunk
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        self.basins = QTableWidget(0, 4)
        self.basins.setHorizontalHeaderLabels([tr("Bacino"), tr("Livello (m)"), "n", tr("Dove")])
        self.basins.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.basins.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.basins.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.basins.setMaximumHeight(140)
        self.basins.itemSelectionChanged.connect(self._selected)
        bl.addWidget(self.basins)
        form = QFormLayout()
        self.e_name = QLineEdit()
        self.e_name.setPlaceholderText(tr("nome del bacino"))
        form.addRow(tr("Nome"), self.e_name)
        self.s_level = QDoubleSpinBox()
        self.s_level.setRange(-12000.0, 12000.0)
        self.s_level.setDecimals(3)
        self.s_level.setSingleStep(0.01)
        self.s_level.setSuffix(" m")
        form.addRow(tr("Livello"), self.s_level)
        self.s_n = QDoubleSpinBox()
        self.s_n.setRange(*water.N_RANGE)
        self.s_n.setDecimals(3)
        self.s_n.setSingleStep(0.01)
        self.s_n.setValue(water.N_SEA)
        form.addRow(tr("Fattore n"), self.s_n)
        bl.addLayout(form)
        row = QHBoxLayout()
        self.b_add_level = QPushButton(tr("Nuovo dal livello"))
        self.b_add_shore = QPushButton(tr("Nuovo dai punti della riva"))
        self.b_add_shore.setToolTip(tr("Clic su tre o più punti della riva nella vista 2D, doppio clic per finire: il "
                                       "piano dell'acqua per le loro quote sul DEM"))
        row.addWidget(self.b_add_level)
        row.addWidget(self.b_add_shore)
        row.addStretch(1)
        bl.addLayout(row)
        row = QHBoxLayout()
        self.b_draw = QPushButton(tr("Disegna il poligono"))
        self.c_layer = QComboBox()
        self.c_layer.setToolTip(tr("Un layer di disegni liberi con i poligoni del bacino"))
        self.b_layer = QPushButton(tr("Poligono dal layer"))
        self.b_everywhere = QPushButton(tr("Dappertutto"))
        self.b_everywhere.setToolTip(tr("Il bacino senza poligono: prende i punti che nessun altro bacino prende"))
        for w in (self.b_draw, self.c_layer, self.b_layer, self.b_everywhere):
            row.addWidget(w)
        row.addStretch(1)
        bl.addLayout(row)
        row = QHBoxLayout()
        self.b_set_n = QPushButton(tr("Imposta n e livello"))
        self.b_set_n.setToolTip(tr("Il fattore e il livello scritti sopra per il bacino scelto"))
        self.b_remove = QPushButton(tr("Rimuovi il bacino"))
        row.addWidget(self.b_set_n)
        row.addWidget(self.b_remove)
        row.addStretch(1)
        bl.addLayout(row)
        step(1, "basins", tr("Bacini del chunk attivo"), body)

        # 3. the factor from control points
        body = QWidget()
        fl = QVBoxLayout(body)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(QLabel(tr("Marker con il riferimento sott'acqua (triangolati attraverso l'acqua):")))
        self.markers = QListWidget()
        self.markers.setMaximumHeight(90)
        fl.addWidget(self.markers)
        row = QHBoxLayout()
        self.b_points = QPushButton(tr("Punti da un CSV..."))
        self.b_points.setToolTip(tr("Colonne label, x, y, z (quota vera) e, se c'è, apparent; la quota apparente "
                                    "altrimenti si legge sul DEM o sulla nuvola"))
        self.c_source = QComboBox()
        self.c_source.addItem(tr("Quota apparente dal DEM"), "dem")
        self.c_source.addItem(tr("Quota apparente dalla nuvola"), "cloud")
        row.addWidget(self.b_points)
        row.addWidget(self.c_source)
        row.addStretch(1)
        fl.addLayout(row)
        row = QHBoxLayout()
        self.c_basin = QComboBox()
        self.c_basin.setToolTip(tr("I punti di un bacino solo, e il fattore applicato a quel bacino"))
        self.c_fitlevel = QCheckBox(tr("Stima anche il livello"))
        row.addWidget(self.c_basin)
        row.addWidget(self.c_fitlevel)
        row.addStretch(1)
        fl.addLayout(row)
        row = QHBoxLayout()
        self.b_estimate = QPushButton(tr("Stima il fattore"))
        self.b_apply = QPushButton(tr("Applica il fattore"))
        row.addWidget(self.b_estimate)
        row.addWidget(self.b_apply)
        row.addStretch(1)
        fl.addLayout(row)
        self.residuals = QTableWidget(0, 5)
        self.residuals.setHorizontalHeaderLabels([tr("Punto"), tr("Quota vera"), tr("Apparente"), tr("Corretta"),
                                                  tr("Scarto")])
        self.residuals.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.residuals.setMaximumHeight(160)
        fl.addWidget(self.residuals)
        step(2, "factor", tr("Fattore dai punti di controllo"), body)

        # 4. the correction
        body = QWidget()
        cl = QHBoxLayout(body)
        cl.setContentsMargins(0, 0, 0, 0)
        self.c_dem = QCheckBox(tr("Ricostruisci il DEM dalla nuvola corretta"))
        self.b_correct = QPushButton(tr("Correggi la nuvola"))
        cl.addWidget(self.b_correct)
        cl.addWidget(self.c_dem)
        cl.addStretch(1)
        step(3, "correct", tr("Correzione della nuvola densa"), body)
        lay.addStretch(1)

        self.b_table.clicked.connect(self.load_table)
        self.b_add_level.clicked.connect(self.add_from_level)
        self.b_add_shore.clicked.connect(self.start_shore)
        self.b_draw.clicked.connect(self.start_polygon)
        self.b_layer.clicked.connect(self.polygon_from_layer)
        self.b_everywhere.clicked.connect(lambda: self._set_polygon(None))
        self.b_set_n.clicked.connect(self.set_selected)
        self.b_remove.clicked.connect(self.remove_selected)
        self.b_points.clicked.connect(self.load_points)
        self.b_estimate.clicked.connect(self.estimate)
        self.b_apply.clicked.connect(self.apply)
        self.b_correct.clicked.connect(self.correct)
        with contextlib.suppress(AttributeError):
            window.runner.busy_changed.connect(self._busy_changed)
        self.refresh()

    # ------------------------------------------------------------------ state
    @property
    def chunk(self):
        return getattr(self.window, "chunk", None)

    def _warn(self, text: str) -> None:
        self.window._warn(text)

    def _err(self, exc: Exception) -> None:
        self._warn(tr("Acqua: {0}", error_text(exc)))

    def _recorded(self, text: str) -> None:
        """A change of the water stored: one step of Annulla, the tree and the pane shown again."""
        w = self.window
        w.setWindowModified(True)
        if w.history is not None and not w.history.applying:
            w.history.record(text)
        with contextlib.suppress(AttributeError):
            w.workspace.populate(w.doc)
        with contextlib.suppress(AttributeError):
            w._refresh_water_dialog()
        self.refresh()

    def _busy_changed(self, busy: bool) -> None:
        if not busy and self.working:
            with contextlib.suppress(RuntimeError):
                self.working = False
                self.refresh()

    def basin_records(self) -> list[dict]:
        ch = self.chunk
        rec = (ch.meta.get("water") or {}) if ch is not None else {}
        return [dict(b) for b in (rec.get("basins") or [])] if isinstance(rec, dict) else []

    def selected_name(self) -> str | None:
        rows = self.basins.selectionModel().selectedRows() if self.basins.selectionModel() else []
        if not rows:
            return None
        item = self.basins.item(rows[0].row(), 0)
        return item.text() if item is not None else None

    def refresh(self) -> None:
        """Lists, tables and the states of the steps as the active chunk has them now."""
        ch = self.chunk
        keep = self.selected_name()
        shown = water.basins_of(ch) if ch is not None else []
        self.basins.blockSignals(True)
        self.basins.setRowCount(len(shown))
        for r, b in enumerate(shown):
            plane = b.get("a") or b.get("b")
            level = number(b["level"], 3) + (" " + tr("(piano)") if plane else "")
            where = tr("dappertutto altrove") if b.get("polygon") is None else tr("nel poligono")
            for c, text in enumerate((b["name"], level, number(b["n"], 3), where)):
                self.basins.setItem(r, c, QTableWidgetItem(text))
            if b["name"] == keep:
                self.basins.selectRow(r)
        self.basins.blockSignals(False)
        self.c_layer.clear()
        self.c_layer.addItems(polygon_layers(ch) if ch is not None else [])
        current = self.c_basin.currentData()
        self.c_basin.clear()
        self.c_basin.addItem(tr("Tutti i punti (il livello del chunk)"), None)
        for b in shown:
            self.c_basin.addItem(tr("Bacino {0}", b["name"]), b["name"])
        idx = self.c_basin.findData(current)
        self.c_basin.setCurrentIndex(max(idx, 0))
        checked = {self.markers.item(i).text() for i in range(self.markers.count())
                   if self.markers.item(i).checkState() == Qt.CheckState.Checked}
        self.markers.clear()
        for m in (ch.markers if ch is not None else []):
            if m.position is not None and m.reference is not None and m.reference.location is not None:
                it = QListWidgetItem(m.label)
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Checked if m.label in checked else Qt.CheckState.Unchecked)
                self.markers.addItem(it)
        self.update_states()

    def update_states(self) -> None:
        ch = self.chunk
        lv = water.level_of(ch) if ch is not None else None
        rec = (ch.meta.get("water") or {}) if ch is not None else {}
        rec = rec if isinstance(rec, dict) else {}
        basins = self.basin_records()
        done = (ch.meta.get("dense") or {}).get("refraction") if ch is not None else None
        states = {"table": "done" if lv is not None else "todo",
                  "basins": "done" if basins else "optional",
                  "factor": "done" if rec.get("factor_fit") else ("optional" if lv is not None else "todo"),
                  "correct": "running" if self.working else ("done" if done else "todo")}
        texts = {
            "table": (tr("Chunk {0}: livello {1} m, n {2}{3}.", ch.label, number(lv["level"], 3), number(lv["n"], 3),
                         (" (" + rec["date"] + ")") if rec.get("date") else "")
                      if lv is not None else tr("Nessun livello per il chunk attivo: carica la tabella o impostalo in "
                                                "Livello dell'acqua e rifrazione.")),
            "basins": (tr("{0} bacini: ogni punto della nuvola prende il livello e il fattore del suo bacino.",
                          len(basins)) if basins else tr("Nessun bacino: un livello e un fattore per tutto il chunk.")),
            "factor": (tr("n {0} da {1} punti, scarto quadratico medio {2} m.", number(rec["factor_fit"]["n"], 4),
                          rec["factor_fit"]["points"], number(rec["factor_fit"].get("rmse") or 0.0, 4))
                       if rec.get("factor_fit") else tr("Scegli i marker o carica i punti, poi Stima il fattore.")),
            "correct": (tr("Nuvola attiva corretta: {0} punti sotto il livello.", number(done.get("below") or 0))
                        if done else tr("Correzione parassiale: vale per foto quasi nadirali.")),
        }
        names = state_names()
        for key in STEPS:
            mark, title, text = self.titles[key]
            mark.setPixmap(icon(STATE_ICONS[states[key]]).pixmap(16, 16))
            title.setText(f"<b>{text}</b> — {names[states[key]]}")
            self.states[key].setText(texts[key])
        self.step_states = states
        has_chunk = ch is not None and not self.working
        sel = self.selected_name() is not None
        for w in (self.b_table,):
            w.setEnabled(not self.working)
        for w in (self.b_add_level, self.b_add_shore):
            w.setEnabled(has_chunk)
        for w in (self.b_draw, self.b_layer, self.b_everywhere, self.b_set_n, self.b_remove):
            w.setEnabled(has_chunk and sel)
        self.b_layer.setEnabled(has_chunk and sel and self.c_layer.count() > 0)
        self.b_estimate.setEnabled(has_chunk)
        self.b_apply.setEnabled(has_chunk and self.fit is not None)
        cloud = ch.product_path("point_cloud") if ch is not None else None
        self.b_correct.setEnabled(has_chunk and lv is not None and cloud is not None and cloud.exists() and not done)

    def _selected(self) -> None:
        name = self.selected_name()
        for b in water.basins_of(self.chunk) if self.chunk is not None else []:
            if b["name"] == name:
                self.e_name.setText(b["name"])
                self.s_level.setValue(float(b["level"]))
                self.s_n.setValue(float(b["n"]))
        self.update_states()

    # ------------------------------------------------------------------ 1. the table
    def load_table(self, path: str | None = None) -> bool:
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, tr("Tabella dei livelli"), "", tr("CSV (*.csv *.txt)"))
        if not path:
            return False
        try:
            done = water.apply_table(self.window.doc, water.read_table(path))
        except (ValueError, OSError, KeyError) as exc:
            self._err(exc)
            return False
        for d in done:
            self.window.log_pane.info(tr("Livello dell'acqua di {0}: {1} m, n {2}", d["chunk"], number(d["level"], 3),
                                         number(d["n"], 3)))
        self._recorded(tr("Tabella dei livelli"))
        return True

    # ------------------------------------------------------------------ 2. the basins
    def _new_name(self) -> str:
        name = self.e_name.text().strip()
        taken = {b["name"] for b in self.basin_records()}
        if not name or name in taken:
            k = len(taken) + 1
            while f"{tr('Bacino')} {k}" in taken:
                k += 1
            name = f"{tr('Bacino')} {k}"
        return name

    def _store_basins(self, basins: list[dict], text: str) -> bool:
        try:
            water.set_basins(self.chunk, basins)
        except ValueError as exc:
            self._err(exc)
            return False
        self._recorded(text)
        return True

    def add_from_level(self) -> bool:
        if self.chunk is None:
            return False
        try:
            new = water.basin(self._new_name(), level=float(self.s_level.value()), n=float(self.s_n.value()))
        except ValueError as exc:
            self._err(exc)
            return False
        return self._add(new)

    def _add(self, new: dict) -> bool:
        free = [b for b in self.basin_records() if b.get("polygon") is None]
        if free and new.get("polygon") is None:  # a second basin without polygon: stored once its polygon is drawn
            self._warn(tr("C'è già un bacino senza poligono: disegna il poligono di «{0}» o prendilo da un layer.",
                          new["name"]))
            return self.start_polygon(new)
        ok = self._store_basins(self.basin_records() + [new], tr("Nuovo bacino"))
        if ok:
            self._select(new["name"])
        return ok

    def _select(self, name: str) -> None:
        for r in range(self.basins.rowCount()):
            if self.basins.item(r, 0).text() == name:
                self.basins.selectRow(r)

    def start_shore(self) -> bool:
        if self.chunk is None:
            return False
        return self.window.request_geometry("line", self.shore_picked,
                                            hint=tr("Clic su tre o più punti della riva, doppio clic o Invio per "
                                                    "finire (Esc annulla)"))

    def shore_picked(self, xy) -> bool:
        """The points clicked on the shore: their heights on the DEM, the plane of the water through them, a new
        basin with it."""
        xy = np.asarray(xy, float).reshape(-1, 2)
        try:
            path = water._dem(self.chunk)
            z = water._heights_at(path, xy)
            ok = np.isfinite(z)
            new = water.basin(self._new_name(), n=float(self.s_n.value()), shore=np.column_stack([xy[ok], z[ok]]))
        except ValueError as exc:
            self._err(exc)
            return False
        f = new["shore"]
        self.window.log_pane.info(tr("Piano della riva: inclinazione {0} gradi, scarto {1} m su {2} punti",
                                     number(f["tilt_deg"], 3), number(f["rmse"], 4), f["points"]))
        return self._add(new)

    def start_polygon(self, new: dict | None = None) -> bool:
        if self.chunk is None:
            return False
        name = new["name"] if isinstance(new, dict) else self.selected_name()
        if not name:
            return False
        return self.window.request_geometry("polygon", lambda xy: self._polygon_drawn(name, xy, new if isinstance(
            new, dict) else None), hint=tr("Il poligono del bacino «{0}»: clic sui vertici, doppio clic per chiudere "
                                           "(Esc annulla)", name))

    def _polygon_drawn(self, name: str, xy, new: dict | None = None) -> bool:
        ring = np.asarray(xy, float).reshape(-1, 2)
        if new is not None:
            try:
                new = {**new, "polygon": water._polygon_wkt(ring)}
            except ValueError as exc:
                self._err(exc)
                return False
            ok = self._store_basins(self.basin_records() + [new], tr("Nuovo bacino"))
            if ok:
                self._select(name)
            return ok
        self._select(name)
        return self._set_polygon(ring)

    def polygon_from_layer(self) -> bool:
        layer = self.c_layer.currentText()
        if not layer:
            return False
        try:
            geom = colour_mask.union(self.chunk, layer)
        except ValueError as exc:
            self._err(exc)
            return False
        return self._set_polygon(geom)

    def _edited(self, change, text: str) -> bool:
        """The basin chosen changed by ``change(record)`` (in the world of now: basins moved since are written again
        in the world of now)."""
        name = self.selected_name()
        if name is None:
            return False
        basins = water.basins_of(self.chunk)
        for b in basins:
            b.pop("built_in", None)
            if b["name"] == name:
                try:
                    change(b)
                except ValueError as exc:
                    self._err(exc)
                    return False
        ok = self._store_basins(basins, text)
        if ok:
            self._select(name)
        return ok

    def _set_polygon(self, polygon) -> bool:
        return self._edited(lambda b: b.update(polygon=water._polygon_wkt(polygon)), tr("Poligono del bacino"))

    def set_selected(self) -> bool:
        level, n = float(self.s_level.value()), water._check_n(float(self.s_n.value()))

        def change(b):  # a plane keeps its tilt, at the level written (at its centre)
            b.update(level=level, n=n)

        return self._edited(change, tr("Livello e fattore del bacino"))

    def remove_selected(self) -> bool:
        name = self.selected_name()
        if name is None:
            return False
        basins = [b for b in water.basins_of(self.chunk) if b["name"] != name]
        for b in basins:
            b.pop("built_in", None)
        return self._store_basins(basins, tr("Bacino rimosso"))

    # ------------------------------------------------------------------ 3. the factor
    def load_points(self, path: str | None = None) -> bool:
        import csv

        if not path:
            path, _ = QFileDialog.getOpenFileName(self, tr("Punti di controllo"), "", tr("CSV (*.csv *.txt)"))
        if not path:
            return False
        rows = []
        try:
            with open(path, encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
                    rows.append({"label": r.get("label") or f"P{len(rows) + 1}", "x": float(r["x"]),
                                 "y": float(r["y"]), "z": float(r["z"]),
                                 "apparent": float(r["apparent"]) if r.get("apparent") else None})
        except (OSError, KeyError, ValueError) as exc:
            self._warn(tr("Punti di controllo: {0}", str(exc)))
            return False
        self.points = rows
        self.window.log_pane.info(tr("{0} punti di controllo caricati", len(rows)))
        return True

    def chosen_markers(self) -> list[str]:
        return [self.markers.item(i).text() for i in range(self.markers.count())
                if self.markers.item(i).checkState() == Qt.CheckState.Checked]

    def estimate(self) -> dict | None:
        """The factor from the markers chosen and the points loaded; the residuals in the table."""
        ch = self.chunk
        if ch is None:
            return None
        try:
            controls = water.control_points(ch, markers=self.chosen_markers(), points=self.points,
                                            source=self.c_source.currentData())
            fit = water.estimate_factor(ch, controls, fit_level=self.c_fitlevel.isChecked(),
                                        basin_name=self.c_basin.currentData())
        except (ValueError, KeyError) as exc:
            self._err(exc if isinstance(exc, ValueError) else ValueError(str(exc)))
            return None
        self.fit = fit
        self.residuals.setRowCount(len(fit["residuals"]))
        for r, row in enumerate(fit["residuals"]):
            vals = (row["label"], number(row["z"], 3), "-" if row["apparent"] is None else number(row["apparent"], 3),
                    "-" if row["corrected"] is None else number(row["corrected"], 3),
                    tr("escluso") if row["residual"] is None else number(row["residual"], 4))
            for c, text in enumerate(vals):
                self.residuals.setItem(r, c, QTableWidgetItem(text))
        text = tr("Fattore stimato: n {0} da {1} punti, scarto quadratico medio {2} m", number(fit["n"], 4),
                  fit["points"], number(fit["rmse"] or 0.0, 4))
        if fit["level"] is not None:
            text += tr(", livello {0} m", number(fit["level"], 3))
        self.window.log_pane.info(text)
        if not fit["in_range"]:
            self._warn(tr("Il fattore stimato ({0}) è fuori dall'intervallo 1-2: controlla i punti.",
                          number(fit["n"], 3)))
        self.update_states()
        return fit

    def apply(self) -> bool:
        if self.fit is None or self.chunk is None:
            return False
        try:
            water.apply_factor(self.chunk, self.fit)
        except ValueError as exc:
            self._err(exc)
            return False
        self._recorded(tr("Fattore della rifrazione"))
        return True

    # ------------------------------------------------------------------ 4. the correction
    def correct(self) -> bool:
        ch, dem = self.chunk, self.c_dem.isChecked()
        if ch is None or self.working:
            return False

        def work(p):
            try:
                return water.correct_cloud(ch, rebuild_dem=dem, progress=p)
            except ValueError as exc:
                return {"error": exc}

        self.working = True
        self.update_states()
        started = self.window.run_task("Correzione della rifrazione", work, self._corrected)
        if not started:
            self.working = False
            self.update_states()
        return started

    def _corrected(self, result: dict) -> None:
        self.working = False
        if "error" in result:
            self._warn(tr("Correzione della rifrazione: {0}", error_text(result["error"])))
        else:
            self.window.log_pane.info(tr("Nuvola corretta per la rifrazione: {0} punti sotto il livello",
                                         number(result["below"])))
        with contextlib.suppress(RuntimeError):
            self.refresh()


__all__ = ["WaterPane", "STEPS"]
