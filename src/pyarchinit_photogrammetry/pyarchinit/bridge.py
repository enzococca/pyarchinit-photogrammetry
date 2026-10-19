"""High-level PyArchInit bridge: read archaeological records, publish
photogrammetric products (orthophotos, DEMs, models, sections, units, volumes).

Example::

    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    pa = PyArchInit.connect()                 # database configured in PyArchInit
    pa.sites()
    pa.publish_orthomosaic(chunk, sito="Lagoi", nome_doc="Ortofoto saggio 1",
                           link_us=[("1", "101"), ("1", "102")])
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np

from ..core import crs as crs_mod
from ..core import paradata
from .database import PyArchInitDB, connect, reproject_wkt
from .media import register_media

US_FIELDS = ["sito", "area", "us", "unita_tipo", "d_stratigrafica", "d_interpretativa", "descrizione",
             "interpretazione", "periodo_iniziale", "fase_iniziale", "periodo_finale", "fase_finale",
             "anno_scavo", "schedatore", "rapporti", "quota_min_usm", "quota_max_usm"]
POTTERY_FIELDS = ["id_rep", "id_number", "sito", "area", "us", "form", "specific_form", "specific_shape", "ware",
                  "fabric", "datazione", "percent", "drawing"]
POTTERY_WEIGHTS = {"diametro_rim": 2.0, "diametro_max": 1.0, "diametro_bottom": 1.0, "diametro_height": 1.0}
UNKNOWN_SHAPE = 0.1  # the difference of shape given to a form saved without its profile


class PyArchInit:
    def __init__(self, db: PyArchInitDB, media_root: str | Path | None = None):
        self.db = db
        self.media_root = Path(media_root) if media_root else None

    @classmethod
    def connect(cls, url: str | None = None, config_path: str | Path | None = None,
                media_root: str | Path | None = None) -> "PyArchInit":
        """``url``: SQLite path or SQLAlchemy URL; ``config_path``: a PyArchInit ``config.cfg``;
        ``media_root``: where media, thumbnails and resized copies go (default: the folders
        of ``config.cfg``, as PyArchInit itself)."""
        return cls(connect(url, config_path), media_root=media_root)

    @property
    def label(self) -> str:
        cfg = self.db.config
        if self.db.is_postgres:
            return self.db.url.split("@")[-1].split("?")[0]
        return str(cfg.sqlite_path) if cfg and not self.db.url.startswith("sqlite") else self.db.url.replace("sqlite:///", "")

    # ------------------------------------------------------------------ sites
    def site_exists(self, sito: str) -> bool:
        return bool(self.db.query("site_table", sito=sito))

    def ensure_site(self, sito: str, nazione: str = "", regione: str = "", comune: str = "", provincia: str = "",
                    descrizione: str = "", definizione_sito: str = "") -> int:
        """Return the id of the site record, creating it when the site is not in ``site_table``
        (PyArchInit shows a site only when its *scheda Sito* exists)."""
        rows = self.db.query("site_table", sito=sito)
        if rows:
            return int(rows[0]["id_sito"])
        values = {"sito": sito, "nazione": nazione, "regione": regione, "comune": comune, "provincia": provincia,
                  "descrizione": descrizione or "Creato da pyarchinit-photogrammetry",
                  "definizione_sito": definizione_sito, "find_check": 0}
        cols = self.db.columns("site_table")
        values = {k: v for k, v in values.items() if k in cols}
        with self.db.begin() as conn:
            return int(self.db.insert(conn, "site_table", values, id_column="id_sito"))

    # ------------------------------------------------------------------ read
    def sites(self) -> list[dict]:
        return self.db.query("site_table")

    def stratigraphic_units(self, sito: str, area: str | None = None, us: str | None = None) -> list[dict]:
        return self.db.query("us_table", sito=sito, area=area, us=us)

    def us_id(self, sito: str, area: str, us: str, unita_tipo: str | None = None) -> int:
        rows = self.db.query("us_table", sito=sito, area=str(area), us=str(us), unita_tipo=unita_tipo)
        if not rows:
            raise KeyError(f"US {us} (area {area}) not found in site {sito}")
        return int(rows[0]["id_us"])

    # ------------------------------------------------------------------ US form
    RELATIONS = ["Copre", "Coperto da", "Taglia", "Tagliato da", "Riempie", "Riempito da", "Si appoggia a",
                 "Gli si appoggia", "Si lega a", "Uguale a", "Anteriore a", "Posteriore a"]
    INVERSE = {"Copre": "Coperto da", "Coperto da": "Copre", "Taglia": "Tagliato da", "Tagliato da": "Taglia",
               "Riempie": "Riempito da", "Riempito da": "Riempie", "Si appoggia a": "Gli si appoggia",
               "Gli si appoggia": "Si appoggia a", "Si lega a": "Si lega a", "Uguale a": "Uguale a",
               "Anteriore a": "Posteriore a", "Posteriore a": "Anteriore a"}

    def us_record(self, sito: str, area: str, us: str, unita_tipo: str | None = None) -> dict | None:
        rows = self.db.query("us_table", sito=sito, area=str(area), us=str(us), unita_tipo=unita_tipo)
        return rows[0] if rows else None

    def us_columns(self) -> set[str]:
        return self.db.columns("us_table")

    def missing_us_columns(self) -> list[str]:
        """Columns of the PyArchInit 5 US form that this database lacks (older or reduced
        ``us_table``, e.g. the demo database of version 0.2 of this program)."""
        have = {c.lower() for c in self.us_columns()}
        return [n for n, _ in reference_us_columns() if n.lower() not in have]

    def upgrade_us_table(self, backup: bool = True) -> dict:
        """Add the missing PyArchInit 5 columns to ``us_table``, as PyArchInit's own updater
        does: a copy of a SQLite file first (``<file>.backup_<date>_<time>``), then
        ``ALTER TABLE ... ADD COLUMN``. Records are not touched. Returns ``{"added", "backup"}``."""
        missing = set(self.missing_us_columns())
        if not missing:
            return {"added": [], "backup": None}
        bak = None
        path = self.db.sqlite_path
        if backup and path is not None and path.exists():
            import sqlite3
            from datetime import datetime

            bak = path.with_name(f"{path.name}.backup_{datetime.now():%Y%m%d_%H%M%S}")
            src, dst = sqlite3.connect(str(path)), sqlite3.connect(str(bak))
            try:
                src.backup(dst)  # consistent copy, WAL included
            finally:
                dst.close()
                src.close()
        added = self.db.add_columns("us_table", [(n, d) for n, d in reference_us_columns() if n in missing])
        return {"added": added, "backup": str(bak) if bak else None}

    def create_us(self, sito: str, area: str, us: str, unita_tipo: str = "US", **fields) -> int:
        """New ``us_table`` record (the site record is created when missing). Unknown fields
        are ignored; ``rapporti`` may be a list of ``(relation, us, area, sito)``."""
        if self.us_record(sito, area, us, unita_tipo) is not None:
            raise FileExistsError(f"{unita_tipo} {us} (area {area}) esiste gia' in {sito}")
        self.ensure_site(sito)
        values = self._us_values({"sito": sito, "area": str(area), "us": str(us), "unita_tipo": unita_tipo, **fields},
                                 default_site=sito)
        with self.db.begin() as conn:
            return int(self.db.insert(conn, "us_table", values, id_column="id_us"))

    def update_us(self, id_us: int, **fields) -> int:
        values = self._us_values(fields, default_site=fields.get("sito"))
        values.pop("id_us", None)
        if not values:
            return 0
        t = self.db.table("us_table")
        with self.db.begin() as conn:
            return conn.execute(t.update().where(t.c.id_us == int(id_us)).values(**values)).rowcount

    def add_relationships(self, sito: str, area: str, us: str, rels, unita_tipo: str | None = None) -> int | None:
        """Relations ``[relation, us, area, sito]`` added to the ``rapporti`` of a US form, those it
        already has left out. Returns how many were added, None when the form does not exist."""
        rec = self.us_record(sito, area, us, unita_tipo)
        if rec is None and unita_tipo:  # a form written without its type
            rec = self.us_record(sito, area, us)
        if rec is None:
            return None
        current = parse_relationships(rec.get("rapporti"))
        have = {(r[0], r[1], r[2] or str(area), r[3] or sito) for r in current}
        new = []
        for r in rels:
            r = [str(x) if x is not None else "" for x in r] + ["", ""]
            row = [r[0], r[1], r[2] or str(area), r[3] or sito]
            if tuple(row) not in have:
                have.add(tuple(row))
                new.append(row)
        if new:
            self.update_us(rec["id_us"], rapporti=current + new)
        return len(new)

    def _us_values(self, fields: dict, default_site: str | None = None) -> dict:
        cols = self.us_columns()
        out = {}
        for k, v in fields.items():
            if k not in cols:
                continue
            if k == "rapporti" and isinstance(v, (list, tuple)):
                v = format_relationships(v, default_site)
            out[k] = None if v is None else str(v)
        return out

    def us_records(self, sito: str) -> dict[str, dict]:
        """US number -> selected attributes (for IFC property sets and labels)."""
        out = {}
        for r in self.stratigraphic_units(sito):
            out[str(r["us"])] = {k: r.get(k) for k in US_FIELDS if k in r}
        return out

    def finds(self, sito: str, **filters) -> list[dict]:
        return self.db.query("inventario_materiali_table", sito=sito, **filters)

    def pottery(self, sito: str, **filters) -> list[dict]:
        return self.db.query("pottery_table", sito=sito, **filters)

    def thesaurus(self, code: str, table: str = "pottery_table", lingua: str | None = None) -> list[tuple[str, str]]:
        """``(sigla, value)`` of a list of PyArchInit's thesaurus (``pyarchinit_thesaurus_sigle``:
        ``11.4`` the forms of the pottery form, ``11.5`` the parts, ``11.6`` the wares, ``11.12``
        the shapes) in ``lingua`` (``IT``, ``EN``...), else Italian, else the first language found."""
        if not self.db.has_table("pyarchinit_thesaurus_sigle"):
            return []
        names = {table.lower(), table.split("_")[0].lower()}  # 'pottery_table', or 'Pottery' in older lists
        langs: dict[str, list] = {}
        for r in self.db.query("pyarchinit_thesaurus_sigle", tipologia_sigla=code):
            if str(r.get("nome_tabella") or "").lower() in names and r.get("sigla_estesa"):
                langs.setdefault(str(r.get("lingua") or "").upper(), []).append((str(r.get("sigla") or ""),
                                                                                  str(r["sigla_estesa"])))
        rows = langs.get((lingua or "").upper()) or langs.get("IT") or next(iter(langs.values()), [])
        seen: set[str] = set()
        return [(s, v) for s, v in rows if not (v in seen or seen.add(v))]

    def pottery_profiles(self) -> dict[int, dict]:
        """``id_rep`` -> the profile saved with the drawing of a pottery form (:meth:`save_pottery`:
        ``<drawing>.profile.json`` beside a CERAMICA media file on this computer)."""
        import json

        out = {}
        for link in self.db.query("media_to_entity_table", entity_type="CERAMICA"):
            rid = int(link["id_entity"])
            if rid in out or not link.get("filepath"):
                continue
            try:
                out[rid] = json.loads(Path(str(link["filepath"])).with_suffix(".profile.json").read_text("utf-8"))
            except (OSError, ValueError):  # no profile, or the media is on another computer
                continue
        return out

    def search_pottery(self, rim_cm: float | None = None, max_cm: float | None = None, base_cm: float | None = None,
                       height_cm: float | None = None, signature=None, sito: str | None = None,
                       exclude: int | None = None, limit: int = 20) -> list[dict]:
        """The forms of ``pottery_table`` most like a vessel, the most similar first (all sites
        unless ``sito``). The diameters (rim, maximum, base) and the total height that a form shares
        with the query are compared as relative differences (the rim counts twice); a form saved
        with its profile (:meth:`save_pottery`) is also compared by shape
        (:func:`archaeo.pottery.signature_distance`), and when ``signature`` is given a form without
        a profile counts as a moderate difference of shape (``UNKNOWN_SHAPE``). ``score``: the two
        differences combined (0 = identical); ``similarity_pct``, ``profile_distance`` and
        ``compared`` (what was compared) are added to the fields of the form; forms that share no
        dimension are left out."""
        import math

        from ..archaeo import pottery

        query = {"diametro_rim": rim_cm, "diametro_max": max_cm, "diametro_bottom": base_cm,
                 "diametro_height": height_cm}
        profiles = self.pottery_profiles() if signature is not None else {}
        out = []
        for r in self.db.query("pottery_table", sito=sito):
            if exclude is not None and int(r["id_rep"]) == int(exclude):
                continue
            num, den, compared = 0.0, 0.0, []
            for col, weight in POTTERY_WEIGHTS.items():
                a, b = _num(query[col]), _num(r.get(col))
                if a and b:
                    num += weight * ((a - b) / max(a, b)) ** 2
                    den += weight
                    compared.append(col)
            if not compared:
                continue
            parts = [math.sqrt(num / den)]
            shape = None
            if signature is not None:
                saved = (profiles.get(int(r["id_rep"])) or {}).get("signature")
                shape = pottery.signature_distance(signature, saved) if saved else None
                parts.append(UNKNOWN_SHAPE if shape is None else shape)
                if shape is not None:
                    compared.append("profilo")
            score = math.hypot(*parts)  # differences of size and of shape add up
            row = {k: r.get(k) for k in POTTERY_FIELDS if k in r}
            row.update({c: _num(r.get(c)) for c in POTTERY_WEIGHTS if c in r})
            out.append({**row, "score": score, "similarity_pct": max(0.0, 100.0 * (1.0 - score)),
                        "profile_distance": shape, "compared": compared})
        out.sort(key=lambda x: x["score"])
        return out[:limit]

    def update_pottery(self, id_rep: int, **fields) -> int:
        """Write ``fields`` (columns of ``pottery_table``) in a pottery form."""
        cols = self.db.columns("pottery_table")
        values = {k: v for k, v in fields.items() if k in cols and k != "id_rep"}
        if not values:
            return 0
        t = self.db.table("pottery_table")
        with self.db.begin() as conn:
            return conn.execute(t.update().where(t.c.id_rep == int(id_rep)).values(**values)).rowcount

    def save_pottery(self, sito: str, measures: dict, id_rep: int | None = None, area: str = "", us: str = "",
                     profile=None, name: str | None = None, title: str | None = None, scale: float = 1.0,
                     overwrite: bool = False, **fields) -> dict:
        """The measurements of a vessel or a sherd (:func:`archaeo.pottery.measure`, metres) in its
        pottery form, in centimetres: ``diametro_rim``, ``diametro_max``, ``diametro_bottom`` (when the
        profile reaches the base), ``diametro_height`` (the total height, when the profile is whole),
        ``diametro_preserved`` (the preserved height) and ``percent`` (the class of the rim kept,
        thesaurus 11.2); rim diameter and percent only when the circle of the rim is reliable (else
        they are listed in ``uncertain`` and stay in the profile file). ``id_rep``: that form, whose
        filled fields are kept unless ``overwrite`` (they come back in ``kept`` as ``{field: (value,
        measured)}``); otherwise a new form (``id_number`` the next of the site), with ``fields``
        (``form``, ``ware``...).

        ``profile``: the drawing at 1:``scale`` in the media folder (``ceramica/<sito>``) as PNG,
        linked to the form as CERAMICA media, SVG (the ``drawing`` of the form when it has none)
        and DXF, with ``<name>.profile.json`` (measurements and signature, for
        :meth:`search_pottery`). ``name`` default ``ceramica_<id_rep>``: a drawing already in the
        media is replaced with ``overwrite``, else the new one takes the next free name."""
        import json

        from .. import __version__
        from ..archaeo import pottery

        def cm(key):
            v = measures.get(key)
            return None if v is None else round(float(v) * 100.0, 2)

        values = {"diametro_rim": cm("rim_diameter_m"), "diametro_max": cm("max_diameter_m"),
                  "diametro_bottom": cm("base_diameter_m"),
                  "diametro_height": cm("height_m") if measures.get("full_profile") else None,
                  "diametro_preserved": cm("height_m"),
                  "percent": pottery.percent_class(measures.get("rim_preserved_pct"))}
        uncertain = [k for k in ("diametro_rim", "percent") if values[k] is not None] \
            if measures.get("rim_reliable") is False else []
        values = {k: v for k, v in values.items() if v is not None and k not in uncertain}
        cols = self.db.columns("pottery_table")
        kept, new = {}, id_rep is None
        if new:
            self.ensure_site(sito)
            numbers = [_num(r.get("id_number")) for r in self.db.query("pottery_table", sito=sito)]
            row = {"sito": sito, "area": str(area), "us": str(us),
                   "id_number": int(max([n for n in numbers if n is not None], default=0)) + 1, **fields, **values}
            with self.db.begin() as conn:
                id_rep = int(self.db.insert(conn, "pottery_table", row, id_column="id_rep"))
            record = row
        else:
            found = self.db.query("pottery_table", id_rep=int(id_rep))
            if not found:
                raise KeyError(f"pottery_table: no form with id_rep {id_rep}")
            record = found[0]
            sito, area, us = record.get("sito") or sito, record.get("area") or area, record.get("us") or us
            write = dict(fields)
            for k, v in values.items():
                old = record.get(k)
                if old not in (None, "") and not overwrite and str(old) != str(v) and _num(old) != _num(v):
                    kept[k] = (old, v)
                else:
                    write[k] = v
            self.update_pottery(id_rep, **write)
        out = {"id_rep": id_rep, "values": values, "kept": kept, "uncertain": uncertain, "id_media": None}
        if profile is None:
            return out
        written, fresh = [], False
        try:
            folder = self._media_folder(sito, "ceramica")
            folder.mkdir(parents=True, exist_ok=True)
            stem = _safe(name) if name else f"ceramica_{id_rep}"
            png, n = folder / f"{stem}.png", 1
            while self.db.query("media_table", filepath=str(png)) and not overwrite:
                n += 1
                png = folder / f"{stem}_{n}.png"
            number = record.get("id_number")
            label = title or " - ".join(str(x) for x in (sito, f"area {area}" if area else "", f"US {us}" if us else "",
                                                         f"n. {number}" if number not in (None, "") else "") if x)
            fresh = not png.exists()
            for f in (png, png.with_suffix(".svg"), png.with_suffix(".dxf")):
                pottery.draw(profile, f, scale=scale, title=label, measures=measures)
                written.append(f)
            side = png.with_suffix(".profile.json")
            written.append(side)
            side.write_text(json.dumps({
                "format": "pyarchinit-photogrammetry pottery profile", "version": 1, "id_rep": id_rep, "sito": sito,
                "measures": {k: v for k, v in measures.items() if k != "signature"},
                "signature": measures.get("signature"), "software": f"pyarchinit-photogrammetry {__version__}",
                "date": date.today().isoformat()}, indent=1, default=float), encoding="utf-8")
            out["id_media"] = register_media(self.db, png, mediatype="image", descrizione=f"Disegno ceramica: {label}",
                                             tags="['fotogrammetria', 'ceramica']", links=[("CERAMICA", id_rep)],
                                             overwrite=overwrite, **self._media_dirs())
        except Exception:
            if new:  # a new form without its drawing would be a half-done save
                t = self.db.table("pottery_table")
                with self.db.begin() as conn:
                    conn.execute(t.delete().where(t.c.id_rep == int(id_rep)))
                for f in written if fresh else []:
                    f.unlink(missing_ok=True)
            raise
        if "drawing" in cols and (overwrite or not record.get("drawing")):
            self.update_pottery(id_rep, drawing=png.with_suffix(".svg").name)
        out.update({"image": str(png), "drawing": str(png.with_suffix(".svg")), "dxf": str(png.with_suffix(".dxf")),
                    "profile": str(side)})
        return out

    def documentation(self, sito: str) -> list[dict]:
        return self.db.query("documentazione_table", sito=sito)

    def media_for(self, entity_type: str, id_entity: int) -> list[dict]:
        links = self.db.query("media_to_entity_table", entity_type=entity_type, id_entity=id_entity)
        return [m for link in links for m in self.db.query("media_table", id_media=link["id_media"])]

    def us_media(self, sito: str, area: str, us: str, unita_tipo: str | None = None) -> list[dict]:
        """The media linked to a stratigraphic unit, with their previews (:meth:`_with_previews`)."""
        return self._with_previews(self.media_for("US", self.us_id(sito, area, us, unita_tipo)))

    def site_media(self, sito: str) -> list[dict]:
        rows = self.db.query("site_table", sito=sito)
        return self._with_previews(self.media_for("SITO", int(rows[0]["id_sito"]))) if rows else []

    def _with_previews(self, media: list[dict]) -> list[dict]:
        """``id_media``, ``filename``, ``filetype``, ``mediatype``, ``descrizione`` and the full
        paths of the original (``path``), the thumbnail (``thumb``) and the resized copy
        (``resize``) - None where the file is not on this computer (or on a remote store)."""
        from .config import is_remote

        cfg = self.db.config
        if self.media_root is not None:
            thumb_dir, resize_dir = self.media_root / "thumb", self.media_root / "resize"
        else:
            thumb_dir = Path(cfg.thumb_dir) if cfg and cfg.thumb_dir and not is_remote(cfg.thumb_dir) else None
            resize_dir = Path(cfg.resize_dir) if cfg and cfg.resize_dir and not is_remote(cfg.resize_dir) else None

        def existing(folder, name):
            if folder is None or not name:
                return None
            p = folder / str(name)
            return p if p.exists() else None

        out, seen = [], set()
        for m in media:
            if m["id_media"] in seen:
                continue
            seen.add(m["id_media"])
            t = (self.db.query("media_thumb_table", id_media=m["id_media"]) or [{}])[0]
            original = Path(str(m.get("filepath") or "")) if m.get("filepath") else None
            out.append({"id_media": m["id_media"], "filename": m.get("filename"), "filetype": m.get("filetype"),
                        "mediatype": m.get("mediatype"), "descrizione": m.get("descrizione"),
                        "path": original if original is not None and original.exists() else None,
                        "thumb": existing(thumb_dir, t.get("media_thumb_filename")),
                        "resize": existing(resize_dir, t.get("path_resize"))})
        return out

    def us_polygons(self, sito: str, area: str | None = None, crs: str | None = None):
        """Stratigraphic unit polygons (``pyunitastratigrafiche``) as shapely geometries, in
        ``crs`` (e.g. the chunk CRS) when given, otherwise in the layer's SRID."""
        from shapely import wkt as swkt

        from ..archaeo.stratigraphy import UnitPolygon

        from ..archaeo.stratigraphy import UNIT_FIELDS

        rows = self.db.geometries_wkt("pyunitastratigrafiche", scavo_s=sito, area_s=area)
        dst = crs_mod.epsg_code(crs) if crs else None
        src = self.srid() if dst else None
        out = []
        for r in rows:
            wkt = r.get("wkt")
            if not wkt:
                continue
            if dst and src and src > 0 and src != dst:
                wkt = reproject_wkt(wkt, src, dst)
            attrs = {"gid": r["gid"], **{k: r[k] for k in UNIT_FIELDS if r.get(k) not in (None, "")}}
            out.append(UnitPolygon(swkt.loads(wkt), us=str(r["us_s"]), area=str(r["area_s"]),
                                   tipo=r.get("unita_tipo_s") or "US", attributes=attrs, sito=r.get("scavo_s") or sito))
        return out

    def srid(self) -> int:
        return self.db.srid("pyunitastratigrafiche")

    # ----------------------------------------------------------------- write
    def _media_folder(self, sito: str, sub: str = "fotogrammetria") -> Path:
        cfg = self.db.config
        if self.media_root is not None:
            base = self.media_root
        else:
            base = Path(cfg.media_dir) if cfg else Path.home() / "pyarchinit_media"
        return base / sub / sito.replace("/", "_")

    def _media_dirs(self) -> dict:
        """thumb/resize folder overrides for :func:`register_media` when ``media_root`` is set."""
        if self.media_root is None:
            return {}
        return {"thumb_dir": str(self.media_root / "thumb") + "/", "resize_dir": str(self.media_root / "resize") + "/"}

    def _links(self, sito: str, link_us) -> list[tuple[str, int]]:
        links = []
        for item in link_us or []:
            area, us = item[0], item[1]
            tipo = item[2] if len(item) > 2 else None
            links.append(("US", self.us_id(sito, area, us, tipo)))
        return links

    def add_documentation(self, sito: str, nome_doc: str, tipo_documentazione: str, sorgente: str = "Fotogrammetria",
                          scala: str = "", disegnatore: str = "", note: str = "", data: str | None = None) -> int:
        existing = self.db.query("documentazione_table", sito=sito, tipo_documentazione=tipo_documentazione,
                                 nome_doc=nome_doc)
        if existing:
            return int(existing[0]["id_documentazione"])
        with self.db.begin() as conn:
            return self.db.insert(conn, "documentazione_table", {
                "sito": sito, "nome_doc": nome_doc, "data": data or date.today().isoformat(),
                "tipo_documentazione": tipo_documentazione, "sorgente": sorgente, "scala": scala,
                "disegnatore": disegnatore, "note": note}, id_column="id_documentazione")

    def add_footprint(self, sito: str, nome_doc: str, tipo_doc: str, geometry, path_qgis_pj: str = "",
                      source_crs: str | None = None) -> int:
        """Outline of a document (raster extent, section trace) in ``pyarchinit_documentazione``."""
        from shapely.geometry import LineString

        # LINEARRING WKT is not accepted by GeomFromText: always write a LINESTRING
        line = LineString(geometry.exterior.coords) if geometry.geom_type == "Polygon" else geometry
        with self.db.begin() as conn:
            return self.db.insert_geometry(conn, "pyarchinit_documentazione",
                                           {"sito": sito, "nome_doc": nome_doc, "tipo_doc": tipo_doc,
                                            "path_qgis_pj": path_qgis_pj},
                                           line.wkt,
                                           source_srid=crs_mod.epsg_code(source_crs))

    def publish_raster(self, raster: str | Path, sito: str, nome_doc: str, tipo_documentazione: str,
                       sorgente: str = "Fotogrammetria", link_us=None, crs: str | None = None, scala: str = "",
                       disegnatore: str = "", note: str = "", footprint: bool = True,
                       overwrite: bool = False) -> dict:
        """Copy a GeoTIFF into the PyArchInit media repository, register it as media
        (thumbnail + preview), create the documentation record, link it to US records
        and draw its extent in ``pyarchinit_documentazione``."""
        import rasterio
        from shapely.geometry import box

        self.ensure_site(sito)
        dest = self._media_folder(sito)
        rid = register_media(self.db, raster, mediatype="image", descrizione=f"{tipo_documentazione}: {nome_doc}",
                             tags="['fotogrammetria', 'raster']", links=self._links(sito, link_us), copy_to=dest,
                             overwrite=overwrite, **self._media_dirs())
        did = self.add_documentation(sito, nome_doc, tipo_documentazione, sorgente, scala, disegnatore, note)
        gid = None
        already = [r for r in self.db.query("pyarchinit_documentazione", sito=sito, nome_doc=nome_doc,
                                            tipo_doc=tipo_documentazione)] if footprint else []
        if already:
            gid = already[0]["gid"]
        elif footprint:
            with rasterio.open(raster) as src:
                b = src.bounds
            try:
                gid = self.add_footprint(sito, nome_doc, tipo_documentazione, box(b.left, b.bottom, b.right, b.top),
                                         path_qgis_pj=str(dest / Path(raster).name), source_crs=crs)
            except RuntimeError as e:  # SpatiaLite missing: keep media and documentation
                gid = f"skipped: {e}"
        return {"id_media": rid, "id_documentazione": did, "footprint_gid": gid}

    def publish_orthomosaic(self, chunk, sito: str, nome_doc: str, link_us=None, **kw) -> dict:
        path = chunk.orthomosaic_path
        if path is None:
            raise RuntimeError("build the orthomosaic first")
        named = path.with_name(_safe(nome_doc) + ".tif")
        import shutil

        shutil.copy2(path, named)
        note = kw.pop("note", "") or paradata.summary(chunk, "orthomosaic")
        out = self.publish_raster(named, sito, nome_doc, "Fotopiano", kw.pop("sorgente", "Fotogrammetria"),
                                  link_us=link_us, crs=chunk.crs, note=note, **kw)
        out["paradata"] = str(paradata.write_provenance(chunk, "orthomosaic", self._media_folder(sito) / named.name))
        return out

    def publish_dem(self, chunk, sito: str, nome_doc: str, link_us=None, **kw) -> dict:
        path = chunk.dem_path
        if path is None:
            raise RuntimeError("build the DEM first")
        import shutil

        named = path.with_name(_safe(nome_doc) + ".tif")
        shutil.copy2(path, named)
        note = kw.pop("note", "") or paradata.summary(chunk, "dem")
        out = self.publish_raster(named, sito, nome_doc, "DEM", kw.pop("sorgente", "Fotogrammetria"),
                                  link_us=link_us, crs=chunk.crs, note=note, **kw)
        out["paradata"] = str(paradata.write_provenance(chunk, "dem", self._media_folder(sito) / named.name))
        return out

    def publish_model(self, chunk, sito: str, nome_doc: str, link_us=None, out_dir: str | Path | None = None,
                      **kw) -> dict:
        """Export the (textured) model in world coordinates and register it as ``3d_model`` media."""
        self.ensure_site(sito)
        dest = Path(out_dir) if out_dir else self._media_folder(sito, "modelli_3d")
        dest.mkdir(parents=True, exist_ok=True)
        obj = chunk.export_model(dest / (_safe(nome_doc) + ".obj"))
        rid = register_media(self.db, obj, mediatype="3d_model", descrizione=f"Modello 3D: {nome_doc}",
                             tags="['fotogrammetria', '3d']", links=self._links(sito, link_us),
                             overwrite=kw.get("overwrite", False), **self._media_dirs())
        product = "model_textured" if chunk.product_path("model_textured") else "model"
        did = self.add_documentation(sito, nome_doc, "Modello tridimensionale", kw.get("sorgente", "Fotogrammetria"),
                                     kw.get("scala", ""), kw.get("disegnatore", ""),
                                     kw.get("note", "") or paradata.summary(chunk, product))
        return {"id_media": rid, "id_documentazione": did, "path": str(obj),
                "paradata": str(paradata.write_provenance(chunk, product, obj))}

    def publish_units(self, units, sito: str, area: str | None = None, crs: str | None = None,
                      rilievo_originale: str = "Fotogrammetria", disegnatore: str = "", nome_doc: str = "",
                      tipo_doc: str = "Fotopiano") -> list[int]:
        """Write unit polygons into ``pyunitastratigrafiche`` (US must be set on each unit).
        The columns a unit carries in its attributes (``stratigraph_index_us``, ``tipo_us_s``,
        ``disegnatore``, ``data``...) win over the arguments, which are the defaults."""
        self.ensure_site(sito)
        gids = []
        with self.db.begin() as conn:
            for u in units:
                if not u.us:
                    raise ValueError("every unit needs its US number before publishing")
                a = u.attributes or {}
                try:
                    index = int(a.get("stratigraph_index_us") or 1)
                except (TypeError, ValueError):
                    index = 1
                gids.append(self.db.insert_geometry(conn, "pyunitastratigrafiche", {
                    "scavo_s": u.sito or sito, "area_s": str(u.area or area or ""), "us_s": str(u.us),
                    "unita_tipo_s": u.tipo or "US", "stratigraph_index_us": index,
                    "tipo_us_s": a.get("tipo_us_s") or "",
                    "rilievo_originale": a.get("rilievo_originale") or rilievo_originale,
                    "disegnatore": a.get("disegnatore") or disegnatore,
                    "data": a.get("data") or date.today().isoformat(),
                    "tipo_doc": a.get("tipo_doc") or tipo_doc, "nome_doc": a.get("nome_doc") or nome_doc},
                    u.geometry.wkt, source_srid=crs_mod.epsg_code(crs), multi=True))
        return gids

    def update_unit(self, gid: int, geometry=None, crs: str | None = None, **fields) -> int:
        """Change a polygon already in ``pyunitastratigrafiche``: attributes (``us``,
        ``area``, ``tipo``, or any column name) and/or its ``geometry`` (shapely, in ``crs``;
        e.g. after merging units). Returns the number of rows updated."""
        names = {"us": "us_s", "area": "area_s", "tipo": "unita_tipo_s", "sito": "scavo_s"}
        cols = self.db.columns("pyunitastratigrafiche")
        values = {names.get(k, k): (None if v is None else str(v)) for k, v in fields.items()}
        values = {k: v for k, v in values.items() if k in cols and k not in ("gid", "the_geom")}
        if not values and geometry is None:
            return 0
        t = self.db.table("pyunitastratigrafiche")
        n = 0
        with self.db.begin() as conn:
            if values:
                n = conn.execute(t.update().where(t.c.gid == int(gid)).values(**values)).rowcount
            if geometry is not None:
                n = max(n, self.db.update_geometry(conn, "pyunitastratigrafiche", int(gid), geometry.wkt,
                                                   source_srid=crs_mod.epsg_code(crs), multi=True))
        return n

    def delete_unit(self, gid: int) -> int:
        """Remove a polygon from ``pyunitastratigrafiche`` (the US form is not touched)."""
        t = self.db.table("pyunitastratigrafiche")
        with self.db.begin() as conn:
            return conn.execute(t.delete().where(t.c.gid == int(gid))).rowcount

    def publish_quote(self, points: list[dict], sito: str, crs: str | None = None) -> list[int]:
        """Spot heights into ``pyarchinit_quote``: ``points`` are dicts with ``x``, ``y`` and the
        columns of the layer (``area_q``, ``us_q``, ``unita_tipo_q``, ``quota_q``,
        ``unita_misu_q``, ``data``, ``disegnatore``, ``rilievo_originale``; ``sito_q`` defaults
        to ``sito``)."""
        self.ensure_site(sito)
        cols = self.db.columns("pyarchinit_quote")
        gids = []
        with self.db.begin() as conn:
            for p in points:
                rec = {"sito_q": p.get("sito_q") or sito, "area_q": str(p.get("area_q") or ""),
                       "us_q": str(p.get("us_q") or ""), "unita_tipo_q": p.get("unita_tipo_q") or "US",
                       "quota_q": None if p.get("quota_q") in (None, "") else round(float(p["quota_q"]), 3),
                       "unita_misu_q": p.get("unita_misu_q") or "m s.l.m.",
                       "data": p.get("data") or date.today().isoformat(), "disegnatore": p.get("disegnatore") or "",
                       "rilievo_originale": p.get("rilievo_originale") or "Fotogrammetria"}
                gids.append(self.db.insert_geometry(conn, "pyarchinit_quote", {k: v for k, v in rec.items() if k in cols},
                                                    f"POINT ({float(p['x'])} {float(p['y'])})",
                                                    source_srid=crs_mod.epsg_code(crs)))
        return gids

    def publish_elevations(self, chunk, sito: str, area: str, us: str, points_xy, unita_tipo: str = "US",
                           disegnatore: str = "") -> list[int]:
        """Spot heights (``pyarchinit_quote``) sampled on the DEM at the given world XY points."""
        from ..engine.dem import read_dem

        z, tr, _, _ = read_dem(chunk.dem_path)
        self.ensure_site(sito)
        gids = []
        with self.db.begin() as conn:
            for x, y in np.asarray(points_xy, float):
                c, r = int(np.floor((x - tr.c) / tr.a)), int(np.floor((y - tr.f) / tr.e))
                if not (0 <= r < z.shape[0] and 0 <= c < z.shape[1]):
                    continue  # outside the DEM
                q = float(z[r, c])
                if not np.isfinite(q):
                    continue
                gids.append(self.db.insert_geometry(conn, "pyarchinit_quote", {
                    "sito_q": sito, "area_q": str(area), "us_q": str(us), "unita_misu_q": "m s.l.m.",
                    "quota_q": round(q, 3), "data": date.today().isoformat(), "disegnatore": disegnatore,
                    "rilievo_originale": "Fotogrammetria", "unita_tipo_q": unita_tipo},
                    f"POINT ({x} {y})", source_srid=crs_mod.epsg_code(chunk.crs)))
        return gids

    def publish_section_trace(self, section, sito: str, id_sezione: str, area: str | None = None, descr: str = "",
                              crs: str | None = None) -> int:
        from shapely.geometry import LineString

        line = LineString([section.p1[:2], section.p2[:2]])
        self.ensure_site(sito)
        with self.db.begin() as conn:
            return self.db.insert_geometry(conn, "pyarchinit_sezioni", {
                "id_sezione": id_sezione, "sito": sito, "area": area, "descr": descr or section.label,
                "tipo_doc": "Sezione", "nome_doc": section.label}, line.wkt,
                source_srid=crs_mod.epsg_code(crs))

    def publish_volume(self, sito: str, nome_calcolo: str, result: dict, tipo_calcolo: str = "Scavo",
                       dem_pre: str = "", dem_post: str = "", layer_poligono: str = "", fase_scavo: str = "",
                       note: str = "") -> int:
        """Store a cut/fill result (see :func:`archaeo.terrain.volume`) in ``computo_metrico_table``."""
        self.ensure_site(sito)
        with self.db.begin() as conn:
            return self.db.insert(conn, "computo_metrico_table", {
                "sito": sito, "nome_calcolo": nome_calcolo, "tipo_calcolo": tipo_calcolo, "dem_pre": dem_pre,
                "dem_post": dem_post, "layer_poligono": layer_poligono, "area_mq": result.get("area_m2"),
                "volume_mc": result.get("net_m3"), "quota_min": result.get("quota_min"),
                "quota_max": result.get("quota_max"), "data_calcolo": date.today().isoformat(),
                "fase_scavo": fase_scavo, "note": note or f"scavo {result.get('cut_m3', 0):.3f} m3, "
                                                          f"riporto {result.get('fill_m3', 0):.3f} m3"},
                id_column="id_computo")


RELATION_WORDS = {"copre": "Copre", "coperto da": "Coperto da", "taglia": "Taglia", "tagliato da": "Tagliato da",
                  "riempie": "Riempie", "riempito da": "Riempito da", "si appoggia a": "Si appoggia a",
                  "gli si appoggia": "Gli si appoggia", "si lega a": "Si lega a", "uguale a": "Uguale a",
                  "anteriore a": "Anteriore a", "posteriore a": "Posteriore a"}


_US_REFERENCE: list[tuple[str, str]] | None = None


def reference_us_columns() -> list[tuple[str, str]]:
    """``us_table`` columns of PyArchInit 5 as ``(name, SQL declaration)``, read from the
    bundled schema (``pyarchinit5_schema.sql``); the primary key is left out."""
    global _US_REFERENCE
    if _US_REFERENCE is None:
        import re
        import sqlite3

        ddl = Path(__file__).with_name("pyarchinit5_schema.sql").read_text(encoding="utf-8")
        m = re.search(r'^CREATE TABLE "?us_table"?\s*\((.*)\);\s*$', ddl, re.M)
        if m is None:
            raise RuntimeError("us_table not found in pyarchinit5_schema.sql")
        con = sqlite3.connect(":memory:")
        try:
            con.execute(f"CREATE TABLE us_table ({m.group(1)})")
            cols = con.execute("PRAGMA table_info(us_table)").fetchall()
        finally:
            con.close()
        _US_REFERENCE = [(name, (ctype or "TEXT") + (f" DEFAULT {dflt}" if dflt is not None else ""))
                         for _cid, name, ctype, _nn, dflt, pk in cols if not pk]
    return list(_US_REFERENCE)


def parse_relationships(text: str | None) -> list[list[str]]:
    """PyArchInit ``rapporti`` (``"[['Copre', '2', '1', 'Sito'], ...]"``) as a list of
    ``[relation, us, area, sito]``; tolerant of the short ``[relation, us]`` form and of
    plain text such as ``"copre 3; tagliato da 7"``."""
    import ast
    import re

    if not text or not str(text).strip():
        return []
    try:
        data = ast.literal_eval(str(text))
    except (ValueError, SyntaxError):
        out = []
        for part in re.split(r"[;,\n]+", str(text)):
            m = re.match(r"\s*([A-Za-z' ]+?)\s+(\S+)\s*$", part)
            if m and m.group(1).lower() in RELATION_WORDS:
                out.append([RELATION_WORDS[m.group(1).lower()], m.group(2), "", ""])
        return out
    if not isinstance(data, (list, tuple)):
        return []
    out = []
    for item in data or []:
        item = [str(x) for x in (item or [])]
        if len(item) >= 2:
            out.append((item + ["", ""])[:4])
    return out


def format_relationships(rels, default_site: str | None = None, default_area: str | None = None) -> str:
    """``[[relation, us, area, sito], ...]`` as PyArchInit stores it."""
    rows = []
    for r in rels:
        r = [str(x) if x is not None else "" for x in r]
        rel, us = r[0], r[1]
        area = r[2] if len(r) > 2 and r[2] else (default_area or "")
        site = r[3] if len(r) > 3 and r[3] else (default_site or "")
        rows.append([rel, us, area, site])
    return repr(rows)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip("_") or "documento"


def _num(value) -> float | None:
    """A number of a form (NUMERIC, text with a decimal comma, empty) as a float, None if missing."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None
