"""Foto di un'area - script per pyarchinit-photogrammetry 1.6.2 (dall'idea di Lorenzo Fornaciari).

Trova le foto che inquadrano l'area selezionata sulla vista 2D (ortofoto o DEM) e, a scelta:

- "filtra":     mostra solo quelle foto nel pannello Foto e scrive l'elenco (non cambia nulla del progetto);
- "disattiva":  disattiva tutte le ALTRE foto del chunk, cosi' mappe di profondita', nuvola densa, mesh,
                texture, DEM e ortofoto si possono rifare solo con le foto di quella parte;
- "ripristina": rimette ogni foto attiva o disattivata com'era prima dell'ultimo "disattiva";
- "esporta":    scrive un CSV con le foto dell'area (copertura, parte visibile, angolo, risoluzione).

Come si usa (nel programma):
1. apri l'ortofoto o il DEM nella vista 2D;
2. con lo strumento Seleziona fai clic su uno o piu' poligoni (una US, un'area disegnata, una patch:
   piu' poligoni selezionati valgono come un'area unica);
3. scegli qui sotto MODO, poi Strumenti > Esegui script Python... e questo file;
4. dopo "disattiva" salva il progetto (File > Salva) e rifai i prodotti che ti servono.

Da terminale (senza selezione) si puo' dare l'area come testo WKT nelle coordinate del chunk:
    ppg run foto_area.py --project progetto.ppg
con AREA_WKT impostato qui sotto, per esempio "POLYGON ((x1 y1, x2 y2, x3 y3, x1 y1))".

Nota: nella 1.6.2 il comando Annulla del programma non riguarda le foto attive o disattivate: per tornare
indietro usa MODO = "ripristina" (lo stato di prima viene conservato nel progetto).
"""
from __future__ import annotations

import csv
import datetime as _dt
import sys
from pathlib import Path

# ------------------------------------------------------------------ impostazioni
MODO = "filtra"          # "filtra", "disattiva", "ripristina" oppure "esporta"
COPERTURA_MINIMA = 0.05  # quota dell'area (0-1) che una foto deve inquadrare per contare (0.05 = 5 %)
AREA_WKT = ""            # solo da terminale: l'area come WKT nelle coordinate del chunk
CHIEDI_CONFERMA = True   # nel programma: chiede conferma prima di disattivare
# --------------------------------------------------------------------------------

CHIAVE = "foto_area"     # dove lo stato di prima viene conservato: chunk.meta["foto_area"]


def _documento_chunk_finestra():
    g = globals()
    doc, finestra = g.get("doc"), g.get("window")
    if doc is None:
        import pyarchinit_photogrammetry as ppg
        doc = ppg.app.document
        if (doc is None or not doc.chunks) and len(sys.argv) > 1 and sys.argv[1].endswith(".ppg"):
            doc = ppg.app.open_document(sys.argv[1])
    if doc is None or not doc.chunks:
        raise RuntimeError("Nessun progetto aperto.")
    chunk = g.get("chunk") or doc.chunk
    if chunk is None:
        raise RuntimeError("Il progetto non ha un chunk attivo.")
    return doc, chunk, finestra


def _area(finestra):
    if AREA_WKT.strip():
        from shapely import wkt
        return wkt.loads(AREA_WKT)
    if finestra is None:
        raise RuntimeError("Da terminale l'area va data in AREA_WKT (nelle coordinate del chunk).")
    area = finestra._selected_polygon()
    if area is None or area.is_empty:
        raise RuntimeError("Nessun poligono selezionato: nella vista 2D usa lo strumento Seleziona e fai clic "
                           "su uno o piu' poligoni, poi esegui di nuovo lo script.")
    return area


def _conferma(finestra, testo) -> bool:
    if finestra is None or not CHIEDI_CONFERMA:
        return True
    from pyarchinit_photogrammetry.gui.dialogs import choice
    return choice(finestra, "question", "Foto dell'area", testo, ["Disattiva"]) == 0


def _aggiorna(finestra, modificato: bool):
    if finestra is None:
        return
    if modificato:
        finestra.mark_modified()
    finestra.refresh_all()


def foto_area():
    doc, chunk, finestra = _documento_chunk_finestra()

    if MODO == "ripristina":
        prima = chunk.meta.get(CHIAVE, {}).get("prima")
        if not prima:
            print("Niente da ripristinare: nessun 'disattiva' registrato in questo chunk.")
            return
        cambiate = 0
        for c in chunk.cameras:
            stato = prima.get(str(c.key))
            if stato is not None and c.enabled != stato:
                c.enabled = stato
                cambiate += 1
        chunk.meta.pop(CHIAVE, None)
        _aggiorna(finestra, True)
        print(f"Ripristinate {cambiate} foto come prima dell'ultimo 'disattiva'. Ricordati di salvare il progetto.")
        return

    if MODO not in ("filtra", "disattiva", "esporta"):
        raise RuntimeError(f"MODO sconosciuto: {MODO!r} (filtra, disattiva, ripristina, esporta)")

    from pyarchinit_photogrammetry.engine.patches import photos_for_area

    area = _area(finestra)
    print(f"Area: {area.area:.2f} m2 - cerco le foto che la inquadrano (puo' richiedere qualche secondo)...")
    righe = photos_for_area(chunk, area, limit=100_000)
    utili = [r for r in righe if r.get("coverage", 0) >= max(COPERTURA_MINIMA, 1e-9)]
    chiavi = {r["key"] for r in utili}
    print(f"{len(utili)} foto su {len(chunk.cameras)} inquadrano almeno il {COPERTURA_MINIMA * 100:.0f} % dell'area.")
    for r in utili[:20]:
        print(f"  {r['label']}: copertura {r['coverage'] * 100:.0f} %, visibile {r['visible'] * 100:.0f} %, "
              f"angolo {r['angle']:.0f} gradi")
    if len(utili) > 20:
        print(f"  ... e altre {len(utili) - 20}")

    if MODO == "filtra":
        if finestra is not None:
            n = finestra.photos.set_filter(chiavi, "Foto dell'area (script)")
            finestra._show_pane(finestra.bottom_tabs, finestra.photos_panel)
            print(f"Il pannello Foto mostra {n} foto (Mostra tutte per tornare all'elenco completo).")
        return

    if MODO == "esporta":
        cartella = doc.files_dir / "foto_area"
        cartella.mkdir(parents=True, exist_ok=True)
        uscita = cartella / f"foto_area_{chunk.key}_{_dt.datetime.now():%Y%m%d_%H%M}.csv"
        with open(uscita, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["foto", "copertura", "visibile", "angolo_gradi", "risoluzione_m", "punteggio"])
            for r in utili:
                w.writerow([r["label"], round(r["coverage"], 4), round(r["visible"], 4), round(r["angle"], 1),
                            r["gsd"], round(r["score"], 4)])
        print(f"Elenco: {uscita}")
        return

    # MODO == "disattiva"
    if len(utili) < 2:
        print("Meno di due foto inquadrano l'area: non disattivo nulla (non basterebbero per i prodotti).")
        return
    altre = [c for c in chunk.cameras if c.key not in chiavi and c.enabled]
    if not altre:
        print("Tutte le foto attive inquadrano gia' l'area: niente da disattivare.")
        return
    if not _conferma(finestra, f"Disattivare {len(altre)} foto che non inquadrano l'area? Ne restano attive "
                               f"{len(utili)}. Si torna indietro con MODO = \"ripristina\"."):
        print("Annullato.")
        return
    stato = chunk.meta.get(CHIAVE, {})
    if "prima" not in stato:  # dopo due "disattiva" di fila si torna allo stato di prima del primo
        stato["prima"] = {str(c.key): bool(c.enabled) for c in chunk.cameras}
    stato["area_m2"], stato["foto_attive"] = round(float(area.area), 3), len(utili)
    chunk.meta[CHIAVE] = stato
    for c in altre:
        c.enabled = False
    _aggiorna(finestra, True)
    print(f"Disattivate {len(altre)} foto; attive {sum(c.enabled for c in chunk.cameras)}. Salva il progetto e rifai "
          "mappe di profondita', nuvola densa, DEM e ortofoto per quella parte. Per tornare indietro: MODO = \"ripristina\".")


foto_area()
