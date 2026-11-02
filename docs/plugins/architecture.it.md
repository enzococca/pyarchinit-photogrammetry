# Elementi architettonici

Il plugin *Elementi architettonici* ricava elementi parametrici dal modello fotogrammetrico: una colonna per
rivoluzione, una modanatura o una cornice portata lungo un percorso, un arco o una volta a botte da una sezione, il vano
di una porta o di una finestra attraverso un muro. Ognuno è un solido con i suoi parametri (raggio, altezza, luce,
freccia, spessore, larghezza, soglia, architrave) scritto come STEP e come solido IFC
parametrico accanto alla mesh, per un modello HBIM; ognuno tiene il profilo misurato sul modello e dice sempre
**quanto il modello si discosta dal solido**, perché nessuno scambi l'ideale per il reale.

Questa pagina descrive la versione 0.2.0 del plugin, che richiede pyarchinit-photogrammetry 1.3.4 o successivo. Il
README e il codice sono nel [repository dei
plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/architecture).

<figure markdown="span">
  ![Il muro degli esempi: il modello in grigio, i solidi a colori](../img/plugins/architecture/wall-3d.jpg)
  <figcaption>Il modello di un muro sintetico fatto dal programma con 76 foto (grigio) e i quattro solidi adattati su
  di esso: la ghiera a sesto acuto (arancio), la cornice in cima (blu), il vano della porta (verde) e quello della
  finestra strombata (rosa).</figcaption>
</figure>

!!! note "Gli esempi di questa pagina"
    Figure e numeri vengono da due **scene sintetiche** fatte per questa guida: una colonna con entasi e un muro con un
    arco a sesto acuto, una cornice, una porta e una finestra strombata, con texture, fotografati dal renderer del
    programma, poi allineati, densificati e trasformati in mesh dal programma come si farebbe con foto vere. La verità è
    nota, così la guida può dire quanto il plugin le si è avvicinato. Lo script è `proof/make_proof.py` nella cartella
    del plugin.

## In breve

1. Installa CadQuery una volta dal pannello *Elementi architettonici* (a destra).
2. **Colonna...**: disegna in pianta la zona della colonna, dai le due quote e il numero di nodi.
3. **Modanatura...**: traccia il percorso sulla pianta, su un prospetto o una sezione, o prendi due marker; dai la
   finestra intorno al percorso in cui si taglia la sezione.
4. **Arco / volta...**: scegli una sezione del progetto e clicca dentro l'apertura, o traccia in pianta un piano
   verticale.
5. **Porta / finestra...**: clicca dentro l'apertura sulla pianta; dai lo spessore del muro se ne è stata fotografata
   una sola faccia, o scegli l'arco che la chiude.
6. Leggi lo **scarto** nella tabella, mostra il solido nella vista 3D, modifica i parametri, esporta STEP, IFC o DXF.

Zone e percorsi in pianta si disegnano sempre sull'ortofoto (o sul DEM) del chunk: il plugin la rimostra prima di
chiedere, anche quando la vista mostrava un prospetto.

<figure markdown="span">
  ![Il pannello del plugin](../img/plugins/architecture/pane.png)
  <figcaption>Il pannello con gli elementi degli esempi: le misure e lo scarto di ciascuno.</figcaption>
</figure>

## Cosa serve

Un chunk con un **modello** in metri: un sistema di coordinate proiettato, o un sistema locale con una scala
(riferimenti, una barra di scala, un modello importato in metri). Un sistema locale senza scala è rifiutato: i numeri
non sarebbero metri. Gli elementi si misurano sul modello, mai sulla nuvola densa; un'ortofoto o un DEM servono solo per
disegnare in pianta, e un prospetto o una sezione del progetto (*Strumenti > Prospetti e sezioni*) per disegnare in
alzato.

## CadQuery e il suo ambiente

I solidi li fanno [CadQuery](https://cadquery.readthedocs.io) e OpenCascade, in un processo a parte: il programma non li
carica mai. *Installa CadQuery* nel pannello (o `ppg plugin architecture install`) crea con `uv` un ambiente nella
cartella del plugin: Python 3.12, CadQuery, OpenCascade (OCP) e il VTK a cui OpenCascade è collegato, senza le parti che
CadQuery usa solo per disegnare e per il suo risolutore di assiemi (casadi, numba, scipy, matplotlib, trame). Occupa
**circa 900 MB** e richiede alcuni minuti; **la prima elaborazione dopo l'installazione richiede circa un minuto**,
mentre Python prepara i moduli di CadQuery. Un Python che ha già CadQuery (`pip install cadquery`) si usa così com'è.

OpenCascade ha versioni per macOS (Apple Silicon e Intel), Linux (x86-64 e aarch64) e Windows x64. Non ce n'è per
**Windows ARM**: lì il pannello dice che gli elementi non sono disponibili.

## La colonna

*Colonna...* chiede una zona disegnata in pianta; il dialogo propone la quota più bassa e la più alta del modello al suo
interno. La parte del modello dentro la zona e fra le due quote è la colonna, o una sua parte.

* **L'asse.** Due partenze: l'asse trovato dalle normali della superficie (come la ceramica del programma trova l'asse
  di un vaso: in qualunque direzione, va bene anche un fusto caduto) e la verticale per il centro della parte. Ognuna
  si affina sui cerchi adattati a fette perpendicolari; vince quella con le fette più rotonde. I cerchi lasciano fuori
  quello che non sta su di loro: le pinne di pixel volanti che una mesh fotogrammetrica fa crescere lungo un contorno,
  una scheggiatura della pietra.
* **Il profilo misurato.** Il modello si taglia con 36 semipiani per l'asse; a ogni quota (un passo dalla risoluzione
  della mesh) il raggio è il più denso fra quelli trovati lì, e se ne tiene la dispersione. Letti e sommità (dove il
  taglio corre in piano) restano fuori.
* **Il solido.** Una spline per K nodi (12 di base) equidistanti dalla base alla cima, con i raggi adattati ai minimi
  quadrati al profilo misurato; oppure, con *Come misurato*, il profilo misurato stesso. Il profilo ruota di 360 gradi.
  L'**entasi** è il raggio più grande oltre la corda fra il raggio della base e quello della cima.
* **Più parti.** Base, fusto e capitello sono elementi separati: *Stesso asse di* dà a una parte l'asse di una colonna
  già fatta.

*Modifica parametri...* cambia il raggio di ogni nodo, il numero dei nodi o la scelta *Come misurato*: il profilo
misurato e l'asse restano come sono, cambia solo il solido, e lo scarto si misura di nuovo. Una modifica che non riesce
o viene annullata non cambia nulla: l'elemento tiene i suoi numeri e i suoi file.

Il plugin avvisa (nella tabella, nel riassunto e nell'IFC) quando il profilo ha un **gradino**, una base o un abaco
dentro la zona: base, fusto e capitello vanno fatti come elementi separati. Per un **fusto coricato** quale capo sia la
base è arbitrario: raggio alla base e in cima possono risultare scambiati.

<figure markdown="span">
  ![Il profilo della colonna](../img/plugins/architecture/column-profile.png)
  <figcaption>La colonna degli esempi: il profilo vero (grigio), i raggi misurati sul modello del programma (rosso),
  i 12 nodi e la spline del solido (blu).</figcaption>
</figure>

<figure markdown="span">
  ![La colonna: modello e solido](../img/plugins/architecture/column-3d.jpg)
  <figcaption>Il modello della colonna (grigio, tolto a destra) e il suo solido (arancio).</figcaption>
</figure>

## La modanatura

Una cornice, una modanatura, una cornice marcapiano: una sezione portata lungo un percorso.

* **Il percorso**: tracciato sulla pianta (la sua quota è quella che dai, di base la sommità del modello al primo
  punto); tracciato su un **prospetto o una sezione** del progetto, mostrato apposta nella vista 2D (ogni punto torna
  nel mondo attraverso il sistema del prospetto, alla profondità del suo rilievo o, dove il rilievo non ha valore, della
  superficie del modello più vicina a chi guarda: un percorso così tiene le sue quote, anche una cornice inclinata);
  oppure dritto **fra due marker**.
* **La sezione**: il modello tagliato dal piano normale al percorso in una stazione (di base il centro del primo
  tratto). Sulla linea del taglio più vicina al punto del percorso il plugin trova la **faccia del muro**: il più
  arretrato dei tratti verticali (di almeno 3 cm) da cui la linea viene o verso cui va, preferendo quello che si vede
  sia sotto sia sopra la modanatura (la fascia o il listello della cornice stessa non sono il muro; un parapetto dietro
  si vede solo sopra). Senza un tratto così vale il punto più arretrato della linea, e l'elemento lo dice («nessuna
  faccia del muro nel tratto: sporgenza dal punto più arretrato»). Il plugin tiene solo il tratto che **ne sporge** più
  della risoluzione della mesh, quello al punto del percorso: l'altezza della modanatura, non il muro sopra e sotto. Il
  tratto si chiude con una linea di fondo *Profondità nel muro* dietro la faccia e si semplifica (Douglas-Peucker, in
  metri). La **sporgenza** si misura dalla faccia del muro. Di base la finestra si trova dal taglio (l'estensione della
  modanatura e 10 cm intorno, conservata nell'elemento); una finestra data nel dialogo o con `--window` limita la
  ricerca. Sporge verso le foto, o dal lato che scegli. Un elemento isolato (un taglio che si richiude su se stesso) si
  tiene intero e si descrive con larghezza e altezza, senza sporgenza. L'**area della sezione** data è quella della
  modanatura davanti alla faccia del muro; la sezione del solido aggiunge la lastra dietro (`section_area_with_slab`).
* **Il solido**: la sezione portata lungo il percorso con gli **angoli a quartabuono**, come gira l'angolo una cornice
  scolpita; un'estrusione se il percorso è dritto.

*Modifica parametri...* cambia la semplificazione e la profondità nel muro, dalla linea del taglio conservata
nell'elemento.

<figure markdown="span">
  ![La sezione della cornice](../img/plugins/architecture/cornice-section.png)
  <figcaption>La cornice degli esempi: il taglio del modello (grigio), la faccia del muro trovata (verde), la sezione
  del solido (blu, chiusa 10 cm dentro il muro) contro quella vera (rosso). La sommità è più alta perché lo è il modello
  del programma: la cima del muro si vedeva solo di scorcio.</figcaption>
</figure>

## L'arco e la volta

* **Il piano**: una sezione del progetto (un prospetto di tipo *sezione*), o un piano verticale tracciato ora con due
  punti in pianta. Il modello si taglia con esso.
* **Il punto nell'apertura**: cliccato sulla sezione mostrata nella vista 2D, o scritto come (u, v); il dialogo propone
  il centro dell'apertura più ampia che trova. Dal punto il plugin si guarda intorno: quello che vede per primo è
  l'**intradosso** con gli stipiti, dove ogni raggio esce dalla ghiera è l'**estradosso**.
* **L'adattamento**, ai minimi quadrati sull'intradosso: un cerchio (**a tutto sesto**, quando il centro sta sulla linea
  d'imposta, o **ribassato**), un arco **a sesto acuto** (due cerchi centrati sulla linea d'imposta che si incontrano in
  chiave), un'**ellisse** con gli assi orizzontale e verticale, o **come misurato**. Gli stipiti sotto la linea
  d'imposta fanno parte dell'adattamento, come linee verticali. Con *il migliore (scarto)* vince il tipo più semplice
  entro il 25 % dello scarto migliore: un cerchio prima di un arco acuto, prima di un'ellisse.
* **Spessore**: la distanza mediana dell'estradosso dall'intradosso, accettata solo quando gli attraversamenti più
  lontani concordano (dispersione sotto il 20 % della mediana, mediana sotto metà della luce); altrimenti il plugin lo
  chiede, con il motivo. Un arco la cui ghiera è **a filo del muro** intorno non mostra un estradosso in sezione: gli
  attraversamenti lontani sono i fianchi e la sommità del muro, e lo spessore va dato. **Profondità**: l'estensione del
  modello dietro la ghiera lungo la normale del piano, o data. *Volta a botte* scrive lo stesso solido come volta.
* **Il tipo chiesto si rispetta**: a tutto sesto mette il centro sulla linea d'imposta, ribassato sotto di essa. La
  tabella, il riassunto e l'IFC danno lo scarto quadratico del tipo scelto e degli altri provati, per esempio «a sesto
  acuto (rms 1,3 mm; cerchio 45,8, ellisse 9,9)».

Una ghiera a tutto sesto, ribassata o a sesto acuto si scrive con archi veri (tre punti ciascuno) in STEP e IFC. Una
ghiera ellittica o come misurata è una spezzata di 96 punti, e l'estradosso di una ghiera ellittica è l'ellisse con i
due semiassi allungati dello spessore (non una curva a distanza costante).

<figure markdown="span">
  ![La sezione dell'arco](../img/plugins/architecture/arch-section.png)
  <figcaption>La ghiera degli esempi sul suo piano: il taglio del modello (grigio), l'intradosso visto dal punto
  (rosso), la ghiera dell'arco a sesto acuto adattato (blu).</figcaption>
</figure>

## Porte e finestre

*Porta / finestra...* (nel pannello e nel menu *Plugin*) chiede un clic **dentro l'apertura sulla pianta**, fra i due
stipiti, sull'ortofoto o sul DEM del chunk. Il solido è il **vano**, da sottrarre al muro.

* **La quota del taglio.** Di base (*automatica*) il plugin taglia il modello in orizzontale a molte quote e tiene
  quelle in cui la pianta di un'apertura si legge intorno al punto; il taglio è a metà di esse. Con *automatica*
  spenta si usa la quota data, qualunque sia (anche 0 m).
* **Prima le facce del muro.** Sul taglio, le facce del muro sono i tratti dritti del taglio sui quali il punto cade in
  un **vano**, il tratto continuo senza interruzioni da entrambe le parti: il più vicino è la faccia vicina, la faccia
  lontana è il tratto parallelo più vicino a essa. Una stanza dietro la porta, un corridoio, un edificio dall'altra
  parte della strada non vengono mai presi: i loro muri sono oltre la faccia lontana, passano attraverso la proiezione
  del punto o finiscono prima.
* **Gli stipiti** sono solo i punti del taglio fra le due facce che partono dagli estremi del vano, ciascuno adattato a
  una retta (uno stipite strombato resta strombato). Se sul modello c'è una sola faccia del muro (una facciata
  fotografata dalla strada) il plugin chiede lo **spessore del muro**: l'altra faccia si mette a quella distanza dietro.
  Uno spessore dato prende sempre il posto di quello misurato (si sposta la faccia interna, quella esterna resta). Un
  muro sottile su un modello rado (facce più vicine di tre lati della mesh) viene detto come tale: dai lo spessore, o
  usa un modello più fitto.
* **La faccia esterna** è quella vista da più foto allineate che inquadrano l'apertura (i punti del modello di ogni
  faccia vicino all'apertura proiettati in ogni foto, dal lato verso cui la faccia guarda). Quando le foto non le
  distinguono (nessuna foto, o tante da una parte quante dall'altra) decide il campo *Faccia esterna*: *auto* prende la
  faccia più vicina al clic, oppure la scegli. Il riepilogo dice quale regola è stata usata.
* **Soglia e architrave.** Dalla sezione verticale attraverso il muro per il punto: la prima linea incontrata verso il
  basso (la soglia o il pavimento) e verso l'alto (l'architrave), come mediana di raggi nella parte centrale dello
  spessore del muro: per una soglia inclinata, la quota a metà spessore, con un avviso che ne dà la pendenza. Si
  possono dare entrambe.
* **La parte alta.** *Architrave piano*, oppure **un arco** già fatto (la sua sezione deve essere verticale e
  attraversare il muro sopra l'apertura): la pianta si estrude fino alla linea d'imposta dell'arco e sopra si aggiunge
  il vuoto sotto il suo intradosso (archi veri per un arco a tutto sesto, ribassato o a sesto acuto), per tutto lo
  spessore: un solo solido. Il plugin avvisa se la luce dell'arco non è la larghezza dell'apertura, e se sotto
  l'imposta il modello ha un architrave (il vuoto dell'arco entrerebbe nella muratura). L'arco di un'apertura non si
  può rimuovere finché l'apertura lo usa; se l'arco si modifica, l'apertura è segnata *da ricostruire* (nella tabella
  e nel riepilogo; le sue esportazioni sono rifiutate) finché **Ricostruisci** non la rifà sull'arco nuovo.
* **Tipo.** *Solo apertura*, *porta* o *finestra*: nell'IFC il vano è un `IfcOpeningElement` in un muro sostitutivo, e
  una porta o una finestra si scrive come `IfcDoor` o `IfcWindow` con lo stesso solido, che lo riempie.

L'elemento riporta la **larghezza** alla faccia esterna e a quella interna e, da altri due tagli, vicino alla soglia e
vicino all'architrave (il plugin avvisa se gli stipiti non sono verticali); l'**altezza**, la **soglia** e
l'**architrave** come quote e come altezze dal suolo del DEM del chunk davanti alla soglia (se c'è un DEM); la
**profondità** degli stipiti (lo spessore del muro, misurato o dato); la **strombatura** di ogni stipite in gradi,
rispetto alla normale della faccia esterna, positiva quando l'apertura si allarga verso la faccia interna; sinistra e
destra come si vedono da fuori; l'area del vano sulla faccia esterna.

*Modifica parametri...* cambia la soglia, l'architrave (non con la parte alta ad arco: viene dall'arco), la larghezza
alla faccia esterna (i due stipiti spostati di metà della differenza, con la loro strombatura) e lo spessore del muro:
quanto misurato resta com'è, cambia solo il solido.

<figure markdown="span">
  ![La finestra: pianta e sezione](../img/plugins/architecture/window.png)
  <figcaption>La finestra strombata degli esempi. A sinistra: il taglio orizzontale del modello a metà altezza
  dell'apertura (grigio), gli stipiti visti dal punto cliccato (rosso), la pianta del solido (blu) e la verità (verde,
  tratteggiata); è stata fotografata solo la faccia esterna del muro, lo spessore è stato dato. A destra: la sezione
  verticale per il punto, soglia e architrave del solido contro la verità.</figcaption>
</figure>

## Come si legge lo scarto

Ogni elemento porta il suo **scarto**: la distanza dei punti del modello dal solido, come **mediana**, **90°
percentile** e **massimo**, con il numero dei punti. Una mediana di pochi millimetri dice che il solido segue il
modello; un massimo grande indica quello che il solido non ha (una scheggiatura, una grappa, un rappezzo più tardo). I
punti sono quelli della zona dell'elemento: per una colonna la parte scelta; per un arco i punti entro tre lati della
mesh dall'intradosso o dall'estradosso, non quelli sulle due facce piane (che una ghiera a filo del muro condivide con
il muro), di cui si dà il numero; per una modanatura il riquadro del solido senza i punti sulla faccia del muro o dietro
(stanno sul solido per costruzione e farebbero sembrare lo scarto migliore), di cui si dà il numero; per un'apertura i
punti degli stipiti, della soglia e dell'architrave (e dell'intradosso dell'arco), fra le due facce del muro e non su di
esse, di cui si dà il numero.

Lo scarto confronta il solido con il **modello**, non con il monumento: un modello dista già di qualche millimetro
dalla superficie vera, e il solido lo eredita.

## Parametri

| Elemento | Parametro | Significato |
|---|---|---|
| Colonna | quota minima e massima | la parte del modello fra le due |
| | nodi | 12; da 2 a 60 |
| | come misurato | il profilo misurato al posto della spline |
| | stesso asse di | l'asse di una colonna già fatta |
| Modanatura | quota | di un percorso tracciato sulla pianta |
| | sezione a | metri dall'inizio del percorso |
| | finestra sotto, sopra, sporgenza massima | 0 = trovata dal taglio (l'estensione della modanatura e 10 cm); altrimenti limita la ricerca |
| | sporge verso | le foto, a sinistra o a destra del percorso |
| | profondità nel muro | della linea di fondo, 0,1 m |
| | semplificazione | tolleranza di Douglas-Peucker, 2 mm |
| Arco | punto nell'apertura | cliccato o (u, v) |
| | tipo | il migliore, a tutto sesto, ribassato, a sesto acuto, ellittico, come misurato |
| | spessore, profondità | 0 = misurati |
| | volta a botte | lo stesso solido come volta |
| Porta / finestra | tipo | solo apertura, porta, finestra |
| | quota del taglio | automatica (a metà dell'apertura), o una quota (anche 0 m) |
| | faccia esterna | auto (quella vista dalle foto; se no la più vicina al clic), la più vicina, l'altra |
| | parte alta | architrave piano, o un arco del chunk |
| | spessore del muro | 0 = misurato (serve quando sul modello c'è una sola faccia) |
| | soglia, architrave, larghezza (modifica) | quote; la larghezza alla faccia esterna, simmetrica |

## File e formati

Tutto di un elemento sta in `<dati del chunk>/architecture/<id>/`: `solid/solid.step` (AP214, in metri),
`solid/solid.stl` (tassellato a 0,5 mm; l'STL non ha unità, i suoi numeri sono metri), `model.ply` (quello che mostra la
vista 3D) ed `element.json` con tutto: origine, parametri, profilo misurato, geometria, scarto, solido, file. Il
modello, la trasformazione e i prodotti del chunk non cambiano mai.

Le coordinate di STEP, STL e IFC sono quelle del sistema del chunk meno un'**origine** tonda (una per chunk, in
`element.json`): tutti gli elementi di un chunk combaciano fra loro e con la mesh, con numeri piccoli. *Esporta STEP...*
copia i file, ognuno con un `<nome>.origin.json` accanto (l'origine, il sistema, le unità, il posizionamento: mondo =
coordinate dello STEP + origine); *DXF...* scrive il profilo o la sezione in scala 1:1 in metri, sui layer SOLIDO,
MISURATO, ASSE e IMPOSTA; per un'apertura la pianta (dal centro della faccia esterna: gli stipiti misurati accanto a
quelli ideali, le facce del muro su FACCE) e, accanto, il prospetto da fuori con la soglia (SOGLIA) e l'architrave
(ARCHITRAVE).

### IFC

*Esporta IFC...* (anche *File > Esporta > Elementi architettonici (IFC)*) scrive IFC4 come l'esportazione IFC del
programma: progetto, sito, metri, `IfcMapConversion` e `IfcProjectedCRS` sull'origine del chunk; la mesh del rilievo
accanto agli elementi a richiesta (`IfcGeographicElement`). I solidi sono **parametrici**:

| Elemento | Classe IFC | Solido |
|---|---|---|
| Colonna | `IfcColumn` (COLUMN) | `IfcRevolvedAreaSolid` della mezza sezione (`IfcArbitraryClosedProfileDef`) |
| Modanatura dritta | `IfcMember` (USERDEFINED, "Moulding") | `IfcExtrudedAreaSolid` |
| Modanatura lungo un percorso | `IfcMember` (USERDEFINED, "Moulding") | `IfcFixedReferenceSweptAreaSolid` su un `IfcIndexedPolyCurve` |
| Arco, volta | `IfcBuildingElementProxy` ("Arch", "Vault") | `IfcExtrudedAreaSolid` di linee e `IfcArcIndex` |
| Apertura | `IfcOpeningElement` (OPENING), che svuota il muro sostitutivo | `IfcExtrudedAreaSolid` della pianta (`IfcArbitraryClosedProfileDef`) dalla soglia in su; con un arco, un secondo del vuoto sotto l'intradosso |
| Porta, finestra | `IfcDoor` (DOOR), `IfcWindow` (WINDOW), che riempiono l'apertura | lo stesso solido; `OverallWidth`, `OverallHeight` |
| Muro sostitutivo | `IfcWall` "Muro (porzione misurata)" | `IfcExtrudedAreaSolid` del muro intorno all'apertura |

`Pset_PyArchInit_Element`: `Kind`, `Method`, `Name`, ogni parametro come `P_<nome>` (metri, gradi), `Fit_median_m`,
`Fit_p90_m`, `Fit_max_m`, `Fit_points`, `Fit_candidates` (archi), `Warnings`, `Source`, `Representation`, `Frames`
(quali valori sono coordinate del mondo e quali stanno nel piano della sezione; gli archi portano anche `P_world_apex` e
i punti d'imposta nel mondo, le modanature gli estremi del percorso) e una nota che il solido è un'interpretazione del
modello misurato (le aperture anche `P_sill`, `P_lintel`, `P_reveal_depth`, `P_splay_left`, `P_splay_right` e `Host`);
`Qto_PyArchInit_Element` con volume e superficie.

L'IFC vuole un'apertura in un muro (`IfcRelVoidsElement`), e la sua porta o finestra nell'apertura
(`IfcRelFillsElement`). Il programma non ha ancora un elemento muro, così il plugin scrive un **muro sostitutivo**: la
porzione di muro misurata intorno all'apertura (spessore misurato o dato, mezzo metro oltre ogni stipite, dal suolo del
DEM davanti alla soglia, o da mezzo metro sotto la soglia, a mezzo metro sopra l'architrave). Le aperture le cui
porzioni si sovrappongono nello stesso muro ne condividono uno. Il suo insieme di proprietà dice che è sostitutivo, non
un muro rilevato: il muro vero lo farà il plugin delle murature. Il file passa `ifcopenshell.validate` (provato). Un IFC
riletto da IfcOpenShell dà il volume del solido di CadQuery entro l'1 % (provato, anche con le cornici inclinate).

### Quando il sistema del chunk cambia

L'origine, la trasformazione e il sistema del chunk si conservano con gli elementi. Se poi il chunk viene
georeferenziato o portato in un altro sistema (riferimenti, *GNSS > Sistema e quote*), gli elementi restano nel sistema
vecchio: il plugin rifiuta di esportarli, mostrarli o modificarli finché **Ricostruisci** (nel pannello, o
`ppg plugin architecture rebuild`) non li rifà al loro posto (stessi identificativi: una parte fatta sullo stesso asse
di una colonna lo resta), portando zone, percorsi, piani e punti nel sistema nuovo e misurandoli di nuovo sul modello.
Anche i parametri modificati a mano (raggi dei nodi, tipo, spessore, profondità, semplificazione; soglia, architrave,
larghezza e spessore del muro di un'apertura) vengono portati, e
l'elemento dice «parametri modificati a mano mantenuti».

## Riga di comando

```bash
ppg plugin architecture install
ppg plugin architecture column PROGETTO.ppg --polygon "POLYGON((x y, ...))" --zmin 0.05 --zmax 1.95 --knots 12
ppg plugin architecture moulding PROGETTO.ppg --path "LINESTRING(x y, x y)" --height 3.35 --station 1 \
    --window -0.4,-0.03,0.02,0.3 --out-side -1
ppg plugin architecture moulding PROGETTO.ppg --elevation "Prospetto sud" --path "LINESTRING(u v, u v)"
ppg plugin architecture arch PROGETTO.ppg --section "Sezione A" --pick 1.8,0.8 --type auto
ppg plugin architecture arch PROGETTO.ppg --points x1,y1,x2,y2 --pick 1.8,0.8 --thickness 0.25
ppg plugin architecture opening PROGETTO.ppg --at x,y --kind window --thickness 0.5 [--height H] [--top Arco] \
    [--outer auto|near|far] \
    [--sill S] [--lintel L]
ppg plugin architecture rebuild PROGETTO.ppg
ppg plugin architecture list PROGETTO.ppg
ppg plugin architecture export PROGETTO.ppg --out elementi.ifc --mesh
ppg plugin architecture export PROGETTO.ppg --out arco.dxf --ids Arco
```

## Gli esempi, contro la verità

Il programma ha fatto i modelli con 64 foto della colonna e 76 del muro. «Modello contro verità» è la distanza del
modello del programma dalla superficie vera nella zona: l'errore della fotogrammetria, che il solido eredita.

| Colonna (2 m, raggio 0,30 m) | plugin | verità |
|---|---|---|
| inclinazione dell'asse | 0,0005° | 0° |
| distanza dell'asse da quello vero | 0,1 mm | 0 |
| raggio alla base / in cima | 300,7 / 273,6 mm | 300,6 / 272,1 mm |
| entasi | 14,1 mm | 14,1 mm |
| nodi: scarto massimo / mediano | 1,6 / 0,7 mm | |
| volume | 0,5246 m³ | 0,5220 m³ (+0,49 %) |
| scarto (mediana / p90) | 0,3 / 0,7 mm | |
| modello contro verità (mediana / p90) | 0,8 / 1,7 mm | |

| Ghiera a sesto acuto (luce 1,6 m, spessore 0,25 m) | plugin | verità |
|---|---|---|
| tipo | a sesto acuto | a sesto acuto |
| luce | 1596,2 mm | 1600,0 mm |
| freccia | 1393,0 mm | 1385,6 mm |
| raggio | 1614,8 mm | 1600,0 mm |
| linea d'imposta | 1388,2 mm | 1400,0 mm |
| spessore della ghiera | 254,0 mm | 250,0 mm |
| scarto sulle facce curve (mediana / p90) | 1,8 / 9,2 mm | |
| punti sulle due facce piane, esclusi | 8645 | |
| modello contro verità (mediana / p90) | 1,9 / 7,6 mm | |

| Cornice (sporgenza 0,18 m, altezza 0,25 m), valori predefiniti del dialogo | plugin | verità |
|---|---|---|
| sporgenza dalla faccia del muro | 184,1 mm | 180,0 mm |
| altezza della cornice | 278,9 mm | 250,0 mm |
| area della sezione davanti al muro | 338,7 cm² | 299,0 cm² (+13,3 %) |
| scarto (mediana / p90) | 1,8 / 5,3 mm | |
| punti sulla faccia del muro o dietro, esclusi | 1599 | |
| modello contro verità (mediana / p90 / max) | 3,2 / 6,6 / 22,0 mm | |

La sezione della cornice segue il modello: sporgenza, altezza e area in più vengono dalla sommità del muro e della
cornice, che il programma ha ricostruito fino a 2 cm più in alto (la vedono solo di scorcio le foto più alte), non dal
plugin. Con i valori predefiniti del dialogo la finestra si è trovata dal taglio e la sezione è la sola cornice.

Nello stesso muro, a sinistra dell'arco, una **porta** (0,9 m, stipiti diritti, una soglia alta 5 cm) e, a destra, una
**finestra strombata di 10 gradi** per parte (0,8 m alla faccia esterna, più larga dentro). È stata fotografata solo la
faccia esterna del muro: senza spessore il plugin lo ha chiesto, e sono stati dati 0,5 m. Il punto è stato cliccato in
pianta in mezzo a ciascuna apertura; la quota del taglio l'ha trovata il plugin.

| Porta (taglio a 1,072 m) | plugin | verità |
|---|---|---|
| larghezza alla faccia esterna | 895,9 mm | 900,0 mm |
| larghezza alla faccia interna | 895,5 mm | 900,0 mm |
| larghezza vicino alla soglia / all'architrave | 895,2 / 898,6 mm | 900,0 mm |
| strombatura sinistra / destra | -0,30° / 0,25° | 0° |
| soglia | 49,1 mm | 50,0 mm |
| architrave | 2099,0 mm | 2100,0 mm |
| volume del vano | 0,9180 m³ | 0,9225 m³ (-0,48 %) |
| scarto (mediana / p90) | 1,6 / 5,3 mm | |
| punti sulle facce del muro, esclusi | 8282 | |
| modello contro verità (mediana / p90) | 2,0 / 6,1 mm | |
| il modello stesso dentro gli stipiti veri, sinistro / destro, alla quota del taglio | 3,0 / 0,6 mm | 0 |
| faccia esterna | vista da 46 foto, l'altra da nessuna | |

| Finestra (taglio a 1,498 m) | plugin | verità |
|---|---|---|
| larghezza alla faccia esterna | 796,0 mm | 800,0 mm |
| larghezza alla faccia interna | 970,0 mm | 976,3 mm |
| larghezza vicino alla soglia / all'architrave | 800,6 / 796,3 mm | 800,0 mm |
| strombatura sinistra / destra | 10,01° / 9,73° | 10° |
| soglia | 998,8 mm | 1000,0 mm |
| architrave | 1997,3 mm | 2000,0 mm |
| volume del vano | 0,4409 m³ | 0,4441 m³ (-0,72 %) |
| scarto (mediana / p90) | 1,7 / 11,0 mm | |
| punti sulle facce del muro, esclusi | 9274 | |
| modello contro verità (mediana / p90) | 2,9 / 11,2 mm | |
| il modello stesso dentro gli stipiti veri, sinistro / destro, alla quota del taglio | 3,6 / 1,1 mm | 0 |
| faccia esterna | vista da 55 foto, l'altra da nessuna | |

Il progetto chiedeva 5 mm. Ogni numero della porta ci sta, e così le larghezze della finestra alla faccia esterna,
vicino alla soglia (0,6 mm) e vicino all'architrave; la larghezza alla faccia interna è 6,3 mm più stretta. È l'errore
del modello, non del plugin: gli stipiti della finestra si vedono di scorcio attraverso un'apertura larga 0,8 m, e alla
quota del taglio il modello del programma è già 3,6 mm (sinistro) e 1,1 mm (destro) dentro gli stipiti veri (l'ultima
riga, misurata dalla prova sul modello: `results.json`); il solido segue il modello, e una strombatura di 10 gradi
porta quell'errore alla faccia interna.

## Limiti

* **Solo angoli a quartabuono.** Una modanatura gira l'angolo come una scolpita; gli angoli arrotondati non ci sono
  (l'IFC descriverebbe un altro solido).
* **Modanature inclinate.** Un percorso in pendenza o che gira è percorso da CadQuery con il suo sistema e scritto
  nell'IFC come una sezione portata con il riferimento verso l'alto: i due coincidono in volume ed estensione
  (provato), ma la sezione di una cornice molto inclinata è quella normale al percorso, non una verticale.
* **Aperture**: la pianta è la stessa a ogni quota (un prisma dalla soglia in su); gli stipiti inclinati sono segnalati
  (le larghezze vicino alla soglia e all'architrave), non modellati. Nell'IFC il muro intorno a un'apertura è
  sostitutivo.
* **L'ambiente** occupa circa 900 MB, e la prima elaborazione dopo l'installazione richiede circa un minuto.
* **Windows ARM**: non disponibile (OpenCascade non ha una versione).
* Non trova gli elementi da solo, non modella l'intaglio di un capitello, né volte a crociera o a cupola, né muri,
  e non modifica nulla nella vista 3D. Il solido vale quanto il modello su cui è adattato: lo scarto dice quanto
  distano.

## Licenze

Il plugin è GPL-2.0-or-later. Nel suo ambiente: CadQuery e OCP (Apache-2.0), OpenCascade (LGPL-2.1 con eccezione), VTK
(BSD-3-Clause), ezdxf (MIT), nlopt (MIT e LGPL), numpy (BSD).
