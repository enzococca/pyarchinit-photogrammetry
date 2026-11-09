"""*Flusso di lavoro > Aggancia chunk con legami*: the ties between chunks (:mod:`..engine.ties`) in a table - name, the
two ends (or the known coordinates), precision, active, the residual of the last check and why a tie is out - the
unknowns of each chunk (the six kinds, or out of the solve; the active chunk fixed), *Robusto*, an optional straight
axis per chunk (two of its markers), *Controlla* (the solve and its checks in the worker; the report with the suspect
ties in red) and *Applica* (after a check; without redundancy only after its own question, default No), *Rimuovi*,
*Da marker omonimi*, *Importa...*. Not modal: the views stay usable while it is open. Applying is one step of
*Modifica > Annulla* (:class:`TiesStep`)."""
from __future__ import annotations

import contextlib
import copy
import html
import json
import logging

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                               QHBoxLayout, QHeaderView, QLabel, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)

from ..core import crs as crs_mod
from ..engine import ties as ties_mod
from . import i18n
from .dialogs import Field, ParamDialog, choice
from .i18n import tr
from .undo import ScriptChanged

log = logging.getLogger(__name__)

KIND_ORDER = ("fixed", "similarity", "rigid", "plan", "shift", "height")
OUT = ""  # the chunk is not in the solve
RED = "#c0392b"
ORANGE = "#b9770e"
GREY = "#7f8c8d"
# messages of the report that point at a wrong tie or a wrong result (red); the precision warnings are advice
ALARMS = {"no_check", "distance_suspect", "distance_failed", "loo_large", "residual_large", "implausible_rotation",
          "implausible_scale", "axis_bent", "distance_scale", "distance_scale_known"}
ADVICE = {"precision_spread", "precision_residuals", "precision_inherited", "not_levelled", "tilt_scale_kept",
          "local_references", "approximated", "robust_unsettled"}


def alive(obj) -> bool:
    """``obj`` is a Qt object not deleted yet (a dialog closed with WA_DeleteOnClose is gone in C++ while its Python
    wrapper stays)."""
    import shiboken6

    return obj is not None and shiboken6.isValid(obj)


def undo_shortcuts(dialog, window) -> None:
    """Ctrl+Z / Ctrl+Shift+Z in a window of its own (not modal) undo and redo in the main window: the steps it makes
    are steps of *Modifica > Annulla*."""
    from PySide6.QtGui import QKeySequence, QShortcut

    for keys, slot in ((QKeySequence.StandardKey.Undo, window.undo), (QKeySequence.StandardKey.Redo, window.redo)):
        QShortcut(keys, dialog).activated.connect(slot)


def say(m: dict) -> str:
    """A message of the engine (:func:`..engine.ties.message`) in the language of the interface."""
    args = [ties_mod.Comma(a) if i18n.current() == "it" and isinstance(a, float) else a for a in m["args"]]
    return tr(m["it"], *args)


def error_text(exc: Exception) -> str:
    """A :class:`..engine.ties.TieError` (or another error of the solve) in the language of the interface."""
    if isinstance(exc, ties_mod.TieError):
        return exc.italian if i18n.current() == "it" else exc.english
    return str(exc.args[0] if isinstance(exc, KeyError) and exc.args else exc)


def kind_name(kind: str) -> str:
    return tr(ties_mod.MESSAGES["unknowns_" + kind][0])


def _held(chunk) -> bool:
    return not crs_mod.is_local(chunk.crs) and ties_mod.held_by_references(chunk)


def default_kinds(doc, active=None) -> dict[int, str]:
    """The unknowns proposed for each chunk: the active one fixed (else the first chunk with ties), a chunk without
    usable ties out of the solve, *in pianta* for a chunk georeferenced by its own references or with 2-3 ties, else
    *similitudine*."""
    counts: dict[int, int] = {}
    for t in ties_mod.ties_of(doc):
        if t.active and not t.broken:
            for k in t.chunk_keys():
                counts[k] = counts.get(k, 0) + 1
    fixed = active.key if active is not None and active in doc.chunks else \
        next((c.key for c in doc.chunks if counts.get(c.key)), None)
    out = {}
    for ch in doc.chunks:
        n = counts.get(ch.key, 0)
        if ch.key == fixed:
            out[ch.key] = "fixed"
        elif not n:
            out[ch.key] = OUT
        elif _held(ch) or n <= 3:
            out[ch.key] = "plan"
        else:
            out[ch.key] = "similarity"
    return out


def check(doc, unknowns: dict, robust: bool, references: bool, axes: dict, progress=None) -> dict:
    """The solve and its checks (in the worker): ``{"solution", "checks", "loo", "warnings", "axis", "report",
    "axis_error"}``, or ``{"error": TieError}`` when the solve cannot be made."""
    step = progress or (lambda *_a, **_k: None)
    try:
        step(0.05, tr("Stima dei legami"))
        solution = ties_mod.solve(doc, unknowns, references=references, robust=robust)
        step(0.3, tr("Distanze fra i legami"))
        checks = ties_mod.distance_checks(doc)
        step(0.4, tr("Prova a togliere un legame per volta"))
        loo = ties_mod.leave_one_out(doc, unknowns, references=references, robust=robust, solution=solution)
        warnings = ties_mod.plausibility(doc, solution)
    except (ties_mod.TieError, ValueError, KeyError) as exc:
        return {"error": exc}
    axis, axis_error = None, None
    if len(axes) >= 2:
        try:
            axis = ties_mod.axis_check(doc, solution, axes)
        except ties_mod.TieError as exc:
            axis_error = exc
    elif axes:
        axis_error = ties_mod.TieError("a straight axis needs its direction in two chunks at least",
                                       "un asse dritto richiede la sua direzione in almeno due chunk")
    step(1.0, tr("Rapporto"))
    return {"solution": solution, "checks": checks, "loo": loo, "warnings": warnings, "axis": axis,
            "axis_error": axis_error, "report": ties_mod.report(solution, checks, loo, warnings, axis)}


def report_html(result: dict) -> str:
    """The report of a check (:func:`check`) as HTML: the solve, the chunks, the messages (alarms in red, advice in
    orange, what was left out in grey), the residual of each tie (suspects in red)."""
    rep = result["report"]

    def line(text: str, colour: str | None = None, bold: bool = False) -> str:
        t = html.escape(text)
        if bold:
            t = f"<b>{t}</b>"
        return f'<div style="color:{colour}">{t}</div>' if colour else f"<div>{t}</div>"

    def m(code: str, *args) -> str:
        return say(ties_mod.message(code, *args))

    out = [line(m("line_system", rep["crs"] or "LOCAL")),
           line(m("line_redundancy", rep["redundancy"]), RED if rep["redundancy"] <= 0 else None,
                bold=rep["redundancy"] <= 0)]
    out.append(line(m("line_fit_no_sigma", float(rep["rms"])) if rep["sigma0"] is None else
                    m("line_fit", float(rep["rms"]), float(rep["sigma0"]))))
    alarms = [x for x in rep["messages"] if x["code"] in ALARMS]
    advice = [x for x in rep["messages"] if x["code"] in ADVICE]
    left = [x for x in rep["messages"] if x["code"].startswith("out_")]
    others = [x for x in rep["messages"] if x not in alarms and x not in advice and x not in left]
    if alarms or others:
        out.append("<br>" + line(tr("Avvisi"), bold=True))
        out += [line(say(x), RED) for x in alarms] + [line(say(x)) for x in others]
    if result.get("axis_error") is not None:
        out.append(line(error_text(result["axis_error"]), ORANGE))
    if advice:
        out.append("<br>" + line(tr("Consigli"), bold=True))
        out += [line(say(x), ORANGE) for x in advice]
    out.append("<br>" + line(tr("Chunk della stima"), bold=True))
    for c in rep["chunks"]:
        out.append(line(m("line_chunk", c["label"], kind_name(c["kind"]), float(c["rotation_deg"]), float(c["shift"]),
                          float(c["scale"]), float(c["precision_m"]))))
    out.append("<br>" + line(tr("Residui"), bold=True))
    suspect = set(rep["suspects"])
    loo = rep["loo"]
    for r in rep["residuals"]:
        kind = tr(ties_mod.MESSAGES["kind_check" if r["check"] else "kind_" + r["kind"]][0])
        value = loo.get(r["id"]) if r["id"] is not None else None
        gone = tr("-") if value is None else i18n.number(value, 3) + " m"
        bad = r["id"] in suspect or (r.get("fitted") and r["normalised"] > ties_mod.CHECK_FACTOR)
        out.append(line(m("line_residual", r["name"], kind, float(np.hypot(r["dx"], r["dy"])), float(r["dz"]),
                          float(r["precision"]), gone), RED if bad else None, bold=r["id"] in suspect))
    comparable = [c for c in result["checks"] if c.get("ok") is not None]
    if comparable:
        out.append("<br>" + line(m("line_distances", sum(1 for c in comparable if c["ok"]), len(comparable)),
                                 RED if any(not c["ok"] for c in comparable) else None))
    axis = result.get("axis")
    if axis is not None:
        out.append(line(m("line_axis_no_before", float(axis["max_deg"])) if axis["max_before_deg"] is None else
                        m("line_axis", float(axis["max_deg"]), float(axis["max_before_deg"])),
                        None if axis["ok"] else RED))
    if left:
        out.append("<br>" + line(tr("Esclusi"), bold=True))
        out += [line(say(x), GREY) for x in left]
    return "".join(out)


class TiesChanged(ScriptChanged):
    """The ties, their markers or their chunks changed after a step of the ties with a change that is not in the
    history: undoing it now would take back half of it. The step leaves the history instead, nothing restored."""

    ties = True


def edit_state(doc, keys=None) -> dict:
    """What an edit of the ties may change: the ties (``doc.meta["ties"]``) and the markers of the chunks ``keys``
    (default every chunk; the ends of the ties are markers, a known tie keeps its coordinates in its marker's
    reference). Deep copies: nothing is shared with the live markers."""
    return {"ties": copy.deepcopy(doc.meta.get(ties_mod.META_KEY)),
            "markers": {ch.key: copy.deepcopy([m.to_dict() for m in ch.markers]) for ch in doc.chunks
                        if keys is None or ch.key in keys}}


def _restrict(state: dict, keys) -> dict:
    return {"ties": state["ties"], "markers": {k: v for k, v in state["markers"].items() if k in keys}}


def fingerprint(doc) -> str:
    """The ties, the markers and the place of every chunk, as text: a check holds while it does not change."""
    state = edit_state(doc)
    state["chunks"] = {ch.key: [np.asarray(ch.transform, float).tolist(), ch.crs] for ch in doc.chunks}
    return json.dumps(state, sort_keys=True, default=str)


class TiesEditStep:
    """An edit of the ties (import, from markers, remove, active, precision) as one step of *Modifica > Annulla*: the
    ties and the markers of the chunks the edit changed, before and after it (:func:`edit_state`). Undo and redo first
    check that the ties and those chunks are as the step left them (else :class:`TiesChanged`: the history drops the
    step; a change in another chunk does not count), then write the other side back - the ties and their markers
    together."""

    def __init__(self, window, before: dict, after: dict, tag=None):
        self.window, self.before, self.after, self.tag = window, before, after, tag

    def _put(self, expected: dict, state: dict) -> None:
        from ..core.marker import Marker

        doc = self.window.doc
        if edit_state(doc, set(expected["markers"])) != expected:
            raise TiesChanged("the ties or the markers changed after the edit of the ties")
        if state["ties"]:
            doc.meta[ties_mod.META_KEY] = copy.deepcopy(state["ties"])
        else:
            doc.meta.pop(ties_mod.META_KEY, None)
        for ch in doc.chunks:
            if ch.key in state["markers"]:
                ch.markers = [Marker.from_dict(d) for d in copy.deepcopy(state["markers"][ch.key])]
        self.window._ties_edited()

    def undo(self) -> None:
        self._put(self.after, self.before)

    def redo(self) -> None:
        self._put(self.before, self.after)


def record_edit(window, text: str, before: dict, tag=None) -> bool:
    """The ties changed from ``before`` (:func:`edit_state`, taken before the edit): one step of Modifica > Annulla
    for the ties and the markers of the chunks the edit changed (:class:`TiesEditStep`; edits of the same ``tag`` in a
    row - the precision of one tie - make one step), the project modified. Returns whether something changed. Used by
    the dialog and by the view *Punti in comune* (ties made by two clicks, ends on photos, an end moved)."""
    after = edit_state(window.doc)
    if after == before:
        return False
    window.setWindowModified(True)
    history = window.history
    if history is None:
        return True
    keys = {k for k in after["markers"] if after["markers"][k] != before["markers"].get(k)}
    last = history.entries[-1] if history.entries and history.index == len(history.entries) else None
    step = getattr(last, "action", None)
    if (tag is not None and isinstance(step, TiesEditStep) and step.tag == tag
            and step.after == _restrict(before, set(step.after["markers"]))):
        # the same edit going on (the arrows of a precision): the step takes the new state
        for k in keys - set(step.before["markers"]):
            step.before["markers"][k] = before["markers"][k]
        step.after = _restrict(after, set(step.before["markers"]))
        history.rebase()
        return True
    history.push(text, TiesEditStep(window, _restrict(before, keys), _restrict(after, keys), tag), document=True)
    return True


def keep_report(doc, report: dict | None) -> None:
    """The report of the checks (distances, leave one out, suspects, axis) kept with the solve applied in
    ``doc.meta["ties_report"]`` (JSON)."""
    if report is not None and isinstance(doc.meta.get("ties_report"), dict):
        doc.meta["ties_report"]["checks"] = json.loads(json.dumps(report, default=float))


class TiesStep:
    """A solve applied (:func:`..engine.ties.apply`) as one step of *Modifica > Annulla*: undone, what it changed is
    put back (:func:`..engine.ties.restore`: transforms, systems, references, the last report); done again, the same
    solve applied again. Each first checks that the chunks are as the step left them: when something moved them
    meanwhile it raises and the history drops the step - never a transform of before put back on a chunk aligned
    since."""

    def __init__(self, window, solution, changed: dict, report: dict | None = None):
        self.window, self.solution, self.changed, self.report = window, solution, changed, report
        self.expected = self._now()

    def _chunks(self) -> dict:
        return {c.key: c for c in self.window.doc.chunks}

    def _now(self) -> dict:
        chunks = self._chunks()
        return {k: (None if k not in chunks else (np.asarray(chunks[k].transform, float).tolist(), chunks[k].crs))
                for k in self.solution.previous}

    def _check(self) -> None:
        if self._now() != self.expected:
            raise TiesChanged("the chunks of the ties changed after the solve was applied")

    def undo(self) -> None:
        self._check()
        ties_mod.restore(self.window.doc, self.changed)
        self.expected = self._now()
        self.window._ties_reload()

    def redo(self) -> None:
        self._check()
        self.changed = ties_mod.apply(self.window.doc, self.solution)
        with contextlib.suppress(TypeError, ValueError):  # the report only: the solve is applied again
            keep_report(self.window.doc, self.report)
        self.expected = self._now()
        self.window._ties_reload()


class TiesDialog(QDialog):
    """The ties of the project, the unknowns per chunk, the check and the apply (see the module)."""

    COLS = ("active", "name", "a", "b", "precision", "residual", "state")

    @classmethod
    def ask(cls, window) -> "TiesDialog":
        dlg = cls(window)
        dlg.show()
        return dlg

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setModal(False)
        self.setWindowTitle(tr("Aggancia chunk con legami"))
        self.resize(1100, 760)
        self.result: dict | None = None  # the last check, while nothing changed after it
        self._generation = 0  # bumped by every change: a check started before one is not taken
        self._fingerprint: str | None = None  # the project when the last check started (fingerprint)
        self._filling = False

        lay = QVBoxLayout(self)
        intro = QLabel(tr("Un legame è lo stesso punto in due chunk (due marker), o un marker con coordinate note. "
                          "Tutti i chunk scelti vengono stimati insieme; il risultato cambia solo la loro "
                          "trasformazione. Controlla prima di applicare: i legami sospetti sono in rosso."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        split = QSplitter(Qt.Orientation.Vertical)
        lay.addWidget(split, 1)

        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(QLabel("<b>" + html.escape(tr("Legami")) + "</b>"))
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels([tr("Attivo"), tr("Nome"), tr("Chunk A"), tr("Chunk B / coordinate"),
                                              tr("Precisione (m)"), tr("Residuo (m)"), tr("Stato")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        tl.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.b_remove = QPushButton(tr("Rimuovi"))
        self.b_from_markers = QPushButton(tr("Da marker omonimi..."))
        self.b_import = QPushButton(tr("Importa..."))
        self.b_points = QPushButton(tr("Punti in comune..."))
        self.b_points.setToolTip(tr("Due viste affiancate di due chunk, collegate: un legame con un clic in ognuna"))
        for b in (self.b_remove, self.b_from_markers, self.b_import, self.b_points):
            row.addWidget(b)
        row.addStretch(1)
        tl.addLayout(row)

        tl.addWidget(QLabel("<b>" + html.escape(tr("Incognite per chunk")) + "</b>"))
        self.chunks = QTableWidget(0, 4)
        self.chunks.setHorizontalHeaderLabels([tr("Chunk"), tr("Incognite"), tr("Asse: da marker"),
                                               tr("Asse: a marker")])
        self.chunks.verticalHeader().setVisible(False)
        self.chunks.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.chunks.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.chunks.horizontalHeader().setStretchLastSection(True)
        tl.addWidget(self.chunks)
        opts = QHBoxLayout()
        self.c_robust = QCheckBox(tr("Robusto"))
        self.c_robust.setToolTip(tr("Pesi di Huber: un legame sbagliato pesa meno (la stima si ripete finché i pesi "
                                    "non cambiano più)"))
        self.c_references = QCheckBox(tr("Usa i riferimenti dei marker"))
        self.c_references.setChecked(True)
        self.c_references.setToolTip(tr("I marker di controllo dei chunk (con coordinate) contano come punti noti"))
        opts.addWidget(self.c_robust)
        opts.addWidget(self.c_references)
        opts.addStretch(1)
        tl.addLayout(opts)
        split.addWidget(top)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(QLabel("<b>" + html.escape(tr("Rapporto")) + "</b>"))
        self.report = QTextBrowser()
        self.report.setPlaceholderText(tr("Premi Controlla: la stima e i controlli, niente viene applicato"))
        bl.addWidget(self.report, 1)
        split.addWidget(bottom)

        buttons = QHBoxLayout()
        self.b_check = QPushButton(tr("Controlla"))
        self.b_apply = QPushButton(tr("Applica"))
        self.b_apply.setEnabled(False)
        self.b_close = QPushButton(tr("Chiudi"))
        buttons.addStretch(1)
        for b in (self.b_check, self.b_apply, self.b_close):
            buttons.addWidget(b)
        lay.addLayout(buttons)

        self.b_check.clicked.connect(self.run_check)
        self.b_apply.clicked.connect(self.apply)
        self.b_close.clicked.connect(self.close)
        self.b_remove.clicked.connect(self.remove_selected)
        self.b_from_markers.clicked.connect(self.from_markers)
        self.b_import.clicked.connect(self.import_table)
        self.b_points.clicked.connect(lambda: self.window.common_points())
        self.table.itemChanged.connect(self._item_changed)
        self.c_robust.toggled.connect(lambda _on: self.invalidate())
        self.c_references.toggled.connect(lambda _on: self.invalidate())
        self.finished.connect(self._forget)  # Esc / reject: Qt closes the dialog without a closeEvent
        undo_shortcuts(self, window)
        self.reload(kinds=default_kinds(window.doc, window.chunk))

    # ------------------------------------------------------------------ contents
    @property
    def doc(self):
        return self.window.doc

    def _end_text(self, end, uid) -> str:
        found = ties_mod.resolve_end(self.doc, end, uid)
        if found is None:
            return tr("chunk {0}, marker {1} (non c'è più)", end[0], end[1])
        return f"{found[0].label} / {found[1].label}"

    def reload(self, kinds: dict | None = None) -> None:
        """The tables from the project (the unknowns chosen kept, or ``kinds``); the last check dropped."""
        kinds = dict(kinds) if kinds is not None else self.kinds(include_out=True)
        axes = self._axis_choices()
        self._filling = True
        try:
            ties = ties_mod.ties_of(self.doc)
            self.table.setRowCount(len(ties))
            for i, t in enumerate(ties):
                active = QTableWidgetItem()
                active.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                                | (Qt.ItemFlag.NoItemFlags if t.broken else Qt.ItemFlag.ItemIsUserCheckable))
                active.setCheckState(Qt.CheckState.Checked if t.active else Qt.CheckState.Unchecked)
                active.setData(Qt.ItemDataRole.UserRole, t.id)
                self.table.setItem(i, 0, active)
                self.table.setItem(i, 1, QTableWidgetItem(t.name))
                self.table.setItem(i, 2, QTableWidgetItem(self._end_text(t.a, t.a_uid)))
                if t.b is None:
                    known = "-" if t.known is None else i18n.decimal_comma(", ".join(f"{v:.3f}" for v in t.known))
                    b = tr("coordinate note {0} ({1})", known, t.crs or "LOCAL")
                else:
                    b = self._end_text(t.b, t.b_uid)
                self.table.setItem(i, 3, QTableWidgetItem(b))
                spin = QDoubleSpinBox()
                spin.setDecimals(3)
                spin.setRange(0.001, 1000.0)
                spin.setSingleStep(0.005)
                spin.setValue(float(t.precision))
                spin.setEnabled(not t.broken)
                spin.setProperty("tie_id", t.id)
                spin.valueChanged.connect(lambda value, tid=t.id: self._precision_changed(tid, value))
                self.table.setCellWidget(i, 4, spin)
                self.table.setItem(i, 5, QTableWidgetItem(""))
                self.table.setItem(i, 6, QTableWidgetItem(self._state_text(t)))
                if t.broken:
                    for c in range(len(self.COLS)):
                        item = self.table.item(i, c)
                        if item is not None:
                            item.setForeground(QColor(GREY))
            self.chunks.setRowCount(len(self.doc.chunks))
            for i, ch in enumerate(self.doc.chunks):
                name = QTableWidgetItem(ch.label + (" " + tr("(attivo)") if ch is self.window.chunk else ""))
                name.setData(Qt.ItemDataRole.UserRole, ch.key)
                self.chunks.setItem(i, 0, name)
                combo = QComboBox()
                combo.addItem(tr("non nella stima"), OUT)
                for kind in KIND_ORDER:
                    combo.addItem(f"{kind_name(kind)} ({ties_mod.KINDS[kind]})", kind)
                combo.setCurrentIndex(max(0, combo.findData(kinds.get(ch.key, OUT))))
                combo.currentIndexChanged.connect(lambda _i: self.invalidate())
                self.chunks.setCellWidget(i, 1, combo)
                for col in (2, 3):
                    mc = QComboBox()
                    mc.addItem(tr("-"), None)
                    for m in ch.markers:
                        mc.addItem(m.label, m.key)
                    chosen = axes.get(ch.key, (None, None))[col - 2]
                    mc.setCurrentIndex(max(0, mc.findData(chosen)))
                    mc.currentIndexChanged.connect(lambda _i: self.invalidate())
                    self.chunks.setCellWidget(i, col, mc)
        finally:
            self._filling = False
        self._update_buttons()
        self.invalidate()

    def _state_text(self, t) -> str:
        if t.broken:
            return "; ".join(say(ties_mod.message(code, t.name, *args)) for code, *args in (t.broken_parts or [
                ("out_broken",)]))
        return tr("attivo") if t.active else tr("non attivo")

    def _axis_choices(self) -> dict:
        out = {}
        for i in range(self.chunks.rowCount()):
            item, a, b = self.chunks.item(i, 0), self.chunks.cellWidget(i, 2), self.chunks.cellWidget(i, 3)
            if item is not None and a is not None and b is not None:
                out[item.data(Qt.ItemDataRole.UserRole)] = (a.currentData(), b.currentData())
        return out

    def kinds(self, include_out: bool = False) -> dict[int, str]:
        """The unknowns chosen: chunk key -> kind (without the chunks out of the solve, unless ``include_out``)."""
        out = {}
        for i in range(self.chunks.rowCount()):
            item, combo = self.chunks.item(i, 0), self.chunks.cellWidget(i, 1)
            if item is None or combo is None:
                continue
            kind = combo.currentData()
            if kind != OUT or include_out:
                out[item.data(Qt.ItemDataRole.UserRole)] = kind
        return out

    def set_kind(self, chunk_key: int, kind: str) -> None:
        for i in range(self.chunks.rowCount()):
            if self.chunks.item(i, 0).data(Qt.ItemDataRole.UserRole) == chunk_key:
                combo = self.chunks.cellWidget(i, 1)
                combo.setCurrentIndex(combo.findData(kind))

    def set_axis(self, chunk_key: int, marker_a: int | None, marker_b: int | None) -> None:
        for i in range(self.chunks.rowCount()):
            if self.chunks.item(i, 0).data(Qt.ItemDataRole.UserRole) == chunk_key:
                for col, key in ((2, marker_a), (3, marker_b)):
                    mc = self.chunks.cellWidget(i, col)
                    mc.setCurrentIndex(max(0, mc.findData(key)))

    def axes(self) -> dict:
        """chunk key -> the internal points of its axis (both markers chosen, placed, different), for the chunks in
        the solve."""
        chunks = {c.key: c for c in self.doc.chunks}
        kinds = self.kinds()
        out = {}
        for key, (a, b) in self._axis_choices().items():
            if key not in kinds or a is None or b is None or a == b or key not in chunks:
                continue
            ch = chunks[key]
            ma = next((m for m in ch.markers if m.key == a), None)
            mb = next((m for m in ch.markers if m.key == b), None)
            pa = None if ma is None else ties_mod.end_internal(ch, ma)
            pb = None if mb is None else ties_mod.end_internal(ch, mb)
            if pa is not None and pb is not None:
                out[key] = (pa, pb)
        return out

    def row_of(self, tie_id: str) -> int:
        for i in range(self.table.rowCount()):
            if self.table.item(i, 0).data(Qt.ItemDataRole.UserRole) == tie_id:
                return i
        return -1

    # ------------------------------------------------------------------ edits of the ties
    def _edited(self, text: str, before: dict, tag=None) -> None:
        """The ties changed from ``before`` (:func:`edit_state`): one step of Modifica > Annulla
        (:func:`record_edit`), the last check dropped, the flags of the photo shown and the view *Punti in comune*
        drawn again."""
        self.invalidate()
        with contextlib.suppress(AttributeError, RuntimeError):  # the flags of the photo shown
            self.window.photo_view.draw_markers()
        record_edit(self.window, text, before, tag)
        self._points_redraw()

    def _item_changed(self, item) -> None:
        if self._filling or item.column() != 0:
            return
        tid = item.data(Qt.ItemDataRole.UserRole)
        before = edit_state(self.doc)
        ties = ties_mod.ties_of(self.doc)
        t = next((x for x in ties if x.id == tid), None)
        if t is None or t.broken:
            return
        t.active = item.checkState() == Qt.CheckState.Checked
        ties_mod.save_ties(self.doc, ties)
        self._filling = True
        try:
            self.table.item(self.row_of(tid), 6).setText(self._state_text(t))
        finally:
            self._filling = False
        self._edited(tr("Legame attivo o no"), before)

    def _precision_changed(self, tie_id: str, value: float) -> None:
        if self._filling:
            return
        before = edit_state(self.doc)
        ties = ties_mod.ties_of(self.doc)
        t = next((x for x in ties if x.id == tie_id), None)
        if t is None:
            return
        if t.b is None:
            ties_mod.update_known(self.doc, tie_id, precision=float(value))
        else:
            t.precision = float(value)
            ties_mod.save_ties(self.doc, ties)
        self._edited(tr("Precisione di un legame"), before, tag=("precision", tie_id))

    def selected_ids(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows]

    def remove_selected(self) -> None:
        ids = self.selected_ids()
        if not ids:
            self.window._warn(tr("Scegli nella tabella i legami da rimuovere."))
            return
        before = edit_state(self.doc)
        for tid in ids:
            ties_mod.remove_tie(self.doc, tid)
        self._edited(tr("Rimuovi legami"), before)
        self.window.refresh_all()
        self.reload()

    def from_markers(self) -> None:
        if len(self.doc.chunks) < 2:
            self.window._warn(tr("Servono almeno due chunk."))
            return
        choices = [(ch.key, ch.label) for ch in self.doc.chunks]
        first = self.window.chunk if self.window.chunk is not None else self.doc.chunks[0]
        other = next(ch for ch in self.doc.chunks if ch is not first)
        v = ParamDialog.ask(self, "Da marker omonimi", [
            Field("a", "Primo chunk", first.key, choices=choices),
            Field("b", "Secondo chunk", other.key, choices=choices)],
            intro="I marker con lo stesso nome nei due chunk diventano legami (nessun marker nuovo).")
        if not v:
            return
        if v["a"] == v["b"]:
            self.window._warn(tr("Scegli due chunk diversi."))
            return
        before = edit_state(self.doc)
        try:
            made = ties_mod.ties_from_markers(self.doc, v["a"], v["b"])
        except (ties_mod.TieError, ValueError, KeyError) as exc:
            self.window._warn(error_text(exc))
            return
        self.window.log_pane.info(tr("Legami dai marker omonimi: {0}", len(made)))
        if made:
            self._edited(tr("Legami dai marker omonimi"), before)
            self.reload(kinds=self._kinds_with_new())
        else:
            self.window._info(tr("Da marker omonimi"), tr("Nessun marker con lo stesso nome nei due chunk, o già legati."))

    def import_table(self, path: str | None = None) -> None:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, tr("Legami da una tabella"), self.window._last_dir(),
                                                  tr("Tabelle (*.csv *.txt)"))
            if not path:
                return
        before = edit_state(self.doc)
        try:
            made = ties_mod.import_ties(self.doc, path)
        except (ties_mod.TieError, ValueError, KeyError, OSError) as exc:
            self.window._warn(tr("Tabella dei legami: {0}", error_text(exc)))
            return
        self.window.log_pane.info(tr("Legami importati: {0}", len(made)))
        if made:
            self._edited(tr("Importa legami"), before)
            self.window.refresh_all()
            self.reload(kinds=self._kinds_with_new())

    def _kinds_with_new(self) -> dict:
        """The unknowns chosen, with the proposal for the chunks that were out of the solve and now have ties."""
        kinds = self.kinds(include_out=True)
        proposed = default_kinds(self.doc, self.window.chunk)
        for key, kind in kinds.items():
            if kind == OUT:
                kinds[key] = proposed.get(key, OUT)
        for key, kind in proposed.items():
            kinds.setdefault(key, kind)
        return kinds

    # ------------------------------------------------------------------ check and apply
    def invalidate(self) -> None:
        """Something changed: the last check no longer holds (Applica off, residuals cleared)."""
        self._generation += 1
        if self.result is None and not self.b_apply.isEnabled():
            return
        self.result = None
        self.b_apply.setEnabled(False)
        self._filling = True
        try:
            for i in range(self.table.rowCount()):
                item = self.table.item(i, 5)
                if item is not None:
                    item.setText("")
        finally:
            self._filling = False
        if self.report.toPlainText():
            self.report.setHtml('<div style="color:%s">%s</div>' % (GREY, html.escape(
                tr("Il rapporto non vale più: qualcosa è cambiato dopo il controllo. Premi Controlla."))))
        self._points_redraw()

    def _points_redraw(self) -> None:
        """The residuals of the view *Punti in comune* follow the check (shown, or gone with it)."""
        view = getattr(self.window, "_common_points", None)
        if alive(view):
            view.redraw()

    def _update_buttons(self, busy: bool = False) -> None:
        for b in (self.b_check, self.b_remove, self.b_from_markers, self.b_import):
            b.setEnabled(not busy)
        self.b_apply.setEnabled(not busy and self.result is not None)

    def run_check(self) -> bool:
        """The solve and its checks in the worker; the report shown when they finish (:meth:`_checked`)."""
        unknowns = self.kinds()
        if not unknowns:
            self.window._warn(tr("Scegli le incognite di almeno un chunk."))
            return False
        robust, references, axes = self.c_robust.isChecked(), self.c_references.isChecked(), self.axes()
        doc = self.doc
        self.invalidate()
        generation, self._fingerprint = self._generation, fingerprint(doc)
        self._update_buttons(busy=True)
        started = self.window.runner.run(tr("Controllo dei legami"),
                                         lambda p: check(doc, unknowns, robust, references, axes, p),
                                         lambda result: self._checked(result, generation), self._failed)
        if not started:
            self._update_buttons()
        return started

    def _failed(self, summary: str, details: str = "") -> None:
        self._update_buttons()
        self.window._error(summary, details)

    def _checked(self, result: dict, generation: int | None = None) -> None:
        try:
            self.isVisible()
        except RuntimeError:  # the dialog was closed meanwhile
            return
        if generation is not None and generation != self._generation:  # something changed while it ran
            self._update_buttons()
            self.invalidate()
            return
        if "error" in result:
            self.result = None
            self._update_buttons()
            self.report.setHtml('<div style="color:%s"><b>%s</b></div>' % (RED, html.escape(
                error_text(result["error"]))))
            return
        self.result = result
        rep = result["report"]
        suspect = set(rep["suspects"])
        rows = {r["id"]: r for r in rep["ties"]}
        left = {x["id"]: x for x in rep["left_out"] if x.get("id")}
        self._filling = True
        try:
            for i in range(self.table.rowCount()):
                tid = self.table.item(i, 0).data(Qt.ItemDataRole.UserRole)
                r = rows.get(tid)
                self.table.item(i, 5).setText("" if r is None else i18n.number(r["d"], 3))
                if tid in left:
                    self.table.item(i, 6).setText("; ".join(say(x) for x in left[tid].get("messages") or []))
                bad = tid in suspect or (r is not None and r["fitted"] and r["normalised"] > ties_mod.CHECK_FACTOR)
                colour = QColor(RED) if bad else QColor(GREY) if tid in left else None
                for c in range(len(self.COLS)):
                    item = self.table.item(i, c)
                    if item is not None and colour is not None:
                        item.setForeground(colour)
                    elif item is not None:
                        item.setData(Qt.ItemDataRole.ForegroundRole, None)
        finally:
            self._filling = False
        self.report.setHtml(report_html(result))
        self._update_buttons()
        self._points_redraw()

    def apply(self) -> bool:
        """The last check applied (one step of Modifica > Annulla). Without redundancy only after its own question
        (default No). Returns whether it was applied."""
        if self.result is None:
            return False
        if self._fingerprint is not None and fingerprint(self.doc) != self._fingerprint:
            self.invalidate()
            self.window._warn(tr("I legami, i marker o i chunk sono cambiati dopo il controllo: premi di nuovo "
                                 "Controlla."))
            return False
        solution = self.result["solution"]
        moving = [solution.per_chunk[k]["label"] for k, kind in solution.unknowns.items() if kind != "fixed"]
        if not moving:
            self.window._info(tr("Aggancia chunk con legami"), tr("Niente da applicare: tutti i chunk sono fissi."))
            return False
        if solution.redundancy <= 0:
            if choice(self, "warning", tr("Nessun controllo"),
                      tr("Gradi di libertà in avanzo 0: nessun controllo, un legame sbagliato non si vede. Applicare "
                         "comunque la stima a {0}?", ", ".join(moving)),
                      [tr("Applica senza controllo"), tr("Non applicare")], default=1, cancel=None) != 0:
                return False
        return self.window.apply_ties(solution, self.result["report"])

    def _forget(self, *_args) -> None:
        if getattr(self.window, "_ties_dialog", None) is self:
            self.window._ties_dialog = None

    def closeEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        self._forget()
        super().closeEvent(ev)


__all__ = ["alive", "undo_shortcuts", "TiesDialog", "TiesStep", "TiesEditStep", "TiesChanged", "edit_state", "record_edit", "fingerprint", "keep_report", "check", "report_html", "default_kinds", "say", "error_text"]
