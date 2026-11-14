"""*Legami automatici*, the section of the window *Aggancia chunk con legami* that finds ties by itself
(:mod:`..engine.auto_ties`), as a workflow of steps with their states:

1. **Coppie** - *Trova coppie*: the pairs of chunks with a DEM and how much they overlap where they are now (in the
   worker); the candidates ticked.
2. **Abbinamento** - *Abbina*: the ticked pairs matched (orthophoto or relief of the DEM, in the worker): for each the
   matches, those that agree, the points refined, the similarity that would bring the second chunk onto the first
   (rotation, scale in ppm, shift) and the residuals; a pair that does not show the same ground *scartata* in red.
3. **Legami** - *Accetta*: the pairs proposed and ticked made into ties (one step of *Modifica > Annulla*); they appear
   in the table of the ties above, with their markers "auto N".
4. **Stima** - the window's own *Controlla* and *Applica* (the joint solve with its checks).

Nothing is stored before *Accetta*. The results of a matching hold while the chunks stay where they were: a chunk
moved (an *Applica*, an alignment) marks them *da rifare*. Worker callbacks come back through the window's TaskRunner;
no widget is touched from the worker."""
from __future__ import annotations

import html

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..engine import auto_ties
from ..engine import ties as ties_mod
from . import i18n
from .i18n import tr

GREEN = "#1e8449"
RED = "#c0392b"
GREY = "#7f8c8d"
STEPS = ("pairs", "match", "accept", "solve")


def say(m: dict | None) -> str:
    """A message of the engine in the language of the interface (``tr`` of its Italian template)."""
    if not m:
        return ""
    args = [ties_mod.Comma(a) if i18n.current() == "it" and isinstance(a, float) else a for a in m.get("args", [])]
    if m.get("code") == "error":  # an error text, not a template of the catalog
        return (m["it"] if i18n.current() == "it" else m["en"]).replace("{{", "{").replace("}}", "}")
    return tr(m["it"], *args)


def placement(doc) -> dict:
    """Where every chunk is now: a matching holds while it does not change."""
    return {c.key: (np.asarray(c.transform, float).tolist(), c.crs) for c in doc.chunks}


class AutoTiesPanel(QWidget):
    """The section of the automatic ties (see the module); ``dialog`` the window of the ties."""

    COLS = ("use", "pair", "overlap", "matches", "agree", "refined", "rotation", "scale", "shift", "rms", "state")

    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        self.pairs: list[dict] = []  # step 1
        self.results: dict[tuple[int, int], auto_ties.PairResult] = {}  # step 2, by (a, b)
        self.state = {k: "todo" for k in STEPS}
        self._placed: dict | None = None  # placement() of the last matching
        self.busy = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>" + html.escape(tr("Legami automatici")) + "</b>"))
        self.step_labels = {}
        for i, (key, text) in enumerate((("pairs", "Coppie"), ("match", "Abbinamento"), ("accept", "Legami"),
                                         ("solve", "Stima"))):
            lab = QLabel()
            lab.setProperty("step", key)
            self.step_labels[key] = (lab, f"{i + 1}. {tr(text)}")
            head.addWidget(lab)
        head.addStretch(1)
        lay.addLayout(head)

        opts = QHBoxLayout()
        opts.addWidget(QLabel(tr("Immagini")))
        self.source = QComboBox()
        self.source.addItem(tr("Ortofoto"), "ortho")
        self.source.addItem(tr("Rilievo del DEM"), "relief")
        self.source.setToolTip(tr("Rilievo del DEM: per rilievi con colori diversi (sabbia spostata, acqua, "
                                  "vegetazione, un'altra luce)"))
        opts.addWidget(self.source)
        opts.addWidget(QLabel(tr("Scarto della posizione (m)")))
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0.0, 1000.0)
        self.margin.setDecimals(1)
        self.margin.setValue(auto_ties.MARGIN)
        self.margin.setToolTip(tr("Quanto può essere sbagliata la posizione attuale di un chunk (il GPS di un drone: "
                                  "qualche metro): le corrispondenze si cercano fin lì"))
        opts.addWidget(self.margin)
        self.whole = QCheckBox(tr("Su tutto il raster"))
        self.whole.setToolTip(tr("Cerca su tutta l'ortofoto, ovunque sia il chunk: per due sistemi locali senza "
                                 "relazione o un GPS molto sbagliato (più lento)"))
        opts.addWidget(self.whole)
        opts.addWidget(QLabel(tr("Legami per coppia")))
        self.max_ties = QSpinBox()
        self.max_ties.setRange(3, 100)
        self.max_ties.setValue(auto_ties.MAX_TIES)
        opts.addWidget(self.max_ties)
        opts.addStretch(1)
        lay.addLayout(opts)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels([tr("Usa"), tr("Coppia"), tr("Sovrapposizione"), tr("Corrispondenze"),
                                              tr("Concordi"), tr("Raffinati"), tr("Rotazione (°)"), tr("Scala (ppm)"),
                                              tr("Spostamento (m)"), tr("RMS (m)"), tr("Esito")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self.show_report)
        lay.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.b_pairs = QPushButton(tr("Trova coppie"))
        self.b_pairs.setToolTip(tr("Le coppie di chunk con un DEM che si sovrappongono dove sono ora (i chunk di uno "
                                   "stesso gruppo no: si muovono insieme)"))
        self.b_match = QPushButton(tr("Abbina"))
        self.b_match.setToolTip(tr("Cerca gli stessi punti nelle coppie spuntate, stima la similitudine fra i due "
                                   "chunk e la raffina: niente viene salvato"))
        self.b_accept = QPushButton(tr("Accetta"))
        self.b_accept.setToolTip(tr("Le coppie proposte e spuntate diventano legami (marker «auto N»), poi "
                                    "Controlla e Applica"))
        for b in (self.b_pairs, self.b_match, self.b_accept):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.b_pairs.clicked.connect(self.find_pairs)
        self.b_match.clicked.connect(self.match)
        self.b_accept.clicked.connect(self.accept)
        self.refresh_steps()

    # ------------------------------------------------------------------ state
    @property
    def doc(self):
        return self.dialog.doc

    @property
    def window(self):
        return self.dialog.window

    def set_step(self, key: str, state: str) -> None:
        self.state[key] = state
        self.refresh_steps()

    def refresh_steps(self) -> None:
        marks = {"todo": ("○", GREY), "running": ("…", "#b9770e"), "done": ("✓", GREEN), "stale": ("!", RED)}
        tips = {"todo": tr("da fare"), "running": tr("in corso"), "done": tr("fatto"), "stale": tr("da rifare")}
        for key, (lab, text) in self.step_labels.items():
            mark, colour = marks[self.state[key]]
            lab.setText(f'<span style="color:{colour}">{mark} {html.escape(text)}</span>')
            lab.setToolTip(tips[self.state[key]])
        ok = any(r.ok for r in self.results.values())
        for b, on in ((self.b_pairs, True), (self.b_match, bool(self.pairs)), (self.b_accept, ok)):
            b.setEnabled(on and not self.busy)

    def follow(self) -> None:
        """The chunks changed (moved, added, removed): a matching done before no longer holds."""
        if self._placed is not None and placement(self.doc) != self._placed:
            self.results = {}
            self._placed = None
            if self.state["match"] == "done":
                self.set_step("match", "stale")
            self.fill()

    def checked_pairs(self) -> list[tuple[int, int]]:
        out = []
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                out.append(tuple(item.data(Qt.ItemDataRole.UserRole)))
        return out

    def check_pairs(self, pairs) -> None:
        """Tick these pairs (``(a, b)`` keys) and untick the others."""
        wanted = {tuple(p) for p in pairs}
        for i in range(self.table.rowCount()):
            item = self.table.item(i, 0)
            item.setCheckState(Qt.CheckState.Checked if tuple(item.data(Qt.ItemDataRole.UserRole)) in wanted
                               else Qt.CheckState.Unchecked)

    # ------------------------------------------------------------------ the table
    def fill(self) -> None:
        checked = set(self.checked_pairs()) if self.table.rowCount() else None
        self.table.setRowCount(len(self.pairs))
        for i, p in enumerate(self.pairs):
            key = (p["a"], p["b"])
            r = self.results.get(key)
            use = QTableWidgetItem()
            use.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            on = key in checked if checked is not None else p["candidate"]
            use.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            use.setData(Qt.ItemDataRole.UserRole, list(key))
            self.table.setItem(i, 0, use)
            self.table.setItem(i, 1, QTableWidgetItem(f"{p['b_label']} → {p['a_label']}"))
            share = i18n.number(100 * p["share_margin"], 0) + " %"
            self.table.setItem(i, 2, QTableWidgetItem(share))
            cells = ["", "", "", "", "", "", ""]
            if r is not None:
                cells = [str(r.matches), str(r.inliers), str(r.refined)]
                if r.transform is not None:
                    cells += [i18n.number(r.rotation_deg, 3), i18n.number(r.scale_ppm, 0), i18n.number(r.shift, 3),
                              i18n.number(r.rms, 4)]
                else:
                    cells += ["", "", "", ""]
            for c, text in enumerate(cells):
                self.table.setItem(i, 3 + c, QTableWidgetItem(text))
            if r is not None:
                state, colour = ((tr("proposta"), GREEN) if r.ok else (tr("scartata"), RED))
                why = "; ".join(say(m) for m in r.messages)
                text = state + (": " + why if why else "")
            else:
                text, colour = (say(p.get("reason")) if not p["candidate"] else tr("candidata")), \
                    (GREY if not p["candidate"] else None)
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            if colour:
                item.setForeground(QColor(colour))
            self.table.setItem(i, 10, item)
        self.refresh_steps()

    def show_report(self) -> None:
        """The residual report of the pair selected in the report of the window."""
        rows = {i.row() for i in self.table.selectedIndexes()}
        if len(rows) != 1:
            return
        item = self.table.item(rows.pop(), 0)
        r = self.results.get(tuple(item.data(Qt.ItemDataRole.UserRole))) if item is not None else None
        if r is None:
            return
        text = auto_ties.report_text(r, "it" if i18n.current() == "it" else "en")
        self.dialog.report.setHtml("<pre>" + html.escape(text) + "</pre>")

    # ------------------------------------------------------------------ steps in the worker
    def _busy(self, on: bool) -> None:
        self.busy = on
        self.refresh_steps()

    def _gone(self, doc) -> bool:
        try:
            self.isVisible()
        except RuntimeError:
            return True
        return doc is not self.doc

    def _failed(self, summary: str, details: str = "") -> None:
        self._busy(False)
        for key in ("pairs", "match"):
            if self.state[key] == "running":
                self.set_step(key, "todo")
        self.window._error(summary, details)

    def find_pairs(self) -> bool:
        """Step 1 in the worker. Returns whether it started."""
        doc = self.doc
        margin = float(self.margin.value())

        def work(p):
            return auto_ties.find_pairs(doc, margin=margin, progress=p)

        def done(pairs):
            if self._gone(doc):
                return
            self._busy(False)
            self.pairs, self.results, self._placed = pairs, {}, None
            self.set_step("pairs", "done")
            self.set_step("match", "todo")
            self.fill()
            n = sum(p["candidate"] for p in pairs)
            self.window.log_pane.info(tr("Legami automatici: {0} coppie candidate su {1}", n, len(pairs)))

        self._busy(True)
        self.set_step("pairs", "running")
        started = self.window.runner.run(tr("Legami automatici: coppie"), work, done, self._failed)
        if not started:
            self._busy(False)
            self.set_step("pairs", "todo")
        return started

    def match(self) -> bool:
        """Step 2 for the ticked pairs, in the worker. Returns whether it started."""
        todo = self.checked_pairs()
        if not todo:
            self.window._warn(tr("Spunta le coppie da abbinare."))
            return False
        doc = self.doc
        source = self.source.currentData()
        margin = None if self.whole.isChecked() else float(self.margin.value())
        placed = placement(doc)

        def work(p):
            return auto_ties.run(doc, todo, source=source, margin=margin, progress=p)

        def done(results):
            if self._gone(doc):
                return
            self._busy(False)
            if placement(self.doc) != placed:  # moved while it ran
                self.set_step("match", "stale")
                return
            for r in results:
                self.results[(r.a, r.b)] = r
                self.window.log_pane.info(auto_ties.report_text(r, "it" if i18n.current() == "it" else "en"))
            self._placed = placed
            self.set_step("match", "done")
            self.fill()
            self.check_pairs([k for k, r in self.results.items() if r.ok])

        self._busy(True)
        self.set_step("match", "running")
        started = self.window.runner.run(tr("Legami automatici: abbinamento"), work, done, self._failed)
        if not started:
            self._busy(False)
            self.set_step("match", "todo")
        return started

    def accept(self) -> int:
        """Step 3: the pairs proposed and ticked made into ties, one step of Modifica > Annulla. Returns how many ties
        were made."""
        from .ties_dialog import edit_state, error_text

        chosen = [self.results[k] for k in self.checked_pairs() if k in self.results and self.results[k].ok]
        if not chosen:
            self.window._warn(tr("Spunta le coppie proposte da accettare."))
            return 0
        before = edit_state(self.doc)
        made = 0
        for r in chosen:
            try:
                made += len(auto_ties.accept(self.doc, r, max_ties=int(self.max_ties.value())))
            except ties_mod.TieError as exc:
                self.window._warn(tr("Legami automatici: {0}", error_text(exc)))
        if not made:
            return 0
        self.dialog._edited(tr("Legami automatici"), before)
        self.dialog.reload(kinds=self.dialog._kinds_with_new())
        self.set_step("accept", "done")
        self.set_step("solve", "todo")
        self.window.log_pane.info(tr("Legami automatici: {0} legami da {1} coppie; ora Controlla e Applica", made,
                                     len(chosen)))
        return made
