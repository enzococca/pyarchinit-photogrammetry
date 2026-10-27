"""The position of a base by static PPP (gnss/ppp.py): the precise products of one centre for the days of a session,
RTKLIB's options of a static PPP, and the position of the mark it finds. The archives are never reached: their folders
are trees of files that the tests write (file:// addresses). RTKLIB is replaced by a function that writes a solution,
or - where the native core has RTKLIB - runs on orbits and clocks that the tests make from the broadcast ephemerides
of RTKLIB's sample data: products only as good as the broadcast ones, but read by RTKLIB as those of the IGS."""
from __future__ import annotations

import dataclasses
import gzip
import hashlib
import logging
import math
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.core.progress import Cancelled, Progress
from pyarchinit_photogrammetry.gnss import atx, download, frames, ppk, ppp, rinex
from gnss_helpers import (START_2005, WEEK, enu, needs_rtklib, on_label, pos_text, rewrite_header, rinex_piece,
                          rinex_records, rtklib_option_names, rtklib_options)

DATA = Path(__file__).parent / "data" / "rtklib"
OWN = DATA / "30400920.05o"  # the user's base: 3040, standing still from 00:00:00 to 00:59:30 every 30 s
NAV = DATA / "30400920.05n"  # its broadcast ephemerides, for all of 2005-04-02
ATX_EXCERPT = Path(__file__).parent / "data" / "gnss" / "igs20_excerpt.atx"  # TRM29659.00 NONE: 0.100 m up (synthetic)
# the middle of the hour of 3040, 00:29:45 GPS time, as a decimal year of UTC (GPS time was 13 s ahead in 2005)
MID_2005 = frames.decimal_year(datetime(2005, 4, 2, 0, 29, 32))
GPS_EPOCH = date(1980, 1, 6)
SESSION = (datetime(2026, 10, 26, 9, 0), datetime(2026, 10, 26, 13, 0))  # a Monday, GPS week 2442, day 299


@pytest.fixture(autouse=True)
def gnss_folder(tmp_path, monkeypatch):
    """The downloads and caches of the test in a folder of its own."""
    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))
    return tmp_path / "gnss"


def _uri(path) -> str:
    return Path(path).as_uri()


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _doy(day: date) -> str:
    return f"{day:%Y}{day.timetuple().tm_yday:03d}"


def _week(day: date) -> int:
    return (day - GPS_EPOCH).days // 7


# --- the files of the archives, as they name them ---------------------------------------------------------------------


def _expected(centre: str, kind: str, day: date) -> dict[str, str]:
    """Where the archive of ``centre`` keeps the orbits, the clocks and the ERP of ``day`` of the ``kind`` of products,
    from the root of its folder of products: written out here, apart from the module, as the archives name them."""
    d, week = _doy(day), f"{_week(day):04d}"
    if (centre, kind) == ("ESA", "final"):
        return {"sp3": f"{week}/ESA0OPSFIN_{d}0000_01D_05M_ORB.SP3.gz",
                "clk": f"{week}/ESA0OPSFIN_{d}0000_01D_30S_CLK.CLK.gz",
                "erp": f"{week}/ESA0OPSFIN_{d}0000_01D_01D_ERP.ERP.gz"}
    if (centre, kind) == ("CODE", "final"):
        return {"sp3": f"{day:%Y}/COD0OPSFIN_{d}0000_01D_05M_ORB.SP3.gz",
                "clk": f"{day:%Y}/COD0OPSFIN_{d}0000_01D_30S_CLK.CLK.gz",
                "erp": f"{day:%Y}/COD0OPSFIN_{d}0000_01D_01D_ERP.ERP.gz"}
    if (centre, kind) == ("IGS", "final"):  # one ERP file for each GPS week, named by its Sunday
        sunday = _doy(GPS_EPOCH + timedelta(weeks=_week(day)))
        return {"sp3": f"{week}/IGS0OPSFIN_{d}0000_01D_15M_ORB.SP3.gz",
                "clk": f"{week}/IGS0OPSFIN_{d}0000_01D_30S_CLK.CLK.gz",
                "erp": f"{week}/IGS0OPSFIN_{sunday}0000_07D_01D_ERP.ERP.gz"}
    if (centre, kind) == ("IGS", "rapid"):
        return {"sp3": f"{week}/IGS0OPSRAP_{d}0000_01D_15M_ORB.SP3.gz",
                "clk": f"{week}/IGS0OPSRAP_{d}0000_01D_05M_CLK.CLK.gz",
                "erp": f"{week}/IGS0OPSRAP_{d}0000_01D_01D_ERP.ERP.gz"}
    if (centre, kind) == ("IGS", "ultra"):  # predicted: never to be used
        return {"sp3": f"{week}/IGS0OPSULT_{d}0000_02D_15M_ORB.SP3.gz",
                "clk": f"{week}/IGS0OPSULT_{d}0000_02D_05M_CLK.CLK.gz",
                "erp": f"{week}/IGS0OPSULT_{d}0000_02D_01D_ERP.ERP.gz"}
    raise AssertionError((centre, kind))


def _mjd(day: date) -> int:
    return (day - date(1858, 11, 17)).days


def _sp3_text(day: date, note: str = "") -> str:
    """The header of an SP3-d file of ``day``, what tells it apart and the time it covers: its first epoch, 288 epochs,
    5 minutes apart."""
    seconds = (datetime(day.year, day.month, day.day) - datetime(1980, 1, 6)).total_seconds()
    return (f"#dP{day.year:4d} {day.month:2d} {day.day:2d}  0  0  0.00000000     288 ORBIT IGS20 FIT  TST\n"
            f"## {int(seconds // WEEK):4d} {seconds % WEEK:15.8f} {300.0:14.8f} {_mjd(day):5d} {0.0:15.13f}\n"
            f"/* {note}\nEOF\n")


def _clk_text(day: date, note: str = "", last: tuple[int, int, int] = (23, 59, 30)) -> str:
    """A RINEX clock file of ``day``: its first two epochs, 30 s apart, and its ``last`` (hour, minute, second)."""
    records = "".join(f"AS G01  {day.year:4d} {day.month:02d} {day.day:02d} {h:02d} {m:02d}{float(sec):10.6f}  1    "
                      "1.000000000000E-04\n" for h, m, sec in ((0, 0, 0), (0, 0, 30), last))
    return ("     3.00           C                   M                   RINEX VERSION / TYPE\n"
            f"{note:<60}COMMENT\n" + " " * 60 + "END OF HEADER\n" + records)


def _erp_text(rows) -> str:
    """An ERP file of the IGS format (version 2): ``rows`` of MJD, the pole x and y (arcseconds) and UT1-UTC (s)."""
    lines = ["VERSION 2", "EOP SOLUTION",
             "  MJD         X        Y     UT1-UTC    LOD   Xsig   Ysig  UTsig LODsig  Nr Nf Nt    Xrt    Yrt  Xrtsig "
             "Yrtsig",
             '                10**-6"        .1us    .1us/d    10**-6"     .1us  .1us/d                10**-6"/d    '
             '10**-6"/d']
    for mjd, xp, yp, ut1 in rows:
        lines.append(f"{mjd:8.2f} {round(xp * 1e6):8d} {round(yp * 1e6):8d} {round(ut1 * 1e7):9d} {0:6d} {10:5d} "
                     f"{10:5d} {20:6d} {20:6d} {50:3d} {5:2d} {5:2d} {0:6d} {0:6d} {30:6d} {30:6d}")
    return "\n".join(lines) + "\n"


def _contents(centre: str, kind: str, day: date) -> dict[str, str]:
    note = f"{centre} {kind} {day}"
    return {"sp3": _sp3_text(day, note), "clk": _clk_text(day, note),
            "erp": _erp_text([(_mjd(day) + 0.5, 0.05, 0.4, -0.1)])}


def _gz(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(text.encode("ascii")))
    return path


def _plain_text(path: Path) -> str:
    return gzip.decompress(path.read_bytes()).decode("ascii")


@pytest.fixture
def archives(tmp_path, monkeypatch):
    """The folders of products of ESA, CODE and the IGS (at BKG) as trees of files, empty until a test puts the files
    of a day in them (``put``)."""
    root = tmp_path / "archives"
    folders = {"ESA": root / "esa", "CODE": root / "code", "IGS": root / "igs"}
    monkeypatch.setattr(ppp, "CENTRES", tuple(dataclasses.replace(c, root=_uri(folders[c.name]) + "/")
                                              for c in ppp.CENTRES))

    def put(centre: str, kind: str, day: date) -> dict[str, Path]:
        """The orbits, the clocks and the ERP of ``day`` in the archive of ``centre``, packed in gzip as there."""
        texts = _contents(centre, kind, day)
        return {role: _gz(folders[centre] / where, texts[role]) for role, where in _expected(centre, kind, day).items()}

    return SimpleNamespace(folders=folders, put=put)


def _fetches(monkeypatch) -> list[str]:
    """The addresses ``download.fetch`` is asked for from now on."""
    calls: list[str] = []
    real = download.fetch

    def spy(url, *args, **kwargs):
        calls.append(url)
        return real(url, *args, **kwargs)

    monkeypatch.setattr(download, "fetch", spy)
    return calls


def _cached(centre: str) -> list[str]:
    folder = download.gnss_dir() / "products" / centre
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# --- the precise products ---------------------------------------------------------------------------------------------


def test_centres_are_the_public_archives():
    """ESA's final products, then CODE's, then the IGS's, then the rapid ones of the IGS, from the public archives that
    ask for no account, over HTTPS; the files of a day as the archives name them."""
    assert [(c.name, c.kind) for c in ppp.CENTRES] == [("ESA", "final"), ("CODE", "final"), ("IGS", "final"),
                                                         ("IGS", "rapid")]
    day = date(2026, 10, 26)
    roots = {"ESA": "https://navigation-office.esa.int/products/gnss-products/",
             "CODE": "https://www.aiub.unibe.ch/download/CODE/",
             "IGS": "https://igs.bkg.bund.de/root_ftp/IGS/products/"}
    for centre in ppp.CENTRES:
        where = _expected(centre.name, centre.kind, day)
        expected = {role: roots[centre.name] + path for role, path in where.items()}
        assert centre.addresses(day) == expected
        assert centre.credit and centre.name in centre.credit
    assert ppp.CENTRES[0].addresses(day)["sp3"] == ("https://navigation-office.esa.int/products/gnss-products/2442/"
                                                    "ESA0OPSFIN_20262990000_01D_05M_ORB.SP3.gz")


ESA_PRODUCTS = "https://navigation-office.esa.int/products/gnss-products/"
CODE_PRODUCTS = "https://www.aiub.unibe.ch/download/CODE/"
BKG_PRODUCTS = "https://igs.bkg.bund.de/root_ftp/IGS/products/"


@pytest.mark.parametrize("day, d, year", [(date(2024, 12, 31), "2024366", "2024"),
                                          (date(2025, 1, 1), "2025001", "2025")], ids=["2024-12-31", "2025-01-01"])
def test_the_names_across_the_new_year(day, d, year):
    """The last day of a leap year is day 366, the next one day 001 of the next year; both are in the folder of GPS
    week 2347 at ESA and BKG, and each in the folder of its own year at CODE; the weekly ERP of the final products of
    the IGS is named by the Sunday of the week, 2024-12-29 (day 364), for both."""
    esa, code, igs_final, igs_rapid = ppp.CENTRES
    assert esa.addresses(day) == {"sp3": f"{ESA_PRODUCTS}2347/ESA0OPSFIN_{d}0000_01D_05M_ORB.SP3.gz",
                                  "clk": f"{ESA_PRODUCTS}2347/ESA0OPSFIN_{d}0000_01D_30S_CLK.CLK.gz",
                                  "erp": f"{ESA_PRODUCTS}2347/ESA0OPSFIN_{d}0000_01D_01D_ERP.ERP.gz"}
    assert code.addresses(day) == {"sp3": f"{CODE_PRODUCTS}{year}/COD0OPSFIN_{d}0000_01D_05M_ORB.SP3.gz",
                                   "clk": f"{CODE_PRODUCTS}{year}/COD0OPSFIN_{d}0000_01D_30S_CLK.CLK.gz",
                                   "erp": f"{CODE_PRODUCTS}{year}/COD0OPSFIN_{d}0000_01D_01D_ERP.ERP.gz"}
    assert igs_final.addresses(day) == {"sp3": f"{BKG_PRODUCTS}2347/IGS0OPSFIN_{d}0000_01D_15M_ORB.SP3.gz",
                                        "clk": f"{BKG_PRODUCTS}2347/IGS0OPSFIN_{d}0000_01D_30S_CLK.CLK.gz",
                                        "erp": f"{BKG_PRODUCTS}2347/IGS0OPSFIN_20243640000_07D_01D_ERP.ERP.gz"}
    assert igs_rapid.addresses(day) == {"sp3": f"{BKG_PRODUCTS}2347/IGS0OPSRAP_{d}0000_01D_15M_ORB.SP3.gz",
                                        "clk": f"{BKG_PRODUCTS}2347/IGS0OPSRAP_{d}0000_01D_05M_CLK.CLK.gz",
                                        "erp": f"{BKG_PRODUCTS}2347/IGS0OPSRAP_{d}0000_01D_01D_ERP.ERP.gz"}


@pytest.mark.parametrize("present, chosen", [
    ([("ESA", "final"), ("CODE", "final"), ("IGS", "final"), ("IGS", "rapid")], ("ESA", "final")),
    ([("CODE", "final"), ("IGS", "final"), ("IGS", "rapid")], ("CODE", "final")),
    ([("IGS", "final"), ("IGS", "rapid")], ("IGS", "final")),
    ([("IGS", "rapid")], ("IGS", "rapid")),
], ids=["ESA", "CODE", "IGS final", "IGS rapid"])
def test_products_final_then_rapid(archives, tmp_path, present, chosen):
    """The final products of ESA, else CODE's, else the IGS's, else the rapid ones of the IGS: decompressed into the
    folder with their extensions, the files downloaded kept in the GNSS folder as the archive names them."""
    day = date(2026, 10, 26)
    for centre, kind in present:
        archives.put(centre, kind, day)
    folder = tmp_path / "products"
    got = ppp.products(*SESSION, folder)
    assert (got.centre, got.kind) == chosen
    names = {role: Path(where).name for role, where in _expected(*chosen, day).items()}
    for role in ("sp3", "clk", "erp"):
        (path,) = getattr(got, role)
        assert path == folder / names[role][:-3]  # ".SP3", ".CLK", ".ERP": RTKLIB tells the files by them
        assert path.read_text() == _contents(*chosen, day)[role]
    assert _cached(chosen[0]) == sorted(names.values())
    assert not list(folder.glob("*.gz")) and not list(download.gnss_dir().rglob("*.part"))


def test_products_never_ultra_rapid(archives, tmp_path, monkeypatch):
    """The ultra-rapid products of the IGS are half predicted: never asked for, even when they are all there is."""
    day = date(2026, 10, 26)
    archives.put("IGS", "ultra", day)
    fetched = _fetches(monkeypatch)
    with pytest.raises(ValueError, match="ultra-rapid"):
        ppp.products(*SESSION, tmp_path / "products")
    archives.put("IGS", "rapid", day)
    got = ppp.products(*SESSION, tmp_path / "products")
    assert (got.centre, got.kind) == ("IGS", "rapid")
    assert fetched and not any("ULT" in url for url in fetched)
    assert not any("ULT" in name for name in _cached("IGS"))


def test_products_across_midnight_one_centre(archives, tmp_path, monkeypatch):
    """A session over midnight takes the products of both days from one centre and of one kind: ESA with only the
    first day is passed over as a whole - without downloading anything of it - for CODE, which has both; a session
    across the end of a GPS week takes the files of two week folders, and two weekly ERP files of the IGS."""
    saturday, sunday = date(2026, 10, 24), date(2026, 10, 25)  # GPS weeks 2441 and 2442
    start, end = datetime(2026, 10, 24, 23, 0), datetime(2026, 10, 25, 1, 0)
    archives.put("ESA", "final", saturday)
    for day in (saturday, sunday):
        archives.put("CODE", "final", day)
        archives.put("IGS", "final", day)
    fetched = _fetches(monkeypatch)
    got = ppp.products(start, end, tmp_path / "products")
    assert (got.centre, got.kind) == ("CODE", "final")

    def names(day: date, role: str) -> str:
        return Path(_expected(got.centre, got.kind, day)[role]).name[:-3]

    assert [p.name for p in got.sp3] == [names(saturday, "sp3"), names(sunday, "sp3")]
    assert [p.name for p in got.clk] == [names(saturday, "clk"), names(sunday, "clk")]
    assert [p.name for p in got.erp] == [names(saturday, "erp"), names(sunday, "erp")]
    # the last day first: ESA's orbits of Sunday are not there, and nothing else of ESA is asked for
    esa = [url for url in fetched if url.startswith(_uri(archives.folders["ESA"]))]
    assert esa == [_uri(archives.folders["ESA"]) + "/" + _expected("ESA", "final", sunday)["sp3"]]
    assert _cached("ESA") == []
    # without CODE's second day (in the archive and in the copies of the program): the IGS, two week folders, an ERP
    # file for each week
    missing = archives.folders["CODE"] / _expected("CODE", "final", sunday)["sp3"]
    missing.unlink()
    (download.gnss_dir() / "products" / "CODE" / missing.name).unlink()
    got = ppp.products(start, end, tmp_path / "other")
    assert (got.centre, got.kind) == ("IGS", "final")
    assert [p.name for p in got.erp] == ["IGS0OPSFIN_20262910000_07D_01D_ERP.ERP",
                                         "IGS0OPSFIN_20262980000_07D_01D_ERP.ERP"]
    assert [p.name for p in got.sp3] == ["IGS0OPSFIN_20262970000_01D_15M_ORB.SP3",
                                         "IGS0OPSFIN_20262980000_01D_15M_ORB.SP3"]


def test_a_centre_passed_over_leaves_in_the_folder_only_what_was_there(archives, tmp_path):
    """ESA with the last day of the session and not the first: the files of the last day are downloaded and kept in
    the GNSS folder, but what the call decompressed of them goes when the first day is not there; a file that an
    earlier call left in the folder stays."""
    saturday, sunday = date(2026, 10, 24), date(2026, 10, 25)
    esa = archives.put("ESA", "final", sunday)
    for day in (saturday, sunday):
        archives.put("CODE", "final", day)
    folder = tmp_path / "products"
    folder.mkdir()
    earlier = folder / esa["erp"].name[:-3]
    earlier.write_text(_contents("ESA", "final", sunday)["erp"])
    got = ppp.products(datetime(2026, 10, 24, 23, 0), datetime(2026, 10, 25, 1, 0), folder)
    assert got.centre == "CODE"
    assert sorted(p.name for p in folder.iterdir()) == sorted([p.name for p in got.sp3 + got.clk + got.erp]
                                                              + [earlier.name])
    assert _cached("ESA") == sorted(path.name for path in esa.values())


def test_products_not_published_says_when(archives, tmp_path, monkeypatch):
    """Nothing published for the session: the error names what is missing where, and says when the final products of
    ESA (about 4 days after the last day of the session) and the rapid ones of the IGS (about 17 hours after it)
    come."""
    monkeypatch.setattr(ppp, "_now", lambda: datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError) as error:
        ppp.products(*SESSION, tmp_path / "products")
    text = str(error.value)
    assert "2026-10-26 09:00" in text and "13:00" in text
    assert "ESA0OPSFIN_20262990000_01D_05M_ORB.SP3.gz" in text and _uri(archives.folders["ESA"]) in text
    assert "COD0OPSFIN_" in text and "IGS0OPSFIN_" in text and "IGS0OPSRAP_" in text
    assert text.count("not published yet") == 4
    assert "about 17 hours" in text and "2026-10-27 17:00" in text  # the rapid ones of the IGS
    assert "about 4 days" in text and "2026-10-31" in text  # the final ones of ESA
    assert "ultra-rapid" in text
    assert not (tmp_path / "products").exists() or not list((tmp_path / "products").iterdir())
    # long after the session: the files should be there; a session older than the long names says so
    monkeypatch.setattr(ppp, "_now", lambda: datetime(2026, 12, 26, tzinfo=timezone.utc))
    with pytest.raises(ValueError) as error:
        ppp.products(*SESSION, tmp_path / "products")
    assert "not published yet" not in str(error.value)
    with pytest.raises(ValueError, match="2238") as error:
        ppp.products(datetime(2021, 6, 1, 9), datetime(2021, 6, 1, 12), tmp_path / "products")
    assert "27 November 2022" in str(error.value)


@pytest.mark.parametrize("now, waiting, due, dates, past", [
    (datetime(2026, 10, 28, 12, 0), 3, 1, ["2026-10-31"], ["2026-10-27 17:00"]),
    (datetime(2026, 11, 14, 12, 0), 1, 3, [], ["2026-10-27 17:00", "2026-10-31"]),
    (datetime(2026, 12, 26, 12, 0), 0, 4, [], ["2026-10-27 17:00", "2026-10-31"]),
], ids=["rapid due", "IGS finals by the week", "all due"])
def test_products_that_should_be_published_by_now_say_so(archives, tmp_path, monkeypatch, now, waiting, due, dates,
                                                          past):
    """Once the products should be published - the rapid ones of the IGS 17 hours after the end of the last day of
    the session, the final ones of ESA 4 days after it - the message says so, and to try again later or to give them
    by hand, with no date that is gone. The final products of the IGS come a GPS week at a time: they are waited for
    from the end of the week of the session (Saturday 2026-10-31), not from the end of its day."""
    monkeypatch.setattr(ppp, "_now", lambda: now.replace(tzinfo=timezone.utc))
    with pytest.raises(ValueError) as error:
        ppp.products(*SESSION, tmp_path / "products")
    text = str(error.value)
    assert text.count("not published yet") == waiting and text.count("should be published by now") == due
    if waiting == 1:
        assert "IGS0OPSFIN_20262990000_01D_15M_ORB.SP3.gz" in [line for line in text.splitlines()
                                                               if "not published yet" in line][0]
    assert "should already be published" in text and "try again later" in text and "by hand" in text
    assert all(when in text for when in dates) and not any(when in text for when in past)
    assert "ultra-rapid" in text


def test_a_damaged_product_is_fetched_again_or_its_centre_passed_over(archives, tmp_path, monkeypatch, caplog):
    """A copy in the GNSS folder that is cut short is downloaded again; a file of the archive that is no good, not
    even downloaded again, passes the centre over for the next one, and is not kept."""
    day = date(2026, 10, 26)
    esa = archives.put("ESA", "final", day)
    archives.put("CODE", "final", day)
    cache = download.gnss_dir() / "products" / "ESA"
    cache.mkdir(parents=True)
    whole = esa["sp3"].read_bytes()
    (cache / esa["sp3"].name).write_bytes(whole[: len(whole) // 2])  # cut short
    got = ppp.products(*SESSION, tmp_path / "products")
    assert got.centre == "ESA" and got.sp3[0].read_text() == _contents("ESA", "final", day)["sp3"]
    assert (cache / esa["sp3"].name).read_bytes() == whole
    # the archive's own file is a page of HTML: CODE
    esa["clk"].write_bytes(b"<html><body>Service unavailable</body></html>")
    (cache / esa["clk"].name).unlink()
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        got = ppp.products(*SESSION, tmp_path / "products2")
    assert (got.centre, got.kind) == ("CODE", "final")
    assert esa["clk"].name not in _cached("ESA")
    assert any(esa["clk"].name in r.getMessage() and "ESA" in r.getMessage() for r in caplog.records)
    # a file that holds something else (the orbits of the archive are a clock file): no good either
    esa["clk"].write_bytes(gzip.compress(_clk_text(day).encode()))
    esa["sp3"].write_bytes(gzip.compress(_clk_text(day).encode()))
    (cache / esa["sp3"].name).unlink()
    assert ppp.products(*SESSION, tmp_path / "products3").centre == "CODE"


def test_an_archive_that_cannot_be_reached_is_passed_over(archives, tmp_path, monkeypatch, caplog):
    """An archive that does not answer passes its centre over for the next (and is not asked again for the other
    kind of products it keeps); when none answers, the error says why for each."""
    day = date(2026, 10, 26)
    for centre, kind in (("ESA", "final"), ("CODE", "final"), ("IGS", "final"), ("IGS", "rapid")):
        archives.put(centre, kind, day)
    asked: list[str] = []
    real = download.fetch
    down: set[str] = set()  # the archives that do not answer

    def fetch(url, *args, **kwargs):
        asked.append(url)
        if any(url.startswith(root) for root in down):
            raise download.DownloadError(f"{url}: could not connect (no route to host)", url)
        return real(url, *args, **kwargs)

    monkeypatch.setattr(download, "fetch", fetch)
    down = {_uri(archives.folders["ESA"])}
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        assert ppp.products(*SESSION, tmp_path / "products").centre == "CODE"
    assert any("ESA" in r.getMessage() and "no route to host" in r.getMessage() for r in caplog.records)
    down = {_uri(archives.folders["ESA"]), _uri(archives.folders["CODE"]), _uri(archives.folders["IGS"])}
    shutil.rmtree(download.gnss_dir() / "products")  # no copy of CODE's files from the first call
    asked.clear()
    with pytest.raises(ValueError) as error:
        ppp.products(*SESSION, tmp_path / "products2")
    assert str(error.value).count("no route to host") >= 3
    assert sum(url.startswith(_uri(archives.folders["IGS"])) for url in asked) == 1  # BKG: once, not for the rapid


def test_products_say_what_they_do_and_can_be_cancelled(archives, tmp_path):
    archives.put("ESA", "final", date(2026, 10, 26))
    seen: list[float] = []
    ppp.products(*SESSION, tmp_path / "products", progress=lambda fraction, message: seen.append(fraction))
    assert seen == sorted(seen) and seen[-1] == 1.0
    progress = Progress()
    progress.cancel_requested = True
    with pytest.raises(Cancelled):
        ppp.products(*SESSION, tmp_path / "other", progress=progress)


def test_products_of_the_user_are_checked(tmp_path):
    sp3, clk, erp = tmp_path / "a.sp3", tmp_path / "a.clk", tmp_path / "a.erp"
    given = ppp.Products(sp3, [str(clk)], (erp,), "GFZ", "final")
    assert given.sp3 == [sp3] and given.clk == [clk] and given.erp == [erp]
    with pytest.raises(ValueError, match="ultra-rapid"):
        ppp.Products([sp3], [clk], [erp], "IGS", "ultra")
    with pytest.raises(ValueError, match="Earth rotation"):
        ppp.Products([sp3], [clk], [], "ESA", "final")
    with pytest.raises(ValueError, match="orbits"):
        ppp.Products([], [clk], [erp], "ESA", "final")


# --- the options of RTKLIB --------------------------------------------------------------------------------------------

ANTENNAS = "/work/ppp/rtklib_inputs/antennas.atx"
ROTATION = "/work/ppp/rtklib_inputs/rotation.erp"


def test_ppp_config_options():
    """Every option of a static PPP: ppp-static on L1 and L2 forward, the ionosphere-free combination, the zenith delay
    of the troposphere estimated (with its gradients from 4 hours of observations), solid earth and pole tides, the
    satellite and receiver antenna models, phase wind-up, eclipsing satellites out, RAIM, GPS and Galileo, precise
    orbits and clocks, no ambiguity fixing (RTKLIB-EX makes float PPP solutions only), the antenna of the RINEX
    header, igs20.atx for the satellites and the receiver, the ERP, one position for the session."""
    options = rtklib_options(ppp.config(ANTENNAS, [ROTATION], 2.0))
    expected = {"pos1-posmode": "ppp-static", "pos1-frequency": "l1+l2", "pos1-soltype": "forward",
                "pos1-ionoopt": "dual-freq", "pos1-tropopt": "est-ztd", "pos1-tidecorr": "5", "pos1-posopt1": "on",
                "pos1-posopt2": "on", "pos1-posopt3": "on", "pos1-posopt4": "on", "pos1-posopt5": "on",
                "pos1-navsys": "9", "pos1-sateph": "precise", "pos2-armode": "off", "ant1-anttype": "*",
                "file-satantfile": ANTENNAS, "file-rcvantfile": ANTENNAS, "file-eopfile": ROTATION,
                "out-solstatic": "single",
                # what read_pos reads: latitude, longitude and ellipsoidal height, in GPS weeks and seconds
                "out-solformat": "llh", "out-outhead": "on", "out-timesys": "gpst", "out-timeform": "tow",
                "out-height": "ellipsoidal"}
    for name, value in expected.items():
        assert options.get(name) == value, name
    assert set(options) <= rtklib_option_names()
    assert not any(name.startswith("ant2-") for name in options)  # no base: the receiver is positioned on its own
    assert rtklib_options(ppp.config(ANTENNAS, [ROTATION], 5.0))["pos1-tropopt"] == "est-ztdgrad"
    assert rtklib_options(ppp.config(ANTENNAS, [ROTATION], 4.0))["pos1-tropopt"] == "est-ztdgrad"
    assert rtklib_options(ppp.config(ANTENNAS, [ROTATION], 3.99))["pos1-tropopt"] == "est-ztd"


@pytest.mark.parametrize("antenna, eops, hours, match", [
    (ANTENNAS, [], 2.0, "Earth rotation"),
    (ANTENNAS, [ROTATION, "/work/ppp/rtklib_inputs/rotation2.erp"], 2.0, "one ERP file"),
    (ANTENNAS, ["/work/ppp/rtklib_inputs/rotation.txt"], 2.0, r"\.erp"),
    (ANTENNAS, ["/work/100%full/rotation.erp"], 2.0, "%"),
    (ANTENNAS, ["/work/#1/rotation.erp"], 2.0, "#"),
    (ANTENNAS, ["/work/" + "x" * 1000 + "/rotation.erp"], 2.0, "bytes"),
    ("/work/ppp/rtklib_inputs/antennas.pcv", [ROTATION], 2.0, r"\.atx"),
    (ANTENNAS, [ROTATION], 0.0, "hours"),
    (ANTENNAS, [ROTATION], float("nan"), "hours"),
], ids=["no erp", "two erp", "erp extension", "erp percent", "erp hash", "erp too long", "atx extension", "no hours",
        "nan hours"])
def test_ppp_config_refuses_what_rtklib_would_misread(antenna, eops, hours, match):
    with pytest.raises(ValueError, match=match):
        ppp.config(antenna, eops, hours)


def test_the_erp_files_of_the_days_are_joined_in_time_order(tmp_path):
    """RTKLIB reads one ERP file and looks its rows up by a binary search: the files of the days are written one after
    the other into one file, in the order of their days whatever the order given."""
    first = tmp_path / "day1.erp"
    first.write_text(_erp_text([(53461.5, 0.05, 0.40, -0.10)]))
    second = tmp_path / "day2.erp"
    second.write_text(_erp_text([(53462.5, 0.06, 0.41, -0.11), (53463.5, 0.07, 0.42, -0.12)]))
    joined = ppp.join_erp([second, first], tmp_path / "joined.erp")
    assert joined.read_text() == first.read_text() + second.read_text()
    rows = [line.split()[0] for line in joined.read_text().splitlines() if line[:1].isdigit()]
    assert rows == ["53461.50", "53462.50", "53463.50"]
    page = tmp_path / "page.erp"
    page.write_text("<html>Not found</html>\n")
    with pytest.raises(ValueError, match="page.erp"):
        ppp.join_erp([first, page], tmp_path / "bad.erp")
    assert not (tmp_path / "bad.erp").exists()


# --- the base by static PPP (RTKLIB a stand-in) -----------------------------------------------------------------------


def _header_llh(path: Path) -> tuple[float, float, float]:
    return rinex.ecef_to_llh(rinex.header(path).approx_xyz)


def _given_products(folder: Path, days=(date(2005, 4, 2),)) -> ppp.Products:
    """Products of the days as a user would give them: the orbits plain, the clocks and the ERP packed in gzip."""
    folder.mkdir(parents=True, exist_ok=True)
    sp3, clk, erp = [], [], []
    for day in days:
        texts = _contents("ESA", "final", day)
        sp3.append(folder / f"orbits_{_doy(day)}.sp3")
        sp3[-1].write_text(texts["sp3"])
        clk.append(_gz(folder / f"clocks_{_doy(day)}.clk.gz", texts["clk"]))
        erp.append(_gz(folder / f"rotation_{_doy(day)}.erp.gz", texts["erp"]))
    return ppp.Products(sp3, clk, erp, "ESA", "final")


@pytest.fixture
def fake_ppp(monkeypatch):
    """RTKLIB replaced: each run is logged - its options, the names and SHA-256 of its inputs, the text of the ERP file
    and the SHA-256 of the antenna file the options name - and writes one single static row of quality ``q``, at
    ``llh`` with the deviations ``sd`` (sdn, sde, sdu, as RTKLIB writes them), or with ``rows`` False a solution without
    rows; its message is ``message``, or one of progress."""
    state = SimpleNamespace(runs=[], q=6, llh=_header_llh(OWN), sd=(0.012, 0.009, 0.031), message="", rows=True)

    def solve(conf, infiles, outfile):
        options = rtklib_options(Path(conf).read_text())
        state.runs.append({"options": options, "inputs": [Path(f).name for f in infiles],
                           "sha": [_sha256(Path(f)) for f in infiles],
                           "erp": Path(options["file-eopfile"]).read_text(),
                           "antenna": _sha256(Path(options["file-rcvantfile"])),
                           "folder": sorted(p.name for p in Path(infiles[0]).parent.iterdir())})
        lat, lon, h = state.llh
        rows = [(START_2005, lat, lon, h, state.q, 9, *state.sd)] if state.rows else []
        Path(outfile).write_text(pos_text(rows), newline="")
        return 0, state.message or f"processing : 2005/04/02 00:59:30 Q={state.q}"

    monkeypatch.setattr(_native, "ppk_solve", solve)
    return state


def _no_downloads(monkeypatch) -> None:
    def refused(*args, **kwargs):
        raise AssertionError("nothing is downloaded here")

    for module, name in ((atx, "atx_file"), (download, "fetch"), (download, "listing")):
        monkeypatch.setattr(module, name, refused)


def test_read_pos_reads_a_ppp_row(tmp_path):
    """A row of a PPP solution (Q 6, RTKLIB's "ppp") is read as any other."""
    path = tmp_path / "ppp.pos"
    path.write_text(pos_text([(START_2005, 35.1, 139.6, 75.0, 6, 9, 0.02, 0.01, 0.05)]), newline="")
    solution = ppk.read_pos(path)
    assert solution.q.tolist() == [6] and ppk.QUALITY[6] == "ppp"
    assert ppk.static_position(path) == ((35.1, 139.6, 75.0), 6, (0.01, 0.02, 0.05))


def test_ppp_result_from_a_solution(tmp_path, fake_ppp, monkeypatch):
    """The mark of the base where RTKLIB's single PPP row puts it, in ITRF2020 at the middle of the session, with
    RTKLIB's deviations east, north, up; RTKLIB given the observations, the orbits and the clocks - plain, under names
    that tell them apart - and, in its options, the antenna file and one ERP file; nothing downloaded."""
    _no_downloads(monkeypatch)
    products = _given_products(tmp_path / "given")
    r = ppp.solve_static(OWN, tmp_path / "work", products=products, atx=ATX_EXCERPT)
    p = r.position
    assert p.source == "ppp" and p.frame == "ITRF2020" and p.epoch == pytest.approx(MID_2005, abs=1e-9)
    lat, lon, h = _header_llh(OWN)  # as the file writes them: degrees to 1e-9, metres to 1e-4
    assert (p.lat, p.lon, p.h) == (pytest.approx(lat, abs=1e-9), pytest.approx(lon, abs=1e-9),
                                   pytest.approx(h, abs=1e-4))
    assert p.sd == (0.009, 0.012, 0.031)  # RTKLIB writes sdn, sde, sdu: east, north, up
    assert r.hours == 1.0 and r.products is products
    assert r.solution.parent == (tmp_path / "work").resolve() / "ppp" and r.solution.suffix == ".pos"
    assert r.solution.is_file() and not (tmp_path / "work" / "ppp" / ppk.INPUTS_FOLDER).exists()
    (run,) = fake_ppp.runs
    assert run["inputs"] == ["base.obs", "orbit1.sp3", "clock1.clk"]
    assert run["sha"] == [_sha256(OWN), _sha256(products.sp3[0]),
                          hashlib.sha256(gzip.decompress(products.clk[0].read_bytes())).hexdigest()]
    assert run["folder"] == sorted(["base.obs", "orbit1.sp3", "clock1.clk", "rotation.erp", "antennas.atx"])
    assert run["erp"] == _plain_text(products.erp[0]) and run["antenna"] == _sha256(ATX_EXCERPT)
    assert run["options"]["pos1-posmode"] == "ppp-static" and run["options"]["pos1-tropopt"] == "est-ztd"
    detail = p.detail
    assert detail["base"] == "3040" and detail["centre"] == "ESA" and detail["kind"] == "final"
    assert detail["products"] == ["orbits_2005092.sp3", "clocks_2005092.clk.gz", "rotation_2005092.erp.gz"]
    assert detail["hours"] == 1.0 and detail["q"] == 6 and detail["solution"] == r.solution.name
    assert detail["antenna"] == "TRM29659.00" and detail["antenna_delta_hen"] == [0.0, 0.0, 0.0]
    assert detail["antenna_calibration"] == {"file": "igs20_excerpt.atx", "entry": "TRM29659.00     NONE",
                                             "without_radome": False, "up_m": 0.1}
    assert "ESA" in detail["citation"] and "igs20_excerpt.atx" in detail["citation"]
    assert detail["session"] == ["2005-04-02 00:00:00", "2005-04-02 00:59:30"]


def test_ppp_joins_the_erp_of_the_days_into_one_file(tmp_path, fake_ppp):
    """Products of two days: the orbits and the clocks of each day, and the ERP of both in one file, in the order of
    the days, whatever the order given."""
    products = _given_products(tmp_path / "given", (date(2005, 4, 2), date(2005, 4, 3)))
    products.erp.reverse()
    ppp.solve_static(OWN, tmp_path / "work", products=products, atx=ATX_EXCERPT)
    (run,) = fake_ppp.runs
    assert run["inputs"] == ["base.obs", "orbit1.sp3", "orbit2.sp3", "clock1.clk", "clock2.clk"]
    assert run["erp"] == _plain_text(products.erp[1]) + _plain_text(products.erp[0])
    assert [line.split()[0] for line in run["erp"].splitlines() if line[:1].isdigit()] == ["53462.50", "53463.50"]


def _session(folder: Path, hours: float, start: datetime = datetime(2005, 4, 2)) -> Path:
    """The observations of 3040, whose hour is repeated, for ``hours`` from ``start`` every 30 s."""
    header, records = rinex_records(OWN)
    lines = list(header)
    for k in range(round(hours * 120)):
        record = records[k % len(records)]
        when = start + timedelta(seconds=30 * k)
        epoch = (f" {when.year % 100:02d} {when.month:2d} {when.day:2d} {when.hour:2d} {when.minute:2d}"
                 f"{when.second:11.7f}")
        lines += [epoch + record[0][26:], *record[1:]]
    path = folder / f"3040_{start:%Y%m%d%H}_{hours:g}h.{start:%y}o"
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", newline="\n")
    return path


@pytest.mark.parametrize("hours, short, station, gradients", [(1.5, True, True, False), (3.0, False, True, False),
                                                              (5.0, False, False, True)])
def test_short_session_warns(tmp_path, fake_ppp, caplog, hours, short, station, gradients):
    """Under 2 hours a PPP is 10-20 cm off; under 4 hours a static baseline from a station nearby is better: warnings,
    in the result and in the log. From 4 hours the gradients of the troposphere are estimated too."""
    obs = _session(tmp_path / "obs", hours)
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        r = ppp.solve_static(obs, tmp_path / "work", products=_given_products(tmp_path / "given"),
                             atx=ATX_EXCERPT)
    assert r.hours == hours
    logged = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    two = [w for w in r.warnings if "10-20 cm" in w]
    four = [w for w in r.warnings if "station" in w]
    assert len(two) == short and len(four) == station
    for warning in two + four:
        assert "3040" in warning and warning in logged
    if short:
        assert f"{ppp.SHORT_H:g} hours" in two[0] and "90 minutes" in two[0]
    if station:
        assert f"{ppp.STATION_BETTER_H:g} hours" in four[0]
    (run,) = fake_ppp.runs
    assert run["options"]["pos1-tropopt"] == ("est-ztdgrad" if gradients else "est-ztd")


def test_ppp_with_the_base_in_two_files(tmp_path, fake_ppp):
    """A base recorded in two files (given in any order) is the base of the whole session; the joined file goes."""
    header, records = rinex_records(OWN)
    first = rinex_piece(tmp_path / "first.05o", header, records[:60])
    second = rinex_piece(tmp_path / "second.05o", header, records[60:])
    r = ppp.solve_static([second, first], tmp_path / "work", products=_given_products(tmp_path / "given"),
                         atx=ATX_EXCERPT)
    assert r.hours == 1.0 and r.position.epoch == pytest.approx(MID_2005, abs=1e-9)
    assert sorted(p.name for p in (tmp_path / "work").iterdir()) == ["ppp"]


@pytest.mark.parametrize("q, what", [(5, "single"), (2, "float"), (1, "fix")])
def test_ppp_refuses_a_solution_that_is_not_ppp(tmp_path, fake_ppp, q, what):
    """RTKLIB-EX makes float PPP solutions (Q 6) only: a row of any other quality is no PPP - a single position is
    metres off - and is refused; the solution stays, named in the message, with what RTKLIB said, which is often
    why."""
    fake_ppp.q, fake_ppp.message = q, "error : no erp data rotation.erp"
    with pytest.raises(ValueError) as error:
        ppp.solve_static(OWN, tmp_path / "work", products=_given_products(tmp_path / "given"), atx=ATX_EXCERPT)
    text = str(error.value)
    assert f"Q={q}" in text and what in text and "3040" in text
    assert "error : no erp data rotation.erp" in text
    (kept,) = (tmp_path / "work" / "ppp").glob("*.pos")
    assert kept.name in text


def test_products_given_by_hand_must_cover_the_session(tmp_path, fake_ppp):
    """Products given by hand are checked against the session: orbits or clocks that leave part of it without products
    are refused - RTKLIB would compute nothing there, and the hours would claim it - naming the files, the time they
    cover and the time they leave out; the products of every day of the session pass."""
    obs = _session(tmp_path / "obs", 2.0, start=datetime(2005, 4, 1, 23))  # 23:00:00 to 00:59:30
    second_day = _given_products(tmp_path / "given", (date(2005, 4, 2),))
    with pytest.raises(ValueError) as error:
        ppp.solve_static(obs, tmp_path / "work", products=second_day, atx=ATX_EXCERPT)
    text = str(error.value)
    assert "orbits" in text and "orbits_2005092.sp3 covers 2005-04-02 00:00:00 to 2005-04-03 00:00:00" in text
    assert "60 minutes" in text and "2005-04-01 23:00:00 to 2005-04-02 00:00:00 GPS time" in text
    both = _given_products(tmp_path / "both", (date(2005, 4, 1), date(2005, 4, 2)))
    r = ppp.solve_static(obs, tmp_path / "work", products=both, atx=ATX_EXCERPT)
    assert r.hours == 2.0 and r.position.detail["hours"] == 2.0
    clocks_of_one_day = dataclasses.replace(both, clk=both.clk[1:])
    with pytest.raises(ValueError, match="clocks") as error:
        ppp.solve_static(obs, tmp_path / "work", products=clocks_of_one_day, atx=ATX_EXCERPT)
    assert "clocks_2005092.clk.gz" in str(error.value) and "60 minutes" in str(error.value)
    # clocks that stop at 00:30:00: the last half hour of the hour of 3040 has none
    stopping = _given_products(tmp_path / "stopping")
    stopping.clk[0].write_bytes(gzip.compress(_clk_text(date(2005, 4, 2), last=(0, 30, 0)).encode()))
    with pytest.raises(ValueError) as error:
        ppp.solve_static(OWN, tmp_path / "work", products=stopping, atx=ATX_EXCERPT)
    assert "29 minutes" in str(error.value) and "2005-04-02 00:30:30 to 2005-04-02 00:59:30" in str(error.value)
    assert len(fake_ppp.runs) == 1


def test_products_before_igs20_warn_of_their_frame_and_antennas(tmp_path, fake_ppp):
    """Before GPS week 2238 (27 November 2022) the products of the IGS and its centres are of IGb14, made for
    igs14.atx: a position computed with igs20.atx and called ITRF2020 can be centimetres off, and is said to be."""
    r = ppp.solve_static(OWN, tmp_path / "old", products=_given_products(tmp_path / "given"), atx=ATX_EXCERPT)
    (note,) = [w for w in r.warnings if "IGb14" in w]
    assert "igs14.atx" in note and "igs20_excerpt.atx" in note and "ITRF2020" in note and "centimetres" in note
    later = _session(tmp_path / "obs", 1.0, start=datetime(2026, 10, 26, 9))
    r = ppp.solve_static(later, tmp_path / "new", products=_given_products(tmp_path / "given26", (date(2026, 10, 26),)),
                         atx=ATX_EXCERPT)
    assert r.position.epoch == pytest.approx(frames.decimal_year(datetime(2026, 10, 26, 9, 29, 27)), abs=1e-9)
    assert not [w for w in r.warnings if "IGb14" in w]


def test_ppp_needs_the_antenna_calibrations(tmp_path, fake_ppp, monkeypatch):
    """No PPP without igs20.atx: the precise orbits are of the satellites' centres of mass, their antennas are up to
    metres away. With "auto" it is downloaded once into the folder of the program; a file given is used as it is."""
    products = _given_products(tmp_path / "given")
    with pytest.raises(ValueError, match="satellites"):
        ppp.solve_static(OWN, tmp_path / "work", products=products, atx=None)
    asked: list[tuple] = []
    kept = tmp_path / "kept" / "igs20.atx"
    kept.parent.mkdir()
    shutil.copyfile(ATX_EXCERPT, kept)

    def antennas(refresh=False, progress=None):
        asked.append(("atx", refresh))
        return kept

    monkeypatch.setattr(atx, "atx_file", antennas)
    ppp.solve_static(OWN, tmp_path / "work", products=products)
    assert asked == [("atx", False)] and fake_ppp.runs[-1]["antenna"] == _sha256(ATX_EXCERPT)

    def offline(refresh=False, progress=None):
        raise download.DownloadError("https://files.igs.org/pub/station/general/igs20.atx: no network", "")

    monkeypatch.setattr(atx, "atx_file", offline)
    with pytest.raises(ValueError, match="igs20.atx") as error:
        ppp.solve_static(OWN, tmp_path / "work", products=products)
    assert "no network" in str(error.value)
    with pytest.raises(ValueError, match="not a file"):
        ppp.solve_static(OWN, tmp_path / "work", products=products, atx=tmp_path / "missing.atx")
    page = tmp_path / "page.atx"
    page.write_text("<html>login</html>\n")
    with pytest.raises(ValueError, match="ANTEX"):
        ppp.solve_static(OWN, tmp_path / "work", products=products, atx=page)
    assert len(fake_ppp.runs) == 1


def _antenna(folder: Path, kind: str) -> Path:
    """A copy of 3040 whose header names the antenna ``kind`` (type and radome)."""
    folder.mkdir(parents=True, exist_ok=True)
    return rewrite_header(OWN, folder / OWN.name, on_label("ANT # / TYPE", f"{'':20}{kind}"))


def test_ppp_warns_of_an_antenna_without_its_calibration(tmp_path, fake_ppp, caplog):
    """In a PPP the whole offset of the phase centre of the antenna goes into the height: an antenna that is not in
    the antenna file, or not with its radome, is a warning; an antenna that RTKLIB's search would give the offsets of
    a satellite is refused."""
    products = _given_products(tmp_path / "given")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        r = ppp.solve_static(_antenna(tmp_path / "absent", "TRM41249.00     NONE"), tmp_path / "w1",
                             products=products, atx=ATX_EXCERPT)
    (note,) = [w for w in r.warnings if "TRM41249.00" in w]
    assert "igs20_excerpt.atx" in note and "too high" in note and "height" in note
    assert note in [record.getMessage() for record in caplog.records]
    assert r.position.detail["antenna_calibration"]["entry"] is None
    r = ppp.solve_static(_antenna(tmp_path / "radome", "TRM57971.00     SCIS"), tmp_path / "w2",
                         products=products, atx=ATX_EXCERPT)
    (note,) = [w for w in r.warnings if "TRM57971.00" in w]
    assert "radome" in note and "TRM57971.00 NONE" in note
    r = ppp.solve_static(OWN, tmp_path / "w3", products=products, atx=ATX_EXCERPT)
    assert not [w for w in r.warnings if "TRM29659.00" in w]
    with pytest.raises(ValueError, match="satellite"):
        ppp.solve_static(_antenna(tmp_path / "satellite", "BLOCK IIA"), tmp_path / "w4", products=products,
                         atx=ATX_EXCERPT)


def test_ppp_with_rapid_products_warns(tmp_path, fake_ppp):
    products = dataclasses.replace(_given_products(tmp_path / "given"), centre="IGS", kind="rapid")
    r = ppp.solve_static(OWN, tmp_path / "work", products=products, atx=ATX_EXCERPT)
    (note,) = [w for w in r.warnings if "rapid" in w]
    assert "final" in note and "ESA" in note


def test_an_error_rtklib_reports_with_its_solution_is_a_warning(tmp_path, fake_ppp):
    fake_ppp.message = "error : no erp data rotation.erp"
    r = ppp.solve_static(OWN, tmp_path / "work", products=_given_products(tmp_path / "given"), atx=ATX_EXCERPT)
    assert "RTKLIB: error : no erp data rotation.erp" in r.warnings


def test_a_run_that_computes_nothing_is_an_error_and_leaves_nothing(tmp_path, fake_ppp):
    fake_ppp.rows, fake_ppp.message = False, "error : no obs data"
    with pytest.raises(ValueError, match="no PPP solution: error : no obs data"):
        ppp.solve_static(OWN, tmp_path / "work", products=_given_products(tmp_path / "given"), atx=ATX_EXCERPT)
    assert list((tmp_path / "work" / "ppp").iterdir()) == []


def test_solve_static_downloads_the_products_it_is_not_given(archives, tmp_path, fake_ppp, monkeypatch):
    """Without products, those of the day of the session are downloaded into the folder of the PPP, beside the folder
    RTKLIB works in."""
    day = date(2005, 4, 2)
    archives.put("ESA", "final", day)
    monkeypatch.setattr(atx, "atx_file", lambda refresh=False, progress=None: ATX_EXCERPT)
    r = ppp.solve_static(OWN, tmp_path / "work")
    assert (r.products.centre, r.products.kind) == ("ESA", "final")
    assert r.products.sp3 == [tmp_path / "work" / "products" / "ESA0OPSFIN_20050920000_01D_05M_ORB.SP3"]
    (run,) = fake_ppp.runs
    assert run["sha"][1] == _sha256(r.products.sp3[0]) and run["sha"][2] == _sha256(r.products.clk[0])
    assert r.position.detail["centre"] == "ESA"
    assert r.position.detail["products"] == ["ESA0OPSFIN_20050920000_01D_05M_ORB.SP3",
                                             "ESA0OPSFIN_20050920000_01D_30S_CLK.CLK",
                                             "ESA0OPSFIN_20050920000_01D_01D_ERP.ERP"]


def test_solve_static_refuses_inputs_in_its_work_folder(tmp_path, fake_ppp):
    """RTKLIB's folder is emptied before every run: nothing given may be in it."""
    work = tmp_path / "work"
    inside = work / "ppp" / ppk.INPUTS_FOLDER
    inside.mkdir(parents=True)
    obs = Path(shutil.copyfile(OWN, inside / OWN.name))
    with pytest.raises(ValueError, match="work folder"):
        ppp.solve_static(obs, work, products=_given_products(tmp_path / "given"), atx=ATX_EXCERPT)
    assert obs.is_file() and fake_ppp.runs == []


# --- the base by static PPP, with RTKLIB ------------------------------------------------------------------------------

_MU, _OMEGA_E = 3.986005e14, 7.2921151467e-5  # the Earth's gravitational constant and rotation rate of GPS (IS-GPS-200)
_GPS_EPOCH = datetime(1980, 1, 6)
_PRODUCTS_SPAN = (datetime(2005, 4, 1, 23, 0), datetime(2005, 4, 2, 2, 0))  # the hour of 3040 and margins


def _ephemerides(path: Path) -> list[tuple[int, datetime, list[float]]]:
    """The messages of a RINEX 2 navigation file of GPS: satellite, time of its clock, the numbers of the message (a
    blank one 0)."""
    lines = path.read_text().splitlines()
    body = lines[next(i for i, line in enumerate(lines) if "END OF HEADER" in line) + 1:]
    found = []
    for k in range(0, len(body) - 7, 8):
        first = body[k]
        toc = datetime(2000 + int(first[2:5]), int(first[5:8]), int(first[8:11]), int(first[11:14]),
                       int(first[14:17])) + timedelta(seconds=float(first[17:22]))
        fields = [first[22 + 19 * j:41 + 19 * j] for j in range(3)]
        fields += [line[3 + 19 * j:22 + 19 * j] for line in body[k + 1:k + 8] for j in range(4)]
        found.append((int(first[:2]), toc, [float(f.replace("D", "E")) if f.strip() else 0.0 for f in fields]))
    return found


def _orbit(message: list[float], when: datetime) -> np.ndarray:
    """The position (ECEF, m) of a GPS satellite at ``when`` (GPS time) by its broadcast message."""
    (_af0, _af1, _af2, _iode, crs, dn, m0, cuc, e, cus, sqrt_a, toe, cic, omega0, cis, i0, crc, w, omega_dot,
     idot) = message[:20]
    a = sqrt_a ** 2
    tk = ((when - _GPS_EPOCH).total_seconds() - toe + WEEK / 2) % WEEK - WEEK / 2
    mean = m0 + (math.sqrt(_MU / a ** 3) + dn) * tk
    eccentric = mean
    for _ in range(30):
        eccentric = mean + e * math.sin(eccentric)
    phi = math.atan2(math.sqrt(1 - e * e) * math.sin(eccentric), math.cos(eccentric) - e) + w
    u = phi + cus * math.sin(2 * phi) + cuc * math.cos(2 * phi)
    r = a * (1 - e * math.cos(eccentric)) + crs * math.sin(2 * phi) + crc * math.cos(2 * phi)
    i = i0 + idot * tk + cis * math.sin(2 * phi) + cic * math.cos(2 * phi)
    node = omega0 + (omega_dot - _OMEGA_E) * tk - _OMEGA_E * toe
    x, y = r * math.cos(u), r * math.sin(u)
    return np.array([x * math.cos(node) - y * math.cos(i) * math.sin(node),
                     x * math.sin(node) + y * math.cos(i) * math.cos(node), y * math.sin(i)])


def _broadcast_products(folder: Path) -> dict[str, Path]:
    """Orbits every 5 minutes (SP3-c) and clocks every 30 s (RINEX clock) of the GPS satellites from the broadcast
    messages of the sample day, over the hour of 3040 and an hour or so on each side, and an ERP file: every satellite
    of the SP3 header has a line at every epoch - zeros and the clock 999999.999999 when it has no message then - as
    RTKLIB reads exactly as many lines after an epoch as the header has satellites."""
    messages = _ephemerides(NAV)
    satellites = sorted({prn for prn, _, _ in messages})

    def message(prn: int, when: datetime):
        near = [(abs((when - toc).total_seconds()), toc, numbers) for p, toc, numbers in messages if p == prn]
        near = [m for m in near if m[0] <= 7200.0]
        return min(near, key=lambda m: m[0])[1:] if near else None

    def clock(numbers: list[float], toc: datetime, when: datetime) -> float:
        dt = (when - toc).total_seconds()
        return numbers[0] + numbers[1] * dt + numbers[2] * dt * dt

    start, end = _PRODUCTS_SPAN
    epochs = [start + timedelta(minutes=5 * k) for k in range(int((end - start).total_seconds() // 300) + 1)]
    ids = [f"G{prn:02d}" for prn in satellites]
    seconds = (start - _GPS_EPOCH).total_seconds()
    sp3 = [f"#cP{start:%Y} {start.month:2d} {start.day:2d} {start.hour:2d} {start.minute:2d}  0.00000000 "
           f"{len(epochs):6d} ORBIT IGS20 BCT  PPG",
           f"## {int(seconds // WEEK):4d} {seconds % WEEK:15.8f} {300.0:14.8f} {_mjd(start.date()):5d} "
           f"{start.hour / 24:15.13f}"]
    for k in range(5):
        chunk = ids[17 * k:17 * (k + 1)]
        sp3.append((f"+  {len(ids):3d}   " if k == 0 else "+        ") + "".join(chunk) + "  0" * (17 - len(chunk)))
    sp3 += ["++       " + "  0" * 17] * 5
    sp3 += ["%c G  cc GPS ccc cccc cccc cccc cccc ccccc ccccc ccccc ccccc",
            "%c cc cc ccc ccc cccc cccc cccc cccc ccccc ccccc ccccc ccccc",
            "%f  1.2500000  1.025000000  0.00000000000  0.000000000000000",
            "%f  0.0000000  0.000000000  0.00000000000  0.000000000000000",
            "%i    0    0    0    0      0      0      0      0         0",
            "%i    0    0    0    0      0      0      0      0         0",
            "/* orbits and clocks of the broadcast messages of 2005-04-02, for a test", "/*", "/*", "/*"]
    for when in epochs:
        sp3.append(f"*  {when:%Y} {when.month:2d} {when.day:2d} {when.hour:2d} {when.minute:2d}  0.00000000")
        for prn in satellites:
            found = message(prn, when)
            if found is None:
                sp3.append(f"PG{prn:02d}{0.0:14.6f}{0.0:14.6f}{0.0:14.6f}{999999.999999:14.6f}")
                continue
            toc, numbers = found
            x, y, z = _orbit(numbers, when) / 1000.0
            sp3.append(f"PG{prn:02d}{x:14.6f}{y:14.6f}{z:14.6f}{clock(numbers, toc, when) * 1e6:14.6f}")
    sp3.append("EOF")
    clk = ["     3.00           C                   G                   RINEX VERSION / TYPE",
           "     1    AS                                                # / TYPES OF DATA",
           " " * 60 + "END OF HEADER"]
    when = start
    while when <= end:
        for prn in satellites:
            found = message(prn, when)
            if found is not None:
                clk.append(f"AS G{prn:02d}  {when:%Y %m %d %H %M} {when.second:9.6f}  1   "
                           f"{clock(found[1], found[0], when):19.12E}")
        when += timedelta(seconds=30)
    folder.mkdir(parents=True, exist_ok=True)
    files = {"sp3": folder / "broadcast.sp3", "clk": folder / "broadcast.clk", "erp": folder / "broadcast.erp"}
    files["sp3"].write_text("\n".join(sp3) + "\n")
    files["clk"].write_text("\n".join(clk) + "\n")
    files["erp"].write_text(_erp_text([(53461.5, 0.05, 0.40, -0.55), (53462.5, 0.06, 0.41, -0.56),
                                       (53463.5, 0.07, 0.42, -0.57)]))
    return files


@pytest.fixture(scope="module")
def broadcast(tmp_path_factory) -> dict[str, Path]:
    return _broadcast_products(tmp_path_factory.mktemp("broadcast"))


def _products_of(files: dict[str, Path], erp=None) -> ppp.Products:
    return ppp.Products([files["sp3"]], [files["clk"]], erp or [files["erp"]], "broadcast", "final")


def _llh(position) -> np.ndarray:
    return np.array([position.lat, position.lon, position.h])


@needs_rtklib
def test_ppp_gives_the_mark_under_the_antenna(tmp_path, broadcast, monkeypatch):
    """RTKLIB's static PPP of 3040 on orbits and clocks as good as the broadcast ones: a position of quality 6 within
    metres of its header, and the mark under the antenna - the antenna height of the RINEX header taken off: a header
    that puts the antenna 1.5 m above the mark gives a mark 1.5 m lower, in the same place across. The antenna not in
    the file of calibrations, its phase centre - 0.100 m above its reference point in the excerpt - is taken for the
    mark: the mark found 0.100 m too high, all of it in the height."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    products = _products_of(broadcast)
    plain = ppp.solve_static(OWN, tmp_path / "plain", products=products, atx=ATX_EXCERPT)
    assert not [w for w in plain.warnings if w.startswith("RTKLIB")], plain.warnings
    offset = enu(_llh(plain.position), _header_llh(OWN))[0]  # broadcast orbits and clocks: metres, not centimetres
    assert math.hypot(offset[0], offset[1]) < 5.0 and abs(offset[2]) < 5.0, offset
    assert plain.position.detail["q"] == 6 and plain.position.sd is not None
    raised = tmp_path / "raised" / OWN.name
    raised.parent.mkdir()
    rewrite_header(OWN, raised, on_label("ANTENNA: DELTA H/E/N", f"{1.5:14.4f}{0.0:14.4f}{0.0:14.4f}"))
    assert rinex.header(raised).antenna_delta_hen == (1.5, 0.0, 0.0)
    lower = ppp.solve_static(raised, tmp_path / "lower", products=products, atx=ATX_EXCERPT)
    offset = enu(_llh(lower.position), _llh(plain.position))[0]
    assert np.allclose(offset, [0.0, 0.0, -1.5], rtol=0, atol=0.005), offset
    absent = ppp.solve_static(_antenna(tmp_path / "absent", "TRM41249.00     NONE"), tmp_path / "absent-work",
                              products=products, atx=ATX_EXCERPT)
    offset = enu(_llh(absent.position), _llh(plain.position))[0]
    assert np.allclose(offset, [0.0, 0.0, 0.100], rtol=0, atol=0.002), offset


@needs_rtklib
def test_rtklib_reads_the_erp_of_the_days_joined(tmp_path, broadcast, monkeypatch):
    """The ERP of two days in two files, given in the wrong order, joined into one: RTKLIB reads all of it, as the
    one file of the same rows - the same position to the last digit. That it reads the second day's rows shows when
    the pole of that day is moved by 3 arcseconds: the pole tide moves the mark by centimetres."""
    monkeypatch.delenv("PPG_RNX2RTKP", raising=False)
    one = ppp.solve_static(OWN, tmp_path / "one", products=_products_of(broadcast), atx=ATX_EXCERPT)
    first, second = tmp_path / "day1.erp", tmp_path / "day2.erp"
    first.write_text(_erp_text([(53461.5, 0.05, 0.40, -0.55)]))
    second.write_text(_erp_text([(53462.5, 0.06, 0.41, -0.56), (53463.5, 0.07, 0.42, -0.57)]))
    two = ppp.solve_static(OWN, tmp_path / "two", products=_products_of(broadcast, [second, first]), atx=ATX_EXCERPT)
    assert not [w for w in two.warnings if w.startswith("RTKLIB")], two.warnings
    assert np.array_equal(_llh(two.position), _llh(one.position))
    second.write_text(_erp_text([(53462.5, 3.06, 3.41, -0.56), (53463.5, 3.07, 3.42, -0.57)]))
    moved = ppp.solve_static(OWN, tmp_path / "moved", products=_products_of(broadcast, [second, first]),
                             atx=ATX_EXCERPT)
    assert np.linalg.norm(enu(_llh(moved.position), _llh(one.position))[0]) > 0.01
