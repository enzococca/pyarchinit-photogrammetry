# Compact RINEX (Hatanaka) test files

Hatanaka-compressed RINEX observation files and the RINEX that the official CRX2RNX of RNXCMP 4.2.0 makes of
them (`<file>.expected`). `tests/test_crinex.py` checks that the decoder of the native core
(`ppg_core.crx2rnx`) gives the same bytes.

Made once, on 2026-10-27, with the macOS arm64 programs of RNXCMP 4.2.0 (Geospatial Information Authority of
Japan, https://terras.gsi.go.jp/ja/crx2rnx.html), run as programs and nothing more:
`RNX2CRX - < file.o > file.d`, then `CRX2RNX - < file.d > file.d.expected`. The format and RNXCMP: Hatanaka, Y.
(2008), A Compression Format and Tools for GNSS Observation Data, Bulletin of the Geographical Survey Institute,
55, 21-30.

| File | Compact RINEX | RINEX inside |
|---|---|---|
| `07590920.05d` | 1.0 | 2.10, GPS, 4 types, at most 9 satellites, 120 epochs at 30 s |
| `07590920.05d.Z` | 1.0, with Unix compress around it | the same |
| `KRAW00POL_R_20262991000_01H_30S_MO.crx` | 3.0 | 3.04, GPS (12 types) and GLONASS (8), 120 epochs at 30 s |
| `kraw_rinex402.crx` | 3.1 | 4.02, the same hour with clock offsets, picoseconds and events |
| `events.05d` | 1.0 | 2.10, 60 epochs with events |
| `kraw299k.26d` | 1.0 | 2.11, 10 types, 11 to 18 satellites, clock offsets, 40 epochs |

## Where each file comes from

- `07590920.05d`: RTKLIB's test observations of GEONET station 0759 (GSI), 2005-04-02 00:00-00:59:30,
  `tests/data/rtklib/07590920.05o`, through RNX2CRX. CRX2RNX gives back the original byte for byte.
- `07590920.05d.Z`: `07590920.05d` through macOS `/usr/bin/compress` (LZW, 16 bits).
- `KRAW00POL_R_20262991000_01H_30S_MO.crx`: EPN station KRAW00POL (Kraków, AGH University of Krakow, Leica
  GRX1200GGPRO), 2026-10-26 (day 299) 10:00-10:59:30 UTC, as BKG's EUREF data centre distributes it: downloaded on
  2026-10-27 from https://igs.bkg.bund.de/root_ftp/EUREF/nrt/299/10/KRAW00POL_R_20262991000_01H_30S_MO.crx.gz and
  taken out of its gzip (its second line names the RNX2CRX 4.2.0 that made it at the source).
- `kraw_rinex402.crx`: the RINEX 3.04 that CRX2RNX makes of the hour of KRAW, with version 4.02 in its first line
  (RNX2CRX then writes Compact RINEX 3.1) and, written in by a script, what RINEX 4.02 adds and the real file
  lacks: receiver clock offsets (F15.12) on most epochs, some blank, of every shape (0, below one, negative, the
  largest that fit); the picoseconds of the epoch (1X,I5 after the offset) on some of them; an event that starts
  moving the antenna (flag 2, two COMMENT lines, after 10:14:30) and a header-information event (flag 4, no epoch,
  after 10:49:30) that gives the observation types again, unchanged; observations of every shape in D1C of G05
  (10:25:00-10:34:30); indicators without their observation in C2S of G14 (10:45:00, 10:45:30, 10:47:30).
- `events.05d`: the first 60 epochs of `07590920.05o` with events written in: after 00:05:00 a header-information
  event (flag 4, no epoch) with two COMMENT lines; after 00:10:00 an external event (flag 5) at 00:10:15 with no
  record; after 00:15:00 the cycle slip records (flag 6) of two satellites; after 00:20:00 a header-information
  event that drops the fourth observation type (P2), after which the records hold three.
- `kraw299k.26d`: the first 40 epochs of the hour of KRAW written by a script as RINEX 2.11: ten types (C1 P2 L1
  L2 D1 D2 S1 S2 C2 P1, a continuation line in the header, two lines a satellite, P1 always blank), the list of
  satellites on a second line, receiver clock offsets (F12.9) of every shape on most epochs (some blank),
  observations of every shape in D1 of G05 and S2 of R18, G09 left out for four epochs (it comes back as a new
  satellite), a power failure flag (1) on one epoch, an epoch of 11 satellites with an offset (the list padded to
  column 68), the second line of R02 blank for four epochs.

The clock offsets, picoseconds, events and values written in by the scripts are made up, not station data.

## What CRX2RNX 4.2.0 does, which the files show or avoid

- It writes numbers below one without the zero (".123", "-.123"; Hatanaka 2008, section 3): so do the
  `.expected` files and the decoder.
- It writes a negative receiver clock offset that is a whole number of tenths of a millisecond (RINEX 3) or of
  tenths of a second (RINEX 2) one tenth nearer zero: -0.001000000000 as -.000900000000, -0.500000000 as
  -.400000000. The files have no such offset; the decoder writes the value the file holds
  (`test_negative_clock_offsets_are_written_as_they_are`).
- Compact RINEX 3.1 keeps the picoseconds as text from one epoch to the next, and RNX2CRX 4.2.0 does not write
  that they are gone: once an epoch has them, CRX2RNX writes the last ones on every later epoch, also on those
  without a clock offset (in `kraw_rinex402.crx.expected` every epoch from 10:01:00 on has picoseconds, where the
  RINEX 4.02 had them on 13). The decoder does the same.
- RNX2CRX refuses a blank observation with indicators in RINEX 2 (Compact RINEX 1.0 cannot hold it), and after an
  event copies as many lines as the event's number: cycle slip records of RINEX 2 with more than five types or more
  than 12 satellites cannot be compressed.

## Licences

- `07590920.05d`, `07590920.05d.Z`, `events.05d` and their `.expected`: RTKLIB's test data, BSD 2-clause licence
  (`tests/data/rtklib/license.txt`); observations of GEONET station 0759, Geospatial Information Authority of
  Japan.
- `KRAW00POL_R_20262991000_01H_30S_MO.crx`, `kraw_rinex402.crx`, `kraw299k.26d` and their `.expected`: data of the
  EUREF Permanent GNSS Network (EPN), licence CC BY 4.0 (the default of the EPN data policy). Credit: EUREF
  Permanent GNSS Network, station KRAW00POL (AGH University of Krakow), data from the BKG data centre.
