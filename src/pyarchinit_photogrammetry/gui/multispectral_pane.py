"""The multispectral workflow as a pane that stays open: the steps in order, the state of each
read from the chunk, the products shown in the raster view and sent to QGIS."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QLabel, QPushButton, QScrollArea,
                               QSpinBox, QVBoxLayout, QWidget)

from ..engine.multispectral import INDICES
from ..engine.thermal import PALETTE_NAMES as THERMAL_PALETTES
from .flow_layout import FlowLayout
from .i18n import tr
from .icons import icon
from .image_views import INDEX_PALETTES

FALSE_COLOUR = ("NIR", "Red", "Green")
TRUE_COLOUR = ("Red", "Green", "Blue")


SPECTRAL_STEPS = ("panel", "reflectance", "index", "zoning")
THERMAL_STEPS = ("thermal_params", "thermal", "anomaly", "thermal_export")


class MultispectralPane(QWidget):
    """The steps of a multispectral camera (``mode="spectral"``: panel, reflectance, indices,
    zoning) or of a thermal one (``mode="thermal"``: the measure's parameters, the orthomosaic in
    degrees, the anomalies, the exports), after the steps they share (captures, alignment,
    surface); each step with its state and its buttons, the buttons of a step that cannot run yet
    disabled, the state saying what is missing. The window has one of each, in two tabs."""

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
    thermal_params_requested = Signal()
    sdk_requested = Signal()  # download DJI's Thermal SDK
    thermal_build_requested = Signal()
    thermal_show_requested = Signal(str, object)  # palette, (low, high) in degrees or None for the automatic range
    anomaly_requested = Signal(float)  # radius in metres
    thermal_export_requested = Signal(str)  # tif, tif16, csv, flir
    probe_requested = Signal()  # the Punto di misura tool on the raster view
    probe_export_requested = Signal()

    def __init__(self, parent=None, mode: str = "spectral"):
        super().__init__(parent)
        self.mode = mode
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
        intro = QLabel(tr("Riprese di una termocamera (R-JPEG DJI, JPEG radiometrici FLIR, TIFF LWIR o in gradi), sole o "
                          "con le foto visibili dello stesso volo: i passi in ordine. Ogni passo dice cosa gli manca.")
                       if mode == "thermal" else
                       tr("Riprese di una camera multispettrale: i passi in ordine. Ogni passo dice cosa gli manca."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.states: dict[str, QLabel] = {}
        self.rows: dict[str, tuple] = {}  # key: (title label, title, widgets, state label)
        self.order: list[str] = []

        def step(row: int, key: str, title: str, widgets: list[QWidget]) -> None:
            # one under the other - the title, the commands (on more lines when the dock is narrow), what
            # the step has - so that nothing ends up beyond the right edge of a narrow dock
            t = QLabel(f"<b>{row + 1}. {title}</b>")
            t.setWordWrap(True)
            lay.addWidget(t)
            holder = QWidget()
            bar = FlowLayout(holder)
            for w in widgets:
                if isinstance(w, QComboBox):  # the longest item does not set the width of the pane
                    w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                    w.setMinimumContentsLength(12)
                bar.addWidget(w)
            lay.addWidget(holder)
            s = QLabel("")
            s.setWordWrap(True)
            s.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            s.setContentsMargins(0, 0, 0, 8)
            lay.addWidget(s)
            self.states[key] = s
            self.rows[key] = (t, title, [holder, *widgets], s)
            self.order.append(key)

        self.b_add = QPushButton(icon("photos"), tr("Aggiungi riprese..."))
        self.b_add.setToolTip(tr("La cartella dei fotogrammi termici e, se ci sono, quella delle foto visibili dello stesso volo")
                              if mode == "thermal" else
                              tr("Una cartella con un file per banda di ogni scatto (MicaSense, DJI, Sequoia)"))
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
        self.c_method.addItem(tr("Interruzioni naturali (Jenks)"), "jenks")
        self.c_method.addItem(tr("K-means"), "kmeans")
        self.c_method.setToolTip(tr("Quantili: lo stesso numero di celle per classe; intervalli uguali: classi della stessa "
                                    "ampiezza; interruzioni naturali (Jenks): le soglie che rendono le classi il più omogenee "
                                    "possibile; K-means: la stessa idea per approssimazioni successive"))
        self.b_zoning = QPushButton(icon("slice"), tr("Zonizza e mostra"))
        self.b_qgis = QPushButton(icon("qgis"), tr("Apri in QGIS"))
        self.b_qgis.setToolTip(tr("Progetto QGIS con riflettanza, indici, zone e i disegni"))
        step(6, "zoning", tr("Zonizzazione (poligoni in un GeoPackage: la mappa di prescrizione)"),
             [self.s_classes, self.c_method, self.b_zoning, self.b_qgis])
        # the temperatures (a thermal band: DJI R-JPEGs, MicaSense LWIR, temperature TIFFs)
        self.b_tparams = QPushButton(icon("settings"), tr("Parametri..."))
        self.b_tparams.setToolTip(tr("Emissività, temperatura riflessa e ambiente, umidità, distanza: valgono per tutti i fotogrammi"))
        self.b_tsdk = QPushButton(icon("update"), tr("Scarica il DJI Thermal SDK..."))
        self.b_tsdk.setToolTip(tr("Il programma di DJI che legge le temperature delle R-JPEG: scaricato dal sito di DJI "
                                  "dopo aver accettato la sua licenza"))
        step(7, "thermal_params", tr("Parametri di misura della termica"), [self.b_tparams, self.b_tsdk])
        self.b_thermal = QPushButton(icon("ortho"), tr("Costruisci..."))
        self.b_thermal.setToolTip(tr("Ortofoto in gradi Celsius: un GeoTIFF a virgola mobile che resta radiometrico in ogni esportazione"))
        self.c_tpalette = QComboBox()
        for key, name in THERMAL_PALETTES:
            self.c_tpalette.addItem(tr(name), key)
        self.s_tlow = QDoubleSpinBox()
        self.s_thigh = QDoubleSpinBox()
        for w, tip in ((self.s_tlow, tr("Temperatura del colore più freddo (°C)")), (self.s_thigh, tr("Temperatura del colore più caldo (°C)"))):
            w.setRange(-100.0, 1000.0)
            w.setDecimals(1)
            w.setSingleStep(0.5)
            w.setSuffix(" °C")
            w.setToolTip(tip)
        self.b_tauto = QPushButton(tr("Auto"))
        self.b_tauto.setToolTip(tr("Dal 2° al 98° percentile dell'ortofoto"))
        self.b_tshow = QPushButton(icon("view"), tr("Mostra"))
        step(8, "thermal", tr("Ortofoto termica (°C)"), [self.b_thermal, self.c_tpalette, self.s_tlow, self.s_thigh,
                                                        self.b_tauto, self.b_tshow])
        self.s_radius = QDoubleSpinBox()
        self.s_radius.setRange(0.1, 500.0)
        self.s_radius.setDecimals(1)
        self.s_radius.setValue(2.0)
        self.s_radius.setPrefix(tr("raggio "))
        self.s_radius.setSuffix(" m")
        self.b_anomaly = QPushButton(icon("run"), tr("Calcola e mostra"))
        self.b_anomaly.setToolTip(tr("La temperatura meno quella dei dintorni: i muri e i fossati sepolti cambiano il suolo sopra di loro"))
        step(9, "anomaly", tr("Anomalie termiche"), [self.s_radius, self.b_anomaly])
        self.c_texport = QComboBox()
        self.c_texport.addItem(tr("GeoTIFF in gradi Celsius (32 bit)"), "tif")
        self.c_texport.addItem(tr("GeoTIFF in centikelvin (16 bit)"), "tif16")
        self.c_texport.addItem(tr("CSV (x, y, temperatura)"), "csv")
        self.c_texport.addItem(tr("JPEG radiometrici FLIR (tasselli con world file)"), "flir")
        self.b_texport = QPushButton(icon("export"), tr("Esporta..."))
        step(10, "thermal_export", tr("Esporta le temperature (restano in gradi)"), [self.c_texport, self.b_texport])
        self.b_tparams.clicked.connect(self.thermal_params_requested)
        self.b_tsdk.clicked.connect(self.sdk_requested)
        self.b_thermal.clicked.connect(self.thermal_build_requested)
        self.b_tshow.clicked.connect(lambda: self.thermal_show_requested.emit(self.thermal_palette(), self.thermal_range()))
        self.c_tpalette.currentIndexChanged.connect(
            lambda _i: self.thermal_show_requested.emit(self.thermal_palette(), self.thermal_range()) if self.b_tshow.isEnabled() else None)
        self.b_tauto.clicked.connect(self._auto_range)
        self.b_anomaly.clicked.connect(lambda: self.anomaly_requested.emit(float(self.s_radius.value())))
        self.b_texport.clicked.connect(lambda: self.thermal_export_requested.emit(str(self.c_texport.currentData())))
        # measuring points, in both tabs: the values of every product at a clicked point
        self.b_probe = QPushButton(icon("probe"), tr("Punto di misura"))
        self.b_probe.setToolTip(tr("Clic sull'ortofoto, termica o multispettrale: un punto con coordinate, quota del DEM, "
                                   "temperatura, bande e indici letti lì a piena risoluzione"))
        self.b_probe_export = QPushButton(icon("export"), tr("Esporta punti..."))
        self.b_probe_export.setToolTip(tr("GeoPackage, CSV o GeoJSON; in QGIS arrivano anche con gli altri layer"))
        step(11, "points", tr("Punti di misura"), [self.b_probe, self.b_probe_export])
        self.b_probe.clicked.connect(self.probe_requested)
        self.b_probe_export.clicked.connect(self.probe_export_requested)
        self._auto_limits: tuple[float, float] | None = None
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

    def thermal_palette(self) -> str:
        return str(self.c_tpalette.currentData())

    def thermal_range(self) -> tuple[float, float] | None:
        """The limits of the temperature colours, or None when they are the automatic ones."""
        lo, hi = float(self.s_tlow.value()), float(self.s_thigh.value())
        if self._auto_limits is not None and abs(lo - self._auto_limits[0]) < 0.05 and abs(hi - self._auto_limits[1]) < 0.05:
            return None
        return (lo, hi) if hi > lo else None

    def set_thermal_limits(self, lo: float, hi: float, automatic: bool = False) -> None:
        for w, v in ((self.s_tlow, lo), (self.s_thigh, hi)):
            w.blockSignals(True)
            w.setValue(round(float(v), 1))
            w.blockSignals(False)
        if automatic:
            self._auto_limits = (round(float(lo), 1), round(float(hi), 1))

    def _auto_range(self) -> None:
        if self._auto_limits is not None:
            self.set_thermal_limits(*self._auto_limits)
        self.thermal_show_requested.emit(self.thermal_palette(), None)

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
        bands = list(ms.get("bands") or [])
        thermal_band = "LWIR" in bands
        hidden = SPECTRAL_STEPS if self.mode == "thermal" else THERMAL_STEPS
        visible = [k for k in self.order if k not in hidden]
        for k in self.order:
            title_label, title, widgets, state = self.rows[k]
            on = k in visible
            for w in [title_label, state] + widgets:
                w.setVisible(on)
            if on:
                title_label.setText(f"<b>{visible.index(k) + 1}. {title}</b>")
        if self.mode == "thermal":
            self._thermal_state(chunk, thermal_band)
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

    def set_points(self, n: int) -> None:
        """How many measuring points the chunk has."""
        self.b_probe_export.setEnabled(n > 0)
        self.states["points"].setText(tr("{0} punti di misura (layer Punti di misura)", n) if n else
                                      tr("Nessun punto: attiva lo strumento e clicca sull'ortofoto o sul DEM."))

    def _thermal_state(self, chunk, thermal_band: bool = True) -> None:
        """The steps of the temperatures: the measure's parameters and who decodes the frames, the
        orthomosaic in degrees with its range, the anomalies, the exports."""
        from ..engine import thermal

        info = (chunk.meta.get("thermal") or {}) if chunk is not None else {}
        p = thermal.params_of(chunk)
        distance = tr("altezza di volo (1-25 m)") if p["distance"] is None else f"{p['distance']:g} m"
        have_sdk = thermal.sdk_dir() is not None
        decoder = (tr("DJI Thermal SDK in Docker") if sys.platform == "darwin" else tr("DJI Thermal SDK")) \
            if have_sdk else tr("senza DJI Thermal SDK: servono i file già in gradi")
        self.b_tsdk.setVisible(not have_sdk)
        text = tr("Emissività {0}, riflessa {1} °C, ambiente {2} °C, umidità {3} %, distanza {4}; {5}",
                  f"{p['emissivity']:g}", f"{p['reflected']:g}", f"{p['ambient']:g}", f"{p['humidity']:g}", distance, decoder)
        if p.get("scale") is not None and p.get("offset") is not None:
            text += tr("; TIFF di interi: {0} × valore {1} °C", f"{p['scale']:g}", f"{p['offset']:+g}")
        self.states["thermal_params"].setText(text)
        has_dem = bool(chunk is not None and chunk.dem_path is not None and chunk.dem_path.exists())
        path = thermal.thermal_path(chunk) if chunk is not None else None
        self.b_thermal.setEnabled(has_dem and thermal_band)
        for w in (self.b_tshow, self.c_tpalette, self.s_tlow, self.s_thigh, self.b_tauto, self.b_anomaly, self.s_radius,
                  self.b_texport, self.c_texport):
            w.setEnabled(path is not None)
        if path is not None:
            st = info.get("stats") or {}
            if st and self._auto_limits is None:
                self.set_thermal_limits(st.get("p2", 0.0), st.get("p98", 1.0), automatic=True)
            spread = (info.get("offsets") or {}).get("spread")
            text = tr("Pronta: {0} cm/px, {1} - {2} °C", f"{float(info.get('resolution', 0)) * 100:.1f}",
                      f"{st.get('p2', 0):.1f}", f"{st.get('p98', 0):.1f}")
            if spread is not None:
                text += tr("; scostamenti tra i fotogrammi tolti ({0} °C)", f"{spread:.2f}")
            self.states["thermal"].setText(text)
        elif not thermal_band:
            self.states["thermal"].setText(tr("Da fare dopo aver aggiunto i fotogrammi termici (passo 1) e costruito il DEM."))
        else:
            self.states["thermal"].setText(tr("Da fare dopo il DEM: le temperature di ogni fotogramma sull'ortofoto, in gradi."))
            self._auto_limits = None
        an = info.get("anomaly")
        self.states["anomaly"].setText(tr("Calcolate con raggio {0} m", f"{an['radius_m']:g}") if an else
                                       tr("Da fare dopo l'ortofoto termica."))
        self.states["thermal_export"].setText(tr("GeoTIFF in gradi o centikelvin con i parametri nei tag, CSV, o JPEG radiometrici FLIR: QGIS, i programmi per le termocamere FLIR e gli altri leggono le temperature."))
