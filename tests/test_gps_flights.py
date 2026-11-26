"""The GPS of the photos flight by flight: the error per flight, the note that points out a flight whose heights are
off, and one vertical offset per flight estimated with the georeference (``gps_height_by_flight``). A synthetic drone
survey of three flights, the third with its GPS heights 13 m too high (a DJI drone starts its relative altitude
again with every battery). No photos are read: the cameras are placed by hand in an internal frame."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core import geometry
from pyarchinit_photogrammetry.markers import flights
from pyarchinit_photogrammetry.markers.gcp import camera_reference_errors

ORIGIN = np.array([500000.0, 4500000.0, 300.0])
NOISE = np.array([0.4, 0.4, 0.5])  # sd of the GPS of a drone without RTK, metres
BIAS = 13.0


def _survey(sizes=(60, 12, 24), bias=(0.0, 0.0, BIAS), untimed: int = 0, seed: int = 0):
    """A chunk with ``sizes`` photos per flight (2 s apart, 5 minutes between flights), placed in an internal frame
    by a known similarity; their references are the true centres plus noise plus the ``bias`` of their flight in
    height. ``untimed`` more photos without a time. Returns ``(chunk, true world centres by key)``."""
    rng = np.random.default_rng(seed)
    ch = ppg.Document().add_chunk()
    ch.crs = "EPSG:32633"
    world_from_internal = geometry.compose_similarity(
        7.0, geometry.decompose_similarity(geometry.umeyama(np.eye(3), np.eye(3)[[1, 2, 0]], with_scale=False))[1],
        ORIGIN)
    internal_from_world = np.linalg.inv(world_from_internal)
    t = datetime(2026, 5, 3, 9, 0, 0)
    truth, key = {}, 0
    plan = [(f, n) for f, n in enumerate(sizes)] + ([(None, untimed)] if untimed else [])
    for f, n in plan:
        for i in range(n):
            xy = rng.uniform(-150, 150, 2)
            world = ORIGIN + np.array([xy[0], xy[1], 80.0 + rng.normal(0, 2)])
            cam = ppg.core.camera.Camera(key=key, label=f"f{f}_{i}", path=f"f{f}_{i}.jpg", sensor_key=0)
            if f is not None:
                cam.meta["datetime"] = t.strftime("%Y:%m:%d %H:%M:%S")
                t += timedelta(seconds=2)
            pose = np.eye(4)
            pose[:3, 3] = internal_from_world[:3, :3] @ world + internal_from_world[:3, 3]
            cam.transform = pose
            ref = world + rng.normal(0, NOISE) + np.array([0.0, 0.0, bias[f] if f is not None else 0.0])
            cam.reference.location = [float(v) for v in ref]
            cam.reference.accuracy = [5.0, 5.0, 10.0]
            cam.reference.enabled = True
            ch.cameras.append(cam)
            truth[key] = world
            key += 1
        t += timedelta(minutes=5)
    return ch, truth


def _rmse(ch) -> float:
    d = np.array([e["delta"] for e in camera_reference_errors(ch).values() if "delta" in e])
    return float(np.sqrt((d ** 2).sum(axis=1).mean()))


def _noise_rmse() -> float:
    return float(np.sqrt((NOISE ** 2).sum()))


def test_flight_groups_split_on_pauses_over_90_seconds():
    t0 = datetime(2026, 5, 3, 9, 0, 0)
    cams = []
    for k, dt in enumerate([0, 2, 92, 182, 273, 400]):  # 90 s exactly is not a pause, 91 s is
        cams.append(ppg.core.camera.Camera(key=k, label=str(k), path=f"{k}.jpg", sensor_key=0,
                                           meta={"datetime": (t0 + timedelta(seconds=dt)).strftime("%Y:%m:%d %H:%M:%S")}))
    cams.append(ppg.core.camera.Camera(key=9, label="x", path="x.jpg", sensor_key=0))
    assert flights.flight_groups(list(reversed(cams))) == {0: 1, 1: 1, 2: 1, 3: 1, 4: 2, 5: 3, 9: flights.NO_TIME}


def test_without_the_option_the_fit_is_unchanged_and_the_flight_is_pointed_out():
    ch, _truth = _survey()
    before = _survey()[0]
    rep = ch.update_transform()
    assert rep["method"] == "cameras" and "gps_height_by_flight" not in ch.meta
    rmse = _rmse(ch)
    assert rmse > 4.0  # the 13 m of the third flight spread over the fit
    # exactly the fit of before the flights: the similarity of all the positions
    before.update_transform(use_cameras=True)
    assert np.allclose(before.transform, ch.transform)
    rec = rep["gps_flights"]
    assert rec == ch.meta["gps_flights"] and not rec["by_flight"] and rec["offsets"] == {}
    assert [r["flight"] for r in rec["flights"]] == [1, 2, 3]
    assert [r["photos"] for r in rec["flights"]] == [60, 12, 24]
    assert all(r["offset"] is None for r in rec["flights"])
    third = rec["flights"][2]
    assert third["mean"][2] < -BIAS / 2 and third["sd"][2] < 1.5  # below its references, tight within the flight
    assert rec["suggest"] == [3]


def test_the_option_brings_the_error_back_to_the_noise():
    ch, truth = _survey()
    rep = ch.update_transform(gps_height_by_flight=True)
    assert rep["method"] == "cameras+flights" and ch.meta["gps_height_by_flight"] is True
    rec = ch.meta["gps_flights"]
    assert rec["by_flight"] and rec["datum"] == 1  # the biggest flight
    offsets = {int(k): v for k, v in rec["offsets"].items()}
    assert offsets[1] == 0.0
    assert offsets[3] == pytest.approx(BIAS, abs=0.5) and abs(offsets[2]) < 0.6
    assert _rmse(ch) < 1.3 * _noise_rmse()
    # the cameras are where they truly are, not 4 m off
    errs = [np.linalg.norm(ch.to_world(c.center) - truth[c.key]) for c in ch.cameras]
    assert float(np.sqrt(np.mean(np.square(errs)))) < 0.5
    third = next(r for r in rec["flights"] if r["flight"] == 3)
    assert third["offset"] == pytest.approx(offsets[3]) and abs(third["mean"][2]) < 0.5
    e = camera_reference_errors(ch)[ch.cameras[-1].key]
    assert e["offset"] == pytest.approx(offsets[3])  # the reference as read, the error after the offset
    assert e["reference"][2] - (ch.to_world(ch.cameras[-1].center)[2] - e["delta"][2]) == pytest.approx(offsets[3])
    # kept: a later georeference (after an optimisation, a new import) estimates them again
    rep = ch.update_transform()
    assert rep["method"] == "cameras+flights"
    rep = ch.update_transform(gps_height_by_flight=False)
    assert rep["method"] == "cameras" and "gps_height_by_flight" not in ch.meta and _rmse(ch) > 4.0
    assert "offset" not in camera_reference_errors(ch)[ch.cameras[-1].key]


def test_photos_without_a_time_are_one_group_never_shifted():
    ch, _truth = _survey(untimed=8)
    rep = ch.update_transform(gps_height_by_flight=True)
    rec = rep["gps_flights"]
    assert [r["flight"] for r in rec["flights"]] == [1, 2, 3, flights.NO_TIME]
    no_time = rec["flights"][-1]
    assert no_time["photos"] == 8 and no_time["offset"] is None and no_time["start"] is None
    assert str(flights.NO_TIME) not in rec["offsets"]
    assert float(rec["offsets"]["3"]) == pytest.approx(BIAS, abs=0.6)
    assert _rmse(ch) < 1.3 * _noise_rmse()


def test_all_photos_without_a_time_give_no_offsets():
    ch, _truth = _survey(sizes=(), untimed=40)
    rep = ch.update_transform(gps_height_by_flight=True)
    assert rep["method"] == "cameras" and rep["gps_flights"]["offsets"] == {}
    assert [r["flight"] for r in rep["gps_flights"]["flights"]] == [flights.NO_TIME]
    assert rep["gps_flights"]["suggest"] == []


def test_one_flight_gives_no_offsets():
    ch, _truth = _survey(sizes=(50,), bias=(0.0,))
    rep = ch.update_transform(gps_height_by_flight=True)
    assert rep["method"] == "cameras"
    rec = rep["gps_flights"]
    assert rec["offsets"] == {} and rec["camera_offsets"] == {} and rec["suggest"] == []
    assert [r["flight"] for r in rec["flights"]] == [1]
    assert all("offset" not in e for e in camera_reference_errors(ch).values())


def test_control_points_rule_and_the_offsets_are_only_measured():
    ch, _truth = _survey()
    internal_from_world = np.linalg.inv(geometry.compose_similarity(
        7.0, geometry.decompose_similarity(geometry.umeyama(np.eye(3), np.eye(3)[[1, 2, 0]], with_scale=False))[1],
        ORIGIN))
    for i, xy in enumerate([(-120, -120), (130, -100), (0, 140), (-60, 30)]):
        world = ORIGIN + np.array([xy[0], xy[1], 1.0 * i])
        ch.add_marker(f"GCP{i}", internal_from_world[:3, :3] @ world + internal_from_world[:3, 3], world)
        ch.markers[-1].reference.accuracy = [0.01, 0.01, 0.02]
    plain = ch.update_transform()
    assert plain["method"] == "markers" and plain["control_rmse"] < 1e-6
    transform = np.array(ch.transform)
    rep = ch.update_transform(gps_height_by_flight=True)
    assert rep["method"] == "markers" and np.allclose(ch.transform, transform)  # the control points rule
    rec = rep["gps_flights"]
    assert rec["datum"] is None
    offsets = {int(k): v for k, v in rec["offsets"].items()}
    assert offsets[3] == pytest.approx(BIAS, abs=0.4) and abs(offsets[1]) < 0.3 and abs(offsets[2]) < 0.5
    assert _rmse(ch) < 1.3 * _noise_rmse()  # the camera errors after the offsets measured


def test_a_chunk_in_local_coordinates_keeps_no_record():
    ch, _truth = _survey()
    ch.crs = "LOCAL"
    for c in ch.cameras:
        c.reference.location = None
    ch.scalebars = []
    rep = ch.update_transform(gps_height_by_flight=True)
    assert rep["gps_flights"] is None and "gps_flights" not in ch.meta


# --- the report and the Reference pane ---------------------------------------------------------------------------

def test_the_report_gives_the_error_per_flight_and_the_note():
    from pyarchinit_photogrammetry.io import report_content

    ch, _truth = _survey()
    ch.update_transform()
    w = report_content.Words("en")
    sec = report_content._reference(ch, w)
    part = next(b for b in sec.blocks if getattr(b, "title", None) == "GPS error per flight")
    table = next(b for b in part.blocks if isinstance(b, report_content.Table))
    assert [row[0] for row in table.rows] == ["1", "2", "3"] and [row[2] for row in table.rows] == ["60", "12", "24"]
    notes = " ".join(b.text for b in part.blocks if isinstance(b, report_content.Paragraph))
    assert "Flights whose GPS height is off from the others" in notes and ": 3." in notes
    ch.update_transform(gps_height_by_flight=True)
    sec = report_content._reference(ch, w)
    part = next(b for b in sec.blocks if getattr(b, "title", None) == "GPS error per flight")
    notes = " ".join(b.text for b in part.blocks if isinstance(b, report_content.Paragraph))
    assert "datum flight: 1" in notes and "is off" not in notes
    table = next(b for b in part.blocks if isinstance(b, report_content.Table))
    assert float(table.rows[2][6]) == pytest.approx(BIAS, abs=0.5)
    assert "with a height offset per flight" in report_content._method(ch, w, [], camera_reference_errors(ch))


def test_one_flight_has_no_table_in_the_report():
    from pyarchinit_photogrammetry.io import report_content

    ch, _truth = _survey(sizes=(40,), bias=(0.0,))
    ch.update_transform()
    sec = report_content._reference(ch, report_content.Words("en"))
    assert not any(getattr(b, "title", None) == "GPS error per flight" for b in sec.blocks)


def test_cli_lines():
    ch, _truth = _survey()
    ch.update_transform()
    lines = flights.summary_lines(ch.meta["gps_flights"])
    assert lines[0].startswith("GPS flight 1: 60 photos") and "--gps-height-by-flight" in lines[-1]
    ch.update_transform(gps_height_by_flight=True)
    lines = flights.summary_lines(ch.meta["gps_flights"])
    assert "height offset +1" in lines[2] and "datum flight 1" in lines[-1]


@pytest.fixture(scope="module")
def qapp():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_reference_pane_per_flight(qapp):
    from pyarchinit_photogrammetry.gui import i18n
    from pyarchinit_photogrammetry.gui.panels import ReferencePane

    i18n.set_current("it")
    try:
        ch, _truth = _survey()
        ch.update_transform()
        pane = ReferencePane()
        asked = []
        pane.update_requested.connect(lambda: asked.append(True))
        pane.set_chunk(ch)
        assert not pane.flights.isHidden() and pane.flights.rowCount() == 3
        assert pane.flights.item(2, 0).text() == "3" and pane.flights.item(2, 2).text() == "24"
        assert float(pane.flights.item(2, 5).text()) < -BIAS / 2
        assert pane.cams.item(len(ch.cameras) - 1, 10).text() == "3"
        assert "Quota GPS per volo" in pane.summary.text()
        assert pane.c_by_flight.isEnabled() and not pane.c_by_flight.isChecked()
        pane.c_by_flight.setChecked(True)  # by hand: kept in the chunk, georeferenced again
        assert ch.meta["gps_height_by_flight"] is True and asked == [True]
        ch.update_transform()
        pane.set_chunk(ch)
        assert pane.c_by_flight.isChecked() and asked == [True]  # filling the pane asks nothing
        assert float(pane.flights.item(2, 8).text()) == pytest.approx(BIAS, abs=0.5)
        assert abs(float(pane.flights.item(2, 5).text())) < 0.5
        assert "offset di quota per volo" in pane.summary.text()
        one, _t = _survey(sizes=(30,), bias=(0.0,))
        one.update_transform()
        pane.set_chunk(one)
        assert pane.flights.isHidden() and not pane.c_by_flight.isEnabled()
    finally:
        i18n.set_current(None)
