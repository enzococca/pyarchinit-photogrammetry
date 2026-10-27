# Files compressed with the Unix compress

Made on 2026-10-27 with the `/usr/bin/compress` of macOS (BSD compress: the format of the `.Z` files that the
archives of the GNSS stations give) from RTKLIB's sample observations of GEONET station 0759
(`tests/data/rtklib/07590920.05o`, 68266 bytes). `tests/test_gnss_rinex_files.py` checks that `gnss/lzw.py` gives
back the bytes of the original.

| File | Made with | What it holds |
|---|---|---|
| `b9-first40k.Z` | `head -c 40960 07590920.05o \| compress -c -b 9` | the first 40960 bytes; codes of the smallest width, one clearing of the dictionary and the 10-bit codes that compress writes after the dictionary of a stream limited to 9 bits fills |
| `b11-whole.Z` | `compress -c -b 11 < 07590920.05o` | the whole file; codes of 9, 10 and 11 bits and one clearing of the dictionary |

`tests/data/crinex/07590920.05d.Z` (compress with the default 16 bits, around a Compact RINEX file) is the third.

## Licence

RTKLIB's test data: BSD 2-clause licence (`tests/data/rtklib/license.txt`); observations of GEONET station 0759,
Geospatial Information Authority of Japan.
