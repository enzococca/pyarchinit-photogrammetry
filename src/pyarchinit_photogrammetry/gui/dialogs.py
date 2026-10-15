"""Parameter dialogs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)


@dataclass
class Field:
    name: str
    label: str
    default: Any = None
    choices: list | None = None  # [(value, label)] -> combo box
    minimum: float = 0.0
    maximum: float = 1e9
    decimals: int = 4
    kind: str | None = None  # "file_save", "file_open", "dir", "text", "optional_float"
    filter: str = ""
    tip: str = ""


class _PathEdit(QWidget):
    def __init__(self, f: Field, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(str(f.default or ""))
        b = QPushButton("...")
        b.setFixedWidth(32)
        lay.addWidget(self.edit, 1)
        lay.addWidget(b)
        self.f = f
        b.clicked.connect(self._browse)

    def _browse(self) -> None:
        if self.f.kind == "dir":
            p = QFileDialog.getExistingDirectory(self, self.f.label, self.edit.text())
        elif self.f.kind == "file_save":
            p, _ = QFileDialog.getSaveFileName(self, self.f.label, self.edit.text(), self.f.filter)
        else:
            p, _ = QFileDialog.getOpenFileName(self, self.f.label, self.edit.text(), self.f.filter)
        if p:
            self.edit.setText(p)

    def value(self) -> str:
        return self.edit.text()


class ParamDialog(QDialog):
    """A form built from a list of :class:`Field` (bool, int, float, str, choices, paths)."""

    def __init__(self, title: str, fields: list[Field], parent=None, intro: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        if intro:
            lab = QLabel(intro)
            lab.setWordWrap(True)
            lay.addWidget(lab)
        form = QFormLayout()
        lay.addLayout(form)
        self.widgets: dict[str, tuple[Field, QWidget]] = {}
        for f in fields:
            w = self._widget(f)
            if f.tip:
                w.setToolTip(f.tip)
            form.addRow(f.label, w)
            self.widgets[f.name] = (f, w)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _widget(self, f: Field) -> QWidget:
        if f.choices is not None:
            w = QComboBox()
            for v, lab in f.choices:
                w.addItem(lab, v)
            i = next((k for k, (v, _) in enumerate(f.choices) if v == f.default), 0)
            w.setCurrentIndex(i)
            return w
        if f.kind in ("file_save", "file_open", "dir"):
            return _PathEdit(f)
        if f.kind == "optional_float":
            w = QLineEdit("" if f.default is None else str(f.default))
            w.setPlaceholderText("automatico")
            return w
        if isinstance(f.default, bool):
            w = QCheckBox()
            w.setChecked(f.default)
            return w
        if isinstance(f.default, int):
            w = QSpinBox()
            w.setRange(int(f.minimum), int(min(f.maximum, 2 ** 31 - 1)))
            w.setValue(f.default)
            return w
        if isinstance(f.default, float):
            w = QDoubleSpinBox()
            w.setDecimals(f.decimals)
            w.setRange(f.minimum, f.maximum)
            w.setValue(f.default)
            return w
        return QLineEdit("" if f.default is None else str(f.default))

    def values(self) -> dict:
        out = {}
        for name, (f, w) in self.widgets.items():
            if isinstance(w, QComboBox):
                out[name] = w.currentData()
            elif isinstance(w, _PathEdit):
                out[name] = w.value()
            elif isinstance(w, QCheckBox):
                out[name] = w.isChecked()
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                out[name] = w.value()
            elif f.kind == "optional_float":
                t = w.text().strip().replace(",", ".")
                out[name] = float(t) if t else None
            else:
                out[name] = w.text()
        return out

    @classmethod
    def ask(cls, parent, title: str, fields: list[Field], intro: str = "") -> dict | None:
        d = cls(title, fields, parent, intro)
        return d.values() if d.exec() == QDialog.DialogCode.Accepted else None


QUALITY = [("highest", "Massima"), ("high", "Alta"), ("medium", "Media"), ("low", "Bassa"), ("lowest", "Minima")]


class PublishDialog(QDialog):
    """Publish products to PyArchInit: site, document name, products, linked US."""

    def __init__(self, pa, chunk, parent=None, sites: list[str] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Pubblica in PyArchInit")
        self.setMinimumWidth(480)
        self.pa = pa
        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)
        self.site = QComboBox()
        self.site.addItems(sites if sites is not None else [str(s.get("sito")) for s in pa.sites()])
        self.site.currentTextChanged.connect(self._load_us)
        self.name = QLineEdit(chunk.label if chunk is not None else "Rilievo")
        self.author = QLineEdit("")
        self.cb_ortho = QCheckBox("Ortofoto")
        self.cb_dem = QCheckBox("DEM")
        self.cb_model = QCheckBox("Modello 3D")
        self.cb_ortho.setChecked(chunk is not None and chunk.orthomosaic_path is not None)
        self.cb_dem.setChecked(chunk is not None and chunk.dem_path is not None)
        self.cb_ortho.setEnabled(chunk is not None and chunk.orthomosaic_path is not None)
        self.cb_dem.setEnabled(chunk is not None and chunk.dem_path is not None)
        self.cb_model.setEnabled(chunk is not None and chunk.product_path("model") is not None)
        self.overwrite = QCheckBox("Sovrascrivi se esiste gia'")
        prods = QHBoxLayout()
        for cb in (self.cb_ortho, self.cb_dem, self.cb_model):
            prods.addWidget(cb)
        form.addRow("Sito", self.site)
        form.addRow("Nome documento", self.name)
        form.addRow("Disegnatore / operatore", self.author)
        form.addRow("Prodotti", prods)
        form.addRow("", self.overwrite)
        lay.addWidget(QLabel("Collega alle US (selezione multipla):"))
        self.us_list = QListWidget()
        self.us_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        lay.addWidget(self.us_list, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._load_us(self.site.currentText())

    def _load_us(self, site: str) -> None:
        self.us_list.clear()
        if not site:
            return
        for u in self.pa.stratigraphic_units(site):
            it = QListWidgetItem(f"Area {u.get('area')} - {u.get('unita_tipo') or 'US'} {u.get('us')}  "
                                 f"{u.get('d_stratigrafica') or ''}")
            it.setData(Qt.ItemDataRole.UserRole, (str(u.get("area")), str(u.get("us"))))
            self.us_list.addItem(it)

    def values(self) -> dict:
        return {"sito": self.site.currentText(), "nome_doc": self.name.text().strip(),
                "disegnatore": self.author.text().strip(), "ortho": self.cb_ortho.isChecked(),
                "dem": self.cb_dem.isChecked(), "model": self.cb_model.isChecked(),
                "overwrite": self.overwrite.isChecked(),
                "link_us": [it.data(Qt.ItemDataRole.UserRole) for it in self.us_list.selectedItems()]}


class UnitDialog(QDialog):
    """Attributes of a drawn stratigraphic unit (area, US, type)."""

    def __init__(self, parent=None, us_choices: list[tuple[str, str, str]] | None = None, default_area: str = "1"):
        super().__init__(parent)
        self.setWindowTitle("Unita' stratigrafica")
        form = QFormLayout(self)
        self.combo = QComboBox()
        self.combo.addItem("(nuova / scrivi sotto)", None)
        for area, us, desc in us_choices or []:
            self.combo.addItem(f"Area {area} - US {us} {desc}", (area, us))
        self.area = QLineEdit(default_area)
        self.us = QLineEdit("")
        self.tipo = QComboBox()
        self.tipo.addItems(["US", "USM", "USR", "USD", "USV"])
        self.combo.currentIndexChanged.connect(self._pick)
        form.addRow("US esistente", self.combo)
        form.addRow("Area", self.area)
        form.addRow("US", self.us)
        form.addRow("Tipo", self.tipo)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _pick(self, _i) -> None:
        d = self.combo.currentData()
        if d:
            self.area.setText(d[0])
            self.us.setText(d[1])

    def values(self) -> dict:
        return {"area": self.area.text().strip(), "us": self.us.text().strip(), "tipo": self.tipo.currentText()}


class ImageDialog(QDialog):
    """Shows an SVG/PNG result (pottery drawing, section) with its measurements."""

    def __init__(self, title: str, path: Path, text: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 680)
        lay = QVBoxLayout(self)
        if Path(path).suffix.lower() == ".svg":
            from PySide6.QtSvgWidgets import QSvgWidget

            w = QSvgWidget(str(path))
            r = w.renderer().defaultSize()
            if r.width() > 0:
                w.setMinimumSize(min(r.width(), 780), min(r.height(), 560))
        else:
            from PySide6.QtGui import QPixmap

            w = QLabel()
            w.setPixmap(QPixmap(str(path)).scaled(780, 560, Qt.AspectRatioMode.KeepAspectRatio))
        w.setStyleSheet("background: white;")
        lay.addWidget(w, 1)
        if text:
            lab = QLabel(text)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lab.setWordWrap(True)
            lay.addWidget(lab)
        lay.addWidget(QLabel(f"File: {path}"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
