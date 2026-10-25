"""Bulk identification preview: a shared name/group and independent numeric identities."""
from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
                               QLineEdit, QMessageBox, QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from ..archaeo.unit_identity import UNIT_TYPES, plan_units, unit_label
from .i18n import tr


class UnitIdentityDialog(QDialog):
    def __init__(self, units, selected=(), parent=None, reserved=(), defaults=None):
        super().__init__(parent)
        self.units, self.selected, self.reserved = list(units), set(selected), list(reserved)
        defaults = defaults or {}
        self.setWindowTitle(tr("Identifica e raggruppa unità"))
        self.resize(780, 660)
        layout = QVBoxLayout(self)
        intro = QLabel(tr("Assegna numeri distinti e un nome progressivo oppure lo stesso nome a un gruppo. "
                          "Il nome uguale crea un gruppo nella TOC; le geometrie restano separate. "
                          "I numeri occupati vengono saltati. Le modifiche si applicano tutte insieme e sono annullabili."))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.scope = self.combo([("selected", "Solo selezionate"), ("missing", "Solo senza numero"),
                                 ("all", "Tutte le unità")],
                                defaults.get("scope", "selected" if self.selected else "missing"))
        self.kind = self.combo([("", "Mantieni il tipo")] + [(v, v) for v in UNIT_TYPES], defaults.get("tipo", ""))
        self.numbers = self.combo([("missing", "Numera quelle senza numero"), ("keep", "Mantieni i numeri"),
                                   ("all", "Rinumera tutte quelle scelte")], defaults.get("number_mode", "missing"))
        self.first = QSpinBox()
        self.first.setRange(0, 2_147_480_000)
        self.first.setSpecialValueText(tr("Automatico: dopo i numeri già usati"))
        self.first.setValue(defaults.get("first") or 0)
        self.naming = self.combo([("keep", "Mantieni i nomi"), ("same", "Stesso nome / gruppo"),
                                  ("progressive", "Nome progressivo")], defaults.get("name_mode", "keep"))
        self.name = QLineEdit(defaults.get("name", ""))
        self.name.setPlaceholderText(tr("Esempio: Tombe settore A, oppure Tomba per un nome progressivo"))
        self.padding = QSpinBox()
        self.padding.setRange(0, 8)
        self.padding.setValue(defaults.get("padding", 4))
        self.project = QLineEdit(defaults.get("progetto", ""))
        self.order = self.combo([("source", "Ordine originale"), ("spatial", "Da nord a sud, da ovest a est")],
                                defaults.get("order", "source"))
        for title, widget in [("Quali unità", self.scope), ("Tipo", self.kind), ("Numerazione", self.numbers),
                               ("Primo numero", self.first), ("Nomi", self.naming), ("Nome / prefisso", self.name),
                               ("Cifre del nome progressivo", self.padding), ("Progetto delle UT", self.project),
                               ("Ordine", self.order)]:
            form.addRow(tr(title), widget)
        layout.addLayout(form)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.preview = QTableWidget(0, 3)
        self.preview.setHorizontalHeaderLabels([tr("Prima"), tr("Dopo"), tr("Gruppo nella TOC")])
        self.preview.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.preview.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.preview)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.validate_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        for widget in (self.scope, self.kind, self.numbers, self.naming, self.order):
            widget.currentIndexChanged.connect(self.refresh_preview)
        for widget in (self.first, self.padding):
            widget.valueChanged.connect(self.refresh_preview)
        for widget in (self.name, self.project):
            widget.textChanged.connect(self.refresh_preview)
        self.refresh_preview()

    @staticmethod
    def combo(values, current):
        widget = QComboBox()
        for value, label in values:
            widget.addItem(tr(label), value)
        widget.setCurrentIndex(max(0, widget.findData(current)))
        return widget

    def changes(self):
        scope = self.scope.currentData()
        indices = (sorted(self.selected) if scope == "selected" else
                   [i for i, u in enumerate(self.units) if not u.us] if scope == "missing" else
                   list(range(len(self.units))))
        if not indices:
            raise ValueError(tr("Nessuna unità nell'ambito scelto: selezionale sul canvas o scegli un altro ambito."))
        mode, name = self.naming.currentData(), self.name.text().strip()
        return plan_units(self.units, indices, tipo=self.kind.currentData() or None,
                          number_mode=self.numbers.currentData(), first=self.first.value() or None,
                          name_mode=mode, name=name, padding=self.padding.value(),
                          group=name if mode == "same" else None,
                          progetto=self.project.text().strip() or None, reserved=self.reserved,
                          order=self.order.currentData())

    def refresh_preview(self, *_):
        try:
            changes = self.changes()
            self.summary.setText(tr("{0} unità da modificare. Anteprima delle prime {1}.", len(changes),
                                    min(100, len(changes))))
            self.preview.setRowCount(min(100, len(changes)))
            for row, change in enumerate(changes[:100]):
                for col, value in enumerate((unit_label(change.before), unit_label(change.after),
                                              change.after.attributes.get("_layer", ""))):
                    self.preview.setItem(row, col, QTableWidgetItem(str(value)))
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(changes))
        except ValueError as exc:
            self.preview.setRowCount(0)
            self.summary.setText(str(exc))
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

    def validate_accept(self):
        try:
            self.changes()
        except ValueError as exc:
            QMessageBox.warning(self, "Identificativi", str(exc))
            return
        self.accept()

    @classmethod
    def ask(cls, units, selected=(), parent=None, reserved=(), defaults=None):
        dialog = cls(units, selected, parent, reserved, defaults)
        try:
            return dialog.changes() if dialog.exec() == QDialog.DialogCode.Accepted else None
        finally:
            dialog.deleteLater()
