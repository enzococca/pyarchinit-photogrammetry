"""*Cerca picchetti* (from the Riferimenti pane and *Flusso di lavoro*): stakes and benchmarks looked for near points of
known coordinates (:func:`..engine.stakes.find`, in the worker), proposed in a table with their picture (the
orthophoto and the photo that sees the head best), checked against each other (:func:`..engine.stakes.check`: the
suspects and the points displaced unlike the others in red), and only the proposals the user accepts made known ties
(:func:`..engine.stakes.accept`) - one step of *Modifica > Annulla* for every acceptance.

The window is not modal; the proposals live in it only: closing it, or opening another project, forgets them.
"""
from __future__ import annotations

import contextlib
import html
import threading
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..engine import stakes
from ..engine import ties as ties_mod
from . import i18n
from .i18n import tr
from .ties_dialog import edit_state, error_text, record_edit, undo_shortcuts

RED = "#c0392b"
GREY = "#7f8c8d"
ORANGE = "#b9770e"
PREVIEW_SIZE = 256  # pixels of each half of the picture of a proposal

FLAG_TEXTS = {  # the flags of a proposal (engine.stakes) as the window says them
    "no_candidate": "nessuna testa trovata",
    "no_height": "nessuna quota per il punto (niente z e niente DEM lì)",
    "outside_orthophoto": "fuori dall'ortofoto",
    "no_aligned_photos": "nessuna foto allineata",
    "photos_unreadable": "foto illeggibili",
}


def _chunks_with_ortho(doc) -> list:
    out = []
    for ch in doc.chunks:
        path = ch.product_path("orthomosaic")
        if path is not None and Path(path).exists():
            out.append(ch)
    return out


def _referenced(ch) -> bool:
    return any(m.reference.location is not None and m.reference.enabled for m in ch.markers)


def find_data(combo, value) -> int:
    """The row of ``combo`` whose data equals ``value``, -1 when none: ``QComboBox.findData`` compares Python objects
    such as tuples by identity, so a tuple made again is never found."""
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            return i
    return -1


class _PreviewRelay(QObject):
    """Brings the picture of a proposal, made in a thread of its own, to the window (a queued signal)."""

    done = Signal(object, object)  # key, RGB array or the exception


def to_pixmap(rgb: np.ndarray) -> QPixmap:
    """An RGB uint8 array as a pixmap (copied: the array may go)."""
    rgb = np.ascontiguousarray(rgb)
    h, w = rgb.shape[:2]
    img = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(img.copy())


class StakesDialog(QDialog):
    """The search of stakes and benchmarks of a chunk (see the module)."""

    COLS = ("point", "proposal", "photos", "contrast", "shift", "warning")

    @classmethod
    def ask(cls, window) -> "StakesDialog":
        dlg = cls(window)
        dlg.show()
        return dlg

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.doc = window.doc
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setModal(False)
        self.setWindowTitle(tr("Cerca picchetti"))
        self.resize(1100, 720)
        self.proposals: list[stakes.Proposal] = []
        self.chunk_key: int | None = None  # the chunk of the proposals shown
        self.result: dict | None = None  # stakes.check of the proposals shown
        self.searching = False
        self._previews: dict = {}
        self._preview_wanted = None  # the key of the picture the window waits for (made in a thread)
        self._preview_relay = _PreviewRelay(self)
        self._preview_relay.done.connect(self._preview_made)
        self.placed: dict | None = None  # the chunk of the proposals at search time: transform, alignment

        lay = QVBoxLayout(self)
        intro = QLabel(tr("Picchetti e capisaldi cercati vicino a punti di coordinate note: teste scure e rotonde "
                          "sull'ortofoto, confermate dalle foto. Sono solo proposte: diventano legami a coordinate "
                          "note solo quando le accetti. Rileva prima i target codificati (Flusso di lavoro > Rileva "
                          "marker codificati...): su un'ortofoto a bassa risoluzione le celle di un target non "
                          "rilevato possono sembrare teste."))
        intro.setWordWrap(True)
        lay.addWidget(intro)

        form = QFormLayout()
        self.c_chunk = QComboBox()
        form.addRow(tr("Chunk"), self.c_chunk)
        src = QHBoxLayout()
        self.c_source = QComboBox()
        self.e_file = QLineEdit()
        self.e_file.setPlaceholderText(tr("nome,x,y[,z][,precisione]"))
        self.b_file = QPushButton(tr("Scegli..."))
        src.addWidget(self.c_source, 1)
        src.addWidget(self.e_file, 2)
        src.addWidget(self.b_file)
        form.addRow(tr("Punti noti"), src)
        self.e_crs = QLineEdit()
        self.e_crs.setPlaceholderText(tr("vuoto: il sistema del chunk"))
        form.addRow(tr("Sistema del CSV"), self.e_crs)
        size = QHBoxLayout()
        self.s_radius = QDoubleSpinBox()
        self.s_radius.setRange(0.05, 100.0)
        self.s_radius.setDecimals(2)
        self.s_radius.setValue(3.0)
        self.s_radius.setSuffix(" m")
        self.s_head_lo = QDoubleSpinBox()
        self.s_head_hi = QDoubleSpinBox()
        for s, v in ((self.s_head_lo, 0.03), (self.s_head_hi, 0.12)):
            s.setRange(0.005, 2.0)
            s.setDecimals(3)
            s.setSingleStep(0.005)
            s.setValue(v)
            s.setSuffix(" m")
        size.addWidget(QLabel(tr("Raggio")))
        size.addWidget(self.s_radius)
        size.addSpacing(16)
        size.addWidget(QLabel(tr("Testa da")))
        size.addWidget(self.s_head_lo)
        size.addWidget(QLabel(tr("fino a")))
        size.addWidget(self.s_head_hi)
        size.addStretch(1)
        self.b_search = QPushButton(tr("Cerca"))
        self.b_search.setDefault(True)
        size.addWidget(self.b_search)
        form.addRow(tr("Ricerca"), size)
        lay.addLayout(form)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels([tr("Punto"), tr("Proposta (x, y, z)"), tr("Foto concordi"),
                                              tr("Contrasto"), tr("Spostamento (m)"), tr("Avviso")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        ll.addWidget(self.table, 1)
        self.summary = QLabel("")
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setWordWrap(True)
        ll.addWidget(self.summary)
        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel("<b>" + html.escape(tr("Ortofoto e foto")) + "</b>"))
        self.preview = QLabel(tr("Scegli una proposta nella tabella"))
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(2 * PREVIEW_SIZE, PREVIEW_SIZE)
        rl.addWidget(self.preview)
        self.preview_text = QLabel("")
        self.preview_text.setWordWrap(True)
        rl.addWidget(self.preview_text)
        rl.addStretch(1)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([700, 2 * PREVIEW_SIZE + 8])
        lay.addWidget(split, 1)

        buttons = QHBoxLayout()
        self.b_accept = QPushButton(tr("Accetta selezionati"))
        self.b_accept.setToolTip(tr("Le proposte scelte diventano legami a coordinate note (un passo di Annulla)"))
        self.b_accept_agreeing = QPushButton(tr("Accetta i concordi"))
        self.b_accept_agreeing.setToolTip(tr("Accetta le proposte confermate dalle foto che non sono sospette"))
        self.b_discard = QPushButton(tr("Scarta"))
        self.b_discard.setToolTip(tr("Togli le proposte scelte dall'elenco (il progetto non cambia)"))
        self.b_other = QPushButton(tr("Scegli un altro candidato"))
        self.b_other.setToolTip(tr("La proposta scelta passa alla testa successiva trovata vicino al punto"))
        self.b_close = QPushButton(tr("Chiudi"))
        for b in (self.b_accept, self.b_accept_agreeing, self.b_discard, self.b_other):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(self.b_close)
        lay.addLayout(buttons)

        self.c_source.currentIndexChanged.connect(self._source_changed)
        self.c_chunk.currentIndexChanged.connect(self._chunk_changed)
        self.b_file.clicked.connect(self._choose_file)
        self.b_search.clicked.connect(self.search)
        self.b_accept.clicked.connect(lambda: self.accept_selected())
        self.b_accept_agreeing.clicked.connect(lambda: self.accept_agreeing())
        self.b_discard.clicked.connect(lambda: self.discard_selected())
        self.b_other.clicked.connect(lambda: self.other_candidate())
        self.b_close.clicked.connect(self.close)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.finished.connect(self._forget)
        window.refreshed.connect(self.follow)  # chunks removed, moved or aligned again while the window is open
        undo_shortcuts(self, window)
        self._fill_chunks()
        self._update_buttons()

    # ------------------------------------------------------------------ fields
    def _fill_chunks(self) -> None:
        chunks = _chunks_with_ortho(self.doc)
        self.c_chunk.blockSignals(True)
        self.c_chunk.clear()
        for ch in chunks:
            self.c_chunk.addItem(ch.label, ch.key)
        active = getattr(self.window, "chunk", None)
        if active is not None:
            i = self.c_chunk.findData(active.key)
            if i >= 0:
                self.c_chunk.setCurrentIndex(i)
        self.c_chunk.blockSignals(False)
        self._fill_sources()

    def _fill_sources(self) -> None:
        """CSV, the known ties of the project, and the markers with coordinates of every chunk."""
        current = self.c_source.currentData()
        self.c_source.blockSignals(True)
        self.c_source.clear()
        self.c_source.addItem(tr("File CSV"), ("csv", None))
        if stakes.known_points("ties", doc=self.doc):
            self.c_source.addItem(tr("Legami a coordinate note del progetto"), ("ties", None))
        for ch in self.doc.chunks:
            if _referenced(ch):
                self.c_source.addItem(tr("Marker con coordinate di {0}", ch.label), ("chunk", ch.key))
        i = find_data(self.c_source, current) if current is not None else -1
        if i < 0:  # the markers of the chunk searched when it has some, else the known ties, else a file
            key = self.c_chunk.currentData()
            for want in (("chunk", key), ("ties", None)):
                i = find_data(self.c_source, want)
                if i >= 0:
                    break
        self.c_source.setCurrentIndex(max(i, 0))
        self.c_source.blockSignals(False)
        self._source_changed()

    def _source_changed(self, *_a) -> None:
        csv = (self.c_source.currentData() or ("csv", None))[0] == "csv"
        for w in (self.e_file, self.b_file, self.e_crs):
            w.setEnabled(csv)

    def _chunk_changed(self, *_a) -> None:
        self._fill_sources()

    def _choose_file(self) -> None:
        path, _f = QFileDialog.getOpenFileName(self, tr("Punti noti"), str(Path(self.e_file.text()).parent)
                                               if self.e_file.text() else "", tr("Tabelle (*.csv *.txt)"))
        if path:
            self.e_file.setText(path)

    def set_csv(self, path) -> None:
        """The known points from the CSV file ``path`` (as *File CSV* and the file chosen)."""
        self.c_source.setCurrentIndex(find_data(self.c_source, ("csv", None)))
        self.e_file.setText(str(path))

    def set_source(self, kind: str, key: int | None = None) -> None:
        """The known points from ``kind`` "ties" or "chunk" (with the ``key`` of the chunk)."""
        i = find_data(self.c_source, (kind, key))
        if i < 0:
            raise KeyError(f"no source {kind} {key}")
        self.c_source.setCurrentIndex(i)

    def _points(self):
        kind, key = self.c_source.currentData() or ("csv", None)
        if kind == "ties":
            return stakes.known_points("ties", doc=self.doc)
        if kind == "chunk":
            ch = next((c for c in self.doc.chunks if c.key == key), None)
            if ch is None:
                raise KeyError(tr("il chunk dei marker scelti non c'è più"))
            return stakes.known_points(ch)
        text = self.e_file.text().strip()
        if not text:
            raise ValueError(tr("Scegli il file CSV dei punti noti."))
        return stakes.known_points(Path(text), crs=self.e_crs.text().strip() or None)

    # ------------------------------------------------------------------ search
    def search(self) -> bool:
        """*Cerca*: :func:`..engine.stakes.find` in the worker; the proposals shown when it ends (nothing stored).
        Returns whether the search started."""
        key = self.c_chunk.currentData()
        if key is None:
            self.window._warn(tr("Nessun chunk con un'ortofoto: costruiscila prima."))
            return False
        try:
            points = self._points()
        except (ValueError, KeyError, OSError) as exc:
            self.window._warn(tr("Cerca picchetti: {0}", error_text(exc)))
            return False
        if not points:
            self.window._warn(tr("Cerca picchetti: nessun punto noto nella sorgente scelta."))
            return False
        lo, hi = self.s_head_lo.value(), self.s_head_hi.value()
        if not lo < hi:
            self.window._warn(tr("Cerca picchetti: la testa più piccola deve essere minore della più grande."))
            return False
        doc, radius = self.doc, self.s_radius.value()

        def work(p):
            try:
                return stakes.find(doc, key, points, radius=radius, head=(lo, hi), progress=p)
            except (ties_mod.TieError, ValueError, KeyError) as exc:
                return {"error": exc}

        self.searching = True
        self._update_buttons()
        started = self.window.runner.run("Cerca picchetti", work, lambda result: self._found(result, doc, key),
                                         self._failed)
        if not started:
            self.searching = False
            self._update_buttons()
        return started

    def _gone(self, doc) -> bool:
        try:
            self.isVisible()
        except RuntimeError:
            return True
        return doc is not self.doc

    def _failed(self, summary: str, details: str = "") -> None:
        with contextlib.suppress(RuntimeError):
            self.searching = False
            self._update_buttons()
        self.window._error(summary, details)

    def _found(self, result, doc, key) -> None:
        if self._gone(doc):
            return
        self.searching = False
        if isinstance(result, dict):
            self._update_buttons()
            self.window._warn(tr("Cerca picchetti: {0}", error_text(result["error"])))
            return
        self.proposals = list(result)
        self.chunk_key = key
        ch = self.chunk()
        self.placed = None if ch is None else {"transform": np.asarray(ch.transform, float).copy(),
                                               "alignment": ties_mod._alignment(ch)}
        self._previews.clear()
        self._refill()
        found = sum(p.best is not None for p in self.proposals)
        self.window.log_pane.info(tr("Cerca picchetti: {0} proposte per {1} punti noti (niente è stato aggiunto)",
                                     found, len(self.proposals)))

    def chunk(self):
        return next((c for c in self.doc.chunks if c.key == self.chunk_key), None)

    def stale(self) -> str | None:
        """Why the proposals shown no longer match their chunk (removed, moved by a solve, aligned again), or None.
        Accepting stays right after a move (the ends are kept in the internal frame) and is refused after a new
        alignment or a removal; the coordinates, displacements and red rows of the table are those of the search."""
        if self.chunk_key is None or not self.proposals:
            return None
        ch = self.chunk()
        if ch is None:
            return tr("il chunk delle proposte non c'è più: cerca di nuovo")
        if self.placed is None:
            return None
        if self.placed["alignment"] != ties_mod._alignment(ch):
            return tr("il chunk è stato allineato di nuovo dopo la ricerca: cerca di nuovo")
        if not np.allclose(np.asarray(ch.transform, float), self.placed["transform"], rtol=0, atol=1e-9):
            return tr("il chunk è stato spostato dopo la ricerca: coordinate, spostamenti e righe rosse sono di "
                      "prima - cerca di nuovo")
        return None

    def follow(self) -> None:
        """The main window refreshed: the chunks of the combo read again (one removed, renamed, a new orthophoto) and
        the note of proposals that no longer match their chunk."""
        try:
            self.isVisible()
        except RuntimeError:
            return
        if self.doc is not self.window.doc:
            return
        seen = [(c.key, c.label) for c in _chunks_with_ortho(self.doc)]
        if seen != [(self.c_chunk.itemData(i), self.c_chunk.itemText(i)) for i in range(self.c_chunk.count())]:
            key = self.c_chunk.currentData()
            self._fill_chunks()
            i = self.c_chunk.findData(key)
            if i >= 0:
                self.c_chunk.blockSignals(True)
                self.c_chunk.setCurrentIndex(i)
                self.c_chunk.blockSignals(False)
        self._fill_sources()  # the markers of a chunk removed (or newly referenced) as a source
        if self.chunk_key is not None and self.chunk() is None:  # the picture of a chunk gone
            self._preview_wanted = None
            self.preview.setPixmap(QPixmap())
            self.preview.setText(tr("Scegli una proposta nella tabella"))
            self.preview_text.setText("")
        self._refill()

    # ------------------------------------------------------------------ table
    def _warning(self, p: stakes.Proposal, suspects: set, outliers: set) -> str:
        parts = []
        name = p.point.name
        if name in suspects:
            parts.append(tr("sospetto: le distanze dagli altri punti non tornano"))
        if name in outliers:
            parts.append(tr("spostato diversamente dagli altri"))
        for f in p.flags:
            if f in FLAG_TEXTS:
                parts.append(tr(FLAG_TEXTS[f]))
        if p.others:
            parts.append(tr("{0} altri candidati", len(p.others)))
        return "; ".join(parts)

    def _refill(self) -> None:
        self.result = stakes.check(self.proposals) if self.proposals else None
        suspects = set(self.result["suspects"]) if self.result else set()
        outliers = set(self.result["outliers"]) if self.result else set()
        selected = self.selected_names()
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.proposals))
        for i, p in enumerate(self.proposals):
            b = p.best
            if b is None:
                cells = [p.point.name, "", "", "", "", self._warning(p, suspects, outliers)]
            else:
                d = p.displacement
                shift = "" if d is None else i18n.number(float(np.hypot(d[0], d[1])), 3)
                photos = tr("solo ortofoto") if b.ortho_only else i18n.number(b.agreeing, 0)
                cells = [p.point.name, ", ".join(i18n.number(float(v), 3) for v in b.world), photos,
                         i18n.number(b.contrast, 0), shift, self._warning(p, suspects, outliers)]
            red = p.point.name in suspects or p.point.name in outliers
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, p.point.name)
                if red:
                    item.setForeground(QColor(RED))
                elif b is None:
                    item.setForeground(QColor(GREY))
                self.table.setItem(i, c, item)
        self.table.blockSignals(False)
        self._select(selected)
        med = self.result["displacement_median"] if self.result else None
        if med is not None and med[0] is not None:
            text = tr("Spostamento mediano dai punti noti: {0} m in pianta.",
                      i18n.number(float(np.hypot(med[0], med[1])), 3))
        else:
            text = ""
        if suspects or outliers:
            text += " " + tr("In rosso: da controllare prima di accettarli.")
        why = self.stale()
        if why:
            text = f'<span style="color:{ORANGE}">{html.escape(why)}</span><br>' + html.escape(text.strip())
        else:
            text = html.escape(text.strip())
        self.summary.setText(text)
        self._update_buttons()
        self._selection_changed()

    def _select(self, names) -> None:
        self.table.clearSelection()
        mode = self.table.selectionMode()
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        for i, p in enumerate(self.proposals):
            if p.point.name in names:
                self.table.selectRow(i)
        self.table.setSelectionMode(mode)

    def selected_names(self) -> list[str]:
        rows = sorted({ix.row() for ix in self.table.selectedIndexes()})
        return [self.proposals[r].point.name for r in rows if r < len(self.proposals)]

    def select(self, names) -> None:
        """Select the proposals of the points ``names`` in the table."""
        self._select(list(names))
        self._selection_changed()

    def _proposal(self, name: str) -> stakes.Proposal | None:
        return next((p for p in self.proposals if p.point.name == name), None)

    def _update_buttons(self) -> None:
        with contextlib.suppress(RuntimeError):
            idle = not self.searching
            have = bool(self.proposals)
            sel = [self._proposal(n) for n in self.selected_names()]
            self.b_search.setEnabled(idle)
            self.b_accept.setEnabled(idle and any(p is not None and p.best is not None for p in sel))
            self.b_accept_agreeing.setEnabled(idle and have)
            self.b_discard.setEnabled(idle and bool(sel))
            self.b_other.setEnabled(idle and len(sel) == 1 and sel[0] is not None and bool(sel[0].others))

    # ------------------------------------------------------------------ preview
    def _selection_changed(self) -> None:
        self._update_buttons()
        names = self.selected_names()
        p = self._proposal(names[0]) if names else None
        ch = self.chunk()
        if p is None or ch is None:
            self.preview.setPixmap(QPixmap())
            self.preview.setText(tr("Scegli una proposta nella tabella"))
            self.preview_text.setText("")
            return
        key = (p.point.name, id(p.best))
        self._preview_wanted = key
        pix = self._previews.get(key)
        if pix is None:  # read in a thread: a slow photo (a network or "online only" folder) never stops the window
            self.preview.setPixmap(QPixmap())
            self.preview.setText(tr("Lettura dell'ortofoto e della foto..."))
            relay = self._preview_relay

            def make():
                try:
                    out = stakes.preview(ch, p, size=PREVIEW_SIZE)
                except Exception as exc:  # noqa: BLE001 - said in the window
                    out = exc
                with contextlib.suppress(RuntimeError):  # the window closed meanwhile
                    relay.done.emit(key, out)

            threading.Thread(target=make, name="ppg-stake-preview", daemon=True).start()
        else:
            self.preview.setPixmap(pix)
        self._preview_caption(p)

    def _preview_made(self, key, out) -> None:
        if isinstance(out, np.ndarray):
            self._previews[key] = to_pixmap(out)
        if key != self._preview_wanted:
            return
        self._preview_wanted = None
        if isinstance(out, np.ndarray):
            self.preview.setPixmap(self._previews[key])
        else:
            self.preview.setPixmap(QPixmap())
            self.preview.setText(error_text(out))

    def wait_preview(self, timeout_s: float = 30.0) -> None:
        """Wait (processing events) for the picture asked: for scripts and tests."""
        import time

        from PySide6.QtWidgets import QApplication

        t0 = time.monotonic()
        while self._preview_wanted is not None and time.monotonic() - t0 < timeout_s:
            QApplication.processEvents()
            time.sleep(0.01)

    def _preview_caption(self, p) -> None:
        if p.best is None:
            self.preview_text.setText(tr("Croce rossa sul punto noto: nessuna testa trovata."))
        else:
            self.preview_text.setText(tr("Croce rossa sulla testa proposta: a sinistra l'ortofoto, a destra la foto "
                                         "che la vede meglio; diametro {0} m.", i18n.number(p.best.diameter, 3)))

    def preview_pixmap(self) -> QPixmap | None:
        pix = self.preview.pixmap()
        return None if pix is None or pix.isNull() else pix

    # ------------------------------------------------------------------ actions
    def other_candidate(self) -> bool:
        """*Scegli un altro candidato*: the selected proposal takes its next candidate. Returns whether it changed."""
        names = self.selected_names()
        if len(names) != 1:
            return False
        i = next(k for k, p in enumerate(self.proposals) if p.point.name == names[0])
        new = stakes.next_candidate(self.proposals[i])
        if new is self.proposals[i]:
            return False
        self.proposals[i] = new
        self._refill()
        return True

    def discard_selected(self) -> int:
        """*Scarta*: the selected proposals taken out of the list (the project does not change)."""
        names = set(self.selected_names())
        before = len(self.proposals)
        self.proposals = [p for p in self.proposals if p.point.name not in names]
        self._refill()
        return before - len(self.proposals)

    def accept_selected(self) -> list:
        """*Accetta selezionati*: the selected proposals with a candidate made known ties (one step of Annulla)."""
        names = [n for n in self.selected_names() if (p := self._proposal(n)) is not None and p.best is not None]
        return self._accept(names)

    def accept_agreeing(self) -> list:
        """*Accetta i concordi*: the proposals confirmed by the photos that are neither suspect nor displaced unlike
        the others."""
        bad = set()
        if self.result:
            bad = set(self.result["suspects"]) | set(self.result["outliers"])
        names = [p.point.name for p in self.proposals
                 if p.best is not None and not p.best.ortho_only and p.point.name not in bad]
        if not names:
            self.window._info(tr("Cerca picchetti"), tr("Nessuna proposta confermata dalle foto e senza avvisi."))
            return []
        return self._accept(names)

    def _accept(self, names: list[str]) -> list:
        if not names:
            return []
        ch = self.chunk()
        if ch is None:
            self.window._warn(tr("Cerca picchetti: {0}", tr("il chunk delle proposte non c'è più: cerca di nuovo")))
            return []
        chosen = [p for p in self.proposals if p.point.name in names]
        before = edit_state(self.doc)
        try:
            made = stakes.accept(self.doc, ch, chosen)
        except (ValueError, KeyError, ties_mod.TieError) as exc:
            # a failure half way leaves what was made before it: one step of Annulla all the same (none if nothing)
            if record_edit(self.window, tr("Accetta i picchetti"), before):
                with contextlib.suppress(AttributeError, RuntimeError):
                    self.window._ties_made()
            self.window._warn(tr("Cerca picchetti: {0}", error_text(exc)))
            return []
        record_edit(self.window, tr("Accetta i picchetti"), before)
        self.window.log_pane.info(tr("Picchetti accettati come legami a coordinate note: {0}",
                                     ", ".join(t.name for t in made)))
        accepted = {t.name for t in made} | set(names)
        self.proposals = [p for p in self.proposals if p.point.name not in accepted]
        self._previews.clear()
        self._refill()
        with contextlib.suppress(AttributeError, RuntimeError):
            self.window._ties_made()
        return made

    # ------------------------------------------------------------------ closing
    def _forget(self, *_args) -> None:
        if getattr(self, "_forgotten", False):
            return
        self._forgotten = True
        with contextlib.suppress(RuntimeError, TypeError):
            self.window.refreshed.disconnect(self.follow)
        if getattr(self.window, "_stakes_dialog", None) is self:
            self.window._stakes_dialog = None

    def closeEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        self._forget()
        super().closeEvent(ev)


__all__ = ["StakesDialog", "to_pixmap"]
