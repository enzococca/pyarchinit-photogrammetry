# Benchmark

pyarchinit-photogrammetry, Agisoft Metashape Pro e ODM (il motore di WebODM) sulle stesse foto di un
tell, sullo stesso computer, con le stesse risoluzioni: il tempo di ogni passo e la qualità dei
risultati.

## Come

* **Computer**: Apple M4 Pro (8 core ad alte prestazioni e 4 ad alta efficienza), 24 GB, macOS 27.
  **Metashape usa la GPU (Metal) oltre alla CPU; pyarchinit-photogrammetry e ODM usano solo la
  CPU**: pyarchinit-photogrammetry nativo, con il suo nucleo nativo, ODM in Docker (una macchina
  virtuale Linux con 11 core e 8 GB).
* **Dati**: Tell Sultan (Siria), drone DJI con fotocamera FC8482 (4032 × 2268 pixel):
  le 57 foto del [tutorial](tutorials/tell.md) (circa 110 m dal suolo, 3,6 cm per pixel) e l'intero
  rilievo di 425 foto (voli verticali a circa 40, 65 e 110 m, e foto oblique).
* **Impostazioni**: punti cercati sulle foto a metà dimensione (Metashape precisione *Media*, ppg
  *Alta*, ODM `--feature-quality high`), mappe di profondità a un quarto (Metashape *Media*, ppg
  *Media*, ODM `--pc-quality medium`), filtro moderato, DEM dalla nuvola densa, ortofoto sul DEM,
  fusione a mosaico; DEM e ortofoto di ODM alle risoluzioni scelte da Metashape. Georeferenziazione con
  il GPS del drone: su Tell Sultan non ci sono punti di controllo.
* **Qualità**: i prodotti di ogni programma vengono portati su quelli di Metashape - uno spostamento
  trovato con la correlazione di fase delle ombreggiature, poi affinato insieme a offset, inclinazione e
  scala delle quote con l'adattamento ai minimi quadrati delle superfici: ogni programma colloca in modo
  un po' diverso un rilievo noto solo dal GPS del drone - e confrontati: NMAD e 95° percentile delle
  differenze di quota, terreno coperto, nitidezza delle ortofoto dove le coprono tutti. L'errore di
  riproiezione è la RMS su tutte le osservazioni dei punti di legame, in pixel delle foto.

Gli script sono nel repository (`benchmark/`): puoi rifarlo sui tuoi dati e sul tuo computer.

!!! note "GPU e CPU"
    Sul Mac Metashape esegue i passi più pesanti - l'abbinamento delle foto e le mappe di profondità -
    sulla GPU con Metal, oltre che sulla CPU; pyarchinit-photogrammetry e ODM fanno tutto sulla CPU.
    I tempi confrontano i programmi come girano su questo computer, non gli algoritmi sullo stesso
    hardware.

## 57 foto (il tutorial)

Tempi in secondi:

| Passo | pyarchinit-photogrammetry 1.1 (CPU) | Metashape Pro 2.1.3 (GPU + CPU) | ODM 3.6.2 (CPU) |
|---|---:|---:|---:|
| Allineamento | 177 | 18 | 119 |
| Mappe di profondità e nuvola densa | 128 | 79 | 170 |
| Mesh | 42 | 32 | 51 ¹ |
| Texture | 8 ⁶ | 19 | 54 |
| DEM | 5 | 4 | 3 |
| Ortofoto | 22 | 26 | 7 |
| **Totale** | **380** | **179** | **418** ² |

Risultati:

| | pyarchinit-photogrammetry | Metashape | ODM |
|---|---:|---:|---:|
| Foto allineate | 57 / 57 | 57 / 57 | 57 / 57 |
| Punti di legame | 316.000 | 65.000 | 55.000 |
| Errore di riproiezione (RMS) | 0,97 px | 1,05 px | - |
| Nuvola densa | 3,3 M punti | 4,2 M | 6,1 M |
| DEM | 19 cm | 15 cm | 15 cm |
| DEM rispetto a quello di Metashape: NMAD / 95° percentile | 15,3 / 68 cm | - | 14,4 / 74 cm |
| Terreno coperto dal DEM ³ | 105 % | 100 % | 100 % |
| Ortofoto | 3,6 cm | 3,8 cm | 3,8 cm |
| Nitidezza dell'ortofoto ⁴ | 58,5 | 52,3 | 35,1 |
| Terreno coperto dall'ortofoto ⁵ | 66 % | 75 % | 86 % |

¹ la mesh 2,5D ha esaurito la memoria una volta ed è stata rifatta con meno processi.
² con la georeferenziazione e il rapporto.
³ celle della griglia comune con una quota, DEM di Metashape = 100 %.
⁴ varianza del laplaciano dell'immagine in grigi a 2 cm, mediana dei tasselli coperti da tutti.
⁵ quota dei tasselli dell'impronta comune; ODM riempie fino ai bordi del volo.
⁶ senza la correzione del colore delle foto, come Metashape senza la sua calibrazione dei colori (con,
come nella finestra: 27 s).

![Ombreggiature dei tre DEM, 57 foto](img/benchmark/sultan57-hillshades.png)

Note:

* Metashape cerca i punti e allinea sulla GPU: 18 s, contro 119 s di ODM e 177 s di ppg sulla
  CPU.
* ppg calcola le mappe di profondità di un rilievo aereo con il PatchMatch del suo nucleo nativo:
  niente gradini a forma di curve di livello e bande, che lo stereo a coppie disegnava su questo tell.
* L'ortofoto di ODM è la più morbida: è proiettata su una mesh 2,5D semplificata a 400.000 facce; il suo
  DSM riempie i vuoti con toppe a lati dritti (visibili a nord).
* La linea che attraversa il nord-ovest dell'ombreggiatura di ppg è un gradino fino a 30-40 cm fra
  terreno visto da strisciate di volo diverse (vedi le note sulle 425 foto).
* L'ortofoto di ppg è la più nitida: per ogni punto prende la foto che lo vede alla risoluzione più
  alta.

## 425 foto (l'intero rilievo)

Tempi in secondi:

| Passo | pyarchinit-photogrammetry 1.1 (CPU) | Metashape Pro 2.1.3 (GPU + CPU) | ODM 3.6.2 (CPU) |
|---|---:|---:|---:|
| Allineamento | 1.634 | 205 | 903 |
| Mappe di profondità e nuvola densa | 1.118 | 872 | ⁷ |
| Mesh | 197 | 278 | |
| Texture | 35 | 131 | |
| DEM | 77 | 16 | |
| Ortofoto | 115 | 146 | |
| **Totale** | **3.178** (53 min) | **1.648** (27 min) | non completato ⁷ |

⁷ nella sua macchina virtuale da 8 GB ODM ha esaurito la memoria mentre univa le mappe di profondità
delle 425 foto, anche a tasselli, e si è fermato senza nuvola densa.

Risultati:

| | pyarchinit-photogrammetry | Metashape |
|---|---:|---:|
| Foto allineate | 425 / 425 | 425 / 425 |
| Coppie di foto abbinate | 6.069 | - |
| Punti di legame | 1.985.000 | 556.000 |
| Errore di riproiezione (RMS) | 1,21 px | 1,34 px |
| Nuvola densa | 24,1 M punti | 15,5 M |
| Mesh | 1,61 M facce | 1,24 M |
| DEM | 7,0 cm | 8,2 cm |
| DEM rispetto a quello di Metashape: NMAD / 95° percentile | 11,3 / 39 cm | - |
| Terreno coperto dal DEM ³ | 101 % | 100 % |
| Ortofoto | 2,0 cm | 2,0 cm |
| Nitidezza dell'ortofoto ⁴ | 658 | 339 |

![Ombreggiature dei DEM, 425 foto](img/benchmark/sultan425-hillshades.png)

Note:

* L'allineamento è dove ppg resta molto indietro: 6.069 coppie di foto abbinate sulla CPU con
  40.000 punti per foto, poi tutte le camere collocate insieme; Metashape abbina sulla GPU. Dopo
  l'allineamento i due sono vicini: 1.543 s per ppg, 1.443 s per Metashape.
* La nuvola densa di ppg tiene i punti visti da almeno tre foto: ai margini dei voli è rada e lì il DEM
  è interpolato (più liscio di quello di Metashape), mentre è molto più densa dove si sovrappongono
  decine di foto (i voli bassi sopra il centro).
* Collocato ogni rilievo sull'altro, i due DEM concordano entro 11,3 cm (NMAD), ma quello di ppg ha
  ampi scostamenti di 10-30 cm che seguono le strisciate di volo: le mappe di profondità di foto di
  strisciate diverse differiscono in media di circa 10 cm, in alcuni punti di 30-40 cm, e dove cambiano
  le foto che vedono il terreno il DEM fa un gradino (anche nell'ombreggiatura delle 57 foto, a
  nord-ovest). Vengono dalle mappe di profondità di ppg, non dal suo allineamento: calcolate
  dall'allineamento di Metashape differiscono allo stesso modo. È in corso di studio; il benchmark su
  Tell Tuqan, che ha i punti di controllo a terra, misurerà anche l'accuratezza assoluta dei due
  programmi.

## Cosa è cambiato in pyarchinit-photogrammetry

Queste prove hanno cambiato il programma; i numeri qui sopra sono quelli della versione con le modifiche:

* allineamento di molte foto: le coppie che si sovrappongono trovate prima su foto ridotte a 1024
  pixel, le 20 più forti di ogni foto abbinate per intero, tutte le camere collocate insieme
  (allineamento globale): 57 foto 472 → 177 s, 425 foto 42 → 27 min, le stesse
  camere entro 1 cm (RMS);
* mappe di profondità dei rilievi aerei con PatchMatch e una fusione che mantiene la densità delle mappe:
  57 foto da 1,1 a 3,3 M punti, niente gradini a forma di curve di livello;
* DEM: risoluzione dal terreno che la nuvola copre; picchi e punti spuri tolti; i margini radi
  interpolati come una superficie unica invece che a puntini: 425 foto, dal 45 al 64 % del
  terreno comune, 95° percentile delle differenze da 87 a 39 cm.
