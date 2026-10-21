# Integrazione con PyArchInit

Il bridge (`pyarchinit_photogrammetry.pyarchinit`) funziona senza QGIS. Rispecchia le convenzioni
di PyArchInit 5 (riferimento: branch `Stratigraph_00001`).

## Connessione

| Sorgente | Come |
| --- | --- |
| Impostazioni di PyArchInit | `PyArchInit.connect()` legge `$PYARCHINIT_HOME/pyarchinit_DB_folder/config.cfg` (`PYARCHINIT_HOME` dall'ambiente, altrimenti `~/pyarchinit_5` o `~/pyarchinit`) |
| Database esplicito | `PyArchInit.connect("postgresql://user:pwd@host:5433/pyarchinit")` o `PyArchInit.connect("/path/db.sqlite")`; le cartelle delle miniature vengono comunque prese da `config.cfg` quando c'è |

`config.cfg` viene letto con `ast.literal_eval` (è un dizionario Python scritto come letterale). I
percorsi SQLite in `DATABASE` sono relativi a `pyarchinit_DB_folder`, come in PyArchInit.

Per SQLite, `mod_spatialite` serve solo per scrivere le geometrie. Il pacchetto inizializza la
libreria PROJ fornita con rasterio/pyproj prima di caricare SpatiaLite: caricare per primo uno
SpatiaLite di sistema può mandare in crash il codice basato su GDAL nello stesso processo.

## Lettura

| Metodo | Tabella |
| --- | --- |
| `sites()` | `site_table` |
| `stratigraphic_units(sito, area=None, us=None)` | `us_table` |
| `us_id(sito, area, us)` | `us_table.id_us` |
| `next_us(sito)` | il numero dopo la US (numerica) più alta del sito |
| `us_records(sito)` | US -> attributi (usati per i property set dell'IFC) |
| `finds(sito)`, `pottery(sito)`, `documentation(sito)` | `inventario_materiali_table`, `pottery_table`, `documentazione_table` |
| `search_pottery(rim_cm, max_cm, base_cm, height_cm, signature, sito=None)` | la `pottery_table` di ogni sito (o di uno solo) ordinata per le differenze relative dei diametri e dell'altezza che una forma ha in comune con la richiesta (orlo con peso 2) e, per le forme salvate con `save_pottery`, per la distanza tra le forme dei profili; `score`, `similarity_pct`, `compared` |
| `pottery_profiles()` | `id_rep` -> il `.profile.json` accanto a un media CERAMICA |
| `thesaurus(code, lingua=None)` | `pyarchinit_thesaurus_sigle` (`11.4` forme, `11.5` parti, `11.6` classi ceramiche, `11.12` forme particolari della scheda ceramica) |
| `media_for(entity_type, id_entity)` | `media_to_entity_table` + `media_table` |
| `us_polygons(sito, area=None)` | `pyunitastratigrafiche` come `UnitPolygon` (shapely) con `sito` (`scavo_s`) e le altre colonne in `attributes` (`stratigraph_index_us`, `tipo_us_s`, `disegnatore`, `data`, `rilievo_originale`, `tipo_doc`, `nome_doc`) |

## Scrittura

| Metodo | Cosa viene scritto |
| --- | --- |
| `publish_orthomosaic(chunk, sito, nome_doc, link_us=[(area, us)])` | GeoTIFF copiato in `<media>/fotogrammetria/<sito>/`, `media_table` (`image`, `tif`), miniature, `media_to_entity_table` (`US`, `us_table`), `documentazione_table` (`Fotopiano`, `Fotogrammetria`, GSD e CRS in `note`), estensione in `pyarchinit_documentazione` |
| `publish_dem(...)` | come sopra con `tipo_documentazione = "DEM"` |
| `publish_model(...)` | OBJ con texture in coordinate mondo, `mediatype = "3d_model"`, `{id}_{name}_3d_thumb.png`, modello copiato nella cartella delle copie ridotte (resize), `Modello tridimensionale` |
| `publish_units(units, sito, area, crs)` | `pyunitastratigrafiche` (MULTIPOLYGON): `scavo_s`, `area_s`, `us_s`, `unita_tipo_s`, `stratigraph_index_us` (1 caratterizzazione, 2 contorno US, come lo legge lo stile del layer di PyArchInit), `tipo_us_s`, `rilievo_originale`, `disegnatore`, `data`, `tipo_doc`, `nome_doc` dall'unità (gli argomenti sono i valori predefiniti) |
| `publish_quote(points, sito, crs)` | `pyarchinit_quote` da dizionari con `x`, `y`, `area_q`, `us_q`, `unita_tipo_q`, `quota_q`, `unita_misu_q`, `data`, `disegnatore`, `rilievo_originale` |
| `publish_elevations(chunk, sito, area, us, points)` | `pyarchinit_quote`, quote campionate sul DEM |
| `publish_section_trace(section, sito, id_sezione)` | `pyarchinit_sezioni` |
| `publish_volume(sito, nome_calcolo, result)` | `computo_metrico_table` |
| `create_us(sito, area, us, unita_tipo, **fields)` | un nuovo record di `us_table` (`id_us` = max+1, `entity_uuid`, `node_uuid`); il record del sito viene creato se manca; `FileExistsError` se sito/area/US/tipo esiste già. I campi sconosciuti vengono ignorati |
| `update_us(id_us, **fields)` | le colonne indicate di un record di `us_table` |
| `upgrade_us_table()` | aggiunge le colonne di PyArchInit 5 che mancano a `us_table` (`missing_us_columns()`), come fa l'aggiornatore di PyArchInit: una copia `<file>.backup_<data>_<ora>` di un file SQLite, poi `ALTER TABLE ... ADD COLUMN`; i record non vengono toccati. Nomi e tipi delle colonne vengono dallo schema incluso nel pacchetto (`reference_us_columns()`) |
| `update_unit(gid, geometry=None, crs=None, **fields)` | attributi e/o geometria di un poligono di `pyunitastratigrafiche` (geometria riproiettata da `crs` allo SRID del layer) |
| `delete_unit(gid)` | toglie un poligono di `pyunitastratigrafiche` (la scheda US non viene toccata) |
| `save_pottery(sito, measures, id_rep=None, area, us, profile, overwrite=False, **fields)` | `pottery_table` in centimetri, come la scheda di PyArchInit: `diametro_rim`, `diametro_max`, `diametro_bottom` (quando il profilo arriva alla base), `diametro_height` (altezza totale, solo con il profilo intero), `diametro_preserved` (altezza conservata, l'etichetta della scheda), `percent` (thesaurus 11.2: `<5`, `5-25`, `25-50`, `>50`); orlo e percent solo quando il cerchio dell'orlo è affidabile. Una scheda nuova riceve `id_rep` = max+1, `id_number` = il successivo del sito ed `entity_uuid`; in una esistente i campi già compilati vengono mantenuti (restituiti in `kept`) a meno di `overwrite`. Con `profile` il disegno va in `<media>/ceramica/<sito>/` come PNG (media `image` collegato come `CERAMICA` / `pottery_table`), SVG (il campo `drawing` quando è vuoto), DXF e `.profile.json` (misure, firma del profilo indipendente dalla dimensione) |
| `update_pottery(id_rep, **fields)` | le colonne indicate di una scheda ceramica |

`us_polygons(sito, crs=...)` restituisce i poligoni nel CRS indicato (quello del chunk),
riproiettati dallo SRID del layer quando sono diversi.

`rapporti` è conservato come fa PyArchInit (`"[['Copre', '2', '1', 'Sito'], ...]"`);
`parse_relationships` lo legge (anche la forma breve `[rel, us]` e il testo semplice come
`copre 3; tagliato da 7`) e `format_relationships` lo scrive. `RELATIONS` elenca i nomi dei
rapporti e `INVERSE` i loro inversi (*Copre* / *Coperto da*...).

Convenzioni:

* gli id sono `max + 1` come in PyArchInit; su PostgreSQL, le colonne con una sequence
  (`gid` dei layer spaziali) prendono `nextval`, così l'editing in QGIS continua a funzionare;
* `entity_uuid` (UUID v4) viene compilato quando la colonna esiste;
* miniature: `THUMB_PATH/{id}_{name}_thumb.png` (max 150 px, 100 dpi), copia ridotta
  `THUMB_RESIZE/{id}_{name}.png` (max 2008 x 1417, 300 dpi); le anteprime dei raster sono
  disegnate con rasterio, così GeoTIFF grandi o in virgola mobile vanno bene;
* le geometrie vengono riproiettate con pyproj dal CRS del chunk allo SRID dichiarato in
  `geometry_columns`;
* pubblicare due volte lo stesso file solleva `FileExistsError`; `overwrite=True` rinnova il file
  e le anteprime e mantiene lo stesso `id_media`;
* la registrazione di un media è una sola transazione: se un'anteprima fallisce non resta nulla.


## Connessioni, siti e il database dimostrativo (0.3)

* `PyArchInit.connect()` legge `~/pyarchinit_5/pyarchinit_DB_folder/config.cfg` (PyArchInit 5) o
  `~/pyarchinit/...`, o `PYARCHINIT_HOME`. `connect(url="path.sqlite")`, `connect(url="postgresql://user:pw@host:5432/db?sslmode=allow")`
  e `connect(config_path=...)` sono le altre vie; `media_root=` manda media, miniature e copie
  ridotte in una cartella a tua scelta invece di quelle di PyArchInit (usato dai test).
* Un sito deve esistere in `site_table` perché PyArchInit mostri le sue schede: `ensure_site(sito, nazione=..., comune=...)`
  crea la *scheda Sito* se manca, e ogni metodo `publish_*` lo chiama.
* `pyarchinit.demo_db.create_demo_database(home, size="large")` costruisce `demo.sqlite` con le
  definizioni delle tabelle di PyArchInit 5 (`pyarchinit5_schema.sql`, 60 tabelle, 21 layer con
  geometria registrati in EPSG:3004 quando `mod_spatialite` è disponibile): tre siti, 73 unità,
  poligoni, quote, una traccia di sezione, ceramica e reperti per il sito dimostrativo. I file
  esistenti vengono conservati (`overwrite=True` per rifarli) e `config.cfg` viene scritto solo se
  manca, così l'interfaccia può creare la demo dentro una vera installazione di PyArchInit senza
  cambiarne le impostazioni.
* Il bridge è stato provato su database PyArchInit 5 veri (87 tabelle, 11 siti, 4.820 poligoni di
  unità, 2.779 media): lettura di siti, unità, poligoni, documentazione, reperti e media, e
  scrittura di siti, poligoni di unità e record di documentazione.
