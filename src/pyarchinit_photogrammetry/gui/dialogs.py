"""Parameter dialogs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
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
    kind: str | None = None  # "file_save", "file_open", "dir", "text", "optional_float", "checklist", "color"
    filter: str = ""
    tip: str = ""


class _ColorButton(QPushButton):
    """A button showing a colour; a click opens the (non-native) colour chooser."""

    def __init__(self, rgb, parent=None):
        super().__init__(parent)
        self.rgb = tuple(int(v) for v in (rgb or (135, 206, 235))[:3])
        self.clicked.connect(self._choose)
        self._show()

    def _show(self) -> None:
        r, g, b = self.rgb
        text = "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "#ffffff"
        self.setText(f"RGB {r}, {g}, {b}")
        self.setStyleSheet(f"background: rgb({r}, {g}, {b}); color: {text}; padding: 4px 10px;")

    def _choose(self) -> None:
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog

        c = QColorDialog.getColor(QColor(*self.rgb), self, "Colore",
                                  QColorDialog.ColorDialogOption.DontUseNativeDialog)
        if c.isValid():
            self.rgb = (c.red(), c.green(), c.blue())
            self._show()


class _PathEdit(QWidget):
    def __init__(self, f: Field, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(str(f.default or ""))
        b = QPushButton("Sfoglia")
        b.setFixedWidth(72)
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

    def showEvent(self, event) -> None:
        super().showEvent(event)
        hint = self.sizeHint()  # the combo boxes know their width only once polished
        if self.width() < hint.width():
            self.resize(hint.width(), max(self.height(), hint.height()))

    def _widget(self, f: Field) -> QWidget:
        if f.kind == "checklist":  # several of the choices: default = the values ticked
            w = QListWidget()
            for v, lab in f.choices or []:
                it = QListWidgetItem(lab)
                it.setData(Qt.ItemDataRole.UserRole, v)
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Checked if v in (f.default or []) else Qt.CheckState.Unchecked)
                w.addItem(it)
            rows = min(max(w.count(), 1), 8)  # every row visible (up to 8, then it scrolls)
            w.setFixedHeight(w.sizeHintForRow(0) * rows + 2 * w.frameWidth() + 6)
            w.setMinimumWidth(min(600, w.sizeHintForColumn(0) + 2 * w.frameWidth() + 24))
            return w
        if f.choices is not None:
            w = QComboBox()
            w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)  # the dialog widens, no cut text
            for v, lab in f.choices:
                w.addItem(lab, v)
            i = next((k for k, (v, _) in enumerate(f.choices) if v == f.default), 0)
            w.setCurrentIndex(i)
            w.setMinimumWidth(min(600, w.sizeHint().width()))
            return w
        if f.kind in ("file_save", "file_open", "dir"):
            return _PathEdit(f)
        if f.kind == "color":
            return _ColorButton(f.default)
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
            if f.kind == "checklist":
                out[name] = [w.item(i).data(Qt.ItemDataRole.UserRole) for i in range(w.count())
                             if w.item(i).checkState() == Qt.CheckState.Checked]
            elif isinstance(w, QComboBox):
                out[name] = w.currentData()
            elif isinstance(w, _PathEdit):
                out[name] = w.value()
            elif isinstance(w, _ColorButton):
                out[name] = w.rgb
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


def _it(value: float, decimals: int = 0) -> str:
    """A number written the Italian way: 1.234.567 or 1.234,56."""
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


class GradualSelectionDialog(QDialog):
    """Gradual selection of the tie points (as in Metashape): a threshold per criterion, the
    points it would remove counted at once, and the optimization of the cameras afterwards.

    ``metrics``: :func:`engine.tiepoints.tie_point_metrics`; ``select(**criteria)`` returns
    :func:`engine.tiepoints.select_tie_points` for those thresholds."""

    CRITERIA = [  # key, label, metric, default, decimals, maximum
        ("max_reprojection_error", "Errore di riproiezione oltre (px)", "reprojection_error", 0.5, 2, 1e6),
        ("min_image_count", "Visti da meno foto di", "image_count", 3, 0, 1e4),
        ("max_reconstruction_uncertainty", "Incertezza di ricostruzione oltre", "reconstruction_uncertainty", 15.0, 1, 1e9),
        ("max_projection_accuracy", "Accuratezza di proiezione oltre (px)", "projection_accuracy", 3.0, 2, 1e6),
    ]

    def __init__(self, metrics: dict, select, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Selezione graduale dei punti di legame")
        self.setMinimumWidth(700)
        self.metrics, self.select = metrics, select
        lay = QVBoxLayout(self)
        n = len(metrics["ids"])
        text = (f"{_it(n)} punti di legame in {metrics['images']} foto, errore medio di riproiezione "
                f"{_it(metrics['mean_reprojection_error'], 3)} px. Spunta i criteri: i punti peggiori vengono tolti "
                "dal modello e, se scegli, le camere vengono ottimizzate senza di loro.")
        if metrics.get("synthetic"):
            text += ("\n\nAttenzione: questo modello e' stato scritto da pose importate (Metashape, WebODM o chunk "
                     "uniti): gli errori di riproiezione sono quasi nulli e conta solo l'incertezza di ricostruzione. "
                     "Per una selezione completa allinea di nuovo le foto qui.")
        intro = QLabel(text)
        intro.setWordWrap(True)
        lay.addWidget(intro)
        form = QFormLayout()
        lay.addLayout(form)
        self.rows = {}
        for key, label, metric, default, decimals, maximum in self.CRITERIA:
            values = metrics.get(metric)
            box = QCheckBox(label)
            spin = QDoubleSpinBox()
            spin.setDecimals(decimals)
            spin.setRange(0.0, maximum)
            spin.setValue(default)
            stats = QLabel("non disponibile")
            if values is None:
                box.setEnabled(False)
                spin.setEnabled(False)
            else:
                v = np.asarray(values, float)
                v = v[np.isfinite(v)]
                if len(v):
                    q = np.percentile(v, [50, 95])
                    stats.setText(f"mediana {q[0]:.3g}, 95% {q[1]:.3g}, max {v.max():.3g}".replace(".", ","))
            stats.setStyleSheet("color: #6c7a89")
            row = QHBoxLayout()
            row.addWidget(spin)
            row.addWidget(stats, 1)
            form.addRow(box, row)
            box.toggled.connect(lambda _on: self._update())
            spin.valueChanged.connect(lambda _v: self._update())
            self.rows[key] = (box, spin)
        self.optimize = QCheckBox("Ottimizza le camere dopo la rimozione")
        self.optimize.setChecked(True)
        lay.addWidget(self.optimize)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-weight: bold")
        lay.addWidget(self.status)
        note = QLabel("I punti tolti non tornano indietro: per ricominciare allinea di nuovo le foto.")
        note.setStyleSheet("color: #b45309")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Togli i punti selezionati")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.ok_button = bb.button(QDialogButtonBox.StandardButton.Ok)
        lay.addWidget(bb)
        self.selection = None
        self._update()

    def criteria(self) -> dict:
        out = {}
        for key, (box, spin) in self.rows.items():
            if box.isChecked():
                out[key] = int(spin.value()) if key == "min_image_count" else float(spin.value())
        return out

    def _update(self) -> None:
        crit = self.criteria()
        if not crit:
            self.selection = None
            self.status.setText("Spunta almeno un criterio.")
            self.ok_button.setEnabled(False)
            return
        sel = self.select(**crit)
        self.selection = sel
        names = {"reprojection_error": "errore", "image_count": "poche foto",
                 "reconstruction_uncertainty": "incertezza", "projection_accuracy": "accuratezza"}
        parts = [f"{names.get(name, name)} {_it(n)}" for name, n in (sel.get("by_criterion") or {}).items()
                 if n is not None]
        text = (f"Selezionati {_it(sel['selected'])} punti su {_it(sel['total'])} ({_it(sel['percent'], 1)}%)"
                + (f" - {', '.join(parts)}" if parts else ""))
        if sel.get("images_below_minimum"):
            text += f"\n{sel['images_below_minimum']} foto resterebbero con troppi pochi punti: alza le soglie."
        self.status.setText(text)
        self.ok_button.setEnabled(sel["selected"] > 0 and not sel.get("images_below_minimum"))

    def values(self) -> dict:
        return {**self.criteria(), "optimize": self.optimize.isChecked()}


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
        self.site.setEditable(True)
        self.site.addItems(sites if sites is not None else [str(s.get("sito")) for s in pa.sites()])
        self.site.currentTextChanged.connect(self._load_us)
        self.b_new_site = QPushButton("Nuovo sito...")
        self.b_new_site.clicked.connect(self._new_site)
        site_row = QHBoxLayout()
        site_row.addWidget(self.site, 1)
        site_row.addWidget(self.b_new_site)
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
        form.addRow("Sito", site_row)
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

    def _new_site(self) -> None:
        dlg = SiteDialog(self, self.site.currentText().strip())
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.values()["sito"]:
            return
        v = dlg.values()
        self.pa.ensure_site(**v)
        if self.site.findText(v["sito"]) < 0:
            self.site.addItem(v["sito"])
        self.site.setCurrentText(v["sito"])

    def _load_us(self, site: str) -> None:
        self.us_list.clear()
        if not site:
            return
        try:
            units = self.pa.stratigraphic_units(site)
        except Exception:
            units = []
        for u in units:
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


UNIT_TYPES = ["US", "USM", "USR", "USD", "USV"]
TIPO_US_SUGGESTIONS = ["", "positiva", "negativa", "struttura", "riempimento", "taglio"]


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


class _SiteHeader(QLabel):
    def __init__(self, sito: str):
        super().__init__(f"Sito: <b>{sito}</b>" if sito else "Sito: <i>non scelto (menu PyArchInit > Sito del progetto)</i>")
        self.setTextFormat(Qt.TextFormat.RichText)


class UnitDialog(QDialog):
    """Attributes of a drawn stratigraphic unit, the columns of PyArchInit's
    ``pyunitastratigrafiche``: area, US, unit type, stratigraphic index, type, draughtsman,
    date, original survey. The site is the one of the project. ``refine=True`` adds a
    *Correggi* button (SAM masks: add or remove parts before saving)."""

    REFINE = 2  # exec() result of the Correggi button

    def __init__(self, parent=None, us_choices: list[tuple[str, str, str]] | None = None, default_area: str = "1",
                 sito: str = "", defaults: dict | None = None, refine: bool = False, title: str = "Unita' stratigrafica"):
        super().__init__(parent)
        self.setWindowTitle(title)
        d = dict(defaults or {})
        form = QFormLayout(self)
        form.addRow(_SiteHeader(sito))
        self.combo = QComboBox()
        self.combo.addItem("(nuova / scrivi sotto)", None)
        for area, us, desc in us_choices or []:
            self.combo.addItem(f"Area {area} - US {us} {desc}", (area, us))
        self.area = QLineEdit(str(d.get("area") or default_area))
        self.us = QLineEdit(str(d.get("us") or ""))
        self.tipo = QComboBox()
        self.tipo.setEditable(True)
        self.tipo.addItems(UNIT_TYPES)
        self.tipo.setCurrentText(str(d.get("tipo") or "US"))
        self.index = QComboBox()
        for k, label in ((1, "1 - Caratterizzazione"), (2, "2 - Contorno US")):
            self.index.addItem(label, k)
        self.index.setCurrentIndex(1 if str(d.get("stratigraph_index_us") or "1") == "2" else 0)
        self.index.setToolTip("stratigraph_index_us di pyunitastratigrafiche: 1 = caratterizzazione (riempita nel "
                              "layer di PyArchInit), 2 = contorno dell'US")
        self.tipo_us = QComboBox()
        self.tipo_us.setEditable(True)
        self.tipo_us.addItems(TIPO_US_SUGGESTIONS)
        self.tipo_us.setCurrentText(str(d.get("tipo_us_s") or ""))
        self.disegnatore = QLineEdit(str(d.get("disegnatore") or ""))
        self.data = QLineEdit(str(d.get("data") or _today()))
        self.rilievo = QLineEdit(str(d.get("rilievo_originale") or "Fotogrammetria"))
        self.combo.currentIndexChanged.connect(self._pick)
        form.addRow("US esistente", self.combo)
        form.addRow("Area", self.area)
        form.addRow("US", self.us)
        form.addRow("Tipo unita'", self.tipo)
        form.addRow("Indice stratigrafico", self.index)
        form.addRow("Tipo (tipo_us_s)", self.tipo_us)
        form.addRow("Disegnatore", self.disegnatore)
        form.addRow("Data", self.data)
        form.addRow("Rilievo originale", self.rilievo)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Salva")
        if refine:
            b = bb.addButton("Correggi...", QDialogButtonBox.ButtonRole.ActionRole)
            b.setToolTip("Torna sulla mappa: Maiusc+clic aggiunge, clic destro toglie; Invio per salvare, Esc per scartare")
            b.clicked.connect(lambda: self.done(self.REFINE))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.us.setFocus()

    def _pick(self, _i) -> None:
        d = self.combo.currentData()
        if d:
            self.area.setText(d[0])
            self.us.setText(d[1])

    def values(self) -> dict:
        return {"area": self.area.text().strip(), "us": self.us.text().strip(), "tipo": self.tipo.currentText().strip(),
                "stratigraph_index_us": int(self.index.currentData() or 1), "tipo_us_s": self.tipo_us.currentText().strip(),
                "disegnatore": self.disegnatore.text().strip(), "data": self.data.text().strip(),
                "rilievo_originale": self.rilievo.text().strip()}


class SiteChoiceDialog(QDialog):
    """The site (excavation) the project belongs to: one of the database's sites or a new name."""

    def __init__(self, parent=None, sites: list[str] | None = None, current: str = "", text: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Sito del progetto")
        lay = QVBoxLayout(self)
        lab = QLabel(text or "Sito dello scavo: tutte le US e le quote di questo progetto appartengono a questo sito.")
        lab.setWordWrap(True)
        lay.addWidget(lab)
        self.combo = QComboBox()
        self.combo.setEditable(True)
        names = list(sites or [])
        if current and current not in names:
            names.insert(0, current)
        self.combo.addItems(names)
        self.combo.setCurrentText(current or (names[0] if names else ""))
        lay.addWidget(self.combo)
        if not names:
            hint = QLabel("Nessun sito nel database (o database non connesso): scrivi il nome del sito.")
            hint.setObjectName("pageSubtitle")
            hint.setWordWrap(True)
            lay.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def value(self) -> str:
        return self.combo.currentText().strip()


class QuotaDialog(QDialog):
    """A spot height with the columns of PyArchInit's ``pyarchinit_quote``: area, US, unit
    type, elevation (read on the DEM, editable), unit of measure, draughtsman, date,
    original survey. *Non chiedere piu'* keeps these values for the next points."""

    def __init__(self, parent=None, us_choices: list[tuple[str, str, str]] | None = None, sito: str = "",
                 quota: float | None = None, defaults: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Quota")
        d = dict(defaults or {})
        form = QFormLayout(self)
        form.addRow(_SiteHeader(sito))
        self.combo = QComboBox()
        self.combo.addItem("(nuova / scrivi sotto)", None)
        for area, us, desc in us_choices or []:
            self.combo.addItem(f"Area {area} - US {us} {desc}", (area, us))
        self.area = QLineEdit(str(d.get("area_q") or "1"))
        self.us = QLineEdit(str(d.get("us_q") or ""))
        self.tipo = QComboBox()
        self.tipo.setEditable(True)
        self.tipo.addItems(UNIT_TYPES)
        self.tipo.setCurrentText(str(d.get("unita_tipo_q") or "US"))
        self.quota = QDoubleSpinBox()
        self.quota.setDecimals(3)
        self.quota.setRange(-100000.0, 100000.0)
        self.quota.setValue(float(quota) if quota is not None else 0.0)
        self.misura = QComboBox()
        self.misura.setEditable(True)
        self.misura.addItems(["m s.l.m.", "m (quota relativa)"])
        self.misura.setCurrentText(str(d.get("unita_misu_q") or "m s.l.m."))
        self.disegnatore = QLineEdit(str(d.get("disegnatore") or ""))
        self.data = QLineEdit(str(d.get("data") or _today()))
        self.rilievo = QLineEdit(str(d.get("rilievo_originale") or "Fotogrammetria"))
        self.keep = QCheckBox("Non chiedere piu': usa questi valori per i prossimi punti")
        self.combo.currentIndexChanged.connect(self._pick)
        form.addRow("US esistente", self.combo)
        form.addRow("Area", self.area)
        form.addRow("US", self.us)
        form.addRow("Tipo unita'", self.tipo)
        form.addRow("Quota", self.quota)
        form.addRow("Unita' di misura", self.misura)
        form.addRow("Disegnatore", self.disegnatore)
        form.addRow("Data", self.data)
        form.addRow("Rilievo originale", self.rilievo)
        form.addRow(self.keep)
        if quota is None:
            form.addRow(QLabel("Fuori dal DEM: scrivi la quota a mano."))
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
        return {"area_q": self.area.text().strip(), "us_q": self.us.text().strip(),
                "unita_tipo_q": self.tipo.currentText().strip(), "quota_q": round(self.quota.value(), 3),
                "unita_misu_q": self.misura.currentText().strip(), "disegnatore": self.disegnatore.text().strip(),
                "data": self.data.text().strip(), "rilievo_originale": self.rilievo.text().strip(),
                "keep": self.keep.isChecked()}


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


def short_label(label: str, size: int = 20) -> str:
    """A photo name short enough for a list or a map label (drones write long ones): the end,
    where the number of the photo is."""
    return label if len(label) <= size else "..." + label[-(size - 3):]


class PatchDialog(QDialog):
    """Choose the photos of an area: a patch of the orthomosaic (``kind="ortho"``) or faces of
    the textured model (``kind="faces"``). The area as it is now beside the area seen from the
    photo chosen in the list; the list shows every photo that sees it, best first, with what
    it covers, its angle from the vertical (or from the faces), its pixel on the ground and
    how much of the area the orthomosaic takes from it now. For the orthomosaic the previews
    (``candidate["previews"][surface]``) show the result: the area from that photo, the rest as
    it is, on the DEM or on the ground around the area. ``texture``: offer to correct the
    texture of the model there too (None: no textured model)."""

    def __init__(self, title: str, current, candidates: list[dict], intro: str = "", kind: str = "ortho",
                 texture: bool | None = None, parent=None):
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QIcon, QPixmap
        from PySide6.QtWidgets import QAbstractItemView, QButtonGroup, QRadioButton

        from .image_views import to_qimage

        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(980, 760)
        self.kind = kind
        self.candidates = candidates
        lay = QVBoxLayout(self)
        if intro:
            lab = QLabel(intro)
            lab.setWordWrap(True)
            lay.addWidget(lab)
        row = QHBoxLayout()
        self._side = 380

        def panel(caption: str):
            box = QVBoxLayout()
            cap = QLabel(caption)
            cap.setObjectName("pageSubtitle")
            img = QLabel()
            img.setAlignment(Qt.AlignmentFlag.AlignCenter)
            img.setMinimumSize(self._side, self._side * 3 // 4)
            img.setStyleSheet("background: #2b2f36; border-radius: 4px;")
            box.addWidget(cap)
            box.addWidget(img, 1)
            row.addLayout(box, 1)
            return img

        self.now_label = panel("Adesso" if kind == "ortho" else "Le facce scelte")
        self.preview_label = panel("Con la foto scelta" if kind == "ortho" else "Dalla foto scelta")
        lay.addLayout(row, 3)
        self._pix = lambda rgba: QPixmap.fromImage(to_qimage(rgba))
        if current is not None:
            self._show(self.now_label, current)
        else:
            self.now_label.setText("(nessuna ortofoto)")
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setIconSize(QSize(150, 150))
        self.list.setGridSize(QSize(172, 246))  # long photo names must not widen the cells
        self.list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setSpacing(6)
        self.list.setWordWrap(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for c in candidates:
            gsd = c.get("gsd") or 0.0
            text = (f"{short_label(c['label'])}\n{_it(100 * c['coverage'])}% · {_it(c['angle'])}° · "
                    f"{_it(100 * gsd, 1)} cm/px")
            if c.get("used"):
                text += f"\nusata ora: {_it(100 * c['used'])}%"
            item = QListWidgetItem(text)
            prev = c.get("preview")
            if prev is not None:
                item.setIcon(QIcon(self._pix(prev)))
            hidden = 1.0 - c.get("visible", 1.0)
            item.setToolTip(f"{c['label']}: inquadra il {_it(100 * c['coverage'])}% dell'area, a {_it(c['angle'])} gradi "
                            + ("dalla verticale" if kind == "ortho" else "dalla perpendicolare alle facce")
                            + f", {_it(100 * gsd, 2)} cm per pixel"
                            + (f"; il {_it(100 * hidden)}% e' nascosto dal modello (chiome, muri)" if hidden > 0.05
                               else "")
                            + (f"; l'ortofoto ora ne prende il {_it(100 * c['used'])}%" if c.get("used") else ""))
            item.setData(Qt.ItemDataRole.UserRole, c["label"])
            self.list.addItem(item)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self._icons = {}
        for c in candidates:
            for surf, img in (c.get("previews") or {}).items():
                self._icons[(c["label"], surf)] = QIcon(self._pix(img))
        lay.addWidget(QLabel("Foto che vedono l'area (le migliori prima; Maiusc o Cmd per sceglierne piu' d'una):"))
        lay.addWidget(self.list, 2)
        modes = QHBoxLayout()
        self.r_assign = QRadioButton("Usa solo le foto scelte")
        self.r_exclude = QRadioButton("Escludi le foto scelte (usa tutte le altre)")
        self.r_assign.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.r_assign)
        group.addButton(self.r_exclude)
        modes.addWidget(self.r_assign)
        modes.addWidget(self.r_exclude)
        modes.addStretch(1)
        lay.addLayout(modes)
        surfaces = QHBoxLayout()
        self.r_dem = QRadioButton("Sul DEM (ortofoto vera)")
        self.r_ground = QRadioButton("Sul suolo attorno all'area: alberi, veicoli e persone come nella foto, "
                                     "senza vortici (riempie anche i buchi del DEM)")
        self.r_dem.setChecked(True)
        sgroup = QButtonGroup(self)
        for r in (self.r_dem, self.r_ground):
            sgroup.addButton(r)
            surfaces.addWidget(r)
            r.toggled.connect(lambda _on: self._surface_changed())
        surfaces.addStretch(1)
        self._surfaces_offered = kind == "ortho" and any(c.get("previews") for c in candidates)
        if self._surfaces_offered:
            lay.addLayout(surfaces)
        else:
            for r in (self.r_dem, self.r_ground):
                r.setVisible(False)
        self.c_texture = QCheckBox("Correggi anche la texture del modello in quest'area")
        self._texture_offered = kind == "ortho" and texture is not None
        self.c_texture.setVisible(self._texture_offered)
        self.c_texture.setChecked(bool(texture))
        lay.addWidget(self.c_texture)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Applica")
        self.ok.setEnabled(False)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        if candidates:
            self.list.setCurrentRow(0)
            self.list.item(0).setSelected(True)
        else:
            self.preview_label.setText("Nessuna foto vede quest'area")

    def _show(self, label: QLabel, rgba) -> None:
        pix = self._pix(rgba)
        label.setPixmap(pix.scaled(self._side, self._side * 3 // 4, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation))

    def surface(self) -> str:
        return "ground" if self.r_ground.isChecked() else "dem"

    def _preview_of(self, label: str):
        c = next((c for c in self.candidates if c["label"] == label), None)
        if c is None:
            return None
        return (c.get("previews") or {}).get(self.surface(), c.get("preview"))

    def _surface_changed(self) -> None:
        surf = self.surface()
        for i in range(self.list.count()):
            item = self.list.item(i)
            ic = self._icons.get((item.data(Qt.ItemDataRole.UserRole), surf))
            if ic is not None:
                item.setIcon(ic)
        self._selection_changed()

    def _selection_changed(self) -> None:
        items = self.list.selectedItems()
        self.ok.setEnabled(bool(items))
        if not items:
            return
        label = items[-1].data(Qt.ItemDataRole.UserRole)
        prev = self._preview_of(label)
        if prev is not None:
            self._show(self.preview_label, prev)
        else:
            self.preview_label.setText(label)

    def values(self) -> dict:
        return {"photos": [it.data(Qt.ItemDataRole.UserRole) for it in self.list.selectedItems()],
                "mode": "exclude" if self.r_exclude.isChecked() else "assign",
                "surface": self.surface() if self._surfaces_offered else "dem",
                "texture": self._texture_offered and self.c_texture.isChecked()}


# ---------------------------------------------------------------- messages and errors
# Native message boxes (NSAlert) crash on recent macOS with Qt 6.x: every message box
# of the interface goes through these helpers, which force the Qt implementation.

def message(parent, kind: str, title: str, text: str, details: str = "", buttons=None, default=None):
    """Non-native message box. ``kind``: info, warning, critical, question. Returns the button."""
    from PySide6.QtWidgets import QMessageBox

    icons = {"info": QMessageBox.Icon.Information, "warning": QMessageBox.Icon.Warning,
             "critical": QMessageBox.Icon.Critical, "question": QMessageBox.Icon.Question}
    box = QMessageBox(icons.get(kind, QMessageBox.Icon.NoIcon), title, text, parent=parent)
    box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    box.setTextFormat(Qt.TextFormat.RichText if text.lstrip().startswith("<") else Qt.TextFormat.PlainText)
    if details:
        box.setDetailedText(details)
    if buttons is not None:
        box.setStandardButtons(buttons)
    if default is not None:
        box.setDefaultButton(default)
    box.exec()
    return box.standardButton(box.clickedButton())


def choice(parent, kind: str, title: str, text: str, options: list[str], default: int = 0,
           cancel: str | None = "Annulla") -> int | None:
    """Non-native message box with buttons named ``options``: the index of the one pressed, or
    ``None`` for ``cancel`` (Esc) or a closed box."""
    from PySide6.QtWidgets import QMessageBox

    icons = {"info": QMessageBox.Icon.Information, "warning": QMessageBox.Icon.Warning,
             "critical": QMessageBox.Icon.Critical, "question": QMessageBox.Icon.Question}
    box = QMessageBox(icons.get(kind, QMessageBox.Icon.NoIcon), title, text, parent=parent)
    box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    buttons = [box.addButton(o, QMessageBox.ButtonRole.AcceptRole) for o in options]
    esc = box.addButton(cancel, QMessageBox.ButtonRole.RejectRole) if cancel else None
    if esc is not None:
        box.setEscapeButton(esc)
    if 0 <= default < len(buttons):
        box.setDefaultButton(buttons[default])
    box.exec()
    clicked = box.clickedButton()
    return buttons.index(clicked) if clicked in buttons else None


_SUGGESTIONS = [
    ("crashed in the worker process", "La ricostruzione e' stata eseguita in un processo separato che si e' interrotto. "
                                      "Il modello viene ricostruito con il metodo alternativo (campo di altezze); "
                                      "con Open3D 0.19 o 0.21+ la Poisson e' piu' stabile."),
    ("mod_spatialite", "Scrivere geometrie in un database SQLite di PyArchInit richiede mod_spatialite: "
                       "installalo (QGIS, Homebrew 'brew install libspatialite', MacPorts) oppure imposta "
                       "SPATIALITE_LIBRARY_PATH."),
    ("No module named 'torch'", "Segment Anything 2.1 richiede PyTorch: pip install \"pyarchinit-photogrammetry[sam]\"."),
    ("__thread_bss", "Le librerie compilate di questo ambiente Python (Anaconda) non si caricano su questa "
                     "versione di macOS: usa l'ambiente virtuale del progetto o l'applicazione pacchettizzata."),
    ("not enough points", "Servono piu' punti: costruisci prima la nuvola densa o usa una qualita' piu' alta."),
    ("photos cannot be read", "Alcune foto non si leggono: un file vuoto (0 byte) di solito e' un file di Dropbox, "
                              "iCloud o OneDrive tenuto «solo online». Rendi la cartella disponibile offline, collega "
                              "le foto a un'altra copia (Flusso di lavoro > Ricollega foto) oppure conferma di procedere "
                              "senza quelle foto."),
    ("Allinea prima", "Esegui Flusso di lavoro > Allinea foto."),
    ("BITMAP_ERROR", "COLMAP non riesce a leggere le foto: controlla che siano JPEG/PNG/TIFF validi e riavvia "
                     "l'interfaccia (la libreria JPEG di sistema e' stata caricata prima di pycolmap)."),
    ("No space left", "Spazio su disco esaurito: libera spazio nella cartella del progetto."),
    ("Permission denied", "Permessi insufficienti sulla cartella: scegli un'altra posizione per il progetto."),
]


def suggestion_for(text: str) -> str | None:
    """A hint in Italian for known error signatures, or None."""
    for key, hint in _SUGGESTIONS:
        if key in text:
            return hint
    return None


class ErrorDialog(QDialog):
    """Error window: summary, optional hint, expandable traceback, copy and log buttons."""

    def __init__(self, title: str, summary: str, details: str = "", suggestion: str | None = None,
                 parent=None, log_path=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QPlainTextEdit, QStyle, QToolButton

        from .icons import icon

        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        self.details = details
        self.summary = summary
        if suggestion is None:
            suggestion = suggestion_for(details or summary)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        pic = QLabel()
        ic = icon("error")
        if ic.isNull():
            ic = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxCritical)
        pic.setPixmap(ic.pixmap(48, 48))
        pic.setAlignment(Qt.AlignmentFlag.AlignTop)
        top.addWidget(pic)
        text = QLabel(summary)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.setObjectName("errorSummary")
        top.addWidget(text, 1)
        lay.addLayout(top)
        if suggestion:
            hint = QLabel(suggestion)
            hint.setWordWrap(True)
            hint.setObjectName("hintBox")
            lay.addWidget(hint)
        self.toggle = QToolButton()
        self.toggle.setText("Dettagli")
        self.toggle.setCheckable(True)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.toggled.connect(self._toggle)
        self.box = QPlainTextEdit(details)
        self.box.setReadOnly(True)
        self.box.setObjectName("tracebackBox")
        self.box.setMinimumHeight(220)
        self.box.hide()
        if details:
            lay.addWidget(self.toggle)
        lay.addWidget(self.box, 1)
        row = QHBoxLayout()
        self.b_copy = QPushButton(icon("copy"), "Copia")
        self.b_copy.clicked.connect(self.copy)
        row.addWidget(self.b_copy)
        self.log_path = log_path
        if log_path is None:
            try:
                from .logging_setup import log_file

                self.log_path = log_file()
            except Exception:  # pragma: no cover
                self.log_path = None
        if self.log_path is not None:
            self.b_log = QPushButton(icon("folder"), "Apri cartella log")
            self.b_log.clicked.connect(self.open_log_folder)
            row.addWidget(self.b_log)
        row.addStretch(1)
        self.b_close = QPushButton("Chiudi")
        self.b_close.setDefault(True)
        self.b_close.clicked.connect(self.accept)
        row.addWidget(self.b_close)
        lay.addLayout(row)

    def _toggle(self, on: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.box.setVisible(on)
        self.adjustSize()

    def report_text(self) -> str:
        from .. import __version__

        return f"pyarchinit-photogrammetry {__version__}\n{self.summary}\n\n{self.details}".strip()

    def copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.report_text())
        self.b_copy.setText("Copiato")

    def open_log_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.log_path).parent)))


# ---------------------------------------------------------------- PyArchInit connection and sites
class ConnectDialog(QDialog):
    """Choose how to reach PyArchInit: its ``config.cfg``, a SQLite file or a PostgreSQL server.

    ``values()`` returns ``{"mode": "config"|"sqlite"|"postgres", "config": path, "url": ...}``;
    the PostgreSQL fields (except the password, unless asked) are remembered in QSettings."""

    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QGroupBox, QRadioButton, QSpinBox

        from ..pyarchinit.config import PyArchInitConfig, pyarchinit_home
        from .icons import icon

        from PySide6.QtWidgets import QButtonGroup

        self.setWindowTitle("Connessione a PyArchInit")
        self.setMinimumWidth(560)
        from . import app_settings

        self.settings = settings or app_settings()
        lay = QVBoxLayout(self)
        self._group = QButtonGroup(self)  # the radio buttons live in different group boxes
        # --- PyArchInit installation
        self.r_config = QRadioButton("Database configurato in PyArchInit (config.cfg)")
        cfg_path = pyarchinit_home() / "pyarchinit_DB_folder" / "config.cfg"
        self.cfg_edit = _PathEdit(Field("config", "config.cfg", str(cfg_path), kind="file_open",
                                        filter="config.cfg (config.cfg);;Tutti (*)"))
        self.cfg_info = QLabel("")
        self.cfg_info.setObjectName("pageSubtitle")
        self.cfg_info.setWordWrap(True)
        g1 = QGroupBox()
        l1 = QVBoxLayout(g1)
        l1.addWidget(self.r_config)
        l1.addWidget(self.cfg_edit)
        l1.addWidget(self.cfg_info)
        lay.addWidget(g1)
        # --- SQLite file
        self.r_sqlite = QRadioButton("File SQLite / SpatiaLite")
        self.sqlite_edit = _PathEdit(Field("sqlite", "Database", str(self.settings.value("pa_sqlite", "")),
                                           kind="file_open", filter="SQLite (*.sqlite *.db *.sqlite3);;Tutti (*)"))
        g2 = QGroupBox()
        l2 = QVBoxLayout(g2)
        l2.addWidget(self.r_sqlite)
        l2.addWidget(self.sqlite_edit)
        lay.addWidget(g2)
        # --- PostgreSQL
        self.r_pg = QRadioButton("PostgreSQL / PostGIS")
        g3 = QGroupBox()
        l3 = QVBoxLayout(g3)
        l3.addWidget(self.r_pg)
        form = QFormLayout()
        self.host = QLineEdit(str(self.settings.value("pa_pg_host", "localhost")))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(self.settings.value("pa_pg_port", 5432)))
        self.dbname = QLineEdit(str(self.settings.value("pa_pg_db", "pyarchinit")))
        self.user = QLineEdit(str(self.settings.value("pa_pg_user", "postgres")))
        self.password = QLineEdit(str(self.settings.value("pa_pg_password", "")))
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.remember = QCheckBox("Ricorda la password (in chiaro nelle impostazioni)")
        self.remember.setChecked(bool(self.settings.value("pa_pg_password", "")))
        self.sslmode = QComboBox()
        for m in ("allow", "prefer", "require", "disable"):
            self.sslmode.addItem(m, m)
        self.sslmode.setCurrentText(str(self.settings.value("pa_pg_sslmode", "allow")))
        form.addRow("Host", self.host)
        form.addRow("Porta", self.port)
        form.addRow("Database", self.dbname)
        form.addRow("Utente", self.user)
        form.addRow("Password", self.password)
        form.addRow("SSL", self.sslmode)
        form.addRow("", self.remember)
        l3.addLayout(form)
        lay.addWidget(g3)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.b_test = QPushButton(icon("ok"), "Prova connessione")
        self.b_test.clicked.connect(self.test)
        bb.addButton(self.b_test, QDialogButtonBox.ButtonRole.ActionRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for r in (self.r_config, self.r_sqlite, self.r_pg):
            self._group.addButton(r)
        mode = str(self.settings.value("pa_mode", "config"))
        {"config": self.r_config, "sqlite": self.r_sqlite, "postgres": self.r_pg}.get(mode, self.r_config).setChecked(True)
        if cfg_path.exists():
            try:
                c = PyArchInitConfig.load(cfg_path)
                self.cfg_info.setText(f"{'PostgreSQL ' + c.host + '/' + c.database if c.is_postgres else c.sqlite_path}"
                                      f"  -  sito corrente: {c.site_set or '-'}")
            except Exception as exc:
                self.cfg_info.setText(f"config.cfg non leggibile: {exc}")
        else:
            self.cfg_info.setText("config.cfg non trovato: indica il file di PyArchInit o usa un'altra connessione")

    def values(self) -> dict:
        if self.r_config.isChecked():
            return {"mode": "config", "config": self.cfg_edit.value(), "url": None}
        if self.r_sqlite.isChecked():
            return {"mode": "sqlite", "config": None, "url": self.sqlite_edit.value()}
        from urllib.parse import quote_plus

        url = (f"postgresql://{quote_plus(self.user.text())}:{quote_plus(self.password.text())}@{self.host.text()}:"
               f"{self.port.value()}/{self.dbname.text()}?sslmode={self.sslmode.currentData()}")
        return {"mode": "postgres", "config": None, "url": url}

    def remember_settings(self) -> None:
        v = self.values()
        self.settings.setValue("pa_mode", v["mode"])
        if v["mode"] == "sqlite":
            self.settings.setValue("pa_sqlite", v["url"])
        elif v["mode"] == "postgres":
            for key, val in (("pa_pg_host", self.host.text()), ("pa_pg_port", self.port.value()),
                             ("pa_pg_db", self.dbname.text()), ("pa_pg_user", self.user.text()),
                             ("pa_pg_sslmode", self.sslmode.currentData())):
                self.settings.setValue(key, val)
            self.settings.setValue("pa_pg_password", self.password.text() if self.remember.isChecked() else "")

    def test(self) -> None:
        from ..pyarchinit import PyArchInit

        v = self.values()
        try:
            pa = PyArchInit.connect(url=v["url"], config_path=v["config"])
            sites = pa.sites()
            self.status.setText(f"Connessione riuscita: {len(sites)} siti"
                                + (f" ({', '.join(str(s['sito']) for s in sites[:4])}...)" if sites else "")
                                + ("" if pa.db.is_postgres or pa.db.spatialite else
                                   "  -  mod_spatialite non trovato: lettura ok, scrittura di geometrie no"))
        except Exception as exc:
            self.status.setText(f"Connessione non riuscita: {str(exc).splitlines()[0][:200]}")

    def accept(self) -> None:
        self.remember_settings()
        super().accept()


class SiteDialog(QDialog):
    """Fields of a new PyArchInit site record (``site_table``)."""

    def __init__(self, parent=None, name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Nuovo sito")
        self.setMinimumWidth(460)
        form = QFormLayout(self)
        self.sito = QLineEdit(name)
        self.nazione = QLineEdit("Italia")
        self.regione = QLineEdit("")
        self.provincia = QLineEdit("")
        self.comune = QLineEdit("")
        self.definizione = QLineEdit("scavo stratigrafico")
        self.descrizione = QLineEdit("")
        for label, w in (("Sito", self.sito), ("Nazione", self.nazione), ("Regione", self.regione),
                         ("Provincia", self.provincia), ("Comune", self.comune), ("Definizione", self.definizione),
                         ("Descrizione", self.descrizione)):
            form.addRow(label, w)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def values(self) -> dict:
        return {"sito": self.sito.text().strip(), "nazione": self.nazione.text().strip(),
                "regione": self.regione.text().strip(), "provincia": self.provincia.text().strip(),
                "comune": self.comune.text().strip(), "definizione_sito": self.definizione.text().strip(),
                "descrizione": self.descrizione.text().strip()}


class IfcPreviewDialog(QDialog):
    """Result of an IFC export: the elements with their PyArchInit attributes and a 3D view."""

    def __init__(self, path, elements: list[dict], parent=None):
        super().__init__(parent)
        import numpy as np
        from PySide6.QtWidgets import QSplitter, QTableWidget, QTableWidgetItem

        from .model_view import SoftwareCanvas
        from .scene import SceneData

        self.setWindowTitle("IFC esportato")
        self.resize(1100, 680)
        lay = QVBoxLayout(self)
        n_faces = sum(len(e["faces"]) for e in elements)
        head = QLabel(f"<b>{Path(path).name}</b> - {len(elements)} elementi, {n_faces:,} triangoli<br>{path}")
        head.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(head)
        split = QSplitter()
        table = QTableWidget(len(elements), 4)
        table.setHorizontalHeaderLabels(["Elemento", "Classe IFC", "Triangoli", "Attributi"])
        palette = [(205, 205, 205), (230, 80, 80), (80, 160, 230), (90, 200, 110), (240, 180, 60), (160, 100, 220)]
        for r, e in enumerate(elements):
            props = {k: v for name, pset in e["psets"].items() if not name.startswith("Qto") for k, v in pset.items()}
            qto = e["psets"].get("Qto_PyArchInit_US", {})
            q = (f"V {qto['NetVolume']:.3f} m3, A {qto.get('GrossArea', 0):.2f} m2; " if "NetVolume" in qto else "")
            vals = [e["name"], e["ifc_class"], f"{len(e['faces']):,}",
                    q + ", ".join(f"{k}={v}" for k, v in list(props.items())[:6])]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 0:
                    from PySide6.QtGui import QColor

                    it.setForeground(QColor(*palette[r % len(palette)]) if r else QColor("#555"))
                table.setItem(r, c, it)
        table.resizeColumnsToContents()
        split.addWidget(table)
        self.canvas = SoftwareCanvas()
        self.canvas.show_cameras = False
        self.canvas.show_markers = False
        if elements:
            show = elements[1:] if len(elements) > 1 else elements  # the per-US elements, not the whole survey
            V, F, C, off = [], [], [], 0
            for i, e in enumerate(show, 1 if len(elements) > 1 else 0):
                V.append(e["vertices"]); F.append(e["faces"] + off)
                C.append(np.repeat(np.array([palette[i % len(palette)]], np.uint8), len(e["vertices"]), 0))
                off += len(e["vertices"])
            V = np.vstack(V)
            centre = V.mean(0)
            sd = SceneData(centre, kind="model", mesh_vertices=V - centre, mesh_faces=np.vstack(F),
                           mesh_colors=np.vstack(C), up=np.array([0.0, 0.0, 1.0]))
            self.canvas.set_scene(sd)
            self.canvas.cam.pitch = 50
            self.canvas.cam.distance *= 1.6  # whole set of elements in view
        split.addWidget(self.canvas)
        split.setSizes([480, 620])
        lay.addWidget(split, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        b_dir = QPushButton("Apri cartella")
        b_dir.clicked.connect(lambda: __import__("PySide6.QtGui", fromlist=["QDesktopServices"]).QDesktopServices.openUrl(
            __import__("PySide6.QtCore", fromlist=["QUrl"]).QUrl.fromLocalFile(str(Path(path).parent))))
        bb.addButton(b_dir, QDialogButtonBox.ButtonRole.ActionRole)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


# ---------------------------------------------------------------- PyArchInit US form
US_TYPES = ["US", "USM", "USR", "USD", "USV", "USVs", "USVn", "SF", "VSF", "CON", "DOC", "TSU", "SE"]
US_FORM_PAGES = [
    ("Descrizione", [("d_stratigrafica", "Definizione stratigrafica"), ("d_interpretativa", "Definizione interpretativa"),
                     ("descrizione", "Descrizione*"), ("interpretazione", "Interpretazione*"),
                     ("osservazioni", "Osservazioni*")]),
    ("Cronologia", [("periodo_iniziale", "Periodo iniziale"), ("fase_iniziale", "Fase iniziale"),
                    ("periodo_finale", "Periodo finale"), ("fase_finale", "Fase finale"), ("datazione", "Datazione"),
                    ("elem_datanti", "Elementi datanti*"), ("affidabilita", "Affidabilita'")]),
    ("Dati di scavo", [("anno_scavo", "Anno di scavo"), ("data_schedatura", "Data schedatura"),
                       ("schedatore", "Schedatore"), ("direttore_us", "Direttore"), ("responsabile_us", "Responsabile"),
                       ("metodo_di_scavo", "Metodo di scavo"), ("scavato", "Scavato"), ("attivita", "Attivita'"),
                       ("settore", "Settore"), ("saggio", "Saggio"), ("ambient", "Ambiente")]),
    ("Caratteri", [("formazione", "Formazione"), ("modo_formazione", "Modo di formazione"),
                   ("stato_di_conservazione", "Stato di conservazione"), ("colore", "Colore"),
                   ("consistenza", "Consistenza"), ("criteri_distinzione", "Criteri di distinzione*")]),
    ("Quote e misure", [("quota_min_abs", "Quota minima assoluta"), ("quota_max_abs", "Quota massima assoluta"),
                        ("quota_min_rel", "Quota minima relativa"), ("quota_max_rel", "Quota massima relativa"),
                        ("lunghezza_max", "Lunghezza max"), ("larghezza_media", "Larghezza media"),
                        ("altezza_max", "Altezza max"), ("altezza_min", "Altezza min"),
                        ("profondita_max", "Profondita' max"), ("profondita_min", "Profondita' min")]),
]


class _SiteCombo(QComboBox):
    """Editable list of the sites of the database, with ``text()`` like a line edit."""

    def __init__(self, pa, sito: str):
        super().__init__()
        self.setEditable(True)
        try:
            names = [str(r.get("sito")) for r in pa.sites() if r.get("sito")]
        except Exception:
            names = []
        if sito and sito not in names:
            names.insert(0, sito)
        self.addItems(names)
        self.setCurrentText(sito)

    def text(self) -> str:
        return self.currentText()

    def setText(self, text: str) -> None:
        self.setCurrentText(text)


class UsFormDialog(QDialog):
    """The US form of PyArchInit (us_table): open, edit or create a record. Only the columns
    present in the database are shown; text fields marked * are multi-line."""

    def __init__(self, pa, sito: str, area: str = "1", us: str = "", unita_tipo: str = "US", parent=None,
                 defaults: dict | None = None):
        super().__init__(parent)
        from PySide6.QtWidgets import QTableWidget, QTabWidget

        from ..pyarchinit.bridge import parse_relationships

        self.pa = pa
        self.cols = pa.us_columns()
        self.record = pa.us_record(sito, area, us, unita_tipo) if us else None
        self.setWindowTitle(("Scheda US - " if self.record else "Nuova US - ") + sito)
        self.resize(760, 640)
        lay = QVBoxLayout(self)
        head = QFormLayout()
        self.sito = _SiteCombo(pa, sito)  # the site of the project; the other sites of the database to choose from
        self.area = QLineEdit(str(area))
        self.us = QLineEdit(str(us))
        self.tipo = QComboBox()
        self.tipo.setEditable(True)
        self.tipo.addItems(US_TYPES)
        self.tipo.setCurrentText(unita_tipo or "US")
        for w in (self.area, self.us):
            w.setReadOnly(self.record is not None)
        self.sito.setEnabled(self.record is None)
        self.tipo.setEnabled(self.record is None)
        row = QHBoxLayout()
        for label, w in (("Sito", self.sito), ("Area", self.area), ("US", self.us), ("Tipo", self.tipo)):
            row.addWidget(QLabel(label))
            row.addWidget(w, 2 if label == "Sito" else 1)
        lay.addLayout(row)
        # an older or reduced us_table (e.g. the demo database of version 0.2) lacks fields of the
        # PyArchInit 5 form: say so and offer to add them instead of showing empty pages
        self.banner = QWidget()
        self.banner.setObjectName("warningBanner")
        self.banner.setStyleSheet("#warningBanner { background: rgba(245, 158, 11, 0.16); "
                                  "border: 1px solid rgba(245, 158, 11, 0.6); border-radius: 6px; }")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(10, 6, 10, 6)
        self.banner_text = QLabel("")
        self.banner_text.setWordWrap(True)
        self.b_upgrade = QPushButton("Aggiorna struttura del database")
        self.b_upgrade.clicked.connect(lambda _=False: self.upgrade_structure())
        bl.addWidget(self.banner_text, 1)
        bl.addWidget(self.b_upgrade)
        lay.addWidget(self.banner)
        self.tabs = QTabWidget()
        self.fields: dict[str, QWidget] = {}
        data = dict(self.record or {})
        data.update({k: v for k, v in (defaults or {}).items() if not data.get(k)})
        # relationships: the table is kept across rebuilds of the pages
        self.rel_page = QWidget()
        rl = QVBoxLayout(self.rel_page)
        self.rel = QTableWidget(0, 4)
        self.rel.setHorizontalHeaderLabels(["Rapporto", "US", "Area", "Sito"])
        self.rel.horizontalHeader().setStretchLastSection(True)
        self.rel.setColumnWidth(0, 190)
        for r in parse_relationships(data.get("rapporti")):
            self._add_rel(r)
        btns = QHBoxLayout()
        b_add, b_del = QPushButton("Aggiungi rapporto"), QPushButton("Rimuovi")
        b_add.clicked.connect(lambda: self._add_rel(["Copre", "", self.area.text(), self.sito.text()]))
        b_del.clicked.connect(lambda: self.rel.removeRow(self.rel.currentRow()) if self.rel.currentRow() >= 0 else None)
        btns.addWidget(b_add)
        btns.addWidget(b_del)
        btns.addStretch(1)
        rl.addWidget(self.rel, 1)
        rl.addLayout(btns)
        lay.addWidget(self.tabs, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._build_pages(data)
        self.saved_id: int | None = None

    def _build_pages(self, data: dict) -> None:
        """Pages of the form for the columns the database has; pages without any are left out."""
        from PySide6.QtWidgets import QPlainTextEdit

        while self.tabs.count():
            page = self.tabs.widget(0)
            self.tabs.removeTab(0)
            if page is not self.rel_page:
                page.deleteLater()
        self.fields = {}
        for page, fields in US_FORM_PAGES:
            present = [(n, label) for n, label in fields if n in self.cols]
            if not present:
                continue
            w = QWidget()
            form = QFormLayout(w)
            for name, label in present:
                multi = label.endswith("*")
                val = "" if data.get(name) is None else str(data.get(name))
                edit = QPlainTextEdit(val) if multi else QLineEdit(val)
                if multi:
                    edit.setMaximumHeight(90)
                form.addRow(label.rstrip("*"), edit)
                self.fields[name] = edit
            self.tabs.addTab(w, page)
        if "rapporti" in self.cols:
            self.tabs.addTab(self.rel_page, "Rapporti")
        try:
            self.missing = self.pa.missing_us_columns()
        except Exception:  # a database without the reference columns at all: nothing to offer
            self.missing = []
        if self.missing:
            what = ", tra cui i rapporti stratigrafici" if "rapporti" in self.missing else ""
            copy = (" Prima viene fatta una copia di sicurezza del file." if self.pa.db.sqlite_path is not None
                    else " Su PostgreSQL fai prima un backup del database.")
            self.banner_text.setText(f"Questo database ha una scheda US ridotta (struttura di una versione precedente): "
                                     f"mancano {len(self.missing)} campi di PyArchInit 5{what}. «Aggiorna struttura» "
                                     f"li aggiunge senza modificare i dati.{copy}")
        self.banner.setVisible(bool(self.missing))

    def _texts(self) -> dict:
        out = {}
        for name, w in self.fields.items():
            text = w.toPlainText() if hasattr(w, "toPlainText") else w.text()
            if text.strip():
                out[name] = text
        return out

    def upgrade_structure(self, confirm: bool = True) -> bool:
        """Add the missing PyArchInit 5 columns (see ``PyArchInit.upgrade_us_table``) and show
        the complete form, keeping what was typed."""
        from PySide6.QtWidgets import QMessageBox

        if confirm:
            ans = message(self, "question", "Aggiorna struttura del database",
                          f"Aggiungere a us_table i {len(self.missing)} campi mancanti della scheda US di PyArchInit 5? "
                          "I record esistenti non vengono modificati.",
                          buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                          default=QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return False
        typed = self._texts()
        try:
            res = self.pa.upgrade_us_table()
        except Exception as exc:
            self.status.setText(f"Aggiornamento non riuscito: {exc}")
            return False
        self.cols = self.pa.us_columns()
        if self.record:
            self.record = self.pa.us_record(self.record["sito"], self.record["area"], self.record["us"],
                                            self.record.get("unita_tipo")) or self.record
        data = dict(self.record or {})
        data.update(typed)
        self._build_pages(data)
        msg = f"Aggiunti {len(res['added'])} campi alla scheda US."
        if res.get("backup"):
            msg += f" Copia di sicurezza: {Path(res['backup']).name}"
        self.status.setText(msg)
        return True

    def _add_rel(self, r) -> None:
        from PySide6.QtWidgets import QTableWidgetItem

        from ..pyarchinit.bridge import PyArchInit

        i = self.rel.rowCount()
        self.rel.insertRow(i)
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(PyArchInit.RELATIONS)
        combo.setCurrentText(r[0])
        self.rel.setCellWidget(i, 0, combo)
        for c, v in enumerate(r[1:4], 1):
            self.rel.setItem(i, c, QTableWidgetItem(v))

    def relationships(self) -> list[list[str]]:
        out = []
        for i in range(self.rel.rowCount()):
            rel = self.rel.cellWidget(i, 0).currentText().strip()
            vals = [(self.rel.item(i, c).text().strip() if self.rel.item(i, c) else "") for c in (1, 2, 3)]
            if rel and vals[0]:
                out.append([rel] + vals)
        return out

    def values(self) -> dict:
        out = {}
        for name, w in self.fields.items():
            text = w.toPlainText() if hasattr(w, "toPlainText") else w.text()
            out[name] = text.strip() or None
        if "rapporti" in self.cols:
            out["rapporti"] = self.relationships()
        return out

    def save(self) -> None:
        vals = self.values()
        try:
            if self.record:
                self.pa.update_us(int(self.record["id_us"]), **vals)
                self.saved_id = int(self.record["id_us"])
            else:
                if not self.us.text().strip():
                    self.status.setText("Indica il numero di US")
                    return
                self.saved_id = self.pa.create_us(self.sito.text().strip(), self.area.text().strip(),
                                                  self.us.text().strip(), self.tipo.currentText().strip() or "US", **vals)
        except Exception as exc:
            self.status.setText(f"Salvataggio non riuscito: {exc}")
            return
        self.accept()


class TableDialog(QDialog):
    """Results as a table (numbers right aligned) with *Salva CSV...* when ``save(path)`` is given."""

    def __init__(self, title: str, headers: list[str], rows: list[list], parent=None, intro: str = "",
                 save=None, csv_name: str = "tabella.csv"):
        from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(560, 320)
        self._save, self._csv_name = save, csv_name
        lay = QVBoxLayout(self)
        if intro:
            lab = QLabel(intro)
            lab.setWordWrap(True)
            lay.addWidget(lab)
        self.table = QTableWidget(len(rows), len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                text = "" if value is None else str(value)
                if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= 10000:
                    text = f"{value:,}".replace(",", ".")  # 114.835 points
                it = QTableWidgetItem(text)
                if isinstance(value, (int, float)):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, it)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        if save is not None:
            b = bb.addButton("Salva CSV...", QDialogButtonBox.ButtonRole.ActionRole)
            b.clicked.connect(self._save_csv)
        lay.addWidget(bb)
        self.status = QLabel("")
        lay.addWidget(self.status)

    def _save_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Salva CSV", self._csv_name, "CSV (*.csv)")
        if path:
            try:
                self.status.setText(f"Salvato {self._save(path)}")
            except Exception as exc:
                self.status.setText(f"Salvataggio non riuscito: {exc}")
