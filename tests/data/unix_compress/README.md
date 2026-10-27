# Files compressed with the Unix compress

Made on 2026-10-27 with the `/usr/bin/compress` of macOS (BSD compress: the format of the `.Z` files that the
archives of the GNSS stations give) from RTKLIB's sample files of GEONET station 0759
(`tests/data/rtklib/07590920.05o`, 68266 bytes, and `07590920.05n`). `tests/test_gnss_rinex_files.py` checks that
`gnss/lzw.py` gives back the bytes of the original.

| File | Made with | What it holds |
|---|---|---|
| `b9-first40k.Z` | `head -c 40960 07590920.05o \| compress -c -b 9` | the first 40960 bytes, which stop in the middle of a line (so `rinex.plain` refuses it as cut short; only `lzw` reads it); codes of the smallest width, one clearing of the dictionary and the 10-bit codes that compress writes after the dictionary of a stream limited to 9 bits fills |
| `b11-whole.Z` | `compress -c -b 11 < 07590920.05o` | the whole file; codes of 9, 10 and 11 bits and one clearing of the dictionary |
| `nav-first300lines-b12.Z` | `head -n 300 07590920.05n \| compress -c -b 12` | the first 300 lines (21856 bytes, a whole number of lines) of the navigation file: a `.n.Z` file as the archives give them |

`tests/data/crinex/07590920.05d.Z` (compress with the default 16 bits, around a Compact RINEX file) is the fourth.

compress writes no end mark and no checksum: a file cut short decodes, without an error, to its beginning. The tests cut
`b11-whole.Z` and `nav-first300lines-b12.Z` where the text stops in the middle of a line.

## Licence

RTKLIB's test data: BSD 2-clause licence (`tests/data/rtklib/license.txt`); observations and navigation messages of
GEONET station 0759, Geospatial Information Authority of Japan.
