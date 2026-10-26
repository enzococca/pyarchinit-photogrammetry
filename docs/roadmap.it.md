# Roadmap

## 0.2 - Interfaccia desktop (fatto)

Albero dell'area di lavoro, vista 3D (VTK o software), vista ortofoto/DEM con gli strumenti di disegno, vista
foto con posizionamento dei marker e prompt SAM, pannello Riferimenti, console Python, elaborazione in
background con annullamento, menu PyArchInit, dati dimostrativi (`ppg demo`).

## 0.3 - Versione desktop robusta (fatto)

Icone colorate (qtawesome), tema chiaro/scuro, pagina iniziale, pannello del log con filtri e
copia/salvataggio, finestre di errore con suggerimenti e traceback, `ppg doctor` / *Aiuto > Diagnostica*,
file di log e di crash, finestre di messaggio non native (macOS 26/27), ricostruzione di Poisson in un
processo isolato con nuovi tentativi e ripiego sul campo di altezze, Open3D 0.20.0 escluso, dati PROJ
ridiretti a quelli di rasterio nei thread di lavoro, applicazioni impacchettate per macOS e Windows
(PyInstaller, workflow `desktop`).

## 0.4 - Integrazioni (fatto)

Attributi dei layer modificabili (riscritti in PyArchInit), progetto QGIS dal vivo, import da
Metashape `.psx`, WebODM/ODM e Photo3D, punto d'ingresso del motore per Photo3D (`ppg photo3d-job`),
SAM 2.1 installato a richiesta nell'app impacchettata, export IFC con avanzamento/anteprima e unità
in metri, volumi di scavo e riporto corretti, firma e notarizzazione nella build. Schede US (creazione/modifica dei
record di `us_table` con i rapporti), proxy 3D delle unità stratigrafiche (OBJ per l'Extended Matrix, media 3D in
PyArchInit), proxy IFC con quantità, materiali, gruppi di fase e rapporti.

## 0.5 - Schede di scavo, classi dei punti, motore (fatto)

Un sito per progetto; unità disegnate e quote con i campi di `pyunitastratigrafiche`
e `pyarchinit_quote`, salvate nel progetto e pubblicate una volta sola; strumento quota separato
da SAM; SAM 2.1 con un tocco, un riquadro, un'area o l'intera ortofoto a tasselli, con correzioni, su
CUDA o sulla GPU Apple; classi dei punti mostrate, nascoste e corrette con un lazo nella vista 3D;
classificatore dei punti addestrabile con modelli riutilizzabili; mesh dalle mappe di profondità (fusione TSDF);
ottimizzazione delle camere vincolata dai punti di controllo; allineamento e unione dei chunk; confidenza
della nuvola densa e del modello; pulizia della nuvola densa, del modello e dei punti di legame
(selezione graduale); ritaglio ed esportazioni ritagliate; foto ricollegate a un'altra cartella; ortofoto
grandi costruite tassello per tassello e mostrate a piena risoluzione quando si ingrandisce; patch che
assegnano le foto ad aree dell'ortofoto e della texture (sul DEM o sul suolo
intorno all'area), aggiornate sul posto con le loro overview.

## 0.6 - Barre di scala e marker come in Metashape (fatto)

Barre di scala tra marker (di controllo e di verifica, più barre mediate con la loro accuratezza), anche in
coordinate locali per oggetti e trincee, stimate insieme ai punti di controllo e alle posizioni delle
camere, dentro l'ottimizzazione delle camere, da file e stampabili; barre di scala e accuratezze dei marker
importate da Metashape (e applicata la scala dei chunk locali di Metashape); pose delle camere
e GCP importati da WebODM; marker posizionati su una sola foto (sulla superficie), sul modello,
sulla nuvola o sull'ortofoto; posizionamento guidato con bandierine blu da confermare nelle foto, filtri
delle foto per marker, punto e area, coordinate stimate nel pannello Riferimenti.

## 0.7 - Aggiornamenti, velocità e qualità dell'immagine (fatto)

Controllo automatico degli aggiornamenti (all'avvio una volta al giorno e da *Aiuto*; su macOS la nuova
applicazione viene scaricata, copiata a parte e scambiata con la vecchia alla chiusura, poi riaperta). Nucleo
nativo in Rust (`native/`, `pyarchinit-photogrammetry-core`, importato come `ppg_core`, facoltativo, numpy tenuto
come ripiego e riferimento): fusione delle stime di profondità, dalla disparità alla profondità, normali,
proiezione con i modelli di camera di COLMAP, punteggio e foto migliori di ogni cella dell'ortofoto, confidenza;
mappe di profondità su CPU di più foto alla volta (trincea sintetica 5,7 s -> 0,9 s, 40 foto del Tell
10,8 s -> 3,2 s). Ortofoto senza cuciture: ogni cella sceglie fra tutte le foto che la inquadrano (nessuna
cucitura segue i tasselli), blending a due bande lungo le cuciture, guadagno di esposizione e vignettatura
misurati sulle foto e corretti (ortofoto e texture), filtro del ghosting, buchi piccoli riempiti. Maschere
delle foto (disegnate, importate, da foto dello sfondo, da un colore, dal modello o da SAM 2.1) escluse
dall'abbinamento, dalle mappe di profondità, dall'ortofoto e dalla texture; corrispondenze ferme scartate per i
turntable. Prospetti: ortofoto e rilievo sul piano di una parete o su un cilindro (pozzi, torri). Modifica del
DEM (piano, lineare, IDW, costante, linee di rottura, il DEM com'era conservato come versione). Regione di
ricostruzione (una scatola intorno al soggetto). La texture generica degli oggetti riceve la correzione del
colore; il blending dentro il suo atlante è rimandato.

## 0.8 - Acquisizione e precisione (fatto)

Obiettivi fisheye riconosciuti dall'EXIF e allineati con il loro modello; foto a 360° divise
nelle facce di un cubo e allineate come un rig COLMAP (un solo centro, rotazioni note, calibrazione tenuta ferma).
Foto da un video (fotogrammi nitidi con la giusta sovrapposizione, GPS dai sottotitoli dei droni DJI) e
qualità delle immagini. Bandierine blu dei marker trovate per correlazione d'immagine sulla superficie intorno
al marker. Precisione di camere, marker e punti di legame dalla covarianza della compensazione (bundle
adjustment, con l'accuratezza dei punti di controllo), mappa della precisione verticale del DEM, quote
con la loro precisione in PyArchInit e nell'IFC (IfcAnnotation). Controllo sul campo subito dopo
l'allineamento: mappa della copertura (aree viste da meno di tre foto), foto non allineate o sfocate,
marker in troppo poche foto.

## 0.9 - Lavoro di ogni giorno (fatto)

* Elaborazione batch su più chunk, con salvataggio dopo ogni passo, ripresa dopo un'interruzione di corrente,
  nuovo tentativo di un passo fallito. *Fatto.*
* Annulla e ripeti per disegni, marker, patch, barre di scala e modifiche dei punti. *Fatto.*
* Diagnostica: grafo delle corrispondenze, corrispondenze tra due foto, residui nella vista foto, finestra di
  calibrazione della camera con il grafico della distorsione, unione dei marker. *Fatto.*
* Misure: area 3D sul modello, pendenza, esposizione e altezza dal suolo sotto il cursore,
  vista della nuvola per altezza dal suolo, raster dell'esposizione. *Fatto.*
* Esportazioni: Cloud Optimized GeoTIFF (JPEG, WebP, ZSTD), COPC, Cesium 3D Tiles, GLB, normal map
  per i modelli leggeri, video di sorvolo. *Fatto.*
* Scansioni laser: import E57 e PTX, registrazione sul chunk fotogrammetrico, colori dalle
  foto. *Fatto.*
* Strumenti sezione e pianta a quota tracciati direttamente nella vista 3D; marker spostati in 3D. *Fatto.*
* Pannello PyArchInit: consultazione dei media collegati alle US. *Fatto.*
* Traduzione dell'interfaccia inglese/italiano: catalogo dei testi, messaggi con valori, i
  messaggi del motore e del log riconosciuti quando vengono mostrati, *Vista > Lingua / Language*. *Fatto.*

## 1.0 - Prima l'archeologia (fatto, pubblicato come 1.0.0)

* Visualizzazione del rilievo dei DEM: sky-view factor, openness positiva e negativa, rilievo
  locale, ombreggiatura multidirezionale, rilievo multiscala (muri e fossati appena visibili su un
  tell). *Fatto* (in NumPy a tasselli e thread: 2000 x 2400 celle in meno di un secondo per metodo, quindi
  non è servito un kernel nativo).
* Scavo nel tempo: fasi (chunk per data) allineate insieme, volume asportato per ogni US,
  cronologia nelle viste, rapporti stratigrafici suggeriti dall'ordine di asportazione. *Fatto*
  (*Strumenti > Fasi dello scavo*, `ppg timeline`; i disegni ora restano con il chunk su cui sono stati
  disegnati).
* Disegno pietra per pietra con SAM su ortofoto e prospetti. *Fatto* (strumento SAM *Pietre*,
  `ppg stones`).
* Colore Munsell delle US da ortofoto calibrate con una tabella colori. *Fatto* (*Misura area*,
  *Calibra il colore con la tabella colori*, `ppg munsell`).
* Pacchetto QField: ortofoto e layer sul tablet in trincea, modifiche riportate in PyArchInit. *Fatto*
  (*File > Esporta > Pacchetto QField*, *File > Importa > Modifiche da QField*, `ppg qfield`).
* Paradati leggibili dalla macchina (provenienza di ogni prodotto) collegati ai media di PyArchInit. *Fatto*
  (W3C PROV in JSON-LD accanto alle esportazioni e ai media pubblicati, `ppg paradata`).
* Ceramica: diametro dell'orlo da un frammento, ricerca tipologica per profilo in `pottery_table`. *Fatto*
  (*Strumenti > Profilo ceramico da mesh*: diametro dell'orlo e arco conservato dal cerchio del labbro,
  *Cerca forme simili*, *Salva in PyArchInit*; `ppg pyarchinit pottery-search`, `pottery-save`).
* Proposta automatica di US dalle maschere SAM + rapporti stratigrafici per l'Extended Matrix. *Fatto*
  (*Strumenti > Proponi US e rapporti*: numeri, tipi e misure, rapporti dai contatti
  e dal DEM, tagli dei riempimenti; scritti nelle schede US da cui PyArchInit ed EMStudio costruiscono
  la matrice; `ppg us-proposal`).
* IFC con rapporti stratigrafici e identificatori s3dgraphy/StratiGraph (`node_uuid`). *Fatto*
  (il `node_uuid` della scheda US è il `GlobalId` del suo elemento; un `IfcRelConnectsElements` per
  rapporto con il suo arco dell'Extended Matrix; vedi [bim.md](bim.md)).

## Ricerca

* Stereo multi-vista PatchMatch su CPU nel nucleo nativo (quello di COLMAP richiede CUDA, che il Mac non
  ha; il percorso CPU era lo SGBM a due viste). Un prototipo plane-sweep sulla GPU Apple trovava le
  stesse profondità del percorso CPU ma era 30 volte più lento dello SGBM di OpenCV. *Fatto* (backend
  `patchmatch`, nucleo 0.2.0: sulla trincea sintetica DEM mediana 0,27 mm, NMAD 0,70 mm, 90°
  percentile 1,5 mm contro 0,76 / 1,69 / 3,7 mm con SGBM, 0,5 s per foto di 600 x 450 pixel;
  le mappe a due viste mettevano punti anche oltre i bordi della superficie).
* Compensazione propria (bundle adjustment) nel nucleo nativo: veri vincoli di distanza per le barre di scala,
  correzione del rolling shutter per i droni, bias GNSS (pycolmap non accetta costi personalizzati).
  *Fatto* in Python (`engine.bundle`: minimi quadrati sparsi di scipy con lo jacobiano raggruppato secondo la
  struttura del problema; `optimize_cameras(engine="ppg")`, `ppg optimize --engine ppg`): su
  blocchi sintetici recupera pose e calibrazione, la scala da due barre, un bias GNSS entro
  2 cm, e porta un blocco con rolling shutter da 1,75 px a 0,002 px; la trincea resta entro le sue
  soglie. Spostarla nel nucleo nativo è rimandato a quando i blocchi diventeranno troppo grandi per lei.
* Atlanti di texture a più pagine, I3S. Pagine *fatte*: una texture più grande di 8192 px viene divisa in pagine
  (gruppi di facce vicine, o tasselli della pianta per la mappatura ortofoto), portate dalle esportazioni,
  dalla vista 3D, dalle versioni, da annulla, dalle patch e dall'import da Metashape. I3S *fatto*: il modello con
  texture come Integrated Mesh scene layer package 1.7 (`chunk.export_i3s`, `ppg export --i3s`, *File >
  Esporta*), globale (WGS84) o locale (CRS proiettato), riletto nodo per nodo da loaders.gl.
* Build per ARM. Linux aarch64 *fatto*: wheel di pycolmap 4.2.1 e Open3D 0.19.0, che i loro
  progetti non pubblicano, costruite in Docker con le loro stesse ricette (`packaging/arm`, workflow
  `arm-wheels`) e provate con questo progetto. Windows su ARM: l'applicazione x64 gira
  nell'emulazione di Windows 11 (verificato); le build ARM64 native (pycolmap, Open3D, OpenCV, lazrs) vengono dopo.

## Dopo la 1.0

* Sito ufficiale della documentazione in italiano e in inglese, con tutorial svolti su un tell vero.
* Plugin: un'interfaccia per i plugin, una guida per scriverli e un repository che li raccoglie.
* Benchmark contro Metashape e WebODM su Tell Tuqan (con punti di controllo): allineamento, mappe di
  profondità, nuvola densa, mesh, DEM e ortofoto - tempi, risultati ed errori.
* Build native per Windows ARM64.

## Dopo la 1.1 (fatto, pubblicato nella 1.2.0)

Fatto nella 1.2.0: la GPU tramite wgpu per l'abbinamento dei descrittori e le mappe di profondità
PatchMatch, più GPU e la CPU insieme (dopo la 1.2.2 anche i punti SIFT e la fusione delle mappe di
profondità; la compensazione resta sulla CPU: Ceres, nessun solutore GPU portabile); i nodi di rete
e `ppg remote`; le riprese multispettrali con il
pannello, il sensore di luce, le ortofoto di riflettanza, gli indici e la zonizzazione; le scene
satellitari ortorettificate sul DEM e il modello di superficie da una coppia stereo. Ancora aperti
di questo elenco: i tasselli di texture e ortofoto sui nodi, le triplette stereo e la verifica sui
dataset pubblici WorldView-3.

* GPU per i passi più lenti, portabile: kernel scritti in WGSL ed eseguiti con wgpu (Metal sul
  Mac, Vulkan su Linux e Windows, niente CUDA), prima per l'abbinamento dei descrittori e la NCC
  del PatchMatch; più GPU e la CPU si dividono il lavoro in proporzione alla loro velocità.
* Elaborazione in rete e remota: il programma avviato come nodo su altri computer della rete
  prende dalla cartella condivisa del progetto il lavoro che si divide per foto (feature, mappe
  di profondità, tasselli di texture e ortofoto); lo stesso via SSH su una macchina a noleggio.
  L'allineamento resta su una macchina sola.
* Camere multispettrali e indici di vegetazione: rig a quattro o cinque bande come i rig 360,
  calibrazione radiometrica con il pannello e il sensore di luce, ortofoto di riflettanza a 16 bit
  per banda, NDVI, NDRE e termico, zonizzazione esportata come poligoni con un attributo (la mappa
  di prescrizione dell'agricoltura; in archeologia i crop mark sopra muri e fossati sepolti).
* Immagini satellitari, in due tappe: l'ortorettifica di una scena col suo modello RPC sul DEM del
  rilievo, tenuta come versione dell'ortofoto; poi modelli di superficie da coppie e triplette
  stereo (approssimazione affine locale dell'RPC per tassello, rettifica, matching denso,
  triangolazione), verificati sui dataset pubblici WorldView-3 con lidar di riferimento.

## Fatto nelle 1.3.5 - 1.5

* **Windows come gli altri sistemi** (1.3.5): i test girano anche su Windows x64 nella CI, e gli errori che
  hanno trovato sono corretti (file del modello lasciato mappato, decodifica del termico DJI, prodotti tenuti
  aperti, SpatiaLite di QGIS non trovato, avanzamento dei processi figli); l'applicazione Windows ARM64 ha la
  vista 3D VTK e l'E57 (VTK 9.7.1 e pye57 compilati per ARM64, vista provata su una macchina Windows 11 ARM64).
* **Satellite** (1.3.5): modelli di superficie da tre scene o più (coppie scelte per angolo di intersezione e
  data, correzione relativa degli RPC dai punti di legame, coppie fuse cella per cella), verificati su due
  siti del benchmark pubblico WorldView-3 di JHU/APL contro il lidar: errore mediano 0,59 m e 1,03 m, 61 % e
  38 % delle celle entro 1 m (vedi la pagina del benchmark).
* **MicaSense** (1.3.5): verificato sulle riprese di esempio di cinque camere contro i numeri di MicaSense -
  radianza, banda termica, pannello trovato dai tag, sensore di luce, registrazione delle bande attraverso le
  lenti; banda termica entro 0,3 pixel termici; pannello + sensore di luce di predefinito.
* **Nodi di rete** (1.4.0): anche i tasselli dell'ortofoto e dell'ortofoto della texture di un terreno, e le
  righe di celle della texture; i pixel sono gli stessi di quando il programma lavora da solo.
* **Calcolatore raster** (1.4.0): *Strumenti > Calcolatore raster*, `ppg calc`: formule come in QGIS su tutti i
  raster di un chunk, anche quelli dei plugin.
* **QGIS nei due sensi** (1.4.0): quello che si salva in QGIS nei layer delle US, delle quote e delle sezioni
  torna nella finestra entro un secondo (modificato, disegnato lì, cancellato), un *Annulla* per salvataggio.
* **Modifica delle US disegnate** (1.4.0): *Vertici* (spostare, aggiungere, togliere vertici, con lo snap) e
  *Rimodella* (come Reshape Features di QGIS); il proxy 3D dell'unità si ricostruisce da solo con la sua base.
* **US nuove nell'Extended Matrix** (1.4.0): un'unità disegnata con il suo numero e senza scheda in PyArchInit
  entra in *Proponi US e rapporti*, e arriva in EMtools ed EMStudio con il prossimo em.json del sito
  esportato da PyArchInit.
* **US create dai plugin** (1.5.0): `context.add_units`; il plugin **samgeo** trova nell'ortofoto quello che
  si scrive («pietre», «muri») con Grounding DINO e SAM 2.1 (SAM 3 facoltativo da samgeo 0.2.0) e il plugin
  **geoai** si addestra sui disegni controllati a mano (anche con DEM, multispettrale e termico).
* **US in gruppi** (1.5.1): identificativi in blocco, gruppi UT con nome e selezione a mano libera.
* **Etichette dei layer** (1.5.2 - 1.5.3): etichette mostrate o nascoste per layer e modificate senza cambiare
  il numero US/UT, salvate per chunk, conservate da *Annulla* e *Ripeti*.
* **Prospetti e sezioni come le ortofoto planari di Metashape** (1.5.5): sul piano della vista 3D com'è ora,
  dei marker con gli assi scelti (u da un marker verso un altro, v perpendicolare o fra altri due), di tre punti
  cliccati nella vista 3D; una sezione toglie quello che sta davanti al piano e salva il profilo del taglio
  (disegnato sull'ortofoto, esportato in DXF); v è la quota sui piani verticali, i marker sono disegnati sul
  prospetto; `ppg elevation`.
* **Posizioni GNSS delle foto, primo passo**: lo stato RTK e le deviazioni standard delle foto DJI (e delle
  bande delle riprese multispettrali), gli scatti di un `Timestamp.MRK` con il braccio di leva dell'antenna,
  file di posizioni di qualsiasi drone o programma, l'accuratezza di ogni foto (il doppio delle deviazioni
  dichiarate, con i minimi del suo stato) e il pannello *GNSS* con una tabella per volo, gli scarti delle camere
  allineate e *Solo le posizioni fix*; `ppg gnss`.

## Prossimi passi

Punti aperti, senza un ordine fisso:

* Parti mai provate su dati veri: import da WebODM, una scena satellitare propria, `ppg remote` su una
  macchina remota, un volo MicaSense intero fino all'ortofoto di riflettanza.
* SpatiaLite su Windows ARM64: aspetta un QGIS per ARM64 (la sua libreria è x64).
* geoai: confronto fra due date coregistrate e differenze di quota dei loro DEM, dopo la verifica su un
  rilievo annotato reale.
* **Disegni sui prospetti e sulle sezioni**: le US, le linee, le quote e i punti disegnati su un prospetto o su una
  sezione tenuti con lui, nelle sue coordinate (u, v; v la quota sui piani verticali), non fra i disegni della
  pianta; mostrati di nuovo quando il prospetto si riapre, esportati con lui (DXF, GeoPackage) e, per una
  sezione, mandati a PyArchInit come disegno di sezione. Anche il disegno pietra per pietra dei prospetti tenuto
  per prospetto (oggi va per nome del file, lo stesso per tutti i prospetti).
* Idee non ancora decise: un plugin **topology** su topologicpy - i rapporti stratigrafici letti dalle facce
  che i proxy 3D delle US condividono (*copre* dai contatti orizzontali, *si appoggia a* da quelli
  verticali) verso l'Extended Matrix, i proxy che si sovrappongono divisi in celle pulite per i volumi, gli
  ambienti degli edifici come celle. Solo come plugin: il suo nucleo geometrico è AGPL-3.0 e non ha una
  versione per Windows ARM64.

## Non previsto

Sistemi di coordinate planetari; la calibrazione del boresight del lidar aereo (nessun dato di
traiettoria con cui validarla; le nuvole lidar si importano e si co-registrano col plugin terrio e
con l'allineamento dei chunk). Il rilevamento delle linee elettriche non fa parte del programma ma
è il plugin `powerlines` del [repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins)
(conduttori come catenarie, pali, franco dal suolo, distanza dagli ostacoli).
