"""The content of the processing report, collected once and drawn as a PDF (:mod:`io.pdf_report`)
or as an HTML page (:mod:`io.report`).

For every chunk: the survey (photos, alignment, tie points, reprojection error, ground resolution,
the camera positions over the orthomosaic), the calibration of the cameras, the references and
their errors (control and check points, scale bars, the GPS of the photos), the processing
parameters kept in ``chunk.meta``, the products with their numbers and figures (dense cloud and its
classes, model, DEM, orthomosaic), the multispectral products (bands, radiometric calibration,
registration, balance of the photos, reflectance in false colour, every index with its statistics,
the zones with their areas), the thermal products (decoder, the parameters of the measure, the
temperature orthomosaic in degrees with its statistics, the anomalies) and the measuring points.
Each part is there only when the chunk has it.

The report speaks Italian or English (``language``, :func:`language_of`); its texts are here
(:data:`TEXT`), not in the catalogue of the interface. A figure is a small image (at most
:data:`FIGURE_SIDE` pixels a side, JPEG or PNG) with what the renderers need to draw its colour bar,
legend, scale bar and north arrow.
"""
from __future__ import annotations

import io
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

LANGUAGES = ("it", "en")
FIGURE_SIDE = 1600  # pixels of the long side of a figure
STATS_CELLS = 16_000_000  # statistics of a raster from at most this many values (a regular sample beyond)
PREVIEW_CELLS = 4_000_000  # cells read for an image shown only (the orthomosaic, the camera positions)
MODEL_BYTES = 1_500_000_000  # a COLMAP model whose points weigh more is not read (its numbers from the files)

# ---------------------------------------------------------------------------------------- texts
TEXT: dict[str, tuple[str, str]] = {  # key: (Italian, English)
    # title page
    "report_title": ("Rapporto di elaborazione", "Processing report"),
    "project": ("Progetto", "Project"),
    "project_file": ("File del progetto", "Project file"),
    "unsaved": ("progetto non salvato", "unsaved project"),
    "date": ("Data", "Date"),
    "version": ("Versione del programma", "Program version"),
    "site": ("Sito", "Site"),
    "description": ("Descrizione", "Description"),
    "contents": ("Indice", "Contents"),
    "page_of": ("Pagina {0} di {1}", "Page {0} of {1}"),
    "chunks_title": ("Chunk del rapporto", "Chunks in the report"),
    "h_chunk": ("Chunk", "Chunk"),
    "h_photos": ("Foto", "Photos"),
    "h_aligned": ("Allineate", "Aligned"),
    "h_crs": ("Sistema di riferimento", "Reference system"),
    "h_products": ("Prodotti", "Products"),
    "chunk_title": ("Chunk: {0}", "Chunk: {0}"),
    "part_failed": ("Questa parte non e' stata letta: {0}", "This part could not be read: {0}"),
    # survey
    "survey": ("Dati del rilievo", "Survey data"),
    "photos": ("Foto", "Photos"),
    "photos_value": ("{0} ({1} attive)", "{0} ({1} enabled)"),
    "aligned": ("Foto allineate", "Aligned photos"),
    "aligned_value": ("{0} di {1}", "{0} of {1}"),
    "sensors": ("Camere (sensori)", "Cameras (sensors)"),
    "tie_points": ("Punti di legame", "Tie points"),
    "projections": ("Proiezioni", "Projections"),
    "track": ("Foto per punto di legame (media)", "Photos per tie point (mean)"),
    "reprojection": ("Errore di riproiezione medio", "Mean reprojection error"),
    "gsd": ("Risoluzione a terra (GSD)", "Ground resolution (GSD)"),
    "camera_distance": ("Distanza di ripresa (mediana)", "Camera distance (median)"),
    "coverage": ("Area coperta dal DEM", "Area covered by the DEM"),
    "crs": ("Sistema di riferimento", "Reference system"),
    "local": ("locale (senza georeferenziazione)", "local (not georeferenced)"),
    "scale": ("Scala del chunk", "Chunk scale"),
    "not_aligned": ("Le foto non sono ancora allineate.", "The photos are not aligned yet."),
    "camera_locations": ("Posizione delle camere e dei marker", "Camera and marker locations"),
    "legend_cameras": ("Camere", "Cameras"),
    "legend_control": ("Punti di controllo", "Control points"),
    "legend_check": ("Punti di verifica", "Check points"),
    "legend_markers": ("Marker senza riferimento", "Markers without reference"),
    # calibration
    "calibration": ("Calibrazione delle camere", "Camera calibration"),
    "sensor_title": ("Sensore: {0}", "Sensor: {0}"),
    "resolution_px": ("Risoluzione dell'immagine", "Image resolution"),
    "camera_model": ("Modello di camera", "Camera model"),
    "sensor_photos": ("Foto", "Photos"),
    "focal_px": ("Focale", "Focal length"),
    "pixel_size": ("Dimensione del pixel", "Pixel size"),
    "fixed": ("Calibrazione fissa", "Fixed calibration"),
    "h_parameter": ("Parametro", "Parameter"),
    "h_value": ("Valore", "Value"),
    "yes": ("si", "yes"),
    "no": ("no", "no"),
    # reference
    "reference": ("Riferimenti e accuratezza", "Reference and accuracy"),
    "georeferencing": ("Georeferenziazione", "Georeferencing"),
    "method_markers": ("punti di controllo", "control points"),
    "method_cameras": ("posizioni GPS delle camere", "camera GPS positions"),
    "method_markers+cameras": ("punti di controllo e GPS delle camere", "control points and camera GPS"),
    "method_scalebars": ("barre di scala (coordinate locali)", "scale bars (local coordinates)"),
    "plus_scalebars": (", con le barre di scala", ", with the scale bars"),
    "control_rmse": ("RMSE dei punti di controllo", "Control points RMSE"),
    "check_rmse": ("RMSE dei punti di verifica", "Check points RMSE"),
    "markers": ("Marker", "Markers"),
    "h_marker": ("Marker", "Marker"),
    "h_type": ("Tipo", "Type"),
    "h_projections": ("Proiezioni", "Projections"),
    "h_error_px": ("Errore (px)", "Error (px)"),
    "h_dx": ("dX (m)", "dX (m)"),
    "h_dy": ("dY (m)", "dY (m)"),
    "h_dz": ("dZ (m)", "dZ (m)"),
    "h_error_m": ("Errore (m)", "Error (m)"),
    "type_control": ("controllo", "control"),
    "type_check": ("verifica", "check"),
    "rmse_title": ("Errori quadratici medi", "Root mean square errors"),
    "h_points": ("Punti", "Points"),
    "h_count": ("Numero", "Count"),
    "h_x_m": ("X (m)", "X (m)"),
    "h_y_m": ("Y (m)", "Y (m)"),
    "h_z_m": ("Z (m)", "Z (m)"),
    "h_xy_m": ("XY (m)", "XY (m)"),
    "h_total_m": ("Totale (m)", "Total (m)"),
    "scalebars": ("Barre di scala", "Scale bars"),
    "h_bar": ("Barra", "Bar"),
    "h_marker_a": ("Marker A", "Marker A"),
    "h_marker_b": ("Marker B", "Marker B"),
    "h_distance": ("Distanza (m)", "Distance (m)"),
    "h_estimated": ("Stimata (m)", "Estimated (m)"),
    "camera_gps": ("Posizioni GPS delle camere", "Camera GPS positions"),
    "cameras_with_gps": ("Camere con una posizione di riferimento", "Cameras with a reference position"),
    "gps_rmse": ("Errore delle camere (RMSE X / Y / Z / totale)", "Camera error (RMSE X / Y / Z / total)"),
    "no_reference": ("Nessun riferimento: il rilievo e' in coordinate locali.",
                     "No reference: the survey is in local coordinates."),
    # parameters
    "parameters": ("Parametri di elaborazione", "Processing parameters"),
    "h_step": ("Passo", "Step"),
    "step_sfm": ("Allineamento delle foto", "Photo alignment"),
    "step_optimize": ("Ottimizzazione delle camere", "Camera optimisation"),
    "step_dense": ("Mappe di profondita'", "Depth maps"),
    "step_point_cloud": ("Nuvola densa", "Dense cloud"),
    "step_model": ("Modello", "Model"),
    "step_texture": ("Texture", "Texture"),
    "step_dem": ("DEM", "DEM"),
    "step_orthomosaic": ("Ortofoto", "Orthomosaic"),
    "p_features": ("Estrazione dei punti", "Feature extraction"),
    "p_matcher": ("Punti", "Key points"),
    "p_quality": ("Qualita'", "Quality"),
    "p_keypoint_limit": ("Limite dei punti chiave", "Key point limit"),
    "p_tiepoint_limit": ("Limite dei punti di legame", "Tie point limit"),
    "p_pair_selection": ("Selezione delle coppie", "Pair selection"),
    "p_generic": ("Preselezione generica", "Generic preselection"),
    "p_reference": ("Preselezione con i riferimenti", "Reference preselection"),
    "p_neighbours": ("Foto vicine", "Neighbours"),
    "p_pairs": ("Coppie abbinate", "Pairs matched"),
    "p_guided_matching": ("Abbinamento guidato", "Guided matching"),
    "p_apply_masks": ("Maschere", "Masks"),
    "p_stationary_removed": ("Corrispondenze ferme tolte", "Stationary matches removed"),
    "p_mapper": ("Metodo", "Mapper"),
    "p_global_fallback": ("Ripiego sull'incrementale", "Fallback to incremental"),
    "p_masked_tie_points": ("Punti di legame mascherati", "Masked tie points"),
    "p_gpu": ("GPU", "GPU"),
    "p_gpu_pairs": ("Coppie sulla GPU", "Pairs on the GPU"),
    "p_cpu_pairs": ("Coppie sulla CPU", "Pairs on the CPU"),
    "p_reprojection_error": ("Errore di riproiezione (px)", "Reprojection error (px)"),
    "p_engine": ("Motore", "Engine"),
    "p_iterations": ("Iterazioni", "Iterations"),
    "p_converged": ("Convergenza", "Converged"),
    "p_control_points": ("Punti di controllo", "Control points"),
    "p_scale_bars": ("Barre di scala", "Scale bars"),
    "p_steps": ("Passi verso i riferimenti", "Steps towards the references"),
    "p_backend": ("Motore", "Engine"),
    "p_filter": ("Filtro", "Filter"),
    "p_fusion": ("Fusione", "Fusion"),
    "p_source": ("Sorgente", "Source"),
    "p_surface": ("Superficie", "Surface"),
    "p_classes": ("Classi", "Classes"),
    "p_mapping": ("Mappatura", "Mapping"),
    "p_texture_size": ("Dimensione richiesta (px)", "Size asked (px)"),
    "p_size": ("Dimensione (px)", "Size (px)"),
    "p_pages": ("Pagine", "Pages"),
    "p_colour_correction": ("Correzione del colore", "Colour correction"),
    "p_skipped_photos": ("Foto saltate", "Photos skipped"),
    "p_resolution": ("Risoluzione (m)", "Resolution (m)"),
    "p_spikes_removed": ("Picchi tolti", "Spikes removed"),
    "p_blending": ("Fusione", "Blending"),
    "p_blend_sigma": ("Sigma della fusione", "Blending sigma"),
    "p_ghosting": ("Filtro degli oggetti in movimento", "Ghosting filter"),
    "p_fill_holes": ("Riempimento dei buchi", "Hole filling"),
    "p_hidden_classes": ("Classi nascoste", "Hidden classes"),
    "p_max_angle": ("Angolo massimo (gradi)", "Maximum angle (degrees)"),
    "p_max_cameras": ("Foto per cella", "Photos per cell"),
    "p_patches": ("Patch", "Patches"),
    "p_point_cloud_source": ("Origine", "Origin"),
    # products
    "dense_cloud": ("Nuvola densa", "Dense cloud"),
    "points": ("Punti", "Points"),
    "file": ("File", "File"),
    "file_size": ("Dimensione del file", "File size"),
    "classes": ("Classi dei punti", "Point classes"),
    "h_code": ("Codice", "Code"),
    "h_class": ("Classe", "Class"),
    "h_percent": ("%", "%"),
    "model_title": ("Modello 3D", "3D model"),
    "faces": ("Facce", "Faces"),
    "vertices": ("Vertici", "Vertices"),
    "texture": ("Texture", "Texture"),
    "texture_value": ("{0} pagina/e da {1} x {2} px", "{0} page(s) of {1} x {2} px"),
    "dem_title": ("Modello digitale di elevazione (DEM)", "Digital elevation model (DEM)"),
    "raster_size": ("Dimensione", "Size"),
    "raster_size_value": ("{0} x {1} celle", "{0} x {1} cells"),
    "resolution": ("Risoluzione", "Resolution"),
    "extent": ("Estensione", "Extent"),
    "elevations": ("Quote (minima - massima)", "Elevations (minimum - maximum)"),
    "mean_elevation": ("Quota media", "Mean elevation"),
    "area_data": ("Area con dati", "Area with data"),
    "elevation_m": ("Quota (m)", "Elevation (m)"),
    "dem_caption": ("Tinte ipsometriche sull'ombreggiatura del rilievo", "Hypsometric tints over the hillshade"),
    "ortho_title": ("Ortofoto", "Orthomosaic"),
    "sample_note": ("statistiche su un campione regolare di {0} valori", "statistics on a regular sample of {0} values"),
    # statistics
    "h_band": ("Banda", "Band"),
    "h_min": ("Minimo", "Minimum"),
    "h_max": ("Massimo", "Maximum"),
    "h_mean": ("Media", "Mean"),
    "h_median": ("Mediana", "Median"),
    "h_p2": ("P2", "P2"),
    "h_p98": ("P98", "P98"),
    "h_index": ("Indice", "Index"),
    "h_formula": ("Formula", "Formula"),
    # multispectral
    "ms_title": ("Multispettrale", "Multispectral"),
    "captures": ("Riprese", "Captures"),
    "bands": ("Bande", "Bands"),
    "reference_band": ("Banda di riferimento", "Reference band"),
    "camera": ("Camera", "Camera"),
    "radiometric_model": ("Modello radiometrico", "Radiometric model"),
    "model_micasense": ("MicaSense (calibrazione radiometrica dei metadati)", "MicaSense (radiometric calibration of the tags)"),
    "model_dji": ("DJI (guadagno, esposizione, vignettatura dei metadati)", "DJI (gain, exposure, vignetting of the tags)"),
    "model_normalised": ("numeri digitali normalizzati per esposizione e guadagno",
                         "digital numbers normalised by exposure and gain"),
    "radiometric_calibration": ("Calibrazione radiometrica", "Radiometric calibration"),
    "cal_panel": ("pannello di calibrazione (ripresa {0}, riflettanza {1})", "calibration panel (capture {0}, reflectance {1})"),
    "cal_panel_dls": ("pannello di calibrazione (ripresa {0}, riflettanza {1}) e sensore solare (DLS) che segue la luce "
                      "durante il volo", "calibration panel (capture {0}, reflectance {1}) and the sun sensor (DLS) "
                      "following the light during the flight"),
    "cal_dls": ("irradianza del sensore solare (DLS) nei metadati", "irradiance of the sun sensor (DLS) in the tags"),
    "cal_none": ("nessun pannello e nessun DLS: gli indici valgono, i valori assoluti no",
                 "no panel and no DLS: the indices hold, the absolute values do not"),
    "balance": ("Pareggiamento delle foto", "Balance of the photos"),
    "balance_planes": ("guadagno e piano per foto, termine radiale", "gain and plane per photo, radial term"),
    "balance_gains": ("guadagno per foto", "gain per photo"),
    "balance_off": ("non applicato", "not applied"),
    "h_panel_factor": ("Fattore del pannello", "Panel factor"),
    "h_registration": ("Registrazione (correlazione)", "Registration (correlation)"),
    "h_registered_on": ("Registrata su", "Registered on"),
    "h_balance_spread": ("Dispersione dei guadagni (%)", "Spread of the gains (%)"),
    "h_balance_residual": ("Residuo (%)", "Residual (%)"),
    "reference_word": ("riferimento", "reference"),
    "reflectance": ("Ortofoto di riflettanza", "Reflectance orthomosaic"),
    "false_colour": ("Falso colore: R = {0}, G = {1}, B = {2}", "False colour: R = {0}, G = {1}, B = {2}"),
    "single_band": ("Banda {0} in scala di grigi", "Band {0} in grey levels"),
    "reflectance_stats": ("Riflettanza delle bande", "Reflectance of the bands"),
    "indices": ("Indici", "Indices"),
    "index_title": ("Indice {0}", "Index {0}"),
    "index_caption": ("{0}: {1}", "{0}: {1}"),
    "zoning_title": ("Zonizzazione di {0}", "Zoning of {0}"),
    "zoning_value": ("{0} zone per {1}, {2} poligoni", "{0} zones by {1}, {2} polygons"),
    "method_quantiles": ("quantili", "quantiles"),
    "method_equal": ("intervalli uguali", "equal intervals"),
    "method_kmeans": ("k-means", "k-means"),
    "h_zone": ("Zona", "Zone"),
    "h_from": ("Da", "From"),
    "h_to": ("A", "To"),
    "h_area_m2": ("Area (m²)", "Area (m²)"),
    "h_area_share": ("% dell'area", "% of the area"),
    "zone_label": ("Zona {0}: {1} - {2}", "Zone {0}: {1} - {2}"),
    # thermal
    "thermal_title": ("Termico", "Thermal"),
    "thermal_frames": ("Fotogrammi termici", "Thermal frames"),
    "decoder": ("Decodifica", "Decoder"),
    "decoder_dji": ("DJI Thermal SDK (R-JPEG)", "DJI Thermal SDK (R-JPEG)"),
    "decoder_flir": ("JPEG radiometrici FLIR", "FLIR radiometric JPEGs"),
    "decoder_tiff": ("TIFF di temperature", "TIFFs of temperatures"),
    "decoder_micasense": ("MicaSense LWIR (centikelvin)", "MicaSense LWIR (centikelvin)"),
    "decoder_counts": ("TIFF di interi (valore x scala + offset)", "TIFFs of integers (value x scale + offset)"),
    "frames_decoded": ("Fotogrammi decodificati", "Frames decoded"),
    "frame_size": ("Dimensione del fotogramma", "Frame size"),
    "frame_size_value": ("{0} x {1} px", "{0} x {1} px"),
    "thermal_geometry": ("Geometria", "Geometry"),
    "thermal_alone": ("i fotogrammi termici allineati da soli", "the thermal frames aligned by themselves"),
    "thermal_registered": ("fotogrammi registrati sulle foto visibili (copertura {0}, correlazione {1}, {2} affinati)",
                           "frames registered on the visible photos (cover {0}, correlation {1}, {2} refined)"),
    "measure_parameters": ("Parametri della misura", "Parameters of the measure"),
    "emissivity": ("Emissivita'", "Emissivity"),
    "reflected": ("Temperatura riflessa", "Reflected temperature"),
    "ambient": ("Temperatura atmosferica", "Atmospheric temperature"),
    "humidity": ("Umidita' relativa", "Relative humidity"),
    "distance": ("Distanza", "Distance"),
    "flight_height": ("altezza di volo di ogni fotogramma", "flight height of every frame"),
    "raw_transform": ("Gradi dai valori interi", "Degrees from the integer values"),
    "offsets": ("Scostamenti tra i fotogrammi (deviazione standard)", "Offsets between the frames (standard deviation)"),
    "offsets_off": ("non pareggiati", "not balanced"),
    "thermal_ortho": ("Ortofoto termica", "Thermal orthomosaic"),
    "temperature_c": ("Temperatura (°C)", "Temperature (°C)"),
    "thermal_caption": ("Temperature in gradi Celsius, palette {0}", "Temperatures in degrees Celsius, palette {0}"),
    "temperature_stats": ("Statistiche delle temperature (°C)", "Temperature statistics (°C)"),
    "anomalies": ("Anomalie termiche", "Thermal anomalies"),
    "anomaly_c": ("Scarto (°C)", "Deviation (°C)"),
    "anomaly_caption": ("Temperatura meno quella dei dintorni entro {0} m", "Temperature minus that of its surroundings within {0} m"),
    # measuring points
    "points_title": ("Punti di misura", "Measurement points"),
    "h_point": ("Punto", "Point"),
}

ASPRS_IT = {0: "Mai classificato", 1: "Non classificato", 2: "Terreno", 3: "Vegetazione bassa", 4: "Vegetazione media",
            5: "Vegetazione alta", 6: "Edificio", 7: "Punto basso (rumore)", 9: "Acqua", 17: "Impalcato di ponte",
            18: "Rumore alto"}

# colour ramps: (position 0-1, (R, G, B)), the same as the views of the interface
DEM_STOPS = [(0.0, (40, 90, 160)), (0.25, (60, 160, 120)), (0.5, (230, 220, 120)), (0.75, (200, 130, 60)),
             (1.0, (250, 250, 250))]
INDEX_STOPS = {"rdylgn": [(0.0, (165, 0, 38)), (0.25, (244, 109, 67)), (0.5, (254, 224, 139)), (0.75, (102, 189, 99)),
                          (1.0, (0, 104, 55))],
               "grey": [(0.0, (0, 0, 0)), (1.0, (255, 255, 255))]}
DIVERGING_STOPS = [(0.0, (33, 102, 172)), (0.25, (103, 169, 207)), (0.5, (247, 247, 247)), (0.75, (239, 138, 98)),
                   (1.0, (178, 24, 43))]
ZONE_COLORS = [(214, 96, 77), (247, 203, 120), (178, 223, 138), (26, 152, 80), (0, 109, 44), (69, 117, 180),
               (152, 78, 163), (255, 127, 0), (166, 86, 40), (120, 120, 120)]  # the zones, 1 = the lowest
CAMERA_COLOUR, CONTROL_COLOUR, CHECK_COLOUR, MARKER_COLOUR = (31, 119, 180), (214, 39, 40), (255, 127, 14), (120, 120, 120)


def language_of(language: str | None = None) -> str:
    """``it`` or ``en``: the one given, else the language of the interface (its choice in *Vista >
    Lingua*, ``PPG_LANG``, the system), else ``PPG_LANG`` and the language of the system."""
    if language:
        code = str(language).strip().lower()[:2]
        if code not in LANGUAGES:
            raise ValueError(f"language: one of {', '.join(LANGUAGES)}, not {language!r}")
        return code
    try:
        from ..gui.i18n import current

        code = current()
        if code in LANGUAGES:
            return code
    except Exception:  # noqa: BLE001 - the interface is not installed
        pass
    code = os.environ.get("PPG_LANG", "").strip().lower()[:2]
    if code in LANGUAGES:
        return code
    import locale

    try:
        loc = locale.getlocale()[0] or os.environ.get("LANG", "")
    except ValueError:
        loc = os.environ.get("LANG", "")
    return "it" if str(loc).lower().startswith("it") else "en"


class Words:
    """The texts and the numbers of a report in one language."""

    def __init__(self, language: str | None = None):
        self.language = language_of(language)
        self._i = 0 if self.language == "it" else 1

    def __call__(self, key: str, *args) -> str:
        pair = TEXT.get(key)
        text = key if pair is None else pair[self._i]
        return text.format(*args) if args else text

    def number(self, value, decimals: int = 2, group: bool = True) -> str:
        """``1,234.56`` in English, ``1.234,56`` in Italian; ``-`` for nothing."""
        if value is None:
            return "-"
        try:
            value = float(value)
        except (TypeError, ValueError):
            return str(value)
        if not math.isfinite(value):
            return "-"
        if round(value, decimals) == 0:
            value = 0.0  # never "-0.00"
        text = f"{value:,.{decimals}f}" if group else f"{value:.{decimals}f}"
        if self.language == "it":
            text = text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
        return text

    def significant(self, value, digits: int = 6) -> str:
        """``value`` with ``digits`` significant digits (calibration parameters, scales)."""
        if value is None:
            return "-"
        text = f"{float(value):.{digits}g}"
        return text.replace(".", ",") if self.language == "it" else text

    def integer(self, value) -> str:
        return "-" if value is None else self.number(int(value), 0)

    def coordinate(self, value, decimals: int = 3) -> str:
        """Coordinates without thousands separators (as surveyors write them)."""
        return self.number(value, decimals, group=False)

    def length(self, metres) -> str:
        """A length in millimetres below a metre, else in metres."""
        if metres is None or not math.isfinite(float(metres)):
            return "-"
        m = float(metres)
        return f"{self.number(m * 1000, 1)} mm" if abs(m) < 1 else f"{self.number(m, 3)} m"

    def resolution(self, metres) -> str:
        """The size of a cell or of a pixel on the ground: mm/px, cm/px or m/px."""
        if metres is None or not math.isfinite(float(metres)) or metres <= 0:
            return "-"
        m = float(metres)
        if m < 0.01:
            return f"{self.number(m * 1000, 2)} mm/px"
        if m < 1:
            return f"{self.number(m * 100, 2)} cm/px"
        return f"{self.number(m, 2)} m/px"

    def area(self, m2) -> str:
        if m2 is None or not math.isfinite(float(m2)):
            return "-"
        m2 = float(m2)
        text = f"{self.number(m2, 2 if m2 < 100 else 0)} m²"
        return text + (f" ({self.number(m2 / 10000, 2)} ha)" if m2 >= 10000 else "")

    def degrees(self, value, decimals: int = 2) -> str:
        return "-" if value is None else f"{self.number(value, decimals)} °C"

    def yes_no(self, flag) -> str:
        return self("yes") if flag else self("no")

    def date(self, when: datetime) -> str:
        return when.strftime("%d/%m/%Y %H:%M") if self.language == "it" else when.strftime("%Y-%m-%d %H:%M")

    def megabytes(self, size: int) -> str:
        return f"{self.number(size / 1e6, 1)} MB"


# ---------------------------------------------------------------------------------------- the blocks
@dataclass
class KeyValues:
    """Pairs of a label and a value, one per line."""

    rows: list[tuple[str, str]]


@dataclass
class Table:
    """A table of texts already formatted; ``align``: ``L``, ``R`` or ``C`` per column (default:
    right for the columns of numbers)."""

    headers: list[str]
    rows: list[list[str]]
    align: list[str] | None = None

    def alignment(self) -> list[str]:
        if self.align:
            return list(self.align)
        out = []
        for c in range(len(self.headers)):
            cells = [str(r[c]) for r in self.rows if c < len(r) and str(r[c]) not in ("", "-")]
            numeric = bool(cells) and all(_looks_numeric(v) for v in cells)
            out.append("R" if numeric else "L")
        return out


@dataclass
class Paragraph:
    text: str
    note: bool = False  # smaller, grey


@dataclass
class ColourBar:
    """The ramp of a figure between ``low`` and ``high`` (``stops``: positions 0-1 and colours)."""

    stops: list[tuple[float, tuple[int, int, int]]]
    low: float
    high: float
    label: str
    decimals: int = 2

    def colours(self, n: int = 256) -> np.ndarray:
        """``(n, 3)`` uint8: the ramp from ``low`` to ``high``."""
        return ramp(np.linspace(0.0, 1.0, n), self.stops)

    def ticks(self, count: int = 5) -> list[float]:
        return nice_ticks(self.low, self.high, count)

    def tick_decimals(self, count: int = 5) -> int:
        """The decimals the ticks need (those of their step: 0.25 -> 2, 5 -> 0)."""
        t = self.ticks(count)
        if len(t) < 2:
            return self.decimals
        step = abs(t[1] - t[0])
        d = 0
        while d < 6 and abs(step * 10 ** d - round(step * 10 ** d)) > 1e-6 * max(1.0, step * 10 ** d):
            d += 1
        return d


@dataclass
class Legend:
    items: list[tuple[tuple[int, int, int], str]]


@dataclass
class Figure:
    data: bytes  # the image, encoded
    mime: str  # image/jpeg or image/png
    width: int
    height: int
    caption: str = ""
    colour_bar: ColourBar | None = None
    legend: Legend | None = None
    metres_per_pixel: float | None = None  # the ground size of a pixel of the image: a scale bar
    north: bool = False


@dataclass
class Section:
    title: str
    level: int = 1  # 0: a chunk, 1: a section, 2: a part of a section
    blocks: list = field(default_factory=list)


@dataclass
class Report:
    words: Words
    title: str
    project: str
    project_file: str
    created: datetime
    version: str
    description: str
    info: KeyValues
    chunks: Table
    sections: list[Section]

    @property
    def language(self) -> str:
        return self.words.language


def _looks_numeric(text: str) -> bool:
    t = text.replace(".", "").replace(",", "").replace(" ", "").replace("%", "").replace("−", "-").lstrip("+-")
    return t.isdigit()


# ---------------------------------------------------------------------------------------- colours
def ramp(t: np.ndarray, stops) -> np.ndarray:
    """Values 0-1 on a colour ramp: ``(..., 3)`` uint8."""
    pos = np.array([s[0] for s in stops], float)
    t = np.clip(np.asarray(t, float), 0.0, 1.0)
    return np.stack([np.interp(t, pos, [s[1][k] for s in stops]) for k in range(3)], -1).astype(np.uint8)


def palette_stops(name: str) -> list:
    """The stops of an index palette of the interface (``rdylgn``, ``grey``, ``viridis``, ``turbo``)."""
    if name in INDEX_STOPS:
        return INDEX_STOPS[name]
    if name in ("viridis", "turbo"):
        import cv2

        cmap = cv2.COLORMAP_VIRIDIS if name == "viridis" else cv2.COLORMAP_TURBO
        line = cv2.applyColorMap(np.linspace(0, 255, 17).astype(np.uint8)[:, None], cmap)[:, 0, ::-1]
        return [(k / 16, tuple(int(v) for v in line[k])) for k in range(17)]
    return INDEX_STOPS["rdylgn"]


def thermal_stops(name: str) -> list:
    from ..engine.thermal import PALETTES

    return [(float(p), tuple(int(v) for v in c)) for p, c in PALETTES.get(name, PALETTES["iron"])]


def nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """Round values between ``low`` and ``high`` for the ticks of a colour bar."""
    if not (math.isfinite(low) and math.isfinite(high)) or high <= low:
        return [low]
    raw = (high - low) / max(count - 1, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(low / step) * step
    out, v = [], first
    while v <= high + step * 1e-9:
        out.append(round(v, 12))
        v += step
    return out or [low, high]


def nice_length(target: float) -> float:
    """1, 2 or 5 times a power of ten, not above ``target`` (the length of a scale bar)."""
    if target <= 0 or not math.isfinite(target):
        return 0.0
    mag = 10 ** math.floor(math.log10(target))
    return max(m * mag for m in (1, 2, 5) if m * mag <= target)


def _encode(rgb: np.ndarray, flat: bool = False) -> tuple[bytes, str]:
    """An RGB array as JPEG (quality 85), or PNG for images of flat colours (zones)."""
    from PIL import Image

    img = Image.fromarray(np.ascontiguousarray(rgb[..., :3]).astype(np.uint8), "RGB")
    buf = io.BytesIO()
    if flat:
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"
    img.save(buf, "JPEG", quality=85, optimize=True)
    return buf.getvalue(), "image/jpeg"


def _figure(rgb: np.ndarray, flat: bool = False, **kwargs) -> Figure:
    data, mime = _encode(rgb, flat)
    return Figure(data=data, mime=mime, width=int(rgb.shape[1]), height=int(rgb.shape[0]), **kwargs)


# ---------------------------------------------------------------------------------------- rasters
@dataclass
class Raster:
    """A raster read for a report: its header and its values at a reduced size."""

    path: Path
    width: int
    height: int
    count: int
    res: float  # ground size of a cell of the file
    bounds: tuple[float, float, float, float]  # left, bottom, right, top
    descriptions: list[str]
    tags: dict
    data: np.ndarray  # (bands, rows, columns) float32, NaN where nothing
    step: float  # cells of the file per cell of data

    @property
    def cell(self) -> float:
        """Ground size of a cell of ``data``."""
        return self.res * self.step

    def values(self, band: int = 0) -> np.ndarray:
        v = self.data[band]
        return v[np.isfinite(v)]

    @property
    def sampled(self) -> bool:
        return self.step > 1.0001


def read_raster(path, bands=None, max_cells: int = STATS_CELLS) -> Raster:
    """``bands`` (1-based, all by default) of a GeoTIFF at a size of at most ``max_cells`` values
    (the overviews of the file serve when it has them), as float32 with NaN where it has no
    value (nodata, the alpha band of an RGBA image)."""
    import rasterio
    from rasterio.enums import Resampling

    with rasterio.open(path) as src:
        idx = list(bands) if bands else list(range(1, src.count + 1))
        f = max(1.0, math.sqrt(src.width * src.height * len(idx) / float(max_cells)))
        h, w = max(1, int(round(src.height / f))), max(1, int(round(src.width / f)))
        arr = src.read(idx, out_shape=(len(idx), h, w), resampling=Resampling.nearest, masked=True)
        data = np.ma.asarray(arr).astype(np.float32).filled(np.nan)
        if src.nodata is not None and not (isinstance(src.nodata, float) and math.isnan(src.nodata)):
            data[data == np.float32(src.nodata)] = np.nan
        b = src.bounds
        return Raster(path=Path(path), width=src.width, height=src.height, count=src.count, res=abs(src.res[0]),
                      bounds=(b.left, b.bottom, b.right, b.top),
                      descriptions=[d or f"b{i + 1}" for i, d in enumerate(src.descriptions)], tags=src.tags(),
                      data=data, step=src.width / float(w))


def stats(values: np.ndarray) -> dict:
    """min, max, mean, median, p2, p98 of finite values (empty: nothing)."""
    v = np.asarray(values, np.float64)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return {}
    p2, med, p98 = np.percentile(v, [2, 50, 98])
    return {"min": float(v.min()), "max": float(v.max()), "mean": float(v.mean()), "median": float(med),
            "p2": float(p2), "p98": float(p98), "n": int(v.size)}


def _block_mean(a: np.ndarray, k: int) -> np.ndarray:
    """``a`` reduced ``k`` times by the mean of its blocks, NaN ignored."""
    if k <= 1:
        return a
    h, w = a.shape
    H, W = -(-h // k) * k, -(-w // k) * k
    pad = np.full((H, W), np.nan, np.float32)
    pad[:h, :w] = a
    blocks = pad.reshape(H // k, k, W // k, k)
    with np.errstate(invalid="ignore"):
        n = np.isfinite(blocks).sum(axis=(1, 3))
        s = np.nansum(blocks, axis=(1, 3))
    return np.where(n > 0, s / np.maximum(n, 1), np.nan).astype(np.float32)


def _reduce(a: np.ndarray, side: int = FIGURE_SIDE, mean: bool = True) -> tuple[np.ndarray, int]:
    """A band of values brought to at most ``side`` cells a side (block mean, or the nearest)."""
    k = max(1, int(math.ceil(max(a.shape) / float(side))))
    if k == 1:
        return a, 1
    return (_block_mean(a, k) if mean else a[::k, ::k]), k


def dem_rgb(z: np.ndarray, cell: float, limits: tuple[float, float]) -> np.ndarray:
    """Hypsometric tints times a hillshade (sun at azimuth 315, 45 degrees high), white where no
    value: the DEM as the view of the interface shows it."""
    valid = np.isfinite(z)
    lo, hi = limits
    t = np.clip((np.where(valid, z, lo) - lo) / max(hi - lo, 1e-9), 0, 1)
    col = ramp(t, DEM_STOPS).astype(np.float32)
    zf = np.where(valid, z, np.nanmean(z[valid]) if valid.any() else 0.0)
    gy, gx = np.gradient(zf, max(cell, 1e-9))
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(gy, -gx)
    az, alt = np.radians(360.0 - 315.0 + 90.0), np.radians(45.0)
    shade = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    shade = np.clip(shade, 0, 1) * 0.7 + 0.3
    rgb = np.clip(col * shade[..., None], 0, 255).astype(np.uint8)
    rgb[~valid] = 255
    return rgb


def _coloured(values: np.ndarray, stops, low: float, high: float) -> np.ndarray:
    valid = np.isfinite(values)
    rgb = ramp((np.where(valid, values, low) - low) / max(high - low, 1e-12), stops)
    rgb[~valid] = 255
    return rgb


def _decimals_for(low: float, high: float) -> int:
    span = abs(high - low)
    if span >= 100:
        return 0
    if span >= 10:
        return 1
    if span >= 1:
        return 2
    return 3


# ---------------------------------------------------------------------------------------- helpers
def _ply_counts(path) -> dict:
    """Elements of a PLY from its header: ``{"vertex": n, "face": m}``."""
    from .mesh_ply import read_header

    try:
        with open(path, "rb") as f:
            _, elements = read_header(f)
    except (OSError, ValueError, KeyError):
        return {}
    return {e["name"]: int(e["count"]) for e in elements}


def _point_count(path) -> int | None:
    path = Path(path)
    if path.suffix.lower() in (".las", ".laz"):
        try:
            import laspy

            with laspy.open(str(path)) as f:
                return int(f.header.point_count)
        except Exception:  # noqa: BLE001
            return None
    return _ply_counts(path).get("vertex")


def _relative(path: Path, chunk) -> str:
    try:
        return str(Path(path).resolve().relative_to(chunk.document.path.parent.resolve()))
    except (ValueError, AttributeError, OSError):
        return str(path)


def _file_rows(w: Words, chunk, path: Path) -> list[tuple[str, str]]:
    rows = [(w("file"), _relative(path, chunk))]
    try:
        rows.append((w("file_size"), w.megabytes(Path(path).stat().st_size)))
    except OSError:
        pass
    return rows


def _crs_name(crs: str) -> str:
    from ..core import crs as crs_mod

    if crs_mod.is_local(crs):
        return ""
    try:
        from pyproj import CRS

        return CRS.from_user_input(crs).name
    except Exception:  # noqa: BLE001
        return ""


def _metric(chunk) -> bool:
    """Coordinates in metres: a projected CRS, or local ones scaled (references, scale bars, a
    transform kept from a georeferencing) or measured in metres (laser scans, models imported in
    metres: ``meta["metric"]``), as for the alignment of chunks."""
    from ..core import crs as crs_mod

    if crs_mod.is_geographic(chunk.crs):
        return False
    if not crs_mod.is_local(chunk.crs) or chunk.meta.get("metric") or abs(chunk.scale - 1.0) > 1e-9:
        return True
    return any(m.reference.location for m in chunk.markers) or any(not sb.checkpoint for sb in chunk.scalebars)


def _georeferenced(chunk) -> bool:
    from ..core import crs as crs_mod

    return not crs_mod.is_local(chunk.crs) and not crs_mod.is_geographic(chunk.crs)


def _extent_text(w: Words, bounds) -> str:
    left, bottom, right, top = bounds
    return f"X {w.coordinate(left)} - {w.coordinate(right)}, Y {w.coordinate(bottom)} - {w.coordinate(top)}"


def alignment_stats(chunk) -> dict:
    """Numbers of the COLMAP model of the chunk: tie points, projections, mean track length,
    mean reprojection error (refreshed), tie points of every photo (``per_image`` by image
    name). Empty when the chunk is not aligned."""
    path = chunk.product_path("reconstruction") if chunk.document is not None else None
    if path is None or not Path(path).exists():
        return {}
    points = Path(path) / "points3D.bin"
    if points.exists() and points.stat().st_size > MODEL_BYTES:  # millions of points: what the files say
        tp = chunk.product_path("tie_points")
        n = _ply_counts(tp).get("vertex") if tp is not None and tp.exists() else None
        err = (chunk.meta.get("sfm") or {}).get("reprojection_error")
        return {"tie_points": n, "projections": None, "track_length": None, "reprojection_error": err, "per_image": {}}
    import pycolmap

    rec = pycolmap.Reconstruction()
    rec.read(str(path))
    try:
        rec.update_point_3d_errors()
    except Exception:  # noqa: BLE001 - an older pycolmap: the stored errors
        pass
    out = {"tie_points": int(rec.num_points3D()), "projections": int(rec.compute_num_observations()),
           "track_length": float(rec.compute_mean_track_length()) if rec.num_points3D() else None,
           "reprojection_error": float(rec.compute_mean_reprojection_error()) if rec.num_points3D() else None,
           "per_image": {img.name: int(img.num_points3D) for img in rec.images.values()}}
    return out


def ground_resolution(chunk, max_points: int = 5000, max_cameras: int = 200) -> tuple[float | None, float | None]:
    """The median ground size of a pixel of the photos and the median distance from the
    cameras to the tie points they see (both in the units of the CRS), on a sample."""
    tp = chunk.tie_points
    cams = chunk.aligned_cameras
    if tp is None or not len(tp) or not cams:
        return None, None
    rng = np.random.default_rng(0)
    xyz = tp.xyz if len(tp) <= max_points else tp.xyz[rng.choice(len(tp), max_points, replace=False)]
    if len(cams) > max_cameras:
        cams = [cams[int(i)] for i in np.linspace(0, len(cams) - 1, max_cameras)]
    gsd, dist = [], []
    for cam in cams:
        s = chunk.sensor(cam)
        m = cam.cam_from_world
        pc = xyz @ m[:3, :3].T + m[:3, 3]
        z = pc[:, 2]
        front = z > 1e-9
        if not front.any():
            continue
        k = s.K
        u = pc[front, 0] / z[front] * k[0, 0] + k[0, 2]
        v = pc[front, 1] / z[front] * k[1, 1] + k[1, 2]
        inside = (u >= 0) & (u < s.width) & (v >= 0) & (v < s.height)
        if inside.sum() < 3:
            continue
        zz = float(np.median(z[front][inside]))
        dist.append(zz)
        gsd.append(zz / max(s.focal_px, 1e-9))
    if not gsd:
        return None, None
    scale = chunk.scale
    return float(np.median(gsd) * scale), float(np.median(dist) * scale)


def class_label(code: int, names: dict, language: str) -> str:
    """The name of a point class in the report's language."""
    name = str(names.get(code, ""))
    if language == "it" and code in ASPRS_IT and name == _asprs_name(code):
        return ASPRS_IT[code]
    if " / " in name:  # the archaeological classes: "Italian / English"
        it, en = name.split(" / ", 1)
        return it if language == "it" else en
    return name or str(code)


def _asprs_name(code: int) -> str:
    from ..core.pointcloud import ASPRS_CLASSES

    return ASPRS_CLASSES.get(code, "")


PARAM_STEPS = [  # (key in chunk.meta, title, parameters in order)
    ("sfm", "step_sfm", ["features", "matcher", "quality", "keypoint_limit", "tiepoint_limit", "pair_selection",
                         "generic", "reference", "neighbours", "pairs", "guided_matching", "apply_masks",
                         "stationary_removed", "mapper", "global_fallback", "masked_tie_points", "gpu", "gpu_pairs",
                         "cpu_pairs", "reprojection_error"]),
    ("optimize", "step_optimize", ["engine", "reprojection_error", "iterations", "converged", "control_points",
                                   "scale_bars", "steps"]),
    ("dense", "step_dense", ["backend", "quality", "filter", "fusion", "skipped_photos"]),
    ("point_cloud_source", "step_point_cloud", []),
    ("model", "step_model", ["source", "surface", "classes", "face_count"]),
    ("texture", "step_texture", ["mapping", "texture_size", "size", "pages", "colour_correction", "skipped_photos"]),
    ("dem", "step_dem", ["resolution", "source", "classes", "spikes_removed"]),
    ("orthomosaic", "step_orthomosaic", ["resolution", "surface", "blending", "blend_sigma", "colour_correction",
                                         "ghosting", "fill_holes", "hidden_classes", "max_angle", "max_cameras",
                                         "patches"]),
]
PARAM_SKIP = {"image_root", "names", "gpu_shares", "nodes", "ba_schedule", "tie_point_limit", "optimize", "added",
              "continued", "gps_priors", "applied_patches", "base", "before", "after", "gnss_bias",
              "rolling_shutter_m", "rigs", "masks", "stationary", "skipped"}


def _param_value(w: Words, key: str, value) -> str | None:
    """A parameter as text (None: not shown)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return w.yes_no(value)
    if isinstance(value, int):
        return w.integer(value)
    if isinstance(value, float):
        if key == "resolution":
            return w.resolution(value)
        return w.number(value, 4 if abs(value) < 1 else 3)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        if all(isinstance(v, (int, float, str)) for v in value):
            if key == "size" and len(value) == 2:
                return f"{value[0]} x {value[1]}"
            items = [str(v) for v in value[:12]]
            return ", ".join(items) + (" ..." if len(value) > 12 else "")
        return w.integer(len(value)) if key == "patches" else None
    return None


def parameter_rows(chunk, w: Words) -> list[tuple[str, str, str]]:
    """``(step, parameter, value)`` of the processing parameters kept in ``chunk.meta``."""
    out = []
    for key, title, keys in PARAM_STEPS:
        if key == "optimize":
            meta = (chunk.meta.get("sfm") or {}).get("optimize")
        else:
            meta = chunk.meta.get(key)
        if meta is None:
            continue
        if not isinstance(meta, dict):
            text = _param_value(w, key, meta)
            if text:
                out.append((w(title), w(f"p_{key}"), text))
            continue
        shown = [k for k in keys if k in meta] + [k for k in meta if k not in keys and k not in PARAM_SKIP]
        for k in shown:
            if key == "sfm" and k == "reprojection_error" and meta.get("optimize"):
                continue  # in the optimisation's rows
            text = _param_value(w, k, meta[k])
            if text is None:
                continue
            label = w(f"p_{k}") if f"p_{k}" in TEXT else k
            out.append((w(title), label, text))
    return out


# ---------------------------------------------------------------------------------------- the parts
def _survey(chunk, w: Words, align: dict, dem: Raster | None, ortho: Raster | None) -> Section:
    from ..core import crs as crs_mod

    sec = Section(w("survey"))
    n = len(chunk.cameras)
    enabled = sum(1 for c in chunk.cameras if c.enabled)
    aligned = len(chunk.aligned_cameras)
    rows = [(w("photos"), w("photos_value", w.integer(n), w.integer(enabled)) if enabled != n else w.integer(n)),
            (w("aligned"), w("aligned_value", w.integer(aligned), w.integer(n))),
            (w("sensors"), w.integer(len(chunk.sensors)))]
    if align:
        rows.append((w("tie_points"), w.integer(align["tie_points"])))
        rows.append((w("projections"), w.integer(align["projections"])))
        if align.get("track_length"):
            rows.append((w("track"), w.number(align["track_length"], 2)))
        if align.get("reprojection_error") is not None:
            rows.append((w("reprojection"), f"{w.number(align['reprojection_error'], 3)} px"))
    elif chunk.tie_points is not None:
        rows.append((w("tie_points"), w.integer(len(chunk.tie_points))))
    metric = _metric(chunk)
    if aligned:
        try:
            gsd, dist = ground_resolution(chunk)
        except Exception as exc:  # noqa: BLE001
            log.warning("report: ground resolution of %s: %s", chunk.label, exc)
            gsd, dist = None, None
        if gsd and metric:
            rows.append((w("gsd"), w.resolution(gsd)))
        if dist and metric:
            rows.append((w("camera_distance"), w.length(dist) if dist < 1 else f"{w.number(dist, 2)} m"))
    if dem is not None and metric:
        valid = np.isfinite(dem.data[0]).sum()
        rows.append((w("coverage"), w.area(valid * dem.cell * dem.cell)))
    name = _crs_name(chunk.crs)
    rows.append((w("crs"), w("local") if crs_mod.is_local(chunk.crs) else (f"{chunk.crs} - {name}" if name else chunk.crs)))
    if crs_mod.is_local(chunk.crs):  # local coordinates: the scale given by the bars (1: none)
        rows.append((w("scale"), w.significant(chunk.scale)))
    sec.blocks.append(KeyValues(rows))
    if not aligned:
        sec.blocks.append(Paragraph(w("not_aligned"), note=True))
        return sec
    try:
        fig = _camera_figure(chunk, w, dem, ortho)
        if fig is not None:
            sec.blocks.append(fig)
    except Exception as exc:  # noqa: BLE001
        log.warning("report: camera positions of %s: %s", chunk.label, exc)
    return sec


def _camera_figure(chunk, w: Words, dem: Raster | None, ortho: Raster | None) -> Figure | None:
    """The camera centres and the markers over the orthomosaic (or the DEM, or the tie points),
    in plan view."""
    from PIL import Image, ImageDraw

    cams = np.array([chunk.to_world(c.center)[:2] for c in chunk.aligned_cameras], float).reshape(-1, 2)
    marks = [(m, chunk.to_world(m.position)[:2]) for m in chunk.markers if m.position is not None]
    bg, bounds = None, None
    if ortho is not None:
        bg, bounds = _ortho_rgb(ortho), ortho.bounds
    elif dem is not None:
        z = dem.data[0]
        v = z[np.isfinite(z)]
        if v.size:
            bg, bounds = dem_rgb(z, dem.cell, tuple(np.percentile(v, [1, 99]))), dem.bounds
    pts = [cams] + ([np.array([p for _m, p in marks])] if marks else [])
    tie = None
    if bg is None and chunk.tie_points is not None and len(chunk.tie_points):
        tp = chunk.tie_points.xyz
        if len(tp) > 200_000:
            tp = tp[np.random.default_rng(0).choice(len(tp), 200_000, replace=False)]
        tie = chunk.to_world(tp)[:, :2]
        pts.append(tie)
    allp = np.vstack([p for p in pts if len(p)])
    xmin, ymin = allp.min(axis=0)
    xmax, ymax = allp.max(axis=0)
    if bounds is not None:
        xmin, ymin = min(xmin, bounds[0]), min(ymin, bounds[1])
        xmax, ymax = max(xmax, bounds[2]), max(ymax, bounds[3])
    span = max(xmax - xmin, ymax - ymin, 1e-6)
    pad = 0.04 * span
    xmin, xmax, ymin, ymax = xmin - pad, xmax + pad, ymin - pad, ymax + pad
    side = 1400
    s = side / max(xmax - xmin, ymax - ymin)
    W, H = max(1, int(round((xmax - xmin) * s))), max(1, int(round((ymax - ymin) * s)))
    k = 2  # drawn at twice the size, then reduced: smooth circles
    canvas = Image.new("RGB", (W * k, H * k), (255, 255, 255))
    if bg is not None:
        x0, y0 = (bounds[0] - xmin) * s * k, (ymax - bounds[3]) * s * k
        bw, bh = max(1, int(round((bounds[2] - bounds[0]) * s * k))), max(1, int(round((bounds[3] - bounds[1]) * s * k)))
        light = (0.72 * bg.astype(np.float32) + 0.28 * 255).astype(np.uint8)  # the points stand out
        canvas.paste(Image.fromarray(light).resize((bw, bh), Image.BILINEAR), (int(round(x0)), int(round(y0))))
    elif tie is not None:
        cols = np.clip(((tie[:, 0] - xmin) * s * k).astype(int), 0, W * k - 1)
        rows = np.clip(((ymax - tie[:, 1]) * s * k).astype(int), 0, H * k - 1)
        dens = np.zeros((H * k, W * k), np.float32)
        np.add.at(dens, (rows, cols), 1.0)
        import cv2

        dens = cv2.GaussianBlur(dens, (0, 0), 2.0 * k)
        if dens.max() > 0:
            g = 255 - (np.log1p(dens) / np.log1p(dens.max()) * 120).astype(np.uint8)
            canvas = Image.fromarray(np.repeat(g[..., None], 3, axis=2))
    draw = ImageDraw.Draw(canvas)
    r = max(4.0, side / 150.0) * k  # about a millimetre and a half on the page
    for x, y in cams:
        px, py = (x - xmin) * s * k, (ymax - y) * s * k
        draw.ellipse([px - r, py - r, px + r, py + r], fill=CAMERA_COLOUR, outline=(255, 255, 255), width=k)
    font = _pil_font(int(24 * k))
    label = len(marks) <= 40
    for m, (x, y) in marks:
        px, py = (x - xmin) * s * k, (ymax - y) * s * k
        colour = (CHECK_COLOUR if m.checkpoint else CONTROL_COLOUR) if m.reference.location else MARKER_COLOUR
        t = r * 1.6
        draw.polygon([(px, py - t), (px - t * 0.9, py + t * 0.6), (px + t * 0.9, py + t * 0.6)], fill=colour,
                     outline=(0, 0, 0))
        if label:
            draw.text((px + t, py - t * 1.2), m.label, fill=(0, 0, 0), font=font, stroke_width=k, stroke_fill=(255, 255, 255))
    img = canvas.resize((W, H), Image.LANCZOS)
    items = [(CAMERA_COLOUR, w("legend_cameras"))]
    kinds = {("check" if m.checkpoint else "control") if m.reference.location else "none" for m, _p in marks}
    for kind, colour, key in (("control", CONTROL_COLOUR, "legend_control"), ("check", CHECK_COLOUR, "legend_check"),
                              ("none", MARKER_COLOUR, "legend_markers")):
        if kind in kinds:
            items.append((colour, w(key)))
    metric = _metric(chunk)
    return _figure(np.asarray(img), caption=w("camera_locations"), legend=Legend(items),
                   metres_per_pixel=(1.0 / s) if metric else None, north=_georeferenced(chunk))


def _pil_font(size: int):
    from PIL import ImageFont

    path = font_files().get("")
    if path:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow before 10.1
        return ImageFont.load_default()


def font_files() -> dict[str, Path]:
    """TrueType files of a Unicode font for the report (``""`` regular, ``"B"`` bold, ``"I"``
    italic, ``"BI"``): DejaVu Sans of matplotlib, else of the system, else Arial; empty when none
    is found (the renderers fall back to the core fonts)."""
    names = {"": "DejaVuSans.ttf", "B": "DejaVuSans-Bold.ttf", "I": "DejaVuSans-Oblique.ttf",
             "BI": "DejaVuSans-BoldOblique.ttf"}
    folders: list[Path] = []
    try:
        import importlib.util

        spec = importlib.util.find_spec("matplotlib")
        for base in (spec.submodule_search_locations or []) if spec else []:
            folders.append(Path(base) / "mpl-data" / "fonts" / "ttf")
    except (ImportError, ValueError):
        pass
    folders += [Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu"),
                Path("/usr/share/fonts/TTF"), Path("/Library/Fonts"), Path.home() / "Library" / "Fonts"]
    for folder in folders:
        if (folder / names[""]).exists():
            return {k: folder / v for k, v in names.items() if (folder / v).exists()}
    try:  # matplotlib's own lookup (builds its font cache the first time)
        from matplotlib import font_manager

        regular = font_manager.findfont(font_manager.FontProperties(family="DejaVu Sans"), fallback_to_default=False)
        if regular and Path(regular).exists():
            folder = Path(regular).parent
            return {k: folder / v for k, v in names.items() if (folder / v).exists()} or {"": Path(regular)}
    except Exception:  # noqa: BLE001 - matplotlib missing
        pass
    arial = [(Path("/System/Library/Fonts/Supplemental"), ("Arial.ttf", "Arial Bold.ttf", "Arial Italic.ttf",
                                                           "Arial Bold Italic.ttf")),
             (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts", ("arial.ttf", "arialbd.ttf", "ariali.ttf",
                                                                        "arialbi.ttf"))]
    for folder, files in arial:
        if (folder / files[0]).exists():
            return {k: folder / f for k, f in zip(("", "B", "I", "BI"), files) if (folder / f).exists()}
    return {}


def _ortho_rgb(r: Raster) -> np.ndarray:
    """RGB of an orthomosaic read with :func:`read_raster` (bands 1-3, and alpha when there),
    white where empty, reduced to the size of a figure."""
    rgb = r.data[:3] if r.data.shape[0] >= 3 else np.repeat(r.data[:1], 3, axis=0)
    empty = ~np.isfinite(rgb).all(axis=0)
    if r.data.shape[0] >= 4:
        empty |= ~(r.data[3] > 0)
    out = np.where(empty[None], 255, np.nan_to_num(rgb, nan=255)).clip(0, 255).astype(np.uint8)
    out = np.moveaxis(out, 0, -1)
    k = max(1, int(math.ceil(max(out.shape[:2]) / float(FIGURE_SIDE))))
    return out[::k, ::k] if k > 1 else out


def _sensors(chunk, w: Words) -> Section | None:
    from ..core.camera import SINGLE_FOCAL

    if not chunk.sensors:
        return None
    sec = Section(w("calibration"))
    names = {"SIMPLE_PINHOLE": ["f", "cx", "cy"], "PINHOLE": ["fx", "fy", "cx", "cy"],
             "SIMPLE_RADIAL": ["f", "cx", "cy", "k"], "RADIAL": ["f", "cx", "cy", "k1", "k2"],
             "OPENCV": ["fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2"],
             "FULL_OPENCV": ["fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6"],
             "SIMPLE_RADIAL_FISHEYE": ["f", "cx", "cy", "k"], "RADIAL_FISHEYE": ["f", "cx", "cy", "k1", "k2"],
             "OPENCV_FISHEYE": ["fx", "fy", "cx", "cy", "k1", "k2", "k3", "k4"], "FOV": ["fx", "fy", "cx", "cy", "omega"],
             "THIN_PRISM_FISHEYE": ["fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "k3", "k4", "sx1", "sy1"]}
    count = {}
    for c in chunk.cameras:
        count[c.sensor_key] = count.get(c.sensor_key, 0) + 1
    for s in chunk.sensors.values():
        sub = Section(w("sensor_title", s.label), level=2)
        focal = f"{w.number(s.focal_px, 2)} px"
        if s.focal_length_mm:
            focal += f" ({w.number(s.focal_length_mm, 2)} mm)"
        rows = [(w("resolution_px"), f"{s.width} x {s.height} px"), (w("camera_model"), s.model),
                (w("sensor_photos"), w.integer(count.get(s.key, 0))), (w("focal_px"), focal)]
        if s.pixel_size_mm:
            rows.append((w("pixel_size"), f"{w.number(s.pixel_size_mm * 1000, 2)} µm"))
        rows.append((w("fixed"), w.yes_no(s.fixed)))
        sub.blocks.append(KeyValues(rows))
        labels = names.get(s.model, [f"p{i}" for i in range(len(s.params))])
        params = []
        for name, v in zip(labels, s.params):
            if name in ("f", "fx", "fy", "cx", "cy"):
                text = w.number(v, 3)
            else:
                text = w.significant(v)
            params.append([name, text])
        if s.model in SINGLE_FOCAL or params:
            sub.blocks.append(Table([w("h_parameter"), w("h_value")], params, align=["L", "R"]))
        sec.blocks.append(sub)
    return sec


def _rmse_row(w: Words, label: str, errors: list[dict]) -> list[str] | None:
    d = np.array([[e["dx"], e["dy"], e["dz"]] for e in errors if e.get("error") is not None], float)
    if not len(d):
        return None
    r = np.sqrt((d ** 2).mean(axis=0))
    xy = float(np.sqrt((d[:, 0] ** 2 + d[:, 1] ** 2).mean()))
    total = float(np.sqrt((d ** 2).sum(axis=1).mean()))
    return [label, w.integer(len(d)), w.number(r[0], 4), w.number(r[1], 4), w.number(r[2], 4), w.number(xy, 4),
            w.number(total, 4)]


def _reference(chunk, w: Words) -> Section | None:
    from ..markers import gcp

    cam_refs = {}
    try:
        cam_refs = gcp.camera_reference_errors(chunk)
    except Exception as exc:  # noqa: BLE001 - an unknown CRS
        log.warning("report: camera references of %s: %s", chunk.label, exc)
    if not chunk.markers and not chunk.scalebars and not cam_refs:
        return None
    sec = Section(w("reference"))
    markers = gcp.marker_errors(chunk) if chunk.markers else []
    bars = gcp.scalebar_errors(chunk) if chunk.scalebars else []
    rows = []
    method = _method(chunk, w, markers, cam_refs)
    if method:
        rows.append((w("georeferencing"), method))
    for kind, key in (("control", "control_rmse"), ("check", "check_rmse")):
        errs = [e["error"] for e in markers if e["type"] == kind and e["error"] is not None]
        if errs:
            rows.append((w(key), w.length(float(np.sqrt(np.mean(np.square(errs)))))))
    if rows:
        sec.blocks.append(KeyValues(rows))
    if markers:
        sub = Section(w("markers"), level=2)
        table = []
        for e in markers:
            table.append([e["label"], w(f"type_{e['type']}"),
                          w.integer(e["projections"]) + (f" (+{e['estimated']})" if e.get("estimated") else ""),
                          w.number(e["pixel_error"], 2) if e.get("pixel_error") is not None else "-",
                          *(w.number(e[k], 4) if e.get(k) is not None else "-" for k in ("dx", "dy", "dz", "error"))])
        sub.blocks.append(Table([w("h_marker"), w("h_type"), w("h_projections"), w("h_error_px"), w("h_dx"), w("h_dy"),
                                 w("h_dz"), w("h_error_m")], table))
        rmse = [r for r in (_rmse_row(w, w("legend_control"), [e for e in markers if e["type"] == "control"]),
                            _rmse_row(w, w("legend_check"), [e for e in markers if e["type"] == "check"])) if r]
        if rmse:
            sub.blocks.append(Paragraph(w("rmse_title"), note=True))
            sub.blocks.append(Table([w("h_points"), w("h_count"), w("h_x_m"), w("h_y_m"), w("h_z_m"), w("h_xy_m"),
                                     w("h_total_m")], rmse))
        sec.blocks.append(sub)
    if bars:
        sub = Section(w("scalebars"), level=2)
        sub.blocks.append(Table([w("h_bar"), w("h_marker_a"), w("h_marker_b"), w("h_distance"), w("h_estimated"),
                                 w("h_error_m"), w("h_type")],
                                [[e["label"], e["marker_a"], e["marker_b"], w.number(e["distance"], 4),
                                  w.number(e["estimated"], 4) if e.get("estimated") is not None else "-",
                                  w.number(e["error"], 4) if e.get("error") is not None else "-", w(f"type_{e['type']}")]
                                 for e in bars]))
        sec.blocks.append(sub)
    if cam_refs:
        sub = Section(w("camera_gps"), level=2)
        rows = [(w("cameras_with_gps"), w.integer(len(cam_refs)))]
        d = np.array([e["delta"] for e in cam_refs.values() if "delta" in e], float).reshape(-1, 3)
        if len(d):
            r = np.sqrt((d ** 2).mean(axis=0))
            total = float(np.sqrt((d ** 2).sum(axis=1).mean()))
            rows.append((w("gps_rmse"), " / ".join(w.length(v) for v in (*r, total))))
        sub.blocks.append(KeyValues(rows))
        sec.blocks.append(sub)
    return sec


def _method(chunk, w: Words, markers: list[dict], cam_refs: dict) -> str:
    """How the chunk was placed, from what it has (the report of update_transform is not kept):
    three control points or more, else the GPS of the photos, else the control scale bars."""
    from ..markers import gcp

    ctrl = [e for e in markers if e["type"] == "control" and e["error"] is not None]
    gps = any("delta" in e for e in cam_refs.values())
    bars = bool(gcp.control_scalebars(chunk))
    if len(ctrl) >= 3:
        text = w("method_markers")
    elif gps:
        text = w("method_cameras")
    elif bars:
        return w("method_scalebars")
    else:
        return ""
    return text + (w("plus_scalebars") if bars else "")


def _parameters(chunk, w: Words) -> Section | None:
    rows = parameter_rows(chunk, w)
    if not rows:
        return None
    sec = Section(w("parameters"))
    table, last = [], None
    for step, name, value in rows:
        table.append([step if step != last else "", name, value])
        last = step
    sec.blocks.append(Table([w("h_step"), w("h_parameter"), w("h_value")], table, align=["L", "L", "L"]))
    return sec


def _dense(chunk, w: Words) -> Section | None:
    path = chunk.product_path("point_cloud")
    if path is None or not path.exists():
        return None
    sec = Section(w("dense_cloud"))
    n = _point_count(path)
    rows = [(w("points"), w.integer(n) if n is not None else "-")] + _file_rows(w, chunk, path)
    sec.blocks.append(KeyValues(rows))
    counts = chunk.point_classes()
    if counts and set(counts) not in ({1}, {0}):
        from ..ai.classify import class_names

        names = class_names(chunk)
        total = max(sum(counts.values()), 1)
        table = [[str(c), class_label(c, names, w.language), w.integer(k), w.number(100.0 * k / total, 2)]
                 for c, k in sorted(counts.items())]
        sub = Section(w("classes"), level=2)
        sub.blocks.append(Table([w("h_code"), w("h_class"), w("h_points"), w("h_percent")], table))
        sec.blocks.append(sub)
    return sec


def _model(chunk, w: Words) -> Section | None:
    path = chunk.product_path("model")
    if path is None or not path.exists():
        return None
    sec = Section(w("model_title"))
    counts = _ply_counts(path) if path.suffix.lower() == ".ply" else {}
    rows = []
    if counts.get("face") is not None:
        rows.append((w("faces"), w.integer(counts["face"])))
    if counts.get("vertex") is not None:
        rows.append((w("vertices"), w.integer(counts["vertex"])))
    tex = chunk.meta.get("texture") or {}
    textured = chunk.product_path("model_textured")
    if textured is not None and textured.exists():
        size = tex.get("size") or [None, None]
        pages = int(tex.get("pages") or 1)
        if size[0]:
            rows.append((w("texture"), w("texture_value", pages, size[0], size[1])))
        else:
            rows.append((w("texture"), _relative(textured, chunk)))
    rows += _file_rows(w, chunk, path)
    sec.blocks.append(KeyValues(rows))
    return sec


def _raster_rows(w: Words, chunk, r: Raster) -> list[tuple[str, str]]:
    return [(w("raster_size"), w("raster_size_value", w.integer(r.width), w.integer(r.height))),
            (w("resolution"), w.resolution(r.res) if _metric(chunk) else w.number(r.res, 6)),
            (w("extent"), _extent_text(w, r.bounds))]


def _scale(chunk, r: Raster, image_width: int) -> float | None:
    """Ground size of a pixel of a figure of ``image_width`` pixels made from ``r`` (None when the
    coordinates are not metres)."""
    if not _metric(chunk) or image_width <= 0:
        return None
    return r.cell * r.data.shape[2] / float(image_width)


def _dem(chunk, w: Words, dem: Raster) -> Section:
    sec = Section(w("dem_title"))
    z = dem.data[0]
    st = stats(z)
    rows = _raster_rows(w, chunk, dem)
    if st:
        rows.append((w("elevations"), f"{w.number(st['min'], 3)} - {w.number(st['max'], 3)} m"))
        rows.append((w("mean_elevation"), f"{w.number(st['mean'], 3)} m"))
        rows.append((w("area_data"), w.area(st["n"] * dem.cell * dem.cell)))
    rows += _file_rows(w, chunk, dem.path)
    sec.blocks.append(KeyValues(rows))
    if dem.sampled and st:
        sec.blocks.append(Paragraph(w("sample_note", w.integer(st["n"])), note=True))
    if st:
        small, k = _reduce(z)
        lo, hi = (float(v) for v in np.percentile(z[np.isfinite(z)], [1, 99]))
        hi = hi if hi > lo else lo + 1e-3
        rgb = dem_rgb(small, dem.cell * k, (lo, hi))
        sec.blocks.append(_figure(rgb, caption=w("dem_caption"),
                                  colour_bar=ColourBar(DEM_STOPS, lo, hi, w("elevation_m"), _decimals_for(lo, hi)),
                                  metres_per_pixel=_scale(chunk, dem, rgb.shape[1]), north=_georeferenced(chunk)))
    return sec


def _ortho(chunk, w: Words, ortho: Raster) -> Section:
    sec = Section(w("ortho_title"))
    sec.blocks.append(KeyValues(_raster_rows(w, chunk, ortho) + _file_rows(w, chunk, ortho.path)))
    rgb = _ortho_rgb(ortho)
    sec.blocks.append(_figure(rgb, caption=w("ortho_title"), metres_per_pixel=_scale(chunk, ortho, rgb.shape[1]),
                              north=_georeferenced(chunk)))
    return sec


# ---------------------------------------------------------------------------------------- multispectral
def _ms_tags(chunk) -> dict:
    """The XMP tags of a spectral band of the first capture whose files can be read (empty when
    none can: the captures moved, a disk unplugged)."""
    from ..engine.multispectral import NON_SPECTRAL, multispectral_cameras, xmp_of

    for cam in multispectral_cameras(chunk)[:5]:
        for band, f in (cam.meta.get("bands") or {}).items():
            if band in NON_SPECTRAL or not Path(f).exists():
                continue
            try:
                return xmp_of(f)
            except Exception:  # noqa: BLE001
                continue
    return {}


def _radiometric_model(tags: dict) -> str:
    """The radiometric model :func:`engine.multispectral.radiance_image` takes for these tags."""
    from ..engine.multispectral import _numbers

    if tags.get("VignettingData") is not None and tags.get("CalibratedOpticalCenterX") is not None:
        return "dji"
    if len(_numbers(tags.get("RadiometricCalibration"))) >= 3:
        return "micasense"
    return "normalised"


def _false_colour(refl: Raster, names: list[str]) -> tuple[np.ndarray, list[str]]:
    """Three bands of the reflectance stretched 2-98 % (NIR, Red, Green when there are), white
    where empty."""
    have = set(names)
    pick = (["NIR", "Red", "Green"] if {"NIR", "Red", "Green"} <= have else
            ["Red", "Green", "Blue"] if {"Red", "Green", "Blue"} <= have else names[:3])
    if len(pick) < 3:
        pick = [pick[0]] * 3
    chans, empty = [], None
    for b in pick:
        v, _k = _reduce(refl.data[names.index(b)])
        fin = v[np.isfinite(v)]
        lo, hi = (np.percentile(fin, [2, 98]) if fin.size > 16 else (0.0, 1.0))
        chans.append(np.clip((np.nan_to_num(v, nan=lo) - lo) / max(hi - lo, 1e-9) * 255, 0, 255))
        missing = ~np.isfinite(v)
        empty = missing if empty is None else (empty | missing)
    rgb = np.stack(chans, -1).astype(np.uint8)
    rgb[empty] = 255
    return rgb, pick


def _multispectral(chunk, w: Words, index_palette: str) -> Section | None:
    from ..engine.multispectral import INDICES, NON_SPECTRAL, irradiance_of, multispectral_cameras

    ms = chunk.meta.get("multispectral") or {}
    spectral = [b for b in (ms.get("bands") or []) if b not in NON_SPECTRAL]
    if not spectral:
        return None
    data = chunk.data_dir
    sec = Section(w("ms_title"))
    cams = multispectral_cameras(chunk)
    tags = _ms_tags(chunk)
    rows = [(w("captures"), w.integer(ms.get("captures") or len(cams))), (w("bands"), ", ".join(spectral)),
            (w("reference_band"), str(ms.get("reference") or "-"))]
    model = tags.get("Model") or (chunk.sensor(cams[0]).label if cams else None)
    if model:
        rows.append((w("camera"), str(model)))
    rows.append((w("radiometric_model"), w(f"model_{_radiometric_model(tags)}")))
    panel = ms.get("panel")
    if panel:
        cam = next((c for c in chunk.cameras if c.key == panel.get("camera")), None)
        refl = panel.get("reflectance")
        refl_text = (", ".join(f"{b} {w.number(v, 3)}" for b, v in refl.items()) if isinstance(refl, dict)
                     else w.number(refl, 3))
        calibration = w("cal_panel_dls" if panel.get("follow_dls") else "cal_panel",
                        cam.meta.get("capture", cam.label) if cam else panel.get("camera"), refl_text)
    elif tags and irradiance_of(tags):
        calibration = w("cal_dls")
    else:
        calibration = w("cal_none")
    rows.append((w("radiometric_calibration"), calibration))
    balance = ms.get("balance") or {}
    if balance:
        rows.append((w("balance"), w("balance_planes") if balance.get("model", "planes") == "planes" else w("balance_gains")))
    elif ms.get("reflectance"):
        rows.append((w("balance"), w("balance_off")))
    sec.blocks.append(KeyValues(rows))
    # the bands: panel factor, registration, balance
    quality = ms.get("registration_quality") or {}
    factors = (panel or {}).get("factors") or {}
    table = []
    for b in spectral:
        cc = [q[b]["correlation"] for q in quality.values() if isinstance(q, dict) and isinstance(q.get(b), dict)]
        on = [q[b].get("on") for q in quality.values() if isinstance(q, dict) and isinstance(q.get(b), dict)]
        common = max(set(on), key=on.count) if on else None
        spread = (balance.get("spread") or {}).get(b)
        residual = (balance.get("residual") or {}).get(b)
        table.append([b + (f" ({w('reference_word')})" if b == ms.get("reference") else ""),
                      w.number(factors[b], 4) if b in factors else "-",
                      w.number(float(np.median(cc)), 2) if cc else "-", str(common) if common else "-",
                      w.number(100 * spread, 1) if spread is not None else "-",
                      w.number(100 * residual, 1) if residual is not None else "-"])
    sec.blocks.append(Table([w("h_band"), w("h_panel_factor"), w("h_registration"), w("h_registered_on"),
                             w("h_balance_spread"), w("h_balance_residual")], table))
    # the reflectance orthomosaic
    rel = ms.get("reflectance")
    if rel and (data / rel).exists():
        refl = read_raster(data / rel)
        names = list(ms.get("reflectance_bands") or refl.descriptions)[:refl.data.shape[0]]
        names += refl.descriptions[len(names):refl.data.shape[0]]
        sub = Section(w("reflectance"), level=2)
        sub.blocks.append(KeyValues(_raster_rows(w, chunk, refl) + _file_rows(w, chunk, refl.path)))
        rgb, pick = _false_colour(refl, names)
        caption = w("false_colour", *pick) if len(set(pick)) == 3 else w("single_band", pick[0])
        sub.blocks.append(_figure(rgb, caption=caption, metres_per_pixel=_scale(chunk, refl, rgb.shape[1]),
                                  north=_georeferenced(chunk)))
        st_rows = []
        for i, b in enumerate(names):
            st = stats(refl.data[i])
            if st:
                st_rows.append([b, *(w.number(st[k], 4) for k in ("min", "max", "mean", "p2", "p98"))])
        if st_rows:
            sub.blocks.append(Paragraph(w("reflectance_stats"), note=True))
            sub.blocks.append(Table([w("h_band"), w("h_min"), w("h_max"), w("h_mean"), w("h_p2"), w("h_p98")], st_rows))
            if refl.sampled:
                sub.blocks.append(Paragraph(w("sample_note", w.integer(int(np.isfinite(refl.data[0]).sum()))), note=True))
        sec.blocks.append(sub)
    # the indices: a table of all of them, then a figure each
    indices = {name: info for name, info in (ms.get("indices") or {}).items()
               if info.get("file") and (data / info["file"]).exists()}
    read: dict[str, tuple[Raster, dict]] = {}
    if indices:
        sub = Section(w("indices"), level=2)
        table = []
        for name, info in indices.items():
            r = read_raster(data / info["file"], [1])
            st = stats(r.data[0])
            read[name] = (r, st)
            table.append([name, info.get("formula") or INDICES.get(name, ("", ""))[0],
                          *(w.number(st.get(k), 4) for k in ("min", "max", "mean", "p2", "p98"))])
        sub.blocks.append(Table([w("h_index"), w("h_formula"), w("h_min"), w("h_max"), w("h_mean"), w("h_p2"),
                                 w("h_p98")], table, align=["L", "L", "R", "R", "R", "R", "R"]))
        sec.blocks.append(sub)
        stops = palette_stops(index_palette)
        for name, (r, st) in read.items():
            if not st:
                continue
            fig_sec = Section(w("index_title", name), level=2)
            lo, hi = st["p2"], st["p98"]
            if lo >= -1.0 and hi <= 1.0:
                lo, hi = max(lo, -1.0), min(hi, 1.0)
            hi = hi if hi > lo else lo + 1e-6
            small, _k = _reduce(r.data[0])
            desc = INDICES.get(name, (None, ""))[1]
            fig_sec.blocks.append(_figure(_coloured(small, stops, lo, hi),
                                          caption=w("index_caption", name, desc) if desc else name,
                                          colour_bar=ColourBar(stops, float(lo), float(hi), name, _decimals_for(lo, hi)),
                                          metres_per_pixel=_scale(chunk, r, small.shape[1]),
                                          north=_georeferenced(chunk)))
            sec.blocks.append(fig_sec)
    # the zones
    for name, summary in (ms.get("zoning") or {}).items():
        raster = summary.get("raster")
        if not raster or not (data / raster).exists():
            continue
        sec.blocks.append(_zoning(chunk, w, name, summary, read.get(name, (None, {}))[1]))
    return sec


def _zoning(chunk, w: Words, name: str, summary: dict, index_stats: dict) -> Section:
    sub = Section(w("zoning_title", name), level=2)
    r = read_raster(chunk.data_dir / summary["raster"], [1], max_cells=PREVIEW_CELLS)
    cls = r.data[0]
    classes = int(summary.get("classes") or (np.nanmax(cls) if np.isfinite(cls).any() else 0))
    breaks = list(summary.get("breaks") or [])
    method = w(f"method_{summary.get('method', 'quantiles')}")
    sub.blocks.append(KeyValues([(w("zoning_title", name), w("zoning_value", classes, method,
                                                              w.integer(summary.get("polygons") or 0)))]))
    ends = [index_stats.get("min")] + breaks + [index_stats.get("max")]
    areas = {int(k): float(v) for k, v in (summary.get("area_m2") or {}).items()}
    total = sum(areas.values()) or 1.0
    small, _k = _reduce(cls, mean=False)
    valid = np.isfinite(small) & (small > 0)
    rgb = np.full(small.shape + (3,), 255, np.uint8)
    kk = (np.where(valid, small, 1).astype(int) - 1) % len(ZONE_COLORS)
    rgb[valid] = np.asarray(ZONE_COLORS, np.uint8)[kk[valid]]
    items, table = [], []
    for z in range(1, classes + 1):
        a = ends[z - 1] if z - 1 < len(ends) else None
        b = ends[z] if z < len(ends) else None
        items.append((ZONE_COLORS[(z - 1) % len(ZONE_COLORS)], w("zone_label", z, w.number(a, 3), w.number(b, 3))))
        table.append([str(z), w.number(a, 4), w.number(b, 4), w.number(areas.get(z), 2) if z in areas else "-",
                      w.number(100.0 * areas.get(z, 0.0) / total, 1) if areas else "-"])
    sub.blocks.append(_figure(rgb, flat=True, caption=w("zoning_title", name), legend=Legend(items),
                              metres_per_pixel=_scale(chunk, r, rgb.shape[1]), north=_georeferenced(chunk)))
    sub.blocks.append(Table([w("h_zone"), w("h_from"), w("h_to"), w("h_area_m2"), w("h_area_share")], table))
    return sub


# ---------------------------------------------------------------------------------------- thermal
def _first_frame(chunk) -> Path | None:
    from ..engine import thermal

    frames = thermal.frames_dir(chunk)
    return next(iter(sorted(frames.glob("*.tif"))), None) if frames.exists() else None


def _decoder_of(chunk) -> str | None:
    """How the thermal frames were decoded: the tag of a frame decoded, else the kind of a file."""
    from ..engine import thermal

    frame = _first_frame(chunk)
    if frame is not None:
        try:
            import rasterio

            with rasterio.open(frame) as src:
                kind = src.tags().get("decoder")
            if kind:
                return kind
        except Exception:  # noqa: BLE001
            pass
    for cam in chunk.cameras:
        f = thermal.thermal_file(cam)
        if f and Path(f).exists():
            try:
                return thermal.kind_of(f)
            except Exception:  # noqa: BLE001
                return None
    return None


def _frame_size(chunk) -> tuple[int, int] | None:
    frame = _first_frame(chunk)
    if frame is None:
        return None
    try:
        import rasterio

        with rasterio.open(frame) as src:
            return src.width, src.height
    except Exception:  # noqa: BLE001
        return None


def _thermal(chunk, w: Words, thermal_palette: str) -> Section | None:
    from ..engine import thermal

    th = chunk.meta.get("thermal") or {}
    cams = [c for c in chunk.cameras if thermal.thermal_file(c)]
    path = thermal.thermal_path(chunk)
    if not cams and path is None:
        return None
    sec = Section(w("thermal_title"))
    rows = [(w("thermal_frames"), w.integer(len(cams)))]
    kind = _decoder_of(chunk)
    if kind:
        rows.append((w("decoder"), w(f"decoder_{kind}") if f"decoder_{kind}" in TEXT else kind))
    if th.get("decoded"):
        rows.append((w("frames_decoded"), w.integer(th["decoded"])))
    size = _frame_size(chunk)
    if size:
        rows.append((w("frame_size"), w("frame_size_value", *size)))
    if thermal.thermal_only(chunk):
        rows.append((w("thermal_geometry"), w("thermal_alone")))
    elif th.get("registration"):
        reg = th["registration"]
        refined = sum(1 for v in (reg.get("captures") or {}).values() if v.get("refined"))
        rows.append((w("thermal_geometry"), w("thermal_registered", w.number(reg.get("cover"), 2),
                                              w.number(reg.get("fixed_cc"), 2), w.integer(refined))))
    if "offsets" in th:
        spread = (th.get("offsets") or {}).get("spread")
        rows.append((w("offsets"), w.degrees(spread) if spread is not None else "-"))
    elif path is not None:
        rows.append((w("offsets"), w("offsets_off")))
    sec.blocks.append(KeyValues(rows))
    p = thermal.params_of(chunk)
    sub = Section(w("measure_parameters"), level=2)
    prow = [(w("emissivity"), w.number(p["emissivity"], 2)), (w("reflected"), w.degrees(p["reflected"], 1)),
            (w("ambient"), w.degrees(p["ambient"], 1)), (w("humidity"), f"{w.number(p['humidity'], 0)} %"),
            (w("distance"), w("flight_height") if p.get("distance") is None else f"{w.number(p['distance'], 1)} m")]
    if p.get("scale") is not None and p.get("offset") is not None:
        prow.append((w("raw_transform"), f"°C = {w.number(p['scale'], 4)} x v + {w.number(p['offset'], 2)}"))
    sub.blocks.append(KeyValues(prow))
    sec.blocks.append(sub)
    if path is not None:
        r = read_raster(path, [1])
        t = r.data[0]
        st = stats(t)
        exact = th.get("stats") or {}
        if st and exact:  # computed on the whole orthomosaic when it was built
            st.update({k: float(exact[k]) for k in ("min", "max", "median", "p2", "p98") if k in exact})
        sub = Section(w("thermal_ortho"), level=2)
        sub.blocks.append(KeyValues(_raster_rows(w, chunk, r) + _file_rows(w, chunk, r.path)))
        if st:
            lo, hi = thermal.limits(t)
            small, _k = _reduce(t)
            stops = thermal_stops(thermal_palette)
            sub.blocks.append(_figure(_coloured(small, stops, lo, hi), caption=w("thermal_caption", thermal_palette),
                                      colour_bar=ColourBar(stops, float(lo), float(hi), w("temperature_c"),
                                                           _decimals_for(lo, hi)),
                                      metres_per_pixel=_scale(chunk, r, small.shape[1]), north=_georeferenced(chunk)))
            sub.blocks.append(Paragraph(w("temperature_stats"), note=True))
            sub.blocks.append(Table([w("h_min"), w("h_max"), w("h_mean"), w("h_median"), w("h_p2"), w("h_p98")],
                                    [[w.number(st.get(k), 2) for k in ("min", "max", "mean", "median", "p2", "p98")]]))
        sec.blocks.append(sub)
    an = th.get("anomaly") or {}
    if an.get("file") and (chunk.data_dir / an["file"]).exists():
        r = read_raster(chunk.data_dir / an["file"], [1], max_cells=2 * PREVIEW_CELLS)
        d = r.data[0]
        fin = d[np.isfinite(d)]
        if fin.size:
            lim = float(max(np.percentile(np.abs(fin), 98), 1e-6))
            small, _k = _reduce(d)
            sub = Section(w("anomalies"), level=2)
            sub.blocks.append(_figure(_coloured(small, DIVERGING_STOPS, -lim, lim),
                                      caption=w("anomaly_caption", w.number(an.get("radius_m"), 1)),
                                      colour_bar=ColourBar(DIVERGING_STOPS, -lim, lim, w("anomaly_c"),
                                                           _decimals_for(-lim, lim)),
                                      metres_per_pixel=_scale(chunk, r, small.shape[1]), north=_georeferenced(chunk)))
            sec.blocks.append(sub)
    return sec


# ---------------------------------------------------------------------------------------- measuring points
POINT_DECIMALS = {"x": 3, "y": 3, "z": 3, "temp_c": 2, "anomaly_c": 2}  # the others (reflectance, indices): 4


def measurement_points(chunk) -> list[dict]:
    """The measuring points of the chunk (``chunk.meta["drawings"]["points"]``)."""
    return [dict(p) for p in ((chunk.meta.get("drawings") or {}).get("points") or []) if "x" in p and "y" in p]


def point_columns(points: list[dict]) -> list[str]:
    """The fields of measuring points: label, x, y, z, temperature, then the bands and indices."""
    keys: list[str] = []
    for p in points:
        for k in p:
            if not str(k).startswith("_") and k not in keys:
                keys.append(k)
    head = [k for k in ("label", "x", "y", "z", "temp_c", "anomaly_c") if k in keys]
    return head + [k for k in keys if k not in head]


def point_header(w: Words, key: str) -> str:
    """The heading of a field of measuring points."""
    if key == "label":
        return w("h_point")
    if key == "temp_c":
        return "T (°C)"
    if key == "anomaly_c":
        return "ΔT (°C)"
    if key.startswith("refl_"):
        return key[5:].upper() if len(key) <= 8 else key[5:]
    return key.upper() if key in ("x", "y", "z") or len(key) <= 5 else key


def _points(chunk, w: Words) -> Section | None:
    points = measurement_points(chunk)
    if not points:
        return None
    sec = Section(w("points_title"))
    cols = point_columns(points)
    rows = []
    for p in points:
        row = []
        for k in cols:
            v = p.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                row.append(w.coordinate(v, 3) if k in ("x", "y") else w.number(v, POINT_DECIMALS.get(k, 4)))
            else:
                row.append("" if v is None else str(v))
        rows.append(row)
    sec.blocks.append(Table([point_header(w, k) for k in cols], rows))
    return sec


# ---------------------------------------------------------------------------------------- the report
def resolve(target, chunks=None):
    """The document and the chunks of a report from a Document or a Chunk and ``chunks``
    (Chunk objects, keys or labels; default: the chunk given, or every enabled chunk)."""
    from ..core.chunk import Chunk

    if isinstance(target, Chunk):
        doc = target.document
        pool = list(doc.chunks) if doc is not None else [target]
        default = [target]
    else:
        doc = target
        pool = list(doc.chunks)
        default = [c for c in pool if c.enabled] or pool
    if chunks is None:
        return doc, default
    out = []
    for ref in chunks:
        found = ref if isinstance(ref, Chunk) else next(
            (c for c in pool if c.label == str(ref) or str(c.key) == str(ref)), None)
        if found is None:
            raise ValueError(f"no chunk {ref!r} in the project")
        out.append(found)
    return doc, out


def _guarded(w: Words, chunk, title: str, fn):
    """A part of the report; one that cannot be read becomes a note, the report goes on."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        log.warning("report: %s of %s not written: %s", title, chunk.label, exc, exc_info=True)
        return Section(title, blocks=[Paragraph(w("part_failed", exc), note=True)])


def _products_of(chunk, w: Words) -> list[str]:
    if chunk.document is None:
        return []
    names = {"point_cloud": "dense_cloud", "model": "model_title", "dem": "step_dem", "orthomosaic": "ortho_title"}
    out = []
    for k, key in names.items():
        p = chunk.product_path(k)
        if p is not None and p.exists():
            out.append(w(key))
    return out


def chunk_sections(chunk, w: Words, index_palette: str = "rdylgn", thermal_palette: str = "iron",
                   progress=None) -> Section:
    """Everything the report says of one chunk: a level-0 section with its parts."""
    from ..core.progress import as_progress

    progress = as_progress(progress)
    top = Section(w("chunk_title", chunk.label), level=0)
    attached = chunk.document is not None

    def raster(product: str, **kw) -> Raster | None:
        path = chunk.product_path(product) if attached else None
        if path is None or not path.exists():
            return None
        try:
            return read_raster(path, **kw)
        except Exception as exc:  # noqa: BLE001
            log.warning("report: %s of %s not read: %s", product, chunk.label, exc)
            return None

    progress(0.0, f"Report of {chunk.label}: rasters")
    dem = raster("dem", bands=[1])
    ortho = raster("orthomosaic", max_cells=4 * FIGURE_SIDE * FIGURE_SIDE)  # RGBA at the size of a figure
    progress(0.15, f"Report of {chunk.label}: alignment")
    try:
        align = alignment_stats(chunk) if attached else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("report: alignment of %s not read: %s", chunk.label, exc)
        align = {}
    parts = [("survey", lambda: _survey(chunk, w, align, dem, ortho)), ("calibration", lambda: _sensors(chunk, w)),
             ("reference", lambda: _reference(chunk, w)), ("parameters", lambda: _parameters(chunk, w))]
    if attached:
        parts += [("dense_cloud", lambda: _dense(chunk, w)), ("model_title", lambda: _model(chunk, w)),
                  ("dem_title", lambda: _dem(chunk, w, dem) if dem is not None else None),
                  ("ortho_title", lambda: _ortho(chunk, w, ortho) if ortho is not None else None),
                  ("ms_title", lambda: _multispectral(chunk, w, index_palette)),
                  ("thermal_title", lambda: _thermal(chunk, w, thermal_palette))]
    parts.append(("points_title", lambda: _points(chunk, w)))
    for i, (key, fn) in enumerate(parts):
        progress(0.2 + 0.8 * i / len(parts), f"Report of {chunk.label}: {key.replace('_title', '').replace('_', ' ')}")
        sec = _guarded(w, chunk, w(key), fn)
        if sec is not None:
            top.blocks.append(sec)
    progress(1.0, f"Report of {chunk.label} done")
    return top


def collect(target, language: str | None = None, chunks=None, title: str | None = None, description: str = "",
            index_palette: str = "rdylgn", thermal_palette: str = "iron", progress=None) -> Report:
    """The content of the report of a :class:`Document` (every enabled chunk) or of a
    :class:`Chunk` (that one); ``chunks`` chooses them (objects, keys or labels)."""
    from .. import __version__
    from ..core import crs as crs_mod
    from ..core.progress import as_progress

    progress = as_progress(progress)
    w = Words(language)
    doc, chosen = resolve(target, chunks)
    path = getattr(doc, "path", None)
    project = path.stem if path is not None else w("unsaved")
    created = datetime.now()
    info = [(w("project"), project), (w("project_file"), str(path) if path is not None else w("unsaved")),
            (w("date"), w.date(created)), (w("version"), f"pyarchinit-photogrammetry {__version__}")]
    site = (getattr(doc, "meta", None) or {}).get("sito")
    if site:
        info.insert(1, (w("site"), str(site)))
    summary = Table([w("h_chunk"), w("h_photos"), w("h_aligned"), w("h_crs"), w("h_products")],
                    [[c.label, w.integer(len(c.cameras)), w.integer(len(c.aligned_cameras)),
                      w("local") if crs_mod.is_local(c.crs) else c.crs, ", ".join(_products_of(c, w)) or "-"]
                     for c in chosen], align=["L", "R", "R", "L", "L"])
    sections = [chunk_sections(c, w, index_palette, thermal_palette, progress.sub(i / len(chosen), 1 / len(chosen)))
                for i, c in enumerate(chosen)]
    return Report(words=w, title=title or w("report_title"), project=project,
                  project_file=str(path) if path is not None else "", created=created, version=__version__,
                  description=description or "", info=KeyValues(info), chunks=summary, sections=sections)


def walk(sections: list[Section]):
    """Every section of a tree, depth first."""
    for s in sections:
        yield s
        yield from walk([b for b in s.blocks if isinstance(b, Section)])
