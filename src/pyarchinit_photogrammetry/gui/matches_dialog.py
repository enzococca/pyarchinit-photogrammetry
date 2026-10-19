"""Diagnostics of the alignment: the matching graph and the matches between two photos
(:mod:`pyarchinit_photogrammetry.engine.diagnostics`)."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QGraphicsScene, QGraphicsView, QHBoxLayout,
                               QLabel, QSpinBox, QVBoxLayout)

from ..engine import diagnostics
from .image_views import to_qimage

GOOD, FAIR, WEAK = "#2a9d55", "#f4a261", "#d1495b"


def _colour(n: int) -> str:
    return GOOD if n >= 100 else (FAIR if n >= 30 else WEAK)


class _GraphView(QGraphicsView):
    pair_clicked = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def wheelEvent(self, ev) -> None:  # noqa: N802 - Qt
        f = 1.15 if ev.angleDelta().y() > 0 else 1 / 1.15
        self.scale(f, f)

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802 - Qt
        item = self.itemAt(ev.position().toPoint())
        pair = item.data(0) if item is not None else None
        if pair:
            self.pair_clicked.emit(*pair)
        else:
            super().mouseDoubleClickEvent(ev)


class MatchGraphDialog(QDialog):
    """Photos as dots - where they were taken when aligned, else in a ring around - and lines
    between the photos matched, thicker and greener with more matches (red under 30). Groups of
    photos that do not link and photos with no match are listed; a double click on a line shows
    the matches of the pair."""

    def __init__(self, chunk, graph: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Grafo delle corrispondenze")
        self.resize(820, 700)
        self.chunk = chunk
        self.graph = diagnostics.match_graph(chunk) if graph is None else graph
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Corrispondenze minime per una linea"))
        self.min_box = QSpinBox()
        self.min_box.setRange(1, 10000)
        self.min_box.setValue(15)
        self.min_box.valueChanged.connect(lambda _v: self.draw())
        top.addWidget(self.min_box)
        top.addStretch(1)
        lay.addLayout(top)
        self.view = _GraphView()
        self.view.pair_clicked.connect(self.show_pair)
        lay.addWidget(self.view, 1)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        lay.addWidget(box)
        self.positions = self._layout()
        self.draw()

    def _layout(self) -> dict[int, tuple[float, float]]:
        """Scene positions: the aligned photos where they were taken (plan, north up), the
        others on a ring around them."""
        cams = [c for c in self.chunk.cameras if c.enabled]
        aligned = [c for c in cams if c.aligned]
        pos = {}
        size = 600.0
        if len(aligned) >= 2:
            xy = self.chunk.to_world(np.array([c.center for c in aligned]))[:, :2]
            lo, span = xy.min(0), max(float(np.ptp(xy, axis=0).max()), 1e-9)
            for c, (x, y) in zip(aligned, xy):
                pos[c.key] = ((x - lo[0]) / span * size, size - (y - lo[1]) / span * size)
        rest = [c for c in cams if c.key not in pos]
        centre, radius = size / 2, size * (0.75 if pos else 0.45)
        for i, c in enumerate(rest):
            a = 2 * np.pi * i / max(len(rest), 1)
            pos[c.key] = (centre + radius * np.cos(a), centre + radius * np.sin(a))
        return pos

    def draw(self) -> None:
        scene = self.view.scene()
        scene.clear()
        lim = self.min_box.value()
        shown = {k: n for k, n in self.graph.items() if n >= lim}
        top = max(shown.values(), default=1)
        for (a, b), n in sorted(shown.items(), key=lambda kv: kv[1]):
            if a not in self.positions or b not in self.positions:
                continue
            pen = QPen(QColor(_colour(n)), 0.6 + 3.0 * np.log1p(n) / np.log1p(top))
            pen.setCosmetic(True)
            line = scene.addLine(*self.positions[a], *self.positions[b], pen)
            line.setData(0, (a, b))
            la, lb = self.chunk.camera(a).label, self.chunk.camera(b).label
            line.setToolTip(f"{la} - {lb}: {n} corrispondenze (doppio clic per vederle)")
        linked = {k for pair in shown for k in pair}
        for key, (x, y) in self.positions.items():
            cam = self.chunk.camera(key)
            colour = "#2e86ab" if cam.aligned else WEAK
            dot = scene.addEllipse(x - 4, y - 4, 8, 8, QPen(QColor("#1f2933"), 0.8), QColor(colour))
            dot.setZValue(2)
            dot.setToolTip(f"{cam.label}" + ("" if cam.aligned else " (non allineata)")
                           + ("" if key in linked else f" - nessuna coppia con almeno {lim} corrispondenze"))
            if key not in linked or not cam.aligned:
                text = scene.addSimpleText(cam.label)
                text.setBrush(QColor(colour))
                text.setPos(x + 6, y - 6)
                text.setZValue(3)
        self.view.fitInView(scene.itemsBoundingRect().adjusted(-20, -20, 20, 20), Qt.AspectRatioMode.KeepAspectRatio)
        groups = diagnostics.components(list(self.positions), self.graph, lim)
        alone = [self.chunk.camera(g[0]).label for g in groups if len(g) == 1]
        parts = [f"{len(self.positions)} foto, {len(shown)} coppie con almeno {lim} corrispondenze"]
        if len(groups) > 1:
            parts.append("gruppi che non si collegano: " + ", ".join(str(len(g)) for g in groups if len(g) > 1)
                         + (" foto" if any(len(g) > 1 for g in groups) else ""))
        if alone:
            parts.append(f"foto isolate ({len(alone)}): " + ", ".join(alone[:20]) + (" ..." if len(alone) > 20 else ""))
        self.info.setText("; ".join(parts) + ". Rosso: meno di 30 corrispondenze, arancio meno di 100.")

    def show_pair(self, a: int, b: int) -> None:
        PairMatchesDialog(self.chunk, a, b, self).exec()


class PairMatchesDialog(QDialog):
    """Two photos side by side with lines between their matched points: green those that fit the
    geometry of the pair, red the other candidates, blue the tie points of the aligned model (at
    most 400 lines of each kind, an even sample)."""

    def __init__(self, chunk, key_a: int, key_b: int, parent=None):
        super().__init__(parent)
        self.chunk = chunk
        ca, cb = chunk.camera(key_a), chunk.camera(key_b)
        self.setWindowTitle(f"Corrispondenze: {ca.label} - {cb.label}")
        self.resize(1100, 640)
        try:
            self.data = diagnostics.pair_matches(chunk, key_a, key_b)
        except Exception:  # not matched (or the database is gone): only the tie points
            self.data = {"a": np.zeros((0, 2)), "b": np.zeros((0, 2)), "candidates": np.zeros((0, 2), int),
                         "verified": np.zeros((0, 2), int)}
        try:
            self.ties = diagnostics.shared_tie_points(chunk, key_a, key_b)
        except Exception:
            self.ties = (np.zeros((0, 2)), np.zeros((0, 2)))
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        verified = {tuple(m) for m in self.data["verified"]}
        rejected = [m for m in self.data["candidates"] if tuple(m) not in verified]
        self.rejected = np.array(rejected).reshape(-1, 2)
        self.c_ver = QCheckBox(f"Verificate ({len(self.data['verified'])})")
        self.c_rej = QCheckBox(f"Scartate ({len(self.rejected)})")
        self.c_tie = QCheckBox(f"Punti di legame ({len(self.ties[0])})")
        self.c_ver.setChecked(True)
        self.c_tie.setChecked(not len(self.data["verified"]))
        for c in (self.c_ver, self.c_rej, self.c_tie):
            c.toggled.connect(lambda _v: self.draw())
            row.addWidget(c)
        row.addStretch(1)
        lay.addLayout(row)
        self.view = _GraphView()
        lay.addWidget(self.view, 1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        lay.addWidget(box)
        self.images = [self._read(ca), self._read(cb)]
        self.draw()

    @staticmethod
    def _read(cam):
        import cv2

        from ..engine.visibility import read_photo

        try:
            img = read_photo(cam)
        except Exception:
            return None, 1.0
        f = min(1.0, 1600 / max(img.shape[:2]))
        if f < 1:
            img = cv2.resize(img, (int(img.shape[1] * f), int(img.shape[0] * f)), interpolation=cv2.INTER_AREA)
        return QPixmap.fromImage(to_qimage(cv2.cvtColor(img, cv2.COLOR_BGR2RGBA))), f

    def draw(self) -> None:
        scene = self.view.scene()
        scene.clear()
        (pa, fa), (pb, fb) = self.images
        offset = (pa.width() if pa is not None else 1600) + 40
        if pa is not None:
            scene.addPixmap(pa)
        if pb is not None:
            scene.addPixmap(pb).setPos(offset, 0)

        def lines(xa, xb, colour, most: int = 400):
            if len(xa) > most:  # thousands of lines hide the photos: an even sample of them
                pick = np.random.default_rng(0).choice(len(xa), most, replace=False)
                xa, xb = xa[pick], xb[pick]
            path = QPainterPath()
            for (x1, y1), (x2, y2) in zip(xa, xb):
                path.moveTo(QPointF(x1 * fa, y1 * fa))
                path.lineTo(QPointF(offset + x2 * fb, y2 * fb))
            pen = QPen(QColor(colour), 1)
            pen.setCosmetic(True)
            scene.addPath(path, pen)

        a, b = self.data["a"], self.data["b"]
        if self.c_rej.isChecked() and len(self.rejected):
            lines(a[self.rejected[:, 0]], b[self.rejected[:, 1]], WEAK)
        if self.c_ver.isChecked() and len(self.data["verified"]):
            v = self.data["verified"]
            lines(a[v[:, 0]], b[v[:, 1]], GOOD)
        if self.c_tie.isChecked() and len(self.ties[0]):
            lines(self.ties[0], self.ties[1], "#2e86ab")
        self.view.fitInView(QRectF(scene.itemsBoundingRect()), Qt.AspectRatioMode.KeepAspectRatio)
