"""The form of a free drawing in 3D (:mod:`..core.free_drawings`): its name, layer (one of those of the chunk or a new
one), colour, attributes (key / value rows) and vertices (x, y read-only, z typed by the user), with its measures from
:func:`..archaeo.measures.feature_measures` (read-only). Opened by a double click on the drawing in the 2D or the 3D
view and by *Proprietà...*; :meth:`FreeDrawingDialog.ask` gives the values to the window, which writes them as one
step of *Modifica > Annulla*. Since 1.7.2. The measures of a polygon on the model are taken in a worker thread that reads
the model and lets it go (:func:`measure_later`, since 1.7.3): no copy of the model is kept."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ..core import free_drawings
from .i18n import decimal_comma, tr

log = logging.getLogger(__name__)


def model_in_chunk(chunk):
    """The model of ``chunk`` as a trimesh in the chunk's coordinates (as the free drawings are), None without one.
    Read each time and not kept: the caller lets it go when the measures are taken."""
    path = chunk.product_path("model") if chunk is not None else None
    if path is None or not path.exists():
        return None
    import trimesh

    from ..io.mesh_ply import read_mesh

    m = read_mesh(path)
    return trimesh.Trimesh(chunk.to_world(np.asarray(m.xyz, np.float64)), m.faces, process=False)


class ModelSource:
    """The model file and the transform of a chunk taken at once (in the interface thread): what a worker reads with
    :func:`model_in_chunk` instead of the chunk, which the window may change meanwhile (an orientation applied, the
    model rebuilt)."""

    def __init__(self, chunk):
        path = chunk.product_path("model") if chunk is not None else None
        self.path = Path(path) if path is not None else None
        self.transform = np.array(chunk.transform, float, copy=True) if chunk is not None else np.eye(4)

    def product_path(self, name: str):
        return self.path if name == "model" else None

    def to_world(self, points) -> np.ndarray:
        from ..core import geometry

        return geometry.apply(self.transform, points)


def _measures(rec: dict, model=None, toward=None):
    """The measures of ``rec`` (:func:`..archaeo.measures.feature_measures`), on the model when it can; None when they
    cannot be taken (too few vertices, all on one line)."""
    from ..archaeo.measures import feature_measures

    try:
        return feature_measures(rec, model=model, toward=toward)
    except ValueError:
        return None
    except Exception as exc:  # noqa: BLE001 - a model that cannot be measured: the drawing's own measures
        log.warning("Misure sul modello non riuscite: %s", exc)
        try:
            return feature_measures(rec)
        except ValueError:
            return None


def measures_words(rec: dict, m) -> str:
    """The measures ``m`` of ``rec`` in words; "-" for None."""
    if m is None:
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


def measures_text(rec: dict, model=None, toward=None) -> str:
    """The measures of the free drawing ``rec`` in words; "-" when they cannot be taken (too few vertices, all on one
    line)."""
    return measures_words(rec, _measures(rec, model, toward))


class _MeasuresRelay(QObject):
    """Hands the measures taken in a worker thread to the interface thread (queued)."""

    ready = Signal(object)

    def __init__(self, parent, rec: dict, done, words=None):
        super().__init__(parent)
        self._rec, self._done, self._words = rec, done, words or measures_words
        self.ready.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def _deliver(self, m) -> None:
        self._done(self._words(self._rec, m))
        self.deleteLater()


def measure_later(parent, chunk, rec: dict, toward, done, words=None) -> bool:
    """The measures of ``rec`` in words given to ``done(text)`` in the interface thread. A polygon of a chunk with a
    model is measured on it in a worker thread that reads the model and lets it go (True: ``done`` comes later, never
    if ``parent`` is closed first); otherwise at once, without the model (False). ``words(rec, measures)`` turns the
    measures (the dict of :func:`..archaeo.measures.feature_measures`, None when they cannot be taken) into what
    ``done`` gets: :func:`measures_words` by default."""
    words = words or measures_words
    path = chunk.product_path("model") if chunk is not None and rec.get("kind") == "polygon" else None
    if path is None or not path.exists():
        done(words(rec, _measures(rec, None, toward)))
        return False
    relay = _MeasuresRelay(parent, rec, done, words)
    source = ModelSource(chunk)  # the path and the transform of now, not read from the chunk in the worker

    def work():
        try:
            model = model_in_chunk(source)
        except Exception as exc:  # noqa: BLE001 - the measures without the model
            log.warning("Modello non letto per le misure: %s", exc)
            model = None
        m = _measures(rec, model, toward)
        del model  # the model let go before the interface gets the measures
        try:
            relay.ready.emit(m)
        except RuntimeError:  # the form closed meanwhile
            pass

    threading.Thread(target=work, name="ppg-free-measures", daemon=True).start()
    return True


def shown_number(value) -> str:
    """A coordinate as the form shows it: three decimals, the decimal comma in Italian (no thousands separator, so
    that a typed value reads back the same)."""
    return decimal_comma(f"{float(value):.3f}")


def parse_number(text: str) -> float:
    """A number typed with a decimal point or a decimal comma (with both, the last one is the decimal separator and
    the other separates the thousands). ValueError when it is not one."""
    t = text.strip().replace(" ", "").replace("\u00a0", "")
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    else:
        t = t.replace(",", ".")
    return float(t)


class FreeDrawingDialog(QDialog):
    """The form of the free drawing ``rec`` of ``chunk`` (a copy of the record: nothing is written here)."""

    def __init__(self, window, chunk, rec: dict, model=None, toward=None):
        """``model`` (a trimesh in the chunk's coordinates) and ``toward`` (the viewer's side) for the measures: when
        no model is given, a polygon is measured on the chunk's model in the background (:func:`measure_later`);
        ``toward`` read from the chunk when not given (the median of the aligned photos' centres)."""
        super().__init__(window)
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
        self.colour_edit.setPlaceholderText(tr("#rrggbb (vuoto: il colore del layer)"))
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
        for x, y, z in rec["xyz"]:
            r = self.vertices.rowCount()
            self.vertices.insertRow(r)
            for c, val in enumerate((x, y, z)):
                item = QTableWidgetItem(shown_number(val))
                if c < 2:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.vertices.setItem(r, c, item)
        lay.addWidget(self.vertices)
        lay.addWidget(QLabel(tr("Misure")))
        self.measures = QLabel(tr("Misure in calcolo..."))
        self.measures.setWordWrap(True)
        self.measures.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.measures)
        if model is not None:
            self.measures.setText(measures_text(rec, model, toward))
        else:
            measure_later(self, chunk, rec, toward, self.measures.setText)
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
        """What the form holds: ``name``, ``layer``, ``colour`` ("#rrggbb"; None when not changed; "" when emptied: the
        layer's colour again), ``attributes``
        (a dict; rows without a field left out), ``xyz``. ValueError for a z that is not a number or a colour that is
        not one."""
        attrs = {}
        for r in range(self.attributes.rowCount()):
            k = (self.attributes.item(r, 0).text() if self.attributes.item(r, 0) else "").strip()
            if k:
                attrs[k] = self.attributes.item(r, 1).text() if self.attributes.item(r, 1) else ""
        xyz = []
        for r, (x, y, _z) in enumerate(self.rec["xyz"]):
            text = self.vertices.item(r, 2).text().strip()
            try:
                # unchanged: the height as it is, not the 3 decimals shown
                z = float(_z) if text == shown_number(_z) else parse_number(text)
            except ValueError:
                raise ValueError(tr("La quota del vertice {0} non è un numero", r + 1)) from None
            if not np.isfinite(z):
                raise ValueError(tr("La quota del vertice {0} non è un numero", r + 1))
            xyz.append([float(x), float(y), z])
        colour = self.colour_edit.text().strip()
        if not colour:
            colour = "" if self.rec.get("colour") else None  # emptied: the layer's colour
        elif colour.lower() == self._own_colour.lower():
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
