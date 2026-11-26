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

    ``frame`` ``"crs"``: every height column as written (a number as a float; a text as it is, the caller scrubs it; a
    value that is not finite left out), whatever ``usm_heights`` says.
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
        if x is not None:
            return round(x, 3)
        return value if isinstance(value, str) else None  # a NaN or an infinity is no height a page can show
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


# --- the records of a chunk: what records.json holds and the key a feature carries (design 3.3) ---

UNIT, ELEVATION, VOLUME = "unit", "elevation", "volume"


def _natural(text: str) -> tuple:
    """``"12b"`` as ``((0, 12), (1, "b"))``: numbers in the order of their value, then the rest as text."""
    return tuple((0, int(t)) if t.isdigit() else (1, t) for t in re.split(r"(\d+)", text) if t)


def _unit_key(sito, area, tipo, us) -> tuple[str, str, str, str]:
    return unit_text(sito), unit_text(area), _type(tipo), unit_text(us)


def volume_unit(entry: dict, polygons: list, project_site) -> tuple[str, str | None, str, str] | None:
    """The unit a volume (an entry of ``chunk.meta["proxies"]``) stands for, as ``(sito, area, tipo, us)``, or None
    when it has no record (design 4.2).

    The ``sito`` written in an entry cannot be trusted, so it is never read: the one unit polygon of the chunk
    (``polygons``, :class:`..archaeo.stratigraphy.UnitPolygon`) with the entry's type and number - and its area
    when both have one - gives its site (else the project's), area, type and number; a unit drawn in several
    pieces of one site and area counts as one polygon. With no such polygon (or several that disagree) the entry's
    area, type and number with the site of the project. No number, or no site at all: None."""
    number = unit_text(entry.get("us"))
    if not number:
        return None
    kind, where = _type(entry.get("tipo")), unit_text(entry.get("area"))
    same = [p for p in polygons
            if unit_text(getattr(p, "us", None)) == number and _type(getattr(p, "tipo", None)) == kind
            and (not where or not unit_text(getattr(p, "area", None)) or unit_text(p.area) == where)]
    if len({(unit_text(p.sito), unit_text(p.area)) for p in same}) == 1:  # one polygon, or one unit in pieces
        p = same[0]
        site = unit_text(p.sito) or unit_text(project_site)
        return (site, p.area, p.tipo or "US", p.us) if site else None
    site = unit_text(project_site)
    if not site:
        return None
    return site, entry.get("area"), entry.get("tipo") or "US", entry.get("us")


def chunk_polygons(chunk) -> list:
    """The unit polygons of the plan of ``chunk`` that a site draws (those with a geometry)."""
    from ..archaeo.stratigraphy import UnitPolygon

    out = []
    for d in (chunk.meta.get("drawings") or {}).get("units") or []:
        try:
            u = UnitPolygon.from_dict(d)
        except Exception:  # noqa: BLE001 - a broken record: left out, as the site does
            continue
        if u.geometry is not None and not u.geometry.is_empty:
            out.append(u)
    return out


def chunk_outlines(chunk, sections=None) -> list[tuple[dict, dict]]:
    """``[(section entry, unit record), ...]``: the unit outlines drawn on the sections and elevations of ``chunk``
    (``sections``: their entries, default all of them)."""
    from ..engine.elevation import frame_drawings
    from . import site_sections

    out = []
    for entry in site_sections.entries(chunk) if sections is None else sections:
        for rec in (frame_drawings(chunk, entry["id"]) or {}).get("units") or []:
            if isinstance(rec, dict):
                out.append((entry, rec))
    return out


class ChunkRecords:
    """The records of the units of one chunk, matched once (:func:`chunk_records`): ``records`` (key -> record, as
    ``records.json`` holds them), ``key(sito, area, tipo, us)`` (the key of a unit, a lookup) and ``report`` (one
    entry per unit: ``{"what", "label", "step", "reason", "ignored"}``)."""

    def __init__(self, project_site, records: dict, keys: dict, report: list[dict]):
        self.project_site = unit_text(project_site)
        self.records = records
        self._keys = keys
        self.report = report

    def key(self, sito, area, tipo, us) -> str | None:
        """The key of the record of a unit (a unit without a site takes the project's), None when it has none."""
        return self._keys.get(_unit_key(unit_text(sito) or self.project_site, area, tipo, us))

    @property
    def matched(self) -> int:
        """How many units of the report have a record."""
        return sum(1 for r in self.report if r["step"])


def _relations(row: dict, matched: list[dict], key_of: dict, scrub) -> list[list]:
    """The relations of ``row`` (``rapporti``, :func:`..pyarchinit.bridge.parse_relationships`) as
    ``[kind, us, area, key or None]``: a relation without site or area is within the record's (as PyArchInit writes
    them); the key is set when exactly one matched row of the chunk has that site, area and number."""
    from ..pyarchinit.bridge import parse_relationships

    try:
        relations = parse_relationships(row.get(RELATIONS_COLUMN))
    except Exception:  # noqa: BLE001 - a broken text gives no relation, never a failed site
        return []
    out = []
    for kind, us, area, site in relations:
        site = unit_text(site) or unit_text(row.get("sito"))
        area = unit_text(area) or unit_text(row.get("area"))
        number = unit_text(us)
        if not number:
            continue
        hits = [r for r in matched if unit_text(r.get("sito")) == site and unit_text(r.get("area")) == area
                and unit_text(r.get("us")) == number]
        key = key_of[id(hits[0])] if len(hits) == 1 else None
        out.append([scrub(str(kind)), scrub(number), scrub(area), key])
    return out


def _record(row: dict, columns: list[str], *, frame, dz, fixed, usm_heights, scrub) -> list[dict]:
    """The groups of the record of ``row``: each ``{"name": group id, "fields": [[column, value], ...]}``, the
    columns in the order of their group, an empty value left out, an empty group too."""
    heights = set(ABSOLUTE_HEIGHTS + RELATIVE_HEIGHTS + USM_HEIGHT_COLUMNS)
    groups = list(RECORD_FIELDS) + [PEOPLE_GROUP]
    published = set(columns)
    out = []
    for gid, _title, group_columns in groups:
        fields = []
        for column in group_columns:
            if column not in published:
                continue
            value = row.get(column)
            if column in heights:
                value = height_value(column, value, frame=frame, dz=dz, fixed=fixed, usm_heights=usm_heights)
                value = scrub(value) if isinstance(value, str) else value
            else:
                value = field_value(column, value, scrub)
            if value is not None:
                fields.append([column, value])
        if fields:
            out.append({"name": gid, "fields": fields})
    return out


def chunk_records(chunk, rows: list[dict], project_site, *, frame: str = "local", dz: float = 0.0,
                  fixed: bool = False, usm_heights: str = "none", people: bool = False, scrub=None, volumes=(),
                  units: bool = True, sections=None) -> ChunkRecords:
    """Match the units of ``chunk`` to ``rows`` of ``us_table`` (:func:`match_unit`) and build their records.

    The units are its polygons (when ``units``), the unit outlines of its sections and elevations (``sections``:
    their entries, default all of them) and its volumes (``volumes``: entries of ``chunk.meta["proxies"]``, matched
    by :func:`volume_unit`); a unit without a site takes ``project_site``. Every unit is matched here, once; the
    keys (``u1``, ``u2``...) are numbered in the order (site, area, type, number) of the matched rows, so a polygon,
    its outline and its volume that match one row share a key. ``frame``, ``dz``, ``fixed``, ``usm_heights``: the
    heights (:func:`height_value`); ``people``: the names of people too; ``scrub``: what removes the paths of the
    computer from a text."""
    scrub = scrub or (lambda text: text)
    project = unit_text(project_site)
    found: dict[tuple, Found] = {}
    report: list[dict] = []

    def match(what: str, label: str, sito, area, tipo, us) -> None:
        unit = _unit_key(unit_text(sito) or project, area, tipo, us)
        if unit not in found:
            found[unit] = match_unit(rows, unit[0], area, tipo, us)
        f = found[unit]
        report.append({"what": what, "label": label, "step": f.step, "reason": f.reason,
                       "ignored": list(f.ignored)})

    polygons = chunk_polygons(chunk)
    for p in polygons if units else []:
        match(UNIT, f"{p.tipo or 'US'} {p.us}" if p.us else (p.tipo or "US"), p.sito, p.area, p.tipo, p.us)
    for entry, rec in chunk_outlines(chunk, sections):
        name = str(entry.get("name") or entry.get("id") or "")
        label = f"{rec.get('tipo') or 'US'} {rec.get('us') or '?'}"
        match(ELEVATION, f"{name}: {label}", rec.get("sito"), rec.get("area"), rec.get("tipo"), rec.get("us"))
    for entry in volumes:
        unit = volume_unit(entry, polygons, project)
        label = str(entry.get("name") or f"{entry.get('tipo') or 'US'} {entry.get('us') or '?'}")
        if unit is None:
            reason = NO_NUMBER if not unit_text(entry.get("us")) else NO_SITE
            report.append({"what": VOLUME, "label": label, "step": 0, "reason": reason, "ignored": []})
        else:
            match(VOLUME, label, *unit)

    matched = list({id(f.row): f.row for f in found.values() if f.row is not None}.values())
    matched.sort(key=lambda r: (unit_text(r.get("sito")), unit_text(r.get("area")), _type(r.get("unita_tipo")),
                                _natural(unit_text(r.get("us")))))
    key_of = {id(r): f"u{n}" for n, r in enumerate(matched, 1)}
    keys = {unit: key_of[id(f.row)] for unit, f in found.items() if f.row is not None}
    columns = published_columns(people)
    records = {}
    for row in matched:
        tipo, us = _type(row.get("unita_tipo")), unit_text(row.get("us"))
        definition = field_value("d_stratigrafica", row.get("d_stratigrafica"), scrub)
        title = scrub(f"{tipo} {us} - {definition}" if definition not in (None, "") else f"{tipo} {us}")
        records[key_of[id(row)]] = {
            "title": title,
            "groups": _record(row, columns, frame=frame, dz=dz, fixed=fixed, usm_heights=usm_heights, scrub=scrub),
            "relations": _relations(row, matched, key_of, scrub) if RELATIONS_COLUMN in columns else []}
    return ChunkRecords(project, records, keys, report)


def _document_chunks(document, chunks) -> list:
    """The chunks of ``document`` asked for (objects, keys or labels); None: all of them."""
    if chunks is None:
        return list(document.chunks)
    if not isinstance(chunks, (list, tuple)):
        chunks = [chunks]
    out = []
    for c in chunks:
        ch = c if hasattr(c, "meta") else next(
            (x for x in document.chunks if x.label == c or str(x.key) == str(c)), None)
        if ch is None:
            raise ValueError(f"no chunk {c!r} in the project")
        out.append(ch)
    return out


def unit_sites(document, chunks=None) -> list[str]:
    """The sites whose rows of ``us_table`` the units of ``chunks`` (default all) may stand for, sorted: the site of
    each unit polygon and unit outline of the sections and elevations, else the project's
    (``document.meta["sito"]``), and the project's when a chunk has volumes. An empty site is never one."""
    project = unit_text((document.meta or {}).get("sito"))
    sites: set[str] = set()
    for chunk in _document_chunks(document, chunks):
        for p in chunk_polygons(chunk):
            sites.add(unit_text(p.sito) or project)
        for _entry, rec in chunk_outlines(chunk):
            sites.add(unit_text(rec.get("sito")) or project)
        if chunk.meta.get("proxies"):
            sites.add(project)
    sites.discard("")
    return sorted(sites)


def match_report(document, chunks, rows: list[dict]) -> list[dict]:
    """How the units of ``chunks`` (default all) match ``rows`` of ``us_table`` (:func:`chunk_records`, the
    project's site for a unit without one; the volumes whose file is there, :func:`.site_volumes.present`): the ``report`` of each chunk, every entry with ``"chunk"``, the label of
    its chunk. Matching only: no value of a record is written for it."""
    from . import site_volumes

    project = (document.meta or {}).get("sito")
    out = []
    for chunk in _document_chunks(document, chunks):
        for entry in chunk_records(chunk, rows, project, volumes=site_volumes.present(chunk)).report:
            out.append({"chunk": chunk.label, **entry})
    return out


def summary(report: list[dict]) -> tuple[int, int]:
    """``(matched, total)`` of a :func:`match_report`: the units with a record, all the units."""
    return sum(1 for r in report if r.get("step")), len(report)
