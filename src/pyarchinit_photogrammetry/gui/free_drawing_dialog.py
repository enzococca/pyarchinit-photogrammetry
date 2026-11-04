"""The form of a free drawing in 3D (:mod:`..core.free_drawings`): its name, layer (one of those of the chunk or a new
one), colour, attributes (key / value rows) and vertices (x, y read-only, z typed by the user), with its measures from
:func:`..archaeo.measures.feature_measures` (read-only). Opened by a double click on the drawing in the 2D or the 3D
view and by *Proprietà...*; :meth:`FreeDrawingDialog.ask` gives the values to the window, which writes them as one
step of *Modifica > Annulla*. Since 1.7.2."""
from __future__ import annotations

import logging

import numpy as np
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ..core import free_drawings
from .i18n import decimal_comma, tr

log = logging.getLogger(__name__)


def model_in_chunk(chunk):
    """The model of ``chunk`` as a trimesh in the chunk's coordinates (as the free drawings are), None without one."""
    path = chunk.product_path("model") if chunk is not None else None
    if path is None or not path.exists():
        return None
    import trimesh

    from ..io.mesh_ply import read_mesh

    m = read_mesh(path)
    return trimesh.Trimesh(chunk.to_world(np.asarray(m.xyz, np.float64)), m.faces, process=False)


def measures_text(rec: dict, model=None, toward=None) -> str:
    """The measures of the free drawing ``rec`` in words; "-" when they cannot be taken (too few vertices, all on one
    line)."""
    from ..archaeo.measures import feature_measures

    try:
        m = feature_measures(rec, model=model, toward=toward)
    except ValueError:
        return "-"
    except Exception as exc:  # noqa: BLE001 - a model that cannot be measured: the drawing's own measures
        log.warning("Misure sul modello non riuscite: %s", exc)
        try:
            m = feature_measures(rec)
        except ValueError:
            return "-"
    if rec["kind"] == "line":
        text = tr("Lunghezza 3D {0} m, in pianta {1} m, dislivello {2} m", f"{m['length_3d']:.3f}",
                  f"{m['length_plan']:.3f}", f"{m['dz']:+.3f}")
    elif rec["kind"] == "polygon":
        text = tr("Area 3D {0} m², in pianta {1} m², perimetro 3D {2} m, pendenza {3}°", f"{m['area_3d']:.3f}",
                  f"{m['area_plan']:.3f}", f"{m['perimeter_3d']:.3f}", f"{m['slope']:.1f}")
        if m.get("aspect", -1) >= 0:
            text += tr(", esposizione {0}°", f"{m['aspect']:.0f}")
        if m.get("model_surface") is not None:
            text += tr(", superficie del modello {0} m²", f"{m['model_surface']:.3f}")
    else:
        text = tr("Quota {0} m", f"{m['z']:.3f}")
    return decimal_comma(text)


class FreeDrawingDialog(QDialog):
    """The form of the free drawing ``rec`` of ``chunk`` (a copy of the record: nothing is written here)."""

    def __init__(self, window, chunk, rec: dict, model=None, toward=None):
        """``model`` (a trimesh in the chunk's coordinates) and ``toward`` (the viewer's side) for the measures: read
        from the chunk when not given (the model, the median of the aligned photos' centres)."""
        super().__init__(window)
        if model is None and rec.get("kind") == "polygon":
            try:
                model = model_in_chunk(chunk)
            except Exception as exc:  # noqa: BLE001 - the measures without the model
                log.warning("Modello non letto per le misure: %s", exc)
        if toward is None and chunk is not None:
            from ..engine.orient import toward_cameras

            try:
                toward = toward_cameras(chunk, np.asarray(rec["xyz"], float))
            except Exception:  # noqa: BLE001 - no photos: the side the faces face
                toward = None
        self.setWindowTitle(tr("Disegno libero: {0}", rec.get("name") or ""))
        self.chunk, self.rec = chunk, rec
        self._own_colour = free_drawings.colour_of(chunk, rec)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(rec.get("name") or "")
        form.addRow(tr("Nome"), self.name_edit)
        self.layer_combo = QComboBox()
        self.layer_combo.setEditable(True)
        self.layer_combo.setProperty("ppg_no_translate", True)  # the names of the user's layers
        for x in free_drawings.layers(chunk):
            self.layer_combo.addItem(x["name"])
        self.layer_combo.setEditText(rec.get("layer") or free_drawings.DEFAULT_LAYER)
        form.addRow(tr("Layer"), self.layer_combo)
        row = QHBoxLayout()
        self.colour_edit = QLineEdit(self._own_colour)
        self.colour_edit.setPlaceholderText("#rrggbb")
        self.b_colour = QPushButton(tr("Scegli..."))
        self.b_colour.clicked.connect(self._pick_colour)
        row.addWidget(self.colour_edit, 1)
        row.addWidget(self.b_colour)
        form.addRow(tr("Colore"), row)
        lay.addLayout(form)
        lay.addWidget(QLabel(tr("Attributi")))
        self.attributes = QTableWidget(0, 2)
        self.attributes.setHorizontalHeaderLabels([tr("Campo"), tr("Valore")])
        self.attributes.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.attributes.verticalHeader().setVisible(False)
        for k, v in (rec.get("attributes") or {}).items():
            self.add_attribute(str(k), "" if v is None else str(v))
        lay.addWidget(self.attributes)
        buttons = QHBoxLayout()
        b_add = QPushButton(tr("Aggiungi attributo"))
        b_add.clicked.connect(lambda: self.add_attribute("", ""))
        b_del = QPushButton(tr("Togli attributo"))
        b_del.clicked.connect(self._remove_attribute)
        buttons.addWidget(b_add)
        buttons.addWidget(b_del)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        lay.addWidget(QLabel(tr("Vertici (la quota si può scrivere)")))
        self.vertices = QTableWidget(0, 3)
        self.vertices.setHorizontalHeaderLabels(["X", "Y", "Z"])
        self.vertices.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.vertices.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                      | QAbstractItemView.EditTrigger.EditKeyPressed
                                      | QAbstractItemView.EditTrigger.AnyKeyPressed)
        from PySide6.QtCore import Qt

        for x, y, z in rec["xyz"]:
            r = self.vertices.rowCount()
            self.vertices.insertRow(r)
            for c, val in enumerate((x, y, z)):
                item = QTableWidgetItem(repr(float(val)))
                if c < 2:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.vertices.setItem(r, c, item)
        lay.addWidget(self.vertices)
        lay.addWidget(QLabel(tr("Misure")))
        self.measures = QLabel(measures_text(rec, model, toward))
        self.measures.setWordWrap(True)
        self.measures.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.measures)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.resize(560, 560)

    # ---------------------------------------------------------------- parts
    def add_attribute(self, key: str, value: str) -> None:
        r = self.attributes.rowCount()
        self.attributes.insertRow(r)
        self.attributes.setItem(r, 0, QTableWidgetItem(key))
        self.attributes.setItem(r, 1, QTableWidgetItem(value))

    def _remove_attribute(self) -> None:
        r = self.attributes.currentRow()
        if r >= 0:
            self.attributes.removeRow(r)

    def _pick_colour(self) -> None:
        c = QColorDialog.getColor(QColor(self.colour_edit.text() or self._own_colour), self, tr("Colore"))
        if c.isValid():
            self.colour_edit.setText(c.name())

    def values(self) -> dict:
        """What the form holds: ``name``, ``layer``, ``colour`` ("#rrggbb", or None when not changed), ``attributes``
        (a dict; rows without a field left out), ``xyz``. ValueError for a z that is not a number or a colour that is
        not one."""
        attrs = {}
        for r in range(self.attributes.rowCount()):
            k = (self.attributes.item(r, 0).text() if self.attributes.item(r, 0) else "").strip()
            if k:
                attrs[k] = self.attributes.item(r, 1).text() if self.attributes.item(r, 1) else ""
        xyz = []
        for r, (x, y, _z) in enumerate(self.rec["xyz"]):
            text = self.vertices.item(r, 2).text().strip().replace(",", ".")
            try:
                z = float(text)
            except ValueError:
                raise ValueError(tr("La quota del vertice {0} non è un numero", r + 1)) from None
            if not np.isfinite(z):
                raise ValueError(tr("La quota del vertice {0} non è un numero", r + 1))
            xyz.append([float(x), float(y), z])
        colour = self.colour_edit.text().strip()
        if colour.lower() == self._own_colour.lower():
            colour = None
        elif not QColor.isValidColorName(colour):
            raise ValueError(tr("Il colore non è valido: {0}", colour))
        else:
            colour = QColor(colour).name()
        layer = self.layer_combo.currentText().strip() or free_drawings.DEFAULT_LAYER
        return {"name": self.name_edit.text().strip() or self.rec.get("name") or "", "layer": layer,
                "colour": colour, "attributes": attrs, "xyz": xyz}

    def _accept(self) -> None:
        try:
            self._values = self.values()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.accept()

    @classmethod
    def ask(cls, window, chunk, rec: dict) -> dict | None:
        """The form shown (modal) on ``rec``: its :meth:`values` on OK, None on Cancel."""
        dlg = cls(window, chunk, rec)
        dlg._values = None
        ok = dlg.exec() == QDialog.DialogCode.Accepted
        return dlg._values if ok else None
