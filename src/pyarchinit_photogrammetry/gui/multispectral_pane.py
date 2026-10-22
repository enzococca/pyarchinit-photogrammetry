"""The multispectral workflow as a pane that stays open: the steps in order, the state of each
read from the chunk, the products shown in the raster view and sent to QGIS."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSpinBox,
                               QVBoxLayout, QWidget)

from ..engine.multispectral import INDICES
from .i18n import tr
from .icons import icon
from .image_views import INDEX_PALETTES

FALSE_COLOUR = ("NIR", "Red", "Green")
TRUE_COLOUR = ("Red", "Green", "Blue")


class MultispectralPane(QWidget):
    """Seven steps, each with its state and its button; the buttons of a step that cannot run yet
    are disabled, and the state says what is missing."""

    add_requested = Signal()
    align_requested = Signal()
    surface_requested = Signal()
    panel_requested = Signal()
    reflectance_requested = Signal()
    show_requested = Signal(str, object)  # "reflectance" with the three bands, "index" or "zones" with the name
    index_requested = Signal(str, str)  # name, formula ("" = the known index)
    zoning_requested = Signal(str, int, str)  # index, classes, method
    qgis_requested = Signal()
    palette_requested = Signal(str)  # the ramp of the indices shown

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        intro = QLabel(tr("Riprese di una camera multispettrale: i passi in ordine. Ogni passo dice cosa gli manca."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.states: dict[str, QLabel] = {}
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        def step(row: int, key: str, title: str, widgets: list[QWidget]) -> None:
            t = QLabel(f"<b>{row + 1}. {title}</b>")
            grid.addWidget(t, row * 2, 0, 1, 2)
            bar = QHBoxLayout()
            for w in widgets:
                bar.addWidget(w)
            bar.addStretch(1)
            grid.addLayout(bar, row * 2 + 1, 0)
            s = QLabel("")
            s.setWordWrap(True)
            s.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(s, row * 2 + 1, 1)
            self.states[key] = s

        self.b_add = QPushButton(icon("photos"), tr("Aggiungi riprese..."))
        self.b_add.setToolTip(tr("Una cartella con un file per banda di ogni scatto (MicaSense, DJI, Sequoia)"))
        step(0, "captures", tr("Riprese"), [self.b_add])
        self.b_align = QPushButton(icon("align"), tr("Allinea foto..."))
        step(1, "alignment", tr("Allineamento (la banda di riferimento come foto)"), [self.b_align])
        self.b_surface = QPushButton(icon("dem"), tr("Costruisci la superficie"))
        self.b_surface.setToolTip(tr("Mappe di profondità, nuvola densa e DEM in un passo solo, con le scelte predefinite"))
        step(2, "surface", tr("Superficie (mappe di profondità, nuvola densa, DEM)"), [self.b_surface])
        self.b_panel = QPushButton(icon("marker"), tr("Pannello..."))
        step(3, "panel", tr("Pannello di calibrazione (facoltativo)"), [self.b_panel])
        self.b_reflectance = QPushButton(icon("ortho"), tr("Costruisci..."))
        self.c_show = QComboBox()
        self.c_show.addItem(tr("Falso colore (NIR, Red, Green)"), "false")
        self.c_show.addItem(tr("Colori veri (Red, Green, Blue)"), "true")
        self.b_show_refl = QPushButton(icon("view"), tr("Mostra"))
        step(4, "reflectance", tr("Ortofoto di riflettanza (una banda per banda)"), [self.b_reflectance, self.c_show, self.b_show_refl])
        self.c_index = QComboBox()
        for name, (_formula, text) in INDICES.items():
            self.c_index.addItem(f"{name}: {text}", name)
        self.c_index.addItem(tr("Formula propria..."), "formula")
        self.b_index = QPushButton(icon("run"), tr("Calcola e mostra"))
        self.c_palette = QComboBox()
        self.c_palette.setToolTip(tr("Scala di colori degli indici"))
        for key, name in INDEX_PALETTES:
            self.c_palette.addItem(tr(name), key)
        step(5, "index", tr("Indice di vegetazione"), [self.c_index, self.b_index, self.c_palette])
        self.s_classes = QSpinBox()
        self.s_classes.setRange(2, 10)
        self.s_classes.setValue(3)
        self.s_classes.setPrefix(tr("classi "))
        self.c_method = QComboBox()
        self.c_method.addItem(tr("Quantili"), "quantiles")
        self.c_method.addItem(tr("Intervalli uguali"), "equal")
        self.c_method.addItem(tr("K-means (soglie naturali)"), "kmeans")
        self.b_zoning = QPushButton(icon("slice"), tr("Zonizza e mostra"))
        self.b_qgis = QPushButton(icon("qgis"), tr("Apri in QGIS"))
        self.b_qgis.setToolTip(tr("Progetto QGIS con riflettanza, indici, zone e i disegni"))
        step(6, "zoning", tr("Zonizzazione (poligoni in un GeoPackage: la mappa di prescrizione)"),
             [self.s_classes, self.c_method, self.b_zoning, self.b_qgis])
        lay.addStretch(1)
        self.b_add.clicked.connect(self.add_requested)
        self.b_align.clicked.connect(self.align_requested)
        self.b_surface.clicked.connect(self.surface_requested)
        self.b_panel.clicked.connect(self.panel_requested)
        self.b_reflectance.clicked.connect(self.reflectance_requested)
        self.b_show_refl.clicked.connect(self._show_reflectance)
        self.b_index.clicked.connect(self._index)
        self.b_zoning.clicked.connect(lambda: self.zoning_requested.emit(self.index_name(), int(self.s_classes.value()),
                                                                         str(self.c_method.currentData())))
        self.b_qgis.clicked.connect(self.qgis_requested)
        self.c_palette.currentIndexChanged.connect(lambda _i: self.palette_requested.emit(self.palette()))
        self.set_state(None)

    # --------------------------------------------------------------- choices
    def index_name(self) -> str:
        return str(self.c_index.currentData())

    def _index(self) -> None:
        name = self.index_name()
        self.index_requested.emit(name, "")

    def _show_reflectance(self) -> None:
        what = str(self.c_show.currentData())
        if what.startswith("band:"):  # one band in grey
            self.show_requested.emit("reflectance", (what[5:],))
        else:
            self.show_requested.emit("reflectance", FALSE_COLOUR if what == "false" else TRUE_COLOUR)

    def _offer_bands(self, bands) -> None:
        """The composites and every band on its own (grey), the choice kept."""
        current = self.c_show.currentData()
        self.c_show.blockSignals(True)
        self.c_show.clear()
        self.c_show.addItem(tr("Falso colore (NIR, Red, Green)"), "false")
        self.c_show.addItem(tr("Colori veri (Red, Green, Blue)"), "true")
        for b in bands:
            self.c_show.addItem(tr("Banda {0} (scala di grigi)", b), f"band:{b}")
        k = self.c_show.findData(current)
        self.c_show.setCurrentIndex(k if k >= 0 else 0)
        self.c_show.blockSignals(False)

    def palette(self) -> str:
        return str(self.c_palette.currentData())

    def palette_label(self) -> str:
        return self.c_palette.currentText()

    def set_palette(self, key: str) -> None:
        """The ramp chosen (from the settings), without announcing it."""
        k = self.c_palette.findData(key)
        if k >= 0:
            self.c_palette.blockSignals(True)
            self.c_palette.setCurrentIndex(k)
            self.c_palette.blockSignals(False)

    def _offer_indices(self, bands) -> None:
        """Only the indices the bands allow (no Thermal without a LWIR band), the choice kept."""
        from ..engine.multispectral import available_indices

        current = self.c_index.currentData()
        wanted = available_indices(bands) if bands else list(INDICES)
        self.c_index.blockSignals(True)
        self.c_index.clear()
        for name in wanted:
            self.c_index.addItem(f"{name}: {INDICES[name][1]}", name)
        self.c_index.addItem(tr("Formula propria..."), "formula")
        k = self.c_index.findData(current)
        self.c_index.setCurrentIndex(k if k >= 0 else 0)
        self.c_index.blockSignals(False)

    # --------------------------------------------------------------- state
    def set_state(self, chunk) -> None:
        """What every step has: read from the chunk (captures, alignment, DEM, panel, the
        reflectance orthomosaic, the indices and the zonings built)."""
        ms = (chunk.meta.get("multispectral") if chunk is not None else None) or {}
        captures = int(ms.get("captures") or 0)
        aligned = len(chunk.aligned_cameras) if chunk is not None else 0
        cams = len(chunk.cameras) if chunk is not None else 0
        has_dem = bool(chunk is not None and chunk.dem_path is not None and chunk.dem_path.exists())
        has_refl = bool(ms.get("reflectance") and chunk is not None and (chunk.data_dir / ms["reflectance"]).exists())
        indices = ms.get("indices") or {}
        zonings = ms.get("zoning") or {}
        if captures:
            self.states["captures"].setText(tr("{0} riprese, bande {1}, riferimento {2}", captures,
                                               ", ".join(ms.get("bands") or []), ms.get("reference") or ""))
        else:
            self.states["captures"].setText(tr("Nessuna ripresa: scegli la cartella dei file delle bande."))
        self.b_align.setEnabled(captures > 0)
        if captures and aligned:
            self.states["alignment"].setText(tr("{0} foto allineate su {1}", aligned, cams))
        else:
            self.states["alignment"].setText(tr("Da fare: allinea le foto (la banda di riferimento)."))
        self.b_surface.setEnabled(aligned > 0)
        if has_dem:
            self.states["surface"].setText(tr("DEM pronto: {0}", chunk.dem_path.name))
        else:
            self.states["surface"].setText(tr("Da fare dopo l'allineamento: l'ortofoto di riflettanza si appoggia sul DEM."))
        self.b_panel.setEnabled(captures > 0)
        panel = ms.get("panel")
        if panel:
            self.states["panel"].setText(tr("Fattori per banda: {0}", ", ".join(f"{b} {f:.3g}" for b, f in (panel.get("factors") or {}).items())))
        else:
            self.states["panel"].setText(tr("Senza pannello: vale il sensore di luce dei tag quando c'è, altrimenti numeri normalizzati."))
        self.b_reflectance.setEnabled(has_dem and aligned > 0)
        self.b_show_refl.setEnabled(has_refl)
        self.c_show.setEnabled(has_refl)
        self._offer_bands(ms.get("reflectance_bands") or [])
        self.c_palette.setEnabled(has_refl)
        if has_refl:
            self.states["reflectance"].setText(tr("Pronta: {0} ({1})", ms["reflectance"], ", ".join(ms.get("reflectance_bands") or [])))
        else:
            self.states["reflectance"].setText(tr("Da fare dopo il DEM: un GeoTIFF a virgola mobile con una banda per banda."))
        self.b_index.setEnabled(has_refl)
        self.c_index.setEnabled(has_refl)
        self._offer_indices(ms.get("reflectance_bands") or ms.get("bands") or [])
        if indices:
            self.states["index"].setText(tr("Calcolati: {0}", ", ".join(sorted(indices))))
        else:
            self.states["index"].setText(tr("Da fare dopo la riflettanza: NDVI e gli altri, o una formula sui nomi delle bande."))
        for w in (self.s_classes, self.c_method, self.b_zoning):
            w.setEnabled(bool(indices))
        self.b_qgis.setEnabled(has_refl or bool(indices))
        if zonings:
            parts = [tr("{0}: {1} poligoni, soglie {2}", name, z.get("polygons"), ", ".join(f"{b:.3g}" for b in z.get("breaks", [])))
                     for name, z in zonings.items()]
            self.states["zoning"].setText("; ".join(parts))
        else:
            self.states["zoning"].setText(tr("Da fare dopo un indice: le classi come poligoni con classe, intervallo, media e area."))
