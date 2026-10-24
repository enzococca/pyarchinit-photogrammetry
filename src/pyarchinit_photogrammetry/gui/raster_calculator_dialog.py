"""The raster calculator of the window (*Strumenti > Calcolatore raster*): the rasters of the active
chunk with their names in the formulas (those of the plugins too), the formula checked while it is
written, the name of the result, its grid and resolution (:mod:`..engine.rastercalc`)."""
from __future__ import annotations

from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHeaderView, QLabel, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..engine import rastercalc
from .i18n import tr


def say(exc: Exception) -> str:
    """What is wrong with a formula (:class:`~..engine.rastercalc.FormulaError`) in the language of the
    window."""
    code = getattr(exc, "code", None)
    v = getattr(exc, "values", {}) or {}
    if code == "empty":
        return tr("Scrivi una formula.")
    if code == "syntax":
        return tr("La formula non si legge ({0}).", v.get("detail", ""))
    if code == "unknown":
        near = tr(" (forse {0}?)", v["near"]) if v.get("near") else ""
        return tr("Nessun raster si chiama {0}{1}.", v.get("name", ""), near)
    if code == "bands":
        return tr("{0} ha {1} bande: scrivi {0}@1 ... {0}@{1}.", v.get("name", ""), v.get("bands", ""))
    if code == "band_number":
        return tr("{0} ha {1} bande: {0}@{2} non esiste.", v.get("name", ""), v.get("bands", ""), v.get("band", ""))
    if code == "band_name":
        known = tr(" (bande: {0})", v["known"]) if v.get("known") else ""
        return tr("{0} non ha la banda {1}{2}.", v.get("name", ""), v.get("band", ""), known)
    if code == "and_or":
        return tr("Scrivi AND, OR (o and, or) invece di & e |.")
    if code == "not":
        return tr("Scrivi NOT (o not) invece di ~.")
    if code == "operator":
        return tr("L'operatore {0} non c'è nelle formule.", v.get("operator", ""))
    if code == "compare":
        return tr("Confronta solo con < <= > >= = <>.")
    if code == "function":
        return tr("{0} non è una funzione delle formule ({1}).", v.get("function", ""),
                  ", ".join(sorted(rastercalc.FUNCTIONS)))
    if code == "keywords":
        return tr("{0}: dai i suoi valori in ordine, senza nomi.", v.get("function", ""))
    if code == "arguments":
        least, most = v.get("least", 1), v.get("most")
        want = str(least) if least == most else tr("almeno {0}", least) if most is None else tr("da {0} a {1}", least, most)
        return tr("{0} vuole {1} valori.", v.get("function", ""), want)
    if code == "is_function":
        return tr("{0} è una funzione: {0}(...).", v.get("function", ""))
    if code == "numbers":
        return tr("Una formula ha solo numeri e raster.")
    if code == "not_allowed":
        return tr("Non può stare in una formula: {0}.", v.get("what", ""))
    if code == "name":
        return tr("{0}: un nome di lettere, cifre e _, che non comincia con una cifra.", v.get("name", ""))
    if code == "name_taken":
        return tr("{0}: c'è già un raster del chunk con questo nome.", v.get("name", ""))
    if code == "rotated":
        return tr("Un'altra risoluzione vuole come griglia un raster non ruotato.")
    if code == "grid":
        return tr("Nessun raster si chiama {0} per la griglia.", v.get("name", ""))
    if code == "no_grid":
        return tr("Una formula senza raster vuole un raster per la griglia.")
    return str(exc)


def _bands(r) -> str:
    if len(r.bands) == 1:
        return r.bands[0] or "1"
    return ", ".join(f"@{k}" + (f" {b}" if b else "") for k, b in enumerate(r.bands, 1))


class RasterCalculatorDialog(QDialog):
    """``catalog``: the rasters of the chunk (:func:`~..engine.rastercalc.rasters`); :meth:`values`
    after ``exec``: formula, name, grid (None: the first raster of the formula) and resolution (None:
    of the grid)."""

    def __init__(self, catalog, parent=None, formula: str = "", name: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("Calcolatore raster"))
        self.resize(820, 600)
        self.rasters = list(catalog)
        self.catalog = {r.name: r for r in self.rasters}
        lay = QVBoxLayout(self)
        intro = QLabel(tr("Formule come nel calcolatore raster di QGIS sui raster del chunk, anche quelli dei plugin: "
                          "una banda come dem@1 o \"orthomosaic@2\" (o col nome della banda, reflectance@NIR), un "
                          "raster di una banda anche col nome solo; + - * / ^, i confronti < <= > >= = <> (1 dove è "
                          "vero, 0 dove è falso), AND OR NOT, if(condizione, a, b) e le funzioni {0}. Una cella senza "
                          "dati in un raster resta senza dati nel risultato (isnan e coalesce per riempirla). Doppio "
                          "clic su un raster per scriverlo nella formula.", ", ".join(sorted(rastercalc.FUNCTIONS))))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.table = QTableWidget(len(self.rasters), 4)
        self.table.setHorizontalHeaderLabels([tr("Nome nella formula"), tr("Raster"), tr("Bande"), tr("Celle")])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        for i, r in enumerate(self.rasters):
            source = tr(rastercalc.SOURCES.get(r.source, r.source))
            cells = f"{r.width} x {r.height}, {r.resolution:.4g} m"
            for j, text in enumerate((r.name, f"{tr(r.label)}  [{source}]", _bands(r), cells)):
                item = QTableWidgetItem(text)
                item.setToolTip(str(r.path))
                self.table.setItem(i, j, item)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.resizeColumnToContents(0)
        self.table.cellDoubleClicked.connect(self._insert)
        lay.addWidget(self.table, 1)
        form = QFormLayout()
        self.formula = QLineEdit(formula)
        self.formula.setPlaceholderText(tr("per esempio: dem - dem_prima_dello_scavo"))
        form.addRow(tr("Formula"), self.formula)
        self.state = QLabel()
        self.state.setWordWrap(True)
        form.addRow("", self.state)
        self.name = QLineEdit(name or self._free_name())
        form.addRow(tr("Nome del risultato"), self.name)
        self.grid = QComboBox()
        self.grid.addItem(tr("Del primo raster della formula"), None)
        for r in self.rasters:
            self.grid.addItem(f"{r.name}  ({r.width} x {r.height}, {r.resolution:.4g} m)", r.name)
        form.addRow(tr("Griglia del risultato"), self.grid)
        self.resolution = QDoubleSpinBox()
        self.resolution.setDecimals(4)
        self.resolution.setRange(0.0, 10000.0)
        self.resolution.setSingleStep(0.01)
        self.resolution.setSpecialValueText(tr("quella della griglia"))
        form.addRow(tr("Risoluzione (m)"), self.resolution)
        lay.addLayout(form)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        for signal in (self.formula.textChanged, self.name.textChanged):
            signal.connect(self._check)
        self.grid.currentIndexChanged.connect(self._check)
        self._check()

    @classmethod
    def ask(cls, catalog, parent=None, formula: str = "", name: str | None = None) -> dict | None:
        """The dialog shown until the user confirms (:meth:`values`) or cancels (None)."""
        d = cls(catalog, parent, formula, name)
        return d.values() if d.exec() == QDialog.DialogCode.Accepted else None

    def _free_name(self) -> str:
        k = 1
        while f"calcolo_{k}" in self.catalog:
            k += 1
        return f"calcolo_{k}"

    def _insert(self, row: int, _col: int = 0) -> None:
        r = self.rasters[row]
        self.formula.insert(r.name if len(r.bands) == 1 else f"{r.name}@1")
        self.formula.setFocus()

    def _check(self) -> bool:
        """The formula and the name checked: what the result reads and where, or what is wrong (OK only
        when nothing is)."""
        try:
            f = rastercalc.parse(self.formula.text(), self.catalog)
            rastercalc.check_name(None, self.name.text(), self.rasters)
            grid = self.grid.currentData() or (f.inputs[0][0].name if f.inputs else None)
            if grid is None:
                raise rastercalc.FormulaError("no_grid", "")
            uses = ", ".join(f"{r.name}@{b}" if len(r.bands) > 1 else r.name for r, b in f.inputs)
            text, ok = tr("Legge {0}; il risultato sulla griglia di {1}.", uses or "-", grid), True
        except ValueError as exc:
            text, ok = say(exc), False
        self.state.setText(text)
        self.state.setStyleSheet("" if ok else "color: #c0392b;")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)
        return ok

    def values(self) -> dict:
        res = self.resolution.value()
        return {"formula": self.formula.text().strip(), "name": self.name.text().strip(),
                "grid": self.grid.currentData(), "resolution": res if res > 0 else None}
