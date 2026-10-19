"""A demo PyArchInit database with the real PyArchInit 5 schema.

``create_demo_database(home)`` writes ``<home>/pyarchinit_DB_folder/demo.sqlite`` (SpatiaLite
when ``mod_spatialite`` is available) with the table definitions of PyArchInit 5
(``pyarchinit5_schema.sql``, branch ``Stratigraph_00001``), the geometry layers registered
in EPSG:3004, and demo content: the site *Saggio dimostrativo* with its stratigraphic units,
documentation, pottery and finds; with ``size="large"`` two more sites (a Roman villa and a
necropolis) with tens of units, unit polygons, spot heights and section traces, to try the
interface against a fuller database. The file is **not** overwritten when it exists (set
``overwrite=True``), and ``config.cfg`` is written only when missing, so an existing
PyArchInit installation keeps its settings. Point PyArchInit (or ``PYARCHINIT_HOME``) at
``<home>`` to open the same database from QGIS.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

SCHEMA_FILE = Path(__file__).with_name("pyarchinit5_schema.sql")

# geometry layers of PyArchInit 5 (table -> type); all in the SRID given to create_demo_database
GEOMETRY_LAYERS = {
    "pyunitastratigrafiche": "MULTIPOLYGON", "pyunitastratigrafiche_usm": "MULTIPOLYGON",
    "pyarchinit_documentazione": "LINESTRING", "pyarchinit_quote": "POINT", "pyarchinit_quote_usm": "POINT",
    "pyarchinit_sezioni": "LINESTRING", "pyarchinit_siti": "POINT", "pyarchinit_siti_polygonal": "POLYGON",
    "pyarchinit_campionature": "POINT", "pyarchinit_individui": "POINT", "pyarchinit_linee_rif": "LINESTRING",
    "pyarchinit_punti_rif": "POINT", "pyarchinit_reperti": "POINT", "pyarchinit_ripartizioni_spaziali": "POLYGON",
    "pyarchinit_sondaggi": "POLYGON", "pyarchinit_strutture_ipotesi": "POLYGON", "pyarchinit_tafonomia": "POINT",
    "pyarchinit_us_negative_doc": "LINESTRING", "pyarchinit_ut_point": "POINT", "pyarchinit_ut_line": "LINESTRING",
    "pyarchinit_ut_polygon": "POLYGON",
}
SPATIAL_LAYERS = tuple((t, GEOMETRY_LAYERS[t]) for t in ("pyunitastratigrafiche", "pyarchinit_documentazione",
                                                        "pyarchinit_quote", "pyarchinit_sezioni"))

DEMO_SITE = "Saggio dimostrativo"
# the synthetic trench of ppg demo lies at EPSG:3004 (2480000, 4500000): 4 x 3 m, origin at the SW corner
TRENCH_X, TRENCH_Y = 2480000.0, 4500000.0

SITES = [
    # sito, nazione, regione, comune, provincia, definizione, descrizione
    (DEMO_SITE, "Italia", "Campania", "Baselice", "BN", "scavo stratigrafico",
     "Saggio dimostrativo di pyarchinit-photogrammetry: trincea 4 x 3 m con un muro e otto target."),
    ("Villa dei Volusii", "Italia", "Lazio", "Fiano Romano", "RM", "villa rustica",
     "Villa romana di eta' repubblicana e imperiale, settore residenziale e pars rustica."),
    ("Necropoli di Colle Mauro", "Italia", "Abruzzo", "Loreto Aprutino", "PE", "necropoli",
     "Necropoli a inumazione di eta' italica, tombe a fossa con corredo."),
]

DEMO_US = [  # area, us, tipo, d_stratigrafica, d_interpretativa, periodo, fase, rapporti
    ("1", "1", "US", "Strato di crollo", "Crollo del muro USM 2", "I", "1", "copre 3"),
    ("1", "2", "USM", "Muro in pietre e malta", "Muro perimetrale", "I", "1", "si appoggia a 3"),
    ("1", "3", "US", "Piano di calpestio", "Battuto pavimentale", "I", "1", "coperto da 1"),
]


def _villa_units() -> list[tuple]:
    out = []
    kinds = [("Strato di humus", "Humus superficiale", "III", "2", "US"),
             ("Strato argilloso compatto", "Livello di abbandono", "II", "2", "US"),
             ("Muro in opera reticolata", "Muro divisorio", "I", "1", "USM"),
             ("Pavimento in cocciopesto", "Piano pavimentale", "I", "2", "US"),
             ("Riempimento di fossa", "Scarico di materiali", "II", "1", "US"),
             ("Strato di crollo di tegole", "Crollo del tetto", "II", "1", "US"),
             ("Muro in opera incerta", "Muro perimetrale", "I", "1", "USM"),
             ("Strato carbonioso", "Focolare", "I", "2", "US"),
             ("Taglio di fossa", "Fossa di spoliazione", "III", "1", "US"),
             ("Strato sabbioso", "Preparazione pavimentale", "I", "2", "US")]
    n = 1
    for area in ("1", "2", "3"):
        for k in range(10):
            d, interp, per, fase, tipo = kinds[(k + int(area)) % len(kinds)]
            rapporti = f"copre {n + 1}" if k < 9 else f"coperto da {n - 1}"
            out.append((area, str(n), tipo, d, interp, per, fase, rapporti))
            n += 1
    return out


def _necropolis_units() -> list[tuple]:
    out = []
    for t in range(1, 21):
        out.append(("1", str(100 + t), "US", f"Riempimento della tomba {t}", "Riempimento di fossa sepolcrale",
                    "I", "1", f"riempie {200 + t}"))
        out.append(("1", str(200 + t), "US", f"Taglio della tomba {t}", "Fossa sepolcrale", "I", "1",
                    f"riempito da {100 + t}"))
    return out


def _polygon(x0, y0, w, h) -> str:
    return (f"MULTIPOLYGON((({x0} {y0}, {x0 + w} {y0}, {x0 + w} {y0 + h}, {x0} {y0 + h}, {x0} {y0})))")


def _load_spatialite(con: sqlite3.Connection) -> bool:
    from .database import _spatialite_paths, pin_spatialite

    try:
        con.enable_load_extension(True)
    except AttributeError:  # pragma: no cover - sqlite3 without extension support
        return False
    for p in _spatialite_paths():
        try:
            con.load_extension(p)
            pin_spatialite(p)
            return True
        except Exception:
            continue
    return False


def create_demo_database(home: str | Path, srid: int = 3004, site: str = DEMO_SITE,
                         us_list=tuple(DEMO_US), size: str = "small", overwrite: bool = False,
                         name: str = "demo.sqlite", demo_content: bool = True) -> Path:
    """Create ``<home>/pyarchinit_DB_folder/<name>`` (and ``config.cfg`` when missing); returns
    the path of ``config.cfg``. ``size="large"`` adds two more sites with many units;
    ``demo_content=False`` writes only the sites and the units (for tests)."""
    from .database import _warm_up_proj

    home = Path(home)
    folder = home / "pyarchinit_DB_folder"
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / name
    cfg = folder / "config.cfg"
    if db.exists() and not overwrite:
        _write_config(cfg, home, name, site, only_if_missing=True)
        return cfg
    if db.exists():
        db.unlink()
    _warm_up_proj()
    con = sqlite3.connect(db)
    spatial = _load_spatialite(con)
    if spatial:
        con.execute("SELECT InitSpatialMetadata(1)")
    con.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    if spatial:
        for table, gtype in GEOMETRY_LAYERS.items():
            con.execute(f"SELECT RecoverGeometryColumn('{table}', 'the_geom', {int(srid)}, '{gtype}', 'XY')")
            con.execute(f"SELECT CreateSpatialIndex('{table}', 'the_geom')")
    today = date.today().isoformat()
    sites = SITES if size == "large" else SITES[:1]
    if site != DEMO_SITE:
        sites = [(site,) + SITES[0][1:]] + list(sites[1:])
    for i, (sito, naz, reg, com, prov, defn, desc) in enumerate(sites, 1):
        con.execute("INSERT INTO site_table (id_sito, sito, nazione, regione, comune, provincia, definizione_sito, "
                    "descrizione, find_check) VALUES (?,?,?,?,?,?,?,?,0)", (i, sito, naz, reg, com, prov, defn, desc))
    units = [(sites[0][0],) + u for u in us_list]
    if size == "large":
        units += [(SITES[1][0],) + u for u in _villa_units()] + [(SITES[2][0],) + u for u in _necropolis_units()]
    from .bridge import RELATION_WORDS, format_relationships

    def rel(text: str, sito: str, area: str) -> str:  # "copre 3" -> PyArchInit [['Copre', '3', area, sito]]
        words, num = text.rsplit(" ", 1) if " " in text else (text, "")
        return format_relationships([[RELATION_WORDS.get(words.lower(), words.capitalize()), num, area, sito]])

    for i, (sito, area, us, tipo, d_strat, d_int, per, fase, rapp) in enumerate(units, 1):
        rapp = rel(rapp, sito, area) if rapp else ""
        con.execute("INSERT INTO us_table (id_us, sito, area, us, unita_tipo, d_stratigrafica, d_interpretativa, "
                    "periodo_iniziale, fase_iniziale, periodo_finale, fase_finale, rapporti, anno_scavo, "
                    "schedatore, data_schedatura, scavato, metodo_di_scavo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, sito, area, us, tipo, d_strat, d_int, per, fase, per, fase, rapp, "2025",
                     "E. Cocca", today, "Si", "Stratigrafico"))
    main = sites[0][0]
    if not demo_content:
        con.commit()
        con.close()
        _write_config(cfg, home, name, site, only_if_missing=True)
        return cfg
    # documentation, pottery and finds of the demo site
    docs = [("Pianta di fine scavo", "Pianta", "Rilievo diretto", "1:20"), ("Sezione A-A'", "Sezione", "Rilievo diretto", "1:20"),
            ("Foto zenitale", "Foto", "Fotografia", ""), ("Matrix", "Matrix", "Elaborazione", "")]
    for i, (nome, tipo, sorg, scala) in enumerate(docs, 1):
        con.execute("INSERT INTO documentazione_table (id_documentazione, sito, nome_doc, data, tipo_documentazione, "
                    "sorgente, scala, disegnatore, note) VALUES (?,?,?,?,?,?,?,?,?)",
                    (i, main, nome, today, tipo, sorg, scala, "E. Cocca", "Documentazione dimostrativa"))
    # form, part, shape and ware as in PyArchInit's thesaurus (11.4, 11.5, 11.12, 11.6); diameters and height in cm
    pottery = [("1", "1", "Aperta", "Orlo", "Ciotola", "Media", 16.0, 16.8, None, None, "5-25"),
               ("1", "1", "Chiusa", "Parete", "Giara", "Grossa", None, 31.0, None, None, "<5"),
               ("1", "3", "Aperta", "Fondo", "Piatto", "Fine", None, None, 8.0, None, "5-25"),
               ("1", "3", "Chiusa", "Ansa", "Anfora", "Media", None, None, None, None, "<5"),
               ("1", "1", "Aperta", "Orlo", "Tazza", "Fine", 12.0, 12.4, 5.0, 7.5, ">50"),
               ("1", "2", "Chiusa", "Parete", "Pithos", "Grossa", None, 64.0, None, None, "<5")]
    pcols = _columns(con, "pottery_table")
    for i, (area, us, form, part, shape, ware, rim, dmax, base, height, pct) in enumerate(pottery, 1):
        row = {"id_rep": i, "id_number": i, "sito": main, "area": area, "us": us, "form": form,
               "specific_form": part, "specific_shape": shape, "ware": ware, "diametro_rim": rim,
               "diametro_max": dmax, "diametro_bottom": base, "diametro_height": height, "percent": pct, "anno": 2025,
               "qty": 1, "material": "Ceramica", "fabric": "depurata", "note": "frammento dimostrativo"}
        _insert(con, "pottery_table", {k: v for k, v in row.items() if k in pcols})
    icols = _columns(con, "inventario_materiali_table")
    finds = [("1", "1", "metallo", "chiodo", 3), ("1", "1", "vetro", "frammento di bicchiere", 1),
             ("1", "3", "osso", "ago", 1), ("1", "2", "pietra", "macina", 1)]
    for i, (area, us, tipo, defn, n) in enumerate(finds, 1):
        row = {"id_invmat": i, "sito": main, "numero_inventario": i, "tipo_reperto": tipo, "definizione": defn,
               "descrizione": f"{defn} dallo strato US {us}", "area": area, "us": us, "nr_cassa": 1,
               "stato_conservazione": "frammentario", "datazione_reperto": "I sec. d.C."}
        _insert(con, "inventario_materiali_table", {k: v for k, v in row.items() if k in icols})
    if spatial:
        _demo_geometries(con, main, units, srid, today)
    con.commit()
    con.close()
    _write_config(cfg, home, name, site, only_if_missing=True)
    return cfg


def _demo_geometries(con, main: str, units, srid: int, today: str) -> None:
    """Unit polygons inside the trench, spot heights and a section trace (EPSG:3004 metres)."""
    x, y = TRENCH_X, TRENCH_Y
    # the trench of the synthetic scene spans x 1..3, y 0.9..2.1; the wall x 1.7..2.3, y 1.3..1.5
    polys = {"1": _polygon(x + 1.05, y + 1.55, 1.9, 0.5), "2": _polygon(x + 1.7, y + 1.3, 0.6, 0.2),
             "3": _polygon(x + 1.05, y + 0.95, 1.9, 0.3)}
    for area, us, tipo, *_ in [u[1:] for u in units if u[0] == main]:
        wkt = polys.get(us)
        if wkt is None:
            continue
        con.execute("INSERT INTO pyunitastratigrafiche (area_s, scavo_s, us_s, stratigraph_index_us, tipo_us_s, "
                    "rilievo_originale, disegnatore, data, tipo_doc, nome_doc, unita_tipo_s, the_geom) "
                    f"VALUES (?,?,?,?,?,?,?,?,?,?,?, GeomFromText(?, {srid}))",
                    (area, main, us, int(us), "", "Rilievo diretto", "E. Cocca", today, "Pianta",
                     "Pianta di fine scavo", tipo, wkt))
    for k, (dx, dy, q) in enumerate([(1.2, 1.0, 99.65), (2.8, 1.0, 99.66), (2.0, 1.4, 99.95), (1.2, 2.0, 99.64)]):
        con.execute("INSERT INTO pyarchinit_quote (sito_q, area_q, us_q, unita_misu_q, quota_q, data, disegnatore, "
                    f"rilievo_originale, unita_tipo_q, the_geom) VALUES (?,?,?,?,?,?,?,?,?, GeomFromText(?, {srid}))",
                    (main, "1", "3" if q < 99.9 else "2", "m s.l.m.", q, today, "E. Cocca", "Rilievo diretto",
                     "US" if q < 99.9 else "USM", f"POINT({x + dx} {y + dy})"))
    con.execute("INSERT INTO pyarchinit_sezioni (id_sezione, sito, area, descr, tipo_doc, nome_doc, the_geom) "
                f"VALUES (?,?,?,?,?,?, GeomFromText(?, {srid}))",
                ("A-A'", main, 1, "Sezione trasversale del saggio", "Sezione", "Sezione A-A'",
                 f"LINESTRING({x + 1.0} {y + 1.5}, {x + 3.0} {y + 1.5})"))


def _columns(con, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _insert(con, table: str, row: dict) -> None:
    cols = ", ".join(row)
    con.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


def _write_config(cfg: Path, home: Path, name: str, site: str, only_if_missing: bool = True) -> None:
    if cfg.exists() and only_if_missing:
        return
    thumb = home / "pyarchinit_Thumbnails_folder"
    resize = home / "pyarchinit_Resize_folder"
    thumb.mkdir(parents=True, exist_ok=True)
    resize.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        f"{{'DATABASE': '{name}', 'SERVER': 'sqlite', 'HOST': '', 'USER': '', 'PASSWORD': '', 'PORT': '', "
        f"'THUMB_PATH': '{thumb.as_posix()}/', 'THUMB_RESIZE': '{resize.as_posix()}/', "
        f"'EXPERIMENTAL': 'No', 'SITE_SET': '{site}', 'LOGO': '', 'SSLMODE': 'allow'}}", encoding="utf-8")


# kept for callers of the previous version
SCHEMA = SCHEMA_FILE.read_text(encoding="utf-8") if SCHEMA_FILE.exists() else ""

__all__ = ["create_demo_database", "DEMO_SITE", "GEOMETRY_LAYERS", "SPATIAL_LAYERS", "SCHEMA_FILE", "SCHEMA"]
