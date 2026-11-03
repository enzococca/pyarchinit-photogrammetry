# L'assistente

Il pannello *Assistente* (in basso, accanto alla *Console Python*; *Vista > Assistente* o *Strumenti >
Assistente > Apri l'assistente*) è una conversazione con un modello linguistico sul progetto e sul programma,
e un editor di script per far girare sul progetto ciò che il modello propone - o ciò che scrivi tu.

![Il pannello Assistente](../img/gui/pane-assistant.it.png)

## Su cosa risponde

Il modello legge le domande insieme ai passi di questa guida che le riguardano, e può guardare il progetto con
strumenti che leggono soltanto: il riepilogo del progetto, l'allineamento e i suoi errori, i marker, il DEM,
l'ortofoto, la nuvola densa, il modello, i prodotti termici e multispettrali. Risponde nella lingua dell'interfaccia. Quando la risposta contiene uno script Python,
*Apri nell'editor* sotto di essa copia lo script nell'editor a destra.

## I tre tipi di modello

| Tipo | Dove gira | Cosa serve |
| --- | --- | --- |
| **Locale** | su questo computer: LM Studio, llama.cpp, Ollama, Unsloth (qualsiasi server con l'API chat-completions di OpenAI) | il server avviato con un modello caricato; il programma lo cerca sulle porte solite, o usa l'indirizzo che gli dai |
| **Claude** | da Anthropic | una chiave; modelli `claude-opus-5-5` (predefinito) o `claude-opus-5` |
| **OpenAI** | da OpenAI | una chiave; i modelli sono quelli del tuo account |

Un server locale è sempre e solo un indirizzo di questo computer (`127.0.0.1`, `::1`, `localhost`): ogni altro
indirizzo è rifiutato. Il programma non scarica mai modelli: li carichi tu nel server.
Alcuni piccoli modelli locali scrivono come JSON nella risposta le chiamate agli strumenti del progetto invece di
farle: il programma legge una chiamata così dal testo e la esegue come una vera, e *Cosa viene inviato* dice
"chiamata letta dal testo".

La casella in alto nel pannello sceglie il tipo (l'ultima voce apre le impostazioni); il pallino accanto è
**verde "locale"** quando niente esce dal computer e **arancione "esterno"** quando le domande vanno a Claude o
a OpenAI.

## Cosa esce dal computer

Con un modello locale non esce niente. Con Claude o OpenAI il testo passa prima da un filtro:

* i nomi del sito, dei luoghi e delle persone del progetto sono sostituiti da segnaposto ("Sito A",
  "Persona 1") e rimessi al loro posto nella risposta;
* si inviano solo coordinate e quote relative, il sistema di riferimento come "proiettato, in metri" o
  "geografico"; i nomi di file, chunk e progetto diventano neutri;
* foto, posizioni GPS e GNSS non si inviano mai.

Il filtro non riconosce ciò che scrivi tu: prima della prima domanda di una conversazione esterna una riga
arancione lo ricorda - **non scrivere coordinate o nomi nella domanda**. *Cosa viene inviato* mostra, domanda
per domanda, il testo esatto uscito dal computer, e la tabella dei nomi sostituiti dal filtro (nome vero,
inviato come).

Una domanda che non può avere risposta - il server locale fermo, troppo lento, una chiave rifiutata - finisce
con una riga rossa che dice l'indirizzo o il motivo; il pannello resta utilizzabile. Una chiamata che il modello ha già fatto nella stessa risposta non viene eseguita di nuovo (*Cosa viene inviato* dice "già chiamato"), e a un modello che ripete solo le sue chiamate viene chiesto di rispondere. I risultati degli strumenti sono tagliati a 4000 caratteri e, con un modello locale, solo gli ultimi 12 vanno interi (i più vecchi come "(risultato omesso)"), entro 80 000 caratteri per richiesta (`assistant/context_chars` nelle impostazioni). Quando il server dice che la conversazione supera il contesto del modello, i risultati più vecchi vengono lasciati fuori ancora una volta; se non basta, inizia una nuova conversazione. *Ferma* interrompe una
domanda mentre il modello scrive ancora; *Nuova conversazione* dimentica la storia. Scegliere un altro modello
(tipo, modello o indirizzo) o aprire un altro progetto comincia anch'esso una nuova conversazione: la chat si
svuota con una riga che dice perché, così la chat e *Cosa viene inviato* raccontano sempre la stessa
conversazione.

## L'editor di script

A destra: scrivi o incolla uno script (Tab = 4 spazi), poi **Esegui**. Lo script vede i nomi della console
Python (`doc`, `chunk`, `window`, `np`, `ppg`, `polygon`, `cameras`, `progress`) e gira in background come
ogni elaborazione: la finestra resta utilizzabile, la finestra di avanzamento mostra ciò che stampa e *Ferma*
lo interrompe. Ciò che stampa e l'eventuale errore sono nel riquadro sotto l'editor.

* **Annulla lo script**: tutto ciò che lo script ha cambiato nel progetto - ogni chunk, i prodotti ricostruiti
  (tornano i precedenti), i file scritti nella cartella del progetto, i chunk aggiunti o tolti - torna indietro
  in un passo, anche quando si è fermato a metà con un errore. È la stessa voce di *Modifica > Annulla*. Ciò
  che ha fatto fuori dal progetto (PyArchInit, il GeoPackage dal vivo di QGIS, le esportazioni) viene detto
  dopo l'esecuzione e non si può annullare.
* **Errore? Chiedi all'assistente**: dopo un errore, lo script e il suo traceback vanno al modello, che
  risponde con lo script corretto.
* **Salva come .py**: lo script in un file, da rieseguire con *Strumenti > Esegui script Python...*.

Uno script parte solo quando premi *Esegui*: l'assistente non esegue mai niente da solo. Vedi
[Script](../scripting.md) per ciò che uno script può fare.

## Impostazioni

*Strumenti > Assistente > Impostazioni dell'assistente...* (o *Impostazioni...* nel pannello):

* **Locale**: l'indirizzo del server (*Trova i server* prova le porte solite) e il modello (*Aggiorna i
  modelli* legge l'elenco dal server);
* **Claude**: il modello e la chiave;
* **OpenAI**: il modello (*Aggiorna i modelli* legge quelli del tuo account) e la chiave;
* **Prova la connessione** chiede al server o al servizio i suoi modelli;
* l'**ultima valutazione** di ogni tipo: sotto 2 il tipo è "limitato per gli script" - le sue risposte vanno
  bene da leggere, i suoi script vanno guardati con attenzione.

Le chiavi stanno solo nel portachiavi del sistema (*Salva nel portachiavi*, *Dimentica*) o si leggono dalle
variabili d'ambiente `ANTHROPIC_API_KEY` e `OPENAI_API_KEY`: mai nelle impostazioni, nel progetto o nei log.

## La valutazione

Quanto un modello lavora bene con il programma si misura su un set fisso di 8 domande e 6 compiti di script sul
progetto dimostrativo (`ppg demo CARTELLA --process`): le domande sul progetto (DEM, ortofoto, allineamento e punti di
controllo, marker) e sul programma (curve di livello, IFC, confidenza, buchi nel DEM); gli script la regione su tre
marker, la mappa delle normali, un chunk con le foto di un'area, le foto fuori da un'area disattivate, un DEM del solo
terreno, i punti del terreno esportati. Ogni script gira su una copia del progetto e viene controllato; dopo un errore
il modello ha un secondo tentativo, come con *Errore? Chiedi all'assistente*. Il punteggio è il numero di compiti di
script riusciti.

```bash
ppg assistant eval CARTELLA/scavo.ppg --kind local --base-url http://127.0.0.1:8080/v1 --out locale.json --record
```

`--record` scrive il punteggio nelle impostazioni, dove *Impostazioni...* lo mostra come ultima valutazione del tipo.

| Modello | Dove | Domande | Script (punteggio) |
| --- | --- | --- | --- |
| Qwen2.5-Coder-7B-Instruct Q4_K_M | locale, llama.cpp (`llama-server --jinja`), Apple M4 Pro | 3/8 in italiano, 2/8 in inglese | 3/6 in italiano, 3/6 in inglese |
| Qwen2.5-7B-Instruct Q4_K_M | locale, lo stesso | 2/8 in italiano, 0/8 in inglese | 1/6 in italiano, 0/6 in inglese |

Le risposte di riferimento del set (`--kind reference`) superano 8/8 e 6/6: ogni controllo è raggiungibile.
Qwen2.5-Coder scrive come testo le chiamate agli strumenti del progetto (il programma le legge, vedi sopra) e fa gli
script più semplici - la regione, la mappa delle normali, il chunk di un'area - ma inventa funzioni per la
classificazione e l'esportazione. Qwen2.5-7B-Instruct fa chiamate vere ma le ripete finché il contesto è pieno, ed è più
lento: fra i due, Qwen2.5-Coder-7B è quello da caricare. Sono entrambi modelli piccoli: le loro risposte sul progetto
vanno lette con attenzione. Claude e OpenAI non sono ancora stati valutati (nessuna chiave sul computer della
valutazione).
