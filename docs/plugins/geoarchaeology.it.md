# Geoarcheologia

Il plugin *Geoarcheologia* trasforma i log dei sondaggi - trivellate manuali, carotaggi meccanici,
saggi, colonne misurate sulla parete di una sezione di scavo - nella stratigrafia sotto il rilievo
fotogrammetrico: la colonna di ogni sondaggio, la superficie dove comincia ogni unità, lo spessore e il
volume di ogni unità, sezioni lungo qualsiasi linea e un modello 3D con un solido per unità. Tutto sta sul
DEM e sull'ortofoto del progetto, nel suo sistema di riferimento.

Questa pagina descrive la versione 0.1.1 del plugin, che richiede pyarchinit-photogrammetry 1.2.2 o
successiva. Il README e il codice sono nel
[repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/geoarchaeology).

<figure markdown="span">
  ![Ombreggiatura del DEM con i sondaggi e le tracce di due sezioni](../img/plugins/geoarchaeology/boreholes-and-traces.jpg)
  <figcaption>Sondaggi simulati (stratigrafia inventata) sul DEM reale di Tell Sultan, in un sistema di
  riferimento locale. Cerchi: sondaggi che raggiungono il naturale; triangoli: sondaggi che si fermano
  prima. La linea tratteggiata delimita le superfici, le linee arancioni sono le tracce di due
  sezioni.</figcaption>
</figure>

!!! note "L'esempio di questa pagina"
    Figure e numeri vengono da una dimostrazione con 18 sondaggi **simulati**: una stratigrafia inventata
    di humus (US 1), crollo e colluvio (US 2), depositi dell'età del Ferro (US 3) e del Bronzo (US 4)
    sopra un naturale sterile (US 5), messa sul DEM reale di Tell Sultan - il sito del
    [tutorial](../tutorials/tell.md) - spostato in un sistema di riferimento locale. Mostrano che cosa fa
    il plugin e non dicono nulla del sito. I [file di esempio](#example-files) contengono gli stessi dati.

## In breve

1. Rileva il sito e costruisci il DEM (e l'ortofoto) in un chunk con un sistema di riferimento
   proiettato, in metri.
2. Misura la posizione di ogni sondaggio nello stesso sistema di riferimento; descrivi ogni log
   intervallo per intervallo.
3. Scrivi una tabella CSV dei collari e una degli intervalli, oppure una tabella sola.
4. Nel pannello *Geoarcheologia*: *Importa sondaggi...*, controlla l'*Ordine delle unità...*, poi
   *Superfici...*.
5. Guarda le superfici sopra l'ortofoto, disegna sezioni e colonne stratigrafiche, costruisci il modello
   3D.
6. Esporta in GeoPackage, GeoTIFF, DXF, PLY/OBJ e CSV, oppure apri tutto in QGIS.

## Che cosa fa

| Prodotto | Che cos'è | A che cosa serve |
|---|---|---|
| Colonne stratigrafiche | il log di un sondaggio con le sue descrizioni, oppure un pannello di più sondaggi affiancati su un asse comune delle quote, con i tetti della stessa unità collegati | documentare e correlare i log |
| Superfici di contatto | il tetto di ogni unità fra i sondaggi - il suo contatto con l'unità sopra - interpolato per kriging, con la sua incertezza | dove comincia il naturale sotto un tell, un piano sepolto, la base dell'humus |
| Spessori e volumi | lo spessore di ogni unità (isopache) e il suo volume | quanto sono profondi i depositi, volumi da scavare, crescita di un tell |
| Sezioni | il terreno, i contatti e le unità lungo una linea, con i sondaggi vicini riportati, in scala | tavole per la relazione, verifica di una correlazione |
| Modello 3D | un solido chiuso per unità, con il suo volume | QGIS 3D, Blender, MeshLab, didattica |

Serve ogni volta che pochi fori devono raccontare che cosa c'è sotto una superficie rilevata: una
campagna di carotaggi su un tell, una griglia di trivellate in un campo prima dello scavo, le sezioni di
trincee separate da collegare. Il plugin disegna quello che dicono i log e riempie lo spazio fra di essi
con la regolarità che i dati permettono. La correlazione dei log - quale strato di un sondaggio è lo
stesso di quale strato di un altro - resta il lavoro dell'archeologo: si scrive nei codici delle unità.

## Che cosa serve prima { #before }

* **Il plugin**, installato dal programma: vedi [Guide ai plugin](guides.md#install).
* **Un progetto con un chunk elaborato fino al DEM** (*Flusso di lavoro > Costruisci DEM...*), meglio
  anche con l'ortofoto (*Flusso di lavoro > Costruisci ortofoto...*). Il plugin prende il terreno dal DEM:
  il tetto della prima unità, la quota dei collari che non ce l'hanno, la superficie a cui vengono
  appese le profondità. Usa il DEM che il chunk mostra, la sua versione attiva: se dentro ci sono alberi o edifici,
  classifica la nuvola densa (*Strumenti > Classificazione della nuvola*), costruisci un DEM dalla sola
  classe *Terreno* e rendi attiva quella versione (tasto destro sulla versione nell'*Area di lavoro*,
  *Rendi attiva (la usano gli altri strumenti)*). Senza DEM il plugin funziona lo stesso - il terreno
  viene interpolato fra le quote dei collari, che allora dovrebbero averla tutti - ma senza DEM né
  ortofoto non c'è dove tracciare una sezione: si possono fare solo sezioni per i sondaggi.
* **Un sistema di riferimento proiettato, in metri**, per il chunk - per esempio il fuso UTM del sito - e
  i sondaggi **misurati nello stesso sistema**. Il plugin mette ogni sondaggio dove dicono la sua x e la
  sua y, sulla griglia del DEM, e misura distanze, aree e volumi nell'unità delle coordinate: sondaggi
  misurati in un altro sistema (latitudine e longitudine, un altro fuso, la griglia locale dello scavo)
  finiscono altrove o da nessuna parte. Convertili prima, per esempio in QGIS. Va bene anche un sistema
  locale, se rilievo e sondaggi stanno tutti e due lì, come nella dimostrazione; allora però i GeoTIFF non
  hanno un sistema di riferimento e il progetto QGIS del programma lascia fuori i layer del plugin.
* **I log** dei sondaggi in tabelle CSV: la sezione che segue.

## I dati

### Due tabelle, oppure una

Il plugin legge file CSV: tabelle di solo testo, che ogni foglio di calcolo sa scrivere. Due modi:

* **due tabelle**: i *collari*, una riga per sondaggio (dove si trova), e gli *intervalli*, una riga per
  ogni intervallo di un log (che cosa ha trovato), legate dal codice del sondaggio;
* **una tabella sola**: una riga per intervallo, con la posizione del sondaggio sulla stessa riga -
  almeno sulla prima riga di ogni sondaggio.

Le colonne possono stare in qualsiasi ordine, con nomi italiani o inglesi. Maiuscole, accenti, spazi e
punteggiatura non contano: `Profondità (m)`, `profondita_m` e `PROFONDITA` sono la stessa colonna. Le
colonne che il plugin non conosce vengono ignorate, così nella tabella puoi tenere colonne tue (data,
operatore, campioni). Quando due colonne potrebbero contenere la stessa cosa - `note` e `descrizione`,
`codice` e `sondaggio` - si legge quella più a sinistra e l'altra viene ignorata.

### La tabella dei collari { #collars }

Il collare è la testa del sondaggio: dove si trova e a che quota comincia.

| Nome: italiano<br>inglese | Obbligatoria | Unità | Esempio | Note |
|---|---|---|---|---|
| **sondaggio**<br>**hole_id** | sì | | `S1` | il codice del sondaggio, lo stesso nella tabella degli intervalli |
| **x**<br>**x**, **easting** | sì | m | `611,00` | nel sistema di riferimento del chunk |
| **y**<br>**y**, **northing** | sì | m | `799,00` | nel sistema di riferimento del chunk |
| **quota**<br>**elevation**, **z** | no | m | `307,130` | il terreno al sondaggio, dove comincia il log; vuota: letta dal DEM |
| **profondita**<br>**depth** | no | m | `12,00` | la profondità raggiunta; vuota: la base dell'ultimo intervallo |
| **tipo**<br>**kind** | no | | `core` | testo libero, per esempio `auger`, `core`, `trench`, `test_pit`; compare con i sondaggi, non entra nei calcoli |

### La tabella degli intervalli { #intervals }

| Nome: italiano<br>inglese | Obbligatoria | Unità | Esempio | Note |
|---|---|---|---|---|
| **sondaggio**<br>**hole_id** | sì | | `S1` | come nella tabella dei collari |
| **da**<br>**from** | sì, oppure `quota_tetto` | m | `0,34` | profondità del tetto dell'intervallo dalla bocca |
| **a**<br>**to** | sì, oppure `quota_letto` | m | `1,31` | profondità della sua base |
| **quota_tetto**<br>**top_z** | al posto di `da` | m | `306,790` | quota del tetto dell'intervallo |
| **quota_letto**<br>**base_z** | al posto di `a` | m | `305,820` | quota della sua base |
| **us**<br>**unit** | sì | | `US 2` | l'unità: un numero di US o un'unità litologica |
| **descrizione**<br>**description** | no | | `crollo e colluvio` | testo libero |
| **colore**<br>**colour** | no | | `7.5YR 6/4` | Munsell, `#rrggbb`, `r,g,b` o un nome di colore inglese |
| **litologia**<br>**lithology** | no | | `argilla` | la litologia principale; aiuta a scegliere il retino dei disegni |

Una tabella sola ha le colonne di tutte e due: una riga per intervallo, con `sondaggio`, `x` e `y` (e
`quota`, `profondita`, `tipo`) almeno sulla prima riga di ogni sondaggio; le righe che seguono, dello
stesso sondaggio, possono lasciarle vuote.

??? info "Tutti i nomi di colonna che il plugin riconosce"
    | Campo | Nomi |
    |---|---|
    | sondaggio | `sondaggio`, `sondaggi`, `id_sondaggio`, `nome_sondaggio`, `carota`, `carotaggio`, `trincea`, `saggio`, `nome`, `sigla`, `codice`, `punto`, `id`, `hole`, `hole_id`, `holeid`, `bhid`, `bh`, `borehole`, `borehole_id`, `boring`, `core_id`, `trench`, `name`, `point`, `station`, `location`, `location_id` |
    | x | `x`, `est`, `e`, `x_coord`, `coord_x`, `xcoord`, `x_utm`, `utm_e`, `utm_x`, `east`, `easting` |
    | y | `y`, `nord`, `n`, `y_coord`, `coord_y`, `ycoord`, `y_utm`, `utm_n`, `utm_y`, `north`, `northing` |
    | quota del collare | `quota`, `quota_bocca`, `quota_testa`, `quota_piano`, `quota_slm`, `quota_p_c`, `z`, `h`, `elevation`, `elev`, `collar_z`, `z_collar`, `collar_elevation`, `rl`, `height`, `altitude`, `alt`, `ground_level` |
    | profondità raggiunta | `profondita`, `profondita_totale`, `profondita_raggiunta`, `prof`, `prof_max`, `prof_totale`, `depth`, `total_depth`, `max_depth`, `final_depth`, `eoh`, `td` |
    | tipo | `tipo`, `tipologia`, `metodo`, `strumento`, `kind`, `type`, `method`, `tool` |
    | tetto dell'intervallo (profondità) | `da`, `dal`, `prof_da`, `profondita_da`, `da_prof`, `tetto`, `inizio`, `from`, `depth_from`, `from_depth`, `top`, `top_depth`, `start` |
    | base dell'intervallo (profondità) | `a`, `al`, `prof_a`, `profondita_a`, `a_prof`, `letto`, `fine`, `to`, `depth_to`, `to_depth`, `bottom`, `bottom_depth`, `base`, `end` |
    | tetto dell'intervallo (quota) | `quota_tetto`, `quota_da`, `quota_sup`, `quota_superiore`, `z_tetto`, `q_tetto`, `top_z`, `z_top`, `z_from`, `from_z`, `top_elevation` |
    | base dell'intervallo (quota) | `quota_letto`, `quota_base`, `quota_a`, `quota_inf`, `quota_inferiore`, `z_letto`, `q_letto`, `base_z`, `bottom_z`, `z_base`, `z_bottom`, `z_to`, `to_z`, `bottom_elevation` |
    | unità | `us`, `unita`, `unita_stratigrafica`, `codice_unita`, `strato`, `orizzonte`, `unit`, `unit_code`, `unit_id`, `us_code`, `layer`, `stratum`, `horizon`, `facies`, `formation`, `litho_unit` |
    | descrizione | `descrizione`, `desc`, `descr`, `note`, `osservazioni`, `description`, `notes`, `remarks` |
    | colore | `colore`, `colore_munsell`, `munsell`, `colour`, `color`, `munsell_colour`, `munsell_color` |
    | litologia | `litologia`, `lito`, `materiale`, `tessitura`, `lithology`, `lith`, `material`, `texture` |

    Un'unità di misura dopo il nome la rende esplicita: `_m`, `_mt`, `_metri`, `_metres`, `_meters` sono
    metri, `_cm` centimetri (`da_cm`, `Profondità (cm)`). I centimetri vengono convertiti solo per le
    profondità (`da`, `a`, `profondita`): le quote sono sempre in metri.

    Attenzione: `tetto` e `letto`, `top`, `bottom` e `base` sono profondità dalla bocca, non quote (le
    quote sono `quota_tetto`, `quota_letto`, `quota_base`); `codice` è il sondaggio, `h` la quota, `e` e
    `n` le coordinate. I nomi che non sono nell'elenco - come `Codice US`, `N. US`, `Sigla sondaggio`,
    `Quota s.l.m.` - vengono ignorati: rinomina la colonna.

### Profondità { #depths }

* Le profondità sono in metri dalla bocca del sondaggio - il terreno in cima al foro - e crescono verso
  il basso: un intervallo da `0,34` a `1,31` comincia 34 cm sotto la superficie ed è spesso 97 cm.
* Ogni log comincia da `0`: il primo intervallo parte dalla bocca. Il tetto della prima unità è allora il
  terreno stesso, il DEM.
* Gli intervalli si susseguono: il valore di `a` di un intervallo è il `da` del successivo. Differenze
  sotto i 5 mm contano come arrotondamenti.
* Le profondità in centimetri vogliono un nome di colonna che lo dica: `da_cm`, `a_cm`, `profondita_cm`.
* `profondita`, la profondità raggiunta, può restare vuota. Se è più profonda della base dell'ultimo
  intervallo, il plugin considera che il foro fosse ancora nell'ultima unità fin laggiù: le unità sotto
  sono più profonde.

### Quote { #elevations }

* `quota` è la quota del terreno al sondaggio, in metri, nello stesso sistema di quote del DEM. Se la
  lasci vuota il plugin la legge dal DEM in quel punto; nella scheda *Sondaggi* del pannello quelle quote
  hanno un `*`.
* Dove la dai, il plugin la confronta con il DEM e scrive il risultato alla fine dell'importazione:
  *Quota dei collari meno DEM: mediana -0,008 m, massimo +0,071 m (S17)*. Qualche centimetro è normale.
  Una mediana lontana da zero vuol dire che quote e DEM stanno in sistemi di quote diversi - per esempio
  quote ellissoidiche di un ricevitore GNSS e un DEM sul geoide - oppure che il rilievo è spostato in
  quota: riportali allo stesso sistema, oppure lascia la colonna vuota. Un solo valore grande indica un
  errore di battitura o un sondaggio nel posto sbagliato.
* I contatti di un sondaggio stanno alla sua quota meno la loro profondità.

### Intervalli per quote { #by-elevation }

I log rilevati con quote assolute (con un livello o una stazione totale) usano `quota_tetto` e
`quota_letto` al posto di `da` e `a`. Il plugin le trasforma in profondità dalla bocca, quindi il collare
deve avere una quota - nella tabella, oppure dal DEM; senza, l'intervallo resta fuori con un avviso. Dai
al collare la quota del tetto del suo primo intervallo. Una tabella può mescolare i due modi: una riga con
`da` e `a` usa quelli, una riga senza usa le quote.

`tetto` e `letto` da soli, come `top`, `bottom` e `base`, sono profondità, non quote.

### Unità { #units }

La colonna `us` dice a quale unità appartiene ogni intervallo. È il cuore dei dati: i sondaggi vengono
collegati fra loro solo attraverso questi codici.

* Va bene qualsiasi codice: un numero di US (`US 12`, `12`), un'unità litologica o stratigrafica (`A`,
  `B`, `Unità 3`).
* Scrivi ogni codice esattamente allo stesso modo in tutti i log: `US 3`, `US3` e `us 3` sono tre unità
  diverse.
* Un codice è un'unità, in un punto della sequenza. Due strati dello stesso tipo a profondità diverse -
  due livelli di cenere - vogliono due codici.
* Più intervalli di seguito possono appartenere alla stessa unità, con descrizioni diverse; un'unità non
  ricompare più in basso nello stesso log.
* Le unità vengono nello stesso ordine in tutti i log. Il plugin ricava l'ordine dai log - un'unità sopra
  un'altra in un sondaggio sta sopra di essa - e dove i log non sono d'accordo vince la maggioranza e il
  disaccordo viene segnalato. L'ordine si può anche scrivere a mano ([passo 2](#order)).
* Un'unità assente da un sondaggio semplicemente non si scrive: un log che passa da `US 2` direttamente a
  `US 4` dice che lì US 3 manca.
* Se i codici sono i numeri delle US disegnate nel chunk con lo strumento *Disegna US*, il modello 3D
  ritaglia ogni unità sui suoi poligoni: `US 12`, `12` e `US12` corrispondono a una US 12 disegnata,
  `USM 3` solo a una USM 3.

Il plugin costruisce una pila: ogni unità sta sopra la successiva dappertutto, con spessore zero dove
manca. Unità affiancate - un muro accanto al suo riempimento, una fossa che taglia gli strati - non
vengono rappresentate come tali: al massimo diventano unità che mancano in alcuni log.

### Descrizioni, colori e litologia { #colours }

* `descrizione` è testo libero. La descrizione più frequente di un'unità le dà il nome nelle legende di
  sezioni e pannelli; ogni intervallo tiene la sua nella colonna di un singolo sondaggio.
* `colore`: una notazione Munsell (`10YR 4/3`, `7.5YR 6/4`, `N 5/`), `#rrggbb`, tre numeri da 0 a 255
  (`160,82,45` oppure `160 82 45`) o un nome di colore inglese (`brown`, `grey`). Scrivi la tinta Munsell
  con il punto: `2.5Y 5/2`, non `2,5Y 5/2`, che non viene letto. Un'unità viene disegnata nel colore
  scritto più spesso per lei; un'unità senza un colore leggibile ne prende uno da una tavolozza di toni di
  terra. Le notazioni Munsell vengono convertite dalla libreria colour-science, che c'è nell'applicazione
  desktop e nelle installazioni `[gui]` e `[all]`; senza, le unità prendono i colori della tavolozza.
* `litologia` è facoltativa: insieme alla descrizione sceglie il retino dell'unità nelle colonne e nelle
  sezioni.

| Retino | Parole cercate nella litologia e nella descrizione |
|---|---|
| linee orizzontali (argilla) | `argill`, `clay` |
| linee e punti (limo) | `limo`, `limos`, `silt` |
| punti (sabbia) | `sabbi`, `sand` |
| cerchietti (ghiaia) | `ghiai`, `ciottol`, `gravel`, `pebble`, `cobble` |
| stelle (pietre, crollo, laterizi) | `crollo`, `pietr`, `macerie`, `laterizi`, `stone`, `rubble`, `brick` |
| croci (cenere, carboni) | `cenere`, `carbon`, `ash`, `charcoal` |
| linee verticali (torba) | `torba`, `peat` |
| linee oblique (organico) | `organico`, `humus`, `organic` |

Le parole vengono cercate anche dentro altre parole, riga per riga in quest'ordine, nella litologia e nella
descrizione insieme: decide la prima riga che trova qualcosa, e la litologia non conta più della
descrizione. Così `limo argilloso` prende il retino dell'argilla, `ghiaia sabbiosa` quello della sabbia,
`concrezioni carbonatiche` quello della cenere se nessuna riga sopra trova niente, l'inglese `mudbrick`
quello delle pietre. Se un'unità prende un retino inatteso, cerca queste parole nelle sue descrizioni.
Nelle sezioni un'unità prende il retino della sua litologia e della sua descrizione più frequenti; nelle
colonne ogni intervallo prende il suo.

### Vuoti e perdite di carota { #gaps }

* Non scrivere una riga per un tratto non recuperato: lascia il vuoto fra due intervalli.
* Un vuoto dentro una sola unità (la stessa unità sopra e sotto) non ha contatto: il plugin lo segnala -
  *vuoto fra 8,9 e 9,1 m dentro US 4 (nessun contatto)* - e la colonna lo mostra in bianco.
* Un vuoto fra due unità diverse mette il loro contatto a metà del vuoto: *vuoto fra 2 e 2,5 m (contatto a
  metà)*. Se sai dov'è il contatto, chiudi il vuoto.
* Non fare un'unità della carota persa (`perdita`, `lacuna`): diventerebbe un'unità della sequenza.

### Sondaggi che si fermano prima { #early }

Un sondaggio che si ferma prima del naturale - in depositi duri, su una pietra, alla falda - si registra
fino a dove è arrivato, con la profondità raggiunta. Non dice nulla delle unità sotto, se non che sono più
profonde del suo fondo: dove una superficie interpolata dagli altri sondaggi passerebbe sopra quel fondo,
il plugin la tiene sotto, e il rapporto conta i *fondi di sondaggi usati come vincolo*. Nei disegni il
fondo di ogni sondaggio è segnato da un trattino.

### File di esempio { #example-files }

| File | Formato | Che cosa mostra |
|---|---|---|
| [sondaggi_collari.csv](geoarchaeology/sondaggi_collari.csv) | collari; nomi italiani, punto e virgola, virgola decimale | 18 sondaggi, 5 senza quota (letta dal DEM) |
| [sondaggi_intervalli.csv](geoarchaeology/sondaggi_intervalli.csv) | intervalli; stesso formato | 117 intervalli con i colori Munsell, una perdita di carota in S1 fra 8,90 e 9,10 m, sondaggi che si fermano prima del naturale, unità che mancano in alcuni log |
| [boreholes_single_table.csv](geoarchaeology/boreholes_single_table.csv) | tabella sola; nomi inglesi, virgole, punto decimale | quattro degli stessi sondaggi con tipo e litologia, la posizione sulla prima riga di ogni sondaggio, S9 senza quota |
| [intervalli_per_quota.csv](geoarchaeology/intervalli_per_quota.csv) | tabella sola, intervalli per quote (`quota_tetto`, `quota_letto`) | S4 e S9 scritti con le quote assolute |

Sono i dati simulati della dimostrazione, nel suo sistema di riferimento locale: usali come modelli e
sostituisci codici, coordinate e log con i tuoi (le loro coordinate non cadono sul tuo rilievo). Le prime
righe delle due tabelle:

```text
sondaggio;x;y;quota;profondita
S1;611,00;799,00;307,130;12,00
S2;607,50;836,00;298,617;12,00
S3;612,50;866,00;;7,90
```

```text
sondaggio;da;a;us;descrizione;colore
S1;0,00;0,34;US 1;terreno vegetale (humus);10YR 3/2
S1;0,34;1,31;US 2;crollo e colluvio di mattoni;7.5YR 6/4
S1;1,31;2,43;US 3;argilla limosa grigia, carboni;10YR 5/2
```

della tabella sola in inglese (S9 non ha quota: viene letta dal DEM):

```text
hole_id,kind,easting,northing,elevation,depth,from,to,unit,description,colour,lithology
S9,auger,607.00,680.00,,3.00,0.00,0.35,US 1,topsoil (humus),10YR 3/2,organic
S9,,,,,,0.35,1.85,US 5,sterile yellowish-brown clay,10YR 6/6,clay
S9,,,,,,1.85,3.00,US 5,"silty clay with carbonate concretions, sterile",10YR 7/4,clay
```

e della tabella per quote:

```text
sondaggio;x;y;quota;quota_tetto;quota_letto;us;descrizione;colore
S9;607,00;680,00;285,553;285,553;285,203;US 1;terreno vegetale (humus);10YR 3/2
S9;607,00;680,00;285,553;285,203;283,703;US 5;argilla bruno-gialla sterile;10YR 6/6
```

### Preparare le tabelle con un foglio di calcolo { #spreadsheet }

Va bene qualsiasi foglio di calcolo: un foglio per tabella, i nomi delle colonne nella prima riga, poi una
riga per sondaggio (collari) o per intervallo, e nient'altro sul foglio: niente titoli, celle unite o
totali.

* **Formatta** le colonne dei codici (`sondaggio`, `us`) come *Testo* prima di scrivere, così codici come
  `1-2` o `01` non diventano date o numeri; formatta le colonne dei numeri come numeri semplici **senza il
  separatore delle migliaia**.
* **LibreOffice Calc**: *File > Salva con nome...*, tipo di file *Testo CSV (.csv)*, spunta *Modifica
  impostazioni filtro*, poi insieme di caratteri *Unicode (UTF-8)* e separatore di campo `;`. I decimali
  vengono scritti come li mostrano le celle (con la virgola, con le impostazioni italiane): il plugin li
  legge in tutti e due i modi.
* **Excel**: *File > Salva con nome*, tipo *CSV UTF-8* (o *CSV*). A seconda delle impostazioni del
  sistema, Excel separa i campi con virgole o con punti e virgola e mette fra virgolette le celle che
  contengono il separatore: il plugin li legge tutti e due.

Il plugin trova da solo il separatore (virgola, punto e virgola, tabulazione o barra verticale; gli spazi
se non c'è nessuno di questi), legge i decimali con il punto o con la virgola, legge file in UTF-8 o nella
codifica Windows di Excel, e salta le righe vuote e quelle che cominciano con `#`, comode per le note in
cima a una tabella. Celle vuote, `-`, `na` e `null` vogliono dire nessun valore. Un testo fra virgolette
può contenere il separatore (`"argilla limosa, carboni"`): i fogli di calcolo mettono le virgolette da
soli.

### Sul campo { #field }

* Decidi i codici delle unità il primo giorno, come un breve elenco con la descrizione di ciascuna, e usa
  gli stessi codici in tutti i log: correlare i sondaggi vuol dire dare lo stesso codice alla stessa unità.
* Una riga per intervallo: da, a, unità, descrizione, colore. Misura le profondità dal terreno in cima al
  sondaggio, sempre allo stesso modo.
* Comincia ogni log da 0, annota la profondità raggiunta, lascia la carota persa come un vuoto.
* Misura la posizione di ogni sondaggio con il ricevitore GNSS o la stazione totale, nel sistema di
  riferimento del rilievo fotogrammetrico, e scrivi il sistema sulla scheda. Prendi la quota nello stesso
  sistema di quote del DEM, oppure lasciala al DEM.
* Scrivi i colori Munsell con il punto nella tinta (`2.5Y 5/2`) e la litologia principale in una parola
  (`argilla`, `limo`, `sabbia`, `ghiaia`...).
* Dai ai sondaggi codici brevi e unici (`S1`, `S2`... oppure `T1`, `T2`...); un sondaggio rifatto accanto
  a uno fallito prende un codice nuovo.
* Una sezione di scavo diventa uno o più log: una colonna ogni uno o due metri lungo la parete, con il
  collare in cima alla parete, tipo `trench`.

### Errori frequenti e avvisi { #mistakes }

Alla fine dell'importazione il plugin elenca quello che gli è sembrato strano: i primi 15 avvisi nella
finestra, tutti dalla riga di comando. I numeri di riga contano l'intestazione come riga 1.

| Che cosa è successo | Che cosa fa o dice il plugin | Che cosa fare |
|---|---|---|
| nessuna colonna per il sondaggio, o un nome che non conosce (`Sigla sondaggio`) | *manca la colonna del sondaggio (id, sondaggio, hole...)*: non si importa nulla | chiamala `sondaggio` |
| una tabella senza x e y e senza da e a | *né coordinate (x, y) né intervalli (da, a / from, to)*: non si importa nulla | controlla i nomi delle colonne |
| nessun collare con le coordinate | *Nessun sondaggio con le coordinate nei file letti* | aggiungi la tabella dei collari, o x e y |
| la colonna dell'unità ha un nome che non conosce (`Codice US`, `N. US`) | ogni intervallo è *senza unità (riga ...): lasciato fuori* | chiamala `us` |
| la colonna della quota ha un nome che non conosce (`Quota s.l.m.`) | nessun avviso: le quote vengono lette dal DEM (il riepilogo conta le *quote prese dal DEM*) | chiamala `quota` |
| quote scritte in `tetto`/`letto` o `top`/`bottom` | lette come profondità: gli intervalli risultano *con la base sopra il tetto* e restano fuori | usa `quota_tetto` e `quota_letto` |
| virgola decimale in un file separato da virgole, senza virgolette (scritto a mano) | nessun messaggio: i numeri vengono spezzati fra le colonne | separa con il punto e virgola, o usa il punto decimale |
| separatore delle migliaia (`1.234.567`) | il numero non viene letto: il collare si perde e i suoi intervalli sono *senza la posizione del sondaggio* | niente separatore delle migliaia |
| la stessa unità scritta in due modi (`US 1` e `US1`) | due unità: l'ordine e le superfici vengono sbagliati | una sola grafia |
| lo stesso codice per due strati diversi, o un log con le unità in un altro ordine | *US 2 sopra US 3 in 2 sondaggi, sotto in 1* nell'ordine; *S7: US 2 sotto US 3, contro l'ordine delle unità* con le superfici | controlla i log, dai codici diversi a strati diversi |
| quote in un altro sistema di quote | *Quota dei collari meno DEM* con una mediana di metri | riportale al sistema del DEM, o lasciale vuote |
| coordinate in un altro sistema, o x e y scambiate | i sondaggi cadono fuori dal DEM: *Senza quota (né nel file né dal DEM)* se la tabella non ce l'ha, e nella vista non sono dove dovrebbero | converti le coordinate |
| un log che non comincia da 0 | nessun messaggio: il tetto della prima unità viene interpolato invece di essere il DEM | comincia da 0 |
| una riga senza `a` (o senza `da`) | lasciata fuori senza messaggio | completala |
| la base sopra il tetto | *intervallo 2-1,8 m con la base sopra il tetto (riga 8): lasciato fuori* | correggi le profondità |
| due intervalli sovrapposti | *intervalli sovrapposti a 0,9 m: il secondo parte dalla base del primo*, oppure *intervallo ... dentro il precedente: lasciato fuori* | correggi le profondità |
| un vuoto fra due intervalli | *vuoto fra 2 e 2,5 m (contatto a metà)*, oppure *vuoto ... dentro US 2 (nessun contatto)* | niente, se è una perdita di carota |
| un intervallo senza unità | *intervallo 2-2,1 m senza unità (riga 9): lasciato fuori* | scrivi l'unità |
| la profondità raggiunta minore dell'ultima base | *profondità 1,5 m minore della base dell'ultimo intervallo (2 m)*: si tiene la base | correggila |
| intervalli di un sondaggio che manca nei collari | *X9: intervalli senza la posizione del sondaggio: lasciati fuori* | aggiungi il sondaggio ai collari, con lo stesso codice |
| un sondaggio con due posizioni diverse | *B ha due posizioni (riga 4): tenuta la prima* | una posizione per sondaggio |
| intervalli per quote, il collare senza quota e fuori dal DEM | *intervallo in quote (riga 2) senza la quota del sondaggio: lasciato fuori* | dai la quota al collare |
| una tinta Munsell con la virgola (`2,5Y 5/2`), un nome di colore in italiano (`marrone`) | nessun messaggio: l'unità prende un colore della tavolozza | `2.5Y 5/2`, `#8b5a2b` o `139,90,43` |
| tre numeri per un colore (`139;90;43`) in una tabella separata da punti e virgola | i numeri finiscono nelle colonne seguenti | `139,90,43` o `139 90 43` |

## Passo per passo nel programma { #program }

### Il pannello

Una volta installato il plugin e riavviato il programma, la scheda *Geoarcheologia* compare nel dock
*Progetto* a sinistra; se le schede non ci stanno, usa le frecce in fondo alla barra delle schede, oppure
*Plugin > Geoarcheologia > Geoarcheologia...*. Il menu *Plugin > Geoarcheologia* ha anche i passi
principali: *Importa sondaggi...*, *Superfici di contatto (kriging)...*, *Sezione geoarcheologica...*,
*Colonne stratigrafiche...*, *Modello 3D delle unità...*, *Nascondi la sovrapposizione*.

<figure markdown="span">
  ![Il pannello Geoarcheologia con la scheda Superfici aperta](../img/plugins/geoarchaeology/pane.png)
  <figcaption>Il pannello dopo le superfici della dimostrazione: la scheda <em>Superfici</em> con il metodo e i
  valori di ogni prodotto; in fondo, i valori della sovrapposizione mostrata (lo spessore di US 4).
  Sondaggi simulati.</figcaption>
</figure>

Dall'alto: una breve descrizione; lo stato del chunk (sondaggi, unità dall'alto, quando sono state
costruite le superfici e su quale griglia, oppure *I sondaggi o l'ordine sono cambiati: ricostruiscile.*);
i pulsanti dei passi; tre schede, *Sondaggi*, *Superfici* e *Sezioni*; i comandi della sovrapposizione
(*Banda*, *Palette*, *Opacità*); *Sovrapponi alla vista*, *Nascondi*, *Apri il disegno*, *Esporta...*,
*Apri in QGIS* e *Rimuovi*; in fondo, i valori ai due estremi dei colori mostrati. I passi più lunghi
lavorano in background con la barra di avanzamento, e il progetto viene salvato quando finiscono.

### 1. Importare i sondaggi { #import }

*Importa sondaggi...*

| Campo | Che cosa fa |
|---|---|
| *Collari, o tabella unica* | la tabella dei collari, o la tabella sola |
| *Intervalli (se in un file a parte)* | la tabella degli intervalli; vuoto con una tabella sola |
| *Tipo* | *dalla tabella*, oppure un solo tipo per tutti i sondaggi di questi file: trivella manuale, carotaggio, sezione di scavo, saggio, altro |
| *Sostituisci i sondaggi del chunk (altrimenti si aggiungono)* | spuntato: i sondaggi importati prima vengono dimenticati; non spuntato: quelli con lo stesso codice vengono sostituiti e gli altri tenuti |

Alla fine una finestra riassume l'importazione; con i file di esempio (righe spezzate qui):

```text
18 sondaggi letti (18 nel chunk), 117 intervalli; unità dall'alto:
  US 1, US 2, US 3, US 4, US 5
5 quote prese dal DEM
Quota dei collari meno DEM: mediana -0,008 m, massimo +0,071 m (S17)
Avvisi (1):
- S1: vuoto fra 8,9 e 9,1 m dentro US 4 (nessun contatto)
```

La scheda *Sondaggi* elenca codice, quota (`*`: letta dal DEM), profondità e unità di ogni sondaggio.
*Sondaggi nella vista* li disegna sull'ortofoto o sul DEM come un layer di punti rossi,
*Geoarcheologia: sondaggi*: con lo strumento *Sposta*, un clic su un punto ne mostra codice, quota,
profondità e unità. Le tabelle lette vengono copiate nel progetto. Tabelle nuove rendono superate le
superfici: ricostruiscile.

### 2. L'ordine delle unità { #order }

*Ordine delle unità...* mostra le unità dall'alto in basso, come risultano dai log, e i disaccordi fra i
sondaggi, se ce ne sono (*US 2 sopra US 3 in 2 sondaggi, sotto in 1*). Per cambiare l'ordine scrivi i
codici dall'alto in basso separati da virgole; i codici non scritti vanno dopo, nell'ordine dei log. Un
ordine dato a mano resta finché i sondaggi non portano unità nuove. Dopo, ricostruisci le superfici e il
modello.

### 3. Superfici di contatto { #surfaces }

*Superfici...* interpola fra i sondaggi il tetto di ogni unità: la superficie dove comincia.

| Campo | Predefinito | Che cosa fa |
|---|---|---|
| *Modello del variogramma* | *scelto dai dati* | sferico, esponenziale o gaussiano: come crescono con la distanza le differenze di un contatto. Scelto dai dati: il modello più stabile fra quelli con l'errore leave-one-out entro il 5 % del migliore |
| *Andamento* | *scelto dai dati* | *nessuno* (kriging ordinario) o *piano inclinato* (kriging universale, per un contatto che pende attraverso il sito); il piano si prova con sei sondaggi o più, non tutti allineati |
| *Interpola* | per contatto | le quote di tutti i contatti, le profondità di tutti i contatti dal terreno, oppure ogni contatto a modo suo (sotto) |
| *Per unità* | | il modo di alcune unità, per esempio `US 2=profondità, US 5=quota`; vanno bene anche `depth` ed `elevation` |
| *Cella* | quella del DEM | il lato delle celle delle superfici, in metri; su un'area grande la cella del DEM viene raddoppiata, triplicata... per restare entro un milione di celle; senza DEM, un valore tondo che dà circa 300 celle attraverso l'area |
| *Pepita* | stimata | la varianza di un contatto fra due sondaggi vicinissimi, in m²: errori di misura e variazioni a breve distanza |
| *Margine attorno ai sondaggi* | la loro distanza tipica | fin dove le superfici vanno oltre i sondaggi esterni; di norma la distanza mediana fra un sondaggio e il più vicino |
| *Sondaggi minimi per il kriging* | 5 | con meno sondaggi che attraversano un contatto, l'inverso della distanza (IDW) al posto del kriging |

**Per quota o per profondità.** Un contatto con una forma sua - il naturale sotto un tell, un piano
sepolto, un canale - viene interpolato dalle sue quote. Un contatto che segue il terreno - la base
dell'humus, strati drappeggiati su un rilievo - viene interpolato dalle sue profondità e appeso al DEM,
così fra i sondaggi continua a seguire il terreno. *Per contatto* sceglie da solo: per profondità quando
le profondità del contatto variano da un sondaggio all'altro meno della metà delle sue quote (tutte e due
misurate attorno a un piano), per quota altrimenti, e per quota quando lo attraversano meno di tre
sondaggi. Se ogni log comincia da 0, il tetto della prima unità è il DEM stesso.

**Poi:**

* un sondaggio che si ferma sopra un contatto tiene il contatto sotto il proprio fondo;
* dove due contatti si incrociano cede quello con la varianza di kriging maggiore, il meno sicuro, e
  nessun contatto sale sopra il terreno;
* un'unità che manca in alcuni log manca anche intorno a loro: i sondaggi votano la sua presenza, i più
  vicini contano di più, e dove vincono quelli senza l'unità e i suoi due contatti non mostrano uno
  spessore vero (più del doppio della loro incertezza), il tetto e la base dell'unità diventano una sola
  superficie; fra i sondaggi non compaiono per caso unità sottili;
* le superfici coprono l'area dei sondaggi più il margine, e fuori non hanno valori.

Alla fine una finestra dice, contatto per contatto, come è stato costruito. Nella dimostrazione (righe
spezzate qui):

```text
945 x 945 celle di 0,38 m
Tetto di US 1: il terreno
Tetto di US 2: kriging gaussiano, errore leave-one-out 0,026 m, per
  profondità (segue il terreno: profondità variabili per 0,03 m, quote per
  6,22 m); corretta dalla pila su 9.736 m² (abbassata fino a 0,01 m, alzata
  fino a 0,02 m)
Tetto di US 3: kriging sferico, errore leave-one-out 0,943 m (il gaussiano
  dava 0,908 m: meno del 5 % di differenza), per profondità (...)
Tetto di US 5: kriging sferico, errore leave-one-out 0,274 m (...), per
  quota (ha una forma sua: profondità variabili per 2,84 m, quote per
  0,22 m); corretta dalla pila su 39.412 m² (abbassata fino a 3,44 m,
  alzata fino a 1,60 m)
Spessore di US 2: media 1,08 m, massimo 5,20 m, 78.462 m³ (+ 2.787 m³ dove
  manca il DEM); assente su 4.956 m² (sondaggi senza: S9, S10)
2.580 m² senza DEM: lì il terreno è la superficie dei collari, i volumi
  sono a parte
```

[Leggere i risultati](#reading) spiega questi numeri. La scheda *Superfici* elenca *Tetto di ...* per ogni
unità e *Spessore di ...* per ogni unità tranne l'ultima, con il metodo (il modello, `+ piano`, per quota o
per profondità) e i valori minimo e massimo; il suggerimento di una riga ne ripete il rapporto.

### 4. Le superfici sulla vista { #overlay }

Scegli una riga della scheda *Superfici*, poi:

* *Banda*, solo per i tetti: *Quota*, *Varianza di kriging* (l'incertezza, m²) o *Correzione della pila*
  (quanto è stato spostato il contatto, m);
* *Palette*: quella del prodotto (ipsometrica per le quote, calore per varianze e correzioni, una rampa di
  marroni per gli spessori) o un'altra (grigi, terra, divergente...);
* *Opacità*.

*Sovrapponi alla vista*, o un doppio clic sulla riga, la mette sopra l'ortofoto o il DEM; la riga in fondo
al pannello dice che cosa si vede e i valori ai due estremi dei colori (il 2° e il 98° percentile).
Spessori, varianze e correzioni partono da zero: così le correzioni mostrano dove un contatto è stato
abbassato, mentre le celle alzate (valori negativi) stanno nella banda 3 del GeoTIFF. *Nascondi* toglie la
sovrapposizione; se ne vede una alla volta.

### 5. Sezioni { #sections }

* *Traccia una sezione*: clicca l'inizio e poi la fine della traccia sull'ortofoto o sul DEM (lo strumento
  linea del programma, prestato al plugin per una traccia).
* *Sezione per i sondaggi*: seleziona due o più sondaggi nella scheda *Sondaggi* (con Ctrl, o Cmd sul Mac);
  la traccia li unisce nell'ordine della tabella.

| Campo | Che cosa fa |
|---|---|
| *Nome* | il titolo del disegno e il nome dei suoi file; una sezione con lo stesso nome viene sostituita |
| *Sondaggi entro* | a quale distanza dalla traccia un sondaggio viene riportato, in metri; vuoto: metà della distanza tipica fra i sondaggi |
| *Scala 1:* | vuota: come per le sezioni del programma, la scala normalizzata più dettagliata (1:20, 1:50, 1:100...) che sta in un foglio A0 circa |
| *Esagerazione verticale* | vuota: scelta perché il disegno non sia una striscia sottile (1, 2, 5, 10, 20, 50 o 100) |

Il disegno ha il terreno (nero), il tetto di ogni unità (grigio), le unità campite con il loro colore e
retino - l'ultima fino al fondo del disegno -, i sondaggi vicini alla traccia come colonne con il codice e
la distanza dalla traccia (positiva a sinistra guardando lungo la traccia), la legenda delle unità. I file
sono un SVG e un PNG; un DXF in coordinate di sezione (distanza lungo la traccia e quota vera, in metri,
senza esagerazione; layer `GEO_TERRENO`, `GEO_CONTATTI`, `GEO_UNITA_<unità>`, `GEO_SONDAGGI`,
`GEO_TESTI`, `GEO_GRIGLIA`); un CSV del profilo (distanza, x, y, terreno, tetto di ogni unità). *Apri il
disegno*, o un doppio clic nella scheda *Sezioni*, apre il PNG; le tracce vengono disegnate sulla vista come
layer *Geoarcheologia: sezioni*. Una sezione è un disegno fatto in quel momento: dopo aver ricostruito le
superfici, rifallo.

<figure markdown="span">
  ![Una sezione nord-sud del tell con le unità e i sondaggi riportati](../img/plugins/geoarchaeology/section-ns.png)
  <figcaption>Una sezione del plugin attraverso nove sondaggi, 1:1000 con esagerazione verticale x4:
  terreno dal DEM, contatti per kriging, i sondaggi entro 15 m con la distanza dalla traccia. Sondaggi
  simulati sul DEM reale di Tell Sultan.</figcaption>
</figure>

### 6. Colonne stratigrafiche { #columns }

*Colonne stratigrafiche*: spunta i sondaggi; quelli selezionati nella tabella vengono per primi.

* Un sondaggio: la sua colonna con le profondità, le quote quando il collare ne ha una, la descrizione e
  il colore di ogni intervallo.
* Più sondaggi: un pannello di colonne affiancate, con i tetti della stessa unità uniti da tratteggi.

*Asse verticale*: le quote se tutti i sondaggi le hanno (predefinito), le quote, oppure le profondità.
*Scala 1:*: vuota per stare in un foglio A4 (da 1:10 a 1:1000). Il plugin scrive un SVG e un PNG e apre il
PNG. Un pannello nuovo sostituisce il precedente (`colonne.svg`, `colonne.png`); dalla riga di comando si
può scrivere in un file a scelta.

<figure markdown="span">
  ![Un pannello di nove colonne stratigrafiche su un asse comune delle quote](../img/plugins/geoarchaeology/columns.png)
  <figcaption>Il transetto nord-sud come pannello di colonne: colori Munsell dei log, retini dalle
  descrizioni, tratteggi che uniscono i tetti della stessa unità, la perdita di carota di S1 lasciata in
  bianco. Sondaggi simulati.</figcaption>
</figure>

### 7. Modello 3D delle unità { #model }

*Modello 3D...*, disponibile quando ci sono le superfici.

| Campo | Che cosa fa |
|---|---|
| *Cella* | vuota: automatica, la griglia delle superfici resa più grossolana fino a 60.000 celle |
| *Ritaglia le unità sulle US disegnate con lo stesso numero* | un'unità il cui codice corrisponde a US disegnate nel chunk viene ritagliata sui loro poligoni |
| *Anche l'ultima unità, fino al fondo* | l'ultima unità non ha niente sotto: il suo solido scende fino a un fondo |
| *Quota del fondo* | vuota: il fondo più basso dei sondaggi, tenuto sotto il tetto più basso dell'ultima unità di un margine pari al 5 % dell'altezza coperta dai sondaggi (almeno 10 cm), perché il solido non abbia buchi |

Una finestra elenca i volumi. I file sono un PLY per unità (coordinate vere in doppia precisione, il
colore dell'unità), un OBJ con un oggetto e un materiale per unità (`unita.obj`, `unita.mtl`) e
`volumi.csv`; aprili in MeshLab, Blender, CloudCompare o QGIS 3D. Il volume dell'ultima unità dipende dal
fondo scelto: non è il volume di un deposito.

<figure markdown="span">
  ![I solidi di cinque unità e un taglio del modello](../img/plugins/geoarchaeology/model-3d.jpg)
  <figcaption>Il modello 3D: un solido chiuso per unità con il suo volume, e un taglio lungo la sezione
  est-ovest. Sondaggi simulati sul DEM reale di Tell Sultan.</figcaption>
</figure>

### 8. Esportare, QGIS, togliere { #export }

* *Esporta...*: nella scheda *Superfici*, con una riga selezionata, quella superficie o quello spessore in
  GeoTIFF; altrimenti tutto in un GeoPackage - sondaggi (punti), intervalli (tabella), tracce delle
  sezioni (linee) - con accanto un GeoTIFF per ogni superficie e spessore e il CSV dei volumi. Lo stesso
  GeoPackage si ottiene da *File > Esporta > Sondaggi e superfici geoarcheologiche (GeoPackage)...*.
* *Apri in QGIS*: il progetto QGIS del programma ([QGIS](../integrations.md)) con le superfici - nascoste
  all'inizio, le quote in grigi e gli spessori in una rampa calda - e i layer dei sondaggi e delle
  sezioni; solo per un chunk georeferenziato.
* *Rimuovi*: nella scheda *Superfici* tutte le superfici e gli spessori (formano una pila sola); nella
  scheda *Sezioni* la sezione selezionata e i suoi disegni; nella scheda *Sondaggi* i sondaggi selezionati.

## Dalla riga di comando { #cli }

Gli stessi passi con `ppg plugin geoarchaeology COSA PROGETTO ...`. Ogni comando lavora sul chunk attivo
(`--chunk ETICHETTA` per un altro) e salva il progetto.

```bash
ppg plugin geoarchaeology help
ppg plugin geoarchaeology import scavo.ppg sondaggi_collari.csv sondaggi_intervalli.csv
ppg plugin geoarchaeology import scavo.ppg campagna2.csv
ppg plugin geoarchaeology import scavo.ppg tutti.csv --replace --kind core
ppg plugin geoarchaeology list scavo.ppg
ppg plugin geoarchaeology order scavo.ppg "US 1,US 2,US 3,US 4,US 5"
ppg plugin geoarchaeology surfaces scavo.ppg
ppg plugin geoarchaeology surfaces scavo.ppg --mode "auto,US 2=profondità,US 5=quota" --min-points 6
ppg plugin geoarchaeology section scavo.ppg --through S9,S8,S7,S6,S1,S2,S3,S4,S5 --name "Sezione N-S"
ppg plugin geoarchaeology section scavo.ppg --coords "455,800;760,800" --distance 15 --scale 1000 --exaggeration 4
ppg plugin geoarchaeology logs scavo.ppg S1
ppg plugin geoarchaeology logs scavo.ppg S5 S4 S3 S2 S1 --out colonne_ns.pdf
ppg plugin geoarchaeology model scavo.ppg --offset 600,800,0
ppg plugin geoarchaeology export scavo.ppg geoarcheologia.gpkg
ppg plugin geoarchaeology export scavo.ppg naturale.tif --id top_4_US_5
ppg plugin geoarchaeology remove scavo.ppg surfaces
```

| Comando | Opzioni |
|---|---|
| `help` | tutti i comandi con le loro opzioni |
| `import PROGETTO FILE...` | una o più tabelle (collari, intervalli, tabelle sole, in qualsiasi ordine); i sondaggi si aggiungono a quelli del chunk, sostituendo quelli con lo stesso codice; `--replace` dimentica quelli che c'erano; `--kind` auger, core, trench, test_pit o other per tutti |
| `list PROGETTO` | sondaggi, unità, superfici, sezioni, modello; `--json`: tutto il registro, con ogni modello di variogramma provato e il suo errore |
| `order PROGETTO UNITÀ` | i codici dall'alto in basso, separati da virgole |
| `surfaces PROGETTO` | `--model` auto, spherical, exponential, gaussian; `--trend` auto, none, linear; `--mode` auto, elevation o depth (anche quota, profondità), e le unità nominate (`"auto,US 2=profondità"`); `--cell` m; `--nugget` m²; `--buffer` m, il margine; `--min-points` (5); `--order` le unità dall'alto in basso |
| `section PROGETTO` | `--coords "x1,y1;x2,y2;..."`, una traccia di due o più punti nel sistema di riferimento del chunk (`--coords=...` se il primo numero è negativo), oppure `--through S1,S4,S7`, in quell'ordine; `--name`; `--distance` m; `--scale` (la N di 1:N); `--exaggeration` |
| `logs PROGETTO [CODICI...]` | i sondaggi (tutti se non se ne dà nessuno); `--out` un file `.svg`, `.png` o `.pdf`; `--vertical` auto, elevation, depth; `--scale` |
| `model PROGETTO` | `--cell` m; `--no-clip`; `--floor` quota; `--no-last` lascia fuori l'ultima unità; `--offset X,Y,Z` sottratto alle coordinate dell'OBJ (scritto nella sua intestazione e in `unita.obj.offset.txt`) |
| `export PROGETTO OUT` | un GeoPackage con accanto i GeoTIFF e i volumi; con `--id` (un id mostrato da `list`, come `top_4_US_5` o `thickness_US_2`) una superficie in GeoTIFF |
| `remove PROGETTO [COSA]` | `all` (predefinito) o `boreholes`: tutto quello del plugin nel chunk; `surfaces`, `sections`, `model`; il codice di un sondaggio o il nome di una sezione |

Le coordinate grandi (UTM) perdono precisione in alcuni programmi 3D: `--offset` con numeri tondi vicini
al sito tiene piccole quelle dell'OBJ; i PLY conservano le coordinate vere.

## Leggere i risultati { #reading }

### Le superfici

Ogni *Tetto di US n* è la superficie dove comincia l'unità n - il suo contatto con l'unità sopra - come
quota in metri sull'area dei sondaggi. Il tetto della prima unità è il terreno; l'ultima unità ha un tetto
ma non una base.

<figure markdown="span">
  ![Il tetto del naturale, la sua incertezza e le correzioni della pila](../img/plugins/geoarchaeology/natural-top.jpg)
  <figcaption>Il tetto del naturale (US 5): (a) la banda 1, la quota; (b) la radice della banda 2,
  l'incertezza di kriging, minima ai sondaggi; (c) la banda 3, la correzione fatta dalla pila. Sondaggi
  simulati sul DEM reale di Tell Sultan.</figcaption>
</figure>

### Varianza di kriging (banda 2)

La varianza di kriging dice quanto è incerta la superficie in ogni punto, in m²; la sua radice è una
deviazione standard in metri (0,09 m² sono ±0,30 m). È minima ai sondaggi e cresce fra di essi e oltre.
Dipende da dove sono i sondaggi e dal variogramma, non dai valori trovati: dice dove servirebbero altri
sondaggi, non se i log sono giusti. I contatti interpolati con l'inverso della distanza (pochi sondaggi) e
il tetto della prima unità (il DEM) non ce l'hanno.

### Errore leave-one-out

Per ogni contatto fatto per kriging, ogni sondaggio viene tolto a turno e il contatto nel suo punto viene
stimato dagli altri; il rapporto dà la radice della media dei quadrati di queste differenze (`list --json`
anche la media e la massima). È l'errore da aspettarsi sulla superficie fra sondaggi vicini. Nella
dimostrazione: 0,03 m per la base dell'humus, che segue il terreno; 0,27 m per il tetto del naturale, una
superficie dolce; 0,9 e 1,3 m per i tetti dei depositi del Ferro e del Bronzo dentro il tell, con sondaggi
a 30-60 m l'uno dall'altro.

Il rapporto nomina il modello usato e, quando un altro aveva un errore appena minore, lo dice: *(il
gaussiano dava 0,908 m: meno del 5 % di differenza)*. I modelli entro il 5 % del migliore valgono quanto
lui, e si usa il più stabile (sferico, poi esponenziale, senza piano inclinato prima che con, gaussiano per
ultimo): con pochi sondaggi il modello gaussiano, che spesso vince gare così strette, esagera fra i
sondaggi. La cartella *surfaces* ha anche una figura per ogni contatto fatto per kriging
(`variogram_<id>.png`): la semivarianza delle coppie di sondaggi rispetto alla loro distanza, e il modello
adattato. Con una dozzina di sondaggi ha pochi punti e il modello è un'indicazione di massima: giudica la
superficie dal suo errore leave-one-out.

### Correzioni della pila (banda 3)

Interpolati uno per uno, due contatti possono incrociarsi o salire sopra il terreno. La pila li rimette in
ordine: dove due contatti si incrociano si incontrano dove sta quello meglio conosciuto, e nessuno sale
sopra il DEM. La banda 3 tiene la correzione fatta in ogni cella (positiva: abbassata, negativa: alzata), il
rapporto l'area e gli spostamenti più grandi (*corretta dalla pila su 39.412 m² (abbassata fino a 3,44 m,
alzata fino a 1,60 m)*). Correzioni di qualche centimetro sono normali. Correzioni di metri su aree grandi
vogliono dire che i sondaggi sono troppo distanti per la velocità con cui cambiano le unità, oppure che
codici o ordine non sono coerenti: guarda la banda 3 e traccia una sezione in quella zona.

### Unità assenti

*assente su 4.956 m² (sondaggi senza: S9, S10)*: l'area dove l'unità manca - tetto e base sono una sola
superficie - e i sondaggi che non ce l'hanno. Lì il suo spessore è zero.

### Spessori e volumi

Ogni *Spessore di US n* è il tetto dell'unità n meno il tetto dell'unità sotto, zero dove n manca. Il suo
volume è sommato sulle celle con il DEM; dove il DEM ha buchi il terreno è la superficie interpolata fra i
collari, e il volume lì viene dato a parte (*+ 2.787 m³ dove manca il DEM*). L'ultima unità non ha
spessore. Il modello 3D dà gli stessi volumi sulla sua griglia più grossolana (entro lo 0,1 % nella
dimostrazione).

<figure markdown="span">
  ![Spessore del deposito sopra il naturale, con le isolinee](../img/plugins/geoarchaeology/deposit-thickness.jpg)
  <figcaption>Lo spessore del deposito sopra il naturale (DEM meno il tetto di US 5), con i volumi delle
  unità calcolati dal plugin. Sondaggi simulati sul DEM reale di Tell Sultan.</figcaption>
</figure>

### Quanto fidarsi

* Tutto dipende da quanto sono distanti i sondaggi rispetto alla velocità con cui cambiano le unità. Un
  contatto che segue il terreno, o che cambia lentamente, è ben conosciuto anche con sondaggi radi; i tetti
  di depositi che vanno e vengono dentro un tell no.
* Prendi l'errore leave-one-out come l'errore da aspettarsi fra i sondaggi, la varianza di kriging per
  vedere dove cresce, la banda 3 per vedere dove i log non sono d'accordo.
* Nella dimostrazione la stratigrafia vera è nota, perché è inventata: il volume di tutto il deposito è
  venuto entro l'1 % di quello vero, i volumi delle singole unità entro il 15 % circa (in parte
  compensandosi), e il 95 % del tetto del naturale entro 0,28 m.
* Il volume di un deposito intero è più affidabile di quelli delle sue unità, e tutti e due più della quota
  di un contatto in un singolo punto.

### Limiti

* **Bordi netti.** Dove un'unità viene tolta perché i sondaggi vicini non ce l'hanno, finisce con un bordo
  verticale a metà strada fra un sondaggio che la ha e uno che non la ha: un'unità che si assottiglia piano
  piano non viene ricostruita.
* **Un sondaggio isolato senza un'unità** la toglie tutto intorno, fino al bordo delle superfici, anche
  dove forse c'è ancora: nessun altro sondaggio dice il contrario.
* **Sondaggi radi.** Con sondaggi a 30-60 m l'uno dall'altro, i tetti delle unità dentro il tell della
  dimostrazione erano sbagliati di 1-2 m (il 95 % degli errori).
* **Una pila di strati.** Unità affiancate, fosse e muri non vengono rappresentati come tali.
* **Niente estrapolazione.** Le superfici si fermano al margine attorno ai sondaggi.
* **Pochi sondaggi.** Sotto i cinque sondaggi (o il numero scelto) un contatto viene interpolato con
  l'inverso della distanza, senza incertezza.

## File scritti { #files }

Tutto va nella cartella del chunk, accanto al file del progetto:
`<progetto>.ppg.files/chunk_<n>/geoarchaeology/`. I dati del plugin restano nel progetto stesso e si
aprono con lui.

* `sources/`: le tabelle CSV importate, così come erano.
* `sondaggi.gpkg`: un GeoPackage tenuto aggiornato, con tre tabelle: `sondaggi` (punti: codice, x, y,
  quota e da dove viene, quota del DEM, profondità, quota del fondo, tipo, unità, numero di intervalli,
  tabella di origine), `intervalli` (senza geometria: sondaggio, da, a, quote del tetto e della base,
  unità, descrizione, colore, litologia) e `sezioni` (linee: nome, lunghezza, distanza, scala,
  sondaggi, disegno).
* `surfaces/top_<k>_<unità>.tif`: il tetto di ogni unità, un GeoTIFF float32 con tre bande: 1 la quota
  (m), 2 la varianza di kriging (m²), 3 la correzione della pila (m, positiva dove è stato abbassato,
  negativa dove è stato alzato); nessun valore fuori dall'area dei sondaggi.
* `surfaces/thickness_<unità>.tif`: lo spessore di ogni unità tranne l'ultima (m).
* `surfaces/variogram_<id>.png`: il variogramma di ogni contatto fatto per kriging.
* `sections/<nome>.svg`, `.png`, `.dxf`, `.csv`: ogni sezione - disegno, DXF in coordinate di sezione,
  profilo.
* `logs/<sondaggio>.svg`, `.png`: la colonna di un sondaggio; `logs/colonne.svg`, `.png`: l'ultimo
  pannello.
* `model/<unità>.ply`, `model/unita.obj` con `unita.mtl`, `model/volumi.csv`: i solidi e i loro volumi;
  `volumi.csv` ha unità, descrizione, volume, area, spessore medio e massimo, tetto e base minimi e
  massimi, celle, ritagliata, fino al fondo, volume e area dove manca il DEM.

Il GeoPackage esportato con *Esporta...* (o `export`) ha le stesse tre tabelle, con accanto
`<nome>_<id>.tif` per ogni superficie e spessore e `<nome>_volumi.csv`.
