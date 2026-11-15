# Limiti per sistema

Il programma è lo stesso su ogni sistema, ma non tutte le sue parti ci sono ovunque: alcune librerie non hanno
una versione per un sistema, alcuni strumenti di altri produttori esistono solo per alcuni, e non tutti i
sistemi sono provati allo stesso modo. Questa pagina elenca quello che si incontra su ciascuno, alla versione
1.10.1, e come aggirarlo quando si può. `ppg doctor` (nella finestra: *Aiuto > Diagnostica ambiente*) dice che
cosa ha questo computer.

## Riepilogo

| | macOS, Apple Silicon | macOS, Intel | Windows x64 | Windows su ARM64 | Linux x86-64 | Linux aarch64 |
|---|---|---|---|---|---|---|
| Applicazione desktop | sì (dmg, firmata e notarizzata) | no: pip | sì (zip) | sì (zip, nativa dalla 1.1.0) | no: pip | no: pip |
| Installazione con pip | sì | sì, non provata | sì | non con le sole wheel di PyPI | sì | con le wheel di pycolmap e Open3D del progetto |
| Aggiornamenti dalla finestra | scaricati, installati, riavvio | comando pip | zip scaricato, da sostituire a mano | zip scaricato, da sostituire a mano | comando pip | comando pip |
| Nucleo nativo su PyPI (velocità, PatchMatch, PPK) | sì | sì, non provato | sì | nell'applicazione; pip: dai sorgenti | sì | sì, non provato |
| GPU del programma (wgpu) | Metal | Metal, non provata | DirectX 12, Vulkan | DirectX 12, Vulkan | Vulkan | Vulkan |
| Mappe di profondità di COLMAP su CUDA | no | no | no | no | solo con una build CUDA di pycolmap | no |
| SAM 2.1 gira su | GPU Apple (MPS) | non verificato | GPU NVIDIA (CUDA), se no CPU | CPU | GPU NVIDIA (CUDA), se no CPU | non verificato |
| SpatiaLite (geometrie in un database SQLite di PyArchInit) | QGIS, Homebrew, MacPorts | QGIS, Homebrew, MacPorts | QGIS, OSGeo4W | **no** (PostGIS funziona) | apt | apt |
| Termico R-JPEG DJI (DJI Thermal SDK) | in Docker Desktop | in Docker Desktop | nativo | non verificato | nativo | non verificato |
| Vista 3D VTK | sì | sì | sì | sì dalla 1.3.5, serve OpenGL 3.2 | sì | sì |
| Scansioni E57 | sì | sì | sì | sì dalla 1.3.5 | sì | compilate all'installazione |
| File raw delle fotocamere (rawpy) | sì | con pip | sì | **non nell'applicazione**; pip con Python 3.11+ | sì | con pip |
| Prove nella CI del progetto | l'applicazione viene costruita e avviata | nessuna | tutti i test, applicazione costruita e avviata | applicazione costruita e avviata, test a richiesta | tutti i test | test a richiesta |

## Su tutti i sistemi

* **Python da 3.10 a 3.12** per un'installazione con pip, in un ambiente virtuale dedicato (non il `base` di
  Anaconda, le cui librerie compilate - SciPy, VTK - possono non caricarsi o andare in crash: `ppg doctor` lo
  segnala). Il pacchetto non rifiuta un Python più recente, ma nessuno è provato: i test automatici girano con
  Python 3.11 e 3.12 su Linux e con 3.12 su Windows; la 3.10 lì non gira.
* **Open3D 0.20.0 è escluso**: la sua ricostruzione Poisson va in crash su alcuni sistemi. pip prende Open3D 0.18
  o 0.19, e pycolmap 4.2; le versioni nuove arrivano a chi usa pip solo dopo essere state provate
  ([Pacchetti](packaging.md#nuove-versioni-di-pycolmap-e-open3d)).
* **Senza il nucleo nativo** (un'installazione con pip senza `pyarchinit-photogrammetry-core`) mappe di profondità e
  ortofoto sono parecchie volte più lente, non c'è il PatchMatch né la GPU, il PPK delle posizioni GNSS richiede il
  programma `rnx2rtkp` di RTKLIB indicato da `PPG_RNX2RTKP`, e i file Hatanaka delle stazioni permanenti il
  programma `CRX2RNX`. Le applicazioni desktop contengono il nucleo;
  `pip install "pyarchinit-photogrammetry[fast]"` lo aggiunge dove PyPI ha una wheel (vedi la tabella).
* **Le mappe di profondità di COLMAP richiedono CUDA** (NVIDIA): le wheel di pycolmap su PyPI per macOS e Windows
  non lo hanno, né quelle che il progetto costruisce per ARM. Le mappe di profondità del programma (SGBM,
  PatchMatch su CPU e su GPU con wgpu) girano ovunque; senza un dispositivo Metal, Vulkan o DirectX 12
  l'abbinamento gira sulla CPU.
* **Segment Anything e i punti chiave appresi** non sono dentro le applicazioni: al primo uso PyTorch viene
  installato in un ambiente separato (1-2 GB, scaricati).
* **La coregistrazione dei DEM con xdem** non è mai nelle applicazioni: solo
  `pip install "pyarchinit-photogrammetry[coreg]"`.
* **QGIS** viene cercato nei posti soliti: `/Applications/QGIS*.app` su macOS, `C:\Program Files\QGIS*` o
  `C:\OSGeo4W` su Windows, `/usr/bin/qgis` o `/usr/local/bin/qgis` su Linux. Altrove imposta `PPG_QGIS`.
* **Le chiavi dell'assistente** stanno nel portachiavi del sistema; senza (un server Linux senza sessione grafica)
  imposta `ANTHROPIC_API_KEY` o `OPENAI_API_KEY` prima di avviare il programma
  ([Errori e aggiornamenti](gui/help.md)).
* **Strumenti > Apri un terminale** (dalla 1.10.1) apre il terminale del sistema, non uno dentro il programma:
  Terminal.app su macOS (sempre Terminal, anche se il predefinito è un altro), Windows Terminal o altrimenti una nuova
  console `cmd` su Windows, su Linux `$TERMINAL` o il primo fra `x-terminal-emulator`, `gnome-terminal`, `konsole`,
  `xfce4-terminal`, `mate-terminal`, `tilix`, `kitty`, `alacritty`, `wezterm`, `lxterminal`, `xterm` (se non ne
  trova nessuno lo dice). Da un'installazione pip `ppg` viene per primo nel PATH. **Le applicazioni non hanno il
  comando `ppg`**: il loro terminale offre `ppg-run script.py` (lo script eseguito dall'applicazione, come `--run`) e
  `ppg-doctor`, e `PPG_APP` indica l'eseguibile; su Windows il loro output compare quando finiscono. I file di avvio
  della shell (`.zshrc`, `.bashrc`, l'inizializzazione di Anaconda) possono comunque mettere altre cartelle, e un
  altro `ppg`, prima: lo dice `which ppg`.

## macOS su Apple Silicon

* **L'applicazione richiede macOS 15 o successivo** (le librerie di rasterio che contiene sono costruite per quello).
* **La dmg è firmata con un Developer ID e notarizzata da Apple** (dalla 1.1.0): si apre senza avvisi. Una dmg
  costruita dal workflow GitHub del progetto non è firmata; quella allegata a ogni release sì.
* **Gli aggiornamenti si installano da soli**: la nuova versione viene scaricata, messa al suo posto e il
  programma si riavvia.
* **Foto termiche R-JPEG di DJI**: DJI pubblica il Thermal SDK solo per Windows e Linux. Sul Mac il programma ne
  esegue la versione Linux x86-64 in **Docker Desktop**, che deve essere installato e avviato (l'immagine viene
  preparata la prima volta). I JPEG FLIR e i TIFF termici non richiedono nulla.
* **Il PatchMatch di COLMAP** (CUDA) sul Mac non esiste: al suo posto il PatchMatch del nucleo nativo, sulla CPU e
  sulla GPU Apple.
* **SpatiaLite** viene da QGIS, Homebrew (`brew install libspatialite`) o MacPorts; altrimenti imposta
  `SPATIALITE_LIBRARY_PATH`.
* **Non coperto dai test automatici**: su macOS la CI del progetto costruisce l'applicazione e la avvia (finestra e
  diagnostica), ma i test girano solo su Linux e Windows.

## macOS su Intel

* **Nessuna applicazione**: la dmg è solo per Apple Silicon, e il controllo degli aggiornamenti non trova un
  installatore per questo sistema. Si installa con pip (Python 3.10-3.12).
* Il nucleo nativo ha una wheel per i Mac Intel su PyPI, costruita ma **non provata**; nient'altro è provato sui
  Mac Intel.
* SpatiaLite da Homebrew (`/usr/local/lib`), MacPorts o QGIS; foto termiche DJI in Docker Desktop, come su Apple
  Silicon.

## Windows x64

* **Lo zip non è firmato**: SmartScreen può chiedere conferma la prima volta (*Ulteriori informazioni > Esegui
  comunque*).
* **Gli aggiornamenti non li installa il programma**: scarica il nuovo zip nella cartella Download; chiudi il
  programma, estrai lo zip e sostituisci la cartella del programma.
* **SpatiaLite** viene trovato in un'installazione di QGIS o di OSGeo4W (`C:\Program Files\QGIS*`, `C:\OSGeo4W`),
  oppure imposta `SPATIALITE_LIBRARY_PATH` su `mod_spatialite.dll`. Senza, manca solo la scrittura di geometrie in
  un database SQLite di PyArchInit; PostGIS non ne risente.
* **Il PPK** usa RTKLIB, che apre i file con le funzioni ANSI di Windows: un progetto in una cartella il cui percorso
  ha lettere fuori dall'ASCII semplice (accenti, altri alfabeti) funziona solo se Windows dà a quella cartella un
  nome breve (8.3); altrimenti il PPK si ferma e chiede di spostare il progetto in un percorso ASCII.
* **Script con `--run`**: l'applicazione non ha una console, quindi reindirizza l'output
  (`"PyArchInit Photogrammetry.exe" --run script.py > out.txt 2>&1`).
* Il PatchMatch di COLMAP richiede CUDA, che il pycolmap di PyPI per Windows non ha; SAM 2.1 installa la versione
  CUDA di PyTorch quando c'è un driver NVIDIA, altrimenti quella per CPU.

## Windows su ARM64

* **L'applicazione ARM64 nativa** (dalla 1.1.0; computer Snapdragon, macchine virtuali ARM64) è costruita con
  librerie che il progetto compila da sé (lazrs, OpenCV, pycolmap, Open3D, VTK, pye57). Anche l'applicazione x64
  gira, nell'emulazione di Windows 11.
* **Niente SpatiaLite**: la sua unica versione per Windows arriva con QGIS, che è x64, e un programma ARM64 non
  può caricarla. La scrittura di geometrie in un database **SQLite** di PyArchInit non è disponibile; **PostGIS**
  funziona (con psycopg 3).
* **Niente file raw delle fotocamere nell'applicazione**: è costruita senza rawpy, quindi DNG, CR2, NEF, ARW...
  non si possono aggiungere; esporta le foto in JPEG o TIFF. Con pip, rawpy qui richiede Python 3.11 o successivo.
* **La vista 3D VTK richiede OpenGL 3.2** (dalla 1.3.5): i computer Snapdragon lo hanno dal driver grafico o dal
  *OpenCL, OpenGL, and Vulkan Compatibility Pack* di Microsoft (Microsoft Store). Dove Windows offre solo OpenGL 1.1
  (macchine virtuali senza driver 3D) il programma usa il renderer software, e la diagnostica dice quale OpenGL
  ha trovato.
* **SAM 2.1 gira sulla CPU**: PyTorch per Windows su ARM ha solo versioni per CPU.
* **pip**: pycolmap, Open3D, OpenCV e lazrs non pubblicano wheel per Windows ARM64 su PyPI; il progetto le
  costruisce solo per l'applicazione. Usa l'applicazione ARM64, o quella x64.
* OpenCV nell'applicazione contiene solo i moduli che il programma usa: un plugin che ne vuole un altro può non
  funzionare lì.
* Zip non firmato, aggiornamenti da sostituire a mano, percorsi ASCII per il PPK: come su Windows x64.

## Linux x86-64

* **Nessuna applicazione**: si installa con pip (Python 3.10-3.12). Il controllo degli aggiornamenti dà il
  comando pip.
* **Su un server senza schermo** Open3D richiede le librerie EGL/GL:
  `sudo apt-get install libegl1 libgl1 libgomp1`.
* **SpatiaLite**: `sudo apt-get install libsqlite3-mod-spatialite`.
* **Il PatchMatch di COLMAP su CUDA** solo con una build CUDA di pycolmap e una GPU NVIDIA (`ppg doctor` lo mostra
  alla riga "COLMAP GPU"). I kernel GPU del programma usano Vulkan.
* Le chiavi dell'assistente richiedono il Secret Service del desktop (GNOME Keyring, KWallet); su un server usa le
  variabili d'ambiente.

## Linux aarch64

* **Nessuna applicazione**: solo pip.
* **pycolmap 4.2.1 e Open3D 0.19.0 non pubblicano wheel per Linux su ARM**: installa prima del pacchetto quelle che
  costruisce il progetto (allegate alle sue release; le costruisce `packaging/arm/build-linux.sh`). Richiedono
  glibc 2.28 (pycolmap) e 2.31 (Open3D) o successive, e non hanno CUDA.
* **Scansioni E57**: pye57 non ha una wheel ARM e viene compilato all'installazione; servono gli header di Xerces-C
  (`sudo apt-get install libxerces-c-dev`).
* Il nucleo nativo ha una wheel aarch64 su PyPI, costruita ma **non provata** nella CI; i test con le wheel ARM del
  progetto girano solo a richiesta.

## Plugin

Alcuni plugin si appoggiano a librerie o programmi di altri produttori che non esistono su ogni sistema. Le loro
guide lo dicono per intero; in breve:

| Plugin | Che cosa manca, e dove |
|---|---|
| [Elementi architettonici](plugins/architecture.md) | i solidi (OpenCascade): Windows su ARM |
| [Mesh (MeshLab)](plugins/mesh-tools.md) | gli strumenti di MeshLab (pymeshlab): Windows su ARM; Linux richiede librerie di sistema |
| [Paesaggio nel tempo](plugins/landscape.md) | le simulazioni (Landlab): Windows su ARM; i Mac Intel richiedono macOS 15 |
| [Geofisica](plugins/geophysics.md) | la tomografia di resistività (pyGIMLi): Windows e Linux su ARM; macOS richiede la 26 o successiva |
| [Terreno](plugins/terrain.md) | WhiteboxTools: nessuna versione per Linux su ARM (installalo e imposta `PPG_WBT`); Windows su ARM usa quella x64 |
| [Spirula](plugins/spirula.md) | Spirula Studio: nessuna versione per i Mac Intel né per i computer ARM con Windows o Linux |
| [Piani di volo](plugins/flightplan.md) | la copia in DJI Fly via MTP su Windows non è ancora provata su un radiocomando vero; su un Mac con l'RC 2, trascina le cartelle esportate |
