# Test data of the GNSS work, phase 4: raw receiver logs converted to RINEX

The raw logs are the first 64 KiB (65536 bytes, `head -c 65536`) of sample files of RTKLIB-EX
(https://github.com/rtklibexplorer/RTKLIB, tag v2.5.1, commit 62d4677ed8425a4e2748c6d390b500d1afb493fc,
`test/data/rcvraw/`), distributed with RTKLIB under its BSD 2-clause licence (Copyright (c) 2007-2020,
T. Takasu; text in `native/vendor/rtklib/license.txt`). SHA-256 of the whole upstream files:

| File here | Upstream file | SHA-256 of the upstream file |
|---|---|---|
| `ubx_20080526_64k.ubx` | `ubx_20080526.ubx` (u-blox, RXM-RAW and RXM-SFRB, GPS and SBAS) | `8986134fbac86b910c9a79f82c53c05dbc7b9442dd5cba669ae7be6a05b75870` |
| `GMSD7_20121014_64k.rtcm3` | `GMSD7_20121014.rtcm3` (RTCM 3, Trimble NetR9, BeiDou MSM) | `8b466a6829122c21ee28053324811022b3e44baa6dcd878a51b9a746a10d8cb1` |
| `oemv_200911218_64k.gps` | `oemv_200911218.gps` (NovAtel OEMV, GPS, GLONASS and SBAS) | `65c4e666c73598fac9a0dc0b915d340a606daa9628dc26cc81f1812f7dfa4d36` |

The `.obs` and `.nav` files are what the official `convbin` of the same commit wrote from them, once, with the
options the native core uses by default (RINEX 3.04, all frequencies, SBAS and NavIC left out, as `systems="GREJC"`).
It was compiled from the upstream sources with the definitions of its makefile
(`app/consapp/convbin/gcc/makefile`: `-DTRACE -DENAGLO -DENAQZS -DENAGAL -DENACMP -DENAIRN -DNFREQ=4 -DNEXOBS=3`)
and run as:

```
convbin -r ubx   -v 3.04 -y S -y I -o ubx_20080526_64k.obs     -n ubx_20080526_64k.nav     ubx_20080526_64k.ubx
convbin -r ubx   -v 3.04 -y S -y I -o ubx_20080526_64k_tm2.obs -n ubx_20080526_64k_tm2.nav ubx_20080526_64k_tm2.ubx
convbin -r rtcm3 -tr 2012/10/14 00:00:00 -v 3.04 -y S -y I -o GMSD7_20121014_64k.obs -n GMSD7_20121014_64k.nav GMSD7_20121014_64k.rtcm3
convbin -r nov   -v 3.04 -y S -y I -o oemv_200911218_64k.obs   -n oemv_200911218_64k.nav   oemv_200911218_64k.gps
```

They are equal to what the native core writes except the line `PGM / RUN BY / DATE` (program and date of the run)
and the `COMMENT` line `log: <input file>` (its path). Equal on these samples, not in general: the core builds RTKLIB
with `NFREQ=3` (as its post-processing needs) where this convbin has `NFREQ=4`, so it keeps at most 6 signals of a
satellite in an epoch instead of 7, and on logs of many signals (Galileo E6, BeiDou B3I, B1C, B2a beside the others)
it may write fewer observation types. These samples have 3 signals at most. RTKLIB-EX's `test/data/rcvraw` at this
commit holds no log of more signals (no u-blox RXM-RAWX, no RTCM 3 MSM7 of four frequencies): the difference is not
pinned by a test. The navigation file of `ubx_20080526_64k_tm2.ubx` is the
same as that of `ubx_20080526_64k.ubx` and is not kept.

`ubx_20080526_64k_tm2.ubx` is not kept either: the tests make it again from `ubx_20080526_64k.ubx` with
`tests/convbin_helpers.py` (`insert_time_marks`), which puts four UBX TIM-TM2 frames written from the u-blox
Interface Description (UBX-18010854) after the first raw epoch at or after their time, GPS week 1481:

| Count | Edge | Time base | Time of the edge | Expected event (GPS time) |
|---|---|---|---|---|
| 1 | falling | GNSS | 107975.5 s | 2008-05-26 05:59:35.5 |
| 2 | falling | UTC | 107976.25 s (UTC) | 2008-05-26 05:59:50.25 (UTC + 14 s) |
| 3 | falling | GNSS | 108005.123456789 s | 2008-05-26 06:00:05.1234568 |
| 4 | rising only | GNSS | 108015.0 s | none: RTKLIB reads the falling edge only |

No public sample with a clear licence was found for Septentrio SBF, Trimble RT-17 or Unicore: the core accepts
these names but their decoders are not built (see `native/vendor/rtklib/VERSION`), and the tests check only
that they are refused cleanly.

## `mrk_behaviour.json`

What `apply_mrk` and `apply_ppk` returned (or the text of their errors), and the state of the photos after each run, on
the three synthetic DJI projects that `tests/gnss_mrk_projects.py` builds (photos paired by shot number, by order, and
a PPK flight on the day of RTKLIB's sample data). It was written by the code of these two functions as it was before
the shots of any source were paired with the photos (commit 8c0ff9d), with:

```
PYTHONPATH=src:tests python tests/gnss_mrk_projects.py
```

`test_gnss_shots.test_mrk_behaviour_unchanged` builds the projects again and compares: the results of a DJI MRK must
never change. Folders of the run are written `<tmp>`. Synthetic data, made for these tests (GPL-2.0-or-later, as the
program).

## PX4 ULog

`sample_appended_multiple.ulg` is `test/sample_appended_multiple.ulg` of pyulog (https://github.com/PX4/pyulog,
master at commit 0b392f8444f843b796405b9766cc15a6dddedf62, the file last changed in
dd44f431deb1f9b2075b7d513d41073b459b1109), a log of PX4 with appended data and a topic of two instances, distributed
under pyulog's BSD 3-clause licence (Copyright (c) 2016, PX4 Pro Drone Autopilot; all rights reserved), whose
notice, conditions and disclaimer `pyulog_LICENSE.md` beside it reproduces (its third clause names
"GpsDrivers" and its contributors, as pyulog's licence file does). SHA-256
`daf30f3224303e39d5c97701e048e84ba04480797e369502331f45ab2e99a2b7`.
`sample_appended_multiple_info.txt` is the topic table (and the dropout line) of pyulog's
`test/sample_appended_multiple_info.txt` of the same commit: the counts of an independent reader, which
`tests/test_gnss_px4.py` checks ours against. No code of pyulog is used. The log has no camera topics: the camera
shots are tested on logs that `tests/gnss_ulog.py` writes.
