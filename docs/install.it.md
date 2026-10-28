# Installazione

## Applicazione desktop

Senza installare Python: il pulsante scarica l'ultima versione per il tuo sistema (macOS: il dmg; Windows: lo
zip a 64 bit, o quello per ARM quando il browser dichiara una macchina ARM); la freccia elenca tutte le versioni.

<div class="ppg-download" data-ppg-download>
<a class="md-button md-button--primary ppg-download__main" href="https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest" data-ppg-main data-ppg-prefix="Scarica per ">Scarica</a><button class="md-button md-button--primary ppg-download__toggle" type="button" aria-haspopup="true" aria-expanded="false" aria-label="Altri sistemi" data-ppg-toggle>&#9662;</button>
<ul class="ppg-download__menu" data-ppg-menu hidden>
<li><a href="https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest/download/PyArchInit-Photogrammetry-macos-arm64.dmg" download data-ppg-system="macos" data-ppg-asset="macos-arm64.dmg" data-ppg-label="macOS (Apple Silicon)">macOS 15 o più recente, Apple Silicon (.dmg)</a></li>
<li><a href="https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest/download/PyArchInit-Photogrammetry-windows-x64.zip" download data-ppg-system="windows-x64" data-ppg-asset="windows-x64.zip" data-ppg-label="Windows (64 bit)">Windows 10/11, 64 bit (.zip)</a></li>
<li><a href="https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest/download/PyArchInit-Photogrammetry-windows-arm64.zip" download data-ppg-system="windows-arm64" data-ppg-fallback="windows-x64" data-ppg-asset="windows-arm64.zip" data-ppg-label="Windows on ARM">Windows 11 su ARM, Snapdragon (.zip)</a></li>
<li><a href="https://github.com/enzococca/pyarchinit-photogrammetry/releases">Tutte le versioni su GitHub</a></li>
</ul>
</div>

Tutte le versioni sono nella [pagina delle release](https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest).
Se l'applicazione ti è utile, [sostieni il progetto su PayPal](https://paypal.me/enzococca): è il lavoro di una persona
sola.

| Sistema | File | |
|---|---|---|
| macOS 15 o successivo, Apple Silicon | `PyArchInit-Photogrammetry-<versione>-macos-arm64.dmg` | aprila e trascina l'app in *Applicazioni* |
| Windows 10/11, 64 bit | `PyArchInit-Photogrammetry-<versione>-windows-x64.zip` | estrai la cartella e avvia `PyArchInit Photogrammetry.exe` |
| Windows 11 su ARM (Snapdragon, macchine virtuali ARM64) | `PyArchInit-Photogrammetry-<versione>-windows-arm64.zip` | versione nativa (dalla 1.1.0; vista 3D VTK e scansioni E57 dalla 1.3.5, renderer software dove Windows non ha OpenGL 3.2); anche lo zip x64 gira, in emulazione |

Le versioni non sono firmate. Su macOS, la prima volta, tasto destro sull'app > *Apri* (oppure
`xattr -dr com.apple.quarantine "/Applications/PyArchInit Photogrammetry.app"`); su Windows *Ulteriori
informazioni > Esegui comunque*. All'avvio l'applicazione controlla se ci sono versioni nuove (al
massimo una volta al giorno) e, su macOS, le installa e si riavvia.

La finestra si apre su una pagina con *Nuovo progetto*, *Apri progetto* e *Progetto demo* (da lì
partono i [tutorial](tutorials/index.md)). L'interfaccia è in italiano o in inglese
(*Vista > Lingua / Language*). Se PyArchInit 5 è installato (`~/pyarchinit_5`), all'avvio
l'applicazione si collega al suo database.

## Con pip

Python 3.10-3.12, in un ambiente virtuale dedicato (non il `base` di Anaconda). Con
[uv](https://docs.astral.sh/uv/):

```bash
uv venv --seed --python 3.12 ~/venvs/ppg && source ~/venvs/ppg/bin/activate
uv pip install "pyarchinit-photogrammetry[all]"
```

oppure con gli strumenti standard:

```bash
python3 -m venv ~/venvs/ppg && source ~/venvs/ppg/bin/activate   # Windows: ~\venvs\ppg\Scripts\activate
pip install -U pip && pip install "pyarchinit-photogrammetry[all]"
```

`[all]` comprende modelli (Open3D), IFC, PostgreSQL, interfaccia desktop, scansioni E57 e colori
Munsell; `[sam]` aggiunge Segment Anything 2.1 (PyTorch). Il nucleo nativo in Rust (mappe di
profondità e ortofoto molto più veloci) si costruisce dai sorgenti (sotto); le applicazioni desktop
lo contengono già. Il PPK delle posizioni GNSS richiede il nucleo 0.5 o più recente, che contiene
RTKLIB, e i file RINEX compressi con Hatanaka (`.crx`, `.##d`, come li danno gli archivi delle stazioni
permanenti) il nucleo 0.6 o più recente (`pip install "pyarchinit-photogrammetry-core>=0.6"`), o il
programma `CRX2RNX` di RNXCMP nel `PATH`; senza, il programma lo dice, e `ppg doctor` dice dove gira il PPK.

```bash
ppg doctor                       # controlla l'ambiente e dice cosa sistemare
ppg demo ~/ppg_demo --process    # i progetti dimostrativi, elaborati
ppg-gui ~/ppg_demo/scavo.ppg     # la finestra sul saggio dimostrativo
```

### Linux

Su un server senza schermo Open3D ha bisogno delle librerie EGL/GL
(`sudo apt-get install libegl1 libgl1 libgomp1`); per scrivere in un database PyArchInit SQLite
serve SpatiaLite (`sudo apt-get install libsqlite3-mod-spatialite`).

Su ARM (aarch64) pycolmap e Open3D non pubblicano wheel: installa prima quelle allegate alla
[release](https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest) per il tuo Python,
poi il pacchetto:

```bash
pip install pycolmap-*-cp312-*aarch64.whl open3d-*-cp312-*aarch64.whl
sudo apt-get install libxerces-c-dev       # per le scansioni E57 di [all]
pip install "pyarchinit-photogrammetry[all]"
```

### macOS e Windows

SpatiaLite viene trovato in QGIS, Homebrew (`brew install libspatialite`) o MacPorts; altrimenti
imposta `SPATIALITE_LIBRARY_PATH` sul file `mod_spatialite`. Per avere `ppg`/`ppg-gui` sempre
disponibili senza attivare nulla: `uv tool install "pyarchinit-photogrammetry[all]"`.

## Dai sorgenti

```bash
git clone https://github.com/enzococca/pyarchinit-photogrammetry
cd pyarchinit-photogrammetry
uv venv --seed --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[all,dev]"
uv pip install ./native        # facoltativo: il nucleo nativo (serve Rust)
pytest -q
```

## Dati GNSS

Il lavoro GNSS scarica solo quello che serve a un calcolo, da archivi pubblici che non chiedono un account, e lo
tiene nella cartella GNSS del programma: `~/Library/Application Support/pyarchinit-photogrammetry/gnss` su
macOS, `%LOCALAPPDATA%\pyarchinit-photogrammetry\gnss` su Windows,
`~/.local/share/pyarchinit-photogrammetry/gnss` su Linux (`PPG_GNSS_DIR` ne indica un'altra). Lì ci sono gli
elenchi e i file delle stazioni permanenti, i prodotti precisi di un PPP, le calibrazioni delle antenne
igs20.atx (62 MB) e le griglie del geoide ([PPK](gui/photos-markers.md#ppk-correzioni-da-una-base)).

* **Il geoide EGM2008** (`us_nga_egm08_25.tif`, 81 MB, dominio pubblico), che serve alle quote sul livello del
  mare, si scarica solo se lo chiedi: *Scarica il geoide EGM2008 (81 MB)...* nel passo *Sistema e quote* del
  pannello GNSS, o `ppg gnss geoid --download`. Senza, una conversione in quote sul geoide si rifiuta: le quote
  sull'ellissoide non passano mai per quote sul livello del mare.
* **Una griglia tua**, come ITALGEO2005 dell'IGM (`geo_igm_mar06.grd`), va nella cartella `proj` della
  cartella GNSS, che `ppg gnss geoid` stampa: PROJ cerca le griglie anche lì.
* PROJ non va mai in rete da solo, anche quando la shell imposta `PROJ_NETWORK=ON`: una griglia si usa solo
  quando è sul disco.

Di più in [Quote sul geoide e sistema del rilievo](gui/photos-markers.md#quote-sul-geoide-e-sistema-del-rilievo).

## Quando qualcosa non va

* `ppg doctor` (nella finestra: *Aiuto > Diagnostica ambiente*) elenca versioni e problemi noti.
* Il registro della finestra: *Aiuto > Apri cartella dei log*.
* `ppg-gui` non fa nulla: una copia vecchia installata in un altro Python viene prima nel `PATH`
  (`which ppg-gui` deve indicare un file dentro l'ambiente).
* Altro in [Pacchetti](packaging.md) (in inglese) e nel
  [README](https://github.com/enzococca/pyarchinit-photogrammetry/blob/main/README.it.md).
