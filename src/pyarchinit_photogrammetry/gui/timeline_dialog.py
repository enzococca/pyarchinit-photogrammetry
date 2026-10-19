"""Strumenti > Fasi dello scavo: the phases of the excavation (chunks by date), what was dug away
from one to the next and the stratigraphic relations that the order of removal suggests
(:mod:`archaeo.timeline`). A window that stays open beside the main one: a double click on a
phase shows it, its date can be corrected, the relations go into the US forms of PyArchInit."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHeaderView, QLabel,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout)

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
        intro = QLabel(tr("Ogni chunk con un DEM e' una fase dello scavo, nell'ordine delle date (dalle foto, "
                          "oppure scritta qui). Le unita' disegnate su una fase erano in luce allora e sono state "
                          "scavate prima della fase seguente: si misura quanto e' stato asportato e, dove si "
                          "sovrappongono in pianta, si propone che coprano quelle messe in luce dopo."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.tabs = QTabWidget()
        self.phases = _table(["Fase", "Data", "Unita' disegnate"])
        self.phases.itemDoubleClicked.connect(self._phase_clicked)
        self.phases.itemChanged.connect(self._date_edited)
        self.steps = _table(["Dalla fase", "Alla fase", "Unita'", "Asportato (m3)", "Riportato (m3)", "Area (m2)"])
        self.relations = _table(["Scrivi", "US sopra", "Rapporto", "US sotto", "Sovrapposizione (m2)", "Fasi"])
        self.tabs.addTab(self.phases, tr("Fasi"))
        self.tabs.addTab(self.steps, tr("Volumi asportati"))
        self.tabs.addTab(self.relations, tr("Rapporti suggeriti"))
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
            self.relations.setItem(r, 4, _item(number(rel["overlap_m2"], 3), right=True))
            self.relations.setItem(r, 5, _item(" -> ".join(rel["phases"])))
        self.b_write.setEnabled(can_write and bool(report["relations"]))
        n = len(report["phases"])
        self.status.setText(tr("{0} fasi, {1} rapporti suggeriti", n, len(report["relations"])) if n >= 2 else
                            tr("Servono almeno due chunk con il DEM, nello stesso sistema di riferimento (Allinea "
                               "chunk), uno per fase."))

    def _add_step(self, a, b, unit, cut, fill, area, bold: bool = False) -> None:
        r = self.steps.rowCount()
        self.steps.insertRow(r)
        for c, v in enumerate([a, b, unit, number(cut, 3), number(fill, 3), number(area, 2)]):
            it = _item(v, right=c >= 3)
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

    def _save_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr("Salva CSV"), "fasi_scavo.csv", "CSV (*.csv)")
        if path:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(timeline.timeline_csv(self.report))
            self.status.setText(tr("Salvato {0}", path))

    def _write(self) -> None:
        chosen = self.chosen_relations()
        if chosen:
            self.write_requested.emit(chosen)
