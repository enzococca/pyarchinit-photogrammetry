"""The records of the stratigraphic units on a web site (:mod:`.site`): which columns of PyArchInit's ``us_table``
a site publishes and how their values are written.

A record is the row of ``us_table`` that a unit of the project (a polygon of the plan, an elevation outline, a
volume) stands for: the visitor who clicks the unit reads its record in the side panel, grouped as the US form of
PyArchInit groups it, and moves along its relations to the other units of the site.

Every column of the bundled schema (``pyarchinit5_schema.sql``, 131 columns with ``id_us``) is in exactly one of
three sets:

* published (101) - what describes the unit: identification, definitions, place, texts, chronology, formation,
  excavation, measures, heights, relations and the masonry tab (``RECORD_FIELDS`` and ``RELATIONS_COLUMN``);
* people (3) - ``schedatore``, ``direttore_us``, ``responsabile_us``: names of other people on a public page,
  published only when whoever publishes asks for them (``PEOPLE_COLUMNS``);
* never (27) - system and administrative columns, accounts, and the lists of documents and places that mean
  nothing outside the database (``NEVER_COLUMNS``).

A column that the database has and these sets do not know (a newer PyArchInit) is never published.

Values: a free text goes out as written once the paths of the computer are scrubbed; a text that reads as a
Python list (PyArchInit writes its table widgets so) goes out as plain text; heights follow the frame of the site
(:func:`height_value`): in a local frame the absolute ones are lowered by the base of the heights of the site, as
the DEM is, and left out when nothing fixes that base, so that the page never tells the true heights.
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass
from decimal import Decimal

RECORD_FIELDS: tuple[tuple[str, dict[str, str], tuple[str, ...]], ...] = (
    ("identification", {"it": "Identificazione", "en": "Identification"},
     ("sito", "area", "us", "unita_tipo")),
    ("definitions", {"it": "Definizione", "en": "Definition"},
     ("d_stratigrafica", "d_interpretativa")),
    ("place", {"it": "Localizzazione", "en": "Location"},
     ("settore", "quad_par", "ambient", "saggio", "struttura", "attivita")),
    ("texts", {"it": "Descrizione", "en": "Description"},
     ("descrizione", "interpretazione", "osservazioni")),
    ("chronology", {"it": "Cronologia", "en": "Chronology"},
     ("periodo_iniziale", "fase_iniziale", "periodo_finale", "fase_finale", "datazione", "elem_datanti",
      "affidabilita")),
    ("formation", {"it": "Formazione e caratteri", "en": "Formation and features"},
     ("formazione", "modo_formazione", "posizione", "criteri_distinzione", "stato_di_conservazione", "colore",
      "consistenza", "inclusi", "campioni", "componenti_organici", "componenti_inorganici", "organici",
      "inorganici", "quantificazioni")),
    ("excavation", {"it": "Scavo", "en": "Excavation"},
     ("scavato", "metodo_di_scavo", "anno_scavo", "flottazione", "setacciatura")),
    ("measures", {"it": "Misure", "en": "Measures"},
     ("lunghezza_max", "larghezza_media", "altezza_max", "altezza_min", "profondita_max", "profondita_min")),
    ("heights", {"it": "Quote", "en": "Heights"},
     ("quota_abs", "quota_max_abs", "quota_min_abs", "quota_relativa", "quota_max_rel", "quota_min_rel",
      "quota_min_usm", "quota_max_usm")),
    ("masonry", {"it": "Muratura", "en": "Masonry"},
     ("funz_statica", "lavorazione", "spess_giunti", "letti_posa", "alt_mod", "un_ed_riass", "reimp", "posa_opera",
      "cons_legante", "col_legante", "aggreg_legante", "con_text_mat", "col_materiale", "inclusi_materiali_usm",
      "lunghezza_usm", "altezza_usm", "spessore_usm", "tecnica_muraria_usm", "modulo_usm", "campioni_malta_usm",
      "campioni_mattone_usm", "campioni_pietra_usm", "provenienza_materiali_usm", "criteri_distinzione_usm",
      "uso_primario_usm", "tipologia_opera", "sezione_muraria", "superficie_analizzata", "orientamento",
      "materiali_lat", "lavorazione_lat", "consistenza_lat", "forma_lat", "colore_lat", "impasto_lat", "forma_p",
      "colore_p", "taglio_p", "posa_opera_p", "inerti_usm", "tipo_legante_usm", "rifinitura_usm", "materiale_p",
      "consistenza_p", "unita_edilizie")),
)
RELATIONS_COLUMN = "rapporti"  # published as the relations of the record (links to the other units), not a field
PEOPLE_COLUMNS = ("schedatore", "direttore_us", "responsabile_us")
PEOPLE_GROUP = ("people", {"it": "Persone", "en": "People"}, PEOPLE_COLUMNS)
NEVER_COLUMNS = (
    "id_us", "cont_per", "order_layer", "documentazione", "doc_usv", "sing_doc", "n_catalogo_generale",
    "n_catalogo_interno", "n_catalogo_internazionale", "soprintendenza", "ref_tm", "ref_ra", "ref_n",
    "cod_ente_schedatore", "data_schedatura", "data_rilevazione", "data_rielaborazione", "version_number",
    "editing_by", "editing_since", "last_modified_by", "last_modified_timestamp", "audit_trail", "entity_uuid",
    "other_locations", "node_uuid", "rapporti2",
)

# The labels of PyArchInit's US form (tabs/US_USM.py and its form, branch Stratigraph_00001), written out where the
# form abbreviates; the site, area and unit as the site already names them (site.FIELDS).
COLUMN_TITLES: dict[str, dict[str, str]] = {
    # identification
    "sito": {"it": "Sito", "en": "Site"},
    "area": {"it": "Area", "en": "Area"},
    "us": {"it": "US", "en": "Unit"},
    "unita_tipo": {"it": "Tipo unità", "en": "Unit type"},
    # definitions
    "d_stratigrafica": {"it": "Definizione stratigrafica", "en": "Stratigraphic definition"},
    "d_interpretativa": {"it": "Definizione interpretativa", "en": "Interpretative definition"},
    # place
    "settore": {"it": "Settore", "en": "Sector"},
    "quad_par": {"it": "Quadrato-Parete", "en": "Square-profile"},
    "ambient": {"it": "Ambiente", "en": "Room"},
    "saggio": {"it": "Saggio", "en": "Trench"},
    "struttura": {"it": "Sigla struttura", "en": "Structure code"},
    "attivita": {"it": "Attività", "en": "Activity"},
    # texts
    "descrizione": {"it": "Descrizione", "en": "Description"},
    "interpretazione": {"it": "Interpretazione", "en": "Interpretation"},
    "osservazioni": {"it": "Osservazioni", "en": "Observations"},
    # chronology
    "periodo_iniziale": {"it": "Periodo iniziale", "en": "Initial period"},
    "fase_iniziale": {"it": "Fase iniziale", "en": "Initial phase"},
    "periodo_finale": {"it": "Periodo finale", "en": "Final period"},
    "fase_finale": {"it": "Fase finale", "en": "Final phase"},
    "datazione": {"it": "Datazione", "en": "Dating"},
    "elem_datanti": {"it": "Elementi datanti", "en": "Dating elements"},
    "affidabilita": {"it": "Affidabilità", "en": "Reliability"},
    # formation and features
    "formazione": {"it": "Formazione", "en": "Formation"},
    "modo_formazione": {"it": "Modo di formazione", "en": "Formation process"},
    "posizione": {"it": "Posizione", "en": "Position"},
    "criteri_distinzione": {"it": "Criteri di distinzione", "en": "Distinction criteria"},
    "stato_di_conservazione": {"it": "Stato di conservazione", "en": "State of preservation"},
    "colore": {"it": "Colore", "en": "Colour"},
    "consistenza": {"it": "Consistenza", "en": "Consistency"},
    "inclusi": {"it": "Inclusi", "en": "Inclusions"},
    "campioni": {"it": "Campioni", "en": "Samples"},
    "componenti_organici": {"it": "Componenti organici", "en": "Organic components"},
    "componenti_inorganici": {"it": "Componenti inorganici", "en": "Inorganic components"},
    "organici": {"it": "Organici", "en": "Organic"},
    "inorganici": {"it": "Inorganici", "en": "Inorganic"},
    "quantificazioni": {"it": "Quantificazioni", "en": "Quantifications"},
    # excavation
    "scavato": {"it": "Scavato", "en": "Excavated"},
    "metodo_di_scavo": {"it": "Metodo di scavo", "en": "Excavation method"},
    "anno_scavo": {"it": "Anno di scavo", "en": "Year of excavation"},
    "flottazione": {"it": "Flottazione", "en": "Flotation"},
    "setacciatura": {"it": "Setacciatura", "en": "Sieving"},
    # measures
    "lunghezza_max": {"it": "Lunghezza massima", "en": "Maximum length"},
    "larghezza_media": {"it": "Larghezza media", "en": "Average width"},
    "altezza_max": {"it": "Altezza massima", "en": "Maximum height"},
    "altezza_min": {"it": "Altezza minima", "en": "Minimum height"},
    "profondita_max": {"it": "Profondità massima", "en": "Maximum depth"},
    "profondita_min": {"it": "Profondità minima", "en": "Minimum depth"},
    # heights
    "quota_abs": {"it": "Quota assoluta", "en": "Absolute elevation"},
    "quota_max_abs": {"it": "Quota massima assoluta", "en": "Maximum absolute elevation"},
    "quota_min_abs": {"it": "Quota minima assoluta", "en": "Minimum absolute elevation"},
    "quota_relativa": {"it": "Quota relativa", "en": "Relative elevation"},
    "quota_max_rel": {"it": "Quota massima relativa", "en": "Maximum relative elevation"},
    "quota_min_rel": {"it": "Quota minima relativa", "en": "Minimum relative elevation"},
    "quota_min_usm": {"it": "Quota minima USM", "en": "Minimum elevation of the masonry unit"},
    "quota_max_usm": {"it": "Quota massima USM", "en": "Maximum elevation of the masonry unit"},
    # masonry
    "funz_statica": {"it": "Funzione statica", "en": "Static function"},
    "lavorazione": {"it": "Lavorazione", "en": "Working"},
    "spess_giunti": {"it": "Spessore giunti", "en": "Joint thickness"},
    "letti_posa": {"it": "Letti di posa", "en": "Bedding courses"},
    "alt_mod": {"it": "Altezza modulo", "en": "Module height"},
    "un_ed_riass": {"it": "Unità edilizia riassuntiva", "en": "Summary building unit"},
    "reimp": {"it": "Reimpiego", "en": "Reuse"},
    "posa_opera": {"it": "Posa in opera", "en": "Laying"},
    "cons_legante": {"it": "Consistenza legante", "en": "Binder consistency"},
    "col_legante": {"it": "Colore legante", "en": "Binder colour"},
    "aggreg_legante": {"it": "Aggregati legante", "en": "Binder aggregates"},
    "con_text_mat": {"it": "Consistenza-Texture", "en": "Consistency-Texture"},
    "col_materiale": {"it": "Colore materiale", "en": "Material colour"},
    "inclusi_materiali_usm": {"it": "Inclusi materiali USM", "en": "Inclusions in the materials"},
    "lunghezza_usm": {"it": "Lunghezza USM", "en": "Length of the masonry unit"},
    "altezza_usm": {"it": "Altezza USM", "en": "Height of the masonry unit"},
    "spessore_usm": {"it": "Spessore USM", "en": "Thickness of the masonry unit"},
    "tecnica_muraria_usm": {"it": "Tecnica muraria", "en": "Masonry technique"},
    "modulo_usm": {"it": "Modulo", "en": "Module"},
    "campioni_malta_usm": {"it": "Campioni malta", "en": "Mortar samples"},
    "campioni_mattone_usm": {"it": "Campioni mattone", "en": "Brick samples"},
    "campioni_pietra_usm": {"it": "Campioni pietra", "en": "Stone samples"},
    "provenienza_materiali_usm": {"it": "Provenienza materiali", "en": "Source of the materials"},
    "criteri_distinzione_usm": {"it": "Criteri di distinzione USM", "en": "Distinction criteria of the masonry unit"},
    "uso_primario_usm": {"it": "Uso primario", "en": "Primary use"},
    "tipologia_opera": {"it": "Tipologia dell'opera", "en": "Type of work"},
    "sezione_muraria": {"it": "Sezione muraria", "en": "Wall section"},
    "superficie_analizzata": {"it": "Superficie analizzata", "en": "Surface analysed"},
    "orientamento": {"it": "Orientamento", "en": "Orientation"},
    "materiali_lat": {"it": "Materiali (laterizi)", "en": "Materials (bricks)"},
    "lavorazione_lat": {"it": "Lavorazione (laterizi)", "en": "Working (bricks)"},
    "consistenza_lat": {"it": "Consistenza (laterizi)", "en": "Consistency (bricks)"},
    "forma_lat": {"it": "Forma (laterizi)", "en": "Shape (bricks)"},
    "colore_lat": {"it": "Colore (laterizi)", "en": "Colour (bricks)"},
    "impasto_lat": {"it": "Impasto (laterizi)", "en": "Fabric (bricks)"},
    "forma_p": {"it": "Forma (pietre)", "en": "Shape (stones)"},
    "colore_p": {"it": "Colore (pietre)", "en": "Colour (stones)"},
    "taglio_p": {"it": "Taglio (pietre)", "en": "Cut (stones)"},
    "posa_opera_p": {"it": "Posa in opera (pietre)", "en": "Laying (stones)"},
    "inerti_usm": {"it": "Inerti", "en": "Aggregates"},
    "tipo_legante_usm": {"it": "Tipo legante", "en": "Binder type"},
    "rifinitura_usm": {"it": "Rifinitura", "en": "Finishing"},
    "materiale_p": {"it": "Materiale (pietre)", "en": "Material (stones)"},
    "consistenza_p": {"it": "Consistenza (pietre)", "en": "Consistency (stones)"},
    "unita_edilizie": {"it": "Unità edilizie", "en": "Building units"},
    # people
    "schedatore": {"it": "Schedatore", "en": "Recorder"},
    "direttore_us": {"it": "Direttore", "en": "Director"},
    "responsabile_us": {"it": "Responsabile", "en": "Supervisor"},
}

ABSOLUTE_HEIGHTS = ("quota_abs", "quota_max_abs", "quota_min_abs")
RELATIVE_HEIGHTS = ("quota_relativa", "quota_max_rel", "quota_min_rel")
USM_HEIGHT_COLUMNS = ("quota_min_usm", "quota_max_usm")
USM_HEIGHTS = ("none", "relative", "absolute")  # what the heights of a masonry unit are: the choice of who publishes

_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


def published_columns(people: bool = False) -> list[str]:
    """The columns a site publishes, in the order of their groups, then the relations (and the names of people
    when ``people``)."""
    cols = [c for _gid, _title, columns in RECORD_FIELDS for c in columns] + [RELATIONS_COLUMN]
    return cols + list(PEOPLE_COLUMNS) if people else cols


def list_text(value) -> str | None:
    """A value that PyArchInit wrote as a list of rows (the text of a Python list, ``ast.literal_eval`` reads it)
    as plain text: the cells of a row joined by a space, the rows by ``"; "``; ``""`` for an empty list. None when
    the value is not such a list."""
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("["):
            return None
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return None
    if not isinstance(value, (list, tuple)):
        return None
    rows = []
    for row in value:
        cells = row if isinstance(row, (list, tuple)) else [row]
        line = " ".join(str(c).strip() for c in cells if c is not None and str(c).strip())
        if line:
            rows.append(line)
    return "; ".join(rows)


def _number(value) -> float | None:
    """``value`` as a number: an int, a float, a Decimal or a text that is only a number with ``,`` or ``.`` as
    its decimal sign. None for anything else (a bool, a text with a unit, a value that is not finite)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        x = float(value)
    elif isinstance(value, str) and _NUMBER.fullmatch(value.strip()):
        x = float(value.strip().replace(",", "."))
    else:
        return None
    return x if math.isfinite(x) else None


def height_value(column: str, value, *, frame: str, dz: float, fixed: bool,
                 usm_heights: str) -> float | str | None:
    """The height ``value`` of ``column`` as the site publishes it, or None to leave it out.

    ``frame`` ``"crs"``: every height column as written (a number as a float), whatever ``usm_heights`` says.
    ``frame`` ``"local"``: the absolute heights (and those of a masonry unit when ``usm_heights`` is
    ``"absolute"``) lowered by ``dz``, the base of the heights of the site, and left out when nothing fixes it
    (``fixed`` false); the relative ones (and those of a masonry unit when ``usm_heights`` is ``"relative"``) as
    written; those of a masonry unit left out when ``usm_heights`` is ``"none"``. In a local frame a value that
    is not a number is left out: a text such as ``"245,30 m s.l.m."`` cannot be lowered and could tell the true
    height. Numbers are rounded to the millimetre."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    x = _number(value)
    if frame == "crs":
        return round(x, 3) if x is not None else value
    if column in USM_HEIGHT_COLUMNS:
        if usm_heights not in ("relative", "absolute"):
            return None
        absolute = usm_heights == "absolute"
    else:
        absolute = column in ABSOLUTE_HEIGHTS
    if x is None:
        return None
    if absolute:
        return round(x - dz, 3) if fixed else None
    return round(x, 3)


def field_value(column: str, value, scrub) -> str | int | float | None:
    """The value of a published ``column`` that is not a height: None for an empty value (None, a blank text, an
    empty list); a list of rows (:func:`list_text`) as its text; a text through ``scrub`` (which removes the paths
    of the computer); a number as it is (a Decimal as a float)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        x = float(value)
        return x if math.isfinite(x) else None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (int, float)):
        return value
    listed = list_text(value)
    if listed is not None:
        return scrub(listed) if listed else None
    text = value if isinstance(value, str) else str(value)
    return scrub(text) if text.strip() else None


# --- matching a unit of the project to its row of us_table (design 3.2) ---

NO_NUMBER = "no number"
NO_SITE = "no site"
NO_ROW = "no row"
SEVERAL = "several rows"


@dataclass(frozen=True)
class Found:
    """What matching a unit gave: the row (None when there is none), the step that found it (0 none, 1 area and
    type, 2 area only, 3 every area), the reason when there is no row and what the step ignored."""
    row: dict | None
    step: int
    reason: str
    ignored: tuple[str, ...] = ()


def unit_text(value) -> str:
    """A site, area, type or number as the matching compares it: trimmed text, ``""`` for None."""
    return "" if value is None else str(value).strip()


def _type(value) -> str:
    return (unit_text(value) or "US").upper()  # a unit is "US" until someone sets its type


def match_unit(rows: list[dict], sito, area, tipo, us) -> Found:
    """The row of ``us_table`` a unit of the project stands for, or why there is none.

    ``rows`` are rows of ``us_table`` of any sites (dicts with ``sito``, ``area``, ``us``, ``unita_tipo``);
    ``tipo`` None means "US". The site is required, so that a unit without one never takes another site's row;
    the number is compared as trimmed text. Among the rows of the site with the number: those with the unit's
    area and type (step 1), else those with its area (step 2); a unit without an area looks on every area, with
    its type, then without (step 3). A step gives the row only when exactly one is left: several end the search
    (``SEVERAL``), never a guess on a looser step.
    """
    site = unit_text(sito)
    if not site:
        return Found(None, 0, NO_SITE)
    number = unit_text(us)
    if not number:
        return Found(None, 0, NO_NUMBER)
    same = [r for r in rows if unit_text(r.get("sito")) == site and unit_text(r.get("us")) == number]
    kind, where = _type(tipo), unit_text(area)
    if where:
        here = [r for r in same if unit_text(r.get("area")) == where]
        steps = (([r for r in here if _type(r.get("unita_tipo")) == kind], 1, ()), (here, 2, ("type",)))
    else:
        steps = (([r for r in same if _type(r.get("unita_tipo")) == kind], 3, ("area",)),
                 (same, 3, ("area", "type")))
    for found, step, ignored in steps:
        if len(found) == 1:
            return Found(found[0], step, "", ignored)
        if found:
            return Found(None, 0, SEVERAL)
    return Found(None, 0, NO_ROW)
