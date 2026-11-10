"""Shots of any source paired with the photos and given to them: the lever arm in the body of the drone turned with its
attitude (only its vertical part without one), the photos paired by the number in their names, by their times (double
and missing events, two flights in one folder) or by order, the latency of the camera, and the DJI Timestamp.MRK whose
results stay those of the code before."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.gnss import apply as gnss_apply
from pyarchinit_photogrammetry.gnss import dji, lever, ppk, shots
from pyarchinit_photogrammetry.gnss.shots import Shot, ShotSet
import gnss_mrk_projects
from gnss_helpers import enu, jpeg, snapshot

GPS_EPOCH = datetime(1980, 1, 6)
LEAP_2024 = 18
ZONE = timedelta(hours=2)  # the camera on the local time of Italy in May
DRIFT_S = 37.6  # and its clock 37.6 s ahead
START = datetime(2024, 5, 12, 8, 30, 15, 400000)  # UTC of the first exposure


def _gps(utc: datetime) -> float:
    return (utc - GPS_EPOCH).total_seconds() + LEAP_2024


def _photo_time(utc: datetime) -> datetime:
    """What a camera writes in the EXIF of a photo exposed at ``utc``: its own clock, to the second."""
    local = utc + ZONE + timedelta(seconds=DRIFT_S)
    return local.replace(microsecond=0)


def _exposures(count: int, seed: int = 4) -> list[datetime]:
    """The UTC times of ``count`` exposures of a survey: about every 2 s, a turn every 40 photos."""
    rng = np.random.default_rng(seed)
    times, t = [], START
    for k in range(count):
        times.append(t)
        t += timedelta(seconds=float(rng.uniform(1.7, 2.3)) + (9.0 if k % 40 == 39 else 0.0))
    return times


def _photos(folder: Path, names, utcs) -> ppg.Chunk:
    folder.mkdir(parents=True, exist_ok=True)
    for name, utc in zip(names, utcs):
        jpeg(folder / name, None, when=None if utc is None else _photo_time(utc))
    ch = ppg.Document().add_chunk()
    ch.add_photos([folder])
    return ch


def _events(utcs, kind="rinex_events", name="rover.obs", **shot) -> ShotSet:
    return ShotSet(shots=[Shot(index=i, gps_time=_gps(t), source={"kind": kind}, **shot)
                          for i, t in enumerate(utcs, 1)], kind=kind, name=name)


# --- the lever arm -----------------------------------------------------------------------------------------------


def test_body_lever_with_attitude():
    # level, heading north: the body is north, east, down
    assert lever.body_to_ned((100, 20, 150), 0, 0, 0) == pytest.approx((100, 20, 150), abs=1e-9)
    # heading east (yaw 90, clockwise from north): forward is east, right is south
    assert lever.body_to_ned((100, 0, 0), 0, 0, 90) == pytest.approx((0, 100, 0), abs=1e-9)
    assert lever.body_to_ned((0, 100, 0), 0, 0, 90) == pytest.approx((-100, 0, 0), abs=1e-9)
    # nose up 10 degrees: a camera forward of the antenna is higher (down less than 0)
    n, e, d = lever.body_to_ned((100, 0, 0), 0, 10, 0)
    assert (n, e, d) == pytest.approx((100 * math.cos(math.radians(10)), 0, -100 * math.sin(math.radians(10))))
    # right wing down 20 degrees: a camera below the antenna swings to the left (west, heading north)
    n, e, d = lever.body_to_ned((0, 0, 100), 20, 0, 0)
    assert (n, e, d) == pytest.approx((0, -100 * math.sin(math.radians(20)), 100 * math.cos(math.radians(20))))

    # the quaternion of PX4 (Hamilton w, x, y, z, body to NED) gives the same angles: yaw, then pitch, then roll
    def q_axis(axis, deg):
        half = math.radians(deg) / 2
        v = [0.0, 0.0, 0.0]
        v[axis] = math.sin(half)
        return np.array([math.cos(half), *v])

    def q_mul(a, b):
        w1, x1, y1, z1 = a
        w2, x2, y2, z2 = b
        return np.array([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                         w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2])

    for roll, pitch, yaw in ((5.0, -8.0, 135.0), (-12.0, 3.0, -60.0), (0.0, 0.0, 359.0)):
        q = q_mul(q_mul(q_axis(2, yaw), q_axis(1, pitch)), q_axis(0, roll))
        r, p, y = lever.quaternion_to_euler(*q)
        assert (r, p) == pytest.approx((roll, pitch), abs=1e-9)
        assert (y - yaw + 180) % 360 - 180 == pytest.approx(0, abs=1e-9)
        # the quaternion turns a vector of the body into NED as body_to_ned does
        v = np.array([0.0, 120.0, -40.0, 190.0])
        conj = q * np.array([1, -1, -1, -1])
        assert q_mul(q_mul(q, v), conj)[1:] == pytest.approx(lever.body_to_ned(v[1:], r, p, y), abs=1e-9)

    # the camera from the antenna: 10 cm forward and 20 cm below, heading east, level
    lon, lat, h, note = lever.camera_from_antenna(41.134, 14.266, 94.35, body_frd_mm=(100, 0, 200),
                                                  attitude=(0, 0, 90))
    assert note is None
    # (the helper takes the Earth for a sphere: half a millimetre in 10 cm)
    assert enu(np.array([[lat, lon, h]]), (41.134, 14.266, 94.35))[0] == pytest.approx([0.1, 0.0, -0.2], abs=5e-4)
    # an arm already north, east, down (the MRK): as dji.lever_arm
    assert lever.camera_from_antenna(41.134, 14.266, 94.35, ned_mm=(120, -330, 189)) == (
        *dji.lever_arm(41.134, 14.266, 94.35, (120, -330, 189)), None)
    with pytest.raises(ValueError, match="both"):
        lever.camera_from_antenna(41.134, 14.266, 94.35, ned_mm=(0, 0, 1), body_frd_mm=(0, 0, 1))


def test_without_attitude_only_vertical(tmp_path):
    lon, lat, h, note = lever.camera_from_antenna(41.134, 14.266, 94.35, body_frd_mm=(100, 50, 200))
    assert note == "vertical only: no attitude"
    assert (lon, lat) == pytest.approx((14.266, 41.134), abs=1e-12) and h == pytest.approx(94.15, abs=1e-9)
    # no arm at all: the antenna
    assert lever.camera_from_antenna(41.134, 14.266, 94.35) == (14.266, 41.134, 94.35, None)

    # apply_shots: shots of a log with the antenna and no attitude, the arm in the body: only down, and a warning
    utcs = _exposures(4)
    ch = _photos(tmp_path / "flat", [f"IMG_{i:04d}.JPG" for i in (1, 2, 3, 4)], utcs)
    shotset = _events(utcs, kind="ardupilot_cam", name="flight.bin", antenna=(41.134, 14.266, 94.35),
                      height="ellipsoidal", status="fix", std=(0.01, 0.01, 0.02))
    result = gnss_apply.apply_shots(ch, shotset, tmp_path / "flat", body_lever_mm=(100, 50, 200))
    assert result["matched"] == 4 and result["by"] == "number"
    assert any("vertical" in w and "4 photos" in w for w in result["warnings"])
    cam = ch.cameras[0]
    assert cam.reference.location == pytest.approx([14.266, 41.134, 94.15], abs=1e-9)
    g = cam.meta["gnss"]
    assert g["source"] == "ardupilot_cam" and g["height"] == "ellipsoidal" and g["status"] == "fix"
    assert g["lever"] == {"body_frd_mm": [100, 50, 200], "attitude": None, "note": "vertical only: no attitude"}
    assert g["shots"]["kind"] == "ardupilot_cam" and g["shots"]["index"] == 1 and g["shots"]["latency_s"] == 0.0
    assert cam.reference.accuracy == pytest.approx([0.02, 0.02, 0.04])

    # with the attitude: forward is east at yaw 90
    shotset = _events(utcs, kind="ardupilot_cam", name="flight.bin", antenna=(41.134, 14.266, 94.35),
                      height="ellipsoidal", status="fix", std=(0.01, 0.01, 0.02), attitude=(0.0, 0.0, 90.0))
    result = gnss_apply.apply_shots(ch, shotset, tmp_path / "flat", body_lever_mm=(100, 0, 0))
    assert not any("vertical" in w for w in result["warnings"])
    assert enu(np.array([[ch.cameras[0].reference.location[i] for i in (1, 0, 2)]]),
               (41.134, 14.266, 94.35))[0] == pytest.approx([0.1, 0.0, 0.0], abs=5e-4)

    # heights above the sea level stay so: "msl", never taken for ellipsoidal ones
    shotset = _events(utcs, kind="ardupilot_cam", name="flight.bin", antenna=(41.134, 14.266, 52.0), height="msl")
    gnss_apply.apply_shots(ch, shotset, tmp_path / "flat")
    assert ch.cameras[0].meta["gnss"]["height"] == "msl" and ch.cameras[0].meta["reference_crs"] == "EPSG:4326"
    # shots without the antenna (events of a receiver): only the PPK can place them
    with pytest.raises(ValueError, match="antenna"):
        gnss_apply.apply_shots(ch, _events(utcs), tmp_path / "flat")


# --- pairing -----------------------------------------------------------------------------------------------------


def test_photo_numbers():
    assert [shots.photo_number(n) for n in ("IMG_0123.JPG", "DSC01234.JPG", "0123.JPG", "DJI_0007.JPG",
                                            "DJI_20240512103015_0001_V.JPG", "a.JPG", "P1010042.JPG")] == [
        123, 1234, 123, 7, 1, None, 1010042]


def test_from_mrk(tmp_path):
    (tmp_path / "Timestamp.MRK").write_text(
        "7\t357163.425624\t[2087]\t    -2,N\t    11,E\t   189,V\t22.53974306,Lat\t113.94255764,Lon\t17.522,Ellh\t"
        "0.012012, 0.010939, 0.022478\t34,Q\n")
    shotset = shots.from_mrk(tmp_path / "Timestamp.MRK")
    (mrk,) = dji.read_mrk(tmp_path / "Timestamp.MRK")
    assert (shotset.kind, shotset.name) == ("mrk", "Timestamp.MRK")
    (shot,) = shotset.shots
    assert (shot.index, shot.gps_time, shot.status, shot.height) == (7, mrk.gps_time, "float", "ellipsoidal")
    assert shot.antenna == (mrk.lat, mrk.lon, mrk.ellh) and shot.lever_ned_mm == mrk.offset_ned_mm
    assert shot.std == mrk.std and shots.utc(shot) == mrk.utc


def test_pair_by_number_generic_names(tmp_path):
    utcs = _exposures(6)
    # the log has a sixth shot (a test on the ground) and the camera numbers its photos IMG_0001...
    names = [f"IMG_{i:04d}.JPG" for i in range(1, 6)]
    ch = _photos(tmp_path / "a", names, utcs[:5])
    pairing = shots.pair(ch, _events(utcs, kind="ardupilot_cam"), tmp_path / "a")
    assert pairing.by == "number" and len(pairing.kept) == 5
    assert [(f.name, s.index) for f, s, _cam in pairing.kept] == [(n, i) for i, n in enumerate(names, 1)]
    assert [s.index for s in pairing.extra_shots] == [6] and pairing.photos_without_shot == []
    assert pairing.clock_s == pytest.approx(ZONE.total_seconds() + DRIFT_S, abs=1.0)
    # other cameras: DSC01234, 0123.JPG
    for folder, pattern in (("dsc", "DSC0{:04d}.JPG"), ("bare", "{:04d}.JPG")):
        ch = _photos(tmp_path / folder, [pattern.format(i) for i in range(1, 6)], utcs[:5])
        pairing = shots.pair(ch, _events(utcs, kind="px4_capture"), tmp_path / folder)
        assert pairing.by == "number" and len(pairing.kept) == 5
    # the numbers of the photos are those of another counter: fewer than half coincide, the times pair them
    utcs = _exposures(61)
    ch = _photos(tmp_path / "c", [f"IMG_{i:04d}.JPG" for i in range(5001, 5061)], utcs[:60])
    pairing = shots.pair(ch, _events(utcs, kind="ardupilot_cam"), tmp_path / "c")
    assert pairing.by == "time" and [s.index for _f, s, _c in pairing.kept] == list(range(1, 61))
    # the event counter of a receiver is no image number: never paired by number in "auto"
    names = [f"IMG_{i:04d}.JPG" for i in range(1, 61)]
    ch = _photos(tmp_path / "d", names, utcs[1:61])
    pairing = shots.pair(ch, _events(utcs), tmp_path / "d")
    assert pairing.by == "time" and [s.index for _f, s, _c in pairing.kept] == list(range(2, 62))
    assert [s.index for s in pairing.extra_shots] == [1]
    # forced by number: what the user asked
    assert shots.pair(ch, _events(utcs[1:61]), tmp_path / "d", by="number").by == "number"
    with pytest.raises(ValueError, match="by"):
        shots.pair(ch, _events(utcs), tmp_path / "d", by="colour")


def test_pair_by_time_with_double_and_missing_events(tmp_path):
    exposures = _exposures(411)
    names = [f"IMG_{i:04d}.JPG" for i in range(1, 412)]
    ch = _photos(tmp_path / "survey", names, exposures)
    missing = {99, 299}  # the receiver lost the events of the 100th and the 300th photo
    doubles = {49, 199, 349}  # a bouncing contact: a second event 50 ms after these
    events = []
    for k, t in enumerate(exposures):
        if k in missing:
            continue
        events.append(t)
        if k in doubles:
            events.append(t + timedelta(milliseconds=50))
    assert len(events) == 412
    shotset = _events(events, name="rover.obs")
    pairing = shots.pair(ch, shotset, tmp_path / "survey")
    assert pairing.by == "time" and len(pairing.kept) == 409
    assert pairing.clock_s == pytest.approx(ZONE.total_seconds() + DRIFT_S - 0.5, abs=0.6)
    truth = {names[k]: _gps(t) for k, t in enumerate(exposures)}
    assert all(shot.gps_time == truth[f.name] for f, shot, _cam in pairing.kept)
    assert [f.name for f in pairing.photos_without_shot] == ["IMG_0100.JPG", "IMG_0300.JPG"]
    # the second event of each double left out, the first one kept
    assert pairing.doubles == 3 and pairing.extra_shots == []
    assert sorted(s.gps_time for s in pairing.left_doubles) == sorted(_gps(exposures[k]) + 0.05 for k in doubles)
    # never by order when the counts differ
    with pytest.raises(ValueError, match="order"):
        shots.pair(ch, shotset, tmp_path / "survey", by="order")

    # photos without a date and counts that differ: nothing pairs them
    ch = _photos(tmp_path / "undated", names[:5], [None] * 5)
    with pytest.raises(ValueError, match="cannot be paired"):
        shots.pair(ch, _events(exposures[:6]), tmp_path / "undated")
    # as many as the shots: by order
    pairing = shots.pair(ch, _events(exposures[:5]), tmp_path / "undated")
    assert pairing.by == "order" and len(pairing.kept) == 5 and pairing.clock_s is None


def test_two_flights_in_one_folder_refused(tmp_path):
    exposures = _exposures(80)
    names = [f"IMG_{i:04d}.JPG" for i in range(1, 161)]
    # the second flight exactly an hour later, the same pattern of shots: the times fit both flights
    later = [t + timedelta(hours=1) for t in exposures]
    ch = _photos(tmp_path / "two", names, exposures + later)
    before = snapshot(ch)
    with pytest.raises(ValueError, match="more than one flight"):
        shots.pair(ch, _events(exposures), tmp_path / "two")
    with pytest.raises(ValueError, match="more than one flight"):
        gnss_apply.apply_shots(ch, _events(exposures, antenna=(41.1, 14.2, 90.0), height="ellipsoidal"),
                               tmp_path / "two")
    assert snapshot(ch) == before
    # the second flight 7 minutes after the first: on no time zone, its photos simply have no shot
    later = [t + timedelta(minutes=7) + (exposures[-1] - exposures[0]) for t in exposures]
    ch = _photos(tmp_path / "next", names, exposures + later)
    pairing = shots.pair(ch, _events(exposures), tmp_path / "next")
    assert len(pairing.kept) == 80 and [f.name for f in pairing.photos_without_shot] == names[80:]
    # the shots of another flight: the photos agree on no time zone
    ch = _photos(tmp_path / "other", names[:80], [t + timedelta(minutes=7) for t in exposures])
    with pytest.raises(ValueError, match="times"):
        shots.pair(ch, _events(exposures), tmp_path / "other")


def _moving_solution(t0: float, seconds: float, path: Path) -> ppk.Solution:
    """A drone flying east at 10 m/s recorded at 5 Hz from ``t0`` for ``seconds``: fix all along."""
    times = t0 + np.arange(0.0, seconds, 0.2)
    east = math.degrees(10.0 / (6378137.0 * math.cos(math.radians(41.134))))
    llh = np.column_stack([np.full(len(times), 41.134), 14.266 + east * (times - t0), np.full(len(times), 94.35)])
    count = len(times)
    return ppk.Solution(times=times, llh=llh, q=np.ones(count, dtype=int), ns=np.full(count, 8),
                        sd=np.array([[0.003, 0.004, 0.009]] * count), base={}, path=path)


def test_latency_moves_the_time(tmp_path):
    exposures = _exposures(60)
    ch = _photos(tmp_path / "f", [f"IMG_{i:04d}.JPG" for i in range(1, 61)], exposures)
    solution = _moving_solution(_gps(exposures[0]) - 5.0, 160.0, tmp_path / "rover.pos")
    shotset = _events(exposures, name="rover.obs")
    result = gnss_apply.apply_ppk_shots(ch, shotset, solution, tmp_path / "f")
    assert result["matched"] == 60 and result["by"] == "time"
    at_zero = [list(c.reference.location) for c in ch.cameras]
    result = gnss_apply.apply_ppk_shots(ch, shotset, solution, tmp_path / "f", latency_s=0.05)
    assert result["matched"] == 60
    for before, cam in zip(at_zero, ch.cameras):
        moved = enu(np.array([[cam.reference.location[i] for i in (1, 0, 2)]]), (before[1], before[0], before[2]))[0]
        assert moved == pytest.approx([0.5, 0.0, 0.0], abs=2e-3)  # 50 ms at 10 m/s
        g = cam.meta["gnss"]
        assert g["source"] == "ppk" and g["shots"]["latency_s"] == 0.05 and g["shots"]["kind"] == "rinex_events"
    # the run is remembered with its shots, never as an MRK
    last = ppk.last_run(ch)
    assert last["mrk"] is None and last["shots"]["kind"] == "rinex_events" and last["shots"]["latency_s"] == 0.05
    # the body lever arm without an attitude: only down, and the warning
    result = gnss_apply.apply_ppk_shots(ch, shotset, solution, tmp_path / "f", body_lever_mm=(0, 0, 250))
    assert any("vertical" in w for w in result["warnings"])
    assert ch.cameras[0].reference.location[2] == pytest.approx(94.35 - 0.25, abs=1e-6)
    # an arm of the body with shots that carry their own (an MRK): refused
    mrk = tmp_path / "Timestamp.MRK"
    mrk.write_text(gnss_mrk_projects._line(1, exposures[0].replace(tzinfo=timezone.utc), (0, 0, 150), 41.134, 50))
    with pytest.raises(ValueError, match="lever arm"):
        gnss_apply.apply_shots(ch, shots.from_mrk(mrk), tmp_path / "f", body_lever_mm=(0, 0, 100))


def test_mrk_behaviour_unchanged(tmp_path):
    expected = json.loads(gnss_mrk_projects.SNAPSHOT.read_text())
    got = json.loads(json.dumps(gnss_mrk_projects.build(tmp_path.resolve()), sort_keys=True))
    assert sorted(got) == sorted(expected)
    for key in expected:
        assert got[key] == expected[key], key
