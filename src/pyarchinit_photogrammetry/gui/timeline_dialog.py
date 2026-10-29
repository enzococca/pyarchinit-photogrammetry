"""Strumenti > Fasi dello scavo: the phases of the excavation (chunks by date), what was dug away
from one to the next and the stratigraphic relations that the order of removal suggests
(:mod:`archaeo.timeline`). A window that stays open beside the main one: a double click on a
phase shows it, its date can be corrected, the relations go into the US forms of PyArchInit.
The DEMs of the phases can be coregistered before the volumes (:mod:`archaeo.coreg`, on by default
with more than one phase): the tab *Coregistrazione* gives the shift of each phase."""
from __future__ import annotations

import math
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QHBoxLayout, QHeaderView, QLabel, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout)

from ..archaeo import timeline
from .i18n import number, tr


def _item(text, value=None, editable: bool = False, right: bool = False) -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    if value is not None:
        it.setData(Qt.ItemDataRole.UserRole, value)
    if right:
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if not editable:
        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return it


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels([tr(h) for h in headers])
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


class TimelineDialog(QDialog):
    """See the module docstring; ``recompute`` and ``write`` are asked of the main window."""

    phase_chosen = Signal(int)  # key of the chunk to show
    date_changed = Signal(int, str)  # key of the chunk, ISO date ("" = from the photos)
    recompute_requested = Signal()
    write_requested = Signal(list)  # the relations ticked

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Fasi dello scavo"))
        self.setMinimumSize(720, 460)
        self.report: dict = {}
        lay = QVBoxLayout(self)
        intro = QLabel(tr("Ogni chunk con un DEM è una fase dello scavo, nell'ordine delle date (dalle foto, "
                          "oppure scritta qui). Le unità disegnate su una fase erano in luce allora e sono state "
                          "scavate prima della fase seguente: si misura quanto è stato asportato e, dove si "
                          "sovrappongono in pianta, si propone che coprano quelle messe in luce dopo."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        row = QHBoxLayout()
        self.coregister = QCheckBox(tr("Coregistra i DEM delle fasi prima dei volumi"))
        self.coregister.setToolTip(tr("Rilievi di giorni diversi non sono mai registrati alla perfezione: uno "
                                      "spostamento di qualche centimetro o una leggera inclinazione fra due DEM "
                                      "diventano falso scavo e riporto su ogni pendio. Ogni fase viene spostata sul "
                                      "terreno che non è cambiato (le unità disegnate restano fuori); i DEM del "
                                      "progetto non cambiano. Ricalcola per applicare la scelta."))
        self.coregister_to = QComboBox()
        self.coregister_to.addItem(tr("sulla prima fase"), "first")
        self.coregister_to.addItem(tr("sulla fase precedente"), "previous")
        self.coregister.toggled.connect(self.coregister_to.setEnabled)
        row.addWidget(self.coregister)
        row.addWidget(self.coregister_to)
        row.addStretch(1)
        lay.addLayout(row)
        self.tabs = QTabWidget()
        self.phases = _table(["Fase", "Data", "Unità disegnate"])
        self.phases.itemDoubleClicked.connect(self._phase_clicked)
        self.phases.itemChanged.connect(self._date_edited)
        self.steps = _table(["Dalla fase", "Alla fase", "Unità", "Asportato (m3)", "Riportato (m3)", "Area (m2)"])
        self.relations = _table(["Scrivi", "US sopra", "Rapporto", "US sotto", "Sovrapposizione (m2)", "Fasi"])
        self.shifts = _table(["Fase", "Coregistrata su", "dx (cm)", "dy (cm)", "dz (cm)", "Inclinazione (mm/m)",
                              "NMAD prima (cm)", "NMAD dopo (cm)", "Celle stabili", "Note"])
        from .table_export import install_excel_menu

        for table, name in ((self.phases, tr("Fasi")), (self.steps, tr("Volumi asportati")),
                            (self.relations, tr("Rapporti suggeriti")), (self.shifts, tr("Coregistrazione"))):
            install_excel_menu(table, name)
        self.tabs.addTab(self.phases, tr("Fasi"))
        self.tabs.addTab(self.steps, tr("Volumi asportati"))
        self.tabs.addTab(self.relations, tr("Rapporti suggeriti"))
        self.tabs.addTab(self.shifts, tr("Coregistrazione"))
        lay.addWidget(self.tabs, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.close)
        self.b_recompute = bb.addButton(tr("Ricalcola"), QDialogButtonBox.ButtonRole.ActionRole)
        self.b_recompute.clicked.connect(self.recompute_requested)
        self.b_csv = bb.addButton(tr("Salva CSV..."), QDialogButtonBox.ButtonRole.ActionRole)
        self.b_csv.clicked.connect(self._save_csv)
        self.b_write = bb.addButton(tr("Scrivi i rapporti nelle schede US"), QDialogButtonBox.ButtonRole.ActionRole)
        self.b_write.clicked.connect(self._write)
        lay.addWidget(bb)

    # ------------------------------------------------------------ coregistration
    def set_coregistration_default(self, phases: int) -> None:
        """The choice of the first computation: coregistered when there is more than one phase."""
        self.coregister.setChecked(phases > 1)
        self.coregister_to.setEnabled(phases > 1)

    def coregistration_options(self) -> dict:
        """The keywords of :func:`archaeo.timeline.timeline` chosen in the window."""
        return {"coregister": self.coregister.isChecked(), "reference": self.coregister_to.currentData() or "first"}

    def _fill_shifts(self, report: dict) -> None:
        from .table_export import RAW_ROLE

        self.shifts.setRowCount(0)
        if not report.get("coregistration"):
            return
        for p in report["phases"]:
            c = p.get("coregistration")
            r = self.shifts.rowCount()
            self.shifts.insertRow(r)
            self.shifts.setItem(r, 0, _item(p["label"], p["key"]))
            if c is None:  # the reference of the others
                self.shifts.setItem(r, 1, _item(tr("riferimento")))
                continue
            self.shifts.setItem(r, 1, _item(c["reference"]))
            if c.get("error"):
                self.shifts.setItem(r, 9, _item(tr("non coregistrata: {0}", c["error"])))
                continue
            tilt = math.hypot(c.get("tilt_x") or 0.0, c.get("tilt_y") or 0.0) * 1000
            values = [(c["dx"], 100, 1), (c["dy"], 100, 1), (c["dz"], 100, 1), (tilt, 1, 2),
                      (c.get("nmad_before"), 100, 2), (c.get("nmad_after"), 100, 2)]
            for col, (v, scale, decimals) in enumerate(values, start=2):
                if v is None:
                    continue
                it = _item(number(v * scale, decimals), right=True)
                it.setData(RAW_ROLE, float(v * scale))  # the Excel export keeps the number
                self.shifts.setItem(r, col, it)
            cells = _item(number(c["cells"]), right=True)
            cells.setData(RAW_ROLE, int(c["cells"]))
            self.shifts.setItem(r, 8, cells)
            notes = []
            if not c.get("horizontal", True):
                notes.append(tr("spostamento orizzontale non misurabile (terreno piano o in pendenza da un solo lato)"))
            if not c.get("converged", True):
                notes.append(tr("non convergente"))
            if c.get("units_excluded"):
                notes.append(tr("unità disegnate escluse"))
            self.shifts.setItem(r, 9, _item("; ".join(notes)))

    # ------------------------------------------------------------ content
    def set_report(self, report: dict, can_write: bool = False) -> None:
        self.report = report
        self.phases.blockSignals(True)
        self.phases.setRowCount(0)
        for p in report["phases"]:
            r = self.phases.rowCount()
            self.phases.insertRow(r)
            self.phases.setItem(r, 0, _item(p["label"], p["key"]))
            date = p["date"].strftime("%Y-%m-%d %H:%M") if p["date"] else ""
            self.phases.setItem(r, 1, _item(date, p["key"], editable=True))
            self.phases.setItem(r, 2, _item(", ".join(p["units"])))
        self.phases.blockSignals(False)
        self.steps.setRowCount(0)
        for s in report["removed"]:
            self._add_step(s["from"], s["to"], "", s["cut_m3"], s["fill_m3"], s["area_m2"], bold=True)
            for u in s["units"]:
                self._add_step("", "", u["name"] + (f" (area {u['area']})" if u["area"] else ""), u["cut_m3"],
                               u["fill_m3"], u["area_m2"])
        self.relations.setRowCount(0)
        for i, rel in enumerate(report["relations"]):
            r = self.relations.rowCount()
            self.relations.insertRow(r)
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            tick.setCheckState(Qt.CheckState.Checked)
            tick.setData(Qt.ItemDataRole.UserRole, i)
            self.relations.setItem(r, 0, tick)
            self.relations.setItem(r, 1, _item(timeline._name(rel["upper"])))
            self.relations.setItem(r, 2, _item(rel["relation"]))
            self.relations.setItem(r, 3, _item(timeline._name(rel["lower"])))
            overlap = _item(number(rel["overlap_m2"], 3), right=True)
            from .table_export import RAW_ROLE

            overlap.setData(RAW_ROLE, float(rel["overlap_m2"]))
            self.relations.setItem(r, 4, overlap)
            self.relations.setItem(r, 5, _item(" -> ".join(rel["phases"])))
        self.b_write.setEnabled(can_write and bool(report["relations"]))
        self._fill_shifts(report)
        n = len(report["phases"])
        text = (tr("{0} fasi, {1} rapporti suggeriti", n, len(report["relations"])) if n >= 2 else
                tr("Servono almeno due chunk con il DEM, nello stesso sistema di riferimento (Allinea chunk), uno per "
                   "fase."))
        coreg = report.get("coregistration")
        if coreg:
            text += "; " + (tr("DEM coregistrati sulla prima fase") if coreg["reference"] == "first" else
                            tr("DEM coregistrati ognuno sulla fase precedente"))
            failed = [p["label"] for p in report["phases"] if (p.get("coregistration") or {}).get("error")]
            if failed:
                text += " " + tr("(non coregistrate: {0})", ", ".join(failed))
        self.status.setText(text)

    def _add_step(self, a, b, unit, cut, fill, area, bold: bool = False) -> None:
        r = self.steps.rowCount()
        self.steps.insertRow(r)
        from .table_export import RAW_ROLE

        for c, v in enumerate([a, b, unit, number(cut, 3), number(fill, 3), number(area, 2)]):
            it = _item(v, right=c >= 3)
            if c >= 3:
                it.setData(RAW_ROLE, float((cut, fill, area)[c - 3]))  # the Excel export keeps the number
            if bold:
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            self.steps.setItem(r, c, it)

    def chosen_relations(self) -> list:
        out = []
        for r in range(self.relations.rowCount()):
            it = self.relations.item(r, 0)
            if it is not None and it.checkState() == Qt.CheckState.Checked:
                out.append(self.report["relations"][it.data(Qt.ItemDataRole.UserRole)])
        return out

    # ------------------------------------------------------------ actions
    def _phase_clicked(self, item) -> None:
        if item.column() != 1:  # the date column is edited with a double click
            self.phase_chosen.emit(int(self.phases.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)))

    def _date_edited(self, item) -> None:
        if item.column() != 1:
            return
        text = item.text().strip()
        try:
            if text:
                datetime.fromisoformat(text)
        except ValueError:
            self.status.setText(tr("Data non valida: {0} (per esempio 2026-07-01 09:30)", text))
            return
        self.date_changed.emit(int(item.data(Qt.ItemDataRole.UserRole)), text)

    def _save_csv(self, path: str | None = None) -> None:
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, tr("Salva CSV"), "fasi_scavo.csv",
                                                  "CSV (*.csv);;Excel (*.xlsx)")
        if not path:
            return
        if path.lower().endswith(".xlsx"):  # the three tables, a sheet each, numbers as numbers
            from ..io.tables import write_xlsx
            from .table_export import table_data

            path = write_xlsx(path, {self.tabs.tabText(i): table_data(self.tabs.widget(i))
                                     for i in range(self.tabs.count())})
        else:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(timeline.timeline_csv(self.report))
        self.status.setText(tr("Salvato {0}", path))

    def _write(self) -> None:
        chosen = self.chosen_relations()
        if chosen:
            self.write_requested.emit(chosen)
