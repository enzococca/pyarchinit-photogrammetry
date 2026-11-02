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
con una riga rossa che dice l'indirizzo o il motivo; il pannello resta utilizzabile. *Ferma* interrompe una
domanda mentre il modello scrive ancora; *Nuova conversazione* dimentica la storia.

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
