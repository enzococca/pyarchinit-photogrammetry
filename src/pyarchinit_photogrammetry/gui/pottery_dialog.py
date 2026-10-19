"""Strumenti > Profilo ceramico da mesh: the drawing of a vessel or of a sherd with its
measurements (:func:`archaeo.pottery.measure`), the forms of PyArchInit's ``pottery_table`` most
like it (:meth:`PyArchInit.search_pottery`) and the save of the measurements and of the drawing
in a form (:meth:`PyArchInit.save_pottery`). A window that stays open beside the main one: the
search and the save are asked of the main window."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
                               QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..archaeo.pottery import percent_class
from .i18n import number, tr

COMPARED = {"diametro_rim": "orlo", "diametro_max": "massimo", "diametro_bottom": "fondo", "diametro_height": "altezza",
            "profilo": "profilo"}
DIMENSIONS = ("diametro_rim", "diametro_max", "diametro_bottom", "diametro_height")
HEADERS = ["Somiglianza", "Sito", "Area", "US", "N.", "Forma", "Parte", "Forma specifica", "Classe", "Ø orlo (cm)",
           "Ø max (cm)", "Ø fondo (cm)", "Altezza (cm)", "Confronto su"]


def measure_rows(meas: dict) -> list[tuple[str, str]]:
    """``(label, value)`` of the measurements of a vessel (:func:`archaeo.pottery.measure`), in
    centimetres as in PyArchInit's form."""
    def cm(key):
        return number(meas[key] * 100.0, 1) + " cm"

    rows = []
    if meas.get("rim_diameter_m"):
        rows.append((tr("Diametro orlo"), cm("rim_diameter_m") + ("" if meas.get("rim_reliable") else
                                                                  tr(" (incerto: arco breve o irregolare)"))))
        pct = meas.get("rim_preserved_pct")
        if pct is not None:
            rows.append((tr("Orlo conservato"), f"{number(pct, 0)}% ({percent_class(pct)})"))
    rows.append((tr("Diametro massimo"), cm("max_diameter_m")))
    if meas.get("base_diameter_m"):
        rows.append((tr("Diametro fondo"), cm("base_diameter_m")))
    rows.append((tr("Altezza totale") if meas.get("full_profile") else tr("Altezza conservata"), cm("height_m")))
    if meas.get("wall_thickness_m"):
        rows.append((tr("Spessore parete"), number(meas["wall_thickness_m"] * 1000.0, 1) + " mm"))
    return rows


def _item(text, value=None, right: bool = False) -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    if value is not None:
        it.setData(Qt.ItemDataRole.UserRole, value)
    if right:
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return it


class PotteryDialog(QDialog):
    """See the module docstring."""

    search_requested = Signal()
    save_requested = Signal(object)  # id_rep of the form chosen among the similar ones (None: none chosen)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Profilo ceramico"))
        self.setMinimumSize(760, 560)
        self.measures: dict = {}
        self.matches: list[dict] = []
        self._pixmap: QPixmap | None = None
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.view = QLabel()
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setStyleSheet("background: white;")
        self.view.setMinimumSize(360, 280)
        # the drawing follows the size of the window (a pixmap would otherwise make the label grow)
        self.view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        top.addWidget(self.view, 3)
        self.info = QLabel()
        self.info.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info.setWordWrap(True)
        top.addWidget(self.info, 2)
        lay.addLayout(top, 3)
        self.caption = QLabel(tr("Forme simili in PyArchInit (prima le piu' simili)"))
        lay.addWidget(self.caption)
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels([tr(h) for h in HEADERS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table, 2)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.close)
        self.b_search = bb.addButton(tr("Cerca forme simili"), QDialogButtonBox.ButtonRole.ActionRole)
        self.b_search.clicked.connect(self.search_requested)
        self.b_save = bb.addButton(tr("Salva in PyArchInit..."), QDialogButtonBox.ButtonRole.ActionRole)
        self.b_save.clicked.connect(lambda: self.save_requested.emit(self.chosen()))
        lay.addWidget(bb)

    # ------------------------------------------------------------ content
    def set_result(self, image: str | Path, measures: dict, files, connected: bool) -> None:
        """A new vessel: its drawing (an image), its measurements and the files written."""
        self.measures = measures
        self._pixmap = QPixmap(str(image))
        self._fit()
        rows = "".join(f"<tr><td>{label}&nbsp;&nbsp;</td><td><b>{value}</b></td></tr>"
                       for label, value in measure_rows(measures))
        names = "<br>".join(Path(f).name for f in files)
        self.info.setText(f"<table>{rows}</table><p>{tr('Disegno salvato in')}<br>{Path(files[0]).parent}<br>"
                          f"{names}</p>")
        self.set_matches([])
        self.set_connected(connected)

    def set_connected(self, connected: bool) -> None:
        self.b_search.setEnabled(connected)
        self.b_save.setEnabled(connected)
        self.status.setText("" if connected else tr("Connetti PyArchInit (menu PyArchInit > Connetti) per cercare le "
                                                    "forme simili e salvare misure e disegno nella scheda ceramica."))

    def set_matches(self, rows: list[dict], searched: bool = False) -> None:
        self.matches = rows
        self.table.setRowCount(0)
        for m in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            values = [number(m["similarity_pct"], 0) + "%", m.get("sito"), m.get("area"), m.get("us"),
                      m.get("id_number"), m.get("form"), m.get("specific_form"), m.get("specific_shape"), m.get("ware")]
            for c, v in enumerate(values):
                self.table.setItem(r, c, _item(v, m["id_rep"] if c == 0 else None, right=c == 0))
            for c, key in enumerate(DIMENSIONS, len(values)):
                v = m.get(key)
                self.table.setItem(r, c, _item("" if v is None else number(v, 1), right=True))
            self.table.setItem(r, len(HEADERS) - 1, _item(", ".join(tr(COMPARED.get(k, k)) for k in m["compared"])))
        if searched:
            self.status.setText(tr("{0} forme confrontabili", len(rows)) if rows else
                                tr("Nessuna scheda ceramica con misure confrontabili"))

    def chosen(self) -> int | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        return int(self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole))

    # ------------------------------------------------------------ drawing
    def _fit(self) -> None:
        if self._pixmap is not None and not self._pixmap.isNull():
            self.view.setPixmap(self._pixmap.scaled(self.view.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()
