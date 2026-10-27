"""The reference frame and the epoch of every GNSS position: ITRF2020 goes to the European frames (ETRF2000, the frame
of RDN2008 and ETRS89) by the time-dependent Helmert of the EPSG at the epoch of the positions, never by the ballpark
PROJ picks by itself, and to WGS 84 as it is; the positions the program writes carry their frame and epoch, and those
of the projects made before keep converting as they always did."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from pyproj import Transformer

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core import crs
from pyarchinit_photogrammetry.core.camera import Camera, Reference
from pyarchinit_photogrammetry.gnss import apply as gnss_apply
from pyarchinit_photogrammetry.gnss import frames, ppk, quality
from pyarchinit_photogrammetry.markers import gcp
from test_gnss import _snapshot  # the references of a chunk
from test_gnss_ppk import PPK_BASE, PPK_NAV, PPK_ROVER, START_2005, _drone_rows, _options, _pos_text, _ppk_flight
from test_gnss_ppk import fake_rtklib  # noqa: F401 - RTKLIB as a script that logs its runs (a fixture)

ROME = [[12.4964, 41.9028, 100.0]]  # longitude, latitude, height above the ellipsoid


# --- the frames and their coordinate systems --------------------------------------------------------------------


def test_the_frames_of_the_coordinate_systems():
    assert frames.FRAMES == {"ITRF2020": "EPSG:9989", "ETRF2000": "EPSG:7931", "ETRF2014": "EPSG:8403",
                             "ETRF2020": "EPSG:10570", "unknown": "EPSG:4979"}
    assert frames.DYNAMIC == frozenset({"ITRF2020"})
    expected = {
        # ITRF2020 in every form, and the realisations of the IGS that are ITRF2020 for the program (IGS20, IGb20)
        "EPSG:9989": "ITRF2020", "EPSG:9988": "ITRF2020", "EPSG:9990": "ITRF2020", "EPSG:10177": "ITRF2020",
        "EPSG:10784": "ITRF2020",
        # ETRF2000, RDN2008 (its realisation in Italy) and ETRS89, also inside a compound system with heights
        "EPSG:7931": "ETRF2000", "EPSG:6706": "ETRF2000", "EPSG:7792": "ETRF2000", "EPSG:7792+3855": "ETRF2000",
        "EPSG:4937": "ETRF2000", "EPSG:25833": "ETRF2000",
        "EPSG:8403": "ETRF2014", "EPSG:10570": "ETRF2020",
        "EPSG:4979": "WGS84", "EPSG:4326": "WGS84", "EPSG:32633": "WGS84",
        # any other datum, a local frame, no system at all
        "EPSG:3004": None, "LOCAL": None, None: None}
    for system, frame in expected.items():
        assert frames.frame_of(system) == frame, system
        if frame is not None:  # every frame frame_of gives has its system
            assert frames.frame_of(frames.crs_of(frame)) == frame
    for name, system in frames.FRAMES.items():
        assert frames.crs_of(name) == system
    assert frames.crs_of("WGS84") == "EPSG:4979"  # WGS 84 is the frame of the positions nobody knows the frame of
    with pytest.raises(ValueError, match="NAD83"):
        frames.crs_of("NAD83")


def test_decimal_year():
    assert frames.decimal_year(datetime(2026, 1, 1)) == 2026.0
    assert frames.decimal_year(datetime(2026, 10, 27, 12)) == pytest.approx(2026 + 299.5 / 365)  # 2026.82
    assert frames.decimal_year(datetime(2024, 7, 2)) == pytest.approx(2024 + 183 / 366)  # a leap year
    # a time that says its zone is taken in UTC: 1 a.m. on New Year's Day at UTC+2 is still the old year
    assert frames.decimal_year(datetime(2027, 1, 1, 1, tzinfo=timezone(timedelta(hours=2)))) == pytest.approx(
        2026 + (364 + 23 / 24) / 365)


# --- ITRF2020 to the European frames at the epoch ------------------------------------------------------------------


def test_itrf2020_to_rdn2008_at_the_epoch():
    p = frames.transform([[12.4964, 41.9028, 100.0]], "EPSG:9989", "EPSG:7792", epoch=2026.82)[0]
    w = crs.transform_points([[12.4964, 41.9028, 100.0]], "EPSG:4979", "EPSG:7792")[0]
    assert abs((p - w)[0] + 0.815) < 0.005 and abs((p - w)[1] + 0.634) < 0.005  # 1.03 m (the probe of 27 October)
    # crs.transform_points takes the same way for a system in ITRF2020
    assert np.allclose(crs.transform_points(ROME, "EPSG:9989", "EPSG:7792", epoch=2026.82)[0], p, atol=1e-9)


def test_itrf2020_without_epoch_refused():
    with pytest.raises(ValueError, match="epoch"):
        frames.transform(ROME, "EPSG:9989", "EPSG:7792")
    with pytest.raises(ValueError, match="epoch"):
        crs.transform_points(ROME, "EPSG:9989", "EPSG:7792")
    with pytest.raises(ValueError, match="epoch"):  # the other way too
        crs.transform_points([[292335.0, 4642015.0, 100.0]], "EPSG:7792", "EPSG:9989")
    # one epoch per position: none may be missing, and an epoch is a year
    with pytest.raises(ValueError, match="epoch"):
        frames.transform(ROME * 2, "EPSG:9989", "EPSG:7792", epoch=[2026.82, None])
    with pytest.raises(ValueError, match="epoch"):
        frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=1.4e9)  # seconds, not a year


def test_itrf2020_to_etrs89_utm_is_finite():
    p = frames.transform(ROME, "EPSG:9989", "EPSG:25833", epoch=2026.82)[0]
    w = crs.transform_points(ROME, "EPSG:4979", "EPSG:25833")[0]
    assert np.isfinite(p).all()
    shift = (frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=2026.82)[0]
             - crs.transform_points(ROME, "EPSG:4979", "EPSG:7792")[0])
    assert np.allclose((p - w)[:2], shift[:2], atol=0.005)  # the same shift as to RDN2008


def test_itrf2020_to_wgs84_utm_unchanged():
    w = crs.transform_points(ROME, "EPSG:4979", "EPSG:32633")
    # ITRF2020 is WGS 84 for the program: no Helmert, no epoch needed, nothing moves at any epoch
    assert np.allclose(frames.transform(ROME, "EPSG:9989", "EPSG:32633"), w, atol=1e-6)
    assert np.allclose(frames.transform(ROME, "EPSG:9989", "EPSG:32633", epoch=2026.82), w, atol=1e-6)
    assert np.allclose(crs.transform_points(w, "EPSG:32633", "EPSG:9989"), ROME, atol=1e-9)


def test_one_epoch_per_position():
    both = frames.transform(ROME * 2, "EPSG:9989", "EPSG:7792", epoch=[2015.0, 2026.82])
    assert np.allclose(both[0], frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=2015.0)[0], atol=1e-9)
    assert np.allclose(both[1], frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=2026.82)[0], atol=1e-9)
    assert np.linalg.norm(both[1, :2] - both[0, :2]) == pytest.approx(0.32, abs=0.01)  # 2.7 cm a year at Rome


def test_back_to_itrf2020_at_the_epoch():
    there = frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=2026.82)
    back = frames.transform(there, "EPSG:7792", "EPSG:9989", epoch=2026.82)
    assert np.allclose(back[0, :2], ROME[0][:2], atol=1e-9) and back[0, 2] == pytest.approx(100.0, abs=1e-4)
    # a position in IGS20 is a position in ITRF2020
    assert np.allclose(frames.transform(ROME, "EPSG:10177", "EPSG:7792", epoch=2026.82), there, atol=1e-6)
    # between two systems of ITRF2020 nothing but conversions: no epoch
    xyz = frames.transform(ROME, "EPSG:9989", "EPSG:9988")
    assert np.allclose(xyz, Transformer.from_crs("EPSG:9989", "EPSG:9988", always_xy=True).transform(*ROME[0]),
                       atol=1e-6)


def test_other_datums_through_etrf2000_or_wgs84():
    # Monte Mario (Gauss-Boaga): PROJ has a transformation from ETRF2000 in Italy, taken after the Helmert
    etrf = frames.transform(ROME, "EPSG:9989", "EPSG:7931", epoch=2026.82)
    gauss_boaga = Transformer.from_crs("EPSG:7931", "EPSG:3004", always_xy=True).transform(*etrf[0])
    assert np.allclose(frames.transform(ROME, "EPSG:9989", "EPSG:3004", epoch=2026.82)[0], gauss_boaga, atol=1e-6)
    with pytest.raises(ValueError, match="epoch"):
        frames.transform(ROME, "EPSG:9989", "EPSG:3004")
    # ED50 / UTM 33N in Campania: none from ETRF2000 there, the one from WGS 84 (ITRF2020 as it is), without an epoch
    campania = [[14.0, 41.0, 10.0]]
    ed50 = frames.transform(campania, "EPSG:9989", "EPSG:23033")
    assert np.allclose(ed50, crs.transform_points(campania, "EPSG:4979", "EPSG:23033"), atol=1e-6)
    assert np.allclose(frames.transform(ed50, "EPSG:23033", "EPSG:9989"), campania, atol=1e-9)


def test_unknown_datum_refused():
    unknown = "+proj=longlat +ellps=intl +no_defs +type=crs"  # a datum PROJ knows only by a ballpark
    with pytest.raises(ValueError, match="ballpark"):
        frames.transform(ROME, "EPSG:9989", unknown, epoch=2026.82)
    with pytest.raises(ValueError, match="ballpark"):
        crs.transform_points(ROME, unknown, "EPSG:9989", epoch=2026.82)


# --- the positions of the photos -----------------------------------------------------------------------------------


def _camera(key: int, when: str = "2026:10:27 12:00:00") -> Camera:
    return Camera(key=key, label=f"c{key}", path=f"c{key}.jpg", sensor_key=0, meta={"datetime": when})


def test_every_position_with_its_frame_and_epoch():
    cam = _camera(0)
    gnss_apply.set_camera_position(cam, [14.0, 41.0, 100.0], "EPSG:4326", [5.0, 5.0, 10.0], "exif")
    assert (cam.meta["gnss"]["frame"], cam.meta["gnss"]["epoch"]) == ("unknown", None)  # WGS 84: nobody knows
    # a positions file keeps its own system, the frame of its datum
    gnss_apply.set_camera_position(cam, [292335.0, 4642015.0, 100.0], "EPSG:7792", [0.02] * 3, "positions_file")
    assert cam.meta["reference_crs"] == "EPSG:7792" and cam.meta["gnss"]["frame"] == "ETRF2000"
    gnss_apply.set_camera_position(cam, [12.5, 41.9, 100.0], frames.crs_of("ITRF2020"), [0.02] * 3, "ppk",
                                   frame="ITRF2020", epoch=2026.82)
    assert cam.meta["reference_crs"] == "EPSG:9989"
    assert (cam.meta["gnss"]["frame"], cam.meta["gnss"]["epoch"]) == ("ITRF2020", 2026.82)
    before = (cam.reference, json.dumps(cam.meta))
    with pytest.raises(ValueError, match="epoch"):  # an ITRF2020 position without its epoch fails where it is written
        gnss_apply.set_camera_position(cam, [12.5, 41.9, 100.0], "EPSG:9989", [0.02] * 3, "ppk")
    with pytest.raises(ValueError, match="ETRF2000"):  # a frame that is not the one of the system
        gnss_apply.set_camera_position(cam, [12.5, 41.9, 100.0], "EPSG:4979", [0.02] * 3, "ppk", frame="ETRF2000",
                                       epoch=2026.82)
    with pytest.raises(ValueError, match="NAD83"):
        gnss_apply.set_camera_position(cam, [12.5, 41.9, 100.0], "EPSG:4979", [0.02] * 3, "ppk", frame="NAD83")
    assert (cam.reference, json.dumps(cam.meta)) == before  # nothing changed


def test_camera_references_use_each_epoch():
    ch = ppg.Document().add_chunk()
    ch.crs = "EPSG:7792"
    epochs = (2015.0, 2026.82)
    for key, epoch in enumerate(epochs):  # the same place measured 11.8 years apart
        cam = _camera(key)
        gnss_apply.set_camera_position(cam, list(ROME[0]), "EPSG:9989", [0.02, 0.02, 0.04], "ppk", status="fix",
                                       height="ellipsoidal", frame="ITRF2020", epoch=epoch)
        ch.cameras.append(cam)
    positions, system = gcp.camera_references(ch, ch.cameras, ch.crs)
    errors = gcp.camera_reference_errors(ch)
    assert system == "EPSG:7792"
    for cam, epoch in zip(ch.cameras, epochs):
        expected = frames.transform(ROME, "EPSG:9989", "EPSG:7792", epoch=epoch)[0]
        assert np.allclose(positions[cam.key], expected, atol=1e-6)
        assert np.allclose(errors[cam.key]["reference"], expected, atol=1e-6)
    assert np.linalg.norm(positions[1][:2] - positions[0][:2]) == pytest.approx(0.32, abs=0.01)


def test_old_projects_as_before():
    """Positions written before the frames - a record without frame and epoch, or no record at all - convert as they
    always did: PROJ from their system to the one of the chunk."""
    ch = ppg.Document().add_chunk()
    ch.crs = "EPSG:7792"
    places = {"EPSG:4326": [12.4964, 41.9028, 100.0], "EPSG:4979": [12.4970, 41.9030, 101.0],
              "EPSG:32633": [292400.0, 4642100.0, 102.0], "EPSG:3004": [2312339.2, 4642025.8, 103.0]}
    for key, (system, place) in enumerate(places.items()):
        cam = _camera(key)
        cam.reference = Reference(location=list(place), accuracy=[1.0, 1.0, 2.0], enabled=True)
        cam.meta["reference_crs"] = system
        if key < 3:  # the record of a position written by the 1.6.0
            cam.meta["gnss"] = {"source": "exif", "status": None, "accuracy": [1.0, 1.0, 2.0], "height": "exif"}
        ch.cameras.append(cam)
    positions, _system = gcp.camera_references(ch, ch.cameras, ch.crs)
    errors = gcp.camera_reference_errors(ch)
    for cam, (system, place) in zip(ch.cameras, places.items()):
        before = Transformer.from_crs(system, "EPSG:7792", always_xy=True).transform(*place)
        assert np.array_equal(positions[cam.key], before) and np.array_equal(errors[cam.key]["reference"], before)
    (flight,) = quality.summary(ch)
    assert flight["frames"] == {"unknown": 4}


def test_the_frames_of_a_flight():
    ch = ppg.Document().add_chunk()
    ch.cameras = [_camera(k, f"2026:10:27 12:0{k}:00") for k in range(4)]
    gnss_apply.set_camera_position(ch.cameras[0], list(ROME[0]), "EPSG:9989", [0.02] * 3, "ppk", frame="ITRF2020",
                                   epoch=2026.82)
    gnss_apply.set_camera_position(ch.cameras[1], [292335.0, 4642015.0, 100.0], "EPSG:7792", [0.02] * 3,
                                   "positions_file")
    gnss_apply.set_camera_position(ch.cameras[2], [12.5, 41.9, 100.0], "EPSG:4326", [5.0, 5.0, 10.0], "exif")
    (flight,) = quality.summary(ch)  # the photo 3 has no position: in none of them
    assert flight["frames"] == {"ITRF2020": 1, "ETRF2000": 1, "unknown": 1}


# --- the base of a PPK solution and the photos it places -----------------------------------------------------------


def test_solve_with_the_position_of_a_station(tmp_path, request):
    runs = request.getfixturevalue("fake_rtklib")
    station = ppk.BasePosition(35.16, 139.61, 70.0, "station", frame="ITRF2020", epoch=2005.25,
                               sd=(0.002, 0.002, 0.005), detail={"station": "0759"})
    solution = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], station, folder=tmp_path / "work")
    options = _options(runs()[-1]["options"])
    assert [float(options[f"ant2-pos{i}"]) for i in (1, 2, 3)] == [35.16, 139.61, 70.0]
    base = solution.base
    assert base["position"] == [139.61, 35.16, 70.0] and base["position_source"] == "station"
    assert (base["frame"], base["epoch"], base["sd"], base["detail"]) == ("ITRF2020", 2005.25, [0.002, 0.002, 0.005],
                                                                          {"station": "0759"})
    # three numbers are typed by the user, in a frame nobody knows
    typed = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], (35.16, 139.61, 70.0), folder=tmp_path / "work").base
    assert typed["position_source"] == "typed" and typed["position"] == [139.61, 35.16, 70.0]
    assert (typed["frame"], typed["epoch"], typed["sd"], typed["detail"]) == ("unknown", None, None, {})
    header = ppk.solve(PPK_ROVER, PPK_BASE, [PPK_NAV], folder=tmp_path / "work").base
    assert header["position_source"] == "header" and header["frame"] == "unknown" and header["epoch"] is None
    # a base in ITRF2020 without its epoch, a source or a frame the program does not know, a latitude off the earth
    with pytest.raises(ValueError, match="epoch"):
        ppk.BasePosition(35.16, 139.61, 70.0, "station", frame="ITRF2020")
    with pytest.raises(ValueError, match="guess"):
        ppk.BasePosition(35.16, 139.61, 70.0, "guess")
    with pytest.raises(ValueError, match="NAD83"):
        ppk.BasePosition(35.16, 139.61, 70.0, "typed", frame="NAD83")
    with pytest.raises(ValueError, match="latitude"):
        ppk.BasePosition(135.16, 139.61, 70.0, "typed")


def test_apply_ppk_writes_frame_and_epoch(tmp_path):
    pos = tmp_path / "drone.pos"
    pos.write_text(_pos_text(rows=_drone_rows()), newline="")
    solution = ppk.read_pos(pos)
    shots = [(i, START_2005 + 30.0 * i, (0, 0, 150)) for i in (2, 3, 4)]

    # a solution read from a file knows no base: its positions in a frame nobody knows, at the epochs of the shots
    folder = _ppk_flight(tmp_path / "file", shots)
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    assert gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)["matched"] == 3
    for cam in ch.cameras:
        g = cam.meta["gnss"]
        assert cam.meta["reference_crs"] == "EPSG:4979" and g["frame"] == "unknown"
        assert g["epoch"] == pytest.approx(frames.decimal_year(datetime.fromisoformat(g["utc"])), abs=1e-12)

    # a base in ITRF2020 at the epoch of the flight (a permanent station): the photos in ITRF2020 at their shots
    solution.base = {"name": "STAZ", "position": [139.62, 35.13, 60.0], "position_source": "station",
                     "frame": "ITRF2020", "epoch": 2005.25, "sd": [0.002, 0.002, 0.005], "detail": {"station": "STAZ"}}
    folder = _ppk_flight(tmp_path / "station", shots)
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    assert gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)["matched"] == 3
    epochs = []
    for cam in ch.cameras:
        g = cam.meta["gnss"]
        assert cam.meta["reference_crs"] == "EPSG:9989" and g["frame"] == "ITRF2020" and g["base"] == solution.base
        assert g["epoch"] == pytest.approx(frames.decimal_year(datetime.fromisoformat(g["utc"])), abs=1e-12)
        epochs.append(g["epoch"])
    assert epochs == sorted(epochs) and 2005.249 < epochs[0] < epochs[-1] < 2005.250  # 30 s apart, 2 April 2005
    json.dumps(ch.meta)
    for cam in ch.cameras:
        json.dumps(cam.meta)  # saved with the project

    # a frame the program does not know: refused, nothing changed
    solution.base = {**solution.base, "frame": "NAD83"}
    before = _snapshot(ch)
    with pytest.raises(ValueError, match="NAD83"):
        gnss_apply.apply_ppk(ch, folder / "Timestamp.MRK", solution)
    assert _snapshot(ch) == before
