# Foto e marker

Tutto ciò che entra nell'allineamento: le foto e le loro maschere, i marker misurati su di esse, le barre di
scala tra i marker.

## Aggiungere le foto

| Comando | Cosa aggiunge |
| --- | --- |
| *Flusso di lavoro > Aggiungi foto...* / *Aggiungi cartella...* | le foto nel chunk attivo; un volo diviso in più cartelle (i rullini di una camera, `100MEDIA` e `101MEDIA`, la cui numerazione ricomincia) si aggiunge cartella dopo cartella o come cartella madre in una volta: i nomi che si ripetono prendono la loro cartella come prefisso, i file già nel chunk restano fuori |
| *Flusso di lavoro > Aggiungi video...* | i fotogrammi scelti quando la vista si è spostata di una parte della sua larghezza (il più nitido attorno a quel momento) o uno ogni pochi secondi, scritti in una cartella accanto al video; con il `.SRT` di un drone DJI accanto al video ogni fotogramma ha la sua posizione GPS |
| *Flusso di lavoro > Aggiungi foto 360°...* | ogni foto equirettangolare diventa le sei facce di un cubo (le facce da tenere - *Sotto* spesso mostra solo il treppiede - la loro ampiezza in gradi e la dimensione), scritte in `facce_360` accanto alle foto; nell'allineamento le facce di una foto restano insieme come un rig |
| *Flusso di lavoro > Ricollega foto (altra cartella)...* | le foto con lo stesso nome in un'altra cartella e nelle sue sottocartelle: una copia su disco esterno di una cartella Dropbox tenuta solo online, una cartella spostata |

I file di una **camera multispettrale** vengono riconosciuti quando si aggiungono le foto: la finestra propone di
aggiungerli come riprese e apre il pannello *Multispettrale*, così le bande non vengono allineate come se fossero
viste diverse (vedi [Multispettrale e termico](multispectral.md)).

Un **obiettivo fisheye** viene riconosciuto dall'EXIF; altrimenti *Modello dell'obiettivo...* (clic destro nel
pannello *Foto*) lo imposta per i sensori delle foto selezionate: normale, fisheye, distorsione forte, nessuna.
Il suggerimento di una faccia di una foto 360° dice da quale foto viene.

!!! tip "Qualità delle immagini"
    Clic destro nel pannello *Foto*: *Qualità delle immagini* dà la nitidezza di ogni foto confrontata con le più
    nitide (nel suggerimento; *sfocata* sotto 0,5), e *Disattiva le foto sfocate* le lascia fuori dal prossimo
    allineamento.

## Posizioni GNSS delle foto

Ogni foto ha una posizione, sa da dove viene e quanto è buona. Il pannello *GNSS* (*Strumenti > GNSS > Pannello
GNSS*, a destra con *Multispettrale* e *Termico*) le mostra e ha i comandi che danno alle foto una posizione
migliore del GPS dell'EXIF.

| Fonte | Cosa dà |
| --- | --- |
| Le foto stesse | Il GPS dell'EXIF, con un'accuratezza di 5, 5, 10 m (nessun dato RTK). Le foto di un drone DJI con RTK portano anche lo stato nell'XMP (`RtkFlag`: 50 fix, 34 float, 16 singola, 0 nessuno) e le deviazioni standard, lette quando le foto vengono aggiunte; quelle di una camera multispettrale le prendono dalle loro bande |
| *Strumenti > GNSS > File MRK di DJI...* | Il `Timestamp.MRK` che il drone scrive sulla scheda: la posizione di ogni scatto, della camera e non dell'antenna (si applica il braccio di leva dello scatto), con la quota ellissoidica, lo stato RTK e le deviazioni standard |
| *Strumenti > GNSS > Correzioni PPK...* (*Calcola PPK...* nel pannello) | La posizione di ogni scatto del `Timestamp.MRK` calcolata di nuovo dal RINEX del drone e da quello di una base: vedi [PPK](#ppk-correzioni-da-una-base) più sotto |
| *Strumenti > GNSS > File di posizioni...* | Un file di testo con una riga per foto, per un drone costruito in casa o posizioni calcolate da un altro programma (una soluzione PPK, una stazione totale): nome, x, y, quota e, se ci sono, `sx`, `sy`, `sz` (deviazioni standard) o `accuracy`, `status`, `yaw`, `pitch`, `roll`; un'intestazione dà i nomi delle colonne in qualsiasi ordine (`name`, `lon`/`lat`/`ellh`, `easting`/`northing`, `std`, `q`...); virgola, punto e virgola, tabulazione o spazi |

**L'MRK si abbina alle foto con il numero dello scatto nel loro nome** (`DJI_0001.JPG` è la riga 1 del file),
così una foto cancellata dalla scheda non sposta mai le posizioni delle altre; per ordine solo quando l'MRK ha
tante righe quante sono le foto della cartella, altrimenti non si fa niente e la finestra dice perché. Ogni coppia
si controlla poi con l'ora che la camera ha scritto nella foto (la sua ora locale, a un fuso orario dall'UTC
dell'MRK): una foto scattata a più di 3 secondi dal suo scatto tiene la posizione che aveva, così le foto di un
altro volo copiate nella stessa cartella, con gli stessi numeri di scatto, restano come sono; quando le ore non
tornano con nessun fuso orario, o la maggior parte delle foto non è all'ora del suo scatto, non si fa niente. Quando anche le foto di un altro volo tornano con un fuso orario e
sono almeno la metà delle foto dell'MRK, le ore non dicono di quale volo sia l'MRK (il suo volo può aver perso
delle foto): non si fa niente e la finestra chiede di tenere ogni volo in una cartella propria. Gli
scatti che non hanno trovato una foto sono elencati nel registro. Le foto sono quelle della cartella dell'MRK; se l'MRK è stato copiato altrove la finestra chiede la cartella (`--photos`
da riga di comando). **Il file di posizioni** chiede il suo sistema di coordinate (`EPSG:4979` è longitudine,
latitudine e quota ellissoidica, come nell'MRK; un sistema proiettato come `EPSG:32633`; `LOCAL`) e il tipo di
quota (*ellissoidica*, come la dà il GNSS, o *dell'EXIF*, sul livello del mare); una riga si abbina alla sua foto
con il nome del file, con o senza estensione (`100MEDIA/DJI_0001` quando lo stesso nome sta in due cartelle).
Applicare le posizioni sostituisce quelle che le foto avevano, e farlo due volte non cambia nulla.

**L'accuratezza** di una posizione è il doppio delle sue deviazioni standard dichiarate (quelle di una soluzione
RTK sono ottimistiche), mai sotto il minimo del suo stato: 1 cm per un fix, 20 cm per un float, 1 m per una
posizione singola. Senza dati RTK resta 5, 5, 10 m. In un file di posizioni `accuracy` vale così com'è; una riga
senza accuratezza né deviazioni prende 5, 5, 10 cm, portati al minimo del suo `status` se ce l'ha. Due passi
pesano ogni foto per la sua accuratezza, così un blocco di fix e qualche posizione singola non è trattato come
fatto di pari: *Allinea foto...* tiene il blocco alle posizioni durante l'allineamento incrementale (l'opzione
*Posizioni GPS delle foto nell'allineamento*, attiva quando le foto hanno posizioni; il metodo globale non le usa,
e la scelta automatica torna all'incrementale quando il blocco globale le manca), e *Ottimizza camere*: con il
motore predefinito, COLMAP, le posizioni accurate (fix, float, PPK: 0,5 m o meglio) tengono il blocco quando non
lo tengono 3 punti di controllo o 2 barre di scala (*Posizioni GNSS delle foto*: *automatico*, vedi
[Ottimizzare le camere](processing.md#ottimizzare-le-camere)), e così anche con il motore *Proprio* su
*automatico*; con il motore *Proprio* ogni posizione entra secondo la sua accuratezza quando quell'opzione è su
*usate con la loro accuratezza (motore proprio)* o *usate, con un bias per volo (motore proprio)*.

**La tabella** ha una riga per volo (una pausa di oltre 20 minuti ne comincia un altro; le foto senza data sono
la riga *senza data*): le foto con una posizione fix, float, singola, con una posizione e nessun dato RTK, senza
alcuna posizione, e quelle il cui riferimento è disattivato; l'accuratezza mediana per asse; il tipo di quota
delle posizioni (*ellissoidica* dal GNSS e dall'MRK, *EXIF*, *sconosciuta* per posizioni lette da un altro
programma); e, quando il chunk è allineato e georeferenziato, quanto le camere sono finite lontano dalle loro
posizioni (quadratica media per asse e distanza maggiore). Un volo i cui scarti sono molto più grandi della sua
accuratezza ha un bias (una base su un punto sbagliato) o posizioni che non gli appartengono. Quando le posizioni
in uso, accurate in quota (sotto il metro), hanno quote di tipo diverso - quelle ellissoidiche di un MRK e quelle
dell'EXIF di altre foto, che possono distare decine di metri - il pannello lo dice sotto le posizioni (e `ppg gnss
status` stampa un avviso): controllale prima di usarle insieme.

**Solo le posizioni fix** disattiva i riferimenti delle foto la cui posizione non è un RTK fissato: restano nel
chunk e si allineano, ma non tirano più le camere. Una posizione senza stato RTK (il GPS dell'EXIF, un file di
posizioni senza la colonna `status`) conta come non fix. Le foto disattivate si ricordano: togliendo la spunta si
riattivano proprio quelle, un riferimento disattivato a mano resta disattivato, e riapplicando le posizioni la
scelta si mantiene.

Da riga di comando:

```bash
ppg gnss status rilievo.ppg                                   # la tabella, per volo
ppg gnss mrk rilievo.ppg /scheda/DCIM/100MEDIA/Timestamp.MRK  # --photos CARTELLA se le foto stanno altrove
ppg gnss positions rilievo.ppg camere.csv --crs EPSG:4979     # --height ellipsoidal|exif
```

Tutti e tre accettano `--chunk` e dicono cosa hanno fatto; `mrk` e `positions` salvano il progetto, e si fermano
con un errore, senza cambiare nulla, quando il file non si può usare.

### PPK: correzioni da una base

**Cosa dà.** Un drone senza collegamento RTK (o che l'ha perso) colloca le sue foto a qualche metro. Con il PPK
(cinematico post-elaborato) RTKLIB ricalcola dove era l'antenna del drone, epoca per epoca, dalle osservazioni dei
satelliti del drone e da quelle di una base che registrava nello stesso momento; ogni foto prende poi la posizione
della camera al suo scatto, come con un MRK, con lo stato (fix, float, singola) e l'accuratezza della soluzione. Con
i fix le foto sono collocate a pochi centimetri rispetto alla base, e un rilievo ha bisogno di pochi punti a terra o
di nessuno. La base decide dove finisce tutto il rilievo: il tuo ricevitore, una stazione permanente vicina al
rilievo, o il tuo ricevitore posto con una linea di base statica da una stazione o con un PPP statico
([La posizione della base](#la-posizione-della-base)); la sua antenna è calibrata con il file dell'IGS, igs20.atx
([Antenna della base](#antenna-della-base)); e ogni posizione tiene il sistema di riferimento e l'epoca delle sue
coordinate ([Sistemi di riferimento ed epoche](#sistemi-di-riferimento-ed-epoche)).

**I file.**

| File | Da dove viene |
| --- | --- |
| Il RINEX del drone e il suo `Timestamp.MRK` | La cartella del volo sulla scheda del drone: un drone DJI scrive `<volo>_Rinex.obs` accanto a `<volo>_Timestamp.MRK` (spesso con `<volo>_PPKNAV.nav`); un altro ricevitore il suo file di osservazioni (`.obs`, `.##o`) |
| Il RINEX della base | La tua base (qualsiasi ricevitore che registra in RINEX) su un punto, oppure una stazione permanente vicina al rilievo: il programma scarica le osservazioni di una stazione dell'EPN, dell'IGS o di RING per le ore del volo (*Una stazione permanente vicina come base*, più sotto), o gli dai il file di una stazione che hai scaricato tu. La base deve registrare per tutto il volo, in un file o in **più file**: i file orari di una stazione, i due file giornalieri di un volo a cavallo della mezzanotte (UTC), i pezzi che scrive un ricevitore - *Aggiungi...* li prende tutti, e il programma li unisce in uno (devono essere di un solo ricevitore: lo stesso punto, la stessa antenna, gli stessi tipi di osservazione) e dice dove cadono i buchi della base durante il volo. Più vicina è meglio è: oltre 20 km il fix diventa difficile (il programma lo avvisa) |
| File di navigazione | Le effemeridi trasmesse: `.nav`, `.##n`, `.##g`, `.##p` o i RINEX 3 `*_MN.rnx` - quelli del drone, della base o il file giornaliero di una stazione; anche il file giornaliero delle effemeridi dell'IGS in RINEX 2 (`brdcDDD0.YYn`, `.YYg`) si trova accanto alle osservazioni, per il loro giorno e i giorni vicini. Per una stazione, una linea di base o un PPP, quando accanto al file del drone non ce n'è nessuno, il programma scarica i messaggi trasmessi delle ore del volo (`BRDC00WRD`, dal BKG) |

**I file compressi** si leggono così come sono, come li danno gli archivi delle stazioni: gzip (`.gz`), il compress di
Unix (`.Z`), uno zip con un solo file dentro (`.zip`) e Hatanaka (`.crx`, `.##d`: Compact RINEX 1.0, 3.0 e 3.1),
anche l'uno dentro l'altro (`.crx.gz`, `.##d.Z`). I file Hatanaka li decodifica il nucleo nativo 0.6 o più recente,
che le applicazioni contengono già (il loro controllo, `--check`, fallisce se manca); senza, il programma `CRX2RNX` di
RNXCMP trovato nel `PATH` (o indicato da `PPG_CRX2RNX`), altrimenti il programma dice che cosa installare. `ppg doctor`
(*Aiuto > Diagnostica ambiente*) dice dove si decodificano. La copia decompressa resta nella cartella GNSS del
programma e si usa di nuovo finché il file non cambia (o non cambia il decodificatore); prima di scaricare i file di una
stazione se ne vanno le copie non usate da due settimane, e le meno usate oltre 1 GB. A RTKLIB non si dà mai un file
compresso.

**I passi.** *Calcola PPK...* nel pannello (*Strumenti > GNSS > Correzioni PPK...*) chiede il `Timestamp.MRK`, poi
apre una finestra:

- *Timestamp.MRK*: i suoi scatti e le ore in cui sono stati fatti, e quanti cadono fuori dalle osservazioni del drone
  (le loro foto terranno la posizione che hanno);
- *Osservazioni del drone*: trovate da sole accanto all'MRK (il `*_Rinex.obs` di un drone DJI, del volo dell'MRK
  quando la cartella ne ha più di uno; altrimenti l'unico file `*.obs` o `*.##o` che c'è), con le ore che coprono;
- *Posizione della base*: uno dei cinque modi qui sotto - con le coordinate, il loro sistema e la loro epoca quando
  sono scritte, con *Scegli la stazione...* per una stazione permanente;
- *Osservazioni della base*: un file o più (*Aggiungi...*; il campo dice allora quanti sono), con quello che dice
  l'intestazione - il punto, il ricevitore, l'antenna, le ore delle osservazioni di tutti i file, la posizione
  nell'intestazione e l'altezza dell'antenna - e, in vista, se lo scostamento del centro di fase di quell'antenna
  verrà applicato; una stazione permanente come base non ne vuole;
- *File di navigazione*: quelli accanto alle osservazioni del drone, poi quelli accanto a quelle della base;
  *Togli* ne toglie uno, *Aggiungi...* ne aggiunge un altro;
- *Antenna della base*: *Calibrazione dell'antenna della base: scarica igs20.atx (62 MB), una volta*
  ([più sotto](#antenna-della-base));
- *Download*: che cosa verrà scaricato per il calcolo - le coordinate e le osservazioni di una stazione, i prodotti
  precisi di un PPP, igs20.atx, i messaggi di navigazione del volo - e il sistema e l'epoca che avranno le posizioni
  delle foto;
- *Fix RTK del drone*: se le foto con un fix RTK del drone stesso prendono anche loro la posizione PPK (sotto).

La finestra riparte dall'ultimo calcolo del chunk: i file della base, come è stata posta la base (la sua posizione
scritta con il suo sistema e la sua epoca, o la sua stazione), i file di navigazione che ci sono ancora (non quelli
accanto alle osservazioni del drone di un altro volo), la scelta sui fix di bordo; per lo stesso MRK anche le
osservazioni del drone. Con più voli sulla stessa base, la base e la sua posizione si danno una volta sola.

*OK* controlla, prima che parta qualsiasi cosa, quello che si può controllare e dice nella finestra che cosa manca o
non va: i file (osservazioni che siano osservazioni, file di navigazione ancora al loro posto), le loro ore (il drone
e la base con ore in comune, gli scatti dell'MRK dentro le osservazioni del drone), nessun file nella cartella di
lavoro del PPK, la posizione della base, la stazione scelta. I file si leggono una volta, dopo una pausa nella
scrittura dei loro nomi; la finestra è alta quanto quello che dice, e su uno schermo basso il suo modulo scorre. Se
l'MRK è stato copiato lontano dalle foto la finestra chiede la loro cartella, come per un MRK. Il lavoro gira in
background: prima la stazione, la linea di base o il PPP, con quello che scaricano (dato a RTKLIB da `gnss/base`,
`gnss/baseline` e `gnss/ppp` della cartella del chunk; i file delle stazioni e i prodotti precisi restano nella
cartella GNSS del programma, dove un secondo calcolo li ritrova), poi RTKLIB in `gnss/ppk`, dove ogni calcolo scrive
le sue opzioni e la sua soluzione con un nome suo, `solution-<data>-<ora>.conf` e `.pos`: le soluzioni dei calcoli
precedenti (gli altri voli) restano, e ogni foto registra la soluzione del suo calcolo. Le foto si abbinano agli
scatti come per un MRK.

#### La posizione della base

Ogni posizione PPK è relativa alla base: dove sta la base, va tutto il rilievo. Cinque modi per porla, nella finestra
e in `ppg gnss ppk`:

| Modo | Che cos'è | Che cosa aspettarsi |
| --- | --- | --- |
| *Dal file della base (intestazione RINEX)* | la posizione scritta nell'intestazione del suo file RINEX | per una stazione permanente di solito la sua posizione pubblicata; per la tua base spesso la posizione che il ricevitore ha trovato da solo, che **può essere sbagliata di metri, e tutto il rilievo si sposta con lei** |
| *Scritta* | la posizione misurata del punto su cui sta la base, nel sistema delle sue coordinate | buona quanto il rilievo del punto |
| *Una stazione permanente vicina come base* | una stazione dell'EPN, dell'IGS o di RING: si scaricano le sue osservazioni per le ore del volo e le sue coordinate in ITRF2020 a metà del volo | coordinate buone al centimetro o meglio; le foto buone quanto un PPK a quella distanza - entro 20 km, più vicina è meglio è |
| *Calcolata con una linea di base da una stazione vicina* | la tua base su un punto che nessuno ha misurato, posta con una linea di base statica da una stazione | fissata e buona al centimetro con un'ora o più di osservazioni comuni e una stazione entro 20 km; una stazione più lontana vuole una sessione più lunga |
| *Calcolata con il PPP statico* | la tua base posta da sola, sulle orbite e sugli orologi precisi dell'IGS e dei suoi centri di analisi | 10-20 cm con meno di 2 ore di osservazioni, qualche centimetro con 4 ore o più, 1-2 cm in pianta e 2-4 cm in quota dopo un giorno: sotto le 4 ore è meglio una linea di base da una stazione vicina |

- **Dal file della base**: il programma dice che la posizione può essere sbagliata di metri nella finestra, in un
  messaggio e nel registro dopo il calcolo, nel pannello e in `ppg gnss ppk`: scrivi la posizione misurata, o poni la
  base con una stazione o con un PPP, e calcola di nuovo.
- **Scritta**: latitudine e longitudine in gradi, quota sull'ellissoide in metri (non sul livello del mare), nel
  sistema delle coordinate (*Sistema delle coordinate scritte*: *Sconosciuto (WGS 84)*, ITRF2020, ETRF2000 (RDN2008),
  ETRF2014, ETRF2020) e, in ITRF2020, alla loro epoca (*Epoca delle coordinate*: un anno decimale come 2026,82, o una
  data; vuota, la metà delle osservazioni del drone). È la strada per una posizione assoluta accurata con un punto che
  hai misurato. Scritta a più di 1 km dalla posizione dell'intestazione della base, o più alta o più bassa di oltre
  30 m, dà un avviso (nella finestra, nel registro e in `ppg gnss ppk`): latitudine e longitudine scambiate o senza il
  segno, una quota sul livello del mare. La stessa base (per il nome del suo punto, altrimenti del suo file) scritta in
  una posizione a più di 5 cm da quella usata per altre foto del chunk dà anche lei un avviso - un errore di battitura
  in un volo sposta quel volo - e il pannello dice quando una base ha posizioni più lontane fra loro.
- **Una stazione permanente come base**: *Scegli la stazione...* apre *Stazioni permanenti vicine*: le stazioni
  dell'EPN, dell'IGS e di RING entro 200 km dal rilievo, la più vicina per prima, con le loro reti e la loro distanza
  (l'elenco si scarica di nuovo una volta alla settimana, e la copia serve quando non c'è rete; *Riprova* quando non
  si scarica). Se gli archivi hanno i file di una stazione per le ore del volo si scopre quando la si usa: le sue
  osservazioni a 1 s quando l'archivio le ha per tutto il volo, altrimenti a 30 s, con dieci minuti prima e dopo (il
  centro dati del BKG per le stazioni dell'EPN e dell'IGS, l'archivio di RING, sulla porta 44324 che alcuni firewall
  chiudono, per quelle di RING); il programma dice quando non sono ancora in rete. Le sue coordinate vengono dalla
  soluzione pluriennale dell'EPN, da IGc20 dell'IGS o da una retta per due anni di posizioni giornaliere di RING,
  portate all'epoca del volo: le posizioni delle foto sono in ITRF2020 a quell'epoca. Una stazione di cui si conosce
  solo la posizione approssimata (buona ai metri) si rifiuta. Le coordinate sono quelle del punto della stazione: il
  programma aggiunge l'altezza dell'antenna dell'intestazione del suo file RINEX, come per ogni base.
- **Una linea di base da una stazione**: la tua base registra per una sessione (un'ora o più) su un punto che nessuno
  ha misurato; *Calcolata con una linea di base da una stazione vicina* scarica le osservazioni della stazione per
  quella sessione e le sue coordinate a metà della sessione, e RTKLIB risolve la tua base come un ricevitore fermo per
  tutta la sessione rispetto alla stazione, in una posizione sola. Toglie l'altezza dell'antenna dell'intestazione
  della tua base: la posizione è quella del punto a terra, in ITRF2020 all'epoca delle coordinate della stazione. Una
  soluzione float, una stazione oltre 20 km, una sessione più corta di un'ora, buchi di oltre un minuto nelle
  osservazioni della base danno avvisi; una soluzione del solo codice (né fix né float), sbagliata di metri, si
  rifiuta. `ppg gnss baseline` trova il punto da solo, per scriverlo per più voli.
- **Un PPP statico**: la tua base da sola, sui prodotti precisi di un solo centro e di un solo tipo: le orbite e gli
  orologi finali di ESA (in rete circa 4 giorni dopo il giorno), di CODE (circa 9 giorni) o dell'IGS (due settimane o
  più), altrimenti quelli rapidi dell'IGS (circa 17 ore); mai gli ultrarapidi, che sono per metà previsti. igs20.atx si
  usa sempre, per le antenne dei satelliti e della tua base. RTKLIB-EX risolve un PPP solo float (non fissa le
  ambiguità): le durate della tabella sono quello che ci si può aspettare. Il punto è in ITRF2020 all'epoca della metà
  della sessione. Una sessione sotto le 2 ore, sotto le 4 ore (è meglio una stazione vicina), prodotti rapidi (calcola
  di nuovo quando escono i finali) e una sessione prima del 27 novembre 2022 (settimana GPS 2238: i prodotti erano
  fatti per igs14.atx) danno avvisi. `ppg gnss ppp` trova il punto da solo.

#### Antenna della base

**L'altezza dell'antenna.** La posizione della base, scritta o dall'intestazione, è quella del punto a terra: il
programma aggiunge l'altezza dell'antenna scritta nell'intestazione RINEX della base (`ANTENNA: DELTA H/E/N`), come
la intende il RINEX, e la finestra dice quanto ("il programma aggiunge l'altezza dell'antenna del file della base,
1,800 m"). Quando l'intestazione non dà l'altezza dell'antenna, o la dà nulla, la posizione dev'essere quella
dell'antenna stessa.

**Il centro di fase.** L'altezza dell'antenna porta la base al punto di riferimento della sua antenna (di solito la
base del suo attacco); i satelliti però si misurano nel suo centro di fase, che sta più in alto di uno scostamento
proprio di ogni modello di antenna, radome e frequenza: qualche centimetro per le antenne topografiche, oltre 10 cm
per alcune choke ring. RTKLIB lo corregge con le calibrazioni delle antenne dell'IGS, **igs20.atx**, dove il programma
cerca l'antenna scritta nell'intestazione RINEX della base (tipo e radome) come fa RTKLIB. *Calibrazione dell'antenna
della base: scarica igs20.atx (62 MB), una volta* nella finestra (spuntata; `--atx download` da riga di comando, o
`ppg gnss atx --download`) lo scarica nella cartella GNSS del programma quando non ce l'ha; una volta lì ogni calcolo
lo applica, anche senza la spunta. Una stazione, una linea di base e un PPP lo scaricano comunque, perché tutte le
antenne siano calibrate, e rinnovano una copia più vecchia di 30 giorni, come fa `ppg gnss atx --download`;
`--atx off` non dà calibrazioni. Nella finestra, sotto la base, il programma dice se lo scostamento verrà applicato; dopo il
calcolo avvisa quando non lo è stato - nessuna calibrazione data (`--atx off`), l'antenna che non è in igs20.atx,
un'intestazione che non dice l'antenna - e quando RTKLIB ha preso la stessa antenna con un altro radome, o il nome più
vicino che ha trovato (qualche millimetro, di più se è un'altra antenna). Senza calibrazione ogni quota PPK risulta
più bassa di quello scostamento - lo stesso per tutte le foto di un calcolo, ma diverso da un'antenna di base
all'altra, così anche i rilievi calcolati con basi diverse differiscono di tanto (differenze di DEM, volumi):
controlla le quote su un punto a terra misurato, o scrivi la quota della base con lo scostamento verticale della sua
antenna già aggiunto. Il [rapporto PDF](exports.md#rapporto-pdf) dice, per ogni calcolo, la voce usata ("calibrata con
TRM57971.00 NONE di igs20.atx") o perché non ce n'era una.

**Foto con un fix RTK del drone stesso.** Una foto la cui posizione è un fix RTK del drone stesso - dall'XMP di una
foto DJI o da un MRK di DJI - lo tiene: il PPK serve per le foto che non sono fix. *Sostituisci anche i fix RTK del
drone* nella finestra (`--replace-fix` dalla riga di comando) dà anche a loro la posizione PPK, così che tutte le foto
stiano nel sistema della base; il programma avvisa quando un fix di bordo diventa float o singola. Quando i fix tenuti
sono a più di 10 cm (mediana) da dove il PPK mette gli stessi scatti (anche in quota quando le due quote sono
ellissoidiche, come in un MRK; solo in pianta per la quota di un EXIF), un avviso dice che cosa vuol dire
probabilmente: la base in un sistema di riferimento diverso da quello delle correzioni RTK del drone (una rete in ETRF
e una base in ITRF, o il contrario) o in una posizione sbagliata, e il volo ora li mescola - controlla la posizione
della base, o calcola di nuovo sostituendo anche i fix di bordo. Una foto che non è fix non sta sempre meglio con il
PPK: quando prende uno stato peggiore di quello che le aveva dato il drone (un float di bordo che nel PPK è singola),
un avviso conta quelle foto - la posizione PPK si usa lo stesso - e vanno controllati la base, la sua distanza dal volo
e le sue osservazioni.

**Il risultato.** Una foto prende l'antenna della soluzione all'ora del suo scatto (un'epoca, o la retta fra due
epoche distanti al massimo un secondo) più il braccio di leva del suo scatto nell'MRK; lo stato viene dalla qualità
di RTKLIB (fix, float - anche dgps - o singola) e l'accuratezza è il doppio delle deviazioni standard della
soluzione, con i minimi del suo stato. Una foto scattata dove la soluzione non ha una posizione (un buco di oltre un
secondo, prima dell'inizio, dopo la fine) tiene la posizione che aveva: niente si inventa attraverso un buco, e
quelle foto e quegli scatti sono elencati nel registro. *Solo le posizioni fix* si decide di nuovo. Il registro dice
anche quante foto hanno tenuto il loro fix di bordo, la base con la sua antenna, quanto è lontana dal volo (dalla
posizione mediana del drone nella soluzione: oltre 20 km un avviso, le ambiguità si fissano a fatica e le posizioni
sono per lo più float) e il file della soluzione. Ogni foto tiene, con la sua posizione, la base rispetto a cui è
stata calcolata - il nome, la posizione come longitudine, latitudine e quota (l'ordine di ogni posizione del
programma), come è stata posta e che cosa dice la fonte della sua posizione (la stazione e la soluzione delle sue
coordinate, la linea di base, il PPP e i suoi prodotti), il suo sistema e la sua epoca, l'antenna della sua
intestazione e la sua calibrazione - il file di osservazioni del drone e il file della soluzione del suo calcolo. Il
passo del pannello dice poi quante foto hanno una posizione PPK e di quale stato e, per ogni base, da dove viene la sua
posizione, in quale sistema e a quale epoca, con l'avviso quando la sua posizione è quella della sua intestazione. Con
posizioni accurate, *Ottimizza camere* tiene il blocco su di loro quando non lo tengono i punti di controllo
([Ottimizzare le camere](processing.md#ottimizzare-le-camere)).

RTKLIB (RTKLIB-EX 2.5.1, licenza BSD) sta nel nucleo nativo 0.5 o più recente, che le applicazioni contengono già (il
loro controllo, `--check`, fallisce se manca); senza, il programma dice che cosa installare. In alternativa
`PPG_RNX2RTKP` può indicare il programma `rnx2rtkp` di RTKLIB-EX 2.5. `ppg doctor` (*Aiuto > Diagnostica ambiente*)
dice dove gira il PPK: RTKLIB nel nucleo nativo e la sua versione, il programma indicato da `PPG_RNX2RTKP`, o che
cosa installare.

**Crediti.** Quello che il programma scarica è pubblico e libero, con i suoi crediti: le osservazioni e le coordinate
delle stazioni dell'EPN (CC BY 4.0; la soluzione pluriennale si cita con il DOI della sua cartella settimanale,
`10.24414/ROB-EUREF-Cwwww`) e di RING dell'INGV (CC BY 4.0), IGc20 e igs20.atx dell'IGS, i prodotti precisi di ESA,
CODE e dell'IGS con il loro nome, il geoide EGM2008 dell'NGA (dominio pubblico). Il rapporto PDF cita quelli che ogni
rilievo ha usato: la stazione e la cartella della soluzione delle sue coordinate, il centro dei prodotti di un PPP.

#### Dalla riga di comando

```bash
ppg gnss ppk rilievo.ppg --mrk /scheda/VOLO_Timestamp.MRK --rover /scheda/VOLO_Rinex.obs --base base.obs
#   --base FILE (ripetibile, per una base in più file: file orari, due giorni)
#   --nav FILE (ripetibile; di default quelli accanto alle osservazioni del drone, altrimenti a quelle della base)
#   --base-position LAT,LON,H (gradi, gradi, metri sull'ellissoide, il punto per i decimali;
#                              di default la posizione dell'intestazione RINEX della base, con il suo avviso)
#   --base-frame unknown|ITRF2020|ETRF2000|ETRF2014|ETRF2020, --base-epoch 2026.82 (o 2026-10-27):
#                              il sistema e l'epoca di --base-position (in ITRF2020 l'epoca di default è
#                              la metà delle osservazioni del drone)
#   --base-station ID (una stazione permanente come base: i suoi file si scaricano, niente --base)
#   --base-baseline ID (la tua --base posta con una linea di base statica dalla stazione ID)
#   --base-ppp (la tua --base posta con un PPP statico)
#   --atx auto|download|off (le calibrazioni delle antenne: igs20.atx se il programma ce l'ha, scaricato una volta, nessuna)
#   --replace-fix (anche le foto con un fix RTK del drone stesso prendono la posizione PPK)
#   --photos CARTELLA, --chunk NOME
ppg gnss stations rilievo.ppg                    # le stazioni permanenti entro 200 km dal rilievo
#   --near LAT,LON al posto di un progetto, --limit N, --refresh (gli elenchi scaricati di nuovo)
ppg gnss baseline base.obs --station AQUI00ITA   # il punto della tua base con una linea di base statica (--folder CARTELLA)
ppg gnss ppp base_1.obs base_2.obs               # il punto della tua base con un PPP statico (--folder CARTELLA)
ppg gnss atx --download                          # igs20.atx (62 MB): dov'è, o scaricato
```

`ppg gnss ppk` stampa le foto per stato, quante hanno tenuto il loro fix di bordo, la base usata (latitudine,
longitudine e quota, come è stata posta, il suo sistema e la sua epoca), l'altezza della sua antenna e quanto è
lontana dal volo, i file di navigazione scaricati per il volo, il file della soluzione del calcolo, gli avvisi (lo
scostamento del centro di fase dell'antenna della base quando non viene applicato; una base nella posizione della sua
intestazione, una posizione scritta lontana da lei o da quella di altre foto; quelli di una linea di base o di un PPP)
e le foto e gli scatti rimasti senza posizione PPK; salva il progetto, e si ferma con un errore, senza cambiare nulla,
quando i file non si possono usare (un file indicato che non c'è la ferma prima che si legga qualsiasi cosa). Un volo
le cui foto hanno tenuto tutte il loro fix di bordo non cambia nulla e non è un errore.
`ppg gnss baseline` e `ppg gnss ppp` stampano il punto trovato con il suo sistema, la sua epoca e le sue deviazioni standard, gli avvisi, e le
opzioni che lo danno a `ppg gnss ppk` (`--base-position ... --base-frame ITRF2020 --base-epoch ...`).

### Sistemi di riferimento ed epoche

Ogni posizione di una foto tiene il sistema di riferimento e l'epoca delle sue coordinate, e il programma la converte
a quell'epoca.

- **ITRF2020** (e IGS20, IGb20, IGc20, lo stesso per il programma): il sistema globale dei prodotti dell'IGS, delle
  coordinate delle stazioni permanenti e di un PPP. In ITRF2020 il terreno si muove con la sua placca - l'Europa di
  circa 2,5 cm l'anno - quindi una posizione in ITRF2020 ha bisogno della sua epoca. Un PPK rispetto a una stazione, o
  rispetto a una base posta con una linea di base o con un PPP, dà posizioni in ITRF2020 all'epoca delle coordinate
  della base.
- **ETRF2000**, e gli altri sistemi europei ETRF2014 ed ETRF2020, si muovono con la placca europea: un punto a terra
  tiene le sue coordinate. **RDN2008**, la rete di riferimento italiana (ed ETRS89 in Italia), è ETRF2000 all'epoca
  2008,0: il programma prende ETRF2000 come RDN2008.
- **Sconosciuto (WGS 84)**: l'RTK del drone stesso (una rete in ETRF, la base del drone), il GPS delle foto, le
  posizioni scritte prima dei sistemi: prese come WGS 84, come sempre.

**Perché ITRF2020 ed ETRF2000 differiscono di circa un metro in Italia.** I due sistemi coincidevano nel 1989, quando
ETRS89 è stato definito sulla placca europea; da allora l'Europa si è spostata nel sistema globale di circa 2,5 cm
l'anno verso nord-est, così che nel 2026 lo stesso punto ha nei due coordinate distanti circa un metro (0,99 m a
Milano, 1,03 m a Roma, 1,06 m a Palermo; le quote entro il centimetro). Un rilievo posto con una stazione in ITRF2020 e
disegnato su carte in RDN2008 è sbagliato di un metro quando i sistemi non si convertono, e ancora di 30 cm quando si
convertono senza l'epoca (PROJ da solo prende il 2015,0). Il programma converte da ITRF2020 a ETRF2000 - e quindi a
RDN2008 e a ETRS89 / UTM - con la trasformazione di Helmert dipendente dal tempo dell'EPSG (*ITRF2020 to ETRF2000 (1)*,
EPSG:10586) all'epoca di ogni posizione; da ITRF2020 a un sistema su WGS 84 le posizioni passano come sono (i due
coincidono entro i centimetri). Fra due sistemi europei - una base scritta in ETRF2014 o ETRF2020, un chunk in RDN2008
o ETRS89 / UTM - le posizioni passano da ITRF2020 alla loro epoca, con le due trasformazioni di Helmert dell'EPSG (PROJ
da solo prende una trasformazione nulla attraverso ETRS89, 8-9 cm di errore a Roma nel 2026): le foto all'epoca del
loro scatto, i marker letti da un file all'epoca del rilievo, che il registro dice. Il moto dell'Italia dentro la sua
placca dal 2008 - qualche millimetro l'anno, di più al sud e intorno ai vulcani - non c'è: RDN2008 è ETRF2000 al 2008,0.

Il sistema e l'epoca vengono con la posizione: una base scritta i suoi (*Sistema delle coordinate scritte*, *Epoca
delle coordinate*; `--base-frame`, `--base-epoch`); un file di posizioni in un sistema di ITRF2020 (`EPSG:9989` e i
sistemi su di lui) l'*Epoca delle posizioni* della sua finestra (`ppg gnss positions --epoch`; vuota, la data EXIF di
ogni foto); l'MRK e le foto sono *sconosciuto (WGS 84)*. I punti senza sistema ed epoca in un sistema di ITRF2020 - i
marker, le posizioni di un progetto fatto prima - si convertono come prima, con la trasformazione di PROJ dove ce n'è
una, altrimenti come posizioni su WGS 84: un'approssimazione, circa un metro in Europa, che il registro dice una
volta. Dalli di nuovo con la loro epoca, o in ETRF2000 (RDN2008).

Il pannello GNSS ha cinque passi: *Posizioni delle foto*, *Correzioni (PPK)*, **Sistema e quote**, *Qualità per volo*
e *Solo le posizioni fix*. *Sistema e quote* elenca i sistemi delle posizioni con le loro epoche, il sistema del chunk
e il tipo delle sue quote, se la griglia del geoide EGM2008 è installata e quello che non torna fra le quote delle foto
e quelle del chunk; i suoi pulsanti sono *Sistema del rilievo...* e *Scarica il geoide EGM2008 (81 MB)...* (sotto). Un
chunk in un sistema su WGS 84 con posizioni in ITRF2020 (una stazione, una linea di base, un PPP) insieme a riferimenti
in un sistema europeo (foto di una base scritta in RDN2008, marker letti da un file in RDN2008 come quelli di una rete
RTK italiana) viene segnalato lì, nel rapporto PDF e da `ppg gnss crs`: in Europa in un sistema così sono a circa un
metro fra loro, e in un sistema di ETRF2000 arrivano tutti esatti.

### Quote sul geoide e sistema del rilievo

Il GNSS dà quote sull'ellissoide; le quote di un rilievo sono sul livello del mare, il geoide, che in Italia sta a
40-50 m (48 m a Roma). Il programma converte fra le due con la griglia del geoide **EGM2008** (`us_nga_egm08_25.tif`,
81 MB, dominio pubblico), che non contiene e **scarica solo se glielo chiedi**: *Scarica il geoide EGM2008 (81 MB)...*
nel passo *Sistema e quote* del pannello GNSS, o `ppg gnss geoid --download`, nella cartella GNSS del programma,
controllata con la dimensione e lo SHA-256 pubblicati. PROJ non va mai in rete da solo. Senza la griglia ogni
conversione fra i due tipi di quota si rifiuta, con il nome della griglia e come averla: le quote sull'ellissoide non
passano mai per quote sul livello del mare. Una griglia tua - come **ITALGEO2005** dell'IGM (`geo_igm_mar06.grd`, le
quote di Genova 1942, EPSG:5214) - si copia nella cartella delle griglie del programma, che `ppg gnss geoid` stampa
(`proj` nella cartella GNSS); i sistemi con quelle quote, come `EPSG:7792+5214`, la usano allora.

**Il sistema del rilievo.** *Sistema del rilievo...* propone il sistema di coordinate del rilievo dove si trova. In
Europa (dove il datum è ETRS89) un sistema di ETRF2000: RDN2008 / UTM in Italia (EPSG:7791, 7792, 7793, il datum
nazionale) ed ETRS89 / UTM altrove, dove le posizioni in ITRF2020 (una stazione permanente, una linea di base, un PPP)
arrivano esatte alla loro epoca, come quelle nei sistemi europei (una base scritta in RDN2008, i marker di una rete
RTK), e niente si sposta con la placca fra voli di anni diversi; fuori dall'Europa la zona UTM su WGS 84. Si può
scegliere anche l'altro (la zona UTM su WGS 84 in Europa, ETRS89 / UTM per posizioni in un sistema europeo fuori), o
un altro sistema (*Altro:*, un codice EPSG o un WKT). *Quote sul geoide EGM2008* gli dà le quote sul livello del mare
(`+3855`); quando la griglia non è installata la finestra propone di scaricarla prima (*Scarica e applica*) o di
applicare lo stesso sistema senza quote proprie (*Applica senza quote sul geoide*). Prima che cambi qualsiasi cosa la
finestra calcola, in background, che cosa fa il cambio, lo mostra e chiede *Applicare?*:

- la georeferenziazione segue il chunk nel nuovo sistema, con il modello, le nuvole e i punti di legame;
- i riferimenti dei marker si convertono da un sistema all'altro, come coordinate misurate a terra; quello che è
  stato disegnato sui prodotti - unità, quote, punti di misura, pietre, le sezioni del progetto, le patch
  dell'ortofoto e della texture, la regione della ricostruzione, la carta dei colori - va con la georeferenziazione,
  così resta sul modello (su un chunk senza niente da seguire si converte come i marker); i poligoni caricati da
  PyArchInit si convertono anche loro come coordinate. Dove la nuova georeferenziazione non è la conversione di quella
  di prima (foto in ITRF2020 verso RDN2008, un calcolo su altri riferimenti) la finestra dice di quanto il modello si
  scosta da quella conversione e di quanto i riferimenti dei marker si allontanano da lui;
- che cosa va costruito di nuovo nel nuovo sistema: il DEM, l'ortofoto, la precisione del DEM, la copertura;
- che cosa resta nel sistema di prima e va rifatto se serve: le versioni non attive di DEM e ortofoto, i prospetti, i
  raster multispettrali, termici e satellitari, i rilievi del DEM e il calcolatore raster, i proxy delle US, le
  pietre disegnate su altri raster, i disegni (SVG, DXF) delle sezioni; le linee che sono solo nella finestra (curve
  di livello, piante a quota, misure) si tolgono, da ridisegnare.

Un chunk in coordinate locali prende solo il sistema: i suoi marker e i suoi disegni restano dove sono, e la sua
georeferenziazione si calcola dopo. *Modifica > Annulla* non attraversa il cambio: quello che era stato modificato prima
resta com'è nel nuovo sistema.

Un sito troppo grande per essere convertito con una sola similitudine fra i due sistemi (sbaglierebbe la pianta di
più di 5 mm) si rifiuta, senza cambiare nulla. Il [rapporto PDF](exports.md#rapporto-pdf) dice il sistema del chunk,
la griglia del suo geoide con il suo SHA-256 e come sono state convertite le posizioni in ITRF2020.

```bash
ppg gnss crs rilievo.ppg                    # il sistema e le sue quote, i sistemi delle posizioni, quello che non torna
ppg gnss crs rilievo.ppg --survey           # nel sistema di un rilievo dove si trova (--frame WGS84|ETRF2000 invece, --no-geoid)
ppg gnss crs rilievo.ppg --set EPSG:7792+3855
ppg gnss geoid                              # la griglia di EGM2008 installata o no, la cartella delle griglie
ppg gnss geoid --download                   # EGM2008 (81 MB, dominio pubblico)
ppg gnss positions rilievo.ppg camere.csv --crs EPSG:9989 --epoch 2026.82   # posizioni in ITRF2020 alla loro epoca
```

## Maschere

Le parti mascherate di una foto (in rosso) non si usano per allineare, nelle mappe di profondità, nell'ortofoto
o nella texture. *Strumenti > Maschere delle foto*:

* **disegna** una maschera sulla foto mostrata: clic sui vertici, doppio clic o ++enter++ chiude, ++esc++
  annulla; *Togli la maschera* o ++shift++ + doppio clic restituisce l'area; *Mostra le maschere* nasconde il
  rosso;
* **importale** da immagini, o dal canale alfa delle foto;
* **creale** da foto dello sfondo senza l'oggetto, dal colore di un fondale, dal modello (resta solo l'oggetto)
  o con SAM 2.1 (l'oggetto al centro di ogni foto);
* **esportale**, o toglile (dalle foto selezionate o da tutte).

*Allinea foto* può anche lasciare fuori i punti che non si muovono tra le foto: il turntable con la camera fissa.

## Marker

![Una foto con i suoi marker](../img/gui/view-photo.png)

I marker sono i target e i punti misurati nelle foto; *Flusso di lavoro > Rileva marker codificati...* trova da
solo i target codificati stampati.

**Un marker misurato in una sola foto** sta dove il suo raggio incontra la superficie (la nuvola densa,
altrimenti i punti di legame: un piano locale adattato attorno al raggio, largo circa 20 pixel). Così ha subito
delle coordinate, si vede sul modello e sull'ortofoto e può chiudere una barra di scala; misurato in una seconda
foto viene triangolato. Sul saggio sintetico i marker da una foto stanno entro 0,6 mm dai target rilevati.

**Marker senza foto**, come *Add Marker* di Metashape:

* *Marker* nella vista 3D: la vista si ferma; clic su un punto del modello o della nuvola (il punto mostrato più
  vicino sotto il cursore, affinato lungo la linea di vista sulla nuvola densa completa);
* *Marker* nella barra Disegno: clic sull'ortofoto o sul DEM (la quota dal DEM a piena risoluzione, altrimenti
  dalla nuvola lungo la verticale).

Il nuovo marker (`point 1`, `point 2`...) viene proiettato sulle foto che lo vedono, non nascosto dalla
superficie: bandierine blu, da trascinare in una foto per fissarlo. I marker sono disegnati sull'ortofoto come
layer *Marker*.

### Posizionamento guidato

Come in Metashape, un marker messo su una foto (o in 3D) riceve le **bandierine blu** sulle altre foto che lo
vedono (al massimo 60, prima quelle dove cade più vicino al centro), e il pannello *Foto* viene filtrato su di
esso.

1. Apri una foto con un doppio clic: è centrata sul marker a piena risoluzione.
2. Trascina la bandierina blu sul punto, o fai doppio clic su di essa quando è giusta: diventa verde.
3. ++page-down++ apre la foto successiva.

Ogni conferma posiziona di nuovo il marker (due bandierine verdi: triangolato) e sposta le bandierine blu delle
altre foto dove ora si trova. Le bandierine blu sono stime, non misure: non spostano mai un marker che ne ha una
verde, né entrano nell'ottimizzazione delle camere.

*Affina le bandierine blu* (clic destro in *Riferimenti*, per i marker selezionati o tutti, oppure *Flusso di
lavoro > Affina le bandierine blu dei marker*) cerca il punto di ogni bandierina blu nella sua foto per
correlazione d'immagine, sulla superficie attorno al marker: le bandierine trovate diventano misure
(automatiche, come i target rilevati), le altre restano blu da confermare a mano.

In *Riferimenti* il clic destro anche **unisce** i marker selezionati (un target codificato e il GCP messo a
mano sopra) o li **elimina**, e *Sposta nella vista 3D* riposiziona un marker con un clic sul modello o sulla
nuvola (per i marker messi in 3D o sull'ortofoto; quelli misurati nelle foto si spostano nelle foto).

## Barre di scala

Una barra di scala è la distanza nota tra due marker, come in Metashape.

1. In *Riferimenti* seleziona due marker (++ctrl++ o ++shift++ + clic) e scegli *Crea barra di scala tra A e
   B...* dal clic destro, oppure premi *Barra di scala...* (i marker selezionati vengono per primi nella
   finestra).
2. La finestra chiede la distanza in metri (tra i centri dei target), la sua accuratezza (pesa la barra quando
   ce ne sono più), se è una barra di check, e se aggiornare subito la scala.

| Tipo di barra | Cosa fa |
| --- | --- |
| **Barra di controllo** | dà la scala al chunk: la scala è la soluzione ai minimi quadrati di *scala x lunghezza nel modello = distanza*, ogni barra pesata con `1 / accuratezza²` (di due barre misurate ugualmente bene, conta di più la più lunga). Senza punti di controllo il chunk viene scalato in coordinate locali (CRS `LOCAL`): un oggetto, un reperto, un muro, un saggio |
| **Barra di check** (*Check* spuntato) | non dà la scala: il suo errore dice quanto è buona la scala |

!!! note "La distanza di una barra è in 3D"
    Con un dislivello è più della distanza in pianta misurata sull'ortofoto.

**Chunk georeferenziati**: una nuova barra viene proposta come check (misura la scala senza cambiarla). Una barra
di controllo su un chunk georeferenziato altrove (un progetto importato) senza riferimenti per rifarne
l'adattamento lo scala attorno alla barra, nel suo CRS; quando la scala cambia, il log dice che DEM e ortofoto
sono ancora nella vecchia e vanno ricostruiti.

**Foto con GPS**: quando le loro posizioni coprono meno di dieci volte la loro accuratezza (un telefono o un drone
sopra un oggetto o un saggio) restano fuori e le barre scalano il chunk in coordinate locali. Su un'area più
grande (un volo su un sito), o con tre o più punti di controllo, posizioni delle foto, punti di controllo e barre
vengono adattati insieme, ognuno pesato con `1 / accuratezza²`: posizioni e punti collocano e orientano il
rilievo, la scala viene da tutti.

**La tabella delle barre**, sotto i marker in *Riferimenti*, ha una riga per barra: *Usa* (togli la spunta per
lasciarla fuori), nome, marker, *Distanza* e *Accur.* (doppio clic per cambiarle: l'errore segue subito),
*Check*, la distanza misurata sul modello (*Stimata*) e l'errore, in rosso quando supera tre volte l'accuratezza
(almeno 5 mm). Il clic destro elimina le barre. Il riepilogo mostra *RMSE barre* (controllo) e *RMSE barre
check*, e come è stato collocato il chunk: scala dalle barre, coordinate locali, o scala da riferimenti e barre
insieme.

La vista 3D disegna le barre tra i loro marker, quelle di controllo in ciano e quelle di check tratteggiate in
arancio, con la loro lunghezza e l'errore; l'area di lavoro le elenca sotto il chunk.

### Importare e stampare le barre

* *Importa barre...* (oppure *Flusso di lavoro > Importa barre di scala (CSV)*): un file di testo con
  `marker_a, marker_b, distance[, accuracy[, check]]` per riga, separati da virgole, punti e virgola,
  tabulazioni o spazi; i marker per nome (`target 12`) o codice del target (`12`); una riga di intestazione viene
  saltata e le coppie i cui marker non sono nel chunk sono elencate nel log.
* *Strumenti > Target > Barra di scala stampabile...*: una pagina per barra con due target codificati a distanza
  nota (gli ultimi codici del dizionario, 48-49 per `aruco_4x4_50`, così non si confondono con i fogli di target
  e i tappetini), un righello di 10 cm per controllare che la stampa sia al 100 %, e il CSV delle barre accanto
  al PDF. Incolla la pagina su qualcosa di rigido, fotografala con l'oggetto, rileva i marker e importa il CSV.
* I progetti importati da Metashape portano le loro barre di scala (distanza, accuratezza, barre di check); i
  task di WebODM portano le pose delle camere e i loro GCP come marker, così si possono tracciare barre tra di
  essi.

### Le barre nell'ottimizzazione

*Ottimizza camere* usa anche le barre, come Metashape: con due o più barre di controllo, o con punti di
controllo, i marker agli estremi delle barre vengono fissati dove le barre hanno la loro lunghezza (ognuno
spostato quanto poco permette la sua precisione nelle foto, `MARKER_PIXEL_ACCURACY` 0,5 px) e il blocco si piega
verso di essi quanto permettono i punti di legame; una sola barra dà solo la scala. Il log riporta gli errori
delle barre prima e dopo.

??? info "Con il motore *Proprio*"
    Le barre sono distanze tra i loro marker dentro l'aggiustamento stesso, e i punti di controllo sono tirati
    verso le loro coordinate secondo la loro accuratezza invece di essere fissati. Possono partecipare le
    posizioni GNSS delle foto (con uno scostamento per volo stimato insieme al blocco: una base RTK su un punto
    sbagliato, un offset dell'antenna) e il rolling shutter dei droni (una velocità di ogni foto mentre il sensore
    viene letto). Su un terreno piano con sole foto nadirali uno scostamento verticale si scambia con la focale:
    servono GCP o foto oblique. Il log riporta lo scostamento di ogni volo e la velocità mediana.
