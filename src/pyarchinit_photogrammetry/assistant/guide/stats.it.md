# Download e visite

Quante persone scaricano pyarchinit-photogrammetry e visitano le sue pagine, e da dove. Un workflow del
repository raccoglie i numeri ogni settimana. Robot, mirror, integrazione continua e le visite dell'autore sono
esclusi dove la fonte lo permette; le note in fondo dicono dove non lo permette.

<!-- ppg-stats it -->

## Come sono fatti i numeri

**Download dell'applicazione.** GitHub conta ogni download dei file di una versione: il dmg per macOS e gli zip
per Windows x64 e Windows su ARM, con e senza la versione nel nome. Non dà il paese, e solo il totale di ogni
file, per questo il workflow lo legge ogni settimana. I download di un mese sono quelli contati alle letture
fatte in quel mese; quelli fatti prima della prima lettura vanno nel mese della loro versione. Il conteggio
comprende l'aggiornamento automatico dell'applicazione, che scarica ogni nuova versione per chi la usa già, e
alcune prove dell'autore.

**Clic di download.** La [pagina di installazione](install.md) conta un clic sul pulsante di download, o su una
voce del suo menu, come evento di GoatCounter: per questi clic si conoscono il paese e la provenienza. Un
download dalla pagina delle release di GitHub, o dall'aggiornamento automatico, non è un clic.

**Installazioni con pip.** PyPI pubblica ogni download in un dataset pubblico su Google BigQuery. Il conteggio
grezzo comprende tutto: integrazione continua, mirror, cache e scanner. Quello reale tiene gli installatori che
usano le persone (pip, uv, poetry, pdm, pipenv e il browser), ed esclude i download che dichiarano un ambiente di
integrazione continua e quelli da un Python più vecchio della 3.10, su cui il programma non gira (sono scanner).
Il nucleo nativo, `pyarchinit-photogrammetry-core`, ha una riga a parte. Finché BigQuery non è configurato e non
ha letto ogni giorno dal 1° ottobre 2026, la pagina mostra i numeri grezzi di
[pypistats.org](https://pypistats.org/packages/pyarchinit-photogrammetry), che escludono solo i mirror noti.

**Visitatori di questo sito.** [GoatCounter](https://www.goatcounter.com) conta un visitatore una volta per
pagina in una sessione di al massimo 8 ore, ed esclude i robot. Sono esclusi anche i browser dell'autore.

**Visitatori del repository.** GitHub conserva le visite del repository per 14 giorni; il workflow le somma
giorno per giorno. Una persona che torna in tre giorni conta tre volte. Le visite dell'autore non si possono
separare e sono comprese. I clone non sono mostrati: quasi tutti vengono dall'integrazione continua.

**Privacy.** Si raccolgono e si pubblicano solo totali e conteggi per paese, sistema, browser, versione e pagina.
GoatCounter non usa cookie e non conserva indirizzi IP; niente qui identifica una persona.
