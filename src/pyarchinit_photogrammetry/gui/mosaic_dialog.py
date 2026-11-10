"""*Strumenti > Ortofoto e DEM uniti...*: the orthophotos or the DEMs of several tied chunks merged into one raster
(:mod:`..engine.mosaic`), in the system of the first chunk of the list.

The chunks are ticked in a list whose order is their priority (*Su* / *Giù*): where two have data the first one wins.
Each chunk can have an area - drawn on the 2D view (*Disegna l'area*) or taken from a layer of its polygons (a colour
mask, a drawing: *Area da un layer*) - that wins over the list inside it; outside every area the cells come from the
list, faded toward white (*Sfondo sfumato*) or left empty (*Nessuno sfondo*). *Costruisci* runs in the worker; the
merged product goes in the node *Prodotti uniti* of the Area di lavoro and is shown in the 2D view, with the chunk each
cell comes from under the cursor. The build is one step of *Modifica > Annulla* (:class:`MosaicBuiltStep`: the undo
puts its folder aside through :mod:`..core.removal`). The window is not modal.
"""
from __future__ import annotations

import contextlib

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ..engine import mosaic
from . import i18n
from .i18n import number, tr
from .ties_dialog import undo_shortcuts

AREA_COLOUR = "#e07000"
AREAS_VIEW_LAYER = "Aree da unire"  # the layer of the 2D view with the areas chosen (the view only)


def error_text(exc: Exception) -> str:
    """An error of the merge in the language of the interface: :class:`..engine.mosaic.MosaicError` and the ties'
    errors carry both."""
    italian, english = getattr(exc, "italian", None), getattr(exc, "english", None)
    if italian and english:
        return italian if i18n.current() == "it" else english
    return i18n.translate(str(exc))


def polygon_layers(chunk) -> list[str]:
    """The layers of free drawings of ``chunk`` that have polygons: where an area can come from."""
    from .water_dialog import polygon_layers as layers

    return layers(chunk)


class MosaicBuiltStep:
    """The build of a merged product as a step of *Modifica > Annulla*: undo takes it out of the document and puts
    its folder aside (``removal``, a :meth:`..core.removal.MosaicRemoval.added`), redo brings it back."""

    def __init__(self, window, removal, mid: str):
        self.window, self.removal, self.mid = window, removal, mid

    def undo(self) -> None:
        self.window._mosaic_release([self.mid])
        self.removal.redo()
        self.window._mosaics_changed()

    def redo(self) -> None:
        self.removal.undo()
        self.window._mosaics_changed()


class MosaicDialog(QDialog):
    """The merged orthophoto or DEM of several chunks (see the module)."""

    @classmethod
    def ask(cls, window) -> "MosaicDialog":
        dlg = cls(window)
        dlg.show()
        return dlg

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.doc = window.doc
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setModal(False)
        self.setWindowTitle(tr("Ortofoto e DEM uniti"))
        self.working = False
        # chunk key -> (polygon, where from, the system it is in): carried into the system of the first chunk at the build
        self.areas: dict[int, tuple[object, str, str | None]] = {}

        lay = QVBoxLayout(self)
        intro = QLabel(tr("Le ortofoto o i DEM di più chunk agganciati uniti in un raster solo, nel sistema del primo "
                          "chunk dell'elenco. L'ordine dell'elenco è la priorità: dove due chunk hanno dati vince il "
                          "primo. Un'area di un chunk (disegnata o da un layer) vince sull'elenco al suo interno; "
                          "fuori da tutte le aree le celle vengono dall'elenco, sfumate verso il bianco, o restano "
                          "vuote."))
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([tr("Chunk"), tr("Prodotto"), tr("Area")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        head.setStretchLastSection(True)
        lay.addWidget(self.table)
        rows = QHBoxLayout()
        self.b_up = QPushButton(tr("Su"))
        self.b_down = QPushButton(tr("Giù"))
        self.b_draw = QPushButton(tr("Disegna l'area"))
        self.b_draw.setToolTip(tr("Un poligono sulla vista 2D: dentro, le celle vengono da questo chunk"))
        self.b_layer = QPushButton(tr("Area da un layer..."))
        self.b_layer.setToolTip(tr("I poligoni di un layer dei disegni liberi del chunk (una maschera dal colore, un "
                                   "disegno)"))
        self.b_clear = QPushButton(tr("Togli l'area"))
        for b in (self.b_up, self.b_down, self.b_draw, self.b_layer, self.b_clear):
            rows.addWidget(b)
        rows.addStretch(1)
        lay.addLayout(rows)

        form = QFormLayout()
        self.c_product = QComboBox()
        self.c_product.addItem(tr("Ortofoto"), "orthomosaic")
        self.c_product.addItem(tr("DEM"), "dem")
        form.addRow(tr("Prodotto"), self.c_product)
        self.s_res = QDoubleSpinBox()
        self.s_res.setRange(0.0, 100.0)
        self.s_res.setDecimals(4)
        self.s_res.setSingleStep(0.005)
        self.s_res.setSuffix(" m")
        self.s_res.setSpecialValueText(tr("la più fine delle sorgenti"))
        self.s_res.setValue(0.0)
        form.addRow(tr("Risoluzione"), self.s_res)
        self.c_background = QComboBox()
        self.c_background.addItem(tr("Sfumato (fuori dalle aree)"), "faded")
        self.c_background.addItem(tr("Nessuno (solo le aree)"), "none")
        self.c_background.setToolTip(tr("Con delle aree: le celle fuori da tutte vengono dall'elenco, sfumate verso il "
                                        "bianco nell'ortofoto (il DEM tiene le quote), o restano vuote"))
        form.addRow(tr("Sfondo"), self.c_background)
        self.e_name = QLineEdit()
        self.e_name.setPlaceholderText(tr("automatico"))
        form.addRow(tr("Nome"), self.e_name)
        lay.addLayout(form)
        self.l_state = QLabel()
        self.l_state.setWordWrap(True)
        lay.addWidget(self.l_state)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.b_build = QPushButton(tr("Costruisci"))
        self.b_close = QPushButton(tr("Chiudi"))
        buttons.addWidget(self.b_build)
        buttons.addWidget(self.b_close)
        lay.addLayout(buttons)
        undo_shortcuts(self, window)
        for button in self.findChildren(QPushButton):  # Enter in a field does not press a button (nor makes it
            button.setAutoDefault(False)  # the default one, in bold: its text would no longer fit)

        self.b_up.clicked.connect(lambda: self.move(-1))
        self.b_down.clicked.connect(lambda: self.move(1))
        self.b_draw.clicked.connect(self.start_area)
        self.b_layer.clicked.connect(self.area_from_layer)
        self.b_clear.clicked.connect(self.clear_area)
        self.b_build.clicked.connect(self.build)
        self.b_close.clicked.connect(self.close)
        self.c_product.currentIndexChanged.connect(self.refresh)
        self.table.itemChanged.connect(lambda *_a: self._update_buttons())
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.fill()
        self.resize(640, 560)

    # ------------------------------------------------------------------ the list
    def _gone(self) -> bool:
        try:
            self.isVisible()
        except RuntimeError:
            return True
        return self.window.doc is not self.doc

    def _chunk(self, row: int):
        item = self.table.item(row, 0)
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return next((c for c in self.doc.chunks if c.key == key), None)

    def fill(self, order: list | None = None, ticked: set | None = None) -> None:
        """The chunks of the project in the list (``order``: their keys in this order first; ``ticked``: the keys
        ticked, by default those with the product)."""
        product = self.c_product.currentData()
        chunks = list(self.doc.chunks)
        if order:
            rank = {k: i for i, k in enumerate(order)}
            place = {c.key: i for i, c in enumerate(chunks)}
            chunks.sort(key=lambda c: rank.get(c.key, len(rank) + place[c.key]))
        self.table.blockSignals(True)
        self.table.setRowCount(len(chunks))
        for r, ch in enumerate(chunks):
            has = self._product(ch, product) is not None
            it = QTableWidgetItem(ch.label)
            it.setData(Qt.ItemDataRole.UserRole, ch.key)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            on = (ch.key in ticked) if ticked is not None else has
            it.setCheckState(Qt.CheckState.Checked if on and has else Qt.CheckState.Unchecked)
            if not has:
                it.setFlags(Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(r, 0, it)
            self.table.setItem(r, 1, QTableWidgetItem(tr("sì") if has else tr("manca")))
            self.table.setItem(r, 2, QTableWidgetItem(self._area_text(ch)))
        self.table.blockSignals(False)
        self._update_buttons()

    @staticmethod
    def _product(chunk, product: str):
        path = chunk.dem_path if product == "dem" else chunk.orthomosaic_path
        return path if path is not None and path.exists() else None

    def _area_text(self, chunk) -> str:
        area = self.areas.get(chunk.key)
        if area is None:
            return tr("nessuna")
        poly, source, system = area
        text = tr("{0}, {1} m2", source, number(float(poly.area), 2))
        chosen = self.chosen()
        if chosen and not self._same(system, chosen[0].crs):
            try:
                self.area_in(chunk.key, chosen[0].crs)
                text += tr(" (in {0}: riproiettata nel sistema del primo chunk)", system or "LOCAL")
            except ValueError:
                text += tr(" (in {0}: non si può usare con il primo chunk)", system or "LOCAL")
        return text

    def refresh(self, *_a) -> None:
        """The list again (another product, a chunk built or removed), the order and the ticks kept."""
        if self._gone():
            return
        self.fill(self.order(all_rows=True), {c.key for c in self.chosen()})

    def order(self, all_rows: bool = False) -> list:
        """The keys of the chunks in the order of the list (ticked only, unless ``all_rows``)."""
        out = []
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it is not None and (all_rows or it.checkState() == Qt.CheckState.Checked):
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def chosen(self) -> list:
        """The chunks ticked, in their order of priority."""
        keys = self.order()
        return [c for k in keys for c in self.doc.chunks if c.key == k]

    def set_order(self, labels: list[str]) -> None:
        """The chunks ``labels`` ticked in this order, before the others (unticked): for scripts and tests."""
        keys = [c.key for lab in labels for c in self.doc.chunks if c.label == lab]
        self.fill(keys, set(keys))

    def move(self, step: int) -> None:
        row = self.table.currentRow()
        to = row + step
        if row < 0 or not 0 <= to < self.table.rowCount():
            return
        keys = self.order(all_rows=True)
        keys[row], keys[to] = keys[to], keys[row]
        self.fill(keys, {c.key for c in self.chosen()})
        self.table.selectRow(to)

    def _current(self):
        row = self.table.currentRow()
        return self._chunk(row) if row >= 0 else None

    def _update_buttons(self) -> None:
        with contextlib.suppress(RuntimeError):
            row = self.table.currentRow()
            current = self._current()
            self.b_up.setEnabled(not self.working and row > 0)
            self.b_down.setEnabled(not self.working and 0 <= row < self.table.rowCount() - 1)
            self.b_draw.setEnabled(not self.working and current is not None)
            self.b_layer.setEnabled(not self.working and current is not None and bool(polygon_layers(current)))
            self.b_clear.setEnabled(not self.working and current is not None and current.key in self.areas)
            chosen = self.chosen()
            self.table.blockSignals(True)  # the areas said against the first chunk ticked now
            for r in range(self.table.rowCount()):
                ch, it = self._chunk(r), self.table.item(r, 2)
                if ch is not None and it is not None:
                    it.setText(self._area_text(ch))
            self.table.blockSignals(False)
            self.c_background.setEnabled(any(c.key in self.areas for c in chosen))
            self.b_build.setEnabled(not self.working and bool(chosen))
            if not chosen:
                self.l_state.setText(tr("Spunta i chunk da unire (quelli che hanno il prodotto)."))
            else:
                first = chosen[0]
                self.l_state.setText(tr("{0} chunk; sistema di {1}: {2}", len(chosen), first.label, first.crs))

    # ------------------------------------------------------------------ areas
    def _view_system(self) -> str | None:
        """The system of the coordinates of the 2D view: a merged product's when one is shown, else the active
        chunk's."""
        shown = self.window._mosaic_on_view()
        if shown is not None:
            return shown.get("crs")
        return self.window.chunk.crs if self.window.chunk is not None else None

    @staticmethod
    def _same(a: str | None, b: str | None) -> bool:
        """Two systems whose coordinates need no conversion: the same, or two local frames (as the ties take them)."""
        from ..core import crs as crs_mod

        return a == b or (crs_mod.is_local(a) and crs_mod.is_local(b))

    def area_in(self, key, system: str | None):
        """The area of the chunk ``key`` in ``system``: carried there from the system it was drawn or taken in when
        both are systems of the world; :class:`..engine.mosaic.MosaicError` for a local frame with a system of the
        world, or two frames only PROJ's approximation would join."""
        import shapely

        from ..core import crs as crs_mod

        poly, _source, have = self.areas[key]
        if self._same(have, system):
            return poly
        label = next((c.label for c in self.doc.chunks if c.key == key), str(key))
        if crs_mod.is_local(have) or crs_mod.is_local(system):
            raise mosaic.MosaicError(
                f"the area of {label} is in {have or 'LOCAL'} and the first chunk in {system or 'LOCAL'}: a local "
                "frame and a system of the world cannot be joined. Draw the area again, or put a chunk in its system "
                "first", f"l'area di {label} è in {have or 'LOCAL'} e il primo chunk in {system or 'LOCAL'}: un "
                "sistema locale e uno del mondo non si uniscono. Disegna di nuovo l'area, o metti primo un chunk nel "
                "suo sistema")
        if mosaic.frame_problem(have, system):
            raise mosaic._frame_error(f"the area of {label}", f"l'area di {label}", have, system)
        chunk = next((c for c in self.doc.chunks if c.key == key), None)
        return shapely.transform(poly, lambda xy: mosaic._to_system(xy, have, system, False, chunk))

    def start_area(self) -> bool:
        """A polygon drawn on the 2D view (:meth:`MainWindow.request_geometry`): the area of the chunk selected."""
        ch = self._current()
        if self._gone() or ch is None or self.window._plan_only():
            return False
        key, system = ch.key, self._view_system()  # the coordinates of the view: carried at the build if need be
        return self.window.request_geometry(
            "polygon", lambda xy: self.set_area(key, xy, tr("disegnata"), system),
            hint=tr("L'area di {0}: clic sui vertici, doppio clic o Invio per chiudere (Esc annulla)", ch.label))

    def set_area(self, key, xy, source: str, system: str | None = None) -> None:
        """The area of the chunk ``key``: a polygon (``(n, 2+)`` vertices, or a shapely polygon) in ``system`` (by
        default the system of the 2D view), carried into the system of the first chunk at the build."""
        from shapely.geometry import Polygon

        if self._gone():
            return
        poly = xy if hasattr(xy, "area") else Polygon(np.asarray(xy, float)[:, :2])
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area <= 0:
            self.window._warn(tr("Ortofoto e DEM uniti: l'area è vuota"))
            return
        self.areas[key] = (poly, source, self._view_system() if system is None else system)
        self.refresh()
        self._draw_areas()

    def choose_layer(self, chunk) -> str | None:
        """The layer of polygons of ``chunk`` to take the area from (a question; tests replace it)."""
        names = polygon_layers(chunk)
        if not names:
            return None
        name, ok = QInputDialog.getItem(self, tr("Area da un layer"), tr("Layer di {0}:", chunk.label), names, 0, False)
        return name if ok and name else None

    def area_from_layer(self) -> bool:
        from ..engine import colour_mask

        ch = self._current()
        if self._gone() or ch is None:
            return False
        layer = self.choose_layer(ch)
        if not layer:
            return False
        try:
            poly = colour_mask.union(ch, layer)
        except ValueError as exc:
            self.window._warn(tr("Ortofoto e DEM uniti: {0}", error_text(exc)))
            return False
        self.set_area(ch.key, poly, tr("dal layer {0}", layer), ch.crs)  # the layer is in the world of its chunk
        return True

    def clear_area(self) -> None:
        ch = self._current()
        if ch is not None and self.areas.pop(ch.key, None) is not None:
            self.refresh()
            self._draw_areas()

    def _draw_areas(self) -> None:
        """The areas on the 2D view (a layer of the view only), when it shows the plan."""
        view = self.window.ortho_view
        self._clear_areas()
        if self.window.drawings_frame is not None or view.tr is None or not self.areas:
            return
        labels = {c.key: c.label for c in self.doc.chunks}
        shown = self._view_system()
        with view.batch():
            for key in list(self.areas):
                try:  # in the coordinates of the view
                    poly = self.area_in(key, shown)
                except ValueError:
                    continue
                for pg in getattr(poly, "geoms", [poly]):
                    view.add_polygon(np.asarray(pg.exterior.coords, float)[:-1], label=labels.get(key, ""),
                                     color=AREA_COLOUR, fill_alpha=40, layer=AREAS_VIEW_LAYER)

    def _clear_areas(self) -> None:
        with contextlib.suppress(RuntimeError, AttributeError):
            view = self.window.ortho_view
            if AREAS_VIEW_LAYER in view.layers:
                view.remove_layer(AREAS_VIEW_LAYER)

    # ------------------------------------------------------------------ build
    def build(self) -> bool:
        """The merged product built in the worker, then put in the tree and shown; one step of *Modifica >
        Annulla*. Returns whether it started."""
        if self._gone() or self.working:
            return False
        chunks = self.chosen()
        if not chunks:
            return False
        try:  # each area in the system of the first chunk
            areas = [(c, self.area_in(c.key, chunks[0].crs)) for c in chunks if c.key in self.areas]
        except ValueError as exc:
            self.window._warn(tr("Ortofoto e DEM uniti: {0}", error_text(exc)))
            self.refresh()
            return False
        product = self.c_product.currentData()
        res = self.s_res.value() or None
        background = self.c_background.currentData()
        name = self.e_name.text().strip() or None
        doc = self.doc

        def work(p):
            try:
                return mosaic.build(doc, chunks, product=product, areas=areas or None, resolution=res,
                                    background=background, name=name, progress=p)
            except ValueError as exc:  # MosaicError, TieError: said in the window's language
                return {"error": exc}

        self.working = True
        self._update_buttons()
        self._saves = doc.saves  # the save run_task makes after the build: the product then in the file
        started = self.window.run_task("Ortofoto e DEM uniti", work, self._built)
        if not started:
            self.working = False
            self._update_buttons()
        else:
            with contextlib.suppress(RuntimeError):
                self.window.runner.busy_changed.connect(self._busy_changed)
        return started

    def _busy_changed(self, busy: bool) -> None:
        if not busy:
            with contextlib.suppress(RuntimeError, TypeError):
                self.window.runner.busy_changed.disconnect(self._busy_changed)
            with contextlib.suppress(RuntimeError):
                self.working = False
                self._update_buttons()

    def _built(self, result) -> None:
        with contextlib.suppress(RuntimeError):
            self.working = False
            self._update_buttons()
        if isinstance(result, dict) and "error" in result:
            self.window._warn(tr("Ortofoto e DEM uniti: {0}", error_text(result["error"])))
            return
        self.window._mosaic_built(result, saved=self.doc.saves > getattr(self, "_saves", self.doc.saves))
        with contextlib.suppress(RuntimeError):
            self._draw_areas()

    def closeEvent(self, ev) -> None:  # noqa: N802 - Qt's name
        self._clear_areas()
        super().closeEvent(ev)

    def reject(self) -> None:  # Esc: no closeEvent
        self._clear_areas()
        super().reject()


__all__ = ["MosaicDialog", "MosaicBuiltStep", "error_text"]
