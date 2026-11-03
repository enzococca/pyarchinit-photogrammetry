"""Controllo del rilievo - script per pyarchinit-photogrammetry 1.6.2.

Fa un controllo di qualita' del chunk attivo e scrive un rapporto, senza cambiare nulla del progetto:

- foto: quante sono, quante allineate, quali NON allineate o disattivate, errore di riproiezione;
- marker e punti di controllo/check: errore in metri (dx, dy, dz) e in pixel, foto su cui sono misurati,
  con un avviso per quelli con poche misure o errore alto;
- barre di scala: distanza misurata contro distanza nota;
- GPS delle foto: scarto mediano e massimo fra posizione stimata e posizione GPS;
- prodotti: nuvola densa (punti), DEM (risoluzione, copertura), ortofoto (risoluzione, dimensioni), modello;
- facoltativo: nitidezza delle foto (le piu' mosse), che legge tutte le foto e quindi richiede tempo.

Come si usa:
- nel programma: Strumenti > Esegui script Python... e scegli questo file (il risultato appare nella
  Console Python; il rapporto viene scritto nella cartella del progetto);
- da terminale: ppg run controllo_rilievo.py --project /percorso/progetto.ppg

Il rapporto va in <progetto>.ppg.files/controllo/ come file Markdown (si apre con qualsiasi editor di
testo) e come CSV dei marker (si apre con Excel o LibreOffice).
"""
from __future__ import annotations

import csv
import datetime as _dt
from pathlib import Path

import numpy as np

# ------------------------------------------------------------------ impostazioni
CONTROLLA_NITIDEZZA = False   # True: misura la nitidezza di ogni foto (lento: legge tutte le foto)
SOGLIA_ERRORE_MARKER_M = 0.05  # oltre questo errore (m) un marker viene segnalato
SOGLIA_ERRORE_MARKER_PX = 2.0  # oltre questo errore in pixel un marker viene segnalato
MISURE_MINIME_MARKER = 3       # sotto queste foto misurate un marker viene segnalato
SOGLIA_QUALITA_FOTO = 0.5      # con CONTROLLA_NITIDEZZA: foto sotto questa qualita' (0-1) segnalate
# --------------------------------------------------------------------------------


def _documento_e_chunk():
    """Il progetto e il chunk attivo: dalla console del programma o da `ppg run`."""
    import sys

    g = globals()
    doc = g.get("doc")
    if doc is None:
        import pyarchinit_photogrammetry as ppg
        doc = ppg.app.document
        # `ppg run script.py progetto.ppg` (senza --project): il progetto arriva fra gli argomenti
        if (doc is None or not doc.chunks) and len(sys.argv) > 1 and sys.argv[1].endswith(".ppg"):
            doc = ppg.app.open_document(sys.argv[1])
    if doc is None or not doc.chunks:
        raise RuntimeError("Nessun progetto aperto (da terminale: ppg run controllo_rilievo.py --project progetto.ppg).")
    chunk = g.get("chunk") or doc.chunk
    if chunk is None:
        raise RuntimeError("Il progetto non ha un chunk attivo.")
    return doc, chunk


def _m(x, cifre=3):
    return "-" if x is None else f"{x:.{cifre}f}"


def _punti_ply(path: Path) -> int | None:
    """Numero di punti dall'intestazione della PLY, senza leggere la nuvola."""
    try:
        with open(path, "rb") as f:
            for _ in range(60):
                riga = f.readline().decode("ascii", "replace").strip()
                if riga.startswith("element vertex"):
                    return int(riga.split()[-1])
                if riga == "end_header":
                    break
    except OSError:
        return None
    return None


def _raster(path: Path) -> dict | None:
    """Risoluzione, dimensioni e copertura di un GeoTIFF letto da una panoramica ridotta."""
    try:
        import rasterio
    except ImportError:
        return None
    with rasterio.open(path) as ds:
        fattore = max(1, int(np.ceil(max(ds.width, ds.height) / 2000)))
        h, w = max(1, ds.height // fattore), max(1, ds.width // fattore)
        mask = ds.read_masks(1, out_shape=(h, w))
        info = {"risoluzione": abs(ds.res[0]), "larghezza": ds.width, "altezza": ds.height,
                "copertura": float((mask > 0).mean() * 100), "bande": ds.count}
        if ds.count == 1:
            dati = ds.read(1, out_shape=(h, w), masked=True)
            if dati.count():
                info["quota_min"] = float(dati.min())
                info["quota_max"] = float(dati.max())
        return info


def _nitidezza(chunk, progresso):
    from pyarchinit_photogrammetry.engine.quality import sharpness
    from pyarchinit_photogrammetry.engine.visibility import read_photo
    import cv2

    valori = {}
    camere = [c for c in chunk.cameras if c.enabled]
    for i, cam in enumerate(camere, 1):
        try:
            valori[cam.label] = sharpness(read_photo(cam, cv2.IMREAD_REDUCED_GRAYSCALE_2))
        except Exception:
            valori[cam.label] = None
        if i % 25 == 0:
            progresso(f"  nitidezza: {i} di {len(camere)} foto")
    noti = [v for v in valori.values() if v is not None]
    rif = float(np.percentile(noti, 90)) if noti else 0.0
    return {k: (None if v is None or rif <= 0 else min(1.0, v / rif)) for k, v in valori.items()}


def controlla(scrivi=print) -> Path:
    from pyarchinit_photogrammetry.markers import gcp

    doc, chunk = _documento_e_chunk()
    righe: list[str] = []
    avvisi: list[str] = []

    def r(testo=""):
        righe.append(testo)

    ora = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    nome = Path(doc.path).stem if doc.path else "progetto non salvato"
    r(f"# Controllo del rilievo - {chunk.label}")
    r(f"Progetto: {nome} - {ora} - sistema: {chunk.crs or 'locale'}")
    r()

    # ---------------------------------------------------------------- foto
    camere = chunk.cameras
    attive = [c for c in camere if c.enabled]
    allineate = chunk.aligned_cameras
    non_allineate = [c.label for c in attive if not c.aligned]
    disattivate = [c.label for c in camere if not c.enabled]
    sfm = chunk.meta.get("sfm", {})
    errore_px = sfm.get("reprojection_error")
    r("## Foto")
    r(f"- Foto: {len(camere)} (attive {len(attive)}, allineate {len(allineate)})")
    if errore_px is not None:
        r(f"- Errore medio di riproiezione: {errore_px:.2f} px")
        if errore_px > 1.5:
            avvisi.append(f"Errore di riproiezione alto ({errore_px:.2f} px): valuta la pulizia dei punti di legame "
                          "e un'ottimizzazione delle camere.")
    if non_allineate:
        r(f"- NON allineate ({len(non_allineate)}): {', '.join(non_allineate[:40])}"
          + (" ..." if len(non_allineate) > 40 else ""))
        if attive and len(non_allineate) / len(attive) > 0.05:
            avvisi.append(f"{len(non_allineate)} foto attive non allineate su {len(attive)}.")
    if disattivate:
        r(f"- Disattivate ({len(disattivate)}): {', '.join(disattivate[:40])}"
          + (" ..." if len(disattivate) > 40 else ""))
    r()

    # ---------------------------------------------------------------- GPS delle foto
    try:
        rif = gcp.camera_reference_errors(chunk)
    except Exception as exc:  # un chunk senza riferimenti utilizzabili
        rif = {}
        scrivi(f"(GPS delle foto non confrontabile: {exc})")
    errori_gps = [v["error"] for v in rif.values() if v.get("error") is not None]
    if errori_gps:
        r("## GPS delle foto")
        r(f"- Foto con GPS confrontate: {len(errori_gps)}")
        r(f"- Scarto stimato-GPS: mediana {np.median(errori_gps):.3f} m, massimo {max(errori_gps):.3f} m")
        r()

    # ---------------------------------------------------------------- marker
    marker = gcp.marker_errors(chunk) if chunk.markers else []
    cartella = (doc.files_dir / "controllo")
    cartella.mkdir(parents=True, exist_ok=True)
    data = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    csv_marker = cartella / f"marker_{chunk.key}_{data}.csv"
    if marker:
        r("## Marker")
        r("| Marker | Tipo | Foto | Errore (m) | dx | dy | dz | Errore (px) |")
        r("|---|---|---|---|---|---|---|---|")
        for e in marker:
            r(f"| {e['label']} | {'controllo' if e['type'] == 'control' else 'check'} | {e['projections']} | "
              f"{_m(e['error'])} | {_m(e['dx'])} | {_m(e['dy'])} | {_m(e['dz'])} | {_m(e['pixel_error'], 2)} |")
            if e["projections"] < MISURE_MINIME_MARKER:
                avvisi.append(f"Marker {e['label']}: misurato solo su {e['projections']} foto.")
            if e["error"] is not None and e["error"] > SOGLIA_ERRORE_MARKER_M:
                avvisi.append(f"Marker {e['label']} ({e['type']}): errore {e['error']:.3f} m.")
            if e["pixel_error"] is not None and e["pixel_error"] > SOGLIA_ERRORE_MARKER_PX:
                avvisi.append(f"Marker {e['label']}: errore di riproiezione {e['pixel_error']:.2f} px "
                              "(una collimazione da controllare?).")
        for tipo, nome_tipo in (("control", "controllo"), ("check", "check")):
            err = [e["error"] for e in marker if e["type"] == tipo and e["error"] is not None]
            if err:
                r(f"- RMSE dei punti di {nome_tipo}: {float(np.sqrt(np.mean(np.square(err)))):.4f} m ({len(err)} punti)")
        r()
        with open(csv_marker, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["marker", "tipo", "foto_misurate", "errore_m", "dx_m", "dy_m", "dz_m", "errore_px"])
            for e in marker:
                w.writerow([e["label"], e["type"], e["projections"], e["error"], e["dx"], e["dy"], e["dz"],
                            e["pixel_error"]])

    # ---------------------------------------------------------------- barre di scala
    barre = gcp.scalebar_errors(chunk) if chunk.scalebars else []
    if barre:
        r("## Barre di scala")
        for b in barre:
            r(f"- {b['label']} ({b['marker_a']}-{b['marker_b']}, {b['type']}): nota {_m(b['distance'], 4)} m, "
              f"misurata {_m(b['estimated'], 4)} m, differenza {_m(b['error'], 4)} m")
        r()

    # ---------------------------------------------------------------- prodotti
    r("## Prodotti")
    nuvola = chunk.product_path("point_cloud")
    if nuvola and nuvola.exists():
        n = _punti_ply(nuvola)
        r(f"- Nuvola densa: {n:,} punti".replace(",", ".") if n else "- Nuvola densa: presente")
    else:
        r("- Nuvola densa: non c'e'")
    for nome_prodotto, percorso in (("DEM", chunk.dem_path), ("Ortofoto", chunk.orthomosaic_path)):
        if percorso and Path(percorso).exists():
            info = _raster(Path(percorso))
            if info:
                testo = (f"- {nome_prodotto}: {info['risoluzione'] * 100:.2f} cm per pixel, "
                         f"{info['larghezza']} x {info['altezza']} pixel, copertura {info['copertura']:.0f} % del riquadro")
                if "quota_min" in info:
                    testo += f", quote da {info['quota_min']:.2f} a {info['quota_max']:.2f} m"
                r(testo)
            else:
                r(f"- {nome_prodotto}: presente")
        else:
            r(f"- {nome_prodotto}: non c'e'")
    modello = chunk.product_path("model")
    r(f"- Modello 3D: {'presente' if modello and modello.exists() else 'non c' + chr(39) + 'e' + chr(39)}")
    r()

    # ---------------------------------------------------------------- nitidezza (facoltativa)
    if CONTROLLA_NITIDEZZA and attive:
        scrivi(f"Misuro la nitidezza di {len(attive)} foto...")
        qualita = _nitidezza(chunk, scrivi)
        mosse = sorted((q, k) for k, q in qualita.items() if q is not None and q < SOGLIA_QUALITA_FOTO)
        r("## Nitidezza delle foto")
        r(f"- Foto sotto la qualita' {SOGLIA_QUALITA_FOTO}: {len(mosse)}")
        for q, k in mosse[:30]:
            r(f"  - {k}: {q:.2f}")
        if mosse:
            avvisi.append(f"{len(mosse)} foto poco nitide (vedi la sezione Nitidezza).")
        r()

    # ---------------------------------------------------------------- avvisi
    r("## Da controllare")
    if avvisi:
        for a in avvisi:
            r(f"- {a}")
    else:
        r("- Nessun problema trovato con le soglie impostate.")

    rapporto = cartella / f"controllo_{chunk.key}_{data}.md"
    rapporto.write_text("\n".join(righe) + "\n", encoding="utf-8")
    scrivi("\n".join(righe))
    scrivi("")
    scrivi(f"Rapporto: {rapporto}")
    if marker:
        scrivi(f"Marker (CSV): {csv_marker}")
    return rapporto


controlla()
