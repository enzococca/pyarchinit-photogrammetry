"""Strumenti > Proponi US e rapporti: the units without a number (found by SAM, or drawn) with the
US proposed for each - number, kind, stratigraphic definition, what the survey measures - and the
stratigraphic relations their contacts suggest, each with its reason (:mod:`archaeo.us_proposal`).
Numbers, kinds and definitions can be corrected in the table; the ticked units and relations go
into the project and, when asked, into the US forms of PyArchInit (from which PyArchInit and
EMStudio build the Extended Matrix). Units drawn with their number but without their form are
proposed with it; the units that have their form (``context``) are not listed among the units,
they take part in the relations only."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout)

from .i18n import number, tr

UNIT_HEADERS = ["Usa", "US", "Tipo", "Definizione", "Area (m2)", "Quote (m)", "Colore", "Nuvola", "Note"]
RELATION_HEADERS = ["Usa", "US sopra", "Rapporto", "US sotto", "Motivo"]
EDITABLE = {1: "us", 2: "tipo", 3: "d_stratigrafica"}


def _item(text, value=None, editable: bool = False, right: bool = False, check: bool | None = None):
    it = QTableWidgetItem("" if text is None else str(text))
    if value is not None:
        it.setData(Qt.ItemDataRole.UserRole, value)
    if right:
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    flags = it.flags() & ~Qt.ItemFlag.ItemIsEditable
    if editable:
        flags |= Qt.ItemFlag.ItemIsEditable
    if check is not None:
        flags |= Qt.ItemFlag.ItemIsUserCheckable
        it.setCheckState(Qt.CheckState.Checked if check else Qt.CheckState.Unchecked)
    it.setFlags(flags)
    return it


def _table(headers) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels([tr(h) for h in headers])
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def reason(rel: dict, names: dict) -> str:
    """Why a relation is proposed, in words."""
    step = rel.get("step_m")
    contact = number(rel.get("contact_m") or 0.0, 2)
    if rel["why"] == "higher":
        return tr("più alta di {0} cm lungo {1} m di contatto", number(abs(step or 0.0) * 100, 1), contact)
    if rel["why"] == "fill":
        return tr("dentro {0} e non più alta: riempimento di un taglio", names.get(rel.get("around"), "?"))
    if rel["why"] == "cut":
        return tr("il taglio attraversa {0}", names.get(rel["lower"], "?"))
    if step is None:
        return tr("contro il muro")
    return tr("contro il muro, più bassa della sua cima di {0} cm", number(abs(step) * 100, 1))


class UsProposalDialog(QDialog):
    """See the module docstring."""

    apply_requested = Signal(list, list, bool)  # unit keys, relation indices, also into PyArchInit
    unit_chosen = Signal(int)  # key of the unit to show on the orthomosaic

    def __init__(self, proposal: dict, can_write: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Proposta di US e rapporti"))
        self.setMinimumSize(820, 520)
        self.proposal = proposal
        lay = QVBoxLayout(self)
        intro = QLabel(tr("Ogni unità senza numero riceve una US: le più alte per prime, USM dove la nuvola "
                          "classificata dice muratura, con area, quote e colore misurati. I rapporti vengono dai "
                          "contatti: chi è più alta lungo il contatto copre l'altra, un'unità chiusa in un'altra e "
                          "non più alta è il riempimento di un taglio (proposto anch'esso), gli strati si "
                          "appoggiano ai muri. Numero, tipo e definizione si correggono nella tabella; doppio clic "
                          "per vedere l'unità sull'ortofoto. Le unità disegnate con il loro numero ma senza scheda "
                          "in PyArchInit lo tengono; quelle che la scheda ce l'hanno contano solo per i rapporti."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.tabs = QTabWidget()
        self.units = _table(UNIT_HEADERS)
        self.units.itemChanged.connect(self._edited)
        self.units.itemDoubleClicked.connect(self._unit_clicked)
        self.relations = _table(RELATION_HEADERS)
        from .table_export import install_excel_menu

        install_excel_menu(self.units, tr("Unità"))
        install_excel_menu(self.relations, tr("Rapporti"))
        self.tabs.addTab(self.units, tr("Unità"))
        self.tabs.addTab(self.relations, tr("Rapporti"))
        lay.addWidget(self.tabs, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        self.b_apply = bb.addButton(tr("Applica al progetto"), QDialogButtonBox.ButtonRole.ActionRole)
        self.b_apply.clicked.connect(lambda: self._apply(False))
        self.b_write = bb.addButton(tr("Applica e scrivi in PyArchInit"), QDialogButtonBox.ButtonRole.ActionRole)
        self.b_write.clicked.connect(lambda: self._apply(True))
        self.b_write.setEnabled(can_write)
        if not can_write:
            self.b_write.setToolTip(tr("Connetti prima un database PyArchInit (menu PyArchInit)"))
        lay.addWidget(bb)
        self._fill()

    # ------------------------------------------------------------ content
    def _names(self) -> dict:
        return {it["key"]: f"{it['unit'].tipo or 'US'} {it['unit'].us}" for it in self.proposal["units"]}

    def _fill(self) -> None:
        self.units.blockSignals(True)
        self.units.setRowCount(0)
        for it in self.proposal["units"]:
            if it.get("context"):  # its form is there: only its relations
                continue
            r = self.units.rowCount()
            self.units.insertRow(r)
            m, u, f = it["measures"], it["unit"], it["fields"]
            z = (f"{number(m['z_min'], 3)} - {number(m['z_max'], 3)}" if m.get("z_min") is not None else "")
            colour = (m.get("colour") or {}).get("chip") or ""
            cloud = it.get("cloud_name") or ""
            note = tr("taglio del riempimento {0}", self._names()[it["fill"]]) if it["kind"] == "cut" else ""
            values = [None, u.us, u.tipo or "US", f.get("d_stratigrafica", ""), number(m.get("area_m2", 0.0), 2), z,
                      colour, cloud, note]
            for c, v in enumerate(values):
                if c == 0:
                    self.units.setItem(r, c, _item("", it["key"], check=it["use"]))
                else:
                    self.units.setItem(r, c, _item(v, editable=c in EDITABLE, right=c in (4, 5)))
        self.units.blockSignals(False)
        self._fill_relations()

    def _fill_relations(self) -> None:
        names = self._names()
        checked = {self.relations.item(r, 0).data(Qt.ItemDataRole.UserRole): self.relations.item(r, 0).checkState()
                   for r in range(self.relations.rowCount())}
        self.relations.setRowCount(0)
        for i, rel in enumerate(self.proposal["relations"]):
            r = self.relations.rowCount()
            self.relations.insertRow(r)
            use = checked[i] == Qt.CheckState.Checked if i in checked else rel["use"]
            self.relations.setItem(r, 0, _item("", i, check=use))
            self.relations.setItem(r, 1, _item(names[rel["upper"]]))
            self.relations.setItem(r, 2, _item(rel["relation"]))
            self.relations.setItem(r, 3, _item(names[rel["lower"]]))
            self.relations.setItem(r, 4, _item(reason(rel, names)))
        n_units = sum(1 for it in self.proposal["units"] if it["kind"] == "unit" and not it.get("context"))
        n_context = sum(1 for it in self.proposal["units"] if it.get("context"))
        n_cuts = len(self.proposal["units"]) - n_units - n_context
        text = tr("{0} unità, {1} tagli proposti, {2} rapporti", n_units, n_cuts, len(self.proposal["relations"]))
        if n_context:
            text += tr("; {0} US con la scheda già in PyArchInit, per i rapporti", n_context)
        self.status.setText(text)

    def _edited(self, item) -> None:
        field = EDITABLE.get(item.column())
        if field is None:
            return
        it = self.proposal["units"][int(self.units.item(item.row(), 0).data(Qt.ItemDataRole.UserRole))]
        text = item.text().strip()
        if field == "us":
            it["unit"].us = text
        elif field == "tipo":
            it["unit"].tipo = text.upper() or "US"
        else:
            it["fields"]["d_stratigrafica"] = text
        self._fill_relations()

    def _unit_clicked(self, item) -> None:
        if item.column() not in EDITABLE:
            self.unit_chosen.emit(int(self.units.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)))

    def chosen(self) -> tuple[list, list]:
        keys = [int(self.units.item(r, 0).data(Qt.ItemDataRole.UserRole)) for r in range(self.units.rowCount())
                if self.units.item(r, 0).checkState() == Qt.CheckState.Checked]
        rels = [int(self.relations.item(r, 0).data(Qt.ItemDataRole.UserRole)) for r in range(self.relations.rowCount())
                if self.relations.item(r, 0).checkState() == Qt.CheckState.Checked]
        return keys, rels

    def _apply(self, write: bool) -> None:
        keys, rels = self.chosen()
        numbers = [self.proposal["units"][k]["unit"].us for k in keys]
        missing = [k for k, n in zip(keys, numbers) if not n]
        twice = sorted({n for n in numbers if n and numbers.count(n) > 1})
        if missing or twice:
            self.status.setText(tr("Ogni unità scelta vuole un numero di US diverso: {0}",
                                   ", ".join(twice) if twice else tr("manca un numero")))
            return
        self.apply_requested.emit(keys, rels, write)
