# Glossario GNSS

I termini che l'interfaccia, i rapporti e la guida usano per le posizioni GNSS delle foto (RTK, PPK, PPP, basi,
stazioni permanenti, sistemi di riferimento, geoide), nell'uso italiano della geodesia e del rilievo (IGM, Rete
Dinamica Nazionale, manuali di topografia). Ogni termine ha la sua forma inglese, quella dell'interfaccia in
inglese. Le varianti nella colonna *Non usare* non compaiono nell'interfaccia: un test le cerca nei testi.

## Ricevitori e osservazioni

| Termine | Inglese | Che cosa è | Non usare |
| --- | --- | --- | --- |
| **base** | base | il ricevitore fermo su un punto di coordinate note (la tua base o una stazione permanente) durante il volo | stazione base, stazione di riferimento |
| **rover** | rover | il ricevitore in movimento: quello del drone | - |
| **stazione permanente** | permanent station (CORS) | un ricevitore fisso di una rete (EPN, IGS, RING, reti regionali) che pubblica le sue osservazioni | stazione CORS |
| **ricevitore** | receiver | lo strumento GNSS che registra le osservazioni | - |
| **antenna** | antenna | l'antenna del ricevitore; il suo tipo (e radome) è scritto nell'intestazione RINEX | - |
| **osservazioni** | observations | pseudodistanze e fasi registrate dal ricevitore (file RINEX di osservazione) | - |
| **epoca** | epoch | un istante di osservazione (le epoche di un file, a 1 s o a 30 s); anche l'istante a cui valgono delle coordinate (epoca di riferimento, 2026.82) | epoch |
| **effemeridi trasmesse** | broadcast ephemerides | orbite e orologi dei satelliti trasmessi dai satelliti stessi: i **file di navigazione** (`.nav`, `.##n`, `*_MN.rnx`) | effemeridi broadcast, file navigation |
| **prodotti precisi** | precise products | orbite e orologi precisi (e rotazione terrestre) calcolati dopo il volo (ESA, IGS), per il PPP | - |
| **dati grezzi** | raw data / raw log | il log del ricevitore nel suo formato (u-blox, RTCM 3, SBF...), da convertire in RINEX | log raw |
| **evento (marca di tempo)** | event (time mark) | l'istante di uno scatto registrato dal ricevitore | - |

## Soluzioni

| Termine | Inglese | Che cosa è | Non usare |
| --- | --- | --- | --- |
| **RTK** | RTK | posizionamento in tempo reale con le correzioni di una base | - |
| **PPK** | PPK | lo stesso calcolato dopo il volo, dalle osservazioni del rover e della base | - |
| **PPP** | PPP | posizionamento di precisione di un solo ricevitore con i prodotti precisi | - |
| **fix** | fix | soluzione con le ambiguità di fase fissate (intere): centimetrica | RTK fissato, posizione fissata, soluzione fissa |
| **float** | float | soluzione con le ambiguità non fissate: decimetrica | flottante |
| **singola** | single | posizione autonoma, senza correzioni: metrica | single |
| **linea di base** | baseline | il vettore tra due ricevitori; *linea di base statica*: la posizione della tua base calcolata da una stazione permanente | baseline (fuori dai nomi di comandi e cartelle) |
| **accuratezza** | accuracy | la deviazione standard stimata di una posizione (per asse) | - |

## Antenne e camera

| Termine | Inglese | Che cosa è | Non usare |
| --- | --- | --- | --- |
| **altezza dell'antenna** | antenna height | la distanza verticale dal punto a terra al punto di riferimento dell'antenna (ARP) | quota dell'antenna |
| **offset dell'antenna** | antenna offset | lo spostamento dell'ARP dal punto a terra (alto, est, nord: `ANTENNA: DELTA H/E/N` del RINEX) | scostamento dell'antenna |
| **offset del centro di fase (PCO)** | phase-centre offset | la distanza dall'ARP al centro di fase elettrico, dalle calibrazioni ANTEX (`igs20.atx`) | scostamento del centro di fase |
| **variazioni del centro di fase (PCV)** | phase-centre variations | le correzioni per elevazione e azimut delle calibrazioni | - |
| **calibrazione dell'antenna** | antenna calibration | la voce ANTEX dell'antenna (PCO e PCV) | - |
| **braccio di leva** | lever arm | lo spostamento dall'antenna del drone al centro della camera (avanti, destra, giù) | lever arm |
| **latenza** | latency | il ritardo tra il comando di scatto e l'esposizione | - |

## Sistemi di riferimento e quote

| Termine | Inglese | Che cosa è | Non usare |
| --- | --- | --- | --- |
| **sistema di riferimento** | reference frame | la realizzazione di un datum: ITRF2020, ETRF2000 (RDN2008 in Italia), WGS 84 | frame |
| **sistema di coordinate** | coordinate system | il sistema di riferimento con le sue coordinate o la sua proiezione: RDN2008 / UTM 33N (EPSG:7792) | - |
| **epoca delle coordinate** | coordinate epoch | l'istante a cui valgono le coordinate in un sistema che si muove con le placche (ITRF2020) | - |
| **quota ellissoidica (h)** | ellipsoidal height | la quota sull'ellissoide: quella che dà il GNSS | altezza ellissoidica |
| **quota ortometrica (H)** | orthometric height | la quota sul geoide, «sul livello del mare»: H = h - N | altezza ortometrica |
| **geoide** | geoid | la superficie equipotenziale di riferimento delle quote ortometriche; il modello usato è EGM2008 (griglia di PROJ) | - |
| **ondulazione del geoide (N)** | geoid undulation | la distanza tra geoide ed ellissoide (40-50 m in Italia) | separazione del geoide |

Gli acronimi PPK, RTK, PPP, RINEX, ANTEX, ARP, PCO, PCV, ITRF, ETRF, CORS restano come sono.
