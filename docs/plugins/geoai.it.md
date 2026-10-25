# GeoAI

GeoAI 0.2 impara classi di pixel dai **disegni US verificati** e applica il modello ad altri rilievi.
L'ortofoto RGB rimane l'impostazione iniziale. Come opzione puoi usare DEM, bande multispettrali,
termiche o altri raster numerici, da soli o combinati con l'RGB.
Produce proposte da controllare: elimina i poligoni errati nella vista, poi trasforma quelli rimasti
o selezionati in US oppure esportali in GeoPackage. Non deduce i rapporti stratigrafici.

Richiede pyarchinit-photogrammetry 1.5.0. GeoAI si installa autonomamente; quando è presente anche
SamGeo, i due plugin condividono `geoai-env` preservando le funzionalità già installate.
PyTorch e GeoAI girano in un processo separato dal programma.
`Plugin > GeoAI > Installa GeoAI...` installa le dipendenze dei modelli.
È consigliata una GPU; sul processore sono adatte solo piccole prove. CUDA e Metal dipendono dalle
versioni installate di PyTorch e GeoAI. Non servono i pesi SAM 3 né il relativo token.

## Nel programma

1. Apri `Plugin > GeoAI > Pannello GeoAI`, con ortofoto e disegni US controllati.
2. Scegli il campo delle US. Nella tabella assegna ogni valore a una classe e a un tipo US (`US`,
   `USM`...). Lo stesso nome raggruppa più valori; una classe vuota esclude quel valore.
3. **Disegna area verificata**: delimita soltanto l'area completamente annotata. Il terreno senza
   US dentro quell'area diventa sfondo. Fuori dall'area, nelle US escluse, nelle sovrapposizioni con
   classi contraddittorie e nei pixel nodata, i pixel restano sconosciuti e sono ignorati.
4. **Prepara dataset...** crea una nuova cartella. Nell'uso RGB si leggono le prime tre bande uint8
   dell'ortofoto; per altri dati attiva l'opzione degli input raster descritta sotto. Il riferimento
   deve avere pixel quadrati e griglia in metri orientata a nord. I tasselli sono di 512 px; per
   l'addestramento U-Net reale occorrono almeno 64 px. L'area deve
   coprire almeno quattro colonne di tasselli: la parte destra è riservata alla validazione e una
   colonna intera separa i due insiemi. Ogni classe e lo sfondo devono comparire in entrambi.
5. **Addestra modello...** sceglie il dataset e una nuova cartella, per esempio `muri.ppgseg`. La
   U-Net/ResNet-34 di GeoAI usa un canale per ogni banda selezionata. Nell'uso RGB parte dall'encoder
   ImageNet e normalizza con uint8/255. Gli input numerici opzionali partono senza pesi ImageNet e
   usano media e deviazione standard calcolate soltanto sui pixel etichettati di addestramento.
   Alla fine sono disponibili IoU per classe e matrice di confusione. Si conserva il modello con
   la migliore IoU media sulla validazione: quella misura serve a scegliere il modello, non è una
   prova finale indipendente.
6. **Applica modello...** usa il modello nel chunk attivo, alla stessa risoluzione del dataset.
   Per un modello con input opzionali scegli i raster del nuovo rilievo: ordine, nomi, bande e unità
   devono corrispondere a quelli del modello. Se serve, ricampiona prima il riferimento. Produce un GeoTIFF delle
   classi e un GeoPackage di proposte, visibili in azzurro.
7. Controlla le proposte con Seleziona/Canc. **US** converte quelle selezionate o tutte quelle
   rimaste se non c'è selezione; le conversioni già presenti non sono duplicate e Annulla permette
   di ripeterle. **GeoPackage...** esporta le proposte corrette. Il raster mantiene la classificazione
   originale del modello anche quando elimini proposte vettoriali.

La cartella `.ppgseg` contiene `model.json`, `weights.pt` e `history.json`: trasferiscila intera.
Le classi, i tipi US, la risoluzione, la normalizzazione, la divisione spaziale, le versioni dei
pacchetti e le metriche accompagnano il modello. Un dataset o addestramento fallito non riceve il
manifest pronto; ripeti in una nuova cartella. Annullare ferma il processo, ma può lasciare file
parziali da ispezionare.

## DEM, multispettrale e termico opzionali

Attiva **Raster opzionali: DEM, multispettrale, termico** e apri **Scegli raster e bande...**. Aggiungi i raster del chunk
oppure GeoTIFF esterni. Per ogni sorgente indica nome, tipo, bande (numerate da 1), ricampionamento e
unità fisica, quando nota. La prima sorgente definisce la griglia di riferimento; deve essere nello
stesso sistema di coordinate dei disegni US del chunk. Puoi scegliere anche solo un DEM o un raster
termico, senza RGB. Per disegnare l'area verificata occorre una mappa visibile nella vista.

Esempi: RGB + DEM per aspetto e quota; multispettrale con le bande di riflettanza; termico con i valori
radiometrici o le temperature. I tipi servono a descrivere i canali: non calcolano automaticamente
NDVI, pendenza o altri indici, che puoi preparare nel calcolatore raster e aggiungere come sorgenti.
Sono ammessi fino a 64 canali.

Le sorgenti aggiuntive vengono riproiettate/ricampionate in lettura sulla griglia scelta, senza
salvare una copia completa dello stack. Un raster con CRS noto non viene abbinato a uno senza CRS;
se entrambi ne sono privi devono già condividere le stesse coordinate locali in metri. Il
ricampionamento allinea griglie già georiferite e non corregge errori di registrazione del rilievo.
Se anche una sola banda ha nodata o valori non finiti, quel pixel viene escluso. Si applicano scale
e offset registrati nel GeoTIFF; il plugin non calibra sensori né converte automaticamente Kelvin
in gradi Celsius. Mantieni la stessa calibrazione e le stesse unità in addestramento e applicazione.

Il modello conserva lo schema dei canali e le statistiche di addestramento: nell'applicazione non
ricalcola le statistiche dal nuovo rilievo e non riutilizza automaticamente i file del progetto di
addestramento. I modelli e dataset RGB della versione 0.1 restano leggibili.

## Riga di comando

```sh
ppg plugin geoai install
ppg plugin geoai status
ppg plugin geoai prepare scavo.ppg classi.json dataset
ppg plugin geoai train dataset muri.ppgseg --epochs 20 --batch-size 2 --device cuda
ppg plugin geoai apply altro.ppg muri.ppgseg --min-area 0.01
ppg plugin geoai apply altro.ppg muri-multibanda.ppgseg --inputs sorgenti-altro.json
ppg plugin geoai export altro.ppg ID_RICERCA proposte.gpkg
```

Esempio di `classi.json`, con l'area da sostituire con quella verificata nelle coordinate del chunk:

```json
{
  "field": "tipo",
  "mapping": {"USM": "muratura", "US": "terreno"},
  "kinds": {"muratura": "USM", "terreno": "US"},
  "reviewed_area": "POLYGON ((0 0, 50 0, 50 50, 0 50, 0 0))",
  "tile_size": 512,
  "validation_fraction": 0.2
}
```

Per gli input opzionali aggiungi `reference` e `inputs` al JSON di preparazione. Esempio della parte
riutilizzabile in `sorgenti-altro.json` per l'applicazione (sostituisci i percorsi con quelli del nuovo rilievo):

```json
{
  "reference": "/rilievo/ortofoto.tif",
  "inputs": [
    {"name": "colore", "kind": "rgb", "path": "/rilievo/ortofoto.tif", "bands": [1, 2, 3], "resampling": "bilinear"},
    {"name": "quota", "kind": "dem", "path": "/rilievo/dem.tif", "bands": [1], "resampling": "bilinear", "unit": "m"},
    {"name": "temperatura", "kind": "thermal", "path": "/rilievo/termico.tif", "bands": [1], "resampling": "bilinear", "unit": "Celsius"}
  ]
}
```

## Ambito della prima versione

Un chunk per dataset. Dataset da più chunk e confronto fra date sono passi successivi. Il confronto
temporale previsto userà due rilievi registrati sulla stessa griglia e, quando presenti, le differenze
di quota dei DEM; non fa parte della versione 0.2. La separazione spaziale limita la contaminazione fra addestramento e validazione;
verifica comunque il modello su un rilievo indipendente. Una classe descrive un'ipotesi di materiale
o aspetto, non l'interpretazione stratigrafica.

I test sintetici verificano etichette, aree sconosciute, nodata, separazione spaziale, processo
isolato, addestramento/applicazione simulati, poligoni alle giunzioni e attributi delle US. Il motore
`PPG_GEOAI_FAKE=1` serve esclusivamente ai test e i suoi modelli sono rifiutati nell'uso normale.
Addestramento GPU reale e qualità archeologica devono essere verificati con dati reali.
Una prova reale U-Net a quattro canali su dati sintetici ha verificato addestramento e applicazione
su CPU; non misura l'accuratezza su un rilievo archeologico.

[API GeoAI](https://opengeoai.org/train/). Codice del plugin GPL-2.0-or-later; le dipendenze e i pesi
preaddestrati conservano le rispettive licenze.
