"""Permanent GNSS stations (EPN, IGS, RING): the list, the nearest ones and their coordinates at the epoch of a flight,
and the download helpers they use. The services are never reached: the URL constants of ``gnss/stations.py`` point at
files (``file://``) - the excerpts of tests/data/gnss, captured on 2026-10-27 from M3G, the IGS and RING, and the EPN
multi-year solution, which is synthetic because the EPN Central Bureau did not answer (see the README there)."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyproj
import pytest

from pyarchinit_photogrammetry import __version__
from pyarchinit_photogrammetry.core.progress import Cancelled, Progress
from pyarchinit_photogrammetry.gnss import download, stations

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

    def listing(url):
        if "C2401" in url:
            raise download.DownloadError("could not be listed", url)
        return real(url)

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

        def listing(url):
            if re.search(r"/C\d{4}/?$", url):
                raise download.DownloadError("no answer", url)
            return real(url)

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


def test_a_file_that_is_not_sinex_is_skipped_with_a_warning(archives, caplog):
    """A file of the EPN that is not laid out as the SINEX ones is not guessed at: the next source answers."""
    for solution in archives.epn.glob("C*/*_SOL.SSC"):
        solution.write_text("DOMES NB. SITE NAME TECH. ID. X/Vx Y/Vy Z/Vz SOLN DATA_START DATA_END\n")
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        c = stations.coordinates(_station("M0SE00ITA"), 2026.5)
    assert c.source == "IGS"  # M0SE00ITA is in the file of the IGS as well
    assert any("not a SINEX" in w and "_SOL.SSC" in w for w in _warnings(caplog))


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
