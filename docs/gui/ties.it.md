# Legami fra chunk

Applicazione **1.7.6**. Più rilievi dello stesso sito - campagne di scavo di anni diversi, un edificio rilevato a
pezzi, voli di giorni diversi - spesso non hanno niente che *Allinea chunk* possa usare: nessun marker con lo stesso
nome, nessuna foto in comune, superfici cambiate da un rilievo all'altro. I **legami** li mettono insieme con punti
scelti da te: lo stesso punto in due chunk, o un punto di un chunk con coordinate note. Poi tutti i chunk vengono
stimati insieme, e un rapporto mostra i legami sbagliati **prima** di applicare qualsiasi cosa.

*Allinea chunk* resta com'è (marker, foto, nuvole); i legami sono una seconda strada, per quando quelle non funzionano.

## Che cos'è un legame

Un legame è **lo stesso punto in due chunk**: l'angolo di un muro, una pietra, la testa di un picchetto, visti in
tutti e due i rilievi. Ogni estremo è un **marker** del suo chunk, creato da solo e chiamato "legame N" (o con il nome
che dai). Così tutto quello che fa un marker lo fa anche l'estremo di un legame: si misura sulle foto, si triangola,
si mette sulla superficie, si affina. Gli estremi tengono la loro posizione nelle coordinate interne del loro chunk: un
legame sopravvive a ogni nuova trasformazione dei suoi chunk (*Allinea chunk*, una stima, l'orientamento di un sistema
locale).

Un nuovo **allineamento delle foto** di un chunk è un'altra cosa: mette il chunk in un nuovo sistema interno. Gli
estremi misurati sulle foto vengono triangolati di nuovo e restano giusti. Gli estremi messi senza foto - cliccati su
un raster, scritti dalla riga di comando, letti da una tabella - tengono i loro vecchi numeri interni, che nel nuovo
sistema sono da un'altra parte, tutti spostati allo stesso modo: nessun controllo della stima potrebbe vederlo. Così,
dopo un nuovo allineamento, quegli estremi sono **da rimisurare**: il legame resta nell'elenco, in arancione, con il
motivo in *Stato*, ed è escluso dalla stima (lo dice anche il registro della finestra). Trascina di nuovo l'estremo sul
suo punto in *Punti in comune*, misuralo sulle foto o - solo se hai controllato che è ancora giusto - seleziona il
legame e premi **Conferma estremi** (`ppg ties confirm` dalla riga di comando). Aggiungere foto a un
allineamento (*Allinea foto* senza *Azzera l'allineamento attuale*) conta anch'esso come un nuovo allineamento: COLMAP
riparte dal blocco vecchio ma lo scala e lo sposta di nuovo (sulla trincea di prova le camere vecchie si sono mosse di
una similitudine di scala 0,90), quindi gli estremi senza foto sono da rimisurare allo stesso modo.

Un legame può anche unire un punto di un chunk a **coordinate note** (un punto GNSS, un caposaldo): quell'estremo è
un marker con le coordinate come riferimento.

Ogni legame ha una **precisione** (metri), che ne dà il peso nella stima (1 / precisione²). Per impostazione
predefinita:

| L'estremo è stato | Precisione |
| --- | --- |
| misurato su due o più foto | l'errore di riproiezione del marker convertito in metri, almeno 1 cm |
| misurato su una sola foto | due celle del DEM (il punto sta sulla superficie) |
| cliccato su un'ortofoto o un DEM | due pixel del raster |
| letto da una tabella | il valore della tabella (predefinito 5 cm) |

I due estremi si combinano. La precisione di ogni legame si può cambiare a mano.

## Fare i legami

Ci sono quattro modi:

* **Punti in comune** (*Flusso di lavoro > Punti in comune...*): due chunk affiancati, un clic in ognuno (vedi
  [sotto](#punti-in-comune));
* **Da marker omonimi**: nella finestra dei legami, due chunk; un legame per ogni nome di marker presente in tutti e
  due;
* **Importa...**: una tabella (CSV) con un legame per riga:
  `chunk_a,x_a,y_a,z_a,chunk_b,x_b,y_b,z_b[,precisione][,nome]`, ogni punto nelle coordinate del mondo del suo chunk;
  `chunk_b` vuoto vuol dire che il secondo punto sono coordinate note; una z vuota si legge dal DEM del chunk;
* la **riga di comando**: `ppg ties add`, `ppg ties known` (vedi [la riga di comando](#riga-di-comando)).

Due altri tipi di registrazione legano i chunk senza scegliere i punti a mano: una **quota stabile** (la quota di un
chunk rispetto a un altro dove il terreno non è cambiato, [più sotto](#quota-da-una-superficie-stabile)) e i
**picchetti e capisaldi** trovati da soli vicino a punti di coordinate note ([più sotto](#capisaldi-e-picchetti)).

## La finestra dei legami

*Flusso di lavoro > Aggancia chunk con legami...* apre la finestra dei legami. Non è modale: può restare aperta mentre
lavori nella finestra principale e in *Punti in comune*.

![La finestra dei legami dopo Controlla: il legame sbagliato in rosso, una quota stabile (arancione: da rimisurare dopo Applica)](../img/gui/ties-dialog.png)

**Legami.** Una riga per legame: *Attivo*, nome, i due estremi (`chunk / marker`, o le coordinate note),
*Precisione (m)* (modificabile), *Residuo (m)* dell'ultimo controllo e *Stato*. Un legame con un estremo che non c'è
più (un marker tolto, un chunk tolto, un riferimento cancellato) è grigio e non si può spuntare; *Stato* dice perché.
Quando l'estremo torna (*Modifica > Annulla* della rimozione) il legame è di nuovo intero, attivo o no come era.
Un legame non attivo resta nell'elenco: spuntalo di nuovo per usarlo. Un legame con un estremo da rimisurare dopo un
nuovo allineamento è arancione (vedi [sopra](#che-cose-un-legame)): *Conferma estremi* conferma gli estremi dei legami
selezionati (se nessuno è selezionato, di tutti). *Rimuovi* cancella i legami selezionati e i marker che avevano
creato. La finestra segue i chunk aggiunti, tolti o rinominati nell'area di lavoro.

**I legami e i cambiamenti dei chunk.** Togliere un chunk lascia i suoi legami nell'elenco, grigi (*chunk tolto*):
rimuovili a mano. *Unisci chunk* crea un chunk nuovo: i legami puntano ancora ai chunk di partenza e si rompono quando
questi vengono tolti - rifalli sul chunk unito. *Aggiungi progetti* copia i chunk di un altro progetto con i loro
marker e i legami fra i chunk che prendi; un legame verso un chunk che lasci fuori non viene copiato (il riepilogo lo
nomina). Un progetto con legami salvato da una versione precedente (1.7.5 o prima) tiene i suoi
legami: vengono riconosciuti di nuovo dalla chiave e dal nome dei loro marker.

**Quote stabili.** Sotto i legami, la tabella delle quote stabili con *Misura quota stabile...*, *Rimisura* e
*Rimuovi* (vedi [quota da una superficie stabile](#quota-da-una-superficie-stabile)).

**Incognite per chunk.** Per ogni chunk, che cosa la stima può cambiare:

| Incognite | Parametri | Quando |
| --- | --- | --- |
| **similitudine** | 7: rotazione, spostamento, scala | un rilievo con un sistema e una scala propri; almeno 3 legami ben distribuiti |
| **rigido** | 6: rotazione e spostamento | la scala è giusta (barre di scala, punti di controllo) |
| **in pianta** | 4: rotazione attorno alla verticale, spostamento in x, y e z | inclinazione e scala sono già giuste (GPS o GNSS del drone); funziona con 2 legami |
| **solo spostamento** | 3 | è sbagliata solo la posizione |
| **solo quota** | 1 | è sbagliata solo la quota |
| **fisso** | 0 | questo chunk dà il sistema |
| *non nella stima* | | il chunk resta fuori |

La finestra propone: il chunk attivo **fisso**; *in pianta* per un chunk georeferenziato dai suoi riferimenti o con
2-3 legami; *similitudine* per gli altri; un chunk senza legami fuori dalla stima. **In pianta** ruota attorno alla
verticale del mondo (l'asse z del sistema di coordinate, o del sistema locale); inclinazione e scala del chunk restano
come sono.

La stima ha bisogno di un sistema: almeno un chunk **fisso**, o coordinate note. Senza, viene rifiutata con un
messaggio. Anche la *similitudine* con 2 soli legami è rifiutata (le equazioni non bastano): scegli *in pianta*.

**Opzioni.** *Robusto* usa i pesi di Huber: un legame sbagliato pesa meno, e gli altri non vengono tirati verso di
lui. *Usa i riferimenti dei marker* fa contare come punti noti i marker di controllo dei chunk (con coordinate).

**Asse dritto** (facoltativo). Per due o più chunk, due marker di ogni chunk che stanno su una stessa linea dritta -
l'asse di un muro, di uno scafo, di una strada. Dopo la stima il rapporto dà l'angolo fra le direzioni nei diversi
chunk; oltre 2 gradi è un avviso. Un legame sbagliato che fa ruotare un chunk si vede qui anche quando le distanze non
lo vedono.

## Controllare prima di applicare

**Controlla** stima insieme tutti i chunk scelti e scrive il rapporto. Non cambia niente.

Il rapporto dà:

* il **sistema della stima** e i **gradi di libertà in avanzo** (equazioni meno incognite). Con avanzo 0 non c'è
  **nessun controllo**: un legame sbagliato non si vede, e il rapporto lo dice in rosso;
* lo **scarto quadratico medio** e **sigma0** (come stanno i residui rispetto alle precisioni: circa 1 quando le
  precisioni sono giuste);
* gli **Avvisi** in rosso:
    * **distanze fra i legami**: per ogni coppia di legami fra gli stessi due chunk, la distanza fra i due punti in un
      rilievo e nell'altro. Non dipende dalla stima. Una coppia che differisce più di 3 volte la sua precisione
      combinata non torna; il legame presente in più coppie che non tornano è indicato come **probabilmente
      sbagliato**;
    * **togli un legame per volta**: ogni legame viene tolto a turno e gli altri ne collocano gli estremi; quando
      finiscono lontani (più di 3 volte la sua precisione combinata con la precisione con cui li collocano gli altri
      da soli - un chunk tenuto da pochi legami vicini fra loro è collocato male lontano da loro), il legame è
      indicato. Così un piccolo errore su un legame molto preciso (sotto circa 4-5 volte la precisione combinata) può
      non essere indicato qui quando il suo chunk ha le incognite **rigido**; le incognite **similitudine** lo
      indicano, e possono farlo anche le distanze fra legami e i residui;
    * **residui grandi**: un legame con un residuo oltre 3 volte la sua precisione;
    * **movimenti non plausibili**: un chunk georeferenziato da GPS o GNSS ruotato di più di 5 gradi, o scalato fuori
      da 0,98-1,02. Un chunk in un sistema locale non ha limiti;
    * l'**asse dritto** piegato di più di 2 gradi;
* i **Consigli** in arancione: con quale precisione ogni chunk è posizionato ai suoi bordi, e perché quando è
  scarsa (legami quasi su una linea o tutti da una parte, legami che non tornano fra loro, o un chunk appeso a un
  altro posizionato male). Sono suggerimenti, non errori;
* per ogni chunk: rotazione, spostamento e scala rispetto a prima, e la sua precisione;
* i **residui** di ogni legame, in pianta e in quota, con il risultato se viene tolto;
* gli **Esclusi**, in grigio: i legami non usati e perché (non attivo, un estremo senza posizione, un chunk non nella
  stima...).

Anche le righe dei legami sospetti diventano rosse nella tabella. Qualsiasi cambiamento dopo il controllo - un legame
spento, una precisione, le incognite, un'opzione - annulla il controllo: premi di nuovo **Controlla**.

## Applicare

**Applica** usa l'ultimo controllo. Viene rifiutato, con un messaggio, quando i legami, i marker o i chunk sono
cambiati dopo il controllo (di nuovo *Controlla*). Con avanzo 0 una domanda a parte chiede la conferma: *Applica senza
controllo* o *Non applicare* (la scelta predefinita).

La stima cambia **solo la trasformazione** dei chunk (e il loro sistema di coordinate, quello della stima), come fa
*Allinea chunk*. Foto, nuvole e modello seguono da soli. **DEM, ortofoto** e gli altri raster sono griglie nel sistema
di prima: il messaggio dopo *Applica* li elenca, e *Flusso di lavoro > Ricostruisci i prodotti da rifare* li rifà. Il
rapporto dell'ultima stima resta nel progetto.

Tutta l'applicazione è **un passo** di *Modifica > Annulla*: trasformazioni, sistemi di coordinate e legami tornano
come erano. Anche ogni modifica dei legami - importa, da marker omonimi, rimuovi, attivo, precisione, un legame fatto
in *Punti in comune* - è un passo di *Annulla*, con i marker dei suoi estremi.

## Punti in comune

*Flusso di lavoro > Punti in comune...* (o *Punti in comune...* nella finestra dei legami) mostra **due chunk
affiancati**, ognuno con la sua ortofoto, il suo DEM o un rilievo (SLRM e gli altri già fatti). Le due viste sono
**collegate**: ingrandisci e sposta in una, l'altra segue, sullo stesso punto del mondo dove lo mettono adesso le
trasformazioni, con gli stessi metri per pixel. Il cursore di una vista si vede come una croce rossa nell'altra.

![Punti in comune: due chunk affiancati, il legame sbagliato in rosso con il suo residuo](../img/gui/ties-common-points.png)

**Un legame in due clic.** Con *Legame* premuto (lo è all'apertura): un clic su un punto in una vista, poi un clic
sullo stesso punto nell'altra. Dopo il primo clic l'altra vista salta dove le trasformazioni mettono quel punto, e
l'estremo è disegnato come "legame a metà". La quota di ogni estremo si legge dal DEM del suo chunk (senza un valore
del DEM lì: niente legame). *Esc*, o *Annulla il legame a metà* nel menu del tasto destro, toglie il legame a metà. Un
secondo clic nello stesso chunk sposta lì il primo estremo.

**I legami nelle viste.** I legami sono disegnati come punti (azzurri; gialli se selezionati; rossi se sospetti;
grigi se non attivi). Dopo *Controlla* ognuno ha il suo residuo come una linea rossa, 20 volte più lunga del vero. Un
clic su un legame lo seleziona (anche la sua riga nella finestra dei legami); trascinare un estremo lo sposta, con la
quota dal DEM. Un estremo misurato sulle foto non si trascina: spostane le bandierine sulle foto. *Controlla* e
*Applica* fanno quello che fanno nella finestra dei legami.

**Estremi sulle foto.** Dove l'ortofoto è morbida (sott'acqua, in ombra) o un punto si vede solo di lato, misura
l'estremo su una foto originale:

1. tasto destro in una vista di *Punti in comune*: **Foto del punto** - quel chunk diventa il chunk attivo e il
   pannello *Foto* elenca le sue foto che vedono il punto;
2. apri una foto; tasto destro sul punto esatto: **Estremo del legame su questa foto**.

Con un legame a metà nell'altro chunk questo fa il legame; senza, il punto della foto è il primo estremo. Con un
legame selezionato, il menu propone anche di misurarne l'estremo **anche su questa foto** (*Estremo di «P3» anche su
questa foto*). Mentre *Punti in comune* è aperta, il menu del tasto destro di una foto allineata nel pannello *Foto* ha
anch'esso *Estremo del legame su questa foto*: apre la foto dove le trasformazioni mettono il legame a metà (una croce
rossa); poi tasto destro sul punto esatto. Un estremo su due o più foto viene triangolato; su una sola foto viene
messo sulla superficie (nuvola o punti di legame).

**Due sistemi diversi.** Un chunk in un sistema locale accanto a un chunk in un sistema del mondo non si può
collegare: le viste sono sovrapposte (centro su centro), e una nota arancione lo dice. I legami si possono fare lo
stesso.

## Quota da una superficie stabile

Due rilievi dello stesso sito spesso coincidono in pianta - il GPS di un drone li mette a pochi centimetri l'uno
dall'altro - e non in quota: decine di centimetri, a volte metri. Dove il terreno non è cambiato fra i due (la sabbia o
il campo intorno a uno scavo, una strada, un pavimento) i loro DEM devono stare alla stessa quota. Una **quota
stabile** misura lì di quanto un chunk sta più in alto di un altro, e dà alla stima solo quella quota.

*Flusso di lavoro > Misura quota stabile...* (o *Misura quota stabile...* sotto la tabella *Quote stabili* della
finestra dei legami) chiede:

* il **chunk** (B) e il chunk **rispetto a** cui misurarlo (A): tutti e due con un DEM;
* l'**area**: *Automatica* (pendenze basse e celle concordi, su tutta la sovrapposizione) o un layer di poligoni dei
  disegni liberi - disegna il terreno che sai non essere cambiato, o usa una sua maschera di colore;
* cosa **escludere** (facoltativo): un layer di poligoni sulle buche, i cumuli di terra, la vegetazione;
* **piano**: un piano (quota e inclinazione, tre ancore) invece di uno spostamento unico. Usalo quando i due rilievi
  possono essere inclinati l'uno rispetto all'altro (un drone con il solo GPS) e il terreno stabile si estende sul sito.

I due DEM sono letti come sono messi adesso: nessuno spostamento in pianta viene misurato o fatto. Un DEM costruito
prima che una stima spostasse il suo chunk viene spostato con il chunk, come lo mostra la vista 2D. Un chunk in un
sistema locale e uno in un sistema del mondo sono rifiutati, come per i legami.

La misura gira in sottofondo e aggiunge una riga alla tabella **Quote stabili**: nome ("quota stabile N"), i due chunk,
*dz* (di quanto B sta più in alto di A), l'NMAD delle differenze sul terreno stabile, le celle stabili, *Piano*,
*Attivo*, il *Residuo* dell'ultimo controllo e *Stato*. La sua **precisione** è l'NMAD diviso la radice delle celle,
contando al massimo 100 celle (celle vicine non sono indipendenti: una grande superficie di sabbia non deve pesare più
di tutti i legami), e almeno 5 mm; ogni ancora di un piano conta √3 volte tanto. Misurare, rimisurare, togliere e
attivare o disattivare una quota stabile sono ognuno un passo di *Modifica > Annulla*.

**Nella stima.** Una quota stabile lega i chunk solo in z: il controllo e la stima usano quelle attive fra chunk della
stima, e i loro residui sono nel rapporto (sezione *Quote stabili*). Un chunk tenuto solo da quote stabili viene
proposto con **solo quota**. Non entrano mai nelle distanze fra legami né nel *leave one out*: non hanno punti
corrispondenti.

**Rimisurare.** Una quota stabile è stata misurata dove i chunk stavano in pianta in quel momento. Quando una stima li
sposta l'uno rispetto all'altro in pianta di più di 2 celle del DEM di A (e almeno 10 cm), la riga diventa arancione e
*Stato* dice di quanto: dopo un controllo "da rimisurare dopo Applica"; dopo *Applica* "da rimisurare". Quindi
l'ordine solito è: prima legare i chunk in pianta (legami, *Applica*), poi premere **Rimisura**: le quote scelte
(nessuna scelta: quelle in arancione) sono misurate di nuovo dove i chunk sono adesso, con gli stessi chunk, aree e
tipo, e tengono nome e stato.

## Capisaldi e picchetti

Capisaldi, punti GNSS e i picchetti della quadrettatura dello scavo sono spesso nelle foto: una testa scura e rotonda
a terra, con le coordinate note nella tabella del topografo. *Cerca picchetti...* li cerca vicino a quelle coordinate e
propone ognuno come **legame a coordinate note** - un punto del chunk con coordinate note. Niente viene aggiunto finché
non lo accetti.

![Cerca picchetti: le proposte, una spostata di 40 cm in rosso, e la testa sull'ortofoto e su una foto](../img/gui/stakes-dialog.png)

Si apre con **Cerca picchetti...** nel pannello [*Riferimenti*](panes.md#riferimenti) o da *Flusso di lavoro > Cerca
picchetti...*. Non è modale.

**Prima rileva i target codificati** (*Flusso di lavoro > Rileva marker codificati...*). La loro impronta resta fuori
dalla ricerca, ma su un'ortofoto grossolana (circa 1 cm per pixel o più) le celle nere di un target non rilevato sono
piccole come una testa e possono essere proposte.

**Punti noti**, da una di queste fonti:

* un **file CSV**: `nome,x,y[,z][,precisione]`, separati da virgole, punti e virgola, tabulazioni o spazi, con una riga
  d'intestazione ammessa (la virgola decimale va bene quando il separatore non è la virgola). Le coordinate sono nel
  sistema del chunk, o in quello scritto in *Sistema del CSV* (un codice EPSG). Senza z la quota viene dal DEM; senza
  precisione 5 cm;
* i **legami a coordinate note del progetto**;
* i **marker con coordinate** di un chunk (i suoi punti di controllo, per esempio di un altro rilievo).

Poi scegli il **chunk** (serve un'ortofoto), il **raggio** intorno a ogni punto (predefinito 3 m) e il **diametro
della testa** (predefinito da 3 a 12 cm), e premi **Cerca**. In sottofondo, vicino a ogni punto:

1. sull'ortofoto: macchie scure almeno 25 livelli di grigio più scure di quello che hanno intorno, rotonde (circolarità
   almeno 0,6), piene e della misura di una testa; radici lunghe, grandi chiazze e le celle dei target codificati
   restano fuori;
2. sulle foto: ogni candidato viene cercato nelle foto che lo vedono (al massimo 12). Quando almeno **3 foto
   concordano** (entro 2 pixel, o un quarto della testa) il punto viene triangolato da quelle, con la precisione delle
   foto; altrimenti viene proposto come **solo ortofoto**, con la precisione di 2 pixel dell'ortofoto. Foto mancanti o
   illeggibili (segnaposto da 0 byte di una cartella "solo online") lasciano una proposta solo ortofoto, mai un errore;
3. al massimo 5 candidati per punto, prima quelli confermati dalle foto, poi per scurezza, rotondità e distanza.

**La tabella.** Una riga per punto noto: la proposta (x, y, z), le *Foto concordi* (o *solo ortofoto*), il
*Contrasto*, lo *Spostamento* dal punto noto e un *Avviso*: *nessuna testa trovata*, *fuori dall'ortofoto*, *nessuna
foto allineata*, *foto illeggibili*, gli altri candidati. Le proposte vengono controllate fra loro prima di
accettarne una, come i legami: le **distanze** fra ogni coppia di teste trovate devono tornare con quelle fra i loro
punti noti (un punto che non torna con la maggior parte degli altri è **sospetto**), e lo **spostamento** di ognuna
deve tornare con la mediana di tutti (un rilievo spostato in blocco si vede nella mediana, scritta sotto la tabella;
un punto sbagliato - un errore di battitura, un altro picchetto - come un punto **spostato diversamente dagli altri**).
Queste righe sono in rosso: controllale prima di accettarle.

A destra, l'**immagine** della proposta scelta: l'ortofoto intorno e la foto che la vede meglio, con una croce rossa
sulla testa proposta. Guardala prima di accettare.

| Pulsante | Cosa fa |
| --- | --- |
| *Accetta selezionati* | le proposte scelte diventano legami a coordinate note |
| *Accetta i concordi* | le proposte confermate dalle foto e non in rosso |
| *Scarta* | toglie le proposte scelte dall'elenco; il progetto non cambia |
| *Scegli un altro candidato* | la proposta scelta passa alla testa successiva trovata vicino al suo punto (quando c'è) |

**Accettare** crea, per ogni proposta, un legame a coordinate note con il nome del punto: un marker del chunk sulla
testa, con le coordinate note come riferimento (un punto senza z prende la quota della testa) e la precisione della
testa e del punto combinate. Le foto che concordavano diventano misure del marker (bandierine fissate), e solo i nuovi
marker vengono triangolati: gli altri marker del chunk restano dove sono. Tutta l'accettazione è **un passo** di
*Modifica > Annulla*. Le proposte accettate escono dall'elenco; i legami compaiono nella finestra dei legami, da
controllare e applicare come tutti gli altri.

Una proposta appartiene all'allineamento in cui è stata trovata: dopo un nuovo allineamento delle foto del chunk,
accettarla viene rifiutato - cerca di nuovo. La finestra lo dice sotto la tabella, e anche quando il chunk è stato
spostato da una stima dopo la ricerca (accettare resta giusto, ma coordinate, spostamenti e righe rosse sono di prima)
o rimosso.

## Riga di comando

```bash
ppg ties add scavo.ppg 2024 1.20,3.40 2025 1.18,3.52      # un legame; senza z: dal DEM di ogni chunk
ppg ties known scavo.ppg 2025 5.0,2.0,1.1 100.0,200.0,10.5 # un punto con coordinate note
ppg ties from-markers scavo.ppg 2024 2025                   # marker con lo stesso nome
ppg ties import scavo.ppg legami.csv
ppg ties list scavo.ppg [--json]
ppg ties confirm scavo.ppg [LEGAME ...]                     # estremi ancora giusti dopo un nuovo allineamento
ppg ties height scavo.ppg 2025 2024 [--layer Sabbia | --area stabile.gpkg] [--exclude Scavo] [--plane]
ppg ties heights scavo.ppg [--json]                         # le quote stabili
ppg ties remeasure scavo.ppg "quota stabile 1"              # rimisurata dove i chunk sono adesso
ppg ties check scavo.ppg --unknowns 2024=fixed,2025=plan [--robust] [--no-references]
ppg ties solve scavo.ppg --unknowns 2024=fixed,2025=plan [--yes]
ppg stakes find scavo.ppg 2025 capisaldi.csv [--crs EPSG:3004] [--radius 3] [--head 0.03,0.12] [--out prop.json]
ppg stakes accept scavo.ppg 2025 prop.json --ids CS1,CS4 [--yes]   # o --agreeing: quelle confermate dalle foto
```

`ppg ties height` misura di quanto CHUNK sta più in alto di REFERENCE sul terreno stabile (un layer di poligoni dei
disegni liberi con `--layer`, un file GeoPackage, GeoJSON o DXF con `--area`; nessuno dei due: trovato da solo) e lo
tiene come quota stabile; `check` e `solve` usano quelle attive. `ppg stakes find` prende come punti noti un file CSV,
`ties` (i legami a coordinate note del progetto) o il nome di un chunk (i suoi marker con coordinate); stampa una riga
per punto e il controllo (spostamento mediano, sospetti, punti spostati diversamente dagli altri), e scrive le
proposte in un file JSON con accanto un'immagine `<punto>.png` per ognuna. Il progetto non cambia. `ppg stakes accept`
stampa cosa farebbe e senza `--yes` non cambia niente.

`check` stampa lo stesso rapporto della finestra, senza l'asse dritto (in inglese), e non cambia niente. `solve` chiede prima di applicare (o `--yes`);
con avanzo 0 fa una domanda a parte, e `--yes` richiede anche `--accept-no-check`. Le coordinate negative si scrivono
come sono, o dopo `--`. Tutte le opzioni sono nell'[API Python](../api.md#allineare-e-unire-i-chunk).
