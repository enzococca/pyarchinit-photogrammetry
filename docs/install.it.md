# Installazione

## Applicazione desktop

Senza installare Python: scarica la versione per il tuo sistema dall'[ultima
release](https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest).

| Sistema | File | |
|---|---|---|
| macOS 15 o successivo, Apple Silicon | `PyArchInit-Photogrammetry-<versione>-macos-arm64.dmg` | aprila e trascina l'app in *Applicazioni* |
| Windows 10/11, 64 bit | `PyArchInit-Photogrammetry-<versione>-windows-x64.zip` | estrai la cartella e avvia `PyArchInit Photogrammetry.exe` |
| Windows 11 su ARM | lo zip per Windows | gira in emulazione |

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
lo contengono già.

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

## Quando qualcosa non va

* `ppg doctor` (nella finestra: *Aiuto > Diagnostica ambiente*) elenca versioni e problemi noti.
* Il registro della finestra: *Aiuto > Apri cartella dei log*.
* `ppg-gui` non fa nulla: una copia vecchia installata in un altro Python viene prima nel `PATH`
  (`which ppg-gui` deve indicare un file dentro l'ambiente).
* Altro in [Pacchetti](packaging.md) (in inglese) e nel
  [README](https://github.com/enzococca/pyarchinit-photogrammetry/blob/main/README.it.md).
