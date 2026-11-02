"""Strumenti > Calibrazione della camera: the parameters of each sensor, editable as the values
the alignment starts from, whether they are held, and a plot of the lens distortion (where every
point of the photo goes compared with an ideal pinhole, and the shift along the radius)."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
                               QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .i18n import decimal_comma, tr

MODELS = [("OPENCV", "Normale (Brown: k1, k2, p1, p2)"), ("OPENCV_FISHEYE", "Fisheye (Kannala-Brandt: k1-k4)"),
          ("FULL_OPENCV", "Normale, distorsione forte (k1-k6, p1, p2)"), ("RADIAL", "Radiale (k1, k2)"),
          ("PINHOLE", "Senza distorsione")]


def parameter_names(sensor) -> list[str]:
    try:
        return [n.strip() for n in sensor.to_pycolmap().params_info.split(",")]
    except Exception:  # pragma: no cover - a model pycolmap does not describe
        return [f"p{i}" for i in range(len(sensor.params))]


def distortion_field(sensor, nx: int = 13, ny: int = 9):
    """Where points of a regular grid over the photo (as an ideal pinhole with the focal length
    and the principal point of the sensor would put them) fall with the lens: ``(ideal, real)``
    pixels (n, 2); rays beyond 85 degrees from the axis are left out."""
    k = sensor.K
    u, v = np.meshgrid(np.linspace(0, sensor.width, nx), np.linspace(0, sensor.height, ny))
    ideal = np.column_stack([u.ravel(), v.ravel()])
    rays = np.column_stack([(ideal[:, 0] - k[0, 2]) / k[0, 0], (ideal[:, 1] - k[1, 2]) / k[1, 1], np.ones(len(ideal))])
    keep = np.degrees(np.arctan(np.hypot(rays[:, 0], rays[:, 1]))) < 85
    real = np.full_like(ideal, np.nan)
    real[keep] = sensor.project(rays[keep])
    return ideal, real


class DistortionPlot(QWidget):
    """The distortion of a sensor: arrows from the ideal pinhole point to the real one over the
    frame of the photo (magnified to be seen), and the shift against the distance from the centre."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sensor = None
        self.setMinimumSize(420, 380)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_sensor(self, sensor) -> None:
        self.sensor = sensor
        self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802 - Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), self.palette().base())
        s = self.sensor
        if s is None:
            return
        try:
            ideal, real = distortion_field(s)
        except Exception as exc:  # pragma: no cover - a calibration that cannot be evaluated
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, tr("Distorsione non calcolabile: {0}", exc))
            return
        d = real - ideal
        mag = np.hypot(d[:, 0], d[:, 1])
        top_h = self.height() * 0.66
        frame = QRectF(10, 24, self.width() - 20, top_h - 34)
        scale = min(frame.width() / s.width, frame.height() / s.height)
        box = QRectF(frame.x() + (frame.width() - s.width * scale) / 2, frame.y(), s.width * scale, s.height * scale)
        p.setPen(QPen(QColor("#8a94a0"), 1))
        p.drawRect(box)
        finite = np.isfinite(mag)
        biggest = float(np.nanmax(mag)) if finite.any() else 0.0
        step = box.width() / 12
        k = 0.8 * step / biggest if biggest > 0 else 1.0  # the longest arrow 80 % of the grid step
        p.setPen(QPen(QColor("#d1495b"), 1.6))
        for (x0, y0), (dx, dy), ok in zip(ideal, d, finite):
            if not ok:
                continue
            a = QPointF(box.x() + x0 * scale, box.y() + y0 * scale)
            b = QPointF(a.x() + dx * k, a.y() + dy * k)
            p.drawLine(a, b)
            p.drawEllipse(b, 1.6, 1.6)
        p.setPen(self.palette().text().color())
        p.drawText(QRectF(10, 2, self.width() - 20, 20), Qt.AlignmentFlag.AlignLeft,
                   decimal_comma(tr("Distorsione: fino a {0:.1f} px dal punto ideale (frecce x{1:.0f})", biggest,
                      k / scale)))
        # the shift along the radius, from the centre to the corner
        chart = QRectF(46, top_h + 18, self.width() - 60, self.height() - top_h - 46)
        kk = s.K
        corner = np.hypot(max(kk[0, 2], s.width - kk[0, 2]), max(kk[1, 2], s.height - kk[1, 2]))
        r = np.linspace(0, corner, 80)
        theta = np.arctan2(s.height, s.width)
        px = np.column_stack([kk[0, 2] + r * np.cos(theta), kk[1, 2] + r * np.sin(theta)])
        rays = np.column_stack([(px[:, 0] - kk[0, 2]) / kk[0, 0], (px[:, 1] - kk[1, 2]) / kk[1, 1], np.ones(len(r))])
        ok = np.degrees(np.arctan(np.hypot(rays[:, 0], rays[:, 1]))) < 85
        shift = np.full(len(r), np.nan)
        if ok.any():
            real_r = s.project(rays[ok])
            shift[ok] = np.hypot(real_r[:, 0] - kk[0, 2], real_r[:, 1] - kk[1, 2]) - r[ok]
        lim = float(np.nanmax(np.abs(shift))) if np.isfinite(shift).any() else 1.0
        lim = lim or 1.0
        p.setPen(QPen(QColor("#8a94a0"), 1))
        p.drawRect(chart)
        zero = chart.center().y()
        p.drawLine(QPointF(chart.left(), zero), QPointF(chart.right(), zero))
        path = QPainterPath()
        first = True
        for ri, si in zip(r, shift):
            if not np.isfinite(si):
                continue
            pt = QPointF(chart.left() + ri / corner * chart.width(), zero - si / lim * chart.height() / 2)
            if first:
                path.moveTo(pt)
                first = False
            else:
                path.lineTo(pt)
        p.setPen(QPen(QColor("#2e86ab"), 2))
        p.drawPath(path)
        p.setPen(self.palette().text().color())
        p.drawText(QRectF(chart.left(), chart.bottom() + 2, chart.width(), 18), Qt.AlignmentFlag.AlignCenter,
                   tr("distanza dal centro (0 - {0:.0f} px, verso l'angolo)", corner))
        p.drawText(QRectF(0, chart.top() - 16, self.width(), 16), Qt.AlignmentFlag.AlignLeft,
                   decimal_comma(tr("   spostamento radiale (+/-{0:.1f} px)", lim)))


class CalibrationDialog(QDialog):
    """The sensors of a chunk, their parameters and their distortion."""

    def __init__(self, chunk, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Calibrazione della camera"))
        self.resize(900, 600)
        self.chunk = chunk
        self.changed = False
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.sensor_box = QComboBox()
        for s in chunk.sensors.values():
            n = sum(1 for c in chunk.cameras if c.sensor_key == s.key)
            self.sensor_box.addItem(tr("{0}  ({1} x {2} px, {3} foto)", s.label, s.width, s.height, n), s.key)
        top.addWidget(QLabel(tr("Sensore")))
        top.addWidget(self.sensor_box, 1)
        lay.addLayout(top)
        body = QHBoxLayout()
        left = QVBoxLayout()
        form = QFormLayout()
        self.model = QComboBox()
        form.addRow("Modello", self.model)
        self.fixed = QCheckBox(tr("Calibrazione fissa (l'allineamento e l'ottimizzazione non la cambiano)"))
        form.addRow(self.fixed)
        left.addLayout(form)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([tr("Parametro"), tr("Valore")])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        from .table_export import install_excel_menu

        install_excel_menu(self.table, tr("Calibrazione"))
        left.addWidget(self.table, 1)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        left.addWidget(self.info)
        body.addLayout(left, 2)
        self.plot = DistortionPlot()
        body.addWidget(self.plot, 3)
        lay.addLayout(body, 1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close)
        box.button(QDialogButtonBox.StandardButton.Apply).setText(tr("Applica"))
        box.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        box.rejected.connect(self.reject)
        box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        lay.addWidget(box)
        self.sensor_box.currentIndexChanged.connect(lambda _i: self._show())
        self.model.currentIndexChanged.connect(lambda _i: self._model_changed())
        self._show()

    @property
    def sensor(self):
        return self.chunk.sensors[self.sensor_box.currentData()]

    def _show(self) -> None:
        s = self.sensor
        self.model.blockSignals(True)
        self.model.clear()
        models = list(MODELS) + ([(s.model, s.model)] if s.model not in dict(MODELS) else [])
        for key, text in models:
            self.model.addItem(text, key)
        self.model.setCurrentIndex(self.model.findData(s.model))
        self.model.blockSignals(False)
        self.fixed.setChecked(bool(s.fixed))
        self._fill(s.model, list(s.params))
        aligned = sum(1 for c in self.chunk.cameras if c.sensor_key == s.key and c.aligned)
        f35 = 36.0 * s.focal_px / max(s.width, s.height)
        self.info.setText(tr("{0} foto allineate con questo sensore. Focale {1:.1f} px (circa {2:.0f} mm "
                             "equivalenti). I valori si possono correggere a mano: sono quelli da cui parte "
                             "l'allineamento; cambiando modello la distorsione riparte da zero.",
                             aligned, s.focal_px, f35))
        self.plot.set_sensor(s)

    def _fill(self, model: str, params) -> None:
        from ..core.camera import Sensor

        tmp = Sensor(key=-1, label="", width=self.sensor.width, height=self.sensor.height, model=model,
                     params=list(params))
        names = parameter_names(tmp)
        self.table.setRowCount(len(params))
        for i, (name, value) in enumerate(zip(names, params)):
            a = QTableWidgetItem(name)
            a.setFlags(a.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(i, 0, a)
            self.table.setItem(i, 1, QTableWidgetItem(f"{float(value):.10g}"))

    def _model_changed(self) -> None:
        model = self.model.currentData()
        s = self.sensor
        if model == s.model:
            self._fill(s.model, s.params)
            return
        self._fill(model, s.converted(model))  # nothing changes until Applica

    def values(self) -> tuple[str, list[float], bool]:
        params = []
        for i in range(self.table.rowCount()):
            text = self.table.item(i, 1).text().replace(",", ".").strip()
            params.append(float(text))
        return self.model.currentData(), params, self.fixed.isChecked()

    def apply(self) -> bool:
        """Write the model, the parameters and the fixed flag into the sensor."""
        try:
            model, params, fixed = self.values()
        except ValueError:
            from .dialogs import message

            message(self, "warning", "Calibrazione", "Un valore non è un numero.")
            return False
        s = self.sensor
        if (model, [float(v) for v in s.params], bool(s.fixed)) != (s.model, params, fixed):
            s.model, s.params, s.fixed = model, params, fixed
            self.changed = True
        self.plot.set_sensor(s)
        return True
