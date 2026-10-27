# Test data for the permanent GNSS stations

Small excerpts of what the public services give, for `tests/test_gnss_stations.py`. The tests never use the network:
they point the URL constants of `gnss/stations.py` at these files (`file://`).

Captured on 2026-10-27, between 07:12 and 07:23 UTC, with `curl` and no account, from the services named below. Each
excerpt keeps the format of the service and the real values; what was cut is said in the table.

| File | What it is | Where it comes from | Licence |
|---|---|---|---|
| `m3g_EPN.geojson` | 5 of the 520 stations of the EPN: M0SE00ITA, ASIR00ITA, AQUI00ITA, MEDI00ITA and PADO00ITA (removed on 2025-04-30). The objects of the service, in its order, one per line | `https://gnss-metadata.eu/v1/sitelog/geojson?network=EPN&pagination=0` (M3G, the metadata system of EUREF and the EPN) | CC BY 4.0 (the `license` of each station: 510 of the 520 are CC BY 4.0, 10 are CC0); DOI of the station `10.24414/ROB-EUREF-<id>` |
| `m3g_RING.geojson` | 4 of the 258 stations of RING: INGR00ITA, RMPO00ITA, MARN00ITA, ZNTE00GRC | `https://gnss-metadata.eu/v1/sitelog/geojson?network=RING&pagination=0` | CC BY 4.0; DOI `10.13127/RING` |
| `IGSNetwork.json` | 3 of the 535 stations of the IGS network: M0SE00ITA, MEDI00ITA, BSHM00ISR. The entries as the service writes them | `https://files.igs.org/pub/station/general/IGSNetwork.json` | open data of the IGS |
| `IGc20.ssc` | The IGc20 reference frame (the IGS realisation of ITRF2020-u2024), cut to the stations M0SE, MEDI and BSHM: the comment block, `SITE/ID`, `SOLUTION/EPOCHS` and `SOLUTION/ESTIMATE` of those three, and the end line. The first line is the real one (it still counts the parameters of the whole file) | `https://files.igs.org/pub/station/coord/IGc20/IGc20.ssc` | open data of the IGS |
| `INGR00ITA.gipsyX.IGS20.pos` | The daily positions of the RING station INGR00ITA in IGS20 (GipsyX): the whole header and the last 150 of 8955 rows (2025-09-14 to 2026-04-04) | `https://webring.gm.ingv.it:44324/products/POS/IGS2/INGR00ITA/INGR00ITA.gipsyX.IGS20.pos` | CC BY 4.0, INGV RING, DOI `10.13127/gnss/gipos` (it is in the header) |
| `index_igs_IGc20.html` | The page that lists the folder IGc20 (Apache: relative links, sort links with `?C=N;O=D`, the link to the parent folder and one to another site). Whole | `https://files.igs.org/pub/station/coord/IGc20/` | - |
| `index_ring_IGS2.html` | The page that lists `products/POS/IGS2` of RING (IIS: links as paths from the root, no slash after a folder, links to the parent folders and to assets). The head and the end of the page are whole, 4 of the 2958 entries are kept: INGP00ITA, INGR00ITA, INSA00FRA, INVE00GBR | `https://webring.gm.ingv.it:44324/products/POS/IGS2/` | - |
| `EPN_synthetic.SSC` | **Not a capture. Made up.** The EPN multi-year solution in the layout of `IGc20.ssc` | see below | - |

## The EPN multi-year solution is not here

The EPN Central Bureau (`epncb.oma.be`, which answers to 193.190.230.11) did not answer on 2026-10-27: ports 21, 80 and
443 all timed out, from the same Mac that reached M3G and the IGS minutes before and RING minutes after. The research note
(`design/2026-10-26-gnss-phase3-research.md`) had the same problem the day before and took the names from copies of
the pages kept by the Wayback Machine.

`EPN_synthetic.SSC` stands in for `EUR0OPSSNX_1996001_2025270_00U_SOL.SSC`, the version in IGS20 (no frame in the
name) of the EPN multi-year position and velocity solution. It was written by a script, not copied from the EPN:

- the layout is the one of the real `IGc20.ssc` above (a SINEX 2.02 file: `SOLUTION/EPOCHS` with `DATA_START` and
  `DATA_END`, `SOLUTION/ESTIMATE` with `STAX`..`VELZ` at a reference epoch), because the IGS names `.ssc` the files of
  that kind (`ITRF96_IGS_RS47.SSC` of 1998, in the same tree, is also a SINEX file) and the `IGc20.snx.gz` next to it
  is 405 MB while the `.ssc` is 1.3 MB; whether the `.SSC` of the EPN has the same layout is **not verified**;
- the numbers are made up: the approximate positions of the site logs (`m3g_EPN.geojson`) moved by a few centimetres,
  a reference epoch of 2015.0 (the IGS file has 2020.0, so that the tests see both), velocities close to the real ones
  of the stations of Rome, and one discontinuity (M0SE, a second solution from 2023 day 152);
- the file says so in its comment block.

The names of the files and folders of the EPN that the tests build (`C2399/`, `C2400/`,
`EUR0OPSSNX_1996001_2025270_00U_SOL[_ETRF2000|_ETRF2014|_ETRF2020|_short].SSC`) come from the research note, not from
a listing seen today.

## What the live services return (and the research note did not say)

- M3G: the "GeoJSON" is a JSON **array** of Feature objects, not a FeatureCollection. The station is
  `properties.id` (9 upper case characters); the approximate position is in `properties.location`:
  `approximatePositionITRF` (`X`, `Y`, `Z`; some of them integers) and `approximatePositionGRS80` (`latitude`,
  `longitude`, `elevation`), which agree to a millimetre. 95 of the 520 stations of the EPN have
  `properties.siteForm.dateRemoved` (the ex-stations, the latest on 2026-10-20); none of RING. No station is in both the
  EPN and the RING lists. `siteForm.siteName` is sometimes the id again; `location.location` is the town.
- `IGSNetwork.json`: an object keyed by the 9-character id. `X`, `Y`, `Z` are numbers; `Latitude`, `Longitude` and
  `Height` are strings with three decimals (up to 74 m from the XYZ, so the program derives latitude, longitude
  and height from the XYZ). There is no name.
- `IGc20.ssc`: SINEX 2.02 with the 4-character code of the station (`M0SE`), the epochs as `YY:DDD:SSSSS` and the
  positions at 2020.0 with a velocity per solution; the last solution of each of the three stations ends in March
  2025 (the epoch of the data is later: the positions are extrapolated).
- The RING `.pos` files have 25 columns; the position is `X Y Z` in columns 4 to 6 and the epoch is the date and
  the modified Julian day in columns 1 to 3; the first line names the frame (`Reference Frame : IGS20`).
