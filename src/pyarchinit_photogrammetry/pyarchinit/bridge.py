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
from .database import PyArchInitDB, connect
from .media import register_media

US_FIELDS = ["sito", "area", "us", "unita_tipo", "d_stratigrafica", "d_interpretativa", "descrizione",
             "interpretazione", "periodo_iniziale", "fase_iniziale", "periodo_finale", "fase_finale",
             "anno_scavo", "schedatore", "rapporti", "quota_min_usm", "quota_max_usm"]


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

    def documentation(self, sito: str) -> list[dict]:
        return self.db.query("documentazione_table", sito=sito)

    def media_for(self, entity_type: str, id_entity: int) -> list[dict]:
        links = self.db.query("media_to_entity_table", entity_type=entity_type, id_entity=id_entity)
        return [m for link in links for m in self.db.query("media_table", id_media=link["id_media"])]

    def us_polygons(self, sito: str, area: str | None = None):
        """Stratigraphic unit polygons (``pyunitastratigrafiche``) as shapely geometries."""
        from shapely import wkt as swkt

        from ..archaeo.stratigraphy import UnitPolygon

        rows = self.db.geometries_wkt("pyunitastratigrafiche", scavo_s=sito, area_s=area)
        return [UnitPolygon(swkt.loads(r["wkt"]), us=str(r["us_s"]), area=str(r["area_s"]),
                            tipo=r.get("unita_tipo_s") or "US", attributes={"gid": r["gid"]})
                for r in rows if r.get("wkt")]

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
        res = chunk.meta.get("orthomosaic", {}).get("resolution")
        note = kw.pop("note", "") or (f"GSD {res * 1000:.1f} mm/px, CRS {chunk.crs}" if res else "")
        return self.publish_raster(named, sito, nome_doc, "Fotopiano", kw.pop("sorgente", "Fotogrammetria"),
                                   link_us=link_us, crs=chunk.crs, note=note, **kw)

    def publish_dem(self, chunk, sito: str, nome_doc: str, link_us=None, **kw) -> dict:
        path = chunk.dem_path
        if path is None:
            raise RuntimeError("build the DEM first")
        import shutil

        named = path.with_name(_safe(nome_doc) + ".tif")
        shutil.copy2(path, named)
        res = chunk.meta.get("dem", {}).get("resolution")
        note = kw.pop("note", "") or (f"DEM {res * 100:.1f} cm/px, CRS {chunk.crs}" if res else "")
        return self.publish_raster(named, sito, nome_doc, "DEM", kw.pop("sorgente", "Fotogrammetria"),
                                   link_us=link_us, crs=chunk.crs, note=note, **kw)

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
        did = self.add_documentation(sito, nome_doc, "Modello tridimensionale", kw.get("sorgente", "Fotogrammetria"),
                                     kw.get("scala", ""), kw.get("disegnatore", ""), kw.get("note", ""))
        return {"id_media": rid, "id_documentazione": did, "path": str(obj)}

    def publish_units(self, units, sito: str, area: str | None = None, crs: str | None = None,
                      rilievo_originale: str = "Fotogrammetria", disegnatore: str = "", nome_doc: str = "",
                      tipo_doc: str = "Fotopiano") -> list[int]:
        """Write unit polygons into ``pyunitastratigrafiche`` (US must be set on each unit)."""
        self.ensure_site(sito)
        gids = []
        with self.db.begin() as conn:
            for u in units:
                if not u.us:
                    raise ValueError("every unit needs its US number before publishing")
                gids.append(self.db.insert_geometry(conn, "pyunitastratigrafiche", {
                    "scavo_s": sito, "area_s": str(u.area or area or ""), "us_s": str(u.us),
                    "unita_tipo_s": u.tipo or "US", "tipo_us_s": u.attributes.get("tipo_us_s", ""),
                    "rilievo_originale": rilievo_originale, "disegnatore": disegnatore,
                    "data": date.today().isoformat(), "tipo_doc": tipo_doc, "nome_doc": nome_doc},
                    u.geometry.wkt, source_srid=crs_mod.epsg_code(crs), multi=True))
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


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip("_") or "documento"
