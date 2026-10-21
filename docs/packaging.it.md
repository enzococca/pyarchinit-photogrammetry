# Applicazione desktop impacchettata

Le versioni pronte all'uso dell'interfaccia, con dentro Python e tutte le librerie, le produce
PyInstaller:

| Sistema | File | Avvio |
| --- | --- | --- |
| macOS 15 o successivo (Apple Silicon) | `PyArchInit-Photogrammetry-<versione>-macos-arm64.dmg` | apri l'immagine disco e trascina l'app in *Applicazioni* |
| Windows 10/11 (64 bit) | `PyArchInit-Photogrammetry-<versione>-windows-x64.zip` | estrai lo zip e avvia `PyArchInit Photogrammetry.exe` |

Sono allegate a ogni [release su GitHub](https://github.com/enzococca/pyarchinit-photogrammetry/releases)
e le produce il workflow `desktop` (`.github/workflows/desktop.yml`, runner `macos-15`
e `windows-latest`). L'applicazione impacchettata contiene lo stesso codice del pacchetto
`pip`: Open3D, VTK, qtawesome e IfcOpenShell sono inclusi; Segment Anything (PyTorch)
no, e viene installato al primo uso in un ambiente separato (vedi
[integrations.md](integrations.md#segment-anything-21-in-the-packaged-application)).

## Primo avvio

* **macOS**: l'applicazione non è firmata con un certificato di sviluppatore Apple. Gatekeeper
  mostra *"Impossibile aprire ... perché non è possibile verificare lo sviluppatore"* (o *"è
  danneggiata"* sui sistemi più recenti). Tasto destro sull'app e *Apri* una volta, oppure togli
  l'attributo di quarantena dal terminale:

  ```bash
  xattr -dr com.apple.quarantine "/Applications/PyArchInit Photogrammetry.app"
  ```

* **Windows**: SmartScreen può chiedere una conferma la prima volta (*Ulteriori informazioni > Esegui comunque*).
  Per scrivere geometrie in un database SQLite di PyArchInit serve `mod_spatialite.dll`: viene trovata
  in un'installazione di QGIS o di OSGeo4W, oppure imposta `SPATIALITE_LIBRARY_PATH`.

* **Windows su ARM** (portatili Snapdragon, Windows 11 in Parallels su un Mac con Apple Silicon):
  l'applicazione x64 gira nell'emulazione di Windows 11 - provata su Windows 11 ARM64 con
  `--check` e `--doctor` (Python, pycolmap, Open3D, OpenCV, rasterio, Qt e VTK si caricano) e
  con la finestra.

I log vengono scritti in `~/Library/Logs/pyarchinit-photogrammetry` (macOS) o in
`%LOCALAPPDATA%\pyarchinit-photogrammetry\logs` (Windows); *Aiuto > Apri cartella dei log*
apre la cartella, *Aiuto > Diagnostica ambiente* esegue gli stessi controlli di `ppg doctor`.

L'applicazione esegue anche script Python, con il suo Python e i suoi moduli e senza la
finestra, come fa Metashape con `-r` (il codice d'uscita è quello dello script):

```bash
"/Applications/PyArchInit Photogrammetry.app/Contents/MacOS/PyArchInit Photogrammetry" --run script.py args
"PyArchInit Photogrammetry.exe" --run script.py args > out.txt 2>&1    # Windows: niente console, ridirigi l'output
```

## Costruire in locale

```bash
pip install -e ".[all,build]"
pip install ./native                 # kernel nativi (serve la toolchain Rust), inclusi nell'app se installati
python packaging/build.py            # icone, PyInstaller, smoke test (--check), dmg/zip in dist/
python packaging/build.py --no-smoke
```

* `packaging/ppg_gui.spec`: lo spec di PyInstaller (build in una cartella, con finestra, gli hook per
  i dati di rasterio, pyproj, Open3D, VTK, qtawesome e pycolmap, esclusi i moduli Qt che non si
  usano).
* `packaging/launcher.py`: il punto d'ingresso; chiama per prima cosa `multiprocessing.freeze_support()`,
  così i processi di lavoro isolati (ricostruzione di Poisson) funzionano nell'applicazione impacchettata.
* `packaging/build.py`: disegna le icone da `gui/resources/logo.svg`, lancia PyInstaller,
  riporta a `@loader_path` i riferimenti alle librerie private di ogni pacchetto
  (macOS: rasterio e pyproj portano build diverse di `libproj` con lo stesso nome, e
  PyInstaller altrimenti gliene farebbe condividere una copia sola), avvia il risultato con `--check`
  (offscreen) e `--doctor`, poi scrive il `.dmg` (`hdiutil`) o lo `.zip`.

Lo smoke test apre la finestra principale, la disegna ed esce con codice 0; la build fallisce
quando la finestra non compare. La sua riga dice anche se dentro c'è il nucleo nativo
(`native: 0.1.0`).

## Nucleo nativo (Rust)

`native/` contiene `pyarchinit-photogrammetry-core`, importato come `ppg_core`: i kernel che il
codice Python eseguiva in numpy e che dominavano il tempo di CPU delle mappe di profondità e
dell'ortofoto - fusione delle stime di profondità di una foto, dalla disparità alla profondità, normali,
proiezione dei punti con i modelli di camera di COLMAP, punteggio delle foto di un tassello
dell'ortofoto, accordo dei punti con una mappa di profondità (confidenza). PyO3 + rayon, una wheel abi3 per
piattaforma per Python 3.10 e successivi. È facoltativo: senza (o con `PPG_NATIVE=0`) gira il
codice numpy, e `tests/test_native.py` controlla che i due diano le stesse mappe, gli stessi pixel e gli
stessi conteggi. Misurato sulla trincea sintetica, mappe di profondità su CPU di 48 foto: 5,7 s con numpy una
foto alla volta, 1,6 s con i kernel nativi, 0,9 s con sei foto alla volta.

Dalla 0.2.0 il nucleo contiene anche lo stereo multi-vista PatchMatch (`patchmatch_depth`, il backend delle
mappe di profondità `patchmatch`, che la scelta automatica usa per i rilievi aerei: foto le cui posizioni
GPS si distribuiscono sul sito). Non ha una versione numpy (sarebbe troppo lenta): senza il nucleo
`auto` ripiega sullo SGBM a due viste, il backend non viene offerto nella finestra e il passo si ferma con un
messaggio quando lo si chiede per nome.

```bash
pip install ./native                         # costruisce e installa (maturin lo scarica pip)
maturin build --release -m native/Cargo.toml # una wheel in native/target/wheels
PPG_NATIVE=0 ppg ...                         # il codice numpy, per confronto
```

Il profilo di release conserva i simboli: la libreria spogliata dei simboli dal profilo di release predefinito
di Rust viene rifiutata dal loader dinamico di macOS 26/27 ("mis-aligned LINKEDIT string pool"). Il
workflow `native` costruisce le wheel (macOS arm64 e x86-64, Windows x64, Linux x86-64 e
aarch64) e, su un tag `core-v*`, le pubblica su PyPI; il workflow `tests` esegue Python 3.12
con il nucleo nativo e 3.11 senza; il workflow `desktop` lo include nelle applicazioni.


## Linux su ARM (aarch64): pycolmap e Open3D

pycolmap 4.2.1 e Open3D 0.19.0 non pubblicano wheel per Linux su ARM (Open3D 0.20.0 sì, ma la sua
ricostruzione di Poisson va in crash e i requisiti la escludono). `packaging/arm/build-linux.sh`
le costruisce in Docker su una macchina arm64 - un Mac con Apple Silicon, un host Linux arm64 - e
il workflow `arm-wheels` fa lo stesso sui runner arm64 di GitHub, poi esegue con loro i test di questo
progetto (a richiesta; le wheel restano come artifact, o vengono allegate alla release di cui si dà
il tag).

```bash
packaging/arm/build-linux.sh                       # entrambe, Python 3.10-3.12, wheel in dist/arm
PYTHONS=3.12 JOBS=3 packaging/arm/build-linux.sh pycolmap
packaging/arm/build-linux.sh test                  # i test con loro (Ubuntu 24.04, Python 3.12)
pip install dist/arm/pycolmap-*-cp312-*.whl dist/arm/open3d-*-cp312-*.whl
pip install "pyarchinit-photogrammetry[all]"
```

* pycolmap: la ricetta di COLMAP per le sue wheel x86-64 (l'immagine manylinux_2_28, vcpkg, GCC 12)
  con un triplet arm64; wheel `manylinux_2_28_aarch64` (glibc 2.28 o successiva).
* Open3D: la ricetta arm64 di Open3D (Ubuntu 20.04, OpenBLAS, VTK ed Embree costruiti dai sorgenti)
  senza la sua GUI nuova (Filament), che questo programma non usa; wheel `manylinux_2_31_aarch64`
  (glibc 2.31 o successiva), nome del pacchetto `open3d`.
* Il lavoro va in cartelle dell'host, non nel disco di Docker (che tiene solo le due
  immagini di build); su un Mac in un'immagine disco case-sensitive, `build/arm.sparseimage`, perché
  i pacchetti Linux contengono nomi che differiscono solo per maiuscole e minuscole (`hdiutil detach build/arm`
  la chiude).
* Alcuni file di COLMAP e Open3D hanno bisogno di parecchi GB ciascuno per compilare: `JOBS` compilatori
  alla volta (3 per impostazione predefinita, per un Docker da 8 GB), quelli che esauriscono la memoria
  compilati di nuovo da soli.
* Open3D su ARM collega la `libgfortran` condivisa (messa nella wheel da auditwheel): con quella
  statica, come fa la build ARM di Open3D stessa, il modulo non si importa ("cannot allocate memory in
  static TLS block").
* Anche `pye57` (scansioni E57, in `[all]`) non ha wheel per aarch64: viene compilato all'installazione e
  ha bisogno degli header di Xerces-C (`sudo apt install libxerces-c-dev` su Debian e Ubuntu).


## Firma, notarizzazione, TestFlight

Le build non sono firmate. Le vie Apple sono due:

* **Developer ID + notarizzazione** (download dal web/GitHub, nessun avviso): serve un
  certificato *Developer ID Application* (creato dall'Account Holder del team Apple
  Developer) e un profilo per il notary (`xcrun notarytool store-credentials`). Poi
  `PPG_CODESIGN_IDENTITY="Developer ID Application: ... (TEAMID)" PPG_NOTARY_PROFILE=<profile> python packaging/build.py`
  firma ogni binario con l'hardened runtime (`packaging/entitlements.plist`), firma e
  notarizza il dmg e vi allega (staple) il biglietto.
* **TestFlight / Mac App Store**: servono un certificato *Apple Distribution*, un profilo di provisioning
  e l'**App Sandbox**. La sandbox vieta ciò su cui questa applicazione si basa:
  caricare `mod_spatialite` da Homebrew/QGIS, avviare processi di lavoro da altri
  interpreti (Poisson isolata, ambiente SAM), scaricare ed eseguire `uv`, scrivere
  accanto ai progetti dell'utente senza una finestra di dialogo, aprire QGIS. Un'edizione con sandbox
  richiederebbe di riprogettare quelle parti, quindi il canale consigliato è Developer ID.
