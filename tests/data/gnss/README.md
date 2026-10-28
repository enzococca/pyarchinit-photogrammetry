# Test data for the permanent GNSS stations

Small excerpts of what the public services give, for `tests/test_gnss_stations.py`. The tests never use the network:
they point the URL constants of `gnss/stations.py` at these files (`file://`).

Captured on 2026-10-27, between 07:12 and 07:23 UTC (`psd_IGc20.snx` and `soln_IGc20.snx` at 08:17 UTC, the two files
of the EPN at 22:28 UTC), with `curl` and no account, from the services named below. Each
excerpt keeps the format of the service and the real values; what was cut is said in the table.

| File | What it is | Where it comes from | Licence |
|---|---|---|---|
| `m3g_EPN.geojson` | 6 of the 520 stations of the EPN: M0SE00ITA, ASIR00ITA, AQUI00ITA, MEDI00ITA, DRAG00ISR and PADO00ITA (removed on 2025-04-30). The objects of the service, in its order, one per line | `https://gnss-metadata.eu/v1/sitelog/geojson?network=EPN&pagination=0` (M3G, the metadata system of EUREF and the EPN) | CC BY 4.0 (the `license` of each station: 510 of the 520 are CC BY 4.0, 10 are CC0); DOI of the station `10.24414/ROB-EUREF-<id>` |
| `m3g_RING.geojson` | 4 of the 258 stations of RING: INGR00ITA, RMPO00ITA, MARN00ITA, ZNTE00GRC | `https://gnss-metadata.eu/v1/sitelog/geojson?network=RING&pagination=0` | CC BY 4.0; DOI `10.13127/RING` |
| `IGSNetwork.json` | 5 of the 535 stations of the IGS network: M0SE00ITA, MEDI00ITA, BSHM00ISR, DRAG00ISR, ADIS00ETH. The entries as the service writes them | `https://files.igs.org/pub/station/general/IGSNetwork.json` | open data of the IGS |
| `IGc20.ssc` | The IGc20 reference frame (the IGS realisation of ITRF2020-u2024), cut to the stations M0SE, MEDI, BSHM, DRAG and ADIS: the comment block, `SITE/ID`, `SOLUTION/EPOCHS` and `SOLUTION/ESTIMATE` of those five, and the end line. The first line is the real one (it still counts the parameters of the whole file) | `https://files.igs.org/pub/station/coord/IGc20/IGc20.ssc` | open data of the IGS |
| `psd_IGc20.snx` | The post-seismic deformation models that go with IGc20 (a SINEX file: `ALOG_E`, `TLOG_E`, `AEXP_N`, `TEXP_N`... for 44 stations, the reference epoch of each being the earthquake), cut to DRAG, ISTA and TUBI (the earthquakes of Izmit 1999 and Kahramanmaras 2023): the first line is the real one (it still counts the parameters of the whole file), the comment block, `SITE/ID` and `SOLUTION/ESTIMATE` of those three, and the end line; the covariance matrix is left out | `https://files.igs.org/pub/station/coord/IGc20/psd_IGc20.snx` | open data of the IGS |
| `soln_IGc20.snx` | The list of discontinuities of IGc20 (SINEX 2.01, one `SOLUTION/DISCONTINUITY` block), kept up to date as breaks come: it gives the validity period of every solution, which the `.ssc` does not. Cut to M0SE, MEDI, BSHM, DRAG and ADIS: all their rows (breaks of the position, event `P`, and of the velocity, event `V`) | `https://files.igs.org/pub/station/coord/IGc20/soln_IGc20.snx` | open data of the IGS |
| `INGR00ITA.gipsyX.IGS20.pos` | The daily positions of the RING station INGR00ITA in IGS20 (GipsyX): the whole header and the last 150 of 8955 rows (2025-09-14 to 2026-04-04) | `https://webring.gm.ingv.it:44324/products/POS/IGS2/INGR00ITA/INGR00ITA.gipsyX.IGS20.pos` | CC BY 4.0, INGV RING, DOI `10.13127/gnss/gipos` (it is in the header) |
| `index_igs_IGc20.html` | The page that lists the folder IGc20 (Apache: relative links, sort links with `?C=N;O=D`, the link to the parent folder and one to another site). Whole | `https://files.igs.org/pub/station/coord/IGc20/` | - |
| `index_ring_IGS2.html` | The page that lists `products/POS/IGS2` of RING (IIS: links as paths from the root, no slash after a folder, links to the parent folders and to assets). The head and the end of the page are whole, 4 of the 2958 entries are kept: INGP00ITA, INGR00ITA, INSA00FRA, INVE00GBR | `https://webring.gm.ingv.it:44324/products/POS/IGS2/` | - |
| `EUR0OPSSNX_1996001_2026115_00U_SOL.SSC` | The multi-year position and velocity solution of the EPN in IGS20, release C2415 (GPS weeks 834 to 2415, data to 2026 day 115), in the columns of the Central Bureau (not SINEX): its title and the names of its columns (the first 8 lines), then the lines of MEDI00ITA (2 solutions), AQUI00ITA (4), M0SE00ITA (5) and ASIR00ITA (1), each solution a line of the position and one of the velocity, in the order of the file, as they are. 483 stations in the real file (368,994 bytes) | `https://epncb.oma.be/pub/product/referenceframe/C2415/EUR0OPSSNX_1996001_2026115_00U_SOL.SSC` | CC BY 4.0, EPN Central Bureau (Royal Observatory of Belgium), DOI `10.24414/ROB-EUREF-C2415` |
| `EPN_discontinuities.snx` | The catalogue of discontinuities that goes with it (SINEX 1.00, one `SOLUTION/DISCONTINUITY` block, the layout of `soln_IGc20.snx`): the validity of every solution. Its first three lines and its last three, and all the rows (position, event `P`, and velocity, event `V`) of AQUI, ASIR, M0SE and MEDI, as they are. The real file (117,025 bytes) is Latin-1 (names such as Düzce in its comments); the excerpt is ASCII | `https://epncb.oma.be/pub/product/referenceframe/C2415/EPN_discontinuities.snx` | CC BY 4.0, as above |
| `EPN_synthetic.SSC` | **Not a capture. Made up.** An EPN multi-year solution in SINEX, the layout of `IGc20.ssc`: for the program's way of reading a solution in SINEX | see below | - |

## The EPN multi-year solution

The EPN Central Bureau (`epncb.oma.be`) did not answer in the morning of 2026-10-27 (ports 21, 80 and 443 timed out),
so the first version of the program read its solution as SINEX, like `IGc20.ssc`, on the synthetic file below. It
answered in the evening, and its solution is **not SINEX**: `EUR0OPSSNX_1996001_2026115_00U_SOL.SSC` above, in the
columns that the README of the folder calls the SSC format. The folder `C2415/` (the newest of `C2370/`, `C2385/`,
`C2400/`, `C2415/`, besides `archive/` and `latest/`) holds the solution in IGS20 (no frame in the name), the same in
ETRF2000, ETRF2014 and ETRF2020 (`_ETRF2000` ... before `.SSC`), the positions of the stations with less than three
years in a `_short.SSC` of each frame, the SINEX of each frame (`.SNX.gz`, 350 MB), `.CRD` and `.VEL` files,
`EPN_discontinuities.snx`, `EPN_outliers.lst` and plots. The SSC holds only "stations with more than 3 years and
without post-seismic deformation"; its classes A/B are no longer marked (the column `CLASS` of the title is empty).

`EPN_synthetic.SSC` stays for the program's way of reading a solution in SINEX (a file that begins with `%=SNX`),
which the tests keep. It stands in for `EUR0OPSSNX_1996001_2025270_00U_SOL.SSC` and was written by a script, not copied
from the EPN:

- the layout is the one of the real `IGc20.ssc` above (a SINEX 2.02 file: `SOLUTION/EPOCHS` with `DATA_START` and
  `DATA_END`, `SOLUTION/ESTIMATE` with `STAX`..`VELZ` at a reference epoch), which the live `.SSC` of the EPN does not
  have;
- the numbers are made up: the approximate positions of the site logs (`m3g_EPN.geojson`) moved by a few centimetres,
  a reference epoch of 2015.0 (the IGS file has 2020.0, so that the tests see both), velocities close to the real ones
  of the stations of Rome, and one discontinuity (M0SE, a second solution from 2023 day 152);
- the file says so in its comment block.

The names of the files and folders of the EPN that the tests build for the SINEX file (`C2399/`, `C2400/`,
`EUR0OPSSNX_1996001_2025270_00U_SOL[_ETRF2000|_ETRF2014|_ETRF2020|_short].SSC`) come from the research note; the
listing of `C2415/` seen on 2026-10-27 has the same pattern of names (`_SOL_ETRF2000.SSC`, `_SOL_short.SSC`), and
the tests of the files in columns put the two excerpts in a folder `C2415/`.

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
  positions at 2020.0 with a velocity per solution. The data of the last solution of M0SE, MEDI, BSHM and DRAG end in
  March 2025 (a later epoch is extrapolated along the line of that solution); the data of ADIS end in December 2024
  and the file has no coordinates for its solutions 4 to 7, the breaks that came after (the header says that IGc20
  has none for solutions that start after 15 March 2025, and that stations with post-seismic models cannot be used
  without them). 46 of the 346 stations are in that case.
- `soln_IGc20.snx`: `CODE PT SOLN T START END EVENT comment` for each solution, `EVENT` being `P` (position) or `V`
  (velocity), with their own numbers; `00:000:00000` is no limit. The numbers of `P` are those of `IGc20.ssc`, and
  every station of the `.ssc` is in it. The solution that is valid at an epoch is the one that has it between its
  start and its end, and the last one is open.
- `psd_IGc20.snx`: the `SOLUTION/ESTIMATE` rows are `ALOG_E`, `TLOG_E` (a logarithmic term: amplitude in m, time in y),
  `AEXP_N`, `TEXP_N` (an exponential one) and the same for `_U`, for a station and an earthquake, with the epoch of
  the earthquake as reference epoch and `----` as solution.
- The RING `.pos` files have 25 columns; the position is `X Y Z` in columns 4 to 6 and the epoch is the date and
  the modified Julian day in columns 1 to 3; the first line names the frame (`Reference Frame : IGS20`).
- The SSC of the EPN: five lines of title (the frame, `REFERENCE FRAME:    IGS20    AT EPOCH OF 2020.0`, the GPS
  weeks, the release name `EPN_IGS20_C2415`, the date), the names of the columns, a line of `CLASS` and one of dashes;
  then for each solution of a station `DOMES SITE GPS CODE X Y Z sX sY sZ SOLN DATA_START DATA_END REF_EPOCH` (m) and
  the line of its velocity, `DOMES VX VY VZ sVX sVY sVZ` (m/y). `DATA_START` and `DATA_END` are the span of its data:
  the solution valid at an epoch is the one of `EPN_discontinuities.snx`, whose `P` rows give the validity, from a
  break to the next (AQUI: the earthquake of 2009 day 96 at 01:32:39 starts solution 3, while the data of solution 2
  run to the end of that day). The numbers of the solutions are those of the SSC. No position solution of the
  catalogue of C2415 starts after the end of the data of the solution (2026 day 115). There is no footer. A catalogue
  knows the breaks up to its making (that of C2415 was made in 2026, day 246; `latest/` holds the same file): a station
  that moved after it is on the line of its old solution until a newer release.

## Antenna calibrations: `igs20_excerpt.atx`

For `tests/test_gnss_atx.py` and the calibration tests of `tests/test_gnss_ppk.py`. The tests never use the network:
they point `atx.ATX_URL` at this file (`file://`), or give it to `ppk.solve` as the antenna file.

It is cut from `igs20.atx`, the antenna calibrations of the IGS (an ANTEX 1.4 file of 61.6 MB, 926 antennas, 414 of
them satellites), captured on 2026-10-27 at about 09:35 UTC with `curl` and no account, from
`https://files.igs.org/pub/station/general/igs20.atx` (`Last-Modified` 2026-09-28 16:05 GMT; the last change its
header lists is GPS week 2438). Open data of the IGS. 180 lines, 16 KB, written by a script (not kept) from that file:

| What | How it is in the excerpt |
|---|---|
| The first two lines (`ANTEX VERSION / SYST`, `PCV TYPE / REFANT`) | As they are |
| The comment block of the header (576 lines in the real file) | Replaced by ten lines of ours that say what this file is and that one entry is synthetic |
| Two satellites: `BLOCK IIA` G01 (G032) and `GLONASS-M` R01 (R730) | Whole, as they are (they have `VALID FROM` and `VALID UNTIL`, no azimuth rows) |
| `ASH700699.L1    NONE` (L1 only) | Whole, as it is |
| `ASH700829.2     SNOW`, `LEIAR25.R3      NONE`, `LEIAR25.R3      LEIT`, `TRM57971.00     NONE` | The lines of the real entry, without the azimuth rows (the `NOAZI` row stays) and so with `DAZI` 0.0; only the frequencies G01 G02 G05 R01 R02 (`# OF FREQUENCIES` says how many); without the `PCV #a #c #s` comment lines, which describe all the frequency sets. `ASH700829.2` is in the real file only with the `SNOW` radome; `LEIAR25.R3` has `NONE` before `LEIT`, as in the real file |
| `TRM29659.00     NONE` | **Made up.** See below |

The order of the file is the real one: satellites, then receiver antennas by name. RTKLIB takes the first entry that
fits (`searchpcv`), so the order matters to the tests.

**`TRM29659.00     NONE` is synthetic, not the calibration of the IGS.** That antenna is the one in the RINEX files of
`tests/data/rtklib` (the stations 0759 and 3040). The real entry has its G01 phase centre 90.51 mm above the reference
point and an offset in each frequency; this one has 100.00 mm on G01 and G02, 0.00 north and east, and zero variations
(a `NOAZI` row of zeros), so that the test that gives the file to RTKLIB can check an exact figure: the base is 0.100 m
higher than the program thought it, and so is every height of the rover. The file says so in the comment lines of its
header and in two `COMMENT` lines inside the entry. The tests that search `TRM29659.00     SCIS`, a radome the file does
not have, get this entry too.

The file is read by RTKLIB (`readpcv`, `searchpcv`) as `atx.receiver_antennas` and `atx.lookup` read it: the same
entries, the same offsets. The bytes are kept as they are when the file is checked out (`.gitattributes`).
