# Pubblicare il sito web di un rilievo

Il pannello **Pubblica sul web** costruisce il sito di un rilievo come cartella (vedi
[Esportazioni e rapporti](exports.it.md#sito-web-pubblica-sul-web)). Il passo 4, **Pubblica**, mette quella cartella su
**Cloudflare**, gratis, con il tuo account Cloudflare; la cartella può anche andare a mano su qualsiasi altro hosting
statico (in fondo a questa pagina).

Il programma non chiede, non legge e non conserva mai password, token o chiavi: esegue **wrangler**, il programma a
riga di comando di Cloudflare, che installi tu e con cui fai tu l'accesso. Legge solo quello che wrangler risponde, e
quello che ne mostra o ne conserva è ripulito da tutto ciò che somiglia a un token, a un identificativo di account o a
un indirizzo e-mail. Il progetto conserva solo l'indirizzo del sito, il nome del progetto Cloudflare e del bucket, e la
data.

## Che cosa è gratuito (letto sulla documentazione di Cloudflare il 14 novembre 2026)

| Servizio | Piano gratuito | Fonte |
|---|---|---|
| **Pages**: file per sito | 20.000 (100.000 nei piani a pagamento) | [Limiti di Pages](https://developers.cloudflare.com/pages/platform/limits/) |
| **Pages**: dimensione di un file | 25 MiB | stessa pagina |
| **Pages**: richieste e traffico di un sito statico | gratuiti e illimitati | [Prezzi di Pages](https://developers.cloudflare.com/pages/functions/pricing/) |
| **Pages**: progetti per account | 100 | Limiti di Pages |
| **Pages**: richieste per intervalli (range) | risponde con il file intero (`200`), non a pezzi | [Serving Pages](https://developers.cloudflare.com/pages/configuration/serving-pages/) |
| **R2**: spazio | 10 GB-mese al mese (Standard storage) | [Prezzi di R2](https://developers.cloudflare.com/r2/pricing/) |
| **R2**: scritture (Classe A: invio, elenco, regola CORS) | 1 milione al mese | stessa pagina |
| **R2**: letture (Classe B: scaricamento, head) | 10 milioni al mese | stessa pagina |
| **R2**: traffico in uscita (egress) | gratuito | stessa pagina |
| **R2**: un invio con wrangler | fino a 315 MB | [Caricare oggetti](https://developers.cloudflare.com/r2/objects/upload-objects/) |
| **R2**: indirizzo pubblico `r2.dev` | limitato, solo "for development purposes"; niente cache né Access | [Bucket pubblici](https://developers.cloudflare.com/r2/buckets/public-buckets/) |

R2 va attivato una volta sull'account ("R2 subscription", un checkout nella dashboard); le pagine di R2 non dicono se
viene chiesto un metodo di pagamento quando l'uso resta nella quota gratuita: aspettati che la dashboard lo chieda.
I Workers static assets hanno gli stessi limiti di Pages (20.000 file, 25 MiB per file); Pages non è deprecato e
`wrangler pages deploy` pubblica una cartella senza file di configurazione, per questo il programma usa Pages.

**Dove vanno i file.** Con **Automatico** (predefinito) tutto il sito va su Pages quando ogni file sta entro 25 MiB e i
file sono al massimo 20.000 - di solito è così per uno scavo normale con la dimensione predefinita di 500 MB.
Altrimenti la pagina (`index.html`, `site.json`, `app/`) va su Pages e la cartella `data/` (ortofoto, DEM, modello,
nuvola) in un **bucket R2**, che serve i file a pezzi: il visualizzatore legge i raster grandi un pezzo alla volta. Su
Pages da solo il visualizzatore prende ogni raster intero (fino a 50 MB per file, il suo limite): funziona, si apre solo
più lentamente.

## La prima volta, passo per passo

1. **Un account Cloudflare**: iscriviti gratis su <https://dash.cloudflare.com/sign-up>.
2. **Node.js**: installalo da <https://nodejs.org> (la versione LTS).
3. **wrangler**: in un terminale (*Terminale* su macOS, *PowerShell* su Windows):

        npm install -g wrangler

   Il programma non lo installa mai da solo. Cerca `wrangler` nel PATH e nelle cartelle solite di Node (Homebrew,
   `~/.npm-global/bin`, nvm, Volta), poi `npx --no wrangler` (che non scarica mai nulla).
4. **Fai l'accesso**, una volta, nello stesso terminale:

        wrangler login

   Il browser si apre su Cloudflare: autorizza wrangler. Le credenziali restano a wrangler; il programma non le vede.
5. Nel programma: costruisci il sito (passi 1-2 del pannello), poi nel passo 4 scrivi il nome del **Progetto Pages**
   (lettere minuscole, cifre, trattini; il sito sarà su `https://<nome>.pages.dev`; Cloudflare aggiunge un suffisso se
   il nome è già preso, e il pannello mostra l'indirizzo che assegna).
6. Premi **Controlla**: il pannello trova wrangler, guarda se hai fatto l'accesso (`wrangler whoami`) e mostra il
   **piano**: quanti file e MB vanno su Pages e su R2, ogni limite gratuito con *entro* o *oltre*, che cosa diventa
   pubblico, gli avvisi (in arancione) e ciò che impedisce la pubblicazione (in rosso). Non invia nulla.
7. Premi **Pubblica...**: un messaggio dice che il sito diventa **pubblico** - chiunque abbia l'indirizzo può aprirlo e
   scaricarne i file - e chiede conferma. Un sito costruito con le **Coordinate vere** chiede una seconda volta, in
   rosso: dice a chiunque dove si trova il sito. Senza entrambe le risposte non parte nulla.
8. La pubblicazione va in background con *Annulla*; i comandi di wrangler e le sue risposte vanno nel pannello Log. Alla
   fine il pannello e l'Area di lavoro (**Sito web**) mostrano l'indirizzo, con **Apri nel browser** e **Copia
   l'indirizzo**; l'indirizzo resta con il progetto. L'indirizzo si legge nelle risposte di wrangler, non si
   indovina mai: quando non lo dicono, il pannello lo segnala, le risposte vanno intere (ripulite) nel pannello Log -
   mandacele - e scrivi tu l'indirizzo (dalla dashboard, *Workers & Pages*, o da `wrangler pages project list`) nel
   campo sotto.

Pubblicare di nuovo invia solo ciò che è cambiato: Pages salta i file che ha già, e il programma ricorda i file inviati
a R2 (`.ppg-publish.json` nella cartella del sito, mai pubblicato), così invia solo quelli nuovi o cambiati e cancella
quelli che non sono più nel sito. Quando il sito viene costruito di nuovo dopo una pubblicazione, il passo 4 dice *da
rifare*.

## Quando i dati vanno su R2

Il pannello chiede il nome di un **bucket** (3-63 lettere minuscole, cifre, trattini). Usa un bucket solo per questo
sito: il programma scrive la sua regola CORS (GET e HEAD con l'intestazione `Range` dall'indirizzo della pagina),
sostituendo ogni altra. Crea il bucket se non c'è, invia ogni file dei dati con il suo tipo e una cache di un'ora, e
attiva l'indirizzo pubblico **r2.dev** del bucket, che la copia pubblicata di `site.json` usa (la cartella sul tuo disco
resta com'è stata costruita). Tutto ciò che è in quel bucket diventa pubblico.

L'indirizzo r2.dev serve per le prove: per un sito che resta in linea collega un **dominio** al bucket nella dashboard
(*R2 > il bucket > Settings > Custom Domains*; il dominio dev'essere su Cloudflare) e scrivi il suo indirizzo `https://`
in **Indirizzo dei dati**: allora il programma non tocca r2.dev.

## Limitare chi può aprire il sito: Cloudflare Access

Il programma pubblica un sito pubblico. Per farlo aprire solo ad alcune persone, imposta **Cloudflare Access** (Zero
Trust) nella dashboard, dopo la pubblicazione:

1. *Workers & Pages* > il progetto > *Settings* > **Enable access policy**: protegge gli indirizzi di anteprima.
2. Per proteggere anche l'indirizzo principale `<nome>.pages.dev`: *Manage* sulla regola appena creata, poi in *Access >
   Applications* apri l'applicazione, *Configure*, e in *Public hostname* cancella l'`*` dal campo *Subdomain* (i passi
   di Cloudflare: [Known issues di Pages](https://developers.cloudflare.com/pages/platform/known-issues/)).
3. Scrivi chi può entrare (per esempio un elenco di indirizzi e-mail, che ricevono un codice per e-mail).

I dati in R2 dietro l'indirizzo r2.dev non si possono proteggere con Access: con un sito riservato usa un dominio per il
bucket e proteggi anche quello con Access, oppure tieni tutto il sito su Pages.

## Cancellare il sito

- Il sito: *Workers & Pages* > il progetto > *Settings* > **Delete project**, oppure in un terminale
  `wrangler pages project delete <nome>`.
- I dati: *R2* > il bucket > cancella gli oggetti, poi il bucket; oppure spegni il suo indirizzo pubblico con
  `wrangler r2 bucket dev-url disable <bucket>`.

## Dalla riga di comando

    ppg web publish cloudflare CARTELLA_SITO --project NOME --dry-run      # il piano, nulla eseguito né inviato
    ppg web publish cloudflare CARTELLA_SITO --project NOME --yes-public
    ppg web publish cloudflare CARTELLA_SITO --project NOME --bucket NOME-dati --yes-public
    ppg web publish cloudflare CARTELLA_SITO --project NOME --yes-public --yes-true-coordinates   # sito con --frame crs

`--yes-public` è obbligatorio; un sito costruito con le coordinate vere chiede anche `--yes-true-coordinates`. Altre
opzioni: `--data auto|pages|r2`, `--data-url https://...` (un dominio del bucket), `--origin https://...` (un altro
indirizzo della pagina autorizzato a leggere il bucket), `--record PROGETTO.ppg` (conserva l'indirizzo nel progetto).

I comandi di wrangler che il programma esegue, in ordine: `whoami --json`; `pages project list --json` e, per un
progetto nuovo, `pages project create NOME --production-branch main`; con R2: `r2 bucket info BUCKET` (e `r2 bucket
create BUCKET` se manca), `r2 object put BUCKET/CHIAVE --file FILE --content-type TIPO --cache-control "public,
max-age=3600" --remote` per ogni file nuovo o cambiato, `r2 object delete BUCKET/CHIAVE --remote` per ogni file che non
c'è più, `r2 bucket dev-url enable BUCKET -y` e `r2 bucket dev-url get BUCKET` (senza un dominio), `r2 bucket cors set
BUCKET --file REGOLA.json -y`; per ultimo `pages deploy CARTELLA --project-name NOME --branch main --commit-dirty=true`.

## Altri hosting

- **Qualsiasi hosting statico** (un server dell'università, Netlify, un bucket S3...): copia la cartella del sito così
  com'è. L'host dovrebbe rispondere alle richieste per intervalli (`Accept-Ranges: bytes`) per i raster grandi; senza,
  il visualizzatore prende ogni file intero fino a 50 MB.
- **GitHub Pages**: il sito pubblicato può essere al massimo di 1 GB, con un limite morbido di 100 GB di traffico al
  mese e di 10 build all'ora ([limiti di GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits));
  Git rifiuta i file sopra 100 MB. Va bene per rilievi piccoli: metti la cartella in un repository e attiva Pages.
- **Tailscale** per un'anteprima riservata ai colleghi: avvia `ppg web serve CARTELLA_SITO` e, in un altro terminale,
  `tailscale serve 8765` (lo vedono solo i computer della tua tailnet) - oppure `tailscale funnel 8765` per aprirlo a
  internet per un po'.
