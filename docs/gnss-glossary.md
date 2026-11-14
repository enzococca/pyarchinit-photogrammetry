# GNSS glossary

The terms the interface, the reports and the guide use for the GNSS positions of the photos (RTK, PPK, PPP, bases,
permanent stations, reference frames, geoid), with the Italian term of the Italian interface - the usage of Italian
geodesy and surveying (IGM, Rete Dinamica Nazionale, surveying handbooks). The variants in the column *Not used* do
not appear in the Italian interface: a test looks for them.

## Receivers and observations

| Term | Italian | What it is | Not used (Italian) |
| --- | --- | --- | --- |
| **base** | base | the receiver standing on a point of known coordinates during the flight (your base or a permanent station) | stazione base, stazione di riferimento |
| **rover** | rover | the moving receiver: the drone's | - |
| **permanent station (CORS)** | stazione permanente | a fixed receiver of a network (EPN, IGS, RING, regional networks) that publishes its observations | stazione CORS |
| **receiver** | ricevitore | the GNSS instrument that records the observations | - |
| **antenna** | antenna | the antenna of the receiver; its type (and radome) is in the RINEX header | - |
| **observations** | osservazioni | pseudoranges and phases recorded by the receiver (RINEX observation files) | - |
| **epoch** | epoca | an instant of observation (the epochs of a file, at 1 s or 30 s); also the instant coordinates hold at (2026.82) | epoch |
| **broadcast ephemerides** | effemeridi trasmesse | orbits and clocks sent by the satellites themselves: the **navigation files** (`.nav`, `.##n`, `*_MN.rnx`) | effemeridi broadcast |
| **precise products** | prodotti precisi | precise orbits and clocks (and Earth rotation) computed after the flight (ESA, IGS), for PPP | - |
| **raw data** | dati grezzi | the receiver's log in its own format (u-blox, RTCM 3, SBF...), converted to RINEX | log raw |
| **event (time mark)** | evento (marca di tempo) | the instant of a shot recorded by the receiver | - |

## Solutions

| Term | Italian | What it is | Not used (Italian) |
| --- | --- | --- | --- |
| **RTK** | RTK | real-time positioning with the corrections of a base | - |
| **PPK** | PPK | the same computed after the flight, from the observations of rover and base | - |
| **PPP** | PPP | precise positioning of a single receiver with precise products | - |
| **fix** | fix | solution with the phase ambiguities fixed (integer): centimetres | RTK fissato, posizione fissata |
| **float** | float | solution with the ambiguities not fixed: decimetres | flottante |
| **single** | singola | autonomous position, no corrections: metres | single |
| **baseline** | linea di base | the vector between two receivers; *static baseline*: the position of your base computed from a permanent station | baseline (outside command and folder names) |
| **accuracy** | accuratezza | the estimated standard deviation of a position (per axis) | - |

## Antennas and camera

| Term | Italian | What it is | Not used (Italian) |
| --- | --- | --- | --- |
| **antenna height** | altezza dell'antenna | the vertical distance from the ground mark to the antenna reference point (ARP) | quota dell'antenna |
| **antenna offset** | offset dell'antenna | the offset of the ARP from the ground mark (up, east, north: `ANTENNA: DELTA H/E/N` of RINEX) | scostamento dell'antenna |
| **phase-centre offset (PCO)** | offset del centro di fase | from the ARP to the electrical phase centre, from the ANTEX calibrations (`igs20.atx`) | scostamento del centro di fase |
| **phase-centre variations (PCV)** | variazioni del centro di fase | the corrections by elevation and azimuth of the calibrations | - |
| **antenna calibration** | calibrazione dell'antenna | the ANTEX entry of the antenna (PCO and PCV) | - |
| **lever arm** | braccio di leva | from the drone's antenna to the camera centre (forward, right, down) | lever arm |
| **latency** | latenza | the delay between the shutter command and the exposure | - |

## Reference frames and heights

| Term | Italian | What it is | Not used (Italian) |
| --- | --- | --- | --- |
| **reference frame** | sistema di riferimento | the realisation of a datum: ITRF2020, ETRF2000 (RDN2008 in Italy), WGS 84 | frame |
| **coordinate system** | sistema di coordinate | a frame with its coordinates or projection: RDN2008 / UTM 33N (EPSG:7792) | - |
| **coordinate epoch** | epoca delle coordinate | the instant coordinates hold at in a frame that moves with the plates (ITRF2020) | - |
| **ellipsoidal height (h)** | quota ellissoidica | the height above the ellipsoid: what GNSS gives | altezza ellissoidica |
| **orthometric height (H)** | quota ortometrica | the height above the geoid, "above sea level": H = h - N | altezza ortometrica |
| **geoid** | geoide | the reference surface of orthometric heights; the model used is EGM2008 (PROJ grid) | - |
| **geoid undulation (N)** | ondulazione del geoide | the distance between geoid and ellipsoid (40-50 m in Italy) | separazione del geoide |

The acronyms PPK, RTK, PPP, RINEX, ANTEX, ARP, PCO, PCV, ITRF, ETRF, CORS stay as they are.
