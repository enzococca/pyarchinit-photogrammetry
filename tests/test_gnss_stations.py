"""Permanent GNSS stations (EPN, IGS, RING): the list, the nearest ones and their coordinates at the epoch of a flight,
the download helpers they use, and the observation and navigation files of a station for the hours of a flight. The
services are never reached: the URL constants of ``gnss/stations.py`` point at files (``file://``) - the excerpts of
tests/data/gnss, captured on 2026-10-27 from M3G, the IGS, RING and the EPN Central Bureau (its multi-year solution
in columns and its catalogue of discontinuities), and a synthetic EPN solution in SINEX (see the README there) - or at
trees of files that the tests write in the layout of the archives of BKG and RING."""
from __future__ import annotations

import functools
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import socket
import ssl
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyproj
import pytest

from pyarchinit_photogrammetry import __version__, _native
from pyarchinit_photogrammetry.core.progress import Cancelled, Progress
from pyarchinit_photogrammetry.gnss import download, rinex, stations

DATA = Path(__file__).parent / "data" / "gnss"
ROME = (41.9028, 12.4964)

# The numbers of the fixtures, copied from the files by hand (the parsers are checked against them): x0 (m) at the
# reference epoch t0, the velocity v (m/y), and the 1-sigma of x0 and of v for the formal error at another epoch.
AQUI_EPN = SimpleNamespace(x0=(4592507.5523, 1089876.3523, 4276392.9031), v=(-0.01455, 0.01808, 0.01215), t0=2015.0,
                           sx0=(8e-4, 6e-4, 7e-4), sv=(1.9e-4, 1.7e-4, 1.8e-4))
M0SE_EPN = {1: SimpleNamespace(x0=(4642432.52, 1028629.389, 4236854.206), v=(-0.01499, 0.01784, 0.01196), t0=2015.0),
            2: SimpleNamespace(x0=(4642432.531, 1028629.382, 4236854.209), v=(-0.01499, 0.01784, 0.01196), t0=2015.0)}
BSHM_IGS = SimpleNamespace(  # solution 4, valid from 2023 day 38 to 2025 day 75
    x0=(4395951.15585911, 3080707.24503006, 3433498.28069637),
    v=(-0.0236629173713173, 0.0122074062138220, 0.0197278101932037), t0=2020.0,
    sx0=(5.34490e-3, 4.19440e-3, 4.30940e-3), sv=(1.26590e-3, 9.90940e-4, 1.02120e-3))
ADIS_IGS = SimpleNamespace(  # solution 3, valid from 2022 day 103 to 2024 day 362: the last one that IGc20.ssc holds
    x0=(4913652.57570628, 3945922.84024369, 995383.512247445),
    v=(-0.0231480595363631, 0.0124181475699407, 0.0168693261520704), t0=2020.0)
DRAG_IGS = SimpleNamespace(  # solution 2, from 2023 day 37 (the earthquake of Kahramanmaras)
    x0=(4432980.34795523, 3149432.30002256, 3322110.74250749),
    v=(-0.0198569875254135, 0.0142578667509555, 0.0187333162957293), t0=2020.0)
MEDI_IGS = {  # solutions 1 (1997 day 26 to 2000 day 320) and 2 (from 2000 day 320)
    1: SimpleNamespace(x0=(4461400.47523980, 919593.864627125, 4449504.94200374),
                       v=(-0.0180923962952918, 0.0191334696817523, 0.0113657719780606), t0=2020.0),
    2: SimpleNamespace(x0=(4461400.47305538, 919593.864907257, 4449504.93785650),
                       v=(-0.0180923845960119, 0.0191334642564910, 0.0113657614008432), t0=2020.0)}
# The live solution of the EPN in columns (release C2415, tests/data/gnss): MEDI00ITA's solution 2, valid since 2000
# day 320, and AQUI00ITA's solutions 2 to 4 around the earthquake of L'Aquila (2009 day 96, 01:32:39) and a new antenna
# (2017 day 327, 12:00), copied from the excerpt by hand.
EPN_COLUMNS = "EUR0OPSSNX_1996001_2026115_00U_SOL.SSC"
MEDI_EPN = SimpleNamespace(x0=(4461400.47249, 919593.86400, 4449504.93760), v=(-0.01815, 0.01907, 0.01138), t0=2020.0,
                           sx0=(6e-5, 2e-5, 6e-5), sv=(1e-5, 1e-5, 1e-5))
AQUI_COLUMNS = {
    2: SimpleNamespace(x0=(4592507.32230, 1089876.63406, 4276393.18332), v=(-0.01650, 0.01816, 0.01254), t0=2020.0),
    3: SimpleNamespace(x0=(4592507.29408, 1089876.63681, 4276393.08471), v=(-0.01650, 0.01815, 0.01253), t0=2020.0),
    4: SimpleNamespace(x0=(4592507.29569, 1089876.63124, 4276393.08072), v=(-0.01650, 0.01815, 0.01254), t0=2020.0)}


def _at(solution, epoch):
    return np.array(solution.x0) + np.array(solution.v) * (epoch - solution.t0)


def _uri(path) -> str:
    return Path(path).as_uri()


def _decimal_year(when: datetime) -> float:
    start = datetime(when.year, 1, 1)
    return when.year + (when - start) / (datetime(when.year + 1, 1, 1) - start)


@pytest.fixture(autouse=True)
def _own_gnss_folder(tmp_path, monkeypatch):
    """Every test has a GNSS folder of its own, and no listing kept in memory by another."""
    monkeypatch.setenv("PPG_GNSS_DIR", str(tmp_path / "gnss"))
    download._LISTINGS.clear()
    yield
    download._LISTINGS.clear()


def _shifted(text: str, dx: float) -> str:
    """An SSC with ``dx`` metres added to every STAX: another solution, for the files that must not be taken."""
    def shift(match):
        return f"{match.group(1)} {float(match.group(2)) + dx:21.14e}{match.group(3)}"

    return re.sub(r"^(.* STAX .* m +2) +(\S+)( +\S+)$", shift, text, flags=re.M)


@pytest.fixture
def archives(tmp_path, monkeypatch):
    """The services as trees of files: the lists and the solution of the IGS from tests/data/gnss, the folder of the
    reference frame of the EPN with two weekly folders and the files that must not be taken (the other frames, the
    short file), and the folder of the RING series, empty until a test writes a series."""
    synthetic = (DATA / "EPN_synthetic.SSC").read_text()
    epn = tmp_path / "epncb" / "referenceframe"
    for week, end, text in (("C2399", "2025263", _shifted(synthetic, 0.05)), ("C2400", "2025270", synthetic)):
        folder = epn / week
        folder.mkdir(parents=True)
        name = f"EUR0OPSSNX_1996001_{end}_00U_SOL"
        (folder / f"{name}.SSC").write_text(text)
        for frame in ("_ETRF2000", "_ETRF2014", "_ETRF2020"):
            (folder / f"{name}{frame}.SSC").write_text(_shifted(text, 1.0))
        (folder / f"{name}_short.SSC").write_text(_shifted(text, 2.0))
        (folder / f"{name}.SNX.gz").write_bytes(b"\x1f\x8b")
    (epn / "C2400.txt").write_text("notes")
    ring = tmp_path / "ring"
    ring.mkdir()
    monkeypatch.setattr(stations, "M3G_GEOJSON", _uri(DATA) + "/m3g_{network}.geojson")
    monkeypatch.setattr(stations, "IGS_NETWORK", _uri(DATA / "IGSNetwork.json"))
    monkeypatch.setattr(stations, "IGS_SSC", _uri(DATA / "IGc20.ssc"))
    monkeypatch.setattr(stations, "EPN_REFERENCE_FRAME", _uri(epn) + "/")
    monkeypatch.setattr(stations, "RING_POS", _uri(ring) + "/{id}/{id}.gipsyX.IGS20.pos")
    return SimpleNamespace(root=tmp_path, epn=epn, ring=ring)


def _station(sid: str) -> stations.Station:
    return {s.id: s for s in stations.station_list()}[sid]


def _fetches(monkeypatch) -> list[str]:
    """The addresses ``download.fetch`` is asked for from now on."""
    calls: list[str] = []
    real = download.fetch

    def spy(url, *args, **kwargs):
        calls.append(url)
        return real(url, *args, **kwargs)

    monkeypatch.setattr(download, "fetch", spy)
    return calls


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def _m3g(name: str) -> dict:
    return {f["properties"]["id"]: f["properties"] for f in json.loads((DATA / name).read_text())}


# --- the list of stations ---------------------------------------------------------------------------------------


def test_station_list_merges_networks(archives, tmp_path, monkeypatch):
    listed = {s.id: s for s in stations.station_list()}
    assert sorted(listed) == ["ADIS00ETH", "AQUI00ITA", "ASIR00ITA", "BSHM00ISR", "DRAG00ISR", "INGR00ITA",
                              "M0SE00ITA", "MARN00ITA", "MEDI00ITA", "RMPO00ITA", "ZNTE00GRC"]
    assert listed["DRAG00ISR"].networks == ("EPN", "IGS") and listed["ADIS00ETH"].networks == ("IGS",)
    assert all(len(s.id) == 9 and s.id == s.id.upper() for s in listed.values())
    # in the lists of the EPN and of the IGS: one station with the two networks, the position of the site log
    moses = listed["M0SE00ITA"]
    assert moses.networks == ("EPN", "IGS")
    site = _m3g("m3g_EPN.geojson")["M0SE00ITA"]["location"]
    assert moses.xyz == tuple(site["approximatePositionITRF"][k] for k in "XYZ")
    grs80 = site["approximatePositionGRS80"]
    assert (moses.lat, moses.lon, moses.h) == (grs80["latitude"], grs80["longitude"], grs80["elevation"])
    assert moses.name == "Rome - The 'Moses' of Michelangelo"
    assert listed["INGR00ITA"].networks == ("RING",) and listed["INGR00ITA"].name == "Roma"
    # a station of the IGS alone: no name in the list, and its latitude, longitude and height come from the XYZ (the
    # strings of the file have three decimals)
    bshm = listed["BSHM00ISR"]
    assert bshm.networks == ("IGS",) and bshm.name == "BSHM00ISR"
    lon, lat, h = pyproj.Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True).transform(*bshm.xyz)
    assert (bshm.lat, bshm.lon, bshm.h) == pytest.approx((lat, lon, h), abs=1e-7)
    assert abs(bshm.lat - 32.779) < 0.001 and abs(bshm.lon - 35.023) < 0.001
    # a station that the EPN removed (PADO00ITA, 2025-04-30) is not offered
    assert "PADO00ITA" not in listed

    # a station that the lists of the EPN and of RING both hold is one station with the three networks (M3G holds
    # none in both today: a station can change network)
    both = tmp_path / "both"
    both.mkdir()
    shutil.copy(DATA / "m3g_EPN.geojson", both / "m3g_EPN.geojson")
    ring = json.loads((DATA / "m3g_RING.geojson").read_text())
    ring.append(next(f for f in json.loads((DATA / "m3g_EPN.geojson").read_text())
                     if f["properties"]["id"] == "M0SE00ITA"))
    (both / "m3g_RING.geojson").write_text(json.dumps(ring))
    monkeypatch.setattr(stations, "M3G_GEOJSON", _uri(both) + "/m3g_{network}.geojson")
    merged = [s for s in stations.station_list(refresh=True) if s.id == "M0SE00ITA"]
    assert [s.networks for s in merged] == [("EPN", "IGS", "RING")]


def test_names_that_are_not_stations_are_left_out(archives, tmp_path, monkeypatch):
    """An id ends up in the name of a file in the cache: nine letters or digits, nothing else."""
    folder = tmp_path / "odd"
    folder.mkdir()
    shutil.copy(DATA / "m3g_EPN.geojson", folder / "m3g_EPN.geojson")
    features = json.loads((DATA / "m3g_RING.geojson").read_text())
    for odd in ("../../x/y", "short", "A B C D E"):
        features.append(json.loads(json.dumps(features[0])))
        features[-1]["properties"]["id"] = odd
    (folder / "m3g_RING.geojson").write_text(json.dumps(features))
    igs = json.loads((DATA / "IGSNetwork.json").read_text())
    igs["../../x/y"] = igs["BSHM00ISR"]
    (folder / "IGSNetwork.json").write_text(json.dumps(igs))
    monkeypatch.setattr(stations, "M3G_GEOJSON", _uri(folder) + "/m3g_{network}.geojson")
    monkeypatch.setattr(stations, "IGS_NETWORK", _uri(folder / "IGSNetwork.json"))
    assert len(stations.station_list()) == 11


def test_station_list_reads_a_feature_collection_too(archives, tmp_path, monkeypatch):
    """M3G answers with an array of features; a GeoJSON FeatureCollection is read as well."""
    folder = tmp_path / "collection"
    folder.mkdir()
    for network in ("EPN", "RING"):
        features = json.loads((DATA / f"m3g_{network}.geojson").read_text())
        (folder / f"m3g_{network}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    monkeypatch.setattr(stations, "M3G_GEOJSON", _uri(folder) + "/m3g_{network}.geojson")
    assert len(stations.station_list()) == 11


def test_a_station_is_removed_from_the_day_it_is_removed(archives, tmp_path, monkeypatch):
    features = json.loads((DATA / "m3g_EPN.geojson").read_text())
    for feature in features:
        if feature["properties"]["id"] == "PADO00ITA":
            feature["properties"]["siteForm"]["dateRemoved"] = "2999-01-01T00:00Z"
    folder = tmp_path / "later"
    folder.mkdir()
    (folder / "m3g_EPN.geojson").write_text(json.dumps(features))
    shutil.copy(DATA / "m3g_RING.geojson", folder / "m3g_RING.geojson")
    monkeypatch.setattr(stations, "M3G_GEOJSON", _uri(folder) + "/m3g_{network}.geojson")
    assert "PADO00ITA" in {s.id for s in stations.station_list()}


def test_station_list_is_kept_for_seven_days(archives, monkeypatch):
    calls = _fetches(monkeypatch)
    now = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(stations, "_now", lambda: now)
    first = stations.station_list()
    assert len(calls) == 3  # the list of the EPN, the one of RING and the one of the IGS
    saved = download.gnss_dir() / "stations" / "stations.json"
    assert json.loads(saved.read_text())["fetched"].startswith("2026-10-27T09:00:00")
    assert sorted(p.name for p in saved.parent.iterdir()) == ["stations.json"]  # nothing of the downloads is left

    monkeypatch.setattr(stations, "_now", lambda: now + timedelta(days=6, hours=23))
    assert stations.station_list() == first and len(calls) == 3
    monkeypatch.setattr(stations, "_now", lambda: now + timedelta(days=7, minutes=1))
    assert stations.station_list() == first and len(calls) == 6  # old: fetched again
    assert stations.station_list(refresh=True) == first and len(calls) == 9


def test_station_list_reports_progress(archives):
    seen: list[float] = []
    stations.station_list(progress=lambda fraction, message: seen.append(fraction))
    assert seen == sorted(seen) and seen[-1] == 1.0 and len(seen) >= 3


def test_offline_uses_the_cached_list(archives, monkeypatch, caplog):
    first = stations.station_list()
    monkeypatch.setattr(stations, "M3G_GEOJSON", _uri(archives.root / "gone") + "/m3g_{network}.geojson")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        again = stations.station_list(refresh=True)
    assert again == first
    warned = _warnings(caplog)
    assert warned and "saved" in warned[0] and "m3g_EPN.geojson" in warned[0]  # which address, and the copy used
    # a list that is old is used in the same way when the network is gone
    monkeypatch.setattr(stations, "_now", lambda: datetime.now(timezone.utc) + timedelta(days=30))
    assert stations.station_list() == first

    # without a copy there is nothing to use: the error says which address failed
    shutil.rmtree(download.gnss_dir() / "stations")
    with pytest.raises(download.DownloadError) as error:
        stations.station_list()
    assert "m3g_EPN.geojson" in str(error.value) and error.value.url.endswith("m3g_EPN.geojson")


def test_an_empty_list_is_not_kept(archives, tmp_path, monkeypatch):
    """An answer of M3G or of the IGS with no station at all (an empty array, an empty object: a service that answers
    but has nothing to say) is not a list of the stations: it is not saved for a week, and a list saved before is used."""
    for network, empty in (("EPN", "[]"), ("IGS", "{}")):
        folder = tmp_path / f"empty_{network}"
        folder.mkdir()
        for other in ("EPN", "RING"):
            text = empty if other == network else (DATA / f"m3g_{other}.geojson").read_text()
            (folder / f"m3g_{other}.geojson").write_text(text)
        (folder / "IGSNetwork.json").write_text(empty if network == "IGS" else (DATA / "IGSNetwork.json").read_text())
        monkeypatch.setattr(stations, "M3G_GEOJSON", _uri(folder) + "/m3g_{network}.geojson")
        monkeypatch.setattr(stations, "IGS_NETWORK", _uri(folder / "IGSNetwork.json"))
        with pytest.raises(download.DownloadError, match="empty"):
            stations.station_list(refresh=True)
        assert not (download.gnss_dir() / "stations" / "stations.json").exists()


def test_a_list_that_does_not_parse_is_a_download_error(archives, tmp_path, monkeypatch):
    folder = tmp_path / "html"
    folder.mkdir()
    (folder / "m3g_EPN.geojson").write_text("<html>Service temporarily unavailable</html>")
    shutil.copy(DATA / "m3g_RING.geojson", folder / "m3g_RING.geojson")
    monkeypatch.setattr(stations, "M3G_GEOJSON", _uri(folder) + "/m3g_{network}.geojson")
    with pytest.raises(download.DownloadError, match="m3g_EPN.geojson"):
        stations.station_list()
    assert not (download.gnss_dir() / "stations" / "stations.json").exists()


# --- the nearest stations ---------------------------------------------------------------------------------------


def test_nearby_sorted_by_distance(archives):
    found = stations.nearby(*ROME)
    assert [s.id for s, _km in found] == ["M0SE00ITA", "INGR00ITA", "ASIR00ITA", "RMPO00ITA", "MARN00ITA",
                                          "AQUI00ITA"]  # within 200 km: Medicina is at 320 km
    assert [km for _s, km in found] == sorted(km for _s, km in found)
    geod = pyproj.Geod(ellps="GRS80")
    for station, km in found:
        assert km == pytest.approx(geod.inv(ROME[1], ROME[0], station.lon, station.lat)[2] / 1000, abs=0.01)
    assert found[0][1] == pytest.approx(1.1, abs=0.1)

    assert [s.id for s, _ in stations.nearby(*ROME, limit=3)] == ["M0SE00ITA", "INGR00ITA", "ASIR00ITA"]
    wide = [s.id for s, _ in stations.nearby(*ROME, limit=20, within_km=1000)]
    assert wide[-2:] == ["MEDI00ITA", "ZNTE00GRC"] and "BSHM00ISR" not in wide  # Haifa is beyond 1000 km
    assert stations.nearby(*ROME, limit=0) == []
    assert stations.nearby(0.0, -30.0) == []  # nothing within 200 km in the Atlantic


def test_nearby_takes_the_stations_it_is_given(monkeypatch):
    monkeypatch.setattr(stations, "station_list", lambda *a, **k: pytest.fail("the list must not be read"))
    near = stations.Station("AAAA00ITA", "a", ("EPN",), 41.95, 12.5, 100.0, (1.0, 2.0, 3.0))
    far = stations.Station("BBBB00ITA", "b", ("RING",), 42.50, 12.5, 100.0, (1.0, 2.0, 3.0))
    found = stations.nearby(41.9, 12.5, stations=[far, near])
    assert [s.id for s, _km in found] == ["AAAA00ITA", "BBBB00ITA"]
    assert [km for _s, km in found] == pytest.approx([5.5536, 66.6463], abs=1e-3)  # along the meridian
    assert stations.nearby(41.9, 12.5, stations=[]) == []


# --- coordinates at an epoch ------------------------------------------------------------------------------------


def _check_llh(c: stations.StationCoordinates):
    lon, lat, h = pyproj.Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True).transform(*c.xyz)
    assert c.llh == pytest.approx((lat, lon, h), abs=1e-7)


def test_epn_coordinates_at_the_epoch(archives):
    aqui = _station("AQUI00ITA")
    c = stations.coordinates(aqui, 2026.5)
    assert np.allclose(c.xyz, np.array(AQUI_EPN.x0) + np.array(AQUI_EPN.v) * (2026.5 - AQUI_EPN.t0), atol=1e-4)
    assert c.source == "EPN" and c.frame == "ITRF2020" and c.epoch == 2026.5 and c.station == aqui
    # the formal error of the position at the reference epoch and of the velocity, brought to the epoch
    assert np.allclose(c.sigma, np.hypot(AQUI_EPN.sx0, np.array(AQUI_EPN.sv) * (2026.5 - AQUI_EPN.t0)), rtol=1e-6)
    assert "10.24414/ROB-EUREF-C2400" in c.citation and "CC BY 4.0" in c.citation  # the folder that was used
    _check_llh(c)
    assert (c.llh[0], c.llh[1]) == pytest.approx((aqui.lat, aqui.lon), abs=1e-3)


def test_epn_uses_the_newest_folder_and_the_igs20_file(archives):
    """C2399 has older numbers (5 cm off); the files of the other frames are 1 m away and the short one 2 m."""
    c = stations.coordinates(_station("AQUI00ITA"), 2026.5)
    assert abs(c.xyz[0] - _at(AQUI_EPN, 2026.5)[0]) < 1e-4
    cached = sorted(p.name for p in (download.gnss_dir() / "stations").iterdir())
    assert any(n.startswith("epn_C2400_") and n.endswith("_SOL.SSC") for n in cached)  # the folder is in the name


def test_epn_goes_back_to_the_folder_before_when_the_newest_has_no_file(archives):
    (archives.epn / "C2401").mkdir()
    (archives.epn / "C2401" / "README").write_text("the solution is not there yet")
    c = stations.coordinates(_station("ASIR00ITA"), 2026.5)
    assert c.source == "EPN" and "C2400" in c.citation


def test_epn_goes_on_to_the_next_folder_when_one_cannot_be_listed(archives, monkeypatch):
    (archives.epn / "C2401").mkdir()
    real = download.listing

    def listing(url, progress=None):
        if "C2401" in url:
            raise download.DownloadError("could not be listed", url)
        return real(url, progress)

    monkeypatch.setattr(download, "listing", listing)
    c = stations.coordinates(_station("ASIR00ITA"), 2026.5)
    assert c.source == "EPN" and "C2400" in c.citation


def test_epn_newer_folder_with_an_unreadable_file_uses_the_saved_older_one(archives, monkeypatch, caplog):
    aqui = _station("AQUI00ITA")
    first = stations.coordinates(aqui, 2026.5)  # saves the solution of C2400
    assert first.source == "EPN" and "C2400" in first.citation
    (archives.epn / "C2415").mkdir()
    (archives.epn / "C2415" / "EUR0OPSSNX_1996001_2026100_00U_SOL.SSC").write_text("a file that cannot be downloaded")
    download._LISTINGS.clear()  # the listing of the weeks is kept for ten minutes
    real = download.fetch

    def fetch(url, *args, **kwargs):
        if "C2415" in url:
            raise download.DownloadError("the server refused", url)
        return real(url, *args, **kwargs)

    monkeypatch.setattr(download, "fetch", fetch)
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        again = stations.coordinates(aqui, 2026.5)
    assert again == first  # the metres of the approximate position would be the result without the saved file
    assert any("C2400" in w and "saved" in w for w in _warnings(caplog))  # and the log names the week


def test_a_page_of_html_is_never_kept_for_a_solution(archives, monkeypatch, caplog):
    """What is downloaded for the coordinates of the stations is looked at before it is kept: a page of HTML (a captive
    portal, an error page) is never saved as the solution of a newer week of the EPN - which would be used for ever
    after - nor put in the place of a good copy of IGc20.ssc or of a series of RING; the good copy is used, with a
    warning."""
    aqui = _station("AQUI00ITA")
    first = stations.coordinates(aqui, 2026.5)  # saves the solution of C2400
    (archives.epn / "C2415").mkdir()
    (archives.epn / "C2415" / "EUR0OPSSNX_1996001_2026100_00U_SOL.SSC").write_text(
        "<html><body>Please log in to the network</body></html>")
    download._LISTINGS.clear()
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        assert stations.coordinates(aqui, 2026.5) == first
    kept = download.gnss_dir() / "stations"
    assert not list(kept.glob("epn_C2415_*")) and any("C2400" in w for w in _warnings(caplog))
    # IGc20.ssc too old to be used as it is, and the archive answers with a page: the good copy stays
    ssc = stations._cached(stations.IGS_SSC, "IGc20.ssc", stations.IGS_SSC_AGE, None, check=stations._sinex_head)
    good = ssc.read_bytes()
    old = (datetime.now() - stations.IGS_SSC_AGE * 2).timestamp()
    os.utime(ssc, (old, old))
    page = archives.root / "portal.html"
    page.write_text("<html>Service temporarily unavailable</html>")
    monkeypatch.setattr(stations, "IGS_SSC", _uri(page))
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        again = stations._cached(stations.IGS_SSC, "IGc20.ssc", stations.IGS_SSC_AGE, None, check=stations._sinex_head)
    assert again == ssc and ssc.read_bytes() == good
    assert any("not the file that was expected" in w for w in _warnings(caplog))
    assert sorted(p.name for p in kept.iterdir() if p.name.startswith("IGc20")) == ["IGc20.ssc"]


def test_a_position_that_is_no_number_is_refused():
    """The guard that takes a position more than 1 km from the one in the list for another station refuses a position
    that is no number too: NaN is farther than nothing."""
    station = stations.Station("ABCD00ITA", "ABCD", ("EPN",), 41.9, 12.5, 50.0, (4642000.0, 1028000.0, 4236000.0))
    with pytest.raises(ValueError, match="no number"):
        stations._same_place(station, (float("nan"),) * 3, "a.SSC")
    stations._same_place(station, (4642000.5, 1028000.0, 4236000.0), "a.SSC")  # half a metre: the same station


def test_a_part_file_is_never_taken_for_the_saved_solution(archives, monkeypatch):
    """Offline, the newest complete copy is used: not a ``.part`` that a killed download left, whatever its week."""
    aqui = _station("AQUI00ITA")
    first = stations.coordinates(aqui, 2026.5)
    kept = download.gnss_dir() / "stations"
    c2400 = next(kept.glob("epn_C2400_*"))
    c2399 = kept / "epn_C2399_EUR0OPSSNX_1996001_2025263_00U_SOL.SSC"  # the week before, complete
    shutil.copy(next((archives.epn / "C2399").glob("*_00U_SOL.SSC")), c2399)
    (kept / f"{c2400.name}.1a2b3c4d.part").write_text("what a download that was killed leaves")
    (kept / "epn_C2415_EUR0OPSSNX_1996001_2026100_00U_SOL.SSC.5e6f7a8b.part").write_text("and one of a newer week")
    (kept / "epn_C2416_EUR0OPSSNX_1996001_2026110_00U_SOL.SSC").mkdir()  # nor a folder that has the name of a copy
    monkeypatch.setattr(stations, "EPN_REFERENCE_FRAME", _uri(archives.root / "gone") + "/")
    assert stations.coordinates(aqui, 2026.5) == first  # the newest complete copy: C2400
    c2400.unlink()
    older = stations.coordinates(aqui, 2026.5)
    assert older.source == "EPN" and "C2399" in older.citation  # the next complete one, never a part file
    c2399.unlink()
    assert stations.coordinates(aqui, 2026.5).source == "approximate"  # part files alone are nothing


@pytest.mark.parametrize("trouble", ["no weekly folder is listed", "no weekly folder can be read"])
def test_epn_uses_the_saved_solution_when_no_week_can_be_found(archives, monkeypatch, caplog, trouble):
    aqui = _station("AQUI00ITA")
    first = stations.coordinates(aqui, 2026.5)
    if trouble == "no weekly folder is listed":
        for folder in archives.epn.glob("C2???"):
            shutil.rmtree(folder)
    else:
        real = download.listing

        def listing(url, progress=None):
            if re.search(r"/C\d{4}/?$", url):
                raise download.DownloadError("no answer", url)
            return real(url, progress)

        monkeypatch.setattr(download, "listing", listing)
    download._LISTINGS.clear()
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        again = stations.coordinates(aqui, 2026.5)
    assert again == first
    assert any("C2400" in w and "saved" in w for w in _warnings(caplog))


def test_epn_with_no_solution_to_use_is_a_warning(archives, caplog):
    """An archive that answers and has no weekly folder is not a quiet miss, as a station that RING does not have is."""
    for folder in archives.epn.glob("C2???"):
        shutil.rmtree(folder)
    with caplog.at_level(logging.INFO, logger="pyarchinit_photogrammetry"):
        c = stations.coordinates(_station("AQUI00ITA"), 2026.5)
    assert c.source == "approximate"
    assert any("no folder of the EPN" in w for w in _warnings(caplog))


def test_an_etrf_name_is_never_the_igs20_file(archives):
    """With a frame in the name it is not the IGS20 solution: an ETRF2000 file read as ITRF2020 is a metre off in Italy."""
    assert stations._EPN_SSC.fullmatch("EUR0OPSSNX_1996001_2025270_00U_SOL.SSC")
    for name in ("EUR0OPSSNX_1996001_2025270_00U_ETRF2000_SOL.SSC", "EUR0OPSSNX_1996001_2025270_00U_SOL_ETRF2000.SSC",
                 "EUR0OPSSNX_1996001_2025270_00U_SOL_short.SSC", "EUR0OPSSNX_1996001_2025270_ETRF2000_SOL.SSC"):
        assert not stations._EPN_SSC.fullmatch(name), name
    # the newest folder holds the IGS20 numbers under a name with a frame in the middle: it is not read, the week before is
    newest = archives.epn / "C2400"
    for solution in newest.glob("*_00U_SOL.SSC"):
        solution.rename(newest / solution.name.replace("_00U_SOL.SSC", "_00U_ETRF2000_SOL.SSC"))
    c = stations.coordinates(_station("AQUI00ITA"), 2026.5)
    assert c.source == "EPN" and "C2399" in c.citation
    assert abs(c.xyz[0] - (_at(AQUI_EPN, 2026.5)[0] + 0.05)) < 1e-4  # the numbers of C2399, 5 cm from those of C2400


@pytest.mark.parametrize("epoch, soln", [(2020.0, 1), (2023.41, 1), (2023.42, 2), (2026.5, 2)])
def test_solution_valid_at_the_epoch(archives, epoch, soln):
    """M0SE00ITA has a discontinuity (a second solution from 2023 day 152, that is 2023.4137): the solution that covers
    the epoch is the one used, and the last one beyond its end."""
    c = stations.coordinates(_station("M0SE00ITA"), epoch)
    assert c.source == "EPN"
    assert np.allclose(c.xyz, _at(M0SE_EPN[soln], epoch), atol=1e-6)
    assert np.abs(np.array(c.xyz) - _at(M0SE_EPN[3 - soln], epoch)).max() > 2e-3  # the jump is 11, 7 and 3 mm


# --- the solution of the EPN as its Central Bureau writes it ---------------------------------------------------------


@pytest.fixture
def epn_release(archives):
    """The folder C2415 of the reference frame of the EPN with the excerpts of the live solution in columns and of its
    catalogue of discontinuities, and the file of another frame beside them, as the real folder has: newer than the
    synthetic C2399 and C2400 of ``archives``, it is the one that is read."""
    folder = archives.epn / "C2415"
    folder.mkdir()
    for name in (EPN_COLUMNS, "EPN_discontinuities.snx"):
        shutil.copy(DATA / name, folder / name)
    etrf = (DATA / EPN_COLUMNS).read_bytes().replace(b"IGS20   ", b"ETRF2000")
    etrf = etrf.replace(b"4461400.47249", b"4461401.47249")  # MEDI a metre away: it shows if the file were read
    (folder / EPN_COLUMNS.replace("_SOL.SSC", "_SOL_ETRF2000.SSC")).write_bytes(etrf)
    return folder


def _enu_of(delta, llh) -> np.ndarray:
    """A difference of X, Y, Z (m) as east, north, up at ``llh`` (degrees)."""
    phi, lam = np.radians(llh[0]), np.radians(llh[1])
    return np.array([[-np.sin(lam), np.cos(lam), 0.0],
                     [-np.sin(phi) * np.cos(lam), -np.sin(phi) * np.sin(lam), np.cos(phi)],
                     [np.cos(phi) * np.cos(lam), np.cos(phi) * np.sin(lam), np.sin(phi)]]) @ np.asarray(delta)


def test_epn_solution_in_columns_at_the_epoch(epn_release):
    """The solution of the EPN as the Central Bureau writes it, in columns: MEDI00ITA at 2026.5 on the line of the
    solution its catalogue says is valid (the second, since 2000 day 320), with the formal error brought to the epoch
    and the citation of the folder that was used; the solution and the catalogue are kept under the name of the
    folder."""
    c = stations.coordinates(_station("MEDI00ITA"), 2026.5)
    assert c.source == "EPN" and c.frame == "ITRF2020" and c.epoch == 2026.5
    assert np.allclose(c.xyz, _at(MEDI_EPN, 2026.5), rtol=0, atol=1e-6)
    assert np.allclose(c.sigma, np.hypot(MEDI_EPN.sx0, np.array(MEDI_EPN.sv) * 6.5), rtol=1e-9, atol=0)
    assert "multi-year position and velocity solution C2415" in c.citation
    assert "10.24414/ROB-EUREF-C2415" in c.citation and "CC BY 4.0" in c.citation
    _check_llh(c)
    kept = {p.name for p in (download.gnss_dir() / "stations").iterdir()}
    assert {f"epn_C2415_{EPN_COLUMNS}", "epn_C2415_EPN_discontinuities.snx"} <= kept


def test_the_epn_and_igc20_agree_on_a_station_of_both(epn_release):
    """MEDI00ITA is in the solution of the EPN and in IGc20, two solutions of ITRF2020 made apart: at 2026.5 they put it
    within 1.5 mm of each other (IGc20 1.1 mm east, 0.7 mm south and 1.0 mm above the EPN)."""
    c = stations.coordinates(_station("MEDI00ITA"), 2026.5)
    apart = _enu_of(_at(MEDI_IGS[2], 2026.5) - np.array(c.xyz), c.llh)
    assert np.all(np.abs(apart) < 0.0015), apart


@pytest.mark.parametrize("epoch, soln", [(2005.0, 2), (2009.2616, 3), (2017.5, 3), (2018.0, 4), (2026.5, 4)])
def test_epn_solution_valid_by_its_catalogue(epn_release, epoch, soln):
    """AQUI00ITA: the catalogue starts solution 3 with the earthquake of L'Aquila, 2009 day 96 at 01:32:39, while the
    data of solution 2 run to the end of that day; at noon of that day (2009.2616) the solution is the third, the one
    the catalogue says, not the one whose data cover the time (2.8 cm across and 9.9 cm in height from it)."""
    c = stations.coordinates(_station("AQUI00ITA"), epoch)
    assert c.source == "EPN"
    assert np.allclose(c.xyz, _at(AQUI_COLUMNS[soln], epoch), rtol=0, atol=1e-6)
    other = 2 if soln == 3 else 3
    assert np.abs(np.array(c.xyz) - _at(AQUI_COLUMNS[other], epoch)).max() > 0.004


def test_epn_refuses_a_solution_its_file_does_not_hold(epn_release, caplog):
    """A break after the end of the data of the file (2026 day 115): a catalogue of a later day starts a third solution
    of MEDI00ITA on 2026 day 200, for which the file has no coordinates. From then on the EPN does not give the
    station - IGc20 does - never the second solution carried past its break; before it, the second stands."""
    catalogue = epn_release / "EPN_discontinuities.snx"
    catalogue.write_bytes(catalogue.read_bytes().replace(
        b" MEDI  A    2 P 00:320:00000 00:000:00000 P -\n",
        b" MEDI  A    2 P 00:320:00000 26:200:00000 P - antenna change\n"
        b" MEDI  A    3 P 26:200:00000 00:000:00000 P -\n"))
    medi = _station("MEDI00ITA")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        after = stations.coordinates(medi, 2026.7)
    assert after.source == "IGS" and np.allclose(after.xyz, _at(MEDI_IGS[2], 2026.7), rtol=0, atol=1e-6)
    assert any("solution 3" in w and "EPN_discontinuities.snx" in w and "MEDI" in w and "does not hold" in w
               for w in _warnings(caplog))
    before = stations.coordinates(medi, 2026.4)
    assert before.source == "EPN" and np.allclose(before.xyz, _at(MEDI_EPN, 2026.4), rtol=0, atol=1e-6)


def test_the_epn_solution_in_columns_needs_its_catalogue(epn_release, caplog):
    """Without the catalogue of discontinuities of its folder the solution in columns is not used - no solution would be
    known to be valid at the epoch: MEDI00ITA comes from IGc20, ASIR00ITA (of the EPN alone) only approximately."""
    (epn_release / "EPN_discontinuities.snx").unlink()
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        assert stations.coordinates(_station("MEDI00ITA"), 2026.5).source == "IGS"
        assert stations.coordinates(_station("ASIR00ITA"), 2026.5).source == "approximate"
    assert any("EPN_discontinuities.snx" in w and "not used without" in w for w in _warnings(caplog))


def test_the_epn_solution_in_columns_offline(epn_release, archives, monkeypatch):
    """The solution and the catalogue of a folder are kept: with the archive gone, the copies give the same position."""
    medi = _station("MEDI00ITA")
    first = stations.coordinates(medi, 2026.5)
    monkeypatch.setattr(stations, "EPN_REFERENCE_FRAME", _uri(archives.root / "gone") + "/")
    download._LISTINGS.clear()
    assert stations.coordinates(medi, 2026.5) == first


def test_an_epn_file_in_another_frame_is_refused(epn_release, caplog):
    """A file with the name of the solution in IGS20 whose title says ETRF2000 (a metre from ITRF2020 in Italy) is not
    read as ITRF2020."""
    path = epn_release / EPN_COLUMNS
    path.write_bytes(path.read_bytes().replace(b"REFERENCE FRAME:    IGS20 ", b"REFERENCE FRAME: ETRF2000 "))
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        c = stations.coordinates(_station("MEDI00ITA"), 2026.5)
    assert c.source == "IGS"
    assert any("ETRF2000" in w and "not in IGS20" in w for w in _warnings(caplog))


def test_the_columns_of_the_epn_as_they_are_written():
    """Every solution of a station, each its position line and the velocity line after it; a position line without
    its velocity is an error that names the line."""
    solutions = stations._read_epn_columns(DATA / EPN_COLUMNS, "M0SE")
    assert [s.number for s in solutions] == [1, 2, 3, 4, 5]
    last = solutions[-1]
    assert last.xyz == (4642432.25432, 1028629.70072, 4236854.41823) and last.velocity == (-0.01489, 0.01801, 0.01198)
    assert last.sx == (7e-5, 3e-5, 6e-5) and last.sv == (1e-5, 1e-5, 1e-5) and last.ref == 2020.0
    assert last.start == pytest.approx(2016 + 304 / 366)
    assert last.end == pytest.approx(2026 + (114 + 86370 / 86400) / 365)
    with pytest.raises(stations._Absent):
        stations._read_epn_columns(DATA / EPN_COLUMNS, "BOLG")


@pytest.mark.parametrize("which", ["a velocity line", "the last velocity line", "the title"])
def test_the_columns_of_the_epn_refuse_what_is_missing(tmp_path, which):
    lines = (DATA / EPN_COLUMNS).read_bytes().split(b"\n")
    code = "MEDI"
    if which == "a velocity line":
        del lines[11]  # the velocity of the second solution of MEDI (the position is line 11)
        match = r"line 12: the line after the position of MEDI \(line 11\) is not its velocity"
    elif which == "the last velocity line":
        del lines[-2]  # the velocity of ASIR, the last line of the file
        code, match = "ASIR", r"line 31: the position of ASIR is the last line, without its velocity"
    else:
        lines = [line for line in lines if b"REFERENCE FRAME" not in line]
        match = "does not name the reference frame"
    path = tmp_path / EPN_COLUMNS
    path.write_bytes(b"\n".join(lines))
    with pytest.raises(ValueError, match=match):
        stations._read_epn_columns(path, code)


@pytest.mark.parametrize("epoch, soln", [(1999.5, 1), (2000.8, 1), (2000.9, 2), (2005.0, 2)])
def test_solution_valid_at_the_epoch_in_the_file_of_the_igs(archives, epoch, soln):
    """MEDI00ITA is not in the file of the EPN: the file of the IGS answers, with its own discontinuity in 2000."""
    c = stations.coordinates(_station("MEDI00ITA"), epoch)
    assert c.source == "IGS" and c.frame == "ITRF2020"
    assert np.allclose(c.xyz, _at(MEDI_IGS[soln], epoch), atol=1e-6)
    assert np.abs(np.array(c.xyz) - _at(MEDI_IGS[3 - soln], epoch)).max() > 1e-3
    assert "IGc20" in c.citation


def test_igs_solution_is_extrapolated_beyond_its_last_epoch(archives):
    """The last solution of BSHM ends in March 2025 (the end of the data of the frame): its line goes on."""
    c = stations.coordinates(_station("BSHM00ISR"), 2026.5)
    assert c.source == "IGS"
    assert np.allclose(c.xyz, _at(BSHM_IGS, 2026.5), atol=1e-6)
    assert np.allclose(c.sigma, np.hypot(BSHM_IGS.sx0, np.array(BSHM_IGS.sv) * 6.5), rtol=1e-6)
    _check_llh(c)


@pytest.fixture
def igs_folder(archives, tmp_path, monkeypatch):
    """A copy of the folder of IGc20 (the solutions, the post-seismic models, the list of discontinuities) that a test can
    change; the other services stay the ones of ``archives``."""
    folder = tmp_path / "igs" / "IGc20"
    folder.mkdir(parents=True)
    for name in ("IGc20.ssc", "psd_IGc20.snx", "soln_IGc20.snx"):
        shutil.copy(DATA / name, folder / name)
    monkeypatch.setattr(stations, "IGS_SSC", _uri(folder / "IGc20.ssc"))
    return folder


def _without(path: Path, code: str) -> None:
    """``path`` without the lines about the station ``code``."""
    path.write_text("".join(line for line in path.read_text().splitlines(keepends=True) if f" {code} " not in line))


def test_igc20_is_refused_for_a_station_with_post_seismic_terms(archives, caplog):
    """The header of IGc20 says that the positions and velocities of such a station cannot be used without the model of
    psd_IGc20.snx, which this program does not apply. DRAG00ISR is one: the earthquake of Kahramanmaras, 2023."""
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        c = stations.coordinates(_station("DRAG00ISR"), 2026.5)
    assert c.source == "approximate" and c.sigma is None  # not even the plain line of IGc20, with its millimetres
    assert any("post-seismic" in w and "DRAG" in w and "psd_IGc20.snx" in w for w in _warnings(caplog))


def test_the_post_seismic_file_is_what_refuses_the_station(igs_folder):
    _without(igs_folder / "psd_IGc20.snx", "DRAG")
    c = stations.coordinates(_station("DRAG00ISR"), 2026.5)
    assert c.source == "IGS" and np.allclose(c.xyz, _at(DRAG_IGS, 2026.5), atol=1e-6)


def test_igc20_is_refused_when_the_solution_of_the_epoch_is_not_in_its_file(archives, caplog):
    """ADIS00ETH had breaks after March 2025: soln_IGc20.snx lists solutions 4 to 7, IGc20.ssc has no coordinates for them.
    From 2025 day 81 the solution is the sixth, from 2026 day 90 the seventh; the third (2022 to 2024) is in the file."""
    adis = _station("ADIS00ETH")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        for epoch in (2025.5, 2026.5):
            assert stations.coordinates(adis, epoch).source == "approximate"
    warned = _warnings(caplog)
    assert any("solution 6" in w and "soln_IGc20.snx" in w and "ADIS" in w for w in warned)
    assert any("solution 7" in w and "soln_IGc20.snx" in w and "ADIS" in w for w in warned)
    c = stations.coordinates(adis, 2024.5)
    assert c.source == "IGS" and np.allclose(c.xyz, _at(ADIS_IGS, 2024.5), atol=1e-6)


def test_the_discontinuity_list_says_which_solution_is_valid_not_the_span_of_the_data(archives):
    """BSHM00ISR: the data of its last solution end in March 2025, but soln_IGc20.snx has it valid from 2023 day 37 on."""
    c = stations.coordinates(_station("BSHM00ISR"), 2030.0)
    assert c.source == "IGS" and np.allclose(c.xyz, _at(BSHM_IGS, 2030.0), atol=1e-6)


@pytest.mark.parametrize("broken, name", [("no psd file", "psd_IGc20.snx"), ("no discontinuity file", "soln_IGc20.snx"),
                                          ("station not in the discontinuity file", "soln_IGc20.snx"),
                                          ("discontinuity file that is not SINEX", "soln_IGc20.snx")])
def test_igc20_is_not_used_without_the_files_that_say_when_it_holds(igs_folder, caplog, broken, name):
    """What cannot be checked is not used: the next source answers, with a warning that names the file."""
    if broken == "no psd file":
        (igs_folder / "psd_IGc20.snx").unlink()
    elif broken == "no discontinuity file":
        (igs_folder / "soln_IGc20.snx").unlink()
    elif broken == "station not in the discontinuity file":
        _without(igs_folder / "soln_IGc20.snx", "BSHM")
    else:
        (igs_folder / "soln_IGc20.snx").write_text("<html>moved</html>\n")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        c = stations.coordinates(_station("BSHM00ISR"), 2026.5)
    assert c.source == "approximate"
    assert any(name in w for w in _warnings(caplog))


def test_the_later_solution_holds_at_a_break_and_nothing_outside_the_list_does():
    spans = [(1, None, 2000.0), (2, 2000.0, 2010.5), (3, 2010.5, None)]
    assert stations._valid_at(spans, 1999.999)[0] == 1
    assert stations._valid_at(spans, 2000.0)[0] == 2  # the break belongs to the solution that begins there
    assert stations._valid_at(spans, 2010.5)[0] == 3 and stations._valid_at(spans, 2100.0)[0] == 3
    assert stations._valid_at([(1, 2000.0, 2001.0)], 2002.0) is None and stations._valid_at([(1, 2000.0, 2001.0)], 1999.0) is None


def test_a_break_of_the_velocity_alone_is_not_a_position_solution(igs_folder):
    """soln_IGc20.snx lists the breaks of the position (event P) and of the velocity (event V) with numbers of their own;
    IGc20.ssc follows the first."""
    soln = igs_folder / "soln_IGc20.snx"
    soln.write_text(soln.read_text().replace(
        " BSHM  A    2 P 23:037:04654 00:000:00000 V - \n",
        " BSHM  A    2 P 23:037:04654 24:001:00000 V - \n BSHM  A    9 P 24:001:00000 00:000:00000 V - made up\n"))
    c = stations.coordinates(_station("BSHM00ISR"), 2026.5)
    assert c.source == "IGS" and np.allclose(c.xyz, _at(BSHM_IGS, 2026.5), atol=1e-6)  # solution 4, not a ninth


def test_the_files_that_go_with_igc20_are_fetched_for_its_stations_only(archives, monkeypatch):
    calls = _fetches(monkeypatch)
    nowhere = stations.Station("ZZZZ00XXX", "not in IGc20", ("IGS",), 0.0, 0.0, 0.0, (6378137.0, 0.0, 0.0))
    assert stations.coordinates(nowhere, 2026.5).source == "approximate"
    assert [u for u in calls if "IGc20" in u] == [_uri(DATA / "IGc20.ssc")]  # only the solutions, to look for it
    stations.coordinates(_station("BSHM00ISR"), 2026.5)
    assert _uri(DATA / "psd_IGc20.snx") in calls and _uri(DATA / "soln_IGc20.snx") in calls  # the same folder
    kept = {p.name for p in (download.gnss_dir() / "stations").iterdir()}
    assert {"IGc20.ssc", "psd_IGc20.snx", "soln_IGc20.snx"} <= kept  # and the same cache


def _write_pos(ring: Path, sid: str, whens: list[datetime], positions: np.ndarray) -> None:
    """A RING series: the header of the real file of INGR00ITA and one row of 25 columns for each day."""
    header = (DATA / "INGR00ITA.gipsyX.IGS20.pos").read_text().splitlines()
    header = header[:next(i for i, line in enumerate(header) if line.startswith("*YYYYMMDD")) + 1]
    rows = []
    for when, xyz in zip(whens, positions):
        mjd = (when - datetime(1858, 11, 17)).total_seconds() / 86400
        rows.append(f" {when:%Y%m%d} 115500 {mjd:10.4f}  {xyz[0]:13.5f}  {xyz[1]:13.5f}  {xyz[2]:13.5f}  0.00230  "
                    "0.00086  0.00201  0.522  0.841  0.491      41.8280877118   12.5148051103  104.86858     "
                    "0.42661   0.54218   0.00596    0.00072  0.00086  0.00285 -0.024  0.001 -0.048 final")
    folder = ring / sid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{sid}.gipsyX.IGS20.pos").write_text("\n".join(header + rows) + "\n")


def _days(count: int, end: datetime = datetime(2026, 4, 4)) -> tuple[list[datetime], np.ndarray]:
    whens = [end - timedelta(days=count - 1 - n) + timedelta(hours=11, minutes=55) for n in range(count)]
    return whens, np.array([_decimal_year(w) for w in whens])


def test_ring_series_coordinates(archives):
    rmpo = _station("RMPO00ITA")
    x0 = np.array(rmpo.xyz) + (0.05, -0.03, 0.02)
    v = np.array([0.020, -0.012, 0.009])  # 2 cm/y
    whens, t = _days(1095)
    positions = x0 + np.outer(t - 2020.0, v) + np.random.default_rng(7).normal(0.0, 0.002, (len(t), 3))
    positions[5::109] += 0.05  # ten days that are 5 cm off
    positions[-4:] += 0.30  # and the last four, 30 cm off: an ordinary line would be thrown far at the epoch
    _write_pos(archives.ring, "RMPO00ITA", whens, positions)
    c = stations.coordinates(rmpo, 2026.8)
    truth = x0 + v * (2026.8 - 2020.0)
    assert c.source == "RING" and c.frame == "ITRF2020" and c.station == rmpo
    assert np.abs(np.array(c.xyz) - truth).max() < 2e-3  # the days that are off do not pull the line
    # the sigma is the scatter of the daily positions around the line: about the 2 mm of the noise, not the days off
    assert c.sigma is not None and all(0.001 < s < 0.004 for s in c.sigma)
    assert "10.13127/gnss/gipos" in c.citation and "CC BY 4.0" in c.citation
    _check_llh(c)


def test_ring_series_uses_the_last_two_years(archives):
    """The velocity changes 2.5 years before the end of the series: only the last two years count."""
    rmpo = _station("RMPO00ITA")
    x0 = np.array(rmpo.xyz)
    whens, t = _days(1600)
    kink = t[-1] - 2.5
    along = np.where(t < kink, 0.020 * (t - kink), -0.060 * (t - kink))  # 2 cm/y, then -6 cm/y
    positions = x0 + np.outer(along, (1.0, 0.0, 0.0))
    _write_pos(archives.ring, "RMPO00ITA", whens, positions)
    c = stations.coordinates(rmpo, 2026.8)
    assert c.xyz[0] == pytest.approx(x0[0] - 0.060 * (2026.8 - kink), abs=1e-3)


def test_ring_series_for_a_flight_in_the_past_uses_the_years_around_it(archives):
    """The series goes on after the epoch: the line is that of the two years around it, not of the end of the series."""
    rmpo = _station("RMPO00ITA")
    x0 = np.array(rmpo.xyz)
    whens, t = _days(3650)
    along = np.where(t < 2019.5, 0.020 * (t - 2019.5), -0.050 * (t - 2019.5))  # a kink at 2019.5
    positions = x0 + np.outer(along, (1.0, 0.0, 0.0)) + np.random.default_rng(3).normal(0.0, 0.001, (len(t), 3))
    _write_pos(archives.ring, "RMPO00ITA", whens, positions)
    c = stations.coordinates(rmpo, 2018.5)
    assert c.xyz[0] == pytest.approx(x0[0] + 0.020 * (2018.5 - 2019.5), abs=1e-3)


def test_ring_series_of_a_real_station(archives):
    """The last 150 days of INGR00ITA as RING wrote them (2025-09-14 to 2026-04-04) give the line of an ordinary fit."""
    ingr = _station("INGR00ITA")
    (archives.ring / "INGR00ITA").mkdir()
    shutil.copy(DATA / "INGR00ITA.gipsyX.IGS20.pos", archives.ring / "INGR00ITA" / "INGR00ITA.gipsyX.IGS20.pos")
    rows = [line.split() for line in (DATA / "INGR00ITA.gipsyX.IGS20.pos").read_text().splitlines()
            if re.match(r"^ \d{8} ", line)]
    assert len(rows) == 150
    t = np.array([_decimal_year(datetime.strptime(r[0] + r[1], "%Y%m%d%H%M%S")) for r in rows])
    xyz = np.array([[float(r[3]), float(r[4]), float(r[5])] for r in rows])
    c = stations.coordinates(ingr, 2026.2)
    assert c.source == "RING"
    ordinary = np.array([np.polyval(np.polyfit(t - t.mean(), xyz[:, k], 1), 2026.2 - t.mean()) for k in range(3)])
    assert np.abs(np.array(c.xyz) - ordinary).max() < 1e-3  # the Huber weights hardly matter on clean days
    assert np.abs(np.array(c.xyz) - xyz[-5:].mean(axis=0)).max() < 0.02  # and it is where the last days of the series are
    assert c.sigma is not None and all(0 < s < 0.02 for s in c.sigma)


def test_a_ring_series_in_another_frame_is_not_used(archives, caplog):
    rmpo = _station("RMPO00ITA")
    whens, t = _days(400)
    _write_pos(archives.ring, "RMPO00ITA", whens, np.tile(rmpo.xyz, (len(t), 1)))
    path = archives.ring / "RMPO00ITA" / "RMPO00ITA.gipsyX.IGS20.pos"
    path.write_text(path.read_text().replace("Reference Frame : IGS20", "Reference Frame : IGb14", 1))
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        c = stations.coordinates(rmpo, 2026.8)
    assert c.source == "approximate" and any("IGb14" in w for w in _warnings(caplog))


def test_approximate_coordinates_have_no_sigma(archives, caplog):
    rmpo = _station("RMPO00ITA")  # a station of RING without a series in the archive
    with caplog.at_level(logging.INFO, logger="pyarchinit_photogrammetry"):
        c = stations.coordinates(rmpo, 2026.5)
    assert c.source == "approximate" and c.sigma is None and c.frame == "ITRF2020" and c.epoch == 2026.5
    assert c.xyz == rmpo.xyz and c.llh == (rmpo.lat, rmpo.lon, rmpo.h) and c.station == rmpo
    assert "approximate" in c.citation.lower() and "metres" in c.citation
    assert any("RMPO00ITA" in r.getMessage() for r in caplog.records)  # the log says what was not found
    assert any("RMPO00ITA" in w and "approximate" in w for w in _warnings(caplog))  # and warns of the metres


def test_a_file_of_another_layout_is_skipped_with_a_warning(archives, caplog):
    """A file of the EPN laid out neither as SINEX nor in the columns of the Central Bureau (here the line of the names
    of the columns without the title that names the frame) is not guessed at: the next source answers."""
    for solution in archives.epn.glob("C*/*_SOL.SSC"):
        solution.write_text("DOMES NB. SITE NAME TECH. ID. X/Vx Y/Vy Z/Vz SOLN DATA_START DATA_END\n")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        c = stations.coordinates(_station("M0SE00ITA"), 2026.5)
    assert c.source == "IGS"  # M0SE00ITA is in the file of the IGS as well
    assert any("neither SINEX nor the columns" in w and "_SOL.SSC" in w for w in _warnings(caplog))


def test_a_station_of_another_place_with_the_same_code_is_refused(archives, caplog):
    """The files give four characters: a 'M0SE' 9000 km from the station of the list is another station."""
    elsewhere = stations.Station("M0SE00BRA", "far away", ("EPN",), -10.0, -50.0, 100.0,
                                 (4_000_000.0, -4_800_000.0, -1_100_000.0))
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        c = stations.coordinates(elsewhere, 2026.5)
    assert c.source == "approximate"
    assert any("another station" in w for w in _warnings(caplog))


def test_epn_unreachable_falls_through_to_the_other_sources(archives, monkeypatch):
    monkeypatch.setattr(stations, "EPN_REFERENCE_FRAME", _uri(archives.root / "no-epn") + "/")
    assert stations.coordinates(_station("M0SE00ITA"), 2026.5).source == "IGS"
    assert stations.coordinates(_station("AQUI00ITA"), 2026.5).source == "approximate"  # not in the file of the IGS


def test_the_files_come_from_the_cache_when_the_network_is_gone(archives, monkeypatch, caplog):
    aqui = _station("AQUI00ITA")
    first = stations.coordinates(aqui, 2026.5)
    calls = _fetches(monkeypatch)
    assert stations.coordinates(aqui, 2026.5) == first
    assert calls == []  # the file of a weekly solution never changes: it is not asked for again
    monkeypatch.setattr(stations, "EPN_REFERENCE_FRAME", _uri(archives.root / "gone") + "/")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        offline = stations.coordinates(aqui, 2026.5)
    assert offline == first and offline.source == "EPN"
    assert any("saved" in w for w in _warnings(caplog))


def test_the_file_of_the_igs_is_asked_for_again_after_a_month(archives, monkeypatch):
    medi = _station("MEDI00ITA")
    now = datetime.now(timezone.utc)
    stations.coordinates(medi, 2026.5)
    calls = _fetches(monkeypatch)
    stations.coordinates(medi, 2026.5)
    assert calls == []
    monkeypatch.setattr(stations, "_now", lambda: now + timedelta(days=31))
    stations.coordinates(medi, 2026.5)
    assert [u for u in calls if u.endswith("IGc20.ssc")]


@pytest.mark.parametrize("epoch", [20261027.0, 0.0, float("nan"), 1850.0])
def test_the_epoch_must_be_a_decimal_year(archives, epoch):
    with pytest.raises(ValueError, match="decimal year"):
        stations.coordinates(_station("AQUI00ITA"), epoch)


# --- the downloads ------------------------------------------------------------------------------------------------


class _Reply(io.BytesIO):
    """What ``urlopen`` returns: a file with the headers of the answer (and its address, as after a redirection)."""

    def __init__(self, data: bytes, headers: dict | None = None, url: str | None = None):
        super().__init__(data)
        self.headers = headers or {}
        self._url = url

    def geturl(self):
        return self._url


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, f"error {code}", {}, io.BytesIO(b""))


@pytest.fixture
def web(monkeypatch):
    """``urlopen`` that answers from a plan (a reply, or an exception to raise, one after the other) and the waiting
    of the retries recorded instead of done."""
    class Web:
        def __init__(self):
            self.plan: list = []
            self.requests: list = []
            self.sleeps: list[float] = []

        def __call__(self, request, timeout=None, **kwargs):
            self.requests.append((request, timeout, kwargs))
            outcome = self.plan.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    fake = Web()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(download, "_sleep", fake.sleeps.append)
    return fake


URL = "https://example.org/products/EUR0OPSSNX.SSC"


def test_user_agent_names_the_program_and_its_version(web, tmp_path):
    assert download.USER_AGENT == f"pyarchinit-photogrammetry/{__version__}"
    web.plan = [_Reply(b"data", {"Content-Length": "4"})]
    download.fetch(URL, tmp_path / "f.bin")
    request, timeout, kwargs = web.requests[0]
    assert request.get_header("User-agent") == download.USER_AGENT and timeout == 60.0
    assert "context" in kwargs  # https: with the certificates of the program


def test_plain_http_and_a_redirection_to_it_are_refused(web, tmp_path):
    """Every archive is reached over HTTPS: a plain http address is refused before anything is asked, and an answer
    that a redirection brought from a plain http address is refused before it is read - its content could have been
    changed on the way. Nothing is written."""
    with pytest.raises(download.DownloadError, match="not HTTPS") as error:
        download.fetch("http://example.org/products/a.gz", tmp_path / "a.gz")
    assert error.value.url == "http://example.org/products/a.gz" and web.requests == []
    web.plan = [_Reply(b"data", {"Content-Length": "4"}, url="http://example.org/elsewhere/a.gz")]
    with pytest.raises(download.DownloadError, match="http://example.org/elsewhere/a.gz"):
        download.fetch(URL, tmp_path / "b.gz", retries=1)
    web.plan = [_Reply(b'<a href="x.gz">x</a>', url="http://example.org/pub/")]
    with pytest.raises(download.DownloadError, match="not HTTPS"):
        download.listing("https://example.org/pub/")
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    # a redirection that stays on HTTPS is followed, as before
    web.plan = [_Reply(b"data", {"Content-Length": "4"}, url="https://mirror.example.org/a.gz")]
    assert download.fetch(URL, tmp_path / "c.gz").read_bytes() == b"data"


def test_fetch_retries_then_names_the_url(web, tmp_path):
    target = tmp_path / "out" / "f.bin"
    web.plan = [_http_error(URL, 503), _http_error(URL, 502), _http_error(URL, 500)]
    with pytest.raises(download.DownloadError) as error:
        download.fetch(URL, target)
    assert not isinstance(error.value, download.NotPublished)
    assert error.value.url == URL and URL in str(error.value) and "500" in str(error.value)
    assert len(web.requests) == 3 and web.sleeps == [2, 4]  # three attempts, waiting 2 s and then 4 s
    assert not target.exists() and list(target.parent.iterdir()) == []  # nothing left, not even a part

    # a failure that goes away: the file is there, and the wait was only 2 s
    web.plan = [_http_error(URL, 503), _Reply(b"hello", {"Content-Length": "5"})]
    web.sleeps.clear()
    assert download.fetch(URL, target) == target and target.read_bytes() == b"hello"
    assert web.sleeps == [2]


@pytest.mark.parametrize("failure", [socket.timeout("timed out"), urllib.error.URLError(ConnectionRefusedError()),
                                     urllib.error.URLError(socket.gaierror("no such host")), ConnectionResetError()])
def test_fetch_retries_timeouts_and_connections_that_fail(web, tmp_path, failure):
    web.plan = [failure, _Reply(b"0123456789", {"Content-Length": "10"})]
    target = tmp_path / "f.bin"
    assert download.fetch(URL, target).read_bytes() == b"0123456789"
    assert len(web.requests) == 2 and web.sleeps == [2]


def test_fetch_retries_a_download_that_is_cut_short(web, tmp_path):
    web.plan = [_Reply(b"12345", {"Content-Length": "10"}), _Reply(b"0123456789", {"Content-Length": "10"})]
    assert download.fetch(URL, tmp_path / "f.bin").read_bytes() == b"0123456789"
    assert web.sleeps == [2]
    web.plan = [_Reply(b"123", {"Content-Length": "10"}) for _ in range(3)]
    with pytest.raises(download.DownloadError, match="3 of 10 bytes"):
        download.fetch(URL, tmp_path / "g.bin")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.bin"]  # the short file was not kept


def test_fetch_does_not_retry_what_will_not_change(web, tmp_path):
    target = tmp_path / "f.bin"
    web.plan = [_http_error(URL, 404)]
    with pytest.raises(download.NotPublished) as error:
        download.fetch(URL, target)
    assert error.value.url == URL and isinstance(error.value, download.DownloadError)
    assert len(web.requests) == 1 and web.sleeps == []

    web.plan = [_http_error(URL, 403)]  # refused
    with pytest.raises(download.DownloadError, match="403"):
        download.fetch(URL, target)
    web.plan = [urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))]
    with pytest.raises(download.DownloadError, match="certificate"):
        download.fetch(URL, target)
    assert len(web.requests) == 3 and web.sleeps == []


def test_fetch_checks_sha256(tmp_path):
    data = b"station data " * 1000
    source = tmp_path / "source" / "data.bin"
    source.parent.mkdir()
    source.write_bytes(data)
    good = hashlib.sha256(data).hexdigest()
    target = tmp_path / "out" / "data.bin"
    assert download.fetch(_uri(source), target, sha256=good.upper()) == target  # file:// is accepted
    assert target.read_bytes() == data

    other = tmp_path / "out" / "other.bin"
    with pytest.raises(download.DownloadError, match="SHA-256") as error:
        download.fetch(_uri(source), other, sha256="0" * 64)
    assert error.value.url == _uri(source) and good[:12] in str(error.value)
    assert not other.exists()
    # a file that was there is not replaced by one that is not the one asked for
    other.write_bytes(b"old")
    with pytest.raises(download.DownloadError):
        download.fetch(_uri(source), other, sha256="0" * 64)
    assert other.read_bytes() == b"old"
    assert sorted(p.name for p in target.parent.iterdir()) == ["data.bin", "other.bin"]  # no part files


def test_fetch_a_file_that_is_not_there(tmp_path):
    with pytest.raises(download.NotPublished) as error:
        download.fetch(_uri(tmp_path / "missing.gz"), tmp_path / "out.gz")
    assert error.value.url.endswith("missing.gz")
    assert not (tmp_path / "out.gz").exists()


def test_fetch_reports_progress_and_can_be_cancelled(web, tmp_path):
    big = b"x" * (3 << 20)
    web.plan = [_Reply(big, {"Content-Length": str(len(big))})]
    seen: list[float] = []
    download.fetch(URL, tmp_path / "big.bin", progress=lambda fraction, message: seen.append(fraction))
    assert seen == sorted(seen) and seen[-1] == 1.0 and len(seen) >= 3

    source = tmp_path / "s.bin"
    source.write_bytes(big)
    progress = Progress()
    progress.cancel_requested = True
    with pytest.raises(Cancelled):
        download.fetch(_uri(source), tmp_path / "c.bin", progress=progress)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["big.bin", "s.bin"]  # nothing of the cancelled one


def test_listing_reads_an_index(web):
    """The page of the IGS (Apache: relative links, the sort links, the parent folder, a link to another site)."""
    web.plan = [_Reply((DATA / "index_igs_IGc20.html").read_bytes())]
    names = download.listing("https://files.igs.org/pub/station/coord/IGc20/")
    assert names == ["IGc20.snx.gz", "IGc20.ssc", "IGc20_core.txt", "psd_IGc20.snx", "soln_IGc20.snx"]
    assert web.requests[0][0].get_header("User-agent") == download.USER_AGENT
    # RING's (IIS: links as paths from the root, a folder without a slash, the folders above it, links to other sites)
    web.plan = [_Reply((DATA / "index_ring_IGS2.html").read_bytes())]
    ring = download.listing("https://webring.gm.ingv.it:44324/products/POS/IGS2/")
    assert ring == ["INGP00ITA", "INGR00ITA", "INSA00FRA", "INVE00GBR"]


def test_listing_does_not_return_names_that_leave_the_folder(web):
    """A link is decoded before it is judged: %2E%2E is .., ..%2F..%2Fetc%2Fpasswd goes up and down, %5C is a backslash,
    %00 ends a name in some languages. Task 4 writes files under a name that comes from here."""
    hostile = ["%2E%2E", "%2e%2e", "%2E", "..%2F..%2Fetc%2Fpasswd", "x%2Fy.gz", "..%5C..%5Cevil.gz", "a%5Cb.gz",
               "a%00b.gz", "sub%2F%2F", "..", "../", ".", "./", "%2F", "//evil.example/x.gz"]
    page = "".join(f'<a href="{href}">x</a>' for href in hostile + ["ok%20file.gz", "folder/", "plain.gz"])
    web.plan = [_Reply(page.encode())]
    assert download.listing("https://example.org/pub/") == ["ok file.gz", "folder/", "plain.gz"]
    assert not download._is_entry("sub//") and not download._is_entry("/") and download._is_entry("sub/")


def test_listing_follows_the_address_after_a_redirection(web):
    """The links are relative to the page that answered, not to the address that was asked for."""
    html = b'<html><a href="C2400/">C2400/</a> <a href="notes.txt">notes.txt</a> <a href="../">Parent</a></html>'
    web.plan = [_Reply(html, url="https://epncb.oma.be/pub/product/referenceframe/")]
    assert download.listing("https://epncb.oma.be/pub/product/referenceframe") == ["C2400/", "notes.txt"]


def test_listing_of_a_folder_and_of_a_page_on_disk(tmp_path):
    (tmp_path / "tree" / "C2400").mkdir(parents=True)
    (tmp_path / "tree" / "C2399").mkdir()
    (tmp_path / "tree" / "notes.txt").write_text("x")
    assert download.listing(_uri(tmp_path / "tree") + "/") == ["C2399/", "C2400/", "notes.txt"]
    page = tmp_path / "tree" / "index.html"
    page.write_text('<a href="?C=N;O=D">Name</a><a href="a%20b.gz">a b.gz</a><a href="sub/">sub/</a>'
                    '<a href="/elsewhere/x">x</a><a href="https://example.org/y">y</a><a href="#top">top</a>')
    assert download.listing(_uri(page)) == ["a b.gz", "sub/"]
    with pytest.raises(download.NotPublished):
        download.listing(_uri(tmp_path / "nothing") + "/")


def test_listing_is_kept_ten_minutes(web, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(download, "_clock", lambda: clock[0])
    url = "https://example.org/pub/"
    web.plan = [_Reply(b'<a href="one.gz">one.gz</a>'), _Reply(b'<a href="two.gz">two.gz</a>')]
    first = download.listing(url)
    assert first == ["one.gz"]
    first.append("changed by the caller")
    clock[0] += 599
    assert download.listing(url) == ["one.gz"] and len(web.requests) == 1  # kept in memory, and not by reference
    clock[0] += 2
    assert download.listing(url) == ["two.gz"] and len(web.requests) == 2  # ten minutes have passed


def test_a_site_that_does_not_answer_is_given_up_within_half_a_minute(web, tmp_path, monkeypatch, caplog):
    """A firewall that drops what goes to the port of RING (44324): a listing waits download.LISTING_TIMEOUT for an
    answer, three times with 2 s and 4 s between the attempts - half a minute at most, not three minutes - while the
    progress names the site; the error says that the port may be closed. The coordinates of RING give up as soon, and
    say so in the log. A cancel between two attempts stops at once."""
    monkeypatch.setattr(stations, "RING_RINEX", "https://webring.gm.ingv.it:44324/rinex/RING/")
    monkeypatch.setattr(stations, "RING_POS", "https://webring.gm.ingv.it:44324/products/POS/IGS2/{id}/{id}.pos")
    ring = _made("INGR00ITA", "RING")
    web.plan = [socket.timeout("timed out") for _ in range(3)]
    messages = []
    with pytest.raises(ValueError) as error:
        stations.download_observations(ring, _t(26, 10), _t(26, 10, 30), tmp_path / "out",
                                       progress=lambda fraction, message: messages.append(message))
    assert len(web.requests) == 3 and {timeout for _r, timeout, _k in web.requests} == {download.LISTING_TIMEOUT}
    assert 3 * download.LISTING_TIMEOUT + sum(web.sleeps) <= 30.0
    assert "RING is on port 44324" in str(error.value)
    assert any(message.startswith("Contacting webring.gm.ingv.it:44324") for message in messages), messages
    # the coordinates of a station of RING alone: the same wait, the port named in the log
    web.requests.clear()
    web.sleeps.clear()
    web.plan = [socket.timeout("timed out") for _ in range(3)]
    with caplog.at_level(logging.WARNING):
        found = stations.coordinates(ring, 2026.8)
    assert found.source == "approximate"
    assert len(web.requests) == 3 and {timeout for _r, timeout, _k in web.requests} == {stations.RING_TIMEOUT}
    assert any("RING is on port 44324" in text for text in _warnings(caplog))
    # cancelled while it waits: no other attempt
    progress = Progress()

    def first(request, timeout=None, **kwargs):
        progress.cancel()
        raise socket.timeout("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", first)
    download._LISTINGS.clear()
    with pytest.raises(Cancelled):
        stations.download_observations(ring, _t(26, 10), _t(26, 10, 30), tmp_path / "out", progress=progress)


# --- the observations and the navigation of a station -----------------------------------------------------------------
#
# BKG and RING as trees of files in the layout of their archives (their index pages, looked at on 2026-10-27):
#   BKG   EUREF|IGS/obs/<yyyy>/<ddd>/                daily files at 30 s
#         EUREF|IGS/nrt/<ddd>/<hh>/                  hourly files at 30 s
#         EUREF|IGS/highrate/<yyyy>/<ddd>/<a-x>/     pieces of 15 minutes at 1 s (IGS/highrate_permanent/ the same)
#         IGS/BRDC/<yyyy>/<ddd>/                     the navigation messages of the day
#   RING  <yyyy>/<ddd>/                              daily files at 30 s;   <yyyy>/<ddd>/<hh>/   hourly files at 1 s
# The files hold the observations of the hour of the EPN station KRAW00POL in tests/data/crinex with the times changed
# to those of their name. The epochs are 30 s apart whatever the interval in the name (a name is what is chosen, and a
# day at 1 s would be 100 MB); only the tests of the Hatanaka decoder use the file as BKG distributes it.

CRINEX = Path(__file__).parent / "data" / "crinex"
RTKLIB = Path(__file__).parent / "data" / "rtklib"
KRAW_HOUR = CRINEX / "KRAW00POL_R_20262991000_01H_30S_MO.crx"  # Compact RINEX 3.0, 2026-10-26 10:00:00 to 10:59:30 GPS
KRAW_NAME = "KRAW00POL_R_20262991000_01H_30S_MO.crx.gz"  # what BKG calls it in EUREF/nrt/299/10/
NAV_A, NAV_B = RTKLIB / "07590920.05n", RTKLIB / "30400920.05n"  # two navigation files with different text

needs_a_decoder = pytest.mark.skipif(_native.kernel("crx2rnx") is None and _native._crx2rnx_program() is None,
                                     reason="neither the native core 0.6 (API 9) nor a CRX2RNX program")


def _t(day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    """A time of October 2026 (the 26th is the day 299 of the year, the 27th the day 300), GPS time without a zone."""
    return datetime(2026, 10, day, hour, minute, second)


def _doy(when: datetime) -> str:
    return f"{when.timetuple().tm_yday:03d}"


def _made(sid: str, *networks: str) -> stations.Station:
    return stations.Station(sid, sid, networks, 50.0, 19.9, 300.0, (3856933.0, 1397750.0, 4867718.0))


@functools.lru_cache(maxsize=1)
def _kraw_hour() -> tuple[list[str], list[list[str]]]:
    """The header (without the lines that count the observations of that hour or name its first and last epoch) and four
    epochs of the hour of KRAW00POL in plain RINEX 3.04."""
    lines = (CRINEX / f"{KRAW_HOUR.name}.expected").read_text().splitlines()
    end = next(i for i, line in enumerate(lines) if line.rstrip().endswith("END OF HEADER")) + 1
    dropped = ("# OF SATELLITES", "PRN / # OF OBS", "TIME OF FIRST OBS", "TIME OF LAST OBS")
    header = [line for line in lines[:end] if line[60:].strip() not in dropped]
    epochs: list[list[str]] = []
    for line in lines[end:]:
        if line.startswith(">"):
            epochs.append([line])
        else:
            epochs[-1].append(line)
    return header, epochs[:4]


def _rinex_gz(first: datetime, last: datetime, skip=(), step: int = 30, header_first: datetime | None = None) -> bytes:
    """Gzipped RINEX with an epoch every ``step`` s from ``first`` up to ``last``, none in the ``skip`` spans (from,
    to). The header says that the first epoch is ``header_first`` when that is given: a header that lies."""
    header, template = _kraw_hour()
    said = header_first or first
    interval = f"{step:10.3f}".ljust(60) + "INTERVAL"
    top = [interval if line[60:].strip() == "INTERVAL" else line for line in header[:-1]] + [
        (f"{said.year:6d}{said.month:6d}{said.day:6d}{said.hour:6d}{said.minute:6d}{said.second:13.7f}     GPS"
         ).ljust(60) + "TIME OF FIRST OBS", header[-1]]
    body, when, n = [], first, 0
    while when < last:
        if not any(a <= when < b for a, b in skip):
            record = list(template[n % len(template)])
            record[0] = re.sub(r"^> \d{4} \d\d \d\d \d\d \d\d [ \d]\d\.\d{7}",
                               f"> {when:%Y %m %d %H %M} {when.second:02d}.0000000", record[0])
            body += record
        when += timedelta(seconds=step)
        n += 1
    return gzip.compress(("\n".join(top + body) + "\n").encode())


def _put(tree: Path, relative: str, data: bytes = b"x") -> Path:
    path = tree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _quarters(tree: Path, sid: str, first: datetime, last: datetime, root: str = "EUREF/highrate", source: str = "R",
              skip=(), step: int = 30) -> None:
    """The pieces of 15 minutes at 1 s of a station from ``first`` up to ``last``: BKG keeps a folder for each hour,
    named by a letter (a is the hour 0)."""
    when = first
    while when < last:
        name = f"{sid}_{source}_{when:%Y}{_doy(when)}{when:%H%M}_15M_01S_MO.crx.gz"
        _put(tree, f"{root}/{when:%Y}/{_doy(when)}/{chr(97 + when.hour)}/{name}",
             _rinex_gz(when, when + timedelta(minutes=15), skip, step))
        when += timedelta(minutes=15)


def _hours(tree: Path, sid: str, first: datetime, last: datetime, root: str = "EUREF/nrt", source: str = "R") -> None:
    """The hourly files at 30 s of a station for the hours from ``first`` up to ``last``, in the folders of the day
    and the hour."""
    when = first
    while when < last:
        name = f"{sid}_{source}_{when:%Y}{_doy(when)}{when:%H%M}_01H_30S_MO.crx.gz"
        _put(tree, f"{root}/{_doy(when)}/{when:%H}/{name}", _rinex_gz(when, when + timedelta(hours=1)))
        when += timedelta(hours=1)


def _day(tree: Path, sid: str, day: datetime, first: datetime, last: datetime, root: str = "EUREF/obs",
         source: str = "R", skip=()) -> str:
    """The file of a day at 30 s, which holds only the observations from ``first`` up to ``last`` (less the ``skip``
    spans); its name."""
    name = f"{sid}_{source}_{day:%Y}{_doy(day)}0000_01D_30S_MO.crx.gz"
    _put(tree, f"{root}/{day:%Y}/{_doy(day)}/{name}", _rinex_gz(first, last, skip))
    return name


def _ring_hours(ring: Path, sid: str, first: datetime, last: datetime) -> None:
    """RING's hourly files at 1 s: <yyyy>/<ddd>/<hh>/."""
    when = first
    while when < last:
        name = f"{sid}_R_{when:%Y}{_doy(when)}{when:%H}00_01H_01S_MO.crx.gz"
        _put(ring, f"{when:%Y}/{_doy(when)}/{when:%H}/{name}", _rinex_gz(when, when + timedelta(hours=1)))
        when += timedelta(hours=1)


def _ring_day(ring: Path, sid: str, day: datetime, first: datetime, last: datetime) -> str:
    name = f"{sid}_R_{day:%Y}{_doy(day)}0000_01D_30S_MO.crx.gz"
    _put(ring, f"{day:%Y}/{_doy(day)}/{name}", _rinex_gz(first, last))
    return name


@pytest.fixture
def trees(tmp_path, monkeypatch):
    """Empty archives of BKG and RING, and a clock two days after the sample hour: everything of the 26th is old."""
    bkg, ring = tmp_path / "bkg", tmp_path / "ring-rinex"
    bkg.mkdir()
    ring.mkdir()
    monkeypatch.setattr(stations, "BKG_ROOT", _uri(bkg) + "/")
    monkeypatch.setattr(stations, "RING_RINEX", _uri(ring) + "/")
    monkeypatch.setattr(stations, "_now", lambda: datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc))
    return SimpleNamespace(bkg=bkg, ring=ring, url=_uri(bkg) + "/", ring_url=_uri(ring) + "/")


def _remote(url: str, name: str, start: datetime, hours: float, interval: float) -> stations.RemoteFile:
    return stations.RemoteFile(url + name, name, start, start + timedelta(hours=hours), interval)


def _names(files) -> list[str]:
    return [f.name for f in files]


def _epochs(path: Path) -> int:
    return sum(1 for line in path.read_text().splitlines() if line.startswith(">"))


def _clock(monkeypatch, *when: int) -> None:
    """The clock (UTC) of the program set to a time of 2026."""
    monkeypatch.setattr(stations, "_now", lambda: datetime(2026, *when, tzinfo=timezone.utc))


# --- which files ------------------------------------------------------------------------------------------------------


def test_one_second_files_when_they_cover_the_flight(trees):
    """1 s is chosen when its pieces of 15 minutes cover the flight and 10 minutes on each side, even when the 30 s
    files are there too; a piece is wanted only when it overlaps that time (the window is open at its end)."""
    kraw = _made("KRAW00POL", "EPN")
    _quarters(trees.bkg, kraw.id, _t(26, 9, 30), _t(26, 12))
    _day(trees.bkg, kraw.id, _t(26), _t(26, 9), _t(26, 13))
    _hours(trees.bkg, kraw.id, _t(26, 9), _t(26, 13))
    folder = trees.url + "EUREF/highrate/2026/299/k/"

    files = stations.observation_files(kraw, _t(26, 10, 20), _t(26, 10, 35))  # 10:10 to 10:45 with the margin
    assert files == [_remote(folder, f"KRAW00POL_R_2026299{hhmm}_15M_01S_MO.crx.gz", _t(26, 10, minute), 0.25, 1.0)
                     for hhmm, minute in (("1000", 0), ("1015", 15), ("1030", 30))]
    assert stations.MARGIN == timedelta(minutes=10)
    # one minute more of flight and the piece of 10:45 is wanted; from 10:25 the one of 10:00 is not
    longer = stations.observation_files(kraw, _t(26, 10, 20), _t(26, 10, 36))
    later = stations.observation_files(kraw, _t(26, 10, 25), _t(26, 10, 35))
    assert longer[-1].name.endswith("1045_15M_01S_MO.crx.gz") and later[0].name.endswith("1015_15M_01S_MO.crx.gz")
    # a flight over an hour takes the folders of both hours
    over = stations.observation_files(kraw, _t(26, 10, 50), _t(26, 11, 5))
    assert [f.url.split("/")[-2] for f in over] == ["k", "k", "l"] and over[-1].name.endswith("1100_15M_01S_MO.crx.gz")
    # the rate "1s" gives the same, and "30s" the file of the day
    assert stations.observation_files(kraw, _t(26, 10, 20), _t(26, 10, 35), rate="1s") == files
    assert stations.observation_files(kraw, _t(26, 10, 20), _t(26, 10, 35), rate="30s") == [
        _remote(trees.url + "EUREF/obs/2026/299/", "KRAW00POL_R_20262990000_01D_30S_MO.crx.gz", _t(26), 24, 30.0)]


def test_thirty_seconds_when_one_second_files_miss_an_hour(trees):
    """The 1 s pieces are there for the hour 10 and not for the hour 11, which the flight (with its margin) reaches: the
    30 s files are used - the file of the day when the listing has it, else the hourly files of nrt."""
    kraw = _made("KRAW00POL", "EPN")
    _quarters(trees.bkg, kraw.id, _t(26, 10), _t(26, 11))
    daily = _day(trees.bkg, kraw.id, _t(26), _t(26, 9), _t(26, 13))
    _hours(trees.bkg, kraw.id, _t(26, 9), _t(26, 13))
    start, end = _t(26, 10, 50), _t(26, 11, 10)

    assert stations.observation_files(kraw, start, end) == [
        _remote(trees.url + "EUREF/obs/2026/299/", daily, _t(26), 24, 30.0)]
    # an explicit "1s" does not fall back: it names the pieces that are missing, and says that 30 s files are there
    with pytest.raises(ValueError) as error:
        stations.observation_files(kraw, start, end, rate="1s")
    assert "KRAW00POL_[RSU]_20262991100_15M_01S_MO.crx.gz" in str(error.value)
    assert "The 30 s files for these hours are online" in str(error.value)

    (trees.bkg / "EUREF/obs/2026/299" / daily).unlink()  # no file of the day: the hourly files of nrt
    download._LISTINGS.clear()
    assert stations.observation_files(kraw, start, end) == [
        _remote(trees.url + "EUREF/nrt/299/10/", "KRAW00POL_R_20262991000_01H_30S_MO.crx.gz", _t(26, 10), 1, 30.0),
        _remote(trees.url + "EUREF/nrt/299/11/", "KRAW00POL_R_20262991100_01H_30S_MO.crx.gz", _t(26, 11), 1, 30.0)]


def test_a_flight_over_midnight_takes_the_day_or_the_hours_of_each_day(trees):
    kraw = _made("KRAW00POL", "EPN")
    first_day = _day(trees.bkg, kraw.id, _t(26), _t(26, 23), _t(27))
    start, end = _t(26, 23, 50), _t(27, 0, 20)  # 23:40 to 00:30 with the margin
    _hours(trees.bkg, kraw.id, _t(27, 0), _t(27, 1))  # the file of the day 300 is not out: its first hour is
    got = stations.observation_files(kraw, start, end)
    assert _names(got) == [first_day, "KRAW00POL_R_20263000000_01H_30S_MO.crx.gz"]
    assert [(f.start, f.end) for f in got] == [(_t(26), _t(27)), (_t(27), _t(27, 1))]
    _day(trees.bkg, kraw.id, _t(27), _t(27), _t(27, 2))  # it comes out: a file for each day
    download._LISTINGS.clear()
    assert _names(stations.observation_files(kraw, start, end)) == [
        first_day, "KRAW00POL_R_20263000000_01D_30S_MO.crx.gz"]


def test_the_flight_is_enough_when_the_margin_is_not_there(trees, monkeypatch):
    """The file of the hour 11 is not out, and the flight ends at 10:56, 4 minutes before it: the hour 10 holds the
    whole flight and is used. Auto does not take 1 s pieces that miss a margin when 30 s files do the work, and ends
    with them when nothing else holds the flight."""
    _clock(monkeypatch, 10, 26, 11, 2)
    kraw = _made("KRAW00POL", "EPN")
    _hours(trees.bkg, kraw.id, _t(26, 10), _t(26, 11))
    _quarters(trees.bkg, kraw.id, _t(26, 10), _t(26, 11))
    # 1 s needs 11:00 for the margin (until 11:06): not complete, so auto takes the hourly file of 30 s
    assert _names(stations.observation_files(kraw, _t(26, 10, 50), _t(26, 10, 56))) == [KRAW_NAME]
    # an explicit rate accepts what holds the flight: the pieces of 10:30 and 10:45
    assert len(stations.observation_files(kraw, _t(26, 10, 50), _t(26, 10, 56), rate="1s")) == 2
    # a flight that reaches into the hour 11 needs its file
    with pytest.raises(ValueError, match="KRAW00POL_.RSU._20262991100_01H_30S_MO.crx.gz"):
        stations.observation_files(kraw, _t(26, 10, 50), _t(26, 11, 1), rate="30s")
    # no 30 s files at all: the 1 s pieces that hold the flight
    shutil.rmtree(trees.bkg / "EUREF/nrt")
    download._LISTINGS.clear()
    assert len(stations.observation_files(kraw, _t(26, 10, 50), _t(26, 10, 56))) == 2


def test_only_real_names_of_the_listing_are_taken(trees, monkeypatch):
    """Nothing is made up: the folder is listed and the names that are the station's, for this time, interval and kind
    of file, are taken - R before S before U when a time has several - and nothing else is ever asked for."""
    kraw = _made("KRAW00POL", "EPN")
    folder = "EUREF/nrt/299/10/"
    for name in ("KRAW00POL_S_20262991000_01H_30S_MO.crx.gz",  # the copy from the stream: R is preferred
                 KRAW_NAME,
                 "KRAW00POL_R_20262991000_01H_MN.rnx.gz",  # navigation messages
                 "KRAW00POL_R_20262991000_01H_05S_MO.crx.gz",  # another interval
                 "KRAW00POL_R_20262991000_01H_30S_MM.rnx.gz",  # meteorology
                 "KRAW00POL_R_20262991000_01H_30S_MO.rnx.gz",  # not Hatanaka
                 "KRAW00POL_R_20262991100_01H_30S_MO.crx.gz",  # another hour
                 "KRAW00POL_R_20262991000_15M_30S_MO.crx.gz",  # another length of file
                 "KRAW01POL_R_20262991000_01H_30S_MO.crx.gz",  # another station
                 "AQUI00ITA_R_20262991000_01H_30S_MO.crx.gz"):
        _put(trees.bkg, folder + name)
    calls = _fetches(monkeypatch)
    span = (_t(26, 10, 20), _t(26, 10, 35))
    assert _names(stations.observation_files(kraw, *span, rate="30s")) == [KRAW_NAME]
    (trees.bkg / folder / KRAW_NAME).unlink()
    download._LISTINGS.clear()
    assert _names(stations.observation_files(kraw, *span, rate="30s")) == ["KRAW00POL_S_20262991000_01H_30S_MO.crx.gz"]
    (trees.bkg / folder / "KRAW00POL_S_20262991000_01H_30S_MO.crx.gz").unlink()
    _put(trees.bkg, folder + "KRAW00POL_U_20262991000_01H_30S_MO.crx.gz")
    download._LISTINGS.clear()
    assert _names(stations.observation_files(kraw, *span, rate="30s")) == ["KRAW00POL_U_20262991000_01H_30S_MO.crx.gz"]
    assert calls == [] and not (download.gnss_dir() / "stations").exists()  # choosing files downloads and writes none


def test_names_from_a_listing_stay_in_the_cache_folder(trees, tmp_path, monkeypatch):
    """A listing can name anything: a colon (a drive on Windows), a new line, other digits, a way out of the folder.
    Only a name that is wholly the name of a file of the station is taken, and nothing is written outside the folder."""
    kraw = _made("KRAW00POL", "EPN")
    good = KRAW_NAME
    hostile = [good + ":evil", "D:x.gz", good + "\n", "\n" + good, good + " ", good + "\nx", good + "/..", "../" + good,
               "..\\" + good, "KRAW00POL_R_٢٠٢٦٢٩٩١٠٠٠_01H_30S_MO.crx.gz",  # Arabic-Indic digits
               "KRAW00POL_R_2026299100_01H_30S_MO.crx.gz", "kraw00pol_R_20262991000_01H_30S_MO.crx.gz"]
    monkeypatch.setattr(download, "listing",
                        lambda url, progress=None: hostile + ([good] if url.endswith("/nrt/299/10/") else []))
    assert _names(stations.observation_files(kraw, _t(26, 10, 20), _t(26, 10, 35), rate="30s")) == [good]
    # with only the hostile names the file is missing, and nothing is fetched
    monkeypatch.setattr(download, "listing", lambda url, progress=None: hostile)
    calls = _fetches(monkeypatch)
    with pytest.raises(ValueError, match="is not in"):
        stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), tmp_path / "base", rate="30s")
    assert calls == []

    # a name that got through anyway (a plan made by hand) is not written: it is checked before anything is fetched
    escaped = tmp_path / "escaped.crx.gz"
    for name in ("../escaped.crx.gz", "D:escaped.crx.gz", "sub/../../escaped.crx.gz", str(escaped), "escaped.crx.gz\n"):
        remote = stations.RemoteFile(trees.url + "x", name, _t(26, 10), _t(26, 11), 30.0)
        with monkeypatch.context() as patch:
            patch.setattr(stations, "observation_files", lambda *a, remote=remote, **k: [remote])
            with pytest.raises(ValueError, match="folder of the station"):
                stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), tmp_path / "base")
    assert calls == [] and not list(tmp_path.rglob("escaped*")) and not escaped.exists()
    # the same for a station whose id would be a path
    with pytest.raises(ValueError, match="station"):
        stations.observation_files(_made("../../ABC", "EPN"), _t(26, 10, 20), _t(26, 10, 35))


def test_an_epn_station_comes_from_euref_and_an_igs_station_from_igs(trees):
    span = (_t(26, 10, 20), _t(26, 10, 35))
    _hours(trees.bkg, "KRAW00POL", _t(26, 10), _t(26, 11))
    _hours(trees.bkg, "BSHM00ISR", _t(26, 10), _t(26, 11), root="IGS/nrt")
    assert [f.url for f in stations.observation_files(_made("KRAW00POL", "EPN"), *span)] == [
        trees.url + "EUREF/nrt/299/10/" + KRAW_NAME]
    assert [f.url for f in stations.observation_files(_made("BSHM00ISR", "IGS"), *span)] == [
        trees.url + "IGS/nrt/299/10/BSHM00ISR_R_20262991000_01H_30S_MO.crx.gz"]
    # each looks in its own tree only
    with pytest.raises(ValueError, match="BSHM00ISR_.RSU._20262991000_01H_30S_MO.crx.gz"):
        stations.observation_files(_made("BSHM00ISR", "EPN"), *span)
    # a station of both networks: EUREF first, then IGS
    both = _made("M0SE00ITA", "EPN", "IGS")
    _hours(trees.bkg, both.id, _t(26, 10), _t(26, 11), root="IGS/nrt")
    download._LISTINGS.clear()
    assert stations.observation_files(both, *span)[0].url.startswith(trees.url + "IGS/nrt/299/10/")
    _hours(trees.bkg, both.id, _t(26, 10), _t(26, 11))
    download._LISTINGS.clear()
    assert stations.observation_files(both, *span)[0].url.startswith(trees.url + "EUREF/nrt/299/10/")


def test_igs_one_second_pieces_come_from_highrate_or_the_permanent_folder(trees):
    span = (_t(26, 10, 20), _t(26, 10, 35))
    bshm = _made("BSHM00ISR", "IGS")
    _quarters(trees.bkg, bshm.id, _t(26, 9, 30), _t(26, 11, 30), root="IGS/highrate_permanent")
    got = stations.observation_files(bshm, *span)
    permanent = trees.url + "IGS/highrate_permanent/2026/299/k/"
    assert len(got) == 3 and all(f.url.startswith(permanent) and f.interval == 1.0 for f in got)
    # the folder of the recent pieces is the first choice for a time it has; the other fills in what it lacks
    _quarters(trees.bkg, bshm.id, _t(26, 10), _t(26, 10, 30), root="IGS/highrate")
    download._LISTINGS.clear()
    assert [f.url.split("/")[-5] for f in stations.observation_files(bshm, *span)] == [
        "highrate", "highrate", "highrate_permanent"]


def test_a_ring_station_comes_from_ring(trees):
    span = (_t(26, 10, 20), _t(26, 10, 35))
    _ring_hours(trees.ring, "INGR00ITA", _t(26, 9), _t(26, 12))
    _ring_day(trees.ring, "INGR00ITA", _t(26), _t(26, 9), _t(26, 13))
    ingr = _made("INGR00ITA", "RING")
    assert stations.observation_files(ingr, *span) == [
        _remote(trees.ring_url + "2026/299/10/", "INGR00ITA_R_20262991000_01H_01S_MO.crx.gz", _t(26, 10), 1, 1.0)]
    assert stations.observation_files(ingr, *span, rate="30s") == [
        _remote(trees.ring_url + "2026/299/", "INGR00ITA_R_20262990000_01D_30S_MO.crx.gz", _t(26), 24, 30.0)]
    assert len(stations.observation_files(ingr, _t(26, 10, 50), _t(26, 11, 5))) == 2  # the hours 10 and 11
    # RING has no hourly files at 30 s, and BKG's folders are not its own
    _hours(trees.bkg, "INGR00ITA", _t(26, 10), _t(26, 11))
    (trees.ring / "2026/299/INGR00ITA_R_20262990000_01D_30S_MO.crx.gz").unlink()
    download._LISTINGS.clear()
    with pytest.raises(ValueError, match="INGR00ITA_.RSU._20262990000_01D_30S_MO.crx.gz"):
        stations.observation_files(ingr, *span, rate="30s")


def test_bkg_first_and_ring_when_bkg_does_not_have_the_span(trees):
    """A station of the EPN and of RING: the whole span from BKG when BKG has it, else the whole span from RING (the
    pieces of one archive join; those of two are of two processings)."""
    span = (_t(26, 10, 20), _t(26, 10, 35))
    both = _made("M0SE00ITA", "EPN", "RING")
    _ring_hours(trees.ring, both.id, _t(26, 9), _t(26, 12))
    assert stations.observation_files(both, *span, rate="1s")[0].url.startswith(trees.ring_url)  # BKG has no 1 s files
    _quarters(trees.bkg, both.id, _t(26, 9, 30), _t(26, 11, 30))
    download._LISTINGS.clear()
    got = stations.observation_files(both, *span, rate="1s")
    assert len(got) == 3 and all(f.url.startswith(trees.url + "EUREF/highrate/") for f in got)
    # one piece of the span missing at BKG: RING's file, which holds all of it
    (trees.bkg / "EUREF/highrate/2026/299/k/M0SE00ITA_R_20262991015_15M_01S_MO.crx.gz").unlink()
    download._LISTINGS.clear()
    assert [f.url for f in stations.observation_files(both, *span, rate="1s")] == [
        trees.ring_url + "2026/299/10/M0SE00ITA_R_20262991000_01H_01S_MO.crx.gz"]


def test_a_station_of_no_network_has_no_archive(trees):
    with pytest.raises(ValueError, match="no archive"):
        stations.observation_files(_made("ZZZZ00XXX"), _t(26, 10, 20), _t(26, 10, 35))


def test_the_times_are_gps_times_and_the_rate_is_one_of_three(trees, tmp_path):
    kraw = _made("KRAW00POL", "EPN")
    aware = datetime(2026, 10, 26, 10, 20, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="GPS time without a time zone"):
        stations.observation_files(kraw, aware, _t(26, 10, 35))
    with pytest.raises(ValueError, match="GPS time without a time zone"):
        stations.download_observations(kraw, _t(26, 10, 20), aware, tmp_path)
    with pytest.raises(ValueError, match="GPS time without a time zone"):
        stations.download_navigation(aware, _t(26, 10, 35), tmp_path)
    with pytest.raises(ValueError, match="before"):
        stations.observation_files(kraw, _t(26, 10, 35), _t(26, 10, 20))
    with pytest.raises(ValueError, match="rate"):
        stations.observation_files(kraw, _t(26, 10, 20), _t(26, 10, 35), rate="5s")


# --- what is not there, and when it comes ----------------------------------------------------------------------------


def test_today_not_published_says_when(trees, monkeypatch):
    """The 27th, 08:50 UTC: the flight of 07:40 to 08:10 has the hourly file of 07 but not the one of 08 (they come
    after the hour) and no file of the day (30 minutes after midnight). The error names the files, says when they come
    and that the 1 s pieces are there."""
    _clock(monkeypatch, 10, 27, 8, 50)
    kraw = _made("KRAW00POL", "EPN")
    _hours(trees.bkg, kraw.id, _t(27, 7), _t(27, 8))
    _quarters(trees.bkg, kraw.id, _t(27, 7, 30), _t(27, 8, 30))  # to the piece of 08:15, which was out at 08:45
    with pytest.raises(ValueError) as error:
        stations.observation_files(kraw, _t(27, 7, 40), _t(27, 8, 10), rate="30s")
    message = str(error.value)
    assert "KRAW00POL_[RSU]_20263000800_01H_30S_MO.crx.gz" in message  # the file, not a guess of its letter
    assert "KRAW00POL_[RSU]_20263000000_01D_30S_MO.crx.gz" in message
    assert "BKG hourly files are online after the hour" in message
    assert "BKG daily files are online about 30 minutes after midnight UTC" in message
    assert "not published yet" in message
    assert "The 1 s files for these hours are online" in message  # the other rate is there: say so
    assert "KRAW00POL_[RSU]_20263000700_01H" not in message  # the hour that is there is not complained of
    assert not isinstance(error.value, download.DownloadError)  # a file that is not out yet is not a failure to connect
    # with "auto" the 1 s pieces are what comes out
    assert len(stations.observation_files(kraw, _t(27, 7, 40), _t(27, 8, 10))) == 4


def test_nothing_published_yet_names_both_rates(trees, monkeypatch):
    _clock(monkeypatch, 10, 27, 8, 50)
    kraw = _made("KRAW00POL", "EPN")
    with pytest.raises(ValueError) as error:
        stations.observation_files(kraw, _t(27, 8, 5), _t(27, 8, 35))
    message = str(error.value)
    assert "30 s files:" in message and "1 s files:" in message
    assert "KRAW00POL_[RSU]_20263000800_01H_30S_MO.crx.gz" in message
    assert "KRAW00POL_[RSU]_20263000800_15M_01S_MO.crx.gz" in message
    assert "BKG 15 minute files are online after each quarter of an hour" in message
    # asking for one rate says that the other is not there either
    with pytest.raises(ValueError, match="The 1 s files for these hours are not there either"):
        stations.observation_files(kraw, _t(27, 8, 5), _t(27, 8, 35), rate="30s")


def test_the_clock_that_says_a_file_is_late_is_gps_time(monkeypatch):
    _clock(monkeypatch, 10, 27, 8, 50)
    assert stations._gps_now() == datetime(2026, 10, 27, 8, 50, 18)  # 18 s ahead of UTC since 2017


def test_an_old_missing_file_is_not_called_unpublished(trees):
    """Two days later nothing is going to come: the message says where the file is not, not that it is late."""
    kraw = _made("KRAW00POL", "EPN")
    with pytest.raises(ValueError) as error:
        stations.observation_files(kraw, _t(26, 8, 5), _t(26, 8, 35), rate="30s")
    message = str(error.value)
    assert "KRAW00POL_[RSU]_20262990800_01H_30S_MO.crx.gz is not in " + trees.url + "EUREF/nrt/299/08/" in message
    assert "not published yet" not in message and "online" not in message


def test_ring_unreachable_names_the_port(trees, web, monkeypatch):
    """RING answers on the port 44324, which some firewalls close: the error says what could not be reached, which port,
    and that a firewall may be why."""
    monkeypatch.setattr(stations, "RING_RINEX", "https://webring.gm.ingv.it:44324/rinex/RING/")
    web.plan = [urllib.error.URLError(ConnectionRefusedError())] * 40
    ingr = _made("INGR00ITA", "RING")
    with pytest.raises(ValueError) as error:
        stations.observation_files(ingr, _t(26, 10, 20), _t(26, 10, 35))
    message = str(error.value)
    assert "44324" in message and "a firewall may block it" in message and "RING could not be reached" in message
    assert "webring.gm.ingv.it" in message  # the address that did not answer
    assert len(web.requests) == 3 and web.sleeps == [2, 4]  # three attempts, once: not again for each rate
    assert "files:" not in message and "not there either" not in message  # nothing was looked at: nothing is claimed
    web.plan = [urllib.error.URLError(ConnectionRefusedError())] * 40
    with pytest.raises(ValueError, match="a firewall may block it") as asked:  # a rate asked for says the same
        stations.observation_files(ingr, _t(26, 10, 20), _t(26, 10, 35), rate="30s")
    assert "not there either" not in str(asked.value)

    # files that are found and then cannot be fetched say the same
    monkeypatch.setattr(stations, "RING_RINEX", trees.ring_url)
    _ring_hours(trees.ring, "INGR00ITA", _t(26, 9), _t(26, 12))

    def refused(url, *args, **kwargs):
        raise download.DownloadError(f"{url}: could not connect (refused) (after 3 attempts)", url)

    monkeypatch.setattr(download, "fetch", refused)
    with pytest.raises(ValueError) as error:
        stations.download_observations(ingr, _t(26, 10, 20), _t(26, 10, 35), trees.ring.parent / "base")
    assert "44324" in str(error.value) and "a firewall may block it" in str(error.value)
    assert "INGR00ITA_R_20262991000_01H_01S_MO.crx.gz" in str(error.value)


def test_ring_serves_a_station_of_the_epn_when_bkg_cannot_be_reached(trees, web, monkeypatch):
    monkeypatch.setattr(stations, "BKG_ROOT", "https://igs.bkg.bund.de/root_ftp/")
    web.plan = [urllib.error.URLError(ConnectionRefusedError())] * 40  # BKG does not answer
    both = _made("M0SE00ITA", "EPN", "IGS", "RING")
    _ring_hours(trees.ring, both.id, _t(26, 9), _t(26, 12))
    got = stations.observation_files(both, _t(26, 10, 20), _t(26, 10, 35))
    assert [f.url for f in got] == [trees.ring_url + "2026/299/10/M0SE00ITA_R_20262991000_01H_01S_MO.crx.gz"]
    assert len(web.requests) == 3  # BKG was tried once, for its two trees and for every rate


def test_bkg_alone_is_enough_for_an_epn_station_and_ring_is_not_asked(trees, web, monkeypatch):
    monkeypatch.setattr(stations, "RING_RINEX", "https://webring.gm.ingv.it:44324/rinex/RING/")
    web.plan = []  # any request to RING would fail the test
    both = _made("M0SE00ITA", "EPN", "RING")
    _hours(trees.bkg, both.id, _t(26, 10), _t(26, 11))
    assert len(stations.observation_files(both, _t(26, 10, 20), _t(26, 10, 35), rate="30s")) == 1
    assert web.requests == []  # (with "auto" RING's hourly pieces at 1 s would be looked for: they beat 30 s files)
    # BKG has nothing and RING cannot be reached: the message tells both stories
    shutil.rmtree(trees.bkg / "EUREF")
    web.plan = [urllib.error.URLError(ConnectionRefusedError())] * 40
    download._LISTINGS.clear()
    with pytest.raises(ValueError) as error:
        stations.observation_files(both, _t(26, 10, 20), _t(26, 10, 35))
    assert "M0SE00ITA_[RSU]_20262991000_01H_30S_MO.crx.gz is not in" in str(error.value) and "44324" in str(error.value)


# --- download, decode and join --------------------------------------------------------------------------------------


def test_download_joins_and_reuses_the_cache(trees, tmp_path, monkeypatch):
    kraw = _made("KRAW00POL", "EPN")
    _quarters(trees.bkg, kraw.id, _t(26, 10), _t(26, 11))
    calls = _fetches(monkeypatch)
    base = tmp_path / "base"
    pruned = []
    real_prune = rinex.prune_cache
    monkeypatch.setattr(rinex, "prune_cache", lambda *a, **k: pruned.append(1) or real_prune(*a, **k))

    out = stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), base)
    assert pruned == [1]  # the decoded copies of earlier downloads kept within their days and bytes, first
    assert out == base / "KRAW00POL_20261026-1020_1035.obs"
    assert [url.rsplit("_", 3)[0].rsplit("_", 1)[1] for url in calls] == [
        "2026299" + hhmm for hhmm in ("1000", "1015", "1030")]  # the start of each piece, as the name says
    # the three pieces, once each: 10:00:00 to 10:44:30, an epoch every 30 s, no hole
    info = rinex.header(out)
    assert (info.first, info.last) == (_t(26, 10), _t(26, 10, 44, 30)) and info.marker == "KRAW00POL"
    assert _epochs(out) == 90
    cache = download.gnss_dir() / "stations" / "KRAW00POL"  # the pieces stay as the archive names them
    assert sorted(p.name for p in cache.iterdir()) == [f"KRAW00POL_R_2026299{h}_15M_01S_MO.crx.gz"
                                                       for h in ("1000", "1015", "1030")]
    # a second call fetches nothing, and makes the file again when it is gone
    out.unlink()
    assert stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), base) == out
    assert len(calls) == 3 and _epochs(out) == 90
    # another flight over the same pieces: only the piece it did not have is fetched
    other = stations.download_observations(kraw, _t(26, 10, 31), _t(26, 10, 36), base)
    assert other.name == "KRAW00POL_20261026-1031_1036.obs" and len(calls) == 4
    assert calls[-1].endswith("1045_15M_01S_MO.crx.gz")


def test_the_name_of_the_file_over_midnight(trees, tmp_path):
    kraw = _made("KRAW00POL", "EPN")
    _day(trees.bkg, kraw.id, _t(26), _t(26, 23), _t(27))
    _day(trees.bkg, kraw.id, _t(27), _t(27), _t(27, 1))
    out = stations.download_observations(kraw, _t(26, 23, 50), _t(27, 0, 20), tmp_path / "base")
    assert out.name == "KRAW00POL_20261026-2350_0020.obs"  # the end is a time of day, as the start is of a date
    info = rinex.header(out)
    assert (info.first, info.last) == (_t(26, 23), _t(27, 0, 59, 30)) and _epochs(out) == 240


@needs_a_decoder
def test_an_hour_as_bkg_distributes_it_is_decoded_and_joined(trees, tmp_path):
    """The file of EUREF/nrt as it is (Hatanaka inside gzip): what comes out is the RINEX that CRX2RNX makes of it."""
    kraw = _made("KRAW00POL", "EPN")
    _put(trees.bkg, f"EUREF/nrt/299/10/{KRAW_NAME}", gzip.compress(KRAW_HOUR.read_bytes()))
    out = stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), tmp_path / "base")
    expected = (CRINEX / f"{KRAW_HOUR.name}.expected").read_text().splitlines()
    produced = out.read_text().splitlines()

    def body(lines):
        return lines[next(i for i, line in enumerate(lines) if line.rstrip().endswith("END OF HEADER")) + 1:]

    assert body(produced) == body(expected) and len(body(produced)) > 1000
    assert rinex.header(out).marker == "KRAW00POL"


def test_a_gap_in_the_base_during_the_flight_is_refused_but_not_one_in_the_margins(trees, tmp_path):
    kraw = _made("KRAW00POL", "EPN")
    base = tmp_path / "base"
    # no epoch from 10:12:00 to 10:15:00 and from 10:38:00 to 10:41:00: in the 10 minutes before the flight, and in
    # the 10 minutes after it - the base is whole where the flight is
    _quarters(trees.bkg, kraw.id, _t(26, 10), _t(26, 11),
              skip=[(_t(26, 10, 12), _t(26, 10, 15)), (_t(26, 10, 38), _t(26, 10, 41))])
    out = stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), base)
    assert out.exists() and _epochs(out) == 90 - 12

    # a hole in the flight: the base is refused, with the hole and the flight in the message, and no file is left
    # (the pieces are other files with the same names: the copies of the first ones are forgotten)
    shutil.rmtree(trees.bkg / "EUREF")
    shutil.rmtree(download.gnss_dir() / "stations" / kraw.id)
    _quarters(trees.bkg, kraw.id, _t(26, 10), _t(26, 11), skip=[(_t(26, 10, 24), _t(26, 10, 27))])
    download._LISTINGS.clear()
    with pytest.raises(ValueError) as error:
        stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), base)
    message = str(error.value)
    assert "10:23:30" in message and "10:27:00" in message and "flight" in message and "KRAW00POL" in message
    assert "tried next" in message  # "auto" tried the 30 s files after the 1 s ones: there are none, and it says so
    assert not out.exists()  # the base of the last run is not left for something else to take

    # a base that begins after the flight does: the same refusal
    shutil.rmtree(trees.bkg / "EUREF")
    _put(trees.bkg, f"EUREF/nrt/299/10/{KRAW_NAME}", _rinex_gz(_t(26, 10, 22), _t(26, 11)))  # the receiver started late
    download._LISTINGS.clear()
    with pytest.raises(ValueError, match="do not cover the flight"):
        stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), base, rate="30s")


def _another_antenna(piece: Path) -> None:
    """The piece of a station with another antenna in its header: it does not join the pieces before it."""
    piece.write_bytes(gzip.compress(gzip.decompress(piece.read_bytes()).replace(b"ASH701945C_M    SNOW",
                                                                                b"TRM59800.00     NONE")))


def test_pieces_that_cannot_be_joined_are_refused_naming_the_station(trees, tmp_path):
    """The pieces of a station have one antenna: a change in the middle of them is reported, not hidden. With "auto"
    the 30 s files are tried next, and there are none here: both reasons are given, and nothing says to ask for the 1 s
    files that were just tried. With the rate asked for, no other rate is tried."""
    kraw = _made("KRAW00POL", "EPN")
    _quarters(trees.bkg, kraw.id, _t(26, 10), _t(26, 11))
    _another_antenna(trees.bkg / "EUREF/highrate/2026/299/k/KRAW00POL_R_20262991030_15M_01S_MO.crx.gz")
    with pytest.raises(ValueError, match="KRAW00POL: .*TRM59800.00") as error:
        stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), tmp_path / "base")
    message = str(error.value)
    assert "tried next" in message and "30S_MO.crx.gz is not in" in message and "ask for the rate" not in message
    with pytest.raises(ValueError, match="KRAW00POL: .*TRM59800.00") as error:
        stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), tmp_path / "base", rate="1s")
    assert "tried next" not in str(error.value)
    assert not (tmp_path / "base").exists() or not list((tmp_path / "base").iterdir())


@needs_a_decoder
def test_a_damaged_piece_is_downloaded_again_once_then_refused(trees, tmp_path, monkeypatch):
    kraw = _made("KRAW00POL", "EPN")
    data = bytearray(gzip.compress(KRAW_HOUR.read_bytes()))
    data[len(data) // 2] ^= 0xFF  # inside the deflate stream: the checksum of gzip or the inflater says so
    _put(trees.bkg, f"EUREF/nrt/299/10/{KRAW_NAME}", bytes(data))
    calls = _fetches(monkeypatch)
    base = tmp_path / "base"
    with pytest.raises(ValueError) as error:
        stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), base, rate="30s")
    assert KRAW_NAME in str(error.value) and "again" in str(error.value)
    assert len(calls) == 2  # fetched, found damaged, fetched again, found damaged: refused
    cache = download.gnss_dir() / "stations" / "KRAW00POL"
    assert list(cache.iterdir()) == [] and not base.exists()  # a damaged file is not kept for the next call to trust

    # the archive is mended: the next call fetches the file and works
    _put(trees.bkg, f"EUREF/nrt/299/10/{KRAW_NAME}", gzip.compress(KRAW_HOUR.read_bytes()))
    out = stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), base, rate="30s")
    assert _epochs(out) == 120 and len(calls) == 3
    # a copy in the cache that went bad is replaced, once
    cached = cache / KRAW_NAME
    cached.write_bytes(cached.read_bytes()[:-40])
    assert stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), base, rate="30s") == out
    assert len(calls) == 4 and _epochs(out) == 120


def test_without_a_decoder_nothing_is_downloaded_again(trees, tmp_path, monkeypatch):
    """A file that cannot be decoded because there is no decoder is not damaged: say what to install, and ask once."""
    kraw = _made("KRAW00POL", "EPN")
    _hours(trees.bkg, kraw.id, _t(26, 10), _t(26, 11))
    calls = _fetches(monkeypatch)

    def no_decoder(path, folder=None):
        raise ValueError(f"{Path(path).name}: {_native.CRINEX_MISSING}")

    monkeypatch.setattr(rinex, "plain", no_decoder)
    with pytest.raises(ValueError, match="native core"):
        stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), tmp_path / "base")
    assert len(calls) == 1


def test_download_reports_progress_and_can_be_cancelled(trees, tmp_path):
    kraw = _made("KRAW00POL", "EPN")
    _quarters(trees.bkg, kraw.id, _t(26, 10), _t(26, 11))
    seen: list[float] = []
    stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), tmp_path / "base",
                                   progress=lambda fraction, message: seen.append(fraction))
    assert seen == sorted(seen) and seen[-1] == 1.0 and len(seen) >= 4

    progress = Progress()
    progress.cancel_requested = True
    with pytest.raises(Cancelled):
        stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), tmp_path / "other", progress=progress)
    assert not (tmp_path / "other").exists()


# --- files that are no good, holes in the base, and the fall back to 30 s -------------------------------------------


def _flip(data: bytes, at: int) -> bytes:
    broken = bytearray(data)
    broken[at] ^= 0xFF
    return bytes(broken)


_HTML = b"<html><body>Service temporarily unavailable</body></html>"


def _zipped(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("piece.rnx", data)
    return buffer.getvalue()


# What an archive can give instead of a good file, made from a good one. Damage in the first bytes of a gzip file leaves
# nothing to inflate for a look at its layers; an empty file, a page of HTML and a gzip file of something else are all
# "files", and a file that is cut short has its fault in the last bytes.
BAD_FILES = {
    "flipped byte 12": lambda good: _flip(good, 12),
    "flipped byte 30": lambda good: _flip(good, 30),
    "flipped byte 200": lambda good: _flip(good, 200),
    "flipped byte in the middle": lambda good: _flip(good, len(good) // 2),
    "cut short": lambda good: good[:-40],
    "empty": lambda good: b"",
    "html": lambda good: _HTML,
    "gzip of html": lambda good: gzip.compress(_HTML),
    "gzip of navigation messages": lambda good: gzip.compress(NAV_A.read_bytes()),
    "not gzip": lambda good: gzip.decompress(good),  # the text itself under the name of a gzip file
    "zip": lambda good: _zipped(gzip.decompress(good)),  # RINEX that a reader could open, but not a gzip file
}


def _good_piece(source: str) -> bytes:
    """An hour of KRAW00POL as gzip: RINEX as it is, or Hatanaka as BKG distributes it."""
    return _rinex_gz(_t(26, 10), _t(26, 11)) if source == "plain" else gzip.compress(KRAW_HOUR.read_bytes())


@pytest.mark.parametrize("source", ["plain", pytest.param("hatanaka", marks=needs_a_decoder)])
@pytest.mark.parametrize("damage", list(BAD_FILES))
def test_a_piece_that_is_no_good_is_fetched_again_once_and_never_kept(trees, tmp_path, monkeypatch, source, damage):
    """Whatever the archive gives instead of a file of observations is fetched again once and then refused, naming
    the file; it is never kept: when the archive is mended the next call fetches the good file, and a bad copy that is
    in the cache is replaced."""
    kraw = _made("KRAW00POL", "EPN")
    good = _good_piece(source)
    bad = BAD_FILES[damage](good)
    archive = _put(trees.bkg, f"EUREF/nrt/299/10/{KRAW_NAME}", bad)
    calls = _fetches(monkeypatch)
    cache = download.gnss_dir() / "stations" / "KRAW00POL"
    base = tmp_path / "base"

    def ask():
        return stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), base, rate="30s")

    with pytest.raises(ValueError) as error:
        ask()
    assert KRAW_NAME in str(error.value) and "again" in str(error.value)
    assert len(calls) == 2  # fetched, no good, fetched again, no good: refused
    assert list(cache.iterdir()) == [] and not base.exists()
    archive.write_bytes(good)  # the archive is mended: nothing was kept, the good file is fetched
    out = ask()
    assert len(calls) == 3 and _epochs(out) == 120
    (cache / KRAW_NAME).write_bytes(bad)  # a bad copy in the cache (from before) is replaced, once
    assert ask() == out and len(calls) == 4 and _epochs(out) == 120


def test_the_epochs_decide_whether_a_base_covers_the_flight_not_its_header(trees, tmp_path):
    """The header of the joined file is that of its first piece. One that says 10:00:00 over epochs that begin at 10:25
    must not let a base through that is shorter than the flight."""
    kraw = _made("KRAW00POL", "EPN")
    _put(trees.bkg, f"EUREF/nrt/299/10/{KRAW_NAME}", _rinex_gz(_t(26, 10, 25), _t(26, 11), header_first=_t(26, 10)))
    base = tmp_path / "base"
    with pytest.raises(ValueError, match="do not cover the flight") as error:
        stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), base, rate="30s")
    assert "10:25:00" in str(error.value) and not list(base.glob("*.obs"))  # what the epochs say is what is told
    # a flight inside the epochs is covered by the same file, whatever the header says
    out = stations.download_observations(kraw, _t(26, 10, 30), _t(26, 10, 40), base, rate="30s")
    assert rinex.header(out).first == _t(26, 10) and rinex.time_span(out)[0] == _t(26, 10, 25)  # the header does lie


SHORT = (_t(26, 10, 20), _t(26, 10, 20, 40))  # a flight of 40 seconds


def _one_second_base(trees, kraw, skip=(), thirty_seconds: bool = True, thirty_skip=()) -> None:
    """With a margin of a minute (the test sets it) the flight SHORT needs one piece of 15 minutes at 1 s, the one of
    10:15, and as the 30 s fall back the file of the day with the hour 10."""
    _quarters(trees.bkg, kraw.id, _t(26, 10, 15), _t(26, 10, 30), skip=skip, step=1)
    if thirty_seconds:
        _day(trees.bkg, kraw.id, _t(26), _t(26, 10), _t(26, 11), skip=thirty_skip)


@pytest.mark.parametrize("until, accepted", [(39, True), (40, False)])
def test_in_a_one_second_base_a_hole_of_thirty_seconds_is_accepted_and_a_longer_one_is_not(
        trees, tmp_path, monkeypatch, caplog, until, accepted):
    """RTKLIB takes base observations up to 30 s old: a hole of 30 s in the base during the flight is accepted, with a
    warning. One of 31 s sends "auto" to the 30 s files, which have no hole."""
    monkeypatch.setattr(stations, "MARGIN", timedelta(minutes=1))
    kraw = _made("KRAW00POL", "EPN")
    # no epoch from 10:20:10 to 10:20:<until>: the epochs around the hole are at 10:20:09 and at 10:20:<until>
    _one_second_base(trees, kraw, skip=[(_t(26, 10, 20, 10), _t(26, 10, 20, until))])
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        out = stations.download_observations(kraw, *SHORT, tmp_path / "base")
    warned = " ".join(_warnings(caplog))
    if accepted:
        assert rinex.header(out).interval == 1.0 and _epochs(out) == 900 - (until - 10)
        assert "10:20:09" in warned and "10:20:39" in warned and "30 s" in warned and "accepted" in warned
    else:
        assert rinex.header(out).interval == 30.0 and _epochs(out) == 120  # the file of the day
        assert "10:20:09" in warned and "10:20:40" in warned and "trying the 30 s files" in warned


def test_many_small_holes_in_a_one_second_base_are_one_warning(trees, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(stations, "MARGIN", timedelta(minutes=1))
    kraw = _made("KRAW00POL", "EPN")
    _one_second_base(trees, kraw, skip=[(_t(26, 10, 20, second), _t(26, 10, 20, second + 3)) for second in (5, 15, 25)])
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        out = stations.download_observations(kraw, *SHORT, tmp_path / "base")
    assert rinex.header(out).interval == 1.0 and _epochs(out) == 900 - 9  # three holes of 4 s between epochs
    holes = [w for w in _warnings(caplog) if "hole" in w]
    assert len(holes) == 1 and "3 holes" in holes[0] and "accepted" in holes[0]


def test_a_long_hole_in_a_one_second_base_is_an_error_when_the_rate_was_asked_for(trees, tmp_path, monkeypatch):
    monkeypatch.setattr(stations, "MARGIN", timedelta(minutes=1))
    kraw = _made("KRAW00POL", "EPN")
    _one_second_base(trees, kraw, skip=[(_t(26, 10, 20, 10), _t(26, 10, 20, 40))])
    calls = _fetches(monkeypatch)
    with pytest.raises(ValueError, match="10:20:09 to 10:20:40") as error:
        stations.download_observations(kraw, *SHORT, tmp_path / "base", rate="1s")
    assert "tried next" not in str(error.value) and len(calls) == 1  # no 30 s file was looked for, none was fetched
    assert not (tmp_path / "base").exists() or not list((tmp_path / "base").glob("*.obs"))


def test_auto_falls_back_to_thirty_seconds_when_the_one_second_base_ends_before_the_flight(
        trees, tmp_path, monkeypatch):
    monkeypatch.setattr(stations, "MARGIN", timedelta(minutes=1))
    monkeypatch.setattr(download, "BLOCK", 1024)  # a download reports its progress many times, not once
    kraw = _made("KRAW00POL", "EPN")
    _one_second_base(trees, kraw, skip=[(_t(26, 10, 20, 30), _t(26, 10, 30))])  # no epoch after 10:20:29
    seen: list[float] = []
    out = stations.download_observations(kraw, *SHORT, tmp_path / "base", progress=lambda f, m: seen.append(f))
    assert rinex.header(out).interval == 30.0 and _epochs(out) == 120
    assert len(seen) > 20 and seen == sorted(seen) and seen[-1] == 1.0  # the bar does not go back for the 30 s files
    with pytest.raises(ValueError, match="do not cover the flight") as error:  # with the rate asked for: refused
        stations.download_observations(kraw, *SHORT, tmp_path / "other", rate="1s")
    assert "10:20:29" in str(error.value)


def test_when_the_fall_back_fails_too_both_reasons_are_given(trees, tmp_path, monkeypatch):
    monkeypatch.setattr(stations, "MARGIN", timedelta(minutes=1))
    kraw = _made("KRAW00POL", "EPN")
    base = tmp_path / "base"
    _one_second_base(trees, kraw, skip=[(_t(26, 10, 20, 10), _t(26, 10, 20, 45))], thirty_seconds=False)
    with pytest.raises(ValueError) as error:  # there are no 30 s files at all
        stations.download_observations(kraw, *SHORT, base)
    message = str(error.value)
    assert "10:20:09 to 10:20:45" in message  # why the base at 1 s was not taken
    assert "tried next" in message  # and why the 30 s files were not taken either
    assert "KRAW00POL_[RSU]_20262990000_01D_30S_MO.crx.gz is not in" in message
    assert "ask for the rate" not in message  # the 1 s files are there, and were just given up
    # the file of the day has a hole of its own in the flight
    _day(trees.bkg, kraw.id, _t(26), _t(26, 10), _t(26, 11), skip=[(_t(26, 10, 19, 30), _t(26, 10, 21))])
    download._LISTINGS.clear()
    with pytest.raises(ValueError) as error:
        stations.download_observations(kraw, *SHORT, base)
    message = str(error.value)
    assert "10:20:09 to 10:20:45" in message and "10:19:00 to 10:21:00" in message and "tried next" in message
    assert not base.exists() or not list(base.glob("*.obs"))


# What can keep a base at 1 s from being had whole, other than a hole, done to the second of its two pieces, and the
# end of the flight: the second piece holds its last half minute, or only the margin after it
ONE_SECOND_TROUBLES = {
    "pieces that do not join": (_another_antenna, _t(26, 10, 30, 30)),
    "a piece that is no good": (lambda piece: piece.write_bytes(_HTML), _t(26, 10, 30, 30)),
    "a piece with only a header": (lambda piece: piece.write_bytes(_rinex_gz(_t(26, 10, 30), _t(26, 10, 30), step=1)),
                                   _t(26, 10, 30, 30)),
    "a piece of the margin that is no good": (lambda piece: piece.write_bytes(_HTML), _t(26, 10, 29, 50)),
    # listed, then gone from the archive: NotPublished, a ValueError of _fetch
    "a piece that cannot be downloaded": (lambda piece: piece.unlink(), _t(26, 10, 30, 30)),
}


@pytest.mark.parametrize("trouble", list(ONE_SECOND_TROUBLES))
def test_auto_falls_back_to_thirty_seconds_when_the_one_second_pieces_cannot_be_used(trees, tmp_path, monkeypatch,
                                                                                    caplog, trouble):
    """With "auto" a base at 1 s that cannot be had whole for any reason - its pieces do not join, one is no good even
    when downloaded again (also one that only holds the margin), one has no epochs, one that the listing of its folder
    names cannot be downloaded - is given up for the 30 s files, as one that does not cover the flight is."""
    monkeypatch.setattr(stations, "MARGIN", timedelta(minutes=1))
    kraw = _made("KRAW00POL", "EPN")
    _quarters(trees.bkg, kraw.id, _t(26, 10, 15), _t(26, 10, 45), step=1)  # the flight needs the pieces of 10:15, 10:30
    _day(trees.bkg, kraw.id, _t(26), _t(26, 10), _t(26, 11))
    damage, end = ONE_SECOND_TROUBLES[trouble]
    assert stations.observation_files(kraw, _t(26, 10, 29, 30), end)[0].interval == 1.0  # the folders listed: 1 s
    damage(trees.bkg / "EUREF/highrate/2026/299/k/KRAW00POL_R_20262991030_15M_01S_MO.crx.gz")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        out = stations.download_observations(kraw, _t(26, 10, 29, 30), end, tmp_path / "base")
    assert rinex.header(out).interval == 30.0 and _epochs(out) == 120  # the file of the day
    assert any("trying the 30 s files" in warning for warning in _warnings(caplog))


def test_a_missing_decoder_is_no_reason_to_try_the_thirty_seconds_files(trees, tmp_path, monkeypatch):
    """Without a Hatanaka decoder the 30 s files could not be read either: the error says what to install, at once."""
    monkeypatch.setattr(stations, "MARGIN", timedelta(minutes=1))
    kraw = _made("KRAW00POL", "EPN")
    _one_second_base(trees, kraw)
    calls = _fetches(monkeypatch)

    def no_decoder(path, folder=None):
        raise ValueError(f"{Path(path).name}: {_native.CRINEX_MISSING}")

    monkeypatch.setattr(rinex, "plain", no_decoder)
    with pytest.raises(ValueError, match="native core") as error:
        stations.download_observations(kraw, *SHORT, tmp_path / "base")
    assert "tried next" not in str(error.value) and len(calls) == 1


def test_auto_that_chose_thirty_seconds_has_nothing_to_fall_back_to(trees, tmp_path, monkeypatch):
    kraw = _made("KRAW00POL", "EPN")
    _put(trees.bkg, f"EUREF/nrt/299/10/{KRAW_NAME}",
         _rinex_gz(_t(26, 10), _t(26, 11), skip=[(_t(26, 10, 24), _t(26, 10, 27))]))
    calls = _fetches(monkeypatch)
    with pytest.raises(ValueError, match="10:23:30 to 10:27:00") as error:
        stations.download_observations(kraw, _t(26, 10, 20), _t(26, 10, 35), tmp_path / "base")
    assert "tried next" not in str(error.value) and len(calls) == 1


# --- the navigation files -------------------------------------------------------------------------------------------


def _brdc(trees, day: datetime, **products) -> None:
    """The navigation folder of a day of BKG with the files of the products given (name_source -> text or file)."""
    for name, content in products.items():
        data = content.read_bytes() if isinstance(content, Path) else content
        file = f"{name}_{day:%Y}{_doy(day)}0000_01D_MN.rnx.gz"
        _put(trees.bkg, f"IGS/BRDC/{day:%Y}/{_doy(day)}/{file}", gzip.compress(data))


def test_navigation_for_every_day_of_the_span(trees, tmp_path, monkeypatch):
    """A flight over midnight UTC takes the files of both days, one within a day takes one. The names are those of the
    listing: of the products there, BRDC00WRD (S before R before U)."""
    _brdc(trees, _t(26), BRDC00WRD_S=NAV_A, BRDC00WRD_R=b"the other copy", BRDC00IGS_R=b"not this product",
          BRD400DLR_S=b"nor this one")
    _brdc(trees, _t(27), BRDC00WRD_R=NAV_B)  # the day 300 has only the R copy
    calls = _fetches(monkeypatch)
    folder = tmp_path / "nav"

    files = stations.download_navigation(_t(26, 23, 50), _t(27, 0, 20), folder)
    assert files == [folder / "BRDC00WRD_S_20262990000_01D_MN.rnx", folder / "BRDC00WRD_R_20263000000_01D_MN.rnx"]
    assert files[0].read_bytes() == NAV_A.read_bytes() and files[1].read_bytes() == NAV_B.read_bytes()
    assert [c.rsplit("/", 1)[1] for c in calls] == ["BRDC00WRD_S_20262990000_01D_MN.rnx.gz",
                                                    "BRDC00WRD_R_20263000000_01D_MN.rnx.gz"]
    assert sorted(p.name for p in folder.iterdir()) == sorted(f.name for f in files)  # no scratch file is left
    assert rinex.header(files[0]).kind == "nav"
    # a flight within a day: one file, and the files of days that are over are kept: nothing is fetched again
    one = stations.download_navigation(_t(26, 10, 20), _t(26, 10, 35), tmp_path / "one")
    assert [p.name for p in one] == ["BRDC00WRD_S_20262990000_01D_MN.rnx"] and len(calls) == 2
    assert stations.download_navigation(_t(26, 23, 50), _t(27, 0, 20), folder) == files and len(calls) == 2
    kept = download.gnss_dir() / "navigation"
    assert sorted(p.name for p in kept.iterdir()) == ["BRDC00WRD_R_20263000000_01D_MN.rnx.gz",
                                                      "BRDC00WRD_S_20262990000_01D_MN.rnx.gz"]


def test_navigation_of_a_day_that_is_not_over_is_never_kept(trees, tmp_path, monkeypatch):
    """BKG writes the file of the day all day long: the copy of 09:00 lacks the hours after it. It is fetched every time
    and not kept, so that a copy of the middle of a day is never taken for the whole of it."""
    _brdc(trees, _t(27), BRDC00WRD_S=NAV_A)
    calls = _fetches(monkeypatch)
    _clock(monkeypatch, 10, 27, 9, 0)
    folder = tmp_path / "nav"

    assert stations.download_navigation(_t(27, 8), _t(27, 8, 30), folder)[0].read_bytes() == NAV_A.read_bytes()
    stations.download_navigation(_t(27, 8), _t(27, 8, 30), folder)
    assert len(calls) == 2
    kept = download.gnss_dir() / "navigation"
    assert not kept.exists() or list(kept.iterdir()) == []
    # the file grows during the day: what was fetched last is what is used
    _brdc(trees, _t(27), BRDC00WRD_S=NAV_B)
    assert stations.download_navigation(_t(27, 8), _t(27, 8, 30), folder)[0].read_bytes() == NAV_B.read_bytes()
    # an hour after the day is over it is final: kept
    _clock(monkeypatch, 10, 28, 1, 30)
    stations.download_navigation(_t(27, 8), _t(27, 8, 30), folder)
    stations.download_navigation(_t(27, 8), _t(27, 8, 30), folder)
    assert len(calls) == 4


def test_a_navigation_file_that_is_not_there_says_when_it_comes(trees, tmp_path, monkeypatch):
    _brdc(trees, _t(26), BRDC00WRD_S=NAV_A)
    calls = _fetches(monkeypatch)
    _clock(monkeypatch, 10, 27, 0, 10)
    with pytest.raises(ValueError) as error:  # a flight of the 26th over midnight, ten minutes into the 27th
        stations.download_navigation(_t(26, 23, 50), _t(27, 0, 5), tmp_path / "nav")
    message = str(error.value)
    assert "BRDC00WRD_[RSU]_20263000000_01D_MN.rnx.gz is not in " + trees.url + "IGS/BRDC/2026/300/" in message
    assert "not published yet" in message
    assert "BKG daily files are online about 30 minutes after midnight UTC" in message
    assert calls == []  # the file that is there is not fetched for nothing
    # for a day long past it is only not there
    _clock(monkeypatch, 12, 1, 0, 0)
    with pytest.raises(ValueError) as error:
        stations.download_navigation(_t(25, 10), _t(25, 11), tmp_path / "nav")
    assert "BRDC00WRD_[RSU]_20262980000_01D_MN.rnx.gz is not in" in str(error.value) and "yet" not in str(error.value)


def test_navigation_names_from_a_listing_stay_in_their_folders(trees, tmp_path, monkeypatch):
    good = "BRDC00WRD_S_20262990000_01D_MN.rnx.gz"
    hostile = [good + ":evil", good + "\n", "\n" + good, "D:" + good, "../" + good, "..\\" + good, good + "/..",
               "BRDC00WRD_S_٢٠٢٦٢٩٩٠٠٠٠_01D_MN.rnx.gz", "brdc00wrd_S_20262990000_01D_MN.rnx.gz",
               "BRDC00WRD_X_20262990000_01D_MN.rnx.gz"]
    monkeypatch.setattr(download, "listing", lambda url, progress=None: hostile)
    calls = _fetches(monkeypatch)
    with pytest.raises(ValueError, match="No broadcast navigation file for 2026-10-26"):
        stations.download_navigation(_t(26, 10), _t(26, 11), tmp_path / "nav")
    assert calls == [] and not list(tmp_path.rglob("*evil*")) and not list(tmp_path.rglob("*.gz"))
    # the good name among them is the one that is taken
    _brdc(trees, _t(26), BRDC00WRD_S=NAV_A)
    monkeypatch.setattr(download, "listing", lambda url, progress=None: hostile + [good])
    files = stations.download_navigation(_t(26, 10), _t(26, 11), tmp_path / "nav")
    assert [f.name for f in files] == ["BRDC00WRD_S_20262990000_01D_MN.rnx"] and len(calls) == 1


def test_a_damaged_navigation_file_is_downloaded_again_once(trees, tmp_path, monkeypatch):
    _brdc(trees, _t(26), BRDC00WRD_S=NAV_A)
    path = trees.bkg / "IGS/BRDC/2026/299/BRDC00WRD_S_20262990000_01D_MN.rnx.gz"
    good = path.read_bytes()
    path.write_bytes(good[:-30])  # cut short
    calls = _fetches(monkeypatch)
    with pytest.raises(ValueError, match="BRDC00WRD_S_20262990000_01D_MN.rnx.gz.*again"):
        stations.download_navigation(_t(26, 10), _t(26, 11), tmp_path / "nav")
    assert len(calls) == 2
    assert not list((download.gnss_dir() / "navigation").glob("*.gz")) and not list((tmp_path / "nav").glob("*.rnx"))
    path.write_bytes(good)
    assert stations.download_navigation(_t(26, 10), _t(26, 11), tmp_path / "nav")[0].read_bytes() == NAV_A.read_bytes()


NAV_BAD_FILES = {
    "empty": lambda good: b"",
    "gzip of nothing": lambda good: gzip.compress(b""),
    "gzip of html": lambda good: gzip.compress(_HTML),
    "html": lambda good: _HTML,
    "flipped byte 12": lambda good: _flip(good, 12),
    "cut short": lambda good: good[:-30],
    "gzip of observations": lambda good: gzip.compress((RTKLIB / "07590920.05o").read_bytes()),
}


@pytest.mark.parametrize("damage", list(NAV_BAD_FILES))
def test_a_navigation_file_that_is_no_good_is_fetched_again_once_and_never_kept(trees, tmp_path, monkeypatch, damage):
    """A file that is empty, or of HTML, or of observations, is not navigation messages even if gzip can read it: it is
    fetched again once and refused, naming the file; it does not stay in the folder or in what is kept for later."""
    _brdc(trees, _t(26), BRDC00WRD_S=NAV_A)
    archive = trees.bkg / "IGS/BRDC/2026/299/BRDC00WRD_S_20262990000_01D_MN.rnx.gz"
    good = archive.read_bytes()
    bad = NAV_BAD_FILES[damage](good)
    archive.write_bytes(bad)
    calls = _fetches(monkeypatch)
    kept, folder = download.gnss_dir() / "navigation", tmp_path / "nav"

    def ask():
        return stations.download_navigation(_t(26, 10), _t(26, 11), folder)

    with pytest.raises(ValueError, match="BRDC00WRD_S_20262990000_01D_MN.rnx.gz.*again"):
        ask()
    assert len(calls) == 2
    assert not list(kept.glob("*.gz")) and not list(folder.glob("*.rnx"))  # nothing kept, nothing left
    archive.write_bytes(good)  # the archive is mended
    assert ask()[0].read_bytes() == NAV_A.read_bytes() and len(calls) == 3
    (kept / archive.name).write_bytes(bad)  # a bad copy that was kept is replaced, once
    assert ask()[0].read_bytes() == NAV_A.read_bytes() and len(calls) == 4


def test_navigation_when_bkg_cannot_be_reached(trees, tmp_path, web, monkeypatch):
    monkeypatch.setattr(stations, "BKG_ROOT", "https://igs.bkg.bund.de/root_ftp/")
    web.plan = [urllib.error.URLError(ConnectionRefusedError())] * 6
    with pytest.raises(ValueError, match="BKG could not be reached.*igs.bkg.bund.de"):
        stations.download_navigation(_t(26, 10), _t(26, 11), tmp_path / "nav")
