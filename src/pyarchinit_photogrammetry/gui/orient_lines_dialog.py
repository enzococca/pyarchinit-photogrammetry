"""*Strumenti > Classifica un disegno...*: the lines of a drawing sorted by their orientation along an axis
(:mod:`..engine.orient_lines`) - along it, across it, pairs of parallel lines, the rest - with a check plate.

The drawing is a layer of free drawings (the plan), the drawings of an elevation (its ``(u, v)``) or a file
(GeoPackage, GeoJSON, DXF). The axis: two points typed, two clicks on the view (*Asse con due clic*), or the
horizontal of a flat elevation; an elevation's drawing is sorted along its own u axis. The parameters are in
*Avanzate*. *Classifica* runs in the worker and shows the plate in the window; *Salva la tavola...* writes it as PNG
or SVG; *Scrivi i layer* writes the elements of a plan drawing as layers of free drawings, one per group (attribute
``gruppo``): one step of *Modifica > Annulla*. The program never names the elements: the plugins do. The window is not
modal; it works on the chunk active when it was opened.
"""
from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QToolButton, QVBoxLayout, QWidget)

from ..engine import orient_lines
from . import i18n
from .i18n import tr
from .ties_dialog import undo_shortcuts

PLATE_PREVIEW = (1200, 840)  # pixels of the plate shown in the window
PARAM_FIELDS = (  # name, label, decimals, maximum, suffix
    ("simplify", "Semplificazione", 3, 1.0, " m"),
    ("min_length", "Tratto più corto", 3, 10.0, " m"),
    ("along_deg", "Lungo l'asse entro", 1, 90.0, "°"),
    ("across_deg", "Di traverso entro", 1, 90.0, "°"),
    ("join_deg", "Unione dei pezzi: angolo", 1, 90.0, "°"),
    ("join_offset", "Unione dei pezzi: scarto laterale", 3, 1.0, " m"),
    ("join_gap", "Unione dei pezzi: distanza", 3, 5.0, " m"),
    ("across_bin", "Linee di traverso raggruppate ogni", 3, 10.0, " m"),
    ("pair_deg", "Coppie: parallele entro", 1, 90.0, "°"),
    ("pair_min", "Coppie: distanza da", 3, 10.0, " m"),
    ("pair_max", "Coppie: distanza fino a", 3, 10.0, " m"),
    ("pair_overlap", "Coppie: sovrapposizione (quota della più corta)", 2, 1.0, ""),
)
GROUP_TEXTS = {"along": "lungo l'asse", "across": "di traverso", "pair_along": "coppie lungo l'asse",
               "pair_across": "coppie di traverso", "other": "altre direzioni"}


def error_text(exc: Exception) -> str:
    """An error of the classification in the language of the interface (an OrientLinesError carries both)."""
    if isinstance(exc, orient_lines.OrientLinesError):
        return exc.italian if i18n.current() == "it" else exc.english
    return i18n.translate(str(exc))


def line_layers(chunk) -> list[str]:
    """The layers of free drawings of ``chunk`` that have lines or polygons."""
    from ..core import free_drawings

    names = []
    for f in free_drawings.features(chunk):
        if f["kind"] in ("line", "polygon") and f["layer"] not in names:
            names.append(f["layer"])
    return names


def plane_elevations(chunk) -> list[dict]:
    from ..engine.elevation import elevations

    return [e for e in elevations(chunk) if (e.get("frame") or {}).get("kind") == "plane"]


class OrientLinesDialog(QDialog):
    """The classification of a drawing of the active chunk by orientation (see the module)."""

    @classmethod
    def ask(cls, window) -> "OrientLinesDialog":
        dlg = cls(window)
        dlg.show()
        return dlg

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.doc = window.doc
        self.chunk = window.chunk
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setModal(False)
        self.setWindowTitle(tr("Classifica un disegno"))
        self.working = False
        self.result: orient_lines.Classification | None = None
        self.file: Path | None = None
        self._tmp = Path(tempfile.mkdtemp(prefix="ppg-plate-"))

        lay = QVBoxLayout(self)
        intro = QLabel(tr("Le linee di un disegno divise per orientamento rispetto a un asse: lungo l'asse, di "
                          "traverso, coppie di linee parallele (i due lati di un elemento) e le altre. I poligoni "
                          "contano per i loro lati, le curve per i loro tratti dritti. Il programma non dà un nome "
                          "agli elementi: lo fanno i plugin (ordinate e comenti di uno scafo, corsi e giunti di un "
                          "muro) dopo il controllo sulla tavola."))
        intro.setWordWrap(True)
        lay.addWidget(intro)

        form = QFormLayout()
        src_row = QHBoxLayout()
        self.c_source = QComboBox()
        self._fill_sources()
        self.b_file = QPushButton(tr("Scegli il file..."))
        src_row.addWidget(self.c_source, 1)
        src_row.addWidget(self.b_file)
        form.addRow(tr("Disegno"), src_row)
        axis = QGridLayout()
        self.s_axis = []
        for k, label in enumerate(("X1", "Y1", "X2", "Y2")):
            s = QDoubleSpinBox()
            s.setRange(-1e9, 1e9)
            s.setDecimals(3)
            s.setValue(1.0 if k == 2 else 0.0)
            axis.addWidget(QLabel(label), k // 2, (k % 2) * 2)
            axis.addWidget(s, k // 2, (k % 2) * 2 + 1)
            self.s_axis.append(s)
        form.addRow(tr("Asse"), axis)
        axis_row = QHBoxLayout()
        self.b_clicks = QPushButton(tr("Asse con due clic"))
        self.b_clicks.setToolTip(tr("Due clic sulla vista: il primo e il secondo punto dell'asse"))
        self.c_elev = QComboBox()
        self.c_elev.addItem(tr("Dall'orizzontale di un prospetto..."), None)
        for e in plane_elevations(self.chunk):
            self.c_elev.addItem(str(e.get("name") or e["id"]), e["id"])
        axis_row.addWidget(self.b_clicks)
        axis_row.addWidget(self.c_elev, 1)
        form.addRow("", axis_row)
        self.l_axis = QLabel()
        self.l_axis.setWordWrap(True)
        form.addRow("", self.l_axis)
        lay.addLayout(form)

        self.b_advanced = QToolButton()
        self.b_advanced.setText(tr("Avanzate"))
        self.b_advanced.setCheckable(True)
        self.b_advanced.setArrowType(Qt.ArrowType.RightArrow)
        self.b_advanced.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        lay.addWidget(self.b_advanced)
        self.w_advanced = QWidget()
        adv = QFormLayout(self.w_advanced)
        self.s_params: dict[str, QDoubleSpinBox] = {}
        for name, label, decimals, top, suffix in PARAM_FIELDS:
            s = QDoubleSpinBox()
            s.setRange(0.0, top)
            s.setDecimals(decimals)
            s.setSingleStep(10 ** -decimals * (5 if decimals else 1))
            s.setValue(orient_lines.DEFAULTS[name])
            s.setSuffix(suffix)
            s.setToolTip(name)
            adv.addRow(tr(label), s)
            self.s_params[name] = s
        self.b_defaults = QPushButton(tr("Valori predefiniti"))
        adv.addRow("", self.b_defaults)
        self.w_advanced.setVisible(False)
        lay.addWidget(self.w_advanced)

        self.l_result = QLabel()
        self.l_result.setWordWrap(True)
        lay.addWidget(self.l_result)
        self.l_plate = QLabel()
        self.l_plate.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.l_plate.setMinimumSize(480, 336)
        lay.addWidget(self.l_plate, 1)

        layers_row = QHBoxLayout()
        layers_row.addWidget(QLabel(tr("Prefisso dei layer")))
        self.e_prefix = QLineEdit(orient_lines.DEFAULT_PREFIX)
        self.e_prefix.setToolTip(tr("Un layer di disegni liberi per gruppo: «prefisso - gruppo»; una classificazione "
                                    "già scritta in quei layer viene sostituita, i disegni fatti a mano restano"))
        layers_row.addWidget(self.e_prefix, 1)
        lay.addLayout(layers_row)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.b_classify = QPushButton(tr("Classifica"))
        self.b_save = QPushButton(tr("Salva la tavola..."))
        self.b_layers = QPushButton(tr("Scrivi i layer"))
        self.b_close = QPushButton(tr("Chiudi"))
        for b in (self.b_classify, self.b_save, self.b_layers, self.b_close):
            buttons.addWidget(b)
        lay.addLayout(buttons)
        undo_shortcuts(self, window)
        for button in self.findChildren(QPushButton):  # Enter in a field does not press a button
            button.setAutoDefault(False)

        self.c_source.currentIndexChanged.connect(self._source_changed)
        self.b_file.clicked.connect(self.choose_file)
        self.b_clicks.clicked.connect(self.start_axis)
        self.c_elev.activated.connect(self._axis_from_elevation)
        self.b_advanced.toggled.connect(self._toggle_advanced)
        self.b_defaults.clicked.connect(self.reset_params)
        self.b_classify.clicked.connect(self.classify)
        self.b_save.clicked.connect(self.save_plate)
        self.b_layers.clicked.connect(self.write_layers)
        self.b_close.clicked.connect(self.close)
        self._source_changed()

    # ------------------------------------------------------------------ state
    def _gone(self) -> bool:
        try:
            self.isVisible()
        except RuntimeError:
            return True
        return self.window.doc is not self.doc or self.chunk not in self.doc.chunks

    def _fill_sources(self) -> None:
        from ..engine.elevation import elevations

        for name in line_layers(self.chunk):
            self.c_source.addItem(tr("Layer: {0}", name), ("layer", name))
        for e in elevations(self.chunk):
            self.c_source.addItem(tr("Prospetto: {0}", e.get("name") or e["id"]), ("elevation", e["id"]))
        self.c_source.addItem(tr("Da un file (GeoPackage, GeoJSON, DXF)"), ("file", None))

    def source(self) -> tuple:
        return self.c_source.currentData() or ("file", None)

    def set_source(self, kind: str, value=None) -> bool:
        """Choose the source ``kind`` ("layer", "elevation", "file") ``value``; returns whether it is there."""
        for i in range(self.c_source.count()):
            data = self.c_source.itemData(i)
            if data and data[0] == kind and (kind == "file" or data[1] == value):
                self.c_source.setCurrentIndex(i)
                if kind == "file" and value is not None:
                    self.file = Path(value)
                    self._source_changed()
                return True
        return False

    def _source_changed(self, *_a) -> None:
        kind, _value = self.source()
        on_elevation = kind == "elevation"
        self.b_file.setVisible(kind == "file")
        for s in self.s_axis:
            s.setEnabled(not on_elevation)
        self.c_elev.setEnabled(not on_elevation and self.c_elev.count() > 1)
        if on_elevation:
            for s, v in zip(self.s_axis, (0.0, 0.0, 1.0, 0.0)):
                s.setValue(v)
            self.l_axis.setText(tr("Il disegno di un prospetto si classifica lungo il suo asse u (l'orizzontale "
                                   "del prospetto)."))
        elif kind == "file":
            self.l_axis.setText(tr("File: {0}", self.file.name) if self.file else tr("Nessun file scelto."))
        else:
            self.l_axis.setText("")
        self.result = None
        self._show_result()

    def axis(self) -> tuple:
        v = [s.value() for s in self.s_axis]
        return (v[0], v[1]), (v[2], v[3])

    def set_axis(self, axis) -> None:
        (x1, y1), (x2, y2) = axis
        for s, v in zip(self.s_axis, (x1, y1, x2, y2)):
            s.setValue(float(v))

    def params(self) -> dict:
        return {name: s.value() for name, s in self.s_params.items()}

    def reset_params(self) -> None:
        for name, s in self.s_params.items():
            s.setValue(orient_lines.DEFAULTS[name])

    def _toggle_advanced(self, on: bool) -> None:
        self.w_advanced.setVisible(on)
        self.b_advanced.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)

    def _update_buttons(self) -> None:
        with contextlib.suppress(RuntimeError):
            done = self.result is not None and not self.working
            self.b_classify.setEnabled(not self.working)
            self.b_save.setEnabled(done)
            self.b_layers.setEnabled(done and self.result.frame is None)
            self.b_layers.setToolTip("" if self.result is None or self.result.frame is None else
                                     tr("Solo un disegno della pianta si scrive come layer di disegni liberi"))

    # ------------------------------------------------------------------ inputs
    def choose_file(self) -> Path | None:
        path, _ = QFileDialog.getOpenFileName(self, tr("Disegno da classificare"), "",
                                              tr("Disegni (*.gpkg *.geojson *.json *.dxf)"))
        if not path:
            return None
        self.file = Path(path)
        self._source_changed()
        return self.file

    def start_axis(self) -> bool:
        """Two clicks on the view (a line of two vertices lent by :meth:`MainWindow.request_geometry`) give the axis:
        on the plan for a plan drawing, on the elevation shown for an elevation's."""
        if self._gone():
            return False
        w = self.window
        kind, value = self.source()
        if w.chunk is not self.chunk:
            w._warn(tr("Classifica un disegno: rendi attivo il chunk {0} per tracciare l'asse.", self.chunk.label))
            return False
        if kind == "elevation" and w.drawings_frame != value:
            w._warn(tr("Classifica un disegno: mostra il prospetto nella vista 2D per tracciarne l'asse."))
            return False
        if kind != "elevation" and w.drawings_frame is not None:
            w._warn(tr("Classifica un disegno: mostra la pianta nella vista 2D per tracciare l'asse."))
            return False
        return w.request_geometry("line", self._axis_drawn,
                                  hint=tr("Due clic: il primo e il secondo punto dell'asse, poi Invio (Esc annulla)"))

    def _axis_drawn(self, xy) -> None:
        if self._gone():
            return
        xy = np.asarray(xy, float).reshape(-1, 2)
        if len(xy) < 2:
            return
        if self.source()[0] == "elevation":
            for s in self.s_axis:  # an axis of its own on the elevation
                s.setEnabled(True)
        self.set_axis((xy[0], xy[-1]))

    def _axis_from_elevation(self, *_a) -> None:
        eid = self.c_elev.currentData()
        if eid is None or self._gone():
            return
        try:
            self.set_axis(orient_lines.elevation_axis(self.chunk, eid))
        except ValueError as exc:
            self.window._warn(tr("Classifica un disegno: {0}", error_text(exc)))
        self.c_elev.setCurrentIndex(0)

    def _lines(self):
        kind, value = self.source()
        if kind == "layer":
            return orient_lines.lines_from_layer(self.chunk, value), None
        if kind == "elevation":
            return orient_lines.lines_from_elevation(self.chunk, value), value
        if self.file is None:
            raise orient_lines.OrientLinesError("choose a file first", "scegli prima un file")
        return orient_lines.lines_from_file(self.file), None

    # ------------------------------------------------------------------ classify
    def classify(self) -> bool:
        """The classification and its plate computed in the worker, shown in the window. Returns whether it
        started."""
        if self._gone() or self.working:
            return False
        try:
            lines, frame = self._lines()
            axis, params = orient_lines.checked_axis(self.axis()), self.params()
            orient_lines.checked_params(params)
        except ValueError as exc:
            self.window._warn(tr("Classifica un disegno: {0}", error_text(exc)))
            return False
        plate = self._tmp / "tavola.png"
        language = "it" if i18n.current() == "it" else "en"

        def work(_p):
            try:
                result = orient_lines.classify(lines, axis, params, frame=frame)
                result.plate(plate, size=PLATE_PREVIEW, language=language)
                return result
            except ValueError as exc:
                return {"error": exc}

        self.working = True
        self._update_buttons()
        started = self.window.runner.run("Classifica un disegno", work, lambda r: self._classified(r, plate),
                                         self._failed)
        if not started:
            self.working = False
            self._update_buttons()
        return started

    def _failed(self, summary: str, details: str = "") -> None:
        with contextlib.suppress(RuntimeError):
            self.working = False
            self._update_buttons()
        self.window._error(summary, details)

    def _classified(self, result, plate: Path) -> None:
        with contextlib.suppress(RuntimeError):
            self.working = False
        if self._gone():
            return
        if isinstance(result, dict):
            self.result = None
            self._show_result()
            self.window._warn(tr("Classifica un disegno: {0}", error_text(result["error"])))
            return
        self.result = result
        self._show_result(plate)

    def _show_result(self, plate: Path | None = None) -> None:
        with contextlib.suppress(RuntimeError):
            r = self.result
            if r is None:
                self.l_result.setText("")
                self.l_plate.clear()
            else:
                counts = r.counts()
                parts = [tr("{0}: {1}", tr(GROUP_TEXTS[g]), counts[g]) for g in orient_lines.GROUPS]
                text = tr("{0} tratti dritti, {1} elementi. ", len(r.segments), len(r.elements)) + "; ".join(parts)
                widths = [e.width for e in r.elements if e.width is not None]
                if widths:
                    text += tr(". Larghezza delle coppie: da {0} a {1} m", f"{min(widths):.3f}", f"{max(widths):.3f}")
                self.l_result.setText(text)
                if plate is not None and plate.exists():
                    pix = QPixmap(str(plate))
                    self.l_plate.setPixmap(pix.scaled(self.l_plate.width(), self.l_plate.height(),
                                                      Qt.AspectRatioMode.KeepAspectRatio,
                                                      Qt.TransformationMode.SmoothTransformation))
            self._update_buttons()

    def save_plate(self, path=None) -> Path | None:
        """The plate of the classification written as PNG or SVG (``path``: without asking)."""
        if self.result is None or self._gone():
            return None
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, tr("Salva la tavola"), "tavola_classificazione.png",
                                                  tr("Immagine PNG (*.png);;Disegno SVG (*.svg)"))
            if not path:
                return None
        path = Path(path)
        if path.suffix.lower() not in (".png", ".svg"):
            path = path.with_suffix(".png")
        try:
            out = self.result.plate(path, language="it" if i18n.current() == "it" else "en")
        except (OSError, ValueError) as exc:
            self.window._warn(tr("Classifica un disegno: {0}", error_text(exc)))
            return None
        self.window.log_pane.info(tr("Classifica un disegno: tavola scritta in {0}", str(out)))
        return out

    # ------------------------------------------------------------------ layers
    def write_layers(self) -> dict | None:
        """The elements of a plan drawing as layers of free drawings, one per group: one step of *Modifica >
        Annulla*."""
        if self.result is None or self._gone() or self.working:
            return None
        w = self.window
        if w.chunk is not self.chunk:  # another chunk active: the step of Annulla would be lost with its history
            w._warn(tr("Classifica un disegno: rendi attivo il chunk {0} per scrivere i layer.", self.chunk.label))
            return None
        prefix = self.e_prefix.text().strip() or orient_lines.DEFAULT_PREFIX
        try:
            written = orient_lines.to_layers(self.chunk, self.result, prefix=prefix)
        except ValueError as exc:
            w._warn(tr("Classifica un disegno: {0}", error_text(exc)))
            return None
        w.setWindowModified(True)
        if w.history is not None and not w.history.applying:
            w.history.record(tr("Classifica un disegno"))
        w._draw_free()
        for name, n in written.items():
            w.log_pane.info(tr("Classifica un disegno: layer {0}, {1} disegni", name, n))
        return written

    def _cleanup(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def closeEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        self._cleanup()
        super().closeEvent(ev)

    def reject(self) -> None:  # Esc: no closeEvent
        self._cleanup()
        super().reject()


__all__ = ["OrientLinesDialog", "error_text", "line_layers"]
